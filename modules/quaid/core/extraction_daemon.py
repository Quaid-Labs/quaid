#!/usr/bin/env python3
"""Quaid Extraction Daemon — per-instance extraction coordinator.

A long-lived process (one per QUAID_INSTANCE) that processes extraction signals
from adapters. Handles chunked extraction with cursor management and
compaction-aware timeout extraction.

Adapters write signal files to $QUAID_INSTANCE_ROOT/data/extraction-signals/.
The daemon polls for signals, processes them serially, and advances
cursors to prevent re-extraction.

Signal types:
    compaction   — Context is about to be compacted. Extract new content.
    reset        — Session reset (/new, /reset). Extract content.
    session_end  — Session ended cleanly. Extract remaining content.

Lifecycle:
    quaid daemon start   — Fork, write PID, exit parent.
    quaid daemon stop    — Send SIGTERM to PID.
    quaid daemon status  — Check if PID is alive.

Adapters ensure the daemon is alive on session init and launch it if not.
Each QUAID_INSTANCE gets its own daemon with its own PID file, signal dir,
and cursor state.
"""

import fcntl
import json
import logging
import logging.handlers
import os
import re
import signal
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure plugin root is importable (B060)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger("quaid.daemon")

# Valid signal types (B062)
VALID_SIGNAL_TYPES = ("compaction", "reset", "session_end", "timeout")

# Session ID validation (B008)
_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")

# Max lines to read from a transcript per extraction (B033)
MAX_TRANSCRIPT_LINES = 50_000

# Max signals to process per poll cycle (B031)
MAX_SIGNALS_PER_POLL = 100

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _quaid_home() -> Path:
    """QUAID_HOME root (contains all instances)."""
    env = os.environ.get("QUAID_HOME", "").strip()
    # B022: Always resolve to absolute path
    return Path(env).resolve() if env else Path.home() / "quaid"


def _instance_id() -> str:
    """Current instance identifier from QUAID_INSTANCE env var."""
    from lib.instance import instance_id
    return instance_id()


def _instance_root() -> Path:
    """Resolved instance root: QUAID_HOME / QUAID_INSTANCE."""
    return _quaid_home() / _instance_id()


def _get_quaid_version() -> str:
    """Read Quaid version from package.json."""
    try:
        pkg = _quaid_home().parent / "package.json"
        if not pkg.exists():
            # Try relative to this file
            pkg = Path(__file__).parent.parent / "package.json"
        if pkg.exists():
            data = json.loads(pkg.read_text())
            return data.get("version", "unknown")
    except (json.JSONDecodeError, OSError):
        pass
    return "unknown"


def _signal_dir() -> Path:
    # Signals are per-instance to prevent cross-instance daemon race conditions.
    d = _instance_root() / "data" / "extraction-signals"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cursor_dir() -> Path:
    d = _instance_root() / "data" / "session-cursors"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _tmp_dir() -> Path:
    """Per-instance temp directory (B030: avoid world-readable /tmp)."""
    d = _instance_root() / "data" / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pid_path() -> Path:
    return _instance_root() / "data" / "extraction-daemon.pid"


def _log_path() -> Path:
    d = _instance_root() / "logs" / "daemon"
    d.mkdir(parents=True, exist_ok=True)
    return d / "extraction-daemon.log"


def _install_state_path() -> Path:
    return _instance_root() / "data" / "installed-at.json"


# ---------------------------------------------------------------------------
# Atomic file writes (B004)
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, content: str) -> None:
    """Write content atomically via temp file + os.replace()."""
    tmp_path = path.with_suffix(f".tmp.{os.getpid()}")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(str(tmp_path), str(path))
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Session ID validation (B008)
# ---------------------------------------------------------------------------

def _validate_session_id(session_id: str) -> str:
    """Validate and sanitize session_id to prevent path traversal."""
    if not session_id or not _SESSION_ID_RE.match(session_id):
        # Generate a safe fallback (B045)
        safe = f"unknown-{int(time.time())}-{os.getpid()}"
        logger.warning("invalid session_id %r, using fallback: %s", session_id, safe)
        return safe
    return session_id


# ---------------------------------------------------------------------------
# PID file management (B001: flock for atomicity)
# ---------------------------------------------------------------------------

