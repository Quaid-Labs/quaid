from types import SimpleNamespace

import pytest

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

