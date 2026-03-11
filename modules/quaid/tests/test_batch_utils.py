"""Tests for lib/batch_utils.py — parallel and waterfall batching."""

import pytest
from lib.batch_utils import chunk_by_tokens, chunk_text_by_tokens, parallel_batch, waterfall_batch


class TestChunkByTokens:
    def test_single_chunk_under_budget(self):
        items = ["hello", "world"]
        chunks = chunk_by_tokens(items, max_tokens=1000)
        assert len(chunks) == 1
        assert chunks[0] == ["hello", "world"]

    def test_splits_at_budget_boundary(self):
        # Each item is ~25 tokens (100 chars / 4)
        items = ["x" * 100, "y" * 100, "z" * 100]
        chunks = chunk_by_tokens(items, max_tokens=60)
        # Each item is ~25 tokens, budget is 60 — fits 2 per chunk
        assert len(chunks) == 2
        assert chunks[0] == ["x" * 100, "y" * 100]
        assert chunks[1] == ["z" * 100]

    def test_oversized_item_gets_own_chunk(self):
        items = ["x" * 1000, "y" * 10]
        chunks = chunk_by_tokens(items, max_tokens=50)
        assert len(chunks) == 2
        assert chunks[0] == ["x" * 1000]  # Over budget but not truncated!
        assert chunks[1] == ["y" * 10]

    def test_empty_input(self):
        assert chunk_by_tokens([], max_tokens=100) == []

    def test_single_item(self):
        chunks = chunk_by_tokens(["hello"], max_tokens=100)
        assert len(chunks) == 1
        assert chunks[0] == ["hello"]


class TestChunkTextByTokens:
    def test_splits_on_newlines(self):
        text = "\n".join(["line " + str(i) for i in range(100)])
        chunks = chunk_text_by_tokens(text, max_tokens=50)
        assert len(chunks) > 1
        # Reassembled text should equal original
        assert "\n".join(chunks) == text

    def test_small_text_single_chunk(self):
        text = "hello\nworld"
        chunks = chunk_text_by_tokens(text, max_tokens=1000)
        assert len(chunks) == 1
        assert chunks[0] == text


class TestParallelBatch:
    def test_processes_all_chunks(self):
        items = ["a" * 100, "b" * 100, "c" * 100]

        def process(text, idx):
            return f"processed-{idx}-{len(text)}"

        results = parallel_batch(items, process, max_tokens=30)
        assert len(results) == 3
        assert all(r.error is None for r in results)
        assert all("processed" in r.output for r in results)

    def test_handles_errors(self):
        items = ["good", "bad", "good2"]

        def process(text, idx):
            if "bad" in text:
                raise ValueError("intentional")
            return f"ok-{idx}"

        results = parallel_batch(items, process, max_tokens=1000)
        assert len(results) == 1  # All in one chunk, fails entirely
        assert results[0].error is not None

    def test_single_chunk_skips_threadpool(self):
        items = ["hello"]
        results = parallel_batch(items, lambda t, i: "done", max_tokens=1000)
        assert len(results) == 1
        assert results[0].output == "done"

    def test_empty_input(self):
        results = parallel_batch([], lambda t, i: None, max_tokens=100)
        assert results == []


class TestWaterfallBatch:
    def test_cascading_carryover(self):
        items = ["a" * 100, "b" * 100, "c" * 100]

        def process(chunk, carryover, idx):
            return f"{carryover}+batch{idx}"

        result = waterfall_batch(items, process, max_tokens=30, initial_carryover="start")
        # Should cascade: start -> start+batch0 -> start+batch0+batch1 -> ...
        assert "start" in result
        assert "batch0" in result
        assert "batch1" in result
        assert "batch2" in result

    def test_single_batch(self):
        items = ["hello"]
        result = waterfall_batch(
            items,
            lambda chunk, carry, idx: f"{carry}|{chunk}",
            max_tokens=1000,
            initial_carryover="init",
        )
        assert result == "init|hello"

    def test_error_preserves_carryover(self):
        items = ["a" * 100, "b" * 100, "c" * 100]
        call_count = [0]

        def process(chunk, carryover, idx):
            call_count[0] += 1
            if idx == 1:
                raise ValueError("fail batch 1")
            return f"{carryover}+batch{idx}"

        result = waterfall_batch(items, process, max_tokens=30, initial_carryover="start")
        # Batch 1 fails — carryover from batch 0 should persist to batch 2
        assert "start+batch0" in result
        assert "batch2" in result

    def test_empty_input_returns_initial(self):
        result = waterfall_batch([], lambda c, ca, i: "x", initial_carryover="init")
        assert result == "init"
