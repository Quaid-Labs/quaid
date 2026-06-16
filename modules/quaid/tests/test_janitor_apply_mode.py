"""Tests for janitor apply-mode policy resolution."""

import os
import sys
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure plugin root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("MEMORY_DB_PATH", ":memory:")

# Janitor loads config at import time; provide a temp standalone config.
_tmp_home = Path(tempfile.mkdtemp(prefix="quaid-janitor-test-"))
(_tmp_home / "config").mkdir(parents=True, exist_ok=True)
(_tmp_home / "config" / "config.json").write_text(
    json.dumps({"adapter": {"type": "standalone"}}), encoding="utf-8"
)
os.environ["QUAID_HOME"] = str(_tmp_home)

import core.lifecycle.janitor as janitor


def test_no_apply_flag_forces_dry_run():
    with patch.object(janitor._cfg.janitor, "apply_mode", "auto"):
        dry_run, warning = janitor._resolve_apply_mode(args_apply=False, args_approve=False)
    assert dry_run is True
    assert warning is None


def test_auto_mode_allows_apply():
    with patch.object(janitor._cfg.janitor, "apply_mode", "auto"):
        dry_run, warning = janitor._resolve_apply_mode(args_apply=True, args_approve=False)
    assert dry_run is False
    assert warning is None


def test_dry_run_mode_blocks_apply():
    with patch.object(janitor._cfg.janitor, "apply_mode", "dry_run"):
        dry_run, warning = janitor._resolve_apply_mode(args_apply=True, args_approve=False)
    assert dry_run is True
    assert warning is not None
    assert "dry_run" in warning


def test_ask_mode_requires_approve():
    with patch.object(janitor._cfg.janitor, "apply_mode", "ask"):
        dry_run, warning = janitor._resolve_apply_mode(args_apply=True, args_approve=False)
    assert dry_run is True
    assert warning is not None
    assert "--approve" in warning


def test_ask_mode_with_approve_allows_apply():
    with patch.object(janitor._cfg.janitor, "apply_mode", "ask"):
        dry_run, warning = janitor._resolve_apply_mode(args_apply=True, args_approve=True)
    assert dry_run is False
    assert warning is None


def test_lightweight_config_failure_logs_and_defaults_to_auto(monkeypatch, caplog):
    monkeypatch.setattr("lib.config._load_lightweight_config", lambda: (_ for _ in ()).throw(RuntimeError("config down")))
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: False)

    with caplog.at_level("WARNING", logger="core.lifecycle.janitor"):
        dry_run, warning = janitor._resolve_apply_mode_lightweight(args_apply=True, args_approve=False)

    assert dry_run is False
    assert warning is None
    assert "Lightweight config load failed; defaulting apply_mode to auto: config down" in caplog.text


def test_lightweight_config_failure_raises_when_failhard(monkeypatch):
    monkeypatch.setattr("lib.config._load_lightweight_config", lambda: (_ for _ in ()).throw(RuntimeError("config down")))
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="config down"):
        janitor._resolve_apply_mode_lightweight(args_apply=True, args_approve=False)
