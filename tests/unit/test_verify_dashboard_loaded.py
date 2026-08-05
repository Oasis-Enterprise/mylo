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

"""verify_change check_type='dashboard_loaded'.

Targets are '<dashboard_id>:<view_path>' or a bare '<view_path>' for
the default dashboard. A view is loaded when it exists in the fetched
config and (for sections views) every section carries a cards list.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mylo.ha.registries import Registries
from mylo.ha.ws_client import CommandError
from mylo.tools import registry as tool_registry
from mylo.tools.executor import execute
from tests.unit._helpers import make_ctx


class _FakeClient:
    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._responses = responses or {}

    async def send_command(self, type_: str, **kwargs: Any) -> Any:
        self.calls.append((type_, kwargs))
        if type_ in self._responses:
            val = self._responses[type_]
            if callable(val):
                return val(**kwargs)
            if isinstance(val, Exception):
                raise val
            return val
        return {}


_DEFAULT_DASHBOARD = {
    "views": [
        {"path": "home", "title": "Home", "cards": []},
        {
            "path": "rooms",
            "title": "Rooms",
            "type": "sections",
            "sections": [{"type": "grid", "cards": [{"type": "heading", "heading": "K"}]}],
        },
    ]
}


@pytest.fixture(autouse=True)
def _load_tools():
    from mylo.tools import executor

    tool_registry._reset_for_tests()
    tool_registry.load_all()
    executor._result_cache.clear()
    yield
    tool_registry._reset_for_tests()
    executor._result_cache.clear()


def _ctx(tmp_path: Path, responses: dict[str, Any]):
    return make_ctx(
        ws_client=_FakeClient(responses), registries=Registries(), tmp_path=tmp_path
    )


async def test_dashboard_loaded_view_present(tmp_path):
    ctx = _ctx(tmp_path, {"lovelace/config": _DEFAULT_DASHBOARD})
    result = await execute(
        "verify_change",
        {"check_type": "dashboard_loaded", "targets": ["rooms"], "wait_seconds": 0},
        ctx,
    )
    assert result.status.value == "ok", result
    assert result.data["all_ok"] is True
    assert result.data["results"]["rooms"]["ok"] is True
    assert result.data["results"]["rooms"]["layout"] == "sections"


async def test_dashboard_loaded_missing_view(tmp_path):
    ctx = _ctx(tmp_path, {"lovelace/config": _DEFAULT_DASHBOARD})
    result = await execute(
        "verify_change",
        {"check_type": "dashboard_loaded", "targets": ["nope"], "wait_seconds": 0},
        ctx,
    )
    assert result.status.value == "ok", result
    assert result.data["all_ok"] is False
    assert result.data["results"]["nope"]["ok"] is False


async def test_dashboard_loaded_scoped_dashboard_id(tmp_path):
    def config_response(**kwargs: Any) -> Any:
        assert kwargs.get("url_path") == "mobile"
        return {"views": [{"path": "main", "title": "Main", "cards": []}]}

    ctx = _ctx(tmp_path, {"lovelace/config": config_response})
    result = await execute(
        "verify_change",
        {"check_type": "dashboard_loaded", "targets": ["mobile:main"], "wait_seconds": 0},
        ctx,
    )
    assert result.status.value == "ok", result
    assert result.data["results"]["mobile:main"]["ok"] is True


async def test_dashboard_loaded_fetch_failure_reported(tmp_path):
    ctx = _ctx(tmp_path, {"lovelace/config": CommandError("config_not_found", "no")})
    result = await execute(
        "verify_change",
        {"check_type": "dashboard_loaded", "targets": ["home"], "wait_seconds": 0},
        ctx,
    )
    assert result.status.value == "ok", result
    assert result.data["all_ok"] is False
    assert "reason" in result.data["results"]["home"]


async def test_dashboard_loaded_sections_view_missing_cards_flagged(tmp_path):
    broken = {
        "views": [
            {
                "path": "rooms",
                "type": "sections",
                "sections": [{"type": "grid"}],
            }
        ]
    }
    ctx = _ctx(tmp_path, {"lovelace/config": broken})
    result = await execute(
        "verify_change",
        {"check_type": "dashboard_loaded", "targets": ["rooms"], "wait_seconds": 0},
        ctx,
    )
    assert result.status.value == "ok", result
    assert result.data["results"]["rooms"]["ok"] is False
