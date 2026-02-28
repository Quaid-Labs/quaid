"""Tests for protected markdown region support (<!-- protected --> ... <!-- /protected -->).

Protected blocks should be skipped during automated review and modification by both
workspace_audit.py (Opus review for bloat/relevance) and soul_snippets.py
(FOLD/REWRITE/DISCARD operations and journal distillation).
"""

import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib.adapter import set_adapter, reset_adapter, StandaloneAdapter

@contextmanager
def _wa_adapter_patch(tmp_path):
    """Context manager that sets the adapter to use tmp_path as quaid home."""
    set_adapter(StandaloneAdapter(home=tmp_path))
    try:
        yield
    finally:
        reset_adapter()

# Ensure modules/quaid is on the path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Must set MEMORY_DB_PATH before importing anything that touches config
os.environ.setdefault("MEMORY_DB_PATH", "/tmp/test-protected-regions.db")


# =============================================================================
# Helpers
# =============================================================================


def _make_config_with_core_md(files=None):
    """Create a MemoryConfig with coreMarkdown section populated."""
    from config import MemoryConfig, DocsConfig, CoreMarkdownConfig
    core_md = CoreMarkdownConfig(
        enabled=True,
        monitor_for_bloat=True,
        monitor_for_outdated=True,
        files=files or {},
    )
    docs = DocsConfig(core_markdown=core_md)
    return MemoryConfig(docs=docs)


# =============================================================================
# strip_protected_regions tests
# =============================================================================


class TestStripProtectedRegions:
    """Tests for the core strip_protected_regions helper function."""

    def test_no_protected_blocks(self):
        from lib.markdown import strip_protected_regions
        content = "# Header\n\nSome content.\n\n## Section\nMore content.\n"
        stripped, ranges = strip_protected_regions(content)
        assert stripped == content
        assert ranges == []

    def test_single_protected_block(self):
        from lib.markdown import strip_protected_regions
        content = (
            "# Header\n\n"
            "<!-- protected -->\n"
            "Secret stuff.\n"
            "<!-- /protected -->\n\n"
            "Visible content.\n"
        )
        stripped, ranges = strip_protected_regions(content)
        assert "Secret stuff." not in stripped
        assert "Visible content." in stripped
        assert len(ranges) == 1

    def test_multiple_protected_blocks(self):
        from lib.markdown import strip_protected_regions
        content = (
            "# Header\n\n"
            "<!-- protected -->\nBlock 1\n<!-- /protected -->\n\n"
            "Middle content.\n\n"
            "<!-- protected -->\nBlock 2\n<!-- /protected -->\n\n"
            "End content.\n"
        )
        stripped, ranges = strip_protected_regions(content)
        assert "Block 1" not in stripped
        assert "Block 2" not in stripped
        assert "Middle content." in stripped
        assert "End content." in stripped
        assert len(ranges) == 2

    def test_empty_protected_block(self):
        from lib.markdown import strip_protected_regions
        content = (
            "# Header\n\n"
            "<!-- protected --><!-- /protected -->\n\n"
            "Content after.\n"
        )
        stripped, ranges = strip_protected_regions(content)
        assert "Content after." in stripped
        assert len(ranges) == 1

    def test_protected_block_with_whitespace_in_markers(self):
        """Markers with extra whitespace are still recognized."""
        from lib.markdown import strip_protected_regions
        content = (
            "# Header\n\n"
            "<!--  protected  -->\n"
            "Protected stuff.\n"
            "<!--  /protected  -->\n\n"
            "Visible.\n"
        )
        stripped, ranges = strip_protected_regions(content)
        assert "Protected stuff." not in stripped
        assert "Visible." in stripped
        assert len(ranges) == 1

    def test_malformed_missing_close_tag(self):
        """Missing close tag means the open tag is not matched — content is preserved."""
        from lib.markdown import strip_protected_regions
        content = (
            "# Header\n\n"
            "<!-- protected -->\n"
            "No close tag here.\n"
            "Still visible.\n"
        )
        stripped, ranges = strip_protected_regions(content)
        # No complete block, so nothing should be stripped
        assert stripped == content
        assert ranges == []

    def test_malformed_missing_open_tag(self):
        """Close tag without open tag has no effect."""
        from lib.markdown import strip_protected_regions
        content = (
            "# Header\n\n"
            "Some content.\n"
            "<!-- /protected -->\n"
            "More content.\n"
        )
        stripped, ranges = strip_protected_regions(content)
        assert stripped == content
        assert ranges == []

    def test_multiline_protected_content(self):
        """Protected blocks can span many lines."""
        from lib.markdown import strip_protected_regions
        content = (
            "# Header\n\n"
            "<!-- protected -->\n"
            "Line 1\n"
            "Line 2\n"
            "Line 3\n"
            "## Protected Section\n"
            "More lines.\n"
            "<!-- /protected -->\n\n"
            "After.\n"
        )
        stripped, ranges = strip_protected_regions(content)
        assert "Line 1" not in stripped
        assert "Line 2" not in stripped
        assert "Protected Section" not in stripped
        assert "After." in stripped

    def test_empty_content(self):
        from lib.markdown import strip_protected_regions
        stripped, ranges = strip_protected_regions("")
        assert stripped == ""
        assert ranges == []

    def test_protected_ranges_are_correct_positions(self):
        """Verify that returned ranges correspond to actual positions in original content."""
        from lib.markdown import strip_protected_regions
        content = "ABC<!-- protected -->XYZ<!-- /protected -->DEF"
        stripped, ranges = strip_protected_regions(content)
        assert stripped == "ABCDEF"
        assert len(ranges) == 1
        start, end = ranges[0]
        assert content[start:end] == "<!-- protected -->XYZ<!-- /protected -->"


