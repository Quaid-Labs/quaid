"""Core wrapper for the InsightDB soul snippets implementation."""

from __future__ import annotations

def _soul_snippets_module():
    from datastore.insightdb import soul_snippets

    return soul_snippets


def write_journal_entry(filename: str, content: str, trigger: str = "Compaction", date_str: str | None = None) -> bool:
    return _soul_snippets_module().write_journal_entry(
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
    return _soul_snippets_module().write_snippet_entry(
        filename=filename,
        snippets=snippets,
        trigger=trigger,
        date_str=date_str,
        time_str=time_str,
    )


_CANONICAL_EXPORTS = {
    "run_soul_snippets_review",
    "run_journal_distillation",
}


def __getattr__(name: str):
    if name in _CANONICAL_EXPORTS:
        return getattr(_soul_snippets_module(), name)
    raise AttributeError(name)


__all__ = [
    "run_journal_distillation",  # noqa: F822 - exported through module __getattr__
    "run_soul_snippets_review",  # noqa: F822 - exported through module __getattr__
    "write_journal_entry",
    "write_snippet_entry",
]
