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

from __future__ import annotations

from typing import Any

from mylo.ha.registries import EntityEntry, Registries, _adaptive_timeout
from mylo.ha.ws_client import CommandTimeout


def test_adaptive_timeout_scales_with_size() -> None:
    assert _adaptive_timeout(200) == 60.0  # floor
    assert _adaptive_timeout(5000) > 60.0  # scales up
    assert _adaptive_timeout(50_000) <= 300.0  # capped


def _entity(eid: str) -> EntityEntry:
    return EntityEntry(
        entity_id=eid,
        name=None,
        original_name=None,
        platform=None,
        device_id=None,
        area_id=None,
        labels=(),
        disabled_by=None,
        hidden_by=None,
    )


async def test_refresh_keeps_warm_data_on_timeout() -> None:
    reg = Registries()

    class _Client:
        async def send_command(self, type_: str, **kwargs: Any) -> Any:
            raise CommandTimeout(type_, kwargs.get("timeout", 60.0))

    reg._client = _Client()  # type: ignore[assignment]
    reg.entities = {"light.kitchen": _entity("light.kitchen")}

    await reg.refresh(force=True)  # must not raise, must not clear

    assert "light.kitchen" in reg.entities


async def test_refresh_for_keeps_warm_data_on_timeout() -> None:
    reg = Registries()

    class _Client:
        async def send_command(self, type_: str, **kwargs: Any) -> Any:
            raise CommandTimeout(type_, kwargs.get("timeout", 60.0))

    reg._client = _Client()  # type: ignore[assignment]
    reg.entities = {"light.kitchen": _entity("light.kitchen")}

    await reg.refresh_for("entity_registry_updated")  # must not raise

    assert "light.kitchen" in reg.entities
