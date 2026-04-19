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

"""Prompt-injection defense for HA-sourced data.

Spec §5.2. Any string from HA — entity friendly_name, automation
description, attribute values, logbook messages — could contain an attempt
to redirect the LLM. We:

1. Enforce per-field length limits so a 4KB injection can't crowd out the
   legitimate prompt.
2. Scan for known injection patterns and, on match, replace the entire
   field with a marker like ``[sanitized: injection-suspected]``. The LLM
   still sees that a value existed but can't be influenced by it.
3. Log every detection via structlog so operators can audit the sync
   digest for attempts.

This module is intentionally strict — false positives are cheap (a field
reads as ``[sanitized]``), false negatives could escalate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from mylo.logging_setup import get_logger

log = get_logger(__name__)

# Fields we know about that enter LLM context. Unknown fields get a
# conservative default limit.
FIELD_LIMITS: dict[str, int] = {
    "friendly_name": 100,
    "name": 100,
    "entity_id": 150,
    "device_id": 150,
    "area_id": 100,
    "description": 500,
    "alias": 200,
    "state": 50,
    "unit_of_measurement": 20,
    "attribute_value": 200,
}
_DEFAULT_LIMIT = 200

# Patterns derived from the spec + common jailbreak corpora. Case-
# insensitive; we match on the lowercased value.
_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"ignore\s+(?:(?:previous|above|all|prior)\s+){1,3}(?:instructions?|prompts?|rules?)",
        r"disregard\s+(?:(?:previous|above|all|prior)\s+){1,3}(?:instructions?|prompts?|rules?)",
        r"you\s+are\s+now\s+(?:a|an|the)\b",
        r"from\s+now\s+on\s+(?:you|act|respond|pretend)",
        r"system\s*:\s*",
        r"<\s*/?\s*(?:system|assistant|user)\s*>",
        r"\[\s*system\s*\]",
        r"act\s+as\s+(?:if|a|an|the)\b",
        r"pretend\s+(?:you|that|to\s+be)",
        r"override\s+(?:security|permissions?|tier|safety)",
        r"forget\s+(?:everything|all|your\s+rules?|prior)",
        r"new\s+instructions?\s*:",
        r"execute\s+the\s+following",
        r"jailbreak",
        r"\bDAN\b.*\b(?:enabled|mode)\b",
    )
)

# Fields so structural / machine-readable that pattern scanning would be
# pure noise (e.g. labels are slugs).
_UNSCANNED_FIELDS: frozenset[str] = frozenset({"entity_id", "area_id", "device_id", "label_id"})

_MARKER = "[sanitized: injection-suspected]"
_TRUNCATED_SUFFIX = "…[truncated]"


@dataclass(slots=True)
class SanitizerStats:
    scanned_fields: int = 0
    truncated: int = 0
    sanitized: int = 0
    # First N fingerprints of sanitized content for debugging. Empty in
    # production — we never retain offending content.
    fingerprints: list[str] = field(default_factory=list)


class ContextSanitizer:
    """Sanitize any HA-sourced data before it enters LLM context.

    Call :meth:`sanitize` on any value: dict, list, or string. The sanitizer
    walks the structure and rewrites suspicious fields in place of the
    original value.
    """

    def __init__(self, *, field_limits: dict[str, int] | None = None) -> None:
        self._limits = dict(FIELD_LIMITS)
        if field_limits:
            self._limits.update(field_limits)
        self.stats = SanitizerStats()

    def limit_for(self, field_name: str | None) -> int:
        if field_name is None:
            return _DEFAULT_LIMIT
        return self._limits.get(field_name, _DEFAULT_LIMIT)

    def sanitize(self, value: Any, *, field_name: str | None = None) -> Any:
        if isinstance(value, str):
            return self._sanitize_string(value, field_name=field_name)
        if isinstance(value, dict):
            return {k: self.sanitize(v, field_name=k) for k, v in value.items()}
        if isinstance(value, list):
            return [self.sanitize(v, field_name=field_name) for v in value]
        # Numbers, bools, None pass through.
        return value

    def _sanitize_string(self, value: str, *, field_name: str | None) -> str:
        self.stats.scanned_fields += 1

        # Truncate first so patterns can't hide past the cut.
        limit = self.limit_for(field_name)
        if len(value) > limit:
            value = value[:limit] + _TRUNCATED_SUFFIX
            self.stats.truncated += 1

        if field_name in _UNSCANNED_FIELDS:
            return value

        if self._contains_injection(value):
            self.stats.sanitized += 1
            log.warning(
                "safety.sanitizer.injection_detected",
                field=field_name,
                length=len(value),
            )
            return _MARKER
        return value

    def _contains_injection(self, value: str) -> bool:
        return any(p.search(value) for p in _INJECTION_PATTERNS)


def sanitize(value: Any) -> Any:
    """Module-level convenience: sanitize with a fresh sanitizer and return
    only the sanitized value (stats discarded). For call sites that don't
    care about stats, which is most of them.
    """
    return ContextSanitizer().sanitize(value)
