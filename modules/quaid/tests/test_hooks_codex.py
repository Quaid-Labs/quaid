"""Tests for Codex-specific hook behavior."""

import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _run_hook_session_init(hook_input: dict, *, monkeypatch):
    from core.interface import hooks

    captured_out = io.StringIO()
    captured_err = io.StringIO()

    with patch("core.interface.hooks._read_stdin_json", return_value=hook_input), \
         patch("core.interface.hooks.sys.stdout", captured_out), \
         patch("core.interface.hooks.sys.stderr", captured_err):
        hooks.hook_session_init(MagicMock())

    return captured_out.getvalue(), captured_err.getvalue()


def _run_hook_codex_stop(hook_input: dict, *, monkeypatch):
    from core.interface import hooks

    captured_out = io.StringIO()
    captured_err = io.StringIO()

    with patch("core.interface.hooks._read_stdin_json", return_value=hook_input), \
         patch("core.interface.hooks.sys.stdout", captured_out), \
         patch("core.interface.hooks.sys.stderr", captured_err):
        hooks.hook_codex_stop(MagicMock())

    return captured_out.getvalue(), captured_err.getvalue()


@pytest.fixture()
def cursor_dir(tmp_path, monkeypatch):
    from core import extraction_daemon

    d = tmp_path / "cursors"
    d.mkdir()
    monkeypatch.setattr(extraction_daemon, "_cursor_dir", lambda: d)
    return d


def test_codex_session_init_emits_additional_context(monkeypatch, tmp_path):
    projects_dir = tmp_path / "projects"
    identity_dir = tmp_path / "identity"
    projects_dir.mkdir()
    identity_dir.mkdir()

    project = projects_dir / "quaid"
    project.mkdir()
    (project / "TOOLS.md").write_text("# Tools\ncodex startup docs", encoding="utf-8")

    ensure_alive_calls = []
    sweep_calls = []

    from core.interface import hooks
    adapter = MagicMock()
    adapter.projects_dir.return_value = projects_dir
    adapter.identity_dir.return_value = identity_dir
    adapter.get_base_context_files.return_value = {}
    adapter.get_cli_tools_snippet.return_value = ""
    adapter.data_dir.return_value = tmp_path / "data"

    monkeypatch.setattr(hooks, "_get_projects_dir", lambda: projects_dir)
    monkeypatch.setattr(hooks, "_get_identity_dir", lambda: identity_dir)
    monkeypatch.setattr(hooks, "_check_janitor_health", lambda: "")
    monkeypatch.setattr(hooks, "_build_runtime_context_block", lambda: "[Quaid runtime]")
    monkeypatch.setattr(hooks, "_current_adapter_id", lambda: "codex")
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "codex-test")
    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)
    monkeypatch.setattr("core.compatibility.notify_on_use_if_degraded", lambda *_args, **_kwargs: "")
    monkeypatch.setattr("core.extraction_daemon.ensure_alive", lambda: ensure_alive_calls.append(True))
    monkeypatch.setattr("core.extraction_daemon.sweep_orphaned_sessions", lambda sid: sweep_calls.append(sid) or 0)
    monkeypatch.setattr("core.extraction_daemon.read_cursor", lambda sid: {"line_offset": 0, "transcript_path": ""})
    monkeypatch.setattr("core.extraction_daemon.write_cursor", lambda *args: None)

    with patch("core.project_registry.list_projects", return_value={}):
        out, err = _run_hook_session_init(
            {"session_id": "codex-s1", "cwd": str(tmp_path)},
            monkeypatch=monkeypatch,
        )

    payload = json.loads(out)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "quaid/TOOLS.md" in context
    assert "codex startup docs" in context
    assert ensure_alive_calls == [True]
    assert sweep_calls == ["codex-s1"]
    assert not (tmp_path / ".claude" / "rules" / "quaid-projects.md").exists()
    assert "emitted Codex startup context" in err


def test_codex_stop_writes_rolling_signal_only(monkeypatch, tmp_path, cursor_dir):
    transcript_path = tmp_path / "rollout-test.jsonl"
    transcript_path.write_text(
        "\n".join(
            [
                json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "My neighbour won a chili cook-off."}}),
                json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "That is memorable."}}),
            ]
        ) + "\n",
        encoding="utf-8",
    )

    written_signals = []

    def fake_write_signal(**kwargs):
        written_signals.append(kwargs)
        return Path(tmp_path / "signals" / "sig-rolling.json")

    monkeypatch.setattr("core.extraction_daemon.write_signal", fake_write_signal)

    out, err = _run_hook_codex_stop(
        {
            "session_id": "sess-codex-stop",
            "transcript_path": str(transcript_path),
            "cwd": str(tmp_path),
        },
        monkeypatch=monkeypatch,
    )

    payload = json.loads(out)
    assert payload == {}
    assert len(written_signals) == 1
    sig = written_signals[0]
    assert sig["signal_type"] == "rolling"
    assert sig["session_id"] == "sess-codex-stop"
    assert sig["transcript_path"] == str(transcript_path)
    assert sig["adapter"] == "codex"
    assert sig["supports_compaction_control"] is False
    assert sig["meta"]["source"] == "hook_codex_stop"
    assert err.strip() == ""


def test_codex_stop_writes_session_end_signal_for_new_command(monkeypatch, tmp_path, cursor_dir):
    transcript_path = tmp_path / "rollout-test-new.jsonl"
    transcript_path.write_text(
        "\n".join(
            [
                json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "/new"}}),
                json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "Started a fresh session."}}),
            ]
        ) + "\n",
        encoding="utf-8",
    )

    written_signals = []

    def fake_write_signal(**kwargs):
        written_signals.append(kwargs)
        return Path(tmp_path / "signals" / "sig-session-end.json")

    monkeypatch.setattr("core.extraction_daemon.write_signal", fake_write_signal)

    out, err = _run_hook_codex_stop(
        {
            "session_id": "sess-codex-new",
            "transcript_path": str(transcript_path),
            "cwd": str(tmp_path),
        },
        monkeypatch=monkeypatch,
    )

    payload = json.loads(out)
    assert payload == {}
    assert len(written_signals) == 1
    sig = written_signals[0]
    assert sig["signal_type"] == "session_end"
    assert sig["session_id"] == "sess-codex-new"
    assert sig["transcript_path"] == str(transcript_path)
    assert sig["adapter"] == "codex"
    assert sig["supports_compaction_control"] is False
    assert sig["meta"]["source"] == "hook_codex_stop"
    assert sig["meta"]["command"] == "/new"
    assert sig["meta"]["reason"] == "command:new"
    assert err.strip() == ""
