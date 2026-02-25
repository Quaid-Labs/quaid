import json
import sys
import types

from core.runtime.events import emit_event, get_event_registry, get_event_capability, list_events, process_events
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
    assert any(c.get("name") == "session.ingest_log" for c in caps)
    assert any(c.get("name") == "session.reset" and c.get("delivery_mode") == "active" for c in caps)
    assert any(c.get("name") == "notification.delayed" and c.get("delivery_mode") == "passive" for c in caps)


def test_event_capability_lookup_has_delivery_mode(tmp_path):
    set_adapter(StandaloneAdapter(home=tmp_path))
    cap_active = get_event_capability("session.reset")
    cap_passive = get_event_capability("notification.delayed")
    cap_janitor = get_event_capability("janitor.run_completed")
    assert cap_active is not None
    assert cap_active.get("delivery_mode") == "active"
    assert cap_passive is not None
    assert cap_passive.get("delivery_mode") == "passive"
    assert cap_janitor is not None
    assert cap_janitor.get("delivery_mode") == "active"


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


def test_event_process_docs_ingest_transcript(monkeypatch, tmp_path):
    set_adapter(StandaloneAdapter(home=tmp_path))
    transcript = tmp_path / "transcript.txt"
    transcript.write_text("session transcript", encoding="utf-8")

    import core.runtime.events as events
    called = {}

    def _fake_run(path, label, session_id=None):
        called["path"] = str(path)
        called["label"] = label
        called["session_id"] = session_id
        return {"status": "updated", "updatedDocs": 1, "staleDocs": 1}

    fake_docs_ingest = types.SimpleNamespace(_run=_fake_run)
    monkeypatch.setitem(sys.modules, "docs_ingest", fake_docs_ingest)

    emit_event(
        name="docs.ingest_transcript",
        payload={
            "transcript_path": str(transcript),
            "label": "Compaction",
            "session_id": "sess-1",
        },
        source="pytest",
    )

    out = process_events(limit=5, names=["docs.ingest_transcript"])
    assert out["processed"] >= 1
    assert out["failed"] == 0
    assert called["path"] == str(transcript)
    assert called["label"] == "Compaction"
    assert called["session_id"] == "sess-1"


def test_event_process_session_ingest_log(monkeypatch, tmp_path):
    set_adapter(StandaloneAdapter(home=tmp_path))

    import core.runtime.events as events
    called = {}

    def _fake_run(**kwargs):
        called.update(kwargs)
        return {"status": "indexed", "session_id": kwargs["session_id"], "chunks": 2}

    fake_ingest = types.SimpleNamespace(_run=_fake_run)
    monkeypatch.setitem(sys.modules, "session_logs_ingest", fake_ingest)

    emit_event(
        name="session.ingest_log",
        payload={
            "session_id": "sess-xyz",
            "owner_id": "quaid",
            "label": "Compaction",
            "session_file": str(tmp_path / "session.jsonl"),
            "message_count": 12,
            "topic_hint": "tracking session behavior",
        },
        source="pytest",
    )

    out = process_events(limit=5, names=["session.ingest_log"])
    assert out["processed"] >= 1
    assert out["failed"] == 0
    assert called["session_id"] == "sess-xyz"
    assert called["owner_id"] == "quaid"
    assert called["label"] == "Compaction"
    assert called["message_count"] == 12


def test_event_process_janitor_run_completed_queues_notifications(monkeypatch, tmp_path):
    set_adapter(StandaloneAdapter(home=tmp_path))

    class _Notifications:
        def should_notify(self, feature, detail=None):
            return feature == "janitor" and detail == "summary"

    fake_cfg = types.SimpleNamespace(notifications=_Notifications())
    monkeypatch.setattr("config.get_config", lambda: fake_cfg)

    emit_event(
        name="janitor.run_completed",
        payload={
            "metrics": {"total_duration_seconds": 10, "llm_calls": 0, "errors": 0},
            "applied_changes": {"memories_reviewed": 1},
            "today_memories": [{"text": "Test memory", "category": "fact"}],
        },
        source="pytest",
    )

    out = process_events(limit=5, names=["janitor.run_completed"])
    assert out["processed"] >= 1
    assert out["failed"] == 0

    requests_path = tmp_path / ".quaid" / "runtime" / "notes" / "delayed-llm-requests.json"
    payload = json.loads(requests_path.read_text(encoding="utf-8"))
    requests = payload.get("requests") or []
    kinds = [str(r.get("kind", "")) for r in requests]
    assert "janitor_summary" in kinds
    assert "janitor_daily_digest" in kinds
