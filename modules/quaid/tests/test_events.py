import json
import inspect
import os
import sys
import types
from pathlib import Path

import pytest

from core.runtime.events import (
    EVENT_HANDLERS,
    DOCS_PROJECT_MAINTENANCE_OBSERVED_EVENT,
    DOCS_PROJECT_UPDATE_REQUEST_EVENT,
    EVOLUTION_JOURNAL_WRITE_REQUEST_EVENT,
    EVOLUTION_SNIPPET_JOURNAL_WRITE_REQUEST_EVENT,
    EVOLUTION_SNIPPET_WRITE_REQUEST_EVENT,
    LIFECYCLE_EVENT_TO_DAEMON_SIGNAL,
    MEMORY_EXTRACTION_PUBLISH_REQUEST_EVENT,
    SESSION_INGEST_LOG_REQUEST_EVENT,
    dispatch_broker_events,
    emit_broker_event,
    emit_event,
    EVENT_ENVELOPE_SCHEMA_VERSION,
    get_event_capability,
    get_event_registry,
    list_events,
    process_events,
    queue_delayed_notification,
    register_request_handler,
    request_broker_event,
    register_event_handler,
    validate_event_envelope,
    validate_declared_event_contract,
)
from core.runtime.paths import get_runtime_root
from lib.adapter import TestAdapter, reset_adapter, set_adapter


_ORIGINAL_EVENT_HANDLERS = dict(EVENT_HANDLERS)


def setup_function():
    reset_adapter()
    import core.runtime.events as events

    with events._EVENT_HANDLERS_LOCK:
        events.EVENT_HANDLERS.clear()
        events.EVENT_HANDLERS.update(_ORIGINAL_EVENT_HANDLERS)
    with events._REQUEST_EVENT_HANDLERS_LOCK:
        events._REQUEST_EVENT_HANDLERS.clear()


def teardown_function():
    reset_adapter()
    import core.runtime.events as events

    with events._EVENT_HANDLERS_LOCK:
        events.EVENT_HANDLERS.clear()
        events.EVENT_HANDLERS.update(_ORIGINAL_EVENT_HANDLERS)
    with events._REQUEST_EVENT_HANDLERS_LOCK:
        events._REQUEST_EVENT_HANDLERS.clear()


def _record_daemon_wake(monkeypatch, *, pid: int = 4242):
    from core import extraction_daemon

    calls = []

    def _ensure_alive():
        calls.append(True)
        return pid

    monkeypatch.setattr(extraction_daemon, "ensure_alive", _ensure_alive)
    return calls


def _fail_on_daemon_wake(monkeypatch):
    from core import extraction_daemon

    monkeypatch.setattr(
        extraction_daemon,
        "ensure_alive",
        lambda: (_ for _ in ()).throw(AssertionError("daemon wake must not be attempted")),
    )


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
    assert event["instance_id"] is None
    assert event["project_id"] is None
    assert event["session_id"] == "sess-1"
    assert event["owner_id"] == "quaid"
    assert event["correlation_id"] is None
    assert event["idempotency_key"] is None
    assert event["duplicate"] is False
    assert event["duplicate_of"] is None
    assert validate_event_envelope(event) == []

    items = list_events(status="pending", limit=10)
    assert len(items) >= 1
    assert any(e.get("name") == "session.reset" for e in items)

    caps = get_event_registry()
    assert any(c.get("name") == "session.reset" for c in caps)
    assert any(c.get("name") == "notification.delayed" for c in caps)
    assert any(c.get("name") == "session.ingest_log" for c in caps)
    assert any(
        c.get("name") == SESSION_INGEST_LOG_REQUEST_EVENT
        and c.get("delivery_mode") == "request"
        and "SessionDB-owned session transcript ingest" in str(c.get("description") or "")
        and "MemoryDB session_chunks projection" in str(c.get("description") or "")
        for c in caps
    )
    assert any(
        c.get("name") == MEMORY_EXTRACTION_PUBLISH_REQUEST_EVENT and c.get("delivery_mode") == "request"
        for c in caps
    )
    assert any(
        c.get("name") == EVOLUTION_SNIPPET_JOURNAL_WRITE_REQUEST_EVENT and c.get("delivery_mode") == "request"
        for c in caps
    )
    assert any(
        c.get("name") == EVOLUTION_SNIPPET_WRITE_REQUEST_EVENT and c.get("delivery_mode") == "request"
        for c in caps
    )
    assert any(
        c.get("name") == EVOLUTION_JOURNAL_WRITE_REQUEST_EVENT and c.get("delivery_mode") == "request"
        for c in caps
    )
    assert any(c.get("name") == "session.reset" and c.get("delivery_mode") == "active" for c in caps)
    assert any(c.get("name") == "notification.delayed" and c.get("delivery_mode") == "passive" for c in caps)


def test_event_timestamps_honor_quaid_now(monkeypatch, tmp_path):
    monkeypatch.setenv("QUAID_NOW", "2026-03-11T00:00:00Z")
    set_adapter(TestAdapter(tmp_path))

    event = emit_event(
        name="session.reset",
        payload={"reason": "benchmark-clock"},
        source="pytest",
    )

    assert event["created_at"] == "2026-03-11T00:00:00+00:00"
    queued = list_events(status="pending", limit=1)
    assert queued[0]["created_at"] == "2026-03-11T00:00:00+00:00"


def test_event_timestamps_reject_malformed_quaid_now(monkeypatch, tmp_path):
    monkeypatch.setenv("QUAID_NOW", "not-a-date")
    set_adapter(TestAdapter(tmp_path))

    with pytest.raises(ValueError, match="Invalid QUAID_NOW"):
        emit_event(
            name="session.reset",
            payload={"reason": "benchmark-clock"},
            source="pytest",
        )

    assert list_events(status="pending", limit=1) == []


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
        return {"status": "ok", "datastore": "memorydb", "target": event["payload"]["target"]}

    def docs_handler(event):
        return {"status": "acked", "datastore": "docsdb", "target": event["payload"]["target"]}

    register_request_handler("datastore.validate.request.v1", memory_handler, datastore_id="memorydb")
    register_request_handler("datastore.validate.request.v1", docs_handler, datastore_id="docsdb")

    result = request_broker_event(
        "datastore.validate.request.v1",
        {"target": "all"},
        source="pytest",
        instance_id="inst-1",
    )

    assert result["status"] == "ok"
    assert result["handler_count"] == 2
    assert result["failed"] == 0
    assert [row["datastore_id"] for row in result["responses"]] == ["memorydb", "docsdb"]
    assert result["responses"][0]["result"]["datastore"] == "memorydb"
    assert result["event"]["event_class"] == "request"
    assert result["event"]["correlation_id"].startswith("corr-")
    assert list_events(status="all", limit=10) == []

    history_path = get_runtime_root(iroot) / "events" / "history.jsonl"
    history = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines()]
    assert any(item.get("op") == "broker.requested" for item in history)
    assert sum(1 for item in history if item.get("op") == "broker.request_acked") == 2


def test_register_request_handler_requires_request_event_type(tmp_path):
    set_adapter(TestAdapter(tmp_path))

    with pytest.raises(ValueError, match="not registered as request"):
        register_request_handler("session.reset", lambda _event: {"status": "ok"}, datastore_id="memorydb")

    with pytest.raises(ValueError, match="not registered as request"):
        register_request_handler("missing.request.v1", lambda _event: {"status": "ok"}, datastore_id="memorydb")


def test_register_request_handler_requires_manifest_declaration(tmp_path):
    set_adapter(TestAdapter(tmp_path))

    with pytest.raises(ValueError, match="request datastore is not registered"):
        register_request_handler("recall.memory.request.v1", lambda _event: {"status": "ok"}, datastore_id="missingdb")

    with pytest.raises(ValueError, match="does not declare request handler"):
        register_request_handler("recall.docs.request.v1", lambda _event: {"status": "ok"}, datastore_id="memorydb")


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


def test_broker_request_failhard_collects_full_fanout_before_raising(monkeypatch, tmp_path):
    set_adapter(TestAdapter(tmp_path))

    import core.runtime.events as events

    calls = []

    register_request_handler(
        "datastore.validate.request.v1",
        lambda _event: calls.append("memorydb") or {"status": "nacked", "error": "memory unavailable"},
        datastore_id="memorydb",
    )
    register_request_handler(
        "datastore.validate.request.v1",
        lambda _event: calls.append("docsdb") or {"status": "ok", "datastore": "docsdb"},
        datastore_id="docsdb",
    )
    monkeypatch.setattr(events, "_request_handler_fail_hard_enabled", lambda _datastore_id: True)

    with pytest.raises(RuntimeError, match="memorydb: memory unavailable"):
        request_broker_event("datastore.validate.request.v1", {"target": "all"}, source="pytest")

    assert calls == ["memorydb", "docsdb"]


def test_broker_request_handler_nack_respects_datastore_fail_hard_policy(monkeypatch, tmp_path):
    set_adapter(TestAdapter(tmp_path))

    import core.datastore_registry as datastore_registry
    import core.runtime.events as events

    manifest = datastore_registry.get_datastore_manifest("memorydb")
    register_request_handler(
        "recall.memory.request.v1",
        lambda _event: {"status": "nacked", "error": "policy failure"},
        datastore_id="memorydb",
    )
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(
        datastore_registry,
        "get_datastore_manifest",
        lambda _datastore_id: dict(manifest, fail_hard_policy="always"),
    )

    with pytest.raises(RuntimeError, match="policy failure"):
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


def test_session_lifecycle_records_sessiondb_observation(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    adapter = TestAdapter(tmp_path); set_adapter(adapter)

    event = emit_event(
        name="session.reset",
        payload={"reason": "operator reset"},
        source="pytest",
        session_id="sess-life",
        owner_id="owner-life",
        idempotency_key="life-reset-1",
    )
    out = process_events(limit=5, names=["session.reset"])

    assert out["processed"] == 1
    result = out["details"][0]["result"]
    assert result["status"] == "acknowledged"
    assert result["event"] == "session.reset"
    assert result["persisted"] is True
    assert result["datastore_id"] == "sessiondb"
    assert result["inserted"] is True

    from datastore.sessiondb.session_store import list_lifecycle_observations

    rows = list_lifecycle_observations(owner_id="owner-life", session_id="sess-life")
    assert len(rows) == 1
    assert rows[0]["event_id"] == event["id"]
    assert rows[0]["idempotency_key"] == "life-reset-1"
    assert rows[0]["event_name"] == "session.reset"
    assert rows[0]["source"] == "pytest"
    assert rows[0]["reason"] == "operator reset"
    assert rows[0]["metadata"]["payload"] == {"reason": "operator reset"}


def test_lifecycle_daemon_signal_mapping_excludes_non_bridge_events():
    assert LIFECYCLE_EVENT_TO_DAEMON_SIGNAL == {
        "session.reset": "reset",
        "session.compaction": "compaction",
        "session.timeout": "timeout",
        "session.agent_end": "session_end",
    }
    assert "session.new" not in LIFECYCLE_EVENT_TO_DAEMON_SIGNAL
    assert "session.agent_start" not in LIFECYCLE_EVENT_TO_DAEMON_SIGNAL
    assert "rolling" not in LIFECYCLE_EVENT_TO_DAEMON_SIGNAL


def test_session_lifecycle_explicit_daemon_signal_writes_existing_signal(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))
    wake_calls = _record_daemon_wake(monkeypatch, pid=4301)

    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")
    emit_event(
        name="session.agent_end",
        payload={
            "reason": "closed",
            "daemon_signal": {
                "enabled": True,
                "transcript_path": str(transcript),
                "reason": "session_closed",
                "source": "pytest-bridge",
                "ignored": "not-propagated",
            },
        },
        source="pytest",
        session_id="sess-bridge",
        owner_id="owner-life",
    )

    out = process_events(limit=5, names=["session.agent_end"])

    assert out["processed"] == 1
    result = out["details"][0]["result"]
    assert result["status"] == "acknowledged"
    assert result["event"] == "session.agent_end"
    assert result["persisted"] is True
    assert result["daemon_signal_queued"] is True
    assert result["daemon_signal_type"] == "session_end"
    assert result["signal_name"].endswith("_session_end.json")
    assert result["daemon_wake_attempted"] is True
    assert result["daemon_wake_succeeded"] is True
    assert result["daemon_wake_pid"] == 4301
    assert wake_calls == [True]

    from core import extraction_daemon

    signals = extraction_daemon.read_pending_signals()
    assert len(signals) == 1
    assert signals[0]["type"] == "session_end"
    assert signals[0]["session_id"] == "sess-bridge"
    assert signals[0]["transcript_path"] == str(transcript)
    assert signals[0]["adapter"] == "pytest"
    assert signals[0]["meta"]["bridge"] == "event_lifecycle_bridge"
    assert signals[0]["meta"]["reason"] == "session_closed"
    assert signals[0]["meta"]["source"] == "pytest-bridge"
    assert signals[0]["meta"]["lifecycle_event_name"] == "session.agent_end"
    assert "ignored" not in signals[0]["meta"]


def test_session_lifecycle_default_reset_signal_writes_existing_signal(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))
    wake_calls = _record_daemon_wake(monkeypatch, pid=4302)

    transcript = tmp_path / "reset-preserved.jsonl"
    transcript.write_text('{"role":"user","content":"pre reset"}\n', encoding="utf-8")
    emit_event(
        name="session.reset",
        payload={
            "reason": "before_reset",
            "reset_transcript_path": str(transcript),
            "reset_transcript_source": "preserved-before-reset",
            "adapter": "openclaw",
            "source": "before-reset",
        },
        source="pytest",
        session_id="sess-reset-default",
        owner_id="owner-life",
    )

    out = process_events(limit=5, names=["session.reset"])

    assert out["processed"] == 1
    result = out["details"][0]["result"]
    assert result["status"] == "acknowledged"
    assert result["event"] == "session.reset"
    assert result["persisted"] is True
    assert result["daemon_signal_queued"] is True
    assert result["daemon_signal_type"] == "reset"
    assert result["daemon_signal_default"] is True
    assert result["signal_name"].endswith("_reset.json")
    assert result["daemon_wake_attempted"] is True
    assert result["daemon_wake_succeeded"] is True
    assert result["daemon_wake_pid"] == 4302
    assert wake_calls == [True]

    from core import extraction_daemon

    signals = extraction_daemon.read_pending_signals()
    assert len(signals) == 1
    assert signals[0]["type"] == "reset"
    assert signals[0]["session_id"] == "sess-reset-default"
    assert signals[0]["transcript_path"] == str(transcript)
    assert signals[0]["adapter"] == "pytest"
    assert signals[0]["meta"]["bridge"] == "event_lifecycle_default_reset_bridge"
    assert signals[0]["meta"]["adapter"] == "openclaw"
    assert signals[0]["meta"]["source"] == "before-reset"
    assert signals[0]["meta"]["reason"] == "before_reset"
    assert signals[0]["meta"]["reset_transcript_source"] == "preserved-before-reset"
    assert signals[0]["meta"]["lifecycle_event_name"] == "session.reset"


def test_session_lifecycle_default_agent_end_signal_writes_existing_signal(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))
    wake_calls = _record_daemon_wake(monkeypatch, pid=4303)

    transcript = tmp_path / "default-session.jsonl"
    transcript.write_text('{"role":"user","content":"default"}\n', encoding="utf-8")
    emit_event(
        name="session.agent_end",
        payload={
            "reason": "closed",
            "transcript_path": str(transcript),
            "adapter": "openclaw",
            "source": "adapter-lifecycle",
        },
        source="pytest",
        session_id="sess-default",
        owner_id="owner-life",
    )

    out = process_events(limit=5, names=["session.agent_end"])

    assert out["processed"] == 1
    result = out["details"][0]["result"]
    assert result["status"] == "acknowledged"
    assert result["event"] == "session.agent_end"
    assert result["persisted"] is True
    assert result["daemon_signal_queued"] is True
    assert result["daemon_signal_type"] == "session_end"
    assert result["daemon_signal_default"] is True
    assert result["signal_name"].endswith("_session_end.json")
    assert result["daemon_wake_attempted"] is True
    assert result["daemon_wake_succeeded"] is True
    assert result["daemon_wake_pid"] == 4303
    assert wake_calls == [True]

    from core import extraction_daemon

    signals = extraction_daemon.read_pending_signals()
    assert len(signals) == 1
    assert signals[0]["type"] == "session_end"
    assert signals[0]["session_id"] == "sess-default"
    assert signals[0]["transcript_path"] == str(transcript)
    assert signals[0]["adapter"] == "pytest"
    assert signals[0]["meta"]["bridge"] == "event_lifecycle_default_bridge"
    assert signals[0]["meta"]["adapter"] == "openclaw"
    assert signals[0]["meta"]["source"] == "adapter-lifecycle"
    assert signals[0]["meta"]["lifecycle_event_name"] == "session.agent_end"


def test_session_lifecycle_default_timeout_signal_writes_existing_signal(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))
    wake_calls = _record_daemon_wake(monkeypatch, pid=4304)

    transcript = tmp_path / "timeout-session.jsonl"
    transcript.write_text('{"role":"user","content":"timeout"}\n', encoding="utf-8")
    emit_event(
        name="session.timeout",
        payload={
            "reason": "idle",
            "transcript_path": str(transcript),
            "adapter": "openclaw",
            "source": "idle-timer",
        },
        source="pytest",
        session_id="sess-timeout-default",
        owner_id="owner-life",
    )

    out = process_events(limit=5, names=["session.timeout"])

    assert out["processed"] == 1
    result = out["details"][0]["result"]
    assert result["status"] == "acknowledged"
    assert result["event"] == "session.timeout"
    assert result["persisted"] is True
    assert result["daemon_signal_queued"] is True
    assert result["daemon_signal_type"] == "timeout"
    assert result["daemon_signal_default"] is True
    assert result["signal_name"].endswith("_timeout.json")
    assert result["daemon_wake_attempted"] is True
    assert result["daemon_wake_succeeded"] is True
    assert result["daemon_wake_pid"] == 4304
    assert wake_calls == [True]

    from core import extraction_daemon

    signals = extraction_daemon.read_pending_signals()
    assert len(signals) == 1
    assert signals[0]["type"] == "timeout"
    assert signals[0]["session_id"] == "sess-timeout-default"
    assert signals[0]["transcript_path"] == str(transcript)
    assert signals[0]["adapter"] == "pytest"
    assert signals[0]["meta"]["bridge"] == "event_lifecycle_default_timeout_bridge"
    assert signals[0]["meta"]["adapter"] == "openclaw"
    assert signals[0]["meta"]["source"] == "idle-timer"
    assert signals[0]["meta"]["lifecycle_event_name"] == "session.timeout"


