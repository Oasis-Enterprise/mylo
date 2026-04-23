# Mylo Roadmap

A living document. Priorities shift based on user feedback and what surfaces from real-world usage. If something here matters to you, open an issue or upvote an existing one.

---

## Now

Features actively being worked on for upcoming releases.

### Conversation Agent Registration
Register Mylo as a Home Assistant conversation agent. Once connected, Assist, voice satellites, and the companion app all route through Mylo — full tool access, memory, and context from any voice interface. "Hey Mylo, lock the front door" works from your phone, Google Home, or any HA voice satellite.

This is the single highest-impact feature on the roadmap. It turns Mylo from a sidebar panel into the voice of the home.

### Helper Entity Creation
Create and manage HA helper entities through conversation — input_booleans, input_numbers, input_selects, timers, counters, input_datetimes. Currently users have to go to Settings → Helpers manually for each one.

- "Create a toggle helper called guest mode"
- "Add an input_number for target temperature with a range of 60-80"
- "Create a timer called laundry with a 45 minute duration"

### Scene Management
Create, edit, activate, and delete scenes through conversation. Capture the current state of a room as a scene without manually listing every entity and attribute.

- "Save the current living room lighting as 'movie night'"
- "Create a scene called 'morning' with kitchen lights at 80% and the coffee maker on"
- "Activate the bedtime scene"

---

## Soon

Next in line after the current batch ships.

### Script Management
Create, edit, and trigger scripts through conversation. Similar to the automation tools but for HA scripts — reusable action sequences without triggers.

- "Build a script that flashes the porch light 3 times"
- "Create a script that announces on all speakers that dinner is ready"
- "Run the welcome home script"

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

### Entity History
Query historical state data and trends, not just current state. Pull from HA's history and long-term statistics APIs and present summarized trends.

- "Show me the basement temperature over the last 48 hours"
- "When was the last time the front door was unlocked?"
- "How much energy did the dryer use this week?"
- "What's the average humidity in the bathroom over the last month?"

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

- Persistent memory system with nightly reconciler (v1.0.0)
- Background monitoring — hourly sweeps, baselines, anomaly detection (v1.0.0)
- Notification suppression filters (v1.0.0)
- Dashboard entity reference validation (v1.0.0)
- Cost controls — result summarization, detail levels, caching, budgets (v1.0.0)
- Surgical dashboard operations — update_view, replace_card, remove_card (v1.0.4)
- Gemini provider support (v1.0.4)
- OpenAI GPT-5.x compatibility (v1.0.3)
- Four LLM providers — Anthropic, OpenAI, Gemini, Ollama (v1.0.4)
