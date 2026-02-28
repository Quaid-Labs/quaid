"""Regression tests for pending-memory review decision coverage."""

import hashlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["MEMORY_DB_PATH"] = ":memory:"

from datastore.memorydb.memory_graph import MemoryGraph, Node
from datastore.memorydb.maintenance_ops import JanitorMetrics, review_pending_memories


def _fake_get_embedding(text: str):
    h = hashlib.md5(text.encode()).digest()
    return [float(b) / 255.0 for b in h] * 8  # 128-dim


def _make_graph(tmp_path: Path) -> MemoryGraph:
    db_file = tmp_path / "review_coverage.db"
    with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
        return MemoryGraph(db_path=db_file)


def _add_pending_fact(graph: MemoryGraph, text: str) -> Node:
    node = Node.create(
        type="Fact",
        name=text,
        confidence=0.75,
        owner_id="quaid",
        status="pending",
    )
    with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
        graph.add_node(node)
    return node


def test_review_pending_memories_retries_missing_decisions(tmp_path):
    graph = _make_graph(tmp_path)
    n1 = _add_pending_fact(graph, "My mother is Wendy")
    n2 = _add_pending_fact(graph, "My father is Kent")
    metrics = JanitorMetrics()

    responses = [
        (json.dumps([{"id": n1.id, "action": "KEEP"}]), 0.1),  # Missing n2 in first pass.
        (json.dumps([{"id": n2.id, "action": "KEEP"}]), 0.1),  # Retry covers missing id.
    ]

    with patch("datastore.memorydb.maintenance_ops.call_deep_reasoning", side_effect=responses):
        result = review_pending_memories(graph, dry_run=False, metrics=metrics, max_items=10)

    assert result["total_reviewed"] == 2
    assert result["review_coverage_ratio"] == 1.0
    assert result["missing_ids"] == []
    assert metrics.warnings, "Expected warning due to first-pass incomplete coverage"

    with graph._get_conn() as conn:
        approved = conn.execute("SELECT COUNT(*) FROM nodes WHERE status = 'approved'").fetchone()[0]
        pending = conn.execute("SELECT COUNT(*) FROM nodes WHERE status = 'pending'").fetchone()[0]
    assert approved == 2
    assert pending == 0


def test_review_pending_memories_raises_on_incomplete_coverage_after_retry(tmp_path):
    graph = _make_graph(tmp_path)
    n1 = _add_pending_fact(graph, "I live in Austin")
    n2 = _add_pending_fact(graph, "My dog is Madu")
    metrics = JanitorMetrics()

    responses = [
        (json.dumps([{"id": n1.id, "action": "KEEP"}]), 0.1),  # Missing n2
        (json.dumps([]), 0.1),  # Retry still missing n2
    ]

    with patch("datastore.memorydb.maintenance_ops.call_deep_reasoning", side_effect=responses):
        with pytest.raises(RuntimeError, match="incomplete decision coverage after retry"):
            review_pending_memories(graph, dry_run=False, metrics=metrics, max_items=10)

    with graph._get_conn() as conn:
        pending = conn.execute("SELECT COUNT(*) FROM nodes WHERE status = 'pending'").fetchone()[0]
    assert pending == 2


def test_review_pending_memories_accepts_wrapped_decisions_payload(tmp_path):
    graph = _make_graph(tmp_path)
    n1 = _add_pending_fact(graph, "My mother is Wendy")
    n2 = _add_pending_fact(graph, "My father is Kent")
    metrics = JanitorMetrics()

    wrapped = {
        "decisions": [
            {"memory_id": n1.id, "decision": "APPROVE"},
            {"memory_id": n2.id, "decision": "APPROVE"},
        ]
    }

    with patch("datastore.memorydb.maintenance_ops.call_deep_reasoning", return_value=(json.dumps(wrapped), 0.1)):
        result = review_pending_memories(graph, dry_run=False, metrics=metrics, max_items=10)

    assert result["total_reviewed"] == 2
    assert result["review_coverage_ratio"] == 1.0
    assert result["missing_ids"] == []

    with graph._get_conn() as conn:
        approved = conn.execute("SELECT COUNT(*) FROM nodes WHERE status = 'approved'").fetchone()[0]
        pending = conn.execute("SELECT COUNT(*) FROM nodes WHERE status = 'pending'").fetchone()[0]
    assert approved == 2
    assert pending == 0


def test_review_pending_memories_aborts_on_connection_errors(tmp_path):
    graph = _make_graph(tmp_path)
    _add_pending_fact(graph, "My favorite editor is vim")
    metrics = JanitorMetrics()

    with patch("datastore.memorydb.maintenance_ops.call_deep_reasoning", side_effect=ConnectionError("gateway unavailable")):
        with pytest.raises(RuntimeError, match="gateway unavailable"):
            review_pending_memories(graph, dry_run=False, metrics=metrics, max_items=10)
    with graph._get_conn() as conn:
        pending = conn.execute("SELECT COUNT(*) FROM nodes WHERE status = 'pending'").fetchone()[0]
    assert pending == 1
