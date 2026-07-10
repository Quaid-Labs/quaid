import os
import sys
import json
from contextlib import contextmanager
from datetime import datetime
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


def _monotonic_sequence(*values):
    iterator = iter(values)
    last = values[-1]

    def _next():
        nonlocal last
        try:
            last = next(iterator)
        except StopIteration:
            pass
        return last

    return _next


def test_default_owner_fallback_and_fail_hard():
    with patch.object(maintenance_ops, "_cfg", SimpleNamespace()), \
         patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=False):
        assert maintenance_ops._default_owner_id() == "default"

    with patch.object(maintenance_ops, "_cfg", SimpleNamespace()), \
         patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=True):
        with pytest.raises(RuntimeError, match="default owner"):
            maintenance_ops._default_owner_id()


def test_get_config_value_logs_and_returns_default_when_fail_open(caplog):
    caplog.set_level("WARNING")

    with patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=False):
        value = maintenance_ops._get_config_value(
            lambda: (_ for _ in ()).throw(RuntimeError("config failed")),
            "fallback",
        )

    assert value == "fallback"
    assert "memorydb maintenance config read failed" in caplog.text


def test_get_config_value_raises_when_failhard():
    with patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=True):
        with pytest.raises(RuntimeError, match="config failed"):
            maintenance_ops._get_config_value(
                lambda: (_ for _ in ()).throw(RuntimeError("config failed")),
                "fallback",
            )


def test_effective_llm_timeout_invalid_value_warns_and_uses_default_when_fail_open(caplog):
    with patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=False), \
         caplog.at_level("WARNING", logger=maintenance_ops.__name__):
        assert maintenance_ops._effective_llm_timeout("not-a-number", 42.0) == 42.0

    assert "Invalid memorydb maintenance LLM timeout 'not-a-number'; using default 42.0s" in caplog.text


def test_effective_llm_timeout_invalid_value_raises_when_failhard(caplog):
    with patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=True), \
         caplog.at_level("WARNING", logger=maintenance_ops.__name__):
        with pytest.raises(RuntimeError, match="Invalid memorydb maintenance LLM timeout") as excinfo:
            maintenance_ops._effective_llm_timeout("not-a-number", 42.0)

    assert isinstance(excinfo.value.__cause__, ValueError)
    assert "Invalid memorydb maintenance LLM timeout 'not-a-number'; using default 42.0s" in caplog.text


def test_effective_llm_timeout_honors_positive_requested_value():
    assert maintenance_ops._effective_llm_timeout(300.0, 60.0) == 300.0
    assert maintenance_ops._effective_llm_timeout(3.0, 60.0) == 5.0


