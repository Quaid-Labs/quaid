import json
import os
from pathlib import Path


def test_first_touch_starts_async_provision_and_skips_core_hook(tmp_path, monkeypatch):
    from adaptors.claude_code import hooks as cc_hooks

    hidden_home = tmp_path / ".quaid"
    visible_home = tmp_path / "quaid"
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    monkeypatch.setenv("QUAID_HOME", str(hidden_home))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(visible_home))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project_dir))

    seen = {"spawn": None, "queued": None, "core_called": False}

    def fake_spawn(name, instance_id, marker_path):
        seen["spawn"] = (name, instance_id, Path(marker_path))

    def fake_queue(instance_id):
        seen["queued"] = instance_id

    def fake_core_main():
        seen["core_called"] = True

    monkeypatch.setattr(cc_hooks, "_spawn_background_provision", fake_spawn)
    monkeypatch.setattr(cc_hooks, "_queue_async_notice", fake_queue)
    monkeypatch.setattr("core.interface.hooks.main", fake_core_main)

    cc_hooks.main()

    assert seen["core_called"] is False
    assert seen["spawn"] is not None
    name, instance_id, marker_path = seen["spawn"]
    assert instance_id.startswith("claude-code-")
    assert name == instance_id.removeprefix("claude-code-")
    assert seen["queued"] == instance_id
    assert marker_path.is_file()


def test_existing_instance_runs_core_hook_without_async_bootstrap(tmp_path, monkeypatch):
    from adaptors.claude_code import hooks as cc_hooks
    from lib.instance import instance_slug_from_project_dir

    hidden_home = tmp_path / ".quaid"
    visible_home = tmp_path / "quaid"
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    slug = instance_slug_from_project_dir(str(project_dir))
    instance_id = f"claude-code-{slug}"
    instance_root = hidden_home / "instances" / instance_id
    instance_root.mkdir(parents=True, exist_ok=True)
    (instance_root / "config.json").write_text(json.dumps({"adapter": {"type": "claude-code"}}), encoding="utf-8")

    monkeypatch.setenv("QUAID_HOME", str(hidden_home))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(visible_home))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project_dir))

    seen = {"spawned": False, "queued": False, "core_calls": 0}

    monkeypatch.setattr(cc_hooks, "_spawn_background_provision", lambda *args: seen.__setitem__("spawned", True))
    monkeypatch.setattr(cc_hooks, "_queue_async_notice", lambda *args: seen.__setitem__("queued", True))
    monkeypatch.setattr("core.interface.hooks.main", lambda: seen.__setitem__("core_calls", seen["core_calls"] + 1))

    cc_hooks.main()

    assert seen["core_calls"] == 1
    assert seen["spawned"] is False
    assert seen["queued"] is False


def test_background_provision_creates_silo_and_starts_daemon(tmp_path, monkeypatch):
    from adaptors.claude_code import hooks as cc_hooks

    hidden_home = tmp_path / ".quaid"
    visible_home = tmp_path / "quaid"
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    instance_id = "claude-code-project"
    marker_path = hidden_home / "instances" / instance_id / ".runtime" / "provisioning.json"
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(json.dumps({"instance_id": instance_id, "started_at": 123.0}), encoding="utf-8")

    monkeypatch.setenv("QUAID_HOME", str(hidden_home))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(visible_home))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project_dir))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")

    seen = {"auto_provision": None, "stored_token": None, "ensure_alive": 0}

    def fake_auto_provision(self, name):
        seen["auto_provision"] = name
        silo_root = hidden_home / "instances" / instance_id
        (silo_root / "data").mkdir(parents=True, exist_ok=True)
        (silo_root / "config.json").write_text(
            json.dumps({"adapter": {"type": "claude-code"}}),
            encoding="utf-8",
        )
        return instance_id, True

    def fake_store_auth(self, token):
        seen["stored_token"] = token
        return hidden_home / "instances" / instance_id / ".auth-token"

    def fake_ensure_alive():
        seen["ensure_alive"] += 1
        return 4242

    monkeypatch.setattr("adaptors.claude_code.instance_manager.ClaudeCodeInstanceManager.auto_provision", fake_auto_provision)
    monkeypatch.setattr("adaptors.claude_code.adapter.ClaudeCodeAdapter.store_auth_token", fake_store_auth)
    monkeypatch.setattr("core.extraction_daemon.ensure_alive", fake_ensure_alive)

    rc = cc_hooks._run_background_provision("project", instance_id, str(marker_path))

    assert rc == 0
    assert seen["auto_provision"] == "project"
    assert seen["stored_token"] == "test-token"
    assert seen["ensure_alive"] == 1
    assert not marker_path.exists()
    assert os.environ.get("QUAID_INSTANCE", "") != instance_id
