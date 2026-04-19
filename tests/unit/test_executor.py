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

"""Tests for tools.executor — routing, param validation, permission gate,
audit emission, error envelopes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field

from mylo.ha.registries import Registries
from mylo.tools import registry as tool_registry
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.executor import execute
from tests.unit._helpers import make_ctx


class _P(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int = Field(ge=1)


@pytest.fixture(autouse=True)
def _isolate_registry() -> Any:
    tool_registry._reset_for_tests()
    yield
    tool_registry._reset_for_tests()


async def _ok(params: _P, _ctx: Any) -> ToolResult:
    return ToolResult.ok({"doubled": params.n * 2})


async def _raises(params: _P, _ctx: Any) -> ToolResult:
    raise RuntimeError("boom")


async def _returns_junk(params: _P, _ctx: Any) -> Any:
    return {"nope": "not a ToolResult"}


def _define(name: str, handler: Any, tier: Tier = Tier.READ) -> ToolDefinition[_P]:
    t = ToolDefinition(name=name, description="t", params_model=_P, tier=tier, handler=handler)
    tool_registry.register(t)
    return t


def _ctx(tmp_path: Path, **kwargs: Any) -> Any:
    return make_ctx(ws_client=None, registries=Registries(), tmp_path=tmp_path, **kwargs)


async def test_unknown_tool_returns_error_envelope(tmp_path: Path) -> None:
    result = await execute("missing", {}, _ctx(tmp_path))
    assert result.error_code == "unknown_tool"
    assert "available" in (result.data or {})


async def test_happy_path(tmp_path: Path) -> None:
    _define("t_ok", _ok)
    result = await execute("t_ok", {"n": 5}, _ctx(tmp_path))
    assert result.status.value == "ok"
    assert result.data == {"doubled": 10}


async def test_invalid_params_returns_structured_errors(tmp_path: Path) -> None:
    _define("t_ok2", _ok)
    result = await execute("t_ok2", {"n": 0}, _ctx(tmp_path))
    assert result.error_code == "invalid_params"
    assert result.data["errors"][0]["loc"] == ["n"]


async def test_handler_exception_becomes_error_envelope(tmp_path: Path) -> None:
    _define("t_raises", _raises)
    result = await execute("t_raises", {"n": 1}, _ctx(tmp_path))
    assert result.error_code == "handler_error"
    assert "RuntimeError" in (result.error_message or "")


async def test_handler_wrong_return_type_is_caught(tmp_path: Path) -> None:
    _define("t_junk", _returns_junk)
    result = await execute("t_junk", {"n": 1}, _ctx(tmp_path))
    assert result.error_code == "handler_error"


# ─── Permission gate ─────────────────────────────────────────────────────────


async def test_tier_1_does_not_require_approval(tmp_path: Path) -> None:
    _define("t_tier1", _ok, tier=Tier.READ)
    result = await execute("t_tier1", {"n": 2}, _ctx(tmp_path, user_approved=False))
    assert result.status.value == "ok"


async def test_tier_2_without_approval_denied(tmp_path: Path) -> None:
    _define("t_tier2", _ok, tier=Tier.MODIFY)
    result = await execute("t_tier2", {"n": 1}, _ctx(tmp_path, user_approved=False))
    assert result.error_code == "confirmation_required"


async def test_tier_2_with_approval_ok(tmp_path: Path) -> None:
    _define("t_tier2_ok", _ok, tier=Tier.MODIFY)
    result = await execute("t_tier2_ok", {"n": 1}, _ctx(tmp_path, user_approved=True))
    assert result.status.value == "ok"


async def test_tier_3_without_approval_denied(tmp_path: Path) -> None:
    _define("t_tier3", _ok, tier=Tier.ACTION)
    result = await execute("t_tier3", {"n": 1}, _ctx(tmp_path, user_approved=False))
    assert result.error_code == "confirmation_required"


async def test_tier_3_rate_limit_kicks_in(tmp_path: Path) -> None:
    _define("t_tier3_burn", _ok, tier=Tier.ACTION)
    ctx = _ctx(tmp_path, user_approved=True)
    # Default per-conversation tier_3_calls limit is 50. Burn through it.
    for _ in range(50):
        r = await execute("t_tier3_burn", {"n": 1}, ctx)
        assert r.status.value == "ok"
    # 51st call should be denied.
    r = await execute("t_tier3_burn", {"n": 1}, ctx)
    assert r.error_code == "rate_limited"


# ─── Audit ──────────────────────────────────────────────────────────────────


async def test_audit_written_for_success(tmp_path: Path) -> None:
    _define("t_audit", _ok)
    ctx = _ctx(tmp_path)
    await execute("t_audit", {"n": 3}, ctx)
    entries = ctx.audit.read_recent(limit=10)
    assert any(e["tool_name"] == "t_audit" and e["result"] == "success" for e in entries)


async def test_audit_written_for_denied(tmp_path: Path) -> None:
    _define("t_audit_denied", _ok, tier=Tier.MODIFY)
    ctx = _ctx(tmp_path, user_approved=False)
    await execute("t_audit_denied", {"n": 1}, ctx)
    entries = ctx.audit.read_recent(limit=10)
    assert any(e["tool_name"] == "t_audit_denied" and e["result"] == "denied" for e in entries)
