"""Unit tests for docs_updater.py — staleness checking, source mapping, git diffs."""

import sys
import os
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure the plugin root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Must set env vars before importing config (which caches)
os.environ["CLAWDBOT_WORKSPACE"] = "/tmp/test-workspace"

import pytest


# Build a minimal test config for docs_updater
def _make_test_config(source_mapping=None, doc_purposes=None, staleness_enabled=True):
    """Create a mock config with DocsConfig."""
    from config import MemoryConfig, DocsConfig, SourceMapping

    sm = {}
    if source_mapping:
        for src, data in source_mapping.items():
            sm[src] = SourceMapping(docs=data["docs"], label=data.get("label", ""))

    docs = DocsConfig(
        auto_update_on_compact=True,
        max_docs_per_update=3,
        staleness_check_enabled=staleness_enabled,
        source_mapping=sm,
        doc_purposes=doc_purposes or {},
    )

    return MemoryConfig(docs=docs)


class TestCheckStaleness:
    """Tests for check_staleness()."""

    def test_no_mapping_returns_empty(self, tmp_path):
        """No source mapping → no stale docs."""
        cfg = _make_test_config(source_mapping={})
        with patch("docs_updater.get_config", return_value=cfg), \
             patch("docs_updater.WORKSPACE", tmp_path):
            from docs_updater import check_staleness
            assert check_staleness() == {}

    def test_staleness_disabled_returns_empty(self, tmp_path):
        """When staleness check is disabled, returns empty."""
        cfg = _make_test_config(
            source_mapping={"src.py": {"docs": ["doc.md"]}},
            staleness_enabled=False,
        )
        with patch("docs_updater.get_config", return_value=cfg), \
             patch("docs_updater.WORKSPACE", tmp_path):
            from docs_updater import check_staleness
            assert check_staleness() == {}

    def test_detects_stale_doc(self, tmp_path):
        """Source file newer than doc → doc is stale."""
        # Create doc first, then source (so source is newer)
        doc_file = tmp_path / "docs" / "doc.md"
        doc_file.parent.mkdir(parents=True)
        doc_file.write_text("old doc content")

        import time
        time.sleep(0.05)  # Ensure mtime difference

        src_file = tmp_path / "src.py"
        src_file.write_text("updated source")

        cfg = _make_test_config(
            source_mapping={"src.py": {"docs": ["docs/doc.md"]}},
        )
        with patch("docs_updater.get_config", return_value=cfg), \
             patch("docs_updater.WORKSPACE", tmp_path):
            from docs_updater import check_staleness
            stale = check_staleness()
            assert "docs/doc.md" in stale
            assert stale["docs/doc.md"].gap_hours > 0
            assert "src.py" in stale["docs/doc.md"].stale_sources

    def test_up_to_date_doc_not_stale(self, tmp_path):
        """Source file older than doc → doc is not stale."""
        src_file = tmp_path / "src.py"
        src_file.write_text("source content")

        import time
        time.sleep(0.05)

        doc_file = tmp_path / "docs" / "doc.md"
        doc_file.parent.mkdir(parents=True)
        doc_file.write_text("fresh doc content")

        cfg = _make_test_config(
            source_mapping={"src.py": {"docs": ["docs/doc.md"]}},
        )
        with patch("docs_updater.get_config", return_value=cfg), \
             patch("docs_updater.WORKSPACE", tmp_path):
            from docs_updater import check_staleness
            stale = check_staleness()
            assert stale == {}

    def test_missing_doc_ignored(self, tmp_path):
        """If doc file doesn't exist, it's not reported as stale."""
        src_file = tmp_path / "src.py"
        src_file.write_text("source content")

        cfg = _make_test_config(
            source_mapping={"src.py": {"docs": ["docs/nonexistent.md"]}},
        )
        with patch("docs_updater.get_config", return_value=cfg), \
             patch("docs_updater.WORKSPACE", tmp_path):
            from docs_updater import check_staleness
            assert check_staleness() == {}

    def test_missing_source_ignored(self, tmp_path):
        """If source file doesn't exist, it's not reported."""
        doc_file = tmp_path / "docs" / "doc.md"
        doc_file.parent.mkdir(parents=True)
        doc_file.write_text("doc content")

        cfg = _make_test_config(
            source_mapping={"nonexistent.py": {"docs": ["docs/doc.md"]}},
        )
        with patch("docs_updater.get_config", return_value=cfg), \
             patch("docs_updater.WORKSPACE", tmp_path):
            from docs_updater import check_staleness
            assert check_staleness() == {}


