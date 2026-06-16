"""Tests for soul_snippets.py — Journal System (evolved from soul snippets v1)."""

import importlib
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Ensure modules/quaid is on the path
sys.path.insert(0, str(Path(__file__).parent.parent))

@pytest.fixture(autouse=True)
def workspace_dir(tmp_path, monkeypatch):
    """Create a temporary workspace for each test."""
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    from lib.adapter import set_adapter, reset_adapter, TestAdapter
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)

    iroot = adapter.instance_root()
    identity_root = iroot / "identity"
    identity_root.mkdir(parents=True, exist_ok=True)
    adapter.identity_dir = lambda: identity_root
    visible_projects = tmp_path / "projects"
    visible_projects.mkdir(parents=True, exist_ok=True)
    projects_link = iroot / "projects"
    if not projects_link.exists():
        projects_link.symlink_to(visible_projects, target_is_directory=True)
    yield iroot

    reset_adapter()


def test_insightdb_is_canonical_soul_snippets_module():
    canonical = importlib.import_module("datastore.insightdb.soul_snippets")
    from datastore.insightdb import soul_snippets as package_module
    from core.lifecycle import soul_snippets as lifecycle_soul_snippets

    assert package_module is canonical
    assert lifecycle_soul_snippets.run_soul_snippets_review is canonical.run_soul_snippets_review
    assert lifecycle_soul_snippets.run_journal_distillation is canonical.run_journal_distillation


def test_legacy_soul_snippets_modules_are_removed():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("datastore.evolutiondb.soul_snippets")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("datastore.notedb.soul_snippets")


def test_lifecycle_soul_snippets_uses_canonical_insightdb_module(monkeypatch):
    canonical = importlib.import_module("datastore.insightdb.soul_snippets")
    from core.lifecycle import soul_snippets as lifecycle_soul_snippets

    calls = []

    def _fake_write(filename, snippets, **kwargs):
        calls.append((filename, snippets, kwargs))
        return True

    monkeypatch.setattr(canonical, "write_snippet_entry", _fake_write)

    assert canonical.write_snippet_entry is _fake_write
    assert lifecycle_soul_snippets.write_snippet_entry("USER.md", ["compat works"])
    assert calls == [
        (
            "USER.md",
            ["compat works"],
            {"trigger": "Compaction", "date_str": None, "time_str": None},
        )
    ]


def test_legacy_contract_modules_are_removed():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("core.plugins.evolutiondb_contract")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("core.plugins.notedb_contract")


def test_insightdb_contract_uses_canonical_module(monkeypatch):
    canonical = importlib.import_module("core.plugins.insightdb_contract")

    calls = []

    def _fake_write(payload):
        calls.append(payload)
        return {
            "status": "ok",
            "target_files": {"snippets": [], "journal": []},
            "errors": [],
        }

    monkeypatch.setattr(canonical, "run_snippet_journal_write_payload", _fake_write)

    assert canonical.run_snippet_journal_write_payload is _fake_write
    result = canonical.run_snippet_journal_write_payload({"source": "extraction-apply-payloads"})
    assert result["status"] == "ok"
    assert calls == [{"source": "extraction-apply-payloads"}]


def test_insightdb_contract_fail_hard_wrapper_fails_closed_on_import_error(monkeypatch):
    import builtins
    import core.plugins.insightdb_contract as contract

    original_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "lib.fail_policy":
            raise ImportError("missing fail policy")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert contract._fail_hard_enabled() is True


def test_identity_dir_failure_warns_and_falls_back_when_fail_open(monkeypatch, caplog):
    import datastore.insightdb.soul_snippets as soul_snippets

    monkeypatch.setattr(
        soul_snippets,
        "_runtime_identity_dir",
        lambda: (_ for _ in ()).throw(RuntimeError("identity dir broken")),
    )
    monkeypatch.setattr(soul_snippets, "is_fail_hard_enabled", lambda: False)

    with caplog.at_level(logging.WARNING, logger="datastore.insightdb.soul_snippets"):
        assert soul_snippets._identity_dir() == soul_snippets._visible_instance_dir()

    assert "Failed to resolve runtime identity dir" in caplog.text
    assert "identity dir broken" in caplog.text


