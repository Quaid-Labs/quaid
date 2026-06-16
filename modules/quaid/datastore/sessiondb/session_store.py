"""Session datastore for transcript chains and microchunk evidence.

SessionDB owns conversational session structure: sessions, coarse transcript
chunks, user/assistant pairs, and small microchunks tied to pair ids. MemoryDB
may project those microchunks for vector recall, but the session chain remains
owned here.
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
from typing import Any, Dict, List, Optional, Tuple

from lib.config import get_session_db_path
from lib.database import get_connection
from lib.fail_policy import is_fail_hard_enabled
from lib.session_text import DEFAULT_MICROCHUNK_TOKENS, parse_message_pairs, split_microchunks
from lib.tokens import estimate_tokens

logger = logging.getLogger(__name__)

# MemoryDB call sites may request a narrow pair radius; split source chunks need
# a few same-pair microchunks so local continuations survive pair-level truncation.
_MICROCHUNK_EXPANSION_RADIUS_FLOOR = 3


def _utcnow_iso() -> str:
    raw = os.environ.get("QUAID_NOW", "").strip()
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            else:
                parsed = parsed.astimezone(timezone.utc)
            return parsed.isoformat()
        except ValueError as exc:
            if is_fail_hard_enabled():
                raise RuntimeError(f"Invalid QUAID_NOW={raw!r}") from exc
            logger.warning("Invalid QUAID_NOW=%r; using wall clock", raw)
    return datetime.now(timezone.utc).isoformat()


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _owner(value: Any) -> str:
    return _clean(value) or "default"


def _session_id(value: Any) -> str:
    sid = _clean(value)
    if not sid:
        raise ValueError("session_id is required")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,160}", sid):
        raise ValueError("invalid session_id")
    return sid


def _hash_id(prefix: str, *parts: Any, length: int = 24) -> str:
    payload = "\x1f".join(str(part or "") for part in parts)
    return f"{prefix}_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:length]}"


def _content_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").strip().encode("utf-8")).hexdigest()


def _source_date(value: Any) -> Optional[str]:
    raw = _clean(value)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except ValueError as exc:
        match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", raw)
        if match:
            return match.group(1)
        logger.warning("failed to parse session source date %r", raw)
        if is_fail_hard_enabled():
            raise RuntimeError(f"failed to parse session source date {raw!r}") from exc
        return None


def _json_list(value: Any) -> str:
    if value is None:
        items: List[str] = []
    elif isinstance(value, str):
        items = [part.strip() for part in value.split(",")]
    else:
        items = [str(part).strip() for part in list(value or [])]
    return json.dumps([part for part in items if part], ensure_ascii=True)


def _json_dict(value: Any) -> str:
    if not isinstance(value, dict):
        return "{}"
    out: Dict[str, str] = {}
    for raw_key, raw_val in value.items():
        key = _clean(raw_key)
        val = _clean(raw_val)
        if key and val:
            out[key] = val
    return json.dumps(out, ensure_ascii=True, sort_keys=True)


def _session_db_path() -> Path:
    path = get_session_db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return path


def _lock_path(session_id: str) -> str:
    return f"{_session_db_path()}.session-{session_id}.lock"


def _stale_lock(path: str) -> bool:
    try:
        pid = int(Path(path).read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return False
    except PermissionError as exc:
        logger.warning("sessiondb lock file %s pid %s is not signalable; treating as live: %s", path, pid, exc)
        return False
    except FileNotFoundError:
        return True
    except ValueError as exc:
        logger.warning("sessiondb lock file %s contains invalid pid: %s", path, exc)
        return True
    except ProcessLookupError:
        return True


def _with_session_lock(session_id: str) -> Tuple[int, str]:
    path = _lock_path(session_id)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    last_err: Optional[Exception] = None
    for attempt in range(300):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.write(fd, str(os.getpid()).encode("utf-8"))
            return fd, path
        except FileExistsError as exc:
            last_err = exc
            if _stale_lock(path):
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    logger.debug("sessiondb stale lock already removed for session_id=%s: %s", session_id, path)
                continue
            if attempt == 299:
                raise RuntimeError(f"failed to acquire sessiondb lock for {session_id}: {last_err}")
            time.sleep(0.2)
    raise RuntimeError(f"failed to acquire sessiondb lock for {session_id}: {last_err}")


def ensure_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            source_label TEXT,
            source_path TEXT,
            source_channel TEXT,
            conversation_id TEXT,
            participant_ids TEXT DEFAULT '[]',
            participant_aliases TEXT DEFAULT '{}',
            message_count INTEGER DEFAULT 0,
            topic_hint TEXT,
            transcript_text TEXT,
            content_hash TEXT,
            indexed_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(owner_id, session_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_chunks (
            chunk_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            source_id TEXT,
            chunk_index INTEGER NOT NULL DEFAULT 0,
            chunk_kind TEXT DEFAULT 'session',
            source_channel TEXT,
            source_conversation_id TEXT,
            conversation_id TEXT,
            source_author_id TEXT,
            source_date TEXT,
            content_hash TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    existing_chunk_cols = {
        row["name"] if hasattr(row, "keys") and "name" in row.keys() else row[1]
        for row in conn.execute("PRAGMA table_info(session_chunks)").fetchall()
    }
    if "source_date" not in existing_chunk_cols:
        conn.execute("ALTER TABLE session_chunks ADD COLUMN source_date TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_pairs (
            pair_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            chunk_id TEXT NOT NULL,
            pair_index INTEGER NOT NULL DEFAULT 0,
            next_pair_id TEXT,
            user_text TEXT,
            assistant_text TEXT,
            text TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_microchunks (
            microchunk_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            chunk_id TEXT NOT NULL,
            pair_id TEXT NOT NULL,
            microchunk_index INTEGER NOT NULL DEFAULT 0,
            text TEXT NOT NULL,
            token_count INTEGER DEFAULT 0,
            content_hash TEXT NOT NULL,
            memory_chunk_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_lifecycle_observations (
            event_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            event_name TEXT NOT NULL,
            idempotency_key TEXT,
            source TEXT,
            reason TEXT,
            observed_at TEXT NOT NULL,
            metadata_json TEXT DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """
    )
    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_session_chunks_session ON session_chunks(owner_id, session_id, chunk_index)",
        "CREATE INDEX IF NOT EXISTS idx_session_pairs_session ON session_pairs(owner_id, session_id, pair_index)",
        "CREATE INDEX IF NOT EXISTS idx_session_pairs_next ON session_pairs(owner_id, session_id, next_pair_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_session_pairs_single_prev ON session_pairs(owner_id, session_id, next_pair_id) WHERE next_pair_id IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_session_microchunks_pair ON session_microchunks(owner_id, pair_id, microchunk_index)",
        "CREATE INDEX IF NOT EXISTS idx_session_microchunks_session ON session_microchunks(owner_id, session_id, chunk_id, microchunk_index)",
        "CREATE INDEX IF NOT EXISTS idx_session_microchunks_memory ON session_microchunks(owner_id, memory_chunk_id)",
        "CREATE INDEX IF NOT EXISTS idx_session_lifecycle_session ON session_lifecycle_observations(owner_id, session_id, observed_at)",
    ):
        conn.execute(stmt)


