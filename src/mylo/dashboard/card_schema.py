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

Checks that are cheap and stable across HA releases:

* every card has a ``type``
* ``custom:*`` types exist in the installed lovelace resources
* sections/stacks/conditional nesting has the right shape
* the handful of options a card cannot render without (``REQUIRED_OPTIONS``)
* ``grid_options`` shape (per-card sizing in sections layout)

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
        "clock",
        "conditional",
        "energy-carbon-consumed-gauge",
        "energy-date-selection",
        "energy-devices-detail-graph",
        "energy-devices-graph",
        "energy-distribution",
        "energy-gas-graph",
        "energy-grid-neutrality-gauge",
        "energy-sankey",
        "energy-self-sufficiency-gauge",
        "energy-solar-consumed-gauge",
        "energy-solar-graph",
        "energy-sources-table",
        "energy-usage-graph",
        "energy-water-graph",
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
        "shopping-list",
        "statistic",
        "statistics-graph",
        "thermostat",
        "tile",
        "todo-list",
        "vertical-stack",
        "weather-forecast",
    }
)

# Options a card cannot render without. Outer tuple = requirements (all
# must hold); inner tuple = alternatives (any one satisfies). Types not
# listed are not option-checked — HA's per-card schemas churn.
REQUIRED_OPTIONS: dict[str, tuple[tuple[str, ...], ...]] = {
    **{
        t: (("entity",),)
        for t in (
            "tile",
            "entity",
            "sensor",
            "gauge",
            "thermostat",
            "humidifier",
            "light",
            "media-control",
            "weather-forecast",
            "alarm-panel",
            "plant-status",
            "picture-entity",
            "statistic",
            "todo-list",
        )
    },
    **{
        t: (("entities",),)
        for t in (
            "entities",
            "glance",
            "history-graph",
            "statistics-graph",
            "logbook",
            "calendar",
            "picture-glance",
        )
    },
    "button": (("entity", "tap_action"),),
    "conditional": (("conditions",), ("card",)),
    "vertical-stack": (("cards",),),
    "horizontal-stack": (("cards",),),
    "grid": (("cards",),),
    "markdown": (("content",),),
    "picture": (("image",),),
    "iframe": (("url",),),
    "map": (("entities", "geo_location_sources"),),
    "area": (("area",),),
}


def validate_view(view: dict[str, Any], installed_custom: set[str] | None) -> ValidationReport:
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

    if isinstance(card_type, str):
        for group in REQUIRED_OPTIONS.get(card_type, ()):
            if not any(card.get(key) not in (None, "", [], {}) for key in group):
                report.error(
                    path,
                    f"{card_type} card is missing required option {' or '.join(group)!s}",
                )
    _validate_grid_options(card, path, report)

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


def _validate_grid_options(card: dict[str, Any], path: str, report: ValidationReport) -> None:
    opts = card.get("grid_options")
    if opts is None:
        return
    if not isinstance(opts, dict):
        report.error(f"{path}.grid_options", "grid_options must be a mapping")
        return

    def _is_int(v: Any) -> bool:
        return isinstance(v, int) and not isinstance(v, bool)

    columns = opts.get("columns")
    if columns is not None and not (columns == "full" or (_is_int(columns) and 1 <= columns <= 12)):
        report.error(f"{path}.grid_options.columns", "columns must be an int 1-12 or 'full'")
    rows = opts.get("rows")
    if rows is not None and not (rows == "auto" or (_is_int(rows) and rows >= 1)):
        report.error(f"{path}.grid_options.rows", "rows must be a positive int or 'auto'")
    for key in ("min_columns", "max_columns", "min_rows", "max_rows"):
        value = opts.get(key)
        if value is not None and not (_is_int(value) and value >= 1):
            report.error(f"{path}.grid_options.{key}", f"{key} must be a positive int")


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