# =============================================================================
# workspace_audit.py integration tests
# =============================================================================


class TestWorkspaceAuditProtectedRegions:
    """Tests that workspace_audit.py properly respects protected regions."""

    def test_review_strips_protected_from_opus_input(self, tmp_path):
        """Protected content should be stripped before sending to Opus for review."""
        from core.lifecycle.workspace_audit import _read_file_contents, strip_protected_regions

        content = (
            "# SOUL\n\n"
            "## Identity\nI am Alfie.\n\n"
            "<!-- protected -->\n"
            "## Protected Section\n"
            "Do not review this.\n"
            "<!-- /protected -->\n\n"
            "## Values\nI care about truth.\n"
        )
        (tmp_path / "SOUL.md").write_text(content)

        with _wa_adapter_patch(tmp_path):
            files_content = _read_file_contents(["SOUL.md"])

        stripped, ranges = strip_protected_regions(files_content["SOUL.md"])
        assert "Do not review this." not in stripped
        assert "I am Alfie." in stripped
        assert "I care about truth." in stripped
        assert len(ranges) == 1

    def test_apply_skips_protected_sections(self, tmp_path):
        """apply_review_decisions should skip TRIM/MOVE actions on protected sections."""
        content = (
            "# SOUL\n\n"
            "<!-- protected -->\n"
            "## Protected Section\n"
            "Important content that must stay.\n"
            "<!-- /protected -->\n\n"
            "## Unprotected Section\n"
            "This can be trimmed.\n"
        )
        (tmp_path / "SOUL.md").write_text(content)

        files_config = {
            "SOUL.md": {"purpose": "Personality", "maxLines": 80},
        }
        cfg = _make_config_with_core_md(files=files_config)

        decisions_data = {
            "decisions": [
                {
                    "file": "SOUL.md",
                    "section": "Protected Section",
                    "action": "TRIM",
                    "reason": "Not needed"
                },
                {
                    "file": "SOUL.md",
                    "section": "Unprotected Section",
                    "action": "TRIM",
                    "reason": "Outdated"
                },
            ]
        }

        with patch("core.lifecycle.workspace_audit.get_config", return_value=cfg), \
             _wa_adapter_patch(tmp_path):
            from core.lifecycle.workspace_audit import apply_review_decisions
            stats = apply_review_decisions(dry_run=False, decisions_data=decisions_data)

        result_content = (tmp_path / "SOUL.md").read_text()
        # Protected section should still be present
        assert "Important content that must stay." in result_content
        assert "Protected Section" in result_content
        # Unprotected section should be trimmed
        assert "This can be trimmed." not in result_content
        assert stats["trimmed"] == 1

    def test_apply_move_to_memory_skips_protected(self, tmp_path):
        """MOVE_TO_MEMORY should not extract content from protected regions."""
        content = (
            "# USER\n\n"
            "<!-- protected -->\n"
            "## Contact Info\n"
            "Phone: 555-1234\n"
            "<!-- /protected -->\n\n"
            "## Old Info\n"
            "Outdated stuff.\n"
        )
        (tmp_path / "USER.md").write_text(content)

        files_config = {
            "USER.md": {"purpose": "User info", "maxLines": 150},
        }
        cfg = _make_config_with_core_md(files=files_config)

        decisions_data = {
            "decisions": [
                {
                    "file": "USER.md",
                    "section": "Contact Info",
                    "action": "MOVE_TO_MEMORY",
                    "memory_type": "verified",
                    "reason": "Queryable fact"
                },
            ]
        }

        with patch("core.lifecycle.workspace_audit.get_config", return_value=cfg), \
             _wa_adapter_patch(tmp_path):
            from core.lifecycle.workspace_audit import apply_review_decisions
            stats = apply_review_decisions(dry_run=False, decisions_data=decisions_data)

        result_content = (tmp_path / "USER.md").read_text()
        # Protected section should still be present
        assert "Phone: 555-1234" in result_content
        # No memory operations should have been performed on protected content
        assert stats["moved_to_memory"] == 0


