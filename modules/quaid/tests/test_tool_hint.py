"""Tests for plan_tool_hint() in memory_graph.py."""

import os
import sys
import json
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["MEMORY_DB_PATH"] = ":memory:"

import pytest

from datastore.memorydb.memory_graph import plan_tool_hint, _load_tools_md


# ---------------------------------------------------------------------------
# Minimal TOOLS.md fixture — contains the file placement section
# ---------------------------------------------------------------------------

_TOOLS_MD = """
# Knowledge Layer — Tool Usage Guide

## Project Commands
- `quaid registry create-project <name>` — Create a new project
- `quaid registry list` — List all projects

### Project File Placement
When working with a project, decide whether files belong in the project
directory or should be linked externally.

**Throwaway / quick / temp / hello-world files** → write to the misc project:
  ~/.openclaw/extensions/quaid/quaid project show misc--$QUAID_INSTANCE

**New durable work** (essays, articles, video scripts, travel plans, research notes,
blog posts, screenplays, outlines, reports) → create a project first:
  ~/.openclaw/extensions/quaid/quaid registry create-project <name> --source-roots <path>

**NEVER write files to /tmp/, /var/tmp/, or any path outside a tracked project.**

## Memory Commands
- `quaid recall "query"` — Search memories
- `quaid store "text"` — Store a new memory
- `quaid stats` — Database statistics
"""


def _llm_returns(hint_text):
    """Return a mock that simulates call_fast_reasoning returning a JSON hint."""
    def _call(*args, **kwargs):
        return json.dumps({"tool_hint": hint_text}), {}
    return _call


def _llm_returns_null(*args, **kwargs):
    return json.dumps({"tool_hint": None}), {}


