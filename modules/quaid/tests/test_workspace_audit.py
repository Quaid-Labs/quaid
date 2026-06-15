"""Tests for workspace_audit.py — bloat checking, monitored file config, file line counts."""

import sys
import os
import json
import tempfile
import fcntl
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, PropertyMock

# Ensure the plugin root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Must set MEMORY_DB_PATH before importing anything that touches config
os.environ.setdefault("MEMORY_DB_PATH", "/tmp/test-workspace-audit.db")

import pytest

from config import (
    MemoryConfig, DocsConfig, CoreMarkdownConfig,
)


from contextlib import contextmanager
from lib.adapter import set_adapter, reset_adapter, TestAdapter

@contextmanager
def _adapter_patch(tmp_path):
    """Context manager that sets the adapter to use tmp_path as visible content root.

    Yields the visible root path where workspace markdown files are resolved.
    """
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)
    try:
        yield adapter.visible_home()
    finally:
        reset_adapter()


@contextmanager
def _hidden_adapter_patch(tmp_path):
    """Context manager that yields the hidden instance root for runtime/log tests."""
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)
    try:
        yield adapter.instance_root()
    finally:
        reset_adapter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config_with_core_md(files=None):
    """Create a MemoryConfig with coreMarkdown section populated."""
    core_md = CoreMarkdownConfig(
        enabled=True,
        monitor_for_bloat=True,
        monitor_for_outdated=True,
        files=files or {},
    )
    docs = DocsConfig(core_markdown=core_md)
    return MemoryConfig(docs=docs)


def _create_test_files(tmp_path, file_specs):
    """Create test files in tmp_path. file_specs is dict of {filename: line_count}."""
    for filename, lines in file_specs.items():
        content = "\n".join([f"Line {i}" for i in range(1, lines + 1)])
        (tmp_path / filename).write_text(content)


# ---------------------------------------------------------------------------
# get_monitored_files
# ---------------------------------------------------------------------------

class TestGetMonitoredFiles:
    """Tests for get_monitored_files() config reading and fallback."""

    def test_returns_config_files_when_available(self):
        """When config has coreMarkdown.files, use them."""
        files = {
            "AGENTS.md": {"purpose": "System ops", "maxLines": 350},
            "SOUL.md": {"purpose": "Personality", "maxLines": 80},
        }
        cfg = _make_config_with_core_md(files=files)

        with patch("core.lifecycle.workspace_audit.get_config", return_value=cfg):
            from core.lifecycle.workspace_audit import get_monitored_files
            result = get_monitored_files()
            assert "AGENTS.md" in result
            assert "SOUL.md" in result
            assert result["AGENTS.md"]["maxLines"] == 350
            assert result["SOUL.md"]["purpose"] == "Personality"

    def test_fallback_when_config_missing(self):
        """When config loading fails and no gateway globs, falls back to hardcoded list."""
        with patch("core.lifecycle.workspace_audit.get_config", side_effect=Exception("config not found")), \
             patch("core.lifecycle.workspace_audit.get_bootstrap_markdown_globs", return_value=[]):
            from core.lifecycle.workspace_audit import get_monitored_files
            result = get_monitored_files()
            # Fallback should have the standard files
            assert "AGENTS.md" in result
            assert "SOUL.md" in result
            assert "TOOLS.md" in result
            assert "USER.md" in result
            assert "ENVIRONMENT.md" in result
            assert "IDENTITY.md" in result
            assert "HEARTBEAT.md" in result
            assert "TODO.md" in result

    def test_fallback_when_files_empty(self):
        """Empty files dict in config with no gateway globs triggers fallback."""
        cfg = _make_config_with_core_md(files={})

        with patch("core.lifecycle.workspace_audit.get_config", return_value=cfg), \
             patch("core.lifecycle.workspace_audit.get_bootstrap_markdown_globs", return_value=[]):
            from core.lifecycle.workspace_audit import get_monitored_files
            result = get_monitored_files()
            # Empty files + no bootstrap → falls through to fallback
            assert "AGENTS.md" in result

    def test_fallback_has_correct_max_lines(self):
        """Hardcoded fallback has sensible maxLines defaults."""
        with patch("core.lifecycle.workspace_audit.get_config", side_effect=Exception("err")), \
             patch("core.lifecycle.workspace_audit.get_bootstrap_markdown_globs", return_value=[]):
            from core.lifecycle.workspace_audit import get_monitored_files
            result = get_monitored_files()
            assert result["SOUL.md"]["maxLines"] == 80
            assert result["IDENTITY.md"]["maxLines"] == 20
            assert result["AGENTS.md"]["maxLines"] == 350

    def test_config_without_core_markdown_attr(self):
        """Config object without docs.core_markdown triggers fallback."""
        # Build a config where docs has no core_markdown attribute
        cfg = MagicMock()
        cfg.docs = MagicMock(spec=[])  # spec=[] means no attributes
        del cfg.docs.core_markdown

        with patch("core.lifecycle.workspace_audit.get_config", return_value=cfg), \
             patch("core.lifecycle.workspace_audit.get_bootstrap_markdown_globs", return_value=[]):
            from core.lifecycle.workspace_audit import get_monitored_files
            result = get_monitored_files()
            assert "AGENTS.md" in result  # fallback

    def test_bootstrap_globs_add_discovered_files(self):
        """Bootstrap globs from gateway config add project-level files."""
        cfg = _make_config_with_core_md(files={
            "SOUL.md": {"purpose": "Personality", "maxLines": 80},
        })

        with patch("core.lifecycle.workspace_audit.get_config", return_value=cfg), \
             patch("core.lifecycle.workspace_audit.get_bootstrap_markdown_globs",
                   return_value=["projects/*/TOOLS.md"]):
            from core.lifecycle.workspace_audit import get_monitored_files
            result = get_monitored_files()
            assert "SOUL.md" in result
            # Should include any matching project TOOLS.md files
            bootstrap_files = [k for k in result if k.startswith("projects/")]
            for bf in bootstrap_files:
                assert result[bf]["maxLines"] == 100  # _BOOTSTRAP_MAX_LINES

    def test_bootstrap_globs_resolve_from_visible_home(self, tmp_path):
        """Bootstrap discovery uses the visible Quaid root, not the hidden instance root."""
        cfg = _make_config_with_core_md(files={})
        hidden_root = tmp_path / ".quaid"
        visible_root = tmp_path / "quaid"
        (hidden_root / "instances" / "pytest-runner").mkdir(parents=True)
        (hidden_root / "instances" / "pytest-runner" / "config.json").write_text("{}")
        tools_path = visible_root / "projects" / "demo" / "TOOLS.md"
        tools_path.parent.mkdir(parents=True, exist_ok=True)
        tools_path.write_text("# Demo Tools\n", encoding="utf-8")

        with patch("core.lifecycle.workspace_audit.get_config", return_value=cfg), \
             patch("core.lifecycle.workspace_audit.get_bootstrap_markdown_globs",
                   return_value=["projects/*/TOOLS.md"]), \
             patch("core.lifecycle.workspace_audit.get_visible_quaid_home", return_value=visible_root):
            from core.lifecycle.workspace_audit import get_monitored_files
            result = get_monitored_files()
            assert "projects/demo/TOOLS.md" in result
            assert result["projects/demo/TOOLS.md"]["maxLines"] == 100


