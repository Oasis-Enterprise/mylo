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

"""``modify_dashboard`` — create, update, delete dashboard views and cards.

Storage-mode dashboards are edited via HA's ``lovelace/config/save``
websocket command. The full dashboard config is read, modified in
memory, then written back. For M7b this is the only path; YAML-mode
dashboards flow through ``write_config_file`` / ``patch_config_file``.

Dry-run returns a structural diff (via :mod:`mylo.files.diff`); apply
writes the updated config. No file ops or reload needed — HA's
frontend picks up Lovelace changes immediately on save.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from mylo.files.diff import diff_structs
from mylo.ha.lovelace_meta import detect_custom_cards, get_resources, get_themes
from mylo.ha.ws_client import CommandError
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.dashboard_refs import extract_entity_refs, validate_refs
from mylo.tools.registry import register
from mylo.validators.dashboard_schema import has_custom_card, validate_view

Action = Literal[
    "create",
    "add_cards",
    "add_section",
    "update_view",
    "replace_card",
    "remove_card",
    "update",
    "delete",
]


class ModifyDashboardParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Action = Field(
        description=(
            "'create' a new view (sections layout by default). "
            "'add_cards' appends cards to an existing view (pass section_index "
            "for sections-layout views). "
            "'add_section' appends one whole section to a sections-layout view "
            "— the incremental builder for new views. "
            "'update_view' replaces a single view by path (other views untouched). "
            "'replace_card' swaps one card in a view by index. "
            "'remove_card' deletes one card from a view by index. "
            "'update' replaces the FULL dashboard config (rarely needed). "
            "'delete' removes a view by view_path. "
            "Prefer update_view/replace_card over update — they're surgical."
        ),
    )
    dashboard_id: str | None = Field(
        default=None,
        description="Dashboard url_path. null = default Overview dashboard.",
    )
    config: dict[str, Any] | None = Field(
        default=None,
        description=(
            "For 'create': a view config {title, path, icon?, cards: [...]}. "
            "For 'update_view': the complete replacement view dict. "
            "For 'replace_card': the new card dict that swaps in. "
            "For 'update': the complete replacement dashboard config. "
            "For 'add_cards', 'remove_card', 'delete': not needed."
        ),
    )
    cards: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "For 'add_cards': list of card configs to append to an existing "
            "view. For 'add_section': shorthand — the cards are wrapped in a "
            "{type: grid, cards: [...]} section. For 'create': alternative to "
            "putting cards inside config — if both config.cards and this field "
            "are present, this field wins. On create these are auto-wrapped "
            "into a single grid section unless layout='masonry'. Build "
            "dashboards incrementally: create the view with its first section, "
            "then add_section per remaining section."
        ),
    )
    sections: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "For 'create'/'update_view': list of section dicts for a "
            "sections-layout view, each {type: 'grid', cards: [...]}. Start "
            "each section with a {type: heading, heading: ...} card. Shorthand "
            "— avoids nesting in config."
        ),
    )
    layout: Literal["sections", "masonry"] | None = Field(
        default=None,
        description=(
            "For 'create': view layout. Defaults to 'sections' (modern HA "
            "layout — headings stay attached to their cards). Pass 'masonry' "
            "only when the user explicitly wants the legacy flat-cards layout."
        ),
    )
    view_path: str | None = Field(
        default=None,
        description=(
            "Target view path for add_cards, update_view, replace_card, remove_card, and delete."
        ),
    )
    card_index: int | None = Field(
        default=None,
        description=(
            "For 'replace_card' and 'remove_card': zero-based index of the "
            "card to target. Use query_dashboard to see the current card list."
        ),
    )
    section_index: int | None = Field(
        default=None,
        description=(
            "For sections-layout views, the zero-based section to act on. "
            "When set, replace_card/remove_card target "
            "view.sections[section_index].cards[card_index] and add_cards "
            "appends to view.sections[section_index].cards. Omit on classic "
            "layouts. Sections-layout views are flagged by 'type: sections' "
            "in query_dashboard output."
        ),
    )
    title: str | None = Field(
        default=None,
        description="For 'create': view title. Shorthand — avoids nesting in config.",
    )
    path: str | None = Field(
        default=None,
        description="For 'create': view URL path slug. Shorthand — avoids nesting in config.",
    )
    icon: str | None = Field(
        default=None,
        description="For 'create': MDI icon. Shorthand — avoids nesting in config.",
    )
    theme: str | None = Field(
        default=None,
        description="For 'create': HA theme name to apply to the view.",
    )
    dry_run: bool = Field(default=True)


async def _fetch_dashboard(ctx: ToolContext, dashboard_id: str | None) -> dict[str, Any] | None:
    """Read the current dashboard config from HA."""
    try:
        result = await ctx.ws_client.send_command("lovelace/config", url_path=dashboard_id)
        return result if isinstance(result, dict) else None
    except CommandError:
        return None


async def _save_dashboard(
    ctx: ToolContext, dashboard_id: str | None, config: dict[str, Any]
) -> None:
    """Write the full dashboard config back to HA."""
    await ctx.ws_client.send_command(
        "lovelace/config/save", write=True, url_path=dashboard_id, config=config
    )


async def _check_view_schema(
    ctx: ToolContext, view: dict[str, Any]
) -> ToolResult | list[dict[str, Any]]:
    """Structure-check a view (or pseudo-view wrapping loose cards).

    Returns a blocking ToolResult on errors, otherwise the list of
    warning dicts to ride along in the preview. The lovelace-resources
    lookup only happens when a custom:* card is actually present.
    """
    installed: set[str] | None = None
    if has_custom_card(view):
        resources = await get_resources(ctx.ws_client)
        if resources is not None:
            installed = detect_custom_cards(resources)
    report = validate_view(view, installed_custom=installed)
    if not report.ok:
        return ToolResult.error(
            "dashboard_schema_issues",
            "the card configs have structural problems — fix every listed issue and retry",
            data=report.to_dict(),
        )
    return [i.to_dict() for i in report.issues]


async def _check_theme(ctx: ToolContext, theme: str) -> ToolResult | None:
    """Reject a theme HA doesn't have. Skipped when themes are unlistable."""
    themes = await get_themes(ctx.ws_client)
    if themes is None or theme in themes.names:
        return None
    return ToolResult.error(
        "invalid_theme",
        f"theme {theme!r} is not installed in HA — pick one of the "
        "available themes (or omit theme to use the default)",
        data={"available_themes": themes.names},
    )


