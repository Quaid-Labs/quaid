"""Unit tests for lib/worker_pool.py.

Covers run_callables() — empty input, sequential fallback, parallel execution,
deterministic ordering, exception propagation, timeout paths, pool reuse,
and shutdown.
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from lib import worker_pool


@pytest.fixture(autouse=True)
def clean_pools():
    """Reset the shared pool registry between tests."""
    worker_pool.shutdown_worker_pools(wait=True)
    yield
    worker_pool.shutdown_worker_pools(wait=True)


# ---------------------------------------------------------------------------
# Empty / trivial inputs
# ---------------------------------------------------------------------------


class TestEmptyAndTrivial:
    def test_empty_callables_returns_empty_list(self):
        assert worker_pool.run_callables([], max_workers=4) == []

    def test_none_callables_returns_empty_list(self):
        assert worker_pool.run_callables(None, max_workers=4) == []

    def test_single_callable(self):
        out = worker_pool.run_callables([lambda: 42], max_workers=4)
        assert out == [42]

    def test_single_callable_sequential_path(self):
        """max_workers=1 forces the sequential code path."""
        out = worker_pool.run_callables([lambda: "hello"], max_workers=1)
        assert out == ["hello"]


# ---------------------------------------------------------------------------
# Sequential path (worker_count=1)
# ---------------------------------------------------------------------------


class TestSequentialPath:
    def test_sequential_when_max_workers_is_one(self):
        calls = []
        out = worker_pool.run_callables(
            [lambda i=i: calls.append(i) or i for i in range(3)],
            max_workers=1,
        )
        assert out == [0, 1, 2]
        assert calls == [0, 1, 2]

    def test_sequential_exception_propagates(self):
        def boom():
            raise ValueError("sequential boom")

        with pytest.raises(ValueError, match="sequential boom"):
            worker_pool.run_callables([boom], max_workers=1)

    def test_sequential_return_exceptions_captures(self):
        def boom():
            raise RuntimeError("captured")

        out = worker_pool.run_callables([boom, lambda: "ok"], max_workers=1, return_exceptions=True)
        assert len(out) == 2
        assert isinstance(out[0], RuntimeError)
        assert out[1] == "ok"


# ---------------------------------------------------------------------------
# Parallel path
# ---------------------------------------------------------------------------


class TestParallelExecution:
    def test_parallel_results_in_input_order(self):
        """Output order matches input order regardless of completion order."""
        # Use different sleep times so callables complete out of submission order
        out = worker_pool.run_callables(
            [lambda: (time.sleep(0.04) or "slow"), lambda: (time.sleep(0.0) or "fast")],
            max_workers=2,
            pool_name="test-order",
        )
        assert out == ["slow", "fast"]

    def test_all_callables_run(self):
        results = []
        lock = threading.Lock()

        def task(i):
            with lock:
                results.append(i)
            return i

        out = worker_pool.run_callables(
            [lambda i=i: task(i) for i in range(8)],
            max_workers=4,
            pool_name="test-all-run",
        )
        assert sorted(out) == list(range(8))
        assert sorted(results) == list(range(8))

    def test_max_workers_capped_to_callable_count(self):
        """max_workers > len(callables) still works correctly."""
        out = worker_pool.run_callables(
            [lambda: "x", lambda: "y"],
            max_workers=100,
            pool_name="test-capped",
        )
        assert out == ["x", "y"]

    def test_parallel_exception_propagates(self):
        def boom():
            raise ValueError("parallel boom")

        with pytest.raises(ValueError, match="parallel boom"):
            worker_pool.run_callables([boom, lambda: "ok"], max_workers=2, pool_name="test-exc")

    def test_parallel_return_exceptions_captures(self):
        def boom():
            raise RuntimeError("parallel captured")

        out = worker_pool.run_callables(
            [boom, lambda: "ok"],
            max_workers=2,
            pool_name="test-exc-capture",
            return_exceptions=True,
        )
        assert len(out) == 2
        assert isinstance(out[0], RuntimeError)
        assert out[1] == "ok"


# ---------------------------------------------------------------------------
# Timeout paths
# ---------------------------------------------------------------------------


class TestTimeout:
    def test_timeout_raises_when_not_return_exceptions(self):
        with pytest.raises(TimeoutError, match="pending_callable_indices"):
            worker_pool.run_callables(
                [lambda: time.sleep(0.3), lambda: time.sleep(0.3)],
                max_workers=2,
                pool_name="test-timeout-raise",
                timeout_seconds=0.01,
                return_exceptions=False,
            )

    def test_timeout_return_exceptions_stores_timeout_per_item(self):
        out = worker_pool.run_callables(
            [lambda: time.sleep(0.3), lambda: time.sleep(0.3)],
            max_workers=2,
            pool_name="test-timeout-per-item",
            timeout_seconds=0.01,
            return_exceptions=True,
        )
        assert len(out) == 2
        assert all(isinstance(item, TimeoutError) for item in out)
        assert "callable_index=0" in str(out[0])
        assert "callable_index=1" in str(out[1])

    def test_no_timeout_completes_normally(self):
        out = worker_pool.run_callables(
            [lambda: "done"],
            max_workers=1,
            timeout_seconds=5.0,
        )
        assert out == ["done"]


# ---------------------------------------------------------------------------
# Pool reuse
# ---------------------------------------------------------------------------


class TestPoolReuse:
    def test_same_name_and_size_returns_same_pool(self):
        # Use 2 callables to force parallel path (1 callable → sequential, no pool created)
        worker_pool.run_callables([lambda: 1, lambda: 2], max_workers=2, pool_name="reuse-test")
        pool_a = worker_pool._POOLS.get(("reuse-test", 2))

        worker_pool.run_callables([lambda: 3, lambda: 4], max_workers=2, pool_name="reuse-test")
        pool_b = worker_pool._POOLS.get(("reuse-test", 2))

        assert pool_a is pool_b

    def test_different_names_create_different_pools(self):
        # Use 2 callables each to force parallel path and pool creation
        worker_pool.run_callables([lambda: 1, lambda: 2], max_workers=2, pool_name="pool-alpha")
        worker_pool.run_callables([lambda: 3, lambda: 4], max_workers=2, pool_name="pool-beta")
        assert ("pool-alpha", 2) in worker_pool._POOLS
        assert ("pool-beta", 2) in worker_pool._POOLS


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    def test_shutdown_clears_registry(self):
        # Use 2 callables to force parallel path (1 callable → sequential, no pool created)
        worker_pool.run_callables([lambda: 1, lambda: 2], max_workers=2, pool_name="pre-shutdown")
        assert worker_pool._POOLS
        worker_pool.shutdown_worker_pools(wait=False)
        assert worker_pool._POOLS == {}

    def test_run_after_shutdown_creates_new_pool(self):
        worker_pool.run_callables([lambda: 1], max_workers=2, pool_name="shutdown-recreate")
        worker_pool.shutdown_worker_pools(wait=True)
        out = worker_pool.run_callables([lambda: "new"], max_workers=2, pool_name="shutdown-recreate")
        assert out == ["new"]
