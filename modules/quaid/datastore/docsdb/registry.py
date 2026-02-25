#!/usr/bin/env python3
"""
Document Registry — CRUD, path resolution, and project management.

Tracks documents across projects, maps file paths to projects,
and provides the foundation for the project system.

Usage:
  python3 docs_registry.py register <file_path> --project <name> [--title ...]
  python3 docs_registry.py list [--project <name>] [--type <type>]
  python3 docs_registry.py read <identifier>
  python3 docs_registry.py unregister <file_path>
  python3 docs_registry.py find-project <file_path>
  python3 docs_registry.py create-project <name> [--label ...] [--source-roots ...]
  python3 docs_registry.py rename-project <old> <new>
  python3 docs_registry.py archive-project <name> [--yes]
  python3 docs_registry.py delete-project <name> [--yes]
  python3 docs_registry.py move-file <path> --to-project <name>
  python3 docs_registry.py verify --project <name> [--json]
  python3 docs_registry.py discover --project <name>
  python3 docs_registry.py sync-external --project <name>
  python3 docs_registry.py sync
  python3 docs_registry.py source-mappings [--project <name>]
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from lib.config import get_db_path
from lib.database import get_connection
from lib.runtime_context import get_workspace_dir

def _workspace() -> Path:
    return get_workspace_dir()

# Strict project name validation — prevents path traversal
_PROJECT_NAME_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]*$')


def _validate_project_name(name: str) -> None:
    """Validate project name is safe for filesystem use."""
    if not name or not _PROJECT_NAME_RE.match(name):
        raise ValueError(
            f"Invalid project name: '{name}'. "
            "Use alphanumeric characters, hyphens, and underscores only. "
            "Must start with a letter or digit."
        )


def _validate_inside_workspace(resolved_path: Path, label: str = "path") -> None:
    """Validate that a resolved path is inside the workspace."""
    try:
        real = resolved_path.resolve()
        workspace_real = _workspace().resolve()
        if not str(real).startswith(str(workspace_real) + "/") and real != workspace_real:
            raise ValueError(f"Refusing {label} outside workspace: {real}")
    except (OSError, ValueError) as e:
        raise ValueError(f"Invalid {label}: {e}")


def _get_default_db_path() -> Path:
    """Get DB path lazily (respects env vars set after import)."""
    return get_db_path()

# PROJECT.md template
PROJECT_MD_TEMPLATE = """# Project: {label}

## Overview
{description}

## Files & Assets

### In This Directory
<!-- Auto-discovered — all files in this directory belong to this project -->

### External Files
| File | Purpose | Auto-Update |
|------|---------|-------------|

## Documents
| Document | Tracks | Auto-Update |
|----------|--------|-------------|

## Related Projects

## Update Rules

