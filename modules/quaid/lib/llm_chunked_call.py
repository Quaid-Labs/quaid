"""LLM chunked call utilities — process large content through LLM without truncation.

Wraps batch_utils chunking with actual LLM calls. Two patterns:

1. **parallel_llm_call**: Split content into chunks, send each to the LLM
   concurrently with the same system prompt, merge results.
   Good for: classification, tagging, independent analysis.

2. **waterfall_llm_call**: Process chunks serially where each chunk's
   distilled LLM output becomes carryover context for the next.
   Good for: extraction, summarization, document updates.

Both accept:
- A system prompt (master instructions)
- Content to process (text or file path)
- Optional per-chunk prompt template
- Config-driven chunk size

IMPORTANT: These exist because truncation is BANNED in this codebase.
"""

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from lib.batch_utils import (
    ChunkResult,
    DEFAULT_CHUNK_TOKENS,
    chunk_text_by_tokens,
)
from lib.tokens import estimate_tokens

logger = logging.getLogger(__name__)

# Default chunk size for LLM calls. Can be overridden per call or via config.
_DEFAULT_LLM_CHUNK_TOKENS = 6000


def _get_configured_chunk_tokens() -> int:
    """Read chunk token budget from config, with fallback."""
    try:
        from config import get_config
        cfg = get_config()
        # Use capture.chunk_size (chars) and convert to approximate tokens
        chunk_chars = getattr(getattr(cfg, "capture", None), "chunk_size", 0)
        if chunk_chars and int(chunk_chars) > 0:
            return int(chunk_chars) // 4  # ~4 chars per token
    except Exception:
        pass
    return _DEFAULT_LLM_CHUNK_TOKENS


def _load_content(content_or_path: str) -> str:
    """Load content from a string or file path."""
    p = Path(content_or_path)
    if p.is_file():
        return p.read_text(encoding="utf-8")
    return content_or_path


def parallel_llm_call(
    system_prompt: str,
    content: str,
    chunk_prompt_template: str = "Process this content:\n\n{chunk}",
    max_chunk_tokens: Optional[int] = None,
    max_workers: int = 4,
    model_tier: str = "fast",
    max_response_tokens: int = 2000,
    timeout: float = 120.0,
) -> List[ChunkResult]:
    """Split content into chunks and process each through the LLM in parallel.

    Each chunk gets the same system prompt and is processed independently.
    Results are returned in chunk order.

    Args:
        system_prompt: Master instructions for the LLM.
        content: Text to process (or path to a file to read).
        chunk_prompt_template: Template for each chunk's user message.
            Must contain {chunk} placeholder. May also use {chunk_index}
            and {total_chunks}.
        max_chunk_tokens: Token budget per chunk. Defaults to config value.
        max_workers: Max concurrent LLM calls.
        model_tier: "fast" for Haiku, "deep" for Opus.
        max_response_tokens: Max tokens for each LLM response.
        timeout: Timeout per LLM call in seconds.

    Returns:
        List of ChunkResult with LLM responses.
    """
    from lib.llm_clients import call_fast_reasoning, call_deep_reasoning

    text = _load_content(content)
    chunk_tokens = max_chunk_tokens or _get_configured_chunk_tokens()
    chunks = chunk_text_by_tokens(text, max_tokens=chunk_tokens)

    if not chunks:
        return []

    total = len(chunks)
    call_fn = call_fast_reasoning if model_tier == "fast" else call_deep_reasoning

    def _process(chunk_text: str, idx: int) -> str:
        user_message = chunk_prompt_template.format(
            chunk=chunk_text,
            chunk_index=idx,
            total_chunks=total,
        )

        if model_tier == "fast":
            response, duration = call_fn(
                user_message,
                max_tokens=max_response_tokens,
                timeout=timeout,
                system_prompt=system_prompt,
            )
        else:
            response, duration = call_fn(
                prompt=user_message,
                system_prompt=system_prompt,
                max_tokens=max_response_tokens,
                timeout=timeout,
            )

        if not response:
            raise RuntimeError(f"Empty LLM response for chunk {idx}")
        return response

    from lib.batch_utils import parallel_batch
    # parallel_batch expects items as list of strings and joins them,
    # but we already have pre-joined chunks. Wrap each chunk as a single item.
    results = []
    if total == 1:
        try:
            output = _process(chunks[0], 0)
            results.append(ChunkResult(
                chunk_index=0, output=output,
                input_tokens=estimate_tokens(chunks[0]),
            ))
        except Exception as e:
            results.append(ChunkResult(
                chunk_index=0, output=None,
                input_tokens=estimate_tokens(chunks[0]),
                error=str(e),
            ))
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        ordered = [None] * total
        with ThreadPoolExecutor(max_workers=min(max_workers, total)) as pool:
            futures = {
                pool.submit(_process, chunk, i): i
                for i, chunk in enumerate(chunks)
            }
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    output = future.result()
                    ordered[idx] = ChunkResult(
                        chunk_index=idx, output=output,
                        input_tokens=estimate_tokens(chunks[idx]),
                    )
                except Exception as e:
                    logger.error("[parallel-llm] Chunk %d failed: %s", idx, e)
                    ordered[idx] = ChunkResult(
                        chunk_index=idx, output=None,
                        input_tokens=estimate_tokens(chunks[idx]),
                        error=str(e),
                    )
        results = [r for r in ordered if r is not None]

    logger.info(
        "[parallel-llm] Processed %d chunks (%d succeeded, %d failed)",
        total,
        sum(1 for r in results if r.error is None),
        sum(1 for r in results if r.error is not None),
    )
    return results


