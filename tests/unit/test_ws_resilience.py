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

import asyncio
from typing import Any

import pytest

from mylo.ha.ws_client import State
from tests.unit.test_ws_client import _FakeSession, _FakeWS, _make_client, _wait_for_sent


async def _ready_client(ws: _FakeWS, session: _FakeSession) -> Any:
    client = _make_client(session)
    await client.start()
    ws.push_server_text({"type": "auth_required"})
    await _wait_for_sent(ws, lambda m: m.get("type") == "auth")
    ws.push_server_text({"type": "auth_ok", "ha_version": "x"})
    for _ in range(200):
        if client.state is State.READY:
            break
        await asyncio.sleep(0.01)
    assert client.state is State.READY
    return client


@pytest.mark.asyncio
async def test_event_callback_can_issue_command_without_deadlock() -> None:
    ws = _FakeWS()
    session = _FakeSession([ws])
    client = await _ready_client(ws, session)
    got: dict[str, Any] = {}

    async def cb(_event: dict[str, Any]) -> None:
        # Issue a command from *inside* an event callback. With inline
        # dispatch this deadlocks: the read loop is blocked here, so the
        # command's result can never be read.
        got["result"] = await client.send_command("get_config")

    # subscribe_events awaits its own result, so deliver it concurrently.
    sub_task = asyncio.create_task(client.subscribe_events("demo_event", cb))
    sub_msg = await _wait_for_sent(ws, lambda m: m.get("type") == "subscribe_events")
    ws.push_server_text({"id": sub_msg["id"], "type": "result", "success": True})
    await sub_task
    ws.push_server_text({"id": sub_msg["id"], "type": "event", "event": {"hello": 1}})
    cmd_msg = await _wait_for_sent(ws, lambda m: m.get("type") == "get_config")
    ws.push_server_text(
        {"id": cmd_msg["id"], "type": "result", "success": True, "result": {"ok": True}}
    )
    for _ in range(300):
        if "result" in got:
            break
        await asyncio.sleep(0.01)
    assert got["result"] == {"ok": True}
    await client.close()