def test_session_lifecycle_default_compaction_signal_writes_existing_signal(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))
    wake_calls = _record_daemon_wake(monkeypatch, pid=4305)

    transcript = tmp_path / "compaction-session.jsonl"
    transcript.write_text('{"role":"user","content":"compact"}\n', encoding="utf-8")
    emit_event(
        name="session.compaction",
        payload={
            "reason": "before_compaction",
            "transcript_path": str(transcript),
            "adapter": "openclaw",
            "source": "before-compaction",
            "supports_compaction_control": True,
        },
        source="pytest",
        session_id="sess-compaction-default",
        owner_id="owner-life",
    )

    out = process_events(limit=5, names=["session.compaction"])

    assert out["processed"] == 1
    result = out["details"][0]["result"]
    assert result["status"] == "acknowledged"
    assert result["event"] == "session.compaction"
    assert result["persisted"] is True
    assert result["daemon_signal_queued"] is True
    assert result["daemon_signal_type"] == "compaction"
    assert result["daemon_signal_default"] is True
    assert result["signal_name"].endswith("_compaction.json")
    assert result["daemon_wake_attempted"] is True
    assert result["daemon_wake_succeeded"] is True
    assert result["daemon_wake_pid"] == 4305
    assert wake_calls == [True]

    from core import extraction_daemon

    signals = extraction_daemon.read_pending_signals()
    assert len(signals) == 1
    assert signals[0]["type"] == "compaction"
    assert signals[0]["session_id"] == "sess-compaction-default"
    assert signals[0]["transcript_path"] == str(transcript)
    assert signals[0]["adapter"] == "pytest"
    assert signals[0]["supports_compaction_control"] is True
    assert signals[0]["meta"]["bridge"] == "event_lifecycle_default_compaction_bridge"
    assert signals[0]["meta"]["adapter"] == "openclaw"
    assert signals[0]["meta"]["source"] == "before-compaction"
    assert signals[0]["meta"]["lifecycle_event_name"] == "session.compaction"


def test_session_lifecycle_without_daemon_signal_does_not_call_write_signal(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))

    from core import extraction_daemon

    monkeypatch.setattr(
        extraction_daemon,
        "write_signal",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("plain lifecycle must not signal daemon")),
    )
    _fail_on_daemon_wake(monkeypatch)

    emit_event(name="session.reset", payload={"reason": "plain"}, source="pytest", session_id="sess-plain")
    out = process_events(limit=5, names=["session.reset"])

    result = out["details"][0]["result"]
    assert result["status"] == "acknowledged"
    assert result["persisted"] is True
    assert "daemon_signal_queued" not in result
    assert "daemon_signal_type" not in result
    assert "daemon_signal_error" not in result
    assert "signal_name" not in result
    assert extraction_daemon.read_pending_signals() == []


@pytest.mark.parametrize(
    "event_name",
    ["session.new", "session.agent_start"],
)
def test_session_lifecycle_default_signal_excludes_unselected_events(monkeypatch, tmp_path, event_name):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))

    from core import extraction_daemon

    monkeypatch.setattr(
        extraction_daemon,
        "write_signal",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("unselected event must not signal daemon")),
    )
    _fail_on_daemon_wake(monkeypatch)
    transcript = tmp_path / f"{event_name}.jsonl"
    transcript.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")

    emit_event(
        name=event_name,
        payload={"reason": "plain", "transcript_path": str(transcript), "reset_transcript_path": str(transcript)},
        source="pytest",
        session_id=event_name.replace(".", "-"),
        owner_id="owner-life",
    )
    out = process_events(limit=5, names=[event_name])

    result = out["details"][0]["result"]
    assert result["status"] == "acknowledged"
    assert result["persisted"] is True
    assert "daemon_signal_queued" not in result
    assert "daemon_signal_default" not in result
    assert "signal_name" not in result
    assert extraction_daemon.read_pending_signals() == []


def test_session_lifecycle_default_reset_signal_ignores_live_transcript_path(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))

    from core import extraction_daemon

    monkeypatch.setattr(
        extraction_daemon,
        "write_signal",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("live reset transcript_path must not signal daemon")),
    )
    _fail_on_daemon_wake(monkeypatch)
    transcript = tmp_path / "live-reset-session.jsonl"
    transcript.write_text('{"role":"user","content":"post reset"}\n', encoding="utf-8")

    emit_event(
        name="session.reset",
        payload={"reason": "plain", "transcript_path": str(transcript)},
        source="pytest",
        session_id="sess-reset-live-path",
        owner_id="owner-life",
    )
    out = process_events(limit=5, names=["session.reset"])

    result = out["details"][0]["result"]
    assert result["status"] == "acknowledged"
    assert result["persisted"] is True
    assert "daemon_signal_queued" not in result
    assert "daemon_signal_default" not in result
    assert "signal_name" not in result
    assert extraction_daemon.read_pending_signals() == []


@pytest.mark.parametrize(
    ("payload", "session_id", "expected_persisted"),
    [
        ({}, "sess-reset-no-path", True),
        ({"reset_transcript_path": ""}, "sess-reset-empty-path", True),
        ({"reset_transcript_path": "missing.jsonl"}, "sess-reset-missing-path", True),
        ({"reset_transcript_path": "session.jsonl"}, "", False),
    ],
)
def test_session_lifecycle_default_reset_signal_noop_without_valid_inputs(
    monkeypatch,
    tmp_path,
    payload,
    session_id,
    expected_persisted,
):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))

    from core import extraction_daemon

    monkeypatch.setattr(
        extraction_daemon,
        "write_signal",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("invalid default reset inputs must not signal daemon")),
    )
    _fail_on_daemon_wake(monkeypatch)
    if payload.get("reset_transcript_path") == "session.jsonl":
        transcript = tmp_path / "session.jsonl"
        transcript.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")
        payload = {**payload, "reset_transcript_path": str(transcript)}
    elif payload.get("reset_transcript_path") == "missing.jsonl":
        payload = {**payload, "reset_transcript_path": str(tmp_path / "missing.jsonl")}

    emit_event(
        name="session.reset",
        payload=payload,
        source="pytest",
        session_id=session_id or None,
        owner_id="owner-life",
    )
    out = process_events(limit=5, names=["session.reset"])

    result = out["details"][0]["result"]
    assert result["status"] == "acknowledged"
    assert result["event"] == "session.reset"
    assert result["persisted"] is expected_persisted
    assert "daemon_signal_queued" not in result
    assert "daemon_signal_default" not in result
    assert "daemon_signal_error" not in result
    assert "signal_name" not in result
    assert extraction_daemon.read_pending_signals() == []


@pytest.mark.parametrize(
    ("payload", "session_id", "expected_persisted"),
    [
        ({}, "sess-agent-end-no-path", True),
        ({"transcript_path": ""}, "sess-agent-end-empty-path", True),
        ({"transcript_path": "missing.jsonl"}, "sess-agent-end-missing-path", True),
        ({"transcript_path": "session.jsonl"}, "", False),
    ],
)
def test_session_lifecycle_default_agent_end_signal_noop_without_valid_inputs(
    monkeypatch,
    tmp_path,
    payload,
    session_id,
    expected_persisted,
):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))

    from core import extraction_daemon

    monkeypatch.setattr(
        extraction_daemon,
        "write_signal",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("invalid default inputs must not signal daemon")),
    )
    _fail_on_daemon_wake(monkeypatch)
    if payload.get("transcript_path") == "session.jsonl":
        transcript = tmp_path / "session.jsonl"
        transcript.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")
        payload = {**payload, "transcript_path": str(transcript)}
    elif payload.get("transcript_path") == "missing.jsonl":
        payload = {**payload, "transcript_path": str(tmp_path / "missing.jsonl")}

    emit_event(
        name="session.agent_end",
        payload=payload,
        source="pytest",
        session_id=session_id or None,
        owner_id="owner-life",
    )
    out = process_events(limit=5, names=["session.agent_end"])

    result = out["details"][0]["result"]
    assert result["status"] == "acknowledged"
    assert result["event"] == "session.agent_end"
    assert result["persisted"] is expected_persisted
    assert "daemon_signal_queued" not in result
    assert "daemon_signal_default" not in result
    assert "daemon_signal_error" not in result
    assert "signal_name" not in result
    assert extraction_daemon.read_pending_signals() == []


@pytest.mark.parametrize(
    ("payload", "session_id", "expected_persisted"),
    [
        ({}, "sess-timeout-no-path", True),
        ({"transcript_path": ""}, "sess-timeout-empty-path", True),
        ({"transcript_path": "missing.jsonl"}, "sess-timeout-missing-path", True),
        ({"transcript_path": "session.jsonl"}, "", False),
    ],
)
def test_session_lifecycle_default_timeout_signal_noop_without_valid_inputs(
    monkeypatch,
    tmp_path,
    payload,
    session_id,
    expected_persisted,
):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))

    from core import extraction_daemon

    monkeypatch.setattr(
        extraction_daemon,
        "write_signal",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("invalid default inputs must not signal daemon")),
    )
    _fail_on_daemon_wake(monkeypatch)
    if payload.get("transcript_path") == "session.jsonl":
        transcript = tmp_path / "session.jsonl"
        transcript.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")
        payload = {**payload, "transcript_path": str(transcript)}
    elif payload.get("transcript_path") == "missing.jsonl":
        payload = {**payload, "transcript_path": str(tmp_path / "missing.jsonl")}

    emit_event(
        name="session.timeout",
        payload=payload,
        source="pytest",
        session_id=session_id or None,
        owner_id="owner-life",
    )
    out = process_events(limit=5, names=["session.timeout"])

    result = out["details"][0]["result"]
    assert result["status"] == "acknowledged"
    assert result["event"] == "session.timeout"
    assert result["persisted"] is expected_persisted
    assert "daemon_signal_queued" not in result
    assert "daemon_signal_default" not in result
    assert "daemon_signal_error" not in result
    assert "signal_name" not in result
    assert extraction_daemon.read_pending_signals() == []


@pytest.mark.parametrize(
    ("payload", "session_id", "expected_persisted"),
    [
        ({}, "sess-compaction-no-path", True),
        ({"transcript_path": ""}, "sess-compaction-empty-path", True),
        ({"transcript_path": "missing.jsonl"}, "sess-compaction-missing-path", True),
        ({"transcript_path": "session.jsonl"}, "", False),
    ],
)
def test_session_lifecycle_default_compaction_signal_noop_without_valid_inputs(
    monkeypatch,
    tmp_path,
    payload,
    session_id,
    expected_persisted,
):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))

    from core import extraction_daemon

    monkeypatch.setattr(
        extraction_daemon,
        "write_signal",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("invalid default inputs must not signal daemon")),
    )
    _fail_on_daemon_wake(monkeypatch)
    if payload.get("transcript_path") == "session.jsonl":
        transcript = tmp_path / "session.jsonl"
        transcript.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")
        payload = {**payload, "transcript_path": str(transcript)}
    elif payload.get("transcript_path") == "missing.jsonl":
        payload = {**payload, "transcript_path": str(tmp_path / "missing.jsonl")}

    emit_event(
        name="session.compaction",
        payload=payload,
        source="pytest",
        session_id=session_id or None,
        owner_id="owner-life",
    )
    out = process_events(limit=5, names=["session.compaction"])

    result = out["details"][0]["result"]
    assert result["status"] == "acknowledged"
    assert result["event"] == "session.compaction"
    assert result["persisted"] is expected_persisted
    assert "daemon_signal_queued" not in result
    assert "daemon_signal_default" not in result
    assert "daemon_signal_error" not in result
    assert "signal_name" not in result
    assert extraction_daemon.read_pending_signals() == []


@pytest.mark.parametrize("event_name", ["session.new", "session.agent_start"])
def test_session_lifecycle_excluded_events_do_not_queue_daemon_signal(monkeypatch, tmp_path, event_name):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))
    _fail_on_daemon_wake(monkeypatch)

    transcript = tmp_path / f"{event_name}.jsonl"
    transcript.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")
    emit_event(
        name=event_name,
        payload={"daemon_signal": {"enabled": True, "transcript_path": str(transcript)}},
        source="pytest",
        session_id=event_name.replace(".", "-"),
        owner_id="owner-life",
    )
    out = process_events(limit=5, names=[event_name])

    from core import extraction_daemon

    result = out["details"][0]["result"]
    assert result["status"] == "acknowledged"
    assert result["persisted"] is True
    assert "daemon_signal_queued" not in result
    assert "signal_name" not in result
    assert extraction_daemon.read_pending_signals() == []


def test_session_lifecycle_explicit_daemon_signal_wins_over_default_reset(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))
    wake_calls = _record_daemon_wake(monkeypatch)

    default_transcript = tmp_path / "default-reset.jsonl"
    explicit_transcript = tmp_path / "explicit-reset.jsonl"
    default_transcript.write_text('{"role":"user","content":"default"}\n', encoding="utf-8")
    explicit_transcript.write_text('{"role":"user","content":"explicit"}\n', encoding="utf-8")

    emit_event(
        name="session.reset",
        payload={
            "reset_transcript_path": str(default_transcript),
            "daemon_signal": {
                "enabled": True,
                "transcript_path": str(explicit_transcript),
                "reason": "explicit-reset",
            },
        },
        source="pytest",
        session_id="sess-reset-explicit-wins",
        owner_id="owner-life",
    )
    out = process_events(limit=5, names=["session.reset"])

    from core import extraction_daemon

    result = out["details"][0]["result"]
    assert result["daemon_signal_queued"] is True
    assert result["daemon_signal_type"] == "reset"
    assert "daemon_signal_default" not in result
    assert result["daemon_wake_attempted"] is True
    assert result["daemon_wake_succeeded"] is True
    assert wake_calls == [True]
    signals = extraction_daemon.read_pending_signals()
    assert len(signals) == 1
    assert signals[0]["transcript_path"] == str(explicit_transcript)
    assert signals[0]["meta"]["bridge"] == "event_lifecycle_bridge"
    assert signals[0]["meta"]["reason"] == "explicit-reset"


def test_session_lifecycle_explicit_daemon_signal_wins_over_default_agent_end(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))
    wake_calls = _record_daemon_wake(monkeypatch)

    default_transcript = tmp_path / "default.jsonl"
    explicit_transcript = tmp_path / "explicit.jsonl"
    default_transcript.write_text('{"role":"user","content":"default"}\n', encoding="utf-8")
    explicit_transcript.write_text('{"role":"user","content":"explicit"}\n', encoding="utf-8")

    emit_event(
        name="session.agent_end",
        payload={
            "transcript_path": str(default_transcript),
            "daemon_signal": {
                "enabled": True,
                "transcript_path": str(explicit_transcript),
                "reason": "explicit",
            },
        },
        source="pytest",
        session_id="sess-explicit-wins",
        owner_id="owner-life",
    )
    out = process_events(limit=5, names=["session.agent_end"])

    from core import extraction_daemon

    result = out["details"][0]["result"]
    assert result["daemon_signal_queued"] is True
    assert result["daemon_signal_type"] == "session_end"
    assert "daemon_signal_default" not in result
    assert result["daemon_wake_attempted"] is True
    assert result["daemon_wake_succeeded"] is True
    assert wake_calls == [True]
    signals = extraction_daemon.read_pending_signals()
    assert len(signals) == 1
    assert signals[0]["transcript_path"] == str(explicit_transcript)
    assert signals[0]["meta"]["bridge"] == "event_lifecycle_bridge"
    assert signals[0]["meta"]["reason"] == "explicit"


def test_session_lifecycle_explicit_daemon_signal_wins_over_default_timeout(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))
    wake_calls = _record_daemon_wake(monkeypatch)

    default_transcript = tmp_path / "default-timeout.jsonl"
    explicit_transcript = tmp_path / "explicit-timeout.jsonl"
    default_transcript.write_text('{"role":"user","content":"default"}\n', encoding="utf-8")
    explicit_transcript.write_text('{"role":"user","content":"explicit"}\n', encoding="utf-8")

    emit_event(
        name="session.timeout",
        payload={
            "transcript_path": str(default_transcript),
            "daemon_signal": {
                "enabled": True,
                "transcript_path": str(explicit_transcript),
                "reason": "explicit-timeout",
            },
        },
        source="pytest",
        session_id="sess-timeout-explicit-wins",
        owner_id="owner-life",
    )
    out = process_events(limit=5, names=["session.timeout"])

    from core import extraction_daemon

    result = out["details"][0]["result"]
    assert result["daemon_signal_queued"] is True
    assert result["daemon_signal_type"] == "timeout"
    assert "daemon_signal_default" not in result
    assert result["daemon_wake_attempted"] is True
    assert result["daemon_wake_succeeded"] is True
    assert wake_calls == [True]
    signals = extraction_daemon.read_pending_signals()
    assert len(signals) == 1
    assert signals[0]["transcript_path"] == str(explicit_transcript)
    assert signals[0]["meta"]["bridge"] == "event_lifecycle_bridge"
    assert signals[0]["meta"]["reason"] == "explicit-timeout"


def test_session_lifecycle_explicit_daemon_signal_wins_over_default_compaction(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))
    wake_calls = _record_daemon_wake(monkeypatch)

    default_transcript = tmp_path / "default-compaction.jsonl"
    explicit_transcript = tmp_path / "explicit-compaction.jsonl"
    default_transcript.write_text('{"role":"user","content":"default"}\n', encoding="utf-8")
    explicit_transcript.write_text('{"role":"user","content":"explicit"}\n', encoding="utf-8")

    emit_event(
        name="session.compaction",
        payload={
            "transcript_path": str(default_transcript),
            "supports_compaction_control": True,
            "daemon_signal": {
                "enabled": True,
                "transcript_path": str(explicit_transcript),
                "reason": "explicit-compaction",
            },
        },
        source="pytest",
        session_id="sess-compaction-explicit-wins",
        owner_id="owner-life",
    )
    out = process_events(limit=5, names=["session.compaction"])

    from core import extraction_daemon

    result = out["details"][0]["result"]
    assert result["daemon_signal_queued"] is True
    assert result["daemon_signal_type"] == "compaction"
    assert "daemon_signal_default" not in result
    assert result["daemon_wake_attempted"] is True
    assert result["daemon_wake_succeeded"] is True
    assert wake_calls == [True]
    signals = extraction_daemon.read_pending_signals()
    assert len(signals) == 1
    assert signals[0]["transcript_path"] == str(explicit_transcript)
    assert signals[0]["supports_compaction_control"] is False
    assert signals[0]["meta"]["bridge"] == "event_lifecycle_bridge"
    assert signals[0]["meta"]["reason"] == "explicit-compaction"


