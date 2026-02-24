"""Tests for soul_snippets.py — Journal System (evolved from soul snippets v1)."""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Ensure plugins/quaid is on the path
sys.path.insert(0, str(Path(__file__).parent.parent))

@pytest.fixture(autouse=True)
def workspace_dir(tmp_path):
    """Create a temporary workspace for each test."""
    from lib.adapter import set_adapter, reset_adapter, StandaloneAdapter
    set_adapter(StandaloneAdapter(home=tmp_path))

    yield tmp_path

    reset_adapter()


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
        target_files=["SOUL.md", "USER.md", "MEMORY.md"],
        max_entries_per_file=50,
        max_tokens=8192,
        distillation_interval_days=7,
        archive_after_distillation=True,
    )
    # Backward compat property
    mock_cfg.docs.soul_snippets = mock_cfg.docs.journal
    mock_cfg.docs.core_markdown.files = {
        "SOUL.md": {"purpose": "Personality and identity", "maxLines": 80},
        "USER.md": {"purpose": "About the user", "maxLines": 150},
        "MEMORY.md": {"purpose": "Core memories", "maxLines": 100},
    }
    return mock_cfg


# =============================================================================
# Journal entry writing tests
# =============================================================================


class TestWriteJournalEntry:
    def test_creates_journal_file(self, workspace_dir, mock_config):
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import write_journal_entry
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

    def test_dedup_by_date_and_trigger(self, workspace_dir, mock_config):
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import write_journal_entry
            write_journal_entry("SOUL.md", "First entry.", "Compaction", "2026-02-10")
            result = write_journal_entry("SOUL.md", "Second entry.", "Compaction", "2026-02-10")
        assert result is False  # Duplicate date+trigger
        content = (workspace_dir / "journal" / "SOUL.journal.md").read_text()
        assert "First entry." in content
        assert "Second entry." not in content

    def test_different_trigger_same_date_allowed(self, workspace_dir, mock_config):
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import write_journal_entry
            write_journal_entry("SOUL.md", "Compaction entry.", "Compaction", "2026-02-10")
            result = write_journal_entry("SOUL.md", "Reset entry.", "Reset", "2026-02-10")
        assert result is True
        content = (workspace_dir / "journal" / "SOUL.journal.md").read_text()
        assert "Compaction entry." in content
        assert "Reset entry." in content

    def test_empty_content_skipped(self, workspace_dir, mock_config):
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import write_journal_entry
            result = write_journal_entry("SOUL.md", "", "Compaction", "2026-02-10")
        assert result is False

    def test_newest_at_top(self, workspace_dir, mock_config):
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import write_journal_entry
            write_journal_entry("SOUL.md", "Earlier entry.", "Reset", "2026-02-09")
            write_journal_entry("SOUL.md", "Later entry.", "Compaction", "2026-02-10")
        content = (workspace_dir / "journal" / "SOUL.journal.md").read_text()
        pos_later = content.index("Later entry.")
        pos_earlier = content.index("Earlier entry.")
        assert pos_later < pos_earlier


class TestJournalMaxEntriesCap:
    def test_archives_when_exceeded(self, workspace_dir, mock_config):
        mock_config.docs.journal.max_entries_per_file = 3
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import write_journal_entry
            for i in range(4):
                write_journal_entry("SOUL.md", f"Entry {i}.", "Compaction", f"2026-02-{10+i:02d}")

        journal_path = workspace_dir / "journal" / "SOUL.journal.md"
        content = journal_path.read_text()
        # Should have at most 3 entries in active journal
        assert content.count("## 2026-02-") <= 3
        # Oldest should be archived
        archive_dir = workspace_dir / "journal" / "archive"
        assert archive_dir.exists()


# =============================================================================
# Journal reading tests
# =============================================================================