class TestMapSourcesToDocs:
    """Tests for map_sources_to_docs()."""

    def test_maps_single_source(self):
        cfg = _make_test_config(
            source_mapping={"janitor.py": {"docs": ["docs/janitor-ref.md"]}},
        )
        with patch("docs_updater.get_config", return_value=cfg):
            from docs_updater import map_sources_to_docs
            result = map_sources_to_docs(["janitor.py"])
            assert "docs/janitor-ref.md" in result
            assert "janitor.py" in result["docs/janitor-ref.md"]

    def test_maps_multiple_sources_to_same_doc(self):
        cfg = _make_test_config(
            source_mapping={
                "index.ts": {"docs": ["docs/impl.md"]},
                "config.py": {"docs": ["docs/impl.md"]},
            },
        )
        with patch("docs_updater.get_config", return_value=cfg):
            from docs_updater import map_sources_to_docs
            result = map_sources_to_docs(["index.ts", "config.py"])
            assert "docs/impl.md" in result
            assert len(result["docs/impl.md"]) == 2

    def test_unmapped_source_ignored(self):
        cfg = _make_test_config(
            source_mapping={"janitor.py": {"docs": ["docs/janitor-ref.md"]}},
        )
        with patch("docs_updater.get_config", return_value=cfg):
            from docs_updater import map_sources_to_docs
            result = map_sources_to_docs(["unknown_file.py"])
            assert result == {}

    def test_empty_input(self):
        cfg = _make_test_config(
            source_mapping={"janitor.py": {"docs": ["docs/janitor-ref.md"]}},
        )
        with patch("docs_updater.get_config", return_value=cfg):
            from docs_updater import map_sources_to_docs
            assert map_sources_to_docs([]) == {}


class TestGetGitDiff:
    """Tests for get_git_diff()."""

    def test_returns_empty_for_nonexistent_file(self, tmp_path):
        with patch("docs_updater.WORKSPACE", tmp_path):
            from docs_updater import get_git_diff
            result = get_git_diff("nonexistent.py", 0.0)
            assert result == ""

    def test_handles_git_not_available(self, tmp_path):
        """If git commands fail, returns empty string gracefully."""
        src_file = tmp_path / "src.py"
        src_file.write_text("content")

        with patch("docs_updater.WORKSPACE", tmp_path), \
             patch("docs_updater.subprocess.run", side_effect=FileNotFoundError):
            from docs_updater import get_git_diff
            result = get_git_diff("src.py", 0.0)
            assert result == ""


class TestGetDocPurposes:
    """Tests for get_doc_purposes()."""

    def test_returns_purposes_from_config(self):
        purposes = {"docs/foo.md": "Foo documentation", "docs/bar.md": "Bar docs"}
        cfg = _make_test_config(doc_purposes=purposes)
        with patch("docs_updater.get_config", return_value=cfg):
            from docs_updater import get_doc_purposes
            result = get_doc_purposes()
            assert result == purposes

    def test_empty_purposes(self):
        cfg = _make_test_config(doc_purposes={})
        with patch("docs_updater.get_config", return_value=cfg):
            from docs_updater import get_doc_purposes
            assert get_doc_purposes() == {}


