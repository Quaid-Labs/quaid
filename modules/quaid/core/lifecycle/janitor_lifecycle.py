"""Lifecycle maintenance registry for janitor datastore routines.

This module provides a narrow orchestration contract between janitor and
module-owned maintenance routines. Datastore/workspace modules own maintenance
logic and register their routines here.
"""

from __future__ import annotations

import importlib
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol


@dataclass
class RoutineContext:
    cfg: Any
    dry_run: bool
    workspace: Path
    force_distill: bool = False
    allow_doc_apply: Optional[Callable[[str, str], bool]] = None
    graph: Any = None
    options: Dict[str, Any] = field(default_factory=dict)


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

    def register(self, name: str, routine: LifecycleRoutine) -> None:
        self._routines[name] = routine

    def has(self, name: str) -> bool:
        return name in self._routines

    def run(self, name: str, ctx: RoutineContext) -> RoutineResult:
        routine = self._routines.get(name)
        if routine is None:
            return RoutineResult(errors=[f"No lifecycle routine registered: {name}"])
        return routine(ctx)

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
        registrar(registry, RoutineResult)
    except Exception as exc:
        msg = f"Lifecycle registration failed: {module_name}: {exc}"
        for routine_name in expected_routines:
            if not registry.has(routine_name):
                _register_failure(routine_name, msg)


def build_default_registry() -> LifecycleRegistry:
    registry = LifecycleRegistry()

    module_specs: List[tuple[str, List[str]]] = [
        ("adaptors.openclaw.maintenance", ["workspace"]),
        ("core.docs.updater", ["docs_staleness", "docs_cleanup"]),
        ("core.lifecycle.soul_snippets", ["snippets", "journal"]),
        ("core.docs.rag", ["rag"]),
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

    for module_name, routines in module_specs:
        _register_module_routines(registry, module_name, routines)

    return registry