def test_janitor_metrics_elapsed_time_uses_monotonic(monkeypatch):
    monotonic_values = iter([100.0, 101.0, 104.5, 108.0])
    monkeypatch.setattr(maintenance_ops.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(
        maintenance_ops.time,
        "time",
        lambda: (_ for _ in ()).throw(AssertionError("wall clock used")),
    )

    metrics = maintenance_ops.JanitorMetrics()
    metrics.start_task("demo")
    metrics.end_task("demo")

    assert metrics.task_duration("demo") == pytest.approx(3.5)
    summary = metrics.summary()
    assert summary["total_duration_seconds"] == pytest.approx(8.0)
    assert summary["task_durations"]["demo"] == pytest.approx(3.5)


def test_janitor_metrics_task_duration_uses_lock():
    class _TrackingLock:
        def __init__(self):
            self.entered = False

        def __enter__(self):
            self.entered = True

        def __exit__(self, exc_type, exc, tb):
            return False

    metrics = maintenance_ops.JanitorMetrics()
    lock = _TrackingLock()
    metrics.task_times["demo"] = {"start": 10.0, "end": 13.5}
    metrics._lock = lock

    assert metrics.task_duration("demo") == pytest.approx(3.5)
    assert lock.entered is True


def test_maintenance_diagnostic_fallbacks_log(monkeypatch, caplog):
    class _BrokenUsers:
        identities = {}

        @property
        def default_owner(self):
            raise RuntimeError("owner cfg failed")

    monkeypatch.delenv("QUAID_JANITOR_EMBED_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("OLLAMA_EMBED_TIMEOUT_S", "not-a-number")
    monkeypatch.setenv("QUAID_JANITOR_DEBUG_DIAGNOSTICS", "1")

    with caplog.at_level("DEBUG", logger=maintenance_ops.__name__):
        assert maintenance_ops._janitor_embedding_timeout_seconds() == 120.0

        with patch.object(maintenance_ops, "_cfg", SimpleNamespace(users=_BrokenUsers())), \
             patch.object(maintenance_ops, "resolve_owner_person", return_value=SimpleNamespace(name="")):
            assert maintenance_ops._owner_display_name() == "the user"
            assert maintenance_ops._owner_full_name("solomon-steadman") == "the user"

        assert maintenance_ops._load_node_attributes_blob("{bad json") == {}

        with patch.object(maintenance_ops.logger, "info", side_effect=RuntimeError("diag sink failed")):
            maintenance_ops._diag_log_decision("unit", payload="value")

        parallel_cfg = SimpleNamespace(
            enabled=True,
            llm_workers=3,
            task_workers={"unit_task": "not-an-int"},
        )
        cfg = SimpleNamespace(core=SimpleNamespace(parallel=parallel_cfg))
        with patch.object(maintenance_ops, "_cfg", cfg):
            assert maintenance_ops._llm_parallel_workers("unit_task") == 3

    assert "Invalid OLLAMA_EMBED_TIMEOUT_S" in caplog.text
    assert "Failed resolving owner display name" in caplog.text
    assert "Failed loading configured owner ids" in caplog.text
    assert "Failed parsing node attributes blob" in caplog.text
    assert "_diag_log_decision failed" in caplog.text
    assert "Failed parsing LLM parallel workers config" in caplog.text


def test_owner_full_name_fallback_accepts_unicode_slug(monkeypatch):
    cfg = SimpleNamespace(
        owner_name="",
        users=SimpleNamespace(
            default_owner="émile-zola",
            identities={"émile-zola": SimpleNamespace(person_node_name="", speakers=[])},
        ),
    )

    monkeypatch.setattr(maintenance_ops, "_cfg", cfg)
    monkeypatch.setattr(maintenance_ops, "resolve_owner_person", lambda _owner: None)

    assert maintenance_ops._owner_full_name("émile-zola") == "Émile Zola"


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


def test_contradiction_elapsed_budget_uses_monotonic(monkeypatch):
    monotonic_values = iter([100.0, 101.0, 105.0, 106.0, 107.0, 108.0])
    monkeypatch.setattr(maintenance_ops.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(
        maintenance_ops.time,
        "time",
        lambda: (_ for _ in ()).throw(AssertionError("wall clock used")),
    )
    monkeypatch.setattr(maintenance_ops, "CONTRADICTION_ENABLED", True)
    monkeypatch.setattr(maintenance_ops, "MAX_EXECUTION_TIME", 20.0)
    captured = {}

    def fake_run_batches(batches, task_name, runner, overall_timeout_seconds=None):
        captured["timeout"] = overall_timeout_seconds
        return [{"batch_num": 1, "batch": batches[0], "results": [None], "duration": 0.0}]

    monkeypatch.setattr(maintenance_ops, "_run_llm_batches_parallel", fake_run_batches)
    metrics = maintenance_ops.JanitorMetrics()

    out = maintenance_ops.find_contradictions_from_pairs(
        [
            {
                "id_a": "a",
                "id_b": "b",
                "text_a": "Ari lives in Porto.",
                "text_b": "Ari lives in Lisbon.",
                "similarity": 0.91,
            }
        ],
        metrics,
        dry_run=True,
    )

    assert out == []
    assert captured["timeout"] == pytest.approx(19.0)
def test_recall_similar_pairs_routes_negated_subset_overlap_to_review(monkeypatch):
    new_node = SimpleNamespace(
        id="new",
        name="Alex keeps blue notebooks near the desk",
        embedding=[1.0, 0.0],
        type="Fact",
        created_at="2024-02-01T00:00:00",
        valid_from=None,
        valid_until=None,
        occurred_start=None,
        occurred_end=None,
        mentioned_at="2024-02-02T00:00:00",
    )
    old_node = SimpleNamespace(
        id="old",
        name="Alex does not keep blue notebooks near the desk",
        embedding=[1.0, 0.0],
        type="Fact",
        created_at="2024-01-01T00:00:00",
        valid_from=None,
        valid_until=None,
        occurred_start=None,
        occurred_end=None,
        mentioned_at="2024-01-02T00:00:00",
    )

    class _Graph:
        def cosine_similarity(self, emb_a, emb_b):
            return float(maintenance_ops.DUPLICATE_MIN_SIM) - 0.12

    monkeypatch.setattr(maintenance_ops, "get_nodes_since", lambda graph, since=None, limit=0: [new_node])
    monkeypatch.setattr(maintenance_ops, "recall_candidates", lambda graph, text, node_id: [old_node])

    out = maintenance_ops.recall_similar_pairs(_Graph(), maintenance_ops.JanitorMetrics())

    assert len(out["duplicates"]) == 1
    dup = out["duplicates"][0]
    assert dup["id_a"] == "new"
    assert dup["id_b"] == "old"
    assert dup["dedup_reason"] == "subset_overlap"


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


def test_batch_duplicate_check_skips_merge_without_merged_text(monkeypatch, caplog):
    def fake_call_fast_reasoning(_prompt, **_kwargs):
        return ('[{"pair": 1, "action": "merge"}]', 0.01)

    monkeypatch.setattr(maintenance_ops, "call_fast_reasoning", fake_call_fast_reasoning)
    metrics = maintenance_ops.JanitorMetrics()
    pairs = [{
        "text_a": "Alex keeps a repair log",
        "text_b": "Alex keeps a repair log",
        "similarity": 0.99,
    }]

    with caplog.at_level("WARNING", logger=maintenance_ops.__name__):
        result = maintenance_ops.batch_duplicate_check(pairs, metrics)

    assert result == [None]
    assert "merge without merged_text" in caplog.text
    assert any("merge without merged_text" in item["warning"] for item in metrics.warnings)


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


def test_review_fix_embeds_outside_db_connection():
    class _Conn:
        def execute(self, sql, params=()):
            text = str(sql).strip().upper()
            if text.startswith("SELECT NAME, STATUS, SOURCE, SPEAKER, ATTRIBUTES FROM NODES"):
                return _DummyResult(rows=[{
                    "name": "old fact",
                    "status": "pending",
                    "source": "unit",
                    "speaker": "user",
                    "attributes": "{}",
                }])
            if text.startswith("DELETE FROM EDGES"):
                return _DummyResult(rowcount=0)
            if text.startswith("UPDATE NODES"):
                return _DummyResult(rowcount=1)
            raise AssertionError(f"unexpected SQL: {sql}")

    class _Graph:
        def __init__(self):
            self.active_connections = 0

        @contextmanager
        def _get_conn(self):
            self.active_connections += 1
            try:
                yield _Conn()
            finally:
                self.active_connections -= 1

    graph = _Graph()

    def _get_embedding(_text):
        assert graph.active_connections == 0
        return [0.1]

    decisions = [{"id": "n1", "action": "FIX", "new_text": "updated text", "edges": []}]

    with patch("lib.embeddings.get_embedding", side_effect=_get_embedding), \
         patch("lib.embeddings.pack_embedding", return_value=b"x"), \
         patch.object(maintenance_ops, "_upsert_vec_embedding", return_value=None):
        out = maintenance_ops.apply_review_decisions_from_list(graph, decisions, dry_run=False)

    assert out["fixed"] == 1


@pytest.mark.parametrize(
    ("action", "expected_resolution", "superseding_id", "superseded_id"),
    [
        ("KEEP_A", "keep_a", "na", "nb"),
        ("KEEP_B", "keep_b", "nb", "na"),
    ],
)
def test_contradiction_resolution_uses_runtime_clock_for_timestamps(
    monkeypatch,
    action,
    expected_resolution,
    superseding_id,
    superseded_id,
):
    monkeypatch.setattr(maintenance_ops, "CONTRADICTION_ENABLED", True)
    monkeypatch.setenv("QUAID_NOW", "2026-07-08T09:10:11")
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
            self.params.append(tuple(params or ()))
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
        "response_duration": (json.dumps([{"pair": 1, "action": action, "reason": "latest"}]), 0.0),
    }]

    with patch.object(maintenance_ops, "get_pending_contradictions", return_value=pending), \
         patch.object(maintenance_ops, "_run_llm_batches_parallel", return_value=llm_batches), \
         patch.object(maintenance_ops, "resolve_contradiction", side_effect=AssertionError("legacy path called")):
        out = maintenance_ops.resolve_contradictions_with_opus(graph, metrics, dry_run=False, max_items=1)

    assert out["resolved"] == 1
    assert len(graph.calls) >= 2  # count query + apply transaction
    apply_call = graph.calls[-1]
    apply_sql = "\n".join(apply_call.sql)
    assert "UPDATE nodes SET superseded_by" in apply_sql
    assert "UPDATE contradictions" in apply_sql
    assert "datetime('now')" not in apply_sql
    assert apply_call.params[0] == (
        superseding_id,
        "2026-07-08T09:10:11",
        "2026-07-08T09:10:11",
        superseded_id,
    )
    assert apply_call.params[1] == (
        expected_resolution,
        "latest",
        "2026-07-08T09:10:11",
        "c1",
    )


def test_contradiction_resolution_summary_failure_logs(monkeypatch, caplog):
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
        def execute(self, sql, params=()):
            if "SELECT COUNT(*) FROM contradictions" in sql:
                return _DummyResult(rows=[(1,)])
            return _DummyResult(rowcount=1)

    class _Graph:
        @contextmanager
        def _get_conn(self):
            yield _Conn()

        def add_node(self, *_args, **_kwargs):
            raise RuntimeError("summary write failed")

        def add_edge(self, *_args, **_kwargs):
            raise AssertionError("add_edge should not run after summary add failure")

    llm_batches = [{
        "batch_num": 1,
        "batch": pending,
        "prompt_tag": "",
        "response_duration": (json.dumps([{
            "pair": 1,
            "action": "MERGE",
            "merged_text": "merged fact text",
            "reason": "latest",
        }]), 0.0),
    }]

    with patch.object(maintenance_ops, "get_pending_contradictions", return_value=pending), \
         patch.object(maintenance_ops, "_run_llm_batches_parallel", return_value=llm_batches), \
         patch.object(maintenance_ops, "resolve_contradiction", return_value=None), \
         patch.object(maintenance_ops, "_merge_nodes_into", return_value={"id": "merged"}), \
         patch.object(maintenance_ops, "_default_owner_id", return_value="default"), \
         caplog.at_level("WARNING", logger=maintenance_ops.__name__):
        out = maintenance_ops.resolve_contradictions_with_opus(
            _Graph(),
            metrics,
            dry_run=False,
            max_items=1,
        )

    assert out["merged"] == 1
    assert "Failed creating contradiction resolution summary node: summary write failed" in caplog.text


def test_contradiction_merge_failure_leaves_contradiction_pending(monkeypatch):
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
    llm_batches = [{
        "batch_num": 1,
        "batch": pending,
        "prompt_tag": "",
        "response_duration": (json.dumps([{
            "pair": 1,
            "action": "MERGE",
            "merged_text": "merged fact text",
            "reason": "latest",
        }]), 0.0),
    }]
    class _Conn:
        def execute(self, sql, params=()):
            if "SELECT COUNT(*) FROM contradictions" in sql:
                return _DummyResult(rows=[(1,)])
            return _DummyResult(rowcount=1)

    class _Graph:
        @contextmanager
        def _get_conn(self):
            yield _Conn()

    with patch.object(maintenance_ops, "get_pending_contradictions", return_value=pending), \
         patch.object(maintenance_ops, "_run_llm_batches_parallel", return_value=llm_batches), \
         patch.object(maintenance_ops, "_merge_nodes_into", side_effect=RuntimeError("merge failed")), \
         patch.object(maintenance_ops, "resolve_contradiction", side_effect=AssertionError("resolved too early")):
        with pytest.raises(RuntimeError, match="merge failed"):
            maintenance_ops.resolve_contradictions_with_opus(_Graph(), metrics, dry_run=False, max_items=1)


def test_contradiction_failed_llm_batch_records_elapsed_time(monkeypatch):
    monkeypatch.setattr(maintenance_ops, "CONTRADICTION_ENABLED", True)
    metrics = maintenance_ops.JanitorMetrics()
    monkeypatch.setattr(maintenance_ops.time, "monotonic", _monotonic_sequence(10.0, 20.0, 23.5, 30.0))
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
        def execute(self, sql, params=()):
            if "SELECT COUNT(*) FROM contradictions" in sql:
                return _DummyResult(rows=[(1,)])
            raise AssertionError(f"unexpected SQL: {sql}")

    class _Graph:
        @contextmanager
        def _get_conn(self):
            yield _Conn()

    with patch.object(maintenance_ops, "get_pending_contradictions", return_value=pending), \
         patch.object(maintenance_ops, "_llm_parallel_workers", return_value=1), \
         patch.object(maintenance_ops, "call_deep_reasoning", side_effect=TimeoutError("llm timeout")), \
         patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=False):
        out = maintenance_ops.resolve_contradictions_with_opus(
            _Graph(),
            metrics,
            dry_run=False,
            max_items=1,
        )

    assert out["resolved"] == 0
    assert out["merged"] == 0
    assert metrics.llm_calls == 1
    assert metrics.llm_time == pytest.approx(3.5)
    assert metrics.task_meta["contradiction_resolution"]["llm_time_seconds"] == pytest.approx(3.5)
    assert any("Contradiction resolution batch 1 failed" in item["error"] for item in metrics.errors)


