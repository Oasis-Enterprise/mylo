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

"""Tests for the Anthropic provider's retry behaviour.

Exercises the transient-error backoff path that catches 429 (rate limit)
and 5xx — including 529 "overloaded", which Anthropic returns during
platform-wide load spikes and which previously propagated unretried.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from anthropic import BadRequestError, InternalServerError

from mylo.llm import anthropic_provider as ap_module
from mylo.llm.anthropic_provider import AnthropicProvider


def _status_error(cls: type, status_code: int) -> Exception:
    """Build a real anthropic ``APIStatusError`` subclass instance.

    The SDK derives ``.status_code`` from the attached httpx response, so
    we have to construct a fake response with the desired code.
    """
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status_code, request=request)
    return cls(f"HTTP {status_code}", response=response, body=None)


def _fake_response() -> Any:
    """Minimum surface the provider reads off a successful response."""
    return SimpleNamespace(content=[], stop_reason="end_turn", usage=None)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub out backoff sleeps so the suite stays fast."""
    monkeypatch.setattr(ap_module.asyncio, "sleep", AsyncMock())


async def test_retries_on_529_overloaded_then_succeeds() -> None:
    """A 529 must trigger backoff+retry, not propagate to the caller.

    Pre-fix the provider only caught ``RateLimitError`` (429); a 529 fell
    straight through and killed every chat the moment Anthropic's API hit
    an overload blip.
    """
    provider = AnthropicProvider(api_key="x")
    create = AsyncMock(
        side_effect=[
            _status_error(InternalServerError, 529),
            _fake_response(),
        ]
    )
    provider._client.messages.create = create  # type: ignore[attr-defined]

    result = await provider.message(system="sys", messages=[], tools=[], model="claude-x")

    assert create.await_count == 2
    assert result.stop_reason == "end_turn"


async def test_does_not_retry_on_400_bad_request() -> None:
    """Non-transient 4xx must propagate immediately — no backoff."""
    provider = AnthropicProvider(api_key="x")
    create = AsyncMock(side_effect=_status_error(BadRequestError, 400))
    provider._client.messages.create = create  # type: ignore[attr-defined]

    with pytest.raises(BadRequestError):
        await provider.message(system="sys", messages=[], tools=[], model="claude-x")

    assert create.await_count == 1


async def test_gives_up_after_exhausting_backoff() -> None:
    """Persistent 529s eventually surface to the caller after retries."""
    provider = AnthropicProvider(api_key="x")
    # _BACKOFF_DELAYS has 3 entries → up to 4 attempts total.
    create = AsyncMock(side_effect=[_status_error(InternalServerError, 529)] * 4)
    provider._client.messages.create = create  # type: ignore[attr-defined]

    with pytest.raises(InternalServerError):
        await provider.message(system="sys", messages=[], tools=[], model="claude-x")

    assert create.await_count == 4


def test_history_cache_breakpoint_annotates_last_block_copy_on_write() -> None:
    from mylo.llm.anthropic_provider import _with_history_cache_breakpoint

    original = [
        {"role": "user", "content": "hello"},
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "{}"},
                {"type": "tool_result", "tool_use_id": "t2", "content": "{}"},
            ],
        },
    ]
    import copy

    snapshot = copy.deepcopy(original)
    out = _with_history_cache_breakpoint(original)

    # Last block of the last message gets the breakpoint.
    assert out[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    # Earlier block in the same message is untouched.
    assert "cache_control" not in out[-1]["content"][0]
    # Copy-on-write: the caller's history is byte-for-byte unchanged.
    assert original == snapshot


def test_history_cache_breakpoint_promotes_string_content() -> None:
    from mylo.llm.anthropic_provider import _with_history_cache_breakpoint

    out = _with_history_cache_breakpoint([{"role": "user", "content": "hi"}])
    block = out[-1]["content"][0]
    assert block["type"] == "text"
    assert block["text"] == "hi"
    assert block["cache_control"] == {"type": "ephemeral"}


def test_history_cache_breakpoint_empty_is_noop() -> None:
    from mylo.llm.anthropic_provider import _with_history_cache_breakpoint

    assert _with_history_cache_breakpoint([]) == []
