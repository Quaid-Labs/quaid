"""Unit tests for docs_updater.py — staleness checking, source mapping, git diffs."""

import builtins
from concurrent.futures import ThreadPoolExecutor
import sys
import os
import json
import sqlite3
import tempfile
import threading
from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

# Ensure the plugin root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Adapter is set per-test via _adapter_patch — no module-level set needed
from lib.adapter import set_adapter, reset_adapter, TestAdapter

import pytest

@contextmanager
def _adapter_patch(tmp_path):
    """Context manager that sets the adapter to use tmp_path as quaid home.

    Yields the instance root path (where files are resolved).
    """
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)
    try:
        yield adapter.instance_root()
    finally:
        reset_adapter()


# Build a minimal test config for docs_updater
def _make_test_config(source_mapping=None, doc_purposes=None, staleness_enabled=True):
    """Create a mock config with DocsConfig."""
    from config import MemoryConfig, DocsConfig, SourceMapping

    sm = {}
    if source_mapping:
        for src, data in source_mapping.items():
            sm[src] = SourceMapping(docs=data["docs"], label=data.get("label", ""))

    docs = DocsConfig(
        auto_update_on_compact=True,
        max_docs_per_update=3,
        staleness_check_enabled=staleness_enabled,
        source_mapping=sm,
        doc_purposes=doc_purposes or {},
    )

    return MemoryConfig(docs=docs)


def test_core_docs_updater_fail_hard_enabled_fails_closed_on_import_error(monkeypatch, caplog):
    from core.docs import updater

    real_import = builtins.__import__

    def failing_import(name, *args, **kwargs):
        if name == "lib.fail_policy":
            raise ImportError("missing fail policy")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing_import)
    caplog.set_level("CRITICAL")

    assert updater._fail_hard_enabled() is True
    assert "fail-hard policy unavailable in docs updater" in caplog.text


