import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datastore.memorydb import memory_graph


class _Conn:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, _query):
        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def fetchall(self):
                return self._rows

        return _Result(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _Graph:
    def __init__(self, rows):
        self._rows = rows

    def _get_conn(self):
        return _Conn(self._rows)


def test_get_edge_keywords_logs_invalid_payload(monkeypatch, caplog):
    monkeypatch.setattr(
        memory_graph,
        "get_graph",
        lambda: _Graph([{"relation": "parent_of", "keywords": "not-json"}]),
    )

    with caplog.at_level("WARNING"):
        out = memory_graph.get_edge_keywords()

    assert out["parent_of"] == []
    assert "invalid edge_keywords payload" in caplog.text


def test_get_edge_keywords_normalizes_non_string_entries(monkeypatch):
    monkeypatch.setattr(
        memory_graph,
        "get_graph",
        lambda: _Graph([{"relation": "parent_of", "keywords": '["mom", "", 42]'}]),
    )

    out = memory_graph.get_edge_keywords()
    assert out["parent_of"] == ["mom", "42"]
