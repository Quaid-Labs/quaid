from __future__ import annotations

from core.datastore_cli_registry import (
    DATASTORE_CLI_REGISTRY,
    get_datastore_cli_entry,
    list_datastore_cli_entries,
)
from core.datastore_cli import main as datastore_cli_main


def test_static_datastore_cli_registry_has_docs_and_session_entries() -> None:
    assert DATASTORE_CLI_REGISTRY["docs"]["module"] == "core.docs_cli"
    assert DATASTORE_CLI_REGISTRY["session"]["module"] == "core.session_cli"


def test_static_datastore_cli_registry_returns_copy_for_listing() -> None:
    entries = list_datastore_cli_entries()
    entries["docs"]["module"] = "mutated"

    assert get_datastore_cli_entry("docs")["module"] == "core.docs_cli"


def test_datastore_cli_dispatch_consumes_static_registry(monkeypatch) -> None:
    from core import docs_cli

    calls = []

    def fake_main(argv) -> int:
        calls.append(list(argv))
        return 0

    monkeypatch.setattr(docs_cli, "main", fake_main)

    assert datastore_cli_main(["docs", "check", "--json"]) == 0
    assert calls == [["check", "--json"]]
