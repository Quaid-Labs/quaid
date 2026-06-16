"""Unit tests for InstanceManager base class and CC subclass."""

import json
import os
import shutil
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
        adapter.visible_home.return_value = tmp_path / "visible"
        adapter.instance_root.return_value = tmp_path / "instances" / "claude-code-main"
        mgr = InstanceManager(adapter)

        with patch("lib.instance.instance_exists", return_value=False), \
             patch("lib.instance.validate_instance_id"):
            silo = mgr.create("myapp", dry_run=True)

        assert silo == tmp_path / "instances" / "claude-code-myapp"
        assert not silo.exists()

    def test_create_makes_silo(self, tmp_path, monkeypatch):
        from lib.instance_manager import InstanceManager
        monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:00:00Z")
        adapter = MagicMock()
        adapter.agent_id_prefix.return_value = "claude-code"
        adapter.adapter_id.return_value = "claude-code"
        adapter.quaid_home.return_value = tmp_path
        adapter.visible_home.return_value = tmp_path / "visible"
        adapter.instance_root.return_value = tmp_path / "instances" / "claude-code-main"
        mgr = InstanceManager(adapter)

        with patch("lib.instance.instance_exists", return_value=False), \
             patch("lib.instance.validate_instance_id"):
            silo = mgr.create("proj")

        assert (silo / "data").is_dir()
        assert (silo / "logs").is_dir()
        assert (silo / "config.json").is_file()
        visible = tmp_path / "visible" / "instances" / "claude-code-proj"
        assert visible.is_dir()
        assert (visible / "journal").is_dir()
        assert (visible / "SOUL.md").is_file()
        assert (visible / "USER.md").is_file()
        assert (visible / "ENVIRONMENT.md").is_file()
        checkpoint = silo / "logs" / "janitor" / "checkpoint-all.json"
        assert checkpoint.is_file()
        checkpoint_data = json.loads(checkpoint.read_text(encoding="utf-8"))
        assert checkpoint_data.get("task") == "all"
        assert checkpoint_data.get("status") == "completed"
        assert checkpoint_data.get("started_at") == "2026-03-11T05:00:00Z"
        assert checkpoint_data.get("heartbeat_at") == "2026-03-11T05:00:00Z"
        assert checkpoint_data.get("last_completed_at") == "2026-03-11T05:00:00Z"
        config = json.loads((silo / "config.json").read_text())
        assert config["instance"]["id"] == "claude-code-proj"
        assert config["adapter"]["type"] == "claude-code"

    def test_create_rejects_malformed_quaid_now_for_janitor_checkpoint(self, tmp_path, monkeypatch):
        from lib.instance_manager import InstanceManager
        monkeypatch.setenv("QUAID_NOW", "not-a-date")
        adapter = MagicMock()
        adapter.agent_id_prefix.return_value = "claude-code"
        adapter.adapter_id.return_value = "claude-code"
        adapter.quaid_home.return_value = tmp_path
        adapter.visible_home.return_value = tmp_path / "visible"
        adapter.instance_root.return_value = tmp_path / "instances" / "claude-code-main"
        mgr = InstanceManager(adapter)

        with patch("lib.instance.instance_exists", return_value=False), \
             patch("lib.instance.validate_instance_id"), \
             pytest.raises(ValueError, match="Invalid QUAID_NOW"):
            mgr.create("proj")

    def test_read_json_object_logs_existing_bad_json(self, tmp_path, caplog):
        from lib.instance_manager import _read_json_object

        path = tmp_path / "config.json"
        path.write_text("{not-json", encoding="utf-8")

        with caplog.at_level("WARNING", logger="lib.instance_manager"):
            assert _read_json_object(path) == {}

        assert "Failed reading JSON object" in caplog.text
        assert str(path) in caplog.text

    def test_init_silo_logs_template_lookup_failure(self, tmp_path, caplog):
        from lib.instance_manager import InstanceManager

        adapter = MagicMock()
        visible_calls = {"count": 0}

        def visible_home():
            visible_calls["count"] += 1
            if visible_calls["count"] == 1:
                return tmp_path / "visible"
            raise RuntimeError("template lookup failed")

        adapter.visible_home.side_effect = visible_home
        adapter.adapter_id.return_value = "test"
        mgr = InstanceManager(adapter)

        with caplog.at_level("WARNING", logger="lib.instance_manager"):
            mgr._init_silo(
                tmp_path / "instances" / "test-main",
                "test-main",
                register_misc_project=False,
            )

        assert (tmp_path / "visible" / "instances" / "test-main" / "SOUL.md").is_file()
        assert "Shared project template lookup failed" in caplog.text
        assert "template lookup failed" in caplog.text

    def test_create_keeps_instance_config_minimal_and_relies_on_layering(self, tmp_path):
        from lib.instance_manager import InstanceManager
        adapter = MagicMock()
        adapter.agent_id_prefix.return_value = "openclaw"
        adapter.adapter_id.return_value = "openclaw"
        adapter.quaid_home.return_value = tmp_path
        adapter.visible_home.return_value = tmp_path / "visible"
        adapter.instance_root.return_value = tmp_path / "instances" / "openclaw-main"
        shared_cfg = tmp_path / "shared" / "config" / "openclaw" / "config.json"
        shared_cfg.parent.mkdir(parents=True)
        shared_cfg.write_text(
            json.dumps({
                "models": {
                    "llmProvider": "openai-codex",
                    "deepReasoning": "gpt-5.4",
                    "fastReasoning": "gpt-5.4-mini",
                },
                "capture": {"chunk_tokens": 500},
                "plugins": {"strict": True},
                "notifications": {"level": "normal"},
            }),
            encoding="utf-8",
        )
        mgr = InstanceManager(adapter)

        with patch("lib.instance.instance_exists", return_value=False), \
             patch("lib.instance.validate_instance_id"):
            silo = mgr.create("m13test")

        config = json.loads((silo / "config.json").read_text())
        assert config["instance"]["id"] == "openclaw-m13test"
        assert config["adapter"]["type"] == "openclaw"
        assert "models" not in config
        assert "capture" not in config
        assert "plugins" not in config
        assert "notifications" not in config
        assert "retrieval" not in config

    def test_init_silo_preserves_existing_instance_overrides(self, tmp_path):
        from lib.instance_manager import InstanceManager
        adapter = MagicMock()
        adapter.agent_id_prefix.return_value = "openclaw"
        adapter.adapter_id.return_value = "openclaw"
        adapter.quaid_home.return_value = tmp_path
        adapter.visible_home.return_value = tmp_path / "visible"
        adapter.instance_root.return_value = tmp_path / "instances" / "openclaw-main"
        mgr = InstanceManager(adapter)

        silo = tmp_path / "instances" / "openclaw-main"
        silo.mkdir(parents=True, exist_ok=True)
        (tmp_path / "visible" / "projects" / "quaid").mkdir(parents=True, exist_ok=True)
        existing = {
            "adapter": {"type": "legacy-type", "capabilities": {"context_refresh_strategy": "turn_based"}},
            "models": {"deepReasoning": "custom-deep"},
            "capture": {"chunk_tokens": 321},
        }
        (silo / "config.json").write_text(json.dumps(existing), encoding="utf-8")

        with patch("core.project_registry.get_project"), \
             patch("core.project_registry.create_project"), \
             patch("core.project_registry.link_project"), \
             patch("core.project_registry._sync_docs_registry_project"):
            mgr._init_silo(silo, "openclaw-main")

        config = json.loads((silo / "config.json").read_text(encoding="utf-8"))
        assert config["instance"]["id"] == "openclaw-main"
        assert config["adapter"]["type"] == "openclaw"
        assert config["adapter"]["capabilities"]["context_refresh_strategy"] == "turn_based"
        assert config["models"]["deepReasoning"] == "custom-deep"
        assert config["capture"]["chunk_tokens"] == 321

    def test_init_silo_does_not_override_running_janitor_checkpoint(self, tmp_path):
        from lib.instance_manager import InstanceManager
        adapter = MagicMock()
        adapter.agent_id_prefix.return_value = "openclaw"
        adapter.adapter_id.return_value = "openclaw"
        adapter.quaid_home.return_value = tmp_path
        adapter.visible_home.return_value = tmp_path / "visible"
        adapter.instance_root.return_value = tmp_path / "instances" / "openclaw-main"
        mgr = InstanceManager(adapter)

        silo = tmp_path / "instances" / "openclaw-main"
        checkpoint = silo / "logs" / "janitor" / "checkpoint-all.json"
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.write_text(
            json.dumps(
                {
                    "task": "all",
                    "status": "running",
                    "started_at": "2026-01-01T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )

        with patch("core.project_registry.get_project"), \
             patch("core.project_registry.create_project"), \
             patch("core.project_registry.link_project"), \
             patch("core.project_registry._sync_docs_registry_project"):
            mgr._init_silo(silo, "openclaw-main")

        checkpoint_data = json.loads(checkpoint.read_text(encoding="utf-8"))
        assert checkpoint_data["status"] == "running"
        assert "last_completed_at" not in checkpoint_data

    def test_init_silo_does_not_override_failed_janitor_checkpoint(self, tmp_path):
        from lib.instance_manager import InstanceManager
        adapter = MagicMock()
        adapter.agent_id_prefix.return_value = "openclaw"
        adapter.adapter_id.return_value = "openclaw"
        adapter.quaid_home.return_value = tmp_path
        adapter.visible_home.return_value = tmp_path / "visible"
        adapter.instance_root.return_value = tmp_path / "instances" / "openclaw-main"
        mgr = InstanceManager(adapter)

        silo = tmp_path / "instances" / "openclaw-main"
        checkpoint = silo / "logs" / "janitor" / "checkpoint-all.json"
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.write_text(
            json.dumps(
                {
                    "task": "all",
                    "status": "failed",
                    "started_at": "2026-01-01T00:00:00Z",
                    "heartbeat_at": "2026-01-01T00:30:00Z",
                }
            ),
            encoding="utf-8",
        )

        with patch("core.project_registry.get_project"), \
             patch("core.project_registry.create_project"), \
             patch("core.project_registry.link_project"), \
             patch("core.project_registry._sync_docs_registry_project"):
            mgr._init_silo(silo, "openclaw-main")

        checkpoint_data = json.loads(checkpoint.read_text(encoding="utf-8"))
        assert checkpoint_data["status"] == "failed"
        assert "last_completed_at" not in checkpoint_data

    def test_init_silo_skips_reseed_when_install_seeded_checkpoint_exists(self, tmp_path):
        from lib.instance_manager import InstanceManager
        adapter = MagicMock()
        adapter.agent_id_prefix.return_value = "openclaw"
        adapter.adapter_id.return_value = "openclaw"
        adapter.quaid_home.return_value = tmp_path
        adapter.visible_home.return_value = tmp_path / "visible"
        adapter.instance_root.return_value = tmp_path / "instances" / "openclaw-main"
        mgr = InstanceManager(adapter)

        silo = tmp_path / "instances" / "openclaw-main"
        checkpoint = silo / "logs" / "janitor" / "checkpoint-all.json"
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.write_text(
            json.dumps(
                {
                    "task": "all",
                    "status": "completed",
                    "started_at": "2026-01-01T00:00:00Z",
                    "heartbeat_at": "2026-01-01T00:30:00Z",
                    "last_completed_at": "2026-01-01T00:30:00Z",
                    "install_seeded": True,
                }
            ),
            encoding="utf-8",
        )

        with patch("core.project_registry.get_project"), \
             patch("core.project_registry.create_project"), \
             patch("core.project_registry.link_project"), \
             patch("core.project_registry._sync_docs_registry_project"):
            mgr._init_silo(silo, "openclaw-main")

        checkpoint_data = json.loads(checkpoint.read_text(encoding="utf-8"))
        assert checkpoint_data["status"] == "completed"
        assert checkpoint_data["install_seeded"] is True
        assert checkpoint_data["last_completed_at"] == "2026-01-01T00:30:00Z"

    def test_create_raises_if_exists(self, tmp_path):
        from lib.instance_manager import InstanceManager
        adapter = MagicMock()
        adapter.agent_id_prefix.return_value = "claude-code"
        adapter.adapter_id.return_value = "claude-code"
        adapter.quaid_home.return_value = tmp_path
        adapter.visible_home.return_value = tmp_path / "visible"
        adapter.instance_root.return_value = tmp_path / "instances" / "claude-code-main"
        mgr = InstanceManager(adapter)

        with patch("lib.instance.instance_exists", return_value=True), \
             patch("lib.instance.validate_instance_id"):
            with pytest.raises(ValueError, match="already exists"):
                mgr.create("existing")

    def test_create_can_skip_misc_project_registration(self, tmp_path):
        from lib.instance_manager import InstanceManager
        adapter = MagicMock()
        adapter.agent_id_prefix.return_value = "claude-code"
        adapter.adapter_id.return_value = "claude-code"
        adapter.quaid_home.return_value = tmp_path
        adapter.visible_home.return_value = tmp_path / "visible"
        adapter.instance_root.return_value = tmp_path / "instances" / "claude-code-main"
        mgr = InstanceManager(adapter)

        with patch("lib.instance.instance_exists", return_value=False), \
             patch("lib.instance.validate_instance_id"), \
             patch("core.project_registry.get_project") as mock_get_project, \
             patch("core.project_registry.create_project") as mock_create_project:
            silo = mgr.create("proj", register_misc_project=False)

        assert (silo / "config.json").is_file()
        assert (silo / "data" / "memory.db").is_file()
        mock_get_project.assert_not_called()
        mock_create_project.assert_not_called()

    def test_create_seeds_and_links_quaid_project(self, tmp_path):
        from lib.instance_manager import InstanceManager
        adapter = MagicMock()
        adapter.agent_id_prefix.return_value = "claude-code"
        adapter.adapter_id.return_value = "claude-code"
        adapter.quaid_home.return_value = tmp_path
        adapter.visible_home.return_value = tmp_path / "visible"
        adapter.instance_root.return_value = tmp_path / "instances" / "claude-code-main"
        mgr = InstanceManager(adapter)

        with patch("lib.instance.instance_exists", return_value=False), \
             patch("lib.instance.validate_instance_id"), \
             patch("core.project_registry.get_project", side_effect=[None, None]) as mock_get_project, \
             patch("core.project_registry.create_project") as mock_create_project, \
             patch("core.project_registry.link_project") as mock_link_project, \
             patch("core.project_registry._sync_docs_registry_project") as mock_sync_docs:
            mgr.create("proj")

        assert (tmp_path / "visible" / "projects" / "quaid").is_dir()
        mock_get_project.assert_any_call("quaid")
        mock_create_project.assert_any_call(
            "quaid",
            description="Quaid runtime, memory, project-doc, and adapter reference docs.",
            initial_instance="claude-code-proj",
        )
        mock_link_project.assert_not_called()
        mock_sync_docs.assert_not_called()

    def test_create_links_existing_quaid_project(self, tmp_path):
        from lib.instance_manager import InstanceManager
        adapter = MagicMock()
        adapter.agent_id_prefix.return_value = "claude-code"
        adapter.adapter_id.return_value = "claude-code"
        adapter.quaid_home.return_value = tmp_path
        adapter.visible_home.return_value = tmp_path / "visible"
        adapter.instance_root.return_value = tmp_path / "instances" / "claude-code-main"
        mgr = InstanceManager(adapter)

        with patch("lib.instance.instance_exists", return_value=False), \
             patch("lib.instance.validate_instance_id"), \
             patch("core.project_registry.get_project", side_effect=[{"instances": ["other"]}, None]), \
             patch("core.project_registry.create_project") as mock_create_project, \
             patch("core.project_registry.link_project") as mock_link_project, \
             patch("core.project_registry._sync_docs_registry_project") as mock_sync_docs:
            mgr.create("proj")

        mock_create_project.assert_any_call(
            "misc--claude-code-proj",
            description="Scratch pad for ephemeral and temporary files.",
            initial_instance="claude-code-proj",
        )
        mock_link_project.assert_called_once_with("quaid", instance_id="claude-code-proj")
        mock_sync_docs.assert_not_called()

    def test_ensure_registered_projects_respects_deleted_misc_registry_marker(self, tmp_path):
        from core.project_registry import _save_registry
        from lib.instance_manager import InstanceManager

        adapter = MagicMock()
        adapter.agent_id_prefix.return_value = "claude-code"
        adapter.adapter_id.return_value = "claude-code"
        adapter.quaid_home.return_value = tmp_path
        adapter.visible_home.return_value = tmp_path / "visible"
        adapter.instance_root.return_value = tmp_path / "instances" / "claude-code-main"
        mgr = InstanceManager(adapter)

        instance_id = "claude-code-proj"
        instance_root = tmp_path / "instances" / instance_id
        instance_root.mkdir(parents=True)
        (instance_root / "stale.txt").write_text("old silo\n", encoding="utf-8")
        with patch.dict(
            os.environ,
            {
                "QUAID_HOME": str(tmp_path),
                "QUAID_VISIBLE_HOME": str(tmp_path / "visible"),
            },
            clear=False,
        ), patch("core.project_registry._sync_docs_registry_project"):
            _save_registry(
                {
                    "projects": {},
                    "deleted_projects": {
                        f"misc--{instance_id}": "2026-04-24T00:00:00+00:00",
                    },
                },
                quaid_home=tmp_path,
            )
        shutil.rmtree(instance_root)

        with patch.object(mgr, "_ensure_shared_quaid_project") as mock_shared, \
             patch("core.project_registry.get_project", return_value=None), \
             patch("core.project_registry.create_project") as mock_create_project, \
             patch("core.project_registry.link_project") as mock_link_project, \
             patch("core.project_registry._sync_docs_registry_project") as mock_sync_docs:
            mgr.ensure_registered_projects(instance_id)

        mock_shared.assert_called_once_with(instance_id)
        mock_create_project.assert_not_called()
        mock_link_project.assert_not_called()
        mock_sync_docs.assert_not_called()


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
        adapter.visible_home.return_value = tmp_path / "visible"
        adapter.instance_root.return_value = tmp_path / "quaid" / "instances" / "claude-code-main"
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
        assert "hooks" in data
        assert "SessionStart" in data["hooks"]
        session_start_cmd = data["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        assert "hook-session-init" in session_start_cmd
        assert "QUAID_INSTANCE='" not in session_start_cmd
        assert 'CLAUDE_PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"' in session_start_cmd

    def test_make_instance_canonicalizes_legacy_project_path_label(self, tmp_path):
        from adaptors.claude_code.instance_manager import ClaudeCodeInstanceManager
        from lib.instance import _legacy_instance_slug_from_project_dir, instance_slug_from_project_dir

        adapter = MagicMock()
        adapter.agent_id_prefix.return_value = "claude-code"
        adapter.adapter_id.return_value = "claude-code"
        adapter.quaid_home.return_value = tmp_path / "quaid"
        adapter.visible_home.return_value = tmp_path / "visible"
        adapter.instance_root.return_value = tmp_path / "quaid" / "instances" / "claude-code-main"
        (tmp_path / "quaid").mkdir()
        mgr = ClaudeCodeInstanceManager(adapter)

        project_dir = tmp_path / "cc-livetest"
        project_dir.mkdir()
        legacy_label = _legacy_instance_slug_from_project_dir(str(project_dir))
        expected_label = instance_slug_from_project_dir(str(project_dir))
        expected_instance = f"claude-code-{expected_label}"

        with patch("lib.instance.instance_exists", return_value=False), \
             patch("lib.instance.validate_instance_id"):
            silo = mgr.make_instance(str(project_dir), legacy_label)

        assert silo == tmp_path / "quaid" / "instances" / expected_instance
        assert not (tmp_path / "quaid" / "instances" / f"claude-code-{legacy_label}").exists()
        data = json.loads((project_dir / ".claude" / "settings.json").read_text())
        assert data["env"]["QUAID_INSTANCE"] == expected_instance

    def test_make_instance_overwrites_existing_instance(self, tmp_path):
        from adaptors.claude_code.instance_manager import ClaudeCodeInstanceManager
        adapter = MagicMock()
        adapter.agent_id_prefix.return_value = "claude-code"
        adapter.adapter_id.return_value = "claude-code"
        adapter.quaid_home.return_value = tmp_path / "quaid"
        adapter.visible_home.return_value = tmp_path / "visible"
        adapter.instance_root.return_value = tmp_path / "quaid" / "instances" / "claude-code-main"
        (tmp_path / "quaid").mkdir()
        mgr = ClaudeCodeInstanceManager(adapter)

        project_dir = tmp_path / "myproject"
        claude_dir = project_dir / ".claude"
        claude_dir.mkdir(parents=True)
        existing = {
            "env": {"QUAID_INSTANCE": "claude-code-old", "OTHER": "value"},
            "hooks": {
                "SessionStart": [
                    {"matcher": "", "hooks": [{"type": "command", "command": "QUAID_INSTANCE='claude-code-old' quaid hook-session-init"}]},
                    {"matcher": "", "hooks": [{"type": "command", "command": "echo keep-me"}]},
                ],
            },
        }
        (claude_dir / "settings.json").write_text(json.dumps(existing))

        with patch("lib.instance.instance_exists", return_value=False), \
             patch("lib.instance.validate_instance_id"):
            mgr.make_instance(str(project_dir), "newapp")

        data = json.loads((claude_dir / "settings.json").read_text())
        assert data["env"]["QUAID_INSTANCE"] == "claude-code-newapp"
        assert data["env"]["OTHER"] == "value"   # other env vars preserved
        session_start_cmds = [
            h["command"]
            for entry in data["hooks"]["SessionStart"]
            for h in entry.get("hooks", [])
        ]
        assert "echo keep-me" in session_start_cmds
        assert any("hook-session-init" in cmd for cmd in session_start_cmds)
        assert all("QUAID_INSTANCE='claude-code-old'" not in cmd for cmd in session_start_cmds)

    def test_make_instance_dry_run_no_writes(self, tmp_path):
        from adaptors.claude_code.instance_manager import ClaudeCodeInstanceManager
        adapter = MagicMock()
        adapter.agent_id_prefix.return_value = "claude-code"
        adapter.quaid_home.return_value = tmp_path / "quaid"
        adapter.visible_home.return_value = tmp_path / "visible"
        adapter.instance_root.return_value = tmp_path / "quaid" / "instances" / "claude-code-main"
        (tmp_path / "quaid").mkdir()
        mgr = ClaudeCodeInstanceManager(adapter)

        project_dir = tmp_path / "myproject"
        project_dir.mkdir()

        # dry_run=True skips _write_hooks but patch anyway for safety.
        with patch("lib.instance.instance_exists", return_value=False), \
             patch("lib.instance.validate_instance_id"), \
             patch.object(mgr, "_write_hooks"):
            mgr.make_instance(str(project_dir), "myapp", dry_run=True)

        assert not (project_dir / ".claude" / "settings.json").exists()

    def test_make_instance_nonexistent_path_raises(self, tmp_path):
        from adaptors.claude_code.instance_manager import ClaudeCodeInstanceManager
        adapter = MagicMock()
        adapter.agent_id_prefix.return_value = "claude-code"
        mgr = ClaudeCodeInstanceManager(adapter)

        with pytest.raises(ValueError, match="does not exist"):
            mgr.make_instance(str(tmp_path / "nonexistent"), "myapp")

    def test_store_auth_token_raises_write_failure_when_failhard_enabled(self, monkeypatch):
        from adaptors.claude_code import instance_manager as manager_mod
        from adaptors.claude_code.instance_manager import ClaudeCodeInstanceManager

        adapter = MagicMock()
        adapter.store_auth_token.side_effect = OSError("disk full")
        mgr = ClaudeCodeInstanceManager(adapter)
        monkeypatch.setattr(manager_mod, "is_fail_hard_enabled", lambda: True)

        with pytest.raises(OSError, match="disk full"):
            mgr._store_auth_token("token-value")

    def test_store_auth_token_warns_write_failure_when_failhard_disabled(self, monkeypatch, capsys):
        from adaptors.claude_code import instance_manager as manager_mod
        from adaptors.claude_code.instance_manager import ClaudeCodeInstanceManager

        adapter = MagicMock()
        adapter.store_auth_token.side_effect = OSError("disk full")
        mgr = ClaudeCodeInstanceManager(adapter)
        monkeypatch.setattr(manager_mod, "is_fail_hard_enabled", lambda: False)

        mgr._store_auth_token("token-value")

        assert "Warning: could not write auth token: disk full" in capsys.readouterr().out


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

    def test_cc_adapter_make_instance_dry_run_canonicalizes_legacy_label(self, tmp_path, capsys):
        from lib.instance import _legacy_instance_slug_from_project_dir, instance_slug_from_project_dir

        adapter = _make_cc_adapter(tmp_path)
        project_dir = tmp_path / "cc-livetest"
        project_dir.mkdir()
        legacy_label = _legacy_instance_slug_from_project_dir(str(project_dir))
        expected_instance = f"claude-code-{instance_slug_from_project_dir(str(project_dir))}"

        adapter._cli_make_instance([str(project_dir), legacy_label, "--dry-run"])

        out = capsys.readouterr().out
        assert f"QUAID_INSTANCE={expected_instance}" in out
        assert f"claude-code-{legacy_label}" not in out

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


def test_adapter_bootstrap_silo_init_skips_misc_project_registration_under_adapter_lock(tmp_path, monkeypatch):
    from lib import adapter as adapter_mod
    from lib.instance_manager import InstanceManager

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "claude-code-bootstrap")
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CODEX_PROJECT_DIR", raising=False)

    adapter_mod.reset_adapter()

    seen = {"skip_misc": False}
    original_init = InstanceManager._init_silo

    def recording_init(self, silo_root, instance_id, *, register_misc_project=True):
        seen["skip_misc"] = (register_misc_project is False)
        return original_init(
            self,
            silo_root,
            instance_id,
            register_misc_project=register_misc_project,
        )

    monkeypatch.setattr(InstanceManager, "_init_silo", recording_init)

    config_path = tmp_path / "instances" / "claude-code-bootstrap" / "config.json"
    with patch("core.project_registry.get_project", side_effect=AssertionError("project registry should not run during adapter bootstrap")), \
         patch("core.project_registry.create_project", side_effect=AssertionError("project registry should not run during adapter bootstrap")):
        adapter_mod._auto_provision_from_env_if_needed()

    assert seen["skip_misc"] is True
    assert config_path.is_file()
    assert (config_path.parent / "data" / "memory.db").is_file()


def test_get_adapter_repairs_instance_projects_after_auto_provision(tmp_path, monkeypatch):
    from adaptors.claude_code.adapter import ClaudeCodeAdapter
    from lib import adapter as adapter_mod
    from lib.instance import instance_slug_from_project_dir

    project_dir = tmp_path / "project"
    project_dir.mkdir()

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path / "visible"))
    monkeypatch.delenv("QUAID_INSTANCE", raising=False)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project_dir))
    monkeypatch.delenv("CODEX_PROJECT_DIR", raising=False)

    adapter_mod.reset_adapter()

    calls = []

    def record_ensure(self, instance_id):
        calls.append(instance_id)

    monkeypatch.setattr("lib.instance_manager.InstanceManager.ensure_registered_projects", record_ensure)
    monkeypatch.setattr(
        adapter_mod,
        "_instantiate_adapter_from_manifest",
        lambda _kind: ClaudeCodeAdapter(home=tmp_path),
    )

    adapter = adapter_mod.get_adapter()

    assert adapter.adapter_id() == "claude-code"
    assert calls == [f"claude-code-{instance_slug_from_project_dir(str(project_dir))}"]


