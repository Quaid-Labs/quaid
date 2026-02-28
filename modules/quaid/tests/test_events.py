import json
import types

import pytest

from core.runtime.events import (
    EVENT_HANDLERS,
    emit_event,
    get_event_capability,
    get_event_registry,
    list_events,
    process_events,
    register_event_handler,
)
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

    monkeypatch.setattr("core.runtime.events.run_docs_ingest", _fake_run)

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

    monkeypatch.setattr("core.runtime.events.run_session_logs_ingest", _fake_run)

    emit_event(
        name="session.ingest_log",
        payload={
            "session_id": "sess-xyz",
            "owner_id": "quaid",
            "label": "Compaction",
            "session_file": str(tmp_path / "session.jsonl"),
            "source_channel": "telegram",
            "conversation_id": "group-1",
            "participant_ids": ["user:owner", "agent:quaid"],
            "participant_aliases": {"<redacted>": "user:owner"},
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
    assert called["source_channel"] == "telegram"
    assert called["conversation_id"] == "group-1"
    assert called["message_count"] == 12


def test_event_process_janitor_run_completed_queues_notifications(monkeypatch, tmp_path):
    set_adapter(StandaloneAdapter(home=tmp_path))

    class _Notifications:
        full_text = False

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


def test_emit_event_caps_queue_length(monkeypatch, tmp_path):
    set_adapter(StandaloneAdapter(home=tmp_path))

    import core.runtime.events as events

    monkeypatch.setattr(events, "MAX_EVENT_QUEUE", 3)
    for i in range(5):
        emit_event(name="session.reset", payload={"idx": i}, source="pytest")

    queue_path = tmp_path / ".quaid" / "runtime" / "events" / "queue.json"
    payload = json.loads(queue_path.read_text(encoding="utf-8"))
    queued = payload.get("events") or []
    assert len(queued) == 3
    assert [int(item.get("payload", {}).get("idx")) for item in queued] == [2, 3, 4]


def test_emit_event_trims_history_file_before_append(monkeypatch, tmp_path):
    set_adapter(StandaloneAdapter(home=tmp_path))

    import core.runtime.events as events

    monkeypatch.setattr(events, "MAX_HISTORY_JSONL_BYTES", 120)
    monkeypatch.setattr(events, "HISTORY_TRIM_TARGET_BYTES", 60)

    history_path = tmp_path / ".quaid" / "runtime" / "events" / "history.jsonl"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    seed = "".join(
        json.dumps({"ts": f"t{i}", "op": "seed", "event": {"id": i}}) + "\n"
        for i in range(12)
    )
    history_path.write_text(seed, encoding="utf-8")

    emit_event(name="session.reset", payload={"reason": "trim-check"}, source="pytest")

    raw = history_path.read_text(encoding="utf-8")
    lines = [line for line in raw.splitlines() if line.strip()]
    assert lines
    assert len(raw.encode("utf-8")) < len(seed.encode("utf-8"))
    last = json.loads(lines[-1])
    assert last.get("op") == "emit"
    assert last.get("event", {}).get("payload", {}).get("reason") == "trim-check"


def test_process_events_handler_error_raises_in_fail_hard(monkeypatch, tmp_path):
    set_adapter(StandaloneAdapter(home=tmp_path))

    import core.runtime.events as events

    transcript = tmp_path / "transcript.txt"
    transcript.write_text("session transcript", encoding="utf-8")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("ingest failed")

    monkeypatch.setattr(events, "run_docs_ingest", _boom)
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: True)

    emit_event(
        name="docs.ingest_transcript",
        payload={"transcript_path": str(transcript), "label": "Compaction"},
        source="pytest",
    )

    with pytest.raises(RuntimeError, match="fail-hard mode"):
        process_events(limit=5, names=["docs.ingest_transcript"])


def test_process_events_handler_error_marks_failed_when_not_fail_hard(monkeypatch, tmp_path):
    set_adapter(StandaloneAdapter(home=tmp_path))

    import core.runtime.events as events

    transcript = tmp_path / "transcript.txt"
    transcript.write_text("session transcript", encoding="utf-8")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("ingest failed")

    monkeypatch.setattr(events, "run_docs_ingest", _boom)
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)

    emit_event(
        name="docs.ingest_transcript",
        payload={"transcript_path": str(transcript), "label": "Compaction"},
        source="pytest",
    )

    out = process_events(limit=5, names=["docs.ingest_transcript"])
    assert out["processed"] == 0
    assert out["failed"] >= 1


def test_emit_event_raises_on_malformed_queue_when_fail_hard(monkeypatch, tmp_path):
    set_adapter(StandaloneAdapter(home=tmp_path))

    import core.runtime.events as events

    queue_path = tmp_path / ".quaid" / "runtime" / "events" / "queue.json"
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.write_text("{bad json", encoding="utf-8")
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="fail-hard mode"):
        emit_event(name="session.reset", payload={"reason": "malformed-queue"}, source="pytest")


def test_emit_event_recovers_on_malformed_queue_when_not_fail_hard(monkeypatch, tmp_path):
    set_adapter(StandaloneAdapter(home=tmp_path))

    import core.runtime.events as events

    queue_path = tmp_path / ".quaid" / "runtime" / "events" / "queue.json"
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.write_text("{bad json", encoding="utf-8")
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)

    event = emit_event(name="session.reset", payload={"reason": "recover-queue"}, source="pytest")
    assert event["name"] == "session.reset"

    payload = json.loads(queue_path.read_text(encoding="utf-8"))
    queued = payload.get("events") or []
    assert len(queued) == 1


def test_emit_event_raises_on_chmod_failure_when_fail_hard(monkeypatch, tmp_path):
    set_adapter(StandaloneAdapter(home=tmp_path))

    import core.runtime.events as events

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(events.os, "chmod", lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("no chmod")))

    with pytest.raises(RuntimeError, match="fail-hard mode"):
        emit_event(name="session.reset", payload={"reason": "chmod-fail"}, source="pytest")


def test_register_event_handler_does_not_overwrite_without_force(caplog):
    original = EVENT_HANDLERS["session.reset"]

    def _replacement(_event):
        return {"status": "processed", "note": "replacement"}

    with caplog.at_level("WARNING"):
        register_event_handler("session.reset", _replacement)
    assert EVENT_HANDLERS["session.reset"] is original
    assert "skipped overwrite" in caplog.text


def test_register_event_handler_overwrites_with_force(caplog):
    original = EVENT_HANDLERS["session.reset"]

    def _replacement(_event):
        return {"status": "processed", "note": "replacement"}

    try:
        with caplog.at_level("WARNING"):
            register_event_handler("session.reset", _replacement, force=True)
        assert EVENT_HANDLERS["session.reset"] is _replacement
        assert "overwriting existing handler" in caplog.text
    finally:
        register_event_handler("session.reset", original, force=True)
