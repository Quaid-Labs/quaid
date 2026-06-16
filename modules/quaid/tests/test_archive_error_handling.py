import pytest
import sqlite3

import lib.archive as archive
from datastore.memorydb import archive_store


def test_search_archive_returns_empty_when_fail_hard_disabled(monkeypatch):
    monkeypatch.setattr(archive_store, "is_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(archive_store, "_get_archive_conn", lambda _db_path=None: (_ for _ in ()).throw(RuntimeError("db down")))
    assert archive.search_archive("hello", db_path=None) == []


def test_search_archive_raises_when_fail_hard_enabled(monkeypatch):
    monkeypatch.setattr(archive_store, "is_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(archive_store, "_get_archive_conn", lambda _db_path=None: (_ for _ in ()).throw(RuntimeError("db down")))
    with pytest.raises(RuntimeError, match="fail-hard mode"):
        archive.search_archive("hello", db_path=None)


def test_archive_node_returns_false_when_fail_hard_disabled(monkeypatch):
    monkeypatch.setattr(archive_store, "is_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(archive_store, "_get_archive_conn", lambda _db_path=None: (_ for _ in ()).throw(RuntimeError("db down")))
    assert archive.archive_node({"id": "n1"}, "test", db_path=None) is False


def test_archive_node_raises_when_fail_hard_enabled(monkeypatch):
    monkeypatch.setattr(archive_store, "is_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(archive_store, "_get_archive_conn", lambda _db_path=None: (_ for _ in ()).throw(RuntimeError("db down")))
    with pytest.raises(RuntimeError, match="fail-hard mode"):
        archive.archive_node({"id": "n1"}, "test", db_path=None)


def test_archive_node_honors_quaid_now(monkeypatch, tmp_path):
    archive_db = tmp_path / "archive.db"
    monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:06:07Z")

    assert archive.archive_node({"id": "n-clock", "name": "Clocked archive"}, "test", db_path=archive_db) is True

    conn = sqlite3.connect(archive_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT archived_at FROM archived_nodes WHERE id = ?", ("n-clock",)).fetchone()
    finally:
        conn.close()

    assert row["archived_at"] == "2026-03-11T05:06:07+00:00"