def test_file_lock_raises_on_lock_failure_when_fail_hard(monkeypatch, tmp_path):
    from datastore.docsdb import updater

    def _fail_flock(*_args):
        raise OSError("flock unavailable")

    monkeypatch.setitem(
        sys.modules,
        "fcntl",
        SimpleNamespace(LOCK_EX=1, LOCK_UN=2, flock=_fail_flock),
    )
    monkeypatch.setattr(updater, "is_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="Failed to acquire file lock"):
        with updater._file_lock(tmp_path / "docs-update.lock"):
            pass


def test_atomic_write_text_locks_project_md(monkeypatch, tmp_path):
    from datastore.docsdb import updater

    seen = []

    @contextmanager
    def _fake_lock(path):
        seen.append(path)
        yield

    target = tmp_path / "projects" / "demo" / "PROJECT.md"
    monkeypatch.setattr(updater, "_file_lock", _fake_lock)

    updater._atomic_write_text(target, "# Demo\n")

    assert seen == [target.with_name(".PROJECT.md.lock")]
    assert target.read_text(encoding="utf-8") == "# Demo\n"


def test_resolve_path_rejects_workspace_escape(tmp_path):
    with _adapter_patch(tmp_path):
        from datastore.docsdb import updater

        with pytest.raises(ValueError, match="escapes workspace"):
            updater._resolve_path("../escaped.md")


class TestCheckStaleness:
    """Tests for check_staleness()."""

    def test_no_mapping_returns_empty(self, tmp_path):
        """No source mapping → no stale docs."""
        cfg = _make_test_config(source_mapping={})
        with patch("datastore.docsdb.updater.get_config", return_value=cfg), \
             _adapter_patch(tmp_path):
            from datastore.docsdb.updater import check_staleness
            assert check_staleness() == {}

    def test_staleness_disabled_returns_empty(self, tmp_path):
        """When staleness check is disabled, returns empty."""
        cfg = _make_test_config(
            source_mapping={"src.py": {"docs": ["doc.md"]}},
            staleness_enabled=False,
        )
        with patch("datastore.docsdb.updater.get_config", return_value=cfg), \
             _adapter_patch(tmp_path):
            from datastore.docsdb.updater import check_staleness
            assert check_staleness() == {}

    def test_detects_stale_doc(self, tmp_path):
        """Source file newer than doc → doc is stale."""
        cfg = _make_test_config(
            source_mapping={"src.py": {"docs": ["docs/doc.md"]}},
        )
        with patch("datastore.docsdb.updater.get_config", return_value=cfg), \
             _adapter_patch(tmp_path) as iroot:
            # Create doc first, then source (so source is newer)
            doc_file = iroot / "docs" / "doc.md"
            doc_file.parent.mkdir(parents=True)
            doc_file.write_text("old doc content")

            import time
            time.sleep(0.05)  # Ensure mtime difference

            src_file = iroot / "src.py"
            src_file.write_text("updated source")

            from datastore.docsdb.updater import check_staleness
            stale = check_staleness()
            assert "docs/doc.md" in stale
            assert stale["docs/doc.md"].gap_hours > 0
            assert "src.py" in stale["docs/doc.md"].stale_sources
    def test_up_to_date_doc_not_stale(self, tmp_path):
        """Source file older than doc → doc is not stale."""
        cfg = _make_test_config(
            source_mapping={"src.py": {"docs": ["docs/doc.md"]}},
        )
        with patch("datastore.docsdb.updater.get_config", return_value=cfg), \
             _adapter_patch(tmp_path) as iroot:
            src_file = iroot / "src.py"
            src_file.write_text("source content")

            import time
            time.sleep(0.05)

            doc_file = iroot / "docs" / "doc.md"
            doc_file.parent.mkdir(parents=True)
            doc_file.write_text("fresh doc content")

            from datastore.docsdb.updater import check_staleness
            stale = check_staleness()
            assert stale == {}

    def test_missing_doc_ignored(self, tmp_path):
        """If doc file doesn't exist, it's not reported as stale."""
        cfg = _make_test_config(
            source_mapping={"src.py": {"docs": ["docs/nonexistent.md"]}},
        )
        with patch("datastore.docsdb.updater.get_config", return_value=cfg), \
             _adapter_patch(tmp_path) as iroot:
            src_file = iroot / "src.py"
            src_file.write_text("source content")
            from datastore.docsdb.updater import check_staleness
            assert check_staleness() == {}

    def test_missing_source_ignored(self, tmp_path):
        """If source file doesn't exist, it's not reported."""
        cfg = _make_test_config(
            source_mapping={"nonexistent.py": {"docs": ["docs/doc.md"]}},
        )
        with patch("datastore.docsdb.updater.get_config", return_value=cfg), \
             _adapter_patch(tmp_path) as iroot:
            doc_file = iroot / "docs" / "doc.md"
            doc_file.parent.mkdir(parents=True)
            doc_file.write_text("doc content")
            from datastore.docsdb.updater import check_staleness
            assert check_staleness() == {}

    def test_registry_mapping_failure_warns_and_uses_config_when_fail_open(self, tmp_path, monkeypatch, caplog):
        cfg = _make_test_config(
            source_mapping={"src.py": {"docs": ["docs/doc.md"]}},
        )
        with patch("datastore.docsdb.updater.get_config", return_value=cfg), \
             _adapter_patch(tmp_path) as iroot:
            from datastore.docsdb import updater

            doc_file = iroot / "docs" / "doc.md"
            doc_file.parent.mkdir(parents=True)
            doc_file.write_text("old doc content")

            import time
            time.sleep(0.05)

            src_file = iroot / "src.py"
            src_file.write_text("updated source")

            class _BrokenRegistry:
                def get_source_mappings(self, project=None):
                    raise OSError("registry unavailable")

            monkeypatch.setattr("datastore.docsdb.registry.DocsRegistry", lambda: _BrokenRegistry())
            monkeypatch.setattr(updater, "is_fail_hard_enabled", lambda: False)
            caplog.set_level("WARNING")

            stale = updater.check_staleness()

        assert "docs/doc.md" in stale
        assert "registry mappings unavailable" in caplog.text

    def test_registry_mapping_failure_raises_when_fail_hard(self, tmp_path, monkeypatch):
        cfg = _make_test_config(source_mapping={"src.py": {"docs": ["docs/doc.md"]}})
        with patch("datastore.docsdb.updater.get_config", return_value=cfg), \
             _adapter_patch(tmp_path):
            from datastore.docsdb import updater

            class _BrokenRegistry:
                def get_source_mappings(self, project=None):
                    raise OSError("registry unavailable")

            monkeypatch.setattr("datastore.docsdb.registry.DocsRegistry", lambda: _BrokenRegistry())
            monkeypatch.setattr(updater, "is_fail_hard_enabled", lambda: True)

            with pytest.raises(RuntimeError, match="Failed to load docs registry source mappings"):
                updater.check_staleness()

    def test_missing_registry_table_falls_back_to_config_when_fail_hard(self, tmp_path, monkeypatch):
        cfg = _make_test_config(
            source_mapping={"src.py": {"docs": ["docs/doc.md"]}},
        )
        with patch("datastore.docsdb.updater.get_config", return_value=cfg), \
             _adapter_patch(tmp_path) as iroot:
            from datastore.docsdb import updater

            doc_file = iroot / "docs" / "doc.md"
            doc_file.parent.mkdir(parents=True)
            doc_file.write_text("old doc content")

            import time
            time.sleep(0.05)

            src_file = iroot / "src.py"
            src_file.write_text("updated source")

            class _MissingTableRegistry:
                def get_source_mappings(self, project=None):
                    raise sqlite3.OperationalError("no such table: doc_registry")

            monkeypatch.setattr("datastore.docsdb.registry.DocsRegistry", lambda: _MissingTableRegistry())
            monkeypatch.setattr(updater, "is_fail_hard_enabled", lambda: True)

            stale = updater.check_staleness()

        assert "docs/doc.md" in stale
        assert "src.py" in stale["docs/doc.md"].stale_sources

    def test_malformed_registry_source_files_are_reported_stale(self, tmp_path, monkeypatch):
        cfg = _make_test_config(source_mapping={})
        with patch("datastore.docsdb.updater.get_config", return_value=cfg), \
             _adapter_patch(tmp_path) as iroot:
            from datastore.docsdb import updater
            from datastore.docsdb.registry import MALFORMED_SOURCE_FILES_MARKER

            doc_file = iroot / "docs" / "doc.md"
            doc_file.parent.mkdir(parents=True)
            doc_file.write_text("doc content")
            marker = f"{MALFORMED_SOURCE_FILES_MARKER}:docs/doc.md"

            class _Registry:
                def get_source_mappings(self, project=None):
                    return {"docs/doc.md": [marker]}

            monkeypatch.setattr("datastore.docsdb.registry.DocsRegistry", lambda: _Registry())

            stale = updater.check_staleness()

        assert "docs/doc.md" in stale
        assert stale["docs/doc.md"].stale_sources == [marker]
        assert stale["docs/doc.md"].change_classification["classification"] == "significant"
        assert "malformed source_files" in stale["docs/doc.md"].change_classification["reasons"][0]


class TestMapSourcesToDocs:
    """Tests for map_sources_to_docs()."""

    def test_maps_single_source(self):
        cfg = _make_test_config(
            source_mapping={"core.lifecycle.janitor.py": {"docs": ["docs/janitor-ref.md"]}},
        )
        with patch("datastore.docsdb.updater.get_config", return_value=cfg):
            from datastore.docsdb.updater import map_sources_to_docs
            result = map_sources_to_docs(["core.lifecycle.janitor.py"])
            assert "docs/janitor-ref.md" in result
            assert "core.lifecycle.janitor.py" in result["docs/janitor-ref.md"]

    def test_maps_multiple_sources_to_same_doc(self):
        cfg = _make_test_config(
            source_mapping={
                "index.ts": {"docs": ["docs/impl.md"]},
                "config.py": {"docs": ["docs/impl.md"]},
            },
        )
        with patch("datastore.docsdb.updater.get_config", return_value=cfg):
            from datastore.docsdb.updater import map_sources_to_docs
            result = map_sources_to_docs(["index.ts", "config.py"])
            assert "docs/impl.md" in result
            assert len(result["docs/impl.md"]) == 2

    def test_unmapped_source_ignored(self):
        cfg = _make_test_config(
            source_mapping={"core.lifecycle.janitor.py": {"docs": ["docs/janitor-ref.md"]}},
        )
        with patch("datastore.docsdb.updater.get_config", return_value=cfg):
            from datastore.docsdb.updater import map_sources_to_docs
            result = map_sources_to_docs(["unknown_file.py"])
            assert result == {}

    def test_empty_input(self):
        cfg = _make_test_config(
            source_mapping={"core.lifecycle.janitor.py": {"docs": ["docs/janitor-ref.md"]}},
        )
        with patch("datastore.docsdb.updater.get_config", return_value=cfg):
            from datastore.docsdb.updater import map_sources_to_docs
            assert map_sources_to_docs([]) == {}


class TestGetGitDiff:
    """Tests for get_git_diff()."""

    def test_returns_empty_for_nonexistent_file(self, tmp_path):
        with _adapter_patch(tmp_path):
            from datastore.docsdb.updater import get_git_diff
            result = get_git_diff("nonexistent.py", 0.0)
            assert result == ""

    def test_handles_git_not_available(self, tmp_path):
        """If git commands fail, returns empty string gracefully."""
        with _adapter_patch(tmp_path) as iroot, \
             patch("datastore.docsdb.updater.subprocess.run", side_effect=FileNotFoundError), \
             patch("datastore.docsdb.updater.logger.debug") as log_debug:
            src_file = iroot / "src.py"
            src_file.write_text("content")
            from datastore.docsdb.updater import get_git_diff
            result = get_git_diff("src.py", 0.0)
            assert result == ""
            debug_messages = [str(call.args[0]) for call in log_debug.call_args_list if call.args]
            assert any("Git log unavailable" in msg for msg in debug_messages)
            assert any("Git diff unavailable" in msg for msg in debug_messages)

    def test_stops_when_git_budget_exhausted(self, tmp_path, caplog):
        with _adapter_patch(tmp_path) as iroot, \
             patch("datastore.docsdb.updater._git_timeout_from_deadline", side_effect=[0.01, None]), \
             patch("datastore.docsdb.updater.subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as run_mock:
            # File must exist at instance root for get_git_diff to proceed
            (iroot / "src.py").write_text("content")
            from datastore.docsdb.updater import get_git_diff
            caplog.set_level("WARNING")
            result = get_git_diff("src.py", 0.0)

        assert result == ""
        assert run_mock.call_count == 1
        assert "Git subprocess budget exhausted while collecting git diff for src.py" in caplog.text

    def test_binary_source_uses_catalog_only_diff_context(self, tmp_path):
        calls = []

        def _fake_run(cmd, *args, **kwargs):
            calls.append(list(cmd))
            if cmd[:2] == ["git", "log"]:
                return MagicMock(returncode=0, stdout="abc123 add video\n", stderr="")
            if cmd[:3] == ["git", "diff", "--stat"]:
                return MagicMock(returncode=0, stdout=" video.mp4 | Bin 0 -> 1024 bytes\n", stderr="")
            if cmd[:3] == ["git", "diff", "HEAD"]:
                raise AssertionError("binary source should not request full patch diff")
            return MagicMock(returncode=0, stdout="", stderr="")

        with _adapter_patch(tmp_path) as iroot, \
             patch("datastore.docsdb.updater.subprocess.run", side_effect=_fake_run):
            (iroot / "video.mp4").write_bytes(b"\0" * 1024)
            from datastore.docsdb.updater import get_git_diff

            result = get_git_diff("video.mp4", 0.0)

        assert "Catalog-only source entry for video.mp4" in result
        assert "safety_mode: catalog_only" in result
        assert "Diff summary for video.mp4" in result
        assert any(cmd[:3] == ["git", "diff", "--stat"] for cmd in calls)


class TestGetDocPurposes:
    """Tests for get_doc_purposes()."""

    def test_returns_purposes_from_config(self):
        purposes = {"docs/foo.md": "Foo documentation", "docs/bar.md": "Bar docs"}
        cfg = _make_test_config(doc_purposes=purposes)
        with patch("datastore.docsdb.updater.get_config", return_value=cfg):
            from datastore.docsdb.updater import get_doc_purposes
            result = get_doc_purposes()
            assert result == purposes

    def test_empty_purposes(self):
        cfg = _make_test_config(doc_purposes={})
        with patch("datastore.docsdb.updater.get_config", return_value=cfg):
            from datastore.docsdb.updater import get_doc_purposes
            assert get_doc_purposes() == {}


class TestDetectChangedSources:
    """Tests for detect_changed_sources_from_transcript()."""

    def test_returns_empty_on_llm_failure(self):
        cfg = _make_test_config(
            source_mapping={"core.lifecycle.janitor.py": {"docs": ["docs/ref.md"]}},
        )
        with patch("datastore.docsdb.updater.get_config", return_value=cfg), \
             patch("lib.llm_chunked_call._fail_hard_enabled", return_value=False), \
             patch("lib.llm_clients.call_fast_reasoning", return_value=(None, 1.0)):
            from datastore.docsdb.updater import detect_changed_sources_from_transcript
            result = detect_changed_sources_from_transcript("some transcript")
            assert result == []

    def test_parses_valid_response(self):
        cfg = _make_test_config(
            source_mapping={
                "core.lifecycle.janitor.py": {"docs": ["docs/ref.md"]},
                "config.py": {"docs": ["docs/impl.md"]},
            },
        )
        response = '{"changed": ["core.lifecycle.janitor.py"]}'
        with patch("datastore.docsdb.updater.get_config", return_value=cfg), \
             patch("lib.llm_clients.call_fast_reasoning", return_value=(response, 1.0)):
            from datastore.docsdb.updater import detect_changed_sources_from_transcript
            result = detect_changed_sources_from_transcript("modified janitor.py")
            assert "core.lifecycle.janitor.py" in result

    def test_filters_unknown_files(self):
        cfg = _make_test_config(
            source_mapping={"core.lifecycle.janitor.py": {"docs": ["docs/ref.md"]}},
        )
        response = '{"changed": ["core.lifecycle.janitor.py", "unknown.py"]}'
        with patch("datastore.docsdb.updater.get_config", return_value=cfg), \
             patch("lib.llm_clients.call_fast_reasoning", return_value=(response, 1.0)):
            from datastore.docsdb.updater import detect_changed_sources_from_transcript
            result = detect_changed_sources_from_transcript("some transcript")
            assert "core.lifecycle.janitor.py" in result
            assert "unknown.py" not in result

    def test_changed_null_is_treated_as_empty(self, monkeypatch):
        cfg = _make_test_config(
            source_mapping={"core.lifecycle.janitor.py": {"docs": ["docs/ref.md"]}},
        )

        class _Result:
            output = '{"changed": null}'
            error = None

        monkeypatch.setattr(
            "lib.llm_chunked_call.parallel_llm_call",
            lambda **_kwargs: [_Result()],
        )
        with patch("datastore.docsdb.updater.get_config", return_value=cfg):
            from datastore.docsdb.updater import detect_changed_sources_from_transcript

            assert detect_changed_sources_from_transcript("modified janitor.py") == []

    def test_changed_non_list_raises_when_fail_hard(self, monkeypatch):
        cfg = _make_test_config(
            source_mapping={"core.lifecycle.janitor.py": {"docs": ["docs/ref.md"]}},
        )

        class _Result:
            output = '{"changed": "core.lifecycle.janitor.py"}'
            error = None

        monkeypatch.setattr(
            "lib.llm_chunked_call.parallel_llm_call",
            lambda **_kwargs: [_Result()],
        )
        with patch("datastore.docsdb.updater.get_config", return_value=cfg):
            from datastore.docsdb import updater

            monkeypatch.setattr(updater, "is_fail_hard_enabled", lambda: True)

            with pytest.raises(RuntimeError, match="Malformed changed-sources payload"):
                updater.detect_changed_sources_from_transcript("modified janitor.py")

    def test_no_mapping_returns_empty(self):
        cfg = _make_test_config(source_mapping={})
        with patch("datastore.docsdb.updater.get_config", return_value=cfg):
            from datastore.docsdb.updater import detect_changed_sources_from_transcript
            assert detect_changed_sources_from_transcript("transcript") == []


class TestGetCoreMarkdownInfo:
    """Tests for _get_core_markdown_info()."""

    def test_detects_core_markdown_file(self):
        """Core markdown files return (purpose, maxLines)."""
        cfg = _make_test_config()
        cfg.docs.core_markdown = MagicMock()
        cfg.docs.core_markdown.files = {
            "TOOLS.md": {"purpose": "API docs and configs", "maxLines": 350},
        }
        with patch("datastore.docsdb.updater.get_config", return_value=cfg):
            from datastore.docsdb.updater import _get_core_markdown_info
            result = _get_core_markdown_info("TOOLS.md")
            assert result == ("API docs and configs", 350)

    def test_returns_none_for_regular_doc(self):
        """Non-core markdown files return None."""
        cfg = _make_test_config()
        cfg.docs.core_markdown = MagicMock()
        cfg.docs.core_markdown.files = {
            "TOOLS.md": {"purpose": "API docs", "maxLines": 350},
        }
        with patch("datastore.docsdb.updater.get_config", return_value=cfg):
            from datastore.docsdb.updater import _get_core_markdown_info
            result = _get_core_markdown_info("projects/quaid/janitor-reference.md")
            assert result is None

    def test_handles_path_with_directory(self):
        """Extracts basename from paths with directories."""
        cfg = _make_test_config()
        cfg.docs.core_markdown = MagicMock()
        cfg.docs.core_markdown.files = {
            "AGENTS.md": {"purpose": "System operations", "maxLines": 350},
        }
        with patch("datastore.docsdb.updater.get_config", return_value=cfg):
            from datastore.docsdb.updater import _get_core_markdown_info
            # Should not match — core markdown keys are bare filenames
            result = _get_core_markdown_info("some/path/AGENTS.md")
            assert result == ("System operations", 350)

    def test_returns_none_when_no_core_markdown_config(self):
        """Returns None when core_markdown config is empty."""
        cfg = _make_test_config()
        cfg.docs.core_markdown = MagicMock()
        cfg.docs.core_markdown.files = {}
        with patch("datastore.docsdb.updater.get_config", return_value=cfg):
            from datastore.docsdb.updater import _get_core_markdown_info
            result = _get_core_markdown_info("TOOLS.md")
            assert result is None


class TestClassifyDocChange:
    """Tests for classify_doc_change() — smart threshold for doc updates."""

    def test_empty_diff_is_trivial(self):
        """Empty diff → trivial with high confidence."""
        from datastore.docsdb.updater import classify_doc_change
        result = classify_doc_change("")
        assert result["classification"] == "trivial"
        assert result["confidence"] == 1.0
        assert "empty diff" in result["reasons"]

    def test_none_diff_is_trivial(self):
        """None diff → trivial."""
        from datastore.docsdb.updater import classify_doc_change
        result = classify_doc_change(None)
        assert result["classification"] == "trivial"
        assert result["confidence"] == 1.0

    def test_whitespace_only_is_trivial(self):
        """Whitespace-only changes → trivial."""
        from datastore.docsdb.updater import classify_doc_change
        diff = (
            "--- a/file.py\n"
            "+++ b/file.py\n"
            "-   \n"
            "+  \n"
            "-\n"
            "+\n"
        )
        result = classify_doc_change(diff)
        assert result["classification"] == "trivial"
        assert any("whitespace" in r for r in result["reasons"])

    def test_comment_only_is_trivial(self):
        """Comment-only changes → trivial."""
        from datastore.docsdb.updater import classify_doc_change
        diff = (
            "--- a/file.py\n"
            "+++ b/file.py\n"
            "-# old comment\n"
            "+# new comment\n"
        )
        result = classify_doc_change(diff)
        assert result["classification"] == "trivial"
        assert any("comment" in r for r in result["reasons"])

    def test_js_comment_is_trivial(self):
        """JavaScript comment changes → trivial."""
        from datastore.docsdb.updater import classify_doc_change
        diff = (
            "--- a/file.js\n"
            "+++ b/file.js\n"
            "-// old comment\n"
            "+// updated comment\n"
        )
        result = classify_doc_change(diff)
        assert result["classification"] == "trivial"
        assert any("comment" in r for r in result["reasons"])

    def test_import_change_is_trivial(self):
        """Import path changes → trivial."""
        from datastore.docsdb.updater import classify_doc_change
        diff = (
            "--- a/file.py\n"
            "+++ b/file.py\n"
            "-from old_module import something\n"
            "+from new_module import something\n"
        )
        result = classify_doc_change(diff)
        assert result["classification"] == "trivial"
        assert any("import" in r for r in result["reasons"])

    def test_version_bump_is_trivial(self):
        """Version bump → trivial."""
        from datastore.docsdb.updater import classify_doc_change
        diff = (
            "--- a/package.json\n"
            "+++ b/package.json\n"
            '-  "version": "1.2.3"\n'
            '+  "version": "1.2.4"\n'
        )
        result = classify_doc_change(diff)
        assert result["classification"] == "trivial"
        assert any("version" in r for r in result["reasons"])

    def test_typo_fix_is_trivial(self):
        """Typo-like edit (high character similarity) → trivial."""
        from datastore.docsdb.updater import classify_doc_change
        diff = (
            "--- a/file.py\n"
            "+++ b/file.py\n"
            "-This is a docstring with a tpyo\n"
            "+This is a docstring with a typo\n"
        )
        result = classify_doc_change(diff)
        assert result["classification"] == "trivial"
        assert any("typo" in r for r in result["reasons"])

    def test_new_function_is_significant(self):
        """New function definition → significant."""
        from datastore.docsdb.updater import classify_doc_change
        diff = (
            "--- a/file.py\n"
            "+++ b/file.py\n"
            "+def new_feature():\n"
            "+    pass\n"
            "+\n"
            "+def another_feature():\n"
            "+    return True\n"
            "+\n"
            "+# some comment\n"
            "+\n"
            "+def third_feature():\n"
            "+    return False\n"
        )
        result = classify_doc_change(diff)
        assert result["classification"] == "significant"
        assert any("function" in r for r in result["reasons"])

    def test_new_class_is_significant(self):
        """New class definition → significant."""
        from datastore.docsdb.updater import classify_doc_change
        diff = (
            "--- a/file.py\n"
            "+++ b/file.py\n"
            "+class NewFeature:\n"
            "+    def __init__(self):\n"
            "+        pass\n"
        )
        result = classify_doc_change(diff)
        assert result["classification"] == "significant"
        assert any("class" in r for r in result["reasons"])

    def test_schema_change_is_significant(self):
        """Schema change → significant."""
        from datastore.docsdb.updater import classify_doc_change
        diff = (
            "--- a/datastore/memorydb/datastore/memorydb/schema.sql\n"
            "+++ b/datastore/memorydb/schema.sql\n"
            "+CREATE TABLE new_table (\n"
            "+    id INTEGER PRIMARY KEY\n"
            "+);\n"
        )
        result = classify_doc_change(diff)
        assert result["classification"] == "significant"
        assert any("schema" in r for r in result["reasons"])

    def test_alter_table_is_significant(self):
        """ALTER TABLE → significant."""
        from datastore.docsdb.updater import classify_doc_change
        diff = (
            "--- a/datastore/memorydb/datastore/memorydb/schema.sql\n"
            "+++ b/datastore/memorydb/schema.sql\n"
            "+ALTER TABLE users ADD COLUMN email TEXT;\n"
        )
        result = classify_doc_change(diff)
        assert result["classification"] == "significant"
        assert any("schema" in r for r in result["reasons"])

    def test_large_change_is_significant(self):
        """Large change (>50 lines) → significant."""
        from datastore.docsdb.updater import classify_doc_change
        lines = ["+" + f"line {i}\n" for i in range(60)]
        diff = "--- a/file.py\n+++ b/file.py\n" + "".join(lines)
        result = classify_doc_change(diff)
        assert result["classification"] == "significant"
        assert any("large change" in r for r in result["reasons"])
        assert result["lines_changed"] == 60

    def test_mixed_trivial_and_significant_is_significant(self):
        """Mixed trivial + significant signals → significant (safety default)."""
        from datastore.docsdb.updater import classify_doc_change
        diff = (
            "--- a/file.py\n"
            "+++ b/file.py\n"
            "-# old comment\n"
            "+# new comment\n"
            "+def new_function():\n"
            "+    pass\n"
            "+class NewClass:\n"
            "+    pass\n"
            "+CREATE TABLE foo (id INT);\n"
        )
        result = classify_doc_change(diff)
        assert result["classification"] == "significant"
        assert result["significant_signals"] > 0

    def test_small_change_counts_as_trivial_signal(self):
        """Changes <=5 lines get 'small change' trivial signal."""
        from datastore.docsdb.updater import classify_doc_change
        diff = (
            "--- a/file.txt\n"
            "+++ b/file.txt\n"
            "-old line\n"
            "+new line\n"
        )
        result = classify_doc_change(diff)
        assert any("small change" in r for r in result["reasons"])
        assert result["lines_changed"] == 2

    def test_confidence_increases_with_signals(self):
        """More signals → higher confidence."""
        from datastore.docsdb.updater import classify_doc_change
        # Single signal
        diff1 = (
            "--- a/file.py\n"
            "+++ b/file.py\n"
            "-# comment\n"
            "+# updated comment\n"
        )
        result1 = classify_doc_change(diff1)

        # Multiple trivial signals (small + comment + whitespace + typo-like)
        diff2 = (
            "--- a/file.py\n"
            "+++ b/file.py\n"
            "-# old commnet\n"
            "+# old comment\n"
            "-  \n"
            "+\n"
        )
        result2 = classify_doc_change(diff2)

        # Both should be trivial, but the one with more signals should have >= confidence
        assert result1["classification"] == "trivial"
        assert result2["classification"] == "trivial"
        assert result2["confidence"] >= result1["confidence"]

    def test_require_change_is_trivial(self):
        """require() import change → trivial."""
        from datastore.docsdb.updater import classify_doc_change
        diff = (
            "--- a/file.js\n"
            "+++ b/file.js\n"
            "-const x = require('old-module')\n"
            "+const x = require('new-module')\n"
        )
        result = classify_doc_change(diff)
        assert result["classification"] == "trivial"
        assert any("import" in r for r in result["reasons"])

    def test_export_change_is_significant(self):
        """Export API change → significant."""
        from datastore.docsdb.updater import classify_doc_change
        diff = (
            "--- a/file.js\n"
            "+++ b/file.js\n"
            "+export default function newApi() {\n"
            "+  return true;\n"
            "+}\n"
            "+export const CONSTANT = 42;\n"
        )
        result = classify_doc_change(diff)
        assert result["classification"] == "significant"
        assert any("API" in r for r in result["reasons"])

    def test_whitespace_only_diff_is_trivial(self):
        """Pure whitespace diff text → trivial."""
        from datastore.docsdb.updater import classify_doc_change
        result = classify_doc_change("   \n  \n  ")
        assert result["classification"] == "trivial"

    def test_result_shape(self):
        """Verify all expected keys are in the result."""
        from datastore.docsdb.updater import classify_doc_change
        result = classify_doc_change("+some change\n-old line\n")
        assert "classification" in result
        assert "confidence" in result
        assert "reasons" in result
        assert "lines_changed" in result
        assert "trivial_signals" in result
        assert "significant_signals" in result
        assert isinstance(result["reasons"], list)
        assert isinstance(result["confidence"], float)

    def test_destructive_change_is_significant(self):
        """Destructive operations (DROP/DELETE/REMOVE) → significant."""
        from datastore.docsdb.updater import classify_doc_change
        diff = (
            "--- a/datastore/memorydb/datastore/memorydb/schema.sql\n"
            "+++ b/datastore/memorydb/schema.sql\n"
            "+DROP TABLE old_table;\n"
            "+DELETE FROM configs WHERE obsolete = 1;\n"
        )
        result = classify_doc_change(diff)
        assert result["classification"] == "significant"
        assert any("destructive" in r for r in result["reasons"])


class TestCleanupStateLocking:
    def test_log_doc_update_honors_quaid_now(self, tmp_path, monkeypatch):
        with _adapter_patch(tmp_path):
            import datastore.docsdb.updater as updater

            monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:06:07Z")

            updater.log_doc_update(
                "docs/test.md",
                "janitor",
                ["src/app.py"],
                "updated",
                dry_run=False,
                success=False,
                chars_before=10,
                chars_after=10,
                notify=False,
            )

            entries = updater._load_changelog()
            assert entries[0]["timestamp"] == "2026-03-11T05:06:07+00:00"

    def test_log_doc_update_rejects_malformed_quaid_now(self, tmp_path, monkeypatch):
        with _adapter_patch(tmp_path):
            import datastore.docsdb.updater as updater

            monkeypatch.setenv("QUAID_NOW", "not-a-date")

            with pytest.raises(ValueError, match="Invalid QUAID_NOW"):
                updater.log_doc_update(
                    "docs/test.md",
                    "janitor",
                    ["src/app.py"],
                    "updated",
                    dry_run=False,
                    success=False,
                    chars_before=10,
                    chars_after=10,
                    notify=False,
                )

            assert not updater._changelog_path().exists()

    def test_reset_cleanup_state_honors_quaid_now(self, tmp_path, monkeypatch):
        with _adapter_patch(tmp_path):
            import datastore.docsdb.updater as updater

            monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:06:07Z")

            updater._reset_cleanup_state("docs/test.md", 123)

            state = updater._load_cleanup_state()
            assert state["docs/test.md"]["last_cleanup"] == "2026-03-11T05:06:07+00:00"

    def test_increment_update_count_is_thread_safe(self, tmp_path):
        with _adapter_patch(tmp_path):
            import datastore.docsdb.updater as updater

            with ThreadPoolExecutor(max_workers=12) as executor:
                list(executor.map(lambda _i: updater._increment_update_count("docs/test.md", 100), range(60)))

            state = updater._load_cleanup_state()
            assert state["docs/test.md"]["updates_since_cleanup"] == 60

    def test_log_doc_update_changelog_append_is_thread_safe(self, tmp_path):
        with _adapter_patch(tmp_path):
            import datastore.docsdb.updater as updater

            def _write(i):
                updater.log_doc_update(
                    "docs/test.md",
                    "janitor",
                    [f"src/{i}.py"],
                    f"entry {i}",
                    dry_run=False,
                    success=False,
                    chars_before=10,
                    chars_after=10,
                    notify=False,
                )

            with ThreadPoolExecutor(max_workers=12) as executor:
                list(executor.map(_write, range(60)))

            entries = updater._load_changelog()
            assert len(entries) == 60
            assert {entry["summary"] for entry in entries} == {f"entry {i}" for i in range(60)}


class TestAuditLogFallback:
    def test_get_update_log_warns_on_read_failure(self, tmp_path, caplog):
        with _adapter_patch(tmp_path):
            import datastore.docsdb.updater as updater

            caplog.set_level("WARNING")
            with patch.object(updater, "_ensure_audit_table", side_effect=RuntimeError("db offline")):
                rows = updater.get_update_log(limit=5)

            assert rows == []
            assert "Failed reading docs update audit log" in caplog.text


class TestDriftDetectionFallback:
    def test_detect_drift_registry_failure_raises_when_fail_hard(self, tmp_path, monkeypatch):
        cfg = _make_test_config(
            source_mapping={"src.py": {"docs": ["docs/doc.md"]}},
        )

        class _BrokenRegistry:
            def get_source_mappings(self):
                raise RuntimeError("registry offline")

        with patch("datastore.docsdb.updater.get_config", return_value=cfg), \
             _adapter_patch(tmp_path), \
             patch("datastore.docsdb.registry.DocsRegistry", return_value=_BrokenRegistry()):
            import datastore.docsdb.updater as updater

            monkeypatch.setattr(updater, "is_fail_hard_enabled", lambda: True)

            with pytest.raises(RuntimeError, match="Failed to load docs registry source mappings"):
                updater.detect_drift_from_git()

    def test_detect_drift_logs_git_timestamp_failures(self, tmp_path, monkeypatch, caplog):
        cfg = _make_test_config(
            source_mapping={"src.py": {"docs": ["docs/doc.md"]}},
        )

        with patch("datastore.docsdb.updater.get_config", return_value=cfg), \
             _adapter_patch(tmp_path) as iroot, \
             patch("datastore.docsdb.updater.subprocess.run", side_effect=RuntimeError("git unavailable")):
            doc = iroot / "docs" / "doc.md"
            src = iroot / "src.py"
            doc.parent.mkdir(parents=True, exist_ok=True)
            doc.write_text("# Doc\n")
            src.write_text("print('x')\n")

            import datastore.docsdb.updater as updater

            monkeypatch.setattr(updater, "is_fail_hard_enabled", lambda: False)
            caplog.set_level("WARNING")
            out = updater.detect_drift_from_git()

        assert out == []
        assert "Failed reading doc commit timestamp" in caplog.text

    def test_detect_drift_uses_conservative_lines_changed_fallback(self, tmp_path, monkeypatch, caplog):
        cfg = _make_test_config(
            source_mapping={"src.py": {"docs": ["docs/doc.md"]}},
        )

        def _fake_run(cmd, *args, **kwargs):
            command = " ".join(cmd)
            if "--format=%ct" in command and "docs/doc.md" in command:
                return MagicMock(stdout="100\n")
            if "--format=%ct" in command and "src.py" in command:
                return MagicMock(stdout="200\n")
            if "--format=%H" in command and "src.py" in command:
                return MagicMock(stdout="abc123\n")
            if "rev-list --count" in command and "src.py" in command:
                return MagicMock(stdout="3\n")
            if "diff --stat" in command and "src.py" in command:
                raise RuntimeError("stat unavailable")
            return MagicMock(stdout="")

        with patch("datastore.docsdb.updater.get_config", return_value=cfg), \
             _adapter_patch(tmp_path) as iroot, \
             patch("datastore.docsdb.updater.subprocess.run", side_effect=_fake_run), \
             patch("datastore.docsdb.updater._compute_staleness_score", return_value=42.0) as score_mock:
            doc = iroot / "docs" / "doc.md"
            src = iroot / "src.py"
            doc.parent.mkdir(parents=True, exist_ok=True)
            doc.write_text("# Doc\n")
            src.write_text("print('x')\n")

            import datastore.docsdb.updater as updater

            monkeypatch.setattr(updater, "is_fail_hard_enabled", lambda: False)
            caplog.set_level("WARNING")
            out = updater.detect_drift_from_git()

        assert len(out) == 1
        assert "Failed parsing changed-line stats for src.py" in caplog.text
        score_mock.assert_called_once()
        # args: commits_behind, lines_changed, days_stale
        assert score_mock.call_args.args[1] == 1

    def test_detect_drift_returns_partial_results_when_budget_exhausted(self, tmp_path, monkeypatch, caplog):
        cfg = _make_test_config(
            source_mapping={
                "src1.py": {"docs": ["docs/doc1.md"]},
                "src2.py": {"docs": ["docs/doc2.md"]},
            },
        )

        timeout_seq = [
            0.01,  # doc1 commit ts
            0.01,  # src1 commit ts
            0.01,  # src1 commit hash
            0.01,  # src1 rev-list count
            0.01,  # src1 diff --stat
            None,  # doc2 commit ts -> budget exhausted
        ]

        def _fake_run(cmd, *args, **kwargs):
            command = " ".join(cmd)
            if "--format=%ct" in command and "docs/doc1.md" in command:
                return MagicMock(stdout="100\n")
            if "--format=%ct" in command and "src1.py" in command:
                return MagicMock(stdout="200\n")
            if "--format=%H" in command and "src1.py" in command:
                return MagicMock(stdout="abc123\n")
            if "rev-list --count" in command and "src1.py" in command:
                return MagicMock(stdout="2\n")
            if "diff --stat" in command and "src1.py" in command:
                return MagicMock(stdout=" 1 file changed, 3 insertions(+), 1 deletion(-)\n")
            return MagicMock(stdout="0\n")

        with patch("datastore.docsdb.updater.get_config", return_value=cfg), \
             _adapter_patch(tmp_path) as iroot, \
             patch("datastore.docsdb.updater._git_timeout_from_deadline", side_effect=timeout_seq), \
             patch("datastore.docsdb.updater.subprocess.run", side_effect=_fake_run), \
             patch("datastore.docsdb.updater._compute_staleness_score", return_value=77.0):
            doc1 = iroot / "docs" / "doc1.md"
            src1 = iroot / "src1.py"
            doc1.parent.mkdir(parents=True, exist_ok=True)
            doc1.write_text("# Doc 1\n")
            src1.write_text("print('one')\n")

            doc2 = iroot / "docs" / "doc2.md"
            src2 = iroot / "src2.py"
            doc2.write_text("# Doc 2\n")
            src2.write_text("print('two')\n")

            import datastore.docsdb.updater as updater
            monkeypatch.setattr(updater, "is_fail_hard_enabled", lambda: False)
            caplog.set_level("WARNING")
            out = updater.detect_drift_from_git()

        assert len(out) == 1
        assert out[0].doc_path == "docs/doc1.md"
        assert "Git subprocess budget exhausted while reading doc commit timestamp for docs/doc2.md" in caplog.text


def test_bounded_diff_context_collects_source_diffs_in_parallel(monkeypatch):
    from datastore.docsdb import updater

    barrier = threading.Barrier(4)
    thread_names: set[str] = set()

    def _fake_diff(src, _mtime):
        thread_names.add(threading.current_thread().name)
        barrier.wait(timeout=2)
        return f"diff for {src}"

    monkeypatch.setattr(updater, "get_git_diff", _fake_diff)
    monkeypatch.setattr(updater, "_max_diff_total_bytes", lambda: 100_000)
    monkeypatch.setattr(updater, "is_fail_hard_enabled", lambda: True)

    context = updater._bounded_diff_context(["a.py", "b.py", "c.py", "d.py"], 0)

    assert "diff for a.py" in context.text
    assert "diff for d.py" in context.text
    assert len(thread_names) == 4


def test_save_changelog_uses_atomic_replace(tmp_path):
    with _adapter_patch(tmp_path):
        from datastore.docsdb import updater
        with patch("datastore.docsdb.updater.os.replace", wraps=updater.os.replace) as mock_replace:
            updater._save_changelog([{"timestamp": "2026-02-26T00:00:00"}])
        assert mock_replace.call_count >= 1


def test_update_doc_from_transcript_skips_partial_edit_blocks(tmp_path, monkeypatch):
    with _adapter_patch(tmp_path) as iroot:
        from datastore.docsdb import updater

        doc = iroot / "docs" / "doc.md"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("# Doc\n\nExisting line.\n", encoding="utf-8")

        response = """<<<EDIT
OLD: Existing line.
NEW: Updated line.
>>>
<<<EDIT
OLD: Missing line.
NEW: Should not be written.
>>>
<<<SUMMARY: partial >>>"""

        monkeypatch.setattr(updater, "call_deep_reasoning", lambda **_kwargs: (response, 0.1))
        monkeypatch.setattr(updater, "is_fail_hard_enabled", lambda: False)

        ok = updater.update_doc_from_transcript(
            "docs/doc.md",
            "test purpose",
            "transcript",
            dry_run=False,
        )

    assert ok is False
    assert doc.read_text(encoding="utf-8") == "# Doc\n\nExisting line.\n"


def test_update_doc_from_transcript_unmatched_edit_raises_when_fail_hard(tmp_path, monkeypatch):
    with _adapter_patch(tmp_path) as iroot:
        from datastore.docsdb import updater

        doc = iroot / "docs" / "doc.md"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("# Doc\n\nExisting line.\n", encoding="utf-8")

        response = """<<<EDIT
OLD: Missing line.
NEW: Should not be written.
>>>
<<<SUMMARY: partial >>>"""

        monkeypatch.setattr(updater, "call_deep_reasoning", lambda **_kwargs: (response, 0.1))
        monkeypatch.setattr(updater, "is_fail_hard_enabled", lambda: True)

        with pytest.raises(RuntimeError, match="unmatched edit blocks"):
            updater.update_doc_from_transcript(
                "docs/doc.md",
                "test purpose",
                "transcript",
                dry_run=False,
            )

    assert doc.read_text(encoding="utf-8") == "# Doc\n\nExisting line.\n"


def test_update_doc_from_transcript_registry_timestamp_honors_quaid_now(tmp_path, monkeypatch):
    with _adapter_patch(tmp_path) as iroot:
        from datastore.docsdb import updater

        doc = iroot / "docs" / "doc.md"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("# Doc\n\nExisting line.\n", encoding="utf-8")

        captured = []

        class _FakeRegistry:
            def update_timestamps(self, doc_path, **kwargs):
                captured.append((doc_path, kwargs))

        response = """<<<EDIT
OLD: Existing line.
NEW: Updated line.
>>>
<<<SUMMARY: update >>>"""

        monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:06:07Z")
        monkeypatch.setattr(updater, "call_deep_reasoning", lambda **_kwargs: (response, 0.1))
        monkeypatch.setattr("datastore.docsdb.registry.DocsRegistry", _FakeRegistry)

        ok = updater.update_doc_from_transcript(
            "docs/doc.md",
            "test purpose",
            "transcript",
            dry_run=False,
        )

        assert ok is True
        assert captured == [
            ("docs/doc.md", {"modified_at": "2026-03-11T05:06:07+00:00"})
        ]


class TestCmdUpdateStaleNeverIndexed:
    def test_update_doc_from_diffs_caps_total_diff_prompt(self, tmp_path, monkeypatch):
        with _adapter_patch(tmp_path) as iroot:
            from datastore.docsdb import updater

            doc = iroot / "docs" / "doc.md"
            doc.parent.mkdir(parents=True, exist_ok=True)
            doc.write_text("# Doc\n\nExisting details.\n", encoding="utf-8")

            monkeypatch.setenv("QUAID_DOCS_PROMPT_DIFF_MAX_BYTES", "4096")
            monkeypatch.setattr(updater, "get_git_diff", lambda *_args, **_kwargs: "X" * 8000)

            captured = {}

            def _fake_deep(prompt, system_prompt=None, max_tokens=0, timeout=0):
                captured["prompt"] = prompt
                return "# Doc\n\nUpdated safely.\n<!-- CHANGE_SUMMARY: bounded -->", 0.1

            monkeypatch.setattr(updater, "call_deep_reasoning", _fake_deep)

            ok = updater.update_doc_from_diffs(
                "docs/doc.md",
                "test purpose",
                ["src/large.txt"],
                dry_run=True,
            )

        assert ok is False
        prompt = captured["prompt"]
        assert "QUAID DOCS SAFETY NOTE" in prompt
        assert "Project diff catalog limit reached" in prompt
        assert len(prompt.encode("utf-8")) < 7000

    def test_update_doc_from_diffs_gate_skip_does_not_count_as_written(self, tmp_path, monkeypatch):
        with _adapter_patch(tmp_path) as iroot:
            from datastore.docsdb import updater

            doc = iroot / "docs" / "doc.md"
            doc.parent.mkdir(parents=True, exist_ok=True)
            doc.write_text("# Doc\n\nExisting details.\n", encoding="utf-8")

            monkeypatch.setattr(updater, "get_git_diff", lambda *_args, **_kwargs: "+# comment only\n")
            monkeypatch.setattr(
                updater,
                "classify_doc_change",
                lambda _diff: {
                    "classification": "trivial",
                    "confidence": 0.9,
                    "reasons": ["comment-only"],
                    "lines_changed": 1,
                    "trivial_signals": 1,
                    "significant_signals": 0,
                },
            )

            ok = updater.update_doc_from_diffs(
                "docs/doc.md",
                "test purpose",
                ["src/comment.py"],
                dry_run=False,
            )

            assert ok is False
            assert doc.read_text(encoding="utf-8") == "# Doc\n\nExisting details.\n"

    def test_update_doc_from_diffs_skips_stale_write_when_doc_changes(self, tmp_path, monkeypatch):
        with _adapter_patch(tmp_path) as iroot:
            from datastore.docsdb import updater

            doc = iroot / "docs" / "doc.md"
            doc.parent.mkdir(parents=True, exist_ok=True)
            doc.write_text("# Doc\n\nExisting details.\n", encoding="utf-8")

            monkeypatch.setattr(updater, "get_git_diff", lambda *_args, **_kwargs: "+meaningful change\n")
            monkeypatch.setattr(
                updater,
                "classify_doc_change",
                lambda _diff: {
                    "classification": "significant",
                    "confidence": 0.95,
                    "reasons": ["meaningful"],
                    "lines_changed": 1,
                    "trivial_signals": 0,
                    "significant_signals": 1,
                },
            )
            monkeypatch.setattr(
                updater,
                "call_deep_reasoning",
                lambda **_kwargs: ("# Doc\n\nLLM update.\n<!-- CHANGE_SUMMARY: update -->", 0.1),
            )
            monkeypatch.setattr(updater, "is_fail_hard_enabled", lambda: False)

            expected_lock = doc.with_name(f".{doc.name}.doc-update.lock")
            lock_paths = []

            @contextmanager
            def _race_lock(path):
                lock_paths.append(path)
                if path == expected_lock:
                    doc.write_text("# Doc\n\nConcurrent update.\n", encoding="utf-8")
                yield

            monkeypatch.setattr(updater, "_file_lock", _race_lock)

            ok = updater.update_doc_from_diffs(
                "docs/doc.md",
                "test purpose",
                ["src/meaningful.py"],
                dry_run=False,
            )

            assert ok is False
            assert expected_lock in lock_paths
            assert doc.read_text(encoding="utf-8") == "# Doc\n\nConcurrent update.\n"

    def test_update_doc_from_diffs_registry_timestamp_honors_quaid_now(self, tmp_path, monkeypatch):
        with _adapter_patch(tmp_path) as iroot:
            from datastore.docsdb import updater

            doc = iroot / "docs" / "doc.md"
            doc.parent.mkdir(parents=True, exist_ok=True)
            doc.write_text("# Doc\n\nExisting details.\n", encoding="utf-8")

            captured = []

            class _FakeRegistry:
                def update_timestamps(self, doc_path, **kwargs):
                    captured.append((doc_path, kwargs))

            monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:06:07Z")
            monkeypatch.setattr(
                updater,
                "get_config",
                lambda: SimpleNamespace(
                    docs=SimpleNamespace(
                        core_markdown=SimpleNamespace(files={}),
                        notify_on_update=False,
                    )
                ),
            )
            monkeypatch.setattr(updater, "get_git_diff", lambda *_args, **_kwargs: "+meaningful change\n")
            monkeypatch.setattr(
                updater,
                "classify_doc_change",
                lambda _diff: {
                    "classification": "significant",
                    "confidence": 0.95,
                    "reasons": ["meaningful"],
                    "lines_changed": 1,
                    "trivial_signals": 0,
                    "significant_signals": 1,
                },
            )
            monkeypatch.setattr(
                updater,
                "call_deep_reasoning",
                lambda **_kwargs: ("# Doc\n\nUpdated.\n<!-- CHANGE_SUMMARY: update -->", 0.1),
            )
            monkeypatch.setattr("datastore.docsdb.registry.DocsRegistry", _FakeRegistry)

            ok = updater.update_doc_from_diffs(
                "docs/doc.md",
                "test purpose",
                ["src/meaningful.py"],
                dry_run=False,
            )

            assert ok is True
            assert captured == [
                ("docs/doc.md", {"modified_at": "2026-03-11T05:06:07+00:00"})
            ]

    def test_fast_gate_failure_raises_when_fail_hard(self, tmp_path, monkeypatch):
        with _adapter_patch(tmp_path) as iroot:
            from datastore.docsdb import updater

            doc = iroot / "docs" / "doc.md"
            doc.parent.mkdir(parents=True, exist_ok=True)
            doc.write_text("# Doc\n\nExisting details.\n", encoding="utf-8")

            monkeypatch.setattr(updater, "get_git_diff", lambda *_args, **_kwargs: "+meaningful change\n")
            monkeypatch.setattr(
                updater,
                "classify_doc_change",
                lambda _diff: {
                    "classification": "significant",
                    "confidence": 0.5,
                    "reasons": ["borderline"],
                    "lines_changed": 1,
                    "trivial_signals": 0,
                    "significant_signals": 1,
                },
            )

            def _fail_fast_gate(**_kwargs):
                raise TimeoutError("fast gate timed out")

            def _unexpected_deep(**_kwargs):
                raise AssertionError("Deep Reasoning should not run after Fast Reasoning failure")

            monkeypatch.setattr("lib.llm_chunked_call.parallel_llm_call", _fail_fast_gate)
            monkeypatch.setattr(updater, "call_deep_reasoning", _unexpected_deep)
            monkeypatch.setattr(updater, "is_fail_hard_enabled", lambda: True)

            with pytest.raises(RuntimeError, match="Fast Reasoning gate failed"):
                updater.update_doc_from_diffs(
                    "docs/doc.md",
                    "test purpose",
                    ["src/borderline.py"],
                    dry_run=False,
                )

            assert doc.read_text(encoding="utf-8") == "# Doc\n\nExisting details.\n"

    def test_skips_protected_project_log_staleness_update(self, tmp_path, monkeypatch):
        with _adapter_patch(tmp_path):
            from datastore.docsdb import updater

            called = []

            class _FakeRegistry:
                def list_docs(self, project=None):
                    return []

            class _FakeRag:
                def needs_reindex_many(self, paths):
                    return {}

            monkeypatch.setattr(
                updater,
                "check_staleness",
                lambda project=None: {
                    "projects/demo/PROJECT.log": SimpleNamespace(
                        change_classification=None,
                        stale_sources=["src/app.py"],
                    )
                },
            )
            monkeypatch.setattr(updater, "get_doc_purposes", lambda: {})
            monkeypatch.setattr("datastore.docsdb.registry.DocsRegistry", _FakeRegistry)
            monkeypatch.setattr("datastore.docsdb.rag.DocsRAG", _FakeRag)
            monkeypatch.setattr("core.docs.updater.index_project_logs", lambda project=None: 0)
            monkeypatch.setattr(
                updater,
                "update_doc_from_diffs",
                lambda *args, **kwargs: called.append((args, kwargs)) or True,
            )

            count = updater.cmd_update_stale(
                dry_run=False,
                project="demo",
                protected_names={"PROJECT.log"},
            )

            assert count == 0
            assert called == []

    def test_indexes_registry_relative_paths_via_registry_resolver(self, tmp_path, monkeypatch):
        with _adapter_patch(tmp_path) as iroot:
            from datastore.docsdb import updater

            rel_doc = "docs/newly-registered.md"
            abs_doc = iroot / rel_doc
            abs_doc.parent.mkdir(parents=True, exist_ok=True)
            abs_doc.write_text("# Canary\n\nfresh content\n", encoding="utf-8")

            class _FakeRegistry:
                def list_docs(self, project=None):
                    return [{"file_path": rel_doc, "last_indexed_at": None}]

                def _resolve_path(self, path_str):
                    return iroot / path_str

            indexed = []

            class _FakeRag:
                def needs_reindex_many(self, paths):
                    return {str(Path(p)): False for p in paths}

                def index_document(self, file_path):
                    indexed.append(str(file_path))
                    return 1

            monkeypatch.setattr("datastore.docsdb.registry.DocsRegistry", _FakeRegistry)
            monkeypatch.setattr("datastore.docsdb.rag.DocsRAG", _FakeRag)
            monkeypatch.setattr(updater, "check_staleness", lambda project=None: {})
            monkeypatch.setattr("core.docs.updater.index_project_logs", lambda project=None: 0)

            count = updater.cmd_update_stale(dry_run=False, project="quaid")
            assert count == 1
            assert indexed == [str(abs_doc.resolve())]

    def test_update_stale_indexes_append_only_project_logs(self, tmp_path, monkeypatch):
        with _adapter_patch(tmp_path):
            from datastore.docsdb import updater

            class _FakeRegistry:
                def list_docs(self, project=None):
                    return []

            class _FakeRag:
                def needs_reindex_many(self, paths):
                    return {}

            indexed_projects = []

            monkeypatch.setattr("datastore.docsdb.registry.DocsRegistry", _FakeRegistry)
            monkeypatch.setattr("datastore.docsdb.rag.DocsRAG", _FakeRag)
            monkeypatch.setattr(updater, "check_staleness", lambda project=None: {})
            count = updater.cmd_update_stale(
                dry_run=False,
                project="quaid",
                project_log_indexer=lambda project=None: indexed_projects.append(project) or 1,
            )

            assert count == 1
            assert indexed_projects == ["quaid"]

    def test_update_stale_cli_main_accepts_project_log_indexer(self, tmp_path, monkeypatch):
        with _adapter_patch(tmp_path):
            from datastore.docsdb import updater

            captured = {}

            def fake_cmd_update_stale(**kwargs):
                captured.update(kwargs)
                return 0

            sentinel_indexer = object()
            monkeypatch.setattr(updater, "cmd_update_stale", fake_cmd_update_stale)

            rc = updater.main(
                ["update-stale", "--apply", "--project", "quaid"],
                project_log_indexer=sentinel_indexer,
            )

            assert rc == 0
            assert captured["dry_run"] is False
            assert captured["trivial_only"] is False
            assert captured["project"] == "quaid"
            assert captured["project_log_indexer"] is sentinel_indexer

    def test_core_docs_updater_cli_wires_project_log_indexer(self, tmp_path, monkeypatch):
        with _adapter_patch(tmp_path):
            from core.docs import updater as core_updater

            captured = {}

            def fake_main(argv=None, *, project_log_indexer=None):
                captured["argv"] = argv
                captured["project_log_indexer"] = project_log_indexer
                return 0

            monkeypatch.setattr(core_updater._updater, "main", fake_main)

            rc = core_updater.main(["update-stale", "--apply", "--project", "quaid"])

            assert rc == 0
            assert captured["argv"] == ["update-stale", "--apply", "--project", "quaid"]
            assert captured["project_log_indexer"] is core_updater.index_project_logs

    def test_update_stale_raises_for_missing_explicit_project(self, tmp_path, monkeypatch):
        with _adapter_patch(tmp_path):
            from datastore.docsdb import updater

            class _FakeRegistry:
                def list_projects(self):
                    return [{"name": "quaid"}]

            monkeypatch.setattr("datastore.docsdb.registry.DocsRegistry", _FakeRegistry)

            with pytest.raises(RuntimeError, match="Project not found for docs update: missing-proj"):
                updater.cmd_update_stale(dry_run=False, project="missing-proj")

    def test_update_stale_normalizes_explicit_project_case(self, tmp_path, monkeypatch):
        with _adapter_patch(tmp_path):
            from datastore.docsdb import updater

            class _FakeRegistry:
                def list_projects(self):
                    return [{"name": "livetest-agentmsg-cdx"}]

                def list_docs(self, project=None):
                    captured["list_docs_project"] = project
                    return []

            class _FakeRag:
                def needs_reindex_many(self, paths):
                    return {}

            captured = {}
            indexed_projects = []

            monkeypatch.setattr("datastore.docsdb.registry.DocsRegistry", _FakeRegistry)
            monkeypatch.setattr("datastore.docsdb.rag.DocsRAG", _FakeRag)
            monkeypatch.setattr(
                updater,
                "check_staleness",
                lambda project=None: captured.update({"staleness_project": project}) or {},
            )

            count = updater.cmd_update_stale(
                dry_run=False,
                project="livetest-agentmsg-CDX",
                project_log_indexer=lambda project=None: indexed_projects.append(project) or 0,
            )

            assert count == 0
            assert captured["staleness_project"] == "livetest-agentmsg-cdx"
            assert captured["list_docs_project"] == "livetest-agentmsg-cdx"
            assert indexed_projects == ["livetest-agentmsg-cdx"]

    def test_reindexes_registry_doc_when_timestamp_exists_but_chunks_are_missing(self, tmp_path, monkeypatch):
        with _adapter_patch(tmp_path) as iroot:
            from datastore.docsdb import updater

            doc_path = iroot / "docs" / "registered.md"
            doc_path.parent.mkdir(parents=True, exist_ok=True)
            doc_path.write_text("# Registered\n\nstale vec/doc chunks\n", encoding="utf-8")

            class _FakeRegistry:
                def list_docs(self, project=None):
                    return [{"file_path": str(doc_path), "last_indexed_at": "2026-04-16T00:00:00"}]

                def _resolve_path(self, path_str):
                    return Path(path_str)

            indexed = []

            class _FakeRag:
                def needs_reindex_many(self, paths):
                    return {str(Path(p)): True for p in paths}

                def index_document(self, file_path):
                    indexed.append(str(file_path))
                    return 2

            monkeypatch.setattr("datastore.docsdb.registry.DocsRegistry", _FakeRegistry)
            monkeypatch.setattr("datastore.docsdb.rag.DocsRAG", _FakeRag)
            monkeypatch.setattr(updater, "check_staleness", lambda project=None: {})
            monkeypatch.setattr("core.docs.updater.index_project_logs", lambda project=None: 0)

            count = updater.cmd_update_stale(dry_run=False, project="quaid")
            assert count == 1
            assert indexed == [str(doc_path.resolve())]

    def test_update_stale_notifies_agent_on_unresolved_registry_path(self, tmp_path, monkeypatch):
        with _adapter_patch(tmp_path):
            from datastore.docsdb import updater

            class _FakeRegistry:
                def list_docs(self, project=None):
                    return [{"file_path": "docs/missing.md", "last_indexed_at": None}]

                def _resolve_path(self, path_str):
                    raise RuntimeError("broken resolver")

            class _FakeRag:
                def needs_reindex_many(self, paths):
                    return {}

                def index_document(self, file_path):
                    raise AssertionError("index_document should not run for unresolved path")

            notices = []
            monkeypatch.setattr("datastore.docsdb.registry.DocsRegistry", _FakeRegistry)
            monkeypatch.setattr("datastore.docsdb.rag.DocsRAG", _FakeRag)
            monkeypatch.setattr(updater, "check_staleness", lambda project=None: {})
            monkeypatch.setattr("core.docs.updater.index_project_logs", lambda project=None: 0)
            monkeypatch.setattr(updater, "is_fail_hard_enabled", lambda: False)
            monkeypatch.setattr(
                updater,
                "notify_agent",
                lambda message, **kwargs: notices.append((message, kwargs)) or True,
            )

            count = updater.cmd_update_stale(dry_run=False, project="quaid")
            assert count == 0
            assert notices
            assert "unresolved registry path" in notices[0][0]
            assert notices[0][1]["severity"] == "warning"

    def test_update_stale_raises_on_unresolved_registry_path_when_fail_hard(self, tmp_path, monkeypatch):
        with _adapter_patch(tmp_path):
            from datastore.docsdb import updater

            class _FakeRegistry:
                def list_docs(self, project=None):
                    return [{"file_path": "docs/missing.md", "last_indexed_at": None}]

                def _resolve_path(self, path_str):
                    raise RuntimeError("broken resolver")

            class _FakeRag:
                def needs_reindex_many(self, paths):
                    return {}

            monkeypatch.setattr("datastore.docsdb.registry.DocsRegistry", _FakeRegistry)
            monkeypatch.setattr("datastore.docsdb.rag.DocsRAG", _FakeRag)
            monkeypatch.setattr(updater, "check_staleness", lambda project=None: {})
            monkeypatch.setattr(updater, "is_fail_hard_enabled", lambda: True)
            monkeypatch.setattr(updater, "notify_agent", lambda *args, **kwargs: True)

            with pytest.raises(RuntimeError, match="Failed to resolve docs registry path"):
                updater.cmd_update_stale(dry_run=False, project="quaid")

    def test_update_stale_raises_on_missing_registered_path_when_fail_hard(self, tmp_path, monkeypatch):
        with _adapter_patch(tmp_path):
            from datastore.docsdb import updater

            missing = tmp_path / "docs" / "missing.md"

            class _FakeRegistry:
                def list_docs(self, project=None):
                    return [{"file_path": str(missing), "last_indexed_at": None}]

                def _resolve_path(self, path_str):
                    return Path(path_str)

            class _FakeRag:
                def needs_reindex_many(self, paths):
                    return {}

            monkeypatch.setattr("datastore.docsdb.registry.DocsRegistry", _FakeRegistry)
            monkeypatch.setattr("datastore.docsdb.rag.DocsRAG", _FakeRag)
            monkeypatch.setattr(updater, "check_staleness", lambda project=None: {})
            monkeypatch.setattr(updater, "is_fail_hard_enabled", lambda: True)
            monkeypatch.setattr(updater, "notify_agent", lambda *args, **kwargs: True)

            with pytest.raises(RuntimeError, match="missing registered path"):
                updater.cmd_update_stale(dry_run=False, project="quaid")

    def test_update_stale_warns_and_stops_when_registry_index_times_out(self, tmp_path, monkeypatch):
        with _adapter_patch(tmp_path) as iroot:
            from datastore.docsdb import updater

            doc_path = iroot / "docs" / "hung.md"
            doc_path.parent.mkdir(parents=True, exist_ok=True)
            doc_path.write_text("# Hung\n", encoding="utf-8")

            class _FakeRegistry:
                def list_docs(self, project=None):
                    return [{"file_path": str(doc_path), "last_indexed_at": None}]

                def _resolve_path(self, path_str):
                    return Path(path_str)

            class _FakeRag:
                def needs_reindex_many(self, paths):
                    return {str(Path(p)): True for p in paths}

            notices = []
            monkeypatch.setattr("datastore.docsdb.registry.DocsRegistry", _FakeRegistry)
            monkeypatch.setattr("datastore.docsdb.rag.DocsRAG", _FakeRag)
            monkeypatch.setattr(updater, "check_staleness", lambda project=None: {})
            monkeypatch.setattr("core.docs.updater.index_project_logs", lambda project=None: 0)
            monkeypatch.setattr(updater, "is_fail_hard_enabled", lambda: False)
            monkeypatch.setattr(
                updater,
                "_index_doc_with_timeout",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("simulated hang")),
            )
            monkeypatch.setattr(
                updater,
                "notify_agent",
                lambda message, **kwargs: notices.append((message, kwargs)) or True,
            )

            count = updater.cmd_update_stale(dry_run=False, project="quaid")
            assert count == 0
            assert notices
            assert "index timeout" in notices[0][0]
            assert notices[0][1]["severity"] == "warning"

    def test_update_stale_raises_when_registry_index_times_out_under_fail_hard(self, tmp_path, monkeypatch):
        with _adapter_patch(tmp_path) as iroot:
            from datastore.docsdb import updater

            doc_path = iroot / "docs" / "hung-hard.md"
            doc_path.parent.mkdir(parents=True, exist_ok=True)
            doc_path.write_text("# Hung Hard\n", encoding="utf-8")

            class _FakeRegistry:
                def list_docs(self, project=None):
                    return [{"file_path": str(doc_path), "last_indexed_at": None}]

                def _resolve_path(self, path_str):
                    return Path(path_str)

            class _FakeRag:
                def needs_reindex_many(self, paths):
                    return {str(Path(p)): True for p in paths}

            monkeypatch.setattr("datastore.docsdb.registry.DocsRegistry", _FakeRegistry)
            monkeypatch.setattr("datastore.docsdb.rag.DocsRAG", _FakeRag)
            monkeypatch.setattr(updater, "check_staleness", lambda project=None: {})
            monkeypatch.setattr(updater, "is_fail_hard_enabled", lambda: True)
            monkeypatch.setattr(
                updater,
                "_index_doc_with_timeout",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("simulated hang")),
            )
            monkeypatch.setattr(updater, "notify_agent", lambda *args, **kwargs: True)

            with pytest.raises(RuntimeError, match="docs update index timeout"):
                updater.cmd_update_stale(dry_run=False, project="quaid")

    def test_update_stale_warns_when_registry_index_fails_fail_open(self, tmp_path, monkeypatch, caplog):
        with _adapter_patch(tmp_path) as iroot:
            from datastore.docsdb import updater

            doc_path = iroot / "docs" / "broken.md"
            doc_path.parent.mkdir(parents=True, exist_ok=True)
            doc_path.write_text("# Broken\n", encoding="utf-8")

            class _FakeRegistry:
                def list_docs(self, project=None):
                    return [{"file_path": str(doc_path), "last_indexed_at": None}]

                def _resolve_path(self, path_str):
                    return Path(path_str)

            class _FakeRag:
                def needs_reindex_many(self, paths):
                    return {str(Path(p)): True for p in paths}

            monkeypatch.setattr("datastore.docsdb.registry.DocsRegistry", _FakeRegistry)
            monkeypatch.setattr("datastore.docsdb.rag.DocsRAG", _FakeRag)
            monkeypatch.setattr(updater, "check_staleness", lambda project=None: {})
            monkeypatch.setattr("core.docs.updater.index_project_logs", lambda project=None: 0)
            monkeypatch.setattr(updater, "is_fail_hard_enabled", lambda: False)
            monkeypatch.setattr(
                updater,
                "_index_doc_with_timeout",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("index failed")),
            )

            caplog.set_level("WARNING")
            count = updater.cmd_update_stale(dry_run=False, project="quaid")

        assert count == 0
        assert "failed to index" in caplog.text

    def test_update_stale_raises_when_registry_index_fails_fail_hard(self, tmp_path, monkeypatch):
        with _adapter_patch(tmp_path) as iroot:
            from datastore.docsdb import updater

            doc_path = iroot / "docs" / "broken-hard.md"
            doc_path.parent.mkdir(parents=True, exist_ok=True)
            doc_path.write_text("# Broken Hard\n", encoding="utf-8")

            class _FakeRegistry:
                def list_docs(self, project=None):
                    return [{"file_path": str(doc_path), "last_indexed_at": None}]

                def _resolve_path(self, path_str):
                    return Path(path_str)

            class _FakeRag:
                def needs_reindex_many(self, paths):
                    return {str(Path(p)): True for p in paths}

            monkeypatch.setattr("datastore.docsdb.registry.DocsRegistry", _FakeRegistry)
            monkeypatch.setattr("datastore.docsdb.rag.DocsRAG", _FakeRag)
            monkeypatch.setattr(updater, "check_staleness", lambda project=None: {})
            monkeypatch.setattr(updater, "is_fail_hard_enabled", lambda: True)
            monkeypatch.setattr(
                updater,
                "_index_doc_with_timeout",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("index failed")),
            )

            with pytest.raises(RuntimeError, match="failed to index registered doc"):
                updater.cmd_update_stale(dry_run=False, project="quaid")
