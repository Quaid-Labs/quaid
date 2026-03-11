"""Tests for core/docs_updater_hook.py — post-extraction docs update."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.docs_updater_hook import (
    update_project_docs,
    _build_update_context,
)
from datastore.docsdb.updater import apply_edit_blocks


class TestApplyEditBlocks:
    def test_replace_text(self):
        doc = "# Title\n\nOld content here.\n\nMore stuff."
        edits = ["SECTION: Title\nOLD: Old content here.\nNEW: New content here."]
        updated, applied = apply_edit_blocks(doc, edits)
        assert applied == 1
        assert "New content here." in updated
        assert "Old content here." not in updated

    def test_add_content(self):
        doc = "# Title\n\nExisting."
        edits = ["SECTION: end\nOLD: ADD\nNEW: ## New Section\n\nNew stuff."]
        updated, applied = apply_edit_blocks(doc, edits)
        assert applied == 1
        assert "New Section" in updated
        assert "New stuff." in updated

    def test_no_match(self):
        doc = "# Title\n\nContent."
        edits = ["SECTION: Title\nOLD: Nonexistent text\nNEW: Replacement"]
        updated, applied = apply_edit_blocks(doc, edits)
        assert applied == 0
        assert updated == doc

    def test_multiple_edits(self):
        doc = "# Title\n\nAAA\n\nBBB"
        edits = [
            "SECTION: a\nOLD: AAA\nNEW: CCC",
            "SECTION: b\nOLD: BBB\nNEW: DDD",
        ]
        updated, applied = apply_edit_blocks(doc, edits)
        assert applied == 2
        assert "CCC" in updated
        assert "DDD" in updated

    def test_empty_edits(self):
        doc = "hello"
        updated, applied = apply_edit_blocks(doc, [])
        assert applied == 0
        assert updated == doc


class TestBuildUpdateContext:
    def test_includes_changes(self):
        ctx = _build_update_context(
            "my-app",
            diff_text="diff --git a/main.py",
            changes=[{"status": "M", "path": "main.py", "old_path": None}],
            project_log=["Added new feature"],
        )
        assert "my-app" in ctx
        assert "main.py" in ctx
        assert "modified" in ctx
        assert "diff --git" in ctx
        assert "Added new feature" in ctx

    def test_empty_context(self):
        ctx = _build_update_context("my-app", "", [], [])
        assert "my-app" in ctx

    def test_renamed_file(self):
        ctx = _build_update_context(
            "my-app", "",
            changes=[{"status": "R", "path": "new.py", "old_path": "old.py"}],
            project_log=[],
        )
        assert "renamed" in ctx
        assert "was: old.py" in ctx


class TestUpdateProjectDocs:
    def test_skips_trivial_changes(self):
        """Trivial diffs should not trigger any LLM calls."""
        # A diff that's just whitespace — classifier should mark trivial
        snapshots = [{
            "project": "my-app",
            "is_initial": False,
            "diff": " \n-  \n+  \n",
            "changes": [{"status": "M", "path": "main.py", "old_path": None}],
        }]

        with patch("datastore.docsdb.updater.classify_doc_change") as mock_classify:
            mock_classify.return_value = {
                "classification": "trivial",
                "confidence": 0.9,
                "reasons": ["whitespace only"],
            }
            metrics = update_project_docs(snapshots)
            assert metrics["trivial_skipped"] == 1
            assert metrics["docs_updated"] == 0

    def test_empty_snapshots(self):
        metrics = update_project_docs([])
        assert metrics["projects_checked"] == 0

    def test_no_diff_no_changes_skipped(self):
        snapshots = [{
            "project": "my-app",
            "is_initial": False,
            "diff": "",
            "changes": [],
        }]
        metrics = update_project_docs(snapshots)
        assert metrics["projects_checked"] == 0
