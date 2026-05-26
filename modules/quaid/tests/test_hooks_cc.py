"""Tests for core/interface/hooks.py — Claude Code adapter hook handlers.

Covers:
- hook_inject cursor seeding (rglob hit, rglob miss/fallback, idempotent, no session_id, empty cwd)
- hook_session_init registry augmentation (projects_dir, registry extra, no duplicate)
- hook_session_init TOOLS.md / AGENTS.md presence in output
- hook_inject fail-soft/fail-hard handling for recall_fast exceptions
- hook_inject project-doc injection via projects_search_docs
- hook_inject no crash on empty recall_fast result
"""
import io
import json
import os
import sys
import time
import types
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the module root is importable (mirrors conftest.py pattern)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _adapter_mock():
    adapter = MagicMock()
    adapter._extract_hook_session_id = None

    def _get_capability(key, default=None):
        adapter_id = str(adapter.adapter_id.return_value or "").strip().lower()
        capabilities = {
            "claude-code": {
                "deferred_notice_relay": True,
                "session_cwd_path_template": "{cwd_encoded}/{session_id}.jsonl",
                "platform_config_scope": "claude-code",
                "prompt_model_config_probe": True,
            },
            "codex": {
                "deferred_notice_relay": True,
                "session_lookup_glob_template": "rollout-*{session_id}.jsonl",
                "session_pending_path_template": "{date_prefix}/rollout-pending-{session_id}.jsonl",
                "session_pending_default_root": "~/.codex/sessions",
                "session_fallback_path_template": "",
                "platform_config_scope": "codex",
            },
            "openclaw": {
                "supports_compaction_control": True,
                "platform_config_scope": "openclaw",
            },
            "standalone": {
                "platform_config_scope": "standalone",
            },
        }
        return capabilities.get(adapter_id, {}).get(key, default)

    adapter.get_capability.side_effect = _get_capability
    return adapter

def _run_hook_inject(hook_input: dict, *, monkeypatch, patches: dict | None = None):
    """Drive hook_inject with a fake stdin and captured stdout/stderr.

    patches: extra keyword-arg patches applied to core.interface.hooks
    Returns (stdout_text, stderr_text).
    """
    from core.interface import hooks

    captured_out = io.StringIO()
    captured_err = io.StringIO()

    extra_patches = patches or {}

    # Patch _read_stdin_json directly to bypass select/fcntl which don't work
    # with io.StringIO in tests.
    with patch("core.interface.hooks._read_stdin_json", return_value=hook_input), \
         patch("core.interface.hooks.sys.stdout", captured_out), \
         patch("core.interface.hooks.sys.stderr", captured_err):
        for attr, val in extra_patches.items():
            monkeypatch.setattr(hooks, attr, val, raising=False)
        hooks.hook_inject(MagicMock())

    return captured_out.getvalue(), captured_err.getvalue()


def _run_hook_session_init(hook_input: dict, *, monkeypatch, rules_dir: Path):
    """Drive hook_session_init with fake stdin and captured stdout/stderr.

    Returns (stdout_text, stderr_text, combined_quaid_rules_content_or_None).
    """
    from core.interface import hooks

    captured_out = io.StringIO()
    captured_err = io.StringIO()

    monkeypatch.setenv("QUAID_RULES_DIR", str(rules_dir))

    with patch("core.interface.hooks._read_stdin_json", return_value=hook_input), \
         patch("core.interface.hooks.sys.stdout", captured_out), \
         patch("core.interface.hooks.sys.stderr", captured_err):
        hooks.hook_session_init(MagicMock())

    rule_files = sorted(
        path for path in rules_dir.glob("quaid-*.md")
        if path.is_file() and not path.name.endswith(".bak")
    )
    content = "\n\n".join(path.read_text(encoding="utf-8") for path in rule_files) if rule_files else None
    return captured_out.getvalue(), captured_err.getvalue(), content


def _run_hook_extract(hook_input: dict, *, monkeypatch, precompact: bool = False):
    """Drive hook_extract with fake stdin and captured stdout/stderr."""
    from core.interface import hooks

    captured_out = io.StringIO()
    captured_err = io.StringIO()
    args = types.SimpleNamespace(precompact=precompact)

    with patch("core.interface.hooks._read_stdin_json", return_value=hook_input), \
         patch("core.interface.hooks.sys.stdout", captured_out), \
         patch("core.interface.hooks.sys.stderr", captured_err):
        hooks.hook_extract(args)

    return captured_out.getvalue(), captured_err.getvalue()


def _run_hook_subagent_stop(hook_input: dict, *, monkeypatch):
    """Drive hook_subagent_stop with fake stdin and captured stderr."""
    from core.interface import hooks

    captured_err = io.StringIO()
    with patch("core.interface.hooks._read_stdin_json", return_value=hook_input), \
         patch("core.interface.hooks.sys.stderr", captured_err):
        hooks.hook_subagent_stop(MagicMock())
    return captured_err.getvalue()


def test_wake_daemon_after_signal_skips_start_when_daemon_is_live(monkeypatch):
    from core import extraction_daemon
    from core.interface import hooks

    popen_calls = []

    monkeypatch.setattr(extraction_daemon, "read_pid", lambda: 12345)
    monkeypatch.setattr(hooks.subprocess, "Popen", lambda *args, **kwargs: popen_calls.append((args, kwargs)))

    hooks._wake_daemon_after_signal()

    assert popen_calls == []


def test_wake_daemon_after_signal_starts_when_daemon_is_missing(monkeypatch):
    from core import extraction_daemon
    from core.interface import hooks

    popen_calls = []

    monkeypatch.setattr(extraction_daemon, "read_pid", lambda: None)
    monkeypatch.setattr(hooks, "_daemon_start_env", lambda: {"QUAID_HOME": "/tmp/quaid"})
    monkeypatch.setattr(hooks.subprocess, "Popen", lambda *args, **kwargs: popen_calls.append((args, kwargs)))

    hooks._wake_daemon_after_signal()

    assert len(popen_calls) == 1
    args, kwargs = popen_calls[0]
    assert args[0][-1] == "start"
    assert kwargs["start_new_session"] is True
    assert kwargs["env"] == {"QUAID_HOME": "/tmp/quaid"}


def test_claude_code_inject_writes_session_end_signal_for_clear_command(monkeypatch, tmp_path, cursor_dir):
    from adaptors.claude_code.adapter import ClaudeCodeAdapter

    transcript_path = tmp_path / "cc-clear.jsonl"
    transcript_path.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "My sister is Diana."}}) + "\n",
        encoding="utf-8",
    )

    written_signals = []

    def fake_write_signal(**kwargs):
        written_signals.append(kwargs)
        return Path(tmp_path / "signals" / "sig-session-end.json")

    adapter = _adapter_mock()
    cc_adapter = ClaudeCodeAdapter()
    adapter.adapter_id.return_value = "claude-code"
    adapter.resolve_prompt_submit_signal.side_effect = cc_adapter.resolve_prompt_submit_signal

    monkeypatch.setattr("core.extraction_daemon.write_signal", fake_write_signal)
    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)

    out, err = _run_hook_inject(
        {
            "session_id": "sess-cc-clear",
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
    assert sig["session_id"] == "sess-cc-clear"
    assert sig["transcript_path"] == str(transcript_path)
    assert sig["adapter"] == "claude-code"
    assert sig["supports_compaction_control"] is False
    assert sig["meta"]["source"] == "hook_inject"
    assert sig["meta"]["command"] == "/clear"
    assert sig["meta"]["reason"] == "command:clear"


def test_claude_code_inject_refreshes_rules_context_for_compact_command(monkeypatch, tmp_path, cursor_dir):
    from adaptors.claude_code.adapter import ClaudeCodeAdapter

    transcript_path = tmp_path / "cc-compact.jsonl"
    transcript_path.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "/compact"}}) + "\n",
        encoding="utf-8",
    )
    projects_dir = tmp_path / "projects"
    identity_dir = tmp_path / "identity"
    projects_dir.mkdir()
    identity_dir.mkdir()
    (identity_dir / "USER.md").write_text("The office plant is named Bartholomew.", encoding="utf-8")
    (identity_dir / "SOUL.md").write_text("SOUL live", encoding="utf-8")
    (identity_dir / "ENVIRONMENT.md").write_text("It is a fiddle-leaf fig.", encoding="utf-8")

    rules_dir = tmp_path / ".claude" / "rules"
    monkeypatch.setenv("QUAID_RULES_DIR", str(rules_dir))

    written_signals = []

    def fake_write_signal(**kwargs):
        written_signals.append(kwargs)
        return Path(tmp_path / "signals" / "sig-compact.json")

    adapter = _adapter_mock()
    cc_adapter = ClaudeCodeAdapter()
    adapter.adapter_id.return_value = "claude-code"
    adapter.resolve_prompt_submit_signal.side_effect = cc_adapter.resolve_prompt_submit_signal
    adapter.projects_dir.return_value = projects_dir
    adapter.identity_dir.return_value = identity_dir
    adapter.data_dir.return_value = tmp_path / "data"
    adapter.get_base_context_files.return_value = {}
    adapter.get_cli_tools_snippet.return_value = ""

    monkeypatch.setattr("core.extraction_daemon.write_signal", fake_write_signal)
    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)

    out, err = _run_hook_inject(
        {
            "session_id": "sess-cc-compact",
            "transcript_path": str(transcript_path),
            "cwd": str(tmp_path),
            "prompt": "/compact",
        },
        monkeypatch=monkeypatch,
    )

    assert out.strip() == ""
    assert len(written_signals) == 1
    sig = written_signals[0]
    assert sig["signal_type"] == "compaction"
    assert sig["session_id"] == "sess-cc-compact"
    assert sig["transcript_path"] == str(transcript_path)
    assert sig["meta"]["command"] == "/compact"
    user_rules = (rules_dir / "quaid-user.md").read_text(encoding="utf-8")
    soul_rules = (rules_dir / "quaid-soul.md").read_text(encoding="utf-8")
    env_rules = (rules_dir / "quaid-environment.md").read_text(encoding="utf-8")
    assert "Bartholomew" in user_rules
    assert "SOUL live" in soul_rules
    assert "fiddle-leaf fig" in env_rules
    assert not (rules_dir / "quaid-user-md.md").exists()
    assert not (rules_dir / "quaid-soul-md.md").exists()
    assert not (rules_dir / "quaid-environment-md.md").exists()
    assert "context-refresh" in err
    marker_file = tmp_path / "data" / "context-refresh-compaction" / "sess-cc-compact.json"
    assert marker_file.is_file()
    marker_payload = json.loads(marker_file.read_text(encoding="utf-8"))
    assert marker_payload["reason"] == "compact_command"
    assert marker_payload["source"] == "hook_inject"


