"""Shared database connection factory.

Provides a configured SQLite connection with Row factory and FK enforcement.
All consumers should use this instead of inline sqlite3.connect() calls.

Usage:
    with get_connection() as conn:
        conn.execute("SELECT ...")
    # Connection is automatically committed and closed.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from .config import get_db_path

# sqlite-vec: optional vector search extension
_has_sqlite_vec = False
try:
    import sqlite_vec
    _has_sqlite_vec = True
except ImportError:
    pass


def has_vec() -> bool:
    """Return True if sqlite-vec is available."""
    return _has_sqlite_vec


@contextmanager
def get_connection(db_path: Optional[Path] = None):
    """Get a configured SQLite connection as a context manager.

    Args:
        db_path: Override DB path. Defaults to config-derived main DB path.

    Yields:
        sqlite3.Connection with row_factory=sqlite3.Row and FK enforcement.
        If sqlite-vec is installed, the extension is loaded automatically.
        Commits on clean exit, rolls back on exception, always closes.
    """
    path = db_path or get_db_path()
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")  # 30s wait for concurrent access
    # Force WAL mode on every connection open to avoid check-then-set races.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous = NORMAL")   # Safe with WAL, faster than FULL
    conn.execute("PRAGMA cache_size = -64000")     # 64MB page cache
    conn.execute("PRAGMA temp_store = MEMORY")     # Temp tables in memory
    # Load sqlite-vec if available
    if _has_sqlite_vec:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
