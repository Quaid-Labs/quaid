"""Tests for core/shadow_git.py — invisible change tracking."""

import pytest
import subprocess
from pathlib import Path

from core.shadow_git import (
    ShadowGit,
    _parse_name_status,
    _DEFAULT_EXCLUDES,
    _SHADOW_GIT_USER_EMAIL,
    _SHADOW_GIT_USER_NAME,
)


def _git_available():
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


@pytest.mark.skipif(not _git_available(), reason="git not available")
class TestShadowGit:
    def test_init_creates_bare_repo(self, tmp_path):
        sg = ShadowGit("test", tmp_path / "project", tracking_base=tmp_path / "tracking")
        (tmp_path / "project").mkdir()

        sg.init()
        assert sg.initialized
        assert (sg.git_dir / "HEAD").is_file()

    def test_init_configures_internal_git_identity(self, tmp_path):
        sg = ShadowGit("test", tmp_path / "project", tracking_base=tmp_path / "tracking")
        (tmp_path / "project").mkdir()

        sg.init()

        name = sg._git("config", "user.name", check=False)
        email = sg._git("config", "user.email", check=False)
        assert name.stdout.strip() == _SHADOW_GIT_USER_NAME
        assert email.stdout.strip() == _SHADOW_GIT_USER_EMAIL

    def test_init_idempotent(self, tmp_path):
        sg = ShadowGit("test", tmp_path / "project", tracking_base=tmp_path / "tracking")
        (tmp_path / "project").mkdir()

        sg.init()
        sg.init()  # Should not raise
        assert sg.initialized

    def test_init_repairs_identity_for_existing_repo(self, tmp_path):
        sg = ShadowGit("test", tmp_path / "project", tracking_base=tmp_path / "tracking")
        (tmp_path / "project").mkdir()
        sg.git_dir.mkdir(parents=True)
        subprocess.run(["git", "init", "--bare", str(sg.git_dir)], capture_output=True, check=True)

        sg.init()

        assert sg._git("config", "user.email", check=False).stdout.strip() == _SHADOW_GIT_USER_EMAIL

    def test_default_excludes_written(self, tmp_path):
        sg = ShadowGit("test", tmp_path / "project", tracking_base=tmp_path / "tracking")
        (tmp_path / "project").mkdir()

        sg.init()
        exclude_file = sg.git_dir / "info" / "exclude"
        assert exclude_file.exists()
        content = exclude_file.read_text()
        assert ".env" in content
        assert "node_modules/" in content
        assert "__pycache__/" in content

    def test_snapshot_detects_new_file(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()

        sg = ShadowGit("test", project, tracking_base=tmp_path / "tracking")
        sg.init()

        # Create a file and snapshot
        (project / "hello.py").write_text("print('hello')")
        result = sg.snapshot()

        assert result is not None
        assert result.is_initial
        assert any(c.path == "hello.py" for c in result.changes)

    def test_snapshot_commit_message_honors_quaid_now(self, tmp_path, monkeypatch):
        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:06:07Z")

        sg = ShadowGit("test", project, tracking_base=tmp_path / "tracking")
        sg.init()
        (project / "hello.py").write_text("print('hello')", encoding="utf-8")

        sg.snapshot()

        log = sg._git("log", "-1", "--format=%s")
        assert log.stdout.strip() == "snapshot 2026-03-11T05:06:07Z"

    def test_snapshot_detects_modification(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()

        sg = ShadowGit("test", project, tracking_base=tmp_path / "tracking")
        sg.init()

        # Initial file
        (project / "hello.py").write_text("v1")
        sg.snapshot()

        # Modify
        (project / "hello.py").write_text("v2")
        result = sg.snapshot()

        assert result is not None
        assert not result.is_initial
        assert any(c.status == "M" and c.path == "hello.py" for c in result.changes)

    def test_snapshot_detects_deletion(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()

        sg = ShadowGit("test", project, tracking_base=tmp_path / "tracking")
        sg.init()

        (project / "hello.py").write_text("delete me")
        sg.snapshot()

        (project / "hello.py").unlink()
        result = sg.snapshot()

        assert result is not None
        assert any(c.status == "D" and c.path == "hello.py" for c in result.changes)

    def test_snapshot_returns_none_when_no_changes(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()

        sg = ShadowGit("test", project, tracking_base=tmp_path / "tracking")
        sg.init()

        (project / "hello.py").write_text("static")
        sg.snapshot()

        # No changes
        result = sg.snapshot()
        assert result is None

    def test_committed_snapshot_since_recovers_already_committed_change(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()

        sg = ShadowGit("test", project, tracking_base=tmp_path / "tracking")
        sg.init()

        (project / "hello.py").write_text("v1")
        base = sg.snapshot().commit_hash
        (project / "hello.py").write_text("v2")
        head = sg.snapshot().commit_hash

        assert sg.snapshot() is None
        recovered = sg.committed_snapshot_since(base)
        diff = sg.committed_diff_since(base, full=True)

        assert recovered is not None
        assert recovered.commit_hash == head
        assert any(c.status == "M" and c.path == "hello.py" for c in recovered.changes)
        assert diff and "v2" in diff

    def test_snapshot_raises_when_commit_fails_with_pending_changes(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        sg = ShadowGit("test", project, tracking_base=tmp_path / "tracking")
        sg.git_dir.mkdir(parents=True)
        (sg.git_dir / "HEAD").write_text("ref: refs/heads/main\n")
        sg._ensure_identity_config = lambda: None  # type: ignore[method-assign]

        def fake_git(*args, **_kwargs):
            cmd = tuple(args)
            if cmd[:2] == ("status", "--porcelain"):
                return subprocess.CompletedProcess(args, 0, stdout="A  app.py\n", stderr="")
            if cmd[0] == "add":
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if cmd[:2] == ("rev-parse", "HEAD"):
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
            if cmd[0] == "commit":
                return subprocess.CompletedProcess(args, 128, stdout="", stderr="Author identity unknown")
            raise AssertionError(f"unexpected git command: {cmd}")

        sg._git = fake_git  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="shadow git commit failed"):
            sg.snapshot()

    def test_ignores_excluded_files(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()

        sg = ShadowGit("test", project, tracking_base=tmp_path / "tracking")
        sg.init()

        # .env should be ignored by default excludes
        (project / ".env").write_text("SECRET=foo")
        (project / "app.py").write_text("real code")
        result = sg.snapshot()

        assert result is not None
        paths = [c.path for c in result.changes]
        assert "app.py" in paths
        assert ".env" not in paths

    def test_add_ignore_patterns(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()

        sg = ShadowGit("test", project, tracking_base=tmp_path / "tracking")
        sg.init()

        sg.add_ignore_patterns(["*.csv", "data/raw/"])
        exclude = (sg.git_dir / "info" / "exclude").read_text()
        assert "*.csv" in exclude
        assert "data/raw/" in exclude

    def test_get_tracked_files(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()

        sg = ShadowGit("test", project, tracking_base=tmp_path / "tracking")
        sg.init()

        (project / "a.py").write_text("a")
        (project / "b.py").write_text("b")
        sg.snapshot()

        tracked = sg.get_tracked_files()
        assert "a.py" in tracked
        assert "b.py" in tracked

    def test_destroy(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()

        sg = ShadowGit("test", project, tracking_base=tmp_path / "tracking")
        sg.init()
        assert sg.initialized

        sg.destroy()
        assert not sg.initialized
        assert not sg.git_dir.exists()

    def test_no_artifacts_in_project_dir(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()

        sg = ShadowGit("test", project, tracking_base=tmp_path / "tracking")
        sg.init()

        (project / "code.py").write_text("code")
        sg.snapshot()

        # No .git or any hidden files should appear in the project dir
        hidden = [f for f in project.iterdir() if f.name.startswith(".")]
        assert len(hidden) == 0


class TestParseNameStatus:
    def test_added(self):
        changes = _parse_name_status("A\tnew_file.py")
        assert len(changes) == 1
        assert changes[0].status == "A"
        assert changes[0].path == "new_file.py"

    def test_modified(self):
        changes = _parse_name_status("M\tchanged.py")
        assert changes[0].status == "M"

    def test_deleted(self):
        changes = _parse_name_status("D\tremoved.py")
        assert changes[0].status == "D"

    def test_renamed(self):
        changes = _parse_name_status("R100\told.py\tnew.py")
        assert changes[0].status == "R"
        assert changes[0].old_path == "old.py"
        assert changes[0].path == "new.py"

    def test_multiple(self):
        output = "A\ta.py\nM\tb.py\nD\tc.py"
        changes = _parse_name_status(output)
        assert len(changes) == 3

    def test_empty(self):
        assert _parse_name_status("") == []
