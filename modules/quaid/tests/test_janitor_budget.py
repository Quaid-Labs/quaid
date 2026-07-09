from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _cfg():
    return SimpleNamespace(
        systems=SimpleNamespace(
            memory=True,
            journal=False,
            projects=False,
            workspace=False,
        ),
        plugins=SimpleNamespace(
            enabled=False,
            strict=False,
            config={},
            slots=SimpleNamespace(adapter="", ingest=[], datastores=[]),
        ),
        janitor=SimpleNamespace(
            apply_mode="auto",
            approval_policies={},
            test_timeout_seconds=60,
            dedup=SimpleNamespace(similarity_threshold=0.85, high_similarity_threshold=0.95),
            contradiction=SimpleNamespace(enabled=False, min_similarity=0.7, max_similarity=0.95),
            task_timeout_minutes=120,
        ),
        notifications=SimpleNamespace(enabled=False, level="normal"),
        users=SimpleNamespace(default_owner="quaid"),
        core=SimpleNamespace(
            parallel=SimpleNamespace(
                enabled=True,
                lock_enforcement_enabled=False,
                lock_wait_seconds=5,
                lock_require_registration=False,
                lifecycle_prepass_timeout_seconds=60,
                lifecycle_prepass_timeout_retries=0,
                lifecycle_prepass_workers=1,
            )
        ),
        rag=SimpleNamespace(docs_dir="docs"),
        database=SimpleNamespace(path=":memory:"),
    )


def test_duplicates_stage_respects_time_budget(monkeypatch, tmp_path, capsys):
    from core.lifecycle import janitor

    monkeypatch.setattr(janitor, "_cfg", _cfg())
    monkeypatch.setattr(janitor, "_refresh_runtime_state", lambda: None)
    monkeypatch.setattr(janitor, "_workspace", lambda: tmp_path)
    monkeypatch.setattr(janitor, "_logs_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(janitor, "_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(janitor, "_benchmark_review_gate_triggered", lambda *_a, **_kw: False)
    monkeypatch.setattr(janitor, "is_benchmark_mode", lambda: False)
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(janitor, "get_llm_provider", lambda: SimpleNamespace(get_profiles=lambda: {"deep": {"available": True}}))
    monkeypatch.setattr(janitor, "get_graph", lambda: object())
    monkeypatch.setattr(janitor, "init_janitor_metadata", lambda _graph: None)
    monkeypatch.setattr(janitor, "get_last_run_time", lambda _graph, _task: None)
    monkeypatch.setattr(janitor, "_check_for_updates", lambda: None)
    monkeypatch.setattr(janitor, "get_token_usage", lambda: {"api_calls": 0, "input_tokens": 0, "output_tokens": 0})
    monkeypatch.setattr(janitor, "estimate_cost", lambda: 0.0)
    monkeypatch.setattr(janitor, "record_health_snapshot", lambda *_a, **_kw: None)
    monkeypatch.setattr(janitor, "record_janitor_run", lambda *_a, **_kw: None)
    monkeypatch.setattr(janitor, "checkpoint_wal", lambda *_a, **_kw: None)
    monkeypatch.setattr(janitor, "save_run_time", lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(janitor, "list_recent_fact_texts", lambda *_a, **_kw: [])
    monkeypatch.setattr(janitor, "count_nodes_by_status", lambda *_a, **_kw: {})

    calls = []

    class _Registry:
        def run(self, name, ctx):
            calls.append(name)
            raise AssertionError("duplicates lifecycle stage should not run after budget skip")

    monkeypatch.setattr(janitor, "_lifecycle_registry", lambda: _Registry())
    monkeypatch.setattr(janitor, "_atomic_write_json", lambda path, payload: path.write_text(json.dumps(payload), encoding="utf-8"))

    janitor._run_task_optimized_inner(
        "duplicates",
        dry_run=True,
        incremental=True,
        time_budget=1,
        force_distill=False,
        user_approved=True,
        resume_checkpoint=False,
    )

    out = capsys.readouterr().out
    assert "Task 3: Find Near-Duplicates] SKIPPED" in out
    assert calls == []