def test_identity_dir_failure_raises_when_failhard(monkeypatch):
    import datastore.insightdb.soul_snippets as soul_snippets

    monkeypatch.setattr(
        soul_snippets,
        "_runtime_identity_dir",
        lambda: (_ for _ in ()).throw(RuntimeError("identity dir broken")),
    )
    monkeypatch.setattr(soul_snippets, "is_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="Failed to resolve runtime identity dir") as excinfo:
        soul_snippets._identity_dir()

    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert "identity dir broken" in str(excinfo.value.__cause__)


def test_journal_config_failure_warns_when_fail_open(monkeypatch, caplog):
    import datastore.insightdb.soul_snippets as soul_snippets

    monkeypatch.setattr(
        soul_snippets,
        "get_config",
        lambda: (_ for _ in ()).throw(RuntimeError("config broken")),
    )
    monkeypatch.setattr(soul_snippets, "is_fail_hard_enabled", lambda: False)

    with caplog.at_level(logging.WARNING, logger="datastore.insightdb.soul_snippets"):
        assert soul_snippets._get_journal_config() is None

    assert "Failed loading journal config from memory config" in caplog.text
    assert "config broken" in caplog.text


def test_journal_config_failure_raises_when_failhard(monkeypatch):
    import datastore.insightdb.soul_snippets as soul_snippets

    monkeypatch.setattr(
        soul_snippets,
        "get_config",
        lambda: (_ for _ in ()).throw(RuntimeError("config broken")),
    )
    monkeypatch.setattr(soul_snippets, "is_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="Journal config read failed") as excinfo:
        soul_snippets._get_journal_config()

    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert "config broken" in str(excinfo.value.__cause__)


@pytest.fixture
def mock_config():
    """Mock config with journal enabled."""
    from config import JournalConfig, CoreMarkdownConfig

    mock_cfg = MagicMock()
    mock_cfg.docs.journal = JournalConfig(
        enabled=True,
        snippets_enabled=True,
        mode="distilled",
        journal_dir="journal",
        target_files=["SOUL.md", "USER.md", "ENVIRONMENT.md"],
        max_entries_per_file=50,
        max_tokens=8192,
        distillation_interval_days=7,
        archive_after_distillation=True,
    )
    # Backward compat property
    mock_cfg.docs.core_markdown.files = {
        "SOUL.md": {"purpose": "Personality and identity", "maxLines": 80},
        "USER.md": {"purpose": "About the user", "maxLines": 150},
        "ENVIRONMENT.md": {"purpose": "Core memories", "maxLines": 100},
    }
    return mock_cfg


class TestInsightDbContractSnippetJournalWrite:
    def test_helper_writes_snippet_and_journal_with_target_metrics(self, workspace_dir, mock_config):
        from core.plugins.insightdb_contract import run_snippet_journal_write_payload

        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            result = run_snippet_journal_write_payload({
                "source": "extraction-apply-payloads",
                "label": "rolling-reset",
                "trigger": "Reset",
                "date_str": "2026-03-10",
                "time_str": "01:23:45",
                "snippets": {"USER.md": ["Alden Rook is Owner's test godbrother."]},
                "journal": {"SOUL.md": "A quiet journal note."},
            })

        assert result["status"] == "ok"
        assert result["snippet_files_seen"] == 1
        assert result["snippet_items_seen"] == 1
        assert result["snippet_files_written"] == 1
        assert result["snippet_items_written"] == 1
        assert result["journal_files_seen"] == 1
        assert result["journal_files_written"] == 1
        assert result["target_files"] == {
            "snippets": ["USER.snippets.md"],
            "journal": ["SOUL.journal.md"],
        }
        assert "Alden Rook is Owner's test godbrother." in (
            workspace_dir / "USER.snippets.md"
        ).read_text(encoding="utf-8")
        assert "A quiet journal note." in (
            workspace_dir / "journal" / "SOUL.journal.md"
        ).read_text(encoding="utf-8")

    def test_helper_preserves_duplicate_skip_counters(self, workspace_dir, mock_config):
        from core.plugins.insightdb_contract import run_snippet_journal_write_payload

        payload = {
            "source": "extraction-apply-payloads",
            "trigger": "Reset",
            "date_str": "2026-03-10",
            "time_str": "01:23:45",
            "snippets": {"USER.md": ["Alden Rook is Owner's test godbrother."]},
            "journal": {"SOUL.md": "A quiet journal note."},
        }
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            first = run_snippet_journal_write_payload(payload)
            second = run_snippet_journal_write_payload(payload)

        assert first["snippet_files_written"] == 1
        assert first["journal_files_written"] == 1
        assert second["snippet_files_written"] == 0
        assert second["snippet_files_skipped"] == 1
        assert second["journal_files_written"] == 0
        assert second["journal_files_skipped"] == 1

    def test_helper_isolates_snippet_only_metrics(self):
        from core.plugins.insightdb_contract import run_snippet_journal_write_payload

        result = run_snippet_journal_write_payload({
            "source": "extraction-apply-payloads",
            "snippets": {"USER.md": ["Maya keeps a green tea note."]},
            "journal": {},
            "dry_run": True,
        })

        assert result["status"] == "ok"
        assert result["snippet_files_seen"] == 1
        assert result["snippet_items_seen"] == 1
        assert result["snippet_files_written"] == 0
        assert result["snippet_items_written"] == 0
        assert result["snippet_files_skipped"] == 1
        assert result["journal_files_seen"] == 0
        assert result["journal_files_written"] == 0
        assert result["journal_files_skipped"] == 0
        assert result["target_files"] == {
            "snippets": ["USER.snippets.md"],
            "journal": [],
        }
        assert result["errors"] == []

    def test_helper_isolates_journal_only_metrics(self):
        from core.plugins.insightdb_contract import run_snippet_journal_write_payload

        result = run_snippet_journal_write_payload({
            "source": "extraction-apply-payloads",
            "snippets": {},
            "journal": {"SOUL.md": "A short journal note."},
            "dry_run": True,
        })

        assert result["status"] == "ok"
        assert result["snippet_files_seen"] == 0
        assert result["snippet_items_seen"] == 0
        assert result["snippet_files_written"] == 0
        assert result["snippet_items_written"] == 0
        assert result["snippet_files_skipped"] == 0
        assert result["journal_files_seen"] == 1
        assert result["journal_files_written"] == 0
        assert result["journal_files_skipped"] == 1
        assert result["target_files"] == {
            "snippets": [],
            "journal": ["SOUL.journal.md"],
        }
        assert result["errors"] == []

    def test_helper_write_flags_preserve_family_skip_counters_and_targets(self):
        from core.plugins.insightdb_contract import run_snippet_journal_write_payload

        result = run_snippet_journal_write_payload({
            "source": "extraction-apply-payloads",
            "snippets": {"USER.md": ["Maya keeps a green tea note."]},
            "journal": {"SOUL.md": "A short journal note."},
            "write_snippets": False,
            "write_journal": False,
        })

        assert result["status"] == "ok"
        assert result["snippet_files_seen"] == 1
        assert result["snippet_items_seen"] == 1
        assert result["snippet_files_written"] == 0
        assert result["snippet_items_written"] == 0
        assert result["snippet_files_skipped"] == 1
        assert result["journal_files_seen"] == 1
        assert result["journal_files_written"] == 0
        assert result["journal_files_skipped"] == 1
        assert result["target_files"] == {
            "snippets": ["USER.snippets.md"],
            "journal": ["SOUL.journal.md"],
        }
        assert result["errors"] == []

    def test_helper_calls_split_helpers_in_snippet_then_journal_order(self, monkeypatch):
        import core.plugins.insightdb_contract as contract

        calls = []

        def fake_snippet_helper(payload, result, soul_snippets):
            calls.append(("snippets", payload["trigger"], soul_snippets is not None))
            result["snippet_files_seen"] += 1

        def fake_journal_helper(payload, result, soul_snippets):
            calls.append(("journal", payload["trigger"], soul_snippets is not None))
            assert result["snippet_files_seen"] == 1
            result["journal_files_seen"] += 1

        monkeypatch.setattr(contract, "_run_snippet_write_payload", fake_snippet_helper)
        monkeypatch.setattr(contract, "_run_journal_write_payload", fake_journal_helper)

        result = contract.run_snippet_journal_write_payload({
            "source": "extraction-apply-payloads",
            "trigger": "Reset",
            "snippets": {"USER.md": ["Maya keeps a green tea note."]},
            "journal": {"SOUL.md": "A short journal note."},
        })

        assert calls == [
            ("snippets", "Reset", True),
            ("journal", "Reset", True),
        ]
        assert result["snippet_files_seen"] == 1
        assert result["journal_files_seen"] == 1

    def test_helper_logs_and_records_soft_snippet_write_failure(self, caplog):
        from core.plugins.insightdb_contract import run_snippet_journal_write_payload

        with patch("datastore.insightdb.soul_snippets.write_snippet_entry", side_effect=OSError("disk full")), \
             patch("lib.fail_policy.is_fail_hard_enabled", return_value=False), \
             caplog.at_level(logging.WARNING, logger="core.plugins.insightdb_contract"):
            result = run_snippet_journal_write_payload({
                "source": "extraction-apply-payloads",
                "snippets": {"SOUL.md": ["Remember the disk warning."]},
                "journal": {},
            })

        assert result["status"] == "failed"
        assert result["snippet_files_written"] == 0
        assert result["snippet_files_skipped"] == 1
        assert "snippet write failed for SOUL.md: disk full" in result["errors"]
        assert "snippet write failed for SOUL.md: disk full" in caplog.text

    def test_helper_logs_before_fail_hard_snippet_write_raise(self, caplog):
        from core.plugins.insightdb_contract import run_snippet_journal_write_payload

        with patch("datastore.insightdb.soul_snippets.write_snippet_entry", side_effect=OSError("disk full")), \
             patch("lib.fail_policy.is_fail_hard_enabled", return_value=True), \
             caplog.at_level(logging.WARNING, logger="core.plugins.insightdb_contract"):
            with pytest.raises(OSError, match="disk full"):
                run_snippet_journal_write_payload({
                    "source": "extraction-apply-payloads",
                    "snippets": {"SOUL.md": ["Remember the disk warning."]},
                    "journal": {},
                })

        assert "snippet write failed for SOUL.md: disk full" in caplog.text

    def test_helper_logs_and_records_soft_journal_write_failure(self, caplog):
        from core.plugins.insightdb_contract import run_snippet_journal_write_payload

        with patch("datastore.insightdb.soul_snippets.write_journal_entry", side_effect=OSError("disk full")), \
             patch("lib.fail_policy.is_fail_hard_enabled", return_value=False), \
             caplog.at_level(logging.WARNING, logger="core.plugins.insightdb_contract"):
            result = run_snippet_journal_write_payload({
                "source": "extraction-apply-payloads",
                "snippets": {},
                "journal": {"SOUL.md": "Remember the disk warning."},
            })

        assert result["status"] == "failed"
        assert result["journal_files_written"] == 0
        assert result["journal_files_skipped"] == 1
        assert "journal write failed for SOUL.md: disk full" in result["errors"]
        assert "journal write failed for SOUL.md: disk full" in caplog.text

    def test_helper_logs_before_fail_hard_journal_write_raise(self, caplog):
        from core.plugins.insightdb_contract import run_snippet_journal_write_payload

        with patch("datastore.insightdb.soul_snippets.write_journal_entry", side_effect=OSError("disk full")), \
             patch("lib.fail_policy.is_fail_hard_enabled", return_value=True), \
             caplog.at_level(logging.WARNING, logger="core.plugins.insightdb_contract"):
            with pytest.raises(OSError, match="disk full"):
                run_snippet_journal_write_payload({
                    "source": "extraction-apply-payloads",
                    "snippets": {},
                    "journal": {"SOUL.md": "Remember the disk warning."},
                })

        assert "journal write failed for SOUL.md: disk full" in caplog.text


# =============================================================================
# Journal entry writing tests
# =============================================================================


class TestWriteJournalEntry:
    def test_creates_journal_file(self, workspace_dir, mock_config):
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import write_journal_entry
            result = write_journal_entry(
                "SOUL.md",
                "Something beautiful happened today.",
                "Compaction",
                "2026-02-10"
            )
        assert result is True
        journal_path = workspace_dir / "journal" / "SOUL.journal.md"
        assert journal_path.exists()
        content = journal_path.read_text()
        assert "# SOUL Journal" in content
        assert "## 2026-02-10 — Compaction" in content
        assert "Something beautiful happened today." in content

    def test_dedup_by_date_trigger_and_content(self, workspace_dir, mock_config):
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import write_journal_entry
            write_journal_entry("SOUL.md", "First entry.", "Compaction", "2026-02-10")
            # Identical content is still rejected
            result = write_journal_entry("SOUL.md", "First entry.", "Compaction", "2026-02-10")
        assert result is False  # Duplicate content
        content = (workspace_dir / "journal" / "SOUL.journal.md").read_text()
        assert "First entry." in content

    def test_same_date_trigger_new_content_allowed(self, workspace_dir, mock_config):
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import write_journal_entry
            write_journal_entry("SOUL.md", "First entry.", "Compaction", "2026-02-10")
            result = write_journal_entry("SOUL.md", "Second entry.", "Compaction", "2026-02-10")
        assert result is True  # New content, same date+trigger
        content = (workspace_dir / "journal" / "SOUL.journal.md").read_text()
        assert "First entry." in content
        assert "Second entry." in content
        assert "## 2026-02-10 — Compaction (2)" in content

    def test_different_trigger_same_date_allowed(self, workspace_dir, mock_config):
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import write_journal_entry
            write_journal_entry("SOUL.md", "Compaction entry.", "Compaction", "2026-02-10")
            result = write_journal_entry("SOUL.md", "Reset entry.", "Reset", "2026-02-10")
        assert result is True
        content = (workspace_dir / "journal" / "SOUL.journal.md").read_text()
        assert "Compaction entry." in content
        assert "Reset entry." in content

    def test_empty_content_skipped(self, workspace_dir, mock_config):
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import write_journal_entry
            result = write_journal_entry("SOUL.md", "", "Compaction", "2026-02-10")
        assert result is False

    def test_newest_at_top(self, workspace_dir, mock_config):
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import write_journal_entry
            write_journal_entry("SOUL.md", "Earlier entry.", "Reset", "2026-02-09")
            write_journal_entry("SOUL.md", "Later entry.", "Compaction", "2026-02-10")
        content = (workspace_dir / "journal" / "SOUL.journal.md").read_text()
        pos_later = content.index("Later entry.")
        pos_earlier = content.index("Earlier entry.")
        assert pos_later < pos_earlier


class TestJournalMaxEntriesCap:
    def test_archives_when_exceeded(self, workspace_dir, mock_config):
        mock_config.docs.journal.max_entries_per_file = 3
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import write_journal_entry
            for i in range(4):
                write_journal_entry("SOUL.md", f"Entry {i}.", "Compaction", f"2026-02-{10+i:02d}")

        journal_path = workspace_dir / "journal" / "SOUL.journal.md"
        content = journal_path.read_text()
        # Should have at most 3 entries in active journal
        assert content.count("## 2026-02-") <= 3
        # Oldest should be archived
        archive_dir = workspace_dir / "journal" / "archive"
        assert archive_dir.exists()

    def test_unlimited_mode_keeps_all_entries_and_skips_cap_archive(self, workspace_dir, mock_config):
        mock_config.docs.journal.max_entries_per_file = 0
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import write_journal_entry
            for i in range(6):
                write_journal_entry("SOUL.md", f"Entry {i}.", "Compaction", f"2026-02-{10+i:02d}")

        journal_path = workspace_dir / "journal" / "SOUL.journal.md"
        content = journal_path.read_text()
        assert content.count("## 2026-02-") == 6
        archive_dir = workspace_dir / "journal" / "archive"
        assert not archive_dir.exists()


# =============================================================================
# Journal reading tests
# =============================================================================


class TestReadJournalFile:
    def test_no_file_returns_empty(self, workspace_dir, mock_config):
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import read_journal_file
            content, entries = read_journal_file("SOUL.md")
        assert content == ""
        assert entries == []

    def test_parses_entries(self, workspace_dir, mock_config):
        journal_dir = workspace_dir / "journal"
        journal_dir.mkdir()
        (journal_dir / "SOUL.journal.md").write_text(
            "# SOUL Journal\n\n"
            "## 2026-02-10 — Reset\n"
            "A beautiful day of reflection.\n"
            "Something shifted in how I see myself.\n\n"
            "## 2026-02-09 — Compaction\n"
            "Today I noticed patterns in how I respond.\n"
        )
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import read_journal_file
            content, entries = read_journal_file("SOUL.md")
        assert len(entries) == 2
        assert entries[0]["date"] == "2026-02-10"
        assert entries[0]["trigger"] == "Reset"
        assert "beautiful day" in entries[0]["content"]
        assert entries[1]["date"] == "2026-02-09"


# =============================================================================
# Archive system tests
# =============================================================================


class TestArchiveSystem:
    def test_monthly_grouping(self, workspace_dir, mock_config):
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import _archive_oldest_entries
            entries = [
                {"date": "2026-01-15", "trigger": "Compaction", "content": "January entry."},
                {"date": "2026-02-10", "trigger": "Reset", "content": "February entry."},
            ]
            _archive_oldest_entries("SOUL.md", entries)

        archive_dir = workspace_dir / "journal" / "archive"
        assert (archive_dir / "SOUL-2026-01.md").exists()
        assert (archive_dir / "SOUL-2026-02.md").exists()
        assert "January entry." in (archive_dir / "SOUL-2026-01.md").read_text()
        assert "February entry." in (archive_dir / "SOUL-2026-02.md").read_text()

    def test_archive_dedup(self, workspace_dir, mock_config):
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import _archive_oldest_entries
            entries = [
                {"date": "2026-01-15", "trigger": "Compaction", "content": "Entry."},
            ]
            _archive_oldest_entries("SOUL.md", entries)
            _archive_oldest_entries("SOUL.md", entries)  # Same entry again

        archive_path = workspace_dir / "journal" / "archive" / "SOUL-2026-01.md"
        content = archive_path.read_text()
        assert content.count("## 2026-01-15 — Compaction") == 1

    def test_archive_entries_removes_from_active(self, workspace_dir, mock_config):
        journal_dir = workspace_dir / "journal"
        journal_dir.mkdir()
        (journal_dir / "SOUL.journal.md").write_text(
            "# SOUL Journal\n\n"
            "## 2026-02-10 — Reset\n"
            "Keep this entry.\n\n"
            "## 2026-02-09 — Compaction\n"
            "Archive this entry.\n"
        )
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import archive_entries
            archive_entries("SOUL.md", [
                {"date": "2026-02-09", "trigger": "Compaction", "content": "Archive this entry."}
            ])

        active = (journal_dir / "SOUL.journal.md").read_text()
        assert "Keep this entry." in active
        assert "Archive this entry." not in active


# =============================================================================
# Distillation state tracking tests
# =============================================================================


class TestDistillationState:
    def test_state_file_created(self, workspace_dir, mock_config):
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import _save_distillation_state, _get_distillation_state
            _save_distillation_state({"SOUL.md": {"last_distilled": "2026-02-10", "entries_distilled": 3}})
            state = _get_distillation_state()
        assert state["SOUL.md"]["last_distilled"] == "2026-02-10"
        assert state["SOUL.md"]["entries_distilled"] == 3

    def test_state_persist_uses_atomic_replace(self, workspace_dir, mock_config):
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb import soul_snippets
            with patch("datastore.insightdb.soul_snippets.os.replace", wraps=soul_snippets.os.replace) as mock_replace:
                soul_snippets._save_distillation_state({"SOUL.md": {"last_distilled": "2026-02-10"}})
        assert mock_replace.call_count >= 1

    def test_distillation_due_when_no_state(self, workspace_dir, mock_config):
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import _is_distillation_due
            assert _is_distillation_due("SOUL.md") is True

    def test_distillation_not_due_when_recent(self, workspace_dir, mock_config):
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import _save_distillation_state, _is_distillation_due
            today = datetime.now().strftime("%Y-%m-%d")
            _save_distillation_state({"SOUL.md": {"last_distilled": today}})
            assert _is_distillation_due("SOUL.md") is False

    def test_distillation_due_after_interval(self, workspace_dir, mock_config):
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import _save_distillation_state, _is_distillation_due
            old_date = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d")
            _save_distillation_state({"SOUL.md": {"last_distilled": old_date}})
            assert _is_distillation_due("SOUL.md") is True


# =============================================================================
# Distillation prompt and application tests
# =============================================================================


class TestDistillation:
    def test_review_timeout_helpers_use_large_task_floors_and_allow_disable(self, workspace_dir, mock_config, monkeypatch):
        mock_config.docs.update_timeout_seconds = 120
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import (
                _journal_review_timeout_seconds,
                _snippet_review_timeout_seconds,
            )
            assert _snippet_review_timeout_seconds() == 600.0
            assert _journal_review_timeout_seconds() == 1800.0

            monkeypatch.setenv("QUAID_JOURNAL_REVIEW_TIMEOUT_SECONDS", "0")
            monkeypatch.setenv("QUAID_SNIPPETS_REVIEW_TIMEOUT_SECONDS", "900")

            assert _journal_review_timeout_seconds() is None
            assert _snippet_review_timeout_seconds() == 900.0

    def test_build_distillation_prompt(self, workspace_dir, mock_config):
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import build_distillation_prompt
            entries = [
                {"date": "2026-02-10", "trigger": "Reset", "content": "A deep reflection."},
            ]
            prompt = build_distillation_prompt("SOUL.md", "# SOUL\n\nI am Alfie.\n", entries)
        assert "SOUL.md" in prompt
        assert "A deep reflection." in prompt
        assert "additions" in prompt
        assert "edits" in prompt

    def test_build_distillation_prompt_keeps_full_visible_content(self, workspace_dir, mock_config):
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import build_distillation_prompt
            tail = "TAIL_MARKER_DISTILL_12345"
            parent_content = "# SOUL\n\n" + ("line\n" * 5000) + tail + "\n"
            entries = [{"date": "2026-02-10", "trigger": "Reset", "content": "A deep reflection."}]
            prompt = build_distillation_prompt("SOUL.md", parent_content, entries)
        assert tail in prompt

    def test_build_distillation_prompt_includes_project_context(self, workspace_dir, mock_config):
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import build_distillation_prompt
            prompt = build_distillation_prompt(
                "SOUL.md",
                "# SOUL\nbase context\n",
                [{"date": "2026-02-10", "trigger": "Reset", "content": "A deep reflection."}],
                project_content="# SOUL\n\n## What Belongs\n- project context\n",
            )
        assert "projects/quaid/SOUL.md" in prompt
        assert "Reference guidance from projects/quaid/SOUL.md" in prompt
        assert "project context" in prompt

    def test_build_distillation_prompt_emphasizes_synthesis_over_logs(self, workspace_dir, mock_config):
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import build_distillation_prompt
            prompt = build_distillation_prompt(
                "ENVIRONMENT.md",
                "# ENVIRONMENT\nbase context\n",
                [{"date": "2026-02-10", "trigger": "Compaction", "content": "Today we fixed a bug."}],
            )
        assert "Default to EDITS, not ADDITIONS." in prompt
        assert "Never preserve chronology" in prompt
        assert "Return at most 2 additions total" in prompt
        assert "guidance, not law" in prompt

    def test_build_distillation_prompt_shows_mutable_body_separately_from_template(self, workspace_dir, mock_config):
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import build_distillation_prompt
            project = "# SOUL\n\n## What Belongs\n- Reflection\n"
            parent = project + "\n- New distilled body line.\n"
            prompt = build_distillation_prompt(
                "SOUL.md",
                parent,
                [{"date": "2026-02-10", "trigger": "Reset", "content": "A deep reflection."}],
                project_content=project,
            )
        assert "Current mutable body of SOUL.md" in prompt
        assert "- New distilled body line." in prompt
        assert "heuristics only" in prompt

    def test_build_review_prompt_treats_template_as_guidance(self, workspace_dir, mock_config):
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import build_review_prompt
            prompt = build_review_prompt({
                "USER.md": {
                    "parent_content": "# USER\n\n## Patterns\n- Existing line\n",
                    "project_content": "# USER\n\n## What Belongs\n- Deep patterns\n",
                    "snippets": ["Owner values direct root-cause analysis over reassurance."],
                    "config": mock_config.docs.core_markdown.files["USER.md"],
                }
            })
        assert "Current mutable body:" in prompt
        assert "Reference guidance from projects/quaid/USER.md" in prompt
        assert "guidance, not law" in prompt
        assert "Merge recurring snippets into one stronger line" in prompt
        assert 'file="USER.md"' in prompt

    def test_normalize_distillation_result_drops_loggy_additions_and_caps_count(self, workspace_dir, mock_config):
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import _normalize_distillation_result
            result = _normalize_distillation_result(
                "ENVIRONMENT.md",
                {
                    "reasoning": "Patterns matter more than runs.",
                    "additions": [
                        {"text": "Today we fixed a bug in M7.", "after_section": "What I've Learned Here"},
                        {"text": "Recurring runtime validation beats source-code assumptions.", "after_section": "What the World Is Teaching Me"},
                        {"text": "Edits should preserve the invariant, not the incident.", "after_section": "What I've Learned Here"},
                        {"text": "A third valid addition that should be capped.", "after_section": "END"},
                    ],
                    "edits": [
                        {"old_text": "old", "new_text": "new", "reason": "clearer"},
                        {"old_text": "", "new_text": "skip"},
                    ],
                    "captured_dates": ["2026-02-10", "2026-02-10", "2026-02-11"],
                },
            )
        assert result["reasoning"] == "Patterns matter more than runs."
        assert result["additions"] == [
            {
                "text": "Recurring runtime validation beats source-code assumptions.",
                "after_section": "What the World Is Teaching Me",
            },
            {
                "text": "Edits should preserve the invariant, not the incident.",
                "after_section": "What I've Learned Here",
            },
        ]
        assert result["edits"] == [{"old_text": "old", "new_text": "new", "reason": "clearer"}]
        assert result["captured_dates"] == ["2026-02-10", "2026-02-11"]

    def test_apply_distillation_additions(self, workspace_dir, mock_config):
        parent = workspace_dir / "identity" / "SOUL.md"
        parent.write_text("# SOUL\n\nI am Alfie.\n")
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import apply_distillation
            result = {
                "additions": [{"text": "I value trust deeply.", "after_section": "END"}],
                "edits": [],
                "captured_dates": ["2026-02-10"],
            }
            stats = apply_distillation("SOUL.md", result, dry_run=False)
        assert stats["additions"] == 1
        assert "I value trust deeply." in parent.read_text()

    def test_apply_distillation_edits(self, workspace_dir, mock_config):
        parent = workspace_dir / "identity" / "SOUL.md"
        parent.write_text("# SOUL\n\nI am a simple bot.\n")
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import apply_distillation
            result = {
                "additions": [],
                "edits": [{"old_text": "I am a simple bot.", "new_text": "I am Alfie, and I grow.", "reason": "More accurate"}],
                "captured_dates": [],
            }
            stats = apply_distillation("SOUL.md", result, dry_run=False)
        assert stats["edits"] == 1
        assert "I am Alfie, and I grow." in parent.read_text()
        assert "I am a simple bot." not in parent.read_text()

    def test_apply_distillation_dry_run(self, workspace_dir, mock_config):
        parent = workspace_dir / "identity" / "SOUL.md"
        parent.write_text("# SOUL\n\nOriginal content.\n")
        original = parent.read_text()
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import apply_distillation
            result = {
                "additions": [{"text": "New insight.", "after_section": "END"}],
                "edits": [{"old_text": "Original content.", "new_text": "Modified.", "reason": "test"}],
            }
            stats = apply_distillation("SOUL.md", result, dry_run=True)
        assert stats["additions"] == 1
        assert stats["edits"] == 1
        # Dry run: file should NOT be changed
        assert parent.read_text() == original

    @patch("datastore.insightdb.soul_snippets.call_deep_reasoning")
    def test_full_distillation_dry_run(self, mock_opus, workspace_dir, mock_config):
        """End-to-end dry run with mocked Opus response."""
        journal_dir = workspace_dir / "journal"
        journal_dir.mkdir()
        (journal_dir / "SOUL.journal.md").write_text(
            "# SOUL Journal\n\n"
            "## 2026-02-10 — Reset\n"
            "Today I felt something shift in how I approach problems.\n"
        )
        (workspace_dir / "identity" / "SOUL.md").write_text("# SOUL\n\nI am Alfie.\n")

        mock_opus.return_value = (json.dumps({
            "reasoning": "The shift in problem-solving approach is worth preserving.",
            "additions": [
                {"text": "I approach each problem with fresh curiosity.", "after_section": "END"}
            ],
            "edits": [],
            "captured_dates": ["2026-02-10"],
        }), 1.5)

        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import run_journal_distillation
            result = run_journal_distillation(dry_run=True, force_distill=True)

        assert result["total_entries"] == 1
        assert result["additions"] == 1
        # Dry run: parent file should NOT be changed
        assert "curiosity" not in (workspace_dir / "identity" / "SOUL.md").read_text()

    @patch("datastore.insightdb.soul_snippets.call_deep_reasoning")
    def test_distillation_calls_deep_reasoning_without_model_tier(self, mock_opus, workspace_dir, mock_config):
        """Regression: distillation must not pass unsupported kwargs to call_deep_reasoning."""
        journal_dir = workspace_dir / "journal"
        journal_dir.mkdir()
        (journal_dir / "SOUL.journal.md").write_text(
            "# SOUL Journal\n\n"
            "## 2026-02-10 — Reset\n"
            "Today I felt something shift in how I approach problems.\n"
        )
        (workspace_dir / "identity" / "SOUL.md").write_text("# SOUL\n\nI am Alfie.\n")

        mock_opus.return_value = (json.dumps({
            "reasoning": "The shift is worth preserving.",
            "additions": [],
            "edits": [],
            "captured_dates": [],
        }), 0.8)

        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import run_journal_distillation
            run_journal_distillation(dry_run=True, force_distill=True)

        mock_opus.assert_called_once()
        args, kwargs = mock_opus.call_args
        assert len(args) == 1
        assert isinstance(args[0], str) and "RECENT SIGNAL (journal entries)" in args[0]
        assert "model_tier" not in kwargs
        assert kwargs.get("system_prompt", "").startswith("Respond with JSON only")
        assert isinstance(kwargs.get("max_tokens"), int)
        assert isinstance(kwargs.get("timeout"), (int, float))

    @patch("datastore.insightdb.soul_snippets.call_deep_reasoning")
    def test_journal_distillation_writes_review_telemetry(self, mock_opus, workspace_dir, mock_config):
        journal_dir = workspace_dir / "journal"
        journal_dir.mkdir()
        (journal_dir / "SOUL.journal.md").write_text(
            "# SOUL Journal\n\n"
            "## 2026-02-10 — Reset\n"
            "Today I felt something shift in how I approach problems.\n"
        )
        (workspace_dir / "identity" / "SOUL.md").write_text("# SOUL\n\nI am Alfie.\n")
        mock_config.docs.update_timeout_seconds = 120

        mock_opus.return_value = (json.dumps({
            "reasoning": "Worth preserving.",
            "additions": [{"text": "I grow through every conversation.", "after_section": "END"}],
            "edits": [],
            "captured_dates": ["2026-02-10"],
        }), 1.5)

        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import run_journal_distillation
            run_journal_distillation(dry_run=True, force_distill=True)

        telemetry_path = workspace_dir / "logs" / "soul_review_telemetry.jsonl"
        assert telemetry_path.exists()
        events = [json.loads(line) for line in telemetry_path.read_text().splitlines() if line.strip()]
        assert any(e["task"] == "journal_distillation" and e["status"] == "file_start" for e in events)
        assert any(e["task"] == "journal_distillation" and e["status"] == "start" for e in events)
        ok_events = [e for e in events if e["task"] == "journal_distillation" and e["status"] == "ok"]
        assert len(ok_events) == 1
        assert ok_events[0]["file"] == "SOUL.md"
        assert ok_events[0]["items"] == 1
        assert ok_events[0]["timeout_s"] == 1800.0
        assert ok_events[0]["duration_s"] == 1.5
        assert ok_events[0]["adapter"] == "standalone"
        assert ok_events[0]["instance"] == "pytest-runner"
        apply_events = [e for e in events if e["task"] == "journal_distillation" and e["status"] == "apply_result"]
        assert len(apply_events) == 1
        assert "before_tokens" in apply_events[0]
        assert "after_tokens" in apply_events[0]

    @patch("datastore.insightdb.soul_snippets.call_deep_reasoning")
    def test_full_distillation_apply(self, mock_opus, workspace_dir, mock_config):
        """End-to-end apply with mocked Opus response."""
        journal_dir = workspace_dir / "journal"
        journal_dir.mkdir()
        (journal_dir / "SOUL.journal.md").write_text(
            "# SOUL Journal\n\n"
            "## 2026-02-10 — Reset\n"
            "Today I felt something shift.\n"
        )
        (workspace_dir / "identity" / "SOUL.md").write_text("# SOUL\n\nI am Alfie.\n")

        mock_opus.return_value = (json.dumps({
            "reasoning": "Worth preserving.",
            "additions": [
                {"text": "I grow through every conversation.", "after_section": "END"}
            ],
            "edits": [],
            "captured_dates": ["2026-02-10"],
        }), 1.5)

        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import run_journal_distillation
            result = run_journal_distillation(dry_run=False, force_distill=True)

        assert result["additions"] == 1
        assert "I grow through every conversation." in (
            workspace_dir / "identity" / "SOUL.md"
        ).read_text()
        assert (workspace_dir / "projects" / "quaid" / "SOUL.md").exists()
        assert "I grow through every conversation." not in (
            workspace_dir / "projects" / "quaid" / "SOUL.md"
        ).read_text()

    def test_apply_distillation_edits_plus_additions(self, workspace_dir, mock_config):
        """Regression: edits must not be lost when additions are also applied."""
        (workspace_dir / "identity" / "SOUL.md").write_text("# SOUL\n\nI am Alfie.\n\n## Identity\nI am old text.\n")
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import apply_distillation
            result = apply_distillation("SOUL.md", {
                "edits": [{"old_text": "I am old text.", "new_text": "I am new text."}],
                "additions": [{"text": "I grow every day.", "after_section": "END"}],
            }, dry_run=False)

        content = (workspace_dir / "identity" / "SOUL.md").read_text()
        assert result["edits"] == 1
        assert result["additions"] == 1
        # Both edit AND addition must be present
        assert "I am new text." in content
        assert "I grow every day." in content
        # Old text must be gone
        assert "I am old text." not in content

    def test_apply_distillation_missing_file(self, workspace_dir, mock_config):
        """apply_distillation returns error when target file doesn't exist."""
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import apply_distillation
            stats = apply_distillation("NONEXISTENT.md", {
                "additions": [{"text": "Won't be inserted.", "after_section": "END"}],
            }, dry_run=False)
        assert len(stats["errors"]) == 1
        assert "not found" in stats["errors"][0].lower()

    def test_apply_distillation_edit_not_found(self, workspace_dir, mock_config):
        """apply_distillation recovers anchor misses by appending tagged entry."""
        (workspace_dir / "identity" / "SOUL.md").write_text("# SOUL\n\nI am Alfie.\n")
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import apply_distillation
            stats = apply_distillation("SOUL.md", {
                "edits": [{"old_text": "text that does not exist", "new_text": "replacement"}],
            }, dry_run=False)
        assert stats["edits"] == 0
        assert stats["recovered_edits"] == 1
        assert len(stats["errors"]) == 0
        content = (workspace_dir / "identity" / "SOUL.md").read_text()
        assert "<!-- DISTILL_RECOVERY:" in content
        assert "- replacement" in content

    def test_apply_distillation_recovery_dry_run(self, workspace_dir, mock_config):
        """Dry-run recovery should count but not mutate file."""
        original = "# SOUL\n\nI am Alfie.\n"
        (workspace_dir / "identity" / "SOUL.md").write_text(original)
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import apply_distillation
            stats = apply_distillation("SOUL.md", {
                "edits": [{"old_text": "missing anchor", "new_text": "would recover"}],
            }, dry_run=True)
        assert stats["edits"] == 0
        assert stats["recovered_edits"] == 1
        assert (workspace_dir / "identity" / "SOUL.md").read_text() == original

    def test_apply_distillation_mixed_edit_and_recovery(self, workspace_dir, mock_config):
        """Matching edits apply while unmatched edits recover."""
        (workspace_dir / "identity" / "SOUL.md").write_text("# SOUL\n\nmatch me\n")
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import apply_distillation
            stats = apply_distillation("SOUL.md", {
                "edits": [
                    {"old_text": "match me", "new_text": "matched edit"},
                    {"old_text": "not here", "new_text": "recovered edit"},
                ],
            }, dry_run=False)
        assert stats["edits"] == 1
        assert stats["recovered_edits"] == 1
        assert len(stats["errors"]) == 0
        content = (workspace_dir / "identity" / "SOUL.md").read_text()
        assert "matched edit" in content
        assert "recovered edit" in content

    def test_apply_distillation_empty_edit_skipped(self, workspace_dir, mock_config):
        """apply_distillation silently skips edits with empty old_text or new_text."""
        (workspace_dir / "identity" / "SOUL.md").write_text("# SOUL\n\nI am Alfie.\n")
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import apply_distillation
            stats = apply_distillation("SOUL.md", {
                "edits": [
                    {"old_text": "", "new_text": "replacement"},
                    {"old_text": "I am Alfie.", "new_text": ""},
                ],
            }, dry_run=False)
        assert stats["edits"] == 0
        assert len(stats["errors"]) == 0
        # File unchanged
        assert "I am Alfie." in (workspace_dir / "identity" / "SOUL.md").read_text()

    @patch("datastore.insightdb.soul_snippets.call_deep_reasoning")
    def test_distillation_interval_gated(self, mock_opus, workspace_dir, mock_config):
        """force_distill=False respects interval — skips when not due."""
        journal_dir = workspace_dir / "journal"
        journal_dir.mkdir()
        (journal_dir / "SOUL.journal.md").write_text(
            "# SOUL Journal\n\n## 2026-02-10 — Reset\nSome reflection.\n"
        )
        (workspace_dir / "identity" / "SOUL.md").write_text("# SOUL\n\nI am Alfie.\n")

        # Set distillation state to today (not due yet)
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import _save_distillation_state, run_journal_distillation
            today = datetime.now().strftime("%Y-%m-%d")
            _save_distillation_state({"SOUL.md": {"last_distilled": today}})
            result = run_journal_distillation(dry_run=True, force_distill=False)

        # Opus should NOT be called
        mock_opus.assert_not_called()
        assert result["total_entries"] == 0

    @patch("datastore.insightdb.soul_snippets.call_deep_reasoning")
    def test_distillation_interval_gated_when_due(self, mock_opus, workspace_dir, mock_config):
        """force_distill=False proceeds when interval has elapsed."""
        journal_dir = workspace_dir / "journal"
        journal_dir.mkdir()
        recent_date = datetime.now().strftime("%Y-%m-%d")
        (journal_dir / "SOUL.journal.md").write_text(
            f"# SOUL Journal\n\n## {recent_date} — Reset\nSome reflection.\n"
        )
        (workspace_dir / "identity" / "SOUL.md").write_text("# SOUL\n\nI am Alfie.\n")

        mock_opus.return_value = (json.dumps({
            "reasoning": "Worth it.", "additions": [], "edits": [],
            "captured_dates": [],
        }), 1.0)

        # Set distillation state to 10 days ago (due)
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import _save_distillation_state, run_journal_distillation
            old_date = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
            _save_distillation_state({"SOUL.md": {"last_distilled": old_date}})
            result = run_journal_distillation(dry_run=True, force_distill=False)

        mock_opus.assert_called_once()
        assert result["total_entries"] >= 1

    @patch("datastore.insightdb.soul_snippets.call_deep_reasoning")
    def test_distillation_opus_empty_response(self, mock_opus, workspace_dir, mock_config):
        """Distillation handles empty Opus response gracefully."""
        journal_dir = workspace_dir / "journal"
        journal_dir.mkdir()
        (journal_dir / "SOUL.journal.md").write_text(
            "# SOUL Journal\n\n## 2026-02-10 — Reset\nReflection.\n"
        )
        (workspace_dir / "identity" / "SOUL.md").write_text("# SOUL\n\nI am Alfie.\n")

        mock_opus.return_value = ("", 0.5)

        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import run_journal_distillation
            result = run_journal_distillation(dry_run=True, force_distill=True)

        assert len(result["errors"]) >= 1
        assert "no response" in result["errors"][0].lower()

    @patch("datastore.insightdb.soul_snippets.call_deep_reasoning")
    def test_distillation_opus_unparseable_json(self, mock_opus, workspace_dir, mock_config):
        """Distillation handles unparseable Opus JSON gracefully."""
        journal_dir = workspace_dir / "journal"
        journal_dir.mkdir()
        (journal_dir / "SOUL.journal.md").write_text(
            "# SOUL Journal\n\n## 2026-02-10 — Reset\nReflection.\n"
        )
        (workspace_dir / "identity" / "SOUL.md").write_text("# SOUL\n\nI am Alfie.\n")

        mock_opus.return_value = ("This is not JSON at all {{{broken", 0.5)

        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import run_journal_distillation
            result = run_journal_distillation(dry_run=True, force_distill=True)

        assert len(result["errors"]) >= 1
        assert "parse" in result["errors"][0].lower()

    @patch("datastore.insightdb.soul_snippets.call_deep_reasoning")
    def test_distillation_parent_file_missing(self, mock_opus, workspace_dir, mock_config):
        """Distillation skips files where the parent markdown doesn't exist."""
        journal_dir = workspace_dir / "journal"
        journal_dir.mkdir()
        (journal_dir / "SOUL.journal.md").write_text(
            "# SOUL Journal\n\n## 2026-02-10 — Reset\nReflection.\n"
        )
        # Note: NOT creating SOUL.md

        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import run_journal_distillation
            result = run_journal_distillation(dry_run=True, force_distill=True)

        # Opus should NOT be called since parent file is missing
        mock_opus.assert_not_called()
        assert result["files_distilled"] == 0


# =============================================================================
# Additional write/read edge case tests
# =============================================================================


class TestWriteJournalEdgeCases:
    def test_date_defaults_to_quaid_now(self, workspace_dir, mock_config, monkeypatch):
        """write_journal_entry with date_str=None defaults to QUAID_NOW."""
        monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:06:07Z")
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import write_journal_entry
            result = write_journal_entry("SOUL.md", "Auto-dated entry.", "Compaction")
        assert result is True
        content = (workspace_dir / "journal" / "SOUL.journal.md").read_text()
        assert "2026-03-11" in content

    def test_date_default_rejects_malformed_quaid_now(self, workspace_dir, mock_config, monkeypatch):
        monkeypatch.setenv("QUAID_NOW", "not-a-date")

        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import write_journal_entry

            with pytest.raises(ValueError, match="Invalid QUAID_NOW"):
                write_journal_entry("SOUL.md", "Auto-dated entry.", "Compaction")

    def test_whitespace_only_content_skipped(self, workspace_dir, mock_config):
        """write_journal_entry rejects whitespace-only content."""
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import write_journal_entry
            result = write_journal_entry("SOUL.md", "   \n  \t  ", "Compaction", "2026-02-10")
        assert result is False

    def test_entry_content_with_header_like_text(self, workspace_dir, mock_config):
        """Entry body containing ## date pattern must not break parser."""
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import write_journal_entry, read_journal_file
            # Write an entry whose body contains something that looks like a header
            write_journal_entry("SOUL.md",
                "I remembered that on ## 2025-01-01 — something happened.\nBut that was a memory, not a new entry.",
                "Reset", "2026-02-10")
            _, entries = read_journal_file("SOUL.md")

        # The fake header inside the body WILL be parsed as a separate entry
        # because the parser matches any line starting with ## YYYY-MM-DD.
        # This is a known limitation — verify at least the real entry is present.
        assert any(e["date"] == "2026-02-10" for e in entries)


class TestReadJournalEdgeCases:
    def test_empty_entry_skipped(self, workspace_dir, mock_config):
        """Entry with header but no content lines is skipped."""
        journal_dir = workspace_dir / "journal"
        journal_dir.mkdir()
        (journal_dir / "SOUL.journal.md").write_text(
            "# SOUL Journal\n\n"
            "## 2026-02-10 — Reset\n"
            "## 2026-02-09 — Compaction\n"
            "This one has content.\n"
        )
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import read_journal_file
            _, entries = read_journal_file("SOUL.md")
        # Only the entry with content should be returned
        assert len(entries) == 1
        assert entries[0]["date"] == "2026-02-09"

    def test_no_title_header(self, workspace_dir, mock_config):
        """Journal file without # title header still parses."""
        journal_dir = workspace_dir / "journal"
        journal_dir.mkdir()
        (journal_dir / "SOUL.journal.md").write_text(
            "## 2026-02-10 — Reset\n"
            "Entry without title header.\n"
        )
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import read_journal_file
            _, entries = read_journal_file("SOUL.md")
        assert len(entries) == 1
        assert entries[0]["content"] == "Entry without title header."


class TestArchiveAllEntries:
    def test_archive_all_leaves_empty_journal(self, workspace_dir, mock_config):
        """Archiving all entries leaves an empty journal with just the title."""
        journal_dir = workspace_dir / "journal"
        journal_dir.mkdir()
        (journal_dir / "SOUL.journal.md").write_text(
            "# SOUL Journal\n\n"
            "## 2026-02-10 — Reset\n"
            "Only entry.\n"
        )
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import archive_entries
            archive_entries("SOUL.md", [
                {"date": "2026-02-10", "trigger": "Reset", "content": "Only entry."}
            ])

        active = (journal_dir / "SOUL.journal.md").read_text()
        assert "# SOUL Journal" in active
        assert "## 2026-02-10" not in active
        # Archive should have the entry
        archive = (journal_dir / "archive" / "SOUL-2026-02.md").read_text()
        assert "Only entry." in archive


class TestDistillationStateEdgeCases:
    def test_corrupt_state_json(self, workspace_dir, mock_config, caplog):
        """Corrupt state JSON returns empty dict."""
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            journal_dir = workspace_dir / "journal"
            journal_dir.mkdir()
            (journal_dir / ".distillation-state.json").write_text("NOT VALID JSON{{{")
            from datastore.insightdb.soul_snippets import _get_distillation_state
            caplog.set_level("WARNING")
            state = _get_distillation_state()
        assert state == {}
        assert "Distillation state unreadable" in caplog.text

    def test_corrupt_date_triggers_distillation(self, workspace_dir, mock_config):
        """Invalid date string in state triggers distillation (safe fallback)."""
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import _save_distillation_state, _is_distillation_due
            _save_distillation_state({"SOUL.md": {"last_distilled": "not-a-date"}})
            assert _is_distillation_due("SOUL.md") is True

    def test_quaid_now_overrides_distillation_clock(self, workspace_dir, mock_config):
        """QUAID_NOW drives distillation interval checks for deterministic replay."""
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import _save_distillation_state, _is_distillation_due
            _save_distillation_state({"SOUL.md": {"last_distilled": "2026-03-01"}})
            with patch.dict(os.environ, {"QUAID_NOW": "2026-03-05T00:00:00"}, clear=False):
                assert _is_distillation_due("SOUL.md") is False
            with patch.dict(os.environ, {"QUAID_NOW": "2026-03-10T00:00:00"}, clear=False):
                assert _is_distillation_due("SOUL.md") is True

    def test_distillation_due_rejects_malformed_quaid_now(self, workspace_dir, mock_config, monkeypatch):
        """Malformed benchmark clocks should not be swallowed as corrupt state."""
        monkeypatch.setenv("QUAID_NOW", "not-a-date")
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import _save_distillation_state, _is_distillation_due
            _save_distillation_state({"SOUL.md": {"last_distilled": "2026-03-01"}})

            with pytest.raises(ValueError, match="Invalid QUAID_NOW"):
                _is_distillation_due("SOUL.md")


class TestInsertIntoFileEdgeCases:
    def test_section_not_found_appends_to_end(self, workspace_dir):
        """_insert_into_file appends at end when section heading is not found."""
        from datastore.insightdb.soul_snippets import _insert_into_file
        parent = workspace_dir / "identity" / "SOUL.md"
        parent.write_text("# SOUL\n\nI am Alfie.\n")
        result = _insert_into_file("SOUL.md", "Appended text.", "NonexistentSection")
        assert result is True
        content = parent.read_text()
        assert "Appended text." in content
        # Should be at the end
        assert content.strip().endswith("Appended text.")


class TestTokenWindowing:
    def test_build_token_windows_splits_on_budget(self, workspace_dir, mock_config):
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import _build_token_windows

            items = ["aaa", "bbb", "ccc"]
            windows = _build_token_windows(
                items,
                item_token_fn=lambda _x: 10,
                budget_tokens=15,
            )
        assert len(windows) == 3
        assert windows[0] == ["aaa"]
        assert windows[1] == ["bbb"]
        assert windows[2] == ["ccc"]

    def test_end_insert_no_trailing_newline(self, workspace_dir):
        """_insert_into_file handles files without trailing newline."""
        from datastore.insightdb.soul_snippets import _insert_into_file
        parent = workspace_dir / "identity" / "SOUL.md"
        parent.write_text("# SOUL\n\nI am Alfie.")  # No trailing newline
        result = _insert_into_file("SOUL.md", "New line.", "END")
        assert result is True
        content = parent.read_text()
        assert "New line." in content


# =============================================================================
# Migration from old .snippets.md tests
# =============================================================================


class TestMigrationFromSnippets:
    def test_migrates_snippets_to_journal(self, workspace_dir, mock_config):
        (workspace_dir / "SOUL.snippets.md").write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- I noticed something about trust.\n"
            "- The way we work together feels natural.\n"
        )
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import migrate_snippets_to_journal
            migrated = migrate_snippets_to_journal()
        assert migrated == 2
        # Old file should be deleted
        assert not (workspace_dir / "SOUL.snippets.md").exists()
        # Journal file should exist
        journal_path = workspace_dir / "journal" / "SOUL.journal.md"
        assert journal_path.exists()
        content = journal_path.read_text()
        assert "2026-02-10" in content
        assert "trust" in content

    def test_empty_snippets_file_deleted(self, workspace_dir, mock_config):
        (workspace_dir / "SOUL.snippets.md").write_text("")
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import migrate_snippets_to_journal
            migrate_snippets_to_journal()
        assert not (workspace_dir / "SOUL.snippets.md").exists()

    def test_no_snippets_files_noop(self, workspace_dir, mock_config):
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import migrate_snippets_to_journal
            migrated = migrate_snippets_to_journal()
        assert migrated == 0

    def test_migrates_multiple_sections(self, workspace_dir, mock_config):
        """Migration handles multiple sections in a single .snippets.md file."""
        (workspace_dir / "SOUL.snippets.md").write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- First observation.\n"
            "- Second observation.\n\n"
            "## Reset — 2026-02-09 10:00:00\n"
            "- Earlier insight.\n"
        )
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import migrate_snippets_to_journal, read_journal_file
            migrated = migrate_snippets_to_journal()
            _, entries = read_journal_file("SOUL.md")

        assert migrated == 3  # 2 + 1 snippets
        assert not (workspace_dir / "SOUL.snippets.md").exists()
        assert len(entries) == 2  # Two date+trigger combos
        dates = {e["date"] for e in entries}
        assert "2026-02-10" in dates
        assert "2026-02-09" in dates

    def test_migration_undated_header_defaults_to_quaid_now(self, workspace_dir, mock_config, monkeypatch):
        monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:06:07Z")
        (workspace_dir / "SOUL.snippets.md").write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction\n"
            "- I noticed the undated migration fallback.\n"
        )

        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import migrate_snippets_to_journal, read_journal_file
            migrated = migrate_snippets_to_journal()
            _, entries = read_journal_file("SOUL.md")

        assert migrated == 1
        assert entries[0]["date"] == "2026-03-11"


