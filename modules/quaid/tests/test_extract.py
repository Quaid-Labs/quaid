"""Tests for extract.py — Memory extraction from conversation transcripts."""

import itertools
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Ensure modules/quaid is on the path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Set env before imports
os.environ.setdefault("MEMORY_DB_PATH", ":memory:")
os.environ.setdefault("QUAID_QUIET", "1")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def workspace_dir(tmp_path):
    """Create a temporary workspace for each test."""
    from lib.adapter import set_adapter, reset_adapter, TestAdapter
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)
    iroot = adapter.instance_root()

    os.environ["CLAWDBOT_WORKSPACE"] = str(iroot)
    os.environ["MOCK_EMBEDDINGS"] = "1"

    # Create required directories
    (iroot / "journal").mkdir(exist_ok=True)
    (iroot / "config").mkdir(exist_ok=True)

    # Create minimal config
    config = {
        "models": {"deepReasoning": "claude-opus-4-6", "fastReasoning": "claude-haiku-4-5"},
        "users": {"defaultOwner": "test-user"},
        "docs": {
            "journal": {
                "enabled": True,
                "snippetsEnabled": True,
                "targetFiles": ["SOUL.md", "USER.md", "ENVIRONMENT.md"],
                "journalDir": "journal",
                "maxEntriesPerFile": 50,
            }
        },
    }
    (iroot / "config" / "memory.json").write_text(json.dumps(config))

    yield iroot

    reset_adapter()
    if "CLAWDBOT_WORKSPACE" in os.environ:
        del os.environ["CLAWDBOT_WORKSPACE"]
    if "MOCK_EMBEDDINGS" in os.environ:
        del os.environ["MOCK_EMBEDDINGS"]


@pytest.fixture
def mock_opus_response():
    """Standard Opus extraction response."""
    return json.dumps({
        "chunk_assessment": "usable",
        "facts": [
            {
                "text": "Test user likes coffee",
                "category": "preference",
                "domains": ["personal"],
                "extraction_confidence": "high",
                "keywords": "beverage drink caffeine morning",
                "privacy": "shared",
                "confidence_reason": "Explicitly stated",
                "edges": [],
            },
            {
                "text": "Test user's sister lives in Portland",
                "category": "fact",
                "domains": ["personal"],
                "extraction_confidence": "medium",
                "keywords": "family sibling location oregon",
                "privacy": "shared",
                "confidence_reason": "Mentioned in passing",
                "edges": [
                    {"subject": "Sister", "relation": "lives_at", "object": "Portland"}
                ],
            },
        ],
        "soul_snippets": {
            "SOUL.md": ["Noticed the user values brevity"],
            "USER.md": [],
            "ENVIRONMENT.md": [],
        },
        "journal_entries": {
            "SOUL.md": "A quiet conversation today.",
            "USER.md": "",
            "ENVIRONMENT.md": "",
        },
    })


# ---------------------------------------------------------------------------
# build_transcript tests
# ---------------------------------------------------------------------------

class TestBuildTranscript:
    def test_basic_transcript(self):
        from ingest.extract import build_transcript

        messages = [
            {"role": "user", "content": "Hello there"},
            {"role": "assistant", "content": "Hi! How can I help?"},
        ]
        result = build_transcript(messages)
        assert "User: Hello there" in result
        assert "Assistant: Hi! How can I help?" in result

    def test_filters_system_messages(self):
        from ingest.extract import build_transcript

        messages = [
            {"role": "system", "content": "You are a helpful assistant"},
            {"role": "user", "content": "Hi"},
        ]
        result = build_transcript(messages)
        assert "system" not in result.lower() or "User: Hi" in result
        assert "User: Hi" in result

    def test_does_not_filter_gateway_restart_in_standalone(self):
        from ingest.extract import build_transcript

        messages = [
            {"role": "user", "content": "GatewayRestart: reloaded"},
            {"role": "user", "content": "Hello"},
        ]
        result = build_transcript(messages)
        assert "GatewayRestart" in result
        assert "User: Hello" in result

    def test_does_not_filter_heartbeat_in_standalone(self):
        from ingest.extract import build_transcript

        messages = [
            {"role": "user", "content": "HEARTBEAT check HEARTBEAT_OK"},
            {"role": "user", "content": "Real message"},
        ]
        result = build_transcript(messages)
        assert "HEARTBEAT" in result
        assert "User: Real message" in result

    def test_strips_channel_prefix(self):
        from ingest.extract import build_transcript

        messages = [
            {"role": "user", "content": "[Telegram user@12345] Hello"},
        ]
        result = build_transcript(messages)
        assert result == "User: Hello"

    def test_strips_message_id(self):
        from ingest.extract import build_transcript

        messages = [
            {"role": "user", "content": "Hello\n[message_id: 42]"},
        ]
        result = build_transcript(messages)
        assert "message_id" not in result
        assert "User: Hello" in result

    def test_empty_messages(self):
        from ingest.extract import build_transcript
        assert build_transcript([]) == ""

    def test_skips_empty_content(self):
        from ingest.extract import build_transcript

        messages = [
            {"role": "user", "content": ""},
            {"role": "assistant", "content": "Reply"},
        ]
        result = build_transcript(messages)
        assert result == "Assistant: Reply"


# ---------------------------------------------------------------------------
# parse_session_jsonl tests
# ---------------------------------------------------------------------------

class TestParseSessionJsonl:
    def test_direct_format(self, tmp_path):
        from ingest.extract import parse_session_jsonl

        jsonl_file = tmp_path / "session.jsonl"
        lines = [
            json.dumps({"role": "user", "content": "My birthday is March 15"}),
            json.dumps({"role": "assistant", "content": "I'll remember that!"}),
        ]
        jsonl_file.write_text("\n".join(lines))

        result = parse_session_jsonl(str(jsonl_file))
        assert "User: My birthday is March 15" in result
        assert "Assistant: I'll remember that!" in result

    def test_wrapped_format(self, tmp_path):
        from ingest.extract import parse_session_jsonl

        jsonl_file = tmp_path / "session.jsonl"
        lines = [
            json.dumps({"type": "message", "message": {"role": "user", "content": "Hello"}}),
            json.dumps({"type": "message", "message": {"role": "assistant", "content": "Hi"}}),
        ]
        jsonl_file.write_text("\n".join(lines))

        result = parse_session_jsonl(str(jsonl_file))
        assert "User: Hello" in result
        assert "Assistant: Hi" in result

    def test_multi_part_content(self, tmp_path):
        from ingest.extract import parse_session_jsonl

        jsonl_file = tmp_path / "session.jsonl"
        lines = [
            json.dumps({"role": "user", "content": [{"text": "Part 1"}, {"text": "Part 2"}]}),
        ]
        jsonl_file.write_text("\n".join(lines))

        result = parse_session_jsonl(str(jsonl_file))
        assert "Part 1 Part 2" in result

    def test_skips_non_message_lines(self, tmp_path):
        from ingest.extract import parse_session_jsonl

        jsonl_file = tmp_path / "session.jsonl"
        lines = [
            json.dumps({"type": "system", "info": "startup"}),
            json.dumps({"role": "user", "content": "Hello"}),
            "not json at all",
            "",
        ]
        jsonl_file.write_text("\n".join(lines))

        result = parse_session_jsonl(str(jsonl_file))
        assert "User: Hello" in result

    def test_empty_file(self, tmp_path):
        from ingest.extract import parse_session_jsonl

        jsonl_file = tmp_path / "session.jsonl"
        jsonl_file.write_text("")

        result = parse_session_jsonl(str(jsonl_file))
        assert result == ""


# ---------------------------------------------------------------------------
# extract_from_transcript tests
# ---------------------------------------------------------------------------

