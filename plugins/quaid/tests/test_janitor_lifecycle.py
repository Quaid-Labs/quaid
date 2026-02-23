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


def test_workspace_lifecycle_returns_phase_and_metrics(monkeypatch, tmp_path):
    workspace_audit_mod = ModuleType("workspace_audit")
    workspace_audit_mod.run_workspace_check = lambda dry_run: {
        "phase": "apply",
        "moved_to_docs": 3,
        "moved_to_memory": 1,
        "trimmed": 2,
        "bloat_warnings": 1,
        "project_detected": 1,
        "bloat_stats": {
            "big.md": {"over_limit": True, "lines": 250, "maxLines": 200},
            "ok.md": {"over_limit": False, "lines": 10, "maxLines": 200},
        },
    }
    monkeypatch.setitem(sys.modules, "workspace_audit", workspace_audit_mod)

    registry = build_default_registry()
    result = registry.run("workspace", RoutineContext(cfg=_make_cfg(False), dry_run=True, workspace=tmp_path))

    assert result.errors == []
    assert result.data["workspace_phase"] == "apply"
    assert result.data["bloated_files"] == ["big.md"]
    assert result.metrics["workspace_moved_to_docs"] == 3
    assert result.metrics["workspace_project_detected"] == 1
    assert any("Would apply review decisions" in line for line in result.logs)


def test_snippets_and_journal_lifecycle_run(monkeypatch, tmp_path):
    soul_snippets_mod = ModuleType("soul_snippets")
    calls = {"journal": []}

    soul_snippets_mod.run_soul_snippets_review = lambda dry_run: {
        "folded": 4,
        "rewritten": 2,
        "discarded": 1,
    }

    def _run_journal_distillation(*, dry_run, force_distill):
        calls["journal"].append((dry_run, force_distill))
        return {"additions": 3, "edits": 1, "total_entries": 9}

    soul_snippets_mod.run_journal_distillation = _run_journal_distillation
    monkeypatch.setitem(sys.modules, "soul_snippets", soul_snippets_mod)

    registry = build_default_registry()

    snippets_result = registry.run("snippets", RoutineContext(cfg=_make_cfg(False), dry_run=False, workspace=tmp_path))
    assert snippets_result.errors == []
    assert snippets_result.metrics["snippets_folded"] == 4
    assert snippets_result.metrics["snippets_rewritten"] == 2
    assert snippets_result.metrics["snippets_discarded"] == 1

    journal_result = registry.run(
        "journal",
        RoutineContext(cfg=_make_cfg(False), dry_run=True, workspace=tmp_path, force_distill=True),
    )
    assert journal_result.errors == []
    assert journal_result.metrics["journal_additions"] == 3
    assert journal_result.metrics["journal_edits"] == 1
    assert journal_result.metrics["journal_entries_distilled"] == 9
    assert calls["journal"] == [(True, True)]


def test_docs_lifecycle_staleness_and_cleanup(monkeypatch, tmp_path):
    docs_updater_mod = ModuleType("docs_updater")
    calls = {"updated": [], "cleaned": []}

    docs_updater_mod.get_doc_purposes = lambda: {"README.md": "summary", "projects/x/NOTES.md": "notes"}
    docs_updater_mod.check_staleness = lambda: {
        "README.md": SimpleNamespace(gap_hours=2.5, stale_sources=["src/a.ts"]),
        "projects/x/NOTES.md": SimpleNamespace(gap_hours=1.0, stale_sources=["src/b.ts"]),
    }
    docs_updater_mod.update_doc_from_diffs = lambda doc_path, purpose, stale_sources, dry_run: (
        calls["updated"].append((doc_path, purpose, tuple(stale_sources), dry_run)) or True
    )
    docs_updater_mod.check_cleanup_needed = lambda: {
        "README.md": SimpleNamespace(reason="updates", updates_since_cleanup=5, growth_ratio=1.0),
        "projects/x/NOTES.md": SimpleNamespace(reason="growth", updates_since_cleanup=1, growth_ratio=2.2),
    }
    docs_updater_mod.cleanup_doc = lambda doc_path, purpose, dry_run: (
        calls["cleaned"].append((doc_path, purpose, dry_run)) or True
    )
    monkeypatch.setitem(sys.modules, "docs_updater", docs_updater_mod)

    allow_calls = []

    def _allow(doc_path, action):
        allow_calls.append((doc_path, action))
        return doc_path.endswith("README.md")

    registry = build_default_registry()

    staleness_result = registry.run(
        "docs_staleness",
        RoutineContext(cfg=_make_cfg(False), dry_run=False, workspace=tmp_path, allow_doc_apply=_allow),
    )
    assert staleness_result.errors == []
    assert staleness_result.metrics["docs_updated"] == 1
    assert [c[0] for c in calls["updated"]] == ["README.md"]

    cleanup_result = registry.run(
        "docs_cleanup",
        RoutineContext(cfg=_make_cfg(False), dry_run=False, workspace=tmp_path, allow_doc_apply=_allow),
    )
    assert cleanup_result.errors == []
    assert cleanup_result.metrics["docs_cleaned"] == 1
    assert [c[0] for c in calls["cleaned"]] == ["README.md"]
    assert ("README.md", "staleness update") in allow_calls
    assert ("projects/x/NOTES.md", "cleanup") in allow_calls
