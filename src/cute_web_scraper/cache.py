"""Bounded TTL cache so composing tools does not re-fetch the same page."""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Generic, TypeVar

T = TypeVar("T")

_CacheKey = tuple[str, bool]


class PageCache(Generic[T]):
    """Least-recently-used cache with a wall-clock TTL, keyed by (url, js_render)."""

    def __init__(
        self,
        *,
        ttl_s: int,
        max_entries: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_s = ttl_s
        self._max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[_CacheKey, tuple[float, T]] = OrderedDict()

    def size(self) -> int:
        return len(self._entries)

    def get(self, url: str, js_render: bool) -> T | None:
        if self._ttl_s <= 0:
            return None
        key = (url, js_render)
        entry = self._entries.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if self._clock() - stored_at > self._ttl_s:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return value

    def put(self, url: str, js_render: bool, value: T) -> None:
        if self._ttl_s <= 0:
            return
        key = (url, js_render)
        self._entries[key] = (self._clock(), value)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
