# Custom Card Authoring — Design

**Status:** Approved design, ready for implementation planning
**Date:** 2026-09-21
**Part of:** the Dashboard Quality pillar, following the plan flow shipped in
v1.5.0 (`docs/superpowers/specs/2026-09-19-dashboard-plan-flow-design.md`).

---

## 1. Context

Mylo can build dashboards from native and installed HACS cards, but it cannot
create a card that does not exist. Users hit this when they want a bespoke
compact row, a status tile with logic, or a layout no installed card offers.
Today the blockers are deliberate:

- `src/mylo/safety/file_access.py` restricts writes to `.yaml`/`.yml`, so a
  `.js` module cannot be saved under `/config/www/`.
- No tool registers a Lovelace resource. The frontend ignores a module until
  it is listed in `lovelace/resources`.
- `plan_dashboard` accepts `custom:*` types only when a registered resource
  provides them (`src/mylo/dashboard/card_schema.py`,
  `src/mylo/ha/lovelace_meta.py:detect_custom_cards`).

Home Assistant serves `/config/www/<path>` at `/local/<path>` and exposes the
resource registry over the websocket: `lovelace/resources` (list),
`lovelace/resources/create` (`res_type` in `js|css|module|html`, `url`),
`lovelace/resources/update` (`resource_id`, optional `res_type`/`url`),
`lovelace/resources/delete` (`resource_id`). Create and update return the
item; entries carry `id`, `url`, `type`.

## 2. Goals

1. Mylo can write a new custom card, save it in its own folder, register it,
   and use it on a dashboard, with one Apply covering the card and the plan.
2. The user sees what the card is and can read the source before approving;
   on an update they see a line diff.
3. Mylo never touches cards it did not create, and the general file-write
   policy stays as strict as it is today.
4. Model-written JavaScript is checked against a small contract before it is
   staged, so a card that cannot load is rejected with a fix list.

Non-goals (§5): removing a card, a real JavaScript syntax check (the runtime
image has no Node), iterating from screenshots, cards outside the `mylo-`
namespace, editing HACS cards.

## 3. Decisions taken with the user

| Question | Decision |
|---|---|
| What to show before Apply | Summary plus a "Show source" toggle; line diff on update |
| Where cards live, how named | Only `/config/www/mylo-cards/<element>.js`; element names `mylo-*` |
| Approval when a card and a plan are staged together | One Apply covers both |

## 4. Architecture

```
model ─stage_custom_card(element, source)─▶ static checks ─▶ CardStore ─▶ {preview, card_id}
model ─plan_dashboard(... custom:mylo-x ...)─▶ validate (staged elements count as installed)
                                                       │
                                        UI: plan card + "Custom cards" block [Show source]
                                                       │ Apply → approved_plan_ids = [card_id, plan_id]
model ─apply_custom_card(card_id)─▶ backup ─▶ write www/mylo-cards/mylo-x.js ─▶ resource create/update
model ─apply_dashboard_plan(plan_id)─▶ (unchanged)
```

New module `src/mylo/dashboard/cards.py` holds the contract checks, the
`CardStore`, and the resource helpers. Two tools:
`src/mylo/tools/read/stage_custom_card.py` and
`src/mylo/tools/write/apply_custom_card.py`. `src/mylo/safety/file_access.py`
gains one narrowly scoped resolver.

### 4.1 Card contract

A card file must satisfy all of the following. Each check has a stable code;
violations are returned together as `card_invalid` issues.

| Code | Rule |
|---|---|
| `element_name` | `element` matches `^mylo-[a-z0-9]+(-[a-z0-9]+)*$`, length ≤ 48 |
| `source_size` | source ≤ 65,536 bytes, valid UTF-8, no NUL bytes |
| `defines_element` | contains `customElements.define("<element>"` (single or double quotes, optional whitespace) exactly once |
| `guards_define` | the define is preceded by `customElements.get("<element>")` (so a hot-reload does not throw) |
| `extends_htmlelement` | contains `class <Name> extends HTMLElement` |
| `has_set_config` | contains `setConfig(` inside the class |
| `has_hass_setter` | contains `set hass(` inside the class |
| `registers_picker` | contains `window.customCards` and `type: "<element>"` (so the card picker lists it); a warning, not an error |
| `no_imports` | no line starts with `import ` or `export `, and no dynamic `import(` anywhere |
| `no_dynamic_code` | no `eval(`, `Function(`, `document.write(`, `createElement('script...`, `new Worker(`/`new SharedWorker(`, or `setTimeout(`/`setInterval(` given a string as the first argument |
| `no_network` | no `fetch(`, `XMLHttpRequest`, `new WebSocket(`, `navigator.sendBeacon(`, `EventSource` — data and actions go through `this._hass` (`states`, `callService`, `callWS`) |
| `no_token_access` | no `access_token`, `hassTokens`, `auth.data`, `localStorage`, `sessionStorage`, `document.cookie` |
| `no_script_tag` | no `<script` string literal (error) |
| `innerhtml_with_state` | a line that assigns `innerHTML`/`outerHTML` or calls `insertAdjacentHTML(` and also mentions `hass` or `state` — warning only; prefer `textContent` for state values |

