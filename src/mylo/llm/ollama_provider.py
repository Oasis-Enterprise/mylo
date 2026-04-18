"""Ollama Provider — uses Ollama's OpenAI-compatible endpoint.

Ollama exposes ``/v1/chat/completions`` with the same shape as
OpenAI's API, including tool calling (for models that support it:
llama3.1, mistral, qwen2, etc.). We reuse :class:`OpenAIProvider`
with a custom ``base_url`` pointed at the local Ollama instance.

Default Ollama URL inside a Home Assistant add-on is
``http://host.docker.internal:11434`` (Ollama running on the host)
or ``http://<ollama-addon>:11434`` if Ollama is another HA add-on.
Configurable via ``OLLAMA_URL`` env var.

Cost: $0. Ollama runs locally. The trade-off is model quality —
smaller local models produce more hallucinations and weaker tool
calling than Claude or GPT-4. The dashboard entity-ref validator
and the reference resolver still catch most errors.
"""

from __future__ import annotations

import os

from mylo.llm.openai_provider import OpenAIProvider
from mylo.logging_setup import get_logger

log = get_logger(__name__)

DEFAULT_OLLAMA_URL = "http://host.docker.internal:11434/v1"
DEFAULT_OLLAMA_MODEL = "llama3.1"


class OllamaProvider(OpenAIProvider):
    """Ollama via the OpenAI-compatible endpoint.

    Inherits all message/tool conversion from OpenAIProvider. The
    only difference is the base_url and that we use a dummy API key
    (Ollama doesn't require one).
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        url = base_url or os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL)
        super().__init__(
            api_key="ollama",  # Ollama ignores the key
            base_url=url,
            default_model=model or DEFAULT_OLLAMA_MODEL,
        )
        log.info("ollama.initialized", base_url=url, model=model or DEFAULT_OLLAMA_MODEL)
