# Mylo

A persistent, memory-aware AI agent that lives inside your Home Assistant instance.

Mylo is accessed through a sidebar chat panel and has full context of your home — devices, areas, automations, dashboards, integrations, and learned preferences. It can read, create, and modify HA configurations, detect anomalies, and proactively surface issues.

> **Status:** pre-alpha. Under active development. Not yet suitable for production HA instances.

## What it does

- Conversational home management — natural-language interface to query, control, and configure HA
- Dashboard generation — build Lovelace dashboards from plain English descriptions
- Automation authoring — create, modify, and debug automations with plain-English previews
- Entity management — bulk rename, reorganize, clean up naming conventions
- Anomaly detection — learn baseline patterns and flag unusual behavior
- Proactive monitoring — surface issues and maintenance suggestions without being asked
- Persistent memory — learn user preferences, home context, and behavioral patterns over time

## Architecture

See [`MYLO_SPEC.md`](MYLO_SPEC.md) for the full architecture specification and [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) for the build plan.

## Installation

_Coming soon._ Distribution will be via a Home Assistant **add-on repository** (not HACS). Once ready, you'll add this repo's URL in Supervisor → Add-on Store → ⋮ → Repositories.

## Development

Requires Python 3.12, Node 20+, and Docker (for integration tests against a real HA instance).

```bash
# Install Python deps
uv sync

# Run the module locally (prints config + exits)
python -m mylo
```

## License

MIT — see [LICENSE](LICENSE).
