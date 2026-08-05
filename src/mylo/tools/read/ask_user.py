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

"""``ask_user`` — pause the turn and ask the user a structured question.

The handler is pure: it echoes the question back with
``await_user_input: True``. The actual pause lives in the tool loop —
:func:`mylo.llm.tool_loop.run_turn` breaks the iteration after any
tool result carrying that flag, so the model structurally cannot
answer its own question. The UI renders the options as buttons; the
selected label (or free text) arrives as the next chat message.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register


class AskUserOption(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, description="Button text shown to the user.")
    value: str | None = Field(
        default=None,
        description="Machine value if it differs from the label (e.g. a theme slug).",
    )
    description: str | None = Field(
        default=None, description="One-line explanation shown under the label."
    )


class AskUserParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, description="The question to show the user.")
    options: list[AskUserOption] = Field(
        default_factory=list,
        max_length=6,
        description=(
            "Clickable choices (max 6). Omit for a free-text question. "
            "Keep to the few realistic choices — this is a quick tap, not a form."
        ),
    )
    allow_free_text: bool = Field(
        default=True,
        description="Whether a typed answer (instead of an option) is acceptable.",
    )
    preference_key: str | None = Field(
        default=None,
        description=(
            "Set when the answer is a lasting preference worth remembering, "
            "e.g. 'dashboard.theme' or 'dashboard.layout'. Record the answer "
            "with memory_note(type='preference') on the next turn."
        ),
    )


async def handler(params: AskUserParams, ctx: ToolContext) -> ToolResult:
    data: dict[str, Any] = {
        "await_user_input": True,
        "question": params.question,
        "options": [o.model_dump(exclude_none=True) for o in params.options],
        "allow_free_text": params.allow_free_text,
        "preference_key": params.preference_key,
        "note": (
            "The turn now pauses. The user's answer arrives as the next "
            "message — do not assume or invent an answer."
        ),
    }
    return ToolResult.ok(data)


TOOL = ToolDefinition(
    name="ask_user",
    description=(
        "Ask the user ONE structured question and pause until they answer. "
        "Use when a build decision is genuinely the user's call — theme, "
        "layout style, which areas to include — and the answer isn't already "
        "in their stated preferences. Provide 2-6 concrete options (the UI "
        "renders them as buttons); the user may also type a free-form "
        "answer. Consolidate: ask one question covering the open decisions "
        "rather than several in a row. Set preference_key when the answer "
        "should be remembered, then record it with "
        "memory_note(type='preference') after they reply."
    ),
    params_model=AskUserParams,
    tier=Tier.READ,
    handler=handler,
)
register(TOOL)
