"""Tests for Codex-specific hook behavior."""

import fcntl
import io
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _adapter_mock():
    adapter = MagicMock()
    adapter._extract_hook_session_id = None
    adapter.adapter_id.return_value = "codex"
    codex_capabilities = {
        "deferred_notice_relay": True,
        "inject_tool_output_trace": True,
        "context_refresh_strategy": "turn_based",
        "context_refresh_guard": {"min_interval_minutes": 30, "min_turns": 50},
        "session_lookup_glob_template": "rollout-*{session_id}.jsonl",
        "session_pending_path_template": "{date_prefix}/rollout-pending-{session_id}.jsonl",
        "session_pending_default_root": "~/.codex/sessions",
        "session_fallback_path_template": "",
        "session_start_output_mode": "additional_context",
        "session_start_include_pending_context": True,
        "platform_config_scope": "codex",
        "supports_compaction_control": False,
    }

    def _get_capability(key, default=None):
        return codex_capabilities.get(key, default)

    adapter.get_capability.side_effect = _get_capability
    adapter.cached_rules_dir.return_value = None
    adapter.projects_dir.return_value = Path("/__quaid_test_missing_projects__")
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


def test_read_stdin_json_logs_malformed_payload_when_fail_open(monkeypatch, caplog):
    from core.interface import hooks

    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"{not-json")
    finally:
        os.close(write_fd)

    stdin_handle = os.fdopen(read_fd, "r", encoding="utf-8", closefd=True)
    try:
        monkeypatch.setattr(hooks.sys, "stdin", stdin_handle)
        monkeypatch.setattr(hooks, "_fail_hard_enabled", lambda: False)
        with caplog.at_level("WARNING", logger="core.interface.hooks"):
            assert hooks._read_stdin_json() == {}
    finally:
        stdin_handle.close()

    assert "Failed reading hook stdin JSON; treating payload as empty" in caplog.text


def test_read_stdin_json_restores_blocking_mode_after_unexpected_read_error(monkeypatch, caplog):
    from core.interface import hooks

    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b'{"session_id":"sess"}')
    finally:
        os.close(write_fd)

    stdin_handle = os.fdopen(read_fd, "r", encoding="utf-8", closefd=True)
    try:
        fd = stdin_handle.fileno()
        original_flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, original_flags & ~os.O_NONBLOCK)
        original_flags = fcntl.fcntl(fd, fcntl.F_GETFL)

        def raise_unexpected(_fd, _size):
            raise RuntimeError("stdin read exploded")

        monkeypatch.setattr(hooks.sys, "stdin", stdin_handle)
        monkeypatch.setattr(hooks, "_fail_hard_enabled", lambda: False)
        monkeypatch.setattr(hooks.os, "read", raise_unexpected)

        with caplog.at_level("WARNING", logger="core.interface.hooks"):
            assert hooks._read_stdin_json() == {}

        restored_flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        assert restored_flags & os.O_NONBLOCK == original_flags & os.O_NONBLOCK
    finally:
        stdin_handle.close()

    assert "Failed reading hook stdin JSON; treating payload as empty" in caplog.text


def test_read_stdin_json_raises_malformed_payload_when_fail_hard(monkeypatch):
    from core.interface import hooks

    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"{not-json")
    finally:
        os.close(write_fd)

    stdin_handle = os.fdopen(read_fd, "r", encoding="utf-8", closefd=True)
    try:
        monkeypatch.setattr(hooks.sys, "stdin", stdin_handle)
        monkeypatch.setattr(hooks, "_fail_hard_enabled", lambda: True)
        with pytest.raises(json.JSONDecodeError):
            hooks._read_stdin_json()
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
    compat_path = tmp_path / "COMPATIBILITY.md"
    compat_path.write_text("# Codex Compatibility\nUse separate instances for parallel agents.", encoding="utf-8")

    project = projects_dir / "quaid"
    project.mkdir()
    (project / "TOOLS.md").write_text("# Tools\ncodex startup docs", encoding="utf-8")

    ensure_alive_calls = []

    from core.interface import hooks
    adapter = _adapter_mock()
    adapter.projects_dir.return_value = projects_dir
    adapter.identity_dir.return_value = identity_dir
    adapter.get_base_context_files.return_value = {}
    adapter.get_compatibility_context_files.return_value = {
        str(compat_path): {"purpose": "compatibility", "maxLines": 20}
    }
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
    monkeypatch.setattr(hooks, "_fail_hard_enabled", lambda: False)
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
    assert "adapter-compatibility/COMPATIBILITY.md" in context
    assert "separate instances for parallel agents" in context
    assert ensure_alive_calls == [True]
    assert not (tmp_path / ".claude" / "rules" / "quaid-projects.md").exists()
    assert "emitted startup additionalContext" in err


