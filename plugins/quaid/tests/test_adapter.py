"""Tests for lib/adapter.py — platform adapter layer."""

import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Ensure plugin root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.adapter import (
    QuaidAdapter,
    StandaloneAdapter,
    TestAdapter,
    ChannelInfo,
    get_adapter,
    set_adapter,
    reset_adapter,
    _read_env_file,
)
from lib.providers import (
    AnthropicLLMProvider,
    ClaudeCodeLLMProvider,
    TestLLMProvider,
)
from adaptors.openclaw.adapter import OpenClawAdapter
from adaptors.openclaw.providers import GatewayLLMProvider


def _write_adapter_config(tmp_path: Path, adapter_type: str) -> None:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "memory.json").write_text(f'{{"adapter": {{"type": "{adapter_type}"}}}}')


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
def standalone(tmp_path):
    """Create a StandaloneAdapter with a temp home dir."""
    adapter = StandaloneAdapter(home=tmp_path)
    set_adapter(adapter)
    return adapter


@pytest.fixture
def openclaw_adapter(tmp_path, monkeypatch):
    """Create an OpenClawAdapter with a test API key."""
    monkeypatch.setenv("CLAWDBOT_WORKSPACE", str(tmp_path))
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
        assert adapter.quaid_home() == Path.home() / "quaid"

    def test_quaid_home_env_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        adapter = StandaloneAdapter()
        assert adapter.quaid_home() == tmp_path

    def test_quaid_home_explicit(self, tmp_path):
        adapter = StandaloneAdapter(home=tmp_path)
        assert adapter.quaid_home() == tmp_path

    def test_data_dir(self, standalone, tmp_path):
        assert standalone.data_dir() == tmp_path / "data"

    def test_config_dir(self, standalone, tmp_path):
        assert standalone.config_dir() == tmp_path / "config"

    def test_logs_dir(self, standalone, tmp_path):
        assert standalone.logs_dir() == tmp_path / "logs"

    def test_journal_dir(self, standalone, tmp_path):
        assert standalone.journal_dir() == tmp_path / "journal"

    def test_projects_dir(self, standalone, tmp_path):
        assert standalone.projects_dir() == tmp_path / "projects"

    def test_core_markdown_dir(self, standalone, tmp_path):
        assert standalone.core_markdown_dir() == tmp_path

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
        assert standalone.get_api_key("MY_KEY") == "sk-from-file"

    def test_get_api_key_env_file_with_quotes(self, standalone, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text('MY_KEY="sk-quoted"\n')
        assert standalone.get_api_key("MY_KEY") == "sk-quoted"

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
        assert standalone.get_repo_slug() == "steadman-labs/quaid"

    def test_install_url(self, standalone):
        url = standalone.get_install_url()
        assert "steadman-labs/quaid" in url
        assert "install.sh" in url


# ---------------------------------------------------------------------------
# OpenClawAdapter Tests
# ---------------------------------------------------------------------------

@pytest.mark.adapter_openclaw
class TestOpenClawAdapter:
    def test_quaid_home_raises_without_env(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CLAWDBOT_WORKSPACE", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        adapter = OpenClawAdapter()
        with pytest.raises(RuntimeError, match="CLAWDBOT_WORKSPACE"):
            adapter.quaid_home()

    def test_quaid_home_env_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAWDBOT_WORKSPACE", str(tmp_path))
        adapter = OpenClawAdapter()
        assert adapter.quaid_home() == tmp_path

    def test_quaid_home_fallback_to_clawdbot_json(self, tmp_path, monkeypatch):
        """When CLAWDBOT_WORKSPACE unset, falls back to ~/.openclaw/clawdbot.json."""
        monkeypatch.delenv("CLAWDBOT_WORKSPACE", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        workspace = tmp_path / "my-workspace"
        workspace.mkdir()
        cfg_dir = tmp_path / ".openclaw"
        cfg_dir.mkdir()
        import json
        (cfg_dir / "clawdbot.json").write_text(json.dumps({
            "agents": {"defaults": {"workspace": str(workspace)}}
        }))
        adapter = OpenClawAdapter()
        assert adapter.quaid_home() == workspace

    def test_quaid_home_prefers_openclaw_json_over_legacy(self, tmp_path, monkeypatch):
        """If both config filenames exist, prefer openclaw.json."""
        monkeypatch.delenv("CLAWDBOT_WORKSPACE", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        ws_new = tmp_path / "workspace-openclaw"
        ws_old = tmp_path / "workspace-legacy"
        ws_new.mkdir()
        ws_old.mkdir()

        cfg_dir = tmp_path / ".openclaw"
        cfg_dir.mkdir()

        import json
        (cfg_dir / "openclaw.json").write_text(json.dumps({
            "agents": {"defaults": {"workspace": str(ws_new)}}
        }))
        (cfg_dir / "clawdbot.json").write_text(json.dumps({
            "agents": {"defaults": {"workspace": str(ws_old)}}
        }))

        adapter = OpenClawAdapter()
        assert adapter.quaid_home() == ws_new

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
        assert adapter.filter_system_messages("What about HEARTBEAT mechanisms?") is False

    def test_get_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env-key")
        adapter = OpenClawAdapter()
        assert adapter.get_api_key("ANTHROPIC_API_KEY") == "sk-env-key"

    def test_get_api_key_from_env_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAWDBOT_WORKSPACE", str(tmp_path))
        monkeypatch.delenv("TEST_KEY", raising=False)
        (tmp_path / ".env").write_text("TEST_KEY=sk-from-ws-env\n")
        adapter = OpenClawAdapter()
        assert adapter.get_api_key("TEST_KEY") == "sk-from-ws-env"

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

    def test_get_sessions_dir(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / ".openclaw" / "sessions"
        sessions_dir.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        adapter = OpenClawAdapter()
        assert adapter.get_sessions_dir() == sessions_dir

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

    def test_notify_delegates_to_clawdbot(self, monkeypatch):
        """Verify notify calls clawdbot CLI."""
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
        with patch("adaptors.openclaw.adapter.subprocess.run", return_value=mock_result) as mock_run:
            result = adapter.notify("test message")
            assert result is True
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            assert "clawdbot" in cmd
            assert "test message" in cmd


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
        monkeypatch.setenv("CLAWDBOT_WORKSPACE", str(tmp_path))
        adapter = get_adapter()
        assert isinstance(adapter, OpenClawAdapter)

    def test_missing_adapter_raises(self, monkeypatch):
        monkeypatch.delenv("QUAID_HOME", raising=False)
        monkeypatch.delenv("CLAWDBOT_WORKSPACE", raising=False)
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
        assert adapter.quaid_home() == Path.home() / "quaid"

    def test_whitespace_quaid_home_uses_default(self, monkeypatch):
        monkeypatch.setenv("QUAID_HOME", "   ")
        adapter = StandaloneAdapter()
        assert adapter.quaid_home() == Path.home() / "quaid"

    def test_empty_clawdbot_workspace_raises(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CLAWDBOT_WORKSPACE", "")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        adapter = OpenClawAdapter()
        with pytest.raises(RuntimeError, match="CLAWDBOT_WORKSPACE"):
            adapter.quaid_home()

    def test_whitespace_clawdbot_workspace_raises(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CLAWDBOT_WORKSPACE", "   ")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        adapter = OpenClawAdapter()
        with pytest.raises(RuntimeError, match="CLAWDBOT_WORKSPACE"):
            adapter.quaid_home()


class TestAdapterSelectionEdgeCases:
    @pytest.mark.adapter_openclaw
    def test_case_insensitive_openclaw(self, monkeypatch, tmp_path):
        (tmp_path / "config").mkdir(parents=True, exist_ok=True)
        (tmp_path / "config" / "memory.json").write_text('{"adapter":"OpenClaw"}')
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("CLAWDBOT_WORKSPACE", str(tmp_path))
        adapter = get_adapter()
        assert isinstance(adapter, OpenClawAdapter)

    def test_case_insensitive_standalone(self, monkeypatch, tmp_path):
        (tmp_path / "config").mkdir(parents=True, exist_ok=True)
        (tmp_path / "config" / "memory.json").write_text('{"adapter":"STANDALONE"}')
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        adapter = get_adapter()
        assert isinstance(adapter, StandaloneAdapter)

    def test_invalid_adapter_raises(self, monkeypatch, tmp_path):
        """Invalid adapter config value should raise."""
        (tmp_path / "config").mkdir(parents=True, exist_ok=True)
        (tmp_path / "config" / "memory.json").write_text('{"adapter":"invalid"}')
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        with pytest.raises(RuntimeError, match="must set adapter type"):
            get_adapter()


@pytest.mark.adapter_openclaw
class TestKeychainFallback:
    def test_no_keychain_fallback(self, tmp_path, monkeypatch):
        """Keychain lookup was removed — env+file miss returns None."""
        monkeypatch.setenv("CLAWDBOT_WORKSPACE", str(tmp_path))
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        adapter = OpenClawAdapter()
        result = adapter.get_api_key("ANTHROPIC_API_KEY")
        assert result is None

    def test_env_file_miss_returns_none(self, tmp_path, monkeypatch):
        """Missing env var + no .env file returns None."""
        monkeypatch.setenv("CLAWDBOT_WORKSPACE", str(tmp_path))
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        adapter = OpenClawAdapter()
        result = adapter.get_api_key("OPENAI_API_KEY")
        assert result is None

    def test_keychain_lookup_is_stub(self):
        """_keychain_lookup is a stub that always returns None."""
        adapter = OpenClawAdapter()
        assert adapter._keychain_lookup("any-service", "any-account") is None


class TestNotifyEdgeCases:
    def test_notify_clawdbot_not_found(self, monkeypatch):
        """notify() returns False when clawdbot binary is missing."""
        adapter = OpenClawAdapter()
        mock_info = ChannelInfo(
            channel="telegram", target="123", account_id="default",
            session_key="agent:main:main"
        )
        monkeypatch.setattr(adapter, "get_last_channel", lambda s="": mock_info)
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
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("adaptors.openclaw.adapter.subprocess.run", return_value=mock_result) as mock_run:
            adapter.notify("test", channel_override="discord")
            cmd = mock_run.call_args[0][0]
            assert "--channel" in cmd
            idx = cmd.index("--channel")
            assert cmd[idx + 1] == "discord"

    def test_notify_non_default_account(self, monkeypatch):
        """Non-default account_id adds --account flag."""
        adapter = OpenClawAdapter()
        mock_info = ChannelInfo(
            channel="telegram", target="123", account_id="work",
            session_key="agent:main:main"
        )
        monkeypatch.setattr(adapter, "get_last_channel", lambda s="": mock_info)
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
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("adaptors.openclaw.adapter.subprocess.run", return_value=mock_result) as mock_run:
            adapter.notify("test")
            cmd = mock_run.call_args[0][0]
            assert "--account" not in cmd


class TestSessionsEdgeCases:
    def test_corrupt_sessions_json(self, monkeypatch, tmp_path):
        """Corrupt sessions.json returns None, no crash."""
        sessions_file = tmp_path / "sessions.json"
        sessions_file.write_text("{broken json")
        adapter = OpenClawAdapter()
        monkeypatch.setattr(adapter, "_find_sessions_json", lambda: sessions_file)
        assert adapter.get_last_channel() is None

    def test_empty_sessions_json(self, monkeypatch, tmp_path):
        """Empty sessions.json returns None."""
        sessions_file = tmp_path / "sessions.json"
        sessions_file.write_text("")
        adapter = OpenClawAdapter()
        monkeypatch.setattr(adapter, "_find_sessions_json", lambda: sessions_file)
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

    def test_find_sessions_json_priority(self, monkeypatch, tmp_path):
        """First candidate path wins when both exist."""
        # Create both candidate paths
        clawdbot_dir = tmp_path / ".clawdbot" / "agents" / "main" / "sessions"
        clawdbot_dir.mkdir(parents=True)
        (clawdbot_dir / "sessions.json").write_text("{}")

        openclaw_dir = tmp_path / ".openclaw" / "agents" / "main" / "sessions"
        openclaw_dir.mkdir(parents=True)
        (openclaw_dir / "sessions.json").write_text("{}")

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        adapter = OpenClawAdapter()
        result = adapter._find_sessions_json()
        assert result is not None
        assert ".clawdbot" in str(result)  # First candidate wins

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
            llm = standalone.get_llm_provider()
        assert isinstance(llm, ClaudeCodeLLMProvider)

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
    def test_openclaw_returns_gateway_provider(self, openclaw_adapter):
        """OpenClawAdapter.get_llm_provider() returns GatewayLLMProvider."""
        llm = openclaw_adapter.get_llm_provider()
        assert isinstance(llm, GatewayLLMProvider)

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
class TestResolveAnthropicCredential:
    """OpenClawAdapter._resolve_anthropic_credential() resolution chain."""

    def _make_adapter(self, tmp_path, monkeypatch):
        """Create an OpenClawAdapter with agent config dir pointed at tmp_path."""
        monkeypatch.setenv("CLAWDBOT_WORKSPACE", str(tmp_path))
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

    def test_reads_legacy_auth_json(self, tmp_path, monkeypatch):
        """Falls back to auth.json if no auth-profiles.json."""
        adapter, agent_dir = self._make_adapter(tmp_path, monkeypatch)
        import json
        auth = {"anthropic": {"type": "api_key", "key": "sk-ant-api-legacy"}}
        (agent_dir / "auth.json").write_text(json.dumps(auth))
        cred = adapter._resolve_anthropic_credential()
        assert cred == "sk-ant-api-legacy"

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

        # Create a log file with content
        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True)
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
        from datetime import datetime

        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "test.log"
        log_file.write_text("test entry\n")

        rotate_logs()

        # Original log should be gone or empty
        today = datetime.now().strftime("%Y-%m-%d")
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
        assert _workspace_root() == tmp_path

    def test_config_paths_use_adapter(self, standalone, tmp_path):
        """config.py should search for config in adapter-relative paths."""
        from config import _config_paths, reload_config
        paths = _config_paths()
        assert paths[0] == tmp_path / "config" / "memory.json"

    def test_notify_delegates_through_adapter(self, standalone, capsys):
        """notify.py should route through adapter.notify()."""
        from core.runtime.notify import notify_user
        # StandaloneAdapter prints to stderr
        notify_user("adapter test")
        captured = capsys.readouterr()
        assert "adapter test" in captured.err
