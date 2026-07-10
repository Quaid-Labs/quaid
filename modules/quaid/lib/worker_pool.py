"""Shared worker pools for bounded parallel execution."""

from __future__ import annotations

import atexit
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


_POOL_GUARD = threading.Lock()
_POOLS: Dict[Tuple[str, int], ThreadPoolExecutor] = {}


def _quiet_env_enabled() -> bool:
    value = str(os.environ.get("QUAID_QUIET", "") or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _pool(pool_name: str, max_workers: int) -> ThreadPoolExecutor:
    key = (str(pool_name or "default"), max(1, int(max_workers)))
    with _POOL_GUARD:
        ex = _POOLS.get(key)
        if ex is None:
            ex = ThreadPoolExecutor(max_workers=key[1], thread_name_prefix=f"quaid-{key[0]}")
            _POOLS[key] = ex
        return ex


def _retire_pool(pool_name: str, max_workers: int, executor: ThreadPoolExecutor) -> None:
    """Remove a timed-out executor so later calls do not queue behind stuck work."""
    key = (str(pool_name or "default"), max(1, int(max_workers)))
    with _POOL_GUARD:
        current = _POOLS.get(key)
        if current is executor:
            _POOLS.pop(key, None)
    executor.shutdown(wait=False, cancel_futures=True)


def shutdown_worker_pools(wait: bool = False) -> None:
    """Shutdown and clear shared thread pools."""
    import sys
    with _POOL_GUARD:
        pool_items = list(_POOLS.items())
        _POOLS.clear()
    quiet = _quiet_env_enabled()
    if pool_items:
        if not quiet:
            print(f"[worker_pool][atexit] shutting down {len(pool_items)} pool(s), wait={wait}", flush=True, file=sys.stderr)
    for key, ex in pool_items:
        running = getattr(ex, "_work_queue", None)
        qsize = running.qsize() if running is not None else "?"
        if not quiet:
            print(f"[worker_pool][atexit]   pool={key[0]!r} max_workers={key[1]} queue_depth={qsize}", flush=True, file=sys.stderr)
        ex.shutdown(wait=wait, cancel_futures=True)
        if not quiet:
            print(f"[worker_pool][atexit]   pool={key[0]!r} shutdown complete", flush=True, file=sys.stderr)
    if pool_items and not quiet:
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
    if timeout_seconds is not None and float(timeout_seconds) <= 0:
        timeout_seconds = None

    worker_count = max(1, min(int(max_workers), len(funcs)))
    if worker_count == 1 and timeout_seconds is None:
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
    timed_out = False

    while pending:
        remaining = None
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
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
                # Retire before raising; the post-loop retire only covers return_exceptions=True.
                _retire_pool(pool_name, worker_count, ex)
                raise TimeoutError(
                    f"Parallel call timed out after {timeout_seconds}s "
                    f"(pending_callable_indices={pending_indices})"
                )

        progressed = False
        try:
            if deadline is None:
                iterator = as_completed(pending)
            else:
                iterator = as_completed(pending, timeout=remaining)

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
                        for pending_fut in pending:
                            pending_fut.cancel()
                        if pending:
                            _retire_pool(pool_name, worker_count, ex)
                        raise
        except (TimeoutError, FuturesTimeoutError):
            continue

        if not progressed:
            break

    if timed_out:
        _retire_pool(pool_name, worker_count, ex)

    return out
