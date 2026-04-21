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

"""Gemini Provider — uses Google's OpenAI-compatible endpoint.

Google exposes an OpenAI-compatible API at
``generativelanguage.googleapis.com/v1beta/openai/`` that accepts
the same chat completions shape including tool calling. We reuse
:class:`OpenAIProvider` with a custom ``base_url``, same as Ollama.

Supported models: gemini-2.5-flash, gemini-2.5-pro, etc.
API key: get one at https://aistudio.google.com/apikey
"""

from __future__ import annotations

from mylo.llm.openai_provider import OpenAIProvider
from mylo.logging_setup import get_logger

log = get_logger(__name__)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"


class GeminiProvider(OpenAIProvider):
    """Gemini via Google's OpenAI-compatible endpoint.

    Inherits all message/tool conversion from OpenAIProvider. The
    only difference is the base_url and default model.
    """

    def __init__(self, api_key: str, *, model: str | None = None) -> None:
        super().__init__(
            api_key=api_key,
            base_url=GEMINI_BASE_URL,
            default_model=model or DEFAULT_GEMINI_MODEL,
        )
        log.info("gemini.initialized", model=model or DEFAULT_GEMINI_MODEL)