# ---------------------------------------------------------------------------
# check_bloat
# ---------------------------------------------------------------------------

class TestCheckBloat:
    """Tests for check_bloat() — line count vs maxLines comparison."""

    def test_under_limit(self, tmp_path):
        """Files under their maxLines are not flagged."""
        files_config = {
            "SOUL.md": {"purpose": "Personality", "maxLines": 80},
            "AGENTS.md": {"purpose": "System ops", "maxLines": 350},
        }
        cfg = _make_config_with_core_md(files=files_config)

        with patch("core.lifecycle.workspace_audit.get_config", return_value=cfg), \
             _adapter_patch(tmp_path) as iroot:
            _create_test_files(iroot, {"SOUL.md": 50, "AGENTS.md": 200})
            from core.lifecycle.workspace_audit import check_bloat
            stats = check_bloat()

            assert stats["SOUL.md"]["lines"] == 50
            assert stats["SOUL.md"]["maxLines"] == 80
            assert stats["SOUL.md"]["over_limit"] is False

            assert stats["AGENTS.md"]["lines"] == 200
            assert stats["AGENTS.md"]["over_limit"] is False

    def test_over_limit(self, tmp_path):
        """Files over their maxLines are flagged."""
        files_config = {
            "SOUL.md": {"purpose": "Personality", "maxLines": 80},
        }
        cfg = _make_config_with_core_md(files=files_config)

        with patch("core.lifecycle.workspace_audit.get_config", return_value=cfg), \
             _adapter_patch(tmp_path) as iroot:
            _create_test_files(iroot, {"SOUL.md": 100})
            from core.lifecycle.workspace_audit import check_bloat
            stats = check_bloat()

            assert stats["SOUL.md"]["lines"] == 100
            assert stats["SOUL.md"]["maxLines"] == 80
            assert stats["SOUL.md"]["over_limit"] is True

    def test_exactly_at_limit(self, tmp_path):
        """File at exactly maxLines is NOT over limit."""
        files_config = {
            "TODO.md": {"purpose": "Tasks", "maxLines": 150},
        }
        cfg = _make_config_with_core_md(files=files_config)

        with patch("core.lifecycle.workspace_audit.get_config", return_value=cfg), \
             _adapter_patch(tmp_path) as iroot:
            _create_test_files(iroot, {"TODO.md": 150})
            from core.lifecycle.workspace_audit import check_bloat
            stats = check_bloat()

            assert stats["TODO.md"]["lines"] == 150
            assert stats["TODO.md"]["over_limit"] is False

    def test_missing_file_reports_zero_lines(self, tmp_path):
        """A monitored file that doesn't exist reports 0 lines."""
        # Don't create MISSING.md
        files_config = {
            "MISSING.md": {"purpose": "Does not exist", "maxLines": 100},
        }
        cfg = _make_config_with_core_md(files=files_config)

        with patch("core.lifecycle.workspace_audit.get_config", return_value=cfg), \
             _adapter_patch(tmp_path):
            from core.lifecycle.workspace_audit import check_bloat
            stats = check_bloat()

            assert stats["MISSING.md"]["lines"] == 0
            assert stats["MISSING.md"]["over_limit"] is False

    def test_purpose_included_in_stats(self, tmp_path):
        """Each entry includes the purpose string."""
        files_config = {
            "SOUL.md": {"purpose": "Personality, vibe, values", "maxLines": 80},
        }
        cfg = _make_config_with_core_md(files=files_config)

        with patch("core.lifecycle.workspace_audit.get_config", return_value=cfg), \
             _adapter_patch(tmp_path) as iroot:
            _create_test_files(iroot, {"SOUL.md": 10})
            from core.lifecycle.workspace_audit import check_bloat
            stats = check_bloat()

            assert stats["SOUL.md"]["purpose"] == "Personality, vibe, values"

    def test_multiple_files_mixed_status(self, tmp_path):
        """Mix of under-limit and over-limit files."""
        files_config = {
            "SOUL.md": {"purpose": "Personality", "maxLines": 80},
            "IDENTITY.md": {"purpose": "Identity", "maxLines": 20},
            "TOOLS.md": {"purpose": "API docs", "maxLines": 350},
            "USER.md": {"purpose": "User info", "maxLines": 150},
        }
        cfg = _make_config_with_core_md(files=files_config)

        with patch("core.lifecycle.workspace_audit.get_config", return_value=cfg), \
             _adapter_patch(tmp_path) as iroot:
            _create_test_files(iroot, {
                "SOUL.md": 90,       # over 80
                "IDENTITY.md": 15,   # under 20
                "TOOLS.md": 400,     # over 350
                "USER.md": 100,      # under 150
            })
            from core.lifecycle.workspace_audit import check_bloat
            stats = check_bloat()

            assert stats["SOUL.md"]["over_limit"] is True
            assert stats["IDENTITY.md"]["over_limit"] is False
            assert stats["TOOLS.md"]["over_limit"] is True
            assert stats["USER.md"]["over_limit"] is False

    def test_no_max_lines_defaults_to_999(self, tmp_path):
        """File entry without maxLines uses 999 as default."""
        files_config = {
            "NOTES.md": {"purpose": "Random notes"},  # no maxLines key
        }
        cfg = _make_config_with_core_md(files=files_config)

        with patch("core.lifecycle.workspace_audit.get_config", return_value=cfg), \
             _adapter_patch(tmp_path) as iroot:
            _create_test_files(iroot, {"NOTES.md": 500})
            from core.lifecycle.workspace_audit import check_bloat
            stats = check_bloat()

            assert stats["NOTES.md"]["maxLines"] == 999
            assert stats["NOTES.md"]["over_limit"] is False


