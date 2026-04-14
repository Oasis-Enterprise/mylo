"""Shape raw HA data into LLM-friendly form.

Spec §4.10: translate raw API values into human-readable form to reduce
hallucination. Brightness 255 → "100%", mireds → "warm white", etc.

Keep each formatter narrow and pure. No IO — pass in whatever data you need.
"""

from __future__ import annotations

from typing import Any

from mylo.ha.registries import AreaEntry, DeviceEntry, EntityEntry, Registries

# ─── Scalar translators ──────────────────────────────────────────────────────


def brightness_to_percent(raw: Any) -> str | None:
    """HA brightness is 0-255. Return ``"N%"`` or None if not an int."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value < 0:
        value = 0
    if value > 255:
        value = 255
    return f"{round(value * 100 / 255)}%"


def color_temp_label(mireds: Any) -> str | None:
    """Crude mireds → plain-English label. Exact ranges don't matter much;
    the LLM just needs a hint so it doesn't reason in raw mireds."""
    try:
        m = int(mireds)
    except (TypeError, ValueError):
        return None
    if m < 200:
        return "cool white"
    if m < 320:
        return "neutral white"
    return "warm white"


# ─── Per-domain key-attribute extraction ─────────────────────────────────────

_KEY_ATTRS_BY_DOMAIN: dict[str, tuple[str, ...]] = {
    "light": ("brightness", "color_temp", "rgb_color", "color_mode"),
    "climate": ("current_temperature", "temperature", "hvac_mode", "preset_mode"),
    "sensor": ("unit_of_measurement", "device_class"),
    "binary_sensor": ("device_class",),
    "switch": (),
    "media_player": ("media_title", "media_artist", "volume_level", "source"),
    "cover": ("current_position", "device_class"),
    "lock": (),
    "fan": ("percentage", "preset_mode"),
}


def _key_attributes(domain: str, attributes: dict[str, Any]) -> dict[str, Any]:
    keys = _KEY_ATTRS_BY_DOMAIN.get(domain, ())
    out: dict[str, Any] = {}
    for key in keys:
        if key not in attributes:
            continue
        raw = attributes[key]
        if key == "brightness":
            pct = brightness_to_percent(raw)
            if pct is not None:
                out["brightness"] = pct
                continue
        if key == "color_temp":
            label = color_temp_label(raw)
            if label is not None:
                out["color_temp"] = label
                continue
        if raw is None:
            continue
        out[key] = raw
    return out


# ─── Entity shaping ──────────────────────────────────────────────────────────


def shape_entity(
    entry: EntityEntry,
    state: dict[str, Any] | None,
    registries: Registries,
    *,
    include_attributes: bool,
) -> dict[str, Any]:
    """Render a single entity for LLM consumption.

    ``state`` is the dict from HA's ``get_states`` (may be ``None`` if the
    entity has no current state, e.g. newly added).
    """
    device: DeviceEntry | None = None
    if entry.device_id:
        device = registries.devices.get(entry.device_id)

    area: AreaEntry | None = None
    area_id = entry.area_id or (device.area_id if device else None)
    if area_id:
        area = registries.areas.get(area_id)

    # Prefer HA's runtime-computed friendly_name (from state.attributes) over
    # the registry's, which is often null — the registry fallback is the bare
    # entity_id, which is worse than anything HA put together at runtime.
    state_attrs = (state or {}).get("attributes") or {}
    state_friendly = state_attrs.get("friendly_name")
    friendly_name = entry.name or state_friendly or entry.original_name or entry.entity_id

    shaped: dict[str, Any] = {
        "entity_id": entry.entity_id,
        "friendly_name": friendly_name,
        "domain": entry.domain,
        "state": (state or {}).get("state"),
        "area": area.name if area else None,
    }

    if device is not None:
        parts: list[str] = []
        if device.manufacturer:
            parts.append(device.manufacturer)
        if device.model:
            parts.append(device.model)
        if parts:
            shaped["device"] = " ".join(parts)

    if entry.platform:
        shaped["integration"] = entry.platform

    if entry.disabled_by:
        shaped["disabled_by"] = entry.disabled_by

    if include_attributes:
        shaped["key_attributes"] = _key_attributes(entry.domain, state_attrs)

    return shaped


# ─── Device shaping ──────────────────────────────────────────────────────────


def shape_device(
    device: DeviceEntry,
    registries: Registries,
    *,
    include_entities: bool,
) -> dict[str, Any]:
    """Render a single device. ``include_entities`` attaches a compact list of
    ``entity_id + domain + state-less friendly name`` for each entity that
    belongs to this device.
    """
    area: AreaEntry | None = registries.areas.get(device.area_id) if device.area_id else None
    shaped: dict[str, Any] = {
        "id": device.id,
        "name": device.display_name,
        "manufacturer": device.manufacturer,
        "model": device.model,
        "area": area.name if area else None,
    }
    if device.disabled_by:
        shaped["disabled_by"] = device.disabled_by
    if include_entities:
        shaped["entities"] = [
            {
                "entity_id": e.entity_id,
                "domain": e.domain,
                "friendly_name": e.friendly_name,
            }
            for e in registries.entities.values()
            if e.device_id == device.id
        ]
    return shaped


# ─── Summary string ──────────────────────────────────────────────────────────


def summarize_entities(
    entities: list[dict[str, Any]],
    *,
    area_name: str | None = None,
) -> dict[str, Any]:
    """Produce a compact summary envelope around a list of shaped entities.

    Follows the "good" example in §4.10:
    { entities_found, area?, summary, entities }
    """
    domain_counts: dict[str, int] = {}
    on_counts: dict[str, int] = {}
    for e in entities:
        d = e.get("domain", "?")
        domain_counts[d] = domain_counts.get(d, 0) + 1
        if e.get("state") == "on":
            on_counts[d] = on_counts.get(d, 0) + 1

    pieces: list[str] = []
    for domain in sorted(domain_counts, key=lambda k: -domain_counts[k]):
        count = domain_counts[domain]
        on = on_counts.get(domain, 0)
        label = f"{count} {domain}s" if count != 1 else f"1 {domain}"
        if on:
            label += f" ({on} on)"
        pieces.append(label)
    summary = ", ".join(pieces) if pieces else "none"

    envelope: dict[str, Any] = {
        "entities_found": len(entities),
        "summary": summary,
        "entities": entities,
    }
    if area_name is not None:
        envelope["area"] = area_name
    return envelope
