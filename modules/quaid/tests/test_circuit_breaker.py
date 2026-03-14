"""Unit tests for lib/circuit_breaker.py.

Covers CircuitBreakerState dataclass, read_circuit_breaker(), check_write_allowed(),
and check_read_allowed() across all three states: NORMAL, DEGRADED, SAFE_MODE.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.circuit_breaker import (
    CIRCUIT_BREAKER_FILE,
    DEGRADED,
    NORMAL,
    SAFE_MODE,
    CircuitBreakerState,
    check_read_allowed,
    check_write_allowed,
    read_circuit_breaker,
)


# ---------------------------------------------------------------------------
# CircuitBreakerState — dataclass methods
# ---------------------------------------------------------------------------


class TestCircuitBreakerStateDefaults:
    def test_default_status_is_normal(self):
        state = CircuitBreakerState()
        assert state.status == NORMAL

    def test_default_fields_are_none(self):
        state = CircuitBreakerState()
        assert state.reason is None
        assert state.set_by is None
        assert state.set_at is None
        assert state.host_version is None
        assert state.message is None

    def test_default_untested_is_false(self):
        state = CircuitBreakerState()
        assert state.untested is False


class TestCircuitBreakerStateNormal:
    def test_is_normal_returns_true(self):
        state = CircuitBreakerState(status=NORMAL)
        assert state.is_normal() is True

    def test_allows_writes(self):
        state = CircuitBreakerState(status=NORMAL)
        assert state.allows_writes() is True

    def test_allows_reads(self):
        state = CircuitBreakerState(status=NORMAL)
        assert state.allows_reads() is True


class TestCircuitBreakerStateDegraded:
    def test_is_normal_returns_false(self):
        state = CircuitBreakerState(status=DEGRADED)
        assert state.is_normal() is False

    def test_does_not_allow_writes(self):
        state = CircuitBreakerState(status=DEGRADED)
        assert state.allows_writes() is False

    def test_allows_reads(self):
        """DEGRADED still allows reads — recall works, extraction disabled."""
        state = CircuitBreakerState(status=DEGRADED)
        assert state.allows_reads() is True


class TestCircuitBreakerStateSafeMode:
    def test_is_normal_returns_false(self):
        state = CircuitBreakerState(status=SAFE_MODE)
        assert state.is_normal() is False

    def test_does_not_allow_writes(self):
        state = CircuitBreakerState(status=SAFE_MODE)
        assert state.allows_writes() is False

    def test_does_not_allow_reads(self):
        """SAFE_MODE disables all operations."""
        state = CircuitBreakerState(status=SAFE_MODE)
        assert state.allows_reads() is False


# ---------------------------------------------------------------------------
# read_circuit_breaker — file missing
# ---------------------------------------------------------------------------


class TestReadCircuitBreakerMissingFile:
    def test_returns_normal_when_no_file(self, tmp_path):
        state = read_circuit_breaker(tmp_path)
        assert state.status == NORMAL

    def test_returned_state_allows_writes_when_no_file(self, tmp_path):
        state = read_circuit_breaker(tmp_path)
        assert state.allows_writes() is True

    def test_returned_state_allows_reads_when_no_file(self, tmp_path):
        state = read_circuit_breaker(tmp_path)
        assert state.allows_reads() is True


# ---------------------------------------------------------------------------
# read_circuit_breaker — normal JSON file
# ---------------------------------------------------------------------------


class TestReadCircuitBreakerNormalFile:
    def _write(self, tmp_path, data):
        (tmp_path / CIRCUIT_BREAKER_FILE).write_text(json.dumps(data))

    def test_reads_normal_status(self, tmp_path):
        self._write(tmp_path, {"status": NORMAL})
        state = read_circuit_breaker(tmp_path)
        assert state.status == NORMAL

    def test_reads_degraded_status(self, tmp_path):
        self._write(tmp_path, {"status": DEGRADED, "reason": "schema drift"})
        state = read_circuit_breaker(tmp_path)
        assert state.status == DEGRADED
        assert state.reason == "schema drift"

    def test_reads_safe_mode_status(self, tmp_path):
        self._write(tmp_path, {"status": SAFE_MODE, "message": "all ops disabled"})
        state = read_circuit_breaker(tmp_path)
        assert state.status == SAFE_MODE
        assert state.message == "all ops disabled"

    def test_reads_all_fields(self, tmp_path):
        data = {
            "status": DEGRADED,
            "reason": "bad schema",
            "set_by": "compat-watcher",
            "set_at": "2026-03-14T10:00:00Z",
            "host_version": "3.11",
            "message": "Extraction disabled",
            "untested": True,
        }
        self._write(tmp_path, data)
        state = read_circuit_breaker(tmp_path)
        assert state.reason == "bad schema"
        assert state.set_by == "compat-watcher"
        assert state.set_at == "2026-03-14T10:00:00Z"
        assert state.host_version == "3.11"
        assert state.message == "Extraction disabled"
        assert state.untested is True

    def test_missing_status_key_defaults_to_normal(self, tmp_path):
        self._write(tmp_path, {"reason": "something"})
        state = read_circuit_breaker(tmp_path)
        assert state.status == NORMAL

    def test_missing_untested_key_defaults_to_false(self, tmp_path):
        self._write(tmp_path, {"status": DEGRADED})
        state = read_circuit_breaker(tmp_path)
        assert state.untested is False


# ---------------------------------------------------------------------------
# read_circuit_breaker — malformed file
# ---------------------------------------------------------------------------


class TestReadCircuitBreakerBadFile:
    def test_invalid_json_returns_normal(self, tmp_path):
        (tmp_path / CIRCUIT_BREAKER_FILE).write_text("not json {{{")
        state = read_circuit_breaker(tmp_path)
        assert state.status == NORMAL

    def test_empty_file_returns_normal(self, tmp_path):
        (tmp_path / CIRCUIT_BREAKER_FILE).write_text("")
        state = read_circuit_breaker(tmp_path)
        assert state.status == NORMAL

    def test_unreadable_file_returns_normal(self, tmp_path):
        """Simulate OSError by making the file unreadable."""
        p = tmp_path / CIRCUIT_BREAKER_FILE
        p.write_text(json.dumps({"status": DEGRADED}))
        p.chmod(0o000)
        try:
            state = read_circuit_breaker(tmp_path)
            assert state.status == NORMAL
        finally:
            p.chmod(0o644)


# ---------------------------------------------------------------------------
# check_write_allowed / check_read_allowed — thin wrappers
# ---------------------------------------------------------------------------


class TestCheckAllowedWrappers:
    def _write(self, tmp_path, data):
        (tmp_path / CIRCUIT_BREAKER_FILE).write_text(json.dumps(data))

    def test_check_write_allowed_normal(self, tmp_path):
        state = check_write_allowed(tmp_path)
        assert state.allows_writes() is True

    def test_check_write_allowed_degraded(self, tmp_path):
        self._write(tmp_path, {"status": DEGRADED})
        state = check_write_allowed(tmp_path)
        assert state.allows_writes() is False

    def test_check_write_allowed_safe_mode(self, tmp_path):
        self._write(tmp_path, {"status": SAFE_MODE})
        state = check_write_allowed(tmp_path)
        assert state.allows_writes() is False

    def test_check_read_allowed_normal(self, tmp_path):
        state = check_read_allowed(tmp_path)
        assert state.allows_reads() is True

    def test_check_read_allowed_degraded(self, tmp_path):
        self._write(tmp_path, {"status": DEGRADED})
        state = check_read_allowed(tmp_path)
        assert state.allows_reads() is True

    def test_check_read_allowed_safe_mode(self, tmp_path):
        self._write(tmp_path, {"status": SAFE_MODE})
        state = check_read_allowed(tmp_path)
        assert state.allows_reads() is False

    def test_both_wrappers_return_same_state(self, tmp_path):
        self._write(tmp_path, {"status": DEGRADED, "reason": "drift"})
        write_state = check_write_allowed(tmp_path)
        read_state = check_read_allowed(tmp_path)
        assert write_state.status == read_state.status
        assert write_state.reason == read_state.reason


# ---------------------------------------------------------------------------
# State transitions across writes
# ---------------------------------------------------------------------------


class TestStateTransitions:
    """Verify that re-reading after file changes picks up the new state."""

    def test_normal_to_degraded(self, tmp_path):
        p = tmp_path / CIRCUIT_BREAKER_FILE
        p.write_text(json.dumps({"status": NORMAL}))
        assert read_circuit_breaker(tmp_path).status == NORMAL

        p.write_text(json.dumps({"status": DEGRADED}))
        assert read_circuit_breaker(tmp_path).status == DEGRADED

    def test_degraded_to_safe_mode(self, tmp_path):
        p = tmp_path / CIRCUIT_BREAKER_FILE
        p.write_text(json.dumps({"status": DEGRADED}))
        assert read_circuit_breaker(tmp_path).allows_reads() is True

        p.write_text(json.dumps({"status": SAFE_MODE}))
        state = read_circuit_breaker(tmp_path)
        assert state.allows_reads() is False
        assert state.allows_writes() is False

    def test_safe_mode_to_normal_recovery(self, tmp_path):
        p = tmp_path / CIRCUIT_BREAKER_FILE
        p.write_text(json.dumps({"status": SAFE_MODE}))
        assert read_circuit_breaker(tmp_path).allows_reads() is False

        p.unlink()
        state = read_circuit_breaker(tmp_path)
        assert state.status == NORMAL
        assert state.allows_reads() is True
        assert state.allows_writes() is True
