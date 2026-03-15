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
from unittest.mock import MagicMock, patch


def _git_available():
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


@pytest.fixture
def project_env(tmp_path):
    """Set up a complete project system environment."""
    quaid_home = tmp_path / "quaid-home"
    quaid_home.mkdir()
    (quaid_home / "shared" / "projects").mkdir(parents=True)
    (quaid_home / "config").mkdir()
    (quaid_home / "config" / "memory.json").write_text("{}")

    # User's source code directory
    user_code = tmp_path / "user-project"
    user_code.mkdir()
    (user_code / "main.py").write_text("def hello():\n    print('hello')\n")
    (user_code / "utils.py").write_text("def add(a, b):\n    return a + b\n")

    adapter = MagicMock()
    adapter.quaid_home.return_value = quaid_home
    adapter.adapter_id.return_value = "test-adapter"
    adapter.projects_dir.return_value = quaid_home / "shared" / "projects"

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

        # Canonical dir created with structure
        canonical = project_env["quaid_home"] / "shared" / "projects" / "my-app"
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

        updated, applied = apply_edit_blocks(doc, edits)
        assert applied == 2
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
