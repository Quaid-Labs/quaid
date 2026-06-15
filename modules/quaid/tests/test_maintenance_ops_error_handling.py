import os
import sys
import json
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


def test_record_health_snapshot_aggregates_confidence_buckets_in_sql():
    inserted = {}

    class _Conn:
        def execute(self, sql, params=()):
            text = str(sql).strip().upper()
            if text == "SELECT CONFIDENCE FROM NODES":
                raise AssertionError("record_health_snapshot must not fetch every node confidence")
            if "SUM(CASE WHEN COALESCE(CONFIDENCE, 0) < 0.3" in text:
                return _DummyResult(rows=[{"b0": 2, "b1": 3, "b2": 5, "b3": 7, "b4": 11}])
            if text.startswith("INSERT INTO HEALTH_SNAPSHOTS"):
                inserted["params"] = params
                return _DummyResult(rowcount=1)
            raise AssertionError(f"unexpected SQL: {sql}")

    class _Graph:
        @contextmanager
        def _get_conn(self):
            yield _Conn()

    health = {
        "total_nodes": 28,
        "total_edges": 4,
        "confidence_by_status": {
            "active": {"count": 10, "avg_confidence": 0.8},
            "approved": {"count": 5, "avg_confidence": 0.4},
        },
        "embedding_coverage": "14/28",
        "staleness_distribution": {"90d+": 2},
        "orphan_nodes": 1,
    }

    summary = maintenance_ops.record_health_snapshot(_Graph(), health)

    assert summary == {"total": 28, "avg_confidence": pytest.approx(10 / 15), "total_edges": 4}
    confidence_distribution = json.loads(inserted["params"][4])
    assert confidence_distribution == {
        "0.0-0.3": 2,
        "0.3-0.5": 3,
        "0.5-0.7": 5,
        "0.7-0.9": 7,
        "0.9-1.0": 11,
    }


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


def test_get_nodes_since_applies_sql_limit_before_materializing():
    calls = []

    class _Conn:
        def execute(self, sql, params=()):
            calls.append((str(sql), params))
            return _DummyResult(rows=[])

    class _Graph:
        @contextmanager
        def _get_conn(self):
            yield _Conn()

        def _row_to_node(self, row):
            return row

    since = maintenance_ops.datetime(2026, 1, 2, 3, 4, 5)
    maintenance_ops.get_nodes_since(_Graph(), limit=10)
    maintenance_ops.get_nodes_since(_Graph(), since=since, limit=7)

    assert "LIMIT ?" in calls[0][0]
    assert calls[0][1] == (10,)
    assert "created_at > ?" in calls[1][0]
    assert "LIMIT ?" in calls[1][0]
    assert calls[1][1] == (since.isoformat(), 7)


def test_recall_similar_pairs_forwards_max_nodes_as_query_limit(monkeypatch):
    calls = []

    def fake_get_nodes_since(graph, since=None, limit=0):
        calls.append({"since": since, "limit": limit})
        return []

    monkeypatch.setattr(maintenance_ops, "get_nodes_since", fake_get_nodes_since)

    metrics = maintenance_ops.JanitorMetrics()
    out = maintenance_ops.recall_similar_pairs(object(), metrics, since=None, max_nodes=23)

    assert out == {"duplicates": [], "contradictions": [], "node_carryover": 0}
    assert calls == [{"since": None, "limit": 24}]


def test_recall_similar_pairs_bounds_materialized_nodes_and_reports_carryover(monkeypatch):
    calls = []
    rows = [
        SimpleNamespace(id=f"n{idx}", name=f"node {idx}", embedding=None)
        for idx in range(3)
    ]

    def fake_get_nodes_since(graph, since=None, limit=0):
        calls.append({"since": since, "limit": limit})
        return rows[:limit]

    monkeypatch.setattr(maintenance_ops, "get_nodes_since", fake_get_nodes_since)

    metrics = maintenance_ops.JanitorMetrics()
    out = maintenance_ops.recall_similar_pairs(object(), metrics, since=None, max_nodes=2)

    assert out == {"duplicates": [], "contradictions": [], "node_carryover": 1}
    assert calls == [{"since": None, "limit": 3}]