@pytest.mark.parametrize(
    ("payload_value", "expected_supports_compaction_control"),
    [
        (True, True),
        (False, False),
        ("true", False),
        (None, False),
    ],
)
def test_session_lifecycle_default_compaction_supports_control_is_explicit_boolean(
    monkeypatch,
    tmp_path,
    payload_value,
    expected_supports_compaction_control,
):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))
    wake_calls = _record_daemon_wake(monkeypatch)

    from core import extraction_daemon

    captured = {}

    def _fake_write_signal(**kwargs):
        captured.update(kwargs)
        return tmp_path / "fake_compaction.json"

    monkeypatch.setattr(extraction_daemon, "write_signal", _fake_write_signal)
    transcript = tmp_path / "compaction-support.jsonl"
    transcript.write_text('{"role":"user","content":"compact"}\n', encoding="utf-8")
    payload = {"transcript_path": str(transcript)}
    if payload_value is not None:
        payload["supports_compaction_control"] = payload_value

    emit_event(
        name="session.compaction",
        payload=payload,
        source="pytest",
        session_id="sess-compaction-support",
        owner_id="owner-life",
    )
    out = process_events(limit=5, names=["session.compaction"])

    result = out["details"][0]["result"]
    assert result["daemon_signal_queued"] is True
    assert result["daemon_wake_succeeded"] is True
    assert wake_calls == [True]
    assert captured["signal_type"] == "compaction"
    assert captured["supports_compaction_control"] is expected_supports_compaction_control


def test_session_lifecycle_daemon_signal_failures_respect_failhard(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))

    import core.runtime.events as events

    missing = tmp_path / "missing.jsonl"
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)
    with caplog.at_level("WARNING"):
        emit_event(
            name="session.compaction",
            payload={"daemon_signal": {"enabled": True, "transcript_path": str(missing)}},
            source="pytest",
            session_id="sess-soft-signal",
            owner_id="owner-life",
        )
        soft = process_events(limit=5, names=["session.compaction"])

    assert soft["processed"] == 1
    soft_result = soft["details"][0]["result"]
    assert soft_result["status"] == "acknowledged"
    assert soft_result["persisted"] is True
    assert soft_result["daemon_signal_queued"] is False
    assert str(missing) in soft_result["daemon_signal_error"]
    assert any("Lifecycle daemon signal bridge failed" in rec.message for rec in caplog.records)

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: True)
    emit_event(
        name="session.compaction",
        payload={"daemon_signal": {"enabled": True, "transcript_path": str(missing)}},
        source="pytest",
        session_id="sess-hard-signal",
        owner_id="owner-life",
    )
    with pytest.raises(RuntimeError, match="Event handler failed while fail-hard mode is enabled") as excinfo:
        process_events(limit=5, names=["session.compaction"])
    assert isinstance(excinfo.value.__cause__, FileNotFoundError)
    assert str(excinfo.value.__cause__) == str(missing)


def test_session_lifecycle_daemon_signal_missing_session_id_is_selected_failure(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))

    import core.runtime.events as events

    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)
    with caplog.at_level("WARNING"):
        emit_event(
            name="session.timeout",
            payload={"daemon_signal": {"enabled": True, "transcript_path": str(transcript)}},
            source="pytest",
        )
        soft = process_events(limit=5, names=["session.timeout"])

    soft_result = soft["details"][0]["result"]
    assert soft_result["persisted"] is False
    assert soft_result["daemon_signal_queued"] is False
    assert "session_id is required" in soft_result["daemon_signal_error"]
    assert any("Lifecycle daemon signal bridge failed" in rec.message for rec in caplog.records)

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: True)
    emit_event(
        name="session.timeout",
        payload={"daemon_signal": {"enabled": True, "transcript_path": str(transcript)}},
        source="pytest",
    )
    with pytest.raises(RuntimeError, match="Event handler failed while fail-hard mode is enabled") as excinfo:
        process_events(limit=5, names=["session.timeout"])
    assert isinstance(excinfo.value.__cause__, ValueError)
    assert str(excinfo.value.__cause__) == "payload.session_id is required for daemon_signal bridge"


def test_session_lifecycle_daemon_signal_write_failure_respects_failhard(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))

    from core import extraction_daemon
    import core.runtime.events as events

    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")

    def _boom(**_kwargs):
        raise RuntimeError("write_signal down")

    monkeypatch.setattr(extraction_daemon, "write_signal", _boom)
    _fail_on_daemon_wake(monkeypatch)
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)
    with caplog.at_level("WARNING"):
        emit_event(
            name="session.reset",
            payload={"daemon_signal": {"enabled": True, "transcript_path": str(transcript)}},
            source="pytest",
            session_id="sess-soft-write",
            owner_id="owner-life",
        )
        soft = process_events(limit=5, names=["session.reset"])

    soft_result = soft["details"][0]["result"]
    assert soft_result["status"] == "acknowledged"
    assert soft_result["persisted"] is True
    assert soft_result["daemon_signal_queued"] is False
    assert soft_result["daemon_signal_error"] == "write_signal down"
    assert any("Lifecycle daemon signal bridge failed" in rec.message for rec in caplog.records)

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: True)
    emit_event(
        name="session.reset",
        payload={"daemon_signal": {"enabled": True, "transcript_path": str(transcript)}},
        source="pytest",
        session_id="sess-hard-write",
        owner_id="owner-life",
    )
    with pytest.raises(RuntimeError, match="Event handler failed while fail-hard mode is enabled") as excinfo:
        process_events(limit=5, names=["session.reset"])
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert str(excinfo.value.__cause__) == "write_signal down"


def test_session_lifecycle_default_reset_write_failure_respects_failhard(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))

    from core import extraction_daemon
    import core.runtime.events as events

    transcript = tmp_path / "reset-preserved.jsonl"
    transcript.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")

    def _boom(**_kwargs):
        raise RuntimeError("default reset write_signal down")

    monkeypatch.setattr(extraction_daemon, "write_signal", _boom)
    _fail_on_daemon_wake(monkeypatch)
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)
    with caplog.at_level("WARNING"):
        emit_event(
            name="session.reset",
            payload={"reset_transcript_path": str(transcript)},
            source="pytest",
            session_id="sess-reset-soft-write",
            owner_id="owner-life",
        )
        soft = process_events(limit=5, names=["session.reset"])

    soft_result = soft["details"][0]["result"]
    assert soft_result["status"] == "acknowledged"
    assert soft_result["persisted"] is True
    assert soft_result["daemon_signal_queued"] is False
    assert soft_result["daemon_signal_default"] is True
    assert soft_result["daemon_signal_error"] == "default reset write_signal down"
    assert any("Lifecycle daemon signal bridge failed" in rec.message for rec in caplog.records)

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: True)
    emit_event(
        name="session.reset",
        payload={"reset_transcript_path": str(transcript)},
        source="pytest",
        session_id="sess-reset-hard-write",
        owner_id="owner-life",
    )
    with pytest.raises(RuntimeError, match="Event handler failed while fail-hard mode is enabled") as excinfo:
        process_events(limit=5, names=["session.reset"])
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert str(excinfo.value.__cause__) == "default reset write_signal down"


def test_session_lifecycle_default_agent_end_write_failure_respects_failhard(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))

    from core import extraction_daemon
    import core.runtime.events as events

    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")

    def _boom(**_kwargs):
        raise RuntimeError("default write_signal down")

    monkeypatch.setattr(extraction_daemon, "write_signal", _boom)
    _fail_on_daemon_wake(monkeypatch)
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)
    with caplog.at_level("WARNING"):
        emit_event(
            name="session.agent_end",
            payload={"transcript_path": str(transcript)},
            source="pytest",
            session_id="sess-default-soft-write",
            owner_id="owner-life",
        )
        soft = process_events(limit=5, names=["session.agent_end"])

    soft_result = soft["details"][0]["result"]
    assert soft_result["status"] == "acknowledged"
    assert soft_result["persisted"] is True
    assert soft_result["daemon_signal_queued"] is False
    assert soft_result["daemon_signal_default"] is True
    assert soft_result["daemon_signal_error"] == "default write_signal down"
    assert any("Lifecycle daemon signal bridge failed" in rec.message for rec in caplog.records)

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: True)
    emit_event(
        name="session.agent_end",
        payload={"transcript_path": str(transcript)},
        source="pytest",
        session_id="sess-default-hard-write",
        owner_id="owner-life",
    )
    with pytest.raises(RuntimeError, match="Event handler failed while fail-hard mode is enabled") as excinfo:
        process_events(limit=5, names=["session.agent_end"])
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert str(excinfo.value.__cause__) == "default write_signal down"


def test_session_lifecycle_default_timeout_write_failure_respects_failhard(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))

    from core import extraction_daemon
    import core.runtime.events as events

    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")

    def _boom(**_kwargs):
        raise RuntimeError("default timeout write_signal down")

    monkeypatch.setattr(extraction_daemon, "write_signal", _boom)
    _fail_on_daemon_wake(monkeypatch)
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)
    with caplog.at_level("WARNING"):
        emit_event(
            name="session.timeout",
            payload={"transcript_path": str(transcript)},
            source="pytest",
            session_id="sess-timeout-soft-write",
            owner_id="owner-life",
        )
        soft = process_events(limit=5, names=["session.timeout"])

    soft_result = soft["details"][0]["result"]
    assert soft_result["status"] == "acknowledged"
    assert soft_result["persisted"] is True
    assert soft_result["daemon_signal_queued"] is False
    assert soft_result["daemon_signal_default"] is True
    assert soft_result["daemon_signal_error"] == "default timeout write_signal down"
    assert any("Lifecycle daemon signal bridge failed" in rec.message for rec in caplog.records)

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: True)
    emit_event(
        name="session.timeout",
        payload={"transcript_path": str(transcript)},
        source="pytest",
        session_id="sess-timeout-hard-write",
        owner_id="owner-life",
    )
    with pytest.raises(RuntimeError, match="Event handler failed while fail-hard mode is enabled") as excinfo:
        process_events(limit=5, names=["session.timeout"])
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert str(excinfo.value.__cause__) == "default timeout write_signal down"


def test_session_lifecycle_default_compaction_write_failure_respects_failhard(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))

    from core import extraction_daemon
    import core.runtime.events as events

    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")

    def _boom(**_kwargs):
        raise RuntimeError("default compaction write_signal down")

    monkeypatch.setattr(extraction_daemon, "write_signal", _boom)
    _fail_on_daemon_wake(monkeypatch)
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)
    with caplog.at_level("WARNING"):
        emit_event(
            name="session.compaction",
            payload={"transcript_path": str(transcript)},
            source="pytest",
            session_id="sess-compaction-soft-write",
            owner_id="owner-life",
        )
        soft = process_events(limit=5, names=["session.compaction"])

    soft_result = soft["details"][0]["result"]
    assert soft_result["status"] == "acknowledged"
    assert soft_result["persisted"] is True
    assert soft_result["daemon_signal_queued"] is False
    assert soft_result["daemon_signal_default"] is True
    assert soft_result["daemon_signal_error"] == "default compaction write_signal down"
    assert any("Lifecycle daemon signal bridge failed" in rec.message for rec in caplog.records)

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: True)
    emit_event(
        name="session.compaction",
        payload={"transcript_path": str(transcript)},
        source="pytest",
        session_id="sess-compaction-hard-write",
        owner_id="owner-life",
    )
    with pytest.raises(RuntimeError, match="Event handler failed while fail-hard mode is enabled") as excinfo:
        process_events(limit=5, names=["session.compaction"])
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert str(excinfo.value.__cause__) == "default compaction write_signal down"


def test_session_lifecycle_daemon_wake_failure_respects_failhard(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))

    from core import extraction_daemon
    import core.runtime.events as events

    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")

    def _wake_down():
        raise RuntimeError("daemon wake down")

    monkeypatch.setattr(extraction_daemon, "ensure_alive", _wake_down)
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)
    with caplog.at_level("WARNING"):
        emit_event(
            name="session.agent_end",
            payload={"transcript_path": str(transcript)},
            source="pytest",
            session_id="sess-wake-soft",
            owner_id="owner-life",
        )
        soft = process_events(limit=5, names=["session.agent_end"])

    soft_result = soft["details"][0]["result"]
    assert soft_result["status"] == "acknowledged"
    assert soft_result["persisted"] is True
    assert soft_result["daemon_signal_queued"] is True
    assert soft_result["daemon_signal_type"] == "session_end"
    assert soft_result["daemon_signal_default"] is True
    assert soft_result["daemon_wake_attempted"] is True
    assert soft_result["daemon_wake_succeeded"] is False
    assert soft_result["daemon_wake_error"] == "daemon wake down"
    assert len(extraction_daemon.read_pending_signals()) == 1
    assert any("Lifecycle daemon wake failed" in rec.message for rec in caplog.records)

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: True)
    emit_event(
        name="session.agent_end",
        payload={"transcript_path": str(transcript)},
        source="pytest",
        session_id="sess-wake-hard",
        owner_id="owner-life",
    )
    with pytest.raises(RuntimeError, match="Event handler failed while fail-hard mode is enabled") as excinfo:
        process_events(limit=5, names=["session.agent_end"])
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert str(excinfo.value.__cause__) == "daemon wake down"


def test_session_lifecycle_daemon_signal_dedupes_with_adapter_signal(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))
    wake_calls = _record_daemon_wake(monkeypatch)

    from core import extraction_daemon

    adapter_transcript = tmp_path / "adapter.jsonl"
    bridge_transcript = tmp_path / "bridge.jsonl"
    adapter_transcript.write_text('{"role":"user","content":"adapter"}\n', encoding="utf-8")
    bridge_transcript.write_text('{"role":"user","content":"bridge"}\n', encoding="utf-8")
    adapter_signal = extraction_daemon.write_signal(
        signal_type="reset",
        session_id="sess-dedupe",
        transcript_path=str(adapter_transcript),
        adapter="adapter-hook",
        meta={"reason": "adapter_reset"},
    )

    emit_event(
        name="session.reset",
        payload={
            "daemon_signal": {
                "enabled": True,
                "transcript_path": str(bridge_transcript),
                "reason": "event_reset",
            },
        },
        source="pytest",
        session_id="sess-dedupe",
        owner_id="owner-life",
    )
    out = process_events(limit=5, names=["session.reset"])

    result = out["details"][0]["result"]
    assert result["daemon_signal_queued"] is True
    assert result["daemon_signal_type"] == "reset"
    assert result["signal_name"] == adapter_signal.name
    assert result["daemon_wake_succeeded"] is True
    assert wake_calls == [True]
    signals = extraction_daemon.read_pending_signals()
    assert len(signals) == 1
    assert signals[0]["type"] == "reset"
    assert signals[0]["session_id"] == "sess-dedupe"
    assert signals[0]["transcript_path"] == str(bridge_transcript)
    assert signals[0]["meta"]["reason"] == "event_reset"


def test_session_lifecycle_default_reset_signal_dedupes_with_adapter_signal(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))
    wake_calls = _record_daemon_wake(monkeypatch)

    from core import extraction_daemon

    adapter_transcript = tmp_path / "adapter-reset.jsonl"
    default_transcript = tmp_path / "default-reset.jsonl"
    adapter_transcript.write_text('{"role":"user","content":"adapter"}\n', encoding="utf-8")
    default_transcript.write_text('{"role":"user","content":"default"}\n', encoding="utf-8")
    adapter_signal = extraction_daemon.write_signal(
        signal_type="reset",
        session_id="sess-reset-default-dedupe",
        transcript_path=str(adapter_transcript),
        adapter="adapter-hook",
        meta={"reason": "adapter_reset", "bypass_recent_reset_dedup": True},
    )

    emit_event(
        name="session.reset",
        payload={"reset_transcript_path": str(default_transcript), "reason": "default-reset"},
        source="pytest",
        session_id="sess-reset-default-dedupe",
        owner_id="owner-life",
    )
    out = process_events(limit=5, names=["session.reset"])

    result = out["details"][0]["result"]
    assert result["daemon_signal_queued"] is True
    assert result["daemon_signal_type"] == "reset"
    assert result["daemon_signal_default"] is True
    assert result["signal_name"] == adapter_signal.name
    assert result["daemon_wake_succeeded"] is True
    assert wake_calls == [True]
    signals = extraction_daemon.read_pending_signals()
    assert len(signals) == 1
    assert signals[0]["type"] == "reset"
    assert signals[0]["session_id"] == "sess-reset-default-dedupe"
    assert signals[0]["transcript_path"] == str(default_transcript)
    assert signals[0]["meta"]["bridge"] == "event_lifecycle_default_reset_bridge"
    assert signals[0]["meta"]["reason"] == "default-reset"
    assert signals[0]["meta"]["bypass_recent_reset_dedup"] is True


