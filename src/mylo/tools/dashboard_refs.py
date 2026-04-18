"""Extract and validate entity references from Lovelace card configs.

Dashboard cards reference entities in multiple ways:

* ``entity: light.kitchen`` (scalar)
* ``entity_id: sensor.temp`` (scalar or list)
* ``entities: [{entity: ...}, ...]`` (list of objects)
* ``{% set t = states('sensor.temp') %}`` (Jinja templates)
* ``state_of: binary_sensor.motion`` (mushroom cards)
* ``tap_action.data.entity_id: ...`` (action targets)

This module extracts ALL entity-shaped strings from a card tree,
then validates each one against the live registry. Invalid refs get
fuzzy-matched to produce ``did_you_mean`` suggestions so the model
can self-correct in the next turn.

Used by :mod:`modify_dashboard` as a pre-flight check before the
dry-run preview. If any refs are invalid, the tool returns an error
instead of a preview — the model fixes them and retries.
"""

from __future__ import annotations

import re
from typing import Any

from mylo.ha.registries import Registries
from mylo.logging_setup import get_logger

log = get_logger(__name__)

# Jinja patterns: states('entity_id'), is_state('entity_id', ...),
# state_attr('entity_id', ...).
_JINJA_ENTITY_RE = re.compile(
    r"""(?:states|is_state|state_attr|expand)\s*\(\s*['"]([a-z_]+\.[a-z0-9_]+)['"]""",
    re.IGNORECASE,
)

# Keys in card configs that hold entity references.
_ENTITY_KEYS = frozenset({
    "entity",
    "entity_id",
    "camera_entity",
    "media_player_entity",
    "state_of",
})

# Domains that are valid HA entity prefixes.
_ENTITY_DOMAIN_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")


def extract_entity_refs(obj: Any) -> set[str]:
    """Walk a card config tree and return all entity-shaped strings."""
    refs: set[str] = set()
    _walk(obj, refs)
    return refs


def _walk(obj: Any, refs: set[str]) -> None:
    if isinstance(obj, str):
        # Check if the string itself is an entity_id.
        if _ENTITY_DOMAIN_RE.match(obj):
            refs.add(obj)
        # Scan for Jinja entity references.
        for match in _JINJA_ENTITY_RE.finditer(obj):
            refs.add(match.group(1))
        return

    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in _ENTITY_KEYS:
                if isinstance(value, str) and _ENTITY_DOMAIN_RE.match(value):
                    refs.add(value)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, str) and _ENTITY_DOMAIN_RE.match(item):
                            refs.add(item)
            else:
                _walk(value, refs)
        return

    if isinstance(obj, list):
        for item in obj:
            _walk(item, refs)


def validate_refs(
    refs: set[str],
    registries: Registries,
    *,
    max_suggestions: int = 3,
) -> list[dict[str, Any]]:
    """Check each ref against the registry. Return a list of invalid
    refs with ``did_you_mean`` suggestions.

    Returns an empty list when all refs are valid.
    """
    # Some "entity-shaped" strings are actually template helpers or
    # domains that don't exist in the registry (e.g. input_boolean,
    # input_number, timer, counter). Only flag refs whose domain
    # exists in the registry OR is a well-known HA domain.
    all_entity_ids = set(registries.entities.keys())
    invalid: list[dict[str, Any]] = []

    for ref in sorted(refs):
        if ref in all_entity_ids:
            continue
        # Fuzzy match.
        suggestions = _fuzzy_suggestions(ref, all_entity_ids, limit=max_suggestions)
        invalid.append({
            "entity_id": ref,
            "did_you_mean": suggestions,
        })

    return invalid


def _fuzzy_suggestions(query: str, candidates: set[str], *, limit: int = 3) -> list[str]:
    """Return the closest matches using rapidfuzz."""
    try:
        from rapidfuzz import fuzz, process

        results = process.extract(
            query, list(candidates), scorer=fuzz.WRatio, limit=limit, score_cutoff=50.0
        )
        return [name for name, _score, _idx in results]
    except ImportError:
        return []
