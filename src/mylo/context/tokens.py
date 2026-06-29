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

"""Shared token estimation and model context-window lookup.

One estimator for the whole codebase, replacing scattered ``len(x)//4``
heuristics. It deliberately OVER-estimates (assumes fewer characters per
token than reality) so a miss can never let an assembled prompt exceed
the real model window — the budget manager and reconciler both rely on
that safety direction.
"""

from __future__ import annotations

import math

# Conservative: real models average ~4 chars/token; 3.5 over-estimates.
_CHARS_PER_TOKEN = 3.5

# Usable context window per model, in tokens. Unknown models fall back to
# the smallest mainstream window so we never assume more room than exists.
_CONTEXT_WINDOWS: dict[str, int] = {
    "claude-opus-4-8": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-haiku-4-5": 200_000,
    "gemini-2.5-pro": 1_000_000,
    "gemini-2.5-flash": 1_000_000,
    "gemini-2.5-flash-lite": 1_000_000,
    "gemini-3-pro-preview": 1_000_000,
    "gpt-5.5": 400_000,
    "gpt-5.4": 400_000,
    "gpt-5.4-mini": 400_000,
    "gpt-5.4-nano": 400_000,
}
_DEFAULT_WINDOW = 200_000


def estimate_tokens(text: str) -> int:
    """Conservative token estimate for ``text`` (rounds up)."""
    if not text:
        return 0
    return math.ceil(len(text) / _CHARS_PER_TOKEN)


def context_window_for(model: str) -> int:
    """Usable context window for ``model`` (dated snapshots match base)."""
    if model in _CONTEXT_WINDOWS:
        return _CONTEXT_WINDOWS[model]
    for key, window in _CONTEXT_WINDOWS.items():
        if model.startswith(key):
            return window
    return _DEFAULT_WINDOW
