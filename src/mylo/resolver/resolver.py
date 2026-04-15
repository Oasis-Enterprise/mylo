"""Reference resolver — the "checksum-for-entity-IDs" we agreed to build.

Every write tool validates entity/device/area refs through this resolver
before touching HA. Three outcomes per ref:

1. **Hit** — ref exists in the registry verbatim. Pass.
2. **Close fuzzy match** (score ≥ ``AUTO_CORRECT_THRESHOLD``, unique) — we
   record an auto-correction the write tool can apply with user approval.
   NEVER auto-applied for tier-3 service calls that touch physical devices.
3. **Miss or ambiguous** — return a :class:`RefMismatch` with
   ``did_you_mean`` suggestions. The write tool surfaces this through the
   standard error envelope; the LLM retry loop fixes itself on the next
   turn.

Performance note: rapidfuzz's ``process.extract`` is C-fast. Even at 2204
entities a single ref lookup is microseconds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from rapidfuzz import fuzz, process

from mylo.ha.registries import Registries
from mylo.resolver.errors import (
    MismatchDict,
    area_not_found,
    device_not_found,
    entity_not_found,
)

# ``≥ this similarity + single candidate`` = auto-correctable.
AUTO_CORRECT_THRESHOLD: float = 92.0

# ``score_cutoff`` for producing suggestions at all. Lower threshold here
# so did_you_mean stays useful even when the LLM made a larger typo.
SUGGESTION_THRESHOLD: float = 60.0

Kind = Literal["entity", "device", "area"]


@dataclass(slots=True)
class ResolvedRef:
    kind: Kind
    original: str
    resolved: str
    # True when ``resolved != original`` — caller should surface the
    # correction to the user if they care.
    corrected: bool
    score: float


@dataclass(slots=True)
class RefMismatch:
    kind: Kind
    original: str
    suggestions: list[str]

    def to_envelope(self) -> MismatchDict:
        if self.kind == "entity":
            return entity_not_found(self.original, self.suggestions)
        if self.kind == "device":
            return device_not_found(self.original, self.suggestions)
        return area_not_found(self.original, self.suggestions)


class Resolver:
    """Resolver wrapping a :class:`Registries` snapshot.

    One instance per turn is fine — registries are live-updated underneath.
    Rebuild the catalog by calling :meth:`refresh` if a huge batch of
    registry changes has landed between calls.
    """

    def __init__(self, registries: Registries) -> None:
        self._registries = registries
        # Haystacks indexed so we don't rebuild per lookup.
        self._entities: list[str] = []
        self._devices_by_name: dict[str, str] = {}
        self._areas: list[str] = []
        self._area_ids_by_name: dict[str, str] = {}
        self.refresh()

    def refresh(self) -> None:
        self._entities = sorted(self._registries.entities.keys())
        # Devices are referred to by id internally but humans use display
        # names; index both so the resolver can accept either form.
        self._devices_by_name = {}
        for d in self._registries.devices.values():
            for key in filter(None, (d.id, d.name_by_user, d.name)):
                self._devices_by_name.setdefault(key.lower(), d.id)
        self._areas = sorted(a.area_id for a in self._registries.areas.values())
        self._area_ids_by_name = {}
        for a in self._registries.areas.values():
            self._area_ids_by_name.setdefault(a.area_id.lower(), a.area_id)
            self._area_ids_by_name.setdefault(a.name.lower(), a.area_id)

    # ─── Entity ─────────────────────────────────────────────────────────────

    def resolve_entity(
        self, ref: str, *, allow_auto_correct: bool = True
    ) -> ResolvedRef | RefMismatch:
        if ref in self._registries.entities:
            return ResolvedRef("entity", ref, ref, corrected=False, score=100.0)

        suggestions = _top_matches(ref, self._entities)

        if (
            allow_auto_correct
            and suggestions
            and suggestions[0][1] >= AUTO_CORRECT_THRESHOLD
            and (len(suggestions) == 1 or suggestions[0][1] - suggestions[1][1] >= 5)
        ):
            best, score = suggestions[0]
            return ResolvedRef("entity", ref, best, corrected=True, score=score)

        return RefMismatch("entity", ref, [name for name, _ in suggestions])

    # ─── Device ─────────────────────────────────────────────────────────────

    def resolve_device(
        self, ref: str, *, allow_auto_correct: bool = True
    ) -> ResolvedRef | RefMismatch:
        # Accept either internal id or display name (case-insensitive).
        if ref in self._registries.devices:
            return ResolvedRef("device", ref, ref, corrected=False, score=100.0)
        hit = self._devices_by_name.get(ref.lower())
        if hit is not None:
            return ResolvedRef("device", ref, hit, corrected=ref != hit, score=100.0)

        name_haystack = list(self._devices_by_name.keys())
        suggestions = _top_matches(ref.lower(), name_haystack)
        resolved_ids: list[str] = []
        for name, _ in suggestions:
            resolved = self._devices_by_name.get(name)
            if resolved and resolved not in resolved_ids:
                resolved_ids.append(resolved)

        if (
            allow_auto_correct
            and suggestions
            and suggestions[0][1] >= AUTO_CORRECT_THRESHOLD
            and len(resolved_ids) == 1
        ):
            return ResolvedRef(
                "device",
                ref,
                resolved_ids[0],
                corrected=True,
                score=suggestions[0][1],
            )

        return RefMismatch("device", ref, resolved_ids[:5])

    # ─── Area ───────────────────────────────────────────────────────────────

    def resolve_area(
        self, ref: str, *, allow_auto_correct: bool = True
    ) -> ResolvedRef | RefMismatch:
        hit = self._area_ids_by_name.get(ref.lower())
        if hit is not None:
            return ResolvedRef("area", ref, hit, corrected=ref != hit, score=100.0)

        name_haystack = list(self._area_ids_by_name.keys())
        suggestions = _top_matches(ref.lower(), name_haystack)
        resolved_ids: list[str] = []
        for name, _ in suggestions:
            resolved = self._area_ids_by_name.get(name)
            if resolved and resolved not in resolved_ids:
                resolved_ids.append(resolved)

        if (
            allow_auto_correct
            and suggestions
            and suggestions[0][1] >= AUTO_CORRECT_THRESHOLD
            and len(resolved_ids) == 1
        ):
            return ResolvedRef(
                "area",
                ref,
                resolved_ids[0],
                corrected=True,
                score=suggestions[0][1],
            )

        return RefMismatch("area", ref, resolved_ids[:5])


def _top_matches(query: str, haystack: list[str]) -> list[tuple[str, float]]:
    """Return up to 5 (name, score) candidates, score ≥ SUGGESTION_THRESHOLD."""
    if not haystack:
        return []
    results = process.extract(
        query,
        haystack,
        scorer=fuzz.WRatio,
        limit=5,
        score_cutoff=SUGGESTION_THRESHOLD,
    )
    # rapidfuzz returns (name, score, index); we only need name+score.
    return [(name, score) for name, score, _ in results]
