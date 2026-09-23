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

"""Tests for the write→reload→verify→rollback pipeline.

A FakeClient simulates HA: records every send_command, can be
programmed to succeed or fail on reload / verify. The pipeline is
exercised end-to-end with a real filesystem (tmp_path).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from mylo.files import rollback as rollback_module
from mylo.files.rollback import (
    apply_optimistic_reload_all,
    apply_with_rollback,
    automation_loaded_verifier,
)
from mylo.files.verifications import VerificationLog


class _FakeClient:
    def __init__(
        self,
        *,
        states_after: list[dict[str, Any]] | None = None,
        reload_raises: Exception | None = None,
    ) -> None:
        self._states = states_after or []
        self._reload_raises = reload_raises
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def send_command(self, type_: str, **kwargs: Any) -> Any:
        self.calls.append((type_, kwargs))
        if type_ == "call_service" and self._reload_raises is not None:
            raise self._reload_raises
        if type_ == "get_states":
            return self._states
        return None


async def test_apply_creates_file_and_reloads(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    target = config_dir / "pkg.yaml"
    mylo_dir = tmp_path / ".mylo"

    client = _FakeClient()
    result = await apply_with_rollback(
        client=client,  # type: ignore[arg-type]
        path=target,
        content="automation: []\n",
        domain="automation",
        config_dir=config_dir,
        mylo_data_dir=mylo_dir,
        verify=None,
        reload_wait_seconds=0,
    )
    assert result.ok
    assert target.read_text() == "automation: []\n"
    assert any(
        type_ == "call_service"
        and kwargs.get("domain") == "automation"
        and kwargs.get("service") == "reload"
        for type_, kwargs in client.calls
    )


async def test_verify_failure_rolls_back(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    target = config_dir / "pkg.yaml"
    target.write_text("original: true\n")
    mylo_dir = tmp_path / ".mylo"

    # Verifier that fails — simulates an automation that didn't load.
    async def failing_verify(_client: Any) -> tuple[bool, str, dict[str, Any]]:
        return False, "entity not present after reload", {}

    client = _FakeClient()
    result = await apply_with_rollback(
        client=client,  # type: ignore[arg-type]
        path=target,
        content="new: content\n",
        domain="automation",
        config_dir=config_dir,
        mylo_data_dir=mylo_dir,
        verify=failing_verify,
        reload_wait_seconds=0,
    )
    assert not result.ok
    assert result.rolled_back
    # Original content restored.
    assert target.read_text() == "original: true\n"
    # Two reload calls: the initial one, then the rollback reload.
    reloads = [c for c in client.calls if c[0] == "call_service"]
    assert len(reloads) == 2


async def test_first_write_rollback_deletes_file(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    target = config_dir / "pkg.yaml"  # doesn't exist yet
    mylo_dir = tmp_path / ".mylo"

    async def failing_verify(_client: Any) -> tuple[bool, str, dict[str, Any]]:
        return False, "not loaded", {}

    client = _FakeClient()
    result = await apply_with_rollback(
        client=client,  # type: ignore[arg-type]
        path=target,
        content="new: content\n",
        domain="automation",
        config_dir=config_dir,
        mylo_data_dir=mylo_dir,
        verify=failing_verify,
        reload_wait_seconds=0,
    )
    assert not result.ok
    assert result.rolled_back
    assert not target.exists()  # rollback = delete


async def test_reload_failure_rolls_back(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    target = config_dir / "pkg.yaml"
    target.write_text("original: true\n")
    mylo_dir = tmp_path / ".mylo"

    from mylo.ha.ws_client import CommandError

    client = _FakeClient(reload_raises=CommandError("reload_failed", "HA said no"))
    result = await apply_with_rollback(
        client=client,  # type: ignore[arg-type]
        path=target,
        content="new: content\n",
        domain="automation",
        config_dir=config_dir,
        mylo_data_dir=mylo_dir,
        verify=None,
        reload_wait_seconds=0,
    )
    assert not result.ok
    # File was written but rollback should have reverted it.
    assert target.read_text() == "original: true\n"
    assert result.rolled_back


async def test_automation_loaded_verifier_passes_when_present(tmp_path: Path) -> None:
    states = [{"entity_id": "automation.my_auto", "state": "on"}]
    client = _FakeClient(states_after=states)
    verify = automation_loaded_verifier("automation.my_auto")
    ok, _message, details = await verify(client)  # type: ignore[arg-type]
    assert ok
    assert details["state"] == "on"


async def test_automation_loaded_verifier_fails_when_missing(tmp_path: Path) -> None:
    client = _FakeClient(states_after=[{"entity_id": "automation.other", "state": "on"}])
    verify = automation_loaded_verifier("automation.my_auto")
    ok, message, _ = await verify(client)  # type: ignore[arg-type]
    assert not ok
    assert "not present" in message


async def test_automation_by_config_id_verifier_matches_on_attribute(tmp_path: Path) -> None:
    """HA names the entity after the alias slug, not the config id. The
    verifier must find the entity by attributes.id regardless of slug."""
    from mylo.files.rollback import automation_by_config_id_verifier

    states = [
        {
            "entity_id": "automation.berkley_room_light_stoplight_schedule",
            "state": "on",
            "attributes": {"id": "mylo_berkley_room_light_stoplight_schedule"},
        }
    ]
    client = _FakeClient(states_after=states)
    verify = automation_by_config_id_verifier("mylo_berkley_room_light_stoplight_schedule")
    ok, message, details = await verify(client)  # type: ignore[arg-type]
    assert ok
    assert details["entity_id"] == "automation.berkley_room_light_stoplight_schedule"
    assert details["state"] == "on"
    assert "loaded" in message


async def test_automation_by_config_id_verifier_fails_when_absent(tmp_path: Path) -> None:
    from mylo.files.rollback import automation_by_config_id_verifier

    states = [
        {"entity_id": "automation.other", "state": "on", "attributes": {"id": "other_id"}},
        {"entity_id": "light.kitchen", "state": "on", "attributes": {"id": "mylo_x"}},
    ]
    client = _FakeClient(states_after=states)
    verify = automation_by_config_id_verifier("mylo_x")
    ok, message, _ = await verify(client)  # type: ignore[arg-type]
    assert not ok
    assert "mylo_x" in message


async def test_automation_by_config_id_verifier_rejects_unavailable(tmp_path: Path) -> None:
    from mylo.files.rollback import automation_by_config_id_verifier

    states = [
        {"entity_id": "automation.a", "state": "unavailable", "attributes": {"id": "mylo_a"}},
    ]
    client = _FakeClient(states_after=states)
    verify = automation_by_config_id_verifier("mylo_a")
    ok, message, details = await verify(client)  # type: ignore[arg-type]
    assert not ok
    assert "unavailable" in message
    assert details["entity_id"] == "automation.a"


class _ReadyClient(_FakeClient):
    async def wait_ready(self, timeout: float) -> None:  # noqa: ASYNC109 - mirrors HaWsClient API
        return None


async def _drain_background() -> None:
    if rollback_module._BACKGROUND_TASKS:
        await asyncio.gather(*rollback_module._BACKGROUND_TASKS)


async def test_optimistic_records_pending_then_verified(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    log = VerificationLog(tmp_path / "v.json")

    async def _ok_verify(_client: Any) -> tuple[bool, str, dict[str, Any]]:
        return True, "present", {}

    result = await apply_optimistic_reload_all(
        client=_ReadyClient(),  # type: ignore[arg-type]
        path=config_dir / "pkg.yaml",
        content="automation: []\n",
        config_dir=config_dir,
        mylo_data_dir=tmp_path / ".mylo",
        verify=_ok_verify,
        reload_wait_seconds=0,
        tool_name="modify_automation",
        verifications=log,
        target="automation porch",
    )
    assert result.ok and result.verification == "pending"
    assert result.verification_id is not None
    assert result.to_dict()["verification"] == "pending"
    assert log.get(result.verification_id).status == "pending"  # type: ignore[union-attr]
    await _drain_background()
    done = log.get(result.verification_id)
    assert done is not None and done.status == "verified" and done.message == "present"
    assert done.target == "automation porch" and done.tool == "modify_automation"


async def test_optimistic_records_failed_verify(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    log = VerificationLog(tmp_path / "v.json")

    async def _bad_verify(_client: Any) -> tuple[bool, str, dict[str, Any]]:
        return False, "entity missing", {}

    result = await apply_optimistic_reload_all(
        client=_ReadyClient(),  # type: ignore[arg-type]
        path=config_dir / "pkg.yaml",
        content="x: 1\n",
        config_dir=config_dir,
        mylo_data_dir=tmp_path / ".mylo",
        verify=_bad_verify,
        reload_wait_seconds=0,
        verifications=log,
    )
    await _drain_background()
    done = log.get(result.verification_id or "")
    assert done is not None and done.status == "failed" and "entity missing" in done.message
    assert done.target == "pkg.yaml"


async def test_synchronous_path_reports_synchronous(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    result = await apply_with_rollback(
        client=_FakeClient(),  # type: ignore[arg-type]
        path=config_dir / "pkg.yaml",
        content="automation: []\n",
        domain="automation",
        config_dir=config_dir,
        mylo_data_dir=tmp_path / ".mylo",
        verify=None,
        reload_wait_seconds=0,
    )
    assert result.verification == "synchronous" and result.verification_id is None


async def test_optimistic_write_failure_reports_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that raises before the reload even fires must not leave
    verification stuck at "pending" — the log entry is already completed
    "failed", so RollbackResult.verification must agree."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    log = VerificationLog(tmp_path / "v.json")

    def _raise(_path: Path, _content: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(rollback_module, "atomic_write", _raise)

    result = await apply_optimistic_reload_all(
        client=_ReadyClient(),  # type: ignore[arg-type]
        path=config_dir / "pkg.yaml",
        content="x: 1\n",
        config_dir=config_dir,
        mylo_data_dir=tmp_path / ".mylo",
        verify=None,
        reload_wait_seconds=0,
        verifications=log,
    )
    assert not result.ok
    assert result.verification == "failed"
    done = log.get(result.verification_id or "")
    assert done is not None and done.status == "failed" and "disk full" in done.message
