#!/usr/bin/env python3
"""Core composition dispatcher for the `quaid docs` namespace."""

from __future__ import annotations

import os
import sys
from typing import Callable, Dict, List, Optional


PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)


Command = Callable[[List[str]], int]


def cmd_list(argv: List[str]) -> int:
    from datastore.docsdb import docs_cli as docsdb_docs_cli

    return docsdb_docs_cli.main(["list", *argv])


def cmd_check(argv: List[str]) -> int:
    from datastore.docsdb import docs_cli as docsdb_docs_cli

    return docsdb_docs_cli.main(["check", *argv])


def cmd_update(argv: List[str]) -> int:
    if not argv or str(argv[0]).startswith("--"):
        from core.docs import updater

        return updater.main(["update-stale", *argv])

    from core import project_docs_cli

    return project_docs_cli.main(["update", *argv])


def cmd_changelog(argv: List[str]) -> int:
    from datastore.docsdb import docs_cli as docsdb_docs_cli

    return docsdb_docs_cli.main(["changelog", *argv])


def cmd_registry(argv: List[str]) -> int:
    from datastore.docsdb import registry

    return registry.main(argv)


commands: Dict[str, Command] = {
    "list": cmd_list,
    "check": cmd_check,
    "update": cmd_update,
    "changelog": cmd_changelog,
    "registry": cmd_registry,
}


def _print_usage() -> None:
    print("Usage: quaid docs {list|check|update|changelog|registry} [args...]", file=sys.stderr)


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    subcmd = args.pop(0) if args else ""
    handler = commands.get(subcmd)
    if handler is None:
        _print_usage()
        return 1
    return handler(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
