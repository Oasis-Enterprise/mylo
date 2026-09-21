from __future__ import annotations

from datetime import UTC, datetime

from mylo.dashboard.cards import CardStore, StagedCard, card_hash, card_url


def _card(card_id: str, element: str = "mylo-x", conversation_id: str = "c1") -> StagedCard:
    return StagedCard(
        card_id=card_id,
        element=element,
        path=f"/config/www/mylo-cards/{element}.js",
        url=card_url(element, "abcdef12"),
        source="class A extends HTMLElement {}",
        previous_source=None,
        hash="abcdef12",
        action="create",
        description="d",
        config_example=None,
        warnings=[],
        created_at=datetime.now(UTC),
        conversation_id=conversation_id,
    )


def test_put_get_remove_and_ids() -> None:
    store = CardStore()
    store.put(_card("a"))
    assert store.get("a") is not None
    store.remove("a")
    assert store.get("a") is None
    ids = {CardStore.new_id() for _ in range(50)}
    assert len(ids) == 50 and all(len(i) == 8 for i in ids)


def test_ttl_and_capacity() -> None:
    now = [0.0]
    store = CardStore(ttl_seconds=10.0, capacity=2, clock=lambda: now[0])
    store.put(_card("a"))
    now[0] = 1.0
    store.put(_card("b"))
    now[0] = 2.0
    store.put(_card("c"))
    assert store.get("a") is None  # evicted by capacity
    now[0] = 11.5  # past b's expiry (11.0) but before c's (12.0)
    assert store.get("b") is None  # expired
    assert store.get("c") is not None


def test_staged_filters_by_conversation() -> None:
    store = CardStore()
    store.put(_card("a", "mylo-a", "c1"))
    store.put(_card("b", "mylo-b", "c2"))
    assert [c.element for c in store.staged("c1")] == ["mylo-a"]


def test_hash_and_url() -> None:
    h = card_hash("hello")
    assert len(h) == 8 and h == card_hash("hello") and h != card_hash("hello!")
    assert card_url("mylo-x", h) == f"/local/mylo-cards/mylo-x.js?v={h}"
