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
    with _POOL_GUARD:
        pools = list(_POOLS.values())
        _POOLS.clear()
    for ex in pools:
        ex.shutdown(wait=wait, cancel_futures=True)


atexit.register(shutdown_worker_pools)


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
                timeout_exc = TimeoutError(f"Parallel call timed out after {timeout_seconds}s")
                if return_exceptions:
                    for fut in pending:
                        idx = fut_to_idx[fut]
                        out[idx] = timeout_exc
                    break
                raise timeout_exc
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
