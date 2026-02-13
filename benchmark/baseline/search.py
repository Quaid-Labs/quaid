#!/usr/bin/env python3
"""
Baseline hybrid search — faithful reimplementation of OpenClaw's
hybrid.ts + manager-search.ts for head-to-head comparison.

Algorithm: 70% vector + 30% BM25, AND-based FTS, no reranking,
no HyDE, no entity resolution, no graph traversal.

Key constants (from OpenClaw source):
- VECTOR_WEIGHT = 0.7
- TEXT_WEIGHT = 0.3
- CANDIDATE_MULTIPLIER = 4
- DEFAULT_LIMIT = 6
- MIN_SCORE = 0.35
"""
import math
import re
import sqlite3
import struct
import sys
from pathlib import Path
from typing import Dict, List, Optional

_has_sqlite_vec = False
try:
    import sqlite_vec
    _has_sqlite_vec = True
except ImportError:
    pass

_DIR = Path(__file__).resolve().parent
_RUNNER = _DIR.parent
if str(_RUNNER) not in sys.path:
    sys.path.insert(0, str(_RUNNER))
if str(_RUNNER.parent) not in sys.path:
    sys.path.insert(0, str(_RUNNER.parent))

import runner  # noqa: F401 — path bootstrap for quaid imports
from lib.embeddings import get_embedding, unpack_embedding
from lib.similarity import cosine_similarity

# ── Constants (from OpenClaw source) ───────────────────────────────────

VECTOR_WEIGHT = 0.7
TEXT_WEIGHT = 0.3
CANDIDATE_MULTIPLIER = 4
DEFAULT_LIMIT = 6
MIN_SCORE = 0.35


# ── FTS Query Construction (exact match to OpenClaw) ──────────────────

def build_fts_query(raw: str) -> Optional[str]:
    """
    Build FTS5 query from raw text.

    Exact match to OpenClaw's implementation:
    - Tokenize on word boundaries
    - Quote each token
    - Join with AND

    Args:
        raw: Raw query string

    Returns:
        FTS5 query string, or None if no tokens
    """
    tokens = re.findall(r'[A-Za-z0-9_]+', raw)
    if not tokens:
        return None
    return ' AND '.join(f'"{t}"' for t in tokens)


def bm25_to_score(rank: float) -> float:
    """
    Convert FTS5 BM25 rank to a 0-1 score.

    Exact match to OpenClaw's normalization:
    score = 1 / (1 + max(0, rank))

    Note: FTS5 rank is typically negative (lower = better match).
    OpenClaw takes max(0, rank), so negative ranks → score = 1.0.
    """
    if not math.isfinite(rank):
        normalized = 999.0
    else:
        normalized = max(0.0, rank)
    return 1.0 / (1.0 + normalized)


# ── Vector Search ─────────────────────────────────────────────────────

def _vector_search_brute(
    conn: sqlite3.Connection,
    query_embedding: List[float],
    limit: int,
) -> List[Dict]:
    """Brute-force vector search (fallback when sqlite-vec unavailable)."""
    rows = conn.execute(
        "SELECT id, text, path, start_line, end_line, embedding FROM chunks WHERE embedding IS NOT NULL"
    ).fetchall()

    scored = []
    for row in rows:
        chunk_id, text, path, start_line, end_line, emb_blob = row
        if not emb_blob:
            continue
        chunk_emb = unpack_embedding(emb_blob)
        sim = cosine_similarity(query_embedding, chunk_emb)
        if sim > 0:
            scored.append({
                "id": chunk_id,
                "text": text,
                "path": path,
                "start_line": start_line,
                "end_line": end_line,
                "vector_score": sim,
            })

    scored.sort(key=lambda x: x["vector_score"], reverse=True)
    return scored[:limit]


def _vector_search_vec(
    conn: sqlite3.Connection,
    query_embedding: List[float],
    limit: int,
) -> List[Dict]:
    """ANN vector search using sqlite-vec."""
    emb_blob = struct.pack(f'{len(query_embedding)}f', *query_embedding)
    try:
        # sqlite-vec returns (rowid, distance) where distance = cosine_distance
        rows = conn.execute(
            "SELECT rowid, distance FROM chunks_vec WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (emb_blob, limit),
        ).fetchall()
    except Exception:
        return _vector_search_brute(conn, query_embedding, limit)

    if not rows:
        return _vector_search_brute(conn, query_embedding, limit)

    results = []
    # Map rowids back to chunks (rowid = insertion order index)
    all_chunks = conn.execute(
        "SELECT id, text, path, start_line, end_line FROM chunks"
    ).fetchall()

    chunk_by_idx = {}
    for idx, row in enumerate(all_chunks):
        chunk_by_idx[idx] = row

    for rowid, distance in rows:
        chunk = chunk_by_idx.get(rowid)
        if not chunk:
            continue
        chunk_id, text, path, start_line, end_line = chunk
        score = 1.0 - distance  # cosine_similarity = 1 - cosine_distance
        results.append({
            "id": chunk_id,
            "text": text,
            "path": path,
            "start_line": start_line,
            "end_line": end_line,
            "vector_score": max(0.0, score),
        })

    return results


