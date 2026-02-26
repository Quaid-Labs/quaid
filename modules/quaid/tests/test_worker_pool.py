import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from lib import worker_pool


def test_shutdown_worker_pools_clears_pool_registry():
    out = worker_pool.run_callables(
        [lambda: 1, lambda: 2],
        max_workers=2,
        pool_name="test-shutdown",
    )
    assert out == [1, 2]
    assert worker_pool._POOLS

    worker_pool.shutdown_worker_pools(wait=False)
    assert worker_pool._POOLS == {}


def test_timeout_exception_includes_pending_callable_indices():
    with pytest.raises(TimeoutError, match="pending_callable_indices"):
        worker_pool.run_callables(
            [lambda: time.sleep(0.2), lambda: time.sleep(0.2)],
            max_workers=2,
            pool_name="test-timeout-indices",
            timeout_seconds=0.01,
            return_exceptions=False,
        )


def test_timeout_return_exceptions_include_callable_index():
    out = worker_pool.run_callables(
        [lambda: time.sleep(0.2), lambda: time.sleep(0.2)],
        max_workers=2,
        pool_name="test-timeout-index-per-item",
        timeout_seconds=0.01,
        return_exceptions=True,
    )
    assert len(out) == 2
    assert all(isinstance(item, TimeoutError) for item in out)
    assert "callable_index=0" in str(out[0])
    assert "callable_index=1" in str(out[1])
