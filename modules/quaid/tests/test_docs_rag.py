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

    def test_splits_oversized_single_line_content(self, tmp_path):
        """Single-line source files still need to respect the chunk budget."""
        rag = _make_rag(tmp_path)
        long_line = '{"payload":"' + ("a" * 1600) + '"}'
        content = f"# Header\n{long_line}"

        chunks = rag.chunk_markdown(content, max_tokens=50)

        assert len(chunks) >= 2
        assert max(rag.estimate_tokens(chunk) for chunk in chunks) <= 55


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

    def test_needs_reindex_many_returns_true_when_doc_chunks_missing(self, tmp_path):
        rag = _make_rag(tmp_path)
        first = tmp_path / "first.md"
        second = tmp_path / "second.md"
        first.write_text("# First\nContent.")
        second.write_text("# Second\nContent.")

        with sqlite3.connect(rag.db_path) as conn:
            conn.execute("DROP TABLE doc_chunks")
            conn.commit()

        result = rag.needs_reindex_many([str(first), str(second)])

        assert result == {
            str(first): True,
            str(second): True,
        }


# ---------------------------------------------------------------------------
# index_document
# ---------------------------------------------------------------------------

class TestIndexDocument:
    """Tests for DocsRAG.index_document()."""

    def test_uses_parallel_embeddings_and_indexes_chunks(self, tmp_path):
        rag = _make_rag(tmp_path)
        test_file = tmp_path / "guide.md"
        test_file.write_text("# Guide\nBody.")

        chunk_texts = ["# Guide\nChunk A", "## Notes\nChunk B"]
        embeddings = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]

        with patch.object(rag, "chunk_markdown", return_value=chunk_texts), \
             patch("datastore.docsdb.rag._lib_get_embeddings", return_value=embeddings) as mock_get_embeddings:
            chunks = rag.index_document(str(test_file))

        assert chunks == 2
        mock_get_embeddings.assert_called_once_with(
            chunk_texts,
            pool_name="rag_embeddings",
            task_name="rag",
        )

        with sqlite3.connect(rag.db_path) as conn:
            rows = conn.execute(
                "SELECT source_file, chunk_index, content, section_header FROM doc_chunks ORDER BY chunk_index"
            ).fetchall()

        assert rows == [
            (str(test_file), 0, "# Guide\nChunk A", "# Guide"),
            (str(test_file), 1, "## Notes\nChunk B", "## Notes"),
        ]

    def test_preserves_existing_chunks_when_any_embedding_fails(self, tmp_path):
        rag = _make_rag(tmp_path)
        test_file = tmp_path / "guide.md"
        test_file.write_text("# Guide\nBody.")

        with sqlite3.connect(rag.db_path) as conn:
            conn.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                ("old:0", str(test_file), 0, "old chunk", "# Old", b"old"),
            )
            conn.commit()

        with patch.object(rag, "chunk_markdown", return_value=["# Guide\nChunk A", "## Notes\nChunk B"]), \
             patch("datastore.docsdb.rag._lib_get_embeddings", return_value=[[0.1, 0.2, 0.3], None]):
            chunks = rag.index_document(str(test_file))

        assert chunks == 0

        with sqlite3.connect(rag.db_path) as conn:
            rows = conn.execute(
                "SELECT id, content, section_header FROM doc_chunks WHERE source_file = ? ORDER BY chunk_index",
                (str(test_file),),
            ).fetchall()

        assert rows == [("old:0", "old chunk", "# Old")]

    def test_syncs_vec_doc_chunks_and_replaces_stale_vec_rows(self, tmp_path):
        from lib.database import get_connection, has_vec
        from lib.embeddings import pack_embedding

        if not has_vec():
            pytest.skip("sqlite-vec not available in this environment")

        rag = _make_rag(tmp_path)
        test_file = tmp_path / "guide.md"
        test_file.write_text("# Guide\nBody.")

        old_embedding = pack_embedding([0.9, 0.1, 0.0])
        with get_connection(rag.db_path) as conn:
            rag._ensure_doc_vec_table(conn, [0.9, 0.1, 0.0])
            conn.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                ("old:0", str(test_file), 0, "old chunk", "# Old", old_embedding),
            )
            conn.execute(
                "INSERT INTO vec_doc_chunks(chunk_id, embedding) VALUES (?, ?)",
                ("old:0", old_embedding),
            )

        chunk_texts = ["# Guide\nChunk A", "## Notes\nChunk B"]
        embeddings = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]

        with patch.object(rag, "chunk_markdown", return_value=chunk_texts), \
             patch("datastore.docsdb.rag._lib_get_embeddings", return_value=embeddings):
            chunks = rag.index_document(str(test_file))

        assert chunks == 2

        with get_connection(rag.db_path) as conn:
            doc_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM doc_chunks WHERE source_file = ? ORDER BY chunk_index",
                    (str(test_file),),
                ).fetchall()
            ]
            vec_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT chunk_id FROM vec_doc_chunks ORDER BY chunk_id"
                ).fetchall()
            ]

        assert doc_ids == [f"{test_file}:0", f"{test_file}:1"]
        assert vec_ids == doc_ids


