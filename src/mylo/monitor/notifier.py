"""HA persistent notification sender with quiet-hours + daily-cap
enforcement.

Every outbound notification goes through :meth:`Notifier.send`. The
notifier checks:

1. ``proactive_notifications`` toggle — when False, nothing sends.
2. Quiet hours — between ``quiet_hours_start`` and ``quiet_hours_end``
   (in the user's HA timezone), only ``severity="critical"`` punches
   through.
3. Daily cap — at most ``max_daily_notifications`` per calendar day.
   Critical bypasses the cap.

Notifications include a deep-link to the panel so the user can click
through to the chat or Memory tab.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

from mylo.ha.ws_client import CommandError, HaWsClient
from mylo.logging_setup import get_logger
from mylo.memory.schema import MemoryFile

if TYPE_CHECKING:
    from mylo.config import AppConfig

log = get_logger(__name__)

Severity = Literal["critical", "normal", "low"]


@dataclass(slots=True)
class Notifier:
    ws_client: HaWsClient
    config: AppConfig
    memory: MemoryFile | None = None
    _daily_count: int = 0
    _daily_date: str = ""
    _tz: ZoneInfo | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        try:
            self._tz = ZoneInfo(self.config.quiet_hours_start.split(":")[0][:0] or "UTC")
        except Exception:
            self._tz = None

    async def send(
        self,
        *,
        title: str,
        message: str,
        notification_id: str,
        severity: Severity = "normal",
        notification_type: str | None = None,
        entity_id: str | None = None,
    ) -> bool:
        """Send a persistent notification if policy allows.

        Returns True if sent, False if suppressed.
        """
        if not self.config.proactive_notifications and severity != "critical":
            log.debug("notifier.suppressed_disabled", title=title)
            return False

        # Check user-defined suppression rules from memory.
        if (
            notification_type
            and self.memory is not None
            and self.memory.is_notification_suppressed(notification_type, entity_id)
        ):
            log.debug(
                "notifier.suppressed_by_filter",
                title=title,
                notification_type=notification_type,
                entity_id=entity_id,
            )
            return False

        now = datetime.now(self._tz) if self._tz else datetime.now().astimezone()

        if severity != "critical" and _in_quiet_hours(
            now, self.config.quiet_hours_start, self.config.quiet_hours_end
        ):
            log.debug("notifier.suppressed_quiet_hours", title=title)
            return False

        today = now.strftime("%Y-%m-%d")
        if today != self._daily_date:
            self._daily_count = 0
            self._daily_date = today

        if severity != "critical" and self._daily_count >= self.config.max_daily_notifications:
            log.debug("notifier.suppressed_daily_cap", title=title, count=self._daily_count)
            return False

        try:
            await self.ws_client.send_command(
                "call_service",
                domain="persistent_notification",
                service="create",
                service_data={
                    "title": f"Mylo: {title}",
                    "message": message,
                    "notification_id": notification_id,
                },
                timeout=10.0,
            )
            self._daily_count += 1
            log.info(
                "notifier.sent",
                title=title,
                notification_id=notification_id,
                severity=severity,
                daily_count=self._daily_count,
            )
            return True
        except CommandError as exc:
            log.warning("notifier.send_failed", error=f"{exc.code}: {exc.message}")
            return False
        except Exception:
            log.exception("notifier.send_failed")
            return False


def _in_quiet_hours(now: datetime, start_str: str, end_str: str) -> bool:
    """Check if ``now`` falls within the quiet window.

    Handles overnight ranges (e.g. 22:00 → 07:00) by splitting the
    comparison into two legs.
    """
    try:
        start = _parse_time(start_str)
        end = _parse_time(end_str)
    except ValueError:
        return False

    current = now.time()
    if start <= end:
        return start <= current <= end
    # Overnight: 22:00 → 07:00.
    return current >= start or current <= end


def _parse_time(s: str) -> time:
    parts = s.split(":")
    return time(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