def waterfall_llm_call(
    system_prompt: str,
    content: str,
    chunk_prompt_template: str = (
        "Process this content. Previous context:\n{carryover}\n\n"
        "New content:\n{chunk}"
    ),
    carryover_prompt: str = (
        "Distill your analysis into a concise summary that captures "
        "all key findings. This will be carried forward as context."
    ),
    initial_carryover: str = "",
    max_chunk_tokens: Optional[int] = None,
    model_tier: str = "deep",
    max_response_tokens: int = 4000,
    timeout: float = 300.0,
) -> str:
    """Process content through the LLM in serial waterfall batches.

    Each chunk is sent with the carryover from the previous chunk's
    distilled output. The final carryover is the result.

    Args:
        system_prompt: Master instructions for the LLM.
        content: Text to process (or path to a file to read).
        chunk_prompt_template: Template for each chunk's user message.
            Must contain {chunk} and {carryover} placeholders.
            May also use {chunk_index} and {total_chunks}.
        carryover_prompt: Added to system prompt to instruct the LLM
            to distill its output for carryover. Only used when there
            are multiple chunks.
        initial_carryover: Starting context for the first chunk.
        max_chunk_tokens: Token budget per chunk. Defaults to config value.
        model_tier: "fast" for Haiku, "deep" for Opus.
        max_response_tokens: Max tokens for each LLM response.
        timeout: Timeout per LLM call in seconds.

    Returns:
        Final distilled output from the last chunk.
    """
    from lib.llm_clients import call_fast_reasoning, call_deep_reasoning

    text = _load_content(content)
    chunk_tokens = max_chunk_tokens or _get_configured_chunk_tokens()
    chunks = chunk_text_by_tokens(text, max_tokens=chunk_tokens)

    if not chunks:
        return initial_carryover

    total = len(chunks)
    call_fn = call_fast_reasoning if model_tier == "fast" else call_deep_reasoning

    # If multiple chunks, augment system prompt with carryover instructions
    effective_system = system_prompt
    if total > 1 and carryover_prompt:
        effective_system = f"{system_prompt}\n\n{carryover_prompt}"

    carryover = initial_carryover

    for i, chunk in enumerate(chunks):
        user_message = chunk_prompt_template.format(
            chunk=chunk,
            carryover=carryover or "(none)",
            chunk_index=i,
            total_chunks=total,
        )

        try:
            if model_tier == "fast":
                response, duration = call_fn(
                    user_message,
                    max_tokens=max_response_tokens,
                    timeout=timeout,
                    system_prompt=effective_system,
                )
            else:
                response, duration = call_fn(
                    prompt=user_message,
                    system_prompt=effective_system,
                    max_tokens=max_response_tokens,
                    timeout=timeout,
                )

            if response:
                carryover = response
                logger.info(
                    "[waterfall-llm] Chunk %d/%d processed (%.1fs, %d tokens out)",
                    i + 1, total, duration, estimate_tokens(response),
                )
            else:
                logger.warning(
                    "[waterfall-llm] Chunk %d/%d: empty response (%.1fs), keeping carryover",
                    i + 1, total, duration,
                )
        except Exception as e:
            logger.error("[waterfall-llm] Chunk %d/%d failed: %s", i + 1, total, e)
            # Continue with existing carryover — don't lose previous work

    return carryover


def merge_parallel_results(
    results: List[ChunkResult],
    separator: str = "\n\n",
) -> str:
    """Merge parallel LLM results into a single string.

    Skips failed chunks and joins successful outputs.
    """
    parts = []
    for r in sorted(results, key=lambda r: r.chunk_index):
        if r.output and r.error is None:
            parts.append(str(r.output))
    return separator.join(parts)
