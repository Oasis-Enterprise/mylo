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
