# Mylo

A persistent, memory-aware AI agent that lives inside your Home Assistant as a sidebar panel add-on.

Mylo has full context of your home — devices, areas, automations, dashboards, integrations, and learned preferences. It can read, create, and modify HA configurations, detect anomalies, and proactively surface issues.

> **Status:** v1.0.0. Functional and tested against a 2200-entity production HA instance. Pre-built images available for amd64 and aarch64.

## Features

**Conversational home management**
- Natural-language query, control, and configuration of Home Assistant
- 21 registered tools across three tiers (read / modify / action)
- Entity resolver with fuzzy matching — catches hallucinated entity IDs before they hit HA

**Write tools with safety**
- Dashboard generation — build and modify Lovelace views from plain English
- Automation authoring — create, update, enable/disable with YAML validation
- Entity rename with reference cascade — scans automations + dashboards for old IDs
- Area/label management — bulk organize via websocket
- Every write is dry-run first → user previews the diff → clicks Apply to commit
- Atomic writes with automatic rollback on failure

**Memory system**
- Persistent memory in `context.yaml` — household, preferences, notes, known issues, patterns
- Scratchpad for immediate in-conversation recall
- Haiku-powered nightly reconciler merges scratchpad → context with conflict detection
- Deterministic pruner (TTL, stale observations, low-confidence patterns)
- Memory tab: browse, delete, sync, resolve conflicts — full transparency

**Background monitoring**
- Hourly availability sweep — detects newly-unavailable entities, stale automations
- Nightly baseline recompute from HA long-term statistics (mean + stddev)
- Z-score anomaly detection against baselines (threshold |z| > 2.5)
- Proactive HA notifications with quiet hours + daily cap enforcement
- `manage_monitored` tool — the AI helps users discover and select sensors to monitor

**Four-layer context assembler**
- Layer 1: Static identity + security rules
- Layer 2: Compressed home topology from live registries
- Layer 3: Selective memory injection (keyword-gated, not dump-everything)
- Layer 4: Task-specific few-shot references (automation, dashboard, troubleshooting, entity management)

**Panel UI (Signal theme)**
- Tactical green-on-black with JetBrains Mono + Inter
- Chat tab: SSE streaming, inline tool calls with status dots + duration, right-aligned user bubbles, approval cards with diff preview
- Memory tab: browse/delete notes/issues/patterns/conflicts, sync button, scratchpad pending view
- Activity tab: audit log timeline grouped by day, filterable by result
- Session budget/cost tracking in the footer
- Animated thinking indicator with rotating contextual phrases

## Installation

### As a Home Assistant add-on (recommended)

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store**
2. Click the three-dot menu (⋮) → **Repositories**
3. Add: `https://github.com/Oasis-Enterprise/mylo`
4. Find **Mylo** in the store and click **Install**
5. In the add-on Configuration tab, set your `api_key` (Anthropic API key)
6. Start the add-on — Mylo appears as a sidebar panel

> **Note:** Pre-built images are available for amd64 (x86 mini PCs, NUCs, Proxmox) and aarch64 (Raspberry Pi 4/5). If no pre-built image exists for your architecture, the add-on builds from source on install.

### Local development

Requires Python 3.12+, Node 20+.

```bash
# Clone
git clone https://github.com/Oasis-Enterprise/mylo.git
cd mylo

# Python
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'

# UI
cd ui && npm install && cd ..

# Environment
cp .env.example .env
# Edit .env: HA_URL, HA_TOKEN, ANTHROPIC_API_KEY, MYLO_CONFIG_DIR
```

**Run the server:**
```bash
.venv/bin/python -m mylo
```

**CLI chat (debugging):**
```bash
.venv/bin/python -m mylo.scripts.chat
```

**Tests:**
```bash
.venv/bin/pytest tests/unit/
.venv/bin/mypy src/
.venv/bin/ruff check src/
```

## Configuration

Set in the add-on Configuration tab (or `options.json` for local dev):

| Option | Default | Description |
|--------|---------|-------------|
| `api_key` | — | Anthropic API key (required) |
| `model` | `claude-sonnet-4-6` | Primary chat model |
| `reconciliation_model` | `claude-haiku-4-5-20251001` | Model for nightly memory sync |
| `sync_frequency` | `nightly` | Memory sync schedule: nightly / weekly / manual |
| `memory_token_limit` | `8000` | Max tokens for the memory section |
| `proactive_notifications` | `true` | Enable hourly monitoring + anomaly alerts |
| `max_daily_notifications` | `3` | Cap on proactive notifications per day |
| `quiet_hours_start` | `22:00` | Suppress non-critical notifications after this time |
| `quiet_hours_end` | `07:00` | Resume notifications after this time |

## Architecture

See [`MYLO_SPEC.md`](MYLO_SPEC.md) for the full specification and [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) for the build plan with current status.

## License

Apache 2.0 — see [LICENSE](LICENSE).