class TestDetectChangedSources:
    """Tests for detect_changed_sources_from_transcript()."""

    def test_returns_empty_on_llm_failure(self):
        cfg = _make_test_config(
            source_mapping={"janitor.py": {"docs": ["docs/ref.md"]}},
        )
        with patch("docs_updater.get_config", return_value=cfg), \
             patch("docs_updater.call_low_reasoning", return_value=(None, 1.0)):
            from docs_updater import detect_changed_sources_from_transcript
            result = detect_changed_sources_from_transcript("some transcript")
            assert result == []

    def test_parses_valid_response(self):
        cfg = _make_test_config(
            source_mapping={
                "janitor.py": {"docs": ["docs/ref.md"]},
                "config.py": {"docs": ["docs/impl.md"]},
            },
        )
        response = '{"changed": ["janitor.py"]}'
        with patch("docs_updater.get_config", return_value=cfg), \
             patch("docs_updater.call_low_reasoning", return_value=(response, 1.0)):
            from docs_updater import detect_changed_sources_from_transcript
            result = detect_changed_sources_from_transcript("modified janitor.py")
            assert result == ["janitor.py"]

    def test_filters_unknown_files(self):
        cfg = _make_test_config(
            source_mapping={"janitor.py": {"docs": ["docs/ref.md"]}},
        )
        response = '{"changed": ["janitor.py", "unknown.py"]}'
        with patch("docs_updater.get_config", return_value=cfg), \
             patch("docs_updater.call_low_reasoning", return_value=(response, 1.0)):
            from docs_updater import detect_changed_sources_from_transcript
            result = detect_changed_sources_from_transcript("some transcript")
            assert result == ["janitor.py"]

    def test_no_mapping_returns_empty(self):
        cfg = _make_test_config(source_mapping={})
        with patch("docs_updater.get_config", return_value=cfg):
            from docs_updater import detect_changed_sources_from_transcript
            assert detect_changed_sources_from_transcript("transcript") == []


class TestGetCoreMarkdownInfo:
    """Tests for _get_core_markdown_info()."""

    def test_detects_core_markdown_file(self):
        """Core markdown files return (purpose, maxLines)."""
        cfg = _make_test_config()
        cfg.docs.core_markdown = MagicMock()
        cfg.docs.core_markdown.files = {
            "TOOLS.md": {"purpose": "API docs and configs", "maxLines": 350},
        }
        with patch("docs_updater.get_config", return_value=cfg):
            from docs_updater import _get_core_markdown_info
            result = _get_core_markdown_info("TOOLS.md")
            assert result == ("API docs and configs", 350)

    def test_returns_none_for_regular_doc(self):
        """Non-core markdown files return None."""
        cfg = _make_test_config()
        cfg.docs.core_markdown = MagicMock()
        cfg.docs.core_markdown.files = {
            "TOOLS.md": {"purpose": "API docs", "maxLines": 350},
        }
        with patch("docs_updater.get_config", return_value=cfg):
            from docs_updater import _get_core_markdown_info
            result = _get_core_markdown_info("projects/quaid/janitor-reference.md")
            assert result is None

    def test_handles_path_with_directory(self):
        """Extracts basename from paths with directories."""
        cfg = _make_test_config()
        cfg.docs.core_markdown = MagicMock()
        cfg.docs.core_markdown.files = {
            "AGENTS.md": {"purpose": "System operations", "maxLines": 350},
        }
        with patch("docs_updater.get_config", return_value=cfg):
            from docs_updater import _get_core_markdown_info
            # Should not match — core markdown keys are bare filenames
            result = _get_core_markdown_info("some/path/AGENTS.md")
            assert result == ("System operations", 350)

    def test_returns_none_when_no_core_markdown_config(self):
        """Returns None when core_markdown config is empty."""
        cfg = _make_test_config()
        cfg.docs.core_markdown = MagicMock()
        cfg.docs.core_markdown.files = {}
        with patch("docs_updater.get_config", return_value=cfg):
            from docs_updater import _get_core_markdown_info
            result = _get_core_markdown_info("TOOLS.md")
            assert result is None


