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

"""Tests for query_traces tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mylo.ha.registries import Registries
from mylo.tools import registry as tool_registry
from mylo.tools.read.query_traces import QueryTracesParams, handler
from tests.unit._helpers import make_ctx

# ─── Sample HA trace payloads ────────────────────────────────────────────────

# HA returns trace/list OLDEST-first; the older run is at index 0. The
# tool must still select abc123 (newest by timestamp) as the latest run.
_TRACE_LIST_RESPONSE = [
    {
        "run_id": "def456",
        "timestamp": {"start": "2026-06-09T08:00:00+00:00", "finish": "2026-06-09T08:00:01+00:00"},
        "state": "stopped",
        "script_execution": "finished",
        "last_step": "action/0",
        "error": None,
    },
    {
        "run_id": "abc123",
        "timestamp": {"start": "2026-06-10T08:00:00+00:00", "finish": "2026-06-10T08:00:01+00:00"},
        "state": "stopped",
        "script_execution": "finished",
        "last_step": "action/0",
        "error": None,
    },
]

_TRACE_GET_RESPONSE = {
    "run_id": "abc123",
    "trace": {
        "trigger/0": [
            {
                "result": {
                    "platform": "time",
                    "run_variables": {
                        "trigger": {"platform": "time", "now": "2026-06-10T08:00:00+00:00"}
                    },
                    "variables": {},
                }
            }
        ],
        "condition/0": [{"result": {"result": True}, "timestamp": "2026-06-10T08:00:00.100+00:00"}],
        "action/0": [
            {"result": {"choice": "default"}, "timestamp": "2026-06-10T08:00:00.200+00:00"}
        ],
    },
    "config": {"alias": "Morning Routine"},
    "context": {"id": "ctx1"},
    "trigger": {"platform": "time", "at": "08:00:00"},
    "error": None,
    "last_step": "action/0",
    "timestamp": {"start": "2026-06-10T08:00:00+00:00", "finish": "2026-06-10T08:00:01+00:00"},
    "state": "stopped",
    "script_execution": "finished",
}

_TRACE_GET_WITH_ERROR = {
    "run_id": "err789",
    "trace": {
        "trigger/0": [{"result": {"platform": "state"}}],
        "action/0": [{"result": {}, "error": "Service call failed"}],
    },
    "config": {"alias": "Broken Script"},
    "context": {"id": "ctx2"},
    "trigger": {"platform": "state", "entity_id": "binary_sensor.door"},
    "error": "Service call failed: light.turn_on",
    "last_step": "action/0",
    "timestamp": {"start": "2026-06-11T10:00:00+00:00", "finish": "2026-06-11T10:00:00.500+00:00"},
    "state": "stopped",
    "script_execution": "error",
}

_EMPTY_TRACE_LIST: list[Any] = []


# ─── Fake WS client ──────────────────────────────────────────────────────────


class _FakeClient:
    """Returns different responses based on command type."""

    def __init__(
        self,
        list_response: Any = None,
        get_response: Any = None,
    ) -> None:
        self._list = list_response if list_response is not None else _TRACE_LIST_RESPONSE
        self._get = get_response if get_response is not None else _TRACE_GET_RESPONSE
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def send_command(self, type_: str, **kwargs: Any) -> Any:
        self.calls.append((type_, kwargs))
        if type_ == "trace/list":
            return self._list
        if type_ == "trace/get":
            return self._get
        raise AssertionError(f"unexpected command: {type_}")


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def _reg() -> Registries:
    return Registries()


# ─── Tests: item_id normalization ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_accepts_full_entity_id(tmp_path: Path, _reg: Registries) -> None:
    """automation.morning_routine should be normalized to morning_routine."""
    client = _FakeClient()
    ctx = make_ctx(ws_client=client, registries=_reg, tmp_path=tmp_path)
    params = QueryTracesParams(item_type="automation", item_id="automation.morning_routine")
    result = await handler(params, ctx)

    assert result.status.value == "ok"
    # Verify the WS call used just the object_id, not the full entity_id
    list_call = next(c for c in client.calls if c[0] == "trace/list")
    assert list_call[1]["item_id"] == "morning_routine"
    assert list_call[1]["domain"] == "automation"


@pytest.mark.asyncio
async def test_accepts_bare_object_id(tmp_path: Path, _reg: Registries) -> None:
    """morning_routine (no dot) should pass through unchanged."""
    client = _FakeClient()
    ctx = make_ctx(ws_client=client, registries=_reg, tmp_path=tmp_path)
    params = QueryTracesParams(item_type="automation", item_id="morning_routine")
    result = await handler(params, ctx)

    assert result.status.value == "ok"
    list_call = next(c for c in client.calls if c[0] == "trace/list")
    assert list_call[1]["item_id"] == "morning_routine"


# ─── Tests: script domain ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_script_item_type_uses_script_domain(tmp_path: Path, _reg: Registries) -> None:
    """item_type='script' should pass domain='script' to HA."""
    client = _FakeClient()
    ctx = make_ctx(ws_client=client, registries=_reg, tmp_path=tmp_path)
    params = QueryTracesParams(item_type="script", item_id="script.my_script")
    result = await handler(params, ctx)

    assert result.status.value == "ok"
    list_call = next(c for c in client.calls if c[0] == "trace/list")
    assert list_call[1]["domain"] == "script"
    assert list_call[1]["item_id"] == "my_script"


# ─── Tests: list + most-recent run ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_returns_runs_summary(tmp_path: Path, _reg: Registries) -> None:
    """Without run_id, returns list of recent runs plus most-recent trace."""
    client = _FakeClient()
    ctx = make_ctx(ws_client=client, registries=_reg, tmp_path=tmp_path)
    params = QueryTracesParams(item_type="automation", item_id="morning_routine")
    result = await handler(params, ctx)

    assert result.status.value == "ok"
    data = result.data
    assert "recent_runs" in data
    assert len(data["recent_runs"]) == 2
    assert data["recent_runs"][0]["run_id"] == "abc123"
    assert data["recent_runs"][1]["run_id"] == "def456"


@pytest.mark.asyncio
async def test_list_fetches_most_recent_trace(tmp_path: Path, _reg: Registries) -> None:
    """Without run_id, also fetches and summarizes the most recent trace."""
    client = _FakeClient()
    ctx = make_ctx(ws_client=client, registries=_reg, tmp_path=tmp_path)
    params = QueryTracesParams(item_type="automation", item_id="morning_routine")
    result = await handler(params, ctx)

    assert result.status.value == "ok"
    data = result.data
    # Should have called trace/get with the first run_id (most recent)
    get_call = next(c for c in client.calls if c[0] == "trace/get")
    assert get_call[1]["run_id"] == "abc123"
    assert "latest_run" in data


@pytest.mark.asyncio
async def test_trace_summary_includes_trigger(tmp_path: Path, _reg: Registries) -> None:
    """The trace summary should include what triggered the run."""
    client = _FakeClient()
    ctx = make_ctx(ws_client=client, registries=_reg, tmp_path=tmp_path)
    params = QueryTracesParams(item_type="automation", item_id="morning_routine")
    result = await handler(params, ctx)

    latest = result.data["latest_run"]
    assert "trigger" in latest
    # Trigger info from _TRACE_GET_RESPONSE
    assert latest["trigger"]["platform"] == "time"


@pytest.mark.asyncio
async def test_trace_summary_includes_steps(tmp_path: Path, _reg: Registries) -> None:
    """The trace summary should include the steps that ran."""
    client = _FakeClient()
    ctx = make_ctx(ws_client=client, registries=_reg, tmp_path=tmp_path)
    params = QueryTracesParams(item_type="automation", item_id="morning_routine")
    result = await handler(params, ctx)

    latest = result.data["latest_run"]
    assert "steps_executed" in latest
    assert len(latest["steps_executed"]) == 3  # trigger/0, condition/0, action/0


@pytest.mark.asyncio
async def test_trace_summary_includes_last_step_and_state(tmp_path: Path, _reg: Registries) -> None:
    """The trace summary should include last_step and final state."""
    client = _FakeClient()
    ctx = make_ctx(ws_client=client, registries=_reg, tmp_path=tmp_path)
    params = QueryTracesParams(item_type="automation", item_id="morning_routine")
    result = await handler(params, ctx)

    latest = result.data["latest_run"]
    assert latest["last_step"] == "action/0"
    assert latest["state"] == "stopped"
    assert latest["script_execution"] == "finished"


# ─── Tests: error case ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trace_summary_includes_error(tmp_path: Path, _reg: Registries) -> None:
    """When the trace has an error, it should be included in the summary."""
    client = _FakeClient(
        list_response=[
            {
                "run_id": "err789",
                "timestamp": {
                    "start": "2026-06-11T10:00:00+00:00",
                    "finish": "2026-06-11T10:00:00.500+00:00",
                },
                "state": "stopped",
                "script_execution": "error",
                "last_step": "action/0",
                "error": "Service call failed: light.turn_on",
            }
        ],
        get_response=_TRACE_GET_WITH_ERROR,
    )
    ctx = make_ctx(ws_client=client, registries=_reg, tmp_path=tmp_path)
    params = QueryTracesParams(item_type="automation", item_id="broken_automation")
    result = await handler(params, ctx)

    assert result.status.value == "ok"
    latest = result.data["latest_run"]
    assert latest["error"] == "Service call failed: light.turn_on"
    assert latest["script_execution"] == "error"


# ─── Tests: no traces ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_traces_returns_clear_result(tmp_path: Path, _reg: Registries) -> None:
    """Empty list from trace/list should return a clear no_traces result."""
    client = _FakeClient(list_response=_EMPTY_TRACE_LIST)
    ctx = make_ctx(ws_client=client, registries=_reg, tmp_path=tmp_path)
    params = QueryTracesParams(item_type="automation", item_id="never_ran")
    result = await handler(params, ctx)

    assert result.status.value == "ok"
    data = result.data
    assert data.get("no_traces") is True
    assert data.get("recent_runs") == []
    # Should NOT have called trace/get when there are no traces
    get_calls = [c for c in client.calls if c[0] == "trace/get"]
    assert len(get_calls) == 0


# ─── Tests: explicit run_id ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_explicit_run_id_fetches_that_run(tmp_path: Path, _reg: Registries) -> None:
    """When run_id is given, skip trace/list and fetch that specific run."""
    client = _FakeClient()
    ctx = make_ctx(ws_client=client, registries=_reg, tmp_path=tmp_path)
    params = QueryTracesParams(item_type="automation", item_id="morning_routine", run_id="abc123")
    result = await handler(params, ctx)

    assert result.status.value == "ok"
    # Should NOT have called trace/list
    list_calls = [c for c in client.calls if c[0] == "trace/list"]
    assert len(list_calls) == 0
    # Should have called trace/get with the specific run_id
    get_calls = [c for c in client.calls if c[0] == "trace/get"]
    assert len(get_calls) == 1
    assert get_calls[0][1]["run_id"] == "abc123"


@pytest.mark.asyncio
async def test_explicit_run_id_result_has_run_summary(tmp_path: Path, _reg: Registries) -> None:
    """With explicit run_id, result should have run summary but no recent_runs list."""
    client = _FakeClient()
    ctx = make_ctx(ws_client=client, registries=_reg, tmp_path=tmp_path)
    params = QueryTracesParams(item_type="automation", item_id="morning_routine", run_id="abc123")
    result = await handler(params, ctx)

    assert result.status.value == "ok"
    data = result.data
    assert "run" in data
    assert "recent_runs" not in data


# ─── Tests: registration ─────────────────────────────────────────────────────


def test_tool_is_registered() -> None:
    """query_traces should be in the tool registry after module import."""
    tool_registry._reset_for_tests()
    tool_registry.load_all()
    tool = tool_registry.get("query_traces")
    assert tool is not None
    assert tool.name == "query_traces"
    tool_registry._reset_for_tests()


def test_tool_is_read_tier() -> None:
    """query_traces should be tier READ (1)."""
    from mylo.tools.base import Tier

    tool_registry._reset_for_tests()
    tool_registry.load_all()
    tool = tool_registry.get("query_traces")
    assert tool is not None
    assert tool.tier == Tier.READ
    tool_registry._reset_for_tests()
