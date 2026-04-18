#!/usr/bin/env python3
"""
RAG for Technical Documentation
Smart chunking, indexing, and semantic search of project documentation.
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Dict, Optional, Tuple

from lib.config import get_docs_db_path
from lib.database import get_connection as _lib_get_connection, has_vec as _lib_has_vec
from lib.embeddings import (
    get_embedding as _lib_get_embedding,
    get_embeddings as _lib_get_embeddings,
    pack_embedding as _lib_pack_embedding,
    unpack_embedding as _lib_unpack_embedding,
)
from lib.similarity import cosine_similarity as _lib_cosine_similarity
from lib.fail_policy import is_fail_hard_enabled
from lib.runtime_context import get_visible_quaid_home, get_workspace_dir

logger = logging.getLogger(__name__)

# Configuration — resolved from config system
def _default_db_path() -> Path:
    return get_docs_db_path()
def _workspace() -> Path:
    return get_workspace_dir()


def _resolve_project_root(raw: str) -> Path:
    p = Path(str(raw or "").strip())
    if p.is_absolute():
        return p
    if not str(raw or "").strip():
        return _workspace()
    if str(raw).startswith("projects/") or str(raw) == "projects":
        return get_visible_quaid_home() / raw
    return _workspace() / raw


def _canonical_source_path(raw_path: str) -> str:
    """Resolve a source path into a stable absolute path string.

    This collapses symlink aliases (for example benchmark run roots exposing the
    same project docs through both ``projects/...`` and
    ``instances/benchrunner/projects/...``). Keeping a canonical source path
    avoids duplicate doc_chunks rows for the same physical file.
    """
    value = str(raw_path or "").strip()
    if not value:
        return ""
    path = Path(value).expanduser()
    try:
        return str(path.resolve(strict=False))
    except TypeError:
        # Python versions without the strict kwarg.
        try:
            return str(path.resolve())
        except Exception:
            pass
    except Exception:
        pass
    try:
        return str(Path(os.path.realpath(str(path))))
    except Exception:
        return str(path)


def _linked_projects_for_current_instance() -> tuple[List[str], bool]:
    """Return projects linked to the active instance.

    Returns (projects, resolved) where resolved=False means instance context
    could not be determined and caller should not enforce scope filtering.
    """
    try:
        from lib.instance import instance_id as _instance_id
        from lib.project_registry import list_all as _list_projects

        current_instance = _instance_id()
        linked: List[str] = []
        for project_name, entry in (_list_projects() or {}).items():
            instances = entry.get("instances") or []
            if current_instance in instances:
                linked.append(str(project_name))
        return linked, True
    except Exception:
        return [], False


def _rag_config():
    """Get RAG config section (lazy import to avoid circular deps)."""
    try:
        from config import get_config
        return get_config().rag
    except Exception:
        return None


def _docs_recall_telemetry_enabled() -> bool:
    """Enable verbose docs recall telemetry via opt-in env flag."""
    raw = str(os.getenv("QUAID_RECALL_TELEMETRY", "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on", "debug"}


def _chunk_max_tokens() -> int:
    cfg = _rag_config()
    return cfg.chunk_max_tokens if cfg else 800


def _chunk_overlap_tokens() -> int:
    cfg = _rag_config()
    return cfg.chunk_overlap_tokens if cfg else 100


def _escape_like(value: str) -> str:
    """Escape SQL LIKE wildcards for literal matching."""
    s = str(value or "")
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _path_suffix_candidates(path_value: str, workspace: Optional[Path] = None) -> List[str]:
    """Return stable relative suffixes for relocated project/docs matching.

    Doc chunk rows can retain absolute source paths from an earlier instance root
    (for example copied benchmark/eval-only runs). Project-scoped filtering should
    therefore match both the current absolute prefix and stable relative suffixes
    like ``projects/recipe-app`` or ``projects/recipe-app/tests/auth.test.js``.
    """
    raw = str(path_value or "").strip()
    if not raw:
        return []

    out: List[str] = []

    def _add(candidate: str) -> None:
        c = str(candidate or "").replace("\\", "/").strip().strip("/")
        if c and c not in out:
            out.append(c)

    p = Path(raw)
    if workspace is not None:
        try:
            _add(str(p.resolve().relative_to(workspace.resolve())))
        except Exception:
            pass

    normalized = raw.replace("\\", "/")
    for marker in ("/projects/",):
        idx = normalized.find(marker)
        if idx >= 0:
            _add(normalized[idx + 1 :])

    if not p.is_absolute():
        _add(raw)

    return out


def _docs_query_terms(query: str) -> List[str]:
    """Extract lightweight lexical terms for doc reranking."""
    raw_terms = re.findall(r"[a-zA-Z0-9_./-]+", str(query or "").lower())
    stop = {
        "the", "a", "an", "and", "or", "to", "for", "of", "in", "on", "at",
        "is", "are", "was", "were", "be", "do", "does", "did", "how", "what",
        "which", "who", "when", "where", "why", "current", "currently",
        "recipe", "app",
    }
    out: List[str] = []
    for term in raw_terms:
        t = term.strip().strip("/.")
        if len(t) < 3 or t in stop:
            continue
        if t not in out:
            out.append(t)
    return out


def _docs_source_penalty(query_terms: List[str], source_file: str) -> float:
    """Return a small path-based penalty for low-signal doc sources.

    Keep the penalty query-aware:
    - fixture/sample/seed/example data should lose to real implementation/docs
      unless the query explicitly asks about seeds/examples/etc.
    - archived logs should lose to implementation/docs unless the query is
      explicitly historical.
    """
    path_lower = str(source_file or "").lower()
    path_parts = {part for part in Path(path_lower).parts if part}
    file_name = Path(path_lower).name
    penalty = 0.0

    asks_for_fixture = any(
        term in {"seed", "seeds", "fixture", "fixtures", "sample", "samples", "example", "examples", "mock", "mocks"}
        for term in query_terms
    )
    asks_for_history = any(
        term in {"history", "historical", "archive", "archived", "changelog", "timeline", "past"}
        for term in query_terms
    )

    fixture_signals = (
        "seed" in path_parts
        or "seeds" in path_parts
        or "fixtures" in path_parts
        or "fixture" in path_parts
        or "samples" in path_parts
        or "sample" in path_parts
        or "examples" in path_parts
        or "example" in path_parts
        or "mocks" in path_parts
        or "mock" in path_parts
        or file_name.startswith(("sample-", "seed-", "fixture-", "mock-", "example-"))
    )
    if fixture_signals and not asks_for_fixture:
        penalty += 0.14

    is_history_log = "/log/" in path_lower or file_name.endswith(".log")
    if is_history_log and not asks_for_history:
        penalty += 0.10

    return penalty


def _docs_rank_score(query_terms: List[str], source_file: str, section_header: Optional[str], content: str, similarity: float) -> float:
    """Blend semantic similarity with lightweight lexical/path features.

    This keeps semantic search as the base signal, but prefers implementation
    files/sections when the query contains concrete code or test vocabulary.
    """
    path_lower = str(source_file or "").lower()
    file_name = Path(source_file or "").name.lower()
    header_lower = str(section_header or "").lower()
    content_lower = str(content or "").lower()

    score = float(similarity)
    path_hits = 0
    header_hits = 0
    content_hits = 0
    for term in query_terms:
        if term in path_lower:
            path_hits += 1
        if term in header_lower:
            header_hits += 1
        if term in content_lower:
            content_hits += 1

    score += min(path_hits, 3) * 0.10
    score += min(header_hits, 3) * 0.06
    score += min(content_hits, 4) * 0.025

    implementation_terms = {
        "test", "tests", "testing", "auth", "error", "errors", "validation",
        "middleware", "schema", "graphql", "resolver", "resolvers", "deploy",
        "deployment", "docker", "container", "database", "sql", "migration",
        "endpoint", "endpoints", "api", "layout", "frontend", "backend",
        "css", "rate", "limiting", "limit",
    }
    wants_impl = any(term in implementation_terms for term in query_terms)
    if wants_impl and file_name in {"project.md", "readme.md", "tools.md", "agents.md"}:
        score -= 0.12
    if wants_impl and header_lower in {"# project: recipe app", "# recipe app", "## overview", "# overview"}:
        score -= 0.06
    if wants_impl:
        score -= _docs_source_penalty(query_terms, source_file)

    return max(0.0, round(score, 4))


class DocumentChunk:
    """A chunk of a document with metadata."""
    def __init__(self, content: str, source_file: str, chunk_index: int, 
                 section_header: Optional[str] = None):
        self.content = content
        self.source_file = source_file
        self.chunk_index = chunk_index
        self.section_header = section_header
        self.id = f"{source_file}:{chunk_index}"
        
    def __repr__(self):
        return f"DocumentChunk(id='{self.id}', content_len={len(self.content)})"


class DocsRAG:
    """RAG system for technical documentation."""
    
    def __init__(self, db_path: Path = None):
        self.db_path = Path(db_path).expanduser() if db_path is not None else _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._shared_scope_enabled = (
            db_path is None and not str(os.environ.get("MEMORY_DB_PATH", "")).strip()
        )
        self._last_scope_hint: Optional[Dict[str, Any]] = None
        self._ensure_schema()
        if self._shared_scope_enabled:
            try:
                from datastore.docsdb.db_migration import migrate_legacy_docs_tables

                migrate_legacy_docs_tables(self.db_path, tables=("doc_chunks",))
            except Exception as exc:
                logger.warning("DocsRAG legacy docs-table merge skipped: %s", exc)

    def _ensure_schema(self):
        """Ensure the doc_chunks table exists."""
        with _lib_get_connection(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS doc_chunks (
                    id TEXT PRIMARY KEY,                    -- source_file:chunk_index
                    source_file TEXT NOT NULL,              -- Full path to source file  
                    chunk_index INTEGER NOT NULL,           -- 0-based chunk number within file
                    content TEXT NOT NULL,                  -- Chunk text content
                    section_header TEXT,                    -- Extracted markdown header (optional)
                    embedding BLOB NOT NULL,                -- float32 array, dim from config (ollama.embeddingDim)
                    
                    -- Metadata
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now')),
                    
                    UNIQUE(source_file, chunk_index)
                )
            """)
            
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_doc_chunks_source 
                ON doc_chunks(source_file)
            """)
            
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_doc_chunks_updated 
                ON doc_chunks(updated_at)
            """)

    def _doc_vec_table_exists(self, conn: sqlite3.Connection) -> bool:
        try:
            conn.execute("SELECT 1 FROM vec_doc_chunks LIMIT 0")
            return True
        except sqlite3.OperationalError:
            return False

    def _ensure_doc_vec_table(self, conn: sqlite3.Connection, embedding: List[float]) -> None:
        """Lazily create vec_doc_chunks using the provided embedding dimension."""
        if self._doc_vec_table_exists(conn):
            return
        dim = len(embedding)
        conn.execute(
            f"CREATE VIRTUAL TABLE vec_doc_chunks USING vec0(chunk_id TEXT PRIMARY KEY, embedding float[{dim}] distance_metric=cosine)"
        )

    def _build_doc_filter_sql(
        self,
        *,
        project: Optional[str],
        project_paths: Optional[Dict[str, Any]],
        registry_paths: List[str],
        doc_filters: List[str],
        workspace: Path,
        source_expr: str = "source_file",
    ) -> Tuple[Optional[str], List[Any], bool]:
        """Build SQL WHERE clause components for doc source filtering."""
        like_clauses: List[str] = []
        params: List[Any] = []
        if project and (project_paths or registry_paths):
            suffixes = set()
            project_like: List[str] = []
            if project_paths and project_paths["home_dir"]:
                project_like.append(f"{source_expr} LIKE ? ESCAPE '\\'")
                params.append(_escape_like(project_paths["home_dir"]) + "%")
                suffixes.update(_path_suffix_candidates(project_paths["home_dir"], workspace))
            if project_paths:
                for root in project_paths.get("source_roots", []):
                    project_like.append(f"{source_expr} LIKE ? ESCAPE '\\'")
                    params.append(_escape_like(root) + "%")
                    suffixes.update(_path_suffix_candidates(root, workspace))
            for rp in registry_paths:
                project_like.append(f"{source_expr} LIKE ? ESCAPE '\\'")
                params.append(_escape_like(rp) + "%")
                suffixes.update(_path_suffix_candidates(rp, workspace))
            for suffix in sorted(suffixes):
                # Some adapters index shared project docs with relative paths like
                # "projects/cross-live-test/..." instead of absolute source paths.
                # Project-scoped recall must match both shapes.
                project_like.append(f"{source_expr} LIKE ? ESCAPE '\\'")
                params.append(_escape_like(suffix))
                project_like.append(f"{source_expr} LIKE ? ESCAPE '\\'")
                params.append(f"{_escape_like(suffix)}/%")
                project_like.append(f"{source_expr} LIKE ? ESCAPE '\\'")
                params.append(f"%/{_escape_like(suffix)}")
                project_like.append(f"{source_expr} LIKE ? ESCAPE '\\'")
                params.append(f"%/{_escape_like(suffix)}/%")
            if project_like:
                like_clauses.append(f"({ ' OR '.join(project_like) })")
            elif not doc_filters:
                return None, [], True

        if doc_filters:
            doc_like = []
            for df in doc_filters:
                doc_like.append(f"{source_expr} LIKE ? ESCAPE '\\'")
                params.append(f"%{_escape_like(df)}%")
            if doc_like:
                like_clauses.append(f"({ ' OR '.join(doc_like) })")

        if not like_clauses:
            return "", params, False
        return " AND ".join(like_clauses), params, False
        
    def estimate_tokens(self, text: str) -> int:
        """Rough token estimation (4 chars ≈ 1 token)."""
        return len(text) // 4

    def _split_oversized_line(self, line: str, max_tokens: int) -> List[str]:
        """Split a single oversized line so chunking never ships a giant blob.

        Registry-managed source files can contain very long single-line JSON/CSS/JS
        content. If we let those through unchanged, a later embed request can fail
        hard even when normal markdown chunking is enabled.
        """
        raw = str(line or "")
        if self.estimate_tokens(raw) <= max_tokens:
            return [raw]

        max_chars = max(16, int(max_tokens) * 4)
        parts: List[str] = []
        remaining = raw
        while remaining:
            if len(remaining) <= max_chars:
                parts.append(remaining)
                break
            split_at = remaining.rfind(" ", 0, max_chars + 1)
            if split_at <= 0:
                split_at = max_chars
            parts.append(remaining[:split_at])
            remaining = remaining[split_at:]
            if remaining[:1].isspace():
                remaining = remaining[1:]
        return [part for part in parts if part]

    def chunk_markdown(self, content: str, max_tokens: int = None) -> List[DocumentChunk]:
        """Smart chunking of markdown content at headers/paragraphs."""
        if max_tokens is None:
            max_tokens = _chunk_max_tokens()
        lines = content.split('\n')
        chunks = []
        current_chunk_lines = []
        current_tokens = 0

        for raw_line in lines:
            expanded_lines = self._split_oversized_line(raw_line, max_tokens)
            for line in expanded_lines:
                line_tokens = self.estimate_tokens(line)

                # Check for header — start a new chunk at each heading boundary
                header_match = re.match(r'^(#{1,3})\s+(.+)', line)
                if header_match:
                    # Save current chunk if it has content
                    if current_chunk_lines:
                        chunks.append('\n'.join(current_chunk_lines))
                        current_chunk_lines = []
                        current_tokens = 0

                    current_chunk_lines.append(line)
                    current_tokens = line_tokens
                    continue

                # Add line to current chunk
                current_chunk_lines.append(line)
                current_tokens += line_tokens

                # Check if chunk is getting too large
                if current_tokens > max_tokens:
                    # Try to split at paragraph break
                    split_point = self._find_paragraph_break(current_chunk_lines)
                    if split_point > 0:
                        # Save chunk up to split point
                        chunks.append('\n'.join(current_chunk_lines[:split_point]))

                        # Start new chunk with overlap
                        overlap_lines = min(
                            max(0, split_point - 1),
                            max(0, _chunk_overlap_tokens() // 10),
                        )
                        overlap_start = split_point - overlap_lines
                        current_chunk_lines = current_chunk_lines[overlap_start:]
                        current_tokens = sum(self.estimate_tokens(l) for l in current_chunk_lines)
        
        # Don't forget the last chunk
        if current_chunk_lines:
            chunks.append('\n'.join(current_chunk_lines))
        
        # Convert to DocumentChunk objects (will be populated with source info later)
        return [chunk for chunk in chunks if chunk.strip()]

    def _find_paragraph_break(self, lines: List[str]) -> int:
        """Find the best place to split at a paragraph break."""
        # Look for empty lines working backwards
        for i in range(len(lines) - 1, 0, -1):
            if lines[i].strip() == '':
                return i
        
        # If no paragraph breaks, split at 75% of the way through
        return int(len(lines) * 0.75)

    def _get_file_hash(self, file_path: str) -> str:
        """Get a hash of file content for change detection."""
        try:
            with open(file_path, 'rb') as f:
                return hashlib.md5(f.read()).hexdigest()
        except Exception:
            return ""

    def _needs_reindex_from_indexed_at(self, file_path: str, indexed_at: Optional[str]) -> bool:
        """Check staleness using a preloaded indexed timestamp."""
        try:
            file_time = datetime.fromtimestamp(os.stat(file_path).st_mtime, tz=timezone.utc)
            if not indexed_at:
                return True
            indexed_time = datetime.fromisoformat(str(indexed_at))
            if indexed_time.tzinfo is None:
                indexed_time = indexed_time.replace(tzinfo=timezone.utc)
            return file_time > indexed_time
        except Exception as e:
            logger.warning("Error checking if %s needs reindex: %s", file_path, e)
            return True  # When in doubt, reindex

    def needs_reindex_many(self, file_paths: List[str]) -> Dict[str, bool]:
        """Check many files for staleness with batched doc_chunks lookups."""
        requested_files = [str(path or "").strip() for path in (file_paths or []) if str(path or "").strip()]
        if not requested_files:
            return {}

        variant_map: Dict[str, List[str]] = {}
        lookup_paths: List[str] = []
        for file_path in requested_files:
            variants: List[str] = []
            canonical = _canonical_source_path(file_path)
            for candidate in (file_path, canonical):
                normalized = str(candidate or "").strip()
                if normalized and normalized not in variants:
                    variants.append(normalized)
                if normalized and normalized not in lookup_paths:
                    lookup_paths.append(normalized)
            variant_map[file_path] = variants

        indexed_at_by_file: Dict[str, str] = {}
        batch_size = 250
        try:
            with _lib_get_connection(self.db_path) as conn:
                for start in range(0, len(lookup_paths), batch_size):
                    batch = lookup_paths[start:start + batch_size]
                    placeholders = ",".join("?" for _ in batch)
                    rows = conn.execute(
                        f"""
                            SELECT source_file, MAX(updated_at) AS indexed_at
                            FROM doc_chunks
                            WHERE source_file IN ({placeholders})
                            GROUP BY source_file
                        """,
                        tuple(batch),
                    ).fetchall()
                    for row in rows:
                        indexed_at_by_file[str(row["source_file"])] = str(row["indexed_at"] or "")
        except sqlite3.OperationalError as exc:
            if "no such table: doc_chunks" in str(exc).lower():
                return {file_path: True for file_path in requested_files}
            raise

        result: Dict[str, bool] = {}
        for file_path in requested_files:
            latest_indexed_at: Optional[str] = None
            for variant in variant_map.get(file_path, []):
                indexed_at = indexed_at_by_file.get(variant)
                if indexed_at and (latest_indexed_at is None or indexed_at > latest_indexed_at):
                    latest_indexed_at = indexed_at
            canonical = _canonical_source_path(file_path) or file_path
            result[file_path] = self._needs_reindex_from_indexed_at(canonical, latest_indexed_at)
        return result

    def needs_reindex(self, file_path: str) -> bool:
        """Check if file needs reindexing based on modification time."""
        try:
            return self.needs_reindex_many([file_path]).get(file_path, True)
        except Exception as e:
            logger.warning("Error checking if %s needs reindex: %s", file_path, e)
            return True  # When in doubt, reindex

    def _is_archive_log(self, file_path: str) -> bool:
        """Check if a file is an archived log (e.g. projects/myapp/log/2026-01.log)."""
        p = Path(file_path)
        return p.parent.name == "log" and p.suffix == ".log" and p.stem != "PROJECT"

    def _archive_temporal_header(self, file_path: str) -> str:
        """Generate a temporal context header for archived log files.

        This tells the LLM the content is historical, not current,
        preventing confusion between past and present state.
        """
        p = Path(file_path)
        month = p.stem  # e.g. "2026-01"
        # Walk up to find the project name (parent of log/)
        project_name = p.parent.parent.name
        return (
            f"# HISTORICAL LOG — {project_name} — {month}\n\n"
            f"> These are ARCHIVED log entries from {month}. "
            f"They reflect past state and may no longer be current. "
            f"For current state, refer to PROJECT.log.\n\n"
        )

    def index_document(self, file_path: str) -> int:
        """Index a single document, returning number of chunks created."""
        canonical_file_path = _canonical_source_path(file_path)
        if not canonical_file_path:
            logger.warning("Empty document path provided for indexing")
            return 0
        source_variants: List[str] = []
        for candidate in (str(file_path or "").strip(), canonical_file_path):
            if candidate and candidate not in source_variants:
                source_variants.append(candidate)
        try:
            with open(canonical_file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            logger.warning("Error reading %s: %s", canonical_file_path, e)
            return 0

        # Add temporal context for archived log files
        if self._is_archive_log(canonical_file_path):
            content = self._archive_temporal_header(canonical_file_path) + content

        # Chunk the content
        chunk_texts = self.chunk_markdown(content)
        if not chunk_texts:
            logger.info("No chunks generated for %s", file_path)
            return 0

        # Collect all embeddings BEFORE deleting old chunks.
        # This prevents data loss if Ollama is down or embedding fails.
        embeddings = _lib_get_embeddings(
            chunk_texts,
            pool_name="rag_embeddings",
            task_name="rag",
        )
        prepared_chunks = []
        for i, (chunk_text, embedding) in enumerate(zip(chunk_texts, embeddings)):
            section_header = self._extract_section_header(chunk_text)
            if not embedding:
                logger.warning(
                    "Failed embedding for chunk %s in %s; aborting reindex to preserve old chunks",
                    i,
                    canonical_file_path,
                )
                return 0
            prepared_chunks.append((
                f"{canonical_file_path}:{i}",
                canonical_file_path,
                i,
                chunk_text,
                section_header,
                _lib_pack_embedding(embedding),
            ))

        if not prepared_chunks:
            logger.warning("All embeddings failed for %s; keeping old chunks", file_path)
            return 0

        # All embeddings succeeded — now safe to delete and replace
        chunks_created = 0
        with _lib_get_connection(self.db_path) as conn:
            placeholders = ",".join("?" for _ in source_variants)
            old_chunk_ids = [row[0] for row in conn.execute(
                f"SELECT id FROM doc_chunks WHERE source_file IN ({placeholders})",
                tuple(source_variants),
            ).fetchall()]
            conn.execute(
                f"DELETE FROM doc_chunks WHERE source_file IN ({placeholders})",
                tuple(source_variants),
            )
            conn.executemany(
                """
                    INSERT INTO doc_chunks
                    (id, source_file, chunk_index, content, section_header, embedding, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                """,
                prepared_chunks,
            )
            chunks_created = len(prepared_chunks)
            if _lib_has_vec() and prepared_chunks:
                if not self._doc_vec_table_exists(conn):
                    self._ensure_doc_vec_table(conn, embeddings[0])
                if old_chunk_ids and self._doc_vec_table_exists(conn):
                    conn.executemany(
                        "DELETE FROM vec_doc_chunks WHERE chunk_id = ?",
                        [(chunk_id,) for chunk_id in old_chunk_ids],
                    )
                target_chunk_ids = list({chunk_id for chunk_id, _, _, _, _, _ in prepared_chunks})
                if target_chunk_ids and self._doc_vec_table_exists(conn):
                    conn.executemany(
                        "DELETE FROM vec_doc_chunks WHERE chunk_id = ?",
                        [(chunk_id,) for chunk_id in target_chunk_ids],
                    )
                conn.executemany(
                    "INSERT INTO vec_doc_chunks(chunk_id, embedding) VALUES (?, ?)",
                    [(chunk_id, packed_embedding) for chunk_id, _, _, _, _, packed_embedding in prepared_chunks],
                )
                # Verify the vec entry was actually created. The INSERT can partially
                # succeed (creating the rowid shadow entry without updating the HNSW
                # index) and leave the doc unfindable via vector search. If the entry
                # is absent, clear chunks_created so last_indexed_at is NOT set and
                # the doc will be re-indexed on the next run.
                first_chunk_id = prepared_chunks[0][0]
                vec_row = conn.execute(
                    "SELECT COUNT(*) FROM vec_doc_chunks_rowids WHERE id = ?",
                    (first_chunk_id,),
                ).fetchone()
                if not vec_row or vec_row[0] == 0:
                    logger.warning(
                        "[docs] vec INSERT appeared to succeed but %s is absent from "
                        "vec_doc_chunks_rowids; last_indexed_at will NOT be set so the "
                        "doc is re-indexed on the next run",
                        canonical_file_path,
                    )
                    chunks_created = 0

        logger.info("[docs] Indexed %s chunks from %s", chunks_created, canonical_file_path)

        # Sync indexed timestamp to registry
        if chunks_created > 0:
            try:
                from datastore.docsdb.registry import DocsRegistry
                registry = DocsRegistry(self.db_path)
                now = datetime.now().isoformat()
                # Try both absolute and relative paths
                registry.update_timestamps(file_path, indexed_at=now)
                registry.update_timestamps(canonical_file_path, indexed_at=now)
                try:
                    rel = str(Path(canonical_file_path).relative_to(_workspace()))
                    registry.update_timestamps(rel, indexed_at=now)
                except ValueError:
                    pass
            except Exception:
                pass  # Registry sync is best-effort

        return chunks_created

    def remove_chunks_for_path(self, file_path: str) -> int:
        """Remove indexed chunks for a source file path (raw + canonical aliases)."""
        raw_path = str(file_path or "").strip()
        canonical_file_path = _canonical_source_path(raw_path)
        source_variants: List[str] = []
        for candidate in (raw_path, canonical_file_path):
            if candidate and candidate not in source_variants:
                source_variants.append(candidate)
        if not source_variants:
            return 0

        removed = 0
        with _lib_get_connection(self.db_path) as conn:
            placeholders = ",".join("?" for _ in source_variants)
            old_chunk_ids = [row[0] for row in conn.execute(
                f"SELECT id FROM doc_chunks WHERE source_file IN ({placeholders})",
                tuple(source_variants),
            ).fetchall()]
            if not old_chunk_ids:
                return 0
            removed = len(old_chunk_ids)
            conn.execute(
                f"DELETE FROM doc_chunks WHERE source_file IN ({placeholders})",
                tuple(source_variants),
            )
            if _lib_has_vec() and self._doc_vec_table_exists(conn):
                conn.executemany(
                    "DELETE FROM vec_doc_chunks WHERE chunk_id = ?",
                    [(chunk_id,) for chunk_id in old_chunk_ids],
                )
        logger.info("[docs] Removed %s indexed chunk(s) for %s", removed, source_variants[0])
        return removed

    def _extract_section_header(self, chunk_text: str) -> Optional[str]:
        """Extract the first header from a chunk."""
        lines = chunk_text.split('\n')
        for line in lines:
            header_match = re.match(r'^(#{1,3})\s+(.+)', line.strip())
            if header_match:
                return line.strip()
        return None

    # Files that live in context (loaded via session-init into .claude/rules/) and
    # should never be RAG-indexed — returning them as doc chunks would be redundant noise.
    # Files injected into context via session-init hook — indexing them as RAG
    # chunks would return redundant noise since they're already in the window.
    _CONTEXT_FILES = frozenset({"SOUL.md", "USER.md", "ENVIRONMENT.md", "AGENTS.md", "TOOLS.md"})

    def _is_context_file(self, path: Path) -> bool:
        return path.name in self._CONTEXT_FILES

    def scan_docs_directory(self, docs_dir: str) -> List[str]:
        """Recursively find indexable docs in directory.

        Scans for:
        - Markdown files (*.md)
        - Current project logs (PROJECT.log)
        - Archived project logs (log/*.log) — historical entries with temporal context

        Excludes files that are always loaded into context (SOUL.md, USER.md, etc.)
        since indexing them would return redundant chunks.
        """
        doc_files = []
        docs_path = Path(docs_dir)

        if not docs_path.exists():
            print(f"Directory does not exist: {docs_dir}")
            return []

        for pattern in ('*.md', 'PROJECT.log'):
            for doc_file in docs_path.rglob(pattern):
                if not self._is_context_file(doc_file):
                    doc_files.append(str(doc_file.absolute()))

        # Include archived log files (monthly archives from log rotation)
        for doc_file in docs_path.rglob('log/*.log'):
            doc_files.append(str(doc_file.absolute()))

        return sorted(set(doc_files))

    def reindex_all(self, docs_dir: str, force: bool = False) -> Dict[str, int]:
        """Scan and index all documentation, optionally forcing full reindex."""
        print(f"Scanning for docs (.md + PROJECT.log) in {docs_dir}")
        md_files = self.scan_docs_directory(docs_dir)
        
        if not md_files:
            print(f"No indexable docs found in {docs_dir}")
            return {"total_files": 0, "indexed_files": 0, "total_chunks": 0}
        
        print(f"Found {len(md_files)} docs")
        
        indexed_files = 0
        total_chunks = 0
        skipped_files = 0
        reindex_needed = self.needs_reindex_many(md_files) if not force else {}
        
        for file_path in md_files:
            if not force and not reindex_needed.get(file_path, True):
                skipped_files += 1
                continue
                
            chunks = self.index_document(file_path)
            if chunks > 0:
                indexed_files += 1
                total_chunks += chunks
        
        result = {
            "total_files": len(md_files),
            "indexed_files": indexed_files,
            "skipped_files": skipped_files,
            "total_chunks": total_chunks
        }
        
        print(f"Indexing complete: {indexed_files} files indexed, {skipped_files} skipped, {total_chunks} total chunks")
        return result

    def _normalize_docs_filter(self, docs: Optional[List[str]]) -> List[str]:
        if not docs:
            return []
        out: List[str] = []
        for raw in docs:
            if raw is None:
                continue
            val = str(raw).strip()
            if not val:
                continue
            out.append(val)
        # De-duplicate while preserving order
        seen = set()
        uniq: List[str] = []
        for item in out:
            if item in seen:
                continue
            seen.add(item)
            uniq.append(item)
        return uniq

    def infer_project_for_source(self, source_file: str) -> Optional[str]:
        """Infer project label for a source path from project definitions/registry."""
        if not source_file:
            return None

        try:
            source_path = Path(source_file).resolve()
        except Exception:
            source_path = Path(str(source_file))

        try:
            from lib.project_registry import list_all as _list_projects

            best_match = None
            best_prefix_len = -1
            for project_name in (_list_projects() or {}).keys():
                path_info = self._get_project_paths(str(project_name))
                candidate_roots: List[Path] = []
                home_dir = str(path_info.get("home_dir") or "").strip()
                if home_dir:
                    candidate_roots.append(Path(home_dir))
                for root in path_info.get("source_roots", []) or []:
                    root_str = str(root or "").strip()
                    if root_str:
                        candidate_roots.append(Path(root_str))

                for candidate in candidate_roots:
                    try:
                        candidate = candidate.resolve()
                    except Exception:
                        pass
                    prefix = str(candidate)
                    if str(source_path) == prefix or str(source_path).startswith(prefix + os.sep):
                        if len(prefix) > best_prefix_len:
                            best_prefix_len = len(prefix)
                            best_match = str(project_name)

            if best_match:
                return best_match
        except Exception:
            pass

        try:
            from datastore.docsdb.registry import DocsRegistry

            try:
                registry = DocsRegistry(self.db_path)
            except TypeError:
                registry = DocsRegistry()
            workspace = _workspace().resolve()
            for doc in registry.list_docs():
                raw_path = str(doc.get("file_path") or "").strip()
                if not raw_path:
                    continue
                p = Path(raw_path)
                if not p.is_absolute():
                    p = workspace / p
                try:
                    same_path = p.resolve() == source_path
                except Exception:
                    same_path = str(p) == str(source_path)
                if same_path:
                    project_name = str(doc.get("project") or "").strip()
                    if project_name:
                        return project_name
        except Exception:
            pass

        return None

    def infer_project_from_chunks(self, chunks: List[Dict]) -> Optional[str]:
        """Choose the most indicated project from retrieved chunks."""
        scores: Dict[str, float] = {}
        counts: Dict[str, int] = {}

        for chunk in chunks or []:
            project = str(chunk.get("project") or "").strip()
            if not project:
                project = self.infer_project_for_source(str(chunk.get("source") or ""))
            if not project:
                continue
            similarity = chunk.get("similarity", 0.0)
            try:
                weight = float(similarity)
            except (TypeError, ValueError):
                weight = 0.0
            scores[project] = scores.get(project, 0.0) + weight
            counts[project] = counts.get(project, 0) + 1

        if not scores:
            return None

        return max(scores.keys(), key=lambda project: (scores[project], counts.get(project, 0), project))

    def load_project_md(self, project: Optional[str]) -> Optional[str]:
        """Load PROJECT.md for a given project if available."""
        if not project:
            return None
        try:
            paths = self._get_project_paths(str(project))
            home_dir = str(paths.get("home_dir") or "").strip()
            if home_dir:
                md_path = Path(home_dir) / "PROJECT.md"
                if md_path.exists():
                    return md_path.read_text(encoding="utf-8")
        except Exception:
            pass
        return None

    def search_docs(self, query: str, limit: int = 5, min_similarity: float = 0.3,
                    project: Optional[str] = None, docs: Optional[List[str]] = None) -> List[Dict]:
        """Semantic search of document chunks.

        Args:
            project: If set, only return results from files belonging to this project.
                     Uses doc_registry + project homeDir for filtering.
            docs: Optional list of doc filters (basename, relative path, or path fragment).
        """
        self._last_scope_hint = None
        query_embedding = _lib_get_embedding(query)
        if not query_embedding:
            if is_fail_hard_enabled():
                raise RuntimeError(
                    "Doc RAG embedding failed while failHard is enabled; "
                    "no degraded docs fallback allowed."
                )
            logger.warning("Failed to get embedding for query; returning no RAG results")
            return []

        # Build project/doc filter — SQL-level filtering avoids full scans.
        doc_filters = self._normalize_docs_filter(docs)
        query_terms = _docs_query_terms(query)

        project_paths = None
        registry_paths: List[str] = []
        workspace = _workspace().resolve()
        project_scope_token: Optional[str] = None

        def _add_scope_paths_for_project(
            project_name: str,
            seen_registry_paths: set[str],
            *,
            include_project_roots: bool,
        ) -> None:
            path_info = self._get_project_paths(project_name)
            if include_project_roots:
                home_dir = str(path_info.get("home_dir") or "").strip()
                if home_dir:
                    for candidate in (home_dir, _canonical_source_path(home_dir)):
                        if candidate and candidate not in seen_registry_paths:
                            seen_registry_paths.add(candidate)
                            registry_paths.append(candidate)
                for root in path_info.get("source_roots", []) or []:
                    root_str = str(root or "").strip()
                    if not root_str:
                        continue
                    for candidate in (root_str, _canonical_source_path(root_str)):
                        if candidate and candidate not in seen_registry_paths:
                            seen_registry_paths.add(candidate)
                            registry_paths.append(candidate)

            # Also include all registered external doc paths for this project.
            try:
                from datastore.docsdb.registry import DocsRegistry

                try:
                    registry = DocsRegistry(self.db_path)
                except TypeError:
                    # Test doubles often expose a no-arg constructor.
                    registry = DocsRegistry()
                reg_docs = registry.list_docs(project=project_name)
                resolver = getattr(registry, "_resolve_path", None)
                for d in reg_docs:
                    raw_path = str(d.get("file_path") or "").strip()
                    if not raw_path:
                        continue
                    candidates: List[Path] = []
                    if callable(resolver):
                        try:
                            candidates.append(Path(resolver(raw_path)))
                        except Exception as exc:
                            if is_fail_hard_enabled():
                                raise RuntimeError(
                                    f"Failed to resolve docs registry path: {raw_path!r}"
                                ) from exc
                            logger.warning(
                                "docs recall failed to resolve registry path %r: %s",
                                raw_path,
                                exc,
                            )
                    p = Path(raw_path)
                    if not p.is_absolute():
                        p = workspace / p
                    candidates.append(p)
                    for candidate in candidates:
                        candidate_str = str(candidate)
                        if candidate_str and candidate_str not in seen_registry_paths:
                            seen_registry_paths.add(candidate_str)
                            registry_paths.append(candidate_str)
                        canonical = _canonical_source_path(candidate_str)
                        if canonical and canonical not in seen_registry_paths:
                            seen_registry_paths.add(canonical)
                            registry_paths.append(canonical)
            except RuntimeError:
                raise
            except Exception as exc:
                if is_fail_hard_enabled():
                    raise RuntimeError(
                        f"Failed to load docs registry paths for project {project_name!r}"
                    ) from exc
                logger.warning(
                    "docs recall failed to load registry paths for project %r: %s",
                    project_name,
                    exc,
                )

        linked_projects: List[str] = []
        scope_resolved = False
        if self._shared_scope_enabled and not doc_filters:
            linked_projects, scope_resolved = _linked_projects_for_current_instance()

        scope_forced_empty = False
        if project:
            if scope_resolved and project not in set(linked_projects):
                scope_forced_empty = True
            project_scope_token = project
            project_paths = self._get_project_paths(project)
            seen_registry_paths: set[str] = set()
            _add_scope_paths_for_project(project, seen_registry_paths, include_project_roots=False)
        elif scope_resolved:
            if not linked_projects:
                scope_forced_empty = True
            else:
                project_scope_token = "__instance_linked_scope__"
                seen_registry_paths: set[str] = set()
                for project_name in linked_projects:
                    _add_scope_paths_for_project(project_name, seen_registry_paths, include_project_roots=True)
                if not registry_paths:
                    scope_forced_empty = True

        results = []
        with _lib_get_connection(self.db_path) as conn:
            if scope_forced_empty:
                self._set_scope_hint_for_unlinked_candidates(
                    conn=conn,
                    query=query,
                    query_embedding=query_embedding,
                    linked_projects=linked_projects,
                    requested_project=project,
                )
                return []

            where, params, empty = self._build_doc_filter_sql(
                project=project_scope_token,
                project_paths=project_paths,
                registry_paths=registry_paths,
                doc_filters=doc_filters,
                workspace=workspace,
                source_expr="dc.source_file",
            )
            if empty:
                if project_scope_token == "__instance_linked_scope__":
                    self._set_scope_hint_for_unlinked_candidates(
                        conn=conn,
                        query=query,
                        query_embedding=query_embedding,
                        linked_projects=linked_projects,
                        requested_project=project,
                    )
                return []

            use_vec = _lib_has_vec() and self._doc_vec_table_exists(conn)
            if use_vec and (project_scope_token or doc_filters):
                # sqlite-vec evaluates ANN k-NN before the SQL WHERE predicate.
                # For project/docs-scoped recall, that can starve valid matches when
                # the global top-k candidate set doesn't contain scoped rows.
                # Prefer exact filtered scan semantics for scoped queries.
                use_vec = False
            if use_vec:
                try:
                    packed_query = _lib_pack_embedding(query_embedding)
                    candidate_limit = max(64, limit * 16)
                    sql = """
                        SELECT dc.*, knn.distance AS vec_distance
                        FROM (
                            SELECT chunk_id, distance
                            FROM vec_doc_chunks
                            WHERE embedding MATCH ? AND k = ?
                        ) knn
                        JOIN doc_chunks dc ON dc.id = knn.chunk_id
                    """
                    if where:
                        sql += f" WHERE {where}"
                    sql += " ORDER BY knn.distance"
                    rows = conn.execute(sql, tuple([packed_query, candidate_limit] + params)).fetchall()
                except Exception as exc:
                    if is_fail_hard_enabled():
                        raise RuntimeError(
                            "Doc RAG vec recall failed while failHard is enabled."
                        ) from exc
                    logger.warning("Doc RAG vec recall failed; falling back to row scan: %s", exc)
                    use_vec = False

            if not use_vec:
                scan_where, scan_params, _ = self._build_doc_filter_sql(
                    project=project_scope_token,
                    project_paths=project_paths,
                    registry_paths=registry_paths,
                    doc_filters=doc_filters,
                    workspace=workspace,
                    source_expr="source_file",
                )
                if scan_where:
                    rows = conn.execute(f"SELECT * FROM doc_chunks WHERE {scan_where}", tuple(scan_params)).fetchall()
                else:
                    rows = conn.execute("SELECT * FROM doc_chunks").fetchall()

            for row in rows:
                source_file = str(row[1] or "")
                if self._is_context_file(Path(source_file)):
                    continue
                if use_vec:
                    distance = row["vec_distance"]
                    similarity = max(-1.0, min(1.0, 1.0 - float(distance if distance is not None else 1.0)))
                else:
                    chunk_embedding = _lib_unpack_embedding(row[5])  # embedding is column 5
                    similarity = _lib_cosine_similarity(query_embedding, chunk_embedding)

                if similarity >= min_similarity:
                    content = row[3]
                    section_header = row[4]
                    rank_score = _docs_rank_score(
                        query_terms,
                        source_file,
                        section_header,
                        content,
                        similarity,
                    )
                    inferred_project = project or self.infer_project_for_source(row[1])
                    results.append({
                        "content": content,
                        "source": source_file,
                        "section_header": section_header,
                        "similarity": min(1.0, rank_score),
                        "chunk_index": row[2],  # chunk_index
                        "project": inferred_project,
                    })

        # Sort by similarity and limit
        results.sort(key=lambda x: x["similarity"], reverse=True)
        limited_results = results[:limit]
        if not limited_results and project_scope_token == "__instance_linked_scope__":
            with _lib_get_connection(self.db_path) as conn:
                self._set_scope_hint_for_unlinked_candidates(
                    conn=conn,
                    query=query,
                    query_embedding=query_embedding,
                    linked_projects=linked_projects,
                    requested_project=project,
                )
        return limited_results

    def search_docs_bundle(
        self,
        query: str,
        limit: int = 5,
        min_similarity: float = 0.3,
        project: Optional[str] = None,
        docs: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Search docs and attach the most indicated project's PROJECT.md."""
        chunks = self.search_docs(
            query=query,
            limit=limit,
            min_similarity=min_similarity,
            project=project,
            docs=docs,
        )
        inferred_project = project or self.infer_project_from_chunks(chunks)
        bundle = {
            "chunks": chunks,
            "project": inferred_project,
            "project_md": self.load_project_md(inferred_project),
        }
        telemetry: Dict[str, Any] = {}
        if _docs_recall_telemetry_enabled():
            telemetry.update({
                "query": query,
                "requested_project": project,
                "resolved_project": inferred_project,
                "requested_docs": list(docs or []),
                "chunk_count": len(chunks),
                "project_md_attached": bool(bundle["project_md"]),
                "sources": [chunk.get("source") for chunk in chunks[:5]],
                "top_similarity": chunks[0].get("similarity") if chunks else None,
            })
        if isinstance(self._last_scope_hint, dict):
            telemetry["scope_hint"] = dict(self._last_scope_hint)
        if telemetry:
            bundle["telemetry"] = telemetry
        return bundle

    def _get_project_paths(self, project: str) -> dict:
        """Get project path info for filtering."""
        try:
            from datastore.docsdb.registry import DocsRegistry

            defn = DocsRegistry(self.db_path).get_project_definition(project)
            if defn is not None:
                return {
                    "home_dir": str(_resolve_project_root(defn.home_dir)),
                    "source_roots": [str(_resolve_project_root(r)) for r in (defn.source_roots or [])],
                }
        except Exception:
            pass

        try:
            from lib.project_registry import lookup as _get_project

            entry = _get_project(project) or {}
            canonical_path = str(entry.get("canonical_path") or "").strip()
            if canonical_path:
                return {
                    "home_dir": canonical_path,
                    "source_roots": [],
                }
        except Exception:
            pass

        return {"home_dir": "", "source_roots": []}

    def _row_value(self, row: Any, key: str, index: int) -> Any:
        try:
            return row[key]
        except Exception:
            try:
                return row[index]
            except Exception:
                return None

    def _suggest_unlinked_projects(
        self,
        *,
        conn: sqlite3.Connection,
        query: str,
        query_embedding: List[float],
        linked_projects: List[str],
        limit: int = 3,
    ) -> List[Dict[str, Any]]:
        linked_set = {str(name or "").strip() for name in list(linked_projects or []) if str(name or "").strip()}
        project_rows: Dict[str, Any] = {}
        try:
            from lib.project_registry import list_all as _list_projects

            project_rows = _list_projects() or {}
        except Exception:
            # Keep scope-hint behavior available even when registry metadata is
            # unavailable; inferred source-project labels can still surface likely
            # unlinked candidates for a scoped miss.
            project_rows = {}

        all_projects = [
            str(name or "").strip()
            for name in project_rows.keys()
            if str(name or "").strip()
        ]

        candidate_projects = [name for name in all_projects if name not in linked_set]
        candidate_set = set(candidate_projects)
        query_lower = str(query or "").lower()
        name_matches: set[str] = set()
        for name in candidate_projects:
            lowered = name.lower()
            if lowered and lowered in query_lower:
                name_matches.add(name)
                continue
            alias = lowered.replace("-", " ").replace("_", " ")
            if alias and alias in query_lower:
                name_matches.add(name)

        scores: Dict[str, float] = {}
        counts: Dict[str, int] = {}
        source_project_cache: Dict[str, Optional[str]] = {}
        use_vec = _lib_has_vec() and self._doc_vec_table_exists(conn)
        rows: List[Any] = []

        if use_vec:
            try:
                packed_query = _lib_pack_embedding(query_embedding)
                candidate_limit = max(96, limit * 40)
                rows = conn.execute(
                    """
                    SELECT dc.source_file AS source_file, knn.distance AS vec_distance
                    FROM (
                        SELECT chunk_id, distance
                        FROM vec_doc_chunks
                        WHERE embedding MATCH ? AND k = ?
                    ) knn
                    JOIN doc_chunks dc ON dc.id = knn.chunk_id
                    ORDER BY knn.distance
                    """,
                    (packed_query, candidate_limit),
                ).fetchall()
            except Exception as exc:
                if is_fail_hard_enabled():
                    raise RuntimeError(
                        "Scoped docs miss fallback failed in vec candidate stage while failHard is enabled."
                    ) from exc
                logger.warning("Scoped docs miss fallback vec stage failed; using row scan: %s", exc)
                use_vec = False

        if not use_vec:
            rows = conn.execute("SELECT source_file, embedding FROM doc_chunks").fetchall()

        for row in rows:
            source_file = str(self._row_value(row, "source_file", 0) or "").strip()
            if not source_file:
                continue
            inferred_project = source_project_cache.get(source_file)
            if inferred_project is None:
                inferred_project = self.infer_project_for_source(source_file)
                source_project_cache[source_file] = inferred_project
            if not inferred_project:
                continue
            if inferred_project in linked_set:
                continue

            if use_vec:
                distance = self._row_value(row, "vec_distance", 1)
                similarity = max(-1.0, min(1.0, 1.0 - float(distance if distance is not None else 1.0)))
            else:
                chunk_embedding = _lib_unpack_embedding(self._row_value(row, "embedding", 1))
                similarity = _lib_cosine_similarity(query_embedding, chunk_embedding)

            base_score = float(similarity or 0.0)
            if inferred_project in name_matches:
                base_score += 0.08
            prior = scores.get(inferred_project, -1.0)
            if base_score > prior:
                scores[inferred_project] = base_score
            counts[inferred_project] = counts.get(inferred_project, 0) + 1

        if not scores and name_matches:
            for name in sorted(name_matches):
                scores[name] = 0.51
                counts[name] = 0

        if not scores:
            return []

        min_score = 0.56
        out: List[Dict[str, Any]] = []
        for project_name, score in scores.items():
            matched = project_name in name_matches
            if score < min_score and not matched:
                continue
            out.append(
                {
                    "project": project_name,
                    "score": round(float(score), 3),
                    "matched_query_name": bool(matched),
                    "match_count": int(counts.get(project_name, 0)),
                }
            )

        out.sort(
            key=lambda item: (
                1 if item.get("matched_query_name") else 0,
                float(item.get("score") or 0.0),
                int(item.get("match_count") or 0),
                str(item.get("project") or ""),
            ),
            reverse=True,
        )
        return out[: max(1, int(limit))]

    def _set_scope_hint_for_unlinked_candidates(
        self,
        *,
        conn: sqlite3.Connection,
        query: str,
        query_embedding: List[float],
        linked_projects: List[str],
        requested_project: Optional[str] = None,
    ) -> None:
        try:
            suggestions = self._suggest_unlinked_projects(
                conn=conn,
                query=query,
                query_embedding=query_embedding,
                linked_projects=linked_projects,
                limit=3,
            )
        except Exception as exc:
            if is_fail_hard_enabled():
                raise RuntimeError(
                    "Failed to compute unlinked project scope hints while failHard is enabled."
                ) from exc
            logger.warning("Failed to compute unlinked project scope hints: %s", exc)
            self._last_scope_hint = None
            return
        if not suggestions:
            self._last_scope_hint = None
            return
        project_names = [str(item.get("project") or "").strip() for item in suggestions if str(item.get("project") or "").strip()]
        if not project_names:
            self._last_scope_hint = None
            return
        self._last_scope_hint = {
            "type": "unlinked_project_candidates",
            "message": (
                "No docs matched inside currently linked projects. "
                "Likely unlinked project candidates were found. "
                "Ask the user whether to link one before falling back to filesystem grep."
            ),
            "requested_project": str(requested_project or "").strip() or None,
            "linked_projects": sorted(
                str(name or "").strip() for name in list(linked_projects or []) if str(name or "").strip()
            ),
            "candidates": suggestions,
        }

    def stats(self) -> Dict:
        """Get indexing statistics."""
        with _lib_get_connection(self.db_path) as conn:
            chunk_count = conn.execute("SELECT COUNT(*) FROM doc_chunks").fetchone()[0]
            file_count = conn.execute("SELECT COUNT(DISTINCT source_file) FROM doc_chunks").fetchone()[0]
            
            last_indexed = conn.execute(
                "SELECT MAX(updated_at) FROM doc_chunks"
            ).fetchone()[0]
            
            # File type breakdown
            file_types = conn.execute("""
                SELECT 
                    CASE 
                        WHEN source_file LIKE '%/docs/%' THEN 'docs/'
                        WHEN source_file LIKE '%.md' AND source_file NOT LIKE '%/docs/%' THEN 'workspace'
                        ELSE 'other'
                    END as category,
                    COUNT(DISTINCT source_file) as file_count,
                    COUNT(*) as chunk_count
                FROM doc_chunks 
                GROUP BY category
            """).fetchall()
        
        return {
            "total_chunks": chunk_count,
            "total_files": file_count,
            "last_indexed": last_indexed,
            "by_category": {row[0]: {"files": row[1], "chunks": row[2]} for row in file_types}
        }


def register_lifecycle_routines(registry, result_factory) -> None:
    """Register docs/project RAG lifecycle maintenance routines."""

    def _run_rag_maintenance(ctx):
        result = result_factory()
        cfg = ctx.cfg
        dry_run = ctx.dry_run
        workspace = ctx.workspace

        try:
            if dry_run:
                result.logs.append("Skipping RAG reindex (dry-run)")
                return result

            if cfg.projects.enabled and not dry_run:
                try:
                    updater_mod = sys.modules.get("project_updater")
                    if updater_mod is not None and hasattr(updater_mod, "process_all_events"):
                        process_all_events = updater_mod.process_all_events
                    else:
                        from datastore.docsdb.project_updater import process_all_events

                    result.logs.append("Processing queued project events...")
                    event_result = process_all_events()
                    processed = int(event_result.get("processed", 0))
                    result.metrics["project_events_processed"] = processed
                    if processed > 0:
                        result.logs.append(f"  Processed {processed} event(s)")
                except Exception as exc:
                    result.errors.append(f"Project event processing failed: {exc}")
            elif cfg.projects.enabled and dry_run:
                result.logs.append("Skipping project event processing (dry-run)")

            if cfg.projects.enabled:
                try:
                    registry_mod = sys.modules.get("docs_registry")
                    if registry_mod is not None and hasattr(registry_mod, "DocsRegistry"):
                        DocsRegistry = registry_mod.DocsRegistry
                    else:
                        from datastore.docsdb.registry import DocsRegistry

                    docs_registry = DocsRegistry()
                    total_discovered = 0
                    for proj_name, proj_defn in cfg.projects.definitions.items():
                        if proj_defn.auto_index:
                            discovered = docs_registry.auto_discover(proj_name)
                            total_discovered += len(discovered)
                    result.metrics["project_files_discovered"] = total_discovered
                    if total_discovered > 0:
                        result.logs.append(f"  Discovered {total_discovered} new file(s)")

                    for proj_name in cfg.projects.definitions:
                        try:
                            docs_registry.sync_external_files(proj_name)
                        except Exception as exc:
                            if is_fail_hard_enabled():
                                raise RuntimeError(
                                    f"Project external sync failed for {proj_name}"
                                ) from exc
                            logger.warning("Project external sync failed for %s: %s", proj_name, exc)
                            continue
                except Exception as exc:
                    if is_fail_hard_enabled():
                        raise RuntimeError("Project auto-discover failed") from exc
                    result.errors.append(f"Project auto-discover failed: {exc}")

            rag = DocsRAG()
            docs_dir = str(workspace / cfg.rag.docs_dir)
            result.logs.append(f"Reindexing {docs_dir}...")
            rag_result = rag.reindex_all(docs_dir, force=False)

            total_files = int(rag_result.get("total_files", 0))
            indexed = int(rag_result.get("indexed_files", 0))
            skipped = int(rag_result.get("skipped_files", 0))
            chunks = int(rag_result.get("total_chunks", 0))

            if cfg.projects.enabled:
                for proj_name, proj_defn in cfg.projects.definitions.items():
                    proj_dir = _resolve_project_root(proj_defn.home_dir)
                    if proj_dir.exists():
                        result.logs.append(f"Reindexing project {proj_name}: {proj_dir}...")
                        proj_result = rag.reindex_all(str(proj_dir), force=False)
                        total_files += int(proj_result.get("total_files", 0))
                        indexed += int(proj_result.get("indexed_files", 0))
                        skipped += int(proj_result.get("skipped_files", 0))
                        chunks += int(proj_result.get("total_chunks", 0))

            # Third pass: index files registered via doc_registry, including source
            # files under project dirs. Passes 1 and 2 only cover markdown/log files,
            # so registry-managed JS/JSON/CSS/HTML files must still be considered here.
            try:
                from datastore.docsdb.registry import DocsRegistry as _DR
                reg_docs = _DR().list_docs()
                runtime_workspace = _workspace()
                registry_paths = []
                seen_registry_paths = set()
                for doc in reg_docs:
                    raw_path = doc.get("file_path", "")
                    if not raw_path:
                        continue
                    p = Path(raw_path)
                    if not p.is_absolute():
                        p = runtime_workspace / p
                    if not p.is_file():
                        continue
                    resolved = str(p.resolve())
                    if resolved in seen_registry_paths:
                        continue
                    seen_registry_paths.add(resolved)
                    registry_paths.append(resolved)

                registry_reindex = rag.needs_reindex_many(registry_paths)
                for file_path in registry_paths:
                    if registry_reindex.get(file_path, True):
                        doc_chunks = rag.index_document(file_path)
                        if doc_chunks > 0:
                            indexed += 1
                            chunks += doc_chunks
                        total_files += 1
                    else:
                        total_files += 1
                        skipped += 1
            except Exception as exc:
                logger.warning("doc_registry pass failed during RAG maintenance: %s", exc)

            result.metrics["rag_total_files"] = total_files
            result.metrics["rag_files_indexed"] = indexed
            result.metrics["rag_files_skipped"] = skipped
            result.metrics["rag_chunks_created"] = chunks
        except Exception as exc:
            if is_fail_hard_enabled():
                raise RuntimeError("RAG maintenance failed") from exc
            result.errors.append(f"RAG maintenance failed: {exc}")

        return result

    registry.register("rag", _run_rag_maintenance)


def main():
    """CLI interface for docs RAG system."""
    parser = argparse.ArgumentParser(description="RAG for Technical Documentation")
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # Reindex command
    reindex_parser = subparsers.add_parser('reindex', help='Reindex documentation')
    reindex_parser.add_argument('--all', action='store_true', help='Force reindex all files')
    reindex_parser.add_argument('--dir', default=None, help='Base directory to scan (default: quaid home)')
    
    # Search command — defaults from config
    _rag_cfg = _rag_config()
    _default_limit = _rag_cfg.search_limit if _rag_cfg else 5
    _default_min_sim = _rag_cfg.min_similarity if _rag_cfg else 0.3
    search_parser = subparsers.add_parser('search', help='Search documentation')
    search_parser.add_argument('query', help='Search query')
    search_parser.add_argument('--limit', type=int, default=_default_limit, help='Max results')
    search_parser.add_argument('--min-similarity', type=float, default=_default_min_sim, help='Minimum similarity threshold')
    search_parser.add_argument('--project', help='Filter results by project name')
    search_parser.add_argument('--docs', help='Comma-separated doc path/name filters')
    
    # Stats command
    stats_parser = subparsers.add_parser('stats', help='Show indexing statistics')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    rag = DocsRAG()
    
    if args.command == 'reindex':
        # Index both docs/ and workspace root .md files
        base_dir = args.dir or str(_workspace())
        docs_dir = os.path.join(base_dir, 'docs')
        
        print("=== Indexing docs/ directory ===")
        docs_result = rag.reindex_all(docs_dir, force=args.all)
        
        print("\n=== Indexing workspace .md files ===")
        workspace_files = [f for f in Path(base_dir).glob('*.md') if f.is_file() and not rag._is_context_file(f)]
        workspace_chunks = 0
        workspace_indexed = 0

        for md_file in workspace_files:
            file_path = str(md_file.absolute())
            if not args.all and not rag.needs_reindex(file_path):
                continue
            chunks = rag.index_document(file_path)
            if chunks > 0:
                workspace_indexed += 1
                workspace_chunks += chunks

        print(f"Workspace indexing: {workspace_indexed} files indexed, {workspace_chunks} chunks")

        print("\n=== Indexing doc_registry entries ===")
        registry_chunks = 0
        registry_indexed = 0
        try:
            from datastore.docsdb.registry import DocsRegistry
            reg = DocsRegistry()
            reg_docs = reg.list_docs()
            for doc in reg_docs:
                raw_path = doc.get("file_path", "")
                if not raw_path:
                    continue
                p = Path(raw_path)
                if not p.is_absolute():
                    p = _workspace() / p
                file_path = str(p.absolute())
                if not Path(file_path).is_file():
                    continue
                if not args.all and not rag.needs_reindex(file_path):
                    continue
                chunks = rag.index_document(file_path)
                if chunks > 0:
                    registry_indexed += 1
                    registry_chunks += chunks
        except Exception as e:
            print(f"Warning: could not index doc_registry entries: {e}")
        print(f"Doc registry indexing: {registry_indexed} files indexed, {registry_chunks} chunks")

        total_files = docs_result['indexed_files'] + workspace_indexed + registry_indexed
        total_chunks = docs_result['total_chunks'] + workspace_chunks + registry_chunks
        print(f"\nTotal: {total_files} files, {total_chunks} chunks")
    
    elif args.command == 'search':
        docs_filter = []
        if getattr(args, "docs", None):
            docs_filter = [d.strip() for d in str(args.docs).split(",") if d.strip()]
        results = rag.search_docs(
            args.query,
            limit=args.limit,
            min_similarity=args.min_similarity,
            project=getattr(args, 'project', None),
            docs=docs_filter,
        )
        
        if not results:
            print("No results found")
            return
        
        print(f"Found {len(results)} results for '{args.query}':\n")
        
        for i, result in enumerate(results, 1):
            source_short = result["source"].replace(str(_workspace()) + "/", "~/")
            header = result.get("section_header", "")
            header_str = f" > {header}" if header else ""
            
            print(f"{i}. {source_short}{header_str} (similarity: {result['similarity']})")
            
            # Print full chunk content (no truncation) so callers can consume complete context.
            content_lines = result["content"].split('\n')
            for line in content_lines:
                print(f"   {line}")
            print()
    
    elif args.command == 'stats':
        stats = rag.stats()
        print(f"Documentation Index Statistics:")
        print(f"  Total files: {stats['total_files']}")
        print(f"  Total chunks: {stats['total_chunks']}")
        print(f"  Last indexed: {stats['last_indexed'] or 'Never'}")
        print(f"\nBy category:")
        for category, data in stats['by_category'].items():
            print(f"  {category}: {data['files']} files, {data['chunks']} chunks")


if __name__ == "__main__":
    main()
