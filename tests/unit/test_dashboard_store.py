"""PlanStore: TTL expiry, capacity eviction, ids."""

from __future__ import annotations

from datetime import UTC, datetime

from mylo.dashboard.plan import DashboardPlan, DeleteView
from mylo.dashboard.store import PlanStore


def _plan(plan_id: str) -> DashboardPlan:
    return DashboardPlan(
        plan_id=plan_id,
        dashboard_id=None,
        summary="s",
        assumptions=[],
        operations=[DeleteView(op="delete_view", view_path="x")],
        resolved=[],
        issues=[],
        created_at=datetime.now(UTC),
        conversation_id="c1",
    )


def test_put_get_remove() -> None:
    store = PlanStore()
    store.put(_plan("a"))
    assert store.get("a") is not None
    assert store.get("b") is None
    store.remove("a")
    assert store.get("a") is None


def test_expires_after_ttl() -> None:
    now = [100.0]
    store = PlanStore(ttl_seconds=60.0, clock=lambda: now[0])
    store.put(_plan("a"))
    now[0] = 159.0
    assert store.get("a") is not None
    now[0] = 160.0
    assert store.get("a") is None


def test_capacity_evicts_oldest() -> None:
    now = [0.0]
    store = PlanStore(capacity=2, clock=lambda: now[0])
    for i, pid in enumerate(("a", "b", "c")):
        now[0] = float(i)
        store.put(_plan(pid))
    assert store.get("a") is None
    assert store.get("b") is not None
    assert store.get("c") is not None


def test_new_id_is_short_and_unique() -> None:
    ids = {PlanStore.new_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(len(i) == 8 for i in ids)
