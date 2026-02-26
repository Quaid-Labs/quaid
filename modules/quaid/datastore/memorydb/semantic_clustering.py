#!/usr/bin/env python3
"""
Semantic Clustering for Memory Maintenance
Groups memories by domain to reduce O(n²) contradiction checking.
"""

import logging
from typing import Dict, List, Set
from datastore.memorydb.memory_graph import Node, MemoryGraph, get_graph
from lib.llm_clients import call_fast_reasoning
from lib.fail_policy import is_fail_hard_enabled

logger = logging.getLogger(__name__)

# Define semantic clusters
SEMANTIC_CLUSTERS = {
    "people": {
        "description": "People, family members, friends, colleagues, relationships",
        "keywords": ["person", "family", "friend", "colleague", "mother", "father", "wife", "husband", "son", "daughter", "brother", "sister", "relationship", "contact", "acquaintance"],
        "types": ["Person"]
    },
    "places": {
        "description": "Locations, addresses, buildings, geographic places", 
        "keywords": ["address", "location", "city", "country", "building", "house", "office", "restaurant", "hotel", "room", "place", "lives", "works", "located"],
        "types": ["Place"]
    },
    "preferences": {
        "description": "Likes, dislikes, preferences, tastes, opinions",
        "keywords": ["likes", "dislikes", "prefers", "loves", "hates", "enjoys", "favorite", "taste", "opinion", "preference", "interested"],
        "types": ["Preference"]
    },
    "technology": {
        "description": "Software, hardware, technical decisions, tools, systems",
        "keywords": ["software", "hardware", "technical", "system", "tool", "app", "program", "computer", "phone", "device", "technology", "AI", "model", "database"],
        "types": ["Concept"]  # Many tech concepts stored as Concept type
    },
    "events": {
        "description": "Events, activities, meetings, appointments, things that happened",
        "keywords": ["meeting", "appointment", "event", "activity", "happened", "occurred", "schedule", "calendar", "plan", "trip", "visit"],
        "types": ["Event"]
    },
    "uncategorized": {
        "description": "Facts that do not map cleanly to semantic buckets",
        "keywords": [],
        "types": []
    }
}

def call_ollama_clustering(prompt: str, max_tokens: int = 50) -> str:
    """Call fast-reasoning provider for clustering classification."""
    try:
        result, _ = call_fast_reasoning(
            prompt=prompt,
            max_tokens=max_tokens,
            timeout=15,
            system_prompt=(
                "Classify the memory into exactly one category token: "
                "people, places, preferences, technology, or events. "
                "Return only the category token."
            ),
            max_retries=0,
        )
        return (result or "").strip()
    except Exception as e:
        logger.warning("semantic clustering LLM call failed: %s", e)
        if is_fail_hard_enabled():
            raise RuntimeError(
                "Semantic clustering LLM call failed while fail-hard mode is enabled"
            ) from e
        return ""

def classify_node_semantic_cluster(node: Node) -> str:
    """Classify a node into a semantic cluster using heuristics + LLM."""
    text_lower = node.name.lower()
    
    # Fast heuristic classification based on type and keywords
    for cluster_name, cluster_info in SEMANTIC_CLUSTERS.items():
        # Check if node type matches cluster types
        if node.type in cluster_info["types"]:
            # Additional keyword check for Concept type (since it's broad)
            if node.type == "Concept":
                if any(keyword in text_lower for keyword in cluster_info["keywords"]):
                    return cluster_name
            else:
                return cluster_name
        
        # Check keywords for any type
        if any(keyword in text_lower for keyword in cluster_info["keywords"]):
            return cluster_name
    
    # If heuristics fail, use LLM for edge cases
    prompt = f"""Classify this memory into one of these categories:

Memory: "{node.name}"

Categories:
- people: People, family, relationships  
- places: Locations, addresses, geography
- preferences: Likes, dislikes, opinions, tastes
- technology: Software, hardware, technical topics
- events: Activities, meetings, things that happened

Return only the category name:"""

    response = call_ollama_clustering(prompt, max_tokens=20)
    
    # Parse response
    for cluster_name in SEMANTIC_CLUSTERS.keys():
        if cluster_name in response.lower():
            return cluster_name
    
    # Default fallback: keep unknowns separate to avoid inflating any real bucket.
    logger.warning("semantic clustering fallback used for node_id=%s", node.id)
    return "uncategorized"

