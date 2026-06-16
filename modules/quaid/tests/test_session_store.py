import json

import pytest

from datastore.sessiondb import session_store


def _insert_lifecycle_observation(tmp_path, monkeypatch, *, metadata_json: str) -> None:
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    with session_store.get_connection(session_store._session_db_path()) as conn:
        session_store.ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO session_lifecycle_observations (
                event_id, owner_id, session_id, event_name, observed_at, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "evt-bad-metadata",
                "owner-life",
                "sess-life",
                "session.reset",
                "2026-03-11T00:00:00+00:00",
                metadata_json,
                "2026-03-11T00:00:00+00:00",
            ),
        )


def test_lifecycle_observations_raise_on_bad_metadata_when_failhard(monkeypatch, tmp_path):
    _insert_lifecycle_observation(tmp_path, monkeypatch, metadata_json="{bad json")
    monkeypatch.setattr(session_store, "is_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="invalid metadata_json") as exc:
        session_store.list_lifecycle_observations(owner_id="owner-life", session_id="sess-life")

    assert exc.value.__cause__ is not None


def test_lifecycle_observations_warn_and_default_bad_metadata_when_fail_open(
    monkeypatch,
    tmp_path,
    caplog,
):
    _insert_lifecycle_observation(tmp_path, monkeypatch, metadata_json="{bad json")
    monkeypatch.setattr(session_store, "is_fail_hard_enabled", lambda: False)

    with caplog.at_level("WARNING", logger="datastore.sessiondb.session_store"):
        rows = session_store.list_lifecycle_observations(owner_id="owner-life", session_id="sess-life")

    assert rows[0]["metadata"] == {}
    assert "invalid metadata_json" in caplog.text


def test_lifecycle_observations_parse_valid_metadata(monkeypatch, tmp_path):
    _insert_lifecycle_observation(tmp_path, monkeypatch, metadata_json=json.dumps({"ok": True}))

    rows = session_store.list_lifecycle_observations(owner_id="owner-life", session_id="sess-life")

    assert rows[0]["metadata"] == {"ok": True}


def test_store_session_source_text_honors_quaid_now(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_NOW", "2026-03-11T00:00:00Z")

    out = session_store.store_session_source_text(
        text="User: alpha fact\n\nAssistant: noted.",
        owner_id="owner-clock",
        session_id="sess-clock",
    )

    assert out["chunk"]["created_at"] == "2026-03-11T00:00:00+00:00"
    assert out["pairs"][0]["created_at"] == "2026-03-11T00:00:00+00:00"
    assert out["microchunks"][0]["created_at"] == "2026-03-11T00:00:00+00:00"


def test_store_session_source_text_malformed_quaid_now_honors_failhard(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_NOW", "not-a-clock")

    monkeypatch.setattr(session_store, "is_fail_hard_enabled", lambda: True)
    with pytest.raises(RuntimeError, match="Invalid QUAID_NOW"):
        session_store.store_session_source_text(
            text="User: alpha fact\n\nAssistant: noted.",
            owner_id="owner-clock",
            session_id="sess-clock",
        )

    monkeypatch.setattr(session_store, "is_fail_hard_enabled", lambda: False)
    out = session_store.store_session_source_text(
        text="User: beta fact\n\nAssistant: noted.",
        owner_id="owner-clock",
        session_id="sess-clock-open",
    )

    assert out["chunk"]["created_at"] != "not-a-clock"


def test_source_date_logs_unparseable_value_when_fail_open(monkeypatch, caplog):
    monkeypatch.setattr(session_store, "is_fail_hard_enabled", lambda: False)

    with caplog.at_level("WARNING", logger="datastore.sessiondb.session_store"):
        assert session_store._source_date("not a date") is None

    assert "failed to parse session source date 'not a date'" in caplog.text


def test_source_date_raises_on_unparseable_value_when_failhard(monkeypatch, caplog):
    monkeypatch.setattr(session_store, "is_fail_hard_enabled", lambda: True)

    with caplog.at_level("WARNING", logger="datastore.sessiondb.session_store"):
        with pytest.raises(RuntimeError, match="failed to parse session source date") as exc:
            session_store._source_date("not a date")

    assert isinstance(exc.value.__cause__, ValueError)
    assert "failed to parse session source date 'not a date'" in caplog.text


def test_store_session_source_text_logs_missing_lock_cleanup(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setattr(session_store.os, "unlink", lambda _path: (_ for _ in ()).throw(FileNotFoundError("gone")))

    with caplog.at_level("DEBUG", logger="datastore.sessiondb.session_store"):
        out = session_store.store_session_source_text(
            text="User: alpha fact\n\nAssistant: noted.",
            owner_id="owner-lock",
            session_id="sess-lock-cleanup",
        )

    assert out["status"] == "stored"
    assert "sessiondb lock file already removed for session_id=sess-lock-cleanup" in caplog.text


def test_stale_lock_treats_permission_error_as_live_process(monkeypatch, tmp_path, caplog):
    lock_path = tmp_path / "session.lock"
    lock_path.write_text("12345", encoding="utf-8")

    def _kill(_pid, _signal):
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(session_store.os, "kill", _kill)

    with caplog.at_level("WARNING", logger="datastore.sessiondb.session_store"):
        assert session_store._stale_lock(str(lock_path)) is False

    assert "is not signalable; treating as live" in caplog.text


def test_with_session_lock_logs_missing_stale_lock_unlink(monkeypatch, tmp_path, caplog):
    lock_path = tmp_path / "session.lock"
    opened = {"count": 0}

    def _open(_path, _flags, _mode):
        opened["count"] += 1
        if opened["count"] == 1:
            raise FileExistsError("exists")
        return 123

    monkeypatch.setattr(session_store, "_lock_path", lambda _session_id: str(lock_path))
    monkeypatch.setattr(session_store, "_stale_lock", lambda _path: True)
    monkeypatch.setattr(session_store.os, "open", _open)
    monkeypatch.setattr(session_store.os, "write", lambda _fd, _data: None)
    monkeypatch.setattr(session_store.os, "unlink", lambda _path: (_ for _ in ()).throw(FileNotFoundError("gone")))

    with caplog.at_level("DEBUG", logger="datastore.sessiondb.session_store"):
        fd, path = session_store._with_session_lock("sess-lock")

    assert fd == 123
    assert path == str(lock_path)
    assert "sessiondb stale lock already removed for session_id=sess-lock" in caplog.text


def test_stale_lock_logs_corrupt_pid(tmp_path, caplog):
    lock_path = tmp_path / "session.lock"
    lock_path.write_text("not-a-pid", encoding="utf-8")

    with caplog.at_level("WARNING", logger="datastore.sessiondb.session_store"):
        assert session_store._stale_lock(str(lock_path)) is True

    assert "contains invalid pid" in caplog.text


def test_stale_lock_treats_missing_process_as_stale(monkeypatch, tmp_path):
    lock_path = tmp_path / "session.lock"
    lock_path.write_text("12345", encoding="utf-8")

    def _kill(_pid, _signal):
        raise ProcessLookupError("no such process")

    monkeypatch.setattr(session_store.os, "kill", _kill)

    assert session_store._stale_lock(str(lock_path)) is True