def _row_dict(row: Any) -> Dict[str, Any]:
    return dict(row) if row is not None else {}


def _session_summary(row: Any) -> Dict[str, Any]:
    data = _row_dict(row)
    if not data:
        return {}
    for key, fallback in [("participant_ids", []), ("participant_aliases", {})]:
        try:
            data[key] = json.loads(data.get(key) or json.dumps(fallback))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            if is_fail_hard_enabled():
                raise RuntimeError(f"sessiondb row has invalid JSON in {key}") from exc
            logger.warning("sessiondb row has invalid JSON in %s; using %r: %s", key, fallback, exc)
            data[key] = fallback
    return data


def record_lifecycle_observation(
    event: Dict[str, Any],
    *,
    owner_id: str = "default",
    session_id: str,
) -> Dict[str, Any]:
    sid = _session_id(session_id)
    owner = _owner(owner_id)
    event_id = _clean(event.get("id") or event.get("event_id") or event.get("idempotency_key"))
    if not event_id:
        raise ValueError("lifecycle observation event_id is required")
    event_name = _clean(event.get("name") or event.get("event_type"))
    if not event_name:
        raise ValueError("lifecycle observation event name is required")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    reason = _clean(payload.get("reason") or event.get("reason")) or None
    observed_at = _clean(event.get("created_at")) or _utcnow_iso()
    metadata = {
        "payload": payload,
        "provenance": event.get("provenance") if isinstance(event.get("provenance"), dict) else {},
        "instance_id": _clean(event.get("instance_id")) or None,
        "project_id": _clean(event.get("project_id")) or None,
    }
    now = _utcnow_iso()
    with get_connection(_session_db_path()) as conn:
        ensure_schema(conn)
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO session_lifecycle_observations (
                event_id, owner_id, session_id, event_name, idempotency_key,
                source, reason, observed_at, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                owner,
                sid,
                event_name,
                _clean(event.get("idempotency_key")) or None,
                _clean(event.get("source")) or None,
                reason,
                observed_at,
                json.dumps(metadata, ensure_ascii=True, sort_keys=True),
                now,
            ),
        )
    return {
        "status": "recorded",
        "datastore_id": "sessiondb",
        "persisted": True,
        "inserted": cursor.rowcount > 0,
        "event_id": event_id,
        "session_id": sid,
        "owner_id": owner,
    }


