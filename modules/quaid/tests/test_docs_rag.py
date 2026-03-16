"""Tests for docs_rag.py — chunking, indexing, search, stats."""

import os
import sys
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, call

# Ensure plugin root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Must set env BEFORE imports
os.environ["MEMORY_DB_PATH"] = ":memory:"

import pytest

from datastore.docsdb.rag import DocsRAG


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_rag(tmp_path):
    """Create a DocsRAG backed by a temp DB."""
    db_path = tmp_path / "test_rag.db"
    return DocsRAG(db_path=db_path)


# ---------------------------------------------------------------------------
# chunk_markdown
# ---------------------------------------------------------------------------

class TestChunkMarkdown:
    """Tests for DocsRAG.chunk_markdown()."""

    def test_splits_at_headers(self, tmp_path):
        rag = _make_rag(tmp_path)
        content = """# Section One
First paragraph content here.

# Section Two
Second paragraph content here.

# Section Three
Third paragraph content here.
"""
        chunks = rag.chunk_markdown(content)
        assert len(chunks) >= 3  # At least one chunk per header

    def test_empty_content(self, tmp_path):
        rag = _make_rag(tmp_path)
        chunks = rag.chunk_markdown("")
        assert chunks == []

    def test_whitespace_only_content(self, tmp_path):
        rag = _make_rag(tmp_path)
        chunks = rag.chunk_markdown("   \n\n   ")
        assert chunks == []

    def test_no_headers(self, tmp_path):
        """Content without headers becomes one chunk."""
        rag = _make_rag(tmp_path)
        content = "Just some text without any markdown headers."
        chunks = rag.chunk_markdown(content)
        assert len(chunks) == 1
        assert "text without any markdown" in chunks[0]

    def test_respects_max_tokens(self, tmp_path):
        """Large content gets split when exceeding max_tokens."""
        rag = _make_rag(tmp_path)
        # Create content larger than a small max_tokens
        long_paragraph = "word " * 500  # ~500 words, ~125 tokens
        content = f"# Header\n{long_paragraph}"
        chunks = rag.chunk_markdown(content, max_tokens=50)
        # Should be split into multiple chunks
        assert len(chunks) >= 2

    def test_preserves_header_in_chunk(self, tmp_path):
        rag = _make_rag(tmp_path)
        content = "# My Header\nSome content below the header."
        chunks = rag.chunk_markdown(content)
        assert len(chunks) >= 1
        assert "# My Header" in chunks[0]

    def test_h2_and_h3_headers(self, tmp_path):
        rag = _make_rag(tmp_path)
        content = """## Section A
Content A.

### Subsection B
Content B.
"""
        chunks = rag.chunk_markdown(content)
        assert len(chunks) >= 2

    def test_returns_strings_not_chunk_objects(self, tmp_path):
        """chunk_markdown returns raw strings, not DocumentChunk objects."""
        rag = _make_rag(tmp_path)
        content = "# Test\nSome content."
        chunks = rag.chunk_markdown(content)
        for chunk in chunks:
            assert isinstance(chunk, str)


# ---------------------------------------------------------------------------
# needs_reindex
# ---------------------------------------------------------------------------

class TestNeedsReindex:
    """Tests for DocsRAG.needs_reindex()."""

    def test_unindexed_file_returns_true(self, tmp_path):
        rag = _make_rag(tmp_path)
        test_file = tmp_path / "test.md"
        test_file.write_text("# Test\nContent.")
        assert rag.needs_reindex(str(test_file)) is True

    def test_nonexistent_file_returns_true(self, tmp_path):
        rag = _make_rag(tmp_path)
        # needs_reindex returns True on error (reindex when in doubt)
        result = rag.needs_reindex("/nonexistent/path.md")
        assert result is True


# ---------------------------------------------------------------------------
# scan_docs_directory
# ---------------------------------------------------------------------------

