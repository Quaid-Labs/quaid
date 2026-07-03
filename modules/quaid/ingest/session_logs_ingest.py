#!/usr/bin/env python3
"""Session log ingest bridge.

Core/runtime emits lifecycle events; this module resolves transcript sources and
hands indexing/search/load operations to datastore-owned session log routines.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from core.services.session_memory_bridge import get_session_memory_bridge
from lib.fail_policy import is_fail_hard_enabled
from lib.runtime_context import get_adapter_instance

logger = logging.getLogger(__name__)


def _normalize_session_id(value: Any) -> str:
    sid = str(value or "").strip()
    if not sid:
        raise ValueError("session_id is required")
    return sid


def _normalize_participant_aliases(value: Any) -> Optional[Dict[str, str]]:
    """Validate participant alias mapping payload."""
    if value is None:
        return None
    if isinstance(value, str):
        payload = value.strip()
        if not payload:
            return None
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("participant_aliases must be a valid JSON object") from exc
    else:
        parsed = value
    if not isinstance(parsed, dict):
        raise ValueError("participant_aliases must be a JSON object")
    normalized: Dict[str, str] = {}
    for raw_key, raw_val in parsed.items():
        key = str(raw_key).strip()
        val = str(raw_val).strip()
        if key and val:
            normalized[key] = val
    return normalized or None


def _build_transcript_from_session_file(path: Path) -> str:
    return get_adapter_instance().parse_session_jsonl(path)


def _looks_like_session_jsonl(text: str) -> bool:
    seen_json_object = False
    seen_session_shape = False
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            if seen_session_shape:
                logger.warning(
                    "Malformed row in session JSONL transcript; routing through adapter parser",
                    exc_info=True,
                )
                return True
            continue
        if not isinstance(obj, dict):
            if seen_session_shape:
                logger.warning("Non-object row in session JSONL transcript; routing through adapter parser")
                return True
            continue
        seen_json_object = True
        keys = {str(key) for key in obj.keys()}
        if {"type", "message", "payload"} & keys or {"role", "content"} <= keys:
            seen_session_shape = True
        if "usage" in keys and {"stop_reason", "stop_sequence", "stop_details"} & keys:
            seen_session_shape = True
    return seen_json_object and seen_session_shape


def _read_transcript_path(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if _looks_like_session_jsonl(text):
        # Daemon lifecycle calls pass host JSONL here; SessionDB must store the
        # adapter-normalized conversational transcript, not raw platform records.
        return _build_transcript_from_session_file(path)
    return text


def _safe_read_transcript_path(path: Path, source_kind: str, *, session_jsonl: bool) -> Optional[str]:
    try:
        return _build_transcript_from_session_file(path) if session_jsonl else _read_transcript_path(path)
    except (OSError, UnicodeDecodeError):
        if is_fail_hard_enabled():
            raise
        logger.warning("Session transcript source could not be read during %s read: %s", source_kind, path)
        return None


def _reset_backup_for_missing_session_path(path: Path) -> Optional[Path]:
    """Find the newest OpenClaw /reset backup for a missing session JSONL.

    OpenClaw renames the native transcript to ``<session>.jsonl.reset.<ts>``
    during reset handling. When the canonical path is missing, that sibling is
    the authoritative completed transcript, so failHard should ingest it rather
    than treating the original path disappearance as data loss.
    """
    if path.name.endswith(".reset.") or ".jsonl.reset." in path.name:
        return None
    if not path.name.endswith(".jsonl"):
        return None
    prefix = f"{path.name}.reset."
    try:
        if not path.parent.is_dir():
            return None
        candidates = [
            candidate
            for candidate in path.parent.iterdir()
            if candidate.name.startswith(prefix) and candidate.is_file()
        ]
    except OSError as exc:
        logger.warning("Failed scanning reset transcript backups for %s: %s", path, exc)
        if is_fail_hard_enabled():
            raise
        return None
    if not candidates:
        return None
    return sorted(candidates, key=lambda candidate: candidate.name)[-1]


def _read_reset_backup_for_missing_path(path: Path, source_kind: str) -> Optional[tuple[Path, str, str]]:
    backup_path = _reset_backup_for_missing_session_path(path)
    if not backup_path:
        return None
    transcript = _safe_read_transcript_path(
        backup_path,
        f"{source_kind}_reset_backup",
        session_jsonl=True,
    )
    if not transcript or not transcript.strip():
        return None
    logger.info(
        "Session transcript source %s was renamed by reset; using backup: %s",
        path,
        backup_path,
    )
    return backup_path, transcript, f"{source_kind}_reset_backup"


def _resolve_transcript_source(
    *,
    session_id: str,
    session_file: Optional[str],
    transcript_path: Optional[str],
) -> tuple[Optional[Path], Optional[str], str]:
    if transcript_path:
        p = Path(str(transcript_path)).expanduser()
        if p.exists() and p.is_file():
            transcript = _safe_read_transcript_path(p, "transcript_path", session_jsonl=False)
            if transcript and transcript.strip():
                return p, transcript, "transcript_path"
        reset_backup = _read_reset_backup_for_missing_path(p, "transcript_path")
        if reset_backup:
            return reset_backup

    if session_file:
        p = Path(str(session_file)).expanduser()
        if p.exists() and p.is_file():
            transcript = _safe_read_transcript_path(p, "session_file", session_jsonl=True)
            if transcript and transcript.strip():
                return p, transcript, "session_file"
        reset_backup = _read_reset_backup_for_missing_path(p, "session_file")
        if reset_backup:
            return reset_backup

    session_path = get_adapter_instance().get_session_path(session_id)
    if session_path and session_path.exists() and session_path.is_file():
        transcript = _safe_read_transcript_path(session_path, "adapter_session_path", session_jsonl=True)
        if transcript and transcript.strip():
            return session_path, transcript, "adapter_session_path"
    if session_path:
        reset_backup = _read_reset_backup_for_missing_path(session_path, "adapter_session_path")
        if reset_backup:
            return reset_backup

    return None, None, "missing"


def _run(
    *,
    session_id: str,
    owner_id: str,
    label: str,
    session_file: Optional[str] = None,
    transcript_path: Optional[str] = None,
    source_channel: Optional[str] = None,
    conversation_id: Optional[str] = None,
    participant_ids: Optional[list[str]] = None,
    participant_aliases: Optional[Dict[str, str]] = None,
    message_count: int = 0,
    topic_hint: str = "",
) -> Dict[str, Any]:
    sid = _normalize_session_id(session_id)
    normalized_aliases = _normalize_participant_aliases(participant_aliases)
    src_path, transcript, source_kind = _resolve_transcript_source(
        session_id=sid,
        session_file=session_file,
        transcript_path=transcript_path,
    )
    if not transcript:
        if is_fail_hard_enabled():
            raise RuntimeError(f"session transcript unavailable under failHard: {sid} ({source_kind})")
        return {
            "status": "skipped",
            "reason": "transcript_unavailable",
            "session_id": sid,
            "source_kind": source_kind,
        }

    result = get_session_memory_bridge().store_session_transcript(
        session_id=sid,
        owner_id=str(owner_id or "default"),
        label=str(label or "unknown"),
        transcript=transcript,
        source_path=str(src_path) if src_path else None,
        source_channel=source_channel,
        conversation_id=conversation_id,
        participant_ids=participant_ids or [],
        participant_aliases=normalized_aliases or {},
        message_count=int(message_count or 0),
        topic_hint=topic_hint,
    )
    result["source_kind"] = source_kind
    return result


def run(
    *,
    session_id: str,
    owner_id: str,
    label: str,
    session_file: Optional[str] = None,
    transcript_path: Optional[str] = None,
    source_channel: Optional[str] = None,
    conversation_id: Optional[str] = None,
    participant_ids: Optional[list[str]] = None,
    participant_aliases: Optional[Dict[str, str]] = None,
    message_count: int = 0,
    topic_hint: str = "",
) -> Dict[str, Any]:
    """Public runtime entrypoint for session log ingest."""
    return _run(
        session_id=session_id,
        owner_id=owner_id,
        label=label,
        session_file=session_file,
        transcript_path=transcript_path,
        source_channel=source_channel,
        conversation_id=conversation_id,
        participant_ids=participant_ids,
        participant_aliases=participant_aliases,
        message_count=message_count,
        topic_hint=topic_hint,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Session log ingest and retrieval bridge")
    sub = parser.add_subparsers(dest="command")

    ingest_p = sub.add_parser("ingest", help="Ingest/index session log")
    ingest_p.add_argument("--session-id", required=True)
    ingest_p.add_argument("--owner", default="default")
    ingest_p.add_argument("--label", default="unknown")
    ingest_p.add_argument("--session-file", default=None)
    ingest_p.add_argument("--transcript-path", default=None)
    ingest_p.add_argument("--source-channel", default=None)
    ingest_p.add_argument("--conversation-id", default=None)
    ingest_p.add_argument("--participant-ids", default=None, help="Comma-separated participant IDs/handles")
    ingest_p.add_argument("--participant-aliases", default=None, help="JSON object mapping alias -> canonical ID")
    ingest_p.add_argument("--message-count", type=int, default=0)
    ingest_p.add_argument("--topic-hint", default="")

    list_p = sub.add_parser("list", help="List indexed sessions")
    list_p.add_argument("--owner", default=None)
    list_p.add_argument("--limit", type=int, default=5)
    list_p.add_argument("--json", action="store_true", help="Emit JSON output (default)")

    load_p = sub.add_parser("load", help="Load indexed session transcript")
    load_p.add_argument("--session-id", required=True)
    load_p.add_argument("--owner", default=None)
    load_p.add_argument("--json", action="store_true", help="Emit JSON output (default)")

    args = parser.parse_args()

    if args.command == "ingest":
        aliases = _normalize_participant_aliases(args.participant_aliases)
        out = run(
            session_id=args.session_id,
            owner_id=args.owner,
            label=args.label,
            session_file=args.session_file,
            transcript_path=args.transcript_path,
            source_channel=(str(args.source_channel or "").strip() or None),
            conversation_id=(str(args.conversation_id or "").strip() or None),
            participant_ids=[p.strip() for p in str(args.participant_ids or "").split(",") if p.strip()],
            participant_aliases=aliases,
            message_count=args.message_count,
            topic_hint=args.topic_hint,
        )
        print(json.dumps(out))
        return 0 if out.get("status") not in {"failed", "error"} else 1

    if args.command == "list":
        print(json.dumps(get_session_memory_bridge().list_session_transcripts(
            limit=int(args.limit or 5),
            owner_id=str(args.owner) if args.owner else None,
        )))
        return 0

    if args.command == "load":
        print(json.dumps(get_session_memory_bridge().load_session_transcript(
            session_id=str(args.session_id),
            owner_id=str(args.owner) if args.owner else None,
        )))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