def list_lifecycle_observations(
    *,
    owner_id: str = "default",
    session_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    owner = _owner(owner_id)
    sid = _clean(session_id)
    with get_connection(_session_db_path()) as conn:
        ensure_schema(conn)
        if sid:
            rows = conn.execute(
                """
                SELECT * FROM session_lifecycle_observations
                WHERE owner_id = ? AND session_id = ?
                ORDER BY observed_at ASC, event_id ASC
                """,
                (owner, sid),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM session_lifecycle_observations
                WHERE owner_id = ?
                ORDER BY observed_at ASC, event_id ASC
                """,
                (owner,),
            ).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            if is_fail_hard_enabled():
                raise RuntimeError("sessiondb lifecycle observation has invalid metadata_json") from exc
            logger.warning("sessiondb lifecycle observation has invalid metadata_json; using {}: %s", exc)
            item["metadata"] = {}
        out.append(item)
    return out


def _pair_text(pair: Dict[str, str]) -> str:
    user = _clean(pair.get("user_text"))
    assistant = _clean(pair.get("assistant_text"))
    parts = []
    if user:
        parts.append(f"User: {user}")
    if assistant:
        parts.append(f"Assistant: {assistant}")
    return "\n".join(parts) or user or assistant


def _fetch_microchunks(conn, owner_id: str, ids: List[str]) -> List[Dict[str, Any]]:
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT * FROM session_microchunks
        WHERE owner_id = ? AND microchunk_id IN ({placeholders})
        ORDER BY microchunk_index ASC, microchunk_id ASC
        """,
        [owner_id, *ids],
    ).fetchall()
    by_id = {str(row["microchunk_id"]): dict(row) for row in rows}
    return [by_id[mid] for mid in ids if mid in by_id]


def _latest_tail_pair(conn, *, owner_id: str, session_id: str, exclude_pair_ids: List[str]) -> Optional[str]:
    params: List[Any] = [owner_id, session_id]
    exclude = ""
    if exclude_pair_ids:
        exclude = "AND pair_id NOT IN (" + ",".join("?" for _ in exclude_pair_ids) + ")"
        params.extend(exclude_pair_ids)
    rows = conn.execute(
        f"""
        SELECT pair_id FROM session_pairs
        WHERE owner_id = ? AND session_id = ? AND next_pair_id IS NULL {exclude}
        ORDER BY pair_index DESC, created_at DESC, pair_id DESC
        """,
        params,
    ).fetchall()
    if len(rows) > 1:
        raise RuntimeError(
            f"session pair chain has multiple tails for session_id={session_id}; "
            "this is a hard SessionDB invariant and cannot be repaired safely in-line"
        )
    return str(rows[0]["pair_id"]) if rows else None


def store_session_source_text(
    *,
    text: str,
    session_id: str,
    owner_id: str = "default",
    source_id: Optional[str] = None,
    chunk_index: int = 0,
    chunk_kind: str = "session",
    source_channel: Optional[str] = None,
    source_conversation_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    source_author_id: Optional[str] = None,
    source_date: Optional[str] = None,
    session_message_count: Optional[int] = None,
    max_microchunk_tokens: int = DEFAULT_MICROCHUNK_TOKENS,
) -> Dict[str, Any]:
    sid = _session_id(session_id)
    owner = _owner(owner_id)
    body = _clean(text)
    if not body:
        return {"status": "skipped", "reason": "empty_text", "session_id": sid, "microchunks": []}
    try:
        index = max(0, int(chunk_index or 0))
    except Exception as exc:
        raise ValueError(f"chunk_index must be an integer, got {chunk_index!r}") from exc
    source_key = _clean(source_id) or sid
    source_day = _source_date(source_date)
    content = _content_hash(body)
    chunk_id = _hash_id("schunk", owner, sid, source_key, index, content)
    now = _utcnow_iso()
    pairs = parse_message_pairs(body)
    msg_count = int(session_message_count) if session_message_count is not None else len(pairs)

    lock_fd, lock_file = _with_session_lock(sid)
    try:
        with get_connection(_session_db_path()) as conn:
            ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO sessions (
                    session_id, owner_id, source_channel, conversation_id,
                    message_count, topic_hint, transcript_text, content_hash, indexed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_id, session_id) DO UPDATE SET
                    source_channel = COALESCE(excluded.source_channel, sessions.source_channel),
                    conversation_id = COALESCE(excluded.conversation_id, sessions.conversation_id),
                    message_count = MAX(sessions.message_count, excluded.message_count),
                    topic_hint = COALESCE(NULLIF(excluded.topic_hint, ''), sessions.topic_hint),
                    updated_at = excluded.updated_at
                """,
                (
                    sid,
                    owner,
                    _clean(source_channel) or None,
                    _clean(conversation_id or source_conversation_id) or None,
                    msg_count,
                    pairs[0]["user_text"][:140] if pairs else "",
                    body,
                    content,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO session_chunks (
                    chunk_id, owner_id, session_id, source_id, chunk_index, chunk_kind,
                    source_channel, source_conversation_id, conversation_id, source_author_id,
                    source_date, content_hash, text, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chunk_id) DO UPDATE SET
                    source_date = COALESCE(session_chunks.source_date, excluded.source_date),
                    updated_at = excluded.updated_at
                """,
                (
                    chunk_id,
                    owner,
                    sid,
                    source_key,
                    index,
                    _clean(chunk_kind) or "session",
                    _clean(source_channel) or None,
                    _clean(source_conversation_id) or None,
                    _clean(conversation_id or source_conversation_id) or None,
                    _clean(source_author_id) or None,
                    source_day,
                    content,
                    body,
                    now,
                    now,
                ),
            )

            pair_ids: List[str] = []
            pair_rows: List[Dict[str, Any]] = []
            pair_payloads: List[Tuple[str, Dict[str, str], str]] = []
            for pair_offset, pair in enumerate(pairs):
                pair_body = _pair_text(pair)
                pair_id = _hash_id("pair", owner, sid, chunk_id, pair_offset, _content_hash(pair_body))
                pair_ids.append(pair_id)
                pair_payloads.append((pair_id, pair, pair_body))

            previous_tail = _latest_tail_pair(conn, owner_id=owner, session_id=sid, exclude_pair_ids=pair_ids)
            created_pair_ids: List[str] = []
            for pair_offset, (pair_id, pair, pair_body) in enumerate(pair_payloads):
                next_pair_id = pair_payloads[pair_offset + 1][0] if pair_offset + 1 < len(pair_payloads) else None
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO session_pairs (
                        pair_id, owner_id, session_id, chunk_id, pair_index, next_pair_id,
                        user_text, assistant_text, text, content_hash, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pair_id,
                        owner,
                        sid,
                        chunk_id,
                        index * 100000 + pair_offset,
                        next_pair_id,
                        _clean(pair.get("user_text")),
                        _clean(pair.get("assistant_text")),
                        pair_body,
                        _content_hash(pair_body),
                        now,
                        now,
                    ),
                )
                if cursor.rowcount > 0:
                    created_pair_ids.append(pair_id)
                elif next_pair_id:
                    conn.execute(
                        """
                        UPDATE session_pairs
                        SET next_pair_id = ?, updated_at = ?
                        WHERE owner_id = ? AND session_id = ? AND pair_id = ? AND next_pair_id IS NULL
                        """,
                        (next_pair_id, now, owner, sid, pair_id),
                    )
            if created_pair_ids and previous_tail:
                first_new = next(pair_id for pair_id in pair_ids if pair_id in created_pair_ids)
                conn.execute(
                    """
                    UPDATE session_pairs
                    SET next_pair_id = ?, updated_at = ?
                    WHERE owner_id = ? AND session_id = ? AND pair_id = ? AND next_pair_id IS NULL
                    """,
                    (first_new, now, owner, sid, previous_tail),
                )

            microchunk_ids: List[str] = []
            for pair_id, pair, pair_body in pair_payloads:
                for micro_index, micro_text in enumerate(split_microchunks(pair_body, max_microchunk_tokens)):
                    micro_id = _hash_id("micro", owner, sid, chunk_id, pair_id, micro_index, _content_hash(micro_text))
                    microchunk_ids.append(micro_id)
                    conn.execute(
                        """
                        INSERT INTO session_microchunks (
                            microchunk_id, owner_id, session_id, chunk_id, pair_id, microchunk_index,
                            text, token_count, content_hash, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(microchunk_id) DO UPDATE SET
                            updated_at = excluded.updated_at
                        """,
                        (
                            micro_id,
                            owner,
                            sid,
                            chunk_id,
                            pair_id,
                            micro_index,
                            micro_text,
                            int(estimate_tokens(micro_text)),
                            _content_hash(micro_text),
                            now,
                            now,
                        ),
                    )
            pair_rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM session_pairs WHERE owner_id = ? AND session_id = ? AND pair_id IN ({}) ORDER BY pair_index ASC".format(
                        ",".join("?" for _ in pair_ids)
                    ),
                    [owner, sid, *pair_ids] if pair_ids else [owner, sid],
                ).fetchall()
            ] if pair_ids else []
            micro_rows = _fetch_microchunks(conn, owner, microchunk_ids)
            chunk_row = dict(
                conn.execute(
                    "SELECT * FROM session_chunks WHERE chunk_id = ? AND owner_id = ?",
                    (chunk_id, owner),
                ).fetchone()
            )
        return {
            "status": "stored",
            "session_id": sid,
            "chunk": chunk_row,
            "pairs": pair_rows,
            "microchunks": micro_rows,
            "pairs_created": len(created_pair_ids),
            "microchunks_stored": len(micro_rows),
        }
    finally:
        try:
            os.close(lock_fd)
        except OSError as exc:
            logger.error("failed to close sessiondb lock fd for session_id=%s: %s", sid, exc)
        try:
            os.unlink(lock_file)
        except FileNotFoundError:
            logger.debug("sessiondb lock file already removed for session_id=%s: %s", sid, lock_file)
        except OSError as exc:
            logger.error("failed to remove sessiondb lock file %s for session_id=%s: %s", lock_file, sid, exc)


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
    sid = _session_id(session_id)
    owner = _owner(owner_id)
    transcript_text = _clean(transcript)
    if not transcript_text:
        return {"status": "skipped", "reason": "empty_transcript", "session_id": sid}
    now = _utcnow_iso()
    content = _content_hash(transcript_text)
    pairs = parse_message_pairs(transcript_text)
    msg_count = int(message_count) if message_count is not None else max(1, len(pairs))
    hint = _clean(topic_hint) or (pairs[0]["user_text"][:140] if pairs else "")

    with get_connection(_session_db_path()) as conn:
        ensure_schema(conn)
        prev = conn.execute(
            "SELECT content_hash FROM sessions WHERE owner_id = ? AND session_id = ?",
            (owner, sid),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO sessions (
                session_id, owner_id, source_label, source_path, source_channel, conversation_id,
                participant_ids, participant_aliases, message_count, topic_hint,
                transcript_text, content_hash, indexed_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(owner_id, session_id) DO UPDATE SET
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
                _clean(source_label) or "unknown",
                _clean(source_path) or None,
                _clean(source_channel) or None,
                _clean(conversation_id) or None,
                _json_list(participant_ids),
                _json_dict(participant_aliases),
                msg_count,
                hint,
                transcript_text,
                content,
                now,
                now,
            ),
        )
    chunk_result = store_session_source_text(
        text=transcript_text,
        session_id=sid,
        owner_id=owner,
        source_id=_clean(source_path) or sid,
        chunk_index=0,
        chunk_kind="session_log",
        source_channel=source_channel,
        source_conversation_id=conversation_id,
        conversation_id=conversation_id,
        session_message_count=msg_count,
        max_microchunk_tokens=DEFAULT_MICROCHUNK_TOKENS,
    )
    return {
        "status": "unchanged" if prev and str(prev["content_hash"]) == content else "indexed",
        "session_id": sid,
        "message_count": msg_count,
        "chunk": chunk_result.get("chunk") or {},
        "pairs": list(chunk_result.get("pairs") or []),
        "microchunks": list(chunk_result.get("microchunks") or []),
        "pairs_stored": len(chunk_result.get("pairs") or []),
        "microchunks_stored": len(chunk_result.get("microchunks") or []),
    }