class TestScanDocsDirectory:
    def test_includes_project_log_and_markdown(self, tmp_path):
        rag = _make_rag(tmp_path)
        docs = tmp_path / "projects" / "demo"
        docs.mkdir(parents=True, exist_ok=True)
        (docs / "PROJECT.md").write_text("# Demo\n")
        (docs / "PROJECT.log").write_text("- [2026-01-01T00:00:00] entry\n")
        (docs / "ignore.txt").write_text("nope")

        out = rag.scan_docs_directory(str(tmp_path))
        assert str((docs / "PROJECT.md").absolute()) in out
        assert str((docs / "PROJECT.log").absolute()) in out
        assert str((docs / "ignore.txt").absolute()) not in out


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

class TestStats:
    """Tests for DocsRAG.stats()."""

    def test_empty_db_stats(self, tmp_path):
        rag = _make_rag(tmp_path)
        s = rag.stats()
        assert s["total_chunks"] == 0
        assert s["total_files"] == 0
        assert s["last_indexed"] is None

    def test_stats_returns_dict(self, tmp_path):
        rag = _make_rag(tmp_path)
        s = rag.stats()
        assert isinstance(s, dict)
        assert "total_chunks" in s
        assert "total_files" in s
        assert "last_indexed" in s
        assert "by_category" in s


# ---------------------------------------------------------------------------
# estimate_tokens
# ---------------------------------------------------------------------------

class TestRagEstimateTokens:
    """Tests for DocsRAG.estimate_tokens()."""

    def test_basic_estimate(self, tmp_path):
        rag = _make_rag(tmp_path)
        assert rag.estimate_tokens("hello world") == 2  # 11 chars // 4

    def test_empty_string(self, tmp_path):
        rag = _make_rag(tmp_path)
        assert rag.estimate_tokens("") == 0

    def test_long_text(self, tmp_path):
        rag = _make_rag(tmp_path)
        text = "a" * 400
        assert rag.estimate_tokens(text) == 100


# ---------------------------------------------------------------------------
# docs filtering + search behavior
# ---------------------------------------------------------------------------

