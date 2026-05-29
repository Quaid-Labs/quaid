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


@pytest.fixture(autouse=True)
def _disable_instance_scope_by_default(monkeypatch):
    monkeypatch.setattr(cli, "_current_instance_id", lambda: "")


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
    def test_main_accepts_json_after_list_subcommand(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["project_registry_cli.py", "list", "--json"])
        with patch("core.project_registry.list_projects", return_value={}):
            cli.main()
        parsed = json.loads(capsys.readouterr().out)
        assert parsed == {}

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
        with patch("core.project_registry.list_projects", return_value=projects), \
             patch("core.project_registry_cli._live_instance_ids", return_value={"claude-code"}):
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

    def test_names_only_outputs_one_project_per_line_without_headers(self, capsys):
        projects = {
            "zeta-proj": {"description": "Z", "instances": []},
            "alpha-proj": {"description": "A", "instances": []},
        }
        with patch("core.project_registry.list_projects", return_value=projects):
            cli.cmd_list(_args(names_only=True))
        out = capsys.readouterr().out
        assert out.splitlines() == ["alpha-proj", "zeta-proj"]

    def test_names_only_empty_projects_emits_empty_stdout(self, capsys):
        with patch("core.project_registry.list_projects", return_value={}):
            cli.cmd_list(_args(names_only=True))
        out = capsys.readouterr().out
        assert out == ""

    def test_list_prunes_stale_instances_using_live_silos(self, capsys):
        projects = {
            "proj": {
                "description": "d",
                "instances": ["alive-instance", "stale-instance"],
            }
        }
        with patch("core.project_registry.list_projects", return_value=projects), \
             patch("core.project_registry_cli._live_instance_ids", return_value={"alive-instance"}):
            cli.cmd_list(_args())
        out = capsys.readouterr().out
        assert "alive-instance" in out
        assert "stale-instance" not in out

    def test_list_filters_projects_to_current_instance(self, capsys):
        projects = {
            "cdx-proj": {
                "description": "CDX",
                "instances": ["codex-private-tmp-cdx-livetest"],
            },
            "cc-proj": {
                "description": "CC",
                "instances": ["claude-code-private-tmp-cc-livetest"],
            },
        }
        with patch("core.project_registry.list_projects", return_value=projects), \
             patch("core.project_registry_cli._current_instance_id", return_value="codex-private-tmp-cdx-livetest"):
            cli.cmd_list(_args(json=True))
        parsed = json.loads(capsys.readouterr().out)
        assert list(parsed) == ["cdx-proj"]


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
        with patch("core.project_registry.create_project", side_effect=ValueError("already exists")), \
             patch("core.project_registry.get_project", return_value=None):
            with pytest.raises(SystemExit) as exc_info:
                cli.cmd_create(_args(name="dup", description=None, source_root=None))
        assert exc_info.value.code == 1
        assert "already exists" in capsys.readouterr().err

    def test_existing_project_error_reports_linked_instances_when_current_instance_hidden(self, capsys):
        project = {
            "description": "fixture",
            "instances": ["standalone-runtime"],
        }
        with patch("core.project_registry.create_project", side_effect=ValueError("Project already exists: dup")), \
             patch("core.project_registry.get_project", return_value=project), \
             patch("core.project_registry_cli._current_instance_id", return_value="cc-livetest"):
            with pytest.raises(SystemExit) as exc_info:
                cli.cmd_create(_args(name="dup", description=None, source_root=None))

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "Project already exists: dup" in err
        assert "Project names are global across Quaid instances" in err
        assert "standalone-runtime" in err
        assert "cc-livetest" in err
        assert "quaid project link dup" in err

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

    def test_show_prunes_stale_instances_using_live_silos(self, capsys):
        project = {"description": "Shown", "instances": ["alive-instance", "stale-instance"]}
        with patch("core.project_registry.get_project", return_value=project), \
             patch("core.project_registry_cli._live_instance_ids", return_value={"alive-instance"}):
            cli.cmd_show(_args(name="my-proj"))
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert parsed["my-proj"]["instances"] == ["alive-instance"]

    def test_show_rejects_project_not_linked_to_current_instance(self, capsys):
        project = {"description": "CDX", "instances": ["codex-private-tmp-cdx-livetest"]}
        with patch("core.project_registry.get_project", return_value=project), \
             patch("core.project_registry_cli._current_instance_id", return_value="claude-code-private-tmp-cc-livetest"):
            with pytest.raises(SystemExit) as exc_info:
                cli.cmd_show(_args(name="cdx-proj"))
        assert exc_info.value.code == 1
        assert "cdx-proj" in capsys.readouterr().err

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
        with patch("core.project_registry.get_project", return_value={"instances": []}):
            with pytest.raises(SystemExit) as exc_info:
                cli.cmd_update(_args(name="proj", description=None, source_root=None))
        assert exc_info.value.code == 1

    def test_update_description_prints_confirmation(self, capsys):
        entry = {"description": "New", "instances": []}
        with patch("core.project_registry.get_project", return_value={"instances": []}), \
             patch("core.project_registry.update_project", return_value=entry):
            cli.cmd_update(_args(name="proj", description="New", source_root=None))
        out = capsys.readouterr().out
        assert "Updated project: proj" in out

    def test_update_source_root_only(self, capsys):
        entry = {"description": "", "instances": []}
        with patch("core.project_registry.get_project", return_value={"instances": []}), \
             patch("core.project_registry.update_project", return_value=entry) as m:
            cli.cmd_update(_args(name="proj", description=None, source_root="/new/path"))
        m.assert_called_once_with("proj", source_root="/new/path")

    def test_json_flag_prints_entry(self, capsys):
        entry = {"description": "Z", "instances": []}
        with patch("core.project_registry.get_project", return_value={"instances": []}), \
             patch("core.project_registry.update_project", return_value=entry):
            cli.cmd_update(_args(name="proj", description="Z", source_root=None, json=True))
        out = capsys.readouterr().out
        lines = out.strip().splitlines()
        json_output = "\n".join(lines[1:])
        parsed = json.loads(json_output)
        assert parsed["description"] == "Z"

    def test_keyerror_exits_with_one(self, capsys):
        with patch("core.project_registry.get_project", return_value={"instances": []}), \
             patch("core.project_registry.update_project", side_effect=KeyError("not found")):
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

    def test_reserved_project_exits_with_one(self, capsys):
        with patch(
            "core.project_registry.unlink_project",
            side_effect=ValueError("Cannot unlink reserved project: quaid"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                cli.cmd_unlink(_args(name="quaid"))
        assert exc_info.value.code == 1
        assert "Cannot unlink reserved project: quaid" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_delete
# ---------------------------------------------------------------------------


class TestCmdDelete:
    def test_delete_prints_confirmation(self, capsys):
        with patch("core.project_registry.get_project", return_value={"instances": []}), \
             patch("core.project_registry.delete_project"):
            cli.cmd_delete(_args(name="old-proj"))
        out = capsys.readouterr().out
        assert "Deleted project: old-proj" in out

    def test_delete_rejects_project_not_linked_to_current_instance(self, capsys):
        project = {"description": "CDX", "instances": ["codex-private-tmp-cdx-livetest"]}
        with patch("core.project_registry.get_project", return_value=project), \
             patch("core.project_registry.delete_project") as delete_project, \
             patch("core.project_registry_cli._current_instance_id", return_value="claude-code-private-tmp-cc-livetest"):
            with pytest.raises(SystemExit) as exc_info:
                cli.cmd_delete(_args(name="cdx-proj"))
        assert exc_info.value.code == 1
        delete_project.assert_not_called()
        assert "cdx-proj" in capsys.readouterr().err

    def test_keyerror_exits_with_one(self, capsys):
        with patch("core.project_registry.get_project", return_value={"instances": []}), \
             patch("core.project_registry.delete_project", side_effect=KeyError("not found")):
            with pytest.raises(SystemExit) as exc_info:
                cli.cmd_delete(_args(name="ghost"))
        assert exc_info.value.code == 1

    def test_reserved_project_exits_with_one(self, capsys):
        with patch("core.project_registry.get_project", return_value={"instances": []}), \
             patch(
                 "core.project_registry.delete_project",
                 side_effect=ValueError("Cannot delete reserved project: quaid"),
             ):
            with pytest.raises(SystemExit) as exc_info:
                cli.cmd_delete(_args(name="quaid"))
        assert exc_info.value.code == 1
        assert "Cannot delete reserved project: quaid" in capsys.readouterr().err

    def test_reserved_project_bypasses_visibility_check(self, capsys):
        with patch("core.project_registry.get_project", side_effect=AssertionError("visibility check should be skipped")), \
             patch(
                 "core.project_registry.delete_project",
                 side_effect=ValueError("Cannot delete reserved project: quaid"),
             ):
            with pytest.raises(SystemExit) as exc_info:
                cli.cmd_delete(_args(name="quaid"))
        assert exc_info.value.code == 1
        assert "Cannot delete reserved project: quaid" in capsys.readouterr().err


class TestCmdStatus:
    def test_status_prints_project_docs_status(self, capsys):
        status = {"project": "proj", "status": "fresh"}
        with patch("core.project_registry.get_project", return_value={"instances": []}), \
             patch("core.project_docs.project_status", return_value=status), \
             patch("core.project_docs.format_status", return_value="Project: proj\nStatus: fresh"):
            cli.cmd_status(_args(name="proj"))
        out = capsys.readouterr().out
        assert "Status: fresh" in out

    def test_status_json(self, capsys):
        with patch("core.project_registry.get_project", return_value={"instances": []}), \
             patch("core.project_docs.project_status", return_value={"project": "proj", "status": "stale"}):
            cli.cmd_status(_args(name="proj", json=True))
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["status"] == "stale"

    def test_status_not_found_exits(self, capsys):
        with patch("core.project_registry.get_project", return_value={"instances": []}), \
             patch("core.project_docs.project_status", side_effect=KeyError("not found")):
            with pytest.raises(SystemExit) as exc_info:
                cli.cmd_status(_args(name="ghost"))
        assert exc_info.value.code == 1


class TestCmdDiff:
    def test_diff_prints_project_docs_diff(self, capsys):
        diff = {"project": "proj", "change_count": 1, "changes": []}
        with patch("core.project_registry.get_project", return_value={"instances": []}), \
             patch("core.project_docs.project_diff", return_value=diff) as m, \
             patch("core.project_docs.format_diff", return_value="Source changes: 1"):
            cli.cmd_diff(_args(name="proj", full=False))
        m.assert_called_once_with("proj", full=False)
        assert "Source changes: 1" in capsys.readouterr().out

    def test_diff_full_flag(self, capsys):
        with patch("core.project_registry.get_project", return_value={"instances": []}), \
             patch("core.project_docs.project_diff", return_value={"project": "proj"}) as m, \
             patch("core.project_docs.format_diff", return_value="ok"):
            cli.cmd_diff(_args(name="proj", full=True))
        m.assert_called_once_with("proj", full=True)


class TestCmdShadowGitRecovery:
    def test_history_json_prints_shadow_commits(self, capsys):
        sg = MagicMock()
        sg.history.return_value = [
            {
                "commit": "abc123",
                "short": "abc123",
                "committed_at": "2026-05-12T00:00:00+00:00",
                "subject": "snapshot",
            }
        ]
        with patch("core.project_registry_cli._shadow_git_for_visible_project", return_value=sg):
            cli.cmd_history(_args(name="proj", file="notes.md", limit=3, json=True))

        sg.history.assert_called_once_with("notes.md", limit=3)
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["project"] == "proj"
        assert parsed["file"] == "notes.md"
        assert parsed["history"][0]["commit"] == "abc123"

    def test_show_version_writes_file_bytes(self, capsys):
        sg = MagicMock()
        sg.show_file.return_value = b"previous contents\n"
        with patch("core.project_registry_cli._shadow_git_for_visible_project", return_value=sg):
            cli.cmd_show_version(_args(name="proj", rev="abc123", file="notes.md"))

        sg.show_file.assert_called_once_with("abc123", "notes.md")
        assert capsys.readouterr().out == "previous contents\n"

    def test_restore_requires_yes_when_noninteractive(self, capsys):
        sg = MagicMock()
        with patch("core.project_registry_cli._shadow_git_for_visible_project", return_value=sg), \
             patch("sys.stdin.isatty", return_value=False):
            with pytest.raises(SystemExit) as exc_info:
                cli.cmd_restore(_args(name="proj", rev="abc123", file="notes.md", yes=False, json=False))

        assert exc_info.value.code == 1
        sg.restore_file.assert_not_called()
        assert "--yes is required" in capsys.readouterr().err

    def test_restore_with_yes_calls_shadow_restore(self, tmp_path, capsys):
        restored = tmp_path / "notes.md"
        sg = MagicMock()
        sg.restore_file.return_value = restored
        with patch("core.project_registry_cli._shadow_git_for_visible_project", return_value=sg):
            cli.cmd_restore(_args(name="proj", rev="abc123", file="notes.md", yes=True, json=False))

        sg.restore_file.assert_called_once_with("abc123", "notes.md")
        assert "Restored notes.md" in capsys.readouterr().out