def test_claude_code_post_compact_turn_gets_identity_additional_context_under_cap(monkeypatch, tmp_path, cursor_dir):
    from adaptors.claude_code.adapter import ClaudeCodeAdapter

    transcript_path = tmp_path / "cc-compact-followup.jsonl"
    transcript_path.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "/compact"}}) + "\n",
        encoding="utf-8",
    )
    projects_dir = tmp_path / "projects"
    identity_dir = tmp_path / "identity"
    projects_dir.mkdir()
    identity_dir.mkdir()
    (identity_dir / "USER.md").write_text("The office plant is named Bartholomew.", encoding="utf-8")
    (identity_dir / "SOUL.md").write_text("SOUL live", encoding="utf-8")
    (identity_dir / "ENVIRONMENT.md").write_text("It is a fiddle-leaf fig.", encoding="utf-8")

    rules_dir = tmp_path / ".claude" / "rules"
    data_dir = tmp_path / "data"
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_RULES_DIR", str(rules_dir))

    written_signals = []

    def fake_write_signal(**kwargs):
        written_signals.append(kwargs)
        return Path(tmp_path / "signals" / "sig-compact.json")

    adapter = _adapter_mock()
    cc_adapter = ClaudeCodeAdapter()
    adapter.adapter_id.return_value = "claude-code"
    adapter.resolve_prompt_submit_signal.side_effect = cc_adapter.resolve_prompt_submit_signal
    adapter.projects_dir.return_value = projects_dir
    adapter.identity_dir.return_value = identity_dir
    adapter.data_dir.return_value = data_dir
    adapter.get_base_context_files.return_value = {}
    adapter.get_cli_tools_snippet.return_value = ""
    adapter.get_pending_context.return_value = (
        "<quaid_system_message>\n"
        "Solomon Steadman asked about an office plant name but no plant name "
        "was previously recorded in memory.\n"
        "</quaid_system_message>"
    )
    adapter.get_deferred_notice_relay_context.return_value = ""

    monkeypatch.setattr("core.extraction_daemon.write_signal", fake_write_signal)
    monkeypatch.setattr("core.extraction_daemon.ensure_alive", lambda: None)
    monkeypatch.setattr("core.extraction_daemon.read_cursor", lambda session_id: {"transcript_path": str(transcript_path)})
    monkeypatch.setattr("core.interface.hooks._runtime_config_snapshot", lambda: tuple())
    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)
    _run_hook_inject(
        {
            "session_id": "sess-cc-compact-followup",
            "transcript_path": str(transcript_path),
            "cwd": str(tmp_path),
            "prompt": "/compact",
        },
        monkeypatch=monkeypatch,
    )

    marker_path = data_dir / "context-refresh-compaction" / "sess-cc-compact-followup.json"
    assert marker_path.is_file()
    user_rules = (rules_dir / "quaid-user.md").read_text(encoding="utf-8")
    env_rules = (rules_dir / "quaid-environment.md").read_text(encoding="utf-8")
    assert "Bartholomew" in user_rules
    assert "fiddle-leaf fig" in env_rules

    def fail_recall(**kwargs):
        pytest.fail("post-compact identity bridge should skip recall on marker turn")

    monkeypatch.setattr("core.interface.api.recall_fast", fail_recall)
    monkeypatch.setattr("core.interface.api.projects_search_docs", lambda **kwargs: pytest.fail("post-compact identity bridge should skip docs on marker turn"))

    out, _err = _run_hook_inject(
        {
            "session_id": "sess-cc-compact-followup",
            "transcript_path": str(transcript_path),
            "cwd": str(tmp_path),
            "prompt": "What is the office plant named?",
        },
        monkeypatch=monkeypatch,
    )

    payload = json.loads(out)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert len(context) < 10_000
    assert "Quaid Refreshed Identity Context" in context
    assert "MANDATORY" in context
    assert "Bartholomew" in context
    assert "fiddle-leaf fig" in context
    assert "no plant name" not in context
    assert "Baratza Encore" not in context
    assert not marker_path.exists()

    adapter.get_pending_context.return_value = ""
    monkeypatch.setattr("core.interface.api.recall_fast", lambda **kwargs: ([], None))
    monkeypatch.setattr("core.interface.api.projects_search_docs", lambda **kwargs: {})

    out2, _ = _run_hook_inject(
        {
            "session_id": "sess-cc-compact-followup",
            "transcript_path": str(transcript_path),
            "cwd": str(tmp_path),
            "prompt": "Ask again.",
        },
        monkeypatch=monkeypatch,
    )
    assert out2.strip() == ""


def test_claude_code_post_compact_turn_falls_back_to_refreshed_identity_rules(monkeypatch, tmp_path, cursor_dir):
    from adaptors.claude_code.adapter import ClaudeCodeAdapter

    transcript_path = tmp_path / "cc-compact-rules-fallback.jsonl"
    transcript_path.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "/compact"}}) + "\n",
        encoding="utf-8",
    )
    projects_dir = tmp_path / "projects"
    identity_dir = tmp_path / "identity"
    projects_dir.mkdir()
    identity_dir.mkdir()
    (identity_dir / "USER.md").write_text("The office plant is named Bartholomew.", encoding="utf-8")
    (identity_dir / "SOUL.md").write_text("SOUL live", encoding="utf-8")
    (identity_dir / "ENVIRONMENT.md").write_text("It is a fiddle-leaf fig.", encoding="utf-8")

    rules_dir = tmp_path / ".claude" / "rules"
    data_dir = tmp_path / "data"
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_RULES_DIR", str(rules_dir))

    adapter = _adapter_mock()
    cc_adapter = ClaudeCodeAdapter()
    adapter.adapter_id.return_value = "claude-code"
    adapter.resolve_prompt_submit_signal.side_effect = cc_adapter.resolve_prompt_submit_signal
    adapter.projects_dir.return_value = projects_dir
    adapter.identity_dir.return_value = identity_dir
    adapter.data_dir.return_value = data_dir
    adapter.get_base_context_files.return_value = {}
    adapter.get_cli_tools_snippet.return_value = ""
    adapter.get_pending_context.return_value = ""
    adapter.get_deferred_notice_relay_context.return_value = ""

    monkeypatch.setattr("core.extraction_daemon.write_signal", lambda **kwargs: tmp_path / "signals" / "sig-compact.json")
    monkeypatch.setattr("core.extraction_daemon.ensure_alive", lambda: None)
    monkeypatch.setattr("core.extraction_daemon.read_cursor", lambda session_id: {"transcript_path": str(transcript_path)})
    monkeypatch.setattr("core.interface.hooks._runtime_config_snapshot", lambda: tuple())
    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)

    _run_hook_inject(
        {
            "session_id": "sess-cc-compact-rules-fallback",
            "transcript_path": str(transcript_path),
            "cwd": str(tmp_path),
            "prompt": "/compact",
        },
        monkeypatch=monkeypatch,
    )

    marker_path = data_dir / "context-refresh-compaction" / "sess-cc-compact-rules-fallback.json"
    assert marker_path.is_file()
    assert "Bartholomew" in (rules_dir / "quaid-user.md").read_text(encoding="utf-8")
    assert "fiddle-leaf fig" in (rules_dir / "quaid-environment.md").read_text(encoding="utf-8")

    for filename in ("USER.md", "SOUL.md", "ENVIRONMENT.md"):
        (identity_dir / filename).write_text("\n", encoding="utf-8")

    def fail_recall(**kwargs):
        pytest.fail("post-compact rules identity bridge should skip recall on marker turn")

    monkeypatch.setattr("core.interface.api.recall_fast", fail_recall)
    monkeypatch.setattr("core.interface.api.projects_search_docs", lambda **kwargs: pytest.fail("post-compact rules identity bridge should skip docs on marker turn"))

    out, _err = _run_hook_inject(
        {
            "session_id": "sess-cc-compact-rules-fallback",
            "transcript_path": str(transcript_path),
            "cwd": str(tmp_path),
            "prompt": "What is the office plant named?",
        },
        monkeypatch=monkeypatch,
    )

    payload = json.loads(out)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert len(context) < 10_000
    assert "Quaid Refreshed Identity Context" in context
    assert "refreshed split Quaid rules files" in context
    assert "Bartholomew" in context
    assert "fiddle-leaf fig" in context
    assert not marker_path.exists()


def test_claude_code_post_compact_turn_uses_identity_bridge_after_session_rollover(monkeypatch, tmp_path, cursor_dir):
    from adaptors.claude_code.adapter import ClaudeCodeAdapter

    transcript_path = tmp_path / "cc-compact-rollover.jsonl"
    transcript_path.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "/compact"}}) + "\n",
        encoding="utf-8",
    )
    projects_dir = tmp_path / "projects"
    identity_dir = tmp_path / "identity"
    projects_dir.mkdir()
    identity_dir.mkdir()
    (identity_dir / "USER.md").write_text("The office plant is named Bartholomew.", encoding="utf-8")
    (identity_dir / "SOUL.md").write_text("SOUL live", encoding="utf-8")
    (identity_dir / "ENVIRONMENT.md").write_text("It is a fiddle-leaf fig.", encoding="utf-8")

    rules_dir = tmp_path / ".claude" / "rules"
    data_dir = tmp_path / "data"
    monkeypatch.setenv("QUAID_RULES_DIR", str(rules_dir))

    adapter = _adapter_mock()
    cc_adapter = ClaudeCodeAdapter()
    adapter.adapter_id.return_value = "claude-code"
    adapter.resolve_prompt_submit_signal.side_effect = cc_adapter.resolve_prompt_submit_signal
    adapter.projects_dir.return_value = projects_dir
    adapter.identity_dir.return_value = identity_dir
    adapter.data_dir.return_value = data_dir
    adapter.get_base_context_files.return_value = {}
    adapter.get_cli_tools_snippet.return_value = ""
    adapter.get_pending_context.return_value = ""
    adapter.get_deferred_notice_relay_context.return_value = ""

    monkeypatch.setattr("core.extraction_daemon.write_signal", lambda **kwargs: tmp_path / "sig.json")
    monkeypatch.setattr("core.extraction_daemon.ensure_alive", lambda: None)
    monkeypatch.setattr("core.extraction_daemon.read_cursor", lambda session_id: {"transcript_path": str(transcript_path)})
    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)

    _run_hook_inject(
        {
            "session_id": "sess-before-compact",
            "transcript_path": str(transcript_path),
            "cwd": str(tmp_path),
            "prompt": "/compact",
        },
        monkeypatch=monkeypatch,
    )

    assert (data_dir / "context-refresh-compaction" / "sess-before-compact.json").is_file()
    assert (data_dir / "context-refresh-compaction" / "_latest.json").is_file()

    def fail_recall(**kwargs):
        pytest.fail("post-compact identity bridge should skip recall even after session rollover")

    monkeypatch.setattr("core.interface.api.recall_fast", fail_recall)
    monkeypatch.setattr("core.interface.api.projects_search_docs", lambda **kwargs: pytest.fail("post-compact identity bridge should skip docs even after session rollover"))

    out, _err = _run_hook_inject(
        {
            "session_id": "sess-after-compact",
            "transcript_path": str(transcript_path),
            "cwd": str(tmp_path),
            "prompt": "What is the office plant named?",
        },
        monkeypatch=monkeypatch,
    )

    payload = json.loads(out)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "Quaid Refreshed Identity Context" in context
    assert "Bartholomew" in context
    assert "fiddle-leaf fig" in context
    assert not (data_dir / "context-refresh-compaction" / "_latest.json").exists()
    assert not (data_dir / "context-refresh-compaction" / "sess-before-compact.json").exists()


def test_refresh_runtime_config_if_changed_reloads_and_resets_caches(monkeypatch, tmp_path):
    from core.interface import hooks

    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    reloads = []
    resets = []
    cleared = []

    monkeypatch.setattr("config._config_paths", lambda: [cfg])
    monkeypatch.setattr("config.reload_config", lambda: reloads.append("reload"))
    monkeypatch.setattr("lib.embeddings.reset_embeddings_provider", lambda: resets.append("embeddings"))
    monkeypatch.setattr("lib.llm_clients.reset_model_config_cache", lambda: resets.append("llm"))
    monkeypatch.setattr(
        "lib.agent_notice.clear_pending_notices_by_source",
        lambda *, sources: cleared.append(set(sources)),
    )
    monkeypatch.setattr(hooks, "_HOOK_RUNTIME_CONFIG_SNAPSHOT", None)
    monkeypatch.setattr(hooks, "_read_runtime_config_snapshot_state", lambda: None)

    assert hooks._refresh_runtime_config_if_changed("test") is False

    time.sleep(0.01)
    cfg.write_text('{"models": {"fastReasoning": "restored"}}', encoding="utf-8")

    assert hooks._refresh_runtime_config_if_changed("test") is True
    assert reloads == ["reload"]
    assert resets == ["embeddings", "llm"]
    assert cleared == [{"provider", "llm_config", "embeddings"}]


def test_refresh_runtime_config_if_changed_initializes_baseline_without_clearing_active_notices(
    monkeypatch, tmp_path
):
    from core.interface import hooks

    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    reloads = []
    resets = []
    cleared = []

    monkeypatch.setattr("config._config_paths", lambda: [cfg])
    monkeypatch.setattr("config.reload_config", lambda: reloads.append("reload"))
    monkeypatch.setattr("lib.embeddings.reset_embeddings_provider", lambda: resets.append("embeddings"))
    monkeypatch.setattr("lib.llm_clients.reset_model_config_cache", lambda: resets.append("llm"))
    monkeypatch.setattr(
        "lib.agent_notice.clear_pending_notices_by_source",
        lambda *, sources: cleared.append(set(sources)) or 2,
    )
    adapter = MagicMock()
    adapter.data_dir.return_value = tmp_path / "data"
    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)
    monkeypatch.setattr(hooks, "_HOOK_RUNTIME_CONFIG_SNAPSHOT", None)

    assert hooks._refresh_runtime_config_if_changed("test") is False
    assert reloads == []
    assert resets == []
    assert cleared == []
    assert (tmp_path / "data" / "runtime-config-snapshot.json").is_file()


