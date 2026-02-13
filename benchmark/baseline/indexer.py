#!/usr/bin/env python3
"""
Chunk + embed + build SQLite index for baseline comparison.

Faithful reimplementation of OpenClaw's line-based chunking algorithm
(internal.ts:chunkMarkdown) and SQLite indexing (memory-schema.ts).

Chunking: line-based, 1600 max chars, 320 char overlap.
Index: SQLite with FTS5 + optional sqlite-vec for ANN.
"""
import hashlib
import math
import os
import re
import sqlite3
import struct
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
from lib.embeddings import get_embedding, pack_embedding


# ── Chunking (faithful to OpenClaw internal.ts:chunkMarkdown) ──────────

MAX_CHARS = 1600
OVERLAP_CHARS = 320


def chunk_markdown(text: str, path: str) -> List[Dict]:
    """
    Line-based chunking matching OpenClaw's algorithm.

    1. Split by newlines
    2. Accumulate lines until MAX_CHARS exceeded
    3. Flush chunk, carry back OVERLAP_CHARS from end of previous chunk
    4. Long lines split at MAX_CHARS boundaries
    5. SHA256 hash per chunk

    Args:
        text: Full markdown text
        path: Source file path (stored with chunk)

    Returns:
        List of chunk dicts: {id, path, start_line, end_line, hash, text}
    """
    lines = text.split("\n")
    chunks = []
    current_lines: List[str] = []
    current_chars = 0
    start_line = 1

    for line_num, line in enumerate(lines, 1):
        # Handle lines longer than MAX_CHARS — split them
        if len(line) > MAX_CHARS:
            # Flush current buffer first
            if current_lines:
                chunk_text = "\n".join(current_lines)
                chunks.append(_make_chunk(chunk_text, path, start_line, line_num - 1))
                current_lines = []
                current_chars = 0

            # Split long line into MAX_CHARS segments
            for i in range(0, len(line), MAX_CHARS):
                segment = line[i:i + MAX_CHARS]
                chunks.append(_make_chunk(segment, path, line_num, line_num))
            start_line = line_num + 1
            continue

        # Would this line exceed max?
        new_chars = current_chars + len(line) + (1 if current_lines else 0)  # +1 for newline
        if new_chars > MAX_CHARS and current_lines:
            # Flush current chunk
            chunk_text = "\n".join(current_lines)
            chunks.append(_make_chunk(chunk_text, path, start_line, line_num - 1))

            # Carry overlap from end of chunk
            overlap_text = chunk_text[-OVERLAP_CHARS:] if len(chunk_text) > OVERLAP_CHARS else chunk_text
            overlap_lines = overlap_text.split("\n")
            current_lines = overlap_lines
            current_chars = len(overlap_text)
            start_line = max(1, line_num - len(overlap_lines))

        current_lines.append(line)
        current_chars += len(line) + (1 if len(current_lines) > 1 else 0)

    # Flush remaining
    if current_lines:
        chunk_text = "\n".join(current_lines)
        if chunk_text.strip():  # Skip empty trailing chunks
            chunks.append(_make_chunk(chunk_text, path, start_line, len(lines)))

    return chunks


def _make_chunk(text: str, path: str, start_line: int, end_line: int) -> Dict:
    """Create a chunk dict with SHA256 hash ID."""
    content_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
    chunk_id = f"{Path(path).stem}:{start_line}-{end_line}:{content_hash}"
    return {
        "id": chunk_id,
        "path": path,
        "start_line": start_line,
        "end_line": end_line,
        "hash": content_hash,
        "text": text,
    }


# ── SQLite Index ───────────────────────────────────────────────────────

def _detect_embedding_dim() -> int:
    """Detect embedding dimension from a test embedding."""
    test_emb = get_embedding("test")
    if test_emb:
        return len(test_emb)
    return 128  # mock embedding dimension


