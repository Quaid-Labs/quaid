"""Tests for Codex-specific hook behavior."""

import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _adapter_mock():
    adapter = MagicMock()
    adapter._extract_hook_session_id = None
    return adapter


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


def _run_hook_inject(hook_input: dict, *, monkeypatch):
    from core.interface import hooks

    captured_out = io.StringIO()
    captured_err = io.StringIO()

    with patch("core.interface.hooks._read_stdin_json", return_value=hook_input), \
         patch("core.interface.hooks.sys.stdout", captured_out), \
         patch("core.interface.hooks.sys.stderr", captured_err):
        hooks.hook_inject(MagicMock())

    return captured_out.getvalue(), captured_err.getvalue()


def test_read_stdin_json_reads_pipe_payload(monkeypatch):
    from core.interface import hooks

    payload = {
        "session_id": "sess-pipe",
        "transcript_path": "/tmp/rollout.jsonl",
        "cwd": "/tmp",
        "prompt": "/new",
    }
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, json.dumps(payload).encode("utf-8"))
    finally:
        os.close(write_fd)

    stdin_handle = os.fdopen(read_fd, "r", encoding="utf-8", closefd=True)
    try:
        monkeypatch.setattr(hooks.sys, "stdin", stdin_handle)
        assert hooks._read_stdin_json() == payload
    finally:
        stdin_handle.close()


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

    from core.interface import hooks
    adapter = _adapter_mock()
    adapter.projects_dir.return_value = projects_dir
    adapter.identity_dir.return_value = identity_dir
    adapter.get_base_context_files.return_value = {}
    adapter.get_cli_tools_snippet.return_value = ""
    adapter.get_pending_context.return_value = ""
    adapter.data_dir.return_value = tmp_path / "data"
    adapter.instance_root.return_value = tmp_path

    monkeypatch.setattr(hooks, "_get_projects_dir", lambda: projects_dir)
    monkeypatch.setattr(hooks, "_get_identity_dir", lambda: identity_dir)
    monkeypatch.setattr(hooks, "_check_janitor_health", lambda: "")
    monkeypatch.setattr(hooks, "_get_deferred_notice_hint", lambda: "")
    monkeypatch.setattr(hooks, "_build_runtime_context_block", lambda: "[Quaid runtime]")
    monkeypatch.setattr(hooks, "_current_adapter_id", lambda: "codex")
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "codex-test")
    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)
    monkeypatch.setattr("core.compatibility.notify_on_use_if_degraded", lambda *_args, **_kwargs: "")
    monkeypatch.setattr("core.extraction_daemon.ensure_alive", lambda: ensure_alive_calls.append(True))
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
    assert context.startswith("<quaid_system_message>\n")
    assert context.rstrip().endswith("</quaid_system_message>")
    assert "quaid/TOOLS.md" in context
    assert "codex startup docs" in context
    assert ensure_alive_calls == [True]
    assert not (tmp_path / ".claude" / "rules" / "quaid-projects.md").exists()
    assert "emitted Codex startup context" in err


def test_runtime_context_block_includes_quaid_home_and_instance_without_metadata(monkeypatch, tmp_path):
    from core.runtime import system_context

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "codex-test")
    monkeypatch.setattr(system_context, "collect_system_context_metadata", lambda **kwargs: {"entries": []})

    content = system_context.build_system_context_block()

    assert content == "\n".join(
        [
            "[Quaid runtime]",
            f"QUAID_HOME: {tmp_path}",
            "QUAID_INSTANCE: codex-test",
            "instance: codex-test",
        ]
    )


