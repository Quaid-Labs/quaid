import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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
