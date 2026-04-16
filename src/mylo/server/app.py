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

from aiohttp import web

from mylo.config import AppConfig, load_config
from mylo.conversation.manager import ConversationManager
from mylo.conversation.storage import ConversationStorage
from mylo.ha.registries import Registries
from mylo.ha.ws_client import HaWsClient
from mylo.llm.anthropic_provider import AnthropicProvider
from mylo.logging_setup import get_logger
from mylo.memory.store import MemoryStore
from mylo.safety.audit import AuditLogger
from mylo.safety.permissions import default_permissions
from mylo.server.routes_chat import register_chat_routes
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

    # LLM provider.
    api_key = os.environ.get("ANTHROPIC_API_KEY") or config.api_key
    if not api_key:
        log.warning("server.no_api_key — chat will be disabled until key is set")
        # Still start server — UI can show a banner. Provider is optional.
    provider = AnthropicProvider(api_key=api_key) if api_key else None
    if provider is not None:
        app[AppKeys.PROVIDER] = provider

    # Tool context (shared across requests; per-request shims can wrap later).
    tool_registry.load_all()
    app[AppKeys.TOOLS_JSON] = [t.to_anthropic() for t in tool_registry.all_tools()]
    app[AppKeys.TOOL_CONTEXT] = ToolContext(
        ws_client=client,
        registries=registries,
        config=config,
        permissions=default_permissions(),
        audit=AuditLogger(config.mylo_data_dir),
        conversation_id=conv.conversation_id,
    )

    log.info(
        "server.started",
        entities=len(registries.entities),
        devices=len(registries.devices),
        api_key=bool(api_key),
    )


async def _cleanup(app: web.Application) -> None:
    client = app.get(AppKeys.HA_CLIENT)
    if client is not None:
        await client.close()


def build_app(config: AppConfig | None = None) -> web.Application:
    app = web.Application()
    app[AppKeys.CONFIG] = config or load_config()
    app.on_startup.append(_startup)
    app.on_cleanup.append(_cleanup)

    register_chat_routes(app)
    register_static_routes(app)

    return app


def run() -> None:
    """Process entrypoint — call from ``__main__``."""
    app = build_app()
    port = int(os.environ.get("MYLO_PORT", "8099"))
    host = os.environ.get("MYLO_HOST", "0.0.0.0")
    web.run_app(app, host=host, port=port, access_log=None, print=None)
