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

"""Tests for OpenAI/Ollama provider message format conversion.

These test the format translation layer without hitting a real API.
The provider converts Anthropic-shaped messages (content blocks) to
OpenAI's chat completions format and back.
"""

from __future__ import annotations

from mylo.llm.openai_provider import _convert_messages, _convert_tools


def test_system_prompt_becomes_system_message() -> None:
    oai = _convert_messages("You are Mylo.", [])
    assert oai[0] == {"role": "system", "content": "You are Mylo."}


def test_string_user_message() -> None:
    oai = _convert_messages("sys", [{"role": "user", "content": "hello"}])
    assert oai[1] == {"role": "user", "content": "hello"}


def test_assistant_text_block() -> None:
    oai = _convert_messages(
        "sys",
        [
            {"role": "assistant", "content": [{"type": "text", "text": "hi there"}]},
        ],
    )
    msg = oai[1]
    assert msg["role"] == "assistant"
    assert msg["content"] == "hi there"


def test_assistant_tool_use_becomes_tool_calls() -> None:
    oai = _convert_messages(
        "sys",
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me check."},
                    {
                        "type": "tool_use",
                        "id": "tc_1",
                        "name": "query_entities",
                        "input": {"filter": {"domain": "light"}},
                    },
                ],
            },
        ],
    )
    msg = oai[1]
    assert msg["role"] == "assistant"
    assert msg["content"] == "Let me check."
    assert len(msg["tool_calls"]) == 1
    tc = msg["tool_calls"][0]
    assert tc["id"] == "tc_1"
    assert tc["function"]["name"] == "query_entities"


def test_tool_result_becomes_tool_message() -> None:
    oai = _convert_messages(
        "sys",
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tc_1",
                        "content": '{"status":"ok","data":{}}',
                    }
                ],
            },
        ],
    )
    # tool_result blocks become role=tool messages.
    tool_msg = [m for m in oai if m.get("role") == "tool"]
    assert len(tool_msg) == 1
    assert tool_msg[0]["tool_call_id"] == "tc_1"


def test_convert_tools_wraps_in_function() -> None:
    anthropic_tools = [
        {
            "name": "query_entities",
            "description": "Search entities.",
            "input_schema": {"type": "object", "properties": {}},
        }
    ]
    oai = _convert_tools(anthropic_tools)
    assert len(oai) == 1
    assert oai[0]["type"] == "function"
    assert oai[0]["function"]["name"] == "query_entities"
    assert oai[0]["function"]["parameters"]["type"] == "object"


def test_default_max_tokens_fits_a_full_view() -> None:
    import inspect

    from mylo.llm.openai_provider import OpenAIProvider

    sig = inspect.signature(OpenAIProvider.message)
    assert sig.parameters["max_tokens"].default == 8192
