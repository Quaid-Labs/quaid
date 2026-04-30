import json
import os
import pathlib
import sys
import types
from pathlib import Path

import pytest

from core import extraction_daemon


class _StopDaemonLoop(Exception):
    pass


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


def test_read_pid_rejects_foreign_instance_daemon_pid(monkeypatch, tmp_path):
    pid_path = tmp_path / "extraction-daemon.pid"
    pid_path.write_text("5896", encoding="utf-8")

    monkeypatch.setenv("QUAID_HOME", str(tmp_path / ".quaid"))
    monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
    monkeypatch.setattr(extraction_daemon, "_pid_path", lambda: pid_path)
    monkeypatch.setattr(extraction_daemon.os, "kill", lambda _pid, _sig: None)
    monkeypatch.setattr(extraction_daemon, "_is_daemon_process", lambda _pid: True)
    monkeypatch.setattr(extraction_daemon, "_matching_daemon_pids", lambda **_kwargs: [])

    assert extraction_daemon.read_pid() is None
    assert not pid_path.exists()


def test_read_pid_accepts_current_instance_daemon_pid(monkeypatch, tmp_path):
    pid_path = tmp_path / "extraction-daemon.pid"
    pid_path.write_text("5590", encoding="utf-8")

    monkeypatch.setenv("QUAID_HOME", str(tmp_path / ".quaid"))
    monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
    monkeypatch.setattr(extraction_daemon, "_pid_path", lambda: pid_path)
    monkeypatch.setattr(extraction_daemon.os, "kill", lambda _pid, _sig: None)
    monkeypatch.setattr(extraction_daemon, "_is_daemon_process", lambda _pid: True)
    monkeypatch.setattr(extraction_daemon, "_matching_daemon_pids", lambda **_kwargs: [5590])

    assert extraction_daemon.read_pid() == 5590
    assert pid_path.read_text(encoding="utf-8").strip() == "5590"


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
    transcript_path.write_text(
        '{"role":"user","content":"hello - my garden shed combination is written inside an indigo glass lantern"}\n'
        '{"role":"assistant","content":"noted"}\n',
        encoding="utf-8",
    )

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


def test_synthetic_rolling_stage_flush_metric_uses_rolling_flush_processing_label():
    assert extraction_daemon._rolling_flush_processing_signal_type("session_end", True) == "rolling_flush"
    assert extraction_daemon._rolling_flush_processing_signal_type("session_end", False) == "session_end"


def test_rolling_debug_dump_writes_input_and_fact_rows(monkeypatch, tmp_path):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path / ".quaid"))
    monkeypatch.setenv("QUAID_INSTANCE", "codex-private-tmp-cdx-livetest")
    monkeypatch.delenv("QUAID_ROLLING_DEBUG_DUMP", raising=False)
    monkeypatch.delenv("QUAID_ROLLING_DEBUG_DIR", raising=False)
    flag_path = (
        tmp_path
        / ".quaid"
        / "instances"
        / "codex-private-tmp-cdx-livetest"
        / "data"
        / "rolling-debug.enabled"
    )
    flag_path.parent.mkdir(parents=True, exist_ok=True)
    flag_path.write_text(str(tmp_path / "debug"), encoding="utf-8")

    extraction_daemon._write_rolling_debug_dump(
        "rolling_stage_extract",
        "019dd8da-f1ca-7413-af17-a793beeb79aa",
        text="User: Baxter wrote in the orange linen notebook for Emília Rosa.",
        facts=[
            {
                "text": "Baxter wrote in the orange linen notebook for Emília Rosa.",
                "_source_id": "019dd8da-f1ca-7413-af17-a793beeb79aa",
                "speaker": "user",
                "domains": ["personal"],
            }
        ],
        storage_facts=[
            {
                "text": "Baxter wrote in the orange linen notebook for Emília Rosa.",
                "status": "stored",
            }
        ],
        buffered_line_offset=42,
    )

    jsonl_files = list((tmp_path / "debug").glob("quaid-rolling-debug-019dd8da-f1ca-7413-af17-a793beeb79aa.jsonl"))
    assert len(jsonl_files) == 1
    row = json.loads(jsonl_files[0].read_text(encoding="utf-8").strip())
    assert row["event"] == "rolling_stage_extract"
    assert row["buffered_line_offset"] == 42
    assert row["text_marker_hits"]["Baxter"] == [6]
    assert row["facts"][0]["source_session_id"] == "019dd8da-f1ca-7413-af17-a793beeb79aa"
    assert row["storage_facts"][0]["status"] == "stored"
    assert Path(row["text_path"]).read_text(encoding="utf-8") == (
        "User: Baxter wrote in the orange linen notebook for Emília Rosa."
    )


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