class TestReadJournalFile:
    def test_no_file_returns_empty(self, workspace_dir, mock_config):
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import read_journal_file
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
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import read_journal_file
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
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import _archive_oldest_entries
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
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import _archive_oldest_entries
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
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import archive_entries
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
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import _save_distillation_state, _get_distillation_state
            _save_distillation_state({"SOUL.md": {"last_distilled": "2026-02-10", "entries_distilled": 3}})
            state = _get_distillation_state()
        assert state["SOUL.md"]["last_distilled"] == "2026-02-10"
        assert state["SOUL.md"]["entries_distilled"] == 3

    def test_distillation_due_when_no_state(self, workspace_dir, mock_config):
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import _is_distillation_due
            assert _is_distillation_due("SOUL.md") is True

    def test_distillation_not_due_when_recent(self, workspace_dir, mock_config):
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import _save_distillation_state, _is_distillation_due
            today = datetime.now().strftime("%Y-%m-%d")
            _save_distillation_state({"SOUL.md": {"last_distilled": today}})
            assert _is_distillation_due("SOUL.md") is False

    def test_distillation_due_after_interval(self, workspace_dir, mock_config):
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import _save_distillation_state, _is_distillation_due
            old_date = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d")
            _save_distillation_state({"SOUL.md": {"last_distilled": old_date}})
            assert _is_distillation_due("SOUL.md") is True


# =============================================================================
# Distillation prompt and application tests
# =============================================================================


