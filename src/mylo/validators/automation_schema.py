"""Schema validation for automations and scripts.

We don't try to replicate HA's full internal schema — that's a moving
target across versions. Instead we validate the shapes the LLM is most
likely to get wrong: the three top-level sections (``trigger``,
``condition``, ``action``), enumerated platforms/services, and the
``choose`` / ``if-then`` branching shapes.

Every validation error is a :class:`SchemaIssue` with a path (dotted or
indexed), a short message, and a severity (``error`` blocks writes,
``warning`` is surfaced in the dry-run preview but doesn't block).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Severity = Literal["error", "warning"]


@dataclass(slots=True)
class SchemaIssue:
    path: str
    message: str
    severity: Severity = "error"

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "message": self.message, "severity": self.severity}


@dataclass(slots=True)
class ValidationReport:
    issues: list[SchemaIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    def error(self, path: str, message: str) -> None:
        self.issues.append(SchemaIssue(path, message, "error"))

    def warn(self, path: str, message: str) -> None:
        self.issues.append(SchemaIssue(path, message, "warning"))

    def extend(self, other: ValidationReport) -> None:
        self.issues.extend(other.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "issues": [i.to_dict() for i in self.issues],
        }


# ─── Known enums. Intentionally permissive — HA adds new platforms all the
# time, so this is a "seen before" list, not an allowlist. An unknown
# platform is a warning, not an error.


KNOWN_TRIGGER_PLATFORMS: frozenset[str] = frozenset(
    {
        "calendar",
        "conversation",
        "device",
        "event",
        "geo_location",
        "homeassistant",
        "mqtt",
        "numeric_state",
        "persistent_notification",
        "state",
        "sun",
        "tag",
        "template",
        "time",
        "time_pattern",
        "webhook",
        "zone",
    }
)

KNOWN_CONDITION_TYPES: frozenset[str] = frozenset(
    {
        "and",
        "or",
        "not",
        "device",
        "numeric_state",
        "state",
        "sun",
        "template",
        "time",
        "trigger",
        "zone",
    }
)


def validate_automation(config: Any) -> ValidationReport:
    """Validate an automation dict (after YAML parse).

    Storage-mode single-automation configs are a dict; YAML-mode can be a
    list-of-dicts (multi-automation file). This function handles both.
    """
    report = ValidationReport()
    if isinstance(config, list):
        for i, item in enumerate(config):
            sub = validate_automation(item)
            for issue in sub.issues:
                issue.path = f"[{i}].{issue.path}" if issue.path else f"[{i}]"
                report.issues.append(issue)
        return report

    if not isinstance(config, dict):
        report.error("", f"expected dict or list of dicts, got {type(config).__name__}")
        return report

    if "trigger" not in config and "triggers" not in config:
        report.error("trigger", "automation must define 'trigger' (or 'triggers')")
    if "action" not in config and "actions" not in config:
        report.error("action", "automation must define 'action' (or 'actions')")

    _validate_triggers(config.get("trigger") or config.get("triggers"), report)
    _validate_conditions(config.get("condition") or config.get("conditions"), report)
    _validate_actions(config.get("action") or config.get("actions"), report)

    if "mode" in config and config["mode"] not in {
        "single",
        "restart",
        "queued",
        "parallel",
    }:
        report.error("mode", f"unknown mode {config['mode']!r}")

    return report


def validate_script(config: Any) -> ValidationReport:
    """Scripts are a map of ``alias -> {sequence|fields|...}`` (storage
    mode stores them keyed by id). Minimal validation: sequence must be
    a list of action steps.
    """
    report = ValidationReport()
    if not isinstance(config, dict):
        report.error("", "script must be a mapping")
        return report
    sequence = config.get("sequence")
    if sequence is None:
        report.error("sequence", "script must define 'sequence'")
    else:
        _validate_actions(sequence, report, path_prefix="sequence")
    return report


# ─── Helpers ────────────────────────────────────────────────────────────────


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _validate_triggers(value: Any, report: ValidationReport) -> None:
    triggers = _as_list(value)
    for i, trig in enumerate(triggers):
        path = f"trigger[{i}]"
        if not isinstance(trig, dict):
            report.error(path, "trigger must be a mapping")
            continue
        platform = trig.get("platform") or trig.get("trigger")
        if platform is None:
            report.error(path, "trigger missing 'platform' (or 'trigger') key")
        elif platform not in KNOWN_TRIGGER_PLATFORMS:
            report.warn(path, f"unknown trigger platform {platform!r}")


def _validate_conditions(
    value: Any, report: ValidationReport, path_prefix: str = "condition"
) -> None:
    conditions = _as_list(value)
    for i, cond in enumerate(conditions):
        path = f"{path_prefix}[{i}]"
        if not isinstance(cond, dict):
            report.error(path, "condition must be a mapping")
            continue
        cond_type = cond.get("condition")
        if cond_type is None:
            # Shorthand: a condition without 'condition:' key is assumed to
            # be a template-string or state check — HA is permissive.
            continue
        if cond_type not in KNOWN_CONDITION_TYPES:
            report.warn(path, f"unknown condition type {cond_type!r}")
        if cond_type in ("and", "or", "not"):
            inner = cond.get("conditions", [])
            _validate_conditions(inner, report, path_prefix=f"{path}.conditions")


def _validate_actions(value: Any, report: ValidationReport, path_prefix: str = "action") -> None:
    actions = _as_list(value)
    for i, act in enumerate(actions):
        path = f"{path_prefix}[{i}]"
        if not isinstance(act, dict):
            report.error(path, "action must be a mapping")
            continue
        # The main shapes: service call, choose, if/then, repeat, wait_*, etc.
        if "choose" in act:
            for j, choice in enumerate(act["choose"] or []):
                if not isinstance(choice, dict):
                    report.error(f"{path}.choose[{j}]", "choose item must be a mapping")
                    continue
                _validate_conditions(
                    choice.get("conditions"),
                    report,
                    path_prefix=f"{path}.choose[{j}].conditions",
                )
                _validate_actions(
                    choice.get("sequence"),
                    report,
                    path_prefix=f"{path}.choose[{j}].sequence",
                )
            if act.get("default"):
                _validate_actions(act["default"], report, path_prefix=f"{path}.default")
        elif "if" in act:
            _validate_conditions(act.get("if"), report, path_prefix=f"{path}.if")
            _validate_actions(act.get("then"), report, path_prefix=f"{path}.then")
            if act.get("else"):
                _validate_actions(act["else"], report, path_prefix=f"{path}.else")
        elif "repeat" in act:
            repeat = act["repeat"]
            if isinstance(repeat, dict):
                _validate_actions(
                    repeat.get("sequence"), report, path_prefix=f"{path}.repeat.sequence"
                )
        else:
            service = act.get("service") or act.get("action")
            if service is not None and not isinstance(service, str):
                report.error(f"{path}.service", "service must be a string")
            elif isinstance(service, str) and "." not in service:
                report.error(
                    f"{path}.service",
                    f"service must be 'domain.name', got {service!r}",
                )