async def handler(params: ModifyDashboardParams, ctx: ToolContext) -> ToolResult:
    if params.theme:
        theme_error = await _check_theme(ctx, params.theme)
        if theme_error is not None:
            return theme_error
    if params.action == "create":
        return await _create_view(ctx, params)
    if params.action == "add_cards":
        return await _add_cards(ctx, params)
    if params.action == "add_section":
        return await _add_section(ctx, params)
    if params.action == "update_view":
        return await _update_view(ctx, params)
    if params.action == "replace_card":
        return await _replace_card(ctx, params)
    if params.action == "remove_card":
        return await _remove_card(ctx, params)
    if params.action == "update":
        return await _update_dashboard(ctx, params)
    if params.action == "delete":
        return await _delete_view(ctx, params)
    return ToolResult.error("invalid_action", f"unknown action {params.action!r}")


def _is_sections_view(view: dict[str, Any]) -> bool:
    return view.get("type") == "sections" or isinstance(view.get("sections"), list)


def _count_cards(view: dict[str, Any]) -> int:
    if _is_sections_view(view):
        return sum(
            len(s.get("cards") or []) for s in view.get("sections") or [] if isinstance(s, dict)
        )
    return len(view.get("cards") or [])


def _view_entity_refs(view: dict[str, Any]) -> set[str]:
    """Every entity ref in a view, whichever layout it uses.

    Walks cards AND sections — validating only ``view.cards`` misses
    every ref in a sections-layout view.
    """
    return extract_entity_refs([view.get("cards") or [], view.get("sections") or []])


