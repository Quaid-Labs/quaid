"""Unit tests for InstanceManager base class and CC subclass."""

import json
import sqlite3
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch


def _make_cc_adapter(tmp_path):
    """Build a ClaudeCodeAdapter with a tmp quaid_home."""
    from adaptors.claude_code.adapter import ClaudeCodeAdapter
    adapter = ClaudeCodeAdapter(home=tmp_path)
    return adapter


# ---- Base InstanceManager ----

class TestInstanceManagerBase:
    def test_resolve_instance_id(self, tmp_path):
        from lib.instance_manager import InstanceManager
        adapter = MagicMock()
        adapter.agent_id_prefix.return_value = "claude-code"
        mgr = InstanceManager(adapter)
        assert mgr.resolve_instance_id("myapp") == "claude-code-myapp"
        assert mgr.resolve_instance_id("MYAPP") == "claude-code-myapp"

    def test_resolve_instance_id_empty_raises(self, tmp_path):
        from lib.instance_manager import InstanceManager
        adapter = MagicMock()
        adapter.agent_id_prefix.return_value = "claude-code"
        mgr = InstanceManager(adapter)
        with pytest.raises(ValueError, match="non-empty"):
            mgr.resolve_instance_id("")

    def test_describe_naming(self, tmp_path):
        from lib.instance_manager import InstanceManager
        adapter = MagicMock()
        adapter.agent_id_prefix.return_value = "claude-code"
        mgr = InstanceManager(adapter)
        desc = mgr.describe_naming()
        assert "claude-code" in desc

    def test_create_dry_run(self, tmp_path):
        from lib.instance_manager import InstanceManager
        adapter = MagicMock()
        adapter.agent_id_prefix.return_value = "claude-code"
        adapter.quaid_home.return_value = tmp_path
        mgr = InstanceManager(adapter)

        with patch("lib.instance.instance_exists", return_value=False), \
             patch("lib.instance.validate_instance_id"):
            silo = mgr.create("myapp", dry_run=True)

        assert silo == tmp_path / "claude-code-myapp"
        assert not silo.exists()

    def test_create_makes_silo(self, tmp_path):
        from lib.instance_manager import InstanceManager
        adapter = MagicMock()
        adapter.agent_id_prefix.return_value = "claude-code"
        adapter.adapter_id.return_value = "claude-code"
        adapter.quaid_home.return_value = tmp_path
        mgr = InstanceManager(adapter)

        with patch("lib.instance.instance_exists", return_value=False), \
             patch("lib.instance.validate_instance_id"):
            silo = mgr.create("proj")

        assert (silo / "config").is_dir()
        assert (silo / "data").is_dir()
        assert (silo / "identity").is_dir()
        assert (silo / "journal").is_dir()
        assert (silo / "logs").is_dir()
        assert (silo / "identity" / "SOUL.md").is_file()
        assert (silo / "identity" / "USER.md").is_file()
        assert (silo / "identity" / "MEMORY.md").is_file()
        assert (silo / "PROJECT.md").is_file()
        config = json.loads((silo / "config" / "memory.json").read_text())
        assert config["adapter"]["type"] == adapter.adapter_id()

    def test_create_raises_if_exists(self, tmp_path):
        from lib.instance_manager import InstanceManager
        adapter = MagicMock()
        adapter.agent_id_prefix.return_value = "claude-code"
        adapter.adapter_id.return_value = "claude-code"
        adapter.quaid_home.return_value = tmp_path
        mgr = InstanceManager(adapter)

        with patch("lib.instance.instance_exists", return_value=True), \
             patch("lib.instance.validate_instance_id"):
            with pytest.raises(ValueError, match="already exists"):
                mgr.create("existing")


# ---- CC InstanceManager ----

