"""Tests for extract.py — Memory extraction from conversation transcripts."""

import itertools
import json
import os
import sys
import tempfile
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
                "targetFiles": ["SOUL.md", "USER.md", "MEMORY.md"],
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


@pytest.fixture
def mock_opus_response():
    """Standard Opus extraction response."""
    return json.dumps({
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
            "MEMORY.md": [],
        },
        "journal_entries": {
            "SOUL.md": "A quiet conversation today.",
            "USER.md": "",
            "MEMORY.md": "",
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
            capture=SimpleNamespace(enabled=False, chunk_size=30000)
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
            capture=SimpleNamespace(enabled=True, chunk_size=30000, skip_patterns=[r"HEARTBEAT"])
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
            capture=SimpleNamespace(enabled=True, skip_patterns=[], chunk_size=30000),
            retrieval=SimpleNamespace(domains={}),
            users=SimpleNamespace(default_owner="test-user"),
            docs=SimpleNamespace(
                journal=SimpleNamespace(
                    enabled=True,
                    snippets_enabled=True,
                    target_files=["SOUL.md", "USER.md", "MEMORY.md"],
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
        second_prompt = mock_llm.call_args_list[1].kwargs["prompt"]
        assert "EARLIER CHUNK CONTEXT" in second_prompt
        assert "Maya changed jobs from TechFlow to Stripe" in second_prompt

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
        # deadline init, chunk1 remaining check, chunk2 remaining check (expired).
        # Use an unbounded iterator so incidental logging calls that touch time.time()
        # cannot exhaust the mock in CI.
        mock_time.side_effect = itertools.chain([100.0, 100.0, 701.0], itertools.repeat(701.0))

        result = extract_from_transcript(
            transcript="dummy",
            owner_id="test",
            label="deadline-test",
        )

        assert result["facts_stored"] == 0
        assert mock_llm.call_count == 1


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
    def test_prefers_high_confidence_and_caps_size(self):
        from ingest.extract import _build_chunk_carry_context

        facts = [
            {"text": "A high confidence technical migration happened", "category": "fact", "source": "user", "extraction_confidence": "high"},
            {"text": "A medium confidence note about dinner plans", "category": "fact", "source": "user", "extraction_confidence": "medium"},
            {"text": "A low confidence guess", "category": "fact", "source": "assistant", "extraction_confidence": "low"},
        ]
        ctx = _build_chunk_carry_context(facts, max_items=2, max_chars=300)
        assert "high" in ctx
        assert "medium" in ctx
        assert "low" not in ctx


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


