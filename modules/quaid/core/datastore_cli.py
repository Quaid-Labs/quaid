#!/usr/bin/env python3
"""Dispatch datastore-owned CLI namespaces through the core registry."""

from __future__ import annotations

import importlib
import os
import sys
from typing import List, Optional


PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

from core.datastore_cli_registry import DATASTORE_CLI_REGISTRY


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    namespace = args.pop(0) if args else ""
    entry = DATASTORE_CLI_REGISTRY.get(namespace)
    if entry is None:
        known = "|".join(sorted(DATASTORE_CLI_REGISTRY))
        print(f"Usage: quaid {{{known}}} [args...]", file=sys.stderr)
        return 1

    module = importlib.import_module(entry["module"])
    exit_code = module.main(args)
    if exit_code is None:
        raise RuntimeError(f"{entry['module']}.main returned no exit code")
    return int(exit_code)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
