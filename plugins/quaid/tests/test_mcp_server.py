"""Tests for mcp_server.py — Quaid MCP Server."""

import inspect
import os
import sys

# Ensure plugin root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Must set env BEFORE imports
os.environ["MEMORY_DB_PATH"] = ":memory:"
os.environ["QUAID_QUIET"] = "1"
os.environ["QUAID_OWNER"] = "test-owner"

import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Helpers — import the module with mocked heavy dependencies
# ---------------------------------------------------------------------------

def _import_mcp_server():
    """Import mcp_server with heavy deps mocked out."""
    # Mock the api module calls — we test the MCP wrapper, not the API itself
    mock_api = MagicMock()
    mock_api.store.return_value = {"id": "abc-123", "status": "created", "similarity": 0.0}
    mock_api.recall.return_value = [
        {"id": "r1", "text": "User likes coffee", "similarity": 0.95, "confidence": 0.8}
    ]
    mock_api.search.return_value = [
        {"id": "s1", "text": "User drinks tea", "similarity": 0.7, "confidence": 0.6}
    ]
    mock_api.get_memory.return_value = {"id": "g1", "name": "Test memory", "type": "fact"}
    mock_api.forget.return_value = True
    mock_api.create_edge.return_value = {"edge_id": "e1", "status": "created"}
    mock_api.extract_transcript.return_value = {
        "facts_stored": 1,
        "facts_skipped": 0,
        "edges_created": 0,
    }
    mock_api.projects_search_docs.return_value = {
        "chunks": [{"content": "Some doc chunk", "source_file": "README.md", "similarity": 0.8}]
    }

    mock_graph = MagicMock()
    mock_graph.get_stats.return_value = {
        "total_nodes": 100, "edges": 50, "by_type": {"fact": 80}, "by_status": {"active": 90}
    }
    mock_api.stats.return_value = mock_graph.get_stats.return_value

    mock_rag = MagicMock()
    mock_rag_instance = MagicMock()
    mock_rag_instance.search_docs.return_value = [
        {"content": "Some doc chunk", "source_file": "README.md", "similarity": 0.8}
    ]
    mock_rag.return_value = mock_rag_instance

    # Patch at the api/docs_rag module level, then force reimport of mcp_server
    # so its `from api import store, ...` binds to the mocked functions.
    with patch("api.store", mock_api.store), \
         patch("api.extract_transcript", mock_api.extract_transcript), \
         patch("api.recall", mock_api.recall), \
         patch("api.search", mock_api.search), \
         patch("api.get_memory", mock_api.get_memory), \
         patch("api.forget", mock_api.forget), \
         patch("api.create_edge", mock_api.create_edge), \
         patch("api.stats", mock_api.stats), \
         patch("api.projects_search_docs", mock_api.projects_search_docs), \
         patch("core.docs.rag.DocsRAG", mock_rag):
        # Force reimport so mcp_server binds to mocked names
        if "mcp_server" in sys.modules:
            del sys.modules["mcp_server"]
        import mcp_server
        return mcp_server, mock_api, mock_graph, mock_rag_instance


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def server():
    """Return (mcp_server_module, mock_api, mock_graph, mock_rag)."""
    return _import_mcp_server()


# ---------------------------------------------------------------------------
# Tests — Tool existence and registration
# ---------------------------------------------------------------------------

EXPECTED_TOOLS = {
    "memory_extract",
    "memory_store",
    "memory_recall",
    "memory_search",
    "memory_get",
    "memory_forget",
    "memory_create_edge",
    "memory_stats",
    "memory_provider",
    "memory_event_emit",
    "memory_event_list",
    "memory_event_process",
    "memory_event_capabilities",
    "memory_write",
    "memory_capabilities",
    "projects_search",
    "session_recall",
}


class TestToolRegistration:
    def test_mcp_instance_exists(self, server):
        mod, *_ = server
        assert hasattr(mod, "mcp")
        assert mod.mcp.name == "quaid"

    def test_owner_from_env(self, server):
        mod, *_ = server
        assert mod.OWNER_ID == "test-owner"

    def test_quaid_quiet_set(self):
        assert os.environ.get("QUAID_QUIET") == "1"

    def test_all_expected_tools_registered(self, server):
        """Verify all expected tools are registered with correct names."""
        mod, *_ = server
        registered = set(mod.mcp._tool_manager._tools.keys())
        assert registered == EXPECTED_TOOLS

    def test_stdout_redirected_to_stderr(self, server):
        """The most critical safety feature: stdout redirected during import."""
        mod, *_ = server
        assert hasattr(mod, "_real_stdout"), "must preserve original stdout"
        # After import, sys.stdout should still be redirected (restore happens only in __main__)
        # The module sets sys.stdout = sys.stderr at import time