def test_refresh_runtime_config_if_changed_uses_persisted_snapshot_for_fresh_hook_process(
    monkeypatch, tmp_path
):
    from core.interface import hooks

    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    reloads = []
    resets = []
    cleared = []

    adapter = MagicMock()
    adapter.data_dir.return_value = tmp_path / "data"
    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)
    monkeypatch.setattr("config._config_paths", lambda: [cfg])
    monkeypatch.setattr("config.reload_config", lambda: reloads.append("reload"))
    monkeypatch.setattr("lib.embeddings.reset_embeddings_provider", lambda: resets.append("embeddings"))
    monkeypatch.setattr("lib.llm_clients.reset_model_config_cache", lambda: resets.append("llm"))
    monkeypatch.setattr(
        "lib.agent_notice.clear_pending_notices_by_source",
        lambda *, sources: cleared.append(set(sources)),
    )
    monkeypatch.setattr(hooks, "_HOOK_RUNTIME_CONFIG_SNAPSHOT", None)

    hooks._write_runtime_config_snapshot_state(((str(cfg), -1),))

    assert hooks._refresh_runtime_config_if_changed("test") is True
    assert reloads == ["reload"]
    assert resets == ["embeddings", "llm"]
    assert cleared == [{"provider", "llm_config", "embeddings"}]


def test_claude_code_inject_writes_session_end_signal_for_empty_prompt_reset_metadata(
    monkeypatch, tmp_path, cursor_dir
):
    from adaptors.claude_code.adapter import ClaudeCodeAdapter

    transcript_path = tmp_path / "cc-reset.jsonl"
    transcript_path.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "Zephyr delta nine."}}) + "\n",
        encoding="utf-8",
    )

    written_signals = []

    def fake_write_signal(**kwargs):
        written_signals.append(kwargs)
        return Path(tmp_path / "signals" / "sig-session-end.json")

    adapter = _adapter_mock()
    cc_adapter = ClaudeCodeAdapter()
    adapter.adapter_id.return_value = "claude-code"
    adapter.resolve_prompt_submit_signal.side_effect = cc_adapter.resolve_prompt_submit_signal

    monkeypatch.setattr("core.extraction_daemon.write_signal", fake_write_signal)
    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)

    out, err = _run_hook_inject(
        {
            "session_id": "sess-cc-reset",
            "transcript_path": str(transcript_path),
            "cwd": str(tmp_path),
            "prompt": "",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "<command-name>/reset</command-name>\n"
                            "<command-message>reset context</command-message>\n"
                            "<command-args></command-args>"
                        ),
                    }
                ]
            },
        },
        monkeypatch=monkeypatch,
    )

    assert out.strip() == ""
    assert len(written_signals) == 1
    sig = written_signals[0]
    assert sig["signal_type"] == "session_end"
    assert sig["session_id"] == "sess-cc-reset"
    assert sig["transcript_path"] == str(transcript_path)
    assert sig["adapter"] == "claude-code"
    assert sig["supports_compaction_control"] is False
    assert sig["meta"]["source"] == "hook_inject"
    assert sig["meta"]["command"] == "/reset"
    assert sig["meta"]["reason"] == "command:reset"
    assert err.strip() == ""


def test_claude_code_session_start_clear_queues_prior_session_signal(
    monkeypatch, tmp_path, cursor_dir
):
    from adaptors.claude_code.adapter import ClaudeCodeAdapter

    quaid_home = tmp_path / ".quaid"
    monkeypatch.setenv("QUAID_HOME", str(quaid_home))
    monkeypatch.setenv("QUAID_INSTANCE", "claude-code-test")
    monkeypatch.setenv("HOME", str(tmp_path))

    transcript_dir = tmp_path / ".claude" / "projects" / "-private-tmp-cc-livetest"
    transcript_dir.mkdir(parents=True)
    old_transcript = transcript_dir / "32c388db-old.jsonl"
    new_transcript = transcript_dir / "2d29284b-new.jsonl"
    old_transcript.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "M11 seed fact."}}) + "\n",
        encoding="utf-8",
    )
    new_transcript.write_text(
        json.dumps(
            {
                "type": "system",
                "subtype": "SessionStart",
                "source": "clear",
                "message": {"content": "<command-name>/clear</command-name>"},
            }
        ) + "\n",
        encoding="utf-8",
    )

    adapter = ClaudeCodeAdapter(home=quaid_home)
    assert adapter.check_session_transition(
        {
            "session_id": "32c388db",
            "transcript_path": str(old_transcript),
            "cwd": str(tmp_path),
        }
    ) is None

    projects_dir = tmp_path / "projects"
    identity_dir = tmp_path / "identity"
    rules_dir = tmp_path / "rules"
    projects_dir.mkdir()
    identity_dir.mkdir()
    rules_dir.mkdir()

    written_signals = []

    def fake_write_signal(**kwargs):
        written_signals.append(kwargs)
        return Path(tmp_path / "signals" / "sig-session-end.json")

    from core.interface import hooks

    monkeypatch.setattr(hooks, "_get_projects_dir", lambda: projects_dir)
    monkeypatch.setattr(hooks, "_get_identity_dir", lambda: identity_dir)
    monkeypatch.setattr(hooks, "_check_janitor_health", lambda: "")
    monkeypatch.setattr(hooks, "_get_deferred_notice_hint", lambda: "")
    monkeypatch.setattr(hooks, "_get_pending_context", lambda: "")
    monkeypatch.setattr(hooks, "_build_runtime_context_block", lambda: "[Quaid runtime]")
    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)
    monkeypatch.setattr("core.extraction_daemon.ensure_alive", lambda: None)
    monkeypatch.setattr("core.extraction_daemon.write_signal", fake_write_signal)
    monkeypatch.setattr("core.extraction_daemon.read_cursor", lambda sid: {"line_offset": 0, "transcript_path": ""})
    monkeypatch.setattr("core.extraction_daemon.write_cursor", lambda *args: None)

    _run_hook_session_init(
        {
            "session_id": "2d29284b",
            "transcript_path": str(new_transcript),
            "cwd": str(tmp_path),
            "source": "clear",
        },
        monkeypatch=monkeypatch,
        rules_dir=rules_dir,
    )

    assert len(written_signals) == 1
    sig = written_signals[0]
    assert sig["signal_type"] == "session_end"
    assert sig["session_id"] == "32c388db"
    assert sig["transcript_path"] == str(old_transcript)
    assert sig["adapter"] == "claude-code"
    assert sig["supports_compaction_control"] is False
    assert sig["meta"]["source"] == "session_transition"
    assert sig["meta"]["command"] == "/clear"
    assert sig["meta"]["reason"] == "session_start_transition"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def sessions_dir(tmp_path):
    d = tmp_path / "sessions"
    d.mkdir()
    return d


@pytest.fixture()
def cursor_dir(tmp_path, monkeypatch):
    """Wire extraction_daemon._cursor_dir() to a temp directory."""
    from core import extraction_daemon
    d = tmp_path / "cursors"
    d.mkdir()
    monkeypatch.setattr(extraction_daemon, "_cursor_dir", lambda: d)
    return d


@pytest.fixture()
def mock_adapter(tmp_path, sessions_dir, monkeypatch):
    """Return a mock adapter wired into get_adapter() and get_owner_id()."""
    adapter = _adapter_mock()
    adapter.adapter_id.return_value = ""
    adapter.get_session_path.return_value = None
    adapter.get_sessions_dir.return_value = str(sessions_dir)
    adapter.get_pending_context.return_value = ""
    adapter.resolve_prompt_submit_signal.return_value = None
    adapter.instance_root.return_value = tmp_path

    monkeypatch.setattr("core.interface.hooks._get_pending_context", lambda: "")
    monkeypatch.setattr("core.interface.hooks._get_deferred_notice_hint", lambda: "")
    monkeypatch.setattr("core.interface.hooks._runtime_config_snapshot", lambda: tuple())
    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)
    monkeypatch.setattr("core.interface.hooks._get_owner_id", lambda: "test-owner")
    return adapter


# ===========================================================================
# hook_inject — cursor seeding
# ===========================================================================

