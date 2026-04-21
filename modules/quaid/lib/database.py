"""Shared database connection factory.

Provides a configured SQLite connection with Row factory and FK enforcement.
All consumers should use this instead of inline sqlite3.connect() calls.

Usage:
    with get_connection() as conn:
        conn.execute("SELECT ...")
    # Connection is automatically committed and closed.
"""

import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from .config import get_db_path

logger = logging.getLogger(__name__)

_CONNECT_RETRY_DELAYS_SECONDS = (0.1, 0.25, 0.5, 1.0, 2.0)
_TRANSIENT_SQLITE_SETUP_ERRORS = (
    "database is locked",
    "database is busy",
    "locking protocol",
)

# sqlite-vec: optional vector search extension.
# Verified at import time against an in-memory connection so that _has_sqlite_vec
# reflects whether vec0 actually works — not just whether the package imports.
_has_sqlite_vec = False
_sqlite_vec_mod = None
try:
    import sqlite_vec as _sqlite_vec_mod  # type: ignore[import]
    _test = sqlite3.connect(":memory:")
    try:
        _test.enable_load_extension(True)
        _sqlite_vec_mod.load(_test)
        _test.enable_load_extension(False)
        _test.execute("SELECT vec_version()")
        _has_sqlite_vec = True
    except Exception as _e:
        logger.warning(
            "sqlite-vec imported but vec0 unavailable (extension load/verify failed: %s); "
            "vector search disabled",
            _e,
        )
    finally:
        _test.close()
        del _test
except ImportError:
    pass


def has_vec() -> bool:
    """Return True if sqlite-vec is available and vec0 is functional."""
    return _has_sqlite_vec


def _load_vec(conn: sqlite3.Connection) -> None:
    """Load sqlite-vec into conn. No-op and silent if not available."""
    if not _has_sqlite_vec or _sqlite_vec_mod is None:
        return
    try:
        conn.enable_load_extension(True)
        _sqlite_vec_mod.load(conn)
        conn.enable_load_extension(False)
    except Exception as exc:
        logger.warning("sqlite-vec per-connection load failed: %s; vec operations may error", exc)


def _is_transient_sqlite_setup_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _TRANSIENT_SQLITE_SETUP_ERRORS)


def _open_configured_connection(path: Path) -> sqlite3.Connection:
    """Open a SQLite connection and absorb transient setup contention with retries.

    `PRAGMA journal_mode=WAL` is intentionally set at connection setup, but it
    can briefly need a database lock. A transient setup lock should not kill the
    long-lived supervisor; if contention persists after bounded retries, callers
    still get the original sqlite error.
    """
    attempts = len(_CONNECT_RETRY_DELAYS_SECONDS) + 1
    last_exc: sqlite3.OperationalError | None = None
    for attempt in range(1, attempts + 1):
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(str(path), timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 30000")  # 30s wait for concurrent access
            # Force WAL mode on every connection open to avoid check-then-set races.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous = NORMAL")   # Safe with WAL, faster than FULL
            conn.execute("PRAGMA cache_size = -64000")     # 64MB page cache
            conn.execute("PRAGMA temp_store = MEMORY")     # Temp tables in memory
            _load_vec(conn)
            return conn
        except sqlite3.OperationalError as exc:
            if conn is not None:
                conn.close()
            last_exc = exc
            if not _is_transient_sqlite_setup_error(exc) or attempt >= attempts:
                raise
            delay = _CONNECT_RETRY_DELAYS_SECONDS[attempt - 1]
            logger.warning(
                "sqlite connection setup contention for %s on attempt %d/%d: %s; retrying in %.2fs",
                path,
                attempt,
                attempts,
                exc,
                delay,
            )
            time.sleep(delay)
        except Exception:
            if conn is not None:
                conn.close()
            raise
    assert last_exc is not None
    raise last_exc


def refresh_read_visibility(conn: sqlite3.Connection) -> None:
    """Best-effort WAL visibility refresh before cross-process vector reads.

    Hook recall commonly reads immediately after a separate CLI process has
    written vec_nodes entries. A passive checkpoint is cheap and avoids a class
    of "fresh write not yet visible to this reader" races without blocking
    active writers.
    """
    try:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except Exception as exc:
        logger.debug("wal checkpoint pre-read refresh skipped: %s", exc)


@contextmanager
def get_connection(db_path: Optional[Path] = None):
    """Get a configured SQLite connection as a context manager.

    Args:
        db_path: Override DB path. Defaults to config-derived main DB path.

    Yields:
        sqlite3.Connection with row_factory=sqlite3.Row and FK enforcement.
        If sqlite-vec is installed and functional, the extension is loaded.
        Commits on clean exit, rolls back on exception, always closes.
    """
    path = db_path or get_db_path()
    conn = _open_configured_connection(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