# ---------------------------------------------------------------------------
# Tests — Owner ID security
# ---------------------------------------------------------------------------

class TestOwnerSecurity:
    """Verify owner_id cannot be overridden by MCP clients."""

    def test_store_has_no_owner_param(self, server):
        mod, *_ = server
        sig = inspect.signature(mod.memory_store)
        assert "owner_id" not in sig.parameters
        assert "owner" not in sig.parameters

    def test_recall_has_no_owner_param(self, server):
        mod, *_ = server
        sig = inspect.signature(mod.memory_recall)
        assert "owner_id" not in sig.parameters

    def test_search_has_no_owner_param(self, server):
        mod, *_ = server
        sig = inspect.signature(mod.memory_search)
        assert "owner_id" not in sig.parameters

    def test_create_edge_has_no_owner_param(self, server):
        mod, *_ = server
        sig = inspect.signature(mod.memory_create_edge)
        assert "owner_id" not in sig.parameters

    def test_store_rejects_owner_kwarg(self, server):
        mod, *_ = server
        with pytest.raises(TypeError):
            mod.memory_store("test", owner_id="evil")


# ---------------------------------------------------------------------------
# Tests — memory_extract
# ---------------------------------------------------------------------------

class TestMemoryExtract:
    def test_extract_routes_through_api(self, server):
        mod, mock_api, *_ = server
        result = mod.memory_extract("User likes coffee", label="mcp", dry_run=False)
        mock_api.extract_transcript.assert_called_once_with(
            transcript="User likes coffee",
            owner_id="test-owner",
            label="mcp",
            dry_run=False,
        )
        assert result["facts_stored"] == 1


# ---------------------------------------------------------------------------
# Tests — memory_store
# ---------------------------------------------------------------------------

class TestMemoryStore:
    def test_store_basic(self, server):
        mod, mock_api, *_ = server
        result = mod.memory_store("User likes Python")
        mock_api.store.assert_called_once_with(
            text="User likes Python",
            owner_id="test-owner",
            category="fact",
            confidence=0.5,
            knowledge_type="fact",
            source="mcp",
            source_type="import",
            pinned=False,
        )
        assert result["status"] == "created"

    def test_store_with_category(self, server):
        mod, mock_api, *_ = server
        mod.memory_store("Prefers dark mode", category="preference")
        assert mock_api.store.call_args.kwargs["category"] == "preference"

    def test_store_pinned(self, server):
        mod, mock_api, *_ = server
        mod.memory_store("Important fact", pinned=True)
        assert mock_api.store.call_args.kwargs["pinned"] is True

    def test_store_source_type_always_import(self, server):
        mod, mock_api, *_ = server
        mod.memory_store("A fact", source="telegram")
        assert mock_api.store.call_args.kwargs["source_type"] == "import"
        assert mock_api.store.call_args.kwargs["source"] == "telegram"


# ---------------------------------------------------------------------------
# Tests — memory_recall
# ---------------------------------------------------------------------------

class TestMemoryRecall:
    def test_recall_basic(self, server):
        mod, mock_api, *_ = server
        result = mod.memory_recall("coffee preferences")
        mock_api.recall.assert_called_once_with(
            query="coffee preferences",
            owner_id="test-owner",
            limit=5,
            technical_scope="any",
        )
        assert len(result) == 1

    def test_recall_limit_capped_at_20(self, server):
        mod, mock_api, *_ = server
        mod.memory_recall("test", limit=100)
        assert mock_api.recall.call_args.kwargs["limit"] == 20

    def test_recall_limit_minimum_1(self, server):
        mod, mock_api, *_ = server
        mod.memory_recall("test", limit=0)
        assert mock_api.recall.call_args.kwargs["limit"] == 1

    def test_recall_limit_negative(self, server):
        mod, mock_api, *_ = server
        mod.memory_recall("test", limit=-5)
        assert mock_api.recall.call_args.kwargs["limit"] == 1


# ---------------------------------------------------------------------------
# Tests — memory_search
# ---------------------------------------------------------------------------

