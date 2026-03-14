"""Unit tests for datastore/memorydb/identity_map.py.

Covers upsert_identity_handle(), resolve_identity_handle(), and
list_identity_handles() — upsert semantics, channel normalization,
conversation-scoped resolution, and fallback to channel-global.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Fixture: isolated SQLite file per test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point MEMORY_DB_PATH at a fresh temp file for each test."""
    db_file = tmp_path / "identity_test.db"
    monkeypatch.setenv("MEMORY_DB_PATH", str(db_file))
    yield db_file


# ---------------------------------------------------------------------------
# upsert_identity_handle — creation
# ---------------------------------------------------------------------------


class TestUpsertIdentityHandleCreate:
    def test_returns_dict_with_expected_keys(self):
        from datastore.memorydb.identity_map import upsert_identity_handle
        result = upsert_identity_handle(
            owner_id="user-1",
            source_channel="telegram",
            handle="Alice",
            canonical_entity_id="entity-abc",
        )
        assert result["handle"] == "Alice"
        assert result["canonical_entity_id"] == "entity-abc"
        assert result["owner_id"] == "user-1"
        assert result["source_channel"] == "telegram"
        assert "id" in result

    def test_channel_normalized_to_lowercase(self):
        from datastore.memorydb.identity_map import upsert_identity_handle
        result = upsert_identity_handle(
            owner_id=None,
            source_channel="TELEGRAM",
            handle="Alice",
            canonical_entity_id="entity-abc",
        )
        assert result["source_channel"] == "telegram"

    def test_none_owner_stored_as_none(self):
        from datastore.memorydb.identity_map import upsert_identity_handle
        result = upsert_identity_handle(
            owner_id=None,
            source_channel="telegram",
            handle="Alice",
            canonical_entity_id="entity-abc",
        )
        assert result["owner_id"] is None

    def test_none_conversation_id_stored_as_none(self):
        from datastore.memorydb.identity_map import upsert_identity_handle
        result = upsert_identity_handle(
            owner_id="user-1",
            source_channel="telegram",
            handle="Alice",
            canonical_entity_id="entity-abc",
            conversation_id=None,
        )
        assert result["conversation_id"] is None

    def test_conversation_scoped_entry(self):
        from datastore.memorydb.identity_map import upsert_identity_handle
        result = upsert_identity_handle(
            owner_id="user-1",
            source_channel="telegram",
            handle="Alice",
            canonical_entity_id="entity-abc",
            conversation_id="conv-99",
        )
        assert result["conversation_id"] == "conv-99"

    def test_confidence_stored_correctly(self):
        from datastore.memorydb.identity_map import upsert_identity_handle
        result = upsert_identity_handle(
            owner_id="user-1",
            source_channel="telegram",
            handle="Alice",
            canonical_entity_id="entity-abc",
            confidence=0.75,
        )
        assert abs(result["confidence"] - 0.75) < 1e-6

    def test_notes_stored_correctly(self):
        from datastore.memorydb.identity_map import upsert_identity_handle
        result = upsert_identity_handle(
            owner_id="user-1",
            source_channel="telegram",
            handle="Alice",
            canonical_entity_id="entity-abc",
            notes="mentioned in first message",
        )
        assert result["notes"] == "mentioned in first message"

    def test_raises_on_empty_channel(self):
        from datastore.memorydb.identity_map import upsert_identity_handle
        with pytest.raises(ValueError, match="source_channel"):
            upsert_identity_handle(
                owner_id="user-1",
                source_channel="",
                handle="Alice",
                canonical_entity_id="entity-abc",
            )

    def test_raises_on_empty_handle(self):
        from datastore.memorydb.identity_map import upsert_identity_handle
        with pytest.raises(ValueError, match="handle"):
            upsert_identity_handle(
                owner_id="user-1",
                source_channel="telegram",
                handle="",
                canonical_entity_id="entity-abc",
            )

    def test_raises_on_empty_canonical_entity_id(self):
        from datastore.memorydb.identity_map import upsert_identity_handle
        with pytest.raises(ValueError, match="canonical_entity_id"):
            upsert_identity_handle(
                owner_id="user-1",
                source_channel="telegram",
                handle="Alice",
                canonical_entity_id="",
            )