def test_session_lifecycle_default_agent_end_signal_dedupes_with_adapter_signal(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))
    wake_calls = _record_daemon_wake(monkeypatch)

    from core import extraction_daemon

    adapter_transcript = tmp_path / "adapter.jsonl"
    default_transcript = tmp_path / "default.jsonl"
    adapter_transcript.write_text('{"role":"user","content":"adapter"}\n', encoding="utf-8")
    default_transcript.write_text('{"role":"user","content":"default"}\n', encoding="utf-8")
    adapter_signal = extraction_daemon.write_signal(
        signal_type="session_end",
        session_id="sess-default-dedupe",
        transcript_path=str(adapter_transcript),
        adapter="adapter-hook",
        meta={"reason": "adapter_end"},
    )

    emit_event(
        name="session.agent_end",
        payload={"transcript_path": str(default_transcript)},
        source="pytest",
        session_id="sess-default-dedupe",
        owner_id="owner-life",
    )
    out = process_events(limit=5, names=["session.agent_end"])

    result = out["details"][0]["result"]
    assert result["daemon_signal_queued"] is True
    assert result["daemon_signal_type"] == "session_end"
    assert result["daemon_signal_default"] is True
    assert result["signal_name"] == adapter_signal.name
    assert result["daemon_wake_succeeded"] is True
    assert wake_calls == [True]
    signals = extraction_daemon.read_pending_signals()
    assert len(signals) == 1
    assert signals[0]["type"] == "session_end"
    assert signals[0]["session_id"] == "sess-default-dedupe"
    assert signals[0]["transcript_path"] == str(default_transcript)
    assert signals[0]["meta"]["bridge"] == "event_lifecycle_default_bridge"


def test_session_lifecycle_default_timeout_signal_dedupes_with_adapter_signal(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))
    wake_calls = _record_daemon_wake(monkeypatch)

    from core import extraction_daemon

    adapter_transcript = tmp_path / "adapter-timeout.jsonl"
    default_transcript = tmp_path / "default-timeout.jsonl"
    adapter_transcript.write_text('{"role":"user","content":"adapter"}\n', encoding="utf-8")
    default_transcript.write_text('{"role":"user","content":"default"}\n', encoding="utf-8")
    adapter_signal = extraction_daemon.write_signal(
        signal_type="timeout",
        session_id="sess-timeout-dedupe",
        transcript_path=str(adapter_transcript),
        adapter="adapter-hook",
        meta={"reason": "adapter_timeout"},
    )

    emit_event(
        name="session.timeout",
        payload={"transcript_path": str(default_transcript)},
        source="pytest",
        session_id="sess-timeout-dedupe",
        owner_id="owner-life",
    )
    out = process_events(limit=5, names=["session.timeout"])

    result = out["details"][0]["result"]
    assert result["daemon_signal_queued"] is True
    assert result["daemon_signal_type"] == "timeout"
    assert result["daemon_signal_default"] is True
    assert result["signal_name"] == adapter_signal.name
    assert result["daemon_wake_succeeded"] is True
    assert wake_calls == [True]
    signals = extraction_daemon.read_pending_signals()
    assert len(signals) == 1
    assert signals[0]["type"] == "timeout"
    assert signals[0]["session_id"] == "sess-timeout-dedupe"
    assert signals[0]["transcript_path"] == str(default_transcript)
    assert signals[0]["meta"]["bridge"] == "event_lifecycle_default_timeout_bridge"


def test_session_lifecycle_default_compaction_signal_dedupes_with_adapter_signal(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))
    wake_calls = _record_daemon_wake(monkeypatch)

    from core import extraction_daemon

    adapter_transcript = tmp_path / "adapter-compaction.jsonl"
    default_transcript = tmp_path / "default-compaction.jsonl"
    adapter_transcript.write_text('{"role":"user","content":"adapter"}\n', encoding="utf-8")
    default_transcript.write_text('{"role":"user","content":"default"}\n', encoding="utf-8")
    adapter_signal = extraction_daemon.write_signal(
        signal_type="compaction",
        session_id="sess-compaction-dedupe",
        transcript_path=str(adapter_transcript),
        adapter="adapter-hook",
        supports_compaction_control=True,
        meta={"reason": "adapter_compaction"},
    )

    emit_event(
        name="session.compaction",
        payload={"transcript_path": str(default_transcript)},
        source="pytest",
        session_id="sess-compaction-dedupe",
        owner_id="owner-life",
    )
    out = process_events(limit=5, names=["session.compaction"])

    result = out["details"][0]["result"]
    assert result["daemon_signal_queued"] is True
    assert result["daemon_signal_type"] == "compaction"
    assert result["daemon_signal_default"] is True
    assert result["signal_name"] == adapter_signal.name
    assert result["daemon_wake_succeeded"] is True
    assert wake_calls == [True]
    signals = extraction_daemon.read_pending_signals()
    assert len(signals) == 1
    assert signals[0]["type"] == "compaction"
    assert signals[0]["session_id"] == "sess-compaction-dedupe"
    assert signals[0]["transcript_path"] == str(default_transcript)
    assert signals[0]["supports_compaction_control"] is False
    assert signals[0]["meta"]["bridge"] == "event_lifecycle_default_compaction_bridge"


def test_session_lifecycle_daemon_signal_helper_preserves_boundaries():
    import core.runtime.events as events

    signal_helper_sources = "\n".join(
        [
            inspect.getsource(events._maybe_queue_lifecycle_daemon_signal),
            inspect.getsource(events._maybe_queue_default_reset_signal),
            inspect.getsource(events._maybe_queue_default_agent_end_signal),
            inspect.getsource(events._maybe_queue_default_timeout_signal),
            inspect.getsource(events._maybe_queue_default_compaction_signal),
        ]
    )
    wake_helper_source = inspect.getsource(events._wake_daemon_after_lifecycle_signal)
    helper_sources = "\n".join([signal_helper_sources, wake_helper_source])
    assert signal_helper_sources.count("from core.extraction_daemon import write_signal") == 5
    assert wake_helper_source.count("from core.extraction_daemon import ensure_alive") == 1
    assert "datastore." not in helper_sources
    assert "_atomic_write" not in helper_sources
    assert "recent_reset" not in helper_sources
    assert "start_daemon" not in helper_sources
    assert "stop_daemon" not in helper_sources
    assert "restart" not in helper_sources
    assert "subprocess" not in helper_sources


def test_session_lifecycle_observation_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    adapter = TestAdapter(tmp_path); set_adapter(adapter)

    event = emit_event(
        name="session.agent_start",
        payload={"reason": "agent boot"},
        source="pytest",
        session_id="sess-life",
        owner_id="owner-life",
    )
    first = EVENT_HANDLERS["session.agent_start"](event)
    second = EVENT_HANDLERS["session.agent_start"](event)

    assert first["persisted"] is True
    assert first["inserted"] is True
    assert second["persisted"] is True
    assert second["inserted"] is False

    from datastore.sessiondb.session_store import list_lifecycle_observations

    rows = list_lifecycle_observations(owner_id="owner-life", session_id="sess-life")
    assert len(rows) == 1
    assert rows[0]["event_id"] == event["id"]


@pytest.mark.parametrize("event_name", ["session.new", "session.agent_end"])
def test_session_lifecycle_remaining_event_names_persist_sessiondb_observation(
    monkeypatch,
    tmp_path,
    event_name,
):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    set_adapter(TestAdapter(tmp_path))

    session_id = event_name.replace(".", "-")
    event = emit_event(
        name=event_name,
        payload={"reason": "lifecycle smoke"},
        source="pytest",
        session_id=session_id,
        owner_id="owner-life",
    )
    out = process_events(limit=5, names=[event_name])

    assert out["processed"] == 1
    result = out["details"][0]["result"]
    assert result["status"] == "acknowledged"
    assert result["event"] == event_name
    assert result["persisted"] is True
    assert result["datastore_id"] == "sessiondb"

    from datastore.sessiondb.session_store import list_lifecycle_observations

    rows = list_lifecycle_observations(owner_id="owner-life", session_id=session_id)
    assert len(rows) == 1
    assert rows[0]["event_id"] == event["id"]
    assert rows[0]["event_name"] == event_name


def test_session_lifecycle_without_session_id_acknowledges_without_persistence(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    adapter = TestAdapter(tmp_path); set_adapter(adapter)

    import core.plugins.sessiondb_contract as sessiondb_contract

    monkeypatch.setattr(
        sessiondb_contract,
        "record_session_lifecycle_observation",
        lambda _event: (_ for _ in ()).throw(AssertionError("missing session_id must not persist")),
    )

    emit_event(name="session.timeout", payload={"reason": "idle"}, source="pytest")
    out = process_events(limit=5, names=["session.timeout"])

    assert out["processed"] == 1
    result = out["details"][0]["result"]
    assert result == {"status": "acknowledged", "event": "session.timeout", "persisted": False}

    from datastore.sessiondb.session_store import list_lifecycle_observations

    assert list_lifecycle_observations(owner_id="default") == []


def test_session_lifecycle_persistence_failure_respects_failhard(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    set_adapter(TestAdapter(tmp_path))

    import core.plugins.sessiondb_contract as sessiondb_contract
    import core.runtime.events as events

    def _boom(_event):
        raise RuntimeError("sessiondb lifecycle down")

    monkeypatch.setattr(sessiondb_contract, "record_session_lifecycle_observation", _boom)
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)
    with caplog.at_level("WARNING"):
        emit_event(name="session.compaction", payload={}, source="pytest", session_id="sess-soft")
        soft = process_events(limit=5, names=["session.compaction"])

    assert soft["processed"] == 1
    assert soft["failed"] == 0
    assert soft["details"][0]["result"] == {
        "status": "acknowledged",
        "event": "session.compaction",
        "persisted": False,
    }
    assert any("SessionDB lifecycle observation persistence failed" in rec.message for rec in caplog.records)

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: True)
    emit_event(name="session.compaction", payload={}, source="pytest", session_id="sess-hard")
    with pytest.raises(RuntimeError, match="Event handler failed while fail-hard mode is enabled") as excinfo:
        process_events(limit=5, names=["session.compaction"])
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert str(excinfo.value.__cause__) == "sessiondb lifecycle down"


def test_session_lifecycle_persistence_failure_does_not_block_failsoft_daemon_signal(
    monkeypatch,
    tmp_path,
    caplog,
):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))
    wake_calls = _record_daemon_wake(monkeypatch)

    import core.plugins.sessiondb_contract as sessiondb_contract
    import core.runtime.events as events

    def _boom(_event):
        raise RuntimeError("sessiondb lifecycle down")

    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")
    monkeypatch.setattr(sessiondb_contract, "record_session_lifecycle_observation", _boom)
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)
    with caplog.at_level("WARNING"):
        emit_event(
            name="session.reset",
            payload={"daemon_signal": {"enabled": True, "transcript_path": str(transcript)}},
            source="pytest",
            session_id="sess-observation-fail",
            owner_id="owner-life",
        )
        out = process_events(limit=5, names=["session.reset"])

    from core import extraction_daemon

    result = out["details"][0]["result"]
    assert result["status"] == "acknowledged"
    assert result["persisted"] is False
    assert result["daemon_signal_queued"] is True
    assert result["daemon_signal_type"] == "reset"
    assert result["daemon_wake_succeeded"] is True
    assert wake_calls == [True]
    assert len(extraction_daemon.read_pending_signals()) == 1
    assert any("SessionDB lifecycle observation persistence failed" in rec.message for rec in caplog.records)


def test_session_lifecycle_persistence_failure_does_not_block_failsoft_default_reset_signal(
    monkeypatch,
    tmp_path,
    caplog,
):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))
    wake_calls = _record_daemon_wake(monkeypatch)

    import core.plugins.sessiondb_contract as sessiondb_contract
    import core.runtime.events as events

    def _boom(_event):
        raise RuntimeError("sessiondb lifecycle down")

    transcript = tmp_path / "reset-preserved.jsonl"
    transcript.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")
    monkeypatch.setattr(sessiondb_contract, "record_session_lifecycle_observation", _boom)
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)
    with caplog.at_level("WARNING"):
        emit_event(
            name="session.reset",
            payload={"reset_transcript_path": str(transcript)},
            source="pytest",
            session_id="sess-reset-observation-fail",
            owner_id="owner-life",
        )
        out = process_events(limit=5, names=["session.reset"])

    from core import extraction_daemon

    result = out["details"][0]["result"]
    assert result["status"] == "acknowledged"
    assert result["persisted"] is False
    assert result["daemon_signal_queued"] is True
    assert result["daemon_signal_type"] == "reset"
    assert result["daemon_signal_default"] is True
    assert result["daemon_wake_succeeded"] is True
    assert wake_calls == [True]
    assert len(extraction_daemon.read_pending_signals()) == 1
    assert any("SessionDB lifecycle observation persistence failed" in rec.message for rec in caplog.records)


def test_session_lifecycle_persistence_failure_does_not_block_failsoft_default_agent_end_signal(
    monkeypatch,
    tmp_path,
    caplog,
):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))
    wake_calls = _record_daemon_wake(monkeypatch)

    import core.plugins.sessiondb_contract as sessiondb_contract
    import core.runtime.events as events

    def _boom(_event):
        raise RuntimeError("sessiondb lifecycle down")

    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")
    monkeypatch.setattr(sessiondb_contract, "record_session_lifecycle_observation", _boom)
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)
    with caplog.at_level("WARNING"):
        emit_event(
            name="session.agent_end",
            payload={"transcript_path": str(transcript)},
            source="pytest",
            session_id="sess-default-observation-fail",
            owner_id="owner-life",
        )
        out = process_events(limit=5, names=["session.agent_end"])

    from core import extraction_daemon

    result = out["details"][0]["result"]
    assert result["status"] == "acknowledged"
    assert result["persisted"] is False
    assert result["daemon_signal_queued"] is True
    assert result["daemon_signal_type"] == "session_end"
    assert result["daemon_signal_default"] is True
    assert result["daemon_wake_succeeded"] is True
    assert wake_calls == [True]
    assert len(extraction_daemon.read_pending_signals()) == 1
    assert any("SessionDB lifecycle observation persistence failed" in rec.message for rec in caplog.records)


def test_session_lifecycle_persistence_failure_does_not_block_failsoft_default_timeout_signal(
    monkeypatch,
    tmp_path,
    caplog,
):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))
    wake_calls = _record_daemon_wake(monkeypatch)

    import core.plugins.sessiondb_contract as sessiondb_contract
    import core.runtime.events as events

    def _boom(_event):
        raise RuntimeError("sessiondb lifecycle down")

    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")
    monkeypatch.setattr(sessiondb_contract, "record_session_lifecycle_observation", _boom)
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)
    with caplog.at_level("WARNING"):
        emit_event(
            name="session.timeout",
            payload={"transcript_path": str(transcript)},
            source="pytest",
            session_id="sess-timeout-observation-fail",
            owner_id="owner-life",
        )
        out = process_events(limit=5, names=["session.timeout"])

    from core import extraction_daemon

    result = out["details"][0]["result"]
    assert result["status"] == "acknowledged"
    assert result["persisted"] is False
    assert result["daemon_signal_queued"] is True
    assert result["daemon_signal_type"] == "timeout"
    assert result["daemon_signal_default"] is True
    assert result["daemon_wake_succeeded"] is True
    assert wake_calls == [True]
    assert len(extraction_daemon.read_pending_signals()) == 1
    assert any("SessionDB lifecycle observation persistence failed" in rec.message for rec in caplog.records)


def test_session_lifecycle_persistence_failure_does_not_block_failsoft_default_compaction_signal(
    monkeypatch,
    tmp_path,
    caplog,
):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "test-inst")
    set_adapter(TestAdapter(tmp_path))
    wake_calls = _record_daemon_wake(monkeypatch)

    import core.plugins.sessiondb_contract as sessiondb_contract
    import core.runtime.events as events

    def _boom(_event):
        raise RuntimeError("sessiondb lifecycle down")

    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")
    monkeypatch.setattr(sessiondb_contract, "record_session_lifecycle_observation", _boom)
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)
    with caplog.at_level("WARNING"):
        emit_event(
            name="session.compaction",
            payload={"transcript_path": str(transcript)},
            source="pytest",
            session_id="sess-compaction-observation-fail",
            owner_id="owner-life",
        )
        out = process_events(limit=5, names=["session.compaction"])

    from core import extraction_daemon

    result = out["details"][0]["result"]
    assert result["status"] == "acknowledged"
    assert result["persisted"] is False
    assert result["daemon_signal_queued"] is True
    assert result["daemon_signal_type"] == "compaction"
    assert result["daemon_signal_default"] is True
    assert result["daemon_wake_succeeded"] is True
    assert wake_calls == [True]
    assert len(extraction_daemon.read_pending_signals()) == 1
    assert any("SessionDB lifecycle observation persistence failed" in rec.message for rec in caplog.records)


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


def _emit_docs_project_maintenance_observed_event(project: str | None = None) -> None:
    emit_broker_event(
        DOCS_PROJECT_MAINTENANCE_OBSERVED_EVENT,
        payload={
            "project": project,
            "observed_at": "2026-05-17T00:00:00+00:00",
            "source": "project-docs-supervisor",
            "tick_kind": "auto_register_and_stale_index",
            "auto_register_interval_seconds": 300.0,
            "stale_index_interval_seconds": 60.0,
            "requested_operations": {"auto_register": True, "stale_index": True},
            "dry_run": False,
        },
        source="pytest",
    )


def test_event_process_docs_project_maintenance_observed_runs_authoritative_listener(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path); set_adapter(adapter); iroot = adapter.instance_root()

    calls: list[object] = []
    project_root = tmp_path / "demo"
    monkeypatch.setattr(
        "core.project_docs.auto_register_project_docs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("old auto-register helper called")),
    )
    monkeypatch.setattr(
        "core.project_docs.index_one_stale_registered_doc",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("old stale-index helper called")),
    )
    monkeypatch.setattr("core.docs.updater.queued_project_log_projects", lambda project=None: [])
    monkeypatch.setattr("core.project_registry.list_projects", lambda: {"demo": {"canonical_path": str(project_root)}})
    monkeypatch.setattr(
        "core.docs.updater.sync_project_visible_docs",
        lambda project, canonical_path, *, root_docs, protected_names: (
            calls.append(("sync", project, canonical_path, sorted(root_docs), sorted(protected_names)))
            or {"registered": 2}
        ),
    )
    monkeypatch.setattr(
        "core.docs.updater.index_one_stale_registered_doc",
        lambda *, project=None: calls.append(("index", project)) or True,
    )

    _emit_docs_project_maintenance_observed_event()

    out = dispatch_broker_events(limit=5, names=[DOCS_PROJECT_MAINTENANCE_OBSERVED_EVENT])
    assert out["processed"] == 1
    assert out["failed"] == 0

    queue_path = get_runtime_root(iroot) / "events" / "queue.json"
    queued = json.loads(queue_path.read_text(encoding="utf-8")).get("events") or []
    event = next(item for item in queued if item.get("name") == DOCS_PROJECT_MAINTENANCE_OBSERVED_EVENT)
    result = event["result"]["listener_result"]
    assert calls == [
        ("sync", "demo", str(project_root), ["AGENTS.md", "PROJECT.md", "TOOLS.md"], ["PROJECT.log"]),
        ("index", None),
    ]
    assert result["mode"] == "authoritative"
    assert result["datastore_id"] == "docsdb"
    assert result["direct_result"]["registered"] == 2
    assert result["direct_result"]["indexed_one"] is True