def _assemble_view(
    params: ModifyDashboardParams, *, for_create: bool = False
) -> dict[str, Any] | None:
    """Build a view dict from either config or the shorthand fields.

    The shorthand (title/path/icon/theme/cards/sections as top-level
    params) is much easier for the model to produce than nesting
    everything inside a config dict — especially for large card lists
    that push the model's output limit.

    New views default to the sections layout: a flat card list on
    create is auto-wrapped into a single grid section unless
    layout='masonry' is passed explicitly. Sections-shaped input never
    gets a ``cards`` key injected — a view carrying both confuses HA
    and the surgical card ops.
    """
    if params.config:
        view = dict(params.config)
    else:
        if not params.title and not params.path:
            return None
        view = {}

    if params.title:
        view["title"] = params.title
    if params.path:
        view["path"] = params.path
    if params.icon:
        view["icon"] = params.icon
    if params.theme:
        view["theme"] = params.theme

    if params.sections is not None:
        view["sections"] = params.sections

    if _is_sections_view(view):
        view["type"] = "sections"
        view.setdefault("sections", [])
        if not view.get("cards"):
            view.pop("cards", None)
        return view

    # cards field wins over config.cards so the model can build
    # incrementally without re-passing the view wrapper each time.
    cards = params.cards if params.cards is not None else view.get("cards") or []

    if for_create and params.layout != "masonry":
        view["type"] = "sections"
        view.setdefault("max_columns", 4)
        view["sections"] = [{"type": "grid", "cards": cards}]
        view.pop("cards", None)
        return view

    view["cards"] = cards
    return view


async def _create_view(ctx: ToolContext, params: ModifyDashboardParams) -> ToolResult:
    new_view = _assemble_view(params, for_create=True)
    if new_view is None:
        return ToolResult.error(
            "missing_param",
            "create requires either 'config' or 'title'+'path' shorthand",
        )

    current = await _fetch_dashboard(ctx, params.dashboard_id)
    if current is None:
        current = {"views": []}

    views = list(current.get("views") or [])

    new_path = new_view.get("path")
    if new_path and any(v.get("path") == new_path for v in views if isinstance(v, dict)):
        return ToolResult.error(
            "already_exists",
            f"view with path {new_path!r} already exists in this dashboard",
        )

    # Pre-flight: validate every entity reference in the card configs
    # against the live registry. If any are invalid, return the full
    # list with did_you_mean suggestions so the model self-corrects
    # before the user ever sees a broken preview.
    entity_refs = _view_entity_refs(new_view)
    invalid = validate_refs(entity_refs, ctx.registries)
    if invalid:
        return ToolResult.error(
            "invalid_entity_refs",
            f"{len(invalid)} entity reference(s) in the cards don't exist in HA. "
            "Fix them and retry. Use the EXACT entity_ids from query_entities or "
            "query_dashboard — do NOT normalize or clean up entity names.",
            data={"invalid_refs": invalid, "total_refs_checked": len(entity_refs)},
        )

    schema_warnings = await _check_view_schema(ctx, new_view)
    if isinstance(schema_warnings, ToolResult):
        return schema_warnings

    views.append(new_view)
    new_config = {**current, "views": views}

    diff = diff_structs(current, new_config)
    layout = "sections" if _is_sections_view(new_view) else "masonry"
    preview: dict[str, Any] = {
        "dashboard_id": params.dashboard_id,
        "action": "create",
        "view_path": new_path,
        "view_title": new_view.get("title"),
        "layout": layout,
        "card_count": _count_cards(new_view),
        "entity_refs_validated": len(entity_refs),
        "diff": diff.to_dict(),
    }
    if layout == "sections":
        preview["section_count"] = len(new_view.get("sections") or [])
    if schema_warnings:
        preview["schema_warnings"] = schema_warnings

    if params.dry_run:
        preview["preview"] = True
        return ToolResult.ok(preview)

    try:
        await _save_dashboard(ctx, params.dashboard_id, new_config)
    except CommandError as exc:
        return ToolResult.error("ha_error", f"{exc.code}: {exc.message}")

    return ToolResult.ok({**preview, "preview": False})