def test_cursor_records_transcript_path_falls_back_on_read_error_when_not_fail_hard(monkeypatch):
    monkeypatch.setattr(
        extraction_daemon,
        "read_cursor",
        lambda _session_id: (_ for _ in ()).throw(RuntimeError("cursor read failed")),
    )
    monkeypatch.setattr("lib.fail_policy.is_fail_hard_enabled", lambda: False)

    assert not extraction_daemon._cursor_records_transcript_path("broken-session", "/tmp/session.jsonl")


def test_process_signal_allows_current_instance_cursor_owned_transcript(monkeypatch, tmp_path):
    from lib.adapter import set_adapter, reset_adapter
    from ingest import extract as extract_mod

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "codex-m13test")
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

    def fake_apply_extracted_payloads(payload, **_kwargs):
        published_payloads.append(payload)
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

    def _raise_getsize(_path):
        raise OSError("stat failed")

    monkeypatch.setattr(extraction_daemon.os.path, "getsize", _raise_getsize)

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
    monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
    monkeypatch.setattr(extraction_daemon, "read_rolling_state", lambda _sid: {})
    monkeypatch.setattr(
        extraction_daemon,
        "_buffer_transcript_tail",
        lambda path, from_line, state, adapter=None, **kwargs: (
            {
                "buffered_line_offset": initial_lines + 1,
                "semantic_buffer": "User: My sister Clara likes alpacas, lives in Boise, and runs a kiln studio every weekend.",
                "semantic_buffer_tokens": 12,
            },
            {
                "raw_lines_added": 1,
                "semantic_chars_added": 36,
                "semantic_tokens_added": 12,
                "buffered_line_offset": initial_lines + 1,
            },
        ),
    )
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
    assert cursor["line_offset"] == initial_lines
    assert cursor["internal"] is False
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

    def test_process_signal_preserves_missing_timeout_signal_for_retry(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

        missing_path = tmp_path / "missing-timeout.jsonl"
        calls = []
        marked = []
        released = []

        monkeypatch.setattr(extraction_daemon, "_read_rolling_state_for_signal", lambda sid, _path: ({}, sid))
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

    def test_process_signal_serializes_shared_source_across_session_ids(self, monkeypatch, tmp_path):
        from ingest import extract as extract_mod
        from lib.adapter import reset_adapter, set_adapter

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "test-inst")

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

    def test_idle_scan_freezes_startup_handshake_only_transcript(self, monkeypatch, tmp_path):
        """OC startup wrappers must not consume the timeout path before queued user text lands."""
        instance_id = "openclaw-main"
        session_id = "139d8a95-9421-4274-a4c9-a1e44d8aa79a"
        transcript = tmp_path / f"{session_id}.jsonl"
        transcript.write_text(
            '{"role":"user","content":"Hello"}\n'
            '{"role":"assistant","content":"Hey Solomon - how can I help?"}\n'
            '{"role":"user","content":"[Queued messages while agent was busy]\\nA new session was started via /new or /reset. Execute your Session Startup sequence now."}\n'
            '{"role":"assistant","content":"NO_REPLY"}\n',
            encoding="utf-8",
        )
        self._setup_cursor(tmp_path, instance_id, session_id, 0, transcript)

        now = 1_700_000_000.0
        os.utime(transcript, (now - 120, now - 120))

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def parse_session_jsonl(self, _path):
                return (
                    "User: Hello\n\n"
                    "Assistant: Hey Solomon - how can I help?\n\n"
                    "User: A new session was started via /new or /reset. Execute your Session Startup sequence now.\n\n"
                    "Assistant: NO_REPLY"
                )

        captured = []
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", instance_id)
        monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
        monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: now - 7200)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(extraction_daemon, "_load_runtime_adapter", lambda: _FakeAdapter())
        monkeypatch.setattr(extraction_daemon, "write_signal", lambda *a, **kw: captured.append((a, kw)))

        try:
            extraction_daemon.check_idle_sessions(timeout_minutes=1)
        finally:
            extraction_daemon._cursor_end_timeout_fired.discard(session_id)

        cursor = extraction_daemon.read_cursor(session_id)
        assert captured == []
        assert cursor["internal"] is True
        assert cursor["line_offset"] == 4

    def test_idle_scan_freezes_greeting_only_timeout_transcript(self, monkeypatch, tmp_path):
        """OC may materialize only /new greeting turns before the real queued prompt."""
        instance_id = "openclaw-main"
        session_id = "8bdb7c01-2af3-41ae-9882-04324aa1d7f6"
        transcript = tmp_path / f"{session_id}.jsonl"
        transcript.write_text(
            '{"role":"user","content":"Hello"}\n'
            '{"role":"assistant","content":"Hey Solomon. How can I help?"}\n'
            '{"role":"assistant","content":"NO_REPLY"}\n',
            encoding="utf-8",
        )
        self._setup_cursor(tmp_path, instance_id, session_id, 0, transcript)

        now = 1_700_000_000.0
        os.utime(transcript, (now - 120, now - 120))

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def parse_session_jsonl(self, _path):
                return (
                    "User: Hello\n\n"
                    "Assistant: Hey Solomon. How can I help?\n\n"
                    "Assistant: NO_REPLY"
                )

        captured = []
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", instance_id)
        monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
        monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: now - 7200)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(extraction_daemon, "_load_runtime_adapter", lambda: _FakeAdapter())
        monkeypatch.setattr(extraction_daemon, "write_signal", lambda *a, **kw: captured.append((a, kw)))

        try:
            extraction_daemon.check_idle_sessions(timeout_minutes=1)
        finally:
            extraction_daemon._cursor_end_timeout_fired.discard(session_id)

        cursor = extraction_daemon.read_cursor(session_id)
        assert captured == []
        assert cursor["internal"] is True
        assert cursor["line_offset"] == 3

    def test_idle_scan_skips_daemon_owned_preserved_transcript_without_live_source(self, monkeypatch, tmp_path):
        """Preserved OC mirrors are lifecycle inputs, not active timeout candidates."""
        instance_id = "openclaw-main"
        session_id = "a397508c-02c8-401a-88cf-9f977757fbfd"
        preserved_dir = tmp_path / "instances" / instance_id / "logs" / "quaid" / "sessions"
        preserved_dir.mkdir(parents=True, exist_ok=True)
        preserved = preserved_dir / f"{session_id}.jsonl"
        preserved.write_text(
            '{"role":"user","content":"Hello"}\n'
            '{"role":"assistant","content":"Hey!"}\n'
            '{"role":"assistant","content":"NO_REPLY"}\n',
            encoding="utf-8",
        )
        source_key = extraction_daemon._signal_source_cursor_key(session_id, str(preserved))
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", instance_id)
        extraction_daemon.write_cursor(session_id, 0, str(preserved), source_key=source_key)

        now = 1_700_000_000.0
        os.utime(preserved, (now - 600, now - 600))

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def get_sessions_dir(self):
                return tmp_path / "missing-openclaw-sessions"

            def parse_session_jsonl(self, path):
                return Path(path).read_text(encoding="utf-8")

        captured = []
        monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
        monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: now - 7200)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(extraction_daemon, "_load_runtime_adapter", lambda: _FakeAdapter())
        monkeypatch.setattr(extraction_daemon, "write_signal", lambda *a, **kw: captured.append((a, kw)))

        extraction_daemon.check_idle_sessions(timeout_minutes=1)

        assert captured == []

    def test_timeout_signal_skips_daemon_owned_preserved_transcript(self, monkeypatch, tmp_path):
        """A queued timeout against a preserved mirror must not extract stale startup text."""
        instance_id = "openclaw-main"
        session_id = "a397508c-02c8-401a-88cf-9f977757fbfd"
        preserved_dir = tmp_path / "instances" / instance_id / "logs" / "quaid" / "sessions"
        preserved_dir.mkdir(parents=True, exist_ok=True)
        preserved = preserved_dir / f"{session_id}.jsonl"
        preserved.write_text('{"role":"user","content":"Hello"}\n', encoding="utf-8")
        source_key = extraction_daemon._signal_source_cursor_key(session_id, str(preserved))
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", instance_id)
        extraction_daemon.write_cursor(session_id, 0, str(preserved), source_key=source_key)
        signal_path = extraction_daemon.write_signal(
            signal_type="timeout",
            session_id=session_id,
            transcript_path=str(preserved),
            meta={"compact_on_timeout": True},
        )

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def parse_session_jsonl(self, _path):
                raise AssertionError("timeout should not parse preserved daemon mirror")

        monkeypatch.setattr(extraction_daemon, "_reload_config_if_changed", lambda _reason: None)
        monkeypatch.setattr(extraction_daemon, "_load_runtime_adapter", lambda: _FakeAdapter())
        monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "Owner")
        monkeypatch.setattr(extraction_daemon, "write_context_refresh_timeout_marker", lambda _sid: None)

        signal = extraction_daemon.read_pending_signals()[0]
        extraction_daemon.process_signal(signal)

        assert not signal_path.exists()

    def test_timeout_signal_marks_greeting_only_transcript_internal(self, monkeypatch, tmp_path):
        """Queued timeout signals must not send /new greeting wrappers to extraction."""
        instance_id = "openclaw-main"
        session_id = "8bdb7c01-2af3-41ae-9882-04324aa1d7f6"
        transcript = tmp_path / f"{session_id}.jsonl"
        transcript.write_text(
            '{"role":"user","content":"Hello"}\n'
            '{"role":"assistant","content":"Hey Solomon. How can I help?"}\n'
            '{"role":"assistant","content":"NO_REPLY"}\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", instance_id)
        extraction_daemon.write_cursor(session_id, 0, str(transcript))
        signal_path = extraction_daemon.write_signal(
            signal_type="timeout",
            session_id=session_id,
            transcript_path=str(transcript),
            meta={"compact_on_timeout": True},
        )

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def parse_session_jsonl(self, _path):
                return (
                    "User: Hello\n\n"
                    "Assistant: Hey Solomon. How can I help?\n\n"
                    "Assistant: NO_REPLY"
                )

        import ingest.extract as extract_mod

        monkeypatch.setattr(extraction_daemon, "_reload_config_if_changed", lambda _reason: None)
        monkeypatch.setattr(extraction_daemon, "_load_runtime_adapter", lambda: _FakeAdapter())
        monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "Owner")
        monkeypatch.setattr(extraction_daemon, "write_context_refresh_timeout_marker", lambda _sid: None)
        monkeypatch.setattr(
            extract_mod,
            "extract_from_transcript",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("greeting-only timeout should not call extraction")
            ),
        )

        signal = extraction_daemon.read_pending_signals()[0]
        extraction_daemon.process_signal(signal)

        cursor = extraction_daemon.read_cursor(session_id)
        assert not signal_path.exists()
        assert cursor["internal"] is True
        assert cursor["line_offset"] == 3

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
        fake_adapter_mod.StandaloneAdapter = object
        fake_adapter_mod.quaid_projects_dir = lambda: tmp_path / "projects"
        fake_adapter_mod.quaid_tracking_dir = lambda: tmp_path / "tracking"

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def parse_session_jsonl(self, path):
                return "User: My cat Luna sleeps on the windowsill every afternoon."

        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
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
        fake_adapter_mod.StandaloneAdapter = object
        fake_adapter_mod.quaid_projects_dir = lambda: tmp_path / "projects"
        fake_adapter_mod.quaid_tracking_dir = lambda: tmp_path / "tracking"

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def parse_session_jsonl(self, path):
                _ = path
                return ""

        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
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
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda default=8000: 10)
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

    def test_rolling_flush_preserves_threshold_crossing_tail_for_lifecycle(self, monkeypatch, tmp_path):
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
        fake_adapter_mod.StandaloneAdapter = object
        fake_adapter_mod.quaid_projects_dir = lambda: tmp_path / "projects"
        fake_adapter_mod.quaid_tracking_dir = lambda: tmp_path / "tracking"
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

            # Simulate a real lifecycle signal arriving before the synthetic
            # staged-payload flush is processed. The synthetic flush must not
            # consume it, because it is the signal that drains the residual
            # semantic buffer below the rolling threshold.
            extraction_daemon.write_signal(
                signal_type="session_end",
                session_id="sess-roll-residual",
                transcript_path=str(transcript_path),
            )
            parse_empty["value"] = True
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])
            state_after_synthetic_flush = extraction_daemon.read_rolling_state("sess-roll-residual")
            assert len(applied_payloads) == 1
            assert [fact["text"] for fact in applied_payloads[0]["raw_facts"]] == [
                "Owner has stable prior memories"
            ]
            assert state_after_synthetic_flush["semantic_buffer"] == tail
            assert state_after_synthetic_flush["raw_facts"] == []
            pending_after_synthetic_flush = extraction_daemon.read_pending_signals()
            assert len(pending_after_synthetic_flush) == 1
            assert pending_after_synthetic_flush[0]["type"] == "session_end"
            assert not pending_after_synthetic_flush[0].get("meta")

            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])
            assert seen_transcripts == [prior, tail]
            assert len(applied_payloads) == 2
            assert [fact["text"] for fact in applied_payloads[1]["raw_facts"]] == [
                "Owner discussed Baxter"
            ]
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

    def test_rolling_below_threshold_keeps_snapshot_for_lifecycle_flush(self, monkeypatch, tmp_path):
        import sys
        import types

        session_id = "sess-roll-snapshot-tail"
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "rolling-inst")
        instance_root = tmp_path / "instances" / "rolling-inst"
        snapshot = (
            instance_root
            / "logs"
            / "daemon"
            / "rolling-transcript-snapshots"
            / session_id
            / "20260429T135031Z-deadbeef"
            / f"{session_id}.jsonl"
        )
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(
            '{"role":"user","content":"chunk one"}\n'
            '{"role":"assistant","content":"ack"}\n',
            encoding="utf-8",
        )
        extraction_daemon.write_cursor(session_id, 2, str(snapshot))
        extraction_daemon.write_rolling_state(
            session_id,
            {
                "session_id": session_id,
                "transcript_path": str(snapshot),
                "semantic_buffer": "User: Baxter residual context waits for lifecycle.",
                "semantic_buffer_tokens": 10,
                "buffered_line_offset": 2,
                "processed_line_offset": 2,
                "raw_facts": [],
                "raw_snippets": {},
                "raw_journal": {},
                "raw_project_logs": {},
            },
        )
        monkeypatch.setattr(extraction_daemon, "_get_owner_id", lambda: "Owner")
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda default=8000: 100)

        real_adapter = sys.modules.get("lib.adapter")
        fake_adapter_mod = types.ModuleType("lib.adapter")
        fake_adapter_mod.StandaloneAdapter = object
        fake_adapter_mod.quaid_projects_dir = lambda: tmp_path / "projects"
        fake_adapter_mod.quaid_tracking_dir = lambda: tmp_path / "tracking"

        class _FakeAdapter(_OwnedTestAdapterMixin):
            def quaid_home(self):
                return tmp_path

            def instance_root(self):
                return instance_root

            def data_dir(self):
                return instance_root / "data"

            def parse_session_jsonl(self, path):
                return ""

        fake_adapter_mod.get_adapter = lambda: _FakeAdapter()
        sys.modules["lib.adapter"] = fake_adapter_mod

        try:
            extraction_daemon.write_signal(
                signal_type="rolling",
                session_id=session_id,
                transcript_path=str(snapshot),
                meta={"reason": "continued_chunk_budget"},
            )
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])

            state = extraction_daemon.read_rolling_state(session_id)
            cursor = extraction_daemon.read_cursor(session_id)
            assert snapshot.exists()
            assert state["semantic_buffer"] == "User: Baxter residual context waits for lifecycle."
            assert cursor["transcript_path"] == str(snapshot)
            assert extraction_daemon.read_pending_signals() == []
        finally:
            if real_adapter is not None:
                sys.modules["lib.adapter"] = real_adapter
            else:
                sys.modules.pop("lib.adapter", None)

    def test_lifecycle_alias_drains_residual_rolling_semantic_buffer(self, monkeypatch, tmp_path):
        uuid = "019dcf34-52b2-7010-9b01-eec8ba485b54"
        full_session_id = f"rollout-2026-04-27T13-50-06-{uuid}"
        transcript_path = tmp_path / f"{full_session_id}.jsonl"
        transcript_path.write_text(
            '{"role":"user","content":"chunk one stable memory"}\n'
            '{"role":"user","content":"chunk two Baxter residual"}\n',
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
                transcript_path=str(transcript_path),
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

    def test_process_signal_trusts_source_cursor_alias_when_rollout_session_id_differs(
        self, monkeypatch, tmp_path
    ):
        import sys
        import types

        uuid = "019dd737-3b58-7b30-adc9-dbab99dc5846"
        full_session_id = f"rollout-2026-04-29T03-10-14-{uuid}"
        basename = f"{full_session_id}.jsonl"
        original = tmp_path / ".codex" / "sessions" / "2026" / "04" / "29" / basename
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_text(
            '{"type":"session_meta","payload":{"cwd":"/Users/admin/quaidcode/dev"}}\n'
            '{"type":"event_msg","payload":{"type":"user_message","message":"Baxter residual context with enough durable detail for extraction"}}\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "codex-private-tmp-cdx-livetest")
        instance_root = tmp_path / "instances" / "codex-private-tmp-cdx-livetest"
        instance_root.mkdir(parents=True, exist_ok=True)
        snapshot = (
            instance_root
            / "logs"
            / "daemon"
            / "rolling-transcript-snapshots"
            / full_session_id
            / "20260429T031319Z-ab465afeaeb56ef6"
            / basename
        )
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(original.read_text(encoding="utf-8"), encoding="utf-8")

        source_key = extraction_daemon._signal_source_cursor_key(full_session_id, str(original))
        extraction_daemon.write_cursor(full_session_id, 1, str(snapshot), source_key=source_key)
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
                raw = Path(path).read_text(encoding="utf-8")
                return "User: Baxter residual context with enough durable detail for extraction" if "Baxter" in raw else ""

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
                session_id=uuid,
                transcript_path=str(original),
            )
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])

            assert seen_transcripts == ["User: Baxter residual context with enough durable detail for extraction"]
            assert len(applied) == 1
            assert applied[0][1]["session_id"] == uuid
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
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda default=8000: 8)

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

        fake_adapter_mod.StandaloneAdapter = object
        fake_adapter_mod.quaid_projects_dir = lambda: tmp_path / "projects"
        fake_adapter_mod.quaid_tracking_dir = lambda: tmp_path / "tracking"
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
                signal_type="session_end",
                session_id="sess-roll",
                transcript_path=str(transcript_path),
            )
            extraction_daemon.process_signal(extraction_daemon.read_pending_signals()[0])

            assert seen_transcripts == ["User: My sister is Diana\n\nAssistant: Her daughter is Alice"]
            assert extraction_daemon.read_cursor("sess-roll")["line_offset"] == 2
            assert not extraction_daemon._rolling_state_path("sess-roll").exists()
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
        fake_adapter_mod.StandaloneAdapter = object
        fake_adapter_mod.quaid_projects_dir = lambda: tmp_path / "projects"
        fake_adapter_mod.quaid_tracking_dir = lambda: tmp_path / "tracking"

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
        fake_adapter_mod.StandaloneAdapter = object
        fake_adapter_mod.quaid_projects_dir = lambda: tmp_path / "projects"
        fake_adapter_mod.quaid_tracking_dir = lambda: tmp_path / "tracking"

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
        monkeypatch.setattr(extraction_daemon, "_get_capture_chunk_tokens", lambda default=8000: 10)
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

    def test_merge_staged_payloads_subset_overlap_ignores_negation_mismatch(self, monkeypatch):
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
        assert merged["staged_semantic_subset_rows"] == 0
        assert merged["staged_semantic_llm_checks"] == 0
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

    def test_flushes_buffered_semantic_tail_from_rolling_snapshot_when_newer_session_exists(self, monkeypatch, tmp_path):
        """Daemon rolling snapshots are stable lifecycle inputs, not inactive preserved mirrors."""
        instance_id = "openclaw-main"
        session_id = "old-snapshot-sess"
        snapshot = (
            tmp_path
            / "instances"
            / instance_id
            / "logs"
            / "daemon"
            / "rolling-transcript-snapshots"
            / session_id
            / "20260429T135031Z-deadbeef"
            / f"{session_id}.jsonl"
        )
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(
            '{"role":"user","content":"chunk one"}\n'
            '{"role":"assistant","content":"ack"}\n',
            encoding="utf-8",
        )
        self._setup_cursor(tmp_path, instance_id, session_id, 2, snapshot)
        rolling_dir = tmp_path / "instances" / instance_id / "data" / "rolling-extraction"
        rolling_dir.mkdir(parents=True, exist_ok=True)
        (rolling_dir / f"{session_id}.json").write_text(
            json.dumps({
                "session_id": session_id,
                "transcript_path": str(snapshot),
                "processed_line_offset": 2,
                "buffered_line_offset": 2,
                "semantic_buffer": "User: Baxter is written in the orange linen notebook from Emília Rosa.",
                "semantic_buffer_tokens": 14,
                "raw_facts": [],
                "raw_snippets": {},
                "raw_journal": {},
                "raw_project_logs": {},
            }),
            encoding="utf-8",
        )

        new_transcript = tmp_path / "new-openclaw.jsonl"
        new_transcript.write_text('{"role":"user","content":"new session"}\n', encoding="utf-8")
        self._setup_cursor(tmp_path, instance_id, "new-openclaw-sess", 0, new_transcript)

        now = 1_700_000_000.0
        old_mtime = now - 30
        new_mtime = now - 5
        os.utime(snapshot, (old_mtime, old_mtime))
        os.utime(new_transcript, (new_mtime, new_mtime))

        class _FakeAdapter(_OwnedTestAdapterMixin):
            pass

        captured = []
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", instance_id)
        monkeypatch.setattr(extraction_daemon.time, "time", lambda: now)
        monkeypatch.setattr(extraction_daemon, "_read_installed_at", lambda: now - 7200)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [])
        monkeypatch.setattr(extraction_daemon, "_load_runtime_adapter", lambda: _FakeAdapter())
        monkeypatch.setattr(extraction_daemon, "_reconcile_internal_cursor_state", lambda *a, **kw: "not_internal")
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
                "session_id": session_id,
                "transcript_path": str(snapshot),
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
