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

"""Discover the dashboard environment: installed themes and lovelace
resources (HACS custom cards).

Both lookups degrade to ``None`` when HA can't answer (old HA, YAML-mode
resources, missing admin rights) — callers treat ``None`` as "unknown,
don't enforce" rather than "empty".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from mylo.ha.ws_client import CommandError
from mylo.logging_setup import get_logger

log = get_logger(__name__)


class _WsClient(Protocol):
    async def send_command(self, type_: str, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class ThemesInfo:
    names: list[str]
    default: str | None


async def get_themes(ws_client: _WsClient) -> ThemesInfo | None:
    """Installed frontend themes, or None when HA can't list them."""
    try:
        result = await ws_client.send_command("frontend/get_themes")
    except CommandError:
        return None
    if not isinstance(result, dict):
        return None
    themes = result.get("themes")
    if not isinstance(themes, dict):
        return None
    return ThemesInfo(names=sorted(themes), default=result.get("default_theme"))


async def get_resources(ws_client: _WsClient) -> list[dict[str, Any]] | None:
    """Registered lovelace resources, or None when unlistable
    (YAML-mode resources, insufficient rights)."""
    try:
        result = await ws_client.send_command("lovelace/resources")
    except CommandError:
        return None
    if not isinstance(result, list):
        return None
    return [r for r in result if isinstance(r, dict)]


# Resource basename → the card element names that bundle provides.
# Single-element bundles fall through to the generic basename rule; only
# multi-element bundles (or ones whose file name doesn't match their
# element name) need an entry here.
_CARD_FAMILIES: dict[str, tuple[str, ...]] = {
    "mushroom": (
        "mushroom-alert-card",
        "mushroom-chips-card",
        "mushroom-climate-card",
        "mushroom-cover-card",
        "mushroom-entity-card",
        "mushroom-fan-card",
        "mushroom-humidifier-card",
        "mushroom-light-card",
        "mushroom-lock-card",
        "mushroom-media-player-card",
        "mushroom-number-card",
        "mushroom-person-card",
        "mushroom-select-card",
        "mushroom-template-card",
        "mushroom-title-card",
        "mushroom-update-card",
        "mushroom-vacuum-card",
    ),
    "bubble-card": ("bubble-card",),
    "button-card": ("button-card",),
    "apexcharts-card": ("apexcharts-card",),
    "mini-graph-card": ("mini-graph-card",),
    "auto-entities": ("auto-entities",),
    "card-mod": ("card-mod",),
}


def detect_custom_cards(resources: list[dict[str, Any]] | None) -> set[str]:
    """Map registered resources to the ``custom:*`` card types they provide.

    Best-effort by URL basename: known bundles expand to their element
    families; anything else contributes its cleaned basename. The result
    is used to steer the model toward installed cards and to flag
    definitely-missing ones — unknown bundles erring toward inclusion is
    fine.
    """
    cards: set[str] = set()
    for resource in resources or []:
        url = resource.get("url")
        if not isinstance(url, str) or not url:
            continue
        basename = url.rsplit("/", 1)[-1].split("?", 1)[0]
        for suffix in (".js", "-bundle", ".min"):
            if basename.endswith(suffix):
                basename = basename[: -len(suffix)]
        basename = basename.removeprefix("lovelace-")
        if not basename:
            continue
        elements = _CARD_FAMILIES.get(basename, (basename,))
        cards.update(f"custom:{el}" for el in elements)
    return cards