def test_contradiction_failed_llm_batch_raises_when_failhard(monkeypatch):
    monkeypatch.setattr(maintenance_ops, "CONTRADICTION_ENABLED", True)
    metrics = maintenance_ops.JanitorMetrics()
    monkeypatch.setattr(maintenance_ops.time, "monotonic", _monotonic_sequence(10.0, 20.0))
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
        def execute(self, sql, params=()):
            if "SELECT COUNT(*) FROM contradictions" in sql:
                return _DummyResult(rows=[(1,)])
            raise AssertionError(f"unexpected SQL: {sql}")

    class _Graph:
        @contextmanager
        def _get_conn(self):
            yield _Conn()

    with patch.object(maintenance_ops, "get_pending_contradictions", return_value=pending), \
         patch.object(maintenance_ops, "_llm_parallel_workers", return_value=1), \
         patch.object(maintenance_ops, "call_deep_reasoning", side_effect=TimeoutError("llm timeout")), \
         patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=True):
        with pytest.raises(TimeoutError, match="llm timeout"):
            maintenance_ops.resolve_contradictions_with_opus(
                _Graph(),
                metrics,
                dry_run=False,
                max_items=1,
            )


def test_quaid_now_rejects_malformed_override_when_failhard_enabled(monkeypatch):
    monkeypatch.setenv("QUAID_NOW", "not-a-date")

    with patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=True):
        with pytest.raises(RuntimeError, match="Invalid QUAID_NOW"):
            maintenance_ops._quaid_now()


def test_quaid_now_normalizes_offset_override_to_naive_local_storage_time(monkeypatch):
    monkeypatch.setenv("QUAID_NOW", "2026-03-11T23:30:00-02:00")

    now = maintenance_ops._quaid_now()

    assert now.tzinfo is None
    assert now.isoformat() == "2026-03-11T23:30:00"


def test_update_check_cache_uses_quaid_now_for_freshness(monkeypatch):
    class _Conn:
        def __init__(self, updated_at):
            self.updated_at = updated_at

        def execute(self, sql, params=()):
            return _DummyResult(rows=[{
                "value": json.dumps({"version": "1.2.3"}),
                "updated_at": self.updated_at,
            }])

    class _Graph:
        def __init__(self, updated_at):
            self.updated_at = updated_at

        @contextmanager
        def _get_conn(self):
            yield _Conn(self.updated_at)

    graph = _Graph("2026-03-10T23:00:00")
    monkeypatch.setenv("QUAID_NOW", "2026-03-11T00:00:00Z")
    assert maintenance_ops.get_update_check_cache(graph, max_age_hours=24) == {"version": "1.2.3"}

    monkeypatch.setenv("QUAID_NOW", "2026-03-12T00:00:01Z")
    assert maintenance_ops.get_update_check_cache(graph, max_age_hours=24) is None


