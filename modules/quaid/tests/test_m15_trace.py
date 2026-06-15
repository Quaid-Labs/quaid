import json
from pathlib import Path

import pytest

from lib.m15_trace import activate_m15_trace_for_prompt, is_m15_trace_active, m15_trace_path, trace_m15


def test_m15_trace_inactive_for_non_m15_prompt(monkeypatch, tmp_path):
    trace_path = tmp_path / "trace.jsonl"
    monkeypatch.delenv("QUAID_M15_TRACE", raising=False)
    monkeypatch.delenv("QUAID_M15_TRACE_FILE", raising=False)
    monkeypatch.delenv("_QUAID_M15_TRACE_ACTIVE", raising=False)
    monkeypatch.setenv("QUAID_M15_TRACE_FILE", "")

    assert activate_m15_trace_for_prompt("ordinary prompt") is False
    assert is_m15_trace_active() is False
    monkeypatch.setenv("QUAID_M15_TRACE_FILE", str(trace_path))
    monkeypatch.delenv("QUAID_M15_TRACE_FILE", raising=False)
    trace_m15("should_not_write")
    assert not trace_path.exists()


def test_m15_trace_activates_for_m15_prompt_and_writes(monkeypatch, tmp_path):
    trace_path = tmp_path / "trace.jsonl"
    monkeypatch.delenv("QUAID_M15_TRACE", raising=False)
    monkeypatch.delenv("_QUAID_M15_TRACE_ACTIVE", raising=False)
    monkeypatch.setenv("QUAID_M15_TRACE_FILE", str(trace_path))
    monkeypatch.setenv("QUAID_INSTANCE", "codex-test")
    monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:06:07Z")

    assert activate_m15_trace_for_prompt("M15 turn N+2") is True
    trace_m15("test.event", prompt="M15 turn N+2", nested={"value": 7})

    rows = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["ts"] == "2026-03-11T05:06:07+00:00"
    assert rows[-1]["event"] == "test.event"
    assert rows[-1]["instance"] == "codex-test"
    assert rows[-1]["nested"] == {"value": 7}


def test_m15_trace_default_path_honors_quaid_now(monkeypatch):
    monkeypatch.delenv("QUAID_M15_TRACE_FILE", raising=False)
    monkeypatch.setenv("QUAID_INSTANCE", "codex-test")
    monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:06:07Z")

    assert m15_trace_path() == "/tmp/m15-trace-codex-test-20260311.jsonl"


def test_m15_trace_rejects_malformed_quaid_now_when_active(monkeypatch, tmp_path):
    trace_path = tmp_path / "trace.jsonl"
    monkeypatch.setenv("QUAID_M15_TRACE_FILE", str(trace_path))
    monkeypatch.setenv("QUAID_NOW", "not-a-date")

    with pytest.raises(ValueError, match="Invalid QUAID_NOW"):
        trace_m15("bad-clock")

    assert not trace_path.exists()


def test_m15_trace_inactive_ignores_malformed_quaid_now(monkeypatch, tmp_path):
    trace_path = tmp_path / "trace.jsonl"
    monkeypatch.delenv("QUAID_M15_TRACE", raising=False)
    monkeypatch.delenv("QUAID_M15_TRACE_FILE", raising=False)
    monkeypatch.delenv("_QUAID_M15_TRACE_ACTIVE", raising=False)
    monkeypatch.setenv("QUAID_NOW", "not-a-date")

    trace_m15("inactive-bad-clock")

    assert not trace_path.exists()