def test_recall_similar_pairs_includes_temporal_metadata_for_dedup(monkeypatch):
    new_node = SimpleNamespace(
        id="new",
        name="Alex moved to Boston",
        embedding=[1.0, 0.0],
        type="Fact",
        created_at="2024-02-01T00:00:00",
        valid_from="2024-02-01",
        valid_until=None,
        occurred_start="2024-02-01",
        occurred_end="2024-02-01",
        mentioned_at="2024-02-02T00:00:00",
    )
    old_node = SimpleNamespace(
        id="old",
        name="Alex moved to Seattle",
        embedding=[1.0, 0.0],
        type="Fact",
        created_at="2025-03-01T00:00:00",
        valid_from="2025-03-01",
        valid_until=None,
        occurred_start="2025-03-01",
        occurred_end="2025-03-01",
        mentioned_at="2025-03-02T00:00:00",
    )

    class _Graph:
        def cosine_similarity(self, emb_a, emb_b):
            return 0.9

    monkeypatch.setattr(maintenance_ops, "get_nodes_since", lambda graph, since=None, limit=0: [new_node])
    monkeypatch.setattr(maintenance_ops, "recall_candidates", lambda graph, text, node_id: [old_node])

    out = maintenance_ops.recall_similar_pairs(_Graph(), maintenance_ops.JanitorMetrics())

    dup = out["duplicates"][0]
    assert dup["occurred_start_a"] == "2024-02-01"
    assert dup["mentioned_at_a"] == "2024-02-02T00:00:00"
    assert dup["occurred_start_b"] == "2025-03-01"
    assert dup["mentioned_at_b"] == "2025-03-02T00:00:00"


def test_batch_duplicate_check_includes_temporal_context_in_prompt(monkeypatch):
    captured = {}

    def fake_call_fast_reasoning(prompt, **kwargs):
        captured["prompt"] = prompt
        return ('[{"pair": 1, "action": "keep_both", "reason": "different dates"}]', 0.01)

    monkeypatch.setattr(maintenance_ops, "call_fast_reasoning", fake_call_fast_reasoning)

    pairs = [{
        "text_a": "Alex moved to Boston",
        "text_b": "Alex moved to Seattle",
        "similarity": 0.9,
        "occurred_start_a": "2024-02-01",
        "occurred_start_b": "2025-03-01",
        "valid_from_a": "2024-02-01",
        "valid_from_b": "2025-03-01",
        "mentioned_at_a": "2024-02-02T00:00:00",
        "mentioned_at_b": "2025-03-02T00:00:00",
    }]

    result = maintenance_ops.batch_duplicate_check(pairs, maintenance_ops.JanitorMetrics())

    assert result == [None]
    assert "occurred_start: 2024-02-01" in captured["prompt"]
    assert "occurred_start: 2025-03-01" in captured["prompt"]
    assert "different dates or validity periods" in captured["prompt"]


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
    monkeypatch.setenv("QUAID_NOW", "2026-03-11T00:00:00Z")
    monkeypatch.setattr(maintenance_ops, "CONTRADICTION_ENABLED", True)
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
            self.params = []

        def execute(self, sql, params=()):
            self.sql.append(str(sql))
            self.params.append(params)
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
    assert "datetime('now')" not in apply_sql
    flat_params = [item for params in graph.calls[-1].params for item in params]
    assert flat_params.count("2026-03-11T00:00:00") == 3


def test_quaid_now_rejects_malformed_override(monkeypatch):
    monkeypatch.setenv("QUAID_NOW", "not-a-date")

    with pytest.raises(ValueError, match="Invalid QUAID_NOW"):
        maintenance_ops._quaid_now()