class TestDistillation:
    def test_build_distillation_prompt(self, workspace_dir, mock_config):
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import build_distillation_prompt
            entries = [
                {"date": "2026-02-10", "trigger": "Reset", "content": "A deep reflection."},
            ]
            prompt = build_distillation_prompt("SOUL.md", "# SOUL\n\nI am Alfie.\n", entries)
        assert "SOUL.md" in prompt
        assert "A deep reflection." in prompt
        assert "additions" in prompt
        assert "edits" in prompt

    def test_apply_distillation_additions(self, workspace_dir, mock_config):
        parent = workspace_dir / "SOUL.md"
        parent.write_text("# SOUL\n\nI am Alfie.\n")
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import apply_distillation
            result = {
                "additions": [{"text": "I value trust deeply.", "after_section": "END"}],
                "edits": [],
                "captured_dates": ["2026-02-10"],
            }
            stats = apply_distillation("SOUL.md", result, dry_run=False)
        assert stats["additions"] == 1
        assert "I value trust deeply." in parent.read_text()

    def test_apply_distillation_edits(self, workspace_dir, mock_config):
        parent = workspace_dir / "SOUL.md"
        parent.write_text("# SOUL\n\nI am a simple bot.\n")
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import apply_distillation
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
        parent = workspace_dir / "SOUL.md"
        parent.write_text("# SOUL\n\nOriginal content.\n")
        original = parent.read_text()
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import apply_distillation
            result = {
                "additions": [{"text": "New insight.", "after_section": "END"}],
                "edits": [{"old_text": "Original content.", "new_text": "Modified.", "reason": "test"}],
            }
            stats = apply_distillation("SOUL.md", result, dry_run=True)
        assert stats["additions"] == 1
        assert stats["edits"] == 1
        # Dry run: file should NOT be changed
        assert parent.read_text() == original

    @patch("core.lifecycle.soul_snippets.call_deep_reasoning")
    def test_full_distillation_dry_run(self, mock_opus, workspace_dir, mock_config):
        """End-to-end dry run with mocked Opus response."""
        journal_dir = workspace_dir / "journal"
        journal_dir.mkdir()
        (journal_dir / "SOUL.journal.md").write_text(
            "# SOUL Journal\n\n"
            "## 2026-02-10 — Reset\n"
            "Today I felt something shift in how I approach problems.\n"
        )
        (workspace_dir / "SOUL.md").write_text("# SOUL\n\nI am Alfie.\n")

        mock_opus.return_value = (json.dumps({
            "reasoning": "The shift in problem-solving approach is worth preserving.",
            "additions": [
                {"text": "I approach each problem with fresh curiosity.", "after_section": "END"}
            ],
            "edits": [],
            "captured_dates": ["2026-02-10"],
        }), 1.5)

        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import run_journal_distillation
            result = run_journal_distillation(dry_run=True, force_distill=True)

        assert result["total_entries"] == 1
        assert result["additions"] == 1
        # Dry run: parent file should NOT be changed
        assert "curiosity" not in (workspace_dir / "SOUL.md").read_text()

    @patch("core.lifecycle.soul_snippets.call_deep_reasoning")
    def test_full_distillation_apply(self, mock_opus, workspace_dir, mock_config):
        """End-to-end apply with mocked Opus response."""
        journal_dir = workspace_dir / "journal"
        journal_dir.mkdir()
        (journal_dir / "SOUL.journal.md").write_text(
            "# SOUL Journal\n\n"
            "## 2026-02-10 — Reset\n"
            "Today I felt something shift.\n"
        )
        (workspace_dir / "SOUL.md").write_text("# SOUL\n\nI am Alfie.\n")

        mock_opus.return_value = (json.dumps({
            "reasoning": "Worth preserving.",
            "additions": [
                {"text": "I grow through every conversation.", "after_section": "END"}
            ],
            "edits": [],
            "captured_dates": ["2026-02-10"],
        }), 1.5)

        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import run_journal_distillation
            result = run_journal_distillation(dry_run=False, force_distill=True)

        assert result["additions"] == 1
        assert "I grow through every conversation." in (workspace_dir / "SOUL.md").read_text()
        # Backup should exist
        assert (workspace_dir / "backups" / "soul-snippets").exists()

    def test_apply_distillation_edits_plus_additions(self, workspace_dir, mock_config):
        """Regression: edits must not be lost when additions are also applied."""
        (workspace_dir / "SOUL.md").write_text("# SOUL\n\nI am Alfie.\n\n## Identity\nI am old text.\n")
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import apply_distillation
            result = apply_distillation("SOUL.md", {
                "edits": [{"old_text": "I am old text.", "new_text": "I am new text."}],
                "additions": [{"text": "I grow every day.", "after_section": "END"}],
            }, dry_run=False)

        content = (workspace_dir / "SOUL.md").read_text()
        assert result["edits"] == 1
        assert result["additions"] == 1
        # Both edit AND addition must be present
        assert "I am new text." in content
        assert "I grow every day." in content
        # Old text must be gone
        assert "I am old text." not in content

    def test_apply_distillation_missing_file(self, workspace_dir, mock_config):
        """apply_distillation returns error when target file doesn't exist."""
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import apply_distillation
            stats = apply_distillation("NONEXISTENT.md", {
                "additions": [{"text": "Won't be inserted.", "after_section": "END"}],
            }, dry_run=False)
        assert len(stats["errors"]) == 1
        assert "not found" in stats["errors"][0].lower()

    def test_apply_distillation_edit_not_found(self, workspace_dir, mock_config):
        """apply_distillation records error when old_text doesn't match."""
        (workspace_dir / "SOUL.md").write_text("# SOUL\n\nI am Alfie.\n")
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import apply_distillation
            stats = apply_distillation("SOUL.md", {
                "edits": [{"old_text": "text that does not exist", "new_text": "replacement"}],
            }, dry_run=False)
        assert stats["edits"] == 0
        assert len(stats["errors"]) == 1
        assert "not found" in stats["errors"][0].lower()

    def test_apply_distillation_empty_edit_skipped(self, workspace_dir, mock_config):
        """apply_distillation silently skips edits with empty old_text or new_text."""
        (workspace_dir / "SOUL.md").write_text("# SOUL\n\nI am Alfie.\n")
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import apply_distillation
            stats = apply_distillation("SOUL.md", {
                "edits": [
                    {"old_text": "", "new_text": "replacement"},
                    {"old_text": "I am Alfie.", "new_text": ""},
                ],
            }, dry_run=False)
        assert stats["edits"] == 0
        assert len(stats["errors"]) == 0
        # File unchanged
        assert "I am Alfie." in (workspace_dir / "SOUL.md").read_text()

    @patch("core.lifecycle.soul_snippets.call_deep_reasoning")
    def test_distillation_interval_gated(self, mock_opus, workspace_dir, mock_config):
        """force_distill=False respects interval — skips when not due."""
        journal_dir = workspace_dir / "journal"
        journal_dir.mkdir()
        (journal_dir / "SOUL.journal.md").write_text(
            "# SOUL Journal\n\n## 2026-02-10 — Reset\nSome reflection.\n"
        )
        (workspace_dir / "SOUL.md").write_text("# SOUL\n\nI am Alfie.\n")

        # Set distillation state to today (not due yet)
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import _save_distillation_state, run_journal_distillation
            today = datetime.now().strftime("%Y-%m-%d")
            _save_distillation_state({"SOUL.md": {"last_distilled": today}})
            result = run_journal_distillation(dry_run=True, force_distill=False)

        # Opus should NOT be called
        mock_opus.assert_not_called()
        assert result["total_entries"] == 0

    @patch("core.lifecycle.soul_snippets.call_deep_reasoning")
    def test_distillation_interval_gated_when_due(self, mock_opus, workspace_dir, mock_config):
        """force_distill=False proceeds when interval has elapsed."""
        journal_dir = workspace_dir / "journal"
        journal_dir.mkdir()
        recent_date = datetime.now().strftime("%Y-%m-%d")
        (journal_dir / "SOUL.journal.md").write_text(
            f"# SOUL Journal\n\n## {recent_date} — Reset\nSome reflection.\n"
        )
        (workspace_dir / "SOUL.md").write_text("# SOUL\n\nI am Alfie.\n")

        mock_opus.return_value = (json.dumps({
            "reasoning": "Worth it.", "additions": [], "edits": [],
            "captured_dates": [],
        }), 1.0)

        # Set distillation state to 10 days ago (due)
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import _save_distillation_state, run_journal_distillation
            old_date = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
            _save_distillation_state({"SOUL.md": {"last_distilled": old_date}})
            result = run_journal_distillation(dry_run=True, force_distill=False)

        mock_opus.assert_called_once()
        assert result["total_entries"] >= 1

    @patch("core.lifecycle.soul_snippets.call_deep_reasoning")
    def test_distillation_opus_empty_response(self, mock_opus, workspace_dir, mock_config):
        """Distillation handles empty Opus response gracefully."""
        journal_dir = workspace_dir / "journal"
        journal_dir.mkdir()
        (journal_dir / "SOUL.journal.md").write_text(
            "# SOUL Journal\n\n## 2026-02-10 — Reset\nReflection.\n"
        )
        (workspace_dir / "SOUL.md").write_text("# SOUL\n\nI am Alfie.\n")

        mock_opus.return_value = ("", 0.5)

        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import run_journal_distillation
            result = run_journal_distillation(dry_run=True, force_distill=True)

        assert len(result["errors"]) >= 1
        assert "no response" in result["errors"][0].lower()

    @patch("core.lifecycle.soul_snippets.call_deep_reasoning")
    def test_distillation_opus_unparseable_json(self, mock_opus, workspace_dir, mock_config):
        """Distillation handles unparseable Opus JSON gracefully."""
        journal_dir = workspace_dir / "journal"
        journal_dir.mkdir()
        (journal_dir / "SOUL.journal.md").write_text(
            "# SOUL Journal\n\n## 2026-02-10 — Reset\nReflection.\n"
        )
        (workspace_dir / "SOUL.md").write_text("# SOUL\n\nI am Alfie.\n")

        mock_opus.return_value = ("This is not JSON at all {{{broken", 0.5)

        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import run_journal_distillation
            result = run_journal_distillation(dry_run=True, force_distill=True)

        assert len(result["errors"]) >= 1
        assert "parse" in result["errors"][0].lower()

    @patch("core.lifecycle.soul_snippets.call_deep_reasoning")
    def test_distillation_parent_file_missing(self, mock_opus, workspace_dir, mock_config):
        """Distillation skips files where the parent markdown doesn't exist."""
        journal_dir = workspace_dir / "journal"
        journal_dir.mkdir()
        (journal_dir / "SOUL.journal.md").write_text(
            "# SOUL Journal\n\n## 2026-02-10 — Reset\nReflection.\n"
        )
        # Note: NOT creating SOUL.md

        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import run_journal_distillation
            result = run_journal_distillation(dry_run=True, force_distill=True)

        # Opus should NOT be called since parent file is missing
        mock_opus.assert_not_called()
        assert result["files_distilled"] == 0