async def _add_cards(ctx: ToolContext, params: ModifyDashboardParams) -> ToolResult:
    """Append cards to an existing view — the incremental builder.

    The model creates a view with a few cards first, then calls
    add_cards in follow-up tool calls to fill in sections. This
    keeps each tool_use block small enough that the model doesn't
    hit its output limit.
    """
    if not params.view_path:
        return ToolResult.error("missing_param", "add_cards requires 'view_path'")
    if not params.cards:
        return ToolResult.error(
            "missing_param", "add_cards requires 'cards' (list of card configs)"
        )

    current = await _fetch_dashboard(ctx, params.dashboard_id)
    if current is None:
        return ToolResult.error("dashboard_not_found", "dashboard not found")

    views = list(current.get("views") or [])
    target_idx: int | None = None
    for i, v in enumerate(views):
        if isinstance(v, dict) and v.get("path") == params.view_path:
            target_idx = i
            break

    if target_idx is None:
        return ToolResult.error(
            "view_not_found",
            f"no view with path {params.view_path!r}",
            data={"available": [v.get("path") for v in views if isinstance(v, dict)]},
        )

    # Validate entity refs in the new cards before merging.
    entity_refs = extract_entity_refs(params.cards)
    invalid = validate_refs(entity_refs, ctx.registries)
    if invalid:
        return ToolResult.error(
            "invalid_entity_refs",
            f"{len(invalid)} entity reference(s) in the cards don't exist in HA. "
            "Fix them and retry. Use the EXACT entity_ids from query_entities or "
            "query_dashboard — do NOT normalize or clean up entity names.",
            data={"invalid_refs": invalid, "total_refs_checked": len(entity_refs)},
        )

    schema_warnings = await _check_view_schema(ctx, {"cards": params.cards})
    if isinstance(schema_warnings, ToolResult):
        return schema_warnings

    target_view = dict(views[target_idx])
    resolved = _resolve_card_target(target_view, params.section_index)
    if isinstance(resolved, ToolResult):
        return resolved
    existing_cards, commit = resolved
    existing_cards.extend(params.cards)
    views[target_idx] = commit(existing_cards)

    new_config = {**current, "views": views}
    diff = diff_structs(current, new_config)
    preview: dict[str, Any] = {
        "dashboard_id": params.dashboard_id,
        "action": "add_cards",
        "view_path": params.view_path,
        "section_index": params.section_index,
        "cards_added": len(params.cards),
        "total_cards": len(existing_cards),
        "entity_refs_validated": len(entity_refs),
        "diff": diff.to_dict(),
    }
    if schema_warnings:
        preview["schema_warnings"] = schema_warnings

    if params.dry_run:
        preview["preview"] = True
        return ToolResult.ok(preview)

    try:
        await _save_dashboard(ctx, params.dashboard_id, new_config)
    except CommandError as exc:
        return ToolResult.error("ha_error", f"{exc.code}: {exc.message}")

    return ToolResult.ok({**preview, "preview": False})


