import os
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

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
        self.logs = []


def test_maintenance_errors_include_exception_type():
    registry = _Registry()
    register_lifecycle_routines(registry, _Result)
    handler = registry.handlers["memory_graph_maintenance"]

    ctx = SimpleNamespace(
        graph=object(),
        options={"subtask": "review"},
        dry_run=True,
    )

    with patch("datastore.memorydb.maintenance.ops.review_pending_memories", side_effect=ValueError("bad input")), \
         patch("datastore.memorydb.maintenance._fail_hard_enabled", return_value=False):
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

    with patch("datastore.memorydb.maintenance.ops.review_pending_memories", side_effect=RuntimeError("boom")), \
         patch("datastore.memorydb.maintenance._fail_hard_enabled", return_value=False):
        result = handler(ctx)

    assert result.errors
    assert result.errors[0].startswith("Memory graph maintenance failed (RuntimeError):")


def test_maintenance_reraises_task_failure_under_failhard():
    registry = _Registry()
    register_lifecycle_routines(registry, _Result)
    handler = registry.handlers["memory_graph_maintenance"]

    ctx = SimpleNamespace(
        graph=object(),
        options={"subtask": "review"},
        dry_run=True,
    )

    with patch("datastore.memorydb.maintenance.ops.review_pending_memories", side_effect=RuntimeError("boom")), \
         patch("datastore.memorydb.maintenance._fail_hard_enabled", return_value=True):
        with pytest.raises(RuntimeError, match="boom"):
            handler(ctx)


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


def test_maintenance_retries_locked_dedup_review():
    from datastore.memorydb import maintenance

    registry = _Registry()
    register_lifecycle_routines(registry, _Result)
    handler = registry.handlers["memory_graph_maintenance"]
    sleeps = []

    ctx = SimpleNamespace(
        graph=object(),
        options={"subtask": "dedup_review"},
        dry_run=False,
    )

    with patch(
        "datastore.memorydb.maintenance.ops.review_dedup_rejections",
        side_effect=[
            sqlite3.OperationalError("database is locked"),
            {"reviewed": 1, "confirmed": 1, "reversed": 0, "carryover": 0},
        ],
    ) as review_mock, patch("datastore.memorydb.maintenance.time.sleep", lambda delay: sleeps.append(delay)):
        result = handler(ctx)

    assert result.errors == []
    assert result.metrics["dedup_reviewed"] == 1
    assert review_mock.call_count == 2
    assert sleeps == [0.1]
    assert any("database busy" in line for line in result.logs)


def test_maintenance_exhausts_locked_dedup_review_retry():
    from datastore.memorydb import maintenance

    registry = _Registry()
    register_lifecycle_routines(registry, _Result)
    handler = registry.handlers["memory_graph_maintenance"]
    sleeps = []

    ctx = SimpleNamespace(
        graph=object(),
        options={"subtask": "dedup_review"},
        dry_run=False,
    )

    with patch(
        "datastore.memorydb.maintenance.ops.review_dedup_rejections",
        side_effect=sqlite3.OperationalError("database is locked"),
    ) as review_mock, \
         patch("datastore.memorydb.maintenance.time.sleep", lambda delay: sleeps.append(delay)), \
         patch("datastore.memorydb.maintenance._fail_hard_enabled", return_value=False):
        result = handler(ctx)

    expected_delays = list(maintenance._DATASTORE_BUSY_RETRY_DELAYS_SECONDS)
    assert result.errors == ["Memory graph maintenance failed (OperationalError): database is locked"]
    assert review_mock.call_count == len(expected_delays) + 1
    assert sleeps == expected_delays


def test_maintenance_does_not_retry_non_lock_sqlite_error():
    registry = _Registry()
    register_lifecycle_routines(registry, _Result)
    handler = registry.handlers["memory_graph_maintenance"]
    sleeps = []

    ctx = SimpleNamespace(
        graph=object(),
        options={"subtask": "dedup_review"},
        dry_run=False,
    )

    with patch(
        "datastore.memorydb.maintenance.ops.review_dedup_rejections",
        side_effect=sqlite3.OperationalError("no such table: dedup_log"),
    ) as review_mock, \
         patch("datastore.memorydb.maintenance.time.sleep", lambda delay: sleeps.append(delay)), \
         patch("datastore.memorydb.maintenance._fail_hard_enabled", return_value=False):
        result = handler(ctx)

    assert result.errors == ["Memory graph maintenance failed (OperationalError): no such table: dedup_log"]
    assert review_mock.call_count == 1
    assert sleeps == []


def test_maintenance_defaults_missing_subtask_to_review():
    registry = _Registry()
    register_lifecycle_routines(registry, _Result)
    handler = registry.handlers["memory_graph_maintenance"]

    ctx = SimpleNamespace(
        graph=object(),
        options={},
        dry_run=True,
    )

    with patch(
        "datastore.memorydb.maintenance.ops.review_pending_memories",
        return_value={"total_reviewed": 0, "deleted": 0, "fixed": 0, "carryover": 0, "review_coverage_ratio": 1.0},
    ) as review_mock:
        result = handler(ctx)

    assert not result.errors
    assert review_mock.call_count == 1