def test_codex_hook_inject_turn_based_refresh_emits_context_on_first_turn_and_after_guard(monkeypatch, tmp_path):
    from core.interface import hooks

    projects_dir = tmp_path / "projects"
    identity_dir = tmp_path / "identity"
    projects_dir.mkdir()
    identity_dir.mkdir()
    (identity_dir / "USER.md").write_text("Turn refresh canary: ember-cascade", encoding="utf-8")
    (identity_dir / "SOUL.md").write_text("SOUL baseline", encoding="utf-8")
    (identity_dir / "ENVIRONMENT.md").write_text("ENV baseline", encoding="utf-8")

    project = projects_dir / "quaid"
    project.mkdir()
    (project / "TOOLS.md").write_text("# Tools\nrefresh toolset", encoding="utf-8")
    (project / "AGENTS.md").write_text("# Agents\nrefresh agents", encoding="utf-8")

    adapter = _adapter_mock()
    adapter.projects_dir.return_value = projects_dir
    adapter.identity_dir.return_value = identity_dir
    adapter.get_base_context_files.return_value = {}
    adapter.get_cli_tools_snippet.return_value = ""
    adapter.get_pending_context.return_value = ""
    adapter.data_dir.return_value = tmp_path / "data"
    adapter.instance_root.return_value = tmp_path
    adapter.adapter_id.return_value = "codex"
    adapter.resolve_prompt_submit_signal.return_value = None
    adapter.get_session_path.return_value = None
    adapter.get_sessions_dir.return_value = str(tmp_path / "sessions")

    def _capability(key, default=None):
        if key == "context_refresh_strategy":
            return "turn_based"
        if key == "context_refresh_guard":
            return {"min_turns": 2, "min_interval_minutes": 999}
        return default

    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)
    monkeypatch.setattr("lib.adapter._ensure_instance_projects_bootstrapped", lambda _adapter: None)
    monkeypatch.setattr(hooks, "_adapter_capability", _capability)
    monkeypatch.setattr(hooks, "_get_pending_context", lambda: "")
    monkeypatch.setattr(hooks, "_get_deferred_notice_hint", lambda: "")
    monkeypatch.setattr(hooks, "_get_deferred_notice_relay_context", lambda: "")
    monkeypatch.setattr(hooks, "_get_quaid_agents_baseline_context", lambda: "")
    monkeypatch.setattr(hooks, "_context_refresh_state_path", lambda: tmp_path / "data" / "context-refresh-state.json")
    monkeypatch.setattr(hooks, "_check_janitor_health", lambda: "")
    monkeypatch.setattr("core.compatibility.notify_on_use_if_degraded", lambda *_args, **_kwargs: "")
    monkeypatch.setattr("core.extraction_daemon.ensure_alive", lambda: None)
    monkeypatch.setattr("core.extraction_daemon.read_cursor", lambda _sid: {"line_offset": 0, "transcript_path": ""})
    monkeypatch.setattr("core.extraction_daemon.write_cursor", lambda *args: None)
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "codex-test")

    with patch("core.project_registry.list_projects", return_value={}):
        _run_hook_session_init(
            {"session_id": "codex-refresh-session", "cwd": str(tmp_path)},
            monkeypatch=monkeypatch,
        )

    with patch("core.interface.api.recall_fast", return_value=([], None)), \
         patch("core.interface.api.projects_search_docs", return_value=None):
        out1, _err1 = _run_hook_inject(
            {
                "prompt": "first turn",
                "session_id": "codex-refresh-session",
                "cwd": str(tmp_path),
            },
            monkeypatch=monkeypatch,
        )
        out2, _err2 = _run_hook_inject(
            {
                "prompt": "second turn",
                "session_id": "codex-refresh-session",
                "cwd": str(tmp_path),
            },
            monkeypatch=monkeypatch,
        )
        out3, _err3 = _run_hook_inject(
            {
                "prompt": "third turn",
                "session_id": "codex-refresh-session",
                "cwd": str(tmp_path),
            },
            monkeypatch=monkeypatch,
        )

    payload1 = json.loads(out1)
    context1 = payload1["hookSpecificOutput"]["additionalContext"]
    assert len(context1) < 10_000
    assert "# Quaid Refreshed Identity Context" in context1
    assert "Turn refresh canary: ember-cascade" in context1
    assert "refresh toolset" not in context1
    assert out2.strip() == ""
    payload = json.loads(out3)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "# Quaid Project Context" in context
    assert "Turn refresh canary: ember-cascade" in context
    assert "refresh toolset" in context


