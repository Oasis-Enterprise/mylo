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
from mylo.safety.audit import AuditLogger, make_entry

log = get_logger(__name__)


# Keep strong references to background verification tasks so they don't
# get garbage-collected mid-flight. aiohttp clears this on shutdown via
# task cancellation.
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()


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
    # Modern HA uses domain-native reload services. The legacy
    # "homeassistant.reload_<domain>" variants were phased out and may
    # not exist in 2024+ versions — every prior hot-path apply failure
    # ("service not found" → rollback) traced to this map pointing at
    # services that don't exist on the user's HA 2026.4.2.
    "automation": ("automation", "reload"),
    "script": ("script", "reload"),
    "scene": ("scene", "reload"),
    "group": ("group", "reload"),
    "input_boolean": ("input_boolean", "reload"),
    "input_number": ("input_number", "reload"),
    "input_select": ("input_select", "reload"),
    "input_text": ("input_text", "reload"),
    "input_datetime": ("input_datetime", "reload"),
    "template": ("template", "reload"),
    "core": ("homeassistant", "reload_core_config"),
    "lovelace": ("lovelace", "reload_resources"),
    # Nuclear option — reloads all YAML-configured components + packages.
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
        result.steps.append(StepResult("reload", ok=False, message=f"{exc.code}: {exc.message}"))
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
            if is_global_reload:
                # HA has already loaded the file (reload_all completed before
                # we got here). Rolling back the file now would DELETE or
                # REVERT it on disk while HA keeps the new automation in
                # memory — creating a drift that only surfaces on the next
                # restart, when the automation silently disappears. Leave
                # the file and surface a warning; the user can inspect.
                result.steps.append(
                    StepResult(
                        "rollback",
                        ok=True,
                        message=(
                            "skipped: reload_all already applied; keeping "
                            "file on disk to stay aligned with HA runtime"
                        ),
                    )
                )
                return result
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


# ─── Optimistic apply (reload_all cold path) ────────────────────────────────


async def apply_optimistic_reload_all(
    *,
    client: HaWsClient,
    path: Path,
    content: str,
    config_dir: Path,
    mylo_data_dir: Path,
    verify: Verifier | None = None,
    reload_wait_seconds: float = 15.0,
    audit: AuditLogger | None = None,
    tool_name: str = "",
    conversation_id: str = "",
    notify_on_failure: bool = True,
) -> RollbackResult:
    """Write synchronously, trigger reload_all, return immediately with an
    optimistic success. Kick off a background task that waits for the
    websocket to reconnect, runs verify, logs the outcome to audit, and
    sends a persistent_notification if verify fails.

    This is the cold-path for first writes to a new package file where
    reload_all is unavoidable and takes 30-45s. The synchronous caller
    doesn't pay that cost; the user sees a response immediately and gets
    a notification later if HA didn't pick the file up correctly.

    :class:`apply_with_rollback` remains the right choice for targeted
    reloads — its rollback semantics are only meaningful when HA is
    known to be in a consistent state.
    """
    result = RollbackResult(ok=True)

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
        result.ok = False
        return result

    # 3. Fire the reload asynchronously and return. The verification
    # task below handles the rest.
    result.steps.append(
        StepResult(
            "reload",
            ok=True,
            message=(
                "homeassistant.reload_all triggered in background — HA may take ~30-45s to settle"
            ),
        )
    )
    result.steps.append(
        StepResult(
            "verify",
            ok=True,
            message=(
                "pending — background task will verify after reload "
                "completes and notify you via HA if it fails"
            ),
        )
    )

    task = asyncio.create_task(
        _background_verify_reload_all(
            client=client,
            path=path,
            verify=verify,
            reload_wait_seconds=reload_wait_seconds,
            audit=audit,
            tool_name=tool_name,
            conversation_id=conversation_id,
            notify_on_failure=notify_on_failure,
        )
    )
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)

    return result


async def _background_verify_reload_all(
    *,
    client: HaWsClient,
    path: Path,
    verify: Verifier | None,
    reload_wait_seconds: float,
    audit: AuditLogger | None,
    tool_name: str,
    conversation_id: str,
    notify_on_failure: bool,
) -> None:
    """Fire reload_all, wait for reconnect, verify, surface outcome."""
    try:
        try:
            await client.send_command(
                "call_service",
                domain="homeassistant",
                service="reload_all",
                timeout=5.0,
            )
        except CommandTimeout:
            # Expected — reload_all resets the websocket.
            pass
        except CommandError as exc:
            await _record_background_failure(
                client,
                audit,
                tool_name,
                conversation_id,
                path,
                f"reload_all call failed: {exc.code}: {exc.message}",
                notify_on_failure,
            )
            return

        try:
            await client.wait_ready(timeout=60.0)
        except TimeoutError:
            await _record_background_failure(
                client,
                audit,
                tool_name,
                conversation_id,
                path,
                "websocket never reconnected after reload_all",
                notify_on_failure,
            )
            return

        if reload_wait_seconds > 0:
            await asyncio.sleep(reload_wait_seconds)

        if verify is None:
            await _record_background_success(
                audit,
                tool_name,
                conversation_id,
                path,
                "reload_all completed (no verifier attached)",
            )
            return

        try:
            ok, message, _details = await verify(client)
        except Exception as exc:
            ok = False
            message = f"verifier raised {type(exc).__name__}: {exc}"

        if ok:
            await _record_background_success(audit, tool_name, conversation_id, path, message)
        else:
            await _record_background_failure(
                client,
                audit,
                tool_name,
                conversation_id,
                path,
                message,
                notify_on_failure,
            )
    except Exception:
        # Any uncaught exception in a background task would otherwise
        # vanish silently. Log and continue.
        log.exception("background_verify.unexpected_error", path=str(path))


async def _record_background_success(
    audit: AuditLogger | None,
    tool_name: str,
    conversation_id: str,
    path: Path,
    message: str,
) -> None:
    log.info("background_verify.ok", path=str(path), message=message, tool_name=tool_name)
    if audit is None:
        return
    entry = make_entry(
        conversation_id=conversation_id,
        tool_name=tool_name or "apply_optimistic_reload_all",
        tier=2,
        params={"path": str(path)},
        dry_run=False,
        user_approved=True,
        result="success",
        details={"background_verify": message},
    )
    try:
        await audit.write(entry)
    except Exception:
        log.exception("background_verify.audit_write_failed")


async def _record_background_failure(
    client: HaWsClient,
    audit: AuditLogger | None,
    tool_name: str,
    conversation_id: str,
    path: Path,
    message: str,
    notify_on_failure: bool,
) -> None:
    log.warning(
        "background_verify.failed",
        path=str(path),
        message=message,
        tool_name=tool_name,
    )
    if audit is not None:
        entry = make_entry(
            conversation_id=conversation_id,
            tool_name=tool_name or "apply_optimistic_reload_all",
            tier=2,
            params={"path": str(path)},
            dry_run=False,
            user_approved=True,
            result="failure",
            details={"background_verify": message},
        )
        try:
            await audit.write(entry)
        except Exception:
            log.exception("background_verify.audit_write_failed")

    if not notify_on_failure:
        return
    try:
        await client.send_command(
            "call_service",
            domain="persistent_notification",
            service="create",
            service_data={
                "title": "Mylo: background verify failed",
                "message": (
                    f"Wrote {path.name} but couldn't confirm it loaded in "
                    f"Home Assistant after reload_all: {message}. "
                    "Check Settings → Automations and the file on disk."
                ),
                "notification_id": f"mylo_verify_failed_{path.stem}",
            },
        )
    except Exception:
        log.exception("background_verify.notification_failed")


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
