"""Tool executor — the sequencer.

Responsibilities, in order:

1. Look up the tool by name.
2. Validate params (pydantic).
3. Check permissions (:mod:`mylo.safety.permissions`).
4. Invoke the handler.
5. Write an audit entry regardless of outcome.
6. Return a ToolResult envelope.

Business logic lives in the collaborators. This module stays thin so the
permission matrix, audit contract, and tool handlers can each evolve
independently.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from mylo.logging_setup import get_logger
from mylo.safety.audit import make_entry
from mylo.safety.permissions import PermissionDecision
from mylo.tools import registry as tool_registry
from mylo.tools.base import ToolResult
from mylo.tools.context import ToolContext

log = get_logger(__name__)


async def execute(
    tool_name: str,
    raw_params: dict[str, Any],
    ctx: ToolContext,
) -> ToolResult:
    """Run a tool. Always returns a ToolResult (never raises for expected
    failures — invalid params, unknown tool, denied permission, handler
    exceptions are all represented in the envelope).
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
        log.info("tools.invalid_params", name=tool_name, errors=exc.error_count())
        return ToolResult.error(
            "invalid_params",
            "parameters failed validation",
            data={"errors": _serialize_validation_errors(exc)},
        )

    # A tool invocation's dry_run status comes from the params, not the
    # context — the LLM asks for dry runs per-call. Extract it early so
    # the permission check can allow tier-2 previews without approval.
    tool_dry_run = _dry_run_from_params(raw_params)
    decision: PermissionDecision = ctx.permissions.check(
        tier=tool.tier,
        conversation_id=ctx.conversation_id,
        user_approved=ctx.user_approved,
        dry_run=tool_dry_run,
    )
    if not decision.allowed:
        await _audit(
            ctx,
            tool_name=tool.name,
            tier=int(tool.tier),
            params=raw_params,
            dry_run=ctx.dry_run,
            user_approved=ctx.user_approved,
            result="denied",
            details={"reason_code": decision.reason_code},
        )
        return ToolResult.error(
            decision.reason_code,
            decision.reason_message,
        )

    try:
        result = await tool.handler(params, ctx)
    except Exception as exc:  # surface as structured result
        log.exception("tools.handler_error", name=tool_name)
        result = ToolResult.error(
            "handler_error",
            f"{type(exc).__name__}: {exc}",
        )

    if not isinstance(result, ToolResult):
        log.error("tools.bad_return_type", name=tool_name, got=type(result).__name__)
        result = ToolResult.error(
            "handler_error",
            "handler did not return a ToolResult",
        )

    await _audit(
        ctx,
        tool_name=tool.name,
        tier=int(tool.tier),
        params=raw_params,
        dry_run=ctx.dry_run,
        user_approved=ctx.user_approved,
        result="success" if result.status.value == "ok" else "failure",
        details={"error_code": result.error_code} if result.error_code else {},
    )
    return result


async def _audit(
    ctx: ToolContext,
    *,
    tool_name: str,
    tier: int,
    params: dict[str, Any],
    dry_run: bool,
    user_approved: bool,
    result: str,
    details: dict[str, Any],
) -> None:
    entry = make_entry(
        conversation_id=ctx.conversation_id,
        tool_name=tool_name,
        tier=tier,
        params=params,
        dry_run=dry_run,
        user_approved=user_approved,
        result=result,  # type: ignore[arg-type]
        details=details,
    )
    try:
        await ctx.audit.write(entry)
    except Exception as exc:  # audit failures must not break tool calls
        log.warning("tools.audit_write_failed", error=str(exc))


def _dry_run_from_params(raw_params: dict[str, Any]) -> bool:
    value = (raw_params or {}).get("dry_run")
    return bool(value)


def _serialize_validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
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