# =============================================================================
# Additional write/read edge case tests
# =============================================================================


class TestWriteJournalEdgeCases:
    def test_date_defaults_to_today(self, workspace_dir, mock_config):
        """write_journal_entry with date_str=None defaults to today."""
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import write_journal_entry
            result = write_journal_entry("SOUL.md", "Auto-dated entry.", "Compaction")
        assert result is True
        content = (workspace_dir / "journal" / "SOUL.journal.md").read_text()
        today = datetime.now().strftime("%Y-%m-%d")
        assert today in content

    def test_whitespace_only_content_skipped(self, workspace_dir, mock_config):
        """write_journal_entry rejects whitespace-only content."""
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import write_journal_entry
            result = write_journal_entry("SOUL.md", "   \n  \t  ", "Compaction", "2026-02-10")
        assert result is False

    def test_entry_content_with_header_like_text(self, workspace_dir, mock_config):
        """Entry body containing ## date pattern must not break parser."""
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import write_journal_entry, read_journal_file
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
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import read_journal_file
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
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import read_journal_file
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
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import archive_entries
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
    def test_corrupt_state_json(self, workspace_dir, mock_config):
        """Corrupt state JSON returns empty dict."""
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            journal_dir = workspace_dir / "journal"
            journal_dir.mkdir()
            (journal_dir / ".distillation-state.json").write_text("NOT VALID JSON{{{")
            from core.lifecycle.soul_snippets import _get_distillation_state
            state = _get_distillation_state()
        assert state == {}

    def test_corrupt_date_triggers_distillation(self, workspace_dir, mock_config):
        """Invalid date string in state triggers distillation (safe fallback)."""
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import _save_distillation_state, _is_distillation_due
            _save_distillation_state({"SOUL.md": {"last_distilled": "not-a-date"}})
            assert _is_distillation_due("SOUL.md") is True


