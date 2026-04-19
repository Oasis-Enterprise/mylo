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

"""Tests for apply_optimistic_reload_all and its background verifier.

The tool returns immediately after a successful write; verification
happens in a spawned task. Tests drive the background task by awaiting
it directly (we capture it from the tasks set rollback.py keeps).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from mylo.files.rollback import (
    _BACKGROUND_TASKS,
    apply_optimistic_reload_all,
)
from mylo.ha.ws_client import CommandTimeout
from mylo.safety.audit import AuditLogger


class _FakeClient:
    """Stand-in for HaWsClient. Programmable per-test so we can simulate
    reload_all-tearing-the-socket, reconnect timing, and verify outcomes.
    """

    def __init__(
        self,
        *,
        reload_raises: Exception | None = None,
        reconnect_immediate: bool = True,
        states_after: list[dict[str, Any]] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._reload_raises = reload_raises
        self._reconnect_immediate = reconnect_immediate
        self._states = states_after or []

    async def send_command(self, type_: str, **kwargs: Any) -> Any:
        self.calls.append((type_, kwargs))
        if (
            type_ == "call_service"
            and kwargs.get("domain") == "homeassistant"
            and kwargs.get("service") == "reload_all"
            and self._reload_raises is not None
        ):
            raise self._reload_raises
        if type_ == "get_states":
            return self._states
        return None

    async def wait_ready(
        self,
        timeout: float | None = None,  # noqa: ASYNC109 - mirrors HaWsClient API
    ) -> None:
        if not self._reconnect_immediate:
            raise TimeoutError(f"never reconnected within {timeout}s")
        return None


async def _drain_background_tasks() -> None:
    pending = list(_BACKGROUND_TASKS)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def test_returns_immediately_with_pending_verify(tmp_path: Path) -> None:
    client = _FakeClient(
        reload_raises=CommandTimeout("call_service", 5.0),
        states_after=[{"entity_id": "automation.target", "state": "on"}],
    )
    target = tmp_path / "config" / "packages" / "agent.yaml"

    from mylo.files.rollback import automation_loaded_verifier

    result = await apply_optimistic_reload_all(
        client=client,  # type: ignore[arg-type]
        path=target,
        content="automation: []\n",
        config_dir=tmp_path / "config",
        mylo_data_dir=tmp_path / ".mylo",
        verify=automation_loaded_verifier("automation.target"),
        reload_wait_seconds=0,
    )

    # The synchronous response is optimistic: ok=True, file written,
    # verify status is "pending — background ...".
    assert result.ok
    assert target.read_text() == "automation: []\n"
    verify_step = next(s for s in result.steps if s.step == "verify")
    assert "pending" in verify_step.message.lower()

    await _drain_background_tasks()


async def test_background_success_writes_audit(tmp_path: Path) -> None:
    client = _FakeClient(
        reload_raises=CommandTimeout("call_service", 5.0),
        states_after=[{"entity_id": "automation.target", "state": "on"}],
    )
    audit = AuditLogger(tmp_path / ".mylo")
    target = tmp_path / "config" / "packages" / "agent.yaml"

    from mylo.files.rollback import automation_loaded_verifier

    await apply_optimistic_reload_all(
        client=client,  # type: ignore[arg-type]
        path=target,
        content="automation: []\n",
        config_dir=tmp_path / "config",
        mylo_data_dir=tmp_path / ".mylo",
        verify=automation_loaded_verifier("automation.target"),
        reload_wait_seconds=0,
        audit=audit,
        tool_name="modify_automation",
    )
    await _drain_background_tasks()

    rows = audit.read_recent(limit=10)
    assert any(r["tool_name"] == "modify_automation" and r["result"] == "success" for r in rows)


async def test_background_failure_writes_audit_and_notifies(
    tmp_path: Path,
) -> None:
    # Verifier will fail because the target entity isn't in the states.
    client = _FakeClient(
        reload_raises=CommandTimeout("call_service", 5.0),
        states_after=[{"entity_id": "automation.something_else", "state": "on"}],
    )
    audit = AuditLogger(tmp_path / ".mylo")
    target = tmp_path / "config" / "packages" / "agent.yaml"

    from mylo.files.rollback import automation_loaded_verifier

    await apply_optimistic_reload_all(
        client=client,  # type: ignore[arg-type]
        path=target,
        content="automation: []\n",
        config_dir=tmp_path / "config",
        mylo_data_dir=tmp_path / ".mylo",
        verify=automation_loaded_verifier("automation.target"),
        reload_wait_seconds=0,
        audit=audit,
        tool_name="modify_automation",
    )
    await _drain_background_tasks()

    rows = audit.read_recent(limit=10)
    assert any(r["tool_name"] == "modify_automation" and r["result"] == "failure" for r in rows)
    # persistent_notification.create should have been called.
    assert any(
        type_ == "call_service"
        and kw.get("domain") == "persistent_notification"
        and kw.get("service") == "create"
        for type_, kw in client.calls
    )


async def test_reconnect_timeout_records_failure(tmp_path: Path) -> None:
    client = _FakeClient(
        reload_raises=CommandTimeout("call_service", 5.0),
        reconnect_immediate=False,
    )
    audit = AuditLogger(tmp_path / ".mylo")
    target = tmp_path / "config" / "packages" / "agent.yaml"

    from mylo.files.rollback import automation_loaded_verifier

    await apply_optimistic_reload_all(
        client=client,  # type: ignore[arg-type]
        path=target,
        content="automation: []\n",
        config_dir=tmp_path / "config",
        mylo_data_dir=tmp_path / ".mylo",
        verify=automation_loaded_verifier("automation.target"),
        reload_wait_seconds=0,
        audit=audit,
        tool_name="modify_automation",
    )
    await _drain_background_tasks()

    rows = audit.read_recent(limit=10)
    assert any("never reconnected" in str(r) for r in rows)
