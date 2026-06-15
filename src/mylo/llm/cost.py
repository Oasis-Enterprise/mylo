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

"""Token-usage → USD estimation and cache-hit accounting.

The provider already captures the four usage counters (input, output,
cache-creation/write, cache-read); this turns them into a dollar
estimate and a cache-hit ratio so we can measure the caching win.

Rates are $ per 1M tokens. Cache writes (5-min TTL) bill at ~1.25x
input; cache reads at ~0.1x input. Verify against the current Anthropic
pricing if rates change.
"""

from __future__ import annotations

from typing import Any

# $ per 1,000,000 tokens, per model.
_PRICES: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_write": 1.25, "cache_read": 0.10},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_read": 0.50},
}
# Fallback when the model id isn't in the table — Sonnet rates (the chat default).
_DEFAULT_RATES = _PRICES["claude-sonnet-4-6"]


def _rates_for(model: str) -> dict[str, float]:
    if model in _PRICES:
        return _PRICES[model]
    for key, rates in _PRICES.items():
        if model.startswith(key):
            return rates
    return _DEFAULT_RATES


def estimate_usd(usage: dict[str, Any], model: str) -> float:
    """Estimate the dollar cost of one usage dict for ``model``."""
    rates = _rates_for(model)
    cost = (
        float(usage.get("input_tokens", 0)) * rates["input"]
        + float(usage.get("output_tokens", 0)) * rates["output"]
        + float(usage.get("cache_creation_input_tokens", 0)) * rates["cache_write"]
        + float(usage.get("cache_read_input_tokens", 0)) * rates["cache_read"]
    )
    return cost / 1_000_000


def cache_hit_ratio(usage: dict[str, Any]) -> float:
    """Fraction of input tokens served from cache (0..1).

    Denominator is total input = uncached + cache-write + cache-read.
    Returns 0.0 when there were no input tokens.
    """
    read = usage.get("cache_read_input_tokens", 0)
    total = usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0) + read
    return read / total if total else 0.0
