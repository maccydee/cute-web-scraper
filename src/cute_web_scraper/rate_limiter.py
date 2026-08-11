"""Adaptive per-domain rate limiting with block-triggered backoff."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .config import Config

_MAX_DELAY_S = 60.0
_BACKOFF_SEED_S = 0.5
"""Seeds the doubling when the base delay is 0, so `SCRAPER_DELAY_MS=0` disables the
polite delay without also disabling block backoff."""
_DECAY_FACTOR = 0.9
_DEFAULT_MAX_DOMAINS = 10_000


@dataclass
class _DomainState:
    delay_s: float
    last_request_at: float | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class DomainRateLimiter:
    """Serialises requests per domain and widens the gap when a domain pushes back."""

    def __init__(self, config: Config, *, max_domains: int = _DEFAULT_MAX_DOMAINS) -> None:
        self._base_s = config.delay_ms / 1000
        self._max_domains = max_domains
        self._states: OrderedDict[str, _DomainState] = OrderedDict()

    def tracked_domains(self) -> int:
        return len(self._states)

    def current_delay(self, url: str) -> float:
        state = self._states.get(_domain_of(url))
        return self._base_s if state is None else state.delay_s

    async def wait(self, url: str) -> None:
        state = self._state_for(_domain_of(url))
        # The lock is held across the sleep so same-domain callers queue behind each
        # other. Different domains hold different locks and never interact.
        async with state.lock:
            loop = asyncio.get_running_loop()
            if state.last_request_at is not None:
                # (MEASURED, seconds) remaining time owed before the next request
                gap = state.delay_s - (loop.time() - state.last_request_at)
                if gap > 0:
                    await asyncio.sleep(gap)
            state.last_request_at = loop.time()

    def record_block(self, url: str) -> None:
        state = self._state_for(_domain_of(url))
        seed = state.delay_s if state.delay_s > 0 else _BACKOFF_SEED_S
        state.delay_s = min(seed * 2, _MAX_DELAY_S)

    def record_success(self, url: str) -> None:
        state = self._state_for(_domain_of(url))
        state.delay_s = max(self._base_s, state.delay_s * _DECAY_FACTOR)
        if state.delay_s - self._base_s < 1e-6:
            state.delay_s = self._base_s

    def _state_for(self, domain: str) -> _DomainState:
        state = self._states.get(domain)
        if state is None:
            state = _DomainState(delay_s=self._base_s)
            self._states[domain] = state
        self._states.move_to_end(domain)
        # Unbounded per-domain state is a slow leak in long-running HTTP mode.
        while len(self._states) > self._max_domains:
            self._states.popitem(last=False)
        return state


def _domain_of(url: str) -> str:
    return urlparse(url).netloc.lower()
