from datastore.memorydb import identity_map
from lib.adapter import StandaloneAdapter, reset_adapter, set_adapter


def setup_function():
    reset_adapter()


def teardown_function():
    reset_adapter()


def test_identity_handle_upsert_resolve_and_list(monkeypatch, tmp_path):
    set_adapter(StandaloneAdapter(home=tmp_path))
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))

    out = identity_map.upsert_identity_handle(
        owner_id="quaid",
        source_channel="telegram",
        conversation_id="group-1",
        handle="ownerAlias",
        canonical_entity_id="entity-albert",
        confidence=0.92,
    )
    assert out["canonical_entity_id"] == "entity-albert"

    resolved = identity_map.resolve_identity_handle(
        owner_id="quaid",
        source_channel="telegram",
        conversation_id="group-1",
        handle="ownerAlias",
    )
    assert resolved is not None
    assert resolved["canonical_entity_id"] == "entity-albert"

    mappings = identity_map.list_identity_handles(
        owner_id="quaid",
        source_channel="telegram",
        limit=10,
    )
    assert len(mappings) == 1
    assert mappings[0]["handle"] == "ownerAlias"


def test_identity_handle_resolution_falls_back_to_channel_scope(monkeypatch, tmp_path):
    set_adapter(StandaloneAdapter(home=tmp_path))
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))

    identity_map.upsert_identity_handle(
        owner_id="quaid",
        source_channel="telegram",
        conversation_id=None,
        handle="bert",
        canonical_entity_id="entity-bert",
    )

    resolved = identity_map.resolve_identity_handle(
        owner_id="quaid",
        source_channel="telegram",
        conversation_id="group-xyz",
        handle="bert",
    )
    assert resolved is not None
    assert resolved["canonical_entity_id"] == "entity-bert"

