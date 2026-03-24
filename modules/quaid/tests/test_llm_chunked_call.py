"""Tests for lib/llm_chunked_call.py — LLM chunked call utilities."""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from lib.llm_chunked_call import (
    parallel_llm_call,
    waterfall_llm_call,
    merge_parallel_results,
    _load_content,
    _get_configured_chunk_tokens,
    _DEFAULT_LLM_CHUNK_TOKENS,
)
from lib.batch_utils import ChunkResult

# All LLM calls are lazy imports, so patch at the source module
_FAST = "lib.llm_clients.call_fast_reasoning"
_DEEP = "lib.llm_clients.call_deep_reasoning"


class TestLoadContent:
    def test_loads_from_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("file content here")
        assert _load_content(str(f)) == "file content here"

    def test_returns_string_directly(self):
        assert _load_content("just a string") == "just a string"

    def test_nonexistent_path_treated_as_string(self):
        result = _load_content("/nonexistent/path/that/is/not/a/file.txt")
        assert result == "/nonexistent/path/that/is/not/a/file.txt"


class TestConfiguredChunkTokens:
    def test_prefers_capture_chunk_tokens(self):
        with patch("config.get_config") as mock_get_config:
            mock_get_config.return_value = MagicMock(capture=MagicMock(chunk_tokens=8000, chunk_size=12000))
            assert _get_configured_chunk_tokens() == 8000

    def test_uses_legacy_chunk_size_without_char_conversion(self):
        with patch("config.get_config") as mock_get_config:
            mock_get_config.return_value = MagicMock(capture=MagicMock(chunk_tokens=0, chunk_size=8000))
            assert _get_configured_chunk_tokens() == 8000


class TestParallelLlmCall:
    def test_single_chunk_no_threadpool(self):
        """Small content should process in a single chunk without threading."""
        with patch(_FAST) as mock_call:
            mock_call.return_value = ("result text", 1.0)

            results = parallel_llm_call(
                system_prompt="Analyze this",
                content="short text",
                max_chunk_tokens=1000,
            )

            assert len(results) == 1
            assert results[0].output == "result text"
            assert results[0].error is None
            mock_call.assert_called_once()

    def test_multiple_chunks_processed(self):
        """Large content should be split and all chunks processed."""
        content = "\n".join([f"line {i} " + "x" * 200 for i in range(50)])

        call_count = [0]
        def mock_call(prompt, max_tokens=200, timeout=120, system_prompt=None):
            call_count[0] += 1
            return (f"result-{call_count[0]}", 0.5)

        with patch(_FAST, side_effect=mock_call):
            results = parallel_llm_call(
                system_prompt="Analyze",
                content=content,
                max_chunk_tokens=500,
            )

            assert len(results) > 1
            assert all(r.error is None for r in results)
            assert call_count[0] == len(results)

    def test_uses_deep_reasoning_when_specified(self):
        with patch(_DEEP) as mock_call:
            mock_call.return_value = ("deep result", 2.0)

            results = parallel_llm_call(
                system_prompt="Deep analysis",
                content="text",
                model_tier="deep",
                max_chunk_tokens=1000,
            )

            assert results[0].output == "deep result"
            mock_call.assert_called_once()

    def test_handles_llm_failure(self):
        with patch(_FAST) as mock_call:
            mock_call.return_value = (None, 1.0)

            results = parallel_llm_call(
                system_prompt="Analyze",
                content="text",
                max_chunk_tokens=1000,
            )

            assert len(results) == 1
            assert results[0].error is not None

    def test_chunk_prompt_template(self):
        with patch(_FAST) as mock_call:
            mock_call.return_value = ("ok", 0.5)

            parallel_llm_call(
                system_prompt="sys",
                content="hello world",
                chunk_prompt_template="Custom: {chunk} (part {chunk_index}/{total_chunks})",
                max_chunk_tokens=1000,
            )

            call_args = mock_call.call_args
            prompt = call_args[0][0]
            assert "Custom: hello world" in prompt
            assert "(part 0/1)" in prompt

    def test_reads_from_file(self, tmp_path):
        f = tmp_path / "data.txt"
        f.write_text("file data")

        with patch(_FAST) as mock_call:
            mock_call.return_value = ("processed", 0.5)

            results = parallel_llm_call(
                system_prompt="sys",
                content=str(f),
                max_chunk_tokens=1000,
            )

            assert results[0].output == "processed"
            call_args = mock_call.call_args
            assert "file data" in call_args[0][0]

    def test_empty_content(self):
        with patch(_FAST) as mock_call:
            mock_call.return_value = ("ok", 0.5)
            results = parallel_llm_call(
                system_prompt="sys",
                content="",
                max_chunk_tokens=1000,
            )
            # Empty string still produces one minimal chunk
            assert len(results) <= 1