def test_write_update_check_cache_uses_quaid_now(monkeypatch):
    monkeypatch.setenv("QUAID_NOW", "2026-03-11T00:00:00Z")
    captured = {}

    class _Conn:
        def execute(self, sql, params=()):
            captured["sql"] = str(sql)
            captured["params"] = params
            return _DummyResult(rowcount=1)

    class _Graph:
        @contextmanager
        def _get_conn(self):
            yield _Conn()

    maintenance_ops.write_update_check_cache(_Graph(), {"version": "1.2.3"})

    assert "datetime('now')" not in captured["sql"]
    assert captured["params"] == ("update_check", '{"version": "1.2.3"}', "2026-03-11T00:00:00")


def test_review_fix_uses_quaid_now_for_updated_at(monkeypatch):
    monkeypatch.setenv("QUAID_NOW", "2026-03-11T00:00:00Z")
    captured = {}

    class _Conn:
        def execute(self, sql, params=()):
            text = str(sql)
            if "SELECT name, status, source, speaker, attributes FROM nodes" in text:
                return _DummyResult(rows=[{
                    "name": "old text",
                    "status": "pending",
                    "source": "unit-test",
                    "speaker": "user",
                    "attributes": "{}",
                }])
            if "UPDATE nodes SET name = ?, embedding = ?, content_hash = ?, updated_at = ?" in text:
                captured["params"] = params
            return _DummyResult(rowcount=1)

    class _Graph:
        @contextmanager
        def _get_conn(self):
            yield _Conn()

    with patch("lib.embeddings.get_embedding", return_value=[0.1]), \
         patch("lib.embeddings.pack_embedding", return_value=b"x"), \
         patch.object(maintenance_ops, "_upsert_vec_embedding", return_value=None):
        out = maintenance_ops.apply_review_decisions_from_list(
            _Graph(),
            [{"id": "n1", "action": "FIX", "new_text": "updated text", "edges": []}],
            dry_run=False,
        )

    assert out["fixed"] == 1
    assert captured["params"][3] == "2026-03-11T00:00:00"


def test_review_decision_merge_failure_logs(caplog):
    decisions = [{
        "action": "MERGE",
        "merge_ids": ["n1", "n2"],
        "merged_text": "merged fact text",
        "reason": "duplicate",
    }]

    with patch.object(maintenance_ops, "_merge_nodes_into", side_effect=ValueError("merge failed")), \
         caplog.at_level("WARNING", logger=maintenance_ops.__name__):
        out = maintenance_ops.apply_review_decisions_from_list(object(), decisions, dry_run=False)

    assert out["merged"] == 0
    assert "MERGE failed for ['n1', 'n2']: merge failed" in caplog.text


def test_review_decision_malformed_attributes_logs(caplog):
    class _Conn:
        def execute(self, sql, params=()):
            text = str(sql).strip().upper()
            if text.startswith("SELECT NAME, STATUS, SOURCE, SPEAKER, ATTRIBUTES FROM NODES"):
                return _DummyResult(rows=[{
                    "name": "old fact",
                    "status": "pending",
                    "source": "unit",
                    "speaker": "user",
                    "attributes": "{bad json",
                }])
            if text.startswith("UPDATE NODES SET STATUS = 'APPROVED'"):
                return _DummyResult(rowcount=1)
            raise AssertionError(f"unexpected SQL: {sql}")

    class _Graph:
        @contextmanager
        def _get_conn(self):
            yield _Conn()

    decisions = [{"id": "n1", "action": "KEEP", "reason": "valid"}]

    with caplog.at_level("WARNING", logger=maintenance_ops.__name__):
        out = maintenance_ops.apply_review_decisions_from_list(_Graph(), decisions, dry_run=False)

    assert out["kept"] == 1
    assert "Failed parsing node attributes for review decision on n1" in caplog.text


@pytest.mark.parametrize(
    ("fail_hard", "raises", "node_deleted"),
    [
        (False, False, True),
        (True, True, False),
    ],
)
def test_review_delete_vec_node_failure_logs_and_respects_failhard(
    fail_hard,
    raises,
    node_deleted,
    caplog,
):
    class _Conn:
        def __init__(self):
            self.node_deleted = False

        def execute(self, sql, params=()):
            text = str(sql).strip().upper()
            if text.startswith("SELECT NAME, STATUS, SOURCE, SPEAKER, ATTRIBUTES FROM NODES"):
                return _DummyResult(rows=[{
                    "name": "old fact",
                    "status": "pending",
                    "source": "unit",
                    "speaker": "user",
                    "attributes": "{}",
                }])
            if text.startswith("DELETE FROM VEC_NODES"):
                raise RuntimeError("vec delete failed")
            if text.startswith("DELETE FROM NODES"):
                self.node_deleted = True
            return _DummyResult(rowcount=1)

    class _Graph:
        def __init__(self):
            self.conn = _Conn()

        @contextmanager
        def _get_conn(self):
            yield self.conn

    graph = _Graph()
    decisions = [{"id": "n1", "action": "DELETE", "reason": "obsolete"}]

    with patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=fail_hard), \
         caplog.at_level("WARNING", logger=maintenance_ops.__name__):
        if raises:
            with pytest.raises(RuntimeError, match="Failed deleting vec_node for n1") as excinfo:
                maintenance_ops.apply_review_decisions_from_list(graph, decisions, dry_run=False)
            assert isinstance(excinfo.value.__cause__, RuntimeError)
        else:
            out = maintenance_ops.apply_review_decisions_from_list(graph, decisions, dry_run=False)
            assert out["deleted"] == 1

    assert graph.conn.node_deleted is node_deleted
    assert "Failed deleting vec_node for n1: vec delete failed" in caplog.text


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


def test_review_dedup_failed_llm_batch_records_elapsed_time(monkeypatch):
    metrics = maintenance_ops.JanitorMetrics()
    monkeypatch.setattr(maintenance_ops.time, "monotonic", _monotonic_sequence(10.0, 40.0, 44.25, 50.0))
    pending = [{
        "id": "d1",
        "new_text": "Alex keeps a repair log",
        "existing_text": "Alex maintains a repair log",
        "similarity": 0.92,
        "decision": "llm_duplicate",
        "llm_reasoning": "same fact",
        "owner_id": "default",
        "source": "unit",
    }]

    class _Conn:
        def execute(self, sql, params=()):
            text = str(sql).strip().upper()
            if text.startswith("UPDATE DEDUP_LOG"):
                return _DummyResult(rowcount=0)
            if text.startswith("SELECT COUNT(*) FROM DEDUP_LOG"):
                return _DummyResult(rows=[(1,)])
            raise AssertionError(f"unexpected SQL: {sql}")

    class _Graph:
        @contextmanager
        def _get_conn(self):
            yield _Conn()

    with patch.object(maintenance_ops, "get_recent_dedup_rejections", return_value=pending), \
         patch.object(maintenance_ops, "_llm_parallel_workers", return_value=1), \
         patch.object(maintenance_ops, "call_deep_reasoning", side_effect=TimeoutError("llm timeout")), \
         patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=False):
        out = maintenance_ops.review_dedup_rejections(_Graph(), metrics, dry_run=False, max_items=1)

    assert out["reviewed"] == 0
    assert out["confirmed"] == 0
    assert out["reversed"] == 0
    assert metrics.llm_calls == 1
    assert metrics.llm_time == pytest.approx(4.25)
    assert metrics.task_meta["dedup_review"]["llm_time_seconds"] == pytest.approx(4.25)
    assert any("Dedup review batch 1 failed" in item["error"] for item in metrics.errors)


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