# ---------------------------------------------------------------------------
# get_file_line_counts
# ---------------------------------------------------------------------------

class TestGetFileLineCounts:
    """Tests for get_file_line_counts()."""

    def test_counts_correct(self, tmp_path):
        """Line counts match actual file content."""
        files_config = {
            "A.md": {"purpose": "A", "maxLines": 100},
            "B.md": {"purpose": "B", "maxLines": 100},
        }
        cfg = _make_config_with_core_md(files=files_config)

        with patch("core.lifecycle.workspace_audit.get_config", return_value=cfg), \
             _adapter_patch(tmp_path) as iroot:
            _create_test_files(iroot, {"A.md": 10, "B.md": 25})
            from core.lifecycle.workspace_audit import get_file_line_counts
            counts = get_file_line_counts()
            assert counts["A.md"] == 10
            assert counts["B.md"] == 25

    def test_missing_file_not_in_counts(self, tmp_path):
        """Missing files are excluded from counts dict."""
        files_config = {
            "GHOST.md": {"purpose": "Nonexistent", "maxLines": 100},
        }
        cfg = _make_config_with_core_md(files=files_config)

        with patch("core.lifecycle.workspace_audit.get_config", return_value=cfg), \
             _adapter_patch(tmp_path):
            from core.lifecycle.workspace_audit import get_file_line_counts
            counts = get_file_line_counts()
            assert "GHOST.md" not in counts

    def test_empty_file_has_zero_lines(self, tmp_path):
        """Empty file has 0 lines."""
        files_config = {
            "EMPTY.md": {"purpose": "Empty", "maxLines": 100},
        }
        cfg = _make_config_with_core_md(files=files_config)

        with patch("core.lifecycle.workspace_audit.get_config", return_value=cfg), \
             _adapter_patch(tmp_path) as iroot:
            (iroot / "EMPTY.md").write_text("")
            from core.lifecycle.workspace_audit import get_file_line_counts
            counts = get_file_line_counts()
            # "".splitlines() returns [] so len is 0
            assert counts["EMPTY.md"] == 0


# ---------------------------------------------------------------------------
# build_review_prompt
# ---------------------------------------------------------------------------

