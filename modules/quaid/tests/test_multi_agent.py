"""Unit tests for multi-agent support: prefix derivation, instance ID resolution,
list_agent_instance_ids, agent_instance_root, naming convention, and CLI registration.
"""

import json
import os
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cc_adapter(tmp_path: Path):
    from adaptors.claude_code.adapter import ClaudeCodeAdapter
    return ClaudeCodeAdapter(home=tmp_path)


def _make_oc_adapter(tmp_path: Path):
    """Build an OpenClawAdapter with a tmp quaid_home.

    CLAWDBOT_WORKSPACE is normally required by oc_workspace(); patch it to
    a tmp dir so we can construct the adapter without a real OC install.
    """
    from adaptors.openclaw.adapter import OpenClawAdapter

    class _OcAdapterWithHome(OpenClawAdapter):
        def __init__(self, home: Path):
            self._home = home

        def quaid_home(self) -> Path:
            return self._home

    return _OcAdapterWithHome(tmp_path)


# ---------------------------------------------------------------------------
# 1. Prefix derivation
# ---------------------------------------------------------------------------

class TestPrefixDerivation:
    def test_cc_adapter_prefix_is_adapter_id(self, tmp_path):
        adapter = _make_cc_adapter(tmp_path)
        assert adapter.agent_id_prefix() == "claude-code"

    def test_oc_adapter_prefix_is_adapter_id(self, tmp_path):
        adapter = _make_oc_adapter(tmp_path)
        assert adapter.agent_id_prefix() == "openclaw"

    def test_base_adapter_strips_main_suffix(self, monkeypatch, tmp_path):
        """Base implementation: 'openclaw-main' → 'openclaw'."""
        from lib.adapter import StandaloneAdapter
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
        adapter = StandaloneAdapter(home=tmp_path)
        assert adapter.agent_id_prefix() == "openclaw"

    def test_base_adapter_no_main_suffix_unchanged(self, monkeypatch, tmp_path):
        """Base implementation: 'myapp' (no -main) → 'myapp'."""
        from lib.adapter import StandaloneAdapter
        monkeypatch.setenv("QUAID_INSTANCE", "myapp")
        adapter = StandaloneAdapter(home=tmp_path)
        assert adapter.agent_id_prefix() == "myapp"

    def test_base_adapter_non_main_suffix_unchanged(self, monkeypatch, tmp_path):
        """Base implementation: 'openclaw-coding' → 'openclaw-coding' (not stripped)."""
        from lib.adapter import StandaloneAdapter
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-coding")
        adapter = StandaloneAdapter(home=tmp_path)
        assert adapter.agent_id_prefix() == "openclaw-coding"


# ---------------------------------------------------------------------------
# 2. Instance ID resolution (via InstanceManager)
# ---------------------------------------------------------------------------

class TestInstanceIdResolution:
    def _cc_mgr(self, tmp_path):
        from adaptors.claude_code.instance_manager import ClaudeCodeInstanceManager
        adapter = _make_cc_adapter(tmp_path)
        return ClaudeCodeInstanceManager(adapter)

    def _oc_mgr(self, tmp_path):
        from lib.instance_manager import InstanceManager
        adapter = _make_oc_adapter(tmp_path)
        return InstanceManager(adapter)

    def test_cc_resolve_main(self, tmp_path):
        mgr = self._cc_mgr(tmp_path)
        assert mgr.resolve_instance_id("main") == "claude-code-main"

    def test_cc_resolve_custom_label(self, tmp_path):
        mgr = self._cc_mgr(tmp_path)
        assert mgr.resolve_instance_id("myapp") == "claude-code-myapp"

    def test_cc_resolve_normalises_to_lowercase(self, tmp_path):
        mgr = self._cc_mgr(tmp_path)
        assert mgr.resolve_instance_id("MYAPP") == "claude-code-myapp"

    def test_cc_resolve_empty_label_raises(self, tmp_path):
        mgr = self._cc_mgr(tmp_path)
        with pytest.raises(ValueError, match="non-empty"):
            mgr.resolve_instance_id("")

    def test_oc_resolve_main(self, tmp_path):
        mgr = self._oc_mgr(tmp_path)
        assert mgr.resolve_instance_id("main") == "openclaw-main"

    def test_oc_resolve_custom_label(self, tmp_path):
        mgr = self._oc_mgr(tmp_path)
        assert mgr.resolve_instance_id("coding") == "openclaw-coding"


