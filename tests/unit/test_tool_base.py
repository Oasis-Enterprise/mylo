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

from mylo.dashboard.plan import PlanDashboardParams
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


def test_json_schema_strips_discriminator_for_discriminated_union() -> None:
    """PlanOp is a pydantic discriminated union; model_json_schema() emits a
    "discriminator": {"propertyName": ..., "mapping": {...}} block whose
    mapping values are $ref strings into $defs. _inline_defs pops $defs, so
    those mapping strings would dangle — strip discriminator entirely.
    """
    tool: ToolDefinition[PlanDashboardParams] = ToolDefinition(
        name="t_plan",
        description="test plan tool",
        params_model=PlanDashboardParams,
        tier=Tier.MODIFY,
        handler=_handler,  # type: ignore[arg-type]
    )
    schema = tool.json_schema()
    assert "$defs" not in schema
    assert "definitions" not in schema
    _assert_no_discriminator(schema)

    # A field literally named "title" (CreateView.title, required) must
    # survive in `properties` and `required` — the noise stripper must not
    # treat property *names* as noise, only the schema-noise keys that
    # appear as siblings of "type"/"properties"/etc.
    variants = schema["properties"]["operations"]["items"]["oneOf"]
    create_view = next(
        v for v in variants if v.get("properties", {}).get("op", {}).get("const") == "create_view"
    )
    assert "title" in create_view["properties"]
    assert "title" in create_view["required"]
    assert create_view["properties"]["title"]["type"] == "string"


def _assert_no_discriminator(node: object) -> None:
    if isinstance(node, dict):
        assert "discriminator" not in node
        for v in node.values():
            _assert_no_discriminator(v)
    elif isinstance(node, list):
        for v in node:
            _assert_no_discriminator(v)


class _TitleAndPropertiesFields(BaseModel):
    """A model with fields literally named "title" and "properties" — the
    exact two words the noise stripper must special-case (strip as schema
    keys, keep as property names)."""

    model_config = ConfigDict(extra="forbid")
    title: str
    properties: dict[str, int]


async def _handler_tp(params: _TitleAndPropertiesFields, ctx: object) -> ToolResult:
    return ToolResult.ok({})


def test_json_schema_keeps_property_named_title_or_properties() -> None:
    tool: ToolDefinition[_TitleAndPropertiesFields] = ToolDefinition(
        name="t_tp",
        description="test",
        params_model=_TitleAndPropertiesFields,
        tier=Tier.READ,
        handler=_handler_tp,
    )
    schema = tool.json_schema()
    # The model's own pydantic-generated top-level "title" (the class name)
    # is still stripped as noise.
    assert "title" not in schema
    # But the two fields named "title" and "properties" survive as entries
    # of the properties map, with their real sub-schemas intact.
    assert schema["properties"]["title"]["type"] == "string"
    assert schema["properties"]["properties"]["type"] == "object"
    assert set(schema["required"]) == {"title", "properties"}


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
