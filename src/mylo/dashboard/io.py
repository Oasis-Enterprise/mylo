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

"""The only two websocket calls the dashboard package makes."""

from __future__ import annotations

from typing import Any, Protocol

from mylo.ha.ws_client import CommandError


class _WsClient(Protocol):
    async def send_command(self, type_: str, **kwargs: Any) -> Any: ...


class DashboardUnavailable(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class DashboardNotFound(DashboardUnavailable):
    pass


async def fetch_dashboard_config(ws_client: _WsClient, dashboard_id: str | None) -> dict[str, Any]:
    """Current config. The default dashboard with nothing saved yet reads
    as an empty config; a named dashboard that doesn't exist raises."""
    try:
        result = await ws_client.send_command("lovelace/config", url_path=dashboard_id)
    except CommandError as exc:
        if exc.code in ("config_not_found", "not_found"):
            if dashboard_id is None:
                return {"views": []}
            raise DashboardNotFound(exc.code, f"no dashboard {dashboard_id!r}") from exc
        raise DashboardUnavailable(exc.code, exc.message) from exc
    if not isinstance(result, dict):
        return {"views": []}
    return result


async def save_dashboard_config(
    ws_client: _WsClient, dashboard_id: str | None, config: dict[str, Any]
) -> None:
    await ws_client.send_command(
        "lovelace/config/save", write=True, url_path=dashboard_id, config=config
    )
