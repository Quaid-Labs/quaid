"""Integration tests for mcp_server.py — real DB, mocked embeddings.

These tests verify the full stack: MCP tool functions → api.py → memory_graph.py → SQLite.
Unit tests (test_mcp_server.py) mock the API layer; these tests use a real database.
"""

import hashlib
import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

# Ensure plugin root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Must set env BEFORE imports
os.environ["MEMORY_DB_PATH"] = ":memory:"
os.environ["QUAID_QUIET"] = "1"
os.environ["QUAID_OWNER"] = "test-owner"
os.environ["MOCK_EMBEDDINGS"] = "1"

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_get_embedding(text):
    """Return a deterministic fake embedding based on text hash."""
    h = hashlib.md5(text.encode()).digest()
    return [float(b) / 255.0 for b in h] * 8  # 128-dim


def _make_graph(tmp_path):
    """Create a MemoryGraph backed by a temp SQLite file."""
    from memory_graph import MemoryGraph
    db_file = tmp_path / "test.db"
    with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
        graph = MemoryGraph(db_path=db_file)
    return graph, db_file


def _get_mcp_module():
    """Import (or re-import) mcp_server module."""
    if "mcp_server" in sys.modules:
        del sys.modules["mcp_server"]
    import mcp_server
    return mcp_server


def _integration_patches(graph):
    """Context manager that patches memory_graph for integration testing.

    Sets the module-level _graph singleton directly so that ALL callers —
    including api.py (which binds get_graph at import time) — use our test graph.

    Patches:
      - memory_graph._graph → our test graph (real DB, temp file)
      - _lib_get_embedding → deterministic fake embeddings
      - _HAS_LLM_CLIENTS → False (skip HyDE, reranker, all LLM calls)
    """
    from contextlib import ExitStack
    import memory_graph as mg

    stack = ExitStack()
    # Set the singleton directly — get_graph() checks `if _graph is None`
    old_graph = mg._graph
    mg._graph = graph
    stack.callback(lambda: setattr(mg, "_graph", old_graph))
    stack.enter_context(patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding))
    stack.enter_context(patch("memory_graph._HAS_LLM_CLIENTS", False))
    return stack


# ---------------------------------------------------------------------------
# Integration Tests — MCP tools with real database
# ---------------------------------------------------------------------------

class TestMcpStoreRecallRoundTrip:
    """Verify store → recall round-trip through the full stack."""

    def test_store_creates_and_recall_finds(self, tmp_path):
        graph, _ = _make_graph(tmp_path)
        mod = _get_mcp_module()
        with _integration_patches(graph):
            # Store a fact
            result = mod.memory_store("The user's favorite language is Python")
            assert result["status"] == "created"
            node_id = result["id"]
            assert node_id  # non-empty UUID

            # Recall it
            memories = mod.memory_recall("favorite programming language", limit=5)
            found_ids = [m["id"] for m in memories]
            assert node_id in found_ids

    def test_store_multiple_and_recall_ranks(self, tmp_path):
        graph, _ = _make_graph(tmp_path)
        mod = _get_mcp_module()
        with _integration_patches(graph):
            mod.memory_store("User likes coffee in the morning")
            mod.memory_store("User prefers tea in the afternoon")
            mod.memory_store("User's dog is named Buddy")

            memories = mod.memory_recall("beverage preferences", limit=10)
            assert len(memories) >= 1  # At least one beverage fact found
            # All results have expected fields
            for m in memories:
                assert "id" in m
                assert "text" in m
                assert "similarity" in m


class TestMcpStoreAndGet:
    """Verify store → get_memory round-trip."""

    def test_store_and_get_by_id(self, tmp_path):
        graph, _ = _make_graph(tmp_path)
        mod = _get_mcp_module()
        with _integration_patches(graph):
            result = mod.memory_store("User was born in Seattle")
            node_id = result["id"]

            mem = mod.memory_get(node_id)
            assert mem is not None
            assert "error" not in mem
            assert mem["id"] == node_id
            assert "Seattle" in mem["name"]
            assert mem["owner_id"] == "test-owner"

    def test_get_nonexistent_returns_error(self, tmp_path):
        graph, _ = _make_graph(tmp_path)
        mod = _get_mcp_module()
        with _integration_patches(graph):
            result = mod.memory_get("00000000-0000-0000-0000-000000000000")
            assert "error" in result


