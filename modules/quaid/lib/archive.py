"""Archive database for decayed memories."""

import json
import logging
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional


def _default_archive_path() -> Path:
    """Get archive DB path from config (lazy to avoid import cycle)."""
    try:
        from .config import get_archive_db_path
        return get_archive_db_path()
    except Exception:
        from .adapter import get_adapter
        return get_adapter().data_dir() / "memory_archive.db"


logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS archived_nodes (
    id TEXT PRIMARY KEY,
    type TEXT,
    name TEXT,
    attributes TEXT,
    confidence REAL,
    speaker TEXT,
    owner_id TEXT,
    created_at TEXT,
    accessed_at TEXT,
    access_count INTEGER,
    archived_at TEXT DEFAULT (datetime('now')),
    archive_reason TEXT
);
"""


@contextmanager
def _get_archive_conn(db_path: Path = None):
    """Get connection to archive database, creating schema if needed.

    Yields a connection, commits on clean exit, rolls back on exception,
    always closes the connection (prevents fd leaks).
    """
    if db_path is None:
        db_path = _default_archive_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    for statement in _SCHEMA.split(";"):
        stmt = statement.strip()
        if stmt:
            conn.execute(stmt)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def archive_node(node_dict: Dict[str, Any], reason: str,
                 db_path: Path = None) -> bool:
    """Archive a node dict to the archive database.

    Args:
        node_dict: Dict with keys matching archived_nodes columns.
        reason: Why the node was archived (e.g. 'confidence_decay', 'decay_review_delete').
        db_path: Override archive DB path (useful for testing). None = use config.
    """
    try:
        with _get_archive_conn(db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO archived_nodes
                (id, type, name, attributes, confidence, speaker, owner_id,
                 created_at, accessed_at, access_count, archive_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                node_dict.get("id"),
                node_dict.get("type"),
                node_dict.get("name") or node_dict.get("text"),
                json.dumps(node_dict.get("attributes", {})) if isinstance(node_dict.get("attributes"), dict) else node_dict.get("attributes"),
                node_dict.get("confidence"),
                node_dict.get("speaker"),
                node_dict.get("owner_id"),
                node_dict.get("created_at"),
                node_dict.get("accessed_at"),
                node_dict.get("access_count", 0),
                reason,
            ))
        return True
    except Exception as e:
        logger.error("archive_node failed: %s", e)
        return False


def search_archive(query: str, limit: int = 10,
                   db_path: Path = None) -> List[Dict[str, Any]]:
    """Simple text search of archive database."""
    try:
        with _get_archive_conn(db_path) as conn:
            escaped = query.replace("%", r"\%").replace("_", r"\_")
            rows = conn.execute("""
                SELECT * FROM archived_nodes
                WHERE name LIKE ? ESCAPE '\\'
                ORDER BY archived_at DESC
                LIMIT ?
            """, (f"%{escaped}%", limit)).fetchall()
        return [dict(row) for row in rows]
    except Exception as e:
        logger.warning("search_archive failed: %s", e)
        return []
