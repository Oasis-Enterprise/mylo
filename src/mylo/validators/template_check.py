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

"""Jinja2 template validation + entity-reference extraction.

Automations are full of inline Jinja: ``{{ states('sensor.x') }}``,
``{{ state_attr('climate.y', 'temperature') }}``, and so on. Broken
templates silently produce ``None`` at runtime, which then silently
breaks the automation — the LLM emitting a typo'd template is the
silent-failure class we most want to catch before writing to disk.

We walk the Jinja AST to find ``states()``, ``state_attr()``,
``is_state()``, ``is_state_attr()``, ``expand()`` calls — each takes an
entity_id as its first arg. Extracted refs flow to the resolver.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jinja2 import Environment, TemplateSyntaxError, meta, nodes


@dataclass(slots=True)
class TemplateCheckResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    entity_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "entity_refs": sorted(set(self.entity_refs)),
        }


# Entity-accepting builtins in HA's template environment.
_ENTITY_FUNCTIONS: frozenset[str] = frozenset(
    {"states", "state_attr", "is_state", "is_state_attr", "expand", "iif"}
)


def _env() -> Environment:
    # No autoescape — HA templates are not HTML. Loader unused.
    return Environment(autoescape=False)


def check_template(template: str) -> TemplateCheckResult:
    """Parse a single Jinja template string and extract entity refs."""
    env = _env()
    try:
        ast = env.parse(template)
    except TemplateSyntaxError as exc:
        return TemplateCheckResult(ok=False, errors=[f"line {exc.lineno}: {exc.message}"])

    refs = _extract_entity_refs(ast)
    # Also warn on undefined variables that aren't HA globals. HA templates
    # have ``states``, ``now``, ``utcnow``, ``today_at``, etc. We focus on
    # entity refs for this version.
    _ = meta.find_undeclared_variables(ast)
    return TemplateCheckResult(ok=True, entity_refs=refs)


def _extract_entity_refs(node: nodes.Node) -> list[str]:
    out: list[str] = []
    for sub in node.find_all(nodes.Call):
        name = _function_name(sub)
        if name not in _ENTITY_FUNCTIONS:
            continue
        if not sub.args:
            continue
        first = sub.args[0]
        if isinstance(first, nodes.Const) and isinstance(first.value, str):
            out.append(first.value)
    return out


def _function_name(call: nodes.Call) -> str | None:
    node = call.node
    if isinstance(node, nodes.Name):
        return node.name
    if isinstance(node, nodes.Getattr):
        return node.attr
    return None


def scan_config_for_templates(config: Any) -> list[tuple[str, str]]:
    """Walk a parsed YAML config, return ``[(path, template_string)]``.

    An entry is recognized as a template when its string value contains
    ``{{`` or ``{%``, OR the key is a known template field (e.g.
    ``value_template``, ``state_template``). Templates inline inside
    service ``data`` / ``target`` show up via the first rule.
    """
    out: list[tuple[str, str]] = []
    _walk(config, "", out)
    return out


_TEMPLATE_KEYS: frozenset[str] = frozenset(
    {
        "value_template",
        "state_template",
        "availability_template",
        "icon_template",
        "friendly_name_template",
        "unit_of_measurement_template",
    }
)


def _walk(value: Any, path: str, out: list[tuple[str, str]]) -> None:
    if isinstance(value, str):
        if "{{" in value or "{%" in value:
            out.append((path, value))
        return
    if isinstance(value, dict):
        for k, v in value.items():
            key_path = f"{path}.{k}" if path else str(k)
            if k in _TEMPLATE_KEYS and isinstance(v, str):
                out.append((key_path, v))
            else:
                _walk(v, key_path, out)
        return
    if isinstance(value, list):
        for i, v in enumerate(value):
            _walk(v, f"{path}[{i}]", out)
