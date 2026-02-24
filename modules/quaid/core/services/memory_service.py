"""Core-owned memory service adapter.

This module provides the core-side composition point for memory operations.
It wraps datastore facade functions behind a core service contract.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.contracts.memory import MemoryServicePort
from datastore.facade import (
    store_memory,
    recall_memories,
    search_memories,
    create_edge,
    datastore_stats,
    forget_memory,
    get_memory_by_id,
    get_graph,
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
        return recall_memories(
            query=query,
            owner_id=owner_id,
            limit=limit,
            min_similarity=min_similarity,
            **kwargs,
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

    def graph(self):
        return get_graph()


_MEMORY_SERVICE: MemoryServicePort = DatastoreMemoryService()


def get_memory_service() -> MemoryServicePort:
    return _MEMORY_SERVICE

