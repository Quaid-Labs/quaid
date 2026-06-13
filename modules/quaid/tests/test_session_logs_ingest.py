import json
import sys
from unittest.mock import MagicMock

import pytest

from ingest import session_logs_ingest
from lib.adapter import TestAdapter, reset_adapter, set_adapter


def setup_function():
    reset_adapter()


def teardown_function():
    reset_adapter()


def test_ingest_from_transcript_path(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path); set_adapter(adapter)
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))

    fake_bridge = MagicMock()
    fake_bridge.store_session_transcript.return_value = {"status": "indexed", "session_id": "sess-a", "chunks": 1}
    monkeypatch.setattr("ingest.session_logs_ingest.get_session_memory_bridge", lambda: fake_bridge)

    transcript = tmp_path / "t.txt"
    transcript.write_text("User: hello\n\nAssistant: hi", encoding="utf-8")

    out = session_logs_ingest._run(
        session_id="sess-a",
        owner_id="quaid",
        label="Compaction",
        transcript_path=str(transcript),
        source_channel="telegram",
        conversation_id="chat-42",
        participant_ids=["user:owner", "agent:quaid"],
        participant_aliases={"operator-alias": "user:owner"},
        message_count=2,
        topic_hint="hello",
    )

    assert out["status"] == "indexed"
    kwargs = fake_bridge.store_session_transcript.call_args.kwargs
    assert kwargs["session_id"] == "sess-a"
    assert kwargs["owner_id"] == "quaid"
    assert kwargs["source_channel"] == "telegram"
    assert kwargs["conversation_id"] == "chat-42"
    assert kwargs["participant_ids"] == ["user:owner", "agent:quaid"]
    assert kwargs["participant_aliases"] == {"operator-alias": "user:owner"}
    assert kwargs["transcript"] == "User: hello\n\nAssistant: hi"


