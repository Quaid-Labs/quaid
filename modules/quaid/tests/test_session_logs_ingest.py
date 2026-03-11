import json
from types import SimpleNamespace

from ingest import session_logs_ingest
from lib.adapter import TestAdapter, reset_adapter, set_adapter


def setup_function():
    reset_adapter()


def teardown_function():
    reset_adapter()


def test_ingest_from_transcript_path(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path); set_adapter(adapter)
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
        source_channel="telegram",
        conversation_id="chat-42",
        participant_ids=["user:owner", "agent:quaid"],
        participant_aliases={"<redacted>": "user:owner"},
        message_count=2,
        topic_hint="hello",
    )

    assert out["status"] == "indexed"
    assert captured["command"] == "ingest"
    assert "--session-id" in captured["args"]
    assert "sess-a" in captured["args"]
    assert "--owner" in captured["args"]
    assert "quaid" in captured["args"]
    assert "--source-channel" in captured["args"]
    assert "telegram" in captured["args"]
    assert "--conversation-id" in captured["args"]
    assert "chat-42" in captured["args"]


def test_call_session_logs_cli_includes_exit_code_and_streams(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path); set_adapter(adapter)

    def _fake_run(*_args, **_kwargs):
        return SimpleNamespace(returncode=7, stderr="boom", stdout="fallback")

    monkeypatch.setattr("ingest.session_logs_ingest.subprocess.run", _fake_run)

    try:
        session_logs_ingest._call_session_logs_cli("ingest", ["--session-id", "s1"])
    except RuntimeError as exc:
        msg = str(exc)
        assert "exit=7" in msg
        assert "stderr: boom" in msg
        assert "stdout: fallback" in msg
    else:
        raise AssertionError("expected RuntimeError")


def test_call_session_logs_cli_uses_module_entrypoint(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path); set_adapter(adapter)
    captured = {}

    def _fake_run(cmd, **_kwargs):
        captured["cmd"] = list(cmd)
        return SimpleNamespace(returncode=0, stderr="", stdout='{"ok": true}')

    monkeypatch.setattr("ingest.session_logs_ingest.subprocess.run", _fake_run)
    out = session_logs_ingest._call_session_logs_cli("list", ["--limit", "5"])

    assert out["ok"] is True
    assert captured["cmd"][:3] == ["python3", "-m", "datastore.memorydb.session_logs"]
    assert captured["cmd"][3:] == ["list", "--limit", "5"]


def test_normalize_participant_aliases_accepts_json_object_string():
    out = session_logs_ingest._normalize_participant_aliases('{" <redacted> ":" user:owner ","":"x"}')
    assert out == {"<redacted>": "user:owner"}


def test_normalize_participant_aliases_rejects_non_object_json():
    try:
        session_logs_ingest._normalize_participant_aliases('["not","an","object"]')
    except ValueError as exc:
        assert "JSON object" in str(exc)
    else:
        raise AssertionError("expected ValueError")
