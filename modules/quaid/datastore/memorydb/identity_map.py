"""Datastore-owned identity handle mapping for multi-user/group-chat groundwork."""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from lib.database import get_connection as _lib_get_connection


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS identity_handles (
            id TEXT PRIMARY KEY,
            owner_id TEXT,
            source_channel TEXT NOT NULL,
            conversation_id TEXT,
            handle TEXT NOT NULL,
            canonical_entity_id TEXT NOT NULL,
            confidence REAL DEFAULT 1.0,
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(owner_id, source_channel, conversation_id, handle)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_identity_handles_lookup ON identity_handles(owner_id, source_channel, conversation_id, handle)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_identity_handles_entity ON identity_handles(canonical_entity_id)"
    )


def upsert_identity_handle(
    *,
    owner_id: Optional[str],
    source_channel: str,
    handle: str,
    canonical_entity_id: str,
    conversation_id: Optional[str] = None,
    confidence: float = 1.0,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    now = _utcnow_iso()
    owner = str(owner_id or "").strip() or None
    channel = str(source_channel or "").strip().lower()
    conv = str(conversation_id or "").strip() or None
    raw_handle = str(handle or "").strip()
    canonical = str(canonical_entity_id or "").strip()
    if not channel:
        raise ValueError("source_channel is required")
    if not raw_handle:
        raise ValueError("handle is required")
    if not canonical:
        raise ValueError("canonical_entity_id is required")

    with _lib_get_connection() as conn:
        ensure_schema(conn)
        row = conn.execute(
            """
            SELECT id FROM identity_handles
            WHERE owner_id IS ? AND source_channel = ? AND conversation_id IS ? AND handle = ?
            """,
            (owner, channel, conv, raw_handle),
        ).fetchone()
        row_id = str(row["id"]) if row else str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO identity_handles (
                id, owner_id, source_channel, conversation_id, handle,
                canonical_entity_id, confidence, notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(owner_id, source_channel, conversation_id, handle) DO UPDATE SET
                canonical_entity_id = excluded.canonical_entity_id,
                confidence = excluded.confidence,
                notes = excluded.notes,
                updated_at = excluded.updated_at
            """,
            (row_id, owner, channel, conv, raw_handle, canonical, float(confidence), notes, now, now),
        )
    return {
        "id": row_id,
        "owner_id": owner,
        "source_channel": channel,
        "conversation_id": conv,
        "handle": raw_handle,
        "canonical_entity_id": canonical,
        "confidence": float(confidence),
        "notes": notes,
    }


def resolve_identity_handle(
    *,
    owner_id: Optional[str],
    source_channel: str,
    handle: str,
    conversation_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    owner = str(owner_id or "").strip() or None
    channel = str(source_channel or "").strip().lower()
    conv = str(conversation_id or "").strip() or None
    raw_handle = str(handle or "").strip()
    if not channel or not raw_handle:
        return None

    with _lib_get_connection() as conn:
        ensure_schema(conn)
        # Prefer conversation-scoped handle resolution, then channel-global.
        row = conn.execute(
            """
            SELECT * FROM identity_handles
            WHERE owner_id IS ? AND source_channel = ? AND conversation_id IS ? AND handle = ?
            ORDER BY updated_at DESC LIMIT 1
            """,
            (owner, channel, conv, raw_handle),
        ).fetchone()
        if not row and conv is not None:
            row = conn.execute(
                """
                SELECT * FROM identity_handles
                WHERE owner_id IS ? AND source_channel = ? AND conversation_id IS NULL AND handle = ?
                ORDER BY updated_at DESC LIMIT 1
                """,
                (owner, channel, raw_handle),
            ).fetchone()
    return dict(row) if row else None


def list_identity_handles(
    *,
    owner_id: Optional[str] = None,
    source_channel: Optional[str] = None,
    canonical_entity_id: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    lim = max(1, min(int(limit or 100), 1000))
    owner = str(owner_id or "").strip() or None
    channel = str(source_channel or "").strip().lower() or None
    canonical = str(canonical_entity_id or "").strip() or None
    where = []
    params: List[Any] = []
    if owner is not None:
        where.append("owner_id IS ?")
        params.append(owner)
    if channel is not None:
        where.append("source_channel = ?")
        params.append(channel)
    if canonical is not None:
        where.append("canonical_entity_id = ?")
        params.append(canonical)
    sql = "SELECT * FROM identity_handles"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(lim)

    with _lib_get_connection() as conn:
        ensure_schema(conn)
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [dict(r) for r in rows]


def _main() -> int:
    parser = argparse.ArgumentParser(description="Identity handle map datastore")
    sub = parser.add_subparsers(dest="command")

    put_p = sub.add_parser("put", help="Insert/update identity handle mapping")
    put_p.add_argument("--owner", default=None)
    put_p.add_argument("--channel", required=True)
    put_p.add_argument("--conversation-id", default=None)
    put_p.add_argument("--handle", required=True)
    put_p.add_argument("--entity-id", required=True)
    put_p.add_argument("--confidence", type=float, default=1.0)
    put_p.add_argument("--notes", default=None)

    get_p = sub.add_parser("get", help="Resolve handle mapping")
    get_p.add_argument("--owner", default=None)
    get_p.add_argument("--channel", required=True)
    get_p.add_argument("--conversation-id", default=None)
    get_p.add_argument("--handle", required=True)

    list_p = sub.add_parser("list", help="List mappings")
    list_p.add_argument("--owner", default=None)
    list_p.add_argument("--channel", default=None)
    list_p.add_argument("--entity-id", default=None)
    list_p.add_argument("--limit", type=int, default=100)

    args = parser.parse_args()
    if args.command == "put":
        print(json.dumps(upsert_identity_handle(
            owner_id=args.owner,
            source_channel=args.channel,
            conversation_id=args.conversation_id,
            handle=args.handle,
            canonical_entity_id=args.entity_id,
            confidence=args.confidence,
            notes=args.notes,
        )))
        return 0
    if args.command == "get":
        print(json.dumps(resolve_identity_handle(
            owner_id=args.owner,
            source_channel=args.channel,
            conversation_id=args.conversation_id,
            handle=args.handle,
        )))
        return 0
    if args.command == "list":
        print(json.dumps({"mappings": list_identity_handles(
            owner_id=args.owner,
            source_channel=args.channel,
            canonical_entity_id=args.entity_id,
            limit=args.limit,
        )}))
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())

