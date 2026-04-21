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

"""aiohttp application factory.

Builds the full server with its singletons — HA client, registries,
conversation store, LLM provider, tool registry — and registers routes.
Caller (``__main__``) calls :func:`build_app`, runs it with
``aiohttp.web.run_app``.

The server lives behind HA Ingress; no authentication is performed beyond
accepting the Ingress-proxied session (the Supervisor adds
``X-Hassio-Ingress`` headers we could validate, but for MVP we trust that
Ingress is enforced at the proxy layer). Outside-of-HA access would need
its own auth — out of scope for v1.
"""

from __future__ import annotations

import os
from typing import Any

from aiohttp import web

from mylo.config import AppConfig, load_config
from mylo.conversation.manager import ConversationManager
from mylo.conversation.storage import ConversationStorage
from mylo.ha.registries import Registries
from mylo.ha.ws_client import HaWsClient
from mylo.llm.anthropic_provider import AnthropicProvider
from mylo.logging_setup import get_logger
from mylo.memory.store import MemoryStore
from mylo.monitor.scheduler import start_scheduler, stop_scheduler
from mylo.safety.audit import AuditLogger
from mylo.safety.permissions import default_permissions
from mylo.server.routes_chat import register_chat_routes
from mylo.server.routes_memory import register_memory_routes
from mylo.server.static import register_static_routes
from mylo.tools import registry as tool_registry
from mylo.tools.context import ToolContext

log = get_logger(__name__)


# Keys on the aiohttp application used to stash singletons.
class AppKeys:
    CONFIG = web.AppKey("config", AppConfig)
    HA_CLIENT = web.AppKey("ha_client", HaWsClient)
    REGISTRIES = web.AppKey("registries", Registries)
    CONVERSATION = web.AppKey("conversation", ConversationManager)
    PROVIDER = web.AppKey("provider", AnthropicProvider)
    TOOL_CONTEXT = web.AppKey("tool_context", ToolContext)
    TOOLS_JSON = web.AppKey("tools_json", list)
    MEMORY = web.AppKey("memory", MemoryStore)
    HA_TIMEZONE = web.AppKey("ha_timezone", str)
    SCHEDULER = web.AppKey("scheduler", object)


_DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-4o",
    "ollama": "llama3.1",
}


def _resolve_model(config: AppConfig) -> str:
    """Return the configured model, falling back to a sensible default
    when the user switches providers but forgets to update the model.

    If the model field contains a provider-specific name that doesn't
    match the selected provider, swap it for the default.
    """
    model = config.model
    provider = config.llm_provider

    # Detect mismatched provider/model combos.
    if provider == "openai" and model.startswith("claude"):
        default = _DEFAULT_MODELS["openai"]
        log.warning(
            "server.model_mismatch",
            configured=model,
            provider=provider,
            using=default,
        )
        return default
    if provider == "anthropic" and model.startswith(("gpt-", "o1-")):
        default = _DEFAULT_MODELS["anthropic"]
        log.warning(
            "server.model_mismatch",
            configured=model,
            provider=provider,
            using=default,
        )
        return default
    if provider == "ollama" and (model.startswith("claude") or model.startswith("gpt-")):
        default = _DEFAULT_MODELS["ollama"]
        log.warning(
            "server.model_mismatch",
            configured=model,
            provider=provider,
            using=default,
        )
        return default

    return model


def _create_provider(config: AppConfig) -> Any:
    """Instantiate the LLM provider based on config.llm_provider."""
    api_key = os.environ.get("ANTHROPIC_API_KEY") or config.api_key
    model = _resolve_model(config)

    if config.llm_provider == "anthropic":
        if not api_key:
            return None
        return AnthropicProvider(api_key=api_key)

    if config.llm_provider == "openai":
        openai_key = os.environ.get("OPENAI_API_KEY") or api_key
        if not openai_key:
            log.warning(
                "server.openai_no_key",
                hint="Set api_key in the Configuration tab to your OpenAI API key",
            )
            return None
        from mylo.llm.openai_provider import OpenAIProvider

        return OpenAIProvider(api_key=openai_key, default_model=model)

    if config.llm_provider == "ollama":
        from mylo.llm.ollama_provider import OllamaProvider

        url = config.ollama_url or os.environ.get("OLLAMA_URL") or None
        return OllamaProvider(base_url=url, model=model)

    log.warning("server.unknown_llm_provider", provider=config.llm_provider)
    return None


