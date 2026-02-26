import os
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datastore.docsdb.rag import register_lifecycle_routines as register_rag_lifecycle_routines
from datastore.memorydb import memory_graph
from datastore.notedb.soul_snippets import register_lifecycle_routines as register_snippets_lifecycle_routines


class _Registry:
    def __init__(self):
        self.handlers = {}

    def register(self, name, handler):
        self.handlers[name] = handler


class _Result:
    def __init__(self):
        self.metrics = {}
        self.logs = []
        self.errors = []
        self.data = {}


def test_store_contradiction_raises_when_fail_hard_enabled():
    class _BrokenConn:
        def __enter__(self):
            raise sqlite3.OperationalError("db down")

        def __exit__(self, exc_type, exc, tb):
            return False

    class _BrokenGraph:
        def _get_conn(self):
            return _BrokenConn()

    with patch("datastore.memorydb.memory_graph.get_graph", return_value=_BrokenGraph()), \
         patch("lib.fail_policy.is_fail_hard_enabled", return_value=True):
        with pytest.raises(RuntimeError, match="Failed to store contradiction"):
            memory_graph.store_contradiction("a-node", "b-node", "reason")


def test_store_contradiction_soft_mode_returns_none_on_db_error():
    class _BrokenConn:
        def __enter__(self):
            raise sqlite3.OperationalError("db down")

        def __exit__(self, exc_type, exc, tb):
            return False

    class _BrokenGraph:
        def _get_conn(self):
            return _BrokenConn()

    with patch("datastore.memorydb.memory_graph.get_graph", return_value=_BrokenGraph()), \
         patch("lib.fail_policy.is_fail_hard_enabled", return_value=False):
        assert memory_graph.store_contradiction("a-node", "b-node", "reason") is None


def test_snippets_lifecycle_raises_when_fail_hard_enabled():
    registry = _Registry()
    register_snippets_lifecycle_routines(registry, _Result)
    handler = registry.handlers["snippets"]

    ctx = SimpleNamespace(dry_run=True, parallel_map=None, options={}, force_distill=False)
    with patch("datastore.notedb.soul_snippets.run_soul_snippets_review", side_effect=RuntimeError("boom")), \
         patch("datastore.notedb.soul_snippets.is_fail_hard_enabled", return_value=True):
        with pytest.raises(RuntimeError, match="Snippets review failed"):
            handler(ctx)


def test_journal_lifecycle_raises_when_fail_hard_enabled():
    registry = _Registry()
    register_snippets_lifecycle_routines(registry, _Result)
    handler = registry.handlers["journal"]

    ctx = SimpleNamespace(dry_run=True, parallel_map=None, options={}, force_distill=False)
    with patch("datastore.notedb.soul_snippets.run_journal_distillation", side_effect=RuntimeError("boom")), \
         patch("datastore.notedb.soul_snippets.is_fail_hard_enabled", return_value=True):
        with pytest.raises(RuntimeError, match="Journal distillation failed"):
            handler(ctx)


def test_rag_lifecycle_raises_when_fail_hard_enabled(tmp_path):
    registry = _Registry()
    register_rag_lifecycle_routines(registry, _Result)
    handler = registry.handlers["rag"]

    cfg = SimpleNamespace(
        rag=SimpleNamespace(docs_dir="docs"),
        projects=SimpleNamespace(enabled=False, definitions={}),
    )
    ctx = SimpleNamespace(cfg=cfg, dry_run=False, workspace=tmp_path)

    with patch("datastore.docsdb.rag.DocsRAG.reindex_all", side_effect=RuntimeError("index failed")), \
         patch("datastore.docsdb.rag.is_fail_hard_enabled", return_value=True):
        with pytest.raises(RuntimeError, match="RAG maintenance failed"):
            handler(ctx)
