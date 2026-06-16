"""Tests for lib/adapter.py — platform adapter layer."""

import json
import os
import re
import shutil
import sys
import builtins
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

# Ensure plugin root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.adapter import (
    QuaidAdapter,
    StandaloneAdapter,
    TestAdapter,
    ChannelInfo,
    get_owner_id,
    get_adapter,
    set_adapter,
    reset_adapter,
    _project_instance_binding_path,
    _read_project_instance_binding,
    _read_env_file,
    _bootstrap_instance_env,
)
from lib.instance import _legacy_instance_slug_from_project_dir, instance_slug_from_project_dir
from lib.providers import (
    AnthropicLLMProvider,
    ClaudeCodeLLMProvider,
    OpenAICodexOAuthLLMProvider,
    OpenAICompatibleLLMProvider,
    TestLLMProvider,
)
from adaptors.openclaw.adapter import OpenClawAdapter
from adaptors.claude_code.adapter import ClaudeCodeAdapter
from adaptors.codex.adapter import CodexAdapter


def _write_adapter_config(tmp_path: Path, adapter_type: str) -> None:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(f'{{"adapter": {{"type": "{adapter_type}"}}}}')


def _write_shared_platform_config(tmp_path: Path, adapter_type: str) -> None:
    cfg_dir = tmp_path / "shared" / "config" / adapter_type
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(f'{{"adapter": {{"type": "{adapter_type}"}}}}')


def _write_adapter_manifest(
    tmp_path: Path,
    adapter_id: str,
    module_name: str,
    class_name: str,
    runtime_path: str = "",
) -> None:
    reg = tmp_path / "adaptors" / adapter_id
    reg.mkdir(parents=True, exist_ok=True)
    runtime = {"module": module_name, "class": class_name}
    if runtime_path:
        runtime["path"] = [runtime_path]
    payload = {
        "schema": "quaid-adapter-install/v1",
        "id": adapter_id,
        "name": adapter_id,
        "install": {"selectLabel": adapter_id},
        "runtime": {"python": runtime},
    }
    (reg / "adapter.json").write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_adapter():
    """Reset adapter singleton between tests."""
    reset_adapter()
    yield
    reset_adapter()


@pytest.fixture
def standalone(tmp_path, monkeypatch):
    """Create a StandaloneAdapter with a temp home dir."""
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path))
    adapter = StandaloneAdapter(home=tmp_path)
    set_adapter(adapter)
    return adapter


@pytest.fixture
def openclaw_adapter(tmp_path, monkeypatch):
    """Create an OpenClawAdapter with a test API key."""
    monkeypatch.setenv("OPENCLAW_WORKSPACE", str(tmp_path))
    # Write a .env with a test API key so get_llm_provider() works
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-test-fixture\n")
    adapter = OpenClawAdapter()
    set_adapter(adapter)
    return adapter


# ---------------------------------------------------------------------------
# StandaloneAdapter Tests
# ---------------------------------------------------------------------------

class TestStandaloneAdapter:
    def test_quaid_home_default(self, tmp_path, monkeypatch):
        monkeypatch.delenv("QUAID_HOME", raising=False)
        adapter = StandaloneAdapter()
        assert adapter.quaid_home() == Path.home() / ".quaid"

    def test_quaid_home_env_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        adapter = StandaloneAdapter()
        assert adapter.quaid_home() == tmp_path

    def test_quaid_home_explicit(self, tmp_path):
        adapter = StandaloneAdapter(home=tmp_path)
        assert adapter.quaid_home() == tmp_path

    def test_data_dir(self, standalone, tmp_path, monkeypatch):
        iid = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        assert standalone.data_dir() == tmp_path / "instances" / iid / "data"

    def test_config_dir(self, standalone, tmp_path):
        iid = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        assert standalone.config_dir() == tmp_path / "instances" / iid

    def test_logs_dir(self, standalone, tmp_path):
        iid = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        assert standalone.logs_dir() == tmp_path / "instances" / iid / "logs"

    def test_journal_dir(self, standalone, tmp_path):
        iid = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        assert standalone.journal_dir() == tmp_path / "instances" / iid / "journal"

    def test_projects_dir(self, standalone, tmp_path):
        assert standalone.projects_dir() == tmp_path / "projects"

    def test_core_markdown_dir(self, standalone, tmp_path):
        iid = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        assert standalone.core_markdown_dir() == tmp_path / "instances" / iid

    def test_instance_root(self, standalone, tmp_path):
        iid = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        assert standalone.instance_root() == tmp_path / "instances" / iid

    def test_notify_stderr(self, standalone, capsys):
        result = standalone.notify("hello world")
        assert result is True
        captured = capsys.readouterr()
        assert "hello world" in captured.err

    def test_notify_disabled(self, standalone, monkeypatch, capsys):
        monkeypatch.setenv("QUAID_DISABLE_NOTIFICATIONS", "1")
        result = standalone.notify("should be silent")
        assert result is True
        captured = capsys.readouterr()
        assert "should be silent" not in captured.err

    def test_notify_dry_run(self, standalone, capsys):
        result = standalone.notify("dry run test", dry_run=True)
        assert result is True
        captured = capsys.readouterr()
        assert "dry-run" in captured.err

    def test_get_last_channel_returns_none(self, standalone):
        assert standalone.get_last_channel() is None

    def test_get_api_key_from_env(self, standalone, monkeypatch):
        monkeypatch.setenv("TEST_API_KEY", "sk-test-123")
        assert standalone.get_api_key("TEST_API_KEY") == "sk-test-123"

    def test_get_api_key_from_env_file(self, standalone, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text('MY_KEY=sk-from-file\n')
        with patch("lib.adapter.is_fail_hard_enabled", return_value=False):
            assert standalone.get_api_key("MY_KEY") == "sk-from-file"

    def test_get_api_key_env_file_with_quotes(self, standalone, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text('MY_KEY="sk-quoted"\n')
        with patch("lib.adapter.is_fail_hard_enabled", return_value=False):
            assert standalone.get_api_key("MY_KEY") == "sk-quoted"

    def test_get_api_key_env_file_blocked_when_failhard_enabled(self, standalone, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text('MY_KEY=sk-from-file\n')
        with patch("lib.adapter.is_fail_hard_enabled", return_value=True):
            assert standalone.get_api_key("MY_KEY") is None

    def test_get_api_key_missing(self, standalone, monkeypatch):
        monkeypatch.delenv("MISSING_KEY", raising=False)
        assert standalone.get_api_key("MISSING_KEY") is None

    def test_get_sessions_dir_missing(self, standalone, tmp_path):
        assert standalone.get_sessions_dir() is None

    def test_get_sessions_dir_exists(self, standalone, tmp_path):
        (tmp_path / "sessions").mkdir()
        assert standalone.get_sessions_dir() == tmp_path / "sessions"

    def test_get_session_path_missing(self, standalone, tmp_path):
        (tmp_path / "sessions").mkdir()
        assert standalone.get_session_path("nonexistent") is None

    def test_get_session_path_exists(self, standalone, tmp_path):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        session_file = sessions / "test-session.jsonl"
        session_file.write_text("{}")
        assert standalone.get_session_path("test-session") == session_file

    def test_filter_system_messages_always_false(self, standalone):
        assert standalone.filter_system_messages("HEARTBEAT_OK") is False
        assert standalone.filter_system_messages("GatewayRestart: ...") is False
        assert standalone.filter_system_messages("normal message") is False

    def test_installer_provider_defaults(self, standalone):
        assert "anthropic" in standalone.installer_supported_providers()
        defaults = standalone.installer_default_models("anthropic")
        assert defaults == {"deep": "claude-sonnet-4-5", "fast": "claude-haiku-4-5"}
        assert standalone.installer_default_models("openai") == {
            "deep": "gpt-5.4",
            "fast": "gpt-5.4-mini",
        }
        assert standalone.get_deep_provider_default() == "anthropic"
        assert standalone.get_fast_provider_default() == "anthropic"

    def test_installer_review_model_pair_flags_unknown_provider(self, standalone):
        review = standalone.installer_review_model_pair(
            "kimik",
            "kimik-2.5-pro",
            "kimik-2.5-fast",
        )
        assert review["needsClarification"] is True
        assert "kimik" in review["reason"]

    def test_build_transcript_uses_adapter_filters_only(self, standalone):
        transcript = standalone.build_transcript([
            {"role": "user", "content": "GatewayRestart: reconnecting"},
            {"role": "user", "content": "Normal user message"},
            {"role": "assistant", "content": "HEARTBEAT check HEARTBEAT_OK"},
            {"role": "assistant", "content": "Normal assistant reply"},
        ])
        assert "GatewayRestart" in transcript
        assert "HEARTBEAT_OK" in transcript
        assert "User: Normal user message" in transcript
        assert "Assistant: Normal assistant reply" in transcript

    def test_build_transcript_strips_quaid_system_notice_leadin(self, standalone):
        transcript = standalone.build_transcript([
            {
                "role": "user",
                "content": (
                    "MANDATORY: Quaid has active notices for the human user. "
                    "Begin your next response by relaying each notice below.\n\n"
                    "<quaid_system_message>\n"
                    "• [Quaid — Janitor] Edges created: 3\n"
                    "</quaid_system_message>\n\n"
                    "My Friday ritual uses marker marigold-anvil-5816."
                ),
            },
        ])

        assert "MANDATORY: Quaid" not in transcript
        assert "quaid_system_message" not in transcript
        assert "Edges created" not in transcript
        assert transcript == "User: My Friday ritual uses marker marigold-anvil-5816."

    def test_build_transcript_strips_oc_timestamp_with_unicode_day_label(self, standalone):
        transcript = standalone.build_transcript([
            {
                "role": "user",
                "content": "[月 2026-03-22 08:14 JST] Meeting notes start here.",
            },
            {
                "role": "user",
                "content": "[重要] Keep this bracketed user content.",
            },
            {
                "role": "user",
                "content": "[Action 2026-01-15 10:00] Keep this dated bracketed note.",
            },
        ])

        assert "2026-03-22 08:14" not in transcript
        assert "User: Meeting notes start here." in transcript
        assert "User: [重要] Keep this bracketed user content." in transcript
        assert "User: [Action 2026-01-15 10:00] Keep this dated bracketed note." in transcript

    def test_parse_session_jsonl_uses_adapter_transcript_rules(self, standalone, tmp_path):
        import json
        jsonl_file = tmp_path / "session.jsonl"
        jsonl_file.write_text("\n".join([
            json.dumps({"role": "user", "content": "GatewayRestart: noisy"}),
            json.dumps({"role": "assistant", "content": "Real content"}),
        ]))
        transcript = standalone.parse_session_jsonl(jsonl_file)
        assert "GatewayRestart" in transcript
        assert "Assistant: Real content" in transcript

    def test_gateway_config_returns_none(self, standalone):
        assert standalone.get_gateway_config_path() is None

    def test_repo_slug(self, standalone):
        assert standalone.get_repo_slug() == "quaid-labs/quaid"

    def test_install_url(self, standalone):
        url = standalone.get_install_url()
        assert "quaid-labs/quaid" in url
        assert "install.sh" in url


class TestOwnerResolution:
    def test_get_owner_id_reads_quaid_home_config(self, tmp_path, monkeypatch):
        from config import reload_config

        iid = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        cfg_dir = tmp_path / "instances" / iid
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "config.json").write_text(
            """
            {
              "adapter": {"type": "claude-code"},
              "users": {"defaultOwner": "owner-user"}
            }
            """.strip()
        )
        reload_config()
        assert get_owner_id() == "owner-user"

    def test_get_owner_id_logs_config_failure_before_default(self, caplog):
        caplog.set_level("WARNING")

        with patch("config.get_config", side_effect=RuntimeError("config boom")):
            assert get_owner_id() == "default"

        assert "Owner id config lookup failed; defaulting to 'default': config boom" in caplog.text


class TestBaseAdapterConfig:
    def test_get_capability_raises_config_failure_when_failhard(self):
        adapter = CodexAdapter()

        with patch("config.get_config", side_effect=RuntimeError("config boom")), \
             patch("lib.adapter.is_fail_hard_enabled", return_value=True):
            with pytest.raises(RuntimeError, match="config boom"):
                adapter.get_capability("turn_scoped_provider_notices", False)

    def test_get_capability_warns_and_uses_adapter_default_when_fail_open(self, caplog):
        adapter = CodexAdapter()
        caplog.set_level("WARNING")

        with patch("config.get_config", side_effect=RuntimeError("config boom")), \
             patch("lib.adapter.is_fail_hard_enabled", return_value=False):
            assert adapter.get_capability("turn_scoped_provider_notices", False) is True

        assert "Failed to read adapter capability 'turn_scoped_provider_notices' from config: config boom" in caplog.text

    def test_installer_install_state_logs_instance_scan_failure(self, tmp_path, monkeypatch, caplog):
        instances_dir = tmp_path / "instances"
        instances_dir.mkdir()

        def _raise_iterdir(self):
            if self == instances_dir:
                raise OSError("scan boom")
            return iter(())

        monkeypatch.setattr(Path, "iterdir", _raise_iterdir)
        caplog.set_level("WARNING")

        state = StandaloneAdapter.installer_install_state(str(tmp_path))

        assert state["status"] == "can_install"
        assert "Installer state scan failed for standalone" in caplog.text
        assert "scan boom" in caplog.text

    def test_bootstrap_instance_env_logs_bad_claude_settings_and_falls_back(self, tmp_path, monkeypatch, caplog):
        project_dir = tmp_path / "project"
        settings = project_dir / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text("{bad json", encoding="utf-8")

        adapter = MagicMock()
        adapter.get_instance_name.return_value = "main"
        adapter.agent_id_prefix.return_value = "fake"
        monkeypatch.delenv("QUAID_INSTANCE", raising=False)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project_dir))
        caplog.set_level("DEBUG")

        _bootstrap_instance_env(adapter)

        assert os.environ["QUAID_INSTANCE"] == "fake-main"
        assert "_bootstrap_instance_env: failed reading Claude settings.json" in caplog.text

    def test_bootstrap_instance_env_logs_get_instance_name_failure(self, monkeypatch, caplog):
        adapter = MagicMock()
        adapter.get_instance_name.side_effect = RuntimeError("name boom")
        monkeypatch.delenv("QUAID_INSTANCE", raising=False)
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        caplog.set_level("DEBUG")

        _bootstrap_instance_env(adapter)

        assert "QUAID_INSTANCE" not in os.environ
        assert "_bootstrap_instance_env: adapter.get_instance_name failed: name boom" in caplog.text


# ---------------------------------------------------------------------------
# OpenClawAdapter Tests
# ---------------------------------------------------------------------------

