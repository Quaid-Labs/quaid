"""Tests for lib/instance.py — instance resolution and validation."""

import os
import pytest
from pathlib import Path

from lib.instance import (
    InstanceError,
    RESERVED_INSTANCE_NAMES,
    validate_instance_id,
    quaid_home,
    instance_id,
    instance_root,
    shared_dir,
    shared_projects_dir,
    shared_registry_path,
    instance_exists,
    list_instances,
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
        assert quaid_home() == Path.home() / "quaid"


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
        assert instance_root() == tmp_path / "openclaw"


class TestSharedPaths:
    def test_shared_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        assert shared_dir() == tmp_path / "shared"

    def test_shared_projects_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        assert shared_projects_dir() == tmp_path / "shared" / "projects"

    def test_shared_registry_path(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        assert shared_registry_path() == tmp_path / "shared" / "project-registry.json"


class TestInstanceExists:
    def test_exists(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        (tmp_path / "openclaw" / "config").mkdir(parents=True)
        (tmp_path / "openclaw" / "config" / "memory.json").write_text("{}")
        assert instance_exists("openclaw") is True

    def test_not_exists(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        assert instance_exists("nonexistent") is False

    def test_invalid_name(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        assert instance_exists("shared") is False


class TestListInstances:
    def test_lists_instances(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        for name in ["openclaw", "claude-code"]:
            (tmp_path / name / "config").mkdir(parents=True)
            (tmp_path / name / "config" / "memory.json").write_text("{}")
        # Create a reserved dir (should be ignored)
        (tmp_path / "shared").mkdir()
        # Create a dir without config (should be ignored)
        (tmp_path / "incomplete").mkdir()

        result = list_instances()
        assert result == ["claude-code", "openclaw"]

    def test_empty(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        assert list_instances() == []


class TestRequireInstanceExists:
    def test_exists(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw")
        (tmp_path / "openclaw" / "config").mkdir(parents=True)
        (tmp_path / "openclaw" / "config" / "memory.json").write_text("{}")
        assert require_instance_exists() == "openclaw"

    def test_not_exists_shows_existing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "missing")
        (tmp_path / "openclaw" / "config").mkdir(parents=True)
        (tmp_path / "openclaw" / "config" / "memory.json").write_text("{}")
        with pytest.raises(InstanceError, match="openclaw"):
            require_instance_exists()

    def test_explicit_name(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        (tmp_path / "work" / "config").mkdir(parents=True)
        (tmp_path / "work" / "config" / "memory.json").write_text("{}")
        assert require_instance_exists("work") == "work"