class TestClassifyDocChange:
    """Tests for classify_doc_change() — smart threshold for doc updates."""

    def test_empty_diff_is_trivial(self):
        """Empty diff → trivial with high confidence."""
        from docs_updater import classify_doc_change
        result = classify_doc_change("")
        assert result["classification"] == "trivial"
        assert result["confidence"] == 1.0
        assert "empty diff" in result["reasons"]

    def test_none_diff_is_trivial(self):
        """None diff → trivial."""
        from docs_updater import classify_doc_change
        result = classify_doc_change(None)
        assert result["classification"] == "trivial"
        assert result["confidence"] == 1.0

    def test_whitespace_only_is_trivial(self):
        """Whitespace-only changes → trivial."""
        from docs_updater import classify_doc_change
        diff = (
            "--- a/file.py\n"
            "+++ b/file.py\n"
            "-   \n"
            "+  \n"
            "-\n"
            "+\n"
        )
        result = classify_doc_change(diff)
        assert result["classification"] == "trivial"
        assert any("whitespace" in r for r in result["reasons"])

    def test_comment_only_is_trivial(self):
        """Comment-only changes → trivial."""
        from docs_updater import classify_doc_change
        diff = (
            "--- a/file.py\n"
            "+++ b/file.py\n"
            "-# old comment\n"
            "+# new comment\n"
        )
        result = classify_doc_change(diff)
        assert result["classification"] == "trivial"
        assert any("comment" in r for r in result["reasons"])

    def test_js_comment_is_trivial(self):
        """JavaScript comment changes → trivial."""
        from docs_updater import classify_doc_change
        diff = (
            "--- a/file.js\n"
            "+++ b/file.js\n"
            "-// old comment\n"
            "+// updated comment\n"
        )
        result = classify_doc_change(diff)
        assert result["classification"] == "trivial"
        assert any("comment" in r for r in result["reasons"])

    def test_import_change_is_trivial(self):
        """Import path changes → trivial."""
        from docs_updater import classify_doc_change
        diff = (
            "--- a/file.py\n"
            "+++ b/file.py\n"
            "-from old_module import something\n"
            "+from new_module import something\n"
        )
        result = classify_doc_change(diff)
        assert result["classification"] == "trivial"
        assert any("import" in r for r in result["reasons"])

    def test_version_bump_is_trivial(self):
        """Version bump → trivial."""
        from docs_updater import classify_doc_change
        diff = (
            "--- a/package.json\n"
            "+++ b/package.json\n"
            '-  "version": "1.2.3"\n'
            '+  "version": "1.2.4"\n'
        )
        result = classify_doc_change(diff)
        assert result["classification"] == "trivial"
        assert any("version" in r for r in result["reasons"])

    def test_typo_fix_is_trivial(self):
        """Typo-like edit (high character similarity) → trivial."""
        from docs_updater import classify_doc_change
        diff = (
            "--- a/file.py\n"
            "+++ b/file.py\n"
            "-This is a docstring with a tpyo\n"
            "+This is a docstring with a typo\n"
        )
        result = classify_doc_change(diff)
        assert result["classification"] == "trivial"
        assert any("typo" in r for r in result["reasons"])

    def test_new_function_is_significant(self):
        """New function definition → significant."""
        from docs_updater import classify_doc_change
        diff = (
            "--- a/file.py\n"
            "+++ b/file.py\n"
            "+def new_feature():\n"
            "+    pass\n"
            "+\n"
            "+def another_feature():\n"
            "+    return True\n"
            "+\n"
            "+# some comment\n"
            "+\n"
            "+def third_feature():\n"
            "+    return False\n"
        )
        result = classify_doc_change(diff)
        assert result["classification"] == "significant"
        assert any("function" in r for r in result["reasons"])

    def test_new_class_is_significant(self):
        """New class definition → significant."""
        from docs_updater import classify_doc_change
        diff = (
            "--- a/file.py\n"
            "+++ b/file.py\n"
            "+class NewFeature:\n"
            "+    def __init__(self):\n"
            "+        pass\n"
        )
        result = classify_doc_change(diff)
        assert result["classification"] == "significant"
        assert any("class" in r for r in result["reasons"])

    def test_schema_change_is_significant(self):
        """Schema change → significant."""
        from docs_updater import classify_doc_change
        diff = (
            "--- a/schema.sql\n"
            "+++ b/schema.sql\n"
            "+CREATE TABLE new_table (\n"
            "+    id INTEGER PRIMARY KEY\n"
            "+);\n"
        )
        result = classify_doc_change(diff)
        assert result["classification"] == "significant"
        assert any("schema" in r for r in result["reasons"])

    def test_alter_table_is_significant(self):
        """ALTER TABLE → significant."""
        from docs_updater import classify_doc_change
        diff = (
            "--- a/schema.sql\n"
            "+++ b/schema.sql\n"
            "+ALTER TABLE users ADD COLUMN email TEXT;\n"
        )
        result = classify_doc_change(diff)
        assert result["classification"] == "significant"
        assert any("schema" in r for r in result["reasons"])

    def test_large_change_is_significant(self):
        """Large change (>50 lines) → significant."""
        from docs_updater import classify_doc_change
        lines = ["+" + f"line {i}\n" for i in range(60)]
        diff = "--- a/file.py\n+++ b/file.py\n" + "".join(lines)
        result = classify_doc_change(diff)
        assert result["classification"] == "significant"
        assert any("large change" in r for r in result["reasons"])
        assert result["lines_changed"] == 60

    def test_mixed_trivial_and_significant_is_significant(self):
        """Mixed trivial + significant signals → significant (safety default)."""
        from docs_updater import classify_doc_change
        diff = (
            "--- a/file.py\n"
            "+++ b/file.py\n"
            "-# old comment\n"
            "+# new comment\n"
            "+def new_function():\n"
            "+    pass\n"
            "+class NewClass:\n"
            "+    pass\n"
            "+CREATE TABLE foo (id INT);\n"
        )
        result = classify_doc_change(diff)
        assert result["classification"] == "significant"
        assert result["significant_signals"] > 0

    def test_small_change_counts_as_trivial_signal(self):
        """Changes <=5 lines get 'small change' trivial signal."""
        from docs_updater import classify_doc_change
        diff = (
            "--- a/file.txt\n"
            "+++ b/file.txt\n"
            "-old line\n"
            "+new line\n"
        )
        result = classify_doc_change(diff)
        assert any("small change" in r for r in result["reasons"])
        assert result["lines_changed"] == 2

    def test_confidence_increases_with_signals(self):
        """More signals → higher confidence."""
        from docs_updater import classify_doc_change
        # Single signal
        diff1 = (
            "--- a/file.py\n"
            "+++ b/file.py\n"
            "-# comment\n"
            "+# updated comment\n"
        )
        result1 = classify_doc_change(diff1)

        # Multiple trivial signals (small + comment + whitespace + typo-like)
        diff2 = (
            "--- a/file.py\n"
            "+++ b/file.py\n"
            "-# old commnet\n"
            "+# old comment\n"
            "-  \n"
            "+\n"
        )
        result2 = classify_doc_change(diff2)

        # Both should be trivial, but the one with more signals should have >= confidence
        assert result1["classification"] == "trivial"
        assert result2["classification"] == "trivial"
        assert result2["confidence"] >= result1["confidence"]

    def test_require_change_is_trivial(self):
        """require() import change → trivial."""
        from docs_updater import classify_doc_change
        diff = (
            "--- a/file.js\n"
            "+++ b/file.js\n"
            "-const x = require('old-module')\n"
            "+const x = require('new-module')\n"
        )
        result = classify_doc_change(diff)
        assert result["classification"] == "trivial"
        assert any("import" in r for r in result["reasons"])

    def test_export_change_is_significant(self):
        """Export API change → significant."""
        from docs_updater import classify_doc_change
        diff = (
            "--- a/file.js\n"
            "+++ b/file.js\n"
            "+export default function newApi() {\n"
            "+  return true;\n"
            "+}\n"
            "+export const CONSTANT = 42;\n"
        )
        result = classify_doc_change(diff)
        assert result["classification"] == "significant"
        assert any("API" in r for r in result["reasons"])

    def test_whitespace_only_diff_is_trivial(self):
        """Pure whitespace diff text → trivial."""
        from docs_updater import classify_doc_change
        result = classify_doc_change("   \n  \n  ")
        assert result["classification"] == "trivial"

    def test_result_shape(self):
        """Verify all expected keys are in the result."""
        from docs_updater import classify_doc_change
        result = classify_doc_change("+some change\n-old line\n")
        assert "classification" in result
        assert "confidence" in result
        assert "reasons" in result
        assert "lines_changed" in result
        assert "trivial_signals" in result
        assert "significant_signals" in result
        assert isinstance(result["reasons"], list)
        assert isinstance(result["confidence"], float)

    def test_destructive_change_is_significant(self):
        """Destructive operations (DROP/DELETE/REMOVE) → significant."""
        from docs_updater import classify_doc_change
        diff = (
            "--- a/schema.sql\n"
            "+++ b/schema.sql\n"
            "+DROP TABLE old_table;\n"
            "+DELETE FROM configs WHERE obsolete = 1;\n"
        )
        result = classify_doc_change(diff)
        assert result["classification"] == "significant"
        assert any("destructive" in r for r in result["reasons"])
