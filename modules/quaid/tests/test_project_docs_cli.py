"""Tests for core/project_docs_cli.py benchmark-facing surfaces."""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import core.project_docs_cli as cli


def test_status_accepts_json_after_project(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["project_docs_cli.py", "status", "demo", "--json"])
    with patch("core.project_docs.project_status", return_value={"project": "demo", "fresh": True}):
        cli.main()
    parsed = json.loads(capsys.readouterr().out)
    assert parsed == {"project": "demo", "fresh": True}


def test_update_accepts_json_after_project(monkeypatch, capsys):
    request = {"project": "demo", "request_id": "req-1"}
    monkeypatch.setattr(sys, "argv", ["project_docs_cli.py", "update", "demo", "--json"])
    with patch("core.project_docs.read_state", return_value={}), \
         patch("core.project_docs.request_update", return_value=request), \
         patch("core.project_docs.ensure_supervisor_alive", return_value=1234):
        cli.main()
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["queued"] is True
    assert parsed["request"] == request
    assert parsed["supervisor_pid"] == 1234


def test_supervisor_run_dispatches_foreground_runner(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "project_docs_cli.py",
            "supervisor",
            "run",
            "--type",
            "project-docs",
            "--once",
            "--interval",
            "0.5",
        ],
    )
    with patch("core.project_docs_supervisor.run_supervisor", return_value=0) as run_supervisor:
        with pytest.raises(SystemExit) as exc:
            cli.main()
    assert exc.value.code == 0
    run_supervisor.assert_called_once_with(once=True, interval_seconds=0.5)
