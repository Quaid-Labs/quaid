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
  python3 docs_registry.py move-file <path> --to-project <name>
  python3 docs_registry.py verify --project <name> [--json]
  python3 docs_registry.py discover --project <name>
  python3 docs_registry.py sync-external --project <name>
  python3 docs_registry.py sync
  python3 docs_registry.py source-mappings [--project <name>]
"""

import argparse
import contextlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from lib.config import get_docs_db_path
from lib.database import get_connection
from lib.project_templates import (
    EXTERNAL_FILES_BEGIN,
    EXTERNAL_FILES_END,
    IN_DIR_FILES_BEGIN,
    IN_DIR_FILES_END,
    PROJECT_HOME_BEGIN,
    PROJECT_HOME_END,
    REGISTERED_DOCS_BEGIN,
    REGISTERED_DOCS_END,
    has_registry_managed_markers,
    SOURCE_ROOTS_BEGIN,
    SOURCE_ROOTS_END,
    render_project_md_template,
    replace_managed_block,
)
from lib.runtime_context import get_quaid_home, get_visible_quaid_home, get_workspace_dir

logger = logging.getLogger(__name__)

ASYNC_REGISTRATION_NOTICE = (
    "Async registration queued; document indexing runs shortly through "
    "the project-docs supervisor. Run `quaid docs update <project> --wait` "
    "if you need a deterministic indexing point now."
)


def _fail_hard_enabled() -> bool:
    try:
        from lib.fail_policy import is_fail_hard_enabled
    except Exception as exc:
        logger.critical("Failed to load fail-hard policy; failing closed: %s", exc)
        raise RuntimeError("Failed to load fail-hard policy") from exc
    return bool(is_fail_hard_enabled())


def _reload_config_after_project_change(action: str) -> bool:
    try:
        from config import reload_config

        reload_config()
        return True
    except Exception as exc:
        if _fail_hard_enabled():
            raise RuntimeError(f"Failed to reload config after {action}") from exc
        logger.warning("Config reload skipped after %s: %s", action, exc)
        return False


def _current_quaid_instance_id() -> str:
    raw = os.environ.get("QUAID_INSTANCE", "").strip()
    if raw:
        return raw
    try:
        from lib.instance import instance_id

        return str(instance_id() or "").strip()
    except Exception as exc:
        logger.debug("Failed resolving current Quaid instance id: %s", exc)
        return ""


def _dedupe_instances(instances: Any) -> List[str]:
    ordered: List[str] = []
    seen: set[str] = set()
    for raw in list(instances or []):
        name = str(raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    return ordered


def _project_md_lock_path(path: Path) -> Optional[Path]:
    if path.name != "PROJECT.md":
        return None
    return path.with_name(".PROJECT.md.lock")


@contextlib.contextmanager
def _project_md_write_lock(path: Path):
    lock_path = _project_md_lock_path(path)
    if lock_path is None:
        yield
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")
    locked = False
    try:
        try:
            import fcntl  # type: ignore

            fcntl.flock(handle, fcntl.LOCK_EX)
            locked = True
        except (ImportError, OSError) as exc:
            logger.warning("PROJECT.md lock unavailable for %s; proceeding without lock: %s", path, exc)
            if _fail_hard_enabled():
                raise RuntimeError(f"Failed to acquire PROJECT.md lock: {path}") from exc
        yield
    finally:
        release_exc: Optional[OSError] = None
        if locked:
            try:
                import fcntl  # type: ignore

                fcntl.flock(handle, fcntl.LOCK_UN)
            except OSError as exc:
                logger.warning("Failed to release PROJECT.md lock for %s: %s", path, exc)
                if _fail_hard_enabled():
                    release_exc = exc
        try:
            handle.close()
        finally:
            if release_exc is not None:
                raise RuntimeError(f"Failed to release PROJECT.md lock: {path}") from release_exc


@contextlib.contextmanager
def _config_write_lock(path: Path):
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")
    locked = False
    try:
        try:
            import fcntl  # type: ignore

            fcntl.flock(handle, fcntl.LOCK_EX)
            locked = True
        except (ImportError, OSError) as exc:
            logger.warning("config.json lock unavailable for %s; proceeding without lock: %s", path, exc)
            if _fail_hard_enabled():
                raise RuntimeError(f"Failed to acquire config.json lock: {path}") from exc
        yield
    finally:
        release_exc: Optional[OSError] = None
        if locked:
            try:
                import fcntl  # type: ignore

                fcntl.flock(handle, fcntl.LOCK_UN)
            except OSError as exc:
                logger.warning("Failed to release config.json lock for %s: %s", path, exc)
                if _fail_hard_enabled():
                    release_exc = exc
        try:
            handle.close()
        finally:
            if release_exc is not None:
                raise RuntimeError(f"Failed to release config.json lock: {path}") from release_exc


def _atomic_write_text_unlocked(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Write text via a same-directory temp file and atomic replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding=encoding,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_name = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        tmp_name = ""
    finally:
        if tmp_name:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass


def _atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    with _project_md_write_lock(path):
        _atomic_write_text_unlocked(path, content, encoding=encoding)


def _write_text_if_missing(path: Path, content: str, *, encoding: str = "utf-8") -> bool:
    with _project_md_write_lock(path):
        if path.exists():
            return False
        _atomic_write_text_unlocked(path, content, encoding=encoding)
        return True


def _docs_project_visible_to_current_instance(project: Optional[str]) -> bool:
    name = str(project or "").strip()
    current = _current_quaid_instance_id()
    if not current or not name or name == "default":
        return True
    if name.startswith("misc--") and name != f"misc--{current}":
        return False
    try:
        from lib.project_registry import lookup

        entry = lookup(name)
    except Exception as exc:
        if _fail_hard_enabled():
            raise RuntimeError(f"Failed checking project linkage for {name!r}: {exc}") from exc
        logger.warning("Project linkage check failed for %s; hiding docs from current instance: %s", name, exc)
        return False
    if not entry:
        return False
    return current in _dedupe_instances(entry.get("instances", []))


def _run_locked_write_with_retry(op, *, op_name: str, max_attempts: int = 3, base_sleep_seconds: float = 2.0):
    """Retry transient SQLite lock failures for small metadata writes."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return op()
        except sqlite3.OperationalError as exc:
            if "database is locked" not in str(exc).lower() or attempt >= max_attempts:
                raise
            sleep_s = base_sleep_seconds * attempt
            logger.warning(
                "%s hit sqlite lock (attempt %d/%d); retrying in %.1fs",
                op_name,
                attempt,
                max_attempts,
                sleep_s,
            )
            time.sleep(sleep_s)

def _workspace() -> Path:
    return get_workspace_dir()


def _visible_home() -> Path:
    return get_visible_quaid_home()


# Keep underscores for installed alpha projects, but reject dots/separators and cap path length.
_MAX_PROJECT_NAME_LEN = 128
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:")
MALFORMED_SOURCE_FILES_MARKER = "__quaid_docs_registry_malformed_source_files__"


def _normalize_project_name(name: str) -> str:
    return unicodedata.normalize("NFKC", str(name or "")).casefold().strip()


def _is_project_name_char_allowed(ch: str) -> bool:
    return ch.isalnum() or unicodedata.category(ch).startswith("M") or ch in {"-", "_"}


def _validate_project_name(name: str) -> str:
    normalized = _normalize_project_name(name)
    if not normalized:
        raise ValueError("Project name is required")
    if (
        len(normalized) > _MAX_PROJECT_NAME_LEN
        or not normalized[0].isalnum()
        or any(not _is_project_name_char_allowed(ch) for ch in normalized[1:])
    ):
        raise ValueError(f"Invalid project name: {name!r}")
    return normalized


def _malformed_source_files_marker(file_path: str) -> str:
    return f"{MALFORMED_SOURCE_FILES_MARKER}:{file_path}"


def _path_is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _validate_inside_workspace(resolved_path: Path, label: str = "path") -> None:
    """Validate that a resolved path is inside the workspace or quaid_home."""
    try:
        real = resolved_path.resolve()
        # Per-instance paths: must be under instance_root.
        # Canonical project paths (projects/...): must be under quaid_home.
        workspace_real = _workspace().resolve()
        hidden_home_real = get_quaid_home().resolve()
        visible_home_real = _visible_home().resolve()
        inside_instance = _path_is_under(real, workspace_real)
        inside_hidden = _path_is_under(real, hidden_home_real)
        inside_visible = _path_is_under(real, visible_home_real)
        if not inside_instance and not inside_hidden and not inside_visible:
            raise ValueError(f"Refusing {label} outside workspace: {real}")
    except (OSError, ValueError) as e:
        raise ValueError(f"Invalid {label}: {e}")


def _validate_discovery_glob(pattern: Any) -> str:
    value = str(pattern or "").strip()
    if not value:
        raise ValueError("empty project discovery glob pattern")
    if "\x00" in value:
        raise ValueError("NUL byte is not allowed in project discovery glob pattern")
    if "\\" in value:
        raise ValueError("backslashes are not allowed in project discovery glob pattern")
    if value.startswith("/") or _WINDOWS_ABSOLUTE_RE.match(value):
        raise ValueError("absolute project discovery glob patterns are not allowed")

    for part in value.split("/"):
        if part in ("", ".", ".."):
            raise ValueError("empty, current, or parent path segments are not allowed in project discovery glob patterns")
        if "**" in part and part != "**":
            raise ValueError("'**' must be an entire path segment in project discovery glob patterns")
    return value


def _iter_project_files(home_abs: Path, patterns: List[str], context: str) -> Iterator[Path]:
    home_real = home_abs.resolve()
    _validate_inside_workspace(home_real, f"{context} root")

    for raw_pattern in patterns:
        try:
            pattern = _validate_discovery_glob(raw_pattern)
        except ValueError as exc:
            logger.warning("Skipping unsafe project discovery glob %r for %s: %s", raw_pattern, context, exc)
            if _fail_hard_enabled():
                raise
            continue

        try:
            for file_path in home_abs.rglob(pattern):
                try:
                    resolved = file_path.resolve()
                    if not _path_is_under(resolved, home_real):
                        raise ValueError(f"project discovery match escaped project root: {resolved}")
                    _validate_inside_workspace(resolved, f"{context} match")
                    if not resolved.is_file():
                        continue
                except (OSError, ValueError) as exc:
                    logger.warning("Skipping unsafe project discovery match %s for %s: %s", file_path, context, exc)
                    if _fail_hard_enabled():
                        raise RuntimeError(f"Unsafe project discovery match for {context}: {file_path}") from exc
                    continue
                yield resolved
        except (OSError, ValueError) as exc:
            logger.warning("Project discovery glob %r failed for %s: %s", raw_pattern, context, exc)
            if _fail_hard_enabled():
                raise RuntimeError(f"Project discovery glob failed for {context}: {raw_pattern!r}") from exc


