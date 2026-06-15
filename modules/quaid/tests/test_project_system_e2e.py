"""End-to-end tests for the project system.

Tests the full pipeline: project creation → shadow git tracking →
file changes → snapshot → sync → docs update classification.

These tests use a real filesystem and real git (no mocks for git),
but mock the LLM calls and adapter.
"""

import json
import pytest
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _git_available():
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def _args(**kwargs):
    defaults = {"json": False}
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


@pytest.fixture
def project_env(tmp_path, monkeypatch):
    """Set up a complete project system environment."""
    quaid_home = tmp_path / "quaid-home"
    quaid_home.mkdir()
    (quaid_home / "shared" / "projects").mkdir(parents=True)
    (quaid_home / "config").mkdir()
    (quaid_home / "config" / "config.json").write_text("{}")

    # User's source code directory
    user_code = tmp_path / "user-project"
    user_code.mkdir()
    (user_code / "main.py").write_text("def hello():\n    print('hello')\n")
    (user_code / "utils.py").write_text("def add(a, b):\n    return a + b\n")

    adapter = MagicMock()
    adapter.quaid_home.return_value = quaid_home
    adapter.instance_root.return_value = quaid_home / "test-instance"
    adapter.adapter_id.return_value = "test-adapter"
    adapter.projects_dir.return_value = quaid_home / "shared" / "projects"
    monkeypatch.setenv("QUAID_HOME", str(quaid_home))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(quaid_home))
    monkeypatch.setenv("QUAID_INSTANCE", "test-instance")

    with patch("lib.adapter.get_adapter", return_value=adapter):
        yield {
            "quaid_home": quaid_home,
            "user_code": user_code,
            "adapter": adapter,
            "tmp_path": tmp_path,
        }


