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

"""Tests for the persistent monthly spend ledger."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from mylo.llm.usage_ledger import UsageLedger

_JUNE = datetime(2026, 6, 15, 9, 0, 0)
_JULY = datetime(2026, 7, 1, 0, 5, 0)


def test_starts_at_zero(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path)
    assert ledger.month_spent(_JUNE) == 0.0


def test_records_accumulate_within_month(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path)
    assert ledger.record(0.12, _JUNE) == pytest.approx(0.12)
    assert ledger.record(0.08, _JUNE) == pytest.approx(0.20)
    assert ledger.month_spent(_JUNE) == pytest.approx(0.20)


def test_rolls_over_on_new_month(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path)
    ledger.record(5.0, _JUNE)
    # A new calendar month starts fresh.
    assert ledger.month_spent(_JULY) == 0.0
    assert ledger.record(1.0, _JULY) == pytest.approx(1.0)


def test_persists_across_instances(tmp_path: Path) -> None:
    UsageLedger(tmp_path).record(0.50, _JUNE)
    # A fresh instance (e.g. after a restart) reads the same file.
    assert UsageLedger(tmp_path).month_spent(_JUNE) == pytest.approx(0.50)


def test_negative_charge_is_ignored(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path)
    ledger.record(1.0, _JUNE)
    assert ledger.record(-5.0, _JUNE) == pytest.approx(1.0)


def test_corrupt_file_resets_to_zero(tmp_path: Path) -> None:
    path = tmp_path / "usage_ledger.json"
    path.write_text("{not json", encoding="utf-8")
    ledger = UsageLedger(tmp_path)
    # Must not raise — a corrupt ledger never breaks a chat turn.
    assert ledger.month_spent(_JUNE) == 0.0
    assert ledger.record(0.25, _JUNE) == pytest.approx(0.25)
