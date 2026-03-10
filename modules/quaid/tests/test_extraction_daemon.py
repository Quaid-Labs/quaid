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

    cursor_dir = tmp_path / "data" / "session-cursors"
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
    import os
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

    cursor_dir = tmp_path / "data" / "session-cursors"
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
    import os
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