def test_codex_session_init_surfaces_startup_notices_and_pending_queue(monkeypatch, tmp_path):
    projects_dir = tmp_path / "projects"
    identity_dir = tmp_path / "identity"
    projects_dir.mkdir()
    identity_dir.mkdir()

    project = projects_dir / "quaid"
    project.mkdir()
    (project / "TOOLS.md").write_text("# Tools\ncodex startup docs", encoding="utf-8")

    from core.interface import hooks
    adapter = _adapter_mock()
    adapter.projects_dir.return_value = projects_dir
    adapter.identity_dir.return_value = identity_dir
    adapter.get_base_context_files.return_value = {}
    adapter.get_cli_tools_snippet.return_value = ""
    adapter.get_pending_context.return_value = (
        "The following are pending notifications for the user — please relay them in your response:\n\n"
        "<quaid_system_message>\n• [Quaid error] [provider] Earlier queued notice\n</quaid_system_message>"
    )
    adapter.data_dir.return_value = tmp_path / "data"

    monkeypatch.setattr(hooks, "_get_projects_dir", lambda: projects_dir)
    monkeypatch.setattr(hooks, "_get_identity_dir", lambda: identity_dir)
    monkeypatch.setattr(hooks, "_check_janitor_health", lambda: "")
    monkeypatch.setattr(
        hooks,
        "_get_deferred_notice_hint",
        lambda: (
            "<quaid_system_message>\n"
            "Quaid has 2 deferred maintenance notices waiting.\n"
            "</quaid_system_message>"
        ),
    )
    monkeypatch.setattr(hooks, "_build_runtime_context_block", lambda: "[Quaid runtime]")
    monkeypatch.setattr(hooks, "_current_adapter_id", lambda: "codex")
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "codex-test")
    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)
    monkeypatch.setattr("core.compatibility.notify_on_use_if_degraded", lambda *_args, **_kwargs: "")
    monkeypatch.setattr("core.extraction_daemon.ensure_alive", lambda: (_ for _ in ()).throw(RuntimeError("daemon offline")))
    monkeypatch.setattr("core.extraction_daemon.read_cursor", lambda sid: {"line_offset": 0, "transcript_path": ""})
    monkeypatch.setattr("core.extraction_daemon.write_cursor", lambda *args: None)

    with patch("core.project_registry.list_projects", return_value={}):
        out, _err = _run_hook_session_init(
            {"session_id": "codex-s1", "cwd": str(tmp_path)},
            monkeypatch=monkeypatch,
        )

    payload = json.loads(out)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "Earlier queued notice" in context
    assert "deferred maintenance notices waiting" in context
    assert "background extraction daemon failed to start" in context
    assert "daemon offline" not in context
    assert "Error type: RuntimeError" in context


def test_codex_hook_inject_surfaces_provider_error_notice(monkeypatch, tmp_path):
    from core.interface import hooks

    adapter = _adapter_mock()
    adapter.get_pending_context.return_value = ""
    adapter.resolve_prompt_submit_signal.return_value = None
    adapter.adapter_id.return_value = "codex"
    adapter.get_session_path.return_value = None
    adapter.get_sessions_dir.return_value = str(tmp_path / "sessions")

    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)
    monkeypatch.setattr("lib.adapter._ensure_instance_projects_bootstrapped", lambda _adapter: None)
    monkeypatch.setattr("core.extraction_daemon.read_cursor", lambda sid: {"line_offset": 0, "transcript_path": ""})
    monkeypatch.setattr("core.extraction_daemon.write_cursor", lambda *args: None)
    monkeypatch.setattr("lib.fail_policy.is_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(hooks, "_get_pending_context", lambda: "")
    monkeypatch.setattr(hooks, "_get_deferred_notice_hint", lambda: "")
    monkeypatch.setattr(hooks, "_get_owner_id", lambda: "codex-owner")

    with patch(
        "core.interface.api.recall_fast",
        side_effect=RuntimeError(
            "Quaid could not access its fast language model provider: codex gateway HTTP 404 model=invalid-model-xyzzy"
        ),
    ), patch("core.interface.api.projects_search_docs", return_value=None):
        out, err = _run_hook_inject(
            {
                "prompt": "What do you know about Maya?",
                "session_id": "sess-codex-provider",
                "cwd": str(tmp_path),
            },
            monkeypatch=monkeypatch,
        )

    payload = json.loads(out)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "[Quaid error]" in context
    assert "[provider]" in context
    assert "Error type: RuntimeError" in context
    assert "invalid-model-xyzzy" not in context
    assert "hook-inject" in err


def test_codex_hook_inject_raises_provider_error_when_fail_hard_enabled(monkeypatch, tmp_path):
    from core.interface import hooks

    adapter = _adapter_mock()
    adapter.get_pending_context.return_value = ""
    adapter.resolve_prompt_submit_signal.return_value = None
    adapter.adapter_id.return_value = "codex"
    adapter.get_session_path.return_value = None
    adapter.get_sessions_dir.return_value = str(tmp_path / "sessions")

    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)
    monkeypatch.setattr("lib.adapter._ensure_instance_projects_bootstrapped", lambda _adapter: None)
    monkeypatch.setattr("core.extraction_daemon.read_cursor", lambda sid: {"line_offset": 0, "transcript_path": ""})
    monkeypatch.setattr("core.extraction_daemon.write_cursor", lambda *args: None)
    monkeypatch.setattr("lib.fail_policy.is_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(hooks, "_get_pending_context", lambda: "")
    monkeypatch.setattr(hooks, "_get_deferred_notice_hint", lambda: "")
    monkeypatch.setattr(hooks, "_get_owner_id", lambda: "codex-owner")

    with patch(
        "core.interface.api.recall_fast",
        side_effect=RuntimeError(
            "Quaid could not access its fast language model provider: codex gateway HTTP 404 model=invalid-model-xyzzy"
        ),
    ), patch("core.interface.api.projects_search_docs", return_value=None), \
         pytest.raises(RuntimeError, match="language model provider"):
        _run_hook_inject(
            {
                "prompt": "What do you know about Maya?",
                "session_id": "sess-codex-provider-failhard",
                "cwd": str(tmp_path),
            },
            monkeypatch=monkeypatch,
        )


def test_codex_stop_does_not_write_signal_for_regular_turn(monkeypatch, tmp_path, cursor_dir):
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
        return Path(tmp_path / "signals" / "sig-unused.json")

    monkeypatch.setattr("core.extraction_daemon.write_signal", fake_write_signal)
    adapter = _adapter_mock()
    adapter.resolve_stop_hook_signal.return_value = None
    adapter.adapter_id.return_value = "codex"
    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)

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
    assert written_signals == []
    assert err.strip() == ""


