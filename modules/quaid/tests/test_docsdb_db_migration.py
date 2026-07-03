import sqlite3
import logging
from pathlib import Path

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


def test_migrate_legacy_docs_tables_skips_signature_failures_with_warning(
    tmp_path,
    monkeypatch,
    caplog,
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
    monkeypatch.setattr(db_migration, "_db_signature", lambda _path: (_ for _ in ()).throw(OSError("stat failed")))
    monkeypatch.setattr(db_migration, "is_fail_hard_enabled", lambda: False)
    db_migration._PROCESS_DONE_KEYS.clear()

    with caplog.at_level(logging.WARNING, logger="datastore.docsdb.db_migration"):
        db_migration.migrate_legacy_docs_tables(target, ("doc_registry",))

    target_conn = sqlite3.connect(str(target))
    try:
        copied = target_conn.execute("SELECT path FROM doc_registry WHERE id = ?", ("d1",)).fetchone()
        state = target_conn.execute("SELECT source_sig FROM docs_db_migration_state").fetchall()
    finally:
        target_conn.close()

    assert copied is None
    assert state == []
    assert "failed reading DB signature" in caplog.text
    assert "stat failed" in caplog.text


def test_migrate_legacy_docs_tables_signature_failure_raises_under_failhard(
    tmp_path,
    monkeypatch,
    caplog,
):
    target = tmp_path / "target.db"
    source = tmp_path / "source.db"
    for path in (target, source):
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("CREATE TABLE doc_registry (id TEXT PRIMARY KEY, path TEXT)")
        finally:
            conn.close()

    monkeypatch.setattr(db_migration, "_legacy_instance_db_paths", lambda _target: [source])
    monkeypatch.setattr(db_migration, "_db_signature", lambda _path: (_ for _ in ()).throw(OSError("stat failed")))
    monkeypatch.setattr(db_migration, "is_fail_hard_enabled", lambda: True)
    db_migration._PROCESS_DONE_KEYS.clear()

    with caplog.at_level(logging.WARNING, logger="datastore.docsdb.db_migration"):
        with pytest.raises(RuntimeError, match="Legacy docs DB signature read failed") as excinfo:
            db_migration.migrate_legacy_docs_tables(target, ("doc_registry",))

    assert isinstance(excinfo.value.__cause__, OSError)
    assert "failed reading DB signature" in caplog.text


def test_migrate_legacy_docs_tables_inner_merge_failure_raises_under_failhard(
    tmp_path,
    monkeypatch,
    caplog,
):
    target = tmp_path / "target.db"
    source = tmp_path / "source.db"
    for path in (target, source):
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("CREATE TABLE doc_registry (id TEXT PRIMARY KEY, path TEXT)")
        finally:
            conn.close()

    monkeypatch.setattr(db_migration, "_legacy_instance_db_paths", lambda _target: [source])
    monkeypatch.setattr(db_migration, "_copy_table_rows", lambda *_args: (_ for _ in ()).throw(RuntimeError("copy failed")))
    monkeypatch.setattr(db_migration, "is_fail_hard_enabled", lambda: True)
    db_migration._PROCESS_DONE_KEYS.clear()

    with caplog.at_level(logging.WARNING, logger="datastore.docsdb.db_migration"):
        with pytest.raises(RuntimeError, match="Legacy docs DB merge failed") as excinfo:
            db_migration.migrate_legacy_docs_tables(target, ("doc_registry",))

    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert "copy failed" in caplog.text


def test_migrate_legacy_docs_tables_detach_failure_raises_under_failhard(
    tmp_path,
    monkeypatch,
    caplog,
):
    target = tmp_path / "target.db"
    source = tmp_path / "source.db"
    for path in (target, source):
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("CREATE TABLE doc_registry (id TEXT PRIMARY KEY, path TEXT)")
        finally:
            conn.close()

    real_connect = sqlite3.connect

    class ConnWrapper:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args, **kwargs):
            if str(sql).lstrip().upper().startswith("DETACH DATABASE"):
                raise sqlite3.OperationalError("detach failed")
            return self._conn.execute(sql, *args, **kwargs)

        def commit(self):
            return self._conn.commit()

        def close(self):
            return self._conn.close()

    monkeypatch.setattr(db_migration, "_legacy_instance_db_paths", lambda _target: [source])
    monkeypatch.setattr(db_migration.sqlite3, "connect", lambda path: ConnWrapper(real_connect(path)))
    monkeypatch.setattr(db_migration, "is_fail_hard_enabled", lambda: True)
    db_migration._PROCESS_DONE_KEYS.clear()

    with caplog.at_level(logging.WARNING, logger="datastore.docsdb.db_migration"):
        with pytest.raises(RuntimeError, match="Failed detaching legacy docs migration database") as excinfo:
            db_migration.migrate_legacy_docs_tables(target, ("doc_registry",))

    assert isinstance(excinfo.value.__cause__, sqlite3.OperationalError)
    assert "Failed detaching legacy docs migration database" in caplog.text