@pytest.mark.adapter_openclaw
class TestOpenClawAdapter:
    def test_quaid_home_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        adapter = OpenClawAdapter()
        assert adapter.quaid_home() == tmp_path

    def test_quaid_home_default(self, monkeypatch):
        monkeypatch.delenv("QUAID_HOME", raising=False)
        adapter = OpenClawAdapter()
        assert adapter.quaid_home() == Path.home() / ".quaid"

    def test_oc_workspace_raises_without_env(self, monkeypatch, tmp_path):
        monkeypatch.delenv("OPENCLAW_WORKSPACE", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        adapter = OpenClawAdapter()
        with pytest.raises(RuntimeError, match="QUAID_HOME|openclaw.json"):
            adapter.oc_workspace()

    def test_oc_workspace_reads_agent_list_workspace(self, tmp_path, monkeypatch):
        """Reads workspace from agents.list[default=True] in openclaw.json."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        workspace = tmp_path / "agent-workspace"
        workspace.mkdir()
        cfg_dir = tmp_path / ".openclaw"
        cfg_dir.mkdir()
        import json
        (cfg_dir / "openclaw.json").write_text(json.dumps({
            "agents": {"list": [{"id": "main", "default": True, "workspace": str(workspace)}]}
        }))
        adapter = OpenClawAdapter()
        assert adapter.oc_workspace() == workspace

    def test_oc_workspace_fallback_to_openclaw_json(self, tmp_path, monkeypatch):
        """When OPENCLAW_WORKSPACE unset, falls back to ~/.openclaw/openclaw.json."""
        monkeypatch.delenv("OPENCLAW_WORKSPACE", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        workspace = tmp_path / "my-workspace"
        workspace.mkdir()
        cfg_dir = tmp_path / ".openclaw"
        cfg_dir.mkdir()
        import json
        (cfg_dir / "openclaw.json").write_text(json.dumps({
            "agents": {"defaults": {"workspace": str(workspace)}}
        }))
        adapter = OpenClawAdapter()
        assert adapter.oc_workspace() == workspace

    def test_oc_workspace_rejects_config_workspace_outside_home_when_fail_hard(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OPENCLAW_WORKSPACE", raising=False)
        home = tmp_path / "home"
        outside = tmp_path / "outside-workspace"
        outside.mkdir(parents=True)
        cfg_dir = home / ".openclaw"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "openclaw.json").write_text(json.dumps({
            "agents": {"defaults": {"workspace": str(outside)}}
        }))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        adapter = OpenClawAdapter()

        with patch("adaptors.openclaw.adapter.is_fail_hard_enabled", return_value=True):
            with pytest.raises(PermissionError, match="workspace outside home"):
                adapter.oc_workspace()

    def test_oc_workspace_ignores_non_openclaw_config(self, tmp_path, monkeypatch):
        """Only ~/.openclaw/openclaw.json is used for workspace fallback."""
        monkeypatch.delenv("OPENCLAW_WORKSPACE", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        ws_new = tmp_path / "workspace-openclaw"
        ws_new.mkdir()

        cfg_dir = tmp_path / ".openclaw"
        cfg_dir.mkdir()

        import json
        (cfg_dir / "openclaw.json").write_text(json.dumps({
            "agents": {"defaults": {"workspace": str(ws_new)}}
        }))

        adapter = OpenClawAdapter()
        assert adapter.oc_workspace() == ws_new

    def test_filter_heartbeat(self):
        adapter = OpenClawAdapter()
        assert adapter.filter_system_messages("**HEARTBEAT_OK**") is True
        assert adapter.filter_system_messages("HEARTBEAT_OK foo") is True

    def test_filter_gateway_restart(self):
        adapter = OpenClawAdapter()
        assert adapter.filter_system_messages("GatewayRestart: reconnecting") is True

    def test_filter_system_message(self):
        adapter = OpenClawAdapter()
        assert adapter.filter_system_messages("System: shutting down") is True

    def test_filter_restart_kind(self):
        adapter = OpenClawAdapter()
        assert adapter.filter_system_messages('{"kind": "restart"}') is True

    def test_filter_normal_message(self):
        adapter = OpenClawAdapter()
        assert adapter.filter_system_messages("hello world") is False

    def test_adapter_config_defaults_include_transcript_mirror_prefixes(self):
        prefixes = OpenClawAdapter.ADAPTER_CONFIG.get("preserve_transcript_mirror_session_prefixes")
        assert isinstance(prefixes, list)
        assert "agent:main:matrix:channel:" in prefixes

    def test_installer_install_state_reports_already_installed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        cfg = tmp_path / "instances" / "openclaw-main"
        cfg.mkdir(parents=True)
        (cfg / "config.json").write_text("{}", encoding="utf-8")
        state = OpenClawAdapter.installer_install_state(str(tmp_path))
        assert state["status"] == "already_installed"

    def test_installer_install_state_accepts_openclaw_binary(self, tmp_path, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/openclaw" if name == "openclaw" else None)
        state = OpenClawAdapter.installer_install_state(str(tmp_path))
        assert state["status"] == "can_install"

    def test_parse_session_jsonl_handles_event_envelopes(self, tmp_path):
        session_file = tmp_path / "oc-session.jsonl"
        session_file.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {"type": "user_message", "message": "First user event"},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {"type": "agent_message", "message": "First assistant event"},
                        }
                    ),
                ]
            ),
            encoding="utf-8",
        )
        adapter = OpenClawAdapter()
        transcript = adapter.parse_session_jsonl(session_file)
        assert "User: First user event" in transcript
        assert "Assistant: First assistant event" in transcript
        assert adapter.filter_system_messages("What about HEARTBEAT mechanisms?") is False

    def test_parse_session_jsonl_filters_host_memory_policy_paths_structurally(self, tmp_path):
        session_file = tmp_path / "oc-memory-policy-path.jsonl"
        session_file.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {"type": "user_message", "message": "このメモを覚えて"},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "agent_message",
                                "message": (
                                    "保存しました: memory/2026-06-15-user-note.md "
                                    "Your appointment is tomorrow at 3pm."
                                ),
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "agent_message",
                                "message": "Durable memory is a product feature, not a transcript fact.",
                            },
                        }
                    ),
                ]
            ),
            encoding="utf-8",
        )

        adapter = OpenClawAdapter()
        transcript = adapter.parse_session_jsonl(session_file)

        assert "memory/2026-06-15-user-note.md" not in transcript
        assert "Your appointment is tomorrow at 3pm." in transcript
        assert "Durable memory is a product feature" in transcript

    def test_host_memory_policy_reply_does_not_use_english_phrase_gate(self):
        assert not OpenClawAdapter._is_host_memory_policy_reply(
            "assistant",
            "Durable memory won't store that unless you want me to.",
        )

    def test_parse_session_jsonl_preserves_openclaw_row_timestamps(self, tmp_path):
        session_file = tmp_path / "oc-session-timestamps.jsonl"
        session_file.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "timestamp": "2026-06-11T15:09:12.914Z",
                            "type": "message",
                            "message": {
                                "role": "user",
                                "content": "I started using a 14mm Sailor Pro Gear nib this week.",
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "timestamp": "2026-06-11T15:09:17.625Z",
                            "type": "message",
                            "message": {
                                "role": "assistant",
                                "content": "Noted.",
                            },
                        }
                    ),
                ]
            ),
            encoding="utf-8",
        )

        adapter = OpenClawAdapter()
        transcript = adapter.parse_session_jsonl(session_file)

        assert "[2026-06-11T15:09:12.914Z] User: I started using a 14mm Sailor" in transcript
        assert "[2026-06-11T15:09:17.625Z] Assistant: Noted." in transcript

    def test_parse_session_jsonl_uses_payload_timestamp_after_event_msg_fallthrough(self, tmp_path):
        session_file = tmp_path / "oc-session-payload-timestamp.jsonl"
        session_file.write_text(
            json.dumps(
                {
                    "timestamp": "2026-06-11T15:09:12.914Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "custom_transcript_row",
                        "timestamp": "2026-06-11T15:10:17.625Z",
                        "role": "assistant",
                        "content": "The payload timestamp should win.",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        adapter = OpenClawAdapter()
        transcript = adapter.parse_session_jsonl(session_file)

        assert "[2026-06-11T15:10:17.625Z] Assistant: The payload timestamp should win." in transcript
        assert "2026-06-11T15:09:12.914Z" not in transcript

    def test_parse_session_jsonl_raises_on_malformed_json_under_failhard(self, monkeypatch, tmp_path):
        session_file = tmp_path / "oc-session-corrupt.jsonl"
        session_file.write_text('{"role":"user","content":"ok"}\n{bad-json\n', encoding="utf-8")
        monkeypatch.setattr("adaptors.openclaw.adapter.is_fail_hard_enabled", lambda: True)

        with pytest.raises(RuntimeError, match="malformed JSON"):
            OpenClawAdapter().parse_session_jsonl(session_file)

    def test_parse_session_jsonl_accepts_role_rows_with_message_field(self, tmp_path):
        session_file = tmp_path / "oc-session-message-field.jsonl"
        session_file.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "role": "user",
                            "message": "My Friday pumpkin seed ritual uses smoked paprika.",
                        }
                    ),
                    json.dumps(
                        {
                            "role": "assistant",
                            "message": "Noted.",
                        }
                    ),
                ]
            ),
            encoding="utf-8",
        )

        adapter = OpenClawAdapter()
        transcript = adapter.parse_session_jsonl(session_file)

        assert "User: My Friday pumpkin seed ritual uses smoked paprika." in transcript
        assert "Assistant: Noted." in transcript

    def test_parse_session_jsonl_marks_subagent_turns_and_strips_oc_wrapper(self, tmp_path):
        session_file = tmp_path / "oc-subagent.jsonl"
        session_file.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "message",
                            "message": {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": (
                                            "[Thu 2026-04-09 17:19 UTC] [Subagent Context] "
                                            "You are running as a subagent (depth 1/1). Results auto-announce.\n\n"
                                            "[Subagent Task]: my uncle owns a vineyard in Mendoza that produces Malbec."
                                        ),
                                    }
                                ],
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "message",
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "text", "text": "Noted: your uncle owns a vineyard in Mendoza."}],
                            },
                        }
                    ),
                ]
            ),
            encoding="utf-8",
        )
        adapter = OpenClawAdapter()
        transcript = adapter.parse_session_jsonl(session_file)
        assert "Subagent/User: my uncle owns a vineyard in Mendoza that produces Malbec." in transcript
        assert "Assistant: Noted: your uncle owns a vineyard in Mendoza." in transcript
        assert "[Subagent Context]" not in transcript
        assert "You are running as a subagent" not in transcript

    def test_parse_session_jsonl_does_not_leak_subagent_source_type(self, tmp_path):
        session_file = tmp_path / "oc-subagent-leak.jsonl"
        session_file.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "user_message",
                                "message": (
                                    "[Thu 2026-04-09 17:19 UTC] [Subagent Context] "
                                    "You are running as a subagent.\n\n"
                                    "[Subagent Task]: My sister is Diana."
                                ),
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "agent_message",
                                "message": "Subagent task acknowledged.",
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "user_message",
                                "message": "My normal follow-up is that I prefer rooibos.",
                            },
                        }
                    ),
                ]
            ),
            encoding="utf-8",
        )

        adapter = OpenClawAdapter()
        transcript = adapter.parse_session_jsonl(session_file)

        assert "Subagent/User: My sister is Diana." in transcript
        assert "Assistant: Subagent task acknowledged." in transcript
        assert "User: My normal follow-up is that I prefer rooibos." in transcript
        assert "Subagent/Assistant: Subagent task acknowledged." not in transcript
        assert "Subagent/User: My normal follow-up is that I prefer rooibos." not in transcript



    def test_parse_session_jsonl_strips_openclaw_hook_memory_context_block(self, tmp_path):
        session_file = tmp_path / "oc-hook-memory-context.jsonl"
        session_file.write_text(
            json.dumps(
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "• Running UserPromptSubmit hook: Quaid recalling memory\n\n"
                                    "UserPromptSubmit hook (completed)\n"
                                    "  hook context: [Quaid Memory Context]\n"
                                    "  1. [fact] Henley lock keeper loves canal chili (relevance: 0.91)\n"
                                    "  2. [fact] The canal museum sells chili jam (relevance: 0.88)\n\n"
                                    "The watermill ledger mentions a 7.4-foot sluice gate."
                                ),
                            }
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )
        adapter = OpenClawAdapter()
        transcript = adapter.parse_session_jsonl(session_file)
        assert "Running UserPromptSubmit hook" not in transcript
        assert "hook context:" not in transcript
        assert "Henley lock keeper loves canal chili" not in transcript
        assert "canal museum sells chili jam" not in transcript
        assert "The watermill ledger mentions a 7.4-foot sluice gate." in transcript

    def test_parse_session_jsonl_strips_openclaw_internal_context_block(self, tmp_path):
        session_file = tmp_path / "oc-internal-context.jsonl"
        session_file.write_text(
            json.dumps(
                {
                    "type": "message",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>\n"
                                    "session_key=agent:main:tui-123\n"
                                    "source=subagent\n"
                                    "action: do not show this raw template\n"
                                    "<<<END_OPENCLAW_INTERNAL_CONTEXT>>>\n\n"
                                    "My kiln controller is named Oriole."
                                ),
                            }
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )
        adapter = OpenClawAdapter()
        transcript = adapter.parse_session_jsonl(session_file)
        assert "User: My kiln controller is named Oriole." in transcript
        assert "BEGIN_OPENCLAW_INTERNAL_CONTEXT" not in transcript
        assert "session_key=agent:main" not in transcript
        assert "source=subagent" not in transcript

    def test_parse_session_jsonl_strips_session_start_boilerplate_lines(self, tmp_path):
        session_file = tmp_path / "oc-startup-boilerplate.jsonl"
        session_file.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "user_message",
                                "message": (
                                    "A new session was started via /new or /reset. "
                                    "If runtime-provided startup context is included for this first turn, use it before responding to the user. "
                                    "Then greet the user in your configured persona, if one is provided. "
                                    "Be yourself - use your defined voice, mannerisms, and mood. "
                                    "Keep it to 1-3 sentences and ask what they want to do. "
                                    "If the runtime model differs from default_model in the system prompt, mention the default model. "
                                    "Do not mention internal steps, files, tools, or reasoning.\n"
                                    "Current time: Thursday, April 16th, 2026 - 9:46 AM (UTC) / 2026-04-16 09:46 UTC\n\n"
                                    "My morning run route takes me past the old watermill on Henley Road."
                                ),
                            },
                        }
                    )
                ]
            ),
            encoding="utf-8",
        )
        adapter = OpenClawAdapter()
        transcript = adapter.parse_session_jsonl(session_file)
        assert "A new session was started via /new or /reset" not in transcript
        assert "Current time:" not in transcript
        assert "User: My morning run route takes me past the old watermill on Henley Road." in transcript

    def test_parse_session_jsonl_strips_session_start_variant_line(self, tmp_path):
        session_file = tmp_path / "oc-startup-boilerplate-variant.jsonl"
        session_file.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "user_message",
                                "message": (
                                    "A new session was started via /new or /reset. Execute your Session Startup sequence now.\n"
                                    "My neighbour won a regional chili cook-off last weekend."
                                ),
                            },
                        }
                    )
                ]
            ),
            encoding="utf-8",
        )
        adapter = OpenClawAdapter()
        transcript = adapter.parse_session_jsonl(session_file)
        assert "A new session was started via /new or /reset" not in transcript
        assert "User: My neighbour won a regional chili cook-off last weekend." in transcript

    def test_parse_session_jsonl_strips_queued_session_wrapper_lines(self, tmp_path):
        session_file = tmp_path / "oc-queued-wrapper.jsonl"
        session_file.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "user_message",
                                "message": (
                                    "[Queued messages while agent was busy]\n"
                                    "---\n"
                                    "Queued #1 (from Solomon Steadman)\n"
                                    "A new session was started via /new or /reset.\n"
                                    "If runtime-provided startup context is included for this first turn, use it before responding to the user.\n"
                                    "My neighbour Marisol won the Willow Basin chili cook-off."
                                ),
                            },
                        }
                    )
                ]
            ),
            encoding="utf-8",
        )
        adapter = OpenClawAdapter()
        transcript = adapter.parse_session_jsonl(session_file)
        assert "Queued #1" not in transcript
        assert "[Queued messages while agent was busy]" not in transcript
        assert "A new session was started via /new or /reset" not in transcript
        assert "User: My neighbour Marisol won the Willow Basin chili cook-off." in transcript

    def test_parse_session_jsonl_filters_quaid_meta_banner_message(self, tmp_path):
        session_file = tmp_path / "oc-quaid-meta-banner.jsonl"
        session_file.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "agent_message",
                                "message": (
                                    "**[Quaid]** 💾 **Compaction extraction summary:**\n\n"
                                    "- 3 facts stored\n"
                                    "- 0 skipped"
                                ),
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "user_message",
                                "message": "Tell me about the watermill ledger.",
                            },
                        }
                    ),
                ]
            ),
            encoding="utf-8",
        )
        adapter = OpenClawAdapter()
        transcript = adapter.parse_session_jsonl(session_file)
        assert "Compaction extraction summary" not in transcript
        assert "facts stored" not in transcript
        assert "User: Tell me about the watermill ledger." in transcript

    def test_parse_session_jsonl_strips_current_time_with_single_digit_hour(self, tmp_path):
        session_file = tmp_path / "oc-startup-time-single-hour.jsonl"
        session_file.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "user_message",
                                "message": (
                                    "Current time: Thursday, April 16th, 2026 - 9:46 AM (UTC) / 2026-04-16 9:46 UTC\n"
                                    "I planted a Japanese maple last autumn."
                                ),
                            },
                        }
                    )
                ]
            ),
            encoding="utf-8",
        )
        adapter = OpenClawAdapter()
        transcript = adapter.parse_session_jsonl(session_file)
        assert "Current time:" not in transcript
        assert "User: I planted a Japanese maple last autumn." in transcript

    def test_sanitize_transcript_text_keeps_single_startup_phrase_in_user_content(self):
        adapter = OpenClawAdapter()
        text = (
            "Question: If runtime-provided startup context is included in the conversation, "
            "the model should consider it before responding to the user."
        )
        assert adapter.sanitize_transcript_text(text) == text

    def test_sanitize_transcript_text_strips_gateway_timestamp_not_user_brackets(self):
        adapter = OpenClawAdapter()
        text = (
            "[Thu 2026-04-09 17:19 UTC] Gateway timestamp should go.\n"
            "[Action 2026-01-15 10:00] Keep this dated bracketed note."
        )

        sanitized = adapter.sanitize_transcript_text(text)

        assert "Gateway timestamp should go." in sanitized
        assert "Thu 2026-04-09 17:19 UTC" not in sanitized
        assert "[Action 2026-01-15 10:00] Keep this dated bracketed note." in sanitized

    def test_sanitize_transcript_text_collapses_newlines_after_startup_strip(self):
        adapter = OpenClawAdapter()
        text = (
            "A new session was started via /new or /reset. Execute your Session Startup sequence now.\n\n\n"
            "My neighbour won a regional chili cook-off last weekend.\n\n\n"
        )
        sanitized = adapter.sanitize_transcript_text(text)
        assert "A new session was started via /new or /reset" not in sanitized
        assert "\n\n\n" not in sanitized
        assert sanitized == "My neighbour won a regional chili cook-off last weekend."

    def test_sanitize_transcript_text_strips_untrusted_daily_memory_blocks(self):
        adapter = OpenClawAdapter()
        text = (
            "[Startup context loaded by runtime]\n"
            "[Untrusted daily memory: memory/2026-04-27-friday-ritual.md]\n"
            "BEGIN_QUOTED_NOTES\n"
            "```text\n"
            "# Session: 2026-04-27 19:23:01 UTC\n"
            "- **Session Key**: agent:main:matrix:direct:@quaid-test-bot:localhost\n"
            "user: <injected_memories>\n"
            "- fact | stale recalled memory\n"
            "</injected_memories>\n"
            "```\n"
            "END_QUOTED_NOTES\n\n"
            "My Friday ritual uses marker marigold-anvil-5816.\n"
        )

        sanitized = adapter.sanitize_transcript_text(text)

        assert "BEGIN_QUOTED_NOTES" not in sanitized
        assert "Bootstrap files like" not in sanitized
        assert "<injected_memories>" not in sanitized
        assert "stale recalled memory" not in sanitized
        assert sanitized.endswith("My Friday ritual uses marker marigold-anvil-5816.")

    def test_get_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env-key")
        adapter = OpenClawAdapter()
        assert adapter.get_api_key("ANTHROPIC_API_KEY") == "sk-env-key"

    def test_get_api_key_from_env_file(self, tmp_path, monkeypatch):
        import json as _json
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        cfg_dir = tmp_path / ".openclaw"
        cfg_dir.mkdir()
        (cfg_dir / "openclaw.json").write_text(_json.dumps({
            "agents": {"defaults": {"workspace": str(tmp_path)}}
        }))
        monkeypatch.delenv("TEST_KEY", raising=False)
        (tmp_path / ".env").write_text("TEST_KEY=sk-from-ws-env\n")
        adapter = OpenClawAdapter()
        with patch("adaptors.openclaw.adapter.is_fail_hard_enabled", return_value=False):
            assert adapter.get_api_key("TEST_KEY") == "sk-from-ws-env"

    def test_get_api_key_from_env_file_blocked_when_failhard_enabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENCLAW_WORKSPACE", str(tmp_path))
        monkeypatch.delenv("TEST_KEY", raising=False)
        (tmp_path / ".env").write_text("TEST_KEY=sk-from-ws-env\n")
        adapter = OpenClawAdapter()
        with patch("adaptors.openclaw.adapter.is_fail_hard_enabled", return_value=True):
            assert adapter.get_api_key("TEST_KEY") is None

    def test_get_last_channel_no_sessions_file(self, monkeypatch):
        monkeypatch.setattr(OpenClawAdapter, "_find_sessions_json",
                           lambda self: None)
        adapter = OpenClawAdapter()
        assert adapter.get_last_channel() is None

    def test_get_last_channel_valid(self, tmp_path, monkeypatch):
        import json
        sessions_file = tmp_path / "sessions.json"
        sessions_file.write_text(json.dumps({
            "agent:main:main": {
                "lastChannel": "telegram",
                "lastTo": "12345",
                "lastAccountId": "default",
            }
        }))
        monkeypatch.setattr(OpenClawAdapter, "_find_sessions_json",
                           lambda self: sessions_file)
        adapter = OpenClawAdapter()
        info = adapter.get_last_channel()
        assert info is not None
        assert info.channel == "telegram"
        assert info.target == "12345"

    def test_get_last_channel_falls_back_to_recent_routable_session(self, tmp_path, monkeypatch):
        import json
        sessions_file = tmp_path / "sessions.json"
        sessions_file.write_text(json.dumps({
            "agent:main:main": {
                "lastTo": "heartbeat",
                "updatedAt": 100,
            },
            "agent:main:telegram:direct:1000000000": {
                "lastChannel": "telegram",
                "lastTo": "telegram:1000000000",
                "lastAccountId": "default",
                "updatedAt": 200,
            },
        }))
        monkeypatch.setattr(OpenClawAdapter, "_find_sessions_json", lambda self: sessions_file)
        adapter = OpenClawAdapter()
        info = adapter.get_last_channel()
        assert info is not None
        assert info.channel == "telegram"
        assert info.target == "telegram:1000000000"
        assert info.session_key == "agent:main:telegram:direct:1000000000"

    def test_get_sessions_dir(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / ".openclaw" / "agents" / "main" / "sessions"
        sessions_dir.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
        adapter = OpenClawAdapter()
        assert adapter.get_sessions_dir() == sessions_dir

    def test_get_instance_name_accepts_bare_openclaw_label(self, monkeypatch):
        monkeypatch.setenv("QUAID_INSTANCE", "coding")
        adapter = OpenClawAdapter()
        assert adapter.get_instance_name() == "coding"

    @pytest.mark.parametrize(
        ("instance_id", "expected_label"),
        [
            ("openclaw-研究", "研究"),
            ("openclaw-Élan", "élan"),
            ("openclaw-فريق1", "فريق1"),
        ],
    )
    def test_get_instance_name_accepts_unicode_openclaw_label(
        self,
        monkeypatch,
        instance_id,
        expected_label,
    ):
        monkeypatch.setenv("QUAID_INSTANCE", instance_id)
        adapter = OpenClawAdapter()
        assert adapter.get_instance_name() == expected_label

    def test_get_instance_name_rejects_unsafe_label_when_fail_hard(self, monkeypatch):
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-../evil")
        adapter = OpenClawAdapter()
        with patch("adaptors.openclaw.adapter.is_fail_hard_enabled", return_value=True):
            with pytest.raises(ValueError, match="Unsafe OpenClaw agent label"):
                adapter.get_instance_name()

    def test_get_sessions_dir_non_main_without_matching_session_key_is_none(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / ".openclaw" / "agents" / "main" / "sessions"
        sessions_dir.mkdir(parents=True)
        (sessions_dir / "sessions.json").write_text(json.dumps({
            "agent:main:matrix:channel:!room": {
                "sessionId": "main-session",
                "sessionFile": str(sessions_dir / "main-session.jsonl"),
            }
        }), encoding="utf-8")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-m5test")

        adapter = OpenClawAdapter()

        assert adapter.get_sessions_dir() is None

    def test_owns_session_path_rejects_main_session_for_non_main_instance(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / ".openclaw" / "agents" / "main" / "sessions"
        sessions_dir.mkdir(parents=True)
        main_transcript = sessions_dir / "main-session.jsonl"
        main_transcript.write_text('{"role":"user","content":"tamarind-lighthouse-3317"}\n', encoding="utf-8")
        (sessions_dir / "sessions.json").write_text(json.dumps({
            "agent:main:matrix:channel:!room": {
                "sessionId": "main-session",
                "sessionFile": str(main_transcript),
            }
        }), encoding="utf-8")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-m5test")

        adapter = OpenClawAdapter()

        assert adapter.owns_session_path(main_transcript, session_id="main-session") is False

    def test_owns_session_path_accepts_matching_non_main_session_key(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / ".openclaw" / "agents" / "main" / "sessions"
        sessions_dir.mkdir(parents=True)
        worker_transcript = sessions_dir / "worker-session.jsonl"
        worker_transcript.write_text('{"role":"user","content":"tamarind-lighthouse-3317"}\n', encoding="utf-8")
        (sessions_dir / "sessions.json").write_text(json.dumps({
            "agent:m5test:matrix:channel:!room": {
                "sessionId": "worker-session",
                "sessionFile": str(worker_transcript),
            }
        }), encoding="utf-8")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-m5test")

        adapter = OpenClawAdapter()

        assert adapter.get_sessions_dir() == sessions_dir
        assert adapter.owns_session_path(worker_transcript, session_id="worker-session") is True

    def test_owns_session_path_rejects_index_path_outside_sessions_dir(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / ".openclaw" / "agents" / "main" / "sessions"
        sessions_dir.mkdir(parents=True)
        outside = tmp_path / "outside.jsonl"
        outside.write_text('{"role":"user","content":"do not ingest"}\n', encoding="utf-8")
        (sessions_dir / "sessions.json").write_text(json.dumps({
            "agent:main:matrix:channel:!room": {
                "sessionId": "session-1",
                "sessionFile": str(outside),
            }
        }), encoding="utf-8")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")

        adapter = OpenClawAdapter()

        assert adapter.owns_session_path(outside, session_id="session-1") is False

    def test_session_id_from_path_rejects_unsafe_filename(self):
        assert OpenClawAdapter._session_id_from_path(Path("safe-session_1.jsonl")) == "safe-session_1"
        assert OpenClawAdapter._session_id_from_path(Path("bad.session?.jsonl")) == ""

    def test_get_bootstrap_markdown_globs(self, tmp_path, monkeypatch):
        import json
        config_path = tmp_path / ".openclaw" / "openclaw.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps({
            "hooks": {
                "internal": {
                    "entries": {
                        "bootstrap-extra-files": {
                            "enabled": True,
                            "paths": ["projects/*/TOOLS.md", "projects/*/AGENTS.md"],
                        }
                    }
                }
            }
        }))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        adapter = OpenClawAdapter()
        assert adapter.get_bootstrap_markdown_globs() == [
            "projects/*/TOOLS.md",
            "projects/*/AGENTS.md",
        ]

    def test_get_bootstrap_markdown_globs_filters_unsafe_paths(self, tmp_path, monkeypatch):
        config_path = tmp_path / ".openclaw" / "openclaw.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps({
            "hooks": {
                "internal": {
                    "entries": {
                        "bootstrap-extra-files": {
                            "enabled": True,
                            "paths": [
                                "projects/*/TOOLS.md",
                                "../secrets/*.md",
                                "/etc/*.md",
                                7,
                            ],
                        }
                    }
                }
            }
        }))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        adapter = OpenClawAdapter()
        assert adapter.get_bootstrap_markdown_globs() == ["projects/*/TOOLS.md"]

    def test_resolve_message_cli_accepts_home_local_openclaw(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        binary = home / ".local" / "bin" / "openclaw"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.setenv("QUAID_MESSAGE_CLI", str(binary))

        adapter = OpenClawAdapter()

        assert adapter._resolve_message_cli() == str(binary.resolve())

    def test_resolve_message_cli_accepts_allowed_symlink_target(self, tmp_path, monkeypatch):
        prefix = tmp_path / "prefix"
        requested = prefix / "bin" / "openclaw"
        target = prefix / "lib" / "node_modules" / "openclaw" / "bin" / "cli.js"
        requested.parent.mkdir(parents=True)
        target.parent.mkdir(parents=True)
        target.write_text("#!/usr/bin/env node\n", encoding="utf-8")
        requested.symlink_to(target)
        monkeypatch.setenv("QUAID_MESSAGE_CLI", str(requested))
        adapter = OpenClawAdapter()
        monkeypatch.setattr(
            adapter,
            "_message_cli_allowed_dirs",
            lambda: (prefix / "bin", prefix / "lib"),
        )

        assert adapter._resolve_message_cli() == str(target.resolve())

    def test_resolve_message_cli_rejects_unsafe_env_path(self, tmp_path, monkeypatch):
        binary = tmp_path / "openclaw"
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("QUAID_MESSAGE_CLI", str(binary))
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        adapter = OpenClawAdapter()

        with patch("adaptors.openclaw.adapter.is_fail_hard_enabled", return_value=False):
            assert adapter._resolve_message_cli() is None
        with patch("adaptors.openclaw.adapter.is_fail_hard_enabled", return_value=True):
            with pytest.raises(PermissionError, match="Rejected unsafe QUAID_MESSAGE_CLI"):
                adapter._resolve_message_cli()

    def test_notify_rejects_option_like_route_values(self, monkeypatch):
        adapter = OpenClawAdapter()
        mock_info = ChannelInfo(
            channel="matrix", target="--help", account_id="default",
            session_key="agent:main:main"
        )
        monkeypatch.setattr(adapter, "get_last_channel", lambda s="": mock_info)
        monkeypatch.setattr(adapter, "_resolve_message_cli", lambda: "openclaw")

        with patch("adaptors.openclaw.adapter.subprocess.run") as mock_run:
            with patch("adaptors.openclaw.adapter.is_fail_hard_enabled", return_value=False):
                assert adapter.notify("test") is False
        mock_run.assert_not_called()

    def test_notify_delegates_to_openclaw(self, monkeypatch):
        """Verify notify calls OpenClaw message CLI."""
        import json
        adapter = OpenClawAdapter()

        # Mock get_last_channel to return a valid channel
        mock_info = ChannelInfo(
            channel="telegram", target="123", account_id="default",
            session_key="agent:main:main"
        )
        monkeypatch.setattr(adapter, "get_last_channel", lambda s="": mock_info)

        # Mock subprocess.run
        mock_result = MagicMock()
        mock_result.returncode = 0
        monkeypatch.setattr(adapter, "_resolve_message_cli", lambda: "openclaw")
        with patch("adaptors.openclaw.adapter.subprocess.run", return_value=mock_result) as mock_run:
            result = adapter.notify("test message")
            assert result is True
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            assert cmd[0] == "openclaw"
            assert "message" in cmd
            assert "send" in cmd
            assert "test message" in cmd

    def test_installer_provider_surface(self, monkeypatch, tmp_path):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        adapter = OpenClawAdapter()
        assert adapter.installer_supported_providers() == ["anthropic", "openai"]
        assert adapter.installer_default_models("anthropic") == {
            "deep": "claude-sonnet-4-5",
            "fast": "claude-haiku-4-5",
        }
        assert adapter.installer_default_models("openai") == {
            "deep": "gpt-5.4",
            "fast": "gpt-5.4-mini",
        }
        assert adapter.get_deep_provider_default() == "anthropic"
        assert adapter.get_fast_provider_default() == "anthropic"

    def test_installer_provider_surface_detects_gateway_provider(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        cfg_dir = home / ".openclaw"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "openclaw.json").write_text(
            json.dumps({"agents": {"defaults": {"modelPrimary": "openai-codex/gpt-5.4"}}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(Path, "home", lambda: home)
        adapter = OpenClawAdapter()
        assert adapter.get_deep_provider_default() == "openai"
        assert adapter.get_fast_provider_default() == "openai"
        assert adapter.get_deep_model_default("default") == "gpt-5.4"
        assert adapter.get_fast_model_default("default") == "gpt-5.4-mini"

    def test_installer_provider_detection_raises_bad_config_when_fail_hard(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        cfg_dir = home / ".openclaw"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "openclaw.json").write_text("{bad json", encoding="utf-8")
        monkeypatch.setattr(Path, "home", lambda: home)
        adapter = OpenClawAdapter()

        with patch("adaptors.openclaw.adapter.is_fail_hard_enabled", return_value=True):
            with pytest.raises(json.JSONDecodeError):
                adapter.get_deep_provider_default()

    def test_openclaw_notify_reraises_transport_failure_when_fail_hard(self, monkeypatch):
        adapter = OpenClawAdapter()
        monkeypatch.setattr(adapter, "get_last_channel", lambda *_args, **_kwargs: SimpleNamespace(
            channel="matrix",
            target="!room:example",
            account_id="default",
        ))
        monkeypatch.setattr(adapter, "_resolve_message_cli", lambda: "openclaw")
        monkeypatch.setattr(
            "adaptors.openclaw.adapter.subprocess.run",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("transport failed")),
        )

        with patch("adaptors.openclaw.adapter.is_fail_hard_enabled", return_value=True):
            with pytest.raises(OSError, match="transport failed"):
                adapter.notify("hello")

    def test_get_llm_provider_detects_agents_list_codex_oauth(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        cfg_dir = home / ".openclaw"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "openclaw.json").write_text(
            json.dumps({
                "agents": {
                    "defaults": {"model": {"primary": "anthropic/claude-haiku-4-5"}},
                    "list": [{"id": "main", "default": True, "model": {"primary": "openai-codex/gpt-5.4"}}],
                }
            }),
            encoding="utf-8",
        )
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.setenv("QUAID_HOME", str(tmp_path / ".quaid"))
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        adapter = OpenClawAdapter()
        adapter.store_shared_auth_token("codex_oauth", "tok.a.b")
        cfg = SimpleNamespace(models=SimpleNamespace(
            llm_provider="default",
            deep_reasoning="gpt-5.4",
            fast_reasoning="gpt-5.4-mini",
            fast_reasoning_effort="none",
            deep_reasoning_effort="high",
            fast_reasoning_provider="default",
            deep_reasoning_provider="default",
            base_url="",
        ))

        with patch("config.get_config", return_value=cfg):
            llm = adapter.get_llm_provider(model_tier="deep")

        assert isinstance(llm, OpenAICodexOAuthLLMProvider)
        assert llm._deep_model == "gpt-5.4"
        assert llm._fast_model == "gpt-5.4-mini"

    def test_get_llm_provider_falls_back_to_codex_oauth_for_inferred_anthropic(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        cfg_dir = home / ".openclaw"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "openclaw.json").write_text(
            json.dumps({"agents": {"defaults": {"model": {"primary": "anthropic/claude-haiku-4-5"}}}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.setenv("QUAID_HOME", str(tmp_path / ".quaid"))
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        adapter = OpenClawAdapter()
        monkeypatch.setattr(adapter, "_resolve_anthropic_credential", lambda: None)
        monkeypatch.setattr(adapter, "_detect_gateway_primary_provider", lambda: "")
        adapter.store_shared_auth_token("codex_oauth", "tok.a.b")
        cfg = SimpleNamespace(models=SimpleNamespace(
            llm_provider="default",
            deep_reasoning="gpt-5.4",
            fast_reasoning="gpt-5.4-mini",
            fast_reasoning_effort="none",
            deep_reasoning_effort="high",
            fast_reasoning_provider="default",
            deep_reasoning_provider="default",
            base_url="",
        ))

        with patch("config.get_config", return_value=cfg):
            llm = adapter.get_llm_provider(model_tier="deep")

        assert isinstance(llm, OpenAICodexOAuthLLMProvider)

    def test_get_llm_provider_keeps_explicit_anthropic_failhard(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path / ".quaid"))
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        adapter = OpenClawAdapter()
        monkeypatch.setattr(adapter, "_resolve_anthropic_credential", lambda: None)
        monkeypatch.setattr(adapter, "_detect_gateway_primary_provider", lambda: "")
        adapter.store_shared_auth_token("codex_oauth", "tok.a.b")
        cfg = SimpleNamespace(models=SimpleNamespace(
            llm_provider="anthropic",
            deep_reasoning="claude-sonnet-4-5",
            fast_reasoning="claude-haiku-4-5",
            fast_reasoning_effort="none",
            deep_reasoning_effort="high",
            fast_reasoning_provider="default",
            deep_reasoning_provider="default",
            base_url="",
        ))

        with patch("config.get_config", return_value=cfg):
            with pytest.raises(RuntimeError, match="no OpenClaw Anthropic token"):
                adapter.get_llm_provider(model_tier="deep")

    def test_get_llm_provider_keeps_explicit_anthropic_even_when_codex_oauth_exists(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path / ".quaid"))
        monkeypatch.setenv("OPENAI_API_KEY", "tok.a.b")
        monkeypatch.delenv("OPENAI_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        adapter = OpenClawAdapter()
        monkeypatch.setattr(adapter, "_resolve_anthropic_credential", lambda: None)
        monkeypatch.setattr(adapter, "_detect_gateway_primary_provider", lambda: "")
        cfg = SimpleNamespace(models=SimpleNamespace(
            llm_provider="anthropic",
            deep_reasoning="claude-sonnet-4-5",
            fast_reasoning="claude-haiku-4-5",
            fast_reasoning_effort="none",
            deep_reasoning_effort="high",
            fast_reasoning_provider="default",
            deep_reasoning_provider="default",
            base_url="",
        ))

        with patch("config.get_config", return_value=cfg):
            with pytest.raises(RuntimeError, match="no OpenClaw Anthropic token"):
                adapter.get_llm_provider(model_tier="deep")

    def test_get_llm_provider_overrides_global_anthropic_when_oc_detects_codex(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        cfg_dir = home / ".openclaw"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "openclaw.json").write_text(
            json.dumps({
                "agents": {
                    "list": [{"id": "main", "default": True, "model": {"primary": "openai-codex/gpt-5.4"}}],
                }
            }),
            encoding="utf-8",
        )
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.setenv("QUAID_HOME", str(tmp_path / ".quaid"))
        monkeypatch.setenv("OPENAI_API_KEY", "tok.a.b")
        monkeypatch.delenv("OPENAI_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        adapter = OpenClawAdapter()
        monkeypatch.setattr(adapter, "_resolve_anthropic_credential", lambda: None)
        notices = []
        monkeypatch.setattr(adapter, "notify", lambda message, **_kwargs: notices.append(message) or True)
        cfg = SimpleNamespace(models=SimpleNamespace(
            llm_provider="anthropic",
            deep_reasoning="gpt-5.4",
            fast_reasoning="gpt-5.4-mini",
            fast_reasoning_effort="none",
            deep_reasoning_effort="high",
            fast_reasoning_provider="default",
            deep_reasoning_provider="default",
            base_url="",
        ))

        with patch("config.get_config", return_value=cfg):
            llm = adapter.get_llm_provider(model_tier="deep")

        assert isinstance(llm, OpenAICodexOAuthLLMProvider)
        assert llm._deep_model == "gpt-5.4"
        assert llm._fast_model == "gpt-5.4-mini"
        assert notices
        assert "OpenClaw gateway is routed to OpenAI/Codex" in notices[0]
        assert "'anthropic'" in notices[0]

    def test_get_llm_provider_keeps_openai_models_when_overriding_global_anthropic(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        cfg_dir = home / ".openclaw"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "openclaw.json").write_text(
            json.dumps({
                "agents": {
                    "list": [{"id": "main", "default": True, "model": {"primary": "openai-codex/gpt-5.4"}}],
                }
            }),
            encoding="utf-8",
        )
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.setenv("QUAID_HOME", str(tmp_path / ".quaid"))
        monkeypatch.setenv("OPENAI_API_KEY", "tok.a.b")
        monkeypatch.delenv("OPENAI_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        adapter = OpenClawAdapter()
        monkeypatch.setattr(adapter, "_resolve_anthropic_credential", lambda: None)
        notices = []
        monkeypatch.setattr(adapter, "notify", lambda message, **_kwargs: notices.append(message) or True)
        cfg = SimpleNamespace(models=SimpleNamespace(
            llm_provider="anthropic",
            deep_reasoning="gpt-5.4",
            fast_reasoning="gpt-5.4-mini",
            fast_reasoning_effort="none",
            deep_reasoning_effort="high",
            fast_reasoning_provider="default",
            deep_reasoning_provider="default",
            base_url="",
        ))

        with patch("config.get_config", return_value=cfg):
            llm = adapter.get_llm_provider(model_tier="deep")

        assert isinstance(llm, OpenAICodexOAuthLLMProvider)
        assert llm._deep_model == "gpt-5.4"
        assert llm._fast_model == "gpt-5.4-mini"

    def test_get_llm_provider_reports_missing_codex_when_oc_detects_codex_without_token(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        cfg_dir = home / ".openclaw"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "openclaw.json").write_text(
            json.dumps({
                "agents": {
                    "list": [{"id": "main", "default": True, "model": {"primary": "openai-codex/gpt-5.4"}}],
                }
            }),
            encoding="utf-8",
        )
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.setenv("QUAID_HOME", str(tmp_path / ".quaid"))
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        adapter = OpenClawAdapter()
        monkeypatch.setattr(adapter, "_resolve_anthropic_credential", lambda: None)
        notices = []
        monkeypatch.setattr(adapter, "notify", lambda message, **_kwargs: notices.append(message) or True)
        cfg = SimpleNamespace(models=SimpleNamespace(
            llm_provider="anthropic",
            deep_reasoning="gpt-5.4",
            fast_reasoning="gpt-5.4-mini",
            fast_reasoning_effort="none",
            deep_reasoning_effort="high",
            fast_reasoning_provider="default",
            deep_reasoning_provider="default",
            base_url="",
        ))

        with patch("config.get_config", return_value=cfg):
            with pytest.raises(RuntimeError, match="no OpenClaw OpenAI OAuth token"):
                adapter.get_llm_provider(model_tier="deep")
        assert notices == []

    def test_get_llm_provider_preserves_tier_anthropic_override_when_oc_detects_codex(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        cfg_dir = home / ".openclaw"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "openclaw.json").write_text(
            json.dumps({
                "agents": {
                    "list": [{"id": "main", "default": True, "model": {"primary": "openai-codex/gpt-5.4"}}],
                }
            }),
            encoding="utf-8",
        )
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.setenv("QUAID_HOME", str(tmp_path / ".quaid"))
        monkeypatch.setenv("OPENAI_API_KEY", "tok.a.b")
        monkeypatch.delenv("OPENAI_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        adapter = OpenClawAdapter()
        monkeypatch.setattr(adapter, "_resolve_anthropic_credential", lambda: None)
        cfg = SimpleNamespace(models=SimpleNamespace(
            llm_provider="default",
            deep_reasoning="claude-sonnet-4-5",
            fast_reasoning="claude-haiku-4-5",
            fast_reasoning_effort="none",
            deep_reasoning_effort="high",
            fast_reasoning_provider="default",
            deep_reasoning_provider="anthropic",
            base_url="",
        ))

        with patch("config.get_config", return_value=cfg):
            with pytest.raises(RuntimeError, match="no OpenClaw Anthropic token"):
                adapter.get_llm_provider(model_tier="deep")

    def test_get_llm_provider_keeps_anthropic_error_without_detected_codex_or_tokens(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path / ".quaid"))
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        adapter = OpenClawAdapter()
        monkeypatch.setattr(adapter, "_detect_gateway_primary_provider", lambda: "")
        monkeypatch.setattr(adapter, "_resolve_anthropic_credential", lambda: None)
        cfg = SimpleNamespace(models=SimpleNamespace(
            llm_provider="default",
            deep_reasoning="claude-sonnet-4-5",
            fast_reasoning="claude-haiku-4-5",
            fast_reasoning_effort="none",
            deep_reasoning_effort="high",
            fast_reasoning_provider="default",
            deep_reasoning_provider="default",
            base_url="",
        ))

        with patch("config.get_config", return_value=cfg):
            with pytest.raises(RuntimeError, match="no OpenClaw Anthropic token"):
                adapter.get_llm_provider(model_tier="deep")

    def test_get_llm_provider_replaces_anthropic_models_when_overriding_global_anthropic(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        cfg_dir = home / ".openclaw"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "openclaw.json").write_text(
            json.dumps({
                "agents": {
                    "list": [{"id": "main", "default": True, "model": {"primary": "openai-codex/gpt-5.4"}}],
                }
            }),
            encoding="utf-8",
        )
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.setenv("QUAID_HOME", str(tmp_path / ".quaid"))
        monkeypatch.setenv("OPENAI_API_KEY", "tok.a.b")
        monkeypatch.delenv("OPENAI_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        adapter = OpenClawAdapter()
        monkeypatch.setattr(adapter, "_resolve_anthropic_credential", lambda: None)
        notices = []
        monkeypatch.setattr(adapter, "notify", lambda message, **_kwargs: notices.append(message) or True)
        cfg = SimpleNamespace(models=SimpleNamespace(
            llm_provider="anthropic",
            deep_reasoning="claude-sonnet-4-5",
            fast_reasoning="claude-haiku-4-5",
            fast_reasoning_effort="none",
            deep_reasoning_effort="high",
            fast_reasoning_provider="default",
            deep_reasoning_provider="default",
            base_url="",
        ))

        with patch("config.get_config", return_value=cfg):
            llm = adapter.get_llm_provider(model_tier="deep")

        assert isinstance(llm, OpenAICodexOAuthLLMProvider)
        assert llm._deep_model == "gpt-5.4"
        assert llm._fast_model == "gpt-5.4-mini"
        assert notices
        assert "deep model 'claude-sonnet-4-5' -> 'gpt-5.4'" in notices[0]
        assert "fast model 'claude-haiku-4-5' -> 'gpt-5.4-mini'" in notices[0]

    def test_installer_review_model_pair_flags_unknown_gateway_provider(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        cfg_dir = home / ".openclaw"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "openclaw.json").write_text(
            json.dumps({"agents": {"defaults": {"modelPrimary": "kimik/kimik-2.5"}}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(Path, "home", lambda: home)
        adapter = OpenClawAdapter()

        review = adapter.installer_review_model_pair("", "kimik-2.5-pro", "kimik-2.5-fast")
        assert review["needsClarification"] is True
        assert "kimik" in review["reason"]

    def test_installer_review_model_pair_accepts_supported_prefixed_models(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        cfg_dir = home / ".openclaw"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "openclaw.json").write_text(
            json.dumps({"agents": {"defaults": {"modelPrimary": "openai-codex/gpt-5.4"}}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(Path, "home", lambda: home)
        adapter = OpenClawAdapter()

        review = adapter.installer_review_model_pair(
            "openai",
            "openai/gpt-5.4",
            "openai/gpt-5.4-mini",
        )
        assert review["needsClarification"] is False
        assert review["deep"]["provider"] == "openai"
        assert review["fast"]["provider"] == "openai"

    def test_installer_validate_model_pair_live_is_disabled(self, monkeypatch):
        adapter = OpenClawAdapter()
        result = adapter.installer_validate_model_pair_live(
            "openai",
            "gpt-5.4",
            "gpt-5.4-mini",
        )

        assert result["supported"] is False
        assert result["ok"] is True
        assert result["results"] == []


class TestClaudeCodeAdapter:
    def test_installer_provider_surface_is_anthropic_only(self):
        from adaptors.claude_code.instance_manager import ClaudeCodeInstanceManager

        adapter = ClaudeCodeAdapter()
        assert adapter.installer_supported_providers() == ["anthropic"]
        assert adapter.installer_default_models("anthropic") == {
            "deep": ClaudeCodeInstanceManager.DEFAULT_DEEP_MODEL,
            "fast": ClaudeCodeInstanceManager.DEFAULT_FAST_MODEL,
        }
        assert adapter.get_deep_provider_default() == "anthropic"
        assert adapter.get_fast_provider_default() == "anthropic"
        assert adapter.installer_default_models("openai") is None

    def test_installer_install_state_reports_missing_claude_cli(self, tmp_path, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        state = ClaudeCodeAdapter.installer_install_state(str(tmp_path))
        assert state["status"] == "cannot_install"
        assert "requires claude" in state["reason"]

    def test_get_llm_provider_raises_config_failure_when_failhard_enabled(
        self, monkeypatch
    ):
        from adaptors.claude_code import adapter as adapter_mod

        adapter = ClaudeCodeAdapter()
        monkeypatch.setattr(adapter_mod, "is_fail_hard_enabled", lambda: True)

        with patch("config.get_config", side_effect=RuntimeError("bad config")):
            with pytest.raises(RuntimeError, match="bad config"):
                adapter.get_llm_provider()

    def test_get_llm_provider_warns_and_falls_back_when_failhard_disabled(
        self, monkeypatch, caplog
    ):
        from adaptors.claude_code import adapter as adapter_mod
        from adaptors.claude_code.providers import ClaudeCodeOAuthLLMProvider

        adapter = ClaudeCodeAdapter()
        monkeypatch.setattr(adapter_mod, "is_fail_hard_enabled", lambda: False)

        with patch("config.get_config", side_effect=RuntimeError("bad config")), caplog.at_level(
            "WARNING"
        ):
            provider = adapter.get_llm_provider()

        assert isinstance(provider, ClaudeCodeOAuthLLMProvider)
        assert provider._deep_model is None
        assert provider._fast_model is None
        assert "failed to load Claude Code model config: bad config" in caplog.text

    def test_get_llm_provider_preserves_explicit_empty_model_strings(self):
        cfg = SimpleNamespace(
            models=SimpleNamespace(deep_reasoning="", fast_reasoning="")
        )
        adapter = ClaudeCodeAdapter()

        with patch("config.get_config", return_value=cfg):
            provider = adapter.get_llm_provider()

        assert provider._deep_model == ""
        assert provider._fast_model == ""

    def test_get_discovery_sessions_dir_scopes_path_derived_instance_to_own_project(self, tmp_path, monkeypatch):
        sessions_root = tmp_path / ".claude" / "projects"
        original_dir = sessions_root / "-private-tmp-cc-livetest"
        sibling_dir = sessions_root / "-private-tmp-cc-livetest-m5b"
        original_dir.mkdir(parents=True)
        sibling_dir.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("QUAID_INSTANCE", "claude-code-private-tmp-cc-livetest-m5b")

        adapter = ClaudeCodeAdapter()

        assert adapter.get_sessions_dir() == sessions_root
        assert adapter.get_discovery_sessions_dir() == sibling_dir

    def test_get_discovery_sessions_dir_uses_claude_path_slug_not_instance_hash(self, tmp_path, monkeypatch):
        from lib.instance import _legacy_instance_slug_from_project_dir

        project_dir = tmp_path / "my_project"
        project_dir.mkdir()
        sessions_root = tmp_path / ".claude" / "projects"
        claude_session_dir = sessions_root / f"-{_legacy_instance_slug_from_project_dir(str(project_dir))}"
        claude_session_dir.mkdir(parents=True)
        hashed_instance = f"claude-code-{instance_slug_from_project_dir(str(project_dir))}"
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project_dir))
        monkeypatch.setenv("QUAID_INSTANCE", hashed_instance)

        adapter = ClaudeCodeAdapter()

        assert adapter.get_instance_name() == instance_slug_from_project_dir(str(project_dir))
        assert adapter.get_discovery_sessions_dir() == claude_session_dir

    def test_get_discovery_sessions_dir_uses_binding_without_claude_project_dir(self, tmp_path, monkeypatch):
        from lib.instance import _legacy_instance_slug_from_project_dir

        project_dir = tmp_path / "my_project"
        project_dir.mkdir()
        sessions_root = tmp_path / ".claude" / "projects"
        claude_session_dir = sessions_root / f"-{_legacy_instance_slug_from_project_dir(str(project_dir))}"
        claude_session_dir.mkdir(parents=True)
        hashed_instance = f"claude-code-{instance_slug_from_project_dir(str(project_dir))}"
        cfg_dir = tmp_path / "instances" / hashed_instance
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "config.json").write_text(
            json.dumps({"adapter": {"type": "claude-code"}}),
            encoding="utf-8",
        )
        binding_path = _project_instance_binding_path(tmp_path, "claude-code", str(project_dir))
        assert binding_path is not None
        binding_path.parent.mkdir(parents=True)
        binding_path.write_text(
            json.dumps(
                {
                    "adapter": "claude-code",
                    "instance": hashed_instance,
                    "project_dir": str(project_dir.resolve()),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", hashed_instance)

        adapter = ClaudeCodeAdapter()

        assert adapter.get_discovery_sessions_dir() == claude_session_dir

    def test_owns_session_path_rejects_sibling_claude_project_transcript(self, tmp_path, monkeypatch):
        sessions_root = tmp_path / ".claude" / "projects"
        original_dir = sessions_root / "-private-tmp-cc-livetest"
        sibling_dir = sessions_root / "-private-tmp-cc-livetest-m5b"
        original_dir.mkdir(parents=True)
        sibling_dir.mkdir(parents=True)
        original = original_dir / "658dbac3-e928-4f57-9125-f29aa4aca21c.jsonl"
        sibling = sibling_dir / "fb4dedd5-7fc8-4afb-9e05-397871c9674d.jsonl"
        original.write_text('{"type":"user","message":{"role":"user","content":"foreign"}}\n', encoding="utf-8")
        sibling.write_text('{"type":"user","message":{"role":"user","content":"owned"}}\n', encoding="utf-8")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("QUAID_INSTANCE", "claude-code-private-tmp-cc-livetest-m5b")

        adapter = ClaudeCodeAdapter()

        assert adapter.owns_session_path(original, session_id=original.stem) is False
        assert adapter.owns_session_path(sibling, session_id=sibling.stem) is True
        assert adapter.get_session_path(sibling.stem) == sibling
        assert adapter.get_session_path(original.stem) is None

    def test_notify_raises_queue_failure_when_failhard_enabled(self, tmp_path, monkeypatch):
        from adaptors.claude_code import adapter as adapter_mod

        monkeypatch.setenv("QUAID_INSTANCE", "claude-code-notify-failhard")
        adapter = ClaudeCodeAdapter(home=tmp_path)
        real_open = builtins.open

        def failing_open(path, *args, **kwargs):
            if Path(path) == adapter.data_dir() / "cc-pending-notifications.jsonl":
                raise OSError("disk full")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", failing_open)
        monkeypatch.setattr(adapter_mod, "is_fail_hard_enabled", lambda: True)

        with pytest.raises(OSError, match="disk full"):
            adapter.notify("hello")

    def test_notify_returns_false_queue_failure_when_failhard_disabled(
        self, tmp_path, monkeypatch, capsys
    ):
        from adaptors.claude_code import adapter as adapter_mod

        monkeypatch.setenv("QUAID_INSTANCE", "claude-code-notify-failopen")
        adapter = ClaudeCodeAdapter(home=tmp_path)
        real_open = builtins.open

        def failing_open(path, *args, **kwargs):
            if Path(path) == adapter.data_dir() / "cc-pending-notifications.jsonl":
                raise OSError("disk full")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", failing_open)
        monkeypatch.setattr(adapter_mod, "is_fail_hard_enabled", lambda: False)

        assert adapter.notify("hello") is False
        assert "Failed to queue notification: disk full" in capsys.readouterr().err

    def test_pending_context_default_ttl_drops_stale_entries(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUAID_INSTANCE", "claude-code-pending-ttl")
        adapter = ClaudeCodeAdapter(home=tmp_path)
        pending_path = adapter.data_dir() / "cc-pending-notifications.jsonl"
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        pending_path.write_text(
            "\n".join(
                [
                    json.dumps({"message": "fresh-note"}),
                    json.dumps({"message": "stale-note", "ts": "2000-01-01T00:00:00Z"}),
                ]
            ) + "\n",
            encoding="utf-8",
        )

        context = adapter.get_pending_context()
        assert "MANDATORY: Quaid has active notices for the human user." in context
        assert "fresh-note" in context
        assert "stale-note" not in context

    def test_pending_context_dedupes_identical_messages(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUAID_INSTANCE", "claude-code-pending-dedupe")
        adapter = ClaudeCodeAdapter(home=tmp_path)
        pending_path = adapter.data_dir() / "cc-pending-notifications.jsonl"
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        pending_path.write_text(
            "\n".join(
                [
                    json.dumps({"message": "repeat-note"}),
                    json.dumps({"message": "repeat-note"}),
                    json.dumps({"message": "other-note"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        context = adapter.get_pending_context()
        assert context.count("repeat-note") == 1
        assert context.count("other-note") == 1

    def test_pending_context_preserves_active_provider_notices(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUAID_INSTANCE", "claude-code-pending-provider")
        adapter = ClaudeCodeAdapter(home=tmp_path)
        pending_path = adapter.data_dir() / "cc-pending-notifications.jsonl"
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        pending_path.write_text(
            json.dumps(
                {
                    "message": "[Quaid error] [provider] HTTP 404 invalid-model-m6-probe",
                    "source": "provider",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        first = adapter.get_pending_context()
        second = adapter.get_pending_context()

        assert "invalid-model-m6-probe" in first
        assert "invalid-model-m6-probe" in second
        assert pending_path.is_file()

    def test_pending_context_honors_quaid_now_for_timestamps_and_ttl(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUAID_INSTANCE", "claude-code-pending-clock")
        monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:00:00Z")
        adapter = ClaudeCodeAdapter(home=tmp_path)
        pending_path = adapter.data_dir() / "cc-pending-notifications.jsonl"

        assert adapter.notify("[Quaid error] [provider] invalid-model-pinned") is True
        entry = json.loads(pending_path.read_text(encoding="utf-8"))
        assert entry["ts"] == "2026-03-11T05:00:00+00:00"

        monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:04:00Z")
        first = adapter.get_pending_context(max_age_seconds=300)
        assert "invalid-model-pinned" in first
        assert pending_path.is_file()

        monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:06:00Z")
        second = adapter.get_pending_context(max_age_seconds=300)
        assert second == ""
        assert not pending_path.exists()

    def test_pending_context_rejects_malformed_quaid_now_on_notify(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUAID_INSTANCE", "claude-code-pending-bad-clock")
        monkeypatch.setenv("QUAID_NOW", "not-a-date")
        adapter = ClaudeCodeAdapter(home=tmp_path)

        with pytest.raises(ValueError, match="Invalid QUAID_NOW"):
            adapter.notify("bad clock")

    def test_pending_context_rejects_malformed_quaid_now_on_drain(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUAID_INSTANCE", "claude-code-pending-bad-drain-clock")
        adapter = ClaudeCodeAdapter(home=tmp_path)
        pending_path = adapter.data_dir() / "cc-pending-notifications.jsonl"
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        pending_path.write_text(json.dumps({"message": "bad drain"}) + "\n", encoding="utf-8")
        monkeypatch.setenv("QUAID_NOW", "not-a-date")

        with pytest.raises(ValueError, match="Invalid QUAID_NOW"):
            adapter.get_pending_context()

    def test_pending_context_drains_non_provider_notices(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUAID_INSTANCE", "claude-code-pending-normal")
        adapter = ClaudeCodeAdapter(home=tmp_path)
        pending_path = adapter.data_dir() / "cc-pending-notifications.jsonl"
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        pending_path.write_text(
            json.dumps({"message": "[Quaid warning] [janitor] review ready", "source": "janitor"}) + "\n",
            encoding="utf-8",
        )

        first = adapter.get_pending_context()
        second = adapter.get_pending_context()

        assert "review ready" in first
        assert second == ""
        assert not pending_path.exists()

    def test_pending_context_does_not_emit_when_cleanup_fails_failopen(
        self, tmp_path, monkeypatch, capsys
    ):
        from adaptors.claude_code import adapter as adapter_mod

        monkeypatch.setenv("QUAID_INSTANCE", "claude-code-pending-cleanup-soft")
        adapter = ClaudeCodeAdapter(home=tmp_path)
        pending_path = adapter.data_dir() / "cc-pending-notifications.jsonl"
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        pending_path.write_text(
            json.dumps({"message": "deliver-once"}) + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(adapter_mod, "is_fail_hard_enabled", lambda: False)

        def fail_unlink(*_args, **_kwargs):
            raise OSError("unlink failed")

        monkeypatch.setattr(Path, "unlink", fail_unlink)

        assert adapter.get_pending_context() == ""
        captured = capsys.readouterr()
        assert "deliver-once" not in captured.out
        assert "Failed to clean up pending notifications: unlink failed" in captured.err
        assert pending_path.exists()

    def test_pending_context_raises_when_cleanup_fails_failhard(self, tmp_path, monkeypatch):
        from adaptors.claude_code import adapter as adapter_mod

        monkeypatch.setenv("QUAID_INSTANCE", "claude-code-pending-cleanup-hard")
        adapter = ClaudeCodeAdapter(home=tmp_path)
        pending_path = adapter.data_dir() / "cc-pending-notifications.jsonl"
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        pending_path.write_text(
            json.dumps({"message": "deliver-once"}) + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(adapter_mod, "is_fail_hard_enabled", lambda: True)

        def fail_unlink(*_args, **_kwargs):
            raise OSError("unlink failed")

        monkeypatch.setattr(Path, "unlink", fail_unlink)

        with pytest.raises(OSError, match="unlink failed"):
            adapter.get_pending_context()

    def test_parse_session_jsonl_strips_local_command_wrapper_blocks(self, tmp_path):
        path = tmp_path / "claude-local-command.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "user",
                            "message": {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": (
                                            "<local-command-caveat>Caveat: The messages below were generated by the user while "
                                            "running local commands. DO NOT respond to these messages or otherwise consider "
                                            "them in your response unless the user explicitly asks you to.</local-command-caveat>\n"
                                            "<command-name>/clear</command-name>\n"
                                            "<command-message>clear</command-message>\n"
                                            "<command-args></command-args>\n"
                                            "<local-command-stdout>[2mCompacted (ctrl+o to see full summary)[22m"
                                            "</local-command-stdout>"
                                        ),
                                    }
                                ],
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "user",
                            "message": {
                                "role": "user",
                                "content": [{"type": "text", "text": "My sister is Diana."}],
                            },
                        }
                    ),
                ]
            ),
            encoding="utf-8",
        )
        adapter = ClaudeCodeAdapter()
        transcript = adapter.parse_session_jsonl(path)
        assert "local-command-caveat" not in transcript
        assert "<command-name>" not in transcript
        assert "Compacted (ctrl+o to see full summary)" not in transcript
        assert "/clear" not in transcript
        assert "My sister is Diana." in transcript

    def test_parse_session_jsonl_preserves_text_after_unclosed_local_command_stdout(self, tmp_path):
        path = tmp_path / "claude-local-command-unclosed-stdout.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "<local-command-stdout>command started\n"
                                    "This visible follow-up should stay in the transcript."
                                ),
                            }
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )
        adapter = ClaudeCodeAdapter()
        transcript = adapter.parse_session_jsonl(path)
        assert "This visible follow-up should stay in the transcript." in transcript

    def test_parse_session_jsonl_strips_local_command_metadata_without_hiding_normal_text(self, tmp_path):
        path = tmp_path / "claude-local-command-inline.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "<command-name>/clear</command-name>\n"
                                    "<command-message>clear</command-message>\n"
                                    "<command-args></command-args>\n"
                                    "Can you remind me where Priya works?"
                                ),
                            }
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )
        adapter = ClaudeCodeAdapter()
        transcript = adapter.parse_session_jsonl(path)
        assert "/clear" not in transcript
        assert "Can you remind me where Priya works?" in transcript

    def test_parse_session_jsonl_filters_assistant_provider_response_metadata(self, tmp_path):
        path = tmp_path / "claude-provider-metadata.jsonl"
        provider_payload = {
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "stop_details": {"type": "stop_sequence"},
            "usage": {"input_tokens": 1200, "output_tokens": 18},
        }
        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "user",
                            "message": {
                                "role": "user",
                                "content": [{"type": "text", "text": "The brass lamp is beside the reading chair."}],
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "assistant",
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "text", "text": json.dumps(provider_payload)}],
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "assistant",
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "text", "text": "Noted."}],
                            },
                        }
                    ),
                ]
            ),
            encoding="utf-8",
        )

        adapter = ClaudeCodeAdapter()
        transcript = adapter.parse_session_jsonl(path)

        assert "The brass lamp is beside the reading chair." in transcript
        assert "Assistant: Noted." in transcript
        assert "stop_reason" not in transcript
        assert "input_tokens" not in transcript

    def test_parse_session_jsonl_keeps_assistant_json_without_provider_stop_metadata(self, tmp_path):
        path = tmp_path / "claude-assistant-json.jsonl"
        assistant_payload = {
            "usage": {"meaning": "normal assistant-authored JSON"},
            "summary": "The blue drawer holds the hiking map.",
        }
        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "assistant",
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "text", "text": json.dumps(assistant_payload)}],
                            },
                        }
                    ),
                ]
            ),
            encoding="utf-8",
        )

        adapter = ClaudeCodeAdapter()
        transcript = adapter.parse_session_jsonl(path)

        assert "Assistant:" in transcript
        assert "usage" in transcript
        assert "The blue drawer holds the hiking map." in transcript

    def test_resolve_prompt_submit_signal_returns_session_end_for_clear_command(self):
        adapter = ClaudeCodeAdapter()
        signal = adapter.resolve_prompt_submit_signal({"prompt": "/clear"})
        assert signal is not None
        assert signal["signal_type"] == "session_end"
        assert signal["meta"]["source"] == "hook_inject"
        assert signal["meta"]["command"] == "/clear"
        assert signal["meta"]["reason"] == "command:clear"

    def test_resolve_prompt_submit_signal_returns_compaction_for_compact_command(self):
        adapter = ClaudeCodeAdapter()
        signal = adapter.resolve_prompt_submit_signal({"prompt": "/compact"})
        assert signal is not None
        assert signal["signal_type"] == "compaction"
        assert signal["meta"]["source"] == "hook_inject"
        assert signal["meta"]["command"] == "/compact"
        assert signal["meta"]["reason"] == "command:compact"

    def test_resolve_prompt_submit_signal_detects_local_command_wrapper(self):
        adapter = ClaudeCodeAdapter()
        signal = adapter.resolve_prompt_submit_signal({
            "prompt": (
                "<local-command-caveat>generated by local command</local-command-caveat>\n"
                "<command-name>/clear</command-name>\n"
                "<command-message>clear</command-message>\n"
                "<command-args></command-args>"
            )
        })
        assert signal is not None
        assert signal["signal_type"] == "session_end"
        assert signal["meta"]["command"] == "/clear"
        assert signal["meta"]["reason"] == "command:clear"

    def test_parse_session_jsonl_marks_sidechain_turns_as_subagent(self, tmp_path):
        path = tmp_path / "claude-subagent.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "user",
                            "isSidechain": True,
                            "agentId": "child-123",
                            "message": {
                                "role": "user",
                                "content": [{"type": "text", "text": "My sister is Diana."}],
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "assistant",
                            "isSidechain": True,
                            "agentId": "child-123",
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "text", "text": "Understood."}],
                            },
                        }
                    ),
                ]
            ),
            encoding="utf-8",
        )
        adapter = ClaudeCodeAdapter()
        transcript = adapter.parse_session_jsonl(path)
        assert "Subagent/User: My sister is Diana." in transcript
        assert "Subagent/Assistant: Understood." in transcript

    def test_parse_session_jsonl_preserves_claude_code_row_timestamps(self, tmp_path):
        path = tmp_path / "claude-timestamps.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "timestamp": "2026-06-11T15:08:30.000Z",
                            "type": "user",
                            "message": {
                                "role": "user",
                                "content": [{"type": "text", "text": "I started using a travel nib this week."}],
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "timestamp": "2026-06-11T15:08:35.000Z",
                            "type": "assistant",
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "text", "text": "Noted."}],
                            },
                        }
                    ),
                ]
            ),
            encoding="utf-8",
        )

        adapter = ClaudeCodeAdapter()
        transcript = adapter.parse_session_jsonl(path)

        assert "[2026-06-11T15:08:30.000Z] User: I started using a travel nib this week." in transcript
        assert "[2026-06-11T15:08:35.000Z] Assistant: Noted." in transcript

class TestCodexAdapter:
    def test_turn_scoped_provider_notices_enabled(self):
        adapter = CodexAdapter()
        assert adapter.get_capability("turn_scoped_provider_notices", False) is True

    def test_installer_provider_surface_is_direct_provider_models(self):
        adapter = CodexAdapter()
        assert adapter.installer_supported_providers() == ["anthropic", "openai"]
        assert adapter.installer_default_models("anthropic") == {
            "deep": "claude-sonnet-4-5",
            "fast": "claude-haiku-4-5",
        }
        assert adapter.installer_default_models("openai") == {
            "deep": "gpt-5.4",
            "fast": "gpt-5.4-mini",
            "deepEffort": "high",
            "fastEffort": "none",
        }
        assert adapter.get_deep_provider_default() == "anthropic"
        assert adapter.get_fast_provider_default() == "anthropic"
        assert adapter.installer_supports_live_model_validation() is False

    def test_installer_install_state_reports_missing_codex_cli(self, tmp_path, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        state = CodexAdapter.installer_install_state(str(tmp_path))
        assert state["status"] == "cannot_install"
        assert "requires codex" in state["reason"]

    def test_pending_context_default_ttl_drops_stale_entries(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUAID_INSTANCE", "codex-pending-ttl")
        adapter = CodexAdapter(home=tmp_path)
        pending_path = adapter.data_dir() / "codex-pending-notifications.jsonl"
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        pending_path.write_text(
            "\n".join(
                [
                    json.dumps({"message": "fresh-note"}),
                    json.dumps({"message": "stale-note", "ts": "2000-01-01T00:00:00Z"}),
                ]
            ) + "\n",
            encoding="utf-8",
        )

        context = adapter.get_pending_context()
        assert "MANDATORY: Quaid has active notices for the human user." in context
        assert "fresh-note" in context
        assert "stale-note" not in context

    def test_pending_context_dedupes_identical_messages(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUAID_INSTANCE", "codex-pending-dedupe")
        adapter = CodexAdapter(home=tmp_path)
        pending_path = adapter.data_dir() / "codex-pending-notifications.jsonl"
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        pending_path.write_text(
            "\n".join(
                [
                    json.dumps({"message": "repeat-note"}),
                    json.dumps({"message": "repeat-note"}),
                    json.dumps({"message": "other-note"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        context = adapter.get_pending_context()
        assert context.count("repeat-note") == 1
        assert context.count("other-note") == 1

    def test_pending_context_preserves_active_provider_notices(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUAID_INSTANCE", "codex-pending-provider")
        adapter = CodexAdapter(home=tmp_path)
        pending_path = adapter.data_dir() / "codex-pending-notifications.jsonl"
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        pending_path.write_text(
            json.dumps(
                {
                    "message": "[Quaid error] [provider] HTTP 404 invalid-model-m6-probe",
                    "source": "provider",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        first = adapter.get_pending_context()
        second = adapter.get_pending_context()

        assert "invalid-model-m6-probe" in first
        assert "invalid-model-m6-probe" in second
        assert pending_path.is_file()

    def test_pending_context_honors_quaid_now_for_timestamps_and_ttl(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUAID_INSTANCE", "codex-pending-clock")
        monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:00:00Z")
        adapter = CodexAdapter(home=tmp_path)
        pending_path = adapter.data_dir() / "codex-pending-notifications.jsonl"

        assert adapter.notify("[Quaid error] [provider] invalid-model-pinned") is True
        entry = json.loads(pending_path.read_text(encoding="utf-8"))
        assert entry["ts"] == "2026-03-11T05:00:00+00:00"

        monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:04:00Z")
        first = adapter.get_pending_context(max_age_seconds=300)
        assert "invalid-model-pinned" in first
        assert pending_path.is_file()

        monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:06:00Z")
        second = adapter.get_pending_context(max_age_seconds=300)
        assert second == ""
        assert not pending_path.exists()

    def test_pending_context_rejects_malformed_quaid_now(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUAID_INSTANCE", "codex-pending-bad-clock")
        monkeypatch.setenv("QUAID_NOW", "not-a-date")
        adapter = CodexAdapter(home=tmp_path)

        with pytest.raises(ValueError, match="Invalid QUAID_NOW"):
            adapter.notify("bad clock")

    def test_pending_context_drains_non_provider_notices(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUAID_INSTANCE", "codex-pending-normal")
        adapter = CodexAdapter(home=tmp_path)
        pending_path = adapter.data_dir() / "codex-pending-notifications.jsonl"
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        pending_path.write_text(
            json.dumps({"message": "[Quaid warning] [janitor] review ready", "source": "janitor"}) + "\n",
            encoding="utf-8",
        )

        first = adapter.get_pending_context()
        second = adapter.get_pending_context()

        assert "review ready" in first
        assert second == ""
        assert not pending_path.exists()

    def test_pending_context_raises_on_drain_failure_when_failhard(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUAID_INSTANCE", "codex-pending-failhard")
        adapter = CodexAdapter(home=tmp_path)
        pending_path = adapter.data_dir() / "codex-pending-notifications.jsonl"
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        pending_path.write_text(json.dumps({"message": "deliver-once"}) + "\n", encoding="utf-8")
        monkeypatch.setattr("adaptors.codex.adapter.is_fail_hard_enabled", lambda: True)

        def fail_unlink(*_args, **_kwargs):
            raise OSError("unlink failed")

        monkeypatch.setattr(Path, "unlink", fail_unlink)

        with pytest.raises(OSError, match="unlink failed"):
            adapter.get_pending_context()

    def test_list_agent_instance_ids_raises_on_instance_scan_failure_when_failhard(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("QUAID_INSTANCE", "codex-current")
        adapter = CodexAdapter(home=tmp_path)
        monkeypatch.setattr("adaptors.codex.adapter.is_fail_hard_enabled", lambda: True)

        def fail_iterdir(_path):
            raise OSError("scan failed")

        monkeypatch.setattr(Path, "iterdir", fail_iterdir)

        with pytest.raises(OSError, match="scan failed"):
            adapter.list_agent_instance_ids()

    def test_get_sessions_dir(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / ".codex" / "sessions"
        sessions_dir.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        adapter = CodexAdapter()
        assert adapter.get_sessions_dir() == sessions_dir

    def test_get_session_path_finds_nested_rollout(self, tmp_path, monkeypatch):
        session_id = "019d4367-1794-7fc2-84f3-bb30ba99a24f"
        project_dir = tmp_path / "cdx-project"
        project_dir.mkdir()
        session_file = (
            tmp_path
            / ".codex"
            / "sessions"
            / "2026"
            / "03"
            / "31"
            / f"rollout-2026-03-31T18-18-42-{session_id}.jsonl"
        )
        session_file.parent.mkdir(parents=True)
        session_file.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": session_id, "cwd": str(project_dir)}}) + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("QUAID_INSTANCE", raising=False)
        monkeypatch.setenv("CODEX_PROJECT_DIR", str(project_dir))
        adapter = CodexAdapter()
        assert adapter.get_session_path(session_id) == session_file

    def test_get_session_path_ignores_foreign_project_rollout(self, tmp_path, monkeypatch):
        own_project = tmp_path / "cdx-m13-test"
        foreign_project = tmp_path / "cdx-livetest"
        own_project.mkdir()
        foreign_project.mkdir()
        own_session = "019d4367-1794-7fc2-84f3-bb30ba99a24f"
        foreign_session = "019d4367-1794-7fc2-84f3-bb30ba99a250"
        sessions_root = tmp_path / ".codex" / "sessions" / "2026" / "04" / "20"
        sessions_root.mkdir(parents=True)
        own_file = sessions_root / f"rollout-2026-04-20T12-00-00-{own_session}.jsonl"
        foreign_file = sessions_root / f"rollout-2026-04-20T13-00-00-{foreign_session}.jsonl"
        own_file.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": own_session, "cwd": str(own_project)}}) + "\n",
            encoding="utf-8",
        )
        foreign_file.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": foreign_session, "cwd": str(foreign_project)}}) + "\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("QUAID_INSTANCE", raising=False)
        monkeypatch.setenv("CODEX_PROJECT_DIR", str(own_project))
        adapter = CodexAdapter()

        assert adapter.owns_session_path(own_file) is True
        assert adapter.owns_session_path(foreign_file) is False
        assert adapter.get_session_path(own_session) == own_file
        assert adapter.get_session_path(foreign_session) is None

    def test_get_session_path_ignores_unclassified_rollout(self, tmp_path, monkeypatch):
        session_id = "019d4367-1794-7fc2-84f3-bb30ba99a24f"
        project_dir = tmp_path / "cdx-project"
        project_dir.mkdir()
        sessions_root = tmp_path / ".codex" / "sessions" / "2026" / "04" / "20"
        sessions_root.mkdir(parents=True)
        session_file = sessions_root / f"rollout-2026-04-20T12-00-00-{session_id}.jsonl"
        session_file.write_text(
            json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "Marisol fact"}}) + "\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("QUAID_INSTANCE", raising=False)
        monkeypatch.setenv("CODEX_PROJECT_DIR", str(project_dir))
        adapter = CodexAdapter()

        assert adapter.owns_session_path(session_file) is False
        assert adapter.get_session_path(session_id) is None

    def test_check_session_transition_accepts_thread_id_payload(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("QUAID_INSTANCE", raising=False)
        monkeypatch.setenv("CODEX_PROJECT_DIR", str(tmp_path))
        adapter = CodexAdapter()
        monkeypatch.setattr(adapter, "data_dir", lambda: tmp_path / "data")
        adapter._write_last_session_id("old-thread")
        ended = (
            tmp_path
            / ".codex"
            / "sessions"
            / "2026"
            / "04"
            / "14"
            / "rollout-2026-04-14T12-00-00-old-thread.jsonl"
        )
        ended.parent.mkdir(parents=True)
        ended.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": "old-thread", "cwd": str(tmp_path)}}) + "\n",
            encoding="utf-8",
        )

        signal = adapter.check_session_transition({"thread_id": "new-thread"})
        assert signal is not None
        assert signal["ended_session_id"] == "old-thread"
        assert signal["signal_type"] == "session_end"
        assert adapter._read_last_session_id() == "new-thread"

    def test_write_last_session_id_raises_failure_when_failhard_enabled(self, tmp_path, monkeypatch):
        adapter = CodexAdapter(home=tmp_path)
        monkeypatch.setattr(adapter, "data_dir", lambda: tmp_path / "data")
        original_write_text = Path.write_text

        def failing_write_text(path, *args, **kwargs):
            if path == adapter._last_session_path():
                raise OSError("disk full")
            return original_write_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", failing_write_text)
        monkeypatch.setattr("adaptors.codex.adapter.is_fail_hard_enabled", lambda: True)

        with pytest.raises(OSError, match="disk full"):
            adapter._write_last_session_id("new-thread")

    def test_write_last_session_id_warns_failure_when_failhard_disabled(
        self, tmp_path, monkeypatch, capsys
    ):
        adapter = CodexAdapter(home=tmp_path)
        monkeypatch.setattr(adapter, "data_dir", lambda: tmp_path / "data")
        original_write_text = Path.write_text

        def failing_write_text(path, *args, **kwargs):
            if path == adapter._last_session_path():
                raise OSError("disk full")
            return original_write_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", failing_write_text)
        monkeypatch.setattr("adaptors.codex.adapter.is_fail_hard_enabled", lambda: False)

        adapter._write_last_session_id("new-thread")

        assert "Failed to write Codex last session id: disk full" in capsys.readouterr().err
        assert adapter._read_last_session_id() == ""

    def test_check_session_transition_allows_unclassified_prior_rollout(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("QUAID_INSTANCE", raising=False)
        monkeypatch.setenv("CODEX_PROJECT_DIR", str(tmp_path))
        adapter = CodexAdapter()
        monkeypatch.setattr(adapter, "data_dir", lambda: tmp_path / "data")
        adapter._write_last_session_id("old-thread")
        ended = (
            tmp_path
            / ".codex"
            / "sessions"
            / "2026"
            / "04"
            / "14"
            / "rollout-2026-04-14T12-00-00-old-thread.jsonl"
        )
        ended.parent.mkdir(parents=True)
        ended.write_text(
            json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "Marisol fact"}}) + "\n",
            encoding="utf-8",
        )

        assert adapter.get_session_path("old-thread") is None
        signal = adapter.check_session_transition({"thread_id": "new-thread"})

        assert signal is not None
        assert signal["ended_session_id"] == "old-thread"
        assert signal["ended_transcript_path"] == str(ended)
        assert signal["signal_type"] == "session_end"
        assert adapter._read_last_session_id() == "new-thread"

    def test_check_session_transition_prefers_owned_prior_rollout(self, tmp_path, monkeypatch):
        own_project = tmp_path / "cdx-livetest"
        own_project.mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("QUAID_INSTANCE", raising=False)
        monkeypatch.setenv("CODEX_PROJECT_DIR", str(own_project))
        adapter = CodexAdapter()
        monkeypatch.setattr(adapter, "data_dir", lambda: tmp_path / "data")
        adapter._write_last_session_id("old-thread")
        sessions_root = tmp_path / ".codex" / "sessions" / "2026" / "04" / "14"
        sessions_root.mkdir(parents=True)
        owned = sessions_root / "rollout-2026-04-14T12-00-00-old-thread.jsonl"
        unclassified = sessions_root / "rollout-2026-04-14T12-00-01-old-thread.jsonl"
        owned.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": "old-thread", "cwd": str(own_project)}}) + "\n",
            encoding="utf-8",
        )
        unclassified.write_text(
            json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "Marisol fact"}}) + "\n",
            encoding="utf-8",
        )

        signal = adapter.check_session_transition({"thread_id": "new-thread"})

        assert signal is not None
        assert signal["ended_transcript_path"] == str(owned)

    def test_check_session_transition_rejects_explicit_foreign_prior_rollout(self, tmp_path, monkeypatch):
        own_project = tmp_path / "cdx-livetest"
        foreign_project = tmp_path / "cdx-m13-test"
        own_project.mkdir()
        foreign_project.mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("QUAID_INSTANCE", raising=False)
        monkeypatch.setenv("CODEX_PROJECT_DIR", str(own_project))
        adapter = CodexAdapter()
        monkeypatch.setattr(adapter, "data_dir", lambda: tmp_path / "data")
        adapter._write_last_session_id("old-thread")
        ended = (
            tmp_path
            / ".codex"
            / "sessions"
            / "2026"
            / "04"
            / "14"
            / "rollout-2026-04-14T12-00-00-old-thread.jsonl"
        )
        ended.parent.mkdir(parents=True)
        ended.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": "old-thread", "cwd": str(foreign_project)}}) + "\n",
            encoding="utf-8",
        )

        with patch("adaptors.codex.adapter.is_fail_hard_enabled", return_value=False):
            signal = adapter.check_session_transition({"thread_id": "new-thread"})

        assert signal is None
        assert adapter._read_last_session_id() == "new-thread"

    def test_check_session_transition_raises_on_unresolved_prior_transcript_when_failhard(self, tmp_path, monkeypatch):
        own_project = tmp_path / "cdx-livetest"
        foreign_project = tmp_path / "cdx-m13-test"
        own_project.mkdir()
        foreign_project.mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("QUAID_INSTANCE", raising=False)
        monkeypatch.setenv("CODEX_PROJECT_DIR", str(own_project))
        adapter = CodexAdapter()
        monkeypatch.setattr(adapter, "data_dir", lambda: tmp_path / "data")
        adapter._write_last_session_id("old-thread")
        ended = (
            tmp_path
            / ".codex"
            / "sessions"
            / "2026"
            / "04"
            / "14"
            / "rollout-2026-04-14T12-00-00-old-thread.jsonl"
        )
        ended.parent.mkdir(parents=True)
        ended.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": "old-thread", "cwd": str(foreign_project)}}) + "\n",
            encoding="utf-8",
        )

        with patch("adaptors.codex.adapter.is_fail_hard_enabled", return_value=True), \
             pytest.raises(RuntimeError, match="Codex session transition detected"):
            adapter.check_session_transition({"thread_id": "new-thread"})

    def test_check_session_transition_uses_cursor_for_explicit_instance_label(self, tmp_path, monkeypatch):
        project_dir = tmp_path / "cdx-m13test"
        project_dir.mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("QUAID_HOME", str(tmp_path / ".quaid"))
        monkeypatch.setenv("QUAID_INSTANCE", "codex-m13test")
        monkeypatch.setenv("CODEX_PROJECT_DIR", str(project_dir))
        adapter = CodexAdapter()
        monkeypatch.setattr(adapter, "data_dir", lambda: tmp_path / "data")
        adapter._write_last_session_id("old-thread")
        ended = (
            tmp_path
            / ".codex"
            / "sessions"
            / "2026"
            / "04"
            / "14"
            / "rollout-2026-04-14T12-00-00-old-thread.jsonl"
        )
        ended.parent.mkdir(parents=True)
        ended.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": "old-thread", "cwd": str(project_dir)}}) + "\n",
            encoding="utf-8",
        )
        cursor_dir = tmp_path / ".quaid" / "instances" / "codex-m13test" / "data" / "session-cursors"
        cursor_dir.mkdir(parents=True)
        (cursor_dir / "old-thread.json").write_text(
            json.dumps({"session_id": "old-thread", "transcript_path": str(ended)}) + "\n",
            encoding="utf-8",
        )

        assert adapter._get_session_path("old-thread", allow_unclassified=True) is None
        signal = adapter.check_session_transition({"thread_id": "new-thread"})

        assert signal is not None
        assert signal["ended_session_id"] == "old-thread"
        assert signal["ended_transcript_path"] == str(ended)
        assert signal["signal_type"] == "session_end"
        assert adapter._read_last_session_id() == "new-thread"

    def test_check_session_transition_prefers_cursor_snapshot_over_unclassified_rollout(self, tmp_path, monkeypatch):
        project_dir = tmp_path / "cdx-livetest"
        project_dir.mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("QUAID_HOME", str(tmp_path / ".quaid"))
        monkeypatch.setenv("QUAID_INSTANCE", "codex-private-tmp-cdx-livetest")
        monkeypatch.setenv("CODEX_PROJECT_DIR", str(project_dir))
        adapter = CodexAdapter()
        instance_data = tmp_path / ".quaid" / "instances" / "codex-private-tmp-cdx-livetest" / "data"
        monkeypatch.setattr(adapter, "data_dir", lambda: instance_data)
        adapter._write_last_session_id("old-thread")

        sessions_root = tmp_path / ".codex" / "sessions" / "2026" / "04" / "28"
        sessions_root.mkdir(parents=True)
        unclassified = sessions_root / "rollout-2026-04-28T13-43-15-old-thread.jsonl"
        unclassified.write_text(
            json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "Tamarind fact"}}) + "\n",
            encoding="utf-8",
        )
        snapshot = (
            tmp_path
            / ".quaid"
            / "instances"
            / "codex-private-tmp-cdx-livetest"
            / "logs"
            / "daemon"
            / "rolling-transcript-snapshots"
            / "old-thread.jsonl"
        )
        snapshot.parent.mkdir(parents=True)
        snapshot.write_text(unclassified.read_text(encoding="utf-8"), encoding="utf-8")
        cursor_dir = instance_data / "session-cursors"
        cursor_dir.mkdir(parents=True)
        (cursor_dir / "source-deadbeef.json").write_text(
            json.dumps({"session_id": "old-thread", "transcript_path": str(snapshot)}) + "\n",
            encoding="utf-8",
        )

        assert adapter._get_session_path("old-thread", allow_unclassified=True) == unclassified
        signal = adapter.check_session_transition({"thread_id": "new-thread"})

        assert signal is not None
        assert signal["ended_session_id"] == "old-thread"
        assert signal["ended_transcript_path"] == str(snapshot)
        assert signal["signal_type"] == "session_end"

    def test_check_session_transition_does_not_read_cursor_from_other_instance(self, tmp_path, monkeypatch):
        project_dir = tmp_path / "cdx-m13test"
        project_dir.mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("QUAID_HOME", str(tmp_path / ".quaid"))
        monkeypatch.setenv("QUAID_INSTANCE", "codex-m13test")
        monkeypatch.setenv("CODEX_PROJECT_DIR", str(project_dir))
        adapter = CodexAdapter()
        monkeypatch.setattr(adapter, "data_dir", lambda: tmp_path / "data")
        adapter._write_last_session_id("old-thread")
        ended = (
            tmp_path
            / ".codex"
            / "sessions"
            / "2026"
            / "04"
            / "14"
            / "rollout-2026-04-14T12-00-00-old-thread.jsonl"
        )
        ended.parent.mkdir(parents=True)
        ended.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": "old-thread", "cwd": str(project_dir)}}) + "\n",
            encoding="utf-8",
        )
        foreign_cursor_dir = tmp_path / ".quaid" / "instances" / "codex-other" / "data" / "session-cursors"
        foreign_cursor_dir.mkdir(parents=True)
        (foreign_cursor_dir / "old-thread.json").write_text(
            json.dumps({"session_id": "old-thread", "transcript_path": str(ended)}) + "\n",
            encoding="utf-8",
        )

        with patch("adaptors.codex.adapter.is_fail_hard_enabled", return_value=False):
            signal = adapter.check_session_transition({"thread_id": "new-thread"})

        assert signal is None

    def test_current_instance_id_for_sessions_raises_when_failhard(self, tmp_path, monkeypatch):
        adapter = CodexAdapter(home=tmp_path)
        monkeypatch.setattr(adapter, "instance_id", lambda: (_ for _ in ()).throw(RuntimeError("id failed")))
        monkeypatch.setattr(adapter, "get_instance_name", lambda: "")
        monkeypatch.setattr("adaptors.codex.adapter.is_fail_hard_enabled", lambda: True)

        with pytest.raises(RuntimeError, match="id failed"):
            adapter._current_instance_id_for_sessions()

    def test_parse_session_jsonl_prefers_event_messages(self, tmp_path):
        path = tmp_path / "rollout.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "First user"}}),
                    json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "First answer"}}),
                    json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "First answer"}}),
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": "fallback answer"}],
                            },
                        }
                    ),
                ]
            ),
            encoding="utf-8",
        )
        adapter = CodexAdapter()
        transcript = adapter.parse_session_jsonl(path)
        assert "User: First user" in transcript
        assert "Assistant: First answer" in transcript
        assert transcript.count("Assistant: First answer") == 1
        assert "fallback answer" not in transcript

    def test_parse_session_jsonl_keeps_later_fallback_user_turns_across_codex_tasks(self, tmp_path):
        path = tmp_path / "rollout-mixed-tasks.jsonl"
        first_chunk = "Chunk 1: Ginkgo checklist lives beside the monitor."
        second_chunk = "Chunk 2: Baxter keeps an orange linen notebook from Emília Rosa."
        path.write_text(
            "\n".join(
                [
                    json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": first_chunk}],
                            },
                        }
                    ),
                    json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": first_chunk}}),
                    json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "ACK"}}),
                    json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}),
                    json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                    json.dumps({"type": "turn_context", "payload": {"cwd": str(tmp_path)}}),
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": second_chunk}],
                            },
                        }
                    ),
                ]
            ),
            encoding="utf-8",
        )

        transcript = CodexAdapter().parse_session_jsonl(path)

        assert f"User: {first_chunk}" in transcript
        assert f"User: {second_chunk}" in transcript
        assert transcript.count(first_chunk) == 1

    def test_parse_session_jsonl_preserves_codex_row_timestamps(self, tmp_path):
        path = tmp_path / "rollout-timestamps.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps({
                        "timestamp": "2026-05-02T14:29:21.414Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "The reading chair has a brass desk lamp beside it.",
                        },
                    }),
                    json.dumps({
                        "timestamp": "2026-05-02T14:29:23.024Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "message": "Noted.",
                        },
                    }),
                ]
            ),
            encoding="utf-8",
        )

        adapter = CodexAdapter()
        transcript = adapter.parse_session_jsonl(path)

        assert "[2026-05-02T14:29:21.414Z] User: The reading chair has a brass desk lamp" in transcript
        assert "[2026-05-02T14:29:23.024Z] Assistant: Noted." in transcript

    def test_parse_session_jsonl_marks_thread_spawn_children_as_subagent(self, tmp_path):
        path = tmp_path / "rollout-subagent.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "session_meta",
                            "payload": {
                                "source": {
                                    "subagent": {
                                        "thread_spawn": {
                                            "parent_thread_id": "parent-1",
                                            "depth": 1,
                                            "agent_nickname": "Hegel",
                                        }
                                    }
                                }
                            },
                        }
                    ),
                    json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "My uncle owns a vineyard in Mendoza."}}),
                    json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "Noted."}}),
                ]
            ),
            encoding="utf-8",
        )
        adapter = CodexAdapter()
        transcript = adapter.parse_session_jsonl(path)
        assert "Subagent/User: My uncle owns a vineyard in Mendoza." in transcript
        assert "Subagent/Assistant: Noted." in transcript

    def test_parse_session_jsonl_resets_codex_subagent_source_on_next_session_meta(self, tmp_path):
        path = tmp_path / "rollout-subagent-reset.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "session_meta",
                            "payload": {
                                "source": {
                                    "subagent": {
                                        "thread_spawn": {
                                            "parent_thread_id": "parent-1",
                                            "depth": 1,
                                        }
                                    }
                                }
                            },
                        }
                    ),
                    json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "Subagent-only fact."}}),
                    json.dumps({"type": "session_meta", "payload": {"id": "main-session"}}),
                    json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "Top-level fact."}}),
                ]
            ),
            encoding="utf-8",
        )

        transcript = CodexAdapter().parse_session_jsonl(path)

        assert "Subagent/User: Subagent-only fact." in transcript
        assert "User: Top-level fact." in transcript
        assert "Subagent/User: Top-level fact." not in transcript

    def test_codex_adapter_detects_subagent_session_from_session_meta(self, tmp_path, monkeypatch):
        session_id = "019d734c-2904-7d32-9f06-52011c9d1adb"
        path = (
            tmp_path
            / ".codex"
            / "sessions"
            / "2026"
            / "04"
            / "09"
            / f"rollout-2026-04-09T17-31-04-{session_id}.jsonl"
        )
        path.parent.mkdir(parents=True)
        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "session_meta",
                            "payload": {
                                "source": {
                                    "subagent": {
                                        "thread_spawn": {
                                            "parent_thread_id": "parent-1",
                                        }
                                    }
                                }
                            },
                        }
                    ),
                    json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "Hello"}}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        adapter = CodexAdapter()
        assert adapter.is_subagent_session(session_id, path) is True

    def test_codex_adapter_discovers_subagent_children_by_parent_thread_id(self, tmp_path, monkeypatch):
        parent_session_id = "parent-1"
        child_session_id = "019d734c-2904-7d32-9f06-52011c9d1adb"
        project_dir = tmp_path / "cdx-parent"
        project_dir.mkdir()
        child_path = (
            tmp_path
            / ".codex"
            / "sessions"
            / "2026"
            / "04"
            / "09"
            / f"rollout-2026-04-09T17-31-04-{child_session_id}.jsonl"
        )
        child_path.parent.mkdir(parents=True)
        child_path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "session_meta",
                            "payload": {
                                "cwd": str(project_dir),
                                "source": {
                                    "subagent": {
                                        "thread_spawn": {
                                            "parent_thread_id": parent_session_id,
                                        }
                                    }
                                }
                            },
                        }
                    ),
                    json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "Hello"}}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("QUAID_INSTANCE", raising=False)
        monkeypatch.setenv("CODEX_PROJECT_DIR", str(project_dir))
        adapter = CodexAdapter()
        children = adapter.discover_subagent_children(parent_session_id)
        assert children == [
            {
                "child_id": child_session_id,
                "transcript_path": str(child_path),
                "child_type": "codex-subagent",
            }
        ]

    def test_parse_session_jsonl_ignores_machine_context_fallback_rows(self, tmp_path):
        path = tmp_path / "rollout-machine-context.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": "<environment_context>\n  <cwd>/private/tmp/cdx-livetest</cwd>\n</environment_context>",
                                    }
                                ],
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "developer",
                                "content": [{"type": "input_text", "text": "<quaid_project_context>\n[Quaid Project Context]\n\nruntime details\n</quaid_project_context>"}],
                            },
                        }
                    ),
                ]
            ),
            encoding="utf-8",
        )
        adapter = CodexAdapter()
        transcript = adapter.parse_session_jsonl(path)
        assert transcript == ""

    def test_parse_session_jsonl_strips_quaid_memory_context_block(self, tmp_path):
        path = tmp_path / "rollout-memory-context.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "user_message",
                                "message": (
                                    "<quaid_memory_context>\n"
                                    "[Quaid Memory Context]\n"
                                    "  1. [fact] Maya lives in South Austin (relevance: 0.91)\n"
                                    "</quaid_memory_context>"
                                ),
                            },
                        }
                    )
                ]
            ),
            encoding="utf-8",
        )
        adapter = CodexAdapter()
        transcript = adapter.parse_session_jsonl(path)
        assert transcript == ""

    def test_parse_session_jsonl_strips_codex_visible_memory_context_rows(self, tmp_path):
        path = tmp_path / "rollout-visible-memory-context.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "timestamp": "2026-05-24T01:31:07.000Z",
                            "type": "event_msg",
                            "payload": {
                                "type": "user_message",
                                "message": "What grinder do I use for my espresso setup?",
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "timestamp": "2026-05-24T01:31:08.000Z",
                            "type": "event_msg",
                            "payload": {
                                "type": "agent_message",
                                "message": (
                                    "[Quaid Memory Context]\n"
                                    "  1. [fact][session_chunk] The espresso setup uses a Baratza Encore grinder "
                                    "(relevance: 0.95)\n\n"
                                    "I checked the stored context and found the grinder detail."
                                ),
                            },
                        }
                    ),
                ]
            ),
            encoding="utf-8",
        )
        adapter = CodexAdapter()

        transcript = adapter.parse_session_jsonl(path)

        assert "User: What grinder do I use for my espresso setup?" in transcript
        assert "Assistant: I checked the stored context and found the grinder detail." in transcript
        assert "[Quaid Memory Context]" not in transcript
        assert "Baratza Encore" not in transcript

    def test_parse_session_jsonl_strips_recalled_source_chunk_output_rows(self, tmp_path):
        path = tmp_path / "rollout-visible-source-chunks.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "agent_message",
                                "message": (
                                    "[0.95] [session_chunk][C:0.5] [memory] [session_chunk] "
                                    "rollout-2026-05-23T23-51-25-session#20: M3 guide details\n"
                                    "[session_chunk] rollout-2026-05-23T23-51-25-session#21: "
                                    "deliberate recall probes and date-range checks\n\n"
                                    "The retest is complete."
                                ),
                            },
                        }
                    )
                ]
            ),
            encoding="utf-8",
        )
        adapter = CodexAdapter()

        transcript = adapter.parse_session_jsonl(path)

        assert transcript == "Assistant: The retest is complete."
        assert "M3 guide details" not in transcript
        assert "deliberate recall probes" not in transcript
        assert "[session_chunk]" not in transcript

    def test_parse_session_jsonl_keeps_user_relevance_numbered_list(self, tmp_path):
        path = tmp_path / "rollout-user-relevance-list.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": (
                            "1. [high-priority] Buy filters for the espresso setup "
                            "(relevance: 0.85)\n"
                            "2. Keep the burr brush in the drawer."
                        ),
                    },
                }
            ),
            encoding="utf-8",
        )
        adapter = CodexAdapter()

        transcript = adapter.parse_session_jsonl(path)

        assert "1. [high-priority] Buy filters for the espresso setup (relevance: 0.85)" in transcript
        assert "2. Keep the burr brush in the drawer." in transcript

    def test_parse_session_jsonl_keeps_normal_numbered_bracket_lists(self, tmp_path):
        path = tmp_path / "rollout-numbered-list.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "1. [task] Buy filters for the espresso setup.",
                    },
                }
            ),
            encoding="utf-8",
        )
        adapter = CodexAdapter()

        transcript = adapter.parse_session_jsonl(path)

        assert "User: 1. [task] Buy filters for the espresso setup." in transcript

    def test_parse_session_jsonl_strips_notification_prefix_from_agent_message(self, tmp_path):
        path = tmp_path / "rollout-notify-prefix.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "agent_message",
                                "message": (
                                    "<quaid_notification>\n"
                                    "• **[Quaid — Memory Extraction]**\n\n"
                                    "**Summary:** 2 stored, 0 skipped, 0 edges\n"
                                    "</quaid_notification>\n"
                                    "---\n"
                                    "Nice! February is great for aurora season."
                                ),
                            },
                        }
                    )
                ]
            ),
            encoding="utf-8",
        )
        adapter = CodexAdapter()
        transcript = adapter.parse_session_jsonl(path)
        assert "Memory Extraction" not in transcript
        assert "Summary:" not in transcript
        assert "Nice! February is great for aurora season." in transcript

    def test_parse_session_jsonl_strips_codex_hook_status_block_from_agent_message(self, tmp_path):
        path = tmp_path / "rollout-hook-status.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "agent_message",
                                "message": (
                                    "• Running UserPromptSubmit hook: Quaid recalling memory\n\n"
                                    "UserPromptSubmit hook (completed)\n"
                                    "  hook context: <quaid_memory_context>\n"
                                    "[Quaid Memory Context]\n"
                                    "  1. [fact] Maya lives in South Austin (relevance: 0.91)\n"
                                    "</quaid_memory_context>\n\n"
                                    "Nice! February is great for aurora season."
                                ),
                            },
                        }
                    )
                ]
            ),
            encoding="utf-8",
        )
        adapter = CodexAdapter()
        transcript = adapter.parse_session_jsonl(path)
        assert "Running UserPromptSubmit hook" not in transcript
        assert "hook context:" not in transcript
        assert "Maya lives in South Austin" not in transcript
        assert "Nice! February is great for aurora season." in transcript

    def test_parse_session_jsonl_preserves_assistant_quaid_notice_bullet_block(self, tmp_path):
        path = tmp_path / "rollout-quaid-notice-bullets.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "agent_message",
                                "message": (
                                    "Hello.\n\n"
                                    "Quaid notices:\n"
                                    "- `Quaid fast LLM call failed`: timed out waiting for Codex turn `abc`.\n"
                                    "- `Janitor has never completed successfully.`\n\n"
                                    "What do you want to work on in this repo?"
                                ),
                            },
                        }
                    )
                ]
            ),
            encoding="utf-8",
        )
        adapter = CodexAdapter()
        transcript = adapter.parse_session_jsonl(path)
        assert "Quaid notices:" in transcript
        assert "fast LLM call failed" in transcript
        assert "Janitor has never completed successfully" in transcript
        assert "What do you want to work on in this repo?" in transcript

    def test_parse_session_jsonl_preserves_assistant_extraction_relay_facts(self, tmp_path):
        path = tmp_path / "rollout-extraction-relay.jsonl"
        chunk_two = (
            "Chunk 2: Baxter keeps an orange linen notebook from Emília Rosa "
            "beside the archive shelf."
        )
        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "user_message",
                                "message": "Chunk 1: Ginkgo checklist lives beside the monitor.",
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "agent_message",
                                "message": (
                                    "Quaid extracted 5 memories, skipped 0, and created 2 relationships. "
                                    "It stored that Solomon Steadman lives in Singapore, uses a Flair 58, "
                                    "and listens to Nils Frahm."
                                ),
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "user_message",
                                "message": chunk_two,
                            },
                        }
                    ),
                    json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "ACK"}}),
                ]
            ),
            encoding="utf-8",
        )

        transcript = CodexAdapter().parse_session_jsonl(path)

        assert "Quaid extracted 5 memories" in transcript
        assert "Solomon Steadman lives in Singapore" in transcript
        assert "Flair 58" in transcript
        assert chunk_two in transcript
        assert "Assistant: ACK" in transcript

    def test_parse_session_jsonl_preserves_assistant_pending_notice_commentary(self, tmp_path):
        path = tmp_path / "rollout-quaid-notice-commentary.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "agent_message",
                                "message": "You started a new interaction. I’m checking the pending Quaid notice first, then I’ll reply directly.",
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "user_message",
                                "message": "Tell me about Baxter.",
                            },
                        }
                    ),
                ]
            ),
            encoding="utf-8",
        )
        adapter = CodexAdapter()
        transcript = adapter.parse_session_jsonl(path)
        assert "pending Quaid notice" in transcript
        assert "Tell me about Baxter." in transcript

    def test_parse_session_jsonl_preserves_assistant_recall_debug_commentary(self, tmp_path):
        path = tmp_path / "rollout-quaid-recall-debug-commentary.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "agent_message",
                                "message": (
                                    "Quaid is noisy on startup here, and the recall output is getting buried. "
                                    "I'm giving it one last pass."
                                ),
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "user_message",
                                "message": "My espresso setup uses a Baratza Encore grinder.",
                            },
                        }
                    ),
                ]
            ),
            encoding="utf-8",
        )
        adapter = CodexAdapter()
        transcript = adapter.parse_session_jsonl(path)
        assert "Assistant: Quaid is noisy on startup here" in transcript
        assert "User: My espresso setup uses a Baratza Encore grinder." in transcript

    def test_parse_session_jsonl_strips_openclaw_memory_paths_without_dropping_reply(self, tmp_path):
        path = tmp_path / "rollout-openclaw-memory-ack.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "user_message",
                                "message": "Quick one to remember: my workshop safe codeword is cobalt-postage-oc.",
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "agent_message",
                                "message": (
                                    "Got it, I have remembered cobalt-postage-oc and saved it in "
                                    "memory/2026-04-25-1909.md / openclaw-workspace."
                                ),
                            },
                        }
                    ),
                ]
            ),
            encoding="utf-8",
        )
        adapter = OpenClawAdapter()
        transcript = adapter.parse_session_jsonl(path)
        assert "cobalt-postage-oc" in transcript
        assert "memory/2026-04-25-1909.md" not in transcript
        assert "openclaw-workspace" not in transcript
        assert "I have remembered cobalt-postage-oc" in transcript

    def test_parse_session_jsonl_preserves_openclaw_durable_memory_refusal_without_path_marker(self, tmp_path):
        path = tmp_path / "rollout-openclaw-memory-refusal.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "user_message",
                                "message": (
                                    "The workshop shelf marker is walnut-umbrella-7142."
                                ),
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "agent_message",
                                "message": (
                                    "I won't store that as durable memory unless you want me to."
                                ),
                            },
                        }
                    ),
                ]
            ),
            encoding="utf-8",
        )
        adapter = OpenClawAdapter()
        transcript = adapter.parse_session_jsonl(path)
        assert "walnut-umbrella-7142" in transcript
        assert "durable memory" in transcript
        assert "won't store that" in transcript

    def test_parse_session_jsonl_preserves_openclaw_reminder_acknowledgement_without_path_marker(self, tmp_path):
        path = tmp_path / "rollout-openclaw-memory-reminder-ack.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "user_message",
                                "message": (
                                    "The workshop shelf marker is cedar-lantern-235854."
                                ),
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "agent_message",
                                "message": (
                                    "Got it. I’ll remember that the workshop shelf marker is "
                                    "cedar-lantern-235854.\n\n"
                                    "Note: I did not schedule a reminder in this turn, so this will "
                                    "not trigger automatically."
                                ),
                            },
                        }
                    ),
                ]
            ),
            encoding="utf-8",
        )
        adapter = OpenClawAdapter()
        transcript = adapter.parse_session_jsonl(path)
        assert "cedar-lantern-235854" in transcript
        assert "i’ll remember" in transcript.lower()
        assert "did not schedule a reminder" in transcript.lower()

    def test_resolve_stop_hook_signal_returns_none_for_regular_turn(self, tmp_path):
        path = tmp_path / "rollout-regular-turn.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "Tell me about Baxter."}}),
                    json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "Baxter loves tennis balls."}}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        adapter = CodexAdapter()
        assert adapter.resolve_stop_hook_signal({}, str(path)) is None

    def test_resolve_stop_hook_signal_returns_session_end_for_lifecycle_command(self, tmp_path):
        path = tmp_path / "rollout-lifecycle.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "/new"}}),
                    json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "Started a fresh session."}}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        adapter = CodexAdapter()
        signal = adapter.resolve_stop_hook_signal({}, str(path))
        assert signal is not None
        assert signal["signal_type"] == "session_end"
        assert signal["meta"]["command"] == "/new"
        assert signal["meta"]["reason"] == "command:new"

    def test_resolve_prompt_submit_signal_returns_session_end_for_lifecycle_command(self):
        adapter = CodexAdapter()
        signal = adapter.resolve_prompt_submit_signal({"prompt": "/clear"})
        assert signal is not None
        assert signal["signal_type"] == "session_end"
        assert signal["meta"]["command"] == "/clear"
        assert signal["meta"]["reason"] == "command:clear"

    def test_get_llm_provider_returns_openai_provider(self, monkeypatch, tmp_path):
        adapter = CodexAdapter()
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
        cfg = SimpleNamespace(
            models=SimpleNamespace(
                llm_provider="openai",
                fast_reasoning_provider="default",
                deep_reasoning_provider="default",
                deep_reasoning="gpt-5.4",
                fast_reasoning="gpt-5.4-mini",
                deep_reasoning_effort="high",
                fast_reasoning_effort="none",
                base_url="",
            )
        )
        with patch("config.get_config", return_value=cfg):
            provider = adapter.get_llm_provider()
        assert isinstance(provider, OpenAICodexOAuthLLMProvider)
        assert provider._base_url == "https://chatgpt.com/backend-api"

    def test_get_llm_provider_uses_configured_openai_api_key_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_OAUTH_TOKEN", raising=False)
        monkeypatch.setenv("BENCHMARK_CODEX_API_KEY", "tok.benchmark.jwt")
        adapter = CodexAdapter()
        adapter.store_shared_auth_token("anthropic_api", "sk-ant-shared")
        cfg = SimpleNamespace(
            models=SimpleNamespace(
                llm_provider="openai-compatible",
                fast_reasoning_provider="default",
                deep_reasoning_provider="default",
                deep_reasoning="gpt-5.4",
                fast_reasoning="gpt-5.4-mini",
                deep_reasoning_effort="high",
                fast_reasoning_effort="none",
                base_url="",
                api_key_env="BENCHMARK_CODEX_API_KEY",
            )
        )
        with patch("config.get_config", return_value=cfg):
            provider = adapter.get_llm_provider()
        assert isinstance(provider, OpenAICodexOAuthLLMProvider)
        assert provider._api_key == "tok.benchmark.jwt"

    def test_get_llm_provider_uses_legacy_codex_auth_token_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.delenv("BENCHMARK_CODEX_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_OAUTH_TOKEN", raising=False)
        adapter = CodexAdapter()
        token_path = adapter.auth_token_path()
        assert token_path is not None
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text("tok.legacy.jwt\n", encoding="utf-8")
        cfg = SimpleNamespace(
            models=SimpleNamespace(
                llm_provider="openai-compatible",
                fast_reasoning_provider="default",
                deep_reasoning_provider="default",
                deep_reasoning="gpt-5.4",
                fast_reasoning="gpt-5.4-mini",
                deep_reasoning_effort="high",
                fast_reasoning_effort="none",
                base_url="",
                api_key_env="BENCHMARK_CODEX_API_KEY",
            )
        )
        with patch("config.get_config", return_value=cfg):
            provider = adapter.get_llm_provider()
        assert isinstance(provider, OpenAICodexOAuthLLMProvider)
        assert provider._api_key == "tok.legacy.jwt"

    def test_get_llm_provider_returns_anthropic_provider(self, monkeypatch):
        adapter = CodexAdapter()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        cfg = SimpleNamespace(
            models=SimpleNamespace(
                llm_provider="anthropic",
                fast_reasoning_provider="default",
                deep_reasoning_provider="default",
                deep_reasoning="claude-sonnet-4-5",
                fast_reasoning="claude-haiku-4-5",
                deep_reasoning_effort="high",
                fast_reasoning_effort="none",
                base_url="",
            )
        )
        with patch("config.get_config", return_value=cfg):
            provider = adapter.get_llm_provider()
        assert isinstance(provider, AnthropicLLMProvider)

    def test_get_llm_provider_uses_model_hints_when_provider_default(self, monkeypatch):
        adapter = CodexAdapter()
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
        cfg = SimpleNamespace(
            models=SimpleNamespace(
                llm_provider="default",
                fast_reasoning_provider="default",
                deep_reasoning_provider="default",
                deep_reasoning="gpt-5.4",
                fast_reasoning="gpt-5.4-mini",
                deep_reasoning_effort="high",
                fast_reasoning_effort="none",
                base_url="",
            )
        )
        with patch("config.get_config", return_value=cfg):
            provider = adapter.get_llm_provider()
        assert isinstance(provider, OpenAICodexOAuthLLMProvider)

    def test_get_llm_provider_uses_shared_auth_when_provider_default(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        adapter = CodexAdapter()
        adapter.store_shared_auth_token("codex_oauth", "tok.a.b")
        cfg = SimpleNamespace(
            models=SimpleNamespace(
                llm_provider="default",
                fast_reasoning_provider="default",
                deep_reasoning_provider="default",
                deep_reasoning="default",
                fast_reasoning="default",
                deep_reasoning_effort="high",
                fast_reasoning_effort="none",
                base_url="",
            )
        )
        with patch("config.get_config", return_value=cfg):
            provider = adapter.get_llm_provider()
        assert isinstance(provider, OpenAICodexOAuthLLMProvider)

    def test_get_llm_provider_prefers_single_anthropic_shared_auth_over_openai_model_hints(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        adapter = CodexAdapter()
        adapter.store_shared_auth_token("anthropic_oauth", "sk-ant-registry")
        cfg = SimpleNamespace(
            models=SimpleNamespace(
                llm_provider="default",
                fast_reasoning_provider="default",
                deep_reasoning_provider="default",
                deep_reasoning="gpt-5.4",
                fast_reasoning="gpt-5.4-mini",
                deep_reasoning_effort="high",
                fast_reasoning_effort="none",
                deep_reasoning_model_classes={},
                fast_reasoning_model_classes={},
                base_url="",
            )
        )
        with patch("config.get_config", return_value=cfg):
            provider = adapter.get_llm_provider()
        assert isinstance(provider, AnthropicLLMProvider)
        assert provider._deep_model == "claude-sonnet-4-5"
        assert provider._fast_model == "claude-haiku-4-5"

    def test_get_llm_provider_uses_single_anthropic_shared_auth_when_configured_openai_missing_credential(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        adapter = CodexAdapter()
        adapter.store_shared_auth_token("anthropic_api", "sk-ant-registry")
        cfg = SimpleNamespace(
            models=SimpleNamespace(
                llm_provider="openai",
                fast_reasoning_provider="default",
                deep_reasoning_provider="default",
                deep_reasoning="gpt-5.4",
                fast_reasoning="gpt-5.4-mini",
                deep_reasoning_effort="high",
                fast_reasoning_effort="none",
                deep_reasoning_model_classes={},
                fast_reasoning_model_classes={},
                base_url="",
            )
        )
        with patch("config.get_config", return_value=cfg):
            provider = adapter.get_llm_provider()
        assert isinstance(provider, AnthropicLLMProvider)
        assert provider._deep_model == "claude-sonnet-4-5"
        assert provider._fast_model == "claude-haiku-4-5"

    def test_get_api_key_reads_codex_auth_token_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        adapter = CodexAdapter()
        token_path = adapter.store_auth_token("sk-codex-file-token")
        assert token_path == tmp_path / "shared" / "auth" / "credentials.json"
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert adapter.get_api_key("OPENAI_API_KEY") == "sk-codex-file-token"

    def test_get_api_key_reads_legacy_codex_auth_token_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_OAUTH_TOKEN", raising=False)
        adapter = CodexAdapter()
        token_path = adapter.auth_token_path()
        assert token_path is not None
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text("tok.legacy.jwt\n", encoding="utf-8")

        assert adapter.get_api_key("OPENAI_API_KEY") == "tok.legacy.jwt"

    def test_get_api_key_reads_shared_registry_before_adapter_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        adapter = CodexAdapter()
        adapter.store_shared_auth_token("anthropic_api", "sk-ant-registry")
        adapter.store_auth_token("sk-openai-file-token")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert adapter.get_api_key("ANTHROPIC_API_KEY") == "sk-ant-registry"

    def test_get_api_key_reads_openclaw_auth_token_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        adapter = OpenClawAdapter()
        token_path = adapter.store_auth_token("sk-openclaw-file-token")
        assert token_path == tmp_path / "shared" / "auth" / "credentials.json"
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert adapter.get_api_key("OPENAI_API_KEY") == "sk-openclaw-file-token"

    def test_get_cli_tools_snippet_includes_project_metadata_update_guidance(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_INSTANCE", "codex-livetest")
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        adapter = CodexAdapter()
        snippet = adapter.get_cli_tools_snippet()
        assert "quaid project update <name> --description" in snippet
        assert "Do not treat edits to `PROJECT.md`" in snippet
        assert "quaid registry register <absolute-file-path> --project misc--codex-livetest" in snippet

    def test_get_api_key_raises_when_fail_hard_enabled(self, monkeypatch):
        adapter = CodexAdapter()
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with patch("adaptors.codex.adapter.is_fail_hard_enabled", return_value=True):
            with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
                adapter.get_api_key("OPENAI_API_KEY")


# ---------------------------------------------------------------------------
# Adapter Selection Tests
# ---------------------------------------------------------------------------

class TestAdapterSelection:
    def test_config_standalone(self, monkeypatch, tmp_path):
        _write_adapter_config(tmp_path, "standalone")
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        adapter = get_adapter()
        assert isinstance(adapter, StandaloneAdapter)

    @pytest.mark.adapter_openclaw
    def test_config_openclaw(self, monkeypatch, tmp_path):
        _write_adapter_config(tmp_path, "openclaw")
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("OPENCLAW_WORKSPACE", str(tmp_path))
        adapter = get_adapter()
        assert isinstance(adapter, OpenClawAdapter)

    def test_config_codex(self, monkeypatch, tmp_path):
        _write_adapter_config(tmp_path, "codex")
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        adapter = get_adapter()
        assert isinstance(adapter, CodexAdapter)

    def test_config_codex_from_lean_instance_config(self, monkeypatch, tmp_path):
        instance_id = "codex-lean"
        cfg_dir = tmp_path / "instances" / instance_id
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "config.json").write_text(
            json.dumps({"instance": {"id": instance_id}, "adapter": {"type": "codex"}}),
            encoding="utf-8",
        )
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", instance_id)
        adapter = get_adapter()
        assert isinstance(adapter, CodexAdapter)

    def test_config_codex_derived_project_instance_writes_binding(self, monkeypatch, tmp_path):
        project_dir = tmp_path / "cdx-project"
        project_dir.mkdir()
        slug = "cdx-project"
        expected_instance = f"codex-{slug}"

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path / "visible"))
        monkeypatch.delenv("QUAID_INSTANCE", raising=False)
        monkeypatch.delenv("QUAID_ADAPTER_TYPE", raising=False)
        monkeypatch.setenv("CODEX_PROJECT_DIR", str(project_dir))

        with patch("lib.instance.instance_slug_from_project_dir", return_value=slug):
            adapter = get_adapter()

        assert isinstance(adapter, CodexAdapter)
        assert os.environ.get("QUAID_INSTANCE") == expected_instance
        with patch("lib.instance.instance_slug_from_project_dir", return_value=slug):
            binding_path = _project_instance_binding_path(tmp_path, "codex", str(project_dir))
        assert binding_path is not None
        payload = json.loads(binding_path.read_text(encoding="utf-8"))
        assert payload["instance"] == expected_instance

    def test_config_codex_explicit_instance_does_not_overwrite_project_binding(self, monkeypatch, tmp_path):
        project_dir = tmp_path / "cdx-livetest"
        project_dir.mkdir()
        explicit_instance = "codex-m13test"
        slug = "private-tmp-cdx-livetest"
        canonical_instance = f"codex-{slug}"
        for instance_id in (explicit_instance, canonical_instance):
            cfg_dir = tmp_path / "instances" / instance_id
            cfg_dir.mkdir(parents=True, exist_ok=True)
            (cfg_dir / "config.json").write_text(
                json.dumps({"adapter": {"type": "codex"}}),
                encoding="utf-8",
            )
            data_dir = cfg_dir / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "memory.db").touch()
        with patch("lib.instance.instance_slug_from_project_dir", return_value=slug):
            binding_path = _project_instance_binding_path(tmp_path, "codex", str(project_dir))
        assert binding_path is not None
        binding_path.parent.mkdir(parents=True, exist_ok=True)
        binding_path.write_text(
            json.dumps(
                {
                    "adapter": "codex",
                    "instance": canonical_instance,
                    "project_dir": str(project_dir.resolve()),
                }
            )
            + "\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path / "visible"))
        monkeypatch.setenv("QUAID_INSTANCE", explicit_instance)
        monkeypatch.setenv("QUAID_ADAPTER_TYPE", "codex")
        monkeypatch.setenv("CODEX_PROJECT_DIR", str(project_dir))

        with patch("lib.instance.instance_slug_from_project_dir", return_value=slug):
            adapter = get_adapter()

        assert isinstance(adapter, CodexAdapter)
        payload = json.loads(binding_path.read_text(encoding="utf-8"))
        assert payload["instance"] == canonical_instance

    def test_project_binding_reads_legacy_slug_when_project_matches(self, tmp_path):
        project_dir = tmp_path / "cdx_project"
        sibling_dir = tmp_path / "cdx-project"
        project_dir.mkdir()
        sibling_dir.mkdir()
        explicit_instance = "codex-explicit"
        cfg_dir = tmp_path / "instances" / explicit_instance
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "config.json").write_text(
            json.dumps({"adapter": {"type": "codex"}}),
            encoding="utf-8",
        )

        new_path = _project_instance_binding_path(tmp_path, "codex", str(project_dir))
        legacy_slug = _legacy_instance_slug_from_project_dir(str(project_dir))
        legacy_path = tmp_path / "shared" / "instance-bindings" / "codex" / f"{legacy_slug}.json"
        assert new_path is not None
        assert new_path != legacy_path
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text(
            json.dumps(
                {
                    "adapter": "codex",
                    "instance": explicit_instance,
                    "project_dir": str(project_dir.resolve()),
                }
            )
            + "\n",
            encoding="utf-8",
        )

        assert _read_project_instance_binding(tmp_path, "codex", str(project_dir)) == explicit_instance
        assert _read_project_instance_binding(tmp_path, "codex", str(sibling_dir)) == ""

    def test_config_missing_adapter_type_fails_loud(self, monkeypatch, tmp_path):
        instance_id = "codex-lean"
        cfg_dir = tmp_path / "instances" / instance_id
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "config.json").write_text(
            json.dumps({"instance": {"id": instance_id}}),
            encoding="utf-8",
        )
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", instance_id)
        monkeypatch.delenv("QUAID_ADAPTER_TYPE", raising=False)
        monkeypatch.delenv("OPENCLAW_WORKSPACE", raising=False)
        monkeypatch.delenv("QUAID_WORKSPACE", raising=False)
        monkeypatch.chdir(tmp_path)
        with pytest.raises(RuntimeError, match="instance config includes adapter.type"):
            get_adapter()

    def test_config_claude_code_from_cwd_with_explicit_adapter_type(self, monkeypatch, tmp_path):
        project_dir = tmp_path / "cc-project"
        project_dir.mkdir()
        slug = instance_slug_from_project_dir(str(project_dir))
        cfg_dir = tmp_path / "instances" / f"claude-code-{slug}"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "config.json").write_text('{"adapter":{"type":"claude-code"}}', encoding="utf-8")

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path / "visible"))
        monkeypatch.setenv("QUAID_ADAPTER_TYPE", "claude-code")
        monkeypatch.delenv("QUAID_INSTANCE", raising=False)
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.delenv("CODEX_PROJECT_DIR", raising=False)
        monkeypatch.chdir(project_dir)

        adapter = get_adapter()

        assert isinstance(adapter, ClaudeCodeAdapter)
        assert os.environ.get("QUAID_INSTANCE") == f"claude-code-{slug}"

    def test_config_codex_from_cwd_with_explicit_adapter_type(self, monkeypatch, tmp_path):
        project_dir = tmp_path / "cdx-project"
        project_dir.mkdir()
        slug = instance_slug_from_project_dir(str(project_dir))
        cfg_dir = tmp_path / "instances" / f"codex-{slug}"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "config.json").write_text('{"adapter":{"type":"codex"}}', encoding="utf-8")

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path / "visible"))
        monkeypatch.setenv("QUAID_ADAPTER_TYPE", "codex")
        monkeypatch.delenv("QUAID_INSTANCE", raising=False)
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.delenv("CODEX_PROJECT_DIR", raising=False)
        monkeypatch.chdir(project_dir)

        adapter = get_adapter()

        assert isinstance(adapter, CodexAdapter)
        assert os.environ.get("QUAID_INSTANCE") == f"codex-{slug}"

    def test_config_claude_code_from_cwd_without_explicit_adapter_type_when_unique(self, monkeypatch, tmp_path):
        project_dir = tmp_path / "cc-project"
        project_dir.mkdir()
        slug = instance_slug_from_project_dir(str(project_dir))
        cfg_dir = tmp_path / "instances" / f"claude-code-{slug}"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "config.json").write_text('{"adapter":{"type":"claude-code"}}', encoding="utf-8")

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path / "visible"))
        monkeypatch.delenv("QUAID_ADAPTER_TYPE", raising=False)
        monkeypatch.delenv("QUAID_INSTANCE", raising=False)
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.delenv("CODEX_PROJECT_DIR", raising=False)
        monkeypatch.delenv("OPENCLAW_WORKSPACE", raising=False)
        monkeypatch.delenv("QUAID_WORKSPACE", raising=False)
        monkeypatch.chdir(project_dir)

        adapter = get_adapter()

        assert isinstance(adapter, ClaudeCodeAdapter)
        assert os.environ.get("QUAID_INSTANCE") == f"claude-code-{slug}"

    def test_config_codex_from_cwd_without_explicit_adapter_type_when_unique(self, monkeypatch, tmp_path):
        project_dir = tmp_path / "cdx-project"
        project_dir.mkdir()
        slug = instance_slug_from_project_dir(str(project_dir))
        cfg_dir = tmp_path / "instances" / f"codex-{slug}"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "config.json").write_text('{"adapter":{"type":"codex"}}', encoding="utf-8")

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path / "visible"))
        monkeypatch.delenv("QUAID_ADAPTER_TYPE", raising=False)
        monkeypatch.delenv("QUAID_INSTANCE", raising=False)
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.delenv("CODEX_PROJECT_DIR", raising=False)
        monkeypatch.delenv("OPENCLAW_WORKSPACE", raising=False)
        monkeypatch.delenv("QUAID_WORKSPACE", raising=False)
        monkeypatch.chdir(project_dir)

        adapter = get_adapter()

        assert isinstance(adapter, CodexAdapter)
        assert os.environ.get("QUAID_INSTANCE") == f"codex-{slug}"

    def test_config_from_cwd_without_explicit_adapter_type_refuses_ambiguous_folder_configs(self, monkeypatch, tmp_path):
        project_dir = tmp_path / "shared-project"
        project_dir.mkdir()
        slug = instance_slug_from_project_dir(str(project_dir))
        for adapter_id in ("claude-code", "codex"):
            cfg_dir = tmp_path / "instances" / f"{adapter_id}-{slug}"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            (cfg_dir / "config.json").write_text(
                json.dumps({"adapter": {"type": adapter_id}}),
                encoding="utf-8",
            )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path / "visible"))
        monkeypatch.delenv("QUAID_ADAPTER_TYPE", raising=False)
        monkeypatch.delenv("QUAID_INSTANCE", raising=False)
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.delenv("CODEX_PROJECT_DIR", raising=False)
        monkeypatch.delenv("OPENCLAW_WORKSPACE", raising=False)
        monkeypatch.delenv("QUAID_WORKSPACE", raising=False)
        monkeypatch.chdir(project_dir)

        with pytest.raises(RuntimeError, match="Ambiguous adapter resolution: multiple cwd-derived adapter"):
            get_adapter()

        assert os.environ.get("QUAID_INSTANCE") is None

    def test_explicit_non_folder_adapter_type_does_not_probe_cwd_folder_configs(self, monkeypatch, tmp_path):
        project_dir = tmp_path / "openclaw-project"
        project_dir.mkdir()
        slug = instance_slug_from_project_dir(str(project_dir))
        cfg_dir = tmp_path / "instances" / f"codex-{slug}"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "config.json").write_text('{"adapter":{"type":"codex"}}', encoding="utf-8")

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path / "visible"))
        monkeypatch.setenv("QUAID_ADAPTER_TYPE", "openclaw")
        monkeypatch.delenv("QUAID_INSTANCE", raising=False)
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.delenv("CODEX_PROJECT_DIR", raising=False)
        monkeypatch.delenv("OPENCLAW_WORKSPACE", raising=False)
        monkeypatch.delenv("QUAID_WORKSPACE", raising=False)
        monkeypatch.chdir(project_dir)

        with pytest.raises(RuntimeError, match="No config file found|must set adapter type"):
            get_adapter()

        assert os.environ.get("QUAID_INSTANCE") is None

    @pytest.mark.adapter_openclaw
    def test_config_openclaw_from_single_shared_platform_config(self, monkeypatch, tmp_path):
        _write_shared_platform_config(tmp_path, "openclaw")
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("OPENCLAW_WORKSPACE", str(tmp_path))
        adapter = get_adapter()
        assert isinstance(adapter, OpenClawAdapter)

    def test_missing_adapter_raises(self, monkeypatch, tmp_path):
        reset_adapter()
        monkeypatch.delenv("QUAID_HOME", raising=False)
        monkeypatch.delenv("OPENCLAW_WORKSPACE", raising=False)
        monkeypatch.chdir(tmp_path)
        with pytest.raises(RuntimeError, match="No config file found|must set adapter type"):
            get_adapter()

    def test_set_adapter(self, tmp_path):
        custom = StandaloneAdapter(home=tmp_path)
        set_adapter(custom)
        assert get_adapter() is custom

    def test_reset_adapter(self, monkeypatch, tmp_path):
        custom = StandaloneAdapter(home=Path("/tmp/custom"))
        set_adapter(custom)
        reset_adapter()
        # After reset, should resolve from config again
        _write_adapter_config(tmp_path, "standalone")
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        adapter = get_adapter()
        assert adapter is not custom

    def test_singleton_caching(self, monkeypatch, tmp_path):
        _write_adapter_config(tmp_path, "standalone")
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        a1 = get_adapter()
        a2 = get_adapter()
        assert a1 is a2


# ---------------------------------------------------------------------------
# _read_env_file Tests
# ---------------------------------------------------------------------------

class TestReadEnvFile:
    def test_reads_simple_value(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("FOO=bar\n")
        assert _read_env_file(env, "FOO") == "bar"

    def test_reads_quoted_value(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text('FOO="hello world"\n')
        assert _read_env_file(env, "FOO") == "hello world"

    def test_skips_comments(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("# FOO=commented\nFOO=real\n")
        assert _read_env_file(env, "FOO") == "real"

    def test_returns_none_for_missing(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("BAR=baz\n")
        assert _read_env_file(env, "FOO") is None

    def test_returns_none_for_missing_file(self, tmp_path):
        assert _read_env_file(tmp_path / "nonexistent", "FOO") is None

    def test_skips_empty_value(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("FOO=\n")
        assert _read_env_file(env, "FOO") is None


# ---------------------------------------------------------------------------
# Integration: Adapter used by other modules
# ---------------------------------------------------------------------------

class TestReadEnvFileEdgeCases:
    """Extended _read_env_file tests for bug-bash findings."""

    def test_inline_comment_stripped(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("API_KEY=sk-real # production key\n")
        assert _read_env_file(env, "API_KEY") == "sk-real"

    def test_quoted_value_with_inline_comment(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text('API_KEY="sk-quoted" # my key\n')
        assert _read_env_file(env, "API_KEY") == "sk-quoted"

    def test_single_quoted_value(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("API_KEY='sk-single'\n")
        assert _read_env_file(env, "API_KEY") == "sk-single"

    def test_hash_inside_quotes_preserved(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text('API_KEY="sk-has#hash"\n')
        assert _read_env_file(env, "API_KEY") == "sk-has#hash"

    def test_no_prefix_collision(self, tmp_path):
        """API_KEY should not match API_KEY_SECONDARY."""
        env = tmp_path / ".env"
        env.write_text("API_KEY_SECONDARY=wrong\nAPI_KEY=right\n")
        assert _read_env_file(env, "API_KEY") == "right"

    def test_whitespace_only_value(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("API_KEY=   \n")
        assert _read_env_file(env, "API_KEY") is None

    def test_no_trailing_newline(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("API_KEY=sk-no-newline")
        assert _read_env_file(env, "API_KEY") == "sk-no-newline"


class TestEmptyEnvVars:
    """Bug: empty string env vars caused Path('') → CWD."""

    def test_empty_quaid_home_uses_default(self, monkeypatch):
        monkeypatch.setenv("QUAID_HOME", "")
        adapter = StandaloneAdapter()
        assert adapter.quaid_home() == Path.home() / ".quaid"

    def test_whitespace_quaid_home_uses_default(self, monkeypatch):
        monkeypatch.setenv("QUAID_HOME", "   ")
        adapter = StandaloneAdapter()
        assert adapter.quaid_home() == Path.home() / ".quaid"

    def test_empty_openclaw_workspace_raises(self, monkeypatch, tmp_path):
        # OPENCLAW_WORKSPACE is no longer read by the OC adapter; the error
        # comes from missing openclaw.json when no workspace can be resolved.
        monkeypatch.setenv("OPENCLAW_WORKSPACE", "")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        adapter = OpenClawAdapter()
        with pytest.raises(RuntimeError, match="openclaw.json"):
            adapter.oc_workspace()

    def test_whitespace_openclaw_workspace_raises(self, monkeypatch, tmp_path):
        # Same as above — whitespace OPENCLAW_WORKSPACE is also ignored.
        monkeypatch.setenv("OPENCLAW_WORKSPACE", "   ")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        adapter = OpenClawAdapter()
        with pytest.raises(RuntimeError, match="openclaw.json"):
            adapter.oc_workspace()


class TestAdapterSelectionEdgeCases:
    @pytest.mark.adapter_openclaw
    def test_case_insensitive_openclaw(self, monkeypatch, tmp_path):
        (tmp_path / "config").mkdir(parents=True, exist_ok=True)
        (tmp_path / "config" / "config.json").write_text('{"adapter":"OpenClaw"}')
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("OPENCLAW_WORKSPACE", str(tmp_path))
        adapter = get_adapter()
        assert isinstance(adapter, OpenClawAdapter)

    def test_case_insensitive_standalone(self, monkeypatch, tmp_path):
        (tmp_path / "config").mkdir(parents=True, exist_ok=True)
        (tmp_path / "config" / "config.json").write_text('{"adapter":"STANDALONE"}')
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        adapter = get_adapter()
        assert isinstance(adapter, StandaloneAdapter)

    def test_invalid_adapter_raises(self, monkeypatch, tmp_path):
        """Invalid adapter config value should raise."""
        (tmp_path / "config").mkdir(parents=True, exist_ok=True)
        (tmp_path / "config" / "config.json").write_text('{"adapter":"invalid"}')
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        with pytest.raises(RuntimeError, match="Unsupported adapter type"):
            get_adapter()

    def test_manifest_runtime_loader_supports_third_party_module(self, monkeypatch, tmp_path):
        _write_adapter_config(tmp_path, "agentfoo")
        runtime_dir = tmp_path / "adapter_runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        (runtime_dir / "agentfoo_runtime.py").write_text(
            "\n".join(
                [
                    "from pathlib import Path",
                    "class AgentFooAdapter:",
                    "    def quaid_home(self): return Path('/tmp/agentfoo-home')",
                    "    def get_instance_name(self): return 'main'",
                    "    def notify(self, message, channel_override=None, dry_run=False, force=False): return True",
                    "    def get_last_channel(self, session_key=''): return None",
                    "    def get_api_key(self, env_var_name): return None",
                    "    def get_sessions_dir(self): return None",
                    "    def filter_system_messages(self, text): return False",
                    "    def get_llm_provider(self, model_tier=None): return object()",
                ]
            ),
            encoding="utf-8",
        )
        _write_adapter_manifest(
            tmp_path=tmp_path,
            adapter_id="agentfoo",
            module_name="agentfoo_runtime",
            class_name="AgentFooAdapter",
            runtime_path="../../adapter_runtime",
        )
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        adapter = get_adapter()
        assert type(adapter).__name__ == "AgentFooAdapter"

    def test_manifest_runtime_loader_rejects_missing_runtime_fields(self, monkeypatch, tmp_path):
        _write_adapter_config(tmp_path, "agentfoo")
        reg = tmp_path / "adaptors" / "agentfoo"
        reg.mkdir(parents=True, exist_ok=True)
        (reg / "adapter.json").write_text(
            json.dumps(
                {
                    "schema": "quaid-adapter-install/v1",
                    "id": "agentfoo",
                    "name": "agentfoo",
                    "install": {"selectLabel": "agentfoo"},
                    "runtime": {"python": {"module": "agentfoo_runtime"}},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        with pytest.raises(RuntimeError, match="runtime\\.python\\.module and runtime\\.python\\.class"):
            get_adapter()


@pytest.mark.adapter_openclaw
class TestKeychainFallback:
    def test_no_keychain_fallback(self, tmp_path, monkeypatch):
        """Keychain lookup was removed — env+file miss returns None."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("OPENCLAW_WORKSPACE", str(tmp_path))
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        adapter = OpenClawAdapter()
        result = adapter.get_api_key("ANTHROPIC_API_KEY")
        assert result is None

    def test_env_file_miss_returns_none(self, tmp_path, monkeypatch):
        """Missing env var + no .env file returns None."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("OPENCLAW_WORKSPACE", str(tmp_path))
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        adapter = OpenClawAdapter()
        result = adapter.get_api_key("OPENAI_API_KEY")
        assert result is None

class TestNotifyEdgeCases:
    def test_openclaw_host_info_parses_version_before_git_hash(self, monkeypatch):
        adapter = OpenClawAdapter()
        monkeypatch.setattr(adapter, "_resolve_message_cli", lambda: "/opt/homebrew/bin/openclaw")
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "OpenClaw 2026.4.15 (abc123)\n"
        mock_result.stderr = ""

        with patch("adaptors.openclaw.adapter.subprocess.run", return_value=mock_result) as mock_run:
            info = adapter.get_host_info()

        assert info.platform == "openclaw"
        assert info.version == "2026.4.15"
        assert info.binary_path == "/opt/homebrew/bin/openclaw"
        assert mock_run.call_args.kwargs["env"]["PATH"].startswith("/opt/homebrew/bin:")

    def test_openclaw_host_info_falls_back_to_package_json_when_cli_fails(self, tmp_path, monkeypatch):
        package_dir = tmp_path / "openclaw"
        package_dir.mkdir()
        binary = package_dir / "openclaw.mjs"
        binary.write_text("#!/usr/bin/env node\n", encoding="utf-8")
        (package_dir / "package.json").write_text(
            json.dumps({"name": "openclaw", "version": "2026.4.15"}),
            encoding="utf-8",
        )

        adapter = OpenClawAdapter()
        monkeypatch.setattr(adapter, "_resolve_message_cli", lambda: str(binary))
        mock_result = MagicMock()
        mock_result.returncode = 127
        mock_result.stdout = ""
        mock_result.stderr = "env: node: No such file or directory\n"

        with patch("adaptors.openclaw.adapter.subprocess.run", return_value=mock_result):
            info = adapter.get_host_info()

        assert info.version == "2026.4.15"

    def test_notify_cli_not_found(self, monkeypatch):
        """notify() returns False when no message CLI is available."""
        adapter = OpenClawAdapter()
        mock_info = ChannelInfo(
            channel="telegram", target="123", account_id="default",
            session_key="agent:main:main"
        )
        monkeypatch.setattr(adapter, "get_last_channel", lambda s="": mock_info)
        monkeypatch.setattr(adapter, "_resolve_message_cli", lambda: None)
        with patch("adaptors.openclaw.adapter.subprocess.run", side_effect=FileNotFoundError):
            result = adapter.notify("test")
            assert result is False

    def test_notify_channel_override(self, monkeypatch):
        """channel_override replaces the session's channel in the command."""
        adapter = OpenClawAdapter()
        mock_info = ChannelInfo(
            channel="telegram", target="123", account_id="default",
            session_key="agent:main:main"
        )
        monkeypatch.setattr(adapter, "get_last_channel", lambda s="": mock_info)
        monkeypatch.setattr(adapter, "_resolve_message_cli", lambda: "openclaw")
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("adaptors.openclaw.adapter.subprocess.run", return_value=mock_result) as mock_run:
            adapter.notify("test", channel_override="discord")
            cmd = mock_run.call_args[0][0]
            assert "--channel" in cmd
            idx = cmd.index("--channel")
            assert cmd[idx + 1] == "discord"

    def test_notify_skips_non_routable_webchat_channel(self, monkeypatch, capsys):
        adapter = OpenClawAdapter()
        mock_info = ChannelInfo(
            channel="webchat", target="thread:abc", account_id="default",
            session_key="agent:main:main"
        )
        monkeypatch.setattr(adapter, "get_last_channel", lambda s="": mock_info)
        monkeypatch.setattr(adapter, "_resolve_message_cli", lambda: "openclaw")
        with patch("adaptors.openclaw.adapter.subprocess.run") as mock_run:
            result = adapter.notify("test")
        assert result is False
        assert "Channel not routable via message CLI: webchat" in capsys.readouterr().err
        mock_run.assert_not_called()

    def test_notify_channel_override_resolves_recent_route_for_channel(self, tmp_path, monkeypatch):
        import json
        sessions_file = tmp_path / "sessions.json"
        sessions_file.write_text(json.dumps({
            "agent:main:main": {
                "lastTo": "heartbeat",
                "updatedAt": 100,
            },
            "agent:main:telegram:direct:1000000000": {
                "lastChannel": "telegram",
                "lastTo": "telegram:1000000000",
                "lastAccountId": "default",
                "updatedAt": 200,
            },
        }))
        monkeypatch.setattr(OpenClawAdapter, "_find_sessions_json", lambda self: sessions_file)
        adapter = OpenClawAdapter()
        monkeypatch.setattr(adapter, "_resolve_message_cli", lambda: "openclaw")
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("adaptors.openclaw.adapter.subprocess.run", return_value=mock_result) as mock_run:
            result = adapter.notify("test", channel_override="telegram")
            assert result is True
            cmd = mock_run.call_args[0][0]
            channel_idx = cmd.index("--channel")
            target_idx = cmd.index("--target")
            assert cmd[channel_idx + 1] == "telegram"
            assert cmd[target_idx + 1] == "telegram:1000000000"

    def test_notify_non_default_account(self, monkeypatch):
        """Non-default account_id adds --account flag."""
        adapter = OpenClawAdapter()
        mock_info = ChannelInfo(
            channel="telegram", target="123", account_id="work",
            session_key="agent:main:main"
        )
        monkeypatch.setattr(adapter, "get_last_channel", lambda s="": mock_info)
        monkeypatch.setattr(adapter, "_resolve_message_cli", lambda: "openclaw")
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("adaptors.openclaw.adapter.subprocess.run", return_value=mock_result) as mock_run:
            adapter.notify("test")
            cmd = mock_run.call_args[0][0]
            assert "--account" in cmd
            idx = cmd.index("--account")
            assert cmd[idx + 1] == "work"

    def test_notify_empty_account_no_flag(self, monkeypatch):
        """Empty account_id does NOT add --account flag."""
        adapter = OpenClawAdapter()
        mock_info = ChannelInfo(
            channel="telegram", target="123", account_id="",
            session_key="agent:main:main"
        )
        monkeypatch.setattr(adapter, "get_last_channel", lambda s="": mock_info)
        monkeypatch.setattr(adapter, "_resolve_message_cli", lambda: "openclaw")
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("adaptors.openclaw.adapter.subprocess.run", return_value=mock_result) as mock_run:
            adapter.notify("test")
            cmd = mock_run.call_args[0][0]
            assert "--account" not in cmd

    def test_notify_uses_env_with_homebrew_path_for_cli_runtime(self, monkeypatch):
        """notify subprocess env should include Homebrew path for node-backed CLI."""
        adapter = OpenClawAdapter()
        mock_info = ChannelInfo(
            channel="matrix", target="!room:localhost", account_id="default",
            session_key="agent:main:main"
        )
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setattr(adapter, "get_last_channel", lambda s="": mock_info)
        monkeypatch.setattr(adapter, "_resolve_message_cli", lambda: "openclaw")
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("adaptors.openclaw.adapter.subprocess.run", return_value=mock_result) as mock_run:
            result = adapter.notify("test")

        assert result is True
        call_env = mock_run.call_args.kwargs.get("env", {})
        call_path = str(call_env.get("PATH") or "")
        assert call_path.startswith("/opt/homebrew/bin")
        assert "/usr/bin" in call_path


class TestSessionsEdgeCases:
    def test_corrupt_sessions_json(self, monkeypatch, tmp_path):
        """Corrupt sessions.json returns None, no crash."""
        sessions_file = tmp_path / "sessions.json"
        sessions_file.write_text("{broken json")
        adapter = OpenClawAdapter()
        monkeypatch.setattr(adapter, "_find_sessions_json", lambda: sessions_file)
        with patch("adaptors.openclaw.adapter.is_fail_hard_enabled", return_value=False):
            assert adapter.get_last_channel() is None

    def test_corrupt_sessions_json_raises_when_fail_hard(self, monkeypatch, tmp_path):
        sessions_file = tmp_path / "sessions.json"
        sessions_file.write_text("{broken json")
        adapter = OpenClawAdapter()
        monkeypatch.setattr(adapter, "_find_sessions_json", lambda: sessions_file)

        with patch("adaptors.openclaw.adapter.is_fail_hard_enabled", return_value=True):
            with pytest.raises(json.JSONDecodeError):
                adapter.get_last_channel()

    def test_empty_sessions_json(self, monkeypatch, tmp_path):
        """Empty sessions.json returns None."""
        sessions_file = tmp_path / "sessions.json"
        sessions_file.write_text("")
        adapter = OpenClawAdapter()
        monkeypatch.setattr(adapter, "_find_sessions_json", lambda: sessions_file)
        with patch("adaptors.openclaw.adapter.is_fail_hard_enabled", return_value=False):
            assert adapter.get_last_channel() is None

    def test_sessions_missing_channel(self, monkeypatch, tmp_path):
        """Session without lastChannel returns None."""
        import json
        sessions_file = tmp_path / "sessions.json"
        sessions_file.write_text(json.dumps({
            "agent:main:main": {"lastTo": "123"}
        }))
        adapter = OpenClawAdapter()
        monkeypatch.setattr(adapter, "_find_sessions_json", lambda: sessions_file)
        assert adapter.get_last_channel() is None

    def test_find_sessions_json_openclaw_path(self, monkeypatch, tmp_path):
        """OpenClaw sessions path is used when present."""
        openclaw_dir = tmp_path / ".openclaw" / "agents" / "main" / "sessions"
        openclaw_dir.mkdir(parents=True)
        (openclaw_dir / "sessions.json").write_text("{}")

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        adapter = OpenClawAdapter()
        result = adapter._find_sessions_json()
        assert result is not None
        assert ".openclaw" in str(result)

    def test_find_sessions_json_honors_openclaw_config_path(self, monkeypatch, tmp_path):
        """OPENCLAW_CONFIG_PATH reroots sessions lookup."""
        home = tmp_path / "home"
        cfg_dir = home / "alt-oc"
        sessions_dir = cfg_dir / "agents" / "main" / "sessions"
        sessions_dir.mkdir(parents=True)
        (sessions_dir / "sessions.json").write_text("{}")
        cfg_path = cfg_dir / "openclaw.json"
        cfg_path.write_text("{}")
        monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(cfg_path))
        monkeypatch.setattr(Path, "home", lambda: home)
        adapter = OpenClawAdapter()
        assert adapter._find_sessions_json() == sessions_dir / "sessions.json"

    def test_find_sessions_json_both_missing(self, monkeypatch, tmp_path):
        """Both candidate paths missing returns None."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        adapter = OpenClawAdapter()
        assert adapter._find_sessions_json() is None


@pytest.mark.adapter_openclaw
class TestGatewayConfigPath:
    def test_returns_none_when_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        adapter = OpenClawAdapter()
        assert adapter.get_gateway_config_path() is None

    def test_returns_path_when_exists(self, monkeypatch, tmp_path):
        config_path = tmp_path / ".openclaw" / "openclaw.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("{}")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        adapter = OpenClawAdapter()
        assert adapter.get_gateway_config_path() == config_path

    def test_honors_openclaw_config_path_env(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        cfg_path = home / "oc-alt" / "openclaw.json"
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text("{}")
        monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(cfg_path))
        monkeypatch.setattr(Path, "home", lambda: home)
        adapter = OpenClawAdapter()
        assert adapter.get_gateway_config_path() == cfg_path

    def test_rejects_openclaw_config_path_outside_home(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        cfg_path = tmp_path / "outside" / "openclaw.json"
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text("{}")
        monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(cfg_path))
        monkeypatch.setattr(Path, "home", lambda: home)
        adapter = OpenClawAdapter()

        with patch("adaptors.openclaw.adapter.is_fail_hard_enabled", return_value=False):
            assert adapter.get_gateway_config_path() is None
        with patch("adaptors.openclaw.adapter.is_fail_hard_enabled", return_value=True):
            with pytest.raises(PermissionError, match="OPENCLAW_CONFIG_PATH outside home"):
                adapter.get_gateway_config_path()


class TestProviderFactoryMethods:
    """Test get_llm_provider() / get_embeddings_provider() on adapters."""

    def test_standalone_returns_anthropic_provider(self, standalone, monkeypatch):
        """StandaloneAdapter.get_llm_provider() returns AnthropicLLMProvider."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        with patch("config.get_config") as mock_cfg:
            mock_cfg.return_value.models.llm_provider = "anthropic"
            llm = standalone.get_llm_provider()
        assert isinstance(llm, AnthropicLLMProvider)

    def test_standalone_explicit_claude_code_provider(self, standalone, monkeypatch):
        """StandaloneAdapter uses ClaudeCodeLLMProvider when config says claude-code."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with patch("config.get_config") as mock_cfg:
            mock_cfg.return_value.models.llm_provider = "claude-code"
            mock_cfg.return_value.models.deep_reasoning = "claude-opus-4-5"
            mock_cfg.return_value.models.fast_reasoning = "claude-haiku-4-5"
            llm = standalone.get_llm_provider()
        assert isinstance(llm, ClaudeCodeLLMProvider)
        assert llm._deep_model == "claude-opus-4-5"
        assert llm._fast_model == "claude-haiku-4-5"

    def test_standalone_respects_tier_provider_overrides(self, standalone, monkeypatch):
        """StandaloneAdapter routes provider selection by model tier when configured."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        cfg = SimpleNamespace(
            models=SimpleNamespace(
                llm_provider="anthropic",
                fast_reasoning_provider="claude-code",
                deep_reasoning_provider="anthropic",
            )
        )
        with patch("config.get_config", return_value=cfg):
            fast_llm = standalone.get_llm_provider(model_tier="fast")
            deep_llm = standalone.get_llm_provider(model_tier="deep")
        assert isinstance(fast_llm, ClaudeCodeLLMProvider)
        assert isinstance(deep_llm, AnthropicLLMProvider)

    def test_standalone_openai_compatible_defaults_to_openai_api_with_key(self, standalone, monkeypatch):
        """OpenAI-compatible provider should use api.openai.com when a key exists but no base URL is set."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
        monkeypatch.delenv("OPENAI_COMPATIBLE_BASE_URL", raising=False)
        cfg = SimpleNamespace(
            models=SimpleNamespace(
                llm_provider="openai-compatible",
                fast_reasoning_provider="default",
                deep_reasoning_provider="default",
                deep_reasoning="gpt-4o-mini",
                fast_reasoning="gpt-4o-mini",
                deep_reasoning_effort="high",
                fast_reasoning_effort="none",
                deep_reasoning_model_classes={},
                fast_reasoning_model_classes={},
                base_url="",
                api_key_env="OPENAI_API_KEY",
            )
        )
        with patch("config.get_config", return_value=cfg):
            llm = standalone.get_llm_provider()
        assert isinstance(llm, OpenAICompatibleLLMProvider)
        assert llm._base_url == "https://api.openai.com"
        assert llm._deep_reasoning_effort == "high"
        assert llm._fast_reasoning_effort == "none"

    def test_standalone_raises_without_any_provider(self, standalone, monkeypatch):
        """StandaloneAdapter raises when config requires anthropic but no key."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with patch("config.get_config") as mock_cfg:
            mock_cfg.return_value.models.llm_provider = "anthropic"
            with pytest.raises(RuntimeError, match="LLM provider is 'anthropic'"):
                standalone.get_llm_provider()

    def test_standalone_explicit_anthropic_raises_without_key(self, standalone, monkeypatch):
        """StandaloneAdapter raises when config says anthropic but no key."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with patch("config.get_config") as mock_cfg:
            mock_cfg.return_value.models.llm_provider = "anthropic"
            with pytest.raises(RuntimeError, match="LLM provider is 'anthropic'"):
                standalone.get_llm_provider()

    def test_standalone_embeddings_returns_none(self, standalone):
        """StandaloneAdapter has no built-in embeddings provider."""
        assert standalone.get_embeddings_provider() is None

    @pytest.mark.adapter_openclaw
    def test_openclaw_returns_direct_provider(self, openclaw_adapter, monkeypatch):
        """OpenClawAdapter.get_llm_provider() returns direct provider clients."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        llm = openclaw_adapter.get_llm_provider()
        assert isinstance(llm, (AnthropicLLMProvider, OpenAICodexOAuthLLMProvider))

    @pytest.mark.adapter_openclaw
    def test_openclaw_embeddings_returns_none(self, openclaw_adapter):
        """OpenClawAdapter has no built-in embeddings provider (yet)."""
        assert openclaw_adapter.get_embeddings_provider() is None

    def test_test_adapter_returns_test_provider(self, tmp_path):
        """TestAdapter.get_llm_provider() returns TestLLMProvider."""
        adapter = TestAdapter(tmp_path)
        set_adapter(adapter)
        llm = adapter.get_llm_provider()
        assert isinstance(llm, TestLLMProvider)

    def test_test_adapter_records_calls(self, tmp_path):
        """TestAdapter exposes llm_calls from its TestLLMProvider."""
        adapter = TestAdapter(tmp_path)
        set_adapter(adapter)
        llm = adapter.get_llm_provider()
        llm.llm_call([{"role": "user", "content": "hello"}])
        assert len(adapter.llm_calls) == 1
        assert adapter.llm_calls[0]["messages"][0]["content"] == "hello"

    def test_test_adapter_custom_responses(self, tmp_path):
        """TestAdapter supports custom canned responses per tier."""
        adapter = TestAdapter(tmp_path, responses={"fast": "custom-low"})
        set_adapter(adapter)
        llm = adapter.get_llm_provider()
        result = llm.llm_call([{"role": "user", "content": "test"}], model_tier="fast")
        assert result.text == "custom-low"

    @pytest.mark.adapter_openclaw
    def test_openclaw_discover_llm_providers_default(self, openclaw_adapter, monkeypatch):
        """discover_llm_providers() returns at least the default provider."""
        monkeypatch.setattr(openclaw_adapter, "get_gateway_config_path", lambda: None)
        providers = openclaw_adapter.discover_llm_providers()
        assert len(providers) >= 1
        assert providers[0]["id"] == "default"

    @pytest.mark.adapter_openclaw
    def test_openclaw_discover_llm_providers_with_profiles(self, openclaw_adapter, tmp_path, monkeypatch):
        """discover_llm_providers() reads auth profiles from openclaw.json."""
        import json
        config = {
            "auth": {
                "profiles": {
                    "anthropic-oauth": {
                        "provider": "anthropic",
                        "mode": "oauth",
                    }
                }
            }
        }
        config_path = tmp_path / "openclaw.json"
        config_path.write_text(json.dumps(config))
        monkeypatch.setattr(openclaw_adapter, "get_gateway_config_path", lambda: config_path)
        providers = openclaw_adapter.discover_llm_providers()
        assert len(providers) == 2  # default + anthropic-oauth
        assert providers[1]["id"] == "anthropic-oauth"
        assert providers[1]["provider"] == "anthropic"

    @pytest.mark.adapter_openclaw
    def test_openclaw_discover_llm_providers_raises_bad_config_when_fail_hard(self, openclaw_adapter, tmp_path, monkeypatch):
        config_path = tmp_path / "openclaw.json"
        config_path.write_text("{bad json", encoding="utf-8")
        monkeypatch.setattr(openclaw_adapter, "get_gateway_config_path", lambda: config_path)

        with patch("adaptors.openclaw.adapter.is_fail_hard_enabled", return_value=True):
            with pytest.raises(json.JSONDecodeError):
                openclaw_adapter.discover_llm_providers()

    @pytest.mark.adapter_openclaw
    def test_get_gateway_auth_raises_bad_config_when_fail_hard(self, openclaw_adapter, tmp_path, monkeypatch):
        config_path = tmp_path / "openclaw.json"
        config_path.write_text("{bad json", encoding="utf-8")
        monkeypatch.setattr(openclaw_adapter, "get_gateway_config_path", lambda: config_path)

        with patch("adaptors.openclaw.adapter.is_fail_hard_enabled", return_value=True):
            with pytest.raises(json.JSONDecodeError):
                openclaw_adapter._get_gateway_auth()