def test_event_project_docs_listener_preserves_project_scope(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path); set_adapter(adapter); iroot = adapter.instance_root()

    calls: list[object] = []
    project_root = tmp_path / "demo"
    monkeypatch.setattr(
        "core.project_docs.auto_register_project_docs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("old auto-register helper called")),
    )
    monkeypatch.setattr(
        "core.project_docs.index_one_stale_registered_doc",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("old stale-index helper called")),
    )
    monkeypatch.setattr(
        "core.docs.updater.queued_project_log_projects",
        lambda project=None: calls.append(("queued", project)) or [],
    )
    monkeypatch.setattr("core.project_registry.list_projects", lambda: (_ for _ in ()).throw(AssertionError("unscoped list called")))
    monkeypatch.setattr("core.project_registry.get_project", lambda project: {"canonical_path": str(project_root)})
    monkeypatch.setattr(
        "core.docs.updater.sync_project_visible_docs",
        lambda project, canonical_path, *, root_docs, protected_names: (
            calls.append(("sync", project, canonical_path))
            or {"registered": 1}
        ),
    )
    monkeypatch.setattr(
        "core.docs.updater.index_one_stale_registered_doc",
        lambda *, project=None: calls.append(("index", project)) or True,
    )

    _emit_docs_project_maintenance_observed_event(project="Demo")

    out = dispatch_broker_events(limit=5, names=[DOCS_PROJECT_MAINTENANCE_OBSERVED_EVENT])
    assert out["processed"] == 1
    assert out["failed"] == 0

    queue_path = get_runtime_root(iroot) / "events" / "queue.json"
    queued = json.loads(queue_path.read_text(encoding="utf-8")).get("events") or []
    event = next(item for item in queued if item.get("name") == DOCS_PROJECT_MAINTENANCE_OBSERVED_EVENT)
    result = event["result"]["listener_result"]
    assert calls == [
        ("queued", "demo"),
        ("sync", "demo", str(project_root)),
        ("index", "demo"),
    ]
    assert result["project"] == "demo"
    assert result["direct_result"]["registered"] == 1
    assert result["direct_result"]["indexed_one"] is True


def test_event_project_docs_listener_materializes_queued_projects_before_sync(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path); set_adapter(adapter); iroot = adapter.instance_root()

    calls: list[object] = []
    projects: dict[str, dict[str, str]] = {}
    queued_root = tmp_path / "queued-demo"
    monkeypatch.setattr(
        "core.project_docs.auto_register_project_docs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("old auto-register helper called")),
    )
    monkeypatch.setattr(
        "core.project_docs.index_one_stale_registered_doc",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("old stale-index helper called")),
    )
    monkeypatch.setattr("core.docs.updater.queued_project_log_projects", lambda project=None: calls.append(("queued", project)) or ["queued-demo"])
    monkeypatch.setattr("core.project_registry.project_exists_raw", lambda name: name in projects)
    monkeypatch.setattr("core.project_registry.project_deleted_raw", lambda name: False)

    def create_project(name, *, description):
        calls.append(("create", name, description))
        projects[name] = {"canonical_path": str(queued_root)}

    monkeypatch.setattr("core.project_registry.create_project", create_project)
    monkeypatch.setattr("core.project_registry.list_projects", lambda: calls.append("list") or dict(projects))
    monkeypatch.setattr(
        "core.docs.updater.sync_project_visible_docs",
        lambda project, canonical_path, *, root_docs, protected_names: (
            calls.append(("sync", project, canonical_path))
            or {"registered": 1}
        ),
    )
    monkeypatch.setattr("core.docs.updater.index_one_stale_registered_doc", lambda *, project=None: calls.append(("index", project)) or False)

    _emit_docs_project_maintenance_observed_event()

    out = dispatch_broker_events(limit=5, names=[DOCS_PROJECT_MAINTENANCE_OBSERVED_EVENT])
    assert out["processed"] == 1
    assert out["failed"] == 0

    queue_path = get_runtime_root(iroot) / "events" / "queue.json"
    queued = json.loads(queue_path.read_text(encoding="utf-8")).get("events") or []
    event = next(item for item in queued if item.get("name") == DOCS_PROJECT_MAINTENANCE_OBSERVED_EVENT)
    assert event["result"]["listener_result"]["direct_result"]["registered"] == 1
    assert calls == [
        ("queued", None),
        ("create", "queued-demo", "Project inferred from conversation continuity."),
        "list",
        ("sync", "queued-demo", str(queued_root)),
        ("index", None),
    ]


def test_event_project_docs_listener_auto_register_failure_respects_fail_soft(monkeypatch, tmp_path, caplog):
    adapter = TestAdapter(tmp_path); set_adapter(adapter); iroot = adapter.instance_root()

    import core.runtime.events as events

    calls: list[str] = []

    def fail_register(*_args, **_kwargs):
        calls.append("sync")
        raise RuntimeError("register boom")

    monkeypatch.setattr(
        "core.project_docs.auto_register_project_docs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("old auto-register helper called")),
    )
    monkeypatch.setattr(
        "core.project_docs.index_one_stale_registered_doc",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("old stale-index helper called")),
    )
    monkeypatch.setattr("core.docs.updater.queued_project_log_projects", lambda project=None: [])
    monkeypatch.setattr("core.project_registry.list_projects", lambda: {"demo": {"canonical_path": str(tmp_path / "demo")}})
    monkeypatch.setattr("core.docs.updater.sync_project_visible_docs", fail_register)
    monkeypatch.setattr("core.docs.updater.index_one_stale_registered_doc", lambda *, project=None: calls.append("index") or True)
    monkeypatch.setattr("core.plugins.docsdb_contract._fail_hard_enabled", lambda: False)
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)

    _emit_docs_project_maintenance_observed_event()
    with caplog.at_level("WARNING"):
        out = dispatch_broker_events(limit=5, names=[DOCS_PROJECT_MAINTENANCE_OBSERVED_EVENT])

    assert out["processed"] == 0
    assert out["failed"] == 1
    assert calls == ["sync", "index"]
    assert "Project docs auto-register failed for demo: register boom" in caplog.text

    queue_path = get_runtime_root(iroot) / "events" / "queue.json"
    queued = json.loads(queue_path.read_text(encoding="utf-8")).get("events") or []
    event = next(item for item in queued if item.get("name") == DOCS_PROJECT_MAINTENANCE_OBSERVED_EVENT)
    direct_result = event["result"]["listener_result"]["direct_result"]
    assert direct_result["indexed_one"] is True
    assert direct_result["errors"] == [{"tick": "auto_register", "error": "demo: register boom"}]


def test_event_project_docs_listener_auto_register_failure_respects_fail_hard(monkeypatch, tmp_path):
    set_adapter(TestAdapter(tmp_path))

    import core.runtime.events as events

    calls: list[str] = []

    def fail_register(*_args, **_kwargs):
        calls.append("sync")
        raise RuntimeError("register boom")

    monkeypatch.setattr(
        "core.project_docs.auto_register_project_docs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("old auto-register helper called")),
    )
    monkeypatch.setattr(
        "core.project_docs.index_one_stale_registered_doc",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("old stale-index helper called")),
    )
    monkeypatch.setattr("core.docs.updater.queued_project_log_projects", lambda project=None: [])
    monkeypatch.setattr("core.project_registry.list_projects", lambda: {"demo": {"canonical_path": str(tmp_path / "demo")}})
    monkeypatch.setattr("core.docs.updater.sync_project_visible_docs", fail_register)
    monkeypatch.setattr("core.docs.updater.index_one_stale_registered_doc", lambda *, project=None: calls.append("index") or True)
    monkeypatch.setattr("core.plugins.docsdb_contract._fail_hard_enabled", lambda: True)
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: True)

    _emit_docs_project_maintenance_observed_event()
    with pytest.raises(RuntimeError, match="project docs auto-register listener failed") as excinfo:
        dispatch_broker_events(limit=5, names=[DOCS_PROJECT_MAINTENANCE_OBSERVED_EVENT])

    assert str(excinfo.value.__cause__) == "project docs auto-register listener failed"
    assert str(excinfo.value.__cause__.__cause__) == "register boom"
    assert calls == ["sync"]


def test_event_project_docs_listener_stale_index_failure_respects_fail_soft(monkeypatch, tmp_path, caplog):
    adapter = TestAdapter(tmp_path); set_adapter(adapter); iroot = adapter.instance_root()

    import core.runtime.events as events

    calls: list[str] = []

    def fail_index(*_args, **_kwargs):
        calls.append("index")
        raise RuntimeError("index boom")

    monkeypatch.setattr(
        "core.project_docs.auto_register_project_docs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("old auto-register helper called")),
    )
    monkeypatch.setattr(
        "core.project_docs.index_one_stale_registered_doc",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("old stale-index helper called")),
    )
    monkeypatch.setattr("core.docs.updater.queued_project_log_projects", lambda project=None: [])
    monkeypatch.setattr("core.project_registry.list_projects", lambda: {"demo": {"canonical_path": str(tmp_path / "demo")}})
    monkeypatch.setattr("core.docs.updater.sync_project_visible_docs", lambda *args, **kwargs: calls.append("sync") or {"registered": 2})
    monkeypatch.setattr("core.docs.updater.index_one_stale_registered_doc", fail_index)
    monkeypatch.setattr("core.plugins.docsdb_contract._fail_hard_enabled", lambda: False)
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)

    _emit_docs_project_maintenance_observed_event()
    with caplog.at_level("WARNING"):
        out = dispatch_broker_events(limit=5, names=[DOCS_PROJECT_MAINTENANCE_OBSERVED_EVENT])

    assert out["processed"] == 0
    assert out["failed"] == 1
    assert calls == ["sync", "index"]
    assert "project docs stale-index listener failed: index boom" in caplog.text

    queue_path = get_runtime_root(iroot) / "events" / "queue.json"
    queued = json.loads(queue_path.read_text(encoding="utf-8")).get("events") or []
    event = next(item for item in queued if item.get("name") == DOCS_PROJECT_MAINTENANCE_OBSERVED_EVENT)
    direct_result = event["result"]["listener_result"]["direct_result"]
    assert direct_result["registered"] == 2
    assert direct_result["errors"] == [{"tick": "stale_index", "error": "index boom"}]


def test_event_project_docs_listener_stale_index_failure_respects_fail_hard(monkeypatch, tmp_path):
    set_adapter(TestAdapter(tmp_path))

    import core.runtime.events as events

    calls: list[str] = []

    def fail_index(*_args, **_kwargs):
        calls.append("index")
        raise RuntimeError("index boom")

    monkeypatch.setattr(
        "core.project_docs.auto_register_project_docs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("old auto-register helper called")),
    )
    monkeypatch.setattr(
        "core.project_docs.index_one_stale_registered_doc",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("old stale-index helper called")),
    )
    monkeypatch.setattr("core.docs.updater.queued_project_log_projects", lambda project=None: [])
    monkeypatch.setattr("core.project_registry.list_projects", lambda: {"demo": {"canonical_path": str(tmp_path / "demo")}})
    monkeypatch.setattr("core.docs.updater.sync_project_visible_docs", lambda *args, **kwargs: calls.append("sync") or {"registered": 2})
    monkeypatch.setattr("core.docs.updater.index_one_stale_registered_doc", fail_index)
    monkeypatch.setattr("core.plugins.docsdb_contract._fail_hard_enabled", lambda: True)
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: True)

    _emit_docs_project_maintenance_observed_event()
    with pytest.raises(RuntimeError, match="project docs stale-index listener failed") as excinfo:
        dispatch_broker_events(limit=5, names=[DOCS_PROJECT_MAINTENANCE_OBSERVED_EVENT])

    assert str(excinfo.value.__cause__) == "project docs stale-index listener failed"
    assert str(excinfo.value.__cause__.__cause__) == "index boom"
    assert calls == ["sync", "index"]


def test_request_project_docs_update_runs_authoritative_handler(monkeypatch, tmp_path):
    set_adapter(TestAdapter(tmp_path))
    from core.plugins.docsdb_contract import register_project_docs_update_request_handler

    calls: list[object] = []
    project_root = tmp_path / "demo"
    project_root.mkdir()
    monkeypatch.setattr("core.project_registry.get_project_raw", lambda project: {"canonical_path": str(project_root)})
    monkeypatch.setattr("core.project_registry.get_project", lambda project: (_ for _ in ()).throw(AssertionError("raw project should be enough")))
    monkeypatch.setattr(
        "core.docs_updater_hook.update_project_docs",
        lambda snapshots, *, extraction_result, dry_run, force_project: (
            calls.append(("update", snapshots, extraction_result, dry_run, force_project))
            or {"projects_checked": 1, "docs_updated": 2, "docs_skipped": 0, "trivial_skipped": 0, "errors": 0}
        ),
    )
    monkeypatch.setattr(
        "core.docs.updater.sync_project_visible_docs",
        lambda project, canonical_path, *, root_docs, protected_names: (
            calls.append(("sync", project, canonical_path, sorted(root_docs), sorted(protected_names)))
            or {"registered": 3, "unregistered": 1, "project_md_refreshed": 1}
        ),
    )
    monkeypatch.setattr(
        "core.docs.updater.update_registered_docs",
        lambda **kwargs: calls.append(("index_docs", kwargs)) or 4,
    )
    monkeypatch.setattr(
        "core.docs.updater.index_project_logs",
        lambda *, project: calls.append(("index_logs", project)) or 5,
    )

    register_project_docs_update_request_handler()
    response = request_broker_event(
        DOCS_PROJECT_UPDATE_REQUEST_EVENT,
        {
            "source": "project-docs-worker",
            "project": "Demo",
            "request_id": "req-1",
            "dry_run": False,
            "snapshots": [{"project": "demo", "changes": [{"path": "tool.py"}]}],
            "project_log_entries": ["- changed"],
            "project_log_offset": 12,
            "request": {"request_id": "req-1"},
        },
        source="pytest",
    )

    assert response["status"] == "ok"
    result = response["responses"][0]["result"]
    assert calls == [
        ("sync", "demo", str(project_root), ["AGENTS.md", "PROJECT.md", "TOOLS.md"], ["PROJECT.log"]),
        (
            "update",
            [{"project": "demo", "changes": [{"path": "tool.py"}]}],
            {"project_logs": {"demo": ["- changed"]}},
            False,
            "demo",
        ),
        ("sync", "demo", str(project_root), ["AGENTS.md", "PROJECT.md", "TOOLS.md"], ["PROJECT.log"]),
        (
            "index_docs",
            {
                "project": "demo",
                "dry_run": False,
                "protected_names": {"PROJECT.log"},
                "index_project_logs_after": False,
            },
        ),
        ("index_logs", "demo"),
    ]
    assert result["metrics"]["docs_updated"] == 2
    assert result["registry_sync"] == {"registered": 6, "unregistered": 2, "project_md_refreshed": 2}
    assert result["indexed_docs"] == 4
    assert result["indexed_project_logs"] == 5


def test_request_project_docs_update_rejects_wrong_source(monkeypatch, tmp_path):
    set_adapter(TestAdapter(tmp_path))
    import core.runtime.events as events
    from core.plugins.docsdb_contract import register_project_docs_update_request_handler

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(
        "core.docs_updater_hook.update_project_docs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("wrong-source request must not update docs")),
    )

    register_project_docs_update_request_handler()
    response = request_broker_event(
        DOCS_PROJECT_UPDATE_REQUEST_EVENT,
        {"source": "pytest", "project": "demo"},
        source="pytest",
    )

    assert response["status"] == "failed"
    assert response["responses"][0]["result"]["error"] == "payload.source must be project-docs-worker"


def test_event_process_docs_project_maintenance_observed_validation_respects_fail_hard(monkeypatch, tmp_path):
    set_adapter(TestAdapter(tmp_path))

    import core.runtime.events as events

    emit_broker_event(
        DOCS_PROJECT_MAINTENANCE_OBSERVED_EVENT,
        payload={"source": "wrong"},
        source="pytest",
    )

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)
    out = dispatch_broker_events(limit=5, names=[DOCS_PROJECT_MAINTENANCE_OBSERVED_EVENT])
    assert out["processed"] == 0
    assert out["failed"] == 1
    assert "payload.source must be project-docs-supervisor" in out["details"][0]["result"]["error"]

    emit_broker_event(
        DOCS_PROJECT_MAINTENANCE_OBSERVED_EVENT,
        payload={"source": "wrong"},
        source="pytest",
    )
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: True)
    with pytest.raises(RuntimeError, match="payload.source must be project-docs-supervisor"):
        dispatch_broker_events(limit=5, names=[DOCS_PROJECT_MAINTENANCE_OBSERVED_EVENT])


def test_event_process_session_ingest_log(monkeypatch, tmp_path):
    set_adapter(TestAdapter(tmp_path))
    import core.plugins.memorydb_contract as memorydb_contract
    import core.plugins.sessiondb_contract as sessiondb_contract

    assert not hasattr(memorydb_contract, "run_session_ingest_payload")

    called = {}

    def _fake_helper(payload):
        called.update(payload)
        return {"status": "indexed", "session_id": payload["session_id"], "chunks": 2}

    monkeypatch.setattr(sessiondb_contract, "run_session_ingest_payload", _fake_helper)

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


def test_event_process_session_ingest_log_has_no_handler_local_exception_or_direct_ingest_call():
    import core.runtime.events as events

    source = Path(events.__file__).read_text(encoding="utf-8")
    handler_source = inspect.getsource(events._handle_session_ingest_log)

    assert "run_session_logs_ingest" not in source
    assert "from core.plugins.memorydb_contract import run_session_ingest_payload" not in source
    assert "from core.plugins.sessiondb_contract import run_session_ingest_payload" in source
    assert "except Exception" not in handler_source


