"""Shared worker pools for bounded parallel execution."""

from __future__ import annotations

import atexit
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


_POOL_GUARD = threading.Lock()
_POOLS: Dict[Tuple[str, int], ThreadPoolExecutor] = {}


def _pool(pool_name: str, max_workers: int) -> ThreadPoolExecutor:
    key = (str(pool_name or "default"), max(1, int(max_workers)))
    with _POOL_GUARD:
        ex = _POOLS.get(key)
        if ex is None:
            ex = ThreadPoolExecutor(max_workers=key[1], thread_name_prefix=f"quaid-{key[0]}")
            _POOLS[key] = ex
        return ex


def shutdown_worker_pools(wait: bool = False) -> None:
    """Shutdown and clear shared thread pools."""
    import sys
    with _POOL_GUARD:
        pool_items = list(_POOLS.items())
        _POOLS.clear()
    if pool_items:
        print(f"[worker_pool][atexit] shutting down {len(pool_items)} pool(s), wait={wait}", flush=True, file=sys.stderr)
    for key, ex in pool_items:
        running = getattr(ex, "_work_queue", None)
        qsize = running.qsize() if running is not None else "?"
        print(f"[worker_pool][atexit]   pool={key[0]!r} max_workers={key[1]} queue_depth={qsize}", flush=True, file=sys.stderr)
        ex.shutdown(wait=wait, cancel_futures=True)
        print(f"[worker_pool][atexit]   pool={key[0]!r} shutdown complete", flush=True, file=sys.stderr)
    if pool_items:
        print(f"[worker_pool][atexit] all pools shut down", flush=True, file=sys.stderr)


atexit.register(shutdown_worker_pools, True)


def run_callables(
    callables: Sequence[Callable[[], Any]],
    *,
    max_workers: int,
    pool_name: str = "default",
    timeout_seconds: Optional[float] = None,
    return_exceptions: bool = False,
) -> List[Any]:
    """Run callables in parallel with deterministic output ordering."""
    funcs = list(callables or [])
    if not funcs:
        return []

    worker_count = max(1, min(int(max_workers), len(funcs)))
    if worker_count == 1:
        out: List[Any] = []
        for fn in funcs:
            try:
                out.append(fn())
            except Exception as exc:
                if return_exceptions:
                    out.append(exc)
                else:
                    raise
        return out

    ex = _pool(pool_name, worker_count)
    fut_to_idx = {ex.submit(fn): idx for idx, fn in enumerate(funcs)}
    out: List[Any] = [None] * len(funcs)
    deadline = None if timeout_seconds is None else (time.monotonic() + max(0.0, float(timeout_seconds)))
    pending = set(fut_to_idx.keys())

    while pending:
        if deadline is None:
            iterator = as_completed(pending)
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                for fut in pending:
                    fut.cancel()
                if return_exceptions:
                    for fut in pending:
                        idx = fut_to_idx[fut]
                        out[idx] = TimeoutError(
                            f"Parallel call timed out after {timeout_seconds}s (callable_index={idx})"
                        )
                    break
                pending_indices = sorted(fut_to_idx[f] for f in pending)
                raise TimeoutError(
                    f"Parallel call timed out after {timeout_seconds}s "
                    f"(pending_callable_indices={pending_indices})"
                )
            iterator = as_completed(pending, timeout=remaining)

        progressed = False
        try:
            for fut in iterator:
                progressed = True
                pending.discard(fut)
                idx = fut_to_idx[fut]
                try:
                    out[idx] = fut.result()
                except Exception as exc:
                    if return_exceptions:
                        out[idx] = exc
                    else:
                        raise
        except TimeoutError:
            continue

        if not progressed:
            break

    return out
