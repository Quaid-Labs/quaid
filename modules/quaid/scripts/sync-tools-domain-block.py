#!/usr/bin/env python3
"""Sync projects/quaid/TOOLS.md domain block from effective runtime domains."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync TOOLS.md domain block")
    parser.add_argument("--workspace", default="", help="Workspace root (optional)")
    args = parser.parse_args()

    workspace = str(args.workspace or "").strip()
    if workspace:
        os.environ["QUAID_HOME"] = workspace
        os.environ["CLAWDBOT_WORKSPACE"] = workspace

    root = _plugin_root()
    sys.path.insert(0, str(root))

    from config import get_config  # noqa: E402
    from lib.tools_domain_sync import sync_tools_domain_block  # noqa: E402

    domains = dict(getattr(get_config().retrieval, "domains", {}) or {})
    changed = sync_tools_domain_block(domains=domains, workspace=Path(os.environ.get("QUAID_HOME", "")) if os.environ.get("QUAID_HOME", "") else None)
    if changed:
        print("[domains] TOOLS.md domain block synced")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

