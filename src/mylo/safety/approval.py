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

"""Bind user approval to one exact tool call.

A dry-run preview and its later apply are the same call minus ``dry_run``;
hashing the tool name plus canonical parameters gives an id the panel can
hand back with Apply. Tools that mint their own ids (dashboard plans,
custom cards) declare ``approval_key`` instead.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

WILDCARD = "*"


def canonical(params: Mapping[str, Any]) -> str:
    stripped = {k: v for k, v in params.items() if k != "dry_run"}
    return json.dumps(stripped, sort_keys=True, separators=(",", ":"), default=str)


def preview_id(tool_name: str, params: Mapping[str, Any]) -> str:
    digest = hashlib.sha256((tool_name + canonical(params)).encode("utf-8")).hexdigest()
    return "pv_" + digest[:12]
