from __future__ import annotations

import pytest

from core.contracts.datastore import (
    DOMAIN_EVENT,
    REQUEST,
    DatastoreHandlerSpec,
    DatastoreIdempotencyLedger,
    ack,
    build_first_party_datastore_contracts,
    list_first_party_datastore_contracts,
    nack,
    validate_datastore_contract,
)


def test_first_party_contracts_match_manifest_handlers_and_ids() -> None:
    contracts = build_first_party_datastore_contracts()

    assert sorted(contracts) == ["docsdb", "insightdb", "memorydb", "sessiondb"]
    for datastore_id, contract in contracts.items():
        manifest = contract.manifest
        assert manifest["id"] == datastore_id
        assert validate_datastore_contract(contract) == []

        domain_events = {spec.event_type for spec in contract.list_domain_event_listeners()}
        request_handlers = {spec.event_type for spec in contract.list_request_handlers()}
        assert domain_events == set(manifest["accepted_events"])
        assert request_handlers == set(manifest["request_handlers"])


def test_first_party_contract_specs_identify_replacement_targets() -> None:
    for contract in list_first_party_datastore_contracts():
        for spec in contract.handler_specs:
            assert spec.replacement_targets
            assert all(str(target).strip() for target in spec.replacement_targets)


def test_datastore_handler_spec_rejects_unsupported_kind() -> None:
    with pytest.raises(ValueError, match="unsupported handler kind"):
        DatastoreHandlerSpec(
            "recall.memory.request.v1",
            "legacy_wrapper",
            ("datastore.memorydb.memory_graph.recall",),
        )


def test_datastore_handler_spec_requires_replacement_targets() -> None:
    with pytest.raises(ValueError, match="replacement_targets"):
        DatastoreHandlerSpec("recall.memory.request.v1", REQUEST, ())


def test_contract_health_and_validation_are_metadata_only() -> None:
    contract = build_first_party_datastore_contracts()["memorydb"]

    assert contract.health() == {"datastore_id": "memorydb", "healthy": True, "active": False}
    assert contract.validate() == {"datastore_id": "memorydb", "valid": True, "errors": []}


def test_sessiondb_contract_owns_session_request_and_memorydb_keeps_recall_projection() -> None:
    contracts = build_first_party_datastore_contracts()
    sessiondb = contracts["sessiondb"]
    memorydb = contracts["memorydb"]

    assert sessiondb.health() == {"datastore_id": "sessiondb", "healthy": True, "active": False}
    assert sessiondb.validate() == {"datastore_id": "sessiondb", "valid": True, "errors": []}
    assert [spec.event_type for spec in sessiondb.list_domain_event_listeners()] == []
    assert [spec.event_type for spec in sessiondb.list_request_handlers()] == [
        "session.ingest_log.request.v1",
        "datastore.validate.request.v1",
        "datastore.explain.request.v1",
        "maintenance.run.request.v1",
    ]
    assert "session.ingest_log.request.v1" not in {
        spec.event_type for spec in memorydb.list_request_handlers()
    }


def test_contract_inactive_handlers_nack_without_calling_legacy_paths() -> None:
    contract = build_first_party_datastore_contracts()["docsdb"]

    result = contract.maintenance({"dry_run": True}).as_dict()

    assert result["datastore_id"] == "docsdb"
    assert result["event_type"] == "maintenance.run.request.v1"
    assert result["status"] == "nacked"
    assert result["handler"] == "maintenance"
    assert result["error"] == "handler not activated in M3"


def test_ack_and_nack_payload_shape() -> None:
    ok = ack("memorydb", "recall.memory.request.v1", "recall", {"rows": 2}).as_dict()
    err = nack("memorydb", "recall.memory.request.v1", "recall", "not ready").as_dict()

    assert ok == {
        "datastore_id": "memorydb",
        "event_type": "recall.memory.request.v1",
        "status": "acked",
        "handler": "recall",
        "result": {"rows": 2},
    }
    assert err == {
        "datastore_id": "memorydb",
        "event_type": "recall.memory.request.v1",
        "status": "nacked",
        "handler": "recall",
        "result": {},
        "error": "not ready",
    }


def test_idempotency_ledger_is_scoped_by_datastore() -> None:
    ledger = DatastoreIdempotencyLedger()

    assert ledger.remember("memorydb", "event-1") is True
    assert ledger.seen("memorydb", "event-1") is True
    assert ledger.remember("memorydb", "event-1") is False
    assert ledger.remember("docsdb", "event-1") is True


def test_contract_lists_split_domain_and_request_specs() -> None:
    contract = build_first_party_datastore_contracts()["memorydb"]

    assert all(spec.kind == DOMAIN_EVENT for spec in contract.list_domain_event_listeners())
    assert all(spec.kind == REQUEST for spec in contract.list_request_handlers())
    assert [spec.event_type for spec in contract.list_domain_event_listeners()] == ["janitor.run_completed"]
