"""Host compatibility checking and circuit breaker.

Watches the host platform (OpenClaw/Claude Code) for version changes and
evaluates compatibility against a published matrix. Provides a circuit
breaker that can disable Quaid operations when an incompatible or dangerous
host version is detected.

Design:
- mtime on host binary checked every daemon tick (cheap stat() call)
- Full version check only when mtime changes or every 24h
- Compatibility matrix fetched from GitHub, cached locally
- Circuit breaker file controls system-wide operation mode
- Three modes: normal, degraded (read-only), safe_mode (all disabled)
- Never blocks boot — warns/degrades, user retains read access when possible
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Circuit breaker primitives and HostInfo live in lib so datastore/lib
# modules can import them without crossing subsystem boundaries.
# Re-export everything here for backward compatibility.
from lib.circuit_breaker import (  # noqa: F401
    NORMAL,
    DEGRADED,
    SAFE_MODE,
    CIRCUIT_BREAKER_FILE,
    CircuitBreakerState,
    read_circuit_breaker,
    check_read_allowed,
    check_write_allowed,
)
from lib.host import HostInfo  # noqa: F401

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Circuit breaker states (kept for reference — canonical values are in lib)
# ---------------------------------------------------------------------------

VERSION_CACHE_FILE = "host-version.json"
MATRIX_CACHE_FILE = "compatibility-matrix.json"

# GitHub raw URL for the compatibility matrix
MATRIX_URL = (
    "https://raw.githubusercontent.com/Quaid-Labs/quaid/main/compatibility.json"
)

# Adaptive check intervals based on current state
CHECK_INTERVAL_NORMAL = 86400       # 24h — known compatible, low urgency
CHECK_INTERVAL_UNTESTED = 3600      # 1h  — new host version, matrix may update soon
CHECK_INTERVAL_DEGRADED = 21600     # 6h  — incompatible, fix may be published
CHECK_INTERVAL_SAFE_MODE = 3600     # 1h  — everything blocked, recover ASAP
CHECK_INTERVAL_KILL_SWITCH = 3600   # 1h  — global emergency, check for lift


@dataclass
class MatrixEntry:
    """Single row in the compatibility matrix."""
    host: str
    host_range: str         # Semver range like ">=2026.3.0 <2026.5.0"
    quaid_range: str        # Semver range for Quaid version
    status: str             # "compatible", "incompatible"
    data_risk: bool = False
    message: str = ""
    fix: str = ""


# ---------------------------------------------------------------------------
# Semver comparison (simple — handles major.minor.patch and major.minor)
# ---------------------------------------------------------------------------

def _parse_version(v: str) -> Tuple[int, ...]:
    """Parse a version string into a tuple of ints."""
    parts = re.findall(r"\d+", v)
    return tuple(int(p) for p in parts) if parts else (0,)


def _version_satisfies(version: str, range_spec: str) -> bool:
    """Check if a version satisfies a range like '>=2026.3.0 <2026.5.0'."""
    v = _parse_version(version)
    for constraint in range_spec.strip().split():
        constraint = constraint.strip()
        if not constraint:
            continue
        if constraint.startswith(">="):
            if v < _parse_version(constraint[2:]):
                return False
        elif constraint.startswith("<="):
            if v > _parse_version(constraint[2:]):
                return False
        elif constraint.startswith(">"):
            if v <= _parse_version(constraint[1:]):
                return False
        elif constraint.startswith("<"):
            if v >= _parse_version(constraint[1:]):
                return False
        elif constraint.startswith("="):
            if v != _parse_version(constraint[1:]):
                return False
        else:
            # Exact match
            if v != _parse_version(constraint):
                return False
    return True


# ---------------------------------------------------------------------------
# Circuit breaker file operations
# (read_circuit_breaker, check_read_allowed, check_write_allowed re-exported
#  from lib.circuit_breaker above)
# ---------------------------------------------------------------------------

def _breaker_path(data_dir: Path) -> Path:
    return data_dir / CIRCUIT_BREAKER_FILE


def write_circuit_breaker(data_dir: Path, state: CircuitBreakerState) -> None:
    """Write the circuit breaker state file."""
    p = _breaker_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": state.status,
        "reason": state.reason,
        "set_by": state.set_by,
        "set_at": state.set_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host_version": state.host_version,
        "message": state.message,
        "untested": state.untested,
    }
    p.write_text(json.dumps(payload, indent=2) + "\n")
    logger.info("Circuit breaker set to %s: %s", state.status, state.reason or "")


def clear_circuit_breaker(data_dir: Path) -> None:
    """Reset circuit breaker to normal."""
    write_circuit_breaker(data_dir, CircuitBreakerState(
        status=NORMAL,
        reason="Compatibility check passed",
        set_by="version_watcher",
        set_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    ))


# ---------------------------------------------------------------------------
# Compatibility matrix
# ---------------------------------------------------------------------------

def _matrix_cache_path(data_dir: Path) -> Path:
    return data_dir / MATRIX_CACHE_FILE


def _version_cache_path(data_dir: Path) -> Path:
    return data_dir / VERSION_CACHE_FILE


def fetch_compatibility_matrix(data_dir: Path) -> Optional[dict]:
    """Fetch the compatibility matrix from GitHub. Cache locally.

    Returns the parsed matrix dict, or None on failure (uses cache as fallback).
    """
    import urllib.request
    import urllib.error

    cache_path = _matrix_cache_path(data_dir)

    try:
        req = urllib.request.Request(MATRIX_URL, headers={"User-Agent": "quaid-compat/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
        matrix = json.loads(raw)
        # Cache it
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(raw)
        logger.debug("Fetched compatibility matrix (%d entries)", len(matrix.get("matrix", [])))
        return matrix
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as e:
        logger.debug("Failed to fetch compatibility matrix: %s", e)

    # Fall back to cache
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return None


def evaluate_compatibility(
    host_info: HostInfo,
    quaid_version: str,
    matrix: dict,
) -> CircuitBreakerState:
    """Evaluate host + quaid version against the compatibility matrix.

    Returns the appropriate circuit breaker state.
    """
    # Check global kill switch first
    if matrix.get("kill_switch"):
        return CircuitBreakerState(
            status=SAFE_MODE,
            reason="Global kill switch activated",
            set_by="compatibility_matrix",
            host_version=host_info.version,
            message=matrix.get("kill_message", "Quaid operations suspended — check for updates"),
        )

    # Find matching matrix entries
    entries = matrix.get("matrix", [])
    matched = None
    for entry in entries:
        if entry.get("host", "").lower() != host_info.platform.lower():
            continue
        host_range = entry.get("host_range", "")
        quaid_range = entry.get("quaid_range", "")
        if (_version_satisfies(host_info.version, host_range) and
                _version_satisfies(quaid_version, quaid_range)):
            matched = entry
            break

    if matched is None:
        # No matching entry — behavior depends on testing_online flag.
        # When testing is online (we're actively maintaining the matrix),
        # untested combos get warnings and accelerated rechecking.
        # When testing is offline, silence — we just don't have data yet.
        # When True, unmatched version combos get warnings and accelerated
        # rechecking. Flip this when version-testing agents are operational.
        testing_online = False
        if testing_online:
            return CircuitBreakerState(
                status=NORMAL,
                reason=f"Untested: {host_info.label()} with Quaid {quaid_version}",
                set_by="version_watcher",
                host_version=host_info.version,
                message=(
                    f"Quaid has not been tested with {host_info.label()}. "
                    "Running in untested mode. If you hit issues, check for updates."
                ),
                untested=True,
            )
        else:
            # Testing offline — no warning, no accelerated checking
            return CircuitBreakerState(
                status=NORMAL,
                reason=f"No data: {host_info.label()} with Quaid {quaid_version}",
                set_by="version_watcher",
                host_version=host_info.version,
            )

    if matched["status"] == "compatible":
        return CircuitBreakerState(
            status=NORMAL,
            reason=f"Compatible: {host_info.label()}",
            set_by="version_watcher",
            host_version=host_info.version,
        )

    # Incompatible
    data_risk = matched.get("data_risk", False)
    return CircuitBreakerState(
        status=SAFE_MODE if data_risk else DEGRADED,
        reason=f"Incompatible: {host_info.label()} — {matched.get('message', '')}",
        set_by="version_watcher",
        host_version=host_info.version,
        message=matched.get("message", f"{host_info.label()} is incompatible with Quaid {quaid_version}"),
    )


# ---------------------------------------------------------------------------
# Installer pre-flight check
# ---------------------------------------------------------------------------

def preflight_compatibility_check(
    host_platform: str,
    host_version: str,
    quaid_version: str,
    cache_dir: Optional[Path] = None,
) -> dict:
    """Pre-install compatibility check. Call from install scripts.

    Fetches the matrix from GitHub (or cache) and evaluates whether the
    given Quaid version is compatible with the host. Returns a dict:

        {"ok": True/False, "status": str, "message": str, "fix": str}

    If not ok, the installer should print the message and bail.

    Args:
        host_platform: "openclaw" or "claude-code"
        host_version: e.g. "2026.3.7"
        quaid_version: e.g. "0.2.15-alpha"
        cache_dir: optional dir for matrix cache (temp dir if None)
    """
    import tempfile

    if cache_dir is None:
        cache_dir = Path(tempfile.mkdtemp(prefix="quaid-preflight-"))

    info = HostInfo(platform=host_platform, version=host_version)
    matrix = fetch_compatibility_matrix(cache_dir)

    if matrix is None:
        # Can't fetch matrix — allow install with warning
        return {
            "ok": True,
            "status": "unknown",
            "message": (
                f"Could not fetch compatibility matrix. "
                f"Installing Quaid {quaid_version} for {info.label()} in untested mode."
            ),
            "fix": "",
        }

    # Check global kill switch
    if matrix.get("kill_switch"):
        return {
            "ok": False,
            "status": "kill_switch",
            "message": matrix.get("kill_message", "Quaid installations are currently suspended."),
            "fix": "Check https://github.com/Quaid-Labs/quaid for status updates.",
        }

    state = evaluate_compatibility(info, quaid_version, matrix)

    if state.status == SAFE_MODE:
        # Find the matching entry to get the fix field
        fix = ""
        for entry in matrix.get("matrix", []):
            if (entry.get("host", "").lower() == host_platform.lower() and
                    _version_satisfies(host_version, entry.get("host_range", "")) and
                    _version_satisfies(quaid_version, entry.get("quaid_range", ""))):
                fix = entry.get("fix", "")
                break
        return {
            "ok": False,
            "status": "incompatible",
            "message": (
                f"Quaid {quaid_version} is incompatible with {info.label()} "
                f"and may corrupt data. {state.message or ''}"
            ),
            "fix": fix or f"Update your host platform or check for a newer Quaid version.",
        }

    if state.status == DEGRADED:
        # Allow install but warn
        return {
            "ok": True,
            "status": "degraded",
            "message": (
                f"Warning: Quaid {quaid_version} has limited compatibility with "
                f"{info.label()}. {state.message or ''} "
                "Installing anyway — extraction/storage may be disabled."
            ),
            "fix": "",
        }

    return {
        "ok": True,
        "status": "compatible",
        "message": f"Quaid {quaid_version} is compatible with {info.label()}.",
        "fix": "",
    }


# ---------------------------------------------------------------------------
# Version watcher — integrates into daemon tick cycle
# ---------------------------------------------------------------------------

class VersionWatcher:
    """Watches host binary mtime and periodically checks compatibility.

    Usage in daemon loop:
        watcher = VersionWatcher(adapter, data_dir, quaid_version)
        # On every tick:
        watcher.tick()
    """

    def __init__(self, data_dir: Path, quaid_version: str):
        self._data_dir = data_dir
        self._quaid_version = quaid_version
        self._last_binary_mtime: Optional[float] = None
        self._last_full_check: float = 0.0
        self._binary_path: Optional[Path] = None
        self._host_info: Optional[HostInfo] = None
        self._last_state: Optional[CircuitBreakerState] = None

        # Load cached version info
        cache = _version_cache_path(data_dir)
        if cache.exists():
            try:
                raw = json.loads(cache.read_text())
                self._last_binary_mtime = raw.get("binary_mtime")
                self._last_full_check = raw.get("last_full_check", 0.0)
                self._host_info = HostInfo(
                    platform=raw.get("platform", "unknown"),
                    version=raw.get("version", "unknown"),
                    binary_path=raw.get("binary_path"),
                )
            except (json.JSONDecodeError, OSError):
                pass

        # Seed last state from circuit breaker on disk
        self._last_state = read_circuit_breaker(data_dir)

    def _check_interval(self) -> int:
        """Return the appropriate check interval based on current state.

        Shorter intervals when state is uncertain or bad, so we pick up
        matrix updates (e.g. new compatible entry) quickly.
        """
        if self._last_state is None:
            return CHECK_INTERVAL_UNTESTED
        if self._last_state.status == SAFE_MODE:
            return CHECK_INTERVAL_SAFE_MODE
        if self._last_state.status == DEGRADED:
            return CHECK_INTERVAL_DEGRADED
        if self._last_state.untested:
            return CHECK_INTERVAL_UNTESTED
        return CHECK_INTERVAL_NORMAL

    def tick(self) -> None:
        """Called on every daemon tick. Cheap mtime check, full check when needed."""
        # Lazy-resolve host info on first tick
        if self._host_info is None:
            self._do_full_check()
            return

        # Cheap mtime check on binary
        binary_path = self._host_info.binary_path
        if binary_path:
            try:
                current_mtime = os.stat(binary_path).st_mtime
                if (self._last_binary_mtime is not None and
                        current_mtime != self._last_binary_mtime):
                    logger.info(
                        "Host binary mtime changed (%s), running version check",
                        binary_path,
                    )
                    self._do_full_check()
                    return
                self._last_binary_mtime = current_mtime
            except OSError:
                pass  # Binary not accessible, skip mtime check

        # Periodic check — interval adapts to current state
        interval = self._check_interval()
        if time.time() - self._last_full_check > interval:
            logger.debug(
                "Periodic compatibility check (interval=%ds, state=%s)",
                interval, self._last_state.status if self._last_state else "unknown",
            )
            self._do_full_check()

    def _do_full_check(self) -> None:
        """Full version check: get host version, fetch matrix, evaluate."""
        from lib.adapter import get_adapter

        try:
            adapter = get_adapter()
            info = adapter.get_host_info()
        except Exception as e:
            logger.warning("Failed to get host info: %s", e)
            return

        self._host_info = info
        if info.binary_path:
            try:
                self._last_binary_mtime = os.stat(info.binary_path).st_mtime
            except OSError:
                self._last_binary_mtime = None

        self._last_full_check = time.time()

        # Cache version info
        self._save_version_cache()

        # Fetch and evaluate matrix
        matrix = fetch_compatibility_matrix(self._data_dir)
        if matrix is None:
            logger.debug("No compatibility matrix available, skipping evaluation")
            return

        state = evaluate_compatibility(info, self._quaid_version, matrix)
        self._last_state = state

        # Apply circuit breaker and notify on state changes
        current = read_circuit_breaker(self._data_dir)
        if state.status != current.status or state.reason != current.reason:
            old_status = current.status
            write_circuit_breaker(self._data_dir, state)

            if state.status != NORMAL:
                logger.warning(
                    "Compatibility: %s — %s",
                    state.status, state.message or state.reason,
                )
            else:
                logger.info("Compatibility: %s", state.reason or "OK")

            # Notify user on state transitions
            self._notify_state_change(old_status, state)

        # Check for Quaid update availability
        self._check_quaid_update(matrix)

    def _check_quaid_update(self, matrix: dict) -> None:
        """Notify user if a newer Quaid version is available."""
        latest = matrix.get("latest_quaid")
        if not latest:
            return

        current = _parse_version(self._quaid_version)
        available = _parse_version(latest)
        if available <= current:
            return  # Already up to date

        # Check if we already notified about this version (don't spam)
        update_cache = self._data_dir / "quaid-update-notified.json"
        try:
            if update_cache.exists():
                raw = json.loads(update_cache.read_text())
                if raw.get("version") == latest:
                    return  # Already notified for this version
        except (json.JSONDecodeError, OSError):
            pass

        update_msg = matrix.get("update_message") or (
            f"Quaid {latest} is available (you have {self._quaid_version}). "
            "Update for latest fixes and compatibility."
        )

        logger.info("Quaid update available: %s (current: %s)", latest, self._quaid_version)

        try:
            from lib.adapter import get_adapter
            get_adapter().notify(f"[Quaid] {update_msg}", force=True)
        except Exception as e:
            logger.debug("Failed to send update notification: %s", e)

        # Record that we notified
        try:
            update_cache.parent.mkdir(parents=True, exist_ok=True)
            update_cache.write_text(json.dumps({"version": latest, "notified_at": time.time()}))
        except OSError:
            pass

    def _save_version_cache(self) -> None:
        """Persist version info to disk."""
        if self._host_info is None:
            return
        cache = _version_cache_path(self._data_dir)
        cache.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "platform": self._host_info.platform,
            "version": self._host_info.version,
            "binary_path": self._host_info.binary_path,
            "binary_mtime": self._last_binary_mtime,
            "last_full_check": self._last_full_check,
        }
        cache.write_text(json.dumps(payload, indent=2) + "\n")

    def _notify_state_change(self, old_status: str, new_state: CircuitBreakerState) -> None:
        """Send a direct notification to user on circuit breaker state changes.

        Notification policy:
        - normal → degraded/safe_mode: direct push (user needs to know)
        - degraded/safe_mode → normal: direct push (good news, recovery)
        - kill switch activated: direct push (emergency)
        - untested version (normal with warning): log only, no push
        """
        if old_status == new_state.status:
            return  # No actual state change

        try:
            from lib.adapter import get_adapter
            adapter = get_adapter()
        except Exception:
            return  # Can't notify if adapter is unavailable

        if new_state.status in (DEGRADED, SAFE_MODE):
            # Entering degraded or safe mode
            mode_label = "DEGRADED MODE" if new_state.status == DEGRADED else "SAFE MODE"
            msg = (
                f"[Quaid] {mode_label} activated. "
                f"{new_state.message or new_state.reason or 'Compatibility issue detected.'}"
            )
            if new_state.status == DEGRADED:
                msg += " Recall still works, but extraction and storage are paused."
            else:
                msg += " All operations paused to prevent data corruption."
            try:
                adapter.notify(msg, force=True)
            except Exception as e:
                logger.warning("Failed to send compatibility notification: %s", e)

        elif old_status in (DEGRADED, SAFE_MODE) and new_state.status == NORMAL:
            # Recovery — back to normal
            try:
                adapter.notify(
                    "[Quaid] Compatibility restored — all operations resumed. "
                    f"{new_state.reason or ''}",
                    force=True,
                )
            except Exception as e:
                logger.warning("Failed to send recovery notification: %s", e)


# ---------------------------------------------------------------------------
# Session-level notification for ongoing degraded state
# ---------------------------------------------------------------------------

NOTIFICATION_COOLDOWN_FILE = "compat-last-notified.json"


def notify_on_use_if_degraded(data_dir: Path) -> Optional[str]:
    """Check if we should notify the user about degraded state on this use.

    Call this from session-init hooks or recall entry points. Returns the
    warning message if the user should be informed, None otherwise.

    Notification policy for ongoing degraded/safe state:
    - Notifies once per session (tracked by cooldown file with session timestamp)
    - Returns the message text for the hook/caller to include in output
    """
    breaker = read_circuit_breaker(data_dir)
    if breaker.is_normal():
        return None

    # Check cooldown — don't repeat within the same session (~30 min window)
    cooldown_path = data_dir / NOTIFICATION_COOLDOWN_FILE
    now = time.time()
    try:
        if cooldown_path.exists():
            raw = json.loads(cooldown_path.read_text())
            last_notified = raw.get("timestamp", 0)
            if now - last_notified < 1800:  # 30 minutes
                return None
    except (json.JSONDecodeError, OSError):
        pass

    # Update cooldown
    try:
        cooldown_path.parent.mkdir(parents=True, exist_ok=True)
        cooldown_path.write_text(json.dumps({"timestamp": now}))
    except OSError:
        pass

    if breaker.status == SAFE_MODE:
        return (
            f"Quaid is in SAFE MODE: {breaker.message or 'Compatibility issue detected.'} "
            "All operations are paused to prevent data corruption. "
            "Check for Quaid updates."
        )
    elif breaker.status == DEGRADED:
        return (
            f"Quaid is in DEGRADED MODE: {breaker.message or 'Compatibility issue detected.'} "
            "Recall works, but extraction and storage are paused. "
            "Check for Quaid updates."
        )
    return None


# ---------------------------------------------------------------------------
# Janitor scheduler — daemon-owned periodic maintenance
# ---------------------------------------------------------------------------

JANITOR_CHECKPOINT_FILE = "logs/janitor/checkpoint-all.json"
JANITOR_DEFAULT_HOUR = 4        # 4 AM local time
JANITOR_WINDOW_HOURS = 2        # 2-hour window (3am-5am for default)
JANITOR_CHECK_INTERVAL = 900    # Check eligibility every 15 minutes


class JanitorScheduler:
    """Daemon-owned janitor scheduling. Replaces external cron/heartbeat.

    Runs janitor at a configured hour of day (default 4am) within a
    configurable window. If the daemon was down during the window, catches
    up on next boot if the checkpoint is stale (>24h old).

    Config keys (in config/memory.json under "janitor"):
    - scheduled_hour: int (0-23, default 4)
    - window_hours: int (default 2)

    Respects circuit breaker (no janitor in degraded/safe mode).
    """

    def __init__(self, data_dir: Path, quaid_home: Path,
                 scheduled_hour: Optional[int] = None,
                 window_hours: Optional[int] = None):
        self._data_dir = data_dir
        self._quaid_home = quaid_home
        self._scheduled_hour = scheduled_hour  # Resolved lazily from config
        self._window_hours = window_hours
        self._last_tick: float = 0.0
        self._ran_today: bool = False
        self._today_date: Optional[str] = None

    def _get_schedule(self) -> tuple:
        """Get scheduled hour and window from config or defaults."""
        hour = self._scheduled_hour
        window = self._window_hours

        if hour is None or window is None:
            try:
                from config import get_config
                cfg = get_config()
                janitor_cfg = getattr(cfg, "janitor", None)
                if janitor_cfg:
                    if hour is None:
                        hour = getattr(janitor_cfg, "scheduled_hour", JANITOR_DEFAULT_HOUR)
                    if window is None:
                        window = getattr(janitor_cfg, "window_hours", JANITOR_WINDOW_HOURS)
            except Exception:
                pass

        return (
            hour if hour is not None else JANITOR_DEFAULT_HOUR,
            window if window is not None else JANITOR_WINDOW_HOURS,
        )

    def tick(self) -> None:
        """Called on every daemon tick. Checks if janitor is due."""
        import datetime

        now = time.time()

        # Only evaluate every 15 minutes (no need to check more often)
        if now - self._last_tick < JANITOR_CHECK_INTERVAL:
            return
        self._last_tick = now

        # Reset daily tracking
        today = datetime.date.today().isoformat()
        if self._today_date != today:
            self._today_date = today
            self._ran_today = False

        if self._ran_today:
            return

        # Don't run janitor if circuit breaker is tripped
        breaker = read_circuit_breaker(self._data_dir)
        if not breaker.allows_writes():
            logger.debug("Janitor skipped: circuit breaker is %s", breaker.status)
            return

        scheduled_hour, window = self._get_schedule()
        current_hour = datetime.datetime.now().hour

        # Check if we're in the scheduled window (handles midnight wrap)
        window_start = scheduled_hour - (window // 2)
        window_end = scheduled_hour + (window - window // 2)
        if window_start < 0 or window_end > 23:
            # Window wraps around midnight — normalize to 0-23
            in_window = current_hour >= (window_start % 24) or current_hour < (window_end % 24)
        else:
            in_window = window_start <= current_hour < window_end

        # Also check for catch-up: if checkpoint is >24h old, run regardless
        # of window (daemon may have been down during the window)
        needs_catchup = False
        checkpoint = self._quaid_home / JANITOR_CHECKPOINT_FILE
        if checkpoint.exists():
            try:
                age = now - checkpoint.stat().st_mtime
                if age < 86400:
                    return  # Ran within 24h, no action needed
                needs_catchup = True
            except OSError:
                needs_catchup = True
        else:
            needs_catchup = True  # Never ran

        if not in_window and not needs_catchup:
            return

        reason = "catch-up (missed window)" if needs_catchup and not in_window else "scheduled"
        logger.info(
            "Janitor %s — hour=%d, window=%d-%d, running maintenance",
            reason, scheduled_hour, window_start, window_end,
        )
        self._run_janitor()
        self._ran_today = True

    def _run_janitor(self) -> None:
        """Run janitor maintenance in-process."""
        try:
            from core.lifecycle.janitor import run_task_optimized
            run_task_optimized(
                task="all",
                dry_run=False,
            )
            logger.info("Janitor maintenance completed successfully")
        except Exception as e:
            logger.error("Janitor maintenance failed: %s", e, exc_info=True)
