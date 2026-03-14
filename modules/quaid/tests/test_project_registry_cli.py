"""Unit tests for core/project_registry_cli.py.

Covers each command function (cmd_list, cmd_create, cmd_show, cmd_update,
cmd_link, cmd_unlink, cmd_delete) with mocked underlying registry calls.
Tests: output formatting, --json flag, error paths (sys.exit(1)).
"""

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import core.project_registry_cli as cli


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _args(**kwargs):
    """Build a minimal SimpleNamespace args object."""
    defaults = {"json": False}
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# cmd_list
# ---------------------------------------------------------------------------


class TestCmdList:
    def test_empty_projects_prints_message(self, capsys):
        with patch("core.project_registry.list_projects", return_value={}):
            cli.cmd_list(_args())
        out = capsys.readouterr().out
        assert "No projects registered" in out

    def test_projects_printed_with_name_description_instances(self, capsys):
        projects = {
            "my-proj": {
                "description": "My test project",
                "source_root": "/tmp/src",
                "instances": ["claude-code"],
            }
        }
        with patch("core.project_registry.list_projects", return_value=projects):
            cli.cmd_list(_args())
        out = capsys.readouterr().out
        assert "my-proj" in out
        assert "My test project" in out
        assert "/tmp/src" in out
        assert "claude-code" in out

    def test_missing_source_root_shows_no_source_root(self, capsys):
        projects = {
            "bare-proj": {
                "description": "",
                "instances": [],
            }
        }
        with patch("core.project_registry.list_projects", return_value=projects):
            cli.cmd_list(_args())
        out = capsys.readouterr().out
        assert "no source root" in out

    def test_json_flag_prints_json(self, capsys):
        projects = {"proj": {"description": "d", "instances": []}}
        with patch("core.project_registry.list_projects", return_value=projects):
            cli.cmd_list(_args(json=True))
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert "proj" in parsed


# ---------------------------------------------------------------------------
# cmd_create
# ---------------------------------------------------------------------------


class TestCmdCreate:
    def test_creates_project_and_prints_confirmation(self, capsys):
        entry = {"description": "A project", "instances": ["claude-code"]}
        with patch("core.project_registry.create_project", return_value=entry):
            cli.cmd_create(_args(name="new-proj", description="A project", source_root=None))
        out = capsys.readouterr().out
        assert "Created project: new-proj" in out

    def test_json_flag_prints_entry(self, capsys):
        entry = {"description": "X", "instances": []}
        with patch("core.project_registry.create_project", return_value=entry):
            cli.cmd_create(_args(name="proj-x", description="X", source_root=None, json=True))
        out = capsys.readouterr().out
        # JSON should contain the entry (after "Created project: ..." line)
        lines = out.strip().splitlines()
        json_output = "\n".join(lines[1:])
        parsed = json.loads(json_output)
        assert parsed["description"] == "X"

    def test_valueerror_exits_with_one(self, capsys):
        with patch("core.project_registry.create_project", side_effect=ValueError("already exists")):
            with pytest.raises(SystemExit) as exc_info:
                cli.cmd_create(_args(name="dup", description=None, source_root=None))
        assert exc_info.value.code == 1
        assert "already exists" in capsys.readouterr().err

    def test_keyerror_exits_with_one(self, capsys):
        with patch("core.project_registry.create_project", side_effect=KeyError("bad")):
            with pytest.raises(SystemExit) as exc_info:
                cli.cmd_create(_args(name="dup", description=None, source_root=None))
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# cmd_show
# ---------------------------------------------------------------------------


class TestCmdShow:
    def test_found_project_prints_json(self, capsys):
        project = {"description": "Shown", "instances": ["claude-code"]}
        with patch("core.project_registry.get_project", return_value=project):
            cli.cmd_show(_args(name="my-proj"))
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert parsed["my-proj"]["description"] == "Shown"

    def test_not_found_exits_with_one(self, capsys):
        with patch("core.project_registry.get_project", return_value=None):
            with pytest.raises(SystemExit) as exc_info:
                cli.cmd_show(_args(name="ghost"))
        assert exc_info.value.code == 1
        assert "ghost" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_update
# ---------------------------------------------------------------------------


