"""Datastore-owned session log indexing and retrieval.

Owns semantic/lexical indexing for session transcripts so adapter/core layers only
orchestrate and do not hold retrieval logic.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from lib.worker_pool import run_callables
from lib.database import get_connection as _lib_get_connection
from lib.embeddings import get_embedding as _lib_get_embedding, pack_embedding as _lib_pack_embedding, unpack_embedding as _lib_unpack_embedding
from lib.similarity import cosine_similarity as _lib_cosine_similarity

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


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _chunk_transcript(transcript: str, max_tokens: int = 900) -> List[str]:
    txt = str(transcript or "").strip()
    if not txt:
        return []
    turns = [t.strip() for t in txt.split("\n\n") if t.strip()]
    if not turns:
        return [txt]

    chunks: List[str] = []
    buf: List[str] = []
    tok = 0
    for turn in turns:
        t = _estimate_tokens(turn)
        if buf and tok + t > max_tokens:
            chunks.append("\n\n".join(buf).strip())
            buf = []
            tok = 0
        buf.append(turn)
        tok += t
    if buf:
        chunks.append("\n\n".join(buf).strip())
    return [c for c in chunks if c]


def _infer_topic_hint(transcript: str) -> str:
    for line in str(transcript or "").splitlines():
        line = line.strip()
        if line.lower().startswith("user:"):
            body = line[5:].strip()
            if body:
                return body[:140]
    return ""


def _parallel_workers(task_name: str, default: int = 4) -> int:
    try:
        from config import get_config

        cfg = get_config()
        parallel = getattr(getattr(cfg, "core", None), "parallel", None)
        if parallel is None or not getattr(parallel, "enabled", True):
            return 1
        workers = int(getattr(parallel, "llm_workers", default) or default)
        task_workers = getattr(parallel, "task_workers", {}) or {}
        override = None
        if isinstance(task_workers, dict):
            for key in (task_name, task_name.upper(), task_name.lower()):
                if key in task_workers:
                    override = task_workers.get(key)
                    break
        raw = override if override is not None else workers
        return max(1, min(int(raw), 16))
    except Exception as exc:
        logger.warning("session_logs parallel worker config parse failed for task=%s: %s", task_name, exc)
        return max(1, int(default))


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

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_log_chunks (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            embedding BLOB,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES session_logs(session_id) ON DELETE CASCADE,
            UNIQUE(session_id, chunk_index)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_session_log_chunks_session ON session_log_chunks(session_id)")


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
    chunks = _chunk_transcript(transcript_text)
    embedding_blobs: List[Optional[bytes]] = []
    if chunks:
        calls = [(lambda chunk_text: (lambda: _lib_get_embedding(chunk_text)))(chunk) for chunk in chunks]
        emb_results = run_callables(
            calls,
            max_workers=min(_parallel_workers("session_log_index"), len(chunks)),
            pool_name="session-log-index",
            return_exceptions=True,
        )
        for item in emb_results:
            if isinstance(item, Exception) or not item:
                embedding_blobs.append(None)
            else:
                try:
                    embedding_blobs.append(_lib_pack_embedding(item))
                except Exception:
                    embedding_blobs.append(None)

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
                "chunks": int(
                    conn.execute("SELECT COUNT(*) AS n FROM session_log_chunks WHERE session_id = ?", (sid,)).fetchone()["n"]
                ),
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

        conn.execute("DELETE FROM session_log_chunks WHERE session_id = ?", (sid,))
        created = 0
        for i, chunk in enumerate(chunks):
            embedding_blob = embedding_blobs[i] if i < len(embedding_blobs) else None
            conn.execute(
                """
                INSERT INTO session_log_chunks (id, session_id, chunk_index, content, embedding, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (f"{sid}:{i}", sid, i, chunk, embedding_blob, now, now),
            )
            created += 1

    return {
        "status": "indexed",
        "session_id": sid,
        "chunks": created,
        "message_count": msg_count,
    }


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