def test_codex_hook_inject_promotes_recall_router_warning_to_provider_notice(monkeypatch, tmp_path):
    from core.interface import hooks

    adapter = _adapter_mock()
    adapter.get_pending_context.return_value = ""
    adapter.resolve_prompt_submit_signal.return_value = None
    adapter.adapter_id.return_value = "codex"
    adapter.instance_root.return_value = tmp_path
    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)
    monkeypatch.setattr(hooks, "_get_owner_id", lambda: "test-owner")
    monkeypatch.setattr(hooks, "_get_deferred_notice_hint", lambda: "")
    monkeypatch.setattr("core.extraction_daemon.write_cursor", lambda *args: None)

    warning = {
        "text": "[RECALL ROUTER WARNING] Fast prepass failed and fallback recall plan was used. Reason: invalid-model-xyzzy provider failure.",
        "similarity": 1.0,
        "category": "system_notice",
    }
    fact = {"text": "Mendoza is known for Malbec", "similarity": 0.9, "category": "fact"}
    meta = {
        "turn_details": [
            {
                "planner": {
                    "bailout_reason": "planner_exception_fallback_off",
                    "fallback_detail": "invalid-model-xyzzy provider failure",
                }
            }
        ]
    }

    with patch("core.interface.api.recall_fast", return_value=([warning, fact], meta)), \
         patch("core.interface.api.projects_search_docs", return_value=None):
        out, _err = _run_hook_inject(
            {
                "prompt": "What do you know about Mendoza?",
                "session_id": "codex-provider-warning",
                "cwd": str(tmp_path),
            },
            monkeypatch=monkeypatch,
        )

    payload = json.loads(out)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "[Quaid error] [provider]" in context
    assert "Mendoza is known for Malbec" in context
    assert "[RECALL ROUTER WARNING]" not in context


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
    adapter = _adapter_mock()
    adapter.resolve_stop_hook_signal.return_value = {
        "signal_type": "session_end",
        "meta": {
            "source": "hook_codex_stop",
            "command": "/new",
            "reason": "command:new",
        },
    }
    adapter.adapter_id.return_value = "codex"
    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)

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


def test_codex_inject_writes_session_end_signal_for_clear_command(monkeypatch, tmp_path, cursor_dir):
    transcript_path = tmp_path / "rollout-test-clear.jsonl"
    transcript_path.write_text(
        json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "Earlier turn"}}) + "\n",
        encoding="utf-8",
    )

    written_signals = []

    def fake_write_signal(**kwargs):
        written_signals.append(kwargs)
        return Path(tmp_path / "signals" / "sig-session-end.json")

    monkeypatch.setattr("core.extraction_daemon.write_signal", fake_write_signal)
    adapter = _adapter_mock()
    adapter.resolve_prompt_submit_signal.return_value = {
        "signal_type": "session_end",
        "meta": {
            "source": "hook_inject",
            "command": "/clear",
            "reason": "command:clear",
        },
    }
    adapter.adapter_id.return_value = "codex"
    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)

    out, err = _run_hook_inject(
        {
            "session_id": "sess-codex-clear",
            "transcript_path": str(transcript_path),
            "cwd": str(tmp_path),
            "prompt": "/clear",
        },
        monkeypatch=monkeypatch,
    )

    assert out.strip() == ""
    assert len(written_signals) == 1
    sig = written_signals[0]
    assert sig["signal_type"] == "session_end"
    assert sig["session_id"] == "sess-codex-clear"
    assert sig["transcript_path"] == str(transcript_path)
    assert sig["adapter"] == "codex"
    assert sig["supports_compaction_control"] is False
    assert sig["meta"]["source"] == "hook_inject"
    assert sig["meta"]["command"] == "/clear"
    assert sig["meta"]["reason"] == "command:clear"
    assert err.strip() == ""


