"""Lifecycle maintenance registry for janitor datastore routines.

This module provides a narrow orchestration contract between janitor and
module-owned maintenance routines. Datastore/workspace modules own maintenance
logic and register their routines here.
"""

from __future__ import annotations

import importlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol

from core.runtime.parallel_runtime import ResourceLockRegistry, get_parallel_config


@dataclass
class RoutineContext:
    cfg: Any
    dry_run: bool
    workspace: Path
    force_distill: bool = False
    allow_doc_apply: Optional[Callable[[str, str], bool]] = None
    graph: Any = None
    options: Dict[str, Any] = field(default_factory=dict)
    parallel_map: Optional[Callable[..., List[Any]]] = None


@dataclass
class RoutineResult:
    metrics: Dict[str, int] = field(default_factory=dict)
    logs: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict)


class LifecycleRoutine(Protocol):
    def __call__(self, ctx: RoutineContext) -> RoutineResult:
        ...


class LifecycleRegistry:
    def __init__(self) -> None:
        self._routines: Dict[str, LifecycleRoutine] = {}
        self._owners: Dict[str, str] = {}
        self._write_resources: Dict[str, List[str]] = {}
        self._lock_registries: Dict[str, ResourceLockRegistry] = {}
        self._lock_registries_guard = threading.Lock()
        self._llm_executor: Optional[ThreadPoolExecutor] = None
        self._llm_executor_workers: int = 0
        self._llm_executor_guard = threading.Lock()

    def register(
        self,
        name: str,
        routine: LifecycleRoutine,
        owner: str = "unknown",
        write_resources: Optional[List[str]] = None,
    ) -> None:
        existing = self._routines.get(name)
        if existing is not None:
            existing_owner = self._owners.get(name, "unknown")
            # Allow exact idempotent re-registration from same owner.
            if existing is routine and existing_owner == owner:
                return
            raise ValueError(
                f"Lifecycle routine '{name}' already registered by '{existing_owner}', "
                f"cannot re-register from '{owner}'"
            )
        self._routines[name] = routine
        self._owners[name] = owner
        self._write_resources[name] = list(write_resources or [])

    def has(self, name: str) -> bool:
        return name in self._routines

    def run(self, name: str, ctx: RoutineContext) -> RoutineResult:
        routine = self._routines.get(name)
        if routine is None:
            return RoutineResult(errors=[f"No lifecycle routine registered: {name}"])
        bound_ctx = self._bind_core_runtime(ctx)
        lock_cfg = self._lock_config(ctx)
        if not lock_cfg["enabled"]:
            return routine(bound_ctx)

        resources = self._resolved_write_resources(name, ctx)
        if lock_cfg["require_registration"] and not resources:
            return RoutineResult(errors=[f"Lifecycle routine '{name}' missing write resource registration"])

        if not resources:
            return routine(bound_ctx)

        lock_registry = self._lock_registry_for_workspace(ctx.workspace)
        try:
            with lock_registry.acquire_many(resources, timeout_seconds=lock_cfg["timeout_seconds"]):
                return routine(bound_ctx)
        except TimeoutError as exc:
            return RoutineResult(errors=[f"Lifecycle routine '{name}' lock timeout: {exc}"])

    def run_many(
        self,
        routines: List[tuple[str, RoutineContext]],
        max_workers: int = 3,
    ) -> Dict[str, RoutineResult]:
        results: Dict[str, RoutineResult] = {}
        if not routines:
            return results
        worker_count = max(1, min(int(max_workers), len(routines)))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            fut_to_name = {
                executor.submit(self.run, name, ctx): name
                for name, ctx in routines
            }
            for fut in as_completed(fut_to_name):
                name = fut_to_name[fut]
                try:
                    results[name] = fut.result()
                except Exception as exc:  # pragma: no cover
                    results[name] = RoutineResult(errors=[f"Parallel lifecycle run failed for {name}: {exc}"])
        return results

    def _lock_registry_for_workspace(self, workspace: Path) -> ResourceLockRegistry:
        lock_root = (Path(workspace).resolve() / ".quaid" / "runtime" / "locks" / "janitor")
        key = str(lock_root)
        with self._lock_registries_guard:
            reg = self._lock_registries.get(key)
            if reg is None:
                reg = ResourceLockRegistry(lock_root)
                self._lock_registries[key] = reg
            return reg

    def _llm_workers(self, ctx: RoutineContext) -> int:
        parallel = get_parallel_config(ctx.cfg)
        workers = int(getattr(parallel, "llm_workers", 4) or 4)
        return max(1, workers)

    def _ensure_llm_executor(self, workers: int) -> ThreadPoolExecutor:
        worker_count = max(1, int(workers))
        with self._llm_executor_guard:
            if self._llm_executor is None:
                self._llm_executor = ThreadPoolExecutor(max_workers=worker_count)
                self._llm_executor_workers = worker_count
            elif worker_count != self._llm_executor_workers:
                old = self._llm_executor
                self._llm_executor = ThreadPoolExecutor(max_workers=worker_count)
                self._llm_executor_workers = worker_count
                old.shutdown(wait=False)
            return self._llm_executor

    def _core_parallel_map(
        self,
        ctx: RoutineContext,
        items: List[Any],
        fn: Callable[[Any], Any],
        *,
        max_workers: Optional[int] = None,
    ) -> List[Any]:
        seq = list(items or [])
        if not seq:
            return []
        requested = max_workers if max_workers is not None else self._llm_workers(ctx)
        worker_count = max(1, min(int(requested), len(seq)))
        if worker_count <= 1:
            return [fn(item) for item in seq]

        executor = self._ensure_llm_executor(self._llm_workers(ctx))
        sem = threading.Semaphore(worker_count)
        results: List[Any] = [None] * len(seq)

        def _run(idx_item: tuple[int, Any]) -> tuple[int, Any]:
            idx, item = idx_item
            with sem:
                return idx, fn(item)

        futs = [executor.submit(_run, (idx, item)) for idx, item in enumerate(seq)]
        for fut in as_completed(futs):
            idx, value = fut.result()
            results[idx] = value
        return results

    def _bind_core_runtime(self, ctx: RoutineContext) -> RoutineContext:
        llm_workers = self._llm_workers(ctx)
        options = dict(ctx.options or {})
        options.setdefault("llm_workers", llm_workers)
        return replace(
            ctx,
            options=options,
            parallel_map=lambda items, fn, max_workers=None: self._core_parallel_map(
                ctx,
                items,
                fn,
                max_workers=max_workers,
            ),
        )

    def _lock_config(self, ctx: RoutineContext) -> Dict[str, Any]:
        if ctx.dry_run:
            return {"enabled": False, "timeout_seconds": 0, "require_registration": False}
        obj = get_parallel_config(ctx.cfg)
        enabled = bool(getattr(obj, "enabled", True) and getattr(obj, "lock_enforcement_enabled", True))
        timeout_seconds = int(getattr(obj, "lock_wait_seconds", 120) or 120)
        require_registration = bool(getattr(obj, "lock_require_registration", True))
        return {
            "enabled": enabled,
            "timeout_seconds": max(1, timeout_seconds),
            "require_registration": require_registration,
        }

    def _resolved_write_resources(self, name: str, ctx: RoutineContext) -> List[str]:
        declared = self._write_resources.get(name) or _DEFAULT_WRITE_RESOURCES.get(name, [])
        out: List[str] = []
        for raw in declared:
            token = str(raw or "").strip()
            if not token:
                continue
            if token == "db:memory":
                db_path = str(getattr(getattr(ctx.cfg, "database", None), "path", "data/memory.db") or "data/memory.db")
                p = Path(db_path)
                if not p.is_absolute():
                    p = ctx.workspace / p
                out.append(f"db:{p.resolve()}")
            elif token in {"files:global", "files"}:
                out.append("files:global")
            elif token == "core_markdown":
                out.append("files:global")
            elif token.startswith("file:"):
                fp = Path(token[5:])
                if not fp.is_absolute():
                    fp = ctx.workspace / fp
                out.append(f"file:{fp.resolve()}")
            else:
                out.append(token)
        # Deterministic order + dedupe.
        return sorted(set(out))