@pytest.mark.skipif(not _git_available(), reason="git not available")
class TestProjectSystemE2E:
    """Full pipeline tests for the project system."""

    def test_create_project_with_source_root(self, project_env):
        """Creating a project with source_root initializes shadow git and syncs."""
        from core.project_registry import create_project, get_project

        entry = create_project(
            "my-app",
            description="Test application",
            source_root=str(project_env["user_code"]),
        )

        # Project registered
        assert get_project("my-app") is not None
        assert entry["source_root"] == str(project_env["user_code"])

        # Canonical dir created with structure (quaid_projects_dir uses quaid_home/projects/)
        canonical = project_env["quaid_home"] / "projects" / "my-app"
        assert canonical.is_dir()
        assert (canonical / "docs").is_dir()
        assert (canonical / "PROJECT.md").is_file()
        assert "Test application" in (canonical / "PROJECT.md").read_text()

        # Shadow git initialized
        tracking = project_env["quaid_home"] / ".git-tracking" / "my-app"
        assert tracking.is_dir()
        assert (tracking / "HEAD").is_file()

    def test_snapshot_detects_file_changes(self, project_env):
        """After creating a project, file modifications are detected by snapshot."""
        from core.project_registry import create_project, snapshot_all_projects

        create_project(
            "my-app",
            description="Test",
            source_root=str(project_env["user_code"]),
        )

        # Modify a file
        (project_env["user_code"] / "main.py").write_text(
            "def hello():\n    print('hello world!')\n"
        )

        # Snapshot should detect the change
        results = snapshot_all_projects()
        assert len(results) == 1
        assert results[0]["project"] == "my-app"
        assert any(c["path"] == "main.py" for c in results[0]["changes"])
        assert results[0]["diff"]  # Should have actual diff text
        assert "hello world" in results[0]["diff"]

    def test_snapshot_detects_new_and_deleted_files(self, project_env):
        """Snapshot detects added and deleted files."""
        from core.project_registry import create_project, snapshot_all_projects

        create_project(
            "my-app",
            description="Test",
            source_root=str(project_env["user_code"]),
        )

        # Add a new file and delete an existing one
        (project_env["user_code"] / "new_module.py").write_text("# new\n")
        (project_env["user_code"] / "utils.py").unlink()

        results = snapshot_all_projects()
        assert len(results) == 1
        changes = {c["path"]: c["status"] for c in results[0]["changes"]}
        assert "new_module.py" in changes
        assert changes["new_module.py"] == "A"
        assert "utils.py" in changes
        assert changes["utils.py"] == "D"

    def test_classify_trivial_changes(self, project_env):
        """Trivial code changes should be classified as trivial."""
        from datastore.docsdb.updater import classify_doc_change

        # Comment-only change
        trivial_diff = (
            "diff --git a/main.py b/main.py\n"
            "--- a/main.py\n"
            "+++ b/main.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-# old comment\n"
            "+# new comment\n"
        )
        result = classify_doc_change(trivial_diff)
        assert result["classification"] == "trivial"

    def test_classify_significant_changes(self, project_env):
        """New function definitions should be classified as significant."""
        from datastore.docsdb.updater import classify_doc_change

        significant_diff = (
            "diff --git a/main.py b/main.py\n"
            "--- a/main.py\n"
            "+++ b/main.py\n"
            "@@ -1,2 +1,10 @@\n"
            " def hello():\n"
            "     print('hello')\n"
            "+\n"
            "+def new_api_endpoint(request):\n"
            "+    '''Handle POST /api/users.'''\n"
            "+    data = request.json()\n"
            "+    user = create_user(data)\n"
            "+    return jsonify(user), 201\n"
            "+\n"
            "+class UserManager:\n"
        )
        result = classify_doc_change(significant_diff)
        assert result["classification"] == "significant"

    def test_full_pipeline_create_change_snapshot(self, project_env):
        """Full pipeline: create → modify files → snapshot → get diff."""
        from core.project_registry import (
            create_project,
            get_project,
            snapshot_all_projects,
            delete_project,
        )

        # Create
        create_project(
            "full-test",
            description="Full pipeline test",
            source_root=str(project_env["user_code"]),
        )

        # Modify
        (project_env["user_code"] / "main.py").write_text(
            "def hello():\n    print('goodbye')\n\ndef new_func():\n    pass\n"
        )
        (project_env["user_code"] / "config.yaml").write_text("key: value\n")

        # Snapshot
        results = snapshot_all_projects()
        assert len(results) == 1
        snap = results[0]
        assert snap["project"] == "full-test"
        assert len(snap["changes"]) >= 2  # main.py modified, config.yaml added
        assert snap["diff"]
        assert "goodbye" in snap["diff"]

        # Verify project is still registered
        assert get_project("full-test") is not None

        # Delete
        delete_project("full-test")
        assert get_project("full-test") is None

        # User's files untouched
        assert (project_env["user_code"] / "main.py").is_file()
        assert (project_env["user_code"] / "config.yaml").is_file()

    def test_shadow_history_can_restore_prior_file_version(self, project_env, capsys):
        """Shadow git exposes prior tracked file versions for recovery."""
        from core.project_registry import create_project, snapshot_all_projects
        from core import project_registry_cli as cli

        create_project(
            "restore-test",
            description="Restore test",
            source_root=str(project_env["user_code"]),
        )

        original = "def hello():\n    print('hello')\n"
        updated = "def hello():\n    print('hello world!')\n"
        target = project_env["user_code"] / "main.py"
        assert target.read_text() == original

        target.write_text(updated)
        results = snapshot_all_projects()
        assert len(results) == 1

        cli.cmd_history(_args(name="restore-test", file="main.py", limit=5, json=True))
        history_payload = json.loads(capsys.readouterr().out)
        commits = history_payload["history"]
        assert len(commits) >= 2
        prior_rev = commits[-1]["commit"]

        cli.cmd_show_version(_args(name="restore-test", rev=prior_rev, file="main.py"))
        assert capsys.readouterr().out == original

        # Simulate a destructive agent edit after the last good snapshot.
        target.write_text("def hello():\n")

        cli.cmd_restore(_args(name="restore-test", rev=prior_rev, file="main.py", yes=True, json=False))
        assert "Restored main.py" in capsys.readouterr().out
        assert target.read_text() == original

    def test_shadow_restore_is_atomic_and_rejects_paths_outside_source_root(self, project_env, monkeypatch):
        """Restore writes via a temp sibling and refuses traversal/outside paths."""
        from core.project_registry import create_project, snapshot_all_projects
        from core.shadow_git import ShadowGit
        from lib.adapter import quaid_tracking_dir

        create_project(
            "atomic-restore",
            description="Atomic restore test",
            source_root=str(project_env["user_code"]),
        )
        target = project_env["user_code"] / "main.py"
        target.write_text("def hello():\n    print('safe backup')\n")
        snapshot_all_projects()

        sg = ShadowGit(
            "atomic-restore",
            project_env["user_code"],
            tracking_base=quaid_tracking_dir(project_env["quaid_home"]),
        )
        backup_rev = sg.history("main.py", limit=1)[0]["commit"]

        target.write_text("current content must survive failed restore\n")
        original_replace = Path.replace

        def fail_replace(self, target_path):
            if self.name.endswith(".quaid_tmp"):
                raise RuntimeError("simulated replace failure")
            return original_replace(self, target_path)

        monkeypatch.setattr(Path, "replace", fail_replace)
        with pytest.raises(RuntimeError, match="simulated replace failure"):
            sg.restore_file(backup_rev, "main.py")
        assert target.read_text() == "current content must survive failed restore\n"

        with pytest.raises(ValueError, match="outside project source root|invalid project file path"):
            sg.restore_file(backup_rev, "../outside.md")
        with pytest.raises(ValueError, match="outside project source root"):
            sg.restore_file(backup_rev, str(project_env["tmp_path"] / "outside.md"))

    def test_multiple_projects(self, project_env):
        """Multiple projects can coexist independently."""
        from core.project_registry import create_project, list_projects, snapshot_all_projects

        # Create two projects with different source roots
        src_a = project_env["tmp_path"] / "project-a"
        src_b = project_env["tmp_path"] / "project-b"
        src_a.mkdir()
        src_b.mkdir()
        (src_a / "app.py").write_text("# app a\n")
        (src_b / "app.py").write_text("# app b\n")

        create_project("proj-a", source_root=str(src_a))
        create_project("proj-b", source_root=str(src_b))

        projects = list_projects()
        assert "proj-a" in projects
        assert "proj-b" in projects

        # Only modify project A
        (src_a / "app.py").write_text("# app a modified\n")

        results = snapshot_all_projects()
        assert len(results) == 1
        assert results[0]["project"] == "proj-a"

    def test_ignored_files_not_tracked(self, project_env):
        """Files matching default ignore patterns should not appear in snapshots."""
        from core.project_registry import create_project, snapshot_all_projects

        create_project(
            "my-app",
            description="Test",
            source_root=str(project_env["user_code"]),
        )

        # Add files that should be ignored
        (project_env["user_code"] / ".env").write_text("SECRET=foo\n")
        node_modules = project_env["user_code"] / "node_modules"
        node_modules.mkdir()
        (node_modules / "package.json").write_text("{}")

        results = snapshot_all_projects()
        if results:
            paths = [c["path"] for c in results[0]["changes"]]
            assert ".env" not in paths
            assert "node_modules/package.json" not in paths

    def test_docs_update_context_building(self, project_env):
        """Verify the docs update context is built correctly from snapshots."""
        from core.docs_updater_hook import _build_update_context

        context = _build_update_context(
            project_name="my-app",
            diff_text="diff --git a/main.py\n-old\n+new\n",
            changes=[
                {"status": "M", "path": "main.py", "old_path": None},
                {"status": "A", "path": "new_file.py", "old_path": None},
            ],
            project_log=["Refactored main module", "Added new utility"],
        )

        assert "my-app" in context
        assert "main.py" in context
        assert "modified" in context
        assert "new_file.py" in context
        assert "added" in context
        assert "diff --git" in context
        assert "Refactored main module" in context

    def test_apply_edit_blocks_integration(self, project_env):
        """Edit blocks from LLM response are correctly applied to docs."""
        from datastore.docsdb.updater import apply_edit_blocks

        doc = "# TOOLS\n\n## API\n\nEndpoint: /api/v1/users\n\n## Config\n\nPort: 8080\n"

        edits = [
            "SECTION: API\nOLD: Endpoint: /api/v1/users\nNEW: Endpoint: /api/v2/users\nMethod: GET, POST",
            "SECTION: Config\nOLD: Port: 8080\nNEW: Port: 9090\nHost: 0.0.0.0",
        ]

        updated, applied, unmatched = apply_edit_blocks(doc, edits)
        assert applied == 2
        assert unmatched == 0
        assert "/api/v2/users" in updated
        assert "Port: 9090" in updated
        assert "/api/v1/users" not in updated
        assert "Port: 8080" not in updated


@pytest.mark.skipif(not _git_available(), reason="git not available")
class TestProjectSystemCLI:
    """Test the CLI entry point for the project system."""

    def test_cli_list_empty(self, project_env):
        """CLI list command works with no projects."""
        from core.project_registry import list_projects
        assert list_projects() == {}

    def test_cli_create_and_list(self, project_env):
        """CLI create followed by list shows the project."""
        from core.project_registry import create_project, list_projects

        create_project("test-app", description="Test")
        projects = list_projects()
        assert "test-app" in projects

    def test_cli_create_invalid_name(self, project_env):
        """CLI rejects invalid project names."""
        from core.project_registry import create_project
        with pytest.raises(ValueError):
            create_project("Invalid Name!")

    def test_cli_show_missing(self, project_env):
        """Show returns None for missing projects."""
        from core.project_registry import get_project
        assert get_project("nonexistent") is None
