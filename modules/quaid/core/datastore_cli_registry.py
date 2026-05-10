"""Static datastore CLI registry.

This is intentionally static for now. Core owns the registry shape so the bash
wrapper and future dynamic datastore registration have a single contract to
converge on without moving command implementations yet.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


DATASTORE_CLI_REGISTRY: Dict[str, Dict[str, str]] = {
    "docs": {
        "module": "core.docs_cli",
        "description": "DocsDB registry, staleness, update, and changelog commands.",
    },
    "session": {
        "module": "core.session_cli",
        "description": "SessionDB inspection commands.",
    }
}


def get_datastore_cli_entry(namespace: str) -> Optional[Dict[str, str]]:
    """Return static CLI entry metadata for a datastore namespace."""
    return DATASTORE_CLI_REGISTRY.get(str(namespace or "").strip())


def list_datastore_cli_entries() -> Dict[str, Dict[str, Any]]:
    """Return a JSON-serializable copy of the static registry."""
    return {name: dict(entry) for name, entry in DATASTORE_CLI_REGISTRY.items()}