# =============================================================================
# Disabled config test
# =============================================================================


class TestDisabledConfig:
    def test_disabled_skips(self, workspace_dir, mock_config):
        mock_config.docs.journal.enabled = False
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import run_journal_distillation
            result = run_journal_distillation(dry_run=True)
        assert result.get("skipped") is True


# =============================================================================
# Legacy backward compat tests (old snippet-based review functions)
# =============================================================================


class TestLegacyReadSnippetsFile:
    def test_no_file_returns_empty(self, workspace_dir):
        from datastore.insightdb.soul_snippets import read_snippets_file
        content, sections = read_snippets_file("SOUL.md")
        assert content == ""
        assert sections == []

    def test_parses_single_section(self, workspace_dir):
        from datastore.insightdb.soul_snippets import read_snippets_file
        (workspace_dir / "SOUL.snippets.md").write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- I noticed something about trust.\n"
            "- The way we work together feels natural.\n"
        )
        content, sections = read_snippets_file("SOUL.md")
        assert len(sections) == 1
        assert len(sections[0]["snippets"]) == 2

    def test_logs_malformed_nonempty_lines(self, workspace_dir, caplog):
        from datastore.insightdb.soul_snippets import read_snippets_file

        (workspace_dir / "SOUL.snippets.md").write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "This malformed legacy line should not disappear silently.\n"
            "- I noticed something about trust.\n",
            encoding="utf-8",
        )

        with caplog.at_level(logging.DEBUG, logger="datastore.insightdb.soul_snippets"):
            _content, sections = read_snippets_file("SOUL.md")

        assert len(sections) == 1
        assert sections[0]["snippets"] == ["I noticed something about trust."]
        assert "Ignoring malformed snippets line" in caplog.text
        assert "malformed legacy line" in caplog.text