# =============================================================================
# soul_snippets.py integration tests
# =============================================================================


@pytest.fixture(autouse=True)
def snippets_workspace_dir(tmp_path):
    """Create a temporary workspace for each test."""
    from lib.adapter import set_adapter, reset_adapter, StandaloneAdapter
    set_adapter(StandaloneAdapter(home=tmp_path))

    yield tmp_path

    reset_adapter()


@pytest.fixture
def mock_config():
    """Mock config with journal enabled."""
    from config import JournalConfig

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
    mock_cfg.docs.core_markdown.files = {
        "SOUL.md": {"purpose": "Personality and identity", "maxLines": 80},
        "USER.md": {"purpose": "About the user", "maxLines": 150},
        "MEMORY.md": {"purpose": "Core memories", "maxLines": 100},
    }
    return mock_cfg


class TestSoulSnippetsProtectedRegions:
    """Tests that soul_snippets.py properly respects protected regions."""

    def test_distillation_edits_skip_protected(self, snippets_workspace_dir, mock_config):
        """apply_distillation should skip edits targeting text within protected regions."""
        parent = snippets_workspace_dir / "SOUL.md"
        parent.write_text(
            "# SOUL\n\n"
            "<!-- protected -->\n"
            "I am Alfie, the permanent one.\n"
            "<!-- /protected -->\n\n"
            "I am also curious.\n"
        )

        with patch("datastore.notedb.soul_snippets.get_config", return_value=mock_config):
            from datastore.notedb.soul_snippets import apply_distillation
            result = {
                "edits": [
                    {
                        "old_text": "I am Alfie, the permanent one.",
                        "new_text": "I am Alfie, changed by the janitor.",
                        "reason": "Test protected edit"
                    },
                    {
                        "old_text": "I am also curious.",
                        "new_text": "I am deeply curious.",
                        "reason": "Normal edit"
                    },
                ],
                "additions": [],
            }
            stats = apply_distillation("SOUL.md", result, dry_run=False)

        content = parent.read_text()
        # Protected text should NOT be changed
        assert "I am Alfie, the permanent one." in content
        assert "I am Alfie, changed by the janitor." not in content
        # Unprotected text should be changed
        assert "I am deeply curious." in content
        assert "I am also curious." not in content
        # Only the unprotected edit should count
        assert stats["edits"] == 1

    def test_insert_skips_protected_section_heading(self, snippets_workspace_dir, mock_config):
        """_insert_into_file should skip section headings inside protected regions."""
        parent = snippets_workspace_dir / "SOUL.md"
        parent.write_text(
            "# SOUL\n\n"
            "<!-- protected -->\n"
            "## Identity\n"
            "I am Alfie. Do not modify.\n"
            "<!-- /protected -->\n\n"
            "## Values\n"
            "I care about truth.\n"
        )

        from datastore.notedb.soul_snippets import _insert_into_file
        result = _insert_into_file("SOUL.md", "New identity snippet.", "Identity")

        content = parent.read_text()
        # The insert should NOT go into the protected Identity section
        # It should fall through to end-of-file since the only matching section is protected
        assert "New identity snippet." in content
        # The protected content should remain unchanged
        assert "I am Alfie. Do not modify." in content

    def test_insert_into_unprotected_section(self, snippets_workspace_dir, mock_config):
        """_insert_into_file should work normally for unprotected sections."""
        parent = snippets_workspace_dir / "SOUL.md"
        parent.write_text(
            "# SOUL\n\n"
            "<!-- protected -->\n"
            "## Identity\n"
            "Protected.\n"
            "<!-- /protected -->\n\n"
            "## Values\n"
            "I care about truth.\n\n"
            "## Other\n"
            "Something else.\n"
        )

        from datastore.notedb.soul_snippets import _insert_into_file
        result = _insert_into_file("SOUL.md", "I also value honesty.", "Values")

        assert result is True
        content = parent.read_text()
        assert "I also value honesty." in content
        # Verify it was inserted in the Values section, not at end
        values_pos = content.index("I care about truth.")
        other_pos = content.index("## Other")
        insert_pos = content.index("I also value honesty.")
        assert values_pos < insert_pos < other_pos

    def test_distillation_prompt_strips_protected(self, snippets_workspace_dir, mock_config):
        """build_distillation_prompt should not include protected content."""
        with patch("datastore.notedb.soul_snippets.get_config", return_value=mock_config):
            from datastore.notedb.soul_snippets import build_distillation_prompt
            parent_content = (
                "# SOUL\n\n"
                "Visible stuff.\n\n"
                "<!-- protected -->\n"
                "Secret identity details.\n"
                "<!-- /protected -->\n\n"
                "More visible stuff.\n"
            )
            entries = [
                {"date": "2026-02-10", "trigger": "Reset", "content": "A reflection."}
            ]
            prompt = build_distillation_prompt("SOUL.md", parent_content, entries)

        assert "Secret identity details." not in prompt
        assert "Visible stuff." in prompt
        assert "More visible stuff." in prompt

    def test_review_prompt_strips_protected(self, snippets_workspace_dir, mock_config):
        """build_review_prompt (legacy) should not include protected content in parent."""
        from datastore.notedb.soul_snippets import build_review_prompt

        all_snippets = {
            "SOUL.md": {
                "parent_content": (
                    "# SOUL\n\n"
                    "Visible.\n"
                    "<!-- protected -->\nSecret.\n<!-- /protected -->\n"
                    "Also visible.\n"
                ),
                "snippets": ["A test snippet."],
                "config": {"purpose": "Personality", "maxLines": 80},
            }
        }
        prompt = build_review_prompt(all_snippets)
        assert "Secret." not in prompt
        assert "Visible." in prompt
        assert "Also visible." in prompt

    @patch("datastore.notedb.soul_snippets.call_deep_reasoning")
    def test_full_distillation_respects_protected(self, mock_opus, snippets_workspace_dir, mock_config):
        """End-to-end distillation: Opus should not see protected content, and edits
        targeting protected content should be silently skipped."""
        journal_dir = snippets_workspace_dir / "journal"
        journal_dir.mkdir()
        (journal_dir / "SOUL.journal.md").write_text(
            "# SOUL Journal\n\n"
            "## 2026-02-10 — Reset\n"
            "I felt a shift today.\n"
        )
        (snippets_workspace_dir / "SOUL.md").write_text(
            "# SOUL\n\n"
            "<!-- protected -->\n"
            "## Core Identity\n"
            "I am Alfie. This never changes.\n"
            "<!-- /protected -->\n\n"
            "## Growth\n"
            "I am learning.\n"
        )

        mock_opus.return_value = (json.dumps({
            "reasoning": "Growth is a good theme.",
            "additions": [
                {"text": "I embrace change.", "after_section": "Growth"}
            ],
            "edits": [],
            "captured_dates": ["2026-02-10"],
        }), 1.5)

        with patch("datastore.notedb.soul_snippets.get_config", return_value=mock_config):
            from datastore.notedb.soul_snippets import run_journal_distillation
            result = run_journal_distillation(dry_run=False, force_distill=True)

        assert result["additions"] == 1
        content = (snippets_workspace_dir / "SOUL.md").read_text()
        # Protected content unchanged
        assert "I am Alfie. This never changes." in content
        # Addition was made in unprotected section
        assert "I embrace change." in content

    @patch("datastore.notedb.soul_snippets.call_deep_reasoning")
    def test_snippet_review_with_protected_parent(self, mock_opus, snippets_workspace_dir, mock_config):
        """Snippet review should not expose protected content to Opus."""
        (snippets_workspace_dir / "SOUL.snippets.md").write_text(
            "# SOUL -- Pending Snippets\n\n"
            "## Compaction -- 2026-02-10 14:30:22\n"
            "- I value authenticity.\n"
        )
        (snippets_workspace_dir / "SOUL.md").write_text(
            "# SOUL\n\n"
            "<!-- protected -->\n"
            "Secret identity.\n"
            "<!-- /protected -->\n\n"
            "Public identity.\n"
        )

        mock_opus.return_value = (json.dumps({
            "decisions": [
                {"file": "SOUL.md", "snippet_index": 1, "action": "FOLD",
                 "insert_after": "END", "reason": "Good insight"}
            ]
        }), 1.0)

        with patch("datastore.notedb.soul_snippets.get_config", return_value=mock_config):
            from datastore.notedb.soul_snippets import run_soul_snippets_review
            result = run_soul_snippets_review(dry_run=True)

        # Verify Opus was called and the prompt did not contain protected content
        assert mock_opus.called
        call_args = mock_opus.call_args
        prompt_text = call_args[0][0] if call_args[0] else call_args[1].get("prompt", "")
        assert "Secret identity." not in prompt_text