def _get_default_db_path() -> Path:
    """Get DB path lazily (respects env vars set after import)."""
    return get_docs_db_path()


def _now_iso() -> str:
    """Return the effective docs-registry write timestamp."""
    raw = str(os.environ.get("QUAID_NOW", "") or "").strip()
    if raw:
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            if _fail_hard_enabled():
                raise RuntimeError(f"Invalid QUAID_NOW={raw!r}") from exc
            logger.warning("Invalid QUAID_NOW=%r; using wall clock", raw)
        else:
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            else:
                value = value.astimezone(timezone.utc)
            return value.isoformat()
    return datetime.now(timezone.utc).isoformat()


def _format_bullets(items: List[str], *, empty: str) -> str:
    cleaned = [str(item).strip() for item in items if str(item).strip()]
    if not cleaned:
        return empty
    return "\n".join(f"- `{item}`" for item in cleaned)


def _resolve_registry_relative_path(path_str: str) -> Path:
    p = Path(path_str)
    if p.is_absolute():
        return p
    if path_str == "projects" or (
        path_str.startswith("projects/")
        and path_str != "projects/staging"
        and not path_str.startswith("projects/staging/")
    ):
        return _visible_home() / path_str
    if path_str.startswith("shared/"):
        return get_quaid_home() / path_str
    return _workspace() / path_str


def _display_registry_path(path_str: str, project_home: Path) -> str:
    abs_path = _resolve_registry_relative_path(path_str)
    resolved = abs_path.resolve()
    try:
        return str(resolved.relative_to(project_home.resolve()))
    except ValueError:
        return str(resolved)


def _to_registry_path(abs_path: Path) -> str:
    """Normalize an absolute path into the registry's relative path form."""
    resolved = abs_path.resolve()
    workspace = _workspace().resolve()
    hidden_home = get_quaid_home().resolve()
    visible_home = _visible_home().resolve()
    projects_root = (visible_home / "projects").resolve()

    try:
        resolved.relative_to(projects_root)
        return str(resolved.relative_to(visible_home))
    except ValueError:
        pass

    try:
        return str(resolved.relative_to(workspace))
    except ValueError:
        pass

    try:
        return str(resolved.relative_to(hidden_home))
    except ValueError:
        return str(resolved)


def _cli_register_file_path(raw_path: str) -> str:
    """Resolve CLI relative paths from the caller's cwd before registry insert."""
    value = str(raw_path or "").strip()
    if not value:
        return value
    path = Path(value).expanduser()
    if path.is_absolute():
        resolved = path.resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"File not found: {value}")
        if not resolved.is_file():
            raise IsADirectoryError(f"Not a file: {value}")
        return value
    cwd_path = (Path.cwd() / path).resolve()
    if not cwd_path.exists():
        raise FileNotFoundError(f"File not found: {value} (resolved to {cwd_path})")
    if not cwd_path.is_file():
        raise IsADirectoryError(f"Not a file: {value} (resolved to {cwd_path})")
    return _to_registry_path(cwd_path)


def _queue_async_indexing_after_register(project: str) -> Dict[str, Any]:
    """Queue the project-docs worker path advertised by the register CLI."""
    name = _normalize_project_name(project) or "default"
    if name == "default":
        return {
            "queued": False,
            "reason": "default project has no project-docs worker",
        }
    try:
        from core import project_docs

        request = project_docs.request_update(
            name,
            reason="docs-registry-register",
            requested_by="docs-registry-cli",
        )
        supervisor_pid = project_docs.ensure_supervisor_alive()
        return {
            "queued": True,
            "request_id": str(request.get("request_id") or ""),
            "supervisor_pid": supervisor_pid,
        }
    except Exception as exc:
        logger.warning("Failed to queue async docs indexing for project %s: %s", name, exc)
        if _fail_hard_enabled():
            raise RuntimeError(f"Failed to queue async docs indexing for project {name}") from exc
        return {
            "queued": False,
            "error": str(exc),
        }


def _manual_index_command(project: str) -> str:
    name = _normalize_project_name(project) or "default"
    if name == "default":
        return "quaid docs update --apply"
    return f"quaid docs update {name} --wait"


def _why_read_entry(doc: Dict[str, Any]) -> str:
    description = str(doc.get("description") or "").strip()
    if description:
        return description
    sources = [str(s).strip() for s in (doc.get("source_files") or []) if str(s).strip()]
    if sources:
        names = ", ".join(Path(s).name for s in sources[:3])
        if len(sources) > 3:
            names += ", ..."
        return f"Tracks: {names}"
    title = str(doc.get("title") or "").strip()
    if title:
        return title
    return "Project reference"


def _managed_project_sections(
    registry: "DocsRegistry",
    project_name: str,
    defn: Any,
) -> Dict[str, str]:
    project_home = registry._resolve_path(defn.home_dir)
    docs = registry.list_docs(project=project_name)

    project_home_body = f"- `{project_home}`"

    source_roots_body = _format_bullets(
        [str(registry._resolve_path(root)) for root in (defn.source_roots or [])],
        empty="- `(none configured yet)`",
    )

    in_dir_docs: List[Dict[str, Any]] = []
    external_docs: List[Dict[str, Any]] = []
    for doc in docs:
        resolved = registry._resolve_path(doc["file_path"]).resolve()
        try:
            resolved.relative_to(project_home.resolve())
            in_dir_docs.append(doc)
        except ValueError:
            external_docs.append(doc)

    in_dir_body = _format_bullets(
        [_display_registry_path(doc["file_path"], project_home) for doc in in_dir_docs],
        empty="(none yet)",
    )

    external_rows = []
    for doc in external_docs:
        purpose = str(doc.get("description") or doc.get("title") or "").strip()
        auto = "Yes" if doc.get("auto_update") else "—"
        external_rows.append(
            f"| `{_display_registry_path(doc['file_path'], project_home)}` | {purpose} | {auto} |"
        )
    external_body = (
        "| File | Purpose | Auto-Update |\n"
        "|------|---------|-------------|\n"
        + ("\n".join(external_rows) if external_rows else "")
    ).rstrip()

    registered_rows = []
    for doc in docs:
        display = _display_registry_path(doc["file_path"], project_home)
        if display == "PROJECT.md":
            continue
        auto = "Yes" if doc.get("auto_update") else "—"
        registered_rows.append(
            f"| `{display}` | {_why_read_entry(doc)} | {auto} |"
        )
    registered_body = (
        "| Document | Why Read It | Auto-Update |\n"
        "|----------|-------------|-------------|\n"
        + ("\n".join(registered_rows) if registered_rows else "")
    ).rstrip()

    return {
        "project_home": project_home_body,
        "source_roots": source_roots_body,
        "in_dir": in_dir_body,
        "external": external_body,
        "registered_docs": registered_body,
    }


def _populate_project_md_sections(
    content: str,
    *,
    project_home_body: str,
    source_roots_body: str,
    in_dir_body: str,
    external_body: str,
    registered_docs_body: str,
) -> str:
    updated = replace_managed_block(content, PROJECT_HOME_BEGIN, PROJECT_HOME_END, project_home_body)
    updated = replace_managed_block(updated, SOURCE_ROOTS_BEGIN, SOURCE_ROOTS_END, source_roots_body)
    updated = replace_managed_block(updated, IN_DIR_FILES_BEGIN, IN_DIR_FILES_END, in_dir_body)
    updated = replace_managed_block(updated, EXTERNAL_FILES_BEGIN, EXTERNAL_FILES_END, external_body)
    updated = replace_managed_block(updated, REGISTERED_DOCS_BEGIN, REGISTERED_DOCS_END, registered_docs_body)
    return updated


