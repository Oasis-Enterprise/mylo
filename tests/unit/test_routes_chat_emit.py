# Copyright 2026 Maxwell Monson / Oasis Enterprise LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

from mylo.server.routes_chat import _safe_emit


class _ClosedResponse:
    async def write(self, _data: bytes) -> None:
        raise ConnectionResetError("Cannot write to closing transport")


class _OpenResponse:
    def __init__(self) -> None:
        self.written: list[bytes] = []

    async def write(self, data: bytes) -> None:
        self.written.append(data)


async def test_safe_emit_swallows_closed_transport() -> None:
    # Must not raise even though the underlying transport is gone.
    await _safe_emit(_ClosedResponse(), "text", {"text": "hi"})


async def test_safe_emit_writes_sse_frame_when_open() -> None:
    resp = _OpenResponse()
    await _safe_emit(resp, "text", {"text": "hi"})
    assert resp.written
    assert resp.written[0].startswith(b"event: text\n")


class _CountingResponse:
    def __init__(self) -> None:
        self.written: list[bytes] = []

    async def write(self, data: bytes) -> None:
        self.written.append(data)


async def test_heartbeat_writes_sse_comments_until_cancelled() -> None:
    import asyncio

    from mylo.server.routes_chat import _heartbeat

    resp = _CountingResponse()
    task = asyncio.create_task(_heartbeat(resp, interval=0.01))
    await asyncio.sleep(0.06)
    task.cancel()
    await task  # _heartbeat swallows its own cancellation
    assert len(resp.written) >= 3
    assert all(chunk == b": ping\n\n" for chunk in resp.written)


async def test_heartbeat_stops_when_transport_closes() -> None:
    import asyncio

    from mylo.server.routes_chat import _heartbeat

    task = asyncio.create_task(_heartbeat(_ClosedResponse(), interval=0.01))
    await asyncio.wait_for(task, timeout=1.0)  # returns on its own, no exception


# ─── Event mapping + cancel endpoint ────────────────────────────────────────


def test_sse_for_maps_new_events() -> None:
    from mylo.llm.tool_loop import StatusEvent, TextDeltaEvent, TextEvent
    from mylo.server.routes_chat import _sse_for

    assert _sse_for(TextDeltaEvent(text="x")) == ("text_delta", {"text": "x"})
    assert _sse_for(StatusEvent(phase="tool", tool="query_logs", label="Checking the logs")) == (
        "status",
        {"phase": "tool", "tool": "query_logs", "label": "Checking the logs"},
    )
    assert _sse_for(TextEvent(text="hi")) == ("text", {"text": "hi"})


async def test_cancel_endpoint_sets_event_only_while_turn_active(tmp_path) -> None:
    import json
    from types import SimpleNamespace

    from mylo.conversation.manager import ConversationManager
    from mylo.conversation.storage import ConversationStorage
    from mylo.server.app import AppKeys
    from mylo.server.routes_chat import _handle_cancel

    storage = ConversationStorage(tmp_path / "c.db")
    await storage.init()
    conv = ConversationManager(storage=storage, conversation_id="t")
    request = SimpleNamespace(app={AppKeys.CONVERSATION: conv})

    idle = await _handle_cancel(request)  # type: ignore[arg-type]
    assert json.loads(idle.body) == {"ok": True, "cancelling": False}
    assert not conv.cancel_requested.is_set()

    conv.turn_active = True
    active = await _handle_cancel(request)  # type: ignore[arg-type]
    assert json.loads(active.body) == {"ok": True, "cancelling": True}
    assert conv.cancel_requested.is_set()


async def test_status_payload_carries_trust_fields(tmp_path) -> None:
    import json
    from types import SimpleNamespace

    from mylo.files.verifications import VerificationLog
    from mylo.server.app import AppKeys
    from mylo.server.routes_chat import _handle_status

    vlog = VerificationLog(tmp_path / "v.json")
    v = vlog.start(tool="modify_automation", target="automation porch", conversation_id="c")
    vlog.complete(v.id, status="verified", message="ok")

    config = SimpleNamespace(model="claude-sonnet-4-6", llm_provider="anthropic")
    # AppKeys.REGISTRIES, MEMORY, CONVERSATION and LAST_TURN are deliberately
    # absent — the handler must fall back gracefully via .get(), not KeyError.
    request = SimpleNamespace(
        app={
            AppKeys.CONFIG: config,
            AppKeys.VERIFICATIONS: vlog,
        }
    )

    resp = await _handle_status(request)  # type: ignore[arg-type]
    payload = json.loads(resp.body)

    assert payload["model"] == "claude-sonnet-4-6"
    assert payload["provider"] == "anthropic"
    assert payload["verifications"] == [v.to_dict()]
    assert payload["memory"]["last_sync_error"] is None
    assert payload["turn_active"] is False


async def test_ack_verification_endpoint(tmp_path) -> None:
    import json
    from types import SimpleNamespace

    from mylo.files.verifications import VerificationLog
    from mylo.server.app import AppKeys
    from mylo.server.routes_chat import _handle_ack_verification

    log = VerificationLog(tmp_path / "v.json")
    v = log.start(tool="t", target="x", conversation_id="c")
    log.complete(v.id, status="verified", message="ok")
    request = SimpleNamespace(app={AppKeys.VERIFICATIONS: log}, match_info={"id": v.id})
    resp = await _handle_ack_verification(request)  # type: ignore[arg-type]
    assert json.loads(resp.body) == {"ok": True}
    assert log.unacknowledged() == []
    missing = SimpleNamespace(app={AppKeys.VERIFICATIONS: log}, match_info={"id": "vf_nope"})
    assert json.loads((await _handle_ack_verification(missing)).body) == {"ok": False}  # type: ignore[arg-type]


async def test_ack_verification_endpoint_without_verification_log() -> None:
    """No VerificationLog configured (e.g. degraded startup) → ok False,
    not a KeyError."""
    import json
    from types import SimpleNamespace

    from mylo.server.routes_chat import _handle_ack_verification

    request = SimpleNamespace(app={}, match_info={"id": "vf_whatever"})
    resp = await _handle_ack_verification(request)  # type: ignore[arg-type]
    assert json.loads(resp.body) == {"ok": False}
