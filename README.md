# Mylo

A persistent, memory-aware AI agent that lives inside your Home Assistant as a sidebar panel add-on.

Mylo connects deeply to your HA instance over websocket — it knows your entities, devices, areas, automations, dashboards, integrations, and learned preferences. It can read, create, and modify your HA configuration, control devices, detect anomalies, and proactively surface issues. It remembers across sessions.

> **Status:** v1.0.3. Tested daily against a 2200-entity production HA instance. Pre-built images for amd64 and aarch64.

## Install

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories**
2. Add: `https://github.com/Oasis-Enterprise/mylo`
3. Install **Mylo**, set your API key in the Configuration tab
4. Start — it appears as a sidebar panel

Pre-built images available for **amd64** (x86 mini PCs, NUCs, Proxmox) and **aarch64** (Raspberry Pi 4/5). If no pre-built image exists for your architecture, the add-on builds from source on install.

The add-on is **free and open source**. You bring your own API key — Anthropic (Claude), OpenAI, Google Gemini, or Ollama (fully local, $0).

---

## What Mylo can do

### Query your home

Ask natural questions and get real answers from live data, not guesses.

- "What lights are on in the kitchen?"
- "Show me all unavailable sensors"
- "Which automations haven't fired in the last week?"
- "What devices are in the garage?"
- "What's in my error log from the last 6 hours?"
- "Show me my dashboard config for the overview"

**Tools used:** `query_entities`, `query_devices`, `query_automations`, `query_dashboard`, `query_logs`, `query_system`, `read_config_file`

Mylo queries your live HA registries and state — it doesn't guess or hallucinate entity names. An entity resolver with fuzzy matching validates every entity reference and catches mistakes with `did_you_mean` suggestions.

### Control devices

Turn things on/off, lock/unlock, run scripts, trigger scenes — with explicit confirmation for every action.

- "Turn on the living room lights"
- "Lock the front door"
- "Set the thermostat to 72"
- "Run the vacuum"
- "Turn off all lights downstairs"

**Tools used:** `call_service`

Every service call requires user approval — you see what's about to happen and click Apply. Certain services are **hard-blocked** and can never be called through Mylo:
- `homeassistant/restart`, `homeassistant/stop`
- `hassio/host_reboot`, `hassio/host_shutdown`

Others get an **extra warning** before confirmation:
- Unlocking locks
- Disarming alarm panels
- Opening covers (garage doors, blinds)

### Build automations

Describe what you want in plain English. Mylo writes the YAML, validates it, and shows you a diff before applying.

- "Create an automation that turns off kitchen lights at 11pm"
- "Build an automation that locks the front door when everyone leaves"
- "Make the hallway light turn on at 50% when motion is detected after sunset"
- "Disable the morning routine automation"

**Tools used:** `modify_automation`, `write_config_file`, `patch_config_file`, `verify_change`, `reload_config`

Every automation write goes through: **dry-run preview → user approval → atomic write → HA reload → verification**. If the reload fails, Mylo rolls back automatically and tells you what went wrong.

### Build dashboards

Create and modify Lovelace views through conversation. Supports mushroom cards, mini-graph, conditional cards, and more.

- "Add a mobile-friendly view to my overview dashboard with room tiles and quick actions"
- "Create a card that shows my energy usage for the last 24 hours"
- "Add a conditional card that only shows when the garage door is open"

**Tools used:** `modify_dashboard` (create, add_cards, update_view, replace_card, remove_card, delete), `query_dashboard`

Dashboard operations are surgical — Mylo can replace a single view by path, swap one card by index, or remove a card without touching the rest of your dashboard. For new views, it builds incrementally: creates the view with an initial batch of cards, then adds more in follow-up calls.

**Entity validation:** Every entity reference in card configs (including inside Jinja templates like `states('sensor.temp')`) is validated against the live registry before preview. If Mylo hallucinates an entity ID, it gets caught and corrected with fuzzy-match suggestions before you ever see a broken card.

### Organize entities

Bulk rename, reorganize areas, manage labels — clean up your HA without clicking through 200 settings pages.

- "Rename all the kitchen entities to follow snake_case with area prefix"
- "Move the office devices to the new upstairs area"
- "Create a 'needs-attention' label and assign it to all unavailable sensors"
- "Rename sensor.temp_1 to sensor.kitchen_temperature"