class TestReindexAll:
    """Tests for DocsRAG.reindex_all()."""

    def test_batches_reindex_checks_and_skips_fresh_files(self, tmp_path):
        rag = _make_rag(tmp_path)
        docs = tmp_path / "docs"
        docs.mkdir()

        fresh_file = docs / "fresh.md"
        fresh_file.write_text("# Fresh\nStill current.")
        stale_file = docs / "stale.md"
        stale_file.write_text("# Stale\nNeeds reindex.")

        with sqlite3.connect(rag.db_path) as conn:
            conn.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("fresh:0", str(fresh_file), 0, "fresh chunk", "# Fresh", b"emb", "2100-01-01T00:00:00+00:00"),
            )
            conn.commit()

        with patch.object(rag, "needs_reindex", side_effect=AssertionError("reindex_all should use batched lookups")), \
             patch.object(rag, "index_document", return_value=3) as mock_index:
            result = rag.reindex_all(str(docs), force=False)

        mock_index.assert_called_once_with(str(stale_file))
        assert result == {
            "total_files": 2,
            "indexed_files": 1,
            "skipped_files": 1,
            "total_chunks": 3,
        }


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
    def test_search_docs_filters_by_project_for_relative_project_paths(self, _sim, _unpack, _embed, tmp_path):
        rag = _make_rag(tmp_path)
        db = sqlite3.connect(rag.db_path)
        try:
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                ("rel:0", "projects/cross-live-test/beacon-maintenance.md", 0, "north pier beacon is offline", "# Beacon", b"e"),
            )
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                ("other:0", "projects/other-test/beacon-maintenance.md", 0, "other beacon", "# Beacon", b"e"),
            )
            db.commit()
        finally:
            db.close()

        with patch.object(
            rag,
            "_get_project_paths",
            return_value={
                "home_dir": str(tmp_path / "codex-livetest" / "projects" / "cross-live-test"),
                "source_roots": [],
            },
        ):
            results = rag.search_docs("north pier beacon", limit=10, project="cross-live-test")

        assert len(results) == 1
        assert results[0]["source"] == "projects/cross-live-test/beacon-maintenance.md"

    @patch("datastore.docsdb.rag._lib_get_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_unpack_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_cosine_similarity", return_value=0.70)
    def test_search_docs_skips_context_files_even_if_indexed(self, _sim, _unpack, _embed, tmp_path):
        rag = _make_rag(tmp_path)
        db = sqlite3.connect(rag.db_path)
        try:
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                ("tools:0", "/tmp/workspace/projects/recipe-app/TOOLS.md", 0, "tool reference", "# Tools", b"e"),
            )
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                ("readme:0", "/tmp/workspace/projects/recipe-app/README.md", 0, "real docs", "# Readme", b"e"),
            )
            db.commit()
        finally:
            db.close()

        results = rag.search_docs("recipe app tools", limit=10)
        assert len(results) == 1
        assert results[0]["source"].endswith("README.md")

    @patch("datastore.docsdb.rag._lib_get_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_unpack_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_cosine_similarity", return_value=0.70)
    def test_search_docs_reranks_specific_impl_files_above_overview_docs(self, _sim, _unpack, _embed, tmp_path):
        rag = _make_rag(tmp_path)
        db = sqlite3.connect(rag.db_path)
        try:
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                ("project:0", "/tmp/workspace/projects/recipe-app/PROJECT.md", 0, "overview", "# Project: Recipe App", b"e"),
            )
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                ("test:0", "/tmp/workspace/projects/recipe-app/tests/graphql.test.js", 0, "describe('graphql tests')", "# GraphQL Tests", b"e"),
            )
            db.commit()
        finally:
            db.close()

        with patch.object(
            rag,
            "_get_project_paths",
            return_value={
                "home_dir": "/tmp/workspace/projects/recipe-app",
                "source_roots": ["/tmp/workspace/projects/recipe-app"],
            },
        ):
            results = rag.search_docs("recipe app tests testing", limit=10, project="recipe-app")

        assert len(results) == 2
        assert results[0]["source"].endswith("tests/graphql.test.js")
        assert results[0]["similarity"] > results[1]["similarity"]

    @patch("datastore.docsdb.rag._lib_get_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_unpack_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_cosine_similarity", return_value=0.80)
    def test_search_docs_penalizes_fixture_files_for_impl_queries(self, _sim, _unpack, _embed, tmp_path):
        rag = _make_rag(tmp_path)
        db = sqlite3.connect(rag.db_path)
        try:
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "seed:0",
                    "/tmp/workspace/projects/recipe-app/seeds/sample-recipes.json",
                    0,
                    "recipe tests mention ingredients and setup",
                    "# Seed Recipes",
                    b"e",
                ),
            )
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "server:0",
                    "/tmp/workspace/projects/recipe-app/server.js",
                    0,
                    "test suites include dietary.test.js and sharing.test.js",
                    "# Tests",
                    b"e",
                ),
            )
            db.commit()
        finally:
            db.close()

        with patch.object(
            rag,
            "_get_project_paths",
            return_value={
                "home_dir": "/tmp/workspace/projects/recipe-app",
                "source_roots": ["/tmp/workspace/projects/recipe-app"],
            },
        ):
            results = rag.search_docs("what test suites exist for the recipe app", limit=10, project="recipe-app")

        assert len(results) == 2
        assert results[0]["source"].endswith("server.js")
        assert results[0]["similarity"] > results[1]["similarity"]

    @patch("datastore.docsdb.rag._lib_get_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_unpack_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_cosine_similarity", return_value=0.80)
    def test_search_docs_keeps_seed_files_when_query_explicitly_asks_for_seeds(self, _sim, _unpack, _embed, tmp_path):
        rag = _make_rag(tmp_path)
        db = sqlite3.connect(rag.db_path)
        try:
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "seed:0",
                    "/tmp/workspace/projects/recipe-app/seeds/sample-recipes.json",
                    0,
                    "safe recipes include grilled salmon and lentil soup",
                    "# Seed Recipes",
                    b"e",
                ),
            )
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "project:0",
                    "/tmp/workspace/projects/recipe-app/PROJECT.md",
                    0,
                    "overview of project",
                    "# Project: Recipe App",
                    b"e",
                ),
            )
            db.commit()
        finally:
            db.close()

        with patch.object(
            rag,
            "_get_project_paths",
            return_value={
                "home_dir": "/tmp/workspace/projects/recipe-app",
                "source_roots": ["/tmp/workspace/projects/recipe-app"],
            },
        ):
            results = rag.search_docs("what seed recipes are safe for mom", limit=10, project="recipe-app")

        assert len(results) == 2
        assert results[0]["source"].endswith("sample-recipes.json")

    @patch("datastore.docsdb.rag._lib_get_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_unpack_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_cosine_similarity", return_value=0.95)
    def test_search_docs_matches_relocated_project_paths_by_suffix(self, _sim, _unpack, _embed, tmp_path):
        rag = _make_rag(tmp_path)
        db = sqlite3.connect(rag.db_path)
        try:
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "relocated:0",
                    "/tmp/old-run/benchrunner/projects/recipe-app/src/middleware/errorHandler.js",
                    0,
                    "error middleware uses AppError",
                    "# Error Handler",
                    b"e",
                ),
            )
            db.commit()
        finally:
            db.close()

        current_workspace = tmp_path / "new-run" / "benchrunner"
        current_workspace.mkdir(parents=True, exist_ok=True)

        with patch("datastore.docsdb.rag._workspace", return_value=current_workspace), \
             patch.object(
                 rag,
                 "_get_project_paths",
                 return_value={
                     "home_dir": str(current_workspace / "projects" / "recipe-app"),
                     "source_roots": [str(current_workspace / "projects" / "recipe-app")],
                 },
             ):
            results = rag.search_docs("error handling", limit=10, project="recipe-app")

        assert len(results) == 1
        assert results[0]["source"].endswith("/projects/recipe-app/src/middleware/errorHandler.js")

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

    def test_search_docs_uses_vec_doc_chunks_when_available(self, tmp_path):
        from lib.database import get_connection, has_vec
        from lib.embeddings import pack_embedding

        if not has_vec():
            pytest.skip("sqlite-vec not available in this environment")

        rag = _make_rag(tmp_path)
        with get_connection(rag.db_path) as conn:
            rag._ensure_doc_vec_table(conn, [1.0, 0.0, 0.0])
            rows = [
                ("alpha:0", "/tmp/docs/alpha.md", 0, "alpha content", "# Alpha", pack_embedding([1.0, 0.0, 0.0])),
                ("beta:0", "/tmp/docs/beta.md", 0, "beta content", "# Beta", pack_embedding([0.0, 1.0, 0.0])),
            ]
            conn.executemany(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.executemany(
                "INSERT INTO vec_doc_chunks(chunk_id, embedding) VALUES (?, ?)",
                [(row[0], row[5]) for row in rows],
            )

        with patch("datastore.docsdb.rag._lib_get_embedding", return_value=[1.0, 0.0, 0.0]), \
             patch("datastore.docsdb.rag._lib_unpack_embedding", side_effect=AssertionError("scan path should not run")):
            results = rag.search_docs("alpha", limit=2)

        assert len(results) == 1
        assert results[0]["source"].endswith("alpha.md")

    def test_search_docs_vec_failure_raises_when_failhard_enabled(self, tmp_path):
        rag = _make_rag(tmp_path)

        class _BoomConn:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, sql, params=()):
                raise sqlite3.OperationalError("vec query exploded")

        with patch("datastore.docsdb.rag._lib_get_connection", return_value=_BoomConn()), \
             patch("datastore.docsdb.rag._lib_get_embedding", return_value=[1.0, 0.0, 0.0]), \
             patch("datastore.docsdb.rag._lib_has_vec", return_value=True), \
             patch.object(rag, "_doc_vec_table_exists", return_value=True), \
             patch("datastore.docsdb.rag.is_fail_hard_enabled", return_value=True):
            with pytest.raises(RuntimeError, match="failHard is enabled"):
                rag.search_docs("alpha")

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
             patch("datastore.docsdb.rag.DocsRAG.needs_reindex_many", return_value={str(external_file): True}), \
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
             patch("datastore.docsdb.rag.DocsRAG.needs_reindex_many", return_value={str(existing_file): False}), \
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
             patch("datastore.docsdb.rag.DocsRAG.needs_reindex_many", return_value={str(test_file): True}), \
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
