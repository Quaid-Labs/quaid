import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datastore.memorydb import session_logs
from lib.adapter import StandaloneAdapter, reset_adapter, set_adapter


def setup_function():
    reset_adapter()


def teardown_function():
    reset_adapter()


def test_index_session_log_embedding_pack_error_fail_hard_behavior(monkeypatch, tmp_path):
    set_adapter(StandaloneAdapter(home=tmp_path))
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr("datastore.memorydb.session_logs._lib_get_embedding", lambda _t: [0.1, 0.2])
    monkeypatch.setattr("datastore.memorydb.session_logs._lib_pack_embedding", lambda _v: (_ for _ in ()).throw(RuntimeError("pack failed")))

    with monkeypatch.context() as m:
        m.setattr("datastore.memorydb.session_logs.is_fail_hard_enabled", lambda: False)
        out = session_logs.index_session_log(session_id="sess-pack-soft", transcript="User: hi", owner_id="quaid")
        assert out["status"] == "indexed"

    with monkeypatch.context() as m:
        m.setattr("datastore.memorydb.session_logs.is_fail_hard_enabled", lambda: True)
        with pytest.raises(RuntimeError, match="embedding pack failed"):
            session_logs.index_session_log(session_id="sess-pack-hard", transcript="User: hi", owner_id="quaid")


def test_search_session_logs_unpack_error_fail_hard_behavior(monkeypatch, tmp_path):
    set_adapter(StandaloneAdapter(home=tmp_path))
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr("datastore.memorydb.session_logs._lib_get_embedding", lambda _t: [0.1, 0.2])

    session_logs.index_session_log(session_id="sess-unpack", transcript="User: hello world", owner_id="quaid")

    with monkeypatch.context() as m:
        m.setattr("datastore.memorydb.session_logs._lib_unpack_embedding", lambda _b: (_ for _ in ()).throw(RuntimeError("unpack failed")))
        m.setattr("datastore.memorydb.session_logs.is_fail_hard_enabled", lambda: False)
        out = session_logs.search_session_logs("hello", owner_id="quaid", limit=5, min_similarity=0.0)
        assert isinstance(out, list)

    with monkeypatch.context() as m:
        m.setattr("datastore.memorydb.session_logs._lib_unpack_embedding", lambda _b: (_ for _ in ()).throw(RuntimeError("unpack failed")))
        m.setattr("datastore.memorydb.session_logs.is_fail_hard_enabled", lambda: True)
        with pytest.raises(RuntimeError, match="embedding unpack failed"):
            session_logs.search_session_logs("hello", owner_id="quaid", limit=5, min_similarity=0.0)
