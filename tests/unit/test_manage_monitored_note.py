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

"""``manage_monitored`` — the add/replace result carries a plain-language
note about the monitoring system's learning period; ``list`` does not.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mylo.ha.registries import EntityEntry, Registries
from mylo.tools import registry as tool_registry
from mylo.tools.context import ToolContext
from mylo.tools.executor import execute
from mylo.tools.write.manage_monitored import MONITORING_NOTE
from tests.unit._helpers import make_ctx


class _FakeClient:
    async def send_command(self, type_: str, **_: Any) -> Any:
        raise AssertionError(f"unexpected command {type_!r}")


@pytest.fixture(autouse=True)
def _load_tool() -> Any:
    tool_registry._reset_for_tests()
    tool_registry.load_all()
    yield
    tool_registry._reset_for_tests()


def _ctx(tmp_path: Path) -> ToolContext:
    reg = Registries()
    reg.entities = {
        "sensor.garage_temp": EntityEntry.from_raw(
            {
                "entity_id": "sensor.garage_temp",
                "original_name": "Garage Temperature",
                "platform": "esphome",
                "area_id": None,
                "labels": [],
            }
        ),
    }
    return make_ctx(
        ws_client=_FakeClient(),
        registries=reg,
        tmp_path=tmp_path,
        user_approved=True,
    )


async def test_add_result_carries_the_monitoring_note(tmp_path: Path) -> None:
    result = await execute(
        "manage_monitored",
        {"action": "add", "entity_ids": ["sensor.garage_temp"]},
        _ctx(tmp_path),
    )
    assert result.status.value == "ok"
    assert result.data["note"] == MONITORING_NOTE


async def test_list_result_has_no_note(tmp_path: Path) -> None:
    result = await execute("manage_monitored", {"action": "list"}, _ctx(tmp_path))
    assert result.status.value == "ok"
    assert "note" not in result.data
