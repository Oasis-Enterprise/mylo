"""Tests for tools.executor — routing, param validation, error envelopes."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field

from mylo.tools import registry as tool_registry
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.executor import execute


class _P(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int = Field(ge=1)


@pytest.fixture(autouse=True)
def _isolate_registry() -> Any:
    tool_registry._reset_for_tests()
    yield
    tool_registry._reset_for_tests()


async def _ok(params: _P, _ctx: object) -> ToolResult:
    return ToolResult.ok({"doubled": params.n * 2})


async def _raises(params: _P, _ctx: object) -> ToolResult:
    raise RuntimeError("boom")


async def _returns_junk(params: _P, _ctx: object) -> Any:
    return {"nope": "not a ToolResult"}


def _define(name: str, handler: Any) -> ToolDefinition[_P]:
    t = ToolDefinition(name=name, description="t", params_model=_P, tier=Tier.READ, handler=handler)
    tool_registry.register(t)
    return t


async def test_unknown_tool_returns_error_envelope() -> None:
    result = await execute("missing", {}, ctx=None)  # type: ignore[arg-type]
    assert result.status.value == "error"
    assert result.error_code == "unknown_tool"
    assert "available" in (result.data or {})


async def test_happy_path() -> None:
    _define("t_ok", _ok)
    result = await execute("t_ok", {"n": 5}, ctx=None)  # type: ignore[arg-type]
    assert result.status.value == "ok"
    assert result.data == {"doubled": 10}


async def test_invalid_params_returns_structured_errors() -> None:
    _define("t_ok2", _ok)
    result = await execute("t_ok2", {"n": 0}, ctx=None)  # type: ignore[arg-type]
    assert result.error_code == "invalid_params"
    assert isinstance(result.data["errors"], list)
    assert result.data["errors"][0]["loc"] == ["n"]


async def test_handler_exception_becomes_error_envelope() -> None:
    _define("t_raises", _raises)
    result = await execute("t_raises", {"n": 1}, ctx=None)  # type: ignore[arg-type]
    assert result.error_code == "handler_error"
    assert "RuntimeError" in (result.error_message or "")


async def test_handler_wrong_return_type_is_caught() -> None:
    _define("t_junk", _returns_junk)
    result = await execute("t_junk", {"n": 1}, ctx=None)  # type: ignore[arg-type]
    assert result.error_code == "handler_error"
