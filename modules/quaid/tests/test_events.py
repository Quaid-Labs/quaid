import json
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
    assert any(c.get("name") == SESSION_INGEST_LOG_REQUEST_EVENT and c.get("delivery_mode") == "request" for c in caps)
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
    with pytest.raises(RuntimeError, match="register boom"):
        dispatch_broker_events(limit=5, names=[DOCS_PROJECT_MAINTENANCE_OBSERVED_EVENT])

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
    with pytest.raises(RuntimeError, match="index boom"):
        dispatch_broker_events(limit=5, names=[DOCS_PROJECT_MAINTENANCE_OBSERVED_EVENT])

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
    assert result["registry_sync"] == {"registered": 3, "unregistered": 1, "project_md_refreshed": 1}
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

    called = {}

    def _fake_helper(payload):
        called.update(payload)
        return {"status": "indexed", "session_id": payload["session_id"], "chunks": 2}

    monkeypatch.setattr("core.plugins.memorydb_contract.run_session_ingest_payload", _fake_helper)

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


def test_event_process_session_ingest_log_has_no_runtime_direct_ingest_call():
    import core.runtime.events as events

    source = Path(events.__file__).read_text(encoding="utf-8")

    assert "run_session_logs_ingest" not in source


def test_event_process_session_ingest_log_failed_result_marks_failed(monkeypatch, tmp_path):
    set_adapter(TestAdapter(tmp_path))

    import core.runtime.events as events

    monkeypatch.setattr(
        "core.plugins.memorydb_contract.run_session_ingest_payload",
        lambda _payload: {"status": "failed", "error": "simulated ingest failure"},
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
        "core.plugins.memorydb_contract.run_session_ingest_payload",
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


def test_request_session_ingest_log_runs_memorydb_handler(monkeypatch, tmp_path):
    set_adapter(TestAdapter(tmp_path))
    from core.plugins.memorydb_contract import register_session_ingest_log_request_handler

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
    assert response["responses"][0]["datastore_id"] == "memorydb"
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
    from core.plugins.memorydb_contract import register_session_ingest_log_request_handler

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


def test_memorydb_session_ingest_wrapper_delegates_to_distinct_sessiondb_helper(monkeypatch):
    import core.plugins.memorydb_contract as memorydb_contract
    import core.plugins.sessiondb_contract as sessiondb_contract

    assert memorydb_contract.run_session_ingest_payload is not sessiondb_contract.run_session_ingest_payload

    called = {}

    def _fake_sessiondb_helper(payload):
        called.update(payload)
        return {"status": "indexed", "session_id": payload["session_id"]}

    monkeypatch.setattr(sessiondb_contract, "run_session_ingest_payload", _fake_sessiondb_helper)

    result = memorydb_contract.run_session_ingest_payload({"session_id": "sess-wrapper"})

    assert result == {"status": "indexed", "session_id": "sess-wrapper"}
    assert called == {"session_id": "sess-wrapper"}
    assert memorydb_contract.run_session_ingest_payload is not sessiondb_contract.run_session_ingest_payload


def test_memorydb_session_ingest_wrapper_propagates_helper_failure_without_registration(
    monkeypatch,
    tmp_path,
):
    set_adapter(TestAdapter(tmp_path))
    import core.plugins.memorydb_contract as memorydb_contract
    import core.plugins.sessiondb_contract as sessiondb_contract
    import core.runtime.events as events

    def _fail(_payload):
        raise RuntimeError("sessiondb helper boom")

    monkeypatch.setattr(sessiondb_contract, "run_session_ingest_payload", _fail)

    with pytest.raises(RuntimeError, match="sessiondb helper boom"):
        memorydb_contract.run_session_ingest_payload({"session_id": "sess-fail"})

    memorydb_contract.register_session_ingest_log_request_handler()
    with events._REQUEST_EVENT_HANDLERS_LOCK:
        handlers = list(events._REQUEST_EVENT_HANDLERS.get(SESSION_INGEST_LOG_REQUEST_EVENT) or [])

    assert [(item["datastore_id"], item["handler"]) for item in handlers] == [
        ("memorydb", memorydb_contract.handle_session_ingest_log_request)
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


def test_request_snippet_journal_write_runs_evolutiondb_handler(monkeypatch, tmp_path):
    set_adapter(TestAdapter(tmp_path))
    import core.plugins.evolutiondb_contract as evolutiondb_contract
    from core.plugins.evolutiondb_contract import register_snippet_journal_write_request_handler

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

    monkeypatch.setattr(evolutiondb_contract, "run_snippet_journal_write_payload", _fake_write)

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
    assert row["datastore_id"] == "evolutiondb"
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
    from core.plugins.evolutiondb_contract import register_snippet_journal_write_request_handler

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)

    register_snippet_journal_write_request_handler()
    response = request_broker_event(
        EVOLUTION_SNIPPET_JOURNAL_WRITE_REQUEST_EVENT,
        payload,
        source="pytest",
    )

    assert response["status"] == "failed"
    assert response["responses"][0]["datastore_id"] == "evolutiondb"
    assert response["responses"][0]["result"]["error"] == error


def test_split_snippet_journal_request_metadata_declared():
    from core.contracts.datastore import build_first_party_datastore_contracts
    from core.datastore_registry import get_datastore_manifest

    manifest = get_datastore_manifest("evolutiondb")
    assert EVOLUTION_SNIPPET_WRITE_REQUEST_EVENT in manifest["request_handlers"]
    assert EVOLUTION_JOURNAL_WRITE_REQUEST_EVENT in manifest["request_handlers"]

    contract = build_first_party_datastore_contracts()["evolutiondb"]
    handlers = {spec.event_type: spec for spec in contract.list_request_handlers()}
    assert EVOLUTION_SNIPPET_WRITE_REQUEST_EVENT in handlers
    assert EVOLUTION_JOURNAL_WRITE_REQUEST_EVENT in handlers
    assert "core.plugins.evolutiondb_contract.handle_snippet_write_request" in handlers[
        EVOLUTION_SNIPPET_WRITE_REQUEST_EVENT
    ].replacement_targets
    assert "core.plugins.evolutiondb_contract.handle_journal_write_request" in handlers[
        EVOLUTION_JOURNAL_WRITE_REQUEST_EVENT
    ].replacement_targets


def test_split_snippet_journal_request_handlers_register_under_evolutiondb(tmp_path):
    set_adapter(TestAdapter(tmp_path))
    import core.runtime.events as events
    from core.plugins.evolutiondb_contract import (
        register_journal_write_request_handler,
        register_snippet_write_request_handler,
    )

    register_snippet_write_request_handler()
    register_journal_write_request_handler()

    with events._REQUEST_EVENT_HANDLERS_LOCK:
        snippet_handlers = list(events._REQUEST_EVENT_HANDLERS.get(EVOLUTION_SNIPPET_WRITE_REQUEST_EVENT) or [])
        journal_handlers = list(events._REQUEST_EVENT_HANDLERS.get(EVOLUTION_JOURNAL_WRITE_REQUEST_EVENT) or [])

    assert [handler["datastore_id"] for handler in snippet_handlers] == ["evolutiondb"]
    assert [handler["datastore_id"] for handler in journal_handlers] == ["evolutiondb"]


def test_split_snippet_journal_request_handlers_return_family_zero_metrics(tmp_path):
    set_adapter(TestAdapter(tmp_path))
    from core.plugins.evolutiondb_contract import (
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
    import core.plugins.evolutiondb_contract as evolutiondb_contract

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)
    monkeypatch.setattr("lib.fail_policy.is_fail_hard_enabled", lambda: False)
    getattr(evolutiondb_contract, register_name)()

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
                    "datastore_id": "evolutiondb",
                    "status": "ok",
                    "result": {
                        "status": "ok",
                        "snippet_journal_metrics": metrics,
                    },
                }
            ],
        }

    monkeypatch.setattr("core.plugins.memorydb_contract.run_extraction_publish_payload", fake_publish)
    monkeypatch.setattr("core.plugins.evolutiondb_contract.run_snippet_journal_write_payload", fake_direct_snippet_journal)
    monkeypatch.setattr("core.plugins.evolutiondb_contract.register_snippet_write_request_handler", lambda: None)
    monkeypatch.setattr("core.plugins.evolutiondb_contract.register_journal_write_request_handler", lambda: None)
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
    import core.plugins.evolutiondb_contract as evolutiondb_contract

    monkeypatch.setattr("lib.fail_policy.is_fail_hard_enabled", lambda: True)

    with caplog.at_level("WARNING", logger="core.plugins.evolutiondb_contract"):
        with pytest.raises(ValueError, match=error):
            getattr(evolutiondb_contract, handler_name)(payload)

    assert error in caplog.text


def test_request_session_ingest_log_matches_direct_session_projection(monkeypatch, tmp_path):
    set_adapter(TestAdapter(tmp_path))
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))

    from core.plugins.memorydb_contract import register_session_ingest_log_request_handler
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