@pytest.mark.adapter_openclaw
class TestResolveAnthropicCredential:
    """OpenClawAdapter._resolve_anthropic_credential() resolution chain."""

    def _make_adapter(self, tmp_path, monkeypatch):
        """Create an OpenClawAdapter with agent config dir pointed at tmp_path."""
        monkeypatch.setenv("OPENCLAW_WORKSPACE", str(tmp_path))
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        adapter = OpenClawAdapter()
        # Point agent config dir to a fake location so we don't read real creds
        fake_agent_dir = tmp_path / "fake_agent"
        fake_agent_dir.mkdir()
        monkeypatch.setattr(adapter, "_get_agent_config_dir", lambda: fake_agent_dir)
        set_adapter(adapter)
        return adapter, fake_agent_dir

    def test_reads_auth_profiles_last_good(self, tmp_path, monkeypatch):
        """Prefers lastGood profile from auth-profiles.json."""
        adapter, agent_dir = self._make_adapter(tmp_path, monkeypatch)
        import json
        profiles = {
            "version": 1,
            "profiles": {
                "anthropic:manual": {
                    "type": "token",
                    "provider": "anthropic",
                    "token": "sk-ant-oat01-test-oauth-token",
                }
            },
            "lastGood": {"anthropic": "anthropic:manual"},
        }
        (agent_dir / "auth-profiles.json").write_text(json.dumps(profiles))
        cred = adapter._resolve_anthropic_credential()
        assert cred == "sk-ant-oat01-test-oauth-token"

    def test_no_profile_fallback_when_last_good_missing(self, tmp_path, monkeypatch):
        """Does not scan arbitrary profiles when lastGood is missing."""
        adapter, agent_dir = self._make_adapter(tmp_path, monkeypatch)
        import json
        profiles = {
            "version": 1,
            "profiles": {
                "anthropic:default": {
                    "type": "api_key",
                    "provider": "anthropic",
                    "key": "sk-ant-api-fallback-key",
                }
            },
            "lastGood": {},
        }
        (agent_dir / "auth-profiles.json").write_text(json.dumps(profiles))
        cred = adapter._resolve_anthropic_credential()
        assert cred is None

    def test_does_not_fall_through_to_env_var(self, tmp_path, monkeypatch):
        """Does not fall through to ANTHROPIC_API_KEY env var."""
        adapter, _ = self._make_adapter(tmp_path, monkeypatch)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-from-env")
        cred = adapter._resolve_anthropic_credential()
        assert cred is None

    def test_does_not_fall_through_to_dotenv(self, tmp_path, monkeypatch):
        """Does not fall through to .env file when gateway auth is missing."""
        adapter, _ = self._make_adapter(tmp_path, monkeypatch)
        (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-test-from-dotenv\n")
        cred = adapter._resolve_anthropic_credential()
        assert cred is None

    def test_returns_none_when_nothing_found(self, tmp_path, monkeypatch):
        """Returns None when no credentials found anywhere."""
        adapter, _ = self._make_adapter(tmp_path, monkeypatch)
        cred = adapter._resolve_anthropic_credential()
        assert cred is None

    def test_raises_bad_auth_profiles_when_fail_hard(self, tmp_path, monkeypatch):
        adapter, agent_dir = self._make_adapter(tmp_path, monkeypatch)
        (agent_dir / "auth-profiles.json").write_text("{bad json", encoding="utf-8")

        with patch("adaptors.openclaw.adapter.is_fail_hard_enabled", return_value=True):
            with pytest.raises(json.JSONDecodeError):
                adapter._resolve_anthropic_credential()

    def test_profiles_take_priority_when_env_present(self, tmp_path, monkeypatch):
        """Gateway-auth profile still wins when env var is present."""
        adapter, agent_dir = self._make_adapter(tmp_path, monkeypatch)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-from-env")
        import json
        profiles = {
            "version": 1,
            "profiles": {
                "anthropic:oauth": {
                    "type": "token",
                    "provider": "anthropic",
                    "token": "sk-ant-oat01-from-profiles",
                }
            },
            "lastGood": {"anthropic": "anthropic:oauth"},
        }
        (agent_dir / "auth-profiles.json").write_text(json.dumps(profiles))
        cred = adapter._resolve_anthropic_credential()
        assert cred == "sk-ant-oat01-from-profiles"

    def test_get_api_key_anthropic_does_not_fall_through_to_codex_shared_token(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("OPENCLAW_WORKSPACE", str(tmp_path))
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        adapter = OpenClawAdapter()
        adapter.store_shared_auth_token("codex_oauth", "eyJ-openai-jwt")
        fake_agent_dir = tmp_path / "fake_agent"
        fake_agent_dir.mkdir()
        monkeypatch.setattr(adapter, "_get_agent_config_dir", lambda: fake_agent_dir)
        assert adapter.get_api_key("ANTHROPIC_API_KEY") is None

    def test_get_api_key_anthropic_reads_gateway_last_good_profile(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("OPENCLAW_WORKSPACE", str(tmp_path))
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        adapter = OpenClawAdapter()
        fake_agent_dir = tmp_path / "fake_agent"
        fake_agent_dir.mkdir()
        monkeypatch.setattr(adapter, "_get_agent_config_dir", lambda: fake_agent_dir)
        (fake_agent_dir / "auth-profiles.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "profiles": {
                        "anthropic:oauth": {
                            "provider": "anthropic",
                            "token": "sk-ant-oat01-gateway-token",
                        }
                    },
                    "lastGood": {"anthropic": "anthropic:oauth"},
                }
            ),
            encoding="utf-8",
        )
        assert adapter.get_api_key("ANTHROPIC_API_KEY") == "sk-ant-oat01-gateway-token"


