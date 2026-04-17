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

import hashlib
import json
import time
from typing import Any

from pydantic import ValidationError

from mylo.logging_setup import get_logger
from mylo.safety.audit import make_entry
from mylo.safety.permissions import PermissionDecision
from mylo.tools import registry as tool_registry
from mylo.tools.base import Tier, ToolResult
from mylo.tools.context import ToolContext

log = get_logger(__name__)

# ─── Read-only tool result cache ────────────────────────────────────────────
# Caches tier-1 (READ) tool results for 120s. Cuts redundant queries
# when the model asks "what lights are on" twice in a conversation or
# fans out with multiple tools that internally call query_entities.

_CACHE_TTL = 120.0
_result_cache: dict[str, tuple[float, ToolResult]] = {}


def _cache_key(tool_name: str, raw_params: dict[str, Any]) -> str:
    canonical = json.dumps(raw_params or {}, sort_keys=True, default=str)
    h = hashlib.md5(canonical.encode(), usedforsecurity=False).hexdigest()[:12]
    return f"{tool_name}:{h}"


def _cache_get(key: str) -> ToolResult | None:
    entry = _result_cache.get(key)
    if entry is None:
        return None
    ts, result = entry
    if time.monotonic() - ts > _CACHE_TTL:
        del _result_cache[key]
        return None
    log.debug("tools.cache_hit", key=key)
    return result


def _cache_put(key: str, result: ToolResult) -> None:
    _result_cache[key] = (time.monotonic(), result)
    # Evict stale entries periodically (keep cache bounded).
    if len(_result_cache) > 100:
        now = time.monotonic()
        stale = [k for k, (ts, _) in _result_cache.items() if now - ts > _CACHE_TTL]
        for k in stale:
            del _result_cache[k]


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

    # Cache check for read-only tools — same params within 120s reuse
    # the prior result. Write/action tools are never cached.
    cache_key: str | None = None
    if tool.tier == Tier.READ:
        cache_key = _cache_key(tool.name, raw_params)
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

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

    # Store successful read results in cache.
    if cache_key is not None and result.status.value == "ok":
        _cache_put(cache_key, result)

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