# ---------------------------------------------------------------------------
# upsert_identity_handle — update semantics
# ---------------------------------------------------------------------------


class TestUpsertIdentityHandleUpdate:
    def test_update_returns_same_id(self):
        from datastore.memorydb.identity_map import upsert_identity_handle
        first = upsert_identity_handle(
            owner_id="user-1", source_channel="telegram",
            handle="Alice", canonical_entity_id="entity-abc",
        )
        second = upsert_identity_handle(
            owner_id="user-1", source_channel="telegram",
            handle="Alice", canonical_entity_id="entity-xyz",
        )
        assert first["id"] == second["id"]

    def test_update_changes_canonical_entity_id(self):
        from datastore.memorydb.identity_map import upsert_identity_handle, resolve_identity_handle
        upsert_identity_handle(
            owner_id="user-1", source_channel="telegram",
            handle="Alice", canonical_entity_id="entity-old",
        )
        upsert_identity_handle(
            owner_id="user-1", source_channel="telegram",
            handle="Alice", canonical_entity_id="entity-new",
        )
        resolved = resolve_identity_handle(
            owner_id="user-1", source_channel="telegram", handle="Alice",
        )
        assert resolved["canonical_entity_id"] == "entity-new"

    def test_different_owners_are_separate_rows(self):
        from datastore.memorydb.identity_map import upsert_identity_handle
        r1 = upsert_identity_handle(
            owner_id="user-1", source_channel="telegram",
            handle="Alice", canonical_entity_id="entity-a",
        )
        r2 = upsert_identity_handle(
            owner_id="user-2", source_channel="telegram",
            handle="Alice", canonical_entity_id="entity-b",
        )
        assert r1["id"] != r2["id"]

    def test_different_channels_are_separate_rows(self):
        from datastore.memorydb.identity_map import upsert_identity_handle
        r1 = upsert_identity_handle(
            owner_id="user-1", source_channel="telegram",
            handle="Alice", canonical_entity_id="entity-a",
        )
        r2 = upsert_identity_handle(
            owner_id="user-1", source_channel="discord",
            handle="Alice", canonical_entity_id="entity-b",
        )
        assert r1["id"] != r2["id"]


# ---------------------------------------------------------------------------
# resolve_identity_handle
# ---------------------------------------------------------------------------


class TestResolveIdentityHandle:
    def test_returns_none_when_not_found(self):
        from datastore.memorydb.identity_map import resolve_identity_handle
        result = resolve_identity_handle(
            owner_id="user-1", source_channel="telegram", handle="Unknown",
        )
        assert result is None

    def test_returns_entry_when_found(self):
        from datastore.memorydb.identity_map import upsert_identity_handle, resolve_identity_handle
        upsert_identity_handle(
            owner_id="user-1", source_channel="telegram",
            handle="Alice", canonical_entity_id="entity-abc",
        )
        result = resolve_identity_handle(
            owner_id="user-1", source_channel="telegram", handle="Alice",
        )
        assert result is not None
        assert result["canonical_entity_id"] == "entity-abc"

    def test_returns_none_for_empty_channel(self):
        from datastore.memorydb.identity_map import resolve_identity_handle
        assert resolve_identity_handle(
            owner_id="user-1", source_channel="", handle="Alice",
        ) is None

    def test_returns_none_for_empty_handle(self):
        from datastore.memorydb.identity_map import resolve_identity_handle
        assert resolve_identity_handle(
            owner_id="user-1", source_channel="telegram", handle="",
        ) is None

    def test_prefers_conversation_scoped_over_channel_global(self):
        from datastore.memorydb.identity_map import upsert_identity_handle, resolve_identity_handle
        upsert_identity_handle(
            owner_id="user-1", source_channel="telegram",
            handle="Alice", canonical_entity_id="entity-global",
            conversation_id=None,
        )
        upsert_identity_handle(
            owner_id="user-1", source_channel="telegram",
            handle="Alice", canonical_entity_id="entity-scoped",
            conversation_id="conv-42",
        )
        result = resolve_identity_handle(
            owner_id="user-1", source_channel="telegram",
            handle="Alice", conversation_id="conv-42",
        )
        assert result["canonical_entity_id"] == "entity-scoped"

    def test_falls_back_to_channel_global_when_conv_not_found(self):
        from datastore.memorydb.identity_map import upsert_identity_handle, resolve_identity_handle
        upsert_identity_handle(
            owner_id="user-1", source_channel="telegram",
            handle="Alice", canonical_entity_id="entity-global",
            conversation_id=None,
        )
        result = resolve_identity_handle(
            owner_id="user-1", source_channel="telegram",
            handle="Alice", conversation_id="conv-99",
        )
        assert result is not None
        assert result["canonical_entity_id"] == "entity-global"

    def test_wrong_owner_returns_none(self):
        from datastore.memorydb.identity_map import upsert_identity_handle, resolve_identity_handle
        upsert_identity_handle(
            owner_id="user-1", source_channel="telegram",
            handle="Alice", canonical_entity_id="entity-abc",
        )
        result = resolve_identity_handle(
            owner_id="user-2", source_channel="telegram", handle="Alice",
        )
        assert result is None


