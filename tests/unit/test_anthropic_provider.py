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

import pytest
from anthropic import APIConnectionError, BadRequestError, InternalServerError

# anthropic >= 1.0 moved its HTTP layer from httpx to httpx2. The SDK's
# error classes wrap whichever library it ships with, so the fake
# Request/Response must come from that same library.
try:
    import httpx2 as httpx
except ImportError:  # anthropic < 1.0
    import httpx

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


def test_default_max_tokens_fits_a_full_view() -> None:
    # 4096 forced piecemeal dashboard building; a whole sections view
    # needs headroom. Must match provider.py and openai_provider.py.
    import inspect

    from mylo.llm.anthropic_provider import AnthropicProvider

    sig = inspect.signature(AnthropicProvider.message)
    assert sig.parameters["max_tokens"].default == 8192


# ─── Streaming ──────────────────────────────────────────────────────────────


class _FakeStream:
    """Mimics anthropic's AsyncMessageStream: iterable events, then a
    final message. Only the surface the provider touches."""

    def __init__(self, deltas: list[str], final: Any) -> None:
        self._deltas = deltas
        self._final = final
        self.closed = False

    def __aiter__(self) -> Any:
        return self._events()

    async def _events(self) -> Any:
        for text in self._deltas:
            yield SimpleNamespace(type="text", text=text, snapshot="")
        yield SimpleNamespace(type="message_stop")

    async def get_final_message(self) -> Any:
        return self._final

    async def close(self) -> None:
        self.closed = True


class _FakeStreamManager:
    def __init__(self, stream: _FakeStream) -> None:
        self._stream = stream

    async def __aenter__(self) -> _FakeStream:
        return self._stream

    async def __aexit__(self, *_exc: Any) -> None:
        await self._stream.close()


class _RaisingStreamManager:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self) -> Any:
        raise self._exc

    async def __aexit__(self, *_exc: Any) -> None:
        return None


def _text_block(text: str) -> Any:
    return SimpleNamespace(
        type="text", text=text, model_dump=lambda: {"type": "text", "text": text}
    )


def _final_message(text: str) -> Any:
    return SimpleNamespace(
        content=[_text_block(text)],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=11,
            output_tokens=3,
            cache_creation_input_tokens=None,
            cache_read_input_tokens=None,
        ),
    )


async def _collect(provider: AnthropicProvider) -> list[Any]:
    return [
        item
        async for item in provider.stream(
            system="sys", messages=[{"role": "user", "content": "hi"}], tools=[], model="m"
        )
    ]


async def test_stream_yields_deltas_then_response() -> None:
    from mylo.llm.provider import ProviderResponse, StreamDelta

    provider = AnthropicProvider(api_key="x")
    fake = _FakeStream(["Hel", "lo"], _final_message("Hello"))
    provider._client.messages.stream = lambda **_kw: _FakeStreamManager(fake)  # type: ignore[attr-defined]

    items = await _collect(provider)

    assert items[:2] == [StreamDelta(text="Hel"), StreamDelta(text="lo")]
    assert isinstance(items[2], ProviderResponse)
    assert len(items) == 3
    assert items[2].text == "Hello"
    assert items[2].stop_reason == "end_turn"
    assert items[2].usage == {"input_tokens": 11, "output_tokens": 3}
    assert items[2].content_blocks == [{"type": "text", "text": "Hello"}]
    assert fake.closed


async def test_stream_response_matches_message_response() -> None:
    provider = AnthropicProvider(api_key="x")
    final = _final_message("Same")
    provider._client.messages.stream = lambda **_kw: _FakeStreamManager(  # type: ignore[attr-defined]
        _FakeStream([], final)
    )
    provider._client.messages.create = AsyncMock(return_value=final)  # type: ignore[attr-defined]

    streamed = (await _collect(provider))[-1]
    direct = await provider.message(
        system="sys", messages=[{"role": "user", "content": "hi"}], tools=[], model="m"
    )
    assert streamed == direct


async def test_stream_retries_transient_error_on_open() -> None:
    provider = AnthropicProvider(api_key="x")
    fake = _FakeStream(["ok"], _final_message("ok"))
    managers = [
        _RaisingStreamManager(_status_error(InternalServerError, 529)),
        _FakeStreamManager(fake),
    ]
    provider._client.messages.stream = lambda **_kw: managers.pop(0)  # type: ignore[attr-defined]

    items = await _collect(provider)
    assert items[0].text == "ok"
    assert len(items) == 2


async def test_stream_does_not_retry_4xx_on_open() -> None:
    provider = AnthropicProvider(api_key="x")
    provider._client.messages.stream = lambda **_kw: _RaisingStreamManager(  # type: ignore[attr-defined]
        _status_error(BadRequestError, 400)
    )
    with pytest.raises(BadRequestError):
        await _collect(provider)


async def test_stream_error_after_first_delta_is_not_retried() -> None:
    """Once text has reached the panel, a mid-stream error must propagate
    as-is — retrying here would silently redo work the user already saw
    output for."""

    class _FailingAfterFirstDelta(_FakeStream):
        async def _events(self) -> Any:
            yield SimpleNamespace(type="text", text=self._deltas[0], snapshot="")
            raise APIConnectionError(
                request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            )

    provider = AnthropicProvider(api_key="x")
    fake = _FailingAfterFirstDelta(["Hel"], _final_message("unused"))
    calls: list[dict[str, Any]] = []

    def _stream(**kw: Any) -> Any:
        calls.append(kw)
        return _FakeStreamManager(fake)

    provider._client.messages.stream = _stream  # type: ignore[attr-defined]

    with pytest.raises(APIConnectionError):
        await _collect(provider)

    assert len(calls) == 1
    assert fake.closed


# ─── Close-after-completion (finding 2) ────────────────────────────────────


class _CloseFailingStream(_FakeStream):
    """A ``_FakeStream`` whose ``close()`` always raises."""

    async def close(self) -> None:
        self.closed = True
        raise ConnectionError("close failed")


class _CloseFailingStreamNoFinal(_CloseFailingStream):
    """Same, but ``get_final_message()`` also fails — nothing completed."""

    async def get_final_message(self) -> Any:
        raise RuntimeError("boom")


async def test_stream_close_error_after_completion_is_ignored() -> None:
    """A transport error from close() after a full turn already arrived
    must not discard that turn — it's logged, not raised."""
    from mylo.llm.provider import ProviderResponse

    provider = AnthropicProvider(api_key="x")
    fake = _CloseFailingStream(["Hi"], _final_message("Hi"))
    provider._client.messages.stream = lambda **_kw: _FakeStreamManager(fake)  # type: ignore[attr-defined]

    items = await _collect(provider)

    assert isinstance(items[-1], ProviderResponse)
    assert items[-1].text == "Hi"
    assert fake.closed


async def test_stream_close_error_before_completion_propagates() -> None:
    """When nothing completed, a close() failure must not mask the real
    error — and must not itself surface in its place."""
    provider = AnthropicProvider(api_key="x")
    fake = _CloseFailingStreamNoFinal(["Hi"], _final_message("unused"))
    provider._client.messages.stream = lambda **_kw: _FakeStreamManager(fake)  # type: ignore[attr-defined]

    with pytest.raises(RuntimeError, match="boom"):
        await _collect(provider)

    assert fake.closed
