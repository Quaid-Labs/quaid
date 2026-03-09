#!/usr/bin/env python3
"""Quaid Extraction Daemon — shared extraction coordinator.

A long-lived process (one per QUAID_HOME) that processes extraction signals
from adapters. Handles chunked extraction with carryover context, cursor
management, and compaction-aware timeout extraction.

Adapters write signal files to $QUAID_HOME/data/extraction-signals/.
The daemon polls for signals, processes them serially, and advances
cursors to prevent re-extraction.

Signal types:
    compaction   — Context is about to be compacted. Extract new content,
                   clear carryover (compaction = logical boundary).
    reset        — Session reset (/new, /reset). Extract + clear carryover.
    session_end  — Session ended cleanly. Extract remaining content.

Lifecycle:
    quaid daemon start   — Fork, write PID, exit parent.
    quaid daemon stop    — Send SIGTERM to PID.
    quaid daemon status  — Check if PID is alive.

Adapters ensure the daemon is alive on session init and launch it if not.
"""

import json
import logging
import os
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("quaid.daemon")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _quaid_home() -> Path:
    env = os.environ.get("QUAID_HOME", "").strip()
    return Path(env) if env else Path.home() / "quaid"


def _signal_dir() -> Path:
    d = _quaid_home() / "data" / "extraction-signals"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cursor_dir() -> Path:
    d = _quaid_home() / "data" / "session-cursors"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _carryover_dir() -> Path:
    d = _quaid_home() / "data" / "extraction-carryover"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pid_path() -> Path:
    return _quaid_home() / "data" / "extraction-daemon.pid"


def _log_path() -> Path:
    d = _quaid_home() / "logs" / "daemon"
    d.mkdir(parents=True, exist_ok=True)
    return d / "extraction-daemon.log"


# ---------------------------------------------------------------------------
# PID file management
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
    _pid_path().write_text(str(pid))


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
    # Use timestamp + PID for uniqueness
    fname = f"{int(time.time() * 1000)}_{os.getpid()}_{signal_type}.json"
    sig_path = sig_dir / fname
    sig_path.write_text(json.dumps(payload), encoding="utf-8")
    return sig_path


def read_pending_signals() -> List[Dict[str, Any]]:
    """Read all pending signal files, sorted by timestamp."""
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
    return signals


def mark_signal_processed(signal_data: Dict[str, Any]) -> None:
    """Remove a processed signal file."""
    sig_path = signal_data.get("_signal_path", "")
    if sig_path:
        try:
            Path(sig_path).unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Cursors
# ---------------------------------------------------------------------------

def read_cursor(session_id: str) -> int:
    """Read extraction cursor (line offset) for a session."""
    cursor_file = _cursor_dir() / f"{session_id}.json"
    if not cursor_file.is_file():
        return 0
    try:
        data = json.loads(cursor_file.read_text(encoding="utf-8"))
        return int(data.get("line_offset", 0))
    except (json.JSONDecodeError, ValueError, OSError):
        return 0