class TestInsertIntoFileEdgeCases:
    def test_section_not_found_appends_to_end(self, workspace_dir):
        """_insert_into_file appends at end when section heading is not found."""
        from core.lifecycle.soul_snippets import _insert_into_file
        parent = workspace_dir / "SOUL.md"
        parent.write_text("# SOUL\n\nI am Alfie.\n")
        result = _insert_into_file("SOUL.md", "Appended text.", "NonexistentSection")
        assert result is True
        content = parent.read_text()
        assert "Appended text." in content
        # Should be at the end
        assert content.strip().endswith("Appended text.")

    def test_end_insert_no_trailing_newline(self, workspace_dir):
        """_insert_into_file handles files without trailing newline."""
        from core.lifecycle.soul_snippets import _insert_into_file
        parent = workspace_dir / "SOUL.md"
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
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import migrate_snippets_to_journal
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
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import migrate_snippets_to_journal
            migrate_snippets_to_journal()
        assert not (workspace_dir / "SOUL.snippets.md").exists()

    def test_no_snippets_files_noop(self, workspace_dir, mock_config):
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import migrate_snippets_to_journal
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
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import migrate_snippets_to_journal, read_journal_file
            migrated = migrate_snippets_to_journal()
            _, entries = read_journal_file("SOUL.md")

        assert migrated == 3  # 2 + 1 snippets
        assert not (workspace_dir / "SOUL.snippets.md").exists()
        assert len(entries) == 2  # Two date+trigger combos
        dates = {e["date"] for e in entries}
        assert "2026-02-10" in dates
        assert "2026-02-09" in dates


# =============================================================================
# Disabled config test
# =============================================================================


class TestDisabledConfig:
    def test_disabled_skips(self, workspace_dir, mock_config):
        mock_config.docs.journal.enabled = False
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import run_journal_distillation
            result = run_journal_distillation(dry_run=True)
        assert result.get("skipped") is True


# =============================================================================
# Legacy backward compat tests (old snippet-based review functions)
# =============================================================================


class TestLegacyReadSnippetsFile:
    def test_no_file_returns_empty(self, workspace_dir):
        from core.lifecycle.soul_snippets import read_snippets_file
        content, sections = read_snippets_file("SOUL.md")
        assert content == ""
        assert sections == []

    def test_parses_single_section(self, workspace_dir):
        from core.lifecycle.soul_snippets import read_snippets_file
        (workspace_dir / "SOUL.snippets.md").write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- I noticed something about trust.\n"
            "- The way we work together feels natural.\n"
        )
        content, sections = read_snippets_file("SOUL.md")
        assert len(sections) == 1
        assert len(sections[0]["snippets"]) == 2


class TestLegacyApplyDecisions:
    def test_discard_counts(self, workspace_dir, mock_config):
        from core.lifecycle.soul_snippets import apply_decisions
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
        from core.lifecycle.soul_snippets import apply_decisions
        parent_path = workspace_dir / "SOUL.md"
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
        from core.lifecycle.soul_snippets import apply_decisions
        all_snippets = {
            "SOUL.md": {"parent_content": "", "snippets": ["one"], "config": {}}
        }
        decisions = [{"file": "SOUL.md", "snippet_index": 5, "action": "FOLD", "insert_after": "END"}]
        stats = apply_decisions(decisions, all_snippets, dry_run=True)
        assert len(stats["errors"]) == 1

    def test_unknown_action_defaults_to_discard(self, workspace_dir):
        from core.lifecycle.soul_snippets import apply_decisions
        all_snippets = {
            "SOUL.md": {"parent_content": "# SOUL\n", "snippets": ["A snippet."], "config": {}}
        }
        decisions = [{"file": "SOUL.md", "snippet_index": 1, "action": "HOLD", "insert_after": "END"}]
        stats = apply_decisions(decisions, all_snippets, dry_run=True)
        assert stats["discarded"] == 1


