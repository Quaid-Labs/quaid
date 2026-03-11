"""Tests for core/compatibility.py — version watcher, circuit breaker, matrix evaluation."""

import json
import os
import time
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.compatibility import (
    NORMAL, DEGRADED, SAFE_MODE,
    CHECK_INTERVAL_NORMAL, CHECK_INTERVAL_UNTESTED,
    CHECK_INTERVAL_DEGRADED, CHECK_INTERVAL_SAFE_MODE,
    HostInfo, CircuitBreakerState,
    _parse_version, _version_satisfies,
    read_circuit_breaker, write_circuit_breaker, clear_circuit_breaker,
    evaluate_compatibility,
    VersionWatcher, JanitorScheduler,
)


class TestSemver:
    def test_parse_version(self):
        assert _parse_version("2026.3.7") == (2026, 3, 7)
        assert _parse_version("2.1.72") == (2, 1, 72)
        assert _parse_version("0.2.15-alpha") == (0, 2, 15)
        assert _parse_version("unknown") == (0,)

    def test_satisfies_gte(self):
        assert _version_satisfies("2026.3.7", ">=2026.3.0")
        assert _version_satisfies("2026.3.0", ">=2026.3.0")
        assert not _version_satisfies("2026.2.9", ">=2026.3.0")

    def test_satisfies_lt(self):
        assert _version_satisfies("2026.2.9", "<2026.3.0")
        assert not _version_satisfies("2026.3.0", "<2026.3.0")

    def test_satisfies_range(self):
        assert _version_satisfies("2026.3.5", ">=2026.3.0 <2026.5.0")
        assert not _version_satisfies("2026.5.0", ">=2026.3.0 <2026.5.0")
        assert not _version_satisfies("2026.2.0", ">=2026.3.0 <2026.5.0")

    def test_satisfies_alpha_version(self):
        assert _version_satisfies("0.2.15-alpha", ">=0.2.0")
        assert not _version_satisfies("0.1.0", ">=0.2.0")


class TestCircuitBreaker:
    def test_read_missing_file(self, tmp_path):
        state = read_circuit_breaker(tmp_path)
        assert state.is_normal()
        assert state.allows_writes()
        assert state.allows_reads()

    def test_write_and_read(self, tmp_path):
        state = CircuitBreakerState(
            status=DEGRADED,
            reason="Test degraded",
            set_by="test",
            message="Testing degraded mode",
        )
        write_circuit_breaker(tmp_path, state)

        loaded = read_circuit_breaker(tmp_path)
        assert loaded.status == DEGRADED
        assert loaded.reason == "Test degraded"
        assert not loaded.allows_writes()
        assert loaded.allows_reads()

    def test_safe_mode_blocks_everything(self, tmp_path):
        state = CircuitBreakerState(status=SAFE_MODE, reason="Emergency")
        write_circuit_breaker(tmp_path, state)

        loaded = read_circuit_breaker(tmp_path)
        assert not loaded.allows_writes()
        assert not loaded.allows_reads()

    def test_clear_resets_to_normal(self, tmp_path):
        write_circuit_breaker(tmp_path, CircuitBreakerState(status=SAFE_MODE))
        clear_circuit_breaker(tmp_path)

        loaded = read_circuit_breaker(tmp_path)
        assert loaded.is_normal()

    def test_corrupt_file_returns_normal(self, tmp_path):
        (tmp_path / "circuit-breaker.json").write_text("not json!")
        state = read_circuit_breaker(tmp_path)
        assert state.is_normal()


