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

"""Structural validation of Lovelace view configs.

Deliberately shallow: HA's per-card option schemas churn every release,
so chasing them would rot fast. What we CAN check cheaply and reliably:

* every card has a ``type``
* ``custom:*`` types actually exist in the installed lovelace resources
  (a missing one renders as "Custom element doesn't exist")
* sections/stacks/conditional nesting has the right shape

Native-type checking mirrors :mod:`automation_schema`'s stance: the
known list is "seen before", not an allowlist — HA adds card types, so
an unknown non-custom type is a warning, never an error.
"""

from __future__ import annotations

from typing import Any

from mylo.validators.automation_schema import ValidationReport

KNOWN_NATIVE_CARD_TYPES: frozenset[str] = frozenset(
    {
        "alarm-panel",
        "area",
        "button",
        "calendar",
        "conditional",
        "energy-distribution",
        "entities",
        "entity",
        "entity-filter",
        "gauge",
        "glance",
        "grid",
        "heading",
        "history-graph",
        "horizontal-stack",
        "humidifier",
        "iframe",
        "light",
        "logbook",
        "map",
        "markdown",
        "media-control",
        "picture",
        "picture-elements",
        "picture-entity",
        "picture-glance",
        "plant-status",
        "sensor",
        "statistic",
        "statistics-graph",
        "thermostat",
        "tile",
        "todo-list",
        "vertical-stack",
        "weather-forecast",
    }
)


def validate_view(
    view: dict[str, Any], installed_custom: set[str] | None
) -> ValidationReport:
    """Structure-check one view. ``installed_custom`` is the set of
    available ``custom:*`` card types, or None when resources couldn't
    be listed (then custom types warn instead of erroring)."""
    report = ValidationReport()

    is_sections = view.get("type") == "sections" or isinstance(view.get("sections"), list)

    if is_sections:
        if view.get("cards"):
            report.warn(
                "view",
                "view has both 'cards' and 'sections' — sections-layout views "
                "should keep all cards inside sections",
            )
        max_columns = view.get("max_columns")
        if isinstance(max_columns, int) and not 1 <= max_columns <= 6:
            report.warn("view.max_columns", f"max_columns {max_columns} outside 1-6")

        sections = view.get("sections")
        if not isinstance(sections, list):
            report.error("view.sections", "'sections' must be a list of section dicts")
        else:
            for i, section in enumerate(sections):
                path = f"sections[{i}]"
                if not isinstance(section, dict):
                    report.error(path, "section must be a dict {type: grid, cards: [...]}")
                    continue
                cards = section.get("cards")
                if cards is not None and not isinstance(cards, list):
                    report.error(f"{path}.cards", "section 'cards' must be a list")
                    continue
                for j, card in enumerate(cards or []):
                    _validate_card(card, f"{path}.cards[{j}]", installed_custom, report)

    top_cards = view.get("cards")
    if isinstance(top_cards, list):
        for j, card in enumerate(top_cards):
            _validate_card(card, f"cards[{j}]", installed_custom, report)

    return report


def _validate_card(
    card: Any,
    path: str,
    installed_custom: set[str] | None,
    report: ValidationReport,
) -> None:
    if not isinstance(card, dict):
        report.error(path, "card must be a dict")
        return

    card_type = card.get("type")
    if not isinstance(card_type, str) or not card_type:
        report.error(path, "card is missing 'type'")
    elif card_type.startswith("custom:"):
        if installed_custom is None:
            report.warn(
                path,
                f"{card_type!r} can't be verified — lovelace resources are "
                "not listable; prefer native cards when unsure",
            )
        elif card_type not in installed_custom:
            report.error(
                path,
                f"{card_type!r} is not installed (no matching lovelace "
                "resource) — it would render as 'Custom element doesn't "
                "exist'. Use a native card instead (tile/entities/heading) "
                "or an installed custom card",
            )
    elif card_type not in KNOWN_NATIVE_CARD_TYPES:
        report.warn(path, f"unrecognized card type {card_type!r} — double-check the name")

    # Recurse into the standard nesting shapes.
    if "cards" in card:
        nested = card.get("cards")
        if not isinstance(nested, list):
            report.error(f"{path}.cards", "'cards' must be a list")
        else:
            for j, sub in enumerate(nested):
                _validate_card(sub, f"{path}.cards[{j}]", installed_custom, report)
    if "card" in card:
        sub = card.get("card")
        if not isinstance(sub, dict):
            report.error(f"{path}.card", "'card' must be a dict")
        else:
            _validate_card(sub, f"{path}.card", installed_custom, report)


def has_custom_card(obj: Any) -> bool:
    """True when any card in the tree uses a ``custom:*`` type — lets
    callers skip the resources lookup entirely for all-native views."""
    if isinstance(obj, dict):
        card_type = obj.get("type")
        if isinstance(card_type, str) and card_type.startswith("custom:"):
            return True
        return any(has_custom_card(v) for v in obj.values())
    if isinstance(obj, list):
        return any(has_custom_card(item) for item in obj)
    return False
