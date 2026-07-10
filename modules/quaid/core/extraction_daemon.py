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
    quaid daemon flush   — Process queued signals synchronously.

Adapters ensure the daemon is alive on session init and launch it if not.
Each QUAID_INSTANCE gets its own daemon with its own PID file, signal dir,
and cursor state.
"""

import fcntl
import hashlib
import json
import logging
import logging.handlers
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import unicodedata
import uuid
from io import StringIO
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# Ensure plugin root is importable (B060)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.llm_clients import ProviderConfigError as _ProviderConfigError
from lib.llm_clients import ProviderUnavailableError as _ProviderUnavailableError

logger = logging.getLogger("quaid.daemon")

# Valid signal types (B062)
VALID_SIGNAL_TYPES = ("compaction", "reset", "session_end", "timeout", "rolling")
DAEMON_EXTRACT_CHUNK_MAX_TOKENS = 900
DAEMON_EXTRACT_CHUNK_RATIO = 0.8
DAEMON_EXTRACT_LLM_TIMEOUT_SECONDS = 120.0
DAEMON_EXTRACT_LLM_SLOT_WAIT_SECONDS = 1800.0
DAEMON_EXTRACT_LLM_MAX_RETRIES = 0
DAEMON_SIGNAL_TO_LIFECYCLE_EVENT = {
    "reset": "session.reset",
    "compaction": "session.compaction",
    "timeout": "session.timeout",
    "session_end": "session.agent_end",
}
_SIGNAL_PRIORITY = {
    "rolling": 0,
    "timeout": 1,
    "session_end": 2,
    "reset": 3,
    "compaction": 4,
}
_SIGNAL_POLL_PRIORITY = {
    "reset": 0,
    "session_end": 1,
    "timeout": 2,
    "rolling": 3,
    "compaction": 4,
}
_ROLLING_INTERNAL_ADVANCE_GRACE_SECONDS = 60.0
_INTERNAL_CURSOR_UNFROZEN_PENDING_FLUSH_KEY = "internal_cursor_unfrozen_pending_flush"
_STAGED_PAYLOAD_PENDING_FLUSH_KEY = "staged_payload_pending_flush"
_DISCOVERY_STALE_ORPHAN_GRACE_SECONDS = 10 * 60
_ORPHANED_CURSOR_RETENTION_SECONDS = 7 * 24 * 60 * 60
_IGNORED_TIMEOUT_USER_TURN_MAX_CHARS = 12
_TRANSCRIPT_ROLE_RE = re.compile(
    r"^\s*(?:\[\d{4}-\d{2}-\d{2}[T ][^\]]+\]\s*)?([^:\r\n]{1,80}):\s*(.*)$",
    re.UNICODE,
)
_TRANSCRIPT_USER_ROLE_KEYS = {
    "user",
    "human",
    "owner",
    "operator",
    "usuario",
    "usuaria",
    "usuário",
    "utilizador",
    "utilisateur",
    "utilisatrice",
    "benutzer",
    "nutzer",
    "utente",
    "gebruiker",
    "пользователь",
    "користувач",
    "użytkownik",
    "用户",
    "用戶",
    "使用者",
    "ユーザー",
    "利用者",
    "사용자",
    "مستخدم",
    "المستخدم",
    "उपयोगकर्ता",
    "kullanıcı",
}
_TRANSCRIPT_ASSISTANT_ROLE_KEYS = {
    "assistant",
    "agent",
    "bot",
    "ai",
    "ai assistant",
    "aiアシスタント",
    "asistente",
    "assistente",
    "assistante",
    "assistent",
    "assistentin",
    "помощник",
    "ассистент",
    "助手",
    "助理",
    "ai助手",
    "智能助手",
    "アシスタント",
    "어시스턴트",
    "도우미",
    "مساعد",
    "المساعد",
    "सहायक",
    "asistan",
}
_TRANSCRIPT_SYSTEM_ROLE_KEYS = {
    "system",
    "developer",
    "tool",
    "function",
}
_TRANSCRIPT_CLASS_INTERNAL_MAINTENANCE = "internal_maintenance"
_TRANSCRIPT_CLASS_IGNORE_CONTENT = "ignore_content"
_TRANSCRIPT_CLASS_MEANINGFUL_USER_CONTENT = "meaningful_user_content"

# Session ID validation (B008)
_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
_PLATFORM_CONFIG_SCOPE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
_SESSION_ID_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_DISCOVERY_ARTIFACT_MARKERS = (".checkpoint.", ".reset.", ".trajectory.")

# Tracks sessions that have already had a cursor-at-end timeout signal fired.
# Prevents repeated signals for sessions where rolling extracted all content
# and the cursor never advances again (e.g. session ended without /exit).
# Cleared per-session when the cursor advances past the previously-seen end.
_cursor_end_timeout_fired: set = set()

# Daemon workers are long-lived and config.get_config() is intentionally cached.
# Track active config file mtimes so live-test/operator edits take effect
# without a manual daemon restart.
_config_file_signature: Optional[Tuple[Tuple[str, Optional[int], Optional[int]], ...]] = None
_config_file_signature_context: Optional[Tuple[str, str]] = None

# Max lines to read from a transcript per extraction (B033)
MAX_TRANSCRIPT_LINES = 50_000

# Max signals to process per poll cycle (B031)
MAX_SIGNALS_PER_POLL = 100
MAX_SIGNAL_PROCESS_ATTEMPTS = 5

TRANSCRIPT_REBASE_MAX_RETRIES = 3

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _quaid_home() -> Path:
    """QUAID_HOME root (contains all instances)."""
    env = os.environ.get("QUAID_HOME", "").strip()
    # B022: Always resolve to absolute path
    return Path(env).resolve() if env else Path.home() / ".quaid"


def _instance_id() -> str:
    """Current instance identifier from QUAID_INSTANCE env var."""
    from lib.instance import instance_id
    return instance_id()


def _instance_root() -> Path:
    """Resolved instance root: QUAID_HOME/instances/QUAID_INSTANCE."""
    return _quaid_home() / "instances" / _instance_id()


def _validate_platform_config_scope(scope: str) -> str:
    """Return a safe shared-config platform directory name."""
    raw = str(scope or "").strip()
    if not raw:
        return ""
    if _PLATFORM_CONFIG_SCOPE_RE.fullmatch(raw):
        return raw
    raise ValueError(f"invalid platform_config_scope: {raw!r}")


def _config_file_paths() -> List[Path]:
    """Return config files that affect this instance, highest priority first."""
    instance = _instance_id()
    home = _quaid_home()
    platform = ""
    adapter = None
    try:
        from lib.adapter import get_adapter

        adapter = get_adapter()
    except Exception as exc:
        logger.warning("daemon config adapter lookup failed; using instance-only config paths: %s", exc)
        if _fail_hard_enabled():
            raise RuntimeError("daemon config adapter lookup failed") from exc
        adapter = None
    if adapter is not None:
        get_capability = getattr(adapter, "get_capability", None)
        if callable(get_capability):
            try:
                raw_platform = str(get_capability("platform_config_scope", "") or "").strip()
            except Exception as exc:
                logger.warning(
                    "daemon config adapter platform scope lookup failed; using instance-only config paths: %s",
                    exc,
                )
                if _fail_hard_enabled():
                    raise RuntimeError("daemon config adapter platform scope lookup failed") from exc
                raw_platform = ""
        else:
            raw_platform = ""
        try:
            platform = _validate_platform_config_scope(raw_platform)
        except ValueError as exc:
            if _fail_hard_enabled():
                raise
            logger.warning("ignoring invalid adapter platform_config_scope %r: %s", raw_platform, exc)
            platform = ""

    if not platform:
        config_root = home / "shared" / "config"
        try:
            candidates = [
                path.name
                for path in config_root.iterdir()
                if path.is_dir() and path.name != "global"
            ]
        except OSError:
            candidates = []
        matches = [
            name for name in candidates
            if instance == name or instance.startswith(f"{name}-")
        ]
        if matches:
            platform = max(matches, key=len)
    if not platform:
        platform = instance.split("-", 1)[0] if "-" in instance else instance
    platform = _validate_platform_config_scope(platform)

    return [
        _instance_root() / "config.json",
        home / "shared" / "config" / platform / "config.json",
        home / "shared" / "config" / "global" / "config.json",
    ]


def _active_config_file_signature() -> Tuple[Tuple[str, Optional[int], Optional[int]], ...]:
    """Return a stable signature for config files that affect this daemon."""
    paths = _config_file_paths()

    signature: List[Tuple[str, Optional[int], Optional[int]]] = []
    for raw_path in paths:
        path = Path(raw_path)
        try:
            st = path.stat()
            signature.append((str(path), int(st.st_mtime_ns), int(st.st_size)))
        except FileNotFoundError:
            signature.append((str(path), None, None))
        except OSError as exc:
            logger.debug("config signature stat failed for %s: %s", path, exc)
            signature.append((str(path), None, None))
    return tuple(signature)


def _config_reload_context() -> Tuple[str, str]:
    return (str(_quaid_home()), _instance_id())


def _prime_config_reload_watcher() -> None:
    """Remember current config file mtimes as the daemon baseline."""
    global _config_file_signature, _config_file_signature_context
    _config_file_signature_context = _config_reload_context()
    _config_file_signature = _active_config_file_signature()


def _force_reload_config() -> None:
    from config import reload_config
    from lib.llm_clients import reset_model_config_cache
    reload_config()
    reset_model_config_cache()


def _reload_config_if_changed(reason: str = "daemon poll") -> bool:
    """Reload cached config when any active config file changed on disk."""
    global _config_file_signature, _config_file_signature_context
    context = _config_reload_context()
    current = _active_config_file_signature()
    if _config_file_signature is None or _config_file_signature_context != context:
        _config_file_signature_context = context
        _config_file_signature = current
        return False
    if current == _config_file_signature:
        return False

    try:
        _force_reload_config()
    except Exception as exc:
        logger.warning("config changed but reload failed before %s: %s", reason, exc)
        if _fail_hard_enabled():
            raise RuntimeError(f"config reload failed before {reason}") from exc
        return False

    _config_file_signature = _active_config_file_signature()
    logger.info("config changed on disk; reloaded before %s", reason)
    return True


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


def _signal_dead_letter_dir() -> Path:
    d = _instance_root() / "data" / "extraction-signals-dead-letter"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _signal_write_lock_path(session_id: str) -> Path:
    d = _signal_dir() / ".locks"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{_validate_session_id(session_id)}.lock"


def _acquire_signal_write_lock(session_id: str) -> Optional[int]:
    fd: Optional[int] = None
    try:
        fd = os.open(str(_signal_write_lock_path(session_id)), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd
    except (OSError, IOError) as exc:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if _fail_hard_enabled():
            raise RuntimeError(f"failed acquiring signal write lock for {session_id}") from exc
        logger.warning("failed acquiring signal write lock for %s; writing without dedupe lock: %s", session_id, exc)
        return None


def _release_signal_write_lock(lock_fd: Optional[int]) -> None:
    if lock_fd is None:
        return
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(lock_fd)
    except OSError:
        pass


def _signal_path_exists(path: Path) -> bool:
    return path.exists()


def _cursor_dir() -> Path:
    d = _instance_root() / "data" / "session-cursors"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _tmp_dir() -> Path:
    """Per-instance temp directory (B030: avoid world-readable /tmp)."""
    d = _instance_root() / "data" / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _rolling_transcript_snapshot_dir() -> Path:
    d = _instance_root() / "logs" / "daemon" / "rolling-transcript-snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pid_path() -> Path:
    return _instance_root() / "data" / "extraction-daemon.pid"


def _pid_lock_path() -> Path:
    pid_path = _pid_path()
    return pid_path.with_name(f"{pid_path.name}.lock")


def _log_path() -> Path:
    d = _instance_root() / "logs" / "daemon"
    d.mkdir(parents=True, exist_ok=True)
    return d / "extraction-daemon.log"


def _discovery_cursor_scan_disabled() -> bool:
    raw = str(os.environ.get("QUAID_DISABLE_DISCOVERY_CURSOR_SCAN", "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _extraction_buffer_log_path() -> Path:
    d = _instance_root() / "logs" / "daemon"
    d.mkdir(parents=True, exist_ok=True)
    return d / "extraction-buffer.log"


def _now_datetime() -> datetime:
    raw = str(os.environ.get("QUAID_NOW", "") or "").strip()
    if raw:
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            logger.warning("Invalid QUAID_NOW=%r; using wall clock for daemon timestamp: %s", raw, exc)
            if _fail_hard_enabled():
                raise RuntimeError(f"Invalid QUAID_NOW={raw!r}") from exc
        else:
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def _extraction_buffer_log_enabled() -> bool:
    try:
        import json as _json

        raw: bool = False
        for _cp in reversed(_config_file_paths()):
            if not _cp.exists():
                continue
            try:
                _data = _json.loads(_cp.read_text(encoding="utf-8"))
            except Exception:
                continue
            _livetest = _data.get("livetest", {})
            if not isinstance(_livetest, dict):
                continue
            _v = _livetest.get("enable_extraction_buffer_log")
            if _v is None:
                _v = _livetest.get("enableExtractionBufferLog")
            if _v is not None:
                raw = bool(_v)
        return raw
    except Exception as exc:
        logger.warning("extraction buffer log config lookup failed; disabling buffer log: %s", exc)
        if _fail_hard_enabled():
            raise RuntimeError("extraction buffer log config lookup failed") from exc
        return False


def _write_extraction_buffer_log(
    session_id: str,
    *,
    phase: str,
    signal_type: str,
    transcript_text: str,
) -> None:
    text = str(transcript_text or "").strip()
    if not text or not _extraction_buffer_log_enabled():
        return
    timestamp = _now_datetime().strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        path = _extraction_buffer_log_path()
        header = (
            f"=== {timestamp} session={session_id} phase={phase} "
            f"signal={signal_type} chars={len(text)} ===\n"
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write(header)
            handle.write(text)
            handle.write("\n\n")
    except Exception as exc:
        logger.warning("failed writing extraction buffer log for %s: %s", session_id, exc)


def _install_state_path() -> Path:
    return _instance_root() / "data" / "installed-at.json"


def _context_refresh_timeout_dir() -> Path:
    d = _instance_root() / "data" / "context-refresh-timeout"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_context_refresh_timeout_marker(session_id: str) -> None:
    """Mark that a timeout lifecycle signal completed for this session.

    Turn-based context refresh consumers (for adapters without compaction hooks)
    read and consume this marker on the next prompt to force a one-time
    system-context refresh after idle timeout extraction.
    """
    sid = _validate_session_id(session_id)
    marker_path = _context_refresh_timeout_dir() / f"{sid}.json"
    payload = {
        "session_id": sid,
        "timeout_completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        _atomic_write(marker_path, json.dumps(payload))
    except OSError as e:
        logger.warning("timeout refresh marker write failed for %s: %s", sid, e)
        if _fail_hard_enabled():
            raise


# ---------------------------------------------------------------------------
# Atomic file writes (B004)
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, content: str) -> None:
    """Write content atomically via temp file + os.replace()."""
    tmp_path = path.with_suffix(f".tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
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
    raw = str(session_id or "").strip()
    if raw and _SESSION_ID_RE.match(raw):
        return raw

    # OpenClaw can emit transcript sidecar IDs like:
    #   <session-id>.checkpoint.<uuid>
    #   <session-id>.reset.<timestamp>
    # Normalize these to the canonical session prefix so timeout extraction
    # stays attributed to the live session.
    if raw:
        for marker in _DISCOVERY_ARTIFACT_MARKERS:
            marker_idx = raw.find(marker)
            if marker_idx > 0:
                prefix = raw[:marker_idx].strip()
                if _SESSION_ID_RE.match(prefix):
                    logger.warning("normalized sidecar session_id %r -> %s", session_id, prefix)
                    return prefix

        uuid_match = _SESSION_ID_UUID_RE.search(raw)
        if uuid_match:
            normalized = uuid_match.group(0)
            if _SESSION_ID_RE.match(normalized):
                logger.warning("normalized uuid-bearing session_id %r -> %s", session_id, normalized)
                return normalized

    # Generate a deterministic fallback so the same malformed ID does not
    # explode into many synthetic unknown-* sessions across idle scans.
    digest_seed = raw or "empty"
    digest = hashlib.blake2b(digest_seed.encode("utf-8", "replace"), digest_size=8).hexdigest()
    safe = f"unknown-{digest}"
    logger.warning("invalid session_id %r, using fallback: %s", session_id, safe)
    return safe


def _session_logs_ingest_error_message(response: Dict[str, Any], row: Optional[Dict[str, Any]] = None) -> str:
    if row:
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        for value in (row.get("error"), result.get("error"), result.get("reason")):
            text = str(value or "").strip()
            if text:
                return text
    for value in (response.get("error"), response.get("status")):
        text = str(value or "").strip()
        if text:
            return text
    return "unknown session_logs ingest request failure"


def _validate_session_logs_ingest_broker_response(response: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(response, dict):
        raise RuntimeError("session_logs ingest request returned a non-object response")
    responses = response.get("responses")
    if not isinstance(responses, list) or len(responses) != 1:
        message = _session_logs_ingest_error_message(response)
        raise RuntimeError(f"session_logs ingest request returned no sessiondb response: {message}")
    row = responses[0]
    if not isinstance(row, dict):
        raise RuntimeError("session_logs ingest request returned malformed sessiondb response")
    if str(row.get("datastore_id") or "").strip() != "sessiondb":
        raise RuntimeError("session_logs ingest request returned a non-sessiondb response")
    result = row.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("session_logs ingest request sessiondb result is not an object")

    response_status = str(response.get("status") or "").strip().lower()
    row_status = str(row.get("status") or result.get("status") or "").strip().lower()
    result_status = str(result.get("status") or "").strip().lower()
    if response_status != "ok" or row_status in {"failed", "error", "nacked"} or result_status in {"failed", "error"}:
        message = _session_logs_ingest_error_message(response, row)
        raise RuntimeError(f"session_logs ingest request failed: {message}")
    return result


def _session_logs_ingest_transcript_path_for_signal(
    transcript_path: Optional[str],
    *,
    session_id: str = "",
    adapter: Any = None,
    signal_meta: Optional[Dict[str, Any]] = None,
    cursor_data: Optional[Dict[str, Any]] = None,
    staged_state: Optional[Dict[str, Any]] = None,
) -> str:
    """Pick the transcript source session_logs should ingest for this signal.

    OpenClaw can switch a session between Quaid's preserved mirror and the
    live host transcript while the daemon is processing a lifecycle signal.
    Extraction may still be able to read the original signal path, but
    session_logs runs through the broker after this function returns; hand it
    the current rolling/live source when one is known and readable.
    """
    current = str(transcript_path or "").strip()
    meta = signal_meta if isinstance(signal_meta, dict) else {}
    cursor = cursor_data if isinstance(cursor_data, dict) else {}
    staged = staged_state if isinstance(staged_state, dict) else {}
    candidates = (
        ("signal_meta_source", meta.get("source_transcript_path")),
        ("signal_meta_buffer", meta.get("buffer_transcript_path")),
        ("rolling_state_source", staged.get("source_transcript_path")),
        ("rolling_state_buffer", staged.get("buffer_transcript_path")),
        ("rolling_state", staged.get("transcript_path")),
        ("cursor", cursor.get("transcript_path")),
        ("current", current),
    )
    seen = set()
    missing_candidates: list[str] = []
    for source, raw_path in candidates:
        path = str(raw_path or "").strip()
        if not path or path in seen:
            continue
        seen.add(path)
        try:
            if os.path.isfile(path):
                return path
            missing_candidates.append(path)
        except OSError as exc:
            logger.warning("session_logs ingest candidate path stat failed from %s: %s", source, exc)
            if _fail_hard_enabled():
                raise RuntimeError(f"session_logs ingest candidate path stat failed from {source}") from exc
    if session_id:
        for path in missing_candidates:
            mirror_path = _preserved_mirror_for_missing_transcript_cursor(session_id, path, adapter=adapter)
            if mirror_path:
                return mirror_path
    return current


def _request_session_logs_ingest(
    *,
    session_id: str,
    owner_id: str,
    label: str,
    session_file: Optional[str] = None,
    transcript_path: Optional[str] = None,
    source_channel: Optional[str] = None,
    conversation_id: Optional[str] = None,
    participant_ids: Optional[List[str]] = None,
    participant_aliases: Optional[Dict[str, str]] = None,
    message_count: int = 0,
    topic_hint: str = "",
) -> Dict[str, Any]:
    from core.plugins.sessiondb_contract import register_session_ingest_log_request_handler
    from core.runtime.events import SESSION_INGEST_LOG_REQUEST_EVENT, request_broker_event

    register_session_ingest_log_request_handler()
    response = request_broker_event(
        SESSION_INGEST_LOG_REQUEST_EVENT,
        {
            "session_id": session_id,
            "owner_id": owner_id,
            "label": label,
            "session_file": session_file,
            "transcript_path": transcript_path,
            "source_channel": source_channel,
            "conversation_id": conversation_id,
            "participant_ids": list(participant_ids or []),
            "participant_aliases": dict(participant_aliases or {}),
            "message_count": int(message_count or 0),
            "topic_hint": topic_hint,
        },
        source="core.extraction_daemon.process_signal",
        session_id=session_id,
        owner_id=owner_id,
        provenance={
            "replacement": "core.extraction_daemon direct run_session_logs_ingest call",
        },
    )
    return _validate_session_logs_ingest_broker_response(response)


def _is_discovery_artifact_transcript(transcript_path: Path) -> bool:
    """Return True when a transcript file is a sidecar artifact, not a live session."""
    try:
        name = str(transcript_path.name or "").lower()
    except Exception:
        return False
    return any(marker in name for marker in _DISCOVERY_ARTIFACT_MARKERS)


# ---------------------------------------------------------------------------
# PID file management (B001: flock for atomicity)
# ---------------------------------------------------------------------------

def _is_daemon_process(pid: int) -> bool:
    """Return True if the given PID is actually running the extraction daemon."""
    try:
        cmdline_path = Path(f"/proc/{pid}/cmdline")
        if cmdline_path.exists():
            cmdline = cmdline_path.read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="replace")
            return "extraction_daemon" in cmdline
        # macOS / BSD: fall back to ps
        import subprocess
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="],
            capture_output=True, text=True, timeout=2,
        )
        return "extraction_daemon" in result.stdout
    except Exception as exc:
        # If we can't verify, assume it's valid to avoid false negatives
        logger.warning("failed to verify daemon process %s; assuming valid: %s", pid, exc)
        if _fail_hard_enabled():
            raise
        return True


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        # Unknown PID probe failures should not cause lock stealing.
        return True


def _all_process_commands_with_env() -> list[tuple[int, str]]:
    """Return (pid, command-with-env) rows for process scanning."""
    commands = [
        # Linux/procps: BSD "-x" requires a BSD personality, but "-e" works
        # and "eww" still includes the full environment needed for ownership.
        ["ps", "eww", "-eo", "pid=,command="],
        # macOS/BSD fallbacks. Darwin accepts the Linux form but can return
        # only caller-session rows, so merge all viable forms instead of
        # trusting the first successful command.
        ["ps", "eww", "-axo", "pid=,command="],
        ["ps", "axeww", "-o", "pid=,command="],
    ]
    rows_by_pid: dict[int, str] = {}
    failures: list[str] = []
    for ps_command in commands:
        try:
            result = subprocess.run(
                ps_command,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except Exception as exc:
            failures.append(f"{' '.join(ps_command)}: {exc}")
            continue
        if result.returncode != 0 or not str(result.stdout or "").strip():
            failures.append(
                f"{' '.join(ps_command)}: rc={result.returncode} stdout_len={len(str(result.stdout or ''))}"
            )
            continue
        rows: list[tuple[int, str]] = []
        for raw_line in StringIO(result.stdout).read().splitlines():
            line = str(raw_line or "").strip()
            if not line:
                continue
            parts = line.split(None, 1)
            try:
                pid = int(parts[0])
            except (IndexError, ValueError):
                continue
            command = parts[1] if len(parts) > 1 else ""
            rows.append((pid, command))
        for pid, command in rows:
            # Prefer the fuller row when forms overlap; env-bearing ps output is
            # usually longer and is required for instance ownership matching.
            if len(command) > len(rows_by_pid.get(pid, "")):
                rows_by_pid[pid] = command
    if not rows_by_pid:
        logger.warning(
            "process ownership ps scan returned no rows after %d variants: %s",
            len(commands),
            "; ".join(failures[-4:]) if failures else "no output",
        )
    return sorted(rows_by_pid.items())


def _command_has_env_value(command: str, key: str, value: str) -> bool:
    """Return true when ps output contains an exact KEY=value env token."""
    if not key or not value:
        return False
    pattern = rf"(?:^|\s){re.escape(key)}={re.escape(value)}(?:\s|$)"
    return re.search(pattern, str(command or "")) is not None


def _command_is_daemon_process(command: str, *, include_foreground: bool = False) -> bool:
    text = f" {str(command or '').strip()} "
    if "extraction_daemon.py" not in text:
        return False
    if " _worker " in text:
        return True
    return bool(include_foreground and " run " in text)


def _matching_daemon_pids(
    *,
    quaid_home: Path | str | None = None,
    instance: str | None = None,
    include_foreground: bool = False,
) -> list[int]:
    """Return live extraction-daemon PIDs for this home+instance."""
    home = str(quaid_home or _quaid_home()).strip()
    instance_id = str(instance or _instance_id()).strip()
    if not home or not instance_id:
        return []
    matches: list[int] = []
    for pid, command in _all_process_commands_with_env():
        if pid <= 0 or pid == os.getpid():
            continue
        if not _command_is_daemon_process(command, include_foreground=include_foreground):
            continue
        if not _command_has_env_value(command, "QUAID_HOME", home):
            continue
        if not _command_has_env_value(command, "QUAID_INSTANCE", instance_id):
            continue
        if _pid_alive(pid):
            matches.append(pid)
    return sorted(set(matches))


def _daemon_pid_matches_current_instance(pid: int) -> bool:
    """Return True when pid belongs to this home+instance daemon.

    A PID file alone is not ownership proof: fresh VM clones can inherit stale
    pid files, and that PID may now belong to a different Quaid instance's live
    daemon. Status/start/stop must never trust a cross-instance daemon just
    because it is an extraction_daemon.py process.
    """
    try:
        return int(pid) in _matching_daemon_pids(include_foreground=True)
    except Exception as exc:
        logger.warning(
            "failed to verify daemon PID %s ownership for home=%s instance=%s: %s",
            pid,
            _quaid_home(),
            _instance_id(),
            exc,
        )
        return False


def _supervisor_alive() -> bool:
    raw = os.environ.get("QUAID_SUPERVISOR_PID", "").strip()
    if not raw:
        return True
    try:
        pid = int(raw)
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def read_pid() -> Optional[int]:
    """Read daemon PID from file. Returns None if not found or stale."""
    pid_file = _pid_path()
    if not pid_file.is_file():
        return None
    pid: Optional[int] = None
    raw_pid: Optional[str] = None
    try:
        raw_pid = pid_file.read_text().strip()
        pid = int(raw_pid)
        # Check if process is alive
        os.kill(pid, 0)
        # Verify it's actually our daemon (PID reuse guard)
        if not _is_daemon_process(pid):
            logger.warning("PID %d in pid file is alive but is not the extraction daemon (PID reused) — treating as stale", pid)
            raise OSError("PID reused by unrelated process")
        if not _daemon_pid_matches_current_instance(pid):
            logger.warning(
                "PID %d in pid file is an extraction daemon but not owned by home=%s instance=%s — treating as stale",
                pid,
                _quaid_home(),
                _instance_id(),
            )
            raise OSError("PID belongs to a different Quaid instance")
        return pid
    except (ValueError, OSError):
        # PID file exists but process is dead or stale
        if pid is not None:
            _remove_pid_if_matches(pid)
        elif raw_pid is not None:
            _remove_pid_if_text_matches(raw_pid)
        return None


def write_pid(pid: int) -> None:
    _pid_path().parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(_pid_path(), str(pid))


def remove_pid() -> None:
    try:
        _pid_path().unlink()
    except OSError:
        pass


def _remove_pid_if_matches(expected_pid: int) -> None:
    pid_file = _pid_path()
    try:
        current = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return
    if current != int(expected_pid):
        return
    try:
        pid_file.unlink()
    except OSError:
        pass


def _remove_pid_if_text_matches(expected_text: str) -> None:
    pid_file = _pid_path()
    try:
        current = pid_file.read_text().strip()
    except OSError:
        return
    if current != str(expected_text).strip():
        return
    try:
        pid_file.unlink()
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
    *,
    dedupe: bool = True,
) -> Path:
    """Write an extraction signal file for the daemon to process.

    Called by adapter hooks (CC, OC) when extraction should happen.
    Returns the path to the signal file.
    """
    # B062: Validate signal type
    if signal_type not in VALID_SIGNAL_TYPES:
        if _fail_hard_enabled():
            raise ValueError(f"unknown signal type {signal_type!r}")
        logger.warning("unknown signal type %r, defaulting to session_end", signal_type)
        signal_type = "session_end"

    # B008: Validate session_id
    session_id = _validate_session_id(session_id)
    incoming_meta = meta or {}

    sig_dir = _signal_dir()
    lock_fd = _acquire_signal_write_lock(session_id) if dedupe else None
    try:
        existing_path = None
        existing_payload: Optional[Dict[str, Any]] = None
        if dedupe:
            for f in sorted(sig_dir.iterdir()):
                if not f.name.endswith(".json"):
                    continue
                try:
                    existing = json.loads(f.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                if _validate_session_id(existing.get("session_id", "")) != session_id:
                    continue
                existing_type = str(existing.get("type", "") or existing.get("signal_type", "")).strip()
                if existing_type != signal_type:
                    continue
                existing_meta = existing.get("meta", {}) if isinstance(existing.get("meta", {}), dict) else {}
                if not _signal_dedupe_compatible(
                    existing_type=existing_type,
                    existing_meta=existing_meta,
                    new_type=signal_type,
                    new_meta=incoming_meta,
                ):
                    continue
                existing_path = f
                existing_payload = existing if isinstance(existing, dict) else None
                break

        payload = {
            "type": signal_type,
            "session_id": session_id,
            "transcript_path": transcript_path,
            "adapter": adapter,
            "supports_compaction_control": supports_compaction_control,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "meta": incoming_meta,
        }
        if existing_path is not None and existing_payload is not None and _signal_path_exists(existing_path):
            merged_meta = dict(existing_payload.get("meta", {}) or {})
            merged_meta.update(meta or {})
            payload["meta"] = merged_meta
            _atomic_write(existing_path, json.dumps(payload))
            return existing_path

        # B047: Use UUID suffix for uniqueness (avoids ms-level collision)
        fname = f"{int(time.time() * 1000)}_{os.getpid()}_{uuid.uuid4().hex[:8]}_{signal_type}.json"
        sig_path = sig_dir / fname
        _atomic_write(sig_path, json.dumps(payload))
        return sig_path
    finally:
        _release_signal_write_lock(lock_fd)


def _is_staged_payload_flush_signal_meta(meta: Dict[str, Any]) -> bool:
    """Return true for synthetic signals that only publish staged rolling payload."""
    if not isinstance(meta, dict):
        return False
    return bool(
        str(meta.get("reason") or "") == "rolling_stage_flush"
        or meta.get("staged_payload_sweep")
        or meta.get("flush_staged_payload_only")
    )


def _is_late_post_reset_content_signal(signal_type: str, meta: Dict[str, Any]) -> bool:
    """Return true for OC's active-session post-reset transcript growth signal."""
    if signal_type != "session_end" or not isinstance(meta, dict):
        return False
    return str(meta.get("reason") or "").strip() == "late_post_reset_content"


def _rolling_flush_processing_signal_type(signal_type: str, staged_payload_sweep_signal: bool) -> str:
    if staged_payload_sweep_signal:
        return "rolling_flush"
    return signal_type


def _signal_dedupe_compatible(
    *,
    existing_type: str,
    existing_meta: Dict[str, Any],
    new_type: str,
    new_meta: Dict[str, Any],
) -> bool:
    if existing_type != new_type:
        return False
    # A synthetic rolling-stage flush is not equivalent to a real lifecycle
    # signal. Merging them rewrites the real lifecycle into "flush staged
    # payload only" and strands the residual transcript tail until a later scan.
    return _is_staged_payload_flush_signal_meta(existing_meta) == _is_staged_payload_flush_signal_meta(new_meta)


def _pending_signal_sort_key(signal_data: Dict[str, Any]) -> Tuple[int, str]:
    signal_type = str(signal_data.get("type") or signal_data.get("signal_type") or "")
    signal_path = str(signal_data.get("_signal_path") or "")
    meta = signal_data.get("meta") if isinstance(signal_data.get("meta"), dict) else {}
    if signal_type == "session_end" and meta.get("reason") == "rolling_stage_flush":
        return (-1, signal_path)
    return (_SIGNAL_POLL_PRIORITY.get(signal_type, 99), signal_path)


def read_pending_signals() -> List[Dict[str, Any]]:
    """Read pending signal files, prioritizing lifecycle flushes before noisy fallback work."""
    sig_dir = _signal_dir()
    if not sig_dir.is_dir():
        return []

    signals = []
    for f in sorted(sig_dir.iterdir()):
        if not f.name.endswith(".json"):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if "type" not in data and "signal_type" in data:
                data["type"] = data.get("signal_type")
            data["_signal_path"] = str(f)
            signals.append(data)
        except json.JSONDecodeError as exc:
            if _fail_hard_enabled():
                logger.critical("invalid pending signal JSON %s while failHard is enabled", f)
                raise
            # Remove structurally corrupt signal files; atomic signal writes make
            # ordinary read failures transient and retryable.
            logger.warning("removing invalid pending signal JSON %s: %s", f, exc)
            try:
                f.unlink()
            except OSError as unlink_exc:
                logger.warning("failed removing invalid pending signal JSON %s: %s", f, unlink_exc)
        except OSError as exc:
            logger.warning("failed reading pending signal %s; preserving for retry: %s", f, exc)
    signals.sort(key=_pending_signal_sort_key)
    return signals[:MAX_SIGNALS_PER_POLL]


def _signal_file_was_consumed(signal_data: Dict[str, Any]) -> bool:
    """Return True when a disk-backed signal was deleted after an in-memory read."""
    sig_path_raw = str(signal_data.get("_signal_path") or "").strip()
    if not sig_path_raw:
        return False
    sig_path = Path(sig_path_raw)
    try:
        signal_root = _signal_dir().resolve()
        if not sig_path.resolve(strict=False).is_relative_to(signal_root):
            return False
        sig_path.stat()
    except FileNotFoundError:
        logger.info("skipping stale in-memory signal whose file was already consumed: %s", sig_path)
        return True
    except OSError as exc:
        logger.warning(
            "failed checking pending signal file %s; processing in-memory signal: %s",
            sig_path,
            exc,
        )
    return False


def _pending_signal_count() -> int:
    sig_dir = _signal_dir()
    if not sig_dir.is_dir():
        return 0
    return sum(1 for f in sig_dir.iterdir() if f.name.endswith(".json"))


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
    except OSError as exc:
        if _fail_hard_enabled():
            raise RuntimeError(f"failed removing processed signal {sig}") from exc
        logger.warning("failed removing processed signal %s; preserving for retry: %s", sig, exc)


def _preserve_missing_transcript_signal_for_retry(
    signal_data: Dict[str, Any],
    *,
    session_id: str,
    signal_type: str,
    transcript_path: str,
    label: str,
    max_wait_seconds: float = 45.0,
) -> bool:
    """Keep a lifecycle signal on disk briefly when transcript flush lags hook fire."""
    sig_path = str(signal_data.get("_signal_path") or "").strip()
    if not sig_path:
        return False
    try:
        first_seen_default = float(time.time())
        existing_meta = dict(signal_data.get("meta") or {})
        raw_first_seen = existing_meta.get("missing_transcript_first_seen_at")
        if raw_first_seen is None or raw_first_seen == "":
            raw_first_seen = first_seen_default
        first_seen = float(raw_first_seen)
        attempts = int(existing_meta.get("missing_transcript_attempts", 0) or 0) + 1
        age_seconds = max(0.0, float(time.time()) - first_seen)
        if age_seconds > float(max_wait_seconds):
            return False
        existing_meta.update({
            "missing_transcript_first_seen_at": first_seen,
            "missing_transcript_last_seen_at": float(time.time()),
            "missing_transcript_attempts": attempts,
            "missing_transcript_last_path": str(transcript_path or ""),
        })
        payload = dict(signal_data)
        payload.pop("_signal_path", None)
        payload["meta"] = existing_meta
        _atomic_write(Path(sig_path), json.dumps(payload))
        logger.info(
            "[%s] session %s: transcript missing for %s; preserving signal for retry "
            "(attempt=%d age=%.1fs path=%s)",
            label,
            session_id,
            signal_type,
            attempts,
            age_seconds,
            str(transcript_path or ""),
        )
        return True
    except Exception as exc:
        logger.warning(
            "[%s] session %s: could not preserve missing-transcript signal for retry: %s",
            label,
            session_id,
            exc,
        )
        if _fail_hard_enabled():
            raise RuntimeError("could not preserve missing-transcript signal for retry") from exc
        return False


def _record_signal_process_failure_for_retry(
    signal_data: Dict[str, Any],
    exc: BaseException,
    *,
    label: str,
) -> bool:
    """Persist failed process attempts and dead-letter signals that keep crashing."""
    sig_path_raw = str(signal_data.get("_signal_path") or "").strip()
    session_id = str(signal_data.get("session_id") or "").strip()
    signal_type = str(signal_data.get("type") or signal_data.get("signal_type") or "").strip()
    if not sig_path_raw:
        logger.warning(
            "[%s] failed processing %s signal for %s with no signal file; cannot update retry attempts: %s",
            label,
            signal_type or "unknown",
            session_id or "unknown",
            exc,
        )
        return False

    sig_path = Path(sig_path_raw)
    try:
        signal_root = _signal_dir().resolve()
        if not sig_path.resolve(strict=False).is_relative_to(signal_root):
            logger.warning(
                "[%s] refusing to update failed signal outside signal dir: %s",
                label,
                sig_path,
            )
            return False

        try:
            attempts = int(signal_data.get("process_attempts") or 0) + 1
        except (TypeError, ValueError):
            attempts = 1
        payload = dict(signal_data)
        payload.pop("_signal_path", None)
        payload["process_attempts"] = attempts
        payload["last_process_failure"] = str(exc)
        payload["last_process_failure_at"] = _now_datetime().isoformat().replace("+00:00", "Z")

        if attempts >= int(MAX_SIGNAL_PROCESS_ATTEMPTS):
            payload["dead_lettered_at"] = _now_datetime().isoformat().replace("+00:00", "Z")
            payload["dead_letter_reason"] = "process_attempts_exhausted"
        _atomic_write(sig_path, json.dumps(payload))

        if attempts >= int(MAX_SIGNAL_PROCESS_ATTEMPTS):
            dead_letter_path = _signal_dead_letter_dir() / sig_path.name
            if dead_letter_path.exists():
                dead_letter_path = dead_letter_path.with_name(
                    f"{dead_letter_path.stem}.{uuid.uuid4().hex[:8]}{dead_letter_path.suffix}"
                )
            os.replace(str(sig_path), str(dead_letter_path))
            logger.error(
                "[%s] signal processing failed %d times; moved to dead letter: %s",
                label,
                attempts,
                dead_letter_path,
            )
            return False

        logger.warning(
            "[%s] signal processing failed; preserving for retry "
            "(attempt=%d/%d session=%s type=%s): %s",
            label,
            attempts,
            MAX_SIGNAL_PROCESS_ATTEMPTS,
            session_id or "unknown",
            signal_type or "unknown",
            exc,
        )
        return True
    except Exception as record_exc:
        logger.warning(
            "[%s] could not update failed signal retry attempts for %s: %s",
            label,
            sig_path,
            record_exc,
        )
        if _fail_hard_enabled():
            raise RuntimeError("could not update failed signal retry attempts") from record_exc
        return False


def _finalize_no_payload_signal(
    *,
    session_id: str,
    transcript_path: str,
    signal_data: Dict[str, Any],
    lock_owner_key: str,
    lock_fd: int,
    cursor_key: Optional[str] = None,
    next_cursor_offset: Optional[int] = None,
    clear_state: bool = False,
    emit_noop_metric: Optional[Callable[[], None]] = None,
    release_lock: bool = True,
) -> None:
    """Finalize a no-payload branch consistently across short-circuit paths."""
    signal_type = str(
        signal_data.get("type") or signal_data.get("signal_type") or ""
    ).strip().lower()
    _record_daemon_lifecycle_observation(
        signal_data,
        session_id=session_id,
        signal_type=signal_type,
        transcript_path=transcript_path,
    )
    if signal_type == "timeout":
        write_context_refresh_timeout_marker(session_id)
    if next_cursor_offset is not None:
        write_cursor(
            session_id,
            int(next_cursor_offset),
            transcript_path,
            source_key=cursor_key,
            processed_signal_type=signal_type,
        )
    if clear_state:
        clear_rolling_state(session_id)
    if callable(emit_noop_metric):
        emit_noop_metric()
    mark_signal_processed(signal_data)
    if release_lock:
        _release_session_processing_lock(lock_owner_key, lock_fd)


def _daemon_lifecycle_event_id(
    signal_data: Dict[str, Any],
    *,
    signal_type: str,
    session_id: str,
) -> str:
    signal_file = _daemon_lifecycle_signal_file(signal_data)
    return f"daemon-signal:{signal_file}:{signal_type}:{session_id}"


def _daemon_lifecycle_signal_file(signal_data: Dict[str, Any]) -> str:
    signal_path = str(signal_data.get("_signal_path") or "").strip()
    signal_file = Path(signal_path).name if signal_path else ""
    if not signal_file:
        signal_file = str(
            signal_data.get("id")
            or signal_data.get("idempotency_key")
            or signal_data.get("timestamp")
            or "inline"
        ).strip() or "inline"
    return signal_file


def _record_daemon_lifecycle_observation(
    signal_data: Dict[str, Any],
    *,
    session_id: str,
    signal_type: str,
    transcript_path: str,
) -> Dict[str, Any]:
    event_name = DAEMON_SIGNAL_TO_LIFECYCLE_EVENT.get(signal_type)
    sid = str(session_id or "").strip()
    if not event_name or not sid:
        return {"status": "skipped", "persisted": False}

    signal_path = str(signal_data.get("_signal_path") or "").strip()
    signal_file = _daemon_lifecycle_signal_file(signal_data)
    meta = signal_data.get("meta") if isinstance(signal_data.get("meta"), dict) else {}
    event_id = _daemon_lifecycle_event_id(
        signal_data,
        signal_type=signal_type,
        session_id=sid,
    )
    source = f"daemon.{signal_type}"
    payload = {
        "session_id": sid,
        "reason": str(meta.get("reason") or meta.get("source") or signal_type),
        "daemon_signal_type": signal_type,
        "transcript_path": str(transcript_path or signal_data.get("transcript_path") or ""),
        "signal_file": signal_file,
        "signal_path": signal_path,
        "adapter": str(signal_data.get("adapter") or ""),
        "meta": meta,
    }
    event = {
        "id": event_id,
        "event_id": event_id,
        "idempotency_key": event_id,
        "name": event_name,
        "event_type": event_name,
        "source": source,
        "session_id": sid,
        "created_at": str(signal_data.get("timestamp") or "").strip(),
        "payload": payload,
        "provenance": {
            "origin": "daemon_signal",
            "signal_type": signal_type,
            "signal_file": signal_file,
            "signal_path": signal_path,
        },
    }
    try:
        from core.plugins.sessiondb_contract import record_session_lifecycle_observation

        return record_session_lifecycle_observation(event)
    except Exception as exc:
        if _fail_hard_enabled():
            raise RuntimeError(
                "SessionDB daemon lifecycle observation failed while failHard is enabled"
            ) from exc
        logger.warning(
            "SessionDB daemon lifecycle observation persistence failed: %s",
            exc,
            exc_info=True,
        )
        return {"status": "failed", "persisted": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Cursors
# ---------------------------------------------------------------------------

def _cursor_storage_key(session_id: str, source_key: Optional[str] = None) -> str:
    """Return the on-disk cursor key for this session/source."""
    sid = _validate_session_id(session_id)
    raw = str(source_key or "").strip()
    if not raw:
        return sid
    if _SESSION_ID_RE.match(raw):
        return raw
    digest = hashlib.blake2b(raw.encode("utf-8", "replace"), digest_size=16).hexdigest()
    return f"source-{digest}"


def _read_cursor_file(cursor_file: Path, fallback_session_id: str) -> Dict[str, Any]:
    defaults = {
        "session_id": fallback_session_id,
        "line_offset": 0,
        "transcript_path": "",
        "internal": False,
        "transcript_size_bytes": 0,
        "transcript_mtime_ns": 0,
        "transcript_inode": 0,
        "transcript_device": 0,
        "transcript_guard_start_line": 0,
        "transcript_guard_line_count": 0,
        "transcript_guard_digest": "",
        "processed_signal_type": "",
        "last_flushed_line_offset": 0,
        "cursor_key": cursor_file.stem,
    }
    if not cursor_file.is_file():
        return defaults
    try:
        data = json.loads(cursor_file.read_text(encoding="utf-8"))
        cursor_key = str(data.get("cursor_key") or cursor_file.stem or "").strip()
        if not _SESSION_ID_RE.match(cursor_key):
            cursor_key = _cursor_storage_key(fallback_session_id, cursor_key)
        line_offset = int(data.get("line_offset", 0))
        processed_signal_type = str(data.get("processed_signal_type") or "")
        if "last_flushed_line_offset" in data:
            last_flushed_line_offset = int(data.get("last_flushed_line_offset", 0) or 0)
        elif processed_signal_type or bool(data.get("internal", False)):
            last_flushed_line_offset = line_offset
        else:
            last_flushed_line_offset = 0
        return {
            "session_id": str(data.get("session_id") or fallback_session_id),
            "line_offset": line_offset,
            "transcript_path": data.get("transcript_path", ""),
            "internal": bool(data.get("internal", False)),
            "transcript_size_bytes": int(data.get("transcript_size_bytes", 0) or 0),
            "transcript_mtime_ns": int(data.get("transcript_mtime_ns", 0) or 0),
            "transcript_inode": int(data.get("transcript_inode", 0) or 0),
            "transcript_device": int(data.get("transcript_device", 0) or 0),
            "transcript_guard_start_line": int(data.get("transcript_guard_start_line", 0) or 0),
            "transcript_guard_line_count": int(data.get("transcript_guard_line_count", 0) or 0),
            "transcript_guard_digest": str(data.get("transcript_guard_digest") or ""),
            "processed_signal_type": processed_signal_type,
            "last_flushed_line_offset": max(0, min(last_flushed_line_offset, line_offset)),
            "cursor_key": cursor_key or cursor_file.stem,
        }
    except (json.JSONDecodeError, ValueError, OSError):
        return defaults


def _find_cursor_file_for_session(session_id: str) -> Optional[Path]:
    """Find the newest cursor file that belongs to this session id."""
    cursor_dir = _cursor_dir()
    newest_path: Optional[Path] = None
    newest_mtime: float = -1.0
    for candidate in cursor_dir.glob("*.json"):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if str(data.get("session_id") or "").strip() != session_id:
            continue
        try:
            mtime = candidate.stat().st_mtime
        except OSError:
            mtime = 0.0
        if mtime >= newest_mtime:
            newest_path = candidate
            newest_mtime = mtime
    return newest_path


def read_cursor(session_id: str, *, source_key: Optional[str] = None) -> Dict[str, Any]:
    """Read extraction cursor for a session. Returns dict with line_offset and transcript_path."""
    session_id = _validate_session_id(session_id)
    resolved_key = _cursor_storage_key(session_id, source_key)
    cursor_dir = _cursor_dir()
    preferred_path = cursor_dir / f"{resolved_key}.json"
    state = _read_cursor_file(preferred_path, session_id)
    if preferred_path.is_file():
        return state
    # Backward-compat alias: fall back to legacy per-session cursor filename.
    legacy_path = cursor_dir / f"{session_id}.json"
    if legacy_path == preferred_path:
        discovered = _find_cursor_file_for_session(session_id)
        if discovered and discovered != preferred_path:
            return _read_cursor_file(discovered, session_id)
        return state
    if legacy_path.is_file():
        return _read_cursor_file(legacy_path, session_id)
    discovered = _find_cursor_file_for_session(session_id)
    if discovered:
        return _read_cursor_file(discovered, session_id)
    return state


def _transcript_size_bytes(transcript_path: str) -> int:
    return int(_transcript_stat_metadata(transcript_path).get("size_bytes", 0) or 0)


def _transcript_stat_metadata(transcript_path: str) -> Dict[str, int]:
    try:
        st = os.stat(transcript_path)
        return {
            "size_bytes": int(st.st_size),
            "mtime_ns": int(getattr(st, "st_mtime_ns", 0) or 0),
            "inode": int(getattr(st, "st_ino", 0) or 0),
            "device": int(getattr(st, "st_dev", 0) or 0),
        }
    except OSError as exc:
        if _should_raise_transcript_stat_error(transcript_path, exc):
            raise
        return {"size_bytes": 0, "mtime_ns": 0, "inode": 0, "device": 0}


def _transcript_line_window_digest(transcript_path: str, start_line: int, line_count: int) -> Dict[str, Any]:
    """Hash the unread transcript line window for rebase detection."""
    start = max(0, int(start_line or 0))
    count = max(0, int(line_count or 0))
    digest = hashlib.blake2b(digest_size=16)
    lines_read = 0
    if count <= 0:
        return {"start_line": start, "line_count": 0, "digest": ""}
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i < start:
                    continue
                if lines_read >= count:
                    break
                digest.update(line.encode("utf-8", "replace"))
                lines_read += 1
    except OSError as exc:
        if _should_raise_transcript_stat_error(transcript_path, exc):
            raise
    return {
        "start_line": start,
        "line_count": lines_read,
        "digest": digest.hexdigest() if lines_read else "",
    }


_CURSOR_REBASE_GUARD_LINES = 3


def _cursor_rebase_guard_digest(transcript_path: str, line_offset: int) -> Dict[str, Any]:
    """Digest the processed line boundary used to validate same-path cursor reuse."""
    offset = max(0, int(line_offset or 0))
    if offset <= 0:
        return {"start_line": 0, "line_count": 0, "digest": ""}
    line_count = min(_CURSOR_REBASE_GUARD_LINES, offset)
    start_line = max(0, offset - line_count)
    return _transcript_line_window_digest(transcript_path, start_line, line_count)


def _cursor_rebase_guard_matches(cursor_data: Dict[str, Any], transcript_path: str) -> bool:
    """Return False when a cursor's processed boundary no longer matches the file."""
    expected_digest = str(cursor_data.get("transcript_guard_digest") or "")
    expected_count = int(cursor_data.get("transcript_guard_line_count", 0) or 0)
    if not expected_digest or expected_count <= 0:
        return True
    expected = {
        "start_line": int(cursor_data.get("transcript_guard_start_line", 0) or 0),
        "line_count": expected_count,
        "digest": expected_digest,
    }
    current = _transcript_line_window_digest(
        transcript_path,
        expected["start_line"],
        expected["line_count"],
    )
    return current == expected


def _should_raise_transcript_stat_error(transcript_path: str, exc: OSError) -> bool:
    if not str(transcript_path or "").strip():
        return False
    if isinstance(exc, FileNotFoundError):
        return False
    return _fail_hard_enabled()


def _transcript_rebase_retry_count(meta: Any) -> int:
    if not isinstance(meta, dict):
        return 0
    try:
        return max(0, int(meta.get("transcript_rebase_retry_count") or 0))
    except (TypeError, ValueError):
        return 0


def _fail_hard_enabled() -> bool:
    try:
        from lib.fail_policy import is_fail_hard_enabled

        return bool(is_fail_hard_enabled())
    except ImportError:
        return True


def _is_daemon_rolling_transcript_snapshot_location(transcript_path: str) -> bool:
    raw = str(transcript_path or "").strip()
    if not raw:
        return False
    try:
        path = Path(raw).expanduser().resolve(strict=False)
        root = (_instance_root() / "logs" / "daemon" / "rolling-transcript-snapshots").resolve()
        return path.is_relative_to(root)
    except OSError as exc:
        if _should_raise_transcript_stat_error(raw, exc):
            raise
        return False


def _is_daemon_rolling_transcript_snapshot_path(transcript_path: str) -> bool:
    raw = str(transcript_path or "").strip()
    if not raw:
        return False
    return Path(raw).expanduser().is_file() and _is_daemon_rolling_transcript_snapshot_location(raw)


def _is_daemon_preserved_session_transcript_path(transcript_path: str) -> bool:
    raw = str(transcript_path or "").strip()
    if not raw:
        return False
    try:
        path = Path(raw).expanduser().resolve()
        # OpenClaw mirrors authoritative transcript copies here before reset /
        # compaction lifecycle signals. These files are instance-local daemon
        # inputs, not active host transcripts for idle timeout discovery.
        root = (_instance_root() / "logs" / "quaid" / "sessions").resolve()
        return path.is_file() and path.is_relative_to(root)
    except OSError as exc:
        if _should_raise_transcript_stat_error(raw, exc):
            raise
        return False


def _is_daemon_owned_transcript_snapshot_path(transcript_path: str) -> bool:
    return (
        _is_daemon_rolling_transcript_snapshot_path(transcript_path)
        or _is_daemon_preserved_session_transcript_path(transcript_path)
    )


def _stable_transcript_snapshot_for_continued_rolling(
    session_id: str,
    transcript_path: str,
) -> str:
    """Copy a rolling transcript before queuing a later tail signal.

    Some hosts reuse the active transcript path immediately after reset/new-session
    transitions. A continued rolling signal must read the tail from the file as it
    existed when the rolling stage split it, not whatever the host writes later at
    the same path.
    """
    raw = str(transcript_path or "").strip()
    if not raw or not os.path.isfile(raw):
        return raw
    tmp: Optional[Path] = None
    try:
        source = Path(raw).expanduser().resolve()
        source_stat = source.stat()
        source_digest = hashlib.blake2b(
            f"{source}:{source_stat.st_mtime_ns}:{source_stat.st_size}".encode(
                "utf-8",
                "replace",
            ),
            digest_size=8,
        ).hexdigest()
        safe_session = re.sub(r"[^A-Za-z0-9_.-]", "_", _validate_session_id(session_id))
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        # Keep the original basename so cursor path-change handling treats this
        # as a same-source relocation instead of a fresh transcript.
        dest_dir = _rolling_transcript_snapshot_dir() / safe_session / f"{stamp}-{source_digest}"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / source.name
        tmp = dest.with_name(f".{dest.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        tmp.write_bytes(source.read_bytes())
        os.replace(tmp, dest)
        return str(dest)
    except OSError as exc:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass
        if _fail_hard_enabled():
            raise
        logger.warning(
            "failed to snapshot rolling transcript for session %s (%s): %s",
            session_id,
            raw,
            exc,
        )
        return raw


def _cleanup_daemon_transcript_snapshot_path(transcript_path: str) -> None:
    raw = str(transcript_path or "").strip()
    if not raw or not _is_daemon_rolling_transcript_snapshot_path(raw):
        return
    try:
        path = Path(raw).expanduser().resolve()
        root = (_instance_root() / "logs" / "daemon" / "rolling-transcript-snapshots").resolve()
        path.unlink()
        parent = path.parent
        while parent != root and parent.is_relative_to(root):
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    except OSError as exc:
        logger.debug("failed to clean rolling transcript snapshot %s: %s", raw, exc)


def _cleanup_rolling_state_snapshot_paths(state: Any) -> None:
    if not isinstance(state, dict):
        return
    seen: set[str] = set()
    for key in ("transcript_path", "buffer_transcript_path"):
        raw = str(state.get(key) or "").strip()
        if not raw or raw in seen:
            continue
        seen.add(raw)
        _cleanup_daemon_transcript_snapshot_path(raw)


def _pending_signal_references_transcript_path(transcript_path: str) -> bool:
    raw = str(transcript_path or "").strip()
    if not raw:
        return False
    try:
        target = str(Path(raw).expanduser().resolve(strict=False))
    except OSError:
        target = raw

    def _same_path(candidate: Any) -> bool:
        candidate_raw = str(candidate or "").strip()
        if not candidate_raw:
            return False
        if candidate_raw == raw:
            return True
        try:
            return str(Path(candidate_raw).expanduser().resolve(strict=False)) == target
        except OSError:
            return False

    try:
        for signal_file in _signal_dir().glob("*.json"):
            try:
                payload = json.loads(signal_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(payload, dict):
                continue
            if _same_path(payload.get("transcript_path")):
                return True
            meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
            if _same_path(meta.get("source_transcript_path")) or _same_path(meta.get("buffer_transcript_path")):
                return True
    except OSError as exc:
        logger.debug("failed scanning pending signals before snapshot cleanup %s: %s", raw, exc)
        return True
    return False


def write_cursor(
    session_id: str,
    line_offset: int,
    transcript_path: str,
    *,
    internal: bool = False,
    source_key: Optional[str] = None,
    processed_signal_type: str = "",
    last_flushed_line_offset: Optional[int] = None,
) -> None:
    """Write extraction cursor after processing."""
    session_id = _validate_session_id(session_id)
    cursor_key = _cursor_storage_key(session_id, source_key)
    cursor_dir = _cursor_dir()
    cursor_file = cursor_dir / f"{cursor_key}.json"
    current_stat = _transcript_stat_metadata(transcript_path)
    current_size_bytes = int(current_stat.get("size_bytes", 0) or 0)
    existing_last_flushed_line_offset = 0
    if cursor_file.is_file():
        existing = _read_cursor_file(cursor_file, session_id)
        existing_offset = int(existing.get("line_offset", 0) or 0)
        existing_last_flushed_line_offset = int(existing.get("last_flushed_line_offset", 0) or 0)
        existing_path = str(existing.get("transcript_path") or "").strip()
        same_source = bool(
            existing_path
            and transcript_path
            and _canonicalize_transcript_source_path(existing_path)
            == _canonicalize_transcript_source_path(transcript_path)
        )
        existing_size_bytes = int(existing.get("transcript_size_bytes", 0) or 0)
        source_shrank = bool(
            existing_size_bytes
            and current_size_bytes < existing_size_bytes
        )
        # Internal cursors mark content that was skipped rather than extracted.
        # If that file later grows, a lower offset may be the correct recovery
        # path so the newly meaningful transcript can be re-buffered in full.
        source_grew_after_internal_skip = bool(
            existing.get("internal")
            and current_size_bytes > existing_size_bytes
        )
        same_size_rebased = False
        if existing_size_bytes and current_size_bytes == existing_size_bytes:
            current_mtime_ns = int(current_stat.get("mtime_ns", 0) or 0)
            current_inode = int(current_stat.get("inode", 0) or 0)
            current_device = int(current_stat.get("device", 0) or 0)
            existing_mtime_ns = int(existing.get("transcript_mtime_ns", 0) or 0)
            existing_inode = int(existing.get("transcript_inode", 0) or 0)
            existing_device = int(existing.get("transcript_device", 0) or 0)
            # Zero-valued existing fields mean a legacy cursor; fall back to
            # size-only behavior instead of treating missing metadata as a rebase.
            # Device is mostly belt-and-suspenders for unusual same-path moves.
            same_size_rebased = bool(
                (existing_mtime_ns and current_mtime_ns and current_mtime_ns != existing_mtime_ns)
                or (existing_inode and current_inode and current_inode != existing_inode)
                or (existing_device and current_device and current_device != existing_device)
            )
        source_rebased = source_shrank or same_size_rebased or source_grew_after_internal_skip
        if same_source and int(line_offset) < existing_offset and not source_rebased:
            logger.warning(
                "refusing to rewind cursor %s for unchanged transcript source "
                "(requested=%d existing=%d path=%s)",
                cursor_key,
                int(line_offset),
                existing_offset,
                transcript_path,
            )
            line_offset = existing_offset
    processed_signal_type = str(processed_signal_type or "").strip()
    if last_flushed_line_offset is None:
        if processed_signal_type or internal:
            resolved_last_flushed_line_offset = int(line_offset or 0)
        elif cursor_file.is_file():
            resolved_last_flushed_line_offset = existing_last_flushed_line_offset
        else:
            resolved_last_flushed_line_offset = int(line_offset or 0)
    else:
        resolved_last_flushed_line_offset = int(last_flushed_line_offset or 0)
    resolved_last_flushed_line_offset = max(
        0,
        min(int(resolved_last_flushed_line_offset or 0), int(line_offset or 0)),
    )
    guard_digest = _cursor_rebase_guard_digest(transcript_path, int(line_offset or 0))
    payload = {
        "session_id": session_id,
        "cursor_key": cursor_key,
        "line_offset": line_offset,
        "transcript_path": transcript_path,
        "internal": bool(internal),
        "transcript_size_bytes": current_size_bytes,
        "transcript_mtime_ns": int(current_stat.get("mtime_ns", 0) or 0),
        "transcript_inode": int(current_stat.get("inode", 0) or 0),
        "transcript_device": int(current_stat.get("device", 0) or 0),
        "transcript_guard_start_line": int(guard_digest.get("start_line", 0) or 0),
        "transcript_guard_line_count": int(guard_digest.get("line_count", 0) or 0),
        "transcript_guard_digest": str(guard_digest.get("digest") or ""),
        "last_flushed_line_offset": resolved_last_flushed_line_offset,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if processed_signal_type:
        payload["processed_signal_type"] = processed_signal_type
    try:
        _atomic_write(cursor_file, json.dumps(payload))
    except OSError as e:
        if _fail_hard_enabled():
            raise
        logger.error("cursor write failed for %s: %s", session_id, e)
        return
    # Migration cleanup: when writing source-keyed cursors, retire stale legacy
    # per-session cursor file to avoid duplicate timeout scans.
    legacy_file = cursor_dir / f"{session_id}.json"
    if legacy_file != cursor_file:
        try:
            if legacy_file.exists():
                legacy_file.unlink()
        except OSError:
            pass


def _canonicalize_transcript_source_path(transcript_path: str) -> str:
    """Normalize transcript path variants that represent the same source."""
    raw = str(transcript_path or "").strip()
    if not raw:
        return ""
    expanded = os.path.abspath(os.path.expanduser(raw))
    lower = expanded.lower()
    for marker in (".jsonl.reset.", ".checkpoint."):
        idx = lower.find(marker)
        if idx > 0:
            expanded = expanded[:idx] + ".jsonl"
            break
    return expanded


def _signal_source_identity(
    session_id: str,
    transcript_path: str,
    *,
    cursor_data: Optional[Dict[str, Any]] = None,
    staged_state: Optional[Dict[str, Any]] = None,
) -> str:
    """Return a stable source identity string used for lock/cursor keying."""
    candidates: List[str] = [str(transcript_path or "").strip()]
    if isinstance(cursor_data, dict):
        candidates.append(str(cursor_data.get("transcript_path") or "").strip())
    if isinstance(staged_state, dict):
        candidates.append(str(staged_state.get("transcript_path") or "").strip())

    for candidate in candidates:
        if not candidate:
            continue
        name = os.path.basename(candidate)
        uuid_match = _SESSION_ID_UUID_RE.search(name)
        if uuid_match:
            return f"uuid:{uuid_match.group(0).lower()}"
        canonical_path = _canonicalize_transcript_source_path(candidate)
        if canonical_path:
            return f"path:{canonical_path}"
    return f"session:{_validate_session_id(session_id)}"


def _signal_source_cursor_key(
    session_id: str,
    transcript_path: str,
    *,
    cursor_data: Optional[Dict[str, Any]] = None,
    staged_state: Optional[Dict[str, Any]] = None,
) -> str:
    identity = _signal_source_identity(
        session_id,
        transcript_path,
        cursor_data=cursor_data,
        staged_state=staged_state,
    )
    digest = hashlib.blake2b(identity.encode("utf-8", "replace"), digest_size=16).hexdigest()
    return f"source-{digest}"


def _signal_meta_cursor_key(session_id: str, meta: Any) -> str:
    if not isinstance(meta, dict):
        return ""
    raw = str(meta.get("source_cursor_key") or "").strip()
    if not raw:
        return ""
    return _cursor_storage_key(session_id, raw)


def _read_cursor_with_source_compat(session_id: str, source_key: Optional[str]) -> Dict[str, Any]:
    """Read a source-aware cursor while tolerating patched one-arg test doubles."""
    if source_key:
        try:
            return read_cursor(session_id, source_key=source_key)
        except TypeError:
            pass
    return read_cursor(session_id)


def _cursor_shadowed_by_source_cursor(
    *,
    cursor_file: Path,
    session_id: str,
    transcript_path: str,
    cursor_data: Dict[str, Any],
) -> bool:
    """Return True when a legacy/alias cursor is behind its source-key cursor."""
    if not transcript_path:
        return False
    try:
        source_key = _signal_source_cursor_key(
            session_id,
            transcript_path,
            cursor_data=cursor_data,
        )
    except Exception as exc:
        logger.warning("cursor shadow check failed for %s at %s: %s", session_id, transcript_path, exc)
        if _fail_hard_enabled():
            raise
        return False
    if cursor_file.stem == source_key:
        return False
    source_file = _cursor_dir() / f"{source_key}.json"
    if not source_file.is_file():
        return False
    source_has_size_metadata = False
    try:
        source_has_size_metadata = "transcript_size_bytes" in json.loads(source_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        source_has_size_metadata = False
    source_cursor = _read_cursor_file(source_file, session_id)
    source_path = str(source_cursor.get("transcript_path") or "").strip()
    if (
        _canonicalize_transcript_source_path(source_path)
        != _canonicalize_transcript_source_path(transcript_path)
    ):
        return False
    source_size_bytes = int(source_cursor.get("transcript_size_bytes", 0) or 0)
    current_size_bytes = _transcript_size_bytes(str(transcript_path))
    if source_has_size_metadata and current_size_bytes and current_size_bytes > source_size_bytes:
        logger.info(
            "session %s source cursor %s is stale for grown transcript "
            "(cursor_size=%d current_size=%d); allowing alias cursor %s to be scanned",
            session_id,
            source_file.name,
            source_size_bytes,
            current_size_bytes,
            cursor_file.name,
        )
        return False
    source_offset = int(source_cursor.get("line_offset", 0) or 0)
    cursor_offset = int(cursor_data.get("line_offset", 0) or 0)
    if source_offset < cursor_offset or (source_offset == cursor_offset == 0):
        return False
    logger.info(
        "session %s cursor %s shadowed by source cursor %s at offset %d >= %d; skipping stale alias",
        session_id,
        cursor_file.name,
        source_file.name,
        source_offset,
        cursor_offset,
    )
    return True


def _retire_shadowed_cursor_alias(
    *,
    cursor_file: Path,
    session_id: str,
    transcript_path: str,
    cursor_data: Dict[str, Any],
) -> bool:
    if not _cursor_shadowed_by_source_cursor(
        cursor_file=cursor_file,
        session_id=session_id,
        transcript_path=transcript_path,
        cursor_data=cursor_data,
    ):
        return False
    try:
        cursor_file.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("failed to retire shadowed cursor alias %s: %s", cursor_file, exc)
    return True


def _active_source_cursor_for_terminal_checkpoint_tail(
    *,
    cursor_file: Path,
    session_id: str,
    transcript_path: str,
    cursor_data: Dict[str, Any],
) -> tuple[Dict[str, Any], Path, str]:
    """Return a source cursor that stopped at session_end before transcript EOF.

    CDX rollout checkpoints can emit a session_end signal at the checkpoint
    boundary while the rollout transcript continues to receive later user turns.
    In that state the source cursor is not terminal for rolling; it is the next
    safe source-relative offset to continue scanning from.
    """
    if not transcript_path:
        return {}, Path(), ""
    try:
        source_key = _signal_source_cursor_key(
            session_id,
            transcript_path,
            cursor_data=cursor_data,
        )
    except Exception as exc:
        if _fail_hard_enabled():
            raise
        logger.warning(
            "session %s terminal checkpoint source cursor lookup failed for %s; returning empty: %s",
            session_id,
            transcript_path,
            exc,
        )
        return {}, Path(), ""
    if cursor_file.stem == source_key:
        return {}, Path(), ""
    source_file = _cursor_dir() / f"{source_key}.json"
    if not source_file.is_file():
        return {}, Path(), ""
    source_cursor = _read_cursor_file(source_file, session_id)
    source_path = str(source_cursor.get("transcript_path") or "").strip()
    if (
        _canonicalize_transcript_source_path(source_path)
        != _canonicalize_transcript_source_path(transcript_path)
    ):
        return {}, Path(), ""
    if str(source_cursor.get("processed_signal_type") or "").strip() != "session_end":
        return {}, Path(), ""
    source_offset = int(source_cursor.get("line_offset", 0) or 0)
    cursor_offset = int(cursor_data.get("line_offset", 0) or 0)
    if source_offset <= cursor_offset:
        return {}, Path(), ""
    try:
        total_lines = count_transcript_lines(transcript_path)
    except Exception as exc:
        if _fail_hard_enabled():
            raise
        logger.warning(
            "session %s terminal checkpoint transcript line count failed for %s; returning empty: %s",
            session_id,
            transcript_path,
            exc,
        )
        return {}, Path(), ""
    if source_offset >= total_lines:
        return {}, Path(), ""
    return source_cursor, source_file, source_key


def _active_source_cursor_for_grown_transcript(
    *,
    cursor_file: Path,
    session_id: str,
    transcript_path: str,
    cursor_data: Dict[str, Any],
) -> tuple[Dict[str, Any], Path, str]:
    """Return a source cursor whose transcript grew in bytes after EOF."""
    if not transcript_path:
        return {}, Path(), ""
    try:
        source_key = _signal_source_cursor_key(
            session_id,
            transcript_path,
            cursor_data=cursor_data,
        )
    except Exception as exc:
        if _fail_hard_enabled():
            raise
        logger.warning(
            "session %s grown transcript source cursor lookup failed for %s; returning empty: %s",
            session_id,
            transcript_path,
            exc,
        )
        return {}, Path(), ""
    if cursor_file.stem == source_key:
        return {}, Path(), ""
    source_file = _cursor_dir() / f"{source_key}.json"
    if not source_file.is_file():
        return {}, Path(), ""
    try:
        source_raw = json.loads(source_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        if isinstance(exc, OSError) and _fail_hard_enabled():
            raise
        logger.warning(
            "session %s grown transcript source cursor read failed for %s; returning empty: %s",
            session_id,
            source_file,
            exc,
        )
        return {}, Path(), ""
    if "transcript_size_bytes" not in source_raw:
        return {}, Path(), ""
    source_cursor = _read_cursor_file(source_file, session_id)
    source_path = str(source_cursor.get("transcript_path") or "").strip()
    if (
        _canonicalize_transcript_source_path(source_path)
        != _canonicalize_transcript_source_path(transcript_path)
    ):
        return {}, Path(), ""
    source_size_bytes = int(source_cursor.get("transcript_size_bytes", 0) or 0)
    current_size_bytes = _transcript_size_bytes(str(transcript_path))
    if not source_size_bytes or not current_size_bytes or current_size_bytes <= source_size_bytes:
        return {}, Path(), ""
    current_stat = _transcript_stat_metadata(str(transcript_path))
    current_inode = int(current_stat.get("inode", 0) or 0)
    current_device = int(current_stat.get("device", 0) or 0)
    source_inode = int(source_cursor.get("transcript_inode", 0) or 0)
    source_device = int(source_cursor.get("transcript_device", 0) or 0)
    if (source_inode and current_inode and source_inode != current_inode) or (
        source_device and current_device and source_device != current_device
    ):
        logger.info(
            "session %s source cursor %s belongs to a replaced transcript; "
            "keeping alias cursor active (source_inode=%s current_inode=%s)",
            session_id,
            source_file.name,
            source_inode,
            current_inode,
        )
        return {}, Path(), ""
    if not _cursor_rebase_guard_matches(source_cursor, str(transcript_path)):
        logger.info(
            "session %s source cursor %s processed-boundary guard no longer matches "
            "grown transcript; keeping alias cursor active",
            session_id,
            source_file.name,
        )
        return {}, Path(), ""
    source_offset = int(source_cursor.get("line_offset", 0) or 0)
    cursor_offset = int(cursor_data.get("line_offset", 0) or 0)
    if source_offset < cursor_offset:
        return {}, Path(), ""
    return source_cursor, source_file, source_key


def _active_source_cursor_for_stale_signal_transcript(
    session_id: str,
    transcript_path: str,
) -> tuple[str, str]:
    if not transcript_path or not _is_daemon_preserved_session_transcript_path(str(transcript_path)):
        return "", ""
    try:
        if os.path.isfile(transcript_path) and _transcript_size_bytes(transcript_path) > 0:
            return "", ""
        alias_cursor = read_cursor(session_id)
        candidate_path = str(alias_cursor.get("transcript_path") or "").strip()
        if (
            not candidate_path
            or candidate_path == transcript_path
            or _is_daemon_preserved_session_transcript_path(candidate_path)
            or not os.path.isfile(candidate_path)
        ):
            active_cursor, active_path, active_key = _active_source_cursor_for_empty_preserved_cursor(
                session_id,
                transcript_path,
            )
            if active_cursor and active_path and active_key:
                return active_path, active_key
            return "", ""
        source_key = _signal_source_cursor_key(
            session_id,
            candidate_path,
            cursor_data=alias_cursor,
        )
        source_cursor = _read_cursor_with_source_compat(session_id, source_key)
        source_path = str(source_cursor.get("transcript_path") or "").strip()
        if (
            _canonicalize_transcript_source_path(source_path)
            != _canonicalize_transcript_source_path(candidate_path)
        ):
            return "", ""
        source_offset = int(source_cursor.get("line_offset", 0) or 0)
        total_lines = count_transcript_lines(candidate_path)
        current_size_bytes = _transcript_size_bytes(candidate_path)
        source_size_bytes = int(source_cursor.get("transcript_size_bytes", 0) or 0)
        if source_offset < total_lines or (
            source_size_bytes > 0 and current_size_bytes > source_size_bytes
        ):
            return candidate_path, source_key
        active_cursor, active_path, active_key = _active_source_cursor_for_empty_preserved_cursor(
            session_id,
            transcript_path,
        )
        if active_cursor and active_path and active_key:
            return active_path, active_key
    except Exception as exc:
        if _fail_hard_enabled():
            raise
        logger.warning(
            "session %s stale signal transcript source cursor lookup failed for %s; returning empty: %s",
            session_id,
            transcript_path,
            exc,
        )
    return "", ""


def _active_source_cursor_for_empty_preserved_cursor(
    session_id: str,
    transcript_path: str,
) -> tuple[Dict[str, Any], str, str]:
    """Return the live cursor behind an empty daemon-preserved transcript alias."""
    if not transcript_path or not _is_daemon_preserved_session_transcript_path(str(transcript_path)):
        return {}, "", ""
    try:
        if os.path.isfile(transcript_path) and _transcript_size_bytes(transcript_path) > 0:
            return {}, "", ""
        transcript_name = Path(transcript_path).name
        best: tuple[float, Dict[str, Any], str, str] | None = None
        for cursor_file in _cursor_dir().glob("*.json"):
            if cursor_file.stem == _validate_session_id(session_id):
                continue
            cursor_data = _read_cursor_file(cursor_file, session_id)
            if str(cursor_data.get("session_id") or "").strip() != str(session_id):
                continue
            candidate_path = str(cursor_data.get("transcript_path") or "").strip()
            if (
                not candidate_path
                or candidate_path == transcript_path
                or _is_daemon_preserved_session_transcript_path(candidate_path)
                or Path(candidate_path).name != transcript_name
                or not os.path.isfile(candidate_path)
                or _transcript_size_bytes(candidate_path) <= 0
            ):
                continue
            try:
                mtime = cursor_file.stat().st_mtime
            except OSError:
                mtime = 0.0
            cursor_key = str(cursor_data.get("cursor_key") or cursor_file.stem or "").strip()
            if not cursor_key:
                continue
            if best is None or mtime >= best[0]:
                best = (mtime, cursor_data, candidate_path, cursor_key)
        if best is not None:
            return best[1], best[2], best[3]
    except Exception as exc:
        if _fail_hard_enabled():
            raise
        logger.warning(
            "session %s empty preserved cursor source lookup failed for %s; returning empty: %s",
            session_id,
            transcript_path,
            exc,
        )
    return {}, "", ""


def _transcript_has_jsonl_rows(transcript_path: str) -> bool:
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                loaded = json.loads(line)
                return isinstance(loaded, dict)
    except (json.JSONDecodeError, OSError):
        if _fail_hard_enabled():
            raise
    return False


def _larger_preserved_mirror_for_live_transcript(session_id: str, transcript_path: str, adapter=None) -> str:
    """Return the OC mirror read-source when active live JSONL is rewritten smaller.

    OpenClaw can compact/rewrite its live transcript during an active session while
    the instance-local preserved mirror still has the full user-message payload.
    Use that mirror only as a semantic-buffer read source; cursor and signal
    identity must remain on the live transcript path.
    """
    raw = str(transcript_path or "").strip()
    if not raw or _is_daemon_preserved_session_transcript_path(raw) or not os.path.isfile(raw):
        return ""
    try:
        live_path = Path(raw).expanduser().resolve()
        if not _adapter_owns_transcript_path(adapter, str(session_id), str(live_path)):
            return ""
        session_uuid = _SESSION_ID_UUID_RE.search(str(session_id or ""))
        filename_uuid = _SESSION_ID_UUID_RE.search(live_path.name)
        if not session_uuid or not filename_uuid or session_uuid.group(0).lower() != filename_uuid.group(0).lower():
            return ""
        mirror_path = (_instance_root() / "logs" / "quaid" / "sessions" / live_path.name).resolve()
        if not mirror_path.is_file() or mirror_path == live_path:
            return ""
        live_size = _transcript_size_bytes(str(live_path))
        mirror_size = _transcript_size_bytes(str(mirror_path))
        if mirror_size > 0 and mirror_size > live_size and _transcript_has_jsonl_rows(str(mirror_path)):
            return str(mirror_path)
    except Exception as exc:
        if _fail_hard_enabled():
            raise
        logger.warning(
            "session %s larger preserved mirror lookup failed for %s; returning empty: %s",
            session_id,
            transcript_path,
            exc,
        )
    return ""


def _larger_live_transcript_for_preserved_mirror(session_id: str, transcript_path: str, adapter=None) -> str:
    """Return an adapter-resolved live transcript when it is richer than a preserved mirror."""
    raw = str(transcript_path or "").strip()
    if not raw or not _is_daemon_preserved_session_transcript_path(raw) or not os.path.isfile(raw):
        return ""
    get_session_path = getattr(adapter, "get_session_path", None)
    if not callable(get_session_path):
        return ""
    try:
        live_candidate = get_session_path(str(session_id))
        live_raw = str(live_candidate or "").strip()
        if not live_raw or _is_daemon_preserved_session_transcript_path(live_raw) or not os.path.isfile(live_raw):
            return ""
        live_path = Path(live_raw).expanduser().resolve()
        if not _adapter_owns_transcript_path(adapter, str(session_id), str(live_path)):
            return ""
        session_uuid = _SESSION_ID_UUID_RE.search(str(session_id or ""))
        filename_uuid = _SESSION_ID_UUID_RE.search(live_path.name)
        if not session_uuid or not filename_uuid or session_uuid.group(0).lower() != filename_uuid.group(0).lower():
            return ""
        mirror_size = _transcript_size_bytes(raw)
        live_size = _transcript_size_bytes(str(live_path))
        if live_size > mirror_size and _transcript_has_jsonl_rows(str(live_path)):
            return str(live_path)
    except Exception as exc:
        if _fail_hard_enabled():
            raise
        logger.warning(
            "session %s larger live transcript lookup failed for %s; returning empty: %s",
            session_id,
            transcript_path,
            exc,
        )
    return ""


def _live_transcript_for_preserved_mirror(session_id: str, transcript_path: str, adapter=None) -> str:
    """Return the adapter live transcript behind a daemon-preserved mirror."""
    raw = str(transcript_path or "").strip()
    if not raw or not _is_daemon_preserved_session_transcript_path(raw) or not os.path.isfile(raw):
        return ""
    get_session_path = getattr(adapter, "get_session_path", None)
    if not callable(get_session_path):
        return ""
    try:
        live_candidate = get_session_path(str(session_id))
        live_raw = str(live_candidate or "").strip()
        if not live_raw or _is_daemon_preserved_session_transcript_path(live_raw) or not os.path.isfile(live_raw):
            return ""
        live_path = Path(live_raw).expanduser().resolve()
        if not _adapter_owns_transcript_path(adapter, str(session_id), str(live_path)):
            return ""
        session_uuid = _SESSION_ID_UUID_RE.search(str(session_id or ""))
        filename_uuid = _SESSION_ID_UUID_RE.search(live_path.name)
        if session_uuid and filename_uuid and session_uuid.group(0).lower() != filename_uuid.group(0).lower():
            return ""
        return str(live_path)
    except Exception:
        if _fail_hard_enabled():
            raise
    return ""


def _adapter_live_transcript_exists(session_id: str, adapter=None) -> bool:
    get_session_path = getattr(adapter, "get_session_path", None)
    if not callable(get_session_path):
        return False
    try:
        live_candidate = get_session_path(str(session_id))
        live_raw = str(live_candidate or "").strip()
        return bool(
            live_raw
            and not _is_daemon_preserved_session_transcript_path(live_raw)
            and os.path.isfile(live_raw)
            and _adapter_owns_transcript_path(adapter, str(session_id), live_raw)
        )
    except Exception:
        if _fail_hard_enabled():
            raise
    return False


def _adapter_live_transcript_missing(session_id: str, adapter=None) -> bool:
    get_session_path = getattr(adapter, "get_session_path", None)
    if not callable(get_session_path):
        return False
    try:
        live_candidate = get_session_path(str(session_id))
        live_raw = str(live_candidate or "").strip()
        if not live_raw or _is_daemon_preserved_session_transcript_path(live_raw):
            return True
        return not os.path.isfile(live_raw)
    except Exception:
        if _fail_hard_enabled():
            raise
    return False


def _preserved_mirror_for_missing_transcript_cursor(session_id: str, transcript_path: str, adapter=None) -> str:
    raw = str(transcript_path or "").strip()
    if not raw or os.path.isfile(raw) or _is_daemon_preserved_session_transcript_path(raw):
        return ""
    try:
        live_path = Path(raw).expanduser()
        session_uuid = _SESSION_ID_UUID_RE.search(str(session_id or ""))
        filename_uuid = _SESSION_ID_UUID_RE.search(live_path.name)
        if not session_uuid or not filename_uuid or session_uuid.group(0).lower() != filename_uuid.group(0).lower():
            return ""
        if _adapter_live_transcript_exists(str(session_id), adapter=adapter):
            return ""
        mirror_path = (_instance_root() / "logs" / "quaid" / "sessions" / live_path.name).resolve()
        if mirror_path.is_file() and _transcript_has_jsonl_rows(str(mirror_path)):
            return str(mirror_path)
    except Exception:
        if _fail_hard_enabled():
            raise
    return ""


def _reap_orphaned_cursor_if_stale(
    *,
    cursor_file: Path,
    session_id: str,
    transcript_path: str,
    now: float,
    adapter=None,
    scanner: str,
) -> bool:
    """Delete old cursors whose source transcript is gone and unrecoverable."""
    raw_path = str(transcript_path or "").strip()
    if not raw_path or os.path.isfile(raw_path):
        return False
    if _preserved_mirror_for_missing_transcript_cursor(str(session_id), raw_path, adapter=adapter):
        return False
    try:
        cursor_mtime = cursor_file.stat().st_mtime
    except OSError as exc:
        if _fail_hard_enabled():
            raise
        logger.debug("failed to stat orphaned cursor %s during %s: %s", cursor_file, scanner, exc)
        return False
    age_seconds = max(0.0, float(now) - float(cursor_mtime))
    if age_seconds < _ORPHANED_CURSOR_RETENTION_SECONDS:
        return False
    try:
        cursor_file.unlink()
    except OSError as exc:
        if _fail_hard_enabled():
            raise
        logger.warning("failed to delete orphaned cursor %s during %s: %s", cursor_file, scanner, exc)
        return False
    logger.info(
        "deleted orphaned cursor %s for missing transcript %s during %s (age=%.0fs)",
        cursor_file.name,
        raw_path,
        scanner,
        age_seconds,
    )
    return True


def _pending_signal_source_keys(
    signals: List[Dict[str, Any]],
    *,
    signal_type: Optional[str] = None,
) -> set[str]:
    keys: set[str] = set()
    for signal in signals:
        current_type = str(signal.get("type") or signal.get("signal_type") or "")
        if signal_type and current_type != signal_type:
            continue
        session_id = str(signal.get("session_id") or "").strip()
        transcript_path = str(signal.get("transcript_path") or "").strip()
        if not session_id:
            continue
        try:
            meta_key = _signal_meta_cursor_key(
                session_id,
                signal.get("meta") if isinstance(signal.get("meta"), dict) else {},
            )
            if meta_key:
                keys.add(meta_key)
            elif transcript_path:
                keys.add(_signal_source_cursor_key(session_id, transcript_path))
        except Exception:
            continue
    return keys


def _remember_queued_source_signal(
    *,
    pending_session_ids: set[str],
    pending_source_keys: set[str],
    session_id: str,
    source_key: str,
) -> None:
    """Track a just-written signal so same-scan alias cursors do not queue duplicates."""
    pending_session_ids.add(str(session_id or "").strip())
    if source_key:
        pending_source_keys.add(source_key)


def _source_signal_already_pending(
    *,
    pending_source_keys: set[str],
    source_key: str,
    session_id: str,
    scanner: str,
) -> bool:
    if source_key not in pending_source_keys:
        return False
    logger.debug(
        "session %s source %s already has pending signal during %s scan; skipping duplicate",
        session_id,
        source_key,
        scanner,
    )
    return True


def _extraction_cache_source_for_missing_rolling_snapshot(transcript_path: str) -> str:
    raw = str(transcript_path or "").strip()
    if not raw or not _is_daemon_rolling_transcript_snapshot_location(raw):
        return ""
    try:
        snapshot_path = Path(raw).expanduser()
        basename = snapshot_path.name
        if not basename:
            return ""
        for parent in snapshot_path.parents:
            candidate = parent / "extraction_cache" / basename
            if candidate.is_file():
                return str(candidate)
    except OSError as exc:
        if _fail_hard_enabled():
            raise
    return ""


def _rolling_state_flush_transcript_path(session_id: str, rolling: Dict[str, Any]) -> str:
    """Return an existing transcript path that can drive a staged flush."""
    primary = str(rolling.get("transcript_path") or rolling.get("buffer_transcript_path") or "").strip()
    missing_basename = Path(primary).name if primary else ""
    candidates: List[str] = []
    for key in ("transcript_path", "buffer_transcript_path", "source_transcript_path"):
        value = str(rolling.get(key) or "").strip()
        if value:
            candidates.append(value)

    def _add_cursor_candidate(cursor: Dict[str, Any]) -> None:
        if str(cursor.get("session_id") or "").strip() != str(session_id or "").strip():
            return
        value = str(cursor.get("transcript_path") or "").strip()
        if value:
            candidates.append(value)

    try:
        _add_cursor_candidate(read_cursor(session_id))
    except Exception as exc:
        if _fail_hard_enabled():
            raise
        logger.debug("failed reading cursor for staged rolling recovery %s: %s", session_id, exc)
    try:
        for cursor_file in _cursor_dir().glob("*.json"):
            try:
                raw_cursor = json.loads(cursor_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(raw_cursor, dict):
                _add_cursor_candidate(raw_cursor)
    except OSError as exc:
        if _fail_hard_enabled():
            raise
        logger.debug("failed scanning cursors for staged rolling recovery %s: %s", session_id, exc)

    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if missing_basename and Path(candidate).name != missing_basename:
            continue
        if os.path.isfile(candidate):
            return candidate

    if primary:
        cache_candidate = _extraction_cache_source_for_missing_rolling_snapshot(primary)
        if cache_candidate:
            return cache_candidate
    return ""


def _queue_missing_staged_rolling_flushes_from_state(
    *,
    pending_session_ids: set[str],
    pending_source_keys: set[str],
    scanner: str,
) -> None:
    """Recover publish-ready rolling stages even when their cursor row disappeared."""
    try:
        state_files = sorted(_rolling_state_dir().glob("*.json"))
    except OSError as exc:
        if _fail_hard_enabled():
            raise
        logger.debug("rolling state recovery scan failed: %s", exc)
        return
    for state_file in state_files:
        try:
            session_id = _validate_session_id(state_file.stem)
            rolling = read_rolling_state(session_id)
        except Exception as exc:
            if _fail_hard_enabled():
                raise
            logger.warning("rolling state recovery scan failed for %s: %s", state_file, exc)
            continue
        if not staged_state_has_payload(rolling):
            continue
        if not bool(rolling.get(_STAGED_PAYLOAD_PENDING_FLUSH_KEY)):
            continue
        if session_id in pending_session_ids:
            continue
        transcript_path = _rolling_state_flush_transcript_path(session_id, rolling)
        if not transcript_path:
            continue
        source_key = _signal_source_cursor_key(
            session_id,
            transcript_path,
            staged_state=rolling,
        )
        if _source_signal_already_pending(
            pending_source_keys=pending_source_keys,
            source_key=source_key,
            session_id=session_id,
            scanner=scanner,
        ):
            continue
        source_lock_active = _processing_lock_active(source_key)
        if source_lock_active:
            logger.info(
                "session %s has staged rolling payload but source lock %s is still active; "
                "queueing missing-flush recovery for retry",
                session_id,
                source_key,
            )
        logger.warning(
            "session %s has staged rolling payload without cursor-visible pending flush; "
            "regenerating rolling_stage_flush",
            session_id,
        )
        write_signal(
            signal_type="session_end",
            session_id=session_id,
            transcript_path=transcript_path,
            meta={
                "reason": "rolling_stage_flush",
                "source_signal": "rolling",
                "staged_payload_sweep": True,
                "recovered_missing_flush": True,
                "recovered_from_rolling_state": True,
                "source_lock_active_at_recovery": source_lock_active,
                "source_cursor_key": source_key,
                "buffered_line_offset": int(rolling.get("buffered_line_offset", 0) or 0),
                "flush_staged_payload_only": True,
            },
        )
        _remember_queued_source_signal(
            pending_session_ids=pending_session_ids,
            pending_source_keys=pending_source_keys,
            session_id=session_id,
            source_key=source_key,
        )


def _queue_idle_semantic_rolling_flushes_without_cursor(
    *,
    pending_session_ids: set[str],
    pending_source_keys: set[str],
    known_cursor_session_ids: set[str],
    now: float,
    timeout_seconds: float,
    installed_at_ts: float,
) -> None:
    """Recover sub-threshold rolling semantic buffers whose cursor row disappeared."""
    try:
        state_files = sorted(_rolling_state_dir().glob("*.json"))
    except OSError as exc:
        if _fail_hard_enabled():
            raise
        logger.debug("idle rolling semantic recovery state scan failed: %s", exc)
        return
    for state_file in state_files:
        try:
            session_id = _validate_session_id(state_file.stem)
            rolling = read_rolling_state(session_id)
        except Exception as exc:
            if _fail_hard_enabled():
                raise
            logger.warning("idle rolling semantic recovery scan failed for %s: %s", state_file, exc)
            continue
        if session_id in known_cursor_session_ids or session_id in pending_session_ids:
            continue
        if not _semantic_buffer_has_content(rolling):
            continue
        transcript_path = _rolling_state_flush_transcript_path(session_id, rolling)
        if not transcript_path:
            continue
        try:
            mtime = os.path.getmtime(transcript_path)
        except OSError as exc:
            if _fail_hard_enabled():
                raise
            logger.debug("idle rolling semantic recovery skipped %s: %s", transcript_path, exc)
            continue
        if mtime < installed_at_ts or now - mtime < timeout_seconds:
            continue
        source_key = _signal_source_cursor_key(session_id, transcript_path, staged_state=rolling)
        if _source_signal_already_pending(
            pending_source_keys=pending_source_keys,
            source_key=source_key,
            session_id=session_id,
            scanner="idle-rolling-state-recovery",
        ):
            continue
        if _processing_lock_active(source_key):
            continue
        logger.info(
            "session %s has idle rolling semantic buffer without cursor row; generating session_end flush",
            session_id,
        )
        write_signal(
            signal_type="session_end",
            session_id=session_id,
            transcript_path=transcript_path,
            meta={
                "reason": "idle_rolling_semantic_buffer_flush",
                "recovered_from_rolling_state": True,
                "source_cursor_key": source_key,
                "semantic_buffer_tokens": int(rolling.get("semantic_buffer_tokens", 0) or 0),
                "buffered_line_offset": int(rolling.get("buffered_line_offset", 0) or 0),
            },
        )
        _remember_queued_source_signal(
            pending_session_ids=pending_session_ids,
            pending_source_keys=pending_source_keys,
            session_id=session_id,
            source_key=source_key,
        )


def _deferred_extraction_dir() -> Path:
    d = _instance_root() / "data" / "deferred-extractions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_deferred_extraction(
    *,
    session_id: str,
    transcript_text: str,
    owner_id: str,
    label: str,
    reason: str,
) -> bool:
    """Save an unextracted transcript chunk for later janitor recovery.

    Written when a provider outage exhausts the 6-hour retry window.
    The janitor's deferred_extraction task picks these up and retries
    extraction when the provider is back.
    """
    session_id = _validate_session_id(session_id)
    ts = int(time.time())
    filename = f"{session_id}_{ts}_{os.getpid()}_{uuid.uuid4().hex}.json"
    path = _deferred_extraction_dir() / filename
    payload = {
        "session_id": session_id,
        "owner_id": owner_id,
        "label": label,
        "reason": reason,
        "saved_at": ts,
        "transcript_text": transcript_text,
    }
    try:
        _atomic_write(path, json.dumps(payload, indent=2))
        logger.warning(
            "[daemon] saved deferred extraction for session %s: %s (%d chars)",
            session_id, path, len(transcript_text),
        )
        return True
    except Exception as e:
        if _fail_hard_enabled():
            raise
        logger.error(
            "[daemon] failed to save deferred extraction for session %s: %s",
            session_id, e,
        )
        return False


def _rolling_state_dir() -> Path:
    d = _instance_root() / "data" / "rolling-extraction"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _processing_lock_dir() -> Path:
    d = _instance_root() / "data" / "session-processing"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _processing_lock_path(session_id: str) -> Path:
    session_id = _validate_session_id(session_id)
    return _processing_lock_dir() / f"{session_id}.lock"


def _read_processing_lock_payload(lock_path: Path) -> Dict[str, Any]:
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
        if not raw:
            return {}
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            loaded = raw.splitlines()[0].strip()
        if isinstance(loaded, dict):
            return loaded
        try:
            return {"pid": int(str(loaded).strip()), "legacy_pid_lock": True}
        except (TypeError, ValueError):
            return {}
    except Exception as exc:
        if _fail_hard_enabled():
            raise
        logger.debug("[daemon] failed reading processing lock payload %s: %s", lock_path, exc)
        return {}


def _processing_lock_old_enough(lock_path: Path) -> bool:
    try:
        raw_min_age = os.environ.get("QUAID_PROCESSING_LOCK_STALE_SECONDS", "")
        min_age_seconds = max(0.0, float(raw_min_age) if raw_min_age.strip() else 10.0)
    except Exception:
        min_age_seconds = 10.0
    try:
        age_seconds = time.time() - lock_path.stat().st_mtime
    except OSError:
        return False
    return age_seconds >= min_age_seconds


def _processing_lock_unlocked(lock_path: Path) -> bool:
    fd: Optional[int] = None
    try:
        fd = os.open(str(lock_path), os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    except FileNotFoundError:
        return True
    except (OSError, IOError):
        return False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _processing_lock_holder_dead(lock_path: Path) -> bool:
    payload = _read_processing_lock_payload(lock_path)
    try:
        pid = int(payload.get("pid") or 0)
    except Exception:
        return False
    if pid == os.getpid():
        return False
    if pid <= 0:
        # A daemon crash can leave an empty or partially-written lock file after
        # the OS releases flock. Treat only old, currently-unlocked files as stale.
        if not _processing_lock_old_enough(lock_path):
            return False
        return _processing_lock_unlocked(lock_path)
    return not _pid_alive(pid)


def _remove_stale_processing_lock(lock_path: Path, *, holder_dead: bool) -> bool:
    if not holder_dead:
        return False
    fd: Optional[int] = None
    try:
        fd = os.open(str(lock_path), os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError):
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        logger.warning(
            "stale session processing lock holder appears dead but file is still locked: %s",
            lock_path,
        )
        return False
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("failed removing stale session processing lock %s: %s", lock_path, exc)
        return False
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
    logger.warning("removed stale session processing lock with dead holder: %s", lock_path)
    return True


def _acquire_session_processing_lock(session_id: str) -> Optional[int]:
    """Acquire a per-session processing lease; returns fd while held."""
    lock_path = _processing_lock_path(session_id)
    try:
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as e:
        logger.warning("failed opening session processing lock for %s: %s", session_id, e)
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError):
        holder_dead = _processing_lock_holder_dead(lock_path)
        try:
            os.close(fd)
        except OSError:
            pass
        if not _remove_stale_processing_lock(lock_path, holder_dead=holder_dead):
            return None
        retry_fd: Optional[int] = None
        try:
            retry_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
            fd = retry_fd
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as exc:
            logger.warning("failed reclaiming stale session processing lock for %s: %s", session_id, exc)
            if retry_fd is not None:
                try:
                    os.close(retry_fd)
                except Exception:
                    pass
            return None
        except Exception as exc:
            logger.warning("failed reopening stale session processing lock for %s: %s", session_id, exc)
            if retry_fd is not None:
                try:
                    os.close(retry_fd)
                except Exception:
                    pass
            return None
    payload = {
        "session_id": _validate_session_id(session_id),
        "pid": os.getpid(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps(payload).encode("utf-8"))
        os.fsync(fd)
    except OSError as exc:
        logger.warning("failed writing session processing lock payload for %s: %s", session_id, exc)
        if _fail_hard_enabled():
            try:
                os.close(fd)
            except OSError:
                pass
            raise RuntimeError("session processing lock payload write failed") from exc
        try:
            os.close(fd)
        except OSError:
            pass
        return None
    return fd


def _release_session_processing_lock(session_id: str, lock_fd: Optional[int]) -> None:
    if lock_fd is None:
        return
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(lock_fd)
    except OSError:
        pass


def _processing_lock_active(session_id: str) -> bool:
    """Return True when another live worker owns this source/session lock."""
    lock_path = _processing_lock_path(session_id)
    if not lock_path.is_file():
        return False
    holder_dead = _processing_lock_holder_dead(lock_path)
    if _remove_stale_processing_lock(lock_path, holder_dead=holder_dead):
        return False
    if _processing_lock_unlocked(lock_path):
        return False
    return True


def _rolling_state_path(session_id: str) -> Path:
    session_id = _validate_session_id(session_id)
    return _rolling_state_dir() / f"{session_id}.json"


def read_rolling_state(session_id: str) -> Dict[str, Any]:
    """Read durable staged extraction state for a session."""
    semantic_defaults = _semantic_stage_metrics_defaults()
    state_path = _rolling_state_path(session_id)
    if not state_path.is_file():
        return {
            "session_id": session_id,
            "transcript_path": "",
            "carry_facts": [],
            "raw_facts": [],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "raw_source_chunks": [],
            "rolling_batches": 0,
            "processed_line_offset": 0,
            "buffered_line_offset": 0,
            "semantic_buffer": "",
            "semantic_buffer_tokens": 0,
            "facts_skipped": 0,
            "payload_duplicate_facts_collapsed": 0,
            "carry_duplicate_facts_dropped": 0,
            "assessment_usable": 0,
            "assessment_nothing_usable": 0,
            "assessment_needs_smaller_chunk": 0,
            "unclassified_empty_payloads": 0,
            **semantic_defaults,
        }
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        if _fail_hard_enabled():
            raise
        logger.warning("rolling state read failed for %s; resetting staged state", session_id)
        return {
            "session_id": session_id,
            "transcript_path": "",
            "carry_facts": [],
            "raw_facts": [],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "raw_source_chunks": [],
            "rolling_batches": 0,
            "processed_line_offset": 0,
            "buffered_line_offset": 0,
            "semantic_buffer": "",
            "semantic_buffer_tokens": 0,
            "facts_skipped": 0,
            "payload_duplicate_facts_collapsed": 0,
            "carry_duplicate_facts_dropped": 0,
            "assessment_usable": 0,
            "assessment_nothing_usable": 0,
            "assessment_needs_smaller_chunk": 0,
            "unclassified_empty_payloads": 0,
            **semantic_defaults,
        }
    if not isinstance(data, dict):
        return {
            "session_id": session_id,
            "transcript_path": "",
            "carry_facts": [],
            "raw_facts": [],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
            "raw_source_chunks": [],
            "rolling_batches": 0,
            "processed_line_offset": 0,
            "buffered_line_offset": 0,
            "semantic_buffer": "",
            "semantic_buffer_tokens": 0,
            "facts_skipped": 0,
            "payload_duplicate_facts_collapsed": 0,
            "carry_duplicate_facts_dropped": 0,
            "assessment_usable": 0,
            "assessment_nothing_usable": 0,
            "assessment_needs_smaller_chunk": 0,
            "unclassified_empty_payloads": 0,
            **semantic_defaults,
        }
    data.setdefault("session_id", session_id)
    data.setdefault("transcript_path", "")
    data.setdefault("carry_facts", [])
    data.setdefault("raw_facts", [])
    data.setdefault("raw_snippets", {})
    data.setdefault("raw_journal", {})
    data.setdefault("raw_project_logs", {})
    data.setdefault("raw_source_chunks", [])
    data.setdefault("rolling_batches", 0)
    data.setdefault("processed_line_offset", 0)
    data.setdefault("buffered_line_offset", int(data.get("processed_line_offset", 0) or 0))
    data.setdefault("buffer_transcript_path", str(data.get("transcript_path") or ""))
    data.setdefault("semantic_buffer", "")
    data.setdefault("semantic_buffer_tokens", 0)
    data.setdefault("semantic_buffer_prior", "")
    data.setdefault("semantic_buffer_tail", "")
    data.setdefault("semantic_buffer_prior_tokens", 0)
    data.setdefault("semantic_buffer_tail_tokens", 0)
    data.setdefault("facts_skipped", 0)
    data.setdefault("payload_duplicate_facts_collapsed", 0)
    data.setdefault("carry_duplicate_facts_dropped", 0)
    data.setdefault("assessment_usable", 0)
    data.setdefault("assessment_nothing_usable", 0)
    data.setdefault("assessment_needs_smaller_chunk", 0)
    data.setdefault("unclassified_empty_payloads", 0)
    for key, value in semantic_defaults.items():
        data.setdefault(key, value)
    return data


def write_rolling_state(session_id: str, state: Dict[str, Any]) -> None:
    payload = dict(state or {})
    normalized_session_id = _validate_session_id(session_id)
    payload["session_id"] = normalized_session_id
    payload["transcript_path"] = str(payload.get("transcript_path", "") or "")

    has_semantic_buffer = bool(str(payload.get("semantic_buffer", "") or "").strip())
    has_semantic_tokens = int(payload.get("semantic_buffer_tokens", 0) or 0) > 0
    has_batches = int(payload.get("rolling_batches", 0) or 0) > 0
    has_carry_facts = _has_text_payload(payload.get("carry_facts"))
    has_raw_facts = _has_text_payload(payload.get("raw_facts"))
    has_raw_snippets = _has_text_payload(payload.get("raw_snippets"))
    has_raw_journal = _has_text_payload(payload.get("raw_journal"))
    has_raw_project_logs = _has_text_payload(payload.get("raw_project_logs"))

    if not any(
        (
            has_semantic_buffer,
            has_semantic_tokens,
            has_batches,
            has_carry_facts,
            has_raw_facts,
            has_raw_snippets,
            has_raw_journal,
            has_raw_project_logs,
        )
    ):
        clear_rolling_state(normalized_session_id)
        return

    payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _atomic_write(_rolling_state_path(normalized_session_id), json.dumps(payload))


def clear_rolling_state(session_id: str) -> None:
    target_path = _rolling_state_path(session_id)
    target_payload: Dict[str, Any] = {}
    try:
        if target_path.is_file():
            raw_payload = json.loads(target_path.read_text(encoding="utf-8"))
            if isinstance(raw_payload, dict):
                target_payload = raw_payload
    except Exception as exc:
        if _fail_hard_enabled():
            raise
        logger.warning("rolling state read failed before cleanup for %s at %s: %s", session_id, target_path, exc)
        target_payload = {}
    removed = False
    try:
        target_path.unlink()
        removed = True
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("rolling state unlink failed for %s at %s: %s", session_id, target_path, exc)

    if removed:
        _cleanup_rolling_state_snapshot_paths(target_payload)
        return

    # Reconcile stale files whose basename drifted from the logical session_id
    # but whose payload still points at this session. This keeps rolling_flush
    # cleanup authoritative even if an earlier write used a mismatched path key.
    try:
        for state_file in _rolling_state_dir().glob("*.json"):
            try:
                payload = json.loads(state_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            if str(payload.get("session_id") or "").strip() != str(session_id or "").strip():
                continue
            try:
                state_file.unlink()
                _cleanup_rolling_state_snapshot_paths(payload)
            except OSError as exc:
                logger.warning("rolling state unlink failed for %s at %s: %s", session_id, state_file, exc)
    except OSError:
        pass


def _merge_unique_strings(existing: List[str], incoming: List[str]) -> List[str]:
    combined = []
    seen = set()
    for item in list(existing or []) + list(incoming or []):
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        combined.append(text)
    return combined


def _normalize_project_log_timestamp(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return f"{raw}T23:59:59"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        logger.debug("[daemon] unrecognized project log timestamp format: %r", raw)
        return None
    return parsed.isoformat(timespec="seconds")


def _normalize_project_log_entry(raw: Any) -> Optional[Dict[str, Any]]:
    if isinstance(raw, dict):
        text = raw.get("text", raw.get("entry", raw.get("note", "")))
        created_at = (
            _normalize_project_log_timestamp(raw.get("created_at"))
            or _normalize_project_log_timestamp(raw.get("timestamp"))
            or _normalize_project_log_timestamp(raw.get("date"))
        )
    else:
        text = raw
        created_at = None
    text = str(text or "").strip()
    if not text:
        return None
    entry: Dict[str, Any] = {"text": text}
    if created_at:
        entry["created_at"] = created_at
    return entry


def _merge_project_log_entries(existing: Any, incoming: Any) -> List[Dict[str, Any]]:
    combined: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str]] = set()
    for raw in list(existing or []) + list(incoming or []):
        entry = _normalize_project_log_entry(raw)
        if not entry:
            continue
        key = (str(entry.get("text", "")), str(entry.get("created_at", "")))
        if key in seen:
            continue
        seen.add(key)
        combined.append(entry)
    return combined


def _warm_payload_embeddings(facts: List[Dict[str, Any]]) -> Dict[str, int]:
    """Front-load embeddings into cache so final publish stays mostly cache-hit."""
    texts: List[str] = []
    for fact in facts or []:
        if not isinstance(fact, dict):
            continue
        text = str(fact.get("text", "") or "").strip()
        if not _semantic_fact_has_dedup_signal(text):
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
    from core.services.memory_service import get_memory_service

    return get_memory_service().warm_embeddings(texts)


def _semantic_stage_metrics_defaults() -> Dict[str, int]:
    return {
        "staged_semantic_duplicate_facts_collapsed": 0,
        "staged_semantic_auto_reject_hits": 0,
        "staged_semantic_gray_zone_rows": 0,
        "staged_semantic_subset_rows": 0,
        "staged_semantic_llm_checks": 0,
        "staged_semantic_llm_same_hits": 0,
        "staged_semantic_llm_different_hits": 0,
    }


def _semantic_confidence_rank(value: Any) -> int:
    raw = str(value or "").strip().lower()
    if raw == "high":
        return 3
    if raw == "medium":
        return 2
    if raw == "low":
        return 1
    return 0


def _merge_fact_keywords(existing: Any, incoming: Any) -> Optional[str]:
    tokens: List[str] = []
    seen: set[str] = set()
    for raw in (existing, incoming):
        text = str(raw or "").strip()
        if not text:
            continue
        for token in text.split():
            clean = token.strip()
            if not clean or clean in seen:
                continue
            seen.add(clean)
            tokens.append(clean)
    return " ".join(tokens) if tokens else None


def _merge_fact_edges(existing: Any, incoming: Any) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str, str]] = set()
    for edges in (existing, incoming):
        for edge in list(edges or []):
            if not isinstance(edge, dict):
                continue
            key = (
                str(edge.get("subject", "") or "").strip(),
                str(edge.get("relation", "") or "").strip(),
                str(edge.get("object", "") or "").strip(),
            )
            if not all(key) or key in seen:
                continue
            seen.add(key)
            merged.append(dict(edge))
    return merged


def _merge_semantic_duplicate_fact(
    existing_fact: Dict[str, Any],
    incoming_fact: Dict[str, Any],
    *,
    prefer_incoming_text: bool,
) -> Dict[str, Any]:
    merged = dict(existing_fact or {})
    incoming = dict(incoming_fact or {})
    existing_text = str(merged.get("text", "") or "").strip()
    incoming_text = str(incoming.get("text", "") or "").strip()
    if prefer_incoming_text and incoming_text:
        merged["text"] = incoming_text
    elif not existing_text and incoming_text:
        merged["text"] = incoming_text

    existing_domains = list(merged.get("domains", []) or [])
    incoming_domains = list(incoming.get("domains", []) or [])
    merged_domains: List[str] = []
    for domain in existing_domains + incoming_domains:
        clean = str(domain or "").strip()
        if clean and clean not in merged_domains:
            merged_domains.append(clean)
    if merged_domains:
        merged["domains"] = merged_domains

    if _semantic_confidence_rank(incoming.get("extraction_confidence")) >= _semantic_confidence_rank(merged.get("extraction_confidence")):
        incoming_conf = incoming.get("extraction_confidence")
        if incoming_conf is not None:
            merged["extraction_confidence"] = incoming_conf

    merged_keywords = _merge_fact_keywords(merged.get("keywords"), incoming.get("keywords"))
    if merged_keywords:
        merged["keywords"] = merged_keywords

    merged_edges = _merge_fact_edges(merged.get("edges"), incoming.get("edges"))
    if merged_edges:
        merged["edges"] = merged_edges

    for key in (
        "category",
        "privacy",
        "project",
        "speaker",
        "source",
    ):
        if not merged.get(key) and incoming.get(key):
            merged[key] = incoming.get(key)
    return merged


def _stage_dedup_settings() -> Tuple[float, float, bool]:
    try:
        from config import get_config

        cfg = get_config()
        auto_reject_thresh = float(cfg.janitor.dedup.auto_reject_threshold)
        gray_zone_low = float(cfg.janitor.dedup.gray_zone_low)
        llm_verify_enabled = bool(cfg.janitor.dedup.llm_verify_enabled)
        return auto_reject_thresh, gray_zone_low, llm_verify_enabled
    except Exception as exc:
        if _fail_hard_enabled():
            raise
        logger.warning("[daemon] failed reading stage dedup settings; using defaults: %s", exc)
        return 0.98, 0.88, False


def _semantic_candidate_overlaps(new_text: str, existing_facts: List[Dict[str, Any]], max_candidates: int = 12) -> List[int]:
    from lib.tokens import extract_key_tokens

    new_tokens = set(extract_key_tokens(new_text, max_tokens=10))
    if not new_tokens:
        return []
    scored: List[Tuple[int, int]] = []
    for idx, fact in enumerate(existing_facts):
        text = str((fact or {}).get("text", "") or "").strip()
        if not _semantic_fact_has_dedup_signal(text):
            continue
        existing_tokens = set(extract_key_tokens(text, max_tokens=10))
        overlap = len(new_tokens & existing_tokens)
        if overlap <= 0:
            overlap = _compact_unicode_overlap_score(new_text, text)
            if overlap <= 0:
                continue
        if overlap >= 2 or len(new_tokens) <= 3:
            scored.append((overlap, idx))
    scored.sort(key=lambda item: (-item[0], -item[1]))
    return [idx for _overlap, idx in scored[:max_candidates]]


def _compact_unicode_overlap_score(first: str, second: str) -> int:
    """Return a small overlap score for compact scripts without whitespace tokens."""
    first_grams = _compact_unicode_char_grams(first)
    second_grams = _compact_unicode_char_grams(second)
    if not first_grams or not second_grams:
        return 0
    shared = len(first_grams & second_grams)
    smaller = min(len(first_grams), len(second_grams))
    union = len(first_grams | second_grams)
    if shared < 3 or smaller <= 0 or union <= 0:
        return 0
    if (shared / smaller) < 0.55 or (shared / union) < 0.35:
        return 0
    return min(shared, 8)


def _compact_unicode_char_grams(text: str) -> set[str]:
    compact = "".join(
        ch
        for ch in unicodedata.normalize("NFKC", str(text or "")).casefold()
        if ch.isalnum()
    )
    if not compact or compact.isascii() or len(compact) < 4:
        return set()
    return {compact[i:i + 2] for i in range(len(compact) - 1)}


def _semantic_fact_has_dedup_signal(text: str) -> bool:
    """Return true when a staged fact has enough lexical signal for dedup routing."""
    clean = str(text or "").strip()
    if not clean:
        return False
    if len(clean.split()) >= 3:
        return True

    from lib.tokens import extract_key_tokens

    tokens = extract_key_tokens(clean, max_tokens=4)
    if not tokens:
        return False
    # Compact scripts often do not use spaces; a single multi-char Unicode token
    # can still be a complete fact anchor and should not bypass semantic dedup.
    return len(clean) >= 4 and any(not token.isascii() and len(token) >= 2 for token in tokens)


def _semantic_subset_overlap_candidate(new_text: str, existing_text: str) -> bool:
    from lib.tokens import is_subset_overlap_candidate

    return is_subset_overlap_candidate(new_text, existing_text)


def _collapse_staged_semantic_duplicates(
    existing_facts: List[Dict[str, Any]],
    incoming_facts: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    from datastore.memorydb.memory_graph import _llm_dedup_check_many, get_graph
    from lib.similarity import cosine_similarity
    from lib.tokens import texts_are_near_identical

    metrics = _semantic_stage_metrics_defaults()
    if not incoming_facts:
        return list(existing_facts or []), metrics

    auto_reject_thresh, gray_zone_low, llm_verify_enabled = _stage_dedup_settings()
    accepted = [dict(fact) for fact in list(existing_facts or []) if isinstance(fact, dict)]
    graph = get_graph()

    for incoming_fact in list(incoming_facts or []):
        if not isinstance(incoming_fact, dict):
            continue
        new_text = str(incoming_fact.get("text", "") or "").strip()
        if not _semantic_fact_has_dedup_signal(new_text):
            accepted.append(dict(incoming_fact))
            continue

        candidate_indexes = _semantic_candidate_overlaps(new_text, accepted)
        if not candidate_indexes:
            accepted.append(dict(incoming_fact))
            continue

        new_embedding = graph.get_embedding(new_text)
        if not new_embedding:
            accepted.append(dict(incoming_fact))
            continue

        gray_zone: List[Tuple[int, Dict[str, Any], float]] = []
        merged = False
        for idx in candidate_indexes:
            existing_fact = accepted[idx]
            existing_text = str(existing_fact.get("text", "") or "").strip()
            if not _semantic_fact_has_dedup_signal(existing_text):
                continue
            existing_embedding = graph.get_embedding(existing_text)
            if not existing_embedding:
                continue
            sim = cosine_similarity(new_embedding, existing_embedding)
            if sim >= auto_reject_thresh and texts_are_near_identical(new_text, existing_text):
                accepted[idx] = _merge_semantic_duplicate_fact(
                    existing_fact,
                    incoming_fact,
                    prefer_incoming_text=len(new_text) >= len(existing_text),
                )
                metrics["staged_semantic_duplicate_facts_collapsed"] += 1
                metrics["staged_semantic_auto_reject_hits"] += 1
                merged = True
                break
            if sim >= gray_zone_low:
                metrics["staged_semantic_gray_zone_rows"] += 1
                gray_zone.append((idx, existing_fact, sim))
                continue
            if _semantic_subset_overlap_candidate(new_text, existing_text):
                metrics["staged_semantic_subset_rows"] += 1
                gray_zone.append((idx, existing_fact, sim))

        if merged:
            continue

        if gray_zone and llm_verify_enabled:
            batch = gray_zone[:4]
            metrics["staged_semantic_llm_checks"] += len(batch)
            llm_results = _llm_dedup_check_many(new_text, [fact.get("text", "") for _idx, fact, _sim in batch])
            if llm_results:
                for result_idx, (accepted_idx, existing_fact, _sim) in enumerate(batch, start=1):
                    llm_result = llm_results.get(result_idx)
                    if llm_result is None:
                        continue
                    if llm_result.get("is_same"):
                        metrics["staged_semantic_duplicate_facts_collapsed"] += 1
                        metrics["staged_semantic_llm_same_hits"] += 1
                        subsumes = llm_result.get("subsumes")
                        prefer_incoming = subsumes == "a_subsumes_b" or (
                            subsumes is None and len(new_text) >= len(str(existing_fact.get("text", "") or "").strip())
                        )
                        accepted[accepted_idx] = _merge_semantic_duplicate_fact(
                            existing_fact,
                            incoming_fact,
                            prefer_incoming_text=prefer_incoming,
                        )
                        merged = True
                        break
                    metrics["staged_semantic_llm_different_hits"] += 1

        if not merged:
            accepted.append(dict(incoming_fact))

    return accepted, metrics


def merge_staged_payloads(state: Dict[str, Any], payload_result: Dict[str, Any]) -> Dict[str, Any]:
    """Merge a dry-run extraction payload into durable staged session state."""
    merged = dict(state or {})
    merged["carry_facts"] = list(payload_result.get("carry_facts", []) or [])
    existing_facts = list(merged.get("raw_facts", []) or [])
    incoming_facts = list(payload_result.get("raw_facts", []) or [])
    raw_facts = existing_facts + incoming_facts
    from ingest.extract import collapse_duplicate_payload_facts
    raw_facts, collapsed_duplicates = collapse_duplicate_payload_facts(raw_facts)
    existing_count = len(existing_facts)
    deduped_existing = raw_facts[: min(existing_count, len(raw_facts))]
    deduped_incoming = raw_facts[min(existing_count, len(raw_facts)) :]
    raw_facts, semantic_metrics = _collapse_staged_semantic_duplicates(deduped_existing, deduped_incoming)
    merged["raw_facts"] = raw_facts
    snippets = dict(merged.get("raw_snippets", {}) or {})
    for filename, items in (payload_result.get("raw_snippets", {}) or {}).items():
        snippets[str(filename)] = _merge_unique_strings(snippets.get(str(filename), []), list(items or []))
    merged["raw_snippets"] = snippets
    journal = dict(merged.get("raw_journal", {}) or {})
    for filename, text in (payload_result.get("raw_journal", {}) or {}).items():
        if not isinstance(text, str) or not text.strip():
            continue
        if filename in journal and journal[filename].strip():
            journal[filename] = f"{journal[filename].strip()}\n\n{text.strip()}"
        else:
            journal[filename] = text.strip()
    merged["raw_journal"] = journal
    project_logs = dict(merged.get("raw_project_logs", {}) or {})
    for project_name, items in (payload_result.get("raw_project_logs", {}) or {}).items():
        project_logs[str(project_name)] = _merge_project_log_entries(
            project_logs.get(str(project_name), []),
            list(items or []) if isinstance(items, list) else [],
        )
    merged["raw_project_logs"] = project_logs
    merged["raw_source_chunks"] = _merge_source_chunk_descriptors(
        merged.get("raw_source_chunks", []),
        payload_result.get("raw_source_chunks", []),
    )
    merged["rolling_batches"] = int(merged.get("rolling_batches", 0) or 0) + 1
    merged["facts_skipped"] = int(merged.get("facts_skipped", 0) or 0) + int(payload_result.get("facts_skipped", 0) or 0)
    merged["payload_duplicate_facts_collapsed"] = int(
        merged.get("payload_duplicate_facts_collapsed", 0) or 0
    ) + int(payload_result.get("payload_duplicate_facts_collapsed", 0) or 0) + int(collapsed_duplicates)
    for key, value in semantic_metrics.items():
        merged[key] = int(merged.get(key, 0) or 0) + int(value or 0)
    merged["carry_duplicate_facts_dropped"] = int(
        merged.get("carry_duplicate_facts_dropped", 0) or 0
    ) + int(payload_result.get("carry_duplicate_facts_dropped", 0) or 0)
    for key in ("root_chunks", "split_events", "split_child_chunks", "leaf_chunks", "chunk_calls", "deep_calls", "repair_calls"):
        merged[key] = int(merged.get(key, 0) or 0) + int(payload_result.get(key, 0) or 0)
    for key in (
        "assessment_usable",
        "assessment_nothing_usable",
        "assessment_needs_smaller_chunk",
        "unclassified_empty_payloads",
    ):
        merged[key] = int(merged.get(key, 0) or 0) + int(payload_result.get(key, 0) or 0)
    merged["max_split_depth"] = max(
        int(merged.get("max_split_depth", 0) or 0),
        int(payload_result.get("max_split_depth", 0) or 0),
    )
    merged["chunks_processed"] = int(merged.get("chunks_processed", 0) or 0) + int(payload_result.get("chunks_processed", 0) or 0)
    merged["chunks_total"] = int(merged.get("chunks_total", 0) or 0) + int(payload_result.get("chunks_total", 0) or 0)
    return merged


def _has_text_payload(value: Any) -> bool:
    """Return True when a payload container has meaningful text content."""
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_has_text_payload(nested) for nested in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_has_text_payload(nested) for nested in value)
    return False


def _merge_source_chunk_descriptors(existing: Any, incoming: Any) -> List[Dict[str, Any]]:
    """Merge staged source chunk descriptors by stable extraction-local ref."""
    merged: List[Dict[str, Any]] = []
    seen: set[str] = set()
    fallback_index = 0
    for value in list(existing or []) + list(incoming or []):
        if not isinstance(value, dict):
            continue
        ref = str(value.get("source_chunk_ref") or value.get("_source_chunk_ref") or "").strip()
        if not ref:
            ref = f"unref:{fallback_index}"
            fallback_index += 1
        if ref in seen:
            continue
        seen.add(ref)
        merged.append(dict(value))
    return merged


def staged_state_has_payload(state: Dict[str, Any]) -> bool:
    return bool(
        _has_text_payload(state.get("raw_facts"))
        or _has_text_payload(state.get("raw_snippets"))
        or _has_text_payload(state.get("raw_journal"))
        or _has_text_payload(state.get("raw_project_logs"))
    )


def clear_staged_payload_from_state(state: Dict[str, Any]) -> Dict[str, Any]:
    """Drop published rolling payload while keeping any deferred semantic tail."""
    cleaned = dict(state or {})
    cleaned["raw_facts"] = []
    cleaned["raw_snippets"] = {}
    cleaned["raw_journal"] = {}
    cleaned["raw_project_logs"] = {}
    cleaned["raw_source_chunks"] = []
    cleaned["rolling_batches"] = 0
    cleaned["facts_skipped"] = 0
    cleaned["payload_duplicate_facts_collapsed"] = 0
    cleaned["carry_duplicate_facts_dropped"] = 0
    cleaned.pop(_STAGED_PAYLOAD_PENDING_FLUSH_KEY, None)
    for key in (
        "root_chunks",
        "split_events",
        "split_child_chunks",
        "leaf_chunks",
        "chunk_calls",
        "deep_calls",
        "repair_calls",
        "chunks_processed",
        "chunks_total",
        "assessment_usable",
        "assessment_nothing_usable",
        "assessment_needs_smaller_chunk",
        "unclassified_empty_payloads",
    ):
        cleaned[key] = 0
    cleaned["max_split_depth"] = 0
    for key in _semantic_stage_metrics_defaults().keys():
        cleaned[key] = 0
    return cleaned


def _session_has_harvestable_subagents(session_id: str, adapter=None) -> bool:
    """Return True when a parent has completed child transcripts waiting."""
    try:
        import importlib
        subagent_registry = importlib.import_module("core.subagent_registry")
        if subagent_registry.get_harvestable(session_id):
            return True
    except Exception as exc:
        if _fail_hard_enabled():
            raise
        logger.warning("subagent harvest registry lookup failed for %s: %s", session_id, exc)
    try:
        discover_children_fn = getattr(adapter, "discover_subagent_children", None) if adapter is not None else None
        if not callable(discover_children_fn):
            return False
        for child in discover_children_fn(session_id):
            if str(child.get("child_id") or "").strip() and str(child.get("transcript_path") or "").strip():
                return True
    except Exception as exc:
        if _fail_hard_enabled():
            raise
        logger.warning("subagent child discovery failed for %s: %s", session_id, exc)
    return False


def _load_runtime_adapter_for_signal(label: str, session_id: str):
    """Load the active adapter for signal ownership checks."""
    try:
        from lib.adapter import get_adapter

        return get_adapter()
    except Exception as exc:
        logger.warning(
            "[%s] session %s: adapter load failed; continuing without adapter ownership checks: %s",
            label,
            session_id,
            exc,
        )
        if _fail_hard_enabled():
            raise
        return None


def write_staged_payload_flush_signals(
    *,
    adapter: str = "",
    reason: str = "staged_payload_sweep",
    exclude_session_ids: Optional[set] = None,
) -> List[Path]:
    """Queue flush signals for every session with durable staged payload.

    Lifecycle hooks can move the host into a fresh session before an async
    rolling extraction finishes. This sweep gives PreCompact a deterministic
    way to drain older staged payloads instead of waiting for idle heuristics.
    """
    excluded = set(exclude_session_ids or set())
    written: List[Path] = []
    try:
        state_files = sorted(_rolling_state_dir().glob("*.json"))
    except OSError:
        return written

    for state_file in state_files:
        session_id = state_file.stem
        if session_id in excluded or not _SESSION_ID_RE.match(session_id):
            continue
        try:
            state = read_rolling_state(session_id)
        except Exception as e:
            if _fail_hard_enabled():
                raise
            logger.warning("failed reading rolling state for staged flush sweep %s: %s", session_id, e)
            continue
        if not staged_state_has_payload(state):
            continue
        transcript_path = str(state.get("transcript_path") or "").strip()
        if not transcript_path:
            transcript_path = str(read_cursor(session_id).get("transcript_path") or "").strip()
        if not transcript_path:
            logger.warning("staged payload for %s has no transcript_path; cannot queue flush", session_id)
            continue
        written.append(
            write_signal(
                signal_type="session_end",
                session_id=session_id,
                transcript_path=transcript_path,
                adapter=adapter,
                meta={"reason": reason, "staged_payload_sweep": True},
            )
        )
    return written


def build_flush_payload(state: Dict[str, Any], tail_result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Combine staged rolling payloads with the final tail extraction payload."""
    combined = {
        "facts_stored": int(state.get("facts_stored", 0) or 0),
        "facts_skipped": int(state.get("facts_skipped", 0) or 0),
        "edges_created": 0,
        "facts": [],
        "snippets": {},
        "journal": {},
        "project_logs": {},
        "project_log_metrics": {},
        "dry_run": False,
        "raw_facts": list(state.get("raw_facts", []) or []),
        "raw_snippets": dict(state.get("raw_snippets", {}) or {}),
        "raw_journal": dict(state.get("raw_journal", {}) or {}),
        "raw_project_logs": dict(state.get("raw_project_logs", {}) or {}),
        "raw_source_chunks": list(state.get("raw_source_chunks", []) or []),
        "carry_facts": list((tail_result or {}).get("carry_facts", state.get("carry_facts", [])) or []),
        "chunks_processed": int(state.get("chunks_processed", 0) or 0),
        "chunks_total": int(state.get("chunks_total", 0) or 0),
        "carry_context_enabled": True,
        "parallel_root_workers": 1,
        "payload_duplicate_facts_collapsed": int(state.get("payload_duplicate_facts_collapsed", 0) or 0),
        "carry_duplicate_facts_dropped": int(state.get("carry_duplicate_facts_dropped", 0) or 0),
        "root_chunks": int(state.get("root_chunks", 0) or 0),
        "split_events": int(state.get("split_events", 0) or 0),
        "split_child_chunks": int(state.get("split_child_chunks", 0) or 0),
        "leaf_chunks": int(state.get("leaf_chunks", 0) or 0),
        "max_split_depth": int(state.get("max_split_depth", 0) or 0),
        "chunk_calls": int(state.get("chunk_calls", 0) or 0),
        "deep_calls": int(state.get("deep_calls", 0) or 0),
        "repair_calls": int(state.get("repair_calls", 0) or 0),
        "assessment_usable": int(state.get("assessment_usable", 0) or 0),
        "assessment_nothing_usable": int(state.get("assessment_nothing_usable", 0) or 0),
        "assessment_needs_smaller_chunk": int(state.get("assessment_needs_smaller_chunk", 0) or 0),
        "unclassified_empty_payloads": int(state.get("unclassified_empty_payloads", 0) or 0),
        "rolling_batches": int(state.get("rolling_batches", 0) or 0),
        **{key: int(state.get(key, 0) or 0) for key in _semantic_stage_metrics_defaults().keys()},
    }
    if not tail_result:
        return combined
    combined["facts_skipped"] = int(combined.get("facts_skipped", 0) or 0) + int(tail_result.get("facts_skipped", 0) or 0)
    combined["carry_duplicate_facts_dropped"] = int(
        combined.get("carry_duplicate_facts_dropped", 0) or 0
    ) + int(tail_result.get("carry_duplicate_facts_dropped", 0) or 0)
    combined["payload_duplicate_facts_collapsed"] = int(
        combined.get("payload_duplicate_facts_collapsed", 0) or 0
    ) + int(tail_result.get("payload_duplicate_facts_collapsed", 0) or 0)
    combined["raw_facts"].extend(list(tail_result.get("raw_facts", []) or []))
    from ingest.extract import collapse_duplicate_payload_facts
    combined["raw_facts"], extra_collapsed = collapse_duplicate_payload_facts(combined["raw_facts"])
    combined["payload_duplicate_facts_collapsed"] += int(extra_collapsed)
    combined["raw_source_chunks"] = _merge_source_chunk_descriptors(
        combined.get("raw_source_chunks", []),
        tail_result.get("raw_source_chunks", []),
    )
    for filename, items in (tail_result.get("raw_snippets", {}) or {}).items():
        combined["raw_snippets"][str(filename)] = _merge_unique_strings(
            combined["raw_snippets"].get(str(filename), []),
            list(items or []),
        )
    for filename, text in (tail_result.get("raw_journal", {}) or {}).items():
        if not isinstance(text, str) or not text.strip():
            continue
        if filename in combined["raw_journal"] and combined["raw_journal"][filename].strip():
            combined["raw_journal"][filename] = f"{combined['raw_journal'][filename].strip()}\n\n{text.strip()}"
        else:
            combined["raw_journal"][filename] = text.strip()
    for project_name, items in (tail_result.get("raw_project_logs", {}) or {}).items():
        combined["raw_project_logs"][str(project_name)] = _merge_project_log_entries(
            combined["raw_project_logs"].get(str(project_name), []),
            list(items or []) if isinstance(items, list) else [],
        )
    for key in (
        "chunks_processed",
        "chunks_total",
        "root_chunks",
        "split_events",
        "split_child_chunks",
        "leaf_chunks",
        "chunk_calls",
        "deep_calls",
        "repair_calls",
        "assessment_usable",
        "assessment_nothing_usable",
        "assessment_needs_smaller_chunk",
        "unclassified_empty_payloads",
    ):
        combined[key] = int(combined.get(key, 0) or 0) + int(tail_result.get(key, 0) or 0)
    for key in _semantic_stage_metrics_defaults().keys():
        combined[key] = int(combined.get(key, 0) or 0) + int(tail_result.get(key, 0) or 0)
    combined["max_split_depth"] = max(
        int(combined.get("max_split_depth", 0) or 0),
        int(tail_result.get("max_split_depth", 0) or 0),
    )
    return combined


def _merge_unique_project_logs(
    existing: Dict[str, Any],
    incoming: Dict[str, Any],
) -> Dict[str, List[Dict[str, Any]]]:
    merged = dict(existing or {})
    for project_name, items in (incoming or {}).items():
        merged[str(project_name)] = _merge_project_log_entries(
            merged.get(str(project_name), []),
            list(items or []) if isinstance(items, list) else [],
        )
    return merged


def _stamp_subagent_payload(
    payload_result: Dict[str, Any],
    *,
    source_label: str,
    child_id: str,
) -> Dict[str, Any]:
    stamped = dict(payload_result or {})
    stamped_facts: List[Dict[str, Any]] = []
    for fact in list(stamped.get("raw_facts", []) or []):
        if not isinstance(fact, dict):
            continue
        item = dict(fact)
        item["source"] = "subagent"
        item["_source_label"] = source_label
        item["_source_id"] = child_id
        stamped_facts.append(item)
    stamped["raw_facts"] = stamped_facts
    return stamped


def _append_payload_result(
    payload: Dict[str, Any],
    extra_result: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    if not isinstance(extra_result, dict):
        return payload
    payload["facts_skipped"] = int(payload.get("facts_skipped", 0) or 0) + int(extra_result.get("facts_skipped", 0) or 0)
    payload["carry_duplicate_facts_dropped"] = int(
        payload.get("carry_duplicate_facts_dropped", 0) or 0
    ) + int(extra_result.get("carry_duplicate_facts_dropped", 0) or 0)
    payload["payload_duplicate_facts_collapsed"] = int(
        payload.get("payload_duplicate_facts_collapsed", 0) or 0
    ) + int(extra_result.get("payload_duplicate_facts_collapsed", 0) or 0)
    payload["raw_facts"].extend(list(extra_result.get("raw_facts", []) or []))
    from ingest.extract import collapse_duplicate_payload_facts
    payload["raw_facts"], extra_collapsed = collapse_duplicate_payload_facts(payload["raw_facts"])
    payload["payload_duplicate_facts_collapsed"] += int(extra_collapsed)
    payload["raw_source_chunks"] = _merge_source_chunk_descriptors(
        payload.get("raw_source_chunks", []),
        extra_result.get("raw_source_chunks", []),
    )
    payload["raw_snippets"] = {
        **dict(payload.get("raw_snippets", {}) or {}),
        **{
            str(filename): _merge_unique_strings(
                dict(payload.get("raw_snippets", {}) or {}).get(str(filename), []),
                list(items or []),
            )
            for filename, items in (extra_result.get("raw_snippets", {}) or {}).items()
        },
    }
    payload["raw_journal"] = dict(payload.get("raw_journal", {}) or {})
    for filename, text in (extra_result.get("raw_journal", {}) or {}).items():
        if not isinstance(text, str) or not text.strip():
            continue
        existing = str((payload.get("raw_journal", {}) or {}).get(filename, "") or "").strip()
        if existing:
            payload["raw_journal"][filename] = f"{existing}\n\n{text.strip()}"
        else:
            payload["raw_journal"][filename] = text.strip()
    payload["raw_project_logs"] = _merge_unique_project_logs(
        dict(payload.get("raw_project_logs", {}) or {}),
        dict(extra_result.get("raw_project_logs", {}) or {}),
    )
    for key in (
        "chunks_processed",
        "chunks_total",
        "root_chunks",
        "split_events",
        "split_child_chunks",
        "leaf_chunks",
        "chunk_calls",
        "deep_calls",
        "repair_calls",
        "assessment_usable",
        "assessment_nothing_usable",
        "assessment_needs_smaller_chunk",
        "unclassified_empty_payloads",
    ):
        payload[key] = int(payload.get(key, 0) or 0) + int(extra_result.get(key, 0) or 0)
    for key in _semantic_stage_metrics_defaults().keys():
        payload[key] = int(payload.get(key, 0) or 0) + int(extra_result.get(key, 0) or 0)
    payload["max_split_depth"] = max(
        int(payload.get("max_split_depth", 0) or 0),
        int(extra_result.get("max_split_depth", 0) or 0),
    )
    return payload


def _rolling_metrics_path() -> Path:
    path = _instance_root() / "logs" / "daemon" / "rolling-extraction.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _usage_events_path() -> Path:
    return _instance_root() / "logs" / "llm-usage-events.jsonl"


_USAGE_TOTAL_KEYS = (
    "calls",
    "input_tokens",
    "output_tokens",
    "fast_calls",
    "fast_input_tokens",
    "fast_output_tokens",
    "deep_calls",
    "deep_input_tokens",
    "deep_output_tokens",
)
_USAGE_TOTALS_CACHE: Dict[str, Any] = {
    "path": "",
    "device": None,
    "inode": None,
    "offset": 0,
    "totals": None,
}


def _zero_usage_totals() -> Dict[str, int]:
    return {
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "fast_calls": 0,
        "fast_input_tokens": 0,
        "fast_output_tokens": 0,
        "deep_calls": 0,
        "deep_input_tokens": 0,
        "deep_output_tokens": 0,
    }


def _apply_usage_event_row(totals: Dict[str, int], row: Dict[str, Any]) -> None:
    tier = str(row.get("tier", "") or "").strip().lower()
    input_tokens = int(row.get("input_tokens", 0) or 0)
    output_tokens = int(row.get("output_tokens", 0) or 0)
    totals["calls"] += 1
    totals["input_tokens"] += input_tokens
    totals["output_tokens"] += output_tokens
    if tier in ("fast", "deep"):
        totals[f"{tier}_calls"] += 1
        totals[f"{tier}_input_tokens"] += input_tokens
        totals[f"{tier}_output_tokens"] += output_tokens


def _read_usage_totals() -> Dict[str, int]:
    path = _usage_events_path()
    if not path.is_file():
        if _USAGE_TOTALS_CACHE.get("path") == str(path):
            _USAGE_TOTALS_CACHE.update({
                "device": None,
                "inode": None,
                "offset": 0,
                "totals": None,
            })
        return _zero_usage_totals()
    try:
        stat = path.stat()
        device = getattr(stat, "st_dev", None)
        inode = getattr(stat, "st_ino", None)
        cached_path = str(_USAGE_TOTALS_CACHE.get("path") or "")
        cached_device = _USAGE_TOTALS_CACHE.get("device")
        cached_inode = _USAGE_TOTALS_CACHE.get("inode")
        cached_offset = int(_USAGE_TOTALS_CACHE.get("offset", 0) or 0)
        cached_totals = _USAGE_TOTALS_CACHE.get("totals")
        if (
            cached_path == str(path)
            and cached_device == device
            and cached_inode == inode
            and isinstance(cached_totals, dict)
            and int(stat.st_size) >= cached_offset
        ):
            totals = {key: int(cached_totals.get(key, 0) or 0) for key in _USAGE_TOTAL_KEYS}
            offset = cached_offset
        else:
            totals = _zero_usage_totals()
            offset = 0

        with path.open("rb") as fh:
            fh.seek(offset)
            while True:
                raw_line = fh.readline()
                if not raw_line:
                    break
                if not raw_line.endswith(b"\n"):
                    break
                offset = fh.tell()
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                _apply_usage_event_row(totals, row)
        _USAGE_TOTALS_CACHE.update({
            "path": str(path),
            "device": device,
            "inode": inode,
            "offset": offset,
            "totals": dict(totals),
        })
    except OSError:
        return _zero_usage_totals()
    return totals


def _usage_delta(before: Dict[str, int], after: Dict[str, int]) -> Dict[str, int]:
    keys = set(before.keys()) | set(after.keys())
    return {key: int(after.get(key, 0) or 0) - int(before.get(key, 0) or 0) for key in keys}


def write_rolling_metric(event: str, session_id: str, **data: Any) -> None:
    payload = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        "session_id": session_id,
    }
    payload.update(data)
    try:
        with _rolling_metrics_path().open("a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.write(json.dumps(payload) + "\n")
                fh.flush()
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        logger.warning("rolling metric write failed for %s: %s", session_id, exc)
        if _fail_hard_enabled():
            raise


def _rolling_debug_dump_enabled() -> bool:
    env_value = str(os.environ.get("QUAID_ROLLING_DEBUG_DUMP", "") or "").strip().lower()
    if env_value in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return True
    try:
        return (_instance_root() / "data" / "rolling-debug.enabled").is_file()
    except Exception:
        return False


def _rolling_debug_dir() -> Path:
    default_dir = _instance_root() / "logs" / "daemon" / "rolling-debug"

    def _contained_debug_dir(raw_path: str, source: str) -> Optional[Path]:
        try:
            candidate = Path(raw_path).expanduser().resolve()
            candidate.relative_to(_quaid_home().resolve())
            return candidate
        except (OSError, ValueError) as exc:
            logger.warning(
                "[rolling-debug] ignoring %s outside QUAID_HOME: %s (%s)",
                source,
                raw_path,
                exc,
            )
            return None

    raw = str(os.environ.get("QUAID_ROLLING_DEBUG_DIR", "") or "").strip()
    if raw:
        debug_dir = _contained_debug_dir(raw, "QUAID_ROLLING_DEBUG_DIR")
        if debug_dir is not None:
            return debug_dir
    try:
        flag_path = _instance_root() / "data" / "rolling-debug.enabled"
        if flag_path.is_file():
            configured = flag_path.read_text(encoding="utf-8").strip()
            if configured:
                debug_dir = _contained_debug_dir(configured, str(flag_path))
                if debug_dir is not None:
                    return debug_dir
    except Exception as exc:
        logger.warning("[rolling-debug] failed reading debug directory flag; using default: %s", exc)
        if _fail_hard_enabled():
            raise
    return default_dir


def _rolling_debug_markers() -> List[str]:
    raw = str(os.environ.get("QUAID_ROLLING_DEBUG_MARKERS", "") or "").strip()
    if raw:
        return [item.strip() for item in raw.split(",") if item.strip()]
    return [
        "Baxter",
        "orange linen notebook",
        "Emília Rosa",
        "Emilia Rosa",
        "tangerine-emilia",
    ]


def _rolling_debug_marker_hits(text: str) -> Dict[str, List[int]]:
    haystack = str(text or "")
    lowered = haystack.casefold()
    hits: Dict[str, List[int]] = {}
    for marker in _rolling_debug_markers():
        needle = marker.casefold()
        if not needle:
            continue
        positions: List[int] = []
        start = 0
        while True:
            idx = lowered.find(needle, start)
            if idx < 0:
                break
            positions.append(idx)
            start = idx + max(1, len(needle))
        if positions:
            hits[marker] = positions
    return hits


def _rolling_debug_fact_rows(facts: Any, *, fallback_session_id: str = "") -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for index, fact in enumerate(list(facts or []), start=1):
        if not isinstance(fact, dict):
            continue
        text = str(fact.get("text") or "").strip()
        source_session_id = (
            str(fact.get("source_session_id") or "").strip()
            or str(fact.get("session_id") or "").strip()
            or str(fact.get("source_id") or "").strip()
            or str(fact.get("_source_id") or "").strip()
            or fallback_session_id
        )
        row = {
            "index": index,
            "text": text,
            "source_session_id": source_session_id,
            "status": str(fact.get("status") or "").strip(),
            "reason": str(fact.get("reason") or "").strip(),
            "source": str(fact.get("source") or "").strip(),
            "source_label": str(fact.get("_source_label") or "").strip(),
            "source_id": str(fact.get("_source_id") or fact.get("source_id") or "").strip(),
            "speaker": str(fact.get("speaker") or "").strip(),
            "category": str(fact.get("category") or "").strip(),
            "domains": fact.get("domains") if isinstance(fact.get("domains"), list) else [],
            "marker_hits": _rolling_debug_marker_hits(text),
        }
        rows.append(row)
    return rows


def _write_rolling_debug_dump(
    event: str,
    session_id: str,
    *,
    text: str = "",
    facts: Any = None,
    storage_facts: Any = None,
    **data: Any,
) -> None:
    if not _rolling_debug_dump_enabled():
        return
    debug_dir = _rolling_debug_dir()
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    safe_session = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(session_id or "unknown")).strip("-") or "unknown"
    unique = f"{int(time.time() * 1000)}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    payload: Dict[str, Any] = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        "instance": _instance_id(),
        "session_id": session_id,
        **data,
    }
    raw_text = str(text or "")
    if raw_text:
        text_path = debug_dir / f"quaid-rolling-debug-{timestamp}-{safe_session}-{event}-{unique}.txt"
        payload.update(
            {
                "text_path": str(text_path),
                "text_byte_len": len(raw_text.encode("utf-8", "replace")),
                "text_preview": raw_text[:4000],
                "text_marker_hits": _rolling_debug_marker_hits(raw_text),
            }
        )
    if facts is not None:
        payload["facts"] = _rolling_debug_fact_rows(facts, fallback_session_id=session_id)
    if storage_facts is not None:
        payload["storage_facts"] = _rolling_debug_fact_rows(storage_facts, fallback_session_id=session_id)

    jsonl_path = debug_dir / f"quaid-rolling-debug-{safe_session}.jsonl"
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        if raw_text:
            text_path.write_text(raw_text, encoding="utf-8")
        with jsonl_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        logger.info(
            "[rolling-debug] %s session %s wrote %s%s",
            event,
            session_id,
            jsonl_path,
            f" text={payload.get('text_path')}" if raw_text else "",
        )
    except Exception as exc:
        logger.warning("[rolling-debug] failed writing %s for session %s: %s", event, session_id, exc)


def _bucket_fact_reason(status: str, reason: str) -> str:
    status_text = str(status or "").strip().lower()
    reason_text = str(reason or "").strip()
    if status_text == "duplicate":
        return "duplicate"
    if status_text == "blocked":
        return "blocked"
    if reason_text.startswith("unsupported domains"):
        return "unsupported domains"
    if reason_text:
        return reason_text
    return status_text or "unknown"


def _summarize_fact_result_buckets(facts: Any) -> Dict[str, Dict[str, int]]:
    status_counts: Dict[str, int] = {}
    skip_buckets: Dict[str, int] = {}
    for fact in list(facts or []):
        if not isinstance(fact, dict):
            continue
        status = str(fact.get("status", "") or "").strip().lower() or "unknown"
        status_counts[status] = int(status_counts.get(status, 0) or 0) + 1
        if status in {"duplicate", "skipped", "blocked", "failed"}:
            bucket = _bucket_fact_reason(status, str(fact.get("reason", "") or ""))
            skip_buckets[bucket] = int(skip_buckets.get(bucket, 0) or 0) + 1
    return {
        "status_counts": status_counts,
        "skip_buckets": skip_buckets,
    }


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
        if _should_raise_transcript_stat_error(transcript_path, e):
            raise
        logger.error("failed reading transcript %s: %s", transcript_path, e)
    return lines


class _TranscriptParseError(RuntimeError):
    """Raised when adapter transcript parsing fails for a semantic buffer window."""


_TRANSCRIPT_PARSE_FAILURE_PATH_KEY = "transcript_parse_failure_path"
_TRANSCRIPT_PARSE_FAILURE_OFFSET_KEY = "transcript_parse_failure_offset"
_TRANSCRIPT_PARSE_FAILURE_END_LINE_KEY = "transcript_parse_failure_end_line"


def _parse_transcript_lines(lines: List[str], adapter=None, *, raise_on_parse_error: bool = False) -> str:
    """Parse raw session JSONL lines into the semantic transcript text the model sees."""
    if not lines:
        return ""
    if adapter is None:
        return "".join(lines)

    tmp_dir = _tmp_dir()
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8", dir=str(tmp_dir)
        ) as tmp:
            tmp.writelines(lines)
            tmp_path = tmp.name
        parsed = adapter.parse_session_jsonl(Path(tmp_path))
        return str(parsed or "").strip()
    except Exception as exc:
        logger.warning("failed parsing transcript window for semantic rolling budget: %s", exc)
        if raise_on_parse_error:
            raise _TranscriptParseError("failed parsing transcript window for semantic rolling budget") from exc
        return ""
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _adapter_id_for_semantic_window(adapter) -> str:
    try:
        adapter_id = getattr(adapter, "adapter_id", None)
        if callable(adapter_id):
            return str(adapter_id() or "").strip().lower()
    except Exception:
        return ""
    return str(getattr(adapter, "adapter_id", "") or "").strip().lower()


class _SingleLineAdapterTranscriptAccumulator:
    """Build a semantic transcript incrementally for adapters with row-local parsing."""

    def __init__(self, adapter, *, raise_on_parse_error: bool = False) -> None:
        self._adapter = adapter
        self._raise_on_parse_error = raise_on_parse_error
        self._parsed_blocks: List[str] = []

    def append_line(self, line: str) -> str:
        parsed = _parse_transcript_lines(
            [line],
            adapter=self._adapter,
            raise_on_parse_error=self._raise_on_parse_error,
        )
        if parsed:
            self._parsed_blocks.append(parsed)
        return "\n\n".join(self._parsed_blocks).strip()


class _CodexTranscriptAccumulator:
    """Incremental equivalent of CodexAdapter.parse_session_jsonl for window sizing."""

    def __init__(self, adapter) -> None:
        self._adapter = adapter
        self._messages: List[Dict[str, Any]] = []
        self._fallback_messages: List[Dict[str, Any]] = []
        self._session_source_type = ""
        self._line_index = -1

    def _row_timestamp(self, row: Dict[str, Any]) -> str:
        row_timestamp = getattr(self._adapter, "_row_timestamp", None)
        if callable(row_timestamp):
            return str(row_timestamp(row) or "").strip()
        return str(row.get("timestamp") or "").strip()

    def _build_transcript(self, messages: List[Dict[str, Any]]) -> str:
        build_timestamped = getattr(self._adapter, "_build_timestamped_transcript", None)
        if callable(build_timestamped):
            return str(build_timestamped(messages) or "").strip()
        return str(self._adapter.build_transcript(messages) or "").strip()

    @staticmethod
    def _message_text_from_content(content: Any) -> str:
        if isinstance(content, list):
            text_parts = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text") or item.get("input_text") or item.get("output_text") or "").strip()
                if text:
                    text_parts.append(text)
            return "\n".join(text_parts).strip()
        if isinstance(content, str):
            return content.strip()
        return ""

    def append_line(self, line: str) -> str:
        self._line_index += 1
        raw = str(line or "").strip()
        if not raw:
            return self.current_transcript()
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return self.current_transcript()
        if not isinstance(obj, dict):
            return self.current_transcript()

        record_type = str(obj.get("type") or "").strip()
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        row_timestamp = self._row_timestamp(obj)

        if record_type == "session_meta":
            source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
            subagent = source.get("subagent") if isinstance(source, dict) else {}
            self._session_source_type = ""
            if isinstance(subagent, dict) and isinstance(subagent.get("thread_spawn"), dict):
                self._session_source_type = "subagent"
            return self.current_transcript()

        if record_type == "event_msg":
            payload_type = str(payload.get("type") or "").strip()
            if payload_type in ("user_message", "agent_message"):
                role = "user" if payload_type == "user_message" else "assistant"
                text = str(payload.get("message") or "").strip()
                if text:
                    self._messages.append({
                        "role": role,
                        "content": text,
                        "source_type": self._session_source_type,
                        "timestamp": row_timestamp,
                        "line_index": self._line_index,
                    })
            return self.current_transcript()

        if record_type == "response_item" and str(payload.get("type") or "").strip() == "message":
            role = str(payload.get("role") or "").strip().lower()
            if role in ("user", "assistant"):
                text = self._message_text_from_content(payload.get("content", []))
                if text:
                    self._fallback_messages.append({
                        "role": role,
                        "content": text,
                        "source_type": self._session_source_type,
                        "timestamp": row_timestamp,
                        "line_index": self._line_index,
                    })
        return self.current_transcript()

    def current_transcript(self) -> str:
        if self._messages:
            selected = sorted(
                self._messages
                + [
                    message
                    for message in self._fallback_messages
                    if message.get("role") == "user"
                ],
                key=lambda message: int(message.get("line_index", 0) or 0),
            )
        else:
            selected = list(self._fallback_messages)

        deduped = []
        last_pair = None
        for message in selected:
            pair = (message.get("role"), message.get("content"))
            if pair == last_pair:
                continue
            deduped.append(message)
            last_pair = pair
        return self._build_transcript(deduped)


def _transcript_window_accumulator(adapter, *, raise_on_parse_error: bool = False):
    adapter_id = _adapter_id_for_semantic_window(adapter)
    if adapter_id == "codex":
        return _CodexTranscriptAccumulator(adapter)
    if adapter_id in {"claude-code", "openclaw", "standalone"}:
        return _SingleLineAdapterTranscriptAccumulator(
            adapter,
            raise_on_parse_error=raise_on_parse_error,
        )
    return None


def _handle_transcript_parse_failure(
    exc: _TranscriptParseError,
    *,
    lines: List[str],
    state: Dict[str, Any],
    transcript_path: str,
    start_line: int,
    session_id: str,
    owner_id: str,
    label: str,
) -> Tuple[Dict[str, Any], Dict[str, int]]:
    if _fail_hard_enabled():
        raise RuntimeError("Failed parsing transcript window while failHard is enabled") from exc
    raw_lines = list(lines or [])
    if not raw_lines:
        raw_lines = read_transcript_slice(transcript_path, start_line)
    transcript_text = "".join(raw_lines)
    preserved_state = dict(state or {})
    failure_end_line = int(start_line or 0) + len(raw_lines)
    raw_failure_offset = preserved_state.get(_TRANSCRIPT_PARSE_FAILURE_OFFSET_KEY)
    raw_failure_end_line = preserved_state.get(_TRANSCRIPT_PARSE_FAILURE_END_LINE_KEY)
    try:
        prior_failure_offset = int(raw_failure_offset) if raw_failure_offset is not None else -1
    except Exception:
        prior_failure_offset = -1
    try:
        prior_failure_end_line = int(raw_failure_end_line) if raw_failure_end_line is not None else -1
    except Exception:
        prior_failure_end_line = -1
    already_deferred = (
        str(preserved_state.get(_TRANSCRIPT_PARSE_FAILURE_PATH_KEY) or "") == str(transcript_path or "")
        and prior_failure_offset == int(start_line or 0)
        and prior_failure_end_line >= failure_end_line
    )
    deferred_saved = already_deferred
    if not already_deferred:
        logger.error(
            "[%s] session %s: failed parsing transcript window; saving deferred extraction and preserving offset %d",
            label,
            session_id,
            start_line,
        )
        deferred_saved = _save_deferred_extraction(
            session_id=session_id or "unknown",
            transcript_text=transcript_text,
            owner_id=owner_id or _get_owner_id(),
            label=label,
            reason="transcript_parse_failure",
        )
    if deferred_saved:
        preserved_state[_TRANSCRIPT_PARSE_FAILURE_PATH_KEY] = str(transcript_path or "")
        preserved_state[_TRANSCRIPT_PARSE_FAILURE_OFFSET_KEY] = int(start_line or 0)
        preserved_state[_TRANSCRIPT_PARSE_FAILURE_END_LINE_KEY] = failure_end_line
    else:
        preserved_state.pop(_TRANSCRIPT_PARSE_FAILURE_PATH_KEY, None)
        preserved_state.pop(_TRANSCRIPT_PARSE_FAILURE_OFFSET_KEY, None)
        preserved_state.pop(_TRANSCRIPT_PARSE_FAILURE_END_LINE_KEY, None)
    return preserved_state, {
        "raw_lines_added": len(raw_lines),
        "semantic_chars_added": 0,
        "semantic_tokens_added": 0,
        "buffered_line_offset": int(start_line or 0),
        "parse_failed": 1,
    }


def _append_semantic_buffer(state: Dict[str, Any], parsed_text: str, line_offset: int) -> Dict[str, Any]:
    """Append parsed transcript text to the persisted semantic rolling buffer."""
    from lib.tokens import estimate_tokens

    merged = dict(state or {})
    existing = str(merged.get("semantic_buffer", "") or "").strip()
    incoming = str(parsed_text or "").strip()
    before_tokens = int(merged.get("semantic_buffer_tokens", 0) or 0)
    if existing and incoming:
        combined = f"{existing}\n\n{incoming}"
    else:
        combined = existing or incoming
    merged["semantic_buffer"] = combined
    merged["semantic_buffer_tokens"] = estimate_tokens(combined) if combined else 0
    if incoming:
        merged["semantic_buffer_prior"] = existing
        merged["semantic_buffer_tail"] = incoming
        merged["semantic_buffer_prior_tokens"] = before_tokens
        merged["semantic_buffer_tail_tokens"] = estimate_tokens(incoming)
    merged["buffered_line_offset"] = max(
        int(merged.get("buffered_line_offset", 0) or 0),
        int(line_offset or 0),
    )
    merged["processed_line_offset"] = int(merged["buffered_line_offset"])
    return merged


def _semantic_buffer_has_content(state: Dict[str, Any]) -> bool:
    return bool(str((state or {}).get("semantic_buffer", "") or "").strip())


def _reset_semantic_buffer_for_source(state: Dict[str, Any], buffer_transcript_path: str) -> Dict[str, Any]:
    """Reset source-relative semantic buffering while preserving staged payloads."""
    reset = dict(state or {})
    reset["buffer_transcript_path"] = str(buffer_transcript_path or "")
    reset["buffered_line_offset"] = 0
    reset["processed_line_offset"] = 0
    reset["semantic_buffer"] = ""
    reset["semantic_buffer_tokens"] = 0
    reset["semantic_buffer_prior"] = ""
    reset["semantic_buffer_tail"] = ""
    reset["semantic_buffer_prior_tokens"] = 0
    reset["semantic_buffer_tail_tokens"] = 0
    reset.pop(_INTERNAL_CURSOR_UNFROZEN_PENDING_FLUSH_KEY, None)
    return reset


def _record_staged_source_transcript_path(
    session_id: str,
    staged_state: Dict[str, Any],
    transcript_path: str,
    signal_meta: Dict[str, Any],
) -> Dict[str, Any]:
    if not staged_state_has_payload(staged_state):
        return staged_state
    if not _is_daemon_owned_transcript_snapshot_path(transcript_path):
        return staged_state
    source_path = str(signal_meta.get("source_transcript_path") or "").strip()
    if not source_path:
        return staged_state
    updated = dict(staged_state or {})
    if str(updated.get("source_transcript_path") or "").strip() == source_path:
        return staged_state
    updated["source_transcript_path"] = source_path
    write_rolling_state(session_id, updated)
    return updated


def _processed_rolling_snapshot_can_be_cleaned(transcript_path: str, staged_state: Dict[str, Any]) -> bool:
    if not staged_state_has_payload(staged_state):
        return True
    if not _is_daemon_rolling_transcript_snapshot_path(transcript_path):
        return True
    if _pending_signal_references_transcript_path(transcript_path):
        return False
    source_path = str((staged_state or {}).get("source_transcript_path") or "").strip()
    if not source_path or source_path == str(transcript_path or "").strip():
        return False
    return os.path.isfile(source_path)


def _rolling_state_has_pending_content(state: Dict[str, Any]) -> bool:
    return bool(staged_state_has_payload(state or {}) or _semantic_buffer_has_content(state or {}))


def _resolve_existing_transcript_fallback_for_signal(
    *,
    label: str,
    session_id: str,
    transcript_path: str,
    signal_meta: Dict[str, Any],
    cursor_data: Dict[str, Any],
    staged_state: Dict[str, Any],
    adapter=None,
) -> Tuple[str, str]:
    """Return an existing fallback transcript path before classifying/reading it."""
    for fallback_source, fallback_path in (
        ("signal_meta_source", str(signal_meta.get("source_transcript_path") or "").strip()),
        ("cursor", str(cursor_data.get("transcript_path") or "").strip()),
        ("rolling_state", str(staged_state.get("transcript_path") or "").strip()),
        ("rolling_state_source", str(staged_state.get("source_transcript_path") or "").strip()),
        ("rolling_state_buffer", str(staged_state.get("buffer_transcript_path") or "").strip()),
    ):
        if fallback_path and os.path.isfile(fallback_path):
            return fallback_path, fallback_source

    if adapter is not None:
        get_session_path = getattr(adapter, "get_session_path", None)
        if callable(get_session_path):
            try:
                adapter_path = get_session_path(session_id)
            except Exception as exc:
                logger.warning(
                    "[%s] failed resolving adapter transcript path for %s: %s",
                    label,
                    session_id,
                    exc,
                )
                if _fail_hard_enabled():
                    raise
            else:
                adapter_path_str = str(adapter_path or "").strip()
                if adapter_path_str and os.path.isfile(adapter_path_str):
                    return adapter_path_str, "adapter"

    if transcript_path and str(transcript_path).endswith(".jsonl"):
        import glob as _glob
        reset_pattern = str(transcript_path)[:-len(".jsonl")] + ".jsonl.reset.*"
        reset_candidates = sorted(_glob.glob(reset_pattern))
        if reset_candidates:
            return reset_candidates[-1], "reset_backup"

    return "", ""


def _read_rolling_state_for_signal(
    session_id: str,
    transcript_path: str,
    *,
    source_cursor_key: str = "",
) -> Tuple[Dict[str, Any], str]:
    """Read rolling state, tolerating host session-id aliases for one transcript source.

    Codex can report lifecycle signals with the bare UUID while rolling scans use the
    full rollout-* session id. Cursors already key by transcript source; staged rolling
    state needs the same source compatibility so lifecycle signals can drain residual
    semantic buffers left by rolling-stage flushes.
    """
    normalized_session_id = _validate_session_id(session_id)
    direct_state = read_rolling_state(normalized_session_id)
    if _rolling_state_has_pending_content(direct_state):
        return direct_state, normalized_session_id

    raw_transcript_path = str(transcript_path or "").strip()
    wanted_source_cursor_key = str(source_cursor_key or "").strip()
    wanted_session_uuid = _session_uuid_for_alias_match(normalized_session_id)
    wanted_path_uuid = _session_uuid_for_alias_match(raw_transcript_path)
    wanted_uuid = wanted_session_uuid or wanted_path_uuid
    if not raw_transcript_path and not wanted_uuid and not wanted_source_cursor_key:
        return direct_state, normalized_session_id

    wanted_canonical_path = (
        _canonicalize_transcript_source_path(raw_transcript_path)
        if raw_transcript_path
        else ""
    )
    try:
        wanted_identity = (
            _signal_source_identity(normalized_session_id, raw_transcript_path)
            if raw_transcript_path
            else ""
        )
    except Exception as exc:
        logger.warning("[rolling] source identity lookup failed for signal %s: %s", normalized_session_id, exc)
        if _fail_hard_enabled():
            raise RuntimeError("rolling state signal identity lookup failed") from exc
        wanted_identity = ""

    try:
        state_files = sorted(_rolling_state_dir().glob("*.json"))
    except OSError:
        return direct_state, normalized_session_id

    for state_file in state_files:
        state_key = _validate_session_id(state_file.stem)
        if state_key == normalized_session_id:
            continue
        state = read_rolling_state(state_key)
        if not _rolling_state_has_pending_content(state):
            continue
        state_transcript_path = str(state.get("transcript_path") or "").strip()
        if not state_transcript_path:
            continue
        state_uuid = (
            _session_uuid_for_alias_match(state_key)
            or _session_uuid_for_alias_match(str(state.get("session_id") or ""))
            or _session_uuid_for_alias_match(state_transcript_path)
        )
        # A Codex lifecycle signal may carry the old session UUID while the
        # transcript path already points at the new /new session. The explicit
        # signal UUID is the stronger ownership signal for residual buffers.
        if wanted_session_uuid and state_uuid and state_uuid != wanted_session_uuid:
            continue
        # UUID match is authoritative here; canonical path may already point
        # at the new /new session by the time the old lifecycle signal lands.
        if wanted_uuid and state_uuid and wanted_uuid == state_uuid:
            return state, _validate_session_id(str(state.get("session_id") or state_key))
        state_canonical_path = _canonicalize_transcript_source_path(state_transcript_path)
        if wanted_canonical_path and state_canonical_path == wanted_canonical_path:
            return state, _validate_session_id(str(state.get("session_id") or state_key))
        try:
            state_identity = _signal_source_identity(
                str(state.get("session_id") or state_key),
                state_transcript_path,
                staged_state=state,
            )
        except Exception as exc:
            logger.warning("[rolling] source identity lookup failed for staged state %s: %s", state_key, exc)
            if _fail_hard_enabled():
                raise RuntimeError("rolling staged-state identity lookup failed") from exc
            state_identity = ""
        if wanted_identity and state_identity == wanted_identity:
            return state, _validate_session_id(str(state.get("session_id") or state_key))
        try:
            state_source_cursor_key = _signal_source_cursor_key(
                str(state.get("session_id") or state_key),
                state_transcript_path,
                staged_state=state,
            )
        except Exception as exc:
            logger.warning("[rolling] source cursor key lookup failed for staged state %s: %s", state_key, exc)
            if _fail_hard_enabled():
                raise RuntimeError("rolling staged-state source cursor lookup failed") from exc
            state_source_cursor_key = ""
        if wanted_source_cursor_key and state_source_cursor_key == wanted_source_cursor_key:
            return state, _validate_session_id(str(state.get("session_id") or state_key))

    return direct_state, normalized_session_id


def _session_uuid_for_alias_match(value: str) -> str:
    match = _SESSION_ID_UUID_RE.search(str(value or ""))
    return match.group(0).lower() if match else ""


def _buffer_transcript_tail(
    transcript_path: str,
    start_line: int,
    state: Dict[str, Any],
    *,
    adapter=None,
    max_tokens: Optional[int] = None,
    max_lines: int = 0,
    include_threshold_crossing_semantic_row: bool = False,
    session_id: str = "",
    owner_id: str = "",
    label: str = "daemon-rolling",
) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """Parse new raw session lines into the semantic rolling buffer."""
    if max_tokens is not None and int(max_tokens or 0) > 0:
        try:
            lines = read_transcript_token_window(
                transcript_path,
                start_line,
                int(max_tokens),
                int(max_lines or 0),
                adapter=adapter,
                include_threshold_crossing_semantic_row=include_threshold_crossing_semantic_row,
                raise_on_parse_error=True,
            )
        except _TranscriptParseError as exc:
            return _handle_transcript_parse_failure(
                exc,
                lines=[],
                state=state,
                transcript_path=transcript_path,
                start_line=start_line,
                session_id=session_id,
                owner_id=owner_id,
                label=label,
            )
    else:
        lines = read_transcript_slice(transcript_path, start_line)
    metrics = {
        "raw_lines_added": len(lines),
        "semantic_chars_added": 0,
        "semantic_tokens_added": 0,
        "buffered_line_offset": int(start_line or 0),
    }
    if not lines:
        return dict(state or {}), metrics

    before_tokens = int((state or {}).get("semantic_buffer_tokens", 0) or 0)
    try:
        parsed_text = _parse_transcript_lines(lines, adapter=adapter, raise_on_parse_error=True)
    except _TranscriptParseError as exc:
        return _handle_transcript_parse_failure(
            exc,
            lines=lines,
            state=state,
            transcript_path=transcript_path,
            start_line=start_line,
            session_id=session_id,
            owner_id=owner_id,
            label=label,
        )
    merged = _append_semantic_buffer(state, parsed_text, start_line + len(lines))
    merged["transcript_path"] = str(transcript_path or merged.get("transcript_path", "") or "")
    merged.pop(_TRANSCRIPT_PARSE_FAILURE_PATH_KEY, None)
    merged.pop(_TRANSCRIPT_PARSE_FAILURE_OFFSET_KEY, None)
    merged.pop(_TRANSCRIPT_PARSE_FAILURE_END_LINE_KEY, None)
    metrics["semantic_chars_added"] = len(str(parsed_text or "").strip())
    metrics["semantic_tokens_added"] = max(
        0,
        int(merged.get("semantic_buffer_tokens", 0) or 0) - before_tokens,
    )
    metrics["buffered_line_offset"] = int(
        merged.get("buffered_line_offset", start_line + len(lines)) or 0
    )
    return merged, metrics


def _stage_semantic_buffer_payload(
    *,
    session_id: str,
    signal_type: str,
    transcript_path: str,
    label: str,
    owner: str,
    staged_state: Dict[str, Any],
    buffered_line_offset: int,
    new_lines: List[str],
    semantic_buffer_metrics: Dict[str, int],
    chunk_budget: int,
    chunk_line_budget: int,
) -> Dict[str, Any]:
    transcript_text = str(staged_state.get("semantic_buffer", "") or "").strip()
    staged_state = dict(staged_state or {})
    if not transcript_text:
        staged_state["semantic_buffer"] = ""
        staged_state["semantic_buffer_tokens"] = 0
        staged_state["buffered_line_offset"] = buffered_line_offset
        staged_state["processed_line_offset"] = buffered_line_offset
        staged_state["transcript_path"] = transcript_path
        write_rolling_state(session_id, staged_state)
        return staged_state

    from ingest.extract import extract_from_transcript
    from lib.tokens import estimate_tokens

    stage_text = transcript_text
    residual_text = ""
    prior_text = str(staged_state.get("semantic_buffer_prior", "") or "").strip()
    tail_text = str(staged_state.get("semantic_buffer_tail", "") or "").strip()
    prior_tokens = int(staged_state.get("semantic_buffer_prior_tokens", 0) or 0)
    if (
        signal_type == "rolling"
        and prior_text
        and tail_text
        and prior_tokens >= _rolling_prior_buffer_threshold(chunk_budget)
        and int(staged_state.get("semantic_buffer_tokens", 0) or 0) >= _rolling_ready_threshold(chunk_budget)
    ):
        stage_text = prior_text
        residual_text = tail_text

    stage_started_at = time.time()
    line_chars = int(semantic_buffer_metrics.get("semantic_chars_added", 0) or len(transcript_text))
    line_estimated_tokens = int(
        staged_state.get("semantic_buffer_tokens", 0)
        or semantic_buffer_metrics.get("semantic_tokens_added", 0)
        or 0
    )
    max_line_chars = max((len(line) for line in new_lines), default=0)
    max_line_estimated_tokens = max((max(1, len(line) // 4) for line in new_lines), default=0)
    carry_facts_in = len(staged_state.get("carry_facts", []) or [])
    _reset_daemon_llm_usage_budget()
    stage_result = extract_from_transcript(
        transcript=stage_text,
        owner_id=owner,
        label=label,
        session_id=session_id,
        dry_run=True,
        carry_facts=list(staged_state.get("carry_facts", []) or []),
        chunk_tokens_override=_daemon_extract_chunk_tokens(chunk_budget),
        llm_timeout_seconds=_daemon_extract_llm_timeout_seconds(),
        llm_slot_wait_timeout_seconds=_daemon_extract_llm_slot_wait_seconds(),
        llm_max_retries=_daemon_extract_llm_max_retries(),
        raise_on_llm_failure=True,
    )
    chunks_processed = int(stage_result.get("chunks_processed", 0) or 0)
    chunks_total = int(stage_result.get("chunks_total", 0) or 0)
    unclassified_empty = int(stage_result.get("unclassified_empty_payloads", 0) or 0)
    if unclassified_empty > 0:
        logger.warning(
            "[%s] session %s: %d/%d chunks returned empty payloads "
            "(model responded but no extractable signal); counting as processed",
            label, session_id, unclassified_empty, chunks_total,
        )
    failed_chunks = chunks_total - chunks_processed - unclassified_empty
    if failed_chunks > 0:
        if _fail_hard_enabled():
            raise RuntimeError(
                f"[{label}] session {session_id}: {failed_chunks}/{chunks_total} "
                "chunks failed extraction while failHard is enabled"
            )
        logger.error(
            "[%s] session %s: %d/%d chunks failed extraction "
            "(non-provider failure); saving transcript for janitor recovery",
            label, session_id, failed_chunks, chunks_total,
        )
        # Deferred retry owns this failed window; staging partial facts here
        # would publish them again when janitor later retries the same text.
        _save_deferred_extraction(
            session_id=session_id,
            transcript_text=stage_text,
            owner_id=owner,
            label=label,
            reason=f"non_provider_failure_{failed_chunks}_of_{chunks_total}_chunks",
        )
        stage_embedding_stats = _warm_payload_embeddings([])
    else:
        stage_embedding_stats = _warm_payload_embeddings(stage_result.get("raw_facts", []) or [])
        staged_state = merge_staged_payloads(staged_state, stage_result)
    _write_rolling_debug_dump(
        "rolling_stage_extract",
        session_id,
        text=stage_text,
        facts=stage_result.get("raw_facts", []) or [],
        label=label,
        signal_type=signal_type,
        transcript_path=transcript_path,
        buffered_line_offset=buffered_line_offset,
        raw_lines_added=int(semantic_buffer_metrics.get("raw_lines_added", len(new_lines)) or 0),
        semantic_chars_added=int(semantic_buffer_metrics.get("semantic_chars_added", 0) or 0),
        semantic_tokens_added=int(semantic_buffer_metrics.get("semantic_tokens_added", 0) or 0),
        semantic_buffer_tokens=int(staged_state.get("semantic_buffer_tokens", 0) or 0),
        stage_raw_fact_count=len(stage_result.get("raw_facts", []) or []),
        staged_fact_count=len(staged_state.get("raw_facts", []) or []),
        staged_facts=_rolling_debug_fact_rows(staged_state.get("raw_facts", []) or [], fallback_session_id=session_id),
        chunks_processed=chunks_processed,
        chunks_total=chunks_total,
        assessment_usable=int(stage_result.get("assessment_usable", 0) or 0),
        assessment_nothing_usable=int(stage_result.get("assessment_nothing_usable", 0) or 0),
        payload_duplicate_facts_collapsed=int(staged_state.get("payload_duplicate_facts_collapsed", 0) or 0),
    )
    staged_state["processed_line_offset"] = buffered_line_offset
    staged_state["buffered_line_offset"] = buffered_line_offset
    _write_extraction_buffer_log(
        session_id,
        phase="rolling_stage",
        signal_type=signal_type,
        transcript_text=stage_text,
    )
    staged_state["semantic_buffer"] = residual_text
    staged_state["semantic_buffer_tokens"] = estimate_tokens(residual_text) if residual_text else 0
    staged_state["semantic_buffer_prior"] = ""
    staged_state["semantic_buffer_tail"] = ""
    staged_state["semantic_buffer_prior_tokens"] = 0
    staged_state["semantic_buffer_tail_tokens"] = 0
    staged_state["transcript_path"] = transcript_path
    if staged_state_has_payload(staged_state):
        staged_state[_STAGED_PAYLOAD_PENDING_FLUSH_KEY] = True
    write_rolling_state(session_id, staged_state)
    write_rolling_metric(
        "rolling_stage",
        session_id,
        signal_type=signal_type,
        line_count=int(semantic_buffer_metrics.get("raw_lines_added", len(new_lines)) or 0),
        line_chars=line_chars,
        line_estimated_tokens=line_estimated_tokens,
        max_line_chars=max_line_chars,
        max_line_estimated_tokens=max_line_estimated_tokens,
        chunk_budget_tokens=chunk_budget,
        chunk_budget_lines=chunk_line_budget,
        buffered_line_offset=buffered_line_offset,
        new_cursor_offset=buffered_line_offset,
        staged_fact_count=len(staged_state.get("raw_facts", []) or []),
        rolling_batches=int(staged_state.get("rolling_batches", 0) or 0),
        carry_facts_in=carry_facts_in,
        carry_facts_out=len(stage_result.get("carry_facts", []) or []),
        payload_duplicate_facts_collapsed=int(
            staged_state.get("payload_duplicate_facts_collapsed", 0) or 0
        ),
        carry_duplicate_facts_dropped=int(stage_result.get("carry_duplicate_facts_dropped", 0) or 0),
        embedding_cache_requested=int(stage_embedding_stats.get("requested", 0) or 0),
        embedding_cache_unique=int(stage_embedding_stats.get("unique", 0) or 0),
        embedding_cache_hits=int(stage_embedding_stats.get("cache_hits", 0) or 0),
        embedding_cache_warmed=int(stage_embedding_stats.get("warmed", 0) or 0),
        embedding_cache_failed=int(stage_embedding_stats.get("failed", 0) or 0),
        stage_raw_fact_count=len(stage_result.get("raw_facts", []) or []),
        chunks_processed=chunks_processed,
        chunks_total=chunks_total,
        root_chunks=int(stage_result.get("root_chunks", 0) or 0),
        split_events=int(stage_result.get("split_events", 0) or 0),
        split_child_chunks=int(stage_result.get("split_child_chunks", 0) or 0),
        leaf_chunks=int(stage_result.get("leaf_chunks", 0) or 0),
        max_split_depth=int(stage_result.get("max_split_depth", 0) or 0),
        deep_calls=int(stage_result.get("deep_calls", 0) or 0),
        repair_calls=int(stage_result.get("repair_calls", 0) or 0),
        assessment_usable=int(stage_result.get("assessment_usable", 0) or 0),
        assessment_nothing_usable=int(stage_result.get("assessment_nothing_usable", 0) or 0),
        assessment_needs_smaller_chunk=int(stage_result.get("assessment_needs_smaller_chunk", 0) or 0),
        unclassified_empty_payloads=int(stage_result.get("unclassified_empty_payloads", 0) or 0),
        staged_semantic_duplicate_facts_collapsed=int(
            staged_state.get("staged_semantic_duplicate_facts_collapsed", 0) or 0
        ),
        staged_semantic_auto_reject_hits=int(
            staged_state.get("staged_semantic_auto_reject_hits", 0) or 0
        ),
        staged_semantic_gray_zone_rows=int(
            staged_state.get("staged_semantic_gray_zone_rows", 0) or 0
        ),
        staged_semantic_llm_checks=int(
            staged_state.get("staged_semantic_llm_checks", 0) or 0
        ),
        staged_semantic_llm_same_hits=int(
            staged_state.get("staged_semantic_llm_same_hits", 0) or 0
        ),
        staged_semantic_llm_different_hits=int(
            staged_state.get("staged_semantic_llm_different_hits", 0) or 0
        ),
        wall_seconds=round(time.time() - stage_started_at, 3),
    )
    return staged_state


def count_transcript_lines(transcript_path: str) -> int:
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except OSError as exc:
        if _should_raise_transcript_stat_error(transcript_path, exc):
            raise
        return 0


def _get_capture_chunk_tokens(default: int = 8_000) -> int:
    """Read the live extraction chunk budget from config."""
    try:
        from config import get_config
        cfg = get_config()
        capture = getattr(cfg, "capture", None)
        raw = getattr(capture, "chunk_tokens", default) if capture is not None else default
        tokens = int(raw)
        return max(1, tokens)
    except Exception:
        return default


def _daemon_extract_chunk_tokens(chunk_budget: int) -> int:
    """Use smaller daemon extraction chunks than the rolling read window.

    Rolling/session signals often contain dense multi-turn buffers. Keeping
    root extraction chunks below the read threshold gives the LLM output budget
    for later facts instead of letting the first dense turn consume the call.
    A 1200-token ceiling still allowed dense combined task messages to bury
    stable background facts in the first extraction call. The 900-token ceiling
    keeps dense single-message context dumps split into smaller calls, while the
    ratio keeps smaller operator budgets proportional instead of widening them.
    """
    try:
        budget = max(1, int(chunk_budget or 1))
    except Exception:
        budget = 1
    if budget < 512:
        return budget
    focused_budget = max(1, int(budget * DAEMON_EXTRACT_CHUNK_RATIO))
    return max(1, min(budget, DAEMON_EXTRACT_CHUNK_MAX_TOKENS, focused_budget))


def _reset_daemon_llm_usage_budget() -> None:
    """Keep the janitor-named LLM cost cap scoped to one daemon extraction."""
    from lib.llm_clients import reset_token_usage

    reset_token_usage()


def _daemon_extract_llm_timeout_seconds(default: float = DAEMON_EXTRACT_LLM_TIMEOUT_SECONDS) -> float:
    """Bound provider calls made while daemon source locks are held."""
    raw = str(os.environ.get("QUAID_DAEMON_EXTRACT_LLM_TIMEOUT_SECONDS", "") or "").strip()
    if not raw:
        return max(1.0, float(default))
    try:
        value = float(raw)
    except Exception as exc:
        if _fail_hard_enabled():
            raise RuntimeError("Invalid QUAID_DAEMON_EXTRACT_LLM_TIMEOUT_SECONDS") from exc
        logger.warning(
            "invalid QUAID_DAEMON_EXTRACT_LLM_TIMEOUT_SECONDS=%r; using default %.1fs",
            raw,
            default,
        )
        return max(1.0, float(default))
    if value <= 0:
        if _fail_hard_enabled():
            raise RuntimeError("QUAID_DAEMON_EXTRACT_LLM_TIMEOUT_SECONDS must be positive")
        logger.warning(
            "non-positive QUAID_DAEMON_EXTRACT_LLM_TIMEOUT_SECONDS=%r; using default %.1fs",
            raw,
            default,
        )
        return max(1.0, float(default))
    return max(1.0, value)


def _daemon_extract_llm_slot_wait_seconds(default: float = DAEMON_EXTRACT_LLM_SLOT_WAIT_SECONDS) -> float:
    """Allow daemon extraction to queue behind active cross-process LLM work."""
    raw = str(os.environ.get("QUAID_DAEMON_EXTRACT_LLM_SLOT_WAIT_SECONDS", "") or "").strip()
    if not raw:
        return max(1.0, float(default))
    try:
        value = float(raw)
    except Exception as exc:
        if _fail_hard_enabled():
            raise RuntimeError("Invalid QUAID_DAEMON_EXTRACT_LLM_SLOT_WAIT_SECONDS") from exc
        logger.warning(
            "invalid QUAID_DAEMON_EXTRACT_LLM_SLOT_WAIT_SECONDS=%r; using default %.1fs",
            raw,
            default,
        )
        return max(1.0, float(default))
    if value <= 0:
        if _fail_hard_enabled():
            raise RuntimeError("QUAID_DAEMON_EXTRACT_LLM_SLOT_WAIT_SECONDS must be positive")
        logger.warning(
            "non-positive QUAID_DAEMON_EXTRACT_LLM_SLOT_WAIT_SECONDS=%r; using default %.1fs",
            raw,
            default,
        )
        return max(1.0, float(default))
    return max(1.0, value)


def _daemon_extract_llm_max_retries(default: int = DAEMON_EXTRACT_LLM_MAX_RETRIES) -> int:
    """Use daemon signal retry instead of holding source locks across provider retries."""
    raw = str(os.environ.get("QUAID_DAEMON_EXTRACT_LLM_MAX_RETRIES", "") or "").strip()
    if not raw:
        return max(0, int(default))
    try:
        value = int(raw)
    except Exception as exc:
        if _fail_hard_enabled():
            raise RuntimeError("Invalid QUAID_DAEMON_EXTRACT_LLM_MAX_RETRIES") from exc
        logger.warning(
            "invalid QUAID_DAEMON_EXTRACT_LLM_MAX_RETRIES=%r; using default %d",
            raw,
            default,
        )
        return max(0, int(default))
    if value < 0:
        if _fail_hard_enabled():
            raise RuntimeError("QUAID_DAEMON_EXTRACT_LLM_MAX_RETRIES must be non-negative")
        logger.warning(
            "negative QUAID_DAEMON_EXTRACT_LLM_MAX_RETRIES=%r; using default %d",
            raw,
            default,
        )
        return max(0, int(default))
    return value


def _get_capture_chunk_max_lines(default: int = 0) -> int:
    """Optional message-line budget for rolling extraction windows.

    Token budgets alone do not prevent highly fragmented windows. This cap
    keeps rolling extraction closer to normal session shapes by limiting how
    many transcript rows can be packed into a single extraction unit.
    """
    raw_env = str(os.environ.get("QUAID_CAPTURE_CHUNK_MAX_LINES", "") or "").strip()
    if raw_env:
        try:
            value = int(raw_env)
            return max(0, value)
        except Exception:
            logger.warning("invalid QUAID_CAPTURE_CHUNK_MAX_LINES=%r; ignoring", raw_env)
    try:
        from config import get_config
        cfg = get_config()
        capture = getattr(cfg, "capture", None)
        raw = getattr(capture, "chunk_max_lines", default) if capture is not None else default
        value = int(raw)
        return max(0, value)
    except Exception:
        return default


def read_transcript_token_window(
    transcript_path: str,
    from_line: int,
    max_tokens: int,
    max_lines: int = 0,
    adapter=None,
    include_threshold_crossing_semantic_row: bool = False,
    raise_on_parse_error: bool = False,
) -> List[str]:
    """Read a single message-aligned transcript window up to the token budget."""
    from lib.tokens import estimate_tokens

    lines: List[str] = []
    approx_tokens = 0
    budgeted_lines = 0
    saw_extractable_conversation = False
    current_parsed = ""
    stop_after_budget_crossing = False
    accumulator = _transcript_window_accumulator(
        adapter,
        raise_on_parse_error=raise_on_parse_error,
    ) if adapter is not None else None
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i < from_line:
                    continue
                if saw_extractable_conversation and max_lines > 0 and budgeted_lines >= max_lines:
                    break
                if adapter is None:
                    line_tokens = max(1, len(line) // 4)
                    # Oversized single rows can exceed this rolling window budget.
                    # Keep them in the returned slice so cursor advancement remains
                    # monotonic and downstream transcript/extraction chunking can
                    # still split/process the content normally; just do not let
                    # them consume this window's token/line budget.
                    if line_tokens > max_tokens:
                        lines.append(line)
                        saw_extractable_conversation = True
                        if len(lines) >= MAX_TRANSCRIPT_LINES:
                            logger.warning(
                                "transcript %s: token window capped at %d lines (from offset %d)",
                                transcript_path,
                                MAX_TRANSCRIPT_LINES,
                                from_line,
                            )
                            break
                        continue
                    if saw_extractable_conversation and budgeted_lines > 0 and approx_tokens + line_tokens > max_tokens:
                        break
                    lines.append(line)
                    approx_tokens += line_tokens
                    budgeted_lines += 1
                    saw_extractable_conversation = True
                    if len(lines) >= MAX_TRANSCRIPT_LINES:
                        logger.warning(
                            "transcript %s: token window capped at %d lines (from offset %d)",
                            transcript_path,
                            MAX_TRANSCRIPT_LINES,
                            from_line,
                        )
                        break
                    continue
                if accumulator is not None:
                    candidate_parsed = accumulator.append_line(line)
                else:
                    candidate = lines + [line]
                    candidate_parsed = _parse_transcript_lines(
                        candidate,
                        adapter=adapter,
                        raise_on_parse_error=raise_on_parse_error,
                    )
                candidate_extractable = bool(candidate_parsed)
                candidate_tokens = estimate_tokens(candidate_parsed) if candidate_extractable else 0
                semantic_changed = candidate_extractable and candidate_parsed != current_parsed
                if stop_after_budget_crossing and semantic_changed:
                    break
                if (
                    saw_extractable_conversation
                    and budgeted_lines > 0
                    and candidate_extractable
                    and candidate_tokens > max_tokens
                    and semantic_changed
                ):
                    if not include_threshold_crossing_semantic_row:
                        break
                    lines.append(line)
                    current_parsed = candidate_parsed
                    stop_after_budget_crossing = True
                    if len(lines) >= MAX_TRANSCRIPT_LINES:
                        logger.warning(
                            "transcript %s: token window capped at %d lines (from offset %d)",
                            transcript_path,
                            MAX_TRANSCRIPT_LINES,
                            from_line,
                        )
                        break
                    continue
                lines.append(line)
                if candidate_extractable:
                    saw_extractable_conversation = True
                    if semantic_changed:
                        budgeted_lines += 1
                    current_parsed = candidate_parsed
                if len(lines) >= MAX_TRANSCRIPT_LINES:
                    logger.warning(
                        "transcript %s: token window capped at %d lines (from offset %d)",
                        transcript_path,
                        MAX_TRANSCRIPT_LINES,
                        from_line,
                    )
                    break
    except OSError as e:
        if _should_raise_transcript_stat_error(transcript_path, e):
            raise
        logger.error("failed reading token window %s: %s", transcript_path, e)
    return lines


def estimate_unextracted_tokens(transcript_path: str, from_line: int, max_tokens: int) -> int:
    """Cheap message-aligned estimate of unextracted transcript tokens."""
    approx_tokens = 0
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i < from_line:
                    continue
                approx_tokens += max(1, len(line) // 4)
                if approx_tokens >= max_tokens:
                    break
    except OSError:
        return 0
    return approx_tokens


def _load_runtime_adapter():
    try:
        from lib.adapter import get_adapter
        return get_adapter()
    except Exception as exc:
        logger.warning("runtime adapter load failed: %s", exc)
        if _fail_hard_enabled():
            raise RuntimeError("runtime adapter load failed") from exc
        return None


def _cursor_records_transcript_path(session_id: str, transcript_path: str) -> bool:
    """Return True when this instance already cursored the transcript for session."""
    if not session_id or not transcript_path:
        return False
    try:
        cursor = read_cursor(session_id)
    except Exception as exc:
        logger.warning(
            "cursor transcript ownership check failed for session %s (%s): %s",
            session_id,
            transcript_path,
            exc,
        )
        if _fail_hard_enabled():
            raise
        return False
    cursor_path = str((cursor or {}).get("transcript_path") or "").strip()
    if not cursor_path:
        return False
    return (
        _canonicalize_transcript_source_path(cursor_path)
        == _canonicalize_transcript_source_path(transcript_path)
    )


def _cursor_data_records_transcript_path(cursor_data: Dict[str, Any], transcript_path: str) -> bool:
    """Return True when an already-loaded instance cursor points at this transcript."""
    cursor_path = str((cursor_data or {}).get("transcript_path") or "").strip()
    if not cursor_path or not transcript_path:
        return False
    return (
        _canonicalize_transcript_source_path(cursor_path)
        == _canonicalize_transcript_source_path(transcript_path)
    )


def _cursor_data_records_transcript_source(
    cursor_data: Dict[str, Any],
    session_id: str,
    transcript_path: str,
) -> bool:
    """Return True when a source-keyed cursor points at the same transcript source.

    Rolling extraction may snapshot a host transcript into the instance-local
    daemon log tree before queueing a later lifecycle flush. The later signal can
    still reference the original host path. If the source-keyed cursor records
    the same rollout/session source, the daemon already accepted that source for
    this instance and should not fall back to adapter-only cwd ownership.
    """
    if not cursor_data or not transcript_path:
        return False
    cursor_path = str(cursor_data.get("transcript_path") or "").strip()
    if not cursor_path:
        return False
    cursor_session = str(cursor_data.get("session_id") or "").strip()
    cursor_key = str(cursor_data.get("cursor_key") or "").strip()
    same_session = not cursor_session or cursor_session == session_id
    if same_session and _cursor_data_records_transcript_path(cursor_data, transcript_path):
        return True
    if not cursor_key.startswith("source-"):
        return False
    cursor_identity_session = cursor_session or session_id
    try:
        cursor_identity = _signal_source_identity(cursor_identity_session, cursor_path)
        transcript_identity = _signal_source_identity(session_id, transcript_path)
        if cursor_identity != transcript_identity:
            return False
        expected_cursor_key = _signal_source_cursor_key(cursor_identity_session, cursor_path)
        expected_transcript_key = _signal_source_cursor_key(session_id, transcript_path)
        return cursor_key in {expected_cursor_key, expected_transcript_key}
    except Exception:
        return False


def _cursor_or_adapter_owns_transcript_path(
    adapter,
    session_id: str,
    transcript_path: str,
    *,
    cursor_data: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return True when this instance has already cursored or owns a transcript."""
    if cursor_data is not None and _cursor_data_records_transcript_source(cursor_data, session_id, transcript_path):
        return True
    if _cursor_records_transcript_path(session_id, transcript_path):
        return True
    return _adapter_owns_transcript_path(adapter, session_id, transcript_path)


def _adapter_owns_transcript_path(adapter, session_id: str, transcript_path: str) -> bool:
    """Return whether an adapter-scoped transcript belongs to this daemon instance."""
    if not transcript_path:
        return False
    if _is_daemon_owned_transcript_snapshot_path(transcript_path):
        return True
    if adapter is None:
        logger.warning(
            "adapter unavailable during transcript ownership check for session %s (%s)",
            session_id,
            transcript_path,
        )
        return False
    owns_fn = getattr(adapter, "owns_session_path", None)
    if not callable(owns_fn):
        owns_fn = getattr(adapter, "owns_transcript_path", None)
    if not callable(owns_fn):
        logger.warning(
            "adapter %s does not expose transcript ownership check; refusing transcript %s for session %s",
            type(adapter).__name__,
            transcript_path,
            session_id,
        )
        return False
    try:
        owns = bool(owns_fn(Path(transcript_path), session_id=session_id))
    except TypeError:
        try:
            owns = bool(owns_fn(Path(transcript_path)))
        except Exception as exc:
            logger.warning(
                "adapter transcript ownership check failed for session %s (%s): %s",
                session_id,
                transcript_path,
                exc,
            )
            if _fail_hard_enabled():
                raise
            return False
    except Exception as exc:
        logger.warning(
            "adapter transcript ownership check failed for session %s (%s): %s",
            session_id,
            transcript_path,
            exc,
        )
        if _fail_hard_enabled():
            raise
        return False
    return bool(owns)


def _ensure_discovered_session_cursors(adapter=None) -> int:
    """Seed cursors for transcript files that exist but have never been seen.

    Some host paths create transcript JSONL files without going through adapter
    lifecycle hooks first (for example OC Matrix channel sessions). The daemon
    still needs to discover those sessions so rolling/timeout extraction can
    operate on them.
    """
    if _discovery_cursor_scan_disabled():
        return 0
    active_adapter = adapter if adapter is not None else _load_runtime_adapter()
    if active_adapter is None or not hasattr(active_adapter, "get_sessions_dir"):
        return 0
    try:
        discovery_dir_fn = getattr(active_adapter, "get_discovery_sessions_dir", None)
        sessions_dir = (
            discovery_dir_fn()
            if callable(discovery_dir_fn)
            else active_adapter.get_sessions_dir()
        )
    except Exception as exc:
        logger.warning("session discovery directory lookup failed: %s", exc)
        if _fail_hard_enabled():
            raise RuntimeError("session discovery directory lookup failed") from exc
        return 0
    if not sessions_dir:
        return 0
    sessions_root = Path(sessions_dir)
    if not sessions_root.is_dir():
        return 0

    discovered = 0
    for transcript_path in sessions_root.rglob("*.jsonl"):
        if _is_discovery_artifact_transcript(transcript_path):
            continue
        if not transcript_path.is_file():
            continue
        try:
            session_id = _validate_session_id(transcript_path.stem)
        except ValueError:
            continue
        if not _adapter_owns_transcript_path(active_adapter, session_id, str(transcript_path)):
            continue
        source_cursor_key = _signal_source_cursor_key(session_id, str(transcript_path))
        cursor_file = _cursor_dir() / f"{source_cursor_key}.json"
        legacy_cursor_file = _cursor_dir() / f"{session_id}.json"
        if (
            not cursor_file.exists()
            and not legacy_cursor_file.exists()
            and _should_skip_newly_discovered_orphan_transcript(transcript_path)
        ):
            logger.info(
                "skipping stale discovered transcript without existing cursor: %s",
                transcript_path,
            )
            continue
        if cursor_file.exists() or legacy_cursor_file.exists():
            cursor_data = read_cursor(session_id, source_key=source_cursor_key)
            existing_path = str(cursor_data.get("transcript_path") or "").strip()
            existing_is_artifact = bool(
                existing_path
                and _is_discovery_artifact_transcript(Path(existing_path))
            )
            existing_is_stale_relocation = _discovered_transcript_supersedes_cursor(
                existing_path,
                str(transcript_path),
            )
            if (
                existing_path
                and os.path.isfile(existing_path)
                and not existing_is_artifact
                and not existing_is_stale_relocation
            ):
                continue
            reset_cursor = existing_is_artifact or existing_is_stale_relocation
            line_offset = 0 if reset_cursor else int(cursor_data.get("line_offset", 0) or 0)
            internal = False if reset_cursor else bool(cursor_data.get("internal", False))
            write_cursor(
                session_id,
                line_offset,
                str(transcript_path),
                internal=internal,
                source_key=source_cursor_key,
            )
            discovered += 1
            continue
        write_cursor(session_id, 0, str(transcript_path), source_key=source_cursor_key)
        discovered += 1
    return discovered


def _discovered_transcript_supersedes_cursor(existing_path: str, transcript_path: str) -> bool:
    """Return True when an active adapter transcript should replace a relocated cursor."""
    if not existing_path or not transcript_path:
        return False
    try:
        existing = Path(existing_path).expanduser()
        current = Path(transcript_path).expanduser()
        if not existing.is_file() or not current.is_file():
            return False
        if existing.resolve() == current.resolve():
            return False
        if existing.name != current.name:
            return False
        if _is_discovery_artifact_transcript(current):
            return False
        if _is_discovery_artifact_transcript(existing):
            return True
        existing_size = _transcript_size_bytes(str(existing))
        current_size = _transcript_size_bytes(str(current))
        if (
            _is_daemon_preserved_session_transcript_path(str(existing))
            and current_size
            and existing_size
            and current_size < existing_size
        ):
            return False
        if current_size != existing_size:
            return True
        return current.stat().st_mtime > existing.stat().st_mtime
    except OSError:
        return False


def _should_skip_newly_discovered_orphan_transcript(transcript_path: Path, now_ts: Optional[float] = None) -> bool:
    """Avoid turning stale host transcripts into fresh timeout work on startup."""
    try:
        installed_at = _read_installed_at()
        if installed_at <= 0:
            return False
        mtime = transcript_path.stat().st_mtime
    except OSError:
        # If we cannot stat the host file, do not fabricate a new cursor for it.
        return True
    except Exception as exc:
        logger.warning(
            "stale orphan transcript check failed for %s; not skipping: %s",
            transcript_path,
            exc,
        )
        if _fail_hard_enabled():
            raise
        return False
    now = time.time() if now_ts is None else float(now_ts)
    if mtime >= installed_at:
        return False
    # Recently touched pre-install files may still be active host sessions. Only
    # skip old orphans that have sat untouched past the startup grace window.
    if now - mtime <= _DISCOVERY_STALE_ORPHAN_GRACE_SECONDS:
        return False
    return True


def _is_internal_transcript_session(
    session_id: str,
    transcript_path: str,
    adapter=None,
) -> bool:
    """Return True only for host/lifecycle maintenance transcripts."""
    return (
        _classify_transcript_session(session_id, transcript_path, adapter=adapter)
        == _TRANSCRIPT_CLASS_INTERNAL_MAINTENANCE
    )


def _classify_transcript_session(
    session_id: str,
    transcript_path: str,
    adapter=None,
) -> str:
    """Classify transcript content before timeout/internal cursor handling."""
    try:
        if not transcript_path or not os.path.isfile(transcript_path):
            return _TRANSCRIPT_CLASS_MEANINGFUL_USER_CONTENT
        total_lines = count_transcript_lines(transcript_path)
        if total_lines <= 0:
            return _TRANSCRIPT_CLASS_MEANINGFUL_USER_CONTENT
        active_adapter = adapter if adapter is not None else _load_runtime_adapter()
        if active_adapter is None:
            return _TRANSCRIPT_CLASS_MEANINGFUL_USER_CONTENT
        parse_session_jsonl = getattr(active_adapter, "parse_session_jsonl", None)
        if not callable(parse_session_jsonl):
            return _TRANSCRIPT_CLASS_MEANINGFUL_USER_CONTENT
        transcript_text = parse_session_jsonl(Path(transcript_path))
        stripped = (transcript_text or "").strip()
        if not stripped:
            return _TRANSCRIPT_CLASS_INTERNAL_MAINTENANCE
        startup_wrapper_predicate = getattr(active_adapter, "is_startup_wrapper_turn", None)
        return _classify_timeout_transcript_content(
            stripped,
            startup_wrapper_predicate=startup_wrapper_predicate if callable(startup_wrapper_predicate) else None,
        )
    except Exception as exc:
        if _fail_hard_enabled():
            raise
        logger.warning(
            "transcript classification failed for session %s (%s): %s",
            session_id,
            transcript_path,
            exc,
        )
        return _TRANSCRIPT_CLASS_MEANINGFUL_USER_CONTENT


def _iter_parsed_transcript_turns(transcript_text: str) -> List[Tuple[str, str]]:
    turns: List[Tuple[str, str]] = []
    current_role = ""
    current_lines: List[str] = []
    for raw_line in str(transcript_text or "").splitlines():
        match = _TRANSCRIPT_ROLE_RE.match(raw_line)
        if match:
            role = _canonical_transcript_role(match.group(1))
            if role is None:
                if current_role:
                    current_lines.append(raw_line)
                continue
            if current_role:
                turns.append((current_role, "\n".join(current_lines).strip()))
            current_role = role
            current_lines = [match.group(2).strip()]
            continue
        if current_role:
            current_lines.append(raw_line)
    if current_role:
        turns.append((current_role, "\n".join(current_lines).strip()))
    return turns


def _preserved_mirror_user_turns_are_covered_by_live(
    adapter: Any,
    live_transcript_path: str,
    preserved_transcript_path: str,
) -> bool:
    """Return True only when the preserved mirror adds no user-authored turns."""
    if adapter is None:
        return False
    parse_session_jsonl = getattr(adapter, "parse_session_jsonl", None)
    if not callable(parse_session_jsonl):
        return False
    try:
        live_turns = _iter_parsed_transcript_turns(parse_session_jsonl(Path(live_transcript_path)))
        preserved_turns = _iter_parsed_transcript_turns(parse_session_jsonl(Path(preserved_transcript_path)))
    except Exception as exc:
        if _fail_hard_enabled():
            raise
        logger.warning(
            "failed comparing preserved mirror user turns (%s -> %s): %s",
            live_transcript_path,
            preserved_transcript_path,
            exc,
        )
        return False

    live_user_turns = {
        re.sub(r"\s+", " ", str(content or "")).strip()
        for role, content in live_turns
        if role == "user" and str(content or "").strip()
    }
    preserved_user_turns = [
        re.sub(r"\s+", " ", str(content or "")).strip()
        for role, content in preserved_turns
        if role == "user" and str(content or "").strip()
    ]
    return all(turn in live_user_turns for turn in preserved_user_turns)


def _is_timeout_startup_user_turn(
    text: str,
    startup_wrapper_predicate: Optional[Callable[[str], bool]] = None,
) -> bool:
    value = str(text or "").strip()
    if not value:
        return True
    if startup_wrapper_predicate is None:
        return False
    try:
        return bool(startup_wrapper_predicate(value))
    except Exception as exc:
        if _fail_hard_enabled():
            raise
        logger.warning("startup wrapper predicate failed; treating turn as user content: %s", exc)
        return False


def _transcript_role_key(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", text.strip())


def _canonical_transcript_role(value: str) -> Optional[str]:
    key = _transcript_role_key(value)
    candidates = [key]
    if "/" in key:
        candidates.append(key.rsplit("/", 1)[-1].strip())
    for candidate in candidates:
        if candidate in _TRANSCRIPT_USER_ROLE_KEYS:
            return "user"
        if candidate in _TRANSCRIPT_ASSISTANT_ROLE_KEYS:
            return "assistant"
        if candidate in _TRANSCRIPT_SYSTEM_ROLE_KEYS:
            return "system"
    return None


def _is_short_ignored_timeout_user_turn(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return True
    normalized = re.sub(r"\s+", " ", value).strip()
    return (
        len(normalized) <= _IGNORED_TIMEOUT_USER_TURN_MAX_CHARS
        and not _has_non_ascii_letter_signal(normalized)
    )


def _has_non_ascii_letter_signal(text: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    return any(ord(ch) > 127 and ch.isalpha() for ch in normalized)


def _short_transcript_should_bypass_length_gate(transcript_text: str) -> bool:
    return _has_non_ascii_letter_signal(transcript_text)


def _transcript_has_meaningful_timeout_user_content(
    transcript_text: str,
    startup_wrapper_predicate: Optional[Callable[[str], bool]] = None,
) -> bool:
    """Return True when parsed transcript text has user content worth timing out."""
    return (
        _classify_timeout_transcript_content(
            transcript_text,
            startup_wrapper_predicate=startup_wrapper_predicate,
        )
        == _TRANSCRIPT_CLASS_MEANINGFUL_USER_CONTENT
    )


def _classify_timeout_transcript_content(
    transcript_text: str,
    startup_wrapper_predicate: Optional[Callable[[str], bool]] = None,
) -> str:
    """Split true maintenance wrappers from user content that should be ignored.

    OpenClaw can leave a post-/new session in a startup/handshake-only state for
    several minutes while the real queued user message is still delayed. Treating
    those wrappers as extractable advances the cursor before the real message
    arrives, so idle scans should freeze only true maintenance wrappers as
    internal. Short NO_REPLY-associated user turns are ignored content, not
    internal maintenance.
    """
    turns = _iter_parsed_transcript_turns(transcript_text)
    if not turns:
        return (
            _TRANSCRIPT_CLASS_INTERNAL_MAINTENANCE
            if _is_timeout_startup_user_turn(
                transcript_text,
                startup_wrapper_predicate=startup_wrapper_predicate,
            )
            else _TRANSCRIPT_CLASS_MEANINGFUL_USER_CONTENT
        )
    saw_user = False
    saw_startup_wrapper = False
    saw_no_reply_assistant = False
    saw_visible_assistant_content = False
    non_startup_user_turns: List[str] = []
    for role, content in turns:
        if role == "assistant":
            assistant_content = str(content or "").strip()
            if assistant_content.upper() == "NO_REPLY":
                saw_no_reply_assistant = True
            elif assistant_content:
                saw_visible_assistant_content = True
        if role != "user":
            continue
        saw_user = True
        if _is_timeout_startup_user_turn(
            content,
            startup_wrapper_predicate=startup_wrapper_predicate,
        ):
            saw_startup_wrapper = True
            continue
        non_startup_user_turns.append(content)
    if not saw_user:
        if saw_visible_assistant_content:
            return _TRANSCRIPT_CLASS_MEANINGFUL_USER_CONTENT
        return (
            _TRANSCRIPT_CLASS_IGNORE_CONTENT
            if saw_no_reply_assistant
            else _TRANSCRIPT_CLASS_INTERNAL_MAINTENANCE
        )
    if saw_startup_wrapper and all(
        _is_short_ignored_timeout_user_turn(turn)
        for turn in non_startup_user_turns
    ):
        return (
            _TRANSCRIPT_CLASS_IGNORE_CONTENT
            if non_startup_user_turns
            else _TRANSCRIPT_CLASS_INTERNAL_MAINTENANCE
        )
    if saw_no_reply_assistant and non_startup_user_turns and all(
        _is_short_ignored_timeout_user_turn(turn)
        for turn in non_startup_user_turns
    ):
        return _TRANSCRIPT_CLASS_IGNORE_CONTENT
    if saw_startup_wrapper:
        return (
            _TRANSCRIPT_CLASS_MEANINGFUL_USER_CONTENT
            if non_startup_user_turns
            else _TRANSCRIPT_CLASS_INTERNAL_MAINTENANCE
        )
    for role, content in turns:
        if role != "user":
            continue
        if content.strip():
            return _TRANSCRIPT_CLASS_MEANINGFUL_USER_CONTENT
    return _TRANSCRIPT_CLASS_IGNORE_CONTENT


def _advance_internal_session_cursor_to_end(
    session_id: str,
    transcript_path: str,
    *,
    cursor_key: Optional[str] = None,
) -> None:
    """Mark an internal session fully consumed by advancing its cursor to EOF."""
    total_lines = 0
    try:
        if transcript_path and os.path.isfile(transcript_path):
            total_lines = count_transcript_lines(transcript_path)
    except OSError:
        total_lines = 0
    write_cursor(
        session_id,
        total_lines,
        transcript_path,
        internal=True,
        source_key=cursor_key,
    )
    clear_rolling_state(session_id)
    _cursor_end_timeout_fired.add(session_id)


def _advance_ignored_session_cursor_to_end(
    session_id: str,
    transcript_path: str,
    *,
    cursor_key: Optional[str] = None,
) -> None:
    """Consume non-extractable user chatter without marking the cursor internal."""
    total_lines = 0
    try:
        if transcript_path and os.path.isfile(transcript_path):
            total_lines = count_transcript_lines(transcript_path)
    except OSError:
        total_lines = 0
    write_cursor(
        session_id,
        total_lines,
        transcript_path,
        internal=False,
        source_key=cursor_key,
        last_flushed_line_offset=total_lines,
    )
    clear_rolling_state(session_id)
    _cursor_end_timeout_fired.add(session_id)


def _reconcile_internal_cursor_state(
    session_id: str,
    transcript_path: str,
    *,
    cursor_data: Optional[Dict[str, Any]] = None,
    cursor_key: Optional[str] = None,
    adapter=None,
    advance_internal: bool = True,
) -> str:
    """Return internal-session handling state for the current transcript.

    States:
    - "frozen": cursor is marked internal and no new transcript lines arrived.
    - "advanced": transcript is still internal-only; cursor advanced unless advance_internal=False.
    - "ignored": transcript has only non-extractable user chatter; cursor consumed as non-internal.
    - "unfrozen": transcript gained non-internal content past a frozen cursor.
    - "not_internal": transcript is not internal and cursor was not frozen.
    """
    state = cursor_data or _read_cursor_with_source_compat(session_id, cursor_key)
    cursor_offset = int(state.get("line_offset", 0) or 0)
    cursor_internal = bool(state.get("internal", False))

    total_lines = 0
    try:
        if transcript_path and os.path.isfile(transcript_path):
            total_lines = count_transcript_lines(transcript_path)
    except OSError:
        total_lines = 0

    prior_path = _canonicalize_transcript_source_path(str(state.get("transcript_path") or ""))
    current_path = _canonicalize_transcript_source_path(transcript_path)
    prior_size_bytes = int(state.get("transcript_size_bytes", 0) or 0)
    current_size_bytes = _transcript_size_bytes(transcript_path)
    source_unchanged = bool(current_path) and current_path == prior_path
    size_unchanged = current_size_bytes == prior_size_bytes

    if cursor_internal and total_lines <= cursor_offset and source_unchanged and size_unchanged:
        return "frozen"
    transcript_class = _classify_transcript_session(session_id, transcript_path, adapter=adapter)
    if transcript_class == _TRANSCRIPT_CLASS_INTERNAL_MAINTENANCE:
        if not advance_internal:
            return "advanced"
        _advance_internal_session_cursor_to_end(
            session_id,
            transcript_path,
            cursor_key=cursor_key,
        )
        return "advanced"
    if transcript_class == _TRANSCRIPT_CLASS_IGNORE_CONTENT:
        _advance_ignored_session_cursor_to_end(
            session_id,
            transcript_path,
            cursor_key=cursor_key,
        )
        return "ignored"

    if cursor_internal:
        rebased_offset = cursor_offset
        if not source_unchanged or not size_unchanged:
            # Internal cursors are advanced without extraction. Once the same
            # session becomes meaningful after a transcript rewrite or append,
            # the frozen EOF offset is no longer trustworthy.
            rebased_offset = 0
        write_cursor(
            session_id,
            rebased_offset,
            transcript_path,
            internal=False,
            source_key=cursor_key,
        )
        if rebased_offset == 0:
            clear_rolling_state(session_id)
        return "unfrozen"

    return "not_internal"


# ---------------------------------------------------------------------------
# Core extraction processing
# ---------------------------------------------------------------------------

def process_signal(signal_data: Dict[str, Any]) -> None:
    """Process a single extraction signal.

    Reads transcript from cursor, passes to extract_from_transcript()
    which handles chunking and storage internally.
    """
    if _signal_file_was_consumed(signal_data):
        return
    _reload_config_if_changed("signal processing")
    signal_type = signal_data.get("type", "unknown")
    session_id = _validate_session_id(signal_data.get("session_id", "unknown"))
    transcript_path = signal_data.get("transcript_path", "")
    label = f"daemon-{signal_type}"
    rolling_mode = signal_type == "rolling"
    original_session_id = session_id
    signal_meta = signal_data.get("meta") if isinstance(signal_data.get("meta"), dict) else {}
    source_cursor_key = _signal_meta_cursor_key(session_id, signal_meta)
    staged_state, rolling_state_session_id = _read_rolling_state_for_signal(
        session_id,
        transcript_path,
        source_cursor_key=source_cursor_key,
    )
    if rolling_state_session_id != session_id:
        session_id = rolling_state_session_id
        adopted_transcript_path = str(staged_state.get("transcript_path") or "").strip()
        if adopted_transcript_path:
            transcript_path = adopted_transcript_path
        logger.info(
            "[%s] session %s: adopting rolling state for aliased session %s",
            label,
            original_session_id,
            session_id,
        )
    transcript_rebase_retry_count = _transcript_rebase_retry_count(signal_meta)
    staged_payload_sweep_signal = bool(signal_meta.get("staged_payload_sweep")) or (
        str(signal_meta.get("reason") or "") == "rolling_stage_flush"
    )
    ended_rolling_buffer_flush_signal = (
        signal_type == "session_end"
        and str(signal_meta.get("reason") or "").strip() == "ended_rolling_buffer_flush"
    )
    ended_rolling_buffer_flush_source_key = (
        str(signal_meta.get("source_cursor_key") or "").strip()
        if ended_rolling_buffer_flush_signal
        else ""
    )
    preserve_active_rolling_state_after_flush = (
        _is_late_post_reset_content_signal(str(signal_type), signal_meta)
        and not staged_payload_sweep_signal
    )
    # Staged rolling sweeps are processed by a synthetic session_end signal, but
    # telemetry consumers need the originating signal type to identify the flush.
    flush_metric_signal_type = (
        str(signal_meta.get("source_signal") or signal_type)
        if staged_payload_sweep_signal
        else signal_type
    )
    flush_metric_processing_signal_type = _rolling_flush_processing_signal_type(
        signal_type,
        staged_payload_sweep_signal,
    )

    def _emit_noop_flush_metric(reason: str) -> None:
        if signal_type not in ("compaction", "timeout"):
            return
        write_rolling_metric(
            "rolling_flush",
            session_id,
            signal_type=signal_type,
            signal_timestamp=signal_data.get("timestamp"),
            noop=True,
            noop_reason=reason,
            staged_batches=int(staged_state.get("rolling_batches", 0) or 0),
            staged_facts=len(staged_state.get("raw_facts", []) or []),
            final_raw_fact_count=0,
            final_facts_stored=0,
            final_facts_skipped=0,
            final_edges_created=0,
            snippets_count=0,
            journals_count=0,
            project_logs_seen=0,
            project_logs_written=0,
            project_logs_projects_updated=0,
            extract_wall_seconds=0.0,
            publish_wall_seconds=0.0,
            flush_wall_seconds=0.0,
            extract_llm_calls=0,
            extract_fast_calls=0,
            extract_deep_calls=0,
            extract_input_tokens=0,
            extract_output_tokens=0,
            publish_llm_calls=0,
            publish_fast_calls=0,
            publish_deep_calls=0,
            publish_input_tokens=0,
            publish_output_tokens=0,
            signal_to_publish_seconds=None,
        )

    if signal_type not in VALID_SIGNAL_TYPES:
        logger.warning("[%s] unknown signal type, skipping", label)
        mark_signal_processed(signal_data)
        return

    resolved_source_key = ""
    resolved_transcript_path, resolved_source_key = _active_source_cursor_for_stale_signal_transcript(
        session_id,
        transcript_path,
    )
    if resolved_transcript_path:
        logger.info(
            "[%s] session %s: signal transcript is an inactive preserved mirror; "
            "using active source cursor transcript: %s",
            label,
            session_id,
            resolved_transcript_path,
        )
        transcript_path = resolved_transcript_path

    lock_owner_key = source_cursor_key or resolved_source_key or _signal_source_cursor_key(
        session_id,
        transcript_path,
        staged_state=staged_state,
    )
    lock_fd = _acquire_session_processing_lock(lock_owner_key)

    if lock_fd is None:
        logger.info(
            "[%s] session %s already has an active extraction (source lock=%s); preserving signal for retry",
            label,
            session_id,
            lock_owner_key,
        )
        # Dedup only redundant rolling signals. Lifecycle and synthetic staged
        # flushes are the publish trigger for durable rolling payloads; dropping
        # them while the lock is unavailable can strand already-extracted facts.
        try:
            sig_dir = _signal_dir()
            current_path = signal_data.get("_signal_path", "")
            for f in sig_dir.iterdir():
                if not f.name.endswith(".json") or str(f) == current_path:
                    continue
                try:
                    other = json.loads(f.read_text(encoding="utf-8"))
                    if other.get("session_id") == session_id:
                        other_type = other.get("type", "")
                        if signal_type != "rolling" or other_type != "rolling":
                            continue
                        other_meta = other.get("meta") if isinstance(other.get("meta"), dict) else {}
                        other_source_key = _signal_meta_cursor_key(session_id, other_meta)
                        other_path = str(other.get("transcript_path") or "").strip()
                        if not other_source_key and other_path:
                            other_source_key = _signal_source_cursor_key(session_id, other_path)
                        if other_source_key == lock_owner_key:
                            mark_signal_processed(signal_data)
                            break
                except (json.JSONDecodeError, OSError):
                    pass
        except Exception as exc:
            if _fail_hard_enabled():
                _release_session_processing_lock(lock_owner_key, lock_fd)
                raise
            logger.debug("[%s] rolling signal dedup while locked failed: %s", label, exc)
        return

    try:
        from core.subagent_registry import is_registered_subagent
        if is_registered_subagent(session_id):
            logger.info("[%s] session %s: registered subagent, skipping standalone extraction", label, session_id)
            mark_signal_processed(signal_data)
            _release_session_processing_lock(lock_owner_key, lock_fd)
            return
    except Exception as exc:
        if _fail_hard_enabled():
            _release_session_processing_lock(lock_owner_key, lock_fd)
            raise
        logger.debug("[%s] registered subagent lookup failed for %s: %s", label, session_id, exc)

    adapter = _load_runtime_adapter_for_signal(label, session_id)
    cursor_data = _read_cursor_with_source_compat(session_id, lock_owner_key)

    if not transcript_path or not os.path.isfile(transcript_path):
        fallback_path, fallback_source = _resolve_existing_transcript_fallback_for_signal(
            label=label,
            session_id=session_id,
            transcript_path=str(transcript_path or ""),
            signal_meta=signal_meta,
            cursor_data=cursor_data,
            staged_state=staged_state,
            adapter=adapter,
        )
        if fallback_path:
            transcript_path = fallback_path
            logger.info(
                "[%s] transcript path missing/invalid before internal cursor reconciliation; "
                "using %s fallback: %s",
                label,
                fallback_source,
                transcript_path,
            )

    if (
        transcript_path
        and os.path.isfile(transcript_path)
        and not _cursor_or_adapter_owns_transcript_path(
            adapter,
            session_id,
            transcript_path,
            cursor_data=cursor_data,
        )
    ):
        logger.warning(
            "[%s] session %s: transcript does not belong to active instance, skipping: %s",
            label,
            session_id,
            transcript_path,
        )
        clear_rolling_state(session_id)
        mark_signal_processed(signal_data)
        _release_session_processing_lock(lock_owner_key, lock_fd)
        return

    if staged_state_has_payload(staged_state) or _semantic_buffer_has_content(staged_state):
        internal_state = "not_internal"
    else:
        internal_state = _reconcile_internal_cursor_state(
            session_id,
            transcript_path,
            cursor_data=cursor_data,
            cursor_key=lock_owner_key,
            adapter=adapter,
        )
    if internal_state == "frozen":
        logger.info("[%s] session %s: cursor marked internal with no new content, skipping signal", label, session_id)
        mark_signal_processed(signal_data)
        _release_session_processing_lock(lock_owner_key, lock_fd)
        return
    if internal_state == "advanced":
        logger.info("[%s] session %s: internal maintenance transcript, advancing cursor to EOF", label, session_id)
        mark_signal_processed(signal_data)
        _release_session_processing_lock(lock_owner_key, lock_fd)
        return
    if internal_state == "ignored":
        logger.info("[%s] session %s: ignored non-extractable transcript content", label, session_id)
        mark_signal_processed(signal_data)
        _release_session_processing_lock(lock_owner_key, lock_fd)
        return
    if internal_state == "unfrozen":
        logger.info(
            "[%s] session %s: non-internal content arrived past frozen internal cursor, resuming extraction",
            label,
            session_id,
        )
        cursor_data = _read_cursor_with_source_compat(session_id, lock_owner_key)

    try:
        is_subagent_session_fn = getattr(adapter, "is_subagent_session", None) if adapter is not None else None
        if callable(is_subagent_session_fn) and is_subagent_session_fn(session_id, Path(transcript_path)):
            logger.info("[%s] session %s: adapter-marked subagent, skipping standalone extraction", label, session_id)
            mark_signal_processed(signal_data)
            _release_session_processing_lock(lock_owner_key, lock_fd)
            return
    except Exception as exc:
        if _fail_hard_enabled():
            _release_session_processing_lock(lock_owner_key, lock_fd)
            raise
        logger.debug("[%s] adapter subagent lookup failed for %s: %s", label, session_id, exc)

    # FIFO ordering: if a lifecycle/timeout signal arrives while a rolling
    # signal for the same session is still pending, defer this signal so rolling
    # can stage threshold-crossing facts first. The lifecycle/timeout signal
    # then flushes staged facts and drains any residual tail.
    if signal_type in ("compaction", "reset", "session_end", "timeout") and not staged_payload_sweep_signal:
        try:
            _ses_sig_path = signal_data.get("_signal_path", "")
            for _rf in list(_signal_dir().iterdir()):
                if not _rf.name.endswith(".json") or str(_rf) == _ses_sig_path:
                    continue
                try:
                    _rs = json.loads(_rf.read_text(encoding="utf-8"))
                    if _rs.get("session_id") == session_id and _rs.get("type") == "rolling":
                        logger.info(
                            "[%s] session %s: %s deferred — pending rolling signal "
                            "found; will retry after rolling extraction completes (FIFO)",
                            label, session_id, signal_type,
                        )
                        _release_session_processing_lock(lock_owner_key, lock_fd)
                        return  # preserve signal on disk; retry next poll cycle
                except (json.JSONDecodeError, OSError):
                    pass
        except Exception as _fifo_err:
            logger.warning("[%s] FIFO rolling-pending check failed: %s", label, _fifo_err)
            if _fail_hard_enabled():
                _release_session_processing_lock(lock_owner_key, lock_fd)
                raise

    # Consume duplicate signals for this session now that we hold the lock.
    # Signals with the same or lower priority are redundant — this extraction
    # will process the content and advance the cursor. Higher-priority signals
    # (e.g. a rolling signal pending while a reset is processing) are preserved
    # so they can stage content before the next flush.
    _current_priority = _SIGNAL_PRIORITY.get(signal_type, 99)
    try:
        _current_sig_path = signal_data.get("_signal_path", "")
        for _dup_f in list(_signal_dir().iterdir()):
            if not _dup_f.name.endswith(".json") or str(_dup_f) == _current_sig_path:
                continue
            try:
                _dup = json.loads(_dup_f.read_text(encoding="utf-8"))
                if _dup.get("session_id") == session_id:
                    if signal_type == "rolling" and _dup.get("type") in ("session_end", "reset", "compaction", "timeout"):
                        # A rolling pass only stages threshold-crossing content.
                        # Lifecycle/timeout signals must survive to drain any
                        # preserved sub-threshold semantic tail.
                        continue
                    _dup_priority = _SIGNAL_PRIORITY.get(_dup.get("type", ""), 99)
                    if _dup_priority < _current_priority:
                        # Higher-priority signal (e.g. rolling) — preserve it so it
                        # can stage content before this flush processes.
                        continue
                    _dup_meta = _dup.get("meta") if isinstance(_dup.get("meta"), dict) else {}
                    if (
                        not staged_payload_sweep_signal
                        and _is_staged_payload_flush_signal_meta(_dup_meta)
                    ):
                        # Synthetic rolling flushes are the only durable publish
                        # trigger for already-staged payloads. A real lifecycle
                        # signal may drain tail content, but it must not consume
                        # the staged-payload flush before it publishes.
                        continue
                    if (
                        staged_payload_sweep_signal
                        and _semantic_buffer_has_content(staged_state)
                        and _dup.get("type") in ("session_end", "reset", "compaction", "timeout")
                        and not _is_staged_payload_flush_signal_meta(_dup_meta)
                    ):
                        # A synthetic rolling flush only publishes the already
                        # staged payload. A real lifecycle signal is still needed
                        # to drain any preserved sub-threshold semantic tail.
                        continue
                    _dup["_signal_path"] = str(_dup_f)
                    mark_signal_processed(_dup)
            except (json.JSONDecodeError, OSError):
                pass
    except Exception as exc:
        if _fail_hard_enabled():
            _release_session_processing_lock(lock_owner_key, lock_fd)
            raise
        logger.warning("[%s] duplicate signal sweep failed for session %s: %s", label, session_id, exc)

    if not transcript_path or not os.path.isfile(transcript_path):
        for _fallback_source, _fallback_path in (
            ("signal_meta_source", str(signal_meta.get("source_transcript_path") or "").strip()),
            ("cursor", str(cursor_data.get("transcript_path") or "").strip()),
            ("rolling_state", str(staged_state.get("transcript_path") or "").strip()),
            ("rolling_state_source", str(staged_state.get("source_transcript_path") or "").strip()),
            ("rolling_state_buffer", str(staged_state.get("buffer_transcript_path") or "").strip()),
        ):
            if _fallback_path and os.path.isfile(_fallback_path):
                transcript_path = _fallback_path
                logger.info(
                    "[%s] transcript path missing/invalid; using %s fallback: %s",
                    label,
                    _fallback_source,
                    transcript_path,
                )
                break
        if (not transcript_path or not os.path.isfile(transcript_path)) and adapter is not None:
            _get_session_path = getattr(adapter, "get_session_path", None)
            if callable(_get_session_path):
                try:
                    _adapter_path = _get_session_path(session_id)
                except Exception as exc:
                    logger.warning(
                        "[%s] failed resolving adapter transcript path for %s: %s",
                        label,
                        session_id,
                        exc,
                    )
                else:
                    _adapter_path_str = str(_adapter_path or "").strip()
                    if _adapter_path_str and os.path.isfile(_adapter_path_str):
                        transcript_path = _adapter_path_str
                        logger.info(
                            "[%s] transcript path missing/invalid; using adapter fallback: %s",
                            label,
                            transcript_path,
                        )

    if not transcript_path or not os.path.isfile(transcript_path):
        # OC /new renames the session file to .jsonl.reset.<timestamp> — check for that backup.
        _reset_found = False
        if transcript_path and transcript_path.endswith(".jsonl"):
            import glob as _glob
            _reset_pattern = transcript_path[:-len(".jsonl")] + ".jsonl.reset.*"
            _reset_candidates = sorted(_glob.glob(_reset_pattern))
            if _reset_candidates:
                transcript_path = _reset_candidates[-1]
                _reset_found = True
                logger.info("[%s] transcript renamed to reset backup, using: %s", label, transcript_path)
        if not _reset_found:
            if signal_type in ("reset", "session_end", "compaction", "timeout") and _preserve_missing_transcript_signal_for_retry(
                signal_data,
                session_id=session_id,
                signal_type=signal_type,
                transcript_path=str(transcript_path or ""),
                label=label,
            ):
                _release_session_processing_lock(lock_owner_key, lock_fd)
                return
            logger.warning("[%s] transcript not found: %s", label, transcript_path)
            mark_signal_processed(signal_data)
            _release_session_processing_lock(lock_owner_key, lock_fd)
            return

    if signal_type == "timeout" and _is_daemon_preserved_session_transcript_path(str(transcript_path)):
        logger.info(
            "[%s] session %s: timeout signal points at daemon-owned preserved transcript; "
            "skipping inactive mirror: %s",
            label,
            session_id,
            transcript_path,
        )
        mark_signal_processed(signal_data)
        _release_session_processing_lock(lock_owner_key, lock_fd)
        return

    if not _cursor_or_adapter_owns_transcript_path(
        adapter,
        session_id,
        transcript_path,
        cursor_data=cursor_data,
    ):
        logger.warning(
            "[%s] session %s: resolved transcript does not belong to active instance, skipping: %s",
            label,
            session_id,
            transcript_path,
        )
        clear_rolling_state(session_id)
        mark_signal_processed(signal_data)
        _release_session_processing_lock(lock_owner_key, lock_fd)
        return

    missing_cursor_fields = [
        field for field in ("line_offset", "transcript_path")
        if field not in cursor_data
    ]
    if missing_cursor_fields:
        message = (
            f"[{label}] session {session_id}: cursor state for {lock_owner_key} "
            f"missing required field(s): {', '.join(missing_cursor_fields)}"
        )
        if _fail_hard_enabled():
            _release_session_processing_lock(lock_owner_key, lock_fd)
            raise RuntimeError(message)
        logger.warning(message)

    try:
        cursor_offset = int(cursor_data.get("line_offset", 0) or 0)
    except (TypeError, ValueError) as exc:
        if _fail_hard_enabled():
            _release_session_processing_lock(lock_owner_key, lock_fd)
            raise
        logger.warning(
            "[%s] session %s: invalid cursor line_offset for %s: %r (%s); "
            "resetting offset to 0",
            label,
            session_id,
            lock_owner_key,
            cursor_data.get("line_offset"),
            exc,
        )
        cursor_offset = 0
    cursor_transcript = str(cursor_data.get("transcript_path") or "").strip()
    cursor_processed_signal_type = str(cursor_data.get("processed_signal_type") or "").strip()
    cursor_marks_processed_extraction = cursor_processed_signal_type in VALID_SIGNAL_TYPES
    reset_staged_state_for_full_reextract = False

    # Write a preliminary cursor entry before extraction begins so that
    # check_idle_sessions() can discover this session even if the daemon is
    # killed before extraction completes. The cursor is updated to the real
    # offset at the end of a successful extraction (line ~1860).
    if not cursor_transcript and transcript_path:
        try:
            write_cursor(
                session_id,
                cursor_offset,
                transcript_path,
                source_key=lock_owner_key,
            )
        except Exception as exc:
            if _fail_hard_enabled():
                _release_session_processing_lock(lock_owner_key, lock_fd)
                raise
            logger.warning(
                "[%s] failed writing preliminary cursor for session %s: %s",
                label,
                session_id,
                exc,
            )

    if cursor_transcript and cursor_transcript != transcript_path:
        # A .jsonl → .jsonl.reset.<ts> rename is OC's /reset backup mechanism.
        # The content up to cursor_offset is identical in the backup file, so
        # preserving the cursor avoids re-extracting already-processed lines.
        _is_reset_rename = (
            cursor_transcript.endswith(".jsonl")
            and transcript_path.startswith(cursor_transcript[:-len(".jsonl")] + ".jsonl.reset.")
        )
        # A same-basename move is a session directory relocation (e.g.
        # .openclaw/agents/.../sessions/X.jsonl -> quaid/logs/.../sessions/X.jsonl).
        # The file content is identical, so preserve the cursor.
        _is_dir_relocation = (
            not _is_reset_rename
            and os.path.basename(cursor_transcript) == os.path.basename(transcript_path)
        )
        _relocated_current_stat = _transcript_stat_metadata(transcript_path) if _is_dir_relocation else {}
        _relocated_cursor_size_bytes = int(cursor_data.get("transcript_size_bytes", 0) or 0)
        _relocated_current_size_bytes = int(_relocated_current_stat.get("size_bytes", 0) or 0)
        _relocated_cursor_mtime_ns = int(cursor_data.get("transcript_mtime_ns", 0) or 0)
        _relocated_current_mtime_ns = int(_relocated_current_stat.get("mtime_ns", 0) or 0)
        _relocated_cursor_inode = int(cursor_data.get("transcript_inode", 0) or 0)
        _relocated_current_inode = int(_relocated_current_stat.get("inode", 0) or 0)
        _relocated_cursor_device = int(cursor_data.get("transcript_device", 0) or 0)
        _relocated_current_device = int(_relocated_current_stat.get("device", 0) or 0)
        _relocated_file_identity_changed = bool(
            (
                _relocated_cursor_mtime_ns
                and _relocated_current_mtime_ns
                and _relocated_current_mtime_ns != _relocated_cursor_mtime_ns
            )
            or (
                _relocated_cursor_inode
                and _relocated_current_inode
                and _relocated_current_inode != _relocated_cursor_inode
            )
            or (
                _relocated_cursor_device
                and _relocated_current_device
                and _relocated_current_device != _relocated_cursor_device
            )
        )
        _relocated_size_changed = bool(
            _relocated_cursor_size_bytes
            and _relocated_current_size_bytes
            and _relocated_current_size_bytes != _relocated_cursor_size_bytes
        )
        # A zero-size cursor can come from an early maintenance/reset pass over
        # an empty handshake file. If the later relocated transcript has content
        # and file identity changed, do not let that stale EOF cursor shadow it.
        _relocated_zero_size_cursor_rebased = bool(
            not _relocated_cursor_size_bytes
            and _relocated_current_size_bytes
            and _relocated_file_identity_changed
        )
        _relocated_content_changed = bool(
            _is_dir_relocation
            and (_relocated_size_changed or _relocated_zero_size_cursor_rebased)
        )
        _relocated_preserved_subset_already_consumed = bool(
            _is_dir_relocation
            and signal_type == "session_end"
            and cursor_offset > 0
            and cursor_marks_processed_extraction
            and _relocated_cursor_size_bytes
            and _relocated_current_size_bytes
            and _relocated_current_size_bytes < _relocated_cursor_size_bytes
            and cursor_transcript
            and not os.path.isfile(cursor_transcript)
            and not _is_daemon_preserved_session_transcript_path(cursor_transcript)
            and _is_daemon_preserved_session_transcript_path(str(transcript_path))
        )
        # Cross-directory reset rename: cursor is at a relocated path (dir2/X.jsonl)
        # and the new transcript is the .reset.* backup in the original directory
        # (dir1/X.jsonl.reset.<ts>).  The directory-level _is_reset_rename check
        # above fails because the dirs differ.  A basename-level check catches it.
        _cursor_base = os.path.basename(cursor_transcript)
        _transcript_base = os.path.basename(transcript_path)
        _is_cross_dir_reset_rename = (
            not _is_reset_rename
            and not _is_dir_relocation
            and _cursor_base.endswith(".jsonl")
            and _transcript_base.startswith(_cursor_base[:-len(".jsonl")] + ".jsonl.reset.")
        )
        # Cursor is on a .reset.* backup; new signal points to the plain preserved
        # copy (.jsonl) of the same session content in a different directory.
        # Treat as already-consumed when cursor_offset > 0.
        _is_cursor_on_backup_to_plain = (
            not _is_reset_rename
            and not _is_dir_relocation
            and not _is_cross_dir_reset_rename
            and _transcript_base.endswith(".jsonl")
            and _cursor_base.startswith(_transcript_base[:-len(".jsonl")] + ".jsonl.reset.")
        )
        if _is_reset_rename and signal_type != "reset":
            # Non-reset signals on a renamed backup (e.g. orphan_reset_check on an
            # active session) — content up to cursor_offset is already extracted, so
            # preserve the cursor to avoid re-extraction.
            logger.info(
                "[%s] session %s: transcript path is reset backup of cursor path (%s -> %s), preserving cursor",
                label, session_id, cursor_transcript, transcript_path,
            )
        elif _is_reset_rename and cursor_offset > 0 and cursor_marks_processed_extraction:
            # Reset signal on a renamed backup, but content was already extracted
            # from the plain path (cursor_offset > 0). This is a late duplicate
            # signal — the backup contains the same content already consumed.
            # Preserve cursor and skip re-extraction to avoid duplicate facts.
            logger.info(
                "[%s] session %s: reset signal on backup path (%s -> %s), "
                "content already extracted at offset %d, skipping",
                label, session_id, cursor_transcript, transcript_path, cursor_offset,
            )
            mark_signal_processed(signal_data)
            _release_session_processing_lock(lock_owner_key, lock_fd)
            return
        elif _is_reset_rename:
            # Reset signal on a renamed backup — this IS the /reset extraction.
            # We want the full session content, so start from offset 0.
            logger.info(
                "[%s] session %s: reset signal on backup path (%s -> %s), resetting cursor for full extraction",
                label, session_id, cursor_transcript, transcript_path,
            )
            cursor_offset = 0
            reset_staged_state_for_full_reextract = True
        elif (
            _is_dir_relocation
            and _relocated_content_changed
            and signal_type == "session_end"
            and cursor_transcript
            and os.path.isfile(cursor_transcript)
            and not _is_daemon_preserved_session_transcript_path(cursor_transcript)
            and _is_daemon_preserved_session_transcript_path(str(transcript_path))
            and _preserved_mirror_user_turns_are_covered_by_live(adapter, cursor_transcript, str(transcript_path))
        ):
            # OpenClaw can emit session_end against the preserved mirror while
            # the live transcript is still active. Treat it as a checkpoint
            # only when every preserved user turn is already in the live file.
            logger.info(
                "[%s] session %s: preserved transcript mirror changed while live transcript still exists "
                "(%s -> %s, cursor_size=%d, current_size=%d); treating as active-session checkpoint",
                label, session_id, cursor_transcript, transcript_path,
                _relocated_cursor_size_bytes, _relocated_current_size_bytes,
            )
            mark_signal_processed(signal_data)
            _release_session_processing_lock(lock_owner_key, lock_fd)
            return
        elif _relocated_preserved_subset_already_consumed:
            _relocated_total_lines = count_transcript_lines(transcript_path)
            if _relocated_total_lines <= cursor_offset:
                # OpenClaw /new can move an already-consumed subset of the live
                # transcript into Quaid's preserved sessions dir after the live
                # file disappears. The smaller preserved copy should not rewind
                # the extraction cursor and replay the prior session.
                logger.info(
                    "[%s] session %s: relocated preserved transcript is already consumed "
                    "(%s -> %s, cursor_offset=%d, preserved_lines=%d, cursor_size=%d, current_size=%d); "
                    "advancing relocated cursor to EOF",
                    label, session_id, cursor_transcript, transcript_path,
                    cursor_offset, _relocated_total_lines,
                    _relocated_cursor_size_bytes, _relocated_current_size_bytes,
                )
                write_cursor(
                    session_id,
                    _relocated_total_lines,
                    transcript_path,
                    source_key=lock_owner_key,
                    processed_signal_type=signal_type,
                )
                mark_signal_processed(signal_data)
                _release_session_processing_lock(lock_owner_key, lock_fd)
                return
            logger.info(
                "[%s] session %s: relocated preserved transcript is smaller but has %d new line(s) "
                "past cursor offset %d (%s -> %s); continuing from cursor",
                label, session_id, _relocated_total_lines - cursor_offset, cursor_offset,
                cursor_transcript, transcript_path,
            )
        elif (
            _is_dir_relocation
            and _relocated_content_changed
            and signal_type == "timeout"
            and _relocated_cursor_size_bytes
            and _relocated_current_size_bytes
            and _relocated_current_size_bytes < _relocated_cursor_size_bytes
            and _is_daemon_preserved_session_transcript_path(cursor_transcript)
            and not _is_daemon_preserved_session_transcript_path(str(transcript_path))
        ):
            if cursor_marks_processed_extraction:
                logger.info(
                    "[%s] session %s: timeout points at smaller stale live transcript after preserved flush "
                    "(%s -> %s, cursor_offset=%d, cursor_size=%d, current_size=%d); preserving cursor",
                    label, session_id, cursor_transcript, transcript_path, cursor_offset,
                    _relocated_cursor_size_bytes, _relocated_current_size_bytes,
                )
                mark_signal_processed(signal_data)
                _release_session_processing_lock(lock_owner_key, lock_fd)
                return
            logger.info(
                "[%s] session %s: timeout points at smaller live transcript after scan-only preserved cursor "
                "(%s -> %s, cursor_offset=%d, cursor_size=%d, current_size=%d); resetting cursor for extraction",
                label, session_id, cursor_transcript, transcript_path, cursor_offset,
                _relocated_cursor_size_bytes, _relocated_current_size_bytes,
            )
            cursor_offset = 0
            reset_staged_state_for_full_reextract = True
        elif _is_dir_relocation and _relocated_content_changed:
            logger.info(
                "[%s] session %s: transcript directory relocation content changed "
                "(%s -> %s, cursor_size=%d, current_size=%d), resetting cursor",
                label, session_id, cursor_transcript, transcript_path,
                _relocated_cursor_size_bytes, _relocated_current_size_bytes,
            )
            cursor_offset = 0
            reset_staged_state_for_full_reextract = True
        elif _is_dir_relocation and signal_type != "reset":
            # Same-basename path: session file relocated to a different directory.
            # Content is identical; preserve cursor to avoid duplicate extraction.
            logger.info(
                "[%s] session %s: transcript directory relocation (%s -> %s), preserving cursor",
                label, session_id, cursor_transcript, transcript_path,
            )
        elif _is_dir_relocation:
            # Reset signal on a relocated transcript. Two sub-cases:
            # (a) cursor_offset == 0 or scan-only cursor: content not yet extracted — reset and extract
            #     the full content from the relocated file (M10 scenario: first
            #     signal for this session arrives via the relocated path; OC
            #     rolling scans may also have advanced to EOF without extracting).
            # (b) cursor_offset > 0 with processed_signal_type: content already
            #     extracted in a prior pass (M2 scenario: duplicate reset signal
            #     after relocation). The relocated file has identical content;
            #     preserve cursor to avoid re-extracting duplicate facts.
            if cursor_offset == 0 or not cursor_marks_processed_extraction:
                logger.info(
                    "[%s] session %s: reset signal on relocated transcript, no prior extraction marker (%s -> %s), resetting cursor for full extraction",
                    label, session_id, cursor_transcript, transcript_path,
                )
                cursor_offset = 0
                reset_staged_state_for_full_reextract = True
            else:
                logger.info(
                    "[%s] session %s: reset signal on relocated transcript, content already extracted at offset %d (%s -> %s), preserving cursor",
                    label, session_id, cursor_offset, cursor_transcript, transcript_path,
                )
        elif _is_cross_dir_reset_rename and signal_type != "reset":
            # Non-reset signal on a cross-directory reset backup — content up to
            # cursor_offset already extracted; preserve cursor.
            logger.info(
                "[%s] session %s: cross-dir reset backup of cursor path (%s -> %s), preserving cursor",
                label, session_id, cursor_transcript, transcript_path,
            )
        elif _is_cross_dir_reset_rename:
            # Reset signal on a cross-directory reset backup — full /reset extraction.
            logger.info(
                "[%s] session %s: reset signal on cross-dir reset backup (%s -> %s), resetting cursor for full extraction",
                label, session_id, cursor_transcript, transcript_path,
            )
            cursor_offset = 0
            reset_staged_state_for_full_reextract = True
        elif _is_cursor_on_backup_to_plain and cursor_offset > 0:
            # Cursor was written against a .reset.* backup; new signal points to
            # the plain session path. OpenClaw can reuse the same session id after
            # /new and then append the real queued user turn to the plain file
            # after the reset backup was extracted. Only skip when the plain file
            # is still at the extracted backup offset; otherwise keep processing
            # the preserved copy so post-reset user content is not lost.
            plain_total_lines = count_transcript_lines(transcript_path)
            cursor_size_bytes = int(cursor_data.get("transcript_size_bytes", 0) or 0)
            plain_size_bytes = _transcript_size_bytes(transcript_path)
            if plain_total_lines < cursor_offset or (
                plain_total_lines == cursor_offset
                and cursor_size_bytes
                and plain_size_bytes
                and plain_size_bytes != cursor_size_bytes
            ):
                logger.info(
                    "[%s] session %s: cursor on reset backup but preserved copy appears rebased "
                    "(%s -> %s, backup_offset=%d, plain_lines=%d); resetting cursor for full extraction",
                    label, session_id, cursor_transcript, transcript_path, cursor_offset, plain_total_lines,
                )
                cursor_offset = 0
                reset_staged_state_for_full_reextract = True
            elif plain_total_lines > cursor_offset:
                logger.info(
                    "[%s] session %s: cursor on reset backup, preserved copy has %d new line(s) "
                    "past offset %d (%s -> %s); continuing from cursor",
                    label, session_id, plain_total_lines - cursor_offset, cursor_offset,
                    cursor_transcript, transcript_path,
                )
            else:
                logger.info(
                    "[%s] session %s: cursor on reset backup, new signal is preserved copy "
                    "(%s -> %s), content already extracted at offset %d, skipping",
                    label, session_id, cursor_transcript, transcript_path, cursor_offset,
                )
                mark_signal_processed(signal_data)
                _release_session_processing_lock(lock_owner_key, lock_fd)
                return
        elif _is_cursor_on_backup_to_plain:
            # cursor_offset == 0: no prior extraction — proceed normally.
            logger.info(
                "[%s] session %s: cursor on reset backup (offset 0), new signal is preserved copy "
                "(%s -> %s), resetting cursor for full extraction",
                label, session_id, cursor_transcript, transcript_path,
            )
            cursor_offset = 0
            reset_staged_state_for_full_reextract = True
        else:
            logger.info(
                "[%s] session %s: transcript path changed (%s -> %s), resetting cursor",
                label, session_id, cursor_transcript, transcript_path,
            )
            cursor_offset = 0
            reset_staged_state_for_full_reextract = True

    if (
        signal_type in ("compaction", "reset", "session_end", "timeout")
        and not rolling_mode
        and not staged_payload_sweep_signal
        and staged_state_has_payload(staged_state)
    ):
        staged_source_path = str(
            staged_state.get("source_transcript_path")
            or staged_state.get("buffer_transcript_path")
            or ""
        ).strip()
        if (
            staged_source_path
            and os.path.isfile(staged_source_path)
            and staged_source_path != str(transcript_path or "")
        ):
            try:
                staged_source_lines = count_transcript_lines(staged_source_path)
                current_lines = count_transcript_lines(transcript_path)
            except OSError as exc:
                if _fail_hard_enabled():
                    _release_session_processing_lock(lock_owner_key, lock_fd)
                    raise
                logger.warning(
                    "[%s] session %s: failed comparing rolling staged source transcript %s to %s: %s",
                    label,
                    session_id,
                    staged_source_path,
                    transcript_path,
                    exc,
                )
            else:
                if staged_source_lines > current_lines or _is_daemon_owned_transcript_snapshot_path(str(transcript_path)):
                    logger.info(
                        "[%s] session %s: staged rolling payload source has live residual "
                        "(%s lines=%d > %s lines=%d); draining source transcript during lifecycle flush",
                        label,
                        session_id,
                        staged_source_path,
                        staged_source_lines,
                        transcript_path,
                        current_lines,
                    )
                    transcript_path = staged_source_path

    total_lines = count_transcript_lines(transcript_path)
    cursor_clamped_to_eof = False
    if cursor_offset > total_lines:
        same_transcript_source = (
            bool(cursor_transcript)
            and bool(transcript_path)
            and _canonicalize_transcript_source_path(cursor_transcript)
            == _canonicalize_transcript_source_path(transcript_path)
        )
        prior_size_bytes = int(cursor_data.get("transcript_size_bytes", 0) or 0)
        current_size_bytes = _transcript_size_bytes(transcript_path)
        same_source_rebased_smaller = bool(
            same_transcript_source
            and prior_size_bytes
            and current_size_bytes
            and current_size_bytes < prior_size_bytes
        )
        if same_source_rebased_smaller:
            logger.warning(
                "[%s] session %s: cursor offset %d > file length %d on same path, "
                "but transcript shrank from %d to %d bytes; resetting cursor for rebased content",
                label,
                session_id,
                cursor_offset,
                total_lines,
                prior_size_bytes,
                current_size_bytes,
            )
            cursor_offset = 0
            reset_staged_state_for_full_reextract = True
        elif same_transcript_source and signal_type != "reset":
            logger.warning(
                "[%s] session %s: cursor offset %d > file length %d on unchanged transcript source, "
                "clamping cursor to EOF to avoid replay",
                label, session_id, cursor_offset, total_lines,
            )
            cursor_offset = total_lines
            cursor_clamped_to_eof = True
        else:
            logger.warning(
                "[%s] session %s: cursor offset %d > file length %d (file truncated?), resetting cursor",
                label, session_id, cursor_offset, total_lines,
            )
            cursor_offset = 0
            reset_staged_state_for_full_reextract = True

    cursor_last_flushed_line_offset = max(
        0,
        min(int(cursor_data.get("last_flushed_line_offset", 0) or 0), int(cursor_offset or 0)),
    )
    transcript_read_guard_start = int(cursor_offset or 0)
    transcript_read_guard_count = max(0, int(total_lines or 0) - transcript_read_guard_start)
    transcript_read_guard_digest = _transcript_line_window_digest(
        transcript_path,
        transcript_read_guard_start,
        transcript_read_guard_count,
    )

    chunk_budget = _get_capture_chunk_tokens()
    chunk_line_budget = _get_capture_chunk_max_lines()
    if (
        reset_staged_state_for_full_reextract
        and not staged_payload_sweep_signal
        and not preserve_active_rolling_state_after_flush
        and _rolling_state_has_pending_content(staged_state)
    ):
        logger.info(
            "[%s] session %s: cursor reset for full extraction; clearing stale rolling buffer state",
            label,
            session_id,
        )
        clear_rolling_state(session_id)
        staged_state = read_rolling_state(session_id)
    semantic_buffer_metrics = {
        "raw_lines_added": 0,
        "semantic_chars_added": 0,
        "semantic_tokens_added": 0,
        "buffered_line_offset": int(staged_state.get("buffered_line_offset", cursor_offset) or 0),
    }
    refreshed_semantic_buffer_for_nonrolling = False
    buffered_line_offset = max(
        int(staged_state.get("buffered_line_offset", cursor_offset) or 0),
        int(cursor_offset or 0),
    )
    recover_unflushed_scanned_window = bool(
        not rolling_mode
        and not staged_payload_sweep_signal
        and signal_type in ("compaction", "reset", "session_end", "timeout")
        and int(cursor_offset or 0) > int(cursor_last_flushed_line_offset or 0)
        and not _rolling_state_has_pending_content(staged_state)
    )
    if recover_unflushed_scanned_window:
        buffered_line_offset = min(buffered_line_offset, cursor_last_flushed_line_offset)
        logger.info(
            "[%s] session %s: lifecycle signal found unflushed rolling scan window "
            "(last_flushed=%d cursor=%d); draining from last flushed line",
            label,
            session_id,
            cursor_last_flushed_line_offset,
            cursor_offset,
        )
    staged_semantic_ready = rolling_mode and _semantic_buffer_has_content(staged_state)
    staged_rolling_batches = int(staged_state.get("rolling_batches", 0) or 0)
    continued_raw_tail_pending = bool(signal_meta.get("flush_staged_payload_only"))
    drain_unstaged_semantic_buffer_on_sweep = bool(
        staged_payload_sweep_signal
        and signal_type == "session_end"
        and _semantic_buffer_has_content(staged_state)
        and (staged_rolling_batches <= 0 or not continued_raw_tail_pending)
    )
    if drain_unstaged_semantic_buffer_on_sweep:
        if staged_rolling_batches > 0:
            logger.info(
                "[%s] session %s: rolling-stage flush has residual semantic buffer and no continued raw tail; "
                "draining before publish",
                label,
                session_id,
            )
        else:
            logger.info(
                "[%s] session %s: rolling-stage flush has no completed rolling batch; "
                "draining pending semantic buffer during lifecycle flush",
                label,
                session_id,
            )
    flush_staged_payload_only = bool(
        staged_payload_sweep_signal
        and signal_meta.get("flush_staged_payload_only")
        and staged_state_has_payload(staged_state)
        and not drain_unstaged_semantic_buffer_on_sweep
    )
    if total_lines > buffered_line_offset and not staged_semantic_ready and not flush_staged_payload_only:
        buffer_kwargs: Dict[str, Any] = {"adapter": adapter}
        if rolling_mode:
            buffer_kwargs["max_tokens"] = chunk_budget
            buffer_kwargs["max_lines"] = chunk_line_budget
        buffer_kwargs["session_id"] = session_id
        buffer_kwargs["label"] = label
        staged_state, semantic_buffer_metrics = _buffer_transcript_tail(
            transcript_path,
            buffered_line_offset,
            staged_state,
            **buffer_kwargs,
        )
        if int(semantic_buffer_metrics.get("parse_failed", 0) or 0):
            write_rolling_state(session_id, staged_state)
            mark_signal_processed(signal_data)
            _release_session_processing_lock(lock_owner_key, lock_fd)
            return
        write_rolling_state(session_id, staged_state)
        if rolling_mode:
            buffered_line_offset = int(
                staged_state.get("buffered_line_offset", buffered_line_offset) or buffered_line_offset
            )
        else:
            refreshed_semantic_buffer_for_nonrolling = True
    elif staged_semantic_ready:
        buffered_line_offset = int(
            staged_state.get("buffered_line_offset", buffered_line_offset) or buffered_line_offset
        )
    read_start_offset = cursor_offset if rolling_mode else buffered_line_offset
    pending_subagent_harvest = False
    new_lines = [] if flush_staged_payload_only else (
        read_transcript_token_window(
            transcript_path,
            cursor_offset,
            chunk_budget,
            chunk_line_budget,
            adapter=adapter,
        )
        if rolling_mode
        else read_transcript_slice(transcript_path, read_start_offset)
    )

    if not new_lines:
        logger.info("[%s] session %s: no new content past cursor (offset=%d)", label, session_id, cursor_offset)
        pending_subagent_harvest = not rolling_mode and _session_has_harvestable_subagents(session_id, adapter=adapter)
        if rolling_mode and _semantic_buffer_has_content(staged_state):
            logger.info(
                "[%s] session %s: no raw tail past cursor but semantic rolling buffer is pending; "
                "continuing with buffered content",
                label,
                session_id,
            )
            new_lines = []
        elif rolling_mode and staged_state_has_payload(staged_state):
            logger.info(
                "[%s] session %s: no raw tail past cursor but staged rolling payload is pending; "
                "queueing staged payload flush",
                label,
                session_id,
            )
            write_signal(
                signal_type="session_end",
                session_id=session_id,
                transcript_path=transcript_path,
                meta={
                    "reason": "rolling_stage_flush",
                    "source_signal": "rolling",
                    "staged_payload_sweep": True,
                    "buffered_line_offset": int(
                        staged_state.get("buffered_line_offset", cursor_offset) or cursor_offset
                    ),
                },
            )
            _finalize_no_payload_signal(
                session_id=session_id,
                transcript_path=transcript_path,
                signal_data=signal_data,
                lock_owner_key=lock_owner_key,
                lock_fd=lock_fd,
                cursor_key=lock_owner_key,
            )
            return
        elif not rolling_mode and (
            staged_state_has_payload(staged_state)
            or _semantic_buffer_has_content(staged_state)
            or pending_subagent_harvest
        ):
            new_lines = []
        else:
            if signal_type == "session_end":
                try:
                    session_logs_transcript_path = _session_logs_ingest_transcript_path_for_signal(
                        str(transcript_path),
                        session_id=session_id,
                        adapter=adapter,
                        signal_meta=signal_meta,
                        cursor_data=cursor_data,
                        staged_state=staged_state,
                    )
                    if session_logs_transcript_path != str(transcript_path):
                        logger.info(
                            "[%s] session %s: session_logs ingest using current source transcript path: %s "
                            "(signal path=%s)",
                            label,
                            session_id,
                            session_logs_transcript_path,
                            transcript_path,
                        )
                    if not session_logs_transcript_path or not os.path.isfile(session_logs_transcript_path):
                        logger.info(
                            "[%s] session %s: session_logs ingest skipped: "
                            "resolved transcript path is unavailable: %s",
                            label,
                            session_id,
                            session_logs_transcript_path,
                        )
                    else:
                        sl_result = _request_session_logs_ingest(
                            session_id=session_id,
                            owner_id=_get_owner_id(),
                            label=label,
                            transcript_path=session_logs_transcript_path,
                            message_count=0,
                            topic_hint="",
                        )
                        sl_status = sl_result.get("status", "unknown") if isinstance(sl_result, dict) else str(sl_result)
                        sl_reason = sl_result.get("reason", "") if isinstance(sl_result, dict) else ""
                        logger.info("[%s] session %s: session_logs ingest (no-new-content path): %s%s",
                                    label, session_id, sl_status,
                                    f" ({sl_reason})" if sl_reason else "")
                except Exception as e:
                    if _fail_hard_enabled():
                        logger.error(
                            "[%s] session %s: session_logs ingest failed on no-new-content path; "
                            "removing stale signal before failHard raise",
                            label,
                            session_id,
                        )
                        mark_signal_processed(signal_data)
                        _release_session_processing_lock(lock_owner_key, lock_fd)
                        raise RuntimeError("session_logs ingest failed (no-new-content path)") from e
                    logger.warning("[%s] session %s: session_logs ingest failed (no-new-content path): %s",
                                   label, session_id, e)
            _finalize_no_payload_signal(
                session_id=session_id,
                transcript_path=transcript_path,
                signal_data=signal_data,
                lock_owner_key=lock_owner_key,
                lock_fd=lock_fd,
                cursor_key=lock_owner_key,
                next_cursor_offset=cursor_offset if cursor_clamped_to_eof else None,
                emit_noop_metric=lambda: _emit_noop_flush_metric("no_new_content"),
            )
            return

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

    tmp_path = None
    operation_phase = "prepare"
    extract_started_at: Optional[float] = None
    publish_started_at: Optional[float] = None
    flush_payload: Dict[str, Any] = {}
    staged_semantic_buffer_for_nonrolling = False
    staged_fresh_semantic_buffer_for_nonrolling = False
    try:
        from ingest.extract import extract_from_transcript, apply_extracted_payloads

        owner = _get_owner_id()
        transcript_text = ""
        if new_lines:
            tmp_dir = _tmp_dir()
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".jsonl", delete=False, encoding="utf-8",
                dir=str(tmp_dir),
            ) as tmp:
                tmp_path = tmp.name
                tmp.writelines(new_lines)
            if adapter is None:
                from lib.adapter import get_adapter
                adapter = get_adapter()
            transcript_text = adapter.parse_session_jsonl(Path(tmp_path))
        if rolling_mode:
            transcript_text = str(staged_state.get("semantic_buffer", "") or "").strip()
        if (
            not rolling_mode
            and (not staged_payload_sweep_signal or drain_unstaged_semantic_buffer_on_sweep)
            and _semantic_buffer_has_content(staged_state)
        ):
            if int(staged_state.get("semantic_buffer_tokens", 0) or 0) >= chunk_budget:
                operation_phase = "rolling_stage_extract"
                staged_semantic_buffer_for_nonrolling = True
                staged_fresh_semantic_buffer_for_nonrolling = bool(refreshed_semantic_buffer_for_nonrolling)
                staged_state = _stage_semantic_buffer_payload(
                    session_id=session_id,
                    signal_type=signal_type,
                    transcript_path=transcript_path,
                    label=label,
                    owner=owner,
                    staged_state=staged_state,
                    buffered_line_offset=buffered_line_offset,
                    new_lines=new_lines,
                    semantic_buffer_metrics=semantic_buffer_metrics,
                    chunk_budget=chunk_budget,
                    chunk_line_budget=chunk_line_budget,
                )
                refreshed_semantic_buffer_for_nonrolling = False
            buffered_text = str(staged_state.get("semantic_buffer", "") or "").strip()
            tail_text = str(transcript_text or "").strip()
            if staged_semantic_buffer_for_nonrolling and staged_fresh_semantic_buffer_for_nonrolling:
                # The fresh tail was appended into semantic_buffer and staged above.
                # Tail extraction would process the same transcript twice before
                # publishing the staged payload.
                transcript_text = ""
            elif refreshed_semantic_buffer_for_nonrolling:
                # The non-rolling pre-buffer step already appended the new tail
                # into semantic_buffer. Re-appending tail_text here would
                # duplicate the fresh session content on under-budget flushes.
                transcript_text = buffered_text or tail_text
            else:
                transcript_text = f"{buffered_text}\n\n{tail_text}" if buffered_text and tail_text else (buffered_text or tail_text)

        startup_wrapper_predicate = getattr(adapter, "is_startup_wrapper_turn", None) if adapter is not None else None
        timeout_transcript_class = (
            _classify_timeout_transcript_content(
                transcript_text,
                startup_wrapper_predicate=startup_wrapper_predicate if callable(startup_wrapper_predicate) else None,
            )
            if (
                signal_type == "timeout"
                and not rolling_mode
                and not staged_payload_sweep_signal
                and transcript_text.strip()
                and not _rolling_state_has_pending_content(staged_state)
            )
            else _TRANSCRIPT_CLASS_MEANINGFUL_USER_CONTENT
        )
        if (
            signal_type == "timeout"
            and not rolling_mode
            and not staged_payload_sweep_signal
            and transcript_text.strip()
            and not _rolling_state_has_pending_content(staged_state)
            and timeout_transcript_class != _TRANSCRIPT_CLASS_MEANINGFUL_USER_CONTENT
        ):
            if timeout_transcript_class == _TRANSCRIPT_CLASS_INTERNAL_MAINTENANCE:
                logger.info(
                    "[%s] session %s: timeout transcript contains only startup content; "
                    "marking cursor internal and waiting for real user content",
                    label,
                    session_id,
                )
                _advance_internal_session_cursor_to_end(
                    session_id,
                    transcript_path,
                    cursor_key=lock_owner_key,
                )
                noop_reason = "internal_timeout"
            else:
                logger.info(
                    "[%s] session %s: timeout transcript contains only ignored user content; "
                    "advancing non-internal cursor",
                    label,
                    session_id,
                )
                _advance_ignored_session_cursor_to_end(
                    session_id,
                    transcript_path,
                    cursor_key=lock_owner_key,
                )
                noop_reason = "ignored_timeout"
            _finalize_no_payload_signal(
                session_id=session_id,
                transcript_path=transcript_path,
                signal_data=signal_data,
                lock_owner_key=lock_owner_key,
                lock_fd=lock_fd,
                cursor_key=lock_owner_key,
                emit_noop_metric=lambda: _emit_noop_flush_metric(noop_reason),
                release_lock=False,
            )
            return

        if not rolling_mode and not transcript_text.strip():
            if staged_state_has_payload(staged_state):
                logger.info(
                    "[%s] session %s: empty transcript after parsing; flushing staged payload only",
                    label, session_id,
                )
            elif pending_subagent_harvest:
                logger.info(
                    "[%s] session %s: empty transcript after parsing; harvesting completed subagents only",
                    label, session_id,
                )
            else:
                logger.info("[%s] session %s: empty transcript after parsing", label, session_id)
                _finalize_no_payload_signal(
                    session_id=session_id,
                    transcript_path=transcript_path,
                    signal_data=signal_data,
                    lock_owner_key=lock_owner_key,
                    lock_fd=lock_fd,
                    cursor_key=lock_owner_key,
                    next_cursor_offset=total_lines,
                    release_lock=False,
                )
                return

        if not rolling_mode and transcript_text.strip():
            # Guard against post-compaction status lines or other metadata-only content
            # (e.g. "Compacted (17k -> 2.1k)") that aren't extractable conversations.
            # 50 chars is enough to skip pure metadata lines (~25 chars) without
            # silently dropping short but valid user messages.
            _MIN_EXTRACTABLE_CHARS = 50
            transcript_len = len(transcript_text.strip())
            if (
                transcript_len < _MIN_EXTRACTABLE_CHARS
                and not _short_transcript_should_bypass_length_gate(transcript_text)
            ):
                if int(cursor_offset or 0) == 0:
                    try:
                        full_transcript_text = adapter.parse_session_jsonl(Path(transcript_path)) if adapter is not None else ""
                    except Exception as exc:
                        if _fail_hard_enabled():
                            raise
                        logger.warning(
                            "[%s] session %s: failed to reparse full transcript before too-short skip: %s",
                            label, session_id, exc,
                        )
                        full_transcript_text = ""
                    full_transcript_len = len(str(full_transcript_text or "").strip())
                    if full_transcript_len >= _MIN_EXTRACTABLE_CHARS and full_transcript_len > transcript_len:
                        logger.warning(
                            "[%s] session %s: recovered full transcript before too-short skip "
                            "(slice=%d chars, full=%d chars)",
                            label, session_id, transcript_len, full_transcript_len,
                        )
                        transcript_text = str(full_transcript_text or "")
                        transcript_len = full_transcript_len
                if staged_state_has_payload(staged_state):
                    logger.info(
                        "[%s] session %s: transcript too short to extract (%d chars < %d min); "
                        "flushing staged payload only",
                        label, session_id, transcript_len, _MIN_EXTRACTABLE_CHARS,
                    )
                    transcript_text = ""
                elif transcript_len < _MIN_EXTRACTABLE_CHARS:
                    logger.info(
                        "[%s] session %s: transcript too short to extract (%d chars < %d min), skipping",
                        label, session_id, transcript_len, _MIN_EXTRACTABLE_CHARS,
                    )
                    # Non-rolling signals pre-buffer transcript tails into semantic_buffer.
                    # When we skip a short metadata-only flush, keep cursor position in
                    # sync with the buffered tail (not the pre-buffer cursor offset) so
                    # later lifecycle signals do not keep replaying the same transcript.
                    next_cursor_offset = max(
                        int(cursor_offset + len(new_lines)),
                        int(buffered_line_offset or 0),
                    )
                    _finalize_no_payload_signal(
                        session_id=session_id,
                        transcript_path=transcript_path,
                        signal_data=signal_data,
                        lock_owner_key=lock_owner_key,
                        lock_fd=lock_fd,
                        cursor_key=lock_owner_key,
                        next_cursor_offset=next_cursor_offset,
                        clear_state=(
                            signal_type in ("reset", "session_end", "compaction")
                            and not preserve_active_rolling_state_after_flush
                        ),
                        release_lock=False,
                    )
                    return

        harvestable = []
        harvestable_payloads: List[Dict[str, str]] = []
        snapshots = []
        mark_harvested_fn = None
        if not rolling_mode:
            MAX_CHILD_CHARS = 50_000
            MAX_MERGED_CHARS = 200_000
            merged_chars = 0
            deferred_subagents: List[Dict[str, Any]] = []
            try:
                import importlib
                subagent_registry = importlib.import_module("core.subagent_registry")
                harvestable = subagent_registry.get_harvestable(session_id)
                mark_harvested_fn = getattr(subagent_registry, "mark_harvested", None)
                register_fn = getattr(subagent_registry, "register", None)
                mark_complete_fn = getattr(subagent_registry, "mark_complete", None)
                discover_children_fn = getattr(adapter, "discover_subagent_children", None) if adapter is not None else None
                if callable(discover_children_fn):
                    for child in discover_children_fn(session_id):
                        child_id = str(child.get("child_id") or "").strip()
                        if not child_id:
                            continue
                        if any(str(existing.get("child_id") or "").strip() == child_id for existing in harvestable):
                            continue
                        child_path = str(child.get("transcript_path") or "").strip()
                        child_type = str(child.get("child_type") or "").strip() or "adapter-discovered"
                        try:
                            if callable(register_fn):
                                register_fn(
                                    session_id,
                                    child_id,
                                    child_transcript_path=child_path,
                                    child_type=child_type,
                                    metadata={"source": "adapter_discovery"},
                                )
                            if callable(mark_complete_fn):
                                mark_complete_fn(session_id, child_id, transcript_path=child_path)
                        except Exception as e:
                            logger.warning(
                                "[%s] session %s: failed to persist discovered subagent %s registry entry: %s",
                                label, session_id, child_id, e,
                            )
                        harvestable.append(child)
                for child in harvestable:
                    child_path = child.get("transcript_path", "")
                    child_id = child.get("child_id", "")
                    if child_path and os.path.isfile(child_path):
                        if merged_chars >= MAX_MERGED_CHARS:
                            deferred_subagents.append(child)
                            continue
                        try:
                            child_text = adapter.parse_subagent_session_jsonl(Path(child_path))
                            if child_text.strip():
                                if len(child_text) > MAX_CHILD_CHARS:
                                    logger.warning(
                                        "[%s] session %s: subagent %s transcript is very large (%d chars), "
                                        "extraction chunker will handle splitting",
                                        label, session_id, child_id, len(child_text),
                                    )
                                harvestable_payloads.append({
                                    "child_id": child_id,
                                    "child_text": child_text,
                                })
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
                    write_signal(
                        signal_type="session_end",
                        session_id=session_id,
                        transcript_path=transcript_path,
                        meta={"reason": "deferred_subagents", "deferred_count": len(deferred_subagents)},
                    )
            except Exception as e:
                logger.warning("[%s] session %s: subagent merge error: %s", label, session_id, e)
                if _fail_hard_enabled():
                    raise

        if rolling_mode:
            operation_phase = "rolling_stage_extract"
            staged_processed_offset = int(staged_state.get("processed_line_offset", 0) or 0)
            staged_buffered_offset = int(staged_state.get("buffered_line_offset", 0) or 0)
            if (
                staged_state_has_payload(staged_state)
                and bool(staged_state.get(_STAGED_PAYLOAD_PENDING_FLUSH_KEY))
                and staged_processed_offset > int(cursor_offset or 0)
                and staged_processed_offset >= staged_buffered_offset
            ):
                staged_state = _record_staged_source_transcript_path(
                    session_id,
                    staged_state,
                    transcript_path,
                    signal_meta,
                )
                logger.warning(
                    "[%s] session %s: rolling signal cursor is behind an already-staged payload "
                    "(cursor=%d staged_offset=%d); advancing cursor and requeueing staged flush",
                    label,
                    session_id,
                    int(cursor_offset or 0),
                    staged_processed_offset,
                )
                write_cursor(
                    session_id,
                    staged_processed_offset,
                    transcript_path,
                    source_key=lock_owner_key,
                    processed_signal_type=signal_type,
                )
                has_remaining_tail = total_lines > staged_processed_offset
                if has_remaining_tail:
                    continued_transcript_path = _stable_transcript_snapshot_for_continued_rolling(
                        session_id,
                        transcript_path,
                    )
                    remaining_tokens = estimate_unextracted_tokens(
                        continued_transcript_path,
                        staged_processed_offset,
                        chunk_budget,
                    )
                    write_signal(
                        signal_type="rolling",
                        session_id=session_id,
                        transcript_path=continued_transcript_path,
                        dedupe=False,
                        meta={
                            "reason": "continued_chunk_budget",
                            "chunk_tokens": chunk_budget,
                            "chunk_lines": chunk_line_budget,
                            "buffered_line_offset": staged_processed_offset,
                            "remaining_tokens_estimate": remaining_tokens,
                            "remaining_lines": max(0, int(total_lines) - int(staged_processed_offset)),
                            "source_cursor_key": lock_owner_key,
                            "source_transcript_path": transcript_path,
                            "source_transcript_size_bytes": _transcript_size_bytes(transcript_path),
                        },
                    )
                flush_transcript_path = (
                    str(signal_meta.get("source_transcript_path") or "").strip()
                    if _is_daemon_owned_transcript_snapshot_path(transcript_path)
                    else ""
                ) or transcript_path
                write_signal(
                    signal_type="session_end",
                    session_id=session_id,
                    transcript_path=flush_transcript_path,
                    meta={
                        "reason": "rolling_stage_flush",
                        "source_signal": "rolling",
                        "staged_payload_sweep": True,
                        "buffered_line_offset": staged_processed_offset,
                        "flush_staged_payload_only": bool(has_remaining_tail),
                        "source_cursor_key": lock_owner_key,
                        "recovered_staged_payload_retry": True,
                    },
                )
                mark_signal_processed(signal_data)
                if _processed_rolling_snapshot_can_be_cleaned(transcript_path, staged_state):
                    _cleanup_daemon_transcript_snapshot_path(transcript_path)
                return
            if not transcript_text.strip():
                logger.info("[%s] session %s: empty rolling transcript after parsing", label, session_id)
                staged_state["semantic_buffer"] = ""
                staged_state["semantic_buffer_tokens"] = 0
                staged_state["buffered_line_offset"] = buffered_line_offset
                staged_state["processed_line_offset"] = buffered_line_offset
                write_rolling_state(session_id, staged_state)
                write_cursor(
                    session_id,
                    buffered_line_offset,
                    transcript_path,
                    source_key=lock_owner_key,
                    last_flushed_line_offset=cursor_last_flushed_line_offset,
                )
                mark_signal_processed(signal_data)
                _cleanup_daemon_transcript_snapshot_path(transcript_path)
                return
            semantic_tokens = int(staged_state.get("semantic_buffer_tokens", 0) or 0)
            if semantic_tokens < _rolling_ready_threshold(chunk_budget):
                logger.info(
                    "[%s] session %s: rolling signal below semantic threshold (%d < %d); "
                    "preserving buffer for lifecycle flush",
                    label,
                    session_id,
                    semantic_tokens,
                    _rolling_ready_threshold(chunk_budget),
                )
                staged_state["buffered_line_offset"] = buffered_line_offset
                staged_state["processed_line_offset"] = buffered_line_offset
                write_rolling_state(session_id, staged_state)
                write_cursor(
                    session_id,
                    cursor_offset,
                    transcript_path,
                    source_key=lock_owner_key,
                    last_flushed_line_offset=cursor_last_flushed_line_offset,
                )
                mark_signal_processed(signal_data)
                # Keep the stable snapshot while the semantic tail is deferred;
                # idle/newer-session scans need a live cursor target to flush it.
                return
            staged_state = _stage_semantic_buffer_payload(
                session_id=session_id,
                signal_type=signal_type,
                transcript_path=transcript_path,
                label=label,
                owner=owner,
                staged_state=staged_state,
                buffered_line_offset=buffered_line_offset,
                new_lines=new_lines,
                semantic_buffer_metrics=semantic_buffer_metrics,
                chunk_budget=chunk_budget,
                chunk_line_budget=chunk_line_budget,
            )
            staged_state = _record_staged_source_transcript_path(
                session_id,
                staged_state,
                transcript_path,
                signal_meta,
            )
            write_cursor(
                session_id,
                buffered_line_offset,
                transcript_path,
                source_key=lock_owner_key,
                processed_signal_type=signal_type,
            )
            has_remaining_tail = buffered_line_offset > cursor_offset and total_lines > buffered_line_offset
            if has_remaining_tail:
                continued_transcript_path = _stable_transcript_snapshot_for_continued_rolling(
                    session_id,
                    transcript_path,
                )
                remaining_tokens = estimate_unextracted_tokens(
                    continued_transcript_path,
                    buffered_line_offset,
                    chunk_budget,
                )
                write_signal(
                    signal_type="rolling",
                    session_id=session_id,
                    transcript_path=continued_transcript_path,
                    dedupe=False,
                    meta={
                        "reason": "continued_chunk_budget",
                        "chunk_tokens": chunk_budget,
                        "chunk_lines": chunk_line_budget,
                        "buffered_line_offset": buffered_line_offset,
                        "remaining_tokens_estimate": remaining_tokens,
                        "remaining_lines": max(0, int(total_lines) - int(buffered_line_offset)),
                        "source_cursor_key": lock_owner_key,
                        "source_transcript_path": transcript_path,
                        "source_transcript_size_bytes": _transcript_size_bytes(transcript_path),
                    },
                )
            if staged_state_has_payload(staged_state):
                flush_transcript_path = (
                    str(signal_meta.get("source_transcript_path") or "").strip()
                    if _is_daemon_owned_transcript_snapshot_path(transcript_path)
                    else ""
                ) or transcript_path
                write_signal(
                    signal_type="session_end",
                    session_id=session_id,
                    transcript_path=flush_transcript_path,
                    meta={
                        "reason": "rolling_stage_flush",
                        "source_signal": "rolling",
                        "staged_payload_sweep": True,
                        "buffered_line_offset": buffered_line_offset,
                        "flush_staged_payload_only": bool(has_remaining_tail),
                        "source_cursor_key": lock_owner_key,
                    },
                )
            mark_signal_processed(signal_data)
            if _processed_rolling_snapshot_can_be_cleaned(transcript_path, staged_state):
                _cleanup_daemon_transcript_snapshot_path(transcript_path)
            return

        tail_result = None
        operation_phase = "flush_extract"
        usage_before_extract = _read_usage_totals()
        extract_started_at = time.time()
        if transcript_text.strip():
            _reset_daemon_llm_usage_budget()
            tail_result = extract_from_transcript(
                transcript=transcript_text,
                owner_id=owner,
                label=label,
                session_id=session_id,
                dry_run=True,
                carry_facts=list(staged_state.get("carry_facts", []) or []),
                chunk_tokens_override=_daemon_extract_chunk_tokens(chunk_budget),
                llm_timeout_seconds=_daemon_extract_llm_timeout_seconds(),
                llm_slot_wait_timeout_seconds=_daemon_extract_llm_slot_wait_seconds(),
                llm_max_retries=_daemon_extract_llm_max_retries(),
                raise_on_llm_failure=True,
            )
            chunks_processed = int(tail_result.get("chunks_processed", 0) or 0)
            chunks_total = int(tail_result.get("chunks_total", 0) or 0)
            unclassified_empty = int(tail_result.get("unclassified_empty_payloads", 0) or 0)
            if unclassified_empty > 0:
                logger.warning(
                    "[%s] session %s: FLUSH — %d/%d chunks returned empty payloads "
                    "(model responded but no extractable signal); counting as processed",
                    label, session_id, unclassified_empty, chunks_total,
                )
            _failed_chunks = chunks_total - chunks_processed - unclassified_empty
            if _failed_chunks > 0:
                if _fail_hard_enabled():
                    raise RuntimeError(
                        f"[{label}] session {session_id}: FLUSH — {_failed_chunks}/{chunks_total} "
                        "chunks failed extraction while failHard is enabled"
                    )
                # Provider outages raise ProviderUnavailableError and kill the daemon
                # before we get here. This path handles non-provider failures only.
                logger.error(
                    "[%s] session %s: FLUSH — %d/%d chunks failed extraction "
                    "(non-provider failure); saving transcript for janitor recovery",
                    label, session_id, _failed_chunks, chunks_total,
                )
                _save_deferred_extraction(
                    session_id=session_id,
                    transcript_text=transcript_text,
                    owner_id=owner,
                    label=label,
                    reason=f"flush_non_provider_failure_{_failed_chunks}_of_{chunks_total}_chunks",
                )
            _write_rolling_debug_dump(
                "rolling_flush_tail_extract",
                session_id,
                text=transcript_text,
                facts=tail_result.get("raw_facts", []) or [],
                label=label,
                signal_type=signal_type,
                processing_signal_type=flush_metric_processing_signal_type,
                transcript_path=transcript_path,
                cursor_offset=cursor_offset,
                buffered_line_offset=buffered_line_offset,
                total_lines=total_lines,
                chunks_processed=chunks_processed,
                chunks_total=chunks_total,
                assessment_usable=int(tail_result.get("assessment_usable", 0) or 0),
                assessment_nothing_usable=int(tail_result.get("assessment_nothing_usable", 0) or 0),
            )
        extract_wall = time.time() - extract_started_at
        usage_after_extract = _read_usage_totals()

        usage_before_publish = usage_after_extract
        operation_phase = "build_flush_payload"
        flush_payload = build_flush_payload(staged_state, tail_result)
        if harvestable_payloads:
            for child_payload in harvestable_payloads:
                child_id = str(child_payload.get("child_id") or "").strip()
                child_text = str(child_payload.get("child_text") or "").strip()
                if not child_id or not child_text:
                    continue
                # Subagent harvests are separately parsed child transcripts, not
                # rolling buffers, so they keep the configured extraction budget.
                _reset_daemon_llm_usage_budget()
                child_result = extract_from_transcript(
                    transcript=child_text,
                    owner_id=owner,
                    label=f"{label}-subagent",
                    session_id=session_id,
                    dry_run=True,
                    carry_facts=[],
                    llm_timeout_seconds=_daemon_extract_llm_timeout_seconds(),
                    llm_slot_wait_timeout_seconds=_daemon_extract_llm_slot_wait_seconds(),
                    llm_max_retries=_daemon_extract_llm_max_retries(),
                    raise_on_llm_failure=True,
                )
                flush_payload = _append_payload_result(
                    flush_payload,
                    _stamp_subagent_payload(
                        child_result,
                        source_label=f"{label}-subagent-extraction",
                        child_id=child_id,
                    ),
                )
        operation_phase = "flush_publish"
        _write_rolling_debug_dump(
            "rolling_flush_publish_start",
            session_id,
            label=label,
            signal_type=flush_metric_signal_type,
            processing_signal_type=flush_metric_processing_signal_type,
            transcript_path=transcript_path,
            staged_batches=int(staged_state.get("rolling_batches", 0) or 0),
            staged_facts=len(staged_state.get("raw_facts", []) or []),
            final_raw_fact_count=len(flush_payload.get("raw_facts", []) or []),
            raw_facts=_rolling_debug_fact_rows(flush_payload.get("raw_facts", []) or [], fallback_session_id=session_id),
            tail_raw_fact_count=len((tail_result or {}).get("raw_facts", []) or []) if isinstance(tail_result, dict) else 0,
            cursor_offset=cursor_offset,
            buffered_line_offset=buffered_line_offset,
            total_lines=total_lines,
        )
        publish_started_at = time.time()
        result = apply_extracted_payloads(
            flush_payload,
            owner_id=owner,
            label=label,
            session_id=session_id,
            dry_run=False,
            memory_publish_mode="request",
            snippet_journal_write_mode="request",
        )
        publish_wall = time.time() - publish_started_at
        usage_after_publish = _read_usage_totals()
        extract_usage = _usage_delta(usage_before_extract, usage_after_extract)
        publish_usage = _usage_delta(usage_before_publish, usage_after_publish)

        facts_stored = result.get("facts_stored", 0)
        facts_skipped = result.get("facts_skipped", 0)
        edges_created = result.get("edges_created", 0)
        _write_rolling_debug_dump(
            "rolling_flush_publish_done",
            session_id,
            label=label,
            signal_type=flush_metric_signal_type,
            processing_signal_type=flush_metric_processing_signal_type,
            transcript_path=transcript_path,
            final_raw_fact_count=len(flush_payload.get("raw_facts", []) or []),
            final_facts_stored=facts_stored,
            final_facts_skipped=facts_skipped,
            final_edges_created=edges_created,
            raw_facts=_rolling_debug_fact_rows(flush_payload.get("raw_facts", []) or [], fallback_session_id=session_id),
            storage_facts=result.get("facts", []) or [],
        )
        fact_bucket_summary = _summarize_fact_result_buckets(result.get("facts"))
        snippets_count = sum(
            len(v)
            for v in (result.get("snippets", {}) or {}).values()
            if isinstance(v, list)
        )
        journals_count = len(result.get("journal", {}) or {})
        project_log_metrics = dict(result.get("project_log_metrics", {}) or {})
        logger.info("[%s] session %s: %d stored, %d skipped, %d edges",
                    label, session_id, facts_stored, facts_skipped, edges_created)
        if fact_bucket_summary["skip_buckets"]:
            logger.info(
                "[%s] session %s: skip detail %s",
                label,
                session_id,
                json.dumps(fact_bucket_summary["skip_buckets"], sort_keys=True),
            )

        _write_extraction_buffer_log(
            session_id,
            phase="final_flush",
            signal_type=signal_type,
            transcript_text=transcript_text,
        )
        if signal_type == "timeout":
            write_context_refresh_timeout_marker(session_id)
        final_cursor_offset = total_lines
        if flush_staged_payload_only:
            try:
                final_cursor_offset = max(
                    int(cursor_offset or 0),
                    int(signal_meta.get("buffered_line_offset") or buffered_line_offset or 0),
                )
            except Exception:
                final_cursor_offset = int(buffered_line_offset or cursor_offset or 0)
        if ended_rolling_buffer_flush_signal:
            final_cursor_offset = max(int(final_cursor_offset or 0), int(total_lines or 0))
        transcript_rebased_during_flush = False
        transcript_rebased_followup = False
        transcript_rebase_retry_limit_reached = False
        transcript_rebase_reread_failed = False
        if (
            not rolling_mode
            and not staged_payload_sweep_signal
            and transcript_read_guard_count > 0
            and final_cursor_offset >= transcript_read_guard_start + transcript_read_guard_count
        ):
            try:
                current_guard_digest = _transcript_line_window_digest(
                    transcript_path,
                    transcript_read_guard_start,
                    transcript_read_guard_count,
                )
            except OSError as exc:
                transcript_rebase_reread_failed = True
                current_guard_digest = {
                    "start_line": transcript_read_guard_start,
                    "line_count": 0,
                    "digest": "",
                    "read_error": type(exc).__name__,
                }
            if transcript_rebase_reread_failed or current_guard_digest != transcript_read_guard_digest:
                transcript_rebased_during_flush = True
                logger.warning(
                    "[%s] session %s: transcript changed while flush was in flight; "
                    "not advancing cursor past offset %d and queueing follow-up extraction "
                    "(path=%s, read_guard=%s, current_guard=%s, retry_count=%d)",
                    label,
                    session_id,
                    cursor_offset,
                    transcript_path,
                    json.dumps(transcript_read_guard_digest, sort_keys=True),
                    json.dumps(current_guard_digest, sort_keys=True),
                    transcript_rebase_retry_count,
                )
                final_cursor_offset = int(cursor_offset or 0)
                if transcript_rebase_retry_count >= TRANSCRIPT_REBASE_MAX_RETRIES:
                    transcript_rebase_retry_limit_reached = True
                    logger.error(
                        "[%s] session %s: transcript rebase follow-up retry limit reached (%d); "
                        "leaving cursor at offset %d without queueing another synthetic signal",
                        label,
                        session_id,
                        transcript_rebase_retry_count,
                        final_cursor_offset,
                    )
                else:
                    transcript_rebased_followup = True
        _record_daemon_lifecycle_observation(
            signal_data,
            session_id=session_id,
            signal_type=signal_type,
            transcript_path=transcript_path,
        )
        write_cursor(
            session_id,
            final_cursor_offset,
            transcript_path,
            source_key=lock_owner_key,
            processed_signal_type=signal_type,
        )
        if ended_rolling_buffer_flush_signal:
            if ended_rolling_buffer_flush_source_key:
                write_cursor(
                    session_id,
                    final_cursor_offset,
                    transcript_path,
                    source_key=ended_rolling_buffer_flush_source_key,
                    processed_signal_type=signal_type,
                )
            write_cursor(
                session_id,
                final_cursor_offset,
                transcript_path,
                processed_signal_type=signal_type,
            )
        if ended_rolling_buffer_flush_signal:
            clear_rolling_state(session_id)
        elif (
            staged_payload_sweep_signal
            and _semantic_buffer_has_content(staged_state)
            and not drain_unstaged_semantic_buffer_on_sweep
        ):
            write_rolling_state(session_id, clear_staged_payload_from_state(staged_state))
        elif preserve_active_rolling_state_after_flush and _rolling_state_has_pending_content(staged_state):
            logger.info(
                "[%s] session %s: preserving active rolling state after late post-reset content flush",
                label,
                session_id,
            )
            write_rolling_state(session_id, clear_staged_payload_from_state(staged_state))
        else:
            clear_rolling_state(session_id)
        if mark_harvested_fn is not None:
            try:
                for child in harvestable:
                    mark_harvested_fn(session_id, child.get("child_id", ""))
            except Exception as e:
                logger.warning("[%s] session %s: mark_harvested error: %s", label, session_id, e)
        mark_signal_processed(signal_data)
        _release_session_processing_lock(lock_owner_key, lock_fd)
        lock_fd = None

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

        if (
            signal_type == "timeout"
            and bool(signal_data.get("supports_compaction_control"))
            and bool((signal_data.get("meta") or {}).get("compact_on_timeout"))
        ):
            try:
                from core.runtime.events import emit_event
                emit_event(
                    name="memory.force_compaction",
                    payload={"reason": f"inactivity timeout for session {session_id}"},
                    source="daemon.timeout",
                )
                logger.info("[%s] session %s: queued post-timeout compaction request", label, session_id)
            except Exception as e:
                logger.warning("[%s] session %s: failed queuing post-timeout compaction: %s", label, session_id, e)

        try:
            session_logs_transcript_path = _session_logs_ingest_transcript_path_for_signal(
                str(transcript_path),
                session_id=session_id,
                adapter=adapter,
                signal_meta=signal_meta,
                cursor_data=cursor_data,
                staged_state=staged_state,
            )
            if session_logs_transcript_path != str(transcript_path):
                logger.info(
                    "[%s] session %s: session_logs ingest using current source transcript path: %s "
                    "(signal path=%s)",
                    label,
                    session_id,
                    session_logs_transcript_path,
                    transcript_path,
                )
            sl_result = _request_session_logs_ingest(
                session_id=session_id,
                owner_id=owner,
                label=label,
                transcript_path=session_logs_transcript_path,
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
            if _fail_hard_enabled():
                raise RuntimeError("session_logs ingest failed") from e

        signal_to_publish_seconds = None
        raw_signal_ts = str(signal_data.get("timestamp", "") or "").strip()
        if raw_signal_ts:
            try:
                signal_dt = datetime.fromisoformat(raw_signal_ts.replace("Z", "+00:00"))
                signal_to_publish_seconds = round(time.time() - signal_dt.timestamp(), 3)
            except Exception:
                signal_to_publish_seconds = None

        write_rolling_metric(
            "rolling_flush",
            session_id,
            signal_type=flush_metric_signal_type,
            processing_signal_type=flush_metric_processing_signal_type,
            signal_timestamp=signal_data.get("timestamp"),
            staged_batches=int(staged_state.get("rolling_batches", 0) or 0),
            staged_facts=len(staged_state.get("raw_facts", []) or []),
            final_raw_fact_count=len(flush_payload.get("raw_facts", []) or []),
            final_facts_stored=facts_stored,
            final_facts_skipped=facts_skipped,
            final_edges_created=edges_created,
            fact_status_counts=fact_bucket_summary["status_counts"],
            skip_buckets=fact_bucket_summary["skip_buckets"],
            snippets_count=snippets_count,
            journals_count=journals_count,
            project_logs_seen=int(project_log_metrics.get("entries_seen", 0) or 0),
            project_logs_written=int(project_log_metrics.get("entries_written", 0) or 0),
            project_logs_queued=int(project_log_metrics.get("entries_queued", 0) or 0),
            project_log_queue_failures=int(project_log_metrics.get("queue_failures", 0) or 0),
            project_logs_projects_updated=int(project_log_metrics.get("projects_updated", 0) or 0),
            extract_wall_seconds=round(extract_wall, 3),
            publish_wall_seconds=round(publish_wall, 3),
            flush_wall_seconds=round(extract_wall + publish_wall, 3),
            extract_llm_calls=int(extract_usage.get("calls", 0) or 0),
            extract_fast_calls=int(extract_usage.get("fast_calls", 0) or 0),
            extract_deep_calls=int(extract_usage.get("deep_calls", 0) or 0),
            extract_input_tokens=int(extract_usage.get("input_tokens", 0) or 0),
            extract_output_tokens=int(extract_usage.get("output_tokens", 0) or 0),
            publish_llm_calls=int(publish_usage.get("calls", 0) or 0),
            publish_fast_calls=int(publish_usage.get("fast_calls", 0) or 0),
            publish_deep_calls=int(publish_usage.get("deep_calls", 0) or 0),
            publish_input_tokens=int(publish_usage.get("input_tokens", 0) or 0),
            publish_output_tokens=int(publish_usage.get("output_tokens", 0) or 0),
            signal_to_publish_seconds=signal_to_publish_seconds,
            carry_facts_final=len(flush_payload.get("carry_facts", []) or []),
            carry_duplicate_facts_dropped=int(flush_payload.get("carry_duplicate_facts_dropped", 0) or 0),
            dedup_hash_exact_hits=int(result.get("dedup_hash_exact_hits", 0) or 0),
            payload_duplicate_facts_collapsed=int(result.get("payload_duplicate_facts_collapsed", 0) or 0),
            dedup_scanned_rows=int(result.get("dedup_scanned_rows", 0) or 0),
            dedup_gray_zone_rows=int(result.get("dedup_gray_zone_rows", 0) or 0),
            dedup_llm_checks=int(result.get("dedup_llm_checks", 0) or 0),
            dedup_llm_same_hits=int(result.get("dedup_llm_same_hits", 0) or 0),
            dedup_llm_different_hits=int(result.get("dedup_llm_different_hits", 0) or 0),
            dedup_fallback_reject_hits=int(result.get("dedup_fallback_reject_hits", 0) or 0),
            dedup_auto_reject_hits=int(result.get("dedup_auto_reject_hits", 0) or 0),
            dedup_vec_query_count=int(result.get("dedup_vec_query_count", 0) or 0),
            dedup_vec_candidates_returned=int(result.get("dedup_vec_candidates_returned", 0) or 0),
            dedup_vec_candidate_limit=int(result.get("dedup_vec_candidate_limit", 0) or 0),
            dedup_vec_limit_hits=int(result.get("dedup_vec_limit_hits", 0) or 0),
            dedup_fts_query_count=int(result.get("dedup_fts_query_count", 0) or 0),
            dedup_fts_candidates_returned=int(result.get("dedup_fts_candidates_returned", 0) or 0),
            dedup_fts_candidate_limit=int(result.get("dedup_fts_candidate_limit", 0) or 0),
            dedup_fts_limit_hits=int(result.get("dedup_fts_limit_hits", 0) or 0),
            dedup_fallback_scan_count=int(result.get("dedup_fallback_scan_count", 0) or 0),
            dedup_fallback_candidates_returned=int(
                result.get("dedup_fallback_candidates_returned", 0) or 0
            ),
            dedup_token_prefilter_terms=int(result.get("dedup_token_prefilter_terms", 0) or 0),
            dedup_token_prefilter_skips=int(result.get("dedup_token_prefilter_skips", 0) or 0),
            embedding_cache_requested=int(result.get("embedding_cache_requested", 0) or 0),
            embedding_cache_unique=int(result.get("embedding_cache_unique", 0) or 0),
            embedding_cache_hits=int(result.get("embedding_cache_hits", 0) or 0),
            embedding_cache_warmed=int(result.get("embedding_cache_warmed", 0) or 0),
            embedding_cache_failed=int(result.get("embedding_cache_failed", 0) or 0),
            staged_semantic_duplicate_facts_collapsed=int(
                flush_payload.get("staged_semantic_duplicate_facts_collapsed", 0) or 0
            ),
            staged_semantic_auto_reject_hits=int(
                flush_payload.get("staged_semantic_auto_reject_hits", 0) or 0
            ),
            staged_semantic_gray_zone_rows=int(
                flush_payload.get("staged_semantic_gray_zone_rows", 0) or 0
            ),
            staged_semantic_llm_checks=int(
                flush_payload.get("staged_semantic_llm_checks", 0) or 0
            ),
            staged_semantic_llm_same_hits=int(
                flush_payload.get("staged_semantic_llm_same_hits", 0) or 0
            ),
            staged_semantic_llm_different_hits=int(
                flush_payload.get("staged_semantic_llm_different_hits", 0) or 0
            ),
            root_chunks=int(flush_payload.get("root_chunks", 0) or 0),
            split_events=int(flush_payload.get("split_events", 0) or 0),
            split_child_chunks=int(flush_payload.get("split_child_chunks", 0) or 0),
            leaf_chunks=int(flush_payload.get("leaf_chunks", 0) or 0),
            max_split_depth=int(flush_payload.get("max_split_depth", 0) or 0),
            deep_calls=int(flush_payload.get("deep_calls", 0) or 0),
            repair_calls=int(flush_payload.get("repair_calls", 0) or 0),
            assessment_usable=int(flush_payload.get("assessment_usable", 0) or 0),
            assessment_nothing_usable=int(flush_payload.get("assessment_nothing_usable", 0) or 0),
            assessment_needs_smaller_chunk=int(flush_payload.get("assessment_needs_smaller_chunk", 0) or 0),
            unclassified_empty_payloads=int(flush_payload.get("unclassified_empty_payloads", 0) or 0),
            transcript_rebased_during_flush=transcript_rebased_during_flush,
            transcript_rebase_retry_count=transcript_rebase_retry_count,
            transcript_rebase_retry_limit_reached=transcript_rebase_retry_limit_reached,
            transcript_rebase_reread_failed=transcript_rebase_reread_failed,
        )
        if transcript_rebased_followup:
            write_signal(
                signal_type="session_end",
                session_id=session_id,
                transcript_path=transcript_path,
                meta={
                    "reason": "transcript_rebased_during_flush",
                    "source_cursor_key": lock_owner_key,
                    "prior_cursor_offset": int(cursor_offset or 0),
                    "read_guard_start": transcript_read_guard_start,
                    "read_guard_count": transcript_read_guard_count,
                    "transcript_rebase_retry_count": transcript_rebase_retry_count + 1,
                    "transcript_rebase_max_retries": TRANSCRIPT_REBASE_MAX_RETRIES,
                },
            )

    except Exception as e:
        if _fail_hard_enabled():
            raise
        should_write_flush_error = (
            not rolling_mode
            and signal_type in ("compaction", "reset", "session_end", "timeout")
            and staged_state_has_payload(staged_state)
        )
        if should_write_flush_error:
            extract_wall = round((time.time() - extract_started_at), 3) if extract_started_at else 0.0
            publish_wall = round((time.time() - publish_started_at), 3) if publish_started_at else 0.0
            signal_to_publish_seconds = None
            raw_signal_ts = str(signal_data.get("timestamp", "") or "").strip()
            if raw_signal_ts:
                try:
                    signal_dt = datetime.fromisoformat(raw_signal_ts.replace("Z", "+00:00"))
                    signal_to_publish_seconds = round(time.time() - signal_dt.timestamp(), 3)
                except Exception:
                    signal_to_publish_seconds = None
            write_rolling_metric(
                "rolling_flush_error",
                session_id,
                signal_type=flush_metric_signal_type,
                processing_signal_type=flush_metric_processing_signal_type,
                signal_timestamp=signal_data.get("timestamp"),
                phase=operation_phase,
                error_type=type(e).__name__,
                error_message=str(e),
                traceback_tail=" | ".join(traceback.format_exc().strip().splitlines()[-4:]),
                staged_batches=int(staged_state.get("rolling_batches", 0) or 0),
                staged_facts=len(staged_state.get("raw_facts", []) or []),
                final_raw_fact_count=len(flush_payload.get("raw_facts", []) or []),
                extract_wall_seconds=extract_wall,
                publish_wall_seconds=publish_wall,
                signal_to_publish_seconds=signal_to_publish_seconds,
            )
        logger.error("[%s] session %s: extraction failed (signal preserved for retry): %s",
                     label, session_id, e, exc_info=True)
    finally:
        _release_session_processing_lock(lock_owner_key, lock_fd)
        if tmp_path:
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
    # If the lower-bound file is first created during an idle sweep, returning
    # "now" would immediately classify any already-written fresh transcript as
    # older than installedAt and skip timeout extraction forever. Seed the file
    # for future scans, but let this first scan evaluate existing transcripts.
    return 0.0


def _get_idle_timeout_minutes(default: int = 30) -> int:
    """Read timeout minutes from live config with a safe fallback.

    Reads the raw JSON config files directly to bypass the module-level config
    cache.  The daemon is a long-running process; the timeout setting may be
    written after the daemon starts, and get_config() would return the stale
    cached value for the lifetime of the process.
    """
    try:
        import json as _json
        from config import _config_paths
        config_paths = list(_config_paths())
    except Exception as exc:
        logger.warning("idle timeout config path lookup failed; using default %s: %s", default, exc)
        if _fail_hard_enabled():
            raise
        return default
    raw: int = default
    for _cp in reversed(config_paths):
        if _cp.exists():
            try:
                _data = _json.loads(_cp.read_text(encoding="utf-8"))
                _capture = _data.get("capture", {})
                if "inactivity_timeout_minutes" in _capture:
                    _v = _capture.get("inactivity_timeout_minutes")
                else:
                    _v = _capture.get("inactivityTimeoutMinutes")
                if _v is not None:
                    raw = _v
            except Exception as exc:
                logger.warning("idle timeout config read failed for %s; ignoring file: %s", _cp, exc)
                if _fail_hard_enabled():
                    raise
    try:
        return max(0, int(raw))
    except Exception as exc:
        logger.warning("idle timeout config value %r is invalid; using default %s: %s", raw, default, exc)
        if _fail_hard_enabled():
            raise
        return default


def _get_compact_on_timeout(default: bool = True) -> bool:
    """Read timeout compaction toggle from live config with legacy alias support."""
    try:
        import json as _json
        from config import _config_paths
        config_paths = list(_config_paths())
    except Exception as exc:
        logger.warning("timeout compaction config path lookup failed; using default %s: %s", default, exc)
        if _fail_hard_enabled():
            raise
        return default
    raw: bool = default
    for _cp in reversed(config_paths):
        if _cp.exists():
            try:
                _data = _json.loads(_cp.read_text(encoding="utf-8"))
                _capture = _data.get("capture", {})
                if "compact_on_timeout" in _capture:
                    raw = bool(_capture.get("compact_on_timeout"))
                elif "compactOnTimeout" in _capture:
                    raw = bool(_capture.get("compactOnTimeout"))
                elif "auto_compaction_on_timeout" in _capture:
                    raw = bool(_capture.get("auto_compaction_on_timeout"))
                elif "autoCompactionOnTimeout" in _capture:
                    raw = bool(_capture.get("autoCompactionOnTimeout"))
            except Exception as exc:
                logger.warning("timeout compaction config read failed for %s; ignoring file: %s", _cp, exc)
                if _fail_hard_enabled():
                    raise
    return bool(raw)


def _adapter_supports_compaction_control() -> bool:
    try:
        from lib.adapter import get_adapter
        adapter = get_adapter()
        return bool(adapter.get_capability("supports_compaction_control", False))
    except Exception:
        return False


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
    _reload_config_if_changed("idle session check")
    adapter = _load_runtime_adapter()
    _ensure_discovered_session_cursors(adapter)
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
    except Exception as exc:
        logger.warning("idle subagent registry load failed: %s", exc)
        if _fail_hard_enabled():
            raise RuntimeError("idle subagent registry load failed") from exc

    # B003: Hoist pending signals read outside the loop
    pending = read_pending_signals()
    pending_session_ids = {s.get("session_id") for s in pending}
    pending_source_keys = _pending_signal_source_keys(pending)
    _queue_missing_staged_rolling_flushes_from_state(
        pending_session_ids=pending_session_ids,
        pending_source_keys=pending_source_keys,
        scanner="idle-state-recovery",
    )

    cursor_rows: list[dict[str, Any]] = []
    for cursor_file in cursor_dir.glob("*.json"):
        try:
            data = json.loads(cursor_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        session_id = data.get("session_id", "")
        transcript_path = data.get("transcript_path", "")
        if not session_id or not transcript_path:
            continue
        if not os.path.isfile(transcript_path):
            _reap_orphaned_cursor_if_stale(
                cursor_file=cursor_file,
                session_id=str(session_id),
                transcript_path=str(transcript_path),
                now=now,
                adapter=adapter,
                scanner="idle scan",
            )
            continue
        if _is_daemon_preserved_session_transcript_path(str(transcript_path)):
            active_cursor, active_path, active_key = _active_source_cursor_for_empty_preserved_cursor(
                str(session_id),
                str(transcript_path),
            )
            if active_cursor and active_path and active_key:
                logger.info(
                    "session %s idle scan using live source cursor %s for empty preserved alias %s",
                    session_id,
                    active_key,
                    transcript_path,
                )
                data = active_cursor
                transcript_path = active_path
                cursor_file = _cursor_dir() / f"{active_key}.json"
            else:
                # Preserved daemon mirrors are valid lifecycle inputs, but they are
                # not active host transcripts. Let discovery replace stale mirrors
                # with the live adapter path; otherwise do not let an old mirror fire
                # timeout extraction before the host materializes the real session.
                live_path = _live_transcript_for_preserved_mirror(
                    str(session_id),
                    str(transcript_path),
                    adapter=adapter,
                )
                if not live_path:
                    continue
                logger.info(
                    "session %s idle scan using live transcript %s for preserved cursor %s",
                    session_id,
                    live_path,
                    transcript_path,
                )
                transcript_path = live_path
        if _is_discovery_artifact_transcript(Path(str(transcript_path))):
            continue
        if _retire_shadowed_cursor_alias(
            cursor_file=cursor_file,
            session_id=str(session_id),
            transcript_path=str(transcript_path),
            cursor_data=data,
        ):
            continue
        if not _cursor_or_adapter_owns_transcript_path(
            adapter,
            str(session_id),
            str(transcript_path),
            cursor_data=data,
        ):
            continue
        current_size_bytes = _transcript_size_bytes(str(transcript_path))
        cursor_size_bytes = int(data.get("transcript_size_bytes", 0) or 0)
        transcript_grew_since_cursor = current_size_bytes > cursor_size_bytes
        internal_state = _reconcile_internal_cursor_state(
            session_id,
            transcript_path,
            cursor_data=data,
            cursor_key=str(data.get("cursor_key") or "").strip() or None,
            adapter=adapter,
            advance_internal=not transcript_grew_since_cursor,
        )
        if internal_state == "frozen":
            continue
        if internal_state == "advanced":
            if transcript_grew_since_cursor:
                write_cursor(
                    session_id,
                    int(data.get("line_offset", 0) or 0),
                    str(transcript_path),
                    internal=False,
                    source_key=str(data.get("cursor_key") or "").strip() or None,
                )
                logger.info(
                    "session %s appears internal maintenance-only during idle scan; "
                    "leaving cursor offset unchanged because transcript grew since cursor",
                    session_id,
                )
                continue
            logger.info(
                "session %s marked internal maintenance-only during idle scan; "
                "advancing cursor to EOF and skipping timeout (transcript=%s, "
                "cursor_offset=%s, cursor_size_bytes=%s)",
                session_id,
                transcript_path,
                data.get("line_offset", 0),
                data.get("transcript_size_bytes", 0),
            )
            continue
        if internal_state == "ignored":
            logger.info(
                "session %s has only ignored user content during idle scan; "
                "advancing non-internal cursor to EOF",
                session_id,
            )
            continue
        if internal_state == "unfrozen":
            logger.info(
                "session %s gained non-internal content past a frozen internal cursor during idle scan",
                session_id,
            )
            data = _read_cursor_with_source_compat(
                str(session_id),
                str(data.get("cursor_key") or "").strip() or None,
            )
            transcript_path = str(data.get("transcript_path") or transcript_path)

        # Skip registered subagents — their transcripts are merged into parent extraction
        if session_id in registered_subagents:
            continue

        try:
            mtime = os.path.getmtime(transcript_path)
        except OSError:
            continue

        cursor_rows.append({
            "session_id": session_id,
            "transcript_path": transcript_path,
            "cursor_offset": int(data.get("line_offset", 0) or 0),
            "cursor_size_bytes": int(data.get("transcript_size_bytes", 0) or 0),
            "has_cursor_size_bytes": "transcript_size_bytes" in data,
            "current_size_bytes": current_size_bytes,
            "mtime": mtime,
            "cursor_data": data,
        })

    _queue_idle_semantic_rolling_flushes_without_cursor(
        pending_session_ids=pending_session_ids,
        pending_source_keys=pending_source_keys,
        known_cursor_session_ids={
            str(row.get("session_id") or "").strip()
            for row in cursor_rows
        },
        now=now,
        timeout_seconds=timeout_seconds,
        installed_at_ts=installed_at_ts,
    )

    # Process recently-active sessions first. Old stale cursors can be expensive
    # to re-extract and should not starve the session that just crossed its idle
    # timeout in front of the user.
    cursor_rows.sort(
        key=lambda row: (float(row.get("mtime", 0.0) or 0.0), str(row.get("session_id") or "")),
        reverse=True,
    )

    for row in cursor_rows:
        session_id = str(row["session_id"])
        transcript_path = str(row["transcript_path"])
        cursor_offset = int(row["cursor_offset"])
        cursor_size_bytes = int(row.get("cursor_size_bytes", 0) or 0)
        has_cursor_size_bytes = bool(row.get("has_cursor_size_bytes", False))
        current_size_bytes = int(row.get("current_size_bytes", 0) or 0)
        mtime = float(row["mtime"])
        row_cursor_data = row.get("cursor_data") if isinstance(row.get("cursor_data"), dict) else {}

        # Check if transcript has grown past cursor
        total_lines = count_transcript_lines(transcript_path)
        transcript_grew_past_cursor = has_cursor_size_bytes and current_size_bytes > cursor_size_bytes
        cursor_at_end = total_lines <= cursor_offset and not transcript_grew_past_cursor

        # If cursor has advanced past a previously-seen end, reset the "fired" marker
        # so new content can trigger a fresh timeout signal if needed.
        if not cursor_at_end:
            _cursor_end_timeout_fired.discard(session_id)

        # Even when all transcript content has been extracted or buffered to EOF,
        # staged rolling state still needs to be flushed. Short OC queued turns can
        # sit below the rolling threshold as semantic_buffer after /new rollover.
        # Flush those only when newer session activity proves the old turn closed.
        rolling: Dict[str, Any] = {}
        has_staged_payload = False
        has_semantic_buffer = False
        semantic_buffer_at_end = False
        try:
            rolling = read_rolling_state(session_id)
            has_staged_payload = staged_state_has_payload(rolling)
            has_semantic_buffer = _semantic_buffer_has_content(rolling)
            buffered_line_offset = int(rolling.get("buffered_line_offset", cursor_offset) or 0)
            semantic_buffer_at_end = bool(has_semantic_buffer and total_lines <= max(buffered_line_offset, cursor_offset))
        except Exception as exc:
            if _fail_hard_enabled():
                raise
            logger.warning("rolling state read failed during idle scan for %s: %s", session_id, exc)
        has_flushable_rolling_content = bool(
            has_staged_payload or (has_semantic_buffer and (cursor_at_end or semantic_buffer_at_end))
        )

        if has_flushable_rolling_content and session_id not in pending_session_ids:
            source_key = _signal_source_cursor_key(session_id, transcript_path, cursor_data=row_cursor_data)
            if _source_signal_already_pending(
                pending_source_keys=pending_source_keys,
                source_key=source_key,
                session_id=session_id,
                scanner="idle",
            ):
                continue
            if has_staged_payload and bool(rolling.get(_STAGED_PAYLOAD_PENDING_FLUSH_KEY)):
                logger.warning(
                    "session %s has staged rolling payload without a pending flush signal; regenerating rolling_stage_flush",
                    session_id,
                )
                write_signal(
                    signal_type="session_end",
                    session_id=session_id,
                    transcript_path=transcript_path,
                    meta={
                        "reason": "rolling_stage_flush",
                        "source_signal": "rolling",
                        "staged_payload_sweep": True,
                        "recovered_missing_flush": True,
                        "source_cursor_key": source_key,
                        "buffered_line_offset": int(
                            rolling.get("buffered_line_offset", cursor_offset) or cursor_offset
                        ),
                        "flush_staged_payload_only": bool(_semantic_buffer_has_content(rolling)),
                    },
                )
                _remember_queued_source_signal(
                    pending_session_ids=pending_session_ids,
                    pending_source_keys=pending_source_keys,
                    session_id=session_id,
                    source_key=source_key,
                )
                continue
            newer_session_exists = any(
                float(other["mtime"]) > mtime and str(other["session_id"]) != session_id
                for other in cursor_rows
            )
            if newer_session_exists:
                logger.info(
                    "session %s has pending rolling content and newer session activity detected, generating session_end flush",
                    session_id,
                )
                write_signal(
                    signal_type="session_end",
                    session_id=session_id,
                    transcript_path=transcript_path,
                )
                _remember_queued_source_signal(
                    pending_session_ids=pending_session_ids,
                    pending_source_keys=pending_source_keys,
                    session_id=session_id,
                    source_key=source_key,
                )
                continue

        if cursor_at_end and not has_flushable_rolling_content:
            terminal_signal_type = str(row_cursor_data.get("processed_signal_type") or "").strip()
            if terminal_signal_type in ("session_end", "timeout", "reset", "compaction"):
                _cursor_end_timeout_fired.add(session_id)
                continue
            # All content already extracted via rolling, but session may be idle without /exit.
            # Fire timeout signal for genuinely idle sessions.
            # Only fires once per session — _cursor_end_timeout_fired prevents repeated signals
            # for sessions where rolling already extracted all content and cursor never advances.
            if session_id not in _cursor_end_timeout_fired:
                if (
                    mtime >= installed_at_ts
                    and (now - mtime) >= timeout_seconds
                    and session_id not in pending_session_ids
                ):
                    source_key = _signal_source_cursor_key(session_id, transcript_path, cursor_data=row_cursor_data)
                    if _source_signal_already_pending(
                        pending_source_keys=pending_source_keys,
                        source_key=source_key,
                        session_id=session_id,
                        scanner="idle",
                    ):
                        continue
                    logger.info(
                        "session %s idle for %.0fs with cursor at end, generating timeout signal",
                        session_id,
                        now - mtime,
                    )
                    write_signal(
                        signal_type="timeout",
                        session_id=session_id,
                        transcript_path=transcript_path,
                        supports_compaction_control=_adapter_supports_compaction_control(),
                        meta={"compact_on_timeout": _get_compact_on_timeout()},
                    )
                    _remember_queued_source_signal(
                        pending_session_ids=pending_session_ids,
                        pending_source_keys=pending_source_keys,
                        session_id=session_id,
                        source_key=source_key,
                    )
                    _cursor_end_timeout_fired.add(session_id)
            continue

        # Check transcript modification time for idle detection
        if mtime < installed_at_ts:
            continue

        idle_seconds = now - mtime
        if idle_seconds < timeout_seconds:
            continue

        # Check if we already have a pending signal for this session
        if session_id in pending_session_ids:
            continue
        source_key = _signal_source_cursor_key(session_id, transcript_path, cursor_data=row_cursor_data)
        if _source_signal_already_pending(
            pending_source_keys=pending_source_keys,
            source_key=source_key,
            session_id=session_id,
            scanner="idle",
        ):
            continue

        logger.info(
            "session %s idle for %.0fs with %d unextracted lines%s, generating timeout signal",
            session_id, idle_seconds, total_lines - cursor_offset,
            " (staged rolling payload pending flush)" if has_staged_payload and cursor_at_end else "",
        )
        write_signal(
            signal_type="timeout",
            session_id=session_id,
            transcript_path=transcript_path,
            supports_compaction_control=_adapter_supports_compaction_control(),
            meta={"compact_on_timeout": _get_compact_on_timeout()},
        )
        _remember_queued_source_signal(
            pending_session_ids=pending_session_ids,
            pending_source_keys=pending_source_keys,
            session_id=session_id,
            source_key=source_key,
        )


def _effective_idle_timeout_minutes(
    configured_timeout_minutes: int,
    *,
    fallback_minutes: int = 120,
    max_timeout_minutes: int = 120,
) -> int:
    """Return a finite timeout for idle extraction/system health."""
    try:
        raw = int(configured_timeout_minutes)
    except Exception:
        raw = 0
    if raw <= 0:
        return int(fallback_minutes)
    return min(raw, int(max_timeout_minutes))


def _rolling_ready_threshold(chunk_budget: int) -> int:
    """Return the semantic-token threshold at which a rolling buffer is ready."""
    budget = max(1, int(chunk_budget or 1))
    # Token estimation is deliberately approximate. Do not let a full semantic
    # buffer miss rolling extraction just because it lands a few estimated tokens
    # under the configured chunk budget.
    return max(1, min(budget, max(int(budget * 0.95), budget - 64)))


def _rolling_prior_buffer_threshold(chunk_budget: int) -> int:
    """Return the minimum prior-buffer size worth staging without the crossing tail."""
    budget = max(1, int(chunk_budget or 1))
    return max(1, int(budget * 0.70))


def check_chunk_ready_sessions(chunk_tokens: Optional[int] = None) -> None:
    """Queue rolling extraction for sessions whose unprocessed tail crossed chunk budget."""
    _reload_config_if_changed("rolling chunk check")
    adapter = _load_runtime_adapter()
    _ensure_discovered_session_cursors(adapter)
    cursor_dir = _cursor_dir()
    if not cursor_dir.is_dir():
        return

    chunk_budget = int(chunk_tokens or _get_capture_chunk_tokens())
    chunk_line_budget = _get_capture_chunk_max_lines()
    pending = read_pending_signals()
    pending_session_ids = {
        str(s.get("session_id") or "").strip()
        for s in pending
    }
    pending_rolling_session_ids = {
        str(s.get("session_id") or "").strip()
        for s in pending
        if str(s.get("type") or s.get("signal_type") or "") == "rolling"
    }
    pending_source_keys = _pending_signal_source_keys(pending)
    pre_recovery_source_keys = set(pending_source_keys)
    _queue_missing_staged_rolling_flushes_from_state(
        pending_session_ids=pending_session_ids,
        pending_source_keys=pending_source_keys,
        scanner="rolling-state-recovery",
    )
    pending_rolling_source_keys = _pending_signal_source_keys(pending, signal_type="rolling")
    pending_rolling_source_keys.update(pending_source_keys - pre_recovery_source_keys)
    pending_session_end_session_ids = {
        str(s.get("session_id") or "").strip()
        for s in pending
        if str(s.get("type") or s.get("signal_type") or "") == "session_end"
    }

    for cursor_file in cursor_dir.glob("*.json"):
        try:
            data = json.loads(cursor_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        session_id = data.get("session_id", "")
        transcript_path = data.get("transcript_path", "")
        if not session_id or not transcript_path:
            continue
        if not os.path.isfile(transcript_path):
            mirror_path = _preserved_mirror_for_missing_transcript_cursor(
                str(session_id),
                str(transcript_path),
                adapter=adapter,
            )
            if mirror_path:
                if str(session_id) in pending_session_end_session_ids:
                    continue
                cursor_key_for_signal = str(data.get("cursor_key") or cursor_file.stem or session_id).strip()
                cursor_offset_for_flush = min(
                    int(data.get("line_offset", 0) or 0),
                    count_transcript_lines(mirror_path),
                )
                logger.info(
                    "session %s rolling scan found missing transcript %s with preserved transcript %s; "
                    "queueing lifecycle flush from preserved cursor offset %d",
                    session_id,
                    transcript_path,
                    mirror_path,
                    cursor_offset_for_flush,
                )
                write_cursor(
                    str(session_id),
                    cursor_offset_for_flush,
                    mirror_path,
                    internal=bool(data.get("internal", False)),
                    source_key=cursor_key_for_signal,
                )
                write_signal(
                    signal_type="session_end",
                    session_id=str(session_id),
                    transcript_path=mirror_path,
                    meta={
                        "reason": "ended_rolling_buffer_flush",
                        "source_cursor_key": cursor_key_for_signal,
                    },
                )
                pending_session_end_session_ids.add(str(session_id))
            else:
                _reap_orphaned_cursor_if_stale(
                    cursor_file=cursor_file,
                    session_id=str(session_id),
                    transcript_path=str(transcript_path),
                    now=time.time(),
                    adapter=adapter,
                    scanner="rolling scan",
                )
            continue
        buffer_transcript_path = str(transcript_path)
        if _is_daemon_preserved_session_transcript_path(str(transcript_path)):
            active_cursor, active_path, active_key = _active_source_cursor_for_empty_preserved_cursor(
                str(session_id),
                str(transcript_path),
            )
            if active_cursor and active_path and active_key:
                logger.info(
                    "session %s rolling scan using live source cursor %s for empty preserved alias %s",
                    session_id,
                    active_key,
                    transcript_path,
                )
                data = active_cursor
                transcript_path = active_path
                buffer_transcript_path = str(transcript_path)
                cursor_file = _cursor_dir() / f"{active_key}.json"
            else:
                if _transcript_size_bytes(str(transcript_path)) <= 0:
                    continue
                live_path = _larger_live_transcript_for_preserved_mirror(
                    str(session_id),
                    str(transcript_path),
                    adapter=adapter,
                )
                if live_path:
                    logger.info(
                        "session %s rolling scan using larger live transcript %s instead of preserved mirror %s",
                        session_id,
                        live_path,
                        transcript_path,
                    )
                    transcript_path = live_path
                    buffer_transcript_path = live_path
                else:
                    logger.info(
                        "session %s rolling scan using non-empty preserved transcript %s",
                        session_id,
                        transcript_path,
                    )
        else:
            mirror_path = _larger_preserved_mirror_for_live_transcript(
                str(session_id),
                str(transcript_path),
                adapter=adapter,
            )
            if mirror_path:
                if (
                    _is_daemon_rolling_transcript_snapshot_path(str(transcript_path))
                    and not _adapter_live_transcript_exists(str(session_id), adapter=adapter)
                ):
                    if str(session_id) in pending_session_end_session_ids:
                        continue
                    mirror_total_lines = count_transcript_lines(mirror_path)
                    cursor_key_for_signal = str(data.get("cursor_key") or cursor_file.stem or session_id).strip()
                    cursor_offset_for_flush = min(int(data.get("line_offset", 0) or 0), mirror_total_lines)
                    logger.info(
                        "session %s rolling scan found ended snapshot %s with preserved transcript %s; "
                        "queueing lifecycle flush from preserved cursor offset %d",
                        session_id,
                        transcript_path,
                        mirror_path,
                        cursor_offset_for_flush,
                    )
                    write_cursor(
                        session_id,
                        cursor_offset_for_flush,
                        mirror_path,
                        internal=bool(data.get("internal", False)),
                        source_key=cursor_key_for_signal,
                    )
                    write_signal(
                        signal_type="session_end",
                        session_id=session_id,
                        transcript_path=mirror_path,
                        meta={
                            "reason": "ended_rolling_buffer_flush",
                            "source_cursor_key": cursor_key_for_signal,
                        },
                    )
                    pending_session_end_session_ids.add(str(session_id))
                    continue
                logger.info(
                    "session %s rolling scan reading larger preserved mirror %s while preserving live cursor %s",
                    session_id,
                    mirror_path,
                    transcript_path,
                )
                buffer_transcript_path = mirror_path
        if _is_discovery_artifact_transcript(Path(str(transcript_path))):
            continue
        active_cursor, active_file, active_key = _active_source_cursor_for_terminal_checkpoint_tail(
            cursor_file=cursor_file,
            session_id=str(session_id),
            transcript_path=str(transcript_path),
            cursor_data=data,
        )
        if active_cursor and active_key:
            logger.info(
                "session %s rolling scan using source cursor %s past checkpoint session_end "
                "(offset=%s transcript=%s)",
                session_id,
                active_key,
                active_cursor.get("line_offset", 0),
                transcript_path,
            )
            data = active_cursor
            session_id = str(data.get("session_id") or session_id)
            cursor_file = active_file
        using_stale_source_cursor_for_grown_transcript = False
        active_cursor, active_file, active_key = _active_source_cursor_for_grown_transcript(
            cursor_file=cursor_file,
            session_id=str(session_id),
            transcript_path=str(transcript_path),
            cursor_data=data,
        )
        if active_cursor and active_key:
            logger.info(
                "session %s rolling scan using stale source cursor %s for grown transcript "
                "(offset=%s cursor_size=%s current_size=%s transcript=%s)",
                session_id,
                active_key,
                active_cursor.get("line_offset", 0),
                active_cursor.get("transcript_size_bytes", 0),
                _transcript_size_bytes(str(transcript_path)),
                transcript_path,
            )
            data = active_cursor
            session_id = str(data.get("session_id") or session_id)
            cursor_file = active_file
            using_stale_source_cursor_for_grown_transcript = True
        if _retire_shadowed_cursor_alias(
            cursor_file=cursor_file,
            session_id=str(session_id),
            transcript_path=str(transcript_path),
            cursor_data=data,
        ):
            continue
        if not _cursor_or_adapter_owns_transcript_path(
            adapter,
            str(session_id),
            str(transcript_path),
            cursor_data=data,
        ):
            continue
        # Size/cursor identity stays on transcript_path even when the semantic
        # buffer reads from a larger mirror. Comparing growth against the mirror
        # here would make an unchanged mirror look newly grown on every scan.
        current_size_bytes = _transcript_size_bytes(str(transcript_path))
        cursor_size_bytes = int(data.get("transcript_size_bytes", 0) or 0)
        transcript_grew_since_cursor = current_size_bytes > cursor_size_bytes
        recently_modified = False
        try:
            recently_modified = (
                time.time() - os.path.getmtime(str(transcript_path))
            ) < _ROLLING_INTERNAL_ADVANCE_GRACE_SECONDS
        except OSError as exc:
            if _should_raise_transcript_stat_error(str(transcript_path), exc):
                raise
            recently_modified = False
        defer_internal_advance = transcript_grew_since_cursor or recently_modified
        internal_state = _reconcile_internal_cursor_state(
            session_id,
            transcript_path,
            cursor_data=data,
            cursor_key=str(data.get("cursor_key") or "").strip() or None,
            adapter=adapter,
            advance_internal=not defer_internal_advance,
        )
        if internal_state == "frozen":
            continue
        if internal_state == "advanced":
            if defer_internal_advance:
                write_cursor(
                    session_id,
                    int(data.get("line_offset", 0) or 0),
                    str(transcript_path),
                    internal=False,
                    source_key=str(data.get("cursor_key") or "").strip() or None,
                )
                logger.info(
                    "session %s appears internal maintenance-only during rolling scan; "
                    "leaving cursor offset unchanged for a later signal",
                    session_id,
                )
            else:
                logger.info(
                    "session %s is internal maintenance-only during rolling scan, advancing cursor to EOF",
                    session_id,
                )
            continue
        if internal_state == "ignored":
            logger.info(
                "session %s has only ignored user content during rolling scan; "
                "advancing non-internal cursor to EOF",
                session_id,
            )
            continue
        if internal_state == "unfrozen":
            logger.info(
                "session %s gained non-internal content past a frozen internal cursor during rolling scan",
                session_id,
            )
            data = _read_cursor_with_source_compat(
                str(session_id),
                str(data.get("cursor_key") or "").strip() or None,
            )
            transcript_path = str(data.get("transcript_path") or transcript_path)
        unfroze_internal_cursor = internal_state == "unfrozen"
        source_key = _signal_source_cursor_key(str(session_id), str(transcript_path), cursor_data=data)
        if _source_signal_already_pending(
            pending_source_keys=pending_rolling_source_keys,
            source_key=source_key,
            session_id=session_id,
            scanner="rolling",
        ):
            continue
        if _processing_lock_active(source_key):
            logger.info(
                "session %s has active extraction lock %s; skipping rolling scan until extraction completes",
                session_id,
                source_key,
            )
            continue

        cursor_offset = int(data.get("line_offset", 0) or 0)
        cursor_rebased_for_byte_growth = False
        cursor_rebase_buffer_reset = False
        total_lines = count_transcript_lines(buffer_transcript_path)
        if (
            transcript_grew_since_cursor
            and total_lines > 0
            and cursor_offset >= total_lines
        ):
            logger.info(
                "session %s cursor reached EOF before transcript grew in place "
                "(offset=%d lines=%d cursor_size=%d current_size=%d); "
                "rescanning from start for rolling readiness",
                session_id,
                cursor_offset,
                total_lines,
                cursor_size_bytes,
                current_size_bytes,
            )
            cursor_offset = 0
            cursor_rebased_for_byte_growth = True
            data = dict(data)
            data["line_offset"] = 0
        state = read_rolling_state(session_id)
        state_buffer_path = str(state.get("buffer_transcript_path") or "").strip()
        ended_preserved_buffer_path = ""
        for candidate_path in (str(buffer_transcript_path), str(transcript_path), state_buffer_path):
            if candidate_path and _is_daemon_preserved_session_transcript_path(candidate_path):
                ended_preserved_buffer_path = candidate_path
                break
        cursor_points_at_ended_source = (
            not os.path.isfile(str(transcript_path))
            or _is_daemon_preserved_session_transcript_path(str(transcript_path))
            or _is_daemon_rolling_transcript_snapshot_path(str(transcript_path))
        )
        preserved_buffer_larger_than_cursor_source = False
        if (
            ended_preserved_buffer_path
            and os.path.isfile(str(transcript_path))
            and not _is_daemon_preserved_session_transcript_path(str(transcript_path))
            and not _is_daemon_rolling_transcript_snapshot_path(str(transcript_path))
            and not defer_internal_advance
        ):
            preserved_buffer_larger_than_cursor_source = (
                _transcript_size_bytes(ended_preserved_buffer_path)
                > _transcript_size_bytes(str(transcript_path))
            )
        state_semantic_tokens = int(state.get("semantic_buffer_tokens", 0) or 0)
        if (
            ended_preserved_buffer_path
            and (cursor_points_at_ended_source or preserved_buffer_larger_than_cursor_source)
            and (
                state_semantic_tokens >= _rolling_ready_threshold(chunk_budget)
                or preserved_buffer_larger_than_cursor_source
            )
            and (
                _adapter_live_transcript_missing(str(session_id), adapter=adapter)
                or preserved_buffer_larger_than_cursor_source
            )
        ):
            if str(session_id) in pending_session_end_session_ids:
                continue
            preserved_total_lines = count_transcript_lines(ended_preserved_buffer_path)
            cursor_key_for_signal = str(data.get("cursor_key") or cursor_file.stem or session_id).strip()
            cursor_offset_for_flush = min(int(data.get("line_offset", 0) or 0), preserved_total_lines)
            if cursor_offset_for_flush >= preserved_total_lines and not _rolling_state_has_pending_content(state):
                logger.info(
                    "session %s rolling scan found ended preserved buffer %s already consumed at EOF; "
                    "preserving cursor without queueing lifecycle flush",
                    session_id,
                    ended_preserved_buffer_path,
                )
                write_cursor(
                    str(session_id),
                    preserved_total_lines,
                    ended_preserved_buffer_path,
                    internal=bool(data.get("internal", False)),
                    source_key=cursor_key_for_signal,
                    processed_signal_type="session_end",
                )
                if _cursor_storage_key(str(session_id), cursor_key_for_signal) != str(session_id):
                    write_cursor(
                        str(session_id),
                        preserved_total_lines,
                        ended_preserved_buffer_path,
                        internal=bool(data.get("internal", False)),
                        processed_signal_type="session_end",
                    )
                continue
            logger.info(
                "session %s rolling scan found ended preserved buffer %s with no active growth; "
                "queueing lifecycle flush from preserved cursor offset %d before threshold check",
                session_id,
                ended_preserved_buffer_path,
                cursor_offset_for_flush,
            )
            write_cursor(
                str(session_id),
                cursor_offset_for_flush,
                ended_preserved_buffer_path,
                internal=bool(data.get("internal", False)),
                source_key=cursor_key_for_signal,
            )
            write_signal(
                signal_type="session_end",
                session_id=str(session_id),
                transcript_path=ended_preserved_buffer_path,
                meta={
                    "reason": "ended_rolling_buffer_flush",
                    "source_cursor_key": cursor_key_for_signal,
                },
            )
            pending_session_end_session_ids.add(str(session_id))
            continue
        buffer_source_differs_from_cursor = str(buffer_transcript_path) != str(transcript_path)
        if state_buffer_path and state_buffer_path != str(buffer_transcript_path):
            if staged_state_has_payload(state):
                # A staged payload is already waiting to flush from the prior
                # source. Do not clear its source-relative offsets here; once
                # that payload is published and cleared, the next scan resets
                # the buffer state for the new source.
                logger.info(
                    "session %s rolling buffer source changed from %s to %s with staged payload present; "
                    "preserving staged state",
                    session_id,
                    state_buffer_path,
                    buffer_transcript_path,
                )
            else:
                logger.info(
                    "session %s rolling buffer source changed from %s to %s; resetting source-relative offset",
                    session_id,
                    state_buffer_path,
                    buffer_transcript_path,
                )
                state = _reset_semantic_buffer_for_source(state, str(buffer_transcript_path))
                cursor_offset = 0
                data = dict(data)
                data["line_offset"] = 0
        elif not state_buffer_path:
            if buffer_source_differs_from_cursor and not staged_state_has_payload(state):
                logger.info(
                    "session %s rolling buffer source %s differs from cursor identity %s with no prior "
                    "buffer source; resetting source-relative offset",
                    session_id,
                    buffer_transcript_path,
                    transcript_path,
                )
                state = _reset_semantic_buffer_for_source(state, str(buffer_transcript_path))
                cursor_offset = 0
                data = dict(data)
                data["line_offset"] = 0
            else:
                state = dict(state)
                state["buffer_transcript_path"] = str(buffer_transcript_path)
        state_buffered_line_offset = int(state.get("buffered_line_offset", cursor_offset) or 0)
        if (
            cursor_rebased_for_byte_growth
            and not staged_state_has_payload(state)
            and total_lines > 0
            and state_buffered_line_offset >= total_lines
        ):
            # Same-line transcript rewrites leave the cursor at EOF while the
            # content bytes changed. If the rolling buffer is also at EOF for
            # this source, reset it so the rewritten lines are parsed once, then
            # refresh the size baseline below if the buffer remains
            # sub-threshold. Partial buffers must keep advancing from their
            # persisted offset.
            state = _reset_semantic_buffer_for_source(state, str(buffer_transcript_path))
            cursor_rebase_buffer_reset = True
            cursor_offset = 0
            data = dict(data)
            data["line_offset"] = 0
        buffered_line_offset = max(
            int(state.get("buffered_line_offset", cursor_offset) or 0),
            cursor_offset,
        )
        if total_lines > buffered_line_offset:
            state, _buffer_metrics = _buffer_transcript_tail(
                buffer_transcript_path,
                buffered_line_offset,
                state,
                adapter=adapter,
                max_tokens=chunk_budget,
                max_lines=chunk_line_budget,
                include_threshold_crossing_semantic_row=True,
                session_id=str(session_id),
                label="daemon-rolling-scan",
            )
            if int(_buffer_metrics.get("parse_failed", 0) or 0):
                state["buffer_transcript_path"] = str(buffer_transcript_path)
                write_rolling_state(session_id, state)
                continue
            if unfroze_internal_cursor and _semantic_buffer_has_content(state):
                state[_INTERNAL_CURSOR_UNFROZEN_PENDING_FLUSH_KEY] = True
            state["buffer_transcript_path"] = str(buffer_transcript_path)
            write_rolling_state(session_id, state)
            buffered_line_offset = int(state.get("buffered_line_offset", buffered_line_offset) or buffered_line_offset)
        scan_cursor_reached_eof_after_growth = (
            transcript_grew_since_cursor
            and total_lines > 0
            and buffered_line_offset >= total_lines
            and current_size_bytes > cursor_size_bytes
            and not bool(data.get("internal", False))
            and not unfroze_internal_cursor
        )

        semantic_tokens = int(state.get("semantic_buffer_tokens", 0) or 0)
        near_budget_threshold = _rolling_ready_threshold(chunk_budget)
        # Do not trigger a fresh rolling pass merely because a sub-threshold
        # residual buffer now has unread tail behind it. That causes post-flush
        # bleed where the next partial chunk gets staged before it independently
        # crosses the rolling threshold. Oversized transcripts are already
        # drained by the in-band continued_chunk_budget signal written from the
        # rolling stage itself; new residual buffers must cross the normal
        # threshold on their own before staging again.
        should_signal = (
            semantic_tokens >= chunk_budget
            or semantic_tokens >= near_budget_threshold
        )
        if scan_cursor_reached_eof_after_growth and should_signal and not using_stale_source_cursor_for_grown_transcript:
            cursor_key_for_write = str(data.get("cursor_key") or cursor_file.stem or "").strip() or None
            write_cursor(
                session_id,
                buffered_line_offset,
                str(transcript_path),
                internal=bool(data.get("internal", False)),
                source_key=cursor_key_for_write,
                last_flushed_line_offset=int(data.get("last_flushed_line_offset", 0) or 0),
            )
        elif scan_cursor_reached_eof_after_growth:
            logger.info(
                "session %s rolling scan buffered source cursor tail to EOF below threshold; "
                "preserving cursor offset %s until rolling extraction or lifecycle drain succeeds",
                session_id,
                data.get("line_offset", 0),
            )
        if not should_signal:
            if cursor_rebase_buffer_reset and total_lines > 0 and buffered_line_offset >= total_lines:
                cursor_key_for_write = str(data.get("cursor_key") or cursor_file.stem or "").strip() or None
                write_cursor(
                    session_id,
                    total_lines,
                    str(transcript_path),
                    internal=bool(data.get("internal", False)),
                    source_key=cursor_key_for_write,
                    last_flushed_line_offset=int(data.get("last_flushed_line_offset", 0) or 0),
                )
            pending_unfrozen_flush = bool(state.get(_INTERNAL_CURSOR_UNFROZEN_PENDING_FLUSH_KEY))
            if semantic_tokens > 0 and (unfroze_internal_cursor or pending_unfrozen_flush):
                if recently_modified:
                    logger.info(
                        "session %s recovered %d semantic tokens past a frozen internal cursor; "
                        "deferring recovery flush while transcript remains active",
                        session_id,
                        semantic_tokens,
                    )
                    continue
                logger.info(
                    "session %s recovered %d semantic tokens past a frozen internal cursor; "
                    "generating session_end flush",
                    session_id,
                    semantic_tokens,
                )
                write_signal(
                    signal_type="session_end",
                    session_id=session_id,
                    transcript_path=transcript_path,
                    meta={
                        "reason": "internal_cursor_unfrozen_flush",
                        "source_cursor_key": source_key,
                        "semantic_buffer_tokens": semantic_tokens,
                        "buffered_line_offset": buffered_line_offset,
                    },
                )
                _remember_queued_source_signal(
                    pending_session_ids=pending_rolling_session_ids,
                    pending_source_keys=pending_rolling_source_keys,
                    session_id=session_id,
                    source_key=source_key,
                )
            continue

        if semantic_tokens >= chunk_budget:
            logger.info(
                "session %s crossed rolling extract budget (%d >= %d semantic tokens), generating rolling signal",
                session_id,
                semantic_tokens,
                chunk_budget,
            )
            reason = "semantic_chunk_budget"
        else:
            logger.info(
                "session %s is near rolling extract budget (%d >= %d semantic tokens, budget=%d), generating rolling signal",
                session_id,
                semantic_tokens,
                near_budget_threshold,
                chunk_budget,
            )
            reason = "semantic_chunk_budget_near"
        signal_meta = {
            "reason": reason,
            "chunk_tokens": chunk_budget,
            "semantic_buffer_tokens": semantic_tokens,
            "buffered_line_offset": buffered_line_offset,
        }
        if str(buffer_transcript_path) != str(transcript_path):
            signal_meta["buffer_transcript_path"] = str(buffer_transcript_path)
        if reason == "semantic_chunk_budget_near":
            signal_meta["near_budget_threshold"] = near_budget_threshold
        write_signal(
            signal_type="rolling",
            session_id=session_id,
            transcript_path=transcript_path,
            meta=signal_meta,
        )
        _remember_queued_source_signal(
            pending_session_ids=pending_rolling_session_ids,
            pending_source_keys=pending_rolling_source_keys,
            session_id=session_id,
            source_key=source_key,
        )


def _retry_missing_embeddings() -> int:
    """Retry embeddings for nodes stored without one (e.g. when Ollama was down).

    Called periodically from the daemon loop. Returns count of nodes updated.
    """
    try:
        from datastore.memorydb.memory_graph import MemoryGraph
        graph = MemoryGraph()
        count = graph.retry_missing_embeddings(limit=20)
        if count:
            logger.info("[daemon] embed-retry: backfilled %d missing embedding(s)", count)
        return count
    except Exception as e:
        if _fail_hard_enabled():
            raise RuntimeError("Missing embedding retry failed while failHard is enabled") from e
        logger.debug("[daemon] embed-retry failed: %s", e)
        return 0


# ---------------------------------------------------------------------------
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
    _prime_config_reload_watcher()

    shutdown_requested = False

    def handle_sigterm(signum, frame):
        nonlocal shutdown_requested
        logger.info("SIGTERM received, processing remaining signals before exit...")
        shutdown_requested = True

    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)

    last_idle_check = 0.0
    last_embed_retry_check = 0.0
    _EMBED_RETRY_INTERVAL = 30.0  # keep fresh extracted facts vector-searchable during active sessions

    # Initialize version watcher. Janitor scheduling is supervisor-owned.
    from core.compatibility import VersionWatcher, read_circuit_breaker
    home = _instance_root()
    data_dir = home / "data"
    quaid_version = _get_quaid_version()
    version_watcher = VersionWatcher(data_dir=data_dir, quaid_version=quaid_version)

    try:
        while not shutdown_requested:
            _reload_config_if_changed("daemon poll")
            if not _supervisor_alive():
                logger.info("supervisor exited; extraction daemon exiting")
                break

            # Version watcher tick — cheap mtime check on every iteration
            try:
                version_watcher.tick()
            except Exception as e:
                logger.warning("version watcher tick failed: %s", e)
                if _fail_hard_enabled():
                    raise RuntimeError("version watcher tick failed while failHard is enabled") from e

            # Check circuit breaker before processing signals
            breaker = read_circuit_breaker(data_dir)
            if not breaker.allows_writes():
                # In degraded/safe mode — skip extraction, just idle
                if breaker.message:
                    logger.debug("Circuit breaker %s: %s", breaker.status, breaker.message)
                time.sleep(poll_interval)
                continue

            # Run before session scans so active-session cursor churn cannot
            # starve facts that were stored while the embedding provider was busy.
            now = time.time()
            if now - last_embed_retry_check > _EMBED_RETRY_INTERVAL:
                try:
                    _retry_missing_embeddings()
                except Exception as e:
                    if _fail_hard_enabled():
                        raise RuntimeError("embed retry failed while failHard is enabled") from e
                    logger.debug("embed retry failed: %s", e)
                last_embed_retry_check = now

            try:
                check_chunk_ready_sessions()
            except Exception as e:
                if _fail_hard_enabled():
                    raise RuntimeError("rolling chunk readiness check failed while failHard is enabled") from e
                logger.error("rolling chunk readiness check failed: %s", e)

            # Process pending signals
            signals = read_pending_signals()
            for sig in signals:
                try:
                    process_signal(sig)
                except _ProviderConfigError as pce:
                    if _fail_hard_enabled():
                        raise
                    logger.error(
                        "Provider configuration error during signal processing - will retry after config repair "
                        "(signal preserved; notice queued): %s",
                        pce,
                    )
                except _ProviderUnavailableError as pue:
                    if _fail_hard_enabled():
                        raise
                    # Provider is confirmed down (retryable HTTP codes exhausted).
                    # Default: log clearly and let the natural retry loop handle it —
                    # the signal stays preserved, daemon retries next poll cycle.
                    # Optional: daemon.shutdown_on_provider_outage=true kills the
                    # daemon so ensure_alive cold-starts it on the next hook call.
                    _shutdown_on_outage = False
                    try:
                        from config import get_config as _gc
                        _shutdown_on_outage = bool(getattr(_gc().daemon, "shutdown_on_provider_outage", False))
                    except Exception:
                        pass
                    if _shutdown_on_outage:
                        logger.critical(
                            "Provider unavailable — daemon shutting down for auto-restart "
                            "(daemon.shutdown_on_provider_outage=true; signals preserved on disk)"
                        )
                        raise
                    logger.error(
                        "Provider unavailable — will retry on next poll cycle "
                        "(signal preserved): %s", pue,
                    )
                except Exception as e:
                    if _fail_hard_enabled():
                        raise
                    logger.error("failed processing signal: %s", e, exc_info=True)
                    _record_signal_process_failure_for_retry(
                        sig,
                        e,
                        label="daemon-loop",
                    )

            # Periodic idle session check. Use a timeout-aware cadence so
            # shorter configured inactivity windows do not wait on a fixed
            # five-minute sweep interval before becoming eligible.
            now = time.time()
            configured_timeout_minutes = _get_idle_timeout_minutes()
            effective_timeout_minutes = _effective_idle_timeout_minutes(configured_timeout_minutes)
            timeout_seconds = effective_timeout_minutes * 60
            effective_idle_check_interval = max(
                poll_interval,
                min(idle_check_interval, max(5.0, timeout_seconds / 2.0)),
            )

            if now - last_idle_check > effective_idle_check_interval:
                try:
                    check_idle_sessions(effective_timeout_minutes)
                except Exception as e:
                    if _fail_hard_enabled():
                        raise RuntimeError("idle check failed while failHard is enabled") from e
                    logger.error("idle check failed: %s", e)
                last_idle_check = now

            time.sleep(poll_interval)

        # On shutdown: process any remaining signals
        logger.info("shutdown: processing remaining signals...")
        signals = read_pending_signals()
        for sig in signals:
            try:
                process_signal(sig)
            except Exception as e:
                if _fail_hard_enabled():
                    raise
                logger.error("shutdown signal processing failed: %s", e)
                _record_signal_process_failure_for_retry(
                    sig,
                    e,
                    label="daemon-shutdown",
                )

    finally:
        _remove_pid_if_matches(os.getpid())
        logger.info("extraction daemon exited")


def flush_pending_signals(
    *,
    timeout_seconds: float = 120.0,
    poll_interval: float = 0.25,
    max_passes: int = 0,
) -> Dict[str, Any]:
    """Synchronously drain currently queued extraction signals.

    This uses the same process_signal path as the background daemon. If another
    daemon process already owns a session lock, the signal remains on disk and
    this helper waits for a later pass instead of corrupting shared state.
    """
    previous_daemon_env = os.environ.get("QUAID_DAEMON")
    os.environ["QUAID_DAEMON"] = "1"
    try:
        started = time.time()
        deadline = started + max(0.0, float(timeout_seconds))
        summary: Dict[str, Any] = {
            "status": "running",
            "attempted": 0,
            "processed": 0,
            "preserved": 0,
            "errors": 0,
            "passes": 0,
            "idle_checks": 0,
            "remaining_signals": 0,
            "elapsed_seconds": 0.0,
            "instance": _instance_id(),
            "instance_root": str(_instance_root()),
        }
        idle_checked = False

        def _run_idle_check_once() -> int:
            nonlocal idle_checked
            idle_checked = True
            summary["idle_checks"] = int(summary["idle_checks"]) + 1
            try:
                check_idle_sessions(_effective_idle_timeout_minutes(_get_idle_timeout_minutes()))
            except Exception as exc:
                if _fail_hard_enabled():
                    raise RuntimeError("idle check failed during daemon flush while failHard is enabled") from exc
                summary["errors"] = int(summary["errors"]) + 1
                logger.error("flush idle check failed: %s", exc, exc_info=True)
            if _pending_signal_count() == 0:
                pending = read_pending_signals()
                pending_session_ids = {s.get("session_id") for s in pending}
                known_cursor_session_ids: set[str] = set()
                try:
                    for cursor_file in _cursor_dir().glob("*.json"):
                        try:
                            cursor_data = json.loads(cursor_file.read_text(encoding="utf-8"))
                        except (json.JSONDecodeError, OSError):
                            continue
                        if isinstance(cursor_data, dict):
                            cursor_session_id = str(cursor_data.get("session_id") or "").strip()
                            if cursor_session_id:
                                known_cursor_session_ids.add(cursor_session_id)
                except OSError as exc:
                    if _fail_hard_enabled():
                        raise RuntimeError(
                            "cursor scan failed during daemon flush while failHard is enabled"
                        ) from exc
                    logger.warning("flush cursor scan failed before rolling recovery: %s", exc)
                _queue_idle_semantic_rolling_flushes_without_cursor(
                    pending_session_ids=pending_session_ids,
                    pending_source_keys=_pending_signal_source_keys(pending),
                    known_cursor_session_ids=known_cursor_session_ids,
                    now=time.time(),
                    timeout_seconds=0.0,
                    installed_at_ts=0.0,
                )
            remaining_after_idle = _pending_signal_count()
            summary["remaining_signals"] = remaining_after_idle
            return remaining_after_idle

        while True:
            signals = read_pending_signals()
            remaining = _pending_signal_count()
            if not signals:
                summary["remaining_signals"] = remaining
                if remaining == 0:
                    if not idle_checked:
                        remaining = _run_idle_check_once()
                        if remaining > 0:
                            continue
                    summary["status"] = "drained"
                    break
                if time.time() >= deadline:
                    summary["status"] = "timeout"
                    break
                time.sleep(max(0.0, float(poll_interval)))
                continue

            summary["passes"] = int(summary["passes"]) + 1
            for sig in signals:
                summary["attempted"] = int(summary["attempted"]) + 1
                sig_path_raw = str(sig.get("_signal_path") or "").strip()
                sig_path = Path(sig_path_raw) if sig_path_raw else None
                try:
                    process_signal(sig)
                except _ProviderConfigError as exc:
                    if _fail_hard_enabled():
                        raise
                    summary["errors"] = int(summary["errors"]) + 1
                    logger.error("flush signal provider config error; signal preserved: %s", exc)
                except _ProviderUnavailableError as exc:
                    if _fail_hard_enabled():
                        raise
                    summary["errors"] = int(summary["errors"]) + 1
                    logger.error("flush signal provider unavailable; signal preserved: %s", exc)
                except Exception as exc:
                    if _fail_hard_enabled():
                        raise
                    summary["errors"] = int(summary["errors"]) + 1
                    logger.error("flush signal processing failed; signal preserved: %s", exc, exc_info=True)
                if sig_path is not None and sig_path.exists():
                    summary["preserved"] = int(summary["preserved"]) + 1
                else:
                    summary["processed"] = int(summary["processed"]) + 1

            remaining = _pending_signal_count()
            summary["remaining_signals"] = remaining
            if remaining == 0:
                if not idle_checked:
                    remaining = _run_idle_check_once()
                    if remaining > 0:
                        continue
                summary["status"] = "drained"
                break
            if int(max_passes or 0) > 0 and int(summary["passes"]) >= int(max_passes):
                summary["status"] = "max_passes"
                break
            if time.time() >= deadline:
                summary["status"] = "timeout"
                break
            time.sleep(max(0.0, float(poll_interval)))

        summary["elapsed_seconds"] = round(time.time() - started, 3)
        return summary
    finally:
        if previous_daemon_env is None:
            os.environ.pop("QUAID_DAEMON", None)
        else:
            os.environ["QUAID_DAEMON"] = previous_daemon_env


# ---------------------------------------------------------------------------
# Daemon lifecycle commands
# ---------------------------------------------------------------------------

def _run_worker_loop() -> None:
    exit_code = 0
    try:
        daemon_loop()
    except Exception as exc:
        exit_code = 1
        logger.error("daemon crashed: %s", exc, exc_info=True)
    finally:
        _remove_pid_if_matches(os.getpid())
        os._exit(exit_code)


def ensure_alive() -> int:
    """Ensure the daemon is running. Start it if not. Returns PID."""
    project_docs_mod = None
    recovered_from_supervisor_failure = False
    if os.environ.get("QUAID_SUPERVISOR_DISABLE", "").strip() != "1":
        explicit_instance = str(os.environ.get("QUAID_INSTANCE", "") or "").strip()
        if explicit_instance:
            try:
                from lib.adapter import get_adapter
                get_adapter()
            except Exception as exc:
                logger.warning(
                    "instance bootstrap before supervisor handoff failed for %s: %s",
                    explicit_instance,
                    exc,
                )
                try:
                    from lib.fail_policy import is_fail_hard_enabled
                except Exception:
                    fail_hard = False
                else:
                    fail_hard = bool(is_fail_hard_enabled())
                if fail_hard:
                    raise
        monitor_disabled_error = False
        try:
            from core import project_docs as project_docs_mod
            instance_id = _instance_id()
            disabled_marker = project_docs_mod.read_instance_monitor_disabled(instance_id)
            if disabled_marker:
                reason = str(disabled_marker.get("reason") or "").strip()
                if reason != "daemon_stop":
                    pid = read_pid()
                    if pid is not None:
                        return pid
                    monitor_disabled_error = True
                    raise RuntimeError(
                        f"instance monitor for {instance_id} is disabled ({reason or 'unknown reason'}); "
                        "not starting extraction daemon directly"
                    )
            project_docs_mod.enable_instance_monitor(instance_id)
            project_docs_mod.ensure_supervisor_alive()
        except Exception as exc:
            if monitor_disabled_error:
                raise
            logger.warning("project docs supervisor ensure_alive failed: %s", exc)
            supervisor_failure_error = getattr(
                project_docs_mod,
                "ProjectDocsSupervisorFailureError",
                None,
            )
            if supervisor_failure_error is not None and isinstance(exc, supervisor_failure_error):
                logger.error(
                    "project docs supervisor is in failed state; starting extraction daemon directly"
                )
                recovered_from_supervisor_failure = True
            else:
                try:
                    from lib.fail_policy import is_fail_hard_enabled
                except Exception:
                    fail_hard = False
                else:
                    fail_hard = bool(is_fail_hard_enabled())
                if fail_hard:
                    raise
        else:
            pid = read_pid()
            if pid is not None:
                return pid
            try:
                wait_default = project_docs_mod.pid_startup_wait_seconds()
                wait_seconds = float(os.environ.get("QUAID_INSTANCE_MONITOR_WAIT_SECONDS", str(wait_default)) or wait_default)
            except ValueError:
                wait_seconds = project_docs_mod.pid_startup_wait_seconds()
            deadline = time.time() + max(0.5, wait_seconds)
            while time.time() < deadline:
                time.sleep(0.1)
                pid = read_pid()
                if pid is not None:
                    return pid
            msg = "supervisor did not start an instance monitor before timeout"
            logger.warning(msg)
            fallback_pid = start_daemon()
            if fallback_pid and fallback_pid > 0:
                logger.warning(
                    "started extraction daemon directly after supervisor handoff timeout (pid=%s)",
                    fallback_pid,
                )
                return fallback_pid
            try:
                from lib.fail_policy import is_fail_hard_enabled
            except Exception:
                fail_hard = False
            else:
                fail_hard = bool(is_fail_hard_enabled())
            if fail_hard:
                raise RuntimeError(msg)
            return fallback_pid
    pid = read_pid()
    if pid is not None:
        return pid
    fallback_pid = start_daemon()
    if recovered_from_supervisor_failure and fallback_pid and fallback_pid > 0:
        try:
            project_docs_mod.clear_supervisor_failure()
        except Exception as exc:
            logger.warning("failed clearing recovered project-docs supervisor failure marker: %s", exc)
    return fallback_pid


def _terminate_daemon_pid(pid: int, *, grace_seconds: float = 10.0) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        deadline = time.time() + max(0.1, grace_seconds)
        while time.time() < deadline:
            if not _pid_alive(pid):
                return True
            time.sleep(0.5)
        if _pid_alive(pid):
            os.kill(pid, signal.SIGKILL)
        return not _pid_alive(pid)
    except OSError:
        return True


def start_daemon() -> int:
    """Start the daemon as a background process. Returns child PID.

    Uses a stable sidecar flock so atomic PID-file replacement does not orphan
    the startup lock.
    """
    # Capture before scrub_background_process_env clears QUAID_INSTANCE; the
    # worker process still needs the validated instance stamped back in.
    instance = _instance_id()
    if not instance:
        raise RuntimeError("cannot start extraction daemon without QUAID_INSTANCE")
    pid_file = _pid_path()
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file = _pid_lock_path()

    try:
        lock_fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as e:
        logger.error("cannot open daemon startup lock file: %s", e)
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
            matching = _matching_daemon_pids(include_foreground=True)
            extras = [pid for pid in matching if pid != existing]
            if extras:
                logger.warning(
                    "reaping %d extra extraction daemon(s) while keeping pidfile target %s for home=%s instance=%s: %s",
                    len(extras),
                    existing,
                    _quaid_home(),
                    _instance_id(),
                    ",".join(str(pid) for pid in extras),
                )
                for pid in extras:
                    _terminate_daemon_pid(pid)
            return existing
        matching_workers = _matching_daemon_pids()
        matching_all = _matching_daemon_pids(include_foreground=True)
        if len(matching_workers) == 1 and matching_all == matching_workers:
            write_pid(matching_workers[0])
            return matching_workers[0]
        if matching_all:
            logger.warning(
                "reaping %d matching extraction daemon(s) before spawn for home=%s instance=%s: %s",
                len(matching_all),
                _quaid_home(),
                _instance_id(),
                ",".join(str(pid) for pid in matching_all),
            )
            for pid in matching_all:
                _terminate_daemon_pid(pid)
            remove_pid()

        # Spawn daemon worker via subprocess.Popen instead of double-fork.
        # double-fork inherits the calling process's Python state (sys.modules,
        # open file descriptors) which causes silent failures when called from
        # within a Claude Code hook or OC gateway process.  Popen spawns a
        # fresh interpreter with a clean environment.
        log_file = _log_path()
        log_file.parent.mkdir(parents=True, exist_ok=True)

        # Strip host-adapter env vars that must not be inherited by the daemon.
        _skip_prefixes = ("OPENCLAW_",)
        _skip_keys = {"CLAUDE_CODE_OAUTH_TOKEN", "MEMORY_DB_PATH", "MEMORY_ARCHIVE_DB_PATH"}
        env = {
            k: v for k, v in os.environ.items()
            if not any(k.startswith(p) for p in _skip_prefixes)
            and k not in _skip_keys
        }
        from core import project_docs
        env = project_docs.scrub_background_process_env(env)
        env["QUAID_HOME"] = str(_quaid_home())
        env["QUAID_INSTANCE"] = instance
        env["QUAID_DAEMON"] = "1"
        for key in list(env):
            if key.startswith("QUAID_SUPERVISOR_"):
                env.pop(key, None)

        with open(log_file, "a") as _lf:
            subprocess.Popen(
                [sys.executable, str(Path(__file__)), "_worker"],
                start_new_session=True,
                stdout=_lf,
                stderr=_lf,
                stdin=subprocess.DEVNULL,
                env=env,
                cwd=str(_quaid_home()),
            )

        # Wait for worker to write its PID file (same budget as double-fork).
        for _ in range(20):
            time.sleep(0.1)
            running_pid = read_pid()
            if running_pid is not None:
                return running_pid

        logger.error("daemon _worker did not write PID file within 2s")
        return -1
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
    if os.environ.get("QUAID_SUPERVISOR_DISABLE", "").strip() != "1":
        try:
            from core import project_docs
            project_docs.disable_instance_monitor(_instance_id(), reason="daemon_stop")
        except Exception:
            logger.exception("failed disabling supervisor instance monitor before daemon stop")
            try:
                from lib.fail_policy import is_fail_hard_enabled
            except Exception:
                fail_hard = False
            else:
                fail_hard = bool(is_fail_hard_enabled())
            if fail_hard:
                raise
    targets: list[int] = []
    pid = read_pid()
    if pid is not None:
        targets.append(pid)
    for match_pid in _matching_daemon_pids(include_foreground=True):
        if match_pid not in targets:
            targets.append(match_pid)
    if not targets:
        return False
    stopped = False
    for target_pid in targets:
        target_stopped = _terminate_daemon_pid(target_pid)
        stopped = target_stopped or stopped
        if target_stopped:
            _remove_pid_if_matches(target_pid)
    return stopped


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
    flush_parser = subparsers.add_parser("flush", help="Process queued extraction signals now")
    flush_parser.add_argument("--timeout", type=float, default=120.0, help="Seconds to wait for queue drain (default: 120)")
    flush_parser.add_argument("--poll-interval", type=float, default=0.25, help="Seconds between retry passes (default: 0.25)")
    flush_parser.add_argument("--max-passes", type=int, default=0, help="Maximum read/process passes; 0 means unlimited until timeout")
    flush_parser.add_argument("--json", action="store_true", help="Print JSON summary")
    # Internal: spawned by start_daemon() via subprocess.Popen.  Not for direct use.
    subparsers.add_parser("_worker", help=argparse.SUPPRESS)

    args = parser.parse_args()

    if args.command == "start":
        pid = ensure_alive()
        print(f"daemon started (pid={pid})")
    elif args.command == "stop":
        stopped = stop_daemon()
        print("daemon stopped" if stopped else "daemon was not running")
    elif args.command == "status":
        status = daemon_status()
        print(json.dumps(status, indent=2))
    elif args.command == "flush":
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        )
        summary = flush_pending_signals(
            timeout_seconds=args.timeout,
            poll_interval=args.poll_interval,
            max_passes=args.max_passes,
        )
        if args.json:
            print(json.dumps(summary, indent=2))
        else:
            print(
                "daemon flush: "
                f"status={summary['status']} "
                f"attempted={summary['attempted']} "
                f"processed={summary['processed']} "
                f"preserved={summary['preserved']} "
                f"errors={summary['errors']} "
                f"remaining={summary['remaining_signals']} "
                f"elapsed={summary['elapsed_seconds']}s"
            )
        if summary.get("status") != "drained" or int(summary.get("errors", 0) or 0) > 0:
            sys.exit(2)
    elif args.command == "run":
        # Foreground mode for debugging
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        )
        daemon_loop()
    elif args.command == "_worker":
        # Internal entrypoint called by start_daemon() via subprocess.Popen.
        # Sets up file logging and runs the daemon loop directly; the caller
        # handles process isolation via start_new_session=True.
        _log = _log_path()
        _log.parent.mkdir(parents=True, exist_ok=True)
        _handler = logging.handlers.RotatingFileHandler(
            str(_log), maxBytes=10 * 1024 * 1024, backupCount=3,
        )
        _handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s %(message)s"
        ))
        _root = logging.getLogger()
        _root.handlers.clear()
        _root.addHandler(_handler)
        _root.setLevel(logging.INFO)
        _run_worker_loop()
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
