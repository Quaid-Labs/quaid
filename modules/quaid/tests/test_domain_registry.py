from pathlib import Path

import pytest

from datastore.memorydb import domain_registry


def test_safe_load_active_domains_warns_and_returns_empty_when_fail_open(monkeypatch, caplog):
    monkeypatch.setattr(
        domain_registry,
        "load_active_domains",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("db unavailable")),
    )
    monkeypatch.setattr(domain_registry, "_fail_hard_enabled", lambda: False)
    caplog.set_level("WARNING")

    assert domain_registry.safe_load_active_domains(Path("/tmp/memory.db")) == {}
    assert "Failed loading active domains" in caplog.text


def test_safe_load_active_domains_raises_under_failhard(monkeypatch):
    monkeypatch.setattr(
        domain_registry,
        "load_active_domains",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("db unavailable")),
    )
    monkeypatch.setattr(domain_registry, "_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="db unavailable"):
        domain_registry.safe_load_active_domains(Path("/tmp/memory.db"))
