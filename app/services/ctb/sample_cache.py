"""Per-job, bounded, single-flight cache of immutable sampled height grids."""

from collections import OrderedDict
from concurrent.futures import Future
from threading import Lock

import numpy as np


class SampleCache:
    def __init__(self, max_bytes: int):
        self.max_bytes = max(0, max_bytes)
        self._bytes = 0
        self._ready = OrderedDict()
        self._pending = {}
        self._lock = Lock()

    def get(self, key, compute):
        with self._lock:
            if key in self._ready:
                self._ready.move_to_end(key)
                return self._ready[key]
            future = self._pending.get(key)
            owner = future is None
            if owner:
                future = Future()
                self._pending[key] = future
        if not owner:
            return future.result()
        try:
            value = np.asarray(compute())
            value.flags.writeable = False
            with self._lock:
                if value.nbytes <= self.max_bytes:
                    while self._bytes + value.nbytes > self.max_bytes:
                        _, old = self._ready.popitem(last=False)
                        self._bytes -= old.nbytes
                    self._ready[key] = value
                    self._bytes += value.nbytes
                self._pending.pop(key)
                future.set_result(value)
            return value
        except BaseException as exc:
            with self._lock:
                self._pending.pop(key, None)
                future.set_exception(exc)
            raise