**Tools used:** `rename_entities`, `modify_areas`, `manage_labels`

Entity renames include an **optional reference cascade** — Mylo scans your automations.yaml, packages/agent.yaml, and all storage-mode dashboards for the old entity ID and shows you how many references exist before applying.

### Remember things

Mylo has persistent memory that survives across sessions. Tell it things and it remembers.

- "Remember that the basement motion sensor is unreliable"
- "The kids go to bed at 7pm — don't turn on their lights after that"
- "The garage is being converted to a workshop"
- "We prefer mushroom cards for dashboards"
- "The outdoor sprinkler system goes offline in winter, that's normal"

**Tools used:** `memory_note`

Notes are stored immediately in a scratchpad and available for the next turn. A **nightly reconciler** (powered by Haiku to keep costs low) merges scratchpad notes into the structured context file, detects contradictions, and merges duplicates.

**Memory tab:** You can browse, edit, and delete everything Mylo knows — household members, preferences, notes, known issues, patterns, conflicts. Full transparency, nothing hidden. A "Sync now" button triggers the reconciler on demand.

### Monitor your home

Set up sensor monitoring through conversation — Mylo discovers your sensors and lets you pick which ones to track.

- "Help me set up monitoring for my home"
- "Monitor my energy sensors and the basement humidity"
- "What sensors should I be watching?"

**Tools used:** `manage_monitored`, `query_entities`