async def _add_section(ctx: ToolContext, params: ModifyDashboardParams) -> ToolResult:
    """Append one section to a sections-layout view — the incremental
    builder for new views (the sections analog of add_cards).
    """
    if not params.view_path:
        return ToolResult.error("missing_param", "add_section requires 'view_path'")

    section = params.config
    if section is None and params.cards:
        section = {"type": "grid", "cards": params.cards}
    if section is None:
        return ToolResult.error(
            "missing_param",
            "add_section requires 'config' (a section dict) or 'cards' "
            "(wrapped into a grid section)",
        )

    current = await _fetch_dashboard(ctx, params.dashboard_id)
    if current is None:
        return ToolResult.error("dashboard_not_found", "dashboard not found")

    views = list(current.get("views") or [])
    target_idx: int | None = None
    for i, v in enumerate(views):
        if isinstance(v, dict) and v.get("path") == params.view_path:
            target_idx = i
            break

    if target_idx is None:
        return ToolResult.error(
            "view_not_found",
            f"no view with path {params.view_path!r}",
            data={"available": [v.get("path") for v in views if isinstance(v, dict)]},
        )

    target_view = dict(views[target_idx])
    if not _is_sections_view(target_view):
        return ToolResult.error(
            "not_sections_layout",
            f"view {params.view_path!r} is not a sections-layout view — "
            "use add_cards for masonry views",
        )

    entity_refs = extract_entity_refs([section])
    invalid = validate_refs(entity_refs, ctx.registries)
    if invalid:
        return ToolResult.error(
            "invalid_entity_refs",
            f"{len(invalid)} entity reference(s) in the section don't exist in HA. "
            "Fix them and retry. Use the EXACT entity_ids from query_entities or "
            "query_dashboard — do NOT normalize or clean up entity names.",
            data={"invalid_refs": invalid, "total_refs_checked": len(entity_refs)},
        )

    schema_warnings = await _check_view_schema(ctx, {"type": "sections", "sections": [section]})
    if isinstance(schema_warnings, ToolResult):
        return schema_warnings

    sections = list(target_view.get("sections") or [])
    sections.append(section)
    target_view["sections"] = sections
    views[target_idx] = target_view

    new_config = {**current, "views": views}
    diff = diff_structs(current, new_config)
    preview: dict[str, Any] = {
        "dashboard_id": params.dashboard_id,
        "action": "add_section",
        "view_path": params.view_path,
        "section_count": len(sections),
        "cards_added": len(section.get("cards") or []),
        "entity_refs_validated": len(entity_refs),
        "diff": diff.to_dict(),
    }
    if schema_warnings:
        preview["schema_warnings"] = schema_warnings

    if params.dry_run:
        preview["preview"] = True
        return ToolResult.ok(preview)

    try:
        await _save_dashboard(ctx, params.dashboard_id, new_config)
    except CommandError as exc:
        return ToolResult.error("ha_error", f"{exc.code}: {exc.message}")

    return ToolResult.ok({**preview, "preview": False})


async def _update_view(ctx: ToolContext, params: ModifyDashboardParams) -> ToolResult:
    """Replace a single view by path — all other views stay untouched."""
    if not params.view_path:
        return ToolResult.error("missing_param", "update_view requires 'view_path'")

    new_view = _assemble_view(params)
    if new_view is None and not params.config:
        return ToolResult.error(
            "missing_param",
            "update_view requires 'config' (the replacement view) or shorthand fields",
        )
    if new_view is None:
        new_view = params.config or {}

    current = await _fetch_dashboard(ctx, params.dashboard_id)
    if current is None:
        return ToolResult.error("dashboard_not_found", "dashboard not found")

    views = list(current.get("views") or [])
    target_idx: int | None = None
    for i, v in enumerate(views):
        if isinstance(v, dict) and v.get("path") == params.view_path:
            target_idx = i
            break

    if target_idx is None:
        return ToolResult.error(
            "view_not_found",
            f"no view with path {params.view_path!r}",
            data={"available": [v.get("path") for v in views if isinstance(v, dict)]},
        )

    # Preserve the path from the original if the replacement doesn't set one.
    if "path" not in new_view:
        new_view["path"] = params.view_path

    entity_refs = _view_entity_refs(new_view)
    invalid = validate_refs(entity_refs, ctx.registries)
    if invalid:
        return ToolResult.error(
            "invalid_entity_refs",
            f"{len(invalid)} entity reference(s) in the cards don't exist in HA. "
            "Fix them and retry.",
            data={"invalid_refs": invalid, "total_refs_checked": len(entity_refs)},
        )

    schema_warnings = await _check_view_schema(ctx, new_view)
    if isinstance(schema_warnings, ToolResult):
        return schema_warnings

    views[target_idx] = new_view
    new_config = {**current, "views": views}

    diff = diff_structs(current, new_config)
    layout = "sections" if _is_sections_view(new_view) else "masonry"
    preview: dict[str, Any] = {
        "dashboard_id": params.dashboard_id,
        "action": "update_view",
        "view_path": params.view_path,
        "layout": layout,
        "card_count": _count_cards(new_view),
        "entity_refs_validated": len(entity_refs),
        "diff": diff.to_dict(),
    }
    if layout == "sections":
        preview["section_count"] = len(new_view.get("sections") or [])
    if schema_warnings:
        preview["schema_warnings"] = schema_warnings

    if params.dry_run:
        preview["preview"] = True
        return ToolResult.ok(preview)

    try:
        await _save_dashboard(ctx, params.dashboard_id, new_config)
    except CommandError as exc:
        return ToolResult.error("ha_error", f"{exc.code}: {exc.message}")

    return ToolResult.ok({**preview, "preview": False})


