"""Tests for core/log_rotation.py — token-budget-based log rotation."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.log_rotation import (
    rotate_log_file,
    rotate_project_logs,
    rotate_journal_logs,
    _estimate_tokens,
    _parse_line_timestamp,
)


class TestEstimateTokens:
    def test_basic_estimate(self):
        assert _estimate_tokens("hello") >= 1
        assert _estimate_tokens("a" * 100) == 25  # 100 / 4

    def test_empty(self):
        assert _estimate_tokens("") == 1  # min 1


class TestRotateLogFile:
    def test_no_rotation_when_under_budget(self, tmp_path):
        log = tmp_path / "test.log"
        log.write_text("- [2026-03-11T10:00:00] entry 1\n- [2026-03-11T10:01:00] entry 2\n")

        archived, kept = rotate_log_file(log, token_budget=10000)
        assert archived == 0
        assert kept == 2

    def test_rotates_old_entries_over_budget(self, tmp_path):
        log = tmp_path / "test.log"
        # Create enough entries to exceed a small budget
        lines = []
        for i in range(50):
            ts = f"2026-03-{(i % 28) + 1:02d}T10:00:00"
            lines.append(f"- [{ts}] entry {i} with some padding text to use tokens")
        log.write_text("\n".join(lines) + "\n")

        # Use a very small budget to force rotation
        archived, kept = rotate_log_file(log, token_budget=200)
        assert archived > 0
        assert kept > 0
        assert archived + kept == 50

        # Check archive was created
        archive_dir = tmp_path / "log"
        assert archive_dir.is_dir()
        archive_files = list(archive_dir.glob("*.log"))
        assert len(archive_files) > 0

    def test_never_splits_entry(self, tmp_path):
        log = tmp_path / "test.log"
        # Two entries, budget can only fit one
        entry1 = "- [2026-03-01T10:00:00] " + "x" * 100
        entry2 = "- [2026-03-11T10:00:00] " + "y" * 100
        log.write_text(f"{entry1}\n{entry2}\n")

        archived, kept = rotate_log_file(log, token_budget=30)
        # Should archive entry1 and keep entry2 (most recent)
        remaining = log.read_text().strip()
        assert "y" * 100 in remaining
        assert archived == 1
        assert kept == 1

    def test_custom_archive_dir(self, tmp_path):
        log = tmp_path / "test.log"
        lines = [f"- [2026-01-{i+1:02d}T10:00:00] entry {i}" for i in range(20)]
        log.write_text("\n".join(lines) + "\n")

        custom_archive = tmp_path / "my-archives"
        rotate_log_file(log, archive_dir=custom_archive, token_budget=50)
        assert custom_archive.is_dir()

    def test_nonexistent_file(self, tmp_path):
        archived, kept = rotate_log_file(tmp_path / "missing.log")
        assert archived == 0
        assert kept == 0

    def test_empty_file(self, tmp_path):
        log = tmp_path / "test.log"
        log.write_text("")
        archived, kept = rotate_log_file(log, token_budget=1000)
        assert archived == 0
        assert kept == 0

    def test_archives_grouped_by_month(self, tmp_path):
        log = tmp_path / "test.log"
        lines = [
            "- [2026-01-15T10:00:00] jan entry",
            "- [2026-02-15T10:00:00] feb entry",
            "- [2026-03-15T10:00:00] mar entry (recent)",
        ]
        log.write_text("\n".join(lines) + "\n")

        # Budget only fits the last entry (~11 tokens)
        rotate_log_file(log, token_budget=12)

        archive_dir = tmp_path / "log"
        jan = archive_dir / "2026-01.log"
        feb = archive_dir / "2026-02.log"
        assert jan.exists()
        assert feb.exists()
        assert "jan entry" in jan.read_text()
        assert "feb entry" in feb.read_text()


class TestParseLineTimestamp:
    def test_iso_timestamp_parsed(self):
        ts = _parse_line_timestamp("- [2026-03-11T10:00:00] some entry")
        assert ts is not None
        assert ts.year == 2026
        assert ts.month == 3
        assert ts.day == 11

    def test_date_only_timestamp_parsed(self):
        ts = _parse_line_timestamp("- [2026-01-15] date-only entry")
        assert ts is not None
        assert ts.year == 2026
        assert ts.month == 1

    def test_no_timestamp_returns_none(self):
        assert _parse_line_timestamp("no timestamp here") is None

    def test_empty_line_returns_none(self):
        assert _parse_line_timestamp("") is None

    def test_malformed_timestamp_returns_none(self):
        assert _parse_line_timestamp("- [not-a-date] content") is None


class TestRotateLogFileEdgeCases:
    def test_undated_entries_grouped_under_undated(self, tmp_path):
        log = tmp_path / "test.log"
        log.write_text(
            "no timestamp line 1\n"
            "no timestamp line 2\n"
            "- [2026-03-11T10:00:00] recent entry\n"
        )
        rotate_log_file(log, token_budget=12)
        archive_dir = tmp_path / "log"
        # undated file should exist
        undated = archive_dir / "undated.log"
        assert undated.exists()

    def test_date_only_timestamps_archived_by_month(self, tmp_path):
        log = tmp_path / "test.log"
        log.write_text(
            "- [2026-01-15] january entry\n"
            "- [2026-03-11T10:00:00] march recent\n"
        )
        rotate_log_file(log, token_budget=10)
        archive_dir = tmp_path / "log"
        jan = archive_dir / "2026-01.log"
        assert jan.exists()
        assert "january entry" in jan.read_text()

    def test_whitespace_only_lines_not_counted(self, tmp_path):
        log = tmp_path / "test.log"
        log.write_text("\n\n   \n- [2026-03-11T10:00:00] only entry\n\n")
        archived, kept = rotate_log_file(log, token_budget=10000)
        assert archived == 0

    def test_most_recent_entry_always_kept(self, tmp_path):
        """Even if budget is 1, the most recent entry survives."""
        log = tmp_path / "test.log"
        log.write_text(
            "- [2026-01-01T10:00:00] " + "a" * 500 + "\n"
            "- [2026-03-11T10:00:00] recent-marker\n"
        )
        archived, kept = rotate_log_file(log, token_budget=1)
        remaining = log.read_text()
        assert "recent-marker" in remaining
        assert archived >= 1
        assert kept >= 1


class TestRotateProjectLogs:
    def test_rotates_all_projects(self, tmp_path):
        for name in ["proj-a", "proj-b"]:
            d = tmp_path / name
            d.mkdir()
            lines = [f"- [2026-01-{i+1:02d}T10:00:00] entry {i}" for i in range(20)]
            (d / "PROJECT.log").write_text("\n".join(lines) + "\n")

        total = rotate_project_logs(tmp_path, token_budget=50)
        assert total > 0

    def test_skips_hidden_dirs(self, tmp_path):
        hidden = tmp_path / ".hidden"
        hidden.mkdir()
        (hidden / "PROJECT.log").write_text("- [2026-01-01T10:00:00] should not be touched\n")

        total = rotate_project_logs(tmp_path, token_budget=1)
        assert total == 0

    def test_no_projects_dir(self, tmp_path):
        total = rotate_project_logs(tmp_path / "nonexistent")
        assert total == 0

    def test_project_without_log_skipped(self, tmp_path):
        """A project directory without PROJECT.log contributes 0."""
        no_log = tmp_path / "no-log-proj"
        no_log.mkdir()
        total = rotate_project_logs(tmp_path, token_budget=50)
        assert total == 0


class TestRotateJournalLogs:
    def test_no_journal_dir_returns_zero(self, tmp_path):
        assert rotate_journal_logs(tmp_path / "nonexistent") == 0

    def test_journal_dir_empty_returns_zero(self, tmp_path):
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        assert rotate_journal_logs(journal_dir) == 0

    def test_rotates_journal_files(self, tmp_path):
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        jf = journal_dir / "2026-03.journal.md"
        lines = [f"- [2026-01-{i+1:02d}T10:00:00] journal entry {i}" for i in range(20)]
        jf.write_text("\n".join(lines) + "\n")

        total = rotate_journal_logs(journal_dir, token_budget=50)
        assert total > 0

    def test_journal_archive_placed_under_stem(self, tmp_path):
        """Archives for foo.journal.md go in journal/archive/foo.journal/."""
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        jf = journal_dir / "2026-01.journal.md"
        lines = [
            "- [2026-01-01T10:00:00] jan entry",
            "- [2026-03-11T10:00:00] mar recent",
        ]
        jf.write_text("\n".join(lines) + "\n")

        rotate_journal_logs(journal_dir, token_budget=10)
        archive_base = journal_dir / "archive" / "2026-01.journal"
        assert archive_base.is_dir()

    def test_non_journal_md_files_ignored(self, tmp_path):
        """Only *.journal.md files are rotated."""
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        other = journal_dir / "notes.md"
        lines = [f"- [2026-01-{i+1:02d}T10:00:00] note {i}" for i in range(20)]
        other.write_text("\n".join(lines) + "\n")

        total = rotate_journal_logs(journal_dir, token_budget=1)
        assert total == 0
