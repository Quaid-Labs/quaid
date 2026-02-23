"""Lifecycle maintenance registry for janitor datastore routines.

This module provides a narrow contract between janitor orchestration and
datastore-specific maintenance implementations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol

from docs_rag import DocsRAG


@dataclass
class RoutineContext:
    cfg: Any
    dry_run: bool
    workspace: Path


@dataclass
class RoutineResult:
    metrics: Dict[str, int] = field(default_factory=dict)
    logs: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


class LifecycleRoutine(Protocol):
    def __call__(self, ctx: RoutineContext) -> RoutineResult:
        ...


class LifecycleRegistry:
    def __init__(self) -> None:
        self._routines: Dict[str, LifecycleRoutine] = {}

    def register(self, name: str, routine: LifecycleRoutine) -> None:
        self._routines[name] = routine

    def has(self, name: str) -> bool:
        return name in self._routines

    def run(self, name: str, ctx: RoutineContext) -> RoutineResult:
        routine = self._routines.get(name)
        if routine is None:
            return RoutineResult(errors=[f"No lifecycle routine registered: {name}"])
        return routine(ctx)


def _run_rag_maintenance(ctx: RoutineContext) -> RoutineResult:
    result = RoutineResult()
    cfg = ctx.cfg
    dry_run = ctx.dry_run
    workspace = ctx.workspace

    try:
        if cfg.projects.enabled and not dry_run:
            try:
                from project_updater import process_all_events

                result.logs.append("Processing queued project events...")
                event_result = process_all_events()
                processed = int(event_result.get("processed", 0))
                result.metrics["project_events_processed"] = processed
                if processed > 0:
                    result.logs.append(f"  Processed {processed} event(s)")
            except Exception as e:
                result.errors.append(f"Project event processing failed: {e}")
        elif cfg.projects.enabled and dry_run:
            result.logs.append("Skipping project event processing (dry-run)")

        if cfg.projects.enabled:
            try:
                from docs_registry import DocsRegistry

                registry = DocsRegistry()
                total_discovered = 0
                for proj_name, proj_defn in cfg.projects.definitions.items():
                    if proj_defn.auto_index:
                        discovered = registry.auto_discover(proj_name)
                        total_discovered += len(discovered)
                result.metrics["project_files_discovered"] = total_discovered
                if total_discovered > 0:
                    result.logs.append(f"  Discovered {total_discovered} new file(s)")

                for proj_name in cfg.projects.definitions:
                    try:
                        registry.sync_external_files(proj_name)
                    except Exception:
                        continue
            except Exception as e:
                result.errors.append(f"Project auto-discover failed: {e}")

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

        result.metrics["rag_total_files"] = total_files
        result.metrics["rag_files_indexed"] = indexed
        result.metrics["rag_files_skipped"] = skipped
        result.metrics["rag_chunks_created"] = chunks
    except Exception as e:
        result.errors.append(f"RAG maintenance failed: {e}")

    return result


def build_default_registry() -> LifecycleRegistry:
    registry = LifecycleRegistry()
    registry.register("rag", _run_rag_maintenance)
    return registry