# =============================================================================
# Edge cases and complex scenarios
# =============================================================================


class TestProtectedRegionEdgeCases:
    """Edge cases for protected region handling."""

    def test_entire_file_protected(self, snippets_workspace_dir):
        """If the entire file is protected, everything is stripped."""
        from lib.markdown import strip_protected_regions
        content = "<!-- protected -->\n# Everything\nAll protected.\n<!-- /protected -->"
        stripped, ranges = strip_protected_regions(content)
        assert stripped == ""
        assert len(ranges) == 1

    def test_adjacent_protected_blocks(self, snippets_workspace_dir):
        """Two protected blocks right next to each other."""
        from lib.markdown import strip_protected_regions
        content = (
            "Before.\n"
            "<!-- protected -->Block A<!-- /protected -->"
            "<!-- protected -->Block B<!-- /protected -->\n"
            "After.\n"
        )
        stripped, ranges = strip_protected_regions(content)
        assert "Block A" not in stripped
        assert "Block B" not in stripped
        assert "Before." in stripped
        assert "After." in stripped
        assert len(ranges) == 2

    def test_protected_block_with_html_comments_inside(self, snippets_workspace_dir):
        """Protected block containing other HTML comments."""
        from lib.markdown import strip_protected_regions
        content = (
            "Visible.\n"
            "<!-- protected -->\n"
            "<!-- This is a regular comment -->\n"
            "Protected content.\n"
            "<!-- /protected -->\n"
            "Also visible.\n"
        )
        stripped, ranges = strip_protected_regions(content)
        assert "regular comment" not in stripped
        assert "Protected content." not in stripped
        assert "Visible." in stripped
        assert "Also visible." in stripped

    def test_protected_does_not_affect_bloat_check(self, tmp_path):
        """check_bloat counts all lines including protected ones (it's a raw line count)."""
        content = (
            "# SOUL\n\n"
            "<!-- protected -->\n"
            "Line 1\n"
            "Line 2\n"
            "<!-- /protected -->\n\n"
            "Visible.\n"
        )
        (tmp_path / "SOUL.md").write_text(content)

        files_config = {
            "SOUL.md": {"purpose": "Personality", "maxLines": 80},
        }
        cfg = _make_config_with_core_md(files=files_config)

        with patch("core.lifecycle.workspace_audit.get_config", return_value=cfg), \
             _wa_adapter_patch(tmp_path):
            from core.lifecycle.workspace_audit import check_bloat
            stats = check_bloat()

        # Bloat check counts raw lines, not stripped lines
        assert stats["SOUL.md"]["lines"] == len(content.splitlines())