class TestBuildReviewPrompt:
    """Tests for build_review_prompt()."""

    def test_includes_file_purposes(self):
        from core.lifecycle.workspace_audit import build_review_prompt
        files_config = {
            "SOUL.md": {"purpose": "Personality and vibe", "maxLines": 80},
            "TOOLS.md": {"purpose": "API documentation", "maxLines": 350},
        }
        prompt = build_review_prompt(files_config)
        assert "SOUL.md" in prompt
        assert "Personality and vibe" in prompt
        assert "max 80 lines" in prompt
        assert "TOOLS.md" in prompt
        assert "API documentation" in prompt

    def test_includes_action_types(self):
        from core.lifecycle.workspace_audit import build_review_prompt
        prompt = build_review_prompt({"X.md": {"purpose": "test", "maxLines": 10}})
        assert "KEEP" in prompt
        assert "MOVE_TO_PROJECT" in prompt
        assert "MOVE_TO_MEMORY" in prompt
        assert "TRIM" in prompt
        assert "FLAG_BLOAT" in prompt

    def test_includes_json_format(self):
        from core.lifecycle.workspace_audit import build_review_prompt
        prompt = build_review_prompt({"X.md": {"purpose": "test", "maxLines": 10}})
        assert "file_stats" in prompt
        assert "decisions" in prompt


# ---------------------------------------------------------------------------
# detect_changed_files
# ---------------------------------------------------------------------------

class TestDetectChangedFiles:
    """Tests for detect_changed_files()."""

    def test_no_previous_mtimes_all_changed(self, tmp_path):
        """With no saved mtimes, all files are changed."""
        files_config = {
            "A.md": {"purpose": "A", "maxLines": 100},
            "B.md": {"purpose": "B", "maxLines": 100},
        }
        cfg = _make_config_with_core_md(files=files_config)

        with patch("core.lifecycle.workspace_audit.get_config", return_value=cfg), \
             _adapter_patch(tmp_path) as iroot, \
             patch("core.lifecycle.workspace_audit.load_last_mtimes", return_value={}):
            _create_test_files(iroot, {"A.md": 5, "B.md": 10})
            from core.lifecycle.workspace_audit import detect_changed_files
            changed = detect_changed_files()
            assert "A.md" in changed
            assert "B.md" in changed

    def test_unchanged_files_not_reported(self, tmp_path):
        """Files with same mtime as last run are not changed."""
        files_config = {
            "A.md": {"purpose": "A", "maxLines": 100},
        }
        cfg = _make_config_with_core_md(files=files_config)

        with patch("core.lifecycle.workspace_audit.get_config", return_value=cfg), \
             _adapter_patch(tmp_path) as iroot:
            _create_test_files(iroot, {"A.md": 5})
            current_mtime = (iroot / "A.md").stat().st_mtime

            with patch("core.lifecycle.workspace_audit.load_last_mtimes", return_value={"A.md": current_mtime}):
                from core.lifecycle.workspace_audit import detect_changed_files
                changed = detect_changed_files()
                assert "A.md" not in changed


class TestMtimePersistence:
    def test_load_last_mtimes_warns_and_returns_empty_when_fail_open(self, tmp_path, caplog):
        from core.lifecycle.workspace_audit import load_last_mtimes

        with _hidden_adapter_patch(tmp_path) as iroot, \
             patch("core.lifecycle.workspace_audit.is_fail_hard_enabled", return_value=False):
            mtime_file = iroot / "logs" / "janitor" / "workspace-mtimes.json"
            mtime_file.parent.mkdir(parents=True, exist_ok=True)
            mtime_file.write_text("{broken json", encoding="utf-8")
            with caplog.at_level("WARNING", logger="core.lifecycle.workspace_audit"):
                assert load_last_mtimes() == {}

        assert "Failed to load workspace mtimes" in caplog.text

    def test_load_last_mtimes_raises_when_fail_hard(self, tmp_path):
        from core.lifecycle.workspace_audit import load_last_mtimes

        with _hidden_adapter_patch(tmp_path) as iroot, \
             patch("core.lifecycle.workspace_audit.is_fail_hard_enabled", return_value=True):
            mtime_file = iroot / "logs" / "janitor" / "workspace-mtimes.json"
            mtime_file.parent.mkdir(parents=True, exist_ok=True)
            mtime_file.write_text("{broken json", encoding="utf-8")
            with pytest.raises(json.JSONDecodeError):
                load_last_mtimes()

    def test_save_mtimes_uses_file_lock(self, tmp_path):
        from core.lifecycle.workspace_audit import save_mtimes

        with _adapter_patch(tmp_path), \
             patch("core.lifecycle.workspace_audit.fcntl.flock") as mock_flock:
            save_mtimes({"A.md": 123.0})

        assert mock_flock.call_count >= 2

    def test_save_mtimes_honors_quaid_now(self, tmp_path, monkeypatch):
        from core.lifecycle.workspace_audit import save_mtimes

        monkeypatch.setenv("QUAID_NOW", "2026-02-03T04:05:06Z")
        with _hidden_adapter_patch(tmp_path) as iroot:
            save_mtimes({"A.md": 123.0})

            payload = json.loads((iroot / "logs" / "janitor" / "workspace-mtimes.json").read_text())

        assert payload["updated_at"] == "2026-02-03T04:05:06+00:00"

    def test_workspace_audit_now_rejects_malformed_quaid_now(self, monkeypatch):
        from core.lifecycle import workspace_audit as wa

        monkeypatch.setenv("QUAID_NOW", "not-a-date")

        with pytest.raises(ValueError, match="Invalid QUAID_NOW"):
            wa._now_iso()

    def test_save_mtimes_truncates_only_after_lock(self, tmp_path):
        import builtins

        from core.lifecycle.workspace_audit import save_mtimes

        operations = []
        real_open = builtins.open

        class _TrackedFile:
            def __init__(self, wrapped):
                self._wrapped = wrapped

            def __enter__(self):
                self._wrapped.__enter__()
                return self

            def __exit__(self, exc_type, exc, tb):
                return self._wrapped.__exit__(exc_type, exc, tb)

            def fileno(self):
                return self._wrapped.fileno()

            def seek(self, *args, **kwargs):
                operations.append("seek")
                return self._wrapped.seek(*args, **kwargs)

            def truncate(self, *args, **kwargs):
                operations.append("truncate")
                return self._wrapped.truncate(*args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._wrapped, name)

        def _tracked_open(path, mode="r", *args, **kwargs):
            opened = real_open(path, mode, *args, **kwargs)
            if str(path).endswith("workspace-mtimes.json"):
                operations.append(f"open:{mode}")
                return _TrackedFile(opened)
            return opened

        def _flock(_fd, mode):
            import fcntl

            if mode & fcntl.LOCK_EX:
                operations.append("lock")
            elif mode & fcntl.LOCK_UN:
                operations.append("unlock")

        with _adapter_patch(tmp_path), \
             patch("core.lifecycle.workspace_audit.open", _tracked_open, create=True), \
             patch("core.lifecycle.workspace_audit.fcntl.flock", _flock):
            save_mtimes({"A.md": 123.0})

        assert operations[:4] == ["open:a+", "lock", "seek", "truncate"]