def test_auto_provision_derives_claude_code_instance_from_cwd_when_adapter_type_is_known(tmp_path, monkeypatch):
    from lib import adapter as adapter_mod
    from lib.instance import instance_slug_from_project_dir

    project_dir = tmp_path / "cc-project"
    project_dir.mkdir()

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path / "visible"))
    monkeypatch.setenv("QUAID_ADAPTER_TYPE", "claude-code")
    monkeypatch.delenv("QUAID_INSTANCE", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CODEX_PROJECT_DIR", raising=False)
    monkeypatch.chdir(project_dir)

    adapter_mod.reset_adapter()
    adapter_mod._auto_provision_from_env_if_needed()

    expected = f"claude-code-{instance_slug_from_project_dir(str(project_dir))}"
    assert os.environ.get("QUAID_INSTANCE") == expected
    assert (tmp_path / "instances" / expected / "config.json").is_file()


@pytest.mark.parametrize("adapter_type", ["claude-code", "codex"])
def test_auto_provision_does_not_derive_instance_from_quaid_plugin_cwd(tmp_path, monkeypatch, adapter_type):
    from lib import adapter as adapter_mod
    from lib.instance import instance_slug_from_project_dir

    home = tmp_path / ".quaid"
    plugin_dir = home / "plugins" / "quaid"
    plugin_dir.mkdir(parents=True)

    monkeypatch.setenv("QUAID_HOME", str(home))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path / "quaid"))
    monkeypatch.setenv("QUAID_ADAPTER_TYPE", adapter_type)
    monkeypatch.delenv("QUAID_INSTANCE", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CODEX_PROJECT_DIR", raising=False)
    monkeypatch.chdir(plugin_dir)

    adapter_mod.reset_adapter()
    adapter_mod._auto_provision_from_env_if_needed()

    blocked = f"{adapter_type}-{instance_slug_from_project_dir(str(plugin_dir))}"
    assert "QUAID_INSTANCE" not in os.environ
    assert not (home / "instances" / blocked / "config.json").exists()
    assert all(blocked not in str(path) for path in adapter_mod._adapter_config_paths())


def test_auto_provision_binds_explicit_codex_instance_to_project_dir(tmp_path, monkeypatch):
    from lib import adapter as adapter_mod
    from lib.instance import instance_slug_from_project_dir

    project_dir = tmp_path / "cdx-project"
    project_dir.mkdir()
    explicit_instance = "codex-m13test"
    slug = instance_slug_from_project_dir(str(project_dir))

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path / "visible"))
    monkeypatch.setenv("QUAID_INSTANCE", explicit_instance)
    monkeypatch.setenv("CODEX_PROJECT_DIR", str(project_dir))
    monkeypatch.delenv("QUAID_ADAPTER_TYPE", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("OPENCLAW_WORKSPACE", raising=False)
    monkeypatch.delenv("QUAID_WORKSPACE", raising=False)

    adapter_mod.reset_adapter()
    adapter_mod._auto_provision_from_env_if_needed()

    assert (tmp_path / "instances" / explicit_instance / "config.json").is_file()
    binding_path = tmp_path / "shared" / "instance-bindings" / "codex" / f"{slug}.json"
    assert binding_path.is_file()
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    assert binding["instance"] == explicit_instance
    assert binding["project_dir"] == str(project_dir.resolve())

    monkeypatch.delenv("QUAID_INSTANCE", raising=False)
    adapter_mod.reset_adapter()
    adapter_mod._auto_provision_from_env_if_needed()

    assert os.environ.get("QUAID_INSTANCE") == explicit_instance
    assert not (tmp_path / "instances" / f"codex-{slug}" / "config.json").exists()


def test_auto_provision_infers_adapter_from_manifest_prefix_for_existing_instance_env(tmp_path, monkeypatch):
    from lib import adapter as adapter_mod

    instance_id = "openclaw-m13test"
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path / "visible"))
    monkeypatch.setenv("QUAID_INSTANCE", instance_id)
    monkeypatch.delenv("QUAID_ADAPTER_TYPE", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CODEX_PROJECT_DIR", raising=False)
    monkeypatch.delenv("OPENCLAW_WORKSPACE", raising=False)
    monkeypatch.delenv("QUAID_WORKSPACE", raising=False)

    adapter_mod.reset_adapter()
    adapter_mod._auto_provision_from_env_if_needed()

    cfg_path = tmp_path / "instances" / instance_id / "config.json"
    assert cfg_path.is_file()
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert cfg.get("adapter", {}).get("type") == "openclaw"
    assert (cfg_path.parent / "data" / "memory.db").is_file()


def test_auto_provision_prefers_explicit_instance_prefix_over_inherited_host_env(tmp_path, monkeypatch):
    from lib import adapter as adapter_mod

    host_project = tmp_path / "cc-host-project"
    host_project.mkdir()
    instance_id = "openclaw-main"
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path / "visible"))
    monkeypatch.setenv("QUAID_INSTANCE", instance_id)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(host_project))
    monkeypatch.delenv("QUAID_ADAPTER_TYPE", raising=False)
    monkeypatch.delenv("CODEX_PROJECT_DIR", raising=False)
    monkeypatch.delenv("OPENCLAW_WORKSPACE", raising=False)
    monkeypatch.delenv("QUAID_WORKSPACE", raising=False)

    adapter_mod.reset_adapter()
    adapter_mod._auto_provision_from_env_if_needed()

    cfg_path = tmp_path / "instances" / instance_id / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert cfg.get("adapter", {}).get("type") == "openclaw"