def test_codex_hook_inject_turn_based_refresh_repairs_legacy_state_without_identity_signature(monkeypatch, tmp_path):
    from core.interface import hooks

    projects_dir = tmp_path / "projects"
    identity_dir = tmp_path / "identity"
    data_dir = tmp_path / "data"
    projects_dir.mkdir()
    identity_dir.mkdir()
    data_dir.mkdir(parents=True, exist_ok=True)
    (identity_dir / "USER.md").write_text(
        "The office plant is named Bartholomew. It is a fiddle-leaf fig.",
        encoding="utf-8",
    )
    (identity_dir / "SOUL.md").write_text("SOUL baseline", encoding="utf-8")
    (identity_dir / "ENVIRONMENT.md").write_text("ENV baseline", encoding="utf-8")

    project = projects_dir / "quaid"
    project.mkdir()
    (project / "TOOLS.md").write_text("# Tools\nlegacy refresh toolset", encoding="utf-8")

    adapter = _adapter_mock()
    adapter.projects_dir.return_value = projects_dir
    adapter.identity_dir.return_value = identity_dir
    adapter.get_base_context_files.return_value = {}
    adapter.get_cli_tools_snippet.return_value = ""
    adapter.get_pending_context.return_value = ""
    adapter.data_dir.return_value = data_dir
    adapter.instance_root.return_value = tmp_path
    adapter.adapter_id.return_value = "codex"
    adapter.resolve_prompt_submit_signal.return_value = None
    adapter.get_session_path.return_value = None
    adapter.get_sessions_dir.return_value = str(tmp_path / "sessions")

    def _capability(key, default=None):
        if key == "context_refresh_strategy":
            return "turn_based"
        if key == "context_refresh_guard":
            return {"min_turns": 500, "min_interval_minutes": 999}
        return default

    state_path = data_dir / "context-refresh-state.json"
    state_path.write_text(
        json.dumps(
            {
                "sessions": {
                    "codex-m7-session": {
                        "turn_count": 8,
                        "last_refresh_turn": 0,
                        "last_refresh_at": int(time.time()),
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)
    monkeypatch.setattr("lib.adapter._ensure_instance_projects_bootstrapped", lambda _adapter: None)
    monkeypatch.setattr(hooks, "_adapter_capability", _capability)
    monkeypatch.setattr(hooks, "_get_pending_context", lambda: "")
    monkeypatch.setattr(hooks, "_get_deferred_notice_hint", lambda: "")
    monkeypatch.setattr(hooks, "_get_deferred_notice_relay_context", lambda: "")
    monkeypatch.setattr(hooks, "_get_quaid_agents_baseline_context", lambda: "")
    monkeypatch.setattr(hooks, "_context_refresh_state_path", lambda: state_path)
    monkeypatch.setattr("core.compatibility.notify_on_use_if_degraded", lambda *_args, **_kwargs: "")
    monkeypatch.setattr("core.extraction_daemon.ensure_alive", lambda: None)
    monkeypatch.setattr("core.extraction_daemon.read_cursor", lambda _sid: {"line_offset": 0, "transcript_path": ""})
    monkeypatch.setattr("core.extraction_daemon.write_cursor", lambda *args: None)
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "codex-test")

    with patch("core.project_registry.list_projects", return_value={}), \
         patch("core.interface.api.recall_fast", return_value=([], None)), \
         patch("core.interface.api.projects_search_docs", return_value=None):
        out, _err = _run_hook_inject(
            {
                "prompt": "What's the office plant named?",
                "session_id": "codex-m7-session",
                "cwd": str(tmp_path),
            },
            monkeypatch=monkeypatch,
        )

    payload = json.loads(out)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert len(context) < 10_000
    assert "# Quaid Refreshed Identity Context" in context
    assert "The office plant is named Bartholomew" in context
    assert "legacy refresh toolset" not in context
    refreshed_state = json.loads(state_path.read_text(encoding="utf-8"))
    entry = refreshed_state["sessions"]["codex-m7-session"]
    assert entry["last_refresh_reason"] == "identity_changed"
    assert entry["last_identity_signature"]


def test_codex_hook_inject_turn_based_refresh_replays_parallel_same_prompt(monkeypatch, tmp_path):
    from core.interface import hooks

    projects_dir = tmp_path / "projects"
    identity_dir = tmp_path / "identity"
    data_dir = tmp_path / "data"
    projects_dir.mkdir()
    identity_dir.mkdir()
    data_dir.mkdir(parents=True, exist_ok=True)
    (identity_dir / "USER.md").write_text(
        "The office plant is named Bartholomew. It is a fiddle-leaf fig.",
        encoding="utf-8",
    )
    (identity_dir / "SOUL.md").write_text("SOUL baseline", encoding="utf-8")
    (identity_dir / "ENVIRONMENT.md").write_text("ENV baseline", encoding="utf-8")

    adapter = _adapter_mock()
    adapter.projects_dir.return_value = projects_dir
    adapter.identity_dir.return_value = identity_dir
    adapter.get_base_context_files.return_value = {}
    adapter.get_cli_tools_snippet.return_value = ""
    adapter.get_pending_context.return_value = ""
    adapter.data_dir.return_value = data_dir
    adapter.instance_root.return_value = tmp_path
    adapter.adapter_id.return_value = "codex"
    adapter.resolve_prompt_submit_signal.return_value = None
    adapter.get_session_path.return_value = None
    adapter.get_sessions_dir.return_value = str(tmp_path / "sessions")

    def _capability(key, default=None):
        if key == "context_refresh_strategy":
            return "turn_based"
        if key == "context_refresh_guard":
            return {"min_turns": 500, "min_interval_minutes": 999}
        return default

    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)
    monkeypatch.setattr("lib.adapter._ensure_instance_projects_bootstrapped", lambda _adapter: None)
    monkeypatch.setattr(hooks, "_adapter_capability", _capability)
    monkeypatch.setattr(hooks, "_get_pending_context", lambda: "")
    monkeypatch.setattr(hooks, "_get_deferred_notice_hint", lambda: "")
    monkeypatch.setattr(hooks, "_get_deferred_notice_relay_context", lambda: "")
    monkeypatch.setattr(hooks, "_get_quaid_agents_baseline_context", lambda: "")
    monkeypatch.setattr(hooks, "_context_refresh_state_path", lambda: data_dir / "context-refresh-state.json")
    monkeypatch.setattr("core.compatibility.notify_on_use_if_degraded", lambda *_args, **_kwargs: "")
    monkeypatch.setattr("core.extraction_daemon.ensure_alive", lambda: None)
    monkeypatch.setattr("core.extraction_daemon.read_cursor", lambda _sid: {"line_offset": 0, "transcript_path": ""})
    monkeypatch.setattr("core.extraction_daemon.write_cursor", lambda *args: None)
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "codex-test")

    prompt = "What's the office plant named?"
    with patch("core.project_registry.list_projects", return_value={}), \
         patch("core.interface.api.recall_fast", return_value=([], None)), \
         patch("core.interface.api.projects_search_docs", return_value=None):
        out1, _err1 = _run_hook_inject(
            {"prompt": prompt, "session_id": "codex-parallel-session", "cwd": str(tmp_path)},
            monkeypatch=monkeypatch,
        )
        out2, _err2 = _run_hook_inject(
            {"prompt": prompt, "session_id": "codex-parallel-session", "cwd": str(tmp_path)},
            monkeypatch=monkeypatch,
        )
        out3, _err3 = _run_hook_inject(
            {"prompt": "different follow-up prompt", "session_id": "codex-parallel-session", "cwd": str(tmp_path)},
            monkeypatch=monkeypatch,
        )

    for raw in (out1, out2):
        payload = json.loads(raw)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert len(context) < 10_000
        assert "# Quaid Refreshed Identity Context" in context
        assert "The office plant is named Bartholomew" in context
    assert out3.strip() == ""


def test_codex_hook_inject_identity_refresh_survives_recall_init_failure_when_fail_open(monkeypatch, tmp_path):
    from core.interface import hooks

    projects_dir = tmp_path / "projects"
    identity_dir = tmp_path / "identity"
    data_dir = tmp_path / "data"
    projects_dir.mkdir()
    identity_dir.mkdir()
    data_dir.mkdir(parents=True, exist_ok=True)
    (identity_dir / "USER.md").write_text(
        "The office plant is named Bartholomew. It is a fiddle-leaf fig.",
        encoding="utf-8",
    )
    (identity_dir / "SOUL.md").write_text("SOUL baseline", encoding="utf-8")
    (identity_dir / "ENVIRONMENT.md").write_text("ENV baseline", encoding="utf-8")

    adapter = _adapter_mock()
    adapter.projects_dir.return_value = projects_dir
    adapter.identity_dir.return_value = identity_dir
    adapter.get_base_context_files.return_value = {}
    adapter.get_cli_tools_snippet.return_value = ""
    adapter.get_pending_context.return_value = ""
    adapter.data_dir.return_value = data_dir
    adapter.instance_root.return_value = tmp_path
    adapter.adapter_id.return_value = "codex"
    adapter.resolve_prompt_submit_signal.return_value = None
    adapter.get_session_path.return_value = None
    adapter.get_sessions_dir.return_value = str(tmp_path / "sessions")

    def _capability(key, default=None):
        if key == "context_refresh_strategy":
            return "turn_based"
        if key == "context_refresh_guard":
            return {"min_turns": 500, "min_interval_minutes": 999}
        return default

    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)
    monkeypatch.setattr("lib.adapter._ensure_instance_projects_bootstrapped", lambda _adapter: None)
    monkeypatch.setattr(hooks, "_adapter_capability", _capability)
    monkeypatch.setattr(hooks, "_get_pending_context", lambda: "")
    monkeypatch.setattr(hooks, "_get_deferred_notice_hint", lambda: "")
    monkeypatch.setattr(hooks, "_get_deferred_notice_relay_context", lambda: "")
    monkeypatch.setattr(hooks, "_get_quaid_agents_baseline_context", lambda: "")
    monkeypatch.setattr(hooks, "_context_refresh_state_path", lambda: data_dir / "context-refresh-state.json")
    monkeypatch.setattr("core.compatibility.notify_on_use_if_degraded", lambda *_args, **_kwargs: "")
    monkeypatch.setattr("core.extraction_daemon.ensure_alive", lambda: None)
    monkeypatch.setattr("core.extraction_daemon.read_cursor", lambda _sid: {"line_offset": 0, "transcript_path": ""})
    monkeypatch.setattr("core.extraction_daemon.write_cursor", lambda *args: None)
    monkeypatch.setattr("lib.fail_policy.is_fail_hard_enabled", lambda: False)
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "codex-test")

    with patch(
        "core.interface.api.recall_fast",
        side_effect=ValueError(
            "Plugin contract init failures: Plugin memorydb.core init hook failed "
            "(on_init): unable to open database file"
        ),
    ), patch("core.interface.api.projects_search_docs", return_value=None):
        out, err = _run_hook_inject(
            {
                "prompt": "What is the office plant named?",
                "session_id": "codex-m7-recall-init-failure",
                "cwd": str(tmp_path),
            },
            monkeypatch=monkeypatch,
        )

    payload = json.loads(out)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "# Quaid Refreshed Identity Context" in context
    assert "The office plant is named Bartholomew" in context
    assert "unable to open database file" not in context
    assert err == ""


