"""Ordered parallel map so tiled writers can stay streaming."""

from __future__ import annotations

import math
import os
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


def default_workers() -> int:
    """Respect affinity, Docker CPU quota and an optional raster worker cap."""
    count = os.cpu_count() or 1
    try:
        count = min(count, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        pass
    quota = _cpu_quota_count()
    if quota is not None:
        count = min(count, quota)
    setting = os.environ.get("OTH_RASTER_WORKERS")
    if setting is not None:
        try:
            requested = int(setting)
        except ValueError as exc:
            raise ValueError("OTH_RASTER_WORKERS must be a positive integer") from exc
        if requested < 1:
            raise ValueError("OTH_RASTER_WORKERS must be a positive integer")
        count = min(count, requested)
    return max(1, count)


def _cpu_quota_count() -> int | None:
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            return max(1, math.ceil(int(quota) / int(period)))
    except (OSError, ValueError, ZeroDivisionError):
        pass
    try:
        root = Path("/sys/fs/cgroup/cpu")
        quota = int((root / "cpu.cfs_quota_us").read_text())
        period = int((root / "cpu.cfs_period_us").read_text())
        if quota > 0 and period > 0:
            return max(1, math.ceil(quota / period))
    except (OSError, ValueError):
        pass
    return None


def raster_workers(cache_bytes: int, task_bytes: int, requested: int | None) -> int:
    # Half for reader cache, half for the ordered map's <= 2*workers tasks.
    workers = default_workers() if requested is None else max(1, int(requested))
    return max(1, min(workers, max(1, cache_bytes // max(4 * task_bytes, 1))))


def ordered_parallel_map(
    items: Iterable[T],
    fn: Callable[[T], R],
    *,
    workers: int,
) -> Iterator[R]:
    """Apply ``fn`` with a thread pool, yielding results in input order."""
    if workers <= 1:
        for item in items:
            yield fn(item)
        return

    workers = max(1, int(workers))
    prefetch = max(workers * 2, workers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        iterator = iter(items)
        pending: deque = deque()

        def _refill() -> None:
            while len(pending) < prefetch:
                try:
                    item = next(iterator)
                except StopIteration:
                    return
                pending.append(pool.submit(fn, item))

        _refill()
        while pending:
            result = pending.popleft().result()
            _refill()
            yield result


def unordered_parallel_map(
    items: Iterable[T],
    fn: Callable[[T], R],
    *,
    workers: int,
) -> Iterator[R]:
    if workers <= 1:
        for item in items:
            yield fn(item)
        return
    workers = max(1, int(workers))
    prefetch = max(workers * 4, workers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        iterator = iter(items)
        inflight: set = set()

        def _submit() -> bool:
            try:
                item = next(iterator)
            except StopIteration:
                return False
            inflight.add(pool.submit(fn, item))
            return True

        while len(inflight) < prefetch and _submit():
            pass
        while inflight:
            for future in as_completed(tuple(inflight)):
                inflight.remove(future)
                yield future.result()
                _submit()
                break


def run_unordered(
    items: Iterable[T],
    fn: Callable[[T], None],
    *,
    workers: int,
    on_done: Callable[[], None] | None = None,
) -> None:
    """Apply ``fn`` with a bounded thread pool; completion order is undefined."""
    if workers <= 1:
        for item in items:
            fn(item)
            if on_done is not None:
                on_done()
        return

    workers = max(1, int(workers))
    prefetch = max(workers * 4, workers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        iterator = iter(items)
        inflight: set = set()

        def _submit() -> bool:
            try:
                item = next(iterator)
            except StopIteration:
                return False
            inflight.add(pool.submit(fn, item))
            return True

        while len(inflight) < prefetch and _submit():
            pass
        while inflight:
            done, inflight = wait(inflight, return_when=FIRST_COMPLETED)
            for fut in done:
                fut.result()
                if on_done is not None:
                    on_done()
                _submit()
