"""Unit tests for core/subagent_registry.py.

Covers register(), mark_complete(), get_harvestable(), mark_harvested(),
is_registered_subagent(), and cleanup_old_registries().
"""

import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Fixture: redirect registry to tmp_path via QUAID_HOME
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path, monkeypatch):
    """Point QUAID_HOME at tmp_path so every test has an isolated registry."""
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    # Force re-resolution of _registry_dir on each call (it's not cached)
    yield tmp_path


def _registry_file(tmp_path, parent_id):
    return tmp_path / "instances" / "pytest-runner" / "data" / "subagent-registry" / f"{parent_id}.json"


def _read_raw(tmp_path, parent_id):
    return json.loads(_registry_file(tmp_path, parent_id).read_text())


def test_fail_hard_enabled_propagates_policy_runtime_errors(monkeypatch):
    import core.subagent_registry as registry

    monkeypatch.setitem(
        sys.modules,
        "lib.fail_policy",
        SimpleNamespace(is_fail_hard_enabled=lambda: (_ for _ in ()).throw(RuntimeError("policy bug"))),
    )

    with pytest.raises(RuntimeError, match="policy bug"):
        registry._fail_hard_enabled()


# ---------------------------------------------------------------------------
# register()
# ---------------------------------------------------------------------------


class TestRegister:
    def test_creates_registry_file(self, tmp_path):
        from core.subagent_registry import register
        register("parent-1", "child-A")
        assert _registry_file(tmp_path, "parent-1").exists()

    def test_child_appears_in_registry(self, tmp_path):
        from core.subagent_registry import register
        register("parent-1", "child-A")
        data = _read_raw(tmp_path, "parent-1")
        assert "child-A" in data["children"]

    def test_fields_set_correctly(self, tmp_path):
        from core.subagent_registry import register
        register("parent-1", "child-A", child_transcript_path="/tmp/sess.jsonl", child_type="retrieval")
        entry = _read_raw(tmp_path, "parent-1")["children"]["child-A"]
        assert entry["child_id"] == "child-A"
        assert entry["parent_session_id"] == "parent-1"
        assert entry["child_type"] == "retrieval"
        assert entry["transcript_path"] == "/tmp/sess.jsonl"
        assert entry["status"] == "running"
        assert entry["harvested"] is False
        assert entry["completed_at"] is None
        assert entry["harvested_at"] is None

    def test_metadata_applied_but_authoritative_fields_win(self, tmp_path):
        """Metadata is applied first, then authoritative fields overwrite conflicts."""
        from core.subagent_registry import register
        register(
            "parent-1", "child-A",
            metadata={"child_id": "should-be-overwritten", "extra": "kept"},
        )
        entry = _read_raw(tmp_path, "parent-1")["children"]["child-A"]
        assert entry["child_id"] == "child-A"   # authoritative field wins
        assert entry["extra"] == "kept"           # non-conflicting metadata preserved

    def test_multiple_children(self, tmp_path):
        from core.subagent_registry import register
        register("parent-1", "child-A")
        register("parent-1", "child-B")
        data = _read_raw(tmp_path, "parent-1")
        assert "child-A" in data["children"]
        assert "child-B" in data["children"]

    def test_multiple_parents_separate_files(self, tmp_path):
        from core.subagent_registry import register
        register("parent-1", "child-A")
        register("parent-2", "child-X")
        assert _registry_file(tmp_path, "parent-1").exists()
        assert _registry_file(tmp_path, "parent-2").exists()

    def test_registered_at_is_iso8601(self, tmp_path):
        from core.subagent_registry import register
        register("parent-1", "child-A")
        entry = _read_raw(tmp_path, "parent-1")["children"]["child-A"]
        assert "T" in entry["registered_at"]
        assert entry["registered_at"].endswith("Z")

    def test_registered_at_honors_quaid_now(self, tmp_path, monkeypatch):
        from core.subagent_registry import register

        monkeypatch.setenv("QUAID_NOW", "2026-02-03T04:05:06Z")
        register("parent-1", "child-A")

        entry = _read_raw(tmp_path, "parent-1")["children"]["child-A"]
        assert entry["registered_at"] == "2026-02-03T04:05:06Z"

    def test_re_register_overwrites_entry(self, tmp_path):
        from core.subagent_registry import register
        register("parent-1", "child-A", child_type="retrieval")
        register("parent-1", "child-A", child_type="tools")
        entry = _read_raw(tmp_path, "parent-1")["children"]["child-A"]
        assert entry["child_type"] == "tools"

    def test_default_child_type_is_unknown(self, tmp_path):
        from core.subagent_registry import register
        register("parent-1", "child-A")
        entry = _read_raw(tmp_path, "parent-1")["children"]["child-A"]
        assert entry["child_type"] == "unknown"


