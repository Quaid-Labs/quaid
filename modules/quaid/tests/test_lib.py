"""Tests for shared library files: similarity, embeddings, database, tokens."""

import os
import sys
import struct
import sqlite3
from pathlib import Path
from unittest.mock import patch

# Ensure plugin root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Set env to avoid touching real DB
os.environ.setdefault("MEMORY_DB_PATH", ":memory:")

import pytest

from lib.similarity import cosine_similarity
from lib.embeddings import pack_embedding, unpack_embedding
from lib.tokens import estimate_tokens, extract_key_tokens, STOPWORDS


# ---------------------------------------------------------------------------
# lib/similarity.py — cosine_similarity
# ---------------------------------------------------------------------------

class TestCosineSimilarity:
    """Tests for cosine_similarity()."""

    def test_identical_vectors_return_one(self):
        v = [1.0, 2.0, 3.0]
        result = cosine_similarity(v, v)
        assert abs(result - 1.0) < 1e-5

    def test_orthogonal_vectors_return_zero(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        result = cosine_similarity(a, b)
        assert abs(result) < 1e-5

    def test_zero_vector_returns_zero(self):
        a = [0.0, 0.0, 0.0]
        b = [1.0, 2.0, 3.0]
        assert cosine_similarity(a, b) == 0.0

    def test_both_zero_vectors_return_zero(self):
        a = [0.0, 0.0]
        b = [0.0, 0.0]
        assert cosine_similarity(a, b) == 0.0

    def test_empty_vectors_return_zero(self):
        assert cosine_similarity([], []) == 0.0

    def test_empty_first_vector(self):
        assert cosine_similarity([], [1.0, 2.0]) == 0.0

    def test_empty_second_vector(self):
        assert cosine_similarity([1.0, 2.0], []) == 0.0

    def test_opposite_vectors_return_negative_one(self):
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        result = cosine_similarity(a, b)
        assert abs(result - (-1.0)) < 1e-5

    def test_similar_vectors_high_similarity(self):
        a = [1.0, 2.0, 3.0]
        b = [1.1, 2.1, 3.1]
        result = cosine_similarity(a, b)
        assert result > 0.99

    def test_dimension_mismatch_returns_zero(self):
        a = [1.0, 2.0]
        b = [1.0, 2.0, 3.0]
        assert cosine_similarity(a, b) == 0.0

    def test_single_element_vectors(self):
        a = [5.0]
        b = [3.0]
        result = cosine_similarity(a, b)
        assert abs(result - 1.0) < 1e-5  # Same direction, just different magnitude


# ---------------------------------------------------------------------------
# lib/embeddings.py — pack/unpack
# ---------------------------------------------------------------------------

class TestPackUnpackEmbedding:
    """Tests for pack_embedding() and unpack_embedding()."""

    def test_round_trip(self):
        original = [1.0, 2.5, 3.14, -0.5, 0.0]
        packed = pack_embedding(original)
        unpacked = unpack_embedding(packed)
        assert len(unpacked) == len(original)
        for a, b in zip(original, unpacked):
            assert abs(a - b) < 1e-5

    def test_pack_produces_correct_byte_length(self):
        embedding = [1.0, 2.0, 3.0]
        packed = pack_embedding(embedding)
        assert len(packed) == 12  # 3 floats * 4 bytes each

    def test_pack_single_element(self):
        embedding = [42.0]
        packed = pack_embedding(embedding)
        assert len(packed) == 4

    def test_pack_empty_list(self):
        packed = pack_embedding([])
        assert len(packed) == 0

    def test_unpack_empty_blob(self):
        result = unpack_embedding(b"")
        assert result == []

    def test_large_vector_round_trip(self):
        """128-dim vector survives pack/unpack."""
        original = [float(i) / 100.0 for i in range(128)]
        packed = pack_embedding(original)
        unpacked = unpack_embedding(packed)
        assert len(unpacked) == 128
        for a, b in zip(original, unpacked):
            assert abs(a - b) < 1e-5

    def test_negative_values_round_trip(self):
        original = [-1.0, -0.5, 0.0, 0.5, 1.0]
        packed = pack_embedding(original)
        unpacked = unpack_embedding(packed)
        for a, b in zip(original, unpacked):
            assert abs(a - b) < 1e-5


# ---------------------------------------------------------------------------
# lib/database.py — get_connection
# ---------------------------------------------------------------------------

class TestGetConnection:
    """Tests for lib.database.get_connection()."""

    def test_returns_connection_with_row_factory(self, tmp_path):
        from lib.database import get_connection
        db_path = tmp_path / "test.db"
        with get_connection(db_path) as conn:
            assert conn.row_factory == sqlite3.Row

    def test_foreign_keys_enabled(self, tmp_path):
        from lib.database import get_connection
        db_path = tmp_path / "test.db"
        with get_connection(db_path) as conn:
            result = conn.execute("PRAGMA foreign_keys").fetchone()
            assert result[0] == 1

    def test_commits_on_clean_exit(self, tmp_path):
        from lib.database import get_connection
        db_path = tmp_path / "test.db"
        with get_connection(db_path) as conn:
            conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, val TEXT)")
            conn.execute("INSERT INTO test VALUES (1, 'hello')")

        # Verify data persisted
        with get_connection(db_path) as conn:
            row = conn.execute("SELECT val FROM test WHERE id = 1").fetchone()
            assert row[0] == "hello"

    def test_rollback_on_exception(self, tmp_path):
        from lib.database import get_connection
        db_path = tmp_path / "test.db"

        # Create table first
        with get_connection(db_path) as conn:
            conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, val TEXT)")

        # Insert in a failing context
        with pytest.raises(ValueError):
            with get_connection(db_path) as conn:
                conn.execute("INSERT INTO test VALUES (1, 'should_rollback')")
                raise ValueError("force rollback")

        # Verify data was rolled back
        with get_connection(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM test").fetchone()[0]
            assert count == 0

    def test_fk_violation_raises(self, tmp_path):
        """Foreign key constraint violations are enforced."""
        from lib.database import get_connection
        db_path = tmp_path / "test.db"
        with get_connection(db_path) as conn:
            conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
            conn.execute("""
                CREATE TABLE child (
                    id INTEGER PRIMARY KEY,
                    parent_id INTEGER REFERENCES parent(id)
                )
            """)

        with pytest.raises(sqlite3.IntegrityError):
            with get_connection(db_path) as conn:
                conn.execute("INSERT INTO child VALUES (1, 999)")


# ---------------------------------------------------------------------------
# lib/tokens.py — estimate_tokens
# ---------------------------------------------------------------------------

class TestEstimateTokens:
    """Additional tests for estimate_tokens() beyond test_token_batching.py."""

    def test_cjk_text_higher_token_estimate(self):
        """CJK text should estimate more tokens per character."""
        cjk = "\u4f60\u597d\u4e16\u754c"  # 4 CJK chars
        ascii_text = "abcd"  # 4 ASCII chars
        cjk_tokens = estimate_tokens(cjk)
        ascii_tokens = estimate_tokens(ascii_text)
        # CJK should produce more tokens than ASCII for same char count
        assert cjk_tokens > ascii_tokens

    def test_mixed_cjk_ascii(self):
        """Mixed CJK + ASCII text."""
        text = "hello \u4f60\u597d"  # 6 ASCII + 2 CJK chars
        tokens = estimate_tokens(text)
        assert tokens >= 1

    def test_emoji_text(self):
        """Emoji characters (> 0x2E80) get higher estimate."""
        # Emojis are above 0x2E80
        emoji_text = "\U0001f600\U0001f601\U0001f602"  # 3 emojis
        tokens = estimate_tokens(emoji_text)
        assert tokens >= 1


class TestExtractKeyTokens:
    """Additional tests for extract_key_tokens()."""

    def test_numbers_excluded(self):
        """Tokens starting with digits are excluded by regex pattern."""
        tokens = extract_key_tokens("Quaid is 35 years old")
        assert "35" not in tokens

    def test_stopwords_in_frozenset(self):
        """Verify key stopwords are present."""
        assert "the" in STOPWORDS
        assert "and" in STOPWORDS
        assert "is" in STOPWORDS

    def test_hyphenated_words(self):
        """Hyphenated words are preserved."""
        tokens = extract_key_tokens("Claude has a self-hosted service running")
        # Should include hyphenated or split tokens
        assert len(tokens) > 0
