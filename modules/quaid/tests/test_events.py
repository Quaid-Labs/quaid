import json
import types
from pathlib import Path

import pytest

from core.runtime.events import (
    EVENT_HANDLERS,
    dispatch_broker_events,
    emit_broker_event,
    emit_event,
    EVENT_ENVELOPE_SCHEMA_VERSION,
    get_event_capability,
    get_event_registry,
    list_events,
    process_events,
    register_request_handler,
    request_broker_event,
    register_event_handler,
    validate_event_envelope,
    validate_declared_event_contract,
)
from core.runtime.paths import get_runtime_root
from lib.adapter import TestAdapter, reset_adapter, set_adapter


def setup_function():
    reset_adapter()
    import core.runtime.events as events

    with events._REQUEST_EVENT_HANDLERS_LOCK:
        events._REQUEST_EVENT_HANDLERS.clear()


def teardown_function():
    reset_adapter()
    import core.runtime.events as events

    with events._REQUEST_EVENT_HANDLERS_LOCK:
        events._REQUEST_EVENT_HANDLERS.clear()


def test_event_emit_list_and_capabilities(tmp_path):
    adapter = TestAdapter(tmp_path); set_adapter(adapter); iroot = adapter.instance_root()

    event = emit_event(
        name="session.reset",
        payload={"reason": "test"},
        source="pytest",
        session_id="sess-1",
        owner_id="quaid",
    )
    assert event["name"] == "session.reset"
    assert event["event_type"] == "session.reset"
    assert event["event_class"] == "domain"
    assert event["schema_version"] == EVENT_ENVELOPE_SCHEMA_VERSION
    assert event["status"] == "pending"
    assert validate_event_envelope(event) == []

    items = list_events(status="pending", limit=10)
    assert len(items) >= 1
    assert any(e.get("name") == "session.reset" for e in items)

    caps = get_event_registry()
    assert any(c.get("name") == "session.reset" for c in caps)
    assert any(c.get("name") == "notification.delayed" for c in caps)
    assert any(c.get("name") == "session.ingest_log" for c in caps)
    assert any(c.get("name") == "session.reset" and c.get("delivery_mode") == "active" for c in caps)
    assert any(c.get("name") == "notification.delayed" and c.get("delivery_mode") == "passive" for c in caps)


def test_broker_event_request_auto_correlation_and_tracing(tmp_path):
    adapter = TestAdapter(tmp_path); set_adapter(adapter); iroot = adapter.instance_root()

    event = emit_broker_event(
        "recall.memory.request.v1",
        payload={"query": "baratza"},
        source="pytest",
        idempotency_key="request-1",
        provenance={"test": "request-correlation"},
    )

    assert event["name"] == "recall.memory.request.v1"
    assert event["event_type"] == "recall.memory.request.v1"
    assert event["event_class"] == "request"
    assert event["correlation_id"].startswith("corr-")
    assert event["idempotency_key"] == "request-1"
    assert event["provenance"] == {"test": "request-correlation"}
    assert validate_event_envelope(event) == []

    history_path = get_runtime_root(iroot) / "events" / "history.jsonl"
    history = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines()]
    assert any(item.get("op") == "broker.emitted" for item in history)


def test_broker_event_rejects_invalid_envelope_under_fail_hard(monkeypatch, tmp_path):
    set_adapter(TestAdapter(tmp_path))

    import core.runtime.events as events

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="Invalid event envelope"):
        emit_broker_event(
            "recall.memory.request.v1",
            payload={"query": "baratza"},
            source="pytest",
            schema_version=999,
        )


def test_broker_event_logs_and_enqueues_invalid_envelope_when_not_fail_hard(
    caplog,
    monkeypatch,
    tmp_path,
):
    set_adapter(TestAdapter(tmp_path))

    import core.runtime.events as events

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)

    with caplog.at_level("ERROR"):
        event = emit_broker_event(
            "recall.memory.request.v1",
            payload={"query": "baratza"},
            source="pytest",
            schema_version=999,
        )

    assert "Invalid event envelope" in caplog.text
    assert event["validation_errors"] == ["schema_version must be 1"]

    queued = list_events(status="pending", limit=10)
    assert len(queued) == 1
    assert queued[0]["id"] == event["id"]
    assert queued[0]["validation_errors"] == ["schema_version must be 1"]


def test_event_envelope_validation_requires_request_correlation():
    errors = validate_event_envelope(
        {
            "id": "evt-test",
            "name": "recall.memory.request.v1",
            "event_type": "recall.memory.request.v1",
            "event_class": "request",
            "schema_version": EVENT_ENVELOPE_SCHEMA_VERSION,
            "source": "pytest",
            "created_at": "2026-05-15T00:00:00+00:00",
            "payload": {},
            "provenance": {},
            "status": "pending",
        }
    )

    assert "request events require correlation_id" in errors