class TestEvaluateCompatibility:
    def _matrix(self, entries=None, kill_switch=False, kill_message=None):
        return {
            "kill_switch": kill_switch,
            "kill_message": kill_message,
            "matrix": entries or [],
        }

    def test_kill_switch(self):
        info = HostInfo(platform="openclaw", version="2026.3.7")
        matrix = self._matrix(kill_switch=True, kill_message="Emergency shutdown")
        state = evaluate_compatibility(info, "0.2.15", matrix)
        assert state.status == SAFE_MODE
        assert "Emergency" in state.message

    def test_compatible_entry(self):
        info = HostInfo(platform="openclaw", version="2026.3.7")
        matrix = self._matrix(entries=[{
            "host": "openclaw",
            "host_range": ">=2026.3.0",
            "quaid_range": ">=0.2.0",
            "status": "compatible",
        }])
        state = evaluate_compatibility(info, "0.2.15", matrix)
        assert state.is_normal()
        assert "Compatible" in state.reason

    def test_incompatible_no_data_risk(self):
        info = HostInfo(platform="openclaw", version="2026.5.0")
        matrix = self._matrix(entries=[{
            "host": "openclaw",
            "host_range": ">=2026.5.0",
            "quaid_range": "<0.3.0",
            "status": "incompatible",
            "data_risk": False,
            "message": "API changed",
        }])
        state = evaluate_compatibility(info, "0.2.15", matrix)
        assert state.status == DEGRADED
        assert "API changed" in state.message

    def test_incompatible_with_data_risk(self):
        info = HostInfo(platform="openclaw", version="2026.5.0")
        matrix = self._matrix(entries=[{
            "host": "openclaw",
            "host_range": ">=2026.5.0",
            "quaid_range": "<0.3.0",
            "status": "incompatible",
            "data_risk": True,
            "message": "Session format changed, may corrupt data",
        }])
        state = evaluate_compatibility(info, "0.2.15", matrix)
        assert state.status == SAFE_MODE

    def test_unknown_version_silent_by_default(self):
        """With testing_online=False (hardcoded default), no warnings."""
        info = HostInfo(platform="openclaw", version="2099.1.0")
        matrix = self._matrix(entries=[{
            "host": "openclaw",
            "host_range": ">=2026.3.0 <2026.5.0",
            "quaid_range": ">=0.2.0",
            "status": "compatible",
        }])
        state = evaluate_compatibility(info, "0.2.15", matrix)
        assert state.is_normal()
        assert state.untested is False
        assert "No data" in state.reason

    def test_wrong_platform_silent_by_default(self):
        info = HostInfo(platform="claude-code", version="2.1.72")
        matrix = self._matrix(entries=[{
            "host": "openclaw",
            "host_range": ">=2026.3.0",
            "quaid_range": ">=0.2.0",
            "status": "compatible",
        }])
        state = evaluate_compatibility(info, "0.2.15", matrix)
        assert state.is_normal()
        assert state.untested is False

    def test_empty_matrix_silent_by_default(self):
        info = HostInfo(platform="openclaw", version="2026.3.7")
        matrix = self._matrix(entries=[])
        state = evaluate_compatibility(info, "0.2.15", matrix)
        assert state.is_normal()
        assert state.untested is False


class TestHostInfo:
    def test_label(self):
        info = HostInfo(platform="openclaw", version="2026.3.7")
        assert info.label() == "openclaw 2026.3.7"


class TestVersionWatcher:
    def test_tick_without_host_info_triggers_full_check(self, tmp_path):
        watcher = VersionWatcher(data_dir=tmp_path, quaid_version="0.2.15")

        mock_adapter = MagicMock()
        mock_adapter.get_host_info.return_value = HostInfo(
            platform="openclaw", version="2026.3.7",
        )
        with patch("core.compatibility.fetch_compatibility_matrix", return_value=None), \
             patch("lib.adapter.get_adapter", return_value=mock_adapter):
            watcher.tick()

        mock_adapter.get_host_info.assert_called_once()
        # Version cache should be written
        assert (tmp_path / "host-version.json").exists()

    def test_mtime_change_triggers_check(self, tmp_path):
        # Create a fake binary
        binary = tmp_path / "fake-binary"
        binary.write_text("v1")

        watcher = VersionWatcher(data_dir=tmp_path, quaid_version="0.2.15")
        watcher._host_info = HostInfo(
            platform="openclaw", version="2026.3.7",
            binary_path=str(binary),
        )
        watcher._last_binary_mtime = binary.stat().st_mtime
        watcher._last_full_check = 999999999999.0  # Far future

        # Touch binary to change mtime
        import time
        time.sleep(0.05)
        binary.write_text("v2")

        mock_adapter = MagicMock()
        mock_adapter.get_host_info.return_value = HostInfo(
            platform="openclaw", version="2026.4.0",
            binary_path=str(binary),
        )
        with patch("core.compatibility.fetch_compatibility_matrix", return_value=None), \
             patch("lib.adapter.get_adapter", return_value=mock_adapter):
            watcher.tick()

        # Full check should have been triggered
        mock_adapter.get_host_info.assert_called_once()