def test_codex_hook_inject_recall_init_failure_raises_when_fail_hard_enabled(monkeypatch, tmp_path):
    from core.interface import hooks

    adapter = _adapter_mock()
    adapter.get_pending_context.return_value = ""
    adapter.data_dir.return_value = tmp_path / "data"
    adapter.instance_root.return_value = tmp_path
    adapter.adapter_id.return_value = "codex"
    adapter.resolve_prompt_submit_signal.return_value = None
    adapter.get_session_path.return_value = None
    adapter.get_sessions_dir.return_value = str(tmp_path / "sessions")

    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)
    monkeypatch.setattr("lib.adapter._ensure_instance_projects_bootstrapped", lambda _adapter: None)
    monkeypatch.setattr(hooks, "_adapter_capability", lambda _key, default=None: default)
    monkeypatch.setattr(hooks, "_get_pending_context", lambda: "")
    monkeypatch.setattr(hooks, "_get_deferred_notice_hint", lambda: "")
    monkeypatch.setattr(hooks, "_get_deferred_notice_relay_context", lambda: "")
    monkeypatch.setattr(hooks, "_get_quaid_agents_baseline_context", lambda: "")
    monkeypatch.setattr("core.compatibility.notify_on_use_if_degraded", lambda *_args, **_kwargs: "")
    monkeypatch.setattr("core.extraction_daemon.ensure_alive", lambda: None)
    monkeypatch.setattr("core.extraction_daemon.read_cursor", lambda _sid: {"line_offset": 0, "transcript_path": ""})
    monkeypatch.setattr("core.extraction_daemon.write_cursor", lambda *args: None)
    monkeypatch.setattr("lib.fail_policy.is_fail_hard_enabled", lambda: True)
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "codex-test")

    with patch(
        "core.interface.api.recall_fast",
        side_effect=ValueError(
            "Plugin contract init failures: Plugin memorydb.core init hook failed "
            "(on_init): unable to open database file"
        ),
    ), patch("core.interface.api.projects_search_docs", return_value=None), pytest.raises(
        ValueError,
        match="unable to open database file",
    ):
        _run_hook_inject(
            {
                "prompt": "What is the office plant named?",
                "session_id": "codex-m7-recall-init-failhard",
                "cwd": str(tmp_path),
            },
            monkeypatch=monkeypatch,
        )


def test_codex_hook_inject_turn_based_refresh_emits_context_after_timeout_marker(monkeypatch, tmp_path):
    from core.interface import hooks

    projects_dir = tmp_path / "projects"
    identity_dir = tmp_path / "identity"
    data_dir = tmp_path / "data"
    projects_dir.mkdir()
    identity_dir.mkdir()
    data_dir.mkdir(parents=True, exist_ok=True)
    (identity_dir / "USER.md").write_text("Timeout refresh canary: bartholomew", encoding="utf-8")
    (identity_dir / "SOUL.md").write_text("SOUL baseline", encoding="utf-8")
    (identity_dir / "ENVIRONMENT.md").write_text("ENV baseline", encoding="utf-8")

    project = projects_dir / "quaid"
    project.mkdir()
    (project / "TOOLS.md").write_text("# Tools\ntimeout refresh toolset", encoding="utf-8")

    adapter = _adapter_mock()
    adapter.projects_dir.return_value = projects_dir
    adapter.identity_dir.return_value = identity_dir
    adapter.get_base_context_files.return_value = {}
    adapter.get_cli_tools_snippet.return_value = ""
    adapter.get_pending_context.return_value = ""
    adapter.data_dir.return_value = data_dir
    adapter.instance_root.return_value = tmp_path
    adapter.adapter_id.return_value = "codex"
    adapter.resolve_prompt_submit_signal.return_value = None
    adapter.get_session_path.return_value = None
    adapter.get_sessions_dir.return_value = str(tmp_path / "sessions")

    def _capability(key, default=None):
        if key == "context_refresh_strategy":
            return "turn_based"
        if key == "context_refresh_guard":
            return {"min_turns": 500, "min_interval_minutes": 999}
        return default

    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)
    monkeypatch.setattr("lib.adapter._ensure_instance_projects_bootstrapped", lambda _adapter: None)
    monkeypatch.setattr(hooks, "_adapter_capability", _capability)
    monkeypatch.setattr(hooks, "_get_pending_context", lambda: "")
    monkeypatch.setattr(hooks, "_get_deferred_notice_hint", lambda: "")
    monkeypatch.setattr(hooks, "_get_deferred_notice_relay_context", lambda: "")
    monkeypatch.setattr(hooks, "_get_quaid_agents_baseline_context", lambda: "")
    monkeypatch.setattr(hooks, "_context_refresh_state_path", lambda: data_dir / "context-refresh-state.json")
    monkeypatch.setattr(hooks, "_check_janitor_health", lambda: "")
    monkeypatch.setattr("core.compatibility.notify_on_use_if_degraded", lambda *_args, **_kwargs: "")
    monkeypatch.setattr("core.extraction_daemon.ensure_alive", lambda: None)
    monkeypatch.setattr("core.extraction_daemon.read_cursor", lambda _sid: {"line_offset": 0, "transcript_path": ""})
    monkeypatch.setattr("core.extraction_daemon.write_cursor", lambda *args: None)
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "codex-test")

    with patch("core.project_registry.list_projects", return_value={}):
        _run_hook_session_init(
            {"session_id": "codex-timeout-refresh", "cwd": str(tmp_path)},
            monkeypatch=monkeypatch,
        )

    timeout_marker = data_dir / "context-refresh-timeout" / "codex-timeout-refresh.json"
    timeout_marker.parent.mkdir(parents=True, exist_ok=True)
    timeout_marker.write_text(
        json.dumps(
            {
                "session_id": "codex-timeout-refresh",
                "timeout_completed_at": "2026-04-17T09:03:59Z",
            }
        ),
        encoding="utf-8",
    )

    with patch("core.interface.api.recall_fast", return_value=([], None)), \
         patch("core.interface.api.projects_search_docs", return_value=None):
        out, _err = _run_hook_inject(
            {
                "prompt": "what is the office plant named?",
                "session_id": "codex-timeout-refresh",
                "cwd": str(tmp_path),
            },
            monkeypatch=monkeypatch,
        )

    payload = json.loads(out)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "# Quaid Project Context" in context
    assert "Timeout refresh canary: bartholomew" in context
    assert "timeout refresh toolset" in context
    assert not timeout_marker.exists()


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
    monkeypatch.setattr(hooks, "_fail_hard_enabled", lambda: False)
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
    assert "RESPONSE CONTRACT" in context
    assert 'first sentence to the user must say: "Quaid memory recall is currently degraded' in context
    assert "[Quaid error]" in context
    assert "[provider]" in context
    assert "invalid-model-xyzzy" in context
    assert "hook-inject" in err


