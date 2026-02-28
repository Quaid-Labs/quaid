"""Shared domain-registry helpers (DB-authoritative)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict

from datastore.memorydb.domain_defaults import default_domain_descriptions
from lib.database import get_connection
from lib.domain_text import (
    MAX_DOMAIN_DESCRIPTION_CHARS,
    normalize_domain_id,
    sanitize_domain_description,
)

logger = logging.getLogger(__name__)


def normalize_domain_map(raw: Dict[str, str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, value in (raw or {}).items():
        norm_key = normalize_domain_id(key)
        if not norm_key:
            continue
        out[norm_key] = sanitize_domain_description(value)
    return out


def ensure_domain_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS domain_registry (
            domain TEXT PRIMARY KEY,
            description TEXT DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS node_domains (
            node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
            domain TEXT NOT NULL REFERENCES domain_registry(domain) ON DELETE RESTRICT,
            created_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (node_id, domain)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_node_domains_domain_node ON node_domains(domain, node_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_node_domains_node_domain ON node_domains(node_id, domain)")


def read_active_domains(conn) -> Dict[str, str]:
    rows = conn.execute(
        "SELECT domain, description FROM domain_registry WHERE active = 1 ORDER BY domain"
    ).fetchall()
    out: Dict[str, str] = {}
    for row in rows:
        domain_id = normalize_domain_id(row[0])
        if not domain_id:
            continue
        out[domain_id] = sanitize_domain_description(row[1])
    return normalize_domain_map(out)


def bootstrap_default_domains(conn) -> Dict[str, str]:
    defaults = default_domain_descriptions()
    for domain_id, description in defaults.items():
        conn.execute(
            """
            INSERT INTO domain_registry(domain, description, active)
            VALUES (?, ?, 1)
            ON CONFLICT(domain) DO UPDATE SET
              description = COALESCE(NULLIF(domain_registry.description, ''), excluded.description),
              active = 1,
              updated_at = datetime('now')
            """,
            (domain_id, description),
        )
    return defaults


def load_active_domains(
    db_path: Path,
    *,
    bootstrap_if_empty: bool = True,
) -> Dict[str, str]:
    with get_connection(db_path) as conn:
        ensure_domain_tables(conn)
        active = read_active_domains(conn)
        if active:
            return active
        if bootstrap_if_empty:
            return bootstrap_default_domains(conn)
        return {}


def safe_load_active_domains(
    db_path: Path,
    *,
    bootstrap_if_empty: bool = True,
) -> Dict[str, str]:
    try:
        return load_active_domains(db_path, bootstrap_if_empty=bootstrap_if_empty)
    except Exception as exc:
        logger.warning("Failed loading active domains from %s: %s", db_path, exc)
        return {}


def apply_domain_set(
    conn,
    domains: Dict[str, str],
    *,
    deactivate_others: bool,
) -> Dict[str, str]:
    norm = normalize_domain_map(domains)
    for domain_id, description in norm.items():
        conn.execute(
            """
            INSERT INTO domain_registry(domain, description, active)
            VALUES (?, ?, 1)
            ON CONFLICT(domain) DO UPDATE SET
              description = excluded.description,
              active = 1,
              updated_at = datetime('now')
            """,
            (domain_id, description),
        )
    if deactivate_others and norm:
        placeholders = ",".join("?" for _ in norm)
        conn.execute(
            f"UPDATE domain_registry SET active = 0, updated_at = datetime('now') WHERE domain NOT IN ({placeholders})",
            tuple(norm.keys()),
        )
    return norm
