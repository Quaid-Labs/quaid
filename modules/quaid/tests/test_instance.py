"""Tests for lib/instance.py — instance resolution and validation."""

import os
import json
import shutil
import pytest
from pathlib import Path

from lib.instance import (
    InstanceError,
    RESERVED_INSTANCE_NAMES,
    internal_path_derived_instance_ids,
    is_internal_path_derived_instance_id,
    validate_instance_id,
    quaid_home,
    visible_home,
    instance_id,
    instance_root,
    visible_instance_root,
    shared_dir,
    shared_projects_dir,
    shared_registry_path,
    instance_exists,
    list_instances,
    prune_livetest_instance_residues,
    prune_stale_openclaw_agent_instances,
    require_instance_exists,
)


class TestValidateInstanceId:
    def test_valid_names(self):
        assert validate_instance_id("openclaw") == "openclaw"
        assert validate_instance_id("claude-code") == "claude-code"
        assert validate_instance_id("work") == "work"
        assert validate_instance_id("personal") == "personal"
        assert validate_instance_id("my_instance.v2") == "my_instance.v2"

    def test_strips_whitespace(self):
        assert validate_instance_id("  openclaw  ") == "openclaw"

    def test_rejects_empty(self):
        with pytest.raises(InstanceError, match="non-empty"):
            validate_instance_id("")
        with pytest.raises(InstanceError, match="non-empty"):
            validate_instance_id("   ")

    def test_rejects_reserved_names(self):
        for name in RESERVED_INSTANCE_NAMES:
            with pytest.raises(InstanceError, match="reserved"):
                validate_instance_id(name)

    def test_rejects_reserved_case_insensitive(self):
        with pytest.raises(InstanceError, match="reserved"):
            validate_instance_id("Shared")
        with pytest.raises(InstanceError, match="reserved"):
            validate_instance_id("CONFIG")

    def test_rejects_invalid_chars(self):
        with pytest.raises(InstanceError, match="invalid"):
            validate_instance_id("my/instance")
        with pytest.raises(InstanceError, match="invalid"):
            validate_instance_id("my instance")
        with pytest.raises(InstanceError, match="invalid"):
            validate_instance_id(".hidden")

    def test_rejects_too_long(self):
        with pytest.raises(InstanceError, match="invalid"):
            validate_instance_id("a" * 65)

    def test_max_length_ok(self):
        name = "a" * 64
        assert validate_instance_id(name) == name

    def test_rejects_starts_with_special(self):
        with pytest.raises(InstanceError, match="invalid"):
            validate_instance_id("-leadinghyphen")
        with pytest.raises(InstanceError, match="invalid"):
            validate_instance_id("_leadingunderscore")


class TestQuaidHome:
    def test_from_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        assert quaid_home() == tmp_path

    def test_default(self, monkeypatch):
        monkeypatch.delenv("QUAID_HOME", raising=False)
        assert quaid_home() == Path.home() / ".quaid"


class TestVisibleHome:
    def test_from_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path))
        assert visible_home() == tmp_path

    def test_default(self, monkeypatch):
        monkeypatch.delenv("QUAID_HOME", raising=False)
        monkeypatch.delenv("QUAID_VISIBLE_HOME", raising=False)
        assert visible_home() == Path.home() / "quaid"

    def test_derives_from_hidden_home(self, monkeypatch, tmp_path):
        hidden = tmp_path / ".quaid-custom"
        monkeypatch.setenv("QUAID_HOME", str(hidden))
        monkeypatch.delenv("QUAID_VISIBLE_HOME", raising=False)
        assert visible_home() == tmp_path / "quaid-custom"


class TestInstanceId:
    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw")
        assert instance_id() == "openclaw"

    def test_missing_raises(self, monkeypatch):
        monkeypatch.delenv("QUAID_INSTANCE", raising=False)
        with pytest.raises(InstanceError, match="QUAID_INSTANCE"):
            instance_id()

    def test_invalid_raises(self, monkeypatch):
        monkeypatch.setenv("QUAID_INSTANCE", "shared")
        with pytest.raises(InstanceError, match="reserved"):
            instance_id()


class TestInstanceRoot:
    def test_resolves(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw")
        assert instance_root() == tmp_path / "instances" / "openclaw"

    def test_visible_instance_root_resolves(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw")
        assert visible_instance_root() == tmp_path / "instances" / "openclaw"


class TestSharedPaths:
    def test_shared_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        assert shared_dir() == tmp_path / "shared"

    def test_shared_projects_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path))
        assert shared_projects_dir() == tmp_path / "projects"

    def test_shared_registry_path(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path))
        assert shared_registry_path() == tmp_path / "project-registry.json"