def read_pid() -> Optional[int]:
    """Read daemon PID from file. Returns None if not found or stale."""
    pid_file = _pid_path()
    if not pid_file.is_file():
        return None
    try:
        pid = int(pid_file.read_text().strip())
        # Check if process is alive
        os.kill(pid, 0)
        return pid
    except (ValueError, OSError):
        # PID file exists but process is dead
        try:
            pid_file.unlink()
        except OSError:
            pass
        return None


def write_pid(pid: int) -> None:
    _pid_path().parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(_pid_path(), str(pid))


def remove_pid() -> None:
    try:
        _pid_path().unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Signal files
# ---------------------------------------------------------------------------

def write_signal(
    signal_type: str,
    session_id: str,
    transcript_path: str,
    adapter: str = "",
    supports_compaction_control: bool = False,
    meta: Optional[Dict[str, Any]] = None,
) -> Path:
    """Write an extraction signal file for the daemon to process.

    Called by adapter hooks (CC, OC) when extraction should happen.
    Returns the path to the signal file.
    """
    # B062: Validate signal type
    if signal_type not in VALID_SIGNAL_TYPES:
        logger.warning("unknown signal type %r, defaulting to session_end", signal_type)
        signal_type = "session_end"

    # B008: Validate session_id
    session_id = _validate_session_id(session_id)

    payload = {
        "type": signal_type,
        "session_id": session_id,
        "transcript_path": transcript_path,
        "adapter": adapter,
        "supports_compaction_control": supports_compaction_control,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "meta": meta or {},
    }
    sig_dir = _signal_dir()
    # B047: Use UUID suffix for uniqueness (avoids ms-level collision)
    fname = f"{int(time.time() * 1000)}_{os.getpid()}_{uuid.uuid4().hex[:8]}_{signal_type}.json"
    sig_path = sig_dir / fname
    _atomic_write(sig_path, json.dumps(payload))
    return sig_path


def read_pending_signals() -> List[Dict[str, Any]]:
    """Read pending signal files, sorted by timestamp, capped at MAX_SIGNALS_PER_POLL."""
    sig_dir = _signal_dir()
    if not sig_dir.is_dir():
        return []

    signals = []
    for f in sorted(sig_dir.iterdir()):
        if not f.name.endswith(".json"):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            data["_signal_path"] = str(f)
            signals.append(data)
        except (json.JSONDecodeError, OSError):
            # Remove corrupt signal files
            try:
                f.unlink()
            except OSError:
                pass
        if len(signals) >= MAX_SIGNALS_PER_POLL:
            break
    return signals


def mark_signal_processed(signal_data: Dict[str, Any]) -> None:
    """Remove a processed signal file."""
    sig_path = signal_data.get("_signal_path", "")
    if not sig_path:
        return
    sig = Path(sig_path)
    # B037: Containment check — only delete files within signal directory
    try:
        if not sig.resolve().is_relative_to(_signal_dir().resolve()):
            logger.warning("refusing to delete signal outside signal dir: %s", sig_path)
            return
    except (ValueError, OSError):
        return
    try:
        sig.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Cursors
# ---------------------------------------------------------------------------

def read_cursor(session_id: str) -> Dict[str, Any]:
    """Read extraction cursor for a session. Returns dict with line_offset and transcript_path."""
    session_id = _validate_session_id(session_id)
    cursor_file = _cursor_dir() / f"{session_id}.json"
    if not cursor_file.is_file():
        return {"line_offset": 0, "transcript_path": ""}
    try:
        data = json.loads(cursor_file.read_text(encoding="utf-8"))
        return {
            "line_offset": int(data.get("line_offset", 0)),
            "transcript_path": data.get("transcript_path", ""),
        }
    except (json.JSONDecodeError, ValueError, OSError):
        return {"line_offset": 0, "transcript_path": ""}