class TestWaterfallLlmCall:
    def test_single_chunk(self):
        with patch(_DEEP) as mock_call:
            mock_call.return_value = ("analysis result", 2.0)

            result = waterfall_llm_call(
                system_prompt="Analyze",
                content="short text",
                max_chunk_tokens=1000,
            )

            assert result == "analysis result"
            mock_call.assert_called_once()

    def test_carryover_cascades(self):
        """Each chunk should receive the previous chunk's output as carryover."""
        content = "\n".join([f"section {i} " + "x" * 200 for i in range(30)])
        calls = []

        def mock_call(prompt, system_prompt=None, max_tokens=4000, timeout=300):
            calls.append(prompt)
            return (f"distilled-{len(calls)}", 1.0)

        with patch(_DEEP, side_effect=mock_call):
            result = waterfall_llm_call(
                system_prompt="Analyze",
                content=content,
                initial_carryover="start",
                max_chunk_tokens=500,
            )

        assert len(calls) > 1
        assert "start" in calls[0]
        assert "distilled-1" in calls[1]
        assert result == f"distilled-{len(calls)}"

    def test_initial_carryover(self):
        with patch(_DEEP) as mock_call:
            mock_call.return_value = ("result", 1.0)

            waterfall_llm_call(
                system_prompt="sys",
                content="text",
                initial_carryover="prior context",
                max_chunk_tokens=1000,
            )

            # deep_reasoning is called with keyword args
            prompt = mock_call.call_args.kwargs.get("prompt", "")
            assert "prior context" in prompt

    def test_error_preserves_carryover(self):
        content = "\n".join([f"line {i} " + "x" * 200 for i in range(30)])
        call_num = [0]

        def mock_call(prompt, system_prompt=None, max_tokens=4000, timeout=300):
            call_num[0] += 1
            if call_num[0] == 2:
                raise RuntimeError("LLM error")
            return (f"ok-{call_num[0]}", 1.0)

        with patch(_DEEP, side_effect=mock_call):
            result = waterfall_llm_call(
                system_prompt="sys",
                content=content,
                max_chunk_tokens=500,
            )

        # Chunk 2 failed, so carryover from chunk 1 should persist
        assert "ok-1" in result or result.startswith("ok-")

    def test_empty_content_returns_initial(self):
        result = waterfall_llm_call(
            system_prompt="sys",
            content="",
            initial_carryover="init",
            max_chunk_tokens=1000,
        )
        assert result == "init"

    def test_uses_fast_reasoning_when_specified(self):
        with patch(_FAST) as mock_call:
            mock_call.return_value = ("fast result", 0.5)

            result = waterfall_llm_call(
                system_prompt="sys",
                content="text",
                model_tier="fast",
                max_chunk_tokens=1000,
            )

            assert result == "fast result"
            mock_call.assert_called_once()


class TestMergeParallelResults:
    def test_merges_in_order(self):
        results = [
            ChunkResult(chunk_index=2, output="third"),
            ChunkResult(chunk_index=0, output="first"),
            ChunkResult(chunk_index=1, output="second"),
        ]
        merged = merge_parallel_results(results)
        assert merged == "first\n\nsecond\n\nthird"

    def test_skips_errors(self):
        results = [
            ChunkResult(chunk_index=0, output="ok"),
            ChunkResult(chunk_index=1, output=None, error="failed"),
            ChunkResult(chunk_index=2, output="also ok"),
        ]
        merged = merge_parallel_results(results)
        assert merged == "ok\n\nalso ok"

    def test_custom_separator(self):
        results = [
            ChunkResult(chunk_index=0, output="a"),
            ChunkResult(chunk_index=1, output="b"),
        ]
        assert merge_parallel_results(results, separator="|") == "a|b"

    def test_empty_results(self):
        assert merge_parallel_results([]) == ""
