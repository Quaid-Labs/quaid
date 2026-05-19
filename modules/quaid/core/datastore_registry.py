#!/usr/bin/env python3
"""Core-owned datastore manifest registry.

M2 is metadata-only: this module describes first-party datastore capabilities
without activating handlers or changing read/write paths.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import re
import sys
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

DATASTORE_MANIFEST_SCHEMA_VERSION = 1
_DATASTORE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")
_REQUIRED_FIELDS = (
    "id",
    "display_name",
    "description",
    "module",
    "plugin_id",
    "schema_version",
    "capabilities",
    "accepted_events",
    "request_handlers",
    "produced_events",
    "maintenance_tasks",
    "migrations",
    "worker_specs",
    "resource_budgets",
    "fail_hard_policy",
    "contracts",
)
_LIST_FIELDS = (
    "accepted_events",
    "request_handlers",
    "produced_events",
    "maintenance_tasks",
    "migrations",
    "worker_specs",
)
_OBJECT_FIELDS = (
    "capabilities",
    "resource_budgets",
    "contracts",
)


FIRST_PARTY_DATASTORE_MANIFESTS: List[Dict[str, Any]] = [
    {
        "id": "memorydb",
        "display_name": "MemoryDB",
        "description": "Memory graph, facts, edges, provenance evidence, and memory maintenance.",
        "module": "datastore.memorydb.memory_graph",
        "plugin_id": "memorydb.core",
        "schema_version": DATASTORE_MANIFEST_SCHEMA_VERSION,
        "capabilities": {
            "stores": ["facts", "edges", "source_chunks", "session_evidence", "archive"],
            "recall": ["vector", "vector_basic", "vector_technical", "graph", "temporal", "session_chunks"],
            "writes": ["facts", "edges", "source_chunks", "session_chunks"],
            "validate": True,
            "explain": True,
            "export": False,
            "import": False,
        },
        "accepted_events": ["janitor.run_completed"],
        "request_handlers": [
            "recall.memory.request.v1",
            "recall.graph.request.v1",
            "session.ingest_log.request.v1",
            "memory.extraction_publish.request.v1",
            "datastore.validate.request.v1",
            "datastore.explain.request.v1",
            "maintenance.run.request.v1",
        ],
        "produced_events": [],
        "maintenance_tasks": ["memory.maintenance", "edge_backfill", "domain_defaults"],
        "migrations": [],
        "worker_specs": [],
        "resource_budgets": {"llm": "normal", "io": "sqlite"},
        "fail_hard_policy": "inherit_global",
        "contracts": {"validate": 1, "explain": 1, "export": 0, "import": 0},
    },
    {
        "id": "docsdb",
        "display_name": "DocsDB",
        "description": "Project docs registry, document index, project logs, and docs freshness policy.",
        "module": "datastore.docsdb.rag",
        "plugin_id": "docsdb.core",
        "schema_version": DATASTORE_MANIFEST_SCHEMA_VERSION,
        "capabilities": {
            "stores": ["documents", "project_logs", "project_registry"],
            "recall": ["docs", "project", "project_context"],
            "writes": ["documents", "project_logs", "registry_rows"],
            "validate": True,
            "explain": True,
            "export": False,
            "import": False,
        },
        "accepted_events": ["docs.ingest_transcript", "docs.project_maintenance_observed"],
        "request_handlers": [
            "recall.docs.request.v1",
            "recall.project_context.request.v1",
            "datastore.validate.request.v1",
            "datastore.explain.request.v1",
            "project.worker_specs.request.v1",
            "docs.project_update.request.v1",
            "maintenance.run.request.v1",
        ],
        "produced_events": [],
        "maintenance_tasks": ["docs.rag_maintenance", "docs.staleness_check", "project_docs.update"],
        "migrations": [],
        "worker_specs": ["project_docs_worker"],
        "resource_budgets": {"llm": "normal", "io": "sqlite+files"},
        "fail_hard_policy": "inherit_global",
        "contracts": {"validate": 1, "explain": 1, "export": 0, "import": 0},
    },
    {
        "id": "evolutiondb",
        "display_name": "EvolutionDB",
        "description": "Canonical datastore id for the current NoteDB snippets and journal implementation.",
        "module": "datastore.notedb.soul_snippets",
        "plugin_id": "notedb.core",
        "schema_version": DATASTORE_MANIFEST_SCHEMA_VERSION,
        "capabilities": {
            "stores": ["snippets", "journal"],
            "recall": ["journal"],
            "writes": ["snippets", "journal"],
            "validate": True,
            "explain": True,
            "export": False,
            "import": False,
        },
        "accepted_events": [],
        "request_handlers": [
            "recall.journal.request.v1",
            "datastore.validate.request.v1",
            "datastore.explain.request.v1",
            "maintenance.run.request.v1",
        ],
        "produced_events": [],
        "maintenance_tasks": ["soul_snippets.maintenance", "journal.maintenance"],
        "migrations": [],
        "worker_specs": [],
        "resource_budgets": {"llm": "normal", "io": "markdown_files"},
        "fail_hard_policy": "inherit_global",
        "contracts": {"validate": 1, "explain": 1, "export": 0, "import": 0},
        "runtime_aliases": ["notedb"],
    },
]


def _is_fail_hard_enabled() -> bool:
    try:
        from lib.fail_policy import is_fail_hard_enabled

        return bool(is_fail_hard_enabled())
    except Exception:
        return True


def _copy_manifest(manifest: Dict[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy(manifest)


def validate_datastore_manifest(manifest: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    if not isinstance(manifest, dict):
        return ["manifest must be an object"]

    for field in _REQUIRED_FIELDS:
        if field not in manifest:
            errors.append(f"missing required field: {field}")

    datastore_id = str(manifest.get("id") or "").strip()
    if not datastore_id:
        errors.append("id is required")
    elif not _DATASTORE_ID_RE.match(datastore_id):
        errors.append(f"invalid datastore id: {datastore_id}")

    for field in ("display_name", "description", "plugin_id", "fail_hard_policy"):
        if not str(manifest.get(field) or "").strip():
            errors.append(f"{field} is required")

    module = str(manifest.get("module") or "").strip()
    if not module:
        errors.append("module is required")
    elif not _MODULE_RE.match(module):
        errors.append(f"invalid module path: {module}")

    if manifest.get("schema_version") != DATASTORE_MANIFEST_SCHEMA_VERSION:
        errors.append(f"schema_version must be {DATASTORE_MANIFEST_SCHEMA_VERSION}")

    for field in _LIST_FIELDS:
        value = manifest.get(field)
        if not isinstance(value, list):
            errors.append(f"{field} must be a list")
            continue
        if any(not isinstance(item, str) or not item.strip() for item in value):
            errors.append(f"{field} must contain only non-empty strings")

    for field in _OBJECT_FIELDS:
        if not isinstance(manifest.get(field), dict):
            errors.append(f"{field} must be an object")

    contracts = manifest.get("contracts")
    if isinstance(contracts, dict):
        for key in ("validate", "explain", "export", "import"):
            if key not in contracts:
                errors.append(f"contracts missing required key: {key}")
            elif not isinstance(contracts.get(key), int):
                errors.append(f"contracts.{key} must be an integer")

    return errors


def build_datastore_registry(
    manifests: Optional[Iterable[Dict[str, Any]]] = None,
    *,
    fail_hard: Optional[bool] = None,
) -> Dict[str, Dict[str, Any]]:
    registry: Dict[str, Dict[str, Any]] = {}
    failures: List[str] = []
    strict = _is_fail_hard_enabled() if fail_hard is None else bool(fail_hard)

    for index, raw_manifest in enumerate(FIRST_PARTY_DATASTORE_MANIFESTS if manifests is None else manifests):
        manifest = _copy_manifest(raw_manifest) if isinstance(raw_manifest, dict) else raw_manifest
        errors = validate_datastore_manifest(manifest)  # type: ignore[arg-type]
        datastore_id = str(manifest.get("id") or f"<index:{index}>").strip() if isinstance(manifest, dict) else f"<index:{index}>"
        if datastore_id in registry:
            errors.append(f"duplicate datastore id: {datastore_id}")
        if errors:
            failures.append(f"{datastore_id}: " + "; ".join(errors))
            continue
        registry[datastore_id] = _copy_manifest(manifest)  # type: ignore[arg-type]

    if failures:
        message = "Invalid datastore manifest registry: " + " | ".join(failures)
        if strict:
            raise RuntimeError(message)
        logger.error(message)

    return registry


def list_datastore_manifests() -> List[Dict[str, Any]]:
    registry = build_datastore_registry()
    return [_copy_manifest(registry[key]) for key in sorted(registry)]


def get_datastore_manifest(datastore_id: str) -> Optional[Dict[str, Any]]:
    key = str(datastore_id or "").strip()
    if not key:
        return None
    registry = build_datastore_registry()
    manifest = registry.get(key)
    return _copy_manifest(manifest) if manifest is not None else None


def list_datastore_capabilities() -> Dict[str, Dict[str, Any]]:
    return {
        key: _copy_manifest(manifest.get("capabilities") or {})
        for key, manifest in sorted(build_datastore_registry().items())
    }


def _print_human_list(manifests: List[Dict[str, Any]]) -> None:
    for manifest in manifests:
        print(f"{manifest['id']}\t{manifest['display_name']}\t{manifest['description']}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Quaid datastore manifest registry")
    sub = parser.add_subparsers(dest="command")

    list_p = sub.add_parser("list", help="List registered datastore manifests")
    list_p.add_argument("--json", action="store_true", help="Emit JSON")

    show_p = sub.add_parser("show", help="Show one datastore manifest")
    show_p.add_argument("datastore_id")
    show_p.add_argument("--json", action="store_true", help="Emit JSON")

    caps_p = sub.add_parser("capabilities", help="List datastore capability metadata")
    caps_p.add_argument("--json", action="store_true", help="Emit JSON")

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1

    if args.command == "list":
        manifests = list_datastore_manifests()
        if args.json:
            print(json.dumps({"status": "ok", "datastores": manifests}, indent=2, sort_keys=True))
        else:
            _print_human_list(manifests)
        return 0

    if args.command == "show":
        manifest = get_datastore_manifest(args.datastore_id)
        if manifest is None:
            print(f"datastore not found: {args.datastore_id}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps({"status": "ok", "datastore": manifest}, indent=2, sort_keys=True))
        else:
            print(f"{manifest['id']}\t{manifest['display_name']}\t{manifest['description']}")
        return 0

    if args.command == "capabilities":
        capabilities = list_datastore_capabilities()
        if args.json:
            print(json.dumps({"status": "ok", "capabilities": capabilities}, indent=2, sort_keys=True))
        else:
            for datastore_id, caps in capabilities.items():
                print(f"{datastore_id}\t{', '.join(sorted(caps))}")
        return 0

    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