def test_broker_event_deduplicates_by_idempotency_key(tmp_path):
    adapter = TestAdapter(tmp_path); set_adapter(adapter); iroot = adapter.instance_root()

    first = emit_broker_event(
        "session.reset",
        payload={"idx": 1},
        source="pytest",
        idempotency_key="reset-once",
    )
    second = emit_broker_event(
        "session.reset",
        payload={"idx": 2},
        source="pytest",
        idempotency_key="reset-once",
    )

    assert second["duplicate"] is True
    assert second["duplicate_of"] == first["id"]
    queued = list_events(status="all", limit=10)
    matching = [
        event
        for event in queued
        if event.get("idempotency_key") == "reset-once"
        and event.get("event_type") == "session.reset"
    ]
    assert len(matching) == 1
    assert matching[0]["payload"] == {"idx": 1}


def test_broker_request_fan_in_dispatches_registered_handlers(tmp_path):
    adapter = TestAdapter(tmp_path); set_adapter(adapter); iroot = adapter.instance_root()

    def memory_handler(event):
        return {"status": "ok", "query": event["payload"]["query"], "rows": ["memory-row"]}

    def graph_handler(event):
        return {"status": "acked", "query": event["payload"]["query"], "rows": ["graph-row"]}

    register_request_handler("recall.memory.request.v1", memory_handler, datastore_id="memorydb")
    register_request_handler("recall.memory.request.v1", graph_handler, datastore_id="graph-shadow")

    result = request_broker_event(
        "recall.memory.request.v1",
        {"query": "baratza"},
        source="pytest",
        instance_id="inst-1",
    )

    assert result["status"] == "ok"
    assert result["handler_count"] == 2
    assert result["failed"] == 0
    assert [row["datastore_id"] for row in result["responses"]] == ["memorydb", "graph-shadow"]
    assert result["responses"][0]["result"]["rows"] == ["memory-row"]
    assert result["event"]["event_class"] == "request"
    assert result["event"]["correlation_id"].startswith("corr-")
    assert list_events(status="all", limit=10) == []

    history_path = get_runtime_root(iroot) / "events" / "history.jsonl"
    history = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines()]
    assert any(item.get("op") == "broker.requested" for item in history)
    assert sum(1 for item in history if item.get("op") == "broker.request_acked") == 2


def test_broker_request_missing_handler_fails_closed_when_not_fail_hard(caplog, monkeypatch, tmp_path):
    set_adapter(TestAdapter(tmp_path))

    import core.runtime.events as events

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)

    with caplog.at_level("ERROR"):
        result = request_broker_event("recall.docs.request.v1", {"query": "docs"}, source="pytest")

    assert result["status"] == "failed"
    assert result["handler_count"] == 0
    assert result["failed"] == 1
    assert result["responses"] == []
    assert result["error"] == "No request handler registered for recall.docs.request.v1"
    assert "No request handler registered for recall.docs.request.v1" in caplog.text


def test_broker_request_missing_handler_raises_under_fail_hard(monkeypatch, tmp_path):
    set_adapter(TestAdapter(tmp_path))

    import core.runtime.events as events

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="Request handler missing while fail-hard mode is enabled"):
        request_broker_event("recall.docs.request.v1", {"query": "docs"}, source="pytest")


def test_broker_request_handler_nack_respects_fail_hard(monkeypatch, tmp_path):
    set_adapter(TestAdapter(tmp_path))

    import core.runtime.events as events

    register_request_handler(
        "recall.memory.request.v1",
        lambda _event: {"status": "nacked", "error": "handler not activated"},
        datastore_id="memorydb",
    )

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)
    result = request_broker_event("recall.memory.request.v1", {"query": "baratza"}, source="pytest")

    assert result["status"] == "failed"
    assert result["failed"] == 1
    assert result["responses"][0]["status"] == "nacked"

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: True)
    with pytest.raises(RuntimeError, match="handler not activated"):
        request_broker_event("recall.memory.request.v1", {"query": "baratza"}, source="pytest")


def test_broker_request_handler_malformed_response_fails_closed(monkeypatch, tmp_path):
    set_adapter(TestAdapter(tmp_path))

    import core.runtime.events as events

    register_request_handler(
        "recall.memory.request.v1",
        lambda _event: "bad-response",
        datastore_id="memorydb",
    )

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)
    result = request_broker_event("recall.memory.request.v1", {"query": "baratza"}, source="pytest")

    assert result["status"] == "failed"
    assert result["responses"][0]["result"]["error"] == (
        "recall.memory.request.v1/memorydb returned non-object response"
    )

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: True)
    with pytest.raises(RuntimeError, match="non-object response"):
        request_broker_event("recall.memory.request.v1", {"query": "baratza"}, source="pytest")


