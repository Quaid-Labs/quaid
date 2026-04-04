"""Unit tests for deferred operator notices."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.adapter import TestAdapter, reset_adapter, set_adapter
from lib.agent_notice import (
    drain_deferred_notices,
    format_deferred_notice_hint,
    get_deferred_notice_status,
    queue_deferred_notice,
)


@pytest.fixture(autouse=True)
def clean_adapter(tmp_path):
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)
    yield adapter
    reset_adapter()


def _notes_path(adapter):
    return adapter.instance_root() / ".runtime" / "notes" / "delayed-llm-requests.json"


def _read_requests(adapter):
    path = _notes_path(adapter)
    return json.loads(path.read_text(encoding="utf-8")).get("requests", [])


# ---------------------------------------------------------------------------
# Basic write
# ---------------------------------------------------------------------------


def test_queue_writes_runtime_note(clean_adapter):
    queued = queue_deferred_notice("update ready", kind="doc_update", priority="normal", source="pytest")

    assert queued is True
    reqs = _read_requests(clean_adapter)
    assert len(reqs) == 1
    assert reqs[0]["kind"] == "doc_update"
    assert reqs[0]["message"] == "update ready"
    assert reqs[0]["status"] == "pending"
    assert reqs[0]["priority"] == "normal"
    assert reqs[0]["source"] == "pytest"


def test_queue_returns_true_on_success(clean_adapter):
    assert queue_deferred_notice("msg", kind="janitor") is True


# ---------------------------------------------------------------------------
# Empty / blank message
# ---------------------------------------------------------------------------


def test_empty_message_returns_false(clean_adapter):
    assert queue_deferred_notice("") is False


def test_whitespace_only_message_returns_false(clean_adapter):
    assert queue_deferred_notice("   ") is False


def test_none_message_returns_false(clean_adapter):
    assert queue_deferred_notice(None) is False


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_default_kind_is_janitor(clean_adapter):
    queue_deferred_notice("hello")
    reqs = _read_requests(clean_adapter)
    assert reqs[0]["kind"] == "janitor"


def test_default_priority_is_normal(clean_adapter):
    queue_deferred_notice("hello")
    reqs = _read_requests(clean_adapter)
    assert reqs[0]["priority"] == "normal"


def test_default_source_is_quaid(clean_adapter):
    queue_deferred_notice("hello")
    reqs = _read_requests(clean_adapter)
    assert reqs[0]["source"] == "quaid"


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def test_same_kind_and_message_deduped(clean_adapter):
    first = queue_deferred_notice("same", kind="janitor")
    second = queue_deferred_notice("same", kind="janitor")
    assert first is True
    assert second is False


def test_dedup_only_one_request_written(clean_adapter):
    queue_deferred_notice("same", kind="janitor")
    queue_deferred_notice("same", kind="janitor")
    assert len(_read_requests(clean_adapter)) == 1


def test_different_messages_both_written(clean_adapter):
    queue_deferred_notice("msg-a", kind="janitor")
    queue_deferred_notice("msg-b", kind="janitor")
    reqs = _read_requests(clean_adapter)
    assert len(reqs) == 2


def test_different_kind_same_message_both_written(clean_adapter):
    queue_deferred_notice("same", kind="janitor")
    queue_deferred_notice("same", kind="doc_update")
    reqs = _read_requests(clean_adapter)
    assert len(reqs) == 2


def test_completed_request_not_deduped(clean_adapter):
    """A request with status != 'pending' should not block re-queueing."""
    queue_deferred_notice("msg", kind="janitor")
    # Manually mark as completed
    path = _notes_path(clean_adapter)
    payload = json.loads(path.read_text())
    payload["requests"][0]["status"] = "completed"
    path.write_text(json.dumps(payload))

    second = queue_deferred_notice("msg", kind="janitor")
    assert second is True
    assert len(_read_requests(clean_adapter)) == 2


# ---------------------------------------------------------------------------
# Recovery from malformed/corrupt file
# ---------------------------------------------------------------------------


def test_malformed_json_file_replaced(clean_adapter):
    path = _notes_path(clean_adapter)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("}{not json", encoding="utf-8")

    queued = queue_deferred_notice("after corruption", kind="janitor")
    assert queued is True
    reqs = _read_requests(clean_adapter)
    assert len(reqs) == 1


def test_non_dict_json_file_replaced(clean_adapter):
    path = _notes_path(clean_adapter)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    queued = queue_deferred_notice("after list", kind="janitor")
    assert queued is True


# ---------------------------------------------------------------------------
# Multiple accumulated requests
# ---------------------------------------------------------------------------


def test_multiple_different_requests_accumulate(clean_adapter):
    for i in range(5):
        queue_deferred_notice(f"message {i}", kind="janitor")
    reqs = _read_requests(clean_adapter)
    assert len(reqs) == 5


def test_deferred_status_and_hint_reflect_pending_requests(clean_adapter):
    queue_deferred_notice("janitor summary", kind="janitor_summary", priority="low")
    queue_deferred_notice("provider outage recap", kind="provider", priority="high")

    status = get_deferred_notice_status()
    assert status["pending_count"] == 2
    assert status["kinds"]["janitor_summary"] == 1
    assert status["kinds"]["provider"] == 1

    hint = format_deferred_notice_hint()
    assert "deferred maintenance notices" in hint
    assert "quaid notify --deferred-drain" in hint


def test_drain_marks_requests_delivered(clean_adapter):
    queue_deferred_notice("first", kind="janitor_summary", priority="low")
    queue_deferred_notice("second", kind="update_available", priority="high")

    drained = drain_deferred_notices(limit=1)
    assert len(drained) == 1
    assert drained[0]["kind"] == "update_available"
    assert drained[0]["status"] == "delivered"

    requests = _read_requests(clean_adapter)
    delivered = [item for item in requests if item["status"] == "delivered"]
    pending = [item for item in requests if item["status"] == "pending"]
    assert len(delivered) == 1
    assert len(pending) == 1
