from __future__ import annotations

from core.datastore_cli_registry import (
    DATASTORE_CLI_REGISTRY,
    get_datastore_cli_entry,
    list_datastore_cli_entries,
)


def test_static_datastore_cli_registry_has_docs_and_session_entries() -> None:
    assert DATASTORE_CLI_REGISTRY["docs"]["module"] == "datastore.docsdb.docs_cli"
    assert DATASTORE_CLI_REGISTRY["session"]["module"] == "core.session_cli"


def test_static_datastore_cli_registry_returns_copy_for_listing() -> None:
    entries = list_datastore_cli_entries()
    entries["docs"]["module"] = "mutated"

    assert get_datastore_cli_entry("docs")["module"] == "datastore.docsdb.docs_cli"