def test_review_dedup_rejections_uses_quaid_now_for_sql_timestamps(monkeypatch):
    monkeypatch.setenv("QUAID_NOW", "2026-03-11T00:00:00Z")
    metrics = maintenance_ops.JanitorMetrics()

    class _Conn:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params=()):
            self.calls.append((str(sql), params))
            if "SELECT COUNT(*) FROM dedup_log" in sql:
                return _DummyResult(rows=[(0,)])
            return _DummyResult(rowcount=2)

    class _Graph:
        def __init__(self):
            self.conn = _Conn()

        @contextmanager
        def _get_conn(self):
            yield self.conn

    graph = _Graph()
    with patch.object(maintenance_ops, "get_recent_dedup_rejections", return_value=[]):
        out = maintenance_ops.review_dedup_rejections(graph, metrics, dry_run=False, max_items=1)

    assert out["confirmed"] == 2
    assert out["reviewed"] == 2
    sql_text = "\n".join(sql for sql, _params in graph.conn.calls)
    assert "datetime('now')" not in sql_text
    assert "created_at > ?" in sql_text
    assert ("2026-03-11T00:00:00",) in [params for _sql, params in graph.conn.calls]
    assert ("2026-03-10T00:00:00",) in [params for _sql, params in graph.conn.calls]


def test_review_decayed_memories_uses_quaid_now_for_queue_review(monkeypatch):
    monkeypatch.setenv("QUAID_NOW", "2026-03-11T00:00:00Z")
    metrics = maintenance_ops.JanitorMetrics()
    pending = [{
        "id": "q1",
        "node_id": "n1",
        "node_text": "Quaid still uses the archive shelf",
        "node_type": "Fact",
        "confidence_at_queue": 0.2,
        "access_count": 0,
        "last_accessed": "2026-01-01T00:00:00",
        "created_at_node": "2025-01-01T00:00:00",
        "verified": 0,
    }]

    class _Conn:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params=()):
            self.calls.append((str(sql), params))
            if "SELECT COUNT(*) FROM decay_review_queue" in sql:
                return _DummyResult(rows=[(1,)])
            if "SELECT attributes FROM nodes" in sql:
                return _DummyResult(rows=[{"attributes": "{\"extraction_confidence\": 0.8}"}])
            return _DummyResult(rowcount=1)

    class _Graph:
        def __init__(self):
            self.conn = _Conn()

        @contextmanager
        def _get_conn(self):
            yield self.conn

    llm_batches = [{
        "batch_num": 1,
        "batch": pending,
        "prompt_tag": "",
        "response_duration": ('[{"item": 1, "action": "EXTEND", "reason": "still useful"}]', 0.0),
    }]

    graph = _Graph()
    with patch.object(maintenance_ops, "get_pending_decay_reviews", return_value=pending), \
         patch.object(maintenance_ops, "_run_llm_batches_parallel", return_value=llm_batches):
        out = maintenance_ops.review_decayed_memories(graph, metrics, dry_run=False, max_items=1)

    assert out["extended"] == 1
    sql_text = "\n".join(sql for sql, _params in graph.conn.calls)
    assert "datetime('now')" not in sql_text
    flat_params = [item for _sql, params in graph.conn.calls for item in params]
    assert flat_params.count("2026-03-11T00:00:00") == 2


def test_backfill_embeddings_vec_upsert_failure_warns_and_continues(monkeypatch):
    monkeypatch.delenv("QUAID_JANITOR_EMBED_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("OLLAMA_EMBED_TIMEOUT_S", raising=False)
    monkeypatch.delenv("QUAID_JANITOR_EMBED_BACKFILL_LIMIT", raising=False)

    class _Conn:
        def execute(self, sql, params=()):
            text = str(sql).strip().upper()
            if text.startswith("SELECT ID, NAME FROM NODES WHERE EMBEDDING IS NULL"):
                assert "LIMIT ?" in text
                assert params == (1000,)
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

    with patch("lib.embeddings.get_embedding", return_value=[0.1, 0.2]) as get_embedding, \
         patch("lib.embeddings.pack_embedding", return_value=b"emb"), \
         patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=False), \
         patch.object(maintenance_ops, "_upsert_vec_embedding", side_effect=RuntimeError("vec write failed")):
        out = maintenance_ops.backfill_embeddings(graph, metrics, dry_run=False)

    assert out["found"] == 1
    assert out["embedded"] == 1
    assert metrics.summary()["warnings"] >= 1
    get_embedding.assert_called_once_with("alpha node", timeout_s=120.0)


