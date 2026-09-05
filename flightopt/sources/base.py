"""Shared adapter plumbing: polite pacing, retries, circuit breaking.

Every source goes through `HttpSource.fetch_json`, so rate limiting and
back-off are enforced in one place rather than per adapter.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import date
from typing import Any

from flightopt.domain.models import Cabin, Money, Offer, Pax

logger = logging.getLogger(__name__)


class SourceError(RuntimeError):
    """Adapter could not produce data. Never fatal to a whole search."""


class SourceBlocked(SourceError):
    """The remote side rejected us (429/403). Trips the circuit breaker."""


class RateLimiter:
    """Token-bucket-ish pacer: at most `per_minute` calls, with jitter.

    Jitter matters more than the exact rate: evenly spaced requests look
    synthetic, and that pattern is itself a bot signal.
    """

    def __init__(self, per_minute: int = 20, jitter: tuple[float, float] = (0.4, 1.6)):
        self.min_interval = 60.0 / max(1, per_minute)
        self.jitter = jitter
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._last + self.min_interval - now
            delay += random.uniform(*self.jitter)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


class CircuitBreaker:
    """After `threshold` consecutive blocks, stop calling for `cooldown` seconds.

    Hammering a source that just blocked us makes the block longer, so the
    breaker protects access more than it protects latency.
    """

    def __init__(self, threshold: int = 3, cooldown: float = 1800.0):
        self.threshold = threshold
        self.cooldown = cooldown
        self.failures = 0
        self.opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if time.monotonic() - self.opened_at >= self.cooldown:
            self.reset()
            return False
        return True

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_block(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()
            logger.warning("circuit opened after %d blocks", self.failures)

    def reset(self) -> None:
        self.failures = 0
        self.opened_at = None

    def remaining(self) -> float:
        if self.opened_at is None:
            return 0.0
        return max(0.0, self.cooldown - (time.monotonic() - self.opened_at))


class HttpSource:
    """Base for adapters that talk plain JSON over HTTPS."""

    name: str = "base"
    carrier: str = ""
    """IATA code of the airline this adapter prices, for filtering and labels."""
    also_carriers: tuple[str, ...] = ()
    """Further airlines this adapter covers, when one booking engine serves
    several brands. A filter on any of them has to keep the adapter."""
    supports_calendar: bool = False
    supports_search: bool = False
    indicative: bool = False
    """True when this source's prices are a guide rather than a bookable fare."""
    per_minute: int = 20
    impersonate: str = "chrome"

    def __init__(self, *, per_minute: int | None = None, proxy: str | None = None):
        self.limiter = RateLimiter(per_minute or self.per_minute)
        self.breaker = CircuitBreaker()
        self.proxy = proxy

    @property
    def carriers(self) -> tuple[str, ...]:
        """Every airline this adapter can price."""
        return tuple(c for c in (self.carrier, *self.also_carriers) if c)

    def supports_route(self, origin: str, destination: str) -> bool:
        return True

    async def fetch_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        retries: int = 3,
        timeout: int = 30,
        session: Any = None,
    ) -> Any:
        """GET, or POST when `json_body` is given.

        `session` lets an adapter reuse cookies it had to collect first; without
        one a fresh connection is made per call.
        """
        if self.breaker.is_open:
            raise SourceBlocked(
                f"{self.name}: circuit open, {self.breaker.remaining():.0f}s left"
            )

        from curl_cffi import requests as creq

        last: Exception | None = None
        for attempt in range(retries):
            await self.limiter.wait()
            try:
                if session is not None:
                    call = session.post if json_body is not None else session.get
                    kwargs: dict[str, Any] = {}
                else:
                    call = creq.post if json_body is not None else creq.get
                    kwargs = {"impersonate": self.impersonate, "proxy": self.proxy}
                if json_body is not None:
                    kwargs["json"] = json_body
                if params:
                    kwargs["params"] = params
                resp = await asyncio.to_thread(
                    call, url, headers=headers or {}, timeout=timeout, **kwargs
                )
            except Exception as exc:  # network layer
                last = exc
                await self._backoff(attempt)
                continue

            if resp.status_code in (429, 403):
                self.breaker.record_block()
                last = SourceBlocked(f"{self.name}: HTTP {resp.status_code}")
                await self._backoff(attempt, base=5.0)
                continue
            if resp.status_code >= 500:
                last = SourceError(f"{self.name}: HTTP {resp.status_code}")
                await self._backoff(attempt)
                continue
            if resp.status_code != 200:
                raise SourceError(f"{self.name}: HTTP {resp.status_code} {resp.text[:150]}")

            self.breaker.record_success()
            try:
                return resp.json()
            except Exception as exc:
                raise SourceError(f"{self.name}: bad JSON: {exc}") from exc

        raise last or SourceError(f"{self.name}: exhausted {retries} attempts")

    @staticmethod
    async def _backoff(attempt: int, base: float = 1.0) -> None:
        # Full jitter: spreads retries instead of synchronising them.
        delay = random.uniform(0, base * (2**attempt))
        await asyncio.sleep(delay)

    # Adapters override whichever of these they can serve.

    async def calendar(
        self, origin: str, destination: str, month: date, *, currency: str = "EUR"
    ) -> dict[date, Money]:
        raise NotImplementedError(f"{self.name} has no calendar endpoint")

    async def search_leg(
        self,
        origin: str,
        destination: str,
        day: date,
        *,
        pax: Pax = Pax(),
        cabin: Cabin = Cabin.ECONOMY,
        currency: str = "EUR",
    ) -> list[Offer]:
        raise NotImplementedError(f"{self.name} has no per-leg search")
