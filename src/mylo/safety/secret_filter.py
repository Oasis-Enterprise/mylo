"""Replace ``!secret`` references with safe placeholders.

Spec §5.3. The agent must understand that a secret exists and its name
without ever seeing the value. ``!secret wifi_password`` →
``[SECRET:wifi_password]``.
"""

from __future__ import annotations

import re

_PATTERN = re.compile(r"!secret\s+([A-Za-z0-9_\-.]+)")


def sanitize_yaml_secrets(content: str) -> str:
    return _PATTERN.sub(lambda m: f"[SECRET:{m.group(1)}]", content)