def test_codex_hook_inject_provider_error_raises_when_fail_hard_enabled(monkeypatch, tmp_path):
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
    queued = []
    adapter.notify.side_effect = lambda message, **kwargs: queued.append((message, kwargs)) or True

    with patch(
        "core.interface.api.recall_fast",
        side_effect=RuntimeError(
            "Quaid could not access its fast language model provider: codex gateway HTTP 404 model=invalid-model-xyzzy"
        ),
    ), patch("core.interface.api.projects_search_docs", return_value=None), \
         pytest.raises(RuntimeError, match="invalid-model-xyzzy"):
        _run_hook_inject(
            {
                "prompt": "What do you know about Maya?",
                "session_id": "sess-codex-provider-failhard",
                "cwd": str(tmp_path),
            },
            monkeypatch=monkeypatch,
        )

    assert queued == []


def test_codex_hook_inject_probes_prompt_model_config(monkeypatch, tmp_path):
    from core.interface import hooks

    adapter = _adapter_mock()
    adapter.get_pending_context.return_value = ""
    adapter.resolve_prompt_submit_signal.return_value = None
    adapter.adapter_id.return_value = "codex"
    adapter.get_session_path.return_value = None
    adapter.get_sessions_dir.return_value = str(tmp_path / "sessions")
    adapter.instance_root.return_value = tmp_path
    adapter.data_dir.return_value = tmp_path / "data"

    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)
    monkeypatch.setattr("lib.adapter._ensure_instance_projects_bootstrapped", lambda _adapter: None)
    monkeypatch.setattr("core.extraction_daemon.ensure_alive", lambda: None)
    monkeypatch.setattr("core.extraction_daemon.read_cursor", lambda sid: {"line_offset": 0, "transcript_path": ""})
    monkeypatch.setattr("core.extraction_daemon.write_cursor", lambda *args: None)
    monkeypatch.setattr("lib.fail_policy.is_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(hooks, "_get_pending_context", lambda: "")
    monkeypatch.setattr(hooks, "_get_deferred_notice_hint", lambda: "")
    monkeypatch.setattr(hooks, "_get_owner_id", lambda: "codex-owner")
    monkeypatch.setattr(
        hooks,
        "_adapter_capability",
        lambda key, default=None: True if key == "prompt_model_config_probe" else default,
    )
    monkeypatch.setattr(
        hooks,
        "_runtime_config_snapshot",
        lambda: ((str(tmp_path / "codex" / "config.json"), 123),),
    )

    with patch(
        "lib.llm_clients.call_fast_reasoning",
        side_effect=RuntimeError(
            "Quaid could not access its fast language model provider: model=invalid-model-m6-probe"
        ),
    ) as probe, patch("core.interface.api.recall_fast", return_value=([], None)), \
         patch("core.interface.api.projects_search_docs", return_value=None):
        out, _err = _run_hook_inject(
            {
                "prompt": "What do you know about Maya?",
                "session_id": "sess-codex-provider-probe",
                "cwd": str(tmp_path),
            },
            monkeypatch=monkeypatch,
        )

    probe.assert_called_once()
    assert probe.call_args.kwargs["timeout"] == 8
    payload = json.loads(out)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "RESPONSE CONTRACT" in context
    assert 'first sentence to the user must say: "Quaid memory recall is currently degraded' in context
    assert "[Quaid error] [provider]" in context
    assert "Tell the user: Quaid memory recall is currently degraded" in context
    assert "invalid-model-m6-probe" in context


def test_codex_prompt_model_recovery_clears_sticky_provider_notices(monkeypatch, tmp_path):
    from core.interface import hooks

    adapter = _adapter_mock()
    adapter.adapter_id.return_value = "codex"
    adapter.data_dir.return_value = tmp_path / "data"
    adapter.instance_root.return_value = tmp_path / "instance"

    pending_path = tmp_path / "data" / "codex-pending-notifications.jsonl"
    pending_path.parent.mkdir(parents=True)
    pending_path.write_text(
        "\n".join(
            [
                json.dumps({
                    "message": "[Quaid error] [provider] invalid-model-m6-probe persisted",
                    "source": "provider",
                }),
                json.dumps({
                    "message": "[Quaid info] [janitor] keep this notice",
                    "source": "janitor",
                }),
            ]
        ) + "\n",
        encoding="utf-8",
    )

    deferred_path = tmp_path / "instance" / ".runtime" / "notes" / "delayed-llm-requests.json"
    deferred_path.parent.mkdir(parents=True)
    deferred_path.write_text(
        json.dumps({
            "version": 1,
            "requests": [
                {
                    "id": "provider-old",
                    "dedupe_key": "provider-old",
                    "source": "provider",
                    "kind": "provider",
                    "priority": "high",
                    "status": "pending",
                    "message": "deferred invalid-model-m6-probe",
                },
                {
                    "id": "janitor-keep",
                    "dedupe_key": "janitor-keep",
                    "source": "janitor",
                    "kind": "janitor",
                    "priority": "normal",
                    "status": "pending",
                    "message": "keep janitor notice",
                },
            ],
        }),
        encoding="utf-8",
    )

    config_path = tmp_path / "codex" / "config.json"
    config_path.parent.mkdir()
    config_mtime = 1
    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)
    monkeypatch.setattr("lib.agent_notice.get_adapter", lambda: adapter)
    monkeypatch.setattr(
        hooks,
        "_adapter_capability",
        lambda key, default=None: True if key == "prompt_model_config_probe" else default,
    )
    monkeypatch.setattr(
        hooks,
        "_runtime_config_snapshot",
        lambda: ((str(config_path), config_mtime),),
    )

    with patch(
        "lib.llm_clients.call_fast_reasoning",
        side_effect=RuntimeError("model=invalid-model-m6-probe"),
    ), patch("core.interface.hooks._fail_hard_enabled", return_value=False):
        first_notice = hooks._validate_prompt_model_config_for_hook("codex")

    assert "invalid-model-m6-probe" in first_notice

    config_mtime = 2
    with patch("lib.llm_clients.call_fast_reasoning", return_value="OK"):
        recovery_notice = hooks._validate_prompt_model_config_for_hook("codex")

    assert "healthy again" in recovery_notice
    assert "provider-error notices" not in recovery_notice
    pending_text = pending_path.read_text(encoding="utf-8")
    assert "invalid-model-m6-probe persisted" not in pending_text
    assert "keep this notice" in pending_text
    deferred_payload = json.loads(deferred_path.read_text(encoding="utf-8"))
    messages = [item["message"] for item in deferred_payload["requests"]]
    assert "deferred invalid-model-m6-probe" not in messages
    assert "keep janitor notice" in messages