def test_broker_dispatch_traces_dispatch_ack_and_failure(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path); set_adapter(adapter); iroot = adapter.instance_root()

    import core.runtime.events as events

    emit_broker_event("session.reset", payload={"idx": 1}, source="pytest")
    dispatch_broker_events(limit=1, names=["session.reset"])

    def _failed(_event):
        return {"status": "failed", "error": "planned failure"}

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)
    original = EVENT_HANDLERS["session.reset"]
    try:
        register_event_handler("session.reset", _failed, force=True)
        emit_broker_event("session.reset", payload={"idx": 2}, source="pytest")
        dispatch_broker_events(limit=1, names=["session.reset"])
    finally:
        register_event_handler("session.reset", original, force=True)

    history_path = get_runtime_root(iroot) / "events" / "history.jsonl"
    history = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines()]
    assert any(item.get("op") == "broker.dispatched" for item in history)
    assert any(item.get("op") == "broker.acked" for item in history)
    assert any(item.get("op") == "broker.failed" for item in history)


def test_event_capability_lookup_has_delivery_mode(tmp_path):
    adapter = TestAdapter(tmp_path); set_adapter(adapter); iroot = adapter.instance_root()
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
    adapter = TestAdapter(tmp_path); set_adapter(adapter); iroot = adapter.instance_root()

    emit_event(
        name="notification.delayed",
        payload={"message": "Please review janitor changes", "kind": "janitor", "priority": "high"},
        source="pytest",
    )

    out = process_events(limit=5)
    assert out["processed"] >= 1
    assert out["failed"] == 0

    requests_path = get_runtime_root(iroot) / "notes" / "delayed-llm-requests.json"
    assert requests_path.exists()
    payload = json.loads(requests_path.read_text(encoding="utf-8"))
    requests = payload.get("requests") or []
    assert any("Please review janitor changes" in str(r.get("message", "")) for r in requests)


def test_event_process_docs_ingest_transcript(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path); set_adapter(adapter); iroot = adapter.instance_root()
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
    set_adapter(TestAdapter(tmp_path))

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
            "participant_aliases": {"operator-alias": "user:owner"},
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
    adapter = TestAdapter(tmp_path); set_adapter(adapter); iroot = adapter.instance_root()

    class _Notifications:
        full_text = False

        def should_notify(self, feature, detail=None):
            return feature == "janitor" and detail in {"summary", "full"}

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

    requests_path = get_runtime_root(iroot) / "notes" / "delayed-llm-requests.json"
    payload = json.loads(requests_path.read_text(encoding="utf-8"))
    requests = payload.get("requests") or []
    kinds = [str(r.get("kind", "")) for r in requests]
    assert "janitor_summary" in kinds
    assert "janitor_daily_digest" in kinds


def test_event_process_janitor_daily_digest_is_independently_gated(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path); set_adapter(adapter); iroot = adapter.instance_root()

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

    requests_path = get_runtime_root(iroot) / "notes" / "delayed-llm-requests.json"
    payload = json.loads(requests_path.read_text(encoding="utf-8"))
    requests = payload.get("requests") or []
    kinds = [str(r.get("kind", "")) for r in requests]
    assert "janitor_summary" in kinds
    assert "janitor_daily_digest" not in kinds


def test_emit_event_caps_queue_length(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path); set_adapter(adapter); iroot = adapter.instance_root()

    import core.runtime.events as events

    monkeypatch.setattr(events, "MAX_EVENT_QUEUE", 3)
    for i in range(5):
        emit_event(name="session.reset", payload={"idx": i}, source="pytest")

    queue_path = get_runtime_root(iroot) / "events" / "queue.json"
    payload = json.loads(queue_path.read_text(encoding="utf-8"))
    queued = payload.get("events") or []
    assert len(queued) == 3
    assert [int(item.get("payload", {}).get("idx")) for item in queued] == [2, 3, 4]


def test_emit_event_trims_history_file_before_append(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path); set_adapter(adapter); iroot = adapter.instance_root()

    import core.runtime.events as events

    monkeypatch.setattr(events, "MAX_HISTORY_JSONL_BYTES", 120)
    monkeypatch.setattr(events, "HISTORY_TRIM_TARGET_BYTES", 60)

    history_path = get_runtime_root(iroot) / "events" / "history.jsonl"
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
    set_adapter(TestAdapter(tmp_path))

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
    set_adapter(TestAdapter(tmp_path))

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
    adapter = TestAdapter(tmp_path); set_adapter(adapter); iroot = adapter.instance_root()

    import core.runtime.events as events

    queue_path = get_runtime_root(iroot) / "events" / "queue.json"
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.write_text("{bad json", encoding="utf-8")
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="fail-hard mode"):
        emit_event(name="session.reset", payload={"reason": "malformed-queue"}, source="pytest")


