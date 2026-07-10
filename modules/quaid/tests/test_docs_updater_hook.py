"""Tests for core/docs_updater_hook.py — post-extraction docs update."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.docs_updater_hook import (
    update_project_docs,
    _build_update_context,
    _bounded_snapshot_diff,
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

    def test_diff_context_uses_prompt_byte_caps(self, monkeypatch):
        monkeypatch.setenv("QUAID_DOCS_PROMPT_DIFF_FILE_MAX_BYTES", "1024")
        diff_text = (
            "diff --git a/large.py b/large.py\n"
            "+++ b/large.py\n"
            + ("+" + "A" * 80 + "\n") * 80
        )

        bounded = _build_update_context("my-app", diff_text, [], [])

        assert len(bounded.encode("utf-8")) <= 1500
        assert "QUAID DOCS SAFETY CAP" in bounded
        assert "A" * 2000 not in bounded

    def test_diff_context_caps_total_snapshot_diff(self, monkeypatch):
        monkeypatch.setenv("QUAID_DOCS_PROMPT_DIFF_FILE_MAX_BYTES", "1200")
        monkeypatch.setenv("QUAID_DOCS_PROMPT_DIFF_MAX_BYTES", "4096")
        diff_text = "\n".join(
            f"diff --git a/file{i}.py b/file{i}.py\n+++ b/file{i}.py\n"
            + ("+" + str(i) * 80 + "\n") * 20
            for i in range(6)
        )

        bounded = _bounded_snapshot_diff(diff_text)

        assert len(bounded.encode("utf-8")) <= 4700
        assert "Snapshot diff prompt limit reached" in bounded
        assert "remaining_diff_sections_not_expanded" in bounded


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

    def test_docs_per_run_is_capped(self, tmp_path, monkeypatch, caplog):
        project_dir = tmp_path / "projects" / "my-app"
        docs_dir = project_dir / "docs"
        docs_dir.mkdir(parents=True)
        for name in ("AGENTS.md", "PROJECT.md", "TOOLS.md"):
            (project_dir / name).write_text(f"# {name}\n", encoding="utf-8")
        for index in range(3):
            (docs_dir / f"extra-{index}.md").write_text("# Extra\n", encoding="utf-8")
        monkeypatch.setenv("QUAID_DOCS_HOOK_MAX_DOCS_PER_RUN", "2")
        snapshots = [{
            "project": "my-app",
            "is_initial": False,
            "diff": "diff --git a/main.py b/main.py\n+print('hello')",
            "changes": [{"status": "M", "path": "main.py", "old_path": None}],
        }]

        caplog.set_level("WARNING")
        with patch("datastore.docsdb.updater.classify_doc_change") as mock_classify, \
             patch("core.project_registry.get_project", return_value={"canonical_path": str(project_dir)}), \
             patch("core.docs_updater_hook._update_single_doc", return_value=False) as update_single:
            mock_classify.return_value = {
                "classification": "significant",
                "confidence": 0.8,
                "reasons": ["clear doc update"],
            }

            metrics = update_project_docs(snapshots)

        assert update_single.call_count == 2
        assert metrics["docs_skipped"] == 6
        assert "limiting docs update run to 2 docs; 4 docs omitted" in caplog.text

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
        assert metrics["docs_skipped"] == 0
        assert metrics["errors"] == 1
        assert doc_path.read_text(encoding="utf-8") == original

    def test_update_unmatched_edit_records_error_when_fail_open(self, tmp_path, caplog):
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

        caplog.set_level("WARNING")
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

        assert doc_path.read_text(encoding="utf-8") == original
        assert metrics["docs_updated"] == 0
        assert metrics["docs_skipped"] == 0
        assert metrics["errors"] == 1
        assert "Unmatched edit block for PROJECT.md #1" in caplog.text
        assert "Missing old text" in caplog.text
        assert "Should not write" in caplog.text

    def test_update_unmatched_edit_raises_when_fail_hard_enabled(self, tmp_path, caplog):
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

        caplog.set_level("WARNING")
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
        assert "Unmatched edit block for PROJECT.md #1" in caplog.text
        assert "Failed to update" in caplog.text

    def test_project_md_empty_section_sentinel_applies_under_fail_hard(self, tmp_path):
        project_dir = tmp_path / "projects" / "my-app"
        project_dir.mkdir(parents=True)
        doc_path = project_dir / "PROJECT.md"
        doc_path.write_text(
            "# Project: Demo\n\n"
            "## What This Is\n"
            "Demo project.\n\n"
            "## Key Constraints and Decisions\n\n"
            "## Where To Learn More\n",
            encoding="utf-8",
        )
        snapshots = [{
            "project": "my-app",
            "is_initial": False,
            "diff": "diff --git a/api.py b/api.py\n+def demo(): pass",
            "changes": [{"status": "M", "path": "api.py", "old_path": None}],
        }]
        response = (
            "<<<EDIT\n"
            "SECTION: Key Constraints and Decisions\n"
            "OLD: (empty)\n"
            "NEW: - Not for production use; livetest fixture only.\n"
            ">>>\n"
            "<<<SUMMARY: captured livetest constraint >>>"
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

            metrics = update_project_docs(snapshots)

        content = doc_path.read_text(encoding="utf-8")
        assert metrics["docs_updated"] == 1
        assert "- Not for production use; livetest fixture only." in content
        assert "## Key Constraints and Decisions\n\n- Not for production use" in content

    def test_project_md_empty_section_sentinel_rejects_nonempty_section_under_fail_hard(
        self, tmp_path
    ):
        project_dir = tmp_path / "projects" / "my-app"
        project_dir.mkdir(parents=True)
        doc_path = project_dir / "PROJECT.md"
        original = (
            "# Project: Demo\n\n"
            "## Key Constraints and Decisions\n"
            "- Existing constraint.\n\n"
            "## Where To Learn More\n"
        )
        doc_path.write_text(original, encoding="utf-8")
        snapshots = [{
            "project": "my-app",
            "is_initial": False,
            "diff": "diff --git a/api.py b/api.py\n+def demo(): pass",
            "changes": [{"status": "M", "path": "api.py", "old_path": None}],
        }]
        response = (
            "<<<EDIT\n"
            "SECTION: Key Constraints and Decisions\n"
            "OLD: (empty)\n"
            "NEW: - Replacement should not apply.\n"
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

    def test_project_md_managed_marker_edit_is_ignored_under_fail_hard(self, tmp_path, caplog):
        project_dir = tmp_path / "projects" / "my-app"
        project_dir.mkdir(parents=True)
        doc_path = project_dir / "PROJECT.md"
        original = (
            "# Project: Demo\n\n"
            "### Registered Docs\n"
            "<!-- BEGIN:REGISTERED_DOCS -->\n"
            "| Document | Why Read It | Auto-Update |\n"
            "|----------|-------------|-------------|\n"
            "<!-- END:REGISTERED_DOCS -->\n"
        )
        doc_path.write_text(original, encoding="utf-8")
        snapshots = [{
            "project": "my-app",
            "is_initial": False,
            "diff": "diff --git a/README.md b/README.md\n+content",
            "changes": [{"status": "A", "path": "README.md", "old_path": None}],
        }]
        response = (
            "<<<EDIT\n"
            "SECTION: Registered Docs\n"
            "OLD: | Document | Why Read It | Auto-Update |\n"
            "|----------|-------------|-------------|\n"
            "NEW: | Document | Why Read It | Auto-Update |\n"
            "|----------|-------------|-------------|\n"
            "| `README.md` | Project overview | Yes |\n"
            ">>>"
        )

        caplog.set_level("WARNING")
        with patch("datastore.docsdb.updater.classify_doc_change") as mock_classify, \
             patch("core.project_registry.get_project", return_value={"canonical_path": str(project_dir)}), \
             patch("core.docs_updater_hook.is_fail_hard_enabled", return_value=True), \
             patch("lib.llm_clients.call_deep_reasoning", return_value=(response, 0.1)):
            mock_classify.return_value = {
                "classification": "significant",
                "confidence": 0.8,
                "reasons": ["clear doc update"],
            }

            metrics = update_project_docs(snapshots)

        assert metrics["docs_skipped"] == 1
        assert metrics["docs_updated"] == 0
        assert doc_path.read_text(encoding="utf-8") == original
        assert "Ignoring PROJECT.md LLM edit targeting registry-managed marker block" in caplog.text
        assert "Registered Docs" in caplog.text

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

    def test_oversized_current_doc_is_not_sent_to_llm(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("QUAID_DOCS_PROMPT_DOC_MAX_BYTES", "4096")
        doc_path = tmp_path / "TOOLS.md"
        doc_path.write_text("# Tools\n\n" + "A" * 5000, encoding="utf-8")

        caplog.set_level("WARNING")
        with patch(
            "lib.llm_clients.call_deep_reasoning",
            side_effect=AssertionError("oversized doc must not reach LLM"),
        ):
            updated = _update_single_doc(
                doc_path,
                "## Changes\n\nSome change.",
                {"classification": "significant", "confidence": 0.8},
                dry_run=False,
            )

        assert updated is False
        assert "current doc exceeds prompt safety cap" in caplog.text