def test_codex_provider_failure_does_not_relay_after_next_successful_turn(monkeypatch, tmp_path):
    from core.interface import hooks

    adapter = _adapter_mock()
    adapter.get_pending_context.return_value = ""
    adapter.resolve_prompt_submit_signal.return_value = None
    adapter.adapter_id.return_value = "codex"
    adapter.get_session_path.return_value = None
    adapter.get_sessions_dir.return_value = str(tmp_path / "sessions")
    adapter.instance_root.return_value = tmp_path
    adapter.data_dir.return_value = tmp_path / "data"

    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)
    monkeypatch.setattr("lib.adapter._ensure_instance_projects_bootstrapped", lambda _adapter: None)
    monkeypatch.setattr("core.extraction_daemon.read_cursor", lambda sid: {"line_offset": 0, "transcript_path": ""})
    monkeypatch.setattr("core.extraction_daemon.write_cursor", lambda *args: None)
    monkeypatch.setattr("lib.fail_policy.is_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(hooks, "_get_deferred_notice_hint", lambda: "")
    monkeypatch.setattr(hooks, "_get_owner_id", lambda: "codex-owner")
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "codex-test")
    pending_notices = []

    def _queue_pending(message, **_kwargs):
        pending_notices.append(str(message))
        return True

    def _drain_pending():
        if not pending_notices:
            return ""
        messages = list(pending_notices)
        pending_notices.clear()
        body = "\n".join(f"• {message}" for message in messages)
        return (
            "The following are pending notifications for the user — please relay them in your response:\n\n"
            f"<quaid_system_message>\n{body}\n</quaid_system_message>"
        )

    adapter.notify.side_effect = _queue_pending
    adapter.get_pending_context.side_effect = _drain_pending

    with patch(
        "core.interface.api.recall_fast",
        side_effect=RuntimeError(
            "Quaid could not access its fast language model provider: codex gateway HTTP 404 model=invalid-model-xyzzy"
        ),
    ), patch("core.interface.api.projects_search_docs", return_value=None):
        out1, _err1 = _run_hook_inject(
            {
                "prompt": "What do you know about Maya?",
                "session_id": "sess-codex-provider-failhard-relay",
                "cwd": str(tmp_path),
            },
            monkeypatch=monkeypatch,
        )
    payload1 = json.loads(out1)
    context1 = payload1["hookSpecificOutput"]["additionalContext"]
    assert "[Quaid error] [provider]" in context1
    assert "invalid-model-xyzzy" in context1

    with patch("core.interface.api.recall_fast", return_value=([], None)), \
         patch("core.interface.api.projects_search_docs", return_value=None):
        out, _err = _run_hook_inject(
            {
                "prompt": "hello on next turn",
                "session_id": "sess-codex-provider-failhard-relay",
                "cwd": str(tmp_path),
            },
            monkeypatch=monkeypatch,
        )

    payload = json.loads(out) if out.strip() else {}
    context = payload.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "[Quaid error] [provider]" not in context
    assert "invalid-model-xyzzy" not in context


def test_codex_hook_inject_traces_raw_tool_output_when_present(monkeypatch, tmp_path):
    from core.interface import hooks

    adapter = _adapter_mock()
    adapter.get_pending_context.return_value = ""
    adapter.resolve_prompt_submit_signal.return_value = None
    adapter.adapter_id.return_value = "codex"
    adapter.get_session_path.return_value = None
    adapter.get_sessions_dir.return_value = str(tmp_path / "sessions")

    trace_entries = []

    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)
    monkeypatch.setattr("lib.adapter._ensure_instance_projects_bootstrapped", lambda _adapter: None)
    monkeypatch.setattr("core.extraction_daemon.read_cursor", lambda sid: {"line_offset": 0, "transcript_path": ""})
    monkeypatch.setattr("core.extraction_daemon.write_cursor", lambda *args: None)
    monkeypatch.setattr(hooks, "_get_pending_context", lambda: "")
    monkeypatch.setattr(hooks, "_get_deferred_notice_relay_context", lambda: "")
    monkeypatch.setattr(hooks, "_get_deferred_notice_hint", lambda: "")
    monkeypatch.setattr(hooks, "_write_hook_trace", lambda event, payload=None: trace_entries.append((event, payload or {})))

    with patch("core.interface.api.recall_fast", return_value=([], None)), \
         patch("core.interface.api.projects_search_docs", return_value=None):
        _run_hook_inject(
            {
                "prompt": "summarize the output",
                "session_id": "sess-codex-tool-trace",
                "cwd": str(tmp_path),
                "tool_output": "quaid project list\nhello-cli\nquaid-live-cli",
            },
            monkeypatch=monkeypatch,
        )

    payloads = [payload for event, payload in trace_entries if event == "hook.inject.codex_payload"]
    assert payloads
    payload = payloads[-1]
    assert payload["tool_output_len"] > 0
    assert payload["tool_output_truncated"] is False
    assert "tool_output" in payload["tool_output_keys"]
    assert "quaid-live-cli" in payload["tool_output"]


def test_codex_tool_output_trace_redacts_sensitive_lines():
    from core.interface import hooks

    payload = hooks._extract_codex_tool_output_trace(
        {
            "tool_output": "\n".join(
                [
                    "quaid project list",
                    "OPENAI_API_KEY=sk-live-abcdefghijklmnopqrstuvwxyz",
                    "safe-project",
                ]
            )
        }
    )

    assert payload["tool_output_redacted"] is True
    assert payload["tool_output_redacted_lines"] == 1
    assert "quaid project list" in payload["tool_output"]
    assert "safe-project" in payload["tool_output"]
    assert "OPENAI_API_KEY" not in payload["tool_output"]
    assert "sk-live" not in payload["tool_output"]


def test_hook_inject_adds_project_list_names_only_hint(monkeypatch, tmp_path):
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
    monkeypatch.setattr(hooks, "_get_pending_context", lambda: "")
    monkeypatch.setattr(hooks, "_get_deferred_notice_hint", lambda: "")

    with patch("core.interface.api.recall_fast", return_value=([], None)), \
         patch("core.interface.api.projects_search_docs", return_value=None):
        out, _err = _run_hook_inject(
            {
                "prompt": "summarize the output",
                "session_id": "sess-codex-tool-fidelity",
                "cwd": str(tmp_path),
                "tool_output": "quaid project list\nhello-cli\nquaid-live-cli",
            },
            monkeypatch=monkeypatch,
        )

    payload = json.loads(out)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "[Tool output reminder]" in context
    assert "quaid project list --names-only" in context


def test_hook_inject_skips_project_list_hint_when_names_only_used(monkeypatch, tmp_path):
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
    monkeypatch.setattr(hooks, "_get_pending_context", lambda: "")
    monkeypatch.setattr(hooks, "_get_deferred_notice_hint", lambda: "")

    with patch("core.interface.api.recall_fast", return_value=([], None)), \
         patch("core.interface.api.projects_search_docs", return_value=None):
        out, _err = _run_hook_inject(
            {
                "prompt": "summarize output",
                "session_id": "sess-project-list-names-only",
                "cwd": str(tmp_path),
                "tool_output": "quaid project list --names-only\nhello-cli\nquaid-live-cli",
            },
            monkeypatch=monkeypatch,
        )

    assert out.strip() == ""