# ---------------------------------------------------------------------------
# mark_complete()
# ---------------------------------------------------------------------------


class TestMarkComplete:
    def test_status_set_to_complete(self, tmp_path):
        from core.subagent_registry import register, mark_complete
        register("parent-1", "child-A")
        mark_complete("parent-1", "child-A")
        entry = _read_raw(tmp_path, "parent-1")["children"]["child-A"]
        assert entry["status"] == "complete"

    def test_completed_at_populated(self, tmp_path):
        from core.subagent_registry import register, mark_complete
        register("parent-1", "child-A")
        mark_complete("parent-1", "child-A")
        entry = _read_raw(tmp_path, "parent-1")["children"]["child-A"]
        assert entry["completed_at"] is not None
        assert "T" in entry["completed_at"]

    def test_completed_at_honors_quaid_now(self, tmp_path, monkeypatch):
        from core.subagent_registry import register, mark_complete

        register("parent-1", "child-A")
        monkeypatch.setenv("QUAID_NOW", "2026-02-03T04:05:06Z")
        mark_complete("parent-1", "child-A")

        entry = _read_raw(tmp_path, "parent-1")["children"]["child-A"]
        assert entry["completed_at"] == "2026-02-03T04:05:06Z"

    def test_transcript_path_updated(self, tmp_path):
        from core.subagent_registry import register, mark_complete
        register("parent-1", "child-A")
        mark_complete("parent-1", "child-A", transcript_path="/sessions/child-A.jsonl")
        entry = _read_raw(tmp_path, "parent-1")["children"]["child-A"]
        assert entry["transcript_path"] == "/sessions/child-A.jsonl"

    def test_transcript_path_not_cleared_if_none(self, tmp_path):
        from core.subagent_registry import register, mark_complete
        register("parent-1", "child-A", child_transcript_path="/sessions/orig.jsonl")
        mark_complete("parent-1", "child-A", transcript_path=None)
        entry = _read_raw(tmp_path, "parent-1")["children"]["child-A"]
        assert entry["transcript_path"] == "/sessions/orig.jsonl"

    def test_late_registration_creates_entry(self, tmp_path):
        """mark_complete on unregistered child should create an entry inline."""
        from core.subagent_registry import mark_complete
        mark_complete("parent-1", "orphan-child", transcript_path="/sessions/orphan.jsonl")
        entry = _read_raw(tmp_path, "parent-1")["children"]["orphan-child"]
        assert entry["status"] == "complete"
        assert entry["transcript_path"] == "/sessions/orphan.jsonl"

    def test_other_children_unaffected(self, tmp_path):
        from core.subagent_registry import register, mark_complete
        register("parent-1", "child-A")
        register("parent-1", "child-B")
        mark_complete("parent-1", "child-A")
        entry_b = _read_raw(tmp_path, "parent-1")["children"]["child-B"]
        assert entry_b["status"] == "running"

    def test_write_failure_raises_when_fail_hard(self, tmp_path, monkeypatch):
        import core.subagent_registry as registry
        from core.subagent_registry import register, mark_complete

        register("parent-1", "child-A")
        monkeypatch.setattr(registry, "_fail_hard_enabled", lambda: True)

        original_write_text = registry.Path.write_text

        def fail_registry_tmp_write(self, *args, **kwargs):
            if str(self).endswith("parent-1.tmp"):
                raise OSError("disk full")
            return original_write_text(self, *args, **kwargs)

        monkeypatch.setattr(registry.Path, "write_text", fail_registry_tmp_write)

        with pytest.raises(RuntimeError, match="Failed to write subagent registry") as excinfo:
            mark_complete("parent-1", "child-A", transcript_path="/sessions/child-A.jsonl")

        assert isinstance(excinfo.value.__cause__, OSError)

    def test_write_failure_degrades_when_fail_hard_disabled(self, tmp_path, monkeypatch, caplog):
        import core.subagent_registry as registry
        from core.subagent_registry import register, mark_complete

        register("parent-1", "child-A")
        before = _registry_file(tmp_path, "parent-1").read_text(encoding="utf-8")
        monkeypatch.setattr(registry, "_fail_hard_enabled", lambda: False)

        original_write_text = registry.Path.write_text

        def fail_registry_tmp_write(self, *args, **kwargs):
            if str(self).endswith("parent-1.tmp"):
                raise OSError("disk full")
            return original_write_text(self, *args, **kwargs)

        monkeypatch.setattr(registry.Path, "write_text", fail_registry_tmp_write)

        with caplog.at_level("ERROR", logger="core.subagent_registry"):
            mark_complete("parent-1", "child-A", transcript_path="/sessions/child-A.jsonl")

        assert "failed to write" in caplog.text
        assert _registry_file(tmp_path, "parent-1").read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# get_harvestable()