@pytest.mark.parametrize("helper_status", ["failed", "error"])
def test_event_process_session_ingest_log_failed_result_marks_failed(monkeypatch, tmp_path, helper_status):
    set_adapter(TestAdapter(tmp_path))

    import core.runtime.events as events

    monkeypatch.setattr(
        "core.plugins.sessiondb_contract.run_session_ingest_payload",
        lambda _payload: {"status": helper_status, "error": "simulated ingest failure"},
    )
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)

    emit_event(
        name="session.ingest_log",
        payload={"session_id": "sess-failed"},
        source="pytest",
    )
    out = process_events(limit=5, names=["session.ingest_log"])
    assert out["processed"] == 0
    assert out["failed"] == 1
    assert out["details"][0]["result"]["result"]["error"] == "simulated ingest failure"

    emit_event(
        name="session.ingest_log",
        payload={"session_id": "sess-failed-hard"},
        source="pytest",
    )
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: True)
    with pytest.raises(RuntimeError, match="Event handler failed while fail-hard mode is enabled"):
        process_events(limit=5, names=["session.ingest_log"])


def test_event_process_session_ingest_log_rejects_missing_session_id(monkeypatch, tmp_path):
    set_adapter(TestAdapter(tmp_path))

    import core.runtime.events as events

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(
        "core.plugins.sessiondb_contract.run_session_ingest_payload",
        lambda _payload: (_ for _ in ()).throw(AssertionError("missing session_id must not ingest")),
    )

    emit_event(
        name="session.ingest_log",
        payload={"owner_id": "owner-missing"},
        source="pytest",
    )
    out = process_events(limit=5, names=["session.ingest_log"])
    assert out["processed"] == 0
    assert out["failed"] == 1
    assert out["details"][0]["result"]["error"] == "payload.session_id is required"


def test_event_process_session_ingest_log_helper_exception_uses_event_exception_path(
    monkeypatch,
    tmp_path,
):
    set_adapter(TestAdapter(tmp_path))

    import core.plugins.sessiondb_contract as sessiondb_contract
    import core.runtime.events as events

    def _fail(_payload):
        raise RuntimeError("simulated helper boom")

    monkeypatch.setattr(sessiondb_contract, "run_session_ingest_payload", _fail)
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)

    emit_event(
        name="session.ingest_log",
        payload={"session_id": "sess-helper-boom"},
        source="pytest",
    )
    out = process_events(limit=5, names=["session.ingest_log"])
    assert out["processed"] == 0
    assert out["failed"] == 1
    assert out["details"][0]["error"] == "simulated helper boom"

    emit_event(
        name="session.ingest_log",
        payload={"session_id": "sess-helper-boom-hard"},
        source="pytest",
    )
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: True)
    with pytest.raises(RuntimeError, match="Event handler failed while fail-hard mode is enabled") as excinfo:
        process_events(limit=5, names=["session.ingest_log"])
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert str(excinfo.value.__cause__) == "simulated helper boom"


def test_request_session_ingest_log_runs_sessiondb_handler(monkeypatch, tmp_path):
    set_adapter(TestAdapter(tmp_path))
    from core.plugins.sessiondb_contract import register_session_ingest_log_request_handler

    called = {}

    def _fake_run(**kwargs):
        called.update(kwargs)
        return {
            "status": "indexed",
            "session_id": kwargs["session_id"],
            "source_kind": "transcript_path",
            "microchunks_stored": 2,
        }

    monkeypatch.setattr("core.ingest_runtime.run_session_logs_ingest", _fake_run)

    register_session_ingest_log_request_handler()
    response = request_broker_event(
        SESSION_INGEST_LOG_REQUEST_EVENT,
        {
            "session_id": "sess-req",
            "owner_id": " owner-req ",
            "label": "SessionEnd",
            "session_file": str(tmp_path / "session.jsonl"),
            "transcript_path": str(tmp_path / "transcript.jsonl"),
            "source_channel": "codex",
            "conversation_id": "conv-req",
            "participant_ids": [" user:owner ", "", "agent:quaid"],
            "participant_aliases": {" Operator ": " user:owner "},
            "message_count": 4,
            "topic_hint": "broker request",
        },
        source="pytest",
    )

    assert response["status"] == "ok"
    result = response["responses"][0]["result"]
    assert response["responses"][0]["datastore_id"] == "sessiondb"
    assert result["status"] == "indexed"
    assert result["microchunks_stored"] == 2
    assert called["session_id"] == "sess-req"
    assert called["owner_id"] == "owner-req"
    assert called["source_channel"] == "codex"
    assert called["conversation_id"] == "conv-req"
    assert called["participant_ids"] == ["user:owner", "agent:quaid"]
    assert called["participant_aliases"] == {" Operator ": " user:owner "}
    assert called["message_count"] == 4
    assert called["topic_hint"] == "broker request"


def test_request_session_ingest_log_rejects_missing_session_id(monkeypatch, tmp_path):
    set_adapter(TestAdapter(tmp_path))
    import core.runtime.events as events
    from core.plugins.sessiondb_contract import register_session_ingest_log_request_handler

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(
        "core.ingest_runtime.run_session_logs_ingest",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("missing session_id must not ingest")),
    )

    register_session_ingest_log_request_handler()
    response = request_broker_event(
        SESSION_INGEST_LOG_REQUEST_EVENT,
        {"owner_id": "owner-req"},
        source="pytest",
    )

    assert response["status"] == "failed"
    assert response["responses"][0]["result"]["error"] == "payload.session_id is required"


def test_sessiondb_session_ingest_helper_normalizes_payload(monkeypatch, tmp_path):
    set_adapter(TestAdapter(tmp_path))
    import core.plugins.sessiondb_contract as sessiondb_contract

    called = {}

    def _fake_run(**kwargs):
        called.update(kwargs)
        return {"status": "indexed", "session_id": kwargs["session_id"]}

    monkeypatch.setattr("core.ingest_runtime.run_session_logs_ingest", _fake_run)

    result = sessiondb_contract.run_session_ingest_payload(
        {
            "session_id": " sess-sessiondb ",
            "owner_id": " owner-sessiondb ",
            "label": " SessionEnd ",
            "session_file": tmp_path / "session.jsonl",
            "transcript_path": tmp_path / "transcript.jsonl",
            "source_channel": " codex ",
            "conversation_id": " conv-sessiondb ",
            "participant_ids": [" user:owner ", "", "agent:quaid"],
            "participant_aliases": {" Operator ": " user:owner "},
            "message_count": "5",
            "topic_hint": " helper ownership ",
        }
    )

    assert result == {"status": "indexed", "session_id": "sess-sessiondb"}
    assert called == {
        "session_id": "sess-sessiondb",
        "owner_id": "owner-sessiondb",
        "label": "SessionEnd",
        "session_file": str(tmp_path / "session.jsonl"),
        "transcript_path": str(tmp_path / "transcript.jsonl"),
        "source_channel": "codex",
        "conversation_id": "conv-sessiondb",
        "participant_ids": ["user:owner", "agent:quaid"],
        "participant_aliases": {" Operator ": " user:owner "},
        "message_count": 5,
        "topic_hint": "helper ownership",
    }


def test_memorydb_session_ingest_wrappers_are_retired_from_memorydb_contract():
    import core.plugins.memorydb_contract as memorydb_contract
    import core.plugins.sessiondb_contract as sessiondb_contract

    retired_names = {
        "run_session_ingest_payload",
        "handle_session_ingest_log_request",
        "register_session_ingest_log_request_handler",
    }
    for name in retired_names:
        assert not hasattr(memorydb_contract, name)
        assert hasattr(sessiondb_contract, name)

    memorydb_source = Path(memorydb_contract.__file__).read_text(encoding="utf-8")
    for name in retired_names:
        assert f"def {name}" not in memorydb_source


def test_memorydb_session_ingest_wrappers_have_no_production_references():
    production_root = Path(__file__).resolve().parents[1]
    forbidden_patterns = [
        "memorydb_contract.run_session_ingest_payload",
        "memorydb_contract.handle_session_ingest_log_request",
        "memorydb_contract.register_session_ingest_log_request_handler",
        "from core.plugins.memorydb_contract import run_session_ingest_payload",
        "from core.plugins.memorydb_contract import handle_session_ingest_log_request",
        "from core.plugins.memorydb_contract import register_session_ingest_log_request_handler",
    ]
    offenders = []
    for path in production_root.rglob("*.py"):
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in forbidden_patterns:
            if pattern in text:
                offenders.append(f"{path.relative_to(production_root)}: {pattern}")

    assert offenders == []


def test_sessiondb_request_registrar_owns_session_ingest_request(tmp_path):
    set_adapter(TestAdapter(tmp_path))
    import core.plugins.sessiondb_contract as sessiondb_contract
    import core.runtime.events as events

    sessiondb_contract.register_session_ingest_log_request_handler()

    with events._REQUEST_EVENT_HANDLERS_LOCK:
        handlers = list(events._REQUEST_EVENT_HANDLERS.get(SESSION_INGEST_LOG_REQUEST_EVENT) or [])

    assert [(item["datastore_id"], item["handler"]) for item in handlers] == [
        ("sessiondb", sessiondb_contract.handle_session_ingest_log_request)
    ]


def test_request_extraction_publish_runs_memorydb_handler(monkeypatch, tmp_path):
    set_adapter(TestAdapter(tmp_path))
    import core.plugins.memorydb_contract as memorydb_contract
    from core.plugins.memorydb_contract import register_extraction_publish_request_handler

    called = {}

    def _fake_publish(result, **kwargs):
        called["result"] = result
        called["kwargs"] = kwargs
        result["facts_stored"] = 1
        result["facts_skipped"] = 0
        result["edges_created"] = 1
        result["facts"] = [{"status": "stored", "text": "Maya keeps the launch checklist in the red binder"}]
        return [{"text": "Maya keeps the launch checklist in the red binder", "project": "launch-app"}]

    monkeypatch.setattr(memorydb_contract, "run_extraction_publish_payload", _fake_publish)

    register_extraction_publish_request_handler()
    response = request_broker_event(
        MEMORY_EXTRACTION_PUBLISH_REQUEST_EVENT,
        {
            "source": "daemon-final-rolling-flush",
            "result": {
                "raw_facts": [{"text": "Maya keeps the launch checklist in the red binder"}],
                "facts": [],
            },
            "owner_id": " owner-req ",
            "label": "RollingFlush",
            "session_id": "sess-publish",
            "source_channel": "codex",
            "target_datastore": "memorydb",
            "source_conversation_id": "conv-publish",
            "participant_entity_ids": [" entity:user ", "", "entity:agent"],
            "dry_run": False,
            "snippet_files": 2,
            "journal_files": 1,
            "project_log_projects": 1,
        },
        source="pytest",
    )

    assert response["status"] == "ok"
    row = response["responses"][0]
    assert row["datastore_id"] == "memorydb"
    result = row["result"]
    assert result["status"] == "ok"
    assert result["publish_result"]["facts_stored"] == 1
    assert result["publish_result"]["edges_created"] == 1
    assert result["facts_for_orchestration"][0]["project"] == "launch-app"
    kwargs = called["kwargs"]
    assert kwargs["owner_id"] == "owner-req"
    assert kwargs["label"] == "RollingFlush"
    assert kwargs["session_id"] == "sess-publish"
    assert kwargs["source_channel"] == "codex"
    assert kwargs["target_datastore"] == "memorydb"
    assert kwargs["source_conversation_id"] == "conv-publish"
    assert kwargs["participant_entity_ids"] == ["entity:user", "entity:agent"]
    assert kwargs["snippet_files"] == 2
    assert kwargs["journal_files"] == 1
    assert kwargs["project_log_projects"] == 1


def test_request_extraction_publish_rejects_wrong_source(monkeypatch, tmp_path):
    set_adapter(TestAdapter(tmp_path))
    import core.runtime.events as events
    from core.plugins.memorydb_contract import register_extraction_publish_request_handler

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)

    register_extraction_publish_request_handler()
    response = request_broker_event(
        MEMORY_EXTRACTION_PUBLISH_REQUEST_EVENT,
        {"source": "cli", "result": {"raw_facts": []}},
        source="pytest",
    )

    assert response["status"] == "failed"
    assert response["responses"][0]["result"]["error"] == "payload.source must be daemon-final-rolling-flush"


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (
            {"source": "daemon-final-rolling-flush", "result": []},
            "payload.result must be an object",
        ),
        (
            {"source": "daemon-final-rolling-flush", "result": {}, "label": "RollingFlush"},
            "payload.owner_id is required",
        ),
        (
            {"source": "daemon-final-rolling-flush", "result": {}, "owner_id": "owner-req"},
            "payload.label is required",
        ),
    ],
)
def test_request_extraction_publish_rejects_required_payload_fields(monkeypatch, tmp_path, payload, error):
    set_adapter(TestAdapter(tmp_path))
    import core.runtime.events as events
    from core.plugins.memorydb_contract import register_extraction_publish_request_handler

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)

    register_extraction_publish_request_handler()
    response = request_broker_event(
        MEMORY_EXTRACTION_PUBLISH_REQUEST_EVENT,
        payload,
        source="pytest",
    )

    assert response["status"] == "failed"
    assert response["responses"][0]["result"]["error"] == error


def test_request_snippet_journal_write_runs_insightdb_handler(monkeypatch, tmp_path):
    set_adapter(TestAdapter(tmp_path))
    import core.plugins.insightdb_contract as insightdb_contract
    from core.plugins.insightdb_contract import register_snippet_journal_write_request_handler

    called = {}

    def _fake_write(payload):
        called["payload"] = payload
        return {
            "status": "ok",
            "snippet_files_seen": 1,
            "snippet_items_seen": 1,
            "snippet_files_written": 1,
            "snippet_items_written": 1,
            "snippet_files_skipped": 0,
            "journal_files_seen": 1,
            "journal_files_written": 1,
            "journal_files_skipped": 0,
            "target_files": {
                "snippets": ["USER.snippets.md"],
                "journal": ["SOUL.journal.md"],
            },
            "errors": [],
        }

    monkeypatch.setattr(insightdb_contract, "run_snippet_journal_write_payload", _fake_write)

    register_snippet_journal_write_request_handler()
    response = request_broker_event(
        EVOLUTION_SNIPPET_JOURNAL_WRITE_REQUEST_EVENT,
        {
            "source": "extraction-apply-payloads",
            "owner_id": "owner-req",
            "session_id": "sess-note",
            "label": "RollingFlush",
            "trigger": "CLI",
            "snippets": {"USER.md": ["Alden Rook is Owner's test godbrother."]},
            "journal": {"SOUL.md": "A quiet journal note."},
            "write_snippets": True,
            "write_journal": True,
            "dry_run": False,
        },
        source="pytest",
    )

    assert response["status"] == "ok"
    row = response["responses"][0]
    assert row["datastore_id"] == "insightdb"
    result = row["result"]
    assert result["status"] == "ok"
    metrics = result["snippet_journal_metrics"]
    assert metrics["snippet_files_written"] == 1
    assert metrics["journal_files_written"] == 1
    assert metrics["target_files"]["snippets"] == ["USER.snippets.md"]
    assert called["payload"]["source"] == "extraction-apply-payloads"
    assert called["payload"]["snippets"] == {"USER.md": ["Alden Rook is Owner's test godbrother."]}
    assert called["payload"]["journal"] == {"SOUL.md": "A quiet journal note."}


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (
            {"source": "wrong", "snippets": {}, "journal": {}},
            "payload.source must be extraction-apply-payloads",
        ),
        (
            {"source": "extraction-apply-payloads", "snippets": [], "journal": {}},
            "payload.snippets must be an object",
        ),
        (
            {"source": "extraction-apply-payloads", "snippets": {}, "journal": []},
            "payload.journal must be an object",
        ),
    ],
)
def test_request_snippet_journal_write_rejects_required_payload_fields(monkeypatch, tmp_path, payload, error):
    set_adapter(TestAdapter(tmp_path))
    import core.runtime.events as events
    from core.plugins.insightdb_contract import register_snippet_journal_write_request_handler

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)

    register_snippet_journal_write_request_handler()
    response = request_broker_event(
        EVOLUTION_SNIPPET_JOURNAL_WRITE_REQUEST_EVENT,
        payload,
        source="pytest",
    )

    assert response["status"] == "failed"
    assert response["responses"][0]["datastore_id"] == "insightdb"
    assert response["responses"][0]["result"]["error"] == error


def test_split_snippet_journal_request_metadata_declared():
    from core.contracts.datastore import build_first_party_datastore_contracts
    from core.datastore_registry import get_datastore_manifest

    manifest = get_datastore_manifest("insightdb")
    assert EVOLUTION_SNIPPET_WRITE_REQUEST_EVENT in manifest["request_handlers"]
    assert EVOLUTION_JOURNAL_WRITE_REQUEST_EVENT in manifest["request_handlers"]

    contract = build_first_party_datastore_contracts()["insightdb"]
    handlers = {spec.event_type: spec for spec in contract.list_request_handlers()}
    assert EVOLUTION_SNIPPET_WRITE_REQUEST_EVENT in handlers
    assert EVOLUTION_JOURNAL_WRITE_REQUEST_EVENT in handlers
    assert "core.plugins.insightdb_contract.handle_snippet_write_request" in handlers[
        EVOLUTION_SNIPPET_WRITE_REQUEST_EVENT
    ].replacement_targets
    assert "core.plugins.insightdb_contract.handle_journal_write_request" in handlers[
        EVOLUTION_JOURNAL_WRITE_REQUEST_EVENT
    ].replacement_targets


def test_split_snippet_journal_request_handlers_register_under_insightdb(tmp_path):
    set_adapter(TestAdapter(tmp_path))
    import core.runtime.events as events
    from core.plugins.insightdb_contract import (
        register_journal_write_request_handler,
        register_snippet_write_request_handler,
    )

    register_snippet_write_request_handler()
    register_journal_write_request_handler()

    with events._REQUEST_EVENT_HANDLERS_LOCK:
        snippet_handlers = list(events._REQUEST_EVENT_HANDLERS.get(EVOLUTION_SNIPPET_WRITE_REQUEST_EVENT) or [])
        journal_handlers = list(events._REQUEST_EVENT_HANDLERS.get(EVOLUTION_JOURNAL_WRITE_REQUEST_EVENT) or [])

    assert [handler["datastore_id"] for handler in snippet_handlers] == ["insightdb"]
    assert [handler["datastore_id"] for handler in journal_handlers] == ["insightdb"]


