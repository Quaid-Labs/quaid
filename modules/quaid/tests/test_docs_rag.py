"""Tests for docs_rag.py — chunking, indexing, search, stats."""

import os
import sys
import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock

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