def test_review_decayed_failed_llm_batch_records_elapsed_time(monkeypatch):
    metrics = maintenance_ops.JanitorMetrics()
    monkeypatch.setattr(maintenance_ops.time, "monotonic", _monotonic_sequence(10.0, 70.0, 75.75, 80.0))
    pending = [{
        "id": "q1",
        "node_id": "n1",
        "node_text": "Alex keeps old travel notes",
        "node_type": "Fact",
        "confidence_at_queue": 0.2,
        "access_count": 0,
        "last_accessed": "2026-01-01T00:00:00",
        "created_at_node": "2025-01-01T00:00:00",
        "verified": 0,
    }]

    class _Conn:
        def execute(self, sql, params=()):
            if "SELECT COUNT(*) FROM decay_review_queue" in sql:
                return _DummyResult(rows=[(1,)])
            raise AssertionError(f"unexpected SQL: {sql}")

    class _Graph:
        @contextmanager
        def _get_conn(self):
            yield _Conn()

    with patch.object(maintenance_ops, "get_pending_decay_reviews", return_value=pending), \
         patch.object(maintenance_ops, "_llm_parallel_workers", return_value=1), \
         patch.object(maintenance_ops, "call_deep_reasoning", side_effect=TimeoutError("llm timeout")), \
         patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=False):
        out = maintenance_ops.review_decayed_memories(_Graph(), metrics, dry_run=False, max_items=1)

    assert out["reviewed"] == 0
    assert out["deleted"] == 0
    assert out["extended"] == 0
    assert out["pinned"] == 0
    assert metrics.llm_calls == 1
    assert metrics.llm_time == pytest.approx(5.75)
    assert metrics.task_meta["decay_review"]["llm_time_seconds"] == pytest.approx(5.75)
    assert any("Decay review batch 1 failed" in item["error"] for item in metrics.errors)


def test_find_stale_memories_includes_old_never_accessed_nodes(monkeypatch):
    monkeypatch.setenv("QUAID_NOW", "2026-07-08T09:10:11")
    monkeypatch.setattr(maintenance_ops, "CONFIDENCE_DECAY_DAYS", 30)
    metrics = maintenance_ops.JanitorMetrics()

    class _Conn:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params=()):
            self.calls.append((str(sql), tuple(params or ())))
            return _DummyResult(rows=[{"id": "n1"}])

    class _Graph:
        def __init__(self):
            self.conn = _Conn()

        @contextmanager
        def _get_conn(self):
            yield self.conn

        def _row_to_node(self, _row):
            return SimpleNamespace(
                id="n1",
                name="old never-accessed fact",
                type="fact",
                confidence=0.5,
                accessed_at=None,
                access_count=0,
                storage_strength=0.0,
                extraction_confidence=0.8,
                verified=False,
                owner_id="operator",
                created_at="2026-05-01T00:00:00",
                speaker=None,
            )

    graph = _Graph()

    stale = maintenance_ops.find_stale_memories_optimized(graph, metrics)

    sql, params = graph.conn.calls[0]
    assert "accessed_at IS NULL" in sql
    assert "COALESCE(created_at, '') < ?" in sql
    assert "ORDER BY COALESCE(accessed_at, created_at, '') ASC" in sql
    assert params == ("2026-06-08T09:10:11", "2026-06-08T09:10:11")
    assert stale[0]["id"] == "n1"
    assert stale[0]["last_accessed"] is None


@pytest.mark.parametrize(
    ("action", "expected_key"),
    [
        ("EXTEND", "extended"),
        ("PIN", "pinned"),
    ],
)
def test_review_decayed_memories_logs_malformed_attributes(monkeypatch, caplog, action, expected_key):
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
        def execute(self, sql, params=()):
            if "SELECT COUNT(*) FROM decay_review_queue" in sql:
                return _DummyResult(rows=[(1,)])
            if "SELECT attributes FROM nodes" in sql:
                return _DummyResult(rows=[{"attributes": "{bad json"}])
            return _DummyResult(rowcount=1)

    class _Graph:
        @contextmanager
        def _get_conn(self):
            yield _Conn()

    llm_batches = [{
        "batch_num": 1,
        "batch": pending,
        "prompt_tag": "",
        "response_duration": (
            json.dumps([{"item": 1, "action": action, "reason": "still useful"}]),
            0.0,
        ),
    }]

    with patch.object(maintenance_ops, "get_pending_decay_reviews", return_value=pending), \
         patch.object(maintenance_ops, "_run_llm_batches_parallel", return_value=llm_batches), \
         caplog.at_level("WARNING", logger=maintenance_ops.__name__):
        out = maintenance_ops.review_decayed_memories(_Graph(), metrics, dry_run=False, max_items=1)

    assert out[expected_key] == 1
    assert "Skipping malformed decay review attributes for node n1" in caplog.text


@pytest.mark.parametrize(
    ("attrs", "default", "expected"),
    [
        ({"extraction_confidence": 0.0}, 0.3, 0.0),
        ({"extraction_confidence": "0"}, 0.7, 0.0),
        ({}, 0.3, 0.3),
        ({"extraction_confidence": None}, 0.7, 0.7),
    ],
)
def test_decay_review_extraction_confidence_preserves_explicit_zero(attrs, default, expected):
    assert maintenance_ops._decay_review_extraction_confidence(attrs, default) == expected


def test_get_completed_review_work_today_uses_quaid_now(monkeypatch):
    monkeypatch.setenv("QUAID_NOW", "2026-03-11T15:30:00Z")
    captured = {}

    class _Conn:
        def execute(self, sql, params=()):
            captured["params"] = params
            return _DummyResult(rows=[(7,)])

    @contextmanager
    def _fake_conn(_db_path):
        yield _Conn()

    with patch("lib.database.get_connection", _fake_conn):
        out = maintenance_ops.get_completed_review_work_today()

    assert out["reviewed"] == 7
    assert captured["params"] == ("2026-03-11T00:00:00",)