_DEFAULT_WRITE_RESOURCES: Dict[str, List[str]] = {
    # Any routine that can write markdown/docs gets the global files lock.
    "workspace": ["files:global"],
    "docs_staleness": ["files:global"],
    "docs_cleanup": ["files:global"],
    "snippets": ["files:global"],
    "journal": ["files:global"],
    # RAG updates docs index and sqlite artifacts.
    "rag": ["files:global", "db:memory"],
    # Memory maintenance is db-write heavy.
    "memory_graph_maintenance": ["db:memory"],
    "datastore_cleanup": ["db:memory"],
}


def _register_module_routines(
    registry: LifecycleRegistry,
    module_name: str,
    expected_routines: List[str],
) -> None:
    """Load module-owned lifecycle registrations with safe fallback errors."""

    def _register_failure(routine_name: str, message: str) -> None:
        def _missing(_ctx: RoutineContext) -> RoutineResult:
            return RoutineResult(errors=[message])

        registry.register(routine_name, _missing)

    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        msg = f"Lifecycle module load failed: {module_name}: {exc}"
        for routine_name in expected_routines:
            _register_failure(routine_name, msg)
        return

    registrar = getattr(module, "register_lifecycle_routines", None)
    if not callable(registrar):
        msg = f"Lifecycle module missing register_lifecycle_routines: {module_name}"
        for routine_name in expected_routines:
            _register_failure(routine_name, msg)
        return

    try:
        class _ScopedRegistry:
            def __init__(self, base: LifecycleRegistry, owner: str) -> None:
                self._base = base
                self._owner = owner

            def register(self, name: str, routine: LifecycleRoutine) -> None:
                self._base.register(name, routine, owner=self._owner)

        registrar(_ScopedRegistry(registry, module_name), RoutineResult)
    except Exception as exc:
        msg = f"Lifecycle registration failed: {module_name}: {exc}"
        for routine_name in expected_routines:
            if not registry.has(routine_name):
                _register_failure(routine_name, msg)