class TestExtractFromTranscript:
    def test_extract_wall_timeout_defaults_to_2400(self, monkeypatch):
        from ingest.extract import _get_extract_wall_timeout_seconds

        monkeypatch.delenv("QUAID_EXTRACT_WALL_TIMEOUT", raising=False)
        assert _get_extract_wall_timeout_seconds() == 2400.0

    def test_extract_wall_timeout_respects_env_override(self, monkeypatch):
        from ingest.extract import _get_extract_wall_timeout_seconds

        monkeypatch.setenv("QUAID_EXTRACT_WALL_TIMEOUT", "7200")
        assert _get_extract_wall_timeout_seconds() == 7200.0

    def test_extract_carry_and_parallel_env_helpers(self, monkeypatch):
        from ingest.extract import (
            _extract_carry_context_enabled,
            _get_extract_parallel_root_workers,
        )

        monkeypatch.delenv("QUAID_EXTRACT_DISABLE_CARRY_CONTEXT", raising=False)
        monkeypatch.delenv("QUAID_EXTRACT_PARALLEL_ROOT_WORKERS", raising=False)
        assert _extract_carry_context_enabled() is True
        assert _get_extract_parallel_root_workers() == 1

        monkeypatch.setenv("QUAID_EXTRACT_DISABLE_CARRY_CONTEXT", "1")
        monkeypatch.setenv("QUAID_EXTRACT_PARALLEL_ROOT_WORKERS", "4")
        assert _extract_carry_context_enabled() is False
        assert _get_extract_parallel_root_workers() == 4

    def test_empty_transcript(self):
        from ingest.extract import extract_from_transcript

        result = extract_from_transcript("", owner_id="test")
        assert result["facts_stored"] == 0
        assert result["facts_skipped"] == 0

    def test_empty_whitespace_transcript(self):
        from ingest.extract import extract_from_transcript

        result = extract_from_transcript("   \n  \n  ", owner_id="test")
        assert result["facts_stored"] == 0

    @patch("ingest.extract.call_deep_reasoning")
    @patch("ingest.extract.get_config")
    def test_capture_disabled_skips_extraction(self, mock_get_config, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_get_config.return_value = SimpleNamespace(
            capture=SimpleNamespace(enabled=False, chunk_tokens=8000)
        )
        result = extract_from_transcript(
            transcript="User: remember this detail\n\nAssistant: ok",
            owner_id="test",
        )

        assert result["facts_stored"] == 0
        assert result["facts_skipped"] == 0
        mock_llm.assert_not_called()

    @patch("ingest.extract.call_deep_reasoning")
    @patch("ingest.extract.get_config")
    def test_capture_skip_patterns_can_filter_transcript(self, mock_get_config, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_get_config.return_value = SimpleNamespace(
            capture=SimpleNamespace(enabled=True, chunk_tokens=8000, skip_patterns=[r"HEARTBEAT"])
        )
        result = extract_from_transcript(
            transcript="HEARTBEAT ping\nHEARTBEAT_OK",
            owner_id="test",
        )

        assert result["facts_stored"] == 0
        assert result["facts_skipped"] == 0
        mock_llm.assert_not_called()

    @patch("ingest.extract.call_deep_reasoning")
    @patch("ingest.extract._memory.store")
    @patch("ingest.extract._memory.create_edge")
    def test_basic_extraction(self, mock_edge, mock_store, mock_llm, mock_opus_response, workspace_dir):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (mock_opus_response, 2.5)
        mock_store.return_value = {"id": "node-1", "status": "created"}
        mock_edge.return_value = {"status": "created"}

        result = extract_from_transcript(
            transcript="User: I like coffee\n\nAssistant: Got it!",
            owner_id="test",
            label="test",
        )

        assert result["facts_stored"] == 2
        assert result["edges_created"] == 1
        assert len(result["facts"]) == 2
        assert result["facts"][0]["status"] == "stored"

    @patch("ingest.extract.call_deep_reasoning")
    @patch("ingest.extract._memory.store")
    def test_dry_run(self, mock_store, mock_llm, mock_opus_response):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (mock_opus_response, 1.0)

        result = extract_from_transcript(
            transcript="User: test\n\nAssistant: ok",
            owner_id="test",
            dry_run=True,
        )

        assert result["dry_run"] is True
        assert result["facts_stored"] == 2
        mock_store.assert_not_called()

    @patch("ingest.extract.call_deep_reasoning")
    @patch("ingest.extract._memory.store")
    def test_duplicate_handling(self, mock_store, mock_llm, mock_opus_response):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (mock_opus_response, 1.0)
        mock_store.return_value = {"status": "duplicate", "existing_text": "Already stored"}

        result = extract_from_transcript(
            transcript="User: test\n\nAssistant: ok",
            owner_id="test",
        )

        assert result["facts_skipped"] == 2
        assert result["facts_stored"] == 0

    @patch("ingest.extract.call_deep_reasoning")
    def test_no_response(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (None, 1.0)

        result = extract_from_transcript(
            transcript="User: test\n\nAssistant: ok",
            owner_id="test",
        )

        assert result["facts_stored"] == 0

    @patch("ingest.extract.call_deep_reasoning")
    def test_unparseable_response(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = ("This is not JSON at all", 1.0)

        result = extract_from_transcript(
            transcript="User: test\n\nAssistant: ok",
            owner_id="test",
        )

        assert result["facts_stored"] == 0

    @patch("ingest.extract.call_fast_reasoning")
    @patch("ingest.extract.call_deep_reasoning")
    @patch("ingest.extract._memory.store")
    def test_unparseable_response_uses_json_repair(self, mock_store, mock_llm, mock_fast):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = ("I remembered that your mother is Wendy.", 1.0)
        mock_fast.return_value = (json.dumps({
            "facts": [
                {
                    "text": "User's mother is Wendy",
                    "category": "fact",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                }
            ],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.5)
        mock_store.return_value = {"id": "n1", "status": "created"}

        result = extract_from_transcript(
            transcript="User: my mother is Wendy\n\nAssistant: got it",
            owner_id="test",
        )

        assert result["facts_stored"] == 1
        mock_fast.assert_called_once()

    @patch("ingest.extract.call_deep_reasoning")
    def test_explicit_nothing_usable_payload_counts_as_processed(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "nothing_usable",
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.8)

        result = extract_from_transcript(
            transcript="User: here is a long filler discussion about generic cooking tips\n\nAssistant: noted",
            owner_id="test",
            dry_run=True,
        )

        assert result["facts_stored"] == 0
        assert result["chunks_processed"] == 1
        assert result["chunks_total"] == 1
        assert result["assessment_nothing_usable"] == 1
        assert result["assessment_usable"] == 0
        assert result["assessment_needs_smaller_chunk"] == 0

    @patch("ingest.extract.call_deep_reasoning")
    def test_dry_run_exposes_raw_payloads_and_carry_facts(self, mock_llm, mock_opus_response):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (mock_opus_response, 0.9)

        result = extract_from_transcript(
            transcript="User: I like coffee\n\nAssistant: noted",
            owner_id="test",
            dry_run=True,
        )

        assert len(result["raw_facts"]) == 2
        assert len(result["carry_facts"]) == 2
        assert result["raw_snippets"]["SOUL.md"] == ["Noticed the user values brevity"]

    def test_carry_selection_is_bounded_and_persistable(self):
        from ingest.extract import _select_carry_facts, _persistable_carry_facts

        facts = [
            {
                "text": f"Maya fact number {i} with value {i}:00 and project recipe-app",
                "category": "fact",
                "speaker": "user",
                "extraction_confidence": "high" if i % 5 == 0 else "medium",
                "project": "recipe-app" if i % 3 == 0 else "",
            }
            for i in range(60)
        ]

        selected = _select_carry_facts(facts, max_items=40, max_chars=4000)
        persisted = _persistable_carry_facts(selected)

        assert len(selected) <= 40
        assert len(persisted) == len(selected)
        assert persisted
        assert all("_carry_bucket" in fact for fact in selected)
        assert all("_carry_bucket" not in fact for fact in persisted)

    @patch("ingest.extract.call_deep_reasoning")
    @patch("ingest.extract._memory.store")
    @patch("ingest.extract._memory.create_edge")
    def test_apply_extracted_payloads_can_publish_prior_dry_run_result(self, mock_edge, mock_store, mock_llm, mock_opus_response):
        from ingest.extract import apply_extracted_payloads, extract_from_transcript

        mock_llm.return_value = (mock_opus_response, 1.0)
        mock_store.side_effect = [
            {
                "id": "node-1",
                "status": "created",
                "dedup_telemetry": {
                    "hash_exact_hits": 0,
                    "scanned_rows": 4,
                    "gray_zone_rows": 2,
                    "llm_checks": 2,
                    "llm_same_hits": 1,
                    "llm_different_hits": 1,
                    "fallback_reject_hits": 0,
                    "auto_reject_hits": 0,
                    "vec_query_count": 1,
                    "vec_candidates_returned": 4,
                    "vec_candidate_limit": 64,
                    "vec_limit_hits": 0,
                    "fts_query_count": 1,
                    "fts_candidates_returned": 4,
                    "fts_candidate_limit": 64,
                    "fts_limit_hits": 0,
                    "fallback_scan_count": 0,
                    "fallback_candidates_returned": 0,
                    "token_prefilter_terms": 6,
                    "token_prefilter_skips": 0,
                },
            },
            {
                "id": "node-2",
                "status": "created",
                "dedup_telemetry": {
                    "hash_exact_hits": 1,
                    "scanned_rows": 3,
                    "gray_zone_rows": 1,
                    "llm_checks": 1,
                    "llm_same_hits": 0,
                    "llm_different_hits": 1,
                    "fallback_reject_hits": 0,
                    "auto_reject_hits": 1,
                    "vec_query_count": 1,
                    "vec_candidates_returned": 3,
                    "vec_candidate_limit": 64,
                    "vec_limit_hits": 0,
                    "fts_query_count": 1,
                    "fts_candidates_returned": 3,
                    "fts_candidate_limit": 64,
                    "fts_limit_hits": 0,
                    "fallback_scan_count": 0,
                    "fallback_candidates_returned": 0,
                    "token_prefilter_terms": 5,
                    "token_prefilter_skips": 0,
                },
            },
        ]
        mock_edge.return_value = {"status": "created"}

        staged = extract_from_transcript(
            transcript="User: I like coffee\n\nAssistant: noted",
            owner_id="test",
            label="stage",
            session_id="sess-stage",
            dry_run=True,
        )

        staged["facts_stored"] = 0
        staged["facts_skipped"] = 0
        staged["edges_created"] = 0
        staged["facts"] = []
        staged["snippets"] = {}
        staged["journal"] = {}
        staged["project_logs"] = {}
        staged["project_log_metrics"] = {}
        staged["dry_run"] = False

        applied = apply_extracted_payloads(
            staged,
            owner_id="test",
            label="flush",
            session_id="sess-stage",
            dry_run=False,
        )

        assert applied["facts_stored"] == 2
        assert applied["edges_created"] == 1
        assert applied["dedup_hash_exact_hits"] == 1
        assert applied["dedup_scanned_rows"] == 7
        assert applied["dedup_gray_zone_rows"] == 3
        assert applied["dedup_llm_checks"] == 3
        assert applied["dedup_llm_same_hits"] == 1
        assert applied["dedup_llm_different_hits"] == 2
        assert applied["dedup_auto_reject_hits"] == 1
        assert applied["dedup_vec_query_count"] == 2
        assert applied["dedup_vec_candidates_returned"] == 7
        assert applied["dedup_vec_candidate_limit"] == 64
        assert applied["dedup_vec_limit_hits"] == 0
        assert applied["dedup_fts_query_count"] == 2
        assert applied["dedup_fts_candidates_returned"] == 7
        assert applied["dedup_fts_candidate_limit"] == 64
        assert applied["dedup_fts_limit_hits"] == 0
        assert applied["dedup_fallback_scan_count"] == 0
        assert applied["dedup_fallback_candidates_returned"] == 0
        assert applied["dedup_token_prefilter_terms"] == 11
        assert applied["dedup_token_prefilter_skips"] == 0
        assert mock_store.call_count == 2
        first_call = mock_store.call_args_list[0].kwargs
        second_call = mock_store.call_args_list[1].kwargs
        assert first_call["confidence"] == pytest.approx(0.9)
        assert first_call["extraction_confidence"] == pytest.approx(0.9)
        assert first_call["provenance_confidence"] == pytest.approx(0.9)
        assert second_call["confidence"] == pytest.approx(0.6)
        assert second_call["extraction_confidence"] == pytest.approx(0.6)
        assert second_call["provenance_confidence"] == pytest.approx(0.6)

    @patch("ingest.extract._memory.store")
    def test_apply_extracted_payloads_collapses_exact_duplicate_fact_rows(self, mock_store):
        from ingest.extract import apply_extracted_payloads

        mock_store.return_value = {"id": "n1", "status": "created", "dedup_telemetry": {}}

        payload = {
            "raw_facts": [
                {
                    "text": "Maya's half marathon finish time was 2:14",
                    "category": "fact",
                    "domains": ["personal"],
                    "extraction_confidence": "medium",
                    "keywords": "half marathon time",
                },
                {
                    "text": "  Maya's half marathon finish time was 2:14  ",
                    "category": "fact",
                    "domains": ["health", "personal"],
                    "extraction_confidence": "high",
                    "keywords": "running exact time",
                    "edges": [{"subject": "Maya", "relation": "ran_time", "object": "2:14"}],
                },
            ],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "facts": [],
            "snippets": {},
            "journal": {},
            "project_logs": {},
            "project_log_metrics": {},
            "facts_stored": 0,
            "facts_skipped": 0,
            "edges_created": 0,
            "dry_run": False,
        }

        applied = apply_extracted_payloads(
            payload,
            owner_id="test",
            label="flush",
            session_id="sess-dupe",
            dry_run=False,
        )

        assert applied["payload_duplicate_facts_collapsed"] == 1
        assert applied["facts_stored"] == 1
        assert mock_store.call_count == 1
        call = mock_store.call_args.kwargs
        assert call["text"] == "  Maya's half marathon finish time was 2:14  " or call["text"] == "Maya's half marathon finish time was 2:14"
        assert call["confidence"] == pytest.approx(0.9)
        assert call["extraction_confidence"] == pytest.approx(0.9)
        assert call["provenance_confidence"] == pytest.approx(0.9)
        assert sorted(call["domains"]) == ["health", "personal"]

    @patch("ingest.extract.call_deep_reasoning")
    @patch("ingest.extract._memory.store")
    def test_skips_short_facts(self, mock_store, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "facts": [
                {"text": "hi", "category": "fact"},
                {"text": "User likes coffee very much", "category": "preference", "domains": ["personal"]},
            ]
        }), 1.0)
        mock_store.return_value = {"id": "n1", "status": "created"}

        result = extract_from_transcript(
            transcript="User: test\n\nAssistant: ok",
            owner_id="test",
        )

        assert result["facts_skipped"] == 1
        assert result["facts_stored"] == 1

    @patch("ingest.extract.call_deep_reasoning")
    @patch("ingest.extract._memory.store")
    def test_confidence_mapping(self, mock_store, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "facts": [
                {"text": "User likes coffee a lot", "extraction_confidence": "high", "domains": ["personal"]},
                {"text": "User might enjoy tea sometimes", "extraction_confidence": "low", "domains": ["personal"]},
            ]
        }), 1.0)
        mock_store.return_value = {"id": "n1", "status": "created"}

        result = extract_from_transcript(
            transcript="User: test\n\nAssistant: ok",
            owner_id="test",
        )

        # Check that store was called with proper confidence values
        calls = mock_store.call_args_list
        assert calls[0].kwargs["confidence"] == 0.9  # high
        assert calls[1].kwargs["confidence"] == 0.3  # low

    @patch("ingest.extract._memory.store")
    def test_apply_extracted_payloads_prewarms_embeddings_before_publish(self, mock_store):
        from ingest.extract import apply_extracted_payloads

        warmed = []
        mock_store.return_value = {"id": "n1", "status": "created", "dedup_telemetry": {}}

        payload = {
            "raw_facts": [
                {
                    "text": "Maya mentioned dietary tagging for the recipe app",
                    "category": "fact",
                    "domains": ["project"],
                    "extraction_confidence": "high",
                },
                {
                    "text": "Maya's birthday dinner is planned for May 18",
                    "category": "fact",
                    "domains": ["personal"],
                    "extraction_confidence": "medium",
                },
            ],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "facts": [],
            "snippets": {},
            "journal": {},
            "project_logs": {},
            "project_log_metrics": {},
            "facts_stored": 0,
            "facts_skipped": 0,
            "edges_created": 0,
            "dry_run": False,
        }

        with patch(
            "ingest.extract._memory.warm_embeddings",
            side_effect=lambda texts: warmed.append(list(texts)) or {
                "requested": len(texts),
                "unique": len(set(texts)),
                "cache_hits": 0,
                "warmed": len(set(texts)),
                "failed": 0,
            },
        ):
            applied = apply_extracted_payloads(
                payload,
                owner_id="test",
                label="rolling-flush",
                session_id="sess-embed",
                dry_run=False,
            )

        assert warmed == [[
            "Maya mentioned dietary tagging for the recipe app",
            "Maya's birthday dinner is planned for May 18",
        ]]
        assert applied["embedding_cache_requested"] == 2
        assert applied["embedding_cache_unique"] == 2
        assert applied["embedding_cache_warmed"] == 2

    @patch("ingest.extract._memory.create_edge", return_value={"status": "created"})
    @patch("ingest.extract._memory.store", return_value={"id": "n1", "status": "created", "dedup_telemetry": {}})
    def test_apply_extracted_payloads_prewarms_edge_entity_embeddings(self, _mock_store, _mock_edge):
        from ingest.extract import apply_extracted_payloads

        warmed = []
        payload = {
            "raw_facts": [
                {
                    "text": "maya currently lives in South Austin",
                    "category": "fact",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                    "edges": [{"subject": "maya", "relation": "lives_at", "object": "Austin"}],
                },
                {
                    "text": "maya works as a product manager at a company called TechFlow",
                    "category": "fact",
                    "domains": ["project"],
                    "extraction_confidence": "high",
                    "edges": [{"subject": "maya", "relation": "works_at", "object": "TechFlow"}],
                },
            ],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "facts": [],
            "snippets": {},
            "journal": {},
            "project_logs": {},
            "project_log_metrics": {},
            "facts_stored": 0,
            "facts_skipped": 0,
            "edges_created": 0,
            "dry_run": False,
        }

        with patch(
            "ingest.extract._memory.warm_embeddings",
            side_effect=lambda texts: warmed.append(list(texts)) or {
                "requested": len(texts),
                "unique": len(set(texts)),
                "cache_hits": 0,
                "warmed": len(set(texts)),
                "failed": 0,
            },
        ):
            applied = apply_extracted_payloads(
                payload,
                owner_id="test",
                label="rolling-flush",
                session_id="sess-edge-embed",
                dry_run=False,
            )

        assert warmed == [
            [
                "maya currently lives in South Austin",
                "maya works as a product manager at a company called TechFlow",
            ],
            [
                "maya",
                "Austin",
                "maya",
                "TechFlow",
            ],
        ]
        assert applied["embedding_cache_requested"] == 2
        assert applied["embedding_cache_unique"] == 2
        assert applied["edge_embedding_cache_requested"] == 4
        assert applied["edge_embedding_cache_unique"] == 3
        assert applied["edge_embedding_cache_warmed"] == 3

    @patch("ingest.extract._memory.create_edge")
    @patch("ingest.extract._memory.store")
    def test_apply_extracted_payloads_uses_shared_batch_write_connection(self, mock_store, mock_edge):
        from ingest.extract import apply_extracted_payloads

        read_conn = MagicMock()
        read_conn.execute.return_value.fetchone.return_value = (0,)
        shared_conn = MagicMock()
        seen_store = []
        seen_edge = []
        entered = []

        def shared_execute(sql, *_args, **_kwargs):
            res = MagicMock()
            if "MAX(rowid)" in sql:
                res.fetchone.return_value = (0,)
            return res

        shared_conn.execute.side_effect = shared_execute

        conns = iter([read_conn, shared_conn])

        @contextmanager
        def fake_batch_write():
            conn = next(conns)
            entered.append(conn)
            yield conn

        def fake_store(**kwargs):
            seen_store.append(kwargs.get("_conn"))
            return {"id": "fact-1", "status": "created", "dedup_telemetry": {}}

        def fake_edge(**kwargs):
            seen_edge.append(kwargs.get("_conn"))
            return {"status": "created"}

        mock_store.side_effect = fake_store
        mock_edge.side_effect = fake_edge

        payload = {
            "raw_facts": [
                {
                    "text": "Maya's birthday dinner is planned for May 18",
                    "category": "fact",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                    "edges": [{"subject": "Maya", "relation": "plans", "object": "birthday dinner"}],
                },
            ],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "facts": [],
            "snippets": {},
            "journal": {},
            "project_logs": {},
            "project_log_metrics": {},
            "facts_stored": 0,
            "facts_skipped": 0,
            "edges_created": 0,
            "dry_run": False,
        }

        with patch("ingest.extract._memory.batch_write", side_effect=fake_batch_write):
            applied = apply_extracted_payloads(
                payload,
                owner_id="test",
                label="rolling-flush",
                session_id="sess-batch-write",
                dry_run=False,
            )

        assert applied["facts_stored"] == 1
        assert applied["edges_created"] == 1
        assert entered == [read_conn, shared_conn]
        assert shared_conn.execute.call_args_list[0].args[0] == "BEGIN IMMEDIATE"
        assert seen_store == [shared_conn]
        assert seen_edge == [shared_conn]

    @patch("ingest.extract._memory.store")
    def test_apply_extracted_payloads_splits_publish_into_bounded_batches(self, mock_store):
        from ingest.extract import apply_extracted_payloads

        entered = []
        read_conn = MagicMock()
        read_conn.execute.return_value.fetchone.return_value = (0,)
        conn_a = MagicMock()
        conn_b = MagicMock()

        def make_write_execute(max_rowid):
            def _execute(sql, *_args, **_kwargs):
                res = MagicMock()
                if "MAX(rowid)" in sql:
                    res.fetchone.return_value = (max_rowid,)
                return res
            return _execute

        conn_a.execute.side_effect = make_write_execute(0)
        conn_b.execute.side_effect = make_write_execute(0)
        conns = iter([read_conn, conn_a, conn_b])

        @contextmanager
        def fake_batch_write():
            conn = next(conns)
            entered.append(conn)
            yield conn

        seen_store = []

        def fake_store(**kwargs):
            seen_store.append(kwargs.get("_conn"))
            return {"id": f"fact-{len(seen_store)}", "status": "created", "dedup_telemetry": {}}

        mock_store.side_effect = fake_store

        payload = {
            "raw_facts": [
                {
                    "text": "Maya's birthday dinner is planned for May 18",
                    "category": "fact",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                },
                {
                    "text": "Maya wants dietary tagging in the recipe app",
                    "category": "fact",
                    "domains": ["project"],
                    "extraction_confidence": "high",
                },
            ],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "facts": [],
            "snippets": {},
            "journal": {},
            "project_logs": {},
            "project_log_metrics": {},
            "facts_stored": 0,
            "facts_skipped": 0,
            "edges_created": 0,
            "dry_run": False,
        }

        with patch("ingest.extract._memory.batch_write", side_effect=fake_batch_write), \
             patch("ingest.extract._get_extract_publish_batch_size", return_value=1):
            applied = apply_extracted_payloads(
                payload,
                owner_id="test",
                label="rolling-flush",
                session_id="sess-batched-publish",
                dry_run=False,
            )

        assert applied["facts_stored"] == 2
        assert applied["publish_batches"] == 2
        assert entered == [read_conn, conn_a, conn_b]
        assert conn_a.execute.call_args_list[0].args[0] == "BEGIN IMMEDIATE"
        assert conn_b.execute.call_args_list[0].args[0] == "BEGIN IMMEDIATE"
        assert seen_store == [conn_a, conn_b]

    @patch("ingest.extract._memory.store")
    def test_apply_extracted_payloads_rechecks_only_new_rows_before_batch_publish(self, mock_store):
        from ingest.extract import apply_extracted_payloads

        initial_snapshot = MagicMock()
        initial_snapshot.execute.return_value.fetchone.return_value = (10,)
        write_conn = MagicMock()
        seen_exec = []

        def write_execute(sql, *_args, **_kwargs):
            seen_exec.append(sql)
            res = MagicMock()
            if "MAX(rowid)" in sql:
                res.fetchone.return_value = (12,)
            return res

        write_conn.execute.side_effect = write_execute
        entered = []
        conns = iter([initial_snapshot, write_conn])

        @contextmanager
        def fake_batch_write():
            conn = next(conns)
            entered.append(conn)
            yield conn

        seen_calls = []

        def fake_store(**kwargs):
            seen_calls.append(kwargs)
            if kwargs.get("_dedup_only"):
                return {
                    "id": "fact-existing",
                    "status": "duplicate",
                    "existing_text": "Maya wants dietary tagging in the recipe app",
                    "dedup_telemetry": {},
                }
            return {"id": "fact-new", "status": "created", "dedup_telemetry": {}}

        mock_store.side_effect = fake_store

        payload = {
            "raw_facts": [
                {
                    "text": "Maya wants dietary tagging in the recipe app",
                    "category": "fact",
                    "domains": ["project"],
                    "extraction_confidence": "high",
                },
            ],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "facts": [],
            "snippets": {},
            "journal": {},
            "project_logs": {},
            "project_log_metrics": {},
            "facts_stored": 0,
            "facts_skipped": 0,
            "edges_created": 0,
            "dry_run": False,
        }

        with patch("ingest.extract._memory.batch_write", side_effect=fake_batch_write):
            applied = apply_extracted_payloads(
                payload,
                owner_id="test",
                label="rolling-flush",
                session_id="sess-delta-recheck",
                dry_run=False,
            )

        assert applied["facts_stored"] == 0
        assert applied["facts_skipped"] == 1
        assert entered == [initial_snapshot, write_conn]
        assert seen_exec[:2] == ["BEGIN IMMEDIATE", "SELECT COALESCE(MAX(rowid), 0) FROM nodes"]
        assert len(seen_calls) == 1
        assert seen_calls[0]["_dedup_only"] is True
        assert seen_calls[0]["_dedup_rowid_min_exclusive"] == 10
        assert seen_calls[0]["_dedup_rowid_max"] == 12

    @patch("ingest.extract._memory.store")
    def test_apply_extracted_payloads_writes_publish_trace_events(self, mock_store, workspace_dir, monkeypatch):
        from ingest.extract import apply_extracted_payloads

        initial_snapshot = MagicMock()
        initial_snapshot.execute.return_value.fetchone.return_value = (0,)
        batch_snapshot = MagicMock()
        batch_snapshot.execute.return_value.fetchone.return_value = (0,)
        write_conn = object()
        conns = iter([initial_snapshot, batch_snapshot, write_conn])

        @contextmanager
        def fake_batch_write():
            yield next(conns)

        mock_store.return_value = {"id": "fact-1", "status": "created", "dedup_telemetry": {}}
        monkeypatch.setenv("QUAID_PUBLISH_TRACE", "1")
        monkeypatch.setenv("QUAID_INSTANCE", "benchrunner")

        payload = {
            "raw_facts": [
                {
                    "text": "Maya's birthday dinner is planned for May 18",
                    "category": "fact",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                },
            ],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "facts": [],
            "snippets": {},
            "journal": {},
            "project_logs": {},
            "project_log_metrics": {},
            "facts_stored": 0,
            "facts_skipped": 0,
            "edges_created": 0,
            "dry_run": False,
        }

        with patch("ingest.extract._memory.batch_write", side_effect=fake_batch_write), \
             patch("ingest.extract._memory.warm_embeddings", return_value={
                 "requested": 1,
                 "unique": 1,
                 "cache_hits": 1,
                 "warmed": 0,
                 "failed": 0,
             }):
            applied = apply_extracted_payloads(
                payload,
                owner_id="test",
                label="rolling-flush",
                session_id="sess-trace",
                dry_run=False,
            )

        assert applied["facts_stored"] == 1
        trace_path = workspace_dir / "benchrunner" / "logs" / "daemon" / "publish-trace.jsonl"
        rows = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        events = [row["event"] for row in rows]
        assert "publish_start" in events
        assert "publish_batch_conn_opened" in events
        assert "publish_store_call_start" in events
        assert "publish_store_call_done" in events
        assert "publish_complete" in events

    @patch("ingest.extract.call_deep_reasoning")
    @patch("ingest.extract._memory.store")
    def test_skips_invalid_fact_payload_items(self, mock_store, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "facts": [
                "not-a-dict",
                {"text": 123, "category": "fact"},
                {"category": "fact"},
                {"text": "User likes orange juice", "category": "preference", "domains": ["personal"]},
            ]
        }), 1.0)
        mock_store.return_value = {"id": "n1", "status": "created"}

        result = extract_from_transcript(
            transcript="User: test\n\nAssistant: ok",
            owner_id="test",
        )

        assert result["facts_stored"] == 1
        assert result["facts_skipped"] == 3

    @patch("ingest.extract.call_deep_reasoning")
    @patch("ingest.extract._memory.store")
    def test_metadata_scope_fields_are_forwarded(self, mock_store, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "facts": [{"text": "User likes oolong tea", "category": "fact", "domains": ["personal"]}]
        }), 1.0)
        mock_store.return_value = {"id": "n1", "status": "created"}

        extract_from_transcript(
            transcript="User: test\n\nAssistant: ok",
            owner_id="test",
            actor_id="user:owner",
            subject_entity_id="user:owner",
            source_channel="telegram",
            source_conversation_id="chat-1",
            source_author_id="<redacted>",
        )

        kwargs = mock_store.call_args.kwargs
        assert kwargs["actor_id"] == "user:owner"
        assert kwargs["subject_entity_id"] == "user:owner"
        assert kwargs["source_channel"] == "telegram"
        assert kwargs["source_conversation_id"] == "chat-1"
        assert kwargs["source_author_id"] == "<redacted>"

    @patch("ingest.extract.call_deep_reasoning")
    @patch("ingest.extract._memory.store")
    def test_target_datastore_is_forwarded(self, mock_store, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "facts": [{"text": "User likes green tea", "category": "fact", "domains": ["personal"]}]
        }), 1.0)
        mock_store.return_value = {"id": "n1", "status": "created"}

        extract_from_transcript(
            transcript="User: test\n\nAssistant: ok",
            owner_id="test",
            target_datastore="memorydb",
        )

        kwargs = mock_store.call_args.kwargs
        assert kwargs["target_datastore"] == "memorydb"

    @patch("ingest.extract.call_deep_reasoning")
    @patch("ingest.extract._memory.store")
    def test_missing_domains_skips_fact(self, mock_store, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "facts": [{"text": "User likes jasmine tea in the morning", "category": "fact"}]
        }), 1.0)
        mock_store.return_value = {"id": "n1", "status": "created"}

        result = extract_from_transcript(
            transcript="User: test\n\nAssistant: ok",
            owner_id="test",
        )
        assert result["facts_stored"] == 0
        assert result["facts_skipped"] == 1
        assert result["facts"][0]["status"] == "skipped"
        assert "missing required domains" in result["facts"][0]["reason"]
        mock_store.assert_not_called()

    @patch("ingest.extract.call_deep_reasoning")
    @patch("ingest.extract._memory.store")
    def test_invalid_domain_skips_fact_but_keeps_valid(self, mock_store, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "facts": [
                {
                    "text": "User likes jasmine tea in the morning",
                    "category": "fact",
                    "domains": ["not_a_real_domain"],
                },
                {
                    "text": "User prefers black coffee after lunch",
                    "category": "preference",
                    "domains": ["personal"],
                },
            ]
        }), 1.0)
        mock_store.return_value = {"id": "n1", "status": "created"}

        result = extract_from_transcript(
            transcript="User: test\n\nAssistant: ok",
            owner_id="test",
        )
        assert result["facts_stored"] == 1
        assert result["facts_skipped"] == 1
        assert any(f["status"] == "skipped" and "unsupported domains" in f.get("reason", "") for f in result["facts"])
        assert any(f["status"] in ("stored", "updated") and "black coffee" in f.get("text", "") for f in result["facts"])
        assert mock_store.call_count == 1

    @patch("ingest.extract.call_deep_reasoning")
    @patch("ingest.extract.get_config")
    def test_raises_when_no_active_domains_registered(self, mock_get_config, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "facts": [{"text": "User likes jasmine tea in the morning", "category": "fact", "domains": ["personal"]}]
        }), 1.0)
        cfg = SimpleNamespace(
            capture=SimpleNamespace(enabled=True, skip_patterns=[], chunk_tokens=8000),
            retrieval=SimpleNamespace(domains={}),
            users=SimpleNamespace(default_owner="test-user"),
            docs=SimpleNamespace(
                journal=SimpleNamespace(
                    enabled=True,
                    snippets_enabled=True,
                    target_files=["SOUL.md", "USER.md", "ENVIRONMENT.md"],
                    journal_dir="journal",
                    max_entries_per_file=50,
                )
            ),
        )
        mock_get_config.return_value = cfg

        with pytest.raises(RuntimeError, match="No active domains are registered"):
            extract_from_transcript(
                transcript="User: test\n\nAssistant: ok",
                owner_id="test",
            )

    @patch("ingest.extract.call_deep_reasoning")
    @patch("ingest.extract._memory.store")
    def test_snippets_written(self, mock_store, mock_llm, mock_opus_response, workspace_dir):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (mock_opus_response, 1.0)
        mock_store.return_value = {"id": "n1", "status": "created"}

        result = extract_from_transcript(
            transcript="User: test\n\nAssistant: ok",
            owner_id="test",
            write_snippets=True,
        )

        assert "SOUL.md" in result["snippets"]
        assert len(result["snippets"]["SOUL.md"]) == 1
        # Check snippet file was created
        snippet_file = workspace_dir / "SOUL.snippets.md"
        assert snippet_file.exists()

    @patch("ingest.extract.call_deep_reasoning")
    @patch("ingest.extract._memory.store")
    def test_journal_written(self, mock_store, mock_llm, mock_opus_response, workspace_dir):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (mock_opus_response, 1.0)
        mock_store.return_value = {"id": "n1", "status": "created"}

        result = extract_from_transcript(
            transcript="User: test\n\nAssistant: ok",
            owner_id="test",
            write_journal=True,
        )

        assert "SOUL.md" in result["journal"]
        journal_file = workspace_dir / "journal" / "SOUL.journal.md"
        assert journal_file.exists()

    @patch("ingest.extract.call_deep_reasoning")
    @patch("ingest.extract._memory.store")
    def test_no_snippets_flag(self, mock_store, mock_llm, mock_opus_response, workspace_dir):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (mock_opus_response, 1.0)
        mock_store.return_value = {"id": "n1", "status": "created"}

        result = extract_from_transcript(
            transcript="User: test\n\nAssistant: ok",
            owner_id="test",
            write_snippets=False,
        )

        # Snippets parsed but not written
        assert "SOUL.md" in result["snippets"]
        snippet_file = workspace_dir / "SOUL.snippets.md"
        assert not snippet_file.exists()

    @patch("ingest.extract.call_deep_reasoning")
    @patch("ingest.extract._memory.store")
    def test_no_journal_flag(self, mock_store, mock_llm, mock_opus_response, workspace_dir):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (mock_opus_response, 1.0)
        mock_store.return_value = {"id": "n1", "status": "created"}

        result = extract_from_transcript(
            transcript="User: test\n\nAssistant: ok",
            owner_id="test",
            write_journal=False,
        )

        assert "SOUL.md" in result["journal"]
        journal_file = workspace_dir / "journal" / "SOUL.journal.md"
        assert not journal_file.exists()

    @patch("ingest.extract.call_deep_reasoning")
    @patch("ingest.extract._memory.store")
    def test_journal_array_fallback(self, mock_store, mock_llm):
        """LLM may return arrays instead of strings for journal entries."""
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "facts": [],
            "journal_entries": {
                "SOUL.md": ["Paragraph one.", "Paragraph two."],
            },
        }), 1.0)

        result = extract_from_transcript(
            transcript="User: test\n\nAssistant: ok",
            owner_id="test",
            write_journal=False,
        )

        assert result["journal"]["SOUL.md"] == "Paragraph one.\n\nParagraph two."

    @patch("ingest.extract.call_deep_reasoning")
    @patch("ingest.extract._memory.store")
    @patch("ingest.extract._memory.create_edge")
    @patch("ingest.extract.logger.warning")
    def test_edge_failure_non_fatal(self, mock_warn, mock_edge, mock_store, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "facts": [{
                "text": "Alice is friends with Bob the great",
                "category": "relationship",
                "domains": ["personal"],
                "edges": [{"subject": "Alice", "relation": "friend_of", "object": "Bob"}],
            }],
        }), 1.0)
        mock_store.return_value = {"id": "n1", "status": "created"}
        mock_edge.side_effect = Exception("DB error")

        result = extract_from_transcript(
            transcript="User: test\n\nAssistant: ok",
            owner_id="test",
        )

        # Fact still stored despite edge failure
        assert result["facts_stored"] == 1
        assert result["edges_created"] == 0
        assert mock_warn.called
        rendered = " ".join(str(arg) for arg in mock_warn.call_args.args)
        assert "edge failed" in rendered

    @patch("lib.batch_utils.chunk_text_by_tokens")
    @patch("ingest.extract.call_deep_reasoning")
    @patch("ingest.extract._memory.store")
    def test_chunk_carry_context_passed_to_later_chunks(self, mock_store, mock_llm, mock_chunk):
        from ingest.extract import extract_from_transcript

        mock_chunk.return_value = [
            "User: Maya said she changed jobs.",
            "User: She starts next week.",
        ]
        mock_llm.side_effect = [
            (
                json.dumps({
                    "facts": [
                        {
                            "text": "Maya changed jobs from TechFlow to Stripe",
                            "category": "fact",
                            "domains": ["work"],
                            "extraction_confidence": "high",
                        }
                    ]
                }),
                0.8,
            ),
            (json.dumps({"facts": []}), 0.7),
        ]
        mock_store.return_value = {"id": "n1", "status": "created"}

        extract_from_transcript(
            transcript="dummy",
            owner_id="test",
            label="test",
        )

        assert mock_llm.call_count == 2
        first_prompt = mock_llm.call_args_list[0].kwargs["prompt"]
        second_prompt = mock_llm.call_args_list[1].kwargs["prompt"]
        assert "BEGIN TRANSCRIPT CHUNK" in first_prompt
        assert "END TRANSCRIPT CHUNK" in first_prompt
        assert "EARLIER CHUNK CONTEXT" in second_prompt
        assert "BEGIN TRANSCRIPT CHUNK" in second_prompt
        assert "Maya changed jobs from TechFlow to Stripe" in second_prompt

    @patch("lib.batch_utils.chunk_text_by_tokens")
    @patch("ingest.extract.call_deep_reasoning")
    def test_carry_repeat_facts_are_dropped_before_recarry(self, mock_llm, mock_chunk):
        from ingest.extract import extract_from_transcript

        mock_chunk.return_value = [
            "User: Maya changed jobs.",
            "User: Repeats the same fact.",
        ]
        repeated_fact = {
            "text": "Maya changed jobs from TechFlow to Stripe",
            "category": "fact",
            "domains": ["work"],
            "extraction_confidence": "high",
        }
        mock_llm.side_effect = [
            (json.dumps({"chunk_assessment": "usable", "facts": [repeated_fact]}), 0.8),
            (json.dumps({"chunk_assessment": "usable", "facts": [repeated_fact]}), 0.7),
        ]

        result = extract_from_transcript(
            transcript="dummy",
            owner_id="test",
            label="test",
            dry_run=True,
        )

        assert len(result["raw_facts"]) == 1
        assert len(result["carry_facts"]) == 1
        assert result["carry_duplicate_facts_dropped"] == 1
        assert result["assessment_nothing_usable"] == 1

    @patch("lib.batch_utils.chunk_text_by_tokens")
    @patch("ingest.extract._repair_non_json_extraction_payload")
    @patch("ingest.extract.call_deep_reasoning")
    @patch("ingest.extract._memory.store")
    def test_recursively_splits_large_unparseable_chunk(
        self,
        mock_store,
        mock_llm,
        mock_repair,
        mock_chunk,
    ):
        from ingest.extract import extract_from_transcript

        giant_chunk = "User: " + ("large context " * 20000)

        chunk_calls = []

        def _chunk_side_effect(text, max_tokens, split_on):
            chunk_calls.append(int(max_tokens))
            if len(chunk_calls) == 1:
                assert max_tokens == 8000
                return [giant_chunk]
            if len(chunk_calls) == 2:
                assert max_tokens == 8000
                return [
                    "User: Maya lives in Austin.",
                    "User: Maya works at Stripe.",
                ]
            raise AssertionError(f"unexpected max_tokens={max_tokens}")

        mock_chunk.side_effect = _chunk_side_effect
        mock_repair.return_value = None
        mock_llm.side_effect = [
            ("not valid json", 0.3),
            (json.dumps({"facts": [{"text": "Maya lives in Austin.", "domains": ["personal"]}]}), 0.2),
            (json.dumps({"facts": [{"text": "Maya works at Stripe.", "domains": ["personal"]}]}), 0.2),
        ]
        mock_store.return_value = {"id": "n1", "status": "created"}

        result = extract_from_transcript(
            transcript="dummy",
            owner_id="test",
            label="split-test",
        )

        assert mock_llm.call_count == 3
        assert result["chunks_total"] == 1
        assert result["root_chunks"] == 1
        assert result["chunks_processed"] == 2
        assert result["facts_stored"] == 2
        assert result["split_events"] == 1
        assert result["split_child_chunks"] == 2
        assert result["leaf_chunks"] == 2
        assert result["max_split_depth"] == 1
        assert result["deep_calls"] == 3
        assert result["repair_calls"] == 1
        assert result["unclassified_empty_payloads"] == 0

    @patch("ingest.extract.time.time")
    @patch("lib.batch_utils.chunk_text_by_tokens")
    @patch("ingest.extract.call_deep_reasoning")
    def test_stops_processing_when_extract_deadline_expires(self, mock_llm, mock_chunk, mock_time):
        from ingest.extract import extract_from_transcript

        mock_chunk.return_value = [
            "User: first chunk",
            "User: second chunk",
        ]
        mock_llm.return_value = (json.dumps({"facts": []}), 0.4)
        # deadline init, outer chunk1 remaining check, inner chunk1 remaining
        # check, outer chunk2 remaining check (expired). Use an unbounded
        # iterator so incidental logging calls that touch time.time() cannot
        # exhaust the mock in CI.
        mock_time.side_effect = itertools.chain([100.0, 100.0, 100.0, 701.0], itertools.repeat(701.0))

        result = extract_from_transcript(
            transcript="dummy",
            owner_id="test",
            label="deadline-test",
            wall_timeout_seconds=600.0,  # pin so test is independent of DEFAULT_EXTRACT_WALL_SECONDS
        )

        assert result["facts_stored"] == 0
        assert mock_llm.call_count == 1

    @patch("lib.batch_utils.chunk_text_by_tokens")
    @patch("ingest.extract.call_deep_reasoning")
    def test_processes_all_chunks_without_silent_cap(self, mock_llm, mock_chunk):
        from ingest.extract import extract_from_transcript

        mock_chunk.return_value = [f"User: chunk {i}" for i in range(12)]
        mock_llm.side_effect = [
            (json.dumps({"facts": []}), 0.1)
            for _ in range(12)
        ]

        result = extract_from_transcript(
            transcript="dummy",
            owner_id="test",
            label="many-chunks",
        )

        assert result["chunks_total"] == 12
        assert mock_llm.call_count == 12
        assert result["root_chunks"] == 12
        assert result["split_events"] == 0
        assert result["leaf_chunks"] == 12
        assert result["max_split_depth"] == 0
        assert result["deep_calls"] == 12

    @patch("lib.batch_utils.chunk_text_by_tokens")
    @patch("ingest.extract.call_deep_reasoning")
    @patch("ingest.extract._memory.store")
    def test_parallel_root_chunk_extraction_requires_disabled_carry(
        self,
        mock_store,
        mock_llm,
        mock_chunk,
        monkeypatch,
    ):
        from ingest.extract import extract_from_transcript

        root_chunks = [
            "User: Maya likes coffee.",
            "User: Maya works at Stripe.",
            "User: Maya lives in Austin.",
        ]
        prompts = []

        def _fake_llm(*, prompt, **_kwargs):
            prompts.append(prompt)
            if "likes coffee" in prompt:
                fact_text = "Maya likes coffee."
            elif "works at Stripe" in prompt:
                fact_text = "Maya works at Stripe."
            elif "lives in Austin" in prompt:
                fact_text = "Maya lives in Austin."
            else:
                raise AssertionError(f"unexpected prompt: {prompt[:120]}")
            return json.dumps({"facts": [{"text": fact_text, "domains": ["personal"]}]}), 0.1

        mock_chunk.return_value = root_chunks
        mock_llm.side_effect = _fake_llm
        mock_store.return_value = {"id": "n1", "status": "created"}
        monkeypatch.setenv("QUAID_EXTRACT_DISABLE_CARRY_CONTEXT", "1")
        monkeypatch.setenv("QUAID_EXTRACT_PARALLEL_ROOT_WORKERS", "3")

        result = extract_from_transcript(
            transcript="dummy",
            owner_id="test",
            label="parallel-roots",
        )

        assert result["carry_context_enabled"] is False
        assert result["parallel_root_workers"] == 3
        assert result["root_chunks"] == 3
        assert result["chunks_processed"] == 3
        assert result["facts_stored"] == 3
        assert result["deep_calls"] == 3
        assert all("EARLIER CHUNK CONTEXT" not in prompt for prompt in prompts)