def attach_memory_chunk(*, microchunk_id: str, memory_chunk_id: str, owner_id: str = "default") -> Dict[str, Any]:
    micro_id = _clean(microchunk_id)
    mem_id = _clean(memory_chunk_id)
    if not micro_id:
        raise ValueError("microchunk_id is required")
    if not mem_id:
        raise ValueError("memory_chunk_id is required")
    owner = _owner(owner_id)
    now = _utcnow_iso()
    with get_connection(_session_db_path()) as conn:
        ensure_schema(conn)
        cursor = conn.execute(
            """
            UPDATE session_microchunks
            SET memory_chunk_id = ?, updated_at = ?
            WHERE owner_id = ? AND microchunk_id = ?
            """,
            (mem_id, now, owner, micro_id),
        )
        if cursor.rowcount <= 0:
            raise RuntimeError(f"microchunk_id not found for memory link: {micro_id}")
    return {"status": "linked", "microchunk_id": micro_id, "memory_chunk_id": mem_id}


def fetch_microchunk(*, microchunk_id: str, owner_id: str = "default") -> Optional[Dict[str, Any]]:
    micro_id = _clean(microchunk_id)
    if not micro_id:
        raise ValueError("microchunk_id is required")
    owner = _owner(owner_id)
    with get_connection(_session_db_path()) as conn:
        ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM session_microchunks WHERE owner_id = ? AND microchunk_id = ?",
            (owner, micro_id),
        ).fetchone()
    return dict(row) if row else None


