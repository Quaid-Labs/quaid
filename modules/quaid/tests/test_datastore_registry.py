from __future__ import annotations

import json

import pytest

from core import datastore_registry
from core.datastore_registry import (
    DATASTORE_MANIFEST_SCHEMA_VERSION,
    build_datastore_registry,
    get_datastore_manifest,
    list_datastore_capabilities,
    list_datastore_manifests,
    validate_datastore_manifest,
)
from core.runtime.events import get_event_capability


def test_first_party_datastore_registry_lists_canonical_manifests() -> None:
    manifests = list_datastore_manifests()
    ids = [manifest["id"] for manifest in manifests]

    assert ids == ["docsdb", "evolutiondb", "memorydb", "sessiondb"]
    assert get_datastore_manifest("evolutiondb")["runtime_aliases"] == ["notedb"]
    assert get_datastore_manifest("evolutiondb")["module"] == "datastore.evolutiondb.soul_snippets"
    sessiondb = get_datastore_manifest("sessiondb")
    assert sessiondb["module"] == "datastore.sessiondb.session_store"
    assert sessiondb["plugin_id"] == "sessiondb.core"
    assert sessiondb["runtime_aliases"] == []


def test_datastore_registry_returns_copies() -> None:
    manifest = get_datastore_manifest("memorydb")
    manifest["display_name"] = "mutated"

    assert get_datastore_manifest("memorydb")["display_name"] == "MemoryDB"


def test_datastore_capabilities_surface_manifest_metadata() -> None:
    capabilities = list_datastore_capabilities()

    assert "graph" in capabilities["memorydb"]["recall"]
    assert "session_chunks" in capabilities["memorydb"]["recall"]
    assert "project_context" in capabilities["docsdb"]["recall"]
    assert capabilities["evolutiondb"]["stores"] == ["snippets", "journal"]
    assert capabilities["sessiondb"]["recall"] == []
    assert capabilities["sessiondb"]["writes"] == [
        "sessions",
        "transcript_chunks",
        "message_pairs",
        "microchunks",
    ]
    assert "microchunks" in capabilities["sessiondb"]["stores"]
    assert "message_pair_attachments" in capabilities["sessiondb"]["stores"]
    assert "message_pair_attachments" not in capabilities["sessiondb"]["writes"]


def test_sessiondb_manifest_owns_ingest_request_and_memorydb_keeps_recall_projection() -> None:
    sessiondb = get_datastore_manifest("sessiondb")
    memorydb = get_datastore_manifest("memorydb")

    assert validate_datastore_manifest(sessiondb) == []
    assert sessiondb["request_handlers"] == [
        "session.ingest_log.request.v1",
        "datastore.validate.request.v1",
        "datastore.explain.request.v1",
        "maintenance.run.request.v1",
    ]
    assert "session.ingest_log.request.v1" not in memorydb["request_handlers"]
    assert sessiondb["capabilities"]["recall"] == []
    assert sessiondb["capabilities"]["writes"] == [
        "sessions",
        "transcript_chunks",
        "message_pairs",
        "microchunks",
    ]
    assert "session_chunks" in memorydb["capabilities"]["recall"]
    assert "session_chunks" in memorydb["capabilities"]["writes"]


def test_manifest_request_handlers_are_registered_request_events() -> None:
    for manifest in list_datastore_manifests():
        for event_type in manifest["request_handlers"]:
            capability = get_event_capability(event_type)
            assert capability is not None, event_type
            assert capability["delivery_mode"] == "request"


def test_manifest_accepted_events_are_registered_domain_events() -> None:
    for manifest in list_datastore_manifests():
        for event_type in manifest["accepted_events"]:
            capability = get_event_capability(event_type)
            assert capability is not None, event_type
            assert capability["delivery_mode"] != "request"


def test_datastore_manifest_validation_rejects_missing_required_field() -> None:
    manifest = get_datastore_manifest("memorydb")
    del manifest["capabilities"]

    errors = validate_datastore_manifest(manifest)

    assert "missing required field: capabilities" in errors
    assert "capabilities must be an object" in errors


def test_datastore_registry_rejects_duplicate_ids() -> None:
    manifest = get_datastore_manifest("memorydb")

    with pytest.raises(RuntimeError, match="duplicate datastore id: memorydb"):
        build_datastore_registry([manifest, manifest], fail_hard=True)


def test_datastore_registry_rejects_unsupported_schema_version() -> None:
    manifest = get_datastore_manifest("memorydb")
    manifest["schema_version"] = DATASTORE_MANIFEST_SCHEMA_VERSION + 1

    with pytest.raises(RuntimeError, match="schema_version must be 1"):
        build_datastore_registry([manifest], fail_hard=True)


def test_datastore_registry_logs_and_skips_invalid_manifest_when_not_fail_hard(caplog) -> None:
    valid = get_datastore_manifest("memorydb")
    invalid = get_datastore_manifest("docsdb")
    invalid["schema_version"] = DATASTORE_MANIFEST_SCHEMA_VERSION + 1

    with caplog.at_level("ERROR"):
        registry = build_datastore_registry([valid, invalid], fail_hard=False)

    assert sorted(registry) == ["memorydb"]
    assert "Invalid datastore manifest registry" in caplog.text
    assert "schema_version must be 1" in caplog.text


def test_datastore_registry_cli_list_json(capsys) -> None:
    assert datastore_registry.main(["list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "ok"
    assert [item["id"] for item in payload["datastores"]] == ["docsdb", "evolutiondb", "memorydb", "sessiondb"]


def test_datastore_registry_cli_show_unknown(capsys) -> None:
    assert datastore_registry.main(["show", "missing-store"]) == 1

    assert "datastore not found: missing-store" in capsys.readouterr().err
