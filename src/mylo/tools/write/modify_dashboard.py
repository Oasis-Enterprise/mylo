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

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from mylo.files.diff import diff_structs
from mylo.ha.ws_client import CommandError
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.dashboard_refs import extract_entity_refs, validate_refs
from mylo.tools.registry import register

Action = Literal[
    "create", "add_cards", "update_view", "replace_card", "remove_card", "update", "delete"
]


class ModifyDashboardParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Action = Field(
        description=(
            "'create' a new view. "
            "'add_cards' appends cards to an existing view. "
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
            "For 'update': the complete replacement dashboard config. "
            "For 'add_cards': not needed (use cards instead). "
            "For 'delete': not needed."
        ),
    )
    cards: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "For 'add_cards': list of card configs to append to an existing "
            "view. For 'create': alternative to putting cards inside config — "
            "if both config.cards and this field are present, this field wins. "
            "Build dashboards incrementally: create the view first with a few "
            "cards, then add_cards in follow-up calls."
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
    await ctx.ws_client.send_command("lovelace/config/save", url_path=dashboard_id, config=config)


async def handler(params: ModifyDashboardParams, ctx: ToolContext) -> ToolResult:
    if params.action == "create":
        return await _create_view(ctx, params)
    if params.action == "add_cards":
        return await _add_cards(ctx, params)
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


def _assemble_view(params: ModifyDashboardParams) -> dict[str, Any] | None:
    """Build a view dict from either config or the shorthand fields.

    The shorthand (title/path/icon/theme/cards as top-level params)
    is much easier for the model to produce than nesting everything
    inside a config dict — especially for large card lists that push
    the model's output limit.
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

    # cards field wins over config.cards so the model can build
    # incrementally without re-passing the view wrapper each time.
    if params.cards is not None:
        view["cards"] = params.cards
    if "cards" not in view:
        view["cards"] = []

    return view


async def _create_view(ctx: ToolContext, params: ModifyDashboardParams) -> ToolResult:
    new_view = _assemble_view(params)
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
    entity_refs = extract_entity_refs(new_view.get("cards") or [])
    invalid = validate_refs(entity_refs, ctx.registries)
    if invalid:
        return ToolResult.error(
            "invalid_entity_refs",
            f"{len(invalid)} entity reference(s) in the cards don't exist in HA. "
            "Fix them and retry. Use the EXACT entity_ids from query_entities or "
            "query_dashboard — do NOT normalize or clean up entity names.",
            data={"invalid_refs": invalid, "total_refs_checked": len(entity_refs)},
        )

    views.append(new_view)
    new_config = {**current, "views": views}

    diff = diff_structs(current, new_config)
    preview: dict[str, Any] = {
        "dashboard_id": params.dashboard_id,
        "action": "create",
        "view_path": new_path,
        "view_title": new_view.get("title"),
        "card_count": len(new_view.get("cards") or []),
        "entity_refs_validated": len(entity_refs),
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

    target_view = dict(views[target_idx])
    existing_cards = list(target_view.get("cards") or [])
    existing_cards.extend(params.cards)
    target_view["cards"] = existing_cards
    views[target_idx] = target_view

    new_config = {**current, "views": views}
    diff = diff_structs(current, new_config)
    preview: dict[str, Any] = {
        "dashboard_id": params.dashboard_id,
        "action": "add_cards",
        "view_path": params.view_path,
        "cards_added": len(params.cards),
        "total_cards": len(existing_cards),
        "entity_refs_validated": len(entity_refs),
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

    entity_refs = extract_entity_refs(new_view.get("cards") or [])
    invalid = validate_refs(entity_refs, ctx.registries)
    if invalid:
        return ToolResult.error(
            "invalid_entity_refs",
            f"{len(invalid)} entity reference(s) in the cards don't exist in HA. "
            "Fix them and retry.",
            data={"invalid_refs": invalid, "total_refs_checked": len(entity_refs)},
        )

    views[target_idx] = new_view
    new_config = {**current, "views": views}

    diff = diff_structs(current, new_config)
    preview: dict[str, Any] = {
        "dashboard_id": params.dashboard_id,
        "action": "update_view",
        "view_path": params.view_path,
        "card_count": len(new_view.get("cards") or []),
        "entity_refs_validated": len(entity_refs),
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
    cards = list(target_view.get("cards") or [])

    if params.card_index < 0 or params.card_index >= len(cards):
        return ToolResult.error(
            "card_not_found",
            f"card_index {params.card_index} out of range (view has {len(cards)} cards)",
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

    old_card_type = (
        cards[params.card_index].get("type", "unknown")
        if isinstance(cards[params.card_index], dict)
        else "unknown"
    )
    cards[params.card_index] = params.config
    target_view["cards"] = cards
    views[target_idx] = target_view

    new_config = {**current, "views": views}
    diff = diff_structs(current, new_config)
    preview: dict[str, Any] = {
        "dashboard_id": params.dashboard_id,
        "action": "replace_card",
        "view_path": params.view_path,
        "card_index": params.card_index,
        "old_card_type": old_card_type,
        "new_card_type": params.config.get("type", "unknown"),
        "entity_refs_validated": len(entity_refs),
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
    cards = list(target_view.get("cards") or [])

    if params.card_index < 0 or params.card_index >= len(cards):
        return ToolResult.error(
            "card_not_found",
            f"card_index {params.card_index} out of range (view has {len(cards)} cards)",
        )

    removed_card = cards.pop(params.card_index)
    removed_type = (
        removed_card.get("type", "unknown") if isinstance(removed_card, dict) else "unknown"
    )
    target_view["cards"] = cards
    views[target_idx] = target_view

    new_config = {**current, "views": views}
    diff = diff_structs(current, new_config)
    preview: dict[str, Any] = {
        "dashboard_id": params.dashboard_id,
        "action": "remove_card",
        "view_path": params.view_path,
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
        "Storage-mode dashboards only. Surgical operations available: "
        "'update_view' replaces a single view by path (others untouched), "
        "'replace_card' swaps one card by index, 'remove_card' deletes "
        "one card by index. Prefer these over 'update' which replaces "
        "the entire dashboard. For new views, build incrementally: "
        "'create' with first batch of cards, then 'add_cards'. Use the "
        "shorthand fields (title, path, icon, cards) instead of nesting "
        "in config. ALWAYS dry_run=true first. Changes are immediate."
    ),
    params_model=ModifyDashboardParams,
    tier=Tier.MODIFY,
    handler=handler,
)
register(TOOL)
