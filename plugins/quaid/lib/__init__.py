"""Shared library for quaid plugin."""

from .config import get_db_path, get_archive_db_path, get_ollama_url, get_embedding_model, get_embedding_dim
from .database import get_connection
from .embeddings import get_embedding, pack_embedding, unpack_embedding
from .similarity import cosine_similarity
from .tokens import extract_key_tokens, texts_are_near_identical, STOPWORDS

__all__ = [
    # Config
    "get_db_path",
    "get_archive_db_path",
    "get_ollama_url",
    "get_embedding_model",
    "get_embedding_dim",
    # Database
    "get_connection",
    # Embeddings
    "get_embedding",
    "pack_embedding",
    "unpack_embedding",
    # Similarity
    "cosine_similarity",
    # Tokens
    "extract_key_tokens",
    "texts_are_near_identical",
    "STOPWORDS",
]
