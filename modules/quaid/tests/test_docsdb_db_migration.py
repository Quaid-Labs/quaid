import sqlite3

import pytest

from datastore.docsdb import db_migration


def test_write_migration_state_honors_quaid_now(monkeypatch):
    monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:06:07Z")
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(db_migration._MIGRATION_TABLE_SQL)
        db_migration._write_migration_state(conn, "/tmp/legacy.db", "doc_chunks", "sig")
        row = conn.execute(
            """
            SELECT migrated_at
            FROM docs_db_migration_state
            WHERE source_db = ? AND table_name = ?
            """,
            ("/tmp/legacy.db", "doc_chunks"),
        ).fetchone()
    finally:
        conn.close()

    assert row == ("2026-03-11T05:06:07+00:00",)


def test_write_migration_state_rejects_malformed_quaid_now(monkeypatch):
    monkeypatch.setenv("QUAID_NOW", "not-a-date")
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(db_migration._MIGRATION_TABLE_SQL)
        with pytest.raises(ValueError, match="Invalid QUAID_NOW"):
            db_migration._write_migration_state(conn, "/tmp/legacy.db", "doc_chunks", "sig")
    finally:
        conn.close()