## Exclude
{exclude_lines}
"""


class DocsRegistry:
    """Document registry with CRUD, path resolution, and project operations."""

    def __init__(self, db_path: Path = None):
        self.db_path = db_path or _get_default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._config = None
        self.ensure_table()

    def _get_config(self):
        """Lazy-load config to avoid circular imports."""
        if self._config is None:
            from config import get_config
            self._config = get_config()
        return self._config

    def ensure_table(self):
        """Create doc_registry table if it doesn't exist."""
        def _ensure_column(conn, table: str, column: str, definition: str) -> None:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

        with get_connection(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS doc_registry (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT NOT NULL UNIQUE,
                    project TEXT NOT NULL DEFAULT 'default',
                    asset_type TEXT NOT NULL DEFAULT 'doc',
                    title TEXT,
                    description TEXT,
                    tags TEXT DEFAULT '[]',
                    state TEXT NOT NULL DEFAULT 'active',
                    auto_update INTEGER DEFAULT 0,
                    source_files TEXT,
                    last_indexed_at TEXT,
                    last_modified_at TEXT,
                    registered_at TEXT NOT NULL DEFAULT (datetime('now')),
                    registered_by TEXT DEFAULT 'system'
                )
            """)
            # Forward-compatible identity/source scope context (additive only).
            _ensure_column(conn, "doc_registry", "source_channel", "TEXT")
            _ensure_column(conn, "doc_registry", "source_conversation_id", "TEXT")
            _ensure_column(conn, "doc_registry", "source_author_id", "TEXT")
            _ensure_column(conn, "doc_registry", "speaker_entity_id", "TEXT")
            _ensure_column(conn, "doc_registry", "subject_entity_id", "TEXT")
            _ensure_column(conn, "doc_registry", "conversation_id", "TEXT")
            _ensure_column(conn, "doc_registry", "visibility_scope", "TEXT DEFAULT 'source_shared'")
            _ensure_column(conn, "doc_registry", "sensitivity", "TEXT DEFAULT 'normal'")
            _ensure_column(conn, "doc_registry", "participant_entity_ids", "TEXT DEFAULT '[]'")
            _ensure_column(conn, "doc_registry", "provenance_confidence", "REAL")
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_doc_registry_project
                ON doc_registry(project)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_doc_registry_state
                ON doc_registry(state)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_doc_registry_type
                ON doc_registry(asset_type)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_doc_registry_source_scope
                ON doc_registry(source_channel, source_conversation_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_doc_registry_subject_state
                ON doc_registry(subject_entity_id, state)
            """)

            # Project definitions table — DB is source of truth (replaces JSON)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS project_definitions (
                    name TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    home_dir TEXT NOT NULL,
                    source_roots TEXT DEFAULT '[]',
                    auto_index INTEGER DEFAULT 1,
                    patterns TEXT DEFAULT '["*.md"]',
                    exclude TEXT DEFAULT '["*.db","*.log","*.pyc","__pycache__/"]',
                    description TEXT DEFAULT '',
                    state TEXT DEFAULT 'active' CHECK(state IN ('active', 'archived', 'deleted')),
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            """)

        # Seed from JSON on first run (empty table)
        self._seed_projects_from_json()

    def _seed_projects_from_json(self):
        """Seed project_definitions table from config/memory.json on first run.
        Only seeds when the table is empty (avoids re-reading JSON on every instantiation).
        """
        with get_connection(self.db_path) as conn:
            # Skip if table already has rows (not first run)
            row = conn.execute("SELECT COUNT(*) FROM project_definitions").fetchone()
            if row[0] > 0:
                return

            config_path = _workspace() / "config" / "memory.json"
            if not config_path.exists():
                return

            try:
                config_data = json.loads(config_path.read_text())
                definitions = config_data.get("projects", {}).get("definitions", {})
                for name, proj_data in definitions.items():
                    conn.execute("""
                        INSERT OR IGNORE INTO project_definitions
                        (name, label, home_dir, source_roots, auto_index, patterns, exclude, description)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        name,
                        proj_data.get("label", ""),
                        proj_data.get("homeDir", ""),
                        json.dumps(proj_data.get("sourceRoots", [])),
                        1 if proj_data.get("autoIndex", True) else 0,
                        json.dumps(proj_data.get("patterns", ["*.md"])),
                        json.dumps(proj_data.get("exclude", ["*.db", "*.log", "*.pyc", "__pycache__/"])),
                        proj_data.get("description", ""),
                    ))
                if definitions:
                    print(f"[docs_registry] Seeded {len(definitions)} project definitions from config/memory.json", file=sys.stderr)
            except Exception as e:
                print(f"[docs_registry] Warning: failed to seed from JSON: {e}", file=sys.stderr)

    # ========================================================================
    # Project Definition CRUD (DB-backed)
    # ========================================================================

    def get_project_definition(self, name: str):
        """Load a single project definition from DB."""
        from config import ProjectDefinition
        with get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM project_definitions WHERE name = ? AND state = 'active'",
                (name,)
            ).fetchone()
            if not row:
                return None
            return ProjectDefinition(
                label=row["label"],
                home_dir=row["home_dir"],
                source_roots=json.loads(row["source_roots"]) if row["source_roots"] else [],
                auto_index=bool(row["auto_index"]),
                patterns=json.loads(row["patterns"]) if row["patterns"] else ["*.md"],
                exclude=json.loads(row["exclude"]) if row["exclude"] else [],
                description=row["description"] or "",
                state=row["state"],
            )

    def get_all_project_definitions(self):
        """Load all active project definitions from DB."""
        from config import ProjectDefinition
        with get_connection(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM project_definitions WHERE state = 'active'"
            ).fetchall()
            result = {}
            for row in rows:
                result[row["name"]] = ProjectDefinition(
                    label=row["label"],
                    home_dir=row["home_dir"],
                    source_roots=json.loads(row["source_roots"]) if row["source_roots"] else [],
                    auto_index=bool(row["auto_index"]),
                    patterns=json.loads(row["patterns"]) if row["patterns"] else ["*.md"],
                    exclude=json.loads(row["exclude"]) if row["exclude"] else [],
                    description=row["description"] or "",
                    state=row["state"],
                )
            return result

    def save_project_definition(self, name: str, defn):
        """Upsert a project definition to DB."""
        with get_connection(self.db_path) as conn:
            conn.execute("""
                INSERT INTO project_definitions
                    (name, label, home_dir, source_roots, auto_index, patterns, exclude, description, state, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(name) DO UPDATE SET
                    label = excluded.label,
                    home_dir = excluded.home_dir,
                    source_roots = excluded.source_roots,
                    auto_index = excluded.auto_index,
                    patterns = excluded.patterns,
                    exclude = excluded.exclude,
                    description = excluded.description,
                    state = excluded.state,
                    updated_at = datetime('now')
            """, (
                name,
                defn.label,
                defn.home_dir,
                json.dumps(defn.source_roots) if defn.source_roots else "[]",
                1 if defn.auto_index else 0,
                json.dumps(defn.patterns) if defn.patterns else '["*.md"]',
                json.dumps(defn.exclude) if defn.exclude else "[]",
                defn.description or "",
                getattr(defn, 'state', 'active'),
            ))

    def delete_project_definition(self, name: str):
        """Soft-delete a project definition (set state to 'deleted')."""
        with get_connection(self.db_path) as conn:
            conn.execute(
                "UPDATE project_definitions SET state = 'deleted', updated_at = datetime('now') WHERE name = ?",
                (name,)
            )

    # ========================================================================
    # CRUD
    # ========================================================================

    def register(
        self,
        file_path: str,
        project: str = "default",
        asset_type: str = "doc",
        title: Optional[str] = None,
        description: Optional[str] = None,
        tags: Optional[List[str]] = None,
        auto_update: bool = False,
        source_files: Optional[List[str]] = None,
        source_channel: Optional[str] = None,
        source_conversation_id: Optional[str] = None,
        source_author_id: Optional[str] = None,
        speaker_entity_id: Optional[str] = None,
        subject_entity_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        visibility_scope: Optional[str] = None,
        sensitivity: Optional[str] = None,
        participant_entity_ids: Optional[List[str]] = None,
        provenance_confidence: Optional[float] = None,
        registered_by: str = "system",
    ) -> int:
        """Register a document in the registry. Returns the row ID."""
        if not file_path or not file_path.strip():
            raise ValueError("file_path must be a non-empty string")
        with get_connection(self.db_path) as conn:
            conn.execute("""
                INSERT INTO doc_registry
                    (file_path, project, asset_type, title, description, tags,
                     auto_update, source_files, source_channel, source_conversation_id,
                     source_author_id, speaker_entity_id, subject_entity_id, conversation_id,
                     visibility_scope, sensitivity, participant_entity_ids,
                     provenance_confidence, registered_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_path) DO UPDATE SET
                    project = excluded.project,
                    asset_type = excluded.asset_type,
                    title = COALESCE(excluded.title, doc_registry.title),
                    description = COALESCE(excluded.description, doc_registry.description),
                    tags = excluded.tags,
                    auto_update = excluded.auto_update,
                    source_files = COALESCE(excluded.source_files, doc_registry.source_files),
                    source_channel = COALESCE(excluded.source_channel, doc_registry.source_channel),
                    source_conversation_id = COALESCE(excluded.source_conversation_id, doc_registry.source_conversation_id),
                    source_author_id = COALESCE(excluded.source_author_id, doc_registry.source_author_id),
                    speaker_entity_id = COALESCE(excluded.speaker_entity_id, doc_registry.speaker_entity_id),
                    subject_entity_id = COALESCE(excluded.subject_entity_id, doc_registry.subject_entity_id),
                    conversation_id = COALESCE(excluded.conversation_id, doc_registry.conversation_id),
                    visibility_scope = COALESCE(excluded.visibility_scope, doc_registry.visibility_scope),
                    sensitivity = COALESCE(excluded.sensitivity, doc_registry.sensitivity),
                    participant_entity_ids = COALESCE(excluded.participant_entity_ids, doc_registry.participant_entity_ids),
                    provenance_confidence = COALESCE(excluded.provenance_confidence, doc_registry.provenance_confidence),
                    state = 'active',
                    registered_by = excluded.registered_by
            """, (
                file_path,
                project,
                asset_type,
                title,
                description,
                json.dumps(tags or []),
                1 if auto_update else 0,
                json.dumps(source_files) if source_files else None,
                str(source_channel or "").strip().lower() or None,
                str(source_conversation_id or "").strip() or None,
                str(source_author_id or "").strip() or None,
                str(speaker_entity_id or "").strip() or None,
                str(subject_entity_id or "").strip() or None,
                str(conversation_id or "").strip() or None,
                str(visibility_scope or "").strip() or None,
                str(sensitivity or "").strip() or None,
                json.dumps(participant_entity_ids or []) if participant_entity_ids is not None else None,
                float(provenance_confidence) if provenance_confidence is not None else None,
                registered_by,
            ))
            row = conn.execute(
                "SELECT id FROM doc_registry WHERE file_path = ?",
                (file_path,)
            ).fetchone()
            return row[0] if row else 0

    def unregister(self, file_path: str) -> bool:
        """Soft-delete a document from the registry."""
        with get_connection(self.db_path) as conn:
            cursor = conn.execute("""
                UPDATE doc_registry SET state = 'deleted'
                WHERE file_path = ? AND state = 'active'
            """, (file_path,))
            return cursor.rowcount > 0

    def get(self, file_path: str) -> Optional[Dict[str, Any]]:
        """Get a registry entry by file path."""
        with get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM doc_registry WHERE file_path = ? AND state = 'active'",
                (file_path,)
            ).fetchone()
            if not row:
                return None
            return self._row_to_dict(row)

    def list_docs(
        self,
        project: Optional[str] = None,
        asset_type: Optional[str] = None,
        state: str = "active",
    ) -> List[Dict[str, Any]]:
        """List registry entries with optional filters."""
        query = "SELECT * FROM doc_registry WHERE state = ?"
        params: list = [state]

        if project:
            query += " AND project = ?"
            params.append(project)
        if asset_type:
            query += " AND asset_type = ?"
            params.append(asset_type)

        query += " ORDER BY project, file_path"

        with get_connection(self.db_path) as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def read(self, identifier: str) -> Optional[Dict[str, Any]]:
        """Read a document by file path or title. Returns entry + content."""
        with get_connection(self.db_path) as conn:
            # Try by path first
            row = conn.execute(
                "SELECT * FROM doc_registry WHERE file_path = ? AND state = 'active'",
                (identifier,)
            ).fetchone()
            if not row:
                # Try by exact title
                row = conn.execute(
                    "SELECT * FROM doc_registry WHERE title = ? AND state = 'active'",
                    (identifier,)
                ).fetchone()
            if not row:
                # Try by title substring (LIKE) — escape wildcards in user input
                safe_id = identifier.replace("%", "\\%").replace("_", "\\_")
                row = conn.execute(
                    "SELECT * FROM doc_registry WHERE title LIKE ? ESCAPE '\\' AND state = 'active' LIMIT 1",
                    (f"%{safe_id}%",)
                ).fetchone()
            if not row:
                # Try by file_path substring (slug or partial path)
                safe_id = identifier.replace("%", "\\%").replace("_", "\\_")
                row = conn.execute(
                    "SELECT * FROM doc_registry WHERE file_path LIKE ? ESCAPE '\\' AND state = 'active' LIMIT 1",
                    (f"%{safe_id}%",)
                ).fetchone()
            if not row:
                return None

            entry = self._row_to_dict(row)

            # Read file content
            abs_path = self._resolve_path(entry["file_path"])
            if abs_path.exists():
                try:
                    entry["content"] = abs_path.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError) as e:
                    entry["content"] = None
                    entry["content_error"] = f"Failed to read: {e}"
            else:
                entry["content"] = None

            return entry

    def update_metadata(self, file_path: str, **kwargs) -> bool:
        """Update metadata fields for a registry entry."""
        allowed = {"title", "description", "tags", "auto_update", "source_files",
                    "project", "asset_type", "source_channel", "source_conversation_id",
                    "source_author_id", "speaker_entity_id", "subject_entity_id",
                    "conversation_id", "visibility_scope", "sensitivity",
                    "participant_entity_ids", "provenance_confidence"}
        updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
        if not updates:
            return False

        # Serialize JSON fields
        if "tags" in updates:
            updates["tags"] = json.dumps(updates["tags"])
        if "source_files" in updates:
            updates["source_files"] = json.dumps(updates["source_files"])
        if "participant_entity_ids" in updates:
            updates["participant_entity_ids"] = json.dumps(updates["participant_entity_ids"])
        if "auto_update" in updates:
            updates["auto_update"] = 1 if updates["auto_update"] else 0

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [file_path]

        with get_connection(self.db_path) as conn:
            cursor = conn.execute(
                f"UPDATE doc_registry SET {set_clause} WHERE file_path = ? AND state = 'active'",
                params,
            )
            return cursor.rowcount > 0

    def update_timestamps(
        self,
        file_path: str,
        indexed_at: Optional[str] = None,
        modified_at: Optional[str] = None,
    ) -> bool:
        """Update timestamp fields for a registry entry."""
        updates = {}
        if indexed_at:
            updates["last_indexed_at"] = indexed_at
        if modified_at:
            updates["last_modified_at"] = modified_at
        if not updates:
            return False

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [file_path]

        with get_connection(self.db_path) as conn:
            cursor = conn.execute(
                f"UPDATE doc_registry SET {set_clause} WHERE file_path = ? AND state = 'active'",
                params,
            )
            return cursor.rowcount > 0

    # ========================================================================
    # Path Resolution
    # ========================================================================

    def find_project_for_path(self, file_path: str) -> Optional[str]:
        """Determine which project owns a given file path.

        Resolution order:
        1. Inside a project's homeDir (cheapest check)
        2. Registered in doc_registry (external files)
        3. Under a project's sourceRoots
        4. Tracked as source_file by a registered doc (reverse lookup)
        """
        cfg = self._get_config()
        abs_path = str(self._resolve_path(file_path))

        # 1. Inside a project's homeDir
        for name, defn in cfg.projects.definitions.items():
            home = str(self._resolve_path(defn.home_dir)).rstrip("/") + "/"
            if abs_path.startswith(home) or abs_path == home.rstrip("/"):
                return name

        # 2. Registered in doc_registry
        entry = self.get(file_path)
        if entry:
            return entry["project"]

        # 3. Under a project's sourceRoots
        for name, defn in cfg.projects.definitions.items():
            for root in defn.source_roots:
                root_abs = str(self._resolve_path(root)).rstrip("/") + "/"
                if abs_path.startswith(root_abs) or abs_path == root_abs.rstrip("/"):
                    return name

        # 4. Reverse lookup from source_files
        project = self.find_project_by_source_file(file_path)
        if project:
            return project

        return None

    def find_project_by_source_file(self, file_path: str) -> Optional[str]:
        """Find which project tracks a file as a source_file (reverse lookup)."""
        with get_connection(self.db_path) as conn:
            rows = conn.execute(
                "SELECT project, source_files FROM doc_registry WHERE state = 'active' AND source_files IS NOT NULL"
            ).fetchall()
            for row in rows:
                try:
                    sources = json.loads(row["source_files"] or "[]")
                    if not isinstance(sources, list):
                        continue
                    if file_path in sources:
                        return row["project"]
                except (json.JSONDecodeError, KeyError):
                    continue
        return None

    def get_source_mappings(self, project: Optional[str] = None) -> Dict[str, List[str]]:
        """Get doc->sources mapping for mtime staleness checks.

        Returns: {doc_path: [source_path, ...]}
        """
        query = """
            SELECT file_path, source_files FROM doc_registry
            WHERE state = 'active' AND auto_update = 1 AND source_files IS NOT NULL
        """
        params: list = []
        if project:
            query += " AND project = ?"
            params.append(project)

        result: Dict[str, List[str]] = {}
        with get_connection(self.db_path) as conn:
            rows = conn.execute(query, params).fetchall()
            for row in rows:
                try:
                    sources = json.loads(row["source_files"] or "[]")
                    if isinstance(sources, list) and sources:
                        result[row["file_path"]] = sources
                except (json.JSONDecodeError, KeyError):
                    continue
        return result

    # ========================================================================
    # Project Operations
    # ========================================================================

    def create_project(
        self,
        name: str,
        label: Optional[str] = None,
        home_dir: Optional[str] = None,
        source_roots: Optional[List[str]] = None,
        description: str = "",
        exclude: Optional[List[str]] = None,
    ) -> Path:
        """Scaffold a new project directory with PROJECT.md template.

        Creates directory, writes PROJECT.md, adds to config/memory.json,
        and registers PROJECT.md in the doc registry.

        Returns path to created PROJECT.md.
        """
        _validate_project_name(name)

        # Guard: don't overwrite an existing project
        cfg = self._get_config()
        if name in cfg.projects.definitions:
            raise ValueError(
                f"Project '{name}' already exists. Use rename-project or delete-project first."
            )

        label = label or name.replace("-", " ").title()
        home = home_dir or f"projects/{name}/"
        home_abs = self._resolve_path(home)
        _validate_inside_workspace(home_abs, "project directory")
        home_abs.mkdir(parents=True, exist_ok=True)

        exclude_list = exclude or ["*.db", "*.log", "*.pyc", "__pycache__/"]
        exclude_lines = "\n".join(f"- {pat}" for pat in exclude_list)

        project_md = PROJECT_MD_TEMPLATE.format(
            label=label,
            description=description or f"{label} project.",
            exclude_lines=exclude_lines,
        )

        project_md_path = home_abs / "PROJECT.md"
        project_md_path.write_text(project_md)

        # Save project definition to DB (source of truth)
        from config import ProjectDefinition, reload_config
        defn = ProjectDefinition(
            label=label,
            home_dir=home,
            source_roots=source_roots or [],
            auto_index=True,
            patterns=["*.md"],
            exclude=exclude_list,
            description=description or f"{label} project.",
        )
        self.save_project_definition(name, defn)

        # Reload config so subsequent calls see the new project
        try:
            reload_config()
            self._config = None
        except Exception:
            pass

        # Register PROJECT.md in the doc registry
        rel_path = str(project_md_path.relative_to(_workspace()))
        self.register(
            file_path=rel_path,
            project=name,
            asset_type="doc",
            title=f"Project: {label}",
            registered_by="create-project",
        )

        print(f"Created project '{name}' at {home_abs}")
        print(f"  PROJECT.md: {project_md_path}")

        return project_md_path

    def auto_discover(self, project_name: str) -> List[str]:
        """Scan a project directory for unregistered files. Register them.

        Returns list of newly registered file paths.
        """
        cfg = self._get_config()
        defn = cfg.projects.definitions.get(project_name)
        if not defn:
            print(f"Project '{project_name}' not found in config")
            return []

        home_abs = self._resolve_path(defn.home_dir)
        if not home_abs.exists():
            print(f"Project home does not exist: {home_abs}")
            return []

        exclude_patterns = defn.exclude or []
        file_patterns = defn.patterns or ["*.md"]
        newly_registered = []

        for pattern in file_patterns:
            for file_path in home_abs.rglob(pattern):
                if not file_path.is_file():
                    continue

                # Check exclusions
                rel_path = str(file_path.relative_to(_workspace()))
                if self._is_excluded(str(file_path), exclude_patterns):
                    continue

                # Check if already registered
                existing = self.get(rel_path)
                if existing:
                    continue

                # Extract title from first heading
                title = self._extract_title(file_path)

                self.register(
                    file_path=rel_path,
                    project=project_name,
                    asset_type="doc",
                    title=title,
                    registered_by="auto-discover",
                )
                newly_registered.append(rel_path)
                print(f"  Discovered: {rel_path}")

        return newly_registered

    def sync_external_files(self, project_name: str) -> List[str]:
        """Parse PROJECT.md External Files section and create registry entries.

        Returns list of newly registered file paths.
        """
        cfg = self._get_config()
        defn = cfg.projects.definitions.get(project_name)
        if not defn:
            return []

        project_md_path = self._resolve_path(defn.home_dir) / "PROJECT.md"
        if not project_md_path.exists():
            return []

        content = project_md_path.read_text()
        newly_registered = []

        # Parse External Files table
        # Format: | ~/projects/webapp/src/routes.js | API implementation | — |
        in_external = False
        for line in content.split("\n"):
            stripped = line.strip()
            if "### External Files" in stripped:
                in_external = True
                continue
            if in_external and stripped.startswith("##"):
                break
            if in_external and stripped.startswith("|") and not stripped.startswith("| File") and not stripped.startswith("|---"):
                parts = [p.strip() for p in stripped.split("|") if p.strip()]
                if len(parts) >= 1:
                    file_path = parts[0].replace("~/", "").replace("~", "")
                    # Resolve ~ paths
                    if file_path.startswith("/"):
                        # Absolute path — make relative to workspace
                        try:
                            file_path = str(Path(file_path).relative_to(_workspace()))
                        except ValueError:
                            pass  # Keep as-is if outside workspace
                    purpose = parts[1] if len(parts) > 1 else None

                    existing = self.get(file_path)
                    if not existing:
                        self.register(
                            file_path=file_path,
                            project=project_name,
                            asset_type="doc",
                            description=purpose,
                            registered_by="sync-external",
                        )
                        newly_registered.append(file_path)
                        print(f"  Registered external: {file_path}")

        return newly_registered

    def sync_from_chunks(self) -> int:
        """Migrate existing doc_chunks entries into registry.

        Returns count of new registrations.
        """
        count = 0
        with get_connection(self.db_path) as conn:
            # Get all unique source_files from doc_chunks
            rows = conn.execute(
                "SELECT DISTINCT source_file FROM doc_chunks"
            ).fetchall()

            for row in rows:
                abs_path = row[0]
                try:
                    rel_path = str(Path(abs_path).relative_to(_workspace()))
                except ValueError:
                    rel_path = abs_path

                existing = self.get(rel_path)
                if existing:
                    continue

                # Determine project from path
                project = self.find_project_for_path(rel_path) or "default"
                title = self._extract_title_from_path(abs_path)

                self.register(
                    file_path=rel_path,
                    project=project,
                    asset_type="doc",
                    title=title,
                    registered_by="sync-from-chunks",
                )
                count += 1
                print(f"  Synced from chunks: {rel_path} -> project={project}")

        return count

    # ========================================================================
    # Bulk Project Operations
    # ========================================================================

    def rename_project(self, old_name: str, new_name: str) -> Dict[str, Any]:
        """Rename a project: update all registry entries, move directory, update config.

        Returns: {"renamed": count, "dir_moved": bool}
        """
        _validate_project_name(new_name)

        # Guard: old project must exist (in config or registry)
        cfg = self._get_config()
        has_config = old_name in cfg.projects.definitions
        has_docs = len(self.list_docs(project=old_name)) > 0
        if not has_config and not has_docs:
            raise ValueError(f"Project '{old_name}' does not exist.")

        # Guard: don't merge into an existing project
        existing_docs = self.list_docs(project=new_name)
        if existing_docs:
            raise ValueError(
                f"Cannot rename to '{new_name}': project already has {len(existing_docs)} registered docs. "
                "Use move-file to migrate docs individually."
            )

        # 1. Bulk update registry + path updates in single transaction
        cfg = self._get_config()
        defn = cfg.projects.definitions.get(old_name)
        old_prefix = defn.home_dir.rstrip("/") if defn else None
        new_prefix = f"projects/{new_name}"

        with get_connection(self.db_path) as conn:
            # Get rows BEFORE renaming the project field
            rows = conn.execute(
                "SELECT id, file_path FROM doc_registry WHERE project = ? AND state = 'active'",
                (old_name,),
            ).fetchall()

            # Update project name for all entries
            cursor = conn.execute(
                "UPDATE doc_registry SET project = ? WHERE project = ? AND state = 'active'",
                (new_name, old_name),
            )
            renamed = cursor.rowcount

            # Update in-directory file paths if dir will move
            if old_prefix:
                for row in rows:
                    if row["file_path"].startswith(old_prefix):
                        new_path = row["file_path"].replace(old_prefix, new_prefix, 1)
                        conn.execute(
                            "UPDATE doc_registry SET file_path = ? WHERE id = ?",
                            (new_path, row["id"]),
                        )

        # 2. Move directory if it exists
        dir_moved = False
        if defn:
            old_dir = self._resolve_path(defn.home_dir)
            new_dir = _workspace() / "projects" / new_name
            _validate_inside_workspace(new_dir, "target directory")
            if old_dir.exists() and not new_dir.exists():
                old_dir.rename(new_dir)
                dir_moved = True

        # 3. Update project definition in DB
        db_defn = self.get_project_definition(old_name)
        if db_defn:
            db_defn.home_dir = f"projects/{new_name}/"
            self.save_project_definition(new_name, db_defn)
            self.delete_project_definition(old_name)

        # Reload config
        try:
            from config import reload_config
            reload_config()
            self._config = None
        except Exception:
            pass

        result = {"renamed": renamed, "dir_moved": dir_moved}
        print(f"Renamed project '{old_name}' -> '{new_name}': {renamed} docs updated, dir_moved={dir_moved}")
        return result

    def archive_project(self, project_name: str) -> Dict[str, Any]:
        """Archive a project: set all entries to archived state, move dir to archive/.

        Returns: {"archived": count, "dir_moved": bool}
        """
        # Guard: project must exist (in config or registry)
        cfg = self._get_config()
        has_config = project_name in cfg.projects.definitions
        has_docs = len(self.list_docs(project=project_name)) > 0
        if not has_config and not has_docs:
            raise ValueError(f"Project '{project_name}' does not exist.")

        # 1. Bulk archive registry entries
        with get_connection(self.db_path) as conn:
            cursor = conn.execute(
                "UPDATE doc_registry SET state = 'archived' WHERE project = ? AND state = 'active'",
                (project_name,),
            )
            archived = cursor.rowcount

        # 2. Move directory to projects/archive/
        cfg = self._get_config()
        defn = cfg.projects.definitions.get(project_name)
        dir_moved = False
        if defn:
            src_dir = self._resolve_path(defn.home_dir)
            archive_dir = _workspace() / "projects" / "archive"
            if src_dir.exists():
                _validate_inside_workspace(src_dir, "archive source")
                archive_dir.mkdir(parents=True, exist_ok=True)
                dest = archive_dir / project_name
                src_dir.rename(dest)
                dir_moved = True

        # 3. Remove from DB
        self.delete_project_definition(project_name)

        # Reload config
        try:
            from config import reload_config
            reload_config()
            self._config = None
        except Exception:
            pass

        result = {"archived": archived, "dir_moved": dir_moved}
        print(f"Archived project '{project_name}': {archived} docs archived, dir_moved={dir_moved}")
        return result

    def delete_project(self, project_name: str) -> Dict[str, Any]:
        """Delete a project: remove all registry entries, trash dir, remove from config.

        Uses 'trash' command for recoverability. Falls back to shutil.rmtree
        only if trash is unavailable.

        Returns: {"deleted": count, "dir_deleted": bool}
        """
        # Guard: project must exist (in config or registry)
        cfg = self._get_config()
        has_config = project_name in cfg.projects.definitions
        has_docs = len(self.list_docs(project=project_name)) > 0
        if not has_config and not has_docs:
            raise ValueError(f"Project '{project_name}' does not exist.")

        # 1. Bulk delete registry entries
        with get_connection(self.db_path) as conn:
            cursor = conn.execute(
                "UPDATE doc_registry SET state = 'deleted' WHERE project = ? AND state IN ('active', 'archived')",
                (project_name,),
            )
            deleted = cursor.rowcount

        # 2. Remove directory (trash > rm)
        cfg = self._get_config()
        defn = cfg.projects.definitions.get(project_name)
        dir_deleted = False
        if defn:
            project_dir = self._resolve_path(defn.home_dir)
            if project_dir.exists():
                _validate_inside_workspace(project_dir, "delete target")
                # Prefer trash for recoverability
                try:
                    subprocess.run(
                        ["trash", str(project_dir)],
                        check=True, capture_output=True, timeout=10,
                    )
                    dir_deleted = True
                except (FileNotFoundError, subprocess.CalledProcessError):
                    # trash not available — warn user instead of irreversible rmtree
                    print(f"  WARNING: 'trash' command not available. Please manually delete: {project_dir}")
                    dir_deleted = False

        # 3. Remove from DB
        self.delete_project_definition(project_name)

        # Reload config
        try:
            from config import reload_config
            reload_config()
            self._config = None
        except Exception:
            pass

        result = {"deleted": deleted, "dir_deleted": dir_deleted}
        print(f"Deleted project '{project_name}': {deleted} docs removed, dir_deleted={dir_deleted}")
        return result

    def move_file(self, file_path: str, to_project: str) -> Dict[str, Any]:
        """Reassign a file's project ownership in the registry.

        NOTE: This only updates the registry entry — it does NOT physically
        move the file on disk. Use 'mv' or 'git mv' separately if needed.

        Returns: {"moved": bool, "new_path": str}
        """
        # Guard: target project must exist
        cfg = self._get_config()
        if to_project not in cfg.projects.definitions:
            raise ValueError(f"Target project '{to_project}' does not exist.")

        # Guard: file must be registered
        entry = self.get(file_path)
        if not entry:
            raise ValueError(f"File '{file_path}' is not registered. Use 'register' first.")

        # Update registry (re-register moves the project assignment)
        self.register(
            file_path=file_path,
            project=to_project,
            asset_type=entry.get("asset_type", "doc"),
            title=entry.get("title"),
            description=entry.get("description"),
            auto_update=entry.get("auto_update", False),
            source_files=entry.get("source_files") or None,
        )

        result = {"moved": True, "new_path": file_path}
        print(f"Moved {file_path} -> project '{to_project}' (registry only)")
        return result

    def verify_project(self, project_name: str) -> Dict[str, Any]:
        """Verify project health: check registered files exist, find orphans.

        Returns: {"total": N, "exists": N, "missing": [...], "orphans": [...]}
        """
        cfg = self._get_config()
        defn = cfg.projects.definitions.get(project_name)

        # Check registered files exist on disk
        docs = self.list_docs(project=project_name)
        exists = []
        missing = []
        for d in docs:
            abs_path = self._resolve_path(d["file_path"])
            if abs_path.exists():
                exists.append(d["file_path"])
            else:
                missing.append(d["file_path"])

        # Find orphans: files in project dir not registered
        orphans = []
        if defn:
            home_abs = self._resolve_path(defn.home_dir)
            if home_abs.exists():
                exclude_patterns = defn.exclude or []
                file_patterns = defn.patterns or ["*.md"]
                registered_paths = {d["file_path"] for d in docs}

                for pattern in file_patterns:
                    for file_path in home_abs.rglob(pattern):
                        if not file_path.is_file():
                            continue
                        rel_path = str(file_path.relative_to(_workspace()))
                        if self._is_excluded(str(file_path), exclude_patterns):
                            continue
                        if rel_path not in registered_paths:
                            orphans.append(rel_path)

        result = {
            "total": len(docs),
            "exists": len(exists),
            "missing": missing,
            "orphans": orphans,
        }

        print(f"Project '{project_name}': {len(docs)} registered, {len(exists)} exist, {len(missing)} missing, {len(orphans)} orphans")
        if missing:
            for f in missing:
                print(f"  MISSING: {f}")
        if orphans:
            for f in orphans:
                print(f"  ORPHAN: {f}")

        return result

    def gc(self, dry_run: bool = True) -> Dict[str, Any]:
        """Garbage collect: remove registry entries pointing to missing files.

        Returns: {"removed": [...], "kept": N}
        """
        from lib.database import get_connection
        removed = []
        kept = 0
        with get_connection(self.db_path) as conn:
            rows = conn.execute("SELECT id, file_path, project FROM doc_registry WHERE state = 'active'").fetchall()
            for row in rows:
                abs_path = self._resolve_path(row["file_path"])
                if not abs_path.exists():
                    removed.append({"id": row["id"], "file_path": row["file_path"], "project": row["project"]})
                    if not dry_run:
                        conn.execute("DELETE FROM doc_registry WHERE id = ?", (row["id"],))
                else:
                    kept += 1

        result = {"removed": removed, "kept": kept}
        if removed:
            action = "Would remove" if dry_run else "Removed"
            print(f"{action} {len(removed)} broken registry entries ({kept} kept)")
            for r in removed:
                print(f"  {r['file_path']} (project: {r['project']})")
        else:
            print(f"No broken entries found ({kept} entries all valid)")
        return result

    # ========================================================================
    # Helpers
    # ========================================================================

    def list_projects(self) -> List[Dict[str, Any]]:
        """List all projects with their metadata and doc counts.

        Returns list of dicts with: name, label, description, doc_count, home_dir.
        """
        cfg = self._get_config()
        projects = []
        for name, defn in cfg.projects.definitions.items():
            docs = self.list_docs(project=name)
            projects.append({
                "name": name,
                "label": defn.label,
                "description": defn.description,
                "doc_count": len(docs),
                "home_dir": defn.home_dir,
                "source_roots": defn.source_roots,
            })
        return projects

    def _update_config(self, mutator_fn) -> bool:
        """Apply a mutation to config/memory.json atomically. Returns True if updated."""
        config_path = _workspace() / "config" / "memory.json"
        if not config_path.exists():
            return False
        try:
            config_data = json.loads(config_path.read_text())
            mutator_fn(config_data)
            # Atomic write: temp file then rename
            tmp_path = config_path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(config_data, indent=2) + "\n")
            tmp_path.replace(config_path)
            # Reload cached config
            try:
                from config import reload_config
                reload_config()
                self._config = None
            except Exception as e:
                print(f"  Warning: config reload failed (stale cache): {e}", file=sys.stderr)
            return True
        except Exception as e:
            print(f"  Warning: config update failed: {e}", file=sys.stderr)
            # Clean up temp file if it exists
            tmp_path = config_path.with_suffix(".tmp")
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
            return False

    def _resolve_path(self, relative: str) -> Path:
        """Resolve a workspace-relative path to absolute."""
        p = Path(relative)
        if p.is_absolute():
            return p
        return _workspace() / relative

    def _is_excluded(self, file_path: str, patterns: List[str]) -> bool:
        """Check if a file matches any exclusion pattern."""
        name = Path(file_path).name
        for pat in patterns:
            if pat.endswith("/"):
                # Directory pattern — match as a path component, not a substring
                dir_name = pat.rstrip("/")
                if f"/{dir_name}/" in file_path or file_path.startswith(f"{dir_name}/"):
                    return True
            elif fnmatch(name, pat):
                return True
        return False

    def _extract_title(self, file_path: Path) -> Optional[str]:
        """Extract title from first markdown heading."""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    match = re.match(r"^#\s+(.+)", line.strip())
                    if match:
                        return match.group(1).strip()
        except Exception:
            pass
        return None

    def _extract_title_from_path(self, abs_path: str) -> Optional[str]:
        """Extract title from file at absolute path."""
        try:
            return self._extract_title(Path(abs_path))
        except Exception:
            return None

    def _row_to_dict(self, row) -> Dict[str, Any]:
        """Convert a database row to a dictionary."""
        return {
            "id": row["id"],
            "file_path": row["file_path"],
            "project": row["project"],
            "asset_type": row["asset_type"],
            "title": row["title"],
            "description": row["description"],
            "tags": json.loads(row["tags"]) if row["tags"] else [],
            "state": row["state"],
            "auto_update": bool(row["auto_update"]),
            "source_files": json.loads(row["source_files"]) if row["source_files"] else [],
            "source_channel": row["source_channel"] if "source_channel" in row.keys() else None,
            "source_conversation_id": row["source_conversation_id"] if "source_conversation_id" in row.keys() else None,
            "source_author_id": row["source_author_id"] if "source_author_id" in row.keys() else None,
            "speaker_entity_id": row["speaker_entity_id"] if "speaker_entity_id" in row.keys() else None,
            "subject_entity_id": row["subject_entity_id"] if "subject_entity_id" in row.keys() else None,
            "conversation_id": row["conversation_id"] if "conversation_id" in row.keys() else None,
            "visibility_scope": row["visibility_scope"] if "visibility_scope" in row.keys() else None,
            "sensitivity": row["sensitivity"] if "sensitivity" in row.keys() else None,
            "participant_entity_ids": json.loads(row["participant_entity_ids"]) if "participant_entity_ids" in row.keys() and row["participant_entity_ids"] else [],
            "provenance_confidence": row["provenance_confidence"] if "provenance_confidence" in row.keys() else None,
            "last_indexed_at": row["last_indexed_at"],
            "last_modified_at": row["last_modified_at"],
            "registered_at": row["registered_at"],
            "registered_by": row["registered_by"],
        }