# ---------------------------------------------------------------------------
# _format_human_summary tests
# ---------------------------------------------------------------------------

class TestFormatHumanSummary:
    def test_basic_summary(self):
        from ingest.extract import _format_human_summary

        result = {
            "facts_stored": 3,
            "facts_skipped": 1,
            "edges_created": 2,
            "facts": [
                {"text": "User likes coffee", "status": "stored", "edges": []},
                {"text": "hi", "status": "skipped", "edges": []},
            ],
            "snippets": {"SOUL.md": ["one"]},
            "journal": {"SOUL.md": "entry"},
            "dry_run": False,
        }

        summary = _format_human_summary(result)
        assert "Facts stored:  3" in summary
        assert "Facts skipped: 1" in summary
        assert "Edges created: 2" in summary

    def test_dry_run_prefix(self):
        from ingest.extract import _format_human_summary

        result = {
            "facts_stored": 1, "facts_skipped": 0, "edges_created": 0,
            "facts": [], "snippets": {}, "journal": {}, "dry_run": True,
        }

        summary = _format_human_summary(result)
        assert "[DRY RUN]" in summary


# ---------------------------------------------------------------------------
# _load_extraction_prompt tests
# ---------------------------------------------------------------------------

class TestLoadPrompt:
    def test_prompt_loads(self):
        from ingest.extract import _load_extraction_prompt

        prompt = _load_extraction_prompt()
        assert "memory extraction system" in prompt.lower()
        assert "facts" in prompt
        assert "edges" in prompt
        assert "soul_snippets" in prompt
        assert "journal_entries" in prompt

    def test_prompt_has_json_schema(self):
        from ingest.extract import _load_extraction_prompt

        prompt = _load_extraction_prompt()
        assert '"facts"' in prompt
        assert '"text"' in prompt
        assert '"category"' in prompt

    def test_prompt_preserves_chunk_assessment_guidance(self):
        from ingest.extract import _load_extraction_prompt

        prompt = _load_extraction_prompt()
        assert "chunk_assessment" in prompt
        assert "needs_smaller_chunk" in prompt
        assert "nothing_usable" in prompt
        assert "usable" in prompt

    @patch("ingest.extract.call_fast_reasoning")
    def test_json_repair_prompt_prefers_needs_smaller_chunk_for_truncated_dense_output(self, mock_fast):
        from ingest.extract import _repair_non_json_extraction_payload

        mock_fast.return_value = (
            json.dumps(
                {
                    "chunk_assessment": "needs_smaller_chunk",
                    "facts": [],
                    "soul_snippets": {},
                    "journal_entries": {},
                    "project_logs": {},
                }
            ),
            0.3,
        )

        repaired = _repair_non_json_extraction_payload(
            response_text="```json\n{\"facts\": [{\"text\": \"truncated",
            chunk_index=1,
            label="repair-test",
        )

        assert repaired["chunk_assessment"] == "needs_smaller_chunk"
        repair_prompt = mock_fast.call_args.kwargs["prompt"]
        assert "return chunk_assessment as needs_smaller_chunk" in repair_prompt