Checks are regex/line based and deliberately conservative. A card that
violates a warning-level rule stages with the warning shown to the user.

The checks target a cooperative author. They cannot stop a determined
adversary (obfuscation is unbounded); the user's Apply on a card they
can read is the security boundary, and `no_token_access` removes the
one thing a card never legitimately needs.

### 4.2 Staging

`stage_custom_card` params:

```python
class StageCustomCardParams(BaseModel):
    element: str            # e.g. "mylo-game-row"
    description: str        # one line, shown on the approval card (≤ 160 chars)
    source: str             # the whole file
    config_example: dict[str, Any] | None = None   # a card config using this element, shown on the approval card
```

Handler:
1. Run the contract checks. Errors → `ToolResult.error("card_invalid", ..., data={"issues": [...]})`, nothing stored.
2. Resolve the path with `resolve_custom_card_path(config_dir, element)` →
   `<config>/www/mylo-cards/<element>.js`. Read the current file if present.
3. If the current file exists and equals `source` byte for byte →
   `card_unchanged` error (the model should not stage a no-op).
4. `hash = sha256(source)[:8]`; `url = f"/local/mylo-cards/{element}.js?v={hash}"`.
5. Store `StagedCard(card_id, element, path, url, source, previous_source, hash, action, description, config_example, warnings, created_at, conversation_id)` in `CardStore` (TTL 1 h, capacity 20, same shape as `PlanStore`, plus `staged(conversation_id) -> list[StagedCard]` for the plan validator).
6. Return `{"preview": True, "card_id", "element", "action": "create"|"update", "url", "line_count", "byte_count", "warnings", "previous_source", "source", "config_example", "note": "Staged. Reference it as custom:<element> in plan_dashboard. Do not call apply_custom_card until the user's next message approves."}`.

The tool is `Tier.READ`, `cacheable=False`.

### 4.3 Applying

`apply_custom_card(card_id)`, `Tier.MODIFY`:

1. `card_id ∉ ctx.approved_plan_ids` → `card_not_approved`. (The channel name stays `approved_plan_ids` on the wire; it carries every staged id the user approved.)
2. Missing / expired / other conversation → `card_not_found`.
3. `take_backup(path, config_dir, mylo_data_dir)` (no-op on first write; keeps `.js` suffix), then `atomic_write(path, source)`. Failure → `write_failed`.
4. Resources: `get_resources`; find the entry whose `url` starts with `/local/mylo-cards/<element>.js`. If found and its `url` differs → `lovelace/resources/update` with `resource_id`, `url`; if not found → `lovelace/resources/create` with `res_type="module"`, `url`. Resources unlistable (`None`) → attempt create anyway. `CommandError` → `resource_failed` with `data={"path", "url", "hint": "file is written; register the resource by hand or retry"}`.
5. Remove the card from the store. Executor's read cache: `invalidate("query_dashboard_env")` so the next environment query lists the new card.
6. Return `{"preview": False, "card_id", "element", "path", "url", "resource_id", "resource_action": "created"|"updated"|"unchanged", "backup": path|None}`.

### 4.4 File policy

`file_access.py` gains:

```python
CUSTOM_CARD_DIR = Path("www") / "mylo-cards"
CUSTOM_CARD_ELEMENT_RE = re.compile(r"^mylo-[a-z0-9]+(-[a-z0-9]+)*$")

def resolve_custom_card_path(config_dir: Path, element: str) -> Path:
    """The ONE place a .js write is allowed: www/mylo-cards/<element>.js."""
```

It validates the element name, builds the path, resolves symlinks, and
asserts the result is inside `config_dir / CUSTOM_CARD_DIR` — the
resolved path must have `www/mylo-cards` as its parent, so a symlinked
card file that resolves elsewhere (or a symlinked `mylo-cards` folder
itself) is refused rather than followed. It is not reachable from
`write_config_file`/`patch_config_file`; the general
`WRITE_ALLOWED_EXTENSIONS` is untouched.

### 4.5 Plan integration

- `ToolContext.cards: CardStore | None`; `AppKeys.CARDS`; wired like `PlanStore` in `app.py`, `routes_chat.py`, `scripts/chat.py`, and `tests/unit/_helpers.py::make_ctx`.
- `plan_dashboard` computes `installed_custom` as today, then unions
  `{f"custom:{c.element}" for c in ctx.cards.staged(ctx.conversation_id)}`.
  When resources are unlistable (`None`) and a staged card is referenced, the
  union still applies, so the card is accepted rather than warned.
