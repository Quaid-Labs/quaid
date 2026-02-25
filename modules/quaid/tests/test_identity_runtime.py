from types import SimpleNamespace

import pytest

from core.services.memory_service import DatastoreMemoryService
from core.runtime import identity_runtime


def setup_function():
    identity_runtime.clear_registrations()


def teardown_function():
    identity_runtime.clear_registrations()


def test_single_registration_enforced_for_identity_resolver():
    identity_runtime.register_identity_resolver("memorydb", lambda payload: payload)
    with pytest.raises(RuntimeError):
        identity_runtime.register_identity_resolver("other", lambda payload: payload)


def test_single_registration_enforced_for_privacy_policy():
    identity_runtime.register_privacy_policy("memorydb", lambda viewer, row, ctx: True)
    with pytest.raises(RuntimeError):
        identity_runtime.register_privacy_policy("other", lambda viewer, row, ctx: True)


def test_multi_user_write_contract_requires_identity_fields(monkeypatch):
    monkeypatch.setattr(
        "core.runtime.identity_runtime.get_config",
        lambda: SimpleNamespace(identity=SimpleNamespace(mode="multi_user")),
    )
    with pytest.raises(ValueError):
        identity_runtime.enforce_write_contract({"source_channel": "telegram"})


def test_filter_recall_results_applies_registered_policy(monkeypatch):
    monkeypatch.setattr(
        "core.runtime.identity_runtime.get_config",
        lambda: SimpleNamespace(identity=SimpleNamespace(mode="multi_user")),
    )
    identity_runtime.register_privacy_policy(
        "memorydb",
        lambda viewer, row, _ctx: row.get("owner_id") == viewer,
    )
    out = identity_runtime.filter_recall_results(
        viewer_entity_id="user:a",
        results=[{"id": "1", "owner_id": "user:a"}, {"id": "2", "owner_id": "user:b"}],
        context={},
    )
    assert [r["id"] for r in out] == ["1"]


def test_multi_user_runtime_readiness_requires_hooks(monkeypatch):
    monkeypatch.setattr(
        "core.runtime.identity_runtime.get_config",
        lambda: SimpleNamespace(identity=SimpleNamespace(mode="multi_user")),
    )
    with pytest.raises(RuntimeError):
        identity_runtime.assert_multi_user_runtime_ready(require_write=True)
    with pytest.raises(RuntimeError):
        identity_runtime.assert_multi_user_runtime_ready(require_read=True)


def test_memory_service_bootstraps_default_identity_runtime(monkeypatch):
    identity_runtime.clear_registrations()
    monkeypatch.setattr(
        "core.runtime.identity_runtime.get_config",
        lambda: SimpleNamespace(
            identity=SimpleNamespace(mode="multi_user"),
            privacy=SimpleNamespace(enforce_strict_filters=True),
        ),
    )
    monkeypatch.setattr(
        "datastore.memorydb.identity_defaults.get_config",
        lambda: SimpleNamespace(privacy=SimpleNamespace(enforce_strict_filters=True)),
    )
    monkeypatch.setattr(
        "core.services.memory_service.store_memory",
        lambda **kwargs: {"id": "m1", "status": "created", "kwargs": kwargs},
    )
    monkeypatch.setattr(
        "core.services.memory_service.recall_memories",
        lambda **kwargs: [{"id": "m1", "owner_id": "user:a", "visibility_scope": "global_shared"}],
    )

    import core.services.memory_service as mem_svc
    mem_svc._IDENTITY_RUNTIME_BOOTSTRAPPED = False

    svc = DatastoreMemoryService()
    out = svc.store(
        text="Alice likes espresso",
        owner_id="user:a",
        source_channel="telegram",
        source_conversation_id="chat-1",
        source_author_id="user:a",
        speaker_entity_id="user:a",
    )
    assert out["status"] == "created"

    recalled = svc.recall(
        query="espresso",
        owner_id="user:a",
        viewer_entity_id="user:a",
    )
    assert len(recalled) == 1


def test_memory_service_search_applies_privacy_filter(monkeypatch):
    identity_runtime.clear_registrations()
    monkeypatch.setattr(
        "core.runtime.identity_runtime.get_config",
        lambda: SimpleNamespace(
            identity=SimpleNamespace(mode="multi_user"),
            privacy=SimpleNamespace(enforce_strict_filters=True),
        ),
    )
    monkeypatch.setattr(
        "datastore.memorydb.identity_defaults.get_config",
        lambda: SimpleNamespace(privacy=SimpleNamespace(enforce_strict_filters=True)),
    )
    monkeypatch.setattr(
        "core.services.memory_service.search_memories",
        lambda **kwargs: [
            {"id": "1", "owner_id": "user:a", "visibility_scope": "global_shared"},
            {"id": "2", "owner_id": "user:b", "visibility_scope": "private_subject", "subject_entity_id": "user:b"},
        ],
    )

    import core.services.memory_service as mem_svc
    mem_svc._IDENTITY_RUNTIME_BOOTSTRAPPED = False

    svc = DatastoreMemoryService()
    out = svc.search(
        query="espresso",
        owner_id="user:a",
        viewer_entity_id="user:a",
    )
    assert [row["id"] for row in out] == ["1"]
