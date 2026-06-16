import sqlite3
from pathlib import Path

import pytest

from datastore.memorydb import domain_registry


def test_safe_load_active_domains_warns_and_returns_empty_when_fail_open(monkeypatch, caplog):
    monkeypatch.setattr(
        domain_registry,
        "load_active_domains",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("db unavailable")),
    )
    monkeypatch.setattr(domain_registry, "_fail_hard_enabled", lambda: False)
    caplog.set_level("WARNING")

    assert domain_registry.safe_load_active_domains(Path("/tmp/memory.db")) == {}
    assert "Failed loading active domains" in caplog.text


def test_safe_load_active_domains_raises_under_failhard(monkeypatch):
    monkeypatch.setattr(
        domain_registry,
        "load_active_domains",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("db unavailable")),
    )
    monkeypatch.setattr(domain_registry, "_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="db unavailable"):
        domain_registry.safe_load_active_domains(Path("/tmp/memory.db"))


def test_apply_domain_set_timestamps_honor_quaid_now(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        domain_registry.ensure_domain_tables(conn)

        monkeypatch.setenv("QUAID_NOW", "2026-09-01T01:02:03+00:00")
        domain_registry.apply_domain_set(
            conn,
            {"personal": "Personal notes", "work": "Work notes"},
            deactivate_others=False,
        )

        monkeypatch.setenv("QUAID_NOW", "2026-09-02T04:05:06+00:00")
        domain_registry.apply_domain_set(
            conn,
            {"personal": "Personal notes updated"},
            deactivate_others=True,
        )

        personal = conn.execute(
            "SELECT description, active, created_at, updated_at FROM domain_registry WHERE domain = ?",
            ("personal",),
        ).fetchone()
        work = conn.execute(
            "SELECT active, updated_at FROM domain_registry WHERE domain = ?",
            ("work",),
        ).fetchone()

        assert personal is not None
        assert personal["description"] == "Personal notes updated"
        assert personal["active"] == 1
        assert personal["created_at"] == "2026-09-01T01:02:03+00:00"
        assert personal["updated_at"] == "2026-09-02T04:05:06+00:00"
        assert work is not None
        assert work["active"] == 0
        assert work["updated_at"] == "2026-09-02T04:05:06+00:00"
    finally:
        conn.close()


def test_quaid_now_without_timezone_is_normalized_to_utc(monkeypatch):
    monkeypatch.setenv("QUAID_NOW", "2026-09-01T01:02:03")

    assert domain_registry._now_iso() == "2026-09-01T01:02:03+00:00"