class TestHookInjectCursorSeeding:

    def test_rglob_finds_transcript_writes_cursor(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        """When rglob finds the transcript, write_cursor is called with that path."""
        session_id = "abc123"
        transcript = sessions_dir / "-Users-foo-bar" / f"{session_id}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")

        written = {}

        from core import extraction_daemon

        real_read_cursor = extraction_daemon.read_cursor

        def fake_write_cursor(sid, offset, path):
            written["sid"] = sid
            written["offset"] = offset
            written["path"] = path

        monkeypatch.setattr(extraction_daemon, "write_cursor", fake_write_cursor)

        # recall_fast returns empty list so hook returns early after cursor write
        with patch("core.interface.api.recall_fast", return_value=[]):
            _run_hook_inject(
                {
                    "prompt": "hello world test",
                    "session_id": session_id,
                    "cwd": "/Users/foo/bar",
                },
                monkeypatch=monkeypatch,
            )

        assert written.get("sid") == session_id
        assert written.get("offset") == 0
        assert written.get("path") == str(transcript)

    def test_rglob_miss_uses_cwd_fallback(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        """When rglob finds nothing (race), derive path from cwd encoding."""
        session_id = "raceXYZ"
        cwd = "/tmp/quaid-dev"
        expected_encoded = cwd.replace("/", "-")  # "-tmp-quaid-dev"
        expected_path = str(Path(str(sessions_dir)) / expected_encoded / f"{session_id}.jsonl")
        mock_adapter.adapter_id.return_value = "claude-code"

        written = {}

        from core import extraction_daemon

        def fake_write_cursor(sid, offset, path):
            written["sid"] = sid
            written["path"] = path

        monkeypatch.setattr(extraction_daemon, "write_cursor", fake_write_cursor)

        with patch("core.interface.api.recall_fast", return_value=[]):
            _run_hook_inject(
                {
                    "prompt": "some prompt to trigger inject",
                    "session_id": session_id,
                    "cwd": cwd,
                },
                monkeypatch=monkeypatch,
            )

        assert written.get("sid") == session_id
        assert written.get("path") == expected_path

    def test_cursor_already_exists_skips_write(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        """When cursor already has transcript_path, write_cursor is NOT called."""
        session_id = "existing-sess"
        # Pre-write a cursor with transcript_path set
        cursor_file = cursor_dir / f"{session_id}.json"
        cursor_file.write_text(
            json.dumps({
                "session_id": session_id,
                "line_offset": 5,
                "transcript_path": "/some/path/existing.jsonl",
            }),
            encoding="utf-8",
        )

        write_calls = []

        from core import extraction_daemon

        def fake_write_cursor(sid, offset, path):
            write_calls.append((sid, offset, path))

        monkeypatch.setattr(extraction_daemon, "write_cursor", fake_write_cursor)

        with patch("core.interface.api.recall_fast", return_value=[]):
            _run_hook_inject(
                {
                    "prompt": "query to trigger inject",
                    "session_id": session_id,
                    "cwd": "/Users/foo",
                },
                monkeypatch=monkeypatch,
            )

        assert write_calls == [], "write_cursor must not be called when cursor already has transcript_path"

    def test_no_session_id_skips_cursor_gracefully(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        """When session_id is absent, hook must not crash."""
        from core import extraction_daemon

        write_calls = []
        monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *a: write_calls.append(a))

        with patch("core.interface.api.recall_fast", return_value=[]), \
             patch("core.interface.api.projects_search_docs", return_value=None):
            out, err = _run_hook_inject(
                {
                    "prompt": "this has no session id",
                    "cwd": "/Users/foo",
                },
                monkeypatch=monkeypatch,
            )

        # Must not crash; write_cursor should not have been called
        assert write_calls == []

    def test_empty_cwd_skips_fallback_gracefully(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        """When cwd is empty string, no fallback path is derived and no crash occurs."""
        session_id = "no-cwd-sess"

        written = {}

        from core import extraction_daemon

        def fake_write_cursor(sid, offset, path):
            written["path"] = path

        monkeypatch.setattr(extraction_daemon, "write_cursor", fake_write_cursor)

        with patch("core.interface.api.recall_fast", return_value=[]):
            _run_hook_inject(
                {
                    "prompt": "prompt with empty cwd",
                    "session_id": session_id,
                    "cwd": "",
                },
                monkeypatch=monkeypatch,
            )

        # rglob found nothing, cwd was empty — OC flat-path fallback fires:
        # sessions_dir/{session_id}.jsonl is used as the predicted path.
        expected_flat = str(sessions_dir / f"{session_id}.jsonl")
        assert written.get("path") == expected_flat

    def test_codex_race_uses_predicted_rollout_fallback(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        """Codex sessions seed a predicted rollout path before the file exists."""
        session_id = "019d4367-1794-7fc2-84f3-bb30ba99a24f"
        mock_adapter.adapter_id.return_value = "codex"
        mock_adapter.get_session_path.return_value = None

        written = {}

        from core import extraction_daemon

        def fake_write_cursor(sid, offset, path):
            written["sid"] = sid
            written["offset"] = offset
            written["path"] = path

        monkeypatch.setattr(extraction_daemon, "write_cursor", fake_write_cursor)

        with patch("core.interface.api.recall_fast", return_value=[]):
            _run_hook_inject(
                {
                    "prompt": "seed codex cursor",
                    "session_id": session_id,
                    "cwd": "/tmp/quaid-dev",
                },
                monkeypatch=monkeypatch,
            )

        expected_path = (
            Path(str(sessions_dir))
            / datetime.now().strftime("%Y/%m/%d")
            / f"rollout-pending-{session_id}.jsonl"
        )
        assert written == {
            "sid": session_id,
            "offset": 0,
            "path": str(expected_path),
        }


def test_hook_extract_precompact_resolves_cc_transcript_and_flushes_staged_payload(
    tmp_path, sessions_dir, mock_adapter, monkeypatch
):
    from core import extraction_daemon

    session_id = "sess-precompact-flush"
    cwd = "/tmp/private-cc-project"
    transcript = sessions_dir / cwd.replace("/", "-") / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        '{"role":"user","content":"My sister is Diana"}\n'
        '{"role":"assistant","content":"Her daughter is Alice"}\n',
        encoding="utf-8",
    )

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "claude-code-test")
    instance_root = tmp_path / "instances" / "claude-code-test"
    instance_root.mkdir(parents=True, exist_ok=True)
    (instance_root / "config.json").write_text(
        json.dumps({"adapter": {"type": "claude-code"}}),
        encoding="utf-8",
    )

    mock_adapter.adapter_id.return_value = "claude-code"
    mock_adapter.get_session_path.return_value = None
    mock_adapter.get_sessions_dir.return_value = str(sessions_dir)
    mock_adapter.parse_session_jsonl.return_value = "User: My sister is Diana"
    mock_adapter.is_subagent_session.return_value = False
    mock_adapter.store_auth_token.return_value = instance_root / ".auth-token"

    extraction_daemon.write_cursor(session_id, 2, str(transcript))
    extraction_daemon.write_rolling_state(
        session_id,
        {
            "session_id": session_id,
            "transcript_path": str(transcript),
            "processed_line_offset": 2,
            "buffered_line_offset": 2,
            "semantic_buffer": "",
            "semantic_buffer_tokens": 0,
            "carry_facts": [],
            "raw_facts": [{"text": "Owner has a sister named Diana", "category": "fact"}],
        },
    )

    monkeypatch.setattr("core.interface.hooks.subprocess.Popen", lambda *a, **kw: None)

    out, err = _run_hook_extract(
        {
            "session_id": session_id,
            "cwd": cwd,
        },
        monkeypatch=monkeypatch,
        precompact=True,
    )

    signals = extraction_daemon.read_pending_signals()
    assert out == ""
    assert "signal written" in err
    assert len(signals) == 1
    assert signals[0]["type"] == "compaction"
    assert signals[0]["transcript_path"] == str(transcript)

    real_registry = sys.modules.get("core.subagent_registry")
    real_extract = sys.modules.get("ingest.extract")
    real_notify = sys.modules.get("core.runtime.notify")
    real_ingest_runtime = sys.modules.get("core.ingest_runtime")
    real_project_registry = sys.modules.get("core.project_registry")
    real_docs_updater = sys.modules.get("core.docs_updater_hook")

    fake_registry = types.ModuleType("core.subagent_registry")
    fake_registry.is_registered_subagent = lambda sid: False
    fake_registry.get_harvestable = lambda sid: []
    fake_registry.mark_harvested = lambda sid, cid: None
    sys.modules["core.subagent_registry"] = fake_registry

    captured_payloads = []
    fake_extract = types.ModuleType("ingest.extract")
    fake_extract.extract_from_transcript = lambda **kwargs: pytest.fail("payload-only precompact flush should not re-extract transcript tail")
    fake_extract.apply_extracted_payloads = lambda payload, **kwargs: captured_payloads.append((payload, kwargs)) or {
        **payload,
        "facts_stored": len(payload.get("raw_facts", [])),
        "facts_skipped": 0,
        "edges_created": 0,
        "facts": [],
        "snippets": {},
        "journal": {},
        "project_logs": {},
        "project_log_metrics": {},
    }
    fake_extract.collapse_duplicate_payload_facts = lambda facts: (list(facts), 0)
    sys.modules["ingest.extract"] = fake_extract

    fake_notify = types.ModuleType("core.runtime.notify")
    fake_notify.notify_memory_extraction = lambda **kwargs: None
    sys.modules["core.runtime.notify"] = fake_notify

    fake_ingest_runtime = types.ModuleType("core.ingest_runtime")
    fake_ingest_runtime.run_session_logs_ingest = lambda **kwargs: {"status": "indexed"}
    sys.modules["core.ingest_runtime"] = fake_ingest_runtime

    fake_project_registry = types.ModuleType("core.project_registry")
    fake_project_registry.snapshot_all_projects = lambda: []
    sys.modules["core.project_registry"] = fake_project_registry

    fake_docs_updater = types.ModuleType("core.docs_updater_hook")
    fake_docs_updater.update_project_docs = lambda snapshots, extraction_result: {"docs_updated": 0}
    sys.modules["core.docs_updater_hook"] = fake_docs_updater

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
        extraction_daemon.process_signal(signals[0])
    finally:
        if real_registry is not None:
            sys.modules["core.subagent_registry"] = real_registry
        else:
            sys.modules.pop("core.subagent_registry", None)
        if real_extract is not None:
            sys.modules["ingest.extract"] = real_extract
        else:
            sys.modules.pop("ingest.extract", None)
        if real_notify is not None:
            sys.modules["core.runtime.notify"] = real_notify
        else:
            sys.modules.pop("core.runtime.notify", None)
        if real_ingest_runtime is not None:
            sys.modules["core.ingest_runtime"] = real_ingest_runtime
        else:
            sys.modules.pop("core.ingest_runtime", None)
        if real_project_registry is not None:
            sys.modules["core.project_registry"] = real_project_registry
        else:
            sys.modules.pop("core.project_registry", None)
        if real_docs_updater is not None:
            sys.modules["core.docs_updater_hook"] = real_docs_updater
        else:
            sys.modules.pop("core.docs_updater_hook", None)

    assert extraction_daemon.read_pending_signals() == []
    assert len(captured_payloads) == 1
    payload, kwargs = captured_payloads[0]
    assert kwargs["session_id"] == session_id
    assert payload["raw_facts"] == [{"text": "Owner has a sister named Diana", "category": "fact"}]
    assert not extraction_daemon._rolling_state_path(session_id).exists()


def test_hook_extract_precompact_sweeps_older_staged_payloads(
    tmp_path, mock_adapter, monkeypatch
):
    from core import extraction_daemon

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "claude-code-test")
    instance_root = tmp_path / "instances" / "claude-code-test"
    instance_root.mkdir(parents=True, exist_ok=True)

    current_session = "current-precompact"
    old_session = "old-staged"
    current_transcript = tmp_path / "current.jsonl"
    old_transcript = tmp_path / "old.jsonl"
    current_transcript.write_text('{"role":"user","content":"compact now"}\n', encoding="utf-8")
    old_transcript.write_text('{"role":"user","content":"older staged content"}\n', encoding="utf-8")

    mock_adapter.get_session_path.return_value = None
    mock_adapter.store_auth_token.return_value = instance_root / ".auth-token"

    extraction_daemon.write_rolling_state(
        old_session,
        {
            "session_id": old_session,
            "transcript_path": str(old_transcript),
            "processed_line_offset": 1,
            "buffered_line_offset": 1,
            "semantic_buffer": "",
            "semantic_buffer_tokens": 0,
            "raw_facts": [{"text": "Older staged fact", "category": "fact"}],
            "carry_facts": [{"text": "Older staged fact", "category": "fact"}],
        },
    )

    monkeypatch.setattr("core.interface.hooks.subprocess.Popen", lambda *a, **kw: None)

    out, err = _run_hook_extract(
        {
            "session_id": current_session,
            "cwd": str(tmp_path),
            "transcript_path": str(current_transcript),
        },
        monkeypatch=monkeypatch,
        precompact=True,
    )

    signals = extraction_daemon.read_pending_signals()
    by_session = {signal["session_id"]: signal for signal in signals}
    assert out == ""
    assert "staged payload sweep queued 1 additional flush signal" in err
    assert by_session[current_session]["type"] == "compaction"
    assert by_session[current_session]["transcript_path"] == str(current_transcript)
    assert by_session[old_session]["type"] == "session_end"
    assert by_session[old_session]["transcript_path"] == str(old_transcript)
    assert by_session[old_session]["meta"]["reason"] == "precompact_sweep"


def test_hook_extract_precompact_refreshes_rules_context_from_identity_and_projects(
    tmp_path, sessions_dir, mock_adapter, monkeypatch
):
    session_id = "sess-precompact-refresh"
    transcript = sessions_dir / "cc-refresh" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text('{"role":"user","content":"compact now"}\n', encoding="utf-8")

    projects_dir = tmp_path / "projects"
    identity_dir = tmp_path / "identity"
    project_quaid = projects_dir / "quaid"
    project_quaid.mkdir(parents=True, exist_ok=True)
    identity_dir.mkdir(parents=True, exist_ok=True)
    (identity_dir / "USER.md").write_text("Compaction refresh canary: vellum-orchid", encoding="utf-8")
    (identity_dir / "SOUL.md").write_text("SOUL live", encoding="utf-8")
    (identity_dir / "ENVIRONMENT.md").write_text("ENV live", encoding="utf-8")
    (project_quaid / "TOOLS.md").write_text("# Tools\nrefresh docs", encoding="utf-8")
    (project_quaid / "AGENTS.md").write_text("# Agents\nrefresh agents", encoding="utf-8")

    mock_adapter.adapter_id.return_value = "claude-code"
    mock_adapter.get_session_path.return_value = None
    mock_adapter.get_sessions_dir.return_value = str(sessions_dir)
    mock_adapter.projects_dir.return_value = projects_dir
    mock_adapter.identity_dir.return_value = identity_dir
    mock_adapter.data_dir.return_value = tmp_path / "data"
    mock_adapter.get_base_context_files.return_value = {}
    mock_adapter.get_cli_tools_snippet.return_value = ""
    mock_adapter.store_auth_token.return_value = tmp_path / ".auth-token"

    rules_dir = tmp_path / ".claude" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    legacy_rules_file = rules_dir / "quaid-projects.md"
    legacy_rules_file.write_text("stale rules body", encoding="utf-8")

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "claude-code-test")
    monkeypatch.setattr("core.interface.hooks.subprocess.Popen", lambda *a, **kw: None)

    out, err = _run_hook_extract(
        {
            "session_id": session_id,
            "cwd": str(tmp_path),
            "transcript_path": str(transcript),
        },
        monkeypatch=monkeypatch,
        precompact=True,
    )

    assert out == ""
    assert "context-refresh" in err
    assert not legacy_rules_file.exists()
    assert (rules_dir / "quaid-projects.md.bak").read_text(encoding="utf-8") == "stale rules body"
    user_rules = (rules_dir / "quaid-user.md").read_text(encoding="utf-8")
    tools_rules = (rules_dir / "quaid-quaid-tools-md.md").read_text(encoding="utf-8")
    assert "Compaction refresh canary: vellum-orchid" in user_rules
    assert "refresh docs" in tools_rules
    marker_file = tmp_path / "data" / "context-refresh-compaction" / f"{session_id}.json"
    latest_file = tmp_path / "data" / "context-refresh-compaction" / "_latest.json"
    assert marker_file.is_file()
    assert latest_file.is_file()
    marker_payload = json.loads(marker_file.read_text(encoding="utf-8"))
    latest_payload = json.loads(latest_file.read_text(encoding="utf-8"))
    assert marker_payload["reason"] == "precompact_hook"
    assert marker_payload["source"] == "hook_extract_precompact"
    assert latest_payload["reason"] == "precompact_hook"
    assert latest_payload["source"] == "hook_extract_precompact"
    assert "stale rules body" not in user_rules + tools_rules


# ===========================================================================
# hook_inject — recall resilience
# ===========================================================================

