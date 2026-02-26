import json

from lib.adapter import StandaloneAdapter, reset_adapter, set_adapter
from lib.delayed_requests import queue_delayed_request


def setup_function():
    reset_adapter()


def teardown_function():
    reset_adapter()


def test_queue_delayed_request_writes_runtime_note(tmp_path):
    set_adapter(StandaloneAdapter(home=tmp_path))

    queued = queue_delayed_request(
        "update ready",
        kind="doc_update",
        priority="normal",
        source="pytest",
    )

    assert queued is True
    path = tmp_path / ".quaid" / "runtime" / "notes" / "delayed-llm-requests.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    requests = payload.get("requests") or []
    assert len(requests) == 1
    assert requests[0]["kind"] == "doc_update"
    assert requests[0]["message"] == "update ready"


def test_queue_delayed_request_dedupes_pending_items(tmp_path):
    set_adapter(StandaloneAdapter(home=tmp_path))

    first = queue_delayed_request("same", kind="janitor", priority="normal", source="pytest")
    second = queue_delayed_request("same", kind="janitor", priority="normal", source="pytest")

    assert first is True
    assert second is False