class DocsRegistry:
    """Document registry with CRUD, path resolution, and project operations."""

    def __init__(self, db_path: Path = None, *, seed_projects: bool = True):
        self.db_path = Path(db_path).expanduser() if db_path is not None else _get_default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._config = None
        self._seed_projects = bool(seed_projects)
        self._shared_scope_enabled = (
            db_path is None and not str(os.environ.get("MEMORY_DB_PATH", "")).strip()
        )
        self.ensure_table()
        if self._shared_scope_enabled:
            try:
                from datastore.docsdb.db_migration import migrate_legacy_docs_tables

                migrate_legacy_docs_tables(
                    self.db_path,
                    tables=("project_definitions", "doc_registry"),
                )
            except Exception as exc:
                if _fail_hard_enabled():
                    raise RuntimeError("DocsRegistry legacy docs-table merge failed") from exc
                logger.warning("DocsRegistry legacy docs-table merge skipped: %s", exc)

    def _get_config(self):
        """Lazy-load config to avoid circular imports."""
        if self._config is None:
            from config import get_config
            self._config = get_config()
        return self._config

    def _decode_json_list(self, raw_value: Any, *, field_name: str, default: Optional[List[Any]] = None) -> List[Any]:
        """Decode a JSON list column defensively."""
        fallback = list(default or [])
        if not raw_value:
            return fallback
        try:
            parsed = json.loads(raw_value)
        except (TypeError, ValueError) as exc:
            logger.warning("Invalid JSON for %s; using default: %s", field_name, exc)
            return fallback
        if not isinstance(parsed, list):
            logger.warning("Invalid JSON type for %s (expected list, got %s); using default", field_name, type(parsed).__name__)
            return fallback
        return parsed

    def _coerce_list_input(self, value: Any, *, field_name: str, default: Optional[List[Any]] = None) -> List[Any]:
        """Normalize list-typed input from config payloads."""
        fallback = list(default or [])
        if value is None:
            return fallback
        if isinstance(value, list):
            return value
        logger.warning("Invalid type for %s (expected list, got %s); using default", field_name, type(value).__name__)
        return fallback

    def _ensure_project_definitions_table(self, conn) -> None:
        """Create project_definitions table if it doesn't exist."""
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

    def _ensure_doc_registry_table(self, conn) -> None:
        """Create doc_registry table and additive columns on this connection."""
        def _ensure_column(conn, table: str, column: str, definition: str) -> None:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

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

    def ensure_table(self):
        """Create doc_registry table if it doesn't exist."""
        with get_connection(self.db_path) as conn:
            self._ensure_doc_registry_table(conn)
            # Project definitions table — DB is source of truth (replaces JSON)
            self._ensure_project_definitions_table(conn)

        # Seed from JSON on first run (empty table)
        if self._seed_projects:
            self._seed_projects_from_json()

    def _seed_projects_from_json(self):
        """Seed project_definitions table from config/config.json on first run.
        Only seeds when the table is empty (avoids re-reading JSON on every instantiation).
        """
        with get_connection(self.db_path) as conn:
            # In-memory and test overrides can hand us a fresh connection even when
            # ensure_table() ran on a prior connection; guarantee table presence.
            self._ensure_project_definitions_table(conn)
            # Skip if table already has rows (not first run)
            row = conn.execute("SELECT COUNT(*) FROM project_definitions").fetchone()
            if row[0] > 0:
                return

            config_path = _workspace() / "config.json"
            if not config_path.exists():
                return

            try:
                config_data = json.loads(config_path.read_text(encoding="utf-8"))
                definitions = config_data.get("projects", {}).get("definitions", {})
                for name, proj_data in definitions.items():
                    now_iso = _now_iso()
                    conn.execute("""
                        INSERT OR IGNORE INTO project_definitions
                        (name, label, home_dir, source_roots, auto_index, patterns, exclude, description, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        name,
                        proj_data.get("label", ""),
                        proj_data.get("homeDir", ""),
                        json.dumps(self._coerce_list_input(
                            proj_data.get("sourceRoots"),
                            field_name=f"projects.definitions.{name}.sourceRoots",
                        )),
                        1 if proj_data.get("autoIndex", True) else 0,
                        json.dumps(self._coerce_list_input(
                            proj_data.get("patterns"),
                            field_name=f"projects.definitions.{name}.patterns",
                            default=["*.md"],
                        )),
                        json.dumps(self._coerce_list_input(
                            proj_data.get("exclude"),
                            field_name=f"projects.definitions.{name}.exclude",
                            default=["*.db", "*.log", "*.pyc", "__pycache__/"],
                        )),
                        proj_data.get("description", ""),
                        now_iso,
                        now_iso,
                    ))
                if definitions:
                    print(f"[docs_registry] Seeded {len(definitions)} project definitions from config/config.json", file=sys.stderr)
            except Exception as e:
                if _fail_hard_enabled():
                    raise
                print(f"[docs_registry] Warning: failed to seed from JSON: {e}", file=sys.stderr)

    # ========================================================================
    # Project Definition CRUD (DB-backed)
    # ========================================================================

    def get_project_definition(self, name: str):
        """Load a single project definition from DB."""
        name = _normalize_project_name(name)
        from config import ProjectDefinition
        with get_connection(self.db_path) as conn:
            self._ensure_project_definitions_table(conn)
            row = conn.execute(
                "SELECT * FROM project_definitions WHERE name = ? AND state = 'active'",
                (name,)
            ).fetchone()
            if not row:
                return None
            return ProjectDefinition(
                label=row["label"],
                home_dir=row["home_dir"],
                source_roots=self._decode_json_list(row["source_roots"], field_name="project_definitions.source_roots"),
                auto_index=bool(row["auto_index"]),
                patterns=self._decode_json_list(row["patterns"], field_name="project_definitions.patterns", default=["*.md"]),
                exclude=self._decode_json_list(row["exclude"], field_name="project_definitions.exclude"),
                description=row["description"] or "",
                state=row["state"],
            )

    def get_all_project_definitions(self):
        """Load all active project definitions from DB."""
        from config import ProjectDefinition
        with get_connection(self.db_path) as conn:
            self._ensure_project_definitions_table(conn)
            rows = conn.execute(
                "SELECT * FROM project_definitions WHERE state = 'active'"
            ).fetchall()
            result = {}
            for row in rows:
                result[row["name"]] = ProjectDefinition(
                    label=row["label"],
                    home_dir=row["home_dir"],
                    source_roots=self._decode_json_list(row["source_roots"], field_name="project_definitions.source_roots"),
                    auto_index=bool(row["auto_index"]),
                    patterns=self._decode_json_list(row["patterns"], field_name="project_definitions.patterns", default=["*.md"]),
                    exclude=self._decode_json_list(row["exclude"], field_name="project_definitions.exclude"),
                    description=row["description"] or "",
                    state=row["state"],
                )
            return result

    def _write_project_definition_row_on_conn(self, conn, name: str, defn) -> None:
        name = _validate_project_name(name)
        self._ensure_project_definitions_table(conn)
        now_iso = _now_iso()
        conn.execute("""
            INSERT INTO project_definitions
                (name, label, home_dir, source_roots, auto_index, patterns, exclude, description, state, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                label = excluded.label,
                home_dir = excluded.home_dir,
                source_roots = excluded.source_roots,
                auto_index = excluded.auto_index,
                patterns = excluded.patterns,
                exclude = excluded.exclude,
                description = excluded.description,
                state = excluded.state,
                updated_at = excluded.updated_at
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
            now_iso,
            now_iso,
        ))

    def _write_project_definition_row(self, name: str, defn) -> None:
        def _write():
            with get_connection(self.db_path) as conn:
                self._write_project_definition_row_on_conn(conn, name, defn)

        _run_locked_write_with_retry(_write, op_name=f"save_project_definition({name})")

    def save_project_definition(self, name: str, defn, *, link_current_instance: bool = True):
        """Upsert a project definition to DB."""
        name = _validate_project_name(name)
        self._write_project_definition_row(name, defn)
        self._ensure_global_project_entry(
            name,
            defn=defn,
            link_current_instance=link_current_instance,
        )

    def delete_project_definition(self, name: str):
        """Soft-delete a project definition (set state to 'deleted')."""
        name = _normalize_project_name(name)
        now_iso = _now_iso()
        with get_connection(self.db_path) as conn:
            conn.execute(
                "UPDATE project_definitions SET state = 'deleted', updated_at = ? WHERE name = ?",
                (now_iso, name)
            )

    def _syncable_project_name(self, project: Optional[str]) -> Optional[str]:
        name = _normalize_project_name(project or "")
        if not name or name == "default":
            return None
        return _validate_project_name(name)

    def _source_root_from_paths(self, paths_in: Optional[List[str]]) -> Optional[str]:
        paths: List[str] = []
        for raw in paths_in or []:
            value = str(raw or "").strip()
            if not value:
                continue
            try:
                resolved = self._resolve_path(value).resolve(strict=False)
            except TypeError:
                resolved = self._resolve_path(value).resolve()
            except Exception as exc:
                logger.warning("Skipping invalid project source path %r: %s", value, exc)
                if _fail_hard_enabled():
                    raise RuntimeError(f"Invalid project source path {value!r}") from exc
                continue
            if resolved.exists() and resolved.is_file():
                resolved = resolved.parent
            elif not resolved.exists() and resolved.suffix:
                resolved = resolved.parent
            paths.append(str(resolved))
        if not paths:
            return None
        try:
            return os.path.commonpath(paths)
        except ValueError:
            return paths[0]

    def _source_root_from_sources(self, source_files: Optional[List[str]]) -> Optional[str]:
        return self._source_root_from_paths(source_files)

    def _canonical_path_from_doc_path(self, name: str, file_path: Optional[str]) -> Optional[Path]:
        if not file_path:
            return None
        try:
            resolved = self._resolve_path(str(file_path)).resolve(strict=False)
        except TypeError:
            resolved = self._resolve_path(str(file_path)).resolve()
        except Exception as exc:
            logger.warning("Failed resolving canonical project path from %r: %s", file_path, exc)
            if _fail_hard_enabled():
                raise RuntimeError(f"Failed resolving canonical project path from {file_path!r}") from exc
            return None
        projects_root = (_visible_home() / "projects").resolve()
        try:
            rel = resolved.relative_to(projects_root)
        except ValueError:
            return None
        if rel.parts and rel.parts[0] == name:
            return projects_root / name
        return None

    def _ensure_project_scaffold(
        self,
        name: str,
        canonical: Path,
        *,
        description: str,
        source_root: Optional[str],
        write_project_md: bool,
    ) -> None:
        name = _validate_project_name(name)
        _validate_inside_workspace(canonical, "project directory")
        canonical.mkdir(parents=True, exist_ok=True)
        (canonical / "docs").mkdir(exist_ok=True)
        project_md = canonical / "PROJECT.md"
        if write_project_md:
            _write_text_if_missing(
                project_md,
                render_project_md_template(
                    label=name.replace("-", " ").replace("_", " ").title(),
                    description=description or f"{name} project.",
                    project_home=str(canonical),
                    source_roots=[source_root] if source_root else [],
                    exclude_patterns=[],
                ),
            )

    def _home_dir_for_project_definition(self, canonical: Path) -> str:
        try:
            rel = canonical.resolve().relative_to(_visible_home().resolve())
            value = str(rel)
        except ValueError:
            value = str(canonical)
        return value.rstrip("/") + "/"

    def _ensure_docs_project_definition(
        self,
        name: str,
        *,
        canonical: Path,
        description: str,
        source_root: Optional[str],
    ) -> None:
        from config import ProjectDefinition

        if self.get_project_definition(name) is not None:
            return
        defn = ProjectDefinition(
            label=name.replace("-", " ").replace("_", " ").title(),
            home_dir=self._home_dir_for_project_definition(canonical),
            source_roots=[source_root] if source_root else [],
            auto_index=True,
            patterns=["*.md"],
            exclude=["*.db", "*.log", "*.pyc", "__pycache__/"],
            description=description or f"{name} project.",
            state="active",
        )
        self._write_project_definition_row(name, defn)

    def _ensure_global_project_entry(
        self,
        project: Optional[str],
        *,
        file_path: Optional[str] = None,
        source_files: Optional[List[str]] = None,
        defn: Optional[Any] = None,
        create_scaffold: bool = True,
        link_current_instance: bool = True,
    ) -> bool:
        """Keep docs registry project labels backed by the canonical project registry."""
        name = self._syncable_project_name(project)
        if not name:
            return False
        try:
            from lib.project_registry import is_deleted as global_project_deleted

            if global_project_deleted(name):
                return False
        except Exception as exc:
            logger.warning("Project delete marker check failed for %s: %s", name, exc)
            if _fail_hard_enabled():
                raise RuntimeError(f"Failed checking project delete marker for {name!r}: {exc}") from exc

        if defn is None:
            try:
                defn = self.get_project_definition(name)
            except Exception as exc:
                logger.warning("Failed loading project definition for %s during global sync: %s", name, exc)
                if _fail_hard_enabled():
                    raise RuntimeError(f"Failed loading project definition for {name!r}") from exc
                defn = None

        canonical: Optional[Path] = None
        description = f"{name} project."
        source_root: Optional[str] = None
        if defn is not None:
            description = str(getattr(defn, "description", "") or description)
            roots = list(getattr(defn, "source_roots", []) or [])
            source_root = str(roots[0]) if roots else None
            home_dir = str(getattr(defn, "home_dir", "") or "").strip()
            if home_dir:
                canonical = self._resolve_path(home_dir).resolve()

        if canonical is None:
            canonical = self._canonical_path_from_doc_path(name, file_path)
        if source_root is None:
            source_root = self._source_root_from_sources(source_files)
        if canonical is None:
            canonical = (_visible_home() / "projects" / name).resolve()
            if source_root is None:
                source_root = self._source_root_from_paths([file_path] if file_path else None)
        _validate_inside_workspace(canonical, "project directory")

        if create_scaffold:
            project_dir_existed = canonical.exists()
            self._ensure_project_scaffold(
                name,
                canonical,
                description=description,
                source_root=source_root,
                write_project_md=not project_dir_existed,
            )
        elif not canonical.exists():
            return False
        if defn is None:
            self._ensure_docs_project_definition(
                name,
                canonical=canonical,
                description=description,
                source_root=source_root,
            )

        try:
            from lib.project_registry import lookup as global_lookup
            from lib.project_registry import register as global_register

            existing_global = global_lookup(name) or {}
            existing_description = str(existing_global.get("description") or "").strip()
            existing_path = str(existing_global.get("canonical_path") or "").strip()
            if existing_description and existing_path:
                try:
                    existing_resolved = Path(existing_path).expanduser().resolve(strict=False)
                    canonical_resolved = canonical.resolve(strict=False)
                    if existing_resolved == canonical_resolved:
                        description = existing_description
                except OSError as exc:
                    if _fail_hard_enabled():
                        raise RuntimeError(
                            f"Failed resolving project registry path for {name!r}: {exc}"
                        ) from exc
                    logger.warning("Project registry path resolution failed for %s: %s", name, exc)
            global_register(
                name=name,
                canonical_path=str(canonical),
                description=description,
                source_root=source_root,
                link_current_instance=link_current_instance,
            )
            return True
        except Exception as exc:
            message = f"Failed to sync docs project {name!r} into project registry: {exc}"
            logger.warning(message, exc_info=True)
            if _fail_hard_enabled():
                raise RuntimeError(message) from exc
            return False

    def reconcile_global_project_registry(self) -> Dict[str, Any]:
        """Promote active docs-registry project metadata into the canonical registry.

        This is an invariant repair, not a compatibility layer: docs rows with a
        project label must not leave `quaid project list/show` blind to that
        project.
        """
        self.ensure_table()
        synced: List[str] = []
        seen: set[str] = set()

        for name, defn in sorted((self.get_all_project_definitions() or {}).items()):
            if self._ensure_global_project_entry(name, defn=defn, link_current_instance=False):
                synced.append(str(name))
                seen.add(str(name))

        orphan_paths_by_project: Dict[str, List[str]] = {}
        with get_connection(self.db_path) as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'doc_registry'"
            ).fetchone()
            if not exists:
                return {"synced": len(synced), "projects": sorted(set(synced))}
            rows = conn.execute(
                """
                SELECT project, file_path, source_files
                FROM doc_registry
                WHERE state = 'active'
                  AND project IS NOT NULL
                  AND TRIM(project) != ''
                  AND project != 'default'
                """
            ).fetchall()
            for row in rows:
                name = str(row["project"] or "").strip()
                if not name or name in seen:
                    continue
                paths = orphan_paths_by_project.setdefault(name, [])
                raw_path = str(row["file_path"] or "").strip()
                if raw_path:
                    paths.append(raw_path)
                try:
                    source_paths = json.loads(row["source_files"] or "[]")
                except (TypeError, ValueError):
                    source_paths = []
                if isinstance(source_paths, list):
                    paths.extend(str(p or "").strip() for p in source_paths if str(p or "").strip())

        for name, paths in sorted(orphan_paths_by_project.items()):
            doc_path = paths[0] if paths else None
            if self._ensure_global_project_entry(
                name,
                file_path=doc_path,
                source_files=paths,
                link_current_instance=False,
            ):
                synced.append(name)
                seen.add(name)

        return {"synced": len(synced), "projects": sorted(set(synced))}

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
        link_current_instance: bool = True,
        allow_project_reassign: bool = False,
    ) -> int:
        """Register a document in the registry. Returns the row ID."""
        project = _normalize_project_name(project) or "default"
        if project != "default":
            project = _validate_project_name(project)
        if not file_path or not file_path.strip():
            raise ValueError("file_path must be a non-empty string")
        raw_path = file_path.strip()
        resolved = self._resolve_path(raw_path).resolve()
        tmp_dir = Path(tempfile.gettempdir()).resolve()
        if Path(raw_path).expanduser().is_absolute() and _path_is_under(resolved, tmp_dir):
            raise ValueError(
                f"Cannot register a file under a temporary directory ({tmp_dir}). "
                "Quaid only tracks durable file paths that persist across reboots. "
                "Move the file to a permanent location first."
            )
        self._ensure_global_project_entry(
            project,
            file_path=file_path,
            source_files=source_files,
            link_current_instance=link_current_instance,
        )
        with get_connection(self.db_path) as conn:
            self._ensure_doc_registry_table(conn)
            existing = conn.execute(
                "SELECT project, state FROM doc_registry WHERE file_path = ?",
                (file_path,),
            ).fetchone()
            if (
                existing
                and existing[1] == "active"
                and existing[0] != project
                and not allow_project_reassign
            ):
                raise ValueError(
                    f"File '{file_path}' is already registered to project '{existing[0]}'. "
                    "Use move-file to reassign ownership explicitly."
                )
            now_iso = _now_iso()
            conn.execute("""
                INSERT INTO doc_registry
                    (file_path, project, asset_type, title, description, tags,
                     auto_update, source_files, source_channel, source_conversation_id,
                     source_author_id, speaker_entity_id, subject_entity_id, conversation_id,
                     visibility_scope, sensitivity, participant_entity_ids,
                     provenance_confidence, registered_at, registered_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    -- Re-registering a doc must force reindex so vec/doc chunks stay in sync.
                    -- Otherwise stale last_indexed_at can cause docs update --apply to skip it.
                    last_indexed_at = NULL,
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
                now_iso,
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
            entry = self._row_to_dict(row)
            return entry if _docs_project_visible_to_current_instance(entry.get("project")) else None

    def _registry_path_semantic_key(self, file_path: str) -> Optional[str]:
        """Resolve a registry path for ownership comparisons, ignoring spelling."""
        value = str(file_path or "").strip()
        if not value:
            return None
        try:
            return str(self._resolve_path(value).resolve(strict=False))
        except Exception as exc:
            logger.warning("Failed resolving docs registry path %r for semantic comparison: %s", value, exc)
            if _fail_hard_enabled():
                raise
            return None

    def _active_registry_path_indexes(self) -> tuple[set[str], set[str]]:
        """Return exact and resolved active registry paths without instance visibility filtering."""
        exact_paths: set[str] = set()
        semantic_paths: set[str] = set()
        with get_connection(self.db_path) as conn:
            self._ensure_doc_registry_table(conn)
            rows = conn.execute(
                "SELECT file_path FROM doc_registry WHERE state = 'active'"
            ).fetchall()
        for row in rows:
            file_path = str(row["file_path"] or "").strip()
            if not file_path:
                continue
            exact_paths.add(file_path)
            key = self._registry_path_semantic_key(file_path)
            if key:
                semantic_paths.add(key)
        return exact_paths, semantic_paths

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
            project = _normalize_project_name(project)
            query += " AND project = ?"
            params.append(project)
        if asset_type:
            query += " AND asset_type = ?"
            params.append(asset_type)

        query += " ORDER BY project, file_path"

        with get_connection(self.db_path) as conn:
            rows = conn.execute(query, params).fetchall()
            entries = [self._row_to_dict(r) for r in rows]
            return [
                entry
                for entry in entries
                if _docs_project_visible_to_current_instance(entry.get("project"))
            ]

    def read(self, identifier: str) -> Optional[Dict[str, Any]]:
        """Read a document by file path or title. Returns entry + content."""
        with get_connection(self.db_path) as conn:
            def _first_visible(rows) -> Optional[Dict[str, Any]]:
                for candidate in rows:
                    entry = self._row_to_dict(candidate)
                    if _docs_project_visible_to_current_instance(entry.get("project")):
                        return entry
                return None

            entry = _first_visible(
                conn.execute(
                    "SELECT * FROM doc_registry WHERE file_path = ? AND state = 'active'",
                    (identifier,)
                ).fetchall()
            )
            if entry is None:
                entry = _first_visible(
                    conn.execute(
                        "SELECT * FROM doc_registry WHERE title = ? AND state = 'active' ORDER BY project, file_path",
                        (identifier,)
                    ).fetchall()
                )
            safe_id = identifier.replace("%", "\\%").replace("_", "\\_")
            if entry is None:
                entry = _first_visible(
                    conn.execute(
                        "SELECT * FROM doc_registry WHERE title LIKE ? ESCAPE '\\' AND state = 'active' "
                        "ORDER BY project, file_path",
                        (f"%{safe_id}%",)
                    ).fetchall()
                )
            if entry is None:
                entry = _first_visible(
                    conn.execute(
                        "SELECT * FROM doc_registry WHERE file_path LIKE ? ESCAPE '\\' AND state = 'active' "
                        "ORDER BY project, file_path",
                        (f"%{safe_id}%",)
                    ).fetchall()
                )
            if entry is None:
                return None

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
        nullable = {"title", "description", "source_files", "source_channel", "source_conversation_id",
                    "source_author_id", "speaker_entity_id", "subject_entity_id",
                    "conversation_id", "visibility_scope", "sensitivity",
                    "participant_entity_ids", "provenance_confidence"}
        updates = {k: v for k, v in kwargs.items() if k in allowed and (v is not None or k in nullable)}
        if not updates:
            return False

        # Serialize JSON fields
        if "tags" in updates:
            updates["tags"] = json.dumps(updates["tags"])
        if "source_files" in updates and updates["source_files"] is not None:
            updates["source_files"] = json.dumps(updates["source_files"])
        if "participant_entity_ids" in updates and updates["participant_entity_ids"] is not None:
            updates["participant_entity_ids"] = json.dumps(updates["participant_entity_ids"])
        for field in (
            "source_channel",
            "source_conversation_id",
            "source_author_id",
            "speaker_entity_id",
            "subject_entity_id",
            "conversation_id",
            "visibility_scope",
            "sensitivity",
        ):
            if field in updates:
                normalized = str(updates[field] or "").strip()
                if field == "source_channel":
                    normalized = normalized.lower()
                updates[field] = normalized or None
        if "auto_update" in updates:
            updates["auto_update"] = 1 if updates["auto_update"] else 0
        if "project" in updates:
            updates["project"] = _normalize_project_name(updates["project"]) or "default"
            if updates["project"] != "default":
                updates["project"] = _validate_project_name(updates["project"])

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
        abs_path = str(self._resolve_path(file_path))
        project_definitions = self.get_all_project_definitions()

        # 1. Inside a project's homeDir
        for name, defn in project_definitions.items():
            if not _docs_project_visible_to_current_instance(name):
                continue
            home = str(self._resolve_path(defn.home_dir)).rstrip("/") + "/"
            if abs_path.startswith(home) or abs_path == home.rstrip("/"):
                return name

        # 2. Registered in doc_registry
        entry = self.get(file_path)
        if entry:
            return entry["project"]

        # 3. Under a project's sourceRoots
        for name, defn in project_definitions.items():
            if not _docs_project_visible_to_current_instance(name):
                continue
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
                        project = row["project"]
                        if _docs_project_visible_to_current_instance(project):
                            return project
                        continue
                except (json.JSONDecodeError, KeyError):
                    continue
        return None

    def _ownership_path(self, value: str) -> Optional[Path]:
        """Resolve trusted registry metadata for ownership comparisons."""
        text = str(value or "").strip()
        if not text:
            return None
        try:
            path = Path(text).expanduser()
            if path.is_absolute():
                return path.resolve(strict=False)
            return self._resolve_path(text).resolve(strict=False)
        except Exception as exc:
            logger.warning("Failed resolving project ownership path %r: %s", text, exc)
            if _fail_hard_enabled():
                raise
            return None

    @staticmethod
    def _path_is_inside_or_equal(path: Path, root: Path) -> bool:
        try:
            resolved = path.resolve(strict=False)
            root_resolved = root.resolve(strict=False)
            return resolved == root_resolved or resolved.is_relative_to(root_resolved)
        except (OSError, ValueError):
            return False

    def _find_project_for_path_unfiltered(self, file_path: str) -> Optional[str]:
        """Determine project ownership for repair/sync work without instance visibility filtering."""
        abs_path = self._ownership_path(file_path)
        if abs_path is None:
            return None

        for name, defn in self.get_all_project_definitions().items():
            home = self._ownership_path(defn.home_dir)
            if home is not None and self._path_is_inside_or_equal(abs_path, home):
                return name

        with get_connection(self.db_path) as conn:
            rows = conn.execute(
                "SELECT project, file_path, source_files FROM doc_registry WHERE state = 'active'"
            ).fetchall()
        for row in rows:
            row_path = self._ownership_path(str(row["file_path"] or ""))
            if row_path is not None and self._path_is_inside_or_equal(abs_path, row_path):
                return str(row["project"] or "").strip() or None

        for name, defn in self.get_all_project_definitions().items():
            for root in defn.source_roots:
                root_path = self._ownership_path(root)
                if root_path is not None and self._path_is_inside_or_equal(abs_path, root_path):
                    return name

        for row in rows:
            try:
                sources = json.loads(row["source_files"] or "[]")
            except (TypeError, ValueError):
                sources = []
            if not isinstance(sources, list):
                continue
            for source in sources:
                source_path = self._ownership_path(str(source or ""))
                if source_path is not None and self._path_is_inside_or_equal(abs_path, source_path):
                    return str(row["project"] or "").strip() or None

        try:
            from lib.project_registry import list_all as global_project_list_all

            global_projects = global_project_list_all()
        except Exception as exc:
            logger.warning("Failed reading global project registry for sync ownership: %s", exc)
            if _fail_hard_enabled():
                raise RuntimeError("Failed reading global project registry for sync ownership") from exc
            global_projects = {}
        for name, entry in sorted((global_projects or {}).items()):
            roots = [entry.get("canonical_path"), entry.get("source_root")]
            for root in roots:
                root_path = self._ownership_path(str(root or ""))
                if root_path is not None and self._path_is_inside_or_equal(abs_path, root_path):
                    return str(name or "").strip() or None

        return None

    def get_source_mappings(self, project: Optional[str] = None) -> Dict[str, List[str]]:
        """Get doc->sources mapping for mtime staleness checks.

        Returns: {doc_path: [source_path, ...]}
        """
        query = """
            SELECT file_path, project, source_files FROM doc_registry
            WHERE state = 'active' AND auto_update = 1 AND source_files IS NOT NULL
        """
        params: list = []
        if project:
            project = _normalize_project_name(project)
            query += " AND project = ?"
            params.append(project)

        result: Dict[str, List[str]] = {}
        with get_connection(self.db_path) as conn:
            rows = conn.execute(query, params).fetchall()
            for row in rows:
                if not _docs_project_visible_to_current_instance(row["project"]):
                    continue
                try:
                    sources = json.loads(row["source_files"] or "[]")
                except (json.JSONDecodeError, KeyError, TypeError) as exc:
                    message = f"Malformed source_files metadata for docs registry row {row['file_path']!r}"
                    logger.warning("%s: %s", message, exc)
                    if _fail_hard_enabled():
                        raise RuntimeError(message) from exc
                    result[row["file_path"]] = [_malformed_source_files_marker(row["file_path"])]
                    continue
                if not isinstance(sources, list):
                    message = (
                        f"Malformed source_files metadata for docs registry row {row['file_path']!r}: "
                        f"expected list, got {type(sources).__name__}"
                    )
                    logger.warning("%s", message)
                    if _fail_hard_enabled():
                        raise RuntimeError(message)
                    result[row["file_path"]] = [_malformed_source_files_marker(row["file_path"])]
                    continue
                if sources:
                    result[row["file_path"]] = sources
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
        reload_config: bool = True,
        quiet: bool = False,
    ) -> Path:
        """Scaffold a new project directory with PROJECT.md template.

        Creates directory, writes PROJECT.md, adds to config/config.json,
        and registers PROJECT.md in the doc registry.

        Returns path to created PROJECT.md.
        """
        name = _validate_project_name(name)

        # Guard: don't overwrite an existing project
        if self.get_project_definition(name) is not None:
            raise ValueError(
                f"Project '{name}' already exists. Use `quaid project rename` or `quaid project delete` first."
            )

        label = label or name.replace("-", " ").title()
        home = home_dir or f"projects/{name}/"
        home_abs = self._resolve_path(home)
        _validate_inside_workspace(home_abs, "project directory")
        home_abs.mkdir(parents=True, exist_ok=True)
        # Create docs/ subdirectory for project documentation
        (home_abs / "docs").mkdir(exist_ok=True)

        exclude_list = exclude or ["*.db", "*.log", "*.pyc", "__pycache__/"]
        project_md = render_project_md_template(
            label=label,
            description=description or f"{label} project.",
            project_home=str(home_abs),
            source_roots=[str(self._resolve_path(root)) for root in (source_roots or [])],
            exclude_patterns=exclude_list,
        )

        project_md_path = home_abs / "PROJECT.md"
        _write_text_if_missing(project_md_path, project_md)

        # Save project definition to DB (source of truth)
        from config import ProjectDefinition
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
        if reload_config and _reload_config_after_project_change("create_project"):
            self._config = None

        # Register PROJECT.md in the doc registry
        rel_path = _to_registry_path(project_md_path)
        self.register(
            file_path=rel_path,
            project=name,
            asset_type="doc",
            title=f"Project: {label}",
            registered_by="create-project",
        )
        _generate_project_md(self, name, self._get_config(), quiet=quiet)

        if not quiet:
            print(f"Created project '{name}' at {home_abs}")
            print(f"  PROJECT.md: {project_md_path}")

        return project_md_path

    def auto_discover(self, project_name: str) -> List[str]:
        """Scan a project directory for unregistered files. Register them.

        Returns list of newly registered file paths.
        """
        project_name = _validate_project_name(project_name)
        defn = self.get_project_definition(project_name)
        if not defn:
            print(f"Project '{project_name}' not found in project definitions")
            return []

        home_abs = self._resolve_path(defn.home_dir)
        if not home_abs.exists():
            print(f"Project home does not exist: {home_abs}")
            return []

        exclude_patterns = defn.exclude or []
        file_patterns = defn.patterns or ["*.md"]
        newly_registered = []

        for file_path in _iter_project_files(home_abs, file_patterns, f"auto_discover:{project_name}"):
            # Check exclusions
            rel_path = _to_registry_path(file_path)
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
        project_name = _validate_project_name(project_name)
        defn = self.get_project_definition(project_name)
        if not defn:
            return []

        project_md_path = self._resolve_path(defn.home_dir) / "PROJECT.md"
        if not project_md_path.exists():
            return []

        content = project_md_path.read_text(encoding="utf-8")
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
                    raw_file_path = parts[0].strip("`").strip()
                    expanded_file_path = Path(raw_file_path).expanduser()
                    file_path = raw_file_path
                    if expanded_file_path.is_absolute():
                        # Absolute path — make relative to workspace
                        try:
                            file_path = _to_registry_path(expanded_file_path)
                        except ValueError as exc:
                            logger.warning(
                                "External file path %s is outside registry roots; keeping absolute path: %s",
                                file_path,
                                exc,
                            )
                            if _fail_hard_enabled():
                                raise RuntimeError(f"External file path is outside registry roots: {file_path}") from exc
                            # Keep as-is in fail-open mode for existing external-doc workflows.
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
        exact_registered, semantic_registered = self._active_registry_path_indexes()
        candidates: List[Dict[str, str]] = []
        with get_connection(self.db_path) as conn:
            # Get all unique source_files from doc_chunks
            rows = conn.execute(
                "SELECT DISTINCT source_file FROM doc_chunks"
            ).fetchall()

            for row in rows:
                abs_path = str(row[0] or "").strip()
                if not abs_path:
                    continue
                source_path = Path(abs_path)
                if source_path.is_absolute():
                    try:
                        rel_path = _to_registry_path(source_path)
                    except ValueError:
                        rel_path = abs_path
                else:
                    rel_path = abs_path

                semantic_key = self._registry_path_semantic_key(rel_path)
                if rel_path in exact_registered or (semantic_key and semantic_key in semantic_registered):
                    continue

                # Determine project from path without current-instance visibility filtering.
                project = self._find_project_for_path_unfiltered(rel_path) or "default"
                if project == "default" and (
                    rel_path == "projects"
                    or (
                        rel_path.startswith("projects/")
                        and rel_path != "projects/staging"
                        and not rel_path.startswith("projects/staging/")
                    )
                ):
                    logger.warning(
                        "Skipping docs chunk source %s during registry sync: project ownership is unresolved",
                        rel_path,
                    )
                    continue
                title = self._extract_title_from_path(abs_path)
                candidates.append({
                    "file_path": rel_path,
                    "project": project,
                    "title": title or "",
                })
                exact_registered.add(rel_path)
                if semantic_key:
                    semantic_registered.add(semantic_key)

        with get_connection(self.db_path) as conn:
            self._ensure_doc_registry_table(conn)
            now_iso = _now_iso()
            for candidate in candidates:
                conn.execute("""
                    INSERT INTO doc_registry
                        (file_path, project, asset_type, title, tags,
                         auto_update, registered_at, registered_by)
                    VALUES (?, ?, 'doc', ?, '[]', 0, ?, 'sync-from-chunks')
                    ON CONFLICT(file_path) DO UPDATE SET
                        project = excluded.project,
                        asset_type = excluded.asset_type,
                        title = COALESCE(excluded.title, doc_registry.title),
                        tags = excluded.tags,
                        auto_update = excluded.auto_update,
                        last_indexed_at = NULL,
                        state = 'active',
                        registered_by = excluded.registered_by
                """, (
                    candidate["file_path"],
                    candidate["project"],
                    candidate["title"] or None,
                    now_iso,
                ))

        for candidate in candidates:
            print(f"  Synced from chunks: {candidate['file_path']} -> project={candidate['project']}")

        return len(candidates)

    # ========================================================================
    # Bulk Project Operations
    # ========================================================================

    def rename_project(self, old_name: str, new_name: str) -> Dict[str, Any]:
        """Rename a project: update all registry entries, move directory, update config.

        Returns: {"renamed": count, "dir_moved": bool}
        """
        old_name = _validate_project_name(old_name)
        new_name = _validate_project_name(new_name)

        # Guard: old project must exist (in project definitions or registry)
        db_defn = self.get_project_definition(old_name)
        has_definition = db_defn is not None
        has_docs = len(self.list_docs(project=old_name)) > 0
        if not has_definition and not has_docs:
            raise ValueError(f"Project '{old_name}' does not exist.")

        # Guard: don't merge into an existing project
        existing_docs = self.list_docs(project=new_name)
        existing_definition = self.get_project_definition(new_name)
        if existing_docs:
            raise ValueError(
                f"Cannot rename to '{new_name}': project already has {len(existing_docs)} registered docs. "
                "Use move-file to migrate docs individually."
            )
        if existing_definition:
            raise ValueError(f"Cannot rename to '{new_name}': project already exists.")

        old_prefix = str(db_defn.home_dir or "").rstrip("/") if db_defn else None
        new_prefix = f"projects/{new_name}"
        if db_defn:
            db_defn.home_dir = f"projects/{new_name}/"
            if db_defn.source_roots:
                updated_roots = []
                for root in db_defn.source_roots:
                    if isinstance(root, str) and old_prefix and root.startswith(old_prefix):
                        updated_roots.append(root.replace(old_prefix, new_prefix, 1))
                    else:
                        updated_roots.append(root)
                db_defn.source_roots = updated_roots

        # Move directory before mutating DB paths. If this fails, registry state
        # remains unchanged instead of pointing at a directory that never moved.
        dir_moved = False
        old_dir: Optional[Path] = None
        new_dir: Optional[Path] = None
        if old_prefix:
            old_dir = self._resolve_path(old_prefix)
            new_dir = _visible_home() / "projects" / new_name
            _validate_inside_workspace(new_dir, "target directory")
            if old_dir.exists() and not new_dir.exists():
                old_dir.rename(new_dir)
                dir_moved = True

        try:
            with get_connection(self.db_path) as conn:
                # Get rows BEFORE renaming the project field.
                rows = conn.execute(
                    "SELECT id, file_path FROM doc_registry WHERE project = ? AND state = 'active'",
                    (old_name,),
                ).fetchall()

                # Update project name for all entries.
                cursor = conn.execute(
                    "UPDATE doc_registry SET project = ? WHERE project = ? AND state = 'active'",
                    (new_name, old_name),
                )
                renamed = cursor.rowcount

                # Update in-directory file paths only after the filesystem move succeeded.
                if old_prefix:
                    for row in rows:
                        if row["file_path"].startswith(old_prefix):
                            new_path = row["file_path"].replace(old_prefix, new_prefix, 1)
                            conn.execute(
                                "UPDATE doc_registry SET file_path = ? WHERE id = ?",
                                (new_path, row["id"]),
                            )
                    chunk_replacements = [(old_prefix, new_prefix)]
                    if old_dir is not None and new_dir is not None:
                        chunk_replacements.append((str(old_dir), str(new_dir)))
                    self._rewrite_doc_chunk_source_prefixes_on_conn(conn, chunk_replacements)

                if db_defn:
                    self._write_project_definition_row_on_conn(conn, new_name, db_defn)
                    now_iso = _now_iso()
                    conn.execute(
                        "UPDATE project_definitions SET state = 'deleted', updated_at = ? WHERE name = ?",
                        (now_iso, old_name),
                    )
        except Exception:
            if dir_moved and old_dir is not None and new_dir is not None and new_dir.exists() and not old_dir.exists():
                try:
                    new_dir.rename(old_dir)
                except OSError as rollback_exc:
                    logger.error(
                        "Failed rolling back project directory rename %s -> %s after registry error: %s",
                        new_dir,
                        old_dir,
                        rollback_exc,
                    )
            raise

        # Reload config
        if _reload_config_after_project_change("rename_project"):
            self._config = None

        try:
            from lib.project_registry import rename as rename_global_project
            rename_global_project(old_name, new_name, canonical_path=str(_visible_home() / "projects" / new_name))
        except Exception as e:
            if _fail_hard_enabled():
                raise RuntimeError(
                    f"Failed to rename global project registry entry {old_name!r} -> {new_name!r}"
                ) from e
            logger.warning("Global project registry rename skipped: %s", e)

        if db_defn:
            try:
                self._ensure_global_project_entry(new_name, defn=db_defn)
            except Exception as e:
                if _fail_hard_enabled():
                    raise RuntimeError(f"Failed to sync global project registry entry {new_name!r}") from e
                logger.warning("Global project registry sync after rename skipped: %s", e)

        try:
            from datastore.docsdb.project_updater import refresh_project_md
            refresh_project_md(new_name)
        except Exception as e:
            if _fail_hard_enabled():
                raise RuntimeError(f"PROJECT.md refresh after rename failed for {new_name!r}") from e
            logger.warning("PROJECT.md refresh after rename skipped: %s", e)

        result = {"renamed": renamed, "dir_moved": dir_moved}
        print(f"Renamed project '{old_name}' -> '{new_name}': {renamed} docs updated, dir_moved={dir_moved}")
        return result

    def _rewrite_doc_chunk_source_prefixes_on_conn(
        self,
        conn: sqlite3.Connection,
        replacements: List[Tuple[str, str]],
    ) -> int:
        """Rewrite docs RAG source paths after a project directory rename."""
        updated = 0
        seen: set[Tuple[str, str]] = set()
        try:
            for old_prefix, new_prefix in replacements:
                old_base = str(old_prefix or "").rstrip("/")
                new_base = str(new_prefix or "").rstrip("/")
                key = (old_base, new_base)
                if not old_base or not new_base or key in seen:
                    continue
                seen.add(key)
                exact = conn.execute(
                    "UPDATE doc_chunks SET source_file = ? WHERE source_file = ?",
                    (new_base, old_base),
                )
                updated += max(exact.rowcount, 0)

                old_with_sep = f"{old_base}/"
                new_with_sep = f"{new_base}/"
                cursor = conn.execute(
                    """
                    UPDATE doc_chunks
                    SET source_file = ? || substr(source_file, ?)
                    WHERE substr(source_file, 1, ?) = ?
                    """,
                    (new_with_sep, len(old_with_sep) + 1, len(old_with_sep), old_with_sep),
                )
                updated += max(cursor.rowcount, 0)
        except sqlite3.OperationalError as exc:
            if "no such table: doc_chunks" in str(exc).lower():
                return updated
            raise
        return updated

    def _remove_project_rag_chunks(
        self,
        project_name: str,
        defn: Optional[Any],
        *,
        states: Tuple[str, ...],
    ) -> int:
        """Remove docs RAG chunks for project-owned document paths."""
        rag_paths: list[str] = []
        for state in states:
            for row in self.list_docs(project=project_name, state=state):
                path_val = str(row.get("file_path") or "").strip()
                if path_val:
                    rag_paths.append(path_val)
        if defn:
            rag_paths.append(str(self._resolve_path(defn.home_dir) / "PROJECT.md"))

        seen_paths: set[str] = set()
        unique_rag_paths: list[str] = []
        for candidate in rag_paths:
            key = str(candidate).strip()
            if not key or key in seen_paths:
                continue
            seen_paths.add(key)
            unique_rag_paths.append(key)
        if not unique_rag_paths:
            return 0

        rag_chunk_deleted = 0
        try:
            from datastore.docsdb.rag import DocsRAG

            rag = DocsRAG(self.db_path)
            for candidate in unique_rag_paths:
                removed = rag.remove_chunks_for_path(candidate)
                rag_chunk_deleted += int(removed) if removed is not None else 0
        except Exception as e:
            if _fail_hard_enabled():
                raise RuntimeError(f"Docs RAG cleanup failed for project {project_name!r}") from e
            logger.warning("Docs RAG cleanup skipped for project '%s': %s", project_name, e)
        return rag_chunk_deleted

    def archive_project(self, project_name: str) -> Dict[str, Any]:
        """Archive a project: set all entries to archived state, move dir to archive/.

        Returns: {"archived": count, "dir_moved": bool}
        """
        project_name = _validate_project_name(project_name)
        # Guard: project must exist (in project definitions or registry)
        defn = self.get_project_definition(project_name)
        has_definition = defn is not None
        has_docs = len(self.list_docs(project=project_name)) > 0
        if not has_definition and not has_docs:
            raise ValueError(f"Project '{project_name}' does not exist.")

        dir_moved = False
        src_dir: Optional[Path] = None
        dest: Optional[Path] = None
        archived = 0
        rag_chunk_deleted = 0
        try:
            # Move directory before mutating DB state. If this fails, registry
            # rows stay active instead of pointing at an archive that never happened.
            if defn:
                src_dir = self._resolve_path(defn.home_dir)
                archive_dir = _visible_home() / "projects" / "archive"
                if src_dir.exists():
                    _validate_inside_workspace(src_dir, "archive source")
                    _validate_inside_workspace(archive_dir, "archive directory")
                    archive_dir.mkdir(parents=True, exist_ok=True)
                    dest = archive_dir / project_name
                    src_dir.rename(dest)
                    dir_moved = True

            # Archived project content must stop participating in docs recall.
            rag_chunk_deleted = self._remove_project_rag_chunks(project_name, defn, states=("active",))

            # Archive registry rows and project definition atomically.
            with get_connection(self.db_path) as conn:
                cursor = conn.execute(
                    "UPDATE doc_registry SET state = 'archived' WHERE project = ? AND state = 'active'",
                    (project_name,),
                )
                archived = cursor.rowcount
                self._ensure_project_definitions_table(conn)
                conn.execute(
                    "UPDATE project_definitions SET state = 'deleted', updated_at = ? WHERE name = ?",
                    (_now_iso(), project_name),
                )
        except Exception:
            if dir_moved and src_dir is not None and dest is not None and dest.exists() and not src_dir.exists():
                try:
                    dest.rename(src_dir)
                except OSError as rollback_exc:
                    logger.error(
                        "Failed rolling back project directory archive %s -> %s after archive error: %s",
                        dest,
                        src_dir,
                        rollback_exc,
                    )
            raise

        # Reload config
        if _reload_config_after_project_change("archive_project"):
            self._config = None

        result = {"archived": archived, "dir_moved": dir_moved, "rag_chunks_deleted": rag_chunk_deleted}
        print(
            f"Archived project '{project_name}': {archived} docs archived, "
            f"rag_chunks_deleted={rag_chunk_deleted}, dir_moved={dir_moved}"
        )
        return result

    def delete_project(self, project_name: str) -> Dict[str, Any]:
        """Delete a project: remove all registry entries, trash dir, remove from config.

        Uses 'trash' command for recoverability. Falls back to shutil.rmtree
        only if trash is unavailable.

        Returns: {"deleted": count, "dir_deleted": bool}
        """
        project_name = _validate_project_name(project_name)
        # Guard: project must exist (in project definitions or registry)
        defn = self.get_project_definition(project_name)
        has_definition = defn is not None
        has_docs = len(self.list_docs(project=project_name)) > 0
        if not has_definition and not has_docs:
            raise ValueError(f"Project '{project_name}' does not exist.")

        # Clean docs RAG chunks for all project paths before registry state transition.
        # This prevents deleted project content from continuing to inject via recall.
        rag_chunk_deleted = self._remove_project_rag_chunks(
            project_name,
            defn,
            states=("active", "archived"),
        )

        # 1. Bulk delete registry entries
        with get_connection(self.db_path) as conn:
            cursor = conn.execute(
                "UPDATE doc_registry SET state = 'deleted' WHERE project = ? AND state IN ('active', 'archived')",
                (project_name,),
            )
            deleted = cursor.rowcount

        # 2. Remove directory (trash > rm)
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
                except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                    logger.warning("trash command failed for project directory %s: %s", project_dir, exc)
                    if _fail_hard_enabled():
                        raise RuntimeError(f"trash failed for project directory {project_dir}") from exc
                    logger.warning("falling back to rmtree for project directory %s", project_dir)
                    try:
                        shutil.rmtree(project_dir)
                        dir_deleted = True
                    except OSError as rm_exc:
                        logger.warning("Failed to delete project directory %s: %s", project_dir, rm_exc)
                        if _fail_hard_enabled():
                            raise RuntimeError(f"Failed to delete project directory {project_dir}") from rm_exc
                        print(f"  WARNING: Failed to delete project directory. Please manually delete: {project_dir}")
                        dir_deleted = False

        # 3. Remove from DB
        self.delete_project_definition(project_name)

        # Reload config
        if _reload_config_after_project_change("delete_project"):
            self._config = None

        try:
            from lib.project_registry import remove as remove_global_project
            remove_global_project(project_name, force=True)
        except Exception as e:
            if _fail_hard_enabled():
                raise RuntimeError(f"Failed to delete global project registry entry {project_name!r}") from e
            logger.warning("Global project registry delete skipped: %s", e)

        result = {"deleted": deleted, "dir_deleted": dir_deleted, "rag_chunks_deleted": rag_chunk_deleted}
        print(
            f"Deleted project '{project_name}': {deleted} docs removed, "
            f"rag_chunks_deleted={rag_chunk_deleted}, dir_deleted={dir_deleted}"
        )
        return result

    def move_file(self, file_path: str, to_project: str) -> Dict[str, Any]:
        """Reassign a file's project ownership in the registry.

        NOTE: This only updates the registry entry — it does NOT physically
        move the file on disk. Use 'mv' or 'git mv' separately if needed.

        Returns: {"moved": bool, "new_path": str}
        """
        to_project = _validate_project_name(to_project)
        # Guard: target project must exist
        if self.get_project_definition(to_project) is None:
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
            allow_project_reassign=True,
        )

        result = {"moved": True, "new_path": file_path}
        print(f"Moved {file_path} -> project '{to_project}' (registry only)")
        return result

    def verify_project(self, project_name: str) -> Dict[str, Any]:
        """Verify project health: check registered files exist, find orphans.

        Returns: {"total": N, "exists": N, "missing": [...], "orphans": [...]}
        """
        project_name = _validate_project_name(project_name)
        defn = self.get_project_definition(project_name)

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

                for file_path in _iter_project_files(home_abs, file_patterns, f"verify_project:{project_name}"):
                    rel_path = _to_registry_path(file_path)
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

    def _rag_source_variants_for_registry_path(self, file_path: str) -> List[str]:
        variants: List[str] = []
        raw_path = str(file_path or "").strip()
        if not raw_path:
            return variants
        for candidate in (raw_path, str(self._resolve_path(raw_path))):
            value = str(candidate or "").strip()
            if not value or value in variants:
                continue
            variants.append(value)
            try:
                resolved = str(Path(value).expanduser().resolve(strict=False))
            except TypeError:
                resolved = str(Path(value).expanduser().resolve())
            if resolved and resolved not in variants:
                variants.append(resolved)
        return variants

    def _vec_doc_chunks_table_exists(self, conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'vec_doc_chunks' LIMIT 1"
        ).fetchone()
        return row is not None

    def _remove_rag_chunks_for_registry_path(self, conn: sqlite3.Connection, file_path: str) -> int:
        source_variants = self._rag_source_variants_for_registry_path(file_path)
        if not source_variants:
            return 0
        placeholders = ",".join("?" for _ in source_variants)
        try:
            old_chunk_ids = [
                row[0]
                for row in conn.execute(
                    f"SELECT id FROM doc_chunks WHERE source_file IN ({placeholders})",
                    tuple(source_variants),
                ).fetchall()
            ]
        except sqlite3.OperationalError as exc:
            if "no such table: doc_chunks" in str(exc).lower():
                return 0
            raise
        if not old_chunk_ids:
            return 0

        conn.execute(
            f"DELETE FROM doc_chunks WHERE source_file IN ({placeholders})",
            tuple(source_variants),
        )
        if self._vec_doc_chunks_table_exists(conn):
            try:
                conn.executemany(
                    "DELETE FROM vec_doc_chunks WHERE chunk_id = ?",
                    [(chunk_id,) for chunk_id in old_chunk_ids],
                )
            except sqlite3.OperationalError as exc:
                if "no such table: vec_doc_chunks" not in str(exc).lower():
                    raise
        return len(old_chunk_ids)

    def gc(self, dry_run: bool = True) -> Dict[str, Any]:
        """Garbage collect: remove registry entries pointing to missing files.

        Returns: {"removed": [...], "kept": N, "rag_chunks_deleted": N}
        """
        from lib.database import get_connection
        removed = []
        failed = []
        kept = 0
        rag_chunks_deleted = 0
        with get_connection(self.db_path) as conn:
            rows = conn.execute("SELECT id, file_path, project FROM doc_registry WHERE state = 'active'").fetchall()
            for row in rows:
                if not _docs_project_visible_to_current_instance(row["project"]):
                    continue
                abs_path = self._resolve_path(row["file_path"])
                if not abs_path.exists():
                    entry = {"id": row["id"], "file_path": row["file_path"], "project": row["project"]}
                    if not dry_run:
                        savepoint_active = False
                        try:
                            conn.execute("SAVEPOINT docs_registry_gc_row")
                            savepoint_active = True
                            deleted = self._remove_rag_chunks_for_registry_path(conn, row["file_path"])
                            conn.execute("DELETE FROM doc_registry WHERE id = ?", (row["id"],))
                            conn.execute("RELEASE SAVEPOINT docs_registry_gc_row")
                            savepoint_active = False
                            entry["rag_chunks_deleted"] = deleted
                            rag_chunks_deleted += deleted
                        except Exception as exc:
                            if savepoint_active:
                                try:
                                    conn.execute("ROLLBACK TO SAVEPOINT docs_registry_gc_row")
                                finally:
                                    conn.execute("RELEASE SAVEPOINT docs_registry_gc_row")
                            if _fail_hard_enabled():
                                raise RuntimeError(f"Failed to clean docs RAG chunks for {row['file_path']!r}") from exc
                            logger.warning(
                                "Skipping broken docs registry entry %s after RAG chunk cleanup failed: %s",
                                row["file_path"],
                                exc,
                            )
                            failed.append({**entry, "error": str(exc)})
                            continue
                    removed.append(entry)
                else:
                    kept += 1

        result = {"removed": removed, "kept": kept, "rag_chunks_deleted": rag_chunks_deleted}
        if failed:
            result["failed"] = failed
        if removed:
            action = "Would remove" if dry_run else "Removed"
            suffix = f", {rag_chunks_deleted} indexed chunk(s) removed" if not dry_run else ""
            print(f"{action} {len(removed)} broken registry entries ({kept} kept{suffix})")
            for r in removed:
                print(f"  {r['file_path']} (project: {r['project']})")
        else:
            print(f"No broken entries found ({kept} entries all valid)")
        if failed:
            print(f"Failed to remove {len(failed)} broken registry entries; see logs.")
        return result

    # ========================================================================
    # Helpers
    # ========================================================================

    def list_projects(self) -> List[Dict[str, Any]]:
        """List all projects with their metadata and doc counts.

        Returns list of dicts with: name, label, description, doc_count, home_dir.
        """
        projects = []
        for name, defn in sorted(self.get_all_project_definitions().items()):
            if not _docs_project_visible_to_current_instance(name):
                continue
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
        """Apply a mutation to config.json atomically. Returns True if updated."""
        config_path = _workspace() / "config.json"
        try:
            with _config_write_lock(config_path):
                if not config_path.exists():
                    return False
                config_data = json.loads(config_path.read_text(encoding="utf-8"))
                mutator_fn(config_data)
                _atomic_write_text_unlocked(config_path, json.dumps(config_data, indent=2) + "\n")
            # Reload cached config
            try:
                from config import reload_config
                reload_config()
                self._config = None
            except Exception as e:
                print(f"  Warning: config reload failed (stale cache): {e}", file=sys.stderr)
                if _fail_hard_enabled():
                    raise RuntimeError("Config reload failed after docs registry update") from e
            return True
        except Exception as e:
            print(f"  Warning: config update failed: {e}", file=sys.stderr)
            if _fail_hard_enabled():
                raise RuntimeError("Config update failed") from e
            return False

    def _resolve_path(self, relative: str) -> Path:
        """Resolve a workspace-relative path to absolute.

        Canonical project paths live at QUAID_VISIBLE_HOME/projects/, while
        per-instance staging still lives under the instance workspace at
        projects/staging/. Paths starting with 'shared/' also live at
        QUAID_HOME for the remaining shared-config surfaces.
        """
        value = str(relative or "").strip()
        p = Path(value).expanduser()
        if p.is_absolute():
            resolved = p.resolve()
        elif value == "projects" or (
            value.startswith("projects/")
            and value != "projects/staging"
            and not value.startswith("projects/staging/")
        ):
            resolved = (_visible_home() / value).resolve()
        elif value.startswith("shared/"):
            resolved = (get_quaid_home() / value).resolve()
        else:
            resolved = (_workspace() / value).resolve()
        _validate_inside_workspace(resolved, "registry path")
        return resolved

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
        except Exception as exc:
            logger.warning("Failed to extract markdown title from %s: %s", file_path, exc)
        return None

    def _extract_title_from_path(self, abs_path: str) -> Optional[str]:
        """Extract title from file at absolute path."""
        try:
            return self._extract_title(Path(abs_path))
        except Exception as exc:
            logger.warning("Failed title extraction from path %s: %s", abs_path, exc)
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
            "tags": self._decode_json_list(row["tags"], field_name="docs_registry.tags"),
            "state": row["state"],
            "auto_update": bool(row["auto_update"]),
            "source_files": self._decode_json_list(row["source_files"], field_name="docs_registry.source_files"),
            "source_channel": row["source_channel"] if "source_channel" in row.keys() else None,
            "source_conversation_id": row["source_conversation_id"] if "source_conversation_id" in row.keys() else None,
            "source_author_id": row["source_author_id"] if "source_author_id" in row.keys() else None,
            "speaker_entity_id": row["speaker_entity_id"] if "speaker_entity_id" in row.keys() else None,
            "subject_entity_id": row["subject_entity_id"] if "subject_entity_id" in row.keys() else None,
            "conversation_id": row["conversation_id"] if "conversation_id" in row.keys() else None,
            "visibility_scope": row["visibility_scope"] if "visibility_scope" in row.keys() else None,
            "sensitivity": row["sensitivity"] if "sensitivity" in row.keys() else None,
            "participant_entity_ids": self._decode_json_list(
                row["participant_entity_ids"] if "participant_entity_ids" in row.keys() else None,
                field_name="docs_registry.participant_entity_ids",
            ),
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


def _generate_project_md(registry: DocsRegistry, project_name: str, cfg, *, quiet: bool = False) -> None:
    """Generate PROJECT.md from the current registry-backed project state."""
    defn = cfg.projects.definitions.get(project_name)
    if not defn:
        return

    project_home = registry._resolve_path(defn.home_dir)
    project_md_path = project_home / "PROJECT.md"
    sections = _managed_project_sections(registry, project_name, defn)
    existing = project_md_path.read_text(encoding="utf-8") if project_md_path.exists() else ""
    if has_registry_managed_markers(existing):
        content = existing
    else:
        content = render_project_md_template(
            label=defn.label,
            description=defn.description or f"{defn.label} project.",
            project_home=str(project_home),
            source_roots=[str(registry._resolve_path(root)) for root in (defn.source_roots or [])],
            exclude_patterns=defn.exclude or [],
        )
    content = _populate_project_md_sections(
        content,
        project_home_body=sections["project_home"],
        source_roots_body=sections["source_roots"],
        in_dir_body=sections["in_dir"],
        external_body=sections["external"],
        registered_docs_body=sections["registered_docs"],
    )
    _atomic_write_text(project_md_path, content)
    if not quiet:
        print(f"  Generated PROJECT.md at {project_md_path}")


# ============================================================================
# CLI
# ============================================================================

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Document Registry")
    subparsers = parser.add_subparsers(dest="command", help="Command")

    # register
    reg_p = subparsers.add_parser("register", help="Register a document")
    reg_p.add_argument("file_path", help="File path (absolute or current-directory relative)")
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

    # discover
    disc_p = subparsers.add_parser("discover", help="Auto-discover files in project")
    disc_p.add_argument("--project", required=True, help="Project name")

    # sync-external
    sync_ext_p = subparsers.add_parser("sync-external", help="Sync PROJECT.md External Files to registry")
    sync_ext_p.add_argument("--project", required=True, help="Project name")

    # sync (migration)
    sync_p = subparsers.add_parser("sync", help="Migrate existing docs into registry")

    # move-file
    mv_p = subparsers.add_parser("move-file", help="Move a file to a different project")
    mv_p.add_argument("file_path", help="File path to move")
    mv_p.add_argument("--to-project", required=True, help="Target project name")

    # verify
    ver_p = subparsers.add_parser("verify", help="Check project health (missing files, orphans)")
    ver_p.add_argument("--project", required=True, help="Project name to verify")
    ver_p.add_argument("--json", action="store_true", help="JSON output")

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

    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    registry = DocsRegistry()

    if args.command == "register":
        # Handle source_files: CLI accepts space-separated paths or a JSON array string
        source_files = args.source_files
        if source_files and len(source_files) == 1 and source_files[0].startswith("["):
            try:
                source_files = json.loads(source_files[0])
            except json.JSONDecodeError:
                pass  # Not valid JSON, treat as literal path
        try:
            file_path = _cli_register_file_path(args.file_path)
        except (FileNotFoundError, IsADirectoryError) as exc:
            parser.error(str(exc))
        row_id = registry.register(
            file_path=file_path,
            project=args.project,
            asset_type=args.asset_type,
            title=args.title,
            description=args.description,
            auto_update=args.auto_update,
            source_files=source_files,
        )
        indexing = _queue_async_indexing_after_register(args.project)
        indexing_queued = bool(indexing.get("queued"))
        if args.json:
            print(json.dumps({
                "id": row_id,
                "file_path": file_path,
                "project": args.project,
                "indexing": "async" if indexing_queued else "not_queued",
                "indexing_request": indexing,
                "manual_index_command": _manual_index_command(args.project),
                "message": ASYNC_REGISTRATION_NOTICE if indexing_queued else "Async indexing was not queued.",
            }))
        else:
            print(f"Registered: {file_path} (project={args.project}, id={row_id})")
            if indexing_queued:
                print(ASYNC_REGISTRATION_NOTICE)
                request_id = str(indexing.get("request_id") or "").strip()
                if request_id:
                    print(f"  request: {request_id}")
                supervisor_pid = indexing.get("supervisor_pid")
                if supervisor_pid:
                    print(f"  supervisor: {supervisor_pid}")
            else:
                reason = str(indexing.get("error") or indexing.get("reason") or "unknown reason")
                print(f"Warning: async indexing was not queued: {reason}", file=sys.stderr)
                print(f"Run `{_manual_index_command(args.project)}` to index deterministically.", file=sys.stderr)

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

    elif args.command == "move-file":
        result = registry.move_file(args.file_path, args.to_project)
        sys.exit(0 if result["moved"] else 1)

    elif args.command == "verify":
        result = registry.verify_project(args.project)
        if args.json:
            print(json.dumps(result, indent=2))
        sys.exit(0 if not result["missing"] else 1)

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
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
