from pathlib import Path

import config as cfgmod
from config import ContradictionConfig


def test_contradiction_config_default_disabled():
    cfg = ContradictionConfig()
    assert cfg.enabled is False


def test_load_config_defaults_contradictions_disabled_when_missing(monkeypatch, tmp_path: Path):
    cfg_path = tmp_path / "memory.json"
    cfg_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(cfgmod, "_config", None)
    monkeypatch.setattr(cfgmod, "_config_loading", False)
    monkeypatch.setattr(cfgmod, "_config_paths", lambda: [cfg_path])
    cfg = cfgmod.load_config()
    assert cfg.janitor.contradiction.enabled is False


def test_load_config_respects_explicit_contradiction_enable(monkeypatch, tmp_path: Path):
    cfg_path = tmp_path / "memory.json"
    cfg_path.write_text('{"janitor": {"contradiction": {"enabled": true}}}\n', encoding="utf-8")
    monkeypatch.setattr(cfgmod, "_config", None)
    monkeypatch.setattr(cfgmod, "_config_loading", False)
    monkeypatch.setattr(cfgmod, "_config_paths", lambda: [cfg_path])
    cfg = cfgmod.load_config()
    assert cfg.janitor.contradiction.enabled is True
