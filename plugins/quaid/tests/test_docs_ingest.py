from pathlib import Path
from types import SimpleNamespace

import docs_ingest


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


def test_docs_ingest_up_to_date(monkeypatch, tmp_path):
    t = tmp_path / "t.txt"
    t.write_text("hello")
    monkeypatch.setattr(docs_ingest, "get_config", lambda: _cfg())
    monkeypatch.setattr(docs_ingest, "check_staleness", lambda: {})
    result = docs_ingest._run(t, "Compaction", "s1")
    assert result["status"] == "up_to_date"
    assert result["staleDocs"] == 0


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