def write_cursor(session_id: str, line_offset: int, transcript_path: str) -> None:
    """Write extraction cursor after processing."""
    session_id = _validate_session_id(session_id)
    cursor_file = _cursor_dir() / f"{session_id}.json"
    payload = {
        "session_id": session_id,
        "line_offset": line_offset,
        "transcript_path": transcript_path,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        _atomic_write(cursor_file, json.dumps(payload))
    except OSError as e:
        logger.error("cursor write failed for %s: %s", session_id, e)


# ---------------------------------------------------------------------------
# Transcript reading
# ---------------------------------------------------------------------------

def read_transcript_slice(transcript_path: str, from_line: int) -> List[str]:
    """Read transcript lines starting at from_line offset.

    Caps at MAX_TRANSCRIPT_LINES to prevent OOM (B033).
    Uses errors='replace' to handle non-UTF8 content (B041).
    """
    lines = []
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= from_line:
                    lines.append(line)
                    if len(lines) >= MAX_TRANSCRIPT_LINES:
                        logger.warning(
                            "transcript %s: capped at %d lines (from offset %d)",
                            transcript_path, MAX_TRANSCRIPT_LINES, from_line,
                        )
                        break
    except OSError as e:
        logger.error("failed reading transcript %s: %s", transcript_path, e)
    return lines


def count_transcript_lines(transcript_path: str) -> int:
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Core extraction processing
# ---------------------------------------------------------------------------

def process_signal(signal_data: Dict[str, Any]) -> None:
    """Process a single extraction signal.

    Reads transcript from cursor, passes to extract_from_transcript()
    which handles chunking and storage internally.
    """
    signal_type = signal_data.get("type", "unknown")
    session_id = signal_data.get("session_id", "unknown")
    transcript_path = signal_data.get("transcript_path", "")
    label = f"daemon-{signal_type}"

    # B062: Skip unknown signal types
    if signal_type not in VALID_SIGNAL_TYPES:
        logger.warning("[%s] unknown signal type, skipping", label)
        mark_signal_processed(signal_data)
        return

    # B008: Validate session_id
    session_id = _validate_session_id(session_id)

    # Skip extraction for registered subagents — their transcripts are
    # merged into the parent session's extraction batch instead.
    try:
        from core.subagent_registry import is_registered_subagent
        if is_registered_subagent(session_id):
            logger.info("[%s] session %s: registered subagent, skipping standalone extraction", label, session_id)
            mark_signal_processed(signal_data)
            return
    except Exception:
        pass

    if not transcript_path or not os.path.isfile(transcript_path):
        logger.warning("[%s] transcript not found: %s", label, transcript_path)
        mark_signal_processed(signal_data)
        return

    # B057: Read cursor and compare transcript_path
    cursor_data = read_cursor(session_id)
    cursor_offset = cursor_data["line_offset"]
    cursor_transcript = cursor_data["transcript_path"]

    # If transcript path changed (file rotation), reset cursor
    if cursor_transcript and cursor_transcript != transcript_path:
        logger.info(
            "[%s] session %s: transcript path changed (%s -> %s), resetting cursor",
            label, session_id, cursor_transcript, transcript_path,
        )
        cursor_offset = 0

    # B055: Detect cursor > file length (file truncation/rotation)
    total_lines = count_transcript_lines(transcript_path)
    if cursor_offset > total_lines:
        logger.warning(
            "[%s] session %s: cursor offset %d > file length %d (file truncated?), resetting cursor",
            label, session_id, cursor_offset, total_lines,
        )
        cursor_offset = 0

    new_lines = read_transcript_slice(transcript_path, cursor_offset)

    if not new_lines:
        logger.info("[%s] session %s: no new content past cursor (offset=%d)",
                     label, session_id, cursor_offset)
        # Still index the session at session_end even if no new content to extract.
        # The cursor may have been advanced by a prior signal — the session is
        # complete and must be reachable via 'quaid session list/load'.
        if signal_type == "session_end":
            try:
                from core.ingest_runtime import run_session_logs_ingest
                sl_result = run_session_logs_ingest(
                    session_id=session_id,
                    owner_id=_get_owner_id(),
                    label=label,
                    transcript_path=str(transcript_path),
                    message_count=0,
                    topic_hint="",
                )
                sl_status = sl_result.get("status", "unknown") if isinstance(sl_result, dict) else str(sl_result)
                sl_reason = sl_result.get("reason", "") if isinstance(sl_result, dict) else ""
                logger.info("[%s] session %s: session_logs ingest (no-new-content path): %s%s",
                            label, session_id, sl_status,
                            f" ({sl_reason})" if sl_reason else "")
            except Exception as e:
                logger.warning("[%s] session %s: session_logs ingest failed (no-new-content path): %s",
                               label, session_id, e)
        mark_signal_processed(signal_data)
        return

    # If read_transcript_slice hit the cap on a compaction/reset signal, the
    # remaining lines beyond the cap could be lost if the transcript gets wiped
    # before the daemon cycles again. Write a follow-up session_end signal now
    # so the remaining lines are extracted even if the original trigger is gone.
    capped_lines = len(new_lines) >= MAX_TRANSCRIPT_LINES
    if capped_lines and signal_type in ("compaction", "reset"):
        remaining_after_cap = total_lines - (cursor_offset + len(new_lines))
        if remaining_after_cap > 0:
            logger.warning(
                "[%s] session %s: transcript cap hit on %s signal; %d lines remain above cap; "
                "writing follow-up session_end signal to prevent data loss on transcript rotation",
                label, session_id, signal_type, remaining_after_cap,
            )
            write_signal(
                signal_type="session_end",
                session_id=session_id,
                transcript_path=transcript_path,
                meta={"reason": "cap_followup", "cap_offset": cursor_offset + len(new_lines)},
            )

    # B030: Write lines to temp file in QUAID_HOME/data/tmp/ (not /tmp)
    tmp_dir = _tmp_dir()
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", delete=False, encoding="utf-8",
        dir=str(tmp_dir),
    ) as tmp:
        tmp.writelines(new_lines)
        tmp_path = tmp.name

    try:
        from lib.adapter import get_adapter
        adapter = get_adapter()
        transcript_text = adapter.parse_session_jsonl(Path(tmp_path))

        if not transcript_text.strip():
            logger.info("[%s] session %s: empty transcript after parsing", label, session_id)
            write_cursor(session_id, cursor_offset + len(new_lines), transcript_path)
            # B002: Mark processed only on success path
            mark_signal_processed(signal_data)
            return

        # Merge harvestable subagent transcripts into parent extraction.
        # B010: Per-child size is advisory (chunker handles large transcripts).
        # MAX_MERGED_CHARS bounds the total injected into this extraction pass.
        # Subagents deferred due to the total cap are NOT dropped — a follow-up
        # session_end signal is written for the parent so they are harvested on
        # the next daemon cycle rather than silently skipped.
        MAX_CHILD_CHARS = 50_000
        MAX_MERGED_CHARS = 200_000
        harvestable = []
        merged_chars = 0
        deferred_subagents: List[Dict[str, Any]] = []
        try:
            from core.subagent_registry import get_harvestable, mark_harvested
            harvestable = get_harvestable(session_id)
            for child in harvestable:
                child_path = child.get("transcript_path", "")
                child_id = child.get("child_id", "")
                if child_path and os.path.isfile(child_path):
                    if merged_chars >= MAX_MERGED_CHARS:
                        # Defer rather than drop — record for follow-up signal.
                        deferred_subagents.append(child)
                        continue
                    try:
                        child_text = adapter.parse_session_jsonl(Path(child_path))
                        if child_text.strip():
                            if len(child_text) > MAX_CHILD_CHARS:
                                logger.warning(
                                    "[%s] session %s: subagent %s transcript is very large (%d chars), "
                                    "extraction chunker will handle splitting",
                                    label, session_id, child_id, len(child_text),
                                )
                            transcript_text += (
                                f"\n\n--- Subagent ({child.get('child_type', 'unknown')}) ---\n"
                                + child_text
                            )
                            merged_chars += len(child_text)
                            logger.info(
                                "[%s] session %s: merged subagent %s transcript (%d chars)",
                                label, session_id, child_id, len(child_text),
                            )
                    except Exception as e:
                        logger.warning(
                            "[%s] session %s: failed to parse subagent %s transcript: %s",
                            label, session_id, child_id, e,
                        )
            if deferred_subagents:
                logger.warning(
                    "[%s] session %s: %d subagent(s) deferred due to merged transcript cap "
                    "(%d chars); writing follow-up session_end signal for parent session",
                    label, session_id, len(deferred_subagents), merged_chars,
                )
                # Write a follow-up signal so the parent session is re-processed
                # and remaining subagents are harvested.
                write_signal(
                    signal_type="session_end",
                    session_id=session_id,
                    transcript_path=transcript_path,
                    meta={"reason": "deferred_subagents", "deferred_count": len(deferred_subagents)},
                )
        except Exception as e:
            logger.warning("[%s] session %s: subagent merge error: %s", label, session_id, e)

        # Delegate to extract_from_transcript() — it handles chunking,
        # LLM calls, fact storage, snippets, journal, and project logs.
        # carry_facts starts empty each tick; extract_from_transcript builds
        # it in-memory across chunks within this single extraction run.
        from ingest.extract import extract_from_transcript

        owner = _get_owner_id()
        result = extract_from_transcript(
            transcript=transcript_text,
            owner_id=owner,
            label=label,
            session_id=session_id,
            carry_facts=[],
        )

        facts_stored = result.get("facts_stored", 0)
        facts_skipped = result.get("facts_skipped", 0)
        edges_created = result.get("edges_created", 0)

        logger.info(
            "[%s] session %s: %d stored, %d skipped, %d edges",
            label, session_id, facts_stored, facts_skipped, edges_created,
        )

        # Send extraction notification (Telegram, stderr, etc.)
        try:
            from core.runtime.notify import notify_memory_extraction
            notify_memory_extraction(
                facts_stored=facts_stored,
                facts_skipped=facts_skipped,
                edges_created=edges_created,
                trigger=signal_type,
                details=result.get("facts"),
                snippet_details=result.get("snippets"),
            )
        except Exception as e:
            logger.warning("[%s] session %s: notification failed: %s", label, session_id, e)

        # Index session into session_logs so 'quaid session list/load' can find it.
        # Run after notification so a failure here doesn't block the main path.
        try:
            from core.ingest_runtime import run_session_logs_ingest
            sl_result = run_session_logs_ingest(
                session_id=session_id,
                owner_id=owner,
                label=label,
                transcript_path=str(transcript_path),
                message_count=len(new_lines),
                topic_hint=result.get("topic_hint", ""),
            )
            sl_status = sl_result.get("status", "unknown") if isinstance(sl_result, dict) else str(sl_result)
            sl_reason = sl_result.get("reason", "") if isinstance(sl_result, dict) else ""
            logger.info("[%s] session %s: session_logs ingest: %s%s",
                        label, session_id, sl_status,
                        f" ({sl_reason})" if sl_reason else "")
        except Exception as e:
            logger.warning("[%s] session %s: session_logs ingest failed: %s", label, session_id, e)

        # B054: Only advance cursor if extraction completed all chunks
        chunks_processed = result.get("chunks_processed", 0)
        chunks_total = result.get("chunks_total", 0)
        if chunks_total > 0 and chunks_processed < chunks_total:
            logger.warning(
                "[%s] session %s: extraction incomplete (%d/%d chunks), "
                "NOT advancing cursor to allow retry",
                label, session_id, chunks_processed, chunks_total,
            )
        else:
            # Advance cursor
            write_cursor(session_id, cursor_offset + len(new_lines), transcript_path)

        # Mark harvested subagent transcripts after successful extraction
        try:
            for child in harvestable:
                mark_harvested(session_id, child.get("child_id", ""))
        except Exception as e:
            logger.warning("[%s] session %s: mark_harvested error: %s", label, session_id, e)

        # B002: Mark processed only on success
        mark_signal_processed(signal_data)

        # Post-extraction hooks: snapshot shadow git for tracked projects.
        snapshots = []
        try:
            from core.project_registry import snapshot_all_projects
            snapshots = snapshot_all_projects()
            for snap in snapshots:
                logger.info(
                    "[%s] shadow snapshot %s: %d changes",
                    label, snap["project"], len(snap["changes"]),
                )
        except Exception as e:
            logger.warning("[%s] post-extraction shadow git error: %s", label, e)

        # Update project docs from shadow git diffs
        if snapshots:
            try:
                from core.docs_updater_hook import update_project_docs
                doc_metrics = update_project_docs(snapshots, extraction_result=result)
                if doc_metrics.get("docs_updated", 0):
                    logger.info("[%s] docs updated: %s", label, doc_metrics)
            except Exception as e:
                logger.warning("[%s] post-extraction docs update error: %s", label, e)

    except Exception as e:
        # B002: Do NOT mark_signal_processed here — leave signal for retry
        logger.error("[%s] session %s: extraction failed (signal preserved for retry): %s",
                     label, session_id, e, exc_info=True)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_owner_id() -> str:
    from lib.adapter import get_owner_id
    return get_owner_id()


