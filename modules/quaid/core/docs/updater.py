"""Core wrapper for docs updater datastore implementation."""

from __future__ import annotations

from datastore.docsdb import updater as _updater
from datastore.docsdb.project_updater import append_project_logs as _append_project_logs


def check_staleness():
    return _updater.check_staleness()


def cmd_update_from_transcript(transcript_path: str, dry_run: bool = False, max_docs: int = 3):
    return _updater.cmd_update_from_transcript(transcript_path, dry_run=dry_run, max_docs=max_docs)


def append_project_logs(project_logs: dict[str, list[str]], trigger: str = "Compaction", dry_run: bool = False):
    return _append_project_logs(project_logs, trigger=trigger, dry_run=dry_run)


__all__ = [
    "check_staleness",
    "cmd_update_from_transcript",
    "append_project_logs",
]
