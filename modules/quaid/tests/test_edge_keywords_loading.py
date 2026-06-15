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


class _FailingConn:
    def execute(self, *_args, **_kwargs):
        raise RuntimeError("edge keyword write failed")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FailingGraph:
    def _get_conn(self):
        return _FailingConn()


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


def test_get_edge_keywords_returns_valid_keywords(monkeypatch):
    monkeypatch.setattr(
        memory_graph,
        "get_graph",
        lambda: _Graph([{"relation": "sibling_of", "keywords": '["sister", "brother"]'}]),
    )

    out = memory_graph.get_edge_keywords()
    assert out["sibling_of"] == ["sister", "brother"]


def test_get_edge_keywords_empty_table_returns_empty_dict(monkeypatch):
    monkeypatch.setattr(
        memory_graph,
        "get_graph",
        lambda: _Graph([]),
    )

    out = memory_graph.get_edge_keywords()
    assert out == {}


def test_get_edge_keywords_multiple_relations(monkeypatch):
    monkeypatch.setattr(
        memory_graph,
        "get_graph",
        lambda: _Graph([
            {"relation": "parent_of", "keywords": '["child", "son", "daughter"]'},
            {"relation": "works_at", "keywords": '["employer", "company"]'},
        ]),
    )

    out = memory_graph.get_edge_keywords()
    assert out["parent_of"] == ["child", "son", "daughter"]
    assert out["works_at"] == ["employer", "company"]


def test_get_edge_keywords_non_array_json_produces_empty(monkeypatch, caplog):
    """A JSON object (not array) should fall back to empty list."""
    monkeypatch.setattr(
        memory_graph,
        "get_graph",
        lambda: _Graph([{"relation": "cousin_of", "keywords": '{"key": "value"}'}]),
    )

    with caplog.at_level("WARNING"):
        out = memory_graph.get_edge_keywords()

    assert out["cousin_of"] == []
    assert "invalid edge_keywords payload" in caplog.text


def test_get_edge_keywords_strips_whitespace_from_keywords(monkeypatch):
    monkeypatch.setattr(
        memory_graph,
        "get_graph",
        lambda: _Graph([{"relation": "lives_at", "keywords": '["  home  ", "  "]'}]),
    )

    out = memory_graph.get_edge_keywords()
    # "  home  " → "home"; "  " → "" → stripped → excluded
    assert out["lives_at"] == ["home"]


def test_store_edge_keywords_logs_and_returns_false_when_fail_hard_disabled(monkeypatch, caplog):
    monkeypatch.setattr(memory_graph, "get_graph", lambda: _FailingGraph())
    monkeypatch.setattr(memory_graph, "_is_fail_hard_mode", lambda: False)

    with caplog.at_level("WARNING"):
        out = memory_graph.store_edge_keywords("parent_of", ["parent"])

    assert out is False
    assert "Failed to store keywords for parent_of" in caplog.text
    assert "edge keyword write failed" in caplog.text


def test_store_edge_keywords_raises_when_fail_hard_enabled(monkeypatch):
    monkeypatch.setattr(memory_graph, "get_graph", lambda: _FailingGraph())
    monkeypatch.setattr(memory_graph, "_is_fail_hard_mode", lambda: True)

    with pytest.raises(RuntimeError, match="Failed to store graph edge keywords"):
        memory_graph.store_edge_keywords("parent_of", ["parent"])
