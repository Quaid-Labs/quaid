from datastore.memorydb import memory_graph


class _FakeGraph:
    def __init__(self, rows):
        self._rows = rows

    def search_hybrid(self, _query, limit=10, owner_id=None):
        rows = list(self._rows)
        if owner_id:
            rows = [r for r in rows if getattr(r[0], "owner_id", None) == owner_id]
        return rows[:limit]


def _node(**kwargs):
    return memory_graph.Node(
        id=kwargs.get("id", "n1"),
        type=kwargs.get("type", "Fact"),
        name=kwargs.get("name", "test memory"),
        owner_id=kwargs.get("owner_id", "user:a"),
        confidence=kwargs.get("confidence", 0.8),
        created_at=kwargs.get("created_at", "2026-01-01T00:00:00"),
        attributes=kwargs.get("attributes", {}),
        speaker_entity_id=kwargs.get("speaker_entity_id"),
        conversation_id=kwargs.get("conversation_id"),
        visibility_scope=kwargs.get("visibility_scope", "source_shared"),
        sensitivity=kwargs.get("sensitivity", "normal"),
        provenance_confidence=kwargs.get("provenance_confidence", 0.5),
    )


def test_search_applies_identity_scope_filters(monkeypatch):
    rows = [
        (
            _node(
                id="ok",
                attributes={
                    "source_channel": "telegram",
                    "source_conversation_id": "chat-1",
                    "source_author_id": "<redacted>",
                    "subject_entity_id": "user:a",
                    "participant_entity_ids": ["user:a", "agent:bert"],
                },
            ),
            0.91,
        ),
        (
            _node(
                id="drop",
                attributes={
                    "source_channel": "discord",
                    "source_conversation_id": "chat-9",
                    "source_author_id": "Other",
                    "subject_entity_id": "user:b",
                    "participant_entity_ids": ["user:b"],
                },
            ),
            0.95,
        ),
    ]
    monkeypatch.setattr(memory_graph, "get_graph", lambda: _FakeGraph(rows))
    out = memory_graph.search(
        "test",
        owner_id="user:a",
        source_channel="telegram",
        source_conversation_id="chat-1",
        source_author_id="<redacted>",
        subject_entity_id="user:a",
        participant_entity_ids=["agent:bert"],
    )
    assert [r["id"] for r in out] == ["ok"]


def test_search_parses_participant_ids_from_json_string(monkeypatch):
    rows = [
        (
            _node(
                id="json-participants",
                attributes={
                    "source_channel": "telegram",
                    "participant_entity_ids": '["agent:bert","user:a"]',
                },
            ),
            0.88,
        ),
    ]
    monkeypatch.setattr(memory_graph, "get_graph", lambda: _FakeGraph(rows))
    out = memory_graph.search(
        "test",
        owner_id="user:a",
        source_channel="telegram",
        participant_entity_ids=["agent:bert"],
    )
    assert len(out) == 1
    assert out[0]["participant_entity_ids"] == ["agent:bert", "user:a"]
