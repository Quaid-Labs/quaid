import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import run_production_benchmark as rpb  # noqa: E402


class _ProcResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_store_facts_retries_edge_timeout_and_succeeds(monkeypatch, tmp_path):
    monkeypatch.setattr(rpb, "_MEMORY_GRAPH_SCRIPT", tmp_path / "dummy.py")
    monkeypatch.setattr(rpb, "_load_active_domain_ids", lambda _ws: ["personal", "project", "work", "technical"])

    calls = {"n": 0}

    def _run(cmd, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _ProcResult(returncode=0, stdout="Stored: fact-1\n")
        if calls["n"] == 2:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)
        return _ProcResult(returncode=0, stdout='{"status":"created"}\n')

    monkeypatch.setattr(rpb.subprocess, "run", _run)
    monkeypatch.setenv("BENCHMARK_EDGE_RETRIES", "1")
    monkeypatch.setenv("BENCHMARK_EDGE_RETRY_BACKOFF_SECONDS", "0")
    monkeypatch.setenv("BENCHMARK_EDGE_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("BENCHMARK_FAIL_ON_STORE_FAILURE", "1")

    facts = [{
        "text": "Maya works at TechFlow as a product manager",
        "category": "fact",
        "privacy": "shared",
        "domains": ["work"],
        "edges": [{"subject": "Maya", "relation": "works_at", "object": "TechFlow"}],
    }]

    stored, edges = rpb._store_facts(tmp_path, facts, os.environ.copy(), 1, "2026-03-01")
    assert stored == 1
    assert edges == 1
    assert calls["n"] == 3


def test_tool_memory_recall_includes_domain_filter_boost_and_project(monkeypatch, tmp_path):
    monkeypatch.setattr(rpb, "_MEMORY_GRAPH_SCRIPT", tmp_path / "dummy.py")

    seen = {"cmd": None}

    def _run(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        return _ProcResult(returncode=0, stdout="[]")

    monkeypatch.setattr(rpb.subprocess, "run", _run)

    out = rpb._tool_memory_recall(
        query="where is maya",
        workspace=tmp_path,
        env=os.environ.copy(),
        domain_filter={"projects": True, "work": True},
        domain_boost={"project": 1.5},
        project="quaid",
    )

    assert out == "[]"
    cmd = seen["cmd"]
    assert cmd is not None
    assert "--project" in cmd
    assert cmd[cmd.index("--project") + 1] == "quaid"
    assert "--domain-filter" in cmd
    filt = json.loads(cmd[cmd.index("--domain-filter") + 1])
    assert filt == {"projects": True, "work": True}
    assert "--domain-boost" in cmd
    boost = json.loads(cmd[cmd.index("--domain-boost") + 1])
    assert boost == {"project": 1.5}
