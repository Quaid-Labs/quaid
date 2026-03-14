"""Unit tests for lib/tools_domain_sync.py.

Covers sync_tools_domain_block(): marker detection, domain normalization,
atomic write, no-change detection, and edge cases.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.tools_domain_sync import (
    END_MARKER,
    START_MARKER,
    sync_tools_domain_block,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HEADER = "# TOOLS\n\nSome preamble.\n\n"
_FOOTER = "\n\nSome postamble.\n"


def _make_tools_md(tmp_path: Path, body: str = "") -> Path:
    """Create projects/quaid/TOOLS.md with start/end markers and given body."""
    p = tmp_path / "projects" / "quaid"
    p.mkdir(parents=True)
    content = _HEADER + START_MARKER + "\n" + body + "\n" + END_MARKER + _FOOTER
    tools = p / "TOOLS.md"
    tools.write_text(content, encoding="utf-8")
    return tools


# ---------------------------------------------------------------------------
# File-missing / marker-missing guards
# ---------------------------------------------------------------------------


class TestSyncGuards:
    def test_returns_false_when_no_tools_md(self, tmp_path):
        (tmp_path / "projects" / "quaid").mkdir(parents=True)
        assert sync_tools_domain_block({"technical": "code"}, workspace=tmp_path) is False

    def test_returns_false_when_start_marker_missing(self, tmp_path):
        p = tmp_path / "projects" / "quaid"
        p.mkdir(parents=True)
        # Only has END_MARKER, no START_MARKER
        (p / "TOOLS.md").write_text(f"content\n{END_MARKER}\n")
        assert sync_tools_domain_block({"technical": "code"}, workspace=tmp_path) is False

    def test_returns_false_when_end_marker_missing(self, tmp_path):
        p = tmp_path / "projects" / "quaid"
        p.mkdir(parents=True)
        (p / "TOOLS.md").write_text(f"content\n{START_MARKER}\n")
        assert sync_tools_domain_block({"technical": "code"}, workspace=tmp_path) is False

    def test_returns_false_when_no_change(self, tmp_path):
        """If the generated block is identical to the existing block, returns False."""
        domains = {"technical": "code and architecture"}
        # Write first time (should apply)
        _make_tools_md(tmp_path)
        sync_tools_domain_block(domains, workspace=tmp_path)
        # Second call with same domains: block already matches, no-op
        assert sync_tools_domain_block(domains, workspace=tmp_path) is False


# ---------------------------------------------------------------------------
# Normal update
# ---------------------------------------------------------------------------


class TestSyncUpdate:
    def test_returns_true_on_first_write(self, tmp_path):
        _make_tools_md(tmp_path)
        result = sync_tools_domain_block({"technical": "code"}, workspace=tmp_path)
        assert result is True

    def test_domain_appears_in_file(self, tmp_path):
        tools = _make_tools_md(tmp_path)
        sync_tools_domain_block({"health": "wellness and training"}, workspace=tmp_path)
        content = tools.read_text()
        assert "- `health`: wellness and training" in content

    def test_multiple_domains_sorted_alphabetically(self, tmp_path):
        tools = _make_tools_md(tmp_path)
        sync_tools_domain_block(
            {"work": "job decisions", "finance": "budget", "health": "wellness"},
            workspace=tmp_path,
        )
        content = tools.read_text()
        finance_pos = content.index("finance")
        health_pos = content.index("health")
        work_pos = content.index("work")
        assert finance_pos < health_pos < work_pos

    def test_preamble_and_postamble_preserved(self, tmp_path):
        tools = _make_tools_md(tmp_path)
        sync_tools_domain_block({"technical": "code"}, workspace=tmp_path)
        content = tools.read_text()
        assert "Some preamble." in content
        assert "Some postamble." in content

    def test_markers_still_present_after_write(self, tmp_path):
        tools = _make_tools_md(tmp_path)
        sync_tools_domain_block({"technical": "code"}, workspace=tmp_path)
        content = tools.read_text()
        assert START_MARKER in content
        assert END_MARKER in content

    def test_empty_domains_clears_block(self, tmp_path):
        tools = _make_tools_md(tmp_path, body="- `old`: old domain")
        sync_tools_domain_block({}, workspace=tmp_path)
        content = tools.read_text()
        assert "old" not in content
        # Markers still present
        assert START_MARKER in content
        assert END_MARKER in content

    def test_old_domain_removed_when_not_in_new_dict(self, tmp_path):
        tools = _make_tools_md(tmp_path)
        sync_tools_domain_block({"technical": "code", "health": "wellness"}, workspace=tmp_path)
        # Now remove "health"
        sync_tools_domain_block({"technical": "code"}, workspace=tmp_path)
        content = tools.read_text()
        assert "health" not in content
        assert "technical" in content


# ---------------------------------------------------------------------------
# Domain key normalization
# ---------------------------------------------------------------------------


class TestDomainNormalization:
    def test_uppercase_key_normalized(self, tmp_path):
        tools = _make_tools_md(tmp_path)
        sync_tools_domain_block({"TECHNICAL": "code"}, workspace=tmp_path)
        content = tools.read_text()
        # Should appear as lowercase
        assert "technical" in content

    def test_invalid_domain_key_skipped(self, tmp_path):
        """Keys that normalize to empty string are skipped."""
        tools = _make_tools_md(tmp_path)
        sync_tools_domain_block({"": "empty key"}, workspace=tmp_path)
        content = tools.read_text()
        # Should not error; block is updated (empty)
        assert START_MARKER in content

    def test_description_appears_verbatim(self, tmp_path):
        tools = _make_tools_md(tmp_path)
        desc = "budgeting, purchases, salary, bills"
        sync_tools_domain_block({"finance": desc}, workspace=tmp_path)
        content = tools.read_text()
        assert desc in content


# ---------------------------------------------------------------------------
# Atomic write safety
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_file_encoding_is_utf8(self, tmp_path):
        tools = _make_tools_md(tmp_path)
        sync_tools_domain_block({"personal": "identité et préférences"}, workspace=tmp_path)
        content = tools.read_text(encoding="utf-8")
        assert "identité" in content

    def test_no_leftover_tmp_files(self, tmp_path):
        tools_dir = tmp_path / "projects" / "quaid"
        _make_tools_md(tmp_path)
        sync_tools_domain_block({"technical": "code"}, workspace=tmp_path)
        # No temp files should remain
        leftover = [f for f in tools_dir.iterdir() if f.name != "TOOLS.md"]
        assert leftover == []