def _read_installed_at() -> float:
    """Read or initialize the install-time lower bound for timeout sweeps."""
    path = _install_state_path()
    try:
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            installed_at = str(raw.get("installedAt", "")).strip()
            if installed_at:
                normalized = installed_at.replace("Z", "+00:00")
                return datetime.fromisoformat(normalized).timestamp()
    except Exception:
        pass

    installed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, json.dumps({"installedAt": installed_at}))
    except Exception:
        pass
    return time.time()


def _get_idle_timeout_minutes(default: int = 30) -> int:
    """Read timeout minutes from live config with a safe fallback."""
    try:
        from config import get_config
        cfg = get_config()
        capture = getattr(cfg, "capture", None)
        raw = getattr(capture, "inactivity_timeout_minutes", default) if capture is not None else default
        minutes = int(raw)
        return max(0, minutes)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Idle session detection (timeout extraction)
# ---------------------------------------------------------------------------

def check_idle_sessions(timeout_minutes: int = 30) -> None:
    """Check for sessions that have been idle beyond the timeout.

    Generates a timeout extraction signal for any session with unextracted
    content whose transcript hasn't been modified for timeout_minutes.
    Cursor tracking prevents double extraction, so this is safe regardless
    of whether the adapter supports compaction control.
    """
    cursor_dir = _cursor_dir()
    if not cursor_dir.is_dir():
        return

    now = time.time()
    timeout_seconds = timeout_minutes * 60
    installed_at_ts = _read_installed_at()

    # B002: Cache registered subagent IDs once instead of scanning per cursor file
    registered_subagents: set = set()
    try:
        from core.subagent_registry import _registry_dir
        for p in _registry_dir().glob("*.json"):
            try:
                rdata = json.loads(p.read_text(encoding="utf-8"))
                registered_subagents.update(rdata.get("children", {}).keys())
            except (json.JSONDecodeError, OSError):
                continue
    except Exception:
        pass

    # B003: Hoist pending signals read outside the loop
    pending = read_pending_signals()
    pending_session_ids = {s.get("session_id") for s in pending}

    for cursor_file in cursor_dir.glob("*.json"):
        try:
            data = json.loads(cursor_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        session_id = data.get("session_id", "")
        transcript_path = data.get("transcript_path", "")
        if not session_id or not transcript_path or not os.path.isfile(transcript_path):
            continue

        # Skip registered subagents — their transcripts are merged into parent extraction
        if session_id in registered_subagents:
            continue

        # Check if transcript has grown past cursor
        cursor_offset = data.get("line_offset", 0)
        total_lines = count_transcript_lines(transcript_path)
        if total_lines <= cursor_offset:
            continue

        # Check transcript modification time for idle detection
        try:
            mtime = os.path.getmtime(transcript_path)
        except OSError:
            continue

        if mtime < installed_at_ts:
            continue

        idle_seconds = now - mtime
        if idle_seconds < timeout_seconds:
            continue

        # Check if we already have a pending signal for this session
        if session_id in pending_session_ids:
            continue

        logger.info(
            "session %s idle for %.0fs with %d unextracted lines, generating timeout signal",
            session_id, idle_seconds, total_lines - cursor_offset,
        )
        write_signal(
            signal_type="timeout",
            session_id=session_id,
            transcript_path=transcript_path,
        )


# ---------------------------------------------------------------------------
# Orphaned session sweep (runs on session-init)
# ---------------------------------------------------------------------------

def sweep_orphaned_sessions(current_session_id: str = "") -> int:
    """Extract tails from previous sessions with un-extracted content.

    Called during session-init. Returns number of sessions swept.
    """
    cursor_dir = _cursor_dir()
    if not cursor_dir.is_dir():
        return 0

    # B002: Cache registered subagent IDs once
    registered_subagents: set = set()
    try:
        from core.subagent_registry import _registry_dir
        for p in _registry_dir().glob("*.json"):
            try:
                rdata = json.loads(p.read_text(encoding="utf-8"))
                registered_subagents.update(rdata.get("children", {}).keys())
            except (json.JSONDecodeError, OSError):
                continue
    except Exception:
        pass

    swept = 0
    for cursor_file in cursor_dir.glob("*.json"):
        try:
            data = json.loads(cursor_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        session_id = data.get("session_id", "")
        if not session_id or session_id == current_session_id:
            continue

        # Skip registered subagents — their transcripts are merged into parent extraction
        if session_id in registered_subagents:
            continue

        transcript_path = data.get("transcript_path", "")
        if not transcript_path or not os.path.isfile(transcript_path):
            continue

        cursor_offset = data.get("line_offset", 0)
        total_lines = count_transcript_lines(transcript_path)
        if total_lines <= cursor_offset:
            continue

        # Write a session_end signal for the daemon to process
        logger.info(
            "orphan sweep: session %s has %d unextracted lines",
            session_id, total_lines - cursor_offset,
        )
        write_signal(
            signal_type="session_end",
            session_id=session_id,
            transcript_path=transcript_path,
        )
        swept += 1

    return swept


# ---------------------------------------------------------------------------
# Daemon main loop
# ---------------------------------------------------------------------------

def daemon_loop(poll_interval: float = 5.0, idle_check_interval: float = 300.0) -> None:
    """Main daemon loop. Polls for signals and processes them."""
    # Mark this process as the extraction daemon so LLM providers skip the
    # claude -p subprocess path.  Using claude -p inside the daemon creates new
    # CC sessions, which fire hooks, which start more daemons — an exponential
    # process storm.  OAuth / API-key layers are used instead.
    os.environ["QUAID_DAEMON"] = "1"

    logger.info("extraction daemon started (pid=%d, home=%s, instance=%s)", os.getpid(), _quaid_home(), _instance_id())
    write_pid(os.getpid())

    shutdown_requested = False

    def handle_sigterm(signum, frame):
        nonlocal shutdown_requested
        logger.info("SIGTERM received, processing remaining signals before exit...")
        shutdown_requested = True

    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)

    last_idle_check = 0.0

    # Initialize version watcher and janitor scheduler
    from core.compatibility import VersionWatcher, JanitorScheduler, read_circuit_breaker
    home = _instance_root()
    data_dir = home / "data"
    quaid_version = _get_quaid_version()
    version_watcher = VersionWatcher(data_dir=data_dir, quaid_version=quaid_version)
    janitor_scheduler = JanitorScheduler(data_dir=data_dir, quaid_home=home)

    try:
        while not shutdown_requested:
            # Version watcher tick — cheap mtime check on every iteration
            try:
                version_watcher.tick()
            except Exception as e:
                logger.debug("version watcher tick failed: %s", e)

            # Check circuit breaker before processing signals
            breaker = read_circuit_breaker(data_dir)
            if not breaker.allows_writes():
                # In degraded/safe mode — skip extraction, just idle
                if breaker.message:
                    logger.debug("Circuit breaker %s: %s", breaker.status, breaker.message)
                time.sleep(poll_interval)
                continue

            # Process pending signals
            signals = read_pending_signals()
            for sig in signals:
                try:
                    process_signal(sig)
                except Exception as e:
                    logger.error("failed processing signal: %s", e, exc_info=True)
                    # Preserve the signal for a future retry. Outer-loop exceptions
                    # mean we do not know whether processing was durable.

            # Periodic idle session check. Use a timeout-aware cadence so
            # shorter configured inactivity windows do not wait on a fixed
            # five-minute sweep interval before becoming eligible.
            now = time.time()
            configured_timeout_minutes = _get_idle_timeout_minutes()
            if configured_timeout_minutes > 0:
                timeout_seconds = configured_timeout_minutes * 60
                effective_idle_check_interval = max(
                    poll_interval,
                    min(idle_check_interval, max(5.0, timeout_seconds / 2.0)),
                )
            else:
                effective_idle_check_interval = idle_check_interval

            if now - last_idle_check > effective_idle_check_interval:
                try:
                    if configured_timeout_minutes > 0:
                        check_idle_sessions(configured_timeout_minutes)
                except Exception as e:
                    logger.error("idle check failed: %s", e)
                last_idle_check = now

            # Janitor scheduler tick — checks if maintenance is due
            try:
                janitor_scheduler.tick()
            except Exception as e:
                logger.debug("janitor scheduler tick failed: %s", e)

            time.sleep(poll_interval)

        # On shutdown: process any remaining signals
        logger.info("shutdown: processing remaining signals...")
        signals = read_pending_signals()
        for sig in signals:
            try:
                process_signal(sig)
            except Exception as e:
                logger.error("shutdown signal processing failed: %s", e)
                # Preserve the signal across shutdown so the next daemon instance
                # can retry it instead of dropping extraction work.

    finally:
        remove_pid()
        logger.info("extraction daemon exited")


# ---------------------------------------------------------------------------
# Daemon lifecycle commands
# ---------------------------------------------------------------------------

def ensure_alive() -> int:
    """Ensure the daemon is running. Start it if not. Returns PID."""
    pid = read_pid()
    if pid is not None:
        return pid
    return start_daemon()


def start_daemon() -> int:
    """Start the daemon as a background process. Returns child PID.

    Uses flock on PID file to prevent concurrent starts (B001).
    """
    # B001: Acquire exclusive lock on PID file to prevent TOCTOU race
    pid_file = _pid_path()
    pid_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        lock_fd = os.open(str(pid_file), os.O_RDWR | os.O_CREAT)
    except OSError as e:
        logger.error("cannot open PID file for locking: %s", e)
        # Fall back to checking existing PID
        existing = read_pid()
        return existing if existing else -1

    try:
        # Non-blocking exclusive lock
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError):
        # Another process holds the lock — daemon is starting or running
        os.close(lock_fd)
        existing = read_pid()
        if existing is not None:
            return existing
        # Lock held but no valid PID — wait briefly and retry
        time.sleep(0.5)

        existing = read_pid()
        return existing if existing else -1

    try:
        # Re-check PID under lock
        existing = read_pid()
        if existing is not None:
            return existing

        # Double-fork to fully detach
        pid = os.fork()
        if pid > 0:
            # B005: Parent waits on first child to prevent zombie
            os.waitpid(pid, 0)
            # Wait briefly for grandchild to write PID. Avoid returning the
            # first-child PID because it exits immediately and is not usable.
            for _ in range(20):
                time.sleep(0.1)
                running_pid = read_pid()
                if running_pid is not None:
                    return running_pid
            return -1

        # First child: create new session
        os.setsid()

        # B029: Set restrictive umask for all files created by daemon
        os.umask(0o077)

        # B059: chdir to QUAID_HOME for stable cwd
        try:
            os.chdir(str(_quaid_home()))
        except OSError:
            pass

        pid2 = os.fork()
        if pid2 > 0:
            os._exit(0)

        # Second child: this is the daemon
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(lock_fd)
        except OSError:
            pass

        # Redirect stdio to log file
        log_file = _log_path()
        sys.stdout.flush()
        sys.stderr.flush()

        with open(log_file, "a") as lf:
            os.dup2(lf.fileno(), sys.stdout.fileno())
            os.dup2(lf.fileno(), sys.stderr.fileno())

        # Close stdin
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, sys.stdin.fileno())
        os.close(devnull)

        # B027: Set up logging with rotation (10MB per file, 3 backups)
        handler = logging.handlers.RotatingFileHandler(
            str(log_file), maxBytes=10 * 1024 * 1024, backupCount=3,
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s %(message)s"
        ))
        root_logger = logging.getLogger()
        root_logger.handlers.clear()
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.INFO)

        try:
            daemon_loop()
        except Exception as e:
            logger.error("daemon crashed: %s", e, exc_info=True)
        finally:
            remove_pid()
            os._exit(0)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(lock_fd)
        except OSError:
            pass


