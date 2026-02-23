import json

from events import emit_event, get_event_registry, get_event_capability, list_events, process_events
from lib.adapter import StandaloneAdapter, reset_adapter, set_adapter


def setup_function():
    reset_adapter()


def teardown_function():
    reset_adapter()


def test_event_emit_list_and_capabilities(tmp_path):
    set_adapter(StandaloneAdapter(home=tmp_path))

    event = emit_event(
        name="session.reset",
        payload={"reason": "test"},
        source="pytest",
        session_id="sess-1",
        owner_id="quaid",
    )
    assert event["name"] == "session.reset"
    assert event["status"] == "pending"

    items = list_events(status="pending", limit=10)
    assert len(items) >= 1
    assert any(e.get("name") == "session.reset" for e in items)

    caps = get_event_registry()
    assert any(c.get("name") == "session.reset" for c in caps)
    assert any(c.get("name") == "notification.delayed" for c in caps)
    assert any(c.get("name") == "session.reset" and c.get("delivery_mode") == "active" for c in caps)
    assert any(c.get("name") == "notification.delayed" and c.get("delivery_mode") == "passive" for c in caps)


def test_event_capability_lookup_has_delivery_mode(tmp_path):
    set_adapter(StandaloneAdapter(home=tmp_path))
    cap_active = get_event_capability("session.reset")
    cap_passive = get_event_capability("notification.delayed")
    assert cap_active is not None
    assert cap_active.get("delivery_mode") == "active"
    assert cap_passive is not None
    assert cap_passive.get("delivery_mode") == "passive"


def test_event_process_delayed_notification_queues_llm_request(tmp_path):
    set_adapter(StandaloneAdapter(home=tmp_path))

    emit_event(
        name="notification.delayed",
        payload={"message": "Please review janitor changes", "kind": "janitor", "priority": "high"},
        source="pytest",
    )

    out = process_events(limit=5)
    assert out["processed"] >= 1
    assert out["failed"] == 0

    requests_path = tmp_path / ".quaid" / "runtime" / "notes" / "delayed-llm-requests.json"
    assert requests_path.exists()
    payload = json.loads(requests_path.read_text(encoding="utf-8"))
    requests = payload.get("requests") or []
    assert any("Please review janitor changes" in str(r.get("message", "")) for r in requests)
