# Mylo Roadmap

A living document. Priorities shift based on user feedback and what surfaces from real-world usage. If something here matters to you, open an issue or upvote an existing one.

---

## Now

Features actively being worked on for upcoming releases.

### Hardening after the 1.7–1.9 UX releases
- Wire the prompt-injection sanitizer into the entity-name surfaces of the system prompt.
- Make the monthly budget a hard stop, not just a warning.
- An integration test that runs a full chat turn against a fake Home Assistant, and image builds pinned to the lockfile.
- Small follow-ups from review: cap the monitored-entity list in the Memory tab, past-tense labels on finished tool rows, and prompt wording for the compact entity shape.

---

## Soon

Next in line after the current batch ships.

### Template Tester
Test Jinja templates without leaving the chat or going to Developer Tools. Useful for debugging automations and understanding template syntax.

- "What does `{{ states('sensor.kitchen_temp') | round(1) }}` evaluate to?"
- "Test this template: `{{ is_state('light.kitchen', 'on') }}`"
- "Help me write a template that shows how long the front door has been open"

### Past Conversation Browser
Browse and search archived conversations. Right now "New conversation" archives to SQLite but there's no way to go back and find what you discussed last week. Adds a conversation list with search to the Chat tab.

- "What did we talk about with the garage door automation?"
- Scroll through past sessions by date
- Search across all conversations for entity names or topics

---

## Later

On the radar but not yet scoped. Some of these are significant architectural work.

### Weekly Digest
A scheduled summary pushed as an HA notification — what happened this week across your home. Builds on the existing monitoring and audit systems.

- Entities that went offline and came back (or didn't)
- Automations that failed or stopped firing
- Energy usage trends vs baseline
- Memory changes and unresolved conflicts
- Anomalies detected

### Automation Conflict Detection
Analyze all automations and flag potential conflicts — two automations that control the same entity at similar times, contradictory conditions, or overlapping triggers.

- "These two automations both try to set the kitchen lights at sunset but with different brightness"
- "This automation disables a script that another automation depends on"

### Proactive Automation Suggestions
Learn from entity state patterns over time and suggest automations the user hasn't built yet.

- "I notice you turn off the kitchen lights manually around 11pm every night. Want me to automate that?"
- "The garage door has been left open past midnight 4 times this month. Want an alert for that?"

Requires entity history analysis beyond what the current baseline system does — pattern mining over days/weeks of state changes, not just point-in-time anomaly detection.

### Multi-User Support
Different household members get their own conversations, permissions, and memory sections. Ties into HA's user system.

- Per-user permission tiers (adults get full access, kids get read-only)
- Per-user preferences and memory (Mary's lighting preferences vs Maxwell's)
- Audit log shows which user initiated each action
- Shared household memory stays common; personal notes are private

---

## Shipped

For reference — features that were on the roadmap and have shipped.

- Plain-language labels, homeowner Memory tab, first-run primer, shared buttons, accessibility and phone-width layout, mechanics-only tool descriptions (v1.9.0)
- Verification outcomes in the chat, sync-failure visibility, scoped per-item approval, backups stated after writes (v1.8.0)
- Streamed replies, Stop button, live status labels (v1.7.0)
- Custom card authoring behind Apply (v1.6.0)
- Dashboard plan/apply flow with heading-addressed sections (v1.5.0)
- Helper entity creation, scene management, script management, entity history, automation trace debugging, zones (v1.1–v1.4)

- Persistent memory system with nightly reconciler (v1.0.0)
- Background monitoring — hourly sweeps, baselines, anomaly detection (v1.0.0)
- Notification suppression filters (v1.0.0)
- Dashboard entity reference validation (v1.0.0)
- Cost controls — result summarization, detail levels, caching, budgets (v1.0.0)
- Surgical dashboard operations — update_view, replace_card, remove_card (v1.0.4)
- Gemini provider support (v1.0.4)
- OpenAI GPT-5.x compatibility (v1.0.3)
- Four LLM providers — Anthropic, OpenAI, Gemini, Ollama (v1.0.4)
