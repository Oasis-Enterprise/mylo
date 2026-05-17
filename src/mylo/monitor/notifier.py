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

"""HA notification sender with mobile app actionable notifications,
quiet-hours enforcement, daily caps, and delivery method routing.

Delivery methods:
- **auto** — use mobile app if detected, fall back to persistent.
- **mobile** — mobile app only, skip if not available.
- **persistent** — HA notification panel only (classic behavior).
- **off** — same as proactive_notifications=false.

Mobile app notifications include action buttons (Turn Off / Ignore /
Don't Ask Again) so users can respond without opening the panel.
Button presses fire ``mobile_app_notification_action`` events that
Mylo subscribes to in the scheduler.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo

from mylo.ha.ws_client import CommandError, HaWsClient
from mylo.logging_setup import get_logger
from mylo.memory.schema import MemoryFile

if TYPE_CHECKING:
    from mylo.config import AppConfig

log = get_logger(__name__)

Severity = Literal["critical", "normal", "low"]


@dataclass(slots=True)
class NotificationAction:
    """An action button on a mobile notification."""

    action: str  # event tag, e.g. "MYLO_ACCEPT_on_while_away_light.kitchen"
    title: str  # button label, e.g. "Turn Off"


@dataclass(slots=True)
class Notifier:
    ws_client: HaWsClient
    config: AppConfig
    memory: MemoryFile | None = None
    _daily_count: int = 0
    _daily_date: str = ""
    _tz: ZoneInfo | None = field(default=None, init=False)
    _mobile_services: list[str] = field(default_factory=list, init=False)
    _mobile_discovered: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        try:
            self._tz = ZoneInfo(self.config.quiet_hours_start.split(":")[0][:0] or "UTC")
        except Exception:
            self._tz = None

    async def discover_mobile_apps(self) -> None:
        """Find notify.mobile_app_* services for actionable notifications."""
        if self._mobile_discovered:
            return
        self._mobile_discovered = True
        try:
            services = await self.ws_client.send_command("get_services", timeout=10.0)
            if isinstance(services, dict):
                notify_services = services.get("notify", {})
                if isinstance(notify_services, dict):
                    self._mobile_services = [
                        f"notify.{name}"
                        for name in notify_services
                        if name.startswith("mobile_app_")
                    ]
                    if self._mobile_services:
                        log.info(
                            "notifier.mobile_apps_found",
                            services=self._mobile_services,
                        )
        except Exception:
            log.debug("notifier.mobile_discovery_failed")

    async def send(
        self,
        *,
        title: str,
        message: str,
        notification_id: str,
        severity: Severity = "normal",
        notification_type: str | None = None,
        entity_id: str | None = None,
        actions: list[NotificationAction] | None = None,
    ) -> bool:
        """Send a notification via the configured delivery method.

        Returns True if sent, False if suppressed.
        """
        method = self.config.notification_method

        # "off" = kill switch.
        if method == "off":
            return False

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

        # Route to the right delivery method.
        sent = False
        if method in ("auto", "mobile"):
            await self.discover_mobile_apps()
            if self._mobile_services:
                sent = await self._send_mobile(
                    title=title,
                    message=message,
                    notification_id=notification_id,
                    actions=actions,
                )

        if not sent and method in ("auto", "persistent"):
            sent = await self._send_persistent(
                title=title,
                message=message,
                notification_id=notification_id,
            )

        if sent:
            self._daily_count += 1
            log.info(
                "notifier.sent",
                title=title,
                notification_id=notification_id,
                severity=severity,
                method="mobile"
                if self._mobile_services and method != "persistent"
                else "persistent",
                daily_count=self._daily_count,
            )

        return sent

    async def _send_mobile(
        self,
        *,
        title: str,
        message: str,
        notification_id: str,
        actions: list[NotificationAction] | None = None,
    ) -> bool:
        """Send via all discovered mobile app notify services."""
        service_data: dict[str, Any] = {
            "title": f"Mylo: {title}",
            "message": message,
            "data": {
                "tag": notification_id,
                "group": "mylo",
            },
        }

        if actions:
            service_data["data"]["actions"] = [
                {"action": a.action, "title": a.title} for a in actions
            ]

        sent_any = False
        for service in self._mobile_services:
            _, name = service.split(".", 1)
            try:
                await self.ws_client.send_command(
                    "call_service",
                    domain="notify",
                    service=name,
                    service_data=service_data,
                    timeout=10.0,
                )
                sent_any = True
            except CommandError as exc:
                log.warning(
                    "notifier.mobile_send_failed",
                    service=service,
                    error=f"{exc.code}: {exc.message}",
                )
            except Exception:
                log.exception("notifier.mobile_send_failed", service=service)

        return sent_any

    async def _send_persistent(
        self,
        *,
        title: str,
        message: str,
        notification_id: str,
    ) -> bool:
        """Send via HA's persistent_notification service."""
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