def stop_daemon() -> bool:
    """Stop the daemon. Returns True if it was running."""
    pid = read_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        # Wait up to 10s for clean shutdown
        for _ in range(20):
            time.sleep(0.5)
            try:
                os.kill(pid, 0)
            except OSError:
                remove_pid()
                return True
        # Force kill if still alive
        os.kill(pid, signal.SIGKILL)
        remove_pid()
        return True
    except OSError:
        remove_pid()
        return False


def daemon_status() -> Dict[str, Any]:
    """Check daemon status. Returns status dict."""
    pid = read_pid()
    pending = len(read_pending_signals())
    return {
        "running": pid is not None,
        "pid": pid,
        "quaid_home": str(_quaid_home()),
        "instance": _instance_id(),
        "instance_root": str(_instance_root()),
        "pending_signals": pending,
        "pid_file": str(_pid_path()),
        "log_file": str(_log_path()),
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Quaid extraction daemon")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("start", help="Start the daemon")
    subparsers.add_parser("stop", help="Stop the daemon")
    subparsers.add_parser("status", help="Check daemon status")
    subparsers.add_parser("run", help="Run in foreground (for debugging)")

    args = parser.parse_args()

    if args.command == "start":
        pid = start_daemon()
        print(f"daemon started (pid={pid})")
    elif args.command == "stop":
        stopped = stop_daemon()
        print("daemon stopped" if stopped else "daemon was not running")
    elif args.command == "status":
        status = daemon_status()
        print(json.dumps(status, indent=2))
    elif args.command == "run":
        # Foreground mode for debugging
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        )
        daemon_loop()
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
