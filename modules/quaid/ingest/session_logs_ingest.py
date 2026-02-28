#!/usr/bin/env python3
"""Session log ingest bridge.

Core/runtime emits lifecycle events; this module resolves transcript sources and
hands indexing/search/load operations to datastore-owned session log routines.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from lib.runtime_context import get_adapter_instance


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
        parsed = json.loads(payload)
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


_SESSION_LOGS_MODULE = "datastore.memorydb.session_logs"


def _call_session_logs_cli(command: str, args: list[str]) -> Dict[str, Any]:
    env = dict(os.environ)
    plugin_root = str(Path(__file__).resolve().parents[1])
    current_pp = str(env.get("PYTHONPATH", "")).strip()
    env["PYTHONPATH"] = plugin_root if not current_pp else f"{plugin_root}:{current_pp}"
    proc = subprocess.run(
        ["python3", "-m", _SESSION_LOGS_MODULE, command, *args],
        cwd=plugin_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        stderr_text = (proc.stderr or "").strip()
        stdout_text = (proc.stdout or "").strip()
        detail_parts = []
        if stderr_text:
            detail_parts.append(f"stderr: {stderr_text}")
        if stdout_text:
            detail_parts.append(f"stdout: {stdout_text}")
        detail = " | ".join(detail_parts) if detail_parts else f"session_logs {command} failed"
        raise RuntimeError(f"session_logs {command} failed (exit={proc.returncode}): {detail}")
    try:
        return json.loads(proc.stdout or "{}")
    except Exception as exc:
        raise RuntimeError(f"session_logs {command} returned invalid JSON: {exc}") from exc


def _resolve_transcript_source(
    *,
    session_id: str,
    session_file: Optional[str],
    transcript_path: Optional[str],
) -> tuple[Optional[Path], Optional[str], str]:
    if transcript_path:
        p = Path(str(transcript_path)).expanduser()
        if p.exists() and p.is_file():
            return p, p.read_text(encoding="utf-8"), "transcript_path"

    if session_file:
        p = Path(str(session_file)).expanduser()
        if p.exists() and p.is_file():
            return p, _build_transcript_from_session_file(p), "session_file"

    session_path = get_adapter_instance().get_session_path(session_id)
    if session_path and session_path.exists() and session_path.is_file():
        return session_path, _build_transcript_from_session_file(session_path), "adapter_session_path"

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
        return {
            "status": "skipped",
            "reason": "transcript_unavailable",
            "session_id": sid,
            "source_kind": source_kind,
        }

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as tmp:
        tmp.write(transcript)
        tmp_path = tmp.name
    try:
        result = _call_session_logs_cli(
            "ingest",
            [
                "--session-id", sid,
                "--owner", str(owner_id or "default"),
                "--label", str(label or "unknown"),
                "--transcript-file", tmp_path,
                *(["--source-channel", str(source_channel)] if source_channel else []),
                *(["--conversation-id", str(conversation_id)] if conversation_id else []),
                *(["--participant-ids", ",".join(str(p).strip() for p in (participant_ids or []) if str(p).strip())] if participant_ids else []),
                *(["--participant-aliases", json.dumps(normalized_aliases or {}, ensure_ascii=True)] if normalized_aliases else []),
                "--message-count", str(int(message_count or 0)),
                "--topic-hint", str(topic_hint or ""),
                *(["--source-path", str(src_path)] if src_path else []),
            ],
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
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

    load_p = sub.add_parser("load", help="Load indexed session transcript")
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
        print(json.dumps(_call_session_logs_cli("list", ["--limit", str(int(args.limit or 5)), *(["--owner", str(args.owner)] if args.owner else [])])))
        return 0

    if args.command == "load":
        print(json.dumps(_call_session_logs_cli("load", ["--session-id", str(args.session_id), *(["--owner", str(args.owner)] if args.owner else [])])))
        return 0

    if args.command == "last":
        payload = _call_session_logs_cli(
            "last",
            [
                *(["--owner", str(args.owner)] if args.owner else []),
                *(["--exclude-session-id", str(args.exclude_session_id)] if args.exclude_session_id else []),
            ],
        )
        print(json.dumps(payload))
        return 0

    if args.command == "search":
        payload = _call_session_logs_cli(
            "search",
            [
                str(args.query),
                "--limit", str(int(args.limit or 5)),
                "--min-similarity", str(float(args.min_similarity or 0.15)),
                *(["--owner", str(args.owner)] if args.owner else []),
            ],
        )
        print(json.dumps(payload))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