**What happens after setup:**
- **Hourly:** Availability sweep detects newly-unavailable entities and stale automations (enabled but haven't fired in >48 hours). Sends HA persistent notifications.
- **Nightly:** Baseline recompute from HA's long-term statistics (7-day mean + standard deviation per monitored sensor).
- **Hourly:** Anomaly detection — z-score check against baselines. If a sensor reading is >2.5 standard deviations from normal, you get notified.

### Control notifications

Mylo's proactive notifications respect your preferences. Configure globally or suppress specific types through conversation.

- "Stop notifying me about stale automations"
- "Don't alert me when the sprinkler system goes unavailable"
- "Mute all proactive notifications"

**Tools used:** `manage_notification_filters`

**Suppression types:**
| Type | What it suppresses |
|------|-------------------|
| `stale_automation` | Automations that haven't fired in >48h |
| `unavailable` | Entities that went unavailable |
| `anomaly` | Z-score anomaly alerts |
| `sync_conflict` | Memory sync conflict alerts |
| `*` | All proactive notifications |

Suppressions can be **global** (all of a type) or **entity-scoped** (just `sensor.sprinkler_system`). They're stored in memory and persist across sessions.

**Built-in notification guardrails:**
- **Quiet hours** — non-critical notifications suppressed between configurable start/end times (default 10pm–7am)
- **Daily cap** — max notifications per day (default 3). Critical alerts bypass the cap.
- **Proactive toggle** — master switch to disable all proactive notifications

---

## The three-tab panel

### Chat
Conversational interface with SSE streaming. User messages appear as right-aligned bubbles; Mylo's responses flow as prose. Tool calls show inline with status dots (green = success, red = error, amber = awaiting approval), tool name, duration, and an expandable params view.

When Mylo proposes a change, an **approval card** appears inline with the diff preview. Click **Apply** to commit or **Reject** to cancel.

A **catch-up banner** appears when you return after a gap (>2 hours), summarizing what happened while you were away — memory syncs, background actions, failures. Built from existing data, no LLM call, zero token cost.

**"+ New" button** in the header archives the current conversation and starts fresh. Old conversations stay in the database — nothing is deleted.

### Memory
Browse everything Mylo knows: household members, preferences, notes, known issues, patterns, rejected suggestions, and pending conflicts. Each item has a delete button. Conflicts show the two claims side by side with Keep A / Keep B / Dismiss controls.

A **"Pending — not yet synced"** section at the top shows scratchpad notes that are already being used in conversations but haven't been folded into the main memory yet. Hit **Sync now** to trigger the reconciler.

### Activity
Audit timeline of every tool call Mylo has made, grouped by day. Each entry shows the tool name, result (success/failure/rolled back/denied), dry-run status, tier level, timestamp, and expandable params + details. Filterable by All / Success / Failures.

---

## Cost management

Running on an LLM API costs real money. A free add-on that burns $5/day isn't free. Mylo attacks this from multiple angles:

| Optimization | What it does | Savings |
|-------------|-------------|---------|
| **Result summarization** | After the model processes a tool result, the full payload is replaced with a compact summary in conversation history | ~7,800 tokens saved per subsequent turn for a typical entity query |
| **Minimal detail queries** | Entity queries default to `detail=minimal` (~30 tokens/entity) instead of full attributes (~150 tokens/entity) | 5x reduction on broad queries |
| **Default limit 50** | Entity queries return max 50 results by default instead of dumping everything | Prevents 200-entity payloads |
| **Read-only result cache** | Identical read-only tool calls within 120 seconds return cached results | Eliminates redundant HA queries |
| **Topology routing** | The home topology in the system prompt often answers questions without a tool call at all | Saves entire tool call round trips |
| **Budget warnings** | When session cost hits 80% of the configured cap, Mylo mentions it naturally | Prevents surprise bills |

**Session budget:** Configurable per-session cap (default $0.50). The UI footer shows running cost and token budget.

**Monthly budget:** Configurable monthly cap (default $15.00).

**Typical session cost:** $0.10–$0.30 on Claude Sonnet for a multi-turn conversation with tool calls.

**$0 option:** Use Ollama with a local model. Tool calling quality depends on the model — llama3.1 and qwen2 work best.

---

## LLM providers

| Provider | Config value | API key | Default model | Cost | Notes |
|----------|-------------|---------|---------------|------|-------|
| **Anthropic** | `anthropic` | Anthropic key | `claude-sonnet-4-6` | ~$3–15/Mtok | Default. Best tool calling quality. |
| **OpenAI** | `openai` | OpenAI key | `gpt-4o` | ~$2.50–10/Mtok | GPT-4o, GPT-4-turbo, etc. |
| **Gemini** | `gemini` | Google AI Studio key | `gemini-2.5-flash` | ~$0.15–10/Mtok | Coming soon in v1.0.4. |
| **Ollama** | `ollama` | none | `llama3.1` | $0 | Local models. Needs Ollama running on host. |

All providers use the same `api_key` field in the Configuration tab — just put the right key for your chosen provider. If you switch providers but forget to update the `model` field, Mylo auto-detects the mismatch and falls back to the provider's default model.

For Ollama: set `ollama_url` in the Configuration tab to your Ollama server's address (e.g. `http://192.168.1.50:11434/v1`). Default is `http://host.docker.internal:11434/v1` which works if Ollama runs on the same machine as HA. Leave `api_key` empty — Ollama doesn't use one. Budget warnings are automatically disabled since cost is $0.

**Ollama model sizing guide:** Mylo has 21 tools with complex schemas. Smaller models struggle to produce valid tool calls reliably.

| Size | Examples | Experience |
|------|----------|-----------|
| **7B** | llama3.1:7b, mistral:7b | Not recommended. Struggles with complex tool parameters, frequently produces malformed JSON, and hallucinates entity IDs. May not self-correct after errors. |
| **14B** | qwen2.5:14b | Usable for simple queries (lights, sensors, basic automations). Will struggle with multi-step tasks like dashboard building or entity rename cascades. |
| **32B** | qwen2.5:32b, deepseek-r1:32b | Good. Handles most Mylo features reliably. Best balance of quality vs hardware requirements. |
| **70B+** | llama3.1:70b | Near cloud-API quality. Requires significant hardware (64GB+ RAM or a dedicated GPU). |

**Minimum recommended: 14B.** For the best local experience: **32B.**

---

## Configuration

Set in the add-on **Configuration** tab:

| Option | Default | Description |
|--------|---------|-------------|
| `api_key` | — | API key for your chosen provider (required for Anthropic/OpenAI/Gemini) |
| `llm_provider` | `anthropic` | LLM backend: `anthropic`, `openai`, `gemini`, or `ollama` |
| `model` | `claude-sonnet-4-6` | Primary chat model (auto-corrects if mismatched with provider) |
| `reconciliation_model` | `claude-haiku-4-5-20251001` | Model for nightly memory sync (use a cheap model) |
| `ollama_url` | — | Ollama server URL (e.g. `http://192.168.1.50:11434/v1`) |
| `sync_frequency` | `nightly` | Memory sync schedule: `nightly` / `weekly` / `manual` |
| `memory_token_limit` | `8000` | Max tokens for the memory section of the system prompt |
| `proactive_notifications` | `true` | Enable hourly monitoring + anomaly alerts |
| `max_daily_notifications` | `3` | Cap on proactive notifications per day |
| `quiet_hours_start` | `22:00` | Suppress non-critical notifications after this time |
| `quiet_hours_end` | `07:00` | Resume notifications after this time |
| `session_budget_usd` | `0.50` | Per-conversation cost cap in USD |
| `monthly_budget_usd` | `15.00` | Monthly cost cap in USD |

---

## Safety model

Mylo uses a three-tier permission system:

| Tier | Actions | Approval required | Examples |
|------|---------|-------------------|----------|
| **Tier 1 — Read** | Query entities, devices, automations, logs, system info, read config files, record memory notes, list labels/areas/monitored entities/notification filters | No | `query_entities`, `memory_note`, `manage_labels list` |
| **Tier 2 — Modify** | Write config files, modify automations, rename entities, modify dashboards, modify areas, manage monitored entities, manage notification filters | Yes (dry-run first) | `modify_automation`, `rename_entities`, `modify_dashboard` |
| **Tier 3 — Action** | Call HA services (lights, locks, covers, scripts, scenes), reload configuration | Yes (explicit confirmation) | `call_service`, `reload_config` |

**Hard-blocked services** (can never be called, even with approval):
- `homeassistant/restart`, `homeassistant/stop`
- `hassio/host_reboot`, `hassio/host_shutdown`, `hassio/supervisor_reload`

**Restricted services** (extra warning before confirmation):
- Unlocking locks, disarming alarm panels, opening covers

**Audit logging:** Every tool call is logged to an append-only JSON Lines audit file with timestamp, tool name, tier, parameters, dry-run status, approval status, and result. Browse the full history in the Activity tab.

**Rollback:** Tier-2 file writes use atomic write → reload → verify → rollback-on-failure. If a config change causes a reload error, the original file is restored automatically.

---

## Things you might not know you can do

**Tell Mylo about your household:**
- "I work from home most days"
- "The kids go to bed at 7pm"
- "My wife prefers warm lighting"

This gets stored in the household section of memory and influences future behavior — Mylo won't suggest turning on kids' lights after bedtime.

**Suppress specific entities from monitoring:**
- "The sprinkler system goes offline in winter, stop alerting about it"
- "Ignore the Bluetooth sensor being unavailable, it's flaky"

**Ask for troubleshooting help:**
- "Why isn't my motion sensor working?"
- "The garage door automation stopped firing, help me debug it"
- "Show me errors from the last 6 hours"

Mylo pulls relevant logs, checks automation traces, and cross-references against known issues in its memory.

**Review what happened while you were away:**
Open the panel after a gap and the catch-up banner shows what changed — memory syncs, background actions, failures. No LLM cost for this.

**Use slash commands:**
- `/clear` or `/new` — archive conversation and start fresh
- `/help` — show available commands

**Check the cost of your session:**
The footer shows `budget: Nk/200k tokens · cost: $X.XX this session` in real time.

---

## Local development

Requires Python 3.12+, Node 20+.

```bash
git clone https://github.com/Oasis-Enterprise/mylo.git
cd mylo

python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cd ui && npm install && cd ..

cp .env.example .env
# Edit .env: HA_URL, HA_TOKEN, ANTHROPIC_API_KEY, MYLO_CONFIG_DIR
```

```bash
.venv/bin/python -m mylo              # run server
.venv/bin/python -m mylo.scripts.chat # CLI chat (debugging)
.venv/bin/pytest tests/unit/          # tests
.venv/bin/mypy src/                   # type check
.venv/bin/ruff check src/             # lint
```

## Architecture

See [`MYLO_SPEC.md`](MYLO_SPEC.md) for the full specification and [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) for the build plan with current status.

## License

Apache 2.0 — see [LICENSE](LICENSE).
