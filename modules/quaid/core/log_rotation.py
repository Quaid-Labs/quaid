"""Log rotation for append-only log files (PROJECT.log, journal entries).

Token-budget-based rotation: keeps recent entries within a configurable
token budget, archives the rest into monthly files. Never splits an entry.

Rotation is triggered after distillation, not on daemon ticks.

See docs/PROJECT-SYSTEM-SPEC.md#project-log-rotation.
"""

import logging
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Default token budget for the recent log window.
# Overridable via config: projects.logTokenBudget
DEFAULT_LOG_TOKEN_BUDGET = 4000

# Rough token estimate: ~4 chars per token (conservative for English text)
_CHARS_PER_TOKEN = 4

# Matches ISO timestamps like [2026-03-11T10:00:00] at the start of log lines
_TS_PATTERN = re.compile(r"^\s*-\s*\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")

# Matches date-only timestamps like [2026-03-11]
_DATE_PATTERN = re.compile(r"^\s*-\s*\[(\d{4}-\d{2}-\d{2})")


def _estimate_tokens(text: str) -> int:
    """Rough token estimate. Conservative (overestimates slightly)."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _parse_line_timestamp(line: str) -> Optional[datetime]:
    """Extract a timestamp from a log line, or None if not found."""
    m = _TS_PATTERN.match(line)
    if m:
        try:
            return datetime.fromisoformat(m.group(1))
        except ValueError:
            pass
    m = _DATE_PATTERN.match(line)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d")
        except ValueError:
            pass
    return None


def _get_log_token_budget() -> int:
    """Get configured token budget for log files."""
    try:
        from config import get_config
        cfg = get_config()
        budget = getattr(getattr(cfg, "projects", None), "log_token_budget", 0)
        if budget and int(budget) > 0:
            return int(budget)
    except Exception:
        pass
    return DEFAULT_LOG_TOKEN_BUDGET


def rotate_log_file(
    log_file: Path,
    archive_dir: Optional[Path] = None,
    token_budget: Optional[int] = None,
) -> Tuple[int, int]:
    """Rotate a log file, archiving old entries that exceed the token budget.

    Keeps the most recent entries that fit within the token budget.
    Older entries are archived into monthly files. Never splits an entry.

    Args:
        log_file: Path to the append-only log file (e.g. PROJECT.log)
        archive_dir: Directory for monthly archives. Defaults to log_file.parent/log/
        token_budget: Max tokens for the recent file. Defaults to config value.

    Returns:
        (entries_archived, entries_kept) tuple
    """
    if not log_file.is_file():
        return 0, 0

    if archive_dir is None:
        archive_dir = log_file.parent / "log"

    if token_budget is None:
        token_budget = _get_log_token_budget()

    lines = log_file.read_text(encoding="utf-8").splitlines()
    if not lines:
        return 0, 0

    # Work backwards from the end to find the cut point.
    # Keep as many recent entries as fit in the token budget.
    # Guarantee: at least the most recent entry is always kept.
    tokens_used = 0
    cut_index = len(lines)  # Index where "recent" starts
    first_entry_seen = False

    for i in range(len(lines) - 1, -1, -1):
        line = lines[i]
        if not line.strip():
            continue
        line_tokens = _estimate_tokens(line)
        if tokens_used + line_tokens > token_budget and first_entry_seen:
            cut_index = i + 1  # This line doesn't fit — cut here
            break
        tokens_used += line_tokens
        first_entry_seen = True
    else:
        cut_index = 0  # Everything fits

    if cut_index == 0:
        return 0, len(lines)  # Nothing to archive

    to_archive = lines[:cut_index]
    recent = lines[cut_index:]

    # Group archived entries by month
    archived_by_month = defaultdict(list)
    for line in to_archive:
        if not line.strip():
            continue
        ts = _parse_line_timestamp(line)
        month_key = ts.strftime("%Y-%m") if ts else "undated"
        archived_by_month[month_key].append(line)

    if not archived_by_month:
        return 0, len(recent)

    # Write archives
    archive_dir.mkdir(parents=True, exist_ok=True)
    total_archived = 0
    for month, month_lines in sorted(archived_by_month.items()):
        archive_file = archive_dir / f"{month}.log"
        try:
            with archive_file.open("a", encoding="utf-8") as f:
                f.write("\n".join(month_lines) + "\n")
            total_archived += len(month_lines)
        except OSError as e:
            logger.warning("[log-rotation] Failed to write archive %s: %s", archive_file, e)
            # Put these back in recent so we don't lose them
            recent = month_lines + recent

    # Rewrite the main file with only recent entries
    try:
        log_file.write_text(
            "\n".join(recent) + "\n" if recent else "",
            encoding="utf-8",
        )
    except OSError as e:
        logger.error("[log-rotation] Failed to rewrite %s: %s", log_file, e)

    logger.info(
        "[log-rotation] %s: archived=%d kept=%d tokens_used=%d/%d",
        log_file.name, total_archived, len(recent), tokens_used, token_budget,
    )
    return total_archived, len(recent)


def rotate_project_logs(projects_dir: Path, **kwargs) -> int:
    """Rotate PROJECT.log for all projects. Call after distillation.

    Args:
        projects_dir: QUAID_HOME/projects/
        **kwargs: Passed to rotate_log_file (token_budget)

    Returns:
        Total entries archived across all projects.
    """
    if not projects_dir.is_dir():
        return 0

    total = 0
    for project_dir in sorted(projects_dir.iterdir()):
        if not project_dir.is_dir() or project_dir.name.startswith("."):
            continue
        log_file = project_dir / "PROJECT.log"
        if log_file.is_file():
            archived, _ = rotate_log_file(log_file, **kwargs)
            total += archived
    return total


def rotate_journal_logs(journal_dir: Path, **kwargs) -> int:
    """Rotate journal log files. Call after journal distillation.

    Args:
        journal_dir: QUAID_HOME/journal/
        **kwargs: Passed to rotate_log_file

    Returns:
        Total entries archived.
    """
    if not journal_dir.is_dir():
        return 0

    total = 0
    for log_file in sorted(journal_dir.glob("*.journal.md")):
        if log_file.is_file():
            archive_dir = journal_dir / "archive" / log_file.stem
            archived, _ = rotate_log_file(log_file, archive_dir=archive_dir, **kwargs)
            total += archived
    return total
