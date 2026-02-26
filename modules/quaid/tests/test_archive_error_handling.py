import pytest

import lib.archive as archive


def test_search_archive_returns_empty_when_fail_hard_disabled(monkeypatch):
    monkeypatch.setattr(archive, "is_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(archive, "_get_archive_conn", lambda _db_path=None: (_ for _ in ()).throw(RuntimeError("db down")))
    assert archive.search_archive("hello", db_path=None) == []


def test_search_archive_raises_when_fail_hard_enabled(monkeypatch):
    monkeypatch.setattr(archive, "is_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(archive, "_get_archive_conn", lambda _db_path=None: (_ for _ in ()).throw(RuntimeError("db down")))
    with pytest.raises(RuntimeError, match="fail-hard mode"):
        archive.search_archive("hello", db_path=None)