class TestDocsSearchFiltering:
    """Tests for doc filters and SQL-level search filtering."""

    def test_normalize_docs_filter_trims_dedupes_and_caps(self, tmp_path):
        rag = _make_rag(tmp_path)
        raw = ["  alpha.md ", "beta.md", "alpha.md", "", "   ", None]
        normalized = rag._normalize_docs_filter(raw)
        assert normalized == ["alpha.md", "beta.md"]

    @patch("datastore.docsdb.rag._lib_get_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_unpack_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_cosine_similarity", return_value=0.95)
    def test_search_docs_filters_by_docs_arg(self, _sim, _unpack, _embed, tmp_path):
        rag = _make_rag(tmp_path)
        db = sqlite3.connect(rag.db_path)
        try:
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                ("a:0", "/tmp/docs/alpha.md", 0, "alpha content", "# Alpha", b"e"),
            )
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                ("b:0", "/tmp/docs/beta.md", 0, "beta content", "# Beta", b"e"),
            )
            db.commit()
        finally:
            db.close()

        results = rag.search_docs("alpha", limit=10, docs=["alpha.md"])
        assert len(results) == 1
        assert results[0]["source"].endswith("alpha.md")

    @patch("datastore.docsdb.rag._lib_get_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_unpack_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_cosine_similarity", return_value=0.95)
    def test_search_docs_filters_by_project_and_docs(self, _sim, _unpack, _embed, tmp_path):
        rag = _make_rag(tmp_path)
        db = sqlite3.connect(rag.db_path)
        try:
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                ("p:0", "/tmp/workspace/projects/quaid/reference/memory.md", 0, "quaid docs", "# Q", b"e"),
            )
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                ("o:0", "/tmp/workspace/projects/other/reference/memory.md", 0, "other docs", "# O", b"e"),
            )
            db.commit()
        finally:
            db.close()

        with patch.object(
            rag,
            "_get_project_paths",
            return_value={
                "home_dir": "/tmp/workspace/projects/quaid",
                "source_roots": [],
            },
        ):
            results = rag.search_docs("memory", limit=10, project="quaid", docs=["memory.md"])

        assert len(results) == 1
        assert "/projects/quaid/" in results[0]["source"]

    @patch("datastore.docsdb.rag._lib_get_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_unpack_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_cosine_similarity", return_value=0.95)
    def test_search_docs_docs_filter_escapes_like_wildcards(self, _sim, _unpack, _embed, tmp_path):
        rag = _make_rag(tmp_path)
        db = sqlite3.connect(rag.db_path)
        try:
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                ("lit:0", "/tmp/docs/report_100%_complete.md", 0, "literal", "# Lit", b"e"),
            )
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                ("other:0", "/tmp/docs/report_100X_complete.md", 0, "wildcard-ish", "# O", b"e"),
            )
            db.commit()
        finally:
            db.close()

        results = rag.search_docs("report", limit=10, docs=["report_100%_complete.md"])
        assert len(results) == 1
        assert results[0]["source"].endswith("report_100%_complete.md")

    @patch("datastore.docsdb.rag.is_fail_hard_enabled", return_value=True)
    @patch("datastore.docsdb.rag._lib_get_embedding", return_value=None)
    def test_search_docs_embedding_failure_raises_when_failhard_enabled(self, _embed, _failhard, tmp_path):
        rag = _make_rag(tmp_path)
        with pytest.raises(RuntimeError, match="failHard is enabled"):
            rag.search_docs("memory", limit=5)

    def test_search_docs_bundle_infers_project_and_attaches_project_md(self, tmp_path):
        rag = _make_rag(tmp_path)
        chunks = [
            {
                "content": "Error middleware uses AppError",
                "source": "/tmp/workspace/projects/recipe-app/docs/api.md",
                "section_header": "## Errors",
                "similarity": 0.91,
                "chunk_index": 0,
                "project": "recipe-app",
            }
        ]

        with patch.object(rag, "search_docs", return_value=chunks), \
             patch.object(rag, "infer_project_from_chunks", return_value="recipe-app"), \
             patch.object(rag, "load_project_md", return_value="# Project: Recipe App\n"):
            bundle = rag.search_docs_bundle("error middleware", project=None)

        assert bundle["project"] == "recipe-app"
        assert bundle["project_md"] == "# Project: Recipe App\n"
        assert bundle["chunks"] == chunks

    def test_search_docs_bundle_includes_telemetry_when_enabled(self, tmp_path, monkeypatch):
        rag = _make_rag(tmp_path)
        monkeypatch.setenv("QUAID_RECALL_TELEMETRY", "1")
        chunks = [
            {
                "content": "Error middleware uses AppError",
                "source": "/tmp/workspace/projects/recipe-app/docs/api.md",
                "section_header": "## Errors",
                "similarity": 0.91,
                "chunk_index": 0,
                "project": "recipe-app",
            }
        ]

        with patch.object(rag, "search_docs", return_value=chunks), \
             patch.object(rag, "infer_project_from_chunks", return_value="recipe-app"), \
             patch.object(rag, "load_project_md", return_value="# Project: Recipe App\n"):
            bundle = rag.search_docs_bundle("error middleware", project=None, docs=["api.md"])

        assert bundle["telemetry"]["requested_project"] is None
        assert bundle["telemetry"]["resolved_project"] == "recipe-app"
        assert bundle["telemetry"]["chunk_count"] == 1
        assert bundle["telemetry"]["requested_docs"] == ["api.md"]

    def test_infer_project_from_chunks_prefers_highest_similarity_sum(self, tmp_path):
        rag = _make_rag(tmp_path)
        chunks = [
            {"source": "/tmp/a.md", "similarity": 0.40, "project": "alpha"},
            {"source": "/tmp/b.md", "similarity": 0.39, "project": "alpha"},
            {"source": "/tmp/c.md", "similarity": 0.75, "project": "beta"},
        ]

        project = rag.infer_project_from_chunks(chunks)

        assert project == "alpha"


