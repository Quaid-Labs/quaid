from pathlib import Path
from types import SimpleNamespace

import pytest

import ingest.docs_ingest as docs_ingest


def _cfg(workspace_enabled: bool = True, auto_update: bool = True, max_docs: int = 3):
    return SimpleNamespace(
        systems=SimpleNamespace(workspace=workspace_enabled),
        docs=SimpleNamespace(auto_update_on_compact=auto_update, max_docs_per_update=max_docs),
    )


def test_docs_ingest_disabled_when_workspace_off(monkeypatch, tmp_path):
    t = tmp_path / "t.txt"
    t.write_text("hello")
    monkeypatch.setattr(docs_ingest, "get_config", lambda: _cfg(workspace_enabled=False))
    result = docs_ingest._run(t, "Compaction", "s1")
    assert result["status"] == "disabled"


def test_docs_ingest_disabled_when_auto_update_off(monkeypatch, tmp_path):
    t = tmp_path / "t.txt"
    t.write_text("hello")
    monkeypatch.setattr(docs_ingest, "get_config", lambda: _cfg(auto_update=False))
    result = docs_ingest._run(t, "Compaction", "s1")
    assert result["status"] == "disabled"


def test_docs_ingest_missing_transcript_returns_error(monkeypatch, tmp_path):
    missing = tmp_path / "missing.txt"
    monkeypatch.setattr(docs_ingest, "get_config", lambda: _cfg())
    monkeypatch.setattr(docs_ingest, "is_fail_hard_enabled", lambda: False)
    result = docs_ingest._run(missing, "Compaction", "s1")
    assert result["status"] == "error"
    assert "not found" in result["message"]


def test_docs_ingest_missing_transcript_raises_when_fail_hard(monkeypatch, tmp_path):
    missing = tmp_path / "missing.txt"
    monkeypatch.setattr(docs_ingest, "get_config", lambda: _cfg())
    monkeypatch.setattr(docs_ingest, "is_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="docs ingest transcript file not found"):
        docs_ingest._run(missing, "Compaction", "s1")


def test_docs_ingest_up_to_date(monkeypatch, tmp_path):
    t = tmp_path / "t.txt"
    t.write_text("hello")
    monkeypatch.setattr(docs_ingest, "get_config", lambda: _cfg())
    monkeypatch.setattr(docs_ingest, "check_staleness", lambda: {})
    result = docs_ingest._run(t, "Compaction", "s1")
    assert result["status"] == "up_to_date"
    assert result["staleDocs"] == 0


def test_docs_ingest_staleness_failure_returns_error_when_not_fail_hard(monkeypatch, tmp_path, caplog):
    t = tmp_path / "t.txt"
    t.write_text("hello")
    monkeypatch.setattr(docs_ingest, "get_config", lambda: _cfg())
    monkeypatch.setattr(docs_ingest, "check_staleness", lambda: (_ for _ in ()).throw(RuntimeError("stale down")))
    monkeypatch.setattr(docs_ingest, "is_fail_hard_enabled", lambda: False)

    with caplog.at_level("WARNING"):
        result = docs_ingest._run(t, "Compaction", "s1")

    assert result["status"] == "error"
    assert result["message"] == "staleness check failed"
    assert "docs ingest staleness check failed" in caplog.text


def test_docs_ingest_staleness_failure_raises_when_fail_hard(monkeypatch, tmp_path, caplog):
    t = tmp_path / "t.txt"
    t.write_text("hello")
    monkeypatch.setattr(docs_ingest, "get_config", lambda: _cfg())
    monkeypatch.setattr(docs_ingest, "check_staleness", lambda: (_ for _ in ()).throw(RuntimeError("stale down")))
    monkeypatch.setattr(docs_ingest, "is_fail_hard_enabled", lambda: True)

    with caplog.at_level("WARNING"):
        with pytest.raises(RuntimeError, match="docs ingest staleness check failed") as excinfo:
            docs_ingest._run(t, "Compaction", "s1")

    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert "stale down" in str(excinfo.value.__cause__)
    assert "docs ingest staleness check failed" in caplog.text


def test_docs_ingest_updates_docs(monkeypatch, tmp_path):
    t = tmp_path / "t.txt"
    t.write_text("hello")
    monkeypatch.setattr(docs_ingest, "get_config", lambda: _cfg(max_docs=5))
    monkeypatch.setattr(docs_ingest, "check_staleness", lambda: {"docs/a.md": object(), "docs/b.md": object()})

    calls = {}

    def _update(path: str, dry_run: bool, max_docs: int):
        calls["path"] = path
        calls["dry_run"] = dry_run
        calls["max_docs"] = max_docs
        return 2

    monkeypatch.setattr(docs_ingest, "cmd_update_from_transcript", _update)
    result = docs_ingest._run(t, "Compaction", "s1")
    assert result["status"] == "updated"
    assert result["staleDocs"] == 2
    assert result["updatedDocs"] == 2
    assert calls["path"] == str(t)
    assert calls["dry_run"] is False
    assert calls["max_docs"] == 5


def test_docs_ingest_update_failure_returns_error_when_not_fail_hard(monkeypatch, tmp_path, caplog):
    t = tmp_path / "t.txt"
    t.write_text("hello")
    monkeypatch.setattr(docs_ingest, "get_config", lambda: _cfg(max_docs=5))
    monkeypatch.setattr(docs_ingest, "check_staleness", lambda: {"docs/a.md": object(), "docs/b.md": object()})
    monkeypatch.setattr(
        docs_ingest,
        "cmd_update_from_transcript",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("update down")),
    )
    monkeypatch.setattr(docs_ingest, "is_fail_hard_enabled", lambda: False)

    with caplog.at_level("WARNING"):
        result = docs_ingest._run(t, "Compaction", "s1")

    assert result["status"] == "error"
    assert result["message"] == "transcript update failed"
    assert result["staleDocs"] == 2
    assert "docs ingest transcript update failed" in caplog.text


def test_docs_ingest_update_failure_raises_when_fail_hard(monkeypatch, tmp_path, caplog):
    t = tmp_path / "t.txt"
    t.write_text("hello")
    monkeypatch.setattr(docs_ingest, "get_config", lambda: _cfg(max_docs=5))
    monkeypatch.setattr(docs_ingest, "check_staleness", lambda: {"docs/a.md": object()})
    monkeypatch.setattr(
        docs_ingest,
        "cmd_update_from_transcript",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("update down")),
    )
    monkeypatch.setattr(docs_ingest, "is_fail_hard_enabled", lambda: True)

    with caplog.at_level("WARNING"):
        with pytest.raises(RuntimeError, match="docs ingest transcript update failed") as excinfo:
            docs_ingest._run(t, "Compaction", "s1")

    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert "update down" in str(excinfo.value.__cause__)
    assert "docs ingest transcript update failed" in caplog.text


def test_docs_ingest_preserves_explicit_zero_max_docs(monkeypatch, tmp_path):
    t = tmp_path / "t.txt"
    t.write_text("hello")
    monkeypatch.setattr(docs_ingest, "get_config", lambda: _cfg(max_docs=0))
    monkeypatch.setattr(docs_ingest, "check_staleness", lambda: {"docs/a.md": object()})

    calls = {}

    def _update(path: str, dry_run: bool, max_docs: int):
        calls["max_docs"] = max_docs
        return 0

    monkeypatch.setattr(docs_ingest, "cmd_update_from_transcript", _update)

    result = docs_ingest._run(t, "Compaction", "s1")

    assert result["status"] == "updated"
    assert calls["max_docs"] == 0
