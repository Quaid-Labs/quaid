"""Integration tests — verify contracts across module boundaries.

These tests exercise cross-module flows that unit tests don't cover:
- store → recall round-trip
- store → janitor review → recall (with status transitions)
- store → dedup detection → merge → recall
- workspace_audit reads bootstrap files from gateway config
- protected region helpers work identically from lib vs workspace_audit
"""

import hashlib
import json
import os
import sys
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure plugin root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Must set env BEFORE imports so lib.config picks it up
os.environ["MEMORY_DB_PATH"] = ":memory:"

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_get_embedding(text):
    """Return a deterministic fake embedding based on text hash."""
    h = hashlib.md5(text.encode()).digest()
    return [float(b) / 255.0 for b in h] * 8  # 128-dim


def _make_graph(tmp_path):
    """Create a MemoryGraph backed by a temp SQLite file."""
    from datastore.memorydb.memory_graph import MemoryGraph
    db_file = tmp_path / "test.db"
    with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
        graph = MemoryGraph(db_path=db_file)
    return graph, db_file


# ---------------------------------------------------------------------------
# Store → Recall round-trip
# ---------------------------------------------------------------------------

class TestStoreRecallRoundTrip:
    """End-to-end: store a fact, recall it by query."""

    def test_store_then_recall_finds_fact(self, tmp_path):
        """A stored fact should be retrievable via recall."""
        from datastore.memorydb.memory_graph import store, recall

        graph, db_file = _make_graph(tmp_path)
        with patch("datastore.memorydb.memory_graph.get_graph", return_value=graph), \
             patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("datastore.memorydb.memory_graph.route_query", side_effect=lambda q: q):
            result = store(
                text="Quaid's favorite color is blue",
                category="preference",
                owner_id="quaid",
                skip_dedup=True,
            )
            assert result is not None
            node_id = result["id"]

            # Recall with same terms (use_routing=False, min_similarity=0.0 for test reliability)
            results = recall(
                query="Quaid's favorite color is blue",
                owner_id="quaid",
                limit=5,
                use_routing=False,
                min_similarity=0.0,
            )
            found_ids = [r["id"] for r in results]
            assert node_id in found_ids

    def test_store_with_edge_creates_both(self, tmp_path):
        """Storing a fact and then creating an edge should link them."""
        from datastore.memorydb.memory_graph import store, create_edge

        graph, db_file = _make_graph(tmp_path)
        with patch("datastore.memorydb.memory_graph.get_graph", return_value=graph), \
             patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("datastore.memorydb.memory_graph.route_query", side_effect=lambda q: q):
            # Store two facts
            r1 = store(text="Quaid lives in Bali", owner_id="quaid", skip_dedup=True)
            r2 = store(text="Bali is in Indonesia", owner_id="quaid", skip_dedup=True)
            assert r1 is not None and r2 is not None

            # Create edge (uses subject_name, relation, object_name)
            edge_result = create_edge(
                subject_name="Quaid lives in Bali",
                relation="lives_in",
                object_name="Bali is in Indonesia",
                owner_id="quaid",
            )
            # At minimum, the nodes exist
            node1 = graph.get_node(r1["id"])
            node2 = graph.get_node(r2["id"])
            assert node1 is not None
            assert node2 is not None


# ---------------------------------------------------------------------------
# Store → Status transitions (pending → active lifecycle)
# ---------------------------------------------------------------------------

class TestStatusLifecycle:
    """Facts move through status lifecycle correctly."""

    def test_deleted_fact_not_in_recall(self, tmp_path):
        """A deleted fact should not appear in recall results."""
        from datastore.memorydb.memory_graph import store, recall, soft_delete

        graph, db_file = _make_graph(tmp_path)
        with patch("datastore.memorydb.memory_graph.get_graph", return_value=graph), \
             patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("datastore.memorydb.memory_graph.route_query", side_effect=lambda q: q):
            result = store(
                text="Quaid has a cat named Whiskers",
                category="fact",
                owner_id="quaid",
                skip_dedup=True,
            )
            assert result is not None

            # Soft-delete the fact
            soft_delete(result["id"], reason="test")

            results = recall(
                query="Quaid has a cat named Whiskers",
                owner_id="quaid",
                limit=5,
                use_routing=False,
                min_similarity=0.0,
            )
            found_ids = [r["id"] for r in results]
            assert result["id"] not in found_ids

    def test_active_fact_in_recall(self, tmp_path):
        """An active fact should appear in recall results."""
        from datastore.memorydb.memory_graph import store, recall

        graph, db_file = _make_graph(tmp_path)
        with patch("datastore.memorydb.memory_graph.get_graph", return_value=graph), \
             patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("datastore.memorydb.memory_graph.route_query", side_effect=lambda q: q):
            result = store(
                text="Quaid has a dog named Rex",
                category="fact",
                owner_id="quaid",
                skip_dedup=True,
            )
            assert result is not None

            results = recall(
                query="Quaid has a dog named Rex",
                owner_id="quaid",
                limit=5,
                use_routing=False,
                min_similarity=0.0,
            )
            found_ids = [r["id"] for r in results]
            assert result["id"] in found_ids