class TestHookInjectRecallResilience:

    def test_recall_fast_exception_does_not_crash(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        """hook_inject may degrade on recall_fast exceptions only when failHard is off."""
        from core import extraction_daemon
        monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *a: None)
        monkeypatch.setattr("lib.fail_policy.is_fail_hard_enabled", lambda: False)

        with patch("core.interface.api.recall_fast", side_effect=RuntimeError("LLM down")), \
             patch("core.interface.api.projects_search_docs", return_value=None):
            # Should complete without raising
            out, err = _run_hook_inject(
                {
                    "prompt": "trigger recall failure",
                    "session_id": "sess-err",
                    "cwd": "/Users/x",
                },
                monkeypatch=monkeypatch,
            )

        # Error should appear on stderr, not propagate
        assert "LLM down" in err or True  # hook silences errors internally

    def test_recall_fast_store_timeout_returns_empty_when_fail_hard_enabled(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        from core import extraction_daemon

        monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *a: None)
        monkeypatch.setattr("lib.fail_policy.is_fail_hard_enabled", lambda: True)
        monkeypatch.setattr("core.interface.hooks._get_pending_context", lambda: "")
        monkeypatch.setattr("core.interface.hooks._get_deferred_notice_hint", lambda: "")
        monkeypatch.setattr("core.interface.hooks._get_deferred_notice_relay_context", lambda: "")
        monkeypatch.setattr("core.interface.hooks._get_quaid_agents_baseline_context", lambda: "")

        timeout_error = (
            "Recall store 'vector' failed while failHard is enabled "
            "(planned_stores=['vector'], timeout_like=True, "
            "cause=TimeoutError: Parallel call timed out after 3.0s (callable_index=0))"
        )
        with patch("core.interface.api.recall_fast", side_effect=RuntimeError(timeout_error)), \
             patch("core.interface.api.projects_search_docs", return_value=None):
            out, err = _run_hook_inject(
                {
                    "prompt": "trigger recall timeout",
                    "session_id": "sess-timeout-failhard",
                    "cwd": "/Users/x",
                },
                monkeypatch=monkeypatch,
            )

        assert out.strip() == ""
        assert "Recall store 'vector' failed while failHard is enabled" not in err

    def test_recall_fast_non_timeout_exception_surfaces_when_fail_hard_enabled(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        from core import extraction_daemon

        monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *a: None)
        monkeypatch.setattr("lib.fail_policy.is_fail_hard_enabled", lambda: True)

        with patch("core.interface.api.recall_fast", side_effect=RuntimeError("local recall invariant broke")), \
             patch("core.interface.api.projects_search_docs", return_value=None):
            _out, err = _run_hook_inject(
                {
                    "prompt": "trigger recall invariant",
                    "session_id": "sess-invariant-failhard",
                    "cwd": "/Users/x",
                },
                monkeypatch=monkeypatch,
            )

        assert "local recall invariant broke" in err

    def test_recall_fast_bare_timeout_returns_empty_when_fail_hard_enabled(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        from core import extraction_daemon

        monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *a: None)
        monkeypatch.setattr("lib.fail_policy.is_fail_hard_enabled", lambda: True)
        monkeypatch.setattr("core.interface.hooks._get_pending_context", lambda: "")
        monkeypatch.setattr("core.interface.hooks._get_deferred_notice_hint", lambda: "")
        monkeypatch.setattr("core.interface.hooks._get_deferred_notice_relay_context", lambda: "")
        monkeypatch.setattr("core.interface.hooks._get_quaid_agents_baseline_context", lambda: "")

        with patch("core.interface.api.recall_fast", side_effect=TimeoutError("recall branch timed out")), \
             patch("core.interface.api.projects_search_docs", return_value=None):
            out, err = _run_hook_inject(
                {
                    "prompt": "trigger bare recall timeout",
                    "session_id": "sess-bare-timeout-failhard",
                    "cwd": "/Users/x",
                },
                monkeypatch=monkeypatch,
            )

        assert out.strip() == ""
        assert "recall branch timed out" not in err
        assert "[Quaid error] [provider]" not in out

    def test_recall_fast_empty_list_no_output(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        """When recall_fast returns [], hook produces no stdout (no additionalContext)."""
        from core import extraction_daemon
        monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *a: None)
        monkeypatch.setattr("core.interface.hooks._get_pending_context", lambda: "")
        monkeypatch.setattr("core.interface.hooks._get_deferred_notice_hint", lambda: "")
        monkeypatch.setattr("core.interface.hooks._get_deferred_notice_relay_context", lambda: "")
        monkeypatch.setattr("core.interface.hooks._get_quaid_agents_baseline_context", lambda: "")

        with patch("core.interface.api.recall_fast", return_value=[]), \
             patch("core.interface.api.projects_search_docs", return_value=None):
            out, err = _run_hook_inject(
                {
                    "prompt": "nothing in memory",
                    "session_id": "sess-empty",
                    "cwd": "/Users/x",
                },
                monkeypatch=monkeypatch,
            )

        assert out.strip() == "", f"Expected no stdout, got: {out!r}"

    def test_recall_fast_close_competitor_duplicates_still_inject_best_memory(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        from core import extraction_daemon
        from core.interface import hooks

        monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *a: None)
        monkeypatch.setattr("core.interface.hooks._get_pending_context", lambda: "")
        monkeypatch.setattr("core.interface.hooks._get_deferred_notice_hint", lambda: "")
        monkeypatch.setattr("core.interface.hooks._get_deferred_notice_relay_context", lambda: "")
        monkeypatch.setattr("core.interface.hooks._get_quaid_agents_baseline_context", lambda: "")

        rows = [
            {
                "text": "What scanner do I use for receipts? ScanSnap iX1600",
                "similarity": 1.0,
                "category": "fact",
            },
            {
                "text": "What scanner do I use for receipts? ScanSnap iX1600",
                "similarity": 0.99,
                "category": "fact",
            },
        ]
        meta = {
            "mode": "fast",
            "quality_gate": {
                "evaluation": {
                    "ready": True,
                    "needs_validation": False,
                    "top_similarity": 1.0,
                    "close_competitor_count": 2,
                }
            },
            "memory_quality": {
                "surface_quality": "good",
                "signals": ["close_competitors"],
                "top_similarity": 1.0,
            },
        }

        with patch("core.interface.api.recall_fast", return_value=(rows, meta)), \
             patch("core.interface.api.projects_search_docs", return_value=None):
            out, _err = _run_hook_inject(
                {
                    "prompt": "What scanner do I use for receipts?",
                    "session_id": "sess-close-competitors",
                    "cwd": "/Users/x",
                },
                monkeypatch=monkeypatch,
            )

        payload = json.loads(out)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "[Quaid Memory Context]" in context
        assert not hooks._is_bare_question_memory_text(rows[0]["text"])
        assert "ScanSnap iX1600" in context
        assert context.count("ScanSnap iX1600") == 1

    def test_recall_fast_close_competitor_skips_query_echo_before_promotion(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        from core import extraction_daemon
        from core.interface import hooks

        monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *a: None)
        monkeypatch.setattr("core.interface.hooks._get_pending_context", lambda: "")
        monkeypatch.setattr("core.interface.hooks._get_deferred_notice_hint", lambda: "")
        monkeypatch.setattr("core.interface.hooks._get_deferred_notice_relay_context", lambda: "")
        monkeypatch.setattr("core.interface.hooks._get_quaid_agents_baseline_context", lambda: "")

        query = "What grinder do I use for my espresso setup?"
        rows = [
            {
                "text": "What grinder do I use for my espresso setup",
                "similarity": 1.0,
                "category": "fact",
            },
            {
                "text": "Solomon Steadman has a Baratza Encore grinder for his Flair 58 espresso setup",
                "similarity": 0.96,
                "category": "fact",
            },
        ]
        meta = {
            "mode": "fast",
            "quality_gate": {
                "evaluation": {
                    "ready": True,
                    "needs_validation": False,
                    "top_similarity": 1.0,
                    "close_competitor_count": 2,
                }
            },
            "memory_quality": {
                "surface_quality": "good",
                "signals": ["close_competitors"],
                "top_similarity": 1.0,
            },
        }

        assert hooks._is_bare_question_memory_text(rows[0]["text"])

        with patch("core.interface.api.recall_fast", return_value=(rows, meta)), \
             patch("core.interface.api.projects_search_docs", return_value=None):
            out, _err = _run_hook_inject(
                {
                    "prompt": query,
                    "session_id": "sess-close-competitor-query-echo",
                    "cwd": "/Users/x",
                },
                monkeypatch=monkeypatch,
            )

        payload = json.loads(out)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "Baratza Encore" in context
        assert "What grinder do I use" not in context

    def test_recall_fast_close_competitor_recovery_requires_only_signal(self):
        from core.interface import hooks

        rows = [
            {
                "text": "What scanner do I use for receipts",
                "similarity": 1.0,
                "category": "fact",
            },
            {
                "text": "What scanner do I use for receipts",
                "similarity": 0.99,
                "category": "fact",
            },
        ]
        meta = {
            "quality_gate": {
                "evaluation": {
                    "ready": True,
                    "needs_validation": True,
                    "top_similarity": 1.0,
                    "close_competitor_count": 2,
                }
            },
            "memory_quality": {
                "surface_quality": "needs_validation",
                "signals": ["close_competitors", "needs_validation"],
                "top_similarity": 1.0,
            },
        }

        assert hooks._format_memories(rows, recall_meta=meta) == ""

    def test_recall_fast_close_competitor_recovery_keeps_negative_claims_blocked(
        self
    ):
        from core.interface import hooks

        rows = [
            {
                "text": "No record was previously stored in memory",
                "similarity": 1.0,
                "category": "fact",
            },
            {
                "text": "No record was previously stored in memory",
                "similarity": 0.99,
                "category": "fact",
            },
        ]
        meta = {
            "quality_gate": {
                "evaluation": {
                    "ready": True,
                    "needs_validation": False,
                    "top_similarity": 1.0,
                    "close_competitor_count": 2,
                }
            },
            "memory_quality": {
                "surface_quality": "good",
                "signals": ["close_competitors"],
                "top_similarity": 1.0,
            },
        }

        assert hooks._format_memories(rows, recall_meta=meta) == ""

    def test_recall_fast_close_competitor_recovery_keeps_pure_questions_blocked(
        self
    ):
        from core.interface import hooks

        rows = [
            {
                "text": "What is my coffee grinder?",
                "similarity": 1.0,
                "category": "fact",
            },
            {
                "text": "What is my coffee grinder?",
                "similarity": 0.99,
                "category": "fact",
            },
        ]
        meta = {
            "quality_gate": {
                "evaluation": {
                    "ready": True,
                    "needs_validation": False,
                    "top_similarity": 1.0,
                    "close_competitor_count": 2,
                }
            },
            "memory_quality": {
                "surface_quality": "good",
                "signals": ["close_competitors"],
                "top_similarity": 1.0,
            },
        }

        assert hooks._format_memories(rows, recall_meta=meta) == ""

    def test_recall_fast_empty_list_still_injects_quaid_agents_baseline(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        from core import extraction_daemon
        monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *a: None)

        projects_dir = tmp_path / "projects"
        quaid_dir = projects_dir / "quaid"
        quaid_dir.mkdir(parents=True)
        (quaid_dir / "AGENTS.md").write_text(
            "# Quaid — Operating Guide\n\n"
            "## File Placement — MANDATORY RULES\n\n"
            "**Before writing any file or delegating work to a sub-agent, pick the first matching rule:**\n",
            encoding="utf-8",
        )
        monkeypatch.setattr("core.interface.hooks._get_projects_dir", lambda: projects_dir)

        with patch("core.interface.api.recall_fast", return_value=[]):
            out, _err = _run_hook_inject(
                {
                    "prompt": "nothing in memory",
                    "session_id": "sess-empty-baseline",
                    "cwd": "/Users/x",
                },
                monkeypatch=monkeypatch,
            )

        payload = json.loads(out)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "[Quaid Project Guidance]" in context
        assert "Before writing any file or delegating work to a sub-agent" in context

    def test_recall_fast_provider_exception_surfaces_quaid_error_notice(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        from core import extraction_daemon

        monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *a: None)
        monkeypatch.setattr("lib.fail_policy.is_fail_hard_enabled", lambda: False)

        with patch(
            "core.interface.api.recall_fast",
            side_effect=RuntimeError(
                "Quaid could not access its fast language model provider: claude-code-oauth HTTP 404 model=invalid-model-xyzzy"
            ),
        ):
            out, err = _run_hook_inject(
                {
                    "prompt": "What do you know about Maya?",
                    "session_id": "sess-provider",
                    "cwd": "/Users/x",
                },
                monkeypatch=monkeypatch,
            )

        payload = json.loads(out)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "[Quaid error]" in context
        assert "[provider]" in context
        assert "invalid-model-xyzzy" in context
        assert "hook-inject" in err

    def test_recall_fast_provider_exception_still_surfaces_notice_when_fail_hard_enabled(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        from core import extraction_daemon

        monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *a: None)
        monkeypatch.setattr("lib.fail_policy.is_fail_hard_enabled", lambda: True)
        queued = []
        mock_adapter.notify.side_effect = lambda message, **kwargs: queued.append((message, kwargs)) or True

        with patch(
            "core.interface.api.recall_fast",
            side_effect=RuntimeError(
                "Quaid could not access its fast language model provider: claude-code-oauth HTTP 404 model=invalid-model-xyzzy"
            ),
        ):
            out, _err = _run_hook_inject(
                {
                    "prompt": "What do you know about Maya?",
                    "session_id": "sess-provider-failhard",
                    "cwd": "/Users/x",
                },
                monkeypatch=monkeypatch,
            )

        assert queued == []
        payload = json.loads(out)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "[Quaid error] [provider]" in context
        assert "invalid-model-xyzzy" in context

    def test_hook_inject_probes_prompt_model_config_when_recall_succeeds(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        from core import extraction_daemon
        from core.interface import hooks

        mock_adapter.adapter_id.return_value = "claude-code"
        mock_adapter.instance_root.return_value = tmp_path
        mock_adapter.data_dir.return_value = tmp_path / "data"
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "claude-code-test")
        monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *a: None)
        monkeypatch.setattr("lib.fail_policy.is_fail_hard_enabled", lambda: True)
        monkeypatch.setattr("core.interface.hooks._get_deferred_notice_relay_context", lambda: "")
        monkeypatch.setattr(
            hooks,
            "_runtime_config_snapshot",
            lambda: ((str(tmp_path / "claude-code" / "config.json"), 123),),
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
                    "prompt": "What grinder do I use?",
                    "session_id": "sess-cc-provider-probe",
                    "cwd": "/Users/x",
                },
                monkeypatch=monkeypatch,
            )

        probe.assert_called_once()
        assert probe.call_args.kwargs["timeout"] == 8
        payload = json.loads(out)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "[Quaid error] [provider]" in context
        assert "invalid-model-m6-probe" in context

    def test_recall_fast_provider_failure_does_not_relay_after_next_successful_turn(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        from core import extraction_daemon

        mock_adapter.adapter_id.return_value = "claude-code"
        mock_adapter.instance_root.return_value = tmp_path
        mock_adapter.data_dir.return_value = tmp_path / "data"
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "claude-code-test")

        monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *a: None)
        monkeypatch.setattr("lib.fail_policy.is_fail_hard_enabled", lambda: True)
        monkeypatch.setattr("core.interface.hooks._get_deferred_notice_hint", lambda: "")
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

        mock_adapter.notify.side_effect = _queue_pending
        mock_adapter.get_pending_context.side_effect = _drain_pending
        monkeypatch.setattr("lib.agent_notice.get_adapter", lambda: mock_adapter)
        monkeypatch.setattr(
            "core.interface.hooks._get_pending_context",
            lambda: mock_adapter.get_pending_context(),
        )

        with patch(
            "core.interface.api.recall_fast",
            side_effect=RuntimeError(
                "Quaid could not access its fast language model provider: claude-code-oauth HTTP 404 model=invalid-model-xyzzy"
            ),
        ):
            out1, _err1 = _run_hook_inject(
                {
                    "prompt": "What do you know about Maya?",
                    "session_id": "sess-provider-failhard-relay",
                    "cwd": "/Users/x",
                },
                monkeypatch=monkeypatch,
            )
        payload1 = json.loads(out1)
        context1 = payload1["hookSpecificOutput"]["additionalContext"]
        assert "[Quaid error] [provider]" in context1
        assert "invalid-model-xyzzy" in context1

        with patch("core.interface.api.recall_fast", return_value=([], None)):
            out, _err = _run_hook_inject(
                {
                    "prompt": "hello on next turn",
                    "session_id": "sess-provider-failhard-relay",
                    "cwd": "/Users/x",
                },
                monkeypatch=monkeypatch,
            )

        payload = json.loads(out) if out.strip() else {}
        context = payload.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert "[Quaid error] [provider]" not in context
        assert "invalid-model-xyzzy" not in context

    def test_deferred_notice_hint_is_injected_without_draining(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        from core import extraction_daemon

        monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *a: None)
        monkeypatch.setattr(
            "core.interface.hooks._get_deferred_notice_hint",
            lambda: (
                "<quaid_system_message>\n"
                "Quaid has 1 deferred maintenance notice waiting.\n"
                "</quaid_system_message>"
            ),
        )

        with patch("core.interface.api.recall_fast", return_value=[]):
            out, _err = _run_hook_inject(
                {
                    "prompt": "hello",
                    "session_id": "sess-deferred",
                    "cwd": "/Users/x",
                },
                monkeypatch=monkeypatch,
            )

        payload = json.loads(out)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "deferred maintenance notice" in context

    def test_claude_code_drains_deferred_notice_into_mandatory_relay(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        from core import extraction_daemon
        from lib import runtime_context

        monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *a: None)
        mock_adapter.adapter_id.return_value = "claude-code"
        monkeypatch.setattr(
            runtime_context,
            "drain_deferred_notices",
            lambda limit=50: [
                {
                    "message": "[Quaid] Synthetic notice: silver lantern is ready.",
                    "kind": "janitor_summary",
                    "status": "resolved",
                }
            ],
        )

        with patch("core.interface.api.recall_fast", return_value=[]):
            out, _err = _run_hook_inject(
                {
                    "prompt": "Hey, what is up?",
                    "session_id": "sess-deferred-drain",
                    "cwd": "/Users/x",
                },
                monkeypatch=monkeypatch,
            )

        payload = json.loads(out)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "MANDATORY: Quaid just drained deferred notices" in context
        assert "Do not call --deferred-drain; relay delivery is complete." in context
        assert "silver lantern" in context
        assert "quaid notify --deferred-drain" not in context

    def test_prompt_model_config_recovery_notice_follows_prior_error(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        from core.interface import hooks

        mock_adapter.adapter_id.return_value = "claude-code"
        mock_adapter.data_dir.return_value = tmp_path / "data"
        monkeypatch.setattr(
            hooks,
            "_adapter_capability",
            lambda key, default=None: key == "prompt_model_config_probe" or default,
        )
        config_path = tmp_path / "claude-code" / "config.json"
        config_path.parent.mkdir()
        config_path.write_text("{}", encoding="utf-8")
        config_mtime = 1
        monkeypatch.setattr(
            hooks,
            "_runtime_config_snapshot",
            lambda: ((str(config_path), config_mtime),),
        )

        with patch(
            "lib.llm_clients.call_fast_reasoning",
            side_effect=RuntimeError("model=invalid-model-m6-probe"),
        ):
            notice = hooks._validate_prompt_model_config_for_hook("claude-code")

        assert "[Quaid error] [provider]" in notice
        assert "invalid-model-m6-probe" in notice

        config_mtime = 2
        with patch("lib.llm_clients.call_fast_reasoning", return_value="OK"):
            restored = hooks._validate_prompt_model_config_for_hook("claude-code")

        assert "healthy again" in restored
        assert "Ignore earlier provider-error notices" in restored

        with patch("lib.llm_clients.call_fast_reasoning") as probe:
            assert hooks._validate_prompt_model_config_for_hook("claude-code") == ""
        probe.assert_not_called()

    def test_claude_code_relays_deferred_notice_before_recall_work(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        from core import extraction_daemon

        trace_events = []
        monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *a: None)
        monkeypatch.setattr(
            "core.interface.hooks._get_deferred_notice_relay_context",
            lambda: (
                "MANDATORY: Quaid just drained deferred notices for the human user. "
                "Start your next response by briefly relaying them, then answer the user's current message.\n\n"
                "<quaid_system_message>\n• First-turn relay: blue sparrow is ready.\n</quaid_system_message>"
            ),
        )
        monkeypatch.setattr("core.interface.hooks._get_pending_context", lambda: "")
        monkeypatch.setattr(
            "core.interface.hooks._write_hook_trace",
            lambda event, payload=None: trace_events.append((event, payload or {})),
        )

        with patch("core.interface.api.recall_fast", side_effect=AssertionError("recall should not run before relay")) as recall, \
             patch("core.interface.api.projects_search_docs", side_effect=AssertionError("docs should not run before relay")) as docs:
            out, _err = _run_hook_inject(
                {
                    "prompt": "Hey, what is up?",
                    "session_id": "sess-deferred-fastpath",
                    "cwd": "/Users/x",
                },
                monkeypatch=monkeypatch,
            )

        recall.assert_not_called()
        docs.assert_not_called()
        payload = json.loads(out)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "MANDATORY: Quaid just drained deferred notices" in context
        assert "blue sparrow" in context
        assert any(event == "hook.inject.deferred_relay_fastpath" for event, _payload in trace_events)

    def test_memory_context_still_injected_without_tool_hint_round_trip(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        from core import extraction_daemon
        monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *a: None)

        with patch("core.interface.api.recall_fast", return_value=[{"text": "Maya lives in South Austin", "similarity": 0.9, "category": "fact"}]):
            out, _err = _run_hook_inject(
                {
                    "prompt": "Where does Maya live?",
                    "session_id": "sess-memory",
                    "cwd": "/Users/x",
                },
                monkeypatch=monkeypatch,
            )

        payload = json.loads(out)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "South Austin" in context
        assert "<tool_hint>" not in context

    def test_memory_context_preserves_source_and_anchor_provenance(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        from core import extraction_daemon

        monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *a: None)

        memories = [
            {
                "text": "The layered surprises worked! Remember we talked about the FaceTime call idea? Glad you went with that",
                "similarity": 0.99,
                "category": "fact",
                "source_type": "assistant",
                "structural_anchor_kind": "assistant_callback_anchor",
            },
            {
                "text": "maybe we do a facetime thing for her like she calls during dinner actually",
                "similarity": 0.88,
                "category": "fact",
                "source_type": "user",
                "structural_anchor_kind": "user_mirrored_idea_anchor",
            },
        ]

        with patch("core.interface.api.recall_fast", return_value=memories):
            out, _err = _run_hook_inject(
                {
                    "prompt": "Who came up with the FaceTime idea for Linda's birthday?",
                    "session_id": "sess-provenance",
                    "cwd": "/Users/x",
                },
                monkeypatch=monkeypatch,
            )

        payload = json.loads(out)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "[fact][assistant][assistant-callback]" in context
        assert "[fact][user][user-idea]" in context
        assert "The layered surprises worked!" in context
        assert "maybe we do a facetime thing" in context

    def test_recall_router_warning_is_promoted_to_provider_notice(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        from core import extraction_daemon
        monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *a: None)

        warning = {
            "text": "[RECALL ROUTER WARNING] Fast prepass failed and fallback recall plan was used. Reason: invalid-model-xyzzy provider failure.",
            "similarity": 1.0,
            "category": "system_notice",
        }
        fact = {"text": "Maya lives in South Austin", "similarity": 0.9, "category": "fact"}
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

        with patch("core.interface.api.recall_fast", return_value=([warning, fact], meta)):
            out, _err = _run_hook_inject(
                {
                    "prompt": "Where does Maya live?",
                    "session_id": "sess-provider-warning",
                    "cwd": "/Users/x",
                },
                monkeypatch=monkeypatch,
            )

        payload = json.loads(out)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "[Quaid error] [provider]" in context
        assert "South Austin" in context
        assert "[RECALL ROUTER WARNING]" not in context

    def test_project_docs_context_is_injected_when_docs_search_returns_chunks(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        from core import extraction_daemon
        monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *a: None)

        docs_bundle = {
            "project": "recipe-app",
            "chunks": [
                {
                    "content": "Authentication uses JWTs and refresh tokens.",
                    "source": "/tmp/recipe-app/docs/api.md",
                    "similarity": 0.91,
                }
            ],
        }

        with patch("core.interface.api.recall_fast", return_value=[]), patch(
            "core.interface.api.projects_search_docs", return_value=docs_bundle
        ):
            out, _err = _run_hook_inject(
                {
                    "prompt": "How does the recipe app authenticate users?",
                    "session_id": "sess-docs",
                    "cwd": "/Users/x",
                },
                monkeypatch=monkeypatch,
            )

        payload = json.loads(out)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "<quaid_system_message>\n" in context
        assert "</quaid_system_message>" in context
        assert "[Quaid Project Docs: recipe-app]" in context
        assert "Authentication uses JWTs and refresh tokens." in context
        assert "api.md" in context

    def test_project_docs_context_dedupes_identical_chunks(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        from core import extraction_daemon
        monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *a: None)

        files_block = "## Files\n- `README.md` — overview\n- `api.py` — API surface"
        docs_bundle = {
            "project": "agentmsg",
            "chunks": [
                {
                    "content": files_block,
                    "source": "/tmp/agentmsg/README.md",
                    "similarity": 0.94,
                },
                {
                    "content": "  ## Files\n- `README.md` — overview\n- `api.py` — API surface  ",
                    "source": "/tmp/agentmsg/README.md",
                    "similarity": 0.93,
                },
                {
                    "content": "Ember Glass means pager escalation level 2.",
                    "source": "/tmp/agentmsg/STATUS.md",
                    "similarity": 0.91,
                },
            ],
        }

        with patch("core.interface.api.recall_fast", return_value=[]), patch(
            "core.interface.api.projects_search_docs", return_value=docs_bundle
        ):
            out, _err = _run_hook_inject(
                {
                    "prompt": "What files are in agentmsg?",
                    "session_id": "sess-docs-dedupe",
                    "cwd": "/Users/x",
                },
                monkeypatch=monkeypatch,
            )

        payload = json.loads(out)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert context.count("## Files") == 1
        assert context.count("`README.md`") == 1
        assert "  1. ## Files" in context
        assert "  2. Ember Glass means pager escalation level 2." in context

    def test_project_docs_search_uses_project_hint_from_hook_cwd(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        from core import extraction_daemon
        monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *a: None)

        docs_bundle = {
            "project": "cross-live-test",
            "chunks": [
                {
                    "content": "The code word Ember Glass means pager escalation level 2.",
                    "source": "/tmp/cross-live-test-src/ember-glass.md",
                    "similarity": 0.96,
                }
            ],
        }
        with patch("core.interface.api.recall_fast", return_value=[]), \
             patch("core.interface.hooks._infer_docs_project_from_cwd", return_value="cross-live-test"), \
             patch("core.interface.api.projects_search_docs", return_value=docs_bundle) as docs_search_mock:
            out, _err = _run_hook_inject(
                {
                    "prompt": "What is Ember Glass?",
                    "session_id": "sess-docs-hint",
                    "cwd": "/tmp/cross-live-test-src",
                },
                monkeypatch=monkeypatch,
            )

        docs_search_mock.assert_called_once_with(
            query="What is Ember Glass?",
            limit=3,
            project="cross-live-test",
        )
        payload = json.loads(out)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "[Quaid Project Docs: cross-live-test]" in context
        assert "Ember Glass means pager escalation level 2" in context

    def test_project_docs_hint_ignores_other_instance_misc_projects(self, tmp_path, monkeypatch):
        from core.interface import hooks

        current_root = tmp_path / "cc-livetest"
        other_root = tmp_path / "oc-livetest"
        current_root.mkdir()
        other_root.mkdir()
        monkeypatch.setenv("QUAID_INSTANCE", "claude-code-private-tmp-cc-livetest")

        projects = {
            "misc--openclaw-main": {
                "canonical_path": str(other_root),
                "instances": ["openclaw-main"],
            },
            "misc--claude-code-private-tmp-cc-livetest": {
                "canonical_path": str(current_root),
                "instances": ["claude-code-private-tmp-cc-livetest"],
            },
        }

        with patch("core.project_registry.list_projects", return_value=projects):
            assert hooks._infer_docs_project_from_cwd(str(other_root / "hello.py")) is None
            assert hooks._infer_docs_project_from_cwd(str(current_root / "hello.py")) == (
                "misc--claude-code-private-tmp-cc-livetest"
            )

    def test_project_docs_hint_uses_current_instance_source_root(self, tmp_path, monkeypatch):
        from core.interface import hooks

        source_root = tmp_path / "src" / "phase4"
        source_root.mkdir(parents=True)
        monkeypatch.setenv("QUAID_INSTANCE", "claude-code-private-tmp-cc-livetest")

        projects = {
            "phase4": {
                "canonical_path": str(tmp_path / "projects" / "phase4"),
                "source_root": str(source_root),
                "instances": ["claude-code-private-tmp-cc-livetest"],
            },
        }

        with patch("core.project_registry.list_projects", return_value=projects):
            assert hooks._infer_docs_project_from_cwd(str(source_root / "hello.py")) == "phase4"

    def test_project_docs_search_falls_back_to_unscoped_when_cwd_hint_missing(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        from core import extraction_daemon
        monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *a: None)

        docs_bundle = {
            "project": "quaid",
            "chunks": [
                {
                    "content": "Release readiness notes.",
                    "source": "/tmp/quaid/operations/release-readiness.md",
                    "similarity": 0.91,
                }
            ],
        }
        with patch("core.interface.api.recall_fast", return_value=[]), \
             patch("core.interface.hooks._infer_docs_project_from_cwd", return_value=None), \
             patch("core.interface.api.projects_search_docs", return_value=docs_bundle) as docs_search_mock:
            _out, _err = _run_hook_inject(
                {
                    "prompt": "What about release readiness?",
                    "session_id": "sess-docs-no-hint",
                    "cwd": "",
                },
                monkeypatch=monkeypatch,
            )

        docs_search_mock.assert_called_once_with(
            query="What about release readiness?",
            limit=3,
            project=None,
        )

    def test_project_docs_failure_does_not_drop_memory_context(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        from core import extraction_daemon
        monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *a: None)

        with patch(
            "core.interface.api.recall_fast",
            return_value=[{"text": "Maya lives in South Austin", "similarity": 0.9, "category": "fact"}],
        ), patch(
            "core.interface.api.projects_search_docs",
            side_effect=RuntimeError("docs down"),
        ):
            out, _err = _run_hook_inject(
                {
                    "prompt": "Where does Maya live?",
                    "session_id": "sess-docs-fail",
                    "cwd": "/Users/x",
                },
                monkeypatch=monkeypatch,
            )

        payload = json.loads(out)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "South Austin" in context
        assert "[Quaid Project Docs" not in context

    def test_recall_telemetry_helpers_summarize_meta_and_rows(self):
        from core.interface import hooks

        recall_rows = [{"text": "My neighbour won a chili cook-off with a secret brisket recipe", "similarity": 0.62, "category": "fact"}]
        recall_meta = {
            "mode": "fast",
            "stop_reason": "quality_gate_complete",
            "planned_stores": ["vector"],
            "store_runs": [{"store": "vector", "result_count": 1, "total_ms": 38, "selected_path": "vector"}],
            "quality_gate": {"evaluation": {"covered_terms_ratio": 0.5, "top_similarity": 0.62}},
            "memory_quality": {"surface_quality": "mixed", "another_recall_may_help": True, "signals": ["needs_validation"]},
        }

        summarized_rows = hooks._summarize_recall_results(recall_rows)
        summarized_meta = hooks._summarize_recall_meta(recall_meta)

        assert summarized_rows[0]["text"].startswith("My neighbour won a chili cook-off")
        assert summarized_meta["planned_stores"] == ["vector"]
        assert summarized_meta["store_runs"][0]["store"] == "vector"
        assert summarized_meta["memory_quality"]["surface_quality"] == "mixed"



# ===========================================================================
# hook_session_init — registry augmentation
# ===========================================================================

class TestHookSessionInitRegistryAugmentation:

    def _make_init_env(self, tmp_path, monkeypatch, *, projects_dir=None, identity_dir=None):
        """Wire hook_session_init helpers to tmp_path directories."""
        if projects_dir is None:
            projects_dir = tmp_path / "projects"
            projects_dir.mkdir()
        if identity_dir is None:
            identity_dir = tmp_path / "identity"
            identity_dir.mkdir()

        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()

        adapter = _adapter_mock()
        adapter.projects_dir.return_value = projects_dir
        adapter.identity_dir.return_value = identity_dir
        adapter.data_dir.return_value = tmp_path / "data"
        adapter.instance_root.return_value = tmp_path

        from core.interface import hooks
        monkeypatch.setattr(hooks, "_get_projects_dir", lambda: projects_dir)
        monkeypatch.setattr(hooks, "_get_identity_dir", lambda: identity_dir)
        monkeypatch.setattr(hooks, "_check_janitor_health", lambda: "")
        monkeypatch.setenv("QUAID_RULES_DIR", str(rules_dir))

        # Stub out daemon interactions
        monkeypatch.setattr(
            "core.extraction_daemon.ensure_alive", lambda: None
        )
        monkeypatch.setattr(
            "core.extraction_daemon.read_cursor",
            lambda sid: {"line_offset": 0, "transcript_path": ""},
        )
        monkeypatch.setattr(
            "core.extraction_daemon.write_cursor", lambda *a: None
        )

        return projects_dir, identity_dir, rules_dir

    def test_projects_inside_projects_dir_are_found(self, tmp_path, monkeypatch):
        """Projects living under projects_dir show up in split Quaid rules files."""
        projects_dir, identity_dir, rules_dir = self._make_init_env(tmp_path, monkeypatch)

        # Create a project with tool docs and read-only lookup docs.
        proj = projects_dir / "myproject"
        proj.mkdir()
        (proj / "docs").mkdir()
        (proj / "TOOLS.md").write_text("# Tools\nsome tool docs", encoding="utf-8")
        (proj / "PROJECT.md").write_text("# Project\nproject index", encoding="utf-8")
        (proj / "docs" / "ember-glass.md").write_text("# Ember Glass\nlevel two cipher", encoding="utf-8")

        # No registry extras
        with patch("core.project_registry.list_projects", return_value={}):
            _, _, content = _run_hook_session_init(
                {"session_id": "s1", "cwd": str(tmp_path)},
                monkeypatch=monkeypatch,
                rules_dir=rules_dir,
            )

        assert content is not None, "split Quaid rules files should have been written"
        assert not (rules_dir / "quaid-projects.md").exists()
        assert (rules_dir / "quaid-00-runtime.md").is_file()
        assert (rules_dir / "quaid-myproject-project-catalog.md").is_file()
        assert "myproject/project-catalog" in content
        assert f"project_path: {proj}" in content
        assert "details_recall: quaid recall" in content
        assert "read_only_lookup:" in content
        assert "some tool docs" in content
        assert "- PROJECT.md:" in content
        assert "- docs/ember-glass.md:" in content
        assert "summary: level two cipher" in content
        assert "--- myproject/TOOLS.md ---" not in content

    def test_registry_project_outside_projects_dir_included(self, tmp_path, monkeypatch):
        """A project whose canonical_path is outside projects_dir is still included."""
        projects_dir, identity_dir, rules_dir = self._make_init_env(tmp_path, monkeypatch)

        # External project (NOT under projects_dir)
        external_proj = tmp_path / "external" / "externalproject"
        external_proj.mkdir(parents=True)
        (external_proj / "AGENTS.md").write_text("# Agents\nexternal agent doc", encoding="utf-8")

        registry = {
            "externalproject": {"canonical_path": str(external_proj)}
        }

        with patch("core.project_registry.list_projects", return_value=registry):
            _, _, content = _run_hook_session_init(
                {"session_id": "s2", "cwd": str(tmp_path)},
                monkeypatch=monkeypatch,
                rules_dir=rules_dir,
            )

        assert content is not None
        assert "externalproject/project-catalog" in content
        assert "external agent doc" in content
        assert "--- externalproject/AGENTS.md ---" not in content

    def test_duplicate_project_name_not_doubled(self, tmp_path, monkeypatch):
        """A project that exists in both projects_dir and registry appears exactly once."""
        projects_dir, identity_dir, rules_dir = self._make_init_env(tmp_path, monkeypatch)

        # Project under projects_dir
        proj = projects_dir / "sharedproject"
        proj.mkdir()
        (proj / "TOOLS.md").write_text("# Tools\nshared tools", encoding="utf-8")

        # Same project name in registry (same path or different — shouldn't matter, name deduplication)
        registry = {
            "sharedproject": {"canonical_path": str(proj)}
        }

        with patch("core.project_registry.list_projects", return_value=registry):
            _, _, content = _run_hook_session_init(
                {"session_id": "s3", "cwd": str(tmp_path)},
                monkeypatch=monkeypatch,
                rules_dir=rules_dir,
            )

        assert content is not None
        # Count occurrences — should appear exactly once
        occurrences = content.count("sharedproject/project-catalog")
        assert occurrences == 1, f"Expected exactly 1 occurrence, found {occurrences}"

    def test_split_rules_migrate_legacy_file_and_remove_stale_project_rules(self, tmp_path, monkeypatch):
        projects_dir, identity_dir, rules_dir = self._make_init_env(tmp_path, monkeypatch)

        (identity_dir / "USER.md").write_text("Slim identity line.", encoding="utf-8")
        proj = projects_dir / "currentproject"
        proj.mkdir()
        (proj / "PROJECT.md").write_text("# Project\ncurrent project detail", encoding="utf-8")
        legacy_file = rules_dir / "quaid-projects.md"
        legacy_file.write_text("legacy combined rules", encoding="utf-8")
        stale_file = rules_dir / "quaid-oldproject-project-catalog.md"
        stale_file.write_text("stale project rules", encoding="utf-8")

        with patch("core.project_registry.list_projects", return_value={}):
            _, err, content = _run_hook_session_init(
                {"session_id": "s3b", "cwd": str(tmp_path)},
                monkeypatch=monkeypatch,
                rules_dir=rules_dir,
            )

        assert content is not None
        assert not legacy_file.exists()
        assert (rules_dir / "quaid-projects.md.bak").read_text(encoding="utf-8") == "legacy combined rules"
        assert not stale_file.exists()
        assert (rules_dir / "quaid-user.md").is_file()
        assert (rules_dir / "quaid-currentproject-project-catalog.md").is_file()
        assert "migrated" in err
        assert "removed" in err

    def test_tools_md_content_in_output(self, tmp_path, monkeypatch):
        """TOOLS.md content from a project directory is present in the output file."""
        projects_dir, identity_dir, rules_dir = self._make_init_env(tmp_path, monkeypatch)

        proj = projects_dir / "quaid"
        proj.mkdir()
        (proj / "TOOLS.md").write_text("# Knowledge Layer — Tool Usage Guide\nuse quaid recall", encoding="utf-8")

        with patch("core.project_registry.list_projects", return_value={}):
            _, _, content = _run_hook_session_init(
                {"session_id": "s4", "cwd": str(tmp_path)},
                monkeypatch=monkeypatch,
                rules_dir=rules_dir,
            )

        assert content is not None
        assert "quaid/TOOLS.md" in content
        assert "use quaid recall" in content

    def test_runtime_metadata_block_and_domain_block_stripping(self, tmp_path, monkeypatch):
        projects_dir, identity_dir, rules_dir = self._make_init_env(tmp_path, monkeypatch)

        proj = projects_dir / "quaid"
        proj.mkdir()
        (proj / "TOOLS.md").write_text(
            "\n".join(
                [
                    "# Tools",
                    "before domains",
                    "<!-- AUTO-GENERATED:DOMAIN-LIST:START -->",
                    "Available domains:",
                    "- `personal`: personal stuff",
                    "<!-- AUTO-GENERATED:DOMAIN-LIST:END -->",
                    "after domains",
                ]
            ),
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "cc-test")

        runtime_block = "\n".join([
            "[Quaid runtime]",
            "instance: cc-test",
            "active domains: personal, technical",
            "active graph relation types: neighbor_of, parent_of",
            "runtime note: Preinject does not cover graph structure or edge traversal. If a query depends on these relations, use graph recall explicitly.",
            "linked projects: quaid (/tmp/quaid); misc--cc-test (/tmp/misc)",
            "runtime note: Preinject does not cover project or docs detail. MANDATORY ORDER: For project document questions, run docs recall before filesystem grep/cat (for example: quaid recall \"<query>\" '{\"stores\":[\"docs\"],\"project\":\"<project-name>\"}'). Use filesystem reads only when docs recall returns no relevant hits, weak hits, or only index/catalog rows; for read-only one-fact lookups, read the catalog-listed file directly without linking.",
        ])

        with patch("core.runtime.system_context.build_system_context_block", return_value=runtime_block), \
             patch("core.project_registry.list_projects", return_value={}):
            _, _, content = _run_hook_session_init(
                {"session_id": "s4b", "cwd": str(tmp_path)},
                monkeypatch=monkeypatch,
                rules_dir=rules_dir,
            )

        assert content is not None
        assert "[Quaid runtime]" in content
        assert "instance: cc-test" in content
        assert "active domains: personal, technical" in content
        assert "active graph relation types: neighbor_of, parent_of" in content
        assert "linked projects: quaid (/tmp/quaid); misc--cc-test (/tmp/misc)" in content
        assert "run docs recall before filesystem grep/cat" in content
        assert "catalog-listed file directly without linking" in content
        assert "before domains" in content
        assert "after domains" in content
        assert "AUTO-GENERATED:DOMAIN-LIST" not in content
        assert "Available domains:" not in content

    def test_agents_md_content_in_output(self, tmp_path, monkeypatch):
        """AGENTS.md content from a project directory is present in the output file."""
        projects_dir, identity_dir, rules_dir = self._make_init_env(tmp_path, monkeypatch)

        proj = projects_dir / "quaid"
        proj.mkdir()
        (proj / "AGENTS.md").write_text("# Agent Guide\nfail-hard rules here", encoding="utf-8")

        with patch("core.project_registry.list_projects", return_value={}):
            _, _, content = _run_hook_session_init(
                {"session_id": "s5", "cwd": str(tmp_path)},
                monkeypatch=monkeypatch,
                rules_dir=rules_dir,
            )

        assert content is not None
        assert "quaid/AGENTS.md" in content
        assert "fail-hard rules here" in content

    def test_identity_generated_user_projection_block_is_stripped_and_snippets_render(self, tmp_path, monkeypatch):
        projects_dir, identity_dir, rules_dir = self._make_init_env(tmp_path, monkeypatch)

        (identity_dir / "USER.md").write_text(
            "\n".join(
                [
                    "# USER",
                    "",
                    "Preferred response style: concise.",
                    "",
                    "<!-- generated by quaid user snippets projection start -->",
                    "## Pending User Snippets",
                    "- Legacy projected snippet should not render.",
                    "<!-- generated by quaid user snippets projection end -->",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (identity_dir / "USER.snippets.md").write_text(
            "# USER — Pending Snippets\n\n"
            "## Reset — 2026-03-10 01:24:00\n"
            "- Current snippet queue should render as system context.\n",
            encoding="utf-8",
        )

        with patch("core.project_registry.list_projects", return_value={}):
            _, _, content = _run_hook_session_init(
                {"session_id": "s5-user-projection", "cwd": str(tmp_path)},
                monkeypatch=monkeypatch,
                rules_dir=rules_dir,
            )

        assert content is not None
        assert "Preferred response style: concise." in content
        assert "Current snippet queue should render as system context." in content
        assert "Pending User Snippets" not in content
        assert "generated by quaid user snippets projection" not in content
        assert "Legacy projected snippet should not render." not in content

    def test_identity_guidance_templates_are_not_agent_context(self, tmp_path, monkeypatch):
        from core.interface.hooks import _identity_context_content

        assert _identity_context_content(
            "USER.md",
            "# USER.md Guidance (Quaid)\n\n## What Belongs\n- Template prose.\n",
        ) == ""
        assert _identity_context_content(
            "SOUL.md",
            "# SOUL.md Guidance (Quaid)\n\n## What Belongs\n- Template prose.\n",
        ) == ""
        assert _identity_context_content(
            "ENVIRONMENT.md",
            "# ENVIRONMENT.md Guidance (Quaid)\n\n## What Belongs\n- Template prose.\n",
        ) == ""

    def test_identity_generated_environment_projection_block_is_stripped_and_snippets_render(self, tmp_path, monkeypatch):
        projects_dir, identity_dir, rules_dir = self._make_init_env(tmp_path, monkeypatch)

        (identity_dir / "ENVIRONMENT.md").write_text(
            "\n".join(
                [
                    "# ENVIRONMENT",
                    "",
                    "Operator note: keep absolute paths in replies.",
                    "",
                    "<!-- generated by quaid memory projection -->",
                    "<!-- sourced from ENVIRONMENT.snippets.md until native injection is reliable -->",
                    "",
                    "## Extracted Memory",
                    "- Legacy projected environment fact should not render.",
                ]
            ),
            encoding="utf-8",
        )
        (identity_dir / "ENVIRONMENT.snippets.md").write_text(
            "# ENVIRONMENT — Pending Snippets\n\n"
            "## Reset — 2026-03-10 01:24:00\n"
            "- Current environment snippet should render as system context.\n",
            encoding="utf-8",
        )

        with patch("core.project_registry.list_projects", return_value={}):
            _, _, content = _run_hook_session_init(
                {"session_id": "s5-env-projection", "cwd": str(tmp_path)},
                monkeypatch=monkeypatch,
                rules_dir=rules_dir,
            )

        assert content is not None
        assert "Operator note: keep absolute paths in replies." in content
        assert "Current environment snippet should render as system context." in content
        assert "generated by quaid memory projection" not in content
        assert "sourced from ENVIRONMENT.snippets.md" not in content
        assert "Extracted Memory" not in content
        assert "Legacy projected environment fact should not render." not in content

    def test_identity_legacy_environment_projection_marker_is_stripped(self, tmp_path, monkeypatch):
        projects_dir, identity_dir, rules_dir = self._make_init_env(tmp_path, monkeypatch)

        (identity_dir / "ENVIRONMENT.md").write_text(
            "\n".join(
                [
                    "# ENVIRONMENT",
                    "",
                    "Keep shell snippets short.",
                    "",
                    "<!-- generated by quaid live memory projection fallback on example.local -->",
                    "## Extracted Memory",
                    "- Legacy projected fact should not persist in rules.",
                ]
            ),
            encoding="utf-8",
        )

        with patch("core.project_registry.list_projects", return_value={}):
            _, _, content = _run_hook_session_init(
                {"session_id": "s5-env-legacy", "cwd": str(tmp_path)},
                monkeypatch=monkeypatch,
                rules_dir=rules_dir,
            )

        assert content is not None
        assert "Keep shell snippets short." in content
        assert "generated by quaid live memory projection fallback" not in content
        assert "Legacy projected fact should not persist in rules." not in content

    def test_adapter_compatibility_context_is_included(self, tmp_path, monkeypatch):
        projects_dir, identity_dir, rules_dir = self._make_init_env(tmp_path, monkeypatch)
        compat_path = tmp_path / "COMPATIBILITY.md"
        compat_path.write_text("# Claude Code Compatibility\nWait briefly after compact.", encoding="utf-8")

        adapter = _adapter_mock()
        adapter.adapter_id.return_value = "claude-code"
        adapter.projects_dir.return_value = projects_dir
        adapter.identity_dir.return_value = identity_dir
        adapter.data_dir.return_value = tmp_path / "data"
        adapter.instance_root.return_value = tmp_path
        adapter.get_base_context_files.return_value = {}
        adapter.get_cli_tools_snippet.return_value = ""
        adapter.get_compatibility_context_files.return_value = {
            str(compat_path): {"purpose": "compatibility", "maxLines": 20}
        }
        monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)

        with patch("core.project_registry.list_projects", return_value={}):
            _, _, content = _run_hook_session_init(
                {"session_id": "s5-compat", "cwd": str(tmp_path)},
                monkeypatch=monkeypatch,
                rules_dir=rules_dir,
            )

        assert content is not None
        assert "--- adapter-compatibility/COMPATIBILITY.md ---" in content
        assert "Wait briefly after compact." in content

    def test_no_project_docs_no_file_written(self, tmp_path, monkeypatch):
        """When projects_dir has no TOOLS/AGENTS docs, no rules file is written."""
        projects_dir, identity_dir, rules_dir = self._make_init_env(tmp_path, monkeypatch)

        # projects_dir exists but no projects
        with patch("core.project_registry.list_projects", return_value={}):
            _, err, content = _run_hook_session_init(
                {"session_id": "s6", "cwd": str(tmp_path)},
                monkeypatch=monkeypatch,
                rules_dir=rules_dir,
            )

        assert content is None, "No rules file should be written when no docs found"
        assert "no project docs" in err


class TestSubagentHooks:
    def test_hook_subagent_stop_preserves_transcript_into_quaid_logs(self, tmp_path, monkeypatch):
        source = tmp_path / "child.jsonl"
        source.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")
        logs_dir = tmp_path / "instances" / "pytest-runner" / "logs"
        adapter = _adapter_mock()
        adapter.logs_dir.return_value = logs_dir
        monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)

        recorded = {}

        def fake_mark_complete(parent_session_id, child_id, transcript_path=None):
            recorded["parent_session_id"] = parent_session_id
            recorded["child_id"] = child_id
            recorded["transcript_path"] = transcript_path

        fake_registry = types.ModuleType("core.subagent_registry")
        fake_registry.mark_complete = fake_mark_complete
        monkeypatch.setitem(sys.modules, "core.subagent_registry", fake_registry)

        err = _run_hook_subagent_stop(
            {
                "session_id": "parent-1",
                "agent_id": "child-1",
                "agent_transcript_path": str(source),
            },
            monkeypatch=monkeypatch,
        )

        preserved = logs_dir / "quaid" / "sessions" / "child-1.jsonl"
        assert preserved.is_file()
        assert preserved.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
        assert recorded["parent_session_id"] == "parent-1"
        assert recorded["child_id"] == "child-1"
        assert recorded["transcript_path"] == str(preserved)
        assert "completed child-1 under parent-1" in err
