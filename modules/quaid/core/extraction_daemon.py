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
import uuid
from io import StringIO
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# Ensure plugin root is importable (B060)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.llm_clients import ProviderUnavailableError as _ProviderUnavailableError

logger = logging.getLogger("quaid.daemon")

# Valid signal types (B062)
VALID_SIGNAL_TYPES = ("compaction", "reset", "session_end", "timeout", "rolling")
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
    "rolling": 2,
    "compaction": 3,
    "timeout": 4,
}

# Session ID validation (B008)
_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
_SESSION_ID_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_DISCOVERY_ARTIFACT_MARKERS = (".checkpoint.", ".reset.")

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


def _config_file_paths() -> List[Path]:
    """Return config files that affect this instance, highest priority first."""
    instance = _instance_id()
    home = _quaid_home()
    platform = ""
    try:
        from lib.adapter import get_adapter

        adapter = get_adapter()
        platform = str(adapter.get_capability("platform_config_scope", "") or "").strip()
    except Exception:
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
    reload_config()


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


def _extraction_buffer_log_path() -> Path:
    d = _instance_root() / "logs" / "daemon"
    d.mkdir(parents=True, exist_ok=True)
    return d / "extraction-buffer.log"


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
    except Exception:
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
    try:
        path = _extraction_buffer_log_path()
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
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
    except Exception:
        # If we can't verify, assume it's valid to avoid false negatives
        return True


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _all_process_commands_with_env() -> list[tuple[int, str]]:
    """Return (pid, command-with-env) rows for process scanning."""
    try:
        result = subprocess.run(
            ["ps", "eww", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
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
        return rows
    except Exception:
        return []


def _matching_daemon_pids(
    *,
    quaid_home: Path | str | None = None,
    instance: str | None = None,
) -> list[int]:
    """Return live extraction-daemon worker PIDs for this home+instance."""
    home = str(quaid_home or _quaid_home()).strip()
    instance_id = str(instance or _instance_id()).strip()
    if not home or not instance_id:
        return []
    matches: list[int] = []
    for pid, command in _all_process_commands_with_env():
        if pid <= 0 or pid == os.getpid():
            continue
        if "extraction_daemon.py" not in command or "_worker" not in command:
            continue
        if f"QUAID_HOME={home}" not in command:
            continue
        if f"QUAID_INSTANCE={instance_id}" not in command:
            continue
        if _pid_alive(pid):
            matches.append(pid)
    return sorted(set(matches))


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
    try:
        pid = int(pid_file.read_text().strip())
        # Check if process is alive
        os.kill(pid, 0)
        # Verify it's actually our daemon (PID reuse guard)
        if not _is_daemon_process(pid):
            logger.warning("PID %d in pid file is alive but is not the extraction daemon (PID reused) — treating as stale", pid)
            raise OSError("PID reused by unrelated process")
        return pid
    except (ValueError, OSError):
        # PID file exists but process is dead or stale
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

    sig_dir = _signal_dir()
    existing_path = None
    existing_payload: Optional[Dict[str, Any]] = None
    for f in sorted(sig_dir.iterdir()):
        if not f.name.endswith(".json"):
            continue
        try:
            existing = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if _validate_session_id(existing.get("session_id", "")) != session_id:
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
        "meta": meta or {},
    }
    if existing_path is not None and existing_payload is not None:
        existing_type = str(existing_payload.get("type", "") or "").strip()
        existing_priority = _SIGNAL_PRIORITY.get(existing_type, 0)
        new_priority = _SIGNAL_PRIORITY.get(signal_type, 0)
        merged_meta = dict(existing_payload.get("meta", {}) or {})
        merged_meta.update(meta or {})
        if existing_priority > new_priority:
            payload["type"] = existing_type
        payload["meta"] = merged_meta
        _atomic_write(existing_path, json.dumps(payload))
        return existing_path

    # B047: Use UUID suffix for uniqueness (avoids ms-level collision)
    fname = f"{int(time.time() * 1000)}_{os.getpid()}_{uuid.uuid4().hex[:8]}_{signal_type}.json"
    sig_path = sig_dir / fname
    _atomic_write(sig_path, json.dumps(payload))
    return sig_path


def _pending_signal_sort_key(signal_data: Dict[str, Any]) -> Tuple[int, str]:
    signal_type = str(signal_data.get("type") or signal_data.get("signal_type") or "")
    signal_path = str(signal_data.get("_signal_path") or "")
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
        except (json.JSONDecodeError, OSError):
            # Remove corrupt signal files
            try:
                f.unlink()
            except OSError:
                pass
    signals.sort(key=_pending_signal_sort_key)
    return signals[:MAX_SIGNALS_PER_POLL]


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
) -> None:
    """Finalize a no-payload branch consistently across short-circuit paths."""
    signal_type = str(
        signal_data.get("type") or signal_data.get("signal_type") or ""
    ).strip().lower()
    if signal_type == "timeout":
        write_context_refresh_timeout_marker(session_id)
    if next_cursor_offset is not None:
        write_cursor(
            session_id,
            int(next_cursor_offset),
            transcript_path,
            source_key=cursor_key,
        )
    if clear_state:
        clear_rolling_state(session_id)
    if callable(emit_noop_metric):
        emit_noop_metric()
    mark_signal_processed(signal_data)
    _release_session_processing_lock(lock_owner_key, lock_fd)


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
        "line_offset": 0,
        "transcript_path": "",
        "internal": False,
        "transcript_size_bytes": 0,
        "cursor_key": cursor_file.stem,
    }
    if not cursor_file.is_file():
        return defaults
    try:
        data = json.loads(cursor_file.read_text(encoding="utf-8"))
        cursor_key = str(data.get("cursor_key") or cursor_file.stem or "").strip()
        if not _SESSION_ID_RE.match(cursor_key):
            cursor_key = _cursor_storage_key(fallback_session_id, cursor_key)
        return {
            "line_offset": int(data.get("line_offset", 0)),
            "transcript_path": data.get("transcript_path", ""),
            "internal": bool(data.get("internal", False)),
            "transcript_size_bytes": int(data.get("transcript_size_bytes", 0) or 0),
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
    try:
        return int(os.path.getsize(transcript_path))
    except OSError:
        return 0


def write_cursor(
    session_id: str,
    line_offset: int,
    transcript_path: str,
    *,
    internal: bool = False,
    source_key: Optional[str] = None,
) -> None:
    """Write extraction cursor after processing."""
    session_id = _validate_session_id(session_id)
    cursor_key = _cursor_storage_key(session_id, source_key)
    cursor_dir = _cursor_dir()
    cursor_file = cursor_dir / f"{cursor_key}.json"
    payload = {
        "session_id": session_id,
        "cursor_key": cursor_key,
        "line_offset": line_offset,
        "transcript_path": transcript_path,
        "internal": bool(internal),
        "transcript_size_bytes": _transcript_size_bytes(transcript_path),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        _atomic_write(cursor_file, json.dumps(payload))
    except OSError as e:
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


def _read_cursor_with_source_compat(session_id: str, source_key: Optional[str]) -> Dict[str, Any]:
    """Read a source-aware cursor while tolerating patched one-arg test doubles."""
    if source_key:
        try:
            return read_cursor(session_id, source_key=source_key)
        except TypeError:
            pass
    return read_cursor(session_id)


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
) -> None:
    """Save an unextracted transcript chunk for later janitor recovery.

    Written when a provider outage exhausts the 6-hour retry window.
    The janitor's deferred_extraction task picks these up and retries
    extraction when the provider is back.
    """
    ts = int(time.time())
    filename = f"{session_id}_{ts}.json"
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
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        logger.warning(
            "[daemon] saved deferred extraction for session %s: %s (%d chars)",
            session_id, path, len(transcript_text),
        )
    except Exception as e:
        logger.error(
            "[daemon] failed to save deferred extraction for session %s: %s",
            session_id, e,
        )


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
        try:
            os.close(fd)
        except OSError:
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
    except OSError:
        pass
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
    try:
        _processing_lock_path(session_id).unlink()
    except OSError:
        pass


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
        logger.warning("rolling state read failed for %s; resetting staged state", session_id)
        return {
            "session_id": session_id,
            "transcript_path": "",
            "carry_facts": [],
            "raw_facts": [],
            "raw_snippets": {},
            "raw_journal": {},
            "raw_project_logs": {},
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
    data.setdefault("rolling_batches", 0)
    data.setdefault("processed_line_offset", 0)
    data.setdefault("buffered_line_offset", int(data.get("processed_line_offset", 0) or 0))
    data.setdefault("semantic_buffer", "")
    data.setdefault("semantic_buffer_tokens", 0)
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
    removed = False
    try:
        target_path.unlink()
        removed = True
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("rolling state unlink failed for %s at %s: %s", session_id, target_path, exc)

    if removed:
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
            if str(payload.get("session_id") or "").strip() != str(session_id or "").strip():
                continue
            try:
                state_file.unlink()
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
    except Exception:
        return 0.98, 0.88, False


def _semantic_candidate_overlaps(new_text: str, existing_facts: List[Dict[str, Any]], max_candidates: int = 12) -> List[int]:
    from lib.tokens import extract_key_tokens

    new_tokens = set(extract_key_tokens(new_text, max_tokens=10))
    if not new_tokens:
        return []
    scored: List[Tuple[int, int]] = []
    for idx, fact in enumerate(existing_facts):
        text = str((fact or {}).get("text", "") or "").strip()
        if len(text.split()) < 3:
            continue
        existing_tokens = set(extract_key_tokens(text, max_tokens=10))
        overlap = len(new_tokens & existing_tokens)
        if overlap <= 0:
            continue
        if overlap >= 2 or len(new_tokens) <= 3:
            scored.append((overlap, idx))
    scored.sort(key=lambda item: (-item[0], -item[1]))
    return [idx for _overlap, idx in scored[:max_candidates]]


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
        if len(new_text.split()) < 3:
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
            if len(existing_text.split()) < 3:
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


def staged_state_has_payload(state: Dict[str, Any]) -> bool:
    return bool(
        _has_text_payload(state.get("raw_facts"))
        or _has_text_payload(state.get("raw_snippets"))
        or _has_text_payload(state.get("raw_journal"))
        or _has_text_payload(state.get("raw_project_logs"))
    )


def _session_has_harvestable_subagents(session_id: str, adapter=None) -> bool:
    """Return True when a parent has completed child transcripts waiting."""
    try:
        import importlib
        subagent_registry = importlib.import_module("core.subagent_registry")
        if subagent_registry.get_harvestable(session_id):
            return True
    except Exception:
        pass
    try:
        discover_children_fn = getattr(adapter, "discover_subagent_children", None) if adapter is not None else None
        if not callable(discover_children_fn):
            return False
        for child in discover_children_fn(session_id):
            if str(child.get("child_id") or "").strip() and str(child.get("transcript_path") or "").strip():
                return True
    except Exception:
        pass
    return False


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


def _read_usage_totals() -> Dict[str, int]:
    totals = {
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
    path = _usage_events_path()
    if not path.is_file():
        return totals
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
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
    except OSError:
        return totals
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
            fh.write(json.dumps(payload) + "\n")
    except OSError as exc:
        logger.warning("rolling metric write failed for %s: %s", session_id, exc)


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


def _parse_transcript_lines(lines: List[str], adapter=None) -> str:
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
        return ""
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _append_semantic_buffer(state: Dict[str, Any], parsed_text: str, line_offset: int) -> Dict[str, Any]:
    """Append parsed transcript text to the persisted semantic rolling buffer."""
    from lib.tokens import estimate_tokens

    merged = dict(state or {})
    existing = str(merged.get("semantic_buffer", "") or "").strip()
    incoming = str(parsed_text or "").strip()
    if existing and incoming:
        combined = f"{existing}\n\n{incoming}"
    else:
        combined = existing or incoming
    merged["semantic_buffer"] = combined
    merged["semantic_buffer_tokens"] = estimate_tokens(combined) if combined else 0
    merged["buffered_line_offset"] = max(
        int(merged.get("buffered_line_offset", 0) or 0),
        int(line_offset or 0),
    )
    merged["processed_line_offset"] = int(merged["buffered_line_offset"])
    return merged


def _semantic_buffer_has_content(state: Dict[str, Any]) -> bool:
    return bool(str((state or {}).get("semantic_buffer", "") or "").strip())


def _buffer_transcript_tail(
    transcript_path: str,
    start_line: int,
    state: Dict[str, Any],
    *,
    adapter=None,
    max_tokens: Optional[int] = None,
    max_lines: int = 0,
) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """Parse new raw session lines into the semantic rolling buffer."""
    if max_tokens is not None and int(max_tokens or 0) > 0:
        lines = read_transcript_token_window(
            transcript_path,
            start_line,
            int(max_tokens),
            int(max_lines or 0),
            adapter=adapter,
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
    parsed_text = _parse_transcript_lines(lines, adapter=adapter)
    merged = _append_semantic_buffer(state, parsed_text, start_line + len(lines))
    merged["transcript_path"] = str(transcript_path or merged.get("transcript_path", "") or "")
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
    stage_result = extract_from_transcript(
        transcript=transcript_text,
        owner_id=owner,
        label=label,
        session_id=session_id,
        dry_run=True,
        carry_facts=list(staged_state.get("carry_facts", []) or []),
        wall_timeout_seconds=600.0,
    )
    stage_embedding_stats = _warm_payload_embeddings(stage_result.get("raw_facts", []) or [])
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
        logger.error(
            "[%s] session %s: %d/%d chunks failed extraction "
            "(non-provider failure); saving transcript for janitor recovery",
            label, session_id, failed_chunks, chunks_total,
        )
        _save_deferred_extraction(
            session_id=session_id,
            transcript_text=transcript_text,
            owner_id=owner,
            label=label,
            reason=f"non_provider_failure_{failed_chunks}_of_{chunks_total}_chunks",
        )
    staged_state = merge_staged_payloads(staged_state, stage_result)
    staged_state["processed_line_offset"] = buffered_line_offset
    staged_state["buffered_line_offset"] = buffered_line_offset
    _write_extraction_buffer_log(
        session_id,
        phase="rolling_stage",
        signal_type=signal_type,
        transcript_text=transcript_text,
    )
    staged_state["semantic_buffer"] = ""
    staged_state["semantic_buffer_tokens"] = 0
    staged_state["transcript_path"] = transcript_path
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
    except OSError:
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
) -> List[str]:
    """Read a single message-aligned transcript window up to the token budget."""
    from lib.tokens import estimate_tokens

    lines: List[str] = []
    approx_tokens = 0
    budgeted_lines = 0
    saw_extractable_conversation = False
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
                candidate = lines + [line]
                candidate_parsed = _parse_transcript_lines(candidate, adapter=adapter)
                candidate_extractable = bool(candidate_parsed)
                candidate_tokens = estimate_tokens(candidate_parsed) if candidate_extractable else 0
                if saw_extractable_conversation and budgeted_lines > 0 and candidate_extractable and candidate_tokens > max_tokens:
                    break
                lines.append(line)
                if candidate_extractable:
                    saw_extractable_conversation = True
                    budgeted_lines += 1
                if len(lines) >= MAX_TRANSCRIPT_LINES:
                    logger.warning(
                        "transcript %s: token window capped at %d lines (from offset %d)",
                        transcript_path,
                        MAX_TRANSCRIPT_LINES,
                        from_line,
                    )
                    break
    except OSError as e:
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
    except Exception:
        return None


def _adapter_owns_transcript_path(adapter, session_id: str, transcript_path: str) -> bool:
    """Return whether an adapter-scoped transcript belongs to this daemon instance."""
    if adapter is None:
        logger.warning(
            "adapter unavailable during transcript ownership check for session %s (%s)",
            session_id,
            transcript_path,
        )
        return False
    if not transcript_path:
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
        return bool(owns_fn(Path(transcript_path), session_id=session_id))
    except TypeError:
        try:
            return bool(owns_fn(Path(transcript_path)))
        except Exception as exc:
            logger.warning(
                "adapter transcript ownership check failed for session %s (%s): %s",
                session_id,
                transcript_path,
                exc,
            )
            try:
                from lib.fail_policy import is_fail_hard_enabled
                if is_fail_hard_enabled():
                    raise
            except ImportError:
                pass
            return False
    except Exception as exc:
        logger.warning(
            "adapter transcript ownership check failed for session %s (%s): %s",
            session_id,
            transcript_path,
            exc,
        )
        try:
            from lib.fail_policy import is_fail_hard_enabled
            if is_fail_hard_enabled():
                raise
        except ImportError:
            pass
        return False


def _ensure_discovered_session_cursors(adapter=None) -> int:
    """Seed cursors for transcript files that exist but have never been seen.

    Some host paths create transcript JSONL files without going through adapter
    lifecycle hooks first (for example OC Matrix channel sessions). The daemon
    still needs to discover those sessions so rolling/timeout extraction can
    operate on them.
    """
    active_adapter = adapter if adapter is not None else _load_runtime_adapter()
    if active_adapter is None or not hasattr(active_adapter, "get_sessions_dir"):
        return 0
    try:
        sessions_dir = active_adapter.get_sessions_dir()
    except Exception:
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
        if cursor_file.exists() or legacy_cursor_file.exists():
            cursor_data = read_cursor(session_id, source_key=source_cursor_key)
            existing_path = str(cursor_data.get("transcript_path") or "").strip()
            if existing_path and os.path.isfile(existing_path):
                continue
            line_offset = int(cursor_data.get("line_offset", 0) or 0)
            internal = bool(cursor_data.get("internal", False))
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


def _is_internal_transcript_session(
    session_id: str,
    transcript_path: str,
    adapter=None,
) -> bool:
    """Return True when a non-empty raw transcript sanitizes to nothing."""
    try:
        if not transcript_path or not os.path.isfile(transcript_path):
            return False
        total_lines = count_transcript_lines(transcript_path)
        if total_lines <= 0:
            return False
        active_adapter = adapter if adapter is not None else _load_runtime_adapter()
        if active_adapter is None:
            return False
        transcript_text = active_adapter.parse_session_jsonl(Path(transcript_path))
        return not bool((transcript_text or "").strip())
    except Exception:
        return False


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


def _reconcile_internal_cursor_state(
    session_id: str,
    transcript_path: str,
    *,
    cursor_data: Optional[Dict[str, Any]] = None,
    cursor_key: Optional[str] = None,
    adapter=None,
) -> str:
    """Return internal-session handling state for the current transcript.

    States:
    - "frozen": cursor is marked internal and no new transcript lines arrived.
    - "advanced": transcript is still internal-only; cursor was advanced to EOF.
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

    if cursor_internal and total_lines <= cursor_offset:
        return "frozen"

    if _is_internal_transcript_session(session_id, transcript_path, adapter=adapter):
        _advance_internal_session_cursor_to_end(
            session_id,
            transcript_path,
            cursor_key=cursor_key,
        )
        return "advanced"

    if cursor_internal:
        write_cursor(
            session_id,
            cursor_offset,
            transcript_path,
            internal=False,
            source_key=cursor_key,
        )
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
    _reload_config_if_changed("signal processing")
    signal_type = signal_data.get("type", "unknown")
    session_id = _validate_session_id(signal_data.get("session_id", "unknown"))
    transcript_path = signal_data.get("transcript_path", "")
    label = f"daemon-{signal_type}"
    rolling_mode = signal_type == "rolling"
    staged_state = read_rolling_state(session_id)

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

    lock_owner_key = _signal_source_cursor_key(
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
        # Dedup: if another signal for this session already exists in the queue,
        # mark this one processed to prevent unbounded pile-up when orphan scan
        # generates multiple redundant signals while a lock is held.
        try:
            sig_dir = _signal_dir()
            current_path = signal_data.get("_signal_path", "")
            for f in sig_dir.iterdir():
                if not f.name.endswith(".json") or str(f) == current_path:
                    continue
                try:
                    other = json.loads(f.read_text(encoding="utf-8"))
                    if other.get("session_id") == session_id:
                        # Do not discard a compaction/reset/session_end signal just
                        # because a rolling signal is already queued — these signals
                        # must survive to flush staged facts once rolling completes
                        # (B009; session_end added for FIFO ordering).
                        other_type = other.get("type", "")
                        if signal_type in ("compaction", "reset", "session_end") and other_type == "rolling":
                            continue
                        mark_signal_processed(signal_data)
                        break
                except (json.JSONDecodeError, OSError):
                    pass
        except Exception:
            pass
        return

    try:
        from core.subagent_registry import is_registered_subagent
        if is_registered_subagent(session_id):
            logger.info("[%s] session %s: registered subagent, skipping standalone extraction", label, session_id)
            mark_signal_processed(signal_data)
            _release_session_processing_lock(lock_owner_key, lock_fd)
            return
    except Exception:
        pass

    adapter = None
    try:
        from lib.adapter import get_adapter
        adapter = get_adapter()
    except Exception:
        adapter = None

    if transcript_path and os.path.isfile(transcript_path) and not _adapter_owns_transcript_path(adapter, session_id, transcript_path):
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

    cursor_data = _read_cursor_with_source_compat(session_id, lock_owner_key)
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
    except Exception:
        pass

    # FIFO ordering: if this is a session_end signal but a rolling signal for
    # the same session is still pending (not yet processing), defer this
    # session_end so rolling can stage its facts first.  Without this, a
    # session_end picked up before the rolling job runs would find empty
    # rolling_state and lose staged carry_facts.
    if signal_type == "session_end":
        try:
            _ses_sig_path = signal_data.get("_signal_path", "")
            for _rf in list(_signal_dir().iterdir()):
                if not _rf.name.endswith(".json") or str(_rf) == _ses_sig_path:
                    continue
                try:
                    _rs = json.loads(_rf.read_text(encoding="utf-8"))
                    if _rs.get("session_id") == session_id and _rs.get("type") == "rolling":
                        logger.info(
                            "[%s] session %s: session_end deferred — pending rolling signal "
                            "found; will retry after rolling extraction completes (FIFO)",
                            label, session_id,
                        )
                        _release_session_processing_lock(lock_owner_key, lock_fd)
                        return  # preserve signal on disk; retry next poll cycle
                except (json.JSONDecodeError, OSError):
                    pass
        except Exception as _fifo_err:
            logger.debug("[%s] FIFO rolling-pending check failed: %s", label, _fifo_err)

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
                    _dup_priority = _SIGNAL_PRIORITY.get(_dup.get("type", ""), 99)
                    if _dup_priority < _current_priority:
                        # Higher-priority signal (e.g. rolling) — preserve it so it
                        # can stage content before this flush processes.
                        continue
                    _dup["_signal_path"] = str(_dup_f)
                    mark_signal_processed(_dup)
            except (json.JSONDecodeError, OSError):
                pass
    except Exception:
        pass

    if not transcript_path or not os.path.isfile(transcript_path):
        for _fallback_source, _fallback_path in (
            ("cursor", str(cursor_data.get("transcript_path") or "").strip()),
            ("rolling_state", str(staged_state.get("transcript_path") or "").strip()),
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
            logger.warning("[%s] transcript not found: %s", label, transcript_path)
            mark_signal_processed(signal_data)
            _release_session_processing_lock(lock_owner_key, lock_fd)
            return

    if not _adapter_owns_transcript_path(adapter, session_id, transcript_path):
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

    cursor_offset = int(cursor_data["line_offset"] or 0)
    cursor_transcript = cursor_data["transcript_path"]

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
        except Exception:
            pass

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
        elif _is_reset_rename and cursor_offset > 0:
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
        elif _is_dir_relocation and signal_type != "reset":
            # Same-basename path: session file relocated to a different directory.
            # Content is identical; preserve cursor to avoid duplicate extraction.
            logger.info(
                "[%s] session %s: transcript directory relocation (%s -> %s), preserving cursor",
                label, session_id, cursor_transcript, transcript_path,
            )
        elif _is_dir_relocation:
            # Reset signal on a relocated transcript. Two sub-cases:
            # (a) cursor_offset == 0: content not yet extracted — reset and extract
            #     the full content from the relocated file (M10 scenario: first
            #     signal for this session arrives via the relocated path).
            # (b) cursor_offset > 0: content already extracted in a prior pass
            #     (M2 scenario: duplicate reset signal after relocation). The
            #     relocated file has identical content; preserve cursor to avoid
            #     re-extracting and storing duplicate facts.
            if cursor_offset == 0:
                logger.info(
                    "[%s] session %s: reset signal on relocated transcript, no prior extraction (%s -> %s), resetting cursor for full extraction",
                    label, session_id, cursor_transcript, transcript_path,
                )
                cursor_offset = 0
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
        elif _is_cursor_on_backup_to_plain and cursor_offset > 0:
            # Cursor was written against a .reset.* backup; new signal points to
            # the plain preserved copy of the same session content. Content up to
            # cursor_offset is already extracted.
            # Skip extraction entirely and leave the cursor on the backup path.
            # If we let extraction run (even with "no new content"), write_cursor
            # would update cursor_transcript to the plain path, which then triggers
            # _is_reset_rename on any late .reset.* signal and re-extracts from 0.
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
        else:
            logger.info(
                "[%s] session %s: transcript path changed (%s -> %s), resetting cursor",
                label, session_id, cursor_transcript, transcript_path,
            )
            cursor_offset = 0

    total_lines = count_transcript_lines(transcript_path)
    cursor_clamped_to_eof = False
    if cursor_offset > total_lines:
        same_transcript_source = (
            bool(cursor_transcript)
            and bool(transcript_path)
            and _canonicalize_transcript_source_path(cursor_transcript)
            == _canonicalize_transcript_source_path(transcript_path)
        )
        if same_transcript_source and signal_type != "reset":
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

    chunk_budget = _get_capture_chunk_tokens()
    chunk_line_budget = _get_capture_chunk_max_lines()
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
    if total_lines > buffered_line_offset:
        buffer_kwargs: Dict[str, Any] = {"adapter": adapter}
        if rolling_mode:
            buffer_kwargs["max_tokens"] = chunk_budget
            buffer_kwargs["max_lines"] = chunk_line_budget
        staged_state, semantic_buffer_metrics = _buffer_transcript_tail(
            transcript_path,
            buffered_line_offset,
            staged_state,
            **buffer_kwargs,
        )
        write_rolling_state(session_id, staged_state)
        if rolling_mode:
            buffered_line_offset = int(
                staged_state.get("buffered_line_offset", buffered_line_offset) or buffered_line_offset
            )
        else:
            refreshed_semantic_buffer_for_nonrolling = True
    read_start_offset = cursor_offset if rolling_mode else buffered_line_offset
    pending_subagent_harvest = False
    new_lines = (
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
        if not rolling_mode and (
            staged_state_has_payload(staged_state)
            or _semantic_buffer_has_content(staged_state)
            or pending_subagent_harvest
        ):
            new_lines = []
        else:
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
                tmp.writelines(new_lines)
                tmp_path = tmp.name
            if adapter is None:
                from lib.adapter import get_adapter
                adapter = get_adapter()
            transcript_text = adapter.parse_session_jsonl(Path(tmp_path))
        if rolling_mode:
            transcript_text = str(staged_state.get("semantic_buffer", "") or "").strip()
        if not rolling_mode and _semantic_buffer_has_content(staged_state):
            if int(staged_state.get("semantic_buffer_tokens", 0) or 0) >= chunk_budget:
                operation_phase = "rolling_stage_extract"
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
            if refreshed_semantic_buffer_for_nonrolling:
                # The non-rolling pre-buffer step already appended the new tail
                # into semantic_buffer. Re-appending tail_text here would
                # duplicate the fresh session content on under-budget flushes.
                transcript_text = buffered_text or tail_text
            else:
                transcript_text = f"{buffered_text}\n\n{tail_text}" if buffered_text and tail_text else (buffered_text or tail_text)

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
                )
                return

        if not rolling_mode and transcript_text.strip():
            # Guard against post-compaction status lines or other metadata-only content
            # (e.g. "Compacted (17k -> 2.1k)") that aren't extractable conversations.
            # 50 chars is enough to skip pure metadata lines (~25 chars) without
            # silently dropping short but valid user messages.
            _MIN_EXTRACTABLE_CHARS = 50
            transcript_len = len(transcript_text.strip())
            if transcript_len < _MIN_EXTRACTABLE_CHARS:
                if staged_state_has_payload(staged_state):
                    logger.info(
                        "[%s] session %s: transcript too short to extract (%d chars < %d min); "
                        "flushing staged payload only",
                        label, session_id, transcript_len, _MIN_EXTRACTABLE_CHARS,
                    )
                    transcript_text = ""
                else:
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
                        clear_state=signal_type in ("reset", "session_end", "compaction"),
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

        if rolling_mode:
            operation_phase = "rolling_stage_extract"
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
                )
                mark_signal_processed(signal_data)
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
            write_cursor(
                session_id,
                buffered_line_offset,
                transcript_path,
                source_key=lock_owner_key,
            )
            mark_signal_processed(signal_data)
            if buffered_line_offset > cursor_offset and total_lines > buffered_line_offset:
                remaining_tokens = estimate_unextracted_tokens(
                    transcript_path,
                    buffered_line_offset,
                    chunk_budget,
                )
                write_signal(
                    signal_type="rolling",
                    session_id=session_id,
                    transcript_path=transcript_path,
                    meta={
                        "reason": "continued_chunk_budget",
                        "chunk_tokens": chunk_budget,
                        "chunk_lines": chunk_line_budget,
                        "buffered_line_offset": buffered_line_offset,
                        "remaining_tokens_estimate": remaining_tokens,
                        "remaining_lines": max(0, int(total_lines) - int(buffered_line_offset)),
                    },
                )
            return

        tail_result = None
        operation_phase = "flush_extract"
        usage_before_extract = _read_usage_totals()
        extract_started_at = time.time()
        if transcript_text.strip():
            tail_result = extract_from_transcript(
                transcript=transcript_text,
                owner_id=owner,
                label=label,
                session_id=session_id,
                dry_run=True,
                carry_facts=list(staged_state.get("carry_facts", []) or []),
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
                child_result = extract_from_transcript(
                    transcript=child_text,
                    owner_id=owner,
                    label=f"{label}-subagent",
                    session_id=session_id,
                    dry_run=True,
                    carry_facts=[],
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
        publish_started_at = time.time()
        result = apply_extracted_payloads(
            flush_payload,
            owner_id=owner,
            label=label,
            session_id=session_id,
            dry_run=False,
        )
        publish_wall = time.time() - publish_started_at
        usage_after_publish = _read_usage_totals()
        extract_usage = _usage_delta(usage_before_extract, usage_after_extract)
        publish_usage = _usage_delta(usage_before_publish, usage_after_publish)

        facts_stored = result.get("facts_stored", 0)
        facts_skipped = result.get("facts_skipped", 0)
        edges_created = result.get("edges_created", 0)
        snippets_count = sum(
            len(v)
            for v in (result.get("snippets", {}) or {}).values()
            if isinstance(v, list)
        )
        journals_count = len(result.get("journal", {}) or {})
        project_log_metrics = dict(result.get("project_log_metrics", {}) or {})
        logger.info("[%s] session %s: %d stored, %d skipped, %d edges",
                    label, session_id, facts_stored, facts_skipped, edges_created)

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

        _write_extraction_buffer_log(
            session_id,
            phase="final_flush",
            signal_type=signal_type,
            transcript_text=transcript_text,
        )
        if signal_type == "timeout":
            write_context_refresh_timeout_marker(session_id)
        write_cursor(
            session_id,
            total_lines,
            transcript_path,
            source_key=lock_owner_key,
        )
        clear_rolling_state(session_id)
        if mark_harvested_fn is not None:
            try:
                for child in harvestable:
                    mark_harvested_fn(session_id, child.get("child_id", ""))
            except Exception as e:
                logger.warning("[%s] session %s: mark_harvested error: %s", label, session_id, e)
        mark_signal_processed(signal_data)

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
            signal_type=signal_type,
            signal_timestamp=signal_data.get("timestamp"),
            staged_batches=int(staged_state.get("rolling_batches", 0) or 0),
            staged_facts=len(staged_state.get("raw_facts", []) or []),
            final_raw_fact_count=len(flush_payload.get("raw_facts", []) or []),
            final_facts_stored=facts_stored,
            final_facts_skipped=facts_skipped,
            final_edges_created=edges_created,
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
        )

    except Exception as e:
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
                signal_type=signal_type,
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
        raw: int = default
        for _cp in reversed(list(_config_paths())):
            if _cp.exists():
                try:
                    _data = _json.loads(_cp.read_text(encoding="utf-8"))
                    _capture = _data.get("capture", {})
                    _v = _capture.get("inactivity_timeout_minutes") or _capture.get("inactivityTimeoutMinutes")
                    if _v is not None:
                        raw = _v
                except Exception:
                    pass
        return max(0, int(raw))
    except Exception:
        return default


def _get_compact_on_timeout(default: bool = True) -> bool:
    """Read timeout compaction toggle from live config with legacy alias support."""
    try:
        import json as _json
        from config import _config_paths
        raw: bool = default
        for _cp in reversed(list(_config_paths())):
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
                except Exception:
                    pass
        return bool(raw)
    except Exception:
        return default


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
    except Exception:
        pass

    # B003: Hoist pending signals read outside the loop
    pending = read_pending_signals()
    pending_session_ids = {s.get("session_id") for s in pending}

    cursor_rows: list[dict[str, Any]] = []
    for cursor_file in cursor_dir.glob("*.json"):
        try:
            data = json.loads(cursor_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        session_id = data.get("session_id", "")
        transcript_path = data.get("transcript_path", "")
        if not session_id or not transcript_path or not os.path.isfile(transcript_path):
            continue
        if not _adapter_owns_transcript_path(adapter, str(session_id), str(transcript_path)):
            continue
        internal_state = _reconcile_internal_cursor_state(
            session_id,
            transcript_path,
            cursor_data=data,
            cursor_key=str(data.get("cursor_key") or "").strip() or None,
            adapter=adapter,
        )
        if internal_state == "frozen":
            continue
        if internal_state == "advanced":
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
        if internal_state == "unfrozen":
            logger.info(
                "session %s gained non-internal content past a frozen internal cursor during idle scan",
                session_id,
            )

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
            "current_size_bytes": _transcript_size_bytes(transcript_path),
            "mtime": mtime,
        })

    for row in cursor_rows:
        session_id = str(row["session_id"])
        transcript_path = str(row["transcript_path"])
        cursor_offset = int(row["cursor_offset"])
        cursor_size_bytes = int(row.get("cursor_size_bytes", 0) or 0)
        has_cursor_size_bytes = bool(row.get("has_cursor_size_bytes", False))
        current_size_bytes = int(row.get("current_size_bytes", 0) or 0)
        mtime = float(row["mtime"])

        # Check if transcript has grown past cursor
        total_lines = count_transcript_lines(transcript_path)
        transcript_grew_past_cursor = has_cursor_size_bytes and current_size_bytes > cursor_size_bytes
        cursor_at_end = total_lines <= cursor_offset and not transcript_grew_past_cursor

        # If cursor has advanced past a previously-seen end, reset the "fired" marker
        # so new content can trigger a fresh timeout signal if needed.
        if not cursor_at_end:
            _cursor_end_timeout_fired.discard(session_id)

        # Even when all transcript content has been extracted (cursor at end),
        # staged rolling state from prior rolling_stage cycles still needs to be
        # flushed.  Check for a pending rolling payload so we can generate a
        # flush signal even when there are no new lines to extract.
        has_staged_payload = False
        if cursor_at_end:
            try:
                rolling = read_rolling_state(session_id)
                has_staged_payload = staged_state_has_payload(rolling)
            except Exception:
                pass

        if cursor_at_end and has_staged_payload and session_id not in pending_session_ids:
            newer_session_exists = any(
                float(other["mtime"]) > mtime and str(other["session_id"]) != session_id
                for other in cursor_rows
            )
            if newer_session_exists:
                logger.info(
                    "session %s cursor at end with staged payload and newer session activity detected, generating session_end flush",
                    session_id,
                )
                write_signal(
                    signal_type="session_end",
                    session_id=session_id,
                    transcript_path=transcript_path,
                )
                continue

        if cursor_at_end and not has_staged_payload:
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
        if not _adapter_owns_transcript_path(adapter, str(session_id), str(transcript_path)):
            continue
        internal_state = _reconcile_internal_cursor_state(
            session_id,
            transcript_path,
            cursor_data=data,
            cursor_key=str(data.get("cursor_key") or "").strip() or None,
            adapter=adapter,
        )
        if internal_state == "frozen":
            continue
        if internal_state == "advanced":
            logger.info("session %s is internal maintenance-only during rolling scan, advancing cursor to EOF", session_id)
            continue
        if internal_state == "unfrozen":
            logger.info(
                "session %s gained non-internal content past a frozen internal cursor during rolling scan",
                session_id,
            )
        if session_id in pending_session_ids:
            continue

        cursor_offset = int(data.get("line_offset", 0) or 0)
        total_lines = count_transcript_lines(transcript_path)
        state = read_rolling_state(session_id)
        buffered_line_offset = max(
            int(state.get("buffered_line_offset", cursor_offset) or 0),
            cursor_offset,
        )
        if total_lines > buffered_line_offset:
            state, _buffer_metrics = _buffer_transcript_tail(
                transcript_path,
                buffered_line_offset,
                state,
                adapter=adapter,
                max_tokens=chunk_budget,
                max_lines=chunk_line_budget,
            )
            write_rolling_state(session_id, state)
            buffered_line_offset = int(state.get("buffered_line_offset", buffered_line_offset) or buffered_line_offset)

        semantic_tokens = int(state.get("semantic_buffer_tokens", 0) or 0)
        should_signal = semantic_tokens >= chunk_budget or (
            semantic_tokens > 0 and buffered_line_offset < total_lines
        )
        if not should_signal:
            continue

        if semantic_tokens >= chunk_budget:
            logger.info(
                "session %s crossed rolling extract budget (%d >= %d semantic tokens), generating rolling signal",
                session_id,
                semantic_tokens,
                chunk_budget,
            )
        else:
            logger.info(
                "session %s has staged semantic buffer below budget (%d < %d) with unread transcript tail, generating rolling signal",
                session_id,
                semantic_tokens,
                chunk_budget,
            )
        write_signal(
            signal_type="rolling",
            session_id=session_id,
            transcript_path=transcript_path,
            meta={
                "reason": "semantic_chunk_budget",
                "chunk_tokens": chunk_budget,
                "semantic_buffer_tokens": semantic_tokens,
                "buffered_line_offset": buffered_line_offset,
            },
        )


def _retry_missing_embeddings() -> int:
    """Retry embeddings for nodes stored without one (e.g. when Ollama was down).

    Called every ~5 minutes from the daemon loop. Returns count of nodes updated.
    """
    try:
        from datastore.memorydb.memory_graph import MemoryGraph
        graph = MemoryGraph()
        count = graph.retry_missing_embeddings(limit=20)
        if count:
            logger.info("[daemon] embed-retry: backfilled %d missing embedding(s)", count)
        return count
    except Exception as e:
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
    _EMBED_RETRY_INTERVAL = 300.0  # retry missing embeddings every 5 minutes

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
                except _ProviderUnavailableError as pue:
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
                    logger.error("failed processing signal: %s", e, exc_info=True)
                    # Preserve the signal for a future retry. Outer-loop exceptions
                    # mean we do not know whether processing was durable.

            try:
                check_chunk_ready_sessions()
            except Exception as e:
                logger.error("rolling chunk readiness check failed: %s", e)

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
                    logger.error("idle check failed: %s", e)
                last_idle_check = now

            # Periodic embedding retry — backfill facts stored without embeddings
            if now - last_embed_retry_check > _EMBED_RETRY_INTERVAL:
                try:
                    _retry_missing_embeddings()
                except Exception as e:
                    logger.debug("embed retry failed: %s", e)
                last_embed_retry_check = now

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
    if os.environ.get("QUAID_SUPERVISOR_DISABLE", "").strip() != "1":
        try:
            from core.project_docs import ensure_supervisor_alive
            from core import project_docs
            project_docs.enable_instance_monitor(_instance_id())
            ensure_supervisor_alive()
        except Exception as exc:
            logger.warning("project docs supervisor ensure_alive failed: %s", exc)
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
                wait_default = project_docs.pid_startup_wait_seconds()
                wait_seconds = float(os.environ.get("QUAID_INSTANCE_MONITOR_WAIT_SECONDS", str(wait_default)) or wait_default)
            except ValueError:
                wait_seconds = project_docs.pid_startup_wait_seconds()
            deadline = time.time() + max(0.5, wait_seconds)
            while time.time() < deadline:
                time.sleep(0.1)
                pid = read_pid()
                if pid is not None:
                    return pid
            msg = "supervisor did not start an instance monitor before timeout"
            logger.warning(msg)
            try:
                from lib.fail_policy import is_fail_hard_enabled
            except Exception:
                fail_hard = False
            else:
                fail_hard = bool(is_fail_hard_enabled())
            if fail_hard:
                raise RuntimeError(msg)
            return -1
    pid = read_pid()
    if pid is not None:
        return pid
    return start_daemon()


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
        matching = _matching_daemon_pids()
        if len(matching) == 1:
            write_pid(matching[0])
            return matching[0]
        if matching:
            logger.warning(
                "reaping %d matching extraction daemon(s) before spawn for home=%s instance=%s: %s",
                len(matching),
                _quaid_home(),
                _instance_id(),
                ",".join(str(pid) for pid in matching),
            )
            for pid in matching:
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
        env["QUAID_HOME"] = str(_quaid_home())
        env["QUAID_DAEMON"] = "1"

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
    for match_pid in _matching_daemon_pids():
        if match_pid not in targets:
            targets.append(match_pid)
    if not targets:
        remove_pid()
        return False
    stopped = False
    for target_pid in targets:
        stopped = _terminate_daemon_pid(target_pid) or stopped
    remove_pid()
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
        try:
            daemon_loop()
        except Exception as _e:
            logger.error("daemon crashed: %s", _e, exc_info=True)
        finally:
            _remove_pid_if_matches(os.getpid())
            os._exit(0)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