class TestMcpStoreAndSearch:
    """Verify store → search round-trip."""

    def test_store_and_search_finds(self, tmp_path):
        graph, _ = _make_graph(tmp_path)
        mod = _get_mcp_module()
        with _integration_patches(graph):
            result = mod.memory_store("User enjoys hiking in the mountains")
            node_id = result["id"]

            results = mod.memory_search("hiking mountains", limit=5)
            found_ids = [r["id"] for r in results]
            assert node_id in found_ids


class TestMcpStoreAndForget:
    """Verify store → forget → verify gone."""

    def test_forget_by_id(self, tmp_path):
        graph, _ = _make_graph(tmp_path)
        mod = _get_mcp_module()
        with _integration_patches(graph):
            result = mod.memory_store("Temporary fact to delete")
            node_id = result["id"]

            # Forget by ID
            forget_result = mod.memory_forget(node_id=node_id)
            assert forget_result["deleted"] is True

            # Verify gone
            mem = mod.memory_get(node_id)
            assert "error" in mem

    def test_forget_by_query(self, tmp_path):
        graph, _ = _make_graph(tmp_path)
        mod = _get_mcp_module()
        with _integration_patches(graph):
            result = mod.memory_store("User has a parrot named Polly")
            node_id = result["id"]

            forget_result = mod.memory_forget(query="parrot named Polly")
            assert forget_result["deleted"] is True

            mem = mod.memory_get(node_id)
            assert "error" in mem

    def test_forget_nonexistent_returns_false(self, tmp_path):
        graph, _ = _make_graph(tmp_path)
        mod = _get_mcp_module()
        with _integration_patches(graph):
            result = mod.memory_forget(node_id="00000000-0000-0000-0000-000000000000")
            assert result["deleted"] is False


class TestMcpCreateEdge:
    """Verify edge creation between entities."""

    def test_create_edge_between_stored_entities(self, tmp_path):
        graph, _ = _make_graph(tmp_path)
        mod = _get_mcp_module()
        with _integration_patches(graph):
            # Store two entities
            mod.memory_store("Alice is a software engineer", category="entity")
            mod.memory_store("Bob works at Google", category="entity")

            # Create edge — entities are looked up by name (fuzzy)
            result = mod.memory_create_edge("Alice", "friend_of", "Bob")
            assert "edge_id" in result or "status" in result


class TestMcpStats:
    """Verify stats reflect actual database state."""

    def test_stats_reflect_stored_data(self, tmp_path):
        graph, _ = _make_graph(tmp_path)
        mod = _get_mcp_module()
        with _integration_patches(graph):
            # Empty DB stats
            stats = mod.memory_stats()
            initial_count = stats["total_nodes"]

            # Store some facts
            mod.memory_store("User likes jazz music")
            mod.memory_store("User plays guitar")

            # Stats should increase
            stats = mod.memory_stats()
            assert stats["total_nodes"] == initial_count + 2
            assert stats["edges"] >= 0


class TestMcpDedup:
    """Verify content hash deduplication works through the MCP layer."""

    def test_duplicate_text_detected(self, tmp_path):
        graph, _ = _make_graph(tmp_path)
        mod = _get_mcp_module()
        with _integration_patches(graph):
            text = "User's favorite book is Dune by Frank Herbert"
            r1 = mod.memory_store(text)
            assert r1["status"] == "created"

            r2 = mod.memory_store(text)
            assert r2["status"] in ("duplicate", "updated")
            assert r1["id"] == r2["id"]


class TestMcpDocsSearch:
    """Verify docs_search works with real DocsRAG (empty index)."""

    def test_docs_search_returns_list(self, tmp_path):
        graph, db_file = _make_graph(tmp_path)
        mod = _get_mcp_module()
        with _integration_patches(graph), \
             patch("docs_rag.DB_PATH", db_file):
            # No docs indexed, should return empty list
            results = mod.docs_search("anything")
            assert isinstance(results, list)
            assert len(results) == 0


