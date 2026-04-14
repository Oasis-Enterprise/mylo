"""Tests for the audit logger — append-only JSON Lines, month rollover,
tolerant recent-read.
"""

from __future__ import annotations

import json
from pathlib import Path

from mylo.safety.audit import AuditLogger, make_entry


async def _write_one(logger: AuditLogger, **overrides: object) -> None:
    entry = make_entry(
        conversation_id=overrides.get("conversation_id", "c1"),  # type: ignore[arg-type]
        tool_name=overrides.get("tool_name", "query_entities"),  # type: ignore[arg-type]
        tier=overrides.get("tier", 1),  # type: ignore[arg-type]
        params=overrides.get("params", {"filter": {"area": "kitchen"}}),  # type: ignore[arg-type]
        dry_run=False,
        user_approved=False,
        result=overrides.get("result", "success"),  # type: ignore[arg-type]
    )
    await logger.write(entry)


async def test_write_creates_monthly_file_with_json_line(tmp_path: Path) -> None:
    logger = AuditLogger(tmp_path)
    await _write_one(logger, tool_name="query_entities")

    files = list((tmp_path / "audit").iterdir())
    assert len(files) == 1
    assert files[0].suffix == ".log"

    line = files[0].read_text().strip().splitlines()[0]
    parsed = json.loads(line)
    assert parsed["tool_name"] == "query_entities"
    assert parsed["tier"] == 1
    assert parsed["result"] == "success"
    assert parsed["conversation_id"] == "c1"
    assert "timestamp" in parsed


async def test_multiple_writes_append(tmp_path: Path) -> None:
    logger = AuditLogger(tmp_path)
    await _write_one(logger, tool_name="a")
    await _write_one(logger, tool_name="b")
    await _write_one(logger, tool_name="c")

    files = list((tmp_path / "audit").iterdir())
    lines = files[0].read_text().strip().splitlines()
    names = [json.loads(line)["tool_name"] for line in lines]
    assert names == ["a", "b", "c"]


async def test_read_recent_returns_newest_first(tmp_path: Path) -> None:
    logger = AuditLogger(tmp_path)
    # Write three entries — the timestamps will be increasing.
    await _write_one(logger, tool_name="first")
    await _write_one(logger, tool_name="second")
    await _write_one(logger, tool_name="third")

    rows = logger.read_recent(limit=10)
    names = [r["tool_name"] for r in rows]
    # Newest first.
    assert names == ["third", "second", "first"]


def test_read_recent_tolerates_missing_files(tmp_path: Path) -> None:
    logger = AuditLogger(tmp_path)
    # No audit/ dir yet.
    assert logger.read_recent() == []


async def test_read_recent_tolerates_malformed_line(tmp_path: Path) -> None:
    logger = AuditLogger(tmp_path)
    await _write_one(logger, tool_name="good")

    # Corrupt the file with a junk line.
    log_file = next((tmp_path / "audit").iterdir())
    with log_file.open("a") as f:
        f.write("{not json at all\n")

    await _write_one(logger, tool_name="also_good")

    rows = logger.read_recent(limit=10)
    assert [r["tool_name"] for r in rows] == ["also_good", "good"]
