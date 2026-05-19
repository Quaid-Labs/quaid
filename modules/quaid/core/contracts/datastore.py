"""Datastore contract primitives for broker/listener migration.

M3 defines the target contract surface without activating production routing.
These classes declare replacement targets; they do not wrap legacy direct calls.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from core.datastore_registry import build_datastore_registry

DOMAIN_EVENT = "domain_event"
REQUEST = "request"
HANDLER_KINDS = {DOMAIN_EVENT, REQUEST}


def _required_text(value: Any, *, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} is required")
    return text


def _string_tuple(values: Iterable[Any] | None, *, label: str) -> Tuple[str, ...]:
    out = tuple(str(value or "").strip() for value in values or [] if str(value or "").strip())
    if not out:
        raise ValueError(f"{label} must contain at least one entry")
    return out


@dataclass(frozen=True)
class DatastoreHandlerSpec:
    event_type: str
    kind: str
    replacement_targets: Tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", _required_text(self.event_type, label="event_type"))
        kind = _required_text(self.kind, label="kind")
        if kind not in HANDLER_KINDS:
            raise ValueError(f"unsupported handler kind: {kind}")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(
            self,
            "replacement_targets",
            _string_tuple(self.replacement_targets, label="replacement_targets"),
        )


@dataclass(frozen=True)
class DatastoreResult:
    datastore_id: str
    event_type: str
    status: str
    handler: str
    result: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "datastore_id": self.datastore_id,
            "event_type": self.event_type,
            "status": self.status,
            "handler": self.handler,
            "result": dict(self.result or {}),
        }
        if self.error:
            payload["error"] = self.error
        return payload


def ack(datastore_id: str, event_type: str, handler: str, result: Optional[Dict[str, Any]] = None) -> DatastoreResult:
    return DatastoreResult(
        datastore_id=_required_text(datastore_id, label="datastore_id"),
        event_type=_required_text(event_type, label="event_type"),
        status="acked",
        handler=_required_text(handler, label="handler"),
        result=dict(result or {}),
    )


def nack(datastore_id: str, event_type: str, handler: str, error: str) -> DatastoreResult:
    return DatastoreResult(
        datastore_id=_required_text(datastore_id, label="datastore_id"),
        event_type=_required_text(event_type, label="event_type"),
        status="nacked",
        handler=_required_text(handler, label="handler"),
        error=_required_text(error, label="error"),
    )


class DatastoreIdempotencyLedger:
    """Process-local helper for datastore listener idempotency tests.

    Durable idempotency belongs to datastore implementations in later migration
    milestones. M3 defines the helper semantics only.
    """

    def __init__(self) -> None:
        self._seen: set[Tuple[str, str]] = set()
        self._lock = RLock()

    def remember(self, datastore_id: str, idempotency_key: str) -> bool:
        key = (
            _required_text(datastore_id, label="datastore_id"),
            _required_text(idempotency_key, label="idempotency_key"),
        )
        with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            return True

    def seen(self, datastore_id: str, idempotency_key: str) -> bool:
        key = (
            _required_text(datastore_id, label="datastore_id"),
            _required_text(idempotency_key, label="idempotency_key"),
        )
        with self._lock:
            return key in self._seen


class DatastoreContractBase:
    datastore_id: str = ""
    handler_specs: Tuple[DatastoreHandlerSpec, ...] = ()

    def __init__(self, manifest: Mapping[str, Any]) -> None:
        self._manifest = copy.deepcopy(dict(manifest))
        manifest_id = _required_text(self._manifest.get("id"), label="manifest.id")
        expected_id = _required_text(self.datastore_id, label="datastore_id")
        if manifest_id != expected_id:
            raise ValueError(f"manifest id mismatch: expected {expected_id}, got {manifest_id}")

    @property
    def manifest(self) -> Dict[str, Any]:
        return copy.deepcopy(self._manifest)

    def list_domain_event_listeners(self) -> List[DatastoreHandlerSpec]:
        return [spec for spec in self.handler_specs if spec.kind == DOMAIN_EVENT]

    def list_request_handlers(self) -> List[DatastoreHandlerSpec]:
        return [spec for spec in self.handler_specs if spec.kind == REQUEST]

    def health(self) -> Dict[str, Any]:
        return {"datastore_id": self.datastore_id, "healthy": True, "active": False}

    def validate(self) -> Dict[str, Any]:
        errors = validate_datastore_contract(self)
        return {"datastore_id": self.datastore_id, "valid": not errors, "errors": errors}

    def maintenance(self, request: Mapping[str, Any]) -> DatastoreResult:
        _ = request
        return nack(self.datastore_id, "maintenance.run.request.v1", "maintenance", "handler not activated in M3")

    def explain(self, request: Mapping[str, Any]) -> DatastoreResult:
        _ = request
        return nack(self.datastore_id, "datastore.explain.request.v1", "explain", "handler not activated in M3")

    def export(self, request: Mapping[str, Any]) -> DatastoreResult:
        _ = request
        return nack(self.datastore_id, "datastore.export.request.v1", "export", "handler not activated in M3")

    def import_data(self, request: Mapping[str, Any]) -> DatastoreResult:
        _ = request
        return nack(self.datastore_id, "datastore.import.request.v1", "import", "handler not activated in M3")


class MemoryDbDatastoreContract(DatastoreContractBase):
    datastore_id = "memorydb"
    handler_specs = (
        DatastoreHandlerSpec(
            "janitor.run_completed",
            DOMAIN_EVENT,
            ("core.runtime.events.EVENT_HANDLERS[janitor.run_completed]",),
        ),
        DatastoreHandlerSpec(
            "recall.memory.request.v1",
            REQUEST,
            ("datastore.memorydb.memory_graph.recall",),
        ),
        DatastoreHandlerSpec(
            "recall.graph.request.v1",
            REQUEST,
            ("datastore.memorydb.memory_graph.graph_aware_recall",),
        ),
        DatastoreHandlerSpec(
            "session.ingest_log.request.v1",
            REQUEST,
            ("core.plugins.memorydb_contract.handle_session_ingest_log_request",),
        ),
        DatastoreHandlerSpec(
            "datastore.validate.request.v1",
            REQUEST,
            ("datastore.memorydb.memory_graph.health",),
        ),
        DatastoreHandlerSpec(
            "datastore.explain.request.v1",
            REQUEST,
            ("datastore.memorydb.memory_graph provenance/debug helpers",),
        ),
        DatastoreHandlerSpec(
            "maintenance.run.request.v1",
            REQUEST,
            ("datastore.memorydb.maintenance.run_memory_graph_maintenance",),
        ),
    )


class DocsDbDatastoreContract(DatastoreContractBase):
    datastore_id = "docsdb"
    handler_specs = (
        DatastoreHandlerSpec(
            "docs.ingest_transcript",
            DOMAIN_EVENT,
            ("core.runtime.events._handle_docs_ingest_transcript",),
        ),
        DatastoreHandlerSpec(
            "docs.project_maintenance_observed",
            DOMAIN_EVENT,
            ("core.plugins.docsdb_contract.handle_project_docs_maintenance_event",),
        ),
        DatastoreHandlerSpec(
            "recall.docs.request.v1",
            REQUEST,
            ("datastore.docsdb.rag.DocsRAG.search",),
        ),
        DatastoreHandlerSpec(
            "recall.project_context.request.v1",
            REQUEST,
            ("datastore.docsdb.system_context.build_system_context_metadata",),
        ),
        DatastoreHandlerSpec(
            "datastore.validate.request.v1",
            REQUEST,
            ("datastore.docsdb.registry/docs index health checks",),
        ),
        DatastoreHandlerSpec(
            "datastore.explain.request.v1",
            REQUEST,
            ("datastore.docsdb.rag provenance/debug helpers",),
        ),
        DatastoreHandlerSpec(
            "project.worker_specs.request.v1",
            REQUEST,
            ("core.project_docs_supervisor worker reconciliation inputs",),
        ),
        DatastoreHandlerSpec(
            "docs.project_update.request.v1",
            REQUEST,
            ("core.project_docs.execute_update_once apply/index operation",),
        ),
        DatastoreHandlerSpec(
            "maintenance.run.request.v1",
            REQUEST,
            ("core.plugins.docsdb_contract.run_project_docs_monitor_maintenance",),
        ),
    )


class EvolutionDbDatastoreContract(DatastoreContractBase):
    datastore_id = "evolutiondb"
    handler_specs = (
        DatastoreHandlerSpec(
            "recall.journal.request.v1",
            REQUEST,
            ("core.facade recallFromJournal / datastore.notedb.soul_snippets journal files",),
        ),
        DatastoreHandlerSpec(
            "datastore.validate.request.v1",
            REQUEST,
            ("datastore.notedb.soul_snippets maintenance validation",),
        ),
        DatastoreHandlerSpec(
            "datastore.explain.request.v1",
            REQUEST,
            ("datastore.notedb.soul_snippets journal/snippet provenance helpers",),
        ),
        DatastoreHandlerSpec(
            "maintenance.run.request.v1",
            REQUEST,
            ("datastore.notedb.soul_snippets.register_lifecycle_routines",),
        ),
    )


_FIRST_PARTY_CONTRACT_CLASSES = {
    "memorydb": MemoryDbDatastoreContract,
    "docsdb": DocsDbDatastoreContract,
    "evolutiondb": EvolutionDbDatastoreContract,
}


def validate_datastore_contract(contract: DatastoreContractBase) -> List[str]:
    errors: List[str] = []
    manifest = contract.manifest
    specs = list(contract.handler_specs)
    if not specs:
        errors.append(f"{contract.datastore_id}: handler_specs must not be empty")

    declared_domain_events = set(manifest.get("accepted_events") or [])
    declared_requests = set(manifest.get("request_handlers") or [])
    spec_domain_events = {spec.event_type for spec in specs if spec.kind == DOMAIN_EVENT}
    spec_requests = {spec.event_type for spec in specs if spec.kind == REQUEST}

    missing_domain = declared_domain_events - spec_domain_events
    extra_domain = spec_domain_events - declared_domain_events
    missing_requests = declared_requests - spec_requests
    extra_requests = spec_requests - declared_requests
    if missing_domain:
        errors.append(f"{contract.datastore_id}: missing domain listener specs: {sorted(missing_domain)}")
    if extra_domain:
        errors.append(f"{contract.datastore_id}: extra domain listener specs: {sorted(extra_domain)}")
    if missing_requests:
        errors.append(f"{contract.datastore_id}: missing request handler specs: {sorted(missing_requests)}")
    if extra_requests:
        errors.append(f"{contract.datastore_id}: extra request handler specs: {sorted(extra_requests)}")

    for spec in specs:
        if not spec.replacement_targets:
            errors.append(f"{contract.datastore_id}: {spec.event_type} has no replacement target")

    return errors


def build_first_party_datastore_contracts() -> Dict[str, DatastoreContractBase]:
    manifests = build_datastore_registry()
    return {
        datastore_id: contract_cls(manifests[datastore_id])
        for datastore_id, contract_cls in sorted(_FIRST_PARTY_CONTRACT_CLASSES.items())
    }


def list_first_party_datastore_contracts() -> List[DatastoreContractBase]:
    contracts = build_first_party_datastore_contracts()
    return [contracts[key] for key in sorted(contracts)]