# ---------------------------------------------------------------------------


class TestGetHarvestable:
    def test_empty_when_no_children(self, tmp_path):
        from core.subagent_registry import get_harvestable
        assert get_harvestable("parent-1") == []

    def test_running_child_not_included(self, tmp_path):
        from core.subagent_registry import register, get_harvestable
        register("parent-1", "child-A", child_transcript_path="/t.jsonl")
        assert get_harvestable("parent-1") == []

    def test_complete_child_with_transcript_included(self, tmp_path):
        from core.subagent_registry import register, mark_complete, get_harvestable
        register("parent-1", "child-A")
        mark_complete("parent-1", "child-A", transcript_path="/sessions/child-A.jsonl")
        results = get_harvestable("parent-1")
        assert len(results) == 1
        assert results[0]["child_id"] == "child-A"

    def test_complete_without_transcript_excluded(self, tmp_path):
        """Complete child with no transcript_path is not harvestable."""
        from core.subagent_registry import register, mark_complete, get_harvestable
        register("parent-1", "child-A")
        mark_complete("parent-1", "child-A", transcript_path=None)
        assert get_harvestable("parent-1") == []

    def test_already_harvested_excluded(self, tmp_path):
        from core.subagent_registry import register, mark_complete, mark_harvested, get_harvestable
        register("parent-1", "child-A")
        mark_complete("parent-1", "child-A", transcript_path="/t.jsonl")
        mark_harvested("parent-1", "child-A")
        assert get_harvestable("parent-1") == []

    def test_only_unharvested_returned(self, tmp_path):
        from core.subagent_registry import register, mark_complete, mark_harvested, get_harvestable
        register("parent-1", "child-A")
        register("parent-1", "child-B")
        mark_complete("parent-1", "child-A", transcript_path="/a.jsonl")
        mark_complete("parent-1", "child-B", transcript_path="/b.jsonl")
        mark_harvested("parent-1", "child-A")
        results = get_harvestable("parent-1")
        assert len(results) == 1
        assert results[0]["child_id"] == "child-B"


# ---------------------------------------------------------------------------
# mark_harvested()
# ---------------------------------------------------------------------------


class TestMarkHarvested:
    def test_harvested_flag_set(self, tmp_path):
        from core.subagent_registry import register, mark_complete, mark_harvested
        register("parent-1", "child-A")
        mark_complete("parent-1", "child-A", transcript_path="/t.jsonl")
        mark_harvested("parent-1", "child-A")
        entry = _read_raw(tmp_path, "parent-1")["children"]["child-A"]
        assert entry["harvested"] is True

    def test_harvested_at_populated(self, tmp_path):
        from core.subagent_registry import register, mark_complete, mark_harvested
        register("parent-1", "child-A")
        mark_complete("parent-1", "child-A", transcript_path="/t.jsonl")
        mark_harvested("parent-1", "child-A")
        entry = _read_raw(tmp_path, "parent-1")["children"]["child-A"]
        assert entry["harvested_at"] is not None

    def test_harvested_at_honors_quaid_now(self, tmp_path, monkeypatch):
        from core.subagent_registry import register, mark_complete, mark_harvested

        register("parent-1", "child-A")
        mark_complete("parent-1", "child-A", transcript_path="/t.jsonl")
        monkeypatch.setenv("QUAID_NOW", "2026-02-03T04:05:06Z")
        mark_harvested("parent-1", "child-A")

        entry = _read_raw(tmp_path, "parent-1")["children"]["child-A"]
        assert entry["harvested_at"] == "2026-02-03T04:05:06Z"

    def test_noop_for_unknown_child(self, tmp_path):
        """mark_harvested on unknown child should not raise."""
        from core.subagent_registry import mark_harvested
        mark_harvested("parent-1", "nonexistent-child")  # Should not raise