def test_split_snippet_journal_request_handlers_return_family_zero_metrics(tmp_path):
    set_adapter(TestAdapter(tmp_path))
    from core.plugins.insightdb_contract import (
        register_journal_write_request_handler,
        register_snippet_write_request_handler,
    )

    register_snippet_write_request_handler()
    register_journal_write_request_handler()

    snippet_response = request_broker_event(
        EVOLUTION_SNIPPET_WRITE_REQUEST_EVENT,
        {
            "source": "extraction-apply-payloads",
            "trigger": "CLI",
            "snippets": {"USER.md": ["Maya keeps a green tea note."]},
            "dry_run": True,
        },
        source="pytest",
    )
    journal_response = request_broker_event(
        EVOLUTION_JOURNAL_WRITE_REQUEST_EVENT,
        {
            "source": "extraction-apply-payloads",
            "trigger": "CLI",
            "journal": {"SOUL.md": "A short journal note."},
            "dry_run": True,
        },
        source="pytest",
    )

    snippet_metrics = snippet_response["responses"][0]["result"]["snippet_journal_metrics"]
    assert snippet_response["status"] == "ok"
    assert snippet_metrics["snippet_files_seen"] == 1
    assert snippet_metrics["snippet_files_skipped"] == 1
    assert snippet_metrics["journal_files_seen"] == 0
    assert snippet_metrics["target_files"] == {
        "snippets": ["USER.snippets.md"],
        "journal": [],
    }

    journal_metrics = journal_response["responses"][0]["result"]["snippet_journal_metrics"]
    assert journal_response["status"] == "ok"
    assert journal_metrics["snippet_files_seen"] == 0
    assert journal_metrics["journal_files_seen"] == 1
    assert journal_metrics["journal_files_skipped"] == 1
    assert journal_metrics["target_files"] == {
        "snippets": [],
        "journal": ["SOUL.journal.md"],
    }


@pytest.mark.parametrize(
    ("event_type", "register_name", "payload", "error"),
    [
        (
            EVOLUTION_SNIPPET_WRITE_REQUEST_EVENT,
            "register_snippet_write_request_handler",
            {"source": "wrong", "snippets": {}},
            "payload.source must be extraction-apply-payloads",
        ),
        (
            EVOLUTION_SNIPPET_WRITE_REQUEST_EVENT,
            "register_snippet_write_request_handler",
            {"snippets": {}},
            "payload.source must be extraction-apply-payloads",
        ),
        (
            EVOLUTION_SNIPPET_WRITE_REQUEST_EVENT,
            "register_snippet_write_request_handler",
            {"source": "extraction-apply-payloads", "snippets": []},
            "payload.snippets must be an object",
        ),
        (
            EVOLUTION_SNIPPET_WRITE_REQUEST_EVENT,
            "register_snippet_write_request_handler",
            {"source": "extraction-apply-payloads", "snippets": {}, "journal": {"SOUL.md": "not allowed"}},
            "payload.journal must be empty for snippet-only writes",
        ),
        (
            EVOLUTION_JOURNAL_WRITE_REQUEST_EVENT,
            "register_journal_write_request_handler",
            {"source": "wrong", "journal": {}},
            "payload.source must be extraction-apply-payloads",
        ),
        (
            EVOLUTION_JOURNAL_WRITE_REQUEST_EVENT,
            "register_journal_write_request_handler",
            {"journal": {}},
            "payload.source must be extraction-apply-payloads",
        ),
        (
            EVOLUTION_JOURNAL_WRITE_REQUEST_EVENT,
            "register_journal_write_request_handler",
            {"source": "extraction-apply-payloads", "journal": []},
            "payload.journal must be an object",
        ),
        (
            EVOLUTION_JOURNAL_WRITE_REQUEST_EVENT,
            "register_journal_write_request_handler",
            {"source": "extraction-apply-payloads", "journal": {}, "snippets": {"USER.md": ["not allowed"]}},
            "payload.snippets must be empty for journal-only writes",
        ),
    ],
)
def test_split_snippet_journal_request_handlers_reject_invalid_payloads_fail_soft(
    monkeypatch,
    tmp_path,
    event_type,
    register_name,
    payload,
    error,
):
    set_adapter(TestAdapter(tmp_path))
    import core.runtime.events as events
    import core.plugins.insightdb_contract as insightdb_contract

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)
    monkeypatch.setattr("lib.fail_policy.is_fail_hard_enabled", lambda: False)
    getattr(insightdb_contract, register_name)()

    response = request_broker_event(event_type, payload, source="pytest")

    assert response["status"] == "failed"
    result = response["responses"][0]["result"]
    assert result["status"] == "failed"
    assert result["error"] == error
    metrics = result["snippet_journal_metrics"]
    assert metrics["status"] == "failed"
    assert metrics["snippet_files_seen"] == 0
    assert metrics["journal_files_seen"] == 0
    assert metrics["target_files"] == {"snippets": [], "journal": []}
    assert metrics["errors"] == [error]


def test_apply_extracted_payloads_request_mode_uses_split_snippet_journal_events(monkeypatch, tmp_path):
    set_adapter(TestAdapter(tmp_path))
    import ingest.extract as extract_mod

    called_events = []
    direct_called = False

    def fake_publish(result, **_kwargs):
        result["facts_stored"] = 0
        return []

    def fake_direct_snippet_journal(*_args, **_kwargs):
        nonlocal direct_called
        direct_called = True
        raise AssertionError("request mode must not fall back to the direct snippet/journal helper")

    def fake_request(event_type, payload, **kwargs):
        called_events.append((event_type, payload, kwargs))
        if event_type == EVOLUTION_SNIPPET_WRITE_REQUEST_EVENT:
            metrics = {
                "status": "ok",
                "snippet_files_seen": 1,
                "snippet_items_seen": 1,
                "snippet_files_written": 1,
                "snippet_items_written": 1,
                "snippet_files_skipped": 0,
                "journal_files_seen": 0,
                "journal_files_written": 0,
                "journal_files_skipped": 0,
                "target_files": {"snippets": ["SOUL.snippets.md"], "journal": []},
                "errors": [],
            }
        elif event_type == EVOLUTION_JOURNAL_WRITE_REQUEST_EVENT:
            metrics = {
                "status": "ok",
                "snippet_files_seen": 0,
                "snippet_items_seen": 0,
                "snippet_files_written": 0,
                "snippet_items_written": 0,
                "snippet_files_skipped": 0,
                "journal_files_seen": 1,
                "journal_files_written": 1,
                "journal_files_skipped": 0,
                "target_files": {"snippets": [], "journal": ["SOUL.journal.md"]},
                "errors": [],
            }
        else:
            raise AssertionError(f"unexpected request event: {event_type}")
        return {
            "status": "ok",
            "responses": [
                {
                    "datastore_id": "insightdb",
                    "status": "ok",
                    "result": {
                        "status": "ok",
                        "snippet_journal_metrics": metrics,
                    },
                }
            ],
        }

    monkeypatch.setattr("core.plugins.memorydb_contract.run_extraction_publish_payload", fake_publish)
    monkeypatch.setattr("core.plugins.insightdb_contract.run_snippet_journal_write_payload", fake_direct_snippet_journal)
    monkeypatch.setattr("core.plugins.insightdb_contract.register_snippet_write_request_handler", lambda: None)
    monkeypatch.setattr("core.plugins.insightdb_contract.register_journal_write_request_handler", lambda: None)
    monkeypatch.setattr("core.runtime.events.request_broker_event", fake_request)

    payload = {
        "raw_facts": [],
        "raw_snippets": {"SOUL.md": ["Keep split event routing explicit."]},
        "raw_journal": {"SOUL.md": "A combined request-mode journal note."},
        "raw_project_logs": {},
        "facts": [],
        "snippets": {},
        "journal": {},
        "project_logs": {},
        "project_log_metrics": {},
        "facts_stored": 0,
        "facts_skipped": 0,
        "edges_created": 0,
        "dry_run": False,
    }

    applied = extract_mod.apply_extracted_payloads(
        payload,
        owner_id="test",
        label="rolling-flush",
        session_id="sess-combined-routing",
        write_snippets=True,
        write_journal=True,
        dry_run=False,
        snippet_journal_write_mode="request",
    )

    assert direct_called is False
    assert [event_type for event_type, _payload, _kwargs in called_events] == [
        EVOLUTION_SNIPPET_WRITE_REQUEST_EVENT,
        EVOLUTION_JOURNAL_WRITE_REQUEST_EVENT,
    ]
    assert EVOLUTION_SNIPPET_JOURNAL_WRITE_REQUEST_EVENT not in [
        event_type for event_type, _payload, _kwargs in called_events
    ]
    snippet_event, journal_event = called_events
    assert snippet_event[1]["source"] == "extraction-apply-payloads"
    assert snippet_event[1]["snippets"] == {"SOUL.md": ["Keep split event routing explicit."]}
    assert snippet_event[1]["journal"] == {}
    assert snippet_event[2]["source"] == "ingest.extract.apply_extracted_payloads"
    assert journal_event[1]["snippets"] == {}
    assert journal_event[1]["journal"] == {"SOUL.md": "A combined request-mode journal note."}
    assert applied["snippet_journal_metrics"]["target_files"] == {
        "snippets": ["SOUL.snippets.md"],
        "journal": ["SOUL.journal.md"],
    }


@pytest.mark.parametrize(
    ("handler_name", "payload", "error"),
    [
        (
            "handle_snippet_write_request",
            {
                "payload": {
                    "source": "extraction-apply-payloads",
                    "snippets": {},
                    "journal": {"SOUL.md": "not allowed"},
                },
            },
            "payload.journal must be empty for snippet-only writes",
        ),
        (
            "handle_journal_write_request",
            {
                "payload": {
                    "source": "extraction-apply-payloads",
                    "journal": {},
                    "snippets": {"USER.md": ["not allowed"]},
                },
            },
            "payload.snippets must be empty for journal-only writes",
        ),
    ],
)
def test_split_snippet_journal_request_handlers_warn_before_fail_hard_raise(
    caplog,
    monkeypatch,
    handler_name,
    payload,
    error,
):
    import core.plugins.insightdb_contract as insightdb_contract

    monkeypatch.setattr("lib.fail_policy.is_fail_hard_enabled", lambda: True)

    with caplog.at_level("WARNING", logger="core.plugins.insightdb_contract"):
        with pytest.raises(ValueError, match=error):
            getattr(insightdb_contract, handler_name)(payload)

    assert error in caplog.text


def test_request_session_ingest_log_matches_direct_session_projection(monkeypatch, tmp_path):
    set_adapter(TestAdapter(tmp_path))
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))

    from core.plugins.sessiondb_contract import register_session_ingest_log_request_handler
    from core.services.datastore_bridge import DatastoreBridge
    from core.services.session_memory_bridge import DatastoreSessionMemoryBridge
    import datastore.memorydb.memory_graph as mg
    from datastore.memorydb.memory_graph import MemoryGraph
    from datastore.sessiondb import session_store
    from ingest import session_logs_ingest

    memory = MemoryGraph(db_path=tmp_path / "memory.db")
    monkeypatch.setattr(memory, "get_embedding", lambda *_args, **_kwargs: None)
    bridge = DatastoreSessionMemoryBridge(memory_service=memory, datastore_bridge=DatastoreBridge())
    monkeypatch.setattr("ingest.session_logs_ingest.get_session_memory_bridge", lambda: bridge)

    provider_payload = {
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 900, "output_tokens": 12},
    }
    session_lines = [
        json.dumps({"type": "session_meta", "payload": {"cwd": str(tmp_path)}}),
        json.dumps({"role": "user", "content": "Mira left the kiln key inside the green ledger."}),
        json.dumps({"role": "assistant", "content": json.dumps(provider_payload)}),
        json.dumps({"role": "assistant", "content": "Logged for session recall."}),
        json.dumps({"role": "user", "content": "Noor hid the harbor map inside the amber folder."}),
        json.dumps({"role": "assistant", "content": "That belongs to a separate pair."}),
    ]
    raw_direct = tmp_path / "direct-session.jsonl"
    raw_broker = tmp_path / "broker-session.jsonl"
    raw_active = tmp_path / "active-session.jsonl"
    for raw_session in (raw_direct, raw_broker, raw_active):
        raw_session.write_text("\n".join(session_lines) + "\n", encoding="utf-8")
    base_payload = {
        "owner_id": "owner-parity",
        "label": "SessionEnd",
        "source_channel": "codex",
        "conversation_id": "conv-parity",
        "participant_ids": ["user:owner", "agent:quaid"],
        "participant_aliases": {"Operator": "user:owner"},
        "message_count": 6,
        "topic_hint": "kiln key",
    }

    direct = session_logs_ingest.run(session_id="sess-direct", transcript_path=str(raw_direct), **base_payload)
    register_session_ingest_log_request_handler()
    broker = request_broker_event(
        SESSION_INGEST_LOG_REQUEST_EVENT,
        {"session_id": "sess-broker", "transcript_path": str(raw_broker), **base_payload},
        source="pytest",
    )
    broker_result = broker["responses"][0]["result"]

    assert broker["status"] == "ok"
    assert broker["responses"][0]["datastore_id"] == "sessiondb"
    assert direct["status"] == "indexed"
    assert broker_result["status"] == "indexed"
    for key in ("message_count", "pairs_stored", "microchunks_stored", "source_kind"):
        assert broker_result[key] == direct[key]

    emit_event(
        name="session.ingest_log",
        payload={"session_id": "sess-active", "transcript_path": str(raw_active), **base_payload},
        source="pytest",
    )
    active = process_events(limit=5, names=["session.ingest_log"])
    assert active["processed"] == 1
    assert active["failed"] == 0
    active_result = active["details"][0]["result"]["result"]
    assert active_result["status"] == "indexed"
    for key in ("message_count", "pairs_stored", "microchunks_stored", "source_kind"):
        assert active_result[key] == direct[key]

    direct_session = session_store.load_session("sess-direct", owner_id="owner-parity")
    broker_session = session_store.load_session("sess-broker", owner_id="owner-parity")
    active_session = session_store.load_session("sess-active", owner_id="owner-parity")
    assert direct_session and broker_session and active_session
    for row in (direct_session, broker_session, active_session):
        assert "Mira left the kiln key" in row["transcript_text"]
        assert "session_meta" not in row["transcript_text"]
        assert "stop_reason" not in row["transcript_text"]
        assert row["source_channel"] == "codex"
        assert row["conversation_id"] == "conv-parity"
        assert row["participant_ids"] == ["user:owner", "agent:quaid"]
        assert row["participant_aliases"] == {"Operator": "user:owner"}

    direct_rows = memory.list_session_chunks(owner_id="owner-parity", session_id="sess-direct")
    broker_rows = memory.list_session_chunks(owner_id="owner-parity", session_id="sess-broker")
    active_rows = memory.list_session_chunks(owner_id="owner-parity", session_id="sess-active")
    assert direct_rows and broker_rows and active_rows
    assert len(broker_rows) == len(direct_rows)
    assert len(active_rows) == len(direct_rows)
    assert len({row["message_pair_id"] for row in broker_rows}) >= 2
    assert len({row["message_pair_id"] for row in active_rows}) >= 2
    for rows, raw_session in ((broker_rows, raw_broker), (active_rows, raw_active)):
        for row in rows:
            assert row["source_id"] == str(raw_session)
            assert row["source_channel"] == "codex"
            assert row["source_conversation_id"] == "conv-parity"
            assert row["conversation_id"] == "conv-parity"
            assert row["chunk_kind"] == "micro"
            assert row["parent_chunk_id"]
            assert row["message_pair_id"]
            assert row["microchunk_id"]

    monkeypatch.setattr(mg, "get_graph", lambda: memory)
    recall_rows, recall_meta, _bundle = mg._run_recall_store_plan(
        "kiln key green ledger",
        stores=["session_chunks"],
        limit=3,
        owner_id="owner-parity",
        min_similarity=0.0,
        planner_profile="off",
        planned_queries=["kiln key green ledger"],
        planner_meta={"planned_stores": ["session_chunks"]},
        fast_mode=False,
        common_kwargs={"source_channel": "codex", "max_chunk_tokens": 80, "max_total_chunk_tokens": 200},
    )
    assert any(row["session_id"] == "sess-broker" and "green ledger" in row["text"] for row in recall_rows)
    assert recall_meta["store_runs"][0]["store"] == "session_chunks"

    center = next(row for row in broker_rows if "kiln key" in row["text"])
    other_pair_ids = {
        row["message_pair_id"]
        for row in broker_rows
        if row["message_pair_id"] != center["message_pair_id"]
    }
    expanded = bridge.expand_microchunk(center["microchunk_id"], owner_id="owner-parity", after=10)
    assert expanded
    assert expanded["pair"]["session_id"] == "sess-broker"
    assert all(row["session_id"] == "sess-broker" for row in expanded["window"])
    assert all(row["session_id"] == "sess-broker" for row in expanded["microchunk_window"])
    assert all(row["pair_id"] == center["message_pair_id"] for row in expanded["microchunk_window"])
    assert not any(row["pair_id"] in other_pair_ids for row in expanded["microchunk_window"])
    assert not any("harbor map" in row["text"] for row in expanded["microchunk_window"])


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


def test_emit_event_caps_queue_length(monkeypatch, tmp_path, caplog):
    adapter = TestAdapter(tmp_path); set_adapter(adapter); iroot = adapter.instance_root()

    import core.runtime.events as events

    monkeypatch.setattr(events, "MAX_EVENT_QUEUE", 3)
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)
    with caplog.at_level("WARNING"):
        for i in range(5):
            emitted = emit_event(name="session.reset", payload={"idx": i}, source="pytest")
    assert emitted["dropped"] == 1
    assert "Event queue overflow" in caplog.text

    queue_path = get_runtime_root(iroot) / "events" / "queue.json"
    payload = json.loads(queue_path.read_text(encoding="utf-8"))
    queued = payload.get("events") or []
    assert len(queued) == 3
    assert [int(item.get("payload", {}).get("idx")) for item in queued] == [2, 3, 4]