def test_emit_event_recovers_on_malformed_queue_when_not_fail_hard(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path); set_adapter(adapter); iroot = adapter.instance_root()

    import core.runtime.events as events

    queue_path = get_runtime_root(iroot) / "events" / "queue.json"
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.write_text("{bad json", encoding="utf-8")
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)

    event = emit_event(name="session.reset", payload={"reason": "recover-queue"}, source="pytest")
    assert event["name"] == "session.reset"

    payload = json.loads(queue_path.read_text(encoding="utf-8"))
    queued = payload.get("events") or []
    assert len(queued) == 1


def test_emit_event_raises_on_chmod_failure_when_fail_hard(monkeypatch, tmp_path):
    set_adapter(TestAdapter(tmp_path))

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


def test_validate_declared_event_contract_accepts_openclaw_aliases(monkeypatch):
    def _fake_collect_declared_exports(*, registry, slots, surface, strict=False):
        assert surface == "events"
        assert strict is True
        return {"openclaw.adapter": ["before_reset", "agent_end", "before_compaction", "session_end"]}

    monkeypatch.setattr(
        "core.runtime.plugins.collect_declared_exports",
        _fake_collect_declared_exports,
    )

    errors = validate_declared_event_contract(
        registry=object(),
        slots={"adapter": "openclaw.adapter", "ingest": [], "datastores": []},
        strict=True,
    )
    assert errors == []


def test_validate_declared_event_contract_accepts_openclaw_command_aliases(monkeypatch):
    def _fake_collect_declared_exports(*, registry, slots, surface, strict=False):
        assert surface == "events"
        assert strict is True
        return {"openclaw.adapter": ["command:new", "command:reset", "command:compact"]}

    monkeypatch.setattr(
        "core.runtime.plugins.collect_declared_exports",
        _fake_collect_declared_exports,
    )

    errors = validate_declared_event_contract(
        registry=object(),
        slots={"adapter": "openclaw.adapter", "ingest": [], "datastores": []},
        strict=True,
    )
    assert errors == []


def test_validate_declared_event_contract_accepts_openclaw_native_message_events(monkeypatch):
    def _fake_collect_declared_exports(*, registry, slots, surface, strict=False):
        assert surface == "events"
        assert strict is True
        return {
            "openclaw.adapter": [
                "before_agent_reply",
                "before_prompt_build",
                "message",
                "message_received",
                "message:preprocessed",
            ]
        }

    monkeypatch.setattr(
        "core.runtime.plugins.collect_declared_exports",
        _fake_collect_declared_exports,
    )

    errors = validate_declared_event_contract(
        registry=object(),
        slots={"adapter": "openclaw.adapter", "ingest": [], "datastores": []},
        strict=True,
    )
    assert errors == []


def test_validate_declared_event_contract_accepts_openclaw_manifest_native_reply_hook():
    from core.runtime.plugins import PluginRegistry, validate_manifest_dict

    manifest_path = Path(__file__).resolve().parents[1] / "adaptors" / "openclaw" / "plugin.json"
    manifest = validate_manifest_dict(
        json.loads(manifest_path.read_text(encoding="utf-8")),
        source_path=str(manifest_path),
    )
    registry = PluginRegistry(api_version=manifest.plugin_api_version)
    registry.register(manifest)

    errors = validate_declared_event_contract(
        registry=registry,
        slots={"adapter": "openclaw.adapter", "ingest": [], "datastores": []},
        strict=True,
    )
    assert errors == []


def test_validate_declared_event_contract_rejects_unknown_declared_events(monkeypatch):
    def _fake_collect_declared_exports(*, registry, slots, surface, strict=False):
        assert surface == "events"
        return {"bad.plugin": ["totally.unknown.event"]}

    monkeypatch.setattr(
        "core.runtime.plugins.collect_declared_exports",
        _fake_collect_declared_exports,
    )

    errors = validate_declared_event_contract(
        registry=object(),
        slots={"adapter": "bad.plugin", "ingest": [], "datastores": []},
        strict=False,
    )
    assert len(errors) == 1
    assert "bad.plugin" in errors[0]

    with pytest.raises(ValueError, match="Event contract validation failed"):
        validate_declared_event_contract(
            registry=object(),
            slots={"adapter": "bad.plugin", "ingest": [], "datastores": []},
            strict=True,
        )
