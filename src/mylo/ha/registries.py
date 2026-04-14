"""In-memory cache of HA's entity / device / area / label registries.

The registries are fetched on every READY transition (initial connect and
after reconnects) and refreshed whenever HA emits a ``*_registry_updated``
event. Refresh strategy is "refetch the whole list" — simple, correct, and
these events are rare (minutes or hours apart in normal operation).

The :class:`Registries` cache is safe to read from any coroutine: all mutation
happens inside a single asyncio task (the event dispatcher), so readers see
consistent snapshots between events.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from mylo.ha.ws_client import HaWsClient, Subscription
from mylo.logging_setup import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class EntityEntry:
    entity_id: str
    name: str | None
    original_name: str | None
    platform: str | None
    device_id: str | None
    area_id: str | None
    labels: tuple[str, ...]
    disabled_by: str | None
    hidden_by: str | None

    @property
    def friendly_name(self) -> str:
        return self.name or self.original_name or self.entity_id

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> EntityEntry:
        labels = raw.get("labels") or []
        return cls(
            entity_id=raw["entity_id"],
            name=raw.get("name"),
            original_name=raw.get("original_name"),
            platform=raw.get("platform"),
            device_id=raw.get("device_id"),
            area_id=raw.get("area_id"),
            labels=tuple(labels),
            disabled_by=raw.get("disabled_by"),
            hidden_by=raw.get("hidden_by"),
        )


@dataclass(frozen=True, slots=True)
class DeviceEntry:
    id: str
    name: str | None
    name_by_user: str | None
    manufacturer: str | None
    model: str | None
    area_id: str | None
    labels: tuple[str, ...]
    disabled_by: str | None

    @property
    def display_name(self) -> str:
        return self.name_by_user or self.name or self.id

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> DeviceEntry:
        labels = raw.get("labels") or []
        return cls(
            id=raw["id"],
            name=raw.get("name"),
            name_by_user=raw.get("name_by_user"),
            manufacturer=raw.get("manufacturer"),
            model=raw.get("model"),
            area_id=raw.get("area_id"),
            labels=tuple(labels),
            disabled_by=raw.get("disabled_by"),
        )


@dataclass(frozen=True, slots=True)
class AreaEntry:
    area_id: str
    name: str
    floor_id: str | None
    labels: tuple[str, ...]

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> AreaEntry:
        labels = raw.get("labels") or []
        return cls(
            area_id=raw["area_id"],
            name=raw.get("name", raw["area_id"]),
            floor_id=raw.get("floor_id"),
            labels=tuple(labels),
        )


@dataclass(frozen=True, slots=True)
class LabelEntry:
    label_id: str
    name: str
    color: str | None

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> LabelEntry:
        return cls(
            label_id=raw["label_id"],
            name=raw.get("name", raw["label_id"]),
            color=raw.get("color"),
        )


@dataclass(slots=True)
class Registries:
    """Live cache of HA registries. Created via :meth:`attach`."""

    entities: dict[str, EntityEntry] = field(default_factory=dict)
    devices: dict[str, DeviceEntry] = field(default_factory=dict)
    areas: dict[str, AreaEntry] = field(default_factory=dict)
    labels: dict[str, LabelEntry] = field(default_factory=dict)

    _client: HaWsClient | None = None
    _refresh_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _loaded_event: asyncio.Event = field(default_factory=asyncio.Event)
    _subs: list[Subscription] = field(default_factory=list)

    # ─── Lifecycle ──────────────────────────────────────────────────────────

    @classmethod
    async def attach(cls, client: HaWsClient) -> Registries:
        """Bind to a client: initial fetch on ready + live updates thereafter."""
        reg = cls()
        reg._client = client
        client.on_ready(reg._on_client_ready)
        # If the client is already ready (unusual but possible), fetch now.
        if client.state.value == "ready":
            await reg.refresh()
            reg._loaded_event.set()
        for event_type in (
            "entity_registry_updated",
            "device_registry_updated",
            "area_registry_updated",
            "label_registry_updated",
        ):
            sub = await client.subscribe_events(event_type, reg._make_handler(event_type))
            reg._subs.append(sub)
        return reg

    async def wait_loaded(self, timeout: float | None = None) -> None:  # noqa: ASYNC109
        # Public API: callers pass a timeout; we map it to asyncio.wait_for.
        await asyncio.wait_for(self._loaded_event.wait(), timeout=timeout)

    async def _on_client_ready(self) -> None:
        await self.refresh()
        self._loaded_event.set()

    def _make_handler(self, event_type: str) -> Callable[[dict[str, Any]], Awaitable[None]]:
        async def handler(event: dict[str, Any]) -> None:
            log.debug("ha.registries.event", event_type=event_type)
            # A targeted delta would be faster but HA's payload format differs
            # per registry and changes between versions. Refetching is cheap
            # and never wrong.
            await self.refresh_for(event_type)

        return handler

    # ─── Fetch ──────────────────────────────────────────────────────────────

    async def refresh(self) -> None:
        async with self._refresh_lock:
            assert self._client is not None
            entities_raw, devices_raw, areas_raw, labels_raw = await asyncio.gather(
                self._client.send_command("config/entity_registry/list"),
                self._client.send_command("config/device_registry/list"),
                self._client.send_command("config/area_registry/list"),
                self._client.send_command("config/label_registry/list"),
                return_exceptions=False,
            )
            self._replace_entities(entities_raw)
            self._replace_devices(devices_raw)
            self._replace_areas(areas_raw)
            self._replace_labels(labels_raw)
            log.info(
                "ha.registries.loaded",
                entities=len(self.entities),
                devices=len(self.devices),
                areas=len(self.areas),
                labels=len(self.labels),
            )

    async def refresh_for(self, event_type: str) -> None:
        assert self._client is not None
        if event_type == "entity_registry_updated":
            raw = await self._client.send_command("config/entity_registry/list")
            self._replace_entities(raw)
        elif event_type == "device_registry_updated":
            raw = await self._client.send_command("config/device_registry/list")
            self._replace_devices(raw)
        elif event_type == "area_registry_updated":
            raw = await self._client.send_command("config/area_registry/list")
            self._replace_areas(raw)
        elif event_type == "label_registry_updated":
            raw = await self._client.send_command("config/label_registry/list")
            self._replace_labels(raw)

    def _replace_entities(self, raw: Any) -> None:
        items = raw if isinstance(raw, list) else []
        self.entities = {e["entity_id"]: EntityEntry.from_raw(e) for e in items}

    def _replace_devices(self, raw: Any) -> None:
        items = raw if isinstance(raw, list) else []
        self.devices = {d["id"]: DeviceEntry.from_raw(d) for d in items}

    def _replace_areas(self, raw: Any) -> None:
        items = raw if isinstance(raw, list) else []
        self.areas = {a["area_id"]: AreaEntry.from_raw(a) for a in items}

    def _replace_labels(self, raw: Any) -> None:
        items = raw if isinstance(raw, list) else []
        self.labels = {el["label_id"]: LabelEntry.from_raw(el) for el in items}

    # ─── Read helpers ───────────────────────────────────────────────────────

    def entities_by_area(self, area_id: str) -> list[EntityEntry]:
        return [e for e in self.entities.values() if e.area_id == area_id]

    def entities_by_domain(self, domain: str) -> list[EntityEntry]:
        return [e for e in self.entities.values() if e.domain == domain]

    def domain_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for e in self.entities.values():
            counts[e.domain] = counts.get(e.domain, 0) + 1
        return counts

    def unassigned_entities(self) -> list[EntityEntry]:
        """Entities not assigned to any area (directly or via their device)."""
        device_area = {d.id: d.area_id for d in self.devices.values()}
        out = []
        for e in self.entities.values():
            if e.area_id:
                continue
            if e.device_id and device_area.get(e.device_id):
                continue
            out.append(e)
        return out