class TestResetAdapterClearsProviders:
    """reset_adapter() should clear the embeddings provider cache."""

    def test_reset_clears_embeddings_provider(self, tmp_path, monkeypatch):
        from lib.embeddings import get_embeddings_provider, set_embeddings_provider
        from lib.providers import MockEmbeddingsProvider

        mock = MockEmbeddingsProvider()
        set_embeddings_provider(mock)
        assert get_embeddings_provider() is mock

        monkeypatch.setenv("MOCK_EMBEDDINGS", "1")
        reset_adapter()
        # After reset, provider should be re-resolved (not our original mock)
        p2 = get_embeddings_provider()
        assert p2 is not mock


class TestLogRotation:
    """Bug: rotate_logs() failed silently because archive dir was never created."""

    def test_rotate_creates_archive_dir(self, standalone, tmp_path):
        """rotate_logs() creates archive/ dir if it doesn't exist."""
        from core.runtime.logger import rotate_logs, _log_dir, _archive_dir

        # Create a log file at the adapter's logs_dir (instance_root/logs)
        log_dir = standalone.logs_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "test.log"
        log_file.write_text("test entry\n")

        # Archive dir should not exist yet
        archive_dir = log_dir / "archive"
        assert not archive_dir.exists()

        rotate_logs()

        # Archive dir should now exist
        assert archive_dir.exists()

    def test_rotate_moves_log_to_archive(self, standalone, tmp_path):
        """rotate_logs() actually moves logs into archive/."""
        from core.runtime.logger import rotate_logs
        from datetime import datetime, timezone

        log_dir = standalone.logs_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "test.log"
        log_file.write_text("test entry\n")

        rotate_logs()

        # Original log should be gone or empty
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        archive_file = log_dir / "archive" / f"test.{today}.log"
        assert archive_file.exists()
        assert "test entry" in archive_file.read_text()


class TestAdapterIntegration:
    def test_lib_config_uses_adapter(self, standalone, tmp_path):
        """lib/config.py should resolve paths through the adapter."""
        # Create minimal config
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "memory.db").touch()

        from lib.config import _workspace_root
        assert _workspace_root() == standalone.instance_root()

    def test_config_paths_use_adapter(self, standalone, tmp_path):
        """config.py should search for config in adapter-relative paths."""
        from config import _config_paths, reload_config
        paths = _config_paths()
        assert paths[0] == standalone.config_dir() / "config.json"

    def test_notify_delegates_through_adapter(self, standalone, capsys):
        """notify.py should route through adapter.notify()."""
        from core.runtime.notify import notify_user
        # StandaloneAdapter prints to stderr
        notify_user("adapter test")
        captured = capsys.readouterr()
        assert "adapter test" in captured.err
