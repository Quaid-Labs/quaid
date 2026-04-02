import hashlib
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from datastore.memorydb import session_logs
from lib.adapter import TestAdapter, reset_adapter, set_adapter


def setup_function():
    reset_adapter()


def teardown_function():
    reset_adapter()


def test_session_log_index_list_load(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path); set_adapter(adapter)
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))

    transcript = (
        "User: My mother's name is Wendy.\n\n"
        "Assistant: Got it.\n\n"
        "User: My father's name is Kent.\n\n"
        "Assistant: Noted."
    )

    out = session_logs.index_session_log(
        session_id="sess-a1",
        transcript=transcript,
        owner_id="quaid",
        source_label="Compaction",
        source_channel="telegram",
        conversation_id="chat-123",
        participant_ids=["quaid", "user:owner"],
        participant_aliases={"operator-alias": "user:owner"},
        message_count=4,
    )
    assert out["status"] == "indexed"

    recent = session_logs.list_recent_sessions(limit=5, owner_id="quaid")
    assert len(recent) == 1
    assert recent[0]["session_id"] == "sess-a1"
    assert recent[0]["source_channel"] == "telegram"
    assert recent[0]["conversation_id"] == "chat-123"

    loaded = session_logs.load_session("sess-a1", owner_id="quaid")
    assert loaded is not None
    assert "Wendy" in loaded["transcript_text"]
    assert loaded["source_channel"] == "telegram"
    assert loaded["conversation_id"] == "chat-123"


def test_session_log_index_serializes_same_session(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path); set_adapter(adapter)
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))

    t1 = "User: alpha fact\n\nAssistant: noted."
    t2 = "User: beta fact\n\nAssistant: noted."

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(session_logs.index_session_log, session_id="sess-lock", transcript=t1, owner_id="quaid")
        f2 = pool.submit(session_logs.index_session_log, session_id="sess-lock", transcript=t2, owner_id="quaid")
        r1 = f1.result()
        r2 = f2.result()

    assert r1["status"] in {"indexed", "unchanged"}
    assert r2["status"] in {"indexed", "unchanged"}

    loaded = session_logs.load_session("sess-lock", owner_id="quaid")
    assert loaded is not None
    transcript_text = loaded["transcript_text"]
    content_hash = hashlib.sha256(transcript_text.encode("utf-8")).hexdigest()

    with session_logs._lib_get_connection() as conn:
        row = conn.execute(
            "SELECT content_hash FROM session_logs WHERE session_id = ?",
            ("sess-lock",),
        ).fetchone()
        assert row is not None
        assert str(row["content_hash"]) == content_hash


def test_session_log_cli_accepts_json_flag(monkeypatch, tmp_path, capsys):
    adapter = TestAdapter(tmp_path); set_adapter(adapter)
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))

    session_logs.index_session_log(
        session_id="sess-json",
        transcript="User: alpha\n\nAssistant: noted.",
        owner_id="quaid",
    )

    monkeypatch.setattr(sys, "argv", ["session_logs.py", "list", "--limit", "1", "--json"])
    assert session_logs._main() == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["sessions"][0]["session_id"] == "sess-json"

    monkeypatch.setattr(sys, "argv", ["session_logs.py", "load", "--session-id", "sess-json", "--json"])
    assert session_logs._main() == 0
    loaded = json.loads(capsys.readouterr().out)
    assert loaded["session"]["session_id"] == "sess-json"
