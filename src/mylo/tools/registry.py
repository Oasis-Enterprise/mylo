"""Tool registry — the catalogue of all tools available to the LLM.

All tools import and call :func:`register` at module-import time. The
:func:`load_all` helper imports the tool modules so that registration happens
before the first lookup.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterable
from typing import Any

from mylo.tools.base import ToolDefinition

_REGISTRY: dict[str, ToolDefinition[Any]] = {}

# Modules to import when loading the default tool set. Each module registers
# exactly one tool at import time. Adding a tool = adding a line here.
_DEFAULT_MODULES: tuple[str, ...] = (
    "mylo.tools.read.query_entities",
    "mylo.tools.read.query_devices",
    "mylo.tools.read.query_automations",
    "mylo.tools.read.query_dashboard",
    "mylo.tools.read.query_logs",
    "mylo.tools.read.query_system",
    "mylo.tools.read.read_config_file",
    "mylo.tools.read.verify_change",
    "mylo.tools.read.memory_note",
    # M7a: tier-2 write tools and tier-3 actions.
    "mylo.tools.write.write_config_file",
    "mylo.tools.write.patch_config_file",
    "mylo.tools.write.modify_automation",
    "mylo.tools.action.call_service",
    "mylo.tools.action.reload_config",
    # M7b: organizational + dashboard tools.
    "mylo.tools.write.modify_areas",
    "mylo.tools.write.manage_labels",
    "mylo.tools.write.modify_dashboard",
    "mylo.tools.write.rename_entities",
)


def register(tool: ToolDefinition[Any]) -> ToolDefinition[Any]:
    if tool.name in _REGISTRY:
        raise ValueError(f"tool already registered: {tool.name!r}")
    _REGISTRY[tool.name] = tool
    return tool


def get(name: str) -> ToolDefinition[Any] | None:
    return _REGISTRY.get(name)


def all_tools() -> Iterable[ToolDefinition[Any]]:
    return list(_REGISTRY.values())


def names() -> list[str]:
    return sorted(_REGISTRY.keys())


def load_all(modules: tuple[str, ...] = _DEFAULT_MODULES) -> None:
    """Import every tool module so its :func:`register` call executes.

    Uses ``importlib.reload`` for already-imported modules so tests that
    reset the registry can re-trigger registration.
    """
    for mod in modules:
        existing = sys.modules.get(mod)
        if existing is not None:
            importlib.reload(existing)
        else:
            importlib.import_module(mod)


def _reset_for_tests() -> None:
    """Clear the registry — for unit tests that register synthetic tools."""
    _REGISTRY.clear()