class TestClaudeCodeInstanceManager:
    def test_settings_snippet_contains_instance_id(self, tmp_path):
        from adaptors.claude_code.instance_manager import ClaudeCodeInstanceManager
        adapter = MagicMock()
        adapter.agent_id_prefix.return_value = "claude-code"
        mgr = ClaudeCodeInstanceManager(adapter)
        snippet = mgr.settings_snippet("claude-code-myapp")
        data = json.loads(snippet)
        assert data["env"]["QUAID_INSTANCE"] == "claude-code-myapp"

    def test_make_instance_creates_settings(self, tmp_path):
        from adaptors.claude_code.instance_manager import ClaudeCodeInstanceManager
        adapter = MagicMock()
        adapter.agent_id_prefix.return_value = "claude-code"
        adapter.adapter_id.return_value = "claude-code"
        adapter.quaid_home.return_value = tmp_path / "quaid"
        (tmp_path / "quaid").mkdir()
        mgr = ClaudeCodeInstanceManager(adapter)

        project_dir = tmp_path / "myproject"
        project_dir.mkdir()

        with patch("lib.instance.instance_exists", return_value=False), \
             patch("lib.instance.validate_instance_id"):
            mgr.make_instance(str(project_dir), "myapp")

        settings_path = project_dir / ".claude" / "settings.json"
        assert settings_path.is_file()
        data = json.loads(settings_path.read_text())
        assert data["env"]["QUAID_INSTANCE"] == "claude-code-myapp"

    def test_make_instance_overwrites_existing_instance(self, tmp_path):
        from adaptors.claude_code.instance_manager import ClaudeCodeInstanceManager
        adapter = MagicMock()
        adapter.agent_id_prefix.return_value = "claude-code"
        adapter.adapter_id.return_value = "claude-code"
        adapter.quaid_home.return_value = tmp_path / "quaid"
        (tmp_path / "quaid").mkdir()
        mgr = ClaudeCodeInstanceManager(adapter)

        project_dir = tmp_path / "myproject"
        claude_dir = project_dir / ".claude"
        claude_dir.mkdir(parents=True)
        existing = {"env": {"QUAID_INSTANCE": "claude-code-old", "OTHER": "value"}, "hooks": {}}
        (claude_dir / "settings.json").write_text(json.dumps(existing))

        with patch("lib.instance.instance_exists", return_value=False), \
             patch("lib.instance.validate_instance_id"):
            mgr.make_instance(str(project_dir), "newapp")

        data = json.loads((claude_dir / "settings.json").read_text())
        assert data["env"]["QUAID_INSTANCE"] == "claude-code-newapp"
        assert data["env"]["OTHER"] == "value"   # other env vars preserved
        assert data["hooks"] == {}               # other settings preserved

    def test_make_instance_dry_run_no_writes(self, tmp_path):
        from adaptors.claude_code.instance_manager import ClaudeCodeInstanceManager
        adapter = MagicMock()
        adapter.agent_id_prefix.return_value = "claude-code"
        adapter.quaid_home.return_value = tmp_path / "quaid"
        (tmp_path / "quaid").mkdir()
        mgr = ClaudeCodeInstanceManager(adapter)

        project_dir = tmp_path / "myproject"
        project_dir.mkdir()

        with patch("lib.instance.instance_exists", return_value=False), \
             patch("lib.instance.validate_instance_id"):
            mgr.make_instance(str(project_dir), "myapp", dry_run=True)

        assert not (project_dir / ".claude" / "settings.json").exists()

    def test_make_instance_nonexistent_path_raises(self, tmp_path):
        from adaptors.claude_code.instance_manager import ClaudeCodeInstanceManager
        adapter = MagicMock()
        adapter.agent_id_prefix.return_value = "claude-code"
        mgr = ClaudeCodeInstanceManager(adapter)

        with pytest.raises(ValueError, match="does not exist"):
            mgr.make_instance(str(tmp_path / "nonexistent"), "myapp")


# ---- Adapter registration ----

class TestAdapterCLIRegistration:
    def test_cc_adapter_namespace(self, tmp_path):
        adapter = _make_cc_adapter(tmp_path)
        assert adapter.get_cli_namespace() == "claudecode"

    def test_cc_adapter_has_make_instance_command(self, tmp_path):
        adapter = _make_cc_adapter(tmp_path)
        cmds = adapter.get_cli_commands()
        assert "make_instance" in cmds
        assert callable(cmds["make_instance"])

    def test_cc_adapter_tools_snippet_contains_commands(self, tmp_path):
        adapter = _make_cc_adapter(tmp_path)
        snippet = adapter.get_cli_tools_snippet()
        assert "make_instance" in snippet
        assert "claudecode" in snippet
        assert "QUAID_INSTANCE" in snippet

    def test_cc_adapter_get_instance_manager(self, tmp_path):
        from adaptors.claude_code.instance_manager import ClaudeCodeInstanceManager
        adapter = _make_cc_adapter(tmp_path)
        mgr = adapter.get_instance_manager()
        assert isinstance(mgr, ClaudeCodeInstanceManager)

    def test_base_adapter_namespace_is_none(self):
        from lib.adapter import QuaidAdapter
        # StandaloneAdapter inherits default None
        from lib.adapter import StandaloneAdapter
        import os
        adapter = StandaloneAdapter()
        assert adapter.get_cli_namespace() is None
        assert adapter.get_cli_commands() == {}
        assert adapter.get_cli_tools_snippet() == ""
        assert adapter.get_instance_manager() is None
