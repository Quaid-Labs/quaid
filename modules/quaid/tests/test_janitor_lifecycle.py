import sys
import sqlite3
from pathlib import Path
from types import ModuleType, SimpleNamespace

from core.lifecycle.janitor_lifecycle import RoutineContext, build_default_registry


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
        database=SimpleNamespace(path="data/memory.db"),
        janitor=SimpleNamespace(
            parallel=SimpleNamespace(
                enabled=True,
                lock_enforcement_enabled=True,
                lock_wait_seconds=5,
                lock_require_registration=True,
            )
        ),
    )


def test_rag_lifecycle_runs_and_returns_metrics(monkeypatch, tmp_path):
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "projects" / "demo").mkdir(parents=True, exist_ok=True)

    fake_rag = _FakeRag()
    monkeypatch.setattr("datastore.docsdb.rag.DocsRAG", lambda: fake_rag)

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
    monkeypatch.setattr("core.lifecycle.workspace_audit.run_workspace_check", lambda dry_run: {
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
    })

    registry = build_default_registry()
    result = registry.run("workspace", RoutineContext(cfg=_make_cfg(False), dry_run=True, workspace=tmp_path))

    assert result.errors == []
    assert result.data["workspace_phase"] == "apply"
    assert result.data["bloated_files"] == ["big.md"]
    assert result.metrics["workspace_moved_to_docs"] == 3
    assert result.metrics["workspace_project_detected"] == 1
    assert any("Would apply review decisions" in line for line in result.logs)


def test_snippets_and_journal_lifecycle_run(monkeypatch, tmp_path):
    calls = {"journal": []}

    monkeypatch.setattr("datastore.docsdb.soul_snippets.run_soul_snippets_review", lambda dry_run: {
        "folded": 4,
        "rewritten": 2,
        "discarded": 1,
    })

    def _run_journal_distillation(*, dry_run, force_distill):
        calls["journal"].append((dry_run, force_distill))
        return {"additions": 3, "edits": 1, "total_entries": 9}

    monkeypatch.setattr("datastore.docsdb.soul_snippets.run_journal_distillation", _run_journal_distillation)

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
    calls = {"updated": [], "cleaned": []}

    monkeypatch.setattr(
        "datastore.docsdb.updater.get_doc_purposes",
        lambda: {"README.md": "summary", "projects/x/NOTES.md": "notes"},
    )
    monkeypatch.setattr("datastore.docsdb.updater.check_staleness", lambda: {
        "README.md": SimpleNamespace(gap_hours=2.5, stale_sources=["src/a.ts"]),
        "projects/x/NOTES.md": SimpleNamespace(gap_hours=1.0, stale_sources=["src/b.ts"]),
    })
    monkeypatch.setattr("datastore.docsdb.updater.update_doc_from_diffs", lambda doc_path, purpose, stale_sources, dry_run: (
        calls["updated"].append((doc_path, purpose, tuple(stale_sources), dry_run)) or True
    ))
    monkeypatch.setattr("datastore.docsdb.updater.check_cleanup_needed", lambda: {
        "README.md": SimpleNamespace(reason="updates", updates_since_cleanup=5, growth_ratio=1.0),
        "projects/x/NOTES.md": SimpleNamespace(reason="growth", updates_since_cleanup=1, growth_ratio=2.2),
    })
    monkeypatch.setattr("datastore.docsdb.updater.cleanup_doc", lambda doc_path, purpose, dry_run: (
        calls["cleaned"].append((doc_path, purpose, dry_run)) or True
    ))

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


def test_datastore_cleanup_lifecycle_runs_with_graph_override(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE recall_log (created_at TEXT);
        CREATE TABLE dedup_log (review_status TEXT, created_at TEXT);
        CREATE TABLE health_snapshots (created_at TEXT);
        CREATE TABLE embedding_cache (created_at TEXT);
        CREATE TABLE metadata (key TEXT, updated_at TEXT);
        CREATE TABLE janitor_runs (completed_at TEXT);
        INSERT INTO recall_log VALUES ('2000-01-01');
        INSERT INTO dedup_log VALUES ('done', '2000-01-01');
        INSERT INTO health_snapshots VALUES ('2000-01-01');
        INSERT INTO embedding_cache VALUES ('2000-01-01');
        INSERT INTO metadata VALUES ('janitor_x', '2000-01-01');
        INSERT INTO janitor_runs VALUES ('2000-01-01');
        """
    )

    class _Graph:
        def _get_conn(self):
            return conn

    registry = build_default_registry()
    result = registry.run(
        "datastore_cleanup",
        RoutineContext(cfg=_make_cfg(False), dry_run=False, workspace=tmp_path, graph=_Graph()),
    )
    assert result.errors == []
    assert result.data["cleanup"]["recall_log"] == 1
    assert result.data["cleanup"]["janitor_runs"] == 1


def test_lifecycle_registry_run_many_executes_in_parallel_shape(tmp_path):
    registry = build_default_registry()

    def _ok_a(_ctx):
        return SimpleNamespace(metrics={"a": 1}, logs=[], errors=[], data={})

    def _ok_b(_ctx):
        return SimpleNamespace(metrics={"b": 1}, logs=[], errors=[], data={})

    registry.register("a", _ok_a)
    registry.register("b", _ok_b)

    out = registry.run_many(
        [
            ("a", RoutineContext(cfg=_make_cfg(False), dry_run=True, workspace=tmp_path)),
            ("b", RoutineContext(cfg=_make_cfg(False), dry_run=True, workspace=tmp_path)),
        ],
        max_workers=2,
    )
    assert set(out.keys()) == {"a", "b"}
    assert out["a"].metrics["a"] == 1
    assert out["b"].metrics["b"] == 1


def test_lifecycle_registry_requires_write_registration_when_enabled(tmp_path):
    from core.lifecycle.janitor_lifecycle import LifecycleRegistry

    registry = LifecycleRegistry()
    registry.register("writer", lambda _ctx: SimpleNamespace(metrics={"ok": 1}, logs=[], errors=[], data={}))

    result = registry.run("writer", RoutineContext(cfg=_make_cfg(False), dry_run=False, workspace=tmp_path))
    assert result.errors
    assert "missing write resource registration" in result.errors[0]


def test_lifecycle_registry_allows_registered_write_locks(tmp_path):
    from core.lifecycle.janitor_lifecycle import LifecycleRegistry

    registry = LifecycleRegistry()
    registry.register(
        "writer",
        lambda _ctx: SimpleNamespace(metrics={"ok": 1}, logs=[], errors=[], data={}),
        write_resources=["files:global", "db:memory"],
    )

    result = registry.run("writer", RoutineContext(cfg=_make_cfg(False), dry_run=False, workspace=tmp_path))
    assert result.errors == []
    assert result.metrics["ok"] == 1
