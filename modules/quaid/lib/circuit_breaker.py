"""Circuit breaker primitives for Quaid operation guards.

Provides read/write operation guards that check the circuit breaker file
written by the compatibility watcher. Lives in lib so both datastore and
core modules can import it without violating subsystem boundaries.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

NORMAL = "normal"
DEGRADED = "degraded"       # Extraction/storage disabled, recall works
SAFE_MODE = "safe_mode"     # All operations disabled

CIRCUIT_BREAKER_FILE = "circuit-breaker.json"


@dataclass
class CircuitBreakerState:
    """Current circuit breaker state."""
    status: str = NORMAL
    reason: Optional[str] = None
    set_by: Optional[str] = None
    set_at: Optional[str] = None
    host_version: Optional[str] = None
    message: Optional[str] = None
    untested: bool = False  # True when no matrix entry matched (unknown combo)

    def is_normal(self) -> bool:
        return self.status == NORMAL

    def allows_writes(self) -> bool:
        """Can we store/extract/update?"""
        return self.status == NORMAL

    def allows_reads(self) -> bool:
        """Can we recall/search?"""
        return self.status in (NORMAL, DEGRADED)


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
            untested=raw.get("untested", False),
        )
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read circuit breaker: %s", e)
        return CircuitBreakerState()


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