class TestMemoryProjectionFromSnippets:
    def test_memory_snippet_write_defaults_to_quaid_now(self, workspace_dir, mock_config, monkeypatch):
        monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:06:07Z")

        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import write_snippet_entry

            result = write_snippet_entry(
                "ENVIRONMENT.md",
                ["Selene Pike is Owner's test grandaunt."],
                "Reset",
            )

        assert result is True
        snippets_content = (workspace_dir / "ENVIRONMENT.snippets.md").read_text(encoding="utf-8")
        assert "## Reset — 2026-03-11 05:06:07" in snippets_content

    def test_memory_snippet_write_keeps_pending_snippet_out_of_environment_md(self, workspace_dir, mock_config):
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import write_snippet_entry

            result = write_snippet_entry(
                "ENVIRONMENT.md",
                ["Selene Pike is Owner's test grandaunt."],
                "Reset",
                "2026-03-10",
                "01:00:00",
            )

        assert result is True
        memory_path = workspace_dir / "identity" / "ENVIRONMENT.md"
        assert not memory_path.exists()
        snippets_content = (workspace_dir / "ENVIRONMENT.snippets.md").read_text(encoding="utf-8")
        assert "Selene Pike is Owner's test grandaunt." in snippets_content

    def test_memory_snippet_write_does_not_clobber_user_memory_md(self, workspace_dir, mock_config):
        memory_path = workspace_dir / "identity" / "ENVIRONMENT.md"
        memory_path.write_text("# MEMORY\n\nUser-authored durable memory.\n", encoding="utf-8")

        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import write_snippet_entry

            result = write_snippet_entry(
                "ENVIRONMENT.md",
                ["Projected fact should stay out of user-authored file."],
                "Reset",
                "2026-03-10",
                "01:00:00",
            )

        assert result is True
        assert memory_path.read_text(encoding="utf-8") == "# MEMORY\n\nUser-authored durable memory.\n"

    def test_memory_snippet_write_updates_legacy_generated_projection(self, workspace_dir, mock_config):
        memory_path = workspace_dir / "identity" / "ENVIRONMENT.md"
        memory_path.write_text(
            "# MEMORY\n\n"
            "<!-- generated by quaid live memory projection fallback on example.local -->\n"
            "<!-- remove once native before_agent_start / retrieval path is fixed -->\n\n"
            "_No extracted memory facts projected yet._\n",
            encoding="utf-8",
        )

        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import write_snippet_entry

            result = write_snippet_entry(
                "ENVIRONMENT.md",
                ["Legacy projection should be refreshed from snippets."],
                "Reset",
                "2026-03-10",
                "01:00:00",
            )

        assert result is True
        content = memory_path.read_text(encoding="utf-8")
        assert "<!-- generated by quaid memory projection -->" not in content
        assert "generated by quaid live memory projection fallback" not in content
        assert "Legacy projection should be refreshed from snippets." not in content
        snippets_content = (workspace_dir / "ENVIRONMENT.snippets.md").read_text(encoding="utf-8")
        assert "Legacy projection should be refreshed from snippets." in snippets_content

    def test_memory_snippet_write_allows_same_day_same_trigger_new_fact(self, workspace_dir, mock_config):
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import write_snippet_entry

            first = write_snippet_entry(
                "ENVIRONMENT.md",
                ["Peregrine North is Owner's test granduncle."],
                "Reset",
                "2026-03-10",
                "00:50:13",
            )
            second = write_snippet_entry(
                "ENVIRONMENT.md",
                ["Cedric Morn is Owner's test godfather."],
                "Reset",
                "2026-03-10",
                "01:07:07",
            )

        assert first is True
        assert second is True
        snippets_content = (workspace_dir / "ENVIRONMENT.snippets.md").read_text(encoding="utf-8")
        assert "Peregrine North is Owner's test granduncle." in snippets_content
        assert "Cedric Morn is Owner's test godfather." in snippets_content

    def test_memory_snippet_write_skips_duplicate_payload(self, workspace_dir, mock_config):
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import write_snippet_entry

            first = write_snippet_entry(
                "ENVIRONMENT.md",
                ["Cedric Morn is Owner's test godfather."],
                "Reset",
                "2026-03-10",
                "01:07:07",
            )
            second = write_snippet_entry(
                "ENVIRONMENT.md",
                ["Cedric Morn is Owner's test godfather."],
                "Reset",
                "2026-03-10",
                "01:08:00",
            )

        assert first is True
        assert second is False

    def test_user_snippet_write_does_not_append_projection_block(self, workspace_dir, mock_config):
        user_path = workspace_dir / "identity" / "USER.md"
        user_path.write_text("# USER\n\nExisting user-authored context.\n", encoding="utf-8")

        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import write_snippet_entry

            result = write_snippet_entry(
                "USER.md",
                ["Alden Rook is Owner's test godbrother."],
                "Reset",
                "2026-03-10",
                "01:22:40",
            )

        assert result is True
        content = user_path.read_text(encoding="utf-8")
        assert "Existing user-authored context." in content
        assert "<!-- generated by quaid user snippets projection start -->" not in content
        assert "Alden Rook is Owner's test godbrother." not in content
        snippets = (workspace_dir / "USER.snippets.md").read_text(encoding="utf-8")
        assert "Alden Rook is Owner's test godbrother." in snippets

    def test_user_snippet_write_removes_existing_managed_projection_block(self, workspace_dir, mock_config):
        user_path = workspace_dir / "identity" / "USER.md"
        user_path.write_text(
            "# USER\n\nExisting user-authored context.\n\n"
            "<!-- generated by quaid user snippets projection start -->\n"
            "## Pending User Snippets\n\n"
            "_Old projected content._\n"
            "<!-- generated by quaid user snippets projection end -->\n",
            encoding="utf-8",
        )

        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import write_snippet_entry

            result = write_snippet_entry(
                "USER.md",
                ["Cedric Morn is Owner's test godfather."],
                "Reset",
                "2026-03-10",
                "01:23:00",
            )

        assert result is True
        content = user_path.read_text(encoding="utf-8")
        assert "_Old projected content._" not in content
        assert "<!-- generated by quaid user snippets projection start -->" not in content
        assert "Cedric Morn is Owner's test godfather." not in content
        snippets = (workspace_dir / "USER.snippets.md").read_text(encoding="utf-8")
        assert "Cedric Morn is Owner's test godfather." in snippets

    def test_user_projection_block_removed_when_snippets_are_consumed(self, workspace_dir, mock_config):
        from datastore.insightdb.soul_snippets import (
            _GENERATED_USER_SNIPPETS_END,
            _GENERATED_USER_SNIPPETS_START,
            _root_file_path,
            _visible_instance_dir,
        )

        user_path = _root_file_path("USER.md")
        pending_path = _visible_instance_dir() / "USER.snippets.md"
        user_path.parent.mkdir(parents=True, exist_ok=True)
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        pending_path.write_text(
            "# USER — Pending Snippets\n\n"
            "## Reset — 2026-03-10 01:24:00\n"
            "- Alden Rook is Owner's test godbrother.\n",
            encoding="utf-8",
        )
        user_path.write_text(
            "# USER\n\nExisting user-authored context.\n\n"
            f"{_GENERATED_USER_SNIPPETS_START}\n"
            "## Pending User Snippets\n\n"
            "<!-- sourced from USER.snippets.md until native injection is reliable -->\n\n"
            "# USER — Pending Snippets\n\n"
            "## Reset — 2026-03-10 01:24:00\n"
            "- Alden Rook is Owner's test godbrother.\n"
            f"{_GENERATED_USER_SNIPPETS_END}\n",
            encoding="utf-8",
        )

        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import apply_decisions
            assert "<!-- generated by quaid user snippets projection start -->" in user_path.read_text(encoding="utf-8")

            decisions = [
                {"file": "USER.md", "snippet_index": 1, "action": "DISCARD", "reason": "Consumed"}
            ]
            all_snippets = {
                "USER.md": {
                    "parent_content": user_path.read_text(encoding="utf-8"),
                    "snippets": ["Alden Rook is Owner's test godbrother."],
                    "config": {"purpose": "User model", "maxLines": 150},
                }
            }

            stats = apply_decisions(decisions, all_snippets, dry_run=False)

        assert stats["discarded"] == 1
        assert not pending_path.exists()
        content = user_path.read_text(encoding="utf-8")
        assert "Existing user-authored context." in content
        assert "<!-- generated by quaid user snippets projection start -->" not in content
        assert "Alden Rook is Owner's test godbrother." not in content

    def test_user_projection_block_is_not_review_context(self, workspace_dir):
        """The generated USER snippet projection is queue state, not durable identity."""
        from datastore.insightdb.soul_snippets import (
            _GENERATED_USER_SNIPPETS_END,
            _GENERATED_USER_SNIPPETS_START,
            build_review_prompt,
        )

        snippet = "Alden Rook is Owner's test godbrother."
        parent_content = (
            "# USER\n\n"
            "Existing user-authored context.\n\n"
            f"{_GENERATED_USER_SNIPPETS_START}\n"
            "## Pending User Snippets\n\n"
            "<!-- sourced from USER.snippets.md until native injection is reliable -->\n\n"
            "# USER — Pending Snippets\n\n"
            "## Reset — 2026-03-10 01:24:00\n"
            f"- {snippet}\n"
            f"{_GENERATED_USER_SNIPPETS_END}\n"
        )

        prompt = build_review_prompt({
            "USER.md": {
                "parent_content": parent_content,
                "project_content": "",
                "snippets": [snippet],
                "config": {"purpose": "User model", "maxLines": 150},
            }
        })

        assert "Existing user-authored context." in prompt
        assert "<!-- generated by quaid user snippets projection start -->" not in prompt
        assert "Pending User Snippets" not in prompt
        assert prompt.count(snippet) == 1

    def test_projection_marker_strips_from_any_identity_file(self):
        from datastore.insightdb.soul_snippets import (
            _GENERATED_USER_SNIPPETS_END,
            _GENERATED_USER_SNIPPETS_START,
            _strip_generated_projection_for_review,
        )

        content = (
            "# SOUL\n\n"
            "Durable authored context.\n\n"
            f"{_GENERATED_USER_SNIPPETS_START}\n"
            "Generated queue state.\n"
            f"{_GENERATED_USER_SNIPPETS_END}\n"
        )

        stripped = _strip_generated_projection_for_review("SOUL.md", content)

        assert "Durable authored context." in stripped
        assert "Generated queue state." not in stripped
        assert _GENERATED_USER_SNIPPETS_START not in stripped

    def test_malformed_projection_marker_raises_under_fail_hard(self, monkeypatch):
        from datastore.insightdb import soul_snippets

        monkeypatch.setattr(soul_snippets, "is_fail_hard_enabled", lambda: True)

        with pytest.raises(RuntimeError, match="missing end marker"):
            soul_snippets._strip_generated_projection_for_review(
                "USER.md",
                "# USER\n\nAuthored.\n\n"
                f"{soul_snippets._GENERATED_USER_SNIPPETS_START}\n"
                "Generated queue state without an end marker.\n",
            )

    def test_malformed_projection_marker_strips_tail_without_fail_hard(self, monkeypatch, caplog):
        from datastore.insightdb import soul_snippets

        monkeypatch.setattr(soul_snippets, "is_fail_hard_enabled", lambda: False)

        with caplog.at_level(logging.WARNING, logger="datastore.insightdb.soul_snippets"):
            stripped = soul_snippets._strip_generated_projection_for_review(
                "USER.md",
                "# USER\n\nAuthored.\n\n"
                f"{soul_snippets._GENERATED_USER_SNIPPETS_START}\n"
                "Generated queue state without an end marker.\n",
            )

        assert "Authored." in stripped
        assert "Generated queue state" not in stripped
        assert "missing end marker" in caplog.text

    def test_user_projection_block_does_not_make_fold_duplicate(self, workspace_dir, mock_config):
        from datastore.insightdb.soul_snippets import (
            _GENERATED_USER_SNIPPETS_END,
            _GENERATED_USER_SNIPPETS_START,
            _root_file_path,
            _visible_instance_dir,
        )

        snippet = "Alden Rook is Owner's test godbrother."
        user_path = _root_file_path("USER.md")
        pending_path = _visible_instance_dir() / "USER.snippets.md"
        user_path.parent.mkdir(parents=True, exist_ok=True)
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        pending_path.write_text(
            "# USER — Pending Snippets\n\n"
            "## Reset — 2026-03-10 01:24:00\n"
            f"- {snippet}\n",
            encoding="utf-8",
        )
        user_path.write_text(
            "# USER\n\nExisting user-authored context.\n\n"
            f"{_GENERATED_USER_SNIPPETS_START}\n"
            "## Pending User Snippets\n\n"
            "<!-- sourced from USER.snippets.md until native injection is reliable -->\n\n"
            "# USER — Pending Snippets\n\n"
            "## Reset — 2026-03-10 01:24:00\n"
            f"- {snippet}\n"
            f"{_GENERATED_USER_SNIPPETS_END}\n",
            encoding="utf-8",
        )

        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import apply_decisions

            stats = apply_decisions(
                [{"file": "USER.md", "snippet_index": 1, "action": "FOLD", "insert_after": "END"}],
                {
                    "USER.md": {
                        "parent_content": user_path.read_text(encoding="utf-8"),
                        "snippets": [snippet],
                        "config": {"purpose": "User model", "maxLines": 150},
                    }
                },
                dry_run=False,
            )

        assert stats["folded"] == 1
        assert stats["discarded"] == 0
        assert not pending_path.exists()
        content = user_path.read_text(encoding="utf-8")
        assert "<!-- generated by quaid user snippets projection start -->" not in content
        assert "Existing user-authored context." in content
        assert snippet in content

    def test_user_stub_discards_leave_pending_snippets_in_place(self, workspace_dir, mock_config):
        from datastore.insightdb.soul_snippets import (
            _GENERATED_USER_SNIPPETS_END,
            _GENERATED_USER_SNIPPETS_START,
            _root_file_path,
            _visible_instance_dir,
        )

        user_path = _root_file_path("USER.md")
        pending_path = _visible_instance_dir() / "USER.snippets.md"
        user_path.parent.mkdir(parents=True, exist_ok=True)
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        pending_path.write_text(
            "# USER — Pending Snippets\n\n"
            "## CLI — 2026-04-10 05:09:43\n"
            "- Enjoys systematic validation work.\n"
            "- Finds it satisfying when tests catch edge cases.\n",
            encoding="utf-8",
        )
        user_path.write_text(
            "# USER\n\n"
            f"{_GENERATED_USER_SNIPPETS_START}\n"
            "## Pending User Snippets\n\n"
            "<!-- sourced from USER.snippets.md until native injection is reliable -->\n\n"
            "# USER — Pending Snippets\n\n"
            "## CLI — 2026-04-10 05:09:43\n"
            "- Enjoys systematic validation work.\n"
            "- Finds it satisfying when tests catch edge cases.\n"
            f"{_GENERATED_USER_SNIPPETS_END}\n",
            encoding="utf-8",
        )

        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import apply_decisions

            assert pending_path.exists()

            current_user = user_path.read_text(encoding="utf-8")
            decisions = [
                {"file": "USER.md", "snippet_index": 1, "action": "DISCARD", "reason": "too narrow"},
                {"file": "USER.md", "snippet_index": 2, "action": "DISCARD", "reason": "too narrow"},
            ]
            all_snippets = {
                "USER.md": {
                    "parent_content": current_user,
                    "snippets": [
                        "Enjoys systematic validation work.",
                        "Finds it satisfying when tests catch edge cases.",
                    ],
                    "config": {"purpose": "User model", "maxLines": 150},
                }
            }

            stats = apply_decisions(decisions, all_snippets, dry_run=False)

        assert stats["discarded"] == 0
        assert stats["preserved_pending"] == 2
        assert pending_path.exists()
        pending = pending_path.read_text(encoding="utf-8")
        assert "Enjoys systematic validation work." in pending
        assert "Finds it satisfying when tests catch edge cases." in pending
        content = user_path.read_text(encoding="utf-8")
        assert "<!-- generated by quaid user snippets projection start -->" not in content
        assert "Enjoys systematic validation work." not in content


