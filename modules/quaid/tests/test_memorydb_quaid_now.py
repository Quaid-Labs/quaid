from contextlib import contextmanager

import pytest

from datastore.memorydb import memory_graph


class _Result:
    def __init__(self, rows=None, rowcount=1):
        self._rows = rows or []
        self.rowcount = rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


def test_memorydb_review_helpers_bind_quaid_now_instead_of_sql_now(monkeypatch):
    monkeypatch.setenv("QUAID_NOW", "2026-03-11T00:00:00Z")
    calls = []

    class _Conn:
        def execute(self, sql, params=()):
            calls.append((str(sql), params))
            if "SELECT id FROM decay_review_queue" in sql:
                return _Result(rows=[])
            return _Result(rowcount=1)

    class _Graph:
        @contextmanager
        def _get_conn(self):
            yield _Conn()

    monkeypatch.setattr(memory_graph, "get_graph", lambda: _Graph())

    assert memory_graph.resolve_contradiction("c1", "keep_a", "reason")
    assert memory_graph.mark_contradiction_false_positive("c2", "reason")
    assert memory_graph.get_recent_dedup_rejections(hours=24, limit=5) == []
    assert memory_graph.resolve_dedup_review("d1", "confirmed", "same")
    assert memory_graph.queue_for_decay_review({
        "id": "n1",
        "text": "Quaid keeps the archive shelf",
        "type": "Fact",
        "confidence": 0.4,
        "access_count": 0,
        "last_accessed": "2026-01-01T00:00:00",
        "verified": False,
        "created_at": "2025-01-01T00:00:00",
    })
    assert memory_graph.resolve_decay_review("q1", "extend", "still useful")
    assert memory_graph.decay_memories() == {"decayed_count": 1}

    sql_text = "\n".join(sql for sql, _params in calls)
    assert "datetime('now')" not in sql_text
    assert "queued_at" in sql_text
    flat_params = [item for _sql, params in calls for item in params]
    assert flat_params.count("2026-03-11T00:00:00+00:00") == 7
    assert "2026-03-10T00:00:00+00:00" in flat_params
    assert "2026-02-09T00:00:00+00:00" in flat_params


def test_memorydb_now_rejects_malformed_quaid_now(monkeypatch):
    monkeypatch.setenv("QUAID_NOW", "not-a-date")

    with pytest.raises(ValueError, match="Invalid QUAID_NOW"):
        memory_graph._now()
