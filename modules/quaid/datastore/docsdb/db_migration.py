"""One-time migration helpers for shared DocsDB storage.

Moves legacy per-instance docs tables into the shared docs database so users
do not need to reindex after the shared-RAG rollout.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List

from lib.instance import quaid_home as _quaid_home

logger = logging.getLogger(__name__)

_MIGRATION_LOCK = threading.Lock()
_PROCESS_DONE_KEYS: set[tuple[str, tuple[str, ...]]] = set()

_MIGRATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS docs_db_migration_state (
    source_db TEXT NOT NULL,
    table_name TEXT NOT NULL,
    source_sig TEXT NOT NULL,
    migrated_at TEXT NOT NULL,
    PRIMARY KEY(source_db, table_name)
)
"""


def _now_iso() -> str:
    raw = os.environ.get("QUAID_NOW", "").strip()
    if raw:
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"Invalid QUAID_NOW={raw!r}") from None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)
        return value.isoformat()
    return datetime.now(timezone.utc).isoformat()


def migrate_legacy_docs_tables(shared_db_path: Path, tables: Iterable[str]) -> None:
    """Best-effort merge of legacy docs tables into shared DB.

    Runs quickly when no legacy changes are present. Uses per-source signatures
    so unchanged legacy DBs are skipped on subsequent starts.
    """
    wanted = tuple(sorted({str(t or "").strip() for t in (tables or []) if str(t or "").strip()}))
    if not wanted:
        return

    target = Path(shared_db_path).expanduser()
    try:
        target_key = str(target.resolve())
    except Exception:
        target_key = str(target)
    cache_key = (target_key, wanted)

    with _MIGRATION_LOCK:
        if cache_key in _PROCESS_DONE_KEYS:
            return
        _migrate_locked(target, wanted)
        _PROCESS_DONE_KEYS.add(cache_key)


def _migrate_locked(target_db: Path, wanted_tables: tuple[str, ...]) -> None:
    target_db.parent.mkdir(parents=True, exist_ok=True)
    source_dbs = _legacy_instance_db_paths(target_db)
    if not source_dbs:
        return

    conn = sqlite3.connect(str(target_db))
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute(_MIGRATION_TABLE_SQL)
        conn.commit()

        target_tables = _list_tables(conn, schema="main")
        wanted_in_target = [t for t in wanted_tables if t in target_tables]
        if not wanted_in_target:
            return

        for source_db in source_dbs:
            try:
                source_sig = _db_signature(source_db)
            except Exception as exc:
                logger.warning(
                    "Legacy docs DB merge skipped for %s: failed reading DB signature: %s",
                    source_db,
                    exc,
                )
                continue

            alias = "src_docs"
            try:
                conn.execute(f"ATTACH DATABASE ? AS {alias}", (str(source_db),))
                src_tables = _list_tables(conn, schema=alias)
                for table_name in wanted_in_target:
                    if table_name not in src_tables:
                        continue
                    if _migration_state_matches(conn, str(source_db), table_name, source_sig):
                        continue
                    migrated = _copy_table_rows(conn, alias, table_name)
                    if migrated:
                        logger.info(
                            "Merged legacy docs table %s from %s into shared docs DB",
                            table_name,
                            source_db,
                        )
                    _write_migration_state(conn, str(source_db), table_name, source_sig)
                conn.commit()
            except Exception as exc:
                if isinstance(exc, ValueError) and str(exc).startswith("Invalid QUAID_NOW="):
                    raise
                logger.warning(
                    "Legacy docs DB merge skipped for %s: %s",
                    source_db,
                    exc,
                )
            finally:
                try:
                    conn.execute(f"DETACH DATABASE {alias}")
                except Exception:
                    pass
    finally:
        conn.close()


def _legacy_instance_db_paths(target_db: Path) -> List[Path]:
    home = _quaid_home()
    instances_dir = home / "instances"
    if not instances_dir.is_dir():
        return []

    out: List[Path] = []
    for child in sorted(instances_dir.iterdir()):
        if not child.is_dir():
            continue
        candidate = child / "data" / "memory.db"
        if not candidate.is_file():
            continue
        try:
            if candidate.resolve() == target_db.resolve():
                continue
        except Exception:
            if str(candidate) == str(target_db):
                continue
        out.append(candidate)
    return out


def _db_signature(path: Path) -> str:
    st = path.stat()
    return f"{int(st.st_mtime_ns)}:{int(st.st_size)}"


def _list_tables(conn: sqlite3.Connection, *, schema: str) -> set[str]:
    rows = conn.execute(
        f"SELECT name FROM {schema}.sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {str(r[0]) for r in rows}


def _table_columns(conn: sqlite3.Connection, *, schema: str, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA {schema}.table_info('{table}')").fetchall()
    return [str(r[1]) for r in rows]


def _copy_table_rows(conn: sqlite3.Connection, source_schema: str, table_name: str) -> bool:
    src_cols = _table_columns(conn, schema=source_schema, table=table_name)
    dst_cols = _table_columns(conn, schema="main", table=table_name)
    if not src_cols or not dst_cols:
        return False
    common_cols = [c for c in dst_cols if c in src_cols]
    if not common_cols:
        return False

    cols_sql = ", ".join(f'"{c}"' for c in common_cols)
    sql = (
        f'INSERT OR REPLACE INTO "{table_name}" ({cols_sql}) '
        f'SELECT {cols_sql} FROM {source_schema}."{table_name}"'
    )
    conn.execute(sql)
    return True


def _migration_state_matches(
    conn: sqlite3.Connection,
    source_db: str,
    table_name: str,
    source_sig: str,
) -> bool:
    row = conn.execute(
        """
        SELECT source_sig
        FROM docs_db_migration_state
        WHERE source_db = ? AND table_name = ?
        """,
        (source_db, table_name),
    ).fetchone()
    return bool(row and str(row[0] or "") == source_sig)


def _write_migration_state(
    conn: sqlite3.Connection,
    source_db: str,
    table_name: str,
    source_sig: str,
) -> None:
    conn.execute(
        """
        INSERT INTO docs_db_migration_state (source_db, table_name, source_sig, migrated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(source_db, table_name)
        DO UPDATE SET
            source_sig = excluded.source_sig,
            migrated_at = excluded.migrated_at
        """,
        (
            source_db,
            table_name,
            source_sig,
            _now_iso(),
        ),
    )
