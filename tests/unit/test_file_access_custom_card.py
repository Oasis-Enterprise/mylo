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
    # .js is not in ALLOWED_EXTENSIONS (the read policy), so it is refused
    # there before ever reaching the write-only check — belt and suspenders:
    # the general write policy never even gets a chance to learn about JS.
    assert exc.value.code == "unsupported_extension"


def test_symlinked_card_file_pointing_at_config_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / "secrets.yaml").write_text("api_key: SECRET\n")
    card_dir = tmp_path / "www" / "mylo-cards"
    card_dir.mkdir(parents=True)
    os.symlink(tmp_path / "secrets.yaml", card_dir / "mylo-x.js")
    with pytest.raises(FileAccessError) as exc:
        resolve_custom_card_path(tmp_path, "mylo-x")
    assert exc.value.code == "path_outside_config"


def test_trailing_newline_in_element_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FileAccessError):
        resolve_custom_card_path(tmp_path, "mylo-x\n")