def write_cursor(session_id: str, line_offset: int, transcript_path: str) -> None:
    """Write extraction cursor after processing."""
    cursor_file = _cursor_dir() / f"{session_id}.json"
    payload = {
        "session_id": session_id,
        "line_offset": line_offset,
        "transcript_path": transcript_path,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        cursor_file.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as e:
        logger.error("cursor write failed for %s: %s", session_id, e)


# ---------------------------------------------------------------------------
# Carryover context (per-session extraction state)
# ---------------------------------------------------------------------------

def read_carryover(session_id: str) -> List[Dict[str, Any]]:
    """Read carryover facts from previous chunk extractions."""
    carry_file = _carryover_dir() / f"{session_id}.json"
    if not carry_file.is_file():
        return []
    try:
        data = json.loads(carry_file.read_text(encoding="utf-8"))
        return data.get("facts", [])
    except (json.JSONDecodeError, OSError):
        return []


def write_carryover(session_id: str, facts: List[Dict[str, Any]]) -> None:
    """Write carryover facts for the next chunk extraction."""
    carry_file = _carryover_dir() / f"{session_id}.json"
    payload = {
        "session_id": session_id,
        "facts": facts,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        carry_file.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as e:
        logger.error("carryover write failed for %s: %s", session_id, e)


def clear_carryover(session_id: str) -> None:
    """Clear carryover on compaction/reset (logical boundary)."""
    carry_file = _carryover_dir() / f"{session_id}.json"
    try:
        carry_file.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Transcript reading
# ---------------------------------------------------------------------------

def read_transcript_slice(transcript_path: str, from_line: int) -> List[str]:
    """Read transcript lines starting at from_line offset."""
    lines = []
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= from_line:
                    lines.append(line)
    except OSError as e:
        logger.error("failed reading transcript %s: %s", transcript_path, e)
    return lines


def count_transcript_lines(transcript_path: str) -> int:
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Core extraction processing
# ---------------------------------------------------------------------------

def process_signal(signal_data: Dict[str, Any]) -> None:
    """Process a single extraction signal.

    Reads transcript from cursor, passes to extract_from_transcript()
    which handles chunking, carryover, and storage internally.
    On compaction/reset signals, clears carryover after extraction.
    """
    signal_type = signal_data.get("type", "unknown")
    session_id = signal_data.get("session_id", "unknown")
    transcript_path = signal_data.get("transcript_path", "")
    label = f"daemon-{signal_type}"

    if not transcript_path or not os.path.isfile(transcript_path):
        logger.warning("[%s] transcript not found: %s", label, transcript_path)
        mark_signal_processed(signal_data)
        return

    cursor_offset = read_cursor(session_id)
    new_lines = read_transcript_slice(transcript_path, cursor_offset)

    if not new_lines:
        logger.info("[%s] session %s: no new content past cursor (offset=%d)",
                     label, session_id, cursor_offset)
        mark_signal_processed(signal_data)
        return

    # Write lines to temp file for the adapter's JSONL parser
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
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
            mark_signal_processed(signal_data)
            return

        # Load carryover from previous extractions in this session
        carry_facts = read_carryover(session_id)

        # Delegate to extract_from_transcript() — it handles chunking,
        # LLM calls, fact storage, snippets, journal, and project logs.
        from ingest.extract import extract_from_transcript

        owner = _get_owner_id()
        result = extract_from_transcript(
            transcript=transcript_text,
            owner_id=owner,
            label=label,
            session_id=session_id,
            carry_facts=carry_facts,
        )

        logger.info(
            "[%s] session %s: %d stored, %d skipped, %d edges",
            label, session_id,
            result.get("facts_stored", 0),
            result.get("facts_skipped", 0),
            result.get("edges_created", 0),
        )

        # Advance cursor
        write_cursor(session_id, cursor_offset + len(new_lines), transcript_path)

        # Carryover management: clear on compaction/reset, persist otherwise
        if signal_type in ("compaction", "reset"):
            clear_carryover(session_id)
        else:
            # Persist the carry_facts accumulated during extraction
            # extract_from_transcript modifies carry_facts in place
            write_carryover(session_id, carry_facts)

    except Exception as e:
        logger.error("[%s] session %s: extraction failed: %s", label, session_id, e,
                     exc_info=True)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    mark_signal_processed(signal_data)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_owner_id() -> str:
    owner = os.environ.get("QUAID_OWNER", "").strip()
    if owner:
        return owner
    try:
        from lib.config import get_config
        return get_config().users.default_owner
    except Exception:
        return "default"


# ---------------------------------------------------------------------------
# Idle session detection (for adapters with compaction control)
# ---------------------------------------------------------------------------

def check_idle_sessions(timeout_minutes: int = 30) -> None:
    """Check for sessions that have been idle beyond the timeout.

    Only generates timeout signals for sessions whose adapters advertise
    compaction control. Without compaction control, timeout extraction
    is pointless (context stays bloated anyway).
    """
    cursor_dir = _cursor_dir()
    if not cursor_dir.is_dir():
        return

    now = time.time()
    timeout_seconds = timeout_minutes * 60

    for cursor_file in cursor_dir.glob("*.json"):
        try:
            data = json.loads(cursor_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        session_id = data.get("session_id", "")
        transcript_path = data.get("transcript_path", "")
        if not session_id or not transcript_path or not os.path.isfile(transcript_path):
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

        idle_seconds = now - mtime
        if idle_seconds < timeout_seconds:
            continue

        # Check if we already have a pending signal for this session
        pending = read_pending_signals()
        already_pending = any(
            s.get("session_id") == session_id for s in pending
        )
        if already_pending:
            continue

        # Only generate timeout signal if we know this adapter supports
        # compaction control. Read from the last signal for this session
        # or from carryover metadata.
        # For now, skip timeout signals — adapters must opt in explicitly.
        # This is a placeholder for when OC migration adds compaction control
        # advertisement to signal metadata.
        logger.debug(
            "session %s idle for %.0fs with %d unextracted lines (skipping — "
            "timeout extraction requires compaction control)",
            session_id, idle_seconds, total_lines - cursor_offset,
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

    swept = 0
    for cursor_file in cursor_dir.glob("*.json"):
        try:
            data = json.loads(cursor_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        session_id = data.get("session_id", "")
        if not session_id or session_id == current_session_id:
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
    logger.info("extraction daemon started (pid=%d, home=%s)", os.getpid(), _quaid_home())
    write_pid(os.getpid())

    shutdown_requested = False

    def handle_sigterm(signum, frame):
        nonlocal shutdown_requested
        logger.info("SIGTERM received, processing remaining signals before exit...")
        shutdown_requested = True

    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)

    last_idle_check = 0.0

    try:
        while not shutdown_requested:
            # Process pending signals
            signals = read_pending_signals()
            for sig in signals:
                try:
                    process_signal(sig)
                except Exception as e:
                    logger.error("failed processing signal: %s", e, exc_info=True)
                    mark_signal_processed(sig)

            # Periodic idle session check
            now = time.time()
            if now - last_idle_check > idle_check_interval:
                try:
                    check_idle_sessions()
                except Exception as e:
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
                logger.error("shutdown signal processing failed: %s", e)
                mark_signal_processed(sig)

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
    """Start the daemon as a background process. Returns child PID."""
    existing = read_pid()
    if existing is not None:
        logger.info("daemon already running (pid=%d)", existing)
        return existing

    # Double-fork to fully detach
    pid = os.fork()
    if pid > 0:
        # Parent: wait briefly for child to write PID, then return
        time.sleep(0.2)
        return read_pid() or pid

    # First child: create new session
    os.setsid()

    pid2 = os.fork()
    if pid2 > 0:
        os._exit(0)

    # Second child: this is the daemon
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

    # Set up logging to the log file
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        force=True,
    )

    try:
        daemon_loop()
    except Exception as e:
        logger.error("daemon crashed: %s", e, exc_info=True)
    finally:
        remove_pid()
        os._exit(0)


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
