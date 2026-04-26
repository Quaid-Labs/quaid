"""Tests for review-driven fixes: slug unification, provider outage shutdown,
deferred extraction queue, and daemon stale-doc indexing."""

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Slug unification (lib/instance.py)
# ---------------------------------------------------------------------------

class TestInstanceSlug:
    def test_basic_slug(self, tmp_path):
        from lib.instance import instance_slug_from_project_dir
        project = tmp_path / "my-project"
        project.mkdir()
        slug = instance_slug_from_project_dir(str(project))
        assert "my-project" in slug
        assert all(c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in slug)

    def test_resolves_symlinks(self, tmp_path):
        """Ensure .resolve() is called so symlinks produce consistent slugs."""
        from lib.instance import instance_slug_from_project_dir
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real_dir)
        assert instance_slug_from_project_dir(str(link)) == instance_slug_from_project_dir(str(real_dir))

    def test_strips_special_chars(self):
        from lib.instance import instance_slug_from_project_dir
        slug = instance_slug_from_project_dir("/tmp/My Project (v2)!")
        # Should only contain [a-z0-9-]
        assert all(c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in slug)
        assert not slug.startswith("-")
        assert not slug.endswith("-")

    def test_cc_adapter_reexports(self):
        """CC adapter should re-export the same function from lib.instance."""
        from lib.instance import instance_slug_from_project_dir as lib_fn
        from adaptors.claude_code.adapter import instance_slug_from_project_dir as cc_fn
        assert lib_fn is cc_fn


# ---------------------------------------------------------------------------
# ProviderUnavailableError (lib/llm_clients.py)
# ---------------------------------------------------------------------------

class TestProviderUnavailableError:
    def test_is_exception(self):
        from lib.llm_clients import ProviderUnavailableError
        assert issubclass(ProviderUnavailableError, Exception)

    def test_not_runtime_error(self):
        """Must NOT be a RuntimeError subclass so daemon can distinguish it."""
        from lib.llm_clients import ProviderUnavailableError
        assert not issubclass(ProviderUnavailableError, RuntimeError)

    def test_daemon_retries_provider_error_by_default(self, monkeypatch):
        """ProviderUnavailableError should NOT kill daemon by default — retry loop handles it."""
        from lib.llm_clients import ProviderUnavailableError
        from core import extraction_daemon

        process_calls = 0

        class _StopLoop(Exception):
            pass

        def fake_process_signal(_sig):
            nonlocal process_calls
            process_calls += 1
            raise ProviderUnavailableError("provider down")

        def fake_sleep(_s):
            if process_calls >= 1:
                raise _StopLoop()

        monkeypatch.setattr(extraction_daemon, "write_pid", lambda _pid: None)
        monkeypatch.setattr(extraction_daemon, "remove_pid", lambda: None)
        monkeypatch.setattr(extraction_daemon, "read_pending_signals", lambda: [{"type": "rolling"}])
        monkeypatch.setattr(extraction_daemon, "process_signal", fake_process_signal)
        monkeypatch.setattr(extraction_daemon.time, "sleep", fake_sleep)
        monkeypatch.setattr(extraction_daemon.signal, "signal", lambda *_a, **_k: None)

        # Should NOT raise ProviderUnavailableError — it should be caught and retried.
        # We stop the loop via _StopLoop from fake_sleep.
        with pytest.raises(_StopLoop):
            extraction_daemon.daemon_loop(poll_interval=0.0, idle_check_interval=999999.0)
        assert process_calls >= 1

    def test_provider_config_errors_notify_even_without_fail_hard(self, monkeypatch):
        """Broken model config should be visible on every failed live request."""
        from lib import llm_clients

        notices = []

        class FakeProvider:
            def llm_call(self, *_args, **_kwargs):
                raise RuntimeError("invalid-model-m6-probe model not found")

        monkeypatch.setattr(llm_clients, "_load_model_config", lambda: None)
        monkeypatch.setattr(llm_clients, "get_llm_provider", lambda model_tier=None: FakeProvider())
        monkeypatch.setattr(llm_clients, "is_fail_hard_enabled", lambda: False)
        monkeypatch.setattr(llm_clients, "estimate_cost", lambda: 0.0)
        monkeypatch.setattr(llm_clients, "is_token_budget_exhausted", lambda: False)
        monkeypatch.setattr(
            llm_clients,
            "_notify_provider_access_error",
            lambda **kwargs: notices.append(kwargs),
        )

        with pytest.raises(RuntimeError, match="invalid-model-m6-probe"):
            llm_clients.call_llm(
                system_prompt="system",
                user_message="user",
                model="invalid-model-m6-probe",
                model_tier="fast",
                max_retries=0,
            )

        assert len(notices) == 1
        assert notices[0]["dedupe_prefix"] == "llm-config"
        assert notices[0]["model"] == "invalid-model-m6-probe"


# ---------------------------------------------------------------------------
# Deferred extraction queue (core/extraction_daemon.py)
# ---------------------------------------------------------------------------

