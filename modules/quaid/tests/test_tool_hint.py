"""Tests for plan_tool_hint() in memory_graph.py."""

import os
import sys
import json
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["MEMORY_DB_PATH"] = ":memory:"

import pytest

from datastore.memorydb.memory_graph import plan_tool_hint
from lib.command_registry import COMMAND_REGISTRY, resolve_command_registry


# ---------------------------------------------------------------------------
# Test registry fixture — minimal resolved entries, no real paths
# ---------------------------------------------------------------------------

_COMMANDS = [
    {
        "id": "misc_project",
        "description": "Throwaway, temp, quick, or hello-world files and scripts",
        "hint": "Throwaway file — write to: /quaid/shared/projects/misc--test",
    },
    {
        "id": "create_project",
        "description": (
            "Durable work: essays, articles, reports, research notes, blog posts, "
            "video scripts, screenplays, travel plans, trip itineraries, project plans"
        ),
        "hint": "Durable work — create a project first: quaid registry create-project <name>",
    },
    {
        "id": "recall",
        "description": "Searching or recalling memories, facts, preferences, past conversations",
        "hint": 'Search memories: quaid recall "your query"',
    },
    {
        "id": "store",
        "description": "Explicitly storing or saving a new fact, preference, decision, or memory",
        "hint": 'Store memory: quaid store "the fact"',
    },
]


def _llm_returns(command_id):
    """Return a mock that simulates call_fast_reasoning returning a command_id."""
    def _call(*args, **kwargs):
        return json.dumps({"command_id": command_id}), {}
    return _call


def _llm_returns_null(*args, **kwargs):
    return json.dumps({"command_id": None}), {}


