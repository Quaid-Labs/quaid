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


def test_migrate_legacy_docs_tables_rejects_malformed_quaid_now_without_done_cache(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "target.db"
    source = tmp_path / "source.db"
    for path in (target, source):
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("CREATE TABLE doc_registry (id TEXT PRIMARY KEY, path TEXT)")
        finally:
            conn.close()
    source_conn = sqlite3.connect(str(source))
    try:
        source_conn.execute(
            "INSERT INTO doc_registry (id, path) VALUES (?, ?)",
            ("d1", "docs/a.md"),
        )
        source_conn.commit()
    finally:
        source_conn.close()

    monkeypatch.setattr(db_migration, "_legacy_instance_db_paths", lambda _target: [source])
    db_migration._PROCESS_DONE_KEYS.clear()

    monkeypatch.setenv("QUAID_NOW", "not-a-date")
    with pytest.raises(ValueError, match="Invalid QUAID_NOW"):
        db_migration.migrate_legacy_docs_tables(target, ("doc_registry",))

    monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:06:07Z")
    db_migration.migrate_legacy_docs_tables(target, ("doc_registry",))

    target_conn = sqlite3.connect(str(target))
    try:
        copied = target_conn.execute("SELECT path FROM doc_registry WHERE id = ?", ("d1",)).fetchone()
        state = target_conn.execute(
            "SELECT migrated_at FROM docs_db_migration_state WHERE source_db = ? AND table_name = ?",
            (str(source), "doc_registry"),
        ).fetchone()
    finally:
        target_conn.close()

    assert copied == ("docs/a.md",)
    assert state == ("2026-03-11T05:06:07+00:00",)