# ============================================================================
# Migration: sync existing sourceMapping + docPurposes into registry
# ============================================================================

def sync_existing_docs(registry: DocsRegistry) -> Dict[str, int]:
    """Migrate existing config sourceMapping + docPurposes into registry.

    Steps:
    1. Create quaid project directory + PROJECT.md
    2. Register existing docs/ files under project="quaid"
    3. Migrate sourceMapping -> per-doc source_files + auto_update
    4. Migrate docPurposes -> per-doc description
    5. Sync from doc_chunks (catch any missed files)

    Returns: {"registered": N, "from_chunks": M}
    """
    from config import get_config
    cfg = get_config()

    # 1. Create project directory
    project_home = _workspace() / cfg.projects.definitions.get(
        "quaid", type("", (), {"home_dir": "projects/quaid/"})()
    ).home_dir
    project_home.mkdir(parents=True, exist_ok=True)

    # 2. Build inverted sourceMapping: doc_path -> [source_paths]
    doc_sources: Dict[str, List[str]] = {}
    for src_path, mapping in cfg.docs.source_mapping.items():
        for doc_path in mapping.docs:
            doc_sources.setdefault(doc_path, []).append(src_path)

    # 3. Register docs with sourceMapping + docPurposes
    registered = 0
    all_doc_paths = set(list(doc_sources.keys()) + list(cfg.docs.doc_purposes.keys()))

    for doc_path in sorted(all_doc_paths):
        sources = doc_sources.get(doc_path, [])
        purpose = cfg.docs.doc_purposes.get(doc_path, "")
        title = registry._extract_title_from_path(str(_workspace() / doc_path))

        registry.register(
            file_path=doc_path,
            project="quaid",
            asset_type="doc",
            title=title,
            description=purpose,
            auto_update=bool(sources),
            source_files=sources if sources else None,
            registered_by="migration",
        )
        registered += 1
        auto_str = " [auto-update]" if sources else ""
        print(f"  Registered: {doc_path}{auto_str}")
        if sources:
            for s in sources:
                print(f"    <- {s}")

    # 4. Sync from doc_chunks (catch workspace .md files etc.)
    from_chunks = registry.sync_from_chunks()

    # 5. Generate PROJECT.md for quaid
    _generate_project_md(registry, "quaid", cfg)

    return {"registered": registered, "from_chunks": from_chunks}