class TestWorkspaceBackups:
    def test_backup_workspace_files_honors_quaid_now(self, tmp_path, monkeypatch):
        from core.lifecycle.workspace_audit import backup_workspace_files

        cfg = _make_config_with_core_md(files={"A.md": {"purpose": "A", "maxLines": 100}})
        monkeypatch.setenv("QUAID_NOW", "2026-02-03T04:05:06Z")
        with _adapter_patch(tmp_path) as iroot, \
             patch("core.lifecycle.workspace_audit.get_config", return_value=cfg):
            (iroot / "A.md").write_text("# A\n", encoding="utf-8")
            backups = backup_workspace_files(["A.md"])

        assert len(backups) == 1
        backup_name = Path(next(iter(backups.values()))).name
        assert backup_name == "A.md.2026-02-03T04-05-06.bak"


class TestReviewDecisionApplyLocking:
    def test_apply_review_decisions_warns_on_corrupt_file_when_fail_open(self, tmp_path, caplog):
        from core.lifecycle.workspace_audit import apply_review_decisions

        with _hidden_adapter_patch(tmp_path) as iroot, \
             patch("core.lifecycle.workspace_audit.is_fail_hard_enabled", return_value=False):
            decisions_file = iroot / "logs" / "janitor" / "workspace-review-decisions.json"
            decisions_file.parent.mkdir(parents=True, exist_ok=True)
            decisions_file.write_text("{broken json", encoding="utf-8")
            with caplog.at_level("WARNING", logger="core.lifecycle.workspace_audit"):
                stats = apply_review_decisions(dry_run=True)

        assert stats == {
            "moved_to_docs": 0,
            "moved_to_memory": 0,
            "trimmed": 0,
            "bloat_warnings": 0,
            "project_detected": 0,
            "errors": 0,
        }
        assert "Failed to load workspace audit review decisions" in caplog.text

    def test_apply_review_decisions_raises_corrupt_file_when_fail_hard(self, tmp_path):
        from core.lifecycle.workspace_audit import apply_review_decisions

        with _hidden_adapter_patch(tmp_path) as iroot, \
             patch("core.lifecycle.workspace_audit.is_fail_hard_enabled", return_value=True):
            decisions_file = iroot / "logs" / "janitor" / "workspace-review-decisions.json"
            decisions_file.parent.mkdir(parents=True, exist_ok=True)
            decisions_file.write_text("{broken json", encoding="utf-8")
            with pytest.raises(json.JSONDecodeError):
                apply_review_decisions(dry_run=True)

    def test_apply_review_decisions_locks_file_transaction(self, tmp_path):
        from core.lifecycle.workspace_audit import apply_review_decisions

        cfg = _make_config_with_core_md(files={"A.md": {"purpose": "A", "maxLines": 100}})
        decisions_data = {
            "decisions": [
                {
                    "file": "A.md",
                    "section": "Trim Me",
                    "action": "TRIM",
                    "reason": "cleanup",
                }
            ]
        }

        with _adapter_patch(tmp_path) as iroot, \
             patch("core.lifecycle.workspace_audit.get_config", return_value=cfg), \
             patch("core.lifecycle.workspace_audit.save_mtimes"), \
             patch("core.lifecycle.workspace_audit.fcntl.flock") as mock_flock:
            path = iroot / "A.md"
            if path.exists() or path.is_symlink():
                path.unlink()
            path.write_text("# A\n\n## Trim Me\n\nold content\n")
            stats = apply_review_decisions(dry_run=False, decisions_data=decisions_data)

        lock_modes = [call.args[1] for call in mock_flock.call_args_list if len(call.args) >= 2]
        assert stats["trimmed"] == 1
        assert "old content" not in (iroot / "A.md").read_text()
        assert sum(1 for mode in lock_modes if mode & fcntl.LOCK_EX) >= 1
        assert lock_modes.count(fcntl.LOCK_UN) >= 1

    def test_move_to_docs_migration_note_honors_quaid_now(self, tmp_path, monkeypatch):
        from core.lifecycle.workspace_audit import apply_review_decisions

        cfg = _make_config_with_core_md(files={"A.md": {"purpose": "A", "maxLines": 100}})
        decisions_data = {
            "decisions": [
                {
                    "file": "A.md",
                    "section": "Move Me",
                    "action": "MOVE_TO_DOCS",
                    "target": "docs/move-me.md",
                    "reason": "belongs in docs",
                }
            ]
        }

        monkeypatch.setenv("QUAID_NOW", "2026-02-03T04:05:06Z")
        with _adapter_patch(tmp_path) as iroot, \
             patch("core.lifecycle.workspace_audit.get_config", return_value=cfg), \
             patch("core.lifecycle.workspace_audit.save_mtimes"):
            (iroot / "A.md").write_text("# A\n\n## Move Me\n\ncontent\n", encoding="utf-8")
            stats = apply_review_decisions(dry_run=False, decisions_data=decisions_data)
            doc_text = (iroot / "docs" / "move-me.md").read_text(encoding="utf-8")

        assert stats["moved_to_docs"] == 1
        assert "> Migrated from `A.md` on 2026-02-03" in doc_text

    def test_apply_review_decisions_blocks_traversal_filename(self, tmp_path):
        from core.lifecycle.workspace_audit import apply_review_decisions

        outside = tmp_path / "outside.md"
        outside.write_text("# A\n\n## Trim Me\n\noutside content\n", encoding="utf-8")
        decisions_data = {
            "decisions": [
                {
                    "file": "../outside.md",
                    "section": "Trim Me",
                    "action": "TRIM",
                    "reason": "malicious path",
                }
            ]
        }

        with _adapter_patch(tmp_path), \
             patch("core.lifecycle.workspace_audit.save_mtimes"):
            stats = apply_review_decisions(dry_run=False, decisions_data=decisions_data)

        assert stats["errors"] == 1
        assert "outside content" in outside.read_text(encoding="utf-8")

    def test_apply_review_decisions_blocks_symlink_escape(self, tmp_path):
        from core.lifecycle.workspace_audit import apply_review_decisions

        decisions_data = {
            "decisions": [
                {
                    "file": "A.md",
                    "section": "Trim Me",
                    "action": "TRIM",
                    "reason": "symlink escape",
                }
            ]
        }

        with _adapter_patch(tmp_path) as iroot, \
             patch("core.lifecycle.workspace_audit.save_mtimes"):
            outside = tmp_path / "outside.md"
            outside.write_text("# A\n\n## Trim Me\n\noutside content\n", encoding="utf-8")
            link = iroot / "A.md"
            link.parent.mkdir(parents=True, exist_ok=True)
            if link.exists() or link.is_symlink():
                link.unlink()
            link.symlink_to(outside)

            stats = apply_review_decisions(dry_run=False, decisions_data=decisions_data)
            if link.is_symlink():
                link.unlink()

        assert stats["errors"] == 1
        assert "outside content" in outside.read_text(encoding="utf-8")

    def test_workspace_file_lock_times_out(self, tmp_path):
        from core.lifecycle.workspace_audit import _lock_file_exclusive

        target = tmp_path / "A.md"
        target.write_text("content\n", encoding="utf-8")
        monotonic_values = iter([0.0, 1.0])

        with target.open("r+", encoding="utf-8") as fh, \
             patch("core.lifecycle.workspace_audit.fcntl.flock", side_effect=BlockingIOError), \
             patch("core.lifecycle.workspace_audit.time.monotonic", side_effect=lambda: next(monotonic_values)), \
             patch("core.lifecycle.workspace_audit.time.sleep"):
            with pytest.raises(TimeoutError, match="workspace file lock"):
                _lock_file_exclusive(fh, timeout_seconds=1.0)


