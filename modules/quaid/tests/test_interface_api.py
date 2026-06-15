from __future__ import annotations

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


def test_recall_fast_allows_compact_non_ascii_query(monkeypatch):
    service = _FakeMemoryService()
    monkeypatch.setattr(api, "_memory", lambda: service)

    rows, meta = api.recall_fast("健康計画", owner_id="owner", return_meta=True)

    assert rows == [{"text": "matched"}]
    assert meta == {"mode": "fast"}
    assert service.calls[0][0] == "recall_fast"
    assert service.calls[0][1]["query"] == "健康計画"