class TestAdaptiveCheckInterval:
    def test_normal_state_uses_24h(self, tmp_path):
        watcher = VersionWatcher(data_dir=tmp_path, quaid_version="0.2.15")
        watcher._last_state = CircuitBreakerState(status=NORMAL)
        assert watcher._check_interval() == CHECK_INTERVAL_NORMAL

    def test_untested_state_uses_1h(self, tmp_path):
        watcher = VersionWatcher(data_dir=tmp_path, quaid_version="0.2.15")
        watcher._last_state = CircuitBreakerState(status=NORMAL, untested=True)
        assert watcher._check_interval() == CHECK_INTERVAL_UNTESTED

    def test_degraded_state_uses_6h(self, tmp_path):
        watcher = VersionWatcher(data_dir=tmp_path, quaid_version="0.2.15")
        watcher._last_state = CircuitBreakerState(status=DEGRADED)
        assert watcher._check_interval() == CHECK_INTERVAL_DEGRADED

    def test_safe_mode_uses_1h(self, tmp_path):
        watcher = VersionWatcher(data_dir=tmp_path, quaid_version="0.2.15")
        watcher._last_state = CircuitBreakerState(status=SAFE_MODE)
        assert watcher._check_interval() == CHECK_INTERVAL_SAFE_MODE

    def test_no_state_defaults_to_untested(self, tmp_path):
        watcher = VersionWatcher(data_dir=tmp_path, quaid_version="0.2.15")
        watcher._last_state = None
        assert watcher._check_interval() == CHECK_INTERVAL_UNTESTED

    def test_untested_flag_persists_through_circuit_breaker(self, tmp_path):
        state = CircuitBreakerState(
            status=NORMAL, reason="Untested", untested=True,
        )
        write_circuit_breaker(tmp_path, state)
        loaded = read_circuit_breaker(tmp_path)
        assert loaded.untested is True

    def test_evaluate_no_match_no_untested_flag_by_default(self):
        """Default (testing_online=False) — no untested flag."""
        info = HostInfo(platform="openclaw", version="2099.1.0")
        matrix = {"matrix": []}
        state = evaluate_compatibility(info, "0.2.15", matrix)
        assert state.untested is False
        assert state.is_normal()

    def test_evaluate_compatible_no_untested_flag(self):
        info = HostInfo(platform="openclaw", version="2026.3.7")
        matrix = {"matrix": [{
            "host": "openclaw", "host_range": ">=2026.3.0",
            "quaid_range": ">=0.2.0", "status": "compatible",
        }]}
        state = evaluate_compatibility(info, "0.2.15", matrix)
        assert state.untested is False


class TestJanitorSchedulerWindow:
    """Test the midnight-wrapping window calculation."""

    def _make_scheduler(self, tmp_path, hour, window):
        # Stale checkpoint so catch-up doesn't interfere
        return JanitorScheduler(
            data_dir=tmp_path, quaid_home=tmp_path,
            scheduled_hour=hour, window_hours=window,
        )

    def test_window_wraps_around_midnight_high(self, tmp_path):
        """scheduled_hour=23, window=4 → hours 21,22,23,0 should be in window."""
        import datetime
        scheduler = self._make_scheduler(tmp_path, 23, 4)
        scheduler._last_tick = 0

        # Make checkpoint stale
        cp_dir = tmp_path / "logs" / "janitor"
        cp_dir.mkdir(parents=True)
        cp = cp_dir / "checkpoint-all.json"
        cp.write_text("{}")
        old_time = time.time() - 172800
        os.utime(cp, (old_time, old_time))

        # Hour 0 should be in window (wrapped)
        with patch("core.compatibility.JanitorScheduler._run_janitor") as mock_run, \
             patch("datetime.datetime") as mock_dt:
            mock_dt.now.return_value.hour = 0
            mock_dt.date.today.return_value.isoformat.return_value = "2026-03-11"
            scheduler.tick()
            mock_run.assert_called_once()

    def test_window_wraps_around_midnight_low(self, tmp_path):
        """scheduled_hour=1, window=4 → hours 23,0,1,2 should be in window."""
        scheduler = self._make_scheduler(tmp_path, 1, 4)
        scheduler._last_tick = 0

        cp_dir = tmp_path / "logs" / "janitor"
        cp_dir.mkdir(parents=True)
        cp = cp_dir / "checkpoint-all.json"
        cp.write_text("{}")
        old_time = time.time() - 172800
        os.utime(cp, (old_time, old_time))

        # Hour 23 should be in window (wrapped)
        with patch("core.compatibility.JanitorScheduler._run_janitor") as mock_run, \
             patch("datetime.datetime") as mock_dt:
            mock_dt.now.return_value.hour = 23
            mock_dt.date.today.return_value.isoformat.return_value = "2026-03-11"
            scheduler.tick()
            mock_run.assert_called_once()


