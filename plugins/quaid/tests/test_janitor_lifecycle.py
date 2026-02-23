import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

from janitor_lifecycle import RoutineContext, build_default_registry


class _FakeRag:
    def __init__(self) -> None:
        self.calls = []

    def reindex_all(self, path: str, force: bool = False):
        self.calls.append((path, force))
        return {"total_files": 2, "indexed_files": 1, "skipped_files": 1, "total_chunks": 3}


def _make_cfg(projects_enabled: bool = True):
    return SimpleNamespace(
        projects=SimpleNamespace(
            enabled=projects_enabled,
            definitions={
                "demo": SimpleNamespace(auto_index=True, home_dir="projects/demo"),
                "off": SimpleNamespace(auto_index=False, home_dir="projects/off"),
            },
        ),
        rag=SimpleNamespace(docs_dir="docs"),
    )


def test_rag_lifecycle_runs_and_returns_metrics(monkeypatch, tmp_path):
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "projects" / "demo").mkdir(parents=True, exist_ok=True)

    fake_rag = _FakeRag()
    monkeypatch.setattr("janitor_lifecycle.DocsRAG", lambda: fake_rag)

    docs_registry_mod = ModuleType("docs_registry")

    class _Registry:
        def auto_discover(self, _project_name):
            return ["a.md", "b.md"]

        def sync_external_files(self, _project_name):
            return None

    docs_registry_mod.DocsRegistry = _Registry
    monkeypatch.setitem(sys.modules, "docs_registry", docs_registry_mod)

    project_updater_mod = ModuleType("project_updater")
    project_updater_mod.process_all_events = lambda: {"processed": 2}
    monkeypatch.setitem(sys.modules, "project_updater", project_updater_mod)

    registry = build_default_registry()
    ctx = RoutineContext(cfg=_make_cfg(projects_enabled=True), dry_run=False, workspace=tmp_path)
    result = registry.run("rag", ctx)

    assert result.errors == []
    assert result.metrics["project_events_processed"] == 2
    assert result.metrics["project_files_discovered"] == 2
    assert result.metrics["rag_files_indexed"] == 2  # docs + project dir
    assert result.metrics["rag_chunks_created"] == 6
    assert any("Reindexing" in line for line in result.logs)


def test_rag_lifecycle_handles_missing_routine():
    registry = build_default_registry()
    result = registry.run("missing", RoutineContext(cfg=_make_cfg(False), dry_run=True, workspace=Path(".")))
    assert result.errors
    assert "No lifecycle routine registered" in result.errors[0]