class TestDeferredExtraction:
    def test_save_deferred_extraction(self, monkeypatch, tmp_path):
        from core import extraction_daemon

        instance_id = os.environ.get("QUAID_INSTANCE", "pytest-runner")
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))

        extraction_daemon._save_deferred_extraction(
            session_id="sess-test",
            transcript_text="Hello world transcript",
            owner_id="quaid",
            label="daemon-rolling",
            reason="test_reason",
        )

        deferred_dir = tmp_path / "instances" / instance_id / "data" / "deferred-extractions"
        files = list(deferred_dir.glob("sess-test_*.json"))
        assert len(files) == 1

        data = json.loads(files[0].read_text(encoding="utf-8"))
        assert data["session_id"] == "sess-test"
        assert data["transcript_text"] == "Hello world transcript"
        assert data["owner_id"] == "quaid"
        assert data["reason"] == "test_reason"
        assert "saved_at" in data


# ---------------------------------------------------------------------------
# Janitor lock retry (core/lifecycle/janitor.py)
# ---------------------------------------------------------------------------

class TestJanitorLockRetry:
    def test_retry_acquires_on_second_attempt(self, monkeypatch):
        from core.lifecycle import janitor

        attempt_count = 0
        original_acquire = janitor._acquire_lock

        def mock_acquire():
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count == 1:
                return False  # First attempt fails
            return True  # Second succeeds

        # Track waits to verify threading.Event().wait is used
        wait_calls = []
        import threading
        original_event_wait = threading.Event.wait

        def mock_event_wait(self, timeout=None):
            wait_calls.append(timeout)

        monkeypatch.setattr(janitor, "_acquire_lock", mock_acquire)
        monkeypatch.setattr(janitor, "_release_lock", lambda: None)
        monkeypatch.setattr(janitor, "_refresh_runtime_state", lambda: None)
        monkeypatch.setattr(janitor, "set_token_budget", lambda _: None)
        monkeypatch.setattr(janitor, "reset_token_budget", lambda: None)
        monkeypatch.setattr(janitor, "_run_task_optimized_inner",
                            lambda *a, **kw: {"success": True})
        monkeypatch.setattr(threading.Event, "wait", mock_event_wait)

        result = janitor.run_task_optimized("all", dry_run=True)
        assert result.get("success") is True
        assert attempt_count == 2
        assert len(wait_calls) == 1
        assert wait_calls[0] == 5  # 5 second wait


# ---------------------------------------------------------------------------
# Project-docs stale-doc indexer
# ---------------------------------------------------------------------------

class TestProjectDocsStaleDocIndex:
    def test_index_one_stale_doc_no_docs(self, monkeypatch):
        """Returns False when no docs are registered."""
        from core import project_docs

        mock_registry = MagicMock()
        mock_registry.list_docs.return_value = []
        mock_rag = MagicMock()

        with patch("core.project_docs.DocsRAG", return_value=mock_rag, create=True), \
             patch("core.project_docs.DocsRegistry", return_value=mock_registry, create=True):
            # index_one_stale_registered_doc does lazy imports, mock them
            import datastore.docsdb.rag as rag_mod
            import datastore.docsdb.registry as reg_mod
            import core.docs.updater as docs_updater
            monkeypatch.setattr(rag_mod, "DocsRAG", lambda: mock_rag)
            monkeypatch.setattr(reg_mod, "DocsRegistry", lambda: mock_registry)
            monkeypatch.setattr(docs_updater, "index_project_logs", lambda project=None: 0)
            result = project_docs.index_one_stale_registered_doc()

        assert result is False

    def test_index_one_stale_doc_indexes_newest_first(self, monkeypatch, tmp_path):
        """Should index the most recently registered doc first."""
        from core import project_docs

        # Create two fake doc files
        old_doc = tmp_path / "old.md"
        old_doc.write_text("old content")
        new_doc = tmp_path / "new.md"
        new_doc.write_text("new content")

        mock_registry = MagicMock()
        mock_registry.list_docs.return_value = [
            {"file_path": str(old_doc), "registered_at": "2026-01-01"},
            {"file_path": str(new_doc), "registered_at": "2026-03-25"},
        ]
        mock_registry._resolve_path.side_effect = lambda path: path

        indexed_paths = []
        mock_rag = MagicMock()
        mock_rag.needs_reindex_many.return_value = {
            str(old_doc): True,
            str(new_doc): True,
        }
        mock_rag.index_document.side_effect = lambda p: indexed_paths.append(p) or 5

        import datastore.docsdb.rag as rag_mod
        import datastore.docsdb.registry as reg_mod
        import core.docs.updater as docs_updater
        monkeypatch.setattr(rag_mod, "DocsRAG", lambda: mock_rag)
        monkeypatch.setattr(reg_mod, "DocsRegistry", lambda: mock_registry)
        monkeypatch.setattr(docs_updater, "index_project_logs", lambda project=None: 0)

        result = project_docs.index_one_stale_registered_doc()

        assert result is True
        assert len(indexed_paths) == 1
        assert indexed_paths[0] == str(new_doc)  # newest first