class TestInstanceExists:
    def test_exists(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        (tmp_path / "instances" / "openclaw").mkdir(parents=True)
        (tmp_path / "instances" / "openclaw" / "config.json").write_text("{}")
        assert instance_exists("openclaw") is True

    def test_not_exists(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        assert instance_exists("nonexistent") is False

    def test_invalid_name(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        assert instance_exists("shared") is False

    def test_internal_path_derived_instance_does_not_exist(self, monkeypatch, tmp_path):
        home = Path("/tmp") / f"qit{os.getpid()}a"
        shutil.rmtree(home, ignore_errors=True)
        try:
            monkeypatch.setenv("QUAID_HOME", str(home))
            internal_instance = next(
                name
                for name in internal_path_derived_instance_ids(home)
                if name.startswith("claude-code-") and name.endswith("-plugins-quaid")
            )
            (home / "instances" / internal_instance).mkdir(parents=True)
            (home / "instances" / internal_instance / "config.json").write_text("{}")

            assert is_internal_path_derived_instance_id(internal_instance, home) is True
            assert instance_exists(internal_instance) is False
        finally:
            shutil.rmtree(home, ignore_errors=True)


class TestListInstances:
    def test_lists_instances(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        for name in ["openclaw", "claude-code"]:
            (tmp_path / "instances" / name).mkdir(parents=True)
            (tmp_path / "instances" / name / "config.json").write_text("{}")
        # Create a hidden dir (should be ignored)
        (tmp_path / "instances" / ".hidden").mkdir(parents=True)
        # Create a dir without config.json (should be ignored)
        (tmp_path / "instances" / "incomplete").mkdir(parents=True)

        result = list_instances()
        assert result == ["claude-code", "openclaw"]

    def test_ignores_internal_path_derived_instances(self, monkeypatch, tmp_path):
        home = Path("/tmp") / f"qit{os.getpid()}b"
        shutil.rmtree(home, ignore_errors=True)
        try:
            monkeypatch.setenv("QUAID_HOME", str(home))
            (home / "instances" / "openclaw-main").mkdir(parents=True)
            (home / "instances" / "openclaw-main" / "config.json").write_text("{}")
            internal_instance = next(
                name
                for name in internal_path_derived_instance_ids(home)
                if name.startswith("codex-") and name.endswith("-plugins-quaid")
            )
            (home / "instances" / internal_instance).mkdir(parents=True)
            (home / "instances" / internal_instance / "config.json").write_text("{}")

            assert list_instances() == ["openclaw-main"]
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_ignores_deleted_openclaw_agent_silos_when_platform_state_is_gone(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        oc_root = tmp_path / "openclaw-runtime"
        oc_root.mkdir()
        monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(oc_root / "openclaw.json"))
        (oc_root / "openclaw.json").write_text(
            json.dumps({
                "agents": {
                    "list": [{"id": "main"}, {"id": "live"}],
                    "deleted": {"workspace": "/tmp/old-deleted-agent"},
                }
            }),
            encoding="utf-8",
        )
        (oc_root / "agents" / "dironly").mkdir(parents=True)

        for name in [
            "openclaw-main",
            "openclaw-live",
            "openclaw-deleted",
            "openclaw-m5silo034449r",
            "claude-code-main",
        ]:
            root = tmp_path / "instances" / name
            root.mkdir(parents=True)
            adapter_type = "openclaw" if name.startswith("openclaw-") else "claude-code"
            root.joinpath("config.json").write_text(
                json.dumps({"adapter": {"type": adapter_type}}),
                encoding="utf-8",
            )

        assert list_instances() == [
            "claude-code-main",
            "openclaw-live",
            "openclaw-main",
        ]
        assert (tmp_path / "instances" / "openclaw-deleted").exists()
        assert (tmp_path / "instances" / "openclaw-m5silo034449r").exists()

    def test_openclaw_physical_prune_requires_livetest_harness(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        oc_root = tmp_path / "openclaw-runtime"
        oc_root.mkdir()
        monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(oc_root / "openclaw.json"))
        (oc_root / "openclaw.json").write_text(
            json.dumps({"agents": {"list": [{"id": "main"}]}}),
            encoding="utf-8",
        )
        deleted_names = ["openclaw-deleted", "openclaw-m5silo034449r"]
        for name in deleted_names:
            deleted = tmp_path / "instances" / name
            deleted.mkdir(parents=True)
            deleted.joinpath("config.json").write_text(
                json.dumps({"adapter": {"type": "openclaw"}}),
                encoding="utf-8",
            )

        with pytest.raises(InstanceError, match="livetest harness"):
            prune_stale_openclaw_agent_instances(tmp_path)
        for name in deleted_names:
            assert (tmp_path / "instances" / name).exists()

        monkeypatch.setenv("QUAID_LIVETEST_HARNESS", "1")
        assert prune_stale_openclaw_agent_instances(tmp_path) == deleted_names
        for name in deleted_names:
            assert not (tmp_path / "instances" / name).exists()

    def test_livetest_prune_removes_empty_instance_bind_point_residue(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        residue = tmp_path / "instances" / "claude-code-private-tmp-cc-livetest-sibling"
        (residue / "data" / "extraction-signals").mkdir(parents=True)
        real = tmp_path / "instances" / "codex-m13test"
        (real / "data" / "extraction-signals").mkdir(parents=True)
        (real / "config.json").write_text(json.dumps({"adapter": {"type": "codex"}}), encoding="utf-8")

        with pytest.raises(InstanceError, match="livetest harness"):
            prune_livetest_instance_residues(tmp_path)
        assert residue.exists()
        assert real.exists()

        monkeypatch.setenv("QUAID_LIVETEST_HARNESS", "1")
        assert prune_livetest_instance_residues(tmp_path) == [residue.name]
        assert not residue.exists()
        assert real.exists()

    def test_empty(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        assert list_instances() == []


class TestRequireInstanceExists:
    def test_exists(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw")
        (tmp_path / "instances" / "openclaw").mkdir(parents=True)
        (tmp_path / "instances" / "openclaw" / "config.json").write_text("{}")
        assert require_instance_exists() == "openclaw"

    def test_not_exists_shows_existing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "missing")
        (tmp_path / "instances" / "openclaw").mkdir(parents=True)
        (tmp_path / "instances" / "openclaw" / "config.json").write_text("{}")
        with pytest.raises(InstanceError, match="openclaw"):
            require_instance_exists()

    def test_explicit_name(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        (tmp_path / "instances" / "work").mkdir(parents=True)
        (tmp_path / "instances" / "work" / "config.json").write_text("{}")
        assert require_instance_exists("work") == "work"
