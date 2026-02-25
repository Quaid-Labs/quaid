"""Core-owned memory service adapter.

This module provides the core-side composition point for memory operations.
It wraps datastore facade functions behind a core service contract.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.contracts.memory import MemoryServicePort
from core.runtime.identity_runtime import (
    enforce_write_contract,
    enrich_identity_payload,
    filter_recall_results,
)
from datastore.facade import (
    store_memory,
    recall_memories,
    search_memories,
    create_edge,
    datastore_stats,
    forget_memory,
    get_memory_by_id,
)


class DatastoreMemoryService(MemoryServicePort):
    def store(
        self,
        text: str,
        owner_id: str,
        category: str = "fact",
        confidence: float = 0.5,
        verified: bool = False,
        pinned: bool = False,
        source: Optional[str] = None,
        knowledge_type: str = "fact",
        source_type: Optional[str] = None,
        is_technical: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        identity_payload = {
            "source_channel": kwargs.get("source_channel"),
            "source_conversation_id": kwargs.get("source_conversation_id"),
            "source_author_id": kwargs.get("source_author_id"),
            "speaker_entity_id": kwargs.get("speaker_entity_id"),
            "subject_entity_id": kwargs.get("subject_entity_id"),
            "participant_entity_ids": kwargs.get("participant_entity_ids"),
        }
        enforce_write_contract(identity_payload)
        resolved = enrich_identity_payload(identity_payload)
        for key, value in resolved.items():
            if key not in kwargs or kwargs.get(key) in (None, ""):
                kwargs[key] = value
        # Compatibility bridge: datastore currently uses actor_id.
        if kwargs.get("speaker_entity_id") and not kwargs.get("actor_id"):
            kwargs["actor_id"] = kwargs["speaker_entity_id"]

        return store_memory(
            text=text,
            owner_id=owner_id,
            category=category,
            confidence=confidence,
            verified=verified,
            pinned=pinned,
            source=source,
            knowledge_type=knowledge_type,
            source_type=source_type,
            is_technical=is_technical,
            **kwargs,
        )

    def recall(
        self,
        query: str,
        owner_id: str,
        limit: int = 5,
        min_similarity: Optional[float] = None,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        viewer_entity_id = kwargs.get("viewer_entity_id")
        results = recall_memories(
            query=query,
            owner_id=owner_id,
            limit=limit,
            min_similarity=min_similarity,
            **kwargs,
        )
        return filter_recall_results(
            viewer_entity_id=viewer_entity_id,
            results=results,
            context={
                "owner_id": owner_id,
                "source_channel": kwargs.get("source_channel"),
                "source_conversation_id": kwargs.get("source_conversation_id"),
                "source_author_id": kwargs.get("source_author_id"),
                "subject_entity_id": kwargs.get("subject_entity_id"),
                "participant_entity_ids": kwargs.get("participant_entity_ids"),
            },
        )

    def search(self, query: str, owner_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        return search_memories(query=query, owner_id=owner_id, limit=limit)

    def create_edge(
        self,
        subject_name: str,
        relation: str,
        object_name: str,
        owner_id: str,
        source_fact_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return create_edge(
            subject_name=subject_name,
            relation=relation,
            object_name=object_name,
            owner_id=owner_id,
            source_fact_id=source_fact_id,
        )

    def forget(self, node_id: Optional[str] = None, query: Optional[str] = None) -> bool:
        return forget_memory(node_id=node_id, query=query)

    def get_memory(self, node_id: str) -> Optional[Dict[str, Any]]:
        return get_memory_by_id(node_id)

    def stats(self) -> Dict[str, Any]:
        return datastore_stats()

_MEMORY_SERVICE: MemoryServicePort = DatastoreMemoryService()


def get_memory_service() -> MemoryServicePort:
    return _MEMORY_SERVICE
