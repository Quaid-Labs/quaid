"""Core wrapper for the EvolutionDB soul snippets implementation."""

from __future__ import annotations

from datastore.evolutiondb import soul_snippets as _soul_snippets


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


# These maintenance callables are intentionally bound at import time. Tests or
# callers that monkeypatch them should patch the canonical EvolutionDB module.
run_soul_snippets_review = _soul_snippets.run_soul_snippets_review
run_journal_distillation = _soul_snippets.run_journal_distillation


__all__ = [
    "run_journal_distillation",
    "run_soul_snippets_review",
    "write_journal_entry",
    "write_snippet_entry",
]
