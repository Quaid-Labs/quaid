"""Tests for temporal maintenance routing."""
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from datastore.memorydb.maintenance_ops import JanitorMetrics, resolve_temporal_references


class _NoScanGraph:
    @contextmanager
    def _get_conn(self):
        raise AssertionError("temporal maintenance must not scan or rewrite stored text")
        yield


def test_resolve_temporal_references_is_llm_review_owned_noop(capsys):
    metrics = JanitorMetrics()

    result = resolve_temporal_references(_NoScanGraph(), dry_run=False, metrics=metrics)

    assert result == {"found": 0, "fixed": 0, "skipped": 0}
    assert metrics.task_duration("temporal_resolution") >= 0.0
    assert "LLM memory review pass" in capsys.readouterr().out
