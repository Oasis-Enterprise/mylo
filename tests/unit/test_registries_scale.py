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

import asyncio
from typing import Any

from mylo.ha.registries import EntityEntry, Registries, _registry_fetch_timeout
from mylo.ha.ws_client import CommandTimeout


def test_registry_fetch_timeout_is_fixed_generous() -> None:
    # No longer size-scaled: a fixed generous backstop now the deadlock and
    # registry storms are handled at the source.
    assert _registry_fetch_timeout() == 120.0


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

    await reg.refresh_for("entity_registry_updated")  # schedules a debounced flush
    await asyncio.sleep(0.4)  # let the debounced refetch run + time out

    assert "light.kitchen" in reg.entities


async def test_registry_events_coalesce_into_one_refetch() -> None:
    calls = {"n": 0}

    class _Client:
        async def send_command(self, type_: str, **kwargs: Any) -> Any:
            calls["n"] += 1
            return []

    reg = Registries()
    reg._client = _Client()  # type: ignore[assignment]
    # Fire 10 entity-registry events rapidly.
    await asyncio.gather(*[reg.refresh_for("entity_registry_updated") for _ in range(10)])
    await asyncio.sleep(0.4)  # let the debounce window elapse
    assert calls["n"] <= 2
