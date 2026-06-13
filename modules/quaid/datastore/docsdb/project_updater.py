#!/usr/bin/env python3
"""
Project docs helpers kept for append-only PROJECT.log writes and PROJECT.md
registry-section refreshes.

Supervisor-owned project docs updates live in core/project_docs*.py and are
requested with `quaid docs update <project>`. Legacy staged event processing was
removed prelaunch.

Usage:
  python3 project_updater.py refresh-project-md <project-name>
"""

import argparse
import logging
import os
import re
import sys
import tempfile

logger = logging.getLogger(__name__)
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import get_config
import datastore.docsdb.registry as docs_registry
from lib.project_templates import (
    has_registry_managed_markers,
    PROJECT_LOG_BEGIN,
    PROJECT_LOG_END,
    render_project_md_template,
    replace_managed_block,
)
from lib.runtime_context import (
    get_quaid_home,
    get_visible_quaid_home,
    get_workspace_dir,
)
from lib.fail_policy import is_fail_hard_enabled

PROJECT_HISTORY_FILENAME = "PROJECT.log"

def _workspace() -> Path:
    return get_workspace_dir()


def _resolve_path(relative: str) -> Path:
    """Resolve a runtime path to absolute.

    Canonical project homes live at QUAID_VISIBLE_HOME/projects/, but per-instance
    staging still lives at <instance>/projects/staging/.
    """
    p = Path(relative)
    if p.is_absolute():
        return p
    if relative == "projects" or (
        relative.startswith("projects/")
        and relative != "projects/staging"
        and not relative.startswith("projects/staging/")
    ):
        return get_visible_quaid_home() / relative
    if relative.startswith("shared/"):
        return get_quaid_home() / relative
    return _workspace() / relative


def _resolve_project_home(home_dir: str) -> Path:
    """Resolve a project home_dir to an absolute path.

    Canonical project homes (`projects/...`) live under QUAID_HOME. Legacy
    `shared/...` homes also resolve under QUAID_HOME.
    """
    return _resolve_path(home_dir)


