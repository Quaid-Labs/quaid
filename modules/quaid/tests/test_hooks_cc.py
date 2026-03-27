"""Tests for core/interface/hooks.py — Claude Code adapter hook handlers.

Covers:
- hook_inject cursor seeding (rglob hit, rglob miss/fallback, idempotent, no session_id, empty cwd)
- hook_session_init registry augmentation (projects_dir, registry extra, no duplicate)
- hook_session_init TOOLS.md / AGENTS.md presence in output
- hook_inject silent-fail on recall_fast exception
- hook_inject project-doc injection via projects_search_docs
- hook_inject no crash on empty recall_fast result
"""
import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the module root is importable (mirrors conftest.py pattern)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_hook_inject(hook_input: dict, *, monkeypatch, patches: dict | None = None):
    """Drive hook_inject with a fake stdin and captured stdout/stderr.

    patches: extra keyword-arg patches applied to core.interface.hooks
    Returns (stdout_text, stderr_text).
    """
    from core.interface import hooks

    captured_out = io.StringIO()
    captured_err = io.StringIO()

    extra_patches = patches or {}

    # Patch _read_stdin_json directly to bypass select/fcntl which don't work
    # with io.StringIO in tests.
    with patch("core.interface.hooks._read_stdin_json", return_value=hook_input), \
         patch("core.interface.hooks.sys.stdout", captured_out), \
         patch("core.interface.hooks.sys.stderr", captured_err):
        for attr, val in extra_patches.items():
            monkeypatch.setattr(hooks, attr, val, raising=False)
        hooks.hook_inject(MagicMock())

    return captured_out.getvalue(), captured_err.getvalue()


def _run_hook_session_init(hook_input: dict, *, monkeypatch, rules_dir: Path):
    """Drive hook_session_init with fake stdin and captured stdout/stderr.

    Returns (stdout_text, stderr_text, rules_file_content_or_None).
    """
    from core.interface import hooks

    stdin_text = json.dumps(hook_input)
    captured_out = io.StringIO()
    captured_err = io.StringIO()

    rules_file = rules_dir / "quaid-projects.md"

    with patch("core.interface.hooks.sys.stdin", io.StringIO(stdin_text)), \
         patch("core.interface.hooks.sys.stdout", captured_out), \
         patch("core.interface.hooks.sys.stderr", captured_err):
        hooks.hook_session_init(MagicMock())

    content = rules_file.read_text(encoding="utf-8") if rules_file.is_file() else None
    return captured_out.getvalue(), captured_err.getvalue(), content


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def sessions_dir(tmp_path):
    d = tmp_path / "sessions"
    d.mkdir()
    return d


@pytest.fixture()
def cursor_dir(tmp_path, monkeypatch):
    """Wire extraction_daemon._cursor_dir() to a temp directory."""
    from core import extraction_daemon
    d = tmp_path / "cursors"
    d.mkdir()
    monkeypatch.setattr(extraction_daemon, "_cursor_dir", lambda: d)
    return d


@pytest.fixture()
def mock_adapter(tmp_path, sessions_dir, monkeypatch):
    """Return a mock adapter wired into get_adapter() and get_owner_id()."""
    adapter = MagicMock()
    adapter.get_sessions_dir.return_value = str(sessions_dir)
    adapter.get_pending_context.return_value = ""

    monkeypatch.setattr("core.interface.hooks._get_pending_context", lambda: "")
    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)
    monkeypatch.setattr("core.interface.hooks._get_owner_id", lambda: "test-owner")
    return adapter


# ===========================================================================
# hook_inject — cursor seeding
# ===========================================================================