def fetch_pair(*, pair_id: str, owner_id: str = "default") -> Optional[Dict[str, Any]]:
    pid = _clean(pair_id)
    if not pid:
        raise ValueError("pair_id is required")
    owner = _owner(owner_id)
    with get_connection(_session_db_path()) as conn:
        ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM session_pairs WHERE owner_id = ? AND pair_id = ?",
            (owner, pid),
        ).fetchone()
    return dict(row) if row else None


def expand_microchunk(
    *,
    microchunk_id: str,
    owner_id: str = "default",
    before: int = 0,
    after: int = 0,
) -> Optional[Dict[str, Any]]:
    micro = fetch_microchunk(microchunk_id=microchunk_id, owner_id=owner_id)
    if not micro:
        return None
    pair = fetch_pair(pair_id=str(micro.get("pair_id") or ""), owner_id=owner_id)
    if not pair:
        return {"microchunk": micro, "pair": None, "window": [], "microchunk_window": []}
    owner = _owner(owner_id)
    window: List[Dict[str, Any]] = []
    before_count = max(0, min(int(before or 0), 20))
    after_count = max(0, min(int(after or 0), 20))
    with get_connection(_session_db_path()) as conn:
        ensure_schema(conn)
        source_chunk = conn.execute(
            "SELECT * FROM session_chunks WHERE owner_id = ? AND chunk_id = ?",
            (owner, str(micro.get("chunk_id") or "")),
        ).fetchone()
        micro_index = int(micro.get("microchunk_index") or 0)
        micro_before = max(before_count, _MICROCHUNK_EXPANSION_RADIUS_FLOOR)
        micro_after = max(after_count, _MICROCHUNK_EXPANSION_RADIUS_FLOOR)
        micro_rows = conn.execute(
            """
            SELECT * FROM session_microchunks
            WHERE owner_id = ?
              AND session_id = ?
              AND chunk_id = ?
              AND pair_id = ?
              AND microchunk_index BETWEEN ? AND ?
            ORDER BY microchunk_index ASC, microchunk_id ASC
            """,
            (
                owner,
                str(micro.get("session_id") or ""),
                str(micro.get("chunk_id") or ""),
                str(micro.get("pair_id") or ""),
                micro_index - micro_before,
                micro_index + micro_after,
            ),
        ).fetchall()
        current = str(pair.get("pair_id") or "")
        previous: List[Dict[str, Any]] = []
        for _ in range(before_count):
            row = conn.execute(
                """
                SELECT * FROM session_pairs
                WHERE owner_id = ? AND session_id = ? AND next_pair_id = ?
                LIMIT 1
                """,
                (owner, pair["session_id"], current),
            ).fetchone()
            if not row:
                break
            item = dict(row)
            previous.append(item)
            current = str(item.get("pair_id") or "")
        previous.reverse()
        following: List[Dict[str, Any]] = []
        next_id = str(pair.get("next_pair_id") or "").strip()
        for _ in range(after_count):
            if not next_id:
                break
            row = conn.execute(
                "SELECT * FROM session_pairs WHERE owner_id = ? AND pair_id = ?",
                (owner, next_id),
            ).fetchone()
            if not row:
                break
            item = dict(row)
            following.append(item)
            next_id = str(item.get("next_pair_id") or "").strip()
    window = [*previous, pair, *following]
    source_chunk_dict = dict(source_chunk) if source_chunk else {}
    source_day = source_chunk_dict.get("source_date")
    microchunk_window = [dict(row) for row in micro_rows]
    if source_day:
        micro = dict(micro)
        micro["source_date"] = source_day
        for item in window:
            item.setdefault("source_date", source_day)
        for item in microchunk_window:
            item.setdefault("source_date", source_day)
    source_window_header = None
    if source_day:
        session_id = str(micro.get("session_id") or "")
        source_window_header = {
            "header_id": f"{session_id}:{source_day}",
            "source_date": str(source_day),
            "session_id": session_id,
            "pair_id": str(micro.get("pair_id") or ""),
            "microchunk_id": str(micro.get("microchunk_id") or ""),
        }
    result = {
        "microchunk": micro,
        "pair": pair,
        "window": window,
        # MemoryDB recall expands split source chunks through this compact
        # first-order window instead of truncating the broader pair transcript.
        "microchunk_window": microchunk_window,
        "source_chunk": source_chunk_dict,
        "source_date": source_day,
    }
    if source_window_header is not None:
        result["source_window_header"] = source_window_header
    return result


