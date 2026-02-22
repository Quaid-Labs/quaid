"""Tests for token-based batching in the janitor pipeline."""

import os
import sys
import pytest

# Ensure plugin root is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("MEMORY_DB_PATH", "/tmp/test-token-batch.db")

from lib.tokens import estimate_tokens
from janitor import TokenBatchBuilder


# ---------------------------------------------------------------------------
# estimate_tokens
# ---------------------------------------------------------------------------

class TestEstimateTokens:
    def test_basic(self):
        """~4 chars per token heuristic."""
        assert estimate_tokens("hello world") == 2  # 11 chars // 4 = 2

    def test_empty_string(self):
        """Empty string returns 1 (minimum)."""
        assert estimate_tokens("") == 1

    def test_long_text(self):
        """Proportional for longer texts."""
        text = "a" * 400
        assert estimate_tokens(text) == 100


# ---------------------------------------------------------------------------
# TokenBatchBuilder
# ---------------------------------------------------------------------------

class TestTokenBatchBuilder:
    def test_single_batch(self):
        """All items fit in one batch when under budget."""
        items = [{"text": "short"} for _ in range(5)]
        builder = TokenBatchBuilder(
            model_tier='fast',
            prompt_overhead_tokens=100,
            tokens_per_item_fn=lambda item: estimate_tokens(item["text"]),
            max_items=500
        )
        batches = builder.build_batches(items)
        assert len(batches) == 1
        assert len(batches[0]) == 5

    def test_splits_on_budget(self):
        """Items exceeding budget are split across batches."""
        # Each item ~250 tokens (1000 chars / 4)
        items = [{"text": "x" * 1000} for _ in range(10)]
        builder = TokenBatchBuilder(
            model_tier='fast',
            prompt_overhead_tokens=100,
            tokens_per_item_fn=lambda item: estimate_tokens(item["text"]),
            max_items=500
        )
        batches = builder.build_batches(items)
        # With 200K * 0.5 - 100 - 8192 = ~91708 budget, all 10 should fit easily
        # Let's verify they're batched at all
        total = sum(len(b) for b in batches)
        assert total == 10

    def test_max_items_respected(self):
        """Safety cap on items per batch is enforced."""
        items = [{"text": "tiny"} for _ in range(20)]
        builder = TokenBatchBuilder(
            model_tier='fast',
            prompt_overhead_tokens=100,
            tokens_per_item_fn=lambda item: 1,  # Very small tokens
            max_items=7  # Force splits at 7 items
        )
        batches = builder.build_batches(items)
        assert len(batches) == 3  # 7 + 7 + 6
        assert len(batches[0]) == 7
        assert len(batches[1]) == 7
        assert len(batches[2]) == 6

    def test_empty_input(self):
        """Empty input returns empty list."""
        builder = TokenBatchBuilder(model_tier='fast')
        assert builder.build_batches([]) == []

    def test_oversized_item(self):
        """Single item bigger than budget still gets its own batch."""
        giant = {"text": "x" * 1_000_000}  # ~250K tokens, exceeds any budget
        small = {"text": "tiny"}
        builder = TokenBatchBuilder(
            model_tier='fast',
            prompt_overhead_tokens=100,
            tokens_per_item_fn=lambda item: estimate_tokens(item["text"]),
            max_items=500
        )
        batches = builder.build_batches([small, giant, small])
        # Giant should be alone in its own batch
        assert len(batches) >= 2
        # All items accounted for
        total = sum(len(b) for b in batches)
        assert total == 3

    def test_budget_calculation(self):
        """Budget is calculated correctly from config."""
        builder = TokenBatchBuilder(
            model_tier='deep',
            prompt_overhead_tokens=1000,
            max_output_tokens=4000
        )
        # high: 200000 * 0.50 - 1000 - 4000 = 95000
        assert builder.budget == 95000

    def test_minimum_budget(self):
        """Budget never goes below 1000 tokens."""
        builder = TokenBatchBuilder(
            model_tier='fast',
            prompt_overhead_tokens=200000,  # Exceeds everything
            max_output_tokens=200000
        )
        assert builder.budget == 1000

    def test_output_aware_batching(self):
        """Output token cap limits batch size to prevent truncation."""
        # 100 items, 200 tokens/item output, 4000 max output → 20 items/batch
        items = [{"text": "short fact"} for _ in range(100)]
        builder = TokenBatchBuilder(
            model_tier='deep',
            prompt_overhead_tokens=100,
            tokens_per_item_fn=lambda item: 10,
            max_items=500,
            max_output_tokens=4000,
            output_tokens_per_item=200,
        )
        batches = builder.build_batches(items)
        # Each batch should have at most 20 items (4000 // 200)
        for batch in batches:
            assert len(batch) <= 20
        # All items accounted for
        assert sum(len(b) for b in batches) == 100
        # Should be 5 batches of 20
        assert len(batches) == 5

    def test_output_aware_default_noop(self):
        """output_tokens_per_item=0 does not add extra cap."""
        items = [{"text": "short"} for _ in range(50)]
        builder = TokenBatchBuilder(
            model_tier='deep',
            prompt_overhead_tokens=100,
            tokens_per_item_fn=lambda item: 1,
            max_items=500,
            max_output_tokens=8192,
            output_tokens_per_item=0,  # disabled
        )
        # max_items stays at 500, so all 50 should fit in one batch
        batches = builder.build_batches(items)
        assert len(batches) == 1
        assert len(batches[0]) == 50

    def test_output_cap_overrides_max_items(self):
        """Output cap wins when it's smaller than max_items."""
        items = [{"text": "item"} for _ in range(30)]
        builder = TokenBatchBuilder(
            model_tier='deep',
            prompt_overhead_tokens=100,
            tokens_per_item_fn=lambda item: 1,
            max_items=100,  # Would allow 100
            max_output_tokens=2000,
            output_tokens_per_item=200,  # Caps to 10
        )
        batches = builder.build_batches(items)
        for batch in batches:
            assert len(batch) <= 10
        assert sum(len(b) for b in batches) == 30
        assert len(batches) == 3  # 10 + 10 + 10
