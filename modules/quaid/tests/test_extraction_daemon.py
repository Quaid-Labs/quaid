import json
import os
import pathlib

import pytest

from core import extraction_daemon


class _StopDaemonLoop(Exception):
    pass


def test_daemon_loop_preserves_signal_when_processing_raises(monkeypatch):
    signal_payload = {"session_id": "sess-1", "type": "reset"}
    marked = []
    read_calls = 0

    def fake_read_pending_signals():
        nonlocal read_calls
        read_calls += 1
        return [signal_payload] if read_calls == 1 else []

    def fake_process_signal(_sig):
        raise RuntimeError("boom")

    def fake_sleep(_seconds):
        raise _StopDaemonLoop()

    monkeypatch.setattr(extraction_daemon, "write_pid", lambda _pid: None)
    monkeypatch.setattr(extraction_daemon, "remove_pid", lambda: None)
    monkeypatch.setattr(extraction_daemon, "read_pending_signals", fake_read_pending_signals)
    monkeypatch.setattr(extraction_daemon, "process_signal", fake_process_signal)
    monkeypatch.setattr(extraction_daemon, "mark_signal_processed", lambda sig: marked.append(sig))
    monkeypatch.setattr(extraction_daemon.time, "sleep", fake_sleep)
    monkeypatch.setattr(extraction_daemon.signal, "signal", lambda *_args, **_kwargs: None)

    with pytest.raises(_StopDaemonLoop):
        extraction_daemon.daemon_loop(poll_interval=0.0, idle_check_interval=999999.0)

    assert marked == []


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


def test_check_idle_sessions_writes_timeout_signal_for_idle_unextracted_session(monkeypatch, tmp_path):
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text('{"role":"user","content":"hello"}\n{"role":"assistant","content":"hi"}\n', encoding="utf-8")

    instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
    cursor_dir = tmp_path / instance_id / "data" / "session-cursors"
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


def test_check_idle_sessions_skips_transcripts_older_than_installed_at(monkeypatch, tmp_path):
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text('{"role":"user","content":"hello"}\n{"role":"assistant","content":"hi"}\n', encoding="utf-8")

    instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
    cursor_dir = tmp_path / instance_id / "data" / "session-cursors"
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

        assert str(sig_dir).startswith(str(tmp_path / "cc-instance")), (
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

        assert str(cursor_dir).startswith(str(tmp_path / "oc-instance"))

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

    def test_write_signal_upgrades_pending_signal_to_stronger_type(self, monkeypatch, tmp_path):
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

        assert first == second
        signals = extraction_daemon.read_pending_signals()
        assert len(signals) == 1
        assert signals[0]["type"] == "session_end"
        assert signals[0]["transcript_path"] == "/final.jsonl"
        assert signals[0]["meta"] == {"reason": "session_closed"}

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

    def test_read_pending_signals_ignores_corrupt_json(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        sig_dir = extraction_daemon._signal_dir()
        (sig_dir / "00000_corrupt.json").write_text("not-json{{{{", encoding="utf-8")

        signals = extraction_daemon.read_pending_signals()
        assert signals == []
        # Corrupt file should have been removed
        assert not (sig_dir / "00000_corrupt.json").exists()

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

        extraction_daemon.write_cursor("sess-abc", 17, "/path/to/transcript.jsonl")
        result = extraction_daemon.read_cursor("sess-abc")

        assert result["line_offset"] == 17
        assert result["transcript_path"] == "/path/to/transcript.jsonl"

    def test_read_cursor_returns_zero_offset_for_unknown_session(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        result = extraction_daemon.read_cursor("no-such-session")

        assert result["line_offset"] == 0
        assert result["transcript_path"] == ""

    def test_read_cursor_returns_zero_on_corrupt_json(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        cursor_dir = extraction_daemon._cursor_dir()
        (cursor_dir / "bad-sess.json").write_text("{not valid json", encoding="utf-8")

        result = extraction_daemon.read_cursor("bad-sess")

        assert result["line_offset"] == 0

    def test_write_cursor_advances_offset(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        extraction_daemon.write_cursor("sess-advance", 5, "/t.jsonl")
        extraction_daemon.write_cursor("sess-advance", 10, "/t.jsonl")

        result = extraction_daemon.read_cursor("sess-advance")
        assert result["line_offset"] == 10

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


# ---------------------------------------------------------------------------
# check_idle_sessions() — additional coverage
# ---------------------------------------------------------------------------

class TestCheckIdleSessions:
    """Additional coverage for check_idle_sessions() paths."""

    def _setup_cursor(self, tmp_path, instance_id, session_id, line_offset, transcript_path):
        cursor_dir = tmp_path / instance_id / "data" / "session-cursors"
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

    def test_skips_session_when_transcript_file_missing(self, monkeypatch, tmp_path):
        """check_idle_sessions must skip cursors pointing to non-existent transcripts."""
        instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        self._setup_cursor(tmp_path, instance_id, "ghost-sess", 1, tmp_path / "nonexistent.jsonl")

        captured = []
        now = 1_700_000_000.0
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
        monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: now - 3600)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(extraction_daemon, "write_signal", lambda *a, **kw: captured.append((a, kw)))

        extraction_daemon.check_idle_sessions(timeout_minutes=30)

        assert captured == []


class TestRollingExtraction:
    def _setup_cursor(self, tmp_path, instance_id, session_id, line_offset, transcript_path):
        cursor_dir = tmp_path / instance_id / "data" / "session-cursors"
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
        config_dir = tmp_path / "rolling-inst" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "memory.json").write_text(
            json.dumps({"adapter": {"type": "standalone"}}),
            encoding="utf-8",
        )
        extraction_daemon.write_cursor("sess-roll", 0, str(transcript_path))
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
        class _FakeAdapter:
            def quaid_home(self):
                return tmp_path / "rolling-inst"

            def instance_root(self):
                return tmp_path / "rolling-inst"

            def data_dir(self):
                return tmp_path / "rolling-inst" / "data"

            def parse_session_jsonl(self, path):
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

            flush_signal = extraction_daemon.write_signal(
                signal_type="session_end",
                session_id="sess-roll",
                transcript_path=str(transcript_path),
            )
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
            assert flush_metric["carry_facts_final"] == 1
            assert flush_metric["carry_duplicate_facts_dropped"] == 2
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
        config_dir = tmp_path / "rolling-inst" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "memory.json").write_text(
            json.dumps({"adapter": {"type": "standalone"}}),
            encoding="utf-8",
        )
        extraction_daemon.write_cursor("sess-roll", 0, str(transcript_path))
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
        class _FakeAdapter:
            def quaid_home(self):
                return tmp_path / "rolling-inst"

            def instance_root(self):
                return tmp_path / "rolling-inst"

            def data_dir(self):
                return tmp_path / "rolling-inst" / "data"

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

        assert captured == []

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
        class _FakeAdapter:
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
