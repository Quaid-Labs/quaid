"""Lifecycle maintenance registry for janitor datastore routines.

This module provides a narrow orchestration contract between janitor and
module-owned maintenance routines. Datastore/workspace modules own maintenance
logic and register their routines here.
"""

from __future__ import annotations

import importlib
import logging
import os
import threading
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol

from core.runtime.parallel_runtime import ResourceLockRegistry, get_parallel_config
logger = logging.getLogger(__name__)


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
        self._registry_guard = threading.Lock()
        self._lock_registries: Dict[str, ResourceLockRegistry] = {}
        self._lock_registries_guard = threading.Lock()
        self._max_lock_registries = max(1, int(os.environ.get("QUAID_MAX_LOCK_REGISTRIES", "64") or 64))
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
        with self._registry_guard:
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
        with self._registry_guard:
            return name in self._routines

    def run(self, name: str, ctx: RoutineContext) -> RoutineResult:
        with self._registry_guard:
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
        overall_timeout_seconds: Optional[float] = None,
    ) -> Dict[str, RoutineResult]:
        results: Dict[str, RoutineResult] = {}
        if not routines:
            return results
        worker_count = max(1, min(int(max_workers), len(routines)))
        timeout_seconds = overall_timeout_seconds
        if timeout_seconds is None:
            try:
                first_cfg = routines[0][1].cfg
                parallel_cfg = get_parallel_config(first_cfg)
                timeout_seconds = float(getattr(parallel_cfg, "lifecycle_prepass_timeout_seconds", 300) or 300)
            except Exception:
                timeout_seconds = 300.0
        timeout_seconds = max(0.001, float(timeout_seconds))
        deadline = time.monotonic() + timeout_seconds
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            fut_to_name = {
                executor.submit(self.run, name, ctx): name
                for name, ctx in routines
            }
            pending = set(fut_to_name.keys())
            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    for fut in list(pending):
                        name = fut_to_name[fut]
                        fut.cancel()
                        results[name] = RoutineResult(
                            errors=[f"Parallel lifecycle run timed out for {name} after {timeout_seconds:.2f}s"]
                        )
                        pending.discard(fut)
                    break
                try:
                    done = next(as_completed(pending, timeout=remaining))
                except TimeoutError:
                    # Futures can complete right as as_completed() times out.
                    # Drain those completed futures before deadline cancellation.
                    done_now = [fut for fut in list(pending) if fut.done()]
                    for completed in done_now:
                        name = fut_to_name[completed]
                        pending.discard(completed)
                        try:
                            results[name] = completed.result()
                        except Exception as exc:  # pragma: no cover
                            results[name] = RoutineResult(
                                errors=[f"Parallel lifecycle run failed for {name}: {exc}"]
                            )
                    continue
                name = fut_to_name[done]
                pending.discard(done)
                try:
                    results[name] = done.result()
                except Exception as exc:  # pragma: no cover
                    results[name] = RoutineResult(errors=[f"Parallel lifecycle run failed for {name}: {exc}"])
        return results

    def _lock_registry_for_workspace(self, workspace: Path) -> ResourceLockRegistry:
        lock_root = (Path(workspace).resolve() / ".quaid" / "runtime" / "locks" / "janitor")
        key = str(lock_root)
        with self._lock_registries_guard:
            reg = self._lock_registries.pop(key, None)
            if reg is None:
                reg = ResourceLockRegistry(lock_root)
            self._lock_registries[key] = reg
            while len(self._lock_registries) > self._max_lock_registries:
                oldest_key = next(iter(self._lock_registries))
                if oldest_key == key and len(self._lock_registries) > 1:
                    keys = iter(self._lock_registries)
                    _ = next(keys, None)
                    oldest_key = next(keys, oldest_key)
                if oldest_key == key:
                    break
                self._lock_registries.pop(oldest_key, None)
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
                print(
                    f"[janitor_lifecycle] Requested executor resize {self._llm_executor_workers} -> {worker_count} "
                    "ignored for safety; restart process to apply.",
                    file=sys.stderr,
                )
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

        executor = self._ensure_llm_executor(worker_count)
        sem = threading.Semaphore(worker_count)
        results: List[Any] = [None] * len(seq)
        timeout_seconds = self._parallel_map_timeout_seconds(ctx)
        deadline = time.monotonic() + timeout_seconds

        def _run(idx_item: tuple[int, Any]) -> tuple[int, Any]:
            idx, item = idx_item
            with sem:
                return idx, fn(item)

        futs = [executor.submit(_run, (idx, item)) for idx, item in enumerate(seq)]
        pending = set(futs)
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                for fut in list(pending):
                    fut.cancel()
                raise TimeoutError(
                    f"Parallel map timed out after {timeout_seconds:.2f}s "
                    f"(items={len(seq)}, workers={worker_count})"
                )
            try:
                done = next(as_completed(pending, timeout=remaining))
            except TimeoutError:
                for fut in list(pending):
                    fut.cancel()
                raise TimeoutError(
                    f"Parallel map timed out after {timeout_seconds:.2f}s "
                    f"(items={len(seq)}, workers={worker_count})"
                )
            pending.discard(done)
            try:
                idx, value = done.result()
            except Exception:
                for fut in list(pending):
                    fut.cancel()
                raise
            results[idx] = value
        return results

    def shutdown(self, wait: bool = False) -> None:
        """Release lifecycle-owned executors."""
        with self._llm_executor_guard:
            ex = self._llm_executor
            self._llm_executor = None
            self._llm_executor_workers = 0
        if ex is not None:
            ex.shutdown(wait=wait, cancel_futures=True)

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

    def _parallel_map_timeout_seconds(self, ctx: RoutineContext, default_seconds: float = 300.0) -> float:
        raw_env = os.environ.get("QUAID_CORE_PARALLEL_MAP_TIMEOUT_SECONDS", "")
        if str(raw_env).strip():
            try:
                parsed_env = float(raw_env)
                if parsed_env > 0:
                    return parsed_env
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid QUAID_CORE_PARALLEL_MAP_TIMEOUT_SECONDS=%r; using config/default",
                    raw_env,
                )
        try:
            parallel_cfg = get_parallel_config(ctx.cfg)
            parsed_cfg = float(
                getattr(parallel_cfg, "lifecycle_prepass_timeout_seconds", default_seconds)
                or default_seconds
            )
            if parsed_cfg > 0:
                return parsed_cfg
        except Exception:
            pass
        return float(default_seconds)

    def _resolved_write_resources(self, name: str, ctx: RoutineContext) -> List[str]:
        with self._registry_guard:
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

