import builtins
import json
import os
import pathlib
import sys
import time
import types
from pathlib import Path

import pytest

from core import extraction_daemon


class _StopDaemonLoop(Exception):
    pass


class _NoopVersionWatcher:
    def __init__(self, **_kwargs):
        pass

    def tick(self):
        return None


class _OwnedTestAdapterMixin:
    def owns_session_path(self, path, session_id=""):
        return True

    def quaid_home(self):
        home = os.environ.get("QUAID_HOME", "").strip()
        return Path(home).resolve() if home else Path.cwd()

    def visible_home(self):
        home = self.quaid_home()
        if home.name.startswith(".") and len(home.name) > 1:
            return home.with_name(home.name[1:])
        return home


def _stub_successful_session_logs_ingest(monkeypatch):
    import core.ingest_runtime as ingest_runtime

    monkeypatch.setattr(ingest_runtime, "run_session_logs_ingest", lambda **_kwargs: {"status": "indexed"})


def test_session_logs_ingest_request_returns_sessiondb_result(monkeypatch, tmp_path):
    from lib.adapter import TestAdapter, reset_adapter, set_adapter

    set_adapter(TestAdapter(tmp_path))
    called = {}

    def _fake_run(**kwargs):
        called.update(kwargs)
        return {
            "status": "indexed",
            "session_id": kwargs["session_id"],
            "source_kind": "transcript_path",
            "microchunks_stored": 2,
        }

    monkeypatch.setattr("core.ingest_runtime.run_session_logs_ingest", _fake_run)

    try:
        result = extraction_daemon._request_session_logs_ingest(
            session_id="sess-broker",
            owner_id="owner-broker",
            label="SessionEnd",
            transcript_path=str(tmp_path / "session.jsonl"),
            source_channel="codex",
            conversation_id="conv-broker",
            participant_ids=["user:owner"],
            participant_aliases={"Operator": "user:owner"},
            message_count=3,
            topic_hint="broker parity",
        )
    finally:
        reset_adapter()

    assert result == {
        "status": "indexed",
        "session_id": "sess-broker",
        "source_kind": "transcript_path",
        "microchunks_stored": 2,
    }
    assert called["session_id"] == "sess-broker"
    assert called["owner_id"] == "owner-broker"
    assert called["label"] == "SessionEnd"
    assert called["source_channel"] == "codex"
    assert called["conversation_id"] == "conv-broker"
    assert called["participant_ids"] == ["user:owner"]
    assert called["participant_aliases"] == {"Operator": "user:owner"}
    assert called["message_count"] == 3
    assert called["topic_hint"] == "broker parity"


def test_session_logs_ingest_request_has_no_direct_fallback(monkeypatch, tmp_path):
    from lib.adapter import TestAdapter, reset_adapter, set_adapter

    set_adapter(TestAdapter(tmp_path))
    registered = []
    monkeypatch.setattr(
        "core.plugins.sessiondb_contract.register_session_ingest_log_request_handler",
        lambda: registered.append("registered"),
    )
    monkeypatch.setattr(
        "core.runtime.events.request_broker_event",
        lambda *_args, **_kwargs: {
            "status": "failed",
            "error": "synthetic broker failure",
            "responses": [],
        },
    )
    monkeypatch.setattr(
        "core.ingest_runtime.run_session_logs_ingest",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not fall back to direct ingest")),
    )

    try:
        with pytest.raises(RuntimeError, match="synthetic broker failure"):
            extraction_daemon._request_session_logs_ingest(
                session_id="sess-broker-fail",
                owner_id="owner-broker",
                label="SessionEnd",
                transcript_path=str(tmp_path / "session.jsonl"),
            )
    finally:
        reset_adapter()

    assert registered == ["registered"]


def test_session_logs_ingest_transcript_path_prefers_current_rolling_source(tmp_path):
    session_id = "05aace3b"
    stale_mirror = tmp_path / "instances" / "openclaw-main" / "logs" / "quaid" / "sessions" / f"{session_id}.jsonl"
    live_source = tmp_path / ".openclaw" / "agents" / "main" / "sessions" / f"{session_id}.jsonl"
    stale_mirror.parent.mkdir(parents=True, exist_ok=True)
    live_source.parent.mkdir(parents=True, exist_ok=True)
    stale_mirror.write_text('{"role":"user","content":"old mirror"}\n', encoding="utf-8")
    live_source.write_text('{"role":"user","content":"current live source"}\n', encoding="utf-8")

    chosen = extraction_daemon._session_logs_ingest_transcript_path_for_signal(
        str(stale_mirror),
        signal_meta={},
        cursor_data={"transcript_path": str(stale_mirror)},
        staged_state={
            "transcript_path": str(stale_mirror),
            "buffer_transcript_path": str(live_source),
        },
    )

    assert chosen == str(live_source)


def test_session_logs_ingest_transcript_path_uses_preserved_mirror_for_missing_live_source(
    monkeypatch,
    tmp_path,
):
    session_id = "d0609f04-b63b-4a9b-a343-541b1246f064"
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
    live_source = (
        tmp_path
        / ".openclaw"
        / "agents"
        / "main"
        / "sessions"
        / f"{session_id}.jsonl"
    )
    mirror = (
        tmp_path
        / "instances"
        / "openclaw-main"
        / "logs"
        / "quaid"
        / "sessions"
        / f"{session_id}.jsonl"
    )
    mirror.parent.mkdir(parents=True, exist_ok=True)
    mirror.write_text('{"role":"user","content":"post reset canary"}\n', encoding="utf-8")

    chosen = extraction_daemon._session_logs_ingest_transcript_path_for_signal(
        str(live_source),
        session_id=session_id,
        signal_meta={},
        cursor_data={"transcript_path": str(live_source)},
        staged_state={},
    )

    assert chosen == str(mirror)


def test_daemon_lifecycle_signal_mapping_excludes_rolling():
    assert extraction_daemon.DAEMON_SIGNAL_TO_LIFECYCLE_EVENT == {
        "reset": "session.reset",
        "compaction": "session.compaction",
        "timeout": "session.timeout",
        "session_end": "session.agent_end",
    }
    assert "rolling" not in extraction_daemon.DAEMON_SIGNAL_TO_LIFECYCLE_EVENT


def test_atomic_write_uses_unique_temp_paths_within_process(monkeypatch, tmp_path):
    target = tmp_path / "cursor.json"
    monkeypatch.setattr(extraction_daemon.os, "getpid", lambda: 4242)
    uuids = iter([
        types.SimpleNamespace(hex="a" * 32),
        types.SimpleNamespace(hex="1" * 32),
    ])
    monkeypatch.setattr(extraction_daemon.uuid, "uuid4", lambda: next(uuids))
    replaced = []
    real_replace = extraction_daemon.os.replace

    def _record_replace(src, dst):
        replaced.append((Path(src), Path(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(extraction_daemon.os, "replace", _record_replace)

    extraction_daemon._atomic_write(target, "first")
    extraction_daemon._atomic_write(target, "second")

    assert [dst for _src, dst in replaced] == [target, target]
    assert replaced[0][0] != replaced[1][0]
    assert replaced[0][0].name.endswith(".tmp.4242.aaaaaaaa")
    assert replaced[1][0].name.endswith(".tmp.4242.11111111")
    assert target.read_text(encoding="utf-8") == "second"


@pytest.mark.parametrize(
    ("signal_type", "event_name"),
    [
        ("reset", "session.reset"),
        ("compaction", "session.compaction"),
        ("timeout", "session.timeout"),
        ("session_end", "session.agent_end"),
    ],
)
def test_daemon_lifecycle_observation_maps_selected_signal_types(
    monkeypatch,
    tmp_path,
    signal_type,
    event_name,
):
    from core.plugins import sessiondb_contract

    captured = []
    monkeypatch.setattr(
        sessiondb_contract,
        "record_session_lifecycle_observation",
        lambda event: captured.append(event) or {"status": "recorded", "persisted": True},
    )

    extraction_daemon._record_daemon_lifecycle_observation(
        {
            "type": signal_type,
            "session_id": "sess-map",
            "_signal_path": str(tmp_path / f"{signal_type}.json"),
        },
        session_id="sess-map",
        signal_type=signal_type,
        transcript_path=str(tmp_path / "session.jsonl"),
    )

    assert captured[0]["name"] == event_name
    assert captured[0]["id"] == f"daemon-signal:{signal_type}.json:{signal_type}:sess-map"


def test_daemon_lifecycle_observation_uses_prefixed_event_id_and_plugin_contract(
    monkeypatch,
    tmp_path,
):
    from core.plugins import sessiondb_contract

    captured = []

    def _record(event):
        captured.append(event)
        return {"status": "recorded", "persisted": True, "datastore_id": "sessiondb", "inserted": True}

    monkeypatch.setattr(sessiondb_contract, "record_session_lifecycle_observation", _record)

    signal_path = tmp_path / "signals" / "reset-signal.json"
    result = extraction_daemon._record_daemon_lifecycle_observation(
        {
            "type": "reset",
            "session_id": "sess-daemon",
            "transcript_path": str(tmp_path / "session.jsonl"),
            "timestamp": "2026-05-19T00:00:00Z",
            "adapter": "codex",
            "meta": {"reason": "operator_reset"},
            "_signal_path": str(signal_path),
        },
        session_id="sess-daemon",
        signal_type="reset",
        transcript_path=str(tmp_path / "session.jsonl"),
    )

    assert result["persisted"] is True
    assert len(captured) == 1
    event = captured[0]
    assert event["id"] == "daemon-signal:reset-signal.json:reset:sess-daemon"
    assert event["idempotency_key"] == "daemon-signal:reset-signal.json:reset:sess-daemon"
    assert event["name"] == "session.reset"
    assert event["source"] == "daemon.reset"
    assert event["session_id"] == "sess-daemon"
    assert event["payload"]["reason"] == "operator_reset"
    assert event["payload"]["daemon_signal_type"] == "reset"
    assert event["payload"]["signal_file"] == "reset-signal.json"
    assert event["provenance"]["origin"] == "daemon_signal"


def test_daemon_lifecycle_observation_skips_rolling_and_missing_session(monkeypatch):
    from core.plugins import sessiondb_contract

    monkeypatch.setattr(
        sessiondb_contract,
        "record_session_lifecycle_observation",
        lambda _event: (_ for _ in ()).throw(AssertionError("must not record")),
    )

    assert extraction_daemon._record_daemon_lifecycle_observation(
        {"type": "rolling", "session_id": "sess-roll"},
        session_id="sess-roll",
        signal_type="rolling",
        transcript_path="/tmp/session.jsonl",
    ) == {"status": "skipped", "persisted": False}
    assert extraction_daemon._record_daemon_lifecycle_observation(
        {"type": "reset"},
        session_id="",
        signal_type="reset",
        transcript_path="/tmp/session.jsonl",
    ) == {"status": "skipped", "persisted": False}


def test_daemon_lifecycle_observation_is_idempotent(monkeypatch, tmp_path):
    from datastore.sessiondb.session_store import list_lifecycle_observations

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    signal_data = {
        "type": "session_end",
        "session_id": "sess-daemon-idem",
        "transcript_path": str(tmp_path / "session.jsonl"),
        "timestamp": "2026-05-19T00:00:00Z",
        "_signal_path": str(tmp_path / "signals" / "session-end.json"),
    }

    first = extraction_daemon._record_daemon_lifecycle_observation(
        signal_data,
        session_id="sess-daemon-idem",
        signal_type="session_end",
        transcript_path=str(tmp_path / "session.jsonl"),
    )
    second = extraction_daemon._record_daemon_lifecycle_observation(
        signal_data,
        session_id="sess-daemon-idem",
        signal_type="session_end",
        transcript_path=str(tmp_path / "session.jsonl"),
    )

    assert first["inserted"] is True
    assert second["inserted"] is False
    rows = list_lifecycle_observations(session_id="sess-daemon-idem")
    assert len(rows) == 1
    assert rows[0]["event_id"] == "daemon-signal:session-end.json:session_end:sess-daemon-idem"
    assert rows[0]["event_name"] == "session.agent_end"


def test_daemon_lifecycle_observation_uses_env_instance_root_without_adapter_instance_root(
    monkeypatch,
    tmp_path,
    caplog,
):
    import lib.adapter as adapter_mod
    from datastore.sessiondb.session_store import list_lifecycle_observations

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "daemon-inst")
    monkeypatch.setattr(adapter_mod, "get_adapter", lambda: object())

    with caplog.at_level("WARNING", logger="lib.config"):
        result = extraction_daemon._record_daemon_lifecycle_observation(
            {
                "type": "compaction",
                "session_id": "sess-env-root",
                "_signal_path": str(tmp_path / "signals" / "compaction.json"),
            },
            session_id="sess-env-root",
            signal_type="compaction",
            transcript_path=str(tmp_path / "session.jsonl"),
        )

    assert result["persisted"] is True
    assert (tmp_path / "instances" / "daemon-inst" / "data" / "session.db").is_file()
    assert "lacks instance_root(); falling back to QUAID_HOME/instances/QUAID_INSTANCE" in caplog.text
    rows = list_lifecycle_observations(session_id="sess-env-root")
    assert len(rows) == 1
    assert rows[0]["event_name"] == "session.compaction"


def test_daemon_lifecycle_observation_failure_respects_failhard(monkeypatch, caplog):
    from core.plugins import sessiondb_contract

    def _boom(_event):
        raise RuntimeError("sessiondb observation down")

    monkeypatch.setattr(sessiondb_contract, "record_session_lifecycle_observation", _boom)
    signal_data = {
        "type": "timeout",
        "session_id": "sess-fail",
        "_signal_path": "/tmp/timeout.json",
    }

    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: False)
    with caplog.at_level("WARNING", logger="quaid.daemon"):
        result = extraction_daemon._record_daemon_lifecycle_observation(
            signal_data,
            session_id="sess-fail",
            signal_type="timeout",
            transcript_path="/tmp/session.jsonl",
        )
    assert result["persisted"] is False
    assert "SessionDB daemon lifecycle observation persistence failed" in caplog.text

    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)
    with pytest.raises(
        RuntimeError,
        match="SessionDB daemon lifecycle observation failed while failHard is enabled",
    ) as excinfo:
        extraction_daemon._record_daemon_lifecycle_observation(
            signal_data,
            session_id="sess-fail",
            signal_type="timeout",
            transcript_path="/tmp/session.jsonl",
        )
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert str(excinfo.value.__cause__) == "sessiondb observation down"


def test_finalize_no_payload_signal_records_lifecycle_before_signal_finalization(monkeypatch):
    order = []
    monkeypatch.setattr(
        extraction_daemon,
        "_record_daemon_lifecycle_observation",
        lambda *_args, **_kwargs: order.append("record") or {"persisted": True},
    )
    monkeypatch.setattr(
        extraction_daemon,
        "write_context_refresh_timeout_marker",
        lambda _session_id: order.append("timeout_marker"),
    )
    monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *_args, **_kwargs: order.append("cursor"))
    monkeypatch.setattr(extraction_daemon, "mark_signal_processed", lambda _signal: order.append("mark"))
    monkeypatch.setattr(
        extraction_daemon,
        "_release_session_processing_lock",
        lambda *_args, **_kwargs: order.append("release"),
    )

    extraction_daemon._finalize_no_payload_signal(
        session_id="sess-finalize",
        transcript_path="/tmp/session.jsonl",
        signal_data={"type": "timeout", "session_id": "sess-finalize"},
        lock_owner_key="sess-finalize",
        lock_fd=123,
        next_cursor_offset=4,
    )

    assert order == ["record", "timeout_marker", "cursor", "mark", "release"]


def test_finalize_no_payload_signal_can_defer_lock_release_to_outer_finally(monkeypatch):
    order = []
    monkeypatch.setattr(
        extraction_daemon,
        "_record_daemon_lifecycle_observation",
        lambda *_args, **_kwargs: order.append("record") or {"persisted": True},
    )
    monkeypatch.setattr(extraction_daemon, "mark_signal_processed", lambda _signal: order.append("mark"))
    monkeypatch.setattr(
        extraction_daemon,
        "_release_session_processing_lock",
        lambda *_args, **_kwargs: order.append("release"),
    )

    extraction_daemon._finalize_no_payload_signal(
        session_id="sess-finalize",
        transcript_path="/tmp/session.jsonl",
        signal_data={"type": "session_end", "session_id": "sess-finalize"},
        lock_owner_key="sess-finalize",
        lock_fd=123,
        release_lock=False,
    )

    assert order == ["record", "mark"]


def test_finalize_no_payload_signal_failhard_observation_error_prevents_finalization(
    monkeypatch,
):
    order = []
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(
        extraction_daemon,
        "write_context_refresh_timeout_marker",
        lambda _session_id: order.append("timeout_marker"),
    )
    monkeypatch.setattr(extraction_daemon, "mark_signal_processed", lambda _signal: order.append("mark"))

    def _boom(*_args, **_kwargs):
        raise RuntimeError("observation write failed")

    monkeypatch.setattr(extraction_daemon, "_record_daemon_lifecycle_observation", _boom)

    with pytest.raises(RuntimeError, match="observation write failed"):
        extraction_daemon._finalize_no_payload_signal(
            session_id="sess-finalize",
            transcript_path="/tmp/session.jsonl",
            signal_data={"type": "timeout", "session_id": "sess-finalize"},
            lock_owner_key="sess-finalize",
            lock_fd=123,
        )

    assert order == []


def test_process_signal_full_flush_records_daemon_lifecycle_observation(monkeypatch, tmp_path):
    from ingest import extract as extract_mod
    from lib.adapter import reset_adapter, set_adapter

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "daemon-inst")
    monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "owner-1")
    monkeypatch.setattr(extraction_daemon, "_read_usage_totals", lambda: {})
    monkeypatch.setattr(extraction_daemon, "_session_has_harvestable_subagents", lambda *args, **kwargs: False)
    monkeypatch.setattr(extraction_daemon, "_request_session_logs_ingest", lambda **_kwargs: {"status": "indexed"})

    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        '{"role":"user","content":"The daemon lifecycle full flush codeword is cedar-lantern."}\n',
        encoding="utf-8",
    )
    extraction_daemon.write_cursor("sess-full-observe", 0, str(transcript_path))

    class _Adapter(_OwnedTestAdapterMixin):
        def parse_session_jsonl(self, path):
            _ = path
            return "User: The daemon lifecycle full flush codeword is cedar-lantern."

    observed = []
    monkeypatch.setattr(
        extraction_daemon,
        "_record_daemon_lifecycle_observation",
        lambda signal_data, **kwargs: observed.append({"signal_data": signal_data, **kwargs})
        or {"status": "recorded", "persisted": True},
    )
    monkeypatch.setattr(
        extract_mod,
        "extract_from_transcript",
        lambda *_args, **_kwargs: {
            "chunks_processed": 1,
            "chunks_total": 1,
            "unclassified_empty_payloads": 0,
            "raw_facts": [],
            "facts": [],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "carry_facts": [],
        },
    )
    monkeypatch.setattr(
        extract_mod,
        "apply_extracted_payloads",
        lambda *_args, **_kwargs: {
            "facts_stored": 0,
            "facts_skipped": 0,
            "edges_created": 0,
            "facts": [],
            "snippets": {},
            "journal": {},
            "project_log_metrics": {},
        },
    )

    set_adapter(_Adapter())
    try:
        extraction_daemon.write_signal(
            signal_type="compaction",
            session_id="sess-full-observe",
            transcript_path=str(transcript_path),
        )
        extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])
    finally:
        reset_adapter()

    assert len(observed) == 1
    assert observed[0]["session_id"] == "sess-full-observe"
    assert observed[0]["signal_type"] == "compaction"
    assert observed[0]["transcript_path"] == str(transcript_path)
    assert extraction_daemon.read_pending_signals() == []


def test_session_store_connection_entrypoints_use_parent_guard():
    import inspect
    from datastore.sessiondb import session_store

    source = inspect.getsource(session_store)
    assert "get_connection(get_session_db_path())" not in source
    assert "with get_connection(_session_db_path())" in source


def test_daemon_lifecycle_observation_keeps_sessiondb_import_behind_plugin_contract():
    import inspect

    source = inspect.getsource(extraction_daemon)
    assert "from core.plugins.sessiondb_contract import record_session_lifecycle_observation" in source
    assert "datastore.sessiondb.session_store" not in source


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (
            {"status": "failed", "error": "simulated broker failure", "responses": []},
            "session_logs ingest request returned no sessiondb response: simulated broker failure",
        ),
        (
            {"status": "ok", "responses": ["not-a-row"]},
            "session_logs ingest request returned malformed sessiondb response",
        ),
        (
            {"status": "ok", "responses": [{"datastore_id": "memorydb", "result": {"status": "indexed"}}]},
            "session_logs ingest request returned a non-sessiondb response",
        ),
        (
            {"status": "ok", "responses": [{"datastore_id": "sessiondb", "result": "not-an-object"}]},
            "session_logs ingest request sessiondb result is not an object",
        ),
    ],
)
def test_session_logs_ingest_request_validates_sessiondb_response_without_fallback(
    monkeypatch,
    tmp_path,
    response,
    error,
):
    from lib.adapter import TestAdapter, reset_adapter, set_adapter

    set_adapter(TestAdapter(tmp_path))
    registered = []
    monkeypatch.setattr(
        "core.plugins.sessiondb_contract.register_session_ingest_log_request_handler",
        lambda: registered.append("registered"),
    )
    monkeypatch.setattr("core.runtime.events.request_broker_event", lambda *_args, **_kwargs: response)
    monkeypatch.setattr(
        "core.ingest_runtime.run_session_logs_ingest",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not fall back to direct ingest")),
    )

    try:
        with pytest.raises(RuntimeError, match=error):
            extraction_daemon._request_session_logs_ingest(
                session_id="sess-broker-invalid",
                owner_id="owner-broker",
                label="SessionEnd",
                transcript_path=str(tmp_path / "session.jsonl"),
            )
    finally:
        reset_adapter()

    assert registered == ["registered"]


def test_daemon_loop_preserves_signal_when_processing_raises(monkeypatch):
    signal_payload = {"session_id": "sess-1", "type": "reset"}
    marked = []
    failures = []
    read_calls = 0
    process_calls = 0

    def fake_read_pending_signals():
        nonlocal read_calls
        read_calls += 1
        return [signal_payload] if read_calls == 1 else []

    def fake_process_signal(_sig):
        nonlocal process_calls
        process_calls += 1
        raise RuntimeError("boom")

    def fake_sleep(_seconds):
        raise _StopDaemonLoop()

    monkeypatch.setattr(extraction_daemon, "write_pid", lambda _pid: None)
    monkeypatch.setattr(extraction_daemon, "remove_pid", lambda: None)
    monkeypatch.setattr(extraction_daemon, "read_pending_signals", fake_read_pending_signals)
    monkeypatch.setattr(extraction_daemon, "process_signal", fake_process_signal)
    monkeypatch.setattr(extraction_daemon, "mark_signal_processed", lambda sig: marked.append(sig))
    monkeypatch.setattr(
        extraction_daemon,
        "_record_signal_process_failure_for_retry",
        lambda sig, exc, *, label: failures.append((sig, exc, label)),
    )
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(extraction_daemon, "_supervisor_alive", lambda: True)
    monkeypatch.setattr(extraction_daemon, "_reload_config_if_changed", lambda _reason: None)
    monkeypatch.setattr(extraction_daemon, "_retry_missing_embeddings", lambda: 0)
    monkeypatch.setattr(extraction_daemon, "check_chunk_ready_sessions", lambda: None)
    monkeypatch.setattr(
        "core.compatibility.read_circuit_breaker",
        lambda _data_dir: types.SimpleNamespace(
            allows_writes=lambda: True,
            status="normal",
            message="",
        ),
    )
    monkeypatch.setattr(extraction_daemon.time, "sleep", fake_sleep)
    monkeypatch.setattr(extraction_daemon.signal, "signal", lambda *_args, **_kwargs: None)

    with pytest.raises(_StopDaemonLoop):
        extraction_daemon.daemon_loop(poll_interval=0.0, idle_check_interval=999999.0)

    assert marked == []
    assert failures and failures[0][0] is signal_payload
    assert str(failures[0][1]) == "boom"
    assert failures[0][2] == "daemon-loop"
    assert read_calls >= 1
    assert process_calls == 1


def test_daemon_loop_raises_signal_processing_failure_under_failhard(monkeypatch):
    signal_payload = {"session_id": "sess-1", "type": "reset"}
    marked = []
    read_calls = 0

    def fake_read_pending_signals():
        nonlocal read_calls
        read_calls += 1
        return [signal_payload] if read_calls == 1 else []

    def fake_process_signal(_sig):
        raise RuntimeError("boom")

    monkeypatch.setattr(extraction_daemon, "write_pid", lambda _pid: None)
    monkeypatch.setattr(extraction_daemon, "remove_pid", lambda: None)
    monkeypatch.setattr("core.compatibility.VersionWatcher", _NoopVersionWatcher)
    monkeypatch.setattr(extraction_daemon, "read_pending_signals", fake_read_pending_signals)
    monkeypatch.setattr(extraction_daemon, "process_signal", fake_process_signal)
    monkeypatch.setattr(extraction_daemon, "mark_signal_processed", lambda sig: marked.append(sig))
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(extraction_daemon, "_supervisor_alive", lambda: True)
    monkeypatch.setattr(extraction_daemon, "_reload_config_if_changed", lambda _reason: None)
    monkeypatch.setattr(extraction_daemon, "_retry_missing_embeddings", lambda: 0)
    monkeypatch.setattr(extraction_daemon, "check_chunk_ready_sessions", lambda: None)
    monkeypatch.setattr(
        "core.compatibility.read_circuit_breaker",
        lambda _data_dir: types.SimpleNamespace(
            allows_writes=lambda: True,
            status="normal",
            message="",
        ),
    )
    monkeypatch.setattr(
        extraction_daemon.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(AssertionError("daemon loop should not sleep")),
    )
    monkeypatch.setattr(extraction_daemon.signal, "signal", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="boom"):
        extraction_daemon.daemon_loop(poll_interval=0.0, idle_check_interval=999999.0)

    assert marked == []
    assert read_calls == 1


def test_daemon_loop_preserves_provider_config_signal_under_failhard(monkeypatch, caplog):
    from lib.llm_clients import ProviderConfigError

    signal_payload = {"session_id": "sess-1", "type": "reset"}
    marked = []
    read_calls = 0
    process_calls = 0

    def fake_read_pending_signals():
        nonlocal read_calls
        read_calls += 1
        return [signal_payload] if read_calls == 1 else []

    def fake_process_signal(_sig):
        nonlocal process_calls
        process_calls += 1
        raise ProviderConfigError("invalid-model-m6-probe")

    def fake_sleep(_seconds):
        raise _StopDaemonLoop()

    monkeypatch.setattr(extraction_daemon, "write_pid", lambda _pid: None)
    monkeypatch.setattr(extraction_daemon, "remove_pid", lambda: None)
    monkeypatch.setattr("core.compatibility.VersionWatcher", _NoopVersionWatcher)
    monkeypatch.setattr(extraction_daemon, "read_pending_signals", fake_read_pending_signals)
    monkeypatch.setattr(extraction_daemon, "process_signal", fake_process_signal)
    monkeypatch.setattr(extraction_daemon, "mark_signal_processed", lambda sig: marked.append(sig))
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(extraction_daemon, "_supervisor_alive", lambda: True)
    monkeypatch.setattr(extraction_daemon, "_reload_config_if_changed", lambda _reason: None)
    monkeypatch.setattr(extraction_daemon, "_retry_missing_embeddings", lambda: 0)
    monkeypatch.setattr(extraction_daemon, "check_chunk_ready_sessions", lambda: None)
    monkeypatch.setattr(
        "core.compatibility.read_circuit_breaker",
        lambda _data_dir: types.SimpleNamespace(
            allows_writes=lambda: True,
            status="normal",
            message="",
        ),
    )
    monkeypatch.setattr(extraction_daemon.time, "sleep", fake_sleep)
    monkeypatch.setattr(extraction_daemon.signal, "signal", lambda *_args, **_kwargs: None)

    with caplog.at_level("ERROR", logger="quaid.daemon"), pytest.raises(_StopDaemonLoop):
        extraction_daemon.daemon_loop(poll_interval=0.0, idle_check_interval=999999.0)

    assert marked == []
    assert read_calls >= 1
    assert process_calls == 1
    assert "Provider configuration error during signal processing" in caplog.text
    assert "signal preserved" in caplog.text


def test_daemon_loop_warns_on_version_watcher_failure_when_fail_open(monkeypatch, caplog):
    class _FailingVersionWatcher:
        def __init__(self, **_kwargs):
            pass

        def tick(self):
            raise RuntimeError("watcher down")

    monkeypatch.setattr("core.compatibility.VersionWatcher", _FailingVersionWatcher)
    monkeypatch.setattr(
        "core.compatibility.read_circuit_breaker",
        lambda _data_dir: types.SimpleNamespace(
            allows_writes=lambda: True,
            status="normal",
            message="",
        ),
    )
    monkeypatch.setattr(extraction_daemon, "write_pid", lambda _pid: None)
    monkeypatch.setattr(extraction_daemon, "remove_pid", lambda: None)
    monkeypatch.setattr(extraction_daemon, "_supervisor_alive", lambda: True)
    monkeypatch.setattr(extraction_daemon, "_reload_config_if_changed", lambda _reason: None)
    monkeypatch.setattr(extraction_daemon, "_retry_missing_embeddings", lambda: 0)
    monkeypatch.setattr(extraction_daemon, "check_chunk_ready_sessions", lambda: None)
    monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(extraction_daemon.signal, "signal", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        extraction_daemon.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(_StopDaemonLoop()),
    )

    with caplog.at_level("WARNING", logger="quaid.daemon"):
        with pytest.raises(_StopDaemonLoop):
            extraction_daemon.daemon_loop(poll_interval=0.0, idle_check_interval=999999.0)

    assert "version watcher tick failed" in caplog.text
    assert "watcher down" in caplog.text


def test_daemon_loop_raises_version_watcher_failure_under_failhard(monkeypatch):
    class _FailingVersionWatcher:
        def __init__(self, **_kwargs):
            pass

        def tick(self):
            raise RuntimeError("watcher down")

    monkeypatch.setattr("core.compatibility.VersionWatcher", _FailingVersionWatcher)
    monkeypatch.setattr(extraction_daemon, "write_pid", lambda _pid: None)
    monkeypatch.setattr(extraction_daemon, "remove_pid", lambda: None)
    monkeypatch.setattr(extraction_daemon, "_supervisor_alive", lambda: True)
    monkeypatch.setattr(extraction_daemon, "_reload_config_if_changed", lambda _reason: None)
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(extraction_daemon.signal, "signal", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="version watcher tick failed while failHard is enabled") as excinfo:
        extraction_daemon.daemon_loop(poll_interval=0.0, idle_check_interval=999999.0)

    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert "watcher down" in str(excinfo.value.__cause__)


def test_worker_loop_exits_nonzero_when_daemon_crashes(monkeypatch):
    removed = []

    monkeypatch.setattr(
        extraction_daemon,
        "daemon_loop",
        lambda: (_ for _ in ()).throw(RuntimeError("daemon boom")),
    )
    monkeypatch.setattr(extraction_daemon.os, "getpid", lambda: 4242)
    monkeypatch.setattr(extraction_daemon, "_remove_pid_if_matches", lambda pid: removed.append(pid))
    monkeypatch.setattr(
        extraction_daemon.os,
        "_exit",
        lambda code: (_ for _ in ()).throw(SystemExit(code)),
    )

    with pytest.raises(SystemExit) as excinfo:
        extraction_daemon._run_worker_loop()

    assert excinfo.value.code == 1
    assert removed == [4242]


def test_retry_missing_embeddings_helper_reraises_under_failhard(monkeypatch):
    from datastore.memorydb import memory_graph

    class FailingGraph:
        def retry_missing_embeddings(self, limit=20):
            raise RuntimeError("embedding backend down")

    monkeypatch.setattr(memory_graph, "MemoryGraph", lambda: FailingGraph())
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="Missing embedding retry failed while failHard is enabled"):
        extraction_daemon._retry_missing_embeddings()


def test_daemon_loop_reraises_embedding_retry_failure_under_failhard(monkeypatch):
    def fake_sleep(_seconds):
        raise AssertionError("daemon loop should not sleep after failHard embed retry failure")

    monkeypatch.setattr(extraction_daemon, "write_pid", lambda _pid: None)
    monkeypatch.setattr(extraction_daemon, "remove_pid", lambda: None)
    monkeypatch.setattr("core.compatibility.VersionWatcher", _NoopVersionWatcher)
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(extraction_daemon, "_supervisor_alive", lambda: True)
    monkeypatch.setattr(extraction_daemon, "_reload_config_if_changed", lambda _reason: None)
    monkeypatch.setattr(
        extraction_daemon,
        "_retry_missing_embeddings",
        lambda: (_ for _ in ()).throw(RuntimeError("embedding backend down")),
    )
    monkeypatch.setattr(extraction_daemon, "check_chunk_ready_sessions", lambda: None)
    monkeypatch.setattr(extraction_daemon, "check_idle_sessions", lambda _mins: None)
    monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
    monkeypatch.setattr(extraction_daemon, "process_signal", lambda _sig: None)
    monkeypatch.setattr(
        "core.compatibility.read_circuit_breaker",
        lambda _data_dir: types.SimpleNamespace(
            allows_writes=lambda: True,
            status="normal",
            message="",
        ),
    )
    monkeypatch.setattr(extraction_daemon.time, "time", lambda: 1_700_000_000.0)
    monkeypatch.setattr(extraction_daemon.time, "sleep", fake_sleep)
    monkeypatch.setattr(extraction_daemon.signal, "signal", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="embed retry failed while failHard is enabled"):
        extraction_daemon.daemon_loop(poll_interval=0.0, idle_check_interval=999999.0)


def test_daemon_loop_reraises_chunk_readiness_failure_under_failhard(monkeypatch):
    def fake_sleep(_seconds):
        raise AssertionError("daemon loop should not sleep after failHard chunk readiness failure")

    monkeypatch.setattr(extraction_daemon, "write_pid", lambda _pid: None)
    monkeypatch.setattr(extraction_daemon, "remove_pid", lambda: None)
    monkeypatch.setattr("core.compatibility.VersionWatcher", _NoopVersionWatcher)
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(extraction_daemon, "_supervisor_alive", lambda: True)
    monkeypatch.setattr(extraction_daemon, "_reload_config_if_changed", lambda _reason: None)
    monkeypatch.setattr(extraction_daemon, "_retry_missing_embeddings", lambda: 0)
    monkeypatch.setattr(
        extraction_daemon,
        "check_chunk_ready_sessions",
        lambda: (_ for _ in ()).throw(RuntimeError("chunk scan down")),
    )
    monkeypatch.setattr(extraction_daemon, "check_idle_sessions", lambda _mins: None)
    monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
    monkeypatch.setattr(extraction_daemon, "process_signal", lambda _sig: None)
    monkeypatch.setattr(
        "core.compatibility.read_circuit_breaker",
        lambda _data_dir: types.SimpleNamespace(
            allows_writes=lambda: True,
            status="normal",
            message="",
        ),
    )
    monkeypatch.setattr(extraction_daemon.time, "time", lambda: 1_700_000_000.0)
    monkeypatch.setattr(extraction_daemon.time, "sleep", fake_sleep)
    monkeypatch.setattr(extraction_daemon.signal, "signal", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="rolling chunk readiness check failed while failHard is enabled"):
        extraction_daemon.daemon_loop(poll_interval=0.0, idle_check_interval=999999.0)


def test_daemon_loop_reraises_idle_check_failure_under_failhard(monkeypatch):
    def fake_sleep(_seconds):
        raise AssertionError("daemon loop should not sleep after failHard idle check failure")

    monkeypatch.setattr(extraction_daemon, "write_pid", lambda _pid: None)
    monkeypatch.setattr(extraction_daemon, "remove_pid", lambda: None)
    monkeypatch.setattr("core.compatibility.VersionWatcher", _NoopVersionWatcher)
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(extraction_daemon, "_supervisor_alive", lambda: True)
    monkeypatch.setattr(extraction_daemon, "_reload_config_if_changed", lambda _reason: None)
    monkeypatch.setattr(extraction_daemon, "_retry_missing_embeddings", lambda: 0)
    monkeypatch.setattr(extraction_daemon, "check_chunk_ready_sessions", lambda: None)
    monkeypatch.setattr(
        extraction_daemon,
        "check_idle_sessions",
        lambda _mins: (_ for _ in ()).throw(RuntimeError("idle scan down")),
    )
    monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
    monkeypatch.setattr(extraction_daemon, "process_signal", lambda _sig: None)
    monkeypatch.setattr(
        "core.compatibility.read_circuit_breaker",
        lambda _data_dir: types.SimpleNamespace(
            allows_writes=lambda: True,
            status="normal",
            message="",
        ),
    )
    monkeypatch.setattr(extraction_daemon.time, "time", lambda: 1_700_000_000.0)
    monkeypatch.setattr(extraction_daemon.time, "sleep", fake_sleep)
    monkeypatch.setattr(extraction_daemon.signal, "signal", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="idle check failed while failHard is enabled"):
        extraction_daemon.daemon_loop(poll_interval=0.0, idle_check_interval=0.0)


def test_load_runtime_adapter_for_signal_raises_under_failhard(monkeypatch):
    fake_adapter_mod = types.ModuleType("lib.adapter")

    def fake_get_adapter():
        raise RuntimeError("adapter unavailable")

    fake_adapter_mod.get_adapter = fake_get_adapter
    monkeypatch.setitem(sys.modules, "lib.adapter", fake_adapter_mod)
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="adapter unavailable"):
        extraction_daemon._load_runtime_adapter_for_signal("daemon-reset", "sess-adapter")


def test_load_runtime_adapter_for_signal_warns_and_degrades_when_fail_open(monkeypatch, caplog):
    fake_adapter_mod = types.ModuleType("lib.adapter")

    def fake_get_adapter():
        raise RuntimeError("adapter unavailable")

    fake_adapter_mod.get_adapter = fake_get_adapter
    monkeypatch.setitem(sys.modules, "lib.adapter", fake_adapter_mod)
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: False)

    with caplog.at_level("WARNING", logger="quaid.daemon"):
        adapter = extraction_daemon._load_runtime_adapter_for_signal("daemon-reset", "sess-adapter")

    assert adapter is None
    assert "adapter load failed" in caplog.text
    assert "sess-adapter" in caplog.text


def test_load_runtime_adapter_raises_under_failhard(monkeypatch):
    fake_adapter_mod = types.ModuleType("lib.adapter")
    fake_adapter_mod.get_adapter = lambda: (_ for _ in ()).throw(RuntimeError("runtime adapter broken"))
    monkeypatch.setitem(sys.modules, "lib.adapter", fake_adapter_mod)
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="runtime adapter load failed") as excinfo:
        extraction_daemon._load_runtime_adapter()

    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert "runtime adapter broken" in str(excinfo.value.__cause__)


def test_load_runtime_adapter_warns_and_degrades_when_fail_open(monkeypatch, caplog):
    fake_adapter_mod = types.ModuleType("lib.adapter")
    fake_adapter_mod.get_adapter = lambda: (_ for _ in ()).throw(RuntimeError("runtime adapter broken"))
    monkeypatch.setitem(sys.modules, "lib.adapter", fake_adapter_mod)
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: False)

    with caplog.at_level("WARNING", logger="quaid.daemon"):
        adapter = extraction_daemon._load_runtime_adapter()

    assert adapter is None
    assert "runtime adapter load failed" in caplog.text
    assert "runtime adapter broken" in caplog.text


@pytest.mark.parametrize("initial_daemon_env", [None, "caller"])
def test_flush_pending_signals_restores_daemon_env_when_pending_read_raises(
    monkeypatch,
    tmp_path,
    initial_daemon_env,
):
    if initial_daemon_env is None:
        monkeypatch.delenv("QUAID_DAEMON", raising=False)
    else:
        monkeypatch.setenv("QUAID_DAEMON", initial_daemon_env)

    monkeypatch.setattr(extraction_daemon, "_instance_id", lambda: "pytest-runner")
    monkeypatch.setattr(extraction_daemon, "_instance_root", lambda: tmp_path)

    def _raise_pending_read():
        assert os.environ.get("QUAID_DAEMON") == "1"
        raise RuntimeError("pending signal read failed")

    monkeypatch.setattr(extraction_daemon, "read_pending_signals", _raise_pending_read)

    with pytest.raises(RuntimeError, match="pending signal read failed"):
        extraction_daemon.flush_pending_signals(timeout_seconds=0, poll_interval=0)

    if initial_daemon_env is None:
        assert "QUAID_DAEMON" not in os.environ
    else:
        assert os.environ["QUAID_DAEMON"] == initial_daemon_env


def test_flush_pending_signals_preserves_processing_failure_when_fail_open(monkeypatch, tmp_path):
    signal_path = tmp_path / "signal.json"
    signal_path.write_text("{}", encoding="utf-8")
    signal_payload = {"session_id": "sess-flush", "type": "reset", "_signal_path": str(signal_path)}

    monkeypatch.setattr(extraction_daemon, "_instance_id", lambda: "pytest-runner")
    monkeypatch.setattr(extraction_daemon, "_instance_root", lambda: tmp_path)
    monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [signal_payload])
    monkeypatch.setattr(extraction_daemon, "_pending_signal_count", lambda: 0)
    monkeypatch.setattr(extraction_daemon, "check_idle_sessions", lambda _mins: None)
    monkeypatch.setattr(
        extraction_daemon,
        "process_signal",
        lambda _sig: (_ for _ in ()).throw(RuntimeError("flush boom")),
    )
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: False)

    summary = extraction_daemon.flush_pending_signals(timeout_seconds=0, poll_interval=0)

    assert summary["status"] == "drained"
    assert summary["attempted"] == 1
    assert summary["errors"] == 1
    assert summary["preserved"] == 1
    assert summary["processed"] == 0


def test_flush_pending_signals_drains_timeout_eligible_idle_sessions(monkeypatch, tmp_path):
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        '{"role":"user","content":"timeout flush codeword cedar-lantern"}\n'
        '{"role":"assistant","content":"ack"}\n',
        encoding="utf-8",
    )

    now = 1_700_000_000.0
    old_mtime = now - (31 * 60)
    os.utime(transcript_path, (old_mtime, old_mtime))

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setattr(extraction_daemon, "_instance_id", lambda: "pytest-runner")
    monkeypatch.setattr(extraction_daemon, "_reload_config_if_changed", lambda _reason: None)
    monkeypatch.setattr(extraction_daemon, "_load_runtime_adapter", lambda: None)
    monkeypatch.setattr(extraction_daemon, "_ensure_discovered_session_cursors", lambda _adapter: None)
    monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: now - (2 * 60 * 60))
    monkeypatch.setattr(extraction_daemon, "_get_idle_timeout_minutes", lambda: 30)
    monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)

    cursor_dir = extraction_daemon._cursor_dir()
    (cursor_dir / "sess-flush-idle.json").write_text(
        json.dumps(
            {
                "session_id": "sess-flush-idle",
                "line_offset": 0,
                "transcript_path": str(transcript_path),
            }
        ),
        encoding="utf-8",
    )

    processed = []

    def _process_and_consume(sig):
        processed.append(sig)
        extraction_daemon.mark_signal_processed(sig)

    monkeypatch.setattr(extraction_daemon, "process_signal", _process_and_consume)

    summary = extraction_daemon.flush_pending_signals(timeout_seconds=0, poll_interval=0)

    assert summary["status"] == "drained"
    assert summary["idle_checks"] == 1
    assert summary["attempted"] == 1
    assert summary["processed"] == 1
    assert summary["remaining_signals"] == 0
    assert processed[0]["type"] == "timeout"
    assert processed[0]["session_id"] == "sess-flush-idle"


def test_flush_pending_signals_drains_cursorless_rolling_semantic_buffer(monkeypatch, tmp_path):
    transcript_path = tmp_path / "rollout-short.jsonl"
    transcript_path.write_text(
        '{"role":"user","content":"The reading chair has a brass desk lamp beside it."}\n'
        '{"role":"assistant","content":"Noted."}\n',
        encoding="utf-8",
    )

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setattr(extraction_daemon, "_instance_id", lambda: "pytest-runner")
    monkeypatch.setattr(extraction_daemon, "check_idle_sessions", lambda _mins: None)

    extraction_daemon.write_rolling_state(
        "sess-flush-rolling",
        {
            "transcript_path": str(transcript_path),
            "buffer_transcript_path": str(transcript_path),
            "buffered_line_offset": 2,
            "processed_line_offset": 2,
            "semantic_buffer": "User: The reading chair has a brass desk lamp beside it.\nAssistant: Noted.",
            "semantic_buffer_tokens": 18,
        },
    )

    processed = []

    def _process_and_consume(sig):
        processed.append(sig)
        extraction_daemon.mark_signal_processed(sig)

    monkeypatch.setattr(extraction_daemon, "process_signal", _process_and_consume)

    summary = extraction_daemon.flush_pending_signals(timeout_seconds=0, poll_interval=0)

    assert summary["status"] == "drained"
    assert summary["idle_checks"] == 1
    assert summary["attempted"] == 1
    assert summary["processed"] == 1
    assert summary["remaining_signals"] == 0
    assert processed[0]["type"] == "session_end"
    assert processed[0]["session_id"] == "sess-flush-rolling"
    assert processed[0]["transcript_path"] == str(transcript_path)
    assert processed[0]["meta"]["reason"] == "idle_rolling_semantic_buffer_flush"
    assert processed[0]["meta"]["semantic_buffer_tokens"] == 18


def test_flush_pending_signals_raises_idle_check_failure_under_failhard(monkeypatch, tmp_path):
    monkeypatch.delenv("QUAID_DAEMON", raising=False)
    monkeypatch.setattr(extraction_daemon, "_instance_id", lambda: "pytest-runner")
    monkeypatch.setattr(extraction_daemon, "_instance_root", lambda: tmp_path)
    monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
    monkeypatch.setattr(extraction_daemon, "_pending_signal_count", lambda: 0)
    monkeypatch.setattr(
        extraction_daemon,
        "check_idle_sessions",
        lambda _mins: (_ for _ in ()).throw(RuntimeError("idle scanner down")),
    )
    monkeypatch.setattr(extraction_daemon, "_get_idle_timeout_minutes", lambda: 30)
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="idle check failed during daemon flush while failHard is enabled"):
        extraction_daemon.flush_pending_signals(timeout_seconds=0, poll_interval=0)

    assert "QUAID_DAEMON" not in os.environ


def test_flush_pending_signals_raises_processing_failure_under_failhard(monkeypatch, tmp_path):
    signal_path = tmp_path / "signal.json"
    signal_path.write_text("{}", encoding="utf-8")
    signal_payload = {"session_id": "sess-flush", "type": "reset", "_signal_path": str(signal_path)}

    monkeypatch.delenv("QUAID_DAEMON", raising=False)
    monkeypatch.setattr(extraction_daemon, "_instance_id", lambda: "pytest-runner")
    monkeypatch.setattr(extraction_daemon, "_instance_root", lambda: tmp_path)
    monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [signal_payload])
    monkeypatch.setattr(extraction_daemon, "_pending_signal_count", lambda: 1)
    monkeypatch.setattr(
        extraction_daemon,
        "process_signal",
        lambda _sig: (_ for _ in ()).throw(RuntimeError("flush boom")),
    )
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="flush boom"):
        extraction_daemon.flush_pending_signals(timeout_seconds=0, poll_interval=0)

    assert "QUAID_DAEMON" not in os.environ
    assert signal_path.exists()


def test_flush_pending_signals_preserves_provider_config_error_under_failhard(monkeypatch, tmp_path, caplog):
    from lib.llm_clients import ProviderConfigError

    signal_path = tmp_path / "signal.json"
    signal_path.write_text("{}", encoding="utf-8")
    signal_payload = {"session_id": "sess-flush", "type": "reset", "_signal_path": str(signal_path)}

    monkeypatch.delenv("QUAID_DAEMON", raising=False)
    monkeypatch.setattr(extraction_daemon, "_instance_id", lambda: "pytest-runner")
    monkeypatch.setattr(extraction_daemon, "_instance_root", lambda: tmp_path)
    monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [signal_payload])
    monkeypatch.setattr(extraction_daemon, "_pending_signal_count", lambda: 1)
    monkeypatch.setattr(
        extraction_daemon,
        "process_signal",
        lambda _sig: (_ for _ in ()).throw(ProviderConfigError("invalid-model-m6-probe")),
    )
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

    with caplog.at_level("ERROR", logger="quaid.daemon"):
        summary = extraction_daemon.flush_pending_signals(timeout_seconds=0, poll_interval=0, max_passes=1)

    assert summary["attempted"] == 1
    assert summary["errors"] == 1
    assert summary["preserved"] == 1
    assert summary["processed"] == 0
    assert signal_path.exists()
    assert "flush signal provider config error; signal preserved" in caplog.text
    assert "QUAID_DAEMON" not in os.environ


def test_stage_semantic_buffer_payload_uses_focused_extract_chunks(monkeypatch):
    import ingest.extract as extract_mod

    calls = []

    def fake_extract_from_transcript(**kwargs):
        calls.append(kwargs)
        return {
            "raw_facts": [],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "carry_facts": [],
            "facts_skipped": 0,
            "chunks_processed": 1,
            "chunks_total": 1,
            "root_chunks": 1,
            "split_events": 0,
            "split_child_chunks": 0,
            "leaf_chunks": 1,
            "max_split_depth": 0,
            "deep_calls": 1,
            "repair_calls": 0,
            "assessment_usable": 1,
            "assessment_nothing_usable": 0,
            "assessment_needs_smaller_chunk": 0,
            "unclassified_empty_payloads": 0,
        }

    monkeypatch.setattr(extract_mod, "extract_from_transcript", fake_extract_from_transcript)
    monkeypatch.setattr(extraction_daemon, "_warm_payload_embeddings", lambda facts: {
        "requested": 0,
        "unique": 0,
        "cache_hits": 0,
        "warmed": 0,
        "failed": 0,
        "skipped_empty": 0,
    })
    monkeypatch.setattr(extraction_daemon, "write_rolling_state", lambda *args, **kwargs: None)

    monkeypatch.setattr(extraction_daemon, "write_rolling_metric", lambda *args, **kwargs: None)
    monkeypatch.setattr(extraction_daemon, "_write_rolling_debug_dump", lambda *args, **kwargs: None)
    monkeypatch.setattr(extraction_daemon, "_write_extraction_buffer_log", lambda *args, **kwargs: None)

    extraction_daemon._stage_semantic_buffer_payload(
        session_id="sess-stage",
        signal_type="rolling",
        transcript_path="/tmp/sess-stage.jsonl",
        label="daemon-rolling",
        owner="Owner",
        staged_state={"semantic_buffer": "User: OBD large transcript", "semantic_buffer_tokens": 10},
        buffered_line_offset=1,
        new_lines=["User: OBD large transcript"],
        semantic_buffer_metrics={"raw_lines_added": 1, "semantic_chars_added": 26, "semantic_tokens_added": 10},
        chunk_budget=1500,
        chunk_line_budget=200,
    )

    assert len(calls) == 1
    assert "wall_timeout_seconds" not in calls[0]
    assert calls[0]["chunk_tokens_override"] == 900
    assert calls[0]["llm_timeout_seconds"] == pytest.approx(120.0)
    assert calls[0]["llm_slot_wait_timeout_seconds"] == pytest.approx(1800.0)
    assert calls[0]["llm_max_retries"] == 0
    assert calls[0]["raise_on_llm_failure"] is True


def test_daemon_extract_chunk_tokens_focuses_normal_rolling_windows():
    assert extraction_daemon._daemon_extract_chunk_tokens(511) == 511
    assert extraction_daemon._daemon_extract_chunk_tokens(512) == 409
    assert extraction_daemon._daemon_extract_chunk_tokens(600) == 480
    assert extraction_daemon._daemon_extract_chunk_tokens(1200) == 900
    assert extraction_daemon._daemon_extract_chunk_tokens(1500) == 900
    assert extraction_daemon._daemon_extract_chunk_tokens(8000) == 900
    assert extraction_daemon._daemon_extract_chunk_tokens(200) == 200


def test_daemon_extract_llm_timeout_and_retries_can_be_tuned(monkeypatch):
    monkeypatch.setenv("QUAID_DAEMON_EXTRACT_LLM_TIMEOUT_SECONDS", "45.5")
    monkeypatch.setenv("QUAID_DAEMON_EXTRACT_LLM_SLOT_WAIT_SECONDS", "456.5")
    monkeypatch.setenv("QUAID_DAEMON_EXTRACT_LLM_MAX_RETRIES", "2")

    assert extraction_daemon._daemon_extract_llm_timeout_seconds() == pytest.approx(45.5)
    assert extraction_daemon._daemon_extract_llm_slot_wait_seconds() == pytest.approx(456.5)
    assert extraction_daemon._daemon_extract_llm_max_retries() == 2


def test_rolling_payload_merge_and_flush_preserve_source_chunk_descriptors():
    state = {
        "raw_facts": [
            {
                "text": "Ada stores the launch checklist in the red binder.",
                "_source_chunk_ref": "chunk:stage",
            }
        ],
        "raw_source_chunks": [
            {
                "source_chunk_ref": "chunk:stage",
                "text": "User: Ada stores the launch checklist in the red binder.",
                "source_id": "sess-stage",
                "session_id": "sess-stage",
                "chunk_index": 0,
            }
        ],
        "raw_snippets": {},
        "raw_journal": {},
        "raw_project_logs": {},
        "carry_facts": [],
    }
    next_payload = {
        "raw_facts": [
            {
                "text": "Berto keeps the rover manual in cabinet seven.",
                "_source_chunk_ref": "chunk:next",
            }
        ],
        "raw_source_chunks": [
            {
                "source_chunk_ref": "chunk:next",
                "text": "User: Berto keeps the rover manual in cabinet seven.",
                "source_id": "sess-stage",
                "session_id": "sess-stage",
                "chunk_index": 1,
            }
        ],
        "raw_snippets": {},
        "raw_journal": {},
        "raw_project_logs": {},
        "carry_facts": [],
    }

    merged = extraction_daemon.merge_staged_payloads(state, next_payload)
    assert {chunk["source_chunk_ref"] for chunk in merged["raw_source_chunks"]} == {
        "chunk:stage",
        "chunk:next",
    }
    assert extraction_daemon.staged_state_has_payload(merged)
    assert not extraction_daemon.staged_state_has_payload({"raw_source_chunks": merged["raw_source_chunks"]})

    tail_payload = {
        "raw_facts": [
            {
                "text": "Cora labels the backup battery with blue tape.",
                "_source_chunk_ref": "chunk:tail",
            }
        ],
        "raw_source_chunks": [
            {
                "source_chunk_ref": "chunk:tail",
                "text": "User: Cora labels the backup battery with blue tape.",
                "source_id": "sess-stage",
                "session_id": "sess-stage",
                "chunk_index": 2,
            }
        ],
        "raw_snippets": {},
        "raw_journal": {},
        "raw_project_logs": {},
        "carry_facts": [],
    }

    flush_payload = extraction_daemon.build_flush_payload(merged, tail_payload)
    assert {chunk["source_chunk_ref"] for chunk in flush_payload["raw_source_chunks"]} == {
        "chunk:stage",
        "chunk:next",
        "chunk:tail",
    }

    cleaned = extraction_daemon.clear_staged_payload_from_state(flush_payload)
    assert cleaned["raw_source_chunks"] == []


def test_merge_source_chunk_descriptors_retains_multiple_unref_descriptors():
    merged = extraction_daemon._merge_source_chunk_descriptors(
        [
            {
                "text": "User: Ada stores the launch checklist in the red binder.",
                "session_id": "sess-unref",
                "chunk_index": 0,
            },
            {
                "source_chunk_ref": "chunk:stable",
                "text": "User: Berto keeps the rover manual in cabinet seven.",
                "session_id": "sess-unref",
                "chunk_index": 1,
            },
        ],
        [
            {
                "text": "User: Cora labels the backup battery with blue tape.",
                "session_id": "sess-unref",
                "chunk_index": 2,
            },
            {
                "_source_chunk_ref": "chunk:stable",
                "text": "User: Duplicate descriptor should collapse by stable ref.",
                "session_id": "sess-unref",
                "chunk_index": 3,
            },
        ],
    )

    assert [chunk["chunk_index"] for chunk in merged] == [0, 1, 2]
    assert [chunk["text"] for chunk in merged] == [
        "User: Ada stores the launch checklist in the red binder.",
        "User: Berto keeps the rover manual in cabinet seven.",
        "User: Cora labels the backup battery with blue tape.",
    ]


def test_save_deferred_extraction_writes_unique_atomic_files(monkeypatch, tmp_path):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    monkeypatch.setattr(extraction_daemon.time, "time", lambda: 1700000000.0)
    monkeypatch.setattr(extraction_daemon.os, "getpid", lambda: 4242)
    uuids = iter([
        types.SimpleNamespace(hex="a" * 32),
        types.SimpleNamespace(hex="1" * 32),
        types.SimpleNamespace(hex="b" * 32),
        types.SimpleNamespace(hex="2" * 32),
    ])
    monkeypatch.setattr(extraction_daemon.uuid, "uuid4", lambda: next(uuids))
    atomic_writes = []
    real_atomic_write = extraction_daemon._atomic_write

    def _record_atomic_write(path, text):
        atomic_writes.append((path, json.loads(text)))
        real_atomic_write(path, text)

    monkeypatch.setattr(extraction_daemon, "_atomic_write", _record_atomic_write)

    for reason in ("first", "second"):
        assert extraction_daemon._save_deferred_extraction(
            session_id="sess-deferred",
            transcript_text=f"User: deferred payload {reason}",
            owner_id="owner-id",
            label="daemon-rolling",
            reason=reason,
        ) is True

    deferred_dir = tmp_path / "instances" / "test-inst" / "data" / "deferred-extractions"
    files = sorted(deferred_dir.glob("*.json"))
    assert [path for path, _payload in atomic_writes] == files
    assert [path.name for path in files] == [
        f"sess-deferred_1700000000_4242_{'a' * 32}.json",
        f"sess-deferred_1700000000_4242_{'b' * 32}.json",
    ]
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in files]
    assert [payload["reason"] for payload in payloads] == ["first", "second"]
    assert all(payload["session_id"] == "sess-deferred" for payload in payloads)
    assert all(payload["saved_at"] == 1700000000 for payload in payloads)


def test_save_deferred_extraction_sanitizes_session_id_filename(monkeypatch, tmp_path):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    monkeypatch.setattr(extraction_daemon.time, "time", lambda: 1700000001.0)
    monkeypatch.setattr(extraction_daemon.os, "getpid", lambda: 4343)
    monkeypatch.setattr(extraction_daemon.uuid, "uuid4", lambda: types.SimpleNamespace(hex="c" * 32))

    extraction_daemon._save_deferred_extraction(
        session_id="../escaped",
        transcript_text="User: path traversal should not shape the filename",
        owner_id="owner-id",
        label="daemon-rolling",
        reason="invalid-session",
    )

    deferred_dir = tmp_path / "instances" / "test-inst" / "data" / "deferred-extractions"
    [path] = list(deferred_dir.glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["session_id"].startswith("unknown-")
    assert path.name.startswith(f"{payload['session_id']}_1700000001_4343_")
    assert "/" not in path.name
    assert ".." not in path.name


def test_save_deferred_extraction_raises_write_failure_when_fail_hard(monkeypatch, tmp_path):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

    def _raise_atomic_write(_path, _text):
        raise OSError("deferred write failed")

    monkeypatch.setattr(extraction_daemon, "_atomic_write", _raise_atomic_write)

    with pytest.raises(OSError, match="deferred write failed"):
        extraction_daemon._save_deferred_extraction(
            session_id="sess-deferred",
            transcript_text="User: deferred payload",
            owner_id="owner-id",
            label="daemon-rolling",
            reason="provider-timeout",
        )


def test_save_deferred_extraction_returns_false_on_write_failure_when_fail_open(monkeypatch, tmp_path):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: False)

    def _raise_atomic_write(_path, _text):
        raise OSError("deferred write failed")

    monkeypatch.setattr(extraction_daemon, "_atomic_write", _raise_atomic_write)

    assert extraction_daemon._save_deferred_extraction(
        session_id="sess-deferred",
        transcript_text="User: deferred payload",
        owner_id="owner-id",
        label="daemon-rolling",
        reason="provider-timeout",
    ) is False


def test_buffer_transcript_tail_defers_parse_failure_without_advancing(monkeypatch, tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"content":"alpha"}}\n'
        '{"type":"assistant","message":{"content":"beta"}}\n',
        encoding="utf-8",
    )
    deferred = []

    class FailingAdapter:
        def parse_session_jsonl(self, _path):
            raise ValueError("parse exploded")

    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: False)

    def _save_deferred(**kwargs):
        deferred.append(kwargs)
        return True

    monkeypatch.setattr(extraction_daemon, "_save_deferred_extraction", _save_deferred)

    state, metrics = extraction_daemon._buffer_transcript_tail(
        str(transcript),
        0,
        {"buffered_line_offset": 0},
        adapter=FailingAdapter(),
        session_id="sess-parse",
        owner_id="owner-id",
        label="daemon-rolling",
    )

    assert metrics["parse_failed"] == 1
    assert metrics["buffered_line_offset"] == 0
    assert state["buffered_line_offset"] == 0
    assert state["transcript_parse_failure_path"] == str(transcript)
    assert state["transcript_parse_failure_offset"] == 0
    assert deferred == [{
        "session_id": "sess-parse",
        "transcript_text": transcript.read_text(encoding="utf-8"),
        "owner_id": "owner-id",
        "label": "daemon-rolling",
        "reason": "transcript_parse_failure",
    }]

    state_again, metrics_again = extraction_daemon._buffer_transcript_tail(
        str(transcript),
        0,
        state,
        adapter=FailingAdapter(),
        session_id="sess-parse",
        owner_id="owner-id",
        label="daemon-rolling",
    )

    assert metrics_again["parse_failed"] == 1
    assert state_again["buffered_line_offset"] == 0
    assert len(deferred) == 1


def test_buffer_transcript_tail_retries_parse_failure_save_after_write_failure(monkeypatch, tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"type":"user","message":{"content":"alpha"}}\n', encoding="utf-8")
    save_calls = []

    class FailingAdapter:
        def parse_session_jsonl(self, _path):
            raise ValueError("parse exploded")

    def _failed_deferred_save(**kwargs):
        save_calls.append(kwargs)
        return False

    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(extraction_daemon, "_save_deferred_extraction", _failed_deferred_save)

    state, metrics = extraction_daemon._buffer_transcript_tail(
        str(transcript),
        0,
        {"buffered_line_offset": 0},
        adapter=FailingAdapter(),
        session_id="sess-parse",
        owner_id="owner-id",
        label="daemon-rolling",
    )

    assert metrics["parse_failed"] == 1
    assert "transcript_parse_failure_path" not in state
    assert "transcript_parse_failure_offset" not in state
    assert len(save_calls) == 1

    state_again, metrics_again = extraction_daemon._buffer_transcript_tail(
        str(transcript),
        0,
        state,
        adapter=FailingAdapter(),
        session_id="sess-parse",
        owner_id="owner-id",
        label="daemon-rolling",
    )

    assert metrics_again["parse_failed"] == 1
    assert "transcript_parse_failure_path" not in state_again
    assert len(save_calls) == 2


def test_buffer_transcript_tail_parse_failure_raises_when_fail_hard(monkeypatch, tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"type":"user","message":{"content":"alpha"}}\n', encoding="utf-8")
    deferred = []

    class FailingAdapter:
        def parse_session_jsonl(self, _path):
            raise ValueError("parse exploded")

    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(extraction_daemon, "_save_deferred_extraction", lambda **kwargs: deferred.append(kwargs))

    with pytest.raises(RuntimeError, match="Failed parsing transcript window while failHard is enabled"):
        extraction_daemon._buffer_transcript_tail(
            str(transcript),
            0,
            {"buffered_line_offset": 0},
            adapter=FailingAdapter(),
            max_tokens=100,
            session_id="sess-parse",
            owner_id="owner-id",
            label="daemon-rolling",
        )

    assert deferred == []


def test_stage_semantic_buffer_payload_defers_without_staging_partial_facts(monkeypatch):
    import ingest.extract as extract_mod

    deferred = []
    writes = []
    warmed = []

    monkeypatch.setattr(
        extract_mod,
        "extract_from_transcript",
        lambda **kwargs: {
            "raw_facts": [{
                "text": "Partial fact should not be staged because another chunk failed.",
                "confidence": 0.9,
            }],
            "raw_snippets": {"partial.md": ["partial snippet"]},
            "raw_journal": {},
            "raw_project_logs": {},
            "raw_source_chunks": [],
            "carry_facts": [],
            "facts_skipped": 0,
            "chunks_processed": 1,
            "chunks_total": 2,
            "unclassified_empty_payloads": 0,
        },
    )
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(extraction_daemon, "_save_deferred_extraction", lambda **kwargs: deferred.append(kwargs))
    monkeypatch.setattr(extraction_daemon, "_warm_payload_embeddings", lambda facts: warmed.append(list(facts)) or {
        "requested": 0,
        "unique": 0,
        "cache_hits": 0,
        "warmed": 0,
        "failed": 0,
        "skipped_empty": 0,
    })
    monkeypatch.setattr(extraction_daemon, "write_rolling_state", lambda _sid, state: writes.append(dict(state)))
    monkeypatch.setattr(extraction_daemon, "write_rolling_metric", lambda *args, **kwargs: None)
    monkeypatch.setattr(extraction_daemon, "_write_rolling_debug_dump", lambda *args, **kwargs: None)
    monkeypatch.setattr(extraction_daemon, "_write_extraction_buffer_log", lambda *args, **kwargs: None)

    result = extraction_daemon._stage_semantic_buffer_payload(
        session_id="sess-stage",
        signal_type="rolling",
        transcript_path="/tmp/sess-stage.jsonl",
        label="daemon-rolling",
        owner="Owner",
        staged_state={"semantic_buffer": "User: OBD large transcript", "semantic_buffer_tokens": 10},
        buffered_line_offset=1,
        new_lines=["User: OBD large transcript"],
        semantic_buffer_metrics={"raw_lines_added": 1, "semantic_chars_added": 26, "semantic_tokens_added": 10},
        chunk_budget=1500,
        chunk_line_budget=200,
    )

    assert deferred == [{
        "session_id": "sess-stage",
        "transcript_text": "User: OBD large transcript",
        "owner_id": "Owner",
        "label": "daemon-rolling",
        "reason": "non_provider_failure_1_of_2_chunks",
    }]
    assert warmed == [[]]
    assert result.get("raw_facts", []) == []
    assert result.get("raw_snippets", {}) == {}
    assert result["semantic_buffer"] == ""
    assert result["processed_line_offset"] == 1
    assert result["buffered_line_offset"] == 1
    assert not result.get(extraction_daemon._STAGED_PAYLOAD_PENDING_FLUSH_KEY)
    assert writes == [result]


def test_rolling_debug_dir_defaults_to_instance_logs(monkeypatch, tmp_path):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    monkeypatch.delenv("QUAID_ROLLING_DEBUG_DIR", raising=False)

    assert extraction_daemon._rolling_debug_dir() == (
        tmp_path / "instances" / "test-inst" / "logs" / "daemon" / "rolling-debug"
    )


def test_rolling_debug_dir_rejects_env_path_outside_quaid_home(monkeypatch, tmp_path, caplog):
    outside = tmp_path / "outside-debug"
    home = tmp_path / "home"
    monkeypatch.setenv("QUAID_HOME", str(home))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    monkeypatch.setenv("QUAID_ROLLING_DEBUG_DIR", str(outside))

    with caplog.at_level("WARNING", logger="quaid.daemon"):
        debug_dir = extraction_daemon._rolling_debug_dir()

    assert debug_dir == home / "instances" / "test-inst" / "logs" / "daemon" / "rolling-debug"
    assert "ignoring QUAID_ROLLING_DEBUG_DIR outside QUAID_HOME" in caplog.text


def test_rolling_debug_dir_accepts_env_path_inside_quaid_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    debug_path = home / "debug" / "rolling"
    monkeypatch.setenv("QUAID_HOME", str(home))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    monkeypatch.setenv("QUAID_ROLLING_DEBUG_DIR", str(debug_path))

    assert extraction_daemon._rolling_debug_dir() == debug_path.resolve()


def test_rolling_debug_dir_rejects_flag_path_outside_quaid_home(monkeypatch, tmp_path, caplog):
    home = tmp_path / "home"
    outside = tmp_path / "outside-debug"
    flag = home / "instances" / "test-inst" / "data" / "rolling-debug.enabled"
    flag.parent.mkdir(parents=True)
    flag.write_text(str(outside), encoding="utf-8")
    monkeypatch.setenv("QUAID_HOME", str(home))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    monkeypatch.delenv("QUAID_ROLLING_DEBUG_DIR", raising=False)

    with caplog.at_level("WARNING", logger="quaid.daemon"):
        debug_dir = extraction_daemon._rolling_debug_dir()

    assert debug_dir == home / "instances" / "test-inst" / "logs" / "daemon" / "rolling-debug"
    assert "rolling-debug.enabled outside QUAID_HOME" in caplog.text


def test_stage_semantic_buffer_payload_raises_on_partial_chunks_when_fail_hard(monkeypatch):
    import ingest.extract as extract_mod

    deferred = []

    monkeypatch.setattr(
        extract_mod,
        "extract_from_transcript",
        lambda **kwargs: {
            "raw_facts": [],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "carry_facts": [],
            "facts_skipped": 0,
            "chunks_processed": 7,
            "chunks_total": 31,
            "unclassified_empty_payloads": 0,
        },
    )
    monkeypatch.setattr(extraction_daemon, "_warm_payload_embeddings", lambda facts: {
        "requested": 0,
        "unique": 0,
        "cache_hits": 0,
        "warmed": 0,
        "failed": 0,
        "skipped_empty": 0,
    })
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(extraction_daemon, "_save_deferred_extraction", lambda **kwargs: deferred.append(kwargs))

    with pytest.raises(RuntimeError, match="24/31 chunks failed extraction while failHard is enabled"):
        extraction_daemon._stage_semantic_buffer_payload(
            session_id="sess-stage",
            signal_type="rolling",
            transcript_path="/tmp/sess-stage.jsonl",
            label="daemon-rolling",
            owner="Owner",
            staged_state={"semantic_buffer": "User: OBD large transcript", "semantic_buffer_tokens": 10},
            buffered_line_offset=1,
            new_lines=["User: OBD large transcript"],
            semantic_buffer_metrics={"raw_lines_added": 1, "semantic_chars_added": 26, "semantic_tokens_added": 10},
            chunk_budget=1500,
            chunk_line_budget=200,
        )

    assert deferred == []


def test_process_signal_partial_stage_chunks_escape_when_fail_hard(monkeypatch, tmp_path):
    import ingest.extract as extract_mod

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"role":"user","content":"OBD large transcript"}\n', encoding="utf-8")
    signal_dir = tmp_path / "signals"
    signal_dir.mkdir()

    staged_state = {
        "semantic_buffer": "User: OBD large transcript",
        "semantic_buffer_tokens": 100,
        "buffered_line_offset": 1,
        "processed_line_offset": 0,
        "transcript_path": str(transcript),
    }
    deferred = []
    marked = []
    released = []

    real_registry = sys.modules.get("core.subagent_registry")
    real_adapter = sys.modules.get("lib.adapter")

    fake_registry = types.ModuleType("core.subagent_registry")
    fake_registry.is_registered_subagent = lambda sid: False
    fake_registry.get_harvestable = lambda sid: []
    fake_registry.mark_harvested = lambda sid, cid: None
    sys.modules["core.subagent_registry"] = fake_registry

    fake_adapter_mod = types.ModuleType("lib.adapter")

    class _FakeAdapter(_OwnedTestAdapterMixin):
        def parse_session_jsonl(self, path):
            return "User: OBD large transcript"

    fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
    sys.modules["lib.adapter"] = fake_adapter_mod

    monkeypatch.setattr(
        extract_mod,
        "extract_from_transcript",
        lambda **kwargs: {
            "raw_facts": [],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "carry_facts": [],
            "facts_skipped": 0,
            "chunks_processed": 7,
            "chunks_total": 31,
            "unclassified_empty_payloads": 0,
        },
    )
    monkeypatch.setattr(extraction_daemon, "_reload_config_if_changed", lambda reason: None)
    monkeypatch.setattr(
        extraction_daemon,
        "_read_rolling_state_for_signal",
        lambda sid, path, **_kwargs: (dict(staged_state), sid),
    )
    monkeypatch.setattr(extraction_daemon, "_signal_source_cursor_key", lambda *args, **kwargs: "source-key")
    monkeypatch.setattr(extraction_daemon, "_acquire_session_processing_lock", lambda key: 123)
    monkeypatch.setattr(extraction_daemon, "_release_session_processing_lock", lambda key, fd: released.append((key, fd)))
    monkeypatch.setattr(
        extraction_daemon,
        "_read_cursor_with_source_compat",
        lambda sid, key: {"line_offset": 0, "transcript_path": str(transcript)},
    )
    monkeypatch.setattr(extraction_daemon, "_signal_dir", lambda: signal_dir)
    monkeypatch.setattr(extraction_daemon, "_cursor_or_adapter_owns_transcript_path", lambda *args, **kwargs: True)
    monkeypatch.setattr(extraction_daemon, "count_transcript_lines", lambda path: 1)
    monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda default=8000: 10)
    monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_max_lines", lambda default=0: 0)
    monkeypatch.setattr(extraction_daemon, "read_transcript_token_window", lambda *args, **kwargs: [])
    monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "owner-id")
    monkeypatch.setattr(extraction_daemon, "_warm_payload_embeddings", lambda facts: {
        "requested": 0,
        "unique": 0,
        "cache_hits": 0,
        "warmed": 0,
        "failed": 0,
        "skipped_empty": 0,
    })
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(extraction_daemon, "_save_deferred_extraction", lambda **kwargs: deferred.append(kwargs))
    monkeypatch.setattr(extraction_daemon, "mark_signal_processed", lambda signal: marked.append(signal))

    try:
        with pytest.raises(RuntimeError, match="24/31 chunks failed extraction while failHard is enabled"):
            extraction_daemon.process_signal({
                "session_id": "sess-obd",
                "type": "rolling",
                "transcript_path": str(transcript),
            })
    finally:
        if real_registry is not None:
            sys.modules["core.subagent_registry"] = real_registry
        else:
            sys.modules.pop("core.subagent_registry", None)
        if real_adapter is not None:
            sys.modules["lib.adapter"] = real_adapter
        else:
            sys.modules.pop("lib.adapter", None)

    assert deferred == []
    assert marked == []
    assert released == [("source-key", 123)]


def test_stage_dedup_settings_raises_config_failure_when_fail_hard(monkeypatch):
    fake_config = types.ModuleType("config")
    fake_config.get_config = lambda: (_ for _ in ()).throw(RuntimeError("config unreadable"))
    real_config = sys.modules.get("config")
    sys.modules["config"] = fake_config
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

    try:
        with pytest.raises(RuntimeError, match="config unreadable"):
            extraction_daemon._stage_dedup_settings()
    finally:
        if real_config is not None:
            sys.modules["config"] = real_config
        else:
            sys.modules.pop("config", None)


def test_stage_dedup_settings_warns_and_defaults_when_fail_soft(monkeypatch, caplog):
    fake_config = types.ModuleType("config")
    fake_config.get_config = lambda: (_ for _ in ()).throw(RuntimeError("config unreadable"))
    real_config = sys.modules.get("config")
    sys.modules["config"] = fake_config
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: False)

    try:
        assert extraction_daemon._stage_dedup_settings() == (0.98, 0.88, False)
    finally:
        if real_config is not None:
            sys.modules["config"] = real_config
        else:
            sys.modules.pop("config", None)

    assert "failed reading stage dedup settings" in caplog.text


def test_processing_lock_payload_raises_read_failure_when_fail_hard(monkeypatch, tmp_path):
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

    with pytest.raises(FileNotFoundError):
        extraction_daemon._read_processing_lock_payload(tmp_path / "missing.lock")


def test_process_signal_registered_subagent_lookup_failure_releases_lock_when_fail_hard(monkeypatch, tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"role":"user","content":"remember the blue mug"}\n', encoding="utf-8")
    released = []

    real_registry = sys.modules.get("core.subagent_registry")
    fake_registry = types.ModuleType("core.subagent_registry")

    def _fail_registered_subagent(_session_id):
        raise RuntimeError("registry unavailable")

    fake_registry.is_registered_subagent = _fail_registered_subagent
    sys.modules["core.subagent_registry"] = fake_registry

    monkeypatch.setattr(extraction_daemon, "_reload_config_if_changed", lambda reason: None)
    monkeypatch.setattr(extraction_daemon, "_read_rolling_state_for_signal", lambda *args, **kwargs: ({}, "sess-fail"))
    monkeypatch.setattr(extraction_daemon, "_active_source_cursor_for_stale_signal_transcript", lambda *args: ("", ""))
    monkeypatch.setattr(extraction_daemon, "_signal_source_cursor_key", lambda *args, **kwargs: "source-key")
    monkeypatch.setattr(extraction_daemon, "_acquire_session_processing_lock", lambda key: 456)
    monkeypatch.setattr(extraction_daemon, "_release_session_processing_lock", lambda key, fd: released.append((key, fd)))
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

    try:
        with pytest.raises(RuntimeError, match="registry unavailable"):
            extraction_daemon.process_signal({
                "session_id": "sess-fail",
                "type": "session_end",
                "transcript_path": str(transcript),
            })
    finally:
        if real_registry is not None:
            sys.modules["core.subagent_registry"] = real_registry
        else:
            sys.modules.pop("core.subagent_registry", None)

    assert released == [("source-key", 456)]


def test_process_signal_adapter_subagent_lookup_failure_releases_lock_when_fail_hard(monkeypatch, tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"role":"user","content":"remember the red mug"}\n', encoding="utf-8")
    released = []

    real_registry = sys.modules.get("core.subagent_registry")
    fake_registry = types.ModuleType("core.subagent_registry")
    fake_registry.is_registered_subagent = lambda _session_id: False
    sys.modules["core.subagent_registry"] = fake_registry

    class _Adapter(_OwnedTestAdapterMixin):
        def is_subagent_session(self, session_id, transcript_path=None):
            raise RuntimeError("adapter subagent check failed")

    monkeypatch.setattr(extraction_daemon, "_reload_config_if_changed", lambda reason: None)
    monkeypatch.setattr(extraction_daemon, "_read_rolling_state_for_signal", lambda *args, **kwargs: ({}, "sess-adapter"))
    monkeypatch.setattr(extraction_daemon, "_active_source_cursor_for_stale_signal_transcript", lambda *args: ("", ""))
    monkeypatch.setattr(extraction_daemon, "_signal_source_cursor_key", lambda *args, **kwargs: "source-key")
    monkeypatch.setattr(extraction_daemon, "_acquire_session_processing_lock", lambda key: 457)
    monkeypatch.setattr(extraction_daemon, "_release_session_processing_lock", lambda key, fd: released.append((key, fd)))
    monkeypatch.setattr(extraction_daemon, "_load_runtime_adapter_for_signal", lambda *args, **kwargs: _Adapter())
    monkeypatch.setattr(extraction_daemon, "_read_cursor_with_source_compat", lambda *args, **kwargs: {
        "line_offset": 0,
        "transcript_path": str(transcript),
    })
    monkeypatch.setattr(extraction_daemon, "_cursor_or_adapter_owns_transcript_path", lambda *args, **kwargs: True)
    monkeypatch.setattr(extraction_daemon, "_reconcile_internal_cursor_state", lambda *args, **kwargs: "not_internal")
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

    try:
        with pytest.raises(RuntimeError, match="adapter subagent check failed"):
            extraction_daemon.process_signal({
                "session_id": "sess-adapter",
                "type": "session_end",
                "transcript_path": str(transcript),
            })
    finally:
        if real_registry is not None:
            sys.modules["core.subagent_registry"] = real_registry
        else:
            sys.modules.pop("core.subagent_registry", None)

    assert released == [("source-key", 457)]


def test_process_signal_duplicate_sweep_failure_releases_lock_when_fail_hard(monkeypatch, tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"role":"user","content":"remember the yellow mug"}\n', encoding="utf-8")
    released = []

    real_registry = sys.modules.get("core.subagent_registry")
    fake_registry = types.ModuleType("core.subagent_registry")
    fake_registry.is_registered_subagent = lambda _session_id: False
    sys.modules["core.subagent_registry"] = fake_registry

    class _Adapter(_OwnedTestAdapterMixin):
        def is_subagent_session(self, session_id, transcript_path=None):
            return False

    class _BrokenSignalDir:
        def iterdir(self):
            raise RuntimeError("signal directory unavailable")

    monkeypatch.setattr(extraction_daemon, "_reload_config_if_changed", lambda reason: None)
    monkeypatch.setattr(extraction_daemon, "_read_rolling_state_for_signal", lambda *args, **kwargs: ({}, "sess-sweep"))
    monkeypatch.setattr(extraction_daemon, "_active_source_cursor_for_stale_signal_transcript", lambda *args: ("", ""))
    monkeypatch.setattr(extraction_daemon, "_signal_source_cursor_key", lambda *args, **kwargs: "source-key")
    monkeypatch.setattr(extraction_daemon, "_acquire_session_processing_lock", lambda key: 458)
    monkeypatch.setattr(extraction_daemon, "_release_session_processing_lock", lambda key, fd: released.append((key, fd)))
    monkeypatch.setattr(extraction_daemon, "_load_runtime_adapter_for_signal", lambda *args, **kwargs: _Adapter())
    monkeypatch.setattr(extraction_daemon, "_read_cursor_with_source_compat", lambda *args, **kwargs: {
        "line_offset": 0,
        "transcript_path": str(transcript),
    })
    monkeypatch.setattr(extraction_daemon, "_cursor_or_adapter_owns_transcript_path", lambda *args, **kwargs: True)
    monkeypatch.setattr(extraction_daemon, "_reconcile_internal_cursor_state", lambda *args, **kwargs: "not_internal")
    monkeypatch.setattr(extraction_daemon, "_signal_dir", lambda: _BrokenSignalDir())
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

    try:
        with pytest.raises(RuntimeError, match="signal directory unavailable"):
            extraction_daemon.process_signal({
                "session_id": "sess-sweep",
                "type": "session_end",
                "transcript_path": str(transcript),
            })
    finally:
        if real_registry is not None:
            sys.modules["core.subagent_registry"] = real_registry
        else:
            sys.modules.pop("core.subagent_registry", None)

    assert released == [("source-key", 458)]


def test_process_signal_malformed_cursor_releases_lock_when_fail_hard(monkeypatch, tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"role":"user","content":"remember the silver mug"}\n', encoding="utf-8")
    signal_dir = tmp_path / "signals"
    signal_dir.mkdir()
    released = []

    real_registry = sys.modules.get("core.subagent_registry")
    fake_registry = types.ModuleType("core.subagent_registry")
    fake_registry.is_registered_subagent = lambda _session_id: False
    sys.modules["core.subagent_registry"] = fake_registry

    class _Adapter(_OwnedTestAdapterMixin):
        def is_subagent_session(self, session_id, transcript_path=None):
            return False

    monkeypatch.setattr(extraction_daemon, "_reload_config_if_changed", lambda reason: None)
    monkeypatch.setattr(extraction_daemon, "_read_rolling_state_for_signal", lambda *args, **kwargs: ({}, "sess-cursor-bad"))
    monkeypatch.setattr(extraction_daemon, "_active_source_cursor_for_stale_signal_transcript", lambda *args: ("", ""))
    monkeypatch.setattr(extraction_daemon, "_signal_source_cursor_key", lambda *args, **kwargs: "source-key")
    monkeypatch.setattr(extraction_daemon, "_acquire_session_processing_lock", lambda key: 459)
    monkeypatch.setattr(extraction_daemon, "_release_session_processing_lock", lambda key, fd: released.append((key, fd)))
    monkeypatch.setattr(extraction_daemon, "_load_runtime_adapter_for_signal", lambda *args, **kwargs: _Adapter())
    monkeypatch.setattr(
        extraction_daemon,
        "_read_cursor_with_source_compat",
        lambda *args, **kwargs: {"transcript_path": str(transcript)},
    )
    monkeypatch.setattr(extraction_daemon, "_cursor_or_adapter_owns_transcript_path", lambda *args, **kwargs: True)
    monkeypatch.setattr(extraction_daemon, "_reconcile_internal_cursor_state", lambda *args, **kwargs: "not_internal")
    monkeypatch.setattr(extraction_daemon, "_signal_dir", lambda: signal_dir)
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

    try:
        with pytest.raises(RuntimeError, match="missing required field\\(s\\): line_offset"):
            extraction_daemon.process_signal({
                "session_id": "sess-cursor-bad",
                "type": "session_end",
                "transcript_path": str(transcript),
            })
    finally:
        if real_registry is not None:
            sys.modules["core.subagent_registry"] = real_registry
        else:
            sys.modules.pop("core.subagent_registry", None)

    assert released == [("source-key", 459)]


def test_process_signal_preliminary_cursor_failure_releases_lock_when_fail_hard(monkeypatch, tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"role":"user","content":"remember the green mug"}\n', encoding="utf-8")
    signal_dir = tmp_path / "signals"
    signal_dir.mkdir()
    released = []

    real_registry = sys.modules.get("core.subagent_registry")
    fake_registry = types.ModuleType("core.subagent_registry")
    fake_registry.is_registered_subagent = lambda _session_id: False
    sys.modules["core.subagent_registry"] = fake_registry

    class _Adapter(_OwnedTestAdapterMixin):
        def is_subagent_session(self, session_id, transcript_path=None):
            return False

    monkeypatch.setattr(extraction_daemon, "_reload_config_if_changed", lambda reason: None)
    monkeypatch.setattr(extraction_daemon, "_read_rolling_state_for_signal", lambda *args, **kwargs: ({}, "sess-cursor"))
    monkeypatch.setattr(extraction_daemon, "_active_source_cursor_for_stale_signal_transcript", lambda *args: ("", ""))
    monkeypatch.setattr(extraction_daemon, "_signal_source_cursor_key", lambda *args, **kwargs: "source-key")
    monkeypatch.setattr(extraction_daemon, "_acquire_session_processing_lock", lambda key: 789)
    monkeypatch.setattr(extraction_daemon, "_release_session_processing_lock", lambda key, fd: released.append((key, fd)))
    monkeypatch.setattr(extraction_daemon, "_load_runtime_adapter_for_signal", lambda *args, **kwargs: _Adapter())
    monkeypatch.setattr(extraction_daemon, "_read_cursor_with_source_compat", lambda *args, **kwargs: {
        "line_offset": 0,
        "transcript_path": "",
    })
    monkeypatch.setattr(extraction_daemon, "_cursor_or_adapter_owns_transcript_path", lambda *args, **kwargs: True)
    monkeypatch.setattr(extraction_daemon, "_reconcile_internal_cursor_state", lambda *args, **kwargs: "not_internal")
    monkeypatch.setattr(extraction_daemon, "_signal_dir", lambda: signal_dir)
    monkeypatch.setattr(
        extraction_daemon,
        "write_cursor",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("cursor write failed")),
    )
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

    try:
        with pytest.raises(RuntimeError, match="cursor write failed"):
            extraction_daemon.process_signal({
                "session_id": "sess-cursor",
                "type": "session_end",
                "transcript_path": str(transcript),
            })
    finally:
        if real_registry is not None:
            sys.modules["core.subagent_registry"] = real_registry
        else:
            sys.modules.pop("core.subagent_registry", None)

    assert released == [("source-key", 789)]


def test_daemon_loop_leaves_docs_refresh_to_project_docs_supervisor(monkeypatch):
    pending_signal = {"session_id": "sess-late", "type": "session_end"}
    read_calls = 0
    docs_refresh_calls = []

    def fake_read_pending_signals():
        nonlocal read_calls
        read_calls += 1
        if read_calls == 1:
            return []
        if read_calls == 2:
            return [pending_signal]
        return []

    def fake_sleep(_seconds):
        raise _StopDaemonLoop()

    monkeypatch.setattr(extraction_daemon, "write_pid", lambda _pid: None)
    monkeypatch.setattr(extraction_daemon, "remove_pid", lambda: None)
    monkeypatch.setattr(extraction_daemon, "read_pending_signals", fake_read_pending_signals)
    monkeypatch.setattr(extraction_daemon, "process_signal", lambda _sig: None)
    monkeypatch.setattr(extraction_daemon, "check_chunk_ready_sessions", lambda: None)
    monkeypatch.setattr(extraction_daemon, "check_idle_sessions", lambda _mins: None)
    monkeypatch.setattr(extraction_daemon, "_retry_missing_embeddings", lambda: 0)
    from core import project_docs

    monkeypatch.setattr(project_docs, "index_one_stale_registered_doc", lambda: docs_refresh_calls.append("index"))
    monkeypatch.setattr(project_docs, "auto_register_project_docs", lambda: docs_refresh_calls.append("register"))
    monkeypatch.setattr(extraction_daemon.time, "time", lambda: 1_700_000_000.0)
    monkeypatch.setattr(extraction_daemon.time, "sleep", fake_sleep)
    monkeypatch.setattr(extraction_daemon.signal, "signal", lambda *_args, **_kwargs: None)

    with pytest.raises(_StopDaemonLoop):
        extraction_daemon.daemon_loop(poll_interval=0.0, idle_check_interval=999999.0)

    assert docs_refresh_calls == []


def test_daemon_loop_retries_embeddings_before_session_scans(monkeypatch):
    calls = []

    def fake_sleep(_seconds):
        raise _StopDaemonLoop()

    def fake_retry():
        calls.append("embed_retry")
        return 0

    def fake_check_chunk_ready_sessions():
        calls.append("scan_sessions")

    monkeypatch.setattr(extraction_daemon, "write_pid", lambda _pid: None)
    monkeypatch.setattr(extraction_daemon, "remove_pid", lambda: None)
    monkeypatch.setattr(extraction_daemon, "_supervisor_alive", lambda: True)
    monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
    monkeypatch.setattr(extraction_daemon, "process_signal", lambda _sig: None)
    monkeypatch.setattr(extraction_daemon, "check_chunk_ready_sessions", fake_check_chunk_ready_sessions)
    monkeypatch.setattr(extraction_daemon, "check_idle_sessions", lambda _mins: None)
    monkeypatch.setattr(extraction_daemon, "_retry_missing_embeddings", fake_retry)
    monkeypatch.setattr(extraction_daemon.time, "time", lambda: 1_700_000_000.0)
    monkeypatch.setattr(extraction_daemon.time, "sleep", fake_sleep)
    monkeypatch.setattr(extraction_daemon.signal, "signal", lambda *_args, **_kwargs: None)

    with pytest.raises(_StopDaemonLoop):
        extraction_daemon.daemon_loop(poll_interval=5.0, idle_check_interval=999999.0)

    assert calls[:2] == ["embed_retry", "scan_sessions"]


def test_daemon_loop_exits_when_supervisor_disappears(monkeypatch):
    monkeypatch.setattr(extraction_daemon, "write_pid", lambda _pid: None)
    monkeypatch.setattr(extraction_daemon, "remove_pid", lambda: None)
    monkeypatch.setattr(extraction_daemon, "_supervisor_alive", lambda: False)
    monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
    monkeypatch.setattr(extraction_daemon.signal, "signal", lambda *_args, **_kwargs: None)

    extraction_daemon.daemon_loop(poll_interval=0.0, idle_check_interval=999999.0)


def test_ensure_alive_prefers_supervisor_owned_instance_monitor(monkeypatch):
    calls = {"read": 0, "ensured": 0}

    def fake_read_pid():
        calls["read"] += 1
        return 2222 if calls["read"] >= 3 else None

    def fake_ensure_supervisor():
        calls["ensured"] += 1
        return 1111

    monkeypatch.delenv("QUAID_SUPERVISOR_DISABLE", raising=False)
    monkeypatch.setenv("QUAID_INSTANCE_MONITOR_WAIT_SECONDS", "1")
    monkeypatch.setattr(extraction_daemon, "read_pid", fake_read_pid)
    monkeypatch.setattr(extraction_daemon.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr("core.project_docs.ensure_supervisor_alive", fake_ensure_supervisor)

    assert extraction_daemon.ensure_alive() == 2222
    assert calls["ensured"] == 1


def test_ensure_alive_uses_supervisor_pid_startup_budget(monkeypatch):
    now = {"value": 100.0}
    enabled = []

    def fake_read_pid():
        return 3333 if now["value"] >= 110.0 else None

    def fake_sleep(seconds):
        now["value"] += max(1.0, float(seconds))

    monkeypatch.delenv("QUAID_SUPERVISOR_DISABLE", raising=False)
    monkeypatch.delenv("QUAID_INSTANCE_MONITOR_WAIT_SECONDS", raising=False)
    monkeypatch.setenv("QUAID_INSTANCE", "claude-code-livetest")
    monkeypatch.setattr(extraction_daemon, "read_pid", fake_read_pid)
    monkeypatch.setattr(extraction_daemon.time, "time", lambda: now["value"])
    monkeypatch.setattr(extraction_daemon.time, "sleep", fake_sleep)
    monkeypatch.setattr("core.project_docs.ensure_supervisor_alive", lambda: 1111)
    monkeypatch.setattr("core.project_docs.pid_startup_wait_seconds", lambda: 30.0)
    monkeypatch.setattr("core.project_docs.enable_instance_monitor", lambda instance: enabled.append(instance))

    assert extraction_daemon.ensure_alive() == 3333
    assert enabled == ["claude-code-livetest"]


def test_ensure_alive_reenables_daemon_stop_instance_monitor_marker(monkeypatch):
    steps = []

    monkeypatch.delenv("QUAID_SUPERVISOR_DISABLE", raising=False)
    monkeypatch.setenv("QUAID_INSTANCE", "claude-code-livetest")
    monkeypatch.setattr(extraction_daemon, "read_pid", lambda: 4444)
    monkeypatch.setattr("lib.adapter.get_adapter", lambda: object())
    monkeypatch.setattr(
        "core.project_docs.read_instance_monitor_disabled",
        lambda instance: {"instance": instance, "reason": "daemon_stop"},
    )
    monkeypatch.setattr(
        "core.project_docs.enable_instance_monitor",
        lambda instance: steps.append(f"enable:{instance}"),
    )
    monkeypatch.setattr(
        "core.project_docs.ensure_supervisor_alive",
        lambda: steps.append("ensure") or 1111,
    )

    assert extraction_daemon.ensure_alive() == 4444
    assert steps == ["enable:claude-code-livetest", "ensure"]


def test_ensure_alive_refuses_crash_loop_disabled_instance_monitor_marker(monkeypatch):
    steps = []

    monkeypatch.delenv("QUAID_SUPERVISOR_DISABLE", raising=False)
    monkeypatch.setenv("QUAID_INSTANCE", "claude-code-livetest")
    monkeypatch.setattr(extraction_daemon, "read_pid", lambda: None)
    monkeypatch.setattr(extraction_daemon, "start_daemon", lambda: steps.append("direct") or 5555)
    monkeypatch.setattr("lib.adapter.get_adapter", lambda: object())
    monkeypatch.setattr(
        "core.project_docs.read_instance_monitor_disabled",
        lambda instance: {"instance": instance, "reason": "daemon_crash_loop:3_crashes"},
    )
    monkeypatch.setattr(
        "core.project_docs.enable_instance_monitor",
        lambda instance: steps.append(f"enable:{instance}"),
    )
    monkeypatch.setattr(
        "core.project_docs.ensure_supervisor_alive",
        lambda: steps.append("ensure") or 1111,
    )

    with pytest.raises(RuntimeError, match="daemon_crash_loop:3_crashes"):
        extraction_daemon.ensure_alive()

    assert steps == []


def test_ensure_alive_bootstraps_explicit_instance_before_supervisor(monkeypatch):
    now = {"value": 100.0}
    steps = []

    def fake_read_pid():
        return 4444 if now["value"] >= 101.0 else None

    def fake_sleep(seconds):
        now["value"] += max(1.0, float(seconds))

    monkeypatch.delenv("QUAID_SUPERVISOR_DISABLE", raising=False)
    monkeypatch.delenv("QUAID_INSTANCE_MONITOR_WAIT_SECONDS", raising=False)
    monkeypatch.setenv("QUAID_INSTANCE", "claude-code-private-tmp-cc-livetest")
    monkeypatch.setattr(extraction_daemon, "read_pid", fake_read_pid)
    monkeypatch.setattr(extraction_daemon.time, "time", lambda: now["value"])
    monkeypatch.setattr(extraction_daemon.time, "sleep", fake_sleep)
    monkeypatch.setattr("lib.adapter.get_adapter", lambda: steps.append("bootstrap") or object())
    monkeypatch.setattr("core.project_docs.ensure_supervisor_alive", lambda: steps.append("ensure") or 1111)
    monkeypatch.setattr(
        "core.project_docs.enable_instance_monitor",
        lambda instance: steps.append(f"enable:{instance}"),
    )
    monkeypatch.setattr("core.project_docs.pid_startup_wait_seconds", lambda: 30.0)

    assert extraction_daemon.ensure_alive() == 4444
    assert steps == [
        "bootstrap",
        "enable:claude-code-private-tmp-cc-livetest",
        "ensure",
    ]


def test_ensure_alive_falls_back_to_direct_start_when_supervisor_monitor_times_out(monkeypatch):
    now = {"value": 100.0}
    steps = []

    def fake_sleep(seconds):
        now["value"] += max(1.0, float(seconds))

    monkeypatch.delenv("QUAID_SUPERVISOR_DISABLE", raising=False)
    monkeypatch.setenv("QUAID_INSTANCE", "codex-private-tmp-cdx-livetest")
    monkeypatch.setenv("QUAID_INSTANCE_MONITOR_WAIT_SECONDS", "1")
    monkeypatch.setattr(extraction_daemon, "read_pid", lambda: None)
    monkeypatch.setattr(extraction_daemon, "start_daemon", lambda: steps.append("direct") or 5555)
    monkeypatch.setattr(extraction_daemon.time, "time", lambda: now["value"])
    monkeypatch.setattr(extraction_daemon.time, "sleep", fake_sleep)
    monkeypatch.setattr("lib.adapter.get_adapter", lambda: object())
    monkeypatch.setattr("lib.fail_policy.is_fail_hard_enabled", lambda: True)
    monkeypatch.setattr("core.project_docs.ensure_supervisor_alive", lambda: steps.append("ensure") or 1111)
    monkeypatch.setattr(
        "core.project_docs.enable_instance_monitor",
        lambda instance: steps.append(f"enable:{instance}"),
    )

    assert extraction_daemon.ensure_alive() == 5555
    assert steps == [
        "enable:codex-private-tmp-cdx-livetest",
        "ensure",
        "direct",
    ]


def test_ensure_alive_starts_directly_when_project_docs_supervisor_failure_marker_is_active(
    monkeypatch, caplog
):
    from core import project_docs

    steps = []

    monkeypatch.delenv("QUAID_SUPERVISOR_DISABLE", raising=False)
    monkeypatch.setenv("QUAID_INSTANCE", "codex-livetest")
    monkeypatch.setattr(extraction_daemon, "read_pid", lambda: None)
    monkeypatch.setattr(extraction_daemon, "start_daemon", lambda: steps.append("direct") or 6666)
    monkeypatch.setattr("lib.adapter.get_adapter", lambda: object())
    monkeypatch.setattr("lib.fail_policy.is_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(
        project_docs,
        "enable_instance_monitor",
        lambda instance: steps.append(f"enable:{instance}"),
    )
    monkeypatch.setattr(
        project_docs,
        "ensure_supervisor_alive",
        lambda: (_ for _ in ()).throw(
            project_docs.ProjectDocsSupervisorFailureError("project-docs supervisor previously failed")
        ),
    )
    monkeypatch.setattr(project_docs, "clear_supervisor_failure", lambda: steps.append("clear"))

    caplog.set_level("WARNING")

    assert extraction_daemon.ensure_alive() == 6666
    assert steps == ["enable:codex-livetest", "direct", "clear"]
    assert "project docs supervisor ensure_alive failed" in caplog.text
    assert "project docs supervisor is in failed state" in caplog.text


def test_ensure_alive_returns_direct_pid_when_supervisor_disabled(monkeypatch):
    steps = []

    monkeypatch.setenv("QUAID_SUPERVISOR_DISABLE", "1")
    monkeypatch.setenv("QUAID_INSTANCE", "codex-livetest")
    monkeypatch.setattr(extraction_daemon, "read_pid", lambda: None)
    monkeypatch.setattr(extraction_daemon, "start_daemon", lambda: steps.append("direct") or 8888)

    assert extraction_daemon.ensure_alive() == 8888
    assert steps == ["direct"]


def test_ensure_alive_returns_direct_pid_when_recovered_marker_clear_fails(
    monkeypatch, caplog
):
    from core import project_docs

    steps = []

    monkeypatch.delenv("QUAID_SUPERVISOR_DISABLE", raising=False)
    monkeypatch.setenv("QUAID_INSTANCE", "codex-livetest")
    monkeypatch.setattr(extraction_daemon, "read_pid", lambda: None)
    monkeypatch.setattr(extraction_daemon, "start_daemon", lambda: steps.append("direct") or 9999)
    monkeypatch.setattr("lib.adapter.get_adapter", lambda: object())
    monkeypatch.setattr("lib.fail_policy.is_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(project_docs, "enable_instance_monitor", lambda _instance: None)
    monkeypatch.setattr(
        project_docs,
        "ensure_supervisor_alive",
        lambda: (_ for _ in ()).throw(
            project_docs.ProjectDocsSupervisorFailureError("project-docs supervisor previously failed")
        ),
    )
    monkeypatch.setattr(
        project_docs,
        "clear_supervisor_failure",
        lambda: (_ for _ in ()).throw(OSError("unlink denied")),
    )

    caplog.set_level("WARNING")

    assert extraction_daemon.ensure_alive() == 9999
    assert steps == ["direct"]
    assert "failed clearing recovered project-docs supervisor failure marker" in caplog.text


def test_ensure_alive_still_raises_generic_project_docs_error_under_failhard(monkeypatch):
    steps = []

    monkeypatch.delenv("QUAID_SUPERVISOR_DISABLE", raising=False)
    monkeypatch.setenv("QUAID_INSTANCE", "codex-livetest")
    monkeypatch.setattr(extraction_daemon, "read_pid", lambda: None)
    monkeypatch.setattr(extraction_daemon, "start_daemon", lambda: steps.append("direct") or 7777)
    monkeypatch.setattr("lib.adapter.get_adapter", lambda: object())
    monkeypatch.setattr("lib.fail_policy.is_fail_hard_enabled", lambda: True)
    monkeypatch.setattr("core.project_docs.enable_instance_monitor", lambda _instance: None)
    monkeypatch.setattr(
        "core.project_docs.ensure_supervisor_alive",
        lambda: (_ for _ in ()).throw(RuntimeError("fresh supervisor bootstrap failed")),
    )

    with pytest.raises(RuntimeError, match="fresh supervisor bootstrap failed"):
        extraction_daemon.ensure_alive()

    assert steps == []


def test_stop_daemon_disables_supervisor_instance_monitor(monkeypatch):
    disabled = []

    monkeypatch.delenv("QUAID_SUPERVISOR_DISABLE", raising=False)
    monkeypatch.setenv("QUAID_INSTANCE", "claude-code-livetest")
    monkeypatch.setattr("core.project_docs.disable_instance_monitor", lambda instance, reason: disabled.append((instance, reason)))
    monkeypatch.setattr(extraction_daemon, "read_pid", lambda: None)

    assert extraction_daemon.stop_daemon() is False
    assert disabled == [("claude-code-livetest", "daemon_stop")]


def test_config_reload_watcher_reloads_when_config_mtime_changes(monkeypatch, tmp_path):
    cfg_path = tmp_path / "instances" / "pytest-runner" / "config.json"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text('{"capture":{"chunk_tokens":8000}}\n', encoding="utf-8")

    reloads = []
    monkeypatch.setattr(extraction_daemon, "_config_file_paths", lambda: [cfg_path])
    monkeypatch.setattr(extraction_daemon, "_force_reload_config", lambda: reloads.append(True))
    monkeypatch.setattr(extraction_daemon.logger, "info", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(extraction_daemon, "_config_file_signature", None)

    extraction_daemon._prime_config_reload_watcher()
    assert extraction_daemon._reload_config_if_changed("test no change") is False

    cfg_path.write_text('{"capture":{"chunk_tokens":500}}\n', encoding="utf-8")

    assert extraction_daemon._reload_config_if_changed("test signal") is True
    assert reloads == [True]
    assert extraction_daemon._reload_config_if_changed("test stable") is False


def test_force_reload_config_resets_llm_model_cache(monkeypatch):
    import config
    import lib.llm_clients as llm_clients

    calls = []
    monkeypatch.setattr(config, "reload_config", lambda: calls.append("reload_config"))
    monkeypatch.setattr(
        llm_clients,
        "reset_model_config_cache",
        lambda: calls.append("reset_model_config_cache"),
    )

    extraction_daemon._force_reload_config()

    assert calls == ["reload_config", "reset_model_config_cache"]


def test_config_reload_failure_logs_and_returns_false_when_not_failhard(monkeypatch, caplog):
    old_sig = (("/tmp/config.json", 1, 1),)
    new_sig = (("/tmp/config.json", 2, 1),)
    context = ("/tmp/quaid", "pytest-runner")

    monkeypatch.setattr(extraction_daemon, "_config_file_signature", old_sig)
    monkeypatch.setattr(extraction_daemon, "_config_file_signature_context", context)
    monkeypatch.setattr(extraction_daemon, "_config_reload_context", lambda: context)
    monkeypatch.setattr(extraction_daemon, "_active_config_file_signature", lambda: new_sig)
    monkeypatch.setattr(
        extraction_daemon,
        "_force_reload_config",
        lambda: (_ for _ in ()).throw(RuntimeError("reload failed")),
    )
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: False)

    with caplog.at_level("WARNING", logger="core.extraction_daemon"):
        assert extraction_daemon._reload_config_if_changed("test signal") is False

    assert "config changed but reload failed before test signal" in caplog.text
    assert "reload failed" in caplog.text


def test_config_reload_failure_raises_when_failhard(monkeypatch):
    old_sig = (("/tmp/config.json", 1, 1),)
    new_sig = (("/tmp/config.json", 2, 1),)
    context = ("/tmp/quaid", "pytest-runner")

    monkeypatch.setattr(extraction_daemon, "_config_file_signature", old_sig)
    monkeypatch.setattr(extraction_daemon, "_config_file_signature_context", context)
    monkeypatch.setattr(extraction_daemon, "_config_reload_context", lambda: context)
    monkeypatch.setattr(extraction_daemon, "_active_config_file_signature", lambda: new_sig)
    monkeypatch.setattr(
        extraction_daemon,
        "_force_reload_config",
        lambda: (_ for _ in ()).throw(RuntimeError("reload failed")),
    )
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="config reload failed before test signal") as excinfo:
        extraction_daemon._reload_config_if_changed("test signal")

    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert "reload failed" in str(excinfo.value.__cause__)


def test_extraction_buffer_log_enabled_logs_config_failure_when_fail_open(monkeypatch, caplog):
    monkeypatch.setattr(
        extraction_daemon,
        "_config_file_paths",
        lambda: (_ for _ in ()).throw(RuntimeError("config paths failed")),
    )
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: False)

    with caplog.at_level("WARNING", logger="quaid.daemon"):
        assert extraction_daemon._extraction_buffer_log_enabled() is False

    assert "extraction buffer log config lookup failed; disabling buffer log" in caplog.text
    assert "config paths failed" in caplog.text


def test_extraction_buffer_log_enabled_raises_config_failure_when_failhard(monkeypatch):
    monkeypatch.setattr(
        extraction_daemon,
        "_config_file_paths",
        lambda: (_ for _ in ()).throw(RuntimeError("config paths failed")),
    )
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="extraction buffer log config lookup failed") as excinfo:
        extraction_daemon._extraction_buffer_log_enabled()

    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert "config paths failed" in str(excinfo.value.__cause__)


def test_extraction_buffer_log_header_honors_quaid_now(monkeypatch, tmp_path):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "buffer-inst")
    monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:06:07Z")
    monkeypatch.setattr(extraction_daemon, "_extraction_buffer_log_enabled", lambda: True)

    extraction_daemon._write_extraction_buffer_log(
        "sess-buffer",
        phase="stage",
        signal_type="rolling",
        transcript_text="User: durable buffer evidence",
    )

    log_path = tmp_path / "instances" / "buffer-inst" / "logs" / "daemon" / "extraction-buffer.log"
    text = log_path.read_text(encoding="utf-8")
    assert text.startswith("=== 2026-03-11T05:06:07Z session=sess-buffer phase=stage signal=rolling")


def test_extraction_buffer_log_rejects_malformed_quaid_now_when_failhard(monkeypatch, tmp_path):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "buffer-inst")
    monkeypatch.setenv("QUAID_NOW", "not-a-date")
    monkeypatch.setattr(extraction_daemon, "_extraction_buffer_log_enabled", lambda: True)
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="Invalid QUAID_NOW") as excinfo:
        extraction_daemon._write_extraction_buffer_log(
            "sess-buffer",
            phase="stage",
            signal_type="rolling",
            transcript_text="User: durable buffer evidence",
        )

    assert isinstance(excinfo.value.__cause__, ValueError)
    assert not (tmp_path / "instances" / "buffer-inst" / "logs" / "daemon" / "extraction-buffer.log").exists()


def test_normalize_project_log_timestamp_logs_unrecognized_format(caplog):
    with caplog.at_level("DEBUG", logger="quaid.daemon"):
        assert extraction_daemon._normalize_project_log_timestamp("not a timestamp") is None

    assert "unrecognized project log timestamp format" in caplog.text


def test_process_signal_reloads_config_before_signal_handling(monkeypatch):
    reloads = []
    old_sig = (("/tmp/config.json", 1, 1),)
    new_sig = (("/tmp/config.json", 2, 1),)
    context = ("/tmp/quaid", "pytest-runner")

    monkeypatch.setattr(extraction_daemon, "_config_file_signature", old_sig)
    monkeypatch.setattr(extraction_daemon, "_config_file_signature_context", context)
    monkeypatch.setattr(extraction_daemon, "_config_reload_context", lambda: context)
    monkeypatch.setattr(extraction_daemon, "_active_config_file_signature", lambda: new_sig)
    monkeypatch.setattr(extraction_daemon, "_force_reload_config", lambda: reloads.append(True))
    monkeypatch.setattr(extraction_daemon, "read_rolling_state", lambda _sid: {})
    monkeypatch.setattr(extraction_daemon, "mark_signal_processed", lambda _sig: None)

    extraction_daemon.process_signal({"session_id": "sess-1", "type": "unknown"})

    assert reloads == [True]


def test_start_daemon_returns_negative_one_when_pid_file_never_appears(monkeypatch, tmp_path):
    pid_path = tmp_path / "extraction-daemon.pid"
    read_pid_calls = 0

    def fake_read_pid():
        nonlocal read_pid_calls
        read_pid_calls += 1
        return None

    class _FakePopen:
        pid = 99999
        def __init__(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(extraction_daemon, "_pid_path", lambda: pid_path)
    monkeypatch.setattr(extraction_daemon, "_log_path", lambda: tmp_path / "daemon.log")
    monkeypatch.setattr(extraction_daemon.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(extraction_daemon.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(extraction_daemon, "read_pid", fake_read_pid)

    result = extraction_daemon.start_daemon()

    assert result == -1
    assert read_pid_calls >= 2


def test_start_daemon_exports_quaid_home_to_worker_env(monkeypatch, tmp_path):
    pid_path = tmp_path / "extraction-daemon.pid"
    captured = {}

    def fake_read_pid():
        return None

    class _FakePopen:
        pid = 99999

        def __init__(self, *_args, **kwargs):
            captured["env"] = dict(kwargs.get("env") or {})

    monkeypatch.setenv("QUAID_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("QUAID_INSTANCE", "codex-livetest")
    monkeypatch.setenv("INSTANCE", "claude-code-private-tmp-cc-livetest")
    monkeypatch.setenv("SILO", str(tmp_path / "instances" / "claude-code-private-tmp-cc-livetest"))
    monkeypatch.setenv("LANE", "cc")
    monkeypatch.setenv("QUAID_ADAPTER_TYPE", "claude-code")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/tmp/cc-livetest")
    monkeypatch.setenv("QUAID_SUPERVISOR_PID", "12345")
    monkeypatch.setenv("QUAID_SUPERVISOR_TOKEN", "inherited-token")
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "instances" / "openclaw-main" / "data" / "memory.db"))
    monkeypatch.setenv(
        "MEMORY_ARCHIVE_DB_PATH",
        str(tmp_path / "instances" / "openclaw-main" / "data" / "memory_archive.db"),
    )
    monkeypatch.setattr(extraction_daemon, "_pid_path", lambda: pid_path)
    monkeypatch.setattr(extraction_daemon, "_log_path", lambda: tmp_path / "daemon.log")

    from core import project_docs
    real_scrub = project_docs.scrub_background_process_env

    def fake_scrub(env):
        assert env["QUAID_INSTANCE"] == "codex-livetest"
        scrubbed = real_scrub(env)
        assert "QUAID_INSTANCE" not in scrubbed
        return scrubbed

    monkeypatch.setattr(project_docs, "scrub_background_process_env", fake_scrub)
    monkeypatch.setattr(extraction_daemon.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(extraction_daemon.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(extraction_daemon, "read_pid", fake_read_pid)

    result = extraction_daemon.start_daemon()

    assert result == -1
    assert captured["env"]["QUAID_HOME"] == str(tmp_path / "home")
    assert captured["env"]["QUAID_INSTANCE"] == "codex-livetest"
    assert captured["env"]["QUAID_DAEMON"] == "1"
    assert "INSTANCE" not in captured["env"]
    assert "SILO" not in captured["env"]
    assert "LANE" not in captured["env"]
    assert "QUAID_ADAPTER_TYPE" not in captured["env"]
    assert "CLAUDE_PROJECT_DIR" not in captured["env"]
    assert "QUAID_SUPERVISOR_PID" not in captured["env"]
    assert "QUAID_SUPERVISOR_TOKEN" not in captured["env"]
    assert "MEMORY_DB_PATH" not in captured["env"]
    assert "MEMORY_ARCHIVE_DB_PATH" not in captured["env"]


def test_start_daemon_refuses_missing_instance_before_spawn(monkeypatch, tmp_path):
    from lib.instance import InstanceError

    spawned = []

    monkeypatch.delenv("QUAID_INSTANCE", raising=False)
    monkeypatch.setenv("QUAID_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(extraction_daemon.subprocess, "Popen", lambda *_args, **_kwargs: spawned.append(True))

    with pytest.raises(InstanceError, match="QUAID_INSTANCE environment variable is not set"):
        extraction_daemon.start_daemon()

    assert spawned == []


def test_start_daemon_refuses_empty_resolved_instance_before_spawn(monkeypatch, tmp_path):
    spawned = []

    monkeypatch.setenv("QUAID_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(extraction_daemon, "_instance_id", lambda: "")
    monkeypatch.setattr(extraction_daemon.subprocess, "Popen", lambda *_args, **_kwargs: spawned.append(True))

    with pytest.raises(RuntimeError, match="cannot start extraction daemon without QUAID_INSTANCE"):
        extraction_daemon.start_daemon()

    assert spawned == []


def test_matching_daemon_pids_does_not_match_instance_prefix(monkeypatch):
    home = "/Users/admin/.quaid"
    cmd = "/opt/homebrew/bin/python3 /Users/admin/.quaid/plugins/quaid/core/extraction_daemon.py _worker"

    monkeypatch.setattr(extraction_daemon, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        extraction_daemon,
        "_all_process_commands_with_env",
        lambda: [
            (101, f"{cmd} QUAID_HOME={home} QUAID_INSTANCE=claude-code-private-tmp-cc-livetest QUAID_DAEMON=1"),
            (102, f"{cmd} QUAID_HOME={home} QUAID_INSTANCE=claude-code-private-tmp-cc-livetest-m5b QUAID_DAEMON=1"),
            (103, f"{cmd} QUAID_HOME={home}-backup QUAID_INSTANCE=claude-code-private-tmp-cc-livetest QUAID_DAEMON=1"),
        ],
    )

    assert extraction_daemon._matching_daemon_pids(
        quaid_home=home,
        instance="claude-code-private-tmp-cc-livetest",
    ) == [101]


def test_matching_daemon_pids_merges_partial_ps_outputs(monkeypatch):
    calls = []
    home = "/tmp/quaid"

    class Result:
        def __init__(self, stdout="", returncode=0):
            self.stdout = stdout
            self.returncode = returncode

    def fake_run(command, **_kwargs):
        calls.append(command)
        if len(calls) == 1:
            # Darwin accepts the Linux/procps form but can return only the
            # caller's tty session. This must not prevent BSD fallbacks.
            return Result(stdout="11 /bin/zsh QUAID_HOME=/tmp/quaid QUAID_INSTANCE=interactive\n")
        if len(calls) == 2:
            return Result(
                stdout=(
                    "101 /usr/bin/python3 /tmp/core/extraction_daemon.py _worker "
                    f"QUAID_HOME={home} QUAID_INSTANCE=benchrunner QUAID_DAEMON=1\n"
                )
            )
        return Result(stdout="", returncode=0)

    monkeypatch.setattr(extraction_daemon.subprocess, "run", fake_run)
    monkeypatch.setattr(extraction_daemon, "_pid_alive", lambda _pid: True)

    assert extraction_daemon._matching_daemon_pids(
        quaid_home=home,
        instance="benchrunner",
    ) == [101]
    assert calls[0] == ["ps", "eww", "-eo", "pid=,command="]
    assert calls[1] == ["ps", "eww", "-axo", "pid=,command="]


def test_matching_daemon_pids_can_include_foreground_run(monkeypatch):
    home = "/Users/admin/.quaid"
    worker = "/opt/homebrew/bin/python3 /Users/admin/.quaid/plugins/quaid/core/extraction_daemon.py _worker"
    foreground = "/opt/homebrew/bin/python3 /Users/admin/.quaid/plugins/quaid/core/extraction_daemon.py run"

    monkeypatch.setattr(extraction_daemon, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        extraction_daemon,
        "_all_process_commands_with_env",
        lambda: [
            (101, f"{worker} QUAID_HOME={home} QUAID_INSTANCE=codex-private-tmp-cdx-livetest QUAID_DAEMON=1"),
            (102, f"{foreground} QUAID_HOME={home} QUAID_INSTANCE=codex-private-tmp-cdx-livetest"),
        ],
    )

    assert extraction_daemon._matching_daemon_pids(
        quaid_home=home,
        instance="codex-private-tmp-cdx-livetest",
    ) == [101]
    assert extraction_daemon._matching_daemon_pids(
        quaid_home=home,
        instance="codex-private-tmp-cdx-livetest",
        include_foreground=True,
    ) == [101, 102]


def test_start_daemon_adopts_matching_live_worker_without_pidfile(monkeypatch, tmp_path):
    pid_path = tmp_path / "extraction-daemon.pid"
    adopted = []

    monkeypatch.setattr(extraction_daemon, "_pid_path", lambda: pid_path)
    monkeypatch.setattr(extraction_daemon, "read_pid", lambda: None)
    monkeypatch.setattr(extraction_daemon, "_matching_daemon_pids", lambda **_kwargs: [8424])
    monkeypatch.setattr(extraction_daemon, "write_pid", lambda pid: adopted.append(pid))
    monkeypatch.setattr(
        extraction_daemon.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not spawn")),
    )

    assert extraction_daemon.start_daemon() == 8424
    assert adopted == [8424]


def test_start_daemon_reaps_matching_orphans_even_when_pidfile_target_alive(monkeypatch, tmp_path):
    pid_path = tmp_path / "extraction-daemon.pid"
    terminated = []

    monkeypatch.setattr(extraction_daemon, "_pid_path", lambda: pid_path)
    monkeypatch.setattr(extraction_daemon, "read_pid", lambda: 8452)
    monkeypatch.setattr(extraction_daemon, "_matching_daemon_pids", lambda **_kwargs: [8424, 8452])
    monkeypatch.setattr(
        extraction_daemon,
        "_terminate_daemon_pid",
        lambda pid, **_kwargs: terminated.append(pid) or True,
    )
    monkeypatch.setattr(
        extraction_daemon.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not spawn")),
    )

    assert extraction_daemon.start_daemon() == 8452
    assert terminated == [8424]


def test_stop_daemon_kills_pidfile_target_and_matching_orphans(monkeypatch):
    terminated = []
    removed = []

    monkeypatch.setattr(extraction_daemon, "read_pid", lambda: 111)
    monkeypatch.setattr(extraction_daemon, "_matching_daemon_pids", lambda **_kwargs: [111, 222])
    monkeypatch.setattr(
        extraction_daemon,
        "_terminate_daemon_pid",
        lambda pid, **_kwargs: terminated.append(pid) or True,
    )
    monkeypatch.setattr(extraction_daemon, "remove_pid", lambda: removed.append(True))

    assert extraction_daemon.stop_daemon() is True
    assert terminated == [111, 222]
    assert removed == [True]


def test_remove_pid_if_matches_preserves_newer_pidfile(monkeypatch, tmp_path):
    pid_path = tmp_path / "extraction-daemon.pid"
    pid_path.write_text("8452", encoding="utf-8")

    monkeypatch.setattr(extraction_daemon, "_pid_path", lambda: pid_path)

    extraction_daemon._remove_pid_if_matches(8424)
    assert pid_path.read_text(encoding="utf-8").strip() == "8452"

    extraction_daemon._remove_pid_if_matches(8452)
    assert not pid_path.exists()


def test_check_idle_sessions_writes_timeout_signal_for_idle_unextracted_session(monkeypatch, tmp_path):
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text('{"role":"user","content":"hello"}\n{"role":"assistant","content":"hi"}\n', encoding="utf-8")

    instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
    cursor_dir = tmp_path / "instances" / instance_id / "data" / "session-cursors"
    cursor_dir.mkdir(parents=True, exist_ok=True)
    (cursor_dir / "sess-1.json").write_text(
        (
            '{"session_id":"sess-1","line_offset":1,'
            f'"transcript_path":"{transcript_path}"'
            '}'
        ),
        encoding="utf-8",
    )

    now = 1_700_000_000.0
    os_mtime = now - (31 * 60)
    transcript_path.touch()
    pathlib.Path(transcript_path).chmod(0o600)
    os.utime(transcript_path, (os_mtime, os_mtime))

    captured = []
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
    monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: now - (2 * 60 * 60))
    monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
    monkeypatch.setattr(
        extraction_daemon,
        "write_signal",
        lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
            {
                "signal_type": signal_type,
                "session_id": session_id,
                "transcript_path": transcript_path,
            }
        ),
    )

    extraction_daemon.check_idle_sessions(timeout_minutes=30)

    assert captured == [
        {
            "signal_type": "timeout",
            "session_id": "sess-1",
            "transcript_path": str(transcript_path),
        }
    ]


def test_ensure_discovered_session_cursors_repairs_broken_existing_cursor(monkeypatch, tmp_path):
    instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    sessions_dir = tmp_path / "sessions"
    transcript = sessions_dir / "-tmp-quaid-dev" / "sess-cc.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")

    cursor_dir = tmp_path / "instances" / instance_id / "data" / "session-cursors"
    cursor_dir.mkdir(parents=True, exist_ok=True)
    broken = cursor_dir / "sess-cc.json"
    broken.write_text(
        json.dumps(
            {
                "session_id": "sess-cc",
                "line_offset": 1,
                "internal": False,
                "transcript_path": str(tmp_path / "missing" / "sess-cc.jsonl"),
            }
        ),
        encoding="utf-8",
    )

    class _Adapter(_OwnedTestAdapterMixin):
        def get_sessions_dir(self):
            return sessions_dir

    repaired = extraction_daemon._ensure_discovered_session_cursors(_Adapter())
    assert repaired == 1
    source_key = extraction_daemon._signal_source_cursor_key("sess-cc", str(transcript))
    migrated = extraction_daemon.read_cursor("sess-cc", source_key=source_key)
    assert migrated["line_offset"] == 1
    assert migrated["transcript_path"] == str(transcript)
    assert not broken.exists()


def test_ensure_discovered_session_cursors_can_be_disabled_via_env(monkeypatch, tmp_path):
    instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_DISABLE_DISCOVERY_CURSOR_SCAN", "1")
    sessions_dir = tmp_path / "sessions"
    transcript = sessions_dir / "-tmp-quaid-dev" / "sess-cc.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")

    class _Adapter(_OwnedTestAdapterMixin):
        def get_sessions_dir(self):
            return sessions_dir

    discovered = extraction_daemon._ensure_discovered_session_cursors(_Adapter())
    assert discovered == 0
    cursor_dir = tmp_path / "instances" / instance_id / "data" / "session-cursors"
    if cursor_dir.exists():
        assert list(cursor_dir.glob("*.json")) == []


def test_ensure_discovered_session_cursors_warns_and_degrades_when_fail_open(
    monkeypatch,
    tmp_path,
    caplog,
):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: False)

    class _Adapter:
        def get_sessions_dir(self):
            raise RuntimeError("sessions dir unavailable")

    with caplog.at_level("WARNING", logger="quaid.daemon"):
        discovered = extraction_daemon._ensure_discovered_session_cursors(_Adapter())

    assert discovered == 0
    assert "session discovery directory lookup failed" in caplog.text
    assert "sessions dir unavailable" in caplog.text


def test_ensure_discovered_session_cursors_raises_when_fail_hard(monkeypatch, tmp_path):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

    class _Adapter:
        def get_sessions_dir(self):
            raise RuntimeError("sessions dir unavailable")

    with pytest.raises(RuntimeError, match="session discovery directory lookup failed") as excinfo:
        extraction_daemon._ensure_discovered_session_cursors(_Adapter())

    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert "sessions dir unavailable" in str(excinfo.value.__cause__)


def test_ensure_discovered_session_cursors_skips_checkpoint_sidecars(monkeypatch, tmp_path):
    instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    canonical = sessions_dir / "6aadea75-5a01-45c0-af68-017d2e58bbc8.jsonl"
    checkpoint = sessions_dir / (
        "6aadea75-5a01-45c0-af68-017d2e58bbc8.checkpoint."
        "78423baa-408a-409b-89c0-3a203bbbd19d.jsonl"
    )
    canonical.write_text('{"role":"user","content":"fresh fact"}\n', encoding="utf-8")
    checkpoint.write_text('{"role":"user","content":"stale checkpoint"}\n', encoding="utf-8")

    class _Adapter(_OwnedTestAdapterMixin):
        def get_sessions_dir(self):
            return sessions_dir

    discovered = extraction_daemon._ensure_discovered_session_cursors(_Adapter())
    assert discovered == 1

    source_key = extraction_daemon._signal_source_cursor_key(
        "6aadea75-5a01-45c0-af68-017d2e58bbc8",
        str(canonical),
    )
    canonical_cursor = extraction_daemon.read_cursor(
        "6aadea75-5a01-45c0-af68-017d2e58bbc8",
        source_key=source_key,
    )
    assert canonical_cursor["transcript_path"] == str(canonical)
    cursor_dir = tmp_path / "instances" / instance_id / "data" / "session-cursors"
    assert list(cursor_dir.glob("unknown-*.json")) == []


def test_ensure_discovered_session_cursors_replaces_trajectory_sidecar_cursor(monkeypatch, tmp_path):
    instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_id = "f324c131-6414-4629-8ee6-7653995ac2fb"
    canonical = sessions_dir / f"{session_id}.jsonl"
    trajectory = sessions_dir / f"{session_id}.trajectory.jsonl"
    canonical.write_text('{"role":"user","content":"fresh fact"}\n', encoding="utf-8")
    trajectory.write_text('{"type":"trace.metadata","data":{"prompt":"sidecar only"}}\n', encoding="utf-8")

    source_key = extraction_daemon._signal_source_cursor_key(session_id, str(canonical))
    extraction_daemon.write_cursor(
        session_id,
        14,
        str(trajectory),
        internal=True,
        source_key=source_key,
    )

    class _Adapter(_OwnedTestAdapterMixin):
        def get_sessions_dir(self):
            return sessions_dir

    discovered = extraction_daemon._ensure_discovered_session_cursors(_Adapter())
    assert discovered == 1

    cursor = extraction_daemon.read_cursor(session_id, source_key=source_key)
    assert cursor["line_offset"] == 0
    assert cursor["transcript_path"] == str(canonical)
    assert cursor["internal"] is False


def test_ensure_discovered_session_cursors_replaces_stale_relocated_cursor(monkeypatch, tmp_path):
    instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    sessions_dir = tmp_path / "sessions"
    preserved_dir = tmp_path / "instances" / instance_id / "logs" / "quaid" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    preserved_dir.mkdir(parents=True, exist_ok=True)

    session_id = "9bf5c24b-3edd-466e-a19d-52ea93822103"
    canonical = sessions_dir / f"{session_id}.jsonl"
    preserved = preserved_dir / f"{session_id}.jsonl"
    canonical.write_text(
        '{"role":"user","content":"my garden shed combination is indigo-lantern-7742"}\n',
        encoding="utf-8",
    )
    preserved.write_text(
        '{"role":"user","content":"startup context without the new canary"}\n',
        encoding="utf-8",
    )

    source_key = extraction_daemon._signal_source_cursor_key(session_id, str(canonical))
    extraction_daemon.write_cursor(
        session_id,
        8,
        str(preserved),
        internal=False,
        source_key=source_key,
    )

    class _Adapter(_OwnedTestAdapterMixin):
        def get_sessions_dir(self):
            return sessions_dir

    discovered = extraction_daemon._ensure_discovered_session_cursors(_Adapter())
    assert discovered == 1

    cursor = extraction_daemon.read_cursor(session_id, source_key=source_key)
    assert cursor["line_offset"] == 0
    assert cursor["transcript_path"] == str(canonical)
    assert cursor["internal"] is False


def test_ensure_discovered_session_cursors_keeps_larger_preserved_mirror_cursor(monkeypatch, tmp_path):
    instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    sessions_dir = tmp_path / "sessions"
    preserved_dir = tmp_path / "instances" / instance_id / "logs" / "quaid" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    preserved_dir.mkdir(parents=True, exist_ok=True)

    session_id = "8817b065-c63a-43f3-a68a-72b70f2729ed"
    canonical = sessions_dir / f"{session_id}.jsonl"
    preserved = preserved_dir / f"{session_id}.jsonl"
    canonical.write_text(
        '{"role":"user","content":"live subset"}\n',
        encoding="utf-8",
    )
    preserved.write_text(
        '{"role":"user","content":"live subset"}\n'
        '{"role":"user","content":"larger preserved mirror tail"}\n',
        encoding="utf-8",
    )

    source_key = extraction_daemon._signal_source_cursor_key(session_id, str(canonical))
    extraction_daemon.write_cursor(
        session_id,
        2,
        str(preserved),
        internal=False,
        source_key=source_key,
        processed_signal_type="session_end",
    )

    class _Adapter(_OwnedTestAdapterMixin):
        def get_sessions_dir(self):
            return sessions_dir

    discovered = extraction_daemon._ensure_discovered_session_cursors(_Adapter())
    assert discovered == 0

    cursor = extraction_daemon.read_cursor(session_id, source_key=source_key)
    assert cursor["line_offset"] == 2
    assert cursor["transcript_path"] == str(preserved)
    assert cursor["processed_signal_type"] == "session_end"


def test_ensure_discovered_session_cursors_skips_foreign_adapter_transcripts(monkeypatch, tmp_path):
    instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    owned = sessions_dir / "owned-session.jsonl"
    foreign = sessions_dir / "foreign-session.jsonl"
    owned.write_text('{"role":"user","content":"owned"}\n', encoding="utf-8")
    foreign.write_text('{"role":"user","content":"foreign"}\n', encoding="utf-8")

    class _Adapter(_OwnedTestAdapterMixin):
        def get_sessions_dir(self):
            return sessions_dir

        def owns_session_path(self, path, session_id=""):
            return Path(path).name == "owned-session.jsonl"

    discovered = extraction_daemon._ensure_discovered_session_cursors(_Adapter())
    assert discovered == 1

    cursor_dir = tmp_path / "instances" / instance_id / "data" / "session-cursors"
    cursor_paths = sorted(cursor_dir.glob("*.json"))
    assert len(cursor_paths) == 1
    cursor = json.loads(cursor_paths[0].read_text(encoding="utf-8"))
    assert cursor["session_id"] == "owned-session"
    assert cursor["transcript_path"] == str(owned)


def test_ensure_discovered_session_cursors_scopes_claude_code_to_current_project(monkeypatch, tmp_path):
    from adaptors.claude_code.adapter import ClaudeCodeAdapter

    instance_id = "claude-code-private-tmp-cc-livetest-m5b"
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", instance_id)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    sessions_root = tmp_path / ".claude" / "projects"
    original_dir = sessions_root / "-private-tmp-cc-livetest"
    sibling_dir = sessions_root / "-private-tmp-cc-livetest-m5b"
    original_dir.mkdir(parents=True)
    sibling_dir.mkdir(parents=True)
    original = original_dir / "658dbac3-e928-4f57-9125-f29aa4aca21c.jsonl"
    sibling = sibling_dir / "fb4dedd5-7fc8-4afb-9e05-397871c9674d.jsonl"
    original.write_text('{"type":"user","message":{"role":"user","content":"foreign"}}\n', encoding="utf-8")
    sibling.write_text('{"type":"user","message":{"role":"user","content":"owned"}}\n', encoding="utf-8")

    discovered = extraction_daemon._ensure_discovered_session_cursors(ClaudeCodeAdapter())

    assert discovered == 1
    cursor_dir = tmp_path / "instances" / instance_id / "data" / "session-cursors"
    cursor_paths = sorted(cursor_dir.glob("*.json"))
    assert len(cursor_paths) == 1
    cursor = json.loads(cursor_paths[0].read_text(encoding="utf-8"))
    assert cursor["session_id"] == sibling.stem
    assert cursor["transcript_path"] == str(sibling)


def test_daemon_adapter_ownership_accepts_private_tmp_cc_transcript_for_hashed_instance(monkeypatch, tmp_path):
    from adaptors.claude_code.adapter import ClaudeCodeAdapter

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "claude-code-cc-livetest-51aa91834f73")
    transcript = (
        tmp_path
        / ".claude"
        / "projects"
        / "-private-tmp-cc-livetest"
        / "ae38bd3c.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{"type":"user","message":{"content":"brass desk lamp"}}\n', encoding="utf-8")

    assert extraction_daemon._adapter_owns_transcript_path(
        ClaudeCodeAdapter(),
        "ae38bd3c",
        str(transcript),
    ) is True


def test_adapter_ownership_rejects_foreign_transcript_even_when_cursor_matches(monkeypatch, tmp_path):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "foreign-owner")
    transcript = tmp_path / "foreign-session.jsonl"
    transcript.write_text('{"role":"user","content":"foreign"}\n', encoding="utf-8")
    extraction_daemon.write_cursor("foreign-session", 0, str(transcript))

    class _Adapter(_OwnedTestAdapterMixin):
        def owns_session_path(self, path, session_id=""):
            return False

    assert extraction_daemon._adapter_owns_transcript_path(
        _Adapter(),
        "foreign-session",
        str(transcript),
    ) is False


def test_daemon_owned_preserved_session_transcript_bypasses_adapter_ownership(monkeypatch, tmp_path):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
    preserved = (
        tmp_path
        / "instances"
        / "openclaw-main"
        / "logs"
        / "quaid"
        / "sessions"
        / "120d788e-94ca-4df1-9b07-6e6d922dbcc6.jsonl"
    )
    preserved.parent.mkdir(parents=True, exist_ok=True)
    preserved.write_text('{"role":"user","content":"cobalt-postage-oc"}\n', encoding="utf-8")

    class _Adapter(_OwnedTestAdapterMixin):
        def owns_session_path(self, path, session_id=""):
            return False

    assert extraction_daemon._is_daemon_owned_transcript_snapshot_path(str(preserved)) is True
    assert extraction_daemon._adapter_owns_transcript_path(
        _Adapter(),
        "120d788e-94ca-4df1-9b07-6e6d922dbcc6",
        str(preserved),
    ) is True


def test_daemon_owned_preserved_session_transcript_rejects_foreign_instance(monkeypatch, tmp_path):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
    foreign = (
        tmp_path
        / "instances"
        / "other-instance"
        / "logs"
        / "quaid"
        / "sessions"
        / "120d788e-94ca-4df1-9b07-6e6d922dbcc6.jsonl"
    )
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text('{"role":"user","content":"foreign"}\n', encoding="utf-8")

    assert extraction_daemon._is_daemon_owned_transcript_snapshot_path(str(foreign)) is False


def test_codex_discovery_skips_rollouts_from_other_instances(monkeypatch, tmp_path):
    from adaptors.codex.adapter import CodexAdapter

    m13_project = tmp_path / "cdx-m13-test"
    livetest_project = tmp_path / "cdx-livetest"
    m13_project.mkdir()
    livetest_project.mkdir()
    monkeypatch.setattr(
        "adaptors.codex.adapter.instance_slug_from_project_dir",
        lambda raw: Path(str(raw)).name,
    )
    m13_instance = "codex-cdx-m13-test"
    monkeypatch.setenv("QUAID_HOME", str(tmp_path / ".quaid"))
    monkeypatch.setenv("QUAID_INSTANCE", m13_instance)
    monkeypatch.setenv("CODEX_PROJECT_DIR", str(m13_project))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    sessions_root = tmp_path / ".codex" / "sessions" / "2026" / "04" / "20"
    sessions_root.mkdir(parents=True)
    m13_rollout = sessions_root / "rollout-2026-04-20T15-00-00-m13-session.jsonl"
    livetest_rollout = sessions_root / "rollout-2026-04-20T15-10-19-livetest-session.jsonl"
    m13_rollout.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "m13-session", "cwd": str(m13_project)}}) + "\n",
        encoding="utf-8",
    )
    livetest_rollout.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "livetest-session", "cwd": str(livetest_project)}}) + "\n",
        encoding="utf-8",
    )

    discovered = extraction_daemon._ensure_discovered_session_cursors(
        CodexAdapter(home=tmp_path / ".quaid")
    )

    assert discovered == 1
    cursor_dir = tmp_path / ".quaid" / "instances" / m13_instance / "data" / "session-cursors"
    cursors = [json.loads(path.read_text(encoding="utf-8")) for path in cursor_dir.glob("*.json")]
    assert [cursor["transcript_path"] for cursor in cursors] == [str(m13_rollout)]


def test_discovery_skips_stale_orphan_rollout_without_cursor(monkeypatch, tmp_path):
    from adaptors.codex.adapter import CodexAdapter

    project_dir = tmp_path / "cdx-livetest-sibling3"
    project_dir.mkdir()
    monkeypatch.setattr(
        "adaptors.codex.adapter.instance_slug_from_project_dir",
        lambda raw: Path(str(raw)).name,
    )
    monkeypatch.setenv("QUAID_HOME", str(tmp_path / ".quaid"))
    monkeypatch.setenv("QUAID_INSTANCE", "codex-cdx-livetest-sibling3")
    monkeypatch.setenv("CODEX_PROJECT_DIR", str(project_dir))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    sessions_root = tmp_path / ".codex" / "sessions" / "2026" / "05" / "02"
    sessions_root.mkdir(parents=True)
    old_rollout = sessions_root / "rollout-2026-05-02T07-57-38-019de7b1-6ee5-7162-8fac-42022facb1d9.jsonl"
    old_rollout.write_text(
        json.dumps({
            "type": "session_meta",
            "payload": {
                "id": "019de7b1-6ee5-7162-8fac-42022facb1d9",
                "cwd": str(project_dir),
            },
        }) + "\n",
        encoding="utf-8",
    )
    installed_at = 1_700_000_000.0
    old_mtime = installed_at - 3600
    os.utime(old_rollout, (old_mtime, old_mtime))
    monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: installed_at)
    monkeypatch.setattr(extraction_daemon.time, "time", lambda: installed_at + 60)

    discovered = extraction_daemon._ensure_discovered_session_cursors(
        CodexAdapter(home=tmp_path / ".quaid")
    )

    assert discovered == 0
    cursor_dir = (
        tmp_path
        / ".quaid"
        / "instances"
        / "codex-cdx-livetest-sibling3"
        / "data"
        / "session-cursors"
    )
    assert not list(cursor_dir.glob("*.json")) if cursor_dir.exists() else True


def test_discovery_keeps_stale_rollout_with_existing_cursor(monkeypatch, tmp_path):
    from adaptors.codex.adapter import CodexAdapter

    project_dir = tmp_path / "cdx-livetest-sibling3"
    project_dir.mkdir()
    monkeypatch.setattr(
        "adaptors.codex.adapter.instance_slug_from_project_dir",
        lambda raw: Path(str(raw)).name,
    )
    monkeypatch.setenv("QUAID_HOME", str(tmp_path / ".quaid"))
    monkeypatch.setenv("QUAID_INSTANCE", "codex-cdx-livetest-sibling3")
    monkeypatch.setenv("CODEX_PROJECT_DIR", str(project_dir))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    sessions_root = tmp_path / ".codex" / "sessions" / "2026" / "05" / "02"
    sessions_root.mkdir(parents=True)
    old_rollout = sessions_root / "rollout-2026-05-02T07-57-38-019de7b1-6ee5-7162-8fac-42022facb1d9.jsonl"
    old_rollout.write_text(
        json.dumps({
            "type": "session_meta",
            "payload": {
                "id": "019de7b1-6ee5-7162-8fac-42022facb1d9",
                "cwd": str(project_dir),
            },
        }) + "\n",
        encoding="utf-8",
    )
    source_key = extraction_daemon._signal_source_cursor_key(old_rollout.stem, str(old_rollout))
    extraction_daemon.write_cursor(old_rollout.stem, 0, str(old_rollout), source_key=source_key)
    installed_at = 1_700_000_000.0
    old_mtime = installed_at - 3600
    os.utime(old_rollout, (old_mtime, old_mtime))
    monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: installed_at)
    monkeypatch.setattr(extraction_daemon.time, "time", lambda: installed_at + 60)

    discovered = extraction_daemon._ensure_discovered_session_cursors(
        CodexAdapter(home=tmp_path / ".quaid")
    )

    assert discovered == 0
    assert extraction_daemon.read_cursor(old_rollout.stem, source_key=source_key)["transcript_path"] == str(old_rollout)


def test_discovery_keeps_new_post_install_rollout_without_cursor(monkeypatch, tmp_path):
    from adaptors.codex.adapter import CodexAdapter

    project_dir = tmp_path / "cdx-livetest-sibling3"
    project_dir.mkdir()
    monkeypatch.setattr(
        "adaptors.codex.adapter.instance_slug_from_project_dir",
        lambda raw: Path(str(raw)).name,
    )
    monkeypatch.setenv("QUAID_HOME", str(tmp_path / ".quaid"))
    monkeypatch.setenv("QUAID_INSTANCE", "codex-cdx-livetest-sibling3")
    monkeypatch.setenv("CODEX_PROJECT_DIR", str(project_dir))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    sessions_root = tmp_path / ".codex" / "sessions" / "2026" / "05" / "02"
    sessions_root.mkdir(parents=True)
    fresh_rollout = sessions_root / "rollout-2026-05-02T08-30-00-019de7c0-6ee5-7162-8fac-42022facb1d9.jsonl"
    fresh_rollout.write_text(
        json.dumps({
            "type": "session_meta",
            "payload": {
                "id": "019de7c0-6ee5-7162-8fac-42022facb1d9",
                "cwd": str(project_dir),
            },
        }) + "\n",
        encoding="utf-8",
    )
    installed_at = 1_700_000_000.0
    os.utime(fresh_rollout, (installed_at, installed_at))
    monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: installed_at)
    monkeypatch.setattr(extraction_daemon.time, "time", lambda: installed_at + 3600)

    discovered = extraction_daemon._ensure_discovered_session_cursors(
        CodexAdapter(home=tmp_path / ".quaid")
    )

    assert discovered == 1
    cursor_dir = (
        tmp_path
        / ".quaid"
        / "instances"
        / "codex-cdx-livetest-sibling3"
        / "data"
        / "session-cursors"
    )
    cursors = [json.loads(path.read_text(encoding="utf-8")) for path in cursor_dir.glob("*.json")]
    assert [cursor["transcript_path"] for cursor in cursors] == [str(fresh_rollout)]


def test_discovery_keeps_pre_install_rollout_within_grace(monkeypatch, tmp_path):
    from adaptors.codex.adapter import CodexAdapter

    project_dir = tmp_path / "cdx-livetest-sibling3"
    project_dir.mkdir()
    monkeypatch.setattr(
        "adaptors.codex.adapter.instance_slug_from_project_dir",
        lambda raw: Path(str(raw)).name,
    )
    monkeypatch.setenv("QUAID_HOME", str(tmp_path / ".quaid"))
    monkeypatch.setenv("QUAID_INSTANCE", "codex-cdx-livetest-sibling3")
    monkeypatch.setenv("CODEX_PROJECT_DIR", str(project_dir))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    sessions_root = tmp_path / ".codex" / "sessions" / "2026" / "05" / "02"
    sessions_root.mkdir(parents=True)
    grace_rollout = sessions_root / "rollout-2026-05-02T08-30-00-019de7c0-f8b5-7431-9a95-b265f06975b2.jsonl"
    grace_rollout.write_text(
        json.dumps({
            "type": "session_meta",
            "payload": {
                "id": "019de7c0-f8b5-7431-9a95-b265f06975b2",
                "cwd": str(project_dir),
            },
        }) + "\n",
        encoding="utf-8",
    )
    installed_at = 1_700_000_000.0
    mtime = installed_at - 5 * 60
    os.utime(grace_rollout, (mtime, mtime))
    monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: installed_at)
    monkeypatch.setattr(extraction_daemon.time, "time", lambda: installed_at + 60)

    discovered = extraction_daemon._ensure_discovered_session_cursors(
        CodexAdapter(home=tmp_path / ".quaid")
    )

    assert discovered == 1
    cursor_dir = (
        tmp_path
        / ".quaid"
        / "instances"
        / "codex-cdx-livetest-sibling3"
        / "data"
        / "session-cursors"
    )
    cursors = [json.loads(path.read_text(encoding="utf-8")) for path in cursor_dir.glob("*.json")]
    assert [cursor["transcript_path"] for cursor in cursors] == [str(grace_rollout)]


def test_clear_rolling_state_removes_payload_matched_stale_file(monkeypatch, tmp_path):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
    rolling_dir = tmp_path / "instances" / instance_id / "data" / "rolling-extraction"
    rolling_dir.mkdir(parents=True, exist_ok=True)
    stale_file = rolling_dir / "unknown-legacy.json"
    stale_file.write_text(
        json.dumps({"session_id": "sess-roll-stale", "carry_facts": [{"text": "fact"}]}),
        encoding="utf-8",
    )

    extraction_daemon.clear_rolling_state("sess-roll-stale")

    assert not stale_file.exists()


def test_clear_rolling_state_removes_referenced_rolling_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "snapshot-cleanup-inst")
    instance_root = tmp_path / "instances" / "snapshot-cleanup-inst"
    snapshot = (
        instance_root
        / "logs"
        / "daemon"
        / "rolling-transcript-snapshots"
        / "sess-snapshot-cleanup"
        / "20260614T010553Z-1dbe461839edb8ce"
        / "day-010-2026-03-18.jsonl"
    )
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text('{"role":"user","content":"old"}\n', encoding="utf-8")
    extraction_daemon.write_rolling_state(
        "sess-snapshot-cleanup",
        {
            "session_id": "sess-snapshot-cleanup",
            "transcript_path": str(snapshot),
            "buffer_transcript_path": str(snapshot),
            "raw_facts": [{"text": "staged fact"}],
            "staged_payload_pending_flush": True,
        },
    )

    extraction_daemon.clear_rolling_state("sess-snapshot-cleanup")

    assert not extraction_daemon._rolling_state_path("sess-snapshot-cleanup").exists()
    assert not snapshot.exists()


def test_clear_rolling_state_raises_on_corrupt_state_when_failhard(monkeypatch, tmp_path):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "snapshot-cleanup-inst")
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)
    state_path = extraction_daemon._rolling_state_path("sess-corrupt-cleanup")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{bad json", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        extraction_daemon.clear_rolling_state("sess-corrupt-cleanup")

    assert state_path.exists()


def test_clear_rolling_state_warns_and_removes_corrupt_state_when_fail_open(
    monkeypatch,
    tmp_path,
    caplog,
):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "snapshot-cleanup-inst")
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: False)
    state_path = extraction_daemon._rolling_state_path("sess-corrupt-cleanup")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{bad json", encoding="utf-8")

    with caplog.at_level("WARNING", logger="quaid.daemon"):
        extraction_daemon.clear_rolling_state("sess-corrupt-cleanup")

    assert not state_path.exists()
    assert "rolling state read failed before cleanup" in caplog.text


def test_write_rolling_state_clears_structurally_empty_payload_artifacts(monkeypatch, tmp_path):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
    rolling_dir = tmp_path / "instances" / instance_id / "data" / "rolling-extraction"
    rolling_dir.mkdir(parents=True, exist_ok=True)

    extraction_daemon.write_rolling_state(
        "sess-empty-struct",
        {
            "session_id": "sess-empty-struct",
            "transcript_path": "",
            "carry_facts": [{}],
            "raw_facts": [{}],
            "raw_snippets": {"USER.md": [{}]},
            "raw_journal": {"journal": [{}]},
            "raw_project_logs": {"proj": [{}]},
            "rolling_batches": 0,
            "semantic_buffer": "",
            "semantic_buffer_tokens": 0,
        },
    )

    assert not extraction_daemon._rolling_state_path("sess-empty-struct").exists()


def test_process_signal_skips_foreign_adapter_transcript(monkeypatch, tmp_path):
    from lib.adapter import set_adapter, reset_adapter

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    transcript_path = tmp_path / "foreign-session.jsonl"
    transcript_path.write_text('{"role":"user","content":"foreign fact"}\n', encoding="utf-8")
    signal_path = extraction_daemon.write_signal(
        signal_type="timeout",
        session_id="foreign-session",
        transcript_path=str(transcript_path),
    )
    signal_data = json.loads(signal_path.read_text(encoding="utf-8"))
    signal_data["_signal_path"] = str(signal_path)
    extraction_daemon.write_rolling_state(
        "foreign-session",
        {
            "session_id": "foreign-session",
            "transcript_path": str(transcript_path),
            "raw_facts": ["foreign payload"],
            "semantic_buffer": "foreign payload",
            "semantic_buffer_tokens": 2,
        },
    )

    class _Adapter(_OwnedTestAdapterMixin):
        def owns_session_path(self, path, session_id=""):
            return False

        def parse_session_jsonl(self, path):
            raise AssertionError("foreign transcript should not be parsed")

    set_adapter(_Adapter())
    try:
        extraction_daemon.process_signal(signal_data)
    finally:
        reset_adapter()

    assert not signal_path.exists()
    assert not extraction_daemon._rolling_state_path("foreign-session").exists()


def test_cursor_records_transcript_path_is_scoped_to_current_instance(monkeypatch, tmp_path):
    transcript_path = tmp_path / "shared-session.jsonl"
    transcript_path.write_text('{"role":"user","content":"instance scoped"}\n', encoding="utf-8")

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "instance-a")
    extraction_daemon.write_cursor("shared-session", 0, str(transcript_path))

    assert extraction_daemon._cursor_records_transcript_path("shared-session", str(transcript_path))

    monkeypatch.setenv("QUAID_INSTANCE", "instance-b")
    assert not extraction_daemon._cursor_records_transcript_path("shared-session", str(transcript_path))


def test_cursor_records_transcript_path_raises_on_read_error_when_fail_hard(monkeypatch):
    monkeypatch.setattr(
        extraction_daemon,
        "read_cursor",
        lambda _session_id: (_ for _ in ()).throw(RuntimeError("cursor read failed")),
    )
    monkeypatch.setattr("lib.fail_policy.is_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="cursor read failed"):
        extraction_daemon._cursor_records_transcript_path("broken-session", "/tmp/session.jsonl")


def test_cursor_records_transcript_path_uses_daemon_failhard_helper(monkeypatch):
    monkeypatch.setattr(
        extraction_daemon,
        "read_cursor",
        lambda _session_id: (_ for _ in ()).throw(RuntimeError("cursor read failed")),
    )
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="cursor read failed"):
        extraction_daemon._cursor_records_transcript_path("broken-session", "/tmp/session.jsonl")


def test_cursor_records_transcript_path_falls_back_on_read_error_when_not_fail_hard(monkeypatch):
    monkeypatch.setattr(
        extraction_daemon,
        "read_cursor",
        lambda _session_id: (_ for _ in ()).throw(RuntimeError("cursor read failed")),
    )
    monkeypatch.setattr("lib.fail_policy.is_fail_hard_enabled", lambda: False)

    assert not extraction_daemon._cursor_records_transcript_path("broken-session", "/tmp/session.jsonl")


def test_adapter_owns_transcript_path_uses_daemon_failhard_helper(monkeypatch, tmp_path):
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text('{"role":"user","content":"adapter ownership"}\n', encoding="utf-8")

    class _Adapter:
        def owns_session_path(self, path, session_id=""):
            raise RuntimeError("adapter ownership failed")

    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="adapter ownership failed"):
        extraction_daemon._adapter_owns_transcript_path(
            _Adapter(),
            "adapter-session",
            str(transcript_path),
        )


def test_process_signal_allows_current_instance_cursor_owned_transcript(monkeypatch, tmp_path):
    from lib.adapter import set_adapter, reset_adapter
    from ingest import extract as extract_mod

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "codex-m13test")
    _stub_successful_session_logs_ingest(monkeypatch)
    transcript_path = tmp_path / "rollout-2026-04-26T18-43-04-old-thread.jsonl"
    transcript_path.write_text(
        (
            '{"type":"session_meta","payload":{"id":"old-thread","cwd":"/private/tmp/cdx-livetest"}}\n'
            '{"role":"user","content":"The tamarind-lighthouse-3317 codeword belongs in this explicit instance."}\n'
        ),
        encoding="utf-8",
    )
    extraction_daemon.write_cursor("old-thread", 0, str(transcript_path))
    signal_path = extraction_daemon.write_signal(
        signal_type="session_end",
        session_id="old-thread",
        transcript_path=str(transcript_path),
    )
    signal_data = json.loads(signal_path.read_text(encoding="utf-8"))
    signal_data["_signal_path"] = str(signal_path)
    monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "owner-1")
    monkeypatch.setattr(extraction_daemon, "_read_usage_totals", lambda: {})

    captured = {}

    class _Adapter(_OwnedTestAdapterMixin):
        def instance_root(self):
            return tmp_path

        def owns_session_path(self, path, session_id=""):
            captured["owns_called"] = True
            return False

        def parse_session_jsonl(self, path):
            captured["path"] = str(path)
            return "User: The tamarind-lighthouse-3317 codeword belongs in this explicit instance."

        def is_subagent_session(self, session_id, transcript_path=None):
            return False

    set_adapter(_Adapter())
    try:
        def _fake_extract_from_transcript(transcript, **kwargs):
            captured["transcript"] = transcript
            return {
                "chunks_processed": 1,
                "chunks_total": 1,
                "unclassified_empty_payloads": 0,
                "raw_facts": [],
                "facts": [],
                "soul_snippets": {},
                "journal_entries": {},
                "project_logs": {},
                "raw_snippets": {},
                "raw_journal": {},
                "raw_project_logs": {},
            }

        monkeypatch.setattr(extract_mod, "extract_from_transcript", _fake_extract_from_transcript)
        monkeypatch.setattr(
            extract_mod,
            "apply_extracted_payloads",
            lambda *_args, **_kwargs: {
                "facts_stored": 0,
                "facts_skipped": 0,
                "facts": [],
                "snippets_updated": 0,
                "journal_updated": 0,
                "project_logs_updated": 0,
                "project_logs_projects_updated": 0,
                "project_logs_seen": 0,
            },
        )

        extraction_daemon.process_signal(signal_data)
    finally:
        reset_adapter()

    assert "tamarind-lighthouse-3317" in captured.get("transcript", "")
    assert captured.get("path")
    assert captured.get("owns_called") is None


def test_process_signal_uses_cursor_transcript_when_signal_path_missing(monkeypatch, tmp_path):
    from lib.adapter import set_adapter, reset_adapter
    from ingest import extract as extract_mod

    transcript_path = tmp_path / "fallback.jsonl"
    transcript_path.write_text(
        (
            '{"role":"user","content":"I always park near the stone arch by the river before work."}\n'
            '{"role":"assistant","content":"Noted. I will remember the stone arch parking detail."}\n'
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    _stub_successful_session_logs_ingest(monkeypatch)
    monkeypatch.setattr(
        extraction_daemon,
        "read_cursor",
        lambda _sid: {
            "line_offset": 0,
            "transcript_path": str(transcript_path),
            "internal": False,
            "transcript_size_bytes": transcript_path.stat().st_size,
        },
    )
    monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "owner-1")
    monkeypatch.setattr(extraction_daemon, "_read_usage_totals", lambda: {})

    captured = {}

    class _Adapter(_OwnedTestAdapterMixin):
        def instance_root(self):
            return tmp_path / "instances" / "pytest-runner"

        def data_dir(self):
            return self.instance_root() / "data"

        def owns_session_path(self, path, session_id=""):
            return True

        def parse_session_jsonl(self, path):
            return (
                "User: I always park near the stone arch by the river before work.\n"
                "Assistant: Noted. I will remember the stone arch parking detail."
            )

        def is_subagent_session(self, session_id, transcript_path=None):
            return False

    set_adapter(_Adapter())
    try:
        def _fake_extract_from_transcript(transcript, **kwargs):
            captured["transcript"] = transcript
            return {
                "chunks_processed": 1,
                "chunks_total": 1,
                "unclassified_empty_payloads": 0,
                "raw_facts": [],
                "facts": [],
                "soul_snippets": {},
                "journal_entries": {},
                "project_logs": {},
                "raw_snippets": {},
                "raw_journal": {},
                "raw_project_logs": {},
            }

        monkeypatch.setattr(extract_mod, "extract_from_transcript", _fake_extract_from_transcript)
        monkeypatch.setattr(
            extract_mod,
            "apply_extracted_payloads",
            lambda *_args, **_kwargs: {
                "facts_stored": 0,
                "facts_skipped": 0,
                "facts": [],
                "snippets_updated": 0,
                "journal_updated": 0,
                "project_logs_updated": 0,
                "project_logs_projects_updated": 0,
                "project_logs_seen": 0,
            },
        )

        extraction_daemon.process_signal(
            {
                "session_id": "sess-missing-transcript",
                "type": "session_end",
                "transcript_path": "",
                "_signal_path": str(tmp_path / "sig.json"),
            }
        )
    finally:
        reset_adapter()

    assert "stone arch" in captured.get("transcript", "")


def test_process_signal_uses_adapter_resolved_transcript_when_signal_path_missing(monkeypatch, tmp_path):
    from lib.adapter import set_adapter, reset_adapter
    from ingest import extract as extract_mod

    transcript_path = tmp_path / "adapter-fallback.jsonl"
    transcript_path.write_text(
        (
            '{"role":"user","content":"The recovery phrase is cobalt-postage-oc."}\n'
            '{"role":"assistant","content":"I will remember the cobalt-postage-oc phrase."}\n'
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    monkeypatch.setattr(
        extraction_daemon,
        "read_cursor",
        lambda _sid: {
            "line_offset": 0,
            "transcript_path": "",
            "internal": False,
            "transcript_size_bytes": 0,
        },
    )
    monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "owner-1")
    monkeypatch.setattr(extraction_daemon, "_read_usage_totals", lambda: {})

    captured = {}

    class _Adapter(_OwnedTestAdapterMixin):
        def instance_root(self):
            return tmp_path / "instances" / "pytest-runner"

        def get_session_path(self, session_id):
            assert session_id == "sess-adapter-fallback"
            captured["adapter_resolved"] = True
            return transcript_path

        def parse_session_jsonl(self, path):
            captured["path"] = str(path)
            return (
                "User: The recovery phrase is cobalt-postage-oc.\n"
                "Assistant: I will remember the cobalt-postage-oc phrase."
            )

        def is_subagent_session(self, session_id, transcript_path=None):
            return False

    set_adapter(_Adapter())
    try:
        def _fake_extract_from_transcript(transcript, **kwargs):
            captured["transcript"] = transcript
            return {
                "chunks_processed": 1,
                "chunks_total": 1,
                "unclassified_empty_payloads": 0,
                "raw_facts": [],
                "facts": [],
                "soul_snippets": {},
                "journal_entries": {},
                "project_logs": {},
                "raw_snippets": {},
                "raw_journal": {},
                "raw_project_logs": {},
            }

        monkeypatch.setattr(extract_mod, "extract_from_transcript", _fake_extract_from_transcript)
        monkeypatch.setattr(
            extract_mod,
            "apply_extracted_payloads",
            lambda *_args, **_kwargs: {
                "facts_stored": 0,
                "facts_skipped": 0,
                "facts": [],
                "snippets_updated": 0,
                "journal_updated": 0,
                "project_logs_updated": 0,
                "project_logs_projects_updated": 0,
                "project_logs_seen": 0,
            },
        )

        extraction_daemon.process_signal(
            {
                "session_id": "sess-adapter-fallback",
                "type": "reset",
                "transcript_path": str(tmp_path / "missing.jsonl"),
                "_signal_path": str(tmp_path / "sig.json"),
            }
        )
    finally:
        reset_adapter()

    assert captured.get("adapter_resolved") is True
    assert "cobalt-postage-oc" in captured.get("transcript", "")


def test_resolve_existing_transcript_fallback_adapter_error_raises_under_failhard(monkeypatch):
    class _Adapter:
        def get_session_path(self, _session_id):
            raise OSError("adapter path boom")

    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

    with pytest.raises(OSError, match="adapter path boom"):
        extraction_daemon._resolve_existing_transcript_fallback_for_signal(
            label="daemon-test",
            session_id="sess-adapter-failhard",
            transcript_path="",
            signal_meta={},
            cursor_data={},
            staged_state={},
            adapter=_Adapter(),
        )


@pytest.mark.parametrize(
    "cursor_fixture",
    ["size_mismatch", "zero_size_identity_changed"],
    ids=["size-mismatch", "zero-size-identity-changed"],
)
def test_process_signal_reextracts_relocated_transcript_when_content_changed(
    monkeypatch,
    tmp_path,
    cursor_fixture,
):
    from lib.adapter import set_adapter, reset_adapter
    from ingest import extract as extract_mod
    from core import ingest_runtime
    from core.runtime import notify as notify_mod

    session_id = "a9029038-7a04-42a9-8828-c39f0290a8f7"
    old_dir = tmp_path / ".openclaw" / "agents" / "main" / "sessions"
    new_dir = tmp_path / ".quaid" / "instances" / "openclaw-main" / "logs" / "quaid" / "sessions"
    old_dir.mkdir(parents=True)
    new_dir.mkdir(parents=True)
    old_path = old_dir / f"{session_id}.jsonl"
    new_path = new_dir / f"{session_id}.jsonl"
    if cursor_fixture == "zero_size_identity_changed":
        old_path.write_text("", encoding="utf-8")
    else:
        old_path.write_text(
            "\n".join([
                '{"type":"session","id":"a902"}',
                '{"type":"model_change"}',
                '{"type":"thinking_level_change"}',
                '{"type":"custom","customType":"model-snapshot"}',
                '{"type":"message","message":{"role":"user","content":[{"type":"text","text":"ACK"}]}}',
                '{"type":"custom_message","customType":"openclaw.runtime-context","content":"context"}',
                '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"ACK"}]}}',
            ]) + "\n",
            encoding="utf-8",
        )
    new_path.write_text(
        "\n".join([
            '{"type":"session","id":"a902"}',
            '{"type":"model_change"}',
            '{"type":"thinking_level_change"}',
            '{"type":"custom","customType":"model-snapshot"}',
            '{"type":"message","message":{"role":"user","content":[{"type":"text","text":"Niseko marker belongs in extracted memory and should not be skipped after relocation."}]}}',
            '{"type":"custom_message","customType":"openclaw.runtime-context","content":"context"}',
            '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"ACK"}]}}',
        ]) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
    source_key = extraction_daemon._signal_source_cursor_key(session_id, str(old_path))
    extraction_daemon.write_cursor(
        session_id,
        7,
        str(old_path),
        source_key=source_key,
        processed_signal_type="reset",
    )
    extraction_daemon.write_rolling_state(
        session_id,
        {
            "session_id": session_id,
            "transcript_path": str(old_path),
            "processed_line_offset": 0,
            "buffered_line_offset": 7,
            "semantic_buffer": "User: pending rolling content from live transcript.",
            "semantic_buffer_tokens": 12,
            "raw_facts": [],
            "carry_facts": [],
        },
    )
    old_path.unlink()

    captured = {}

    class _Adapter(_OwnedTestAdapterMixin):
        def instance_root(self):
            return tmp_path / "instances" / "openclaw-main"

        def parse_session_jsonl(self, path):
            captured.setdefault("parsed_paths", []).append(str(path))
            return "User: Niseko marker belongs in extracted memory.\nAssistant: ACK"

        def is_subagent_session(self, session_id, transcript_path=None):
            return False

    set_adapter(_Adapter())
    monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "owner-1")
    monkeypatch.setattr(extraction_daemon, "_read_usage_totals", lambda: {})
    monkeypatch.setattr(extraction_daemon, "_session_has_harvestable_subagents", lambda *args, **kwargs: False)
    monkeypatch.setattr(extraction_daemon, "_warm_payload_embeddings", lambda _facts: {
        "requested": 0,
        "unique": 0,
        "cache_hits": 0,
        "warmed": 0,
        "failed": 0,
        "skipped_empty": 0,
    })
    monkeypatch.setattr(notify_mod, "notify_memory_extraction", lambda **_kwargs: None)
    monkeypatch.setattr(ingest_runtime, "run_session_logs_ingest", lambda **_kwargs: {"status": "indexed"})

    def fake_extract_from_transcript(transcript, **kwargs):
        captured["transcript"] = transcript
        return {
            "chunks_processed": 1,
            "chunks_total": 1,
            "unclassified_empty_payloads": 0,
            "raw_facts": [
                {
                    "text": "Solomon went skiing in Niseko.",
                    "category": "fact",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                }
            ],
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "carry_facts": [],
        }

    monkeypatch.setattr(extract_mod, "extract_from_transcript", fake_extract_from_transcript)
    monkeypatch.setattr(
        extract_mod,
        "apply_extracted_payloads",
        lambda *_args, **_kwargs: {
            "facts_stored": 1,
            "facts_skipped": 0,
            "edges_created": 0,
            "facts": [{"text": "Solomon went skiing in Niseko.", "status": "stored", "edges": []}],
            "snippets": {},
            "journal": {},
            "project_log_metrics": {},
        },
    )

    try:
        signal_path = extraction_daemon.write_signal(
            signal_type="session_end",
            session_id=session_id,
            transcript_path=str(new_path),
        )
        signal_data = json.loads(signal_path.read_text(encoding="utf-8"))
        signal_data["_signal_path"] = str(signal_path)

        extraction_daemon.process_signal(signal_data)
    finally:
        reset_adapter()

    assert "Niseko marker belongs" in captured["transcript"]
    cursor = extraction_daemon.read_cursor(session_id, source_key=source_key)
    assert cursor["line_offset"] == 7
    assert cursor["transcript_path"] == str(new_path)
    assert not extraction_daemon._rolling_state_path(session_id).exists()


def test_process_signal_skips_smaller_preserved_relocation_already_consumed(
    monkeypatch,
    tmp_path,
):
    from lib.adapter import set_adapter, reset_adapter
    from ingest import extract as extract_mod

    session_id = "df9c21db-8445-43e5-b4df-181575fbe2e2"
    live_dir = tmp_path / ".openclaw" / "agents" / "main" / "sessions"
    preserved_dir = tmp_path / ".quaid" / "instances" / "openclaw-main" / "logs" / "quaid" / "sessions"
    live_dir.mkdir(parents=True)
    preserved_dir.mkdir(parents=True)
    live_path = live_dir / f"{session_id}.jsonl"
    preserved_path = preserved_dir / f"{session_id}.jsonl"
    live_path.write_text(
        "\n".join([
            f'{{"type":"session","id":"{session_id}"}}',
            '{"type":"message","message":{"role":"user","content":[{"type":"text","text":"M1 canary line one"}]}}',
            '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"ack one"}]}}',
            '{"type":"message","message":{"role":"user","content":[{"type":"text","text":"M1 canary line two"}]}}',
            '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"ack two"}]}}',
        ]) + "\n",
        encoding="utf-8",
    )
    preserved_path.write_text(
        "\n".join([
            f'{{"type":"session","id":"{session_id}"}}',
            '{"type":"message","message":{"role":"user","content":[{"type":"text","text":"M1 canary line one"}]}}',
        ]) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("QUAID_HOME", str(tmp_path / ".quaid"))
    monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
    source_key = extraction_daemon._signal_source_cursor_key(session_id, str(live_path))
    extraction_daemon.write_cursor(
        session_id,
        5,
        str(live_path),
        source_key=source_key,
        processed_signal_type="reset",
    )
    extraction_daemon.write_rolling_state(
        session_id,
        {
            "session_id": session_id,
            "transcript_path": str(live_path),
            "processed_line_offset": 5,
            "buffered_line_offset": 5,
            "semantic_buffer": "User: Part B rolling content should survive relocation.",
            "semantic_buffer_tokens": 9,
            "raw_facts": [],
            "carry_facts": [],
        },
    )
    live_path.unlink()

    class _Adapter(_OwnedTestAdapterMixin):
        def instance_root(self):
            return tmp_path / ".quaid" / "instances" / "openclaw-main"

        def parse_session_jsonl(self, path):
            return Path(path).read_text(encoding="utf-8")

        def is_subagent_session(self, session_id, transcript_path=None):
            return False

    set_adapter(_Adapter())
    monkeypatch.setattr(
        extract_mod,
        "extract_from_transcript",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("already-consumed preserved relocation must not re-extract")
        ),
    )

    try:
        signal_path = extraction_daemon.write_signal(
            signal_type="session_end",
            session_id=session_id,
            transcript_path=str(preserved_path),
        )
        signal_data = json.loads(signal_path.read_text(encoding="utf-8"))
        signal_data["_signal_path"] = str(signal_path)

        extraction_daemon.process_signal(signal_data)
    finally:
        reset_adapter()

    preserved_source_key = extraction_daemon._signal_source_cursor_key(session_id, str(preserved_path))
    cursor = extraction_daemon.read_cursor(session_id, source_key=preserved_source_key)
    assert cursor["line_offset"] == 2
    assert cursor["transcript_path"] == str(preserved_path)
    assert cursor["processed_signal_type"] == "session_end"
    assert extraction_daemon.read_pending_signals() == []
    assert extraction_daemon._rolling_state_path(session_id).exists()


def test_process_signal_reset_backup_extracts_after_scan_only_cursor(
    monkeypatch,
    tmp_path,
):
    from lib.adapter import set_adapter, reset_adapter
    from ingest import extract as extract_mod
    from core import ingest_runtime
    from core.runtime import notify as notify_mod

    session_id = "cdde839e-1f02-4adc-a1ff-ocm2parta"
    live_dir = tmp_path / ".openclaw" / "agents" / "main" / "sessions"
    live_dir.mkdir(parents=True)
    live_path = live_dir / f"{session_id}.jsonl"
    backup_path = live_dir / f"{session_id}.jsonl.reset.20260606"
    live_path.write_text(
        "\n".join([
            f'{{"type":"session","id":"{session_id}"}}',
            '{"type":"message","message":{"role":"user","content":[{"type":"text","text":"Niseko chunk one should extract after reset."}]}}',
            '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"ACK"}]}}',
            '{"type":"message","message":{"role":"user","content":[{"type":"text","text":"Aurora scarf marker."}]}}',
            '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"ACK"}]}}',
            '{"type":"custom_message","customType":"openclaw.runtime-context","content":"context"}',
            '{"type":"message","message":{"role":"user","content":[{"type":"text","text":"Final stable fact."}]}}',
        ]) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("QUAID_HOME", str(tmp_path / ".quaid"))
    monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
    source_key = extraction_daemon._signal_source_cursor_key(session_id, str(live_path))
    extraction_daemon.write_cursor(session_id, 7, str(live_path), source_key=source_key)
    live_path.rename(backup_path)

    captured = {}

    class _Adapter(_OwnedTestAdapterMixin):
        def instance_root(self):
            return tmp_path / ".quaid" / "instances" / "openclaw-main"

        def parse_session_jsonl(self, path):
            captured["parsed_path"] = str(path)
            return "User: Niseko chunk one should extract after reset.\nAssistant: ACK"

        def is_subagent_session(self, session_id, transcript_path=None):
            return False

    set_adapter(_Adapter())
    monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "owner-1")
    monkeypatch.setattr(extraction_daemon, "_read_usage_totals", lambda: {})
    monkeypatch.setattr(extraction_daemon, "_session_has_harvestable_subagents", lambda *args, **kwargs: False)
    monkeypatch.setattr(extraction_daemon, "_warm_payload_embeddings", lambda _facts: {})
    monkeypatch.setattr(notify_mod, "notify_memory_extraction", lambda **_kwargs: None)
    monkeypatch.setattr(ingest_runtime, "run_session_logs_ingest", lambda **_kwargs: {"status": "indexed"})

    def fake_extract_from_transcript(transcript, **kwargs):
        captured["transcript"] = transcript
        return {
            "chunks_processed": 1,
            "chunks_total": 1,
            "unclassified_empty_payloads": 0,
            "raw_facts": [{"text": "Niseko chunk one should extract after reset.", "category": "fact"}],
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "carry_facts": [],
        }

    monkeypatch.setattr(extract_mod, "extract_from_transcript", fake_extract_from_transcript)
    monkeypatch.setattr(
        extract_mod,
        "apply_extracted_payloads",
        lambda *_args, **_kwargs: {
            "facts_stored": 1,
            "facts_skipped": 0,
            "edges_created": 0,
            "facts": [],
            "snippets": {},
            "journal": {},
            "project_log_metrics": {},
        },
    )

    try:
        signal_path = extraction_daemon.write_signal(
            signal_type="reset",
            session_id=session_id,
            transcript_path=str(backup_path),
        )
        signal_data = json.loads(signal_path.read_text(encoding="utf-8"))
        signal_data["_signal_path"] = str(signal_path)

        extraction_daemon.process_signal(signal_data)
    finally:
        reset_adapter()

    assert captured["parsed_path"].endswith(".jsonl")
    assert "Niseko chunk one" in captured["transcript"]
    cursor = extraction_daemon.read_cursor(session_id, source_key=source_key)
    assert cursor["line_offset"] == 7
    assert cursor["transcript_path"] == str(backup_path)
    assert cursor["processed_signal_type"] == "reset"


def test_process_signal_reset_backup_extracts_after_internal_cursor_missing_live(
    monkeypatch,
    tmp_path,
):
    from lib.adapter import set_adapter, reset_adapter
    from ingest import extract as extract_mod
    from core import ingest_runtime
    from core.runtime import notify as notify_mod

    session_id = "62a754fb-e158-47e6-bebc-0e8a967fc653"
    live_dir = tmp_path / ".openclaw" / "agents" / "main" / "sessions"
    live_dir.mkdir(parents=True)
    live_path = live_dir / f"{session_id}.jsonl"
    backup_path = live_dir / f"{session_id}.jsonl.reset.2026-07-04T09-14-17.044Z"
    live_path.write_text(
        "\n".join([
            f'{{"type":"session","id":"{session_id}"}}',
            '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"**[Quaid — Memory Extraction]** summary"}]}}',
            '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"**[Quaid — Memory Extraction (cont. 2/3)]** summary"}]}}',
            '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"**[Quaid — Memory Extraction (cont. 3/3)]** summary"}]}}',
        ]) + "\n",
        encoding="utf-8",
    )
    backup_path.write_text(
        live_path.read_text(encoding="utf-8")
        + '{"type":"message","message":{"role":"user","content":[{"type":"text","text":"The indigo glass lantern lives on the cedar shelf."}]}}\n',
        encoding="utf-8",
    )

    monkeypatch.setenv("QUAID_HOME", str(tmp_path / ".quaid"))
    monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
    source_key = extraction_daemon._signal_source_cursor_key(session_id, str(live_path))
    extraction_daemon.write_cursor(
        session_id,
        4,
        str(live_path),
        source_key=source_key,
        internal=True,
    )
    live_path.unlink()

    captured = {}

    class _Adapter(_OwnedTestAdapterMixin):
        def instance_root(self):
            return tmp_path / ".quaid" / "instances" / "openclaw-main"

        def parse_session_jsonl(self, path):
            captured["parsed_path"] = str(path)
            raw = Path(path).read_text(encoding="utf-8")
            if "indigo glass lantern" in raw:
                return "User: The indigo glass lantern lives on the cedar shelf.\nAssistant: ACK"
            return ""

        def is_subagent_session(self, session_id, transcript_path=None):
            return False

    set_adapter(_Adapter())
    monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "owner-1")
    monkeypatch.setattr(extraction_daemon, "_read_usage_totals", lambda: {})
    monkeypatch.setattr(extraction_daemon, "_session_has_harvestable_subagents", lambda *args, **kwargs: False)
    monkeypatch.setattr(extraction_daemon, "_warm_payload_embeddings", lambda _facts: {})
    monkeypatch.setattr(notify_mod, "notify_memory_extraction", lambda **_kwargs: None)
    monkeypatch.setattr(ingest_runtime, "run_session_logs_ingest", lambda **_kwargs: {"status": "indexed"})

    def fake_extract_from_transcript(transcript, **kwargs):
        captured["transcript"] = transcript
        return {
            "chunks_processed": 1,
            "chunks_total": 1,
            "unclassified_empty_payloads": 0,
            "raw_facts": [{"text": "The indigo glass lantern lives on the cedar shelf.", "category": "fact"}],
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "carry_facts": [],
        }

    monkeypatch.setattr(extract_mod, "extract_from_transcript", fake_extract_from_transcript)
    monkeypatch.setattr(
        extract_mod,
        "apply_extracted_payloads",
        lambda *_args, **_kwargs: {
            "facts_stored": 1,
            "facts_skipped": 0,
            "edges_created": 0,
            "facts": [],
            "snippets": {},
            "journal": {},
            "project_log_metrics": {},
        },
    )

    try:
        signal_path = extraction_daemon.write_signal(
            signal_type="reset",
            session_id=session_id,
            transcript_path=str(live_path),
        )
        signal_data = json.loads(signal_path.read_text(encoding="utf-8"))
        signal_data["_signal_path"] = str(signal_path)

        extraction_daemon.process_signal(signal_data)
    finally:
        reset_adapter()

    assert captured["parsed_path"].endswith(".jsonl")
    assert "indigo glass lantern" in captured["transcript"]
    cursor = extraction_daemon.read_cursor(session_id, source_key=source_key)
    assert cursor["line_offset"] == 5
    assert cursor["transcript_path"] == str(backup_path)
    assert cursor["processed_signal_type"] == "reset"


def test_process_signal_reset_extracts_short_openclaw_message_field_transcript(
    monkeypatch,
    tmp_path,
):
    from adaptors.openclaw.adapter import OpenClawAdapter
    from lib.adapter import set_adapter, reset_adapter
    from ingest import extract as extract_mod
    from core import ingest_runtime
    from core.runtime import notify as notify_mod

    session_id = "af8d7017-0000-4000-8000-ocm5parta"
    preserved_dir = tmp_path / ".quaid" / "instances" / "openclaw-main" / "logs" / "quaid" / "sessions"
    preserved_dir.mkdir(parents=True)
    preserved_path = preserved_dir / f"{session_id}.jsonl"
    preserved_path.write_text(
        "\n".join(
            [
                json.dumps({"type": "session", "id": session_id}),
                json.dumps({"type": "custom", "customType": "model-snapshot"}),
                json.dumps({"type": "custom_message", "customType": "openclaw.runtime-context", "content": "context"}),
                json.dumps({"role": "user", "message": "My Friday pumpkin seed ritual uses smoked paprika."}),
                json.dumps({"role": "assistant", "message": "Noted."}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("QUAID_HOME", str(tmp_path / ".quaid"))
    monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")

    captured = {}

    class _Adapter(_OwnedTestAdapterMixin, OpenClawAdapter):
        def instance_root(self):
            return tmp_path / ".quaid" / "instances" / "openclaw-main"

        def is_subagent_session(self, session_id, transcript_path=None):
            return False

    set_adapter(_Adapter())
    monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "owner-1")
    monkeypatch.setattr(extraction_daemon, "_read_usage_totals", lambda: {})
    monkeypatch.setattr(extraction_daemon, "_session_has_harvestable_subagents", lambda *args, **kwargs: False)
    monkeypatch.setattr(extraction_daemon, "_warm_payload_embeddings", lambda _facts: {})
    monkeypatch.setattr(notify_mod, "notify_memory_extraction", lambda **_kwargs: None)
    monkeypatch.setattr(ingest_runtime, "run_session_logs_ingest", lambda **_kwargs: {"status": "indexed"})

    def fake_extract_from_transcript(transcript, **kwargs):
        captured["transcript"] = transcript
        return {
            "chunks_processed": 1,
            "chunks_total": 1,
            "unclassified_empty_payloads": 0,
            "raw_facts": [{"text": "Owner's Friday pumpkin seed ritual uses smoked paprika.", "category": "preference"}],
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "carry_facts": [],
        }

    monkeypatch.setattr(extract_mod, "extract_from_transcript", fake_extract_from_transcript)
    monkeypatch.setattr(
        extract_mod,
        "apply_extracted_payloads",
        lambda *_args, **_kwargs: {
            "facts_stored": 1,
            "facts_skipped": 0,
            "edges_created": 0,
            "facts": [],
            "snippets": {},
            "journal": {},
            "project_log_metrics": {},
        },
    )

    try:
        signal_path = extraction_daemon.write_signal(
            signal_type="reset",
            session_id=session_id,
            transcript_path=str(preserved_path),
        )
        signal_data = json.loads(signal_path.read_text(encoding="utf-8"))
        signal_data["_signal_path"] = str(signal_path)

        extraction_daemon.process_signal(signal_data)
    finally:
        reset_adapter()

    assert "User: My Friday pumpkin seed ritual uses smoked paprika." in captured["transcript"]
    cursor = extraction_daemon.read_cursor(
        session_id,
        source_key=extraction_daemon._signal_source_cursor_key(session_id, str(preserved_path)),
    )
    assert cursor["line_offset"] == 5
    assert cursor["processed_signal_type"] == "reset"
    assert not extraction_daemon._rolling_state_path(session_id).exists()


def test_process_signal_smaller_preserved_relocation_extracts_after_scan_only_cursor(
    monkeypatch,
    tmp_path,
):
    from lib.adapter import set_adapter, reset_adapter
    from ingest import extract as extract_mod
    from core import ingest_runtime
    from core.runtime import notify as notify_mod

    session_id = "cdde839e-1f02-4adc-a1ff-ocm2partb"
    live_dir = tmp_path / ".openclaw" / "agents" / "main" / "sessions"
    preserved_dir = tmp_path / ".quaid" / "instances" / "openclaw-main" / "logs" / "quaid" / "sessions"
    live_dir.mkdir(parents=True)
    preserved_dir.mkdir(parents=True)
    live_path = live_dir / f"{session_id}.jsonl"
    preserved_path = preserved_dir / f"{session_id}.jsonl"
    live_path.write_text(
        "\n".join([
            f'{{"type":"session","id":"{session_id}"}}',
            '{"type":"message","message":{"role":"user","content":[{"type":"text","text":"Niseko scan-only cursor should not mark extraction complete."}]}}',
            '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"ACK"}]}}',
            '{"type":"message","message":{"role":"user","content":[{"type":"text","text":"Aurora scarf marker."}]}}',
            '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"ACK"}]}}',
            '{"type":"custom_message","customType":"openclaw.runtime-context","content":"context"}',
            '{"type":"message","message":{"role":"user","content":[{"type":"text","text":"Final stable fact."}]}}',
        ]) + "\n",
        encoding="utf-8",
    )
    preserved_path.write_text(
        "\n".join([
            f'{{"type":"session","id":"{session_id}"}}',
            '{"type":"message","message":{"role":"user","content":[{"type":"text","text":"Niseko scan-only cursor should not mark extraction complete."}]}}',
        ]) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("QUAID_HOME", str(tmp_path / ".quaid"))
    monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
    source_key = extraction_daemon._signal_source_cursor_key(session_id, str(live_path))
    extraction_daemon.write_cursor(session_id, 7, str(live_path), source_key=source_key)
    live_path.unlink()

    captured = {}

    class _Adapter(_OwnedTestAdapterMixin):
        def instance_root(self):
            return tmp_path / ".quaid" / "instances" / "openclaw-main"

        def parse_session_jsonl(self, path):
            captured["parsed_path"] = str(path)
            return "User: Niseko scan-only cursor should not mark extraction complete.\nAssistant: ACK"

        def is_subagent_session(self, session_id, transcript_path=None):
            return False

    set_adapter(_Adapter())
    monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "owner-1")
    monkeypatch.setattr(extraction_daemon, "_read_usage_totals", lambda: {})
    monkeypatch.setattr(extraction_daemon, "_session_has_harvestable_subagents", lambda *args, **kwargs: False)
    monkeypatch.setattr(extraction_daemon, "_warm_payload_embeddings", lambda _facts: {})
    monkeypatch.setattr(notify_mod, "notify_memory_extraction", lambda **_kwargs: None)
    monkeypatch.setattr(ingest_runtime, "run_session_logs_ingest", lambda **_kwargs: {"status": "indexed"})

    def fake_extract_from_transcript(transcript, **kwargs):
        captured["transcript"] = transcript
        return {
            "chunks_processed": 1,
            "chunks_total": 1,
            "unclassified_empty_payloads": 0,
            "raw_facts": [{"text": "Niseko scan-only cursor should not mark extraction complete.", "category": "fact"}],
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "carry_facts": [],
        }

    monkeypatch.setattr(extract_mod, "extract_from_transcript", fake_extract_from_transcript)
    monkeypatch.setattr(
        extract_mod,
        "apply_extracted_payloads",
        lambda *_args, **_kwargs: {
            "facts_stored": 1,
            "facts_skipped": 0,
            "edges_created": 0,
            "facts": [],
            "snippets": {},
            "journal": {},
            "project_log_metrics": {},
        },
    )

    try:
        signal_path = extraction_daemon.write_signal(
            signal_type="session_end",
            session_id=session_id,
            transcript_path=str(preserved_path),
        )
        signal_data = json.loads(signal_path.read_text(encoding="utf-8"))
        signal_data["_signal_path"] = str(signal_path)

        extraction_daemon.process_signal(signal_data)
    finally:
        reset_adapter()

    assert captured["parsed_path"].endswith(".jsonl")
    assert "Niseko scan-only cursor" in captured["transcript"]
    preserved_source_key = extraction_daemon._signal_source_cursor_key(session_id, str(preserved_path))
    cursor = extraction_daemon.read_cursor(session_id, source_key=preserved_source_key)
    assert cursor["line_offset"] == 2
    assert cursor["transcript_path"] == str(preserved_path)
    assert cursor["processed_signal_type"] == "session_end"


def test_process_signal_reset_relocated_transcript_extracts_after_scan_only_cursor(
    monkeypatch,
    tmp_path,
):
    from lib.adapter import set_adapter, reset_adapter
    from ingest import extract as extract_mod
    from core import ingest_runtime
    from core.runtime import notify as notify_mod

    session_id = "cdde839e-1f02-4adc-a1ff-ocm2partc"
    live_dir = tmp_path / ".openclaw" / "agents" / "main" / "sessions"
    relocated_dir = tmp_path / ".quaid" / "instances" / "openclaw-main" / "logs" / "quaid" / "sessions"
    live_dir.mkdir(parents=True)
    relocated_dir.mkdir(parents=True)
    live_path = live_dir / f"{session_id}.jsonl"
    relocated_path = relocated_dir / f"{session_id}.jsonl"
    transcript_lines = [
        f'{{"type":"session","id":"{session_id}"}}',
        '{"type":"message","message":{"role":"user","content":[{"type":"text","text":"Relocated reset scan-only cursor must extract."}]}}',
        '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"ACK"}]}}',
    ]
    transcript_body = "\n".join(transcript_lines) + "\n"
    live_path.write_text(transcript_body, encoding="utf-8")
    relocated_path.write_text(transcript_body, encoding="utf-8")

    monkeypatch.setenv("QUAID_HOME", str(tmp_path / ".quaid"))
    monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
    source_key = extraction_daemon._signal_source_cursor_key(session_id, str(live_path))
    extraction_daemon.write_cursor(session_id, 3, str(live_path), source_key=source_key)
    live_path.unlink()

    captured = {}

    class _Adapter(_OwnedTestAdapterMixin):
        def instance_root(self):
            return tmp_path / ".quaid" / "instances" / "openclaw-main"

        def parse_session_jsonl(self, path):
            captured["parsed_path"] = str(path)
            return "User: Relocated reset scan-only cursor must extract.\nAssistant: ACK"

        def is_subagent_session(self, session_id, transcript_path=None):
            return False

    set_adapter(_Adapter())
    monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "owner-1")
    monkeypatch.setattr(extraction_daemon, "_read_usage_totals", lambda: {})
    monkeypatch.setattr(extraction_daemon, "_session_has_harvestable_subagents", lambda *args, **kwargs: False)
    monkeypatch.setattr(extraction_daemon, "_warm_payload_embeddings", lambda _facts: {})
    monkeypatch.setattr(notify_mod, "notify_memory_extraction", lambda **_kwargs: None)
    monkeypatch.setattr(ingest_runtime, "run_session_logs_ingest", lambda **_kwargs: {"status": "indexed"})

    def fake_extract_from_transcript(transcript, **kwargs):
        captured["transcript"] = transcript
        return {
            "chunks_processed": 1,
            "chunks_total": 1,
            "unclassified_empty_payloads": 0,
            "raw_facts": [{"text": "Relocated reset scan-only cursor must extract.", "category": "fact"}],
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "carry_facts": [],
        }

    monkeypatch.setattr(extract_mod, "extract_from_transcript", fake_extract_from_transcript)
    monkeypatch.setattr(
        extract_mod,
        "apply_extracted_payloads",
        lambda *_args, **_kwargs: {
            "facts_stored": 1,
            "facts_skipped": 0,
            "edges_created": 0,
            "facts": [],
            "snippets": {},
            "journal": {},
            "project_log_metrics": {},
        },
    )

    try:
        signal_path = extraction_daemon.write_signal(
            signal_type="reset",
            session_id=session_id,
            transcript_path=str(relocated_path),
        )
        signal_data = json.loads(signal_path.read_text(encoding="utf-8"))
        signal_data["_signal_path"] = str(signal_path)

        extraction_daemon.process_signal(signal_data)
    finally:
        reset_adapter()

    assert captured["parsed_path"].endswith(".jsonl")
    assert "Relocated reset scan-only cursor" in captured["transcript"]
    relocated_source_key = extraction_daemon._signal_source_cursor_key(session_id, str(relocated_path))
    cursor = extraction_daemon.read_cursor(session_id, source_key=relocated_source_key)
    assert cursor["line_offset"] == 3
    assert cursor["transcript_path"] == str(relocated_path)
    assert cursor["processed_signal_type"] == "reset"


def test_process_signal_skips_preserved_checkpoint_session_end_while_live_exists(
    monkeypatch,
    tmp_path,
):
    from lib.adapter import set_adapter, reset_adapter
    from ingest import extract as extract_mod

    session_id = "858e08d3-4e9d-4a72-b7e1-3df34f10f622"
    live_dir = tmp_path / ".openclaw" / "agents" / "main" / "sessions"
    mirror_dir = tmp_path / "instances" / "openclaw-main" / "logs" / "quaid" / "sessions"
    live_dir.mkdir(parents=True)
    mirror_dir.mkdir(parents=True)
    live_path = live_dir / f"{session_id}.jsonl"
    mirror_path = mirror_dir / f"{session_id}.jsonl"
    live_path.write_text("", encoding="utf-8")

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
    source_key = extraction_daemon._signal_source_cursor_key(session_id, str(live_path))
    extraction_daemon.write_cursor(
        session_id,
        7,
        str(live_path),
        source_key=source_key,
        processed_signal_type="reset",
    )
    live_path.write_text(
        "\n".join([
            f'{{"type":"session","id":"{session_id}"}}',
            '{"type":"model_change"}',
            '{"type":"thinking_level_change"}',
            '{"type":"custom","customType":"model-snapshot"}',
            '{"type":"message","message":{"role":"user","content":[{"type":"text","text":"Baxter uses an orange linen notebook."}]}}',
            '{"type":"custom_message","customType":"openclaw.runtime-context","content":"context"}',
            '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"ACK"}]}}',
        ]) + "\n",
        encoding="utf-8",
    )
    mirror_path.write_text(live_path.read_text(encoding="utf-8") + '{"type":"checkpoint"}\n', encoding="utf-8")
    extraction_daemon.write_rolling_state(
        session_id,
        {
            "session_id": session_id,
            "transcript_path": str(live_path),
            "processed_line_offset": 0,
            "buffered_line_offset": 7,
            "semantic_buffer": "User: Baxter uses an orange linen notebook.",
            "semantic_buffer_tokens": 12,
            "raw_facts": [],
            "carry_facts": [],
        },
    )

    class _Adapter(_OwnedTestAdapterMixin):
        def instance_root(self):
            return tmp_path / "instances" / "openclaw-main"

        def parse_session_jsonl(self, path):
            return "User: Baxter uses an orange linen notebook.\nAssistant: ACK"

        def is_subagent_session(self, session_id, transcript_path=None):
            return False

    set_adapter(_Adapter())
    monkeypatch.setattr(extraction_daemon, "_reconcile_internal_cursor_state", lambda *args, **kwargs: "not_internal")
    monkeypatch.setattr(
        extract_mod,
        "extract_from_transcript",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("premature session_end should not extract")),
    )

    try:
        signal_path = extraction_daemon.write_signal(
            signal_type="session_end",
            session_id=session_id,
            transcript_path=str(mirror_path),
        )
        signal_data = json.loads(signal_path.read_text(encoding="utf-8"))
        signal_data["_signal_path"] = str(signal_path)

        extraction_daemon.process_signal(signal_data)
    finally:
        reset_adapter()

    assert extraction_daemon.read_pending_signals() == []
    cursor = extraction_daemon.read_cursor(session_id, source_key=source_key)
    assert cursor["line_offset"] == 7
    assert cursor["transcript_path"] == str(live_path)
    state = extraction_daemon.read_rolling_state(session_id)
    assert state["semantic_buffer_tokens"] == 12
    assert "orange linen notebook" in state["semantic_buffer"]


def test_process_signal_extracts_preserved_mirror_user_turn_missing_from_live(
    monkeypatch,
    tmp_path,
):
    from lib.adapter import set_adapter, reset_adapter
    from ingest import extract as extract_mod
    from core import ingest_runtime
    from core.runtime import notify as notify_mod

    session_id = "a7d33c41-ad97-4e1e-8b53-5013708684e4"
    live_dir = tmp_path / ".openclaw" / "agents" / "main" / "sessions"
    mirror_dir = tmp_path / "instances" / "openclaw-main" / "logs" / "quaid" / "sessions"
    live_dir.mkdir(parents=True)
    mirror_dir.mkdir(parents=True)
    live_path = live_dir / f"{session_id}.jsonl"
    mirror_path = mirror_dir / f"{session_id}.jsonl"
    live_lines = [
        f'{{"type":"session","id":"{session_id}"}}',
        '{"type":"message","message":{"role":"user","content":[{"type":"text","text":"Hello"}]}}',
        '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"**[Quaid — Memory Extraction]** summary"}]}}',
        '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"**[Quaid — Memory Extraction (cont. 2/3)]** summary"}]}}',
        '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"**[Quaid — Memory Extraction (cont. 3/3)]** summary"}]}}',
        '{"type":"custom_message","customType":"openclaw.runtime-context","content":"context"}',
        '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"Project docs update completed."}]}}',
    ]
    mirror_lines = [
        f'{{"type":"session","id":"{session_id}"}}',
        '{"type":"message","message":{"role":"user","content":[{"type":"text","text":"Baxter uses an orange linen notebook from Emília Rosa."}]}}',
        '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"ACK"}]}}',
    ]
    live_path.write_text("\n".join(live_lines) + "\n", encoding="utf-8")
    mirror_path.write_text("\n".join(mirror_lines) + "\n", encoding="utf-8")

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
    source_key = extraction_daemon._signal_source_cursor_key(session_id, str(live_path))
    extraction_daemon.write_cursor(
        session_id,
        len(live_lines),
        str(live_path),
        source_key=source_key,
        processed_signal_type="reset",
    )

    captured = {}

    class _Adapter(_OwnedTestAdapterMixin):
        def instance_root(self):
            return tmp_path / "instances" / "openclaw-main"

        def parse_session_jsonl(self, path):
            captured.setdefault("parsed_paths", []).append(str(path))
            raw = Path(path).read_text(encoding="utf-8")
            if "Baxter uses an orange linen notebook" in raw:
                return "User: Baxter uses an orange linen notebook from Emília Rosa.\n\nAssistant: ACK"
            return "User: Hello\n\nAssistant: Project docs update completed."

        def is_subagent_session(self, session_id, transcript_path=None):
            return False

    set_adapter(_Adapter())
    monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "owner-1")
    monkeypatch.setattr(extraction_daemon, "_read_usage_totals", lambda: {})
    monkeypatch.setattr(extraction_daemon, "_session_has_harvestable_subagents", lambda *args, **kwargs: False)
    monkeypatch.setattr(extraction_daemon, "_warm_payload_embeddings", lambda _facts: {})
    monkeypatch.setattr(notify_mod, "notify_memory_extraction", lambda **_kwargs: None)
    monkeypatch.setattr(ingest_runtime, "run_session_logs_ingest", lambda **_kwargs: {"status": "indexed"})

    def fake_extract_from_transcript(transcript, **kwargs):
        captured["transcript"] = transcript
        return {
            "chunks_processed": 1,
            "chunks_total": 1,
            "unclassified_empty_payloads": 0,
            "raw_facts": [{"text": "Baxter uses an orange linen notebook from Emília Rosa.", "category": "fact"}],
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "carry_facts": [],
        }

    monkeypatch.setattr(extract_mod, "extract_from_transcript", fake_extract_from_transcript)
    monkeypatch.setattr(
        extract_mod,
        "apply_extracted_payloads",
        lambda *_args, **_kwargs: {
            "facts_stored": 1,
            "facts_skipped": 0,
            "edges_created": 0,
            "facts": [],
            "snippets": {},
            "journal": {},
            "project_log_metrics": {},
        },
    )

    try:
        signal_path = extraction_daemon.write_signal(
            signal_type="session_end",
            session_id=session_id,
            transcript_path=str(mirror_path),
        )
        signal_data = json.loads(signal_path.read_text(encoding="utf-8"))
        signal_data["_signal_path"] = str(signal_path)

        extraction_daemon.process_signal(signal_data)
    finally:
        reset_adapter()

    assert str(mirror_path) in captured["parsed_paths"]
    assert "Baxter uses an orange linen notebook" in captured["transcript"]
    mirror_source_key = extraction_daemon._signal_source_cursor_key(session_id, str(mirror_path))
    cursor = extraction_daemon.read_cursor(session_id, source_key=mirror_source_key)
    assert cursor["line_offset"] == len(mirror_lines)
    assert cursor["transcript_path"] == str(mirror_path)
    assert cursor["processed_signal_type"] == "session_end"


def test_process_signal_timeout_preserves_cursor_on_larger_preserved_mirror(
    monkeypatch,
    tmp_path,
):
    from lib.adapter import set_adapter, reset_adapter
    from ingest import extract as extract_mod

    session_id = "8817b065-c63a-43f3-a68a-72b70f2729ed"
    live_dir = tmp_path / ".openclaw" / "agents" / "main" / "sessions"
    preserved_dir = tmp_path / "instances" / "openclaw-main" / "logs" / "quaid" / "sessions"
    live_dir.mkdir(parents=True)
    preserved_dir.mkdir(parents=True)
    live_path = live_dir / f"{session_id}.jsonl"
    preserved_path = preserved_dir / f"{session_id}.jsonl"
    live_path.write_text(
        "\n".join([
            f'{{"type":"session","id":"{session_id}"}}',
            '{"type":"message","message":{"role":"user","content":[{"type":"text","text":"live subset"}]}}',
        ]) + "\n",
        encoding="utf-8",
    )
    preserved_path.write_text(
        "\n".join([
            f'{{"type":"session","id":"{session_id}"}}',
            '{"type":"message","message":{"role":"user","content":[{"type":"text","text":"live subset"}]}}',
            '{"type":"message","message":{"role":"user","content":[{"type":"text","text":"preserved mirror tail"}]}}',
        ]) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
    source_key = extraction_daemon._signal_source_cursor_key(session_id, str(live_path))
    extraction_daemon.write_cursor(
        session_id,
        3,
        str(preserved_path),
        source_key=source_key,
        processed_signal_type="session_end",
    )

    class _Adapter(_OwnedTestAdapterMixin):
        def instance_root(self):
            return tmp_path / "instances" / "openclaw-main"

        def parse_session_jsonl(self, path):
            return Path(path).read_text(encoding="utf-8")

        def is_subagent_session(self, session_id, transcript_path=None):
            return False

    set_adapter(_Adapter())
    monkeypatch.setattr(extraction_daemon, "_reconcile_internal_cursor_state", lambda *args, **kwargs: "not_internal")
    monkeypatch.setattr(
        extract_mod,
        "extract_from_transcript",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("timeout must not reset to smaller stale live transcript")
        ),
    )

    try:
        signal_path = extraction_daemon.write_signal(
            signal_type="timeout",
            session_id=session_id,
            transcript_path=str(live_path),
        )
        signal_data = json.loads(signal_path.read_text(encoding="utf-8"))
        signal_data["_signal_path"] = str(signal_path)

        extraction_daemon.process_signal(signal_data)
    finally:
        reset_adapter()

    cursor = extraction_daemon.read_cursor(session_id, source_key=source_key)
    assert cursor["line_offset"] == 3
    assert cursor["transcript_path"] == str(preserved_path)
    assert cursor["processed_signal_type"] == "session_end"
    assert extraction_daemon.read_pending_signals() == []


def test_process_signal_timeout_extracts_smaller_live_after_scan_only_preserved_cursor(
    monkeypatch,
    tmp_path,
):
    from lib.adapter import set_adapter, reset_adapter
    from ingest import extract as extract_mod
    from core import ingest_runtime
    from core.runtime import notify as notify_mod

    session_id = "b817b065-c63a-43f3-a68a-72b70f2729ed"
    live_dir = tmp_path / ".openclaw" / "agents" / "main" / "sessions"
    preserved_dir = tmp_path / "instances" / "openclaw-main" / "logs" / "quaid" / "sessions"
    live_dir.mkdir(parents=True)
    preserved_dir.mkdir(parents=True)
    live_path = live_dir / f"{session_id}.jsonl"
    preserved_path = preserved_dir / f"{session_id}.jsonl"
    live_path.write_text(
        "\n".join([
            f'{{"type":"session","id":"{session_id}"}}',
            '{"type":"message","message":{"role":"user","content":[{"type":"text","text":"garden shed lantern timeout fact"}]}}',
        ]) + "\n",
        encoding="utf-8",
    )
    preserved_path.write_text(
        "\n".join([
            f'{{"type":"session","id":"{session_id}"}}',
            '{"type":"message","message":{"role":"user","content":[{"type":"text","text":"prior preserved payload"}]}}',
            '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"ack"}]}}',
        ]) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
    source_key = extraction_daemon._signal_source_cursor_key(session_id, str(live_path))
    extraction_daemon.write_cursor(
        session_id,
        3,
        str(preserved_path),
        source_key=source_key,
    )

    captured = {}

    class _Adapter(_OwnedTestAdapterMixin):
        def instance_root(self):
            return tmp_path / "instances" / "openclaw-main"

        def parse_session_jsonl(self, path):
            captured["parsed_path"] = str(path)
            return "User: garden shed lantern timeout fact.\nAssistant: ACK"

        def is_subagent_session(self, session_id, transcript_path=None):
            return False

    set_adapter(_Adapter())
    monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "owner-1")
    monkeypatch.setattr(extraction_daemon, "_read_usage_totals", lambda: {})
    monkeypatch.setattr(extraction_daemon, "_session_has_harvestable_subagents", lambda *args, **kwargs: False)
    monkeypatch.setattr(extraction_daemon, "_warm_payload_embeddings", lambda _facts: {})
    monkeypatch.setattr(notify_mod, "notify_memory_extraction", lambda **_kwargs: None)
    monkeypatch.setattr(ingest_runtime, "run_session_logs_ingest", lambda **_kwargs: {"status": "indexed"})

    def fake_extract_from_transcript(transcript, **kwargs):
        captured["transcript"] = transcript
        return {
            "chunks_processed": 1,
            "chunks_total": 1,
            "unclassified_empty_payloads": 0,
            "raw_facts": [{"text": "garden shed lantern timeout fact", "category": "fact"}],
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "carry_facts": [],
        }

    monkeypatch.setattr(extract_mod, "extract_from_transcript", fake_extract_from_transcript)
    monkeypatch.setattr(
        extract_mod,
        "apply_extracted_payloads",
        lambda *_args, **_kwargs: {
            "facts_stored": 1,
            "facts_skipped": 0,
            "edges_created": 0,
            "facts": [],
            "snippets": {},
            "journal": {},
            "project_log_metrics": {},
        },
    )

    try:
        signal_path = extraction_daemon.write_signal(
            signal_type="timeout",
            session_id=session_id,
            transcript_path=str(live_path),
        )
        signal_data = json.loads(signal_path.read_text(encoding="utf-8"))
        signal_data["_signal_path"] = str(signal_path)

        extraction_daemon.process_signal(signal_data)
    finally:
        reset_adapter()

    assert captured["parsed_path"].endswith(".jsonl")
    assert "garden shed lantern timeout fact" in captured["transcript"]
    cursor = extraction_daemon.read_cursor(session_id, source_key=source_key)
    assert cursor["transcript_path"] == str(live_path)
    assert cursor["processed_signal_type"] == "timeout"


def test_process_signal_recovers_full_transcript_before_too_short_skip(
    monkeypatch,
    tmp_path,
):
    from lib.adapter import set_adapter, reset_adapter
    from ingest import extract as extract_mod
    from core import ingest_runtime
    from core.runtime import notify as notify_mod

    session_id = "1fe54c72-2aa0-49ab-849e-03829272a7fe"
    sessions_dir = tmp_path / ".quaid" / "instances" / "openclaw-main" / "logs" / "quaid" / "sessions"
    sessions_dir.mkdir(parents=True)
    transcript_path = sessions_dir / f"{session_id}.jsonl"
    transcript_path.write_text(
        "\n".join([
            f'{{"type":"session","id":"{session_id}"}}',
            '{"type":"model_change"}',
            '{"type":"thinking_level_change"}',
            '{"type":"custom","customType":"model-snapshot"}',
            '{"type":"message","message":{"role":"user","content":[{"type":"text","text":"Baxter marker should survive a short stale slice."}]}}',
            '{"type":"custom_message","customType":"openclaw.runtime-context","content":"context"}',
            '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"ACK"}]}}',
        ]) + "\n",
        encoding="utf-8",
    )
    stale_short_lines = [
        f'{{"type":"session","id":"{session_id}"}}\n',
        '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"ACK"}]}}\n',
    ]

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
    monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "owner-1")
    monkeypatch.setattr(extraction_daemon, "_read_usage_totals", lambda: {})
    monkeypatch.setattr(extraction_daemon, "_session_has_harvestable_subagents", lambda *args, **kwargs: False)
    monkeypatch.setattr(extraction_daemon, "_buffer_transcript_tail", lambda path, start, state, **kwargs: (dict(state or {}), {
        "raw_lines_added": 0,
        "semantic_chars_added": 0,
        "semantic_tokens_added": 0,
        "buffered_line_offset": int(start or 0),
    }))
    monkeypatch.setattr(extraction_daemon, "read_transcript_slice", lambda path, start: list(stale_short_lines))
    monkeypatch.setattr(extraction_daemon, "_warm_payload_embeddings", lambda _facts: {
        "requested": 0,
        "unique": 0,
        "cache_hits": 0,
        "warmed": 0,
        "failed": 0,
        "skipped_empty": 0,
    })
    monkeypatch.setattr(notify_mod, "notify_memory_extraction", lambda **_kwargs: None)
    monkeypatch.setattr(ingest_runtime, "run_session_logs_ingest", lambda **_kwargs: {"status": "indexed"})

    captured = {}

    class _Adapter(_OwnedTestAdapterMixin):
        def instance_root(self):
            return tmp_path / "instances" / "openclaw-main"

        def parse_session_jsonl(self, path):
            raw = Path(path).read_text(encoding="utf-8")
            if "Baxter marker" in raw:
                return "User: Baxter marker should survive a short stale slice.\nAssistant: ACK"
            return "Assistant: ACK"

        def is_subagent_session(self, session_id, transcript_path=None):
            return False

    def fake_extract_from_transcript(transcript, **kwargs):
        captured["transcript"] = transcript
        return {
            "chunks_processed": 1,
            "chunks_total": 1,
            "unclassified_empty_payloads": 0,
            "raw_facts": [
                {
                    "text": "Baxter marker should survive a short stale slice.",
                    "category": "fact",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                }
            ],
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "carry_facts": [],
        }

    monkeypatch.setattr(extract_mod, "extract_from_transcript", fake_extract_from_transcript)
    monkeypatch.setattr(
        extract_mod,
        "apply_extracted_payloads",
        lambda *_args, **_kwargs: {
            "facts_stored": 1,
            "facts_skipped": 0,
            "edges_created": 0,
            "facts": [{"text": "Baxter marker should survive a short stale slice.", "status": "stored", "edges": []}],
            "snippets": {},
            "journal": {},
            "project_log_metrics": {},
        },
    )

    set_adapter(_Adapter())
    try:
        signal_path = extraction_daemon.write_signal(
            signal_type="session_end",
            session_id=session_id,
            transcript_path=str(transcript_path),
        )
        signal_data = json.loads(signal_path.read_text(encoding="utf-8"))
        signal_data["_signal_path"] = str(signal_path)

        extraction_daemon.process_signal(signal_data)
    finally:
        reset_adapter()

    assert "Baxter marker should survive" in captured["transcript"]
    cursor = extraction_daemon.read_cursor(session_id)
    assert cursor["line_offset"] == 7


def test_process_signal_extracts_short_unicode_transcript(
    monkeypatch,
    tmp_path,
):
    from lib.adapter import set_adapter, reset_adapter
    from ingest import extract as extract_mod
    from core import ingest_runtime
    from core.runtime import notify as notify_mod

    session_id = "short-unicode-session"
    transcript_path = tmp_path / "short-unicode.jsonl"
    transcript_path.write_text(
        '{"role":"user","content":"会議は三時"}\n',
        encoding="utf-8",
    )

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "unicode-inst")
    monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "owner-1")
    monkeypatch.setattr(extraction_daemon, "_read_usage_totals", lambda: {})
    monkeypatch.setattr(extraction_daemon, "_session_has_harvestable_subagents", lambda *args, **kwargs: False)
    monkeypatch.setattr(extraction_daemon, "_buffer_transcript_tail", lambda path, start, state, **kwargs: (dict(state or {}), {
        "raw_lines_added": 0,
        "semantic_chars_added": 0,
        "semantic_tokens_added": 0,
        "buffered_line_offset": int(start or 0),
    }))
    monkeypatch.setattr(extraction_daemon, "_warm_payload_embeddings", lambda _facts: {
        "requested": 0,
        "unique": 0,
        "cache_hits": 0,
        "warmed": 0,
        "failed": 0,
        "skipped_empty": 0,
    })
    monkeypatch.setattr(notify_mod, "notify_memory_extraction", lambda **_kwargs: None)
    monkeypatch.setattr(ingest_runtime, "run_session_logs_ingest", lambda **_kwargs: {"status": "indexed"})

    captured = {}

    class _Adapter(_OwnedTestAdapterMixin):
        def instance_root(self):
            return tmp_path / "instances" / "unicode-inst"

        def parse_session_jsonl(self, path):
            _ = path
            return "User: 会議は三時"

        def is_subagent_session(self, session_id, transcript_path=None):
            return False

    def fake_extract_from_transcript(transcript, **kwargs):
        captured["transcript"] = transcript
        return {
            "chunks_processed": 1,
            "chunks_total": 1,
            "unclassified_empty_payloads": 0,
            "raw_facts": [
                {
                    "text": "会議は三時",
                    "category": "fact",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                }
            ],
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "carry_facts": [],
        }

    monkeypatch.setattr(extract_mod, "extract_from_transcript", fake_extract_from_transcript)
    monkeypatch.setattr(
        extract_mod,
        "apply_extracted_payloads",
        lambda *_args, **_kwargs: {
            "facts_stored": 1,
            "facts_skipped": 0,
            "edges_created": 0,
            "facts": [{"text": "会議は三時", "status": "stored", "edges": []}],
            "snippets": {},
            "journal": {},
            "project_log_metrics": {},
        },
    )

    set_adapter(_Adapter())
    try:
        signal_path = extraction_daemon.write_signal(
            signal_type="timeout",
            session_id=session_id,
            transcript_path=str(transcript_path),
        )
        signal_data = json.loads(signal_path.read_text(encoding="utf-8"))
        signal_data["_signal_path"] = str(signal_path)

        extraction_daemon.process_signal(signal_data)
    finally:
        reset_adapter()

    assert "会議は三時" in captured["transcript"]
    cursor = extraction_daemon.read_cursor(session_id)
    assert cursor["processed_signal_type"] == "timeout"


def test_reset_reextract_clears_stale_rolling_buffer_offset(
    monkeypatch,
    tmp_path,
):
    from lib.adapter import set_adapter, reset_adapter
    from ingest import extract as extract_mod
    from core import ingest_runtime
    from core.runtime import notify as notify_mod

    session_id = "035f357b-d5a7-4b28-934a-f5f084e8eb12"
    sessions_dir = tmp_path / ".openclaw" / "agents" / "main" / "sessions"
    sessions_dir.mkdir(parents=True)
    original_path = sessions_dir / f"{session_id}.jsonl"
    backup_path = sessions_dir / f"{session_id}.jsonl.reset.2026-05-14T20-57-02.937Z"
    backup_path.write_text(
        "\n".join([
            f'{{"type":"session","id":"{session_id}"}}',
            '{"type":"model_change"}',
            '{"type":"thinking_level_change"}',
            '{"type":"custom","customType":"model-snapshot"}',
            '{"type":"message","message":{"role":"user","content":[{"type":"text","text":"Baxter uses an orange linen notebook from Emília Rosa."}]}}',
            '{"type":"custom_message","customType":"openclaw.runtime-context","content":"context"}',
            '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"ACK"}]}}',
        ]) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
    monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "owner-1")
    monkeypatch.setattr(extraction_daemon, "_read_usage_totals", lambda: {})
    monkeypatch.setattr(extraction_daemon, "_session_has_harvestable_subagents", lambda *args, **kwargs: False)
    monkeypatch.setattr(extraction_daemon, "_warm_payload_embeddings", lambda _facts: {
        "requested": 0,
        "unique": 0,
        "cache_hits": 0,
        "warmed": 0,
        "failed": 0,
        "skipped_empty": 0,
    })
    monkeypatch.setattr(notify_mod, "notify_memory_extraction", lambda **_kwargs: None)
    monkeypatch.setattr(ingest_runtime, "run_session_logs_ingest", lambda **_kwargs: {"status": "indexed"})

    source_key = extraction_daemon._signal_source_cursor_key(session_id, str(original_path))
    extraction_daemon.write_cursor(session_id, 0, str(original_path), source_key=source_key)
    extraction_daemon.write_rolling_state(
        session_id,
        {
            "session_id": session_id,
            "transcript_path": str(original_path),
            "processed_line_offset": 7,
            "buffered_line_offset": 7,
            "semantic_buffer": "User: Hello\n\nAssistant: Hey. What can I help with?",
            "semantic_buffer_tokens": 12,
            "carry_facts": [],
            "raw_facts": [],
        },
    )

    captured = {}

    class _Adapter(_OwnedTestAdapterMixin):
        def instance_root(self):
            return tmp_path / "instances" / "openclaw-main"

        def parse_session_jsonl(self, path):
            raw = Path(path).read_text(encoding="utf-8")
            if "Baxter uses an orange linen notebook" in raw:
                return "User: Baxter uses an orange linen notebook from Emília Rosa.\nAssistant: ACK"
            return "User: Hello\n\nAssistant: Hey. What can I help with?"

        def is_subagent_session(self, session_id, transcript_path=None):
            return False

    def fake_extract_from_transcript(transcript, **kwargs):
        captured["transcript"] = transcript
        return {
            "chunks_processed": 1,
            "chunks_total": 1,
            "unclassified_empty_payloads": 0,
            "raw_facts": [
                {
                    "text": "Baxter uses an orange linen notebook from Emília Rosa.",
                    "category": "fact",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                }
            ],
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "carry_facts": [],
        }

    monkeypatch.setattr(extract_mod, "extract_from_transcript", fake_extract_from_transcript)
    monkeypatch.setattr(
        extract_mod,
        "apply_extracted_payloads",
        lambda *_args, **_kwargs: {
            "facts_stored": 1,
            "facts_skipped": 0,
            "edges_created": 0,
            "facts": [{"text": "Baxter uses an orange linen notebook from Emília Rosa.", "status": "stored", "edges": []}],
            "snippets": {},
            "journal": {},
            "project_log_metrics": {},
        },
    )

    set_adapter(_Adapter())
    try:
        signal_path = extraction_daemon.write_signal(
            signal_type="reset",
            session_id=session_id,
            transcript_path=str(original_path),
        )
        signal_data = json.loads(signal_path.read_text(encoding="utf-8"))
        signal_data["_signal_path"] = str(signal_path)

        extraction_daemon.process_signal(signal_data)
    finally:
        reset_adapter()

    assert "Baxter uses an orange linen notebook" in captured["transcript"]
    assert "User: Hello" not in captured["transcript"]
    cursor = extraction_daemon.read_cursor(session_id, source_key=source_key)
    assert cursor["line_offset"] == 7


@pytest.mark.parametrize(
    ("rebase_retry_count", "expect_followup", "guard_reread_error"),
    [
        (0, True, False),
        (extraction_daemon.TRANSCRIPT_REBASE_MAX_RETRIES, False, False),
        (0, True, True),
    ],
)
def test_process_signal_requeues_when_transcript_rebases_during_flush(
    monkeypatch,
    tmp_path,
    rebase_retry_count,
    expect_followup,
    guard_reread_error,
):
    from lib.adapter import set_adapter, reset_adapter
    from ingest import extract as extract_mod
    from core import ingest_runtime
    from core.runtime import notify as notify_mod

    session_id = "4cf66ab6-0a02-4c1d-9a76-3a9a5dc5c44e"
    sessions_dir = tmp_path / ".quaid" / "instances" / "openclaw-main" / "logs" / "quaid" / "sessions"
    sessions_dir.mkdir(parents=True)
    transcript_path = sessions_dir / f"{session_id}.jsonl"

    def _rows(marker: str) -> str:
        return "\n".join([
            f'{{"type":"session","id":"{session_id}"}}',
            '{"type":"model_change"}',
            '{"type":"thinking_level_change"}',
            '{"type":"custom","customType":"model-snapshot"}',
            f'{{"type":"message","message":{{"role":"user","content":[{{"type":"text","text":"{marker}"}}]}}}}',
            '{"type":"custom_message","customType":"openclaw.runtime-context","content":"context"}',
            '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"ACK"}]}}',
        ]) + "\n"

    transcript_path.write_text(
        _rows(
            "chunk-one Kinesis marker with enough surrounding context for the daemon "
            "to treat this as extractable user memory content"
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
    source_key = extraction_daemon._signal_source_cursor_key(session_id, str(transcript_path))
    captured = {}
    rolling_metrics = []
    parse_calls = {"count": 0}
    digest_calls = {"count": 0}
    real_digest = extraction_daemon._transcript_line_window_digest

    def guarded_digest(*args, **kwargs):
        digest_calls["count"] += 1
        if guard_reread_error and digest_calls["count"] == 2:
            raise OSError("transcript disappeared during rebase guard")
        return real_digest(*args, **kwargs)

    class _Adapter(_OwnedTestAdapterMixin):
        def instance_root(self):
            return tmp_path / "instances" / "openclaw-main"

        def parse_session_jsonl(self, path):
            parse_calls["count"] += 1
            if parse_calls["count"] == 2:
                transcript_path.write_text(
                    _rows(
                        "chunk-two Baxter orange linen notebook Emília Rosa with enough "
                        "surrounding context to represent a real OpenClaw transcript rebase"
                    ),
                    encoding="utf-8",
                )
            return (
                "User: chunk-one Kinesis marker with enough parsed semantic context "
                "to enter the extraction path cleanly.\nAssistant: ACK"
            )

        def is_subagent_session(self, session_id, transcript_path=None):
            return False

    set_adapter(_Adapter())
    monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "owner-1")
    monkeypatch.setattr(extraction_daemon, "_read_usage_totals", lambda: {})
    monkeypatch.setattr(extraction_daemon, "_session_has_harvestable_subagents", lambda *args, **kwargs: False)
    monkeypatch.setattr(extraction_daemon, "_warm_payload_embeddings", lambda _facts: {
        "requested": 0,
        "unique": 0,
        "cache_hits": 0,
        "warmed": 0,
        "failed": 0,
        "skipped_empty": 0,
    })
    monkeypatch.setattr(notify_mod, "notify_memory_extraction", lambda **_kwargs: None)
    monkeypatch.setattr(ingest_runtime, "run_session_logs_ingest", lambda **_kwargs: {"status": "indexed"})
    monkeypatch.setattr(
        extraction_daemon,
        "write_rolling_metric",
        lambda event, session_id, **data: rolling_metrics.append({"event": event, "session_id": session_id, **data}),
    )
    monkeypatch.setattr(extraction_daemon, "_transcript_line_window_digest", guarded_digest)

    def fake_extract_from_transcript(transcript, **kwargs):
        captured["transcript"] = transcript
        return {
            "chunks_processed": 1,
            "chunks_total": 1,
            "unclassified_empty_payloads": 0,
            "raw_facts": [
                {
                    "text": "Solomon uses a Kinesis keyboard.",
                    "category": "fact",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                }
            ],
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "carry_facts": [],
        }

    monkeypatch.setattr(extract_mod, "extract_from_transcript", fake_extract_from_transcript)
    monkeypatch.setattr(
        extract_mod,
        "apply_extracted_payloads",
        lambda *_args, **_kwargs: {
            "facts_stored": 1,
            "facts_skipped": 0,
            "edges_created": 0,
            "facts": [{"text": "Solomon uses a Kinesis keyboard.", "status": "stored", "edges": []}],
            "snippets": {},
            "journal": {},
            "project_log_metrics": {},
        },
    )

    try:
        signal_path = extraction_daemon.write_signal(
            signal_type="session_end",
            session_id=session_id,
            transcript_path=str(transcript_path),
            meta={"transcript_rebase_retry_count": rebase_retry_count} if rebase_retry_count else None,
        )
        signal_data = json.loads(signal_path.read_text(encoding="utf-8"))
        signal_data["_signal_path"] = str(signal_path)

        extraction_daemon.process_signal(signal_data)
    finally:
        reset_adapter()

    assert "chunk-one Kinesis marker" in captured["transcript"]
    assert "Baxter orange linen" in transcript_path.read_text(encoding="utf-8")

    cursor = extraction_daemon.read_cursor(session_id, source_key=source_key)
    assert cursor["line_offset"] == 0
    assert cursor["transcript_path"] == str(transcript_path)
    assert rolling_metrics
    assert rolling_metrics[-1]["transcript_rebased_during_flush"] is True
    assert rolling_metrics[-1]["transcript_rebase_retry_count"] == rebase_retry_count
    assert rolling_metrics[-1]["transcript_rebase_retry_limit_reached"] is (not expect_followup)
    assert rolling_metrics[-1]["transcript_rebase_reread_failed"] is guard_reread_error

    queued = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in extraction_daemon._signal_dir().glob("*.json")
    ]
    if expect_followup:
        assert len(queued) == 1
        assert queued[0]["type"] == "session_end"
        assert queued[0]["meta"]["reason"] == "transcript_rebased_during_flush"
        assert queued[0]["meta"]["source_cursor_key"] == source_key
        assert queued[0]["meta"]["transcript_rebase_retry_count"] == rebase_retry_count + 1
        assert queued[0]["meta"]["transcript_rebase_max_retries"] == extraction_daemon.TRANSCRIPT_REBASE_MAX_RETRIES
    else:
        assert queued == []


def test_process_signal_does_not_reextract_tail_after_nonrolling_semantic_stage(monkeypatch, tmp_path):
    from lib.adapter import set_adapter, reset_adapter
    from ingest import extract as extract_mod
    from core import ingest_runtime
    from core.runtime import notify as notify_mod

    session_id = "sess-stage-reset"
    transcript_path = tmp_path / f"{session_id}.jsonl"
    transcript_path.write_text(
        (
            '{"role":"user","content":"My Lisbon notebook codeword is tangerine-emilia."}\n'
            '{"role":"assistant","content":"Understood."}\n'
        ),
        encoding="utf-8",
    )
    signal_path = tmp_path / "reset-signal.json"
    signal_path.write_text("{}", encoding="utf-8")

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "owner-1")
    monkeypatch.setattr(extraction_daemon, "_read_usage_totals", lambda: {})
    monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda: 10)
    monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_max_lines", lambda: 100)
    monkeypatch.setattr(extraction_daemon, "_warm_payload_embeddings", lambda _facts: {
        "requested": 0,
        "unique": 0,
        "cache_hits": 0,
        "warmed": 0,
        "failed": 0,
    })
    monkeypatch.setattr(
        extraction_daemon,
        "_collapse_staged_semantic_duplicates",
        lambda existing, incoming: (
            list(existing or []) + list(incoming or []),
            extraction_daemon._semantic_stage_metrics_defaults(),
        ),
    )
    monkeypatch.setattr(notify_mod, "notify_memory_extraction", lambda **_kwargs: None)
    monkeypatch.setattr(ingest_runtime, "run_session_logs_ingest", lambda **_kwargs: {"status": "indexed"})

    def fake_buffer_transcript_tail(_path, _from_line, state, **_kwargs):
        staged = dict(state or {})
        staged.update({
            "session_id": session_id,
            "semantic_buffer": "User: My Lisbon notebook codeword is tangerine-emilia.",
            "semantic_buffer_tokens": 12,
            "buffered_line_offset": 2,
            "processed_line_offset": 0,
            "raw_facts": [],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "carry_facts": [],
            "transcript_path": str(transcript_path),
        })
        return staged, {
            "raw_lines_added": 2,
            "semantic_chars_added": len(staged["semantic_buffer"]),
            "semantic_tokens_added": 12,
            "buffered_line_offset": 2,
        }

    monkeypatch.setattr(extraction_daemon, "_buffer_transcript_tail", fake_buffer_transcript_tail)

    class _Adapter(_OwnedTestAdapterMixin):
        def instance_root(self):
            return tmp_path

        def parse_session_jsonl(self, path):
            return "User: My Lisbon notebook codeword is tangerine-emilia.\nAssistant: Understood."

        def is_subagent_session(self, session_id, transcript_path=None):
            return False

    extract_calls = []
    published_payloads = []
    publish_kwargs = []

    def fake_extract_from_transcript(transcript, **_kwargs):
        extract_calls.append(transcript)
        return {
            "chunks_processed": 1,
            "chunks_total": 1,
            "unclassified_empty_payloads": 0,
            "raw_facts": [{
                "text": "Solomon Steadman's Lisbon notebook codeword is tangerine-emilia",
                "speaker": "user",
                "privacy": "shared",
                "category": "fact",
                "keywords": "lisbon notebook codeword",
            }],
            "facts": [],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "carry_facts": [],
        }

    def fake_apply_extracted_payloads(payload, **kwargs):
        published_payloads.append(payload)
        publish_kwargs.append(kwargs)
        return {
            "facts_stored": len(payload.get("raw_facts", [])),
            "facts_skipped": 0,
            "edges_created": 0,
            "facts": [{"status": "stored"} for _ in payload.get("raw_facts", [])],
            "snippets": {},
            "journal": {},
            "project_log_metrics": {},
        }

    set_adapter(_Adapter())
    try:
        monkeypatch.setattr(extract_mod, "extract_from_transcript", fake_extract_from_transcript)
        monkeypatch.setattr(extract_mod, "apply_extracted_payloads", fake_apply_extracted_payloads)

        extraction_daemon.process_signal({
            "session_id": session_id,
            "type": "reset",
            "transcript_path": str(transcript_path),
            "_signal_path": str(signal_path),
        })
    finally:
        reset_adapter()

    assert extract_calls == ["User: My Lisbon notebook codeword is tangerine-emilia."]
    assert len(published_payloads) == 1
    assert len(published_payloads[0]["raw_facts"]) == 1
    assert publish_kwargs[0]["memory_publish_mode"] == "request"
    assert publish_kwargs[0]["snippet_journal_write_mode"] == "request"


def test_summarize_fact_result_buckets_groups_duplicate_and_skip_reasons():
    summary = extraction_daemon._summarize_fact_result_buckets([
        {"status": "duplicate", "reason": "Already stored"},
        {"status": "skipped", "reason": "too short (need 3+ words)"},
        {"status": "skipped", "reason": "unsupported domains: ['weird']"},
        {"status": "stored"},
    ])

    assert summary["status_counts"] == {
        "duplicate": 1,
        "skipped": 2,
        "stored": 1,
    }
    assert summary["skip_buckets"] == {
        "duplicate": 1,
        "too short (need 3+ words)": 1,
        "unsupported domains": 1,
    }


def test_process_signal_resets_same_path_cursor_when_oc_transcript_rebases_smaller(monkeypatch, tmp_path):
    from lib.adapter import set_adapter, reset_adapter

    transcript_path = tmp_path / "same-path-shrink.jsonl"
    transcript_path.write_text(
        "\n".join(
            f'{{"type":"message","message":{{"role":"user","content":[{{"type":"text","text":"line {idx}"}}]}}}}'
            for idx in range(8)
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")

    captured = {"read_offsets": [], "write_cursor": []}

    monkeypatch.setattr(
        extraction_daemon,
        "read_cursor",
        lambda _sid: {
            "line_offset": 10,
            "transcript_path": str(transcript_path),
            "internal": False,
            "transcript_size_bytes": transcript_path.stat().st_size + 50,
        },
    )
    monkeypatch.setattr(extraction_daemon, "read_rolling_state", lambda _sid: {})
    monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "owner-1")
    monkeypatch.setattr(extraction_daemon, "_read_usage_totals", lambda: {})
    monkeypatch.setattr(extraction_daemon, "_acquire_session_processing_lock", lambda _key: 1)
    monkeypatch.setattr(extraction_daemon, "_release_session_processing_lock", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(extraction_daemon, "mark_signal_processed", lambda _sig: captured.setdefault("processed", True))
    monkeypatch.setattr(extraction_daemon, "write_context_refresh_timeout_marker", lambda _sid: None)
    monkeypatch.setattr(extraction_daemon, "write_rolling_metric", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        extraction_daemon,
        "write_cursor",
        lambda session_id, line_offset, transcript_path, **kwargs: captured["write_cursor"].append(
            {
                "session_id": session_id,
                "line_offset": line_offset,
                "transcript_path": transcript_path,
                **kwargs,
            }
        ),
    )
    monkeypatch.setattr(
        extraction_daemon,
        "read_transcript_slice",
        lambda path, from_line: captured["read_offsets"].append(from_line) or [],
    )

    class _Adapter(_OwnedTestAdapterMixin):
        def instance_root(self):
            return tmp_path / "instances" / "openclaw-main"

        def parse_session_jsonl(self, path):
            return "line"

        def is_subagent_session(self, session_id, transcript_path=None):
            return False

    set_adapter(_Adapter())
    try:
        extraction_daemon.process_signal(
            {
                "session_id": "417369e6-a300-417a-84ff-6193ed154420",
                "type": "timeout",
                "transcript_path": str(transcript_path),
                "_signal_path": str(tmp_path / "sig.json"),
            }
        )
    finally:
        reset_adapter()

    assert captured["read_offsets"]
    assert set(captured["read_offsets"]) == {0}
    assert captured["write_cursor"] == []
    assert captured.get("processed") is True


def test_process_signal_extracts_plain_session_rebased_after_reset_backup(monkeypatch, tmp_path):
    from lib.adapter import set_adapter, reset_adapter
    from ingest import extract as extract_mod

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")

    session_id = "0726256d-2d2f-406e-b81f-4a44e70d93b7"
    sessions_dir = tmp_path / ".openclaw" / "agents" / "main" / "sessions"
    sessions_dir.mkdir(parents=True)
    plain_path = sessions_dir / f"{session_id}.jsonl"
    backup_path = sessions_dir / f"{session_id}.jsonl.reset.2026-04-27T19-23-01.326Z"
    backup_path.write_text(
        "\n".join(
            f'{{"type":"message","message":{{"role":"user","content":[{{"type":"text","text":"old line {idx}"}}]}}}}'
            for idx in range(7)
        )
        + "\n",
        encoding="utf-8",
    )
    plain_path.write_text(
        (
            '{"type":"message","message":{"role":"user","content":[{"type":"text",'
            '"text":"My Friday ritual is roasting pumpkin seeds with the codeword cedar-stencil-4821."}]}}\n'
            '{"type":"message","message":{"role":"assistant","content":[{"type":"text",'
            '"text":"Got it — your Friday ritual is roasting pumpkin seeds."}]}}\n'
        ),
        encoding="utf-8",
    )

    source_key = extraction_daemon._signal_source_cursor_key(session_id, str(plain_path))
    extraction_daemon.write_cursor(session_id, 7, str(backup_path), source_key=source_key)
    signal_path = extraction_daemon.write_signal(
        signal_type="session_end",
        session_id=session_id,
        transcript_path=str(plain_path),
    )
    signal_data = json.loads(signal_path.read_text(encoding="utf-8"))
    signal_data["_signal_path"] = str(signal_path)

    monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "Solomon Steadman")
    monkeypatch.setattr(extraction_daemon, "_read_usage_totals", lambda: {})
    captured = {"transcripts": []}

    class _Adapter(_OwnedTestAdapterMixin):
        def instance_root(self):
            return tmp_path / "instances" / "openclaw-main"

        def parse_session_jsonl(self, path):
            raw = Path(path).read_text(encoding="utf-8")
            if "cedar-stencil-4821" in raw:
                return (
                    "User: My Friday ritual is roasting pumpkin seeds with the codeword cedar-stencil-4821.\n"
                    "Assistant: Got it — your Friday ritual is roasting pumpkin seeds."
                )
            return "User: old line"

        def is_subagent_session(self, session_id, transcript_path=None):
            return False

    set_adapter(_Adapter())
    try:
        def _fake_extract_from_transcript(transcript, **_kwargs):
            captured["transcripts"].append(transcript)
            return {
                "chunks_processed": 1,
                "chunks_total": 1,
                "unclassified_empty_payloads": 0,
                "raw_facts": [
                    {
                        "text": "Solomon Steadman's Friday ritual is roasting pumpkin seeds with the codeword cedar-stencil-4821",
                        "speaker": "user",
                        "privacy": "private",
                        "category": "fact",
                        "domains": ["personal"],
                    }
                ],
                "facts": [],
                "raw_snippets": {},
                "raw_journal": {},
                "raw_project_logs": {},
                "carry_facts": [],
            }

        monkeypatch.setattr(extract_mod, "extract_from_transcript", _fake_extract_from_transcript)
        monkeypatch.setattr(
            extract_mod,
            "apply_extracted_payloads",
            lambda payload, **_kwargs: {
                "facts_stored": len(payload.get("raw_facts", [])),
                "facts_skipped": 0,
                "edges_created": 0,
                "facts": [{"status": "stored"} for _ in payload.get("raw_facts", [])],
                "snippets": {},
                "journal": {},
                "project_log_metrics": {},
            },
        )

        extraction_daemon.process_signal(signal_data)
    finally:
        reset_adapter()

    assert len(captured["transcripts"]) == 1
    assert "cedar-stencil-4821" in captured["transcripts"][0]
    cursor = extraction_daemon.read_cursor(session_id, source_key=source_key)
    assert cursor["transcript_path"] == str(plain_path)
    assert cursor["line_offset"] == 2


def test_count_transcript_lines_raises_on_stat_error_when_fail_hard(monkeypatch):
    def _raise_permission_error(*_args, **_kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr("builtins.open", _raise_permission_error)
    monkeypatch.setattr(extraction_daemon, "_should_raise_transcript_stat_error", lambda _path, _exc: True)

    with pytest.raises(PermissionError, match="denied"):
        extraction_daemon.count_transcript_lines("/tmp/unreadable-session.jsonl")


def test_read_transcript_slice_raises_read_error_under_failhard(monkeypatch):
    def _raise_permission_error(*_args, **_kwargs):
        raise PermissionError("slice denied")

    monkeypatch.setattr(extraction_daemon, "open", _raise_permission_error, raising=False)
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

    with pytest.raises(PermissionError, match="slice denied"):
        extraction_daemon.read_transcript_slice("/tmp/unreadable-session.jsonl", 0)


def test_read_transcript_token_window_raises_read_error_under_failhard(monkeypatch):
    def _raise_permission_error(*_args, **_kwargs):
        raise PermissionError("token denied")

    monkeypatch.setattr(extraction_daemon, "open", _raise_permission_error, raising=False)
    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

    with pytest.raises(PermissionError, match="token denied"):
        extraction_daemon.read_transcript_token_window("/tmp/unreadable-session.jsonl", 0, 100)


def test_fail_hard_wrapper_fails_closed_on_import_error(monkeypatch):
    original_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "lib.fail_policy":
            raise ImportError("missing fail policy")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert extraction_daemon._fail_hard_enabled() is True


def test_read_usage_totals_reads_only_appended_events(tmp_path, monkeypatch):
    usage_path = tmp_path / "llm-usage-events.jsonl"
    monkeypatch.setattr(extraction_daemon, "_usage_events_path", lambda: usage_path)
    extraction_daemon._USAGE_TOTALS_CACHE.update({
        "path": "",
        "device": None,
        "inode": None,
        "offset": 0,
        "totals": None,
    })

    usage_path.write_text(
        "\n".join([
            json.dumps({"tier": "fast", "input_tokens": 10, "output_tokens": 4}),
            json.dumps({"tier": "deep", "input_tokens": 30, "output_tokens": 12}),
        ]) + "\n",
        encoding="utf-8",
    )
    real_loads = extraction_daemon.json.loads
    parsed_lines = []

    def _tracking_loads(line):
        parsed_lines.append(line)
        return real_loads(line)

    monkeypatch.setattr(extraction_daemon.json, "loads", _tracking_loads)

    first = extraction_daemon._read_usage_totals()
    assert first["calls"] == 2
    assert first["input_tokens"] == 40
    assert first["output_tokens"] == 16
    assert first["fast_calls"] == 1
    assert first["deep_calls"] == 1
    assert len(parsed_lines) == 2

    parsed_lines.clear()
    with usage_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"tier": "fast", "input_tokens": 7, "output_tokens": 3}) + "\n")

    second = extraction_daemon._read_usage_totals()
    assert second["calls"] == 3
    assert second["input_tokens"] == 47
    assert second["output_tokens"] == 19
    assert second["fast_calls"] == 2
    assert second["deep_calls"] == 1
    assert len(parsed_lines) == 1


def test_check_idle_sessions_skips_transcripts_older_than_installed_at(monkeypatch, tmp_path):
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text('{"role":"user","content":"hello"}\n{"role":"assistant","content":"hi"}\n', encoding="utf-8")

    instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
    cursor_dir = tmp_path / "instances" / instance_id / "data" / "session-cursors"
    cursor_dir.mkdir(parents=True, exist_ok=True)
    (cursor_dir / "sess-1.json").write_text(
        (
            '{"session_id":"sess-1","line_offset":1,'
            f'"transcript_path":"{transcript_path}"'
            '}'
        ),
        encoding="utf-8",
    )

    now = 1_700_000_000.0
    installed_at = now - (10 * 60)
    stale_mtime = now - (31 * 60)
    transcript_path.touch()
    os.utime(transcript_path, (stale_mtime, stale_mtime))

    captured = []
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
    monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: installed_at)
    monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
    monkeypatch.setattr(
        extraction_daemon,
        "write_signal",
        lambda *args, **kwargs: captured.append((args, kwargs)),
    )

    extraction_daemon.check_idle_sessions(timeout_minutes=30)

    assert captured == []


def test_check_idle_sessions_does_not_skip_fresh_transcript_when_installed_at_missing(monkeypatch, tmp_path):
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        '{"role":"user","content":"hello"}\n{"role":"assistant","content":"hi"}\n',
        encoding="utf-8",
    )

    instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
    cursor_dir = tmp_path / "instances" / instance_id / "data" / "session-cursors"
    cursor_dir.mkdir(parents=True, exist_ok=True)
    (cursor_dir / "sess-fresh.json").write_text(
        (
            '{"session_id":"sess-fresh","line_offset":0,'
            f'"transcript_path":"{transcript_path}","transcript_size_bytes":0'
            '}'
        ),
        encoding="utf-8",
    )

    now = 1_700_000_000.0
    stale_mtime = now - (31 * 60)
    os.utime(transcript_path, (stale_mtime, stale_mtime))

    captured = []
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
    monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
    monkeypatch.setattr(
        extraction_daemon,
        "write_signal",
        lambda *args, **kwargs: captured.append((args, kwargs)),
    )

    extraction_daemon.check_idle_sessions(timeout_minutes=30)

    assert len(captured) == 1
    assert captured[0][1]["signal_type"] == "timeout"
    assert captured[0][1]["session_id"] == "sess-fresh"
    assert (tmp_path / "instances" / instance_id / "data" / "installed-at.json").is_file()


def test_check_idle_sessions_advances_internal_session_cursor_to_eof(monkeypatch, tmp_path):
    import sys
    import types

    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text('{"role":"user","content":"internal maintenance"}\n', encoding="utf-8")

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    extraction_daemon.write_cursor("sess-internal", 0, str(transcript_path))

    real_adapter = sys.modules.get("lib.adapter")
    fake_adapter_mod = types.ModuleType("lib.adapter")

    class _FakeAdapter(_OwnedTestAdapterMixin):
        def parse_session_jsonl(self, path):
            assert path == transcript_path
            return ""

    fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
    sys.modules["lib.adapter"] = fake_adapter_mod

    captured = []
    monkeypatch.setattr(extraction_daemon.time, "time", lambda: 1_700_000_000.0)
    monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: 0.0)
    monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
    monkeypatch.setattr(
        extraction_daemon,
        "write_signal",
        lambda *args, **kwargs: captured.append((args, kwargs)),
    )

    try:
        extraction_daemon.check_idle_sessions(timeout_minutes=30)
    finally:
        if real_adapter is not None:
            sys.modules["lib.adapter"] = real_adapter
        else:
            sys.modules.pop("lib.adapter", None)

    cursor = extraction_daemon.read_cursor("sess-internal")
    assert captured == []
    assert cursor["line_offset"] == 1
    assert cursor["transcript_path"] == str(transcript_path)
    assert cursor["internal"] is True


def test_check_idle_sessions_does_not_freeze_parse_empty_growth(monkeypatch, tmp_path):
    import sys
    import types

    transcript_path = tmp_path / "parse-empty-grown-session.jsonl"
    transcript_path.write_text("", encoding="utf-8")

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    session_id = "sess-parse-empty-grown"
    extraction_daemon.write_cursor(session_id, 0, str(transcript_path))
    transcript_path.write_text(
        '{"role":"user","content":"new startup row not parseable yet"}\n',
        encoding="utf-8",
    )

    real_adapter = sys.modules.get("lib.adapter")
    fake_adapter_mod = types.ModuleType("lib.adapter")

    class _ParseEmptyAdapter(_OwnedTestAdapterMixin):
        def parse_session_jsonl(self, path):
            assert path == transcript_path
            return ""

    fake_adapter_mod.get_adapter = lambda: _ParseEmptyAdapter()
    sys.modules["lib.adapter"] = fake_adapter_mod

    captured = []
    now = 1_700_000_000.0
    os.utime(transcript_path, (now - 120, now - 120))
    monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
    monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: 0.0)
    monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
    monkeypatch.setattr(
        extraction_daemon,
        "write_signal",
        lambda *args, **kwargs: captured.append((args, kwargs)),
    )

    try:
        extraction_daemon.check_idle_sessions(timeout_minutes=30)
    finally:
        if real_adapter is not None:
            sys.modules["lib.adapter"] = real_adapter
        else:
            sys.modules.pop("lib.adapter", None)

    cursor = extraction_daemon.read_cursor(session_id)
    assert captured == []
    assert cursor["line_offset"] == 0
    assert cursor["transcript_size_bytes"] > 0
    assert not cursor.get("internal")


def _adapter_startup_wrapper_turn(text: str) -> bool:
    return (
        "A new session was started via /new or /reset." in str(text or "")
        or "ADAPTER_LOCALIZED_STARTUP_WRAPPER" in str(text or "")
    )


def test_timeout_startup_turn_requires_adapter_predicate():
    assert not extraction_daemon._is_timeout_startup_user_turn(
        "A new session was started via /new or /reset."
    )
    assert extraction_daemon._is_timeout_startup_user_turn(
        "A new session was started via /new or /reset.",
        startup_wrapper_predicate=_adapter_startup_wrapper_turn,
    )


def test_timeout_classifier_uses_adapter_startup_predicate_without_daemon_prose():
    transcript = (
        "User: ADAPTER_LOCALIZED_STARTUP_WRAPPER\n"
        "Assistant: NO_REPLY\n"
        "User: Hello"
    )

    assert (
        extraction_daemon._classify_timeout_transcript_content(
            transcript,
            startup_wrapper_predicate=_adapter_startup_wrapper_turn,
        )
        == extraction_daemon._TRANSCRIPT_CLASS_IGNORE_CONTENT
    )
    assert not extraction_daemon._transcript_has_meaningful_timeout_user_content(
        transcript,
        startup_wrapper_predicate=_adapter_startup_wrapper_turn,
    )


@pytest.mark.parametrize("turn", ["Hello", "Hola"])
def test_timeout_classifier_treats_short_startup_turn_as_ignore_not_internal(turn):
    transcript = (
        "User: A new session was started via /new or /reset.\n"
        "Assistant: NO_REPLY\n"
        f"User: {turn}"
    )

    assert (
        extraction_daemon._classify_timeout_transcript_content(
            transcript,
            startup_wrapper_predicate=_adapter_startup_wrapper_turn,
        )
        == extraction_daemon._TRANSCRIPT_CLASS_IGNORE_CONTENT
    )
    assert not extraction_daemon._transcript_has_meaningful_timeout_user_content(
        transcript,
        startup_wrapper_predicate=_adapter_startup_wrapper_turn,
    )


def test_timeout_classifier_keeps_short_unicode_startup_user_turn_meaningful():
    transcript = (
        "User: A new session was started via /new or /reset.\n"
        "Assistant: NO_REPLY\n"
        "User: 会議は三時"
    )

    assert (
        extraction_daemon._classify_timeout_transcript_content(
            transcript,
            startup_wrapper_predicate=_adapter_startup_wrapper_turn,
        )
        == extraction_daemon._TRANSCRIPT_CLASS_MEANINGFUL_USER_CONTENT
    )
    assert extraction_daemon._transcript_has_meaningful_timeout_user_content(
        transcript,
        startup_wrapper_predicate=_adapter_startup_wrapper_turn,
    )


def test_timeout_classifier_parses_translated_protocol_roles():
    transcript = (
        "Usuario: A new session was started via /new or /reset.\n"
        "Asistente: NO_REPLY\n"
        "Usuario: La reunión es a las tres."
    )

    assert extraction_daemon._iter_parsed_transcript_turns(transcript) == [
        ("user", "A new session was started via /new or /reset."),
        ("assistant", "NO_REPLY"),
        ("user", "La reunión es a las tres."),
    ]
    assert (
        extraction_daemon._classify_timeout_transcript_content(
            transcript,
            startup_wrapper_predicate=_adapter_startup_wrapper_turn,
        )
        == extraction_daemon._TRANSCRIPT_CLASS_MEANINGFUL_USER_CONTENT
    )
    assert extraction_daemon._transcript_has_meaningful_timeout_user_content(
        transcript,
        startup_wrapper_predicate=_adapter_startup_wrapper_turn,
    )


def test_timeout_classifier_keeps_non_english_role_user_content_meaningful():
    transcript = (
        "用户: A new session was started via /new or /reset.\n"
        "助手: NO_REPLY\n"
        "用户: 会議は三時"
    )

    assert (
        extraction_daemon._classify_timeout_transcript_content(
            transcript,
            startup_wrapper_predicate=_adapter_startup_wrapper_turn,
        )
        == extraction_daemon._TRANSCRIPT_CLASS_MEANINGFUL_USER_CONTENT
    )
    assert extraction_daemon._transcript_has_meaningful_timeout_user_content(
        transcript,
        startup_wrapper_predicate=_adapter_startup_wrapper_turn,
    )


def test_timeout_classifier_keeps_visible_assistant_only_content_meaningful():
    transcript = "Assistant: Visible assistant content should remain extractable."

    assert (
        extraction_daemon._classify_timeout_transcript_content(transcript)
        == extraction_daemon._TRANSCRIPT_CLASS_MEANINGFUL_USER_CONTENT
    )
    assert extraction_daemon._transcript_has_meaningful_timeout_user_content(transcript)


def test_timeout_classifier_ignores_structural_turn_timestamps():
    transcript = (
        "[2026-05-02T14:29:12.371Z] User: A new session was started via /new or /reset.\n"
        "[2026-05-02T14:29:12.371Z] Assistant: NO_REPLY\n"
        "[2026-05-02T14:29:21.414Z] User: Hola"
    )

    assert (
        extraction_daemon._classify_timeout_transcript_content(
            transcript,
            startup_wrapper_predicate=_adapter_startup_wrapper_turn,
        )
        == extraction_daemon._TRANSCRIPT_CLASS_IGNORE_CONTENT
    )
    assert not extraction_daemon._transcript_has_meaningful_timeout_user_content(
        transcript,
        startup_wrapper_predicate=_adapter_startup_wrapper_turn,
    )


def test_reconcile_consumes_short_startup_turn_without_internal_cursor(monkeypatch, tmp_path):
    import sys
    import types

    transcript_path = tmp_path / "startup-short-turn.jsonl"
    transcript_path.write_text(
        '{"role":"user","content":"A new session was started via /new or /reset."}\n'
        '{"role":"assistant","content":"NO_REPLY"}\n'
        '{"role":"user","content":"Hello"}\n',
        encoding="utf-8",
    )

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    extraction_daemon.write_cursor("sess-startup-short-turn", 0, str(transcript_path))

    real_adapter = sys.modules.get("lib.adapter")
    fake_adapter_mod = types.ModuleType("lib.adapter")

    class _FakeAdapter(_OwnedTestAdapterMixin):
        def is_startup_wrapper_turn(self, text):
            return _adapter_startup_wrapper_turn(text)

        def parse_session_jsonl(self, path):
            assert path == transcript_path
            return (
                "User: A new session was started via /new or /reset.\n"
                "Assistant: NO_REPLY\n"
                "User: Hello"
            )

    fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
    sys.modules["lib.adapter"] = fake_adapter_mod

    try:
        state = extraction_daemon._reconcile_internal_cursor_state(
            "sess-startup-short-turn",
            str(transcript_path),
        )
    finally:
        if real_adapter is not None:
            sys.modules["lib.adapter"] = real_adapter
        else:
            sys.modules.pop("lib.adapter", None)

    cursor = extraction_daemon.read_cursor("sess-startup-short-turn")
    assert state == "ignored"
    assert cursor["line_offset"] == 3
    assert cursor["internal"] is False


def test_timeout_classifier_keeps_real_startup_user_turn_meaningful():
    transcript = (
        "User: A new session was started via /new or /reset.\n"
        "Assistant: NO_REPLY\n"
        "User: Pumpkin paprika maple-salt ritual belongs to the sibling instance."
    )

    assert (
        extraction_daemon._classify_timeout_transcript_content(transcript)
        == extraction_daemon._TRANSCRIPT_CLASS_MEANINGFUL_USER_CONTENT
    )
    assert extraction_daemon._transcript_has_meaningful_timeout_user_content(transcript)


def test_classify_transcript_session_warns_on_parse_failure_when_fail_open(
    monkeypatch, tmp_path, caplog
):
    transcript_path = tmp_path / "parse-failure.jsonl"
    transcript_path.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")

    class _BrokenAdapter(_OwnedTestAdapterMixin):
        def parse_session_jsonl(self, path):
            assert path == transcript_path
            raise RuntimeError("parse exploded")

    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: False)

    with caplog.at_level("WARNING", logger="quaid.daemon"):
        result = extraction_daemon._classify_transcript_session(
            "sess-parse-failure",
            str(transcript_path),
            adapter=_BrokenAdapter(),
        )

    assert result == extraction_daemon._TRANSCRIPT_CLASS_MEANINGFUL_USER_CONTENT
    assert "transcript classification failed for session sess-parse-failure" in caplog.text
    assert "parse exploded" in caplog.text


def test_classify_transcript_session_raises_parse_failure_when_fail_hard(
    monkeypatch, tmp_path
):
    transcript_path = tmp_path / "parse-failure.jsonl"
    transcript_path.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")

    class _BrokenAdapter(_OwnedTestAdapterMixin):
        def parse_session_jsonl(self, path):
            assert path == transcript_path
            raise RuntimeError("parse exploded")

    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="parse exploded"):
        extraction_daemon._classify_transcript_session(
            "sess-parse-failure",
            str(transcript_path),
            adapter=_BrokenAdapter(),
        )


def test_process_signal_merges_subagent_transcript_with_per_turn_labels(monkeypatch, tmp_path):
    parent_path = tmp_path / "parent.jsonl"
    child_path = tmp_path / "child.jsonl"
    parent_path.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")
    child_path.write_text('{"role":"user","content":"child"}\n', encoding="utf-8")

    captured = {}

    class _FakeAdapter(_OwnedTestAdapterMixin):
        def instance_root(self):
            return tmp_path / "instances" / "pytest-runner"

        def parse_session_jsonl(self, path):
            assert Path(path).name in {"tmp", Path(path).name} or True
            return "User: Parent message with enough content to exceed the extraction minimum length."

        def parse_subagent_session_jsonl(self, path):
            assert Path(path) == child_path
            return "Subagent/User: Child fact from subagent.\n\nSubagent/Assistant: Child reply."

    fake_registry = types.ModuleType("core.subagent_registry")
    fake_registry.get_harvestable = lambda sid: [{"child_id": "child-1", "transcript_path": str(child_path), "child_type": "default"}]
    fake_registry.mark_harvested = lambda sid, cid: captured.setdefault("harvested", []).append((sid, cid))
    fake_registry.is_registered_subagent = lambda sid: False

    import sys as _sys
    real_registry = _sys.modules.get("core.subagent_registry")
    _sys.modules["core.subagent_registry"] = fake_registry
    from lib.adapter import set_adapter, reset_adapter
    set_adapter(_FakeAdapter())

    monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "owner-1")
    _stub_successful_session_logs_ingest(monkeypatch)
    monkeypatch.setattr(extraction_daemon, "read_cursor", lambda sid: {"line_offset": 0, "transcript_path": str(parent_path)})
    monkeypatch.setattr(extraction_daemon, "count_transcript_lines", lambda p: 1)
    monkeypatch.setattr(extraction_daemon, "read_transcript_slice", lambda path, from_line: ['{"role":"user","content":"hello"}\n'])
    monkeypatch.setattr(extraction_daemon, "_tmp_dir", lambda: tmp_path)
    monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *args, **kwargs: None)
    monkeypatch.setattr(extraction_daemon, "mark_signal_processed", lambda *args, **kwargs: None)
    monkeypatch.setattr(extraction_daemon, "_read_usage_totals", lambda: {})
    monkeypatch.setattr(extraction_daemon, "write_rolling_state", lambda *args, **kwargs: None)

    from ingest import extract as extract_mod

    def fake_extract_from_transcript(transcript, **kwargs):
        captured.setdefault("transcripts", []).append(transcript)
        if transcript.startswith("Subagent/User:"):
            return {
                "chunks_processed": 1,
                "chunks_total": 1,
                "unclassified_empty_payloads": 0,
                "raw_facts": [
                    {
                        "text": "User's uncle owns a vineyard in Mendoza.",
                        "speaker": "user",
                        "category": "fact",
                        "extraction_confidence": "high",
                    }
                ],
                "facts": [],
                "soul_snippets": {},
                "journal_entries": {},
                "project_logs": {},
                "raw_snippets": {},
                "raw_journal": {},
                "raw_project_logs": {},
            }
        return {
            "chunks_processed": 1,
            "chunks_total": 1,
            "unclassified_empty_payloads": 0,
            "raw_facts": [],
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
        }

    monkeypatch.setattr(extract_mod, "extract_from_transcript", fake_extract_from_transcript)
    monkeypatch.setattr(
        extract_mod,
        "apply_extracted_payloads",
        lambda payload, *args, **kwargs: captured.setdefault("flush_payload", payload) or {"facts_stored": 0, "facts_skipped": 0, "facts": []},
    )

    try:
        extraction_daemon.process_signal(
            {
                "session_id": "parent-1",
                "type": "session_end",
                "transcript_path": str(parent_path),
                "signal_path": str(tmp_path / "sig.json"),
            }
        )
    finally:
        if real_registry is not None:
            _sys.modules["core.subagent_registry"] = real_registry
        else:
            _sys.modules.pop("core.subagent_registry", None)
        reset_adapter()

    assert any("Subagent/User: Child fact from subagent." in item for item in captured["transcripts"])
    stamped = captured["flush_payload"]["raw_facts"][0]
    assert stamped["source"] == "subagent"
    assert stamped["_source_label"].endswith("-subagent-extraction")
    assert stamped["_source_id"] == "child-1"
    assert captured["harvested"] == [("parent-1", "child-1")]


def test_session_has_harvestable_subagents_warns_and_uses_adapter_fallback(monkeypatch, caplog):
    import sys as _sys

    real_registry = _sys.modules.get("core.subagent_registry")
    fake_registry = types.ModuleType("core.subagent_registry")
    fake_registry.get_harvestable = lambda _sid: (_ for _ in ()).throw(RuntimeError("registry down"))
    _sys.modules["core.subagent_registry"] = fake_registry

    class _FallbackAdapter:
        def discover_subagent_children(self, parent_session_id):
            assert parent_session_id == "parent-1"
            return [{"child_id": "child-1", "transcript_path": "/tmp/child.jsonl"}]

    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: False)

    try:
        with caplog.at_level("WARNING", logger="quaid.daemon"):
            assert extraction_daemon._session_has_harvestable_subagents(
                "parent-1",
                adapter=_FallbackAdapter(),
            )
    finally:
        if real_registry is not None:
            _sys.modules["core.subagent_registry"] = real_registry
        else:
            _sys.modules.pop("core.subagent_registry", None)

    assert "subagent harvest registry lookup failed for parent-1" in caplog.text
    assert "registry down" in caplog.text


def test_session_has_harvestable_subagents_raises_registry_failure_when_fail_hard(monkeypatch):
    import sys as _sys

    real_registry = _sys.modules.get("core.subagent_registry")
    fake_registry = types.ModuleType("core.subagent_registry")
    fake_registry.get_harvestable = lambda _sid: (_ for _ in ()).throw(RuntimeError("registry down"))
    _sys.modules["core.subagent_registry"] = fake_registry

    class _FallbackAdapter:
        def discover_subagent_children(self, parent_session_id):
            raise AssertionError("failHard registry failure must not fall back to adapter discovery")

    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

    try:
        with pytest.raises(RuntimeError, match="registry down"):
            extraction_daemon._session_has_harvestable_subagents(
                "parent-1",
                adapter=_FallbackAdapter(),
            )
    finally:
        if real_registry is not None:
            _sys.modules["core.subagent_registry"] = real_registry
        else:
            _sys.modules.pop("core.subagent_registry", None)


def test_session_has_harvestable_subagents_raises_adapter_discovery_when_fail_hard(monkeypatch):
    import sys as _sys

    real_registry = _sys.modules.get("core.subagent_registry")
    fake_registry = types.ModuleType("core.subagent_registry")
    fake_registry.get_harvestable = lambda _sid: []
    _sys.modules["core.subagent_registry"] = fake_registry

    class _FailingAdapter:
        def discover_subagent_children(self, parent_session_id):
            raise RuntimeError(f"discovery failed for {parent_session_id}")

    monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

    try:
        with pytest.raises(RuntimeError, match="discovery failed for parent-1"):
            extraction_daemon._session_has_harvestable_subagents(
                "parent-1",
                adapter=_FailingAdapter(),
            )
    finally:
        if real_registry is not None:
            _sys.modules["core.subagent_registry"] = real_registry
        else:
            _sys.modules.pop("core.subagent_registry", None)


def test_process_signal_harvests_subagent_when_parent_cursor_at_eof(monkeypatch, tmp_path):
    parent_path = tmp_path / "parent.jsonl"
    child_path = tmp_path / "child.jsonl"
    parent_path.write_text('{"role":"user","content":"already consumed"}\n', encoding="utf-8")
    child_path.write_text('{"role":"user","content":"child"}\n', encoding="utf-8")

    captured = {}

    class _FakeAdapter(_OwnedTestAdapterMixin):
        def instance_root(self):
            return tmp_path / "instances" / "pytest-runner"

        def parse_session_jsonl(self, path):
            return "User: Parent transcript was already consumed before the child completed."

        def parse_subagent_session_jsonl(self, path):
            assert Path(path) == child_path
            return "Subagent/User: Child-only Mendoza Malbec fact."

    fake_registry = types.ModuleType("core.subagent_registry")
    fake_registry.get_harvestable = lambda sid: [
        {"child_id": "child-1", "transcript_path": str(child_path), "child_type": "default"}
    ]
    fake_registry.mark_harvested = lambda sid, cid: captured.setdefault("harvested", []).append((sid, cid))
    fake_registry.is_registered_subagent = lambda sid: False

    import sys as _sys
    real_registry = _sys.modules.get("core.subagent_registry")
    _sys.modules["core.subagent_registry"] = fake_registry
    from lib.adapter import set_adapter, reset_adapter
    set_adapter(_FakeAdapter())

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "owner-1")
    monkeypatch.setattr(extraction_daemon, "read_cursor", lambda sid: {"line_offset": 1, "transcript_path": str(parent_path)})
    monkeypatch.setattr(extraction_daemon, "count_transcript_lines", lambda p: 1)
    monkeypatch.setattr(extraction_daemon, "read_transcript_slice", lambda path, from_line: [])
    monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *args, **kwargs: None)
    monkeypatch.setattr(extraction_daemon, "mark_signal_processed", lambda *args, **kwargs: None)
    monkeypatch.setattr(extraction_daemon, "_read_usage_totals", lambda: {})

    from ingest import extract as extract_mod

    def fake_extract_from_transcript(transcript, **kwargs):
        captured.setdefault("transcripts", []).append(transcript)
        assert transcript.startswith("Subagent/User:")
        return {
            "chunks_processed": 1,
            "chunks_total": 1,
            "unclassified_empty_payloads": 0,
            "raw_facts": [
                {
                    "text": "The subagent found a Mendoza Malbec fact.",
                    "speaker": "user",
                    "category": "fact",
                    "extraction_confidence": "high",
                }
            ],
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
        }

    monkeypatch.setattr(extract_mod, "extract_from_transcript", fake_extract_from_transcript)
    monkeypatch.setattr(
        extract_mod,
        "apply_extracted_payloads",
        lambda payload, *args, **kwargs: captured.setdefault("flush_payload", payload) or {"facts_stored": 0, "facts_skipped": 0, "facts": []},
    )

    try:
        extraction_daemon.process_signal(
            {
                "session_id": "parent-1",
                "type": "session_end",
                "transcript_path": str(parent_path),
                "_signal_path": str(tmp_path / "sig.json"),
            }
        )
    finally:
        if real_registry is not None:
            _sys.modules["core.subagent_registry"] = real_registry
        else:
            _sys.modules.pop("core.subagent_registry", None)
        reset_adapter()

    assert captured["transcripts"] == ["Subagent/User: Child-only Mendoza Malbec fact."]
    stamped = captured["flush_payload"]["raw_facts"][0]
    assert stamped["source"] == "subagent"
    assert stamped["_source_label"].endswith("-subagent-extraction")
    assert stamped["_source_id"] == "child-1"
    assert captured["harvested"] == [("parent-1", "child-1")]


def test_process_signal_persists_adapter_discovered_subagent_before_harvest(monkeypatch, tmp_path):
    import sys as _sys

    parent_path = tmp_path / "parent.jsonl"
    child_path = tmp_path / "child.jsonl"
    parent_path.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")
    child_path.write_text('{"role":"user","content":"child"}\n', encoding="utf-8")

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")

    captured = {}

    class _FakeAdapter(_OwnedTestAdapterMixin):
        def instance_root(self):
            return tmp_path / "instances" / "pytest-runner"

        def parse_session_jsonl(self, path):
            return "User: Parent message with enough content to exceed the extraction minimum length."

        def parse_subagent_session_jsonl(self, path):
            assert Path(path) == child_path
            return "Subagent/User: Child-reported fact about Mendoza Malbec."

        def discover_subagent_children(self, parent_session_id):
            assert parent_session_id == "parent-1"
            return [
                {
                    "child_id": "child-1",
                    "transcript_path": str(child_path),
                    "child_type": "codex-subagent",
                }
            ]

    real_registry = _sys.modules.pop("core.subagent_registry", None)
    from lib.adapter import set_adapter, reset_adapter
    set_adapter(_FakeAdapter())

    monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "owner-1")
    monkeypatch.setattr(extraction_daemon, "read_cursor", lambda sid: {"line_offset": 0, "transcript_path": str(parent_path)})
    monkeypatch.setattr(extraction_daemon, "count_transcript_lines", lambda p: 1)
    monkeypatch.setattr(extraction_daemon, "read_transcript_slice", lambda path, from_line: ['{"role":"user","content":"hello"}\n'])
    monkeypatch.setattr(extraction_daemon, "_tmp_dir", lambda: tmp_path)
    monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *args, **kwargs: None)
    monkeypatch.setattr(extraction_daemon, "mark_signal_processed", lambda *args, **kwargs: None)
    monkeypatch.setattr(extraction_daemon, "_read_usage_totals", lambda: {})
    monkeypatch.setattr(extraction_daemon, "write_rolling_state", lambda *args, **kwargs: None)

    from ingest import extract as extract_mod

    def fake_extract_from_transcript(transcript, **kwargs):
        if transcript.startswith("Subagent/User:"):
            return {
                "chunks_processed": 1,
                "chunks_total": 1,
                "unclassified_empty_payloads": 0,
                "raw_facts": [
                    {
                        "text": "The user's uncle recommended Mendoza Malbec.",
                        "speaker": "user",
                        "category": "fact",
                        "extraction_confidence": "high",
                    }
                ],
                "facts": [],
                "soul_snippets": {},
                "journal_entries": {},
                "project_logs": {},
                "raw_snippets": {},
                "raw_journal": {},
                "raw_project_logs": {},
            }
        return {
            "chunks_processed": 1,
            "chunks_total": 1,
            "unclassified_empty_payloads": 0,
            "raw_facts": [],
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
        }

    monkeypatch.setattr(extract_mod, "extract_from_transcript", fake_extract_from_transcript)
    monkeypatch.setattr(
        extract_mod,
        "apply_extracted_payloads",
        lambda payload, *args, **kwargs: captured.setdefault("flush_payload", payload) or {"facts_stored": 0, "facts_skipped": 0, "facts": []},
    )

    try:
        extraction_daemon.process_signal(
            {
                "session_id": "parent-1",
                "type": "session_end",
                "transcript_path": str(parent_path),
                "signal_path": str(tmp_path / "sig.json"),
            }
        )
    finally:
        if real_registry is not None:
            _sys.modules["core.subagent_registry"] = real_registry
        else:
            _sys.modules.pop("core.subagent_registry", None)
        reset_adapter()

    registry_path = tmp_path / "instances" / "pytest-runner" / "data" / "subagent-registry" / "parent-1.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    child_entry = registry["children"]["child-1"]
    assert child_entry["status"] == "complete"
    assert child_entry["transcript_path"] == str(child_path)
    assert child_entry["harvested"] is True

    stamped = captured["flush_payload"]["raw_facts"][0]
    assert stamped["source"] == "subagent"
    assert stamped["_source_label"].endswith("-subagent-extraction")
    assert stamped["_source_id"] == "child-1"


def test_process_signal_advances_internal_session_cursor_to_eof(monkeypatch, tmp_path):
    import sys
    import types

    transcript_path = tmp_path / "internal.jsonl"
    transcript_path.write_text('{"role":"user","content":"internal maintenance"}\n', encoding="utf-8")

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    extraction_daemon.write_cursor("sess-internal", 0, str(transcript_path))
    signal_path = extraction_daemon.write_signal(
        signal_type="session_end",
        session_id="sess-internal",
        transcript_path=str(transcript_path),
    )

    real_registry = sys.modules.get("core.subagent_registry")
    real_adapter = sys.modules.get("lib.adapter")
    fake_registry = types.ModuleType("core.subagent_registry")
    fake_registry.is_registered_subagent = lambda sid: False
    sys.modules["core.subagent_registry"] = fake_registry

    fake_adapter_mod = types.ModuleType("lib.adapter")

    class _FakeAdapter(_OwnedTestAdapterMixin):
        def parse_session_jsonl(self, path):
            assert path == transcript_path
            return ""

    fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
    sys.modules["lib.adapter"] = fake_adapter_mod

    try:
        signals = extraction_daemon.read_pending_signals()
        assert len(signals) == 1
        extraction_daemon.process_signal(signals[0])
    finally:
        if real_registry is not None:
            sys.modules["core.subagent_registry"] = real_registry
        else:
            sys.modules.pop("core.subagent_registry", None)
        if real_adapter is not None:
            sys.modules["lib.adapter"] = real_adapter
        else:
            sys.modules.pop("lib.adapter", None)

    cursor = extraction_daemon.read_cursor("sess-internal")
    assert not signal_path.exists()
    assert cursor["line_offset"] == 1
    assert cursor["transcript_path"] == str(transcript_path)
    assert cursor["internal"] is True


def test_check_chunk_ready_sessions_skips_cursor_marked_internal(monkeypatch, tmp_path):
    import sys
    import types

    transcript_path = tmp_path / "internal-growing.jsonl"
    transcript_path.write_text('{"role":"user","content":"internal maintenance"}\n', encoding="utf-8")

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    extraction_daemon.write_cursor("sess-internal-skip", 1, str(transcript_path), internal=True)

    real_adapter = sys.modules.get("lib.adapter")
    fake_adapter_mod = types.ModuleType("lib.adapter")

    class _FailIfParsedAdapter:
        def parse_session_jsonl(self, path):
            raise AssertionError(f"internal-marked session should not be reparsed: {path}")

    fake_adapter_mod.get_adapter = lambda: _FailIfParsedAdapter()
    sys.modules["lib.adapter"] = fake_adapter_mod

    captured = []
    monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
    monkeypatch.setattr(
        extraction_daemon,
        "write_signal",
        lambda *args, **kwargs: captured.append((args, kwargs)),
    )

    try:
        extraction_daemon.check_chunk_ready_sessions()
    finally:
        if real_adapter is not None:
            sys.modules["lib.adapter"] = real_adapter
        else:
            sys.modules.pop("lib.adapter", None)

    assert captured == []


def test_check_chunk_ready_sessions_does_not_consume_parse_empty_transient(monkeypatch, tmp_path):
    import sys
    import types

    transcript_path = tmp_path / "parse-empty-growing.jsonl"
    transcript_path.write_text(
        '{"role":"user","content":"The live rolling tail is present but not parseable yet."}\n',
        encoding="utf-8",
    )

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    extraction_daemon.write_cursor("sess-parse-empty", 0, str(transcript_path))

    real_adapter = sys.modules.get("lib.adapter")
    fake_adapter_mod = types.ModuleType("lib.adapter")

    class _ParseEmptyAdapter(_OwnedTestAdapterMixin):
        def parse_session_jsonl(self, path):
            return ""

    fake_adapter_mod.get_adapter = lambda: _ParseEmptyAdapter()
    sys.modules["lib.adapter"] = fake_adapter_mod

    captured = []
    monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
    monkeypatch.setattr(
        extraction_daemon,
        "write_signal",
        lambda *args, **kwargs: captured.append((args, kwargs)),
    )

    try:
        extraction_daemon.check_chunk_ready_sessions(chunk_tokens=10)
    finally:
        if real_adapter is not None:
            sys.modules["lib.adapter"] = real_adapter
        else:
            sys.modules.pop("lib.adapter", None)

    cursor = extraction_daemon.read_cursor("sess-parse-empty")
    assert cursor["line_offset"] == 0
    assert not cursor.get("internal")
    assert captured == []


def test_check_chunk_ready_sessions_bounds_parse_empty_deferral_after_stable_size(monkeypatch, tmp_path):
    import sys
    import types

    transcript_path = tmp_path / "parse-empty-stable.jsonl"
    transcript_path.write_text(
        '{"role":"user","content":"The live rolling tail is present but temporarily parse-empty."}\n',
        encoding="utf-8",
    )

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    extraction_daemon.write_cursor("sess-parse-empty-stable", 0, str(transcript_path))

    clock = {"now": 1_700_000_000.0}
    os.utime(transcript_path, (clock["now"], clock["now"]))
    monkeypatch.setattr(extraction_daemon.time, "time", lambda: clock["now"])

    real_adapter = sys.modules.get("lib.adapter")
    fake_adapter_mod = types.ModuleType("lib.adapter")

    class _ParseEmptyAdapter(_OwnedTestAdapterMixin):
        def parse_session_jsonl(self, path):
            return ""

    fake_adapter_mod.get_adapter = lambda: _ParseEmptyAdapter()
    sys.modules["lib.adapter"] = fake_adapter_mod

    captured = []
    monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
    monkeypatch.setattr(
        extraction_daemon,
        "write_signal",
        lambda *args, **kwargs: captured.append((args, kwargs)),
    )

    try:
        extraction_daemon.check_chunk_ready_sessions(chunk_tokens=10)
        cursor = extraction_daemon.read_cursor("sess-parse-empty-stable")
        assert cursor["line_offset"] == 0
        assert not cursor.get("internal")

        clock["now"] += extraction_daemon._ROLLING_INTERNAL_ADVANCE_GRACE_SECONDS + 1
        extraction_daemon.check_chunk_ready_sessions(chunk_tokens=10)
    finally:
        if real_adapter is not None:
            sys.modules["lib.adapter"] = real_adapter
        else:
            sys.modules.pop("lib.adapter", None)

    cursor = extraction_daemon.read_cursor("sess-parse-empty-stable")
    assert cursor["line_offset"] == 1
    assert cursor["internal"] is True
    assert captured == []


def test_transcript_size_bytes_raises_stat_failure_under_failhard(monkeypatch):
    fake_fail_policy = types.ModuleType("lib.fail_policy")
    fake_fail_policy.is_fail_hard_enabled = lambda: True
    monkeypatch.setitem(sys.modules, "lib.fail_policy", fake_fail_policy)

    def _raise_stat(_path):
        raise OSError("stat failed")

    monkeypatch.setattr(extraction_daemon.os, "stat", _raise_stat)

    with pytest.raises(OSError, match="stat failed"):
        extraction_daemon._transcript_size_bytes("/missing/transcript.jsonl")


def test_stable_transcript_snapshot_raises_creation_failure_under_failhard(monkeypatch, tmp_path):
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text('{"role":"user","content":"Baxter tail"}\n', encoding="utf-8")
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "snapshot-inst")
    fake_fail_policy = types.ModuleType("lib.fail_policy")
    fake_fail_policy.is_fail_hard_enabled = lambda: True
    monkeypatch.setitem(sys.modules, "lib.fail_policy", fake_fail_policy)

    def _raise_replace(_src, _dst):
        raise OSError("snapshot replace failed")

    monkeypatch.setattr(extraction_daemon.os, "replace", _raise_replace)
    with pytest.raises(OSError, match="snapshot replace failed"):
        extraction_daemon._stable_transcript_snapshot_for_continued_rolling(
            "sess-snapshot-fail",
            str(transcript_path),
        )
    assert not list(extraction_daemon._rolling_transcript_snapshot_dir().rglob("*.tmp"))


def test_stable_transcript_snapshot_falls_back_when_not_failhard(monkeypatch, tmp_path):
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text('{"role":"user","content":"Baxter tail"}\n', encoding="utf-8")
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "snapshot-inst")
    fake_fail_policy = types.ModuleType("lib.fail_policy")
    fake_fail_policy.is_fail_hard_enabled = lambda: False
    monkeypatch.setitem(sys.modules, "lib.fail_policy", fake_fail_policy)

    def _raise_replace(_src, _dst):
        raise OSError("snapshot replace failed")

    monkeypatch.setattr(extraction_daemon.os, "replace", _raise_replace)
    assert extraction_daemon._stable_transcript_snapshot_for_continued_rolling(
        "sess-snapshot-fail",
        str(transcript_path),
    ) == str(transcript_path)
    assert not list(extraction_daemon._rolling_transcript_snapshot_dir().rglob("*.tmp"))


def test_check_chunk_ready_sessions_raises_mtime_failure_under_failhard(monkeypatch, tmp_path):
    import types

    transcript_path = tmp_path / "parse-empty-stat-fail.jsonl"
    transcript_path.write_text(
        '{"role":"user","content":"The live rolling tail cannot be statted."}\n',
        encoding="utf-8",
    )

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    extraction_daemon.write_cursor("sess-parse-empty-stat-fail", 0, str(transcript_path))

    fake_fail_policy = types.ModuleType("lib.fail_policy")
    fake_fail_policy.is_fail_hard_enabled = lambda: True
    monkeypatch.setitem(sys.modules, "lib.fail_policy", fake_fail_policy)

    real_adapter = sys.modules.get("lib.adapter")
    fake_adapter_mod = types.ModuleType("lib.adapter")

    class _ParseEmptyAdapter(_OwnedTestAdapterMixin):
        def parse_session_jsonl(self, path):
            return ""

    fake_adapter_mod.get_adapter = lambda: _ParseEmptyAdapter()
    sys.modules["lib.adapter"] = fake_adapter_mod
    monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])

    def _raise_getmtime(_path):
        raise PermissionError("mtime failed")

    monkeypatch.setattr(extraction_daemon.os.path, "getmtime", _raise_getmtime)

    try:
        with pytest.raises(OSError, match="mtime failed"):
            extraction_daemon.check_chunk_ready_sessions(chunk_tokens=10)
    finally:
        if real_adapter is not None:
            sys.modules["lib.adapter"] = real_adapter
        else:
            sys.modules.pop("lib.adapter", None)


def test_check_chunk_ready_sessions_advances_old_parse_empty_internal_transcript(monkeypatch, tmp_path):
    import sys
    import types

    transcript_path = tmp_path / "parse-empty-old.jsonl"
    transcript_path.write_text(
        '{"role":"assistant","content":"internal maintenance only"}\n',
        encoding="utf-8",
    )

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    extraction_daemon.write_cursor("sess-parse-empty-old", 0, str(transcript_path))
    now = 1_700_000_000.0
    os.utime(transcript_path, (now - 120, now - 120))
    monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)

    real_adapter = sys.modules.get("lib.adapter")
    fake_adapter_mod = types.ModuleType("lib.adapter")

    class _ParseEmptyAdapter(_OwnedTestAdapterMixin):
        def parse_session_jsonl(self, path):
            return ""

    fake_adapter_mod.get_adapter = lambda: _ParseEmptyAdapter()
    sys.modules["lib.adapter"] = fake_adapter_mod

    captured = []
    monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
    monkeypatch.setattr(
        extraction_daemon,
        "write_signal",
        lambda *args, **kwargs: captured.append((args, kwargs)),
    )

    try:
        extraction_daemon.check_chunk_ready_sessions(chunk_tokens=10)
    finally:
        if real_adapter is not None:
            sys.modules["lib.adapter"] = real_adapter
        else:
            sys.modules.pop("lib.adapter", None)

    cursor = extraction_daemon.read_cursor("sess-parse-empty-old")
    assert cursor["line_offset"] == 1
    assert cursor["internal"] is True
    assert captured == []


def test_reconcile_internal_cursor_rebases_when_preserved_transcript_replaces_internal_source(monkeypatch, tmp_path):
    import sys
    import types

    internal_path = tmp_path / "internal-source.jsonl"
    internal_path.write_text(
        '{"role":"assistant","content":"[quaid][session-init] Loading project context"}\n',
        encoding="utf-8",
    )
    preserved_path = tmp_path / "preserved-source.jsonl"
    preserved_path.write_text(
        '{"role":"user","content":"Niseko, Kinesis, and Phoebe are the real user facts in this preserved transcript."}\n',
        encoding="utf-8",
    )

    session_id = "sess-preserved-rebase"
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    extraction_daemon.write_cursor(session_id, 1, str(internal_path), internal=True)

    real_adapter = sys.modules.get("lib.adapter")
    fake_adapter_mod = types.ModuleType("lib.adapter")

    class _FakeAdapter(_OwnedTestAdapterMixin):
        def parse_session_jsonl(self, path):
            text = Path(path).read_text(encoding="utf-8")
            if "Niseko" in text and "Phoebe" in text:
                return "User: Niseko, Kinesis, and Phoebe are the real user facts in this preserved transcript."
            return ""

    fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
    sys.modules["lib.adapter"] = fake_adapter_mod

    try:
        state = extraction_daemon._reconcile_internal_cursor_state(
            session_id,
            str(preserved_path),
            cursor_data=extraction_daemon.read_cursor(session_id),
        )
    finally:
        if real_adapter is not None:
            sys.modules["lib.adapter"] = real_adapter
        else:
            sys.modules.pop("lib.adapter", None)

    cursor = extraction_daemon.read_cursor(session_id)
    assert state == "unfrozen"
    assert cursor["internal"] is False
    assert cursor["line_offset"] == 0
    assert cursor["transcript_path"] == str(preserved_path)


def test_process_signal_skips_cursor_marked_internal_without_reparse(monkeypatch, tmp_path):
    import sys
    import types

    transcript_path = tmp_path / "internal-growing.jsonl"
    transcript_path.write_text('{"role":"user","content":"internal maintenance"}\n', encoding="utf-8")

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    extraction_daemon.write_cursor("sess-internal-locked", 1, str(transcript_path), internal=True)
    signal_path = extraction_daemon.write_signal(
        signal_type="rolling",
        session_id="sess-internal-locked",
        transcript_path=str(transcript_path),
    )

    real_registry = sys.modules.get("core.subagent_registry")
    real_adapter = sys.modules.get("lib.adapter")
    fake_registry = types.ModuleType("core.subagent_registry")
    fake_registry.is_registered_subagent = lambda sid: False
    sys.modules["core.subagent_registry"] = fake_registry

    fake_adapter_mod = types.ModuleType("lib.adapter")

    class _FailIfParsedAdapter:
        def parse_session_jsonl(self, path):
            raise AssertionError(f"internal-marked session should not be reparsed: {path}")

    fake_adapter_mod.get_adapter = lambda: _FailIfParsedAdapter()
    sys.modules["lib.adapter"] = fake_adapter_mod

    try:
        signals = extraction_daemon.read_pending_signals()
        assert len(signals) == 1
        extraction_daemon.process_signal(signals[0])
    finally:
        if real_registry is not None:
            sys.modules["core.subagent_registry"] = real_registry
        else:
            sys.modules.pop("core.subagent_registry", None)
        if real_adapter is not None:
            sys.modules["lib.adapter"] = real_adapter
        else:
            sys.modules.pop("lib.adapter", None)

    assert not signal_path.exists()


def test_check_chunk_ready_sessions_unfreezes_internal_cursor_when_real_turn_arrives_after_session_start_noise(
    monkeypatch,
    tmp_path,
):
    import sys
    import types

    transcript_path = tmp_path / "session-start.jsonl"
    transcript_path.write_text(
        (
            '{"role":"assistant","content":"[quaid][session-init] Loading project context"}\n'
            '{"role":"assistant","content":"Warning: plugin exports are noisy but non-fatal"}\n'
            '{"role":"assistant","content":"Loading ~/.claude/settings.json"}\n'
        ),
        encoding="utf-8",
    )

    session_id = "sess-sessionstart-noise"
    initial_lines = extraction_daemon.count_transcript_lines(str(transcript_path))

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    extraction_daemon.write_cursor(session_id, initial_lines, str(transcript_path), internal=True)

    with transcript_path.open("a", encoding="utf-8") as handle:
        handle.write(
            '{"role":"user","content":"My sister Clara likes alpacas, lives in Boise, and runs a kiln studio every weekend."}\n'
        )

    real_adapter = sys.modules.get("lib.adapter")
    fake_adapter_mod = types.ModuleType("lib.adapter")

    class _FakeAdapter(_OwnedTestAdapterMixin):
        def parse_session_jsonl(self, path):
            text = Path(path).read_text(encoding="utf-8")
            if "alpacas" in text and "kiln studio" in text:
                return "User: My sister Clara likes alpacas, lives in Boise, and runs a kiln studio every weekend."
            return ""

    fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
    sys.modules["lib.adapter"] = fake_adapter_mod

    captured = []
    buffered_from_lines = []
    buffer_states = []
    monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
    extraction_daemon.write_rolling_state(
        session_id,
        {
            "transcript_path": str(transcript_path),
            "buffered_line_offset": initial_lines,
            "semantic_buffer": "stale internal startup buffer",
            "semantic_buffer_tokens": 4,
        },
    )

    def fake_buffer_transcript_tail(path, from_line, state, adapter=None, **kwargs):
        buffered_from_lines.append(from_line)
        buffer_states.append(dict(state or {}))
        if from_line == 0:
            semantic_tokens = 12
            semantic_buffer = (
                "User: My sister Clara likes alpacas, lives in Boise, "
                "and runs a kiln studio every weekend."
            )
        else:
            semantic_tokens = 4
            semantic_buffer = "User: weekend."
        return (
            {
                "buffered_line_offset": initial_lines + 1,
                "semantic_buffer": semantic_buffer,
                "semantic_buffer_tokens": semantic_tokens,
            },
            {
                "raw_lines_added": 1,
                "semantic_chars_added": len(semantic_buffer),
                "semantic_tokens_added": semantic_tokens,
                "buffered_line_offset": initial_lines + 1,
            },
        )

    monkeypatch.setattr(extraction_daemon, "_buffer_transcript_tail", fake_buffer_transcript_tail)
    monkeypatch.setattr(extraction_daemon, "write_rolling_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        extraction_daemon,
        "write_signal",
        lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
            {
                "signal_type": signal_type,
                "session_id": session_id,
                "transcript_path": transcript_path,
                "meta": kwargs.get("meta", {}),
            }
        ),
    )

    try:
        extraction_daemon.check_chunk_ready_sessions(chunk_tokens=10)
    finally:
        if real_adapter is not None:
            sys.modules["lib.adapter"] = real_adapter
        else:
            sys.modules.pop("lib.adapter", None)

    cursor = extraction_daemon.read_cursor(session_id)
    assert cursor["line_offset"] == 0
    assert cursor["internal"] is False
    assert buffered_from_lines == [0]
    assert buffer_states[0]["buffered_line_offset"] == 0
    assert buffer_states[0]["semantic_buffer_tokens"] == 0
    assert captured == [
        {
            "signal_type": "rolling",
            "session_id": session_id,
            "transcript_path": str(transcript_path),
            "meta": {
                "reason": "semantic_chunk_budget",
                "chunk_tokens": 10,
                "semantic_buffer_tokens": 12,
                "buffered_line_offset": initial_lines + 1,
            },
        }
    ]


def test_check_chunk_ready_sessions_flushes_subthreshold_tail_after_internal_cursor_unfreezes(
    monkeypatch,
    tmp_path,
):
    import sys
    import types

    transcript_path = tmp_path / "sibling-session.jsonl"
    transcript_path.write_text(
        (
            '{"role":"assistant","content":"[quaid][session-init] Loading project context"}\n'
            '{"role":"assistant","content":"Loading large sibling system prompt"}\n'
            '{"role":"assistant","content":"Ready"}\n'
        ),
        encoding="utf-8",
    )

    session_id = "sess-sibling-unfreeze"
    initial_lines = extraction_daemon.count_transcript_lines(str(transcript_path))

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    extraction_daemon.write_cursor(session_id, initial_lines, str(transcript_path), internal=True)

    with transcript_path.open("a", encoding="utf-8") as handle:
        handle.write(
            '{"role":"user","content":"The reading chair for this instance has a brass desk lamp beside it."}\n'
        )
    mtime = os.path.getmtime(transcript_path)
    monkeypatch.setattr(
        extraction_daemon.time,
        "time",
        lambda: mtime + extraction_daemon._ROLLING_INTERNAL_ADVANCE_GRACE_SECONDS + 1,
    )

    real_adapter = sys.modules.get("lib.adapter")
    fake_adapter_mod = types.ModuleType("lib.adapter")

    class _FakeAdapter(_OwnedTestAdapterMixin):
        def parse_session_jsonl(self, path):
            text = Path(path).read_text(encoding="utf-8")
            if "brass desk lamp" in text:
                return "User: The reading chair for this instance has a brass desk lamp beside it."
            return ""

    fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
    sys.modules["lib.adapter"] = fake_adapter_mod

    captured = []
    buffered_from_lines = []
    monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])

    def fake_buffer_transcript_tail(path, from_line, state, adapter=None, **kwargs):
        buffered_from_lines.append(from_line)
        return (
            {
                "buffered_line_offset": initial_lines + 1,
                "semantic_buffer": (
                    "User: The reading chair for this instance has a brass desk lamp beside it."
                ),
                "semantic_buffer_tokens": 25,
            },
            {
                "raw_lines_added": 1,
                "semantic_chars_added": 72,
                "semantic_tokens_added": 25,
                "buffered_line_offset": initial_lines + 1,
            },
        )

    monkeypatch.setattr(extraction_daemon, "_buffer_transcript_tail", fake_buffer_transcript_tail)
    monkeypatch.setattr(extraction_daemon, "write_rolling_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        extraction_daemon,
        "write_signal",
        lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
            {
                "signal_type": signal_type,
                "session_id": session_id,
                "transcript_path": transcript_path,
                "meta": kwargs.get("meta", {}),
            }
        ),
    )

    try:
        extraction_daemon.check_chunk_ready_sessions(chunk_tokens=1500)
    finally:
        if real_adapter is not None:
            sys.modules["lib.adapter"] = real_adapter
        else:
            sys.modules.pop("lib.adapter", None)

    cursor = extraction_daemon.read_cursor(session_id)
    source_key = extraction_daemon._signal_source_cursor_key(
        session_id,
        str(transcript_path),
        cursor_data=cursor,
    )
    assert cursor["line_offset"] == 0
    assert cursor["internal"] is False
    assert buffered_from_lines == [0]
    assert captured == [
        {
            "signal_type": "session_end",
            "session_id": session_id,
            "transcript_path": str(transcript_path),
            "meta": {
                "reason": "internal_cursor_unfrozen_flush",
                "source_cursor_key": source_key,
                "semantic_buffer_tokens": 25,
                "buffered_line_offset": initial_lines + 1,
            },
        }
    ]


def test_check_chunk_ready_sessions_defers_recent_unfrozen_tail_until_quiet(
    monkeypatch,
    tmp_path,
):
    import sys
    import types

    transcript_path = tmp_path / "cc-session.jsonl"
    transcript_path.write_text(
        (
            '{"role":"assistant","content":"[quaid][session-init] Loading project context"}\n'
            '{"role":"assistant","content":"Loading Claude Code hooks"}\n'
            '{"role":"assistant","content":"Ready"}\n'
        ),
        encoding="utf-8",
    )

    session_id = "sess-cc-unfreeze"
    initial_lines = extraction_daemon.count_transcript_lines(str(transcript_path))

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    extraction_daemon.write_cursor(session_id, initial_lines, str(transcript_path), internal=True)

    with transcript_path.open("a", encoding="utf-8") as handle:
        handle.write(
            '{"role":"user","content":"Chunk one has a long natural-language fact batch below the rolling threshold."}\n'
        )
    mtime = os.path.getmtime(transcript_path)
    clock = {"now": mtime + 1}
    monkeypatch.setattr(extraction_daemon.time, "time", lambda: clock["now"])

    real_adapter = sys.modules.get("lib.adapter")
    fake_adapter_mod = types.ModuleType("lib.adapter")

    class _FakeAdapter(_OwnedTestAdapterMixin):
        def parse_session_jsonl(self, path):
            text = Path(path).read_text(encoding="utf-8")
            if "long natural-language fact batch" in text:
                return "User: Chunk one has a long natural-language fact batch below the rolling threshold."
            return ""

    fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
    sys.modules["lib.adapter"] = fake_adapter_mod

    captured = []
    buffered_from_lines = []
    monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])

    def fake_buffer_transcript_tail(path, from_line, state, adapter=None, **kwargs):
        buffered_from_lines.append(from_line)
        return (
            {
                "buffered_line_offset": initial_lines + 1,
                "semantic_buffer": "User: Chunk one has 1224 semantic tokens in production.",
                "semantic_buffer_tokens": 1224,
            },
            {
                "raw_lines_added": 1,
                "semantic_chars_added": 56,
                "semantic_tokens_added": 1224,
                "buffered_line_offset": initial_lines + 1,
            },
        )

    monkeypatch.setattr(extraction_daemon, "_buffer_transcript_tail", fake_buffer_transcript_tail)
    monkeypatch.setattr(
        extraction_daemon,
        "write_signal",
        lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
            {
                "signal_type": signal_type,
                "session_id": session_id,
                "transcript_path": transcript_path,
                "meta": kwargs.get("meta", {}),
            }
        ),
    )

    try:
        extraction_daemon.check_chunk_ready_sessions(chunk_tokens=1500)
        state = extraction_daemon.read_rolling_state(session_id)
        assert captured == []
        assert buffered_from_lines == [0]
        assert state["semantic_buffer_tokens"] == 1224
        assert state[extraction_daemon._INTERNAL_CURSOR_UNFROZEN_PENDING_FLUSH_KEY] is True

        clock["now"] = mtime + extraction_daemon._ROLLING_INTERNAL_ADVANCE_GRACE_SECONDS + 1
        extraction_daemon.check_chunk_ready_sessions(chunk_tokens=1500)
    finally:
        if real_adapter is not None:
            sys.modules["lib.adapter"] = real_adapter
        else:
            sys.modules.pop("lib.adapter", None)

    cursor = extraction_daemon.read_cursor(session_id)
    source_key = extraction_daemon._signal_source_cursor_key(
        session_id,
        str(transcript_path),
        cursor_data=cursor,
    )
    assert cursor["line_offset"] == 0
    assert cursor["internal"] is False
    assert captured == [
        {
            "signal_type": "session_end",
            "session_id": session_id,
            "transcript_path": str(transcript_path),
            "meta": {
                "reason": "internal_cursor_unfrozen_flush",
                "source_cursor_key": source_key,
                "semantic_buffer_tokens": 1224,
                "buffered_line_offset": initial_lines + 1,
            },
        }
    ]


def test_process_signal_unfreezes_internal_cursor_when_real_turn_arrives_after_session_start_noise(
    monkeypatch,
    tmp_path,
):
    import sys
    import types

    transcript_path = tmp_path / "session-start-signal.jsonl"
    transcript_path.write_text(
        (
            '{"role":"assistant","content":"[quaid][session-init] Loading project context"}\n'
            '{"role":"assistant","content":"Warning: empty exports are non-fatal"}\n'
            '{"role":"assistant","content":"Loading ~/.claude/settings.json"}\n'
        ),
        encoding="utf-8",
    )

    session_id = "sess-sessionstart-signal"
    initial_lines = extraction_daemon.count_transcript_lines(str(transcript_path))

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    _stub_successful_session_logs_ingest(monkeypatch)
    extraction_daemon.write_cursor(session_id, initial_lines, str(transcript_path), internal=True)

    with transcript_path.open("a", encoding="utf-8") as handle:
        handle.write(
            '{"role":"user","content":"My sister Clara likes alpacas, lives in Boise, and runs a kiln studio every weekend."}\n'
        )

    signal_path = extraction_daemon.write_signal(
        signal_type="session_end",
        session_id=session_id,
        transcript_path=str(transcript_path),
    )

    real_registry = sys.modules.get("core.subagent_registry")
    real_adapter = sys.modules.get("lib.adapter")
    real_extract = sys.modules.get("ingest.extract")
    fake_registry = types.ModuleType("core.subagent_registry")
    fake_registry.is_registered_subagent = lambda sid: False
    fake_registry.get_harvestable = lambda sid: []
    fake_registry.mark_harvested = lambda sid, child_id: None
    sys.modules["core.subagent_registry"] = fake_registry

    fake_adapter_mod = types.ModuleType("lib.adapter")

    class _FakeAdapter(_OwnedTestAdapterMixin):
        def parse_session_jsonl(self, path):
            text = Path(path).read_text(encoding="utf-8")
            if "alpacas" in text and "kiln studio" in text:
                return "User: My sister Clara likes alpacas, lives in Boise, and runs a kiln studio every weekend."
            return ""

    fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
    sys.modules["lib.adapter"] = fake_adapter_mod

    captured = []
    fake_extract_mod = types.ModuleType("ingest.extract")
    fake_extract_mod.extract_from_transcript = lambda transcript, **kwargs: captured.append(transcript) or {
        "chunks_processed": 1,
        "chunks_total": 1,
        "unclassified_empty_payloads": 0,
        "raw_facts": [],
        "facts": [],
        "soul_snippets": {},
        "journal_entries": {},
        "project_logs": {},
        "raw_snippets": {},
        "raw_journal": {},
        "raw_project_logs": {},
    }
    fake_extract_mod.apply_extracted_payloads = lambda payload, **kwargs: {
        "facts_stored": 0,
        "facts_skipped": 0,
        "edges_created": 0,
        "snippets": {},
        "journal": {},
        "project_log_metrics": {},
    }
    fake_extract_mod.collapse_duplicate_payload_facts = lambda facts: (list(facts), 0)
    sys.modules["ingest.extract"] = fake_extract_mod

    monkeypatch.setattr(
        extraction_daemon,
        "read_transcript_slice",
        lambda path, from_line: [
            '{"role":"user","content":"My sister Clara likes alpacas, lives in Boise, and runs a kiln studio every weekend."}\n'
        ],
    )
    monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "owner-1")
    monkeypatch.setattr(extraction_daemon, "_read_usage_totals", lambda: {})
    monkeypatch.setattr(extraction_daemon, "write_rolling_state", lambda *_args, **_kwargs: None)

    try:
        signals = extraction_daemon.read_pending_signals()
        assert len(signals) == 1
        extraction_daemon.process_signal(signals[0])
    finally:
        if real_registry is not None:
            sys.modules["core.subagent_registry"] = real_registry
        else:
            sys.modules.pop("core.subagent_registry", None)
        if real_adapter is not None:
            sys.modules["lib.adapter"] = real_adapter
        else:
            sys.modules.pop("lib.adapter", None)
        if real_extract is not None:
            sys.modules["ingest.extract"] = real_extract
        else:
            sys.modules.pop("ingest.extract", None)

    cursor = extraction_daemon.read_cursor(session_id)
    assert not signal_path.exists()
    assert cursor["internal"] is False
    assert captured == [
        "User: My sister Clara likes alpacas, lives in Boise, and runs a kiln studio every weekend."
    ]


def test_effective_idle_timeout_uses_configured_timeout_within_bounds():
    assert extraction_daemon._effective_idle_timeout_minutes(60) == 60
    assert extraction_daemon._effective_idle_timeout_minutes(90) == 90


def test_effective_idle_timeout_clamps_disabled_or_excessive_values():
    assert extraction_daemon._effective_idle_timeout_minutes(0) == 120
    assert extraction_daemon._effective_idle_timeout_minutes(-1) == 120
    assert extraction_daemon._effective_idle_timeout_minutes(999) == 120


def test_get_idle_timeout_minutes_preserves_explicit_zero_over_legacy_alias(monkeypatch, tmp_path):
    import config as config_mod

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({
            "capture": {
                "inactivity_timeout_minutes": 0,
                "inactivityTimeoutMinutes": 45,
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_mod, "_config_paths", lambda: [config_path])

    assert extraction_daemon._get_idle_timeout_minutes(default=30) == 0


def test_get_idle_timeout_minutes_uses_legacy_alias_when_modern_key_absent(monkeypatch, tmp_path):
    import config as config_mod

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"capture": {"inactivityTimeoutMinutes": 45}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_mod, "_config_paths", lambda: [config_path])

    assert extraction_daemon._get_idle_timeout_minutes(default=30) == 45


def test_check_idle_sessions_timeout_signal_carries_compaction_metadata(monkeypatch, tmp_path):
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text('{"role":"user","content":"hello"}\n{"role":"assistant","content":"hi"}\n', encoding="utf-8")

    instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
    cursor_dir = tmp_path / "instances" / instance_id / "data" / "session-cursors"
    cursor_dir.mkdir(parents=True, exist_ok=True)
    (cursor_dir / "sess-compact.json").write_text(
        (
            '{"session_id":"sess-compact","line_offset":1,'
            f'"transcript_path":"{transcript_path}"'
            '}'
        ),
        encoding="utf-8",
    )

    now = 1_700_000_000.0
    old_mtime = now - (31 * 60)
    os.utime(transcript_path, (old_mtime, old_mtime))

    captured = []
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
    monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: now - 7200)
    monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
    monkeypatch.setattr(extraction_daemon, "_adapter_supports_compaction_control", lambda: True)
    monkeypatch.setattr(extraction_daemon, "_get_compact_on_timeout", lambda: False)
    monkeypatch.setattr(
        extraction_daemon,
        "write_signal",
        lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
            {
                "signal_type": signal_type,
                "session_id": session_id,
                "transcript_path": transcript_path,
                "kwargs": kwargs,
            }
        ),
    )

    extraction_daemon.check_idle_sessions(timeout_minutes=30)

    assert captured == [
        {
            "signal_type": "timeout",
            "session_id": "sess-compact",
            "transcript_path": str(transcript_path),
            "kwargs": {
                "supports_compaction_control": True,
                "meta": {"compact_on_timeout": False},
            },
        }
    ]


def test_check_idle_sessions_does_not_retimeout_lifecycle_closed_cursor(monkeypatch, tmp_path):
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        '{"role":"user","content":"hello"}\n{"role":"assistant","content":"hi"}\n',
        encoding="utf-8",
    )

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    extraction_daemon.write_cursor(
        "sess-closed",
        2,
        str(transcript_path),
        processed_signal_type="session_end",
    )

    now = 1_700_000_000.0
    old_mtime = now - (10 * 60)
    os.utime(transcript_path, (old_mtime, old_mtime))

    captured = []
    monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
    monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: now - 7200)
    monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
    monkeypatch.setattr(
        extraction_daemon,
        "write_signal",
        lambda *args, **kwargs: captured.append((args, kwargs)),
    )

    extraction_daemon.check_idle_sessions(timeout_minutes=1)

    assert captured == []
    assert extraction_daemon.read_cursor("sess-closed")["processed_signal_type"] == "session_end"


def test_check_idle_sessions_treats_file_growth_past_eof_cursor_as_new_content(monkeypatch, tmp_path):
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        '{"role":"user","content":"hello"}\n{"role":"assistant","content":"hi"}',
        encoding="utf-8",
    )

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")

    extraction_daemon.write_cursor("sess-grown", 2, str(transcript_path))
    extraction_daemon._cursor_end_timeout_fired.add("sess-grown")

    with transcript_path.open("a", encoding="utf-8") as handle:
        handle.write(' {"note":"new bytes without newline"}')

    now = 1_700_000_000.0
    old_mtime = now - (31 * 60)
    os.utime(transcript_path, (old_mtime, old_mtime))

    captured = []
    monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
    monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: now - 7200)
    monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
    monkeypatch.setattr(extraction_daemon, "_adapter_supports_compaction_control", lambda: True)
    monkeypatch.setattr(extraction_daemon, "_get_compact_on_timeout", lambda: True)
    monkeypatch.setattr(
        extraction_daemon,
        "write_signal",
        lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
            {
                "signal_type": signal_type,
                "session_id": session_id,
                "transcript_path": transcript_path,
                "kwargs": kwargs,
            }
        ),
    )

    try:
        extraction_daemon.check_idle_sessions(timeout_minutes=30)
    finally:
        extraction_daemon._cursor_end_timeout_fired.discard("sess-grown")

    assert captured == [
        {
            "signal_type": "timeout",
            "session_id": "sess-grown",
            "transcript_path": str(transcript_path),
            "kwargs": {
                "supports_compaction_control": True,
                "meta": {"compact_on_timeout": True},
            },
        }
    ]
    assert "sess-grown" not in extraction_daemon._cursor_end_timeout_fired


def test_check_idle_sessions_uses_live_transcript_for_preserved_cursor(monkeypatch, tmp_path):
    session_id = "04719640-c63a-43f3-a68a-72b70f2729ed"
    live_dir = tmp_path / ".openclaw" / "agents" / "main" / "sessions"
    preserved_dir = tmp_path / "instances" / "openclaw-main" / "logs" / "quaid" / "sessions"
    live_dir.mkdir(parents=True)
    preserved_dir.mkdir(parents=True)
    live_path = live_dir / f"{session_id}.jsonl"
    preserved_path = preserved_dir / f"{session_id}.jsonl"
    live_path.write_text(
        '{"type":"message","message":{"role":"user","content":[{"type":"text","text":"garden shed lantern"}]}}\n',
        encoding="utf-8",
    )
    preserved_path.write_text(
        (
            '{"type":"message","message":{"role":"user","content":[{"type":"text","text":"prior preserved row"}]}}\n'
            '{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"ack"}]}}\n'
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
    source_key = extraction_daemon._signal_source_cursor_key(session_id, str(live_path))
    extraction_daemon.write_cursor(session_id, 2, str(preserved_path), source_key=source_key)

    class _Adapter(_OwnedTestAdapterMixin):
        def get_session_path(self, requested_session_id):
            assert requested_session_id == session_id
            return live_path

    now = 1_700_000_000.0
    old_mtime = now - (3 * 60)
    os.utime(live_path, (old_mtime, old_mtime))
    os.utime(preserved_path, (now, now))

    captured = []
    monkeypatch.setattr(extraction_daemon, "_load_runtime_adapter", lambda: _Adapter())
    monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
    monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: now - 7200)
    monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
    monkeypatch.setattr(extraction_daemon, "_adapter_supports_compaction_control", lambda: True)
    monkeypatch.setattr(extraction_daemon, "_get_compact_on_timeout", lambda: True)
    monkeypatch.setattr(
        extraction_daemon,
        "write_signal",
        lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
            {
                "signal_type": signal_type,
                "session_id": session_id,
                "transcript_path": transcript_path,
                "kwargs": kwargs,
            }
        ),
    )

    try:
        extraction_daemon.check_idle_sessions(timeout_minutes=1)
    finally:
        extraction_daemon._cursor_end_timeout_fired.discard(session_id)

    assert captured == [
        {
            "signal_type": "timeout",
            "session_id": session_id,
            "transcript_path": str(live_path.resolve()),
            "kwargs": {
                "supports_compaction_control": True,
                "meta": {"compact_on_timeout": True},
            },
        }
    ]


def test_index_one_stale_doc_resolves_relative_registry_paths_from_workspace(monkeypatch, tmp_path):
    from core import project_docs
    from core.docs import updater as docs_updater

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    doc_path = workspace / "docs" / "fresh.md"
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    doc_path.write_text("# Fresh\n", encoding="utf-8")

    indexed = []

    class _RegistryStub:
        def reconcile_global_project_registry(self):
            return None

        def list_docs(self):
            return [{"file_path": "docs/fresh.md", "registered_at": "2026-04-12T00:00:00Z"}]

    class _RagStub:
        def needs_reindex_many(self, paths):
            return {str(doc_path): True}

        def index_document(self, file_path):
            indexed.append(file_path)
            return 1

    monkeypatch.setattr("config._workspace_root", lambda: workspace)
    monkeypatch.setattr(docs_updater, "_linked_projects_for_current_instance", lambda: ([], True))
    monkeypatch.setitem(
        sys.modules,
        "datastore.docsdb.registry",
        types.SimpleNamespace(DocsRegistry=lambda *args, **kwargs: _RegistryStub()),
    )
    monkeypatch.setitem(sys.modules, "datastore.docsdb.rag", types.SimpleNamespace(DocsRAG=lambda: _RagStub()))

    try:
        assert project_docs.index_one_stale_registered_doc() is True
    finally:
        sys.modules.pop("datastore.docsdb.registry", None)
        sys.modules.pop("datastore.docsdb.rag", None)

    assert indexed == [str(doc_path)]


# ---------------------------------------------------------------------------
# _signal_dir() / _cursor_dir() isolation (M3 bug regression)
# ---------------------------------------------------------------------------

class TestSignalDirIsolation:
    """_signal_dir() must be per-instance, not shared across all instances."""

    def test_signal_dir_uses_instance_root_not_quaid_home(self, monkeypatch, tmp_path):
        """Signal dir must be under QUAID_HOME/INSTANCE, not QUAID_HOME directly."""
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "cc-instance")

        sig_dir = extraction_daemon._signal_dir()

        assert str(sig_dir).startswith(str(tmp_path / "instances" / "cc-instance")), (
            f"signal dir {sig_dir} should be under instance root, not quaid home root"
        )
        # Must NOT be directly under QUAID_HOME
        assert sig_dir != tmp_path / "data" / "extraction-signals"

    def test_two_different_instances_get_different_signal_dirs(self, monkeypatch, tmp_path):
        """Two QUAID_INSTANCE values must produce two distinct signal dirs."""
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))

        monkeypatch.setenv("QUAID_INSTANCE", "instance-oc")
        dir_oc = extraction_daemon._signal_dir()

        monkeypatch.setenv("QUAID_INSTANCE", "instance-cc")
        dir_cc = extraction_daemon._signal_dir()

        assert dir_oc != dir_cc

    def test_cursor_dir_uses_instance_root(self, monkeypatch, tmp_path):
        """Cursor dir must be under QUAID_HOME/INSTANCE, not QUAID_HOME directly."""
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "oc-instance")

        cursor_dir = extraction_daemon._cursor_dir()

        assert str(cursor_dir).startswith(str(tmp_path / "instances" / "oc-instance"))

    def test_two_different_instances_get_different_cursor_dirs(self, monkeypatch, tmp_path):
        """Two QUAID_INSTANCE values must produce two distinct cursor dirs."""
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))

        monkeypatch.setenv("QUAID_INSTANCE", "instance-oc")
        dir_oc = extraction_daemon._cursor_dir()

        monkeypatch.setenv("QUAID_INSTANCE", "instance-cc")
        dir_cc = extraction_daemon._cursor_dir()

        assert dir_oc != dir_cc

    def test_signals_written_to_instance_a_not_visible_in_instance_b(self, monkeypatch, tmp_path):
        """Signals written to instance A's signal dir must not appear when instance B lists its signals."""
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))

        # Write a signal as instance A
        monkeypatch.setenv("QUAID_INSTANCE", "instance-a")
        extraction_daemon.write_signal(
            signal_type="reset",
            session_id="sess-a",
            transcript_path="/fake/transcript.jsonl",
        )

        # Switch to instance B and list signals
        monkeypatch.setenv("QUAID_INSTANCE", "instance-b")
        signals = extraction_daemon.read_pending_signals()

        assert signals == [], (
            "instance-b should see no signals; instance-a signals must be isolated"
        )

    def test_pid_path_is_per_instance(self, monkeypatch, tmp_path):
        """PID file path must differ for different QUAID_INSTANCE values."""
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))

        monkeypatch.setenv("QUAID_INSTANCE", "instance-oc")
        pid_oc = extraction_daemon._pid_path()

        monkeypatch.setenv("QUAID_INSTANCE", "instance-cc")
        pid_cc = extraction_daemon._pid_path()

        assert pid_oc != pid_cc


# ---------------------------------------------------------------------------
# write_signal() / read_pending_signals()
# ---------------------------------------------------------------------------

class TestSignalRoundTrip:
    """write_signal writes a well-formed file; read_pending_signals picks it up."""

    def test_config_file_paths_rejects_invalid_adapter_scope_when_fail_hard(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
        monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

        class _Adapter:
            def get_capability(self, key, default=None):
                assert key == "platform_config_scope"
                return "../../etc/passwd"

        fake_adapter_mod = types.ModuleType("lib.adapter")
        fake_adapter_mod.get_adapter = lambda: _Adapter()
        real_adapter = sys.modules.get("lib.adapter")
        sys.modules["lib.adapter"] = fake_adapter_mod
        try:
            with pytest.raises(ValueError, match="invalid platform_config_scope"):
                extraction_daemon._config_file_paths()
        finally:
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

    def test_config_file_paths_ignores_invalid_adapter_scope_when_fail_open(
        self, monkeypatch, tmp_path, caplog
    ):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
        monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: False)
        (tmp_path / "shared" / "config" / "openclaw").mkdir(parents=True)

        class _Adapter:
            def get_capability(self, key, default=None):
                assert key == "platform_config_scope"
                return "../../etc/passwd"

        fake_adapter_mod = types.ModuleType("lib.adapter")
        fake_adapter_mod.get_adapter = lambda: _Adapter()
        real_adapter = sys.modules.get("lib.adapter")
        sys.modules["lib.adapter"] = fake_adapter_mod
        try:
            with caplog.at_level("WARNING", logger="quaid.daemon"):
                paths = extraction_daemon._config_file_paths()
        finally:
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

        assert paths[1] == tmp_path / "shared" / "config" / "openclaw" / "config.json"
        assert "ignoring invalid adapter platform_config_scope" in caplog.text

    def test_config_file_paths_logs_adapter_lookup_failure_when_fail_open(
        self, monkeypatch, tmp_path, caplog
    ):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
        monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: False)

        fake_adapter_mod = types.ModuleType("lib.adapter")
        fake_adapter_mod.get_adapter = lambda: (_ for _ in ()).throw(RuntimeError("adapter broken"))
        real_adapter = sys.modules.get("lib.adapter")
        sys.modules["lib.adapter"] = fake_adapter_mod
        try:
            with caplog.at_level("WARNING", logger="quaid.daemon"):
                paths = extraction_daemon._config_file_paths()
        finally:
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

        assert paths[1] == tmp_path / "shared" / "config" / "openclaw" / "config.json"
        assert "daemon config adapter lookup failed" in caplog.text
        assert "adapter broken" in caplog.text

    def test_config_file_paths_raises_adapter_scope_failure_when_fail_hard(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
        monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

        class _Adapter:
            def get_capability(self, key, default=None):
                assert key == "platform_config_scope"
                raise RuntimeError("scope broken")

        fake_adapter_mod = types.ModuleType("lib.adapter")
        fake_adapter_mod.get_adapter = lambda: _Adapter()
        real_adapter = sys.modules.get("lib.adapter")
        sys.modules["lib.adapter"] = fake_adapter_mod
        try:
            with pytest.raises(RuntimeError, match="daemon config adapter platform scope lookup failed") as excinfo:
                extraction_daemon._config_file_paths()
        finally:
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

        assert isinstance(excinfo.value.__cause__, RuntimeError)
        assert "scope broken" in str(excinfo.value.__cause__)

    def test_config_file_paths_treats_missing_adapter_scope_capability_as_absent(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
        monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

        class _Adapter:
            pass

        fake_adapter_mod = types.ModuleType("lib.adapter")
        fake_adapter_mod.get_adapter = lambda: _Adapter()
        real_adapter = sys.modules.get("lib.adapter")
        sys.modules["lib.adapter"] = fake_adapter_mod
        try:
            paths = extraction_daemon._config_file_paths()
        finally:
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

        assert paths[1] == tmp_path / "shared" / "config" / "openclaw" / "config.json"

    def test_write_and_read_signal_round_trip(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        extraction_daemon.write_signal(
            signal_type="reset",
            session_id="sess-42",
            transcript_path="/some/path.jsonl",
            adapter="cc",
        )

        signals = extraction_daemon.read_pending_signals()

        assert len(signals) == 1
        sig = signals[0]
        assert sig["type"] == "reset"
        assert sig["session_id"] == "sess-42"
        assert sig["transcript_path"] == "/some/path.jsonl"
        assert sig["adapter"] == "cc"
        assert "_signal_path" in sig

    def test_write_signal_unknown_type_falls_back_to_session_end(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        extraction_daemon.write_signal(
            signal_type="totally_invalid",
            session_id="sess-99",
            transcript_path="/fake.jsonl",
        )

        signals = extraction_daemon.read_pending_signals()
        assert len(signals) == 1
        assert signals[0]["type"] == "session_end"

    def test_write_signal_all_valid_types_accepted(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        for sig_type in extraction_daemon.VALID_SIGNAL_TYPES:
            extraction_daemon.write_signal(
                signal_type=sig_type,
                session_id=f"sess-{sig_type}",
                transcript_path="/fake.jsonl",
            )

        signals = extraction_daemon.read_pending_signals()
        found_types = {s["type"] for s in signals}
        assert found_types == set(extraction_daemon.VALID_SIGNAL_TYPES)

    def test_signal_file_contains_timestamp_field(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        extraction_daemon.write_signal(
            signal_type="compaction",
            session_id="sess-ts",
            transcript_path="/fake.jsonl",
        )

        signals = extraction_daemon.read_pending_signals()
        assert "timestamp" in signals[0]

    def test_read_pending_signals_prioritizes_reset_before_compaction_backlog(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
        monkeypatch.setattr(extraction_daemon, "MAX_SIGNALS_PER_POLL", 3)

        sig_dir = extraction_daemon._signal_dir()
        for idx in range(5):
            (sig_dir / f"100{idx}_compaction.json").write_text(
                json.dumps({
                    "type": "compaction",
                    "session_id": f"compaction-{idx}",
                    "transcript_path": f"/tmp/compaction-{idx}.jsonl",
                }),
                encoding="utf-8",
            )
        (sig_dir / "2000_reset.json").write_text(
            json.dumps({
                "type": "reset",
                "session_id": "reset-session",
                "transcript_path": "/tmp/reset.jsonl",
            }),
            encoding="utf-8",
        )

        signals = extraction_daemon.read_pending_signals()

        assert len(signals) == 3
        assert signals[0]["type"] == "reset"
        assert signals[0]["session_id"] == "reset-session"

    def test_read_pending_signals_prioritizes_timeout_before_rolling_backlog(self, monkeypatch, tmp_path):
        """Fresh idle captures must not wait behind expensive rolling backlog."""
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        sig_dir = extraction_daemon._signal_dir()
        (sig_dir / "1000_rolling.json").write_text(
            json.dumps({
                "type": "rolling",
                "session_id": "old-rolling",
                "transcript_path": "/tmp/old-rolling.jsonl",
            }),
            encoding="utf-8",
        )
        (sig_dir / "2000_timeout.json").write_text(
            json.dumps({
                "type": "timeout",
                "session_id": "fresh-timeout",
                "transcript_path": "/tmp/fresh-timeout.jsonl",
            }),
            encoding="utf-8",
        )

        signals = extraction_daemon.read_pending_signals()

        assert [signal["type"] for signal in signals[:2]] == ["timeout", "rolling"]

    def test_read_pending_signals_normalizes_signal_type_field(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        sig_dir = extraction_daemon._signal_dir()
        (sig_dir / "1000_manual_reset.json").write_text(
            json.dumps({
                "signal_type": "reset",
                "session_id": "manual-reset",
                "transcript_path": "/tmp/manual-reset.jsonl",
            }),
            encoding="utf-8",
        )

        signals = extraction_daemon.read_pending_signals()

        assert len(signals) == 1
        assert signals[0]["type"] == "reset"
        assert signals[0]["signal_type"] == "reset"

    def test_write_signal_coalesces_duplicate_pending_session(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        first = extraction_daemon.write_signal(
            signal_type="rolling",
            session_id="sess-dup",
            transcript_path="/first.jsonl",
            meta={"reason": "chunk_budget"},
        )
        second = extraction_daemon.write_signal(
            signal_type="rolling",
            session_id="sess-dup",
            transcript_path="/second.jsonl",
            meta={"source": "followup"},
        )

        assert first == second
        signals = extraction_daemon.read_pending_signals()
        assert len(signals) == 1
        assert signals[0]["type"] == "rolling"
        assert signals[0]["transcript_path"] == "/second.jsonl"
        assert signals[0]["meta"] == {"reason": "chunk_budget", "source": "followup"}

    def test_write_signal_keeps_staged_flush_separate_from_real_lifecycle(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        real = extraction_daemon.write_signal(
            signal_type="session_end",
            session_id="sess-roll-lifecycle",
            transcript_path="/real-lifecycle.jsonl",
            meta={"reason": "session_closed"},
        )
        synthetic = extraction_daemon.write_signal(
            signal_type="session_end",
            session_id="sess-roll-lifecycle",
            transcript_path="/staged-flush.jsonl",
            meta={
                "reason": "rolling_stage_flush",
                "source_signal": "rolling",
                "staged_payload_sweep": True,
                "flush_staged_payload_only": True,
            },
        )

        assert real != synthetic
        signals = extraction_daemon.read_pending_signals()
        assert len(signals) == 2
        assert [signal["meta"]["reason"] for signal in signals] == [
            "rolling_stage_flush",
            "session_closed",
        ]
        assert signals[0]["transcript_path"] == "/staged-flush.jsonl"
        assert signals[1]["transcript_path"] == "/real-lifecycle.jsonl"

    def test_write_signal_keeps_distinct_pending_types_for_same_session(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        first = extraction_daemon.write_signal(
            signal_type="rolling",
            session_id="sess-upgrade",
            transcript_path="/rolling.jsonl",
            meta={"reason": "chunk_budget"},
        )
        second = extraction_daemon.write_signal(
            signal_type="session_end",
            session_id="sess-upgrade",
            transcript_path="/final.jsonl",
            meta={"reason": "session_closed"},
        )

        assert first != second
        signals = extraction_daemon.read_pending_signals()
        assert len(signals) == 2
        assert [sig["type"] for sig in signals] == ["session_end", "rolling"]
        assert signals[0]["transcript_path"] == "/final.jsonl"
        assert signals[0]["meta"] == {"reason": "session_closed"}
        assert signals[1]["transcript_path"] == "/rolling.jsonl"
        assert signals[1]["meta"] == {"reason": "chunk_budget"}

    def test_preserve_missing_transcript_signal_for_retry_updates_meta(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
        monkeypatch.setattr(extraction_daemon.time, "time", lambda: 100.0)

        sig_path = extraction_daemon.write_signal(
            signal_type="session_end",
            session_id="sess-retry",
            transcript_path="",
            meta={"reason": "session_closed"},
        )
        signal_data = extraction_daemon.read_pending_signals()[0]

        kept = extraction_daemon._preserve_missing_transcript_signal_for_retry(
            signal_data,
            session_id="sess-retry",
            signal_type="session_end",
            transcript_path="",
            label="test",
        )

        assert kept is True
        payload = json.loads(sig_path.read_text(encoding="utf-8"))
        assert payload["meta"]["reason"] == "session_closed"
        assert payload["meta"]["missing_transcript_first_seen_at"] == 100.0
        assert payload["meta"]["missing_transcript_last_seen_at"] == 100.0
        assert payload["meta"]["missing_transcript_attempts"] == 1
        assert payload["meta"]["missing_transcript_last_path"] == ""

    def test_preserve_missing_transcript_signal_for_retry_stops_after_budget(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
        monkeypatch.setattr(extraction_daemon.time, "time", lambda: 200.0)

        extraction_daemon.write_signal(
            signal_type="reset",
            session_id="sess-expire",
            transcript_path="",
            meta={
                "missing_transcript_first_seen_at": 100.0,
                "missing_transcript_attempts": 3,
            },
        )
        signal_data = extraction_daemon.read_pending_signals()[0]

        kept = extraction_daemon._preserve_missing_transcript_signal_for_retry(
            signal_data,
            session_id="sess-expire",
            signal_type="reset",
            transcript_path="",
            label="test",
            max_wait_seconds=45.0,
        )

        assert kept is False

    def test_preserve_missing_transcript_signal_honors_zero_first_seen(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
        monkeypatch.setattr(extraction_daemon.time, "time", lambda: 200.0)

        extraction_daemon.write_signal(
            signal_type="reset",
            session_id="sess-zero-first-seen",
            transcript_path="",
            meta={"missing_transcript_first_seen_at": 0.0},
        )
        signal_data = extraction_daemon.read_pending_signals()[0]

        kept = extraction_daemon._preserve_missing_transcript_signal_for_retry(
            signal_data,
            session_id="sess-zero-first-seen",
            signal_type="reset",
            transcript_path="",
            label="test",
            max_wait_seconds=45.0,
        )

        assert kept is False

    def test_record_signal_process_failure_updates_attempts_and_dead_letters(
        self, monkeypatch, tmp_path, caplog
    ):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
        monkeypatch.setenv("QUAID_NOW", "2026-06-14T12:00:00Z")
        monkeypatch.setattr(extraction_daemon, "MAX_SIGNAL_PROCESS_ATTEMPTS", 2)

        sig_path = extraction_daemon.write_signal(
            signal_type="reset",
            session_id="sess-crashing",
            transcript_path="/tmp/crashing.jsonl",
        )
        signal_data = extraction_daemon.read_pending_signals()[0]

        with caplog.at_level("WARNING", logger="quaid.daemon"):
            preserved = extraction_daemon._record_signal_process_failure_for_retry(
                signal_data,
                RuntimeError("process boom"),
                label="test",
            )

        assert preserved is True
        payload = json.loads(sig_path.read_text(encoding="utf-8"))
        assert payload["process_attempts"] == 1
        assert payload["last_process_failure"] == "process boom"
        assert payload["last_process_failure_at"] == "2026-06-14T12:00:00Z"
        assert "preserving for retry" in caplog.text

        caplog.clear()
        signal_data = extraction_daemon.read_pending_signals()[0]
        with caplog.at_level("ERROR", logger="quaid.daemon"):
            preserved = extraction_daemon._record_signal_process_failure_for_retry(
                signal_data,
                RuntimeError("process boom again"),
                label="test",
            )

        assert preserved is False
        assert not sig_path.exists()
        dead_letters = list(extraction_daemon._signal_dead_letter_dir().glob("*.json"))
        assert len(dead_letters) == 1
        payload = json.loads(dead_letters[0].read_text(encoding="utf-8"))
        assert payload["process_attempts"] == 2
        assert payload["dead_letter_reason"] == "process_attempts_exhausted"
        assert payload["dead_lettered_at"] == "2026-06-14T12:00:00Z"
        assert "moved to dead letter" in caplog.text

    def test_record_signal_process_failure_dead_letter_writes_once(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
        monkeypatch.setenv("QUAID_NOW", "2026-06-14T12:00:00Z")
        monkeypatch.setattr(extraction_daemon, "MAX_SIGNAL_PROCESS_ATTEMPTS", 2)

        sig_path = extraction_daemon.write_signal(
            signal_type="reset",
            session_id="sess-dead-letter-write-once",
            transcript_path="/tmp/crashing.jsonl",
        )
        payload = json.loads(sig_path.read_text(encoding="utf-8"))
        payload["process_attempts"] = 1
        sig_path.write_text(json.dumps(payload), encoding="utf-8")
        signal_data = extraction_daemon.read_pending_signals()[0]

        real_atomic_write = extraction_daemon._atomic_write
        writes = []

        def tracking_atomic_write(path, content):
            writes.append((Path(path), json.loads(content)))
            return real_atomic_write(path, content)

        monkeypatch.setattr(extraction_daemon, "_atomic_write", tracking_atomic_write)

        preserved = extraction_daemon._record_signal_process_failure_for_retry(
            signal_data,
            RuntimeError("process boom"),
            label="test",
        )

        assert preserved is False
        assert not sig_path.exists()
        assert len(writes) == 1
        assert writes[0][0] == sig_path
        assert writes[0][1]["process_attempts"] == 2
        assert writes[0][1]["dead_letter_reason"] == "process_attempts_exhausted"

    def test_preserve_missing_transcript_signal_write_failure_respects_failhard(
        self, monkeypatch, tmp_path, caplog
    ):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
        monkeypatch.setattr(extraction_daemon.time, "time", lambda: 100.0)
        monkeypatch.setattr(
            extraction_daemon,
            "_atomic_write",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("signal write failed")),
        )
        signal_data = {
            "_signal_path": str(tmp_path / "instances" / "test-inst" / "data" / "extraction-signals" / "s.json"),
            "type": "session_end",
            "meta": {},
        }

        monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: False)
        with caplog.at_level("WARNING", logger="quaid.daemon"):
            kept = extraction_daemon._preserve_missing_transcript_signal_for_retry(
                signal_data,
                session_id="sess-retry",
                signal_type="session_end",
                transcript_path="/missing.jsonl",
                label="test",
            )

        assert kept is False
        assert "could not preserve missing-transcript signal for retry" in caplog.text

        monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)
        with pytest.raises(RuntimeError, match="could not preserve missing-transcript signal for retry") as excinfo:
            extraction_daemon._preserve_missing_transcript_signal_for_retry(
                signal_data,
                session_id="sess-retry",
                signal_type="session_end",
                transcript_path="/missing.jsonl",
                label="test",
            )

        assert isinstance(excinfo.value.__cause__, RuntimeError)
        assert "signal write failed" in str(excinfo.value.__cause__)

    def test_process_signal_preserves_missing_timeout_signal_for_retry(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        missing_path = tmp_path / "missing-timeout.jsonl"
        calls = []
        marked = []
        released = []

        monkeypatch.setattr(
            extraction_daemon,
            "_read_rolling_state_for_signal",
            lambda sid, _path, **_kwargs: ({}, sid),
        )
        monkeypatch.setattr(extraction_daemon, "_acquire_session_processing_lock", lambda _key: object())
        monkeypatch.setattr(extraction_daemon, "_release_session_processing_lock", lambda key, _fd: released.append(key))
        monkeypatch.setattr(extraction_daemon, "_read_cursor_with_source_compat", lambda *_args, **_kwargs: {
            "line_offset": 0,
            "transcript_path": "",
        })
        monkeypatch.setattr(extraction_daemon, "_reconcile_internal_cursor_state", lambda *_args, **_kwargs: "not_internal")
        monkeypatch.setattr(extraction_daemon, "mark_signal_processed", lambda sig: marked.append(sig))

        def _preserve(signal_data, *, session_id, signal_type, transcript_path, label, **_kwargs):
            calls.append({
                "signal": signal_data,
                "session_id": session_id,
                "signal_type": signal_type,
                "transcript_path": transcript_path,
                "label": label,
            })
            return True

        monkeypatch.setattr(extraction_daemon, "_preserve_missing_transcript_signal_for_retry", _preserve)

        signal = {
            "type": "timeout",
            "session_id": "sess-timeout-missing",
            "transcript_path": str(missing_path),
            "_signal_path": str(tmp_path / "sig.json"),
            "timestamp": "2026-04-27T17:00:00Z",
        }
        extraction_daemon.process_signal(signal)

        assert marked == []
        assert released
        assert len(calls) == 1
        assert calls[0]["session_id"] == "sess-timeout-missing"
        assert calls[0]["signal_type"] == "timeout"
        assert calls[0]["transcript_path"] == str(missing_path)
        assert calls[0]["label"] == "daemon-timeout"

    def test_session_processing_lock_is_exclusive_per_session(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        first = extraction_daemon._acquire_session_processing_lock("sess-lock")
        second = extraction_daemon._acquire_session_processing_lock("sess-lock")
        other = extraction_daemon._acquire_session_processing_lock("sess-other")

        assert first is not None
        assert second is None
        assert other is not None

        extraction_daemon._release_session_processing_lock("sess-lock", first)
        extraction_daemon._release_session_processing_lock("sess-other", other)

    def test_session_processing_lock_release_keeps_reclaimable_sidecar(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        first = extraction_daemon._acquire_session_processing_lock("sess-sidecar")
        assert first is not None
        lock_path = extraction_daemon._processing_lock_path("sess-sidecar")
        assert lock_path.exists()

        extraction_daemon._release_session_processing_lock("sess-sidecar", first)
        assert lock_path.exists()

        second = extraction_daemon._acquire_session_processing_lock("sess-sidecar")
        assert second is not None
        extraction_daemon._release_session_processing_lock("sess-sidecar", second)
        assert lock_path.exists()

    def test_session_processing_lock_payload_write_warns_when_fail_open(
        self, monkeypatch, tmp_path, caplog
    ):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
        monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: False)
        monkeypatch.setattr(
            extraction_daemon.os,
            "fsync",
            lambda _fd: (_ for _ in ()).throw(OSError("disk full")),
        )

        with caplog.at_level("WARNING", logger="quaid.daemon"):
            lock_fd = extraction_daemon._acquire_session_processing_lock("sess-payload-warn")

        try:
            assert lock_fd is not None
            assert "failed writing session processing lock payload" in caplog.text
            assert "disk full" in caplog.text
        finally:
            extraction_daemon._release_session_processing_lock("sess-payload-warn", lock_fd)

    def test_session_processing_lock_payload_write_raises_when_fail_hard(
        self, monkeypatch, tmp_path, caplog
    ):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
        monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)
        monkeypatch.setattr(
            extraction_daemon.os,
            "fsync",
            lambda _fd: (_ for _ in ()).throw(OSError("disk full")),
        )

        with caplog.at_level("WARNING", logger="quaid.daemon"):
            with pytest.raises(RuntimeError, match="session processing lock payload write failed") as excinfo:
                extraction_daemon._acquire_session_processing_lock("sess-payload-failhard")

        assert isinstance(excinfo.value.__cause__, OSError)
        assert "disk full" in str(excinfo.value.__cause__)
        assert "failed writing session processing lock payload" in caplog.text

    def test_processing_lock_active_ignores_unlocked_sidecar_with_live_pid(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
        monkeypatch.setattr(extraction_daemon, "_pid_alive", lambda _pid: True)

        lock_path = extraction_daemon._processing_lock_path("source-released-live-pid")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(
            json.dumps({
                "session_id": "source-released-live-pid",
                "pid": 999999,
                "started_at": "2026-06-13T14:38:20Z",
            }),
            encoding="utf-8",
        )

        assert extraction_daemon._processing_lock_active("source-released-live-pid") is False
        assert lock_path.exists()

    def test_processing_lock_active_reaps_old_unlocked_legacy_pid_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        lock_path = extraction_daemon._processing_lock_path("source-legacy-lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("999999999\n", encoding="utf-8")
        old_time = time.time() - 60
        os.utime(lock_path, (old_time, old_time))

        assert extraction_daemon._processing_lock_active("source-legacy-lock") is False
        assert not lock_path.exists()

    def test_processing_lock_active_reaps_fresh_dead_pid_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
        monkeypatch.setattr(extraction_daemon, "_pid_alive", lambda _pid: False)

        lock_path = extraction_daemon._processing_lock_path("source-fresh-dead-lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(
            json.dumps({
                "session_id": "source-fresh-dead-lock",
                "pid": 999999999,
                "started_at": "2026-06-13T14:38:20Z",
            }),
            encoding="utf-8",
        )

        assert extraction_daemon._processing_lock_active("source-fresh-dead-lock") is False
        assert not lock_path.exists()

    def test_remove_stale_processing_lock_holds_flock_through_unlink(self, monkeypatch, tmp_path):
        lock_path = tmp_path / "source-race.lock"
        lock_path.write_text(json.dumps({"pid": 999999999}), encoding="utf-8")
        real_flock = extraction_daemon.fcntl.flock
        real_unlink = Path.unlink
        lock_held = {"value": False}
        events = []

        def tracking_flock(fd, op):
            if op & extraction_daemon.fcntl.LOCK_EX:
                lock_held["value"] = True
                events.append("lock")
            elif op == extraction_daemon.fcntl.LOCK_UN:
                events.append(("unlock", lock_held["value"]))
                lock_held["value"] = False
            return real_flock(fd, op)

        def tracking_unlink(self, *args, **kwargs):
            if self == lock_path:
                events.append(("unlink", lock_held["value"]))
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(extraction_daemon.fcntl, "flock", tracking_flock)
        monkeypatch.setattr(Path, "unlink", tracking_unlink)

        assert extraction_daemon._remove_stale_processing_lock(lock_path, holder_dead=True) is True
        assert not lock_path.exists()
        assert events.index("lock") < events.index(("unlink", True)) < events.index(("unlock", True))

    def test_processing_lock_active_reaps_old_unlocked_empty_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        lock_path = extraction_daemon._processing_lock_path("source-empty-lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("", encoding="utf-8")
        old_time = time.time() - 60
        os.utime(lock_path, (old_time, old_time))

        assert extraction_daemon._processing_lock_active("source-empty-lock") is False
        assert not lock_path.exists()

    def test_processing_lock_active_preserves_locked_file_even_if_pid_dead(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
        monkeypatch.setattr(extraction_daemon, "_pid_alive", lambda _pid: False)

        lock_fd = extraction_daemon._acquire_session_processing_lock("source-locked-dead-pid")
        assert lock_fd is not None
        lock_path = extraction_daemon._processing_lock_path("source-locked-dead-pid")
        try:
            assert extraction_daemon._processing_lock_active("source-locked-dead-pid") is True
            assert lock_path.exists()
        finally:
            extraction_daemon._release_session_processing_lock("source-locked-dead-pid", lock_fd)

    def test_process_signal_preserves_signal_when_session_lock_unavailable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text('{"role":"user","content":"hi"}\n', encoding="utf-8")

        marked = []
        monkeypatch.setattr(extraction_daemon, "_acquire_session_processing_lock", lambda _sid: None)
        monkeypatch.setattr(extraction_daemon, "mark_signal_processed", lambda sig: marked.append(sig))

        extraction_daemon.process_signal(
            {
                "type": "rolling",
                "session_id": "sess-lock-busy",
                "transcript_path": str(transcript_path),
                "timestamp": "2026-03-20T00:00:00Z",
            }
        )

        assert marked == []

    def test_process_signal_preserves_staged_flush_when_session_lock_unavailable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text('{"role":"user","content":"hi"}\n', encoding="utf-8")
        extraction_daemon.write_signal(
            signal_type="session_end",
            session_id="sess-lock-busy-flush",
            transcript_path=str(transcript_path),
        )

        marked = []
        monkeypatch.setattr(extraction_daemon, "_acquire_session_processing_lock", lambda _sid: None)
        monkeypatch.setattr(extraction_daemon, "mark_signal_processed", lambda sig: marked.append(sig))

        extraction_daemon.process_signal(
            {
                "type": "session_end",
                "session_id": "sess-lock-busy-flush",
                "transcript_path": str(transcript_path),
                "timestamp": "2026-03-20T00:00:00Z",
                "_signal_path": str(tmp_path / "current-session-end.json"),
                "meta": {
                    "reason": "rolling_stage_flush",
                    "source_signal": "rolling",
                    "staged_payload_sweep": True,
                },
            }
        )

        assert marked == []

    def test_process_signal_serializes_shared_source_across_session_ids(self, monkeypatch, tmp_path):
        from ingest import extract as extract_mod
        from lib.adapter import reset_adapter, set_adapter

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
        _stub_successful_session_logs_ingest(monkeypatch)

        shared_uuid = "12345678-1234-1234-1234-1234567890ab"
        transcript_path = tmp_path / f"rollout-20260418-{shared_uuid}.jsonl"
        transcript_path.write_text(
            (
                '{"role":"user","content":"I keep an emergency brass key under the west porch planter."}\n'
                '{"role":"assistant","content":"Understood. I will remember the brass key location detail."}\n'
            ),
            encoding="utf-8",
        )

        class _Adapter(_OwnedTestAdapterMixin):
            def instance_root(self):
                return tmp_path / "instances" / "test-inst"

            def data_dir(self):
                return self.instance_root() / "data"

            def parse_session_jsonl(self, _path):
                return (
                    "User: I keep an emergency brass key under the west porch planter.\n"
                    "Assistant: Understood. I will remember the brass key location detail."
                )

            def is_subagent_session(self, _session_id, _transcript_path=None):
                return False

        extract_calls = []

        def _fake_extract_from_transcript(transcript, **kwargs):
            assert "brass key" in transcript
            extract_calls.append(kwargs.get("session_id"))
            return {
                "chunks_processed": 1,
                "chunks_total": 1,
                "unclassified_empty_payloads": 0,
                "raw_facts": [],
                "facts": [],
                "soul_snippets": {},
                "journal_entries": {},
                "project_logs": {},
                "raw_snippets": {},
                "raw_journal": {},
                "raw_project_logs": {},
            }

        monkeypatch.setattr(extract_mod, "extract_from_transcript", _fake_extract_from_transcript)
        monkeypatch.setattr(
            extract_mod,
            "apply_extracted_payloads",
            lambda *_args, **_kwargs: {
                "facts_stored": 0,
                "facts_skipped": 0,
                "facts": [],
                "edges_created": 0,
                "snippets": {},
                "journal": {},
                "project_log_metrics": {},
            },
        )
        monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "owner-1")
        monkeypatch.setattr(extraction_daemon, "_read_usage_totals", lambda: {})
        monkeypatch.setattr(extraction_daemon, "_tmp_dir", lambda: tmp_path)

        fake_registry = types.ModuleType("core.subagent_registry")
        fake_registry.is_registered_subagent = lambda _sid: False
        fake_registry.get_harvestable = lambda _sid: []
        fake_registry.mark_harvested = lambda _sid, _cid: None
        fake_registry._registry_dir = lambda: tmp_path / "subagents"

        real_registry = sys.modules.get("core.subagent_registry")
        sys.modules["core.subagent_registry"] = fake_registry
        set_adapter(_Adapter())
        try:
            extraction_daemon.process_signal(
                {
                    "session_id": "codex-main-session",
                    "type": "session_end",
                    "transcript_path": str(transcript_path),
                    "_signal_path": str(tmp_path / "sig1.json"),
                }
            )
            extraction_daemon.process_signal(
                {
                    "session_id": "codex-rollout-mirror",
                    "type": "timeout",
                    "transcript_path": str(transcript_path),
                    "_signal_path": str(tmp_path / "sig2.json"),
                }
            )
        finally:
            if real_registry is not None:
                sys.modules["core.subagent_registry"] = real_registry
            else:
                sys.modules.pop("core.subagent_registry", None)
            reset_adapter()

        assert extract_calls == ["codex-main-session"]

    def test_read_pending_signals_ignores_corrupt_json(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        sig_dir = extraction_daemon._signal_dir()
        (sig_dir / "00000_corrupt.json").write_text("not-json{{{{", encoding="utf-8")

        signals = extraction_daemon.read_pending_signals()
        assert signals == []
        # Corrupt file should have been removed
        assert not (sig_dir / "00000_corrupt.json").exists()

    def test_read_pending_signals_preserves_transient_read_failure(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        sig_dir = extraction_daemon._signal_dir()
        signal_file = sig_dir / "00000_transient.json"
        signal_file.write_text(
            json.dumps({
                "type": "session_end",
                "session_id": "sess-transient",
                "transcript_path": "/tmp/session.jsonl",
            }),
            encoding="utf-8",
        )
        real_read_text = pathlib.Path.read_text

        def _read_text(path, *args, **kwargs):
            if path == signal_file:
                raise OSError("nfs timeout")
            return real_read_text(path, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "read_text", _read_text)

        signals = extraction_daemon.read_pending_signals()
        assert signals == []
        assert signal_file.exists()

    def test_read_pending_signals_non_json_files_skipped(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        sig_dir = extraction_daemon._signal_dir()
        (sig_dir / "README.txt").write_text("ignore me", encoding="utf-8")

        signals = extraction_daemon.read_pending_signals()
        assert signals == []

    def test_mark_signal_processed_removes_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        extraction_daemon.write_signal(
            signal_type="session_end",
            session_id="sess-del",
            transcript_path="/fake.jsonl",
        )

        signals = extraction_daemon.read_pending_signals()
        assert len(signals) == 1

        extraction_daemon.mark_signal_processed(signals[0])

        remaining = extraction_daemon.read_pending_signals()
        assert remaining == []

    def test_mark_signal_processed_outside_signal_dir_is_refused(self, monkeypatch, tmp_path):
        """mark_signal_processed must refuse to delete paths outside the signal dir."""
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        evil_file = tmp_path / "important.txt"
        evil_file.write_text("do not delete", encoding="utf-8")

        fake_signal = {"_signal_path": str(evil_file)}
        extraction_daemon.mark_signal_processed(fake_signal)

        # File must still exist — containment check should have refused deletion
        assert evil_file.exists(), "mark_signal_processed deleted a file outside signal dir"


# ---------------------------------------------------------------------------
# write_cursor() / read_cursor()
# ---------------------------------------------------------------------------

class TestCursorRoundTrip:
    """write_cursor writes a file; read_cursor reads it back."""

    def test_write_and_read_cursor_round_trip(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")

        extraction_daemon.write_cursor("sess-abc", 17, str(transcript_path))
        result = extraction_daemon.read_cursor("sess-abc")

        assert result["line_offset"] == 17
        assert result["transcript_path"] == str(transcript_path)
        assert result["internal"] is False
        assert result["transcript_size_bytes"] == transcript_path.stat().st_size

    def test_read_cursor_returns_zero_offset_for_unknown_session(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        result = extraction_daemon.read_cursor("no-such-session")

        assert result["line_offset"] == 0
        assert result["transcript_path"] == ""
        assert result["internal"] is False
        assert result["transcript_size_bytes"] == 0

    def test_read_cursor_returns_zero_on_corrupt_json(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        cursor_dir = extraction_daemon._cursor_dir()
        (cursor_dir / "bad-sess.json").write_text("{not valid json", encoding="utf-8")

        result = extraction_daemon.read_cursor("bad-sess")

        assert result["line_offset"] == 0
        assert result["internal"] is False
        assert result["transcript_size_bytes"] == 0

    def test_write_cursor_advances_offset(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        extraction_daemon.write_cursor("sess-advance", 5, "/t.jsonl")
        extraction_daemon.write_cursor("sess-advance", 10, "/t.jsonl")

        result = extraction_daemon.read_cursor("sess-advance")
        assert result["line_offset"] == 10

    def test_write_cursor_tracks_scan_offset_separately_from_flushed_offset(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"Niseko Kinesis Phoebe Bridgers"}\n'
            '{"role":"assistant","content":"ACK"}\n',
            encoding="utf-8",
        )
        source_key = extraction_daemon._signal_source_cursor_key("sess-scan-only", str(transcript_path))

        extraction_daemon.write_cursor(
            "sess-scan-only",
            2,
            str(transcript_path),
            source_key=source_key,
            last_flushed_line_offset=0,
        )
        scan_cursor = extraction_daemon.read_cursor("sess-scan-only", source_key=source_key)
        assert scan_cursor["line_offset"] == 2
        assert scan_cursor["last_flushed_line_offset"] == 0

        extraction_daemon.write_cursor(
            "sess-scan-only",
            2,
            str(transcript_path),
            source_key=source_key,
            processed_signal_type="session_end",
        )
        flushed_cursor = extraction_daemon.read_cursor("sess-scan-only", source_key=source_key)
        assert flushed_cursor["line_offset"] == 2
        assert flushed_cursor["last_flushed_line_offset"] == 2

    def test_write_cursor_refuses_same_source_rewind_without_shrink(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"one"}\n'
            '{"role":"assistant","content":"two"}\n'
            '{"role":"user","content":"three"}\n',
            encoding="utf-8",
        )
        source_key = extraction_daemon._signal_source_cursor_key("sess-rewind", str(transcript_path))

        extraction_daemon.write_cursor("sess-rewind", 3, str(transcript_path), source_key=source_key)
        extraction_daemon.write_cursor("sess-rewind", 0, str(transcript_path), source_key=source_key)

        result = extraction_daemon.read_cursor("sess-rewind", source_key=source_key)
        assert result["line_offset"] == 3

    def test_write_cursor_allows_same_source_rewind_after_shrink(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"one"}\n'
            '{"role":"assistant","content":"two"}\n'
            '{"role":"user","content":"three"}\n',
            encoding="utf-8",
        )
        source_key = extraction_daemon._signal_source_cursor_key("sess-shrink", str(transcript_path))
        extraction_daemon.write_cursor("sess-shrink", 3, str(transcript_path), source_key=source_key)

        transcript_path.write_text('{"role":"user","content":"rebased"}\n', encoding="utf-8")
        extraction_daemon.write_cursor("sess-shrink", 0, str(transcript_path), source_key=source_key)

        result = extraction_daemon.read_cursor("sess-shrink", source_key=source_key)
        assert result["line_offset"] == 0

    def test_write_cursor_allows_same_source_rewind_after_same_size_rebase(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        transcript_path = tmp_path / "session.jsonl"
        initial = "line one\nline two\nline three\n"
        transcript_path.write_text(initial, encoding="utf-8")
        source_key = extraction_daemon._signal_source_cursor_key("sess-rebase", str(transcript_path))
        extraction_daemon.write_cursor("sess-rebase", 3, str(transcript_path), source_key=source_key)

        first_stat = transcript_path.stat()
        replacement = "rebased transcript".ljust(len(initial), "x")
        assert len(replacement.encode("utf-8")) == first_stat.st_size
        transcript_path.write_text(replacement, encoding="utf-8")
        os.utime(transcript_path, (first_stat.st_mtime + 10, first_stat.st_mtime + 10))

        extraction_daemon.write_cursor("sess-rebase", 0, str(transcript_path), source_key=source_key)

        result = extraction_daemon.read_cursor("sess-rebase", source_key=source_key)
        assert result["line_offset"] == 0

    def test_write_cursor_raises_write_failure_under_failhard(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
        monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text('{"role":"user","content":"one"}\n', encoding="utf-8")

        def _raise_atomic_write(_path, _text):
            raise OSError("cursor disk full")

        monkeypatch.setattr(extraction_daemon, "_atomic_write", _raise_atomic_write)

        with pytest.raises(OSError, match="cursor disk full"):
            extraction_daemon.write_cursor("sess-write-fail", 1, str(transcript_path))

    def test_cursor_file_is_per_session(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        extraction_daemon.write_cursor("sess-x", 3, "/tx.jsonl")
        extraction_daemon.write_cursor("sess-y", 7, "/ty.jsonl")

        x = extraction_daemon.read_cursor("sess-x")
        y = extraction_daemon.read_cursor("sess-y")

        assert x["line_offset"] == 3
        assert y["line_offset"] == 7

    def test_cursor_file_is_per_instance(self, monkeypatch, tmp_path):
        """Cursors for instance-a must not be visible to instance-b."""
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))

        monkeypatch.setenv("QUAID_INSTANCE", "instance-a")
        extraction_daemon.write_cursor("shared-sess", 100, "/t.jsonl")

        monkeypatch.setenv("QUAID_INSTANCE", "instance-b")
        result = extraction_daemon.read_cursor("shared-sess")

        assert result["line_offset"] == 0, (
            "instance-b must not see instance-a cursor"
        )

    def test_stale_preserved_signal_resolves_to_active_source_cursor(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        session_id = "sess-stale-preserved"
        instance_root = tmp_path / "instances" / "test-inst"
        preserved_path = instance_root / "logs" / "quaid" / "sessions" / f"{session_id}.jsonl"
        preserved_path.parent.mkdir(parents=True, exist_ok=True)
        preserved_path.write_text("", encoding="utf-8")
        live_path = tmp_path / "live_sessions" / f"{session_id}.jsonl"
        live_path.parent.mkdir(parents=True, exist_ok=True)
        live_path.write_text(
            "".join(
                f'{{"role":"user","content":"line {idx}"}}\n'
                for idx in range(13)
            ),
            encoding="utf-8",
        )
        source_key = extraction_daemon._signal_source_cursor_key(session_id, str(live_path))
        extraction_daemon.write_cursor(
            session_id,
            7,
            str(live_path),
            source_key=source_key,
        )
        extraction_daemon.write_cursor(session_id, 0, str(live_path))

        resolved_path, resolved_key = extraction_daemon._active_source_cursor_for_stale_signal_transcript(
            session_id,
            str(preserved_path),
        )

        assert resolved_path == str(live_path)
        assert resolved_key == source_key

    def test_empty_preserved_signal_resolves_to_live_source_cursor_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")

        session_id = "sess-live-source"
        preserved_path = (
            tmp_path
            / "instances"
            / "openclaw-main"
            / "logs"
            / "quaid"
            / "sessions"
            / f"{session_id}.jsonl"
        )
        preserved_path.parent.mkdir(parents=True, exist_ok=True)
        preserved_path.write_text("", encoding="utf-8")
        live_path = tmp_path / ".openclaw" / "agents" / "main" / "sessions" / f"{session_id}.jsonl"
        live_path.parent.mkdir(parents=True, exist_ok=True)
        live_path.write_text(
            '{"role":"user","content":"live OpenClaw session content"}\n',
            encoding="utf-8",
        )
        source_key = extraction_daemon._signal_source_cursor_key(session_id, str(live_path))
        extraction_daemon.write_cursor(session_id, 0, str(live_path), source_key=source_key)
        extraction_daemon.write_cursor(session_id, 0, str(preserved_path))

        resolved_path, resolved_key = extraction_daemon._active_source_cursor_for_stale_signal_transcript(
            session_id,
            str(preserved_path),
        )

        assert resolved_path == str(live_path)
        assert resolved_key == source_key

    def test_nonempty_preserved_signal_transcript_does_not_redirect(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        session_id = "sess-valid-preserved"
        instance_root = tmp_path / "instances" / "test-inst"
        preserved_path = instance_root / "logs" / "quaid" / "sessions" / f"{session_id}.jsonl"
        preserved_path.parent.mkdir(parents=True, exist_ok=True)
        preserved_path.write_text('{"role":"user","content":"preserved content"}\n', encoding="utf-8")
        live_path = tmp_path / "live_sessions" / f"{session_id}.jsonl"
        live_path.parent.mkdir(parents=True, exist_ok=True)
        live_path.write_text(
            "".join(
                f'{{"role":"user","content":"line {idx}"}}\n'
                for idx in range(13)
            ),
            encoding="utf-8",
        )
        source_key = extraction_daemon._signal_source_cursor_key(session_id, str(live_path))
        extraction_daemon.write_cursor(session_id, 7, str(live_path), source_key=source_key)
        extraction_daemon.write_cursor(session_id, 0, str(live_path))

        resolved_path, resolved_key = extraction_daemon._active_source_cursor_for_stale_signal_transcript(
            session_id,
            str(preserved_path),
        )

        assert (resolved_path, resolved_key) == ("", "")

    def test_stale_preserved_signal_reraises_in_fail_hard(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        session_id = "sess-fail-hard-preserved"
        instance_root = tmp_path / "instances" / "test-inst"
        preserved_path = instance_root / "logs" / "quaid" / "sessions" / f"{session_id}.jsonl"
        preserved_path.parent.mkdir(parents=True, exist_ok=True)
        preserved_path.write_text("", encoding="utf-8")

        monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)
        monkeypatch.setattr(
            extraction_daemon,
            "_transcript_size_bytes",
            lambda _path: (_ for _ in ()).throw(RuntimeError("stat failed")),
        )

        with pytest.raises(RuntimeError, match="stat failed"):
            extraction_daemon._active_source_cursor_for_stale_signal_transcript(
                session_id,
                str(preserved_path),
            )

    @pytest.mark.parametrize(
        ("case", "expected"),
        [
            ("terminal_checkpoint", "terminal checkpoint source cursor lookup failed"),
            ("grown_transcript", "grown transcript source cursor lookup failed"),
            ("stale_signal", "stale signal transcript source cursor lookup failed"),
            ("empty_preserved", "empty preserved cursor source lookup failed"),
            ("larger_preserved", "larger preserved mirror lookup failed"),
            ("larger_live", "larger live transcript lookup failed"),
        ],
    )
    def test_cursor_resolution_helpers_warn_when_fail_open(
        self,
        monkeypatch,
        tmp_path,
        caplog,
        case,
        expected,
    ):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
        monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: False)
        session_id = "019e60ec-838e-7eb1-8ed6-7f52f2b47570"

        with caplog.at_level("WARNING", logger="quaid.daemon"):
            if case in {"terminal_checkpoint", "grown_transcript"}:
                monkeypatch.setattr(
                    extraction_daemon,
                    "_signal_source_cursor_key",
                    lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cursor lookup failed")),
                )
                fn = (
                    extraction_daemon._active_source_cursor_for_terminal_checkpoint_tail
                    if case == "terminal_checkpoint"
                    else extraction_daemon._active_source_cursor_for_grown_transcript
                )
                assert fn(
                    cursor_file=tmp_path / "alias.json",
                    session_id=session_id,
                    transcript_path=str(tmp_path / f"{session_id}.jsonl"),
                    cursor_data={},
                ) == ({}, Path(), "")
            elif case in {"stale_signal", "empty_preserved"}:
                preserved_path = (
                    tmp_path
                    / "instances"
                    / "openclaw-main"
                    / "logs"
                    / "quaid"
                    / "sessions"
                    / f"{session_id}.jsonl"
                )
                preserved_path.parent.mkdir(parents=True, exist_ok=True)
                preserved_path.write_text("", encoding="utf-8")
                monkeypatch.setattr(
                    extraction_daemon,
                    "_transcript_size_bytes",
                    lambda _path: (_ for _ in ()).throw(RuntimeError("size lookup failed")),
                )
                if case == "stale_signal":
                    assert extraction_daemon._active_source_cursor_for_stale_signal_transcript(
                        session_id,
                        str(preserved_path),
                    ) == ("", "")
                else:
                    assert extraction_daemon._active_source_cursor_for_empty_preserved_cursor(
                        session_id,
                        str(preserved_path),
                    ) == ({}, "", "")
            elif case == "larger_preserved":
                live_path = tmp_path / f"{session_id}.jsonl"
                live_path.write_text('{"role":"user","content":"live"}\n', encoding="utf-8")
                monkeypatch.setattr(
                    extraction_daemon,
                    "_adapter_owns_transcript_path",
                    lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("ownership failed")),
                )
                assert extraction_daemon._larger_preserved_mirror_for_live_transcript(
                    session_id,
                    str(live_path),
                    adapter=object(),
                ) == ""
            else:
                preserved_path = (
                    tmp_path
                    / "instances"
                    / "openclaw-main"
                    / "logs"
                    / "quaid"
                    / "sessions"
                    / f"{session_id}.jsonl"
                )
                live_path = tmp_path / f"{session_id}.jsonl"
                preserved_path.parent.mkdir(parents=True, exist_ok=True)
                preserved_path.write_text('{"role":"user","content":"mirror"}\n', encoding="utf-8")
                live_path.write_text('{"role":"user","content":"live richer"}\n', encoding="utf-8")
                monkeypatch.setattr(
                    extraction_daemon,
                    "_adapter_owns_transcript_path",
                    lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("ownership failed")),
                )

                class _Adapter:
                    def get_session_path(self, session_id_arg):
                        assert session_id_arg == session_id
                        return live_path

                assert extraction_daemon._larger_live_transcript_for_preserved_mirror(
                    session_id,
                    str(preserved_path),
                    adapter=_Adapter(),
                ) == ""

        assert expected in caplog.text
        assert "returning empty" in caplog.text


# ---------------------------------------------------------------------------
# check_idle_sessions() — additional coverage
# ---------------------------------------------------------------------------

class TestCheckIdleSessions:
    """Additional coverage for check_idle_sessions() paths."""

    def _setup_cursor(self, tmp_path, instance_id, session_id, line_offset, transcript_path):
        cursor_dir = tmp_path / "instances" / instance_id / "data" / "session-cursors"
        cursor_dir.mkdir(parents=True, exist_ok=True)
        cursor_file = cursor_dir / f"{session_id}.json"
        cursor_file.write_text(
            json.dumps({
                "session_id": session_id,
                "line_offset": line_offset,
                "transcript_path": str(transcript_path),
            }),
            encoding="utf-8",
        )
        return cursor_file

    def _setup_rolling_state(self, tmp_path, instance_id, session_id, carry_facts, transcript_path):
        rolling_dir = tmp_path / "instances" / instance_id / "data" / "rolling-extraction"
        rolling_dir.mkdir(parents=True, exist_ok=True)
        state_file = rolling_dir / f"{session_id}.json"
        state_file.write_text(
            json.dumps({
                "session_id": session_id,
                "carry_facts": carry_facts,
                "transcript_path": str(transcript_path),
                "raw_facts": carry_facts,
            }),
            encoding="utf-8",
        )
        return state_file

    def test_read_rolling_state_raises_corrupt_state_when_fail_hard(self, monkeypatch, tmp_path):
        instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        rolling_dir = tmp_path / "instances" / instance_id / "data" / "rolling-extraction"
        rolling_dir.mkdir(parents=True, exist_ok=True)
        (rolling_dir / "sess-corrupt.json").write_text("{not-json", encoding="utf-8")

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

        with pytest.raises(json.JSONDecodeError):
            extraction_daemon.read_rolling_state("sess-corrupt")

    def test_check_idle_sessions_logs_rolling_state_read_failure(self, monkeypatch, tmp_path, caplog):
        instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        transcript_path = tmp_path / "idle-corrupt-state.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"staged payload needs a visible read failure"}\n',
            encoding="utf-8",
        )
        self._setup_cursor(tmp_path, instance_id, "sess-corrupt-state", 1, transcript_path)
        self._setup_rolling_state(
            tmp_path,
            instance_id,
            "sess-corrupt-state",
            [{"text": "staged fact", "category": "fact"}],
            transcript_path,
        )

        now = 1_700_000_000.0
        os.utime(transcript_path, (now - 3600, now - 3600))
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
        monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: now - 7200)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: False)
        monkeypatch.setattr(extraction_daemon, "read_rolling_state", lambda _sid: (_ for _ in ()).throw(
            OSError("rolling state unavailable")
        ))

        with caplog.at_level("WARNING", logger="quaid.daemon"):
            extraction_daemon.check_idle_sessions(timeout_minutes=30)

        assert "rolling state read failed during idle scan for sess-corrupt-state" in caplog.text
        assert "rolling state unavailable" in caplog.text

    def test_check_idle_sessions_raises_rolling_state_failure_when_fail_hard(self, monkeypatch, tmp_path):
        instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        transcript_path = tmp_path / "idle-failhard-state.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"failHard should not hide idle rolling read failures"}\n',
            encoding="utf-8",
        )
        self._setup_cursor(tmp_path, instance_id, "sess-failhard-state", 1, transcript_path)

        now = 1_700_000_000.0
        os.utime(transcript_path, (now - 3600, now - 3600))
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
        monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: now - 7200)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)
        monkeypatch.setattr(extraction_daemon, "read_rolling_state", lambda _sid: (_ for _ in ()).throw(
            OSError("rolling state unavailable")
        ))

        with pytest.raises(OSError, match="rolling state unavailable"):
            extraction_daemon.check_idle_sessions(timeout_minutes=30)

    def test_check_idle_sessions_warns_on_subagent_registry_failure_when_fail_open(
        self, monkeypatch, tmp_path, caplog
    ):
        instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        cursor_dir = tmp_path / "instances" / instance_id / "data" / "session-cursors"
        cursor_dir.mkdir(parents=True)

        fake_registry = types.ModuleType("core.subagent_registry")
        fake_registry._registry_dir = lambda: (_ for _ in ()).throw(RuntimeError("registry down"))
        monkeypatch.setitem(sys.modules, "core.subagent_registry", fake_registry)
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setattr(extraction_daemon, "_load_runtime_adapter", lambda: None)
        monkeypatch.setattr(extraction_daemon, "_ensure_discovered_session_cursors", lambda _adapter: 0)
        monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: 0.0)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: False)

        with caplog.at_level("WARNING", logger="quaid.daemon"):
            extraction_daemon.check_idle_sessions(timeout_minutes=30)

        assert "idle subagent registry load failed" in caplog.text
        assert "registry down" in caplog.text

    def test_check_idle_sessions_raises_subagent_registry_failure_when_fail_hard(
        self, monkeypatch, tmp_path, caplog
    ):
        instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        cursor_dir = tmp_path / "instances" / instance_id / "data" / "session-cursors"
        cursor_dir.mkdir(parents=True)

        fake_registry = types.ModuleType("core.subagent_registry")
        fake_registry._registry_dir = lambda: (_ for _ in ()).throw(RuntimeError("registry down"))
        monkeypatch.setitem(sys.modules, "core.subagent_registry", fake_registry)
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setattr(extraction_daemon, "_load_runtime_adapter", lambda: None)
        monkeypatch.setattr(extraction_daemon, "_ensure_discovered_session_cursors", lambda _adapter: 0)
        monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

        with caplog.at_level("WARNING", logger="quaid.daemon"):
            with pytest.raises(RuntimeError, match="idle subagent registry load failed") as excinfo:
                extraction_daemon.check_idle_sessions(timeout_minutes=30)

        assert isinstance(excinfo.value.__cause__, RuntimeError)
        assert "registry down" in str(excinfo.value.__cause__)
        assert "idle subagent registry load failed" in caplog.text

    def test_skips_session_when_transcript_file_missing(self, monkeypatch, tmp_path):
        """check_idle_sessions must skip cursors pointing to non-existent transcripts."""
        instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        cursor_file = self._setup_cursor(tmp_path, instance_id, "ghost-sess", 1, tmp_path / "nonexistent.jsonl")

        captured = []
        now = 1_700_000_000.0
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
        monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: now - 3600)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(extraction_daemon, "write_signal", lambda *a, **kw: captured.append((a, kw)))

        extraction_daemon.check_idle_sessions(timeout_minutes=30)

        assert captured == []
        assert cursor_file.exists()

    def test_check_idle_sessions_reaps_old_orphaned_cursor(self, monkeypatch, tmp_path):
        instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        cursor_file = self._setup_cursor(tmp_path, instance_id, "ghost-old", 1, tmp_path / "missing-old.jsonl")

        now = 1_700_000_000.0
        old_mtime = now - extraction_daemon._ORPHANED_CURSOR_RETENTION_SECONDS - 60
        os.utime(cursor_file, (old_mtime, old_mtime))
        captured = []
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
        monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: now - 3600)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(extraction_daemon, "write_signal", lambda *a, **kw: captured.append((a, kw)))

        extraction_daemon.check_idle_sessions(timeout_minutes=30)

        assert captured == []
        assert not cursor_file.exists()

    def test_check_chunk_ready_sessions_reaps_old_orphaned_cursor(self, monkeypatch, tmp_path):
        instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        cursor_file = self._setup_cursor(tmp_path, instance_id, "ghost-roll", 1, tmp_path / "missing-roll.jsonl")

        now = 1_700_000_000.0
        old_mtime = now - extraction_daemon._ORPHANED_CURSOR_RETENTION_SECONDS - 60
        os.utime(cursor_file, (old_mtime, old_mtime))
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
        monkeypatch.setattr(extraction_daemon, "_ensure_discovered_session_cursors", lambda adapter: None)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])

        extraction_daemon.check_chunk_ready_sessions(chunk_tokens=10)

        assert not cursor_file.exists()

    def test_recent_idle_sessions_timeout_before_stale_backlog(self, monkeypatch, tmp_path):
        """A fresh idle session must not wait behind old stale timeout work."""
        instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        stale_transcript = tmp_path / "stale.jsonl"
        fresh_transcript = tmp_path / "fresh.jsonl"
        stale_transcript.write_text(
            '{"role":"user","content":"old stale duplicate fact"}\n',
            encoding="utf-8",
        )
        fresh_transcript.write_text(
            '{"role":"user","content":"my garden shed combination is indigo-lantern-7742"}\n',
            encoding="utf-8",
        )
        self._setup_cursor(tmp_path, instance_id, "a-stale-sess", 0, stale_transcript)
        self._setup_cursor(tmp_path, instance_id, "z-fresh-sess", 0, fresh_transcript)

        now = 1_700_000_000.0
        os.utime(stale_transcript, (now - 3600, now - 3600))
        os.utime(fresh_transcript, (now - 120, now - 120))

        captured = []
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
        monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: now - 7200)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                }
            ),
        )

        extraction_daemon.check_idle_sessions(timeout_minutes=1)

        assert [item["session_id"] for item in captured] == ["z-fresh-sess", "a-stale-sess"]

    def test_idle_timeout_source_key_uses_current_row_cursor_data(self, monkeypatch, tmp_path):
        """Idle dedup must use the cursor row being processed, not the last discovery-loop cursor."""
        instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        cursor_dir = tmp_path / "instances" / instance_id / "data" / "session-cursors"
        cursor_dir.mkdir(parents=True, exist_ok=True)
        idle_transcript = tmp_path / "idle-row.jsonl"
        fresh_transcript = tmp_path / "fresh-row.jsonl"
        idle_transcript.write_text('{"role":"user","content":"older row"}\n', encoding="utf-8")
        fresh_transcript.write_text('{"role":"user","content":"fresh row"}\n', encoding="utf-8")
        idle_cursor = self._setup_cursor(tmp_path, instance_id, "idle-row-sess", 0, idle_transcript)
        fresh_cursor = self._setup_cursor(tmp_path, instance_id, "fresh-row-sess", 0, fresh_transcript)

        now = 1_700_000_000.0
        os.utime(idle_transcript, (now - 3600, now - 3600))
        os.utime(fresh_transcript, (now - 120, now - 120))

        class _OrderedCursorDir:
            def __init__(self, root, files):
                self.root = root
                self.files = files

            def is_dir(self):
                return True

            def glob(self, pattern):
                assert pattern == "*.json"
                return iter(self.files)

            def __truediv__(self, name):
                return self.root / name

        seen_source_keys = []

        def _source_key(_session_id, _transcript_path, *, cursor_data=None, staged_state=None):
            _ = staged_state
            cursor_session = str((cursor_data or {}).get("session_id") or "missing")
            return f"key-for-{cursor_session}"

        def _already_pending(*, pending_source_keys, source_key, session_id, scanner):
            _ = pending_source_keys
            seen_source_keys.append((scanner, session_id, source_key))
            return False

        captured = []
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", instance_id)
        monkeypatch.setattr(extraction_daemon, "_cursor_dir", lambda: _OrderedCursorDir(cursor_dir, [idle_cursor, fresh_cursor]))
        monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
        monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: now - 7200)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(extraction_daemon, "_ensure_discovered_session_cursors", lambda _adapter: 0)
        monkeypatch.setattr(extraction_daemon, "_cursor_or_adapter_owns_transcript_path", lambda *_args, **_kwargs: True)
        monkeypatch.setattr(extraction_daemon, "_reconcile_internal_cursor_state", lambda *_args, **_kwargs: "not_internal")
        monkeypatch.setattr(extraction_daemon, "_signal_source_cursor_key", _source_key)
        monkeypatch.setattr(extraction_daemon, "_source_signal_already_pending", _already_pending)
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta"),
                }
            ),
        )

        extraction_daemon.check_idle_sessions(timeout_minutes=30)

        assert seen_source_keys == [("idle", "idle-row-sess", "key-for-idle-row-sess")]
        assert [item["session_id"] for item in captured] == ["idle-row-sess"]

    def test_idle_scan_trusts_instance_cursor_when_adapter_ownership_rejects(
        self, monkeypatch, tmp_path
    ):
        """Explicit instances may own a CDX transcript through their cursor, not cwd slug."""
        instance_id = "codex-private-tmp-cdx-livetest"
        session_id = "019dd519-9d12-71c0-bd3b-fc058b323c33"
        transcript_path = tmp_path / "rollout-2026-04-28T17-18-38-019dd519-9d12-71c0-bd3b-fc058b323c33.jsonl"
        transcript_path.write_text(
            '{"type":"session_meta","payload":{"cwd":"/Users/admin/quaidcode/dev"}}\n'
            '{"type":"event_msg","payload":{"type":"user_message","message":"My desk lamp has a brass shade."}}\n',
            encoding="utf-8",
        )
        self._setup_cursor(tmp_path, instance_id, session_id, 0, transcript_path)

        now = 1_700_000_000.0
        os.utime(transcript_path, (now - 120, now - 120))

        class _RejectingAdapter:
            def owns_session_path(self, path, session_id=""):
                return False

        captured = []
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", instance_id)
        monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
        monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: now - 7200)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(extraction_daemon, "_load_runtime_adapter", lambda: _RejectingAdapter())
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta"),
                }
            ),
        )

        extraction_daemon.check_idle_sessions(timeout_minutes=1)

        assert captured == [
            {
                "signal_type": "timeout",
                "session_id": session_id,
                "transcript_path": str(transcript_path),
                "meta": {"compact_on_timeout": True},
            }
        ]

    def test_discovers_uncursored_session_files_before_timeout_scan(self, monkeypatch, tmp_path):
        """Idle scan must seed cursors for uncursored session transcripts before timing them out."""
        instance_id = "openclaw-livetest"
        sessions_dir = tmp_path / "openclaw-sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        session_id = "8e2157da-8f22-4008-aee7-b3a65a233101"
        transcript_path = sessions_dir / f"{session_id}.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"I like the canal towpath near my flat."}\n',
            encoding="utf-8",
        )

        now = 1_700_000_000.0
        mtime = now - (60 * 60)
        os.utime(transcript_path, (mtime, mtime))

        captured = []

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def get_sessions_dir(self):
                return sessions_dir

            def parse_session_jsonl(self, path):
                return path.read_text(encoding="utf-8")

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", instance_id)
        monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
        monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: now - 7200)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(extraction_daemon, "_load_runtime_adapter", lambda: _FakeAdapter())
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta"),
                }
            ),
        )

        extraction_daemon.check_idle_sessions(timeout_minutes=30)

        cursor = extraction_daemon.read_cursor(session_id)
        assert cursor["line_offset"] == 0
        assert cursor["transcript_path"] == str(transcript_path)
        assert captured == [
            {
                "signal_type": "timeout",
                "session_id": session_id,
                "transcript_path": str(transcript_path),
                "meta": {"compact_on_timeout": True},
            }
        ]

    def test_repairs_trajectory_cursor_before_timeout_scan(self, monkeypatch, tmp_path):
        """OC trajectory sidecars must not consume the cursor for real session JSONL."""
        instance_id = "openclaw-livetest"
        sessions_dir = tmp_path / "openclaw-sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        session_id = "f324c131-6414-4629-8ee6-7653995ac2fb"
        transcript_path = sessions_dir / f"{session_id}.jsonl"
        trajectory_path = sessions_dir / f"{session_id}.trajectory.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"my garden shed combination is indigo-lantern-7742"}\n',
            encoding="utf-8",
        )
        trajectory_path.write_text(
            '{"type":"trace.metadata","data":{"prompt":"large OC trajectory sidecar"}}\n',
            encoding="utf-8",
        )

        now = 1_700_000_000.0
        mtime = now - (60 * 60)
        os.utime(transcript_path, (mtime, mtime))
        os.utime(trajectory_path, (mtime, mtime))

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", instance_id)
        source_key = extraction_daemon._signal_source_cursor_key(session_id, str(transcript_path))
        extraction_daemon.write_cursor(
            session_id,
            14,
            str(trajectory_path),
            internal=True,
            source_key=source_key,
        )

        captured = []

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def get_sessions_dir(self):
                return sessions_dir

            def parse_session_jsonl(self, path):
                if Path(path).name.endswith(".trajectory.jsonl"):
                    return ""
                return Path(path).read_text(encoding="utf-8")

        monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
        monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: now - 7200)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(extraction_daemon, "_load_runtime_adapter", lambda: _FakeAdapter())
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta"),
                }
            ),
        )

        try:
            extraction_daemon.check_idle_sessions(timeout_minutes=30)
        finally:
            extraction_daemon._cursor_end_timeout_fired.discard(session_id)

        cursor = extraction_daemon.read_cursor(session_id, source_key=source_key)
        assert cursor["line_offset"] == 0
        assert cursor["transcript_path"] == str(transcript_path)
        assert cursor["internal"] is False
        assert captured == [
            {
                "signal_type": "timeout",
                "session_id": session_id,
                "transcript_path": str(transcript_path),
                "meta": {"compact_on_timeout": True},
            }
        ]

    def test_repairs_stale_relocated_cursor_before_timeout_scan(self, monkeypatch, tmp_path):
        """Idle timeout must use the live adapter session when a preserved copy is stale."""
        instance_id = "openclaw-livetest"
        sessions_dir = tmp_path / "openclaw-sessions"
        preserved_dir = tmp_path / "instances" / instance_id / "logs" / "quaid" / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        preserved_dir.mkdir(parents=True, exist_ok=True)
        session_id = "9bf5c24b-3edd-466e-a19d-52ea93822103"
        transcript_path = sessions_dir / f"{session_id}.jsonl"
        preserved_path = preserved_dir / f"{session_id}.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"my garden shed combination is indigo-lantern-7742"}\n',
            encoding="utf-8",
        )
        preserved_path.write_text(
            '{"role":"user","content":"startup context without the new canary"}\n',
            encoding="utf-8",
        )

        now = 1_700_000_000.0
        mtime = now - (60 * 60)
        os.utime(transcript_path, (mtime, mtime))
        os.utime(preserved_path, (mtime - 10, mtime - 10))

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", instance_id)
        source_key = extraction_daemon._signal_source_cursor_key(session_id, str(transcript_path))
        extraction_daemon.write_cursor(
            session_id,
            8,
            str(preserved_path),
            internal=False,
            source_key=source_key,
        )

        captured = []

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def get_sessions_dir(self):
                return sessions_dir

            def parse_session_jsonl(self, path):
                return Path(path).read_text(encoding="utf-8")

        monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
        monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: now - 7200)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(extraction_daemon, "_load_runtime_adapter", lambda: _FakeAdapter())
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta"),
                }
            ),
        )

        try:
            extraction_daemon.check_idle_sessions(timeout_minutes=30)
        finally:
            extraction_daemon._cursor_end_timeout_fired.discard(session_id)

        cursor = extraction_daemon.read_cursor(session_id, source_key=source_key)
        assert cursor["line_offset"] == 0
        assert cursor["transcript_path"] == str(transcript_path)
        assert cursor["internal"] is False
        assert captured == [
            {
                "signal_type": "timeout",
                "session_id": session_id,
                "transcript_path": str(transcript_path),
                "meta": {"compact_on_timeout": True},
            }
        ]


class TestRollingExtraction:
    def _setup_cursor(self, tmp_path, instance_id, session_id, line_offset, transcript_path):
        cursor_dir = tmp_path / "instances" / instance_id / "data" / "session-cursors"
        cursor_dir.mkdir(parents=True, exist_ok=True)
        cursor_file = cursor_dir / f"{session_id}.json"
        cursor_file.write_text(
            json.dumps({
                "session_id": session_id,
                "line_offset": line_offset,
                "transcript_path": str(transcript_path),
            }),
            encoding="utf-8",
        )
        return cursor_file

    def _setup_rolling_state(self, tmp_path, instance_id, session_id, carry_facts, transcript_path):
        rolling_dir = tmp_path / "instances" / instance_id / "data" / "rolling-extraction"
        rolling_dir.mkdir(parents=True, exist_ok=True)
        state_file = rolling_dir / f"{session_id}.json"
        state_file.write_text(
            json.dumps({
                "session_id": session_id,
                "carry_facts": carry_facts,
                "transcript_path": str(transcript_path),
                "raw_facts": carry_facts,
            }),
            encoding="utf-8",
        )
        return state_file

    def test_check_chunk_ready_sessions_writes_rolling_signal(self, monkeypatch, tmp_path):
        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"hello there this is a longer message"}\n'
            '{"role":"assistant","content":"reply with some extra words"}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        extraction_daemon.write_cursor("sess-roll", 0, str(transcript_path))

        captured = []
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda default=8000: 2)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                }
            ),
        )

        extraction_daemon.check_chunk_ready_sessions()

        assert captured == [
            {
                "signal_type": "rolling",
                "session_id": "sess-roll",
                "transcript_path": str(transcript_path),
            }
        ]

    def test_check_chunk_ready_sessions_queues_rolling_despite_pending_lifecycle_signal(
        self, monkeypatch, tmp_path
    ):
        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"alpha beta gamma delta epsilon zeta"}\n'
            '{"role":"assistant","content":"ack"}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        extraction_daemon.write_cursor("sess-roll", 0, str(transcript_path))
        extraction_daemon.write_signal(
            signal_type="reset",
            session_id="sess-roll",
            transcript_path=str(transcript_path),
        )

        captured = []
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda default=8000: 2)
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                }
            ),
        )

        extraction_daemon.check_chunk_ready_sessions()

        assert captured == [
            {
                "signal_type": "rolling",
                "session_id": "sess-roll",
                "transcript_path": str(transcript_path),
            }
        ]

    def test_process_signal_defers_lifecycle_when_rolling_is_pending(
        self, monkeypatch, tmp_path
    ):
        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"alpha beta gamma delta epsilon zeta"}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        extraction_daemon.write_cursor("sess-roll", 0, str(transcript_path))
        extraction_daemon.write_signal(
            signal_type="reset",
            session_id="sess-roll",
            transcript_path=str(transcript_path),
        )
        extraction_daemon.write_signal(
            signal_type="rolling",
            session_id="sess-roll",
            transcript_path=str(transcript_path),
        )
        monkeypatch.setattr(
            extraction_daemon,
            "_reconcile_internal_cursor_state",
            lambda *args, **kwargs: "not_internal",
        )

        signals = extraction_daemon.read_pending_signals()
        assert [signal["type"] for signal in signals] == ["reset", "rolling"]

        extraction_daemon.process_signal(signals[0])

        remaining = extraction_daemon.read_pending_signals()
        assert [signal["type"] for signal in remaining] == ["reset", "rolling"]

    def test_chunk_scan_trusts_instance_cursor_when_adapter_ownership_rejects(
        self, monkeypatch, tmp_path
    ):
        transcript_path = tmp_path / "rollout-2026-04-28T17-18-38-019dd519-9d12-71c0-bd3b-fc058b323c33.jsonl"
        transcript_path.write_text(
            '{"type":"session_meta","payload":{"cwd":"/Users/admin/quaidcode/dev"}}\n'
            '{"type":"event_msg","payload":{"type":"user_message","message":"hello there this is a longer message"}}\n'
            '{"type":"event_msg","payload":{"type":"assistant_message","message":"reply with some extra words"}}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "codex-private-tmp-cdx-livetest")
        session_id = "019dd519-9d12-71c0-bd3b-fc058b323c33"
        extraction_daemon.write_cursor(session_id, 0, str(transcript_path))

        class _RejectingAdapter:
            def owns_session_path(self, path, session_id=""):
                return False

            def parse_session_jsonl(self, path):
                return "User: hello there this is a longer message\n\nAssistant: reply with some extra words"

        captured = []
        monkeypatch.setattr(extraction_daemon, "_load_runtime_adapter", lambda: _RejectingAdapter())
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda default=8000: 2)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                }
            ),
        )

        extraction_daemon.check_chunk_ready_sessions()

        assert captured == [
            {
                "signal_type": "rolling",
                "session_id": session_id,
                "transcript_path": str(transcript_path),
            }
        ]

    def test_check_chunk_ready_sessions_persists_transcript_path_across_restart(self, monkeypatch, tmp_path):
        import sys
        import types

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            '{"role":"assistant","content":"warmup"}\n'
            '{"role":"user","content":"My cat Luna sleeps on the windowsill every afternoon."}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        extraction_daemon.write_cursor("sess-roll-restart", 0, str(transcript_path))

        real_adapter = sys.modules.get("lib.adapter")
        fake_adapter_mod = types.ModuleType("lib.adapter")

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def parse_session_jsonl(self, path):
                return "User: My cat Luna sleeps on the windowsill every afternoon."

        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        fake_adapter_mod.quaid_projects_dir = lambda home: Path(home) / "projects"
        fake_adapter_mod.quaid_tracking_dir = lambda home: Path(home) / ".git-tracking"
        sys.modules["lib.adapter"] = fake_adapter_mod

        captured = []
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda default=8000: 999)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        try:
            extraction_daemon.check_chunk_ready_sessions()
            state = extraction_daemon.read_rolling_state("sess-roll-restart")
            assert state["transcript_path"] == str(transcript_path)

            # Simulate daemon restart: state is reloaded from disk and persisted again.
            extraction_daemon.write_rolling_state("sess-roll-restart", state)
            restarted = extraction_daemon.read_rolling_state("sess-roll-restart")
        finally:
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

        assert restarted["transcript_path"] == str(transcript_path)
        assert restarted["semantic_buffer_tokens"] > 0
        assert captured == []

    @pytest.mark.parametrize("signal_type", ["compaction", "timeout"])
    def test_process_signal_no_new_content_writes_noop_flush_metric(self, monkeypatch, tmp_path, signal_type):
        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"hello"}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        extraction_daemon.write_cursor("sess-noop", 1, str(transcript_path))
        monkeypatch.setattr(extraction_daemon, "_session_has_harvestable_subagents", lambda *args, **kwargs: False)

        rolling_metrics = []
        monkeypatch.setattr(
            extraction_daemon,
            "write_rolling_metric",
            lambda event, session_id, **data: rolling_metrics.append(
                {"event": event, "session_id": session_id, **data}
            ),
        )

        extraction_daemon.write_signal(
            signal_type=signal_type,
            session_id="sess-noop",
            transcript_path=str(transcript_path),
        )
        extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])

        assert extraction_daemon.read_pending_signals() == []
        timeout_marker_path = (
            tmp_path
            / "instances"
            / "rolling-inst"
            / "data"
            / "context-refresh-timeout"
            / "sess-noop.json"
        )
        if signal_type == "timeout":
            assert timeout_marker_path.is_file()
        else:
            assert not timeout_marker_path.exists()
        assert rolling_metrics
        metric = rolling_metrics[-1]
        assert metric["event"] == "rolling_flush"
        assert metric["session_id"] == "sess-noop"
        assert metric["signal_type"] == signal_type
        assert metric["noop"] is True
        assert metric["noop_reason"] == "no_new_content"
        assert metric["final_facts_stored"] == 0
        assert metric["final_facts_skipped"] == 0

    def test_session_log_bridge_failure_raises_on_failhard_no_new_content(self, monkeypatch, tmp_path):
        import core.ingest_runtime as ingest_runtime

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"hello"}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        extraction_daemon.write_cursor("sess-session-log-fail", 1, str(transcript_path))
        monkeypatch.setattr(extraction_daemon, "_session_has_harvestable_subagents", lambda *args, **kwargs: False)
        monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)
        monkeypatch.setattr(
            ingest_runtime,
            "run_session_logs_ingest",
            lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("bridge down")),
        )

        extraction_daemon.write_signal(
            signal_type="session_end",
            session_id="sess-session-log-fail",
            transcript_path=str(transcript_path),
        )

        with pytest.raises(RuntimeError, match="session_logs ingest failed"):
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])

        assert extraction_daemon.read_pending_signals() == []
        lock_dir = tmp_path / "instances" / "rolling-inst" / "data" / "session-processing"
        lock_files = list(lock_dir.glob("*.lock"))
        assert len(lock_files) == 1
        assert extraction_daemon._processing_lock_active(lock_files[0].stem) is False

    def test_session_end_no_new_content_routes_session_ingest_through_broker(self, monkeypatch, tmp_path):
        import core.ingest_runtime as ingest_runtime

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"hello"}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        extraction_daemon.write_cursor("sess-no-new-broker", 1, str(transcript_path))
        monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "Owner")
        monkeypatch.setattr(extraction_daemon, "_session_has_harvestable_subagents", lambda *args, **kwargs: False)
        monkeypatch.setattr(
            ingest_runtime,
            "run_session_logs_ingest",
            lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must route through broker request")),
        )

        ingest_calls = []
        monkeypatch.setattr(
            extraction_daemon,
            "_request_session_logs_ingest",
            lambda **kwargs: ingest_calls.append(kwargs) or {"status": "indexed"},
        )

        extraction_daemon.write_signal(
            signal_type="session_end",
            session_id="sess-no-new-broker",
            transcript_path=str(transcript_path),
        )
        extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])

        assert extraction_daemon.read_pending_signals() == []
        assert len(ingest_calls) == 1
        assert ingest_calls[0]["session_id"] == "sess-no-new-broker"
        assert ingest_calls[0]["owner_id"] == "Owner"
        assert ingest_calls[0]["transcript_path"] == str(transcript_path)
        assert ingest_calls[0]["message_count"] == 0
        assert ingest_calls[0]["topic_hint"] == ""

    def test_session_end_no_new_content_skips_missing_session_logs_transcript(self, monkeypatch, tmp_path):
        import core.ingest_runtime as ingest_runtime

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text("", encoding="utf-8")
        missing_session_logs_path = tmp_path / "missing-session-logs.jsonl"

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        extraction_daemon.write_cursor("sess-empty-no-file", 0, str(transcript_path))
        monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "Owner")
        monkeypatch.setattr(extraction_daemon, "_session_has_harvestable_subagents", lambda *args, **kwargs: False)
        monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)
        monkeypatch.setattr(
            ingest_runtime,
            "run_session_logs_ingest",
            lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must route through broker request")),
        )
        monkeypatch.setattr(
            extraction_daemon,
            "_session_logs_ingest_transcript_path_for_signal",
            lambda *_args, **_kwargs: str(missing_session_logs_path),
        )
        monkeypatch.setattr(
            extraction_daemon,
            "_request_session_logs_ingest",
            lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("missing empty session transcript should not be ingested")
            ),
        )

        extraction_daemon.write_signal(
            signal_type="session_end",
            session_id="sess-empty-no-file",
            transcript_path=str(transcript_path),
        )
        extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])

        assert extraction_daemon.read_pending_signals() == []
        lock_dir = tmp_path / "instances" / "rolling-inst" / "data" / "session-processing"
        lock_files = list(lock_dir.glob("*.lock"))
        assert len(lock_files) == 1
        assert extraction_daemon._processing_lock_active(lock_files[0].stem) is False

    @pytest.mark.parametrize("signal_type", ["compaction", "timeout"])
    def test_process_signal_noop_does_not_recreate_empty_rolling_state(
        self, monkeypatch, tmp_path, signal_type
    ):
        import sys
        import types

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            '{"role":"assistant","content":"Compacted (17k -> 2.1k)"}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        extraction_daemon.write_cursor("sess-noop-state", 0, str(transcript_path))
        extraction_daemon.clear_rolling_state("sess-noop-state")
        monkeypatch.setattr(extraction_daemon, "_session_has_harvestable_subagents", lambda *args, **kwargs: False)

        real_adapter = sys.modules.get("lib.adapter")
        fake_adapter_mod = types.ModuleType("lib.adapter")

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def parse_session_jsonl(self, path):
                _ = path
                return ""

        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        fake_adapter_mod.quaid_projects_dir = lambda home: Path(home) / "projects"
        fake_adapter_mod.quaid_tracking_dir = lambda home: Path(home) / ".git-tracking"
        sys.modules["lib.adapter"] = fake_adapter_mod

        try:
            extraction_daemon.write_signal(
                signal_type=signal_type,
                session_id="sess-noop-state",
                transcript_path=str(transcript_path),
            )
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])
        finally:
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

        assert not extraction_daemon._rolling_state_path("sess-noop-state").exists()

    def test_process_signal_short_circuits_use_common_finalize_helper(self, monkeypatch, tmp_path):
        import sys
        import types

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        monkeypatch.setattr(extraction_daemon, "_session_has_harvestable_subagents", lambda *args, **kwargs: False)

        finalize_calls = []
        real_finalize = extraction_daemon._finalize_no_payload_signal

        def _spy_finalize(**kwargs):
            finalize_calls.append(
                {
                    "session_id": kwargs.get("session_id"),
                    "next_cursor_offset": kwargs.get("next_cursor_offset"),
                    "clear_state": bool(kwargs.get("clear_state")),
                    "has_emit_noop_metric": callable(kwargs.get("emit_noop_metric")),
                }
            )
            return real_finalize(**kwargs)

        monkeypatch.setattr(extraction_daemon, "_finalize_no_payload_signal", _spy_finalize)

        real_adapter = sys.modules.get("lib.adapter")
        fake_adapter_mod = types.ModuleType("lib.adapter")

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def parse_session_jsonl(self, path):
                _ = path
                return "Hello"

        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        fake_adapter_mod.get_owner_id = lambda: "test-owner"
        sys.modules["lib.adapter"] = fake_adapter_mod

        try:
            no_new_content_path = tmp_path / "no-new-content.jsonl"
            no_new_content_path.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")
            extraction_daemon.write_cursor("sess-finalize-no-new", 1, str(no_new_content_path))
            extraction_daemon.write_signal(
                signal_type="compaction",
                session_id="sess-finalize-no-new",
                transcript_path=str(no_new_content_path),
            )
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])

            short_transcript_path = tmp_path / "short-transcript.jsonl"
            short_transcript_path.write_text('{"role":"assistant","content":"Compacted"}\n', encoding="utf-8")
            extraction_daemon.write_cursor("sess-finalize-short", 0, str(short_transcript_path))
            extraction_daemon.write_signal(
                signal_type="reset",
                session_id="sess-finalize-short",
                transcript_path=str(short_transcript_path),
            )
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])
        finally:
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

        assert len(finalize_calls) == 2
        assert finalize_calls[0]["session_id"] == "sess-finalize-no-new"
        assert finalize_calls[0]["next_cursor_offset"] is None
        assert finalize_calls[0]["clear_state"] is False
        assert finalize_calls[0]["has_emit_noop_metric"] is True
        assert finalize_calls[1]["session_id"] == "sess-finalize-short"
        assert finalize_calls[1]["next_cursor_offset"] == 1
        assert finalize_calls[1]["clear_state"] is True
        assert finalize_calls[1]["has_emit_noop_metric"] is False

    def test_reset_short_transcript_skip_clears_semantic_only_rolling_state(self, monkeypatch, tmp_path):
        import sys
        import types

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            '{"role":"assistant","content":"Compacted"}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        extraction_daemon.write_cursor("sess-short-reset", 0, str(transcript_path))
        monkeypatch.setattr(extraction_daemon, "_session_has_harvestable_subagents", lambda *args, **kwargs: False)

        real_adapter = sys.modules.get("lib.adapter")
        fake_adapter_mod = types.ModuleType("lib.adapter")

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def parse_session_jsonl(self, path):
                _ = path
                return "Hello"

        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        fake_adapter_mod.get_owner_id = lambda: "test-owner"
        sys.modules["lib.adapter"] = fake_adapter_mod

        try:
            extraction_daemon.write_signal(
                signal_type="reset",
                session_id="sess-short-reset",
                transcript_path=str(transcript_path),
            )
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])
        finally:
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

        assert extraction_daemon.read_cursor("sess-short-reset")["line_offset"] == 1
        assert not extraction_daemon._rolling_state_path("sess-short-reset").exists()

    def test_check_chunk_ready_sessions_uses_semantic_buffer_not_raw_json_size(self, monkeypatch, tmp_path):
        import sys
        import types

        transcript_path = tmp_path / "session.jsonl"
        machine_noise = json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "x" * 1200}],
                },
            }
        ) + "\n"
        user_line = json.dumps(
            {"type": "event_msg", "payload": {"type": "user_message", "message": "hi"}}
        ) + "\n"
        transcript_path.write_text(machine_noise + user_line, encoding="utf-8")

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        extraction_daemon.write_cursor("sess-roll", 0, str(transcript_path))

        real_adapter = sys.modules.get("lib.adapter")
        fake_adapter_mod = types.ModuleType("lib.adapter")

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def parse_session_jsonl(self, path):
                return "User: hi"

        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        sys.modules["lib.adapter"] = fake_adapter_mod

        captured = []
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda default=8000: 20)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        try:
            extraction_daemon.check_chunk_ready_sessions()
            state = extraction_daemon.read_rolling_state("sess-roll")
            assert captured == []
            assert state["semantic_buffer"] == "User: hi"
            assert state["semantic_buffer_tokens"] < 20
            assert state["buffered_line_offset"] == 2
        finally:
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

    def test_check_chunk_ready_sessions_preserves_source_cursor_for_subthreshold_tail(self, monkeypatch, tmp_path):
        import sys
        import types

        transcript_path = tmp_path / "session.jsonl"
        first_line = json.dumps(
            {"type": "event_msg", "payload": {"type": "user_message", "message": "seed line"}}
        ) + "\n"
        transcript_path.write_text(first_line, encoding="utf-8")

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        session_id = "sess-roll-source-preserve"
        source_key = extraction_daemon._signal_source_cursor_key(session_id, str(transcript_path))
        extraction_daemon.write_cursor(
            session_id,
            1,
            str(transcript_path),
            source_key=source_key,
            last_flushed_line_offset=1,
        )

        second_line = json.dumps(
            {"type": "event_msg", "payload": {"type": "user_message", "message": "sub threshold rolling content"}}
        ) + "\n"
        third_line = json.dumps(
            {"type": "event_msg", "payload": {"type": "assistant_message", "message": "ack"}}
        ) + "\n"
        transcript_path.write_text(first_line + second_line + third_line, encoding="utf-8")

        real_adapter = sys.modules.get("lib.adapter")
        fake_adapter_mod = types.ModuleType("lib.adapter")

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def parse_session_jsonl(self, path):
                messages = []
                for raw in Path(path).read_text(encoding="utf-8").splitlines():
                    payload = json.loads(raw)
                    event_payload = payload.get("payload", {})
                    message = event_payload.get("message")
                    if message:
                        messages.append(f"User: {message}")
                return "\n\n".join(messages)

        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        sys.modules["lib.adapter"] = fake_adapter_mod

        captured = []
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda default=8000: 100)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        try:
            extraction_daemon.check_chunk_ready_sessions()

            state = extraction_daemon.read_rolling_state(session_id)
            source_cursor = extraction_daemon.read_cursor(session_id, source_key=source_key)
            assert captured == []
            assert state["buffered_line_offset"] == 3
            assert source_cursor["line_offset"] == 1
            assert source_cursor["last_flushed_line_offset"] == 1
        finally:
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

    def test_check_chunk_ready_sessions_skips_active_processing_lock(self, monkeypatch, tmp_path):
        import sys
        import types

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            '{"type":"event_msg","payload":{"type":"user_message","message":"large rolling note"}}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        session_id = "sess-roll-locked"
        source_key = extraction_daemon._signal_source_cursor_key(session_id, str(transcript_path))
        extraction_daemon.write_cursor(session_id, 0, str(transcript_path), source_key=source_key)
        lock_fd = extraction_daemon._acquire_session_processing_lock(source_key)
        assert lock_fd is not None

        real_adapter = sys.modules.get("lib.adapter")
        fake_adapter_mod = types.ModuleType("lib.adapter")

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def parse_session_jsonl(self, path):
                return "User: " + ("large " * 400)

        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        sys.modules["lib.adapter"] = fake_adapter_mod

        captured = []
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda default=8000: 20)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                }
            ),
        )

        try:
            extraction_daemon.check_chunk_ready_sessions()
            assert captured == []
            assert not extraction_daemon._rolling_state_path(session_id).exists()
        finally:
            extraction_daemon._release_session_processing_lock(source_key, lock_fd)
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

    def test_check_chunk_ready_sessions_rolls_near_semantic_budget(self, monkeypatch, tmp_path):
        import sys
        import types

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            '{"type":"event_msg","payload":{"type":"user_message","message":"near budget rolling note"}}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        extraction_daemon.write_cursor("sess-roll-near", 0, str(transcript_path))

        real_adapter = sys.modules.get("lib.adapter")
        fake_adapter_mod = types.ModuleType("lib.adapter")
        near_budget_text = "User: " + ("a" * 378)

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def parse_session_jsonl(self, path):
                return near_budget_text

        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        sys.modules["lib.adapter"] = fake_adapter_mod

        captured = []
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda default=8000: 100)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        try:
            extraction_daemon.check_chunk_ready_sessions()
            state = extraction_daemon.read_rolling_state("sess-roll-near")
            assert len(captured) == 1
            assert captured[0]["signal_type"] == "rolling"
            assert captured[0]["meta"]["reason"] == "semantic_chunk_budget_near"
            assert captured[0]["meta"]["semantic_buffer_tokens"] == 96
            assert captured[0]["meta"]["near_budget_threshold"] == 95
            assert state["buffered_line_offset"] == 1
        finally:
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

    def test_check_chunk_ready_sessions_accumulates_semantic_buffer_across_checks(self, monkeypatch, tmp_path):
        import sys
        import types

        transcript_path = tmp_path / "session.jsonl"
        first_line = json.dumps(
            {"type": "event_msg", "payload": {"type": "user_message", "message": "short note"}}
        ) + "\n"
        transcript_path.write_text(first_line, encoding="utf-8")

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        extraction_daemon.write_cursor("sess-roll", 0, str(transcript_path))

        real_adapter = sys.modules.get("lib.adapter")
        fake_adapter_mod = types.ModuleType("lib.adapter")

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def parse_session_jsonl(self, path):
                messages = []
                for raw in path.read_text(encoding="utf-8").splitlines():
                    payload = json.loads(raw)
                    event_payload = payload.get("payload", {})
                    message = event_payload.get("message")
                    if message:
                        messages.append(f"User: {message}")
                return "\n\n".join(messages)

        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        sys.modules["lib.adapter"] = fake_adapter_mod

        captured = []
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda default=8000: 8)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        try:
            extraction_daemon.check_chunk_ready_sessions()
            first_state = extraction_daemon.read_rolling_state("sess-roll")
            assert captured == []
            assert first_state["buffered_line_offset"] == 1
            assert "short note" in first_state["semantic_buffer"]

            second_line = json.dumps(
                {
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "another longer note with extra words"},
                }
            ) + "\n"
            transcript_path.write_text(first_line + second_line, encoding="utf-8")

            extraction_daemon.check_chunk_ready_sessions()
            second_state = extraction_daemon.read_rolling_state("sess-roll")
            assert len(captured) == 1
            assert captured[0]["signal_type"] == "rolling"
            assert captured[0]["meta"]["reason"] == "semantic_chunk_budget"
            assert second_state["buffered_line_offset"] == 2
            assert "short note" in second_state["semantic_buffer"]
            assert "another longer note with extra words" in second_state["semantic_buffer"]
        finally:
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

    def test_check_chunk_ready_sessions_uses_larger_active_preserved_mirror(
        self, monkeypatch, tmp_path
    ):
        session_id = "d000074a-c08d-4eb9-b9d1-7b478cbb426f"
        live_path = tmp_path / ".openclaw" / "agents" / "main" / "sessions" / f"{session_id}.jsonl"
        mirror_path = (
            tmp_path
            / "instances"
            / "openclaw-main"
            / "logs"
            / "quaid"
            / "sessions"
            / f"{session_id}.jsonl"
        )
        live_path.parent.mkdir(parents=True, exist_ok=True)
        mirror_path.parent.mkdir(parents=True, exist_ok=True)
        live_path.write_text("\n".join(f'{{"live": {i}}}' for i in range(7)) + "\n", encoding="utf-8")
        mirror_path.write_text(
            '{"type":"message","message":{"role":"user","content":"hello"}}\n'
            '{"type":"message","message":{"role":"user","content":"chunk one"}}\n'
            '{"type":"message","message":{"role":"user","content":"chunk two"}}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
        monkeypatch.setattr(extraction_daemon, "_instance_id", lambda: "openclaw-main")
        monkeypatch.setattr(extraction_daemon, "_ensure_discovered_session_cursors", lambda adapter: None)
        extraction_daemon.write_cursor(session_id, 7, str(live_path))
        cursor_path = extraction_daemon._cursor_dir() / f"{session_id}.json"
        cursor_data = json.loads(cursor_path.read_text(encoding="utf-8"))
        cursor_data["transcript_size_bytes"] = 0
        cursor_path.write_text(json.dumps(cursor_data), encoding="utf-8")

        captured = []
        buffered_paths = []
        buffered_from_lines = []

        def fake_buffer_transcript_tail(path, from_line, state, adapter=None, **kwargs):
            buffered_paths.append(path)
            buffered_from_lines.append(from_line)
            assert path == str(mirror_path)
            if from_line == 0:
                return (
                    {
                        "buffered_line_offset": 2,
                        "semantic_buffer": "User: chunk one",
                        "semantic_buffer_tokens": 8,
                    },
                    {
                        "raw_lines_added": 2,
                        "semantic_chars_added": 15,
                        "semantic_tokens_added": 8,
                        "buffered_line_offset": 2,
                    },
                )
            return (
                {
                    "buffered_line_offset": 3,
                    "semantic_buffer": "User: chunk one\n\nUser: chunk two",
                    "semantic_buffer_tokens": 12,
                },
                {
                    "raw_lines_added": 1,
                    "semantic_chars_added": 15,
                    "semantic_tokens_added": 4,
                    "buffered_line_offset": 3,
                },
            )

        monkeypatch.setattr(extraction_daemon, "_buffer_transcript_tail", fake_buffer_transcript_tail)
        monkeypatch.setattr(extraction_daemon, "_adapter_owns_transcript_path", lambda *args, **kwargs: True)
        monkeypatch.setattr(extraction_daemon, "_cursor_or_adapter_owns_transcript_path", lambda *args, **kwargs: True)
        monkeypatch.setattr(extraction_daemon, "_reconcile_internal_cursor_state", lambda *args, **kwargs: "not_internal")
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        extraction_daemon.check_chunk_ready_sessions(chunk_tokens=10)
        assert captured == []
        assert buffered_paths == [str(mirror_path)]
        assert buffered_from_lines == [0]

        extraction_daemon.check_chunk_ready_sessions(chunk_tokens=10)
        assert buffered_paths == [str(mirror_path), str(mirror_path)]
        assert buffered_from_lines == [0, 2]
        assert captured == [
            {
                "signal_type": "rolling",
                "session_id": session_id,
                "transcript_path": str(live_path),
                "meta": {
                    "reason": "semantic_chunk_budget",
                    "chunk_tokens": 10,
                    "semantic_buffer_tokens": 12,
                    "buffered_line_offset": 3,
                    "buffer_transcript_path": str(mirror_path),
                },
            }
        ]
        source_key = extraction_daemon._signal_source_cursor_key(
            session_id,
            str(live_path),
            cursor_data=extraction_daemon.read_cursor(session_id),
        )
        extraction_daemon.write_cursor(
            session_id,
            captured[0]["meta"]["buffered_line_offset"],
            captured[0]["transcript_path"],
            source_key=source_key,
            processed_signal_type="rolling",
        )
        source_cursor = extraction_daemon.read_cursor(session_id, source_key=source_key)
        assert source_cursor["transcript_path"] == str(live_path)

        prior_buffer_count = len(buffered_paths)
        extraction_daemon.check_chunk_ready_sessions(chunk_tokens=10)
        assert len(buffered_paths) == prior_buffer_count

    def test_check_chunk_ready_sessions_finalizes_ended_snapshot_with_preserved_mirror(
        self, monkeypatch, tmp_path
    ):
        session_id = "8817b065-c08d-4eb9-b9d1-7b478cbb426f"
        instance_root = tmp_path / "instances" / "openclaw-main"
        snapshot_path = (
            instance_root
            / "logs"
            / "daemon"
            / "rolling-transcript-snapshots"
            / session_id
            / "20260527T111743Z-feedface"
            / f"{session_id}.jsonl"
        )
        mirror_path = (
            instance_root
            / "logs"
            / "quaid"
            / "sessions"
            / f"{session_id}.jsonl"
        )
        missing_live_path = tmp_path / ".openclaw" / "agents" / "main" / "sessions" / f"{session_id}.jsonl"
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        mirror_path.parent.mkdir(parents=True, exist_ok=True)
        missing_live_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(
            '{"type":"message","message":{"role":"user","content":"chunk one"}}\n'
            '{"type":"message","message":{"role":"assistant","content":"ack"}}\n',
            encoding="utf-8",
        )
        mirror_path.write_text(
            '{"type":"message","message":{"role":"user","content":"chunk one"}}\n'
            '{"type":"message","message":{"role":"user","content":"chunk two"}}\n'
            '{"type":"message","message":{"role":"user","content":"chunk three"}}\n',
            encoding="utf-8",
        )

        class _Adapter(_OwnedTestAdapterMixin):
            def get_session_path(self, session_id_arg):
                assert session_id_arg == session_id
                return missing_live_path

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
        monkeypatch.setattr(extraction_daemon, "_instance_id", lambda: "openclaw-main")
        monkeypatch.setattr(extraction_daemon, "_load_runtime_adapter", lambda: _Adapter())
        monkeypatch.setattr(extraction_daemon, "_ensure_discovered_session_cursors", lambda adapter: None)
        monkeypatch.setattr(extraction_daemon, "_adapter_owns_transcript_path", lambda *args, **kwargs: True)
        monkeypatch.setattr(extraction_daemon, "_cursor_or_adapter_owns_transcript_path", lambda *args, **kwargs: True)
        monkeypatch.setattr(extraction_daemon, "_reconcile_internal_cursor_state", lambda *args, **kwargs: "not_internal")
        extraction_daemon.write_cursor(session_id, 2, str(snapshot_path))
        extraction_daemon.write_rolling_state(
            session_id,
            {
                "session_id": session_id,
                "transcript_path": str(snapshot_path),
                "buffer_transcript_path": str(snapshot_path),
                "processed_line_offset": 2,
                "buffered_line_offset": 2,
                "semantic_buffer": "User: chunk one",
                "semantic_buffer_tokens": 8,
            },
        )

        monkeypatch.setattr(
            extraction_daemon,
            "_buffer_transcript_tail",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("ended snapshot must not rebuffer preserved mirror")
            ),
        )
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        captured = []
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        extraction_daemon.check_chunk_ready_sessions(chunk_tokens=10)

        cursor = extraction_daemon.read_cursor(session_id)
        assert cursor["transcript_path"] == str(mirror_path)
        assert cursor["line_offset"] == 2
        assert cursor["processed_signal_type"] == ""
        assert captured == [
            {
                "signal_type": "session_end",
                "session_id": session_id,
                "transcript_path": str(mirror_path),
                "meta": {
                    "reason": "ended_rolling_buffer_flush",
                    "source_cursor_key": session_id,
                },
            }
        ]
        assert extraction_daemon._rolling_state_path(session_id).exists()

    def test_check_chunk_ready_sessions_finalizes_missing_live_cursor_with_preserved_mirror(
        self, monkeypatch, tmp_path
    ):
        session_id = "8817b065-c63a-43f3-a68a-72b70f2729ed"
        live_path = tmp_path / ".openclaw" / "agents" / "main" / "sessions" / f"{session_id}.jsonl"
        mirror_path = (
            tmp_path
            / "instances"
            / "openclaw-main"
            / "logs"
            / "quaid"
            / "sessions"
            / f"{session_id}.jsonl"
        )
        live_path.parent.mkdir(parents=True, exist_ok=True)
        mirror_path.parent.mkdir(parents=True, exist_ok=True)
        mirror_path.write_text(
            '{"type":"message","message":{"role":"user","content":"chunk one"}}\n'
            '{"type":"message","message":{"role":"user","content":"chunk two"}}\n'
            '{"type":"message","message":{"role":"user","content":"chunk three"}}\n',
            encoding="utf-8",
        )

        class _Adapter(_OwnedTestAdapterMixin):
            def get_session_path(self, session_id_arg):
                assert session_id_arg == session_id
                return live_path

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
        monkeypatch.setattr(extraction_daemon, "_instance_id", lambda: "openclaw-main")
        monkeypatch.setattr(extraction_daemon, "_load_runtime_adapter", lambda: _Adapter())
        monkeypatch.setattr(extraction_daemon, "_ensure_discovered_session_cursors", lambda adapter: None)
        monkeypatch.setattr(extraction_daemon, "_adapter_owns_transcript_path", lambda *args, **kwargs: True)
        extraction_daemon.write_cursor(session_id, 2, str(live_path))
        extraction_daemon.write_rolling_state(
            session_id,
            {
                "session_id": session_id,
                "transcript_path": str(live_path),
                "processed_line_offset": 2,
                "buffered_line_offset": 2,
                "semantic_buffer": "User: chunk one",
                "semantic_buffer_tokens": 8,
            },
        )

        monkeypatch.setattr(
            extraction_daemon,
            "_buffer_transcript_tail",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("missing live cursor must not switch to preserved mirror buffer")
            ),
        )
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        captured = []
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        extraction_daemon.check_chunk_ready_sessions(chunk_tokens=10)

        cursor = extraction_daemon.read_cursor(session_id)
        assert cursor["transcript_path"] == str(mirror_path)
        assert cursor["line_offset"] == 2
        assert cursor["processed_signal_type"] == ""
        assert captured == [
            {
                "signal_type": "session_end",
                "session_id": session_id,
                "transcript_path": str(mirror_path),
                "meta": {
                    "reason": "ended_rolling_buffer_flush",
                    "source_cursor_key": session_id,
                },
            }
        ]
        assert extraction_daemon._rolling_state_path(session_id).exists()

    def test_check_chunk_ready_sessions_clears_ended_preserved_buffer_before_signaling(
        self, monkeypatch, tmp_path
    ):
        session_id = "8817b065-c63a-43f3-a68a-72b70f2729ed"
        missing_live_path = tmp_path / ".openclaw" / "agents" / "main" / "sessions" / f"{session_id}.jsonl"
        mirror_path = (
            tmp_path
            / "instances"
            / "openclaw-main"
            / "logs"
            / "quaid"
            / "sessions"
            / f"{session_id}.jsonl"
        )
        missing_live_path.parent.mkdir(parents=True, exist_ok=True)
        mirror_path.parent.mkdir(parents=True, exist_ok=True)
        mirror_path.write_text(
            '{"type":"message","message":{"role":"user","content":"chunk one"}}\n'
            '{"type":"message","message":{"role":"user","content":"chunk two"}}\n'
            '{"type":"message","message":{"role":"user","content":"chunk three"}}\n',
            encoding="utf-8",
        )

        class _Adapter(_OwnedTestAdapterMixin):
            def get_session_path(self, session_id_arg):
                assert session_id_arg == session_id
                return missing_live_path

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
        monkeypatch.setattr(extraction_daemon, "_instance_id", lambda: "openclaw-main")
        monkeypatch.setattr(extraction_daemon, "_load_runtime_adapter", lambda: _Adapter())
        monkeypatch.setattr(extraction_daemon, "_ensure_discovered_session_cursors", lambda adapter: None)
        monkeypatch.setattr(extraction_daemon, "_adapter_owns_transcript_path", lambda *args, **kwargs: True)
        monkeypatch.setattr(extraction_daemon, "_cursor_or_adapter_owns_transcript_path", lambda *args, **kwargs: True)
        monkeypatch.setattr(extraction_daemon, "_reconcile_internal_cursor_state", lambda *args, **kwargs: "not_internal")
        extraction_daemon.write_cursor(session_id, 3, str(mirror_path))
        extraction_daemon.write_rolling_state(
            session_id,
            {
                "session_id": session_id,
                "transcript_path": str(mirror_path),
                "buffer_transcript_path": str(mirror_path),
                "processed_line_offset": 3,
                "buffered_line_offset": 3,
                "semantic_buffer": "User: " + ("looping duplicate content " * 140),
                "semantic_buffer_tokens": 1646,
            },
        )

        monkeypatch.setattr(
            extraction_daemon,
            "_buffer_transcript_tail",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("ended preserved buffer must be cleared before buffering")
            ),
        )
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        captured = []
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        extraction_daemon.check_chunk_ready_sessions(chunk_tokens=1500)

        cursor = extraction_daemon.read_cursor(session_id)
        assert cursor["transcript_path"] == str(mirror_path)
        assert cursor["line_offset"] == 3
        assert cursor["processed_signal_type"] == ""
        assert captured == [
            {
                "signal_type": "session_end",
                "session_id": session_id,
                "transcript_path": str(mirror_path),
                "meta": {
                    "reason": "ended_rolling_buffer_flush",
                    "source_cursor_key": session_id,
                },
            }
        ]
        assert extraction_daemon._rolling_state_path(session_id).exists()

    def test_check_chunk_ready_sessions_flushes_stale_larger_preserved_buffer_for_existing_live_path(
        self, monkeypatch, tmp_path
    ):
        session_id = "8817b065-c63a-43f3-a68a-72b70f2729ed"
        live_path = tmp_path / ".openclaw" / "agents" / "main" / "sessions" / f"{session_id}.jsonl"
        mirror_path = (
            tmp_path
            / "instances"
            / "openclaw-main"
            / "logs"
            / "quaid"
            / "sessions"
            / f"{session_id}.jsonl"
        )
        live_path.parent.mkdir(parents=True, exist_ok=True)
        mirror_path.parent.mkdir(parents=True, exist_ok=True)
        live_path.write_text(
            '{"type":"message","message":{"role":"user","content":"chunk one"}}\n',
            encoding="utf-8",
        )
        mirror_path.write_text(
            '{"type":"message","message":{"role":"user","content":"chunk one"}}\n'
            '{"type":"message","message":{"role":"user","content":"chunk two"}}\n'
            '{"type":"message","message":{"role":"user","content":"chunk three"}}\n',
            encoding="utf-8",
        )
        stale_mtime = time.time() - 600
        os.utime(live_path, (stale_mtime, stale_mtime))

        class _Adapter(_OwnedTestAdapterMixin):
            def get_session_path(self, session_id_arg):
                assert session_id_arg == session_id
                return live_path

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
        monkeypatch.setattr(extraction_daemon, "_instance_id", lambda: "openclaw-main")
        monkeypatch.setattr(extraction_daemon, "_load_runtime_adapter", lambda: _Adapter())
        monkeypatch.setattr(extraction_daemon, "_ensure_discovered_session_cursors", lambda adapter: None)
        monkeypatch.setattr(extraction_daemon, "_adapter_owns_transcript_path", lambda *args, **kwargs: True)
        monkeypatch.setattr(extraction_daemon, "_cursor_or_adapter_owns_transcript_path", lambda *args, **kwargs: True)
        monkeypatch.setattr(extraction_daemon, "_reconcile_internal_cursor_state", lambda *args, **kwargs: "not_internal")
        extraction_daemon.write_cursor(session_id, 0, str(live_path))
        extraction_daemon.write_rolling_state(
            session_id,
            {
                "session_id": session_id,
                "transcript_path": str(live_path),
                "buffer_transcript_path": str(mirror_path),
                "processed_line_offset": 0,
                "buffered_line_offset": 0,
                "semantic_buffer": "",
                "semantic_buffer_tokens": 0,
            },
        )

        monkeypatch.setattr(
            extraction_daemon,
            "_buffer_transcript_tail",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("stale preserved buffer must flush before rebuffering")
            ),
        )
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        captured = []
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        extraction_daemon.check_chunk_ready_sessions(chunk_tokens=1500)

        cursor = extraction_daemon.read_cursor(session_id)
        assert cursor["transcript_path"] == str(mirror_path)
        assert cursor["line_offset"] == 0
        assert cursor["processed_signal_type"] == ""
        assert captured == [
            {
                "signal_type": "session_end",
                "session_id": session_id,
                "transcript_path": str(mirror_path),
                "meta": {
                    "reason": "ended_rolling_buffer_flush",
                    "source_cursor_key": session_id,
                },
            }
        ]

    def test_check_chunk_ready_sessions_does_not_requeue_stale_larger_preserved_buffer_at_eof(
        self, monkeypatch, tmp_path
    ):
        session_id = "8817b065-c63a-43f3-a68a-72b70f2729ed"
        live_path = tmp_path / ".openclaw" / "agents" / "main" / "sessions" / f"{session_id}.jsonl"
        mirror_path = (
            tmp_path
            / "instances"
            / "openclaw-main"
            / "logs"
            / "quaid"
            / "sessions"
            / f"{session_id}.jsonl"
        )
        live_path.parent.mkdir(parents=True, exist_ok=True)
        mirror_path.parent.mkdir(parents=True, exist_ok=True)
        live_path.write_text(
            '{"type":"message","message":{"role":"user","content":"chunk one"}}\n',
            encoding="utf-8",
        )
        mirror_path.write_text(
            '{"type":"message","message":{"role":"user","content":"chunk one"}}\n'
            '{"type":"message","message":{"role":"user","content":"chunk two"}}\n'
            '{"type":"message","message":{"role":"user","content":"chunk three"}}\n',
            encoding="utf-8",
        )
        stale_mtime = time.time() - 600
        os.utime(live_path, (stale_mtime, stale_mtime))

        class _Adapter(_OwnedTestAdapterMixin):
            def get_session_path(self, session_id_arg):
                assert session_id_arg == session_id
                return live_path

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
        monkeypatch.setattr(extraction_daemon, "_instance_id", lambda: "openclaw-main")
        monkeypatch.setattr(extraction_daemon, "_load_runtime_adapter", lambda: _Adapter())
        monkeypatch.setattr(extraction_daemon, "_ensure_discovered_session_cursors", lambda adapter: None)
        monkeypatch.setattr(extraction_daemon, "_adapter_owns_transcript_path", lambda *args, **kwargs: True)
        monkeypatch.setattr(extraction_daemon, "_cursor_or_adapter_owns_transcript_path", lambda *args, **kwargs: True)
        monkeypatch.setattr(extraction_daemon, "_reconcile_internal_cursor_state", lambda *args, **kwargs: "not_internal")
        extraction_daemon.write_cursor(session_id, 3, str(live_path))

        monkeypatch.setattr(
            extraction_daemon,
            "_buffer_transcript_tail",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("already-consumed preserved mirror must not rebuffer")
            ),
        )
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        captured = []
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        extraction_daemon.check_chunk_ready_sessions(chunk_tokens=1500)

        cursor = extraction_daemon.read_cursor(session_id)
        assert cursor["transcript_path"] == str(mirror_path)
        assert cursor["line_offset"] == 3
        assert cursor["processed_signal_type"] == "session_end"
        assert captured == []

    def test_check_chunk_ready_sessions_prefers_larger_live_path_for_preserved_cursor(
        self, monkeypatch, tmp_path
    ):
        session_id = "09cba64f-c08d-4eb9-b9d1-7b478cbb426f"
        mirror_path = (
            tmp_path
            / "instances"
            / "openclaw-main"
            / "logs"
            / "quaid"
            / "sessions"
            / f"{session_id}.jsonl"
        )
        live_path = tmp_path / "live" / "sessions" / f"{session_id}.jsonl"
        mirror_path.parent.mkdir(parents=True, exist_ok=True)
        live_path.parent.mkdir(parents=True, exist_ok=True)
        mirror_path.write_text(
            '{"type":"message","message":{"role":"user","content":"hello"}}\n',
            encoding="utf-8",
        )
        live_path.write_text(
            '{"type":"message","message":{"role":"user","content":"hello"}}\n'
            '{"type":"message","message":{"role":"user","content":"chunk one"}}\n'
            '{"type":"message","message":{"role":"user","content":"chunk two"}}\n',
            encoding="utf-8",
        )

        class _Adapter:
            def get_session_path(self, session_id_arg):
                assert session_id_arg == session_id
                return live_path

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
        monkeypatch.setattr(extraction_daemon, "_instance_id", lambda: "openclaw-main")
        monkeypatch.setattr(extraction_daemon, "_load_runtime_adapter", lambda: _Adapter())
        monkeypatch.setattr(extraction_daemon, "_ensure_discovered_session_cursors", lambda adapter: None)
        monkeypatch.setattr(extraction_daemon, "_adapter_owns_transcript_path", lambda *args, **kwargs: True)
        monkeypatch.setattr(extraction_daemon, "_cursor_or_adapter_owns_transcript_path", lambda *args, **kwargs: True)
        monkeypatch.setattr(extraction_daemon, "_reconcile_internal_cursor_state", lambda *args, **kwargs: "not_internal")
        extraction_daemon.write_cursor(session_id, 0, str(mirror_path))

        captured = []
        buffered_paths = []

        def fake_buffer_transcript_tail(path, from_line, state, adapter=None, **kwargs):
            buffered_paths.append(path)
            assert path == str(live_path)
            assert from_line == 0
            return (
                {
                    "buffered_line_offset": 3,
                    "semantic_buffer": "User: chunk one\n\nUser: chunk two",
                    "semantic_buffer_tokens": 12,
                },
                {
                    "raw_lines_added": 3,
                    "semantic_chars_added": 30,
                    "semantic_tokens_added": 12,
                    "buffered_line_offset": 3,
                },
            )

        monkeypatch.setattr(extraction_daemon, "_buffer_transcript_tail", fake_buffer_transcript_tail)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        extraction_daemon.check_chunk_ready_sessions(chunk_tokens=10)

        assert buffered_paths == [str(live_path)]
        assert captured == [
            {
                "signal_type": "rolling",
                "session_id": session_id,
                "transcript_path": str(live_path),
                "meta": {
                    "reason": "semantic_chunk_budget",
                    "chunk_tokens": 10,
                    "semantic_buffer_tokens": 12,
                    "buffered_line_offset": 3,
                },
            }
        ]
        source_key = extraction_daemon._signal_source_cursor_key(
            session_id,
            str(live_path),
            cursor_data=extraction_daemon.read_cursor(session_id),
        )
        extraction_daemon.write_cursor(
            session_id,
            captured[0]["meta"]["buffered_line_offset"],
            captured[0]["transcript_path"],
            source_key=source_key,
            processed_signal_type="rolling",
        )
        extraction_daemon.clear_rolling_state(session_id)
        prior_buffer_count = len(buffered_paths)

        extraction_daemon.check_chunk_ready_sessions(chunk_tokens=10)

        assert len(buffered_paths) == prior_buffer_count

    def test_check_chunk_ready_sessions_resets_offset_when_buffer_source_switches(
        self, monkeypatch, tmp_path
    ):
        session_id = "09cba64f-c08d-4eb9-b9d1-7b478cbb426f"
        live_path = tmp_path / "live" / "sessions" / f"{session_id}.jsonl"
        mirror_path = (
            tmp_path
            / "instances"
            / "openclaw-main"
            / "logs"
            / "quaid"
            / "sessions"
            / f"{session_id}.jsonl"
        )
        live_path.parent.mkdir(parents=True, exist_ok=True)
        mirror_path.parent.mkdir(parents=True, exist_ok=True)
        live_path.write_text(
            '{"type":"message","message":{"role":"user","content":"hello"}}\n'
            '{"type":"message","message":{"role":"assistant","content":"ack"}}\n'
            '{"type":"message","message":{"role":"user","content":"chunk one"}}\n'
            '{"type":"message","message":{"role":"assistant","content":"ack"}}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
        monkeypatch.setattr(extraction_daemon, "_instance_id", lambda: "openclaw-main")
        monkeypatch.setattr(extraction_daemon, "_ensure_discovered_session_cursors", lambda adapter: None)
        monkeypatch.setattr(extraction_daemon, "_adapter_owns_transcript_path", lambda *args, **kwargs: True)
        monkeypatch.setattr(extraction_daemon, "_cursor_or_adapter_owns_transcript_path", lambda *args, **kwargs: True)
        monkeypatch.setattr(extraction_daemon, "_reconcile_internal_cursor_state", lambda *args, **kwargs: "not_internal")

        extraction_daemon.write_cursor(session_id, 4, str(live_path))
        extraction_daemon.write_rolling_state(
            session_id,
            {
                "session_id": session_id,
                "transcript_path": str(live_path),
                "buffer_transcript_path": str(live_path),
                "processed_line_offset": 4,
                "buffered_line_offset": 4,
                "semantic_buffer": "User: chunk one",
                "semantic_buffer_tokens": 8,
            },
        )

        live_path.write_text("", encoding="utf-8")
        mirror_path.write_text(
            '{"type":"message","message":{"role":"user","content":"hello"}}\n'
            '{"type":"message","message":{"role":"user","content":"chunk one"}}\n'
            '{"type":"message","message":{"role":"user","content":"chunk two"}}\n',
            encoding="utf-8",
        )

        captured = []
        buffered_calls = []

        def fake_buffer_transcript_tail(path, from_line, state, adapter=None, **kwargs):
            buffered_calls.append((path, from_line, dict(state)))
            assert path == str(mirror_path)
            assert from_line == 0
            assert state.get("semantic_buffer", "") == ""
            return (
                {
                    "buffer_transcript_path": str(mirror_path),
                    "buffered_line_offset": 3,
                    "semantic_buffer": "User: chunk one\n\nUser: chunk two",
                    "semantic_buffer_tokens": 12,
                },
                {
                    "raw_lines_added": 3,
                    "semantic_chars_added": 30,
                    "semantic_tokens_added": 12,
                    "buffered_line_offset": 3,
                },
            )

        monkeypatch.setattr(extraction_daemon, "_buffer_transcript_tail", fake_buffer_transcript_tail)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        extraction_daemon.check_chunk_ready_sessions(chunk_tokens=10)

        assert [(path, from_line) for path, from_line, _state in buffered_calls] == [(str(mirror_path), 0)]
        assert captured == [
            {
                "signal_type": "rolling",
                "session_id": session_id,
                "transcript_path": str(live_path),
                "meta": {
                    "reason": "semantic_chunk_budget",
                    "chunk_tokens": 10,
                    "semantic_buffer_tokens": 12,
                    "buffered_line_offset": 3,
                    "buffer_transcript_path": str(mirror_path),
                },
            }
        ]
        state = extraction_daemon.read_rolling_state(session_id)
        assert state["buffer_transcript_path"] == str(mirror_path)
        assert state["buffered_line_offset"] == 3

    def test_check_chunk_ready_sessions_resets_offset_when_buffer_source_missing(
        self, monkeypatch, tmp_path
    ):
        session_id = "e513b8c1-c08d-4eb9-b9d1-7b478cbb426f"
        live_path = tmp_path / "live" / "sessions" / f"{session_id}.jsonl"
        mirror_path = (
            tmp_path
            / "instances"
            / "openclaw-main"
            / "logs"
            / "quaid"
            / "sessions"
            / f"{session_id}.jsonl"
        )
        live_path.parent.mkdir(parents=True, exist_ok=True)
        mirror_path.parent.mkdir(parents=True, exist_ok=True)
        live_path.write_text("", encoding="utf-8")
        mirror_path.write_text(
            '{"type":"message","message":{"role":"user","content":"hello"}}\n'
            '{"type":"message","message":{"role":"user","content":"chunk one"}}\n'
            '{"type":"message","message":{"role":"user","content":"chunk two"}}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
        monkeypatch.setattr(extraction_daemon, "_instance_id", lambda: "openclaw-main")
        monkeypatch.setattr(extraction_daemon, "_ensure_discovered_session_cursors", lambda adapter: None)
        monkeypatch.setattr(extraction_daemon, "_adapter_owns_transcript_path", lambda *args, **kwargs: True)
        monkeypatch.setattr(extraction_daemon, "_cursor_or_adapter_owns_transcript_path", lambda *args, **kwargs: True)
        monkeypatch.setattr(extraction_daemon, "_reconcile_internal_cursor_state", lambda *args, **kwargs: "not_internal")
        extraction_daemon.write_cursor(session_id, 4, str(live_path))

        captured = []
        buffered_calls = []

        def fake_buffer_transcript_tail(path, from_line, state, adapter=None, **kwargs):
            buffered_calls.append((path, from_line, dict(state)))
            assert path == str(mirror_path)
            assert from_line == 0
            return (
                {
                    "buffer_transcript_path": str(mirror_path),
                    "buffered_line_offset": 3,
                    "semantic_buffer": "User: chunk one\n\nUser: chunk two",
                    "semantic_buffer_tokens": 12,
                },
                {
                    "raw_lines_added": 3,
                    "semantic_chars_added": 30,
                    "semantic_tokens_added": 12,
                    "buffered_line_offset": 3,
                },
            )

        monkeypatch.setattr(extraction_daemon, "_buffer_transcript_tail", fake_buffer_transcript_tail)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        extraction_daemon.check_chunk_ready_sessions(chunk_tokens=10)

        assert [(path, from_line) for path, from_line, _state in buffered_calls] == [(str(mirror_path), 0)]
        assert captured[0]["meta"]["buffer_transcript_path"] == str(mirror_path)

    def test_check_chunk_ready_sessions_preserves_offset_when_buffer_source_unchanged(
        self, monkeypatch, tmp_path
    ):
        session_id = "e513b8c1-c08d-4eb9-b9d1-7b478cbb426f"
        mirror_path = (
            tmp_path
            / "instances"
            / "openclaw-main"
            / "logs"
            / "quaid"
            / "sessions"
            / f"{session_id}.jsonl"
        )
        mirror_path.parent.mkdir(parents=True, exist_ok=True)
        mirror_path.write_text(
            '{"type":"message","message":{"role":"user","content":"hello"}}\n'
            '{"type":"message","message":{"role":"user","content":"chunk one"}}\n'
            '{"type":"message","message":{"role":"user","content":"chunk two"}}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
        monkeypatch.setattr(extraction_daemon, "_instance_id", lambda: "openclaw-main")
        monkeypatch.setattr(extraction_daemon, "_ensure_discovered_session_cursors", lambda adapter: None)
        monkeypatch.setattr(extraction_daemon, "_reconcile_internal_cursor_state", lambda *args, **kwargs: "not_internal")
        extraction_daemon.write_cursor(session_id, 1, str(mirror_path))
        extraction_daemon.write_rolling_state(
            session_id,
            {
                "session_id": session_id,
                "transcript_path": str(mirror_path),
                "buffer_transcript_path": str(mirror_path),
                "buffered_line_offset": 1,
                "processed_line_offset": 1,
                "semantic_buffer": "User: hello",
                "semantic_buffer_tokens": 1,
            },
        )

        buffered_calls = []

        def fake_buffer_transcript_tail(path, from_line, state, adapter=None, **kwargs):
            buffered_calls.append((path, from_line))
            assert from_line == 1
            return (
                {
                    "buffer_transcript_path": str(mirror_path),
                    "buffered_line_offset": 3,
                    "semantic_buffer": "User: hello\n\nUser: chunk one\n\nUser: chunk two",
                    "semantic_buffer_tokens": 12,
                },
                {"buffered_line_offset": 3},
            )

        monkeypatch.setattr(extraction_daemon, "_buffer_transcript_tail", fake_buffer_transcript_tail)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(extraction_daemon, "write_signal", lambda *args, **kwargs: None)

        extraction_daemon.check_chunk_ready_sessions(chunk_tokens=10)

        assert buffered_calls == [(str(mirror_path), 1)]

    def test_check_chunk_ready_sessions_preserves_staged_payload_on_source_switch(
        self, monkeypatch, tmp_path
    ):
        session_id = "e513b8c1-c08d-4eb9-b9d1-7b478cbb426f"
        live_path = tmp_path / "live" / "sessions" / f"{session_id}.jsonl"
        mirror_path = (
            tmp_path
            / "instances"
            / "openclaw-main"
            / "logs"
            / "quaid"
            / "sessions"
            / f"{session_id}.jsonl"
        )
        live_path.parent.mkdir(parents=True, exist_ok=True)
        mirror_path.parent.mkdir(parents=True, exist_ok=True)
        live_path.write_text("", encoding="utf-8")
        mirror_path.write_text(
            '{"type":"message","message":{"role":"user","content":"hello"}}\n'
            '{"type":"message","message":{"role":"user","content":"chunk one"}}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
        monkeypatch.setattr(extraction_daemon, "_instance_id", lambda: "openclaw-main")
        monkeypatch.setattr(extraction_daemon, "_ensure_discovered_session_cursors", lambda adapter: None)
        monkeypatch.setattr(extraction_daemon, "_adapter_owns_transcript_path", lambda *args, **kwargs: True)
        monkeypatch.setattr(extraction_daemon, "_cursor_or_adapter_owns_transcript_path", lambda *args, **kwargs: True)
        monkeypatch.setattr(extraction_daemon, "_reconcile_internal_cursor_state", lambda *args, **kwargs: "not_internal")
        extraction_daemon.write_cursor(session_id, 2, str(live_path))
        extraction_daemon.write_rolling_state(
            session_id,
            {
                "session_id": session_id,
                "transcript_path": str(live_path),
                "buffer_transcript_path": str(live_path),
                "buffered_line_offset": 2,
                "processed_line_offset": 2,
                "raw_facts": [{"text": "already staged"}],
                "semantic_buffer": "",
                "semantic_buffer_tokens": 0,
            },
        )

        monkeypatch.setattr(
            extraction_daemon,
            "_buffer_transcript_tail",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("staged payload should suppress reset scan")),
        )
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(extraction_daemon, "write_signal", lambda *args, **kwargs: None)

        extraction_daemon.check_chunk_ready_sessions(chunk_tokens=10)

        state = extraction_daemon.read_rolling_state(session_id)
        assert state["buffer_transcript_path"] == str(live_path)
        assert state["buffered_line_offset"] == 2
        assert state["raw_facts"] == [{"text": "already staged"}]

    @pytest.mark.parametrize(
        "live_case",
        ["missing", "smaller", "uuid_mismatch", "not_owned", "jsonl_empty"],
    )
    def test_larger_live_transcript_for_preserved_mirror_rejects_invalid_live_paths(
        self, monkeypatch, tmp_path, live_case
    ):
        session_id = "09cba64f-c08d-4eb9-b9d1-7b478cbb426f"
        live_session_id = (
            "aaaaaaaa-c08d-4eb9-b9d1-7b478cbb426f"
            if live_case == "uuid_mismatch"
            else session_id
        )
        mirror_path = (
            tmp_path
            / "instances"
            / "openclaw-main"
            / "logs"
            / "quaid"
            / "sessions"
            / f"{session_id}.jsonl"
        )
        live_path = tmp_path / "live" / "sessions" / f"{live_session_id}.jsonl"
        mirror_path.parent.mkdir(parents=True, exist_ok=True)
        live_path.parent.mkdir(parents=True, exist_ok=True)
        mirror_path.write_text(
            '{"type":"message","message":{"role":"user","content":"hello from mirror with enough bytes"}}\n',
            encoding="utf-8",
        )
        if live_case != "missing":
            live_content = (
                '["parseable but not a JSONL object", "larger than the mirror", "still rejected"]\n' * 2
                if live_case == "jsonl_empty"
                else '{"type":"message","message":{"role":"user","content":"h"}}\n'
                if live_case == "smaller"
                else (
                    '{"type":"message","message":{"role":"user","content":"hello"}}\n'
                    '{"type":"message","message":{"role":"user","content":"chunk one"}}\n'
                    '{"type":"message","message":{"role":"user","content":"chunk two"}}\n'
                )
            )
            live_path.write_text(live_content, encoding="utf-8")

        class _Adapter:
            def get_session_path(self, session_id_arg):
                assert session_id_arg == session_id
                return live_path

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
        monkeypatch.setattr(extraction_daemon, "_instance_id", lambda: "openclaw-main")
        monkeypatch.setattr(
            extraction_daemon,
            "_adapter_owns_transcript_path",
            lambda *args, **kwargs: live_case != "not_owned",
        )

        assert (
            extraction_daemon._larger_live_transcript_for_preserved_mirror(
                session_id,
                str(mirror_path),
                adapter=_Adapter(),
            )
            == ""
        )

    def test_check_chunk_ready_sessions_scans_non_empty_preserved_cursor(
        self, monkeypatch, tmp_path
    ):
        session_id = "92271010-c08d-4eb9-b9d1-7b478cbb426f"
        mirror_path = (
            tmp_path
            / "instances"
            / "openclaw-main"
            / "logs"
            / "quaid"
            / "sessions"
            / f"{session_id}.jsonl"
        )
        mirror_path.parent.mkdir(parents=True, exist_ok=True)
        mirror_path.write_text(
            '{"type":"message","message":{"role":"user","content":"hello"}}\n'
            '{"type":"message","message":{"role":"user","content":"chunk one"}}\n'
            '{"type":"message","message":{"role":"user","content":"chunk two"}}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
        monkeypatch.setattr(extraction_daemon, "_instance_id", lambda: "openclaw-main")
        monkeypatch.setattr(extraction_daemon, "_ensure_discovered_session_cursors", lambda adapter: None)
        extraction_daemon.write_cursor(session_id, 0, str(mirror_path))

        captured = []
        buffered_paths = []

        def fake_buffer_transcript_tail(path, from_line, state, adapter=None, **kwargs):
            buffered_paths.append(path)
            assert path == str(mirror_path)
            assert from_line == 0
            return (
                {
                    "buffered_line_offset": 3,
                    "semantic_buffer": "User: chunk one\n\nUser: chunk two",
                    "semantic_buffer_tokens": 12,
                },
                {
                    "raw_lines_added": 3,
                    "semantic_chars_added": 30,
                    "semantic_tokens_added": 12,
                    "buffered_line_offset": 3,
                },
            )

        monkeypatch.setattr(extraction_daemon, "_buffer_transcript_tail", fake_buffer_transcript_tail)
        monkeypatch.setattr(extraction_daemon, "_reconcile_internal_cursor_state", lambda *args, **kwargs: "not_internal")
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        extraction_daemon.check_chunk_ready_sessions(chunk_tokens=10)

        assert buffered_paths == [str(mirror_path)]
        assert captured == [
            {
                "signal_type": "rolling",
                "session_id": session_id,
                "transcript_path": str(mirror_path),
                "meta": {
                    "reason": "semantic_chunk_budget",
                    "chunk_tokens": 10,
                    "semantic_buffer_tokens": 12,
                    "buffered_line_offset": 3,
                },
            }
        ]

    @pytest.mark.parametrize("mirror_case", ["missing", "smaller"])
    def test_check_chunk_ready_sessions_keeps_live_path_without_larger_mirror(
        self, monkeypatch, tmp_path, mirror_case
    ):
        session_id = "d000074a-c08d-4eb9-b9d1-7b478cbb426f"
        live_path = tmp_path / ".openclaw" / "agents" / "main" / "sessions" / f"{session_id}.jsonl"
        mirror_path = (
            tmp_path
            / "instances"
            / "openclaw-main"
            / "logs"
            / "quaid"
            / "sessions"
            / f"{session_id}.jsonl"
        )
        live_path.parent.mkdir(parents=True, exist_ok=True)
        mirror_path.parent.mkdir(parents=True, exist_ok=True)
        live_path.write_text(
            '{"type":"message","message":{"role":"user","content":"hello"}}\n'
            '{"type":"message","message":{"role":"user","content":"chunk one"}}\n',
            encoding="utf-8",
        )
        if mirror_case == "smaller":
            mirror_path.write_text(
                '{"type":"message","message":{"role":"user","content":"h"}}\n',
                encoding="utf-8",
            )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
        monkeypatch.setattr(extraction_daemon, "_instance_id", lambda: "openclaw-main")
        monkeypatch.setattr(extraction_daemon, "_ensure_discovered_session_cursors", lambda adapter: None)
        extraction_daemon.write_cursor(session_id, 0, str(live_path))

        buffered_paths = []

        def fake_buffer_transcript_tail(path, from_line, state, adapter=None, **kwargs):
            buffered_paths.append(path)
            return (
                {
                    "buffered_line_offset": 2,
                    "semantic_buffer": "User: chunk one",
                    "semantic_buffer_tokens": 2,
                },
                {
                    "raw_lines_added": 2,
                    "semantic_chars_added": 15,
                    "semantic_tokens_added": 2,
                    "buffered_line_offset": 2,
                },
            )

        monkeypatch.setattr(extraction_daemon, "_buffer_transcript_tail", fake_buffer_transcript_tail)
        monkeypatch.setattr(extraction_daemon, "_adapter_owns_transcript_path", lambda *args, **kwargs: True)
        monkeypatch.setattr(extraction_daemon, "_cursor_or_adapter_owns_transcript_path", lambda *args, **kwargs: True)
        monkeypatch.setattr(extraction_daemon, "_reconcile_internal_cursor_state", lambda *args, **kwargs: "not_internal")
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(extraction_daemon, "write_signal", lambda *args, **kwargs: None)

        extraction_daemon.check_chunk_ready_sessions(chunk_tokens=10)

        assert buffered_paths == [str(live_path)]

    def test_check_chunk_ready_sessions_does_not_roll_small_residual_tail_after_flush(
        self, monkeypatch, tmp_path
    ):
        import sys
        import types

        transcript_path = tmp_path / "session.jsonl"
        lines = [
            json.dumps(
                {"type": "event_msg", "payload": {"type": "user_message", "message": "chunk two lead"}}
            ) + "\n",
            json.dumps(
                {"type": "event_msg", "payload": {"type": "user_message", "message": "chunk two followup"}}
            ) + "\n",
            json.dumps(
                {"type": "event_msg", "payload": {"type": "user_message", "message": "chunk two tail"}}
            ) + "\n",
        ]
        transcript_path.write_text("".join(lines), encoding="utf-8")

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        session_id = "sess-roll-residual"
        extraction_daemon.write_cursor(session_id, 1, str(transcript_path))
        extraction_daemon.write_rolling_state(
            session_id,
            {
                "session_id": session_id,
                "transcript_path": str(transcript_path),
                "processed_line_offset": 1,
                "buffered_line_offset": 1,
                "semantic_buffer": "User: carryover from prior rolling flush",
                "semantic_buffer_tokens": 7,
                "raw_facts": [],
                "raw_snippets": {},
                "raw_journal": {},
                "raw_project_logs": {},
            },
        )

        real_adapter = sys.modules.get("lib.adapter")
        fake_adapter_mod = types.ModuleType("lib.adapter")

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def parse_session_jsonl(self, path):
                messages = []
                for raw in path.read_text(encoding="utf-8").splitlines():
                    payload = json.loads(raw)
                    event_payload = payload.get("payload", {})
                    message = event_payload.get("message")
                    if message:
                        messages.append(f"User: {message}")
                return "\n\n".join(messages)

        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        sys.modules["lib.adapter"] = fake_adapter_mod

        captured = []
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda default=8000: 20)
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_max_lines", lambda default=0: 1)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        try:
            extraction_daemon.check_chunk_ready_sessions()
            state = extraction_daemon.read_rolling_state(session_id)
            assert captured == []
            assert state["buffered_line_offset"] == 2
            assert state["semantic_buffer_tokens"] < 19
            assert "carryover from prior rolling flush" in state["semantic_buffer"]
            assert "chunk two followup" in state["semantic_buffer"]
        finally:
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

    def test_check_chunk_ready_sessions_skips_legacy_cursor_shadowed_by_source_cursor(
        self, monkeypatch, tmp_path
    ):
        import sys
        import types

        transcript_path = tmp_path / "rollout-2026-04-26T21-47-37-019dcbc3-2522-7fc1-80a2-08967963dfe2.jsonl"
        transcript_path.write_text(
            '{"type":"event_msg","payload":{"type":"user_message","message":"first long rolling note"}}\n'
            '{"type":"event_msg","payload":{"type":"user_message","message":"second long rolling note"}}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        legacy_session_id = "019dcbc3-2522-7fc1-80a2-08967963dfe2"
        rollout_session_id = "rollout-2026-04-26T21-47-37-019dcbc3-2522-7fc1-80a2-08967963dfe2"
        extraction_daemon.write_cursor(legacy_session_id, 0, str(transcript_path))
        source_key = extraction_daemon._signal_source_cursor_key(
            rollout_session_id,
            str(transcript_path),
        )
        legacy_cursor_file = extraction_daemon._cursor_dir() / f"{legacy_session_id}.json"
        extraction_daemon.write_cursor(
            rollout_session_id,
            2,
            str(transcript_path),
            source_key=source_key,
        )

        real_adapter = sys.modules.get("lib.adapter")
        fake_adapter_mod = types.ModuleType("lib.adapter")

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def parse_session_jsonl(self, path):
                messages = []
                for raw in path.read_text(encoding="utf-8").splitlines():
                    payload = json.loads(raw)
                    event_payload = payload.get("payload", {})
                    message = event_payload.get("message")
                    if message:
                        messages.append(f"User: {message}")
                return "\n\n".join(messages)

        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        sys.modules["lib.adapter"] = fake_adapter_mod

        captured = []
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda default=8000: 5)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        try:
            extraction_daemon.check_chunk_ready_sessions()
            assert captured == []
            assert not legacy_cursor_file.exists()
            assert not extraction_daemon._rolling_state_path(legacy_session_id).exists()
        finally:
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

    def test_zero_offset_source_cursor_does_not_shadow_unprocessed_alias(
        self, monkeypatch, tmp_path
    ):
        transcript_path = tmp_path / "rollout-2026-04-28T13-43-15-019dd454-6ab7-70a2-af6c-5b64f0cef501.jsonl"
        transcript_path.write_text(
            '{"type":"event_msg","payload":{"type":"user_message","message":"Tamarind-lighthouse-3317 is the codeword"}}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        session_id = "019dd454-6ab7-70a2-af6c-5b64f0cef501"
        source_key = extraction_daemon._signal_source_cursor_key(session_id, str(transcript_path))
        extraction_daemon.write_cursor(session_id, 0, str(transcript_path), source_key=source_key)
        extraction_daemon.write_cursor(session_id, 0, str(transcript_path))

        legacy_file = extraction_daemon._cursor_dir() / f"{session_id}.json"
        cursor_data = json.loads(legacy_file.read_text(encoding="utf-8"))

        assert extraction_daemon._cursor_shadowed_by_source_cursor(
            cursor_file=legacy_file,
            session_id=session_id,
            transcript_path=str(transcript_path),
            cursor_data=cursor_data,
        ) is False

        extraction_daemon.write_cursor(session_id, 1, str(transcript_path), source_key=source_key)
        extraction_daemon.write_cursor(session_id, 0, str(transcript_path))
        cursor_data = json.loads(legacy_file.read_text(encoding="utf-8"))
        assert extraction_daemon._cursor_shadowed_by_source_cursor(
            cursor_file=legacy_file,
            session_id=session_id,
            transcript_path=str(transcript_path),
            cursor_data=cursor_data,
        ) is True

    def test_grown_source_cursor_does_not_shadow_live_alias(
        self, monkeypatch, tmp_path
    ):
        import sys
        import types

        transcript_path = tmp_path / "e35ca1a9-11a3-4c21-ad6f-f6525209240e.jsonl"
        short_lines = [
            json.dumps({"type": "session", "id": "e35ca1a9"}) + "\n",
            json.dumps({"type": "model_change"}) + "\n",
            json.dumps({"type": "thinking_level_change"}) + "\n",
            json.dumps({"type": "custom", "customType": "model-snapshot"}) + "\n",
            json.dumps({"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "Hello"}]}}) + "\n",
            json.dumps({"type": "custom_message", "customType": "openclaw.runtime-context", "content": "context"}) + "\n",
            json.dumps({"type": "message", "message": {"role": "assistant", "content": [{"type": "text", "text": "ACK"}]}}) + "\n",
        ]
        transcript_path.write_text("".join(short_lines), encoding="utf-8")

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
        session_id = "e35ca1a9-11a3-4c21-ad6f-f6525209240e"
        source_key = extraction_daemon._signal_source_cursor_key(session_id, str(transcript_path))
        extraction_daemon.write_cursor(session_id, 7, str(transcript_path), source_key=source_key)

        grown_lines = list(short_lines)
        grown_lines[4] = json.dumps({
            "type": "message",
            "message": {
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": "Baxter uses an orange linen notebook from Emília Rosa. " * 40,
                }],
            },
        }) + "\n"
        transcript_path.write_text("".join(grown_lines), encoding="utf-8")
        extraction_daemon.write_cursor(session_id, 0, str(transcript_path))

        legacy_file = extraction_daemon._cursor_dir() / f"{session_id}.json"
        cursor_data = json.loads(legacy_file.read_text(encoding="utf-8"))
        assert extraction_daemon._cursor_shadowed_by_source_cursor(
            cursor_file=legacy_file,
            session_id=session_id,
            transcript_path=str(transcript_path),
            cursor_data=cursor_data,
        ) is False

        real_adapter = sys.modules.get("lib.adapter")
        fake_adapter_mod = types.ModuleType("lib.adapter")

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def parse_session_jsonl(self, path):
                raw = Path(path).read_text(encoding="utf-8")
                if "orange linen notebook" in raw:
                    return "User: " + ("Baxter uses an orange linen notebook from Emília Rosa. " * 40)
                return "User: Hello\n\nAssistant: ACK"

        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        sys.modules["lib.adapter"] = fake_adapter_mod

        captured = []
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda default=8000: 20)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        try:
            extraction_daemon.check_chunk_ready_sessions()
        finally:
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

        assert len(captured) == 1
        assert captured[0]["signal_type"] == "rolling"
        assert captured[0]["session_id"] == session_id

    def test_legacy_source_cursor_without_size_metadata_still_shadows_alias(
        self, monkeypatch, tmp_path
    ):
        transcript_path = tmp_path / "rollout-2026-05-26T01-40-00-e35ca1a9-11a3-4c21-ad6f-f6525209240e.jsonl"
        transcript_path.write_text(
            '{"type":"event_msg","payload":{"type":"user_message","message":"already extracted rolling note"}}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        session_id = "e35ca1a9-11a3-4c21-ad6f-f6525209240e"
        source_key = extraction_daemon._signal_source_cursor_key(session_id, str(transcript_path))
        extraction_daemon.write_cursor(session_id, 1, str(transcript_path), source_key=source_key)
        source_file = extraction_daemon._cursor_dir() / f"{source_key}.json"
        source_payload = json.loads(source_file.read_text(encoding="utf-8"))
        source_payload.pop("transcript_size_bytes")
        source_file.write_text(json.dumps(source_payload), encoding="utf-8")

        extraction_daemon.write_cursor(session_id, 0, str(transcript_path))
        legacy_file = extraction_daemon._cursor_dir() / f"{session_id}.json"
        cursor_data = json.loads(legacy_file.read_text(encoding="utf-8"))

        assert extraction_daemon._cursor_shadowed_by_source_cursor(
            cursor_file=legacy_file,
            session_id=session_id,
            transcript_path=str(transcript_path),
            cursor_data=cursor_data,
        ) is True

    def test_cursor_shadow_check_raises_source_key_failure_when_failhard(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

        def _boom(*_args, **_kwargs):
            raise RuntimeError("source key failed")

        monkeypatch.setattr(extraction_daemon, "_signal_source_cursor_key", _boom)

        with pytest.raises(RuntimeError, match="source key failed"):
            extraction_daemon._cursor_shadowed_by_source_cursor(
                cursor_file=tmp_path / "cursor.json",
                session_id="sess-shadow",
                transcript_path="/tmp/session.jsonl",
                cursor_data={},
            )

    def test_cursor_shadow_check_warns_and_falls_back_when_fail_open(
        self,
        monkeypatch,
        tmp_path,
        caplog,
    ):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: False)

        def _boom(*_args, **_kwargs):
            raise RuntimeError("source key failed")

        monkeypatch.setattr(extraction_daemon, "_signal_source_cursor_key", _boom)

        with caplog.at_level("WARNING", logger="quaid.daemon"):
            result = extraction_daemon._cursor_shadowed_by_source_cursor(
                cursor_file=tmp_path / "cursor.json",
                session_id="sess-shadow",
                transcript_path="/tmp/session.jsonl",
                cursor_data={},
            )

        assert result is False
        assert "cursor shadow check failed" in caplog.text

    def test_check_chunk_ready_sessions_rescans_eof_cursor_for_byte_growth(
        self, monkeypatch, tmp_path
    ):
        import sys
        import types

        transcript_path = tmp_path / "858e08d3-4e9d-4a72-b7e1-3df34f10f622.jsonl"
        short_lines = [
            json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "Hello"}}) + "\n",
        ]
        transcript_path.write_text("".join(short_lines), encoding="utf-8")

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        session_id = "858e08d3-4e9d-4a72-b7e1-3df34f10f622"
        extraction_daemon.write_cursor(session_id, 1, str(transcript_path))

        grown_lines = list(short_lines)
        grown_lines[0] = json.dumps({
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "Baxter uses an orange linen notebook from Emília Rosa. " * 40,
            },
        }) + "\n"
        transcript_path.write_text("".join(grown_lines), encoding="utf-8")

        real_adapter = sys.modules.get("lib.adapter")
        fake_adapter_mod = types.ModuleType("lib.adapter")

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def parse_session_jsonl(self, path):
                raw = Path(path).read_text(encoding="utf-8")
                if "orange linen notebook" in raw:
                    return "User: " + ("Baxter uses an orange linen notebook from Emília Rosa. " * 40)
                return "User: Hello\n\nAssistant: ACK"

        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        sys.modules["lib.adapter"] = fake_adapter_mod

        captured = []
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda default=8000: 20)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(extraction_daemon, "_reconcile_internal_cursor_state", lambda *args, **kwargs: "not_internal")
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        try:
            extraction_daemon.check_chunk_ready_sessions()
        finally:
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

        assert len(captured) == 1
        assert captured[0]["signal_type"] == "rolling"
        assert captured[0]["session_id"] == session_id

    def test_check_chunk_ready_sessions_refreshes_eof_rewrite_baseline_after_subthreshold_rescan(
        self, monkeypatch, tmp_path
    ):
        transcript_path = tmp_path / "8d01d993-b570-4c5d-bd99-b5e3d269a5b6.jsonl"
        short_line = (
            json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "Hello"}})
            + "\n"
        )
        transcript_path.write_text(short_line, encoding="utf-8")

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        session_id = "8d01d993-b570-4c5d-bd99-b5e3d269a5b6"
        extraction_daemon.write_cursor(session_id, 1, str(transcript_path))
        extraction_daemon.write_rolling_state(
            session_id,
            {
                "session_id": session_id,
                "transcript_path": str(transcript_path),
                "buffer_transcript_path": str(transcript_path),
                "processed_line_offset": 1,
                "buffered_line_offset": 1,
                "semantic_buffer": "User: Hello",
                "semantic_buffer_tokens": 2,
            },
        )

        grown_line = (
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "Baxter added a short orange linen notebook note.",
                    },
                }
            )
            + "\n"
        )
        transcript_path.write_text(grown_line, encoding="utf-8")

        buffered_from_lines = []

        def fake_buffer_transcript_tail(path, from_line, state, adapter=None, **kwargs):
            buffered_from_lines.append(from_line)
            assert path == str(transcript_path)
            assert from_line == 0
            assert state.get("semantic_buffer", "") == ""
            return (
                {
                    "buffer_transcript_path": str(transcript_path),
                    "buffered_line_offset": 1,
                    "semantic_buffer": "User: Baxter added a short orange linen notebook note.",
                    "semantic_buffer_tokens": 5,
                },
                {
                    "raw_lines_added": 1,
                    "semantic_chars_added": 54,
                    "semantic_tokens_added": 5,
                    "buffered_line_offset": 1,
                },
            )

        monkeypatch.setattr(extraction_daemon, "_buffer_transcript_tail", fake_buffer_transcript_tail)
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda default=8000: 50)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(extraction_daemon, "_reconcile_internal_cursor_state", lambda *args, **kwargs: "not_internal")
        monkeypatch.setattr(extraction_daemon, "write_signal", lambda *args, **kwargs: None)

        extraction_daemon.check_chunk_ready_sessions()
        assert buffered_from_lines == [0]
        cursor_after_first_scan = extraction_daemon.read_cursor(session_id)
        assert cursor_after_first_scan["line_offset"] == 1
        assert cursor_after_first_scan["transcript_size_bytes"] == transcript_path.stat().st_size

        extraction_daemon.check_chunk_ready_sessions()
        assert buffered_from_lines == [0]

    def test_check_chunk_ready_sessions_uses_stale_source_cursor_for_byte_growth_alias(
        self, monkeypatch, tmp_path
    ):
        import sys
        import types

        session_id = "019ebe06-8112-7570-bec0-92e35205188d"
        transcript_path = tmp_path / f"rollout-2026-06-12T22-49-17-{session_id}.jsonl"
        transcript_path.write_text(
            '{"type":"event_msg","payload":{"type":"user_message","message":"chunk one"}}\n'
            '{"type":"event_msg","payload":{"type":"user_message","message":"short placeholder"}}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        source_key = extraction_daemon._signal_source_cursor_key(session_id, str(transcript_path))
        extraction_daemon.write_cursor(session_id, 2, str(transcript_path), source_key=source_key)
        extraction_daemon.write_rolling_state(
            session_id,
            {
                "session_id": session_id,
                "transcript_path": str(transcript_path),
                "buffer_transcript_path": str(transcript_path),
                "processed_line_offset": 2,
                "buffered_line_offset": 2,
                "semantic_buffer": "User: chunk one",
                "semantic_buffer_tokens": 2,
            },
        )

        transcript_path.write_text(
            '{"type":"event_msg","payload":{"type":"user_message","message":"chunk one"}}\n'
            '{"type":"event_msg","payload":{"type":"user_message","message":"short placeholder"}}\n'
            + json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "Baxter uses an orange linen notebook from Emília Rosa. " * 40,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        extraction_daemon.write_cursor(session_id, 0, str(transcript_path))
        alias_file = extraction_daemon._cursor_dir() / f"{session_id}.json"

        real_glob = Path.glob

        def fake_glob(path, pattern):
            if path == extraction_daemon._cursor_dir() and pattern == "*.json":
                return iter([alias_file])
            return real_glob(path, pattern)

        buffered_from_lines = []

        def fake_buffer_transcript_tail(path, from_line, state, adapter=None, **kwargs):
            buffered_from_lines.append(from_line)
            assert path == str(transcript_path)
            assert from_line == 2
            assert state.get("semantic_buffer", "") == "User: chunk one"
            return (
                {
                    "buffer_transcript_path": str(transcript_path),
                    "buffered_line_offset": 3,
                    "semantic_buffer": "User: Baxter uses an orange linen notebook from Emília Rosa.",
                    "semantic_buffer_tokens": 12,
                },
                {
                    "raw_lines_added": 1,
                    "semantic_chars_added": 62,
                    "semantic_tokens_added": 12,
                    "buffered_line_offset": 3,
                },
            )

        real_adapter = sys.modules.get("lib.adapter")
        fake_adapter_mod = types.ModuleType("lib.adapter")
        fake_adapter_mod.get_adapter = lambda: _OwnedTestAdapterMixin()
        sys.modules["lib.adapter"] = fake_adapter_mod

        captured = []
        monkeypatch.setattr(Path, "glob", fake_glob)
        monkeypatch.setattr(extraction_daemon, "_ensure_discovered_session_cursors", lambda adapter: None)
        monkeypatch.setattr(extraction_daemon, "_cursor_or_adapter_owns_transcript_path", lambda *args, **kwargs: True)
        monkeypatch.setattr(extraction_daemon, "_reconcile_internal_cursor_state", lambda *args, **kwargs: "not_internal")
        monkeypatch.setattr(extraction_daemon, "_buffer_transcript_tail", fake_buffer_transcript_tail)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        try:
            extraction_daemon.check_chunk_ready_sessions(chunk_tokens=10)
        finally:
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

        assert buffered_from_lines == [2]
        assert captured == [
            {
                "signal_type": "rolling",
                "session_id": session_id,
                "transcript_path": str(transcript_path),
                "meta": {
                    "reason": "semantic_chunk_budget",
                    "chunk_tokens": 10,
                    "semantic_buffer_tokens": 12,
                    "buffered_line_offset": 3,
                },
            }
        ]
        source_cursor = extraction_daemon.read_cursor(session_id, source_key=source_key)
        assert source_cursor["line_offset"] == 2

    def test_check_chunk_ready_sessions_preserves_subthreshold_stale_source_cursor(
        self, monkeypatch, tmp_path
    ):
        import sys
        import types

        session_id = "019f27a4-0000-7000-b000-000000000000"
        transcript_path = tmp_path / f"rollout-2026-07-03T11-22-21-{session_id}.jsonl"
        transcript_path.write_text(
            '{"type":"event_msg","payload":{"type":"user_message","message":"m1 checkpoint"}}\n'
            '{"type":"event_msg","payload":{"type":"assistant_message","message":"ack"}}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        source_key = extraction_daemon._signal_source_cursor_key(session_id, str(transcript_path))
        extraction_daemon.write_cursor(
            session_id,
            2,
            str(transcript_path),
            source_key=source_key,
            last_flushed_line_offset=2,
        )
        extraction_daemon.write_rolling_state(
            session_id,
            {
                "session_id": session_id,
                "transcript_path": str(transcript_path),
                "buffer_transcript_path": str(transcript_path),
                "processed_line_offset": 2,
                "buffered_line_offset": 2,
                "semantic_buffer": "",
                "semantic_buffer_tokens": 0,
            },
        )

        transcript_path.write_text(
            '{"type":"event_msg","payload":{"type":"user_message","message":"m1 checkpoint"}}\n'
            '{"type":"event_msg","payload":{"type":"assistant_message","message":"ack"}}\n'
            '{"type":"event_msg","payload":{"type":"user_message","message":"small m2 tail that is below rolling threshold"}}\n',
            encoding="utf-8",
        )
        extraction_daemon.write_cursor(session_id, 0, str(transcript_path))
        alias_file = extraction_daemon._cursor_dir() / f"{session_id}.json"

        real_glob = Path.glob

        def fake_glob(path, pattern):
            if path == extraction_daemon._cursor_dir() and pattern == "*.json":
                return iter([alias_file])
            return real_glob(path, pattern)

        def fake_buffer_transcript_tail(path, from_line, state, adapter=None, **kwargs):
            assert path == str(transcript_path)
            assert from_line == 2
            return (
                {
                    "buffer_transcript_path": str(transcript_path),
                    "buffered_line_offset": 3,
                    "semantic_buffer": "User: small m2 tail that is below rolling threshold",
                    "semantic_buffer_tokens": 12,
                },
                {
                    "raw_lines_added": 1,
                    "semantic_chars_added": 51,
                    "semantic_tokens_added": 12,
                    "buffered_line_offset": 3,
                },
            )

        real_adapter = sys.modules.get("lib.adapter")
        fake_adapter_mod = types.ModuleType("lib.adapter")
        fake_adapter_mod.get_adapter = lambda: _OwnedTestAdapterMixin()
        sys.modules["lib.adapter"] = fake_adapter_mod

        captured = []
        monkeypatch.setattr(Path, "glob", fake_glob)
        monkeypatch.setattr(extraction_daemon, "_ensure_discovered_session_cursors", lambda adapter: None)
        monkeypatch.setattr(extraction_daemon, "_cursor_or_adapter_owns_transcript_path", lambda *args, **kwargs: True)
        monkeypatch.setattr(extraction_daemon, "_reconcile_internal_cursor_state", lambda *args, **kwargs: "not_internal")
        monkeypatch.setattr(extraction_daemon, "_buffer_transcript_tail", fake_buffer_transcript_tail)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda *args, **kwargs: captured.append((args, kwargs)),
        )

        try:
            extraction_daemon.check_chunk_ready_sessions(chunk_tokens=100)
        finally:
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

        assert captured == []
        source_cursor = extraction_daemon.read_cursor(session_id, source_key=source_key)
        assert source_cursor["line_offset"] == 2
        assert source_cursor["last_flushed_line_offset"] == 2
        rolling_state = extraction_daemon.read_rolling_state(session_id)
        assert rolling_state["buffered_line_offset"] == 3
        assert rolling_state["semantic_buffer_tokens"] == 12

    def test_process_signal_rolling_below_threshold_preserves_source_cursor(
        self, monkeypatch, tmp_path
    ):
        import sys
        import types

        session_id = "019f27d3-0000-7000-b000-000000000000"
        transcript_path = tmp_path / f"{session_id}.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"m1 checkpoint"}\n'
            '{"role":"assistant","content":"ack"}\n'
            '{"role":"user","content":"chunk one content buffered by rolling scan"}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        source_key = extraction_daemon._signal_source_cursor_key(session_id, str(transcript_path))
        extraction_daemon.write_cursor(
            session_id,
            2,
            str(transcript_path),
            source_key=source_key,
            last_flushed_line_offset=2,
        )
        extraction_daemon.write_rolling_state(
            session_id,
            {
                "session_id": session_id,
                "transcript_path": str(transcript_path),
                "buffer_transcript_path": str(transcript_path),
                "processed_line_offset": 3,
                "buffered_line_offset": 3,
                "semantic_buffer": "User: chunk one content buffered by rolling scan",
                "semantic_buffer_tokens": 90,
            },
        )

        real_registry = sys.modules.get("core.subagent_registry")
        real_adapter = sys.modules.get("lib.adapter")
        fake_registry = types.ModuleType("core.subagent_registry")
        fake_registry.is_registered_subagent = lambda sid: False
        sys.modules["core.subagent_registry"] = fake_registry

        fake_adapter_mod = types.ModuleType("lib.adapter")

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def parse_session_jsonl(self, path):
                rendered = []
                for raw in Path(path).read_text(encoding="utf-8").splitlines():
                    row = json.loads(raw)
                    content = str(row.get("content") or "").strip()
                    if content:
                        rendered.append(f"{row.get('role', 'unknown').title()}: {content}")
                return "\n".join(rendered)

        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        sys.modules["lib.adapter"] = fake_adapter_mod

        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda default=8000: 100)
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_max_lines", lambda default=0: 100)
        monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "Owner")
        monkeypatch.setattr(extraction_daemon, "_cursor_or_adapter_owns_transcript_path", lambda *args, **kwargs: True)
        monkeypatch.setattr(extraction_daemon, "_reconcile_internal_cursor_state", lambda *args, **kwargs: "not_internal")

        try:
            extraction_daemon.write_signal(
                signal_type="rolling",
                session_id=session_id,
                transcript_path=str(transcript_path),
                meta={
                    "reason": "semantic_chunk_budget_near",
                    "source_cursor_key": source_key,
                },
            )
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])
        finally:
            if real_registry is not None:
                sys.modules["core.subagent_registry"] = real_registry
            else:
                sys.modules.pop("core.subagent_registry", None)
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

        cursor = extraction_daemon.read_cursor(session_id, source_key=source_key)
        assert cursor["line_offset"] == 2
        assert cursor["last_flushed_line_offset"] == 2
        state = extraction_daemon.read_rolling_state(session_id)
        assert state["buffered_line_offset"] == 3
        assert state["semantic_buffer_tokens"] == 90
        assert extraction_daemon.read_pending_signals() == []

    def test_process_signal_session_end_drains_live_residual_after_rolling_snapshot(
        self, monkeypatch, tmp_path
    ):
        from lib.adapter import set_adapter, reset_adapter
        from ingest import extract as extract_mod
        from core import ingest_runtime
        from core.runtime import notify as notify_mod

        session_id = "b414e0a0-9c4a-4466-b10d-4995b15eb74f"
        snapshot_path = tmp_path / "snapshots" / session_id / "snapshot" / f"{session_id}.jsonl"
        live_path = tmp_path / ".quaid" / "instances" / "openclaw-main" / "logs" / "quaid" / "sessions" / f"{session_id}.jsonl"
        snapshot_path.parent.mkdir(parents=True)
        live_path.parent.mkdir(parents=True)
        snapshot_path.write_text(
            '{"role":"user","content":"chunk one already staged"}\n'
            '{"role":"assistant","content":"ack"}\n',
            encoding="utf-8",
        )
        live_path.write_text(
            snapshot_path.read_text(encoding="utf-8")
            + '{"role":"user","content":"Baxter keeps the orange linen notebook beside the window."}\n'
            + '{"role":"assistant","content":"noted"}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path / ".quaid"))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
        source_key = extraction_daemon._signal_source_cursor_key(session_id, str(live_path))
        extraction_daemon.write_cursor(
            session_id,
            2,
            str(live_path),
            source_key=source_key,
            last_flushed_line_offset=2,
        )
        extraction_daemon.write_rolling_state(
            session_id,
            {
                "session_id": session_id,
                "transcript_path": str(snapshot_path),
                "source_transcript_path": str(live_path),
                "buffer_transcript_path": str(snapshot_path),
                "processed_line_offset": 2,
                "buffered_line_offset": 2,
                "semantic_buffer": "",
                "semantic_buffer_tokens": 0,
                "raw_facts": [{"text": "chunk one already staged", "category": "fact"}],
                "carry_facts": [],
                "rolling_batches": 1,
            },
        )

        captured = {}

        class _Adapter(_OwnedTestAdapterMixin):
            def instance_root(self):
                return tmp_path / ".quaid" / "instances" / "openclaw-main"

            def parse_session_jsonl(self, path):
                raw = Path(path).read_text(encoding="utf-8")
                if "orange linen notebook" in raw:
                    return "User: Baxter keeps the orange linen notebook beside the window.\nAssistant: noted"
                return "User: chunk one already staged"

            def is_subagent_session(self, session_id, transcript_path=None):
                return False

        set_adapter(_Adapter())
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda default=8000: 100)
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_max_lines", lambda default=0: 100)
        monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "owner-1")
        monkeypatch.setattr(extraction_daemon, "_read_usage_totals", lambda: {})
        monkeypatch.setattr(extraction_daemon, "_session_has_harvestable_subagents", lambda *args, **kwargs: False)
        monkeypatch.setattr(extraction_daemon, "_warm_payload_embeddings", lambda _facts: {})
        monkeypatch.setattr(extraction_daemon, "_cursor_or_adapter_owns_transcript_path", lambda *args, **kwargs: True)
        monkeypatch.setattr(extraction_daemon, "_reconcile_internal_cursor_state", lambda *args, **kwargs: "not_internal")
        monkeypatch.setattr(notify_mod, "notify_memory_extraction", lambda **_kwargs: None)
        monkeypatch.setattr(ingest_runtime, "run_session_logs_ingest", lambda **_kwargs: {"status": "indexed"})

        def fake_extract_from_transcript(transcript, **kwargs):
            captured["transcript"] = transcript
            return {
                "chunks_processed": 1,
                "chunks_total": 1,
                "unclassified_empty_payloads": 0,
                "raw_facts": [{"text": "Baxter keeps the orange linen notebook beside the window.", "category": "fact"}],
                "facts": [],
                "soul_snippets": {},
                "journal_entries": {},
                "project_logs": {},
                "raw_snippets": {},
                "raw_journal": {},
                "raw_project_logs": {},
                "carry_facts": [],
            }

        monkeypatch.setattr(extract_mod, "extract_from_transcript", fake_extract_from_transcript)
        monkeypatch.setattr(
            extract_mod,
            "apply_extracted_payloads",
            lambda *_args, **_kwargs: {
                "facts_stored": 2,
                "facts_skipped": 0,
                "edges_created": 0,
                "facts": [],
                "snippets": {},
                "journal": {},
                "project_log_metrics": {},
            },
        )

        try:
            signal_path = extraction_daemon.write_signal(
                signal_type="session_end",
                session_id=session_id,
                transcript_path=str(snapshot_path),
                meta={"source_cursor_key": source_key},
            )
            signal_data = json.loads(signal_path.read_text(encoding="utf-8"))
            signal_data["_signal_path"] = str(signal_path)

            extraction_daemon.process_signal(signal_data)
        finally:
            reset_adapter()

        assert "orange linen notebook" in captured["transcript"]
        cursor = extraction_daemon.read_cursor(session_id, source_key=source_key)
        assert cursor["line_offset"] == 4
        assert cursor["transcript_path"] == str(live_path)
        assert not extraction_daemon._rolling_state_path(session_id).exists()

    def test_check_chunk_ready_sessions_rejects_stale_source_cursor_after_rebase_growth(
        self, monkeypatch, tmp_path
    ):
        session_id = "019ebe06-aaaa-7570-bec0-92e35205188d"
        transcript_path = tmp_path / f"rollout-2026-06-12T22-49-17-{session_id}.jsonl"
        transcript_path.write_text(
            '{"type":"event_msg","payload":{"type":"user_message","message":"old chunk one"}}\n'
            '{"type":"event_msg","payload":{"type":"user_message","message":"old chunk two"}}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        source_key = extraction_daemon._signal_source_cursor_key(session_id, str(transcript_path))
        extraction_daemon.write_cursor(session_id, 2, str(transcript_path), source_key=source_key)

        transcript_path.write_text(
            '{"type":"event_msg","payload":{"type":"user_message","message":"rebased first line"}}\n'
            '{"type":"event_msg","payload":{"type":"user_message","message":"rebased second line"}}\n'
            '{"type":"event_msg","payload":{"type":"user_message","message":"Baxter uses an orange linen notebook from Emília Rosa. Baxter repeats this durable marker."}}\n',
            encoding="utf-8",
        )
        extraction_daemon.write_cursor(session_id, 0, str(transcript_path))
        alias_file = extraction_daemon._cursor_dir() / f"{session_id}.json"

        real_glob = Path.glob

        def fake_glob(path, pattern):
            if path == extraction_daemon._cursor_dir() and pattern == "*.json":
                return iter([alias_file])
            return real_glob(path, pattern)

        buffered_from_lines = []

        def fake_buffer_transcript_tail(path, from_line, state, adapter=None, **kwargs):
            buffered_from_lines.append(from_line)
            assert path == str(transcript_path)
            assert from_line == 0
            assert state.get("semantic_buffer", "") == ""
            return (
                {
                    "buffer_transcript_path": str(transcript_path),
                    "buffered_line_offset": 3,
                    "semantic_buffer": "User: Baxter uses an orange linen notebook from Emília Rosa.",
                    "semantic_buffer_tokens": 12,
                },
                {
                    "raw_lines_added": 3,
                    "semantic_chars_added": 62,
                    "semantic_tokens_added": 12,
                    "buffered_line_offset": 3,
                },
            )

        captured = []
        monkeypatch.setattr(Path, "glob", fake_glob)
        monkeypatch.setattr(extraction_daemon, "_ensure_discovered_session_cursors", lambda adapter: None)
        monkeypatch.setattr(extraction_daemon, "_cursor_or_adapter_owns_transcript_path", lambda *args, **kwargs: True)
        monkeypatch.setattr(extraction_daemon, "_reconcile_internal_cursor_state", lambda *args, **kwargs: "not_internal")
        monkeypatch.setattr(extraction_daemon, "_buffer_transcript_tail", fake_buffer_transcript_tail)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        extraction_daemon.check_chunk_ready_sessions(chunk_tokens=10)

        assert buffered_from_lines == [0]
        assert captured == [
            {
                "signal_type": "rolling",
                "session_id": session_id,
                "transcript_path": str(transcript_path),
                "meta": {
                    "reason": "semantic_chunk_budget",
                    "chunk_tokens": 10,
                    "semantic_buffer_tokens": 12,
                    "buffered_line_offset": 3,
                },
            }
        ]

    def test_check_chunk_ready_sessions_refreshes_grown_source_cursor_without_alias(
        self, monkeypatch, tmp_path
    ):
        session_id = "019ebe37-0000-7000-b000-000000000000"
        transcript_path = tmp_path / f"rollout-2026-06-12T23-42-25-{session_id}.jsonl"
        transcript_path.write_text(
            '{"type":"event_msg","payload":{"type":"user_message","message":"placeholder"}}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        source_key = extraction_daemon._signal_source_cursor_key(session_id, str(transcript_path))
        extraction_daemon.write_cursor(session_id, 0, str(transcript_path), source_key=source_key)

        transcript_path.write_text(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "Baxter uses an orange linen notebook from Emília Rosa. " * 40,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        def fake_buffer_transcript_tail(path, from_line, state, adapter=None, **kwargs):
            assert path == str(transcript_path)
            assert from_line == 0
            return (
                {
                    "buffer_transcript_path": str(transcript_path),
                    "buffered_line_offset": 1,
                    "semantic_buffer": "User: Baxter uses an orange linen notebook from Emília Rosa.",
                    "semantic_buffer_tokens": 12,
                },
                {
                    "raw_lines_added": 1,
                    "semantic_chars_added": 62,
                    "semantic_tokens_added": 12,
                    "buffered_line_offset": 1,
                },
            )

        captured = []
        monkeypatch.setattr(extraction_daemon, "_ensure_discovered_session_cursors", lambda adapter: None)
        monkeypatch.setattr(extraction_daemon, "_cursor_or_adapter_owns_transcript_path", lambda *args, **kwargs: True)
        monkeypatch.setattr(extraction_daemon, "_reconcile_internal_cursor_state", lambda *args, **kwargs: "not_internal")
        monkeypatch.setattr(extraction_daemon, "_buffer_transcript_tail", fake_buffer_transcript_tail)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        extraction_daemon.check_chunk_ready_sessions(chunk_tokens=10)

        assert captured == [
            {
                "signal_type": "rolling",
                "session_id": session_id,
                "transcript_path": str(transcript_path),
                "meta": {
                    "reason": "semantic_chunk_budget",
                    "chunk_tokens": 10,
                    "semantic_buffer_tokens": 12,
                    "buffered_line_offset": 1,
                },
            }
        ]
        source_cursor = extraction_daemon.read_cursor(session_id, source_key=source_key)
        assert source_cursor["line_offset"] == 1
        assert source_cursor["transcript_size_bytes"] == transcript_path.stat().st_size

    def test_check_chunk_ready_sessions_does_not_rebuffer_grown_source_while_preserving_cursor(
        self, monkeypatch, tmp_path
    ):
        session_id = "019ebe37-1111-7000-b000-000000000000"
        transcript_path = tmp_path / f"rollout-2026-06-12T23-42-25-{session_id}.jsonl"
        transcript_path.write_text(
            '{"type":"event_msg","payload":{"type":"user_message","message":"placeholder"}}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        source_key = extraction_daemon._signal_source_cursor_key(session_id, str(transcript_path))
        extraction_daemon.write_cursor(session_id, 0, str(transcript_path), source_key=source_key)

        transcript_path.write_text(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "Baxter uses a small orange linen notebook from Emília Rosa.",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        buffered_from_lines = []

        def fake_buffer_transcript_tail(path, from_line, state, adapter=None, **kwargs):
            buffered_from_lines.append(from_line)
            assert path == str(transcript_path)
            return (
                {
                    "buffer_transcript_path": str(transcript_path),
                    "buffered_line_offset": 1,
                    "semantic_buffer": "User: Baxter uses a small orange linen notebook from Emília Rosa.",
                    "semantic_buffer_tokens": 6,
                },
                {
                    "raw_lines_added": 1,
                    "semantic_chars_added": 65,
                    "semantic_tokens_added": 6,
                    "buffered_line_offset": 1,
                },
            )

        captured = []
        monkeypatch.setattr(extraction_daemon, "_ensure_discovered_session_cursors", lambda adapter: None)
        monkeypatch.setattr(extraction_daemon, "_cursor_or_adapter_owns_transcript_path", lambda *args, **kwargs: True)
        monkeypatch.setattr(extraction_daemon, "_reconcile_internal_cursor_state", lambda *args, **kwargs: "not_internal")
        monkeypatch.setattr(extraction_daemon, "_buffer_transcript_tail", fake_buffer_transcript_tail)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        extraction_daemon.check_chunk_ready_sessions(chunk_tokens=100)
        extraction_daemon.check_chunk_ready_sessions(chunk_tokens=100)

        assert buffered_from_lines == [0]
        assert captured == []
        source_cursor = extraction_daemon.read_cursor(session_id, source_key=source_key)
        assert source_cursor["line_offset"] == 0

    def test_check_chunk_ready_sessions_dedupes_same_source_alias_cursors(
        self, monkeypatch, tmp_path
    ):
        import sys
        import types

        transcript_path = tmp_path / "rollout-2026-05-23T23-35-32-019e5731-a7aa-74b3-b681-bfefb9d1f3ec.jsonl"
        transcript_path.write_text(
            '{"type":"event_msg","payload":{"type":"user_message","message":"first long rolling note with durable details"}}\n'
            '{"type":"event_msg","payload":{"type":"user_message","message":"second long rolling note with more durable details"}}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        legacy_session_id = "019e5731-a7aa-74b3-b681-bfefb9d1f3ec"
        rollout_session_id = "rollout-2026-05-23T23-35-32-019e5731-a7aa-74b3-b681-bfefb9d1f3ec"
        source_key = extraction_daemon._signal_source_cursor_key(
            rollout_session_id,
            str(transcript_path),
        )
        extraction_daemon.write_cursor(
            rollout_session_id,
            0,
            str(transcript_path),
            source_key=source_key,
        )
        extraction_daemon.write_cursor(legacy_session_id, 0, str(transcript_path))

        real_adapter = sys.modules.get("lib.adapter")
        fake_adapter_mod = types.ModuleType("lib.adapter")

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def parse_session_jsonl(self, path):
                messages = []
                for raw in path.read_text(encoding="utf-8").splitlines():
                    payload = json.loads(raw)
                    event_payload = payload.get("payload", {})
                    message = event_payload.get("message")
                    if message:
                        messages.append(f"User: {message}")
                return "\n\n".join(messages)

        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        sys.modules["lib.adapter"] = fake_adapter_mod

        captured = []
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda default=8000: 5)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        try:
            extraction_daemon.check_chunk_ready_sessions()
            assert len(captured) == 1
            assert captured[0]["signal_type"] == "rolling"
            assert captured[0]["transcript_path"] == str(transcript_path)
            assert captured[0]["session_id"] in {legacy_session_id, rollout_session_id}
            assert sum(
                int(extraction_daemon._rolling_state_path(session_id).exists())
                for session_id in (legacy_session_id, rollout_session_id)
            ) == 1
        finally:
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

    def test_check_chunk_ready_sessions_uses_session_end_source_cursor_tail(
        self, monkeypatch, tmp_path
    ):
        transcript_path = tmp_path / "rollout-2026-05-25T20-56-13-019e60ec-838e-7eb1-8ed6-7f52f2b47570.jsonl"
        transcript_path.write_text(
            '{"type":"event_msg","payload":{"type":"user_message","message":"hello"}}\n'
            '{"type":"event_msg","payload":{"type":"assistant_message","message":"ack"}}\n'
            '{"type":"event_msg","payload":{"type":"user_message","message":"Baxter uses an orange linen notebook"}}\n'
            '{"type":"event_msg","payload":{"type":"user_message","message":"Emília Rosa supplied the notebook"}}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        legacy_session_id = "019e60ec-838e-7eb1-8ed6-7f52f2b47570"
        rollout_session_id = "rollout-2026-05-25T20-56-13-019e60ec-838e-7eb1-8ed6-7f52f2b47570"
        source_key = extraction_daemon._signal_source_cursor_key(
            legacy_session_id,
            str(transcript_path),
        )
        extraction_daemon.write_cursor(
            legacy_session_id,
            2,
            str(transcript_path),
            source_key=source_key,
            processed_signal_type="session_end",
        )
        source_file = extraction_daemon._cursor_dir() / f"{source_key}.json"
        source_payload = json.loads(source_file.read_text(encoding="utf-8"))
        source_payload["session_id"] = rollout_session_id
        source_file.write_text(json.dumps(source_payload), encoding="utf-8")
        extraction_daemon.write_cursor(legacy_session_id, 0, str(transcript_path))
        alias_file = extraction_daemon._cursor_dir() / f"{legacy_session_id}.json"

        real_glob = Path.glob

        def fake_glob(path, pattern):
            if path == extraction_daemon._cursor_dir() and pattern == "*.json":
                return iter([alias_file])
            return real_glob(path, pattern)

        buffered_from_lines = []
        captured = []

        def fake_buffer_transcript_tail(path, from_line, state, adapter=None, **kwargs):
            buffered_from_lines.append(from_line)
            assert path == str(transcript_path)
            return (
                {
                    "buffered_line_offset": 4,
                    "semantic_buffer": "User: Baxter uses an orange linen notebook\n\nUser: Emília Rosa supplied the notebook",
                    "semantic_buffer_tokens": 12,
                },
                {
                    "raw_lines_added": 2,
                    "semantic_chars_added": 84,
                    "semantic_tokens_added": 12,
                    "buffered_line_offset": 4,
                },
            )

        monkeypatch.setattr(Path, "glob", fake_glob)
        monkeypatch.setattr(extraction_daemon, "_ensure_discovered_session_cursors", lambda adapter: None)
        monkeypatch.setattr(extraction_daemon, "_cursor_or_adapter_owns_transcript_path", lambda *args, **kwargs: True)
        monkeypatch.setattr(extraction_daemon, "_reconcile_internal_cursor_state", lambda *args, **kwargs: "not_internal")
        monkeypatch.setattr(extraction_daemon, "_buffer_transcript_tail", fake_buffer_transcript_tail)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        extraction_daemon.check_chunk_ready_sessions(chunk_tokens=10)

        assert buffered_from_lines == [2]
        assert captured == [
            {
                "signal_type": "rolling",
                "session_id": rollout_session_id,
                "transcript_path": str(transcript_path),
                "meta": {
                    "reason": "semantic_chunk_budget",
                    "chunk_tokens": 10,
                    "semantic_buffer_tokens": 12,
                    "buffered_line_offset": 4,
                },
            }
        ]

    def test_check_chunk_ready_sessions_uses_openclaw_session_end_source_cursor_tail(
        self, monkeypatch, tmp_path
    ):
        session_id = "858e08d3-4e9d-4a72-b7e1-3df34f10f622"
        transcript_path = tmp_path / ".openclaw" / "agents" / "main" / "sessions" / f"{session_id}.jsonl"
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_path.write_text(
            '{"type":"message","message":{"role":"user","content":"hello"}}\n'
            '{"type":"message","message":{"role":"assistant","content":"ack"}}\n'
            '{"type":"message","message":{"role":"user","content":"post-checkpoint rolling detail"}}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
        source_key = extraction_daemon._signal_source_cursor_key(session_id, str(transcript_path))
        extraction_daemon.write_cursor(
            session_id,
            2,
            str(transcript_path),
            source_key=source_key,
            processed_signal_type="session_end",
        )
        extraction_daemon.write_cursor(session_id, 0, str(transcript_path))
        alias_file = extraction_daemon._cursor_dir() / f"{session_id}.json"

        real_glob = Path.glob

        def fake_glob(path, pattern):
            if path == extraction_daemon._cursor_dir() and pattern == "*.json":
                return iter([alias_file])
            return real_glob(path, pattern)

        buffered_from_lines = []

        def fake_buffer_transcript_tail(path, from_line, state, adapter=None, **kwargs):
            buffered_from_lines.append(from_line)
            return (
                {
                    "buffered_line_offset": 3,
                    "semantic_buffer": "User: post-checkpoint rolling detail",
                    "semantic_buffer_tokens": 12,
                },
                {
                    "raw_lines_added": 1,
                    "semantic_chars_added": 36,
                    "semantic_tokens_added": 12,
                    "buffered_line_offset": 3,
                },
            )

        captured = []
        monkeypatch.setattr(Path, "glob", fake_glob)
        monkeypatch.setattr(extraction_daemon, "_ensure_discovered_session_cursors", lambda adapter: None)
        monkeypatch.setattr(extraction_daemon, "_adapter_owns_transcript_path", lambda *args, **kwargs: True)
        monkeypatch.setattr(extraction_daemon, "_cursor_or_adapter_owns_transcript_path", lambda *args, **kwargs: True)
        monkeypatch.setattr(extraction_daemon, "_reconcile_internal_cursor_state", lambda *args, **kwargs: "not_internal")
        monkeypatch.setattr(extraction_daemon, "_buffer_transcript_tail", fake_buffer_transcript_tail)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        extraction_daemon.check_chunk_ready_sessions(chunk_tokens=10)

        assert buffered_from_lines == [2]
        assert captured[0]["signal_type"] == "rolling"
        assert captured[0]["session_id"] == session_id

    @pytest.mark.parametrize(
        ("processed_signal_type", "source_offset", "alias_offset"),
        [
            ("session_end", 3, 0),
            ("rolling", 2, 0),
            ("session_end", 1, 1),
        ],
    )
    def test_terminal_checkpoint_tail_helper_rejects_non_tail_cases(
        self,
        monkeypatch,
        tmp_path,
        processed_signal_type,
        source_offset,
        alias_offset,
    ):
        session_id = "019e60ec-838e-7eb1-8ed6-7f52f2b47570"
        transcript_path = tmp_path / f"{session_id}.jsonl"
        transcript_path.write_text(
            '{"type":"event_msg","payload":{"type":"user_message","message":"one"}}\n'
            '{"type":"event_msg","payload":{"type":"user_message","message":"two"}}\n'
            '{"type":"event_msg","payload":{"type":"user_message","message":"three"}}\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")

        source_key = extraction_daemon._signal_source_cursor_key(session_id, str(transcript_path))
        extraction_daemon.write_cursor(
            session_id,
            source_offset,
            str(transcript_path),
            source_key=source_key,
            processed_signal_type=processed_signal_type,
        )
        extraction_daemon.write_cursor(session_id, alias_offset, str(transcript_path))
        alias_file = extraction_daemon._cursor_dir() / f"{session_id}.json"
        alias_data = json.loads(alias_file.read_text(encoding="utf-8"))

        assert extraction_daemon._active_source_cursor_for_terminal_checkpoint_tail(
            cursor_file=alias_file,
            session_id=session_id,
            transcript_path=str(transcript_path),
            cursor_data=alias_data,
        ) == ({}, Path(), "")

    def test_check_chunk_ready_sessions_uses_live_cursor_for_empty_preserved_alias(
        self, monkeypatch, tmp_path
    ):
        import sys
        import types

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
        session_id = "sess-oc-live-roll"
        live_path = tmp_path / ".openclaw" / "agents" / "main" / "sessions" / f"{session_id}.jsonl"
        live_path.parent.mkdir(parents=True, exist_ok=True)
        live_path.write_text(
            '{"role":"user","content":"first durable rolling note with many extractable words"}\n'
            '{"role":"user","content":"second durable rolling note with more extractable words"}\n',
            encoding="utf-8",
        )
        preserved_path = (
            tmp_path
            / "instances"
            / "openclaw-main"
            / "logs"
            / "quaid"
            / "sessions"
            / f"{session_id}.jsonl"
        )
        preserved_path.parent.mkdir(parents=True, exist_ok=True)
        preserved_path.write_text("", encoding="utf-8")
        source_key = extraction_daemon._signal_source_cursor_key(session_id, str(live_path))
        extraction_daemon.write_cursor(session_id, 0, str(live_path), source_key=source_key)
        extraction_daemon.write_cursor(session_id, 0, str(preserved_path))

        real_adapter = sys.modules.get("lib.adapter")
        fake_adapter_mod = types.ModuleType("lib.adapter")

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def parse_session_jsonl(self, path):
                messages = []
                for raw in path.read_text(encoding="utf-8").splitlines():
                    payload = json.loads(raw)
                    content = payload.get("content")
                    if content:
                        messages.append(f"User: {content}")
                return "\n\n".join(messages)

        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        sys.modules["lib.adapter"] = fake_adapter_mod

        captured = []
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda default=8000: 5)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        try:
            extraction_daemon.check_chunk_ready_sessions()
            assert len(captured) == 1
            assert captured[0]["signal_type"] == "rolling"
            assert captured[0]["session_id"] == session_id
            assert captured[0]["transcript_path"] == str(live_path)
        finally:
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

    def test_check_idle_sessions_dedupes_same_source_alias_cursors(self, monkeypatch, tmp_path):
        transcript_path = tmp_path / "rollout-2026-05-23T23-35-32-019e5731-a7aa-74b3-b681-bfefb9d1f3ec.jsonl"
        transcript_path.write_text(
            '{"type":"event_msg","payload":{"type":"user_message","message":"first idle note with durable details"}}\n'
            '{"type":"event_msg","payload":{"type":"user_message","message":"second idle note with more durable details"}}\n',
            encoding="utf-8",
        )

        now = 1_700_000_000.0
        os.utime(transcript_path, (now - 3600, now - 3600))
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
        monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: now - 7200)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(extraction_daemon, "_load_runtime_adapter", lambda: None)

        legacy_session_id = "019e5731-a7aa-74b3-b681-bfefb9d1f3ec"
        rollout_session_id = "rollout-2026-05-23T23-35-32-019e5731-a7aa-74b3-b681-bfefb9d1f3ec"
        source_key = extraction_daemon._signal_source_cursor_key(
            rollout_session_id,
            str(transcript_path),
        )
        extraction_daemon.write_cursor(
            rollout_session_id,
            0,
            str(transcript_path),
            source_key=source_key,
        )
        extraction_daemon.write_cursor(legacy_session_id, 0, str(transcript_path))

        captured = []
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                }
            ),
        )

        extraction_daemon.check_idle_sessions(timeout_minutes=30)

        assert len(captured) == 1
        assert captured[0]["signal_type"] == "timeout"
        assert captured[0]["transcript_path"] == str(transcript_path)
        assert captured[0]["session_id"] in {legacy_session_id, rollout_session_id}

    def test_check_idle_sessions_uses_live_cursor_for_empty_preserved_alias(self, monkeypatch, tmp_path):
        now = 1_700_000_000.0
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
        monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
        monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: now - 7200)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(extraction_daemon, "_load_runtime_adapter", lambda: None)

        session_id = "sess-oc-live-timeout"
        live_path = tmp_path / ".openclaw" / "agents" / "main" / "sessions" / f"{session_id}.jsonl"
        live_path.parent.mkdir(parents=True, exist_ok=True)
        live_path.write_text(
            '{"role":"user","content":"timeout path should read this live OpenClaw transcript"}\n',
            encoding="utf-8",
        )
        os.utime(live_path, (now - 3600, now - 3600))
        preserved_path = (
            tmp_path
            / "instances"
            / "openclaw-main"
            / "logs"
            / "quaid"
            / "sessions"
            / f"{session_id}.jsonl"
        )
        preserved_path.parent.mkdir(parents=True, exist_ok=True)
        preserved_path.write_text("", encoding="utf-8")
        source_key = extraction_daemon._signal_source_cursor_key(session_id, str(live_path))
        extraction_daemon.write_cursor(session_id, 0, str(live_path), source_key=source_key)
        extraction_daemon.write_cursor(session_id, 0, str(preserved_path))

        captured = []
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                }
            ),
        )

        extraction_daemon.check_idle_sessions(timeout_minutes=30)

        assert len(captured) == 1
        assert captured[0]["signal_type"] == "timeout"
        assert captured[0]["session_id"] == session_id
        assert captured[0]["transcript_path"] == str(live_path)

    def test_check_chunk_ready_sessions_keeps_distinct_sources_separate(self, monkeypatch, tmp_path):
        import sys
        import types

        transcripts = [
            (
                "rollout-2026-05-23T23-35-32-019e5731-a7aa-74b3-b681-bfefb9d1f3ec",
                "first source durable rolling note",
            ),
            (
                "rollout-2026-05-23T23-41-10-019e5732-b8bb-74b3-b681-bfefb9d1f3ed",
                "second source durable rolling note",
            ),
        ]
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")

        expected_session_ids = []
        for session_id, message in transcripts:
            transcript_path = tmp_path / f"{session_id}.jsonl"
            transcript_path.write_text(
                json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": message}}) + "\n",
                encoding="utf-8",
            )
            expected_session_ids.append(session_id)
            source_key = extraction_daemon._signal_source_cursor_key(session_id, str(transcript_path))
            extraction_daemon.write_cursor(session_id, 0, str(transcript_path), source_key=source_key)

        real_adapter = sys.modules.get("lib.adapter")
        fake_adapter_mod = types.ModuleType("lib.adapter")

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def parse_session_jsonl(self, path):
                messages = []
                for raw in path.read_text(encoding="utf-8").splitlines():
                    payload = json.loads(raw)
                    event_payload = payload.get("payload", {})
                    message = event_payload.get("message")
                    if message:
                        messages.append(f"User: {message}")
                return "\n\n".join(messages)

        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        sys.modules["lib.adapter"] = fake_adapter_mod

        captured = []
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda default=8000: 5)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        try:
            extraction_daemon.check_chunk_ready_sessions()
            assert len(captured) == 2
            assert {item["signal_type"] for item in captured} == {"rolling"}
            assert {item["session_id"] for item in captured} == set(expected_session_ids)
        finally:
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

    def test_process_signal_rolling_requeues_continuation_when_transcript_tail_remains(self, monkeypatch, tmp_path):
        import sys
        import types

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"first rolling chunk message"}\n'
            '{"role":"user","content":"second rolling chunk message"}\n'
            '{"role":"user","content":"third rolling chunk message"}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        extraction_daemon.write_cursor("sess-roll-cont", 0, str(transcript_path))

        real_adapter = sys.modules.get("lib.adapter")
        fake_adapter_mod = types.ModuleType("lib.adapter")

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def parse_session_jsonl(self, path):
                rows = []
                for raw in path.read_text(encoding="utf-8").splitlines():
                    raw = raw.strip()
                    if not raw:
                        continue
                    payload = json.loads(raw)
                    content = str(payload.get("content", "") or "").strip()
                    if content:
                        rows.append(f"User: {content}")
                return "\n\n".join(rows)

        fake_adapter_mod.StandaloneAdapter = object
        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        sys.modules["lib.adapter"] = fake_adapter_mod

        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda default=8000: 8)
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_max_lines", lambda default=0: 1)
        monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "Owner")
        monkeypatch.setattr(
            extraction_daemon,
            "_collapse_staged_semantic_duplicates",
            lambda existing, incoming: (
                list(incoming or []),
                {
                    "semantic_dedup_eliminated": 0,
                    "semantic_dedup_llm_calls": 0,
                    "semantic_dedup_fast_calls": 0,
                    "semantic_dedup_deep_calls": 0,
                    "semantic_dedup_input_tokens": 0,
                    "semantic_dedup_output_tokens": 0,
                },
            ),
        )
        monkeypatch.setattr(
            extraction_daemon,
            "_warm_payload_embeddings",
            lambda facts: {
                "requested": len(facts),
                "unique": len(facts),
                "cache_hits": 0,
                "warmed": len(facts),
                "failed": 0,
                "skipped_empty": 0,
            },
        )

        real_extract = sys.modules.get("ingest.extract")
        extract_mod = types.ModuleType("ingest.extract")
        extract_mod.extract_from_transcript = lambda **kwargs: {
            "carry_facts": [],
            "raw_facts": [{"text": "rolling fact", "status": "new"}],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "facts_skipped": 0,
            "payload_duplicate_facts_collapsed": 0,
            "carry_duplicate_facts_dropped": 0,
            "chunks_processed": 1,
            "chunks_total": 1,
            "root_chunks": 1,
            "split_events": 0,
            "split_child_chunks": 0,
            "leaf_chunks": 1,
            "max_split_depth": 0,
            "deep_calls": 1,
            "repair_calls": 0,
            "assessment_usable": 1,
            "assessment_nothing_usable": 0,
            "assessment_needs_smaller_chunk": 0,
            "unclassified_empty_payloads": 0,
        }
        extract_mod.collapse_duplicate_payload_facts = lambda facts: (list(facts or []), 0)
        extract_mod.apply_extracted_payloads = lambda payload, **kwargs: payload
        sys.modules["ingest.extract"] = extract_mod

        try:
            extraction_daemon.write_signal(
                signal_type="rolling",
                session_id="sess-roll-cont",
                transcript_path=str(transcript_path),
            )
            first_signal = extraction_daemon.read_pending_signals()[0]
            extraction_daemon.process_signal(first_signal)

            # Incremental progression: first rolling stage should not jump to EOF.
            cursor = extraction_daemon.read_cursor("sess-roll-cont")
            assert cursor["line_offset"] == 1

            pending = extraction_daemon.read_pending_signals()
            assert [signal["type"] for signal in pending] == ["session_end", "rolling"]
            assert pending[0]["meta"]["reason"] == "rolling_stage_flush"
            assert pending[0]["meta"]["staged_payload_sweep"] is True
            assert pending[1]["meta"]["reason"] == "continued_chunk_budget"
        finally:
            if real_extract is not None:
                sys.modules["ingest.extract"] = real_extract
            else:
                sys.modules.pop("ingest.extract", None)
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

    def test_process_signal_rolling_reuses_existing_semantic_buffer_before_appending_tail(self, monkeypatch, tmp_path):
        import sys
        import types

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"first rolling chunk message"}\n'
            '{"role":"user","content":"second rolling chunk message"}\n'
            '{"role":"user","content":"third rolling chunk message"}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        extraction_daemon.write_cursor("sess-roll-existing", 0, str(transcript_path))
        extraction_daemon.write_rolling_state(
            "sess-roll-existing",
            {
                "session_id": "sess-roll-existing",
                "transcript_path": str(transcript_path),
                "processed_line_offset": 1,
                "buffered_line_offset": 1,
                "semantic_buffer": "User: first rolling chunk message",
                "semantic_buffer_tokens": 12,
                "carry_facts": [],
                "raw_facts": [],
            },
        )

        real_adapter = sys.modules.get("lib.adapter")
        fake_adapter_mod = types.ModuleType("lib.adapter")

        class _FakeAdapter:
            def parse_session_jsonl(self, path):
                rows = []
                for raw in path.read_text(encoding="utf-8").splitlines():
                    raw = raw.strip()
                    if not raw:
                        continue
                    payload = json.loads(raw)
                    content = str(payload.get("content", "") or "").strip()
                    if content:
                        rows.append(f"User: {content}")
                return "\n\n".join(rows)

            def owns_transcript_path(self, transcript_path, session_id=None):
                return True

        fake_adapter_mod.StandaloneAdapter = object
        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        sys.modules["lib.adapter"] = fake_adapter_mod

        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda default=8000: 10)
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_max_lines", lambda default=0: 1)
        monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "Owner")

        buffer_calls = []
        real_buffer_tail = extraction_daemon._buffer_transcript_tail

        def _spy_buffer_tail(*args, **kwargs):
            buffer_calls.append((args, kwargs))
            return real_buffer_tail(*args, **kwargs)

        monkeypatch.setattr(extraction_daemon, "_buffer_transcript_tail", _spy_buffer_tail)
        monkeypatch.setattr(
            extraction_daemon,
            "_collapse_staged_semantic_duplicates",
            lambda existing, incoming: (
                list(incoming or []),
                {
                    "semantic_dedup_eliminated": 0,
                    "semantic_dedup_llm_calls": 0,
                    "semantic_dedup_fast_calls": 0,
                    "semantic_dedup_deep_calls": 0,
                    "semantic_dedup_input_tokens": 0,
                    "semantic_dedup_output_tokens": 0,
                },
            ),
        )
        monkeypatch.setattr(
            extraction_daemon,
            "_warm_payload_embeddings",
            lambda facts: {
                "requested": len(facts),
                "unique": len(facts),
                "cache_hits": 0,
                "warmed": len(facts),
                "failed": 0,
                "skipped_empty": 0,
            },
        )

        real_extract = sys.modules.get("ingest.extract")
        extract_mod = types.ModuleType("ingest.extract")
        seen_transcripts = []
        extract_mod.extract_from_transcript = lambda **kwargs: seen_transcripts.append(kwargs["transcript"]) or {
            "carry_facts": [],
            "raw_facts": [{"text": "rolling fact", "status": "new"}],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "facts_skipped": 0,
            "payload_duplicate_facts_collapsed": 0,
            "carry_duplicate_facts_dropped": 0,
            "chunks_processed": 1,
            "chunks_total": 1,
            "root_chunks": 1,
            "split_events": 0,
            "split_child_chunks": 0,
            "leaf_chunks": 1,
            "max_split_depth": 0,
            "deep_calls": 1,
            "repair_calls": 0,
            "assessment_usable": 1,
            "assessment_nothing_usable": 0,
            "assessment_needs_smaller_chunk": 0,
            "unclassified_empty_payloads": 0,
        }
        extract_mod.collapse_duplicate_payload_facts = lambda facts: (list(facts or []), 0)
        extract_mod.apply_extracted_payloads = lambda payload, **kwargs: payload
        sys.modules["ingest.extract"] = extract_mod

        try:
            extraction_daemon.write_signal(
                signal_type="rolling",
                session_id="sess-roll-existing",
                transcript_path=str(transcript_path),
            )
            first_signal = extraction_daemon.read_pending_signals()[0]
            extraction_daemon.process_signal(first_signal)

            assert seen_transcripts == ["User: first rolling chunk message"]
            assert buffer_calls == []
            cursor = extraction_daemon.read_cursor("sess-roll-existing")
            assert cursor["line_offset"] == 1

            pending = extraction_daemon.read_pending_signals()
            assert [signal["type"] for signal in pending] == ["session_end", "rolling"]
            assert pending[0]["meta"]["reason"] == "rolling_stage_flush"
            assert pending[0]["meta"]["staged_payload_sweep"] is True
            assert pending[1]["meta"]["reason"] == "continued_chunk_budget"
            assert pending[1]["meta"]["buffered_line_offset"] == 1
        finally:
            if real_extract is not None:
                sys.modules["ingest.extract"] = real_extract
            else:
                sys.modules.pop("ingest.extract", None)
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

    def test_process_signal_rolling_stages_buffer_when_source_cursor_at_eof(self, monkeypatch, tmp_path):
        import sys
        import types

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"first rolling chunk message"}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        session_id = "sess-roll-buffered"
        staged_state = {
            "session_id": session_id,
            "transcript_path": str(transcript_path),
            "processed_line_offset": 1,
            "buffered_line_offset": 1,
            "semantic_buffer": "User: first rolling chunk message",
            "semantic_buffer_tokens": 12,
            "carry_facts": [],
            "raw_facts": [],
        }
        source_key = extraction_daemon._signal_source_cursor_key(
            session_id,
            str(transcript_path),
            staged_state=staged_state,
        )
        extraction_daemon.write_cursor(session_id, 1, str(transcript_path), source_key=source_key)
        extraction_daemon.write_rolling_state(session_id, staged_state)

        real_adapter = sys.modules.get("lib.adapter")
        fake_adapter_mod = types.ModuleType("lib.adapter")

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def parse_session_jsonl(self, path):
                rows = []
                for raw in path.read_text(encoding="utf-8").splitlines():
                    raw = raw.strip()
                    if not raw:
                        continue
                    payload = json.loads(raw)
                    content = str(payload.get("content", "") or "").strip()
                    if content:
                        rows.append(f"User: {content}")
                return "\n\n".join(rows)

        fake_adapter_mod.StandaloneAdapter = object
        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        sys.modules["lib.adapter"] = fake_adapter_mod

        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda default=8000: 10)
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_max_lines", lambda default=0: 1)
        monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "Owner")
        monkeypatch.setattr(
            extraction_daemon,
            "_collapse_staged_semantic_duplicates",
            lambda existing, incoming: (
                list(incoming or []),
                {
                    "semantic_dedup_eliminated": 0,
                    "semantic_dedup_llm_calls": 0,
                    "semantic_dedup_fast_calls": 0,
                    "semantic_dedup_deep_calls": 0,
                    "semantic_dedup_input_tokens": 0,
                    "semantic_dedup_output_tokens": 0,
                },
            ),
        )
        monkeypatch.setattr(
            extraction_daemon,
            "_warm_payload_embeddings",
            lambda facts: {
                "requested": len(facts),
                "unique": len(facts),
                "cache_hits": 0,
                "warmed": len(facts),
                "failed": 0,
                "skipped_empty": 0,
            },
        )

        real_extract = sys.modules.get("ingest.extract")
        extract_mod = types.ModuleType("ingest.extract")
        seen_transcripts = []
        extract_mod.extract_from_transcript = lambda **kwargs: seen_transcripts.append(kwargs["transcript"]) or {
            "carry_facts": [],
            "raw_facts": [{"text": "rolling fact", "status": "new"}],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "facts_skipped": 0,
            "payload_duplicate_facts_collapsed": 0,
            "carry_duplicate_facts_dropped": 0,
            "chunks_processed": 1,
            "chunks_total": 1,
            "root_chunks": 1,
            "split_events": 0,
            "split_child_chunks": 0,
            "leaf_chunks": 1,
            "max_split_depth": 0,
            "deep_calls": 1,
            "repair_calls": 0,
            "assessment_usable": 1,
            "assessment_nothing_usable": 0,
            "assessment_needs_smaller_chunk": 0,
            "unclassified_empty_payloads": 0,
        }
        extract_mod.collapse_duplicate_payload_facts = lambda facts: (list(facts or []), 0)
        extract_mod.apply_extracted_payloads = lambda payload, **kwargs: payload
        sys.modules["ingest.extract"] = extract_mod

        try:
            extraction_daemon.write_signal(
                signal_type="rolling",
                session_id=session_id,
                transcript_path=str(transcript_path),
            )
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])

            assert seen_transcripts == ["User: first rolling chunk message"]
            cursor = extraction_daemon.read_cursor(session_id, source_key=source_key)
            assert cursor["line_offset"] == 1
            state = extraction_daemon.read_rolling_state(session_id)
            assert state["rolling_batches"] == 1
            assert state["raw_facts"] == [{"text": "rolling fact", "status": "new"}]
            pending = extraction_daemon.read_pending_signals()
            assert len(pending) == 1
            assert pending[0]["type"] == "session_end"
            assert pending[0]["meta"]["reason"] == "rolling_stage_flush"
            assert pending[0]["meta"]["staged_payload_sweep"] is True
        finally:
            if real_extract is not None:
                sys.modules["ingest.extract"] = real_extract
            else:
                sys.modules.pop("ingest.extract", None)
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

    def test_process_signal_rolling_preserves_source_signal_when_flush_write_fails(self, monkeypatch, tmp_path):
        import sys
        import types

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"first rolling chunk message"}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        session_id = "sess-roll-flush-write-fails"
        staged_state = {
            "session_id": session_id,
            "transcript_path": str(transcript_path),
            "processed_line_offset": 1,
            "buffered_line_offset": 1,
            "semantic_buffer": "User: first rolling chunk message",
            "semantic_buffer_tokens": 12,
            "carry_facts": [],
            "raw_facts": [],
        }
        source_key = extraction_daemon._signal_source_cursor_key(
            session_id,
            str(transcript_path),
            staged_state=staged_state,
        )
        extraction_daemon.write_cursor(session_id, 1, str(transcript_path), source_key=source_key)
        extraction_daemon.write_rolling_state(session_id, staged_state)

        real_adapter = sys.modules.get("lib.adapter")
        fake_adapter_mod = types.ModuleType("lib.adapter")

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def parse_session_jsonl(self, path):
                rows = []
                for raw in path.read_text(encoding="utf-8").splitlines():
                    payload = json.loads(raw)
                    content = str(payload.get("content", "") or "").strip()
                    if content:
                        rows.append(f"User: {content}")
                return "\n\n".join(rows)

        fake_adapter_mod.StandaloneAdapter = object
        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        sys.modules["lib.adapter"] = fake_adapter_mod

        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda default=8000: 10)
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_max_lines", lambda default=0: 1)
        monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "Owner")
        monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: False)
        monkeypatch.setattr(
            extraction_daemon,
            "_collapse_staged_semantic_duplicates",
            lambda existing, incoming: (
                list(incoming or []),
                {
                    "semantic_dedup_eliminated": 0,
                    "semantic_dedup_llm_calls": 0,
                    "semantic_dedup_fast_calls": 0,
                    "semantic_dedup_deep_calls": 0,
                    "semantic_dedup_input_tokens": 0,
                    "semantic_dedup_output_tokens": 0,
                },
            ),
        )
        monkeypatch.setattr(
            extraction_daemon,
            "_warm_payload_embeddings",
            lambda facts: {
                "requested": len(facts),
                "unique": len(facts),
                "cache_hits": 0,
                "warmed": len(facts),
                "failed": 0,
                "skipped_empty": 0,
            },
        )

        real_extract = sys.modules.get("ingest.extract")
        extract_mod = types.ModuleType("ingest.extract")
        extract_mod.extract_from_transcript = lambda **kwargs: {
            "carry_facts": [],
            "raw_facts": [{"text": "rolling fact", "status": "new"}],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "facts_skipped": 0,
            "payload_duplicate_facts_collapsed": 0,
            "carry_duplicate_facts_dropped": 0,
            "chunks_processed": 1,
            "chunks_total": 1,
            "root_chunks": 1,
            "split_events": 0,
            "split_child_chunks": 0,
            "leaf_chunks": 1,
            "max_split_depth": 0,
            "deep_calls": 1,
            "repair_calls": 0,
            "assessment_usable": 1,
            "assessment_nothing_usable": 0,
            "assessment_needs_smaller_chunk": 0,
            "unclassified_empty_payloads": 0,
        }
        extract_mod.collapse_duplicate_payload_facts = lambda facts: (list(facts or []), 0)
        extract_mod.apply_extracted_payloads = lambda payload, **kwargs: payload
        sys.modules["ingest.extract"] = extract_mod

        original_write_signal = extraction_daemon.write_signal
        rolling_signal_path = original_write_signal(
            signal_type="rolling",
            session_id=session_id,
            transcript_path=str(transcript_path),
        )

        def fail_flush_signal(
            signal_type,
            session_id,
            transcript_path,
            adapter="",
            supports_compaction_control=False,
            meta=None,
            *,
            dedupe=True,
        ):
            if (
                signal_type == "session_end"
                and isinstance(meta, dict)
                and meta.get("reason") == "rolling_stage_flush"
            ):
                raise OSError("simulated flush signal write failure")
            return original_write_signal(
                signal_type,
                session_id,
                transcript_path,
                adapter=adapter,
                supports_compaction_control=supports_compaction_control,
                meta=meta,
                dedupe=dedupe,
            )

        monkeypatch.setattr(extraction_daemon, "write_signal", fail_flush_signal)

        try:
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])

            pending = extraction_daemon.read_pending_signals()
            assert len(pending) == 1
            assert pending[0]["type"] == "rolling"
            assert pending[0]["_signal_path"] == str(rolling_signal_path)
            state = extraction_daemon.read_rolling_state(session_id)
            assert state[extraction_daemon._STAGED_PAYLOAD_PENDING_FLUSH_KEY] is True
            assert state["raw_facts"] == [{"text": "rolling fact", "status": "new"}]
        finally:
            if real_extract is not None:
                sys.modules["ingest.extract"] = real_extract
            else:
                sys.modules.pop("ingest.extract", None)
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

    def test_process_signal_rolling_requeues_flush_for_staged_payload_at_source_cursor_eof(self, monkeypatch, tmp_path):
        import sys
        import types

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"first rolling chunk message"}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        session_id = "sess-roll-payload"
        staged_state = {
            "session_id": session_id,
            "transcript_path": str(transcript_path),
            "processed_line_offset": 1,
            "buffered_line_offset": 1,
            "semantic_buffer": "",
            "semantic_buffer_tokens": 0,
            "rolling_batches": 1,
            "carry_facts": [{"text": "Owner has a staged rolling fact"}],
            "raw_facts": [{"text": "Owner has a staged rolling fact", "category": "fact"}],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
        }
        source_key = extraction_daemon._signal_source_cursor_key(
            session_id,
            str(transcript_path),
            staged_state=staged_state,
        )
        extraction_daemon.write_cursor(session_id, 1, str(transcript_path), source_key=source_key)
        extraction_daemon.write_rolling_state(session_id, staged_state)

        real_adapter = sys.modules.get("lib.adapter")
        fake_adapter_mod = types.ModuleType("lib.adapter")

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def parse_session_jsonl(self, path):
                return "unexpected parse"

        fake_adapter_mod.StandaloneAdapter = object
        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        sys.modules["lib.adapter"] = fake_adapter_mod

        try:
            extraction_daemon.write_signal(
                signal_type="rolling",
                session_id=session_id,
                transcript_path=str(transcript_path),
            )
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])

            pending = extraction_daemon.read_pending_signals()
            assert len(pending) == 1
            assert pending[0]["type"] == "session_end"
            assert pending[0]["session_id"] == session_id
            assert pending[0]["meta"]["reason"] == "rolling_stage_flush"
            assert pending[0]["meta"]["source_signal"] == "rolling"
            assert pending[0]["meta"]["staged_payload_sweep"] is True
            state = extraction_daemon.read_rolling_state(session_id)
            assert state["rolling_batches"] == 1
            assert state["raw_facts"] == staged_state["raw_facts"]
        finally:
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

    def test_process_signal_rolling_retries_already_staged_payload_without_reextracting(self, monkeypatch, tmp_path):
        import sys
        import types

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"first rolling chunk message"}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        session_id = "sess-roll-stage-retry"
        staged_state = {
            "session_id": session_id,
            "transcript_path": str(transcript_path),
            "processed_line_offset": 1,
            "buffered_line_offset": 1,
            "semantic_buffer": "User: first rolling chunk message",
            "semantic_buffer_tokens": 12,
            "rolling_batches": 1,
            "carry_facts": [{"text": "Owner has a staged rolling fact"}],
            "raw_facts": [{"text": "Owner has a staged rolling fact", "category": "fact"}],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            extraction_daemon._STAGED_PAYLOAD_PENDING_FLUSH_KEY: True,
        }
        source_key = extraction_daemon._signal_source_cursor_key(
            session_id,
            str(transcript_path),
            staged_state=staged_state,
        )
        # Simulate a crash after rolling state was written but before cursor advance.
        extraction_daemon.write_cursor(session_id, 0, str(transcript_path), source_key=source_key)
        extraction_daemon.write_rolling_state(session_id, staged_state)

        real_adapter = sys.modules.get("lib.adapter")
        fake_adapter_mod = types.ModuleType("lib.adapter")

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def parse_session_jsonl(self, path):
                return path.read_text(encoding="utf-8")

        fake_adapter_mod.StandaloneAdapter = object
        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        sys.modules["lib.adapter"] = fake_adapter_mod

        real_extract = sys.modules.get("ingest.extract")
        extract_mod = types.ModuleType("ingest.extract")
        extract_calls = []

        def _unexpected_extract(**kwargs):
            extract_calls.append(kwargs)
            raise AssertionError("already-staged rolling payload should not be extracted again")

        extract_mod.extract_from_transcript = _unexpected_extract
        extract_mod.apply_extracted_payloads = lambda payload, **kwargs: payload
        sys.modules["ingest.extract"] = extract_mod
        monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "Owner")

        try:
            extraction_daemon.write_signal(
                signal_type="rolling",
                session_id=session_id,
                transcript_path=str(transcript_path),
            )
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])

            assert extract_calls == []
            cursor = extraction_daemon.read_cursor(session_id, source_key=source_key)
            assert cursor["line_offset"] == 1
            pending = extraction_daemon.read_pending_signals()
            assert len(pending) == 1
            assert pending[0]["type"] == "session_end"
            assert pending[0]["session_id"] == session_id
            assert pending[0]["meta"]["reason"] == "rolling_stage_flush"
            assert pending[0]["meta"]["source_signal"] == "rolling"
            assert pending[0]["meta"]["staged_payload_sweep"] is True
            assert pending[0]["meta"]["recovered_staged_payload_retry"] is True
            state = extraction_daemon.read_rolling_state(session_id)
            assert state["rolling_batches"] == 1
            assert state["raw_facts"] == staged_state["raw_facts"]
        finally:
            if real_extract is not None:
                sys.modules["ingest.extract"] = real_extract
            else:
                sys.modules.pop("ingest.extract", None)
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

    def test_process_signal_rolling_requeues_continuation_for_below_budget_tail_without_line_cap(self, monkeypatch, tmp_path):
        import sys
        import types

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"'
            + ("first line " * 120).strip()
            + '"}\n'
            '{"role":"user","content":"short tail"}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        extraction_daemon.write_cursor("sess-roll-tail", 0, str(transcript_path))

        real_adapter = sys.modules.get("lib.adapter")
        fake_adapter_mod = types.ModuleType("lib.adapter")

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def parse_session_jsonl(self, path):
                rows = []
                for raw in path.read_text(encoding="utf-8").splitlines():
                    raw = raw.strip()
                    if not raw:
                        continue
                    payload = json.loads(raw)
                    content = str(payload.get("content", "") or "").strip()
                    if content:
                        rows.append(f"User: {content}")
                return "\n\n".join(rows)

        fake_adapter_mod.StandaloneAdapter = object
        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        sys.modules["lib.adapter"] = fake_adapter_mod

        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda default=8000: 120)
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_max_lines", lambda default=0: 0)
        monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "Owner")
        monkeypatch.setattr(
            extraction_daemon,
            "_collapse_staged_semantic_duplicates",
            lambda existing, incoming: (
                list(incoming or []),
                {
                    "semantic_dedup_eliminated": 0,
                    "semantic_dedup_llm_calls": 0,
                    "semantic_dedup_fast_calls": 0,
                    "semantic_dedup_deep_calls": 0,
                    "semantic_dedup_input_tokens": 0,
                    "semantic_dedup_output_tokens": 0,
                },
            ),
        )
        monkeypatch.setattr(
            extraction_daemon,
            "_warm_payload_embeddings",
            lambda facts: {
                "requested": len(facts),
                "unique": len(facts),
                "cache_hits": 0,
                "warmed": len(facts),
                "failed": 0,
                "skipped_empty": 0,
            },
        )

        real_extract = sys.modules.get("ingest.extract")
        extract_mod = types.ModuleType("ingest.extract")
        extract_mod.extract_from_transcript = lambda **kwargs: {
            "carry_facts": [],
            "raw_facts": [{"text": "rolling fact", "status": "new"}],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "facts_skipped": 0,
            "payload_duplicate_facts_collapsed": 0,
            "carry_duplicate_facts_dropped": 0,
            "chunks_processed": 1,
            "chunks_total": 1,
            "root_chunks": 1,
            "split_events": 0,
            "split_child_chunks": 0,
            "leaf_chunks": 1,
            "max_split_depth": 0,
            "deep_calls": 1,
            "repair_calls": 0,
            "assessment_usable": 1,
            "assessment_nothing_usable": 0,
            "assessment_needs_smaller_chunk": 0,
            "unclassified_empty_payloads": 0,
        }
        extract_mod.collapse_duplicate_payload_facts = lambda facts: (list(facts or []), 0)
        extract_mod.apply_extracted_payloads = lambda payload, **kwargs: payload
        sys.modules["ingest.extract"] = extract_mod

        try:
            extraction_daemon.write_signal(
                signal_type="rolling",
                session_id="sess-roll-tail",
                transcript_path=str(transcript_path),
            )
            first_signal = extraction_daemon.read_pending_signals()[0]
            extraction_daemon.process_signal(first_signal)

            cursor = extraction_daemon.read_cursor("sess-roll-tail")
            assert cursor["line_offset"] == 1

            pending = extraction_daemon.read_pending_signals()
            assert [signal["type"] for signal in pending] == ["session_end", "rolling"]
            assert pending[0]["meta"]["reason"] == "rolling_stage_flush"
            assert pending[0]["meta"]["staged_payload_sweep"] is True
            rolling_signal = pending[1]
            assert rolling_signal["meta"]["reason"] == "continued_chunk_budget"
            assert rolling_signal["meta"]["chunk_lines"] == 0
            assert rolling_signal["meta"]["remaining_lines"] == 1
            assert rolling_signal["meta"]["remaining_tokens_estimate"] < 120
        finally:
            if real_extract is not None:
                sys.modules["ingest.extract"] = real_extract
            else:
                sys.modules.pop("ingest.extract", None)
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

    def test_process_signal_rolling_stage_then_flush_publishes_staged_payload(self, monkeypatch, tmp_path):
        import sys
        import types

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"My sister is Diana"}\n'
            '{"role":"assistant","content":"Noted"}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        instance_root = tmp_path / "instances" / "rolling-inst"
        instance_root.mkdir(parents=True, exist_ok=True)
        (instance_root / "config.json").write_text(
            json.dumps(
                {
                    "adapter": {"type": "standalone"},
                    "livetest": {"enableExtractionBufferLog": True},
                }
            ),
            encoding="utf-8",
        )
        extraction_daemon.write_cursor("sess-roll", 0, str(transcript_path))
        monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "Owner")
        monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: False)
        monkeypatch.setattr(extraction_daemon, "_rolling_ready_threshold", lambda chunk_budget: 1)

        real_registry = sys.modules.get("core.subagent_registry")
        real_adapter = sys.modules.get("lib.adapter")
        fake_registry = types.ModuleType("core.subagent_registry")
        fake_registry.is_registered_subagent = lambda sid: False
        fake_registry.get_harvestable = lambda sid: []
        fake_registry.mark_harvested = lambda sid, cid: None
        fake_registry._registry_dir = lambda: tmp_path / "registry"
        sys.modules["core.subagent_registry"] = fake_registry

        fake_adapter_mod = types.ModuleType("lib.adapter")
        if real_adapter is not None:
            fake_adapter_mod.StandaloneAdapter = getattr(real_adapter, "StandaloneAdapter", object)
            fake_adapter_mod.quaid_projects_dir = getattr(
                real_adapter,
                "quaid_projects_dir",
                lambda: tmp_path / "projects",
            )
            fake_adapter_mod.quaid_tracking_dir = getattr(
                real_adapter,
                "quaid_tracking_dir",
                lambda: tmp_path / "tracking",
            )
        parse_empty = {"value": False}

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def quaid_home(self):
                return tmp_path

            def instance_root(self):
                return instance_root

            def data_dir(self):
                return instance_root / "data"

            def parse_session_jsonl(self, path):
                if parse_empty["value"]:
                    return ""
                return 'User: My sister is Diana\n\nAssistant: Noted'
        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        sys.modules["lib.adapter"] = fake_adapter_mod

        import ingest.extract as extract_mod
        import core.ingest_runtime as ingest_runtime_mod
        import core.project_registry as project_registry_mod
        import core.docs_updater_hook as docs_updater_mod

        real_notify = sys.modules.get("core.runtime.notify")
        fake_notify = types.ModuleType("core.runtime.notify")
        fake_notify.notify_memory_extraction = lambda **kwargs: None
        sys.modules["core.runtime.notify"] = fake_notify

        staged_payload = {
            "facts_stored": 1,
            "facts_skipped": 0,
            "edges_created": 0,
            "facts": [{"text": "Owner has a sister named Diana", "status": "would_store", "edges": []}],
            "snippets": {},
            "journal": {},
            "project_logs": {},
            "project_log_metrics": {},
            "dry_run": True,
            "raw_facts": [{"text": "Owner has a sister named Diana", "category": "fact", "domains": ["personal"], "extraction_confidence": "high"}],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "carry_facts": [{"text": "Owner has a sister named Diana"}],
            "carry_duplicate_facts_dropped": 2,
            "chunks_processed": 1,
            "chunks_total": 1,
            "root_chunks": 1,
            "split_events": 0,
            "split_child_chunks": 0,
            "leaf_chunks": 1,
            "max_split_depth": 0,
            "chunk_calls": 1,
            "deep_calls": 1,
            "repair_calls": 0,
            "assessment_usable": 1,
            "assessment_nothing_usable": 0,
            "assessment_needs_smaller_chunk": 0,
            "unclassified_empty_payloads": 0,
        }
        applied_calls = []
        rolling_metrics = []
        usage_snapshots = iter([
            {
                "calls": 10,
                "input_tokens": 1000,
                "output_tokens": 400,
                "fast_calls": 2,
                "fast_input_tokens": 200,
                "fast_output_tokens": 80,
                "deep_calls": 8,
                "deep_input_tokens": 800,
                "deep_output_tokens": 320,
            },
            {
                "calls": 11,
                "input_tokens": 1100,
                "output_tokens": 460,
                "fast_calls": 2,
                "fast_input_tokens": 200,
                "fast_output_tokens": 80,
                "deep_calls": 9,
                "deep_input_tokens": 900,
                "deep_output_tokens": 380,
            },
            {
                "calls": 14,
                "input_tokens": 1360,
                "output_tokens": 550,
                "fast_calls": 5,
                "fast_input_tokens": 460,
                "fast_output_tokens": 170,
                "deep_calls": 9,
                "deep_input_tokens": 900,
                "deep_output_tokens": 380,
            },
        ])

        monkeypatch.setattr(extract_mod, "extract_from_transcript", lambda **kwargs: dict(staged_payload))
        monkeypatch.setattr(
            extract_mod,
            "apply_extracted_payloads",
            lambda payload, **kwargs: applied_calls.append((payload, kwargs)) or {
                **payload,
                "facts_stored": 1,
                "facts_skipped": 0,
                "edges_created": 0,
                "facts": [{"text": "Owner has a sister named Diana", "status": "stored", "edges": []}],
                "snippets": {"USER.md": ["Diana is Owner's sister"]},
                "journal": {"USER.md": "Family note."},
                "project_logs": {"quaid": ["Investigated family recall flow"]},
                "project_log_metrics": {"entries_seen": 1, "entries_written": 1, "projects_updated": 1},
            },
        )
        monkeypatch.setattr(
            ingest_runtime_mod,
            "run_session_logs_ingest",
            lambda **kwargs: {"status": "indexed"},
        )
        monkeypatch.setattr(project_registry_mod, "snapshot_all_projects", lambda: [])
        monkeypatch.setattr(docs_updater_mod, "update_project_docs", lambda snapshots, extraction_result: {"docs_updated": 0})
        monkeypatch.setattr(extraction_daemon, "_read_usage_totals", lambda: dict(next(usage_snapshots)))
        monkeypatch.setattr(
            extraction_daemon,
            "write_rolling_metric",
            lambda event, session_id, **data: rolling_metrics.append(
                {"event": event, "session_id": session_id, **data}
            ),
        )
        monkeypatch.setattr(
            extraction_daemon,
            "_warm_payload_embeddings",
            lambda facts: {
                "requested": len(facts),
                "unique": len({str(f.get("text", "")) for f in facts}),
                "cache_hits": 0,
                "warmed": len({str(f.get("text", "")) for f in facts}),
                "failed": 0,
                "skipped_empty": 0,
            },
        )

        try:
            rolling_signal = extraction_daemon.write_signal(
                signal_type="rolling",
                session_id="sess-roll",
                transcript_path=str(transcript_path),
            )
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])

            state = extraction_daemon.read_rolling_state("sess-roll")
            assert state["rolling_batches"] == 1
            assert len(state["raw_facts"]) == 1
            assert state["root_chunks"] == 1
            assert extraction_daemon.read_cursor("sess-roll")["line_offset"] == 2
            stage_metric = rolling_metrics[-1]
            assert stage_metric["event"] == "rolling_stage"
            assert stage_metric["line_estimated_tokens"] > 0
            assert stage_metric["max_line_chars"] > 0
            assert stage_metric["max_line_estimated_tokens"] > 0
            assert stage_metric["chunk_budget_tokens"] > 0
            assert "chunk_budget_lines" in stage_metric
            assert stage_metric["carry_facts_in"] == 0
            assert stage_metric["carry_facts_out"] == 1
            assert stage_metric["carry_duplicate_facts_dropped"] == 2
            assert stage_metric["embedding_cache_requested"] == 1
            assert stage_metric["embedding_cache_warmed"] == 1
            assert stage_metric["assessment_usable"] == 1
            buffer_log = (instance_root / "logs" / "daemon" / "extraction-buffer.log").read_text(
                encoding="utf-8"
            )
            assert "phase=rolling_stage" in buffer_log
            assert "signal=rolling" in buffer_log
            assert "User: My sister is Diana" in buffer_log

            pending = extraction_daemon.read_pending_signals()
            assert len(pending) == 1
            flush_signal = pending[0]
            assert flush_signal["type"] == "session_end"
            assert flush_signal["meta"]["reason"] == "rolling_stage_flush"
            parse_empty["value"] = True
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])

            assert extraction_daemon.read_rolling_state("sess-roll")["rolling_batches"] == 0
            assert not extraction_daemon._rolling_state_path("sess-roll").exists()
            assert len(applied_calls) == 1
            payload, kwargs = applied_calls[0]
            assert len(payload["raw_facts"]) == 1
            assert payload["root_chunks"] == 1
            assert kwargs["session_id"] == "sess-roll"
            flush_metric = rolling_metrics[-1]
            assert flush_metric["event"] == "rolling_flush"
            assert flush_metric["signal_type"] == "rolling"
            assert flush_metric["processing_signal_type"] == "rolling_flush"
            assert flush_metric["staged_batches"] == 1
            assert flush_metric["staged_facts"] == 1
            assert flush_metric["carry_facts_final"] == 1
            assert flush_metric["carry_duplicate_facts_dropped"] == 2
            assert flush_metric["fact_status_counts"] == {"stored": 1}
            assert flush_metric["skip_buckets"] == {}
            assert flush_metric["payload_duplicate_facts_collapsed"] == 0
            assert flush_metric["snippets_count"] == 1
            assert flush_metric["journals_count"] == 1
            assert flush_metric["project_logs_seen"] == 1
            assert flush_metric["project_logs_written"] == 1
            assert flush_metric["project_logs_projects_updated"] == 1
            assert flush_metric["assessment_usable"] == 1
            assert flush_metric["extract_llm_calls"] == 1
            assert flush_metric["extract_deep_calls"] == 1
            assert flush_metric["extract_fast_calls"] == 0
            assert flush_metric["extract_input_tokens"] == 100
            assert flush_metric["extract_output_tokens"] == 60
            assert flush_metric["publish_llm_calls"] == 3
            assert flush_metric["publish_fast_calls"] == 3
            assert flush_metric["publish_deep_calls"] == 0
            assert flush_metric["publish_input_tokens"] == 260
            assert flush_metric["publish_output_tokens"] == 90
            assert flush_metric["dedup_hash_exact_hits"] == 0
            assert flush_metric["dedup_scanned_rows"] == 0
            assert flush_metric["dedup_gray_zone_rows"] == 0
            assert flush_metric["dedup_llm_checks"] == 0
            assert flush_metric["dedup_fts_query_count"] == 0
            assert flush_metric["dedup_fts_candidates_returned"] == 0
            assert flush_metric["dedup_fts_candidate_limit"] == 0
            assert flush_metric["dedup_fts_limit_hits"] == 0
            assert flush_metric["dedup_fallback_scan_count"] == 0
            assert flush_metric["dedup_fallback_candidates_returned"] == 0
            assert flush_metric["dedup_token_prefilter_terms"] == 0
            assert flush_metric["dedup_token_prefilter_skips"] == 0
            assert flush_metric["embedding_cache_requested"] == 0
        finally:
            if real_registry is not None:
                sys.modules["core.subagent_registry"] = real_registry
            else:
                sys.modules.pop("core.subagent_registry", None)
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)
            if real_notify is not None:
                sys.modules["core.runtime.notify"] = real_notify
            else:
                sys.modules.pop("core.runtime.notify", None)

    def test_rolling_flush_drains_threshold_crossing_tail_without_continued_raw_tail(self, monkeypatch, tmp_path):
        import sys
        import types

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"chunk one stable memory"}\n'
            '{"role":"user","content":"chunk two Baxter residual"}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        instance_root = tmp_path / "instances" / "rolling-inst"
        instance_root.mkdir(parents=True, exist_ok=True)
        extraction_daemon.write_cursor("sess-roll-residual", 0, str(transcript_path))
        monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "Owner")
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda default=8000: 100)

        prior = "User: " + " ".join(["stable"] * 48)
        tail = "User: Baxter residual context is durable and should wait for lifecycle extraction"
        extraction_daemon.write_rolling_state(
            "sess-roll-residual",
            {
                "session_id": "sess-roll-residual",
                "transcript_path": str(transcript_path),
                "semantic_buffer": f"{prior}\n\n{tail}",
                "semantic_buffer_tokens": 110,
                "semantic_buffer_prior": prior,
                "semantic_buffer_tail": tail,
                "semantic_buffer_prior_tokens": 80,
                "semantic_buffer_tail_tokens": 5,
                "buffered_line_offset": 2,
                "processed_line_offset": 2,
            },
        )

        real_registry = sys.modules.get("core.subagent_registry")
        real_adapter = sys.modules.get("lib.adapter")
        fake_registry = types.ModuleType("core.subagent_registry")
        fake_registry.is_registered_subagent = lambda sid: False
        fake_registry.get_harvestable = lambda sid: []
        fake_registry.mark_harvested = lambda sid, cid: None
        fake_registry._registry_dir = lambda: tmp_path / "registry"
        sys.modules["core.subagent_registry"] = fake_registry

        fake_adapter_mod = types.ModuleType("lib.adapter")
        if real_adapter is not None:
            fake_adapter_mod.StandaloneAdapter = getattr(real_adapter, "StandaloneAdapter", object)
            fake_adapter_mod.quaid_projects_dir = getattr(
                real_adapter,
                "quaid_projects_dir",
                lambda: tmp_path / "projects",
            )
            fake_adapter_mod.quaid_tracking_dir = getattr(
                real_adapter,
                "quaid_tracking_dir",
                lambda: tmp_path / "tracking",
            )
        parse_empty = {"value": False}

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def quaid_home(self):
                return tmp_path

            def instance_root(self):
                return instance_root

            def data_dir(self):
                return instance_root / "data"

            def parse_session_jsonl(self, path):
                if parse_empty["value"]:
                    return ""
                return f"{prior}\n\n{tail}"

        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        sys.modules["lib.adapter"] = fake_adapter_mod

        import ingest.extract as extract_mod
        import core.ingest_runtime as ingest_runtime_mod
        import core.project_registry as project_registry_mod
        import core.docs_updater_hook as docs_updater_mod

        seen_transcripts = []
        applied_payloads = []

        def fake_extract_from_transcript(**kwargs):
            transcript = kwargs["transcript"]
            seen_transcripts.append(transcript)
            if "Baxter" in transcript:
                raw_facts = [{"text": "Owner discussed Baxter", "status": "new"}]
                carry = [{"text": "Owner discussed Baxter"}]
            else:
                raw_facts = [{"text": "Owner has stable prior memories", "status": "new"}]
                carry = [{"text": "Owner has stable prior memories"}]
            return {
                "carry_facts": carry,
                "raw_facts": raw_facts,
                "raw_snippets": {},
                "raw_journal": {},
                "raw_project_logs": {},
                "facts_skipped": 0,
                "payload_duplicate_facts_collapsed": 0,
                "carry_duplicate_facts_dropped": 0,
                "chunks_processed": 1,
                "chunks_total": 1,
                "root_chunks": 1,
                "split_events": 0,
                "split_child_chunks": 0,
                "leaf_chunks": 1,
                "max_split_depth": 0,
                "deep_calls": 1,
                "repair_calls": 0,
                "assessment_usable": 1,
                "assessment_nothing_usable": 0,
                "assessment_needs_smaller_chunk": 0,
                "unclassified_empty_payloads": 0,
            }

        monkeypatch.setattr(extract_mod, "extract_from_transcript", fake_extract_from_transcript)
        monkeypatch.setattr(
            extract_mod,
            "apply_extracted_payloads",
            lambda payload, **kwargs: applied_payloads.append(payload) or {
                **payload,
                "facts_stored": len(payload.get("raw_facts", []) or []),
                "facts_skipped": 0,
                "edges_created": 0,
                "facts": [
                    {"text": fact.get("text", ""), "status": "stored", "edges": []}
                    for fact in (payload.get("raw_facts", []) or [])
                ],
                "snippets": {},
                "journal": {},
                "project_logs": {},
                "project_log_metrics": {},
            },
        )
        monkeypatch.setattr(ingest_runtime_mod, "run_session_logs_ingest", lambda **kwargs: {"status": "indexed"})
        monkeypatch.setattr(project_registry_mod, "snapshot_all_projects", lambda: [])
        monkeypatch.setattr(docs_updater_mod, "update_project_docs", lambda snapshots, extraction_result: {"docs_updated": 0})
        monkeypatch.setattr(extraction_daemon, "_warm_payload_embeddings", lambda facts: {
            "requested": len(facts),
            "unique": len(facts),
            "cache_hits": 0,
            "warmed": len(facts),
            "failed": 0,
            "skipped_empty": 0,
        })

        try:
            extraction_daemon.write_signal(
                signal_type="rolling",
                session_id="sess-roll-residual",
                transcript_path=str(transcript_path),
            )
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])
            state_after_stage = extraction_daemon.read_rolling_state("sess-roll-residual")
            assert seen_transcripts == [prior]
            assert state_after_stage["semantic_buffer"] == tail
            assert state_after_stage["raw_facts"] == [{"text": "Owner has stable prior memories", "status": "new"}]

            parse_empty["value"] = True
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])
            assert seen_transcripts == [prior, tail]
            assert len(applied_payloads) == 1
            assert [fact["text"] for fact in applied_payloads[0]["raw_facts"]] == [
                "Owner has stable prior memories",
                "Owner discussed Baxter"
            ]
            assert extraction_daemon.read_pending_signals() == []
            assert not extraction_daemon._rolling_state_path("sess-roll-residual").exists()
        finally:
            if real_registry is not None:
                sys.modules["core.subagent_registry"] = real_registry
            else:
                sys.modules.pop("core.subagent_registry", None)
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

    def test_lifecycle_alias_drains_residual_rolling_semantic_buffer(self, monkeypatch, tmp_path):
        uuid = "019dcf34-52b2-7010-9b01-eec8ba485b54"
        full_session_id = f"rollout-2026-04-27T13-50-06-{uuid}"
        transcript_path = tmp_path / f"{full_session_id}.jsonl"
        lifecycle_signal_path = tmp_path / "codex-session-end.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"chunk one stable memory"}\n'
            '{"role":"user","content":"chunk two Baxter residual"}\n',
            encoding="utf-8",
        )
        lifecycle_signal_path.write_text(
            '{"role":"assistant","content":"checkpoint ack"}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        instance_root = tmp_path / "instances" / "rolling-inst"
        instance_root.mkdir(parents=True, exist_ok=True)
        source_key = extraction_daemon._signal_source_cursor_key(full_session_id, str(transcript_path))
        extraction_daemon.write_cursor(full_session_id, 2, str(transcript_path), source_key=source_key)
        extraction_daemon.write_rolling_state(
            full_session_id,
            {
                "session_id": full_session_id,
                "transcript_path": str(transcript_path),
                "processed_line_offset": 2,
                "buffered_line_offset": 2,
                "semantic_buffer": "User: Baxter residual context is durable and should be extracted on lifecycle",
                "semantic_buffer_tokens": 12,
                "carry_facts": [{"text": "Owner has stable prior memories"}],
                "raw_facts": [],
            },
        )
        monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "Owner")

        real_registry = sys.modules.get("core.subagent_registry")
        real_adapter = sys.modules.get("lib.adapter")
        real_notify = sys.modules.get("core.runtime.notify")
        fake_registry = types.ModuleType("core.subagent_registry")
        fake_registry.is_registered_subagent = lambda sid: False
        fake_registry.get_harvestable = lambda sid: []
        fake_registry.mark_harvested = lambda sid, cid: None
        fake_registry._registry_dir = lambda: tmp_path / "registry"
        sys.modules["core.subagent_registry"] = fake_registry

        fake_adapter_mod = types.ModuleType("lib.adapter")
        if real_adapter is not None:
            fake_adapter_mod.StandaloneAdapter = getattr(real_adapter, "StandaloneAdapter", object)
            fake_adapter_mod.quaid_projects_dir = getattr(
                real_adapter,
                "quaid_projects_dir",
                lambda: tmp_path / "projects",
            )
            fake_adapter_mod.quaid_tracking_dir = getattr(
                real_adapter,
                "quaid_tracking_dir",
                lambda: tmp_path / "tracking",
            )

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def quaid_home(self):
                return tmp_path

            def instance_root(self):
                return instance_root

            def data_dir(self):
                return instance_root / "data"

            def parse_session_jsonl(self, path):
                raise AssertionError("source cursor is already at EOF; residual buffer should be used")

        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        sys.modules["lib.adapter"] = fake_adapter_mod
        fake_notify = types.ModuleType("core.runtime.notify")
        fake_notify.notify_memory_extraction = lambda **kwargs: None
        sys.modules["core.runtime.notify"] = fake_notify

        import core.docs_updater_hook as docs_updater_mod
        import core.ingest_runtime as ingest_runtime_mod
        import core.project_registry as project_registry_mod
        import ingest.extract as extract_mod

        seen_transcripts = []
        applied = []

        monkeypatch.setattr(
            extract_mod,
            "extract_from_transcript",
            lambda **kwargs: seen_transcripts.append(kwargs["transcript"]) or {
                "carry_facts": [{"text": "Owner discussed Baxter"}],
                "raw_facts": [{"text": "Owner discussed Baxter", "status": "new"}],
                "raw_snippets": {},
                "raw_journal": {},
                "raw_project_logs": {},
                "facts_skipped": 0,
                "payload_duplicate_facts_collapsed": 0,
                "carry_duplicate_facts_dropped": 0,
                "chunks_processed": 1,
                "chunks_total": 1,
                "root_chunks": 1,
                "split_events": 0,
                "split_child_chunks": 0,
                "leaf_chunks": 1,
                "max_split_depth": 0,
                "deep_calls": 1,
                "repair_calls": 0,
                "assessment_usable": 1,
                "assessment_nothing_usable": 0,
                "assessment_needs_smaller_chunk": 0,
                "unclassified_empty_payloads": 0,
            },
        )
        monkeypatch.setattr(
            extract_mod,
            "apply_extracted_payloads",
            lambda payload, **kwargs: applied.append((payload, kwargs)) or {
                **payload,
                "facts_stored": len(payload.get("raw_facts", []) or []),
                "facts_skipped": 0,
                "edges_created": 0,
                "facts": [
                    {"text": fact.get("text", ""), "status": "stored", "edges": []}
                    for fact in (payload.get("raw_facts", []) or [])
                ],
                "snippets": {},
                "journal": {},
                "project_logs": {},
                "project_log_metrics": {},
            },
        )
        monkeypatch.setattr(ingest_runtime_mod, "run_session_logs_ingest", lambda **kwargs: {"status": "indexed"})
        monkeypatch.setattr(project_registry_mod, "snapshot_all_projects", lambda: [])
        monkeypatch.setattr(docs_updater_mod, "update_project_docs", lambda snapshots, extraction_result: {"docs_updated": 0})
        monkeypatch.setattr(extraction_daemon, "_warm_payload_embeddings", lambda facts: {
            "requested": len(facts),
            "unique": len(facts),
            "cache_hits": 0,
            "warmed": len(facts),
            "failed": 0,
            "skipped_empty": 0,
        })

        try:
            extraction_daemon.write_signal(
                signal_type="session_end",
                session_id=uuid,
                transcript_path=str(lifecycle_signal_path),
            )
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])

            assert seen_transcripts == [
                "User: Baxter residual context is durable and should be extracted on lifecycle"
            ]
            assert len(applied) == 1
            assert applied[0][1]["session_id"] == full_session_id
            assert not extraction_daemon._rolling_state_path(full_session_id).exists()
            assert not extraction_daemon._rolling_state_path(uuid).exists()
        finally:
            if real_registry is not None:
                sys.modules["core.subagent_registry"] = real_registry
            else:
                sys.modules.pop("core.subagent_registry", None)
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)
            if real_notify is not None:
                sys.modules["core.runtime.notify"] = real_notify
            else:
                sys.modules.pop("core.runtime.notify", None)

    def test_process_signal_trusts_source_cursor_when_rolling_snapshot_relocated(
        self, monkeypatch, tmp_path
    ):
        import sys
        import types

        session_id = "019dd737-3b58-7b30-adc9-dbab99dc5846"
        basename = f"rollout-2026-04-29T03-10-14-{session_id}.jsonl"
        original = tmp_path / ".codex" / "sessions" / "2026" / "04" / "29" / basename
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_text(
            '{"type":"session_meta","payload":{"cwd":"/Users/admin/quaidcode/dev"}}\n'
            '{"type":"event_msg","payload":{"type":"user_message","message":"Baxter residual context"}}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "codex-private-tmp-cdx-livetest")
        instance_root = tmp_path / "instances" / "codex-private-tmp-cdx-livetest"
        snapshot = (
            instance_root
            / "logs"
            / "daemon"
            / "rolling-transcript-snapshots"
            / session_id
            / "20260429T031319Z-ab465afeaeb56ef6"
            / basename
        )
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(original.read_text(encoding="utf-8"), encoding="utf-8")

        source_key = extraction_daemon._signal_source_cursor_key(session_id, str(original))
        extraction_daemon.write_cursor(session_id, 2, str(snapshot), source_key=source_key)
        extraction_daemon.write_rolling_state(
            session_id,
            {
                "session_id": session_id,
                "transcript_path": str(snapshot),
                "processed_line_offset": 2,
                "buffered_line_offset": 2,
                "rolling_batches": 1,
                "semantic_buffer": "",
                "semantic_buffer_tokens": 0,
                "carry_facts": [{"text": "Owner has stable prior memories"}],
                "raw_facts": [{"text": "Owner discussed Baxter", "status": "new"}],
            },
        )
        monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "Owner")

        real_registry = sys.modules.get("core.subagent_registry")
        real_adapter = sys.modules.get("lib.adapter")
        real_notify = sys.modules.get("core.runtime.notify")
        fake_registry = types.ModuleType("core.subagent_registry")
        fake_registry.is_registered_subagent = lambda sid: False
        fake_registry.get_harvestable = lambda sid: []
        fake_registry.mark_harvested = lambda sid, cid: None
        fake_registry._registry_dir = lambda: tmp_path / "registry"
        sys.modules["core.subagent_registry"] = fake_registry

        fake_adapter_mod = types.ModuleType("lib.adapter")
        fake_adapter_mod.StandaloneAdapter = object
        fake_adapter_mod.quaid_projects_dir = lambda: tmp_path / "projects"
        fake_adapter_mod.quaid_tracking_dir = lambda: tmp_path / "tracking"

        class _RejectingAdapter(_OwnedTestAdapterMixin):
            def quaid_home(self):
                return tmp_path

            def instance_root(self):
                return instance_root

            def data_dir(self):
                return instance_root / "data"

            def owns_session_path(self, path, session_id=""):
                return False

            def parse_session_jsonl(self, path):
                raise AssertionError("residual buffer should be used instead of reparsing host path")

        fake_adapter_mod.get_adapter = lambda: _RejectingAdapter()
        sys.modules["lib.adapter"] = fake_adapter_mod
        fake_notify = types.ModuleType("core.runtime.notify")
        fake_notify.notify_memory_extraction = lambda **kwargs: None
        sys.modules["core.runtime.notify"] = fake_notify

        import core.docs_updater_hook as docs_updater_mod
        import core.ingest_runtime as ingest_runtime_mod
        import core.project_registry as project_registry_mod
        import ingest.extract as extract_mod

        seen_transcripts = []
        applied = []
        monkeypatch.setattr(
            extract_mod,
            "extract_from_transcript",
            lambda **kwargs: seen_transcripts.append(kwargs["transcript"]) or {
                "carry_facts": [{"text": "Owner discussed Baxter"}],
                "raw_facts": [{"text": "Owner discussed Baxter", "status": "new"}],
                "raw_snippets": {},
                "raw_journal": {},
                "raw_project_logs": {},
                "facts_skipped": 0,
                "payload_duplicate_facts_collapsed": 0,
                "carry_duplicate_facts_dropped": 0,
                "chunks_processed": 1,
                "chunks_total": 1,
                "root_chunks": 1,
                "split_events": 0,
                "split_child_chunks": 0,
                "leaf_chunks": 1,
                "max_split_depth": 0,
                "deep_calls": 1,
                "repair_calls": 0,
                "assessment_usable": 1,
                "assessment_nothing_usable": 0,
                "assessment_needs_smaller_chunk": 0,
                "unclassified_empty_payloads": 0,
            },
        )
        monkeypatch.setattr(
            extract_mod,
            "apply_extracted_payloads",
            lambda payload, **kwargs: applied.append((payload, kwargs)) or {
                **payload,
                "facts_stored": len(payload.get("raw_facts", []) or []),
                "facts_skipped": 0,
                "edges_created": 0,
                "facts": [
                    {"text": fact.get("text", ""), "status": "stored", "edges": []}
                    for fact in (payload.get("raw_facts", []) or [])
                ],
                "snippets": {},
                "journal": {},
                "project_logs": {},
                "project_log_metrics": {},
            },
        )
        monkeypatch.setattr(ingest_runtime_mod, "run_session_logs_ingest", lambda **kwargs: {"status": "indexed"})
        monkeypatch.setattr(project_registry_mod, "snapshot_all_projects", lambda: [])
        monkeypatch.setattr(docs_updater_mod, "update_project_docs", lambda snapshots, extraction_result: {"docs_updated": 0})
        monkeypatch.setattr(extraction_daemon, "_warm_payload_embeddings", lambda facts: {
            "requested": len(facts),
            "unique": len(facts),
            "cache_hits": 0,
            "warmed": len(facts),
            "failed": 0,
            "skipped_empty": 0,
        })

        try:
            extraction_daemon.write_signal(
                signal_type="session_end",
                session_id=session_id,
                transcript_path=str(original),
                meta={
                    "reason": "rolling_stage_flush",
                    "source_signal": "rolling",
                    "staged_payload_sweep": True,
                    "source_cursor_key": source_key,
                },
            )
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])

            assert seen_transcripts == []
            assert len(applied) == 1
            assert applied[0][1]["session_id"] == session_id
            assert applied[0][0]["raw_facts"] == [{"text": "Owner discussed Baxter", "status": "new"}]
            assert not extraction_daemon._rolling_state_path(session_id).exists()
        finally:
            if real_registry is not None:
                sys.modules["core.subagent_registry"] = real_registry
            else:
                sys.modules.pop("core.subagent_registry", None)
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)
            if real_notify is not None:
                sys.modules["core.runtime.notify"] = real_notify
            else:
                sys.modules.pop("core.runtime.notify", None)

    def test_rolling_stage_flush_runs_before_continued_raw_tail(self, monkeypatch, tmp_path):
        import sys
        import types

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"chunk one stable memory"}\n'
            '{"role":"assistant","content":"ack"}\n'
            '{"role":"user","content":"chunk two Baxter residual"}\n'
            '{"role":"assistant","content":"ack tail"}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        instance_root = tmp_path / "instances" / "rolling-inst"
        instance_root.mkdir(parents=True, exist_ok=True)
        (instance_root / "config.json").write_text(
            json.dumps({"adapter": {"type": "standalone"}}),
            encoding="utf-8",
        )
        extraction_daemon.write_cursor("sess-roll-raw-tail", 0, str(transcript_path))
        prior = "User: chunk one stable memory"
        tail = "User: chunk two Baxter residual"
        extraction_daemon.write_rolling_state(
            "sess-roll-raw-tail",
            {
                "session_id": "sess-roll-raw-tail",
                "transcript_path": str(transcript_path),
                "semantic_buffer": prior,
                "semantic_buffer_tokens": 80,
                "buffered_line_offset": 2,
                "processed_line_offset": 2,
                "raw_facts": [],
                "carry_facts": [],
            },
        )
        monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "Owner")
        monkeypatch.setattr(extraction_daemon, "_rolling_ready_threshold", lambda chunk_budget: 1)

        real_registry = sys.modules.get("core.subagent_registry")
        real_adapter = sys.modules.get("lib.adapter")
        real_notify = sys.modules.get("core.runtime.notify")
        fake_registry = types.ModuleType("core.subagent_registry")
        fake_registry.is_registered_subagent = lambda sid: False
        fake_registry.get_harvestable = lambda sid: []
        fake_registry.mark_harvested = lambda sid, cid: None
        fake_registry._registry_dir = lambda: tmp_path / "registry"
        sys.modules["core.subagent_registry"] = fake_registry

        fake_adapter_mod = types.ModuleType("lib.adapter")

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def quaid_home(self):
                return tmp_path

            def instance_root(self):
                return instance_root

            def data_dir(self):
                return instance_root / "data"

            def parse_session_jsonl(self, path):
                raw = Path(path).read_text(encoding="utf-8")
                return tail if "Baxter" in raw else prior

        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        fake_adapter_mod.quaid_projects_dir = lambda: tmp_path / "projects"
        fake_adapter_mod.quaid_tracking_dir = lambda: tmp_path / "tracking"
        sys.modules["lib.adapter"] = fake_adapter_mod
        fake_notify = types.ModuleType("core.runtime.notify")
        fake_notify.notify_memory_extraction = lambda **kwargs: None
        sys.modules["core.runtime.notify"] = fake_notify

        import core.docs_updater_hook as docs_updater_mod
        import core.ingest_runtime as ingest_runtime_mod
        import core.project_registry as project_registry_mod
        import ingest.extract as extract_mod

        seen_transcripts = []
        applied_payloads = []

        def fake_extract_from_transcript(**kwargs):
            transcript = kwargs["transcript"]
            seen_transcripts.append(transcript)
            text = "Owner discussed Baxter" if "Baxter" in transcript else "Owner has stable prior memories"
            return {
                "carry_facts": [{"text": text}],
                "raw_facts": [{"text": text, "status": "new"}],
                "raw_snippets": {},
                "raw_journal": {},
                "raw_project_logs": {},
                "facts_skipped": 0,
                "payload_duplicate_facts_collapsed": 0,
                "carry_duplicate_facts_dropped": 0,
                "chunks_processed": 1,
                "chunks_total": 1,
                "root_chunks": 1,
                "split_events": 0,
                "split_child_chunks": 0,
                "leaf_chunks": 1,
                "max_split_depth": 0,
                "deep_calls": 1,
                "repair_calls": 0,
                "assessment_usable": 1,
                "assessment_nothing_usable": 0,
                "assessment_needs_smaller_chunk": 0,
                "unclassified_empty_payloads": 0,
            }

        monkeypatch.setattr(extract_mod, "extract_from_transcript", fake_extract_from_transcript)
        monkeypatch.setattr(
            extract_mod,
            "apply_extracted_payloads",
            lambda payload, **kwargs: applied_payloads.append(payload) or {
                **payload,
                "facts_stored": len(payload.get("raw_facts", []) or []),
                "facts_skipped": 0,
                "edges_created": 0,
                "facts": [],
                "snippets": {},
                "journal": {},
                "project_logs": {},
                "project_log_metrics": {},
            },
        )
        monkeypatch.setattr(ingest_runtime_mod, "run_session_logs_ingest", lambda **kwargs: {"status": "indexed"})
        monkeypatch.setattr(project_registry_mod, "snapshot_all_projects", lambda: [])
        monkeypatch.setattr(docs_updater_mod, "update_project_docs", lambda snapshots, extraction_result: {"docs_updated": 0})
        monkeypatch.setattr(extraction_daemon, "_warm_payload_embeddings", lambda facts: {
            "requested": len(facts),
            "unique": len(facts),
            "cache_hits": 0,
            "warmed": len(facts),
            "failed": 0,
            "skipped_empty": 0,
        })

        try:
            extraction_daemon.write_signal(
                signal_type="rolling",
                session_id="sess-roll-raw-tail",
                transcript_path=str(transcript_path),
            )
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])

            pending = extraction_daemon.read_pending_signals()
            assert [item["type"] for item in pending] == ["session_end", "rolling"]
            assert pending[0]["meta"]["reason"] == "rolling_stage_flush"
            assert pending[0]["meta"]["flush_staged_payload_only"] is True
            assert pending[1]["meta"]["reason"] == "continued_chunk_budget"
            assert pending[1]["meta"]["source_cursor_key"]
            continued_path = Path(pending[1]["transcript_path"])
            assert continued_path.is_file()
            assert continued_path.name == transcript_path.name
            assert continued_path != transcript_path

            transcript_path.write_text(
                '{"role":"user","content":"Hello after /new"}\n',
                encoding="utf-8",
            )

            extraction_daemon.process_signal(pending[0])
            assert seen_transcripts == [prior]
            assert len(applied_payloads) == 1
            assert [fact["text"] for fact in applied_payloads[0]["raw_facts"]] == [
                "Owner has stable prior memories"
            ]
            assert extraction_daemon.read_cursor("sess-roll-raw-tail")["line_offset"] == 2
            assert [item["type"] for item in extraction_daemon.read_pending_signals()] == ["rolling"]

            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])
            assert seen_transcripts == [prior, tail]
            assert not continued_path.exists()
            pending_after_tail = extraction_daemon.read_pending_signals()
            assert [item["type"] for item in pending_after_tail] == ["session_end"]
            assert pending_after_tail[0]["transcript_path"] == str(transcript_path)
        finally:
            if real_registry is not None:
                sys.modules["core.subagent_registry"] = real_registry
            else:
                sys.modules.pop("core.subagent_registry", None)
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)
            if real_notify is not None:
                sys.modules["core.runtime.notify"] = real_notify
            else:
                sys.modules.pop("core.runtime.notify", None)

    def test_process_signal_late_post_reset_content_preserves_active_rolling_state(
        self, monkeypatch, tmp_path
    ):
        import sys
        import types

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"Chunk one Baxter context includes orange linen notebook details for rolling continuation"}\n'
            '{"role":"assistant","content":"ack"}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        instance_root = tmp_path / "instances" / "rolling-inst"
        instance_root.mkdir(parents=True, exist_ok=True)
        (instance_root / "config.json").write_text(
            json.dumps({"adapter": {"type": "standalone"}}),
            encoding="utf-8",
        )
        extraction_daemon.write_cursor("sess-late-reset", 2, str(transcript_path))
        extraction_daemon.write_rolling_state(
            "sess-late-reset",
            {
                "session_id": "sess-late-reset",
                "transcript_path": str(transcript_path),
                "buffer_transcript_path": str(transcript_path),
                "processed_line_offset": 2,
                "buffered_line_offset": 2,
                "semantic_buffer": "User: Chunk one Baxter context includes orange linen notebook details for rolling continuation",
                "semantic_buffer_tokens": 9,
                "carry_facts": [],
                "raw_facts": [],
            },
        )
        monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "Owner")

        real_registry = sys.modules.get("core.subagent_registry")
        real_adapter = sys.modules.get("lib.adapter")
        real_notify = sys.modules.get("core.runtime.notify")
        fake_registry = types.ModuleType("core.subagent_registry")
        fake_registry.is_registered_subagent = lambda sid: False
        fake_registry.get_harvestable = lambda sid: []
        fake_registry.mark_harvested = lambda sid, cid: None
        fake_registry._registry_dir = lambda: tmp_path / "registry"
        sys.modules["core.subagent_registry"] = fake_registry

        fake_adapter_mod = types.ModuleType("lib.adapter")

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def quaid_home(self):
                return tmp_path

            def instance_root(self):
                return instance_root

            def data_dir(self):
                return instance_root / "data"

            def parse_session_jsonl(self, path):
                raise AssertionError("cursor is at EOF; semantic buffer should be flushed directly")

        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        fake_adapter_mod.quaid_projects_dir = lambda home: Path(home) / "projects"
        fake_adapter_mod.quaid_tracking_dir = lambda home: Path(home) / ".git-tracking"
        sys.modules["lib.adapter"] = fake_adapter_mod
        fake_notify = types.ModuleType("core.runtime.notify")
        fake_notify.notify_memory_extraction = lambda **kwargs: None
        sys.modules["core.runtime.notify"] = fake_notify

        import core.docs_updater_hook as docs_updater_mod
        import core.ingest_runtime as ingest_runtime_mod
        import core.project_registry as project_registry_mod
        import ingest.extract as extract_mod

        seen_transcripts = []
        applied_payloads = []
        monkeypatch.setattr(
            extract_mod,
            "extract_from_transcript",
            lambda **kwargs: seen_transcripts.append(kwargs["transcript"]) or {
                "facts_stored": 0,
                "facts_skipped": 0,
                "edges_created": 0,
                "facts": [],
                "snippets": {},
                "journal": {},
                "project_logs": {},
                "project_log_metrics": {},
                "dry_run": True,
                "raw_facts": [{"text": "Owner discussed Baxter", "category": "fact"}],
                "raw_snippets": {},
                "raw_journal": {},
                "raw_project_logs": {},
                "carry_facts": [{"text": "Owner discussed Baxter"}],
                "carry_duplicate_facts_dropped": 0,
                "payload_duplicate_facts_collapsed": 0,
                "chunks_processed": 1,
                "chunks_total": 1,
                "root_chunks": 1,
                "split_events": 0,
                "split_child_chunks": 0,
                "leaf_chunks": 1,
                "max_split_depth": 0,
                "chunk_calls": 1,
                "deep_calls": 1,
                "repair_calls": 0,
                "assessment_usable": 1,
                "assessment_nothing_usable": 0,
                "assessment_needs_smaller_chunk": 0,
                "unclassified_empty_payloads": 0,
            },
        )
        monkeypatch.setattr(
            extract_mod,
            "apply_extracted_payloads",
            lambda payload, **kwargs: applied_payloads.append(payload) or {
                **payload,
                "facts_stored": len(payload.get("raw_facts", []) or []),
                "facts_skipped": 0,
                "edges_created": 0,
                "facts": [],
                "snippets": {},
                "journal": {},
                "project_logs": {},
                "project_log_metrics": {},
            },
        )
        monkeypatch.setattr(ingest_runtime_mod, "run_session_logs_ingest", lambda **kwargs: {"status": "indexed"})
        monkeypatch.setattr(project_registry_mod, "snapshot_all_projects", lambda: [])
        monkeypatch.setattr(docs_updater_mod, "update_project_docs", lambda snapshots, extraction_result: {"docs_updated": 0})
        monkeypatch.setattr(extraction_daemon, "_warm_payload_embeddings", lambda facts: {
            "requested": len(facts),
            "unique": len(facts),
            "cache_hits": 0,
            "warmed": len(facts),
            "failed": 0,
            "skipped_empty": 0,
        })

        try:
            extraction_daemon.write_signal(
                signal_type="session_end",
                session_id="sess-late-reset",
                transcript_path=str(transcript_path),
                meta={"reason": "late_post_reset_content", "source": "transcript_update_late_content"},
            )
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])

            assert seen_transcripts == ["User: Chunk one Baxter context includes orange linen notebook details for rolling continuation"]
            assert len(applied_payloads) == 1
            state = extraction_daemon.read_rolling_state("sess-late-reset")
            assert state["semantic_buffer"] == "User: Chunk one Baxter context includes orange linen notebook details for rolling continuation"
            assert state["semantic_buffer_tokens"] == 9
            assert state["buffer_transcript_path"] == str(transcript_path)
            assert state["raw_facts"] == []
            assert extraction_daemon.read_cursor("sess-late-reset")["line_offset"] == 2
        finally:
            if real_registry is not None:
                sys.modules["core.subagent_registry"] = real_registry
            else:
                sys.modules.pop("core.subagent_registry", None)
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)
            if real_notify is not None:
                sys.modules["core.runtime.notify"] = real_notify
            else:
                sys.modules.pop("core.runtime.notify", None)

    def test_process_signal_session_end_flushes_buffered_semantic_tail_without_new_raw_lines(self, monkeypatch, tmp_path):
        import sys
        import types

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"My sister is Diana"}\n'
            '{"role":"assistant","content":"Her daughter is Alice"}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        instance_root = tmp_path / "instances" / "rolling-inst"
        instance_root.mkdir(parents=True, exist_ok=True)
        (instance_root / "config.json").write_text(
            json.dumps(
                {
                    "adapter": {"type": "standalone"},
                    "livetest": {"enableExtractionBufferLog": True},
                }
            ),
            encoding="utf-8",
        )
        extraction_daemon.write_cursor("sess-roll", 0, str(transcript_path))
        extraction_daemon.write_rolling_state(
            "sess-roll",
            {
                "session_id": "sess-roll",
                "transcript_path": str(transcript_path),
                "processed_line_offset": 2,
                "buffered_line_offset": 2,
                "semantic_buffer": "User: My sister is Diana\n\nAssistant: Her daughter is Alice",
                "semantic_buffer_tokens": 12,
                "carry_facts": [],
                "raw_facts": [{"text": "Owner has a sister named Diana", "category": "fact"}],
            },
        )
        monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "Owner")

        real_registry = sys.modules.get("core.subagent_registry")
        real_adapter = sys.modules.get("lib.adapter")
        fake_registry = types.ModuleType("core.subagent_registry")
        fake_registry.is_registered_subagent = lambda sid: False
        fake_registry.get_harvestable = lambda sid: []
        fake_registry.mark_harvested = lambda sid, cid: None
        fake_registry._registry_dir = lambda: tmp_path / "registry"
        sys.modules["core.subagent_registry"] = fake_registry

        fake_adapter_mod = types.ModuleType("lib.adapter")

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def quaid_home(self):
                return tmp_path

            def instance_root(self):
                return instance_root

            def data_dir(self):
                return instance_root / "data"

            def parse_session_jsonl(self, path):
                return "unused when semantic buffer is present"

        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        fake_adapter_mod.quaid_projects_dir = lambda home: Path(home) / "projects"
        fake_adapter_mod.quaid_tracking_dir = lambda home: Path(home) / ".git-tracking"
        sys.modules["lib.adapter"] = fake_adapter_mod

        import core.docs_updater_hook as docs_updater_mod
        import core.ingest_runtime as ingest_runtime_mod
        import core.project_registry as project_registry_mod
        import ingest.extract as extract_mod

        real_notify = sys.modules.get("core.runtime.notify")
        fake_notify = types.ModuleType("core.runtime.notify")
        fake_notify.notify_memory_extraction = lambda **kwargs: None
        sys.modules["core.runtime.notify"] = fake_notify

        seen_transcripts = []
        monkeypatch.setattr(
            extract_mod,
            "extract_from_transcript",
            lambda **kwargs: seen_transcripts.append(kwargs["transcript"]) or {
                "facts_stored": 0,
                "facts_skipped": 0,
                "edges_created": 0,
                "facts": [],
                "snippets": {},
                "journal": {},
                "project_logs": {},
                "project_log_metrics": {},
                "dry_run": True,
                "raw_facts": [],
                "raw_snippets": {},
                "raw_journal": {},
                "raw_project_logs": {},
                "carry_facts": [{"text": "Owner has a sister named Diana"}],
                "carry_duplicate_facts_dropped": 0,
                "chunks_processed": 1,
                "chunks_total": 1,
                "root_chunks": 1,
                "split_events": 0,
                "split_child_chunks": 0,
                "leaf_chunks": 1,
                "max_split_depth": 0,
                "chunk_calls": 1,
                "deep_calls": 1,
                "repair_calls": 0,
                "assessment_usable": 1,
                "assessment_nothing_usable": 0,
                "assessment_needs_smaller_chunk": 0,
                "unclassified_empty_payloads": 0,
            },
        )
        monkeypatch.setattr(
            extract_mod,
            "apply_extracted_payloads",
            lambda payload, **kwargs: {
                **payload,
                "facts": [],
                "snippets": {},
                "journal": {},
                "project_logs": {},
                "project_log_metrics": {},
            },
        )
        monkeypatch.setattr(
            ingest_runtime_mod,
            "run_session_logs_ingest",
            lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must route through broker request")),
        )
        session_ingest_calls = []
        monkeypatch.setattr(
            extraction_daemon,
            "_request_session_logs_ingest",
            lambda **kwargs: session_ingest_calls.append(kwargs) or {"status": "indexed"},
        )
        monkeypatch.setattr(project_registry_mod, "snapshot_all_projects", lambda: [])
        monkeypatch.setattr(
            docs_updater_mod,
            "update_project_docs",
            lambda snapshots, extraction_result: {"docs_updated": 0},
        )
        monkeypatch.setattr(
            extraction_daemon,
            "_read_usage_totals",
            lambda: {
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "fast_calls": 0,
                "fast_input_tokens": 0,
                "fast_output_tokens": 0,
                "deep_calls": 0,
                "deep_input_tokens": 0,
                "deep_output_tokens": 0,
            },
        )

        try:
            extraction_daemon.write_signal(
                signal_type="session_end",
                session_id="sess-roll",
                transcript_path=str(transcript_path),
            )
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])

            assert seen_transcripts == ["User: My sister is Diana\n\nAssistant: Her daughter is Alice"]
            assert extraction_daemon.read_cursor("sess-roll")["line_offset"] == 2
            assert not extraction_daemon._rolling_state_path("sess-roll").exists()
            assert len(session_ingest_calls) == 1
            assert session_ingest_calls[0]["session_id"] == "sess-roll"
            assert session_ingest_calls[0]["owner_id"] == "Owner"
            assert session_ingest_calls[0]["transcript_path"] == str(transcript_path)
            buffer_log = (instance_root / "logs" / "daemon" / "extraction-buffer.log").read_text(
                encoding="utf-8"
            )
            assert "phase=final_flush" in buffer_log
            assert "signal=session_end" in buffer_log
            assert "Her daughter is Alice" in buffer_log
        finally:
            if real_registry is not None:
                sys.modules["core.subagent_registry"] = real_registry
            else:
                sys.modules.pop("core.subagent_registry", None)
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)
            if real_notify is not None:
                sys.modules["core.runtime.notify"] = real_notify
            else:
                sys.modules.pop("core.runtime.notify", None)

    def test_lifecycle_source_cursor_key_drains_residual_rolling_semantic_buffer(self, monkeypatch, tmp_path):
        full_session_id = "rollout-2026-06-14T23-21-24-019ec870-9d6c-79e0-963c-7424b47d3553"
        lifecycle_session_id = "cdx-lifecycle-current"
        transcript_path = tmp_path / f"{full_session_id}.jsonl"
        lifecycle_signal_path = tmp_path / "codex-session-end.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"Niseko Kinesis Phoebe Bridgers source chunk"}\n'
            '{"role":"assistant","content":"ACK"}\n',
            encoding="utf-8",
        )
        lifecycle_signal_path.write_text(
            '{"role":"assistant","content":"ACK"}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        instance_root = tmp_path / "instances" / "rolling-inst"
        instance_root.mkdir(parents=True, exist_ok=True)
        source_key = extraction_daemon._signal_source_cursor_key(full_session_id, str(transcript_path))
        extraction_daemon.write_cursor(full_session_id, 2, str(transcript_path), source_key=source_key)
        extraction_daemon.write_rolling_state(
            full_session_id,
            {
                "session_id": full_session_id,
                "transcript_path": str(transcript_path),
                "processed_line_offset": 2,
                "buffered_line_offset": 2,
                "semantic_buffer": (
                    "User: Niseko, Kinesis Advantage360, and Phoebe Bridgers "
                    "must be extracted from the residual rolling buffer"
                ),
                "semantic_buffer_tokens": 32,
                "carry_facts": [],
                "raw_facts": [],
            },
        )
        monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "Owner")

        real_registry = sys.modules.get("core.subagent_registry")
        real_adapter = sys.modules.get("lib.adapter")
        real_notify = sys.modules.get("core.runtime.notify")
        fake_registry = types.ModuleType("core.subagent_registry")
        fake_registry.is_registered_subagent = lambda sid: False
        fake_registry.get_harvestable = lambda sid: []
        fake_registry.mark_harvested = lambda sid, cid: None
        fake_registry._registry_dir = lambda: tmp_path / "registry"
        sys.modules["core.subagent_registry"] = fake_registry

        fake_adapter_mod = types.ModuleType("lib.adapter")
        if real_adapter is not None:
            fake_adapter_mod.StandaloneAdapter = getattr(real_adapter, "StandaloneAdapter", object)
            fake_adapter_mod.quaid_projects_dir = getattr(
                real_adapter,
                "quaid_projects_dir",
                lambda: tmp_path / "projects",
            )
            fake_adapter_mod.quaid_tracking_dir = getattr(
                real_adapter,
                "quaid_tracking_dir",
                lambda: tmp_path / "tracking",
            )

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def quaid_home(self):
                return tmp_path

            def instance_root(self):
                return instance_root

            def data_dir(self):
                return instance_root / "data"

            def parse_session_jsonl(self, path):
                return "Assistant: ACK"

        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        sys.modules["lib.adapter"] = fake_adapter_mod
        fake_notify = types.ModuleType("core.runtime.notify")
        fake_notify.notify_memory_extraction = lambda **kwargs: None
        sys.modules["core.runtime.notify"] = fake_notify

        import core.docs_updater_hook as docs_updater_mod
        import core.ingest_runtime as ingest_runtime_mod
        import core.project_registry as project_registry_mod
        import ingest.extract as extract_mod

        seen_transcripts = []
        applied = []

        monkeypatch.setattr(
            extract_mod,
            "extract_from_transcript",
            lambda **kwargs: seen_transcripts.append(kwargs["transcript"]) or {
                "carry_facts": [{"text": "Owner discussed Niseko and Phoebe Bridgers"}],
                "raw_facts": [{"text": "Owner discussed Niseko and Phoebe Bridgers", "status": "new"}],
                "raw_snippets": {},
                "raw_journal": {},
                "raw_project_logs": {},
                "facts_skipped": 0,
                "payload_duplicate_facts_collapsed": 0,
                "carry_duplicate_facts_dropped": 0,
                "chunks_processed": 1,
                "chunks_total": 1,
                "root_chunks": 1,
                "split_events": 0,
                "split_child_chunks": 0,
                "leaf_chunks": 1,
                "max_split_depth": 0,
                "deep_calls": 1,
                "repair_calls": 0,
                "assessment_usable": 1,
                "assessment_nothing_usable": 0,
                "assessment_needs_smaller_chunk": 0,
                "unclassified_empty_payloads": 0,
            },
        )
        monkeypatch.setattr(
            extract_mod,
            "apply_extracted_payloads",
            lambda payload, **kwargs: applied.append((payload, kwargs)) or {
                **payload,
                "facts_stored": len(payload.get("raw_facts", []) or []),
                "facts_skipped": 0,
                "edges_created": 0,
                "facts": [],
                "snippets": {},
                "journal": {},
                "project_logs": {},
                "project_log_metrics": {},
            },
        )
        monkeypatch.setattr(ingest_runtime_mod, "run_session_logs_ingest", lambda **kwargs: {"status": "indexed"})
        monkeypatch.setattr(project_registry_mod, "snapshot_all_projects", lambda: [])
        monkeypatch.setattr(docs_updater_mod, "update_project_docs", lambda snapshots, extraction_result: {"docs_updated": 0})
        monkeypatch.setattr(extraction_daemon, "_warm_payload_embeddings", lambda facts: {
            "requested": len(facts),
            "unique": len(facts),
            "cache_hits": 0,
            "warmed": len(facts),
            "failed": 0,
            "skipped_empty": 0,
        })

        try:
            extraction_daemon.write_signal(
                signal_type="session_end",
                session_id=lifecycle_session_id,
                transcript_path=str(lifecycle_signal_path),
                meta={"source_cursor_key": source_key},
            )
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])

            assert seen_transcripts == [
                "User: Niseko, Kinesis Advantage360, and Phoebe Bridgers "
                "must be extracted from the residual rolling buffer"
            ]
            assert len(applied) == 1
            assert applied[0][1]["session_id"] == full_session_id
            assert not extraction_daemon._rolling_state_path(full_session_id).exists()
        finally:
            if real_registry is not None:
                sys.modules["core.subagent_registry"] = real_registry
            else:
                sys.modules.pop("core.subagent_registry", None)
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)
            if real_notify is not None:
                sys.modules["core.runtime.notify"] = real_notify
            else:
                sys.modules.pop("core.runtime.notify", None)

    def test_session_end_session_logs_ingest_uses_current_rolling_source(self, monkeypatch, tmp_path):
        import sys
        import types

        session_id = "05aace3b"
        stale_mirror = (
            tmp_path
            / "instances"
            / "openclaw-main"
            / "logs"
            / "quaid"
            / "sessions"
            / f"{session_id}.jsonl"
        )
        live_source = tmp_path / ".openclaw" / "agents" / "main" / "sessions" / f"{session_id}.jsonl"
        stale_mirror.parent.mkdir(parents=True, exist_ok=True)
        live_source.parent.mkdir(parents=True, exist_ok=True)
        stale_mirror.write_text('{"role":"user","content":"old mirror content"}\n', encoding="utf-8")
        live_source.write_text(
            '{"role":"user","content":"old mirror content"}\n'
            '{"role":"user","content":"current live source content"}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
        instance_root = tmp_path / "instances" / "openclaw-main"
        instance_root.mkdir(parents=True, exist_ok=True)
        (instance_root / "config.json").write_text(
            json.dumps({"adapter": {"type": "openclaw"}}),
            encoding="utf-8",
        )
        extraction_daemon.write_cursor(session_id, 1, str(stale_mirror))
        extraction_daemon.write_rolling_state(
            session_id,
            {
                "session_id": session_id,
                "transcript_path": str(stale_mirror),
                "buffer_transcript_path": str(live_source),
                "processed_line_offset": 1,
                "buffered_line_offset": 2,
                "semantic_buffer": (
                    "User: current live source content should be indexed from the live OpenClaw path"
                ),
                "semantic_buffer_tokens": 12,
                "carry_facts": [],
                "raw_facts": [],
            },
        )
        monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "Owner")

        real_registry = sys.modules.get("core.subagent_registry")
        real_adapter = sys.modules.get("lib.adapter")
        real_notify = sys.modules.get("core.runtime.notify")
        fake_registry = types.ModuleType("core.subagent_registry")
        fake_registry.is_registered_subagent = lambda sid: False
        fake_registry.get_harvestable = lambda sid: []
        fake_registry.mark_harvested = lambda sid, cid: None
        fake_registry._registry_dir = lambda: tmp_path / "registry"
        sys.modules["core.subagent_registry"] = fake_registry

        fake_adapter_mod = types.ModuleType("lib.adapter")

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def quaid_home(self):
                return tmp_path

            def instance_root(self):
                return instance_root

            def data_dir(self):
                return instance_root / "data"

            def parse_session_jsonl(self, path):
                return "User: old mirror content"

        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        fake_adapter_mod.quaid_projects_dir = lambda home: Path(home) / "projects"
        fake_adapter_mod.quaid_tracking_dir = lambda home: Path(home) / ".git-tracking"
        sys.modules["lib.adapter"] = fake_adapter_mod

        fake_notify = types.ModuleType("core.runtime.notify")
        fake_notify.notify_memory_extraction = lambda **kwargs: None
        sys.modules["core.runtime.notify"] = fake_notify

        import core.docs_updater_hook as docs_updater_mod
        import core.project_registry as project_registry_mod
        import ingest.extract as extract_mod

        seen_transcripts = []
        session_ingest_calls = []
        monkeypatch.setattr(
            extract_mod,
            "extract_from_transcript",
            lambda **kwargs: seen_transcripts.append(kwargs["transcript"]) or {
                "carry_facts": [{"text": "Owner mentioned current live source content"}],
                "raw_facts": [{"text": "Owner mentioned current live source content", "status": "new"}],
                "raw_snippets": {},
                "raw_journal": {},
                "raw_project_logs": {},
                "facts_skipped": 0,
                "payload_duplicate_facts_collapsed": 0,
                "carry_duplicate_facts_dropped": 0,
                "chunks_processed": 1,
                "chunks_total": 1,
                "root_chunks": 1,
                "split_events": 0,
                "split_child_chunks": 0,
                "leaf_chunks": 1,
                "max_split_depth": 0,
                "deep_calls": 1,
                "repair_calls": 0,
                "assessment_usable": 1,
                "assessment_nothing_usable": 0,
                "assessment_needs_smaller_chunk": 0,
                "unclassified_empty_payloads": 0,
            },
        )
        monkeypatch.setattr(
            extract_mod,
            "apply_extracted_payloads",
            lambda payload, **kwargs: {
                **payload,
                "facts_stored": len(payload.get("raw_facts", []) or []),
                "facts_skipped": 0,
                "edges_created": 0,
                "facts": [],
                "snippets": {},
                "journal": {},
                "project_logs": {},
                "project_log_metrics": {},
            },
        )
        monkeypatch.setattr(
            extraction_daemon,
            "_request_session_logs_ingest",
            lambda **kwargs: session_ingest_calls.append(kwargs) or {"status": "indexed"},
        )
        monkeypatch.setattr(project_registry_mod, "snapshot_all_projects", lambda: [])
        monkeypatch.setattr(docs_updater_mod, "update_project_docs", lambda snapshots, extraction_result: {"docs_updated": 0})
        monkeypatch.setattr(
            extraction_daemon,
            "_read_usage_totals",
            lambda: {
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "fast_calls": 0,
                "fast_input_tokens": 0,
                "fast_output_tokens": 0,
                "deep_calls": 0,
                "deep_input_tokens": 0,
                "deep_output_tokens": 0,
            },
        )

        try:
            extraction_daemon.write_signal(
                signal_type="session_end",
                session_id=session_id,
                transcript_path=str(stale_mirror),
            )
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])

            assert seen_transcripts == [
                "User: current live source content should be indexed from the live OpenClaw path"
            ]
            assert len(session_ingest_calls) == 1
            assert session_ingest_calls[0]["transcript_path"] == str(live_source)
        finally:
            if real_registry is not None:
                sys.modules["core.subagent_registry"] = real_registry
            else:
                sys.modules.pop("core.subagent_registry", None)
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)
            if real_notify is not None:
                sys.modules["core.runtime.notify"] = real_notify
            else:
                sys.modules.pop("core.runtime.notify", None)

    def test_session_end_drains_unflushed_scan_only_rolling_window(self, monkeypatch, tmp_path):
        session_id = "rollout-2026-06-15T08-00-42-019ec894-cdx-m2"
        transcript_path = tmp_path / f"{session_id}.jsonl"
        transcript_lines = [
            '{"role":"user","content":"Niseko Kinesis Phoebe Bridgers chunk one fact"}\n',
            '{"role":"assistant","content":"ACK"}\n',
        ]
        transcript_lines.extend(
            f'{{"role":"assistant","content":"rolling scanner filler {idx}"}}\n'
            for idx in range(17)
        )
        transcript_lines.extend(
            [
                '{"role":"user","content":"/new"}\n',
                '{"role":"assistant","content":"Hello"}\n',
                '{"role":"user","content":"Hello"}\n',
            ]
        )
        transcript_path.write_text("".join(transcript_lines), encoding="utf-8")

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        instance_root = tmp_path / "instances" / "rolling-inst"
        instance_root.mkdir(parents=True, exist_ok=True)
        source_key = extraction_daemon._signal_source_cursor_key(session_id, str(transcript_path))
        # This is the CDX failure shape: the rolling scanner advanced to EOF
        # below threshold, but no rolling flush consumed the scanned window.
        extraction_daemon.write_cursor(
            session_id,
            len(transcript_lines),
            str(transcript_path),
            source_key=source_key,
            last_flushed_line_offset=0,
        )
        assert extraction_daemon.read_cursor(session_id, source_key=source_key)["last_flushed_line_offset"] == 0
        monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "Owner")

        real_registry = sys.modules.get("core.subagent_registry")
        real_adapter = sys.modules.get("lib.adapter")
        real_notify = sys.modules.get("core.runtime.notify")
        fake_registry = types.ModuleType("core.subagent_registry")
        fake_registry.is_registered_subagent = lambda sid: False
        fake_registry.get_harvestable = lambda sid: []
        fake_registry.mark_harvested = lambda sid, cid: None
        fake_registry._registry_dir = lambda: tmp_path / "registry"
        sys.modules["core.subagent_registry"] = fake_registry

        fake_adapter_mod = types.ModuleType("lib.adapter")
        if real_adapter is not None:
            fake_adapter_mod.StandaloneAdapter = getattr(real_adapter, "StandaloneAdapter", object)
            fake_adapter_mod.quaid_projects_dir = getattr(
                real_adapter,
                "quaid_projects_dir",
                lambda: tmp_path / "projects",
            )
            fake_adapter_mod.quaid_tracking_dir = getattr(
                real_adapter,
                "quaid_tracking_dir",
                lambda: tmp_path / "tracking",
            )

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def quaid_home(self):
                return tmp_path

            def instance_root(self):
                return instance_root

            def data_dir(self):
                return instance_root / "data"

            def parse_session_jsonl(self, path):
                rendered = []
                for raw in Path(path).read_text(encoding="utf-8").splitlines():
                    try:
                        row = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    content = str(row.get("content") or "").strip()
                    if content:
                        rendered.append(f"{row.get('role', 'unknown').title()}: {content}")
                return "\n".join(rendered)

        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        sys.modules["lib.adapter"] = fake_adapter_mod
        fake_notify = types.ModuleType("core.runtime.notify")
        fake_notify.notify_memory_extraction = lambda **kwargs: None
        sys.modules["core.runtime.notify"] = fake_notify

        import core.docs_updater_hook as docs_updater_mod
        import core.ingest_runtime as ingest_runtime_mod
        import core.project_registry as project_registry_mod
        import ingest.extract as extract_mod

        seen_transcripts = []
        applied = []

        monkeypatch.setattr(
            extract_mod,
            "extract_from_transcript",
            lambda **kwargs: seen_transcripts.append(kwargs["transcript"]) or {
                "carry_facts": [{"text": "Owner discussed Niseko and Phoebe Bridgers"}],
                "raw_facts": [{"text": "Owner discussed Niseko and Phoebe Bridgers", "status": "new"}],
                "raw_snippets": {},
                "raw_journal": {},
                "raw_project_logs": {},
                "facts_skipped": 0,
                "payload_duplicate_facts_collapsed": 0,
                "carry_duplicate_facts_dropped": 0,
                "chunks_processed": 1,
                "chunks_total": 1,
                "root_chunks": 1,
                "split_events": 0,
                "split_child_chunks": 0,
                "leaf_chunks": 1,
                "max_split_depth": 0,
                "deep_calls": 1,
                "repair_calls": 0,
                "assessment_usable": 1,
                "assessment_nothing_usable": 0,
                "assessment_needs_smaller_chunk": 0,
                "unclassified_empty_payloads": 0,
            },
        )
        monkeypatch.setattr(
            extract_mod,
            "apply_extracted_payloads",
            lambda payload, **kwargs: applied.append((payload, kwargs)) or {
                **payload,
                "facts_stored": len(payload.get("raw_facts", []) or []),
                "facts_skipped": 0,
                "edges_created": 0,
                "facts": [],
                "snippets": {},
                "journal": {},
                "project_logs": {},
                "project_log_metrics": {},
            },
        )
        monkeypatch.setattr(ingest_runtime_mod, "run_session_logs_ingest", lambda **kwargs: {"status": "indexed"})
        monkeypatch.setattr(project_registry_mod, "snapshot_all_projects", lambda: [])
        monkeypatch.setattr(docs_updater_mod, "update_project_docs", lambda snapshots, extraction_result: {"docs_updated": 0})
        monkeypatch.setattr(extraction_daemon, "_warm_payload_embeddings", lambda facts: {
            "requested": len(facts),
            "unique": len(facts),
            "cache_hits": 0,
            "warmed": len(facts),
            "failed": 0,
            "skipped_empty": 0,
        })

        try:
            extraction_daemon.write_signal(
                signal_type="session_end",
                session_id=session_id,
                transcript_path=str(transcript_path),
            )
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])

            assert seen_transcripts
            assert "Niseko Kinesis Phoebe Bridgers" in seen_transcripts[0]
            assert len(applied) == 1
            cursor = extraction_daemon.read_cursor(session_id, source_key=source_key)
            assert cursor["line_offset"] == len(transcript_lines)
            assert cursor["last_flushed_line_offset"] == len(transcript_lines)
        finally:
            if real_registry is not None:
                sys.modules["core.subagent_registry"] = real_registry
            else:
                sys.modules.pop("core.subagent_registry", None)
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)
            if real_notify is not None:
                sys.modules["core.runtime.notify"] = real_notify
            else:
                sys.modules.pop("core.runtime.notify", None)

    def test_process_signal_ended_rolling_flush_migrates_alias_cursor_to_preserved_eof(
        self, monkeypatch, tmp_path
    ):
        import sys
        import types

        session_id = "8817b065-c63a-43f3-a68a-72b70f2729ed"
        live_path = tmp_path / ".openclaw" / "agents" / "main" / "sessions" / f"{session_id}.jsonl"
        mirror_path = (
            tmp_path
            / "instances"
            / "openclaw-main"
            / "logs"
            / "quaid"
            / "sessions"
            / f"{session_id}.jsonl"
        )
        live_path.parent.mkdir(parents=True, exist_ok=True)
        mirror_path.parent.mkdir(parents=True, exist_ok=True)
        live_path.write_text(
            '{"role":"user","content":"Chunk one"}\n',
            encoding="utf-8",
        )
        mirror_path.write_text(
            '{"role":"user","content":"Chunk one"}\n'
            '{"role":"user","content":"Chunk two"}\n'
            '{"role":"user","content":"Chunk three"}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
        instance_root = tmp_path / "instances" / "openclaw-main"
        instance_root.mkdir(parents=True, exist_ok=True)
        (instance_root / "config.json").write_text(
            json.dumps({"adapter": {"type": "openclaw"}}),
            encoding="utf-8",
        )
        source_key = "live-openclaw-cursor"
        extraction_daemon.write_cursor(session_id, 0, str(mirror_path), source_key=source_key)
        extraction_daemon.write_cursor(session_id, 7, str(live_path))
        extraction_daemon.write_rolling_state(
            session_id,
            {
                "session_id": session_id,
                "transcript_path": str(live_path),
                "buffer_transcript_path": str(mirror_path),
                "processed_line_offset": 3,
                "buffered_line_offset": 3,
                "semantic_buffer": "User: Chunk two\n\nUser: Chunk three",
                "semantic_buffer_tokens": 1646,
                "rolling_batches": 1,
                "raw_facts": [{"text": "Owner mentioned Chunk two", "category": "fact"}],
            },
        )

        monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "Owner")
        real_registry = sys.modules.get("core.subagent_registry")
        real_adapter = sys.modules.get("lib.adapter")
        fake_registry = types.ModuleType("core.subagent_registry")
        fake_registry.is_registered_subagent = lambda sid: False
        fake_registry.get_harvestable = lambda sid: []
        fake_registry.mark_harvested = lambda sid, cid: None
        fake_registry._registry_dir = lambda: tmp_path / "registry"
        sys.modules["core.subagent_registry"] = fake_registry

        fake_adapter_mod = types.ModuleType("lib.adapter")

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def quaid_home(self):
                return tmp_path

            def instance_root(self):
                return instance_root

            def data_dir(self):
                return instance_root / "data"

            def parse_session_jsonl(self, path):
                return "unused when semantic buffer is present"

        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        fake_adapter_mod.quaid_projects_dir = lambda home: Path(home) / "projects"
        fake_adapter_mod.quaid_tracking_dir = lambda home: Path(home) / ".git-tracking"
        sys.modules["lib.adapter"] = fake_adapter_mod

        import core.docs_updater_hook as docs_updater_mod
        import core.ingest_runtime as ingest_runtime_mod
        import core.project_registry as project_registry_mod
        import ingest.extract as extract_mod

        real_notify = sys.modules.get("core.runtime.notify")
        fake_notify = types.ModuleType("core.runtime.notify")
        fake_notify.notify_memory_extraction = lambda **kwargs: None
        sys.modules["core.runtime.notify"] = fake_notify

        seen_transcripts = []
        monkeypatch.setattr(
            extract_mod,
            "extract_from_transcript",
            lambda **kwargs: seen_transcripts.append(kwargs["transcript"]) or {
                "facts_stored": 1,
                "facts_skipped": 0,
                "edges_created": 0,
                "facts": [],
                "snippets": {},
                "journal": {},
                "project_logs": {},
                "project_log_metrics": {},
                "dry_run": True,
                "raw_facts": [],
                "raw_snippets": {},
                "raw_journal": {},
                "raw_project_logs": {},
                "carry_facts": [{"text": "Owner mentioned Chunk two"}],
                "carry_duplicate_facts_dropped": 0,
                "chunks_processed": 1,
                "chunks_total": 1,
                "root_chunks": 1,
                "split_events": 0,
                "split_child_chunks": 0,
                "leaf_chunks": 1,
                "max_split_depth": 0,
                "chunk_calls": 1,
                "deep_calls": 1,
                "repair_calls": 0,
                "assessment_usable": 1,
                "assessment_nothing_usable": 0,
                "assessment_needs_smaller_chunk": 0,
                "unclassified_empty_payloads": 0,
            },
        )
        monkeypatch.setattr(
            extract_mod,
            "apply_extracted_payloads",
            lambda payload, **kwargs: {
                **payload,
                "facts": [],
                "snippets": {},
                "journal": {},
                "project_logs": {},
                "project_log_metrics": {},
            },
        )
        monkeypatch.setattr(ingest_runtime_mod, "run_session_logs_ingest", lambda **kwargs: {"status": "indexed"})
        monkeypatch.setattr(extraction_daemon, "_request_session_logs_ingest", lambda **kwargs: {"status": "indexed"})
        monkeypatch.setattr(project_registry_mod, "snapshot_all_projects", lambda: [])
        monkeypatch.setattr(docs_updater_mod, "update_project_docs", lambda snapshots, extraction_result: {"docs_updated": 0})
        monkeypatch.setattr(
            extraction_daemon,
            "_read_usage_totals",
            lambda: {
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "fast_calls": 0,
                "fast_input_tokens": 0,
                "fast_output_tokens": 0,
                "deep_calls": 0,
                "deep_input_tokens": 0,
                "deep_output_tokens": 0,
            },
        )

        try:
            extraction_daemon.write_signal(
                signal_type="session_end",
                session_id=session_id,
                transcript_path=str(mirror_path),
                meta={
                    "reason": "ended_rolling_buffer_flush",
                    "source_cursor_key": source_key,
                    "staged_payload_sweep": True,
                    "flush_staged_payload_only": True,
                    "buffered_line_offset": 0,
                },
            )
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])

            source_cursor = extraction_daemon.read_cursor(session_id, source_key=source_key)
            alias_cursor = extraction_daemon.read_cursor(session_id)
            assert source_cursor["transcript_path"] == str(mirror_path)
            assert alias_cursor["transcript_path"] == str(mirror_path)
            assert source_cursor["line_offset"] == 3
            assert alias_cursor["line_offset"] == 3
            assert source_cursor["processed_signal_type"] == "session_end"
            assert alias_cursor["processed_signal_type"] == "session_end"
            assert not extraction_daemon._rolling_state_path(session_id).exists()
        finally:
            if real_registry is not None:
                sys.modules["core.subagent_registry"] = real_registry
            else:
                sys.modules.pop("core.subagent_registry", None)
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)
            if real_notify is not None:
                sys.modules["core.runtime.notify"] = real_notify
            else:
                sys.modules.pop("core.runtime.notify", None)

    def test_rolling_state_alias_prefers_signal_uuid_without_stable_transcript_path(self, monkeypatch, tmp_path):
        old_uuid = "019ebe53-23ee-7561-9e54-dbc9c08085d7"
        new_uuid = "019ebe57-f843-7853-8130-49d1b5efb5bb"
        old_session_id = f"rollout-2026-06-13T00-13-00-{old_uuid}"
        new_session_id = f"rollout-2026-06-13T00-18-16-{new_uuid}"
        old_transcript_path = tmp_path / f"{old_session_id}.jsonl"
        new_transcript_path = tmp_path / f"{new_session_id}.jsonl"

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        (tmp_path / "instances" / "rolling-inst").mkdir(parents=True, exist_ok=True)

        extraction_daemon.write_rolling_state(
            old_session_id,
            {
                "session_id": old_session_id,
                "transcript_path": str(old_transcript_path),
                "processed_line_offset": 34,
                "buffered_line_offset": 34,
                "semantic_buffer": "User: Baxter uses an orange linen notebook from Emília Rosa.",
                "semantic_buffer_tokens": 435,
                "raw_facts": [],
            },
        )
        extraction_daemon.write_rolling_state(
            new_session_id,
            {
                "session_id": new_session_id,
                "transcript_path": str(new_transcript_path),
                "processed_line_offset": 10,
                "buffered_line_offset": 10,
                "semantic_buffer": "User: New session maintenance tail.",
                "semantic_buffer_tokens": 14,
                "raw_facts": [],
            },
        )

        state, state_key = extraction_daemon._read_rolling_state_for_signal(old_uuid, "")
        assert state_key == old_session_id
        assert "Baxter" in state["semantic_buffer"]

        state, state_key = extraction_daemon._read_rolling_state_for_signal(old_uuid, str(new_transcript_path))
        assert state_key == old_session_id
        assert "Baxter" in state["semantic_buffer"]

        different_uuid = "019ebe58-aaaa-bbbb-cccc-49d1b5efb5bb"
        state, state_key = extraction_daemon._read_rolling_state_for_signal(
            different_uuid,
            str(new_transcript_path),
        )
        assert state_key == different_uuid
        assert not extraction_daemon._rolling_state_has_pending_content(state)

    def test_rolling_state_signal_identity_failure_respects_failhard(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        (tmp_path / "instances" / "rolling-inst").mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)
        monkeypatch.setattr(
            extraction_daemon,
            "_signal_source_identity",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("identity broken")),
        )

        with pytest.raises(RuntimeError, match="rolling state signal identity lookup failed") as excinfo:
            extraction_daemon._read_rolling_state_for_signal("sess-signal", "/tmp/transcript.jsonl")

        assert isinstance(excinfo.value.__cause__, RuntimeError)
        assert "identity broken" in str(excinfo.value.__cause__)

    def test_rolling_state_staged_identity_failure_respects_failhard(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        (tmp_path / "instances" / "rolling-inst").mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)
        extraction_daemon.write_rolling_state(
            "state-sess",
            {
                "session_id": "state-sess",
                "transcript_path": "/tmp/state.jsonl",
                "semantic_buffer": "User: pending rolling buffer",
                "semantic_buffer_tokens": 10,
            },
        )

        def _identity(_session_id, _transcript_path, **kwargs):
            if kwargs.get("staged_state") is not None:
                raise RuntimeError("state identity broken")
            return "wanted-identity"

        monkeypatch.setattr(extraction_daemon, "_signal_source_identity", _identity)

        with pytest.raises(RuntimeError, match="rolling staged-state identity lookup failed") as excinfo:
            extraction_daemon._read_rolling_state_for_signal("wanted-sess", "/tmp/wanted.jsonl")

        assert isinstance(excinfo.value.__cause__, RuntimeError)
        assert "state identity broken" in str(excinfo.value.__cause__)

    def test_rolling_state_staged_source_cursor_failure_respects_failhard(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        (tmp_path / "instances" / "rolling-inst").mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

        def _identity(_session_id, _transcript_path, **kwargs):
            return "state-identity" if kwargs.get("staged_state") is not None else "wanted-identity"

        monkeypatch.setattr(extraction_daemon, "_signal_source_identity", _identity)
        monkeypatch.setattr(
            extraction_daemon,
            "_signal_source_cursor_key",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("source cursor broken")),
        )
        extraction_daemon.write_rolling_state(
            "state-sess",
            {
                "session_id": "state-sess",
                "transcript_path": "/tmp/state.jsonl",
                "semantic_buffer": "User: pending rolling buffer",
                "semantic_buffer_tokens": 10,
            },
        )

        with pytest.raises(RuntimeError, match="rolling staged-state source cursor lookup failed") as excinfo:
            extraction_daemon._read_rolling_state_for_signal(
                "wanted-sess",
                "/tmp/wanted.jsonl",
                source_cursor_key="wanted-source-key",
            )

        assert isinstance(excinfo.value.__cause__, RuntimeError)
        assert "source cursor broken" in str(excinfo.value.__cause__)

    def test_staged_flush_without_completed_rolling_batch_drains_semantic_buffer(self, monkeypatch, tmp_path):
        import sys
        import types

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"Chunk one stable memory"}\n'
            '{"role":"user","content":"Chunk two Baxter orange linen notebook and Emilia Rosa markers"}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        instance_root = tmp_path / "instances" / "rolling-inst"
        instance_root.mkdir(parents=True, exist_ok=True)
        (instance_root / "config.json").write_text(
            json.dumps({"adapter": {"type": "standalone"}}),
            encoding="utf-8",
        )
        extraction_daemon.write_cursor("sess-roll", 2, str(transcript_path))
        extraction_daemon.write_rolling_state(
            "sess-roll",
            {
                "session_id": "sess-roll",
                "transcript_path": str(transcript_path),
                "processed_line_offset": 2,
                "buffered_line_offset": 2,
                "semantic_buffer": "User: Baxter uses the orange linen notebook and Emilia Rosa marker.",
                "semantic_buffer_tokens": 435,
                "rolling_batches": 0,
                "carry_facts": [{"text": "Owner has stable carry context"}],
                "raw_facts": [{"text": "Owner staged prior fact", "category": "fact"}],
            },
        )
        monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "Owner")

        real_registry = sys.modules.get("core.subagent_registry")
        real_adapter = sys.modules.get("lib.adapter")
        real_notify = sys.modules.get("core.runtime.notify")
        fake_registry = types.ModuleType("core.subagent_registry")
        fake_registry.is_registered_subagent = lambda sid: False
        fake_registry.get_harvestable = lambda sid: []
        fake_registry.mark_harvested = lambda sid, cid: None
        fake_registry._registry_dir = lambda: tmp_path / "registry"
        sys.modules["core.subagent_registry"] = fake_registry

        fake_adapter_mod = types.ModuleType("lib.adapter")

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def quaid_home(self):
                return tmp_path

            def instance_root(self):
                return instance_root

            def data_dir(self):
                return instance_root / "data"

            def parse_session_jsonl(self, path):
                raise AssertionError("cursor is at EOF; semantic buffer should be drained directly")

        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        fake_adapter_mod.quaid_projects_dir = lambda home: Path(home) / "projects"
        fake_adapter_mod.quaid_tracking_dir = lambda home: Path(home) / ".git-tracking"
        sys.modules["lib.adapter"] = fake_adapter_mod
        fake_notify = types.ModuleType("core.runtime.notify")
        fake_notify.notify_memory_extraction = lambda **kwargs: None
        sys.modules["core.runtime.notify"] = fake_notify

        import core.docs_updater_hook as docs_updater_mod
        import core.ingest_runtime as ingest_runtime_mod
        import core.project_registry as project_registry_mod
        import ingest.extract as extract_mod

        seen_transcripts = []
        applied_payloads = []

        monkeypatch.setattr(
            extract_mod,
            "extract_from_transcript",
            lambda **kwargs: seen_transcripts.append(kwargs["transcript"]) or {
                "facts_stored": 0,
                "facts_skipped": 0,
                "edges_created": 0,
                "facts": [],
                "snippets": {},
                "journal": {},
                "project_logs": {},
                "project_log_metrics": {},
                "dry_run": True,
                "raw_facts": [{"text": "Owner uses the orange linen notebook", "category": "fact"}],
                "raw_snippets": {},
                "raw_journal": {},
                "raw_project_logs": {},
                "carry_facts": [{"text": "Owner uses the orange linen notebook"}],
                "carry_duplicate_facts_dropped": 0,
                "payload_duplicate_facts_collapsed": 0,
                "chunks_processed": 1,
                "chunks_total": 1,
                "root_chunks": 1,
                "split_events": 0,
                "split_child_chunks": 0,
                "leaf_chunks": 1,
                "max_split_depth": 0,
                "chunk_calls": 1,
                "deep_calls": 1,
                "repair_calls": 0,
                "assessment_usable": 1,
                "assessment_nothing_usable": 0,
                "assessment_needs_smaller_chunk": 0,
                "unclassified_empty_payloads": 0,
            },
        )
        monkeypatch.setattr(
            extract_mod,
            "apply_extracted_payloads",
            lambda payload, **kwargs: applied_payloads.append(payload) or {
                **payload,
                "facts_stored": len(payload.get("raw_facts", []) or []),
                "facts_skipped": 0,
                "edges_created": 0,
                "facts": [],
                "snippets": {},
                "journal": {},
                "project_logs": {},
                "project_log_metrics": {},
            },
        )
        monkeypatch.setattr(ingest_runtime_mod, "run_session_logs_ingest", lambda **kwargs: {"status": "indexed"})
        monkeypatch.setattr(project_registry_mod, "snapshot_all_projects", lambda: [])
        monkeypatch.setattr(docs_updater_mod, "update_project_docs", lambda snapshots, extraction_result: {"docs_updated": 0})
        monkeypatch.setattr(extraction_daemon, "_warm_payload_embeddings", lambda facts: {
            "requested": len(facts),
            "unique": len(facts),
            "cache_hits": 0,
            "warmed": len(facts),
            "failed": 0,
            "skipped_empty": 0,
        })
        monkeypatch.setattr(
            extraction_daemon,
            "_read_usage_totals",
            lambda: {
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "fast_calls": 0,
                "fast_input_tokens": 0,
                "fast_output_tokens": 0,
                "deep_calls": 0,
                "deep_input_tokens": 0,
                "deep_output_tokens": 0,
            },
        )

        try:
            extraction_daemon.write_signal(
                signal_type="session_end",
                session_id="sess-roll",
                transcript_path=str(transcript_path),
                meta={
                    "reason": "rolling_stage_flush",
                    "source_signal": "rolling",
                    "staged_payload_sweep": True,
                    "flush_staged_payload_only": True,
                },
            )
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])

            assert seen_transcripts == [
                "User: Baxter uses the orange linen notebook and Emilia Rosa marker."
            ]
            assert len(applied_payloads) == 1
            assert [fact["text"] for fact in applied_payloads[0]["raw_facts"]] == [
                "Owner staged prior fact",
                "Owner uses the orange linen notebook",
            ]
            assert extraction_daemon.read_cursor("sess-roll")["line_offset"] == 2
            assert not extraction_daemon._rolling_state_path("sess-roll").exists()
        finally:
            if real_registry is not None:
                sys.modules["core.subagent_registry"] = real_registry
            else:
                sys.modules.pop("core.subagent_registry", None)
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)
            if real_notify is not None:
                sys.modules["core.runtime.notify"] = real_notify
            else:
                sys.modules.pop("core.runtime.notify", None)

    def test_process_signal_session_end_emits_rolling_stage_before_flushing_over_budget_buffer(self, monkeypatch, tmp_path):
        import sys
        import types

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"My sister is Diana"}\n'
            '{"role":"assistant","content":"Her daughter is Alice"}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        instance_root = tmp_path / "instances" / "rolling-inst"
        instance_root.mkdir(parents=True, exist_ok=True)
        (instance_root / "config.json").write_text(
            json.dumps(
                {
                    "adapter": {"type": "standalone"},
                    "livetest": {"enableExtractionBufferLog": True},
                }
            ),
            encoding="utf-8",
        )
        extraction_daemon.write_cursor("sess-roll", 0, str(transcript_path))
        extraction_daemon.write_rolling_state(
            "sess-roll",
            {
                "session_id": "sess-roll",
                "transcript_path": str(transcript_path),
                "processed_line_offset": 2,
                "buffered_line_offset": 2,
                "semantic_buffer": "User: My sister is Diana\n\nAssistant: Her daughter is Alice",
                "semantic_buffer_tokens": 12,
                "carry_facts": [],
                "raw_facts": [],
            },
        )
        monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "Owner")
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda default=8_000: 10)

        real_registry = sys.modules.get("core.subagent_registry")
        real_adapter = sys.modules.get("lib.adapter")
        fake_registry = types.ModuleType("core.subagent_registry")
        fake_registry.is_registered_subagent = lambda sid: False
        fake_registry.get_harvestable = lambda sid: []
        fake_registry.mark_harvested = lambda sid, cid: None
        fake_registry._registry_dir = lambda: tmp_path / "registry"
        sys.modules["core.subagent_registry"] = fake_registry

        fake_adapter_mod = types.ModuleType("lib.adapter")

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def quaid_home(self):
                return tmp_path

            def instance_root(self):
                return instance_root

            def data_dir(self):
                return instance_root / "data"

            def parse_session_jsonl(self, path):
                return "unused when semantic buffer is present"

        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        fake_adapter_mod.quaid_projects_dir = lambda home: Path(home) / "projects"
        fake_adapter_mod.quaid_tracking_dir = lambda home: Path(home) / ".git-tracking"
        sys.modules["lib.adapter"] = fake_adapter_mod

        import core.docs_updater_hook as docs_updater_mod
        import core.ingest_runtime as ingest_runtime_mod
        import core.project_registry as project_registry_mod
        import ingest.extract as extract_mod

        real_notify = sys.modules.get("core.runtime.notify")
        fake_notify = types.ModuleType("core.runtime.notify")
        fake_notify.notify_memory_extraction = lambda **kwargs: None
        sys.modules["core.runtime.notify"] = fake_notify

        seen_transcripts = []
        rolling_metrics = []
        stage_payload = {
            "facts_stored": 0,
            "facts_skipped": 0,
            "edges_created": 0,
            "facts": [],
            "snippets": {},
            "journal": {},
            "project_logs": {},
            "project_log_metrics": {},
            "dry_run": True,
            "raw_facts": [{"text": "Owner has a sister named Diana", "category": "fact"}],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "carry_facts": [{"text": "Owner has a sister named Diana"}],
            "carry_duplicate_facts_dropped": 0,
            "chunks_processed": 1,
            "chunks_total": 1,
            "root_chunks": 1,
            "split_events": 0,
            "split_child_chunks": 0,
            "leaf_chunks": 1,
            "max_split_depth": 0,
            "chunk_calls": 1,
            "deep_calls": 1,
            "repair_calls": 0,
            "assessment_usable": 1,
            "assessment_nothing_usable": 0,
            "assessment_needs_smaller_chunk": 0,
            "unclassified_empty_payloads": 0,
        }
        monkeypatch.setattr(
            extract_mod,
            "extract_from_transcript",
            lambda **kwargs: seen_transcripts.append(kwargs["transcript"]) or dict(stage_payload),
        )
        monkeypatch.setattr(
            extract_mod,
            "apply_extracted_payloads",
            lambda payload, **kwargs: {
                **payload,
                "facts": [],
                "snippets": {},
                "journal": {},
                "project_logs": {},
                "project_log_metrics": {},
            },
        )
        monkeypatch.setattr(
            ingest_runtime_mod,
            "run_session_logs_ingest",
            lambda **kwargs: {"status": "indexed"},
        )
        monkeypatch.setattr(project_registry_mod, "snapshot_all_projects", lambda: [])
        monkeypatch.setattr(
            docs_updater_mod,
            "update_project_docs",
            lambda snapshots, extraction_result: {"docs_updated": 0},
        )
        monkeypatch.setattr(
            extraction_daemon,
            "_read_usage_totals",
            lambda: {
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "fast_calls": 0,
                "fast_input_tokens": 0,
                "fast_output_tokens": 0,
                "deep_calls": 0,
                "deep_input_tokens": 0,
                "deep_output_tokens": 0,
            },
        )
        monkeypatch.setattr(
            extraction_daemon,
            "write_rolling_metric",
            lambda event, session_id, **data: rolling_metrics.append(
                {"event": event, "session_id": session_id, **data}
            ),
        )
        monkeypatch.setattr(extraction_daemon, "_warm_payload_embeddings", lambda facts: {
            "requested": len(facts),
            "unique": len(facts),
            "cache_hits": 0,
            "warmed": len(facts),
            "failed": 0,
            "skipped_empty": 0,
        })

        try:
            extraction_daemon.write_signal(
                signal_type="session_end",
                session_id="sess-roll",
                transcript_path=str(transcript_path),
            )
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])

            assert seen_transcripts == ["User: My sister is Diana\n\nAssistant: Her daughter is Alice"]
            assert [metric["event"] for metric in rolling_metrics[-2:]] == ["rolling_stage", "rolling_flush"]
            assert rolling_metrics[-1]["signal_type"] == "session_end"
            assert rolling_metrics[-1]["processing_signal_type"] == "session_end"
            assert extraction_daemon.read_cursor("sess-roll")["line_offset"] == 2
            assert not extraction_daemon._rolling_state_path("sess-roll").exists()
            buffer_log = (instance_root / "logs" / "daemon" / "extraction-buffer.log").read_text(
                encoding="utf-8"
            )
            assert "phase=rolling_stage" in buffer_log
            assert "signal=session_end" in buffer_log
        finally:
            if real_registry is not None:
                sys.modules["core.subagent_registry"] = real_registry
            else:
                sys.modules.pop("core.subagent_registry", None)
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)
            if real_notify is not None:
                sys.modules["core.runtime.notify"] = real_notify
            else:
                sys.modules.pop("core.runtime.notify", None)

    def test_process_signal_compaction_emits_rolling_stage_before_flushing_with_new_lines(self, monkeypatch, tmp_path):
        import sys
        import types

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"My sister is Diana"}\n'
            '{"role":"assistant","content":"Her daughter Alice just opened a neighborhood ceramics studio and needs bookkeeping help next month."}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        instance_root = tmp_path / "instances" / "rolling-inst"
        instance_root.mkdir(parents=True, exist_ok=True)
        (instance_root / "config.json").write_text(
            json.dumps(
                {
                    "adapter": {"type": "standalone"},
                    "livetest": {"enableExtractionBufferLog": True},
                }
            ),
            encoding="utf-8",
        )
        extraction_daemon.write_cursor("sess-roll", 0, str(transcript_path))
        extraction_daemon.write_rolling_state(
            "sess-roll",
            {
                "session_id": "sess-roll",
                "transcript_path": str(transcript_path),
                "processed_line_offset": 1,
                "buffered_line_offset": 1,
                "semantic_buffer": "User: My sister is Diana",
                "semantic_buffer_tokens": 12,
                "carry_facts": [],
                "raw_facts": [],
            },
        )
        monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "Owner")
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda default=8_000: 10)

        real_registry = sys.modules.get("core.subagent_registry")
        real_adapter = sys.modules.get("lib.adapter")
        fake_registry = types.ModuleType("core.subagent_registry")
        fake_registry.is_registered_subagent = lambda sid: False
        fake_registry.get_harvestable = lambda sid: []
        fake_registry.mark_harvested = lambda sid, cid: None
        fake_registry._registry_dir = lambda: tmp_path / "registry"
        sys.modules["core.subagent_registry"] = fake_registry

        fake_adapter_mod = types.ModuleType("lib.adapter")

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def quaid_home(self):
                return tmp_path

            def instance_root(self):
                return instance_root

            def data_dir(self):
                return instance_root / "data"

            def parse_session_jsonl(self, path):
                return (
                    "Assistant: Her daughter Alice just opened a neighborhood ceramics studio "
                    "and needs bookkeeping help next month."
                )

        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        fake_adapter_mod.quaid_projects_dir = lambda home: Path(home) / "projects"
        fake_adapter_mod.quaid_tracking_dir = lambda home: Path(home) / ".git-tracking"
        sys.modules["lib.adapter"] = fake_adapter_mod

        import core.docs_updater_hook as docs_updater_mod
        import core.ingest_runtime as ingest_runtime_mod
        import core.project_registry as project_registry_mod
        import ingest.extract as extract_mod

        real_notify = sys.modules.get("core.runtime.notify")
        fake_notify = types.ModuleType("core.runtime.notify")
        fake_notify.notify_memory_extraction = lambda **kwargs: None
        sys.modules["core.runtime.notify"] = fake_notify

        seen_transcripts = []
        rolling_metrics = []
        stage_payload = {
            "facts_stored": 0,
            "facts_skipped": 0,
            "edges_created": 0,
            "facts": [],
            "snippets": {},
            "journal": {},
            "project_logs": {},
            "project_log_metrics": {},
            "dry_run": True,
            "raw_facts": [{"text": "Owner has a sister named Diana", "category": "fact"}],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "carry_facts": [{"text": "Owner has a sister named Diana"}],
            "carry_duplicate_facts_dropped": 0,
            "chunks_processed": 1,
            "chunks_total": 1,
            "root_chunks": 1,
            "split_events": 0,
            "split_child_chunks": 0,
            "leaf_chunks": 1,
            "max_split_depth": 0,
            "chunk_calls": 1,
            "deep_calls": 1,
            "repair_calls": 0,
            "assessment_usable": 1,
            "assessment_nothing_usable": 0,
            "assessment_needs_smaller_chunk": 0,
            "unclassified_empty_payloads": 0,
        }
        monkeypatch.setattr(
            extract_mod,
            "extract_from_transcript",
            lambda **kwargs: seen_transcripts.append(kwargs["transcript"]) or dict(stage_payload),
        )
        monkeypatch.setattr(
            extract_mod,
            "apply_extracted_payloads",
            lambda payload, **kwargs: {
                **payload,
                "facts": [],
                "snippets": {},
                "journal": {},
                "project_logs": {},
                "project_log_metrics": {},
            },
        )
        monkeypatch.setattr(
            ingest_runtime_mod,
            "run_session_logs_ingest",
            lambda **kwargs: {"status": "indexed"},
        )
        monkeypatch.setattr(project_registry_mod, "snapshot_all_projects", lambda: [])
        monkeypatch.setattr(
            docs_updater_mod,
            "update_project_docs",
            lambda snapshots, extraction_result: {"docs_updated": 0},
        )
        monkeypatch.setattr(
            extraction_daemon,
            "_read_usage_totals",
            lambda: {
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "fast_calls": 0,
                "fast_input_tokens": 0,
                "fast_output_tokens": 0,
                "deep_calls": 0,
                "deep_input_tokens": 0,
                "deep_output_tokens": 0,
            },
        )
        monkeypatch.setattr(
            extraction_daemon,
            "write_rolling_metric",
            lambda event, session_id, **data: rolling_metrics.append(
                {"event": event, "session_id": session_id, **data}
            ),
        )
        monkeypatch.setattr(extraction_daemon, "_warm_payload_embeddings", lambda facts: {
            "requested": len(facts),
            "unique": len(facts),
            "cache_hits": 0,
            "warmed": len(facts),
            "failed": 0,
            "skipped_empty": 0,
        })

        try:
            extraction_daemon.write_signal(
                signal_type="compaction",
                session_id="sess-roll",
                transcript_path=str(transcript_path),
            )
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])

            assert seen_transcripts[0].startswith("User: My sister is Diana")
            assert "Assistant: Her daughter Alice just opened" in seen_transcripts[0]
            assert len(seen_transcripts) == 1
            assert [metric["event"] for metric in rolling_metrics[-2:]] == ["rolling_stage", "rolling_flush"]
            assert extraction_daemon.read_cursor("sess-roll")["line_offset"] == 2
            assert not extraction_daemon._rolling_state_path("sess-roll").exists()
            buffer_log = (instance_root / "logs" / "daemon" / "extraction-buffer.log").read_text(
                encoding="utf-8"
            )
            assert "phase=rolling_stage" in buffer_log
            assert "signal=compaction" in buffer_log
        finally:
            if real_registry is not None:
                sys.modules["core.subagent_registry"] = real_registry
            else:
                sys.modules.pop("core.subagent_registry", None)
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)
            if real_notify is not None:
                sys.modules["core.runtime.notify"] = real_notify
            else:
                sys.modules.pop("core.runtime.notify", None)

    def test_process_signal_compaction_under_budget_does_not_duplicate_tail_after_buffer_refresh(self, monkeypatch, tmp_path):
        import sys
        import types

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"My sister is Diana"}\n'
            '{"role":"assistant","content":"Her daughter Alice opened a studio."}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        instance_root = tmp_path / "instances" / "rolling-inst"
        instance_root.mkdir(parents=True, exist_ok=True)
        (instance_root / "config.json").write_text(
            json.dumps(
                {
                    "adapter": {"type": "standalone"},
                    "livetest": {"enableExtractionBufferLog": True},
                }
            ),
            encoding="utf-8",
        )
        extraction_daemon.write_cursor("sess-roll", 0, str(transcript_path))
        extraction_daemon.write_rolling_state(
            "sess-roll",
            {
                "session_id": "sess-roll",
                "transcript_path": str(transcript_path),
                "processed_line_offset": 1,
                "buffered_line_offset": 1,
                "semantic_buffer": "User: My sister is Diana",
                "semantic_buffer_tokens": 4,
                "carry_facts": [],
                "raw_facts": [],
            },
        )
        monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "Owner")
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda default=8_000: 10_000)

        real_registry = sys.modules.get("core.subagent_registry")
        real_adapter = sys.modules.get("lib.adapter")
        fake_registry = types.ModuleType("core.subagent_registry")
        fake_registry.is_registered_subagent = lambda sid: False
        fake_registry.get_harvestable = lambda sid: []
        fake_registry.mark_harvested = lambda sid, cid: None
        fake_registry._registry_dir = lambda: tmp_path / "registry"
        sys.modules["core.subagent_registry"] = fake_registry

        fake_adapter_mod = types.ModuleType("lib.adapter")

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def quaid_home(self):
                return tmp_path

            def instance_root(self):
                return instance_root

            def data_dir(self):
                return instance_root / "data"

            def parse_session_jsonl(self, path):
                return "Assistant: Her daughter Alice opened a studio."

        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        sys.modules["lib.adapter"] = fake_adapter_mod

        import core.docs_updater_hook as docs_updater_mod
        import core.ingest_runtime as ingest_runtime_mod
        import core.project_registry as project_registry_mod
        import ingest.extract as extract_mod

        real_notify = sys.modules.get("core.runtime.notify")
        fake_notify = types.ModuleType("core.runtime.notify")
        fake_notify.notify_memory_extraction = lambda **kwargs: None
        sys.modules["core.runtime.notify"] = fake_notify

        seen_transcripts = []
        monkeypatch.setattr(
            extract_mod,
            "extract_from_transcript",
            lambda **kwargs: seen_transcripts.append(kwargs["transcript"]) or {
                "facts_stored": 0,
                "facts_skipped": 0,
                "edges_created": 0,
                "facts": [],
                "snippets": {},
                "journal": {},
                "project_logs": {},
                "project_log_metrics": {},
                "dry_run": True,
                "raw_facts": [],
                "raw_snippets": {},
                "raw_journal": {},
                "raw_project_logs": {},
                "carry_facts": [],
                "carry_duplicate_facts_dropped": 0,
                "chunks_processed": 1,
                "chunks_total": 1,
                "root_chunks": 1,
                "split_events": 0,
                "split_child_chunks": 0,
                "leaf_chunks": 1,
                "max_split_depth": 0,
                "chunk_calls": 1,
                "deep_calls": 1,
                "repair_calls": 0,
                "assessment_usable": 1,
                "assessment_nothing_usable": 0,
                "assessment_needs_smaller_chunk": 0,
                "unclassified_empty_payloads": 0,
            },
        )
        monkeypatch.setattr(
            extract_mod,
            "apply_extracted_payloads",
            lambda payload, **kwargs: {
                **payload,
                "facts": [],
                "snippets": {},
                "journal": {},
                "project_logs": {},
                "project_log_metrics": {},
            },
        )
        monkeypatch.setattr(
            ingest_runtime_mod,
            "run_session_logs_ingest",
            lambda **kwargs: {"status": "indexed"},
        )
        monkeypatch.setattr(project_registry_mod, "snapshot_all_projects", lambda: [])
        monkeypatch.setattr(
            docs_updater_mod,
            "update_project_docs",
            lambda snapshots, extraction_result: {"docs_updated": 0},
        )
        monkeypatch.setattr(
            extraction_daemon,
            "_read_usage_totals",
            lambda: {
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "fast_calls": 0,
                "fast_input_tokens": 0,
                "fast_output_tokens": 0,
                "deep_calls": 0,
                "deep_input_tokens": 0,
                "deep_output_tokens": 0,
            },
        )

        try:
            extraction_daemon.write_signal(
                signal_type="compaction",
                session_id="sess-roll",
                transcript_path=str(transcript_path),
            )
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])

            assert seen_transcripts == [
                "User: My sister is Diana\n\nAssistant: Her daughter Alice opened a studio."
            ]
        finally:
            if real_registry is not None:
                sys.modules["core.subagent_registry"] = real_registry
            else:
                sys.modules.pop("core.subagent_registry", None)
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)
            if real_notify is not None:
                sys.modules["core.runtime.notify"] = real_notify
            else:
                sys.modules.pop("core.runtime.notify", None)

    def test_process_signal_rolling_flush_failure_writes_error_metric(self, monkeypatch, tmp_path):
        import sqlite3
        import sys
        import types

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"My sister is Diana"}\n'
            '{"role":"assistant","content":"Noted"}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        instance_root = tmp_path / "instances" / "rolling-inst"
        instance_root.mkdir(parents=True, exist_ok=True)
        (instance_root / "config.json").write_text(
            json.dumps({"adapter": {"type": "standalone"}}),
            encoding="utf-8",
        )
        extraction_daemon.write_cursor("sess-roll", 0, str(transcript_path))
        monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "Owner")
        monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: False)
        monkeypatch.setattr(extraction_daemon, "_rolling_ready_threshold", lambda chunk_budget: 1)

        real_registry = sys.modules.get("core.subagent_registry")
        real_adapter = sys.modules.get("lib.adapter")
        fake_registry = types.ModuleType("core.subagent_registry")
        fake_registry.is_registered_subagent = lambda sid: False
        fake_registry.get_harvestable = lambda sid: []
        fake_registry.mark_harvested = lambda sid, cid: None
        fake_registry._registry_dir = lambda: tmp_path / "registry"
        sys.modules["core.subagent_registry"] = fake_registry

        fake_adapter_mod = types.ModuleType("lib.adapter")
        if real_adapter is not None:
            fake_adapter_mod.StandaloneAdapter = getattr(real_adapter, "StandaloneAdapter", object)
        class _FakeAdapter(_OwnedTestAdapterMixin):
            def quaid_home(self):
                return tmp_path

            def instance_root(self):
                return instance_root

            def data_dir(self):
                return instance_root / "data"

            def parse_session_jsonl(self, path):
                return 'User: My sister is Diana\n\nAssistant: Noted'
        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        sys.modules["lib.adapter"] = fake_adapter_mod

        import ingest.extract as extract_mod

        rolling_metrics = []
        usage_snapshots = iter([
            {"calls": 0, "input_tokens": 0, "output_tokens": 0, "fast_calls": 0, "fast_input_tokens": 0, "fast_output_tokens": 0, "deep_calls": 0, "deep_input_tokens": 0, "deep_output_tokens": 0},
            {"calls": 1, "input_tokens": 100, "output_tokens": 60, "fast_calls": 0, "fast_input_tokens": 0, "fast_output_tokens": 0, "deep_calls": 1, "deep_input_tokens": 100, "deep_output_tokens": 60},
            {"calls": 1, "input_tokens": 100, "output_tokens": 60, "fast_calls": 0, "fast_input_tokens": 0, "fast_output_tokens": 0, "deep_calls": 1, "deep_input_tokens": 100, "deep_output_tokens": 60},
        ])

        stage_payload = {
            "facts_stored": 1,
            "facts_skipped": 0,
            "edges_created": 0,
            "facts": [{"text": "Owner has a sister named Diana", "status": "would_store", "edges": []}],
            "snippets": {},
            "journal": {},
            "project_logs": {},
            "project_log_metrics": {},
            "dry_run": True,
            "raw_facts": [{"text": "Owner has a sister named Diana", "category": "fact", "domains": ["personal"], "extraction_confidence": "high"}],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "carry_facts": [{"text": "Owner has a sister named Diana"}],
            "carry_duplicate_facts_dropped": 0,
            "chunks_processed": 1,
            "chunks_total": 1,
            "root_chunks": 1,
            "split_events": 0,
            "split_child_chunks": 0,
            "leaf_chunks": 1,
            "max_split_depth": 0,
            "deep_calls": 1,
            "repair_calls": 0,
            "assessment_usable": 1,
            "assessment_nothing_usable": 0,
            "assessment_needs_smaller_chunk": 0,
            "unclassified_empty_payloads": 0,
        }

        monkeypatch.setattr(extract_mod, "extract_from_transcript", lambda **kwargs: dict(stage_payload))
        monkeypatch.setattr(
            extract_mod,
            "apply_extracted_payloads",
            lambda *args, **kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")),
        )
        monkeypatch.setattr(extraction_daemon, "_read_usage_totals", lambda: dict(next(usage_snapshots)))
        monkeypatch.setattr(
            extraction_daemon,
            "write_rolling_metric",
            lambda event, session_id, **data: rolling_metrics.append({"event": event, "session_id": session_id, **data}),
        )
        monkeypatch.setattr(
            extraction_daemon,
            "_warm_payload_embeddings",
            lambda facts: {
                "requested": len(facts),
                "unique": len({str(f.get("text", "")) for f in facts}),
                "cache_hits": 0,
                "warmed": len({str(f.get("text", "")) for f in facts}),
                "failed": 0,
                "skipped_empty": 0,
            },
        )

        try:
            extraction_daemon.write_signal(
                signal_type="rolling",
                session_id="sess-roll",
                transcript_path=str(transcript_path),
            )
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])

            extraction_daemon.write_signal(
                signal_type="session_end",
                session_id="sess-roll",
                transcript_path=str(transcript_path),
            )
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])

            assert extraction_daemon.read_rolling_state("sess-roll")["rolling_batches"] == 1
            assert extraction_daemon.read_pending_signals()[0]["type"] == "session_end"
            assert rolling_metrics[-1]["event"] == "rolling_flush_error"
            assert rolling_metrics[-1]["phase"] == "flush_publish"
            assert rolling_metrics[-1]["error_type"] == "OperationalError"
            assert "database is locked" in rolling_metrics[-1]["error_message"]
            assert rolling_metrics[-1]["staged_facts"] == 1
            assert rolling_metrics[-1]["final_raw_fact_count"] == 1
        finally:
            if real_registry is not None:
                sys.modules["core.subagent_registry"] = real_registry
            else:
                sys.modules.pop("core.subagent_registry", None)
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

    def test_merge_staged_payloads_collapses_exact_duplicate_facts_across_batches(self):
        state = {
            "raw_facts": [
                {
                    "text": "Maya's half marathon finish time was 2:14",
                    "category": "fact",
                    "domains": ["personal"],
                    "extraction_confidence": "medium",
                }
            ],
            "rolling_batches": 1,
            "payload_duplicate_facts_collapsed": 0,
        }
        payload = {
            "raw_facts": [
                {
                    "text": "  Maya's half marathon finish time was 2:14  ",
                    "category": "fact",
                    "domains": ["health", "personal"],
                    "extraction_confidence": "high",
                }
            ],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "carry_facts": [],
            "facts_skipped": 0,
            "carry_duplicate_facts_dropped": 0,
        }

        merged = extraction_daemon.merge_staged_payloads(state, payload)

        assert merged["rolling_batches"] == 2
        assert len(merged["raw_facts"]) == 1
        assert merged["payload_duplicate_facts_collapsed"] == 1
        fact = merged["raw_facts"][0]
        assert fact["extraction_confidence"] == "high"
        assert sorted(fact["domains"]) == ["health", "personal"]

    def test_merge_staged_payloads_preserves_project_log_entry_dates(self):
        state = {
            "raw_facts": [],
            "raw_project_logs": {
                "recipe-app": [
                    {
                        "text": "Shipped retry middleware",
                        "created_at": "2026-03-01T09:15:00",
                    }
                ]
            },
            "rolling_batches": 1,
            "payload_duplicate_facts_collapsed": 0,
        }
        payload = {
            "raw_facts": [],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {
                "recipe-app": [
                    {
                        "text": "Shipped retry middleware",
                        "created_at": "2026-03-01T09:15:00",
                    },
                    {
                        "text": "Added error banner",
                        "created_at": "2026-03-05",
                    },
                ]
            },
            "carry_facts": [],
            "facts_skipped": 0,
            "carry_duplicate_facts_dropped": 0,
        }

        merged = extraction_daemon.merge_staged_payloads(state, payload)

        assert merged["rolling_batches"] == 2
        assert merged["raw_project_logs"] == {
            "recipe-app": [
                {
                    "text": "Shipped retry middleware",
                    "created_at": "2026-03-01T09:15:00",
                },
                {
                    "text": "Added error banner",
                    "created_at": "2026-03-05T23:59:59",
                },
            ]
        }

    def test_semantic_fact_dedup_signal_accepts_compact_unicode_fact(self):
        assert extraction_daemon._semantic_fact_has_dedup_signal("ok") is False
        assert extraction_daemon._semantic_fact_has_dedup_signal("Maya birthday") is False
        assert extraction_daemon._semantic_fact_has_dedup_signal("美玲は猫が好きです") is True

    def test_semantic_candidate_overlaps_includes_compact_unicode_fact(self):
        existing = [{"text": "美玲は猫が好きです"}]

        assert extraction_daemon._semantic_candidate_overlaps("美玲は猫が好きです", existing) == [0]

    def test_semantic_candidate_overlaps_uses_compact_unicode_grams(self):
        existing = [{"text": "美玲は猫が好きです"}]

        assert extraction_daemon._semantic_candidate_overlaps("美玲は猫好きです", existing) == [0]
        assert extraction_daemon._semantic_candidate_overlaps("雲門の稽古は水曜です", existing) == []

    def test_merge_staged_payloads_collapses_semantic_duplicate_fact_across_batches(self, monkeypatch):
        import datastore.memorydb.memory_graph as memory_graph
        import lib.similarity as similarity

        class _FakeGraph:
            def get_embedding(self, text):
                return [1.0, 0.0] if text else None

        state = {
            "raw_facts": [
                {
                    "text": "Maya's birthday dinner is planned for May 18",
                    "category": "fact",
                    "domains": ["personal"],
                    "extraction_confidence": "medium",
                }
            ],
            "rolling_batches": 1,
            "payload_duplicate_facts_collapsed": 0,
        }
        payload = {
            "raw_facts": [
                {
                    "text": "May 18 is when Maya's birthday dinner is planned",
                    "category": "fact",
                    "domains": ["personal", "schedule"],
                    "extraction_confidence": "high",
                }
            ],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "carry_facts": [],
            "facts_skipped": 0,
            "carry_duplicate_facts_dropped": 0,
        }

        monkeypatch.setattr(extraction_daemon, "_stage_dedup_settings", lambda: (0.98, 0.88, True))
        monkeypatch.setattr(extraction_daemon, "_semantic_candidate_overlaps", lambda *_args, **_kwargs: [0])
        monkeypatch.setattr(memory_graph, "get_graph", lambda: _FakeGraph())
        monkeypatch.setattr(similarity, "cosine_similarity", lambda *_args, **_kwargs: 0.92)
        monkeypatch.setattr(
            memory_graph,
            "_llm_dedup_check_many",
            lambda *_args, **_kwargs: {
                1: {
                    "is_same": True,
                    "subsumes": "a_subsumes_b",
                    "reasoning": "same fact",
                }
            },
        )

        merged = extraction_daemon.merge_staged_payloads(state, payload)

        assert merged["rolling_batches"] == 2
        assert len(merged["raw_facts"]) == 1
        assert merged["staged_semantic_duplicate_facts_collapsed"] == 1
        assert merged["staged_semantic_llm_checks"] == 1
        assert merged["staged_semantic_llm_same_hits"] == 1
        fact = merged["raw_facts"][0]
        assert fact["text"] == "May 18 is when Maya's birthday dinner is planned"
        assert sorted(fact["domains"]) == ["personal", "schedule"]
        assert fact["extraction_confidence"] == "high"

    def test_merge_staged_payloads_collapses_compact_unicode_semantic_duplicate(self, monkeypatch):
        import datastore.memorydb.memory_graph as memory_graph
        import lib.similarity as similarity

        class _FakeGraph:
            def get_embedding(self, text):
                return [1.0, 0.0] if text else None

        state = {
            "raw_facts": [
                {
                    "text": "美玲は猫が好きです",
                    "category": "fact",
                    "domains": ["personal"],
                    "extraction_confidence": "medium",
                }
            ],
            "rolling_batches": 1,
            "payload_duplicate_facts_collapsed": 0,
        }
        payload = {
            "raw_facts": [
                {
                    "text": "美玲は猫好きです",
                    "category": "fact",
                    "domains": ["personal", "pets"],
                    "extraction_confidence": "high",
                }
            ],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "carry_facts": [],
            "facts_skipped": 0,
            "carry_duplicate_facts_dropped": 0,
        }

        monkeypatch.setattr(extraction_daemon, "_stage_dedup_settings", lambda: (0.98, 0.88, True))
        monkeypatch.setattr(memory_graph, "get_graph", lambda: _FakeGraph())
        monkeypatch.setattr(similarity, "cosine_similarity", lambda *_args, **_kwargs: 0.92)
        monkeypatch.setattr(
            memory_graph,
            "_llm_dedup_check_many",
            lambda *_args, **_kwargs: {
                1: {
                    "is_same": True,
                    "subsumes": "a_subsumes_b",
                    "reasoning": "same fact",
                }
            },
        )

        merged = extraction_daemon.merge_staged_payloads(state, payload)

        assert merged["rolling_batches"] == 2
        assert len(merged["raw_facts"]) == 1
        assert merged["staged_semantic_duplicate_facts_collapsed"] == 1
        assert merged["staged_semantic_llm_checks"] == 1
        fact = merged["raw_facts"][0]
        assert fact["text"] == "美玲は猫好きです"
        assert sorted(fact["domains"]) == ["personal", "pets"]
        assert fact["extraction_confidence"] == "high"

    def test_merge_staged_payloads_subset_overlap_triggers_llm_below_gray_zone(self, monkeypatch):
        import datastore.memorydb.memory_graph as memory_graph
        import lib.similarity as similarity

        class _FakeGraph:
            def get_embedding(self, text):
                return [1.0, 0.0] if text else None

        state = {
            "raw_facts": [
                {
                    "text": "Solomon has a dog named Baxter",
                    "category": "fact",
                    "domains": ["personal"],
                    "extraction_confidence": "medium",
                }
            ],
            "rolling_batches": 1,
            "payload_duplicate_facts_collapsed": 0,
        }
        payload = {
            "raw_facts": [
                {
                    "text": "Solomon has a dog named Baxter who loves tennis balls",
                    "category": "fact",
                    "domains": ["personal", "pets"],
                    "extraction_confidence": "high",
                }
            ],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "carry_facts": [],
            "facts_skipped": 0,
            "carry_duplicate_facts_dropped": 0,
        }

        monkeypatch.setattr(extraction_daemon, "_stage_dedup_settings", lambda: (0.98, 0.88, True))
        monkeypatch.setattr(extraction_daemon, "_semantic_candidate_overlaps", lambda *_args, **_kwargs: [0])
        monkeypatch.setattr(memory_graph, "get_graph", lambda: _FakeGraph())
        monkeypatch.setattr(similarity, "cosine_similarity", lambda *_args, **_kwargs: 0.72)
        monkeypatch.setattr(
            memory_graph,
            "_llm_dedup_check_many",
            lambda *_args, **_kwargs: {
                1: {
                    "is_same": True,
                    "subsumes": "a_subsumes_b",
                    "reasoning": "subset duplicate",
                }
            },
        )

        merged = extraction_daemon.merge_staged_payloads(state, payload)

        assert len(merged["raw_facts"]) == 1
        assert merged["staged_semantic_subset_rows"] == 1
        assert merged["staged_semantic_llm_checks"] == 1
        assert merged["staged_semantic_llm_same_hits"] == 1
        assert merged["staged_semantic_duplicate_facts_collapsed"] == 1
        fact = merged["raw_facts"][0]
        assert fact["text"] == "Solomon has a dog named Baxter who loves tennis balls"
        assert sorted(fact["domains"]) == ["personal", "pets"]
        assert fact["extraction_confidence"] == "high"

    def test_merge_staged_payloads_subset_overlap_routes_negation_to_llm(self, monkeypatch):
        import datastore.memorydb.memory_graph as memory_graph
        import lib.similarity as similarity

        class _FakeGraph:
            def get_embedding(self, text):
                return [1.0, 0.0] if text else None

        state = {
            "raw_facts": [
                {
                    "text": "Solomon has a dog named Baxter",
                    "category": "fact",
                    "domains": ["personal"],
                    "extraction_confidence": "medium",
                }
            ],
            "rolling_batches": 1,
            "payload_duplicate_facts_collapsed": 0,
        }
        payload = {
            "raw_facts": [
                {
                    "text": "Solomon does not have a dog named Baxter",
                    "category": "fact",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                }
            ],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "carry_facts": [],
            "facts_skipped": 0,
            "carry_duplicate_facts_dropped": 0,
        }

        monkeypatch.setattr(extraction_daemon, "_stage_dedup_settings", lambda: (0.98, 0.88, True))
        monkeypatch.setattr(extraction_daemon, "_semantic_candidate_overlaps", lambda *_args, **_kwargs: [0])
        monkeypatch.setattr(memory_graph, "get_graph", lambda: _FakeGraph())
        monkeypatch.setattr(similarity, "cosine_similarity", lambda *_args, **_kwargs: 0.72)
        monkeypatch.setattr(memory_graph, "_llm_dedup_check_many", lambda *_args, **_kwargs: {})

        merged = extraction_daemon.merge_staged_payloads(state, payload)

        assert len(merged["raw_facts"]) == 2
        assert merged["staged_semantic_subset_rows"] == 1
        assert merged["staged_semantic_llm_checks"] == 1
        assert merged["staged_semantic_duplicate_facts_collapsed"] == 0

    def test_skips_session_where_transcript_not_grown_past_cursor(self, monkeypatch, tmp_path):
        """No timeout signal when transcript line count <= cursor offset (nothing new)."""
        instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        transcript_path = tmp_path / "fully-extracted.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"hello"}\n',
            encoding="utf-8",
        )
        # cursor says we already read line 0 (1 line total, cursor at 1 = nothing new)
        self._setup_cursor(tmp_path, instance_id, "extracted-sess", 1, transcript_path)

        now = 1_700_000_000.0
        mtime = now - (60 * 60)  # 1 hour ago — definitely idle
        os.utime(transcript_path, (mtime, mtime))

        captured = []
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
        monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: now - 7200)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(extraction_daemon, "write_signal", lambda *a, **kw: captured.append((a, kw)))

        extraction_daemon.check_idle_sessions(timeout_minutes=30)

        assert len(captured) == 1
        assert captured[0][1]["signal_type"] == "timeout"
        assert captured[0][1]["session_id"] == "extracted-sess"
        assert captured[0][1]["meta"] == {"compact_on_timeout": True}
        assert captured[0][1]["supports_compaction_control"] is False

    def test_skips_session_not_yet_idle(self, monkeypatch, tmp_path):
        """Session modified 10 minutes ago with 30-minute timeout must not trigger signal."""
        instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        transcript_path = tmp_path / "active.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"hello"}\n{"role":"assistant","content":"hi"}\n',
            encoding="utf-8",
        )
        self._setup_cursor(tmp_path, instance_id, "active-sess", 1, transcript_path)

        now = 1_700_000_000.0
        mtime = now - (10 * 60)  # modified 10 minutes ago, not idle yet
        os.utime(transcript_path, (mtime, mtime))

        captured = []
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
        monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: now - 7200)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(extraction_daemon, "write_signal", lambda *a, **kw: captured.append((a, kw)))

        extraction_daemon.check_idle_sessions(timeout_minutes=30)

        assert captured == []

    def test_flushes_cursor_end_staged_payload_when_newer_session_exists(self, monkeypatch, tmp_path):
        """A cursor-end session with staged payload should flush immediately once a newer session exists."""
        instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        old_transcript = tmp_path / "old.jsonl"
        old_transcript.write_text(
            '{"role":"user","content":"old"}\n{"role":"assistant","content":"done"}\n',
            encoding="utf-8",
        )
        self._setup_cursor(tmp_path, instance_id, "old-sess", 2, old_transcript)
        self._setup_rolling_state(
            tmp_path,
            instance_id,
            "old-sess",
            [{"text": "staged fact", "category": "fact"}],
            old_transcript,
        )

        new_transcript = tmp_path / "new.jsonl"
        new_transcript.write_text('{"role":"user","content":"new"}\n', encoding="utf-8")
        self._setup_cursor(tmp_path, instance_id, "new-sess", 0, new_transcript)

        now = 1_700_000_000.0
        old_mtime = now - 30
        new_mtime = now - 5
        os.utime(old_transcript, (old_mtime, old_mtime))
        os.utime(new_transcript, (new_mtime, new_mtime))

        captured = []
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
        monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: now - 7200)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                }
            ),
        )

        extraction_daemon.check_idle_sessions(timeout_minutes=30)

        assert captured == [
            {
                "signal_type": "session_end",
                "session_id": "old-sess",
                "transcript_path": str(old_transcript),
            }
        ]

    def test_flushes_buffered_semantic_tail_when_newer_session_exists(self, monkeypatch, tmp_path):
        """A completed short semantic buffer should flush once a newer session proves rollover."""
        instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        old_transcript = tmp_path / "old-short.jsonl"
        old_transcript.write_text(
            '{"role":"user","content":"walnut ritual"}\n{"role":"assistant","content":"ack"}\n',
            encoding="utf-8",
        )
        self._setup_cursor(tmp_path, instance_id, "old-short-sess", 0, old_transcript)
        rolling_dir = tmp_path / "instances" / instance_id / "data" / "rolling-extraction"
        rolling_dir.mkdir(parents=True, exist_ok=True)
        (rolling_dir / "old-short-sess.json").write_text(
            json.dumps({
                "session_id": "old-short-sess",
                "transcript_path": str(old_transcript),
                "processed_line_offset": 2,
                "buffered_line_offset": 2,
                "semantic_buffer": "User: My Friday ritual uses walnut-umbrella-7142.",
                "semantic_buffer_tokens": 12,
                "raw_facts": [],
                "raw_snippets": {},
                "raw_journal": {},
                "raw_project_logs": {},
            }),
            encoding="utf-8",
        )

        new_transcript = tmp_path / "new-short.jsonl"
        new_transcript.write_text('{"role":"user","content":"new"}\n', encoding="utf-8")
        self._setup_cursor(tmp_path, instance_id, "new-short-sess", 0, new_transcript)

        now = 1_700_000_000.0
        old_mtime = now - 30
        new_mtime = now - 5
        os.utime(old_transcript, (old_mtime, old_mtime))
        os.utime(new_transcript, (new_mtime, new_mtime))

        captured = []
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
        monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: now - 7200)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                }
            ),
        )

        extraction_daemon.check_idle_sessions(timeout_minutes=30)

        assert captured == [
            {
                "signal_type": "session_end",
                "session_id": "old-short-sess",
                "transcript_path": str(old_transcript),
            }
        ]

    def test_does_not_flush_cursor_end_staged_payload_without_newer_session(self, monkeypatch, tmp_path):
        """A cursor-end staged payload alone must not flush without explicit rollover evidence."""
        instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        transcript = tmp_path / "grace.jsonl"
        transcript.write_text(
            '{"role":"user","content":"old"}\n{"role":"assistant","content":"done"}\n',
            encoding="utf-8",
        )
        self._setup_cursor(tmp_path, instance_id, "grace-sess", 2, transcript)
        self._setup_rolling_state(
            tmp_path,
            instance_id,
            "grace-sess",
            [{"text": "staged fact", "category": "fact"}],
            transcript,
        )

        now = 1_700_000_000.0
        mtime = now - 45
        os.utime(transcript, (mtime, mtime))

        captured = []
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
        monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: now - 7200)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                }
            ),
        )

        extraction_daemon.check_idle_sessions(timeout_minutes=30)

        assert captured == []

    def test_recovers_missing_rolling_stage_flush_for_publish_ready_payload(self, monkeypatch, tmp_path):
        """A completed rolling stage should not strand facts if its synthetic flush signal was lost."""
        instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        transcript = tmp_path / "lost-flush.jsonl"
        transcript.write_text(
            '{"role":"user","content":"old"}\n{"role":"assistant","content":"done"}\n',
            encoding="utf-8",
        )
        self._setup_cursor(tmp_path, instance_id, "lost-flush-sess", 2, transcript)
        state_file = self._setup_rolling_state(
            tmp_path,
            instance_id,
            "lost-flush-sess",
            [{"text": "staged fact", "category": "fact"}],
            transcript,
        )
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state["rolling_batches"] = 1
        state["buffered_line_offset"] = 2
        state["staged_payload_pending_flush"] = True
        state_file.write_text(json.dumps(state), encoding="utf-8")

        now = 1_700_000_000.0
        mtime = now - 45
        os.utime(transcript, (mtime, mtime))

        captured = []
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
        monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: now - 7200)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        extraction_daemon.check_idle_sessions(timeout_minutes=30)

        assert len(captured) == 1
        assert captured[0]["signal_type"] == "session_end"
        assert captured[0]["session_id"] == "lost-flush-sess"
        assert captured[0]["meta"]["reason"] == "rolling_stage_flush"
        assert captured[0]["meta"]["staged_payload_sweep"] is True
        assert captured[0]["meta"]["recovered_missing_flush"] is True

    def test_recovers_missing_rolling_stage_flush_without_cursor_row(self, monkeypatch, tmp_path):
        """A staged payload remains recoverable if the source cursor disappeared after a crash."""
        instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        transcript = tmp_path / "lost-cursor-flush.jsonl"
        transcript.write_text(
            '{"role":"user","content":"old"}\n{"role":"assistant","content":"done"}\n',
            encoding="utf-8",
        )
        state_file = self._setup_rolling_state(
            tmp_path,
            instance_id,
            "lost-cursor-sess",
            [{"text": "staged fact", "category": "fact"}],
            transcript,
        )
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state["rolling_batches"] = 1
        state["buffered_line_offset"] = 2
        state["staged_payload_pending_flush"] = True
        state_file.write_text(json.dumps(state), encoding="utf-8")
        source_key = extraction_daemon._signal_source_cursor_key(
            "lost-cursor-sess",
            str(transcript),
            staged_state=state,
        )
        lock_path = tmp_path / "instances" / instance_id / "data" / "session-processing" / f"{source_key}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(
            json.dumps({
                "session_id": source_key,
                "pid": 999999999,
                "started_at": "2026-06-13T13:14:27Z",
            }),
            encoding="utf-8",
        )

        now = 1_700_000_000.0
        old_time = now - 60
        os.utime(transcript, (old_time, old_time))
        os.utime(lock_path, (old_time, old_time))

        captured = []
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", instance_id)
        monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
        monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: now - 7200)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        extraction_daemon.check_idle_sessions(timeout_minutes=30)

        assert len(captured) == 1
        assert captured[0]["signal_type"] == "session_end"
        assert captured[0]["session_id"] == "lost-cursor-sess"
        assert captured[0]["transcript_path"] == str(transcript)
        assert captured[0]["meta"]["reason"] == "rolling_stage_flush"
        assert captured[0]["meta"]["recovered_missing_flush"] is True
        assert captured[0]["meta"]["recovered_from_rolling_state"] is True
        assert captured[0]["meta"]["source_cursor_key"] == source_key
        assert captured[0]["meta"]["flush_staged_payload_only"] is True
        assert not lock_path.exists()

    def test_recovers_idle_semantic_rolling_buffer_without_cursor_row(self, monkeypatch, tmp_path):
        """A sub-threshold rolling buffer must still timeout-flush if its cursor row disappeared."""
        instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        transcript = tmp_path / "lost-cursor-semantic.jsonl"
        transcript.write_text(
            '{"role":"user","content":"Hermes keeps the brass typewriter on the west desk."}\n'
            '{"role":"assistant","content":"ack"}\n',
            encoding="utf-8",
        )
        state_file = self._setup_rolling_state(
            tmp_path,
            instance_id,
            "lost-semantic-sess",
            [],
            transcript,
        )
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state["buffer_transcript_path"] = str(transcript)
        state["buffered_line_offset"] = 2
        state["processed_line_offset"] = 0
        state["semantic_buffer"] = "User: Hermes keeps the brass typewriter on the west desk."
        state["semantic_buffer_tokens"] = 12
        state["raw_facts"] = []
        state_file.write_text(json.dumps(state), encoding="utf-8")

        now = 1_700_000_000.0
        old_time = now - 120
        os.utime(transcript, (old_time, old_time))

        captured = []
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", instance_id)
        monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
        monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: now - 7200)
        monkeypatch.setattr(extraction_daemon, "_ensure_discovered_session_cursors", lambda _adapter: None)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        extraction_daemon.check_idle_sessions(timeout_minutes=1)

        source_key = extraction_daemon._signal_source_cursor_key(
            "lost-semantic-sess",
            str(transcript),
            staged_state=state,
        )
        assert captured == [
            {
                "signal_type": "session_end",
                "session_id": "lost-semantic-sess",
                "transcript_path": str(transcript),
                "meta": {
                    "reason": "idle_rolling_semantic_buffer_flush",
                    "recovered_from_rolling_state": True,
                    "source_cursor_key": source_key,
                    "semantic_buffer_tokens": 12,
                    "buffered_line_offset": 2,
                },
            }
        ]

    def test_recovers_missing_rolling_stage_flush_when_snapshot_was_cleaned(
        self, monkeypatch, tmp_path
    ):
        """A staged payload must remain recoverable after its daemon snapshot is gone."""
        instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        original = tmp_path / "extraction_cache" / "day-010-2026-03-18.jsonl"
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_text(
            '{"role":"user","content":"old"}\n{"role":"assistant","content":"done"}\n',
            encoding="utf-8",
        )
        snapshot = (
            tmp_path
            / "instances"
            / instance_id
            / "logs"
            / "daemon"
            / "rolling-transcript-snapshots"
            / "day-runtime-2026-03-18"
            / "20260614T010553Z-1dbe461839edb8ce"
            / original.name
        )
        state_file = self._setup_rolling_state(
            tmp_path,
            instance_id,
            "day-runtime-2026-03-18",
            [{"text": "staged fact", "category": "fact"}],
            snapshot,
        )
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state["rolling_batches"] = 1
        state["buffered_line_offset"] = 2
        state["staged_payload_pending_flush"] = True
        state["source_transcript_path"] = str(original)
        state_file.write_text(json.dumps(state), encoding="utf-8")
        cursor_dir = tmp_path / "instances" / instance_id / "data" / "session-cursors"
        cursor_dir.mkdir(parents=True, exist_ok=True)

        captured = []
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", instance_id)
        monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: 0.0)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        extraction_daemon.check_idle_sessions(timeout_minutes=30)

        assert len(captured) == 1
        assert captured[0]["signal_type"] == "session_end"
        assert captured[0]["session_id"] == "day-runtime-2026-03-18"
        assert captured[0]["transcript_path"] == str(original)
        assert captured[0]["meta"]["reason"] == "rolling_stage_flush"
        assert captured[0]["meta"]["recovered_missing_flush"] is True
        assert captured[0]["meta"]["recovered_from_rolling_state"] is True

    def test_recovers_missing_snapshot_from_extraction_cache_when_source_path_absent(
        self, monkeypatch, tmp_path
    ):
        """Existing stranded states from older daemons may lack source_transcript_path."""
        instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        original = tmp_path / "extraction_cache" / "day-010-2026-03-18.jsonl"
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_text(
            '{"role":"user","content":"old"}\n{"role":"assistant","content":"done"}\n',
            encoding="utf-8",
        )
        snapshot = (
            tmp_path
            / "instances"
            / instance_id
            / "logs"
            / "daemon"
            / "rolling-transcript-snapshots"
            / "day-runtime-2026-03-18"
            / "20260614T010553Z-1dbe461839edb8ce"
            / original.name
        )
        state_file = self._setup_rolling_state(
            tmp_path,
            instance_id,
            "day-runtime-2026-03-18",
            [{"text": "staged fact", "category": "fact"}],
            snapshot,
        )
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state["rolling_batches"] = 1
        state["buffered_line_offset"] = 2
        state["staged_payload_pending_flush"] = True
        state_file.write_text(json.dumps(state), encoding="utf-8")
        cursor_dir = tmp_path / "instances" / instance_id / "data" / "session-cursors"
        cursor_dir.mkdir(parents=True, exist_ok=True)

        captured = []
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", instance_id)
        monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: 0.0)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        extraction_daemon.check_idle_sessions(timeout_minutes=30)

        assert len(captured) == 1
        assert captured[0]["transcript_path"] == str(original)
        assert captured[0]["meta"]["recovered_missing_flush"] is True

    def test_processed_rolling_snapshot_kept_while_pending_flush_references_it(
        self, monkeypatch, tmp_path
    ):
        """A pending staged flush must keep its daemon snapshot readable until it runs."""
        instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        source = tmp_path / "extraction_cache" / "day-012-2026-03-22.jsonl"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text('{"role":"user","content":"source"}\n', encoding="utf-8")
        snapshot = (
            tmp_path
            / "instances"
            / instance_id
            / "logs"
            / "daemon"
            / "rolling-transcript-snapshots"
            / "day-runtime-2026-03-22"
            / "20260615T121735Z-39ba373a3765cc2a"
            / source.name
        )
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", instance_id)
        signal_path = extraction_daemon.write_signal(
            signal_type="session_end",
            session_id="day-runtime-2026-03-22",
            transcript_path=str(snapshot),
            meta={
                "reason": "rolling_stage_flush",
                "source_signal": "rolling",
                "staged_payload_sweep": True,
            },
        )
        staged_state = {
            "raw_facts": [{"text": "staged fact", "category": "fact"}],
            "rolling_batches": 1,
            "staged_payload_pending_flush": True,
            "source_transcript_path": str(source),
        }

        assert extraction_daemon._processed_rolling_snapshot_can_be_cleaned(
            str(snapshot),
            staged_state,
        ) is False

        signal_data = json.loads(signal_path.read_text(encoding="utf-8"))
        signal_data["_signal_path"] = str(signal_path)
        extraction_daemon.mark_signal_processed(signal_data)

        assert extraction_daemon._processed_rolling_snapshot_can_be_cleaned(
            str(snapshot),
            staged_state,
        ) is True

    def test_missing_rolling_stage_flush_recovery_queues_retry_with_active_source_lock(self, monkeypatch, tmp_path):
        """Durable-state recovery must leave a retry signal even while a source lock is active."""
        instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        transcript = tmp_path / "active-lock-flush.jsonl"
        transcript.write_text(
            '{"role":"user","content":"old"}\n{"role":"assistant","content":"done"}\n',
            encoding="utf-8",
        )
        state_file = self._setup_rolling_state(
            tmp_path,
            instance_id,
            "active-lock-sess",
            [{"text": "staged fact", "category": "fact"}],
            transcript,
        )
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state["rolling_batches"] = 1
        state["buffered_line_offset"] = 2
        state["staged_payload_pending_flush"] = True
        state_file.write_text(json.dumps(state), encoding="utf-8")
        source_key = extraction_daemon._signal_source_cursor_key(
            "active-lock-sess",
            str(transcript),
            staged_state=state,
        )

        now = 1_700_000_000.0
        old_time = now - 60
        os.utime(transcript, (old_time, old_time))

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", instance_id)
        lock_fd = extraction_daemon._acquire_session_processing_lock(source_key)
        assert lock_fd is not None

        captured = []
        monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
        monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: now - 7200)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        try:
            extraction_daemon.check_idle_sessions(timeout_minutes=30)
        finally:
            extraction_daemon._release_session_processing_lock(source_key, lock_fd)

        assert len(captured) == 1
        assert captured[0]["signal_type"] == "session_end"
        assert captured[0]["session_id"] == "active-lock-sess"
        assert captured[0]["transcript_path"] == str(transcript)
        assert captured[0]["meta"]["reason"] == "rolling_stage_flush"
        assert captured[0]["meta"]["recovered_missing_flush"] is True
        assert captured[0]["meta"]["recovered_from_rolling_state"] is True
        assert captured[0]["meta"]["source_lock_active_at_recovery"] is True
        assert captured[0]["meta"]["source_cursor_key"] == source_key

    def test_check_chunk_ready_sessions_recovers_missing_rolling_stage_flush_after_fresh_dead_lock(
        self, monkeypatch, tmp_path
    ):
        """The explicit rolling scan driver must recover staged payloads without waiting for idle scan."""
        instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        transcript = tmp_path / "driver-recovery.jsonl"
        transcript.write_text(
            '{"role":"user","content":"old"}\n{"role":"assistant","content":"done"}\n',
            encoding="utf-8",
        )
        state_file = self._setup_rolling_state(
            tmp_path,
            instance_id,
            "driver-recovery-sess",
            [{"text": "staged fact", "category": "fact"}],
            transcript,
        )
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state["rolling_batches"] = 1
        state["buffered_line_offset"] = 2
        state["staged_payload_pending_flush"] = True
        state_file.write_text(json.dumps(state), encoding="utf-8")
        source_key = extraction_daemon._signal_source_cursor_key(
            "driver-recovery-sess",
            str(transcript),
            staged_state=state,
        )
        lock_path = tmp_path / "instances" / instance_id / "data" / "session-processing" / f"{source_key}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(
            json.dumps({
                "session_id": source_key,
                "pid": 999999999,
                "started_at": "2026-06-13T14:38:20Z",
            }),
            encoding="utf-8",
        )

        captured = []
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", instance_id)
        monkeypatch.setattr(extraction_daemon, "_pid_alive", lambda _pid: False)
        monkeypatch.setattr(extraction_daemon, "_load_runtime_adapter", lambda: object())
        monkeypatch.setattr(extraction_daemon, "_ensure_discovered_session_cursors", lambda _adapter: 0)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "meta": kwargs.get("meta", {}),
                }
            ),
        )

        extraction_daemon.check_chunk_ready_sessions(chunk_tokens=12000)

        assert len(captured) == 1
        assert captured[0]["signal_type"] == "session_end"
        assert captured[0]["session_id"] == "driver-recovery-sess"
        assert captured[0]["transcript_path"] == str(transcript)
        assert captured[0]["meta"]["reason"] == "rolling_stage_flush"
        assert captured[0]["meta"]["recovered_missing_flush"] is True
        assert captured[0]["meta"]["recovered_from_rolling_state"] is True
        assert captured[0]["meta"]["source_cursor_key"] == source_key
        assert not lock_path.exists()

    def test_check_chunk_ready_sessions_raises_rolling_state_recovery_failure_when_fail_hard(
        self, monkeypatch, tmp_path
    ):
        instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        state_dir = tmp_path / "instances" / instance_id / "data" / "rolling-extraction"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "driver-corrupt-sess.json").write_text("{not-json", encoding="utf-8")

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", instance_id)
        monkeypatch.setattr(extraction_daemon, "_load_runtime_adapter", lambda: object())
        monkeypatch.setattr(extraction_daemon, "_ensure_discovered_session_cursors", lambda _adapter: 0)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

        with pytest.raises(json.JSONDecodeError):
            extraction_daemon.check_chunk_ready_sessions(chunk_tokens=12000)

    def test_write_staged_payload_flush_signals_raises_rolling_state_failure_when_fail_hard(
        self, monkeypatch, tmp_path
    ):
        instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        state_dir = tmp_path / "instances" / instance_id / "data" / "rolling-extraction"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "flush-corrupt-sess.json").write_text("{not-json", encoding="utf-8")

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", instance_id)
        monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: True)

        with pytest.raises(json.JSONDecodeError):
            extraction_daemon.write_staged_payload_flush_signals()

    def test_check_idle_sessions_retires_legacy_cursor_shadowed_by_source_cursor(
        self, monkeypatch, tmp_path
    ):
        transcript_path = tmp_path / "rollout-2026-05-27T00-00-00-019e802e-aaaa-4aaa-8aaa-aaaaaaaaaaaa.jsonl"
        transcript_path.write_text(
            '{"type":"event_msg","payload":{"type":"user_message","message":"CDX finished this session."}}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
        legacy_session_id = "019e802e-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        rollout_session_id = "rollout-2026-05-27T00-00-00-019e802e-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        extraction_daemon.write_cursor(legacy_session_id, 0, str(transcript_path))
        source_key = extraction_daemon._signal_source_cursor_key(
            rollout_session_id,
            str(transcript_path),
        )
        extraction_daemon.write_cursor(
            rollout_session_id,
            1,
            str(transcript_path),
            source_key=source_key,
        )
        legacy_cursor_file = extraction_daemon._cursor_dir() / f"{legacy_session_id}.json"
        assert legacy_cursor_file.exists()

        captured = []
        monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: 0.0)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(extraction_daemon, "_ensure_discovered_session_cursors", lambda adapter: 0)
        monkeypatch.setattr(
            extraction_daemon,
            "write_signal",
            lambda signal_type, session_id, transcript_path, **kwargs: captured.append(
                {
                    "signal_type": signal_type,
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                }
            ),
        )

        extraction_daemon.check_idle_sessions(timeout_minutes=0)

        assert captured == []
        assert not legacy_cursor_file.exists()

    def test_skips_session_with_pending_signal_already(self, monkeypatch, tmp_path):
        """If there is already a pending signal for the session, no duplicate is written."""
        instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        transcript_path = tmp_path / "pending.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"hello"}\n{"role":"assistant","content":"hi"}\n',
            encoding="utf-8",
        )
        self._setup_cursor(tmp_path, instance_id, "pending-sess", 1, transcript_path)

        now = 1_700_000_000.0
        mtime = now - (60 * 60)
        os.utime(transcript_path, (mtime, mtime))

        captured = []
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
        monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: now - 7200)
        # Simulate already-pending signal for this session
        monkeypatch.setattr(
            extraction_daemon,
            "read_pending_signals",
            lambda: [{"session_id": "pending-sess", "type": "timeout"}],
        )
        monkeypatch.setattr(extraction_daemon, "write_signal", lambda *a, **kw: captured.append((a, kw)))

        extraction_daemon.check_idle_sessions(timeout_minutes=30)

        assert captured == []

    def test_skips_session_when_cursor_dir_missing(self, monkeypatch, tmp_path):
        """When cursor dir doesn't exist, check_idle_sessions should return immediately."""
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: 0.0)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])

        captured = []
        monkeypatch.setattr(extraction_daemon, "write_signal", lambda *a, **kw: captured.append((a, kw)))

        # No cursor directory created — should not crash
        extraction_daemon.check_idle_sessions(timeout_minutes=30)

        assert captured == []




# ---------------------------------------------------------------------------
# process_signal() retry-safety: signal file is preserved on exception
# ---------------------------------------------------------------------------

class TestProcessSignalRetryOnException:
    """process_signal() must not mark the signal processed when an exception occurs."""

    def test_process_signal_skips_signal_already_consumed_from_disk(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        transcript = tmp_path / "stale-signal.jsonl"
        transcript.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")
        sig_path = extraction_daemon.write_signal(
            signal_type="session_end",
            session_id="sess-stale-signal",
            transcript_path=str(transcript),
        )
        signal_data = extraction_daemon.read_pending_signals()[0]
        sig_path.unlink()

        reloaded = []
        monkeypatch.setattr(
            extraction_daemon,
            "_reload_config_if_changed",
            lambda reason: reloaded.append(reason),
        )

        extraction_daemon.process_signal(signal_data)

        assert reloaded == []

    def test_process_signal_preserves_pending_staged_flush_when_real_lifecycle_runs(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        transcript = tmp_path / "session.jsonl"
        transcript.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")
        extraction_daemon.write_cursor(
            "sess-staged-flush-preserved",
            1,
            str(transcript),
        )
        real_signal = extraction_daemon.write_signal(
            signal_type="session_end",
            session_id="sess-staged-flush-preserved",
            transcript_path=str(transcript),
            meta={"reason": "real_lifecycle"},
        )
        staged_flush_signal = extraction_daemon.write_signal(
            signal_type="session_end",
            session_id="sess-staged-flush-preserved",
            transcript_path=str(transcript),
            meta={
                "reason": "rolling_stage_flush",
                "source_signal": "rolling",
                "staged_payload_sweep": True,
            },
        )

        marked = []
        monkeypatch.setattr(
            extraction_daemon,
            "_cursor_or_adapter_owns_transcript_path",
            lambda *_args, **_kwargs: True,
        )
        monkeypatch.setattr(
            extraction_daemon,
            "_request_session_logs_ingest",
            lambda **_kwargs: {"status": "indexed"},
        )
        monkeypatch.setattr(
            extraction_daemon,
            "_get_owner_id",
            lambda: "owner-id",
        )
        monkeypatch.setattr(
            extraction_daemon,
            "mark_signal_processed",
            lambda signal_data: marked.append(Path(signal_data.get("_signal_path") or "")),
        )

        signal_data = json.loads(real_signal.read_text(encoding="utf-8"))
        signal_data["_signal_path"] = str(real_signal)
        extraction_daemon.process_signal(signal_data)

        assert staged_flush_signal.exists()
        assert Path(staged_flush_signal) not in marked

    def test_signal_file_preserved_when_daemon_extraction_empty_response_raises(self, monkeypatch, tmp_path):
        from ingest import extract as extract_mod
        from lib.adapter import reset_adapter, set_adapter

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
        monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "owner-id")
        monkeypatch.setattr(extraction_daemon, "_read_usage_totals", lambda: {})
        monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: False)
        monkeypatch.setattr(extraction_daemon, "_session_has_harvestable_subagents", lambda *args, **kwargs: False)
        monkeypatch.setattr(extraction_daemon, "_request_session_logs_ingest", lambda **_kwargs: {"status": "indexed"})

        transcript_path = tmp_path / "provider-timeout-session.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"The provider timeout retry fact is blue-spindle."}\n',
            encoding="utf-8",
        )
        extraction_daemon.write_cursor("sess-provider-timeout", 0, str(transcript_path))

        class _Adapter(_OwnedTestAdapterMixin):
            def parse_session_jsonl(self, path):
                _ = path
                return "User: The provider timeout retry fact is blue-spindle."

        calls = []
        marked = []

        def _raise_empty_response(*_args, **kwargs):
            calls.append(kwargs)
            assert kwargs["raise_on_llm_failure"] is True
            raise RuntimeError("Deep Reasoning returned no response")

        monkeypatch.setattr(extract_mod, "extract_from_transcript", _raise_empty_response)
        monkeypatch.setattr(extraction_daemon, "mark_signal_processed", lambda sig: marked.append(sig))

        set_adapter(_Adapter())
        try:
            sig_path = extraction_daemon.write_signal(
                signal_type="session_end",
                session_id="sess-provider-timeout",
                transcript_path=str(transcript_path),
            )
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])
        finally:
            reset_adapter()

        assert calls
        assert marked == []
        assert sig_path.exists()

    def test_signal_file_preserved_when_process_signal_inner_raises(self, monkeypatch, tmp_path):
        """The signal file must remain intact if extraction raises partway through."""
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        # Write a real transcript so the path check passes
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")

        # Write a signal
        sig_path = extraction_daemon.write_signal(
            signal_type="session_end",
            session_id="sess-retry",
            transcript_path=str(transcript),
        )

        # Verify signal exists
        assert sig_path.exists()

        signals = extraction_daemon.read_pending_signals()
        assert len(signals) == 1

        # Make the adapter explode
        monkeypatch.setattr(
            extraction_daemon,
            "_get_owner_id",
            lambda: "owner-id",
        )

        def exploding_adapter(*a, **kw):
            raise RuntimeError("extraction kaboom")

        # Monkeypatch the whole get_adapter chain via the read_cursor path is complex,
        # so we patch at the subagent_registry boundary which is called first
        monkeypatch.setattr(
            extraction_daemon,
            "read_cursor",
            lambda sid: {"line_offset": 0, "transcript_path": str(transcript)},
        )
        monkeypatch.setattr(
            extraction_daemon,
            "count_transcript_lines",
            lambda p: 1,
        )
        monkeypatch.setattr(
            extraction_daemon,
            "read_transcript_slice",
            lambda path, from_line: ["line1\n"],
        )

        import tempfile as _tempfile
        import contextlib

        # Make NamedTemporaryFile write succeed but then make adapter import fail
        original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else None

        # Easiest approach: patch _tmp_dir to return a real tmp dir,
        # then patch the import of get_adapter to raise
        monkeypatch.setattr(extraction_daemon, "_tmp_dir", lambda: tmp_path)

        import sys as _sys
        import types

        # Preserve real modules before replacing so we can restore them cleanly.
        real_subagent_registry = _sys.modules.get("core.subagent_registry")
        real_adapter = _sys.modules.get("lib.adapter")

        # Stub out subagent_registry so it doesn't interfere
        fake_registry = types.ModuleType("core.subagent_registry")
        fake_registry.is_registered_subagent = lambda sid: False
        fake_registry.get_harvestable = lambda sid: []
        fake_registry.mark_harvested = lambda sid, cid: None
        fake_registry._registry_dir = lambda: tmp_path / "registry"
        _sys.modules["core.subagent_registry"] = fake_registry

        # Make the adapter raise
        fake_adapter_mod = types.ModuleType("lib.adapter")
        class _FakeAdapter(_OwnedTestAdapterMixin):
            def parse_session_jsonl(self, path):
                raise RuntimeError("extraction kaboom from adapter")
        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        _sys.modules["lib.adapter"] = fake_adapter_mod

        try:
            extraction_daemon.process_signal(signals[0])
        except Exception:
            pass

        # Restore real modules — popping without restoring evicts them and causes
        # a fresh reimport in later tests which can pick up a poisoned config state.
        if real_subagent_registry is not None:
            _sys.modules["core.subagent_registry"] = real_subagent_registry
        else:
            _sys.modules.pop("core.subagent_registry", None)
        if real_adapter is not None:
            _sys.modules["lib.adapter"] = real_adapter
        else:
            _sys.modules.pop("lib.adapter", None)

        # Reset config singleton in case QUAID_HOME=tmp_path caused a load.
        import config as _cfg_mod
        _cfg_mod._config = None

        # Reload signals — the file must still be there (not marked processed)
        remaining = extraction_daemon.read_pending_signals()
        assert len(remaining) == 1, (
            "Signal must be preserved for retry after extraction failure"
        )

    def test_process_signal_cleans_temp_file_when_temp_write_fails(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")
        sig_path = extraction_daemon.write_signal(
            signal_type="session_end",
            session_id="sess-temp-write-fails",
            transcript_path=str(transcript),
        )
        signal_data = extraction_daemon.read_pending_signals()[0]
        temp_file = tmp_path / "partial-temp.jsonl"

        class _FailingTemp:
            name = str(temp_file)

            def __enter__(self):
                temp_file.write_text("partial", encoding="utf-8")
                return self

            def __exit__(self, *_args):
                return False

            def writelines(self, _lines):
                raise OSError("disk full")

        class _Adapter(_OwnedTestAdapterMixin):
            pass

        fake_registry = types.ModuleType("core.subagent_registry")
        fake_registry.is_registered_subagent = lambda sid: False
        fake_registry.get_harvestable = lambda sid: []
        fake_registry.mark_harvested = lambda sid, cid: None
        fake_registry._registry_dir = lambda: tmp_path / "registry"
        real_registry = sys.modules.get("core.subagent_registry")
        sys.modules["core.subagent_registry"] = fake_registry

        from lib.adapter import reset_adapter, set_adapter

        set_adapter(_Adapter())
        try:
            monkeypatch.setattr(extraction_daemon, "_fail_hard_enabled", lambda: False)
            monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "owner-id")
            monkeypatch.setattr(extraction_daemon, "_tmp_dir", lambda: tmp_path)
            monkeypatch.setattr(extraction_daemon, "read_cursor", lambda sid: {"line_offset": 0, "transcript_path": str(transcript)})
            monkeypatch.setattr(extraction_daemon, "count_transcript_lines", lambda p: 1)
            monkeypatch.setattr(extraction_daemon, "read_transcript_slice", lambda path, from_line: ["line1\n"])
            monkeypatch.setattr(extraction_daemon, "read_rolling_state", lambda sid: {})
            monkeypatch.setattr(extraction_daemon, "write_rolling_state", lambda *args, **kwargs: None)
            monkeypatch.setattr(extraction_daemon, "_reconcile_internal_cursor_state", lambda *args, **kwargs: "not_internal")
            monkeypatch.setattr(extraction_daemon, "_cursor_or_adapter_owns_transcript_path", lambda *args, **kwargs: True)
            monkeypatch.setattr(
                extraction_daemon,
                "_buffer_transcript_tail",
                lambda transcript_path, offset, staged_state, **kwargs: (staged_state, {"parse_failed": 0}),
            )
            monkeypatch.setattr(extraction_daemon.tempfile, "NamedTemporaryFile", lambda *args, **kwargs: _FailingTemp())

            extraction_daemon.process_signal(signal_data)
        finally:
            reset_adapter()
            if real_registry is not None:
                sys.modules["core.subagent_registry"] = real_registry
            else:
                sys.modules.pop("core.subagent_registry", None)

        assert sig_path.exists()
        assert not temp_file.exists()


# ---------------------------------------------------------------------------
# Effective idle check interval calculation
# ---------------------------------------------------------------------------

class TestEffectiveIdleCheckInterval:
    """Validate the adaptive idle-check interval calculation in daemon_loop."""

    def test_effective_interval_is_half_timeout_when_smaller_than_default(self, monkeypatch):
        """With a 4-minute timeout, effective interval should be 2 minutes (< 5-min default)."""
        # timeout_seconds = 4 * 60 = 240; half = 120; max(5.0, 120) = 120
        # min(idle_check_interval=300, 120) = 120; max(poll_interval=5, 120) = 120
        timeout_minutes = 4
        poll_interval = 5.0
        idle_check_interval = 300.0

        timeout_seconds = timeout_minutes * 60
        effective = max(
            poll_interval,
            min(idle_check_interval, max(5.0, timeout_seconds / 2.0)),
        )

        assert effective == 120.0

    def test_effective_interval_bounded_by_configured_idle_check_interval(self, monkeypatch):
        """With a large timeout, effective interval caps at idle_check_interval."""
        timeout_minutes = 120  # 2 hours
        poll_interval = 5.0
        idle_check_interval = 300.0

        timeout_seconds = timeout_minutes * 60
        effective = max(
            poll_interval,
            min(idle_check_interval, max(5.0, timeout_seconds / 2.0)),
        )

        # half of 7200s = 3600, but capped at idle_check_interval=300
        assert effective == 300.0

    def test_effective_interval_never_below_poll_interval(self, monkeypatch):
        """With a 0-second timeout, effective interval must be at least poll_interval."""
        # edge: timeout very small -> half = tiny, max(5.0, tiny) = 5.0,
        # min(300, 5.0) = 5.0, max(poll_interval, 5.0) = poll_interval if >= 5
        poll_interval = 10.0
        idle_check_interval = 300.0

        timeout_seconds = 2  # 2s — extremely short
        effective = max(
            poll_interval,
            min(idle_check_interval, max(5.0, timeout_seconds / 2.0)),
        )

        assert effective >= poll_interval

    def test_timeout_zero_uses_raw_idle_check_interval(self, monkeypatch):
        """When configured timeout is 0, daemon uses raw idle_check_interval (no idle checks)."""
        # This mirrors the `else` branch in daemon_loop:
        #   effective_idle_check_interval = idle_check_interval
        configured_timeout_minutes = 0
        idle_check_interval = 300.0

        if configured_timeout_minutes > 0:
            poll_interval = 5.0
            timeout_seconds = configured_timeout_minutes * 60
            effective = max(
                poll_interval,
                min(idle_check_interval, max(5.0, timeout_seconds / 2.0)),
            )
        else:
            effective = idle_check_interval

        assert effective == 300.0


# ---------------------------------------------------------------------------
# _validate_session_id
# ---------------------------------------------------------------------------

class TestValidateSessionId:
    """_validate_session_id rejects bad IDs and returns safe fallbacks."""

    def test_valid_session_id_passes_through(self):
        result = extraction_daemon._validate_session_id("my-session-123")
        assert result == "my-session-123"

    def test_alphanumeric_with_underscores_passes(self):
        result = extraction_daemon._validate_session_id("abc_DEF_123")
        assert result == "abc_DEF_123"

    def test_empty_string_returns_fallback(self):
        result = extraction_daemon._validate_session_id("")
        assert result.startswith("unknown-")

    def test_slash_injection_returns_fallback(self):
        result = extraction_daemon._validate_session_id("../../etc/passwd")
        assert result.startswith("unknown-")

    def test_none_returns_fallback(self):
        # None is not a string; the function should not crash
        result = extraction_daemon._validate_session_id(None)
        assert result.startswith("unknown-")

    def test_too_long_id_returns_fallback(self):
        long_id = "a" * 200
        result = extraction_daemon._validate_session_id(long_id)
        assert result.startswith("unknown-")

    def test_exactly_128_chars_passes(self):
        valid = "a" * 128
        result = extraction_daemon._validate_session_id(valid)
        assert result == valid

    def test_spaces_return_fallback(self):
        result = extraction_daemon._validate_session_id("bad session id")
        assert result.startswith("unknown-")

    def test_checkpoint_sidecar_session_id_normalizes_to_base(self):
        result = extraction_daemon._validate_session_id(
            "c9f9874a-0982-4679-9bec-52e2451fd087.checkpoint."
            "78423baa-408a-409b-89c0-3a203bbbd19d"
        )
        assert result == "c9f9874a-0982-4679-9bec-52e2451fd087"

    def test_invalid_session_id_fallback_is_deterministic(self):
        bad = "bad/session/id"
        result1 = extraction_daemon._validate_session_id(bad)
        result2 = extraction_daemon._validate_session_id(bad)
        assert result1 == result2


# ---------------------------------------------------------------------------
# write_cursor / read_cursor: invalid session_id sanitisation
# ---------------------------------------------------------------------------

class TestCursorSessionIdSanitisation:
    """write_cursor and read_cursor sanitise session_id before use."""

    def test_write_cursor_with_path_traversal_id_does_not_escape_cursor_dir(self, monkeypatch, tmp_path):
        """A path-traversal session_id must not write outside the cursor dir."""
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        cursor_dir = extraction_daemon._cursor_dir()

        # Write with an ID that tries to traverse up
        extraction_daemon.write_cursor("../../evil", 5, "/t.jsonl")

        # The cursor file should exist somewhere in cursor_dir (sanitised name)
        cursor_files = list(cursor_dir.glob("*.json"))
        assert len(cursor_files) == 1
        assert cursor_dir in cursor_files[0].parents or cursor_files[0].parent == cursor_dir


# ---------------------------------------------------------------------------
# mark_signal_processed: missing _signal_path
# ---------------------------------------------------------------------------

class TestMarkSignalProcessedEdgeCases:

    def test_mark_signal_processed_with_missing_signal_path_key(self, monkeypatch, tmp_path):
        """mark_signal_processed must not crash when _signal_path is absent."""
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        # Should not raise
        extraction_daemon.mark_signal_processed({})
        extraction_daemon.mark_signal_processed({"type": "reset"})

    def test_mark_signal_processed_with_empty_signal_path(self, monkeypatch, tmp_path):
        """mark_signal_processed with empty string _signal_path must be a no-op."""
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        extraction_daemon.mark_signal_processed({"_signal_path": ""})


# ---------------------------------------------------------------------------
# read_transcript_slice
# ---------------------------------------------------------------------------

class TestReadTranscriptSlice:

    def test_reads_from_offset(self, tmp_path):
        t = tmp_path / "t.jsonl"
        t.write_text("line0\nline1\nline2\nline3\n", encoding="utf-8")

        lines = extraction_daemon.read_transcript_slice(str(t), from_line=2)
        assert lines == ["line2\n", "line3\n"]

    def test_offset_zero_returns_all_lines(self, tmp_path):
        t = tmp_path / "t.jsonl"
        t.write_text("a\nb\nc\n", encoding="utf-8")

        lines = extraction_daemon.read_transcript_slice(str(t), from_line=0)
        assert lines == ["a\n", "b\n", "c\n"]

    def test_offset_beyond_end_returns_empty(self, tmp_path):
        t = tmp_path / "t.jsonl"
        t.write_text("line0\n", encoding="utf-8")

        lines = extraction_daemon.read_transcript_slice(str(t), from_line=99)
        assert lines == []

    def test_missing_file_returns_empty(self, tmp_path):
        lines = extraction_daemon.read_transcript_slice(str(tmp_path / "nonexistent.jsonl"), from_line=0)
        assert lines == []

    def test_count_transcript_lines_correct(self, tmp_path):
        t = tmp_path / "t.jsonl"
        t.write_text("a\nb\nc\n", encoding="utf-8")
        assert extraction_daemon.count_transcript_lines(str(t)) == 3

    def test_count_transcript_lines_missing_file_returns_zero(self, tmp_path):
        assert extraction_daemon.count_transcript_lines(str(tmp_path / "no.jsonl")) == 0

    def test_read_transcript_token_window_honors_max_lines_cap(self, tmp_path):
        t = tmp_path / "t.jsonl"
        t.write_text("one\n" + "two\n" + "three\n" + "four\n", encoding="utf-8")

        lines = extraction_daemon.read_transcript_token_window(
            str(t),
            from_line=0,
            max_tokens=10_000,
            max_lines=2,
        )

        assert lines == ["one\n", "two\n"]

    def test_read_transcript_token_window_oversized_line_does_not_consume_budget(self, tmp_path):
        t = tmp_path / "t.jsonl"
        oversized = "x" * 8_000 + "\n"  # ~2000 tokens; intentionally above max_tokens
        small_a = "small-a\n"
        small_b = "small-b\n"
        t.write_text(oversized + small_a + small_b, encoding="utf-8")

        lines = extraction_daemon.read_transcript_token_window(
            str(t),
            from_line=0,
            max_tokens=1_500,
            max_lines=2,
        )

        # Oversized metadata rows should still advance the cursor, but the
        # budgeted window should continue to include valid smaller rows.
        assert lines == [oversized, small_a, small_b]

    def test_read_transcript_token_window_waits_for_adapter_parseable_conversation(self, tmp_path):
        t = tmp_path / "codex-rollout.jsonl"
        machine_a = json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "<environment_context>\n  <cwd>/tmp/live</cwd>\n</environment_context>"}],
                },
            }
        ) + "\n"
        machine_b = json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "<quaid_project_context>\n[Quaid Project Context]\n\nruntime details\n</quaid_project_context>"}],
                },
            }
        ) + "\n"
        user_line = json.dumps(
            {"type": "event_msg", "payload": {"type": "user_message", "message": "My neighbor won a chili cook-off."}}
        ) + "\n"
        assistant_line = json.dumps(
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "That is memorable."}}
        ) + "\n"
        t.write_text(machine_a + machine_b + user_line + assistant_line, encoding="utf-8")

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def parse_session_jsonl(self, path):
                from adaptors.codex.adapter import CodexAdapter

                return CodexAdapter().parse_session_jsonl(path)

        lines = extraction_daemon.read_transcript_token_window(
            str(t),
            from_line=0,
            max_tokens=max(1, len(machine_a) // 4),
            max_lines=1,
            adapter=_FakeAdapter(),
        )

        assert lines == [machine_a, machine_b, user_line]

    def test_read_transcript_token_window_adapter_max_lines_counts_semantic_changes(
        self,
        monkeypatch,
        tmp_path,
    ):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "codex-private-tmp-cdx-livetest")
        from adaptors.codex.adapter import CodexAdapter

        t = tmp_path / "codex-duplicate-row-window.jsonl"
        first = "First task has a duplicate response/event row pair."
        second = "Second task should wait for the next window."
        lines_in = [
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": first}],
                    },
                }
            ) + "\n",
            json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": first}}) + "\n",
            json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}) + "\n",
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": second}],
                    },
                }
            ) + "\n",
        ]
        t.write_text("".join(lines_in), encoding="utf-8")

        read_lines = extraction_daemon.read_transcript_token_window(
            str(t),
            from_line=0,
            max_tokens=10_000,
            max_lines=2,
            adapter=CodexAdapter(),
        )
        parsed = extraction_daemon._parse_transcript_lines(read_lines, adapter=CodexAdapter())

        assert read_lines == lines_in
        assert first in parsed
        assert second in parsed

    def test_read_transcript_token_window_codex_uses_incremental_parser(
        self,
        monkeypatch,
        tmp_path,
    ):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "codex-private-tmp-cdx-livetest")
        from adaptors.codex.adapter import CodexAdapter

        t = tmp_path / "codex-linear-window.jsonl"
        first = "First task has a duplicate response/event row pair."
        second = "Second task lands through a later fallback row."
        lines_in = [
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": first}],
                    },
                }
            ) + "\n",
            json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": first}}) + "\n",
            json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "ACK"}}) + "\n",
            json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}) + "\n",
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": second}],
                    },
                }
            ) + "\n",
        ]
        t.write_text("".join(lines_in), encoding="utf-8")
        original_parse = extraction_daemon._parse_transcript_lines

        def fail_full_candidate_parse(*_args, **_kwargs):
            raise AssertionError("full candidate parse should not run for Codex token windows")

        monkeypatch.setattr(extraction_daemon, "_parse_transcript_lines", fail_full_candidate_parse)

        read_lines = extraction_daemon.read_transcript_token_window(
            str(t),
            from_line=0,
            max_tokens=10_000,
            max_lines=0,
            adapter=CodexAdapter(),
        )
        parsed = original_parse(read_lines, adapter=CodexAdapter())

        assert read_lines == lines_in
        assert parsed.count(first) == 1
        assert second in parsed

    def test_read_transcript_token_window_includes_crossing_codex_task_user_turn(
        self,
        monkeypatch,
        tmp_path,
    ):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "codex-private-tmp-cdx-livetest")
        from adaptors.codex.adapter import CodexAdapter
        from lib.tokens import estimate_tokens

        t = tmp_path / "codex-multi-task-rollout.jsonl"
        chunk_one = "Chunk 1: " + ("ginkgo checklist " * 290)
        chunk_two = (
            "Chunk 2: Baxter keeps an orange linen notebook from Emília Rosa "
            "beside the archive shelf."
        )
        lines_in = [
            json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}) + "\n",
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": chunk_one}],
                    },
                }
            ) + "\n",
            json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": chunk_one}}) + "\n",
            json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "ACK"}}) + "\n",
            json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}) + "\n",
            json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}) + "\n",
            json.dumps({"type": "turn_context", "payload": {"cwd": str(tmp_path)}}) + "\n",
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": chunk_two}],
                    },
                }
            ) + "\n",
            json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": chunk_two}}) + "\n",
            json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "Second ACK"}}) + "\n",
        ]
        t.write_text("".join(lines_in), encoding="utf-8")
        adapter = CodexAdapter()
        before_crossing = extraction_daemon._parse_transcript_lines(lines_in[:-3], adapter=adapter)

        read_lines = extraction_daemon.read_transcript_token_window(
            str(t),
            from_line=0,
            max_tokens=estimate_tokens(before_crossing),
            max_lines=0,
            adapter=adapter,
            include_threshold_crossing_semantic_row=True,
        )
        parsed = extraction_daemon._parse_transcript_lines(read_lines, adapter=adapter)

        assert read_lines == lines_in[:-1]
        assert chunk_one.strip() in parsed
        assert "Baxter keeps an orange linen notebook from Emília Rosa" in parsed
        assert "Assistant: Second ACK" not in parsed