# ---------------------------------------------------------------------------
# _run_rag_maintenance third pass — doc_registry enumeration
# ---------------------------------------------------------------------------
#
# The third pass (rag.py lines 623-648) iterates DocsRegistry().list_docs()
# and conditionally indexes files registered outside the workspace.
#
# Mock setup requirements:
#   - cfg.projects.enabled = False  → skips the first two passes entirely
#   - DocsRAG.reindex_all patched   → returns empty counters (pass 1 stub)
#   - datastore.docsdb.registry.DocsRegistry patched → controls list_docs() output
#   - DocsRAG.needs_reindex patched → controls up-to-date vs. stale decision
#   - DocsRAG.index_document patched → controls indexing side-effects
#
# ---------------------------------------------------------------------------

def _make_rag_ctx(tmp_path):
    """Build a minimal _run_rag_maintenance ctx with projects disabled."""
    cfg = SimpleNamespace(
        rag=SimpleNamespace(docs_dir="docs"),
        projects=SimpleNamespace(enabled=False, definitions={}),
    )
    return SimpleNamespace(cfg=cfg, dry_run=False, workspace=tmp_path)


class _Result:
    def __init__(self):
        self.metrics = {}
        self.logs = []
        self.errors = []
        self.data = {}


def _empty_reindex_result():
    return {"total_files": 0, "indexed_files": 0, "skipped_files": 0, "total_chunks": 0}