def test_emit_event_queue_overflow_raises_under_fail_hard(monkeypatch, tmp_path):
    set_adapter(TestAdapter(tmp_path))

    import core.runtime.events as events

    monkeypatch.setattr(events, "MAX_EVENT_QUEUE", 1)
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: True)
    emit_event(name="session.reset", payload={"idx": 1}, source="pytest")

    with pytest.raises(RuntimeError, match="Event queue overflow"):
        emit_event(name="session.reset", payload={"idx": 2}, source="pytest")


def test_emit_event_validates_envelope_before_enqueue(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)

    import core.runtime.events as events

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: True)
    with pytest.raises(RuntimeError, match="Invalid event envelope"):
        emit_event(name="session.reset", payload=["not", "an", "object"], source="pytest")  # type: ignore[arg-type]

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)
    event = emit_event(name="session.reset", payload=["not", "an", "object"], source="pytest")  # type: ignore[arg-type]
    assert event["validation_errors"] == ["payload must be an object"]

    queue_path = get_runtime_root(adapter.instance_root()) / "events" / "queue.json"
    queued = json.loads(queue_path.read_text(encoding="utf-8")).get("events") or []
    assert queued[0]["validation_errors"] == ["payload must be an object"]


def test_emit_event_dedup_ignores_failed_events_for_retry(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)

    import core.runtime.events as events

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)

    def _failed(_event):
        return {"status": "failed", "error": "first attempt failed"}

    original = EVENT_HANDLERS["session.reset"]
    try:
        register_event_handler("session.reset", _failed, force=True)
        first = emit_event(
            name="session.reset",
            payload={"attempt": 1},
            source="pytest",
            session_id="sess-retry",
            idempotency_key="retry-key",
        )
        out = process_events(limit=5, names=["session.reset"])
        assert out["failed"] == 1

        second = emit_event(
            name="session.reset",
            payload={"attempt": 2},
            source="pytest",
            session_id="sess-retry",
            idempotency_key="retry-key",
        )
    finally:
        register_event_handler("session.reset", original, force=True)

    assert second["duplicate"] is False
    queue_path = get_runtime_root(adapter.instance_root()) / "events" / "queue.json"
    queued = json.loads(queue_path.read_text(encoding="utf-8")).get("events") or []
    matching = [event for event in queued if event.get("idempotency_key") == "retry-key"]
    assert [event["id"] for event in matching] == [first["id"], second["id"]]
    assert matching[0]["status"] == "failed"
    assert matching[1]["status"] == "pending"


def test_emit_event_dedup_is_session_scoped_when_sessions_present(tmp_path):
    set_adapter(TestAdapter(tmp_path))

    first = emit_event(
        name="session.reset",
        payload={"session": 1},
        source="pytest",
        session_id="sess-one",
        idempotency_key="same-key",
    )
    second = emit_event(
        name="session.reset",
        payload={"session": 2},
        source="pytest",
        session_id="sess-two",
        idempotency_key="same-key",
    )

    assert first["duplicate"] is False
    assert second["duplicate"] is False
    queued = list_events(status="pending", limit=10)
    matching = [event for event in queued if event.get("idempotency_key") == "same-key"]
    assert {event["session_id"] for event in matching} == {"sess-one", "sess-two"}


def test_process_events_treats_error_and_nacked_status_as_failed(monkeypatch, tmp_path):
    set_adapter(TestAdapter(tmp_path))

    import core.runtime.events as events

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)

    def _error(event):
        return {"status": event["payload"]["status"], "error": "bad handler status"}

    original = EVENT_HANDLERS["session.reset"]
    try:
        register_event_handler("session.reset", _error, force=True)
        emit_event(name="session.reset", payload={"status": "error"}, source="pytest")
        emit_event(name="session.reset", payload={"status": "nacked"}, source="pytest")
        out = process_events(limit=5, names=["session.reset"])
    finally:
        register_event_handler("session.reset", original, force=True)

    assert out["processed"] == 0
    assert out["failed"] == 2
    assert [detail["status"] for detail in out["details"]] == ["failed", "failed"]


def test_process_events_commits_statuses_before_failhard_raise(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)

    import core.runtime.events as events

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: True)
    calls = []

    def _mixed(event):
        idx = int(event["payload"]["idx"])
        calls.append(idx)
        if idx == 2:
            return {"status": "failed", "error": "second failed"}
        return {"status": "processed"}

    original = EVENT_HANDLERS["session.reset"]
    try:
        register_event_handler("session.reset", _mixed, force=True)
        emit_event(name="session.reset", payload={"idx": 1}, source="pytest")
        emit_event(name="session.reset", payload={"idx": 2}, source="pytest")
        with pytest.raises(RuntimeError, match="second failed"):
            process_events(limit=5, names=["session.reset"])

        queue_path = get_runtime_root(adapter.instance_root()) / "events" / "queue.json"
        queued = json.loads(queue_path.read_text(encoding="utf-8")).get("events") or []
        assert [event["status"] for event in queued] == ["processed", "failed"]

        out = process_events(limit=5, names=["session.reset"])
    finally:
        register_event_handler("session.reset", original, force=True)

    assert out["touched"] == 0
    assert calls == [1, 2]


def test_process_events_snapshots_handlers_for_claimed_batch(monkeypatch, tmp_path):
    set_adapter(TestAdapter(tmp_path))

    import core.runtime.events as events

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)
    seen = []

    def _replacement(_event):
        seen.append("replacement")
        return {"status": "processed"}

    def _first(event):
        seen.append(event["payload"]["idx"])
        register_event_handler("session.reset", _replacement, force=True)
        return {"status": "processed"}

    original = EVENT_HANDLERS["session.reset"]
    try:
        register_event_handler("session.reset", _first, force=True)
        emit_event(name="session.reset", payload={"idx": 1}, source="pytest")
        emit_event(name="session.reset", payload={"idx": 2}, source="pytest")
        out = process_events(limit=5, names=["session.reset"])
    finally:
        register_event_handler("session.reset", original, force=True)

    assert out["processed"] == 2
    assert seen == [1, 2]


def test_process_events_reports_name_filter_skips(monkeypatch, tmp_path):
    set_adapter(TestAdapter(tmp_path))
    monkeypatch.setattr("core.runtime.events._is_fail_hard_enabled", lambda: False)

    emit_event(name="session.reset", payload={"idx": 1}, source="pytest")
    emit_event(name="notification.delayed", payload={"message": "queued"}, source="pytest")

    out = process_events(limit=5, names=["notification.delayed"])
    assert out["processed"] == 1
    assert out["skipped"] == 1


def test_process_events_expires_stale_pending_events(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)

    import core.runtime.events as events

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: True)
    called = []

    def _handler(_event):
        called.append(True)
        return {"status": "processed"}

    original = EVENT_HANDLERS["session.reset"]
    try:
        register_event_handler("session.reset", _handler, force=True)
        emitted = emit_event(name="session.reset", payload={"idx": 1}, source="pytest")
        queue_path = get_runtime_root(adapter.instance_root()) / "events" / "queue.json"
        payload = json.loads(queue_path.read_text(encoding="utf-8"))
        payload["events"][0]["created_at"] = "2020-01-01T00:00:00+00:00"
        queue_path.write_text(json.dumps(payload), encoding="utf-8")

        out = process_events(limit=5, names=["session.reset"], max_age_seconds=60)
    finally:
        register_event_handler("session.reset", original, force=True)

    assert called == []
    assert out["expired"] == 1
    assert out["details"][0]["id"] == emitted["id"]
    assert out["details"][0]["status"] == "expired"
    queued = list_events(status="expired", limit=10)
    assert queued[0]["result"]["reason"] == "event_exceeded_max_age"


def test_process_events_expiry_honors_quaid_now(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)

    import core.runtime.events as events

    monkeypatch.setenv("QUAID_NOW", "2026-03-11T00:00:00Z")
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: True)
    called = []

    def _handler(_event):
        called.append(True)
        return {"status": "processed"}

    original = EVENT_HANDLERS["session.reset"]
    try:
        register_event_handler("session.reset", _handler, force=True)
        emitted = emit_event(name="session.reset", payload={"idx": 1}, source="pytest")
        queue_path = get_runtime_root(adapter.instance_root()) / "events" / "queue.json"
        payload = json.loads(queue_path.read_text(encoding="utf-8"))
        payload["events"][0]["created_at"] = "2026-03-10T23:59:30+00:00"
        queue_path.write_text(json.dumps(payload), encoding="utf-8")

        out = process_events(limit=5, names=["session.reset"], max_age_seconds=60)
    finally:
        register_event_handler("session.reset", original, force=True)

    assert called == [True]
    assert out["processed"] == 1
    assert out["expired"] == 0
    queued = list_events(status="processed", limit=10)
    assert queued[0]["id"] == emitted["id"]


def test_process_events_recovers_stale_processing_events(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)

    import core.runtime.events as events

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)
    emitted = emit_event(name="session.reset", payload={"idx": 1}, source="pytest")
    queue_path = get_runtime_root(adapter.instance_root()) / "events" / "queue.json"
    payload = json.loads(queue_path.read_text(encoding="utf-8"))
    payload["events"][0]["status"] = "processing"
    payload["events"][0]["processing_started_at"] = "2020-01-01T00:00:00+00:00"
    queue_path.write_text(json.dumps(payload), encoding="utf-8")

    out = process_events(limit=5, names=["session.reset"])

    assert out["processed"] == 1
    queued = list_events(status="processed", limit=10)
    assert queued[0]["id"] == emitted["id"]
    assert "processing_started_at" not in queued[0]


def test_lifecycle_daemon_signal_rejects_transcript_outside_managed_roots(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)
    outside = tmp_path.parent / f"{tmp_path.name}-outside.jsonl"
    outside.write_text('{"role":"user","content":"outside"}\n', encoding="utf-8")

    import core.runtime.events as events

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)
    emit_event(
        name="session.agent_end",
        payload={"daemon_signal": {"enabled": True, "transcript_path": str(outside)}},
        source="pytest",
        session_id="sess-outside",
        owner_id="owner-life",
    )

    out = process_events(limit=5, names=["session.agent_end"])
    assert out["processed"] == 1
    assert out["failed"] == 0
    result = out["details"][0]["result"]
    assert result["daemon_signal_queued"] is False
    assert "outside Quaid-managed roots" in result["daemon_signal_error"]


def test_docs_ingest_rejects_transcript_outside_managed_roots(monkeypatch, tmp_path):
    set_adapter(TestAdapter(tmp_path))
    outside = tmp_path.parent / f"{tmp_path.name}-docs-outside.jsonl"
    outside.write_text("session transcript", encoding="utf-8")

    import core.runtime.events as events

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(
        events,
        "run_docs_ingest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unsafe path must not ingest")),
    )
    emit_event(
        name="docs.ingest_transcript",
        payload={"transcript_path": str(outside), "label": "Compaction"},
        source="pytest",
    )

    out = process_events(limit=5, names=["docs.ingest_transcript"])
    assert out["processed"] == 0
    assert out["failed"] == 1
    assert "outside Quaid-managed roots" in str(out["details"][0]["result"]["error"])


def test_queue_delayed_notification_reports_when_different_event_processed(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)

    import core.runtime.events as events

    queued_messages = []
    monkeypatch.setattr(events, "_queue_delayed_llm_request", lambda **kwargs: queued_messages.append(kwargs["message"]) or True)
    emit_event(
        name="notification.delayed",
        payload={"message": "older notice", "kind": "janitor", "priority": "normal"},
        source="pytest",
    )

    result = queue_delayed_notification("new notice", kind="janitor", priority="normal", source="pytest")

    assert result["status"] == "queued_not_processed"
    assert queued_messages == ["older notice"]
    pending = list_events(status="pending", limit=10)
    assert [event["payload"]["message"] for event in pending] == ["new notice"]


def test_emit_event_trims_history_file_before_append(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path); set_adapter(adapter); iroot = adapter.instance_root()

    import core.runtime.events as events

    monkeypatch.setattr(events, "MAX_HISTORY_JSONL_BYTES", 120)
    monkeypatch.setattr(events, "HISTORY_TRIM_TARGET_BYTES", 60)

    history_path = get_runtime_root(iroot) / "events" / "history.jsonl"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    seed = "".join(
        json.dumps({"ts": f"t{i}", "op": "seed", "event": {"id": i}}) + "\n"
        for i in range(80)
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


def test_emit_event_appends_when_history_trim_fails_without_fail_hard(monkeypatch, tmp_path, caplog):
    adapter = TestAdapter(tmp_path); set_adapter(adapter); iroot = adapter.instance_root()

    import core.runtime.events as events

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(events, "MAX_HISTORY_JSONL_BYTES", 120)
    monkeypatch.setattr(events, "HISTORY_TRIM_TARGET_BYTES", 60)

    history_path = get_runtime_root(iroot) / "events" / "history.jsonl"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    seed = "".join(
        json.dumps({"ts": f"t{i}", "op": "seed", "event": {"id": i}}) + "\n"
        for i in range(80)
    )
    history_path.write_text(seed, encoding="utf-8")

    real_replace = os.replace

    def fail_trim_replace(src, dst):
        if Path(dst) == history_path:
            raise OSError("trim blocked")
        return real_replace(src, dst)

    monkeypatch.setattr(events.os, "replace", fail_trim_replace)

    with caplog.at_level("WARNING", logger="core.runtime.events"):
        emit_event(name="session.reset", payload={"reason": "trim-failed"}, source="pytest")

    raw = history_path.read_text(encoding="utf-8")
    lines = [line for line in raw.splitlines() if line.strip()]
    assert len(raw.encode("utf-8")) > len(seed.encode("utf-8"))
    assert "appending new entry anyway" in caplog.text
    assert not list(history_path.parent.glob("tmp*"))
    last = json.loads(lines[-1])
    assert last.get("op") == "emit"
    assert last.get("event", {}).get("payload", {}).get("reason") == "trim-failed"


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


def test_process_events_handler_error_marks_failed_when_not_fail_hard(monkeypatch, tmp_path, caplog):
    set_adapter(TestAdapter(tmp_path))

    import core.runtime.events as events

    def _boom(*_args, **_kwargs):
        raise RuntimeError("handler exploded")

    register_event_handler("session.reset", _boom, force=True)
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)

    emit_event(
        name="session.reset",
        payload={"session_id": "event-handler-soft-failure"},
        source="pytest",
    )

    with caplog.at_level("ERROR", logger="core.runtime.events"):
        out = process_events(limit=5, names=["session.reset"])

    assert out["processed"] == 0
    assert out["failed"] >= 1
    assert "Event handler session.reset failed" in caplog.text
    assert "handler exploded" in caplog.text


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


def test_event_file_lock_warns_and_continues_on_lock_failure_when_not_fail_hard(
    monkeypatch,
    tmp_path,
    caplog,
):
    import core.runtime.events as events

    fake_fcntl = types.SimpleNamespace(
        LOCK_EX=1,
        LOCK_UN=2,
        flock=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("lock unavailable")),
    )
    monkeypatch.setitem(sys.modules, "fcntl", fake_fcntl)
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)

    with caplog.at_level("WARNING", logger="core.runtime.events"):
        with events._file_lock(tmp_path / "events.lock"):
            pass

    assert "Failed to acquire event file lock" in caplog.text
    assert "lock unavailable" in caplog.text


def test_event_file_lock_raises_on_lock_failure_when_fail_hard(monkeypatch, tmp_path):
    import core.runtime.events as events

    fake_fcntl = types.SimpleNamespace(
        LOCK_EX=1,
        LOCK_UN=2,
        flock=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("lock unavailable")),
    )
    monkeypatch.setitem(sys.modules, "fcntl", fake_fcntl)
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="Failed to acquire event file lock") as excinfo:
        with events._file_lock(tmp_path / "events.lock"):
            pass

    assert isinstance(excinfo.value.__cause__, OSError)
    assert "lock unavailable" in str(excinfo.value.__cause__)


def test_event_file_lock_release_failure_respects_fail_hard(monkeypatch, tmp_path):
    import core.runtime.events as events

    calls = []

    def _flock(_handle, operation):
        calls.append(operation)
        if operation == fake_fcntl.LOCK_UN:
            raise OSError("unlock unavailable")

    fake_fcntl = types.SimpleNamespace(LOCK_EX=1, LOCK_UN=2, flock=_flock)
    monkeypatch.setitem(sys.modules, "fcntl", fake_fcntl)
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="Failed to release event file lock") as excinfo:
        with events._file_lock(tmp_path / "events.lock"):
            pass

    assert calls == [fake_fcntl.LOCK_EX, fake_fcntl.LOCK_UN]
    assert isinstance(excinfo.value.__cause__, OSError)
    assert "unlock unavailable" in str(excinfo.value.__cause__)


def test_event_file_lock_release_failure_does_not_mask_body_exception(
    monkeypatch,
    tmp_path,
    caplog,
):
    import core.runtime.events as events

    def _flock(_handle, operation):
        if operation == fake_fcntl.LOCK_UN:
            raise OSError("unlock unavailable")

    fake_fcntl = types.SimpleNamespace(LOCK_EX=1, LOCK_UN=2, flock=_flock)
    monkeypatch.setitem(sys.modules, "fcntl", fake_fcntl)
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: True)

    with caplog.at_level("WARNING", logger="core.runtime.events"):
        with pytest.raises(ValueError, match="body failed"):
            with events._file_lock(tmp_path / "events.lock"):
                raise ValueError("body failed")

    assert "Failed to release event file lock" in caplog.text
    assert "unlock unavailable" in caplog.text


def test_register_event_handler_does_not_overwrite_without_force(caplog):
    original = EVENT_HANDLERS["session.reset"]

    def _replacement(_event):
        return {"status": "processed", "note": "replacement"}

    with caplog.at_level("WARNING"):
        register_event_handler("session.reset", _replacement)
    assert EVENT_HANDLERS["session.reset"] is original
    assert "skipped overwrite" in caplog.text


def test_register_event_handler_rejects_unknown_or_request_events():
    def _handler(_event):
        return {"status": "processed"}

    with pytest.raises(ValueError, match="not registered"):
        register_event_handler("totally.unknown.event", _handler)
    with pytest.raises(ValueError, match="not an active processable event"):
        register_event_handler("recall.memory.request.v1", _handler)
    with pytest.raises(TypeError, match="not callable"):
        register_event_handler("session.reset", object())  # type: ignore[arg-type]


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