def _resolve_card_target(
    target_view: dict[str, Any], section_index: int | None
) -> tuple[list[Any], Callable[[list[Any]], dict[str, Any]]] | ToolResult:
    """Pick the card list to mutate (top-level or nested under a section).

    Returns ``(cards, commit_fn)`` where ``commit_fn(new_cards)`` returns a
    fully-updated copy of ``target_view``. On a layout mismatch returns a
    ToolResult error the caller can pass through.
    """
    has_sections = target_view.get("type") == "sections" or isinstance(
        target_view.get("sections"), list
    )

    if section_index is not None:
        if not has_sections:
            return ToolResult.error(
                "section_index_invalid",
                "section_index was provided but the view isn't a sections layout",
            )
        sections = list(target_view.get("sections") or [])
        if section_index < 0 or section_index >= len(sections):
            return ToolResult.error(
                "section_not_found",
                f"section_index {section_index} out of range (view has {len(sections)} sections)",
            )
        target_section = (
            dict(sections[section_index]) if isinstance(sections[section_index], dict) else {}
        )
        cards = list(target_section.get("cards") or [])

        def _commit_sectioned(new_cards: list[Any]) -> dict[str, Any]:
            target_section["cards"] = new_cards
            sections[section_index] = target_section
            updated = dict(target_view)
            updated["sections"] = sections
            return updated

        return cards, _commit_sectioned

    # Classic top-level layout — but if the view is sections-only the
    # model needs to know to pass section_index instead.
    if has_sections and not target_view.get("cards"):
        return ToolResult.error(
            "section_index_required",
            "this view uses sections layout — pass section_index to target a "
            "card inside a section. See the view structure from query_dashboard.",
        )
    cards = list(target_view.get("cards") or [])

    def _commit_classic(new_cards: list[Any]) -> dict[str, Any]:
        updated = dict(target_view)
        updated["cards"] = new_cards
        return updated

    return cards, _commit_classic


