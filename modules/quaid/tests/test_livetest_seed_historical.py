"""Tests for live-test historical memory seed data."""

import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def test_seed_historical_records_event_dates_on_occurred_axis(tmp_path, monkeypatch):
    instance = "seed-history-test"
    data_dir = tmp_path / ".quaid" / "instances" / instance / "data"
    data_dir.mkdir(parents=True)
    db_path = data_dir / "memory.db"

    root = Path(__file__).resolve().parents[1]
    schema_path = root / "datastore" / "memorydb" / "schema.sql"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(schema_path.read_text(encoding="utf-8"))

    script_path = root / "tests" / "livetest" / "livetest-guide" / "data" / "seed-historical.sh"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("INSTANCE", instance)

    before = datetime.now(timezone.utc)
    subprocess.run([str(script_path)], check=True, cwd=root)
    after = datetime.now(timezone.utc)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT occurred_start, occurred_end, mentioned_at, created_at, updated_at
            FROM nodes
            WHERE name LIKE '%hist-amber-valentine-2023%'
            """
        ).fetchone()
        count = conn.execute(
            "SELECT count(*) FROM nodes WHERE name LIKE '%hist-%'"
        ).fetchone()[0]

    assert count == 7
    assert row["occurred_start"] == "2023-02-14T10:00:00Z"
    assert row["occurred_end"] == "2023-02-14T10:00:00Z"
    created_at = _parse_utc(row["created_at"])
    assert before.replace(microsecond=0) <= created_at <= after + timedelta(seconds=1)
    assert row["updated_at"] == row["created_at"]
    assert row["mentioned_at"] == row["created_at"]