# ---------------------------------------------------------------------------
# list_identity_handles
# ---------------------------------------------------------------------------


class TestListIdentityHandles:
    def _seed(self, entries):
        from datastore.memorydb.identity_map import upsert_identity_handle
        for e in entries:
            upsert_identity_handle(**e)

    def test_returns_empty_when_no_entries(self):
        from datastore.memorydb.identity_map import list_identity_handles
        assert list_identity_handles() == []

    def test_returns_all_entries_with_no_filter(self):
        from datastore.memorydb.identity_map import list_identity_handles
        self._seed([
            dict(owner_id="user-1", source_channel="telegram", handle="A", canonical_entity_id="e-a"),
            dict(owner_id="user-1", source_channel="telegram", handle="B", canonical_entity_id="e-b"),
        ])
        results = list_identity_handles()
        assert len(results) == 2

    def test_filters_by_owner_id(self):
        from datastore.memorydb.identity_map import list_identity_handles
        self._seed([
            dict(owner_id="user-1", source_channel="telegram", handle="A", canonical_entity_id="e-a"),
            dict(owner_id="user-2", source_channel="telegram", handle="B", canonical_entity_id="e-b"),
        ])
        results = list_identity_handles(owner_id="user-1")
        assert len(results) == 1
        assert results[0]["owner_id"] == "user-1"

    def test_filters_by_channel(self):
        from datastore.memorydb.identity_map import list_identity_handles
        self._seed([
            dict(owner_id="user-1", source_channel="telegram", handle="A", canonical_entity_id="e-a"),
            dict(owner_id="user-1", source_channel="discord", handle="B", canonical_entity_id="e-b"),
        ])
        results = list_identity_handles(source_channel="telegram")
        assert len(results) == 1
        assert results[0]["source_channel"] == "telegram"

    def test_filters_by_canonical_entity_id(self):
        from datastore.memorydb.identity_map import list_identity_handles
        self._seed([
            dict(owner_id="user-1", source_channel="telegram", handle="A", canonical_entity_id="entity-X"),
            dict(owner_id="user-1", source_channel="telegram", handle="B", canonical_entity_id="entity-Y"),
        ])
        results = list_identity_handles(canonical_entity_id="entity-X")
        assert len(results) == 1
        assert results[0]["canonical_entity_id"] == "entity-X"

    def test_respects_limit(self):
        from datastore.memorydb.identity_map import list_identity_handles
        self._seed([
            dict(owner_id="user-1", source_channel="t", handle=f"handle-{i}", canonical_entity_id=f"e-{i}")
            for i in range(10)
        ])
        results = list_identity_handles(limit=3)
        assert len(results) == 3

    def test_limit_clamped_to_minimum_one(self):
        from datastore.memorydb.identity_map import list_identity_handles
        self._seed([
            dict(owner_id="user-1", source_channel="telegram", handle="A", canonical_entity_id="e-a"),
        ])
        results = list_identity_handles(limit=0)
        assert len(results) == 1
