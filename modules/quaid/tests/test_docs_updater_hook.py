"""Tests for core/docs_updater_hook.py — post-extraction docs update."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.docs_updater_hook import (
    update_project_docs,
    _build_update_context,
    _update_single_doc,
    _FAST_GATE_MAX_TOKENS,
)
from datastore.docsdb.updater import apply_edit_blocks


class TestApplyEditBlocks:
    def test_replace_text(self):
        doc = "# Title\n\nOld content here.\n\nMore stuff."
        edits = ["SECTION: Title\nOLD: Old content here.\nNEW: New content here."]
        updated, applied, unmatched = apply_edit_blocks(doc, edits)
        assert applied == 1
        assert unmatched == 0
        assert "New content here." in updated
        assert "Old content here." not in updated

    def test_add_content(self):
        doc = "# Title\n\nExisting."
        edits = ["SECTION: end\nOLD: ADD\nNEW: ## New Section\n\nNew stuff."]
        updated, applied, unmatched = apply_edit_blocks(doc, edits)
        assert applied == 1
        assert unmatched == 0
        assert "New Section" in updated
        assert "New stuff." in updated

    def test_no_match(self):
        doc = "# Title\n\nContent."
        edits = ["SECTION: Title\nOLD: Nonexistent text\nNEW: Replacement"]
        updated, applied, unmatched = apply_edit_blocks(doc, edits)
        assert applied == 0
        assert unmatched == 1
        assert updated == doc

    def test_multiple_edits(self):
        doc = "# Title\n\nAAA\n\nBBB"
        edits = [
            "SECTION: a\nOLD: AAA\nNEW: CCC",
            "SECTION: b\nOLD: BBB\nNEW: DDD",
        ]
        updated, applied, unmatched = apply_edit_blocks(doc, edits)
        assert applied == 2
        assert unmatched == 0
        assert "CCC" in updated
        assert "DDD" in updated

    def test_empty_edits(self):
        doc = "hello"
        updated, applied, unmatched = apply_edit_blocks(doc, [])
        assert applied == 0
        assert unmatched == 0
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

    def test_fast_gate_uses_non_truncating_token_budget(self, tmp_path):
        project_dir = tmp_path / "projects" / "my-app"
        project_dir.mkdir(parents=True)
        (project_dir / "PROJECT.md").write_text("# Project\n\nInitial.", encoding="utf-8")
        snapshots = [{
            "project": "my-app",
            "is_initial": False,
            "diff": "diff --git a/main.py b/main.py\n+print('hello')",
            "changes": [{"status": "M", "path": "main.py", "old_path": None}],
        }]

        with patch("datastore.docsdb.updater.classify_doc_change") as mock_classify, \
             patch("core.project_registry.get_project", return_value={"canonical_path": str(project_dir)}), \
             patch("lib.llm_clients.call_fast_reasoning", return_value=("NO: documentation is already current.", 0.1)) as fast:
            mock_classify.return_value = {
                "classification": "significant",
                "confidence": 0.5,
                "reasons": ["borderline"],
            }

            metrics = update_project_docs(snapshots)

        assert metrics["docs_skipped"] == 1
        fast.assert_called_once()
        assert fast.call_args.kwargs["max_tokens"] == _FAST_GATE_MAX_TOKENS
        assert _FAST_GATE_MAX_TOKENS > 50

    def test_fast_gate_failure_raises_when_fail_hard_enabled(self, tmp_path):
        project_dir = tmp_path / "projects" / "my-app"
        project_dir.mkdir(parents=True)
        (project_dir / "PROJECT.md").write_text("# Project\n\nInitial.", encoding="utf-8")
        snapshots = [{
            "project": "my-app",
            "is_initial": False,
            "diff": "diff --git a/main.py b/main.py\n+print('hello')",
            "changes": [{"status": "M", "path": "main.py", "old_path": None}],
        }]

        with patch("datastore.docsdb.updater.classify_doc_change") as mock_classify, \
             patch("core.project_registry.get_project", return_value={"canonical_path": str(project_dir)}), \
             patch("core.docs_updater_hook.is_fail_hard_enabled", return_value=True), \
             patch("lib.llm_clients.call_fast_reasoning", side_effect=RuntimeError("truncated while failHard is enabled")):
            mock_classify.return_value = {
                "classification": "significant",
                "confidence": 0.5,
                "reasons": ["borderline"],
            }

            with pytest.raises(RuntimeError, match="truncated"):
                update_project_docs(snapshots)

    def test_per_doc_update_failure_raises_when_fail_hard_enabled(self, tmp_path):
        project_dir = tmp_path / "projects" / "my-app"
        project_dir.mkdir(parents=True)
        (project_dir / "PROJECT.md").write_text("# Project\n\nInitial.", encoding="utf-8")
        snapshots = [{
            "project": "my-app",
            "is_initial": False,
            "diff": "diff --git a/main.py b/main.py\n+print('hello')",
            "changes": [{"status": "M", "path": "main.py", "old_path": None}],
        }]

        with patch("datastore.docsdb.updater.classify_doc_change") as mock_classify, \
             patch("core.project_registry.get_project", return_value={"canonical_path": str(project_dir)}), \
             patch("core.docs_updater_hook.is_fail_hard_enabled", return_value=True), \
             patch("core.docs_updater_hook._update_single_doc", side_effect=RuntimeError("doc update failed")):
            mock_classify.return_value = {
                "classification": "significant",
                "confidence": 0.8,
                "reasons": ["clear doc update"],
            }

            with pytest.raises(RuntimeError, match="doc update failed"):
                update_project_docs(snapshots)

    def test_update_rejects_partial_edit_blocks_without_writing(self, tmp_path):
        project_dir = tmp_path / "projects" / "my-app"
        project_dir.mkdir(parents=True)
        doc_path = project_dir / "PROJECT.md"
        original = "# Project\n\nInitial.\n\nStable."
        doc_path.write_text(original, encoding="utf-8")
        snapshots = [{
            "project": "my-app",
            "is_initial": False,
            "diff": "diff --git a/main.py b/main.py\n+print('hello')",
            "changes": [{"status": "M", "path": "main.py", "old_path": None}],
        }]
        response = (
            "<<<EDIT\n"
            "SECTION: Project\n"
            "OLD: Initial.\n"
            "NEW: Changed.\n"
            ">>>\n"
            "<<<EDIT\n"
            "SECTION: Project\n"
            "OLD: Missing old text\n"
            "NEW: Should not write\n"
            ">>>"
        )

        with patch("datastore.docsdb.updater.classify_doc_change") as mock_classify, \
             patch("core.project_registry.get_project", return_value={"canonical_path": str(project_dir)}), \
             patch("core.docs_updater_hook.is_fail_hard_enabled", return_value=False), \
             patch("lib.llm_clients.call_deep_reasoning", return_value=(response, 0.1)):
            mock_classify.return_value = {
                "classification": "significant",
                "confidence": 0.8,
                "reasons": ["clear doc update"],
            }

            metrics = update_project_docs(snapshots)

        assert metrics["docs_updated"] == 0
        assert metrics["docs_skipped"] == 1
        assert doc_path.read_text(encoding="utf-8") == original

    def test_update_unmatched_edit_raises_when_fail_hard_enabled(self, tmp_path):
        project_dir = tmp_path / "projects" / "my-app"
        project_dir.mkdir(parents=True)
        doc_path = project_dir / "PROJECT.md"
        original = "# Project\n\nInitial."
        doc_path.write_text(original, encoding="utf-8")
        snapshots = [{
            "project": "my-app",
            "is_initial": False,
            "diff": "diff --git a/main.py b/main.py\n+print('hello')",
            "changes": [{"status": "M", "path": "main.py", "old_path": None}],
        }]
        response = (
            "<<<EDIT\n"
            "SECTION: Project\n"
            "OLD: Missing old text\n"
            "NEW: Should not write\n"
            ">>>"
        )

        with patch("datastore.docsdb.updater.classify_doc_change") as mock_classify, \
             patch("core.project_registry.get_project", return_value={"canonical_path": str(project_dir)}), \
             patch("core.docs_updater_hook.is_fail_hard_enabled", return_value=True), \
             patch("lib.llm_clients.call_deep_reasoning", return_value=(response, 0.1)):
            mock_classify.return_value = {
                "classification": "significant",
                "confidence": 0.8,
                "reasons": ["clear doc update"],
            }

            with pytest.raises(RuntimeError, match="did not match PROJECT.md content"):
                update_project_docs(snapshots)

        assert doc_path.read_text(encoding="utf-8") == original

    def test_project_md_prompt_excludes_registry_managed_marker_blocks(self, tmp_path):
        doc_path = tmp_path / "PROJECT.md"
        doc_path.write_text(
            "# Project: Demo\n\n"
            "### Registered Docs\n"
            "<!-- BEGIN:REGISTERED_DOCS -->\n"
            "| Document | Why Read It | Auto-Update |\n"
            "|----------|-------------|-------------|\n"
            "<!-- END:REGISTERED_DOCS -->\n",
            encoding="utf-8",
        )
        captured = {}

        def _fake_deep_reasoning(**kwargs):
            captured["system_prompt"] = kwargs["system_prompt"]
            return "NO_CHANGES_NEEDED", 0.1

        with patch("lib.llm_clients.call_deep_reasoning", side_effect=_fake_deep_reasoning):
            assert _update_single_doc(
                doc_path,
                "## Changes\n\nRegistered source docs changed.",
                {"classification": "significant", "confidence": 0.8},
                dry_run=False,
            ) is False

        assert "Do not edit those marker blocks" in captured["system_prompt"]
        assert "Registered Docs" in captured["system_prompt"]