class TestJanitorScheduler:
    def test_skips_when_circuit_breaker_tripped(self, tmp_path):
        write_circuit_breaker(tmp_path, CircuitBreakerState(status=SAFE_MODE))
        scheduler = JanitorScheduler(
            data_dir=tmp_path, quaid_home=tmp_path,
            scheduled_hour=0, window_hours=24,  # Always in window
        )
        scheduler._last_tick = 0  # Force check

        with patch("core.compatibility.JanitorScheduler._run_janitor") as mock_run:
            scheduler.tick()
            mock_run.assert_not_called()

    def test_skips_when_recently_run(self, tmp_path):
        checkpoint_dir = tmp_path / "logs" / "janitor"
        checkpoint_dir.mkdir(parents=True)
        (checkpoint_dir / "checkpoint-all.json").write_text("{}")

        scheduler = JanitorScheduler(
            data_dir=tmp_path, quaid_home=tmp_path,
            scheduled_hour=0, window_hours=24,
        )
        scheduler._last_tick = 0

        with patch("core.compatibility.JanitorScheduler._run_janitor") as mock_run:
            scheduler.tick()
            mock_run.assert_not_called()

    def test_triggers_when_checkpoint_stale(self, tmp_path):
        checkpoint_dir = tmp_path / "logs" / "janitor"
        checkpoint_dir.mkdir(parents=True)
        cp = checkpoint_dir / "checkpoint-all.json"
        cp.write_text("{}")
        # Set mtime to 2 days ago
        old_time = time.time() - 172800
        os.utime(cp, (old_time, old_time))

        scheduler = JanitorScheduler(
            data_dir=tmp_path, quaid_home=tmp_path,
            scheduled_hour=0, window_hours=24,
        )
        scheduler._last_tick = 0

        with patch("core.compatibility.JanitorScheduler._run_janitor") as mock_run:
            scheduler.tick()
            mock_run.assert_called_once()


class TestPreflightCheck:
    def test_compatible_returns_ok(self, tmp_path):
        from core.compatibility import preflight_compatibility_check
        with patch("core.compatibility.fetch_compatibility_matrix", return_value={
            "matrix": [{
                "host": "openclaw", "host_range": ">=2026.3.0",
                "quaid_range": ">=0.2.0", "status": "compatible",
            }],
        }):
            result = preflight_compatibility_check(
                "openclaw", "2026.3.7", "0.2.15", cache_dir=tmp_path,
            )
        assert result["ok"]
        assert result["status"] == "compatible"

    def test_incompatible_with_data_risk_blocks(self, tmp_path):
        from core.compatibility import preflight_compatibility_check
        with patch("core.compatibility.fetch_compatibility_matrix", return_value={
            "matrix": [{
                "host": "openclaw", "host_range": ">=2026.5.0",
                "quaid_range": "<0.3.0", "status": "incompatible",
                "data_risk": True, "message": "Breaks data",
                "fix": "Update OC",
            }],
        }):
            result = preflight_compatibility_check(
                "openclaw", "2026.5.0", "0.2.15", cache_dir=tmp_path,
            )
        assert not result["ok"]
        assert result["status"] == "incompatible"
        assert "Breaks data" in result["message"]

    def test_kill_switch_blocks(self, tmp_path):
        from core.compatibility import preflight_compatibility_check
        with patch("core.compatibility.fetch_compatibility_matrix", return_value={
            "kill_switch": True, "kill_message": "Emergency", "matrix": [],
        }):
            result = preflight_compatibility_check(
                "openclaw", "2026.3.7", "0.2.15", cache_dir=tmp_path,
            )
        assert not result["ok"]
        assert result["status"] == "kill_switch"

    def test_no_matrix_allows_with_warning(self, tmp_path):
        from core.compatibility import preflight_compatibility_check
        with patch("core.compatibility.fetch_compatibility_matrix", return_value=None):
            result = preflight_compatibility_check(
                "openclaw", "2026.3.7", "0.2.15", cache_dir=tmp_path,
            )
        assert result["ok"]
        assert result["status"] == "unknown"


class TestNotifyOnUse:
    def test_normal_returns_none(self, tmp_path):
        from core.compatibility import notify_on_use_if_degraded
        assert notify_on_use_if_degraded(tmp_path) is None

    def test_degraded_returns_message(self, tmp_path):
        from core.compatibility import notify_on_use_if_degraded
        write_circuit_breaker(tmp_path, CircuitBreakerState(
            status=DEGRADED, message="API changed",
        ))
        msg = notify_on_use_if_degraded(tmp_path)
        assert msg is not None
        assert "DEGRADED" in msg

    def test_cooldown_prevents_repeat(self, tmp_path):
        from core.compatibility import notify_on_use_if_degraded
        write_circuit_breaker(tmp_path, CircuitBreakerState(status=DEGRADED))
        msg1 = notify_on_use_if_degraded(tmp_path)
        msg2 = notify_on_use_if_degraded(tmp_path)
        assert msg1 is not None
        assert msg2 is None  # Cooled down