def get_memory_clusters(graph: MemoryGraph) -> Dict[str, List[Node]]:
    """Group all memory nodes by semantic cluster."""
    clusters = {name: [] for name in SEMANTIC_CLUSTERS.keys()}
    
    # Get all nodes
    with graph._get_conn() as conn:
        rows = conn.execute("SELECT * FROM nodes WHERE type = 'Fact'").fetchall()
    
    for row in rows:
        node = graph._row_to_node(row)
        cluster = classify_node_semantic_cluster(node)
        clusters[cluster].append(node)
    
    return clusters

def get_contradiction_pairs_by_cluster(graph: MemoryGraph, new_nodes: List[Node], 
                                     all_nodes: List[Node]) -> Dict[str, List[tuple]]:
    """Get contradiction candidate pairs grouped by semantic cluster."""
    # Classify all nodes
    node_clusters = {}
    for node in all_nodes:
        if node.type == 'Fact':
            cluster = classify_node_semantic_cluster(node)
            node_clusters[node.id] = cluster
    
    # Group pairs by cluster
    cluster_pairs = {name: [] for name in SEMANTIC_CLUSTERS.keys()}
    
    for new_node in new_nodes:
        if new_node.type != 'Fact' or not new_node.embedding:
            continue
            
        new_cluster = classify_node_semantic_cluster(new_node)
        
        for existing_node in all_nodes:
            if (new_node.id == existing_node.id or 
                existing_node.type != 'Fact' or 
                not existing_node.embedding):
                continue
            
            existing_cluster = node_clusters.get(existing_node.id, "uncategorized")
            
            # Only check pairs within same cluster
            if new_cluster == existing_cluster:
                cluster_pairs[new_cluster].append((new_node, existing_node))
    
    return cluster_pairs

def print_clustering_stats(clusters: Dict[str, List[Node]]):
    """Print statistics about semantic clustering."""
    total_nodes = sum(len(nodes) for nodes in clusters.values())
    
    print("\n📊 Semantic Clustering Statistics:")
    print(f"  Total Fact nodes: {total_nodes}")
    
    for cluster_name, nodes in clusters.items():
        percentage = (len(nodes) / total_nodes * 100) if total_nodes > 0 else 0
        print(f"  {cluster_name:12}: {len(nodes):4d} nodes ({percentage:5.1f}%)")
    
    # Calculate contradiction check reduction
    total_pairs_without_clustering = total_nodes * (total_nodes - 1) // 2
    total_pairs_with_clustering = sum(
        len(nodes) * (len(nodes) - 1) // 2 for nodes in clusters.values()
    )
    
    if total_pairs_without_clustering > 0:
        reduction = (1 - total_pairs_with_clustering / total_pairs_without_clustering) * 100
        print(f"\n⚡ Contradiction Check Reduction: {reduction:.1f}%")
        print(f"  Without clustering: {total_pairs_without_clustering:,} pairs")
        print(f"  With clustering:    {total_pairs_with_clustering:,} pairs")

if __name__ == "__main__":
    # Test clustering on current database
    graph = get_graph()
    clusters = get_memory_clusters(graph)
    print_clustering_stats(clusters)
    
    # Test classification on a few examples
    print("\n🧪 Sample Classifications:")
    with graph._get_conn() as conn:
        sample_rows = conn.execute("SELECT * FROM nodes WHERE type = 'Fact' LIMIT 10").fetchall()
    
    for row in sample_rows:
        node = graph._row_to_node(row)
        cluster = classify_node_semantic_cluster(node)
        print(f"  {cluster:12}: {node.name[:60]}...")
