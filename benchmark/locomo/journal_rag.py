"""RAG index over journal archive entries for selective retrieval.

Instead of dumping entire journal files into context, this module enables
semantic search over journal archives to retrieve only the most relevant entries.
"""

import sys
from pathlib import Path
from typing import List, Tuple, Optional

# Add quaid plugin to path for shared libraries
QUAID_PATH = Path(__file__).resolve().parents[4] / "plugins" / "quaid"
if str(QUAID_PATH) not in sys.path:
    sys.path.insert(0, str(QUAID_PATH))

from lib.embeddings import get_embedding
from lib.similarity import cosine_similarity


class JournalChunk:
    """A single journal entry chunk with metadata."""

    def __init__(self, text: str, header: str, file_path: str):
        """Initialize a journal chunk.

        Args:
            text: The full chunk text (header + content)
            header: The markdown header (e.g., "## 2023-03-15 — Session 6")
            file_path: Source file path for debugging
        """
        self.text = text
        self.header = header
        self.file_path = file_path
        self.embedding: Optional[List[float]] = None

    def __repr__(self) -> str:
        return f"JournalChunk({self.header}, {len(self.text)} chars)"


class JournalRAG:
    """RAG index over journal archive entries for selective retrieval."""

    def __init__(self, conv_dir: Path, verbose: bool = False):
        """Build index from journal files in conv_dir/journal/archive/*.md

        Args:
            conv_dir: Conversation directory containing journal/ subdirectory
            verbose: Print indexing progress
        """
        self.conv_dir = conv_dir
        self.verbose = verbose
        self.chunks: List[JournalChunk] = []
        self._build_index()

    def _build_index(self) -> None:
        """Load and index all journal archive files."""
        journal_dir = self.conv_dir / "journal"
        if not journal_dir.exists():
            if self.verbose:
                print(f"No journal directory found at {journal_dir}")
            return

        # Index both archive and current journal files
        archive_dir = journal_dir / "archive"
        archive_files = list(archive_dir.glob("*.md")) if archive_dir.exists() else []
        current_files = list(journal_dir.glob("*.journal.md"))

        all_files = archive_files + current_files
        if not all_files:
            if self.verbose:
                print(f"No journal files found in {journal_dir}")
            return

        if self.verbose:
            print(f"Indexing {len(all_files)} journal files...")

        for file_path in all_files:
            self._index_file(file_path)

        if self.verbose:
            print(f"Indexed {len(self.chunks)} journal chunks")

    def _index_file(self, file_path: Path) -> None:
        """Parse and index a single journal file."""
        try:
            content = file_path.read_text()
        except Exception as e:
            if self.verbose:
                print(f"Error reading {file_path}: {e}")
            return

        # Split by ## headers (each header = one distilled entry)
        lines = content.split('\n')
        current_chunk = []
        current_header = ""

        for line in lines:
            if line.startswith('## '):
                # Save previous chunk if it exists
                if current_chunk and current_header:
                    self._add_chunk(current_chunk, current_header, file_path)
                # Start new chunk
                current_header = line
                current_chunk = [line]
            elif current_chunk:
                current_chunk.append(line)

        # Save final chunk
        if current_chunk and current_header:
            self._add_chunk(current_chunk, current_header, file_path)

    def _add_chunk(self, lines: List[str], header: str, file_path: Path) -> None:
        """Add a chunk to the index if it meets minimum length."""
        text = '\n'.join(lines).strip()
        if len(text) < 20:  # Skip very short chunks
            return

        chunk = JournalChunk(text, header, str(file_path))

        # Get embedding
        embedding = get_embedding(text)
        if embedding is None:
            if self.verbose:
                print(f"Failed to embed chunk: {header}")
            return

        chunk.embedding = embedding
        self.chunks.append(chunk)

    def search(self, query: str, top_k: int = 5) -> str:
        """Search journal index, return formatted context string.

        Args:
            query: Search query
            top_k: Number of top results to return

        Returns:
            Formatted markdown string with top-k journal entries
        """
        if not self.chunks:
            return ""

        # Embed query
        query_embedding = get_embedding(query)
        if query_embedding is None:
            if self.verbose:
                print("Failed to embed query")
            return ""

        # Calculate similarities
        scores: List[Tuple[float, JournalChunk]] = []
        for chunk in self.chunks:
            if chunk.embedding is None:
                continue
            score = cosine_similarity(query_embedding, chunk.embedding)
            scores.append((score, chunk))

        # Sort by score descending
        scores.sort(reverse=True, key=lambda x: x[0])

        # Take top-k
        top_chunks = scores[:top_k]

        if not top_chunks:
            return ""

        # Format as markdown
        lines = ["## Journal Context (most relevant entries)"]
        for score, chunk in top_chunks:
            # Extract header text (remove leading ##)
            header_text = chunk.header.lstrip('#').strip()
            # Add the header and content (skip the header line since we're adding it)
            content_lines = chunk.text.split('\n')[1:]  # Skip first line (header)
            content = '\n'.join(content_lines).strip()

            lines.append(f"### {header_text}")
            lines.append(content)
            lines.append("")  # Blank line between entries

        return '\n'.join(lines).strip()

    def count_tokens(self, text: str) -> int:
        """Rough token count estimate (4 chars per token)."""
        return len(text) // 4

    def search_with_budget(self, query: str, max_tokens: int = 1000) -> str:
        """Search and return results within token budget.

        Args:
            query: Search query
            max_tokens: Maximum token budget for results

        Returns:
            Formatted markdown string with results fitting within budget
        """
        # Start with top-k=10, progressively reduce if over budget
        for k in [10, 7, 5, 3, 1]:
            result = self.search(query, top_k=k)
            if self.count_tokens(result) <= max_tokens:
                return result

        # If even 1 result is too large, return empty
        return ""
