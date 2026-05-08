import json
import sys
from unittest.mock import MagicMock

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
