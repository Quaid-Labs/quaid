"""Batch processing utilities for LLM calls.

Two core patterns used throughout Quaid:

1. **Parallel batching**: Split input into token-sized chunks, process all
   chunks concurrently, merge results. Good when chunks are independent.

2. **Waterfall (cascading) batching**: Process chunks serially where each
   batch's distilled output feeds the next batch as carryover context.
   Good when later chunks need context from earlier ones (e.g. extraction).

IMPORTANT: These exist because truncation is BANNED in this codebase.
Never truncate data to fit a context window. Always batch it instead.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, TypeVar

from lib.tokens import estimate_tokens

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Default chunk size in tokens. Conservative to leave room for system prompt
# and response. Override per call site based on model context window.
DEFAULT_CHUNK_TOKENS = 8000


@dataclass
class ChunkResult:
    """Result from processing a single chunk."""
    chunk_index: int
    output: Any
    input_tokens: int = 0
    error: Optional[str] = None


def chunk_by_tokens(
    items: List[str],
    max_tokens: int = DEFAULT_CHUNK_TOKENS,
    separator: str = "\n",
) -> List[List[str]]:
    """Split a list of text items into chunks that fit within a token budget.

    Never splits an individual item across chunks. If a single item exceeds
    the budget, it gets its own chunk (we process it anyway — no truncation).

    Args:
        items: List of text strings to chunk
        max_tokens: Maximum tokens per chunk
        separator: Separator used between items (counted toward tokens)

    Returns:
        List of chunks, where each chunk is a list of items
    """
    chunks = []
    current_chunk = []
    current_tokens = 0
    sep_tokens = estimate_tokens(separator)

    for item in items:
        item_tokens = estimate_tokens(item)

        # If adding this item would exceed budget and we have items already
        if current_chunk and (current_tokens + item_tokens + sep_tokens) > max_tokens:
            chunks.append(current_chunk)
            current_chunk = []
            current_tokens = 0

        current_chunk.append(item)
        current_tokens += item_tokens + (sep_tokens if current_tokens > 0 else 0)

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def chunk_text_by_tokens(
    text: str,
    max_tokens: int = DEFAULT_CHUNK_TOKENS,
    split_on: str = "\n",
) -> List[str]:
    """Split a single text into token-sized chunks at natural boundaries.

    Splits on the given delimiter (default: newlines). Never splits mid-line.
    If a single line exceeds the budget, it gets its own chunk.

    Returns:
        List of text chunks
    """
    lines = text.split(split_on)
    chunks = chunk_by_tokens(lines, max_tokens=max_tokens, separator=split_on)
    return [split_on.join(chunk) for chunk in chunks]


def parallel_batch(
    items: List[str],
    process_fn: Callable[[str, int], Any],
    max_tokens: int = DEFAULT_CHUNK_TOKENS,
    max_workers: int = 4,
    separator: str = "\n",
) -> List[ChunkResult]:
    """Process items in parallel batches.

    Splits items into token-sized chunks, processes all chunks concurrently,
    returns results in order.

    Args:
        items: List of text items to process
        process_fn: Function(chunk_text, chunk_index) -> result
        max_tokens: Max tokens per chunk
        max_workers: Max concurrent workers
        separator: Separator for joining items in a chunk

    Returns:
        List of ChunkResult in chunk order
    """
    chunks = chunk_by_tokens(items, max_tokens=max_tokens, separator=separator)
    if not chunks:
        return []

    results = [None] * len(chunks)

    def _process(idx: int, chunk: List[str]) -> ChunkResult:
        chunk_text = separator.join(chunk)
        try:
            output = process_fn(chunk_text, idx)
            return ChunkResult(
                chunk_index=idx, output=output,
                input_tokens=estimate_tokens(chunk_text),
            )
        except Exception as e:
            logger.error("[batch] Chunk %d failed: %s", idx, e)
            return ChunkResult(
                chunk_index=idx, output=None,
                input_tokens=estimate_tokens(chunk_text),
                error=str(e),
            )

    if len(chunks) == 1:
        # Skip thread pool overhead for single chunk
        results[0] = _process(0, chunks[0])
    else:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(chunks))) as pool:
            futures = {
                pool.submit(_process, i, chunk): i
                for i, chunk in enumerate(chunks)
            }
            for future in as_completed(futures):
                idx = futures[future]
                results[idx] = future.result()

    return [r for r in results if r is not None]


def waterfall_batch(
    items: List[str],
    process_fn: Callable[[str, str, int], str],
    max_tokens: int = DEFAULT_CHUNK_TOKENS,
    initial_carryover: str = "",
    separator: str = "\n",
) -> str:
    """Process items in a serial waterfall where each batch's output feeds the next.

    This is the cascading distillation pattern used in extraction: the
    distilled output from batch N becomes carryover context for batch N+1.

    Args:
        items: List of text items to process
        process_fn: Function(chunk_text, carryover, chunk_index) -> distilled_output
        max_tokens: Max tokens per chunk (excluding carryover)
        initial_carryover: Starting carryover context (empty for first run)
        separator: Separator for joining items in a chunk

    Returns:
        Final distilled output from the last batch
    """
    chunks = chunk_by_tokens(items, max_tokens=max_tokens, separator=separator)
    if not chunks:
        return initial_carryover

    carryover = initial_carryover
    for i, chunk in enumerate(chunks):
        chunk_text = separator.join(chunk)
        try:
            carryover = process_fn(chunk_text, carryover, i)
            logger.info(
                "[waterfall] Batch %d/%d processed (%d tokens in, %d tokens out)",
                i + 1, len(chunks),
                estimate_tokens(chunk_text), estimate_tokens(carryover),
            )
        except Exception as e:
            logger.error("[waterfall] Batch %d failed: %s", i, e)
            # Continue with existing carryover — don't lose previous work
            continue

    return carryover
