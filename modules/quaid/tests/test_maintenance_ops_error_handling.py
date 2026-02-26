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
    class _Conn:
        def execute(self, sql, params):
            if "INSERT OR REPLACE INTO vec_nodes" in sql:
                raise RuntimeError("vec write failed")
            return _DummyResult(rowcount=1)

    class _Graph:
        @contextmanager
        def _get_conn(self):
            yield _Conn()

    graph = _Graph()
    decisions = [{"id": "n1", "action": "FIX", "new_text": "updated text", "edges": []}]

    with patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=False), \
         patch("lib.embeddings.get_embedding", return_value=[0.1]), \
         patch("lib.embeddings.pack_embedding", return_value=b"x"):
        out = maintenance_ops.apply_review_decisions_from_list(graph, decisions, dry_run=False)
        assert out["fixed"] == 1

    with patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=True), \
         patch("lib.embeddings.get_embedding", return_value=[0.1]), \
         patch("lib.embeddings.pack_embedding", return_value=b"x"):
        with pytest.raises(RuntimeError, match="vec_nodes update failed"):
            maintenance_ops.apply_review_decisions_from_list(graph, decisions, dry_run=False)