# ---------------------------------------------------------------------------
# is_registered_subagent()
# ---------------------------------------------------------------------------


class TestIsRegisteredSubagent:
    def test_returns_false_for_unknown_session(self, tmp_path):
        from core.subagent_registry import is_registered_subagent
        assert is_registered_subagent("unknown-session") is False

    def test_returns_true_for_registered_child(self, tmp_path):
        from core.subagent_registry import register, is_registered_subagent
        register("parent-1", "child-A")
        assert is_registered_subagent("child-A") is True

    def test_returns_false_for_parent_session(self, tmp_path):
        """Parent session is not a child; is_registered_subagent should return False."""
        from core.subagent_registry import register, is_registered_subagent
        register("parent-1", "child-A")
        assert is_registered_subagent("parent-1") is False

    def test_scans_multiple_registry_files(self, tmp_path):
        from core.subagent_registry import register, is_registered_subagent
        register("parent-1", "child-A")
        register("parent-2", "child-B")
        assert is_registered_subagent("child-A") is True
        assert is_registered_subagent("child-B") is True

    def test_survives_malformed_json_file(self, tmp_path, monkeypatch):
        """Malformed registry files are skipped without raising."""
        import core.subagent_registry as registry
        from core.subagent_registry import register, is_registered_subagent

        monkeypatch.setattr(registry, "_fail_hard_enabled", lambda: False)
        register("parent-1", "child-A")
        # Corrupt a registry file manually
        bad = tmp_path / "instances" / "pytest-runner" / "data" / "subagent-registry" / "bad-parent.json"
        bad.write_text("not json {{{", encoding="utf-8")
        assert is_registered_subagent("child-A") is True  # Still finds child-A

    def test_malformed_json_file_raises_when_fail_hard(self, tmp_path, monkeypatch):
        import core.subagent_registry as registry
        from core.subagent_registry import is_registered_subagent

        reg_dir = tmp_path / "instances" / "pytest-runner" / "data" / "subagent-registry"
        reg_dir.mkdir(parents=True, exist_ok=True)
        (reg_dir / "bad-parent.json").write_text("not json {{{", encoding="utf-8")
        monkeypatch.setattr(registry, "_fail_hard_enabled", lambda: True)

        with pytest.raises(json.JSONDecodeError):
            is_registered_subagent("child-A")

    def test_registry_scan_failure_raises_when_fail_hard(self, monkeypatch):
        import core.subagent_registry as registry

        class BadRegistryDir:
            def glob(self, _pattern):
                raise OSError("scan failed")

        monkeypatch.setattr(registry, "_registry_dir", lambda: BadRegistryDir())
        monkeypatch.setattr(registry, "_fail_hard_enabled", lambda: True)

        with pytest.raises(OSError, match="scan failed"):
            registry.is_registered_subagent("child-A")

    def test_registry_scan_failure_returns_false_when_fail_open(self, monkeypatch, caplog):
        import core.subagent_registry as registry

        class BadRegistryDir:
            def glob(self, _pattern):
                raise OSError("scan failed")

        monkeypatch.setattr(registry, "_registry_dir", lambda: BadRegistryDir())
        monkeypatch.setattr(registry, "_fail_hard_enabled", lambda: False)
        caplog.set_level("WARNING")

        assert registry.is_registered_subagent("child-A") is False
        assert "Failed scanning subagent registry directory" in caplog.text


# ---------------------------------------------------------------------------
# cleanup_old_registries()
# ---------------------------------------------------------------------------


