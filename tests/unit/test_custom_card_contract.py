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
    assert "source_size" in _codes(
        check_card_source(EL, "class A extends HTMLElement {}\x00"), "error"
    )


def test_defines_element_exactly_once() -> None:
    missing = REFERENCE_CARD_SOURCE.replace(
        'customElements.define("mylo-entity-row"', 'customElements.define("mylo-other"'
    )
    assert "defines_element" in _codes(check_card_source(EL, missing), "error")
    twice = REFERENCE_CARD_SOURCE + '\ncustomElements.define("mylo-entity-row", MyloEntityRow);\n'
    assert "defines_element" in _codes(check_card_source(EL, twice), "error")


def test_guards_define() -> None:
    unguarded = REFERENCE_CARD_SOURCE.replace(
        'if (!customElements.get("mylo-entity-row")) {\n  customElements.define("mylo-entity-row", MyloEntityRow);\n}',
        'customElements.define("mylo-entity-row", MyloEntityRow);',
    )
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


def test_reference_card_in_examples_matches_module_constant() -> None:
    import textwrap
    from pathlib import Path

    text = Path("src/mylo/data/references/dashboard_examples.yaml").read_text()
    block = text.split("custom_card_reference: |\n", 1)[1]
    assert textwrap.dedent(block).strip() == REFERENCE_CARD_SOURCE.strip()
