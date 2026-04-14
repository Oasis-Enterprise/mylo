"""Interactive chat against a live Home Assistant.

Usage::

    python -m mylo.scripts.chat

Reads ``HA_URL``, ``HA_TOKEN``, ``ANTHROPIC_API_KEY``, ``MYLO_CONFIG_DIR``
from the environment (``.env`` is loaded if present).

Slash commands inside the REPL:

* ``/clear``   — wipe this conversation and start fresh.
* ``/history`` — dump the raw message history.
* ``/usage``   — show cumulative tokens used this session.
* ``/quit``    — exit.

Every other line is a user message. Streaming is simulated with per-event
print; the underlying provider call is non-streaming in M4a.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from mylo.config import load_config
from mylo.context.basic_prompt import load_system_prompt
from mylo.conversation.manager import ConversationManager
from mylo.conversation.storage import ConversationStorage
from mylo.ha.registries import Registries
from mylo.ha.ws_client import AuthFailed, HaWsClient
from mylo.llm.anthropic_provider import AnthropicProvider
from mylo.llm.tool_loop import (
    DoneEvent,
    TextEvent,
    ToolCallEvent,
    ToolResultEvent,
    run_turn,
)
from mylo.logging_setup import configure_logging, get_logger
from mylo.safety.audit import AuditLogger
from mylo.safety.permissions import default_permissions
from mylo.tools import registry as tool_registry
from mylo.tools.context import ToolContext
from mylo.util.env import load_dotenv

# ANSI colors — terminals that don't support them will show the raw codes.
# Kept minimal.
_CYAN = "\x1b[36m"
_DIM = "\x1b[2m"
_YELLOW = "\x1b[33m"
_RED = "\x1b[31m"
_RESET = "\x1b[0m"


def _print_tool_call(event: ToolCallEvent) -> None:
    args_preview = json.dumps(event.input, default=str)
    if len(args_preview) > 100:
        args_preview = args_preview[:97] + "..."
    sys.stdout.write(f"{_DIM}→ {event.name}({args_preview}){_RESET}\n")
    sys.stdout.flush()


def _print_tool_result(event: ToolResultEvent) -> None:
    if event.status == "ok":
        # Pull a short summary hint when the tool provides one.
        summary = ""
        if isinstance(event.data, dict):
            for key in ("summary", "entities_found", "devices_found", "count"):
                if key in event.data:
                    summary = f" ({key}={event.data[key]})"
                    break
        sys.stdout.write(f"{_DIM}  ✓ {event.name}{summary}{_RESET}\n")
    else:
        sys.stdout.write(f"{_RED}  ✗ {event.name}: {event.error_code}{_RESET}\n")
    sys.stdout.flush()


def _print_text(event: TextEvent) -> None:
    sys.stdout.write(f"\n{event.text}\n")
    sys.stdout.flush()


def _print_done(event: DoneEvent, *, verbose: bool) -> None:
    if verbose and event.usage:
        sys.stdout.write(
            f"{_DIM}[{event.stop_reason}; in={event.usage.get('input_tokens', 0)} "
            f"out={event.usage.get('output_tokens', 0)}]{_RESET}\n"
        )


async def _run() -> int:
    log = get_logger(__name__)
    load_dotenv()

    ha_url = os.environ.get("HA_URL")
    ha_token = os.environ.get("HA_TOKEN")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not (ha_url and ha_token and api_key):
        print(
            "error: HA_URL, HA_TOKEN, and ANTHROPIC_API_KEY must be set "
            "(in .env or the environment).",
            file=sys.stderr,
        )
        return 2

    config = load_config()
    prompt = load_system_prompt()
    provider = AnthropicProvider(api_key=api_key)

    tool_registry.load_all()
    tool_specs = [t.to_anthropic() for t in tool_registry.all_tools()]

    db_path = config.mylo_data_dir / "conversations.db"
    storage = ConversationStorage(db_path)
    await storage.init()
    conv = ConversationManager(storage=storage)
    # Keep only the last ~40 messages in the working window for now —
    # rolling summarization lands with M4b.
    await conv.load(limit=40)

    client = HaWsClient(ha_url, ha_token)

    try:
        await client.start()
        try:
            await client.wait_ready(timeout=15.0)
        except TimeoutError:
            log.error("chat.timeout_waiting_for_ready")
            return 1

        registries = await Registries.attach(client)
        await registries.wait_loaded(timeout=15.0)

        ctx = ToolContext(
            ws_client=client,
            registries=registries,
            config=config,
            permissions=default_permissions(),
            audit=AuditLogger(config.mylo_data_dir),
            conversation_id=conv.conversation_id,
            user_approved=False,
        )

        session_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}

        print(
            f"{_CYAN}Mylo{_RESET} ({config.model}, prompt v{prompt.version}, "
            f"{len(conv.history)} prior messages). Type /quit to exit.\n"
        )

        while True:
            try:
                user_line = await asyncio.to_thread(input, f"{_YELLOW}you> {_RESET}")
            except (EOFError, KeyboardInterrupt):
                print()
                return 0

            user_line = user_line.strip()
            if not user_line:
                continue

            if user_line == "/quit":
                return 0
            if user_line == "/clear":
                await conv.clear()
                print(f"{_DIM}(history cleared){_RESET}")
                continue
            if user_line == "/history":
                print(json.dumps(conv.history, indent=2, default=str))
                continue
            if user_line == "/usage":
                print(f"session usage: {session_usage}")
                continue

            try:
                async for event in run_turn(
                    user_message=user_line,
                    conversation=conv,
                    provider=provider,
                    ctx=ctx,
                    system=prompt.text,
                    tools=tool_specs,
                    model=config.model,
                    prompt_version=prompt.version,
                ):
                    if isinstance(event, TextEvent):
                        _print_text(event)
                    elif isinstance(event, ToolCallEvent):
                        _print_tool_call(event)
                    elif isinstance(event, ToolResultEvent):
                        _print_tool_result(event)
                    elif isinstance(event, DoneEvent):
                        for k, v in event.usage.items():
                            session_usage[k] = session_usage.get(k, 0) + v
                        _print_done(event, verbose=True)
            except Exception as exc:  # keep the REPL alive on errors
                print(f"{_RED}error: {exc}{_RESET}")
                log.exception("chat.turn_failed")

    except AuthFailed as exc:
        log.error("chat.auth_failed", error=str(exc))
        return 2
    finally:
        await client.close()


def main() -> int:
    configure_logging()
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
