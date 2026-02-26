from concurrent.futures import ThreadPoolExecutor
import threading
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


def test_filter_recall_results_accepts_structured_policy_decisions(monkeypatch):
    monkeypatch.setattr(
        "core.runtime.identity_runtime.get_config",
        lambda: SimpleNamespace(identity=SimpleNamespace(mode="multi_user")),
    )
    identity_runtime.register_privacy_policy(
        "memorydb",
        lambda _viewer, row, _ctx: {"action": "allow_redacted"} if row["id"] == "1" else {"action": "deny"},
    )
    out = identity_runtime.filter_recall_results(
        viewer_entity_id="user:a",
        results=[{"id": "1"}, {"id": "2"}],
        context={},
    )
    assert [r["id"] for r in out] == ["1"]


def test_filter_recall_results_denies_invalid_policy_decisions(monkeypatch):
    monkeypatch.setattr(
        "core.runtime.identity_runtime.get_config",
        lambda: SimpleNamespace(identity=SimpleNamespace(mode="multi_user")),
    )
    identity_runtime.register_privacy_policy(
        "memorydb",
        lambda _viewer, _row, _ctx: "allow",
    )
    out = identity_runtime.filter_recall_results(
        viewer_entity_id="user:a",
        results=[{"id": "1"}],
        context={},
    )
    assert out == []


def test_multi_user_runtime_readiness_requires_hooks(monkeypatch):
    monkeypatch.setattr(
        "core.runtime.identity_runtime.get_config",
        lambda: SimpleNamespace(identity=SimpleNamespace(mode="multi_user")),
    )
    with pytest.raises(RuntimeError, match="resolver_owner=none"):
        identity_runtime.assert_multi_user_runtime_ready(require_write=True)
    with pytest.raises(RuntimeError, match="policy_owner=none"):
        identity_runtime.assert_multi_user_runtime_ready(require_read=True)


def test_identity_mode_warns_on_config_error(monkeypatch, caplog):
    def _boom():
        raise RuntimeError("config unavailable")

    monkeypatch.setattr("core.runtime.identity_runtime.get_config", _boom)
    caplog.set_level("WARNING")
    mode = identity_runtime.identity_mode()

    assert mode == "single_user"
    assert "identity_mode config read failed" in caplog.text


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


def test_memory_service_search_passes_identity_scope_to_datastore(monkeypatch):
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
    captured = {}

    def _fake_search(**kwargs):
        captured.update(kwargs)
        return [{"id": "1", "owner_id": "user:a", "visibility_scope": "global_shared"}]

    monkeypatch.setattr("core.services.memory_service.search_memories", _fake_search)

    import core.services.memory_service as mem_svc
    mem_svc._IDENTITY_RUNTIME_BOOTSTRAPPED = False

    svc = DatastoreMemoryService()
    out = svc.search(
        query="espresso",
        owner_id="user:a",
        viewer_entity_id="user:a",
        source_channel="telegram",
        source_conversation_id="chat-1",
        source_author_id="ownerAlias",
        subject_entity_id="user:a",
        participant_entity_ids=["user:a", "agent:bert"],
    )
    assert [row["id"] for row in out] == ["1"]
    assert captured["source_channel"] == "telegram"
    assert captured["source_conversation_id"] == "chat-1"
    assert captured["source_author_id"] == "ownerAlias"
    assert captured["subject_entity_id"] == "user:a"
    assert captured["participant_entity_ids"] == ["user:a", "agent:bert"]


def test_enrich_identity_payload_reports_resolver_return_type():
    identity_runtime.clear_registrations()
    identity_runtime.register_identity_resolver("memorydb", lambda payload: "not-a-dict")

    with pytest.raises(RuntimeError, match="type=str"):
        identity_runtime.enrich_identity_payload({"source_channel": "telegram"})


def test_memory_service_bootstrap_is_thread_safe(monkeypatch):
    import core.services.memory_service as mem_svc

    mem_svc._IDENTITY_RUNTIME_BOOTSTRAPPED = False
    calls = {"resolver": 0, "policy": 0}
    lock = threading.Lock()

    def _count_resolver(_name, _fn):
        with lock:
            calls["resolver"] += 1

    def _count_policy(_name, _fn):
        with lock:
            calls["policy"] += 1

    monkeypatch.setattr(mem_svc, "register_identity_resolver", _count_resolver)
    monkeypatch.setattr(mem_svc, "register_privacy_policy", _count_policy)
    monkeypatch.setattr(
        "datastore.memorydb.identity_defaults.default_identity_resolver",
        lambda payload: payload,
    )
    monkeypatch.setattr(
        "datastore.memorydb.identity_defaults.default_privacy_policy",
        lambda _viewer, _row, _ctx: True,
    )

    with ThreadPoolExecutor(max_workers=12) as executor:
        list(executor.map(lambda _i: mem_svc._ensure_identity_runtime_bootstrap(), range(40)))

    assert calls["resolver"] == 1
    assert calls["policy"] == 1


def test_concurrent_conflicting_identity_resolver_registration_is_serialized(monkeypatch):
    identity_runtime.clear_registrations()

    # Widen the race window around resolver construction.
    original_registered_hook = identity_runtime._RegisteredHook

    class SlowRegisteredHook(original_registered_hook):
        def __init__(self, owner, fn):
            import time
            time.sleep(0.01)
            super().__init__(owner=owner, fn=fn)

    monkeypatch.setattr(identity_runtime, "_RegisteredHook", SlowRegisteredHook)

    barrier = threading.Barrier(2)
    errors = []

    def _register(owner_name: str):
        try:
            barrier.wait(timeout=1)
            identity_runtime.register_identity_resolver(owner_name, lambda payload: payload)
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=_register, args=("owner-a",))
    t2 = threading.Thread(target=_register, args=("owner-b",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