def test_backfill_embeddings_vec_upsert_failure_raises_under_failhard(monkeypatch):
    monkeypatch.delenv("QUAID_JANITOR_EMBED_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("OLLAMA_EMBED_TIMEOUT_S", raising=False)
    monkeypatch.delenv("QUAID_JANITOR_EMBED_BACKFILL_LIMIT", raising=False)

    class _Conn:
        def execute(self, sql, params=()):
            text = str(sql).strip().upper()
            if text.startswith("SELECT ID, NAME FROM NODES WHERE EMBEDDING IS NULL"):
                return _DummyResult(rows=[{"id": "n1", "name": "alpha node"}])
            return _DummyResult(rowcount=1)

    class _Graph:
        @contextmanager
        def _get_conn(self):
            yield _Conn()

    with patch("lib.embeddings.get_embedding", return_value=[0.1, 0.2]), \
         patch("lib.embeddings.pack_embedding", return_value=b"emb"), \
         patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=True), \
         patch.object(maintenance_ops, "_upsert_vec_embedding", side_effect=RuntimeError("vec write failed")):
        with pytest.raises(RuntimeError, match="vec write failed"):
            maintenance_ops.backfill_embeddings(_Graph(), maintenance_ops.JanitorMetrics(), dry_run=False)


def test_backfill_embeddings_uses_global_timeout_when_janitor_override_unset(monkeypatch):
    class _Conn:
        def execute(self, sql, params=()):
            text = str(sql).strip().upper()
            if text.startswith("SELECT ID, NAME FROM NODES WHERE EMBEDDING IS NULL"):
                assert "LIMIT ?" in text
                assert params == (1000,)
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

    monkeypatch.delenv("QUAID_JANITOR_EMBED_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("QUAID_JANITOR_EMBED_BACKFILL_LIMIT", raising=False)
    monkeypatch.setenv("OLLAMA_EMBED_TIMEOUT_S", "85")
    metrics = maintenance_ops.JanitorMetrics()
    graph = _Graph()

    with patch("lib.embeddings.get_embedding", return_value=[0.1, 0.2]) as get_embedding, \
         patch("lib.embeddings.pack_embedding", return_value=b"emb"), \
         patch.object(maintenance_ops, "_upsert_vec_embedding", return_value=None):
        out = maintenance_ops.backfill_embeddings(graph, metrics, dry_run=False)

    assert out["found"] == 1
    assert out["embedded"] == 1
    get_embedding.assert_called_once_with("alpha node", timeout_s=85.0)


def test_backfill_embeddings_uses_env_override_timeout(monkeypatch):
    class _Conn:
        def execute(self, sql, params=()):
            text = str(sql).strip().upper()
            if text.startswith("SELECT ID, NAME FROM NODES WHERE EMBEDDING IS NULL"):
                assert "LIMIT ?" in text
                assert params == (1000,)
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

    monkeypatch.setenv("QUAID_JANITOR_EMBED_TIMEOUT_SECONDS", "17")
    monkeypatch.delenv("QUAID_JANITOR_EMBED_BACKFILL_LIMIT", raising=False)
    metrics = maintenance_ops.JanitorMetrics()
    graph = _Graph()

    with patch("lib.embeddings.get_embedding", return_value=[0.1, 0.2]) as get_embedding, \
         patch("lib.embeddings.pack_embedding", return_value=b"emb"), \
         patch.object(maintenance_ops, "_upsert_vec_embedding", return_value=None):
        out = maintenance_ops.backfill_embeddings(graph, metrics, dry_run=False)

    assert out["found"] == 1
    assert out["embedded"] == 1
    get_embedding.assert_called_once_with("alpha node", timeout_s=17.0)


def test_backfill_embeddings_applies_env_query_limit(monkeypatch):
    captured = {}

    class _Conn:
        def execute(self, sql, params=()):
            text = str(sql).strip().upper()
            if text.startswith("SELECT ID, NAME FROM NODES WHERE EMBEDDING IS NULL"):
                captured["sql"] = str(sql)
                captured["params"] = params
                return _DummyResult(rows=[])
            if text.startswith("SELECT COUNT(*) FROM NODES_FTS"):
                return _DummyResult(rows=[(0,)])
            if text.startswith("SELECT COUNT(*) FROM NODES"):
                return _DummyResult(rows=[(0,)])
            return _DummyResult(rowcount=1)

    class _Graph:
        @contextmanager
        def _get_conn(self):
            yield _Conn()

    monkeypatch.setenv("QUAID_JANITOR_EMBED_BACKFILL_LIMIT", "17")

    out = maintenance_ops.backfill_embeddings(_Graph(), maintenance_ops.JanitorMetrics(), dry_run=True)

    assert "LIMIT ?" in captured["sql"]
    assert captured["params"] == (17,)
    assert out["found"] == 0


def test_backfill_embeddings_warns_when_query_limit_is_reached(monkeypatch):
    class _Conn:
        def execute(self, sql, params=()):
            text = str(sql).strip().upper()
            if text.startswith("SELECT ID, NAME FROM NODES WHERE EMBEDDING IS NULL"):
                assert params == (2,)
                return _DummyResult(rows=[
                    {"id": "n1", "name": "alpha node"},
                    {"id": "n2", "name": "beta node"},
                ])
            if text.startswith("SELECT COUNT(*) FROM NODES_FTS"):
                return _DummyResult(rows=[(0,)])
            if text.startswith("SELECT COUNT(*) FROM NODES"):
                return _DummyResult(rows=[(2,)])
            return _DummyResult(rowcount=1)

    class _Graph:
        @contextmanager
        def _get_conn(self):
            yield _Conn()

    monkeypatch.setenv("QUAID_JANITOR_EMBED_BACKFILL_LIMIT", "2")
    metrics = maintenance_ops.JanitorMetrics()

    out = maintenance_ops.backfill_embeddings(_Graph(), metrics, dry_run=True)

    assert out["found"] == 2
    assert metrics.summary()["warnings"] == 1
    assert "batch cap (2)" in metrics.warnings[0]["warning"]


def test_backfill_embeddings_invalid_limit_honors_fail_hard(monkeypatch):
    monkeypatch.setenv("QUAID_JANITOR_EMBED_BACKFILL_LIMIT", "not-an-int")

    with patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=False):
        assert maintenance_ops._janitor_embedding_backfill_limit(default_limit=11) == 11

    with patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=True):
        with pytest.raises(RuntimeError, match="Invalid QUAID_JANITOR_EMBED_BACKFILL_LIMIT"):
            maintenance_ops._janitor_embedding_backfill_limit(default_limit=11)


def test_backfill_embeddings_non_positive_limit_honors_fail_hard(monkeypatch):
    monkeypatch.setenv("QUAID_JANITOR_EMBED_BACKFILL_LIMIT", "0")

    with patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=False):
        assert maintenance_ops._janitor_embedding_backfill_limit(default_limit=11) == 11

    with patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=True):
        with pytest.raises(RuntimeError, match="Invalid QUAID_JANITOR_EMBED_BACKFILL_LIMIT"):
            maintenance_ops._janitor_embedding_backfill_limit(default_limit=11)
