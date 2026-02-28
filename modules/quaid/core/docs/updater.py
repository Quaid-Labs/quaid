"""Core wrapper for docs updater datastore implementation."""

from __future__ import annotations

from datastore.docsdb import updater as _updater


def check_staleness():
    return _updater.check_staleness()


def cmd_update_from_transcript(transcript_path: str, dry_run: bool = False, max_docs: int = 3):
    return _updater.cmd_update_from_transcript(transcript_path, dry_run=dry_run, max_docs=max_docs)


__all__ = [
    "check_staleness",
    "cmd_update_from_transcript",
]