def test_resolve_temporal_references_does_not_rewrite_with_quaid_now(monkeypatch):
    monkeypatch.setenv("QUAID_NOW", "2026-03-11T00:00:00Z")

    class _Graph:
        @contextmanager
        def _get_conn(self):
            raise AssertionError("temporal maintenance should not scan or rewrite nodes")
            yield

    out = maintenance_ops.resolve_temporal_references(_Graph(), dry_run=False)

    assert out == {"found": 0, "fixed": 0, "skipped": 0}


def test_review_dedup_rejections_uses_runtime_clock_for_sql_paths(monkeypatch):
    monkeypatch.setenv("QUAID_NOW", "2026-07-08T09:10:11")
    metrics = maintenance_ops.JanitorMetrics()

    class _Conn:
        def __init__(self):
            self.sql = []
            self.params = []

        def execute(self, sql, params=()):
            self.sql.append(str(sql))
            self.params.append(tuple(params or ()))
            text = str(sql).strip().upper()
            if text.startswith("UPDATE DEDUP_LOG"):
                return _DummyResult(rowcount=2)
            if text.startswith("SELECT COUNT(*) FROM DEDUP_LOG"):
                return _DummyResult(rows=[(0,)])
            raise AssertionError(f"unexpected SQL: {sql}")

    class _Graph:
        def __init__(self):
            self.calls = []

        @contextmanager
        def _get_conn(self):
            conn = _Conn()
            self.calls.append(conn)
            yield conn

    graph = _Graph()

    with patch.object(maintenance_ops, "get_recent_dedup_rejections", return_value=[]):
        out = maintenance_ops.review_dedup_rejections(graph, metrics, dry_run=False, max_items=1)

    assert out["confirmed"] == 2
    assert out["reviewed"] == 2
    all_sql = "\n".join(sql for call in graph.calls for sql in call.sql)
    assert "datetime('now')" not in all_sql
    assert graph.calls[0].params[0] == ("2026-07-08T09:10:11",)
    assert graph.calls[1].params[0] == ("2026-07-07T09:10:11",)


@pytest.mark.parametrize(
    ("action", "expected_queue_params"),
    [
        ("EXTEND", ("extend", "still useful", "2026-07-08T09:10:11", "dq1")),
        ("PIN", ("pin", "still useful", "2026-07-08T09:10:11", "dq1")),
    ],
)
def test_review_decayed_memories_uses_runtime_clock_for_inline_queue_updates(
    monkeypatch,
    action,
    expected_queue_params,
):
    monkeypatch.setenv("QUAID_NOW", "2026-07-08T09:10:11")
    metrics = maintenance_ops.JanitorMetrics()
    pending = [{
        "id": "dq1",
        "node_id": "n1",
        "node_text": "durable personal fact",
        "node_type": "fact",
        "confidence_at_queue": 0.1,
        "access_count": 2,
        "last_accessed": "2026-06-01T00:00:00",
        "created_at_node": "2026-01-01T00:00:00",
        "verified": False,
    }]

    class _Conn:
        def __init__(self):
            self.sql = []
            self.params = []

        def execute(self, sql, params=()):
            self.sql.append(str(sql))
            self.params.append(tuple(params or ()))
            text = str(sql).strip().upper()
            if text.startswith("SELECT COUNT(*) FROM DECAY_REVIEW_QUEUE"):
                return _DummyResult(rows=[(1,)])
            if text.startswith("SELECT ATTRIBUTES FROM NODES"):
                return _DummyResult(rows=[{"attributes": json.dumps({"extraction_confidence": 0.8})}])
            if text.startswith("UPDATE NODES"):
                return _DummyResult(rowcount=1)
            if text.startswith("UPDATE DECAY_REVIEW_QUEUE"):
                return _DummyResult(rowcount=1)
            raise AssertionError(f"unexpected SQL: {sql}")

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
        "response_duration": (json.dumps([{"item": 1, "action": action, "reason": "still useful"}]), 0.0),
    }]

    with patch.object(maintenance_ops, "get_pending_decay_reviews", return_value=pending), \
         patch.object(maintenance_ops, "_run_llm_batches_parallel", return_value=llm_batches):
        out = maintenance_ops.review_decayed_memories(graph, metrics, dry_run=False, max_items=1)

    assert out["reviewed"] == 1
    all_sql = "\n".join(sql for call in graph.calls for sql in call.sql)
    assert "datetime('now')" not in all_sql
    apply_call = graph.calls[-1]
    assert apply_call.params[-1] == expected_queue_params


def test_update_check_cache_uses_runtime_clock_for_freshness_and_writes(monkeypatch):
    monkeypatch.setenv("QUAID_NOW", "2030-01-02T00:00:00")

    class _Conn:
        def __init__(self):
            self.sql = []
            self.params = []

        def execute(self, sql, params=()):
            self.sql.append(str(sql))
            self.params.append(tuple(params or ()))
            text = str(sql).strip().upper()
            if text.startswith("SELECT VALUE, UPDATED_AT FROM JANITOR_METADATA"):
                return _DummyResult(rows=[{
                    "value": json.dumps({"version": "1.2.3"}),
                    "updated_at": "2030-01-01T00:00:00",
                }])
            if text.startswith("INSERT OR REPLACE INTO JANITOR_METADATA"):
                return _DummyResult(rowcount=1)
            raise AssertionError(f"unexpected SQL: {sql}")

    class _Graph:
        def __init__(self):
            self.calls = []

        @contextmanager
        def _get_conn(self):
            conn = _Conn()
            self.calls.append(conn)
            yield conn

    graph = _Graph()

    assert maintenance_ops.get_update_check_cache(graph, max_age_hours=12) is None
    maintenance_ops.write_update_check_cache(graph, {"version": "2.0.0"})

    all_sql = "\n".join(sql for call in graph.calls for sql in call.sql)
    assert "datetime('now')" not in all_sql
    assert graph.calls[-1].params[-1] == (
        "update_check",
        json.dumps({"version": "2.0.0"}),
        "2030-01-02T00:00:00",
    )


def test_review_fix_decision_uses_runtime_clock_for_updated_at(monkeypatch):
    monkeypatch.setenv("QUAID_NOW", "2026-07-08T09:10:11")

    class _Conn:
        def __init__(self):
            self.sql = []
            self.params = []

        def execute(self, sql, params=()):
            self.sql.append(str(sql))
            self.params.append(tuple(params or ()))
            text = str(sql).strip().upper()
            if text.startswith("SELECT NAME, STATUS"):
                return _DummyResult(rows=[{
                    "name": "old fact",
                    "status": "pending",
                    "source": "unit",
                    "speaker": "user",
                    "attributes": "{}",
                }])
            if text.startswith("DELETE FROM EDGES"):
                return _DummyResult(rowcount=0)
            if text.startswith("UPDATE NODES"):
                return _DummyResult(rowcount=1)
            raise AssertionError(f"unexpected SQL: {sql}")

    class _Graph:
        def __init__(self):
            self.calls = []

        @contextmanager
        def _get_conn(self):
            conn = _Conn()
            self.calls.append(conn)
            yield conn

    graph = _Graph()
    decisions = [{"id": "n1", "action": "FIX", "new_text": "updated fact", "edges": []}]

    with patch("lib.embeddings.get_embedding", return_value=[0.1]), \
         patch("lib.embeddings.pack_embedding", return_value=None):
        out = maintenance_ops.apply_review_decisions_from_list(graph, decisions, dry_run=False)

    assert out["fixed"] == 1
    all_sql = "\n".join(sql for call in graph.calls for sql in call.sql)
    assert "datetime('now')" not in all_sql
    update_params = [
        params for call in graph.calls for sql, params in zip(call.sql, call.params)
        if str(sql).strip().upper().startswith("UPDATE NODES")
    ][0]
    assert update_params[3] == "2026-07-08T09:10:11"