def load_last_session(owner_id: Optional[str] = None, exclude_session_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    exclude = str(exclude_session_id or "").strip()
    with _lib_get_connection() as conn:
        ensure_schema(conn)
        params: List[Any] = []
        where: List[str] = []
        if owner_id:
            where.append("owner_id = ?")
            params.append(str(owner_id))
        if exclude:
            where.append("session_id != ?")
            params.append(exclude)

        sql = (
            "SELECT session_id, owner_id, source_label, source_path, source_channel, conversation_id, participant_ids, participant_aliases, "
            "message_count, topic_hint, indexed_at, updated_at, transcript_text "
            "FROM session_logs "
        )
        if where:
            sql += "WHERE " + " AND ".join(where) + " "
        sql += "ORDER BY updated_at DESC LIMIT 1"
        row = conn.execute(sql, tuple(params)).fetchone()
    return dict(row) if row else None


def _lexical_score(query: str, text: str) -> float:
    q_tokens = [t for t in re.findall(r"[a-z0-9_]+", query.lower()) if len(t) >= 2]
    if not q_tokens:
        return 0.0
    hay = text.lower()
    hits = sum(1 for t in q_tokens if t in hay)
    return hits / len(q_tokens)


def search_session_logs(
    query: str,
    *,
    owner_id: Optional[str] = None,
    limit: int = 5,
    min_similarity: float = 0.15,
) -> List[Dict[str, Any]]:
    q = str(query or "").strip()
    if not q:
        return []

    lim = max(1, min(int(limit or 5), 25))
    q_emb = _lib_get_embedding(q)

    with _lib_get_connection() as conn:
        ensure_schema(conn)
        params: List[Any] = []
        if owner_id:
            rows = conn.execute(
                """
                SELECT c.session_id, c.chunk_index, c.content, c.embedding,
                       s.owner_id, s.source_label, s.message_count, s.topic_hint, s.updated_at
                FROM session_log_chunks c
                JOIN session_logs s ON s.session_id = c.session_id
                WHERE s.owner_id = ?
                """,
                (str(owner_id),),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT c.session_id, c.chunk_index, c.content, c.embedding,
                       s.owner_id, s.source_label, s.message_count, s.topic_hint, s.updated_at
                FROM session_log_chunks c
                JOIN session_logs s ON s.session_id = c.session_id
                """
            ).fetchall()

    def _score_row(row: Any) -> Optional[Dict[str, Any]]:
        content = str(row["content"] or "")
        sem = 0.0
        if q_emb and row["embedding"]:
            try:
                emb = _lib_unpack_embedding(row["embedding"])
                sem = _lib_cosine_similarity(q_emb, emb)
            except Exception:
                sem = 0.0
        lex = _lexical_score(q, content)
        score = max(sem, lex * 0.6)
        if score < float(min_similarity):
            return None
        return {
            "session_id": row["session_id"],
            "chunk_index": int(row["chunk_index"]),
            "text": content,
            "score": round(float(score), 4),
            "semantic": round(float(sem), 4),
            "lexical": round(float(lex), 4),
            "owner_id": row["owner_id"],
            "source_label": row["source_label"],
            "message_count": int(row["message_count"] or 0),
            "topic_hint": row["topic_hint"] or "",
            "updated_at": row["updated_at"],
        }

    results: List[Dict[str, Any]] = []
    worker_count = min(_parallel_workers("session_log_search"), len(rows))
    if worker_count > 1 and len(rows) >= 32:
        calls = [(lambda r: (lambda: _score_row(r)))(row) for row in rows]
        scored = run_callables(
            calls,
            max_workers=worker_count,
            pool_name="session-log-search",
            return_exceptions=True,
        )
        for item in scored:
            if isinstance(item, Exception) or item is None:
                continue
            results.append(item)
    else:
        for row in rows:
            scored = _score_row(row)
            if scored is not None:
                results.append(scored)

    results.sort(key=lambda x: (x["score"], x["updated_at"]), reverse=True)
    return results[:lim]


def _format_search_results(results: List[Dict[str, Any]]) -> str:
    if not results:
        return "No session log matches found."
    lines = [f"Found {len(results)} session log match(es):"]
    for i, r in enumerate(results, start=1):
        text = str(r.get("text") or "").replace("\n", " ").strip()
        if len(text) > 280:
            text = text[:277] + "..."
        lines.append(
            f"{i}. session={r.get('session_id')} chunk={r.get('chunk_index')} score={r.get('score'):.3f} "
            f"[{r.get('source_label') or 'unknown'}] {text}"
        )
    return "\n".join(lines)


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

    load_p = sub.add_parser("load", help="Load one indexed session transcript")
    load_p.add_argument("--session-id", required=True)
    load_p.add_argument("--owner", default=None)

    last_p = sub.add_parser("last", help="Load most recent indexed session transcript")
    last_p.add_argument("--owner", default=None)
    last_p.add_argument("--exclude-session-id", default=None)

    search_p = sub.add_parser("search", help="Search indexed session logs")
    search_p.add_argument("query")
    search_p.add_argument("--owner", default=None)
    search_p.add_argument("--limit", type=int, default=5)
    search_p.add_argument("--min-similarity", type=float, default=0.15)

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

    if args.command == "last":
        print(json.dumps({"session": load_last_session(owner_id=args.owner, exclude_session_id=args.exclude_session_id)}))
        return 0

    if args.command == "search":
        results = search_session_logs(args.query, owner_id=args.owner, limit=args.limit, min_similarity=args.min_similarity)
        print(json.dumps({"results": results, "summary": _format_search_results(results)}))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
