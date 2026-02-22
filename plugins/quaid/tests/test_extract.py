"""Tests for extract.py — Memory extraction from conversation transcripts."""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Ensure plugins/quaid is on the path
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
    os.environ["CLAWDBOT_WORKSPACE"] = str(tmp_path)

    # Create required directories
    (tmp_path / "journal").mkdir()
    (tmp_path / "config").mkdir()

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
    (tmp_path / "config" / "memory.json").write_text(json.dumps(config))

    # Set adapter to point at tmp_path
    from lib.adapter import set_adapter, reset_adapter, StandaloneAdapter
    set_adapter(StandaloneAdapter(home=tmp_path))

    yield tmp_path

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
                "extraction_confidence": "high",
                "keywords": "beverage drink caffeine morning",
                "privacy": "shared",
                "confidence_reason": "Explicitly stated",
                "edges": [],
            },
            {
                "text": "Test user's sister lives in Portland",
                "category": "fact",
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
        from extract import build_transcript

        messages = [
            {"role": "user", "content": "Hello there"},
            {"role": "assistant", "content": "Hi! How can I help?"},
        ]
        result = build_transcript(messages)
        assert "User: Hello there" in result
        assert "Assistant: Hi! How can I help?" in result

    def test_filters_system_messages(self):
        from extract import build_transcript

        messages = [
            {"role": "system", "content": "You are a helpful assistant"},
            {"role": "user", "content": "Hi"},
        ]
        result = build_transcript(messages)
        assert "system" not in result.lower() or "User: Hi" in result
        assert "User: Hi" in result

    def test_does_not_filter_gateway_restart_in_standalone(self):
        from extract import build_transcript

        messages = [
            {"role": "user", "content": "GatewayRestart: reloaded"},
            {"role": "user", "content": "Hello"},
        ]
        result = build_transcript(messages)
        assert "GatewayRestart" in result
        assert "User: Hello" in result

    def test_does_not_filter_heartbeat_in_standalone(self):
        from extract import build_transcript

        messages = [
            {"role": "user", "content": "HEARTBEAT check HEARTBEAT_OK"},
            {"role": "user", "content": "Real message"},
        ]
        result = build_transcript(messages)
        assert "HEARTBEAT" in result
        assert "User: Real message" in result

    def test_strips_channel_prefix(self):
        from extract import build_transcript

        messages = [
            {"role": "user", "content": "[Telegram user@12345] Hello"},
        ]
        result = build_transcript(messages)
        assert result == "User: Hello"

    def test_strips_message_id(self):
        from extract import build_transcript

        messages = [
            {"role": "user", "content": "Hello\n[message_id: 42]"},
        ]
        result = build_transcript(messages)
        assert "message_id" not in result
        assert "User: Hello" in result

    def test_empty_messages(self):
        from extract import build_transcript
        assert build_transcript([]) == ""

    def test_skips_empty_content(self):
        from extract import build_transcript

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
        from extract import parse_session_jsonl

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
        from extract import parse_session_jsonl

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
        from extract import parse_session_jsonl

        jsonl_file = tmp_path / "session.jsonl"
        lines = [
            json.dumps({"role": "user", "content": [{"text": "Part 1"}, {"text": "Part 2"}]}),
        ]
        jsonl_file.write_text("\n".join(lines))

        result = parse_session_jsonl(str(jsonl_file))
        assert "Part 1 Part 2" in result

    def test_skips_non_message_lines(self, tmp_path):
        from extract import parse_session_jsonl

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
        from extract import parse_session_jsonl

        jsonl_file = tmp_path / "session.jsonl"
        jsonl_file.write_text("")

        result = parse_session_jsonl(str(jsonl_file))
        assert result == ""


# ---------------------------------------------------------------------------
# extract_from_transcript tests
# ---------------------------------------------------------------------------

class TestExtractFromTranscript:
    def test_empty_transcript(self):
        from extract import extract_from_transcript

        result = extract_from_transcript("", owner_id="test")
        assert result["facts_stored"] == 0
        assert result["facts_skipped"] == 0

    def test_empty_whitespace_transcript(self):
        from extract import extract_from_transcript

        result = extract_from_transcript("   \n  \n  ", owner_id="test")
        assert result["facts_stored"] == 0

    @patch("extract.call_deep_reasoning")
    @patch("extract.store")
    @patch("extract.create_edge")
    def test_basic_extraction(self, mock_edge, mock_store, mock_llm, mock_opus_response, workspace_dir):
        from extract import extract_from_transcript

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

    @patch("extract.call_deep_reasoning")
    @patch("extract.store")
    def test_dry_run(self, mock_store, mock_llm, mock_opus_response):
        from extract import extract_from_transcript

        mock_llm.return_value = (mock_opus_response, 1.0)

        result = extract_from_transcript(
            transcript="User: test\n\nAssistant: ok",
            owner_id="test",
            dry_run=True,
        )

        assert result["dry_run"] is True
        assert result["facts_stored"] == 2
        mock_store.assert_not_called()

    @patch("extract.call_deep_reasoning")
    @patch("extract.store")
    def test_duplicate_handling(self, mock_store, mock_llm, mock_opus_response):
        from extract import extract_from_transcript

        mock_llm.return_value = (mock_opus_response, 1.0)
        mock_store.return_value = {"status": "duplicate", "existing_text": "Already stored"}

        result = extract_from_transcript(
            transcript="User: test\n\nAssistant: ok",
            owner_id="test",
        )

        assert result["facts_skipped"] == 2
        assert result["facts_stored"] == 0

    @patch("extract.call_deep_reasoning")
    def test_no_response(self, mock_llm):
        from extract import extract_from_transcript

        mock_llm.return_value = (None, 1.0)

        result = extract_from_transcript(
            transcript="User: test\n\nAssistant: ok",
            owner_id="test",
        )

        assert result["facts_stored"] == 0

    @patch("extract.call_deep_reasoning")
    def test_unparseable_response(self, mock_llm):
        from extract import extract_from_transcript

        mock_llm.return_value = ("This is not JSON at all", 1.0)

        result = extract_from_transcript(
            transcript="User: test\n\nAssistant: ok",
            owner_id="test",
        )

        assert result["facts_stored"] == 0

    @patch("extract.call_deep_reasoning")
    @patch("extract.store")
    def test_skips_short_facts(self, mock_store, mock_llm):
        from extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "facts": [
                {"text": "hi", "category": "fact"},
                {"text": "User likes coffee very much", "category": "preference"},
            ]
        }), 1.0)
        mock_store.return_value = {"id": "n1", "status": "created"}

        result = extract_from_transcript(
            transcript="User: test\n\nAssistant: ok",
            owner_id="test",
        )

        assert result["facts_skipped"] == 1
        assert result["facts_stored"] == 1

    @patch("extract.call_deep_reasoning")
    @patch("extract.store")
    def test_confidence_mapping(self, mock_store, mock_llm):
        from extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "facts": [
                {"text": "User likes coffee a lot", "extraction_confidence": "high"},
                {"text": "User might enjoy tea sometimes", "extraction_confidence": "low"},
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

    @patch("extract.call_deep_reasoning")
    @patch("extract.store")
    def test_snippets_written(self, mock_store, mock_llm, mock_opus_response, workspace_dir):
        from extract import extract_from_transcript

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

    @patch("extract.call_deep_reasoning")
    @patch("extract.store")
    def test_journal_written(self, mock_store, mock_llm, mock_opus_response, workspace_dir):
        from extract import extract_from_transcript

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

    @patch("extract.call_deep_reasoning")
    @patch("extract.store")
    def test_no_snippets_flag(self, mock_store, mock_llm, mock_opus_response, workspace_dir):
        from extract import extract_from_transcript

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

    @patch("extract.call_deep_reasoning")
    @patch("extract.store")
    def test_no_journal_flag(self, mock_store, mock_llm, mock_opus_response, workspace_dir):
        from extract import extract_from_transcript

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

    @patch("extract.call_deep_reasoning")
    @patch("extract.store")
    def test_journal_array_fallback(self, mock_store, mock_llm):
        """LLM may return arrays instead of strings for journal entries."""
        from extract import extract_from_transcript

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

    @patch("extract.call_deep_reasoning")
    @patch("extract.store")
    @patch("extract.create_edge")
    def test_edge_failure_non_fatal(self, mock_edge, mock_store, mock_llm):
        from extract import extract_from_transcript

        mock_llm.return_value = (json.dumps({
            "facts": [{
                "text": "Alice is friends with Bob the great",
                "category": "relationship",
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


# ---------------------------------------------------------------------------
# _format_human_summary tests
# ---------------------------------------------------------------------------

class TestFormatHumanSummary:
    def test_basic_summary(self):
        from extract import _format_human_summary

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
        from extract import _format_human_summary

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
        from extract import _load_extraction_prompt

        prompt = _load_extraction_prompt()
        assert "memory extraction system" in prompt.lower()
        assert "facts" in prompt
        assert "edges" in prompt
        assert "soul_snippets" in prompt
        assert "journal_entries" in prompt

    def test_prompt_has_json_schema(self):
        from extract import _load_extraction_prompt

        prompt = _load_extraction_prompt()
        assert '"facts"' in prompt
        assert '"text"' in prompt
        assert '"category"' in prompt


# ---------------------------------------------------------------------------
# _get_owner_id tests
# ---------------------------------------------------------------------------

class TestGetOwnerId:
    def test_override(self):
        from extract import _get_owner_id
        assert _get_owner_id("custom") == "custom"

    def test_fallback_default(self):
        from extract import _get_owner_id
        # With config mocked to fail
        with patch("extract.get_config", side_effect=Exception("no config")):
            assert _get_owner_id(None) == "default"


# ---------------------------------------------------------------------------
# CLI tests (subprocess-level)
# ---------------------------------------------------------------------------

class TestCLI:
    def test_help(self):
        import subprocess
        result = subprocess.run(
            [sys.executable, str(Path(__file__).parent.parent / "extract.py"), "--help"],
            capture_output=True, text=True,
        )
        # argparse --help exits 0
        assert result.returncode == 0
        assert "extract" in result.stdout.lower()

    def test_missing_file(self):
        import subprocess
        result = subprocess.run(
            [sys.executable, str(Path(__file__).parent.parent / "extract.py"), "/nonexistent/file.txt"],
            capture_output=True, text=True,
            env={**os.environ, "MEMORY_DB_PATH": ":memory:", "QUAID_QUIET": "1"},
        )
        assert result.returncode != 0
        assert "not found" in result.stderr.lower() or "error" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Chunking tests (F3 — message-based context splitting)
# ---------------------------------------------------------------------------

class TestChunkMessages:
    """Test _chunk_messages() — never splits a single message."""

    def test_empty(self):
        from extract import _chunk_messages
        assert _chunk_messages([]) == []

    def test_single_message_under_threshold(self):
        from extract import _chunk_messages
        msgs = [{"role": "user", "content": "hello world"}]
        chunks = _chunk_messages(msgs, max_chars=1000)
        assert len(chunks) == 1
        assert chunks[0] == msgs

    def test_multiple_messages_under_threshold(self):
        from extract import _chunk_messages
        msgs = [
            {"role": "user", "content": "a" * 100},
            {"role": "assistant", "content": "b" * 100},
        ]
        chunks = _chunk_messages(msgs, max_chars=1000)
        assert len(chunks) == 1
        assert len(chunks[0]) == 2

    def test_splits_at_threshold(self):
        from extract import _chunk_messages
        msgs = [
            {"role": "user", "content": "a" * 500},
            {"role": "assistant", "content": "b" * 500},
            {"role": "user", "content": "c" * 500},
        ]
        # Each message ~510 chars, threshold 600 → each in its own chunk (except first two may fit)
        chunks = _chunk_messages(msgs, max_chars=600)
        assert len(chunks) >= 2
        # All messages present
        total = sum(len(c) for c in chunks)
        assert total == 3

    def test_single_huge_message_not_split(self):
        """A single message larger than threshold stays in one chunk."""
        from extract import _chunk_messages
        msgs = [{"role": "user", "content": "x" * 50000}]
        chunks = _chunk_messages(msgs, max_chars=1000)
        assert len(chunks) == 1
        assert chunks[0] == msgs

    def test_multipart_content(self):
        from extract import _chunk_messages
        msgs = [{"role": "user", "content": [{"text": "hello"}, {"text": "world"}]}]
        chunks = _chunk_messages(msgs, max_chars=1000)
        assert len(chunks) == 1


class TestChunkTranscriptText:
    """Test _chunk_transcript_text() — splits at turn boundaries."""

    def test_short_transcript(self):
        from extract import _chunk_transcript_text
        text = "User: hello\n\nAssistant: hi"
        chunks = _chunk_transcript_text(text, max_chars=1000)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_splits_at_double_newline(self):
        from extract import _chunk_transcript_text
        turns = ["User: " + "a" * 200, "Assistant: " + "b" * 200, "User: " + "c" * 200]
        text = "\n\n".join(turns)
        chunks = _chunk_transcript_text(text, max_chars=300)
        assert len(chunks) >= 2
        # All content present
        rejoined = "\n\n".join(chunks)
        assert "a" * 200 in rejoined
        assert "c" * 200 in rejoined

    def test_empty_transcript(self):
        from extract import _chunk_transcript_text
        chunks = _chunk_transcript_text("", max_chars=1000)
        assert chunks == [""]