def test_hook_inject_surfaces_unlinked_project_scope_hint(monkeypatch, tmp_path):
    from core.interface import hooks

    adapter = _adapter_mock()
    adapter.get_pending_context.return_value = ""
    adapter.resolve_prompt_submit_signal.return_value = None
    adapter.adapter_id.return_value = "codex"
    adapter.get_session_path.return_value = None
    adapter.get_sessions_dir.return_value = str(tmp_path / "sessions")

    project_path = tmp_path / "projects" / "livetest-agentmsg-xp"
    source_root = tmp_path / "projects" / "livetest-agentmsg-xp-src"
    docs_bundle = {
        "chunks": [],
        "project": None,
        "project_md": None,
        "telemetry": {
            "scope_hint": {
                "type": "unlinked_project_candidates",
                "message": (
                    "No docs matched inside currently linked projects. "
                    "Likely unlinked project candidates were found."
                ),
                "requested_project": None,
                "linked_projects": ["quaid"],
                "candidates": [
                    {
                        "project": "livetest-agentmsg-xp",
                        "path": str(project_path),
                        "source_root": str(source_root),
                        "canonical_path": str(project_path),
                        "score": 0.91,
                    }
                ],
            }
        },
    }

    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)
    monkeypatch.setattr("lib.adapter._ensure_instance_projects_bootstrapped", lambda _adapter: None)
    monkeypatch.setattr("core.extraction_daemon.read_cursor", lambda sid: {"line_offset": 0, "transcript_path": ""})
    monkeypatch.setattr("core.extraction_daemon.write_cursor", lambda *args: None)
    monkeypatch.setattr(hooks, "_get_pending_context", lambda: "")
    monkeypatch.setattr(hooks, "_get_deferred_notice_hint", lambda: "")

    with patch("core.interface.api.recall_fast", return_value=([], None)), \
         patch("core.interface.api.projects_search_docs", return_value=docs_bundle):
        out, _err = _run_hook_inject(
            {
                "prompt": "I just want one fact from the livetest-agentmsg-xp project. What does Ember Glass mean?",
                "session_id": "sess-project-scope-hint",
                "cwd": str(tmp_path),
            },
            monkeypatch=monkeypatch,
        )

    payload = json.loads(out)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "[Quaid Project Discovery]" in context
    assert "livetest-agentmsg-xp" in context
    assert f"source_root={source_root}" in context
    assert f"canonical_path={project_path}" in context
    assert "currently_linked_projects: quaid" in context
    assert "do not run `quaid project link`" in context


def test_format_project_docs_scope_hint_skips_unusable_candidates():
    from core.interface import hooks

    context = hooks._format_project_docs_scope_hint({
        "telemetry": {
            "scope_hint": {
                "type": "unlinked_project_candidates",
                "candidates": [{"score": 0.5}],
            }
        }
    })

    assert context == ""


def test_format_project_docs_scope_hint_skips_unknown_hint_type():
    from core.interface import hooks

    context = hooks._format_project_docs_scope_hint({
        "telemetry": {
            "scope_hint": {
                "type": "linked_project_candidates",
                "candidates": [{"project": "livetest-agentmsg-xp"}],
            }
        }
    })

    assert context == ""


def test_hook_inject_appends_scope_hint_to_project_docs(monkeypatch, tmp_path):
    from core.interface import hooks

    adapter = _adapter_mock()
    adapter.get_pending_context.return_value = ""
    adapter.resolve_prompt_submit_signal.return_value = None
    adapter.adapter_id.return_value = "codex"
    adapter.get_session_path.return_value = None
    adapter.get_sessions_dir.return_value = str(tmp_path / "sessions")

    docs_bundle = {
        "chunks": [
            {
                "content": "The codeword Ember Glass means pager escalation level 2.",
                "source": str(tmp_path / "projects" / "livetest-agentmsg-xp" / "docs" / "pager.md"),
                "similarity": 0.91,
            }
        ],
        "project": "livetest-agentmsg-xp",
        "project_md": None,
        "telemetry": {
            "scope_hint": {
                "type": "unlinked_project_candidates",
                "message": "Do not trust upstream wording for link policy.",
                "candidates": [{"project": "livetest-agentmsg-xp"}],
            }
        },
    }

    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)
    monkeypatch.setattr("lib.adapter._ensure_instance_projects_bootstrapped", lambda _adapter: None)
    monkeypatch.setattr("core.extraction_daemon.read_cursor", lambda sid: {"line_offset": 0, "transcript_path": ""})
    monkeypatch.setattr("core.extraction_daemon.write_cursor", lambda *args: None)
    monkeypatch.setattr(hooks, "_get_pending_context", lambda: "")
    monkeypatch.setattr(hooks, "_get_deferred_notice_hint", lambda: "")

    with patch("core.interface.api.recall_fast", return_value=([], None)), \
         patch("core.interface.api.projects_search_docs", return_value=docs_bundle):
        out, _err = _run_hook_inject(
            {
                "prompt": "What does Ember Glass mean?",
                "session_id": "sess-project-docs-and-scope-hint",
                "cwd": str(tmp_path),
            },
            monkeypatch=monkeypatch,
        )

    payload = json.loads(out)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "[Quaid Project Docs: livetest-agentmsg-xp]" in context
    assert "Ember Glass means pager escalation level 2" in context
    assert "[Quaid Project Discovery]" in context
    assert "Do not trust upstream wording" not in context
    assert "do not run `quaid project link`" in context


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
    monkeypatch.setattr(hooks, "_get_deferred_notice_relay_context", lambda: "")
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


def test_deferred_notice_hint_logs_failure_when_fail_open(monkeypatch, caplog):
    from core.interface import hooks

    monkeypatch.setattr(hooks, "_fail_hard_enabled", lambda: False)
    with caplog.at_level("WARNING", logger="core.interface.hooks"), \
         patch("lib.runtime_context.format_deferred_notice_hint", side_effect=RuntimeError("hint broken")):
        context = hooks._get_deferred_notice_hint()

    assert context == ""
    assert "Failed reading deferred notice hint: hint broken" in caplog.text


def test_deferred_notice_hint_raises_failure_when_fail_hard(monkeypatch, caplog):
    from core.interface import hooks

    monkeypatch.setattr(hooks, "_fail_hard_enabled", lambda: True)
    with caplog.at_level("WARNING", logger="core.interface.hooks"), \
         patch("lib.runtime_context.format_deferred_notice_hint", side_effect=RuntimeError("hint broken")), \
         pytest.raises(RuntimeError, match="hint broken"):
        hooks._get_deferred_notice_hint()

    assert "Failed reading deferred notice hint: hint broken" in caplog.text