_ALLOWED_LIFECYCLE_MODULE_PREFIXES: tuple[str, ...] = (
    "adaptors.",
    "core.",
    "datastore.",
)


def _lifecycle_module_allowed(module_name: str) -> bool:
    return any(module_name.startswith(prefix) for prefix in _ALLOWED_LIFECYCLE_MODULE_PREFIXES)


def _register_module_routines(
    registry: LifecycleRegistry,
    module_name: str,
    expected_routines: List[str],
) -> None:
    """Load module-owned lifecycle registrations with safe fallback errors."""

    def _register_failure(routine_name: str, message: str) -> None:
        def _missing(_ctx: RoutineContext) -> RoutineResult:
            return RoutineResult(errors=[message])

        registry.register(routine_name, _missing, owner=module_name)

    def _remove_module_failure_stubs() -> None:
        with registry._registry_guard:
            for routine_name in expected_routines:
                existing = registry._routines.get(routine_name)
                if existing is None:
                    continue
                owner = registry._owners.get(routine_name, "unknown")
                if owner != module_name:
                    continue
                if getattr(existing, "__name__", "") != "_missing":
                    continue
                registry._routines.pop(routine_name, None)
                registry._owners.pop(routine_name, None)
                registry._write_resources.pop(routine_name, None)

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
        _remove_module_failure_stubs()
        class _ScopedRegistry:
            def __init__(self, base: LifecycleRegistry, owner: str) -> None:
                self._base = base
                self._owner = owner

            def register(
                self,
                name: str,
                routine: LifecycleRoutine,
                write_resources: Optional[List[str]] = None,
            ) -> None:
                self._base.register(
                    name,
                    routine,
                    owner=self._owner,
                    write_resources=write_resources,
                )

        registrar(_ScopedRegistry(registry, module_name), RoutineResult)
    except Exception as exc:
        msg = f"Lifecycle registration failed: {module_name}: {exc}"
        for routine_name in expected_routines:
            if not registry.has(routine_name):
                _register_failure(routine_name, msg)


def _resolve_adapter_maintenance_module(default_module: str = "adaptors.openclaw.maintenance") -> str:
    """Resolve adapter maintenance module from active adapter manifest."""
    try:
        from config import get_config  # local import avoids hard dependency at module import
        from core.runtime.plugins import discover_plugin_manifests

        cfg = get_config()
        plugins_cfg = getattr(cfg, "plugins", None)
        if plugins_cfg is None:
            return default_module

        slots = getattr(plugins_cfg, "slots", None)
        adapter_id = str(getattr(slots, "adapter", "") or "").strip()
        if not adapter_id:
            return default_module

        manifests, _ = discover_plugin_manifests(
            paths=list(getattr(plugins_cfg, "paths", []) or []),
            allowlist=list(getattr(plugins_cfg, "allowlist", []) or []),
            strict=False,
        )
        for manifest in manifests:
            if str(getattr(manifest, "plugin_id", "") or "").strip() != adapter_id:
                continue
            module_name = str(getattr(manifest, "module", "") or "").strip()
            if not module_name or "." not in module_name:
                return default_module
            parts = module_name.split(".")
            parts[-1] = "maintenance"
            return ".".join(parts)
    except Exception:
        pass
    return default_module


def build_default_registry() -> LifecycleRegistry:
    registry = LifecycleRegistry()

    adapter_module = _resolve_adapter_maintenance_module()
    module_specs: List[tuple[str, List[str]]] = [
        (adapter_module, ["workspace"]),
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
            if not _lifecycle_module_allowed(module_name):
                logger.warning(
                    "Ignoring lifecycle module outside allowed prefixes: %s",
                    module_name,
                )
                continue
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
            if not _lifecycle_module_allowed(name):
                logger.warning(
                    "Ignoring lifecycle.modules entry outside allowed prefixes: %s",
                    name,
                )
                continue
            module_specs.append((name, []))
    except Exception:
        logger.warning("Failed to parse lifecycle.modules config; using default lifecycle registry.", exc_info=True)

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
