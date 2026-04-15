"""Entity-reference validation across a full automation/script config.

Two entry points:

* :func:`check_entity_refs` — walk a parsed config, find every entity_id-
  shaped field and every template-extracted ref, resolve each through the
  :class:`Resolver`, report mismatches.

* :func:`extract_refs` — just find the references without resolving. Used
  in tests and for producing dry-run previews that show what the config
  touches.

The walk deliberately doesn't know about specific HA schemas — it looks
for the conventional shapes (``entity_id`` keys, ``target.entity_id`` on
service calls, template strings) and works on any tree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mylo.resolver.resolver import RefMismatch, ResolvedRef, Resolver
from mylo.validators.template_check import check_template, scan_config_for_templates


@dataclass(slots=True)
class Reference:
    """One entity reference found in a config."""

    kind: str  # "entity"
    ref: str
    path: str


@dataclass(slots=True)
class RefCheckResult:
    ok: bool
    resolved: list[ResolvedRef] = field(default_factory=list)
    mismatches: list[tuple[str, RefMismatch]] = field(default_factory=list)  # (path, mismatch)
    template_errors: list[tuple[str, str]] = field(default_factory=list)  # (path, msg)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "resolved": [
                {
                    "original": r.original,
                    "resolved": r.resolved,
                    "corrected": r.corrected,
                }
                for r in self.resolved
            ],
            "mismatches": [{"path": p, **m.to_envelope()} for p, m in self.mismatches],
            "template_errors": [{"path": p, "message": m} for p, m in self.template_errors],
        }


def extract_refs(config: Any) -> list[Reference]:
    """Find every entity reference in a config tree. Deduplicated per path."""
    out: list[Reference] = []
    _walk(config, "", out)

    # Templates inline in strings — extract entity refs from their Jinja.
    for path, template in scan_config_for_templates(config):
        result = check_template(template)
        for ref in result.entity_refs:
            out.append(Reference("entity", ref, f"{path}<template>"))
    return out


def check_entity_refs(config: Any, resolver: Resolver) -> RefCheckResult:
    """Validate every entity ref through ``resolver``. Returns a structured
    summary with the resolutions + mismatches the write path needs.
    """
    result = RefCheckResult(ok=True)

    # Template syntax check first — a broken Jinja would otherwise hide
    # real refs from the rest of the walk.
    for path, template in scan_config_for_templates(config):
        tresult = check_template(template)
        if not tresult.ok:
            result.ok = False
            for err in tresult.errors:
                result.template_errors.append((path, err))

    seen: set[tuple[str, str]] = set()
    for ref in extract_refs(config):
        key = (ref.ref, ref.path)
        if key in seen:
            continue
        seen.add(key)
        resolution = resolver.resolve_entity(ref.ref)
        if isinstance(resolution, RefMismatch):
            result.ok = False
            result.mismatches.append((ref.path, resolution))
        else:
            result.resolved.append(resolution)
    return result


# ─── Walk ───────────────────────────────────────────────────────────────────


def _walk(value: Any, path: str, out: list[Reference]) -> None:
    if isinstance(value, dict):
        # Direct entity_id fields.
        for key in ("entity_id", "entity_ids"):
            if key in value:
                _collect_entities(value[key], f"{path}.{key}" if path else key, out)
        # Service calls: target.entity_id.
        target = value.get("target")
        if isinstance(target, dict):
            for subkey, subval in target.items():
                if subkey == "entity_id":
                    _collect_entities(
                        subval,
                        f"{path}.target.entity_id" if path else "target.entity_id",
                        out,
                    )

        for k, v in value.items():
            sub_path = f"{path}.{k}" if path else str(k)
            _walk(v, sub_path, out)
        return

    if isinstance(value, list):
        for i, v in enumerate(value):
            _walk(v, f"{path}[{i}]", out)


def _collect_entities(value: Any, path: str, out: list[Reference]) -> None:
    if isinstance(value, str):
        # Strings like "light.kitchen" — accept if domain.anything shape.
        if "." in value and not value.startswith("{"):
            out.append(Reference("entity", value, path))
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _collect_entities(item, f"{path}[{i}]", out)
