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

"""Core tool abstractions.

Every tool is defined by:

* a pydantic v2 params model (for validation AND JSON Schema generation),
* a handler coroutine that takes the validated params + a ToolContext and
  returns a ToolResult envelope.

The executor layer (``tools.executor``) is the only place that instantiates
params from raw dicts — handlers never see unvalidated input.

JSON Schemas for both providers are derived from the pydantic model so the
schema and the handler's argument type cannot drift apart.
"""

from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, TypeVar

from pydantic import BaseModel

# Forward references — avoid circular imports. These are set at tool-registration
# time rather than imported here.
ToolContext = Any  # set to concrete type in context.py below


class Tier(enum.IntEnum):
    """Permission tier per spec §4.2."""

    READ = 1
    MODIFY = 2
    ACTION = 3


class ResultStatus(enum.StrEnum):
    OK = "ok"
    ERROR = "error"


@dataclass(slots=True)
class ToolResult:
    """What every tool returns. The ``data`` field is LLM-facing content.

    ``error_code`` is a stable machine string (e.g. ``entity_not_found``,
    ``invalid_params``). ``data`` on error should include ``did_you_mean``
    where relevant — the resolver envelope (Milestone 6.5) will standardize
    that shape.
    """

    status: ResultStatus
    data: Any
    error_code: str | None = None
    error_message: str | None = None

    @classmethod
    def ok(cls, data: Any) -> ToolResult:
        return cls(status=ResultStatus.OK, data=data)

    @classmethod
    def error(cls, code: str, message: str, data: Any = None) -> ToolResult:
        return cls(
            status=ResultStatus.ERROR,
            data=data,
            error_code=code,
            error_message=message,
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status.value}
        if self.error_code is not None:
            out["error"] = {
                "code": self.error_code,
                "message": self.error_message or "",
            }
        if self.data is not None:
            out["data"] = self.data
        return out


_Params = TypeVar("_Params", bound=BaseModel)

ToolHandler = Callable[[_Params, "ToolContext"], Awaitable[ToolResult]]


@dataclass(slots=True)
class ToolDefinition[Params: BaseModel]:
    """Declarative definition of one tool.

    ``name``: the identifier the LLM uses (snake_case).
    ``description``: what the LLM sees; keep under ~400 chars.
    ``params_model``: pydantic BaseModel subclass defining parameters.
    ``tier``: 1, 2, or 3.
    ``handler``: async function taking (params, ctx) → ToolResult.
    """

    name: str
    description: str
    params_model: type[Params]
    tier: Tier
    handler: ToolHandler[Params]

    # Providers sometimes want a slightly different shape than pydantic's
    # default JSON schema. Filled lazily via :meth:`json_schema`.
    _schema_cache: dict[str, Any] | None = field(default=None, init=False)

    _CLEAN_KEYS: ClassVar[tuple[str, ...]] = ("title",)

    def json_schema(self) -> dict[str, Any]:
        """Return a JSON Schema for the parameters, suitable for LLM providers.

        Strips pydantic's ``title`` keys (noise for the model) and inlines
        ``$defs`` so the model sees a single flat schema.
        """
        if self._schema_cache is None:
            raw = self.params_model.model_json_schema(mode="serialization")
            inlined = _inline_defs(raw)
            cleaned = _strip_titles(inlined)
            assert isinstance(cleaned, dict)
            self._schema_cache = cleaned
        return self._schema_cache

    def to_anthropic(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.json_schema(),
        }

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.json_schema(),
            },
        }


# ─── Schema helpers ──────────────────────────────────────────────────────────


def _inline_defs(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline ``$defs`` references so the schema is self-contained.

    Anthropic and OpenAI both accept ``$defs``/``$ref`` but inlining keeps
    generated schemas flat and easier to eyeball during debugging.
    """
    defs = schema.pop("$defs", None) or schema.pop("definitions", None)
    if not defs:
        return schema

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node and isinstance(node["$ref"], str):
                name = node["$ref"].rsplit("/", 1)[-1]
                target = defs.get(name)
                if target is None:
                    return node
                # Merge non-ref keys (e.g. "description") with the target.
                merged = dict(resolve(target))
                for k, v in node.items():
                    if k != "$ref":
                        merged[k] = resolve(v)
                return merged
            return {k: resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(x) for x in node]
        return node

    resolved = resolve(schema)
    assert isinstance(resolved, dict)
    return resolved


def _strip_titles(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {k: _strip_titles(v) for k, v in schema.items() if k != "title"}
    if isinstance(schema, list):
        return [_strip_titles(x) for x in schema]
    return schema
