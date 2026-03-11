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

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Circuit breaker states
# ---------------------------------------------------------------------------

NORMAL = "normal"
DEGRADED = "degraded"       # Extraction/storage disabled, recall works
SAFE_MODE = "safe_mode"     # All operations disabled

CIRCUIT_BREAKER_FILE = "circuit-breaker.json"
VERSION_CACHE_FILE = "host-version.json"
MATRIX_CACHE_FILE = "compatibility-matrix.json"

# GitHub raw URL for the compatibility matrix
MATRIX_URL = (
    "https://raw.githubusercontent.com/Quaid-Labs/quaid/main/compatibility.json"
)

# How often to do a full version + matrix check even without mtime change
FULL_CHECK_INTERVAL_SECONDS = 86400  # 24 hours


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class HostInfo:
    """Information about the host platform."""
    platform: str           # "openclaw", "claude-code", "standalone"
    version: str            # "2026.3.7", "2.1.72", etc.
    binary_path: Optional[str] = None  # Path to the host binary (for mtime)

    def label(self) -> str:
        return f"{self.platform} {self.version}"


@dataclass
class CircuitBreakerState:
    """Current circuit breaker state."""
    status: str = NORMAL
    reason: Optional[str] = None
    set_by: Optional[str] = None
    set_at: Optional[str] = None
    host_version: Optional[str] = None
    message: Optional[str] = None

    def is_normal(self) -> bool:
        return self.status == NORMAL

    def allows_writes(self) -> bool:
        """Can we store/extract/update?"""
        return self.status == NORMAL

    def allows_reads(self) -> bool:
        """Can we recall/search?"""
        return self.status in (NORMAL, DEGRADED)


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
# ---------------------------------------------------------------------------

def _breaker_path(data_dir: Path) -> Path:
    return data_dir / CIRCUIT_BREAKER_FILE


def read_circuit_breaker(data_dir: Path) -> CircuitBreakerState:
    """Read the current circuit breaker state. Returns NORMAL if no file."""
    p = _breaker_path(data_dir)
    if not p.exists():
        return CircuitBreakerState()
    try:
        raw = json.loads(p.read_text())
        return CircuitBreakerState(
            status=raw.get("status", NORMAL),
            reason=raw.get("reason"),
            set_by=raw.get("set_by"),
            set_at=raw.get("set_at"),
            host_version=raw.get("host_version"),
            message=raw.get("message"),
        )
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read circuit breaker: %s", e)
        return CircuitBreakerState()


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
# Entry point guards — call these at the top of critical operations
# ---------------------------------------------------------------------------

def check_write_allowed(data_dir: Path) -> CircuitBreakerState:
    """Check if write operations (extract, store, update) are allowed.

    Returns the state. Caller should check state.allows_writes() and use
    state.message for user-facing error text.
    """
    return read_circuit_breaker(data_dir)


def check_read_allowed(data_dir: Path) -> CircuitBreakerState:
    """Check if read operations (recall, search) are allowed.

    Returns the state. Caller should check state.allows_reads().
    """
    return read_circuit_breaker(data_dir)


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
        # No matching entry — unknown combination, warn but allow
        return CircuitBreakerState(
            status=NORMAL,
            reason=f"Untested: {host_info.label()} with Quaid {quaid_version}",
            set_by="version_watcher",
            host_version=host_info.version,
            message=(
                f"Quaid has not been tested with {host_info.label()}. "
                "Running in untested mode. If you hit issues, check for updates."
            ),
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

        # Periodic full check (24h)
        if time.time() - self._last_full_check > FULL_CHECK_INTERVAL_SECONDS:
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

        # Apply circuit breaker
        current = read_circuit_breaker(self._data_dir)
        if state.status != current.status or state.reason != current.reason:
            write_circuit_breaker(self._data_dir, state)
            if state.status != NORMAL:
                logger.warning(
                    "Compatibility: %s — %s",
                    state.status, state.message or state.reason,
                )
            else:
                logger.info("Compatibility: %s", state.reason or "OK")

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


# ---------------------------------------------------------------------------
# Janitor scheduler — daemon-owned periodic maintenance
# ---------------------------------------------------------------------------

JANITOR_DEFAULT_INTERVAL_SECONDS = 86400  # 24 hours
JANITOR_CHECKPOINT_FILE = "logs/janitor/checkpoint-all.json"


class JanitorScheduler:
    """Daemon-owned janitor scheduling. Replaces external cron/heartbeat.

    Checks if janitor has run within the configured interval. If not,
    triggers a maintenance run. Respects circuit breaker (no janitor in
    degraded/safe mode).
    """

    def __init__(self, data_dir: Path, quaid_home: Path,
                 interval_seconds: float = JANITOR_DEFAULT_INTERVAL_SECONDS):
        self._data_dir = data_dir
        self._quaid_home = quaid_home
        self._interval = interval_seconds
        self._last_check: float = 0.0

    def tick(self) -> None:
        """Called on every daemon tick. Checks if janitor is due."""
        now = time.time()

        # Only check once per interval (don't spam stat() on checkpoint file)
        if now - self._last_check < min(self._interval, 3600):
            return
        self._last_check = now

        # Don't run janitor if circuit breaker is tripped
        breaker = read_circuit_breaker(self._data_dir)
        if not breaker.allows_writes():
            logger.debug("Janitor skipped: circuit breaker is %s", breaker.status)
            return

        # Check if janitor has run recently
        checkpoint = self._quaid_home / JANITOR_CHECKPOINT_FILE
        if checkpoint.exists():
            try:
                age = now - checkpoint.stat().st_mtime
                if age < self._interval:
                    return  # Ran recently, nothing to do
            except OSError:
                pass

        # Time to run janitor
        logger.info("Janitor due (interval=%ds), triggering maintenance run", self._interval)
        self._run_janitor()

    def _run_janitor(self) -> None:
        """Run janitor maintenance in-process."""
        try:
            from core.lifecycle.janitor import run_maintenance
            run_maintenance(
                task="all",
                apply_mode=True,
                dry_run=False,
            )
            logger.info("Janitor maintenance completed successfully")
        except Exception as e:
            logger.error("Janitor maintenance failed: %s", e, exc_info=True)
