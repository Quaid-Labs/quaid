"""Tests for extract.py — Memory extraction from conversation transcripts."""

import importlib
import json
import os
import sys
import tempfile
from contextlib import contextmanager, nullcontext
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
    vroot = adapter.visible_instance_root()

    os.environ["OPENCLAW_WORKSPACE"] = str(iroot)
    os.environ["QUAID_VISIBLE_HOME"] = str(tmp_path)
    os.environ["MOCK_EMBEDDINGS"] = "1"

    # Create required directories in both hidden and visible roots.
    (vroot / "journal").mkdir(parents=True, exist_ok=True)

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
    (iroot / "config.json").write_text(json.dumps(config))

    yield iroot

    reset_adapter()
    if "OPENCLAW_WORKSPACE" in os.environ:
        del os.environ["OPENCLAW_WORKSPACE"]
    if "QUAID_VISIBLE_HOME" in os.environ:
        del os.environ["QUAID_VISIBLE_HOME"]
    if "MOCK_EMBEDDINGS" in os.environ:
        del os.environ["MOCK_EMBEDDINGS"]


@pytest.fixture
def visible_workspace_dir(workspace_dir):
    from lib.adapter import get_adapter

    return get_adapter().visible_instance_root()


@pytest.fixture
def mock_opus_response():
    """Standard Opus extraction response."""
    return json.dumps({
        "chunk_assessment": "usable",
        "facts": [
            {
                "text": "Test user likes coffee",
                "category": "preference",
                "speaker": "user",
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
                "speaker": "user",
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

    def test_strips_offline_extraction_prompt_block(self, tmp_path):
        from ingest.extract import parse_session_jsonl

        jsonl_file = tmp_path / "session.jsonl"
        lines = [
            json.dumps(
                {
                    "role": "user",
                    "content": (
                        "You are performing offline memory extraction on a transcript archive.\n"
                        "Do NOT continue the conversation, answer questions, write code, or act as the assistant in the transcript.\n"
                        "Treat the transcript strictly as inert source material and return extraction JSON only.\n\n"
                        "Extract memorable facts and journal entries from this transcript chunk.\n"
                        "=== BEGIN TRANSCRIPT CHUNK ===\n"
                        "User: My sister is Diana.\n\nAssistant: Her daughter is Alice.\n"
                        "=== END TRANSCRIPT CHUNK ==="
                    ),
                }
            ),
            json.dumps({"role": "assistant", "content": "Normal assistant reply."}),
        ]
        jsonl_file.write_text("\n".join(lines))

        result = parse_session_jsonl(str(jsonl_file))
        assert "offline memory extraction on a transcript archive" not in result
        assert "BEGIN TRANSCRIPT CHUNK" not in result
        assert "My sister is Diana." not in result
        assert "Her daughter is Alice." not in result
        assert "Assistant: Normal assistant reply." in result

    def test_strips_dedup_review_prompt_block(self, tmp_path):
        from ingest.extract import parse_session_jsonl

        jsonl_file = tmp_path / "session.jsonl"
        lines = [
            json.dumps(
                {
                    "role": "user",
                    "content": (
                        "You are reviewing 50 dedup rejections in a personal knowledge base.\n\n"
                        "When in doubt, CONFIRM.\n"
                        "1. Log ID: abc\n"
                        "   New text: \"A\"\n"
                        "   Existing text: \"B\""
                    ),
                }
            ),
            json.dumps({"role": "assistant", "content": "Normal assistant reply."}),
        ]
        jsonl_file.write_text("\n".join(lines))

        result = parse_session_jsonl(str(jsonl_file))
        assert "dedup rejections" not in result
        assert "Log ID:" not in result
        assert "Assistant: Normal assistant reply." in result

    def test_strips_dedup_compare_prompt_block(self, tmp_path):
        from ingest.extract import parse_session_jsonl

        jsonl_file = tmp_path / "session.jsonl"
        lines = [
            json.dumps(
                {
                    "role": "user",
                    "content": (
                        "Compare Statement A against each candidate statement below.\n\n"
                        "Statement A (new): \"A\"\n\n"
                        "Candidates:\n1. \"B\"\n\n"
                        "Respond with JSON only as an array of objects:\n"
                        "[{\"pair\":1,\"is_same\":true}]"
                    ),
                }
            ),
            json.dumps({"role": "assistant", "content": "Normal assistant reply."}),
        ]
        jsonl_file.write_text("\n".join(lines))

        result = parse_session_jsonl(str(jsonl_file))
        assert "Compare Statement A" not in result
        assert "Statement A (new):" not in result
        assert "Candidates:" not in result
        assert "Assistant: Normal assistant reply." in result


# ---------------------------------------------------------------------------
# extract_from_transcript tests
# ---------------------------------------------------------------------------

class TestExtractFromTranscript:
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
        assert result["facts_stored"] == 0
        assert result["facts_planned"] == 2
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

    @patch("ingest.extract.call_deep_reasoning")
    @patch("ingest.extract._memory.store")
    def test_unparseable_response_uses_json_repair(self, mock_store, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.side_effect = [
            ("I remembered that your mother is Wendy.", 1.0),
            (json.dumps({
                "facts": [
                    {
                        "text": "User's mother is Wendy",
                        "category": "fact",
                        "speaker": "user",
                        "domains": ["personal"],
                        "extraction_confidence": "high",
                    }
                ],
                "soul_snippets": {},
                "journal_entries": {},
                "project_logs": {},
            }), 0.5),
        ]
        mock_store.return_value = {"id": "n1", "status": "created"}

        result = extract_from_transcript(
            transcript="User: my mother is Wendy\n\nAssistant: got it",
            owner_id="test",
        )

        assert result["facts_stored"] == 1
        assert result["repair_calls"] == 1
        assert mock_llm.call_count == 2

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

    @patch("ingest.extract.call_deep_reasoning")
    def test_explicit_structural_anchor_is_preserved_when_llm_omits_marker(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "usable",
            "facts": [
                {
                    "text": "Solomon Steadman has a Friday ritual of roasting pumpkin seeds",
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                    "privacy": "private",
                }
            ],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        result = extract_from_transcript(
            transcript=(
                "User: My Friday ritual is roasting pumpkin seeds with the codeword "
                "walnut-umbrella-7142.\n\n"
                "Assistant: Got it. I won't repeat or store that codeword unless asked.\n\n"
                "User: My Friday ritual is roasting pumpkin seeds with the codeword "
                "walnut-umbrella-7142.\n\n"
                "Assistant: Understood."
            ),
            owner_id="Solomon Steadman",
            dry_run=True,
        )

        texts = [fact["text"] for fact in result["raw_facts"]]
        assert result["explicit_structural_anchor_facts"] == 1
        assert texts[0] == (
            "My Friday ritual is roasting pumpkin seeds with the codeword "
            "walnut-umbrella-7142"
        )
        assert any("walnut-umbrella-7142" in text for text in texts)

    @patch("ingest.extract.call_deep_reasoning")
    def test_explicit_structural_anchor_preserves_non_english_statement(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "usable",
            "facts": [
                {
                    "text": "El ritual de los viernes de Solomon es tostar semillas de calabaza",
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                    "privacy": "private",
                }
            ],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        result = extract_from_transcript(
            transcript=(
                "User: Mi ritual de viernes es tostar semillas de calabaza con la clave "
                "cedro-plantilla-4821.\n\nAssistant: Entendido."
            ),
            owner_id="Solomon Steadman",
            dry_run=True,
        )

        texts = [fact["text"] for fact in result["raw_facts"]]
        assert result["explicit_structural_anchor_facts"] == 1
        assert texts[0] == "Mi ritual de viernes es tostar semillas de calabaza con la clave cedro-plantilla-4821"

    def test_prefixed_turn_parser_accepts_codex_row_timestamps(self):
        from ingest import extract as extract_mod

        turns = extract_mod._iter_prefixed_turns(
            "[2026-05-02T14:29:21.414Z] User: My shelf marker is cedar-lantern-4821.\n\n"
            "[2026-05-02T14:29:23.024Z] Assistant: Noted."
        )

        assert turns == [
            ("user", "My shelf marker is cedar-lantern-4821."),
            ("assistant", "Noted."),
        ]

    def test_transcript_timestamp_hint_ignores_project_log_entry_dates(self):
        from ingest import extract as extract_mod

        assert extract_mod._first_transcript_timestamp_hint(
            "- [2023-02-14T10:00:00] hist-amber-valentine-2023"
        ) is None
        assert (
            extract_mod._first_transcript_timestamp_hint(
                "[2026-05-02T14:29:21.414Z] User: My shelf marker is cedar-lantern-4821."
            )
            == "2026-05-02T14:29:21+00:00"
        )
        assert (
            extract_mod._first_transcript_timestamp_hint(
                "[2026-05-02T14:29:22.414Z] Subagent/User: Child task found Mendoza Malbec."
            )
            == "2026-05-02T14:29:22+00:00"
        )
        assert (
            extract_mod._first_transcript_timestamp_hint(
                "2026-05-02T14:29:23Z Subagent/Assistant: Child reply."
            )
            == "2026-05-02T14:29:23+00:00"
        )

    @patch("ingest.extract.call_deep_reasoning")
    def test_extraction_defaults_mentioned_at_to_transcript_timestamp(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "usable",
            "facts": [
                {
                    "text": "The reading chair has a brass desk lamp beside it",
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["household"],
                    "extraction_confidence": "high",
                    "privacy": "private",
                }
            ],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        result = extract_from_transcript(
            transcript=(
                "[2026-05-02T14:29:21.414Z] User: The reading chair has a brass desk lamp beside it.\n\n"
                "[2026-05-02T14:29:23.024Z] Assistant: Noted."
            ),
            owner_id="Solomon Steadman",
            session_id="rollout-2026-05-02T14-28-38-019de917-68bb-7922-a85b-4c154596e703",
            dry_run=True,
        )

        assert result["raw_facts"][0]["mentioned_at"] == "2026-05-02T14:29:21+00:00"
        assert result["raw_facts"][0]["_source_timestamp"] == "2026-05-02T14:29:21+00:00"
        assert "created_at" not in result["raw_facts"][0]

    @patch("ingest.extract._current_utc_timestamp", return_value="2026-05-09T08:00:00+00:00")
    @patch("ingest.extract.call_deep_reasoning")
    def test_extraction_does_not_use_project_log_entry_date_as_created_at(self, mock_llm, _mock_now):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "usable",
            "facts": [
                {
                    "text": "hist-amber-valentine-2023 was the amber-tinted valentine dinner",
                    "category": "fact",
                    "speaker": "assistant",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                    "privacy": "shared",
                }
            ],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        result = extract_from_transcript(
            transcript="- [2023-02-14T10:00:00] hist-amber-valentine-2023: amber-tinted valentine dinner",
            owner_id="Solomon Steadman",
            session_id="project-log-index",
            dry_run=True,
        )

        fact = result["raw_facts"][0]
        assert "created_at" not in fact
        assert fact["mentioned_at"] == "2026-05-09T08:00:00+00:00"
        assert "_source_timestamp" not in fact

    @patch("ingest.extract._current_utc_timestamp", return_value="2026-05-07T03:42:00+00:00")
    @patch("ingest.extract.call_deep_reasoning")
    def test_extraction_defaults_mentioned_at_without_source_timestamp(self, mock_llm, mock_now):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "usable",
            "facts": [
                {
                    "text": "Solomon Steadman keeps a ceramic compass on the desk",
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                    "privacy": "shared",
                }
            ],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        result = extract_from_transcript(
            transcript="User: I keep a ceramic compass on the desk.\n\nAssistant: Noted.",
            owner_id="Solomon Steadman",
            dry_run=True,
        )

        fact = result["raw_facts"][0]
        assert fact["mentioned_at"] == "2026-05-07T03:42:00+00:00"
        assert "created_at" not in fact
        assert mock_now.called

    @patch("ingest.extract.call_deep_reasoning")
    def test_extraction_prefers_transcript_timestamp_over_same_day_date_only_fact(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "usable",
            "facts": [
                {
                    "text": "The green velvet armchair has a marble side table beside it",
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["household"],
                    "extraction_confidence": "high",
                    "privacy": "private",
                    "created_at": "2026-05-02",
                }
            ],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        result = extract_from_transcript(
            transcript=(
                "[2026-05-02T14:49:46.911Z] User: The green velvet armchair has a marble side table beside it.\n\n"
                "[2026-05-02T14:49:49.302Z] Assistant: Noted."
            ),
            owner_id="Solomon Steadman",
            session_id="rollout-2026-05-02T14-49-28-019de92a-7bf6-7d72-8ef1-bb553cfd9d21",
            dry_run=True,
        )

        assert result["raw_facts"][0]["mentioned_at"] == "2026-05-02T14:49:46+00:00"
        assert result["raw_facts"][0]["_source_timestamp"] == "2026-05-02T14:49:46+00:00"
        assert "created_at" not in result["raw_facts"][0]

    @patch("ingest.extract.call_deep_reasoning")
    def test_extraction_preserves_occurred_range_separate_from_mentioned_at(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "usable",
            "facts": [
                {
                    "text": "Melanie attended the May 2023 art workshop",
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                    "privacy": "shared",
                    "occurred_start": "2023-05-01",
                    "occurred_end": "2023-05-31",
                }
            ],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        result = extract_from_transcript(
            transcript=(
                "[2026-05-02T14:49:46.911Z] User: Melanie told me she attended "
                "the May 2023 art workshop.\n\n"
                "[2026-05-02T14:49:49.302Z] Assistant: Noted."
            ),
            owner_id="Solomon Steadman",
            dry_run=True,
        )

        fact = result["raw_facts"][0]
        assert fact["occurred_start"] == "2023-05-01T23:59:59"
        assert fact["occurred_end"] == "2023-05-31T23:59:59"
        assert fact["mentioned_at"] == "2026-05-02T14:49:46+00:00"
        assert fact["_source_timestamp"] == "2026-05-02T14:49:46+00:00"
        assert "created_at" not in fact

    @patch("ingest.extract.call_deep_reasoning")
    def test_extraction_prefers_source_mention_time_over_llm_mentioned_at(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "usable",
            "facts": [
                {
                    "text": "Melanie attended the May 2023 art workshop",
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                    "privacy": "shared",
                    "mentioned_at": "2025-01-13T23:59:59",
                    "occurred_start": "2023-05-01",
                    "occurred_end": "2023-05-31",
                }
            ],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        result = extract_from_transcript(
            transcript=(
                "[2026-05-07T05:10:21.414Z] User: Melanie told me she attended "
                "the May 2023 art workshop.\n\n"
                "[2026-05-07T05:10:23.024Z] Assistant: Noted."
            ),
            owner_id="Solomon Steadman",
            dry_run=True,
        )

        fact = result["raw_facts"][0]
        assert fact["mentioned_at"] == "2026-05-07T05:10:21+00:00"
        assert fact["occurred_start"] == "2023-05-01T23:59:59"
        assert fact["occurred_end"] == "2023-05-31T23:59:59"

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_named_option_anchor_is_preserved_when_llm_omits_it(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "usable",
            "facts": [
                {
                    "text": "David planned a surprise birthday dinner for Linda",
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                    "privacy": "shared",
                },
                {
                    "text": "Underbelly",
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                    "privacy": "shared",
                }
            ],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: D wants to do a surprise birthday dinner for my mom in Houston.\n\n"
            "Assistant: Montrose is actually perfect for this — it's one of the best food neighborhoods in Houston.\n"
            "  - **Underbelly** successor restaurants (Chris Shepherd's places)\n"
            "  - **Local Foods** — more casual but great for dietary flexibility\n"
            "  - **Uchi Houston** — Japanese, maybe too fancy?\n"
            "  Want me to look into any of those?\n\n"
            "User: the local foods direction sounds right.\n\n"
            "Assistant: I'd look at places like Weights + Measures, or Feges BBQ for something more casual.\n"
            "  Both in the Montrose/Heights area.\n"
            "  Do you want me to look up specific menus?\n"
            "\nUser: menus would help.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_facts = [
            fact
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        agent_texts = [fact["text"] for fact in agent_facts]
        assert any(text.startswith("Local Foods") for text in agent_texts)
        assert any(text.startswith("Uchi Houston") for text in agent_texts)
        assert any("Weights + Measures" in text and "Feges BBQ" in text for text in agent_texts)
        assert all(
            fact.get("extraction_confidence") == "high"
            for fact in agent_facts
            if any(
                marker in str(fact.get("text", "") or "")
                for marker in ("Local Foods", "Uchi Houston", "Weights + Measures", "Feges BBQ")
            )
        )

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_plan_anchor_is_preserved_when_llm_omits_it(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "usable",
            "facts": [
                {
                    "text": "Rachel FaceTimed into Linda's birthday dinner with Ethan and Lily",
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                    "privacy": "shared",
                }
            ],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: maybe we do a FaceTime thing for Rachel during dinner?\n\n"
            "Assistant: The FaceTime call during dinner is actually a great idea — it makes the surprise even bigger.\n"
            "  Your mom thinks it's just her and David, then you show up, then Rachel's on the phone.\n"
            "  Layer the surprises. It'll be a great moment.\n"
            "  Want Rachel to have a specific time to call?\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_facts = [
            fact
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        agent_texts = [fact["text"] for fact in agent_facts]
        assert any(
            "FaceTime call during dinner" in text and "David" in text and "Rachel" in text
            for text in agent_texts
        )
        assert any(
            fact.get("extraction_confidence") == "high"
            and "FaceTime call during dinner" in str(fact.get("text", "") or "")
            for fact in agent_facts
        )

    @patch("ingest.extract.call_deep_reasoning")
    def test_question_shaped_user_idea_anchor_is_preserved_when_assistant_builds_on_it(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "usable",
            "facts": [
                {
                    "text": "Rachel FaceTimed into Linda's birthday dinner with Ethan and Lily",
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                    "privacy": "shared",
                }
            ],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: maybe we do a FaceTime thing for Rachel during dinner?\n"
            "  like she calls once we're seated.\n\n"
            "Assistant: The FaceTime call during dinner is actually a great idea — it makes the surprise even bigger.\n"
            "  Your mom thinks it's just her and David, then you show up, then Rachel's on the phone.\n"
            "  Layer the surprises. It'll be a great moment.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        user_facts = [
            fact
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "user"
        ]
        assert any(
            fact.get("extraction_confidence") == "high"
            and "FaceTime thing for Rachel during dinner" in str(fact.get("text", "") or "")
            for fact in user_facts
        )

    @patch("ingest.extract.call_deep_reasoning")
    def test_question_shaped_user_idea_anchor_is_preserved_in_spanish_without_titlecase(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "usable",
            "facts": [
                {
                    "text": "Raquel llamó durante la cena de cumpleaños de Linda",
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                    "privacy": "shared",
                }
            ],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: y si hacemos una videollamada con raquel durante la cena?\n"
            "  ella llama cuando ya estemos sentados.\n\n"
            "Assistant: la videollamada durante la cena es una gran idea.\n"
            "  tu mamá piensa que solo está con david, luego apareces tú, y después raquel entra en la llamada.\n"
            "  así la sorpresa crece por capas.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        user_facts = [
            fact
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "user"
        ]
        assert any(
            fact.get("extraction_confidence") == "high"
            and "videollamada con raquel durante la cena" in str(fact.get("text", "") or "").lower()
            for fact in user_facts
        )

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_callback_anchor_is_preserved_when_llm_omits_it(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "usable",
            "facts": [
                {
                    "text": "Biscuit learned to shake hands by May 2026 after 3 months of training",
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                    "privacy": "shared",
                },
                {
                    "text": "Biscuit tried to eat a pinecone and David had to wrestle it away from him",
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                    "privacy": "shared",
                },
            ],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: biscuit learned a new trick. he can shake hands now.\n\n"
            "Assistant: And Biscuit learning to shake is a triumph of persistence over brain cells.\n"
            "  For a golden retriever who once tried to eat a pinecone, this is character growth.\n"
            "  3 months for one trick is very on-brand for the one-brain-cell dog.\n\n"
            "User: ok i can't believe you remember the pinecone thing from months ago.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_texts = [
            fact["text"]
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        assert any("pinecone" in text and "Biscuit" in text for text in agent_texts)
        assert any(
            fact.get("extraction_confidence") == "high"
            and "pinecone" in str(fact.get("text", "") or "").lower()
            and "biscuit" in str(fact.get("text", "") or "").lower()
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        )

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_callback_anchor_is_preserved_from_surprise_reaction_without_older_fact(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "usable",
            "facts": [
                {
                    "text": "Biscuit learned to shake hands by May 2026 after 3 months of training",
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                    "privacy": "shared",
                },
            ],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: biscuit learned a new trick. he can shake hands now.\n\n"
            "Assistant: And Biscuit learning to shake is a triumph of persistence over brain cells.\n"
            "  For a golden retriever who once tried to eat a pinecone, this is character growth.\n"
            "  3 months for one trick is very on-brand for the one-brain-cell dog.\n\n"
            "User: THE PINECONE. i forgot about that. ok i can't believe you remember the pinecone thing from months ago.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_facts = [
            fact
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        assert any(
            fact.get("structural_anchor_kind") == "assistant_callback_anchor"
            and "pinecone" in str(fact.get("text", "") or "").lower()
            and "biscuit" in str(fact.get("text", "") or "").lower()
            for fact in agent_facts
        )

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_callback_anchor_survives_real_session20_paragraph_split(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "usable",
            "facts": [
                {
                    "text": "Maya and David's dog Biscuit learned to shake hands after 3 months of training.",
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                    "privacy": "shared",
                },
                {
                    "text": "Biscuit is a golden retriever with a short attention span who once tried to eat a pinecone.",
                    "category": "fact",
                    "speaker": "agent",
                    "domains": ["personal"],
                    "extraction_confidence": "medium",
                    "privacy": "shared",
                },
            ],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: also biscuit learned a new trick. he can shake hands now. it took us like 3 months to teach him because he has the attention span of a goldfish but he did it\n\n"
            "Assistant: The Thai place on South Congress! That's become a regular spot for you two.\n\n"
            "And Biscuit learning to shake is a triumph of persistence over brain cells. For a golden retriever who once tried to eat a pinecone, this is character growth.\n\n"
            "3 months for one trick is very on-brand for the one-brain-cell dog.\n\n"
            "User: THE PINECONE. i forgot about that. god he's dumb. i love him so much\n\n"
            "ok i can't believe you remember the pinecone thing from like... months ago.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_facts = [
            fact
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        assert any(
            fact.get("structural_anchor_kind") == "assistant_callback_anchor"
            and "pinecone" in str(fact.get("text", "") or "").lower()
            and "biscuit" in str(fact.get("text", "") or "").lower()
            for fact in agent_facts
        )

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_callback_anchor_survives_post_surprise_confirmation_turn(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "usable",
            "facts": [
                {
                    "text": "Biscuit learned to shake hands after 3 months of training due to his limited attention span",
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                    "privacy": "shared",
                },
                {
                    "text": "Biscuit once tried to eat a pinecone and someone had to stop him",
                    "category": "fact",
                    "speaker": "agent",
                    "domains": ["personal"],
                    "extraction_confidence": "medium",
                    "privacy": "shared",
                },
            ],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: also biscuit learned a new trick. he can shake hands now. it took us like 3 months to teach him because he has the attention span of a goldfish but he did it\n\n"
            "Assistant: The Thai place on South Congress! That's become a regular spot for you two.\n\n"
            "And Biscuit learning to shake is a triumph of persistence over brain cells. For a golden retriever who once tried to eat a pinecone, this is character growth.\n\n"
            "3 months for one trick is very on-brand for the one-brain-cell dog.\n\n"
            "User: THE PINECONE. i forgot about that. god he's dumb. i love him so much\n\n"
            "ok i can't believe you remember the pinecone thing from like... months ago.\n\n"
            "Assistant: Some things are unforgettable. And Biscuit committing fully to eating a pinecone is definitely one of those things.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_facts = [
            fact
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        assert any(
            fact.get("structural_anchor_kind") == "assistant_callback_anchor"
            and "some things are unforgettable" in str(fact.get("text", "") or "").lower()
            and "pinecone" in str(fact.get("text", "") or "").lower()
            for fact in agent_facts
        )

    @patch("ingest.extract.call_deep_reasoning")
    def test_user_recall_reaction_anchor_is_preserved_from_real_session20_reaction(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "usable",
            "facts": [
                {
                    "text": "Biscuit is a golden retriever and has learned to shake hands as a new trick.",
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                    "privacy": "shared",
                },
                {
                    "text": "Biscuit previously tried to eat a pinecone, demonstrating his limited intelligence according to Maya.",
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                    "privacy": "shared",
                },
            ],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "Maya: also biscuit learned a new trick. he can shake hands now. it took us like 3 months to teach him because he has the attention span of a goldfish but he did it\n\n"
            "Assistant: And Biscuit learning to shake is a triumph of persistence over brain cells. For a golden retriever who once tried to eat a pinecone, this is character growth.\n\n"
            "Maya: THE PINECONE. i forgot about that. god he's dumb. i love him so much\n\n"
            "ok i can't believe you remember the pinecone thing from like... months ago.\n\n"
            "Assistant: Some things are unforgettable. And Biscuit committing fully to eating a pinecone is definitely one of those things.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        user_facts = [
            fact
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "user"
        ]
        assert any(
            fact.get("structural_anchor_kind") == "user_recall_reaction_anchor"
            and "remember the pinecone thing" in str(fact.get("text", "") or "").lower()
            and "months ago" in str(fact.get("text", "") or "").lower()
            for fact in user_facts
        )

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_meta_capability_chatter_is_not_preserved_as_anchor(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "nothing_usable",
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: are you connected to my calendar or email or anything?\n\n"
            "Assistant: Mostly \"ask questions get answers\" for now — I can't see your calendar, email, or anything like that. "
            "Just this conversation.\n\n"
            "User: ok got it.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_texts = [
            fact["text"]
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        assert not any("ask questions get answers" in text.lower() for text in agent_texts)
        assert not any("calendar, email" in text.lower() for text in agent_texts)

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_generic_greeting_list_is_not_preserved_as_anchor(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "nothing_usable",
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: just figuring out what this is.\n\n"
            "Assistant: Hey Maya! Nice to meet you. I'm here whenever you need help with anything — "
            "work stuff, random questions, whatever.\n\n"
            "User: cool.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_texts = [
            fact["text"]
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        assert not any("nice to meet you" in text.lower() for text in agent_texts)
        assert not any("random questions" in text.lower() for text in agent_texts)

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_reference_bullets_are_not_preserved_as_anchors(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "nothing_usable",
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: ok show me the endpoints and filter params.\n\n"
            "Assistant: Sure — the main ones are:\n"
            "  - GET /api/recipes/:id — now includes structuredIngredients array\n"
            "  - POST /api/meal-plans — create a meal plan\n"
            "  - @param {boolean} [filters.safeForMom] - Shortcut for diabetic-friendly + low-sodium\n"
            "  - recipes: findAll (with dietary/prep time/pagination filters)\n\n"
            "User: got it.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_texts = [
            fact["text"]
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        assert not any(text.startswith("GET /api/recipes/:id") for text in agent_texts)
        assert not any(text.startswith("@param") for text in agent_texts)
        assert not any(text.startswith("recipes: findAll") for text in agent_texts)

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_file_map_bullets_are_not_preserved_as_anchors(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "nothing_usable",
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: ok show me the recipe app file map.\n\n"
            "Assistant: The starter layout is:\n"
            "  - database.js` — SQLite setup, recipes table\n"
            "  - server.js` — Express REST API (CRUD endpoints)\n"
            "  - public/index.html` — Simple frontend to list/add recipes\n"
            "  - config/database.js` — Connection factory\n"
            "  - .gitignore` — node_modules, db, env\n"
            "  - .env.example` — Config template\n\n"
            "User: ok.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_texts = [
            fact["text"]
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        assert not any(text.startswith("database.js") for text in agent_texts)
        assert not any(text.startswith("server.js") for text in agent_texts)
        assert not any(text.startswith("public/index.html") for text in agent_texts)
        assert not any(text.startswith("config/database.js") for text in agent_texts)
        assert not any(text.startswith(".gitignore") for text in agent_texts)
        assert not any(text.startswith(".env.example") for text in agent_texts)

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_implementation_bullets_are_not_preserved_as_anchors(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "nothing_usable",
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: ok show me the implementation notes.\n\n"
            "Assistant: The main pieces are:\n"
            "  - A `meal_plans` table for weekly containers (week start date, name)\n"
            "  - Express middleware that logs request completion\n"
            "  - setDb() for test injection — same pattern as the error handler\n"
            "  - Parse a comma-separated ingredient string into structured objects\n"
            "  - 2 cups flour\" or \"1/2 tsp salt\". Falls back to the full text\n"
            "  - Generate a grocery list for a meal plan using GROUP BY aggregation\n\n"
            "User: ok.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_texts = [
            fact["text"]
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        assert not any(text.startswith("A `meal_plans` table") for text in agent_texts)
        assert not any(text.startswith("Express middleware") for text in agent_texts)
        assert not any(text.startswith("setDb() for test injection") for text in agent_texts)
        assert not any(text.startswith("Parse a comma-separated ingredient string") for text in agent_texts)
        assert not any(text.startswith("2 cups flour") for text in agent_texts)
        assert not any(text.startswith("Generate a grocery list") for text in agent_texts)

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_recipe_test_harness_bullets_are_not_preserved_as_anchors(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "nothing_usable",
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: ok show me the recipe app test and error-handler notes.\n\n"
            "Assistant: Here are the implementation notes:\n"
            "  - tests/setup.js` — test database setup with in-memory SQLite\n"
            "  - tests/helpers.js` — data factories and assertion utilities\n"
            "  - Custom application error with HTTP status code\n"
            "  - Operational errors (user input, not-found, auth) are safe to expose\n"
            "  - Express error-handling middleware (4 arguments)\n"
            "  - Catch-all 404 handler — mount before errorHandler\n\n"
            "User: ok.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_texts = [
            fact["text"]
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        assert not any(text.startswith("tests/setup.js") for text in agent_texts)
        assert not any(text.startswith("tests/helpers.js") for text in agent_texts)
        assert not any(text.startswith("Custom application error") for text in agent_texts)
        assert not any(text.startswith("Operational errors") for text in agent_texts)
        assert not any(text.startswith("Express error-handling middleware") for text in agent_texts)
        assert not any(text.startswith("Catch-all 404 handler") for text in agent_texts)

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_technical_meta_callback_is_not_preserved_as_anchor(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "nothing_usable",
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: can you redo the recipe app frontend?\n\n"
            "Assistant: Let me redesign the whole frontend. Give me a sec.\n\n"
            "User: ok.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_texts = [
            fact["text"]
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        assert not any(text.startswith("Let me redesign the whole frontend") for text in agent_texts)

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_code_snippet_list_is_not_preserved_as_anchor(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "nothing_usable",
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: ok show me the recipe test examples.\n\n"
            "Assistant: Here's the shape:\n"
            "describe('Recipe CRUD', () => { it('should create a recipe', () => { expect(true).toBe(true); }); });\n\n"
            "User: ok.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_texts = [
            fact["text"]
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        assert not any(text.startswith("describe('Recipe CRUD'") for text in agent_texts)

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_schema_feature_bullets_are_not_preserved_as_anchors(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "nothing_usable",
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: ok list the graphql and feature bullets.\n\n"
            "Assistant: Sure:\n"
            "  - Recipe with nested ingredientList, dietaryTags, prepTime, owner\n"
            "  - GroceryItem as a computed type (aggregated from ingredients)\n"
            "  - AuthPayload stub for when auth comes later\n"
            "  - SAFE_FOR_MOM constant is now exported (used by resolvers)\n"
            "  - Recipe CRUD — create, read, update, delete recipes\n"
            "  - Health check at `/health` for Docker healthcheck\n\n"
            "User: ok.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_texts = [
            fact["text"]
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        assert not any(text.startswith("Recipe with nested ingredientList") for text in agent_texts)
        assert not any(text.startswith("GroceryItem as a computed type") for text in agent_texts)
        assert not any(text.startswith("AuthPayload stub") for text in agent_texts)
        assert not any("SAFE_FOR_MOM constant is now exported" in text for text in agent_texts)
        assert not any(text.startswith("Recipe CRUD") for text in agent_texts)
        assert not any(text.startswith("Health check at") for text in agent_texts)

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_technical_summary_paragraph_is_not_preserved_as_anchor(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "nothing_usable",
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: ok what comes next for graphql?\n\n"
            "Assistant: I'll write a comprehensive test suite for the GraphQL resolvers. "
            "Tests for every query and mutation, dietary filtering, share code idempotency, "
            "field resolvers, and an explicit test that documents the N+1 behavior.\n\n"
            "User: ok.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_texts = [
            fact["text"]
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        assert not any(text.startswith("I'll write a comprehensive test suite") for text in agent_texts)

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_recipe_bootstrap_callback_is_not_preserved_as_anchor(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "nothing_usable",
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: ok how is the recipe app wired up?\n\n"
            "Assistant: Maya's recipe app uses SQLite with WAL mode for better concurrency. "
            "The .env file is gitignored and includes placeholders for future auth and nutrition API.\n\n"
            "User: ok.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_texts = [
            fact["text"]
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        assert not any(text.startswith("Maya's recipe app uses SQLite with WAL mode") for text in agent_texts)

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_dietary_endpoint_callback_is_not_preserved_as_anchor(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "nothing_usable",
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: ok what changed for dietary filtering?\n\n"
            "Assistant: Filtering is built into the main `/api/recipes` endpoint — "
            "`?safeForMom=true` for the preset, or `?diet=vegetarian,gluten-free` for custom filters. "
            "Also added a `/api/dietary-labels` endpoint so the frontend can fetch the label list dynamically.\n\n"
            "User: ok.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_texts = [
            fact["text"]
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        assert not any(text.startswith("Filtering is built into the main `/api/recipes` endpoint") for text in agent_texts)

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_validation_middleware_plan_is_not_preserved_as_anchor(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "nothing_usable",
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: ok what validator infrastructure did you add?\n\n"
            "Assistant: Build an Express middleware that validates req.body against a rules object.\n\n"
            "Assistant: /** Trim whitespace from string values. */ function trimValue(value) { "
            "return typeof value === 'string' ? value.trim() : value; }\n\n"
            "User: ok.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_texts = [
            fact["text"]
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        assert not any(text.startswith("Build an Express middleware") for text in agent_texts)
        assert not any(text.startswith("/** Trim whitespace from string values.") for text in agent_texts)

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_share_code_test_plan_is_not_preserved_as_anchor(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "nothing_usable",
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: ok what sharing tests should we add?\n\n"
            "Assistant: The missing cases are:\n"
            "  - Share code generation (creates a code, correct format, unique per recipe)\n"
            "  - Idempotency (sharing the same recipe twice returns the same code)\n"
            "  - Retrieval by code (correct recipe, handles invalid codes)\n"
            "  - Edge cases (deleted recipe cascade, unique constraint enforcement)\n\n"
            "User: ok.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_texts = [
            fact["text"]
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        assert not any(text.startswith("Share code generation") for text in agent_texts)
        assert not any(text.startswith("Idempotency (sharing the same recipe twice returns the same code)") for text in agent_texts)
        assert not any(text.startswith("Retrieval by code") for text in agent_texts)
        assert not any(text.startswith("Edge cases (deleted recipe cascade") for text in agent_texts)

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_rate_limit_plumbing_is_not_preserved_as_anchor(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "nothing_usable",
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: ok how are you wiring rate limiting?\n\n"
            "Assistant: We should also add rate limiting to the API. Right now there's nothing stopping someone from hammering the endpoints.\n\n"
            "Assistant: // Periodically purge expired entries to prevent unbounded memory growth. // The interval is unref'd so it doesn't keep the process alive.\n\n"
            "Assistant: Got it — I'll apply it to `/api` routes only. The health check and GraphQL endpoint stay unthrottled.\n\n"
            "User: ok.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_texts = [
            fact["text"]
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        assert not any(text.startswith("We should also add rate limiting to the API") for text in agent_texts)
        assert not any(text.startswith("// Periodically purge expired entries") for text in agent_texts)
        assert not any(text.startswith("Got it — I'll apply it to `/api` routes only") for text in agent_texts)

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_auth_wiring_and_todos_are_not_preserved_as_anchor(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "nothing_usable",
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: ok what auth plumbing did you add?\n\n"
            "Assistant: An auth middleware for protected routes.\n"
            "Assistant: User profiles (so each person can set their preferences).\n"
            "Assistant: // BUG: No authorization check — any user can update any recipe.\n"
            "Assistant: // NOTE: requireOwnership() is NOT implemented.\n"
            "Assistant: I'll add a `dietary_preferences` column to the users table — stored as a JSON array so people can have multiple restrictions.\n"
            "Assistant: That's +15 lines to database.js. The `dietary_preferences` column defaults to '[]'.\n"
            "Assistant: Committed! Auth is live — you and David can register, login, and save dietary preferences.\n\n"
            "User: ok.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_texts = [
            fact["text"]
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        assert not any(text.startswith("An auth middleware for protected routes") for text in agent_texts)
        assert not any(text.startswith("User profiles (so each person can set their preferences)") for text in agent_texts)
        assert not any(text.startswith("// BUG: No authorization check") for text in agent_texts)
        assert not any(text.startswith("// NOTE: requireOwnership() is NOT implemented") for text in agent_texts)
        assert not any(text.startswith("I'll add a `dietary_preferences` column to the users table") for text in agent_texts)
        assert not any(text.startswith("That's +15 lines to database.js") for text in agent_texts)
        assert not any(text.startswith("Committed! Auth is live") for text in agent_texts)

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_meal_plan_route_snippets_and_refactor_notes_are_not_preserved(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "nothing_usable",
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: ok what implementation cleanup did you do for meal planning?\n\n"
            "Assistant: // Search recipes app.get('/api/recipes/search', (req, res) => { return res.json([]); });\n\n"
            "Assistant: // Remove item from meal plan app.delete('/api/meal-plans/:planId/items/:itemId', (req, res) => { return res.json({ message: 'Item removed' }); });\n\n"
            "Assistant: Good call. Server.js is getting long with all the inline SQL. I'll create `src/db/queries.js` with three namespaces.\n\n"
            "Assistant: The setup now mirrors the full cumulative schema. Each test file gets a fresh in-memory database with all tables created.\n\n"
            "Assistant: Huge session! Nine files changed, almost 1500 lines added.\n\n"
            "User: ok.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_texts = [
            fact["text"]
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        assert not any(text.startswith("// Search recipes") for text in agent_texts)
        assert not any(text.startswith("// Remove item from meal plan") for text in agent_texts)
        assert not any(text.startswith("Good call. Server.js is getting long with all the inline SQL") for text in agent_texts)
        assert not any(text.startswith("The setup now mirrors the full cumulative schema") for text in agent_texts)
        assert not any(text.startswith("Huge session! Nine files changed") for text in agent_texts)

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_grocery_query_implementation_notes_are_not_preserved(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "nothing_usable",
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: ok how does the grocery aggregation work under the hood?\n\n"
            "Assistant: Foreign keys with ON DELETE CASCADE throughout. The `recipe_ingredients` table is what makes the grocery list work.\n\n"
            "Assistant: The grocery list query will GROUP BY name and unit, then SUM the amounts.\n\n"
            "Assistant: The grocery list query is the key — it joins meal_plan_items to recipe_ingredients to recipes.\n\n"
            "Assistant: Yeah, the sample data from session 3 is pretty thin.\n\n"
            "Assistant: Ha, art imitating life. The seed data should give us good coverage for testing the grocery list aggregation too.\n\n"
            "User: ok.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_texts = [
            fact["text"]
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        assert not any(text.startswith("Foreign keys with ON DELETE CASCADE throughout") for text in agent_texts)
        assert not any(text.startswith("The grocery list query will GROUP BY name and unit") for text in agent_texts)
        assert not any(text.startswith("The grocery list query is the key") for text in agent_texts)
        assert not any(text.startswith("Yeah, the sample data from session 3 is pretty thin") for text in agent_texts)
        assert not any(text.startswith("Ha, art imitating life.") for text in agent_texts)

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_code_shaped_recipe_app_paragraphs_are_not_preserved(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "nothing_usable",
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: ok what changed in the recipe internals?\n\n"
            "Assistant: // Search recipes app.get('/api/recipes/search', (req, res) => { "
            "const { q } = req.query; return res.json([]); });\n\n"
            "Assistant: const merged = { title: data.title ?? existing.title, dietary_tags: "
            "JSON.stringify(data.dietary_tags ?? existing.dietary_tags) };\n\n"
            "Assistant: search(query) { const db = getDb(); const pattern = `%${query}%`; "
            "return db.prepare('SELECT * FROM recipes').all(pattern); }\n\n"
            "Assistant: const items = db.prepare('SELECT * FROM meal_plan_items WHERE plan_id = ?').all(plan.id);\n\n"
            "User: ok.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_texts = [
            fact["text"]
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        assert not any(text.startswith("// Search recipes app.get") for text in agent_texts)
        assert not any(text.startswith("const merged = { title: data.title") for text in agent_texts)
        assert not any(text.startswith("search(query) { const db = getDb()") for text in agent_texts)
        assert not any(text.startswith("const items = db.prepare(") for text in agent_texts)

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_recipe_infra_and_test_summaries_are_not_preserved(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "nothing_usable",
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: ok summarize the implementation details.\n\n"
            "Assistant: 67 lines. Hooks into the response `finish` event so the log includes the actual status code and timing.\n\n"
            "Assistant: Two files. First, `seeds/sample-recipes.json` with 20 recipes:\n\n"
            "Assistant: 1. `tests/dietary.test.js` — SAFE_FOR_MOM preset and multi-tag intersection. "
            "2. `tests/mealplan.test.js` — plan CRUD, grocery aggregation, cascade deletes.\n\n"
            "Assistant: Dockerfile uses Alpine for a small image. docker-compose mounts SQLite data and .env read-only.\n\n"
            "Assistant: Environment-driven configuration. CORS origins are comma-separated and rate limiting is 100 requests per 15 minutes.\n\n"
            "Assistant: 638 lines. Covers all queries, all mutations, field resolvers, and the documented N+1 bug.\n\n"
            "Assistant: The N+1 test is especially important — it's not a failure test, it's a documentation test.\n\n"
            "Assistant: Concise README: features, quick start, API table, dev commands, Docker, tech stack.\n\n"
            "User: ok.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_texts = [
            fact["text"]
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        assert not any(text.startswith("67 lines. Hooks into the response `finish` event") for text in agent_texts)
        assert not any(text.startswith("Two files. First, `seeds/sample-recipes.json`") for text in agent_texts)
        assert not any(text.startswith("1. `tests/dietary.test.js`") for text in agent_texts)
        assert not any(text.startswith("Dockerfile uses Alpine") for text in agent_texts)
        assert not any(text.startswith("Environment-driven configuration") for text in agent_texts)
        assert not any(text.startswith("638 lines. Covers all queries") for text in agent_texts)
        assert not any(text.startswith("The N+1 test is especially important") for text in agent_texts)
        assert not any(text.startswith("Concise README:") for text in agent_texts)

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_graphql_schema_and_mount_bullets_are_not_preserved(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "nothing_usable",
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: ok what schema and infra bullets were added?\n\n"
            "Assistant: Sure:\n"
            "  - Ingredient with name, amount, unit, category\n"
            "  - MealPlanItem with day, meal type, and the full recipe\n"
            "  - New `recipe_shares` table with unique code constraint and CASCADE delete\n"
            "  - Apollo Server mounted at `/graphql` via expressMiddleware\n\n"
            "User: ok.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_texts = [
            fact["text"]
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        assert not any(text.startswith("Ingredient with name, amount, unit, category") for text in agent_texts)
        assert not any(text.startswith("MealPlanItem with day, meal type") for text in agent_texts)
        assert not any(text.startswith("New `recipe_shares` table") for text in agent_texts)
        assert not any(text.startswith("Apollo Server mounted at `/graphql`") for text in agent_texts)

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_graphql_exact_value_technical_summaries_are_not_preserved(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "nothing_usable",
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: ok let's add graphql but keep rest.\n\n"
            "Assistant: GraphQL at `/graphql`, REST at `/api/*`. Best of both worlds.\n\n"
            "User: ok what about the follow-up implementation notes?\n\n"
            "Assistant: I left a TODO comment in the resolver. It's not a problem at our current scale "
            "(we have maybe 20 recipes) but it'll bite us if the list grows.\n\n"
            "Assistant: Environment-driven configuration. CORS origins are comma-separated, rate limiting "
            "is 100 requests per 15 minutes per IP, pagination caps at 100 items.\n\n"
            "Assistant: The N+1 test is especially important — it's not a failure test, it's a documentation test.\n\n"
            "Assistant: Concise README: features, quick start, API table, dev commands, Docker, tech stack.\n\n"
            "User: ok.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_texts = [
            fact["text"]
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        assert not any(text.startswith("I left a TODO comment in the resolver") for text in agent_texts)
        assert not any(text.startswith("Environment-driven configuration.") for text in agent_texts)
        assert not any(text.startswith("The N+1 test is especially important") for text in agent_texts)
        assert not any(text.startswith("Concise README:") for text in agent_texts)

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_auth_config_and_test_count_summaries_are_not_preserved(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "nothing_usable",
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: ok what auth internals did you add?\n\n"
            "Assistant: 33 lines. All auth parameters centralized — JWT secret, algorithm, expiry, PBKDF2 iterations, key length, salt length.\n\n"
            "Assistant: 70 lines. Uses the real `jsonwebtoken` library — not hand-rolled HMAC. "
            "`requireAuth` verifies the JWT, attaches the decoded payload to `req.user`, and handles expired and invalid tokens separately.\n\n"
            "Assistant: 291 lines. Five test sections: User Registration, Password Hashing, Recipe Ownership, "
            "Test Token Generation, and User Queries.\n\n"
            "User: ok.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_texts = [
            fact["text"]
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        assert not any(text.startswith("33 lines. All auth parameters centralized") for text in agent_texts)
        assert not any(text.startswith("70 lines. Uses the real `jsonwebtoken` library") for text in agent_texts)
        assert not any(text.startswith("291 lines. Five test sections:") for text in agent_texts)

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_graphql_share_and_n_plus_one_meta_summaries_are_not_preserved(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "nothing_usable",
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: ok what did you wire for sharing and graphql internals?\n\n"
            "Assistant: The share endpoint is already set up: `shareRecipe` generates the code, and `/shared/:code` serves the recipe.\n\n"
            "Assistant: One share per recipe (unique index on recipe_id), and the code is unique too. CASCADE delete means if the recipe is deleted, the share link goes away.\n\n"
            "Assistant: No, you're right — it IS a real problem. It's called the N+1 query issue. For 50 recipes, that's 51 database queries.\n\n"
            "Assistant: For a personal app with maybe 20-30 recipes, it's fine. If it grows, we'd add DataLoader to batch those queries.\n\n"
            "Assistant: nanoid: ^3.3.7` (for share code generation, though we're using short custom wrappers).\n\n"
            "Assistant: Committed! GraphQL is live alongside REST, sharing is ready for Linda, and auth is queued for next session.\n\n"
            "User: ok.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_texts = [
            fact["text"]
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        assert not any(text.startswith("The share endpoint is already set up:") for text in agent_texts)
        assert not any(text.startswith("One share per recipe (unique index on recipe_id)") for text in agent_texts)
        assert not any(text.startswith("No, you're right — it IS a real problem. It's called the N+1 query issue") for text in agent_texts)
        assert not any(text.startswith("For a personal app with maybe 20-30 recipes") for text in agent_texts)
        assert not any(text.startswith("nanoid: ^3.3.7") for text in agent_texts)
        assert not any(text.startswith("Committed! GraphQL is live alongside REST") for text in agent_texts)

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_route_cleanup_summary_is_not_preserved_as_anchor(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "nothing_usable",
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: ok what else changed in the recipe server?\n\n"
            "Assistant: I also cleaned up the route handlers to use consistent error responses. "
            "The search endpoint now uses the parameterized query fix we discussed, and all routes "
            "return proper JSON error objects.\n\n"
            "User: ok.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_texts = [
            fact["text"]
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        assert not any(text.startswith("I also cleaned up the route handlers") for text in agent_texts)

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_recipe_app_upgrade_summary_is_not_preserved_as_anchor(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "nothing_usable",
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: ok how did the recipe app cleanup land?\n\n"
            "Assistant: The app went from \"spreadsheet\" to \"actual recipe app\" with proper tests and error handling. Nice upgrade.\n\n"
            "User: nice.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_texts = [
            fact["text"]
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        assert not any(text.startswith("The app went from \"spreadsheet\"") for text in agent_texts)

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_project_plan_anchor_is_not_preserved(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "nothing_usable",
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: can you add proper tests for the recipe app routes?\n\n"
            "Assistant: Absolutely. I'll set up a proper test suite with Jest. "
            "The SQL injection tests will be the headline, then parameterized query coverage.\n\n"
            "User: sounds good.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_texts = [
            fact["text"]
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        assert not any(text.startswith("Absolutely. I'll set up a proper test suite with Jest") for text in agent_texts)

    @patch("ingest.extract.call_deep_reasoning")
    def test_assistant_named_option_anchor_is_preserved_without_titlecase_spans(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "usable",
            "facts": [
                {
                    "text": "David planned a surprise birthday dinner for Linda",
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                    "privacy": "shared",
                }
            ],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        transcript = (
            "User: david quiere hacer una cena sorpresa para mi mamá.\n\n"
            "Assistant: el barrio de montrose es perfecto para eso.\n"
            "  - mercado comunal — más casual y flexible con dietas\n"
            "  - casa umi — japonesa, quizá demasiado elegante\n"
            "  ¿quieres que revise menús?\n\n"
            "User: lo casual suena mejor.\n\n"
            "Assistant: yo miraría bodega central, o fuego bbq para algo aún más relajado.\n"
            "  Ambos quedan cerca del centro.\n"
            "\nUser: sí, revisa menús.\n"
        )

        result = extract_from_transcript(
            transcript=transcript,
            owner_id="Maya Chen",
            dry_run=True,
        )

        agent_texts = [
            fact["text"]
            for fact in result["raw_facts"]
            if str(fact.get("speaker", "") or "").lower() == "agent"
        ]
        assert any(text.startswith("mercado comunal") for text in agent_texts)
        assert any(text.startswith("casa umi") for text in agent_texts)
        assert any("bodega central" in text.lower() and "fuego bbq" in text.lower() for text in agent_texts)

    @patch("ingest.extract.call_deep_reasoning")
    def test_explicit_structural_anchor_does_not_store_user_questions(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "nothing_usable",
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        result = extract_from_transcript(
            transcript="User: What is the codeword walnut-umbrella-7142?\n\nAssistant: I don't know.",
            owner_id="Solomon Steadman",
            dry_run=True,
        )

        assert result["explicit_structural_anchor_facts"] == 0
        assert result["raw_facts"] == []

    @patch("ingest.extract.call_deep_reasoning")
    def test_extraction_drops_injected_memory_and_session_artifact_facts(self, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "chunk_assessment": "usable",
            "facts": [
                {
                    "text": (
                        "BEGIN_QUOTED_NOTES ```text # Session: 2026-04-27 19:32:05 UTC "
                        "- **Session Key**: agent:main:matrix"
                    ),
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                },
                {
                    "text": (
                        "<injected_memories> - fact | Solomon does strength work Friday "
                        "</injected_memories>"
                    ),
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                },
                {
                    "text": "Solomon Steadman uses marker marigold-anvil-5816 for pumpkin seeds",
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                    "privacy": "private",
                },
            ],
            "soul_snippets": {},
            "journal_entries": {},
            "project_logs": {},
        }), 0.1)

        result = extract_from_transcript(
            transcript="User: My Friday ritual uses marker marigold-anvil-5816.\n\nAssistant: noted",
            owner_id="Solomon Steadman",
            dry_run=True,
        )

        texts = [fact["text"] for fact in result["raw_facts"]]
        assert result["artifact_facts_dropped"] == 2
        assert result["facts_skipped"] == 2
        assert texts == ["Solomon Steadman uses marker marigold-anvil-5816 for pumpkin seeds"]

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
    @patch("ingest.extract._session_bridge.list_session_chunks", return_value=[])
    @patch("ingest.extract._session_bridge.store_session_source_text")
    @patch("ingest.extract._memory.store")
    @patch("ingest.extract._memory.create_edge")
    def test_apply_extracted_payloads_can_publish_prior_dry_run_result(
        self,
        mock_edge,
        mock_store,
        mock_store_source_chunks,
        _mock_list_source_chunks,
        mock_llm,
        mock_opus_response,
    ):
        from ingest.extract import apply_extracted_payloads, extract_from_transcript

        mock_llm.return_value = (mock_opus_response, 1.0)
        mock_store_source_chunks.return_value = [
            {
                "chunk_id": "sch_staged",
                "status": "created",
                "text": "User: I like coffee\n\nAssistant: noted",
                "chunk_index": 0,
            }
        ]
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

        assert staged["raw_source_chunks"]
        assert all("_source_chunk_ref" in fact for fact in staged["raw_facts"])
        mock_store_source_chunks.assert_not_called()

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
        assert applied["source_chunks_stored"] == 1
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
        assert mock_store_source_chunks.call_count == 1
        source_chunk_call = mock_store_source_chunks.call_args.kwargs
        assert source_chunk_call["text"] == "User: I like coffee\n\nAssistant: noted"
        assert source_chunk_call["session_id"] == "sess-stage"
        assert source_chunk_call["start_index"] == 0
        assert source_chunk_call["chunk_kind"] == "micro"
        assert {call.kwargs["source_chunk_id"] for call in mock_store.call_args_list} == {"sch_staged"}
        first_call = mock_store.call_args_list[0].kwargs
        second_call = mock_store.call_args_list[1].kwargs
        assert first_call["confidence"] == pytest.approx(0.9)
        assert first_call["extraction_confidence"] == pytest.approx(0.9)
        assert first_call["provenance_confidence"] == pytest.approx(0.9)
        assert second_call["confidence"] == pytest.approx(0.6)
        assert second_call["extraction_confidence"] == pytest.approx(0.6)
        assert second_call["provenance_confidence"] == pytest.approx(0.6)

    def test_apply_extracted_payloads_delegates_memory_publish_through_memorydb_contract(self, monkeypatch):
        import ingest.extract as extract_mod

        seen = {}

        def fake_publish(result, **kwargs):
            seen["kwargs"] = kwargs
            result["facts_stored"] = 1
            result["facts_skipped"] = 0
            result["edges_created"] = 0
            result.setdefault("facts", []).append({
                "text": "Maya moved the launch checklist into the red binder",
                "status": "stored",
                "edges": [],
            })
            return [{
                "text": "Maya moved the launch checklist into the red binder",
                "domains": ["project"],
                "project": "launch-app",
            }]

        fake_enqueue = MagicMock(return_value={
            "entries_seen": 1,
            "entries_queued": 1,
            "projects_queued": 1,
            "queue_failures": 0,
        })
        monkeypatch.setattr(
            "core.plugins.memorydb_contract.run_extraction_publish_payload",
            fake_publish,
        )
        monkeypatch.setattr(extract_mod, "enqueue_project_logs", fake_enqueue)

        payload = {
            "raw_facts": [{
                "text": "Maya moved the launch checklist into the red binder",
                "category": "fact",
                "speaker": "user",
                "domains": ["project"],
                "project": "launch-app",
            }],
            "raw_snippets": {"SOUL.md": ["Keep launch checklist references precise"]},
            "raw_journal": {},
            "raw_project_logs": {"launch-app": ["Moved launch checklist into red binder"]},
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

        applied = extract_mod.apply_extracted_payloads(
            payload,
            owner_id="test",
            label="flush",
            session_id="sess-contract",
            write_snippets=False,
            write_journal=False,
            dry_run=False,
        )

        assert applied["facts_stored"] == 1
        assert applied["snippets"]["SOUL.md"] == ["Keep launch checklist references precise"]
        assert applied["project_logs"]["launch-app"] == ["Moved launch checklist into red binder"]
        fake_enqueue.assert_called_once()
        kwargs = seen["kwargs"]
        assert kwargs["memory_service"] is extract_mod._memory
        assert kwargs["session_bridge"] is extract_mod._session_bridge
        assert kwargs["snippet_files"] == 1
        assert kwargs["project_log_projects"] == 1
        assert "normalize_fact_temporal_hint" not in kwargs
        assert "collapse_duplicate_payload_facts" not in kwargs
        assert "normalize_fact_provenance" not in kwargs
        assert "write_publish_trace" not in kwargs
        assert "publish_batch_size" not in kwargs
        assert "default_session_microchunk_tokens" not in kwargs

    def test_apply_extracted_payloads_request_mode_routes_through_memorydb_request(self, monkeypatch):
        import ingest.extract as extract_mod

        called = {}

        def fake_register():
            called["registered"] = True

        def fake_direct_publish(*_args, **_kwargs):
            raise AssertionError("request mode must not fall back to direct publish helper")

        def fake_request(event_type, payload, **kwargs):
            called["event_type"] = event_type
            called["payload"] = payload
            called["kwargs"] = kwargs
            publish_result = dict(payload["result"])
            publish_result["facts_stored"] = 1
            publish_result["facts_skipped"] = 0
            publish_result["edges_created"] = 0
            publish_result["facts"] = [{
                "text": "Maya moved the launch checklist into the red binder",
                "status": "stored",
                "edges": [],
            }]
            return {
                "status": "ok",
                "responses": [{
                    "datastore_id": "memorydb",
                    "status": "ok",
                    "result": {
                        "status": "ok",
                        "publish_result": publish_result,
                        "facts_for_orchestration": [{
                            "text": "Maya moved the launch checklist into the red binder",
                            "domains": ["project"],
                            "project": "launch-app",
                        }],
                    },
                }],
            }

        fake_enqueue = MagicMock(return_value={
            "entries_seen": 1,
            "entries_queued": 1,
            "projects_queued": 1,
            "queue_failures": 0,
        })
        monkeypatch.setattr(
            "core.plugins.memorydb_contract.register_extraction_publish_request_handler",
            fake_register,
        )
        monkeypatch.setattr(
            "core.plugins.memorydb_contract.run_extraction_publish_payload",
            fake_direct_publish,
        )
        monkeypatch.setattr("core.runtime.events.request_broker_event", fake_request)
        monkeypatch.setattr(extract_mod, "enqueue_project_logs", fake_enqueue)

        payload = {
            "raw_facts": [{
                "text": "Maya moved the launch checklist into the red binder",
                "category": "fact",
                "speaker": "user",
                "domains": ["project"],
                "project": "launch-app",
            }],
            "raw_snippets": {"SOUL.md": ["Keep launch checklist references precise"]},
            "raw_journal": {},
            "raw_project_logs": {"launch-app": ["Moved launch checklist into red binder"]},
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

        applied = extract_mod.apply_extracted_payloads(
            payload,
            owner_id="test",
            label="rolling-flush",
            session_id="sess-request",
            source_channel="codex",
            target_datastore="memorydb",
            source_conversation_id="conv-request",
            participant_entity_ids=["entity:user"],
            write_snippets=False,
            write_journal=False,
            dry_run=False,
            memory_publish_mode="request",
        )

        assert called["registered"] is True
        assert called["event_type"] == "memory.extraction_publish.request.v1"
        assert called["payload"]["source"] == "daemon-final-rolling-flush"
        assert called["payload"]["owner_id"] == "test"
        assert called["payload"]["session_id"] == "sess-request"
        assert called["payload"]["source_channel"] == "codex"
        assert called["payload"]["target_datastore"] == "memorydb"
        assert called["payload"]["source_conversation_id"] == "conv-request"
        assert called["payload"]["participant_entity_ids"] == ["entity:user"]
        assert called["payload"]["snippet_files"] == 1
        assert called["payload"]["project_log_projects"] == 1
        assert called["kwargs"]["source"] == "ingest.extract.apply_extracted_payloads"
        assert applied["facts_stored"] == 1
        assert applied["snippets"]["SOUL.md"] == ["Keep launch checklist references precise"]
        assert applied["project_logs"]["launch-app"] == ["Moved launch checklist into red binder"]
        fake_enqueue.assert_called_once()

    def test_apply_extracted_payloads_routes_snippet_journal_through_evolutiondb_contract(self, monkeypatch):
        import ingest.extract as extract_mod

        seen = {}

        def fake_publish(result, **_kwargs):
            result["facts_stored"] = 0
            return []

        def fake_snippet_journal(payload):
            seen["payload"] = payload
            return {
                "status": "ok",
                "snippet_files_seen": 1,
                "snippet_items_seen": 1,
                "snippet_files_written": 1,
                "snippet_items_written": 1,
                "snippet_files_skipped": 0,
                "journal_files_seen": 1,
                "journal_files_written": 1,
                "journal_files_skipped": 0,
                "target_files": {
                    "snippets": ["SOUL.snippets.md"],
                    "journal": ["SOUL.journal.md"],
                },
                "errors": [],
            }

        monkeypatch.setattr("core.plugins.memorydb_contract.run_extraction_publish_payload", fake_publish)
        monkeypatch.setattr("core.plugins.notedb_contract.run_snippet_journal_write_payload", fake_snippet_journal)

        payload = {
            "raw_facts": [],
            "raw_snippets": {"SOUL.md": ["Keep launch checklist references precise"]},
            "raw_journal": {"SOUL.md": "A quiet launch note."},
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

        applied = extract_mod.apply_extracted_payloads(
            payload,
            owner_id="test",
            label="rolling-compaction",
            session_id="sess-evolution-seam",
            write_snippets=True,
            write_journal=True,
            dry_run=False,
        )

        helper_payload = seen["payload"]
        assert helper_payload["source"] == "extraction-apply-payloads"
        assert helper_payload["owner_id"] == "test"
        assert helper_payload["session_id"] == "sess-evolution-seam"
        assert helper_payload["trigger"] == "Compaction"
        assert helper_payload["snippets"] == {"SOUL.md": ["Keep launch checklist references precise"]}
        assert helper_payload["journal"] == {"SOUL.md": "A quiet launch note."}
        assert helper_payload["write_snippets"] is True
        assert helper_payload["write_journal"] is True
        assert helper_payload["dry_run"] is False
        assert applied["snippet_journal_metrics"]["target_files"] == {
            "snippets": ["SOUL.snippets.md"],
            "journal": ["SOUL.journal.md"],
        }

    def test_apply_extracted_payloads_request_mode_does_not_fallback_after_broker_failure(self, monkeypatch):
        import ingest.extract as extract_mod

        direct_called = False

        def fake_direct_publish(*_args, **_kwargs):
            nonlocal direct_called
            direct_called = True
            raise AssertionError("request-mode failure must not route around broker")

        monkeypatch.setattr(
            "core.plugins.memorydb_contract.register_extraction_publish_request_handler",
            lambda: None,
        )
        monkeypatch.setattr(
            "core.plugins.memorydb_contract.run_extraction_publish_payload",
            fake_direct_publish,
        )
        monkeypatch.setattr(
            "core.runtime.events.request_broker_event",
            lambda *_args, **_kwargs: {
                "status": "failed",
                "error": "simulated broker failure",
                "responses": [],
            },
        )

        payload = {
            "raw_facts": [{"text": "Maya keeps the launch checklist in the red binder"}],
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

        with pytest.raises(RuntimeError, match="extraction publish request returned no memorydb response"):
            extract_mod.apply_extracted_payloads(
                payload,
                owner_id="test",
                label="rolling-flush",
                session_id="sess-request-fail",
                dry_run=False,
                memory_publish_mode="request",
            )

        assert direct_called is False
        assert payload["facts_stored"] == 0

    def test_apply_extracted_payloads_routes_snippet_journal_request_mode_through_broker(self, monkeypatch):
        import ingest.extract as extract_mod

        called = {}
        direct_called = False

        def fake_publish(result, **_kwargs):
            result["facts_stored"] = 0
            return []

        def fake_direct_snippet_journal(*_args, **_kwargs):
            nonlocal direct_called
            direct_called = True
            raise AssertionError("snippet/journal request mode must not call direct helper")

        def fake_request(event_type, payload, **kwargs):
            called["event_type"] = event_type
            called["payload"] = payload
            called["kwargs"] = kwargs
            return {
                "status": "ok",
                "responses": [{
                    "datastore_id": "evolutiondb",
                    "status": "ok",
                    "result": {
                        "status": "ok",
                        "snippet_journal_metrics": {
                            "status": "ok",
                            "snippet_files_seen": 1,
                            "snippet_items_seen": 1,
                            "snippet_files_written": 1,
                            "snippet_items_written": 1,
                            "snippet_files_skipped": 0,
                            "journal_files_seen": 1,
                            "journal_files_written": 1,
                            "journal_files_skipped": 0,
                            "target_files": {
                                "snippets": ["SOUL.snippets.md"],
                                "journal": ["SOUL.journal.md"],
                            },
                            "errors": [],
                        },
                    },
                }],
            }

        monkeypatch.setattr("core.plugins.memorydb_contract.run_extraction_publish_payload", fake_publish)
        monkeypatch.setattr("core.plugins.notedb_contract.run_snippet_journal_write_payload", fake_direct_snippet_journal)
        monkeypatch.setattr("core.plugins.notedb_contract.register_snippet_journal_write_request_handler", lambda: None)
        monkeypatch.setattr("core.runtime.events.request_broker_event", fake_request)

        payload = {
            "raw_facts": [],
            "raw_snippets": {"SOUL.md": ["Keep launch checklist references precise"]},
            "raw_journal": {"SOUL.md": "A quiet launch note."},
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

        applied = extract_mod.apply_extracted_payloads(
            payload,
            owner_id="test",
            label="rolling-flush",
            session_id="sess-note-request",
            write_snippets=True,
            write_journal=True,
            dry_run=False,
            snippet_journal_write_mode="request",
        )

        assert direct_called is False
        assert called["event_type"] == "evolution.snippet_journal_write.request.v1"
        assert called["payload"]["source"] == "extraction-apply-payloads"
        assert called["payload"]["owner_id"] == "test"
        assert called["payload"]["session_id"] == "sess-note-request"
        assert called["payload"]["trigger"] == "CLI"
        assert called["payload"]["snippets"] == {"SOUL.md": ["Keep launch checklist references precise"]}
        assert called["payload"]["journal"] == {"SOUL.md": "A quiet launch note."}
        assert called["kwargs"]["source"] == "ingest.extract.apply_extracted_payloads"
        assert applied["snippet_journal_metrics"]["target_files"] == {
            "snippets": ["SOUL.snippets.md"],
            "journal": ["SOUL.journal.md"],
        }

    def test_apply_extracted_payloads_snippet_journal_request_mode_does_not_fallback_after_broker_failure(self, monkeypatch):
        import ingest.extract as extract_mod

        direct_called = False

        def fake_publish(result, **_kwargs):
            result["facts_stored"] = 0
            return []

        def fake_direct_snippet_journal(*_args, **_kwargs):
            nonlocal direct_called
            direct_called = True
            raise AssertionError("request-mode failure must not route around direct snippet/journal helper")

        monkeypatch.setattr("core.plugins.memorydb_contract.run_extraction_publish_payload", fake_publish)
        monkeypatch.setattr("core.plugins.notedb_contract.run_snippet_journal_write_payload", fake_direct_snippet_journal)
        monkeypatch.setattr("core.plugins.notedb_contract.register_snippet_journal_write_request_handler", lambda: None)
        monkeypatch.setattr(
            "core.runtime.events.request_broker_event",
            lambda *_args, **_kwargs: {
                "status": "failed",
                "error": "simulated snippet broker failure",
                "responses": [],
            },
        )

        payload = {
            "raw_facts": [],
            "raw_snippets": {"SOUL.md": ["Keep launch checklist references precise"]},
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

        with pytest.raises(RuntimeError, match="snippet/journal write request returned no evolutiondb response"):
            extract_mod.apply_extracted_payloads(
                payload,
                owner_id="test",
                label="rolling-flush",
                session_id="sess-note-request-fail",
                dry_run=False,
                snippet_journal_write_mode="request",
            )

        assert direct_called is False
        assert "snippet_journal_metrics" not in payload

    def test_apply_extracted_payloads_rejects_unknown_snippet_journal_write_mode(self, monkeypatch):
        import ingest.extract as extract_mod

        def fake_publish(result, **_kwargs):
            result["facts_stored"] = 0
            return []

        monkeypatch.setattr("core.plugins.memorydb_contract.run_extraction_publish_payload", fake_publish)

        payload = {
            "raw_facts": [],
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

        with pytest.raises(ValueError, match="Unsupported snippet_journal_write_mode"):
            extract_mod.apply_extracted_payloads(
                payload,
                owner_id="test",
                label="rolling-flush",
                session_id="sess-note-bad-mode",
                dry_run=False,
                snippet_journal_write_mode="bogus",
            )

    @pytest.mark.parametrize(
        ("response", "message"),
        [
            ([], "extraction publish request returned a non-object response"),
            (
                {"status": "failed", "error": "simulated broker failure", "responses": []},
                "extraction publish request returned no memorydb response: simulated broker failure",
            ),
            (
                {"status": "ok", "responses": ["bad-row"]},
                "extraction publish request returned malformed memorydb response",
            ),
            (
                {"status": "ok", "responses": [{"datastore_id": "docsdb", "status": "ok", "result": {}}]},
                "extraction publish request returned a non-memorydb response",
            ),
            (
                {"status": "ok", "responses": [{"datastore_id": "memorydb", "status": "ok", "result": []}]},
                "extraction publish request memorydb result is not an object",
            ),
            (
                {
                    "status": "failed",
                    "responses": [{
                        "datastore_id": "memorydb",
                        "status": "failed",
                        "result": {"status": "failed", "error": "handler rejected publish"},
                    }],
                },
                "extraction publish request failed: handler rejected publish",
            ),
            (
                {
                    "status": "ok",
                    "responses": [{
                        "datastore_id": "memorydb",
                        "status": "ok",
                        "result": {"status": "ok", "facts_for_orchestration": []},
                    }],
                },
                "extraction publish request memorydb publish_result is not an object",
            ),
            (
                {
                    "status": "ok",
                    "responses": [{
                        "datastore_id": "memorydb",
                        "status": "ok",
                        "result": {"status": "ok", "publish_result": {}, "facts_for_orchestration": {}},
                    }],
                },
                "extraction publish request memorydb facts_for_orchestration is not a list",
            ),
        ],
    )
    def test_validate_extraction_publish_broker_response_warns_before_raise(self, caplog, response, message):
        import ingest.extract as extract_mod

        caplog.set_level("WARNING", logger="ingest.extract")

        with pytest.raises(RuntimeError, match=message):
            extract_mod._validate_extraction_publish_broker_response(response)

        assert any(message in record.getMessage() for record in caplog.records)

    @pytest.mark.parametrize(
        ("response", "message"),
        [
            ([], "snippet/journal write request returned a non-object response"),
            (
                {"status": "failed", "error": "simulated broker failure", "responses": []},
                "snippet/journal write request returned no evolutiondb response: simulated broker failure",
            ),
            (
                {"status": "ok", "responses": ["bad-row"]},
                "snippet/journal write request returned malformed evolutiondb response",
            ),
            (
                {"status": "ok", "responses": [{"datastore_id": "memorydb", "status": "ok", "result": {}}]},
                "snippet/journal write request returned a non-evolutiondb response",
            ),
            (
                {"status": "ok", "responses": [{"datastore_id": "evolutiondb", "status": "ok", "result": []}]},
                "snippet/journal write request evolutiondb result is not an object",
            ),
            (
                {
                    "status": "failed",
                    "responses": [{
                        "datastore_id": "evolutiondb",
                        "status": "failed",
                        "result": {"status": "failed", "error": "handler rejected snippet write"},
                    }],
                },
                "snippet/journal write request failed: handler rejected snippet write",
            ),
            (
                {
                    "status": "ok",
                    "responses": [{
                        "datastore_id": "evolutiondb",
                        "status": "ok",
                        "result": {"status": "ok"},
                    }],
                },
                "snippet/journal write request evolutiondb snippet_journal_metrics is not an object",
            ),
            (
                {
                    "status": "ok",
                    "responses": [{
                        "datastore_id": "evolutiondb",
                        "status": "ok",
                        "result": {
                            "status": "ok",
                            "snippet_journal_metrics": {"status": "ok", "errors": []},
                        },
                    }],
                },
                "snippet/journal write request evolutiondb target_files is not an object",
            ),
            (
                {
                    "status": "ok",
                    "responses": [{
                        "datastore_id": "evolutiondb",
                        "status": "ok",
                        "result": {
                            "status": "ok",
                            "snippet_journal_metrics": {
                                "status": "ok",
                                "target_files": {"snippets": [], "journal": []},
                            },
                        },
                    }],
                },
                "snippet/journal write request evolutiondb errors is not a list",
            ),
        ],
    )
    def test_validate_snippet_journal_write_broker_response_warns_before_raise(self, caplog, response, message):
        import ingest.extract as extract_mod

        caplog.set_level("WARNING", logger="ingest.extract")

        with pytest.raises(RuntimeError, match=message):
            extract_mod._validate_snippet_journal_write_broker_response(response)

        assert any(message in record.getMessage() for record in caplog.records)

    @patch("ingest.extract._memory.store")
    def test_apply_extracted_payloads_collapses_exact_duplicate_fact_rows(self, mock_store):
        from ingest.extract import apply_extracted_payloads

        mock_store.return_value = {"id": "n1", "status": "created", "dedup_telemetry": {}}

        payload = {
            "raw_facts": [
                {
                    "text": "Maya's half marathon finish time was 2:14",
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["personal"],
                    "extraction_confidence": "medium",
                    "keywords": "half marathon time",
                },
                {
                    "text": "  Maya's half marathon finish time was 2:14  ",
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["health", "personal"],
                    "extraction_confidence": "high",
                    "keywords": "running exact time",
                    "created_at": "2026-03-12T23:59:59",
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
        assert "created_at" not in call
        assert call["mentioned_at"] == "2026-03-12T23:59:59"
        assert sorted(call["domains"]) == ["health", "personal"]

    @patch("ingest.extract._memory.store")
    def test_apply_extracted_payloads_resolves_domain_policy_inside_memorydb_boundary(
        self,
        mock_store,
        monkeypatch,
    ):
        from ingest.extract import apply_extracted_payloads

        mock_store.return_value = {"id": "n-domain", "status": "created", "dedup_telemetry": {}}
        monkeypatch.setattr(
            "datastore.memorydb.extraction_publish.get_config",
            lambda: SimpleNamespace(retrieval=SimpleNamespace(domains={"personal": "Personal facts"})),
        )

        payload = {
            "raw_facts": [{
                "text": "Maya prefers jasmine tea in the morning",
                "category": "fact",
                "speaker": "user",
                "domains": ["personal"],
                "extraction_confidence": "high",
            }],
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
            session_id="sess-domain",
            dry_run=False,
            allowed_domains={"project"},
        )

        assert applied["facts_stored"] == 1
        assert applied["facts_skipped"] == 0
        assert mock_store.call_args.kwargs["domains"] == ["personal"]

    @patch("ingest.extract.is_fail_hard_enabled", return_value=True)
    @patch("ingest.extract._memory.store")
    def test_apply_extracted_payloads_raises_on_domain_policy_failure_under_failhard(
        self,
        mock_store,
        _mock_failhard,
        monkeypatch,
    ):
        from ingest.extract import apply_extracted_payloads

        monkeypatch.setattr(
            "datastore.memorydb.extraction_publish.get_config",
            lambda: (_ for _ in ()).throw(RuntimeError("config unavailable")),
        )

        payload = {
            "raw_facts": [{
                "text": "Maya prefers jasmine tea in the morning",
                "category": "fact",
                "speaker": "user",
                "domains": ["personal"],
                "extraction_confidence": "high",
            }],
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

        with pytest.raises(RuntimeError, match="Failed to resolve MemoryDB extraction publish domains"):
            apply_extracted_payloads(
                payload,
                owner_id="test",
                label="flush",
                session_id="sess-domain-failhard",
                dry_run=False,
            )
        mock_store.assert_not_called()

    def test_memorydb_extraction_publish_initializes_facts_planned_for_dry_run(self, monkeypatch):
        from datastore.memorydb.extraction_publish import run_extraction_publish_payload

        monkeypatch.setattr(
            "datastore.memorydb.extraction_publish.get_config",
            lambda: SimpleNamespace(retrieval=SimpleNamespace(domains={"personal": "Personal facts"})),
        )
        result = {
            "raw_facts": [{
                "text": "Maya keeps the launch checklist in the red binder",
                "category": "fact",
                "speaker": "user",
                "domains": ["personal"],
            }],
        }

        returned = run_extraction_publish_payload(
            result,
            owner_id="test",
            label="unit",
            session_id="sess-dry-run",
            actor_id=None,
            speaker_entity_id=None,
            subject_entity_id=None,
            source_channel=None,
            target_datastore=None,
            source_conversation_id=None,
            participant_entity_ids=None,
            source_author_id=None,
            dry_run=True,
            snippet_files=0,
            journal_files=0,
            project_log_projects=0,
            memory_service=object(),
            session_bridge=object(),
            fail_hard_enabled=lambda: False,
        )

        assert returned[0]["text"] == result["raw_facts"][0]["text"]
        assert "mentioned_at" in returned[0]
        assert result["facts_planned"] == 1
        assert result["facts"][0]["status"] == "would_store"

    @patch("ingest.extract._memory.store")
    def test_apply_extracted_payloads_passes_temporal_provenance_to_store(self, mock_store):
        from ingest.extract import apply_extracted_payloads

        mock_store.return_value = {"id": "n-time", "status": "created", "dedup_telemetry": {}}

        payload = {
            "raw_facts": [
                {
                    "text": "Maya attended the spring pottery workshop",
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                    "privacy": "shared",
                    "created_at": "2026-05-06T10:31:00",
                    "occurred_start": "2026-03-01T23:59:59",
                    "occurred_end": "2026-03-31T23:59:59",
                    "mentioned_at": "2026-05-06T10:30:00",
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
            session_id="sess-time",
            dry_run=False,
        )

        assert applied["facts_stored"] == 1
        call = mock_store.call_args.kwargs
        assert "created_at" not in call
        assert call["occurred_start"] == "2026-03-01T23:59:59"
        assert call["occurred_end"] == "2026-03-31T23:59:59"
        assert call["mentioned_at"] == "2026-05-06T10:30:00"

    def test_merge_parsed_payloads_attaches_source_chunk_id_to_facts(self):
        from ingest.extract import _merge_parsed_payloads

        all_facts = []
        result = {
            "chunks_processed": 0,
            "facts_skipped": 0,
            "unsupported_specificity_facts_dropped": 0,
        }

        _merge_parsed_payloads(
            [
                {
                    "facts": [
                        {
                            "text": "Ada stores the launch checklist in the red binder.",
                            "category": "fact",
                            "speaker": "user",
                            "domains": ["project"],
                            "extraction_confidence": "high",
                        }
                    ]
                }
            ],
            transcript_text="User: Ada stores the launch checklist in the red binder.",
            all_facts=all_facts,
            all_snippets={},
            all_journal={},
            all_project_logs={},
            result=result,
            chunk_label="1",
            label="unit",
            source_chunk_id="sch_testchunk",
        )

        assert all_facts[0]["_source_chunk_id"] == "sch_testchunk"
        assert all_facts[0]["_source_chunk_index"] == "1"

    def test_merge_parsed_payloads_attaches_source_chunk_ref_to_staged_facts(self):
        from ingest.extract import _merge_parsed_payloads

        all_facts = []
        result = {
            "chunks_processed": 0,
            "facts_skipped": 0,
            "unsupported_specificity_facts_dropped": 0,
        }

        _merge_parsed_payloads(
            [
                {
                    "facts": [
                        {
                            "text": "Ada stores the launch checklist in the red binder.",
                            "category": "fact",
                            "speaker": "user",
                            "domains": ["project"],
                            "extraction_confidence": "high",
                        }
                    ]
                }
            ],
            transcript_text="User: Ada stores the launch checklist in the red binder.",
            all_facts=all_facts,
            all_snippets={},
            all_journal={},
            all_project_logs={},
            result=result,
            chunk_label="1",
            label="unit",
            source_chunk_ref="chunk:unitref",
        )

        assert all_facts[0]["_source_chunk_ref"] == "chunk:unitref"
        assert all_facts[0]["_source_chunk_index"] == "1"

    @patch("ingest.extract._session_bridge.list_session_chunks", return_value=[])
    @patch("ingest.extract._session_bridge.store_session_source_text")
    @patch("ingest.extract._memory.store")
    def test_extract_from_transcript_stores_source_chunk_and_links_facts(
        self,
        mock_store,
        mock_store_source_chunks,
        _mock_list_source_chunks,
        mock_opus_response,
    ):
        from ingest.extract import extract_from_transcript

        mock_store_source_chunks.return_value = [
            {
                "chunk_id": "sch_extract_1",
                "status": "created",
                "text": "User: I like coffee\n\nAssistant: noted",
                "chunk_index": 0,
            }
        ]
        mock_store.return_value = {"id": "node-1", "status": "created", "dedup_telemetry": {}}

        with patch("ingest.extract.call_deep_reasoning", return_value=(mock_opus_response, 1.0)):
            result = extract_from_transcript(
                transcript="User: I like coffee\n\nAssistant: noted",
                owner_id="test",
                label="chunklink",
                session_id="sess-chunklink",
                source_channel="test",
                source_conversation_id="conv-chunklink",
                source_author_id="author-chunklink",
                write_snippets=False,
                write_journal=False,
                dry_run=False,
            )

        assert result["source_chunks_stored"] == 1
        assert mock_store_source_chunks.call_args.kwargs["session_id"] == "sess-chunklink"
        assert mock_store_source_chunks.call_args.kwargs["start_index"] == 0
        assert mock_store_source_chunks.call_args.kwargs["chunk_kind"] == "micro"
        assert mock_store.call_count == 2
        assert {call.kwargs["source_chunk_id"] for call in mock_store.call_args_list} == {"sch_extract_1"}

    @patch("ingest.extract.is_fail_hard_enabled", return_value=True)
    @patch("ingest.extract._session_bridge.list_session_chunks", return_value=[])
    @patch("ingest.extract._session_bridge.store_session_source_text", side_effect=RuntimeError("chunk store failed"))
    def test_apply_extracted_payloads_raises_when_source_chunk_store_fails_under_failhard(
        self,
        _mock_store_source_chunks,
        _mock_list_source_chunks,
        _mock_fail_hard,
    ):
        from ingest.extract import apply_extracted_payloads

        payload = {
            "raw_facts": [
                {
                    "text": "Ada keeps the launch checklist in the red binder.",
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["project"],
                    "extraction_confidence": "high",
                    "_source_chunk_ref": "chunk:failhard",
                },
            ],
            "raw_source_chunks": [
                {
                    "source_chunk_ref": "chunk:failhard",
                    "text": "User: Ada keeps the launch checklist in the red binder.",
                    "source_id": "sess-failhard",
                    "session_id": "sess-failhard",
                    "chunk_index": 0,
                }
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

        with pytest.raises(RuntimeError, match="Failed to store extraction source chunk"):
            apply_extracted_payloads(
                payload,
                owner_id="test",
                label="flush",
                session_id="sess-failhard",
                dry_run=False,
            )

    @patch("ingest.extract.is_fail_hard_enabled", return_value=True)
    @patch("ingest.extract._session_bridge.list_session_chunks", return_value=[])
    @patch("ingest.extract._session_bridge.store_session_source_text", return_value=[{"status": "created"}])
    def test_apply_extracted_payloads_raises_when_source_chunk_store_returns_no_id_under_failhard(
        self,
        _mock_store_source_chunks,
        _mock_list_source_chunks,
        _mock_fail_hard,
    ):
        from ingest.extract import apply_extracted_payloads

        payload = {
            "raw_facts": [
                {
                    "text": "Ada keeps the launch checklist in the red binder.",
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["project"],
                    "extraction_confidence": "high",
                    "_source_chunk_ref": "chunk:noid",
                },
            ],
            "raw_source_chunks": [
                {
                    "source_chunk_ref": "chunk:noid",
                    "text": "User: Ada keeps the launch checklist in the red binder.",
                    "source_id": "sess-noid",
                    "session_id": "sess-noid",
                    "chunk_index": 0,
                }
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

        with pytest.raises(RuntimeError, match="Source chunk store returned no chunk_id"):
            apply_extracted_payloads(
                payload,
                owner_id="test",
                label="flush",
                session_id="sess-noid",
                dry_run=False,
            )

    @patch("ingest.extract._memory.store")
    def test_apply_extracted_payloads_passes_source_chunk_id_to_store(self, mock_store):
        from ingest.extract import apply_extracted_payloads

        mock_store.return_value = {"id": "n-source-chunk", "status": "created", "dedup_telemetry": {}}

        payload = {
            "raw_facts": [
                {
                    "text": "Ada keeps the launch checklist in the red binder.",
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["project"],
                    "extraction_confidence": "high",
                    "_source_chunk_id": "sch_fact_evidence",
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
            session_id="sess-source-chunk",
            dry_run=False,
        )

        assert applied["facts_stored"] == 1
        assert mock_store.call_args.kwargs["source_chunk_id"] == "sch_fact_evidence"

    @patch("ingest.extract._session_bridge.list_session_chunks", return_value=[])
    @patch("ingest.extract._session_bridge.store_session_source_text")
    @patch("ingest.extract._memory.store")
    def test_apply_extracted_payloads_maps_each_fact_to_its_referenced_chunk(
        self,
        mock_store,
        mock_store_source_chunks,
        _mock_list_source_chunks,
    ):
        from ingest.extract import apply_extracted_payloads

        mock_store.return_value = {"id": "n-source-chunk", "status": "created", "dedup_telemetry": {}}

        def _store_chunks(**kwargs):
            return [
                {
                    "chunk_id": f"sch_chunk_{kwargs['start_index']}",
                    "status": "created",
                    "text": kwargs["text"],
                    "chunk_index": kwargs["start_index"],
                }
            ]

        mock_store_source_chunks.side_effect = _store_chunks
        payload = {
            "raw_facts": [
                {
                    "text": "Ada keeps the launch checklist in the red binder.",
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["project"],
                    "extraction_confidence": "high",
                    "_source_chunk_ref": "chunk:first",
                },
                {
                    "text": "Berto keeps the rover manual in cabinet seven.",
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["project"],
                    "extraction_confidence": "high",
                    "_source_chunk_ref": "chunk:second",
                },
            ],
            "raw_source_chunks": [
                {
                    "source_chunk_ref": "chunk:first",
                    "text": "User: Ada keeps the launch checklist in the red binder.",
                    "source_id": "sess-multichunk",
                    "session_id": "sess-multichunk",
                    "chunk_index": 0,
                },
                {
                    "source_chunk_ref": "chunk:unused",
                    "text": "User: Cora labels the backup battery with blue tape.",
                    "source_id": "sess-multichunk",
                    "session_id": "sess-multichunk",
                    "chunk_index": 2,
                },
                {
                    "source_chunk_ref": "chunk:first",
                    "text": "User: Ada keeps the launch checklist in the red binder.",
                    "source_id": "sess-multichunk",
                    "session_id": "sess-multichunk",
                    "chunk_index": 0,
                },
                {
                    "source_chunk_ref": "chunk:second",
                    "text": "User: Berto keeps the rover manual in cabinet seven.",
                    "source_id": "sess-multichunk",
                    "session_id": "sess-multichunk",
                    "chunk_index": 1,
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
            session_id="sess-multichunk",
            dry_run=False,
        )

        assert applied["facts_stored"] == 2
        assert applied["source_chunks_stored"] == 2
        assert applied["source_chunks_failed"] == 0
        assert [call.kwargs["start_index"] for call in mock_store_source_chunks.call_args_list] == [0, 1]
        assert [call.kwargs["chunk_kind"] for call in mock_store_source_chunks.call_args_list] == ["micro", "micro"]
        assert [call.kwargs["source_chunk_id"] for call in mock_store.call_args_list] == [
            "sch_chunk_0",
            "sch_chunk_1",
        ]

    def test_split_session_source_microchunks_bounds_large_transcripts(self):
        from ingest.extract import _split_session_source_microchunks, estimate_tokens

        long_text = "\n\n".join(
            [
                "User: Ada keeps the launch checklist in the red binder " * 8,
                "Assistant: acknowledged " * 8,
                "User: Berto keeps the rover manual in cabinet seven " * 8,
            ]
        )

        chunks = _split_session_source_microchunks(long_text, max_tokens=128)

        assert len(chunks) > 1
        assert all(estimate_tokens(chunk) <= 128 for chunk in chunks)
        assert "red binder" in " ".join(chunks)
        assert "cabinet seven" in " ".join(chunks)

        unbroken_chunks = _split_session_source_microchunks("x" * 50000, max_tokens=128)
        assert len(unbroken_chunks) > 1
        assert all(estimate_tokens(chunk) <= 128 for chunk in unbroken_chunks)

    @patch("ingest.extract._session_bridge.list_session_chunks", return_value=[{"chunk_index": 4}])
    @patch("ingest.extract._session_bridge.store_session_source_text")
    @patch("ingest.extract._memory.store")
    def test_apply_extracted_payloads_microchunks_source_and_links_best_matching_fact(
        self,
        mock_store,
        mock_store_source_chunks,
        _mock_list_source_chunks,
    ):
        from ingest.extract import apply_extracted_payloads

        mock_store.return_value = {"id": "n-source-chunk", "status": "created", "dedup_telemetry": {}}

        def _store_chunks(**kwargs):
            chunks = [
                "User: Ada keeps the launch checklist in the red binder.",
                "Assistant: acknowledged.",
                "User: Berto keeps the rover manual in cabinet seven.",
            ]
            return [
                {
                    "chunk_id": f"sch_micro_{kwargs['start_index'] + offset}",
                    "status": "created",
                    "text": text,
                    "chunk_index": kwargs["start_index"] + offset,
                }
                for offset, text in enumerate(chunks)
            ]

        mock_store_source_chunks.side_effect = _store_chunks
        payload = {
            "raw_facts": [
                {
                    "text": "Ada keeps the launch checklist in the red binder.",
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["project"],
                    "extraction_confidence": "high",
                    "_source_chunk_ref": "chunk:long",
                },
                {
                    "text": "Berto keeps the rover manual in cabinet seven.",
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["project"],
                    "extraction_confidence": "high",
                    "_source_chunk_ref": "chunk:long",
                },
            ],
            "raw_source_chunks": [
                {
                    "source_chunk_ref": "chunk:long",
                    "text": "oversized root transcript text",
                    "source_id": "sess-microchunk",
                    "session_id": "sess-microchunk",
                    "chunk_index": 0,
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
            session_id="sess-microchunk",
            dry_run=False,
        )

        assert applied["facts_stored"] == 2
        assert applied["source_chunks_stored"] == 3
        assert applied["source_chunks_micro_split"] == 2
        chunk_call = mock_store_source_chunks.call_args.kwargs
        assert chunk_call["start_index"] == 5
        assert chunk_call["chunk_kind"] == "micro"
        assert [call.kwargs["source_chunk_id"] for call in mock_store.call_args_list] == [
            "sch_micro_5",
            "sch_micro_7",
        ]

    @patch("ingest.extract._session_bridge.list_session_chunks", return_value=[])
    @patch("ingest.extract._session_bridge.store_session_source_text")
    @patch("ingest.extract._memory.store")
    def test_apply_extracted_payloads_skips_orphan_chunk_descriptors_and_leaves_missing_refs_unlinked(
        self,
        mock_store,
        mock_store_source_chunks,
        mock_list_source_chunks,
    ):
        from ingest.extract import apply_extracted_payloads

        mock_store.return_value = {"id": "n-orphan-source-chunk", "status": "created", "dedup_telemetry": {}}
        payload = {
            "raw_facts": [
                {
                    "text": "Ada keeps the launch checklist in the red binder.",
                    "category": "fact",
                    "speaker": "user",
                    "domains": ["project"],
                    "extraction_confidence": "high",
                    "_source_chunk_ref": "chunk:missing",
                },
            ],
            "raw_source_chunks": [
                {
                    "source_chunk_ref": "chunk:unused",
                    "text": "User: This descriptor has no extracted fact.",
                    "source_id": "sess-orphan",
                    "session_id": "sess-orphan",
                    "chunk_index": 0,
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
            session_id="sess-orphan",
            dry_run=False,
        )

        assert applied["facts_stored"] == 1
        assert applied["source_chunks_stored"] == 0
        assert applied["source_chunks_failed"] == 0
        mock_store_source_chunks.assert_not_called()
        mock_list_source_chunks.assert_not_called()
        assert mock_store.call_args.kwargs["source_chunk_id"] is None

    @patch("ingest.extract._memory.store")
    def test_apply_extracted_payloads_honors_subagent_source_overrides(self, mock_store):
        from ingest.extract import apply_extracted_payloads

        mock_store.return_value = {"id": "n-subagent", "status": "created", "dedup_telemetry": {}}

        payload = {
            "raw_facts": [
                {
                    "text": "The user's uncle owns a vineyard in Mendoza.",
                    "category": "fact",
                    "speaker": "user",
                    "source": "subagent",
                    "_source_label": "daemon-session_end-subagent-extraction",
                    "_source_id": "child-session-1",
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

        applied = apply_extracted_payloads(
            payload,
            owner_id="test",
            label="daemon-session_end",
            session_id="parent-session",
            dry_run=False,
        )

        assert applied["facts_stored"] == 1
        call = mock_store.call_args.kwargs
        assert call["source"] == "daemon-session_end-subagent-extraction"
        assert call["source_id"] == "child-session-1"
        assert call["session_id"] == "parent-session"
        assert call["source_type"] == "subagent"
        assert call["speaker"] == "user"

    @patch("ingest.extract._memory.store")
    def test_apply_extracted_payloads_prefers_subagent_provenance_when_duplicates_collapse(self, mock_store):
        from ingest.extract import apply_extracted_payloads

        mock_store.return_value = {"id": "n-subagent", "status": "created", "dedup_telemetry": {}}

        payload = {
            "raw_facts": [
                {
                    "text": "The user's uncle owns a vineyard in Mendoza.",
                    "category": "fact",
                    "speaker": "user",
                    "source": "user",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                },
                {
                    "text": "The user's uncle owns a vineyard in Mendoza.",
                    "category": "fact",
                    "speaker": "user",
                    "source": "subagent",
                    "_source_label": "daemon-session_end-subagent-extraction",
                    "_source_id": "child-session-1",
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

        applied = apply_extracted_payloads(
            payload,
            owner_id="test",
            label="daemon-session_end",
            session_id="parent-session",
            dry_run=False,
        )

        assert applied["payload_duplicate_facts_collapsed"] == 1
        assert applied["facts_stored"] == 1
        call = mock_store.call_args.kwargs
        assert call["source"] == "daemon-session_end-subagent-extraction"
        assert call["source_id"] == "child-session-1"
        assert call["source_type"] == "subagent"

    @patch("ingest.extract.enqueue_project_logs")
    @patch("ingest.extract._memory.store")
    def test_apply_extracted_payloads_synthesizes_project_logs_from_project_facts_when_missing(
        self,
        mock_store,
        mock_enqueue_project_logs,
    ):
        from ingest.extract import apply_extracted_payloads

        mock_store.return_value = {"id": "fact-1", "status": "created", "dedup_telemetry": {}}
        mock_enqueue_project_logs.return_value = {
            "projects_seen": 1,
            "entries_seen": 1,
            "entries_queued": 1,
            "entries_written": 0,
            "projects_queued": 1,
            "queue_failures": 0,
        }

        payload = {
            "raw_facts": [
                {
                    "text": "Added a hello_world.py scratch helper for the live test",
                    "category": "fact",
                    "speaker": "assistant",
                    "domains": ["project"],
                    "extraction_confidence": "high",
                    "project": "quaid",
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
            label="carry-flush",
            session_id="sess-project-log",
            dry_run=False,
        )

        assert applied["facts_stored"] == 1
        assert applied["project_logs"] == {
            "quaid": ["Added a hello_world.py scratch helper for the live test"],
        }
        assert applied["project_log_metrics"]["entries_queued"] == 1
        mock_enqueue_project_logs.assert_called_once_with(
            {"quaid": [{"text": "Added a hello_world.py scratch helper for the live test"}]},
            trigger="CLI",
            date_str=None,
            session_id="sess-project-log",
            owner_id="test",
            source_instance=os.environ.get("QUAID_INSTANCE"),
            source_adapter=os.environ.get("QUAID_ADAPTER_TYPE"),
            dry_run=False,
        )

    @patch("ingest.extract.enqueue_project_logs")
    @patch("ingest.extract._memory.store")
    def test_apply_extracted_payloads_project_logs_use_session_date_when_quaid_now_missing(
        self,
        mock_store,
        mock_enqueue_project_logs,
        monkeypatch,
    ):
        from ingest.extract import apply_extracted_payloads

        monkeypatch.delenv("QUAID_NOW", raising=False)
        mock_store.return_value = {"id": "fact-1", "status": "created", "dedup_telemetry": {}}
        mock_enqueue_project_logs.return_value = {
            "projects_seen": 1,
            "entries_seen": 1,
            "entries_queued": 1,
            "entries_written": 0,
            "projects_queued": 1,
            "queue_failures": 0,
        }

        payload = {
            "raw_facts": [],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {"recipe-app": ["Added tests/recipe.test.js"]},
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

        apply_extracted_payloads(
            payload,
            owner_id="test",
            label="daemon-compaction",
            session_id="day-runtime-2026-03-11",
            dry_run=False,
        )

        mock_enqueue_project_logs.assert_called_once_with(
            {
                "recipe-app": [
                    {
                        "text": "Added tests/recipe.test.js",
                        "created_at": "2026-03-11T23:59:59",
                    }
                ]
            },
            trigger="Compaction",
            date_str="2026-03-11",
            session_id="day-runtime-2026-03-11",
            owner_id="test",
            source_instance=os.environ.get("QUAID_INSTANCE"),
            source_adapter=os.environ.get("QUAID_ADAPTER_TYPE"),
            dry_run=False,
        )

    @patch("ingest.extract.enqueue_project_logs")
    @patch("ingest.extract._memory.store")
    def test_apply_extracted_payloads_facts_use_session_date_as_mentioned_at_only(
        self,
        mock_store,
        mock_enqueue_project_logs,
    ):
        from ingest.extract import apply_extracted_payloads

        mock_store.return_value = {"id": "fact-1", "status": "created", "dedup_telemetry": {}}
        mock_enqueue_project_logs.return_value = {
            "projects_seen": 1,
            "entries_seen": 1,
            "entries_queued": 1,
            "entries_written": 0,
            "projects_queued": 1,
            "queue_failures": 0,
        }

        payload = {
            "raw_facts": [
                {
                    "text": "Recipe app added centralized retry middleware",
                    "category": "fact",
                    "speaker": "assistant",
                    "domains": ["project"],
                    "extraction_confidence": "high",
                    "project": "recipe-app",
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

        apply_extracted_payloads(
            payload,
            owner_id="test",
            label="daemon-compaction",
            session_id="session-2026-03-12",
            dry_run=False,
        )

        assert "created_at" not in mock_store.call_args.kwargs
        assert mock_store.call_args.kwargs["mentioned_at"] == "2026-03-12T23:59:59"
        assert payload["project_log_metrics"]["entries_queued"] == 1
        mock_enqueue_project_logs.assert_called_once_with(
            {
                "recipe-app": [
                    {
                        "text": "Recipe app added centralized retry middleware",
                        "created_at": "2026-03-12T23:59:59",
                    }
                ]
            },
            trigger="Compaction",
            date_str="2026-03-12",
            session_id="session-2026-03-12",
            owner_id="test",
            source_instance=os.environ.get("QUAID_INSTANCE"),
            source_adapter=os.environ.get("QUAID_ADAPTER_TYPE"),
            dry_run=False,
        )

    @patch("ingest.extract.call_deep_reasoning")
    @patch("ingest.extract._memory.store")
    def test_skips_short_facts(self, mock_store, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "facts": [
                {"text": "hi", "category": "fact", "speaker": "user"},
                {
                    "text": "User likes coffee very much",
                    "category": "preference",
                    "speaker": "user",
                    "domains": ["personal"],
                },
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
                {
                    "text": "User likes coffee a lot",
                    "speaker": "user",
                    "extraction_confidence": "high",
                    "domains": ["personal"],
                },
                {
                    "text": "User might enjoy tea sometimes",
                    "speaker": "user",
                    "extraction_confidence": "low",
                    "domains": ["personal"],
                },
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
                    "speaker": "user",
                    "domains": ["project"],
                    "extraction_confidence": "high",
                },
                {
                    "text": "Maya's birthday dinner is planned for May 18",
                    "category": "fact",
                    "speaker": "user",
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
                    "speaker": "user",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                    "edges": [{"subject": "maya", "relation": "lives_at", "object": "Austin"}],
                },
                {
                    "text": "maya works as a product manager at a company called TechFlow",
                    "category": "fact",
                    "speaker": "user",
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
                    "speaker": "user",
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
                    "speaker": "user",
                    "domains": ["personal"],
                    "extraction_confidence": "high",
                },
                {
                    "text": "Maya wants dietary tagging in the recipe app",
                    "category": "fact",
                    "speaker": "user",
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
             patch("datastore.memorydb.extraction_publish._get_extract_publish_batch_size", return_value=1):
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

    def test_extract_import_does_not_bind_memory_service_at_module_import(self, monkeypatch):
        import ingest.extract as extract_mod

        calls = {"count": 0}

        def fake_get_memory_service():
            calls["count"] += 1

            class _Svc:
                def warm_embeddings(self, texts):
                    return {
                        "requested": len(texts),
                        "unique": len(texts),
                        "cache_hits": 0,
                        "warmed": len(texts),
                        "failed": 0,
                        "skipped_empty": 0,
                    }

            return _Svc()

        monkeypatch.setattr("core.services.memory_service.get_memory_service", fake_get_memory_service)
        reloaded = importlib.reload(extract_mod)

        assert calls["count"] == 0

        stats = reloaded._memory.warm_embeddings(["alpha", "beta"])
        assert calls["count"] == 1
        assert stats["requested"] == 2


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
                    "speaker": "user",
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
                    "speaker": "user",
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
        trace_path = workspace_dir.parent / "benchrunner" / "logs" / "daemon" / "publish-trace.jsonl"
        rows = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        events = [row["event"] for row in rows]
        assert "publish_start" in events
        assert "publish_batch_conn_opened" in events
        assert "publish_store_call_start" in events
        assert "publish_store_call_done" in events
        assert "publish_facts_complete" in events
        assert "publish_complete" in events
        assert events.index("publish_facts_complete") < events.index("publish_complete")

    def test_publish_complete_trace_waits_for_orchestration_side_effects(self, workspace_dir, monkeypatch):
        from ingest.extract import apply_extracted_payloads

        monkeypatch.setattr(
            "datastore.memorydb.extraction_publish.get_config",
            lambda: SimpleNamespace(retrieval=SimpleNamespace(domains={"personal": "Personal facts"})),
        )
        monkeypatch.setenv("QUAID_PUBLISH_TRACE", "1")
        monkeypatch.setenv("QUAID_INSTANCE", "benchrunner")
        trace_path = workspace_dir.parent / "benchrunner" / "logs" / "daemon" / "publish-trace.jsonl"

        def fake_enqueue_project_logs(*_args, **_kwargs):
            rows = [
                json.loads(line)
                for line in trace_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            events_during_side_effect = [row["event"] for row in rows]
            assert "publish_facts_complete" in events_during_side_effect
            assert "publish_complete" not in events_during_side_effect
            return {
                "entries_seen": 1,
                "entries_queued": 1,
                "projects_queued": 1,
                "queue_failures": 0,
            }

        monkeypatch.setattr("ingest.extract.enqueue_project_logs", fake_enqueue_project_logs)

        payload = {
            "raw_facts": [{
                "text": "Maya keeps the launch checklist in the red binder",
                "category": "fact",
                "speaker": "user",
                "domains": ["personal"],
            }],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {"launch-app": ["Moved launch checklist into red binder"]},
            "facts": [],
            "snippets": {},
            "journal": {},
            "project_logs": {},
            "project_log_metrics": {},
            "facts_stored": 0,
            "facts_skipped": 0,
            "edges_created": 0,
            "dry_run": True,
        }

        applied = apply_extracted_payloads(
            payload,
            owner_id="test",
            label="rolling-flush",
            session_id="sess-trace-order",
            dry_run=True,
        )

        assert applied["project_log_metrics"]["entries_queued"] == 1
        rows = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        events = [row["event"] for row in rows]
        assert events.index("publish_facts_complete") < events.index("publish_complete")

    @patch("ingest.extract.call_deep_reasoning")
    @patch("ingest.extract._memory.store")
    def test_skips_invalid_fact_payload_items(self, mock_store, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "facts": [
                "not-a-dict",
                {"text": 123, "category": "fact", "speaker": "user"},
                {"category": "fact"},
                {
                    "text": "User likes orange juice",
                    "category": "preference",
                    "speaker": "user",
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
        assert result["facts_skipped"] == 3

    @patch("ingest.extract.call_deep_reasoning")
    @patch("ingest.extract._memory.store")
    def test_metadata_scope_fields_are_forwarded(self, mock_store, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "facts": [{
                "text": "User likes oolong tea",
                "category": "fact",
                "speaker": "user",
                "subject_entity_name": "Maya",
                "domains": ["personal"],
            }]
        }), 1.0)
        mock_store.return_value = {"id": "n1", "status": "created"}

        with patch("ingest.extract._memory.warm_embeddings", return_value={"requested": 1, "warmed": 1}), \
             patch("ingest.extract._memory.batch_write", return_value=nullcontext(None)):
            extract_from_transcript(
                transcript="User: test\n\nAssistant: ok",
                owner_id="test",
                actor_id="user:owner",
                subject_entity_id="user:owner",
                source_channel="telegram",
                source_conversation_id="chat-1",
                source_author_id="operator-alias",
            )

        kwargs = mock_store.call_args.kwargs
        assert kwargs["actor_id"] == "user:owner"
        assert kwargs["subject_entity_id"] == "user:owner"
        assert kwargs["subject_entity_name"] == "Maya"
        assert kwargs["source_channel"] == "telegram"
        assert kwargs["source_conversation_id"] == "chat-1"
        assert kwargs["source_author_id"] == "operator-alias"

    @patch("ingest.extract.call_deep_reasoning")
    @patch("ingest.extract._memory.store")
    def test_target_datastore_is_forwarded(self, mock_store, mock_llm):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "facts": [{
                "text": "User likes green tea",
                "category": "fact",
                "speaker": "user",
                "domains": ["personal"],
            }]
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
            "facts": [{
                "text": "User likes jasmine tea in the morning",
                "category": "fact",
                "speaker": "user",
            }]
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
                    "speaker": "user",
                    "domains": ["not_a_real_domain"],
                },
                {
                    "text": "User prefers black coffee after lunch",
                    "category": "preference",
                    "speaker": "user",
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
            "facts": [{
                "text": "User likes jasmine tea in the morning",
                "category": "fact",
                "speaker": "user",
                "domains": ["personal"],
            }]
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
    def test_snippets_written(self, mock_store, mock_llm, mock_opus_response, visible_workspace_dir):
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
        snippet_file = visible_workspace_dir / "SOUL.snippets.md"
        assert snippet_file.exists()

    @patch("ingest.extract.call_deep_reasoning")
    @patch("ingest.extract._memory.store")
    def test_synthesizes_user_snippets_when_personal_facts_exist_but_user_snippets_missing(
        self, mock_store, mock_llm, visible_workspace_dir
    ):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (
            json.dumps(
                {
                    "chunk_assessment": "usable",
                    "facts": [
                        {
                            "text": "Test user prefers methodical debugging",
                            "category": "preference",
                            "speaker": "user",
                            "domains": ["personal"],
                            "extraction_confidence": "high",
                            "keywords": "debugging methodical preference",
                            "privacy": "shared",
                            "confidence_reason": "Explicitly stated",
                            "edges": [],
                        }
                    ],
                    "soul_snippets": {
                        "SOUL.md": [],
                        "USER.md": [],
                        "ENVIRONMENT.md": [],
                    },
                    "journal_entries": {
                        "SOUL.md": "",
                        "USER.md": "",
                        "ENVIRONMENT.md": "",
                    },
                }
            ),
            1.0,
        )
        mock_store.return_value = {"id": "n1", "status": "created"}

        result = extract_from_transcript(
            transcript="User: I prefer methodical debugging.\n\nAssistant: Noted.",
            owner_id="test",
            write_snippets=True,
        )

        assert result["snippets"]["USER.md"] == ["Test user prefers methodical debugging"]
        snippet_file = visible_workspace_dir / "USER.snippets.md"
        assert snippet_file.exists()

    @patch("ingest.extract.call_deep_reasoning")
    @patch("ingest.extract._memory.store")
    def test_journal_written(self, mock_store, mock_llm, mock_opus_response, visible_workspace_dir):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (mock_opus_response, 1.0)
        mock_store.return_value = {"id": "n1", "status": "created"}

        result = extract_from_transcript(
            transcript="User: test\n\nAssistant: ok",
            owner_id="test",
            write_journal=True,
        )

        assert "SOUL.md" in result["journal"]
        journal_file = visible_workspace_dir / "journal" / "SOUL.journal.md"
        assert journal_file.exists()

    @patch("ingest.extract.call_deep_reasoning")
    @patch("ingest.extract._memory.store")
    def test_no_snippets_flag(self, mock_store, mock_llm, mock_opus_response, visible_workspace_dir):
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
        snippet_file = visible_workspace_dir / "SOUL.snippets.md"
        assert not snippet_file.exists()

    @patch("ingest.extract.call_deep_reasoning")
    @patch("ingest.extract._memory.store")
    def test_no_journal_flag(self, mock_store, mock_llm, mock_opus_response, visible_workspace_dir):
        from ingest.extract import extract_from_transcript

        mock_llm.return_value = (mock_opus_response, 1.0)
        mock_store.return_value = {"id": "n1", "status": "created"}

        result = extract_from_transcript(
            transcript="User: test\n\nAssistant: ok",
            owner_id="test",
            write_journal=False,
        )

        assert "SOUL.md" in result["journal"]
        journal_file = visible_workspace_dir / "journal" / "SOUL.journal.md"
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
                "speaker": "user",
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
                            "speaker": "user",
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

    def test_extraction_prompt_preserves_exact_lists_routines_and_callback_anchors(self):
        from ingest.extract import _load_extraction_prompt

        prompt = _load_extraction_prompt({}, owner_id="maya")

        assert "short exact list of named options or steps" in prompt
        assert "minimum viable stretching routine" in prompt
        assert "pet got into a household item" in prompt
        assert "new interface while preserving an older compatibility path" in prompt
        assert "subject_entity_name" in prompt

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
            "speaker": "user",
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
            (
                json.dumps(
                    {"facts": [{"text": "Maya lives in Austin.", "speaker": "user", "domains": ["personal"]}]}
                ),
                0.2,
            ),
            (
                json.dumps(
                    {"facts": [{"text": "Maya works at Stripe.", "speaker": "user", "domains": ["personal"]}]}
                ),
                0.2,
            ),
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

    @patch("lib.batch_utils.chunk_text_by_tokens")
    @patch("ingest.extract.call_deep_reasoning")
    def test_wall_timeout_parameter_does_not_stop_chunk_processing(self, mock_llm, mock_chunk):
        from ingest.extract import extract_from_transcript

        mock_chunk.return_value = [
            "User: first chunk",
            "User: second chunk",
        ]
        mock_llm.return_value = (json.dumps({"facts": []}), 0.4)

        result = extract_from_transcript(
            transcript="dummy",
            owner_id="test",
            label="no-wall-deadline-test",
            wall_timeout_seconds=0.001,
        )

        assert result["facts_stored"] == 0
        assert result["chunks_total"] == 2
        assert mock_llm.call_count == 2

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
            return json.dumps({"facts": [{"text": fact_text, "speaker": "user", "domains": ["personal"]}]}), 0.1

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


class TestUnsupportedSpecificityFilters:
    def test_drops_invented_date_anchor_not_in_chunk_or_session_hint(self):
        from ingest.extract import _filter_unsupported_specificity_facts

        facts = [
            {
                "text": "Maya started her first day at Stripe on 2024-01-16",
                "category": "fact",
                "speaker": "user",
                "extraction_confidence": "high",
            }
        ]
        result = {"facts_skipped": 0, "unsupported_specificity_facts_dropped": 0}

        filtered = _filter_unsupported_specificity_facts(
            facts,
            transcript_text="today: first day at stripe. my brain is mush.",
            session_date_hint="2026-05-19",
            result=result,
            label="unit",
            chunk_label="1",
        )

        assert filtered == []
        assert result["facts_skipped"] == 1
        assert result["unsupported_specificity_facts_dropped"] == 1

    def test_keeps_date_anchor_when_it_matches_session_hint(self):
        from ingest.extract import _filter_unsupported_specificity_facts

        facts = [
            {
                "text": "Maya's Stripe start date is 2026-05-19",
                "category": "fact",
                "speaker": "user",
                "extraction_confidence": "high",
            }
        ]
        result = {"facts_skipped": 0, "unsupported_specificity_facts_dropped": 0}

        filtered = _filter_unsupported_specificity_facts(
            facts,
            transcript_text="today: first day at stripe. my brain is mush.",
            session_date_hint="2026-05-19",
            result=result,
            label="unit",
            chunk_label="1",
        )

        assert filtered == facts
        assert result["facts_skipped"] == 0
        assert result["unsupported_specificity_facts_dropped"] == 0

    def test_non_iso_date_anchors_are_left_to_prompt_layer(self):
        from ingest.extract import _filter_unsupported_specificity_facts

        facts = [
            {
                "text": "Maya started her first day at Stripe on January 16, 2024",
                "category": "fact",
                "speaker": "user",
                "extraction_confidence": "high",
            }
        ]
        result = {"facts_skipped": 0, "unsupported_specificity_facts_dropped": 0}

        filtered = _filter_unsupported_specificity_facts(
            facts,
            transcript_text="today: first day at stripe. my brain is mush.",
            session_date_hint="2026-05-19",
            result=result,
            label="unit",
            chunk_label="2",
        )

        assert filtered == facts
        assert result["unsupported_specificity_facts_dropped"] == 0

    def test_keeps_multilingual_pet_fact_without_english_transcript_scanning(self):
        from ingest.extract import _filter_unsupported_specificity_facts

        facts = [
            {
                "text": "健太はビスケットという猫を飼っている",
                "category": "fact",
                "speaker": "user",
                "extraction_confidence": "high",
            }
        ]
        result = {"facts_skipped": 0, "unsupported_specificity_facts_dropped": 0}

        filtered = _filter_unsupported_specificity_facts(
            facts,
            transcript_text="今日はビスケットを撫でながら映画を見る。",
            session_date_hint="2026-05-19",
            result=result,
            label="unit",
            chunk_label="3",
        )

        assert filtered == facts
        assert result["unsupported_specificity_facts_dropped"] == 0


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
            "facts_stored": 0, "facts_planned": 1, "facts_skipped": 0, "edges_created": 0,
            "facts": [], "snippets": {}, "journal": {}, "dry_run": True,
        }

        summary = _format_human_summary(result)
        assert "[DRY RUN]" in summary
        assert "Facts planned:  1" in summary


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

    def test_prompt_requires_full_sentence_facts_not_bare_fragments(self):
        from ingest.extract import _load_extraction_prompt

        prompt = _load_extraction_prompt()
        assert "complete, self-contained statement of at least 3 words" in prompt
        assert "Never emit a bare name, lone codeword, or noun fragment as a fact" in prompt

    def test_prompt_forbids_invented_specific_anchors(self):
        from ingest.extract import _load_extraction_prompt

        prompt = _load_extraction_prompt()
        assert "Do not invent a more specific anchor than the transcript actually provides." in prompt
        assert "Never manufacture an exact calendar date" in prompt

    def test_prompt_requires_temporal_provenance_contract(self):
        from ingest.extract import _load_extraction_prompt

        prompt = _load_extraction_prompt()
        assert "TEMPORAL PROVENANCE (per fact)" in prompt
        assert '"mentioned_at"' in prompt
        assert '"occurred_start"' in prompt
        assert "Do not copy the message timestamp into `occurred_start`" in prompt
        assert '"May 2023" -> `occurred_start: "2023-05-01"`' in prompt

    def test_truncated_array_scanner_stops_on_mid_string_truncation(self):
        from ingest.extract import _complete_json_objects_from_array

        fact_one = {
            "text": "Maya keeps a red field notebook.",
            "category": "fact",
            "speaker": "user",
            "domains": ["personal"],
            "extraction_confidence": "high",
        }
        response = (
            "["
            f"{json.dumps(fact_one)},"
            '{"text":"Maya wrote \\"'
        )

        scanned = _complete_json_objects_from_array(response, 1)

        assert [fact["text"] for fact in scanned] == ["Maya keeps a red field notebook."]

    def test_truncated_array_scanner_stops_on_mid_nested_object_truncation(self):
        from ingest.extract import _complete_json_objects_from_array

        fact_one = {
            "text": "Maya keeps a red field notebook.",
            "category": "fact",
            "speaker": "user",
            "domains": ["personal"],
            "extraction_confidence": "high",
        }
        response = (
            "["
            f"{json.dumps(fact_one)},"
            '{"text":"Maya mentors Ana","category":"fact","speaker":"user",'
            '"domains":["personal"],"extraction_confidence":"medium","edges":['
            '{"subject":"Maya","relation":"mentors","object":'
        )

        scanned = _complete_json_objects_from_array(response, 1)

        assert [fact["text"] for fact in scanned] == ["Maya keeps a red field notebook."]

    def test_truncated_array_scanner_returns_no_objects_for_single_truncated_object(self):
        from ingest.extract import _complete_json_objects_from_array

        response = (
            "["
            '{"text":"Maya keeps a red field notebook.","category":"fact","speaker":"user",'
            '"domains":["personal"],"extraction_confidence":"high"'
        )

        scanned = _complete_json_objects_from_array(response, 1)

        assert scanned == []

    def test_salvages_complete_facts_from_truncated_json_array(self):
        from ingest.extract import _salvage_truncated_extraction_payload

        fact_one = {
            "text": "Maya keeps a red field notebook.",
            "category": "fact",
            "speaker": "user",
            "domains": ["personal"],
            "extraction_confidence": "high",
        }
        fact_two = {
            "text": "Maya plans a Sunday canal walk.",
            "category": "fact",
            "speaker": "user",
            "domains": ["personal"],
            "extraction_confidence": "medium",
        }
        response = (
            "```json\n"
            '{"chunk_assessment":"usable","facts":['
            f"{json.dumps(fact_one)},{json.dumps(fact_two)},"
            '{"text":"Maya has a truncated fact","category":"fact","extraction_confidence":'
        )
        telemetry = {}

        salvaged = _salvage_truncated_extraction_payload(
            response_text=response,
            chunk_index="1",
            label="salvage-test",
            telemetry=telemetry,
        )

        assert salvaged["chunk_assessment"] == "usable"
        assert [fact["text"] for fact in salvaged["facts"]] == [
            "Maya keeps a red field notebook.",
            "Maya plans a Sunday canal walk.",
        ]
        assert "_salvaged_truncated_response" not in salvaged
        assert telemetry["truncated_salvage_calls"] == 1
        assert telemetry["truncated_salvage_facts"] == 2

    def test_salvage_returns_none_when_response_has_no_facts_array(self):
        from ingest.extract import _salvage_truncated_extraction_payload

        salvaged = _salvage_truncated_extraction_payload(
            response_text="I remembered that Maya keeps a red field notebook.",
            chunk_index="1",
            label="salvage-test",
            telemetry={},
        )

        assert salvaged is None

    @patch("ingest.extract.call_deep_reasoning")
    def test_extract_uses_truncated_json_salvage_before_repair(self, mock_llm):
        from ingest.extract import extract_from_transcript

        fact = {
            "text": "Maya stores her binoculars in a green case.",
            "category": "fact",
            "speaker": "user",
            "domains": ["personal"],
            "extraction_confidence": "high",
        }
        mock_llm.return_value = (
            '{"chunk_assessment":"usable","facts":['
            f"{json.dumps(fact)},"
            '{"text":"unfinished","extraction_confidence":',
            0.4,
        )

        result = extract_from_transcript(
            transcript="User: Maya stores her binoculars in a green case.",
            owner_id="test",
            label="salvage-test",
            dry_run=True,
        )

        assert result["facts_planned"] == 1
        assert result["raw_facts"][0]["text"] == "Maya stores her binoculars in a green case."
        assert result["truncated_salvage_calls"] == 1
        assert result["truncated_salvage_facts"] == 1
        assert result["repair_calls"] == 0

    @patch("ingest.extract.call_deep_reasoning")
    def test_json_repair_prompt_prefers_needs_smaller_chunk_for_truncated_dense_output(self, mock_deep):
        from ingest.extract import _repair_non_json_extraction_payload

        mock_deep.return_value = (
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
            chunk_index="1",
            label="repair-test",
        )

        assert repaired["chunk_assessment"] == "needs_smaller_chunk"
        repair_prompt = mock_deep.call_args.kwargs["prompt"]
        assert "return chunk_assessment as needs_smaller_chunk" in repair_prompt
        assert mock_deep.call_args.kwargs["max_tokens"] >= 4096

    @patch("ingest.extract.call_deep_reasoning", side_effect=RuntimeError("repair boom"))
    def test_json_repair_raises_when_fail_hard_enabled(self, _mock_deep):
        from ingest.extract import _repair_non_json_extraction_payload

        with patch("ingest.extract.is_fail_hard_enabled", return_value=True):
            with pytest.raises(RuntimeError, match="repair boom"):
                _repair_non_json_extraction_payload(
                    response_text="not json",
                    chunk_index="1",
                    label="repair-test",
                )

    @patch("ingest.extract.call_deep_reasoning", side_effect=RuntimeError("repair boom"))
    def test_json_repair_returns_none_when_fail_hard_disabled(self, _mock_deep):
        from ingest.extract import _repair_non_json_extraction_payload

        with patch("ingest.extract.is_fail_hard_enabled", return_value=False):
            repaired = _repair_non_json_extraction_payload(
                response_text="not json",
                chunk_index="1",
                label="repair-test",
            )

        assert repaired is None


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


class TestNormalizeFactProvenance:
    def test_raises_when_missing_speaker_and_source_under_fail_hard(self):
        from datastore.memorydb.extraction_publish import _normalize_fact_provenance

        with pytest.raises(RuntimeError, match="missing provenance"):
            _normalize_fact_provenance(
                {},
                label="unit",
                fact_index=1,
                fail_hard_enabled=lambda: True,
                log=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
            )

    def test_defaults_to_user_when_missing_speaker_and_source_non_fail_hard(self):
        from datastore.memorydb.extraction_publish import _normalize_fact_provenance

        speaker, source_type = _normalize_fact_provenance(
            {},
            label="unit",
            fact_index=2,
            fail_hard_enabled=lambda: False,
            log=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
        )
        assert speaker == "user"
        assert source_type == "user"

    def test_normalizes_agent_and_tool_source_variants(self):
        from datastore.memorydb.extraction_publish import _normalize_fact_provenance

        kwargs = {
            "fail_hard_enabled": lambda: False,
            "log": SimpleNamespace(warning=lambda *_args, **_kwargs: None),
        }

        speaker, source_type = _normalize_fact_provenance(
            {"speaker": "agent"},
            label="unit",
            fact_index=3,
            **kwargs,
        )
        assert speaker == "agent"
        assert source_type == "assistant"

        speaker, source_type = _normalize_fact_provenance(
            {"source": "agent"},
            label="unit",
            fact_index=4,
            **kwargs,
        )
        assert speaker == "agent"
        assert source_type == "assistant"

        speaker, source_type = _normalize_fact_provenance(
            {"source": "both"},
            label="unit",
            fact_index=5,
            **kwargs,
        )
        assert speaker == "user"
        assert source_type == "both"

        speaker, source_type = _normalize_fact_provenance(
            {"source": "tool"},
            label="unit",
            fact_index=6,
            **kwargs,
        )
        assert speaker == "user"
        assert source_type == "tool"

        speaker, source_type = _normalize_fact_provenance(
            {"speaker": "user", "source": "subagent"},
            label="unit",
            fact_index=7,
            **kwargs,
        )
        assert speaker == "user"
        assert source_type == "subagent"

        speaker, source_type = _normalize_fact_provenance(
            {"speaker": "agent", "source": "subagent"},
            label="unit",
            fact_index=8,
            **kwargs,
        )
        assert speaker == "agent"
        assert source_type == "subagent"


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
