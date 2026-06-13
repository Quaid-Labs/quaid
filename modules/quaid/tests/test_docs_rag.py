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


@pytest.mark.parametrize("bad_date", ["2023-13-01", "2023-99-99"])
def test_docs_recall_date_bound_rejects_invalid_calendar_dates(bad_date):
    from datastore.docsdb.rag import _normalize_date_bound

    with pytest.raises(ValueError, match="valid YYYY-MM-DD"):
        _normalize_date_bound(bad_date)


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

    def test_chunk_max_tokens_rejects_non_positive_failhard(self, monkeypatch):
        import datastore.docsdb.rag as rag_mod

        monkeypatch.setattr(rag_mod, "_rag_config", lambda: SimpleNamespace(chunk_max_tokens=0))
        monkeypatch.setattr(rag_mod, "is_fail_hard_enabled", lambda: True)

        with pytest.raises(RuntimeError, match="chunk_max_tokens"):
            rag_mod._chunk_max_tokens()

    def test_chunk_max_tokens_non_positive_falls_back_when_fail_open(self, monkeypatch, caplog):
        import datastore.docsdb.rag as rag_mod

        monkeypatch.setattr(rag_mod, "_rag_config", lambda: SimpleNamespace(chunk_max_tokens=-10))
        monkeypatch.setattr(rag_mod, "is_fail_hard_enabled", lambda: False)

        caplog.set_level("WARNING")
        assert rag_mod._chunk_max_tokens() == 800
        assert "Non-positive docs RAG chunk_max_tokens" in caplog.text


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
            timeout_s=60.0,
        )

        with sqlite3.connect(rag.db_path) as conn:
            rows = conn.execute(
                "SELECT source_file, chunk_index, content, section_header FROM doc_chunks ORDER BY chunk_index"
            ).fetchall()

        assert rows == [
            (str(test_file), 0, "# Guide\nChunk A", "# Guide"),
            (str(test_file), 1, "## Notes\nChunk B", "## Notes"),
        ]

    def test_index_document_chunks_project_log_into_focused_day_slices(self, tmp_path):
        rag = _make_rag(tmp_path)
        project_dir = tmp_path / "projects" / "recipe-app"
        project_dir.mkdir(parents=True, exist_ok=True)
        test_file = project_dir / "PROJECT.log"
        test_file.write_text(
            "\n".join(
                [
                    "# Project Log",
                    "- [2026-03-22T23:59:59] Authorization gaps documented in updateRecipe and deleteRecipe mutations",
                    "- [2026-03-22T23:59:59] Implemented resolvers.js with queries and mutations",
                    "- [2026-03-22T23:59:59] Known N+1 bug in Recipe.ingredientList field resolver",
                    "- [2026-03-22T23:59:59] Added recipe_shares table with unique code constraint",
                    "- [2026-03-22T23:59:59] Added owner_id column to recipes table",
                    "- [2026-03-22T23:59:59] Implemented share endpoints for public recipe links",
                    "- [2026-03-22T23:59:59] Added /health endpoint for Docker healthcheck",
                    "- [2026-03-22T23:59:59] Added Dockerfile with Node 18 Alpine, production deps only",
                    "- [2026-03-22T23:59:59] Added docker-compose.yml with SQLite volume mount and healthcheck",
                    "- [2026-03-22T23:59:59] Added Makefile with docker-up and docker-down targets",
                    "- [2026-03-22T23:59:59] Created docs/api.md with deployment and API docs",
                    "- [2026-03-23T23:59:59] Added follow-up release checklist",
                ]
            ),
            encoding="utf-8",
        )

        def _embed_many(chunks, **_kwargs):
            out = []
            for chunk in chunks:
                if "Dockerfile" in chunk or "docker-compose.yml" in chunk or "docker-up" in chunk:
                    out.append([0.95])
                else:
                    out.append([0.35])
            return out

        def _pack_embedding(embedding):
            return str(float(embedding[0])).encode("ascii")

        def _unpack_embedding(blob):
            return [float(bytes(blob).decode("ascii"))]

        def _similarity(_query_embedding, chunk_embedding):
            return float(chunk_embedding[0])

        with (
            patch.object(rag, "chunk_markdown", side_effect=AssertionError("PROJECT.log should use specialized chunking")),
            patch("datastore.docsdb.rag._lib_get_embeddings", side_effect=_embed_many),
            patch("datastore.docsdb.rag._lib_pack_embedding", side_effect=_pack_embedding),
            patch("datastore.docsdb.rag._lib_get_embedding", return_value=[1.0]),
            patch("datastore.docsdb.rag._lib_unpack_embedding", side_effect=_unpack_embedding),
            patch("datastore.docsdb.rag._lib_cosine_similarity", side_effect=_similarity),
            patch.object(
                rag,
                "_get_project_paths",
                return_value={
                    "home_dir": str(project_dir),
                    "source_roots": [],
                },
            ),
        ):
            chunk_count = rag.index_document(str(test_file))
            results = rag.search_docs(
                "As of 2026-03-22, how did the recipe app handle deployment?",
                limit=2,
                project="recipe-app",
                date_to="2026-03-22",
            )

        assert chunk_count >= 3
        assert len(results) == 2
        assert "Dockerfile" in results[0]["content"]
        assert "docker-compose.yml" in results[0]["content"]
        assert "2026-03-23" not in results[0]["content"]

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
             patch("datastore.docsdb.rag._lib_get_embeddings", return_value=[[0.1, 0.2, 0.3], None]), \
             patch("datastore.docsdb.rag.is_fail_hard_enabled", return_value=False):
            chunks = rag.index_document(str(test_file))

        assert chunks == 0

        with sqlite3.connect(rag.db_path) as conn:
            rows = conn.execute(
                "SELECT id, content, section_header FROM doc_chunks WHERE source_file = ? ORDER BY chunk_index",
                (str(test_file),),
            ).fetchall()

        assert rows == [("old:0", "old chunk", "# Old")]

    def test_preserves_existing_chunks_when_reindex_generates_no_chunks(self, tmp_path):
        rag = _make_rag(tmp_path)
        test_file = tmp_path / "guide.md"
        test_file.write_text("   \n\n   ", encoding="utf-8")

        with sqlite3.connect(rag.db_path) as conn:
            conn.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                ("old:0", str(test_file), 0, "old chunk", "# Old", b"old"),
            )
            conn.commit()

        assert rag.index_document(str(test_file)) == 0

        with sqlite3.connect(rag.db_path) as conn:
            rows = conn.execute(
                "SELECT id, content FROM doc_chunks WHERE source_file = ?",
                (str(test_file),),
            ).fetchall()

        assert rows == [("old:0", "old chunk")]

    def test_embedding_failure_raises_when_fail_hard(self, tmp_path):
        rag = _make_rag(tmp_path)
        test_file = tmp_path / "guide.md"
        test_file.write_text("# Guide\nBody.")

        with patch.object(rag, "chunk_markdown", return_value=["# Guide\nChunk A"]), \
             patch("datastore.docsdb.rag._lib_get_embeddings", return_value=[None]), \
             patch("datastore.docsdb.rag.is_fail_hard_enabled", return_value=True), \
             pytest.raises(RuntimeError, match="Failed embedding"):
            rag.index_document(str(test_file))

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

    def test_index_document_collapses_symlinked_source_alias_paths(self, tmp_path):
        rag = _make_rag(tmp_path)
        real_file = tmp_path / "projects" / "quaid" / "reference" / "memory-reference.md"
        real_file.parent.mkdir(parents=True, exist_ok=True)
        real_file.write_text("# Reference\nCanonical content.")

        alias_projects = tmp_path / "instances" / "benchrunner" / "projects"
        alias_projects.parent.mkdir(parents=True, exist_ok=True)
        alias_projects.symlink_to(tmp_path / "projects", target_is_directory=True)
        alias_file = alias_projects / "quaid" / "reference" / "memory-reference.md"
        assert alias_file.exists()
        assert alias_file.samefile(real_file)

        with patch.object(rag, "chunk_markdown", return_value=["# Reference\nCanonical content."]), \
             patch("datastore.docsdb.rag._lib_get_embeddings", return_value=[[0.1, 0.2, 0.3]]):
            assert rag.index_document(str(real_file)) == 1
            assert rag.index_document(str(alias_file)) == 1

        with sqlite3.connect(rag.db_path) as conn:
            rows = conn.execute(
                "SELECT source_file, COUNT(*) FROM doc_chunks GROUP BY source_file"
            ).fetchall()
            ids = [row[0] for row in conn.execute("SELECT id FROM doc_chunks").fetchall()]

        assert rows == [(str(real_file.resolve()), 1)]
        assert ids == [f"{real_file.resolve()}:0"]

    def test_remove_chunks_for_path_clears_alias_rows(self, tmp_path):
        rag = _make_rag(tmp_path)
        real_file = tmp_path / "projects" / "quaid" / "PROJECT.md"
        real_file.parent.mkdir(parents=True, exist_ok=True)
        real_file.write_text("# Project\n", encoding="utf-8")
        alias_projects = tmp_path / "instances" / "benchrunner" / "projects"
        alias_projects.parent.mkdir(parents=True, exist_ok=True)
        alias_projects.symlink_to(tmp_path / "projects", target_is_directory=True)
        alias_file = alias_projects / "quaid" / "PROJECT.md"

        with sqlite3.connect(rag.db_path) as conn:
            conn.executemany(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, embedding) VALUES (?, ?, ?, ?, ?)",
                [
                    ("real:0", str(real_file.resolve()), 0, "real", b"x"),
                    ("alias:0", str(alias_file), 0, "alias", b"y"),
                ],
            )
            conn.commit()

        removed = rag.remove_chunks_for_path(str(alias_file))
        assert removed == 2

        with sqlite3.connect(rag.db_path) as conn:
            remaining = conn.execute("SELECT id FROM doc_chunks ORDER BY id").fetchall()
        assert remaining == []


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

    def test_diversify_suite_results_preserves_rows_without_content_key(self):
        from datastore.docsdb.rag import _diversify_suite_results

        rows = [
            {"source": "/tmp/mixed-shape.md", "chunk_index": 0},
            {
                "source": "/tmp/projects/demo/PROJECT.log",
                "chunk_index": 1,
                "content": "Added tests/payment.test.js coverage",
            },
        ]

        out = _diversify_suite_results(rows, limit=2)

        assert out[0] is rows[0]
        assert rows[1] in out

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

    def test_search_docs_empty_query_raises_under_failhard(self, tmp_path):
        rag = _make_rag(tmp_path)

        with patch("datastore.docsdb.rag.is_fail_hard_enabled", return_value=True), \
             patch("datastore.docsdb.rag._lib_get_embedding") as embed:
            with pytest.raises(ValueError, match="must not be empty"):
                rag.search_docs("   ")

        embed.assert_not_called()

    def test_search_docs_row_scan_fallback_is_bounded(self, tmp_path):
        rag = _make_rag(tmp_path)
        with sqlite3.connect(rag.db_path) as db:
            db.executemany(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        f"doc:{i}",
                        f"/tmp/docs/doc{i}.md",
                        0,
                        f"content {i}",
                        None,
                        b"e",
                    )
                    for i in range(300)
                ],
            )
            db.commit()

        with patch("datastore.docsdb.rag._lib_has_vec", return_value=False), \
             patch("datastore.docsdb.rag._lib_get_embedding", return_value=[1.0]), \
             patch("datastore.docsdb.rag._lib_unpack_embedding", return_value=[1.0]) as unpack, \
             patch("datastore.docsdb.rag._lib_cosine_similarity", return_value=0.95):
            results = rag.search_docs("content", limit=1)

        assert len(results) == 1
        assert unpack.call_count == 256

    def test_project_source_fallback_read_failure_raises_under_failhard(self, tmp_path, monkeypatch):
        rag = _make_rag(tmp_path)
        project_dir = tmp_path / "projects" / "demo"
        project_dir.mkdir(parents=True, exist_ok=True)
        md_path = project_dir / "PROJECT.md"
        md_path.write_text("# Demo\n", encoding="utf-8")
        real_read_text = Path.read_text

        def _read_text(path, *args, **kwargs):
            if Path(path) == md_path:
                raise OSError("permission denied")
            return real_read_text(path, *args, **kwargs)

        monkeypatch.setattr(rag, "_get_project_paths", lambda _project: {"home_dir": str(project_dir), "source_roots": []})

        with patch("datastore.docsdb.rag.is_fail_hard_enabled", return_value=True), \
             patch.object(Path, "read_text", _read_text):
            with pytest.raises(RuntimeError, match="PROJECT.md fallback"):
                rag._project_source_fallback_chunks(
                    query="demo",
                    limit=1,
                    project="demo",
                    docs=None,
                    date_from=None,
                    date_to=None,
                )

    @patch("datastore.docsdb.rag._lib_get_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_unpack_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_cosine_similarity", return_value=0.95)
    def test_search_docs_filters_project_log_lines_by_date(self, _sim, _unpack, _embed, tmp_path):
        rag = _make_rag(tmp_path)
        db = sqlite3.connect(rag.db_path)
        try:
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "log:0",
                    "/tmp/workspace/projects/quaid/PROJECT.log",
                    0,
                    "\n".join(
                        [
                            "- [2026-03-05T23:59:59] Added legacy recall mode",
                            "- [2026-03-15T23:59:59] Switched recall planner to hybrid",
                        ]
                    ),
                    None,
                    b"e",
                ),
            )
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "project-md:0",
                    "/tmp/workspace/projects/quaid/PROJECT.md",
                    0,
                    "Current recall planner summary",
                    "# Project: Quaid",
                    b"e",
                ),
            )
            db.commit()
        finally:
            db.close()

        results = rag.search_docs(
            "recall planner",
            limit=10,
            docs=["PROJECT.log", "PROJECT.md"],
            date_from="2026-03-01",
            date_to="2026-03-10",
        )

        assert len(results) == 1
        assert results[0]["source"].endswith("PROJECT.log")
        assert "Added legacy recall mode" in results[0]["content"]
        assert "Switched recall planner to hybrid" not in results[0]["content"]
        assert "Current recall planner summary" not in results[0]["content"]

    @patch("datastore.docsdb.rag._lib_get_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_unpack_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_cosine_similarity", return_value=0.95)
    def test_search_docs_date_filtered_project_log_orders_latest_state_first(
        self,
        _sim,
        _unpack,
        _embed,
        tmp_path,
    ):
        rag = _make_rag(tmp_path)
        db = sqlite3.connect(rag.db_path)
        try:
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "portfolio-log:0",
                    "/tmp/workspace/projects/portfolio-site/PROJECT.log",
                    0,
                    "\n".join(
                        [
                            "- [2026-03-15T23:59:59] Current company listed: TechFlow",
                            "- [2026-04-21T23:59:59] Portfolio still lists TechFlow before Stripe start date",
                            "- [2026-04-28T23:59:59] Updated subtitle and About section: TechFlow -> Stripe",
                        ]
                    ),
                    None,
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
                "home_dir": "/tmp/workspace/projects/portfolio-site",
                "source_roots": [],
            },
        ):
            results = rag.search_docs(
                "As of 2026-04-28, what company was listed on Maya's portfolio site?",
                limit=1,
                project="portfolio-site",
                date_to="2026-04-28",
            )

        assert len(results) == 1
        lines = results[0]["content"].splitlines()
        assert "TechFlow -> Stripe" in lines[0]
        assert "still lists TechFlow" in lines[1]

    @patch("datastore.docsdb.rag._lib_get_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_unpack_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_cosine_similarity", return_value=0.95)
    def test_search_docs_date_filtered_project_log_orders_same_day_query_matches_first(
        self,
        _sim,
        _unpack,
        _embed,
        tmp_path,
    ):
        rag = _make_rag(tmp_path)
        db = sqlite3.connect(rag.db_path)
        try:
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "recipe-log:0",
                    "/tmp/workspace/projects/recipe-app/PROJECT.log",
                    0,
                    "\n".join(
                        [
                            "- [2026-03-18T23:59:59] Added meal planning schema: meal_plans and meal_plan_items",
                            "- [2026-03-18T23:59:59] Added grocery list endpoint: GET /api/meal-plans/:id/grocery-list",
                            "- [2026-03-18T23:59:59] Added tests/dietary.test.js covering DIETARY_LABELS and SAFE_FOR_MOM",
                            "- [2026-03-18T23:59:59] Added tests/mealplan.test.js covering meal plan CRUD",
                        ]
                    ),
                    None,
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
                "source_roots": [],
            },
        ):
            results = rag.search_docs(
                "As of 2026-03-18, what test suites existed for the recipe app?",
                limit=1,
                project="recipe-app",
                date_to="2026-03-18",
            )

        assert len(results) == 1
        lines = results[0]["content"].splitlines()
        assert "tests/dietary.test.js" in lines[0]
        assert "tests/mealplan.test.js" in lines[1]

    def test_search_docs_date_filtered_project_log_query_lines_outrank_semantic_noise(self, tmp_path):
        rag = _make_rag(tmp_path)
        db = sqlite3.connect(rag.db_path)
        try:
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "recipe-log:schema",
                    "/tmp/workspace/projects/recipe-app/PROJECT.log",
                    0,
                    "\n".join(
                        [
                            "- [2026-03-18T23:59:59] Added meal planning schema: meal_plans and meal_plan_items",
                            "- [2026-03-18T23:59:59] Added grocery list endpoint: GET /api/meal-plans/:id/grocery-list",
                        ]
                    ),
                    None,
                    b"0.95",
                ),
            )
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "recipe-log:tests",
                    "/tmp/workspace/projects/recipe-app/PROJECT.log",
                    1,
                    "\n".join(
                        [
                            "- [2026-03-18T23:59:59] Added tests/dietary.test.js covering DIETARY_LABELS and SAFE_FOR_MOM",
                            "- [2026-03-18T23:59:59] tests/mealplan.test.js: 368 lines covering meal plan CRUD",
                        ]
                    ),
                    None,
                    b"0.55",
                ),
            )
            db.commit()
        finally:
            db.close()

        def _unpack(blob):
            return [float(bytes(blob).decode("ascii"))]

        def _sim(_query_embedding, chunk_embedding):
            return float(chunk_embedding[0])

        with (
            patch("datastore.docsdb.rag._lib_get_embedding", return_value=[1.0]),
            patch("datastore.docsdb.rag._lib_unpack_embedding", side_effect=_unpack),
            patch("datastore.docsdb.rag._lib_cosine_similarity", side_effect=_sim),
            patch.object(
                rag,
                "_get_project_paths",
                return_value={
                    "home_dir": "/tmp/workspace/projects/recipe-app",
                    "source_roots": [],
                },
            ),
        ):
            results = rag.search_docs(
                "As of 2026-03-18, what test suites existed for the recipe app?",
                limit=2,
                project="recipe-app",
                date_to="2026-03-18",
            )

        assert len(results) == 2
        assert "tests/dietary.test.js" in results[0]["content"]
        assert "grocery list endpoint" in results[1]["content"]
        assert all(result["similarity"] <= 1.0 for result in results)

        with (
            patch("datastore.docsdb.rag._lib_get_embedding", return_value=[1.0]),
            patch("datastore.docsdb.rag._lib_unpack_embedding", side_effect=_unpack),
            patch("datastore.docsdb.rag._lib_cosine_similarity", side_effect=_sim),
            patch.object(
                rag,
                "_get_project_paths",
                return_value={
                    "home_dir": "/tmp/workspace/projects/recipe-app",
                    "source_roots": [],
                },
            ),
        ):
            since_results = rag.search_docs(
                "Since 2026-03-01, what test suites existed for the recipe app?",
                limit=2,
                project="recipe-app",
                date_from="2026-03-01",
            )

        assert len(since_results) == 2
        assert "tests/dietary.test.js" in since_results[0]["content"]
        assert "grocery list endpoint" in since_results[1]["content"]

    def test_search_docs_date_filtered_project_log_diversifies_test_suite_inventory(self, tmp_path):
        rag = _make_rag(tmp_path)
        db = sqlite3.connect(rag.db_path)
        try:
            rows = [
                (
                    "suite:sharing",
                    "/tmp/workspace/projects/recipe-app/PROJECT.log",
                    0,
                    "- [2026-05-08T23:59:59] Created sharing.test.js with 14 tests covering share code generation and idempotency",
                    None,
                    b"0.96",
                ),
                (
                    "suite:mealplan:1",
                    "/tmp/workspace/projects/recipe-app/PROJECT.log",
                    1,
                    "- [2026-04-12T23:59:59] Completed tests/mealplan.test.js with 368 lines covering meal plan CRUD and grocery list aggregation",
                    None,
                    b"0.95",
                ),
                (
                    "suite:mealplan:2",
                    "/tmp/workspace/projects/recipe-app/PROJECT.log",
                    2,
                    "- [2026-03-18T23:59:59] tests/mealplan.test.js includes five describe blocks for meal plan creation and validation",
                    None,
                    b"0.94",
                ),
                (
                    "suite:recipe",
                    "/tmp/workspace/projects/recipe-app/PROJECT.log",
                    3,
                    "- [2026-03-11T23:59:59] Added Jest test framework with tests/setup.js, tests/helpers.js, and tests/recipe.test.js for CRUD and SQL injection regression",
                    None,
                    b"0.60",
                ),
                (
                    "suite:graphql",
                    "/tmp/workspace/projects/recipe-app/PROJECT.log",
                    4,
                    "- [2026-03-22T23:59:59] Created tests/graphql.test.js covering queries, mutations, and field resolvers",
                    None,
                    b"0.61",
                ),
            ]
            db.executemany(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            db.commit()
        finally:
            db.close()

        def _unpack(blob):
            return [float(bytes(blob).decode("ascii"))]

        def _sim(_query_embedding, chunk_embedding):
            return float(chunk_embedding[0])

        with (
            patch("datastore.docsdb.rag._lib_get_embedding", return_value=[1.0]),
            patch("datastore.docsdb.rag._lib_unpack_embedding", side_effect=_unpack),
            patch("datastore.docsdb.rag._lib_cosine_similarity", side_effect=_sim),
            patch.object(
                rag,
                "_get_project_paths",
                return_value={
                    "home_dir": "/tmp/workspace/projects/recipe-app",
                    "source_roots": [],
                },
            ),
        ):
            results = rag.search_docs(
                "As of 2026-05-08, what test suites existed for the recipe app?",
                limit=4,
                project="recipe-app",
                date_to="2026-05-08",
            )

        assert len(results) == 4
        top_lines = [result["content"].splitlines()[0] for result in results]
        assert any("sharing.test.js" in line for line in top_lines)
        assert any("mealplan.test.js" in line for line in top_lines)
        assert any("recipe.test.js" in line for line in top_lines)
        assert any("graphql.test.js" in line for line in top_lines)

    @patch("datastore.docsdb.rag._lib_get_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_unpack_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_cosine_similarity", return_value=0.05)
    def test_search_docs_date_filtered_project_log_bypasses_similarity_floor(
        self,
        _sim,
        _unpack,
        _embed,
        tmp_path,
    ):
        rag = _make_rag(tmp_path)
        db = sqlite3.connect(rag.db_path)
        try:
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "recipe-log:0",
                    "/tmp/workspace/projects/recipe-app/PROJECT.log",
                    0,
                    "\n".join(
                        [
                            "- [2026-03-04T23:59:59] Initial scaffold complete: Express + SQLite CRUD API and single-page frontend",
                            "- [2026-03-18T23:59:59] Added meal planning tests and dietary filtering suite",
                        ]
                    ),
                    None,
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
                "source_roots": [],
            },
        ):
            results = rag.search_docs(
                "recipe app features",
                limit=10,
                project="recipe-app",
                date_to="2026-03-05",
            )

        assert len(results) == 1
        assert results[0]["source"].endswith("PROJECT.log")
        assert "Initial scaffold complete" in results[0]["content"]
        assert "meal planning tests" not in results[0]["content"]

    @patch("datastore.docsdb.rag._lib_get_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_unpack_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_cosine_similarity", return_value=0.40)
    def test_search_docs_date_filtered_project_log_prefers_cutoff_day_evidence(
        self,
        _sim,
        _unpack,
        _embed,
        tmp_path,
    ):
        rag = _make_rag(tmp_path)
        db = sqlite3.connect(rag.db_path)
        try:
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "recipe-log:0",
                    "/tmp/workspace/projects/recipe-app/PROJECT.log",
                    0,
                    "- [2026-03-11T23:59:59] Test suites existed for recipe app baseline CRUD validation tests",
                    None,
                    b"e",
                ),
            )
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "recipe-log:1",
                    "/tmp/workspace/projects/recipe-app/PROJECT.log",
                    1,
                    "- [2026-03-18T23:59:59] Added tests/dietary.test.js and tests/mealplan.test.js",
                    None,
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
                "source_roots": [],
            },
        ):
            results = rag.search_docs(
                "As of 2026-03-18, what test suites existed for the recipe app?",
                limit=2,
                project="recipe-app",
                date_to="2026-03-18",
            )

        assert len(results) == 2
        assert "mealplan.test.js" in results[0]["content"]
        assert "baseline CRUD" in results[1]["content"]

    @patch("datastore.docsdb.rag._lib_get_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_unpack_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_cosine_similarity", return_value=0.95)
    def test_search_docs_bundle_does_not_attach_current_project_md_for_date_queries(
        self,
        _sim,
        _unpack,
        _embed,
        tmp_path,
    ):
        rag = _make_rag(tmp_path)
        db = sqlite3.connect(rag.db_path)
        try:
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "log:0",
                    "/tmp/workspace/projects/quaid/PROJECT.log",
                    0,
                    "- [2026-03-05T23:59:59] Added legacy recall mode",
                    None,
                    b"e",
                ),
            )
            db.commit()
        finally:
            db.close()

        with patch.object(rag, "load_project_md", side_effect=AssertionError("current PROJECT.md should not attach")):
            bundle = rag.search_docs_bundle(
                "recall mode",
                limit=10,
                docs=["PROJECT.log"],
                date_to="2026-03-10",
            )

        assert bundle["project_md"] is None
        assert len(bundle["chunks"]) == 1

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
    @patch("datastore.docsdb.rag._lib_cosine_similarity", return_value=0.95)
    def test_search_docs_project_filter_bypasses_vec_candidate_stage(self, _sim, _unpack, _embed, tmp_path):
        rag = _make_rag(tmp_path)
        db = sqlite3.connect(rag.db_path)
        try:
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "scoped:0",
                    "/tmp/workspace/projects/quaid/m10-test-doc.md",
                    0,
                    "veiled-wintersmith-runebell",
                    "# Canary",
                    b"e",
                ),
            )
            db.commit()
        finally:
            db.close()

        with patch("datastore.docsdb.rag._lib_has_vec", return_value=True), \
             patch.object(rag, "_doc_vec_table_exists", return_value=True), \
             patch("datastore.docsdb.rag.is_fail_hard_enabled", return_value=False), \
             patch("datastore.docsdb.rag.logger.warning") as warn_mock, \
             patch.object(
                 rag,
                 "_get_project_paths",
                 return_value={"home_dir": "/tmp/workspace/projects/quaid", "source_roots": []},
             ):
            results = rag.search_docs("veiled-wintersmith-runebell", limit=10, project="quaid")

        assert len(results) == 1
        vec_warnings = [str(call.args[0]) for call in warn_mock.call_args_list if call.args]
        assert not any("Doc RAG vec recall failed" in msg for msg in vec_warnings)

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

    @patch("datastore.docsdb.rag._lib_get_embedding", return_value=[1.0])
    @patch("datastore.docsdb.rag._lib_unpack_embedding", side_effect=lambda blob: [0.95] if blob == b"generic" else [0.55])
    @patch("datastore.docsdb.rag._lib_cosine_similarity", side_effect=lambda _query, chunk: chunk[0])
    def test_search_docs_exact_multiterm_anchor_beats_generic_project_boilerplate(self, _sim, _unpack, _embed, tmp_path):
        rag = _make_rag(tmp_path)
        db = sqlite3.connect(rag.db_path)
        try:
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "status:0",
                    "/tmp/workspace/projects/livetest-agentmsg-xp/docs/status.md",
                    0,
                    "The project is operational and ready for coordination.",
                    "# Operational Status",
                    b"generic",
                ),
            )
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "pager:0",
                    "/tmp/workspace/projects/livetest-agentmsg-xp/docs/pager-escalation.md",
                    0,
                    "The codeword `Ember Glass` means pager escalation level 2.",
                    "# Pager Escalation",
                    b"ember",
                ),
            )
            db.commit()
        finally:
            db.close()

        with patch.object(
            rag,
            "_get_project_paths",
            return_value={
                "home_dir": "/tmp/workspace/projects/livetest-agentmsg-xp",
                "source_roots": ["/tmp/workspace/projects/livetest-agentmsg-xp"],
            },
        ):
            results = rag.search_docs("Ember Glass", limit=2, project="livetest-agentmsg-xp")

        assert len(results) == 2
        assert results[0]["source"].endswith("pager-escalation.md")

    @patch("datastore.docsdb.rag._lib_get_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_unpack_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_cosine_similarity", return_value=0.95)
    def test_search_docs_prefers_source_file_over_project_catalog_mention(self, _sim, _unpack, _embed, tmp_path):
        rag = _make_rag(tmp_path)
        db = sqlite3.connect(rag.db_path)
        try:
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "project-current:0",
                    "/tmp/workspace/projects/widget-router/PROJECT.md",
                    0,
                    (
                        "## Current State\n"
                        "- Includes examples.md with distinctive amber field token marker for docs recall probes"
                    ),
                    "## Current State",
                    b"e",
                ),
            )
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "examples:0",
                    "/tmp/workspace/projects/widget-router/docs/examples.md",
                    0,
                    "send('amber field token: subscribed delivery')",
                    "## Subscription Example",
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
                "home_dir": "/tmp/workspace/projects/widget-router",
                "source_roots": ["/tmp/workspace/projects/widget-router"],
            },
        ):
            results = rag.search_docs("amber field token", limit=2, project="widget-router")

        assert len(results) == 2
        assert results[0]["source"].endswith("docs/examples.md")
        assert results[0]["similarity"] >= results[1]["similarity"]

    @patch("datastore.docsdb.rag._lib_get_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_unpack_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_cosine_similarity", return_value=0.95)
    def test_search_docs_prefers_project_log_answer_line_over_project_scaffold(self, _sim, _unpack, _embed, tmp_path):
        rag = _make_rag(tmp_path)
        db = sqlite3.connect(rag.db_path)
        try:
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "project-home:0",
                    "/tmp/workspace/projects/portfolio-site/PROJECT.md",
                    0,
                    "### Project Home\n- `/tmp/workspace/projects/portfolio-site`",
                    "### Project Home",
                    b"e",
                ),
            )
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "recent:0",
                    "/tmp/workspace/projects/portfolio-site/PROJECT.md",
                    1,
                    "## Recent Major Changes\n- [2026-03-15] Initial portfolio site created with about, projects, and contact sections",
                    "## Recent Major Changes",
                    b"e",
                ),
            )
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "log:0",
                    "/tmp/workspace/projects/portfolio-site/PROJECT.log",
                    0,
                    "\n".join(
                        [
                            "- [2026-03-15T23:59:59] Created initial portfolio site with three sections: about, projects, contact",
                            "- [2026-03-15T23:59:59] Projects section includes: recipe app and TechFlow platform redesign",
                        ]
                    ),
                    None,
                    b"e",
                ),
            )
            db.commit()
        finally:
            db.close()

        with patch.object(
            rag,
            "_get_project_paths",
            return_value={"home_dir": "/tmp/workspace/projects/portfolio-site", "source_roots": []},
        ):
            results = rag.search_docs(
                "As of 2026-03-15, what projects were on Maya's portfolio site?",
                limit=5,
                project="portfolio-site",
            )

        assert len(results) >= 1
        assert results[0]["source"].endswith("PROJECT.log")
        assert "Projects section includes:" in results[0]["content"]

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
    def test_search_docs_project_filter_uses_registry_resolved_alias_path(self, _sim, _unpack, _embed, tmp_path):
        rag = _make_rag(tmp_path)
        db = sqlite3.connect(rag.db_path)
        try:
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "alias:0",
                    "/canonical/docs/m10-test-doc.md",
                    0,
                    "veiled-wintersmith-runebell",
                    "# Canary",
                    b"e",
                ),
            )
            db.commit()
        finally:
            db.close()

        class _FakeRegistry:
            def list_docs(self, project=None):
                return [{"file_path": "/alias/docs/m10-test-doc.md"}]

            def _resolve_path(self, path_str):
                return Path(str(path_str).replace("/alias/", "/canonical/"))

        with patch("datastore.docsdb.registry.DocsRegistry", _FakeRegistry), patch.object(
            rag,
            "_get_project_paths",
            return_value={"home_dir": "", "source_roots": []},
        ):
            results = rag.search_docs("veiled-wintersmith-runebell", limit=10, project="quaid")

        assert len(results) == 1
        assert results[0]["source"] == "/canonical/docs/m10-test-doc.md"

    @patch("datastore.docsdb.rag._lib_get_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_unpack_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_cosine_similarity", return_value=0.95)
    def test_search_docs_bundle_adds_scope_hint_for_unlinked_candidates_on_scoped_miss(self, _sim, _unpack, _embed, tmp_path):
        rag = _make_rag(tmp_path)
        rag._shared_scope_enabled = True

        db = sqlite3.connect(rag.db_path)
        try:
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "unlinked:0",
                    "/tmp/workspace/projects/cross-live-test/PROJECT.md",
                    0,
                    "north pier beacon maintenance notes",
                    "# Notes",
                    b"e",
                ),
            )
            db.commit()
        finally:
            db.close()

        with patch("datastore.docsdb.rag._linked_projects_for_current_instance", return_value=(["quaid"], True)), \
             patch(
                 "lib.project_registry.list_all",
                 return_value={
                     "quaid": {"instances": ["cc-main"]},
                     "cross-live-test": {
                         "canonical_path": "/tmp/workspace/projects/cross-live-test",
                         "instances": [],
                     },
                 },
             ), \
             patch.object(
                 rag,
                 "_get_project_paths",
                 return_value={"home_dir": "/tmp/workspace/projects/quaid", "source_roots": []},
             ), \
             patch.object(rag, "infer_project_for_source", return_value="cross-live-test"):
            bundle = rag.search_docs_bundle("north pier beacon", limit=5)

        assert bundle["chunks"] == []
        hint = ((bundle.get("telemetry") or {}).get("scope_hint") or {})
        assert hint.get("type") == "unlinked_project_candidates"
        assert [c["project"] for c in hint.get("candidates", [])] == ["cross-live-test"]
        assert hint["candidates"][0]["path"] == "/tmp/workspace/projects/cross-live-test"

    def test_linked_project_scope_fails_closed_when_reconcile_fails(self, tmp_path):
        from datastore.docsdb import rag as rag_module

        with patch("datastore.docsdb.registry.DocsRegistry", side_effect=RuntimeError("registry boom")), \
             patch("datastore.docsdb.rag.is_fail_hard_enabled", return_value=False), \
             patch("lib.instance.instance_id", return_value="claude-code-private-tmp-cc-livetest"):
            linked, resolved = rag_module._linked_projects_for_current_instance()

        assert linked == []
        assert resolved is True

    def test_linked_project_scope_raises_under_failhard_when_reconcile_fails(self, tmp_path):
        from datastore.docsdb import rag as rag_module

        with patch("datastore.docsdb.registry.DocsRegistry", side_effect=RuntimeError("registry boom")), \
             patch("datastore.docsdb.rag.is_fail_hard_enabled", return_value=True), \
             patch("lib.instance.instance_id", return_value="claude-code-private-tmp-cc-livetest"), \
             pytest.raises(RuntimeError, match="Failed to resolve current instance project scope"):
            rag_module._linked_projects_for_current_instance()

    def test_linked_project_scope_returns_unresolved_when_instance_missing(self, tmp_path):
        from datastore.docsdb import rag as rag_module
        from lib.instance import InstanceError

        with patch("datastore.docsdb.rag.is_fail_hard_enabled", return_value=False), \
             patch("lib.instance.instance_id", side_effect=InstanceError("instance missing")):
            linked, resolved = rag_module._linked_projects_for_current_instance()

        assert linked == []
        assert resolved is False

    @patch("datastore.docsdb.rag._lib_get_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_unpack_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_cosine_similarity", return_value=0.95)
    def test_search_docs_bundle_explicit_project_tolerates_missing_instance_scope(self, _sim, _unpack, _embed, tmp_path):
        from lib.instance import InstanceError

        rag = _make_rag(tmp_path)
        rag._shared_scope_enabled = True

        db = sqlite3.connect(rag.db_path)
        try:
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "chunk-ambient-project",
                    "/tmp/workspace/projects/ambient-app/PROJECT.md",
                    0,
                    "North pier beacon protocol lives here.",
                    "PROJECT",
                    b"e",
                ),
            )
            db.commit()
        finally:
            db.close()

        with patch("lib.instance.instance_id", side_effect=InstanceError("instance missing")), \
             patch.object(
                 rag,
                 "_get_project_paths",
                 return_value={"home_dir": "/tmp/workspace/projects/ambient-app", "source_roots": []},
             ), \
             patch.object(rag, "infer_project_for_source", return_value="ambient-app"):
            bundle = rag.search_docs_bundle("north pier beacon", limit=5, project="ambient-app")

        assert len(bundle["chunks"]) == 1
        assert bundle["chunks"][0]["project"] == "ambient-app"

    @patch("datastore.docsdb.rag._lib_get_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_unpack_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_cosine_similarity", return_value=0.20)
    def test_search_docs_bundle_explicit_project_date_query_falls_back_when_workspace_resolution_needs_instance(
        self,
        _sim,
        _unpack,
        _embed,
        tmp_path,
    ):
        from lib.instance import InstanceError

        rag = _make_rag(tmp_path)
        rag._shared_scope_enabled = True

        db = sqlite3.connect(rag.db_path)
        try:
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "chunk-plog-2023",
                    "/tmp/workspace/projects/livetest-agentmsg-cc/PROJECT.log",
                    0,
                    "\n".join(
                        [
                            "- [2023-02-14T10:00:00] plog-amber-valentine-2023",
                            "- [2024-01-18T08:00:00] plog-jasper-retreat-2024",
                        ]
                    ),
                    None,
                    b"e",
                ),
            )
            db.commit()
        finally:
            db.close()

        with patch("lib.instance.instance_id", side_effect=InstanceError("instance missing")), \
             patch("datastore.docsdb.rag._workspace", side_effect=RuntimeError("workspace unavailable without instance")), \
             patch("datastore.docsdb.rag.get_visible_quaid_home", return_value=Path("/tmp/workspace")), \
             patch.object(
                 rag,
                 "_get_project_paths",
                 return_value={"home_dir": "projects/livetest-agentmsg-cc", "source_roots": []},
             ), \
             patch.object(rag, "infer_project_for_source", return_value="livetest-agentmsg-cc"):
            bundle = rag.search_docs_bundle(
                "plog",
                limit=5,
                project="livetest-agentmsg-cc",
                date_from="2023-01-01",
                date_to="2023-12-31",
            )

        assert len(bundle["chunks"]) == 1
        assert bundle["chunks"][0]["project"] == "livetest-agentmsg-cc"
        assert bundle["chunks"][0]["source_date"] == "2023-02-14"
        assert "2024-01-18" not in bundle["chunks"][0]["content"]

    @patch("datastore.docsdb.rag._lib_get_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_unpack_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag._lib_cosine_similarity", return_value=0.95)
    def test_search_docs_bundle_blocks_explicit_unlinked_project_and_returns_scope_hint(self, _sim, _unpack, _embed, tmp_path):
        rag = _make_rag(tmp_path)
        rag._shared_scope_enabled = True

        db = sqlite3.connect(rag.db_path)
        try:
            db.execute(
                "INSERT INTO doc_chunks (id, source_file, chunk_index, content, section_header, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "unlinked:0",
                    "/tmp/workspace/projects/cross-live-test/PROJECT.md",
                    0,
                    "north pier beacon maintenance notes",
                    "# Notes",
                    b"e",
                ),
            )
            db.commit()
        finally:
            db.close()

        def _project_paths(name: str):
            if name == "cross-live-test":
                return {"home_dir": "/tmp/workspace/projects/cross-live-test", "source_roots": []}
            return {"home_dir": "/tmp/workspace/projects/quaid", "source_roots": []}

        with patch("datastore.docsdb.rag._linked_projects_for_current_instance", return_value=(["quaid"], True)), \
             patch(
                 "lib.project_registry.list_all",
                 return_value={
                     "quaid": {"instances": ["cc-main"]},
                     "cross-live-test": {
                         "canonical_path": "/tmp/workspace/projects/cross-live-test",
                         "instances": [],
                     },
                 },
             ), \
             patch.object(rag, "_get_project_paths", side_effect=_project_paths), \
             patch.object(rag, "infer_project_for_source", return_value="cross-live-test"):
            bundle = rag.search_docs_bundle("north pier beacon", limit=5, project="cross-live-test")

        assert bundle["chunks"] == []
        hint = ((bundle.get("telemetry") or {}).get("scope_hint") or {})
        assert hint.get("type") == "unlinked_project_candidates"
        assert hint.get("requested_project") == "cross-live-test"
        assert [c["project"] for c in hint.get("candidates", [])] == ["cross-live-test"]
        assert hint["candidates"][0]["path"] == "/tmp/workspace/projects/cross-live-test"

    @patch("datastore.docsdb.rag._lib_get_embedding", return_value=[0.1, 0.2, 0.3])
    @patch("datastore.docsdb.rag.is_fail_hard_enabled", return_value=True)
    def test_search_docs_project_filter_raises_when_registry_paths_fail_under_failhard(self, _failhard, _embed, tmp_path):
        rag = _make_rag(tmp_path)

        class _BrokenRegistry:
            def list_docs(self, project=None):
                raise RuntimeError("registry unavailable")

        with patch("datastore.docsdb.registry.DocsRegistry", _BrokenRegistry), \
             patch.object(
                 rag,
                 "_get_project_paths",
                 return_value={"home_dir": "", "source_roots": []},
             ):
            with pytest.raises(RuntimeError, match="registry unavailable|Failed to load docs registry paths"):
                rag.search_docs("veiled-wintersmith-runebell", limit=10, project="quaid")

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

    def test_get_project_paths_falls_back_to_visible_project_dir(self, tmp_path, monkeypatch):
        rag = _make_rag(tmp_path)
        visible_home = tmp_path / "visible"
        project_dir = visible_home / "projects" / "portfolio-site"
        project_dir.mkdir(parents=True)
        (project_dir / "PROJECT.log").write_text("- [2026-03-15T10:00:00] Built About and Projects sections\n")
        monkeypatch.setenv("QUAID_VISIBLE_HOME", str(visible_home))

        paths = rag._get_project_paths("portfolio-site")

        assert paths["home_dir"] == str(project_dir)
        assert paths["source_roots"] == []

    @patch("datastore.docsdb.rag._lib_get_embedding", return_value=[0.1, 0.2, 0.3])
    def test_search_docs_bundle_reads_project_sources_when_index_empty(self, _embed, tmp_path, monkeypatch):
        rag = _make_rag(tmp_path)
        visible_home = tmp_path / "visible"
        project_dir = visible_home / "projects" / "portfolio-site"
        project_dir.mkdir(parents=True)
        (project_dir / "PROJECT.log").write_text(
            "\n".join(
                [
                    "- [2026-03-12T10:00:00] Unrelated recipe-app backend cleanup",
                    "- [2026-03-15T11:00:00] Built portfolio site About, Projects, and Contact sections",
                    "- [2026-03-16T12:00:00] Added Stripe Payments Platform project card",
                ]
            )
        )
        (project_dir / "PROJECT.md").write_text(
            "# Portfolio Site\nCurrent state: About, Projects, Contact, and Stripe Payments Platform card\n"
        )
        monkeypatch.setenv("QUAID_VISIBLE_HOME", str(visible_home))

        bundle = rag.search_docs_bundle(
            "What did the agent build for Maya's portfolio site?",
            limit=3,
            project="portfolio-site",
        )

        assert bundle["project"] == "portfolio-site"
        contents = "\n".join(chunk["content"] for chunk in bundle["chunks"])
        assert "About, Projects, and Contact" in contents
        assert "Stripe Payments Platform" in contents

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

    def test_registry_third_pass_dedupes_duplicate_paths(self, tmp_path):
        """Duplicate registry rows for the same file are indexed once."""
        handler = self._register_and_get_handler()
        ctx = _make_rag_ctx(tmp_path)

        existing_file = tmp_path / "dup.md"
        existing_file.write_text("# Duplicate\nStill one file.")
        resolved = str(existing_file.resolve())

        fake_reg = MagicMock()
        fake_reg.list_docs.return_value = [
            {"file_path": str(existing_file)},
            {"file_path": str(existing_file)},
        ]

        with patch("datastore.docsdb.rag.DocsRAG.reindex_all", return_value=_empty_reindex_result()), \
             patch("datastore.docsdb.registry.DocsRegistry", return_value=fake_reg), \
             patch("datastore.docsdb.rag.DocsRAG.needs_reindex_many", return_value={resolved: True}), \
             patch("datastore.docsdb.rag.DocsRAG.index_document", return_value=2) as mock_index:
            result = handler(ctx)

        mock_index.assert_called_once_with(resolved)
        assert result.metrics["rag_files_indexed"] >= 1

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
             patch("datastore.docsdb.rag.is_fail_hard_enabled", return_value=False), \
             patch("datastore.docsdb.registry.DocsRegistry", return_value=fake_reg):
            # Must not raise — exception is swallowed and logged as a warning
            result = handler(ctx)

        # No unhandled errors in result (the warning goes to logger, not result.errors)
        assert result.errors == []

    def test_list_docs_exception_raises_when_fail_hard(self, tmp_path):
        handler = self._register_and_get_handler()
        ctx = _make_rag_ctx(tmp_path)

        fake_reg = MagicMock()
        fake_reg.list_docs.side_effect = Exception("db exploded")

        with patch("datastore.docsdb.rag.DocsRAG.reindex_all", return_value=_empty_reindex_result()), \
             patch("datastore.docsdb.rag.is_fail_hard_enabled", return_value=True), \
             patch("datastore.docsdb.registry.DocsRegistry", return_value=fake_reg), \
             pytest.raises(RuntimeError, match="RAG maintenance failed"):
            handler(ctx)
