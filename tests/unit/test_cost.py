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

"""Tests for token→USD estimation and cache-hit accounting."""

from __future__ import annotations

import pytest

from mylo.llm.cost import cache_hit_ratio, estimate_usd


def test_estimate_usd_sonnet_all_buckets() -> None:
    usage = {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_creation_input_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
    }
    # 3.00 + 15.00 + 3.75 + 0.30
    assert estimate_usd(usage, "claude-sonnet-4-6") == pytest.approx(22.05)


def test_estimate_usd_unknown_model_falls_back_to_sonnet() -> None:
    usage = {"input_tokens": 1_000_000}
    assert estimate_usd(usage, "some-future-model") == pytest.approx(3.0)


def test_estimate_usd_missing_fields_default_zero() -> None:
    assert estimate_usd({}, "claude-sonnet-4-6") == 0.0


def test_cache_read_is_far_cheaper_than_uncached() -> None:
    cached = estimate_usd({"cache_read_input_tokens": 1_000_000}, "claude-sonnet-4-6")
    uncached = estimate_usd({"input_tokens": 1_000_000}, "claude-sonnet-4-6")
    assert cached == pytest.approx(0.30)
    assert cached < uncached / 9  # ~10x cheaper


def test_cache_hit_ratio() -> None:
    usage = {
        "input_tokens": 1000,
        "cache_creation_input_tokens": 1000,
        "cache_read_input_tokens": 8000,
    }
    assert cache_hit_ratio(usage) == pytest.approx(0.8)


def test_cache_hit_ratio_zero_when_no_input() -> None:
    assert cache_hit_ratio({"output_tokens": 500}) == 0.0
