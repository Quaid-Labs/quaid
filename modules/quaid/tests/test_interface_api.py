from __future__ import annotations

import logging
import sys
from types import ModuleType

from core.contracts.memory import MemoryServicePort
from core.interface import api


class _FakeMemoryService:
    def __init__(self) -> None:
        self.calls = []

    def recall(self, **kwargs):
        self.calls.append(("recall", kwargs))
        return [{"text": "matched"}]

    def recall_fast(self, **kwargs):
        self.calls.append(("recall_fast", kwargs))
        return ([{"text": "matched"}], {"mode": "fast"}) if kwargs.get("return_meta") else [{"text": "matched"}]


def test_memory_service_contract_declares_recall_fast():
    assert "recall_fast" in MemoryServicePort.__dict__


def test_recall_skips_short_ascii_query(monkeypatch):
    service = _FakeMemoryService()
    monkeypatch.setattr(api, "_memory", lambda: service)

    assert api.recall("hi", owner_id="owner") == []
    assert service.calls == []


def test_recall_allows_compact_non_ascii_query(monkeypatch):
    service = _FakeMemoryService()
    monkeypatch.setattr(api, "_memory", lambda: service)

    rows = api.recall("旅行計画", owner_id="owner")

    assert rows == [{"text": "matched"}]
    assert service.calls[0][0] == "recall"
    assert service.calls[0][1]["query"] == "旅行計画"


def test_recall_allows_compact_combining_mark_query(monkeypatch):
    service = _FakeMemoryService()
    monkeypatch.setattr(api, "_memory", lambda: service)

    rows = api.recall("स्वास्थ्य", owner_id="owner")

    assert rows == [{"text": "matched"}]
    assert service.calls[0][0] == "recall"
    assert service.calls[0][1]["query"] == "स्वास्थ्य"


def test_recall_fast_skips_short_ascii_query(monkeypatch):
    service = _FakeMemoryService()
    monkeypatch.setattr(api, "_memory", lambda: service)

    assert api.recall_fast("hi", owner_id="owner", return_meta=True) == ([], None)
    assert service.calls == []


def test_recall_fast_logs_m15_trace_failures_for_short_query(monkeypatch, caplog):
    service = _FakeMemoryService()
    monkeypatch.setattr(api, "_memory", lambda: service)

    trace_mod = ModuleType("lib.m15_trace")
    trace_mod.trace_m15 = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("trace broke"))
    monkeypatch.setitem(sys.modules, "lib.m15_trace", trace_mod)

    with caplog.at_level(logging.DEBUG, logger="core.interface.api"):
        assert api.recall_fast("hi", owner_id="owner", return_meta=True) == ([], None)

    assert service.calls == []
    assert "[api.recall_fast] m15 trace failed for entry" in caplog.text
    assert "[api.recall_fast] m15 trace failed for short_query_skip" in caplog.text


def test_recall_fast_allows_compact_non_ascii_query(monkeypatch):
    service = _FakeMemoryService()
    monkeypatch.setattr(api, "_memory", lambda: service)

    rows, meta = api.recall_fast("健康計画", owner_id="owner", return_meta=True)

    assert rows == [{"text": "matched"}]
    assert meta == {"mode": "fast"}
    assert service.calls[0][0] == "recall_fast"
    assert service.calls[0][1]["query"] == "健康計画"


def test_recall_fast_logs_m15_trace_failure_on_exit(monkeypatch, caplog):
    service = _FakeMemoryService()
    monkeypatch.setattr(api, "_memory", lambda: service)

    trace_mod = ModuleType("lib.m15_trace")
    calls = []

    def _trace(event, **_kwargs):
        calls.append(event)
        if event == "api.recall_fast.exit":
            raise RuntimeError("exit trace broke")

    trace_mod.trace_m15 = _trace
    monkeypatch.setitem(sys.modules, "lib.m15_trace", trace_mod)

    with caplog.at_level(logging.DEBUG, logger="core.interface.api"):
        rows, meta = api.recall_fast("健康計画", owner_id="owner", return_meta=True)

    assert rows == [{"text": "matched"}]
    assert meta == {"mode": "fast"}
    assert calls == ["api.recall_fast.entry", "api.recall_fast.exit"]
    assert "[api.recall_fast] m15 trace failed for exit" in caplog.text


def test_recall_fast_logs_m15_trace_failure_on_exception(monkeypatch, caplog):
    err = RuntimeError("recall failed")

    class _FailingMemoryService:
        def recall_fast(self, **_kwargs):
            raise err

    monkeypatch.setattr(api, "_memory", lambda: _FailingMemoryService())

    trace_mod = ModuleType("lib.m15_trace")
    calls = []

    def _trace(event, **_kwargs):
        calls.append(event)
        if event == "api.recall_fast.exception":
            raise RuntimeError("exception trace broke")

    trace_mod.trace_m15 = _trace
    monkeypatch.setitem(sys.modules, "lib.m15_trace", trace_mod)

    with caplog.at_level(logging.DEBUG, logger="core.interface.api"):
        try:
            api.recall_fast("健康計画", owner_id="owner", return_meta=True)
        except RuntimeError as exc:
            assert exc is err
        else:
            raise AssertionError("recall_fast should propagate service failure")

    assert calls == ["api.recall_fast.entry", "api.recall_fast.exception"]
    assert "[api.recall_fast] m15 trace failed for exception" in caplog.text