# ---------------------------------------------------------------------------
# 3. list_agent_instance_ids / is_multi_agent
# ---------------------------------------------------------------------------

class TestListAgentInstanceIds:
    def test_cc_returns_current_instance_main(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_INSTANCE", "claude-code-main")
        adapter = _make_cc_adapter(tmp_path)
        ids = adapter.list_agent_instance_ids()
        assert ids == ["claude-code-main"]

    def test_cc_returns_current_instance_project(self, monkeypatch, tmp_path):
        monkeypatch.setenv("QUAID_INSTANCE", "claude-code-myproject")
        adapter = _make_cc_adapter(tmp_path)
        ids = adapter.list_agent_instance_ids()
        assert ids == ["claude-code-myproject"]

    def test_oc_is_multi_agent(self, tmp_path):
        adapter = _make_oc_adapter(tmp_path)
        assert adapter.is_multi_agent() is True

    def test_cc_is_not_multi_agent(self, tmp_path):
        adapter = _make_cc_adapter(tmp_path)
        assert adapter.is_multi_agent() is False

    def test_oc_list_ids_all_start_with_prefix(self, tmp_path):
        """OC list_agent_instance_ids() must return strings all starting with 'openclaw-'."""
        adapter = _make_oc_adapter(tmp_path)
        prefix = adapter.agent_id_prefix()

        # Provide a mock openclaw.json so the adapter reads agent labels from it.
        oc_json_content = json.dumps({
            "agents": {
                "list": [
                    {"id": "main"},
                    {"id": "coding"},
                    {"id": "work"},
                ]
            }
        })

        fake_cfg_path = tmp_path / "openclaw.json"
        fake_cfg_path.write_text(oc_json_content, encoding="utf-8")

        with patch.object(adapter, "get_gateway_config_path", return_value=fake_cfg_path):
            ids = adapter.list_agent_instance_ids()

        assert len(ids) >= 1
        for iid in ids:
            assert iid.startswith(f"{prefix}-"), (
                f"Instance ID '{iid}' does not start with prefix '{prefix}-'"
            )

    def test_oc_list_ids_fallback_to_main_when_no_config(self, tmp_path):
        """When no openclaw.json exists, list returns ['openclaw-main']."""
        adapter = _make_oc_adapter(tmp_path)
        with patch.object(adapter, "get_gateway_config_path", return_value=None):
            ids = adapter.list_agent_instance_ids()
        assert ids == ["openclaw-main"]

    def test_oc_list_ids_main_is_first(self, tmp_path):
        """When agents list includes 'main', it must appear first."""
        adapter = _make_oc_adapter(tmp_path)
        oc_json_content = json.dumps({
            "agents": {
                "list": [
                    {"id": "coding"},
                    {"id": "main"},
                    {"id": "work"},
                ]
            }
        })
        fake_cfg_path = tmp_path / "openclaw.json"
        fake_cfg_path.write_text(oc_json_content, encoding="utf-8")

        with patch.object(adapter, "get_gateway_config_path", return_value=fake_cfg_path):
            ids = adapter.list_agent_instance_ids()

        assert ids[0] == "openclaw-main"


# ---------------------------------------------------------------------------
# 4. agent_instance_root
# ---------------------------------------------------------------------------

class TestAgentInstanceRoot:
    def test_cc_instance_root(self, tmp_path):
        adapter = _make_cc_adapter(tmp_path)
        root = adapter.agent_instance_root("claude-code-myapp")
        assert root == tmp_path / "claude-code-myapp"

    def test_oc_instance_root(self, tmp_path):
        adapter = _make_oc_adapter(tmp_path)
        root = adapter.agent_instance_root("openclaw-coding")
        assert root == tmp_path / "openclaw-coding"


# ---------------------------------------------------------------------------
# 5. Naming convention consistency (round-trip)
# ---------------------------------------------------------------------------

class TestNamingConvention:
    def _strip_prefix(self, full_id: str, prefix: str) -> str:
        """Strip '<prefix>-' from the beginning of full_id."""
        sep = f"{prefix}-"
        assert full_id.startswith(sep), f"'{full_id}' does not start with '{sep}'"
        return full_id[len(sep):]

    def test_cc_round_trip(self, tmp_path):
        """resolve_instance_id(label) → strip prefix → get back label."""
        from adaptors.claude_code.instance_manager import ClaudeCodeInstanceManager
        adapter = _make_cc_adapter(tmp_path)
        mgr = ClaudeCodeInstanceManager(adapter)
        prefix = adapter.agent_id_prefix()
        for label in ("main", "myapp", "work"):
            full_id = mgr.resolve_instance_id(label)
            recovered = self._strip_prefix(full_id, prefix)
            assert recovered == label, (
                f"Round-trip failed: resolve('{label}') → '{full_id}' → strip → '{recovered}'"
            )

    def test_oc_round_trip(self, tmp_path):
        """resolve_instance_id(label) → strip prefix → get back label."""
        from lib.instance_manager import InstanceManager
        adapter = _make_oc_adapter(tmp_path)
        mgr = InstanceManager(adapter)
        prefix = adapter.agent_id_prefix()
        for label in ("main", "coding", "work"):
            full_id = mgr.resolve_instance_id(label)
            recovered = self._strip_prefix(full_id, prefix)
            assert recovered == label

    def test_cc_list_ids_all_start_with_prefix(self, monkeypatch, tmp_path):
        """list_agent_instance_ids() IDs must all start with agent_id_prefix() + '-'."""
        monkeypatch.setenv("QUAID_INSTANCE", "claude-code-main")
        adapter = _make_cc_adapter(tmp_path)
        prefix = adapter.agent_id_prefix()
        for iid in adapter.list_agent_instance_ids():
            assert iid.startswith(f"{prefix}-")

    def test_oc_list_ids_all_start_with_prefix(self, tmp_path):
        """OC list_agent_instance_ids() IDs must all start with agent_id_prefix() + '-'."""
        adapter = _make_oc_adapter(tmp_path)
        prefix = adapter.agent_id_prefix()
        fake_cfg = tmp_path / "openclaw.json"
        fake_cfg.write_text(
            json.dumps({"agents": {"list": [{"id": "main"}, {"id": "coding"}]}}),
            encoding="utf-8",
        )
        with patch.object(adapter, "get_gateway_config_path", return_value=fake_cfg):
            ids = adapter.list_agent_instance_ids()
        for iid in ids:
            assert iid.startswith(f"{prefix}-")


# ---------------------------------------------------------------------------
# 6. CC adapter CLI registration
# ---------------------------------------------------------------------------

class TestCCAdapterCLIRegistration:
    def test_get_cli_namespace(self, tmp_path):
        adapter = _make_cc_adapter(tmp_path)
        assert adapter.get_cli_namespace() == "claudecode"

    def test_get_cli_commands_has_make_instance(self, tmp_path):
        adapter = _make_cc_adapter(tmp_path)
        cmds = adapter.get_cli_commands()
        assert "make_instance" in cmds
        assert callable(cmds["make_instance"])

    def test_get_cli_tools_snippet_contains_make_instance(self, tmp_path):
        adapter = _make_cc_adapter(tmp_path)
        snippet = adapter.get_cli_tools_snippet()
        assert "make_instance" in snippet

    def test_get_cli_tools_snippet_contains_claudecode(self, tmp_path):
        adapter = _make_cc_adapter(tmp_path)
        snippet = adapter.get_cli_tools_snippet()
        assert "claudecode" in snippet

    def test_get_cli_tools_snippet_contains_quaid_instance(self, tmp_path):
        adapter = _make_cc_adapter(tmp_path)
        snippet = adapter.get_cli_tools_snippet()
        assert "QUAID_INSTANCE" in snippet

    def test_get_instance_manager_returns_cc_type(self, tmp_path):
        from adaptors.claude_code.instance_manager import ClaudeCodeInstanceManager
        adapter = _make_cc_adapter(tmp_path)
        mgr = adapter.get_instance_manager()
        assert isinstance(mgr, ClaudeCodeInstanceManager)


# ---------------------------------------------------------------------------
# 7. OC adapter CLI registration
# ---------------------------------------------------------------------------

class TestOCAdapterCLIRegistration:
    def test_oc_get_cli_namespace_is_none(self, tmp_path):
        """OC manages instances at install time — no CLI namespace needed."""
        adapter = _make_oc_adapter(tmp_path)
        assert adapter.get_cli_namespace() is None

    def test_oc_get_instance_manager_is_none(self, tmp_path):
        """OC instances are created at install time, not by CLI command."""
        adapter = _make_oc_adapter(tmp_path)
        assert adapter.get_instance_manager() is None