class TestCmdUpdate:
    def test_nothing_to_update_exits_with_one(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            cli.cmd_update(_args(name="proj", description=None, source_root=None))
        assert exc_info.value.code == 1

    def test_update_description_prints_confirmation(self, capsys):
        entry = {"description": "New", "instances": []}
        with patch("core.project_registry.update_project", return_value=entry):
            cli.cmd_update(_args(name="proj", description="New", source_root=None))
        out = capsys.readouterr().out
        assert "Updated project: proj" in out

    def test_update_source_root_only(self, capsys):
        entry = {"description": "", "instances": []}
        with patch("core.project_registry.update_project", return_value=entry) as m:
            cli.cmd_update(_args(name="proj", description=None, source_root="/new/path"))
        m.assert_called_once_with("proj", source_root="/new/path")

    def test_json_flag_prints_entry(self, capsys):
        entry = {"description": "Z", "instances": []}
        with patch("core.project_registry.update_project", return_value=entry):
            cli.cmd_update(_args(name="proj", description="Z", source_root=None, json=True))
        out = capsys.readouterr().out
        lines = out.strip().splitlines()
        json_output = "\n".join(lines[1:])
        parsed = json.loads(json_output)
        assert parsed["description"] == "Z"

    def test_keyerror_exits_with_one(self, capsys):
        with patch("core.project_registry.update_project", side_effect=KeyError("not found")):
            with pytest.raises(SystemExit) as exc_info:
                cli.cmd_update(_args(name="ghost", description="x", source_root=None))
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# cmd_link
# ---------------------------------------------------------------------------


class TestCmdLink:
    def test_link_prints_instances(self, capsys):
        entry = {"instances": ["openclaw", "claude-code"]}
        with patch("core.project_registry.link_project", return_value=entry):
            cli.cmd_link(_args(name="proj"))
        out = capsys.readouterr().out
        assert "proj" in out
        assert "openclaw" in out
        assert "claude-code" in out

    def test_json_flag_prints_entry(self, capsys):
        entry = {"instances": ["openclaw"]}
        with patch("core.project_registry.link_project", return_value=entry):
            cli.cmd_link(_args(name="proj", json=True))
        out = capsys.readouterr().out
        lines = out.strip().splitlines()
        json_output = "\n".join(lines[1:])
        parsed = json.loads(json_output)
        assert "openclaw" in parsed["instances"]

    def test_keyerror_exits_with_one(self, capsys):
        with patch("core.project_registry.link_project", side_effect=KeyError("not found")):
            with pytest.raises(SystemExit) as exc_info:
                cli.cmd_link(_args(name="ghost"))
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# cmd_unlink
# ---------------------------------------------------------------------------


class TestCmdUnlink:
    def test_unlink_prints_remaining_instances(self, capsys):
        entry = {"instances": ["openclaw"]}
        with patch("core.project_registry.unlink_project", return_value=entry):
            cli.cmd_unlink(_args(name="proj"))
        out = capsys.readouterr().out
        assert "proj" in out
        assert "openclaw" in out

    def test_unlink_empty_instances_shows_none(self, capsys):
        entry = {"instances": []}
        with patch("core.project_registry.unlink_project", return_value=entry):
            cli.cmd_unlink(_args(name="proj"))
        out = capsys.readouterr().out
        assert "(none)" in out

    def test_keyerror_exits_with_one(self, capsys):
        with patch("core.project_registry.unlink_project", side_effect=KeyError("not found")):
            with pytest.raises(SystemExit) as exc_info:
                cli.cmd_unlink(_args(name="ghost"))
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# cmd_delete
# ---------------------------------------------------------------------------


class TestCmdDelete:
    def test_delete_prints_confirmation(self, capsys):
        with patch("core.project_registry.delete_project"):
            cli.cmd_delete(_args(name="old-proj"))
        out = capsys.readouterr().out
        assert "Deleted project: old-proj" in out

    def test_keyerror_exits_with_one(self, capsys):
        with patch("core.project_registry.delete_project", side_effect=KeyError("not found")):
            with pytest.raises(SystemExit) as exc_info:
                cli.cmd_delete(_args(name="ghost"))
        assert exc_info.value.code == 1
