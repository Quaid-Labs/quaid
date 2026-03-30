"""Regression tests for config-backed docs bundle caps in _docs_bundle_to_rows.

Covers:
- per_chunk_char_cap: long chunks are truncated at the configured limit
- total_char_budget: iteration stops once total chars consumed exceeds budget
- Early-exit when remaining budget <= 160 chars
- Config override via retrieval.docs_per_chunk_char_cap / retrieval.docs_total_char_budget
- Default cap values (900 / 9000) apply when config is absent or raises
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datastore.memorydb.memory_graph import _docs_bundle_to_rows, _validate_docs_bundle


def _bundle(*texts, similarity=0.8):
    """Build a minimal docs bundle from non-empty chunk texts."""
    return {
        "chunks": [
            {"content": t, "source": "test.md", "section_header": "", "similarity": similarity}
            for t in texts
        ]
    }


def _mock_config(per_chunk=900, total=9000):
    retrieval = SimpleNamespace(docs_per_chunk_char_cap=per_chunk, docs_total_char_budget=total)
    return SimpleNamespace(retrieval=retrieval)


def _content_part(row):
    """Extract the content portion after the '[docs] source: ' prefix."""
    return row["text"].split(": ", 1)[1]


# ---------------------------------------------------------------------------
# Default cap behaviour (no config override)
# ---------------------------------------------------------------------------

class TestDocsBundleDefaultCaps:
    def test_short_chunks_pass_through_unchanged(self):
        text = "x" * 100
        rows = _docs_bundle_to_rows(_bundle(text), limit=10)
        assert len(rows) == 1
        assert text in rows[0]["text"]

    def test_chunk_truncated_at_default_per_chunk_cap(self):
        # 1100 chars > 900 default cap — content portion should be truncated.
        long_text = "a" * 1100
        rows = _docs_bundle_to_rows(_bundle(long_text), limit=10)
        assert len(rows) == 1
        part = _content_part(rows[0])
        assert len(part) <= 900
        assert part.endswith("…")

    def test_total_budget_stops_iteration(self):
        # 11 chunks of 900 chars — 10th fills the 9000-char budget, 11th skipped.
        chunk = "b" * 900
        rows = _docs_bundle_to_rows(_bundle(*([chunk] * 11)), limit=20)
        assert len(rows) == 10

    def test_limit_respected_independent_of_budget(self):
        chunk = "e" * 10
        rows = _docs_bundle_to_rows(_bundle(*([chunk] * 20)), limit=3)
        assert len(rows) == 3

    def test_empty_bundle_returns_empty(self):
        assert _docs_bundle_to_rows(_bundle(), limit=10) == []



# ---------------------------------------------------------------------------
# Config override via retrieval.docs_per_chunk_char_cap / docs_total_char_budget
# ---------------------------------------------------------------------------

class TestDocsBundleConfigOverride:
    def test_config_per_chunk_cap_overrides_default(self):
        # Small cap (100) — a 200-char chunk must be truncated to ≤100.
        with patch("config.get_config", return_value=_mock_config(per_chunk=100, total=9000)):
            text = "f" * 200
            rows = _docs_bundle_to_rows(_bundle(text), limit=10)
        assert len(rows) == 1
        part = _content_part(rows[0])
        assert len(part) <= 100
        assert part.endswith("…")

    def test_config_total_budget_overrides_default(self):
        # Budget of 300: three 100-char chunks fit (300 total), fourth is skipped.
        with patch("config.get_config", return_value=_mock_config(per_chunk=900, total=300)):
            chunk = "g" * 100
            rows = _docs_bundle_to_rows(_bundle(*([chunk] * 5)), limit=10)
        assert len(rows) == 3

    def test_early_exit_when_remaining_budget_le_160(self):
        # Budget=600, first chunk 500 chars → consumed=500, remaining=100 ≤ 160.
        # Second chunk (200 chars) triggers the early-exit break.
        with patch("config.get_config", return_value=_mock_config(per_chunk=900, total=600)):
            rows = _docs_bundle_to_rows(_bundle("h" * 500, "i" * 200), limit=10)
        assert len(rows) == 1

    def test_config_exception_falls_back_to_defaults(self):
        # If get_config raises, caps fall back to 900/9000.
        with patch("config.get_config", side_effect=RuntimeError("no config")):
            text = "j" * 1100
            rows = _docs_bundle_to_rows(_bundle(text), limit=10)
        assert len(rows) == 1
        part = _content_part(rows[0])
        assert len(part) <= 900
        assert part.endswith("…")

    def test_config_missing_retrieval_attr_falls_back_to_defaults(self):
        # Config with no retrieval attribute — defaults apply.
        with patch("config.get_config", return_value=SimpleNamespace()):
            text = "k" * 1100
            rows = _docs_bundle_to_rows(_bundle(text), limit=10)
        part = _content_part(rows[0])
        assert len(part) <= 900


# ---------------------------------------------------------------------------
# _validate_docs_bundle — used by the adaptive empty-fallback check
# ---------------------------------------------------------------------------

class TestValidateDocsBundleForAdaptiveCheck:
    """_validate_docs_bundle(result).get("chunks") is what the adaptive fallback
    inspects. Verify its behaviour on empty and non-empty bundles."""

    def test_none_bundle_has_empty_chunks(self):
        assert _validate_docs_bundle(None).get("chunks") == []

    def test_empty_dict_bundle_has_empty_chunks(self):
        assert _validate_docs_bundle({}).get("chunks") == []

    def test_bundle_with_chunks_is_non_empty(self):
        b = _bundle("some text")
        assert len(_validate_docs_bundle(b).get("chunks", [])) == 1
