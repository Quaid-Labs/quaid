"""Per-project flock for shared project doc updates.

Prevents multiple instance janitors from redundantly running LLM doc updates
on the same shared project simultaneously.

Lock:       QUAID_HOME/shared/projects/<name>/.doc-update.lock
Checkpoint: read from the project's existing janitor checkpoint mechanism

Usage:
    from lib.shared_project_lock import try_claim_project_update

    with try_claim_project_update(quaid_home, project_name, max_age_seconds=3600) as claimed:
        if not claimed:
            return  # Another instance handled it or just finished
        ... do LLM work ...
        ... write docs ...
"""

import fcntl
import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional

logger = logging.getLogger("quaid.shared_project_lock")


def _lock_path(quaid_home: Path, project_name: str) -> Path:
    return quaid_home / "shared" / "projects" / project_name / ".doc-update.lock"


def _checkpoint_path(quaid_home: Path, project_name: str) -> Path:
    return quaid_home / "shared" / "projects" / project_name / ".doc-update-checkpoint"


def _read_checkpoint_age(quaid_home: Path, project_name: str) -> Optional[float]:
    """Return seconds since last successful doc update, or None if never run."""
    cp = _checkpoint_path(quaid_home, project_name)
    if not cp.is_file():
        return None
    try:
        ts = float(cp.read_text().strip())
        return time.time() - ts
    except (ValueError, OSError):
        return None


def write_checkpoint(quaid_home: Path, project_name: str) -> None:
    """Write a fresh checkpoint timestamp after a successful doc update."""
    cp = _checkpoint_path(quaid_home, project_name)
    try:
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(str(time.time()) + "\n")
    except OSError as e:
        logger.warning("write_checkpoint failed for %s: %s", project_name, e)


@contextmanager
def try_claim_project_update(
    quaid_home: Path,
    project_name: str,
    max_age_seconds: float = 3600.0,
) -> Generator[bool, None, None]:
    """Context manager that yields True if this instance should do the update.

    Pattern:
        1. Non-blocking flock attempt
        2. Miss → read checkpoint; if fresh, skip (someone else is working or just finished)
        3. Hit → double-check checkpoint (close TOCTOU gap)
        4. Fresh after lock → release and skip
        5. Stale after lock → yield True (caller does work, then calls write_checkpoint)
        6. Release lock

    Yields False in all skip cases so callers can log/skip cleanly.
    """
    lock_file = _lock_path(quaid_home, project_name)
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        fd = open(lock_file, "w")
    except OSError as e:
        logger.warning("cannot open project lock for %s: %s — skipping", project_name, e)
        yield False
        return

    acquired = False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
    except OSError:
        # Could not acquire — check if recently updated
        age = _read_checkpoint_age(quaid_home, project_name)
        if age is not None and age < max_age_seconds:
            logger.debug(
                "project %s doc update skipped: lock busy, checkpoint fresh (%.0fs ago)",
                project_name, age,
            )
        else:
            logger.debug(
                "project %s doc update skipped: lock busy, checkpoint stale or absent",
                project_name,
            )
        fd.close()
        yield False
        return

    # Lock acquired — double-check checkpoint
    age = _read_checkpoint_age(quaid_home, project_name)
    if age is not None and age < max_age_seconds:
        logger.debug(
            "project %s doc update skipped: checkpoint fresh after lock (%.0fs ago)",
            project_name, age,
        )
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()
        yield False
        return

    # We hold the lock and the checkpoint is stale — this instance should do the work
    try:
        yield True
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()
