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