class TestCleanupOldRegistries:
    def test_removes_old_file(self, tmp_path):
        from core.subagent_registry import register, cleanup_old_registries
        register("parent-old", "child-A")
        p = _registry_file(tmp_path, "parent-old")
        # Backdate mtime to 72 hours ago
        old_time = time.time() - (72 * 3600)
        os.utime(p, (old_time, old_time))
        removed = cleanup_old_registries(max_age_hours=48.0)
        assert removed == 1
        assert not p.exists()

    def test_preserves_recent_file(self, tmp_path):
        from core.subagent_registry import register, cleanup_old_registries
        register("parent-new", "child-A")
        removed = cleanup_old_registries(max_age_hours=48.0)
        assert removed == 0
        assert _registry_file(tmp_path, "parent-new").exists()

    def test_returns_count(self, tmp_path):
        from core.subagent_registry import register, cleanup_old_registries
        for i in range(3):
            register(f"parent-{i}", "child-A")
            p = _registry_file(tmp_path, f"parent-{i}")
            old_time = time.time() - (72 * 3600)
            os.utime(p, (old_time, old_time))
        removed = cleanup_old_registries(max_age_hours=48.0)
        assert removed == 3

    def test_cleanup_cutoff_honors_quaid_now(self, tmp_path, monkeypatch):
        from core.subagent_registry import register, cleanup_old_registries

        monkeypatch.setenv("QUAID_NOW", "2026-02-03T04:05:06Z")
        register("parent-old", "child-A")
        register("parent-new", "child-B")
        old_time = datetime(2026, 1, 31, tzinfo=timezone.utc).timestamp()
        new_time = datetime(2026, 2, 2, 12, tzinfo=timezone.utc).timestamp()
        os.utime(_registry_file(tmp_path, "parent-old"), (old_time, old_time))
        os.utime(_registry_file(tmp_path, "parent-new"), (new_time, new_time))

        removed = cleanup_old_registries(max_age_hours=48.0)

        assert removed == 1
        assert not _registry_file(tmp_path, "parent-old").exists()
        assert _registry_file(tmp_path, "parent-new").exists()

    def test_cleanup_rechecks_age_after_registry_lock(self, tmp_path, monkeypatch):
        import core.subagent_registry as registry

        registry.register("parent-old", "child-A")
        p = _registry_file(tmp_path, "parent-old")
        old_time = time.time() - (72 * 3600)
        os.utime(p, (old_time, old_time))
        real_lock = registry._lock_registry
        locked = []

        @contextmanager
        def refresh_during_lock(parent_session_id):
            with real_lock(parent_session_id):
                locked.append(parent_session_id)
                os.utime(p, None)
                yield

        monkeypatch.setattr(registry, "_lock_registry", refresh_during_lock)

        removed = registry.cleanup_old_registries(max_age_hours=48.0)

        assert locked == ["parent-old"]
        assert removed == 0
        assert p.exists()

    def test_zero_age_removes_all(self, tmp_path):
        """max_age_hours=0 removes everything."""
        from core.subagent_registry import register, cleanup_old_registries
        register("parent-1", "child-A")
        register("parent-2", "child-B")
        removed = cleanup_old_registries(max_age_hours=0.0)
        assert removed == 2

    def test_returns_zero_when_empty(self, tmp_path):
        from core.subagent_registry import cleanup_old_registries
        assert cleanup_old_registries() == 0

    def test_registry_cleanup_scan_failure_raises_when_fail_hard(self, monkeypatch):
        import core.subagent_registry as registry

        class BadRegistryDir:
            def glob(self, _pattern):
                raise OSError("cleanup scan failed")

        monkeypatch.setattr(registry, "_registry_dir", lambda: BadRegistryDir())
        monkeypatch.setattr(registry, "_fail_hard_enabled", lambda: True)

        with pytest.raises(OSError, match="cleanup scan failed"):
            registry.cleanup_old_registries()

    def test_registry_cleanup_scan_failure_returns_zero_when_fail_open(self, monkeypatch, caplog):
        import core.subagent_registry as registry

        class BadRegistryDir:
            def glob(self, _pattern):
                raise OSError("cleanup scan failed")

        monkeypatch.setattr(registry, "_registry_dir", lambda: BadRegistryDir())
        monkeypatch.setattr(registry, "_fail_hard_enabled", lambda: False)
        caplog.set_level("WARNING")

        assert registry.cleanup_old_registries() == 0
        assert "Failed listing subagent registry directory for cleanup" in caplog.text


