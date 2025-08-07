"""Mock executors for test performance optimization."""

import threading
from collections.abc import Callable
from concurrent.futures import Future


class MockThreadPoolExecutor:
    """Mock ThreadPoolExecutor that executes tasks synchronously for faster tests."""

    def __init__(self, max_workers=None, **kwargs) -> None:
        self.max_workers = max_workers or 1
        self._shutdown = False
        self._lock = threading.Lock()

    def submit(self, fn: Callable, *args, **kwargs) -> Future:
        """Submit a task for execution (runs synchronously)."""
        if self._shutdown:
            msg = "Cannot submit to a shutdown executor"
            raise RuntimeError(msg)

        future = Future()
        try:
            result = fn(*args, **kwargs)
            future.set_result(result)
        except Exception as e:
            future.set_exception(e)
        return future

    def map(self, fn: Callable, *iterables, timeout=None, chunksize=1):
        """Map function over iterables (runs synchronously)."""
        if self._shutdown:
            msg = "Cannot map on a shutdown executor"
            raise RuntimeError(msg)

        results = []
        for args in zip(*iterables, strict=False):
            try:
                result = fn(*args)
                results.append(result)
            except Exception as e:
                results.append(e)
        return results

    def shutdown(self, wait=True, *, cancel_futures=False) -> None:
        """Shutdown the executor."""
        with self._lock:
            self._shutdown = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown()


def patch_thread_pool_executor():
    """Patch ThreadPoolExecutor to use MockThreadPoolExecutor for faster tests."""
    import concurrent.futures

    original_executor = concurrent.futures.ThreadPoolExecutor

    def mock_executor(*args, **kwargs):
        return MockThreadPoolExecutor(*args, **kwargs)

    concurrent.futures.ThreadPoolExecutor = mock_executor
    return original_executor


def restore_thread_pool_executor(original_executor) -> None:
    """Restore the original ThreadPoolExecutor."""
    import concurrent.futures

    concurrent.futures.ThreadPoolExecutor = original_executor