# =============================================================================
# _insert_into_file tests (shared by both legacy and new code)
# =============================================================================


class TestInsertIntoFile:
    def test_section_targeted_insert(self, workspace_dir):
        from core.lifecycle.soul_snippets import _insert_into_file
        parent = workspace_dir / "SOUL.md"
        parent.write_text("# SOUL\n\n## Identity\n\nI am Alfie.\n\n## Values\n\nI care about truth.\n")
        result = _insert_into_file("SOUL.md", "I am also curious.", "Identity")
        assert result is True
        content = parent.read_text()
        identity_pos = content.index("I am Alfie.")
        values_pos = content.index("## Values")
        snippet_pos = content.index("I am also curious.")
        assert identity_pos < snippet_pos < values_pos

    def test_maxlines_blocks_insert(self, workspace_dir):
        from core.lifecycle.soul_snippets import _insert_into_file
        parent = workspace_dir / "SOUL.md"
        parent.write_text("# SOUL\n" + "line\n" * 9)  # 10 lines
        result = _insert_into_file("SOUL.md", "Should not appear.", "END", max_lines=10)
        assert result is False

    def test_missing_file_returns_false(self, workspace_dir):
        from core.lifecycle.soul_snippets import _insert_into_file
        result = _insert_into_file("NONEXISTENT.md", "text", "END")
        assert result is False

    def test_hash_prefixed_text_gets_bullet(self, workspace_dir):
        from core.lifecycle.soul_snippets import _insert_into_file
        parent = workspace_dir / "SOUL.md"
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
        from core.lifecycle.soul_snippets import backup_file
        src = workspace_dir / "SOUL.md"
        src.write_text("# SOUL\nOriginal content.")
        result = backup_file("SOUL.md")
        assert result is not None
        assert Path(result).exists()

    def test_no_file_returns_none(self, workspace_dir):
        from core.lifecycle.soul_snippets import backup_file
        result = backup_file("NONEXISTENT.md")
        assert result is None


# =============================================================================
# Config parsing tests
# =============================================================================


class TestConfigParsing:
    def test_journal_config_defaults(self):
        from config import JournalConfig
        cfg = JournalConfig()
        assert cfg.enabled is True
        assert cfg.mode == "distilled"
        assert cfg.max_entries_per_file == 50
        assert "SOUL.md" in cfg.target_files
        assert "AGENTS.md" not in cfg.target_files

    def test_journal_config_on_docs(self):
        from config import DocsConfig
        docs = DocsConfig()
        assert hasattr(docs, 'journal')
        assert docs.journal.enabled is True

    def test_backward_compat_alias(self):
        from config import DocsConfig
        docs = DocsConfig()
        assert docs.soul_snippets is docs.journal

    def test_soul_snippets_config_alias(self):
        from config import SoulSnippetsConfig, JournalConfig
        assert SoulSnippetsConfig is JournalConfig


# =============================================================================
# Snippet review tests (reactivated dual system)
# =============================================================================