# ── FTS Search ────────────────────────────────────────────────────────

def _fts_search(
    conn: sqlite3.Connection,
    query: str,
    limit: int,
) -> List[Dict]:
    """FTS5 BM25 search with OpenClaw's AND-based query construction."""
    fts_query = build_fts_query(query)
    if not fts_query:
        return []

    try:
        rows = conn.execute(
            "SELECT chunks_fts.id, chunks_fts.text, rank "
            "FROM chunks_fts "
            "WHERE chunks_fts MATCH ? "
            "ORDER BY rank "
            "LIMIT ?",
            (fts_query, limit),
        ).fetchall()
    except Exception:
        return []

    results = []
    for chunk_id, text, rank in rows:
        # Look up full chunk info
        chunk_row = conn.execute(
            "SELECT path, start_line, end_line FROM chunks WHERE id = ?",
            (chunk_id,),
        ).fetchone()
        path = chunk_row[0] if chunk_row else ""
        start_line = chunk_row[1] if chunk_row else 0
        end_line = chunk_row[2] if chunk_row else 0

        results.append({
            "id": chunk_id,
            "text": text,
            "path": path,
            "start_line": start_line,
            "end_line": end_line,
            "text_score": bm25_to_score(rank),
        })

    return results


# ── Hybrid Search (main entry point) ─────────────────────────────────

def search(
    query: str,
    db_path: Path,
    limit: int = DEFAULT_LIMIT,
    min_score: float = MIN_SCORE,
) -> List[Dict]:
    """
    OpenClaw hybrid search: 70% vector + 30% BM25.

    Algorithm:
    1. Embed query
    2. Vector search: ANN (sqlite-vec) or brute-force fallback
    3. FTS5 BM25: AND-based query from tokenized input
    4. Merge by chunk ID: hybrid_score = 0.7 * vector + 0.3 * text
    5. Filter by min_score
    6. Return top `limit` results

    Args:
        query: Search query string
        db_path: Path to baseline SQLite database
        limit: Max results to return (default: 6)
        min_score: Minimum hybrid score threshold (default: 0.35)

    Returns:
        List of result dicts: {text, path, score, start_line, end_line, id,
                               vector_score, text_score}
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    candidate_limit = limit * CANDIDATE_MULTIPLIER

    # 1. Embed query
    query_embedding = get_embedding(query)

    # 2. Vector search
    vector_results = []
    if query_embedding:
        has_vec = False
        if _has_sqlite_vec:
            try:
                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
                conn.enable_load_extension(False)
                has_vec = True
            except Exception:
                pass

        if has_vec:
            vector_results = _vector_search_vec(conn, query_embedding, candidate_limit)
        else:
            vector_results = _vector_search_brute(conn, query_embedding, candidate_limit)

    # 3. FTS search
    fts_results = _fts_search(conn, query, candidate_limit)

    conn.close()

    # 4. Merge by chunk ID
    merged: Dict[str, Dict] = {}

    for r in vector_results:
        merged[r["id"]] = {
            "id": r["id"],
            "text": r["text"],
            "path": r["path"],
            "start_line": r["start_line"],
            "end_line": r["end_line"],
            "vector_score": r["vector_score"],
            "text_score": 0.0,
        }

    for r in fts_results:
        if r["id"] in merged:
            merged[r["id"]]["text_score"] = r["text_score"]
        else:
            merged[r["id"]] = {
                "id": r["id"],
                "text": r["text"],
                "path": r["path"],
                "start_line": r["start_line"],
                "end_line": r["end_line"],
                "vector_score": 0.0,
                "text_score": r["text_score"],
            }

    # 5. Compute hybrid scores and filter
    results = []
    for item in merged.values():
        hybrid_score = (VECTOR_WEIGHT * item["vector_score"] +
                        TEXT_WEIGHT * item["text_score"])
        if hybrid_score >= min_score:
            item["score"] = round(hybrid_score, 4)
            results.append(item)

    # 6. Sort by score descending, return top limit
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:limit]


def main():
    """CLI for testing baseline search."""
    import argparse

    parser = argparse.ArgumentParser(description="Baseline hybrid search")
    parser.add_argument("query", help="Search query")
    parser.add_argument("--db-path", required=True, help="Baseline SQLite database path")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--min-score", type=float, default=MIN_SCORE)
    args = parser.parse_args()

    results = search(args.query, Path(args.db_path), args.limit, args.min_score)
    print(f"\nResults for: {args.query!r} ({len(results)} found)")
    for i, r in enumerate(results):
        print(f"\n  [{i+1}] score={r['score']:.4f} (vec={r['vector_score']:.3f}, "
              f"txt={r['text_score']:.3f})")
        print(f"      path={r['path']}:{r['start_line']}-{r['end_line']}")
        preview = r['text'][:120].replace('\n', ' ')
        print(f"      {preview}...")


if __name__ == "__main__":
    main()