def _generate_project_md(registry: DocsRegistry, project_name: str, cfg) -> None:
    """Generate PROJECT.md with auto file list from registry."""
    defn = cfg.projects.definitions.get(project_name)
    if not defn:
        return

    project_home = _workspace() / defn.home_dir
    project_md_path = project_home / "PROJECT.md"

    docs = registry.list_docs(project=project_name)

    # Build files section
    in_dir_files = []
    external_files = []
    home_abs = str(project_home)

    for doc in docs:
        abs_path = str((_workspace() / doc["file_path"]).resolve())
        if abs_path.startswith(str(project_home.resolve())):
            in_dir_files.append(doc)
        else:
            external_files.append(doc)

    # Build documents section (docs with source_files)
    tracked_docs = [d for d in docs if d.get("source_files")]

    # Format external files table
    ext_lines = []
    for d in external_files:
        purpose = d.get("description") or ""
        auto = "Yes" if d.get("auto_update") else "No"
        ext_lines.append(f"| {d['file_path']} | {purpose} | {auto} |")

    # Format documents table
    doc_lines = []
    for d in tracked_docs:
        sources = ", ".join(d.get("source_files", []))
        auto = "Yes" if d.get("auto_update") else "No"
        doc_lines.append(f"| {d['file_path']} | {sources} | {auto} |")

    # Build source→doc update rules
    rules = []
    for d in tracked_docs:
        sources = d.get("source_files", [])
        if sources:
            src_str = " or ".join(Path(s).name for s in sources)
            rules.append(f"- When {src_str} changes → update {d['file_path']}")

    exclude_patterns = defn.exclude or []
    exclude_lines = "\n".join(f"- {pat}" for pat in exclude_patterns)

    content = f"""# Project: {defn.label}

## Overview
{defn.description}

## Files & Assets

### In This Directory
<!-- Auto-discovered — all files in this directory belong to this project -->
{chr(10).join('- ' + d['file_path'] for d in in_dir_files) if in_dir_files else '(none yet)'}

### External Files
| File | Purpose | Auto-Update |
|------|---------|-------------|
{chr(10).join(ext_lines) if ext_lines else ''}

## Documents
| Document | Tracks | Auto-Update |
|----------|--------|-------------|
{chr(10).join(doc_lines) if doc_lines else ''}

## Related Projects

## Update Rules
{chr(10).join(rules) if rules else '- (none configured)'}

## Exclude
{exclude_lines}
"""

    project_md_path.write_text(content)
    print(f"  Generated PROJECT.md at {project_md_path}")


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Document Registry")
    subparsers = parser.add_subparsers(dest="command", help="Command")

    # register
    reg_p = subparsers.add_parser("register", help="Register a document")
    reg_p.add_argument("file_path", help="File path (workspace-relative)")
    reg_p.add_argument("--project", default="default", help="Project name")
    reg_p.add_argument("--type", dest="asset_type", default="doc", help="Asset type")
    reg_p.add_argument("--title", help="Document title")
    reg_p.add_argument("--description", help="Document description")
    reg_p.add_argument("--auto-update", action="store_true", help="Auto-update when sources change")
    reg_p.add_argument("--source-files", nargs="*", help="Source files this doc tracks")
    reg_p.add_argument("--json", action="store_true", help="JSON output")

    # list
    list_p = subparsers.add_parser("list", help="List registered docs")
    list_p.add_argument("--project", help="Filter by project")
    list_p.add_argument("--type", dest="asset_type", help="Filter by type")
    list_p.add_argument("--json", action="store_true", help="JSON output")

    # read
    read_p = subparsers.add_parser("read", help="Read a document")
    read_p.add_argument("identifier", help="File path or title")

    # unregister
    unreg_p = subparsers.add_parser("unregister", help="Unregister a document")
    unreg_p.add_argument("file_path", help="File path")

    # find-project
    find_p = subparsers.add_parser("find-project", help="Find which project owns a file")
    find_p.add_argument("file_path", help="File path to look up")

    # create-project
    create_p = subparsers.add_parser("create-project", help="Scaffold a new project")
    create_p.add_argument("name", help="Project name (kebab-case)")
    create_p.add_argument("--label", help="Display label")
    create_p.add_argument("--source-roots", nargs="*", help="Source root directories")
    create_p.add_argument("--description", default="", help="Project description")

    # discover
    disc_p = subparsers.add_parser("discover", help="Auto-discover files in project")
    disc_p.add_argument("--project", required=True, help="Project name")

    # sync-external
    sync_ext_p = subparsers.add_parser("sync-external", help="Sync PROJECT.md External Files to registry")
    sync_ext_p.add_argument("--project", required=True, help="Project name")

    # sync (migration)
    sync_p = subparsers.add_parser("sync", help="Migrate existing docs into registry")

    # rename-project
    ren_p = subparsers.add_parser("rename-project", help="Rename a project (registry + dir + config)")
    ren_p.add_argument("old_name", help="Current project name")
    ren_p.add_argument("new_name", help="New project name")

    # archive-project
    arch_p = subparsers.add_parser("archive-project", help="Archive a project")
    arch_p.add_argument("name", help="Project name to archive")
    arch_p.add_argument("--yes", action="store_true", help="Skip confirmation")

    # delete-project
    del_p = subparsers.add_parser("delete-project", help="Delete a project entirely")
    del_p.add_argument("name", help="Project name to delete")
    del_p.add_argument("--yes", action="store_true", help="Skip confirmation")

    # move-file
    mv_p = subparsers.add_parser("move-file", help="Move a file to a different project")
    mv_p.add_argument("file_path", help="File path to move")
    mv_p.add_argument("--to-project", required=True, help="Target project name")

    # verify
    ver_p = subparsers.add_parser("verify", help="Check project health (missing files, orphans)")
    ver_p.add_argument("--project", required=True, help="Project name to verify")
    ver_p.add_argument("--json", action="store_true", help="JSON output")

    # list-projects
    lp_p = subparsers.add_parser("list-projects", help="List all defined projects")
    lp_p.add_argument("--json", action="store_true", help="JSON output")
    lp_p.add_argument("--names-only", action="store_true", help="Output project names only, one per line")

    # stats
    stats_p = subparsers.add_parser("stats", help="Show registry statistics")
    stats_p.add_argument("--json", action="store_true", help="JSON output")

    # gc
    gc_p = subparsers.add_parser("gc", help="Remove broken registry entries (missing files)")
    gc_p.add_argument("--apply", action="store_true", help="Actually delete (default: dry-run)")
    gc_p.add_argument("--json", action="store_true", help="JSON output")

    # source-mappings
    sm_p = subparsers.add_parser("source-mappings", help="Show source->doc mappings for staleness")
    sm_p.add_argument("--project", help="Filter by project")
    sm_p.add_argument("--json", action="store_true", help="JSON output")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    registry = DocsRegistry()

    if args.command == "register":
        # Handle source_files: CLI accepts space-separated paths or a JSON array string
        source_files = args.source_files
        if source_files and len(source_files) == 1 and source_files[0].startswith("["):
            try:
                source_files = json.loads(source_files[0])
            except json.JSONDecodeError:
                pass  # Not valid JSON, treat as literal path
        row_id = registry.register(
            file_path=args.file_path,
            project=args.project,
            asset_type=args.asset_type,
            title=args.title,
            description=args.description,
            auto_update=args.auto_update,
            source_files=source_files,
        )
        if args.json:
            print(json.dumps({"id": row_id, "file_path": args.file_path, "project": args.project}))
        else:
            print(f"Registered: {args.file_path} (project={args.project}, id={row_id})")

    elif args.command == "list":
        docs = registry.list_docs(project=args.project, asset_type=args.asset_type)
        if args.json:
            print(json.dumps(docs, indent=2))
        else:
            if not docs:
                print("No documents registered.")
            else:
                current_project = None
                for d in docs:
                    if d["project"] != current_project:
                        current_project = d["project"]
                        print(f"\n[{current_project}]")
                    auto = " [auto]" if d["auto_update"] else ""
                    title = f" — {d['title']}" if d["title"] else ""
                    print(f"  {d['file_path']}{title}{auto}")

    elif args.command == "read":
        entry = registry.read(args.identifier)
        if not entry:
            print(f"Not found: {args.identifier}")
            sys.exit(1)
        print(f"File: {entry['file_path']}")
        print(f"Project: {entry['project']}")
        if entry.get("title"):
            print(f"Title: {entry['title']}")
        print("---")
        if entry.get("content"):
            print(entry["content"])
        else:
            print("(file not found on disk)")

    elif args.command == "unregister":
        ok = registry.unregister(args.file_path)
        if ok:
            print(f"Unregistered: {args.file_path}")
        else:
            print(f"Not found or already deleted: {args.file_path}")
            sys.exit(1)

    elif args.command == "find-project":
        project = registry.find_project_for_path(args.file_path)
        if project:
            print(project)
        else:
            print(f"No project found for: {args.file_path}")
            sys.exit(1)

    elif args.command == "create-project":
        registry.create_project(
            name=args.name,
            label=args.label,
            source_roots=args.source_roots,
            description=args.description,
        )

    elif args.command == "discover":
        found = registry.auto_discover(args.project)
        print(f"\nDiscovered {len(found)} new file(s)")

    elif args.command == "sync-external":
        found = registry.sync_external_files(args.project)
        print(f"\nSynced {len(found)} external file(s)")

    elif args.command == "sync":
        print("Migrating existing docs into registry...")
        result = sync_existing_docs(registry)
        print(f"\nMigration complete:")
        print(f"  Registered from config: {result['registered']}")
        print(f"  Synced from chunks: {result['from_chunks']}")

    elif args.command == "rename-project":
        result = registry.rename_project(args.old_name, args.new_name)
        sys.exit(0 if result["renamed"] > 0 else 1)

    elif args.command == "archive-project":
        if not args.yes:
            docs = registry.list_docs(project=args.name)
            print(f"Will archive project '{args.name}' ({len(docs)} docs).")
            confirm = input("Continue? [y/N] ").strip().lower()
            if confirm != "y":
                print("Aborted.")
                sys.exit(1)
        result = registry.archive_project(args.name)
        sys.exit(0 if result["archived"] > 0 else 1)

    elif args.command == "delete-project":
        if not args.yes:
            docs = registry.list_docs(project=args.name)
            print(f"Will DELETE project '{args.name}' ({len(docs)} docs) and remove directory.")
            confirm = input("Continue? [y/N] ").strip().lower()
            if confirm != "y":
                print("Aborted.")
                sys.exit(1)
        result = registry.delete_project(args.name)
        sys.exit(0 if result["deleted"] > 0 else 1)

    elif args.command == "move-file":
        result = registry.move_file(args.file_path, args.to_project)
        sys.exit(0 if result["moved"] else 1)

    elif args.command == "verify":
        result = registry.verify_project(args.project)
        if args.json:
            print(json.dumps(result, indent=2))
        sys.exit(0 if not result["missing"] else 1)

    elif args.command == "list-projects":
        if args.names_only:
            defs = registry.get_all_project_definitions()
            for name in sorted(defs.keys()):
                print(name)
        elif args.json:
            projects = registry.list_projects()
            print(json.dumps(projects, indent=2))
        else:
            projects = registry.list_projects()
            if not projects:
                print("No projects defined.")
            else:
                for p in projects:
                    print(f"  {p['name']:20s} {p['label']:25s} {p['doc_count']:3d} docs  {p['home_dir']}")

    elif args.command == "stats":
        projects = registry.list_projects()
        all_docs = registry.list_docs()
        # Count per project
        per_project = {}
        for d in all_docs:
            per_project[d["project"]] = per_project.get(d["project"], 0) + 1
        # Check missing files
        missing = []
        for d in all_docs:
            abs_path = registry._resolve_path(d["file_path"])
            if not abs_path.exists():
                missing.append(d["file_path"])
        stats = {
            "total_projects": len(projects),
            "total_docs": len(all_docs),
            "per_project": per_project,
            "missing_files": missing,
            "missing_count": len(missing),
        }
        if args.json:
            print(json.dumps(stats, indent=2))
        else:
            print(f"Projects: {stats['total_projects']}")
            print(f"Total docs: {stats['total_docs']}")
            for proj, count in sorted(per_project.items()):
                print(f"  {proj}: {count}")
            if missing:
                print(f"\nMissing files ({len(missing)}):")
                for m in missing:
                    print(f"  {m}")
            else:
                print("\nAll registered files exist on disk.")

    elif args.command == "source-mappings":
        mappings = registry.get_source_mappings(project=args.project)
        if args.json:
            print(json.dumps(mappings, indent=2))
        else:
            if not mappings:
                print("No source mappings configured.")
            else:
                for doc, sources in mappings.items():
                    print(f"  {doc}")
                    for s in sources:
                        print(f"    <- {s}")

    elif args.command == "gc":
        result = registry.gc(dry_run=not args.apply)
        if args.json:
            print(json.dumps(result, default=str, indent=2))


if __name__ == "__main__":
    try:
        main()
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