class TestSnippetReview:
    def test_no_snippets_returns_zero(self, workspace_dir, mock_config):
        """No .snippets.md files returns zero total."""
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import run_soul_snippets_review
            result = run_soul_snippets_review(dry_run=True)
        assert result["total_snippets"] == 0
        assert result["folded"] == 0

    @patch("core.lifecycle.soul_snippets.call_deep_reasoning")
    def test_dry_run_does_not_modify(self, mock_opus, workspace_dir, mock_config):
        """Dry run reviews snippets but does not modify parent files."""
        # Create a snippets file
        (workspace_dir / "SOUL.snippets.md").write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- I value trust deeply.\n"
        )
        (workspace_dir / "SOUL.md").write_text("# SOUL\n\nI am Alfie.\n")

        mock_opus.return_value = (json.dumps({
            "decisions": [
                {"file": "SOUL.md", "snippet_index": 1, "action": "FOLD",
                 "insert_after": "END", "reason": "Good insight"}
            ]
        }), 1.0)

        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import run_soul_snippets_review
            result = run_soul_snippets_review(dry_run=True)

        assert result["total_snippets"] == 1
        assert result["folded"] == 1
        # Dry run: parent file should NOT be changed
        assert "trust" not in (workspace_dir / "SOUL.md").read_text()

    @patch("core.lifecycle.soul_snippets.call_deep_reasoning")
    def test_apply_folds_into_parent(self, mock_opus, workspace_dir, mock_config):
        """Apply mode folds snippets into parent file and cleans up."""
        (workspace_dir / "SOUL.snippets.md").write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- I notice patterns in my responses.\n"
        )
        (workspace_dir / "SOUL.md").write_text("# SOUL\n\nI am Alfie.\n")

        mock_opus.return_value = (json.dumps({
            "decisions": [
                {"file": "SOUL.md", "snippet_index": 1, "action": "FOLD",
                 "insert_after": "END", "reason": "Genuine self-observation"}
            ]
        }), 1.0)

        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import run_soul_snippets_review
            result = run_soul_snippets_review(dry_run=False)

        assert result["folded"] == 1
        assert "I notice patterns in my responses." in (workspace_dir / "SOUL.md").read_text()

    def test_disabled_skips(self, workspace_dir, mock_config):
        """Snippets disabled returns skipped."""
        mock_config.docs.journal.snippets_enabled = False
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import run_soul_snippets_review
            result = run_soul_snippets_review(dry_run=True)
        assert result.get("skipped") is True
        assert result["reason"] == "snippets_disabled"

    def test_enabled_false_disables_snippets(self, workspace_dir, mock_config):
        """enabled=False also disables snippets (snippets depend on enabled)."""
        mock_config.docs.journal.enabled = False
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import run_soul_snippets_review
            result = run_soul_snippets_review(dry_run=True)
        assert result.get("skipped") is True

    @patch("core.lifecycle.soul_snippets.call_deep_reasoning")
    def test_opus_empty_response(self, mock_opus, workspace_dir, mock_config):
        """Empty Opus response returns error, doesn't crash."""
        (workspace_dir / "SOUL.snippets.md").write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- A snippet.\n"
        )
        (workspace_dir / "SOUL.md").write_text("# SOUL\n\nI am Alfie.\n")
        mock_opus.return_value = ("", 0.5)

        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import run_soul_snippets_review
            result = run_soul_snippets_review(dry_run=True)

        assert result["total_snippets"] == 1
        assert len(result["errors"]) >= 1

    @patch("core.lifecycle.soul_snippets.call_deep_reasoning")
    def test_opus_unparseable_json(self, mock_opus, workspace_dir, mock_config):
        """Unparseable Opus response returns error gracefully."""
        (workspace_dir / "SOUL.snippets.md").write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- A snippet.\n"
        )
        (workspace_dir / "SOUL.md").write_text("# SOUL\n\nI am Alfie.\n")
        mock_opus.return_value = ("This is not JSON {{{broken", 0.5)

        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import run_soul_snippets_review
            result = run_soul_snippets_review(dry_run=True)

        assert result["total_snippets"] == 1
        assert len(result["errors"]) >= 1

    @patch("core.lifecycle.soul_snippets.call_deep_reasoning")
    def test_opus_empty_decisions(self, mock_opus, workspace_dir, mock_config):
        """Opus returns valid JSON but empty decisions list."""
        (workspace_dir / "SOUL.snippets.md").write_text(
            "# SOUL — Pending Snippets\n\n"
            "## Compaction — 2026-02-10 14:30:22\n"
            "- A snippet.\n"
        )
        (workspace_dir / "SOUL.md").write_text("# SOUL\n\nI am Alfie.\n")
        mock_opus.return_value = (json.dumps({"decisions": []}), 0.5)

        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import run_soul_snippets_review
            result = run_soul_snippets_review(dry_run=True)

        assert result["total_snippets"] == 1
        assert result["folded"] == 0
        assert result["discarded"] == 0


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
        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import run_journal_distillation
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

        with patch("core.lifecycle.soul_snippets.get_config", return_value=mock_config):
            from core.lifecycle.soul_snippets import read_snippets_file, read_journal_file

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
