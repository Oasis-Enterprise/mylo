# Surface — Design

**Status:** approved as a section-level design in conversation 2026-09-22;
third of three UX releases (1.7 Live turn → 1.8 Trust → **1.9 Surface**).
**Target release:** 1.9.0.

## 1. Problem

The panel now tells the truth (1.7, 1.8) but still speaks in internals:

1. **Jargon.** Tool rows show `rename_entities`; approval cards show
   "TIER-2"; errors render as a 10 px monospace strip of `Type: message`.
2. **The Memory tab is an admin panel.** Raw ids in `<code>`, "prune
   candidates", scratchpad `type`/`scope` tags, Keep A / Keep B conflict
   buttons, and a Delete with no confirmation. `preferences` and
   `monitored_entities` exist in the data but are never shown.
3. **First run is a dead end.** The four quick-start rows are plain text.
   Nothing explains what Mylo can touch or that approval gates every
   change. Monitoring is silent for ~14 days (`profiles.py`
   `MIN_DAYS_FOR_DURATION = 14`) and nothing says so.
4. **Three review cards, three action rows.** `ApprovalCard`,
   `DashboardPlanCard` and `QuestionCard` each style their own buttons.
5. **No responsive rules, no accessibility.** Zero breakpoints, one
   `aria-label` in the whole tree, no visible focus styles, fixed pixel
   gutters (`paddingRight: 40`, `paddingLeft: 60`), 10 px labels
   everywhere.
6. **Tool surface rough edges.** `verify_change` advertises six checks and
   implements three. Tool descriptions restate policy that also lives in
   the prompt (five over 400 chars). Three tools have no description at
   all. A whole-home entity gather is ~30 tokens per entity with no
   cheaper shape.

## 2. Goals

1. Every name the user sees is plain language; internals stay one click
   away.