- `detect_custom_cards` already maps `/local/mylo-cards/mylo-x.js?v=abcd` →
  `custom:mylo-x` (query string stripped, `.js` stripped); no change.
- `query_dashboard_env` result gains `mylo_cards: [{element, url}]` from the
  resource list, so the model sees what it has already built.

### 4.6 UI

- `api.ts`/`App.tsx`: `planIdsFromRecords` also collects `card_id` from
  `stage_custom_card` results where `preview === true`. The approved list
  sent on Apply therefore carries both kinds of id.
- `types.ts`: `StagedCardData { card_id, element, action, url, line_count, byte_count, warnings: string[], previous_source: string | null, source, config_example, description }`.
- `buildApprovalContext` gains a `stage_custom_card` branch producing
  `{ description: "Custom card mylo-x (new, 84 lines)", card }`.
- `DashboardPlanCard` gains a `cards: StagedCardData[]` prop and renders a
  "Custom cards" block before the assumptions: one entry per card with
  element, action, line count, the description, the config example as YAML
  (via the existing `toYaml`), warnings, and a "Show source" toggle. On
  `update`, the toggle shows a line diff (added lines in the accent color,
  removed lines in the warning color) computed by a small LCS line diff in
  `ui/src/lib/lineDiff.ts`; on `create` it shows the source. The card
  renders when `plans` is empty and `cards` is not.
- Buttons unchanged: Reject · Modify · Show YAML · Apply.

### 4.7 Prompt (0.7.0) and references

New "Custom cards" block after "Dashboard work":

- Write a custom card only when native cards plus installed custom cards
  cannot express what the user asked for, or the user asks for one.
- The contract: file at `www/mylo-cards/<element>.js`, element `mylo-*`,
  plain `HTMLElement` rendering inside `<ha-card>`, `setConfig` + `set hass`,
  `customElements.get` guard around `define`, `window.customCards` entry, no
  imports, no `fetch`, no `eval`; read `hass.states`, act via
  `hass.callService`.
- Flow: `stage_custom_card` → `plan_dashboard` referencing `custom:<element>`
  → END TURN. On the approval turn: `apply_custom_card` for every staged
  card FIRST, then `apply_dashboard_plan`.
- Updating a card: stage the whole new source (not a patch); the user sees a
  diff. If the user reports the card looks wrong, revise and re-stage.
- After apply, tell the user a hard refresh may be needed the first time.

`src/mylo/data/references/dashboard_examples.yaml` gains a fenced example
card (~45 lines) meeting the contract: an entity row with icon, name, state,
and a tap that calls `toggle`.

### 4.8 Error handling

| Situation | Where | Outcome |
|---|---|---|
| Contract violated | stage | `card_invalid` with issues; nothing stored |
| Source identical to file on disk | stage | `card_unchanged` |
| Apply without approved id | apply | `card_not_approved`; nothing written |
| Staged card expired / other conversation | apply | `card_not_found` |
| Backup or write fails | apply | `write_failed`; nothing registered |
| Resource create/update fails | apply | `resource_failed`; file stays written; hint to retry |
| Plan references a `custom:mylo-*` that is neither registered nor staged | plan | existing `card_schema` error (uninstalled custom card) |

### 4.9 Testing

- `test_custom_card_contract.py`: every code in §4.1 fires on a minimal
  violating source and does not fire on the reference card.
- `test_file_access_custom_card.py`: path resolves; bad element names,
  traversal, and symlink escape are refused; `.yaml` writes are still
  refused for `.js`.
- `test_card_store.py`: TTL, capacity, `staged(conversation_id)`.
- `test_stage_custom_card_tool.py`: staged/not staged, `card_unchanged`,
  distinct ids, warnings threaded, `preview: true` shape.
- `test_apply_custom_card_tool.py`: one write, backup on update, resource
  created vs updated (fake client returns a resource list), `resource_failed`
  leaves the file, `card_not_approved`/`card_not_found`, cache invalidation.
- `test_plan_dashboard_tool.py`: a staged `mylo-` element validates in the
  same turn; an unstaged one still errors.
- UI: `npx tsc -b --noEmit`; manual check in HA.

### 4.10 Migration and release

No config or schema changes. Existing dashboards unaffected. Ships as
1.6.0 through `scripts/release.sh` after the changelog entry.

## 5. Follow-ups

- `remove_custom_card`: delete file, delete resource, refuse while any
  dashboard references the element.
- Optional syntax check by shipping a tiny JS parser (e.g. `esprima`
  Python port) if malformed cards become a problem.
- Screenshot-driven iteration once the panel can capture a card render.
