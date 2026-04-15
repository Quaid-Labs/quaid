"""Concurrency guards for memory graph singleton initialization."""

import os
import threading
import time


# Ensure plugin root is importable
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_get_graph_initializes_singleton_once_under_concurrency():
    import datastore.memorydb.memory_graph as mg

    old_graph = mg._graph
    old_memory_graph_cls = mg.MemoryGraph
    init_calls = {"count": 0}

    class FakeMemoryGraph:
        def __init__(self):
            init_calls["count"] += 1
            time.sleep(0.01)

    mg._graph = None
    mg.MemoryGraph = FakeMemoryGraph
    try:
        results = []
        errors = []

        def worker():
            try:
                results.append(mg.get_graph())
            except Exception as exc:  # pragma: no cover - defensive
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(results) == 8
        assert init_calls["count"] == 1
        first = results[0]
        assert all(obj is first for obj in results)
    finally:
        mg.MemoryGraph = old_memory_graph_cls
        mg._graph = old_graph


def test_health_repairs_missing_visible_identity_stubs(tmp_path, monkeypatch):
    from lib.adapter import TestAdapter, set_adapter, reset_adapter
    import datastore.memorydb.memory_graph as mg

    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "codex-main")
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)
    try:
        visible_root = adapter.visible_instance_root()
        visible_root.mkdir(parents=True, exist_ok=True)
        created = mg._ensure_visible_identity_stubs()
        assert sorted(created) == ["ENVIRONMENT.md", "SOUL.md", "USER.md"]
        assert (visible_root / "SOUL.md").read_text(encoding="utf-8") == "# SOUL\n"
        assert (visible_root / "USER.md").read_text(encoding="utf-8") == "# USER\n"
        assert (visible_root / "ENVIRONMENT.md").read_text(encoding="utf-8") == "# ENVIRONMENT\n"
    finally:
        reset_adapter()
