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

"""Plain-language status labels for the live turn indicator.

The panel shows these while a tool runs ("Reading your automations")
instead of the raw tool name. Every registered tool must have an entry
— the test suite enforces it — so the fallback only exists for tools
registered in tests.
"""

from __future__ import annotations

THINKING_LABEL = "Thinking"
CANCELLED_LABEL = "Stopped"
FALLBACK_LABEL = "Working"

_LABELS: dict[str, str] = {
    "query_entities": "Looking at your devices",
    "query_devices": "Looking at your devices",
    "query_automations": "Reading your automations",
    "query_dashboard": "Reading the dashboard",
    "query_dashboard_env": "Checking themes and cards",
    "plan_dashboard": "Planning the dashboard change",
    "stage_custom_card": "Writing the card",
    "read_custom_card": "Reading the card",
    "apply_dashboard_plan": "Updating the dashboard",
    "apply_custom_card": "Installing the card",
    "query_logs": "Checking the logs",
    "query_history": "Checking history",
    "query_traces": "Checking automation runs",
    "query_system": "Checking system health",
    "read_config_file": "Reading configuration",
    "write_config_file": "Updating configuration",
    "patch_config_file": "Updating configuration",
    "modify_automation": "Updating the automation",
    "modify_script": "Updating the script",
    "modify_scene": "Updating the scene",
    "modify_zones": "Updating zones",
    "modify_areas": "Updating areas",
    "rename_entities": "Renaming",
    "manage_helpers": "Updating helpers",
    "manage_labels": "Updating labels",
    "manage_monitored": "Updating monitoring",
    "manage_notification_filters": "Updating alert filters",
    "call_service": "Controlling a device",
    "reload_config": "Reloading Home Assistant",
    "verify_change": "Verifying the change",
    "memory_note": "Taking a note",
    "ask_user": "Asking you a question",
}


def label_for(tool_name: str) -> str:
    """Return the label for ``tool_name``, or :data:`FALLBACK_LABEL`."""
    return _LABELS.get(tool_name, FALLBACK_LABEL)
