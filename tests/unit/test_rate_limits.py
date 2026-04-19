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

"""Tests for rate limit counters."""

from __future__ import annotations

import pytest

from mylo.safety.rate_limits import (
    DEFAULT_DAILY_LIMITS,
    RateLimitCounters,
    RateLimitExceeded,
)


def test_below_limit_increments_ok() -> None:
    c = RateLimitCounters()
    for _ in range(5):
        c.check_and_increment("file_writes", conversation_id="c1")


def test_daily_limit_exceeded() -> None:
    c = RateLimitCounters(daily_limits={"file_writes": 3})  # type: ignore[arg-type]
    c.check_and_increment("file_writes", conversation_id="c1")
    c.check_and_increment("file_writes", conversation_id="c1")
    c.check_and_increment("file_writes", conversation_id="c1")

    with pytest.raises(RateLimitExceeded) as exc:
        c.check_and_increment("file_writes", conversation_id="c1")
    assert exc.value.kind == "daily"


def test_per_conversation_limit_exceeded() -> None:
    c = RateLimitCounters(
        daily_limits={},  # type: ignore[arg-type]
        per_conversation_limits={"tier_3_calls": 2},  # type: ignore[arg-type]
    )
    c.check_and_increment("tier_3_calls", conversation_id="c1")
    c.check_and_increment("tier_3_calls", conversation_id="c1")

    with pytest.raises(RateLimitExceeded) as exc:
        c.check_and_increment("tier_3_calls", conversation_id="c1")
    assert exc.value.kind == "per-conversation"

    # A different conversation has its own budget.
    c.check_and_increment("tier_3_calls", conversation_id="c2")


def test_separate_scopes_are_independent() -> None:
    c = RateLimitCounters(
        daily_limits={"file_writes": 1, "service_calls": 1},  # type: ignore[arg-type]
        per_conversation_limits={},  # type: ignore[arg-type]
    )
    c.check_and_increment("file_writes", conversation_id="c1")
    c.check_and_increment("service_calls", conversation_id="c1")  # different scope, ok


def test_failed_check_does_not_consume_token() -> None:
    c = RateLimitCounters(
        daily_limits={"file_writes": 0},  # type: ignore[arg-type]
        per_conversation_limits={},  # type: ignore[arg-type]
    )
    with pytest.raises(RateLimitExceeded):
        c.check_and_increment("file_writes", conversation_id="c1")
    with pytest.raises(RateLimitExceeded):
        c.check_and_increment("file_writes", conversation_id="c1")


def test_default_limits_match_spec() -> None:
    # Spec §5.5.
    assert DEFAULT_DAILY_LIMITS["file_writes"] == 20
    assert DEFAULT_DAILY_LIMITS["service_calls"] == 100
    assert DEFAULT_DAILY_LIMITS["entity_renames"] == 50
