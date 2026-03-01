import os
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datastore.memorydb import maintenance_ops


class _DummyResult:
    def __init__(self, rows=None, rowcount=1):
        self._rows = rows or []
        self.rowcount = rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


def test_default_owner_fallback_and_fail_hard():
    with patch.object(maintenance_ops, "_cfg", SimpleNamespace()), \
         patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=False):
        assert maintenance_ops._default_owner_id() == "default"

    with patch.object(maintenance_ops, "_cfg", SimpleNamespace()), \
         patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=True):
        with pytest.raises(RuntimeError, match="default owner"):
            maintenance_ops._default_owner_id()


def test_get_last_successful_janitor_completed_at_fail_hard_behavior():
    class _BrokenGraph:
        @contextmanager
        def _get_conn(self):
            raise RuntimeError("db unavailable")
            yield

    graph = _BrokenGraph()
    with patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=False):
        assert maintenance_ops.get_last_successful_janitor_completed_at(graph) is None

    with patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=True):
        with pytest.raises(RuntimeError, match="janitor completion status"):
            maintenance_ops.get_last_successful_janitor_completed_at(graph)


def test_recall_candidates_fail_hard_behavior():
    class _Conn:
        def execute(self, sql, params):
            if "nodes_fts MATCH" in sql:
                raise RuntimeError("fts broken")
            return _DummyResult(rows=[])

    class _Graph:
        @contextmanager
        def _get_conn(self):
            yield _Conn()

        def _row_to_node(self, row):
            return row

    graph = _Graph()
    with patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=False):
        out = maintenance_ops.recall_candidates(graph, "alice likes coffee", "x1", limit=5)
        assert out == []

    with patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=True):
        with pytest.raises(RuntimeError, match="fail-hard mode"):
            maintenance_ops.recall_candidates(graph, "alice likes coffee", "x1", limit=5)


def test_fix_vec_nodes_insert_error_respects_fail_hard():
    class _ConnRecovering:
        def execute(self, sql, params):
            if "INSERT OR REPLACE INTO vec_nodes" in sql:
                raise RuntimeError("vec write failed")
            return _DummyResult(rowcount=1)

    class _ConnAlwaysFail:
        def execute(self, sql, params):
            if "vec_nodes" in sql:
                raise RuntimeError("vec write failed")
            return _DummyResult(rowcount=1)

    class _GraphRecovering:
        @contextmanager
        def _get_conn(self):
            yield _ConnRecovering()

    class _GraphAlwaysFail:
        @contextmanager
        def _get_conn(self):
            yield _ConnAlwaysFail()

    decisions = [{"id": "n1", "action": "FIX", "new_text": "updated text", "edges": []}]

    with patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=False), \
         patch("lib.embeddings.get_embedding", return_value=[0.1]), \
         patch("lib.embeddings.pack_embedding", return_value=b"x"):
        out = maintenance_ops.apply_review_decisions_from_list(_GraphRecovering(), decisions, dry_run=False)
        assert out["fixed"] == 1

    # Fail-hard should not raise when delete+insert recovery succeeds.
    with patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=True), \
         patch("lib.embeddings.get_embedding", return_value=[0.1]), \
         patch("lib.embeddings.pack_embedding", return_value=b"x"):
        out = maintenance_ops.apply_review_decisions_from_list(_GraphRecovering(), decisions, dry_run=False)
        assert out["fixed"] == 1

    # Fail-hard should raise only if both primary upsert and fallback fail.
    with patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=True), \
         patch("lib.embeddings.get_embedding", return_value=[0.1]), \
         patch("lib.embeddings.pack_embedding", return_value=b"x"):
        with pytest.raises(RuntimeError, match="vec_nodes update failed"):
            maintenance_ops.apply_review_decisions_from_list(_GraphAlwaysFail(), decisions, dry_run=False)


def test_contradiction_keep_a_uses_atomic_sql_path(monkeypatch):
    metrics = maintenance_ops.JanitorMetrics()
    pending = [{
        "id": "c1",
        "node_a_id": "na",
        "node_b_id": "nb",
        "text_a": "A",
        "text_b": "B",
        "conf_a": 0.9,
        "conf_b": 0.8,
        "created_a": "2026-01-01",
        "created_b": "2026-01-02",
        "source_a": "user",
        "source_b": "user",
        "speaker_a": "alice",
        "speaker_b": "alice",
        "access_a": 1,
        "access_b": 1,
        "explanation": "conflict",
    }]

    class _Conn:
        def __init__(self):
            self.sql = []

        def execute(self, sql, params=()):
            self.sql.append(str(sql))
            if "SELECT COUNT(*) FROM contradictions" in sql:
                return _DummyResult(rows=[(1,)])
            return _DummyResult(rowcount=1)

    class _Graph:
        def __init__(self):
            self.calls = []

        @contextmanager
        def _get_conn(self):
            conn = _Conn()
            self.calls.append(conn)
            yield conn

    graph = _Graph()
    llm_batches = [{
        "batch_num": 1,
        "batch": pending,
        "prompt_tag": "",
        "response_duration": ('[{"pair": 1, "action": "KEEP_A", "reason": "latest"}]', 0.0),
    }]

    with patch.object(maintenance_ops, "get_pending_contradictions", return_value=pending), \
         patch.object(maintenance_ops, "_run_llm_batches_parallel", return_value=llm_batches), \
         patch.object(maintenance_ops, "resolve_contradiction", side_effect=AssertionError("legacy path called")):
        out = maintenance_ops.resolve_contradictions_with_opus(graph, metrics, dry_run=False, max_items=1)

    assert out["resolved"] == 1
    assert len(graph.calls) >= 2  # count query + apply transaction
    apply_sql = "\n".join(graph.calls[-1].sql)
    assert "UPDATE nodes SET superseded_by" in apply_sql
    assert "UPDATE contradictions" in apply_sql


def test_backfill_embeddings_vec_upsert_failure_warns_and_continues():
    class _Conn:
        def execute(self, sql, params=()):
            text = str(sql).strip().upper()
            if text.startswith("SELECT ID, NAME FROM NODES WHERE EMBEDDING IS NULL"):
                return _DummyResult(rows=[{"id": "n1", "name": "alpha node"}])
            if text.startswith("SELECT COUNT(*) FROM NODES_FTS"):
                return _DummyResult(rows=[(0,)])
            if text.startswith("SELECT COUNT(*) FROM NODES"):
                return _DummyResult(rows=[(1,)])
            if text.startswith("SELECT ROWID, NAME FROM NODES ORDER BY ROWID DESC LIMIT 1"):
                return _DummyResult(rows=[(1, "alpha node")])
            if text.startswith("SELECT ROWID FROM NODES_FTS WHERE ROWID = ?"):
                return _DummyResult(rows=[(1,)])
            return _DummyResult(rowcount=1)

    class _Graph:
        @contextmanager
        def _get_conn(self):
            yield _Conn()

    metrics = maintenance_ops.JanitorMetrics()
    graph = _Graph()

    with patch("lib.embeddings.get_embedding", return_value=[0.1, 0.2]), \
         patch("lib.embeddings.pack_embedding", return_value=b"emb"), \
         patch.object(maintenance_ops, "_upsert_vec_embedding", side_effect=RuntimeError("vec write failed")):
        out = maintenance_ops.backfill_embeddings(graph, metrics, dry_run=False)

    assert out["found"] == 1
    assert out["embedded"] == 1
    assert metrics.summary()["warnings"] >= 1