def _llm_raises(*args, **kwargs):
    raise RuntimeError("LLM unavailable")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPlanToolHint:

    def test_throwaway_file_query_returns_hint(self):
        """A throwaway-file message should produce a misc-project hint."""
        with patch("lib.llm_clients.call_fast_reasoning", _llm_returns(
            "Write throwaway files to the misc project using: quaid project show misc--$QUAID_INSTANCE"
        )):
            result = plan_tool_hint(
                "Can you write a quick hello world script? Just put it somewhere temporary.",
                tools_md=_TOOLS_MD,
            )
        assert result is not None
        assert result.startswith("<tool_hint>")
        assert result.endswith("</tool_hint>")
        assert "misc" in result

    def test_unrelated_query_returns_none_when_llm_says_null(self):
        """A query with no tool match should return None."""
        with patch("lib.llm_clients.call_fast_reasoning", _llm_returns_null):
            result = plan_tool_hint(
                "What is the capital of France?",
                tools_md=_TOOLS_MD,
            )
        assert result is None

    def test_durable_project_query_returns_hint(self):
        """A multi-file build request should hint at project creation."""
        with patch("lib.llm_clients.call_fast_reasoning", _llm_returns(
            "Create a project first: quaid registry create-project <name> --source-roots <path>"
        )):
            result = plan_tool_hint(
                "I want to build a proper CLI tool with tests and a README.",
                tools_md=_TOOLS_MD,
            )
        assert result is not None
        assert "create-project" in result or "project" in result.lower()

    def test_essay_query_returns_hint(self):
        """An essay-writing request should hint at project creation."""
        with patch("lib.llm_clients.call_fast_reasoning", _llm_returns(
            "Create a project first: quaid registry create-project essay-title"
        )):
            result = plan_tool_hint(
                "Help me write an essay about the history of the Roman Empire.",
                tools_md=_TOOLS_MD,
            )
        assert result is not None
        assert "project" in result.lower()

    def test_video_script_query_returns_hint(self):
        """A video script request should hint at project creation."""
        with patch("lib.llm_clients.call_fast_reasoning", _llm_returns(
            "Create a project first: quaid registry create-project my-video"
        )):
            result = plan_tool_hint(
                "Write a YouTube video script for a 10-minute explainer on black holes.",
                tools_md=_TOOLS_MD,
            )
        assert result is not None
        assert "project" in result.lower()

    def test_travel_plan_query_returns_hint(self):
        """A travel plan request should hint at project creation."""
        with patch("lib.llm_clients.call_fast_reasoning", _llm_returns(
            "Create a project first: quaid registry create-project japan-trip"
        )):
            result = plan_tool_hint(
                "Help me plan a 2-week trip to Japan including hotels and activities.",
                tools_md=_TOOLS_MD,
            )
        assert result is not None
        assert "project" in result.lower()

    def test_research_notes_query_returns_hint(self):
        """A research notes request should hint at project creation."""
        with patch("lib.llm_clients.call_fast_reasoning", _llm_returns(
            "Create a project first: quaid registry create-project quantum-research"
        )):
            result = plan_tool_hint(
                "I need to write up research notes on quantum computing for my thesis.",
                tools_md=_TOOLS_MD,
            )
        assert result is not None
        assert "project" in result.lower()

    def test_recall_query_returns_hint(self):
        """A memory recall request should hint at quaid recall."""
        with patch("lib.llm_clients.call_fast_reasoning", _llm_returns(
            "Search memories with: quaid recall \"your query\""
        )):
            result = plan_tool_hint(
                "What do you remember about my diet preferences?",
                tools_md=_TOOLS_MD,
            )
        assert result is not None
        assert "recall" in result.lower()

    def test_empty_query_returns_none(self):
        """Empty or whitespace query must return None without calling the LLM."""
        with patch("lib.llm_clients.call_fast_reasoning", _llm_raises):
            assert plan_tool_hint("", tools_md=_TOOLS_MD) is None
            assert plan_tool_hint("   ", tools_md=_TOOLS_MD) is None

    def test_no_tools_md_returns_none(self):
        """If TOOLS.md content is empty, return None without calling the LLM."""
        with patch("lib.llm_clients.call_fast_reasoning", _llm_raises):
            assert plan_tool_hint("write a script", tools_md="") is None
            assert plan_tool_hint("write a script", tools_md=None) is None

    def test_llm_failure_returns_none(self):
        """LLM errors must not propagate — return None gracefully."""
        with patch("lib.llm_clients.call_fast_reasoning", _llm_raises):
            result = plan_tool_hint("write a throwaway script", tools_md=_TOOLS_MD)
        assert result is None

    def test_llm_returns_short_hint_is_rejected(self):
        """Hints shorter than 6 chars are discarded."""
        with patch("lib.llm_clients.call_fast_reasoning", _llm_returns("ok")):
            result = plan_tool_hint("write a script", tools_md=_TOOLS_MD)
        assert result is None

    def test_llm_returns_malformed_json_returns_none(self):
        """Malformed LLM output must not propagate."""
        def _bad_json(*args, **kwargs):
            return "not json at all", {}
        with patch("lib.llm_clients.call_fast_reasoning", _bad_json):
            result = plan_tool_hint("write a script", tools_md=_TOOLS_MD)
        assert result is None

    def test_llm_returns_json_with_trailing_text(self):
        """JSON followed by extra prose must still parse correctly."""
        def _json_with_trailing(*args, **kwargs):
            return '{"tool_hint": "Use misc project for throwaway files"}\n\nSome extra explanation here.', {}
        with patch("lib.llm_clients.call_fast_reasoning", _json_with_trailing):
            result = plan_tool_hint("write a quick script", tools_md=_TOOLS_MD)
        assert result is not None
        assert "misc" in result

    def test_llm_returns_fenced_json(self):
        """JSON wrapped in markdown code fences must still parse correctly."""
        def _fenced_json(*args, **kwargs):
            return '```json\n{"tool_hint": "Use misc project for throwaway files"}\n```', {}
        with patch("lib.llm_clients.call_fast_reasoning", _fenced_json):
            result = plan_tool_hint("write a quick script", tools_md=_TOOLS_MD)
        assert result is not None
        assert "misc" in result

    def test_tools_md_content_passed_to_llm(self):
        """The TOOLS.md content must appear in the prompt sent to the LLM."""
        captured = {}

        def _capture(prompt, **kwargs):
            captured["prompt"] = prompt
            return json.dumps({"tool_hint": None}), {}

        with patch("lib.llm_clients.call_fast_reasoning", _capture):
            plan_tool_hint("write a quick script", tools_md=_TOOLS_MD)

        assert "<tools>" in captured.get("prompt", "")
        assert "misc" in captured.get("prompt", "")


class TestLoadToolsMd:

    def test_load_from_quaid_project(self, tmp_path):
        """_load_tools_md finds TOOLS.md in shared/projects/quaid/."""
        tools_file = tmp_path / "shared" / "projects" / "quaid" / "TOOLS.md"
        tools_file.parent.mkdir(parents=True)
        tools_file.write_text("# Tools\n- quaid recall")

        with patch("lib.adapter.get_adapter") as mock_adapter:
            mock_adapter.return_value.projects_dir.return_value = tmp_path / "shared" / "projects"
            content = _load_tools_md()

        assert content == "# Tools\n- quaid recall"

    def test_falls_back_to_any_project(self, tmp_path):
        """_load_tools_md falls back to first TOOLS.md found if no quaid/ project."""
        tools_file = tmp_path / "shared" / "projects" / "myproject" / "TOOLS.md"
        tools_file.parent.mkdir(parents=True)
        tools_file.write_text("# My Tools")

        with patch("lib.adapter.get_adapter") as mock_adapter:
            mock_adapter.return_value.projects_dir.return_value = tmp_path / "shared" / "projects"
            content = _load_tools_md()

        assert content == "# My Tools"

    def test_returns_none_when_no_tools_md(self, tmp_path):
        """_load_tools_md returns None when no TOOLS.md exists."""
        projects_dir = tmp_path / "shared" / "projects"
        projects_dir.mkdir(parents=True)

        with patch("lib.adapter.get_adapter") as mock_adapter:
            mock_adapter.return_value.projects_dir.return_value = projects_dir
            content = _load_tools_md()

        assert content is None