class TestMcpInputValidation:
    """Verify input validation through the full stack."""

    def test_store_empty_text_raises(self, tmp_path):
        graph, _ = _make_graph(tmp_path)
        mod = _get_mcp_module()
        with _integration_patches(graph):
            with pytest.raises(ValueError, match="empty"):
                mod.memory_store("")

    def test_store_too_short_raises(self, tmp_path):
        graph, _ = _make_graph(tmp_path)
        mod = _get_mcp_module()
        with _integration_patches(graph):
            with pytest.raises(ValueError, match="at least 3 words"):
                mod.memory_store("two words")

    def test_store_unicode_works(self, tmp_path):
        graph, _ = _make_graph(tmp_path)
        mod = _get_mcp_module()
        with _integration_patches(graph):
            result = mod.memory_store("ユーザーはコーヒーが好きです coffee lover")
            assert result["status"] == "created"


class TestMcpOwnerIsolation:
    """Verify owner_id from QUAID_OWNER is used throughout."""

    def test_stored_fact_has_correct_owner(self, tmp_path):
        graph, _ = _make_graph(tmp_path)
        mod = _get_mcp_module()
        with _integration_patches(graph):
            result = mod.memory_store("Owner isolation test fact here")
            node = graph.get_node(result["id"])
            assert node.owner_id == "test-owner"


# ---------------------------------------------------------------------------
# Protocol Tests — MCP JSON-RPC over stdio subprocess
# ---------------------------------------------------------------------------

MCP_SERVER_PATH = os.path.join(os.path.dirname(__file__), "..", "mcp_server.py")


def _make_server_env(db_path):
    """Build environment for the MCP server subprocess."""
    env = os.environ.copy()
    env["MEMORY_DB_PATH"] = str(db_path)
    env["QUAID_OWNER"] = "proto-test"
    env["QUAID_QUIET"] = "1"
    env["MOCK_EMBEDDINGS"] = "1"
    # Empty API key makes LLM calls (HyDE, reranker) fail fast instead of timing out
    env["ANTHROPIC_API_KEY"] = ""
    # Ensure plugin dir is findable
    env["CLAWDBOT_WORKSPACE"] = os.path.join(os.path.dirname(__file__), "..", "..", "..")
    return env


def _readline_with_timeout(proc, timeout=15):
    """Read a line from proc.stdout with timeout. Returns parsed JSON or None."""
    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    if not ready:
        return None
    line = proc.stdout.readline()
    if not line:
        return None
    return json.loads(line)


def _send_message(proc, msg):
    """Send a JSON-RPC message (dict) to the subprocess."""
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()