def list_recent_sessions(limit: int = 5, owner_id: Optional[str] = None) -> List[Dict[str, Any]]:
    lim = max(1, min(int(limit or 5), 50))
    owner = _clean(owner_id)
    with get_connection(_session_db_path()) as conn:
        ensure_schema(conn)
        if owner:
            rows = conn.execute(
                """
                SELECT * FROM sessions
                WHERE owner_id = ?
                ORDER BY updated_at DESC LIMIT ?
                """,
                (owner, lim),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (lim,)).fetchall()
    return [_session_summary(row) for row in rows]


def load_session(session_id: str, owner_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    sid = _session_id(session_id)
    owner = _clean(owner_id)
    with get_connection(_session_db_path()) as conn:
        ensure_schema(conn)
        if owner:
            row = conn.execute(
                "SELECT * FROM sessions WHERE owner_id = ? AND session_id = ?",
                (owner, sid),
            ).fetchone()
        else:
            row = conn.execute("SELECT * FROM sessions WHERE session_id = ? LIMIT 1", (sid,)).fetchone()
    return _session_summary(row) if row else None


def _main() -> int:
    parser = argparse.ArgumentParser(description="SessionDB datastore")
    sub = parser.add_subparsers(dest="command")
    list_p = sub.add_parser("list", help="List recent sessions")
    list_p.add_argument("--owner", default=None)
    list_p.add_argument("--limit", type=int, default=5)
    load_p = sub.add_parser("load", help="Load one session")
    load_p.add_argument("--session-id", required=True)
    load_p.add_argument("--owner", default=None)
    args = parser.parse_args()
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