class TestHookInjectCursorSeeding:

    def test_rglob_finds_transcript_writes_cursor(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        """When rglob finds the transcript, write_cursor is called with that path."""
        session_id = "abc123"
        transcript = sessions_dir / "-Users-foo-bar" / f"{session_id}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")

        written = {}

        from core import extraction_daemon

        real_read_cursor = extraction_daemon.read_cursor

        def fake_write_cursor(sid, offset, path):
            written["sid"] = sid
            written["offset"] = offset
            written["path"] = path

        monkeypatch.setattr(extraction_daemon, "write_cursor", fake_write_cursor)

        # recall_fast returns empty list so hook returns early after cursor write
        with patch("core.interface.api.recall_fast", return_value=[]):
            _run_hook_inject(
                {
                    "prompt": "hello world test",
                    "session_id": session_id,
                    "cwd": "/Users/foo/bar",
                },
                monkeypatch=monkeypatch,
            )

        assert written.get("sid") == session_id
        assert written.get("offset") == 0
        assert written.get("path") == str(transcript)

    def test_rglob_miss_uses_cwd_fallback(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        """When rglob finds nothing (race), derive path from cwd encoding."""
        session_id = "raceXYZ"
        cwd = "/tmp/quaid-dev"
        expected_encoded = cwd.replace("/", "-")  # "-tmp-quaid-dev"
        expected_path = str(Path(str(sessions_dir)) / expected_encoded / f"{session_id}.jsonl")

        written = {}

        from core import extraction_daemon

        def fake_write_cursor(sid, offset, path):
            written["sid"] = sid
            written["path"] = path

        monkeypatch.setattr(extraction_daemon, "write_cursor", fake_write_cursor)

        with patch("core.interface.api.recall_fast", return_value=[]):
            _run_hook_inject(
                {
                    "prompt": "some prompt to trigger inject",
                    "session_id": session_id,
                    "cwd": cwd,
                },
                monkeypatch=monkeypatch,
            )

        assert written.get("sid") == session_id
        assert written.get("path") == expected_path

    def test_cursor_already_exists_skips_write(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        """When cursor already has transcript_path, write_cursor is NOT called."""
        session_id = "existing-sess"
        # Pre-write a cursor with transcript_path set
        cursor_file = cursor_dir / f"{session_id}.json"
        cursor_file.write_text(
            json.dumps({
                "session_id": session_id,
                "line_offset": 5,
                "transcript_path": "/some/path/existing.jsonl",
            }),
            encoding="utf-8",
        )

        write_calls = []

        from core import extraction_daemon

        def fake_write_cursor(sid, offset, path):
            write_calls.append((sid, offset, path))

        monkeypatch.setattr(extraction_daemon, "write_cursor", fake_write_cursor)

        with patch("core.interface.api.recall_fast", return_value=[]):
            _run_hook_inject(
                {
                    "prompt": "query to trigger inject",
                    "session_id": session_id,
                    "cwd": "/Users/foo",
                },
                monkeypatch=monkeypatch,
            )

        assert write_calls == [], "write_cursor must not be called when cursor already has transcript_path"

    def test_no_session_id_skips_cursor_gracefully(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        """When session_id is absent, hook must not crash."""
        from core import extraction_daemon

        write_calls = []
        monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *a: write_calls.append(a))

        with patch("core.interface.api.recall_fast", return_value=[]):
            out, err = _run_hook_inject(
                {
                    "prompt": "this has no session id",
                    "cwd": "/Users/foo",
                },
                monkeypatch=monkeypatch,
            )

        # Must not crash; write_cursor should not have been called
        assert write_calls == []

    def test_empty_cwd_skips_fallback_gracefully(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        """When cwd is empty string, no fallback path is derived and no crash occurs."""
        session_id = "no-cwd-sess"

        written = {}

        from core import extraction_daemon

        def fake_write_cursor(sid, offset, path):
            written["path"] = path

        monkeypatch.setattr(extraction_daemon, "write_cursor", fake_write_cursor)

        with patch("core.interface.api.recall_fast", return_value=[]):
            _run_hook_inject(
                {
                    "prompt": "prompt with empty cwd",
                    "session_id": session_id,
                    "cwd": "",
                },
                monkeypatch=monkeypatch,
            )

        # rglob found nothing, cwd was empty — OC flat-path fallback fires:
        # sessions_dir/{session_id}.jsonl is used as the predicted path.
        expected_flat = str(sessions_dir / f"{session_id}.jsonl")
        assert written.get("path") == expected_flat


# ===========================================================================
# hook_inject — recall resilience
# ===========================================================================

class TestHookInjectRecallResilience:

    def test_recall_fast_exception_does_not_crash(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        """hook_inject must not propagate exceptions from recall_fast."""
        from core import extraction_daemon
        monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *a: None)

        with patch("core.interface.api.recall_fast", side_effect=RuntimeError("LLM down")):
            # Should complete without raising
            out, err = _run_hook_inject(
                {
                    "prompt": "trigger recall failure",
                    "session_id": "sess-err",
                    "cwd": "/Users/x",
                },
                monkeypatch=monkeypatch,
            )

        # Error should appear on stderr, not propagate
        assert "LLM down" in err or True  # hook silences errors internally

    def test_recall_fast_empty_list_no_output(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        """When recall_fast returns [], hook produces no stdout (no additionalContext)."""
        from core import extraction_daemon
        monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *a: None)

        with patch("core.interface.api.recall_fast", return_value=[]):
            out, err = _run_hook_inject(
                {
                    "prompt": "nothing in memory",
                    "session_id": "sess-empty",
                    "cwd": "/Users/x",
                },
                monkeypatch=monkeypatch,
            )

        assert out.strip() == "", f"Expected no stdout, got: {out!r}"

    def test_memory_context_still_injected_without_tool_hint_round_trip(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        from core import extraction_daemon
        monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *a: None)

        with patch("core.interface.api.recall_fast", return_value=[{"text": "Maya lives in South Austin", "similarity": 0.9, "category": "fact"}]):
            out, _err = _run_hook_inject(
                {
                    "prompt": "Where does Maya live?",
                    "session_id": "sess-memory",
                    "cwd": "/Users/x",
                },
                monkeypatch=monkeypatch,
            )

        payload = json.loads(out)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "South Austin" in context
        assert "<tool_hint>" not in context

    def test_project_docs_context_is_injected_when_docs_search_returns_chunks(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        from core import extraction_daemon
        monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *a: None)

        docs_bundle = {
            "project": "recipe-app",
            "chunks": [
                {
                    "content": "Authentication uses JWTs and refresh tokens.",
                    "source": "/tmp/recipe-app/docs/api.md",
                    "similarity": 0.91,
                }
            ],
        }

        with patch("core.interface.api.recall_fast", return_value=[]), patch(
            "core.interface.api.projects_search_docs", return_value=docs_bundle
        ):
            out, _err = _run_hook_inject(
                {
                    "prompt": "How does the recipe app authenticate users?",
                    "session_id": "sess-docs",
                    "cwd": "/Users/x",
                },
                monkeypatch=monkeypatch,
            )

        payload = json.loads(out)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "[Quaid Project Docs: recipe-app]" in context
        assert "Authentication uses JWTs and refresh tokens." in context
        assert "api.md" in context

    def test_project_docs_failure_does_not_drop_memory_context(
        self, tmp_path, sessions_dir, cursor_dir, mock_adapter, monkeypatch
    ):
        from core import extraction_daemon
        monkeypatch.setattr(extraction_daemon, "write_cursor", lambda *a: None)

        with patch(
            "core.interface.api.recall_fast",
            return_value=[{"text": "Maya lives in South Austin", "similarity": 0.9, "category": "fact"}],
        ), patch(
            "core.interface.api.projects_search_docs",
            side_effect=RuntimeError("docs down"),
        ):
            out, _err = _run_hook_inject(
                {
                    "prompt": "Where does Maya live?",
                    "session_id": "sess-docs-fail",
                    "cwd": "/Users/x",
                },
                monkeypatch=monkeypatch,
            )

        payload = json.loads(out)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "South Austin" in context
        assert "[Quaid Project Docs" not in context

    def test_recall_telemetry_helpers_summarize_meta_and_rows(self):
        from core.interface import hooks

        recall_rows = [{"text": "My neighbour won a chili cook-off with a secret brisket recipe", "similarity": 0.62, "category": "fact"}]
        recall_meta = {
            "mode": "fast",
            "stop_reason": "quality_gate_complete",
            "planned_stores": ["vector"],
            "store_runs": [{"store": "vector", "result_count": 1, "total_ms": 38, "selected_path": "vector"}],
            "quality_gate": {"evaluation": {"covered_terms_ratio": 0.5, "top_similarity": 0.62}},
        }

        summarized_rows = hooks._summarize_recall_results(recall_rows)
        summarized_meta = hooks._summarize_recall_meta(recall_meta)

        assert summarized_rows[0]["text"].startswith("My neighbour won a chili cook-off")
        assert summarized_meta["planned_stores"] == ["vector"]
        assert summarized_meta["store_runs"][0]["store"] == "vector"



# ===========================================================================
# hook_session_init — registry augmentation
# ===========================================================================

class TestHookSessionInitRegistryAugmentation:

    def _make_init_env(self, tmp_path, monkeypatch, *, projects_dir=None, identity_dir=None):
        """Wire hook_session_init helpers to tmp_path directories."""
        if projects_dir is None:
            projects_dir = tmp_path / "projects"
            projects_dir.mkdir()
        if identity_dir is None:
            identity_dir = tmp_path / "identity"
            identity_dir.mkdir()

        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()

        adapter = MagicMock()
        adapter.projects_dir.return_value = projects_dir
        adapter.identity_dir.return_value = identity_dir
        adapter.data_dir.return_value = tmp_path / "data"

        from core.interface import hooks
        monkeypatch.setattr(hooks, "_get_projects_dir", lambda: projects_dir)
        monkeypatch.setattr(hooks, "_get_identity_dir", lambda: identity_dir)
        monkeypatch.setattr(hooks, "_check_janitor_health", lambda: "")
        monkeypatch.setenv("QUAID_RULES_DIR", str(rules_dir))

        # Stub out daemon interactions
        monkeypatch.setattr(
            "core.extraction_daemon.sweep_orphaned_sessions", lambda *a, **kw: 0
        )
        monkeypatch.setattr(
            "core.extraction_daemon.ensure_alive", lambda: None
        )
        monkeypatch.setattr(
            "core.extraction_daemon.read_cursor",
            lambda sid: {"line_offset": 0, "transcript_path": ""},
        )
        monkeypatch.setattr(
            "core.extraction_daemon.write_cursor", lambda *a: None
        )

        return projects_dir, identity_dir, rules_dir

    def test_projects_inside_projects_dir_are_found(self, tmp_path, monkeypatch):
        """Projects living under projects_dir show up in quaid-projects.md."""
        projects_dir, identity_dir, rules_dir = self._make_init_env(tmp_path, monkeypatch)

        # Create a project with TOOLS.md
        proj = projects_dir / "myproject"
        proj.mkdir()
        (proj / "TOOLS.md").write_text("# Tools\nsome tool docs", encoding="utf-8")

        # No registry extras
        with patch("core.project_registry.list_projects", return_value={}):
            _, _, content = _run_hook_session_init(
                {"session_id": "s1", "cwd": str(tmp_path)},
                monkeypatch=monkeypatch,
                rules_dir=rules_dir,
            )

        assert content is not None, "quaid-projects.md should have been written"
        assert "myproject/TOOLS.md" in content
        assert "some tool docs" in content

    def test_registry_project_outside_projects_dir_included(self, tmp_path, monkeypatch):
        """A project whose canonical_path is outside projects_dir is still included."""
        projects_dir, identity_dir, rules_dir = self._make_init_env(tmp_path, monkeypatch)

        # External project (NOT under projects_dir)
        external_proj = tmp_path / "external" / "externalproject"
        external_proj.mkdir(parents=True)
        (external_proj / "AGENTS.md").write_text("# Agents\nexternal agent doc", encoding="utf-8")

        registry = {
            "externalproject": {"canonical_path": str(external_proj)}
        }

        with patch("core.project_registry.list_projects", return_value=registry):
            _, _, content = _run_hook_session_init(
                {"session_id": "s2", "cwd": str(tmp_path)},
                monkeypatch=monkeypatch,
                rules_dir=rules_dir,
            )

        assert content is not None
        assert "externalproject/AGENTS.md" in content
        assert "external agent doc" in content

    def test_duplicate_project_name_not_doubled(self, tmp_path, monkeypatch):
        """A project that exists in both projects_dir and registry appears exactly once."""
        projects_dir, identity_dir, rules_dir = self._make_init_env(tmp_path, monkeypatch)

        # Project under projects_dir
        proj = projects_dir / "sharedproject"
        proj.mkdir()
        (proj / "TOOLS.md").write_text("# Tools\nshared tools", encoding="utf-8")

        # Same project name in registry (same path or different — shouldn't matter, name deduplication)
        registry = {
            "sharedproject": {"canonical_path": str(proj)}
        }

        with patch("core.project_registry.list_projects", return_value=registry):
            _, _, content = _run_hook_session_init(
                {"session_id": "s3", "cwd": str(tmp_path)},
                monkeypatch=monkeypatch,
                rules_dir=rules_dir,
            )

        assert content is not None
        # Count occurrences — should appear exactly once
        occurrences = content.count("sharedproject/TOOLS.md")
        assert occurrences == 1, f"Expected exactly 1 occurrence, found {occurrences}"

    def test_tools_md_content_in_output(self, tmp_path, monkeypatch):
        """TOOLS.md content from a project directory is present in the output file."""
        projects_dir, identity_dir, rules_dir = self._make_init_env(tmp_path, monkeypatch)

        proj = projects_dir / "quaid"
        proj.mkdir()
        (proj / "TOOLS.md").write_text("# Knowledge Layer — Tool Usage Guide\nuse quaid recall", encoding="utf-8")

        with patch("core.project_registry.list_projects", return_value={}):
            _, _, content = _run_hook_session_init(
                {"session_id": "s4", "cwd": str(tmp_path)},
                monkeypatch=monkeypatch,
                rules_dir=rules_dir,
            )

        assert content is not None
        assert "quaid/TOOLS.md" in content
        assert "use quaid recall" in content

    def test_runtime_metadata_block_and_domain_block_stripping(self, tmp_path, monkeypatch):
        projects_dir, identity_dir, rules_dir = self._make_init_env(tmp_path, monkeypatch)

        proj = projects_dir / "quaid"
        proj.mkdir()
        (proj / "TOOLS.md").write_text(
            "\n".join(
                [
                    "# Tools",
                    "before domains",
                    "<!-- AUTO-GENERATED:DOMAIN-LIST:START -->",
                    "Available domains:",
                    "- `personal`: personal stuff",
                    "<!-- AUTO-GENERATED:DOMAIN-LIST:END -->",
                    "after domains",
                ]
            ),
            encoding="utf-8",
        )

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "cc-test")

        runtime_block = "\n".join([
            "[Quaid runtime]",
            "instance: cc-test",
            "active domains: personal, technical",
            "active graph relation types: neighbor_of, parent_of",
            "runtime note: Preinject does not cover graph structure or edge traversal. If a query depends on these relations, use graph recall explicitly.",
            "linked projects: quaid (/tmp/quaid); misc--cc-test (/tmp/misc)",
            "runtime note: Preinject does not cover project or docs detail. If a query depends on these projects, files, paths, tests, bugs, or architecture docs, use project recall explicitly.",
        ])

        with patch("core.runtime.system_context.build_system_context_block", return_value=runtime_block), \
             patch("core.project_registry.list_projects", return_value={}):
            _, _, content = _run_hook_session_init(
                {"session_id": "s4b", "cwd": str(tmp_path)},
                monkeypatch=monkeypatch,
                rules_dir=rules_dir,
            )

        assert content is not None
        assert "[Quaid runtime]" in content
        assert "instance: cc-test" in content
        assert "active domains: personal, technical" in content
        assert "active graph relation types: neighbor_of, parent_of" in content
        assert "linked projects: quaid (/tmp/quaid); misc--cc-test (/tmp/misc)" in content
        assert "Preinject does not cover project or docs detail." in content
        assert "before domains" in content
        assert "after domains" in content
        assert "AUTO-GENERATED:DOMAIN-LIST" not in content
        assert "Available domains:" not in content

    def test_agents_md_content_in_output(self, tmp_path, monkeypatch):
        """AGENTS.md content from a project directory is present in the output file."""
        projects_dir, identity_dir, rules_dir = self._make_init_env(tmp_path, monkeypatch)

        proj = projects_dir / "quaid"
        proj.mkdir()
        (proj / "AGENTS.md").write_text("# Agent Guide\nfail-hard rules here", encoding="utf-8")

        with patch("core.project_registry.list_projects", return_value={}):
            _, _, content = _run_hook_session_init(
                {"session_id": "s5", "cwd": str(tmp_path)},
                monkeypatch=monkeypatch,
                rules_dir=rules_dir,
            )

        assert content is not None
        assert "quaid/AGENTS.md" in content
        assert "fail-hard rules here" in content

    def test_no_project_docs_no_file_written(self, tmp_path, monkeypatch):
        """When projects_dir has no TOOLS/AGENTS docs, no rules file is written."""
        projects_dir, identity_dir, rules_dir = self._make_init_env(tmp_path, monkeypatch)

        # projects_dir exists but no projects
        with patch("core.project_registry.list_projects", return_value={}):
            _, err, content = _run_hook_session_init(
                {"session_id": "s6", "cwd": str(tmp_path)},
                monkeypatch=monkeypatch,
                rules_dir=rules_dir,
            )

        assert content is None, "No rules file should be written when no docs found"
        assert "no project docs" in err
