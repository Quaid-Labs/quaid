#!/usr/bin/env python3
"""Docs ingestion pipeline for adapter-triggered transcript updates.

Moves transcript->docs orchestration out of the adapter layer.
"""

from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
from typing import Any, Dict

from config import get_config


def _docs_updater_module():
    return importlib.import_module("datastore.docsdb.updater")


def check_staleness():
    return _docs_updater_module().check_staleness()


def cmd_update_from_transcript(transcript_path: str, dry_run: bool = False, max_docs: int = 3):
    return _docs_updater_module().cmd_update_from_transcript(transcript_path, dry_run=dry_run, max_docs=max_docs)


def _run(transcript_path: Path, label: str, session_id: str | None = None) -> Dict[str, Any]:
    cfg = get_config()
    if not getattr(cfg.systems, "workspace", True):
        return {"status": "disabled", "message": "workspace system disabled"}
    docs_cfg = getattr(cfg, "docs", None)
    if not docs_cfg or not getattr(docs_cfg, "auto_update_on_compact", False):
        return {"status": "disabled", "message": "docs auto-update disabled"}
    if not transcript_path.exists():
        return {"status": "error", "message": "transcript file not found"}

    stale = check_staleness()
    stale_docs = len(stale or {})
    if stale_docs == 0:
        return {"status": "up_to_date", "staleDocs": 0, "updatedDocs": 0}

    max_docs = int(getattr(docs_cfg, "max_docs_per_update", 3) or 3)
    updated = int(cmd_update_from_transcript(str(transcript_path), dry_run=False, max_docs=max_docs))
    return {
        "status": "updated",
        "label": label,
        "sessionId": session_id,
        "staleDocs": stale_docs,
        "updatedDocs": updated,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run docs ingest pipeline from transcript")
    parser.add_argument("--transcript", required=True, help="Path to transcript file")
    parser.add_argument("--label", default="Unknown", help="Trigger label")
    parser.add_argument("--session-id", default=None, help="Session identifier")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    args = parser.parse_args()

    result = _run(Path(args.transcript), args.label, args.session_id)
    if args.json:
        print(json.dumps(result))
    else:
        print(result)
    return 0 if result.get("status") != "error" else 1


if __name__ == "__main__":
    raise SystemExit(main())
