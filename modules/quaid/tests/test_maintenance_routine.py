import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datastore.memorydb.maintenance import register_lifecycle_routines


class _Registry:
    def __init__(self):
        self.handlers = {}

    def register(self, name, handler):
        self.handlers[name] = handler


class _Result:
    def __init__(self):
        self.metrics = {}
        self.data = {}
        self.errors = []


def test_maintenance_errors_include_exception_type():
    registry = _Registry()
    register_lifecycle_routines(registry, _Result)
    handler = registry.handlers["memory_graph_maintenance"]

    ctx = SimpleNamespace(
        graph=object(),
        options={"subtask": "review"},
        dry_run=True,
    )

    with patch("datastore.memorydb.maintenance.ops.review_pending_memories", side_effect=ValueError("bad input")):
        result = handler(ctx)

    assert result.errors
    assert "ValueError" in result.errors[0]


def test_maintenance_runtime_errors_use_consistent_prefix():
    registry = _Registry()
    register_lifecycle_routines(registry, _Result)
    handler = registry.handlers["memory_graph_maintenance"]

    ctx = SimpleNamespace(
        graph=object(),
        options={"subtask": "review"},
        dry_run=True,
    )

    with patch("datastore.memorydb.maintenance.ops.review_pending_memories", side_effect=RuntimeError("boom")):
        result = handler(ctx)

    assert result.errors
    assert result.errors[0].startswith("Memory graph maintenance failed (RuntimeError):")


def test_maintenance_passes_llm_timeout_to_dedup_review():
    registry = _Registry()
    register_lifecycle_routines(registry, _Result)
    handler = registry.handlers["memory_graph_maintenance"]

    ctx = SimpleNamespace(
        graph=object(),
        options={"subtask": "dedup_review", "llm_timeout_seconds": 42},
        dry_run=True,
    )

    with patch(
        "datastore.memorydb.maintenance.ops.review_dedup_rejections",
        return_value={"reviewed": 0, "confirmed": 0, "reversed": 0, "carryover": 0},
    ) as review_mock:
        result = handler(ctx)

    assert not result.errors
    assert review_mock.call_args.kwargs["llm_timeout_seconds"] == 42.0
