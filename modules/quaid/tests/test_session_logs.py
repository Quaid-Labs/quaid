import hashlib

from datastore.memorydb import session_logs
from lib.adapter import StandaloneAdapter, reset_adapter, set_adapter


def _fake_embedding(text: str):
    h = hashlib.md5(text.encode("utf-8")).digest()
    return [float(b) / 255.0 for b in h] * 8


def setup_function():
    reset_adapter()


def teardown_function():
    reset_adapter()


def test_session_log_index_list_load_search(monkeypatch, tmp_path):
    set_adapter(StandaloneAdapter(home=tmp_path))
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr("datastore.memorydb.session_logs._lib_get_embedding", _fake_embedding)

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
        message_count=4,
    )
    assert out["status"] == "indexed"
    assert out["chunks"] >= 1

    recent = session_logs.list_recent_sessions(limit=5, owner_id="quaid")
    assert len(recent) == 1
    assert recent[0]["session_id"] == "sess-a1"

    loaded = session_logs.load_session("sess-a1", owner_id="quaid")
    assert loaded is not None
    assert "Wendy" in loaded["transcript_text"]

    hits = session_logs.search_session_logs("mother Wendy", owner_id="quaid", limit=5, min_similarity=0.05)
    assert len(hits) >= 1
    assert any(h["session_id"] == "sess-a1" for h in hits)


def test_session_log_last_session_excludes_current(monkeypatch, tmp_path):
    set_adapter(StandaloneAdapter(home=tmp_path))
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr("datastore.memorydb.session_logs._lib_get_embedding", _fake_embedding)

    session_logs.index_session_log(
        session_id="sess-old",
        transcript="User: old fact\n\nAssistant: ack",
        owner_id="quaid",
        source_label="Reset",
        message_count=2,
    )
    session_logs.index_session_log(
        session_id="sess-new",
        transcript="User: new fact\n\nAssistant: ack",
        owner_id="quaid",
        source_label="Compaction",
        message_count=2,
    )

    last = session_logs.load_last_session(owner_id="quaid", exclude_session_id="sess-new")
    assert last is not None
    assert last["session_id"] == "sess-old"