# ---------------------------------------------------------------------------
# Atomic write: no leftover .tmp files
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_now_rejects_malformed_quaid_now(self, monkeypatch):
        import core.subagent_registry as registry

        monkeypatch.setenv("QUAID_NOW", "not-a-date")

        with pytest.raises(ValueError, match="Invalid QUAID_NOW"):
            registry._now_iso_z()

    def test_no_tmp_files_after_register(self, tmp_path):
        from core.subagent_registry import register
        register("parent-1", "child-A")
        reg_dir = tmp_path / "instances" / "pytest-runner" / "data" / "subagent-registry"
        tmp_files = list(reg_dir.glob("*.tmp"))
        assert tmp_files == []

    def test_registry_file_valid_json(self, tmp_path):
        from core.subagent_registry import register
        register("parent-1", "child-A")
        data = _read_raw(tmp_path, "parent-1")
        assert isinstance(data, dict)
        assert "children" in data

    def test_malformed_existing_file_falls_back_to_empty(self, tmp_path, monkeypatch):
        """If the registry file is corrupted, register() recovers gracefully."""
        import core.subagent_registry as registry
        from core.subagent_registry import register

        monkeypatch.setattr(registry, "_fail_hard_enabled", lambda: False)
        reg_dir = tmp_path / "instances" / "pytest-runner" / "data" / "subagent-registry"
        reg_dir.mkdir(parents=True, exist_ok=True)
        (reg_dir / "parent-1.json").write_text("}{invalid json", encoding="utf-8")
        register("parent-1", "child-A")
        data = _read_raw(tmp_path, "parent-1")
        assert "child-A" in data["children"]

    def test_malformed_existing_file_raises_fail_hard_without_writeback(self, tmp_path, monkeypatch):
        import core.subagent_registry as registry
        from core.subagent_registry import mark_complete

        monkeypatch.setattr(registry, "_fail_hard_enabled", lambda: True)
        reg_dir = tmp_path / "instances" / "pytest-runner" / "data" / "subagent-registry"
        reg_dir.mkdir(parents=True, exist_ok=True)
        registry_file = reg_dir / "parent-1.json"
        corrupt_json = "}{invalid json"
        registry_file.write_text(corrupt_json, encoding="utf-8")

        with pytest.raises(RuntimeError, match="Failed to read subagent registry") as excinfo:
            mark_complete("parent-1", "child-A", transcript_path="/logs/child-A.jsonl")

        assert isinstance(excinfo.value.__cause__, json.JSONDecodeError)
        assert registry_file.read_text(encoding="utf-8") == corrupt_json

    def test_duplicate_register_does_not_downgrade_completed_child(self, tmp_path):
        from core.subagent_registry import register, mark_complete

        register("parent-1", "child-A", child_transcript_path="/logs/child-A.jsonl", child_type="general")
        mark_complete("parent-1", "child-A", transcript_path="/logs/child-A.jsonl")

        register("parent-1", "child-A", child_type="general")

        entry = _read_raw(tmp_path, "parent-1")["children"]["child-A"]
        assert entry["status"] == "complete"
        assert entry["transcript_path"] == "/logs/child-A.jsonl"
        assert entry["completed_at"] is not None

    def test_re_register_harvested_child_starts_fresh_run(self, tmp_path):
        from core.subagent_registry import register, mark_complete, mark_harvested, get_harvestable

        register("parent-1", "child-A", child_transcript_path="/logs/old.jsonl", child_type="general")
        mark_complete("parent-1", "child-A", transcript_path="/logs/old.jsonl")
        mark_harvested("parent-1", "child-A")

        register("parent-1", "child-A", child_type="general")

        entry = _read_raw(tmp_path, "parent-1")["children"]["child-A"]
        assert entry["status"] == "running"
        assert entry["transcript_path"] == ""
        assert entry["harvested"] is False
        assert entry["completed_at"] is None
        assert entry["harvested_at"] is None
        assert get_harvestable("parent-1") == []

        mark_complete("parent-1", "child-A", transcript_path="/logs/new.jsonl")
        harvestable = get_harvestable("parent-1")
        assert len(harvestable) == 1
        assert harvestable[0]["child_id"] == "child-A"
        assert harvestable[0]["transcript_path"] == "/logs/new.jsonl"

    def test_completion_prunes_stale_placeholder_running_child(self, tmp_path):
        from core.subagent_registry import register, mark_complete, get_harvestable

        register("parent-1", "general-purpose", child_type="general-purpose")
        mark_complete("parent-1", "child-real", transcript_path="/logs/child-real.jsonl")

        children = _read_raw(tmp_path, "parent-1")["children"]
        assert "general-purpose" not in children
        assert children["child-real"]["status"] == "complete"
        assert get_harvestable("parent-1")[0]["child_id"] == "child-real"