def build_default_registry() -> LifecycleRegistry:
    registry = LifecycleRegistry()

    module_specs: List[tuple[str, List[str]]] = [
        ("adaptors.openclaw.maintenance", ["workspace"]),
        ("datastore.docsdb.updater", ["docs_staleness", "docs_cleanup"]),
        ("datastore.notedb.soul_snippets", ["snippets", "journal"]),
        ("datastore.docsdb.rag", ["rag"]),
        ("datastore.memorydb.maintenance", ["memory_graph_maintenance"]),
        ("datastore.memorydb.memory_graph", ["datastore_cleanup"]),
    ]

    # Extension hook for external/plugin datastores:
    # comma-separated module list in env, each module must expose
    # `register_lifecycle_routines(registry, result_factory)`.
    extra_modules_raw = os.environ.get("QUAID_LIFECYCLE_MODULES", "").strip()
    if extra_modules_raw:
        for module_name in [m.strip() for m in extra_modules_raw.split(",") if m.strip()]:
            module_specs.append((module_name, []))

    # Optional config hook:
    # config.lifecycle.modules = ["my_datastore.lifecycle", ...]
    try:
        from config import get_config  # local import avoids hard dependency at module import

        cfg = get_config()
        cfg_modules = list(getattr(getattr(cfg, "lifecycle", None), "modules", []) or [])
        for module_name in cfg_modules:
            name = str(module_name).strip()
            if not name:
                continue
            module_specs.append((name, []))
    except Exception:
        pass

    # Preserve order, prevent duplicate module registrations in one build pass.
    seen_modules = set()
    deduped_specs: List[tuple[str, List[str]]] = []
    for module_name, routines in module_specs:
        if module_name in seen_modules:
            continue
        seen_modules.add(module_name)
        deduped_specs.append((module_name, routines))

    for module_name, routines in deduped_specs:
        _register_module_routines(registry, module_name, routines)

    return registry
