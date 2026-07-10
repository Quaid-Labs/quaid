import logging
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


def test_default_archive_path_uses_adapter_fallback_when_fail_hard_disabled(monkeypatch, tmp_path, caplog):
    import lib.adapter as adapter_mod
    import lib.config as config_mod

    class FakeAdapter:
        def data_dir(self):
            return tmp_path / "data"

    monkeypatch.setattr(archive_store, "is_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(config_mod, "get_archive_db_path", lambda: (_ for _ in ()).throw(RuntimeError("config down")))
    monkeypatch.setattr(adapter_mod, "get_adapter", lambda: FakeAdapter())

    with caplog.at_level(logging.WARNING, logger="datastore.memorydb.archive_store"):
        assert archive_store._default_archive_path() == tmp_path / "data" / "memory_archive.db"

    assert "Failed resolving archive db path from config" in caplog.text
    assert "config down" in caplog.text


def test_default_archive_path_raises_config_failure_when_fail_hard_enabled(monkeypatch, caplog):
    import lib.adapter as adapter_mod
    import lib.config as config_mod

    def fail_get_adapter():
        raise AssertionError("adapter fallback should not run")

    monkeypatch.setattr(archive_store, "is_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(config_mod, "get_archive_db_path", lambda: (_ for _ in ()).throw(RuntimeError("config down")))
    monkeypatch.setattr(adapter_mod, "get_adapter", fail_get_adapter)

    with caplog.at_level(logging.WARNING, logger="datastore.memorydb.archive_store"):
        with pytest.raises(RuntimeError, match="config down"):
            archive_store._default_archive_path()

    assert "Failed resolving archive db path from config" in caplog.text


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
