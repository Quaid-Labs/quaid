"""Tests for core/log_rotation.py — token-budget-based log rotation."""

import pytest
from pathlib import Path
from datetime import datetime, timedelta

from core.log_rotation import rotate_log_file, rotate_project_logs, _estimate_tokens


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