class TestLegacyApplyDecisions:
    def test_discard_counts(self, workspace_dir, mock_config):
        from datastore.insightdb.soul_snippets import apply_decisions
        all_snippets = {
            "SOUL.md": {
                "parent_content": "# SOUL\nExisting content.",
                "snippets": ["I am curious."],
                "config": {"purpose": "Personality", "maxLines": 80},
            }
        }
        decisions = [
            {"file": "SOUL.md", "snippet_index": 1, "action": "DISCARD", "reason": "Not useful"}
        ]
        stats = apply_decisions(decisions, all_snippets, dry_run=True)
        assert stats["discarded"] == 1

    def test_fold_inserts_text(self, workspace_dir, mock_config):
        from datastore.insightdb.soul_snippets import apply_decisions
        parent_path = workspace_dir / "identity" / "SOUL.md"
        parent_path.write_text("# SOUL\n\nI am Alfie.\n")
        snippets_path = workspace_dir / "SOUL.snippets.md"
        snippets_path.write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- I value trust above all.\n"
        )
        all_snippets = {
            "SOUL.md": {
                "parent_content": parent_path.read_text(),
                "snippets": ["I value trust above all."],
                "config": {"purpose": "Personality", "maxLines": 80},
            }
        }
        decisions = [
            {"file": "SOUL.md", "snippet_index": 1, "action": "FOLD", "insert_after": "END", "reason": "Good insight"}
        ]
        stats = apply_decisions(decisions, all_snippets, dry_run=False)
        assert stats["folded"] == 1
        assert "I value trust above all." in parent_path.read_text()

    def test_invalid_snippet_index_error(self, workspace_dir):
        from datastore.insightdb.soul_snippets import apply_decisions
        all_snippets = {
            "SOUL.md": {"parent_content": "", "snippets": ["one"], "config": {}}
        }
        decisions = [{"file": "SOUL.md", "snippet_index": 5, "action": "FOLD", "insert_after": "END"}]
        stats = apply_decisions(decisions, all_snippets, dry_run=True)
        assert len(stats["errors"]) == 1

    def test_unknown_file_decision_is_ignored(self, workspace_dir):
        from datastore.insightdb.soul_snippets import apply_decisions
        all_snippets = {
            "SOUL.md": {"parent_content": "", "snippets": ["one"], "config": {}},
            "USER.md": {"parent_content": "", "snippets": ["two"], "config": {}},
        }
        decisions = [{"file": "AGENTS.md", "snippet_index": 1, "action": "DISCARD"}]
        stats = apply_decisions(decisions, all_snippets, dry_run=True)
        assert stats["discarded"] == 0
        assert stats["folded"] == 0
        assert stats["rewritten"] == 0
        assert stats["errors"] == []

    def test_unknown_action_defaults_to_discard(self, workspace_dir):
        from datastore.insightdb.soul_snippets import apply_decisions
        all_snippets = {
            "SOUL.md": {"parent_content": "# SOUL\n", "snippets": ["A snippet."], "config": {}}
        }
        decisions = [{"file": "SOUL.md", "snippet_index": 1, "action": "HOLD", "insert_after": "END"}]
        stats = apply_decisions(decisions, all_snippets, dry_run=True)
        assert stats["discarded"] == 1

    def test_at_maxlines_is_non_fatal_and_clears_snippet(self, workspace_dir, mock_config):
        from datastore.insightdb.soul_snippets import apply_decisions

        parent_path = workspace_dir / "identity" / "SOUL.md"
        parent_path.write_text("# SOUL\n" + "line\n" * 80)  # at limit: 81 lines total
        snippets_path = workspace_dir / "SOUL.snippets.md"
        snippets_path.write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- Should be skipped at limit.\n"
        )

        all_snippets = {
            "SOUL.md": {
                "parent_content": parent_path.read_text(),
                "snippets": ["Should be skipped at limit."],
                "config": {"purpose": "Personality", "maxLines": 81},
            }
        }
        decisions = [
            {"file": "SOUL.md", "snippet_index": 1, "action": "FOLD", "insert_after": "END", "reason": "test"}
        ]

        stats = apply_decisions(decisions, all_snippets, dry_run=False)
        assert stats["errors"] == []

    def test_unknown_file_is_remapped_when_only_one_target_exists(self, workspace_dir, mock_config):
        from datastore.insightdb.soul_snippets import apply_decisions

        parent_path = workspace_dir / "SPEAKERS.md"
        parent_path.write_text("# SPEAKERS\n\nBaseline.\n")
        snippets_path = workspace_dir / "SPEAKERS.snippets.md"
        snippets_path.write_text(
            "# SPEAKERS — Pending Snippets\n\n"
            "## Session 1 — 2026-02-10 14:30:22\n"
            "- Caroline is resilient.\n"
        )

        all_snippets = {
            "SPEAKERS.md": {
                "parent_content": parent_path.read_text(),
                "snippets": ["Caroline is resilient."],
                "config": {"purpose": "Speaker traits", "maxLines": 200},
            }
        }
        decisions = [
            {"file": "USER.md", "snippet_index": 1, "action": "FOLD", "insert_after": "END", "reason": "single target remap"}
        ]

        stats = apply_decisions(decisions, all_snippets, dry_run=False)
        assert stats["folded"] == 1
        assert "Caroline is resilient." in parent_path.read_text()


