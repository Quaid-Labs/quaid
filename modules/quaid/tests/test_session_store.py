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


def test_stale_lock_treats_permission_error_as_live_process(monkeypatch, tmp_path):
    lock_path = tmp_path / "session.lock"
    lock_path.write_text("12345", encoding="utf-8")

    def _kill(_pid, _signal):
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(session_store.os, "kill", _kill)

    assert session_store._stale_lock(str(lock_path)) is False


def test_stale_lock_treats_missing_process_as_stale(monkeypatch, tmp_path):
    lock_path = tmp_path / "session.lock"
    lock_path.write_text("12345", encoding="utf-8")

    def _kill(_pid, _signal):
        raise ProcessLookupError("no such process")

    monkeypatch.setattr(session_store.os, "kill", _kill)

    assert session_store._stale_lock(str(lock_path)) is True