# ---------------------------------------------------------------------------
# _queue_project_review / get_pending_project_reviews / clear
# ---------------------------------------------------------------------------

class TestProjectReviewQueue:
    """Tests for the pending project review queue system."""

    def test_queue_creates_file(self, tmp_path):
        """Queueing a review creates the JSON file."""
        from core.lifecycle.workspace_audit import _queue_project_review

        with _hidden_adapter_patch(tmp_path) as iroot:
            (iroot / "logs" / "janitor").mkdir(parents=True, exist_ok=True)
            _queue_project_review(
                section="My API Docs",
                source_file="TOOLS.md",
                reason="Project-specific API docs",
                project_hint="REST API server",
                content_preview="## My API\nEndpoints...",
            )
            pending_file = iroot / "logs" / "janitor" / "pending-project-review.json"
            assert pending_file.exists()
            data = json.loads(pending_file.read_text())
            assert len(data) == 1
            assert data[0]["section"] == "My API Docs"
            assert data[0]["source_file"] == "TOOLS.md"
            assert data[0]["project_hint"] == "REST API server"
            assert data[0]["content_preview"] == "## My API\nEndpoints..."
            assert data[0]["reason"] == "Project-specific API docs"
            assert "timestamp" in data[0]

    def test_queue_honors_quaid_now(self, tmp_path, monkeypatch):
        from core.lifecycle.workspace_audit import _queue_project_review

        monkeypatch.setenv("QUAID_NOW", "2026-02-03T04:05:06Z")
        with _hidden_adapter_patch(tmp_path) as iroot:
            (iroot / "logs" / "janitor").mkdir(parents=True, exist_ok=True)
            _queue_project_review(section="Test", source_file="TOOLS.md")

            pending_file = iroot / "logs" / "janitor" / "pending-project-review.json"
            data = json.loads(pending_file.read_text())

        assert data[0]["timestamp"] == "2026-02-03T04:05:06+00:00"

    def test_queue_appends_to_existing(self, tmp_path):
        """Multiple queues accumulate in the same file."""
        from core.lifecycle.workspace_audit import _queue_project_review

        with _hidden_adapter_patch(tmp_path) as iroot:
            (iroot / "logs" / "janitor").mkdir(parents=True, exist_ok=True)
            _queue_project_review(section="First", source_file="TOOLS.md")
            _queue_project_review(section="Second", source_file="AGENTS.md")
            pending_file = iroot / "logs" / "janitor" / "pending-project-review.json"
            data = json.loads(pending_file.read_text())
            assert len(data) == 2
            assert data[0]["section"] == "First"
            assert data[1]["section"] == "Second"

    def test_queue_handles_corrupt_file(self, tmp_path):
        """If existing file is corrupt, starts fresh (doesn't crash)."""
        from core.lifecycle.workspace_audit import _queue_project_review

        with _hidden_adapter_patch(tmp_path) as iroot:
            (iroot / "logs" / "janitor").mkdir(parents=True, exist_ok=True)
            pending_file = iroot / "logs" / "janitor" / "pending-project-review.json"
            pending_file.write_text("{broken json")
            _queue_project_review(section="New", source_file="TOOLS.md")
            data = json.loads(pending_file.read_text())
            assert len(data) == 1
            assert data[0]["section"] == "New"

    def test_get_returns_pending(self, tmp_path):
        """get_pending_project_reviews returns queued items without deleting."""
        from core.lifecycle.workspace_audit import _queue_project_review, get_pending_project_reviews

        with _hidden_adapter_patch(tmp_path) as iroot:
            (iroot / "logs" / "janitor").mkdir(parents=True, exist_ok=True)
            _queue_project_review(section="Test", source_file="TOOLS.md")
            reviews = get_pending_project_reviews()
            assert len(reviews) == 1
            assert reviews[0]["section"] == "Test"
            pending_file = iroot / "logs" / "janitor" / "pending-project-review.json"
            assert pending_file.exists()

    def test_get_returns_empty_for_missing_file(self, tmp_path):
        """get_pending_project_reviews returns [] if no file."""
        from core.lifecycle.workspace_audit import get_pending_project_reviews

        with _hidden_adapter_patch(tmp_path):
            assert get_pending_project_reviews() == []

    def test_get_returns_empty_for_corrupt_file(self, tmp_path):
        """get_pending_project_reviews returns [] if file is corrupt."""
        from core.lifecycle.workspace_audit import get_pending_project_reviews

        with _hidden_adapter_patch(tmp_path) as iroot:
            (iroot / "logs" / "janitor").mkdir(parents=True, exist_ok=True)
            pending_file = iroot / "logs" / "janitor" / "pending-project-review.json"
            pending_file.write_text("not valid json!")
            assert get_pending_project_reviews() == []

    def test_get_logs_warning_for_corrupt_file(self, tmp_path, caplog):
        from core.lifecycle.workspace_audit import get_pending_project_reviews

        with _hidden_adapter_patch(tmp_path) as iroot:
            (iroot / "logs" / "janitor").mkdir(parents=True, exist_ok=True)
            pending_file = iroot / "logs" / "janitor" / "pending-project-review.json"
            pending_file.write_text("not valid json!")
            with caplog.at_level("WARNING"):
                assert get_pending_project_reviews() == []
        assert any("pending project reviews" in rec.message for rec in caplog.records)

    def test_clear_deletes_file(self, tmp_path):
        """clear_pending_project_reviews removes the file."""
        from core.lifecycle.workspace_audit import _queue_project_review, clear_pending_project_reviews

        with _hidden_adapter_patch(tmp_path) as iroot:
            (iroot / "logs" / "janitor").mkdir(parents=True, exist_ok=True)
            _queue_project_review(section="Test", source_file="TOOLS.md")
            pending_file = iroot / "logs" / "janitor" / "pending-project-review.json"
            assert pending_file.exists()
            clear_pending_project_reviews()
            assert not pending_file.exists()

    def test_clear_idempotent(self, tmp_path):
        """clear_pending_project_reviews is safe on missing file."""
        from core.lifecycle.workspace_audit import clear_pending_project_reviews

        with _hidden_adapter_patch(tmp_path):
            clear_pending_project_reviews()  # Should not raise

    def test_queue_rejects_unknown_kwargs(self):
        """_queue_project_review raises TypeError on unknown keyword args."""
        from core.lifecycle.workspace_audit import _queue_project_review

        with pytest.raises(TypeError):
            _queue_project_review(
                section="X", source_file="Y", bogus_arg="should fail"
            )


