"""Core wrapper for notedb soul snippets implementation."""

from __future__ import annotations

from datastore.notedb import soul_snippets as _soul_snippets


def write_journal_entry(filename: str, content: str, trigger: str = "Compaction", date_str: str | None = None) -> bool:
    return _soul_snippets.write_journal_entry(
        filename=filename,
        content=content,
        trigger=trigger,
        date_str=date_str,
    )


def write_snippet_entry(
    filename: str,
    snippets: list[str],
    trigger: str = "Compaction",
    date_str: str | None = None,
    time_str: str | None = None,
) -> bool:
    return _soul_snippets.write_snippet_entry(
        filename=filename,
        snippets=snippets,
        trigger=trigger,
        date_str=date_str,
        time_str=time_str,
    )


__all__ = [
    "write_journal_entry",
    "write_snippet_entry",
]
