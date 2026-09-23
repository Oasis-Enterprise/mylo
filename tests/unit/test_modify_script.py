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

"""Tests for modify_script's apply-failed envelope.

Regression coverage for Trust Task 2 fix round 1: the apply_failed error
branch must carry the same top-level verification/verification_id/
backup_path keys as the success branch, not just nested under
data["rollback"].
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mylo.files.rollback import RollbackResult
from mylo.ha.registries import Registries
from mylo.tools import registry as tool_registry
from mylo.tools.context import ToolContext
from mylo.tools.executor import execute
from tests.unit._helpers import make_ctx


class _FakeClient:
    async def send_command(self, type_: str, **kwargs: Any) -> Any:
        return None


@pytest.fixture
def _ctx(tmp_path: Path) -> ToolContext:
    (tmp_path / "config").mkdir()
    return make_ctx(
        ws_client=_FakeClient(),
        registries=Registries(),
        tmp_path=tmp_path / "config",
        user_approved=True,
    )


@pytest.fixture(autouse=True)
def _load_tools() -> Any:
    tool_registry._reset_for_tests()
    tool_registry.load_all()
    yield
    tool_registry._reset_for_tests()


async def test_apply_failed_envelope_carries_verification_fields(
    _ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """apply_optimistic_reload_all reporting failure must still surface
    top-level verification/verification_id/backup_path in the error data,
    mirroring the success envelope's shape."""
    import mylo.tools.write.modify_script as modify_script

    async def _fake_apply(**_kwargs: Any) -> RollbackResult:
        return RollbackResult(
            ok=False, verification="failed", verification_id="vf_x", backup_path=None
        )

    monkeypatch.setattr(modify_script, "apply_optimistic_reload_all", _fake_apply)

    result = await execute(
        "modify_script",
        {
            "action": "create",
            "config": {"alias": "Test Script", "sequence": []},
            "dry_run": False,
        },
        _ctx,
    )
    assert result.error_code == "apply_failed"
    assert result.data["verification"] == "failed"
    assert result.data["verification_id"] == "vf_x"
    assert result.data["backup_path"] is None
