import json

from ingest import session_logs_ingest
from lib.adapter import StandaloneAdapter, reset_adapter, set_adapter


def setup_function():
    reset_adapter()


def teardown_function():
    reset_adapter()


def test_ingest_from_transcript_path(monkeypatch, tmp_path):
    set_adapter(StandaloneAdapter(home=tmp_path))
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))

    captured = {}

    def _fake_call(command, args):
        captured["command"] = command
        captured["args"] = list(args)
        return {"status": "indexed", "session_id": "sess-a", "chunks": 1}

    monkeypatch.setattr("ingest.session_logs_ingest._call_session_logs_cli", _fake_call)

    transcript = tmp_path / "t.txt"
    transcript.write_text("User: hello\n\nAssistant: hi", encoding="utf-8")

    out = session_logs_ingest._run(
        session_id="sess-a",
        owner_id="quaid",
        label="Compaction",
        transcript_path=str(transcript),
        message_count=2,
        topic_hint="hello",
    )

    assert out["status"] == "indexed"
    assert captured["command"] == "ingest"
    assert "--session-id" in captured["args"]
    assert "sess-a" in captured["args"]
    assert "--owner" in captured["args"]
    assert "quaid" in captured["args"]