def test_migrate_legacy_docs_tables_logs_target_resolution_failure(
    monkeypatch,
    caplog,
):
    class BrokenTarget:
        def expanduser(self):
            return self

        def resolve(self):
            raise OSError("resolve failed")

        def __str__(self):
            return "/tmp/broken-target.db"

    calls = []
    db_migration._PROCESS_DONE_KEYS.clear()
    monkeypatch.setattr(db_migration, "Path", lambda _path: BrokenTarget())
    monkeypatch.setattr(db_migration, "_migrate_locked", lambda target, wanted: calls.append((target, wanted)))
    monkeypatch.setattr(db_migration, "is_fail_hard_enabled", lambda: False)

    with caplog.at_level(logging.WARNING, logger="datastore.docsdb.db_migration"):
        db_migration.migrate_legacy_docs_tables("/unused", ("doc_registry",))

    assert calls
    assert "Failed resolving docs DB migration target path /tmp/broken-target.db" in caplog.text
    assert "resolve failed" in caplog.text


def test_migrate_legacy_docs_tables_target_resolution_failure_raises_under_failhard(
    monkeypatch,
):
    class BrokenTarget:
        def expanduser(self):
            return self

        def resolve(self):
            raise OSError("resolve failed")

        def __str__(self):
            return "/tmp/broken-target.db"

    db_migration._PROCESS_DONE_KEYS.clear()
    monkeypatch.setattr(db_migration, "Path", lambda _path: BrokenTarget())
    monkeypatch.setattr(db_migration, "_migrate_locked", lambda _target, _wanted: None)
    monkeypatch.setattr(db_migration, "is_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="Failed resolving docs DB migration target path") as excinfo:
        db_migration.migrate_legacy_docs_tables("/unused", ("doc_registry",))
    assert isinstance(excinfo.value.__cause__, OSError)


def test_legacy_instance_db_path_compare_failure_raises_under_failhard(tmp_path, monkeypatch, caplog):
    home = tmp_path / "home"
    candidate = home / "instances" / "alpha" / "data" / "memory.db"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("")
    target = tmp_path / "target.db"

    real_resolve = Path.resolve

    def fail_resolve(path, *args, **kwargs):
        if path in {candidate, target}:
            raise OSError("resolve failed")
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(db_migration, "_quaid_home", lambda: home)
    monkeypatch.setattr(db_migration.Path, "resolve", fail_resolve)
    monkeypatch.setattr(db_migration, "is_fail_hard_enabled", lambda: True)

    with caplog.at_level(logging.WARNING, logger="datastore.docsdb.db_migration"):
        with pytest.raises(RuntimeError, match="Failed comparing legacy docs DB path") as excinfo:
            db_migration._legacy_instance_db_paths(target)

    assert isinstance(excinfo.value.__cause__, OSError)
    assert "Failed comparing legacy docs DB path" in caplog.text