def _llm_raises(*args, **kwargs):
    raise RuntimeError("LLM unavailable")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPlanToolHint:

    def test_throwaway_file_query_returns_hint(self):
        """A throwaway-file message should produce a misc-project hint."""
        with patch("lib.llm_clients.call_fast_reasoning", _llm_returns("misc_project")):
            result = plan_tool_hint(
                "Can you write a quick hello world script? Just put it somewhere temporary.",
                commands=_COMMANDS,
            )
        assert result is not None
        assert result.startswith("<tool_hint>")
        assert result.endswith("</tool_hint>")
        assert "misc" in result

    def test_unrelated_query_returns_none_when_llm_says_null(self):
        """A query with no command match should return None."""
        with patch("lib.llm_clients.call_fast_reasoning", _llm_returns_null):
            result = plan_tool_hint(
                "What is the capital of France?",
                commands=_COMMANDS,
            )
        assert result is None

    def test_durable_project_query_returns_hint(self):
        """A multi-file build request should hint at project creation."""
        with patch("lib.llm_clients.call_fast_reasoning", _llm_returns("create_project")):
            result = plan_tool_hint(
                "I want to build a proper CLI tool with tests and a README.",
                commands=_COMMANDS,
            )
        assert result is not None
        assert "create-project" in result or "project" in result.lower()

    def test_essay_query_returns_hint(self):
        """An essay-writing request should hint at project creation."""
        with patch("lib.llm_clients.call_fast_reasoning", _llm_returns("create_project")):
            result = plan_tool_hint(
                "Help me write an essay about the history of the Roman Empire.",
                commands=_COMMANDS,
            )
        assert result is not None
        assert "project" in result.lower()

    def test_video_script_query_returns_hint(self):
        """A video script request should hint at project creation."""
        with patch("lib.llm_clients.call_fast_reasoning", _llm_returns("create_project")):
            result = plan_tool_hint(
                "Write a YouTube video script for a 10-minute explainer on black holes.",
                commands=_COMMANDS,
            )
        assert result is not None
        assert "project" in result.lower()

    def test_travel_plan_query_returns_hint(self):
        """A travel plan request should hint at project creation."""
        with patch("lib.llm_clients.call_fast_reasoning", _llm_returns("create_project")):
            result = plan_tool_hint(
                "Help me plan a 2-week trip to Japan including hotels and activities.",
                commands=_COMMANDS,
            )
        assert result is not None
        assert "project" in result.lower()

    def test_research_notes_query_returns_hint(self):
        """A research notes request should hint at project creation."""
        with patch("lib.llm_clients.call_fast_reasoning", _llm_returns("create_project")):
            result = plan_tool_hint(
                "I need to write up research notes on quantum computing for my thesis.",
                commands=_COMMANDS,
            )
        assert result is not None
        assert "project" in result.lower()

    def test_recall_query_returns_hint(self):
        """A memory recall request should hint at quaid recall."""
        with patch("lib.llm_clients.call_fast_reasoning", _llm_returns("recall")):
            result = plan_tool_hint(
                "What do you remember about my diet preferences?",
                commands=_COMMANDS,
            )
        assert result is not None
        assert "recall" in result.lower()

    def test_store_query_returns_hint(self):
        """A memory store request should hint at quaid store."""
        with patch("lib.llm_clients.call_fast_reasoning", _llm_returns("store")):
            result = plan_tool_hint(
                "Remember that I prefer dark mode in all my editors.",
                commands=_COMMANDS,
            )
        assert result is not None
        assert "store" in result.lower()

    def test_empty_query_returns_none(self):
        """Empty or whitespace query must return None without calling the LLM."""
        with patch("lib.llm_clients.call_fast_reasoning", _llm_raises):
            assert plan_tool_hint("", commands=_COMMANDS) is None
            assert plan_tool_hint("   ", commands=_COMMANDS) is None

    def test_empty_registry_returns_none(self):
        """Empty command registry must return None without calling the LLM."""
        with patch("lib.llm_clients.call_fast_reasoning", _llm_raises):
            assert plan_tool_hint("write a script", commands=[]) is None

    def test_llm_failure_returns_none(self):
        """LLM errors must not propagate — return None gracefully."""
        with patch("lib.llm_clients.call_fast_reasoning", _llm_raises):
            result = plan_tool_hint("write a throwaway script", commands=_COMMANDS)
        assert result is None

    def test_llm_returns_unknown_command_id_returns_none(self):
        """Unknown command_id not in registry must return None."""
        with patch("lib.llm_clients.call_fast_reasoning", _llm_returns("nonexistent_cmd")):
            result = plan_tool_hint("write a script", commands=_COMMANDS)
        assert result is None

    def test_llm_returns_malformed_json_returns_none(self):
        """Malformed LLM output must not propagate."""
        def _bad_json(*args, **kwargs):
            return "not json at all", {}
        with patch("lib.llm_clients.call_fast_reasoning", _bad_json):
            result = plan_tool_hint("write a script", commands=_COMMANDS)
        assert result is None

    def test_command_list_passed_to_llm(self):
        """Command descriptions must appear in the prompt sent to the LLM."""
        captured = {}

        def _capture(prompt, **kwargs):
            captured["prompt"] = prompt
            return json.dumps({"command_id": None}), {}

        with patch("lib.llm_clients.call_fast_reasoning", _capture):
            plan_tool_hint("write a quick script", commands=_COMMANDS)

        assert "misc_project" in captured.get("prompt", "")
        assert "create_project" in captured.get("prompt", "")
        assert "recall" in captured.get("prompt", "")


class TestResolveCommandRegistry:

    def test_resolves_misc_path_from_env(self, tmp_path, monkeypatch):
        """resolve_command_registry substitutes {misc_path} from env vars."""
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
        resolved = resolve_command_registry()
        misc_entry = next(c for c in resolved if c["id"] == "misc_project")
        expected = str(tmp_path / "shared" / "projects" / "misc--openclaw-main")
        assert expected in misc_entry["hint"]

    def test_falls_back_to_directory_scan(self, tmp_path, monkeypatch):
        """resolve_command_registry finds instance by scanning misc-- dirs."""
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.delenv("QUAID_INSTANCE", raising=False)
        misc_dir = tmp_path / "shared" / "projects" / "misc--my-instance"
        misc_dir.mkdir(parents=True)
        resolved = resolve_command_registry()
        misc_entry = next(c for c in resolved if c["id"] == "misc_project")
        assert "misc--my-instance" in misc_entry["hint"]

    def test_unresolved_template_when_no_instance(self, monkeypatch):
        """Without instance info, {misc_path} stays as-is in the hint."""
        monkeypatch.delenv("QUAID_HOME", raising=False)
        monkeypatch.delenv("QUAID_INSTANCE", raising=False)
        monkeypatch.delenv("CLAWDBOT_WORKSPACE", raising=False)
        resolved = resolve_command_registry()
        misc_entry = next(c for c in resolved if c["id"] == "misc_project")
        assert "{misc_path}" in misc_entry["hint"]
