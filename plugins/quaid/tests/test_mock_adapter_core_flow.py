"""Deterministic mock adapter/core flow tests (no live LLM dependency)."""

from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path
from typing import Dict, List
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import memory_graph
from lib.adapter import StandaloneAdapter


def _fake_get_embedding(text: str) -> List[float]:
    h = hashlib.md5(text.encode()).digest()
    return [float(b) / 255.0 for b in h] * 8  # 128-dim deterministic embedding


class MockAdapter(StandaloneAdapter):
    """Mock adapter that deterministically maps slash commands to extraction signals."""

    COMMAND_TO_SIGNAL: Dict[str, str] = {
        "new": "NewSignal",
        "reset": "ResetSignal",
        "restart": "ResetSignal",
        "compact": "CompactionSignal",
    }

    def command_signal(self, command_text: str) -> str | None:
        cmd = command_text.strip().lower()
        if cmd.startswith("/"):
            cmd = cmd[1:]
        cmd = cmd.split()[0] if cmd else ""
        return self.COMMAND_TO_SIGNAL.get(cmd)


class MockCore:
    """Mock core that appends transcript turns and stores deterministic extraction outputs."""

    def __init__(self, owner_id: str = "quaid") -> None:
        self.owner_id = owner_id
        self._logs: Dict[str, List[Dict[str, str]]] = {}

    def append_turn(self, session_id: str, user_text: str, assistant_text: str) -> None:
        self._logs.setdefault(session_id, []).extend(
            [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": assistant_text},
            ]
        )

    def extract_session(self, session_id: str, label: str) -> int:
        turns = self._logs.get(session_id, [])
        stored = 0

        for msg in turns:
            if msg.get("role") != "user":
                continue
            text = msg.get("content", "")

            sister = re.search(r"\bmy sister(?:'s name)? is ([a-z][a-z0-9_-]*)\b", text, re.I)
            if sister:
                name = sister.group(1).capitalize()
                fact = f"Quaid has a sister named {name}."
                memory_graph.store(
                    text=fact,
                    category="fact",
                    owner_id=self.owner_id,
                    source=f"{label.lower()}-extraction",
                    skip_dedup=True,
                )
                memory_graph.create_edge(
                    subject_name="Quaid",
                    relation="sibling_of",
                    object_name=name,
                    owner_id=self.owner_id,
                )
                stored += 1

            dog = re.search(r"\bmy dog(?:'s name)? is ([a-z][a-z0-9_-]*)\b", text, re.I)
            if dog:
                name = dog.group(1).capitalize()
                fact = f"Quaid has a dog named {name}."
                memory_graph.store(
                    text=fact,
                    category="fact",
                    owner_id=self.owner_id,
                    source=f"{label.lower()}-extraction",
                    skip_dedup=True,
                )
                stored += 1

        # Extraction consumes this session log.
        self._logs[session_id] = []
        return stored


def test_mock_adapter_signal_mapping(tmp_path: Path) -> None:
    adapter = MockAdapter(home=tmp_path)
    assert adapter.command_signal("/new") == "NewSignal"
    assert adapter.command_signal("/reset now") == "ResetSignal"
    assert adapter.command_signal("/restart") == "ResetSignal"
    assert adapter.command_signal("/compact") == "CompactionSignal"
    assert adapter.command_signal("/unknown") is None


def test_mock_core_persists_fact_and_graph_on_reset_signal(tmp_path: Path) -> None:
    db_file = tmp_path / "memory.db"
    os.environ["MEMORY_DB_PATH"] = str(db_file)

    with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
         patch("memory_graph.route_query", side_effect=lambda q: q):
        graph = memory_graph.MemoryGraph(db_path=db_file)
        with patch("memory_graph.get_graph", return_value=graph):
            adapter = MockAdapter(home=tmp_path)
            core = MockCore(owner_id="quaid")
            session_id = "session-mock-1"

            core.append_turn(session_id, "My sister is Shannon.", "Saved.")
            signal = adapter.command_signal("/reset")
            assert signal == "ResetSignal"
            stored = core.extract_session(session_id, signal)
            assert stored == 1

            # DB fact exists
            with graph._get_conn() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM nodes WHERE owner_id = ? AND name LIKE ?",
                    ("quaid", "%sister named Shannon%"),
                ).fetchone()
                assert row[0] >= 1

                # Graph relation exists
                edge_row = conn.execute(
                    "SELECT COUNT(*) FROM edges WHERE relation = ?",
                    ("sibling_of",),
                ).fetchone()
                assert edge_row[0] >= 1

            # Recall returns stored fact
            results = memory_graph.recall(
                query="sister Shannon",
                owner_id="quaid",
                limit=5,
                use_routing=False,
                min_similarity=0.0,
            )
            assert any("Shannon" in r.get("text", "") for r in results)

            # Session log was consumed/cleared
            assert core.extract_session(session_id, signal) == 0