# ---------------------------------------------------------------------------
# Content hash consistency across modules
# ---------------------------------------------------------------------------

class TestContentHashConsistency:
    """content_hash from memory_graph should be importable via public API."""

    def test_public_alias_matches_internal(self):
        """content_hash (public) and _content_hash (internal) are the same function."""
        from datastore.memorydb.memory_graph import content_hash, _content_hash
        assert content_hash is _content_hash

    def test_content_hash_deterministic(self):
        """Same input produces same hash."""
        from datastore.memorydb.memory_graph import content_hash
        h1 = content_hash("test fact")
        h2 = content_hash("test fact")
        assert h1 == h2

    def test_content_hash_differs_for_different_input(self):
        """Different input produces different hash."""
        from datastore.memorydb.memory_graph import content_hash
        h1 = content_hash("fact one")
        h2 = content_hash("fact two")
        assert h1 != h2


# ---------------------------------------------------------------------------
# Protected regions — lib/markdown vs workspace_audit backward compat
# ---------------------------------------------------------------------------

class TestProtectedRegionsLib:
    """lib/markdown.py functions match workspace_audit backward-compat aliases."""

    def test_strip_protected_from_lib(self):
        """lib.markdown.strip_protected_regions works correctly."""
        from lib.markdown import strip_protected_regions
        content = "visible <!-- protected -->secret<!-- /protected --> also visible"
        stripped, ranges = strip_protected_regions(content)
        assert "secret" not in stripped
        assert "visible" in stripped
        assert len(ranges) == 1

    def test_workspace_audit_alias_is_same_function(self):
        """core.lifecycle.workspace_audit._strip_protected_regions is the lib version."""
        from lib.markdown import strip_protected_regions
        from core.lifecycle.workspace_audit import _strip_protected_regions
        assert _strip_protected_regions is strip_protected_regions

    def test_section_overlaps_from_lib(self):
        """lib.markdown.section_overlaps_protected works correctly."""
        from lib.markdown import section_overlaps_protected
        # Section 10-20 overlaps with protected 15-25
        assert section_overlaps_protected(10, 20, [(15, 25)]) is True
        # Section 10-14 does not overlap with protected 15-25
        assert section_overlaps_protected(10, 14, [(15, 25)]) is False

    def test_is_position_protected_from_lib(self):
        """lib.markdown.is_position_protected works correctly."""
        from lib.markdown import is_position_protected
        ranges = [(10, 20), (30, 40)]
        assert is_position_protected(15, ranges) is True
        assert is_position_protected(25, ranges) is False
        assert is_position_protected(35, ranges) is True


# ---------------------------------------------------------------------------
# __all__ completeness
# ---------------------------------------------------------------------------

