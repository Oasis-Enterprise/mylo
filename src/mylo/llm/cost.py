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

The provider already captures the usage counters (input, output, and —
for Anthropic — cache-creation/write and cache-read); this turns them
into a dollar estimate and a cache-hit ratio so we can measure cost and
the caching win.

Rates are $ per 1M tokens. For Anthropic, cache writes (5-min TTL) bill
at ~1.25x input and cache reads at ~0.1x input. The non-Anthropic
providers Mylo supports don't report cache counters through our usage
dict, so their cache_* rates are inert (set to sensible values for
forward-compat). Local models (Ollama) are free — pass ``is_local=True``.

The non-Anthropic figures are a snapshot of published pricing and will
drift as providers change rates / retire models. A stale figure only
skews the *estimate*; it never blocks a model. Unknown models fall back
to Sonnet rates and are logged so we notice what's unpriced. Verify
against the providers' current pricing pages if accuracy matters:
  - Anthropic: https://www.anthropic.com/pricing
  - Gemini:    https://ai.google.dev/gemini-api/docs/pricing
  - OpenAI:    https://openai.com/api/pricing/
"""

from __future__ import annotations

from typing import Any

from mylo.logging_setup import get_logger

log = get_logger(__name__)

# $ per 1,000,000 tokens, per model. cache_write/cache_read are only
# meaningful for Anthropic (the only provider whose usage dict carries
# cache counters today); for others they mirror input as a safe default.
_PRICES: dict[str, dict[str, float]] = {
    # ── Anthropic ──
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_write": 1.25, "cache_read": 0.10},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_read": 0.50},
    # ── Google Gemini (paid tier; Pro figures are the ≤200k-prompt tier,
    #    which is where an HA assistant lives — the >200k tier is higher) ──
    "gemini-2.5-pro": {"input": 1.25, "output": 10.0, "cache_write": 1.25, "cache_read": 0.3125},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50, "cache_write": 0.30, "cache_read": 0.075},
    "gemini-2.5-flash-lite": {
        "input": 0.10,
        "output": 0.40,
        "cache_write": 0.10,
        "cache_read": 0.025,
    },
    "gemini-3-pro-preview": {"input": 2.0, "output": 12.0, "cache_write": 2.0, "cache_read": 0.50},
    # ── OpenAI (GPT-5.x lineup; cache_read = published cached-input rate) ──
    "gpt-5.5": {"input": 5.0, "output": 30.0, "cache_write": 5.0, "cache_read": 0.50},
    "gpt-5.4": {"input": 2.50, "output": 15.0, "cache_write": 2.50, "cache_read": 0.25},
    "gpt-5.4-mini": {"input": 0.75, "output": 4.50, "cache_write": 0.75, "cache_read": 0.075},
    "gpt-5.4-nano": {"input": 0.20, "output": 1.25, "cache_write": 0.20, "cache_read": 0.02},
}
# Fallback when the model id isn't in the table — Sonnet rates (the chat default).
_DEFAULT_RATES = _PRICES["claude-sonnet-4-6"]


def _rates_for(model: str) -> dict[str, float]:
    if model in _PRICES:
        return _PRICES[model]
    # Dated snapshots (e.g. claude-haiku-4-5-20251001) and minor variants
    # share their base model's rates.
    for key, rates in _PRICES.items():
        if model.startswith(key):
            return rates
    log.warning("cost.unpriced_model_using_sonnet_fallback", model=model)
    return _DEFAULT_RATES


def estimate_usd(usage: dict[str, Any], model: str, *, is_local: bool = False) -> float:
    """Estimate the dollar cost of one usage dict for ``model``.

    ``is_local=True`` (Ollama / any self-hosted model) is always $0 —
    the tokens are free, so don't price them at the fallback rate.
    """
    if is_local:
        return 0.0
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