def test_codex_deferred_notice_relay_context_is_enabled(monkeypatch):
    from core.interface import hooks

    monkeypatch.setattr(hooks, "_adapter_capability", lambda key, default=None: True if key == "deferred_notice_relay" else default)
    with patch("lib.runtime_context.drain_deferred_notices", return_value=[{"message": "Deferred relay test"}]):
        context = hooks._get_deferred_notice_relay_context()

    assert "Deferred relay test" in context
    assert "must" in context.lower() or "relay" in context.lower()


def test_codex_hook_inject_surfaces_new_pending_notice_on_same_turn(monkeypatch, tmp_path):
    from core.interface import hooks

    adapter = _adapter_mock()
    adapter.get_pending_context.return_value = ""
    adapter.resolve_prompt_submit_signal.return_value = None
    adapter.adapter_id.return_value = "codex"
    adapter.get_session_path.return_value = None
    adapter.get_sessions_dir.return_value = str(tmp_path / "sessions")

    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)
    monkeypatch.setattr("core.interface.hooks._get_owner_id", lambda: "codex-owner")
    monkeypatch.setattr("core.interface.hooks._get_deferred_notice_hint", lambda: "")
    monkeypatch.setattr("core.interface.hooks._get_deferred_notice_relay_context", lambda: "")
    monkeypatch.setattr("lib.fail_policy.is_fail_hard_enabled", lambda: False)
    monkeypatch.setattr("core.extraction_daemon.read_cursor", lambda sid: {"line_offset": 0, "transcript_path": ""})
    monkeypatch.setattr("core.extraction_daemon.write_cursor", lambda *args: None)

    def _raise_and_queue(*_args, **_kwargs):
        adapter.get_pending_context.return_value = (
            "The following are pending notifications for the user — please relay them in your response:\n\n"
            "<quaid_system_message>\n• [Quaid error] [provider] queued during recall\n</quaid_system_message>"
        )
        raise RuntimeError("provider HTTP 500")

    with patch("core.interface.api.recall_fast", side_effect=_raise_and_queue), \
         patch("core.interface.api.projects_search_docs", return_value=None):
        out, err = _run_hook_inject(
            {
                "prompt": "What do you know about Maya?",
                "session_id": "sess-codex-provider-same-turn",
                "cwd": str(tmp_path),
                "thread_id": "sess-codex-provider-same-turn",
            },
            monkeypatch=monkeypatch,
        )

    payload = json.loads(out)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "queued during recall" in context
    assert "[Quaid error] [provider]" in context
    assert "hook-inject" in err


def test_codex_session_init_passes_full_hook_payload_to_transition_check(monkeypatch, tmp_path):
    from core.interface import hooks

    projects_dir = tmp_path / "projects"
    identity_dir = tmp_path / "identity"
    projects_dir.mkdir()
    identity_dir.mkdir()

    adapter = _adapter_mock()
    adapter.projects_dir.return_value = projects_dir
    adapter.identity_dir.return_value = identity_dir
    adapter.get_base_context_files.return_value = {}
    adapter.get_cli_tools_snippet.return_value = ""
    adapter.get_pending_context.return_value = ""
    adapter.data_dir.return_value = tmp_path / "data"
    adapter.instance_root.return_value = tmp_path
    adapter.adapter_id.return_value = "codex"
    adapter.check_session_transition.return_value = None

    monkeypatch.setattr(hooks, "_get_projects_dir", lambda: projects_dir)
    monkeypatch.setattr(hooks, "_get_identity_dir", lambda: identity_dir)
    monkeypatch.setattr(hooks, "_check_janitor_health", lambda: "")
    monkeypatch.setattr(hooks, "_get_deferred_notice_hint", lambda: "")
    monkeypatch.setattr(hooks, "_build_runtime_context_block", lambda: "[Quaid runtime]")
    monkeypatch.setattr(hooks, "_current_adapter_id", lambda: "codex")
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "codex-test")
    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)
    monkeypatch.setattr("core.compatibility.notify_on_use_if_degraded", lambda *_args, **_kwargs: "")
    monkeypatch.setattr("core.extraction_daemon.ensure_alive", lambda: None)
    monkeypatch.setattr("core.extraction_daemon.read_cursor", lambda sid: {"line_offset": 0, "transcript_path": ""})
    monkeypatch.setattr("core.extraction_daemon.write_cursor", lambda *args: None)

    with patch("core.project_registry.list_projects", return_value={}):
        _run_hook_session_init(
            {"thread_id": "thread-123", "cwd": str(tmp_path)},
            monkeypatch=monkeypatch,
        )

    adapter.check_session_transition.assert_called_once_with({"thread_id": "thread-123", "cwd": str(tmp_path)})