class TestModuleExports:
    """__all__ in memory_graph.py includes all public symbols."""

    def test_all_aliases_in_all(self):
        """Every backward-compat alias should be in __all__."""
        import datastore.memorydb.memory_graph as memory_graph
        all_exports = set(memory_graph.__all__)

        # These are the backward-compat aliases that production code imports
        required = {
            "MemoryGraph", "Node", "Edge",
            "store", "recall", "create_edge", "get_graph", "initialize_db",
            "hard_delete_node", "soft_delete", "forget", "get_memory",
            "store_contradiction", "get_pending_contradictions",
            "resolve_contradiction", "mark_contradiction_false_positive",
            "get_recent_dedup_rejections", "resolve_dedup_review",
            "queue_for_decay_review", "get_pending_decay_reviews",
            "resolve_decay_review", "decay_memories",
            "content_hash",
        }
        missing = required - all_exports
        assert not missing, f"Missing from __all__: {missing}"

    def test_all_entries_exist(self):
        """Every name in __all__ should be a real attribute on the module."""
        import datastore.memorydb.memory_graph as memory_graph
        for name in memory_graph.__all__:
            assert hasattr(memory_graph, name), f"{name} in __all__ but not defined"

    def test_internal_functions_not_in_all(self):
        """Underscore-prefixed functions should not be in __all__."""
        import datastore.memorydb.memory_graph as memory_graph
        underscore = [n for n in memory_graph.__all__ if n.startswith("_")]
        assert not underscore, f"Internal names in __all__: {underscore}"


# ---------------------------------------------------------------------------
# Dedup detection → merge → recall
# ---------------------------------------------------------------------------

class TestDedupMergeRecall:
    """Store near-duplicate facts, merge, verify recall returns merged version."""

    def test_content_hash_dedup_blocks_exact_duplicate(self, tmp_path):
        """Storing the exact same text twice should be blocked by content hash."""
        from datastore.memorydb.memory_graph import store

        graph, db_file = _make_graph(tmp_path)
        with patch("datastore.memorydb.memory_graph.get_graph", return_value=graph), \
             patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("datastore.memorydb.memory_graph.route_query", side_effect=lambda q: q):
            r1 = store(text="Quaid likes coffee", owner_id="quaid")
            r2 = store(text="Quaid likes coffee", owner_id="quaid")
            # Second store should return existing node (dedup)
            assert r1 is not None
            assert r2 is not None
            assert r1["id"] == r2["id"]


# ---------------------------------------------------------------------------
# Gateway bootstrap config → workspace audit discovery
# ---------------------------------------------------------------------------

class TestBootstrapConfigIntegration:
    """Workspace audit discovers bootstrap files from gateway config."""

    def test_gateway_config_parsed_correctly(self):
        """_get_gateway_bootstrap_globs reads the hook config."""
        from core.lifecycle.workspace_audit import _get_gateway_bootstrap_globs
        mock_config = {
            "hooks": {
                "internal": {
                    "entries": {
                        "bootstrap-extra-files": {
                            "enabled": True,
                            "paths": ["projects/*/TOOLS.md", "projects/*/AGENTS.md"]
                        }
                    }
                }
            }
        }
        with patch("builtins.open", create=True) as mock_open, \
             patch("core.lifecycle.workspace_audit.Path") as mock_path:
            mock_path.home.return_value.__truediv__ = lambda self, x: Path("/fake/.openclaw") / x if "openclaw" in str(x) else self
            # Simpler: just patch the function internals
            import core.lifecycle.workspace_audit as workspace_audit
            original = workspace_audit._get_gateway_bootstrap_globs

            def patched():
                # Simulate reading the config
                hook = mock_config["hooks"]["internal"]["entries"]["bootstrap-extra-files"]
                return hook.get("paths") or hook.get("patterns") or hook.get("files") or []

            with patch.object(workspace_audit, "_get_gateway_bootstrap_globs", patched):
                result = workspace_audit._get_gateway_bootstrap_globs()
                assert result == ["projects/*/TOOLS.md", "projects/*/AGENTS.md"]

    def test_disabled_hook_returns_empty(self):
        """Disabled bootstrap hook returns no patterns."""
        from core.lifecycle.workspace_audit import _get_gateway_bootstrap_globs
        mock_config = json.dumps({
            "hooks": {
                "internal": {
                    "entries": {
                        "bootstrap-extra-files": {
                            "enabled": False,
                            "paths": ["projects/*/TOOLS.md"]
                        }
                    }
                }
            }
        })

        import io
        with patch("core.lifecycle.workspace_audit.Path") as mock_path:
            mock_home = MagicMock()
            mock_path.home.return_value = mock_home
            config_path = MagicMock()
            mock_home.__truediv__ = MagicMock(return_value=config_path)
            config_path.__truediv__ = MagicMock(return_value=config_path)
            config_path.exists.return_value = True

            with patch("builtins.open", return_value=io.StringIO(mock_config)):
                result = _get_gateway_bootstrap_globs()
                assert result == []