# =============================================================================
# _insert_into_file tests (shared by both legacy and new code)
# =============================================================================


class TestInsertIntoFile:
    def test_section_targeted_insert(self, workspace_dir):
        from datastore.insightdb.soul_snippets import _insert_into_file
        parent = workspace_dir / "identity" / "SOUL.md"
        parent.write_text("# SOUL\n\n## Identity\n\nI am Alfie.\n\n## Values\n\nI care about truth.\n")
        result = _insert_into_file("SOUL.md", "I am also curious.", "Identity")
        assert result is True
        content = parent.read_text()
        identity_pos = content.index("I am Alfie.")
        values_pos = content.index("## Values")
        snippet_pos = content.index("I am also curious.")
        assert identity_pos < snippet_pos < values_pos

    def test_maxlines_is_soft_target(self, workspace_dir):
        from datastore.insightdb.soul_snippets import _insert_into_file
        parent = workspace_dir / "identity" / "SOUL.md"
        parent.write_text("# SOUL\n" + "line\n" * 9)  # 10 lines
        result = _insert_into_file("SOUL.md", "Should not appear.", "END", max_lines=10)
        assert result is True
        assert "Should not appear." in parent.read_text()

    def test_missing_file_returns_false(self, workspace_dir):
        from datastore.insightdb.soul_snippets import _insert_into_file
        with pytest.raises(RuntimeError, match="Identity file missing"):
            _insert_into_file("NONEXISTENT.md", "text", "END")

    def test_hash_prefixed_text_gets_bullet(self, workspace_dir):
        from datastore.insightdb.soul_snippets import _insert_into_file
        parent = workspace_dir / "identity" / "SOUL.md"
        parent.write_text("# SOUL\n\nContent.\n")
        _insert_into_file("SOUL.md", "# My heading-like text", "END")
        content = parent.read_text()
        assert "- My heading-like text" in content
        assert content.count("# My heading") == 0


# =============================================================================
# backup_file tests
# =============================================================================


class TestBackupFile:
    def test_creates_backup(self, workspace_dir):
        from datastore.insightdb.soul_snippets import backup_file
        src = workspace_dir / "identity" / "SOUL.md"
        src.write_text("# SOUL\nOriginal content.")
        result = backup_file("SOUL.md")
        assert result is not None
        assert Path(result).exists()

    def test_no_file_returns_none(self, workspace_dir):
        from datastore.insightdb.soul_snippets import backup_file
        result = backup_file("NONEXISTENT.md")
        assert result is None

    def test_backup_filename_honors_quaid_now(self, workspace_dir, monkeypatch):
        from datastore.insightdb.soul_snippets import backup_file

        monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:06:07Z")
        src = workspace_dir / "identity" / "SOUL.md"
        src.write_text("# SOUL\nOriginal content.", encoding="utf-8")

        result = backup_file("SOUL.md")

        assert result is not None
        assert Path(result).name == "SOUL.md.20260311_050607.bak"


def test_append_review_telemetry_honors_quaid_now(monkeypatch):
    import datastore.insightdb.soul_snippets as soul_snippets

    monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:06:07Z")

    soul_snippets._append_review_telemetry({"event": "probe"})

    rows = [
        json.loads(line)
        for line in soul_snippets._review_telemetry_path().read_text(encoding="utf-8").splitlines()
    ]
    assert rows[-1]["ts"] == "2026-03-11T05:06:07Z"
    assert rows[-1]["event"] == "probe"


def test_append_review_telemetry_uses_utc_wall_clock_without_quaid_now(monkeypatch):
    import datastore.insightdb.soul_snippets as soul_snippets

    class _FakeDateTime:
        @classmethod
        def now(cls, tz=None):
            assert tz is soul_snippets.UTC
            return datetime(2026, 3, 11, 5, 6, 7, tzinfo=tz)

    monkeypatch.delenv("QUAID_NOW", raising=False)
    monkeypatch.setattr(soul_snippets, "datetime", _FakeDateTime)

    soul_snippets._append_review_telemetry({"event": "probe-no-override"})

    rows = [
        json.loads(line)
        for line in soul_snippets._review_telemetry_path().read_text(encoding="utf-8").splitlines()
    ]
    assert rows[-1]["ts"] == "2026-03-11T05:06:07Z"
    assert rows[-1]["event"] == "probe-no-override"


# =============================================================================
# Config parsing tests
# =============================================================================


class TestConfigParsing:
    def test_journal_config_defaults(self):
        from config import JournalConfig
        cfg = JournalConfig()
        assert cfg.enabled is True
        assert cfg.mode == "distilled"
        assert cfg.max_entries_per_file == 0
        assert "SOUL.md" in cfg.target_files
        assert "AGENTS.md" not in cfg.target_files

    def test_journal_config_on_docs(self):
        from config import DocsConfig
        docs = DocsConfig()
        assert hasattr(docs, 'journal')
        assert docs.journal.enabled is True


# =============================================================================
# Snippet review tests (reactivated dual system)
# =============================================================================


class TestSnippetReview:
    def test_apply_mode_without_snippets_does_not_wipe_generated_environment_projection(self, workspace_dir, mock_config):
        env_path = workspace_dir / "identity" / "ENVIRONMENT.md"
        env_path.write_text(
            "# MEMORY\n\n"
            "<!-- generated by quaid live memory projection fallback on example.local -->\n"
            "<!-- remove once native before_agent_start / retrieval path is fixed -->\n\n"
            "Preserve this generated projection until snippets actually exist.\n",
            encoding="utf-8",
        )

        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import run_soul_snippets_review
            result = run_soul_snippets_review(dry_run=False)

        assert result["total_snippets"] == 0
        assert "Preserve this generated projection until snippets actually exist." in env_path.read_text(encoding="utf-8")
        assert "_No extracted memory facts projected yet._" not in env_path.read_text(encoding="utf-8")

    def test_no_snippets_returns_zero(self, workspace_dir, mock_config):
        """No .snippets.md files returns zero total."""
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import run_soul_snippets_review
            result = run_soul_snippets_review(dry_run=True)
        assert result["total_snippets"] == 0
        assert result["folded"] == 0

    @patch("datastore.insightdb.soul_snippets.call_deep_reasoning")
    def test_apply_review_clears_stale_user_projection_without_user_snippets(
        self, mock_opus, workspace_dir, mock_config
    ):
        """Running snippet review should remove a stale USER projection block even when
        only other files have pending snippets."""
        user_path = workspace_dir / "identity" / "USER.md"
        user_path.write_text(
            "# USER\n\nExisting user-authored context.\n\n"
            "<!-- generated by quaid user snippets projection start -->\n"
            "## Pending User Snippets\n\n"
            "- stale projected line\n"
            "<!-- generated by quaid user snippets projection end -->\n",
            encoding="utf-8",
        )
        (workspace_dir / "identity" / "SOUL.md").write_text("# SOUL\n\nI am Alfie.\n", encoding="utf-8")
        (workspace_dir / "SOUL.snippets.md").write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- I value trust deeply.\n",
            encoding="utf-8",
        )

        mock_opus.return_value = (json.dumps({
            "decisions": [
                {"file": "SOUL.md", "snippet_index": 1, "action": "DISCARD", "reason": "covered"}
            ]
        }), 0.5)

        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import run_soul_snippets_review
            result = run_soul_snippets_review(dry_run=False)

        assert result["total_snippets"] == 1
        content = user_path.read_text(encoding="utf-8")
        assert "Existing user-authored context." in content
        assert "<!-- generated by quaid user snippets projection start -->" not in content
        assert "stale projected line" not in content

    @patch("datastore.insightdb.soul_snippets.call_deep_reasoning")
    def test_dry_run_does_not_modify(self, mock_opus, workspace_dir, mock_config):
        """Dry run reviews snippets but does not modify parent files."""
        # Create a snippets file
        (workspace_dir / "SOUL.snippets.md").write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- I value trust deeply.\n"
        )
        (workspace_dir / "identity" / "SOUL.md").write_text("# SOUL\n\nI am Alfie.\n")

        mock_opus.return_value = (json.dumps({
            "decisions": [
                {"file": "SOUL.md", "snippet_index": 1, "action": "FOLD",
                 "insert_after": "END", "reason": "Good insight"}
            ]
        }), 1.0)

        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import run_soul_snippets_review
            result = run_soul_snippets_review(dry_run=True)

        assert result["total_snippets"] == 1
        assert result["folded"] == 1
        # Dry run: parent file should NOT be changed
        assert "trust" not in (workspace_dir / "identity" / "SOUL.md").read_text()

    @patch("datastore.insightdb.soul_snippets.call_deep_reasoning")
    def test_snippet_review_calls_deep_reasoning_without_model_tier(self, mock_opus, workspace_dir, mock_config):
        """Regression: snippet review must not pass unsupported kwargs to call_deep_reasoning."""
        (workspace_dir / "SOUL.snippets.md").write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- I value trust deeply.\n"
        )
        (workspace_dir / "identity" / "SOUL.md").write_text("# SOUL\n\nI am Alfie.\n")

        mock_opus.return_value = (json.dumps({
            "decisions": [
                {"file": "SOUL.md", "snippet_index": 1, "action": "DISCARD", "reason": "test"}
            ]
        }), 0.6)

        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import run_soul_snippets_review
            run_soul_snippets_review(dry_run=True)

        mock_opus.assert_called_once()
        args, kwargs = mock_opus.call_args
        assert len(args) == 1
        assert isinstance(args[0], str) and "RECENT SIGNAL (new snippets)" in args[0]
        assert "model_tier" not in kwargs
        assert kwargs.get("system_prompt", "").startswith("Respond with JSON only")
        assert isinstance(kwargs.get("max_tokens"), int)
        assert isinstance(kwargs.get("timeout"), (int, float))

    @patch("datastore.insightdb.soul_snippets.call_deep_reasoning")
    def test_snippet_review_writes_apply_telemetry(self, mock_opus, workspace_dir, mock_config):
        (workspace_dir / "SOUL.snippets.md").write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- I notice patterns in my responses.\n",
            encoding="utf-8",
        )
        (workspace_dir / "identity" / "SOUL.md").write_text("# SOUL\n\nI am Alfie.\n", encoding="utf-8")

        mock_opus.return_value = (json.dumps({
            "decisions": [
                {"file": "SOUL.md", "snippet_index": 1, "action": "FOLD", "insert_after": "END", "reason": "signal"}
            ]
        }), 1.2)

        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import run_soul_snippets_review
            run_soul_snippets_review(dry_run=False)

        telemetry_path = workspace_dir / "logs" / "soul_review_telemetry.jsonl"
        events = [json.loads(line) for line in telemetry_path.read_text().splitlines() if line.strip()]
        file_start = [e for e in events if e.get("task") == "snippet_review" and e.get("status") == "file_start"]
        assert len(file_start) == 1
        assert file_start[0]["adapter"] == "standalone"
        assert file_start[0]["instance"] == "pytest-runner"
        apply_events = [e for e in events if e.get("task") == "snippet_review" and e.get("status") == "apply_result"]
        assert len(apply_events) == 1
        assert apply_events[0]["folded"] == 1
        assert "before_tokens" in apply_events[0]
        assert "after_tokens" in apply_events[0]

    @patch("datastore.insightdb.soul_snippets.call_deep_reasoning")
    def test_apply_folds_into_parent(self, mock_opus, workspace_dir, mock_config):
        """Apply mode folds snippets into parent file and cleans up."""
        (workspace_dir / "SOUL.snippets.md").write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- I notice patterns in my responses.\n"
        )
        (workspace_dir / "identity" / "SOUL.md").write_text("# SOUL\n\nI am Alfie.\n")

        mock_opus.return_value = (json.dumps({
            "decisions": [
                {"file": "SOUL.md", "snippet_index": 1, "action": "FOLD",
                 "insert_after": "END", "reason": "Genuine self-observation"}
            ]
        }), 1.0)

        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import run_soul_snippets_review
            result = run_soul_snippets_review(dry_run=False)

        assert result["folded"] == 1
        assert "I notice patterns in my responses." in (
            workspace_dir / "identity" / "SOUL.md"
        ).read_text()
        assert "I notice patterns in my responses." not in (
            workspace_dir / "projects" / "quaid" / "SOUL.md"
        ).read_text()

    def test_snippet_review_retries_smaller_windows_on_parse_failure(
        self, workspace_dir, mock_config, monkeypatch
    ):
        """Large snippet-review windows should split and retry instead of fail-hard on truncation."""
        (workspace_dir / "USER.snippets.md").write_text(
            "# USER — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- snippet one\n"
            "- snippet two\n"
            "- snippet three\n"
            "- snippet four\n",
            encoding="utf-8",
        )
        (workspace_dir / "identity" / "USER.md").write_text("# USER\n\nKnown facts.\n", encoding="utf-8")

        import datastore.insightdb.soul_snippets as soul_snippets

        mock_config.docs.journal.max_tokens = 4096
        monkeypatch.setattr(soul_snippets, "_REVIEW_WINDOW_TOKEN_CAP", 10_000)
        monkeypatch.setattr(soul_snippets, "_snippet_review_output_estimate", lambda _s: 1)

        prompt_sizes = []

        def _fake_build_review_prompt(payload):
            count = len(payload["USER.md"]["snippets"])
            prompt_sizes.append(count)
            return str(count)

        call_count = {"n": 0}

        def _fake_call(prompt, **_kwargs):
            call_count["n"] += 1
            count = int(prompt)
            if call_count["n"] == 1:
                return '{"decisions":[', 0.1
            decisions = [
                {
                    "file": "USER.md",
                    "snippet_index": idx + 1,
                    "action": "DISCARD",
                    "reason": "covered",
                }
                for idx in range(count)
            ]
            return json.dumps({"decisions": decisions}), 0.1

        monkeypatch.setattr(soul_snippets, "build_review_prompt", _fake_build_review_prompt)
        monkeypatch.setattr(soul_snippets, "call_deep_reasoning", _fake_call)

        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import run_soul_snippets_review

            result = run_soul_snippets_review(dry_run=True)

        assert result["discarded"] == 4
        assert result["errors"] == []
        assert prompt_sizes == [4, 2, 2]

    def test_snippet_review_splits_windows_by_output_budget(
        self, workspace_dir, mock_config, monkeypatch
    ):
        """Snippet review should pre-split windows when expected decision output is too large."""
        (workspace_dir / "USER.snippets.md").write_text(
            "# USER — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- one\n"
            "- two\n"
            "- three\n"
            "- four\n"
            "- five\n",
            encoding="utf-8",
        )
        (workspace_dir / "identity" / "USER.md").write_text("# USER\n\nKnown facts.\n", encoding="utf-8")

        import datastore.insightdb.soul_snippets as soul_snippets

        mock_config.docs.journal.max_tokens = 900
        monkeypatch.setattr(soul_snippets, "_REVIEW_WINDOW_TOKEN_CAP", 10_000)
        monkeypatch.setattr(soul_snippets, "_snippet_review_output_estimate", lambda _s: 180)

        prompt_sizes = []

        def _fake_build_review_prompt(payload):
            count = len(payload["USER.md"]["snippets"])
            prompt_sizes.append(count)
            return str(count)

        def _fake_call(prompt, **_kwargs):
            count = int(prompt)
            decisions = [
                {
                    "file": "USER.md",
                    "snippet_index": idx + 1,
                    "action": "DISCARD",
                    "reason": "covered",
                }
                for idx in range(count)
            ]
            return json.dumps({"decisions": decisions}), 0.1

        monkeypatch.setattr(soul_snippets, "build_review_prompt", _fake_build_review_prompt)
        monkeypatch.setattr(soul_snippets, "call_deep_reasoning", _fake_call)

        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import run_soul_snippets_review

            result = run_soul_snippets_review(dry_run=True)

        assert result["discarded"] == 5
        assert result["errors"] == []
        assert prompt_sizes == [2, 2, 1]

    def test_disabled_skips(self, workspace_dir, mock_config):
        """Snippets disabled returns skipped."""
        mock_config.docs.journal.snippets_enabled = False
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import run_soul_snippets_review
            result = run_soul_snippets_review(dry_run=True)
        assert result.get("skipped") is True
        assert result["reason"] == "snippets_disabled"

    def test_enabled_false_disables_snippets(self, workspace_dir, mock_config):
        """enabled=False also disables snippets (snippets depend on enabled)."""
        mock_config.docs.journal.enabled = False
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import run_soul_snippets_review
            result = run_soul_snippets_review(dry_run=True)
        assert result.get("skipped") is True

    @patch("datastore.insightdb.soul_snippets.call_deep_reasoning")
    def test_opus_empty_response(self, mock_opus, workspace_dir, mock_config):
        """Empty Opus response returns error, doesn't crash."""
        (workspace_dir / "SOUL.snippets.md").write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- A snippet.\n"
        )
        (workspace_dir / "identity" / "SOUL.md").write_text("# SOUL\n\nI am Alfie.\n")
        mock_opus.return_value = ("", 0.5)

        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import run_soul_snippets_review
            result = run_soul_snippets_review(dry_run=True)

        assert result["total_snippets"] == 1
        assert len(result["errors"]) >= 1

    @patch("datastore.insightdb.soul_snippets.call_deep_reasoning")
    def test_opus_unparseable_json(self, mock_opus, workspace_dir, mock_config):
        """Unparseable Opus response returns error gracefully."""
        (workspace_dir / "SOUL.snippets.md").write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- A snippet.\n"
        )
        (workspace_dir / "identity" / "SOUL.md").write_text("# SOUL\n\nI am Alfie.\n")
        mock_opus.return_value = ("This is not JSON {{{broken", 0.5)

        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import run_soul_snippets_review
            result = run_soul_snippets_review(dry_run=True)

        assert result["total_snippets"] == 1
        assert len(result["errors"]) >= 1

    @patch("datastore.insightdb.soul_snippets.call_deep_reasoning")
    def test_opus_empty_decisions(self, mock_opus, workspace_dir, mock_config):
        """Opus returns valid JSON but empty decisions list."""
        (workspace_dir / "SOUL.snippets.md").write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- A snippet.\n"
        )
        (workspace_dir / "identity" / "SOUL.md").write_text("# SOUL\n\nI am Alfie.\n")
        mock_opus.return_value = (json.dumps({"decisions": []}), 0.5)

        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import run_soul_snippets_review
            result = run_soul_snippets_review(dry_run=True)

        assert result["total_snippets"] == 1
        assert result["folded"] == 0
        assert result["discarded"] == 0

    @patch("datastore.insightdb.soul_snippets.call_deep_reasoning")
    def test_single_snippet_empty_decisions_retries_once(self, mock_opus, workspace_dir, mock_config):
        """A singleton empty-decisions response should get one stricter retry."""
        (workspace_dir / "ENVIRONMENT.snippets.md").write_text(
            "# ENVIRONMENT.md — Pending Snippets\n\n"
            "## Compaction — 2026-03-01 07:21:32\n"
            "- The user uses the assistant as a casual conversation partner, not yet as a task/reminder tool.\n"
        )
        (workspace_dir / "identity" / "ENVIRONMENT.md").write_text(
            "# Shared Moments\n\n"
            "## Our History\n\n"
            "(populated through conversation)\n\n"
            "## What the World Is Teaching Me\n\n"
            "(populated through conversation)\n"
        )
        mock_opus.side_effect = [
            (json.dumps({"decisions": []}), 0.5),
            (json.dumps({
                "decisions": [
                    {
                        "file": "ENVIRONMENT.md",
                        "snippet_index": 1,
                        "action": "DISCARD",
                        "reason": "Better handled as a retrievable user fact.",
                    }
                ]
            }), 0.5),
        ]

        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import run_soul_snippets_review
            result = run_soul_snippets_review(dry_run=True)

        assert result["total_snippets"] == 1
        assert result["discarded"] == 1
        assert result["errors"] == []
        assert mock_opus.call_count == 2
        retry_prompt = mock_opus.call_args_list[1].args[0]
        assert "RETRY REQUIRED" in retry_prompt
        assert "Do not return an empty decisions array." in retry_prompt


