"""Tests for the write→reload→verify→rollback pipeline.

A FakeClient simulates HA: records every send_command, can be
programmed to succeed or fail on reload / verify. The pipeline is
exercised end-to-end with a real filesystem (tmp_path).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mylo.files.rollback import apply_with_rollback, automation_loaded_verifier


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
    # Loose match — the reload step passes timeout= among its kwargs.
    assert any(
        type_ == "call_service"
        and kwargs.get("domain") == "homeassistant"
        and kwargs.get("service") == "reload_automation"
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
