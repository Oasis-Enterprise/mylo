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

"""Tests for ToolDefinition schema generation."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from mylo.tools.base import ResultStatus, Tier, ToolDefinition, ToolResult


class Nested(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(description="what kind")


class Params(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int = Field(ge=1, description="count")
    nested: Nested


async def _handler(params: Params, ctx: object) -> ToolResult:
    return ToolResult.ok({"n": params.n})


TOOL: ToolDefinition[Params] = ToolDefinition(
    name="t_base",
    description="test tool",
    params_model=Params,
    tier=Tier.READ,
    handler=_handler,
)


def test_json_schema_inlines_defs_and_strips_titles() -> None:
    schema = TOOL.json_schema()
    # No leftover $defs/definitions.
    assert "$defs" not in schema
    assert "definitions" not in schema
    # Nested model inlined under properties.nested.
    nested = schema["properties"]["nested"]
    assert nested["type"] == "object"
    assert "kind" in nested["properties"]
    # Titles stripped everywhere.
    _assert_no_titles(schema)


def _assert_no_titles(node: object) -> None:
    if isinstance(node, dict):
        assert "title" not in node
        for v in node.values():
            _assert_no_titles(v)
    elif isinstance(node, list):
        for v in node:
            _assert_no_titles(v)


def test_to_anthropic_shape() -> None:
    spec = TOOL.to_anthropic()
    assert spec["name"] == "t_base"
    assert spec["description"] == "test tool"
    assert "input_schema" in spec
    assert spec["input_schema"]["type"] == "object"


def test_to_openai_shape() -> None:
    spec = TOOL.to_openai()
    assert spec["type"] == "function"
    assert spec["function"]["name"] == "t_base"
    assert spec["function"]["parameters"]["type"] == "object"


def test_tool_result_envelope() -> None:
    ok = ToolResult.ok({"hello": 1})
    assert ok.status is ResultStatus.OK
    assert ok.to_dict() == {"status": "ok", "data": {"hello": 1}}

    err = ToolResult.error("bad_thing", "went wrong", data={"hint": "try x"})
    assert err.to_dict() == {
        "status": "error",
        "error": {"code": "bad_thing", "message": "went wrong"},
        "data": {"hint": "try x"},
    }