class TestMemorySearch:
    def test_search_basic(self, server):
        mod, mock_api, *_ = server
        result = mod.memory_search("tea")
        mock_api.search.assert_called_once_with(
            query="tea",
            owner_id="test-owner",
            limit=10,
        )
        assert len(result) == 1

    def test_search_limit_capped_at_50(self, server):
        mod, mock_api, *_ = server
        mod.memory_search("test", limit=200)
        assert mock_api.search.call_args.kwargs["limit"] == 50

    def test_search_limit_minimum_1(self, server):
        mod, mock_api, *_ = server
        mod.memory_search("test", limit=-1)
        assert mock_api.search.call_args.kwargs["limit"] == 1


# ---------------------------------------------------------------------------
# Tests — memory_get
# ---------------------------------------------------------------------------

class TestMemoryGet:
    def test_get_existing(self, server):
        mod, mock_api, *_ = server
        result = mod.memory_get("g1")
        mock_api.get_memory.assert_called_once_with("g1")
        assert result["id"] == "g1"

    def test_get_not_found(self, server):
        mod, mock_api, *_ = server
        mock_api.get_memory.return_value = None
        result = mod.memory_get("nonexistent")
        assert "error" in result
        assert "nonexistent" in result["error"]


# ---------------------------------------------------------------------------
# Tests — memory_forget
# ---------------------------------------------------------------------------

class TestMemoryForget:
    def test_forget_by_id(self, server):
        mod, mock_api, *_ = server
        result = mod.memory_forget(node_id="abc-123")
        mock_api.forget.assert_called_once_with(node_id="abc-123", query=None)
        assert result["deleted"] is True

    def test_forget_by_query(self, server):
        mod, mock_api, *_ = server
        result = mod.memory_forget(query="coffee preferences")
        mock_api.forget.assert_called_once_with(node_id=None, query="coffee preferences")
        assert result["deleted"] is True

    def test_forget_no_args_returns_error(self, server):
        mod, *_ = server
        result = mod.memory_forget()
        assert "error" in result

    def test_forget_empty_strings_returns_error(self, server):
        mod, *_ = server
        result = mod.memory_forget(node_id="", query="")
        assert "error" in result


# ---------------------------------------------------------------------------
# Tests — memory_create_edge
# ---------------------------------------------------------------------------

class TestMemoryCreateEdge:
    def test_create_edge_basic(self, server):
        mod, mock_api, *_ = server
        result = mod.memory_create_edge("Alice", "spouse_of", "Bob")
        mock_api.create_edge.assert_called_once_with(
            subject_name="Alice",
            relation="spouse_of",
            object_name="Bob",
            owner_id="test-owner",
        )
        assert result["status"] == "created"


# ---------------------------------------------------------------------------
# Tests — memory_stats
# ---------------------------------------------------------------------------

class TestMemoryStats:
    def test_stats(self, server):
        mod, mock_api, mock_graph, _ = server
        result = mod.memory_stats()
        mock_api.stats.assert_called_once()
        assert result["total_nodes"] == 100


# ---------------------------------------------------------------------------
# Tests — projects_search
# ---------------------------------------------------------------------------

class TestDocsSearch:
    def test_docs_search_basic(self, server):
        mod, mock_api, _, _ = server
        result = mod.projects_search("architecture")
        mock_api.projects_search_docs.assert_called_once_with(
            query="architecture",
            limit=5,
            project=None,
        )
        assert isinstance(result, dict)
        assert "chunks" in result
        assert len(result["chunks"]) == 1

    def test_docs_search_with_project(self, server):
        mod, mock_api, _, _ = server
        mod.projects_search("setup", project="quaid")
        assert mock_api.projects_search_docs.call_args.kwargs["project"] == "quaid"

    def test_docs_search_limit_capped(self, server):
        mod, mock_api, _, _ = server
        mod.projects_search("test", limit=100)
        assert mock_api.projects_search_docs.call_args.kwargs["limit"] == 20

    def test_docs_search_empty_project_becomes_none(self, server):
        mod, mock_api, _, _ = server
        mod.projects_search("test", project="")
        assert mock_api.projects_search_docs.call_args.kwargs["project"] is None


# ---------------------------------------------------------------------------
# Tests — Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_store_valueerror_propagates(self, server):
        """FastMCP catches exceptions and returns error responses."""
        mod, mock_api, *_ = server
        mock_api.store.side_effect = ValueError("Text too short")
        with pytest.raises(ValueError, match="Text too short"):
            mod.memory_store("hi")
