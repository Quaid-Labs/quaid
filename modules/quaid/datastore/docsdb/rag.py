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

from lib.config import get_db_path
from lib.database import get_connection as _lib_get_connection
from lib.embeddings import get_embedding as _lib_get_embedding, pack_embedding as _lib_pack_embedding, unpack_embedding as _lib_unpack_embedding
from lib.similarity import cosine_similarity as _lib_cosine_similarity
from lib.fail_policy import is_fail_hard_enabled
from lib.runtime_context import get_workspace_dir

logger = logging.getLogger(__name__)

# Configuration — resolved from config system
DB_PATH = get_db_path()
def _workspace() -> Path:
    return get_workspace_dir()

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
        self.db_path = db_path or DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

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
        
    def estimate_tokens(self, text: str) -> int:
        """Rough token estimation (4 chars ≈ 1 token)."""
        return len(text) // 4

    def chunk_markdown(self, content: str, max_tokens: int = None) -> List[DocumentChunk]:
        """Smart chunking of markdown content at headers/paragraphs."""
        if max_tokens is None:
            max_tokens = _chunk_max_tokens()
        lines = content.split('\n')
        chunks = []
        current_chunk_lines = []
        current_tokens = 0

        for line in lines:
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
                    overlap_start = max(0, split_point - _chunk_overlap_tokens() // 10)
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

    def needs_reindex(self, file_path: str) -> bool:
        """Check if file needs reindexing based on modification time."""
        try:
            stat = os.stat(file_path)
            # Use UTC to match SQLite datetime('now') which is always UTC
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
            
            with _lib_get_connection(self.db_path) as conn:
                result = conn.execute(
                    "SELECT updated_at FROM doc_chunks WHERE source_file = ? LIMIT 1",
                    (file_path,)
                ).fetchone()
                
                if not result:
                    return True  # File not indexed yet
                
                indexed_time = datetime.fromisoformat(result[0]).replace(tzinfo=timezone.utc)
                file_time = datetime.fromisoformat(mtime)

                return file_time > indexed_time
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
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            logger.warning("Error reading %s: %s", file_path, e)
            return 0

        # Add temporal context for archived log files
        if self._is_archive_log(file_path):
            content = self._archive_temporal_header(file_path) + content

        # Chunk the content
        chunk_texts = self.chunk_markdown(content)
        if not chunk_texts:
            logger.info("No chunks generated for %s", file_path)
            return 0

        # Collect all embeddings BEFORE deleting old chunks.
        # This prevents data loss if Ollama is down or embedding fails.
        prepared_chunks = []
        for i, chunk_text in enumerate(chunk_texts):
            section_header = self._extract_section_header(chunk_text)
            embedding = _lib_get_embedding(chunk_text)
            if not embedding:
                logger.warning(
                    "Failed embedding for chunk %s in %s; aborting reindex to preserve old chunks",
                    i,
                    file_path,
                )
                return 0
            prepared_chunks.append((
                f"{file_path}:{i}",
                file_path,
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
            conn.execute("DELETE FROM doc_chunks WHERE source_file = ?", (file_path,))
            for chunk_data in prepared_chunks:
                conn.execute("""
                    INSERT INTO doc_chunks
                    (id, source_file, chunk_index, content, section_header, embedding, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                """, chunk_data)
                chunks_created += 1
        
        logger.info("[docs] Indexed %s chunks from %s", chunks_created, file_path)

        # Sync indexed timestamp to registry
        if chunks_created > 0:
            try:
                from datastore.docsdb.registry import DocsRegistry
                registry = DocsRegistry(self.db_path)
                now = datetime.now().isoformat()
                # Try both absolute and relative paths
                registry.update_timestamps(file_path, indexed_at=now)
                try:
                    rel = str(Path(file_path).relative_to(_workspace()))
                    registry.update_timestamps(rel, indexed_at=now)
                except ValueError:
                    pass
            except Exception:
                pass  # Registry sync is best-effort

        return chunks_created

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
    _CONTEXT_FILES = frozenset({"SOUL.md", "USER.md", "MEMORY.md", "AGENTS.md", "TOOLS.md"})

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
        
        for file_path in md_files:
            if not force and not self.needs_reindex(file_path):
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
            from config import get_config

            cfg = get_config()
            workspace = _workspace().resolve()
            best_match = None
            best_prefix_len = -1

            for project_name, defn in (cfg.projects.definitions or {}).items():
                candidate_roots = []
                if getattr(defn, "home_dir", None):
                    candidate_roots.append((workspace / defn.home_dir).resolve())
                for root in getattr(defn, "source_roots", []) or []:
                    candidate_roots.append((workspace / root).resolve())

                for candidate in candidate_roots:
                    prefix = str(candidate)
                    if str(source_path) == prefix or str(source_path).startswith(prefix + os.sep):
                        if len(prefix) > best_prefix_len:
                            best_prefix_len = len(prefix)
                            best_match = project_name

            if best_match:
                return best_match

            try:
                from datastore.docsdb.registry import DocsRegistry

                registry = DocsRegistry()
                for project_name in (cfg.projects.definitions or {}).keys():
                    for doc in registry.list_docs(project=project_name):
                        raw_path = str(doc.get("file_path") or "").strip()
                        if not raw_path:
                            continue
                        p = Path(raw_path)
                        if not p.is_absolute():
                            p = workspace / p
                        try:
                            if p.resolve() == source_path:
                                return project_name
                        except Exception:
                            if str(p) == str(source_path):
                                return project_name
            except Exception:
                pass
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
            from config import get_config

            cfg = get_config()
            defn = cfg.projects.definitions.get(project)
            if defn and defn.home_dir:
                md_path = _workspace() / defn.home_dir / "PROJECT.md"
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
        query_embedding = _lib_get_embedding(query)
        if not query_embedding:
            if is_fail_hard_enabled():
                raise RuntimeError(
                    "Doc RAG embedding failed while failHard is enabled; "
                    "no degraded docs fallback allowed."
                )
            logger.warning("Failed to get embedding for query; returning no RAG results")
            return []

        # Build project filter — use SQL-level filtering to avoid full scan
        project_paths = None
        registry_paths = []
        if project:
            project_paths = self._get_project_paths(project)
            # Also get registered external file paths from doc_registry
            try:
                from datastore.docsdb.registry import DocsRegistry
                registry = DocsRegistry()
                reg_docs = registry.list_docs(project=project)
                registry_paths = [str(_workspace() / d["file_path"]) for d in reg_docs]
            except Exception:
                pass

        doc_filters = self._normalize_docs_filter(docs)

        results = []
        with _lib_get_connection(self.db_path) as conn:
            # Filter at SQL level when project is specified (avoids loading all chunks)
            like_clauses = []
            params = []
            if project and (project_paths or registry_paths):
                params = []
                if project_paths and project_paths["home_dir"]:
                    like_clauses.append("source_file LIKE ? ESCAPE '\\'")
                    params.append(_escape_like(project_paths["home_dir"]) + "%")
                if project_paths:
                    for root in project_paths.get("source_roots", []):
                        like_clauses.append("source_file LIKE ? ESCAPE '\\'")
                        params.append(_escape_like(root) + "%")
                # Include registered external files by exact path prefix
                for rp in registry_paths:
                    like_clauses.append("source_file LIKE ? ESCAPE '\\'")
                    params.append(_escape_like(rp) + "%")
                if like_clauses:
                    like_clauses = [f"({ ' OR '.join(like_clauses) })"]
                else:
                    rows = []  # No matching paths for this project
                    if not doc_filters:
                        return rows

            # Optional explicit doc filter set
            if doc_filters:
                doc_like = []
                for df in doc_filters:
                    doc_like.append("source_file LIKE ? ESCAPE '\\'")
                    params.append(f"%{_escape_like(df)}%")
                if doc_like:
                    like_clauses.append(f"({ ' OR '.join(doc_like) })")

            if like_clauses:
                where = " AND ".join(like_clauses)
                rows = conn.execute(f"SELECT * FROM doc_chunks WHERE {where}", params).fetchall()
            else:
                rows = conn.execute("SELECT * FROM doc_chunks").fetchall()

            for row in rows:
                chunk_embedding = _lib_unpack_embedding(row[5])  # embedding is column 5
                similarity = _lib_cosine_similarity(query_embedding, chunk_embedding)

                if similarity >= min_similarity:
                    inferred_project = project or self.infer_project_for_source(row[1])
                    results.append({
                        "content": row[3],  # content
                        "source": row[1],   # source_file
                        "section_header": row[4],  # section_header
                        "similarity": round(similarity, 3),
                        "chunk_index": row[2],  # chunk_index
                        "project": inferred_project,
                    })

        # Sort by similarity and limit
        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results[:limit]

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
        if _docs_recall_telemetry_enabled():
            bundle["telemetry"] = {
                "query": query,
                "requested_project": project,
                "resolved_project": inferred_project,
                "requested_docs": list(docs or []),
                "chunk_count": len(chunks),
                "project_md_attached": bool(bundle["project_md"]),
                "sources": [chunk.get("source") for chunk in chunks[:5]],
                "top_similarity": chunks[0].get("similarity") if chunks else None,
            }
        return bundle

    def _get_project_paths(self, project: str) -> dict:
        """Get project path info for filtering."""
        try:
            from config import get_config
            cfg = get_config()
            defn = cfg.projects.definitions.get(project)
            if defn:
                return {
                    "home_dir": str(_workspace() / defn.home_dir),
                    "source_roots": [str(_workspace() / r) for r in defn.source_roots],
                }
        except Exception:
            pass
        return {"home_dir": "", "source_roots": []}

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
                    proj_dir = workspace / proj_defn.home_dir
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
                for doc in reg_docs:
                    raw_path = doc.get("file_path", "")
                    if not raw_path:
                        continue
                    p = Path(raw_path)
                    if not p.is_absolute():
                        p = runtime_workspace / p
                    if not p.is_file():
                        continue
                    if rag.needs_reindex(str(p)):
                        doc_chunks = rag.index_document(str(p))
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
