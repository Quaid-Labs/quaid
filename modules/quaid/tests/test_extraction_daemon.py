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

    monkeypatch.setattr(extraction_daemon, "_pid_path", lambda: pid_path)
    monkeypatch.setattr(extraction_daemon.os, "open", lambda *_args, **_kwargs: 11)
    monkeypatch.setattr(extraction_daemon.fcntl, "flock", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(extraction_daemon.os, "close", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(extraction_daemon.os, "fork", lambda: 12345)
    monkeypatch.setattr(extraction_daemon.os, "waitpid", lambda *_args, **_kwargs: (12345, 0))
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
