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

"""Tool descriptions describe mechanics and stay short; policy lives in the prompt."""

from __future__ import annotations

from mylo.tools import registry

POLICY_PHRASES = ("ALWAYS", "never guess", "Check the topology", "Only when native")


def test_every_description_is_short_and_mechanical() -> None:
    registry._reset_for_tests()
    registry.load_all()
    try:
        too_long = [
            (t.name, len(t.description)) for t in registry.all_tools() if len(t.description) > 300
        ]
        policy = [
            t.name for t in registry.all_tools() if any(p in t.description for p in POLICY_PHRASES)
        ]
        missing = [t.name for t in registry.all_tools() if not t.description.strip()]
    finally:
        registry._reset_for_tests()
    assert too_long == []
    assert policy == []
    assert missing == []


def test_prompt_version_is_0_9_0() -> None:
    from mylo.context.basic_prompt import load_system_prompt

    assert load_system_prompt().version == "0.9.0"
