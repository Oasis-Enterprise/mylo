from __future__ import annotations

from typing import Any

from mylo.dashboard.cards import ensure_card_resource


class _Client:
    def __init__(self, resources: list[dict[str, Any]] | None) -> None:
        self.resources = resources
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def send_command(self, type_: str, **kwargs: Any) -> Any:
        self.calls.append((type_, kwargs))
        if type_ == "lovelace/resources":
            if self.resources is None:
                from mylo.ha.ws_client import CommandError

                raise CommandError("unknown_command", "no")
            return self.resources
        if type_ == "lovelace/resources/create":
            return {"id": "new1", "url": kwargs["url"], "type": kwargs["res_type"]}
        if type_ == "lovelace/resources/update":
            return {"id": kwargs["resource_id"], "url": kwargs["url"], "type": "module"}
        return None


async def test_creates_when_absent() -> None:
    client = _Client([{"id": "r1", "url": "/hacsfiles/mushroom.js", "type": "module"}])
    rid, action = await ensure_card_resource(client, "mylo-x", "/local/mylo-cards/mylo-x.js?v=aa")
    assert (rid, action) == ("new1", "created")
    create = next(k for t, k in client.calls if t == "lovelace/resources/create")
    assert create == {
        "write": True,
        "res_type": "module",
        "url": "/local/mylo-cards/mylo-x.js?v=aa",
    }


async def test_updates_when_url_differs() -> None:
    client = _Client([{"id": "r9", "url": "/local/mylo-cards/mylo-x.js?v=old", "type": "module"}])
    rid, action = await ensure_card_resource(client, "mylo-x", "/local/mylo-cards/mylo-x.js?v=new")
    assert (rid, action) == ("r9", "updated")
    update = next(k for t, k in client.calls if t == "lovelace/resources/update")
    assert update["resource_id"] == "r9" and update["url"].endswith("v=new")


async def test_unchanged_when_url_matches() -> None:
    client = _Client([{"id": "r9", "url": "/local/mylo-cards/mylo-x.js?v=same", "type": "module"}])
    rid, action = await ensure_card_resource(client, "mylo-x", "/local/mylo-cards/mylo-x.js?v=same")
    assert (rid, action) == ("r9", "unchanged")
    assert not any(t.endswith("/update") or t.endswith("/create") for t, _ in client.calls)


async def test_creates_when_resources_unlistable() -> None:
    client = _Client(None)
    rid, action = await ensure_card_resource(client, "mylo-x", "/local/mylo-cards/mylo-x.js?v=aa")
    assert (rid, action) == ("new1", "created")