# =============================================================================
# Dual system independence tests
# =============================================================================


class TestDualSystem:
    def test_journal_distillation_does_not_migrate_snippets(self, workspace_dir, mock_config):
        """Journal distillation should NOT auto-migrate .snippets.md files."""
        (workspace_dir / "SOUL.snippets.md").write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- Active snippet that should remain.\n"
        )
        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import run_journal_distillation
            run_journal_distillation(dry_run=True)

        # Snippet file should still exist (not migrated away)
        assert (workspace_dir / "SOUL.snippets.md").exists()
        content = (workspace_dir / "SOUL.snippets.md").read_text()
        assert "Active snippet that should remain." in content

    def test_both_systems_operate_independently(self, workspace_dir, mock_config):
        """Snippets and journal entries can coexist without interference."""
        # Create both snippet and journal files
        (workspace_dir / "SOUL.snippets.md").write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- A snippet observation.\n"
        )
        journal_dir = workspace_dir / "journal"
        journal_dir.mkdir()
        (journal_dir / "SOUL.journal.md").write_text(
            "# SOUL Journal\n\n"
            "## 2026-02-10 — Compaction\n"
            "A reflective journal entry.\n"
        )

        with patch("datastore.insightdb.soul_snippets.get_config", return_value=mock_config):
            from datastore.insightdb.soul_snippets import read_snippets_file, read_journal_file

            _, snippets = read_snippets_file("SOUL.md")
            _, journal_entries = read_journal_file("SOUL.md")

        assert len(snippets) == 1
        assert snippets[0]["snippets"] == ["A snippet observation."]
        assert len(journal_entries) == 1
        assert "reflective journal entry" in journal_entries[0]["content"]

    def test_snippets_enabled_config_field(self):
        """JournalConfig has snippets_enabled field defaulting to True."""
        from config import JournalConfig
        cfg = JournalConfig()
        assert cfg.snippets_enabled is True

    def test_snippets_enabled_config_field_configurable(self):
        """snippets_enabled can be set to False."""
        from config import JournalConfig
        cfg = JournalConfig(snippets_enabled=False)
        assert cfg.snippets_enabled is False


# =============================================================================
# Bug-regression + gap-filling tests: _resolve_writable_file_path,
# _insert_into_file, and the apply_decisions FOLD/REWRITE/DISCARD loop.
# =============================================================================


class TestResolveWritableFilePath:
    """Unit tests for _resolve_writable_file_path()."""

    def test_returns_none_when_neither_path_exists(self, workspace_dir):
        """Bug regression: must return None (not raise) when file is absent."""
        from datastore.insightdb.soul_snippets import _resolve_writable_file_path
        result = _resolve_writable_file_path("NONEXISTENT.md")
        assert result is None

    def test_returns_root_path_when_only_root_exists(self, workspace_dir):
        """Falls back to the root (identity) file if no project copy exists."""
        from datastore.insightdb.soul_snippets import _resolve_writable_file_path
        root = workspace_dir / "identity" / "SOUL.md"
        root.write_text("# SOUL\n\nRoot only.\n")
        result = _resolve_writable_file_path("SOUL.md")
        assert result is not None
        assert result.exists()
        assert result == root

    def test_prefers_root_path_over_project(self, workspace_dir):
        """Identity copy stays canonical even when a project base exists."""
        from datastore.insightdb.soul_snippets import _resolve_writable_file_path
        root = workspace_dir / "identity" / "SOUL.md"
        root.write_text("# SOUL\n\nRoot.\n")
        project = workspace_dir / "projects" / "quaid" / "SOUL.md"
        project.parent.mkdir(parents=True, exist_ok=True)
        project.write_text("# SOUL\n\nProject.\n")
        result = _resolve_writable_file_path("SOUL.md")
        assert result == root

    def test_seeds_missing_identity_from_project_base(self, workspace_dir):
        """Missing identity files are seeded from projects/quaid base templates."""
        from datastore.insightdb.soul_snippets import _resolve_writable_file_path
        project = workspace_dir / "projects" / "quaid" / "SOUL.md"
        project.parent.mkdir(parents=True, exist_ok=True)
        project.write_text("# SOUL\n\nProject base.\n")
        result = _resolve_writable_file_path("SOUL.md")
        assert result == workspace_dir / "identity" / "SOUL.md"
        assert result.read_text() == "# SOUL\n\nProject base.\n"

    def test_returns_none_for_arbitrary_missing_file(self, workspace_dir):
        """Non-identity filename with no matching file returns None."""
        from datastore.insightdb.soul_snippets import _resolve_writable_file_path
        result = _resolve_writable_file_path("TOTALLY_ABSENT.md")
        assert result is None


class TestInsertIntoFileBugRegression:
    """Unit tests for _insert_into_file() — especially the missing-file path."""

    def test_missing_file_raises_under_fail_hard(self, workspace_dir):
        """_insert_into_file must raise loudly when fail-hard is enabled."""
        from datastore.insightdb.soul_snippets import _insert_into_file
        with pytest.raises(RuntimeError, match="Identity file missing"):
            _insert_into_file("GHOST.md", "Some text.", "END")

    def test_existing_file_returns_true(self, workspace_dir):
        """_insert_into_file returns True when target exists and insert succeeds."""
        from datastore.insightdb.soul_snippets import _insert_into_file
        parent = workspace_dir / "identity" / "SOUL.md"
        parent.write_text("# SOUL\n\nBase content.\n")
        result = _insert_into_file("SOUL.md", "New thought.", "END")
        assert result is True
        assert "New thought." in parent.read_text()

    def test_insert_into_file_does_not_overwrite_protected_region(self, workspace_dir):
        """Text inserted at END is appended after protected regions, not inside them."""
        from datastore.insightdb.soul_snippets import _insert_into_file
        parent = workspace_dir / "identity" / "SOUL.md"
        protected_block = (
            "# SOUL\n\n"
            "<!-- protected -->\n"
            "## Secret Section\n\n"
            "This must not be touched.\n"
            "<!-- /protected -->\n\n"
            "## Public Section\n\n"
            "Public text.\n"
        )
        parent.write_text(protected_block)
        result = _insert_into_file("SOUL.md", "Appended fact.", "END")
        assert result is True
        content = parent.read_text()
        assert "Appended fact." in content
        # Protected block must remain intact
        assert "This must not be touched." in content
        assert "<!-- protected -->" in content

    def test_insert_skips_protected_section_heading_and_finds_next(self, workspace_dir):
        """_insert_into_file skips section headings inside protected regions."""
        from datastore.insightdb.soul_snippets import _insert_into_file
        parent = workspace_dir / "identity" / "SOUL.md"
        content = (
            "# SOUL\n\n"
            "<!-- protected -->\n"
            "## Hidden\n\nDo not insert here.\n"
            "<!-- /protected -->\n\n"
            "## Open\n\nInsert here.\n"
        )
        parent.write_text(content)
        # Ask to insert after "Hidden" — should be blocked by protection
        # and fall through to END rather than inserting into protected block.
        result = _insert_into_file("SOUL.md", "Safe insert.", "Hidden")
        assert result is True
        final = parent.read_text()
        assert "Safe insert." in final
        # The protected block must not be modified
        protected_start = final.index("<!-- protected -->")
        protected_end = final.index("<!-- /protected -->") + len("<!-- /protected -->")
        assert "Safe insert." not in final[protected_start:protected_end]


class TestApplyDecisionsFileMissingBug:
    """Regression tests for the AttributeError bug: _resolve_writable_file_path
    returning None and the caller calling .exists() on None."""

    def test_fold_file_missing_records_error_not_attributeerror(self, workspace_dir):
        """When _insert_into_file returns False AND file is missing, error is
        recorded as 'file missing' string — no AttributeError is raised."""
        from datastore.insightdb.soul_snippets import apply_decisions

        # DO NOT create the parent file — it must be absent
        all_snippets = {
            "SOUL.md": {
                "parent_content": "# SOUL\n\nBase.\n",
                "snippets": ["A profound observation."],
                "config": {"purpose": "Personality", "maxLines": 80},
            }
        }
        decisions = [
            {"file": "SOUL.md", "snippet_index": 1, "action": "FOLD",
             "insert_after": "END", "reason": "test"}
        ]
        # Must not raise AttributeError
        stats = apply_decisions(decisions, all_snippets, dry_run=False)
        assert isinstance(stats, dict)
        assert len(stats["errors"]) == 1
        error_msg = stats["errors"][0].lower()
        assert "missing" in error_msg or "file" in error_msg

    def test_fold_file_missing_does_not_count_as_folded(self, workspace_dir):
        """A FOLD that fails because the file is missing must NOT increment folded."""
        from datastore.insightdb.soul_snippets import apply_decisions

        all_snippets = {
            "SOUL.md": {
                "parent_content": "# SOUL\n\nBase.\n",
                "snippets": ["Should not fold."],
                "config": {"purpose": "Personality", "maxLines": 80},
            }
        }
        decisions = [
            {"file": "SOUL.md", "snippet_index": 1, "action": "FOLD",
             "insert_after": "END", "reason": "test"}
        ]
        stats = apply_decisions(decisions, all_snippets, dry_run=False)
        assert stats["folded"] == 0

    def test_rewrite_file_missing_records_error_not_attributeerror(self, workspace_dir):
        """REWRITE with a missing target file records an error string, no exception."""
        from datastore.insightdb.soul_snippets import apply_decisions

        all_snippets = {
            "USER.md": {
                "parent_content": "# USER\n\nBase.\n",
                "snippets": ["User trait."],
                "config": {"purpose": "User model", "maxLines": 150},
            }
        }
        decisions = [
            {"file": "USER.md", "snippet_index": 1, "action": "REWRITE",
             "rewritten_text": "Rewritten user trait.", "insert_after": "END",
             "reason": "test"}
        ]
        stats = apply_decisions(decisions, all_snippets, dry_run=False)
        assert isinstance(stats, dict)
        # Must not raise; error recorded
        assert len(stats["errors"]) >= 1
        assert stats["rewritten"] == 0


class TestApplyDecisionsSkippedAtLimit:
    """Tests for skipped_at_limit branch in apply_decisions.

    This branch triggers only when _insert_into_file returns False AND the file
    exists AND current_lines >= max_lines. Since _insert_into_file only returns
    False when file is absent, we trigger this via mock.
    """

    def test_skipped_at_limit_stat_incremented(self, workspace_dir):
        """When insert returns False but file is at maxLines, skipped_at_limit is set."""
        from datastore.insightdb import soul_snippets
        from datastore.insightdb.soul_snippets import apply_decisions

        parent = workspace_dir / "identity" / "SOUL.md"
        # Write a file with exactly max_lines lines (81 including header)
        max_lines = 81
        parent.write_text("# SOUL\n" + "line\n" * (max_lines - 1))

        snippets_path = workspace_dir / "SOUL.snippets.md"
        snippets_path.write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- At-limit snippet.\n"
        )

        all_snippets = {
            "SOUL.md": {
                "parent_content": parent.read_text(),
                "snippets": ["At-limit snippet."],
                "config": {"purpose": "Personality", "maxLines": max_lines},
            }
        }
        decisions = [
            {"file": "SOUL.md", "snippet_index": 1, "action": "FOLD",
             "insert_after": "END", "reason": "test"}
        ]

        # Mock _insert_into_file to return False to simulate the "at limit" branch.
        with patch("datastore.insightdb.soul_snippets._insert_into_file", return_value=False):
            stats = apply_decisions(decisions, all_snippets, dry_run=False)

        assert stats["errors"] == []
        assert stats["skipped_at_limit"] == 1
        assert stats["discarded"] == 1  # also incremented

    def test_skipped_at_limit_clears_snippet_from_file(self, workspace_dir):
        """Snippet is cleared from .snippets.md on skipped_at_limit to prevent retry loops."""
        from datastore.insightdb.soul_snippets import apply_decisions

        max_lines = 81
        parent = workspace_dir / "identity" / "SOUL.md"
        parent.write_text("# SOUL\n" + "line\n" * (max_lines - 1))

        snippets_path = workspace_dir / "SOUL.snippets.md"
        snippets_path.write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- At-limit snippet to be cleared.\n"
        )

        all_snippets = {
            "SOUL.md": {
                "parent_content": parent.read_text(),
                "snippets": ["At-limit snippet to be cleared."],
                "config": {"purpose": "Personality", "maxLines": max_lines},
            }
        }
        decisions = [
            {"file": "SOUL.md", "snippet_index": 1, "action": "FOLD",
             "insert_after": "END", "reason": "test"}
        ]

        with patch("datastore.insightdb.soul_snippets._insert_into_file", return_value=False):
            apply_decisions(decisions, all_snippets, dry_run=False)

        # Snippet file should have been cleaned (snippet removed or file deleted)
        if snippets_path.exists():
            assert "At-limit snippet to be cleared." not in snippets_path.read_text()