class TestMcpProtocol:
    """Test the MCP server as a real subprocess over stdio."""

    def _start_server(self, db_path):
        """Start the MCP server subprocess."""
        env = _make_server_env(db_path)
        proc = subprocess.Popen(
            [sys.executable, MCP_SERVER_PATH],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            bufsize=1,  # line-buffered
        )
        return proc

    def _initialize(self, proc):
        """Perform MCP protocol handshake."""
        _send_message(proc, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0"},
                "protocolVersion": "2024-11-05",
            },
        })
        response = _readline_with_timeout(proc)
        assert response is not None, "Server did not respond to initialize"
        assert response.get("id") == 1
        assert "result" in response

        # Send initialized notification
        _send_message(proc, {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        })
        return response

    def test_server_starts_and_initializes(self, tmp_path):
        db_file = tmp_path / "proto.db"
        proc = self._start_server(db_file)
        try:
            response = self._initialize(proc)
            assert "serverInfo" in response["result"]
            assert response["result"]["serverInfo"]["name"] == "quaid"
        finally:
            proc.stdin.close()
            proc.wait(timeout=5)

    def test_tools_list_returns_9_tools(self, tmp_path):
        db_file = tmp_path / "proto.db"
        proc = self._start_server(db_file)
        try:
            self._initialize(proc)

            _send_message(proc, {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
            })
            response = _readline_with_timeout(proc)
            assert response is not None, "Server did not respond to tools/list"
            assert response.get("id") == 2

            tools = response["result"]["tools"]
            tool_names = {t["name"] for t in tools}
            assert len(tool_names) == 9
            assert "memory_extract" in tool_names
            assert "memory_store" in tool_names
            assert "memory_recall" in tool_names
            assert "memory_search" in tool_names
            assert "memory_get" in tool_names
            assert "memory_forget" in tool_names
            assert "memory_create_edge" in tool_names
            assert "memory_stats" in tool_names
            assert "docs_search" in tool_names
        finally:
            proc.stdin.close()
            proc.wait(timeout=5)

    def test_store_and_stats_over_protocol(self, tmp_path):
        db_file = tmp_path / "proto.db"
        proc = self._start_server(db_file)
        try:
            self._initialize(proc)

            # Call memory_store
            _send_message(proc, {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "memory_store",
                    "arguments": {
                        "text": "Protocol test fact about gardening hobby",
                    },
                },
            })
            store_resp = _readline_with_timeout(proc)
            assert store_resp is not None, "Server did not respond to memory_store"
            assert store_resp.get("id") == 3
            assert "result" in store_resp
            # Parse the content — FastMCP returns content blocks
            content = store_resp["result"]["content"]
            assert len(content) > 0
            store_data = json.loads(content[0]["text"])
            assert store_data["status"] == "created"
            assert "id" in store_data

            # Call memory_stats to verify
            _send_message(proc, {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "memory_stats",
                    "arguments": {},
                },
            })
            stats_resp = _readline_with_timeout(proc)
            assert stats_resp is not None, "Server did not respond to memory_stats"
            stats_data = json.loads(stats_resp["result"]["content"][0]["text"])
            assert stats_data["total_nodes"] >= 1

        finally:
            proc.stdin.close()
            proc.wait(timeout=5)

    def test_store_and_recall_over_protocol(self, tmp_path):
        db_file = tmp_path / "proto.db"
        proc = self._start_server(db_file)
        try:
            self._initialize(proc)

            # Store
            _send_message(proc, {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "memory_store",
                    "arguments": {
                        "text": "The user has three cats named Luna and Mochi and Tofu",
                    },
                },
            })
            store_resp = _readline_with_timeout(proc)
            assert store_resp is not None
            store_data = json.loads(store_resp["result"]["content"][0]["text"])
            node_id = store_data["id"]

            # Recall
            _send_message(proc, {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "memory_recall",
                    "arguments": {
                        "query": "cats named Luna Mochi Tofu",
                        "limit": 5,
                    },
                },
            })
            recall_resp = _readline_with_timeout(proc, timeout=30)
            assert recall_resp is not None, "Server did not respond to memory_recall"
            # FastMCP serializes list returns as JSON in a single content block.
            # Parse the content — handle both list and single dict (FastMCP may unwrap single-item lists).
            content_blocks = recall_resp["result"]["content"]
            recall_items = []
            for block in content_blocks:
                parsed = json.loads(block["text"])
                if isinstance(parsed, list):
                    recall_items.extend(parsed)
                elif isinstance(parsed, dict):
                    recall_items.append(parsed)
            assert len(recall_items) >= 1, f"Expected at least 1 recall result, got: {content_blocks}"
            found_ids = [m["id"] for m in recall_items]
            assert node_id in found_ids, f"Stored fact {node_id} not found in recall results: {recall_items}"

        finally:
            proc.stdin.close()
            proc.wait(timeout=10)

    def test_no_stdout_contamination(self, tmp_path):
        """Verify no stray output on stdout besides JSON-RPC messages."""
        db_file = tmp_path / "proto.db"
        proc = self._start_server(db_file)
        try:
            self._initialize(proc)

            # Do a store (triggers embedding, potential print paths)
            _send_message(proc, {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "memory_store",
                    "arguments": {
                        "text": "Stdout contamination test fact here please",
                    },
                },
            })
            response = _readline_with_timeout(proc)
            assert response is not None

            # Every line on stdout must be valid JSON-RPC
            assert "jsonrpc" in response
            assert response["jsonrpc"] == "2.0"

            # The response content should not contain stray print output
            content_text = response.get("result", {}).get("content", [{}])[0].get("text", "")
            assert "Embedding error" not in content_text

        finally:
            proc.stdin.close()
            proc.wait(timeout=5)

    def test_tool_schemas_have_descriptions(self, tmp_path):
        """Verify tool schemas include descriptions for LLM consumption."""
        db_file = tmp_path / "proto.db"
        proc = self._start_server(db_file)
        try:
            self._initialize(proc)

            _send_message(proc, {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/list",
            })
            response = _readline_with_timeout(proc)
            tools = response["result"]["tools"]

            for tool in tools:
                assert "description" in tool, f"Tool {tool['name']} missing description"
                assert len(tool["description"]) > 10, f"Tool {tool['name']} description too short"
                assert "inputSchema" in tool, f"Tool {tool['name']} missing input schema"

        finally:
            proc.stdin.close()
            proc.wait(timeout=5)