def _load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Load sqlite-vec extension into connection. Returns True if loaded."""
    if not _has_sqlite_vec:
        return False
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception:
        return False


def create_index(db_path: Path, chunks: List[Dict], show_progress: bool = True) -> Dict:
    """
    Build SQLite index from chunks: embed, store, create FTS5 + vec tables.

    Args:
        db_path: Path for the SQLite database
        chunks: List of chunk dicts from chunk_markdown()
        show_progress: Print progress dots

    Returns:
        Dict with: total_chunks, embedded, errors, db_path
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    dim = _detect_embedding_dim()

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # Create schema
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS chunks (
            id TEXT PRIMARY KEY,
            path TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            hash TEXT NOT NULL,
            model TEXT DEFAULT 'baseline',
            text TEXT NOT NULL,
            embedding BLOB
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            text, id UNINDEXED, tokenize='porter'
        );
    """)

    # Try sqlite-vec
    has_vec = _load_sqlite_vec(conn)
    if not has_vec and not os.environ.get("MOCK_EMBEDDINGS"):
        raise RuntimeError(
            "sqlite-vec not loaded — baseline comparison requires ANN search "
            "for credible results. Install: pip install sqlite-vec"
        )
    if has_vec:
        try:
            conn.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec
                USING vec0(embedding float[{dim}])
            """)
        except Exception:
            has_vec = False

    embedded = 0
    errors = 0

    for i, chunk in enumerate(chunks):
        if show_progress and (i + 1) % 10 == 0:
            print(f"  Indexing chunk {i + 1}/{len(chunks)}...", flush=True)

        # Embed
        embedding = get_embedding(chunk["text"])
        emb_blob = pack_embedding(embedding) if embedding else None

        # Insert into chunks table
        conn.execute(
            "INSERT OR REPLACE INTO chunks (id, path, start_line, end_line, hash, text, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chunk["id"], chunk["path"], chunk["start_line"], chunk["end_line"],
             chunk["hash"], chunk["text"], emb_blob),
        )

        # Insert into FTS
        conn.execute(
            "INSERT OR REPLACE INTO chunks_fts (text, id) VALUES (?, ?)",
            (chunk["text"], chunk["id"]),
        )

        # Insert into vec index
        if has_vec and embedding:
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO chunks_vec (rowid, embedding) VALUES (?, ?)",
                    (i, emb_blob),
                )
            except Exception:
                pass  # vec insert failures are non-fatal

        if embedding:
            embedded += 1
        else:
            errors += 1

    # Self-test: verify at least one vector query returns results
    if has_vec and embedded > 0:
        test_row = conn.execute(
            "SELECT embedding FROM chunks WHERE embedding IS NOT NULL LIMIT 1"
        ).fetchone()
        if test_row and test_row[0]:
            test_results = conn.execute(
                "SELECT rowid, distance FROM chunks_vec WHERE embedding MATCH ? LIMIT 1",
                (test_row[0],),
            ).fetchall()
            if not test_results:
                raise RuntimeError("sqlite-vec self-test FAILED: vector query returned no results")
            print(f"  sqlite-vec self-test: PASSED (1 result, distance={test_results[0][1]:.4f})")

    conn.commit()
    conn.close()

    return {
        "total_chunks": len(chunks),
        "embedded": embedded,
        "errors": errors,
        "has_vec": has_vec,
        "embedding_dim": dim,
        "db_path": str(db_path),
    }


def index_directory(source_dir: Path, db_path: Path, show_progress: bool = True) -> Dict:
    """
    Index all markdown files in a directory.

    Args:
        source_dir: Directory containing .md files (journals + MEMORY.md)
        db_path: Path for the SQLite database

    Returns:
        Dict with indexing stats
    """
    # Collect all markdown files: journals + parent MEMORY.md
    md_files = sorted(source_dir.glob("*.md"))

    # Also include MEMORY.md from parent directory if it exists
    memory_md = source_dir.parent / "MEMORY.md"
    if memory_md.exists() and memory_md not in md_files:
        md_files.insert(0, memory_md)

    all_chunks = []
    for md_file in md_files:
        text = md_file.read_text()
        if not text.strip():
            continue
        rel_path = str(md_file.relative_to(source_dir.parent))
        file_chunks = chunk_markdown(text, rel_path)
        all_chunks.extend(file_chunks)

    if show_progress:
        print(f"  Chunked {len(md_files)} files into {len(all_chunks)} chunks")

    result = create_index(db_path, all_chunks, show_progress)
    result["files_indexed"] = len(md_files)
    return result


def main():
    """CLI for indexing baseline journals."""
    import argparse

    parser = argparse.ArgumentParser(description="Index baseline journal files")
    parser.add_argument("--source-dir", required=True, help="Directory with .md files")
    parser.add_argument("--db-path", required=True, help="Output SQLite database path")
    args = parser.parse_args()

    result = index_directory(Path(args.source_dir), Path(args.db_path))
    print(f"\nIndexed {result['files_indexed']} files → {result['total_chunks']} chunks")
    print(f"Embedded: {result['embedded']}, Errors: {result['errors']}")
    print(f"sqlite-vec: {'yes' if result.get('has_vec') else 'no'}")
    print(f"Database: {result['db_path']}")


if __name__ == "__main__":
    main()