def _atomic_write_text(path: Path, content: str) -> None:
    """Atomically write UTF-8 text using a unique temp file in the target dir."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError as exc:
                logger.warning("Failed cleaning up temp file %s: %s", tmp_path, exc)


# Legacy staged project event processing was removed prelaunch.
# Project docs updates are now supervisor-owned and requested with
# `quaid docs update <project>`. Append-only PROJECT.log support remains below.


def refresh_project_md(project_name: str) -> bool:
    """Refresh registry-backed navigation sections inside PROJECT.md."""
    cfg = get_config()
    defn = cfg.projects.definitions.get(project_name)
    if not defn:
        print(f"Project '{project_name}' not found in config")
        return False

    registry = docs_registry.DocsRegistry()
    _refresh_file_list(registry, project_name, cfg)
    return True


# ============================================================================
# PROJECT.md registry-section refresh helpers
# ============================================================================

def _refresh_file_list(registry, project_name: str, cfg) -> None:
    """Refresh registry-backed navigation sections of PROJECT.md."""
    defn = cfg.projects.definitions.get(project_name)
    if not defn:
        return

    project_md_path = _resolve_path(defn.home_dir) / "PROJECT.md"
    if not project_md_path.exists():
        return

    content = project_md_path.read_text(encoding="utf-8")
    sections = docs_registry._managed_project_sections(registry, project_name, defn)
    has_markers = has_registry_managed_markers(content)
    if has_markers:
        content = docs_registry._populate_project_md_sections(
            content,
            project_home_body=str(sections.get("project_home") or ""),
            source_roots_body=str(sections.get("source_roots") or ""),
            in_dir_body=str(sections.get("in_dir") or ""),
            external_body=str(sections.get("external") or ""),
            registered_docs_body=str(sections.get("registered_docs") or ""),
        )
    else:
        rebuilt = render_project_md_template(
            label=defn.label,
            description=defn.description or f"{defn.label} project.",
            project_home=str(_resolve_project_home(defn.home_dir)),
            source_roots=[str(registry._resolve_path(root)) for root in (defn.source_roots or [])],
            exclude_patterns=defn.exclude or [],
        )
        rebuilt = docs_registry._populate_project_md_sections(
            rebuilt,
            project_home_body=str(sections.get("project_home") or ""),
            source_roots_body=str(sections.get("source_roots") or ""),
            in_dir_body=str(sections.get("in_dir") or ""),
            external_body=str(sections.get("external") or ""),
            registered_docs_body=str(sections.get("registered_docs") or ""),
        )
        if PROJECT_LOG_BEGIN in content and PROJECT_LOG_END in content:
            match = re.search(
                re.escape(PROJECT_LOG_BEGIN) + r"(.*?)" + re.escape(PROJECT_LOG_END),
                content,
                flags=re.DOTALL,
            )
            if match:
                rebuilt = replace_managed_block(rebuilt, PROJECT_LOG_BEGIN, PROJECT_LOG_END, match.group(1).strip())
        content = rebuilt
        print("  Rebuilt PROJECT.md with canonical navigation scaffold")

    try:
        _atomic_write_text(project_md_path, content)
    except OSError as e:
        print(f"  Error writing PROJECT.md: {e}", file=sys.stderr)
        if is_fail_hard_enabled():
            raise


def _project_md_recent_log_limit(default: int = 15) -> int:
    raw = os.getenv("QUAID_PROJECT_MD_RECENT_LIMIT", str(default))
    try:
        limit = int(raw)
    except Exception:
        limit = default
    return max(1, limit)


def _project_log_now(date_str: Optional[str] = None) -> datetime:
    """Return the effective project-log timestamp."""
    raw = str(date_str or os.environ.get("QUAID_NOW", "") or "").strip()
    if raw:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            raw = f"{raw}T23:59:59"
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            logger.warning("Invalid project log date override %r; using wall clock", raw)
    return datetime.now()


def _index_project_history_log(log_path: Path, *, project_name: str, trigger: str) -> int:
    """Index PROJECT.log immediately after append so dated project recall can find it.

    This intentionally indexes on the append hot path instead of waiting for a
    later sweep; the r1383 temporal A/B showed stale PROJECT.log chunks were
    directly suppressing historical project answers. Relying on sweep-based
    staleness reindex was considered but rejected because same-cycle recall
    needs the freshly appended dated log evidence.
    """
    try:
        from datastore.docsdb.rag import DocsRAG

        chunks = int(DocsRAG().index_document(str(log_path)) or 0)
    except Exception as exc:
        raise RuntimeError(
            "append_project_logs: failed to index PROJECT.log "
            f"project={project_name} path={log_path} trigger={trigger} error={exc}"
        ) from exc

    if chunks <= 0:
        logger.warning(
            "append_project_logs: PROJECT.log indexing produced no chunks "
            "project=%s path=%s trigger=%s",
            project_name,
            log_path,
            trigger,
        )
        return 0

    logger.info(
        "append_project_logs: indexed PROJECT.log project=%s path=%s chunks=%s trigger=%s",
        project_name,
        log_path,
        chunks,
        trigger,
    )
    return chunks
def append_project_logs(
    project_logs: Dict[str, List[Any]],
    trigger: str = "Compaction",
    date_str: Optional[str] = None,
    dry_run: bool = False,
    *,
    index_history: bool = True,
    update_project_md: bool = True,
) -> Dict[str, int]:
    """Append project log bullets to per-project PROJECT.md files.

    Project logs are written under:
      ## Recent Major Changes
      <!-- BEGIN:PROJECT_LOG -->
      - YYYY-MM-DD [Trigger] note
      <!-- END:PROJECT_LOG -->
    """
    metrics = {
        "projects_seen": 0,
        "projects_updated": 0,
        "entries_seen": 0,
        "entries_written": 0,
        "projects_unknown": 0,
        "projects_missing_file": 0,
        "projects_history_only": 0,
        "history_entries_written": 0,
        "history_logs_indexed": 0,
        "history_chunks_indexed": 0,
        "history_logs_unindexed": 0,
        "history_index_failures": 0,
    }
    if not isinstance(project_logs, dict) or not project_logs:
        return metrics

    cfg = get_config()
    registry = docs_registry.DocsRegistry()
    effective_now = _project_log_now(date_str)
    marker_begin = PROJECT_LOG_BEGIN
    marker_end = PROJECT_LOG_END
    recent_limit = _project_md_recent_log_limit()
    session_prefix_re = re.compile(r"^\s*Session\s+\d+\s*(?:\([^)]*\))?\s*:\s*", flags=re.IGNORECASE)

    def _normalize_log_entry(raw: object) -> str:
        if isinstance(raw, dict):
            raw = raw.get("text", raw.get("entry", raw.get("note", "")))
        text = str(raw or "").strip()
        if not text:
            return ""
        text = re.sub(r"^\s*[-*+]\s*", "", text)
        text = session_prefix_re.sub("", text)
        return re.sub(r"\s+", " ", text).strip()

    def _normalize_entry_timestamp(raw: object) -> Optional[str]:
        text = str(raw or "").strip()
        if not text:
            return None
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            text = f"{text}T23:59:59"
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat(timespec="seconds")
        except ValueError:
            return None

    def _normalize_log_record(raw: object) -> Optional[Dict[str, str]]:
        text = _normalize_log_entry(raw)
        if not text:
            return None
        created_at = None
        if isinstance(raw, dict):
            created_at = (
                _normalize_entry_timestamp(raw.get("created_at"))
                or _normalize_entry_timestamp(raw.get("timestamp"))
                or _normalize_entry_timestamp(raw.get("date"))
            )
        if not created_at:
            created_at = effective_now.isoformat(timespec="seconds")
        return {
            "text": text,
            "created_at": created_at,
            "day": created_at[:10],
        }

    def _dedupe_visible_log_records(entries: List[object]) -> List[Dict[str, str]]:
        deduped: List[Dict[str, str]] = []
        seen: set[Tuple[str, str]] = set()
        for raw in entries:
            record = _normalize_log_record(raw)
            if not record:
                continue
            key = (record["text"], record["day"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(record)
        return deduped

    def _append_project_history_log(project_md_path: Path, entries: List[object]) -> int:
        """Append normalized project log entries to PROJECT.log.

        Existing exact lines are skipped so queue replays after a crash do not
        duplicate already-committed visible history.
        """
        normalized = [_normalize_log_record(x) for x in entries]
        normalized = [x for x in normalized if x]
        if not normalized:
            return 0
        log_path = project_md_path.with_name(PROJECT_HISTORY_FILENAME)
        lines = [f"- [{record['created_at']}] {record['text']}" for record in normalized]
        existing_lines: set[str] = set()
        try:
            existing_lines = {
                line.strip()
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            }
        except FileNotFoundError:
            existing_lines = set()
        except OSError as exc:
            if is_fail_hard_enabled():
                raise
            logger.warning("append_project_logs: failed reading existing PROJECT.log %s: %s", log_path, exc)
        lines = [line for line in lines if line not in existing_lines]
        if not lines:
            return 0
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return len(lines)

    def _index_written_history_log(project_name: str, project_md_path: Path) -> int:
        log_path = project_md_path.with_name(PROJECT_HISTORY_FILENAME)
        try:
            chunks = _index_project_history_log(
                log_path,
                project_name=project_name,
                trigger=trigger,
            )
        except Exception as e:
            metrics["history_index_failures"] += 1
            logger.warning(
                "append_project_logs: failed PROJECT.log index for project=%s path=%s error=%s",
                project_name,
                log_path,
                e,
            )
            print(
                f"[project-log][warn] failed PROJECT.log index: "
                f"project={project_name} error={e}"
            )
            if is_fail_hard_enabled():
                raise
            return 0
        if chunks > 0:
            metrics["history_logs_indexed"] += 1
            metrics["history_chunks_indexed"] += chunks
        else:
            metrics["history_logs_unindexed"] += 1
        return chunks

    for project_name, raw_entries in project_logs.items():
        metrics["projects_seen"] += 1
        entries = _dedupe_visible_log_records(list(raw_entries or []))
        metrics["entries_seen"] += len(entries)
        if not entries:
            continue
        entries_for_write = list(entries)
        history_entries = list(raw_entries or [])

        defn = cfg.projects.definitions.get(project_name)
        if not defn:
            defn = registry.get_project_definition(project_name)
        if not defn:
            # Force-flush: reload the config and registry before falling back to
            # the filesystem. The config singleton may be stale (e.g. written by
            # the installer after this process started). A fresh load gives the
            # project one more chance to be found via the normal path.
            try:
                from config import load_config as _reload_cfg
                fresh_cfg = _reload_cfg()
                defn = fresh_cfg.projects.definitions.get(project_name)
                if not defn:
                    defn = docs_registry.DocsRegistry().get_project_definition(project_name)
            except Exception as exc:
                if is_fail_hard_enabled():
                    raise
                logger.warning(
                    "append_project_logs: config reload fallback failed for project=%s: %s",
                    project_name,
                    exc,
                )
        if not defn:
            # Filesystem fallback: project may have been deleted from the
            # registry during the session but its directory still exists.
            try:
                from lib.instance import visible_projects_dir
                candidate = visible_projects_dir() / project_name
            except Exception:
                home = get_quaid_home()
                if home.name.startswith(".") and len(home.name) > 1:
                    home = home.with_name(home.name[1:])
                candidate = home / "projects" / project_name
            if (candidate / "PROJECT.md").exists():
                project_md = candidate / "PROJECT.md"
                print(f"[project-log] registry miss, using filesystem fallback: {project_md}")
            else:
                metrics["projects_unknown"] += 1
                reroute_name = "quaid"
                reroute_defn = cfg.projects.definitions.get(reroute_name)
                if not reroute_defn:
                    reroute_defn = registry.get_project_definition(reroute_name)
                reroute_md: Optional[Path] = None
                if reroute_defn:
                    candidate_md = _resolve_project_home(reroute_defn.home_dir) / "PROJECT.md"
                    if candidate_md.exists():
                        reroute_md = candidate_md
                if reroute_md is None:
                    print(f"[project-log] unknown project: {project_name}")
                    continue
                prefix = f"[from deleted/unknown project {project_name}] "
                entries_for_write = [
                    {
                        **entry,
                        "text": f"{prefix}{entry['text']}",
                    }
                    for entry in entries
                ]
                history_entries = [
                    {
                        **record,
                        "text": f"{prefix}{record['text']}",
                    }
                    for record in (_normalize_log_record(entry) for entry in (raw_entries or []))
                    if record
                ]
                project_md = reroute_md
                print(
                    f"[project-log] unknown project: {project_name}; "
                    f"rerouting {len(entries_for_write)} entries to {reroute_name}"
                )
        else:
            project_md = _resolve_project_home(defn.home_dir) / "PROJECT.md"
            if not project_md.exists():
                metrics["projects_missing_file"] += 1
                print(f"[project-log][warn] missing PROJECT.md: {project_md}")
                logger.warning(
                    "append_project_logs: missing PROJECT.md for project=%s path=%s trigger=%s",
                    project_name,
                    project_md,
                    trigger,
                )
                # Keep append-only history alive even if PROJECT.md is missing.
                # This prevents silent data drops when project docs are missing.
                if not dry_run:
                    try:
                        history_written = _append_project_history_log(project_md, history_entries)
                    except Exception as e:
                        logger.warning(
                            "append_project_logs: failed PROJECT.log append for project=%s path=%s error=%s",
                            project_name,
                            project_md.with_name(PROJECT_HISTORY_FILENAME),
                            e,
                        )
                        print(
                            f"[project-log][warn] failed PROJECT.log append: "
                            f"project={project_name} error={e}"
                        )
                        if is_fail_hard_enabled():
                            raise
                    else:
                        if history_written > 0:
                            metrics["projects_history_only"] += 1
                            metrics["history_entries_written"] += history_written
                            if index_history:
                                _index_written_history_log(project_name, project_md)
                            print(
                                f"[project-log] project={project_name} history_only_entries={history_written} "
                                f"file={project_md.with_name(PROJECT_HISTORY_FILENAME)} dry_run={dry_run}"
                            )
                continue

        if not dry_run:
            history_written = _append_project_history_log(project_md, history_entries)
            if history_written > 0:
                metrics["history_entries_written"] += history_written
                if index_history:
                    _index_written_history_log(project_name, project_md)

        if not update_project_md:
            continue

        lines = [f"- {entry['day']} [{trigger}] {entry['text']}" for entry in entries_for_write]
        content = project_md.read_text(encoding="utf-8")
        if marker_begin in content and marker_end in content:
            pattern = re.compile(
                re.escape(marker_begin) + r"(.*?)" + re.escape(marker_end),
                flags=re.DOTALL,
            )
            m = pattern.search(content)
            existing_lines = []
            if m and m.group(1).strip():
                existing_lines = [line.strip() for line in m.group(1).strip().splitlines() if line.strip()]
            body_lines = (existing_lines + lines)[-recent_limit:]
            body = "\n".join(body_lines)
            replacement = f"{marker_begin}\n{body}\n{marker_end}"
            updated = pattern.sub(lambda _m: replacement, content, count=1)
        else:
            updated = (
                content.rstrip()
                + "\n\n## Recent Major Changes\n"
                + f"{marker_begin}\n"
                + "\n".join(lines[-recent_limit:])
                + f"\n{marker_end}\n"
            )

        metrics["entries_written"] += len(lines)
        metrics["projects_updated"] += 1
        print(
            f"[project-log] project={project_name} entries={len(lines)} "
            f"file={project_md} dry_run={dry_run}"
        )
        if not dry_run:
            _atomic_write_text(project_md, updated)

    return metrics


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Project Updater")
    subparsers = parser.add_subparsers(dest="command", help="Command")

    # refresh-project-md
    rp_p = subparsers.add_parser("refresh-project-md", help="Regenerate PROJECT.md file list")
    rp_p.add_argument("project_name", help="Project name")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == "refresh-project-md":
        ok = refresh_project_md(args.project_name)
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
