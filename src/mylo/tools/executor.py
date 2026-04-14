"""Tool executor — the only entry point for running a tool.

Milestone 2 responsibility is small: look up the tool, validate params,
invoke the handler, shape the result. Permissions, dry-run enforcement,
rate limiting, and audit logging land in Milestone 3.

The executor is deliberately kept thin. When it grows, business logic
belongs in the validators/safety/formatters modules it calls, not here.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from mylo.logging_setup import get_logger
from mylo.tools import registry as tool_registry
from mylo.tools.base import ToolResult
from mylo.tools.context import ToolContext

log = get_logger(__name__)


async def execute(
    tool_name: str,
    raw_params: dict[str, Any],
    ctx: ToolContext,
) -> ToolResult:
    """Run a tool by name. Always returns a ToolResult (never raises for
    expected failures — invalid params, unknown tool, handler errors are
    all represented in the envelope).
    """
    tool = tool_registry.get(tool_name)
    if tool is None:
        log.warning("tools.unknown", name=tool_name)
        return ToolResult.error(
            "unknown_tool",
            f"no tool named {tool_name!r}",
            data={"available": tool_registry.names()},
        )

    try:
        params = tool.params_model.model_validate(raw_params or {})
    except ValidationError as exc:
        log.info(
            "tools.invalid_params",
            name=tool_name,
            errors=exc.error_count(),
        )
        return ToolResult.error(
            "invalid_params",
            "parameters failed validation",
            data={"errors": _serialize_validation_errors(exc)},
        )

    try:
        result = await tool.handler(params, ctx)
    except Exception as exc:
        log.exception("tools.handler_error", name=tool_name)
        return ToolResult.error(
            "handler_error",
            f"{type(exc).__name__}: {exc}",
        )

    if not isinstance(result, ToolResult):
        log.error("tools.bad_return_type", name=tool_name, got=type(result).__name__)
        return ToolResult.error(
            "handler_error",
            "handler did not return a ToolResult",
        )
    return result


def _serialize_validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    """Turn pydantic errors into JSON-friendly dicts.

    pydantic's own ``errors()`` output includes non-serializable ``ctx``
    entries sometimes; we flatten to loc + msg + type.
    """
    out: list[dict[str, Any]] = []
    for err in exc.errors(include_url=False, include_context=False, include_input=False):
        out.append(
            {
                "loc": list(err.get("loc", ())),
                "msg": err.get("msg", ""),
                "type": err.get("type", ""),
            }
        )
    return out