def test_codex_deferred_notice_relay_context_logs_drain_failure(monkeypatch, caplog):
    from core.interface import hooks

    monkeypatch.setattr(hooks, "_adapter_capability", lambda key, default=None: True if key == "deferred_notice_relay" else default)
    monkeypatch.setattr(hooks, "_fail_hard_enabled", lambda: False)
    with caplog.at_level("WARNING", logger="core.interface.hooks"), \
         patch("lib.runtime_context.drain_deferred_notices", side_effect=RuntimeError("drain broken")):
        context = hooks._get_deferred_notice_relay_context()

    assert context == ""
    assert "Failed draining deferred notice relay context: drain broken" in caplog.text


def test_codex_deferred_notice_relay_context_raises_drain_failure_when_failhard(monkeypatch, caplog):
    from core.interface import hooks

    monkeypatch.setattr(hooks, "_adapter_capability", lambda key, default=None: True if key == "deferred_notice_relay" else default)
    monkeypatch.setattr(hooks, "_fail_hard_enabled", lambda: True)
    with caplog.at_level("WARNING", logger="core.interface.hooks"), \
         patch("lib.runtime_context.drain_deferred_notices", side_effect=RuntimeError("drain broken")), \
         pytest.raises(RuntimeError, match="drain broken"):
        hooks._get_deferred_notice_relay_context()

    assert "Failed draining deferred notice relay context: drain broken" in caplog.text


def test_clear_provider_notice_state_logs_failures(monkeypatch, caplog):
    from core.interface import hooks

    def fail_pending(**_kwargs):
        raise RuntimeError("pending broken")

    def fail_deferred(**_kwargs):
        raise RuntimeError("deferred broken")

    monkeypatch.setattr("lib.agent_notice.clear_pending_notices_by_source", fail_pending)
    monkeypatch.setattr("lib.agent_notice.clear_deferred_notices_by_source", fail_deferred)
    monkeypatch.setattr(hooks, "_fail_hard_enabled", lambda: False)

    with caplog.at_level("WARNING", logger="core.interface.hooks"):
        cleared = hooks._clear_provider_notice_state()

    assert cleared == {"pending": 0, "deferred": 0}
    assert "Failed clearing pending provider notices: pending broken" in caplog.text
    assert "Failed clearing deferred provider notices: deferred broken" in caplog.text


def test_clear_provider_notice_state_raises_pending_failure_when_fail_hard(monkeypatch, caplog):
    from core.interface import hooks

    def fail_pending(**_kwargs):
        raise RuntimeError("pending broken")

    monkeypatch.setattr("lib.agent_notice.clear_pending_notices_by_source", fail_pending)
    monkeypatch.setattr(hooks, "_fail_hard_enabled", lambda: True)

    with caplog.at_level("WARNING", logger="core.interface.hooks"), pytest.raises(RuntimeError, match="pending broken"):
        hooks._clear_provider_notice_state()

    assert "Failed clearing pending provider notices: pending broken" in caplog.text


def test_clear_provider_notice_state_raises_deferred_failure_when_fail_hard(monkeypatch, caplog):
    from core.interface import hooks

    def clear_pending(**_kwargs):
        return 1

    def fail_deferred(**_kwargs):
        raise RuntimeError("deferred broken")

    monkeypatch.setattr("lib.agent_notice.clear_pending_notices_by_source", clear_pending)
    monkeypatch.setattr("lib.agent_notice.clear_deferred_notices_by_source", fail_deferred)
    monkeypatch.setattr(hooks, "_fail_hard_enabled", lambda: True)

    with caplog.at_level("WARNING", logger="core.interface.hooks"), pytest.raises(RuntimeError, match="deferred broken"):
        hooks._clear_provider_notice_state()

    assert "Failed clearing deferred provider notices: deferred broken" in caplog.text


def test_codex_hook_inject_relays_deferred_notice_before_recall_work(monkeypatch, tmp_path):
    from core.interface import hooks

    adapter = _adapter_mock()
    adapter.get_pending_context.return_value = ""
    adapter.resolve_prompt_submit_signal.return_value = None
    adapter.adapter_id.return_value = "codex"
    adapter.get_session_path.return_value = None
    adapter.get_sessions_dir.return_value = str(tmp_path / "sessions")

    trace_events = []
    call_order = []
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "codex-test")
    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)
    monkeypatch.setattr("lib.adapter._ensure_instance_projects_bootstrapped", lambda _adapter: None)
    monkeypatch.setattr("core.extraction_daemon.ensure_alive", lambda: call_order.append("daemon"))
    monkeypatch.setattr("core.extraction_daemon.read_cursor", lambda sid: {"line_offset": 0, "transcript_path": ""})
    monkeypatch.setattr("core.extraction_daemon.write_cursor", lambda *args: None)
    monkeypatch.setattr(hooks, "_get_pending_context", lambda: "")

    def deferred_relay():
        call_order.append("relay")
        return (
            "MANDATORY: Quaid just drained deferred notices for the human user. "
            "Start your next response by briefly relaying them, then answer the user's current message.\n\n"
            "<quaid_system_message>\n• First-turn relay: brass lantern is ready.\n</quaid_system_message>"
        )

    monkeypatch.setattr(hooks, "_get_deferred_notice_relay_context", deferred_relay)
    monkeypatch.setattr(hooks, "_get_deferred_notice_hint", lambda: "")
    monkeypatch.setattr(hooks, "_get_owner_id", lambda: "codex-owner")
    monkeypatch.setattr(
        hooks,
        "_write_hook_trace",
        lambda event, payload=None: trace_events.append((event, payload or {})),
    )

    def recall_fast(**_kwargs):
        call_order.append("recall")
        return [
            {
                "id": "m-codex-grinder",
                "text": "Espresso setup uses a Baratza Encore grinder.",
                "similarity": 0.96,
                "category": "fact",
            }
        ], {"mode": "fast"}

    with patch("core.interface.api.recall_fast", side_effect=recall_fast) as recall, \
         patch("core.interface.api.projects_search_docs", return_value=None):
        out, _err = _run_hook_inject(
            {
                "prompt": "What grinder do I use for my espresso setup?",
                "session_id": "sess-codex-deferred-fastpath",
                "cwd": str(tmp_path),
                "thread_id": "sess-codex-deferred-fastpath",
            },
            monkeypatch=monkeypatch,
        )

    recall.assert_called_once()
    assert call_order[:3] == ["relay", "daemon", "recall"]
    payload = json.loads(out)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "MANDATORY: Quaid just drained deferred notices" in context
    assert "brass lantern" in context
    assert "Baratza Encore" in context
    assert any(
        event == "hook.inject.deferred_relay_predrained" and _payload.get("phase") == "pre_probe"
        for event, _payload in trace_events
    )
    assert any(event == "hook.inject.context_emitted" for event, _payload in trace_events)
    log_path = tmp_path / "instances" / "codex-test" / "logs" / "daemon" / "preinject.jsonl"
    entry = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert entry["injected"][0]["text"] == "Espresso setup uses a Baratza Encore grinder."


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
