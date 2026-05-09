#!/usr/bin/env python3
"""DocsDB command dispatcher.

The command implementations remain in their existing modules. This file owns
the docs namespace dispatch so the bash wrapper does not hardcode datastore
subcommands.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from typing import Callable, Dict, Iterator, List, Optional


PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)


Command = Callable[[List[str]], int]


@contextmanager
def _patched_argv(argv: List[str]) -> Iterator[None]:
    previous = sys.argv[:]
    sys.argv = argv
    try:
        yield
    finally:
        sys.argv = previous


def _coerce_exit_code(value: object) -> int:
    return int(value or 0)


def cmd_list(argv: List[str]) -> int:
    from datastore.docsdb import registry

    with _patched_argv(["docs_cli.py", "list", *argv]):
        return _coerce_exit_code(registry.main())


def cmd_check(argv: List[str]) -> int:
    from datastore.docsdb import updater

    return _coerce_exit_code(updater.main(["check", *argv]))


def cmd_update(argv: List[str]) -> int:
    if not argv or str(argv[0]).startswith("--"):
        from core.docs import updater

        return _coerce_exit_code(updater.main(["update-stale", *argv]))

    from core import project_docs_cli

    with _patched_argv(["docs_cli.py", "update", *argv]):
        return _coerce_exit_code(project_docs_cli.main())


def cmd_changelog(argv: List[str]) -> int:
    from datastore.docsdb import updater

    return _coerce_exit_code(updater.main(["changelog", *argv]))


commands: Dict[str, Command] = {
    "list": cmd_list,
    "check": cmd_check,
    "update": cmd_update,
    "changelog": cmd_changelog,
}


def _print_usage() -> None:
    print("Usage: quaid docs {list|check|update|changelog} [args...]", file=sys.stderr)


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
