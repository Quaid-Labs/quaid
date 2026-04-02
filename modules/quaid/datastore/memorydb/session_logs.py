"""Datastore-owned session log indexing and retrieval.

Owns semantic/lexical indexing for session transcripts so adapter/core layers only
orchestrate and do not hold retrieval logic.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from lib.config import get_db_path as _lib_get_db_path
from lib.database import get_connection as _lib_get_connection

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_session_id(session_id: str) -> str:
    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("session_id is required")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", sid):
        raise ValueError("invalid session_id")
    return sid


def _is_quaid_process(pid: int) -> bool:
    """Return True if the given PID is running quaid code (not an unrelated recycled PID)."""
    try:
        cmdline_path = Path(f"/proc/{pid}/cmdline")
        if cmdline_path.exists():
            cmdline = cmdline_path.read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="replace")
            return "quaid" in cmdline
        import subprocess
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="],
            capture_output=True, text=True, timeout=2,
        )
        return "quaid" in result.stdout
    except Exception:
        # If we can't verify, assume it's a live quaid process (safe default).
        return True


def _is_stale_lock(lock_path: str) -> bool:
    """Return True if the lock file is orphaned (owning process is dead or PID was reused)."""
    try:
        with open(lock_path, "r") as f:
            pid = int(f.read().strip())
        # Lock is stale if the recorded PID is no longer running.
        os.kill(pid, 0)
        # PID is alive — but verify it's actually a quaid process, not a recycled PID
        # from an unrelated process that happened to inherit the same PID after a crash.
        if not _is_quaid_process(pid):
            return True  # PID reuse — treat as stale.
        return False  # Process is alive and is a quaid process — lock is live.
    except (ValueError, FileNotFoundError):
        # No PID or file gone — treat as stale.
        return True
    except OSError:
        # kill(pid, 0) raises OSError(ESRCH) if process is dead.
        return True


def _with_session_lock(session_id: str) -> tuple[int, str]:
    lock_path = f"{_lib_get_db_path()}.session-{session_id}.lock"
    last_err: Optional[Exception] = None
    for attempt in range(300):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            # Write our PID so staleness can be detected if we crash.
            os.write(fd, str(os.getpid()).encode())
            return fd, lock_path
        except FileExistsError as exc:
            last_err = exc
            if _is_stale_lock(lock_path):
                try:
                    os.unlink(lock_path)
                except FileNotFoundError:
                    pass
                continue  # Retry immediately after clearing stale lock.
            if attempt == 299:
                raise RuntimeError(f"failed to acquire session log lock for {session_id}: {last_err}")
            time.sleep(0.2)


def _infer_topic_hint(transcript: str) -> str:
    for line in str(transcript or "").splitlines():
        line = line.strip()
        if line.lower().startswith("user:"):
            body = line[5:].strip()
            if body:
                return body[:140]
    return ""


def ensure_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_logs (
            session_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            source_label TEXT,
            source_path TEXT,
            source_channel TEXT,
            conversation_id TEXT,
            participant_ids TEXT,
            participant_aliases TEXT,
            message_count INTEGER DEFAULT 0,
            topic_hint TEXT,
            transcript_text TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            indexed_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    # Forward-compatible additive migrations for older DBs.
    for col, ddl in [
        ("source_channel", "TEXT"),
        ("conversation_id", "TEXT"),
        ("participant_ids", "TEXT"),
        ("participant_aliases", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE session_logs ADD COLUMN {col} {ddl}")
        except Exception as exc:
            logger.warning("session_logs migration skipped for column=%s: %s", col, exc)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_session_logs_owner_updated ON session_logs(owner_id, updated_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_session_logs_updated ON session_logs(updated_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_session_logs_conversation ON session_logs(conversation_id, updated_at DESC)")


def index_session_log(
    *,
    session_id: str,
    transcript: str,
    owner_id: str = "default",
    source_label: str = "unknown",
    source_path: Optional[str] = None,
    source_channel: Optional[str] = None,
    conversation_id: Optional[str] = None,
    participant_ids: Optional[List[str]] = None,
    participant_aliases: Optional[Dict[str, str]] = None,
    message_count: Optional[int] = None,
    topic_hint: Optional[str] = None,
) -> Dict[str, Any]:
    sid = _normalize_session_id(session_id)
    lock_fd, lock_path = _with_session_lock(sid)
    try:
        transcript_text = str(transcript or "").strip()
        if not transcript_text:
            return {"status": "skipped", "reason": "empty_transcript", "session_id": sid}

        owner = str(owner_id or "default").strip() or "default"
        now = _utcnow_iso()
        hint = str(topic_hint or "").strip() or _infer_topic_hint(transcript_text)
        msg_count = int(message_count or 0)
        if msg_count <= 0:
            msg_count = transcript_text.count("\n\n") + 1

        content_hash = hashlib.sha256(transcript_text.encode("utf-8")).hexdigest()

        with _lib_get_connection() as conn:
            ensure_schema(conn)
            prev = conn.execute(
                "SELECT content_hash FROM session_logs WHERE session_id = ?",
                (sid,),
            ).fetchone()
            if prev and str(prev["content_hash"]) == content_hash:
                conn.execute(
                    """
                    UPDATE session_logs
                    SET owner_id = ?, source_label = ?, source_path = ?, source_channel = ?, conversation_id = ?,
                        participant_ids = ?, participant_aliases = ?, message_count = ?, topic_hint = ?, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (
                        owner,
                        str(source_label or "unknown"),
                        source_path,
                        str(source_channel or "").strip() or None,
                        str(conversation_id or "").strip() or None,
                        json.dumps(participant_ids or [], ensure_ascii=True),
                        json.dumps(participant_aliases or {}, ensure_ascii=True),
                        msg_count,
                        hint,
                        now,
                        sid,
                    ),
                )
                return {
                    "status": "unchanged",
                    "session_id": sid,
                }

            conn.execute(
                """
                INSERT INTO session_logs (
                    session_id, owner_id, source_label, source_path, source_channel, conversation_id,
                    participant_ids, participant_aliases, message_count,
                    topic_hint, transcript_text, content_hash, indexed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    source_label = excluded.source_label,
                    source_path = excluded.source_path,
                    source_channel = excluded.source_channel,
                    conversation_id = excluded.conversation_id,
                    participant_ids = excluded.participant_ids,
                    participant_aliases = excluded.participant_aliases,
                    message_count = excluded.message_count,
                    topic_hint = excluded.topic_hint,
                    transcript_text = excluded.transcript_text,
                    content_hash = excluded.content_hash,
                    updated_at = excluded.updated_at
                """,
                (
                    sid,
                    owner,
                    str(source_label or "unknown"),
                    source_path,
                    str(source_channel or "").strip() or None,
                    str(conversation_id or "").strip() or None,
                    json.dumps(participant_ids or [], ensure_ascii=True),
                    json.dumps(participant_aliases or {}, ensure_ascii=True),
                    msg_count,
                    hint,
                    transcript_text,
                    content_hash,
                    now,
                    now,
                ),
            )

        return {
            "status": "indexed",
            "session_id": sid,
            "message_count": msg_count,
        }
    finally:
        try:
            os.close(lock_fd)
        except Exception:
            pass
        try:
            os.unlink(lock_path)
        except Exception:
            pass