def test_resolve_temporal_references_skips_storage_when_llm_review_owns_dates(monkeypatch):
    monkeypatch.setenv("QUAID_NOW", "2026-07-08T09:10:11")
    metrics = maintenance_ops.JanitorMetrics()

    class _Graph:
        @contextmanager
        def _get_conn(self):
            raise AssertionError("temporal maintenance should not scan or rewrite nodes")
            yield

    out = maintenance_ops.resolve_temporal_references(_Graph(), dry_run=False, metrics=metrics)

    assert out == {"found": 0, "fixed": 0, "skipped": 0}
    assert metrics.task_duration("temporal_resolution") >= 0.0


def test_completed_review_work_today_uses_runtime_clock_for_midnight(monkeypatch):
    monkeypatch.setenv("QUAID_NOW", "2030-01-02T12:34:56")

    class _Conn:
        def __init__(self):
            self.sql = []
            self.params = []

        def execute(self, sql, params=()):
            self.sql.append(str(sql))
            self.params.append(tuple(params or ()))
            return _DummyResult(rows=[(7,)])

    conn = _Conn()

    @contextmanager
    def _fake_get_connection(_db_path):
        yield conn

    with patch("lib.database.get_connection", side_effect=_fake_get_connection):
        out = maintenance_ops.get_completed_review_work_today()

    assert out["reviewed"] == 7
    assert conn.params == [("2030-01-02T00:00:00",)]


def test_quaid_now_malformed_clock_honors_failhard(monkeypatch):
    monkeypatch.setenv("QUAID_NOW", "not-a-date")

    with patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=True):
        with pytest.raises(RuntimeError, match="Invalid QUAID_NOW"):
            maintenance_ops._quaid_now()

    with patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=False):
        assert isinstance(maintenance_ops._quaid_now(), datetime)


def test_janitor_metrics_events_use_quaid_now(monkeypatch):
    monkeypatch.setenv("QUAID_NOW", "2030-01-02T03:04:05")

    metrics = maintenance_ops.JanitorMetrics()
    metrics.add_error("broken")
    metrics.add_warning("soft")
    summary = metrics.summary()

    assert summary["error_details"] == [{"time": "2030-01-02T03:04:05", "error": "broken"}]
    assert summary["warning_details"] == [{"time": "2030-01-02T03:04:05", "warning": "soft"}]


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

    with patch("lib.embeddings.get_embeddings", return_value=[[0.1, 0.2]]) as get_embeddings, \
         patch("lib.embeddings.pack_embedding", return_value=b"emb"), \
         patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=False), \
         patch.object(maintenance_ops, "_upsert_vec_embedding", return_value=False):
        out = maintenance_ops.backfill_embeddings(graph, metrics, dry_run=False)

    assert out["found"] == 1
    assert out["embedded"] == 0
    assert metrics.summary()["warnings"] >= 1
    get_embeddings.assert_called_once_with(
        ["alpha node"],
        pool_name="janitor_embedding_backfill",
        task_name="janitor",
        timeout_s=120.0,
    )


def test_backfill_embeddings_rolls_back_node_embedding_when_vec_sync_skips(monkeypatch, tmp_path):
    monkeypatch.delenv("QUAID_JANITOR_EMBED_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("OLLAMA_EMBED_TIMEOUT_S", raising=False)
    monkeypatch.delenv("QUAID_JANITOR_EMBED_BACKFILL_LIMIT", raising=False)

    graph = maintenance_ops.MemoryGraph(db_path=tmp_path / "memory.db")
    node = maintenance_ops.Node.create(type="Fact", name="alpha node")
    graph.add_node(node, embed=False)

    metrics = maintenance_ops.JanitorMetrics()
    with patch("lib.embeddings.get_embeddings", return_value=[[0.1, 0.2]]), \
         patch("lib.embeddings.pack_embedding", return_value=b"emb"), \
         patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=False), \
         patch.object(maintenance_ops, "_upsert_vec_embedding", return_value=False):
        out = maintenance_ops.backfill_embeddings(graph, metrics, dry_run=False)

    assert out["found"] == 1
    assert out["embedded"] == 0
    assert metrics.summary()["warnings"] >= 1
    with graph._get_conn() as conn:
        row = conn.execute("SELECT embedding FROM nodes WHERE id = ?", (node.id,)).fetchone()
    assert row is not None
    assert row["embedding"] is None