class TestDefaultOwner:
    def test_default_owner_fallback_when_fail_hard_disabled(self):
        from core.lifecycle.workspace_audit import _default_owner_id
        with patch("core.lifecycle.workspace_audit.get_config", return_value=object()), \
             patch("core.lifecycle.workspace_audit.is_fail_hard_enabled", return_value=False):
            assert _default_owner_id() == "default"

    def test_default_owner_raises_when_fail_hard_enabled(self):
        from core.lifecycle.workspace_audit import _default_owner_id
        with patch("core.lifecycle.workspace_audit.get_config", return_value=object()), \
             patch("core.lifecycle.workspace_audit.is_fail_hard_enabled", return_value=True):
            with pytest.raises(RuntimeError, match="default owner"):
                _default_owner_id()


# ---------------------------------------------------------------------------
# section_overlaps_protected
# ---------------------------------------------------------------------------

class TestSectionOverlapsProtected:
    """Tests for the section-range protected region check."""

    def test_no_overlap(self):
        from lib.markdown import section_overlaps_protected
        # Protected: [100, 200], section: [0, 50]
        assert not section_overlaps_protected(0, 50, [(100, 200)])

    def test_section_inside_protected(self):
        from lib.markdown import section_overlaps_protected
        # Protected: [0, 200], section: [50, 100]
        assert section_overlaps_protected(50, 100, [(0, 200)])

    def test_protected_inside_section(self):
        from lib.markdown import section_overlaps_protected
        # Protected: [50, 100], section: [0, 200]
        assert section_overlaps_protected(0, 200, [(50, 100)])

    def test_partial_overlap_start(self):
        from lib.markdown import section_overlaps_protected
        # Protected: [50, 150], section: [0, 100]
        assert section_overlaps_protected(0, 100, [(50, 150)])

    def test_partial_overlap_end(self):
        from lib.markdown import section_overlaps_protected
        # Protected: [0, 100], section: [50, 200]
        assert section_overlaps_protected(50, 200, [(0, 100)])

    def test_adjacent_no_overlap(self):
        from lib.markdown import section_overlaps_protected
        # Protected: [0, 50], section: [50, 100] — adjacent but not overlapping
        assert not section_overlaps_protected(50, 100, [(0, 50)])

    def test_multiple_ranges_one_overlaps(self):
        from lib.markdown import section_overlaps_protected
        # Two protected ranges, second one overlaps
        assert section_overlaps_protected(150, 250, [(0, 50), (200, 300)])


