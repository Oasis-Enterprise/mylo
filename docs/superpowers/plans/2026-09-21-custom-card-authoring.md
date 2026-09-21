# Custom Card Authoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Mylo write a custom Lovelace card into its own folder, register it as a resource, and use it on a dashboard, with one Apply covering the card and the plan.

**Architecture:** A new `mylo.dashboard.cards` module holds the card contract checks, a `CardStore` (same shape as `PlanStore`), and the resource-registration helper. `stage_custom_card` (READ, uncached) validates and stages; `apply_custom_card` (MODIFY, needs the card id in the request's approved ids) backs up, writes `www/mylo-cards/<element>.js`, and creates or updates the module resource. `plan_dashboard` treats staged elements as installed. The UI collects card ids into the same approved list and renders a "Custom cards" block with a source toggle and line diff.

**Tech Stack:** Python 3.12, pydantic v2, aiohttp websocket client, pytest; React 18 + TypeScript.

**Spec:** `docs/superpowers/specs/2026-09-21-custom-card-authoring-design.md`

## Global Constraints

- Every commit leaves `pytest tests/unit`, `ruff check src tests`, `ruff format --check src tests`, and `mypy` (strict, `files = ["src/mylo"]`) green, and `python -c "from mylo.server.app import build_app"` imports. UI tasks additionally run `cd ui && npx tsc -b --noEmit`. Do not run `npm run build`.
- New source files start with the Apache 2.0 header block copied verbatim from `src/mylo/dashboard/plan.py` (Python) or `ui/src/App.tsx` (TS).
- Commit messages end with the trailer lines:
  `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_01QarEC58cAhbRGyQ2oSU69e`
- The only `.js` write path in the whole codebase is `resolve_custom_card_path`; `WRITE_ALLOWED_EXTENSIONS` in `file_access.py` stays `{".yaml", ".yml"}`.
- Element names match `^mylo-[a-z0-9]+(-[a-z0-9]+)*$`, length ≤ 48. Source ≤ 65,536 bytes.
- Error codes are exactly: `card_invalid`, `card_unchanged`, `card_not_approved`, `card_not_found`, `cards_unavailable`, `write_failed`, `resource_failed`.
- The approval channel stays named `approved_plan_ids` on the wire and on `ToolContext`; it carries card ids too.
- Releases go through `scripts/release.sh 1.6.0` after the changelog entry, only when the user says.

## File Structure

| File | Responsibility |
|---|---|
| `src/mylo/dashboard/cards.py` (new) | `CardIssue`, `check_card_source`, `StagedCard`, `CardStore`, `ensure_card_resource` |
| `src/mylo/safety/file_access.py` | `CUSTOM_CARD_DIR`, `CUSTOM_CARD_ELEMENT_RE`, `resolve_custom_card_path` |
| `src/mylo/tools/read/stage_custom_card.py` (new) | the staging tool |
| `src/mylo/tools/write/apply_custom_card.py` (new) | the apply tool |
| `src/mylo/tools/read/plan_dashboard.py` | union staged elements into `installed_custom` |
| `src/mylo/tools/read/query_dashboard_env.py` | `mylo_cards` list |
| `src/mylo/tools/context.py`, `src/mylo/server/app.py`, `src/mylo/server/routes_chat.py`, `src/mylo/scripts/chat.py`, `tests/unit/_helpers.py` | `cards: CardStore` wiring |
| `src/mylo/data/system_prompt.txt`, `PROMPT_CHANGELOG.md`, `references/dashboard_examples.yaml` | prompt 0.7.0 + example card |
| `ui/src/types.ts`, `ui/src/App.tsx`, `ui/src/components/DashboardPlanCard.tsx`, `ui/src/lib/lineDiff.ts` (new) | staged-card block, id collection, diff |
| `CHANGELOG.md` | 1.6.0 entry |

---

### Task 1: Card contract checks

**Files:**
- Create: `src/mylo/dashboard/cards.py`
- Test: `tests/unit/test_custom_card_contract.py`

**Interfaces:**
- Produces: `CardIssue(code: str, message: str, severity: Literal["error","warning"])` dataclass with `to_dict()`; `check_card_source(element: str, source: str) -> list[CardIssue]`; `MAX_CARD_BYTES = 65_536`; `ELEMENT_RE`; `REFERENCE_CARD_SOURCE` (module constant used by tests and, in Task 9, the prompt reference).

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_custom_card_contract.py`:

```python
"""Card contract: every rule fires on a minimal violation and none fire
on the reference card."""

from __future__ import annotations

from mylo.dashboard.cards import (
    MAX_CARD_BYTES,
    REFERENCE_CARD_SOURCE,
    check_card_source,
)

EL = "mylo-entity-row"


def _codes(issues, severity=None):
    return sorted(i.code for i in issues if severity is None or i.severity == severity)


def test_reference_card_is_clean() -> None:
    issues = check_card_source(EL, REFERENCE_CARD_SOURCE)
    assert issues == [], [i.to_dict() for i in issues]


def test_element_name_rules() -> None:
    assert "element_name" in _codes(check_card_source("game-row", REFERENCE_CARD_SOURCE))
    assert "element_name" in _codes(check_card_source("mylo-Game", REFERENCE_CARD_SOURCE))
    assert "element_name" in _codes(check_card_source("mylo-" + "a" * 50, REFERENCE_CARD_SOURCE))


def test_source_size_and_bytes() -> None:
    big = REFERENCE_CARD_SOURCE + "\n// " + "x" * MAX_CARD_BYTES
    assert "source_size" in _codes(check_card_source(EL, big), "error")
    assert "source_size" in _codes(check_card_source(EL, "class A extends HTMLElement {}\x00"), "error")


def test_defines_element_exactly_once() -> None:
    missing = REFERENCE_CARD_SOURCE.replace('customElements.define("mylo-entity-row"', 'customElements.define("mylo-other"')
    assert "defines_element" in _codes(check_card_source(EL, missing), "error")
    twice = REFERENCE_CARD_SOURCE + '\ncustomElements.define("mylo-entity-row", MyloEntityRow);\n'
    assert "defines_element" in _codes(check_card_source(EL, twice), "error")


def test_guards_define() -> None:
    unguarded = REFERENCE_CARD_SOURCE.replace('if (!customElements.get("mylo-entity-row")) {\n  customElements.define("mylo-entity-row", MyloEntityRow);\n}', 'customElements.define("mylo-entity-row", MyloEntityRow);')
    assert "guards_define" in _codes(check_card_source(EL, unguarded), "error")


def test_class_shape_rules() -> None:
    no_extends = REFERENCE_CARD_SOURCE.replace("extends HTMLElement", "extends Thing")
    assert "extends_htmlelement" in _codes(check_card_source(EL, no_extends), "error")
    no_config = REFERENCE_CARD_SOURCE.replace("setConfig(config)", "configure(config)")
    assert "has_set_config" in _codes(check_card_source(EL, no_config), "error")
    no_hass = REFERENCE_CARD_SOURCE.replace("set hass(hass)", "update(hass)")
    assert "has_hass_setter" in _codes(check_card_source(EL, no_hass), "error")


def test_picker_registration_is_a_warning() -> None:
    no_picker = REFERENCE_CARD_SOURCE.replace("window.customCards", "window.other")
    issues = check_card_source(EL, no_picker)
    assert "registers_picker" in _codes(issues, "warning")
    assert _codes(issues, "error") == []


def test_forbidden_constructs() -> None:
    cases = {
        "no_imports": 'import { x } from "y";\n' + REFERENCE_CARD_SOURCE,
        "no_dynamic_code": REFERENCE_CARD_SOURCE + "\neval('1');",
        "no_network": REFERENCE_CARD_SOURCE + "\nfetch('https://x');",
        "no_script_tag": REFERENCE_CARD_SOURCE + "\nconst s = '<script>';",
    }
    for code, src in cases.items():
        assert code in _codes(check_card_source(EL, src), "error"), code


def test_innerhtml_with_state_is_a_warning() -> None:
    src = REFERENCE_CARD_SOURCE + "\nfoo.innerHTML = this._hass.states['x'].state;"
    issues = check_card_source(EL, src)
    assert "innerhtml_with_state" in _codes(issues, "warning")
    assert _codes(issues, "error") == []
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_custom_card_contract.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mylo.dashboard.cards'`.

- [ ] **Step 3: Implement the contract and the reference card**

Create `src/mylo/dashboard/cards.py` (license header first):

```python
"""Custom card authoring: contract checks, staging store, resource helper.

A custom card is one JavaScript module at ``www/mylo-cards/<element>.js``
defining a ``mylo-*`` element. HA cannot validate JavaScript for us, so
``check_card_source`` enforces a small contract with regex/line checks:
enough to reject a card that cannot load or that reaches outside HA.
"""

from __future__ import annotations

import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

MAX_CARD_BYTES = 65_536
ELEMENT_RE = re.compile(r"^mylo-[a-z0-9]+(-[a-z0-9]+)*$")
MAX_ELEMENT_LEN = 48

Severity = Literal["error", "warning"]


@dataclass(slots=True)
class CardIssue:
    code: str
    message: str
    severity: Severity = "error"

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "severity": self.severity}


_EXTENDS_RE = re.compile(r"\bclass\s+\w+\s+extends\s+HTMLElement\b")
_SET_CONFIG_RE = re.compile(r"\bsetConfig\s*\(")
_HASS_SETTER_RE = re.compile(r"\bset\s+hass\s*\(")
_DYNAMIC_RE = re.compile(r"\beval\s*\(|\bnew\s+Function\s*\(|\bdocument\.write\s*\(")
_NETWORK_RE = re.compile(
    r"\bfetch\s*\(|\bXMLHttpRequest\b|\bnew\s+WebSocket\s*\(|\bnavigator\.sendBeacon\s*\("
)
_SCRIPT_TAG_RE = re.compile(r"<script", re.IGNORECASE)
_INNERHTML_RE = re.compile(r"\.innerHTML\s*[+]?=")
_STATE_WORD_RE = re.compile(r"\bhass\b|\bstate\b|\bstates\b")


def _define_re(element: str) -> re.Pattern[str]:
    return re.compile(r"customElements\s*\.\s*define\s*\(\s*['\"]" + re.escape(element) + r"['\"]")


def _get_re(element: str) -> re.Pattern[str]:
    return re.compile(r"customElements\s*\.\s*get\s*\(\s*['\"]" + re.escape(element) + r"['\"]")


def _picker_type_re(element: str) -> re.Pattern[str]:
    return re.compile(r"type\s*:\s*['\"]" + re.escape(element) + r"['\"]")


def check_card_source(element: str, source: str) -> list[CardIssue]:
    issues: list[CardIssue] = []

    if not ELEMENT_RE.match(element) or len(element) > MAX_ELEMENT_LEN:
        issues.append(
            CardIssue(
                "element_name",
                f"element {element!r} must match mylo-<lowercase-words> and be <= "
                f"{MAX_ELEMENT_LEN} chars",
            )
        )

    raw = source.encode("utf-8", errors="strict")
    if len(raw) > MAX_CARD_BYTES or "\x00" in source:
        issues.append(
            CardIssue("source_size", f"source must be <= {MAX_CARD_BYTES} bytes with no NUL bytes")
        )

    defines = list(_define_re(element).finditer(source))
    if len(defines) != 1:
        issues.append(
            CardIssue(
                "defines_element",
                f"source must call customElements.define({element!r}, ...) exactly once "
                f"(found {len(defines)})",
            )
        )
    else:
        guard = _get_re(element).search(source)
        if guard is None or guard.start() > defines[0].start():
            issues.append(
                CardIssue(
                    "guards_define",
                    f"wrap the define in `if (!customElements.get({element!r})) {{ ... }}` "
                    "so a reload does not throw",
                )
            )

    if not _EXTENDS_RE.search(source):
        issues.append(CardIssue("extends_htmlelement", "card class must `extends HTMLElement`"))
    if not _SET_CONFIG_RE.search(source):
        issues.append(CardIssue("has_set_config", "card class must define setConfig(config)"))
    if not _HASS_SETTER_RE.search(source):
        issues.append(CardIssue("has_hass_setter", "card class must define `set hass(hass)`"))

    if "window.customCards" not in source or not _picker_type_re(element).search(source):
        issues.append(
            CardIssue(
                "registers_picker",
                "push {type: <element>, name, description} onto window.customCards so the "
                "card picker lists it",
                severity="warning",
            )
        )

    for lineno, line in enumerate(source.splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("import ") or stripped.startswith("export "):
            issues.append(
                CardIssue("no_imports", f"line {lineno}: imports/exports are not allowed")
            )
        if _INNERHTML_RE.search(line) and _STATE_WORD_RE.search(line):
            issues.append(
                CardIssue(
                    "innerhtml_with_state",
                    f"line {lineno}: assigning innerHTML from state values — use textContent",
                    severity="warning",
                )
            )

    if _DYNAMIC_RE.search(source):
        issues.append(CardIssue("no_dynamic_code", "eval / new Function / document.write are not allowed"))
    if _NETWORK_RE.search(source):
        issues.append(
            CardIssue(
                "no_network",
                "fetch / XMLHttpRequest / WebSocket / sendBeacon are not allowed — read "
                "hass.states and act via hass.callService",
            )
        )
    if _SCRIPT_TAG_RE.search(source):
        issues.append(CardIssue("no_script_tag", "'<script' string literals are not allowed"))

    return issues


REFERENCE_CARD_SOURCE = '''class MyloEntityRow extends HTMLElement {
  setConfig(config) {
    if (!config || !config.entity) throw new Error("mylo-entity-row: 'entity' is required");
    this._config = config;
    if (!this._root) {
      this._root = this.attachShadow({ mode: "open" });
      this._root.innerHTML = `
        <ha-card>
          <div class="row">
            <ha-icon id="icon"></ha-icon>
            <span id="name"></span>
            <span id="value"></span>
          </div>
        </ha-card>
        <style>
          .row { display: flex; align-items: center; gap: 12px; padding: 12px 16px; cursor: pointer; }
          #name { flex: 1; }
          #value { color: var(--secondary-text-color); }
        </style>`;
      this._root.querySelector(".row").addEventListener("click", () => this._toggle());
    }
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _render() {
    if (!this._hass || !this._config || !this._root) return;
    const st = this._hass.states[this._config.entity];
    const name = this._config.name || (st && st.attributes.friendly_name) || this._config.entity;
    this._root.getElementById("name").textContent = name;
    this._root.getElementById("value").textContent = st ? st.state : "unavailable";
    const icon = this._config.icon || (st && st.attributes.icon) || "mdi:circle";
    this._root.getElementById("icon").setAttribute("icon", icon);
  }

  _toggle() {
    if (!this._hass) return;
    this._hass.callService("homeassistant", "toggle", { entity_id: this._config.entity });
  }

  getCardSize() {
    return 1;
  }

  static getStubConfig() {
    return { entity: "light.example" };
  }
}

if (!customElements.get("mylo-entity-row")) {
  customElements.define("mylo-entity-row", MyloEntityRow);
}

window.customCards = window.customCards || [];
window.customCards.push({
  type: "mylo-entity-row",
  name: "Mylo entity row",
  description: "Compact tappable entity row (icon, name, state).",
});
'''
```

Note: `secrets`, `time`, `Callable`, `field`, `datetime`, `Path` are imported now because Task 3 adds `StagedCard`/`CardStore` to this module; ruff will flag them unused until then. To keep this commit green, put those imports in during Task 3 instead — for this task import only `re` and the typing names actually used.

- [ ] **Step 4: Run the tests**

Run: `pytest tests/unit/test_custom_card_contract.py -v`
Expected: PASS.

- [ ] **Step 5: Gate and commit**

```bash
ruff check src tests && ruff format src tests && mypy && pytest tests/unit -q
git add src/mylo/dashboard/cards.py tests/unit/test_custom_card_contract.py
git commit -m "feat(dashboard): custom card contract checks + reference card

<trailers>"
```

---

### Task 2: File policy — the one allowed `.js` path

**Files:**
- Modify: `src/mylo/safety/file_access.py`
- Test: `tests/unit/test_file_access_custom_card.py`

**Interfaces:**
- Produces: `CUSTOM_CARD_DIR = Path("www") / "mylo-cards"`, `resolve_custom_card_path(config_dir: Path, element: str) -> Path` raising `FileAccessError` codes `bad_element`, `path_outside_config`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_file_access_custom_card.py`:

```python
from __future__ import annotations

import os
from pathlib import Path

import pytest

from mylo.safety.file_access import (
    CUSTOM_CARD_DIR,
    FileAccessError,
    resolve_custom_card_path,
    resolve_under_config_writable,
)


def test_resolves_inside_mylo_cards(tmp_path: Path) -> None:
    p = resolve_custom_card_path(tmp_path, "mylo-game-row")
    assert p == (tmp_path / CUSTOM_CARD_DIR / "mylo-game-row.js").resolve()


def test_rejects_bad_element_names(tmp_path: Path) -> None:
    for bad in ("game-row", "mylo-Game", "mylo-a/../b", "mylo-", "mylo-x.js", "mylo-" + "a" * 50):
        with pytest.raises(FileAccessError) as exc:
            resolve_custom_card_path(tmp_path, bad)
        assert exc.value.code == "bad_element", bad


def test_symlinked_folder_escaping_config_is_refused(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "www").mkdir()
    os.symlink(outside, tmp_path / "www" / "mylo-cards")
    with pytest.raises(FileAccessError) as exc:
        resolve_custom_card_path(tmp_path, "mylo-x")
    assert exc.value.code == "path_outside_config"


def test_general_write_policy_still_refuses_js(tmp_path: Path) -> None:
    with pytest.raises(FileAccessError) as exc:
        resolve_under_config_writable(tmp_path, "www/mylo-cards/mylo-x.js")
    assert exc.value.code == "unsupported_write_extension"
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_file_access_custom_card.py -v`
Expected: FAIL with `ImportError: cannot import name 'CUSTOM_CARD_DIR'`.

- [ ] **Step 3: Implement**

In `src/mylo/safety/file_access.py`, add `import re` next to `from pathlib import Path`, and after `WRITE_ALLOWED_EXTENSIONS` add:

```python
# The ONE place a .js write is allowed. Custom cards Mylo authors live
# here and nowhere else; the general write policy above never learns
# about JavaScript. See mylo.dashboard.cards.
CUSTOM_CARD_DIR: Path = Path("www") / "mylo-cards"
CUSTOM_CARD_ELEMENT_RE = re.compile(r"^mylo-[a-z0-9]+(-[a-z0-9]+)*$")
CUSTOM_CARD_MAX_ELEMENT_LEN = 48
```

At the end of the module add:

```python
def resolve_custom_card_path(config_dir: Path, element: str) -> Path:
    """Absolute path for a Mylo-authored card: ``www/mylo-cards/<element>.js``.

    Enforces the element-name grammar (which also rules out traversal —
    no dots, slashes, or uppercase) and that the resolved path, symlinks
    included, stays inside the config directory.
    """
    if not CUSTOM_CARD_ELEMENT_RE.match(element) or len(element) > CUSTOM_CARD_MAX_ELEMENT_LEN:
        raise FileAccessError(
            "bad_element",
            f"element {element!r} must match mylo-<lowercase-words> (<= "
            f"{CUSTOM_CARD_MAX_ELEMENT_LEN} chars)",
        )
    base = Path(config_dir).resolve()
    candidate = (base / CUSTOM_CARD_DIR / f"{element}.js").resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise FileAccessError(
            "path_outside_config", f"{CUSTOM_CARD_DIR}/{element}.js escapes the config directory"
        ) from exc
    return candidate
```

- [ ] **Step 4: Run, gate, commit**

```bash
pytest tests/unit/test_file_access_custom_card.py -v
ruff check src tests && ruff format src tests && mypy && pytest tests/unit -q
git add src/mylo/safety/file_access.py tests/unit/test_file_access_custom_card.py
git commit -m "feat(safety): resolve_custom_card_path — the one allowed .js write

<trailers>"
```

---

### Task 3: Staged cards and the CardStore

**Files:**
- Modify: `src/mylo/dashboard/cards.py`
- Test: `tests/unit/test_card_store.py`

**Interfaces:**
- Produces: `StagedCard` (pydantic model: `card_id, element, path: str, url, source, previous_source: str | None, hash, action: Literal["create","update"], description, config_example: dict | None, warnings: list[dict], created_at: datetime, conversation_id`); `CardStore(ttl_seconds=3600.0, capacity=20, clock=time.monotonic)` with `put/get/remove/new_id` exactly like `PlanStore` plus `staged(conversation_id: str) -> list[StagedCard]`; `card_hash(source) -> str` (8 hex chars of sha256); `card_url(element, hash) -> str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_card_store.py`:

```python
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
    now[0] = 12.5
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
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/test_card_store.py -v` — FAIL with `ImportError`.

- [ ] **Step 3: Implement**

Append to `src/mylo/dashboard/cards.py` (add the imports `hashlib`, `secrets`, `time`, `Callable` from `collections.abc`, `dataclass`/`field` already present, `datetime`, `BaseModel, ConfigDict` from pydantic):

```python
CARD_URL_PREFIX = "/local/mylo-cards/"


def card_hash(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:8]


def card_url(element: str, content_hash: str) -> str:
    return f"{CARD_URL_PREFIX}{element}.js?v={content_hash}"


class StagedCard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card_id: str
    element: str
    path: str
    url: str
    source: str
    previous_source: str | None
    hash: str
    action: Literal["create", "update"]
    description: str
    config_example: dict[str, Any] | None
    warnings: list[dict[str, Any]]
    created_at: datetime
    conversation_id: str


@dataclass(slots=True)
class _Entry:
    card: StagedCard
    expires_at: float


class CardStore:
    """In-memory staged cards awaiting Apply. Same lifecycle as PlanStore."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 3600.0,
        capacity: int = 20,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._capacity = capacity
        self._clock = clock
        self._entries: dict[str, _Entry] = {}

    @staticmethod
    def new_id() -> str:
        return secrets.token_hex(4)

    def put(self, card: StagedCard) -> None:
        self._expire()
        self._entries[card.card_id] = _Entry(card=card, expires_at=self._clock() + self._ttl)
        while len(self._entries) > self._capacity:
            oldest = min(self._entries, key=lambda k: self._entries[k].expires_at)
            del self._entries[oldest]

    def get(self, card_id: str) -> StagedCard | None:
        self._expire()
        entry = self._entries.get(card_id)
        return entry.card if entry is not None else None

    def remove(self, card_id: str) -> None:
        self._entries.pop(card_id, None)

    def staged(self, conversation_id: str) -> list[StagedCard]:
        self._expire()
        return [e.card for e in self._entries.values() if e.card.conversation_id == conversation_id]

    def _expire(self) -> None:
        now = self._clock()
        for key in [k for k, e in self._entries.items() if e.expires_at <= now]:
            del self._entries[key]
```

- [ ] **Step 4: Run, gate, commit**

```bash
pytest tests/unit/test_card_store.py -v
ruff check src tests && ruff format src tests && mypy && pytest tests/unit -q
git add src/mylo/dashboard/cards.py tests/unit/test_card_store.py
git commit -m "feat(dashboard): StagedCard + CardStore

<trailers>"
```

---

### Task 4: Resource registration helper

**Files:**
- Modify: `src/mylo/dashboard/cards.py`
- Test: `tests/unit/test_card_resources.py`

**Interfaces:**
- Produces: `ensure_card_resource(ws_client, element: str, url: str) -> tuple[str | None, Literal["created","updated","unchanged"]]` — raises `CommandError` through (the tool maps it).

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_card_resources.py`:

```python
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
    assert create == {"write": True, "res_type": "module", "url": "/local/mylo-cards/mylo-x.js?v=aa"}


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
```

- [ ] **Step 2: Run to verify failure** — `ImportError`.

- [ ] **Step 3: Implement**

Append to `cards.py` (import `get_resources` from `mylo.ha.lovelace_meta` and the `_WsClient` protocol shape — define a local `class _WsClient(Protocol)` with `async def send_command(self, type_: str, **kwargs: Any) -> Any: ...`):

```python
async def ensure_card_resource(
    ws_client: _WsClient, element: str, url: str
) -> tuple[str | None, Literal["created", "updated", "unchanged"]]:
    """Register ``url`` as a module resource, or bump an existing entry
    for this element to the new cache-busting URL. CommandError propagates."""
    prefix = f"{CARD_URL_PREFIX}{element}.js"
    resources = await get_resources(ws_client)
    existing = next(
        (
            r
            for r in (resources or [])
            if isinstance(r.get("url"), str) and str(r["url"]).split("?", 1)[0] == prefix
        ),
        None,
    )
    if existing is not None:
        rid = str(existing.get("id")) if existing.get("id") is not None else None
        if existing.get("url") == url:
            return rid, "unchanged"
        await ws_client.send_command(
            "lovelace/resources/update", write=True, resource_id=rid, url=url
        )
        return rid, "updated"
    created = await ws_client.send_command(
        "lovelace/resources/create", write=True, res_type="module", url=url
    )
    rid = str(created.get("id")) if isinstance(created, dict) and created.get("id") is not None else None
    return rid, "created"
```

- [ ] **Step 4: Run, gate, commit**

```bash
pytest tests/unit/test_card_resources.py -v
ruff check src tests && ruff format src tests && mypy && pytest tests/unit -q
git add src/mylo/dashboard/cards.py tests/unit/test_card_resources.py
git commit -m "feat(dashboard): ensure_card_resource registers or bumps a module resource

<trailers>"
```

---

### Task 5: Plumbing — CardStore on the context

**Files:**
- Modify: `src/mylo/tools/context.py`, `src/mylo/server/app.py`, `src/mylo/server/routes_chat.py`, `src/mylo/scripts/chat.py`, `tests/unit/_helpers.py`
- Test: `tests/unit/test_routes_chat_approval.py` (one assertion)

**Interfaces:**
- Produces: `ToolContext.cards: CardStore | None = None`; `AppKeys.CARDS`; `make_ctx(..., cards: CardStore | None = None)`.

- [ ] **Step 1: Implement**

`context.py`: `from mylo.dashboard.cards import CardStore`; add after `plans`:
```python
    # Process-wide store of staged custom cards awaiting Apply.
    cards: CardStore | None = None
```
`app.py`: import `CardStore`; `CARDS = web.AppKey("cards", CardStore)` after `PLANS`; `card_store = CardStore(); app[AppKeys.CARDS] = card_store` next to the plan store; pass `cards=card_store` into the base `ToolContext`.
`routes_chat.py`: per-turn `ToolContext(...)` gains `cards=base_ctx.cards,`.
`scripts/chat.py`: `cards=CardStore(),` beside `plans=PlanStore()` (import it).
`tests/unit/_helpers.py`: `cards: CardStore | None = None` param forwarded.

- [ ] **Step 2: Test**

Append to `tests/unit/test_routes_chat_approval.py`:
```python
def test_make_ctx_carries_card_store(tmp_path) -> None:
    from mylo.dashboard.cards import CardStore
    from mylo.ha.registries import Registries
    from tests.unit._helpers import make_ctx

    store = CardStore()
    ctx = make_ctx(ws_client=None, registries=Registries(), tmp_path=tmp_path, cards=store)
    assert ctx.cards is store
```

- [ ] **Step 3: Gate and commit**

```bash
ruff check src tests && ruff format src tests && mypy && pytest tests/unit -q && python -c "from mylo.server.app import build_app"
git add -A src/mylo/tools/context.py src/mylo/server/app.py src/mylo/server/routes_chat.py src/mylo/scripts/chat.py tests/unit/_helpers.py tests/unit/test_routes_chat_approval.py
git commit -m "feat(tools): CardStore on the tool context

<trailers>"
```

---

### Task 6: `stage_custom_card` tool

**Files:**
- Create: `src/mylo/tools/read/stage_custom_card.py`
- Modify: `src/mylo/tools/registry.py` (add after `"mylo.tools.read.plan_dashboard",`)
- Test: `tests/unit/test_stage_custom_card_tool.py`

- [ ] **Step 1: Write the failing tests**

```python
"""stage_custom_card: contract → store → preview envelope."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mylo.dashboard.cards import REFERENCE_CARD_SOURCE, CardStore
from mylo.ha.registries import Registries
from mylo.tools import registry as tool_registry
from mylo.tools.executor import execute
from tests.unit._helpers import make_ctx


@pytest.fixture(autouse=True)
def _load_tools():
    tool_registry._reset_for_tests()
    tool_registry.load_all()
    yield
    tool_registry._reset_for_tests()


def _ctx(tmp_path: Path, store: CardStore | None = None):
    return make_ctx(ws_client=None, registries=Registries(), tmp_path=tmp_path, cards=store or CardStore())


def _params(**over: Any) -> dict[str, Any]:
    base = {"element": "mylo-entity-row", "description": "Compact row", "source": REFERENCE_CARD_SOURCE}
    base.update(over)
    return base


async def test_valid_card_is_staged(tmp_path: Path) -> None:
    store = CardStore()
    result = await execute("stage_custom_card", _params(config_example={"type": "custom:mylo-entity-row", "entity": "light.a"}), _ctx(tmp_path, store))
    assert result.status.value == "ok", result.error_message
    d = result.data
    assert d["preview"] is True and d["action"] == "create" and d["previous_source"] is None
    assert d["url"].startswith("/local/mylo-cards/mylo-entity-row.js?v=")
    assert d["line_count"] > 10 and d["warnings"] == []
    staged = store.get(d["card_id"])
    assert staged is not None and staged.path.endswith("www/mylo-cards/mylo-entity-row.js")


async def test_invalid_card_not_staged(tmp_path: Path) -> None:
    store = CardStore()
    result = await execute("stage_custom_card", _params(source="class A extends Thing {}"), _ctx(tmp_path, store))
    assert result.error_code == "card_invalid"
    assert any(i["code"] == "defines_element" for i in result.data["issues"])
    assert store.staged("test") == []


async def test_update_reports_previous_source(tmp_path: Path) -> None:
    path = tmp_path / "www" / "mylo-cards" / "mylo-entity-row.js"
    path.parent.mkdir(parents=True)
    old = REFERENCE_CARD_SOURCE.replace("Compact tappable", "Old")
    path.write_text(old)
    result = await execute("stage_custom_card", _params(), _ctx(tmp_path))
    assert result.data["action"] == "update" and result.data["previous_source"] == old


async def test_unchanged_source_rejected(tmp_path: Path) -> None:
    path = tmp_path / "www" / "mylo-cards" / "mylo-entity-row.js"
    path.parent.mkdir(parents=True)
    path.write_text(REFERENCE_CARD_SOURCE)
    result = await execute("stage_custom_card", _params(), _ctx(tmp_path))
    assert result.error_code == "card_unchanged"


async def test_two_calls_distinct_ids(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    a = await execute("stage_custom_card", _params(), ctx)
    b = await execute("stage_custom_card", _params(), ctx)
    assert a.data["card_id"] != b.data["card_id"]


async def test_bad_element_name(tmp_path: Path) -> None:
    result = await execute("stage_custom_card", _params(element="game-row"), _ctx(tmp_path))
    assert result.error_code == "card_invalid"
    assert any(i["code"] == "element_name" for i in result.data["issues"])
```

- [ ] **Step 2: Run to verify failure** — `unknown_tool`.

- [ ] **Step 3: Implement**

`src/mylo/tools/read/stage_custom_card.py`:

```python
"""``stage_custom_card`` — check a model-written card and hold it for Apply."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from mylo.dashboard.cards import (
    CardStore,
    StagedCard,
    card_hash,
    card_url,
    check_card_source,
)
from mylo.files.manager import exists, read_text
from mylo.safety.file_access import FileAccessError, resolve_custom_card_path
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register


class StageCustomCardParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    element: str = Field(description="Custom element name, e.g. 'mylo-game-row'. Must start with 'mylo-'.")
    description: str = Field(min_length=1, max_length=160, description="One line: what the card shows and what config it takes.")
    source: str = Field(min_length=1, description="The complete JavaScript module (see the contract in the prompt).")
    config_example: dict[str, Any] | None = Field(default=None, description="A card config using this element, shown to the user.")


async def handler(params: StageCustomCardParams, ctx: ToolContext) -> ToolResult:
    if ctx.cards is None:
        return ToolResult.error("cards_unavailable", "card store not configured")

    issues = check_card_source(params.element, params.source)
    errors = [i for i in issues if i.severity == "error"]
    if errors:
        return ToolResult.error(
            "card_invalid",
            f"{len(errors)} contract violation(s) — fix every listed issue and stage again",
            data={"issues": [i.to_dict() for i in issues]},
        )
    try:
        path = resolve_custom_card_path(ctx.config.ha_config_dir, params.element)
    except FileAccessError as exc:
        return ToolResult.error("card_invalid", exc.message, data={"issues": [{"code": exc.code, "message": exc.message, "severity": "error"}]})

    previous = read_text(path) if exists(path) else None
    if previous is not None and previous == params.source:
        return ToolResult.error("card_unchanged", "the staged source is identical to the file on disk")

    content_hash = card_hash(params.source)
    card = StagedCard(
        card_id=CardStore.new_id(),
        element=params.element,
        path=str(path),
        url=card_url(params.element, content_hash),
        source=params.source,
        previous_source=previous,
        hash=content_hash,
        action="update" if previous is not None else "create",
        description=params.description,
        config_example=params.config_example,
        warnings=[i.to_dict() for i in issues],
        created_at=datetime.now(UTC),
        conversation_id=ctx.conversation_id,
    )
    ctx.cards.put(card)
    return ToolResult.ok(
        {
            "preview": True,
            "card_id": card.card_id,
            "element": card.element,
            "action": card.action,
            "url": card.url,
            "line_count": params.source.count("\n") + 1,
            "byte_count": len(params.source.encode("utf-8")),
            "warnings": card.warnings,
            "previous_source": previous,
            "source": params.source,
            "config_example": params.config_example,
            "description": params.description,
            "note": (
                f"Staged. Reference it as custom:{card.element} in plan_dashboard. Do not call "
                "apply_custom_card until the user's next message approves."
            ),
        }
    )


TOOL = ToolDefinition(
    name="stage_custom_card",
    description=(
        "Stage a custom Lovelace card you wrote for the user's approval. Only when "
        "native and installed custom cards cannot do the job. The source must follow "
        "the card contract (plain HTMLElement in <ha-card>, setConfig + set hass, "
        "guarded customElements.define of a mylo-* element, window.customCards entry, "
        "no imports/fetch/eval). Returns card_id; reference custom:<element> in "
        "plan_dashboard in the same turn. Nothing is written here."
    ),
    params_model=StageCustomCardParams,
    tier=Tier.READ,
    handler=handler,
    cacheable=False,
)
register(TOOL)
```

Registry: add `"mylo.tools.read.stage_custom_card",` after the plan_dashboard line.

- [ ] **Step 4: Run, gate, commit**

```bash
pytest tests/unit/test_stage_custom_card_tool.py -v
ruff check src tests && ruff format src tests && mypy && pytest tests/unit -q
git add src/mylo/tools/read/stage_custom_card.py src/mylo/tools/registry.py tests/unit/test_stage_custom_card_tool.py
git commit -m "feat(tools): stage_custom_card

<trailers>"
```

---

### Task 7: `apply_custom_card` tool

**Files:**
- Create: `src/mylo/tools/write/apply_custom_card.py`
- Modify: `src/mylo/tools/registry.py` (after `"mylo.tools.write.apply_dashboard_plan",`)
- Test: `tests/unit/test_apply_custom_card_tool.py`

- [ ] **Step 1: Write the failing tests**

```python
"""apply_custom_card: approval binding, backup+write, resource registration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mylo.dashboard.cards import REFERENCE_CARD_SOURCE, CardStore
from mylo.ha.registries import Registries
from mylo.ha.ws_client import CommandError
from mylo.tools import registry as tool_registry
from mylo.tools.executor import execute
from tests.unit._helpers import make_ctx


class _Client:
    def __init__(self, resources: list[dict[str, Any]] | None = None, *, create_raises: Exception | None = None) -> None:
        self.resources = resources if resources is not None else []
        self.create_raises = create_raises
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def send_command(self, type_: str, **kwargs: Any) -> Any:
        self.calls.append((type_, kwargs))
        if type_ == "lovelace/resources":
            return self.resources
        if type_ == "lovelace/resources/create":
            if self.create_raises:
                raise self.create_raises
            return {"id": "new1", "url": kwargs["url"], "type": "module"}
        if type_ == "lovelace/resources/update":
            return {"id": kwargs["resource_id"], "url": kwargs["url"], "type": "module"}
        return {}


@pytest.fixture(autouse=True)
def _load_tools():
    tool_registry._reset_for_tests()
    tool_registry.load_all()
    yield
    tool_registry._reset_for_tests()


async def _staged(tmp_path: Path, client: _Client, store: CardStore, source: str = REFERENCE_CARD_SOURCE) -> str:
    ctx = make_ctx(ws_client=client, registries=Registries(), tmp_path=tmp_path, cards=store)
    r = await execute("stage_custom_card", {"element": "mylo-entity-row", "description": "d", "source": source}, ctx)
    assert r.status.value == "ok", r.data
    return r.data["card_id"]


def _apply_ctx(tmp_path: Path, client: _Client, store: CardStore, card_id: str, **kw: Any):
    return make_ctx(ws_client=client, registries=Registries(), tmp_path=tmp_path, cards=store,
                    user_approved=kw.pop("user_approved", True),
                    approved_plan_ids=kw.pop("approved_plan_ids", frozenset({card_id})), **kw)


async def test_apply_writes_file_and_creates_resource(tmp_path: Path) -> None:
    client, store = _Client(), CardStore()
    card_id = await _staged(tmp_path, client, store)
    result = await execute("apply_custom_card", {"card_id": card_id}, _apply_ctx(tmp_path, client, store, card_id))
    assert result.status.value == "ok", result.error_message
    d = result.data
    assert d["preview"] is False and d["resource_action"] == "created" and d["resource_id"] == "new1"
    assert (tmp_path / "www" / "mylo-cards" / "mylo-entity-row.js").read_text() == REFERENCE_CARD_SOURCE
    assert d["backup"] is None
    assert store.get(card_id) is None


async def test_update_takes_backup_and_bumps_resource(tmp_path: Path) -> None:
    path = tmp_path / "www" / "mylo-cards" / "mylo-entity-row.js"
    path.parent.mkdir(parents=True)
    old = REFERENCE_CARD_SOURCE.replace("Compact tappable", "Old")
    path.write_text(old)
    client = _Client([{"id": "r9", "url": "/local/mylo-cards/mylo-entity-row.js?v=old", "type": "module"}])
    store = CardStore()
    card_id = await _staged(tmp_path, client, store)
    result = await execute("apply_custom_card", {"card_id": card_id}, _apply_ctx(tmp_path, client, store, card_id))
    assert result.data["resource_action"] == "updated" and result.data["resource_id"] == "r9"
    assert result.data["backup"] and Path(result.data["backup"]).read_text() == old
    assert path.read_text() == REFERENCE_CARD_SOURCE


async def test_refuses_unapproved(tmp_path: Path) -> None:
    client, store = _Client(), CardStore()
    card_id = await _staged(tmp_path, client, store)
    result = await execute("apply_custom_card", {"card_id": card_id}, _apply_ctx(tmp_path, client, store, card_id, approved_plan_ids=frozenset()))
    assert result.error_code == "card_not_approved"
    assert not (tmp_path / "www" / "mylo-cards" / "mylo-entity-row.js").exists()


async def test_unknown_card(tmp_path: Path) -> None:
    client, store = _Client(), CardStore()
    result = await execute("apply_custom_card", {"card_id": "zzzz"}, _apply_ctx(tmp_path, client, store, "zzzz"))
    assert result.error_code == "card_not_found"


async def test_resource_failure_keeps_file(tmp_path: Path) -> None:
    client = _Client(create_raises=CommandError("home_assistant_error", "boom"))
    store = CardStore()
    card_id = await _staged(tmp_path, client, store)
    result = await execute("apply_custom_card", {"card_id": card_id}, _apply_ctx(tmp_path, client, store, card_id))
    assert result.error_code == "resource_failed"
    assert (tmp_path / "www" / "mylo-cards" / "mylo-entity-row.js").exists()
    assert result.data["url"].startswith("/local/mylo-cards/")


async def test_apply_invalidates_env_cache(tmp_path: Path) -> None:
    from mylo.tools import executor as tool_executor

    client, store = _Client(), CardStore()
    card_id = await _staged(tmp_path, client, store)
    ctx = _apply_ctx(tmp_path, client, store, card_id)
    await execute("query_dashboard_env", {}, ctx)
    before = sum(1 for t, _ in client.calls if t == "lovelace/resources")
    await execute("apply_custom_card", {"card_id": card_id}, ctx)
    await execute("query_dashboard_env", {}, ctx)
    after = sum(1 for t, _ in client.calls if t == "lovelace/resources")
    assert after > before + 1  # apply's own list + a fresh env query, not a cache hit
    tool_executor._result_cache.clear()
```

- [ ] **Step 2: Run to verify failure** — `unknown_tool`.

- [ ] **Step 3: Implement**

`src/mylo/tools/write/apply_custom_card.py`:

```python
"""``apply_custom_card`` — write a staged card and register its resource."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from mylo.dashboard.cards import ensure_card_resource
from mylo.files.backup import take_backup
from mylo.files.manager import atomic_write
from mylo.ha.ws_client import CommandError
from mylo.logging_setup import get_logger
from mylo.tools import executor as tool_executor
from mylo.tools.base import Tier, ToolDefinition, ToolResult
from mylo.tools.context import ToolContext
from mylo.tools.registry import register

log = get_logger(__name__)


class ApplyCustomCardParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card_id: str = Field(min_length=1, description="card_id from stage_custom_card, after the user clicked Apply.")


async def handler(params: ApplyCustomCardParams, ctx: ToolContext) -> ToolResult:
    if params.card_id not in ctx.approved_plan_ids:
        return ToolResult.error("card_not_approved", "this card was not approved by the user — present it and wait for Apply")
    if ctx.cards is None:
        return ToolResult.error("cards_unavailable", "card store not configured")
    card = ctx.cards.get(params.card_id)
    if card is None or card.conversation_id != ctx.conversation_id:
        return ToolResult.error("card_not_found", "staged card expired or unknown — stage it again")

    path = Path(card.path)
    backup_path: str | None = None
    try:
        handle = take_backup(path, ctx.config.ha_config_dir, ctx.config.mylo_data_dir)
        backup_path = str(handle.backup_path) if handle.backup_path else None
        atomic_write(path, card.source)
    except OSError as exc:
        return ToolResult.error("write_failed", f"could not write {path}: {exc}")

    try:
        resource_id, action = await ensure_card_resource(ctx.ws_client, card.element, card.url)
    except CommandError as exc:
        return ToolResult.error(
            "resource_failed",
            f"{exc.code}: {exc.message}",
            data={"path": str(path), "url": card.url, "hint": "the file is written; register the resource by hand or retry apply_custom_card"},
        )

    ctx.cards.remove(card.card_id)
    tool_executor.invalidate("query_dashboard_env")
    log.info("dashboard.custom_card_applied", element=card.element, action=action, resource_id=resource_id)
    return ToolResult.ok(
        {
            "preview": False,
            "card_id": card.card_id,
            "element": card.element,
            "path": str(path),
            "url": card.url,
            "resource_id": resource_id,
            "resource_action": action,
            "backup": backup_path,
            "note": "Tell the user a hard refresh may be needed the first time a new card loads.",
        }
    )


TOOL = ToolDefinition(
    name="apply_custom_card",
    description=(
        "Write a staged custom card to www/mylo-cards/ and register it as a Lovelace "
        "module resource. Pass the card_id from stage_custom_card after the user clicked "
        "Apply. Call this BEFORE apply_dashboard_plan when a plan uses the card."
    ),
    params_model=ApplyCustomCardParams,
    tier=Tier.MODIFY,
    handler=handler,
)
register(TOOL)
```

Note on `take_backup`: when the file does not exist it returns a handle with `backup_path=None` and never touches the disk; on update it copies to `<mylo_data_dir>/backups/www/mylo-cards/<element>.js/<ts>.js`. Confirm that the `.js` suffix survives (it uses `source.suffix`).

- [ ] **Step 4: Run, gate, commit**

```bash
pytest tests/unit/test_apply_custom_card_tool.py -v
ruff check src tests && ruff format src tests && mypy && pytest tests/unit -q
git add src/mylo/tools/write/apply_custom_card.py src/mylo/tools/registry.py tests/unit/test_apply_custom_card_tool.py
git commit -m "feat(tools): apply_custom_card — backup, write, register resource

<trailers>"
```

---

### Task 8: Plan validator accepts staged cards; env lists Mylo cards

**Files:**
- Modify: `src/mylo/tools/read/plan_dashboard.py`, `src/mylo/tools/read/query_dashboard_env.py`
- Test: `tests/unit/test_plan_dashboard_tool.py`, `tests/unit/test_lovelace_meta.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_plan_dashboard_tool.py` (it already has `_ctx`, `_params`, `_DASHBOARD`; extend `_ctx` to accept `cards: CardStore | None = None` forwarded to `make_ctx`):

```python
async def test_staged_mylo_card_is_accepted_in_same_turn(tmp_path: Path) -> None:
    from mylo.dashboard.cards import REFERENCE_CARD_SOURCE, CardStore

    cards = CardStore()
    ctx = _ctx(tmp_path, {"lovelace/resources": []}, cards=cards)
    staged = await execute(
        "stage_custom_card",
        {"element": "mylo-entity-row", "description": "d", "source": REFERENCE_CARD_SOURCE},
        ctx,
    )
    assert staged.status.value == "ok"
    result = await execute(
        "plan_dashboard",
        _params({"op": "add_cards", "view_path": "rooms", "section": "Lights",
                 "cards": [{"type": "custom:mylo-entity-row", "entity": "light.kitchen"}]}),
        ctx,
    )
    assert result.status.value == "ok", result.data


async def test_unstaged_mylo_card_still_errors(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, {"lovelace/resources": []})
    result = await execute(
        "plan_dashboard",
        _params({"op": "add_cards", "view_path": "rooms", "section": "Lights",
                 "cards": [{"type": "custom:mylo-nope", "entity": "light.kitchen"}]}),
        ctx,
    )
    assert result.error_code == "plan_invalid"
```

Append to `tests/unit/test_lovelace_meta.py` (uses its existing `_FakeClient` + `make_ctx`):

```python
async def test_query_dashboard_env_lists_mylo_cards(tmp_path):
    client = _FakeClient({
        "frontend/get_themes": {"themes": {}, "default_theme": None},
        "lovelace/resources": [
            {"id": "a", "url": "/local/mylo-cards/mylo-game-row.js?v=1234abcd", "type": "module"},
            {"id": "b", "url": "/hacsfiles/mushroom/mushroom.js", "type": "module"},
        ],
    })
    ctx = make_ctx(ws_client=client, registries=Registries(), tmp_path=tmp_path)
    result = await execute("query_dashboard_env", {}, ctx)
    assert result.data["mylo_cards"] == [{"element": "mylo-game-row", "url": "/local/mylo-cards/mylo-game-row.js?v=1234abcd"}]
    assert "custom:mylo-game-row" in result.data["custom_cards_detected"]
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement**

`plan_dashboard.py`: after computing `installed_custom`, add:

```python
    # Cards staged in this conversation count as installed so a plan can
    # reference a card the model wrote in the same turn; one Apply then
    # applies the card first and the plan second.
    if ctx.cards is not None:
        staged = {f"custom:{c.element}" for c in ctx.cards.staged(ctx.conversation_id)}
        if staged:
            installed_custom = (installed_custom or set()) | staged
```

Note: when resources are unlistable (`installed_custom is None`) and nothing is staged, behavior is unchanged (warnings). When something is staged, `installed_custom` becomes a set, which turns *other* unverifiable custom cards into errors. That is acceptable: a plan mixing a staged Mylo card with an unverifiable HACS card is rare, and the error names the card.

`query_dashboard_env.py`: after `custom_cards_detected`, add:

```python
    mylo_cards: list[dict[str, str]] = []
    for r in resources or []:
        url = r.get("url")
        if isinstance(url, str) and url.startswith("/local/mylo-cards/"):
            element = url.rsplit("/", 1)[-1].split("?", 1)[0].removesuffix(".js")
            mylo_cards.append({"element": element, "url": url})
    data["mylo_cards"] = sorted(mylo_cards, key=lambda c: c["element"])
```

and extend the tool description with: "mylo_cards lists custom cards Mylo has already authored; update one by staging new source with stage_custom_card."

- [ ] **Step 4: Run, gate, commit**

```bash
ruff check src tests && ruff format src tests && mypy && pytest tests/unit -q
git add src/mylo/tools/read/plan_dashboard.py src/mylo/tools/read/query_dashboard_env.py tests/unit/test_plan_dashboard_tool.py tests/unit/test_lovelace_meta.py
git commit -m "feat(dashboard): plans accept staged Mylo cards; env lists them

<trailers>"
```

---

### Task 9: Prompt 0.7.0 and the reference card

**Files:**
- Modify: `src/mylo/data/system_prompt.txt`, `src/mylo/data/PROMPT_CHANGELOG.md`, `src/mylo/data/references/dashboard_examples.yaml`

- [ ] **Step 1: Header**

Line 1 → `# version: 0.7.0`; insert after line 5:
```
# v0.7.0 adds: custom card authoring (stage_custom_card / apply_custom_card),
# the card contract, and the apply ordering (cards before plans).
```

- [ ] **Step 2: New block**

Insert directly after the line `  first via ask_user.` that ends the "Dashboard work" block (before `Monitoring system:`):

```
Custom cards (only when nothing installed can do it):
- Prefer native cards, then installed custom cards from
  query_dashboard_env. Write a custom card only when neither can express
  what the user asked for, or the user asks for one. Check mylo_cards
  first — you may already have one to update.
- Contract: one JavaScript module, element name mylo-<words>, a class
  that `extends HTMLElement` rendering inside <ha-card>, `setConfig(config)`
  and `set hass(hass)`, `customElements.define` wrapped in
  `if (!customElements.get(...))`, a `window.customCards` entry, no
  import/export, no fetch/XMLHttpRequest/WebSocket, no eval. Read
  `hass.states`, act with `hass.callService`. Use textContent for state
  values. Keep it under 300 lines. The reference card in the dashboard
  examples shows the exact shape.
- Flow: stage_custom_card (whole file) → plan_dashboard referencing
  custom:<element> → tell the user the card and plan are ready → END
  TURN. On the approval turn call apply_custom_card for EVERY staged
  card FIRST, then apply_dashboard_plan.
- Updating a card: stage the complete new source, never a patch; the
  user sees a diff. If the user says the card looks wrong, revise and
  stage again. After the first apply of a new card, mention that a hard
  refresh may be needed once.
```

- [ ] **Step 3: Changelog entry** at the top of `PROMPT_CHANGELOG.md` (after the intro):

```markdown
## 0.7.0 — 2026-09-21

Custom card authoring. New "Custom cards" block: when a custom card is
justified, the card contract in six lines, the stage → plan → wait →
apply-cards-then-plan flow, and update-by-whole-file with a diff.
```

- [ ] **Step 4: Reference card**

Append to `dashboard_examples.yaml`:

```yaml

# ─── Reference custom card (stage_custom_card contract) ─────────────
# Copy this shape. Element name mylo-*, guarded define, customCards
# entry, no imports, no fetch. Read hass.states; act via callService.
# The full file is the `source` argument of stage_custom_card.
custom_card_reference: |
```
followed by `REFERENCE_CARD_SOURCE` from `src/mylo/dashboard/cards.py` indented by two spaces. Add a unit test in `tests/unit/test_custom_card_contract.py`:

```python
def test_reference_card_in_examples_matches_module_constant() -> None:
    from pathlib import Path
    import textwrap

    text = Path("src/mylo/data/references/dashboard_examples.yaml").read_text()
    block = text.split("custom_card_reference: |\n", 1)[1]
    assert textwrap.dedent(block).strip() == REFERENCE_CARD_SOURCE.strip()
```

- [ ] **Step 5: Gate and commit**

```bash
grep -n "stage_custom_card\|apply_custom_card" src/mylo/data/system_prompt.txt
ruff check src tests && ruff format src tests && mypy && pytest tests/unit -q
git add src/mylo/data/system_prompt.txt src/mylo/data/PROMPT_CHANGELOG.md src/mylo/data/references/dashboard_examples.yaml tests/unit/test_custom_card_contract.py
git commit -m "feat(prompt): custom card authoring rules v0.7.0 + reference card

<trailers>"
```

---

### Task 10: UI — staged-card block, id collection, line diff

**Files:**
- Create: `ui/src/lib/lineDiff.ts`
- Modify: `ui/src/types.ts`, `ui/src/App.tsx`, `ui/src/components/DashboardPlanCard.tsx`

- [ ] **Step 1: types.ts** — append:

```ts
export interface StagedCardData {
  card_id: string;
  element: string;
  action: "create" | "update";
  url: string;
  line_count: number;
  byte_count: number;
  warnings: { code: string; message: string; severity: string }[];
  previous_source: string | null;
  source: string;
  config_example: Record<string, unknown> | null;
  description: string;
}
```

- [ ] **Step 2: lineDiff.ts**

```ts
// (license header)

export type DiffLine = { kind: "same" | "add" | "del"; text: string };

// Minimal LCS line diff — enough for a card source of a few hundred
// lines. O(n*m) memory is fine at that size.
export function lineDiff(before: string, after: string): DiffLine[] {
  const a = before.split("\n");
  const b = after.split("\n");
  const n = a.length;
  const m = b.length;
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      out.push({ kind: "same", text: a[i] });
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      out.push({ kind: "del", text: a[i] });
      i++;
    } else {
      out.push({ kind: "add", text: b[j] });
      j++;
    }
  }
  while (i < n) out.push({ kind: "del", text: a[i++] });
  while (j < m) out.push({ kind: "add", text: b[j++] });
  return out;
}
```

- [ ] **Step 3: App.tsx**

- `ApprovalContext` gains `card?: StagedCardData;` (import the type).
- In `buildApprovalContext`, before the `plan_dashboard` branch:
```ts
  if (call.name === "stage_custom_card" && data.preview === true && typeof data.element === "string") {
    const card = data as unknown as StagedCardData;
    return {
      description: `Custom card ${card.element} (${card.action === "update" ? "update" : "new"}, ${card.line_count} lines)`,
      card,
      tierLabel: "TIER-2",
    };
  }
```
- `planIdsFromRecords`: also collect `data.card_id` when `call.name === "stage_custom_card"` and `data.preview === true`. Rename nothing.
- Next to `planContexts`: `const cardContexts = approvalContexts.filter((c) => c.card !== undefined);` and `otherContexts` excludes both plan and card contexts.
- Render: `planContexts.length > 0 || cardContexts.length > 0 ? <DashboardPlanCard plans={...} cards={cardContexts.map((c) => c.card!)} ... /> : <ApprovalCard .../>`.

- [ ] **Step 4: DashboardPlanCard.tsx**

- Props: `cards?: StagedCardData[]` (default `[]`).
- Header text: if `plans.length === 0`, show "Custom card" instead of "Dashboard plan"; summary line joins plan summaries and card descriptions.
- Insert a "Custom cards" block before the assumptions block:

```tsx
      {cards.map((card) => (
        <StagedCardBlock key={card.card_id} card={card} />
      ))}
```

with

```tsx
function StagedCardBlock({ card }: { card: StagedCardData }) {
  const [showSource, setShowSource] = useState(false);
  const diff = card.action === "update" && card.previous_source !== null
    ? lineDiff(card.previous_source, card.source)
    : null;
  const added = diff ? diff.filter((l) => l.kind === "add").length : card.line_count;
  const removed = diff ? diff.filter((l) => l.kind === "del").length : 0;
  return (
    <div className="px-3 py-3 space-y-2 border-t" style={{ borderColor: "var(--color-border)" }}>
      <div className="flex items-center gap-2">
        <Tag tone="muted">CUSTOM CARD</Tag>
        <span className="font-mono text-[11px] font-bold" style={{ color: "var(--color-accent)" }}>
          custom:{card.element}
        </span>
        <span className="font-mono text-[10px]" style={{ color: "var(--color-text-dim)" }}>
          {card.action === "update" ? `update · +${added} −${removed}` : `new · ${card.line_count} lines`}
        </span>
      </div>
      <div className="font-sans text-[12px]" style={{ color: "var(--color-text)" }}>{card.description}</div>
      {card.config_example ? (
        <pre className="font-mono text-[10px] overflow-x-auto" style={{ color: "var(--color-text-muted)" }}>
          {toYaml(card.config_example)}
        </pre>
      ) : null}
      {card.warnings.map((w, i) => (
        <div key={i} className="font-mono text-[10px]" style={{ color: "var(--color-warning)" }}>
          {w.code}: {w.message}
        </div>
      ))}
      <button type="button" onClick={() => setShowSource((v) => !v)} className={ghostBtn} style={ghostStyle}>
        {showSource ? "Hide source" : "Show source"}
      </button>
      {showSource ? (
        <pre className="font-mono text-[10px] overflow-x-auto rounded border px-2 py-1.5"
             style={{ borderColor: "var(--color-border)", backgroundColor: "var(--color-surface)" }}>
          {diff
            ? diff.map((l, i) => (
                <div key={i} style={{ color: l.kind === "add" ? "var(--color-accent)" : l.kind === "del" ? "var(--color-warning)" : "var(--color-text-muted)" }}>
                  {l.kind === "add" ? "+ " : l.kind === "del" ? "− " : "  "}{l.text}
                </div>
              ))
            : card.source}
        </pre>
      ) : null}
    </div>
  );
}
```

Import `lineDiff` from `../lib/lineDiff` and `StagedCardData` from `../types`; `ghostBtn`/`ghostStyle`/`toYaml`/`Tag` already exist in the file.

- [ ] **Step 5: Type-check and commit**

```bash
cd ui && npx tsc -b --noEmit && cd ..
git add ui/src/lib/lineDiff.ts ui/src/types.ts ui/src/App.tsx ui/src/components/DashboardPlanCard.tsx
git commit -m "feat(ui): staged custom cards in the approval card with source toggle and diff

<trailers>"
```

---

### Task 11: Changelog

**Files:** `CHANGELOG.md` (insert above `## [1.5.2]`)

```markdown
## [1.6.0] — 2026-09-21

### Added
- **Mylo can now write its own custom cards.** When native and installed cards cannot do what you asked, Mylo writes a small JavaScript card, saves it under `/config/www/mylo-cards/`, registers it as a dashboard resource, and uses it in the plan, all behind one Apply. The approval view shows what the card does, its config, and a "Show source" toggle; updating an existing card shows a line diff. Cards are checked against a contract before they can be staged (plain `HTMLElement`, `setConfig` and `hass`, guarded registration, no imports, no network calls, no `eval`). Mylo only ever writes to its own folder and its own `mylo-` element names.
- `query_dashboard_env` lists the cards Mylo has already authored.

### Changed
- Prompt 0.7.0 adds the custom-card rules and the apply ordering (cards before plans).
```

Commit `docs(changelog): 1.6.0 — custom card authoring`. Release only when the user says: `scripts/release.sh 1.6.0`.

---

## Manual verification in Home Assistant

1. "Make me a compact one-line card that shows a lock's state with a big tap target and use it for the front and side door on the phone dashboard." Expect: `query_dashboard_env`, one `stage_custom_card`, one `plan_dashboard`, then a plan card with a CUSTOM CARD block (new · N lines, Show source) above the sections. Apply → Mylo reports the card written and registered, then the plan applied. Hard refresh once; the card renders.
2. "Make the lock card's text bigger." Expect: `stage_custom_card` with the whole file, the block reads "update · +a −b", Show source shows the diff, Apply bumps the resource `?v=`.
3. Ask for a card with `fetch(` in it (e.g. "pull weather from an API"). Expect Mylo either uses `hass.states` or reports the contract violation and offers an alternative.