def test_backfill_embeddings_batches_provider_calls(monkeypatch):
    monkeypatch.delenv("QUAID_JANITOR_EMBED_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("OLLAMA_EMBED_TIMEOUT_S", raising=False)
    monkeypatch.delenv("QUAID_JANITOR_EMBED_BACKFILL_LIMIT", raising=False)

    class _Conn:
        def execute(self, sql, params=()):
            text = str(sql).strip().upper()
            if text.startswith("SELECT ID, NAME FROM NODES WHERE EMBEDDING IS NULL"):
                return _DummyResult(rows=[
                    {"id": "n1", "name": "alpha node"},
                    {"id": "n2", "name": "bravo node"},
                ])
            if text.startswith("SELECT COUNT(*) FROM NODES_FTS"):
                return _DummyResult(rows=[(2,)])
            if text.startswith("SELECT COUNT(*) FROM NODES"):
                return _DummyResult(rows=[(2,)])
            if text.startswith("SELECT ROWID, NAME FROM NODES ORDER BY ROWID DESC LIMIT 1"):
                return _DummyResult(rows=[(2, "bravo node")])
            if text.startswith("SELECT ROWID FROM NODES_FTS WHERE ROWID = ?"):
                return _DummyResult(rows=[(2,)])
            return _DummyResult(rowcount=1)

    class _Graph:
        @contextmanager
        def _get_conn(self):
            yield _Conn()

    metrics = maintenance_ops.JanitorMetrics()
    with patch("lib.embeddings.get_embeddings", return_value=[[0.1], [0.2]]) as get_embeddings, \
         patch("lib.embeddings.pack_embedding", return_value=b"emb"), \
         patch.object(maintenance_ops, "_upsert_vec_embedding", return_value=None):
        out = maintenance_ops.backfill_embeddings(_Graph(), metrics, dry_run=False)

    assert out["found"] == 2
    assert out["embedded"] == 2
    get_embeddings.assert_called_once_with(
        ["alpha node", "bravo node"],
        pool_name="janitor_embedding_backfill",
        task_name="janitor",
        timeout_s=120.0,
    )


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

    with patch("lib.embeddings.get_embeddings", return_value=[[0.1, 0.2]]), \
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

    with patch("lib.embeddings.get_embeddings", return_value=[[0.1, 0.2]]) as get_embeddings, \
         patch("lib.embeddings.pack_embedding", return_value=b"emb"), \
         patch.object(maintenance_ops, "_upsert_vec_embedding", return_value=None):
        out = maintenance_ops.backfill_embeddings(graph, metrics, dry_run=False)

    assert out["found"] == 1
    assert out["embedded"] == 1
    get_embeddings.assert_called_once_with(
        ["alpha node"],
        pool_name="janitor_embedding_backfill",
        task_name="janitor",
        timeout_s=85.0,
    )


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

    with patch("lib.embeddings.get_embeddings", return_value=[[0.1, 0.2]]) as get_embeddings, \
         patch("lib.embeddings.pack_embedding", return_value=b"emb"), \
         patch.object(maintenance_ops, "_upsert_vec_embedding", return_value=None):
        out = maintenance_ops.backfill_embeddings(graph, metrics, dry_run=False)

    assert out["found"] == 1
    assert out["embedded"] == 1
    get_embeddings.assert_called_once_with(
        ["alpha node"],
        pool_name="janitor_embedding_backfill",
        task_name="janitor",
        timeout_s=17.0,
    )


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


def test_backfill_embeddings_fts_failure_warns_and_continues_when_not_failhard(caplog):
    class _Conn:
        def execute(self, sql, params=()):
            text = str(sql).strip().upper()
            if text.startswith("SELECT ID, NAME FROM NODES WHERE EMBEDDING IS NULL"):
                return _DummyResult(rows=[])
            if text.startswith("SELECT COUNT(*) FROM NODES_FTS"):
                raise RuntimeError("fts unavailable")
            return _DummyResult(rows=[(0,)])

    class _Graph:
        @contextmanager
        def _get_conn(self):
            yield _Conn()

    metrics = maintenance_ops.JanitorMetrics()

    with patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=False), caplog.at_level("WARNING"):
        out = maintenance_ops.backfill_embeddings(_Graph(), metrics, dry_run=True)

    assert out == {"found": 0, "embedded": 0, "fts_rebuilt": False}
    assert any("FTS5 integrity check failed" in warning["warning"] for warning in metrics.warnings)
    assert "FTS5 integrity check failed" in caplog.text


def test_backfill_embeddings_fts_failure_raises_when_failhard():
    class _Conn:
        def execute(self, sql, params=()):
            text = str(sql).strip().upper()
            if text.startswith("SELECT ID, NAME FROM NODES WHERE EMBEDDING IS NULL"):
                return _DummyResult(rows=[])
            if text.startswith("SELECT COUNT(*) FROM NODES_FTS"):
                raise RuntimeError("fts unavailable")
            return _DummyResult(rows=[(0,)])

    class _Graph:
        @contextmanager
        def _get_conn(self):
            yield _Conn()

    with patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=True):
        with pytest.raises(RuntimeError, match="FTS5 integrity check failed during embedding backfill") as excinfo:
            maintenance_ops.backfill_embeddings(_Graph(), maintenance_ops.JanitorMetrics(), dry_run=True)

    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert "fts unavailable" in str(excinfo.value.__cause__)


def _review_pending_graph():
    class _Conn:
        def execute(self, sql, params=()):
            text = str(sql).strip().upper()
            if text.startswith("SELECT COUNT(*) FROM NODES WHERE STATUS = 'PENDING'"):
                return _DummyResult(rows=[(1,)])
            if "FROM NODES" in text and "WHERE STATUS = 'PENDING'" in text:
                return _DummyResult(
                    rows=[
                        {
                            "id": "mem-1",
                            "type": "Fact",
                            "name": "Solomon keeps the orange notebook in the cabinet",
                            "created_at": "2026-03-11T00:00:00",
                            "verified": 0,
                            "confidence": 0.8,
                            "source": "unit-test",
                            "session_id": "sess-1",
                            "speaker": "user",
                        }
                    ]
                )
            return _DummyResult(rows=[])

    class _Graph:
        @contextmanager
        def _get_conn(self):
            yield _Conn()

    return _Graph()


def _review_pending_config():
    return SimpleNamespace(
        models=SimpleNamespace(max_output=lambda tier: 1024),
        core=SimpleNamespace(parallel=SimpleNamespace(enabled=False, llm_workers=1, task_workers={})),
    )


def test_review_pending_apply_failure_warns_and_continues_when_not_failhard(caplog):
    metrics = maintenance_ops.JanitorMetrics()

    with patch.object(maintenance_ops, "_cfg", _review_pending_config()), \
         patch.object(maintenance_ops, "_owner_display_name", return_value="Solomon"), \
         patch.object(maintenance_ops, "_owner_full_name", return_value="Solomon Steadman"), \
         patch.object(maintenance_ops, "call_deep_reasoning", return_value=('[{"id":"mem-1","action":"KEEP"}]', 0.05)), \
         patch.object(maintenance_ops, "apply_review_decisions_from_list", side_effect=ValueError("apply failed")), \
         patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=False), \
         caplog.at_level("WARNING"):
        out = maintenance_ops.review_pending_memories(
            _review_pending_graph(),
            dry_run=False,
            metrics=metrics,
            max_items=1,
        )

    assert out["total_reviewed"] == 1
    assert out["kept"] == 0
    assert any("Review batch 1: apply failed" in error["error"] for error in metrics.errors)
    assert "Review batch 1 failed" in caplog.text


def test_review_pending_apply_failure_raises_when_failhard():
    metrics = maintenance_ops.JanitorMetrics()

    with patch.object(maintenance_ops, "_cfg", _review_pending_config()), \
         patch.object(maintenance_ops, "_owner_display_name", return_value="Solomon"), \
         patch.object(maintenance_ops, "_owner_full_name", return_value="Solomon Steadman"), \
         patch.object(maintenance_ops, "call_deep_reasoning", return_value=('[{"id":"mem-1","action":"KEEP"}]', 0.05)), \
         patch.object(maintenance_ops, "apply_review_decisions_from_list", side_effect=ValueError("apply failed")), \
         patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=True):
        with pytest.raises(RuntimeError, match="Review batch 1 failed while failHard is enabled") as excinfo:
            maintenance_ops.review_pending_memories(
                _review_pending_graph(),
                dry_run=False,
                metrics=metrics,
                max_items=1,
            )

    assert isinstance(excinfo.value.__cause__, ValueError)
    assert "apply failed" in str(excinfo.value.__cause__)