# ---------------------------------------------------------------------------
# _get_owner_id tests
# ---------------------------------------------------------------------------

class TestGetOwnerId:
    def test_override(self):
        from ingest.extract import _get_owner_id
        assert _get_owner_id("custom") == "custom"

    def test_fallback_default(self):
        from ingest.extract import _get_owner_id
        # With config mocked to fail
        with patch("ingest.extract.get_config", side_effect=Exception("no config")), \
             patch("ingest.extract.is_fail_hard_enabled", return_value=False):
            assert _get_owner_id(None) == "default"

    def test_fallback_raises_when_fail_hard_enabled(self):
        from ingest.extract import _get_owner_id
        with patch("ingest.extract.get_config", side_effect=Exception("no config")), \
             patch("ingest.extract.is_fail_hard_enabled", return_value=True):
            with pytest.raises(RuntimeError, match="extract owner"):
                _get_owner_id(None)


class TestChunkCarryContext:
    def test_prefers_recent_tail_and_caps_size(self):
        from ingest.extract import _build_chunk_carry_context

        facts = [
            {"text": "Old stable fact about Maya's first job", "category": "fact", "speaker": "user", "extraction_confidence": "high"},
            {"text": "Middle fact about dinner plans next week", "category": "fact", "speaker": "user", "extraction_confidence": "medium"},
            {"text": "Recent fact about Stripe start date", "category": "fact", "speaker": "user", "extraction_confidence": "high"},
        ]
        ctx = _build_chunk_carry_context(facts, max_items=2, max_chars=300)
        assert "Old stable fact" not in ctx
        assert "Middle fact" in ctx
        assert "Recent fact" in ctx
        assert "Recent carry facts:" in ctx
        assert "[fact, user, high]" in ctx or "[fact, user, medium]" in ctx

    def test_keeps_sticky_exact_and_agent_facts_in_bounded_context(self):
        from ingest.extract import _build_chunk_carry_context

        facts = [
            {
                "text": "Maya finished her half marathon in 2:14.",
                "category": "fact",
                "speaker": "user",
                "extraction_confidence": "high",
            },
            {
                "text": "The agent recommended a foam roller routine after Maya's long run.",
                "category": "decision",
                "speaker": "agent",
                "extraction_confidence": "medium",
                "project": "recipe-app",
            },
        ]
        for idx in range(12):
            facts.append(
                {
                    "text": f"Generic recent project chatter number {idx} with no exact value",
                    "category": "fact",
                    "speaker": "user",
                    "extraction_confidence": "medium",
                }
            )

        ctx = _build_chunk_carry_context(facts, max_items=8, max_chars=1600)
        assert "Anchor carry facts:" in ctx
        assert "Recent carry facts:" in ctx
        assert "2:14" in ctx
        assert "foam roller routine" in ctx
        assert "project:recipe-app" in ctx

    def test_anchor_facts_survive_char_budget_before_recent_chatter(self):
        from ingest.extract import _build_chunk_carry_context

        facts = [
            {
                "text": "Maya finished her half marathon in 2:14 with a strong last mile.",
                "category": "fact",
                "speaker": "user",
                "extraction_confidence": "high",
            },
            {
                "text": "The agent recommended a foam roller routine for Maya's knee.",
                "category": "decision",
                "speaker": "agent",
                "extraction_confidence": "medium",
            },
        ]
        for idx in range(20):
            facts.append(
                {
                    "text": (
                        f"Generic recent project chatter number {idx} about ongoing cleanup "
                        f"and follow-up tasks with no exact retrieval handle."
                    ),
                    "category": "fact",
                    "speaker": "user",
                    "extraction_confidence": "medium",
                }
            )

        ctx = _build_chunk_carry_context(facts, max_items=10, max_chars=420)
        assert "Anchor carry facts:" in ctx
        assert "2:14" in ctx
        assert "foam roller routine" in ctx


# ---------------------------------------------------------------------------
# CLI tests (subprocess-level)
# ---------------------------------------------------------------------------

class TestCLI:
    def test_help(self):
        import subprocess
        result = subprocess.run(
            [sys.executable, str(Path(__file__).parent.parent / "ingest" / "extract.py"), "--help"],
            capture_output=True, text=True,
        )
        # argparse --help exits 0
        assert result.returncode == 0
        assert "extract" in result.stdout.lower()

    def test_missing_file(self):
        import subprocess
        result = subprocess.run(
            [sys.executable, str(Path(__file__).parent.parent / "ingest" / "extract.py"), "/nonexistent/file.txt"],
            capture_output=True, text=True,
            env={**os.environ, "MEMORY_DB_PATH": ":memory:", "QUAID_QUIET": "1"},
        )
        assert result.returncode != 0
        assert "not found" in result.stderr.lower() or "error" in result.stderr.lower()
