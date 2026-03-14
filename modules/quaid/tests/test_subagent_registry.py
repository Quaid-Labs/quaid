"""Unit tests for core/subagent_registry.py.

Covers register(), mark_complete(), get_harvestable(), mark_harvested(),
is_registered_subagent(), and cleanup_old_registries().
"""

import json
import os
import sys
import time

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
    return tmp_path / "data" / "subagent-registry" / f"{parent_id}.json"


def _read_raw(tmp_path, parent_id):
    return json.loads(_registry_file(tmp_path, parent_id).read_text())


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

    def test_survives_malformed_json_file(self, tmp_path):
        """Malformed registry files are skipped without raising."""
        from core.subagent_registry import register, is_registered_subagent
        register("parent-1", "child-A")
        # Corrupt a registry file manually
        bad = tmp_path / "data" / "subagent-registry" / "bad-parent.json"
        bad.write_text("not json {{{", encoding="utf-8")
        assert is_registered_subagent("child-A") is True  # Still finds child-A


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


# ---------------------------------------------------------------------------
# Atomic write: no leftover .tmp files
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_no_tmp_files_after_register(self, tmp_path):
        from core.subagent_registry import register
        register("parent-1", "child-A")
        reg_dir = tmp_path / "data" / "subagent-registry"
        tmp_files = list(reg_dir.glob("*.tmp"))
        assert tmp_files == []

    def test_registry_file_valid_json(self, tmp_path):
        from core.subagent_registry import register
        register("parent-1", "child-A")
        data = _read_raw(tmp_path, "parent-1")
        assert isinstance(data, dict)
        assert "children" in data

    def test_malformed_existing_file_falls_back_to_empty(self, tmp_path):
        """If the registry file is corrupted, register() recovers gracefully."""
        from core.subagent_registry import register
        reg_dir = tmp_path / "data" / "subagent-registry"
        reg_dir.mkdir(parents=True, exist_ok=True)
        (reg_dir / "parent-1.json").write_text("}{invalid json", encoding="utf-8")
        register("parent-1", "child-A")
        data = _read_raw(tmp_path, "parent-1")
        assert "child-A" in data["children"]