def test_transcript_path_jsonl_is_adapter_parsed_before_sessiondb(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)

    fake_bridge = MagicMock()
    fake_bridge.store_session_transcript.return_value = {"status": "indexed", "session_id": "sess-jsonl", "chunks": 1}
    monkeypatch.setattr("ingest.session_logs_ingest.get_session_memory_bridge", lambda: fake_bridge)

    provider_payload = {
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 900, "output_tokens": 12},
    }
    raw_session = tmp_path / "session.jsonl"
    raw_session.write_text(
        "\n".join(
            [
                json.dumps({"type": "session_meta", "payload": {"cwd": str(tmp_path)}}),
                json.dumps({"role": "user", "content": "The Hokkaido route includes Niseko for four days."}),
                json.dumps({"role": "assistant", "content": json.dumps(provider_payload)}),
                json.dumps({"role": "assistant", "content": "Noted."}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    out = session_logs_ingest._run(
        session_id="sess-jsonl",
        owner_id="quaid",
        label="SessionEnd",
        transcript_path=str(raw_session),
    )

    assert out["status"] == "indexed"
    transcript = fake_bridge.store_session_transcript.call_args.kwargs["transcript"]
    assert "User: The Hokkaido route includes Niseko for four days." in transcript
    assert "Assistant: Noted." in transcript
    assert "session_meta" not in transcript
    assert "stop_reason" not in transcript
    assert "input_tokens" not in transcript


def test_transcript_path_malformed_jsonl_after_session_shape_still_uses_adapter(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)

    fake_bridge = MagicMock()
    fake_bridge.store_session_transcript.return_value = {"status": "indexed", "session_id": "sess-malformed", "chunks": 1}
    monkeypatch.setattr("ingest.session_logs_ingest.get_session_memory_bridge", lambda: fake_bridge)

    provider_payload = {
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 800, "output_tokens": 8},
    }
    raw_session = tmp_path / "session-malformed.jsonl"
    raw_session.write_text(
        "\n".join(
            [
                json.dumps({"role": "user", "content": "The ferry from Aomori reaches Hakodate before lunch."}),
                "{malformed-json",
                json.dumps({"role": "assistant", "content": json.dumps(provider_payload)}),
                json.dumps({"role": "assistant", "content": "Logged."}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    out = session_logs_ingest._run(
        session_id="sess-malformed",
        owner_id="quaid",
        label="SessionEnd",
        transcript_path=str(raw_session),
    )

    assert out["status"] == "indexed"
    transcript = fake_bridge.store_session_transcript.call_args.kwargs["transcript"]
    assert "User: The ferry from Aomori reaches Hakodate before lunch." in transcript
    assert "Assistant: Logged." in transcript
    assert "malformed-json" not in transcript
    assert "stop_reason" not in transcript
    assert "input_tokens" not in transcript


def test_transcript_path_malformed_first_line_still_uses_adapter(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)

    fake_bridge = MagicMock()
    fake_bridge.store_session_transcript.return_value = {"status": "indexed", "session_id": "sess-early-malformed", "chunks": 1}
    monkeypatch.setattr("ingest.session_logs_ingest.get_session_memory_bridge", lambda: fake_bridge)

    raw_session = tmp_path / "session-early-malformed.jsonl"
    raw_session.write_text(
        "{not-json-yet}\n"
        + json.dumps({"role": "user", "content": "The first valid row should still classify this as JSONL."})
        + "\n"
        + json.dumps({"role": "assistant", "content": "Stored."})
        + "\n",
        encoding="utf-8",
    )

    out = session_logs_ingest._run(
        session_id="sess-early-malformed",
        owner_id="quaid",
        label="SessionEnd",
        transcript_path=str(raw_session),
    )

    assert out["status"] == "indexed"
    transcript = fake_bridge.store_session_transcript.call_args.kwargs["transcript"]
    assert "User: The first valid row should still classify this as JSONL." in transcript
    assert "{not-json-yet}" not in transcript


def test_normalize_participant_aliases_accepts_json_object_string():
    out = session_logs_ingest._normalize_participant_aliases('{" operator-alias ":" user:owner ","":"x"}')
    assert out == {"operator-alias": "user:owner"}


def test_normalize_participant_aliases_rejects_non_object_json():
    try:
        session_logs_ingest._normalize_participant_aliases('["not","an","object"]')
    except ValueError as exc:
        assert "JSON object" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_normalize_participant_aliases_rejects_malformed_json():
    try:
        session_logs_ingest._normalize_participant_aliases('{"operator":')
    except ValueError as exc:
        assert "valid JSON object" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_resolve_transcript_source_skips_toctou_deleted_path_when_failsoft(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)
    transcript = tmp_path / "vanishing.txt"
    transcript.write_text("User: hello", encoding="utf-8")

    monkeypatch.setattr(session_logs_ingest, "is_fail_hard_enabled", lambda: False)

    original_read_text = type(transcript).read_text

    def _vanish(self, *args, **kwargs):
        if self == transcript:
            transcript.unlink(missing_ok=True)
            raise FileNotFoundError(str(transcript))
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(type(transcript), "read_text", _vanish)
    src_path, content, source_kind = session_logs_ingest._resolve_transcript_source(
        session_id="sess-vanish",
        session_file=None,
        transcript_path=str(transcript),
    )

    assert src_path is None
    assert content is None
    assert source_kind == "missing"


def test_resolve_transcript_source_raises_toctou_deleted_path_under_failhard(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)
    transcript = tmp_path / "vanishing.txt"
    transcript.write_text("User: hello", encoding="utf-8")

    monkeypatch.setattr(session_logs_ingest, "is_fail_hard_enabled", lambda: True)

    original_read_text = type(transcript).read_text

    def _vanish(self, *args, **kwargs):
        if self == transcript:
            transcript.unlink(missing_ok=True)
            raise FileNotFoundError(str(transcript))
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(type(transcript), "read_text", _vanish)
    try:
        session_logs_ingest._resolve_transcript_source(
            session_id="sess-vanish",
            session_file=None,
            transcript_path=str(transcript),
        )
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected FileNotFoundError under failHard")


def test_resolve_transcript_source_skips_unicode_decode_error_when_failsoft(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)
    transcript = tmp_path / "non-utf8.txt"
    transcript.write_bytes(b"\xff\xfe\x00")

    monkeypatch.setattr(session_logs_ingest, "is_fail_hard_enabled", lambda: False)

    src_path, content, source_kind = session_logs_ingest._resolve_transcript_source(
        session_id="sess-unicode",
        session_file=None,
        transcript_path=str(transcript),
    )

    assert src_path is None
    assert content is None
    assert source_kind == "missing"


def test_resolve_transcript_source_raises_unicode_decode_error_under_failhard(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)
    transcript = tmp_path / "non-utf8.txt"
    transcript.write_bytes(b"\xff\xfe\x00")

    monkeypatch.setattr(session_logs_ingest, "is_fail_hard_enabled", lambda: True)

    try:
        session_logs_ingest._resolve_transcript_source(
            session_id="sess-unicode",
            session_file=None,
            transcript_path=str(transcript),
        )
    except UnicodeDecodeError:
        pass
    else:
        raise AssertionError("expected UnicodeDecodeError under failHard")


def test_resolve_transcript_source_falls_back_from_empty_transcript_path(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)
    empty_transcript = tmp_path / "empty.txt"
    empty_transcript.write_text("", encoding="utf-8")
    session_file = tmp_path / "session.jsonl"
    session_file.write_text(
        json.dumps({"role": "user", "content": "Fallback transcript survives empty source."}) + "\n",
        encoding="utf-8",
    )

    src_path, content, source_kind = session_logs_ingest._resolve_transcript_source(
        session_id="sess-empty",
        session_file=str(session_file),
        transcript_path=str(empty_transcript),
    )

    assert src_path == session_file
    assert source_kind == "session_file"
    assert "Fallback transcript survives empty source." in str(content)


def test_session_logs_ingest_unavailable_transcript_raises_when_fail_hard(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)
    monkeypatch.setattr(session_logs_ingest, "is_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="session transcript unavailable under failHard"):
        session_logs_ingest._run(
            session_id="sess-missing",
            owner_id="quaid",
            label="SessionEnd",
            session_file=None,
            transcript_path=str(tmp_path / "missing.txt"),
        )


def test_main_accepts_json_flag_for_list_and_load(monkeypatch, tmp_path, capsys):
    adapter = TestAdapter(tmp_path); set_adapter(adapter)
    fake_bridge = MagicMock()
    fake_bridge.list_session_transcripts.return_value = [{"session_id": "sess-json"}]
    fake_bridge.load_session_transcript.return_value = {"session_id": "sess-json"}
    monkeypatch.setattr("ingest.session_logs_ingest.get_session_memory_bridge", lambda: fake_bridge)

    monkeypatch.setattr(sys, "argv", ["session_logs_ingest.py", "list", "--limit", "1", "--json"])
    assert session_logs_ingest.main() == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["session_id"] == "sess-json"

    monkeypatch.setattr(sys, "argv", ["session_logs_ingest.py", "load", "--session-id", "sess-json", "--json"])
    assert session_logs_ingest.main() == 0
    loaded = json.loads(capsys.readouterr().out)
    assert loaded["session_id"] == "sess-json"

    fake_bridge.list_session_transcripts.assert_called_once_with(limit=1, owner_id=None)
    fake_bridge.load_session_transcript.assert_called_once_with(session_id="sess-json", owner_id=None)
