"""Write → reload → verify → rollback pipeline.

Every tier-2 write-and-apply goes through :func:`apply_with_rollback`.
The pipeline:

1. **Backup** the current file (if it exists).
2. **Write** the new content atomically.
3. **Reload** the affected HA domain.
4. **Verify** — domain-specific check that the change took effect.
5. On verify failure: **restore** the backup, reload again, report.

Each step's outcome shows up in the returned :class:`RollbackResult` so
the calling tool can shape a friendly ToolResult.error envelope with a
diagnosis (e.g. "entity doesn't exist after reload, rolled back").

The reload step uses a small mapping of domain → service. Extending to a
new domain is two lines: a :data:`RELOAD_SERVICES` entry and (optionally)
a :class:`Verifier`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from mylo.files.backup import BackupHandle, take_backup
from mylo.files.manager import atomic_write
from mylo.ha.ws_client import CommandError, CommandTimeout, HaWsClient
from mylo.logging_setup import get_logger

log = get_logger(__name__)


StepName = Literal["backup", "write", "reload", "verify", "rollback", "rollback_reload"]


@dataclass(slots=True)
class StepResult:
    step: StepName
    ok: bool
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RollbackResult:
    ok: bool
    steps: list[StepResult] = field(default_factory=list)
    backup_path: str | None = None
    rolled_back: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "rolled_back": self.rolled_back,
            "backup_path": self.backup_path,
            "steps": [
                {
                    "step": s.step,
                    "ok": s.ok,
                    "message": s.message,
                    **({"details": s.details} if s.details else {}),
                }
                for s in self.steps
            ],
        }


# Verifier signature. Returns (ok, message, details). If ok=False the
# pipeline rolls back. Details are surfaced in the result.
Verifier = Callable[[HaWsClient], Awaitable[tuple[bool, str, dict[str, Any]]]]


RELOAD_SERVICES: dict[str, tuple[str, str]] = {
    # domain → (service_domain, service_name)
    "automation": ("homeassistant", "reload_automation"),
    "script": ("homeassistant", "reload_script"),
    "scene": ("homeassistant", "reload_scene"),
    "group": ("homeassistant", "reload_group"),
    "input_boolean": ("homeassistant", "reload_input_boolean"),
    "input_number": ("homeassistant", "reload_input_number"),
    "input_select": ("homeassistant", "reload_input_select"),
    "input_text": ("homeassistant", "reload_input_text"),
    "input_datetime": ("homeassistant", "reload_input_datetime"),
    "template": ("homeassistant", "reload_template"),
    "core": ("homeassistant", "reload_core_config"),
    "lovelace": ("lovelace", "reload_resources"),
    # Fallback — reloads most YAML-configured components.
    "all": ("homeassistant", "reload_all"),
}


async def apply_with_rollback(
    *,
    client: HaWsClient,
    path: Path,
    content: str,
    domain: str,
    config_dir: Path,
    mylo_data_dir: Path,
    verify: Verifier | None = None,
    reload_wait_seconds: float = 10.0,
) -> RollbackResult:
    """Execute the full write pipeline.

    ``domain`` selects the reload service from :data:`RELOAD_SERVICES`.
    ``verify`` is the domain-specific check; use
    :func:`automation_loaded_verifier` for automations or write your own.

    ``reload_wait_seconds`` is deliberately long for slow Pi hardware
    (M4b plan called for 15s on dashboards specifically; 10s is a sane
    default for everything else and a tool can pass a larger value for
    dashboard reloads).
    """
    result = RollbackResult(ok=False)

    # 1. Backup.
    backup: BackupHandle = take_backup(path, config_dir, mylo_data_dir)
    result.backup_path = str(backup.backup_path) if backup.backup_path else None
    result.steps.append(
        StepResult(
            "backup",
            ok=True,
            message=(
                f"backup saved to {backup.backup_path}"
                if backup.backup_path
                else "no existing file — nothing to back up"
            ),
        )
    )

    # 2. Write.
    try:
        atomic_write(path, content)
        result.steps.append(StepResult("write", ok=True, message=str(path)))
    except Exception as exc:
        result.steps.append(StepResult("write", ok=False, message=f"{type(exc).__name__}: {exc}"))
        return result

    # 3. Reload.
    #
    # homeassistant.reload_all restarts enough of HA core (including the
    # ingress integration) that our own websocket gets reset mid-call —
    # the call_service request never receives a response. That's not a
    # failure; it just means HA accepted the reload and the transport
    # rolled. We use a short timeout in that case so we don't sit 60s
    # waiting for a reply that can't come, then wait for reconnect
    # before verifying.
    reload_service = RELOAD_SERVICES.get(domain, RELOAD_SERVICES["all"])
    is_global_reload = domain == "all"
    call_timeout = 5.0 if is_global_reload else 60.0
    reload_message = f"{reload_service[0]}.{reload_service[1]}"
    try:
        await client.send_command(
            "call_service",
            domain=reload_service[0],
            service=reload_service[1],
            timeout=call_timeout,
        )
        result.steps.append(StepResult("reload", ok=True, message=reload_message))
    except CommandTimeout:
        if not is_global_reload:
            result.steps.append(
                StepResult("reload", ok=False, message=f"{reload_message}: timed out")
            )
            await _rollback(client, path, backup, reload_service, result)
            return result
        # reload_all's websocket-reset is the expected signal that HA is
        # processing the reload. Keep going and verify after reconnect.
        result.steps.append(
            StepResult(
                "reload",
                ok=True,
                message=f"{reload_message} (websocket reset — reload in progress)",
            )
        )
    except CommandError as exc:
        result.steps.append(
            StepResult("reload", ok=False, message=f"{exc.code}: {exc.message}")
        )
        await _rollback(client, path, backup, reload_service, result)
        return result

    # For a global reload, wait for our client to reconnect before
    # verifying — the call likely tore the websocket.
    if is_global_reload:
        try:
            await client.wait_ready(timeout=30.0)
        except TimeoutError:
            result.steps.append(
                StepResult(
                    "verify",
                    ok=False,
                    message="websocket never reconnected after reload_all",
                )
            )
            # Don't rollback: without a working client we can't verify
            # OR revert safely. The file stays; user reconciles.
            return result

    # Wait for HA to process the reload before verifying. Reload calls
    # return immediately but the domain takes a moment to pick up new
    # entities.
    if reload_wait_seconds > 0:
        await asyncio.sleep(reload_wait_seconds)

    # 4. Verify.
    if verify is not None:
        try:
            ok, message, details = await verify(client)
        except Exception as exc:
            ok = False
            message = f"verifier raised {type(exc).__name__}: {exc}"
            details = {}
        result.steps.append(StepResult("verify", ok=ok, message=message, details=details))
        if not ok:
            await _rollback(client, path, backup, reload_service, result)
            return result
    else:
        result.steps.append(StepResult("verify", ok=True, message="no verifier"))

    result.ok = True
    return result


async def _rollback(
    client: HaWsClient,
    path: Path,
    backup: BackupHandle,
    reload_service: tuple[str, str],
    result: RollbackResult,
) -> None:
    """Restore the backup and reload. Records both steps in ``result``."""
    if backup.backup_path is None:
        # First-write case — restoration means deleting the file we
        # wrote. pathlib's sync unlink is a single syscall on a local
        # filesystem; fine in an async handler.
        try:
            path.unlink(missing_ok=True)  # noqa: ASYNC240
            result.steps.append(
                StepResult(
                    "rollback",
                    ok=True,
                    message=f"deleted {path} (no prior version existed)",
                )
            )
            result.rolled_back = True
        except Exception as exc:
            result.steps.append(
                StepResult("rollback", ok=False, message=f"{type(exc).__name__}: {exc}")
            )
            return
    else:
        try:
            atomic_write(path, backup.backup_path.read_text(encoding="utf-8"))
            result.steps.append(
                StepResult(
                    "rollback",
                    ok=True,
                    message=f"restored from {backup.backup_path}",
                )
            )
            result.rolled_back = True
        except Exception as exc:
            result.steps.append(
                StepResult("rollback", ok=False, message=f"{type(exc).__name__}: {exc}")
            )
            return

    try:
        await client.send_command(
            "call_service",
            domain=reload_service[0],
            service=reload_service[1],
        )
        result.steps.append(
            StepResult(
                "rollback_reload",
                ok=True,
                message=f"{reload_service[0]}.{reload_service[1]}",
            )
        )
    except CommandError as exc:
        result.steps.append(
            StepResult(
                "rollback_reload",
                ok=False,
                message=(f"rollback applied to disk but reload failed: {exc.code}: {exc.message}"),
            )
        )


# ─── Verifiers ───────────────────────────────────────────────────────────────


def automation_loaded_verifier(entity_id: str) -> Verifier:
    """Verify an automation entity exists and is in a healthy state.

    HA's automation domain exposes each automation as an ``automation.*``
    entity whose state is ``on`` (enabled) or ``off`` (disabled). A
    missing entity after a reload usually means a schema error HA logged
    but we didn't catch pre-write, or the automation file wasn't picked
    up (e.g. a newly-added package that requires reload_all instead of
    reload_automation).
    """

    async def _verify(client: HaWsClient) -> tuple[bool, str, dict[str, Any]]:
        try:
            states = await client.send_command("get_states")
        except CommandError as exc:
            return False, f"{exc.code}: {exc.message}", {}

        if not isinstance(states, list):
            return False, "could not fetch states", {}

        match = next(
            (s for s in states if isinstance(s, dict) and s.get("entity_id") == entity_id),
            None,
        )
        if match is None:
            return False, f"{entity_id} not present after reload", {}

        current = match.get("state")
        if current in ("on", "off"):
            return True, f"loaded (state={current})", {"state": current}
        return False, f"entity present but state is {current!r}", {"state": current}

    return _verify


def entity_exists_verifier(entity_id: str) -> Verifier:
    """Looser verifier: just check the entity appears in get_states."""

    async def _verify(client: HaWsClient) -> tuple[bool, str, dict[str, Any]]:
        states = await client.send_command("get_states")
        if not isinstance(states, list):
            return False, "could not fetch states", {}
        present = any(isinstance(s, dict) and s.get("entity_id") == entity_id for s in states)
        return (
            (True, f"{entity_id} present", {})
            if present
            else (False, f"{entity_id} not present after reload", {})
        )

    return _verify
