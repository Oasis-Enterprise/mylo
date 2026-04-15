"""Shared error envelope for reference-resolution failures.

Every tool that validates entity/device/area refs and finds a mismatch
emits this exact shape, so the LLM's retry loop sees a consistent schema
and can programmatically look for ``did_you_mean`` suggestions.

See :class:`ResolvedRef` / :class:`RefMismatch` in
:mod:`mylo.resolver.resolver` for the richer types returned at runtime;
this module is just the on-the-wire dict form.
"""

from __future__ import annotations

from typing import Any, TypedDict


class MismatchDict(TypedDict, total=False):
    error: str  # stable machine code, e.g. "entity_not_found"
    invalid_ref: str
    kind: str  # "entity" | "device" | "area"
    did_you_mean: list[str]
    hint: str


def entity_not_found(
    invalid: str, suggestions: list[str], *, hint: str | None = None
) -> MismatchDict:
    return _envelope("entity_not_found", invalid, "entity", suggestions, hint)


def device_not_found(
    invalid: str, suggestions: list[str], *, hint: str | None = None
) -> MismatchDict:
    return _envelope("device_not_found", invalid, "device", suggestions, hint)


def area_not_found(
    invalid: str, suggestions: list[str], *, hint: str | None = None
) -> MismatchDict:
    return _envelope("area_not_found", invalid, "area", suggestions, hint)


def _envelope(
    code: str,
    invalid: str,
    kind: str,
    suggestions: list[str],
    hint: str | None,
) -> MismatchDict:
    payload: dict[str, Any] = {
        "error": code,
        "invalid_ref": invalid,
        "kind": kind,
        "did_you_mean": suggestions,
    }
    if hint:
        payload["hint"] = hint
    return payload  # type: ignore[return-value]