2. The Memory tab reads as "what Mylo knows about your home" by default,
   with the tooling behind one Advanced toggle (user's choice, 2026-09-22).
3. A first-time user can click to start, learns the approval model in
   three lines, and knows when monitoring will speak.
4. One set of button primitives and one action bar across the review
   cards.
5. Usable at phone width with a keyboard and a screen reader.
6. The tool surface says only what it does; policy lives in the prompt.

## 3. Non-goals

- Theme, typography scale, or colour changes beyond what accessibility
  needs (contrast is already fine on the dark palette).
- New features, new tools, or new endpoints beyond `tool_labels` in
  status.
- A model-generated welcome turn (decided against: costs a call and
  varies; the primer is static).
- Rewriting the Memory tab's data model or endpoints.

## 4. Design

### 4.1 Plain-language labels and errors

**Server.** `/api/status` gains `tools: {name: {"label": str, "tier": int}}`
for every registered tool, using `status_labels.label_for` and
`ToolDefinition.tier`. One dict, ~32 entries, fetched on the existing
poll.

**Panel.** New `ui/src/lib/labels.ts`:

```ts
export function toolLabel(name: string, table?: ToolLabelTable): string
// table hit → its label; else humanize: "query_entities" → "Query entities".
export function tierLabel(tier: number | string): string
// 1 → "Read-only", 2 → "Changes config", 3 → "Controls devices";
// accepts "TIER-2"-style strings from existing contexts.
```

`useSession` stores the table from status. Consumers:

- `ToolCallBlock`: row title is `toolLabel(name)`; the raw name moves into
  the expanded details header. Status and duration unchanged.
- `ApprovalCard` / `DashboardPlanCard` / `turnState.buildApprovalContext`:
  `tierLabel` replaces "TIER-2"/"TIER-3" text; the `Tag` keeps its tone.
- The generic fallback description `"${call.name} · dry run"` becomes
  `"${toolLabel(call.name)} (preview)"`.
- `ActivityTab`: the tool column shows the label with the raw name as
  `title`.

**Errors.** The bottom error strip becomes prose (13 px sans): a short
human sentence chosen by `describeError(errorType, message)` in
`ui/src/lib/errors.ts` (network/`TypeError: Failed to fetch` → "Lost the
connection to Mylo. It will reconnect on its own."; `turn_in_progress` →
existing copy; HTTP 5xx → "Mylo hit a problem on the server. Try again in
a moment."; default → "Something went wrong."), followed by a "Details"
disclosure that reveals the raw `type: message` in mono. The strip gets
`role="alert"` and a dismiss ✕. The reconnect strip gets `role="status"`.

### 4.2 Memory tab: homeowner view + Advanced

Default view (top to bottom):

1. **Header**: "Mylo's memory of your home" · "updated {relative}" (or the
   existing sync-failed line) · an **Advanced** toggle (persisted in
   `localStorage` key `mylo.memory.advanced`, wrapped in try/catch).
2. **Needs your attention** (only when present): pending conflicts count →
   "Mylo found N facts that disagree. Review them under Advanced."; sync
   error line.
3. **Your household**: members as "Name · role" with notes; shared facts
   from `household.shared` rendered as "Key: value" with humanized keys.
4. **Preferences**: `preferences` entries as "Key: value" (humanized keys,
   values stringified; nested objects rendered as their JSON one-liner).
5. **What Mylo remembers**: notes as plain text, newest first, each with a
   **Forget** button that first turns into "Forget this?" and confirms on
   the second click (calls the existing delete endpoint). No ids, no
   `refs`, no `protected` pill.
6. **Known issues**: description, suggested fix, status word.
7. **Monitored**: "Mylo is watching N entities" plus the ids, and the
   sentence "Alerts start after about two weeks of learning what normal
   looks like." when `monitored_entities` is non-empty.

Advanced (appended below when toggled on): Sync now + `SyncResultCard`
with prune, the scratchpad "pending" list, the conflicts section with
Keep A / Keep B / Dismiss, rejected suggestions, and ids/refs/protected
shown on notes and issues. Every existing capability remains reachable;
nothing is removed.

### 4.3 First run

- **Quick-starts are buttons**: each row becomes a `<button>` that calls
  the same `handleSubmit(text)` the composer uses; keyboard focusable,
  `aria-label="Ask: <text>"`.
- **Primer card** in `EmptyState`, above the quick-starts, titled "How Mylo
  works" with three lines:
  - "Mylo can read everything in your Home Assistant, and asks before it
    changes anything."
  - "Every change is previewed. Nothing is written until you click Apply,
    and a backup is taken first."
  - "Turning devices on or off also needs your Apply."
  The card renders only while the conversation is empty.
- **Monitoring copy**: `manage_monitored` add/replace results gain
  `note: "Alerts start after about two weeks of learning what normal looks like."`
  and prompt 0.9.0 tells the model to relay it. The same sentence appears
  in the Memory tab (§4.2).
- `/help` lists the three slash commands and the primer's three lines.

### 4.4 Shared primitives and action bar

`ui/src/components/ui/Button.tsx`: `PrimaryButton` (accent, `btn-glow`,
`tone="accent" | "error"`), `GhostButton`, `IconButton` (requires
`aria-label`). All: `min-h-9` on touch (`@media (pointer: coarse)`),
`focus-visible` ring, `disabled:opacity-40`.

`ui/src/components/ui/ActionBar.tsx`:

```ts
interface ActionBarProps {
  primary: { label: string; onClick: () => void; disabled?: boolean; tone?: "accent" | "error" };
  secondary?: Array<{ label: string; onClick: () => void }>;
  note?: ReactNode;  // e.g. "Deletes: …"
}
```

Renders `note` above a right-aligned, wrapping row (secondary ghosts, then
primary). `ApprovalCard` and `DashboardPlanCard` replace their footers
with it; `QuestionCard` uses `GhostButton` for options and keeps its own
layout (it has no primary action).

### 4.5 Responsive and accessible

- **Gutters**: one `px-4` on `<main>`; the `paddingRight: 40` /
  `paddingLeft: 60` inline styles become `sm:pr-10` / `sm:pl-16` classes
  (nothing at phone width). Action rows `flex-wrap gap-2`. Header status
  bar already wraps.
- **Type**: the smallest prose stays 12.5 px; 9 px status labels become
  10 px at `<sm` (`text-[9px] sm:text-[10px]` inverted where needed) — a
  single Tailwind change in `Composer` and `Header`.
- **Focus**: global `:focus-visible { outline: 2px solid var(--color-accent); outline-offset: 2px }`
  in `index.css`; no `outline-none` left on interactive elements.
- **ARIA**: `role="tablist"`/`role="tab"`/`aria-selected` on header tabs;
  `aria-label` on every icon-only button (✕ dismiss, Stop, + New,
  chevrons); `role="status" aria-live="polite"` on `ThinkingIndicator`
  and the reconnect strip; `role="alert"` on the error strip; the
  composer textarea gets `aria-label="Message Mylo"`; checkboxes keep
  their labels; `ToolCallBlock` expander is a `<button aria-expanded>`.
- **Touch**: `@media (pointer: coarse)` raises interactive `min-height` to
  36 px via the Button primitives; the Stop/send button too.

### 4.6 Tool surface cleanup

- `verify_change`: drop `no_new_errors`, `service_available`,
  `full_health` from `CheckType` and the description; delete the
  `not_implemented` branch; update `tests/unit/test_tools_m2b.py`.
- **Descriptions are mechanics only.** Trim every `ToolDefinition.description`
  to what the tool does and what its parameters mean, ≤ 300 chars. Policy
  sentences move to the prompt (0.9.0) where not already present:
  query_entities ("check the topology summary first; one broad gather for
  bulk work; `detail="ids"` for whole-home lists"), ask_user
  (consolidate to one question), query_dashboard_env ("never guess a
  theme; only listed custom cards"), stage_custom_card ("only when native
  or installed cards cannot"). Approval-flow sentences already in the
  prompt are deleted from descriptions. The three tools with no
  description (`rename_entities`, `read_custom_card`, `query_system` —
  verify; they may build the definition another way) get one.
- **Compact gather**: `query_entities.detail` gains `"ids"` → each row is
  `{entity_id, friendly_name}` only; the forced downgrade rule becomes
  `>100 rows → minimal`, `>500 rows → ids`. `plan_dashboard`-style bulk
  work is told to use it via the prompt.
- Prompt 0.9.0 with a `PROMPT_CHANGELOG` entry.

### 4.7 Error handling

- Missing `tools` in status (older server) → `toolLabel` humanizes.
- `localStorage` unavailable → Advanced defaults off, no error.
- Forget confirmation resets if the user clicks elsewhere (blur) or after
  5 s.
- `detail="ids"` rows lack `state`; the model is told so in the param
  description.

### 4.8 Testing

Python: `verify_change` enum test; `query_entities` `ids` shape and the
500-row downgrade; status `tools` payload; description length ≤ 300 for
every registered tool (`test_tool_descriptions.py`); prompt 0.9.0.
UI (Vitest): `labels.ts` (table hit, humanize fallback, tier strings);
`errors.ts` mapping; `memoryView.ts` pure helpers (humanize keys,
stringify values, newest-first notes). `tsc` clean. Manual: the panel at
400 px width has no horizontal scroll and every control is reachable by
Tab.

## 5. Rollout

Changelog `## [1.9.0]`; merge = release via `scripts/release.sh 1.9.0`.
