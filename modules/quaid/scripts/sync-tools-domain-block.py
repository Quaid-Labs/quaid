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

    root = _plugin_root()
    workspace = str(args.workspace or "").strip()
    if not workspace:
        workspace = str(os.environ.get("QUAID_HOME", "") or os.environ.get("CLAWDBOT_WORKSPACE", "")).strip()
    if not workspace:
        workspace = str(root.parent.parent)
    os.environ["QUAID_HOME"] = workspace
    os.environ["CLAWDBOT_WORKSPACE"] = workspace
    sys.path.insert(0, str(root))

    from config import get_config  # noqa: E402
    from lib.config import get_db_path  # noqa: E402
    from lib.database import get_connection  # noqa: E402
    from lib.tools_domain_sync import sync_tools_domain_block  # noqa: E402

    domains = {}
    try:
        db_path = get_db_path()
        with get_connection(db_path) as conn:
            rows = conn.execute(
                "SELECT domain, description FROM domain_registry WHERE active = 1 ORDER BY domain"
            ).fetchall()
        domains = {str(r[0]).strip(): str(r[1] or "").strip() for r in rows if str(r[0]).strip()}
    except Exception:
        domains = {}
    if not domains:
        domains = dict(getattr(get_config().retrieval, "domains", {}) or {})
    workspace_path = Path(workspace).expanduser().resolve()
    tools_path = workspace_path / "projects" / "quaid" / "TOOLS.md"
    if not tools_path.exists():
        print(f"[domains] TOOLS.md missing at expected path: {tools_path}", file=sys.stderr)
        return 1
    changed = sync_tools_domain_block(domains=domains, workspace=workspace_path)
    if changed:
        print("[domains] TOOLS.md domain block synced")
    else:
        print("[domains] TOOLS.md domain block already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
