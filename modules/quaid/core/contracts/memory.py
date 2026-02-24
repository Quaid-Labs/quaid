"""Core memory service contracts."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol


class MemoryServicePort(Protocol):
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
    ) -> Dict[str, Any]: ...

    def recall(
        self,
        query: str,
        owner_id: str,
        limit: int = 5,
        min_similarity: Optional[float] = None,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]: ...

    def search(self, query: str, owner_id: str, limit: int = 10) -> List[Dict[str, Any]]: ...

    def create_edge(
        self,
        subject_name: str,
        relation: str,
        object_name: str,
        owner_id: str,
        source_fact_id: Optional[str] = None,
    ) -> Dict[str, Any]: ...

    def forget(self, node_id: Optional[str] = None, query: Optional[str] = None) -> bool: ...

    def get_memory(self, node_id: str) -> Optional[Dict[str, Any]]: ...

    def stats(self) -> Dict[str, Any]: ...

