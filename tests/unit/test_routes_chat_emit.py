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
