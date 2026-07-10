"""Unit tests for deferred operator notices."""

import json
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.adapter import TestAdapter, reset_adapter, set_adapter
from lib.agent_notice import (
    clear_deferred_notices_by_source,
    deliver_deferred_notices,
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


def test_deferred_file_lock_logs_acquire_failure(tmp_path, monkeypatch, caplog):
    import lib.agent_notice as agent_notice

    class FakeFcntl:
        LOCK_EX = 1
        LOCK_UN = 2

        def flock(self, _handle, operation):
            if operation == self.LOCK_EX:
                raise OSError("lock unavailable")

    monkeypatch.setitem(sys.modules, "fcntl", FakeFcntl())

    with caplog.at_level("WARNING", logger="lib.agent_notice"):
        with agent_notice._file_lock(tmp_path / "deferred.lock"):
            pass

    assert "Deferred-notices file lock acquisition failed" in caplog.text
    assert "lock unavailable" in caplog.text


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


def test_queue_and_drain_timestamps_honor_quaid_now(clean_adapter, monkeypatch):
    monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:06:07Z")
    assert queue_deferred_notice("clocked", kind="doc_update", priority="normal", source="pytest")

    reqs = _read_requests(clean_adapter)
    assert reqs[0]["created_at"] == "2026-03-11T05:06:07+00:00"

    monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:07:08Z")
    drained = drain_deferred_notices(limit=1)
    assert drained[0]["delivered_at"] == "2026-03-11T05:07:08+00:00"

    reqs = _read_requests(clean_adapter)
    assert reqs[0]["delivered_at"] == "2026-03-11T05:07:08+00:00"


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


def test_delivered_janitor_request_deduped(clean_adapter):
    queue_deferred_notice("msg", kind="janitor_summary", priority="low", source="janitor")
    path = _notes_path(clean_adapter)
    payload = json.loads(path.read_text())
    payload["requests"][0]["status"] = "delivered"
    payload["requests"][0]["delivered_at"] = "2026-04-26T00:00:00Z"
    path.write_text(json.dumps(payload))

    second = queue_deferred_notice("msg", kind="janitor_summary", priority="low", source="janitor")
    assert second is False
    assert len(_read_requests(clean_adapter)) == 1


def test_delivered_provider_request_not_deduped(clean_adapter):
    queue_deferred_notice("msg", kind="provider", priority="high", source="provider")
    path = _notes_path(clean_adapter)
    payload = json.loads(path.read_text())
    payload["requests"][0]["status"] = "delivered"
    payload["requests"][0]["delivered_at"] = "2026-04-26T00:00:00Z"
    path.write_text(json.dumps(payload))

    second = queue_deferred_notice("msg", kind="provider", priority="high", source="provider")
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

    status = get_deferred_notice_status(include_items=True)
    assert status["pending_count"] == 2
    assert status["kinds"]["janitor_summary"] == 1
    assert status["kinds"]["provider"] == 1
    assert status["items"][0]["kind"] == "provider"
    assert status["items"][1]["kind"] == "janitor_summary"

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


def test_clear_deferred_notices_by_source_prunes_provider_pending_and_delivered(clean_adapter):
    queue_deferred_notice("provider delivered", kind="provider", priority="high", source="provider")
    queue_deferred_notice("provider pending", kind="provider", priority="high", source="provider")
    queue_deferred_notice("janitor pending", kind="janitor_summary", priority="low", source="janitor")

    path = _notes_path(clean_adapter)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["requests"][0]["status"] = "delivered"
    payload["requests"][0]["delivered_at"] = "2026-04-25T00:00:00Z"
    path.write_text(json.dumps(payload), encoding="utf-8")

    removed = clear_deferred_notices_by_source(sources={"provider"})

    assert removed == 2
    requests = _read_requests(clean_adapter)
    assert len(requests) == 1
    assert requests[0]["source"] == "janitor"
    assert requests[0]["status"] == "pending"


def test_deliver_deferred_notices_marks_only_successful_sends(clean_adapter):
    queue_deferred_notice("first", kind="janitor_summary", priority="low")
    queue_deferred_notice("second", kind="provider", priority="high")

    with patch("lib.runtime_context.send_notification", side_effect=[True, False]) as mock_send:
        delivered = deliver_deferred_notices(limit=10)

    assert [item["kind"] for item in delivered] == ["provider"]
    assert mock_send.call_count == 2

    requests = _read_requests(clean_adapter)
    delivered_items = [item for item in requests if item["status"] == "delivered"]
    pending_items = [item for item in requests if item["status"] == "pending"]
    assert len(delivered_items) == 1
    assert delivered_items[0]["kind"] == "provider"
    assert len(pending_items) == 1
    assert pending_items[0]["kind"] == "janitor_summary"


def test_deliver_deferred_notices_raises_send_failure_when_failhard(clean_adapter):
    queue_deferred_notice("first", kind="janitor_summary", priority="low")

    with patch("lib.runtime_context.send_notification", side_effect=RuntimeError("sender broken")), \
         patch("lib.agent_notice.is_fail_hard_enabled", return_value=True):
        with pytest.raises(RuntimeError, match="sender broken"):
            deliver_deferred_notices(limit=10)


def test_deliver_deferred_notices_raises_import_failure_when_failhard(clean_adapter, monkeypatch):
    import builtins

    queue_deferred_notice("first", kind="janitor_summary", priority="low")
    real_import = builtins.__import__

    def failing_import(name, *args, **kwargs):
        if name == "lib.runtime_context":
            raise ImportError("runtime context unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing_import)

    with patch("lib.agent_notice.is_fail_hard_enabled", return_value=True):
        with pytest.raises(ImportError, match="runtime context unavailable"):
            deliver_deferred_notices(limit=10)


def test_deliver_deferred_notices_dry_run_keeps_items_pending(clean_adapter):
    queue_deferred_notice("first", kind="janitor_summary", priority="low")

    with patch("lib.runtime_context.send_notification", return_value=True) as mock_send:
        delivered = deliver_deferred_notices(limit=10, dry_run=True)

    assert delivered == []
    mock_send.assert_called_once()

    requests = _read_requests(clean_adapter)
    assert len(requests) == 1
    assert requests[0]["status"] == "pending"
