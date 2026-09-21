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

"""Custom card authoring: contract checks, staging store, resource helper.

A custom card is one JavaScript module at ``www/mylo-cards/<element>.js``
defining a ``mylo-*`` element. HA cannot validate JavaScript for us, so
``check_card_source`` enforces a small contract with regex/line checks:
enough to reject a card that cannot load or that reaches outside HA.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

MAX_CARD_BYTES = 65_536
ELEMENT_RE = re.compile(r"^mylo-[a-z0-9]+(-[a-z0-9]+)*$")
MAX_ELEMENT_LEN = 48

Severity = Literal["error", "warning"]


@dataclass(slots=True)
class CardIssue:
    code: str
    message: str
    severity: Severity = "error"

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "severity": self.severity}


_EXTENDS_RE = re.compile(r"\bclass\s+\w+\s+extends\s+HTMLElement\b")
_SET_CONFIG_RE = re.compile(r"\bsetConfig\s*\(")
_HASS_SETTER_RE = re.compile(r"\bset\s+hass\s*\(")
_DYNAMIC_RE = re.compile(r"\beval\s*\(|\bnew\s+Function\s*\(|\bdocument\.write\s*\(")
_NETWORK_RE = re.compile(
    r"\bfetch\s*\(|\bXMLHttpRequest\b|\bnew\s+WebSocket\s*\(|\bnavigator\.sendBeacon\s*\("
)
_SCRIPT_TAG_RE = re.compile(r"<script", re.IGNORECASE)
_INNERHTML_RE = re.compile(r"\.innerHTML\s*[+]?=")
_STATE_WORD_RE = re.compile(r"\bhass\b|\bstate\b|\bstates\b")


def _define_re(element: str) -> re.Pattern[str]:
    return re.compile(r"customElements\s*\.\s*define\s*\(\s*['\"]" + re.escape(element) + r"['\"]")


def _get_re(element: str) -> re.Pattern[str]:
    return re.compile(r"customElements\s*\.\s*get\s*\(\s*['\"]" + re.escape(element) + r"['\"]")


def _picker_type_re(element: str) -> re.Pattern[str]:
    return re.compile(r"type\s*:\s*['\"]" + re.escape(element) + r"['\"]")


def check_card_source(element: str, source: str) -> list[CardIssue]:
    issues: list[CardIssue] = []

    if not ELEMENT_RE.match(element) or len(element) > MAX_ELEMENT_LEN:
        issues.append(
            CardIssue(
                "element_name",
                f"element {element!r} must match mylo-<lowercase-words> and be <= "
                f"{MAX_ELEMENT_LEN} chars",
            )
        )

    raw = source.encode("utf-8", errors="strict")
    if len(raw) > MAX_CARD_BYTES or "\x00" in source:
        issues.append(
            CardIssue("source_size", f"source must be <= {MAX_CARD_BYTES} bytes with no NUL bytes")
        )

    defines = list(_define_re(element).finditer(source))
    if len(defines) != 1:
        issues.append(
            CardIssue(
                "defines_element",
                f"source must call customElements.define({element!r}, ...) exactly once "
                f"(found {len(defines)})",
            )
        )
    else:
        guard = _get_re(element).search(source)
        if guard is None or guard.start() > defines[0].start():
            issues.append(
                CardIssue(
                    "guards_define",
                    f"wrap the define in `if (!customElements.get({element!r})) {{ ... }}` "
                    "so a reload does not throw",
                )
            )

    if not _EXTENDS_RE.search(source):
        issues.append(CardIssue("extends_htmlelement", "card class must `extends HTMLElement`"))
    if not _SET_CONFIG_RE.search(source):
        issues.append(CardIssue("has_set_config", "card class must define setConfig(config)"))
    if not _HASS_SETTER_RE.search(source):
        issues.append(CardIssue("has_hass_setter", "card class must define `set hass(hass)`"))

    if "window.customCards" not in source or not _picker_type_re(element).search(source):
        issues.append(
            CardIssue(
                "registers_picker",
                "push {type: <element>, name, description} onto window.customCards so the "
                "card picker lists it",
                severity="warning",
            )
        )

    for lineno, line in enumerate(source.splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("import ") or stripped.startswith("export "):
            issues.append(
                CardIssue("no_imports", f"line {lineno}: imports/exports are not allowed")
            )
        if _INNERHTML_RE.search(line) and _STATE_WORD_RE.search(line):
            issues.append(
                CardIssue(
                    "innerhtml_with_state",
                    f"line {lineno}: assigning innerHTML from state values — use textContent",
                    severity="warning",
                )
            )

    if _DYNAMIC_RE.search(source):
        issues.append(
            CardIssue("no_dynamic_code", "eval / new Function / document.write are not allowed")
        )
    if _NETWORK_RE.search(source):
        issues.append(
            CardIssue(
                "no_network",
                "fetch / XMLHttpRequest / WebSocket / sendBeacon are not allowed — read "
                "hass.states and act via hass.callService",
            )
        )
    if _SCRIPT_TAG_RE.search(source):
        issues.append(CardIssue("no_script_tag", "'<script' string literals are not allowed"))

    return issues


REFERENCE_CARD_SOURCE = """class MyloEntityRow extends HTMLElement {
  setConfig(config) {
    if (!config || !config.entity) throw new Error("mylo-entity-row: 'entity' is required");
    this._config = config;
    if (!this._root) {
      this._root = this.attachShadow({ mode: "open" });
      this._root.innerHTML = `
        <ha-card>
          <div class="row">
            <ha-icon id="icon"></ha-icon>
            <span id="name"></span>
            <span id="value"></span>
          </div>
        </ha-card>
        <style>
          .row { display: flex; align-items: center; gap: 12px; padding: 12px 16px; cursor: pointer; }
          #name { flex: 1; }
          #value { color: var(--secondary-text-color); }
        </style>`;
      this._root.querySelector(".row").addEventListener("click", () => this._toggle());
    }
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _render() {
    if (!this._hass || !this._config || !this._root) return;
    const st = this._hass.states[this._config.entity];
    const name = this._config.name || (st && st.attributes.friendly_name) || this._config.entity;
    this._root.getElementById("name").textContent = name;
    this._root.getElementById("value").textContent = st ? st.state : "unavailable";
    const icon = this._config.icon || (st && st.attributes.icon) || "mdi:circle";
    this._root.getElementById("icon").setAttribute("icon", icon);
  }

  _toggle() {
    if (!this._hass) return;
    this._hass.callService("homeassistant", "toggle", { entity_id: this._config.entity });
  }

  getCardSize() {
    return 1;
  }

  static getStubConfig() {
    return { entity: "light.example" };
  }
}

if (!customElements.get("mylo-entity-row")) {
  customElements.define("mylo-entity-row", MyloEntityRow);
}

window.customCards = window.customCards || [];
window.customCards.push({
  type: "mylo-entity-row",
  name: "Mylo entity row",
  description: "Compact tappable entity row (icon, name, state).",
});
"""