async def _replace_card(ctx: ToolContext, params: ModifyDashboardParams) -> ToolResult:
    """Swap one card in a view by index — everything else untouched."""
    if not params.view_path:
        return ToolResult.error("missing_param", "replace_card requires 'view_path'")
    if params.card_index is None:
        return ToolResult.error("missing_param", "replace_card requires 'card_index'")
    if not params.config:
        return ToolResult.error(
            "missing_param", "replace_card requires 'config' (the new card config)"
        )

    current = await _fetch_dashboard(ctx, params.dashboard_id)
    if current is None:
        return ToolResult.error("dashboard_not_found", "dashboard not found")

    views = list(current.get("views") or [])
    target_idx: int | None = None
    for i, v in enumerate(views):
        if isinstance(v, dict) and v.get("path") == params.view_path:
            target_idx = i
            break

    if target_idx is None:
        return ToolResult.error(
            "view_not_found",
            f"no view with path {params.view_path!r}",
            data={"available": [v.get("path") for v in views if isinstance(v, dict)]},
        )

    target_view = dict(views[target_idx])
    resolved = _resolve_card_target(target_view, params.section_index)
    if isinstance(resolved, ToolResult):
        return resolved
    cards, commit = resolved

    if params.card_index < 0 or params.card_index >= len(cards):
        scope = f"section {params.section_index}" if params.section_index is not None else "view"
        return ToolResult.error(
            "card_not_found",
            f"card_index {params.card_index} out of range ({scope} has {len(cards)} cards)",
        )

    entity_refs = extract_entity_refs([params.config])
    invalid = validate_refs(entity_refs, ctx.registries)
    if invalid:
        return ToolResult.error(
            "invalid_entity_refs",
            f"{len(invalid)} entity reference(s) in the card don't exist in HA. "
            "Fix them and retry.",
            data={"invalid_refs": invalid, "total_refs_checked": len(entity_refs)},
        )

    schema_warnings = await _check_view_schema(ctx, {"cards": [params.config]})
    if isinstance(schema_warnings, ToolResult):
        return schema_warnings

    old_card_type = (
        cards[params.card_index].get("type", "unknown")
        if isinstance(cards[params.card_index], dict)
        else "unknown"
    )
    cards[params.card_index] = params.config
    views[target_idx] = commit(cards)

    new_config = {**current, "views": views}
    diff = diff_structs(current, new_config)
    preview: dict[str, Any] = {
        "dashboard_id": params.dashboard_id,
        "action": "replace_card",
        "view_path": params.view_path,
        "section_index": params.section_index,
        "card_index": params.card_index,
        "old_card_type": old_card_type,
        "new_card_type": params.config.get("type", "unknown"),
        "entity_refs_validated": len(entity_refs),
        "diff": diff.to_dict(),
    }
    if schema_warnings:
        preview["schema_warnings"] = schema_warnings

    if params.dry_run:
        preview["preview"] = True
        return ToolResult.ok(preview)

    try:
        await _save_dashboard(ctx, params.dashboard_id, new_config)
    except CommandError as exc:
        return ToolResult.error("ha_error", f"{exc.code}: {exc.message}")

    return ToolResult.ok({**preview, "preview": False})


async def _remove_card(ctx: ToolContext, params: ModifyDashboardParams) -> ToolResult:
    """Delete one card from a view by index — everything else untouched."""
    if not params.view_path:
        return ToolResult.error("missing_param", "remove_card requires 'view_path'")
    if params.card_index is None:
        return ToolResult.error("missing_param", "remove_card requires 'card_index'")

    current = await _fetch_dashboard(ctx, params.dashboard_id)
    if current is None:
        return ToolResult.error("dashboard_not_found", "dashboard not found")

    views = list(current.get("views") or [])
    target_idx: int | None = None
    for i, v in enumerate(views):
        if isinstance(v, dict) and v.get("path") == params.view_path:
            target_idx = i
            break

    if target_idx is None:
        return ToolResult.error(
            "view_not_found",
            f"no view with path {params.view_path!r}",
            data={"available": [v.get("path") for v in views if isinstance(v, dict)]},
        )

    target_view = dict(views[target_idx])
    resolved = _resolve_card_target(target_view, params.section_index)
    if isinstance(resolved, ToolResult):
        return resolved
    cards, commit = resolved

    if params.card_index < 0 or params.card_index >= len(cards):
        scope = f"section {params.section_index}" if params.section_index is not None else "view"
        return ToolResult.error(
            "card_not_found",
            f"card_index {params.card_index} out of range ({scope} has {len(cards)} cards)",
        )

    removed_card = cards.pop(params.card_index)
    removed_type = (
        removed_card.get("type", "unknown") if isinstance(removed_card, dict) else "unknown"
    )
    views[target_idx] = commit(cards)

    new_config = {**current, "views": views}
    diff = diff_structs(current, new_config)
    preview: dict[str, Any] = {
        "dashboard_id": params.dashboard_id,
        "action": "remove_card",
        "view_path": params.view_path,
        "section_index": params.section_index,
        "card_index": params.card_index,
        "removed_card_type": removed_type,
        "remaining_cards": len(cards),
        "diff": diff.to_dict(),
    }

    if params.dry_run:
        preview["preview"] = True
        return ToolResult.ok(preview)

    try:
        await _save_dashboard(ctx, params.dashboard_id, new_config)
    except CommandError as exc:
        return ToolResult.error("ha_error", f"{exc.code}: {exc.message}")

    return ToolResult.ok({**preview, "preview": False})