async def _startup(app: web.Application) -> None:
    config: AppConfig = app[AppKeys.CONFIG]

    # HA websocket.
    ha_url = os.environ.get("HA_URL")
    ha_token = config.supervisor_token or os.environ.get("HA_TOKEN")
    if not ha_url:
        # Inside the add-on, HA is reachable at the well-known Supervisor
        # proxy URL. Locally we require HA_URL.
        ha_url = "http://supervisor/core" if config.supervisor_token else None
    if not ha_url or not ha_token:
        raise RuntimeError("HA_URL/HA_TOKEN (dev) or SUPERVISOR_TOKEN (add-on) must be set")

    client = HaWsClient(ha_url, ha_token)
    await client.start()
    await client.wait_ready(timeout=15.0)
    registries = await Registries.attach(client)
    await registries.wait_loaded(timeout=15.0)
    app[AppKeys.HA_CLIENT] = client
    app[AppKeys.REGISTRIES] = registries

    # Conversation store.
    storage = ConversationStorage(config.mylo_data_dir / "conversations.db")
    await storage.init()
    conv = ConversationManager(storage=storage)
    await conv.load(limit=int(os.environ.get("MYLO_HISTORY_LIMIT", "12")))
    app[AppKeys.CONVERSATION] = conv

    # Memory (context.yaml + scratchpad reader).
    memory_store = MemoryStore(mylo_data_dir=config.mylo_data_dir)
    await memory_store.load()
    app[AppKeys.MEMORY] = memory_store

    # HA timezone — cached at startup so the memory section renders
    # the current time in the user's local tz on every chat turn.
    try:
        ha_config = await client.send_command("get_config", timeout=10.0)
        if isinstance(ha_config, dict) and isinstance(ha_config.get("time_zone"), str):
            app[AppKeys.HA_TIMEZONE] = ha_config["time_zone"]
    except Exception as exc:
        log.warning("server.timezone_fetch_failed", error=str(exc))

    # LLM provider — selected by config.llm_provider.
    provider = _create_provider(config)
    if provider is not None:
        app[AppKeys.PROVIDER] = provider
    else:
        log.warning("server.no_provider — chat will be disabled")

    # Tool context (shared across requests; per-request shims can wrap later).
    tool_registry.load_all()
    # Always store Anthropic format. Each provider's message() method
    # converts internally — _convert_tools in openai_provider handles
    # the Anthropic→OpenAI translation. Storing pre-converted tools
    # caused a double-conversion KeyError for OpenAI/Ollama users.
    app[AppKeys.TOOLS_JSON] = [t.to_anthropic() for t in tool_registry.all_tools()]
    app[AppKeys.TOOL_CONTEXT] = ToolContext(
        ws_client=client,
        registries=registries,
        config=config,
        permissions=default_permissions(),
        audit=AuditLogger(config.mylo_data_dir),
        conversation_id=conv.conversation_id,
    )

    # Background scheduler (nightly reconciler + hourly availability).
    scheduler = await start_scheduler(app)
    app[AppKeys.SCHEDULER] = scheduler

    log.info(
        "server.started",
        entities=len(registries.entities),
        devices=len(registries.devices),
        llm_provider=config.llm_provider,
        has_provider=AppKeys.PROVIDER in app,
    )


async def _cleanup(app: web.Application) -> None:
    await stop_scheduler(app.get(AppKeys.SCHEDULER))
    client = app.get(AppKeys.HA_CLIENT)
    if client is not None:
        await client.close()


def build_app(config: AppConfig | None = None) -> web.Application:
    app = web.Application()
    app[AppKeys.CONFIG] = config or load_config()
    app.on_startup.append(_startup)
    app.on_cleanup.append(_cleanup)

    register_chat_routes(app)
    register_memory_routes(app)
    register_static_routes(app)

    return app


def run() -> None:
    """Process entrypoint — call from ``__main__``."""
    app = build_app()
    port = int(os.environ.get("MYLO_PORT", "8099"))
    host = os.environ.get("MYLO_HOST", "0.0.0.0")
    web.run_app(app, host=host, port=port, access_log=None, print=None)