def test_run_workspace_check_passes_configurable_llm_timeout(monkeypatch):
    from core.lifecycle import workspace_audit as wa

    cfg = SimpleNamespace(
        janitor=SimpleNamespace(opus_review=SimpleNamespace(max_tokens=256)),
        docs=SimpleNamespace(update_timeout_seconds=77),
    )
    captured: dict[str, float] = {}

    def _fake_call(prompt, system_prompt, max_tokens, timeout):
        captured["timeout"] = float(timeout)
        return ('{"decisions": []}', 0.01)

    monkeypatch.setenv("QUAID_WORKSPACE_AUDIT_TIMEOUT_SECONDS", "33")
    monkeypatch.setattr(wa, "get_config", lambda: cfg)
    monkeypatch.setattr(wa, "get_monitored_files", lambda: {"AGENTS.md": {"purpose": "ops", "maxLines": 10}})
    monkeypatch.setattr(wa, "check_bloat", lambda: {"AGENTS.md": {"over_limit": False, "lines": 1, "maxLines": 10}})
    monkeypatch.setattr(wa, "detect_changed_files", lambda: ["AGENTS.md"])
    monkeypatch.setattr(wa, "_read_file_contents", lambda files: {"AGENTS.md": "line 1\nline 2"})
    monkeypatch.setattr(wa, "build_review_prompt", lambda _cfg: "prompt")
    monkeypatch.setattr(wa, "call_deep_reasoning", _fake_call)
    monkeypatch.setattr(wa, "parse_json_response", lambda _text: {"decisions": []})
    monkeypatch.setattr(
        wa,
        "apply_review_decisions",
        lambda dry_run, decisions_data=None: {
            "moved_to_docs": 0,
            "moved_to_memory": 0,
            "trimmed": 0,
            "bloat_warnings": 0,
            "project_detected": 0,
            "kept": 0,
            "errors": 0,
        },
    )

    wa.run_workspace_check(dry_run=True)
    assert captured["timeout"] == 33.0


class TestMoveToDocsTargetSanitization:
    def test_accepts_valid_docs_target(self):
        from core.lifecycle.workspace_audit import _sanitize_move_to_docs_target

        assert _sanitize_move_to_docs_target("docs/architecture/overview.md") == "docs/architecture/overview.md"

    def test_rejects_unsafe_docs_target(self):
        from core.lifecycle.workspace_audit import _sanitize_move_to_docs_target

        assert _sanitize_move_to_docs_target("../outside.md") is None
        assert _sanitize_move_to_docs_target("docs/../../outside.md") is None
        assert _sanitize_move_to_docs_target("docs/evil?.md") is None
        assert _sanitize_move_to_docs_target("C:/tmp/pwn.md") is None
        assert _sanitize_move_to_docs_target("notes/other.md") is None
