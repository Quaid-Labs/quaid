from __future__ import annotations

import sys
import threading
from types import SimpleNamespace

import pytest


def test_run_all_deadline_watchdog_writes_markers_releases_lock_and_exits(monkeypatch):
    from core import janitor_worker

    marker_calls = []
    release_calls = []
    exit_codes = []
    exit_called = threading.Event()

    class FakeJanitor:
        @staticmethod
        def _release_lock():
            release_calls.append(True)

    def fake_markers(exc, janitor_module):
        marker_calls.append((exc, janitor_module))

    def fake_exit(code):
        exit_codes.append(code)
        exit_called.set()

    monkeypatch.setattr(janitor_worker, "_write_run_all_failure_markers", fake_markers)
    monkeypatch.setattr(janitor_worker.os, "_exit", fake_exit)

    stop = janitor_worker._start_run_all_deadline_watchdog(FakeJanitor, timeout_seconds=0.01)
    try:
        assert exit_called.wait(1.0)
    finally:
        stop.set()

    assert exit_codes == [124]
    assert release_calls == [True]
    assert len(marker_calls) == 1
    exc, janitor_module = marker_calls[0]
    assert isinstance(exc, TimeoutError)
    assert "janitor run-all-once exceeded hard timeout" in str(exc)
    assert janitor_module is FakeJanitor


def test_run_all_timeout_invalid_env_raises_when_failhard(monkeypatch):
    from core import janitor_worker

    monkeypatch.setenv("QUAID_JANITOR_RUN_ALL_TIMEOUT_SECONDS", "bad")
    monkeypatch.setitem(
        sys.modules,
        "config",
        SimpleNamespace(
            get_config=lambda: SimpleNamespace(
                janitor=SimpleNamespace(task_timeout_minutes=4)
            )
        ),
    )
    monkeypatch.setattr(janitor_worker, "_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="QUAID_JANITOR_RUN_ALL_TIMEOUT_SECONDS config invalid"):
        janitor_worker._run_all_timeout_seconds()


def test_run_all_timeout_uses_janitor_task_timeout_config(monkeypatch):
    from core import janitor_worker

    monkeypatch.delenv("QUAID_JANITOR_RUN_ALL_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "config",
        SimpleNamespace(
            get_config=lambda: SimpleNamespace(
                janitor=SimpleNamespace(task_timeout_minutes=4)
            )
        ),
    )

    assert janitor_worker._run_all_timeout_seconds() == 240.0


def test_run_all_timeout_env_overrides_config(monkeypatch):
    from core import janitor_worker

    monkeypatch.setenv("QUAID_JANITOR_RUN_ALL_TIMEOUT_SECONDS", "15")
    monkeypatch.setitem(
        sys.modules,
        "config",
        SimpleNamespace(
            get_config=lambda: SimpleNamespace(
                janitor=SimpleNamespace(task_timeout_minutes=4)
            )
        ),
    )

    assert janitor_worker._run_all_timeout_seconds() == 15.0


def test_run_all_timeout_zero_config_uses_large_finite_ceiling(monkeypatch):
    from core import janitor_worker

    monkeypatch.delenv("QUAID_JANITOR_RUN_ALL_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "config",
        SimpleNamespace(
            get_config=lambda: SimpleNamespace(
                janitor=SimpleNamespace(task_timeout_minutes=0)
            )
        ),
    )

    assert janitor_worker._run_all_timeout_seconds() == 86400.0


def test_run_all_timeout_invalid_env_falls_back_when_not_failhard(monkeypatch):
    from core import janitor_worker

    monkeypatch.setenv("QUAID_JANITOR_RUN_ALL_TIMEOUT_SECONDS", "bad")
    monkeypatch.setitem(
        sys.modules,
        "config",
        SimpleNamespace(
            get_config=lambda: SimpleNamespace(
                janitor=SimpleNamespace(task_timeout_minutes=4)
            )
        ),
    )
    monkeypatch.setattr(janitor_worker, "_fail_hard_enabled", lambda: False)

    assert janitor_worker._run_all_timeout_seconds() == 240.0