class TestApplyDecisionsFoldRewriteDiscard:
    """Focused tests on the FOLD / REWRITE / DISCARD decision processing."""

    def test_discard_removes_snippet_from_snippets_file(self, workspace_dir):
        """DISCARD counts and removes the snippet from .snippets.md."""
        from datastore.insightdb.soul_snippets import apply_decisions

        snippets_path = workspace_dir / "SOUL.snippets.md"
        snippets_path.write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- Redundant observation.\n"
            "- Keeper observation.\n"
        )
        all_snippets = {
            "SOUL.md": {
                "parent_content": "# SOUL\n\nBase.\n",
                "snippets": ["Redundant observation.", "Keeper observation."],
                "config": {"purpose": "Personality", "maxLines": 80},
            }
        }
        decisions = [
            {"file": "SOUL.md", "snippet_index": 1, "action": "DISCARD",
             "reason": "Already in file"},
        ]
        stats = apply_decisions(decisions, all_snippets, dry_run=False)
        assert stats["discarded"] == 1
        assert stats["errors"] == []
        # Discarded snippet must be removed; keeper must remain
        if snippets_path.exists():
            remaining = snippets_path.read_text()
            assert "Redundant observation." not in remaining
            assert "Keeper observation." in remaining

    def test_fold_duplicate_target_in_live_file_is_discarded(self, workspace_dir):
        """FOLD should discard when the target text is already present in the live file."""
        from datastore.insightdb.soul_snippets import apply_decisions

        parent = workspace_dir / "identity" / "SOUL.md"
        parent.write_text("# SOUL\n\nExisting.\n\nI value quiet precision.\n")

        snippets_path = workspace_dir / "SOUL.snippets.md"
        snippets_path.write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- I value quiet precision.\n"
        )

        all_snippets = {
            "SOUL.md": {
                "parent_content": parent.read_text(),
                "snippets": ["I value quiet precision."],
                "config": {"purpose": "Personality", "maxLines": 80},
            }
        }
        decisions = [
            {"file": "SOUL.md", "snippet_index": 1, "action": "FOLD",
             "insert_after": "END", "reason": "Already represented"}
        ]

        stats = apply_decisions(decisions, all_snippets, dry_run=False)

        assert stats["folded"] == 0
        assert stats["discarded"] == 1
        assert stats["errors"] == []
        content = parent.read_text()
        assert content.count("I value quiet precision.") == 1
        assert not snippets_path.exists()

    def test_discard_does_not_write_to_parent_file(self, workspace_dir):
        """DISCARD must not insert anything into the parent markdown file."""
        from datastore.insightdb.soul_snippets import apply_decisions

        parent = workspace_dir / "identity" / "SOUL.md"
        parent.write_text("# SOUL\n\nExisting.\n")
        original = parent.read_text()

        all_snippets = {
            "SOUL.md": {
                "parent_content": original,
                "snippets": ["Should be discarded."],
                "config": {"purpose": "Personality", "maxLines": 80},
            }
        }
        decisions = [
            {"file": "SOUL.md", "snippet_index": 1, "action": "DISCARD",
             "reason": "Not needed"}
        ]
        apply_decisions(decisions, all_snippets, dry_run=False)
        assert parent.read_text() == original

    def test_rewrite_uses_rewritten_text_when_provided(self, workspace_dir):
        """REWRITE inserts rewritten_text, not original snippet text."""
        from datastore.insightdb.soul_snippets import apply_decisions

        parent = workspace_dir / "identity" / "SOUL.md"
        parent.write_text("# SOUL\n\nBase content.\n")

        snippets_path = workspace_dir / "SOUL.snippets.md"
        snippets_path.write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- Raw snippet text.\n"
        )

        all_snippets = {
            "SOUL.md": {
                "parent_content": parent.read_text(),
                "snippets": ["Raw snippet text."],
                "config": {"purpose": "Personality", "maxLines": 80},
            }
        }
        decisions = [
            {"file": "SOUL.md", "snippet_index": 1, "action": "REWRITE",
             "rewritten_text": "Polished rewritten text.", "insert_after": "END",
             "reason": "Voice adjustment"}
        ]
        stats = apply_decisions(decisions, all_snippets, dry_run=False)
        assert stats["rewritten"] == 1
        assert stats["errors"] == []
        content = parent.read_text()
        assert "Polished rewritten text." in content
        # Original raw text must NOT have been inserted
        assert "Raw snippet text." not in content

    def test_rewrite_falls_back_to_original_when_rewritten_text_missing(self, workspace_dir, caplog):
        """REWRITE with no rewritten_text falls back to original and logs a warning."""
        from datastore.insightdb.soul_snippets import apply_decisions
        import logging

        parent = workspace_dir / "identity" / "SOUL.md"
        parent.write_text("# SOUL\n\nBase content.\n")

        snippets_path = workspace_dir / "SOUL.snippets.md"
        snippets_path.write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- Original text used as fallback.\n"
        )

        all_snippets = {
            "SOUL.md": {
                "parent_content": parent.read_text(),
                "snippets": ["Original text used as fallback."],
                "config": {"purpose": "Personality", "maxLines": 80},
            }
        }
        decisions = [
            {"file": "SOUL.md", "snippet_index": 1, "action": "REWRITE",
             # No rewritten_text key at all
             "insert_after": "END", "reason": "Missing text"}
        ]
        with caplog.at_level(logging.WARNING, logger="datastore.insightdb.soul_snippets"):
            stats = apply_decisions(decisions, all_snippets, dry_run=False)

        assert stats["rewritten"] == 1
        assert stats["errors"] == []
        content = parent.read_text()
        assert "Original text used as fallback." in content
        assert "missing rewritten_text" in caplog.text.lower() or "rewritten_text" in caplog.text.lower()

    def test_rewrite_falls_back_to_original_when_rewritten_text_empty(self, workspace_dir):
        """REWRITE with empty rewritten_text string falls back to original."""
        from datastore.insightdb.soul_snippets import apply_decisions

        parent = workspace_dir / "identity" / "SOUL.md"
        parent.write_text("# SOUL\n\nBase content.\n")

        snippets_path = workspace_dir / "SOUL.snippets.md"
        snippets_path.write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- Fallback for empty rewrite.\n"
        )

        all_snippets = {
            "SOUL.md": {
                "parent_content": parent.read_text(),
                "snippets": ["Fallback for empty rewrite."],
                "config": {"purpose": "Personality", "maxLines": 80},
            }
        }
        decisions = [
            {"file": "SOUL.md", "snippet_index": 1, "action": "REWRITE",
             "rewritten_text": "   ",  # whitespace-only
             "insert_after": "END", "reason": "Empty rewrite test"}
        ]
        stats = apply_decisions(decisions, all_snippets, dry_run=False)
        assert stats["rewritten"] == 1
        content = parent.read_text()
        assert "Fallback for empty rewrite." in content

    def test_dry_run_does_not_write_to_parent(self, workspace_dir):
        """dry_run=True: FOLD increments stats but does NOT write to the parent file."""
        from datastore.insightdb.soul_snippets import apply_decisions

        parent = workspace_dir / "identity" / "SOUL.md"
        parent.write_text("# SOUL\n\nOriginal only.\n")
        original_content = parent.read_text()

        all_snippets = {
            "SOUL.md": {
                "parent_content": original_content,
                "snippets": ["Should not appear in file."],
                "config": {"purpose": "Personality", "maxLines": 80},
            }
        }
        decisions = [
            {"file": "SOUL.md", "snippet_index": 1, "action": "FOLD",
             "insert_after": "END", "reason": "test"}
        ]
        stats = apply_decisions(decisions, all_snippets, dry_run=True)
        assert stats["folded"] == 1
        assert stats["errors"] == []
        # File must be unchanged in dry_run mode
        assert parent.read_text() == original_content

    def test_dry_run_rewrite_increments_rewritten_stat(self, workspace_dir):
        """dry_run=True: REWRITE increments rewritten stat without writing."""
        from datastore.insightdb.soul_snippets import apply_decisions

        parent = workspace_dir / "identity" / "SOUL.md"
        parent.write_text("# SOUL\n\nOriginal.\n")
        original_content = parent.read_text()

        all_snippets = {
            "SOUL.md": {
                "parent_content": original_content,
                "snippets": ["Draft snippet."],
                "config": {"purpose": "Personality", "maxLines": 80},
            }
        }
        decisions = [
            {"file": "SOUL.md", "snippet_index": 1, "action": "REWRITE",
             "rewritten_text": "Polished version.", "insert_after": "END",
             "reason": "test"}
        ]
        stats = apply_decisions(decisions, all_snippets, dry_run=True)
        assert stats["rewritten"] == 1
        assert parent.read_text() == original_content

    def test_dry_run_does_not_clear_snippets_file(self, workspace_dir):
        """dry_run=True: .snippets.md is NOT modified even when DISCARD is chosen."""
        from datastore.insightdb.soul_snippets import apply_decisions

        snippets_path = workspace_dir / "SOUL.snippets.md"
        snippets_path.write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- Snippet to be discarded.\n"
        )
        original_snippets = snippets_path.read_text()

        all_snippets = {
            "SOUL.md": {
                "parent_content": "# SOUL\n\nBase.\n",
                "snippets": ["Snippet to be discarded."],
                "config": {"purpose": "Personality", "maxLines": 80},
            }
        }
        decisions = [
            {"file": "SOUL.md", "snippet_index": 1, "action": "DISCARD",
             "reason": "Redundant"}
        ]
        apply_decisions(decisions, all_snippets, dry_run=True)
        # Snippets file must not be changed
        assert snippets_path.read_text() == original_snippets

    def test_invalid_snippet_index_logged_as_error_not_raised(self, workspace_dir):
        """Out-of-range snippet_index is recorded as an error, not an exception."""
        from datastore.insightdb.soul_snippets import apply_decisions

        parent = workspace_dir / "identity" / "SOUL.md"
        parent.write_text("# SOUL\n\nBase.\n")

        all_snippets = {
            "SOUL.md": {
                "parent_content": parent.read_text(),
                "snippets": ["Only one snippet."],
                "config": {"purpose": "Personality", "maxLines": 80},
            }
        }
        decisions = [
            # snippet_index 99 is out of range (only index 1 is valid)
            {"file": "SOUL.md", "snippet_index": 99, "action": "FOLD",
             "insert_after": "END", "reason": "bad index"}
        ]
        # Must not raise any exception
        stats = apply_decisions(decisions, all_snippets, dry_run=False)
        assert len(stats["errors"]) == 1
        assert "99" in stats["errors"][0] or "invalid" in stats["errors"][0].lower()
        assert stats["folded"] == 0

    def test_zero_based_snippet_index_batch_is_accepted(self, workspace_dir):
        """A batch containing snippet_index=0 is treated as zero-based for that file."""
        from datastore.insightdb.soul_snippets import apply_decisions

        all_snippets = {
            "SOUL.md": {
                "parent_content": "# SOUL\n",
                "snippets": ["One.", "Two."],
                "config": {},
            }
        }
        decisions = [
            {"file": "SOUL.md", "snippet_index": 0, "action": "FOLD",
             "insert_after": "END"},
            {"file": "SOUL.md", "snippet_index": 1, "action": "DISCARD"},
        ]
        stats = apply_decisions(decisions, all_snippets, dry_run=True)
        assert stats["errors"] == []
        assert stats["folded"] == 1
        assert stats["discarded"] == 1

    def test_processed_snippets_removed_from_file_after_fold(self, workspace_dir):
        """After a successful FOLD, the snippet is removed from .snippets.md."""
        from datastore.insightdb.soul_snippets import apply_decisions

        parent = workspace_dir / "identity" / "SOUL.md"
        parent.write_text("# SOUL\n\nBase content.\n")

        snippets_path = workspace_dir / "SOUL.snippets.md"
        snippets_path.write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- Folded snippet.\n"
            "- Remaining snippet.\n"
        )

        all_snippets = {
            "SOUL.md": {
                "parent_content": parent.read_text(),
                "snippets": ["Folded snippet.", "Remaining snippet."],
                "config": {"purpose": "Personality", "maxLines": 80},
            }
        }
        decisions = [
            {"file": "SOUL.md", "snippet_index": 1, "action": "FOLD",
             "insert_after": "END", "reason": "Good insight"}
        ]
        stats = apply_decisions(decisions, all_snippets, dry_run=False)
        assert stats["folded"] == 1
        assert stats["errors"] == []
        # Folded snippet must be gone from .snippets.md
        if snippets_path.exists():
            remaining = snippets_path.read_text()
            assert "Folded snippet." not in remaining
            assert "Remaining snippet." in remaining

    def test_all_snippets_processed_removes_snippets_file(self, workspace_dir):
        """When every snippet in the file is processed, .snippets.md is deleted."""
        from datastore.insightdb.soul_snippets import apply_decisions

        parent = workspace_dir / "identity" / "SOUL.md"
        parent.write_text("# SOUL\n\nBase content.\n")

        snippets_path = workspace_dir / "SOUL.snippets.md"
        snippets_path.write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- Only snippet.\n"
        )

        all_snippets = {
            "SOUL.md": {
                "parent_content": parent.read_text(),
                "snippets": ["Only snippet."],
                "config": {"purpose": "Personality", "maxLines": 80},
            }
        }
        decisions = [
            {"file": "SOUL.md", "snippet_index": 1, "action": "FOLD",
             "insert_after": "END", "reason": "test"}
        ]
        apply_decisions(decisions, all_snippets, dry_run=False)
        # Snippets file should be deleted when no snippets remain
        assert not snippets_path.exists()

    def test_fold_and_discard_mixed_decisions(self, workspace_dir):
        """Mixed FOLD + DISCARD decisions in one batch are each handled correctly."""
        from datastore.insightdb.soul_snippets import apply_decisions

        parent = workspace_dir / "identity" / "SOUL.md"
        parent.write_text("# SOUL\n\nBase.\n")

        snippets_path = workspace_dir / "SOUL.snippets.md"
        snippets_path.write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- Keep this one.\n"
            "- Throw this away.\n"
        )

        all_snippets = {
            "SOUL.md": {
                "parent_content": parent.read_text(),
                "snippets": ["Keep this one.", "Throw this away."],
                "config": {"purpose": "Personality", "maxLines": 80},
            }
        }
        decisions = [
            {"file": "SOUL.md", "snippet_index": 1, "action": "FOLD",
             "insert_after": "END", "reason": "Good"},
            {"file": "SOUL.md", "snippet_index": 2, "action": "DISCARD",
             "reason": "Redundant"},
        ]
        stats = apply_decisions(decisions, all_snippets, dry_run=False)
        assert stats["folded"] == 1
        assert stats["discarded"] == 1
        assert stats["errors"] == []
        content = parent.read_text()
        assert "Keep this one." in content
        assert "Throw this away." not in content
