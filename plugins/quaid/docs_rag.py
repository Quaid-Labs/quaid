#!/usr/bin/env python3
"""
RAG for Technical Documentation
Smart chunking, indexing, and semantic search of project documentation.
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from lib.config import get_db_path
from lib.database import get_connection as _lib_get_connection
from lib.embeddings import get_embedding as _lib_get_embedding, pack_embedding as _lib_pack_embedding, unpack_embedding as _lib_unpack_embedding
from lib.similarity import cosine_similarity as _lib_cosine_similarity

# Configuration — resolved from config system
DB_PATH = get_db_path()
def _workspace() -> Path:
    from lib.adapter import get_adapter
    return get_adapter().quaid_home()

WORKSPACE = None  # Lazy — use _workspace() instead


def _rag_config():
    """Get RAG config section (lazy import to avoid circular deps)."""
    try:
        from config import get_config
        return get_config().rag
    except Exception:
        return None


def _chunk_max_tokens() -> int:
    cfg = _rag_config()
    return cfg.chunk_max_tokens if cfg else 800


def _chunk_overlap_tokens() -> int:
    cfg = _rag_config()
    return cfg.chunk_overlap_tokens if cfg else 100


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
        current_header = None
        current_tokens = 0
        
        for line in lines:
            line_tokens = self.estimate_tokens(line)
            
            # Check for header
            header_match = re.match(r'^(#{1,3})\s+(.+)', line)
            if header_match:
                # Save current chunk if it has content
                if current_chunk_lines:
                    chunks.append('\n'.join(current_chunk_lines))
                    current_chunk_lines = []
                    current_tokens = 0
                
                # Update current header
                current_header = line
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
            print(f"Error checking if {file_path} needs reindex: {e}")
            return True  # When in doubt, reindex

    def index_document(self, file_path: str) -> int:
        """Index a single document, returning number of chunks created."""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            print(f"Error reading {file_path}: {e}")
            return 0
        
        # Chunk the content
        chunk_texts = self.chunk_markdown(content)
        if not chunk_texts:
            print(f"No chunks generated for {file_path}")
            return 0

        # Collect all embeddings BEFORE deleting old chunks.
        # This prevents data loss if Ollama is down or embedding fails.
        prepared_chunks = []
        for i, chunk_text in enumerate(chunk_texts):
            section_header = self._extract_section_header(chunk_text)
            embedding = _lib_get_embedding(chunk_text)
            if not embedding:
                print(f"Failed to get embedding for chunk {i} in {file_path}, aborting reindex to preserve old chunks")
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
            print(f"All embeddings failed for {file_path}, keeping old chunks")
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
        
        print(f"[docs] Indexed {chunks_created} chunks from {file_path}")

        # Sync indexed timestamp to registry
        if chunks_created > 0:
            try:
                from docs_registry import DocsRegistry
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

    def scan_docs_directory(self, docs_dir: str) -> List[str]:
        """Recursively find all .md files in directory."""
        md_files = []
        docs_path = Path(docs_dir)
        
        if not docs_path.exists():
            print(f"Directory does not exist: {docs_dir}")
            return []
        
        for md_file in docs_path.rglob('*.md'):
            md_files.append(str(md_file.absolute()))
        
        return sorted(md_files)

    def reindex_all(self, docs_dir: str, force: bool = False) -> Dict[str, int]:
        """Scan and index all documentation, optionally forcing full reindex."""
        print(f"Scanning for .md files in {docs_dir}")
        md_files = self.scan_docs_directory(docs_dir)
        
        if not md_files:
            print(f"No .md files found in {docs_dir}")
            return {"total_files": 0, "indexed_files": 0, "total_chunks": 0}
        
        print(f"Found {len(md_files)} .md files")
        
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

    def search_docs(self, query: str, limit: int = 5, min_similarity: float = 0.3,
                    project: Optional[str] = None) -> List[Dict]:
        """Semantic search of document chunks.

        Args:
            project: If set, only return results from files belonging to this project.
                     Uses doc_registry + project homeDir for filtering.
        """
        query_embedding = _lib_get_embedding(query)
        if not query_embedding:
            print("Failed to get embedding for query", file=sys.stderr)
            return []

        # Build project filter — use SQL-level filtering to avoid full scan
        project_paths = None
        registry_paths = []
        if project:
            project_paths = self._get_project_paths(project)
            # Also get registered external file paths from doc_registry
            try:
                from docs_registry import DocsRegistry
                registry = DocsRegistry()
                reg_docs = registry.list_docs(project=project)
                registry_paths = [str(_workspace() / d["file_path"]) for d in reg_docs]
            except Exception:
                pass

        results = []
        with _lib_get_connection(self.db_path) as conn:
            # Filter at SQL level when project is specified (avoids loading all chunks)
            if project and (project_paths or registry_paths):
                like_clauses = []
                params = []
                if project_paths and project_paths["home_dir"]:
                    like_clauses.append("source_file LIKE ?")
                    params.append(project_paths["home_dir"] + "%")
                if project_paths:
                    for root in project_paths.get("source_roots", []):
                        like_clauses.append("source_file LIKE ?")
                        params.append(root + "%")
                # Include registered external files by exact path prefix
                for rp in registry_paths:
                    like_clauses.append("source_file LIKE ?")
                    params.append(rp + "%")
                if like_clauses:
                    where = " OR ".join(like_clauses)
                    rows = conn.execute(f"SELECT * FROM doc_chunks WHERE ({where})", params).fetchall()
                else:
                    rows = []  # No matching paths for this project
            else:
                rows = conn.execute("SELECT * FROM doc_chunks").fetchall()

            for row in rows:
                chunk_embedding = _lib_unpack_embedding(row[5])  # embedding is column 5
                similarity = _lib_cosine_similarity(query_embedding, chunk_embedding)

                if similarity >= min_similarity:
                    results.append({
                        "content": row[3],  # content
                        "source": row[1],   # source_file
                        "section_header": row[4],  # section_header
                        "similarity": round(similarity, 3),
                        "chunk_index": row[2]  # chunk_index
                    })

        # Sort by similarity and limit
        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results[:limit]

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
        workspace_files = [f for f in Path(base_dir).glob('*.md') if f.is_file()]
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
        print(f"\nTotal: {docs_result['indexed_files'] + workspace_indexed} files, {docs_result['total_chunks'] + workspace_chunks} chunks")
    
    elif args.command == 'search':
        results = rag.search_docs(args.query, limit=args.limit, min_similarity=args.min_similarity,
                                   project=getattr(args, 'project', None))
        
        if not results:
            print("No results found")
            return
        
        print(f"Found {len(results)} results for '{args.query}':\n")
        
        for i, result in enumerate(results, 1):
            source_short = result["source"].replace(str(_workspace()) + "/", "~/")
            header = result.get("section_header", "")
            header_str = f" > {header}" if header else ""
            
            print(f"{i}. {source_short}{header_str} (similarity: {result['similarity']})")
            
            # Show first few lines of content
            content_lines = result["content"].split('\n')
            preview_lines = [line for line in content_lines[:4] if line.strip()]
            for line in preview_lines:
                print(f"   {line[:80]}{'...' if len(line) > 80 else ''}")
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