def list_recent_sessions(limit: int = 5, owner_id: Optional[str] = None) -> List[Dict[str, Any]]:
    lim = max(1, min(int(limit or 5), 50))
    with _lib_get_connection() as conn:
        ensure_schema(conn)
        if owner_id:
            rows = conn.execute(
                """
                SELECT session_id, owner_id, source_label, source_path, message_count, topic_hint, indexed_at, updated_at
                       , source_channel, conversation_id, participant_ids, participant_aliases
                FROM session_logs WHERE owner_id = ?
                ORDER BY updated_at DESC LIMIT ?
                """,
                (str(owner_id), lim),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT session_id, owner_id, source_label, source_path, message_count, topic_hint, indexed_at, updated_at
                       , source_channel, conversation_id, participant_ids, participant_aliases
                FROM session_logs ORDER BY updated_at DESC LIMIT ?
                """,
                (lim,),
            ).fetchall()
    return [dict(r) for r in rows]


def load_session(session_id: str, owner_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    sid = _normalize_session_id(session_id)
    with _lib_get_connection() as conn:
        ensure_schema(conn)
        if owner_id:
            row = conn.execute(
                """
                SELECT session_id, owner_id, source_label, source_path, message_count, topic_hint, indexed_at, updated_at, transcript_text
                       , source_channel, conversation_id, participant_ids, participant_aliases
                FROM session_logs WHERE session_id = ? AND owner_id = ?
                """,
                (sid, str(owner_id)),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT session_id, owner_id, source_label, source_path, message_count, topic_hint, indexed_at, updated_at, transcript_text
                       , source_channel, conversation_id, participant_ids, participant_aliases
                FROM session_logs WHERE session_id = ?
                """,
                (sid,),
            ).fetchone()
    return dict(row) if row else None


def _main() -> int:
    parser = argparse.ArgumentParser(description="Session log datastore")
    sub = parser.add_subparsers(dest="command")

    ingest_p = sub.add_parser("ingest", help="Index a session transcript")
    ingest_p.add_argument("--session-id", required=True)
    ingest_p.add_argument("--owner", default="default")
    ingest_p.add_argument("--label", default="unknown")
    ingest_p.add_argument("--source-path", default=None)
    ingest_p.add_argument("--source-channel", default=None)
    ingest_p.add_argument("--conversation-id", default=None)
    ingest_p.add_argument("--participant-ids", default=None, help="Comma-separated participant IDs/handles")
    ingest_p.add_argument("--participant-aliases", default=None, help="JSON object mapping alias -> canonical ID")
    ingest_p.add_argument("--message-count", type=int, default=0)
    ingest_p.add_argument("--topic-hint", default="")
    ingest_p.add_argument("--transcript-file", required=True)

    list_p = sub.add_parser("list", help="List recent indexed sessions")
    list_p.add_argument("--owner", default=None)
    list_p.add_argument("--limit", type=int, default=5)
    list_p.add_argument("--json", action="store_true", help="Emit JSON output (default)")

    load_p = sub.add_parser("load", help="Load one indexed session transcript")
    load_p.add_argument("--session-id", required=True)
    load_p.add_argument("--owner", default=None)
    load_p.add_argument("--json", action="store_true", help="Emit JSON output (default)")

    args = parser.parse_args()

    if args.command == "ingest":
        transcript = Path(args.transcript_file).read_text(encoding="utf-8")
        out = index_session_log(
            session_id=args.session_id,
            transcript=transcript,
            owner_id=args.owner,
            source_label=args.label,
            source_path=args.source_path,
            source_channel=args.source_channel,
            conversation_id=args.conversation_id,
            participant_ids=[p.strip() for p in str(args.participant_ids or "").split(",") if p.strip()],
            participant_aliases=json.loads(args.participant_aliases) if args.participant_aliases else None,
            message_count=args.message_count,
            topic_hint=args.topic_hint,
        )
        print(json.dumps(out))
        return 0

    if args.command == "list":
        print(json.dumps({"sessions": list_recent_sessions(limit=args.limit, owner_id=args.owner)}))
        return 0

    if args.command == "load":
        print(json.dumps({"session": load_session(args.session_id, owner_id=args.owner)}))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