class TestRagMaintenanceThirdPass:
    """Third pass: doc_registry enumeration inside _run_rag_maintenance."""

    def _register_and_get_handler(self):
        from datastore.docsdb.rag import register_lifecycle_routines
        class _Reg:
            def __init__(self):
                self.handlers = {}
            def register(self, name, handler):
                self.handlers[name] = handler
        reg = _Reg()
        register_lifecycle_routines(reg, _Result)
        return reg.handlers["rag"]

    def test_external_doc_outside_workspace_gets_indexed(self, tmp_path):
        """A registered doc whose path is outside workspace (absolute) is indexed."""
        handler = self._register_and_get_handler()
        ctx = _make_rag_ctx(tmp_path)

        # Create a real file outside the workspace
        external_dir = tmp_path / "external"
        external_dir.mkdir()
        external_file = external_dir / "outside.md"
        external_file.write_text("# External\nContent.")

        fake_reg = MagicMock()
        fake_reg.list_docs.return_value = [{"file_path": str(external_file)}]

        with patch("datastore.docsdb.rag.DocsRAG.reindex_all", return_value=_empty_reindex_result()), \
             patch("datastore.docsdb.registry.DocsRegistry", return_value=fake_reg), \
             patch("datastore.docsdb.rag.DocsRAG.needs_reindex", return_value=True), \
             patch("datastore.docsdb.rag.DocsRAG.index_document", return_value=3) as mock_index:
            result = handler(ctx)

        mock_index.assert_called_once_with(str(external_file))
        assert result.metrics["rag_files_indexed"] >= 1
        assert result.metrics["rag_chunks_created"] >= 3

    def test_up_to_date_doc_is_skipped(self, tmp_path):
        """A registered doc that needs_reindex=False is counted as skipped, not indexed."""
        handler = self._register_and_get_handler()
        ctx = _make_rag_ctx(tmp_path)

        existing_file = tmp_path / "current.md"
        existing_file.write_text("# Up to date\nStill current.")

        fake_reg = MagicMock()
        fake_reg.list_docs.return_value = [{"file_path": str(existing_file)}]

        with patch("datastore.docsdb.rag.DocsRAG.reindex_all", return_value=_empty_reindex_result()), \
             patch("datastore.docsdb.registry.DocsRegistry", return_value=fake_reg), \
             patch("datastore.docsdb.rag.DocsRAG.needs_reindex", return_value=False), \
             patch("datastore.docsdb.rag.DocsRAG.index_document") as mock_index:
            result = handler(ctx)

        mock_index.assert_not_called()
        assert result.metrics["rag_files_skipped"] >= 1

    def test_nonexistent_path_is_silently_skipped(self, tmp_path):
        """A registered doc whose path does not exist on disk is silently skipped."""
        handler = self._register_and_get_handler()
        ctx = _make_rag_ctx(tmp_path)

        fake_reg = MagicMock()
        fake_reg.list_docs.return_value = [{"file_path": "/nonexistent/ghost.md"}]

        with patch("datastore.docsdb.rag.DocsRAG.reindex_all", return_value=_empty_reindex_result()), \
             patch("datastore.docsdb.registry.DocsRegistry", return_value=fake_reg), \
             patch("datastore.docsdb.rag.DocsRAG.index_document") as mock_index:
            result = handler(ctx)

        # No index attempt, no error raised, metrics counters stay at zero for this path
        mock_index.assert_not_called()
        assert result.errors == []

    def test_registered_project_source_file_inside_project_dir_gets_indexed(self, tmp_path):
        """Registry-managed source files under a scanned project dir still get indexed."""
        handler = self._register_and_get_handler()
        instance_root = tmp_path / "benchrunner"
        project_dir = instance_root / "projects" / "recipe-app" / "tests"
        project_dir.mkdir(parents=True, exist_ok=True)
        test_file = project_dir / "recipe.test.js"
        test_file.write_text("describe('recipe', () => {})")

        cfg = SimpleNamespace(
            rag=SimpleNamespace(docs_dir="docs"),
            projects=SimpleNamespace(
                enabled=True,
                definitions={
                    "recipe-app": SimpleNamespace(
                        auto_index=True,
                        home_dir="projects/recipe-app",
                        source_roots=["projects/recipe-app"],
                    )
                },
            ),
        )
        ctx = SimpleNamespace(cfg=cfg, dry_run=False, workspace=tmp_path)

        fake_reg = MagicMock()
        fake_reg.auto_discover.return_value = []
        fake_reg.sync_external_files.return_value = None
        fake_reg.list_docs.return_value = [{"file_path": "projects/recipe-app/tests/recipe.test.js"}]

        with patch("datastore.docsdb.rag.DocsRAG.reindex_all", return_value=_empty_reindex_result()), \
             patch("datastore.docsdb.rag._workspace", return_value=instance_root), \
             patch("datastore.docsdb.registry.DocsRegistry", return_value=fake_reg), \
             patch("datastore.docsdb.rag.DocsRAG.needs_reindex", return_value=True), \
             patch("datastore.docsdb.rag.DocsRAG.index_document", return_value=4) as mock_index:
            result = handler(ctx)

        mock_index.assert_called_once_with(str(test_file))
        assert result.metrics["rag_files_indexed"] >= 1
        assert result.metrics["rag_chunks_created"] >= 4

    def test_list_docs_exception_is_swallowed(self, tmp_path):
        """An exception from DocsRegistry().list_docs() is caught and logged as a warning."""
        handler = self._register_and_get_handler()
        ctx = _make_rag_ctx(tmp_path)

        fake_reg = MagicMock()
        fake_reg.list_docs.side_effect = Exception("db exploded")

        with patch("datastore.docsdb.rag.DocsRAG.reindex_all", return_value=_empty_reindex_result()), \
             patch("datastore.docsdb.registry.DocsRegistry", return_value=fake_reg):
            # Must not raise — exception is swallowed and logged as a warning
            result = handler(ctx)

        # No unhandled errors in result (the warning goes to logger, not result.errors)
        assert result.errors == []
