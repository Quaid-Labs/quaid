"""MemoryDB-owned extraction fact/source publish helpers."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from config import get_config
from lib.domain_text import normalize_domain_id
from lib.fail_policy import is_fail_hard_enabled

logger = logging.getLogger(__name__)


FailHardEnabled = Callable[[], bool]

_SESSION_DATE_RE = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")
DEFAULT_EXTRACT_PUBLISH_BATCH_SIZE = 100
DEFAULT_SESSION_MICROCHUNK_TOKENS = 40


def _publish_date_for_session(session_id: str) -> Optional[str]:
    """Resolve a source date hint for MemoryDB publish metadata."""
    quaid_now = os.environ.get("QUAID_NOW", "").strip()
    if quaid_now:
        return quaid_now
    match = _SESSION_DATE_RE.search(str(session_id or ""))
    if match:
        return match.group(1)
    return None


def _normalize_extracted_timestamp(value: Any) -> Optional[str]:
    """Normalize extractor timestamps to stable ISO strings when possible."""
    raw = str(value or "").strip()
    if not raw:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return f"{raw}T23:59:59"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.isoformat(timespec="seconds")

def _current_utc_timestamp() -> str:
    quaid_now = os.environ.get("QUAID_NOW", "").strip()
    if quaid_now:
        normalized = _normalize_extracted_timestamp(quaid_now)
        if normalized:
            return normalized
        if is_fail_hard_enabled():
            raise RuntimeError(f"Invalid QUAID_NOW={quaid_now!r}")
        logger.warning("[datastore-memorydb] invalid QUAID_NOW=%r; falling back to wall clock", quaid_now)
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def _normalize_fact_temporal_hint(
    fact: Dict[str, Any],
    *,
    default_created_at: Optional[str] = None,
    default_mentioned_at: Optional[str] = None,
    prefer_default_mentioned_at: bool = False,
) -> Dict[str, Any]:
    """Normalize fact temporal metadata and backfill source mention time."""
    normalized = dict(fact or {})
    raw_created_at = str(normalized.get("created_at") or "").strip()
    source_created_at = _normalize_extracted_timestamp(raw_created_at)
    existing_source_timestamp = _normalize_extracted_timestamp(normalized.get("_source_timestamp"))
    fallback_source_at = _normalize_extracted_timestamp(default_created_at)
    fallback_mentioned_at = _normalize_extracted_timestamp(default_mentioned_at) or fallback_source_at
    if (
        source_created_at
        and fallback_source_at
        and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_created_at)
        and raw_created_at == fallback_source_at[:10]
    ):
        source_created_at = fallback_source_at
    source_timestamp = source_created_at or existing_source_timestamp or fallback_source_at
    # MemoryDB owns created_at as record/write time. Extracted source timestamps
    # are source mention time and must not override the datastore record axis.
    normalized.pop("created_at", None)
    if source_timestamp:
        normalized["_source_timestamp"] = source_timestamp
    else:
        normalized.pop("_source_timestamp", None)
    occurred_start = _normalize_extracted_timestamp(
        normalized.get("occurred_start") or normalized.get("occurred_at")
    )
    occurred_end = _normalize_extracted_timestamp(normalized.get("occurred_end"))
    if occurred_start:
        normalized["occurred_start"] = occurred_start
    else:
        normalized.pop("occurred_start", None)
    if occurred_end:
        normalized["occurred_end"] = occurred_end
    else:
        normalized.pop("occurred_end", None)
    extracted_mentioned_at = _normalize_extracted_timestamp(normalized.get("mentioned_at"))
    if prefer_default_mentioned_at:
        mentioned_at = fallback_mentioned_at or extracted_mentioned_at or source_timestamp
    else:
        mentioned_at = extracted_mentioned_at or source_timestamp or fallback_mentioned_at
    if mentioned_at:
        normalized["mentioned_at"] = mentioned_at
    else:
        normalized.pop("mentioned_at", None)
    normalized.pop("occurred_at", None)
    return normalized

def _collapse_duplicate_payload_facts(facts: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    from lib.extraction_fact_merge import collapse_duplicate_payload_facts as _collapse

    return _collapse(facts)

def _get_extract_publish_batch_size() -> int:
    raw = str(os.environ.get("QUAID_EXTRACT_PUBLISH_BATCH_SIZE", "") or "").strip()
    if not raw:
        return DEFAULT_EXTRACT_PUBLISH_BATCH_SIZE
    try:
        size = int(raw)
    except Exception as exc:
        logger.warning(
            "[datastore-memorydb] invalid QUAID_EXTRACT_PUBLISH_BATCH_SIZE=%r; defaulting to %d",
            raw,
            DEFAULT_EXTRACT_PUBLISH_BATCH_SIZE,
        )
        if is_fail_hard_enabled():
            raise RuntimeError(f"Invalid QUAID_EXTRACT_PUBLISH_BATCH_SIZE={raw!r}") from exc
        return DEFAULT_EXTRACT_PUBLISH_BATCH_SIZE
    return max(1, size)

def _publish_trace_enabled() -> bool:
    raw = str(os.environ.get("QUAID_PUBLISH_TRACE", "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _extract_publish_embedding_timeout_s(default: float = 30.0) -> float:
    raw = str(
        os.environ.get("QUAID_EXTRACT_PUBLISH_EMBED_TIMEOUT_S")
        or os.environ.get("QUAID_MEMORYDB_EMBED_TIMEOUT_S")
        or ""
    ).strip()
    if not raw:
        return float(default)
    try:
        value = float(raw)
    except Exception as exc:
        logger.warning(
            "[datastore-memorydb] invalid extraction publish embedding timeout %r; defaulting to %.1fs",
            raw,
            float(default),
        )
        if is_fail_hard_enabled():
            raise RuntimeError(f"Invalid extraction publish embedding timeout={raw!r}") from exc
        return float(default)
    if value <= 0:
        return float(default)
    return value


def _publish_trace_path() -> Optional[Path]:
    if not _publish_trace_enabled():
        return None
    instance = str(os.environ.get("QUAID_INSTANCE", "benchrunner") or "benchrunner").strip() or "benchrunner"
    from lib.adapter import get_adapter

    path = get_adapter().quaid_home() / "instances" / instance / "logs" / "daemon" / "publish-trace.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path

def _write_publish_trace(event: str, **data: Any) -> None:
    path = _publish_trace_path()
    if path is None:
        return
    payload = {
        "timestamp": _current_utc_timestamp(),
        "pid": os.getpid(),
        "event": event,
    }
    payload.update(data)
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=True) + "\n")
    except OSError as exc:
        logger.warning("[datastore-memorydb] extraction publish trace write failed: %s", exc)


def _content_hash(text: str) -> str:
    normalized = " ".join(str(text or "").lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _source_chunk_match_tokens(text: Any) -> set[str]:
    tokens: set[str] = set()
    for raw in " ".join(str(text or "").lower().split()).split(" "):
        token = raw.strip(".,;:!?()[]{}\"'`<>")
        if len(token) >= 3:
            tokens.add(token)
    return tokens


def _fact_source_match_text(fact: Dict[str, Any]) -> str:
    parts = [
        fact.get("text"),
        fact.get("content"),
        fact.get("keywords"),
        fact.get("subject_entity_name"),
        fact.get("object_entity_name"),
    ]
    return " ".join(str(part or "") for part in parts if part)


def _select_source_chunk_for_fact(
    fact: Dict[str, Any],
    chunks: List[Dict[str, Any]],
) -> Optional[str]:
    if not chunks:
        return None
    fact_tokens = _source_chunk_match_tokens(_fact_source_match_text(fact))
    if not fact_tokens:
        return str(chunks[0].get("chunk_id") or "").strip() or None
    best_chunk_id: Optional[str] = None
    best_key: Tuple[int, float, int] = (0, 0.0, 0)
    for idx, chunk in enumerate(chunks):
        chunk_id = str(chunk.get("chunk_id") or "").strip()
        if not chunk_id:
            continue
        chunk_tokens = _source_chunk_match_tokens(chunk.get("text"))
        overlap = len(fact_tokens & chunk_tokens)
        ratio = overlap / max(1, len(fact_tokens))
        key = (overlap, ratio, -idx)
        if best_chunk_id is None or key > best_key:
            best_chunk_id = chunk_id
            best_key = key
    if best_key[0] <= 0:
        return str(chunks[0].get("chunk_id") or "").strip() or None
    return best_chunk_id


def _store_payload_source_chunks(
    result: Dict[str, Any],
    *,
    owner_id: str,
    label: str,
    session_id: Optional[str],
    source_channel: Optional[str],
    source_conversation_id: Optional[str],
    source_author_id: Optional[str],
    session_bridge: Any,
    fail_hard_enabled: FailHardEnabled,
    log: logging.Logger,
    max_microchunk_tokens: int,
    embedding_timeout_s: float,
) -> Dict[str, List[Dict[str, Any]]]:
    """Materialize staged source chunk descriptors at publish time."""
    ref_to_chunks: Dict[str, List[Dict[str, Any]]] = {}
    descriptors = list(result.get("raw_source_chunks", []) or [])
    if not descriptors:
        return ref_to_chunks
    referenced_refs = {
        str(fact.get("_source_chunk_ref") or fact.get("source_chunk_ref") or "").strip()
        for fact in list(result.get("raw_facts", []) or [])
        if isinstance(fact, dict)
    }
    referenced_refs = {ref for ref in referenced_refs if ref}
    if not referenced_refs:
        return ref_to_chunks
    seen_refs: set[str] = set()
    next_chunk_index = 0
    session_chunk_offsets: Dict[Tuple[str, str], int] = {}
    for raw in descriptors:
        if not isinstance(raw, dict):
            continue
        ref = str(raw.get("source_chunk_ref") or raw.get("_source_chunk_ref") or "").strip()
        if not ref or ref not in referenced_refs or ref in seen_refs:
            continue
        seen_refs.add(ref)
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        chunk_session_id = str(raw.get("session_id") or session_id or "").strip() or None
        chunk_source_conversation_id = str(
            raw.get("source_conversation_id") or source_conversation_id or ""
        ).strip() or None
        source_key = str(raw.get("source_id") or chunk_session_id or chunk_source_conversation_id or "").strip()
        if not source_key:
            continue
        if not chunk_session_id:
            chunk_session_id = source_key
        owner_session_key = (str(owner_id or "").strip(), chunk_session_id)
        if owner_session_key not in session_chunk_offsets:
            try:
                existing = session_bridge.list_session_chunks(
                    owner_id=owner_id,
                    session_id=chunk_session_id,
                    order="desc",
                    limit=1,
                )
                latest_existing = next((row for row in existing if isinstance(row, dict)), None)
                max_existing_index = int(latest_existing.get("chunk_index", -1)) if latest_existing else -1
            except Exception as exc:
                log.warning(
                    "[extract] %s source chunk %s: failed to inspect existing session chunks: %s",
                    label,
                    ref,
                    exc,
                    exc_info=True,
                )
                if fail_hard_enabled():
                    raise RuntimeError("Failed to inspect existing session chunks") from exc
                max_existing_index = -1
            session_chunk_offsets[owner_session_key] = max_existing_index + 1
        next_chunk_index = session_chunk_offsets[owner_session_key]
        try:
            stored_rows = session_bridge.store_session_source_text(
                text=text,
                owner_id=owner_id,
                source_id=source_key,
                session_id=chunk_session_id,
                start_index=next_chunk_index,
                chunk_index=int(raw.get("chunk_index", next_chunk_index) or next_chunk_index),
                chunk_kind="micro",
                source_channel=raw.get("source_channel") or source_channel,
                source_conversation_id=chunk_source_conversation_id,
                conversation_id=raw.get("conversation_id") or chunk_source_conversation_id,
                source_author_id=raw.get("source_author_id") or source_author_id,
                max_microchunk_tokens=max_microchunk_tokens,
                embedding_timeout_s=embedding_timeout_s,
            )
        except Exception as exc:
            log.warning(
                "[extract] %s source chunk %s: failed to store source chunk: %s",
                label,
                ref,
                exc,
                exc_info=True,
            )
            if fail_hard_enabled():
                raise RuntimeError("Failed to store extraction source chunk") from exc
            result["source_chunks_failed"] = int(result.get("source_chunks_failed", 0) or 0) + 1
            continue
        stored_chunks = [row for row in list(stored_rows or []) if isinstance(row, dict)]
        if not stored_chunks or any(not str(row.get("chunk_id") or "").strip() for row in stored_chunks):
            result["source_chunks_failed"] = int(result.get("source_chunks_failed", 0) or 0) + 1
            log.warning(
                "[extract] %s source chunk %s: store returned no chunk_id",
                label,
                ref,
            )
            if fail_hard_enabled():
                raise RuntimeError("Source chunk store returned no chunk_id")
            continue
        session_chunk_offsets[owner_session_key] = next_chunk_index + len(stored_chunks)
        ref_to_chunks[ref] = stored_chunks
        result["source_chunks_micro_split"] = int(result.get("source_chunks_micro_split", 0) or 0) + max(
            0,
            len(stored_chunks) - 1,
        )
        for stored in stored_chunks:
            status = str((stored or {}).get("status") or "").strip()
            if status == "existing":
                result["source_chunks_existing"] = int(result.get("source_chunks_existing", 0) or 0) + 1
            else:
                result["source_chunks_stored"] = int(result.get("source_chunks_stored", 0) or 0) + 1
    return ref_to_chunks


def _attach_materialized_source_chunk_ids(
    raw_facts: List[Any],
    ref_to_chunks: Dict[str, Any],
) -> List[Any]:
    if not ref_to_chunks:
        return raw_facts
    out: List[Any] = []
    for fact in raw_facts:
        if not isinstance(fact, dict):
            out.append(fact)
            continue
        item = dict(fact)
        if not str(item.get("_source_chunk_id") or item.get("source_chunk_id") or "").strip():
            ref = str(item.get("_source_chunk_ref") or item.get("source_chunk_ref") or "").strip()
            stored = ref_to_chunks.get(ref)
            if isinstance(stored, str):
                chunk_id = stored
            elif isinstance(stored, list):
                chunk_id = _select_source_chunk_for_fact(item, stored)
            else:
                chunk_id = None
            if chunk_id:
                item["_source_chunk_id"] = chunk_id
        out.append(item)
    return out


def _prewarm_payload_embeddings(
    facts: List[Dict[str, Any]],
    *,
    label: str,
    memory_service: Any,
    log: logging.Logger,
) -> Dict[str, int]:
    """Precompute embeddings for candidate facts before serial publish."""
    texts: List[str] = []
    for fact in facts or []:
        if not isinstance(fact, dict):
            continue
        text = str(fact.get("text", "") or "").strip()
        if not text or len(text.split()) < 3:
            continue
        texts.append(text)
    if not texts:
        return {
            "requested": 0,
            "unique": 0,
            "cache_hits": 0,
            "warmed": 0,
            "failed": 0,
            "skipped_empty": 0,
        }
    try:
        stats = memory_service.warm_embeddings(texts, timeout_s=_extract_publish_embedding_timeout_s())
        if isinstance(stats, dict):
            log.info(
                "[extract] %s: embedding prewarm requested=%d unique=%d hits=%d warmed=%d failed=%d",
                label,
                int(stats.get("requested", 0) or 0),
                int(stats.get("unique", 0) or 0),
                int(stats.get("cache_hits", 0) or 0),
                int(stats.get("warmed", 0) or 0),
                int(stats.get("failed", 0) or 0),
            )
            return stats
    except Exception:
        log.warning("[extract] %s: embedding prewarm failed", label, exc_info=True)
        raise
    return {
        "requested": 0,
        "unique": 0,
        "cache_hits": 0,
        "warmed": 0,
        "failed": 0,
        "skipped_empty": 0,
    }


def _prewarm_edge_entity_embeddings(
    facts: List[Dict[str, Any]],
    *,
    label: str,
    memory_service: Any,
    log: logging.Logger,
) -> Dict[str, int]:
    """Precompute embeddings for edge subject/object entity names before publish."""
    texts: List[str] = []
    for fact in facts or []:
        if not isinstance(fact, dict):
            continue
        edges = fact.get("edges", [])
        if not isinstance(edges, list):
            continue
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            for key in ("subject", "object"):
                text = str(edge.get(key, "") or "").strip()
                if text:
                    texts.append(text)
    if not texts:
        return {
            "requested": 0,
            "unique": 0,
            "cache_hits": 0,
            "warmed": 0,
            "failed": 0,
            "skipped_empty": 0,
        }
    try:
        stats = memory_service.warm_embeddings(texts, timeout_s=_extract_publish_embedding_timeout_s())
        if isinstance(stats, dict):
            log.info(
                "[extract] %s: edge entity embedding prewarm requested=%d unique=%d hits=%d warmed=%d failed=%d",
                label,
                int(stats.get("requested", 0) or 0),
                int(stats.get("unique", 0) or 0),
                int(stats.get("cache_hits", 0) or 0),
                int(stats.get("warmed", 0) or 0),
                int(stats.get("failed", 0) or 0),
            )
            return stats
    except Exception:
        log.warning("[extract] %s: edge entity embedding prewarm failed", label, exc_info=True)
        raise
    return {
        "requested": 0,
        "unique": 0,
        "cache_hits": 0,
        "warmed": 0,
        "failed": 0,
        "skipped_empty": 0,
    }


def _resolve_allowed_domains(*, fail_hard_enabled: FailHardEnabled, log: logging.Logger) -> set[str]:
    try:
        retrieval_cfg = get_config().retrieval
        domain_defs = getattr(retrieval_cfg, "domains", {}) or {}
        if not isinstance(domain_defs, dict):
            domain_defs = {}
    except Exception as exc:
        log.warning("[extract] failed to resolve MemoryDB extraction publish domains: %s", exc)
        if fail_hard_enabled():
            raise RuntimeError("Failed to resolve MemoryDB extraction publish domains") from exc
        return set()
    allowed = {str(k).strip() for k in domain_defs.keys() if str(k).strip()}
    if not allowed:
        log.warning("[extract] MemoryDB extraction publish has no active domains")
        if fail_hard_enabled():
            raise RuntimeError("No active domains are registered in retrieval config")
    return allowed


def _normalize_fact_provenance(
    fact: Dict[str, Any],
    *,
    label: str,
    fact_index: int,
    fail_hard_enabled: FailHardEnabled,
    log: logging.Logger,
) -> Tuple[str, str]:
    """Normalize speaker/source into canonical labels and enforce provenance presence."""
    raw_speaker = str(fact.get("speaker", "") or "").strip().lower()
    raw_source = str(fact.get("source", "") or "").strip().lower()
    if not raw_speaker and not raw_source:
        msg = (
            f"[extract] missing provenance (speaker/source) for fact index={fact_index} "
            f"in {label} extraction"
        )
        if fail_hard_enabled():
            raise RuntimeError(msg)
        log.warning("%s - defaulting to user", msg)
        raw_speaker = "user"
        raw_source = "user"
    if raw_speaker not in {"agent", "assistant", "user"}:
        raw_speaker = "agent" if raw_source in {"agent", "assistant"} else "user"
    if not raw_source:
        raw_source = raw_speaker
    speaker_label = "agent" if raw_speaker in {"agent", "assistant"} else "user"
    if raw_source in {"agent", "assistant"}:
        source_type = "assistant"
    elif raw_source == "subagent":
        source_type = "subagent"
    elif raw_source == "both":
        source_type = "both"
    elif raw_source == "tool":
        source_type = "tool"
    else:
        source_type = "user"
    return speaker_label, source_type


def run_extraction_publish_payload(
    result: Dict[str, Any],
    *,
    owner_id: str,
    label: str,
    session_id: Optional[str],
    actor_id: Optional[str],
    speaker_entity_id: Optional[str],
    subject_entity_id: Optional[str],
    source_channel: Optional[str],
    target_datastore: Optional[str],
    source_conversation_id: Optional[str],
    participant_entity_ids: Optional[List[str]],
    source_author_id: Optional[str],
    dry_run: bool,
    snippet_files: int,
    journal_files: int,
    project_log_projects: int,
    memory_service: Any,
    session_bridge: Any,
    fail_hard_enabled: FailHardEnabled,
    log: logging.Logger = logger,
) -> List[Dict[str, Any]]:
    """Publish extracted fact/edge/source evidence through MemoryDB ownership.

    Returns the normalized raw fact list for orchestration-layer synthesis while
    mutating result["facts"] with status/edge entries. Snippet, journal, and
    project-log counts are trace-only; those writes stay outside MemoryDB.
    """
    result.setdefault("source_chunks_stored", 0)
    result.setdefault("source_chunks_existing", 0)
    result.setdefault("source_chunks_failed", 0)
    result.setdefault("facts", [])
    result.setdefault("facts_stored", 0)
    result.setdefault("facts_skipped", 0)
    result.setdefault("facts_planned", 0)
    result.setdefault("edges_created", 0)
    raw_facts = list(result.get("raw_facts", []) or [])
    default_created_at = _publish_date_for_session(session_id or "")
    default_mentioned_at = default_created_at or _current_utc_timestamp()
    prefer_default_mentioned_at = bool(default_created_at)
    if not dry_run:
        ref_to_chunk_id = _store_payload_source_chunks(
            result,
            owner_id=owner_id,
            label=label,
            session_id=session_id,
            source_channel=source_channel,
            source_conversation_id=source_conversation_id,
            source_author_id=source_author_id,
            session_bridge=session_bridge,
            fail_hard_enabled=fail_hard_enabled,
            log=log,
            max_microchunk_tokens=DEFAULT_SESSION_MICROCHUNK_TOKENS,
            embedding_timeout_s=_extract_publish_embedding_timeout_s(),
        )
        raw_facts = _attach_materialized_source_chunk_ids(raw_facts, ref_to_chunk_id)
        result["raw_facts"] = raw_facts
    facts = [
        _normalize_fact_temporal_hint(
            fact,
            default_created_at=default_created_at,
            default_mentioned_at=default_mentioned_at,
            prefer_default_mentioned_at=prefer_default_mentioned_at,
        )
        if isinstance(fact, dict)
        else fact
        for fact in raw_facts
    ]
    facts, collapsed_duplicates = _collapse_duplicate_payload_facts(facts)
    allowed = _resolve_allowed_domains(fail_hard_enabled=fail_hard_enabled, log=log)

    chunks_total = int(result.get("chunks_total", 0) or 0)
    chunk_suffix = f" from {chunks_total} chunks" if chunks_total > 1 else ""
    log.info("[extract] %s: LLM returned %d candidate facts%s", label, len(facts), chunk_suffix)
    result.setdefault("dedup_hash_exact_hits", 0)
    result.setdefault("dedup_scanned_rows", 0)
    result.setdefault("dedup_gray_zone_rows", 0)
    result.setdefault("dedup_llm_checks", 0)
    result.setdefault("dedup_llm_same_hits", 0)
    result.setdefault("dedup_llm_different_hits", 0)
    result.setdefault("dedup_fallback_reject_hits", 0)
    result.setdefault("dedup_auto_reject_hits", 0)
    result.setdefault("dedup_vec_query_count", 0)
    result.setdefault("dedup_vec_candidates_returned", 0)
    result.setdefault("dedup_vec_candidate_limit", 0)
    result.setdefault("dedup_vec_limit_hits", 0)
    result.setdefault("dedup_fts_query_count", 0)
    result.setdefault("dedup_fts_candidates_returned", 0)
    result.setdefault("dedup_fts_candidate_limit", 0)
    result.setdefault("dedup_fts_limit_hits", 0)
    result.setdefault("dedup_fallback_scan_count", 0)
    result.setdefault("dedup_fallback_candidates_returned", 0)
    result.setdefault("dedup_token_prefilter_terms", 0)
    result.setdefault("dedup_token_prefilter_skips", 0)
    result["payload_duplicate_facts_collapsed"] = int(collapsed_duplicates)
    result.setdefault("embedding_cache_requested", 0)
    result.setdefault("embedding_cache_unique", 0)
    result.setdefault("embedding_cache_hits", 0)
    result.setdefault("embedding_cache_warmed", 0)
    result.setdefault("embedding_cache_failed", 0)
    result.setdefault("edge_embedding_cache_requested", 0)
    result.setdefault("edge_embedding_cache_unique", 0)
    result.setdefault("edge_embedding_cache_hits", 0)
    result.setdefault("edge_embedding_cache_warmed", 0)
    result.setdefault("edge_embedding_cache_failed", 0)
    _write_publish_trace(
        "publish_start",
        session_id=session_id,
        label=label,
        fact_count=len(facts),
        snippet_files=int(snippet_files),
        journal_files=int(journal_files),
        project_log_projects=int(project_log_projects),
        dry_run=bool(dry_run),
    )

    if collapsed_duplicates:
        log.info(
            "[extract] %s: collapsed %d exact duplicate fact(s) before publish",
            label,
            collapsed_duplicates,
        )

    if not dry_run and facts:
        warm_stats = _prewarm_payload_embeddings(facts, label=label, memory_service=memory_service, log=log)
        result["embedding_cache_requested"] = int(warm_stats.get("requested", 0) or 0)
        result["embedding_cache_unique"] = int(warm_stats.get("unique", 0) or 0)
        result["embedding_cache_hits"] = int(warm_stats.get("cache_hits", 0) or 0)
        result["embedding_cache_warmed"] = int(warm_stats.get("warmed", 0) or 0)
        result["embedding_cache_failed"] = int(warm_stats.get("failed", 0) or 0)
        edge_warm_stats = _prewarm_edge_entity_embeddings(facts, label=label, memory_service=memory_service, log=log)
        result["edge_embedding_cache_requested"] = int(edge_warm_stats.get("requested", 0) or 0)
        result["edge_embedding_cache_unique"] = int(edge_warm_stats.get("unique", 0) or 0)
        result["edge_embedding_cache_hits"] = int(edge_warm_stats.get("cache_hits", 0) or 0)
        result["edge_embedding_cache_warmed"] = int(edge_warm_stats.get("warmed", 0) or 0)
        result["edge_embedding_cache_failed"] = int(edge_warm_stats.get("failed", 0) or 0)
        _write_publish_trace(
            "publish_prewarm_done",
            session_id=session_id,
            label=label,
            requested=result["embedding_cache_requested"],
            unique=result["embedding_cache_unique"],
            cache_hits=result["embedding_cache_hits"],
            warmed=result["embedding_cache_warmed"],
            failed=result["embedding_cache_failed"],
            edge_requested=result["edge_embedding_cache_requested"],
            edge_unique=result["edge_embedding_cache_unique"],
            edge_cache_hits=result["edge_embedding_cache_hits"],
            edge_warmed=result["edge_embedding_cache_warmed"],
            edge_failed=result["edge_embedding_cache_failed"],
        )

    dedup_rowid_max = None
    if not dry_run and facts:
        try:
            with memory_service.batch_write() as snapshot_conn:
                row = snapshot_conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM nodes").fetchone()
                dedup_rowid_max = int(row[0] or 0) if row else 0
        except Exception as exc:
            log.warning("Failed snapshotting pre-publish dedup rowid for %s: %s", label, exc)
            if fail_hard_enabled():
                raise
            dedup_rowid_max = None
        _write_publish_trace(
            "publish_snapshot_rowid",
            session_id=session_id,
            label=label,
            dedup_rowid_max=dedup_rowid_max,
        )

    publish_batch_size = max(1, int(_get_extract_publish_batch_size() or 1))
    result["publish_batches"] = 0

    def _accumulate_dedup_meta(store_result: Optional[Dict[str, Any]]) -> None:
        dedup_meta = store_result.get("dedup_telemetry", {}) if isinstance(store_result, dict) else {}
        if isinstance(dedup_meta, dict):
            result["dedup_hash_exact_hits"] += int(dedup_meta.get("hash_exact_hits", 0) or 0)
            result["dedup_scanned_rows"] += int(dedup_meta.get("scanned_rows", 0) or 0)
            result["dedup_gray_zone_rows"] += int(dedup_meta.get("gray_zone_rows", 0) or 0)
            result["dedup_llm_checks"] += int(dedup_meta.get("llm_checks", 0) or 0)
            result["dedup_llm_same_hits"] += int(dedup_meta.get("llm_same_hits", 0) or 0)
            result["dedup_llm_different_hits"] += int(dedup_meta.get("llm_different_hits", 0) or 0)
            result["dedup_fallback_reject_hits"] += int(dedup_meta.get("fallback_reject_hits", 0) or 0)
            result["dedup_auto_reject_hits"] += int(dedup_meta.get("auto_reject_hits", 0) or 0)
            result["dedup_vec_query_count"] += int(dedup_meta.get("vec_query_count", 0) or 0)
            result["dedup_vec_candidates_returned"] += int(dedup_meta.get("vec_candidates_returned", 0) or 0)
            result["dedup_vec_candidate_limit"] = max(
                int(result.get("dedup_vec_candidate_limit", 0) or 0),
                int(dedup_meta.get("vec_candidate_limit", 0) or 0),
            )
            result["dedup_vec_limit_hits"] += int(dedup_meta.get("vec_limit_hits", 0) or 0)
            result["dedup_fts_query_count"] += int(dedup_meta.get("fts_query_count", 0) or 0)
            result["dedup_fts_candidates_returned"] += int(dedup_meta.get("fts_candidates_returned", 0) or 0)
            result["dedup_fts_candidate_limit"] = max(
                int(result.get("dedup_fts_candidate_limit", 0) or 0),
                int(dedup_meta.get("fts_candidate_limit", 0) or 0),
            )
            result["dedup_fts_limit_hits"] += int(dedup_meta.get("fts_limit_hits", 0) or 0)
            result["dedup_fallback_scan_count"] += int(dedup_meta.get("fallback_scan_count", 0) or 0)
            result["dedup_fallback_candidates_returned"] += int(
                dedup_meta.get("fallback_candidates_returned", 0) or 0
            )
            result["dedup_token_prefilter_terms"] += int(dedup_meta.get("token_prefilter_terms", 0) or 0)
            result["dedup_token_prefilter_skips"] += int(dedup_meta.get("token_prefilter_skips", 0) or 0)

    def _finalize_store_result(
        *,
        store_result: Dict[str, Any],
        fact: Dict[str, Any],
        fact_entry: Dict[str, Any],
        write_conn: Any,
    ) -> bool:
        _accumulate_dedup_meta(store_result)

        status = store_result.get("status")
        if status == "created":
            fact_entry["status"] = "stored"
            result["facts_stored"] += 1
        elif status == "duplicate":
            fact_entry["status"] = "duplicate"
            fact_entry["reason"] = store_result.get("existing_text", "")
            result["facts_skipped"] += 1
        elif status == "updated":
            fact_entry["status"] = "updated"
            result["facts_stored"] += 1
        elif status == "blocked":
            fact_entry["status"] = "blocked"
            result["facts_skipped"] += 1
            result["circuit_breaker"] = store_result.get("reason", "blocked")
            log.warning("[extract] circuit breaker tripped mid-extraction, aborting remaining facts")
            result["facts"].append(fact_entry)
            return True
        elif status == "not_found":
            fact_entry["status"] = "pending"
            return False
        else:
            fact_entry["status"] = "failed"
            result["facts_skipped"] += 1

        fact_id = store_result.get("id") if status in ("created", "updated") else None
        if fact_id and isinstance(fact.get("edges"), list):
            for edge in fact.get("edges", []):
                if not isinstance(edge, dict):
                    continue
                subj = edge.get("subject")
                rel = edge.get("relation")
                obj = edge.get("object")
                if subj and rel and obj:
                    try:
                        edge_result = memory_service.create_edge(
                            subject_name=subj,
                            relation=rel,
                            object_name=obj,
                            owner_id=owner_id,
                            source_fact_id=fact_id,
                            _conn=write_conn,
                        )
                        if edge_result.get("status") == "created":
                            result["edges_created"] += 1
                            fact_entry["edges"].append(f"{subj} --{rel}--> {obj}")
                    except Exception as e:
                        log.warning(
                            "[extract] %s: edge failed for %s --%s--> %s: %s",
                            label,
                            subj,
                            rel,
                            obj,
                            e,
                            exc_info=True,
                        )
                        if fail_hard_enabled():
                            raise
        return fact_entry["status"] == "blocked"

    def _begin_publish_batch_write(write_conn: Any, *, batch_index: int) -> None:
        execute = getattr(write_conn, "execute", None)
        if not callable(execute):
            return
        execute("BEGIN IMMEDIATE")
        _write_publish_trace(
            "publish_batch_lock_acquired",
            session_id=session_id,
            label=label,
            batch_index=batch_index,
        )

    def _snapshot_publish_batch_rowid_max(
        write_conn: Any,
        *,
        batch_index: int,
        external_rowid_seen: Optional[int],
    ) -> Optional[int]:
        if external_rowid_seen is None:
            return external_rowid_seen
        execute = getattr(write_conn, "execute", None)
        if not callable(execute):
            return external_rowid_seen
        row = execute("SELECT COALESCE(MAX(rowid), 0) FROM nodes").fetchone()
        current_max = int(row[0] or 0) if row else 0
        delta_rowid_max = external_rowid_seen
        if current_max > int(external_rowid_seen or 0):
            delta_rowid_max = current_max
        _write_publish_trace(
            "publish_batch_rowid_window",
            session_id=session_id,
            label=label,
            batch_index=batch_index,
            external_rowid_seen=external_rowid_seen,
            delta_rowid_max=delta_rowid_max,
        )
        return delta_rowid_max

    def _process_fact(
        fact: Dict[str, Any],
        write_conn: Any,
        *,
        batch_index: int,
        fact_index: int,
    ) -> bool:
        text = fact.get("text", "")
        text_preview = str(text or "")[:120]
        text_hash_preview = None
        if text:
            try:
                text_hash_preview = _content_hash(text)[:12]
            except Exception:
                text_hash_preview = None
        _write_publish_trace(
            "publish_fact_start",
            session_id=session_id,
            label=label,
            batch_index=batch_index,
            fact_index=fact_index,
            text_hash=text_hash_preview,
            text_preview=text_preview,
            word_count=len(str(text or "").split()),
        )
        if not text or len(text.strip().split()) < 3:
            result["facts_skipped"] += 1
            result["facts"].append({
                "text": text or "(empty)",
                "status": "skipped",
                "reason": "too short (need 3+ words)",
            })
            _write_publish_trace(
                "publish_fact_done",
                session_id=session_id,
                label=label,
                batch_index=batch_index,
                fact_index=fact_index,
                text_hash=text_hash_preview,
                status="skipped",
                reason="too short",
            )
            return False

        conf_str = fact.get("extraction_confidence", "medium")
        conf_num = {"high": 0.9, "medium": 0.6, "low": 0.3}.get(conf_str, 0.6)

        category = fact.get("category", "fact")
        privacy = fact.get("privacy", "shared")
        keywords = fact.get("keywords")
        if isinstance(keywords, list):
            keywords = " ".join(str(k) for k in keywords if k)
        domains = fact.get("domains")
        if isinstance(domains, str):
            domains = [domains]
        if not isinstance(domains, list):
            domains = []
        domains = [normalize_domain_id(d) for d in domains if str(d).strip()]
        domains = [d for d in domains if d]
        if not domains:
            log.warning(
                "[extract] skipped fact with missing required domains array (fact=%r)",
                text,
            )
            result["facts_skipped"] += 1
            result["facts"].append({
                "text": text,
                "status": "skipped",
                "reason": "missing required domains",
            })
            return False
        invalid_domains = [d for d in domains if allowed and d not in allowed]
        if invalid_domains:
            log.warning(
                "[extract] skipped fact with unsupported domains %s (allowed=%s, fact=%r)",
                invalid_domains,
                sorted(allowed),
                text,
            )
            result["facts_skipped"] += 1
            result["facts"].append({
                "text": text,
                "status": "skipped",
                "reason": f"unsupported domains: {invalid_domains}",
            })
            return False

        project = fact.get("project")
        subject_entity_name = " ".join(str(fact.get("subject_entity_name", "") or "").split()).strip() or None
        knowledge_type = "preference" if category == "preference" else "fact"
        source_label = str(fact.get("_source_label") or f"{label}-extraction")
        source_id_value = str(fact.get("_source_id") or session_id or "")
        source_chunk_id = str(fact.get("_source_chunk_id") or fact.get("source_chunk_id") or "").strip() or None
        speaker_label, source_type = _normalize_fact_provenance(
            fact,
            label=label,
            fact_index=fact_index,
            fail_hard_enabled=fail_hard_enabled,
            log=log,
        )

        fact_entry = {"text": text, "status": "pending", "edges": []}

        if not dry_run:
            store_started_at = time.time()
            _write_publish_trace(
                "publish_store_call_start",
                session_id=session_id,
                label=label,
                batch_index=batch_index,
                fact_index=fact_index,
                text_hash=text_hash_preview,
            )
            store_result = memory_service.store(
                text=text,
                category=category,
                verified=False,
                pinned=False,
                confidence=conf_num,
                extraction_confidence=conf_num,
                provenance_confidence=conf_num,
                privacy=privacy,
                source=source_label,
                source_id=source_id_value or session_id,
                source_chunk_id=source_chunk_id,
                owner_id=owner_id,
                session_id=session_id,
                knowledge_type=knowledge_type,
                keywords=keywords,
                source_type=source_type,
                speaker=speaker_label,
                domains=domains,
                project=project,
                actor_id=actor_id,
                speaker_entity_id=speaker_entity_id,
                subject_entity_id=subject_entity_id,
                subject_entity_name=subject_entity_name,
                source_channel=source_channel,
                target_datastore=target_datastore,
                source_conversation_id=source_conversation_id,
                conversation_id=source_conversation_id,
                participant_entity_ids=participant_entity_ids,
                source_author_id=source_author_id,
                occurred_start=fact.get("occurred_start"),
                occurred_end=fact.get("occurred_end"),
                mentioned_at=fact.get("mentioned_at"),
                structural_anchor_kind=fact.get("structural_anchor_kind"),
                _conn=write_conn,
                _dedup_rowid_max=dedup_rowid_max,
            )
            _write_publish_trace(
                "publish_store_call_done",
                session_id=session_id,
                label=label,
                batch_index=batch_index,
                fact_index=fact_index,
                text_hash=text_hash_preview,
                status=str(store_result.get("status", "")),
                wall_seconds=round(time.time() - store_started_at, 3),
            )
            if _finalize_store_result(
                store_result=store_result,
                fact=fact,
                fact_entry=fact_entry,
                write_conn=write_conn,
            ):
                _write_publish_trace(
                    "publish_fact_done",
                    session_id=session_id,
                    label=label,
                    batch_index=batch_index,
                    fact_index=fact_index,
                    text_hash=text_hash_preview,
                    status=str(fact_entry.get("status", "")),
                    blocked=True,
                )
                return True
        else:
            fact_entry["status"] = "would_store"
            result["facts_planned"] += 1

        result["facts"].append(fact_entry)
        _write_publish_trace(
            "publish_fact_done",
            session_id=session_id,
            label=label,
            batch_index=batch_index,
            fact_index=fact_index,
            text_hash=text_hash_preview,
            status=str(fact_entry.get("status", "")),
        )
        return fact_entry["status"] == "blocked"

    if dry_run:
        for fact_index, fact in enumerate(facts, start=1):
            if not isinstance(fact, dict):
                continue
            _process_fact(fact, None, batch_index=1, fact_index=fact_index)
    else:
        external_rowid_seen = dedup_rowid_max
        for offset in range(0, len(facts), publish_batch_size):
            batch = facts[offset:offset + publish_batch_size]
            batch_index = (offset // publish_batch_size) + 1
            delta_rowid_max = external_rowid_seen
            _write_publish_trace(
                "publish_batch_begin",
                session_id=session_id,
                label=label,
                batch_index=batch_index,
                batch_size=len(batch),
                external_rowid_seen=external_rowid_seen,
            )
            with memory_service.batch_write() as write_conn:
                _begin_publish_batch_write(write_conn, batch_index=batch_index)
                if external_rowid_seen is not None:
                    try:
                        delta_rowid_max = _snapshot_publish_batch_rowid_max(
                            write_conn,
                            batch_index=batch_index,
                            external_rowid_seen=external_rowid_seen,
                        )
                    except Exception as exc:
                        log.warning(
                            "Failed snapshotting publish batch rowid for %s batch %d: %s",
                            label,
                            batch_index,
                            exc,
                        )
                        if fail_hard_enabled():
                            raise
                        delta_rowid_max = external_rowid_seen
                _write_publish_trace(
                    "publish_batch_conn_opened",
                    session_id=session_id,
                    label=label,
                    batch_index=batch_index,
                    batch_size=len(batch),
                )
                result["publish_batches"] += 1
                should_abort = False
                for local_index, fact in enumerate(batch):
                    global_fact_index = offset + local_index + 1
                    if not isinstance(fact, dict):
                        continue
                    if (
                        external_rowid_seen is not None
                        and delta_rowid_max is not None
                        and int(delta_rowid_max or 0) > int(external_rowid_seen or 0)
                    ):
                        text = fact.get("text", "")
                        conf_str = fact.get("extraction_confidence", "medium")
                        conf_num = {"high": 0.9, "medium": 0.6, "low": 0.3}.get(conf_str, 0.6)
                        category = fact.get("category", "fact")
                        privacy = fact.get("privacy", "shared")
                        keywords = fact.get("keywords")
                        if isinstance(keywords, list):
                            keywords = " ".join(str(k) for k in keywords if k)
                        domains = fact.get("domains")
                        if isinstance(domains, str):
                            domains = [domains]
                        if not isinstance(domains, list):
                            domains = []
                        domains = [normalize_domain_id(d) for d in domains if str(d).strip()]
                        domains = [d for d in domains if d]
                        project = fact.get("project")
                        subject_entity_name = " ".join(str(fact.get("subject_entity_name", "") or "").split()).strip() or None
                        knowledge_type = "preference" if category == "preference" else "fact"
                        source_label = str(fact.get("_source_label") or f"{label}-extraction")
                        source_id_value = str(fact.get("_source_id") or session_id or "")
                        source_chunk_id = str(fact.get("_source_chunk_id") or fact.get("source_chunk_id") or "").strip() or None
                        speaker_label, source_type = _normalize_fact_provenance(
                            fact,
                            label=label,
                            fact_index=global_fact_index,
                            fail_hard_enabled=fail_hard_enabled,
                            log=log,
                        )
                        delta_entry = {"text": text, "status": "pending", "edges": []}
                        delta_result = memory_service.store(
                            text=text,
                            category=category,
                            verified=False,
                            pinned=False,
                            confidence=conf_num,
                            extraction_confidence=conf_num,
                            provenance_confidence=conf_num,
                            privacy=privacy,
                            source=source_label,
                            source_id=source_id_value or session_id,
                            source_chunk_id=source_chunk_id,
                            owner_id=owner_id,
                            session_id=session_id,
                            knowledge_type=knowledge_type,
                            keywords=keywords,
                            source_type=source_type,
                            speaker=speaker_label,
                            domains=domains,
                            project=project,
                            actor_id=actor_id,
                            speaker_entity_id=speaker_entity_id,
                            subject_entity_id=subject_entity_id,
                            subject_entity_name=subject_entity_name,
                            source_channel=source_channel,
                            target_datastore=target_datastore,
                            source_conversation_id=source_conversation_id,
                            conversation_id=source_conversation_id,
                            participant_entity_ids=participant_entity_ids,
                            source_author_id=source_author_id,
                            occurred_start=fact.get("occurred_start"),
                            occurred_end=fact.get("occurred_end"),
                            mentioned_at=fact.get("mentioned_at"),
                            structural_anchor_kind=fact.get("structural_anchor_kind"),
                            _conn=write_conn,
                            _dedup_rowid_min_exclusive=external_rowid_seen,
                            _dedup_rowid_max=delta_rowid_max,
                            _dedup_only=True,
                        )
                        _write_publish_trace(
                            "publish_delta_recheck_done",
                            session_id=session_id,
                            label=label,
                            batch_index=batch_index,
                            fact_index=global_fact_index,
                            status=str(delta_result.get("status", "")),
                            delta_rowid_min=external_rowid_seen,
                            delta_rowid_max=delta_rowid_max,
                        )
                        if _finalize_store_result(
                            store_result=delta_result,
                            fact=fact,
                            fact_entry=delta_entry,
                            write_conn=write_conn,
                        ):
                            should_abort = True
                            break
                        if delta_entry["status"] in {"duplicate", "updated", "blocked"}:
                            result["facts"].append(delta_entry)
                            continue
                    if _process_fact(
                        fact,
                        write_conn,
                        batch_index=batch_index,
                        fact_index=global_fact_index,
                    ):
                        should_abort = True
                        break
                _write_publish_trace(
                    "publish_batch_done",
                    session_id=session_id,
                    label=label,
                    batch_index=batch_index,
                    should_abort=bool(should_abort),
                    facts_stored=int(result.get("facts_stored", 0) or 0),
                    facts_skipped=int(result.get("facts_skipped", 0) or 0),
                )
            if should_abort:
                break
            if (
                external_rowid_seen is not None
                and delta_rowid_max is not None
                and int(delta_rowid_max or 0) > int(external_rowid_seen or 0)
            ):
                external_rowid_seen = int(delta_rowid_max or 0)

    _write_publish_trace(
        "publish_facts_complete",
        session_id=session_id,
        label=label,
        facts_stored=int(result.get("facts_stored", 0) or 0),
        facts_skipped=int(result.get("facts_skipped", 0) or 0),
        edges_created=int(result.get("edges_created", 0) or 0),
        publish_batches=int(result.get("publish_batches", 0) or 0),
    )
    return facts