async def _update_dashboard(ctx: ToolContext, params: ModifyDashboardParams) -> ToolResult:
    if not params.config:
        return ToolResult.error(
            "missing_param",
            "update requires 'config' (the full replacement dashboard config)",
        )

    current = await _fetch_dashboard(ctx, params.dashboard_id)
    new_config = params.config

    diff = diff_structs(current, new_config)
    preview: dict[str, Any] = {
        "dashboard_id": params.dashboard_id,
        "action": "update",
        "diff": diff.to_dict(),
    }

    if params.dry_run:
        preview["preview"] = True
        return ToolResult.ok(preview)

    try:
        await _save_dashboard(ctx, params.dashboard_id, new_config)
    except CommandError as exc:
        return ToolResult.error("ha_error", f"{exc.code}: {exc.message}")

    return ToolResult.ok({**preview, "preview": False})


async def _delete_view(ctx: ToolContext, params: ModifyDashboardParams) -> ToolResult:
    if not params.view_path:
        return ToolResult.error("missing_param", "delete requires 'view_path'")

    current = await _fetch_dashboard(ctx, params.dashboard_id)
    if current is None:
        return ToolResult.error("dashboard_not_found", "dashboard not found")

    views = list(current.get("views") or [])
    new_views = [
        v for v in views if not (isinstance(v, dict) and v.get("path") == params.view_path)
    ]

    if len(new_views) == len(views):
        return ToolResult.error(
            "view_not_found",
            f"no view with path {params.view_path!r}",
            data={"available": [v.get("path") for v in views if isinstance(v, dict)]},
        )

    new_config = {**current, "views": new_views}
    diff = diff_structs(current, new_config)
    preview: dict[str, Any] = {
        "dashboard_id": params.dashboard_id,
        "action": "delete",
        "view_path": params.view_path,
        "diff": diff.to_dict(),
    }

    if params.dry_run:
        preview["preview"] = True
        return ToolResult.ok(preview)

    try:
        await _save_dashboard(ctx, params.dashboard_id, new_config)
    except CommandError as exc:
        return ToolResult.error("ha_error", f"{exc.code}: {exc.message}")

    return ToolResult.ok({**preview, "preview": False})


TOOL = ToolDefinition(
    name="modify_dashboard",
    description=(
        "Create, update, or delete Lovelace dashboard views and cards. "
        "Storage-mode dashboards only. New views default to the modern "
        "sections layout: 'create' auto-wraps a flat card list into one "
        "grid section (so later add_cards needs section_index=0), or pass "
        "'sections' directly — one grid section per group, each led by a "
        "{type: heading} card. Grow a view with 'add_section' (one section "
        "per call). Surgical operations: 'update_view' replaces a single "
        "view by path (others untouched), 'replace_card' swaps one card by "
        "index, 'remove_card' deletes one card by index. Prefer these over "
        "'update' which replaces the entire dashboard. For sections-layout "
        "views (query_dashboard shows 'layout: sections'), pass "
        "section_index alongside card_index to target a nested card. Use "
        "the shorthand fields (title, path, icon, cards, sections) instead "
        "of nesting in config. ALWAYS dry_run=true first. Changes are "
        "immediate."
    ),
    params_model=ModifyDashboardParams,
    tier=Tier.MODIFY,
    handler=handler,
)
register(TOOL)
