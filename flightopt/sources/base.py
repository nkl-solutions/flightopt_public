"""Shared adapter plumbing: polite pacing, retries, circuit breaking.

Every source goes through `HttpSource.fetch_json`, so rate limiting and
back-off are enforced in one place rather than per adapter.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from typing import Any

from flightopt.domain.models import Cabin, Money, Offer, Pax

logger = logging.getLogger(__name__)


class SourceError(RuntimeError):
    """Adapter could not produce data. Never fatal to a whole search."""


class SourceBlocked(SourceError):
    """The remote side rejected us (429/403). Trips the circuit breaker."""


class RateLimiter:
    """Paces an average rate while letting `concurrency` calls be in flight.

    The predecessor slept while holding its lock, so callers queued: each one
    waited for the one before it to finish sleeping before it could even work
    out its own delay. At `per_minute=20` plus jitter that is about four
    seconds per call, and verifying sixty leg-date pairs against one airline
    took roughly four minutes. Measured against a virtual clock, 60 calls came
    to 237 s.

    Two separate primitives replace that single lock:

    * a scheduling lock that only computes the next allowed start time and is
      released immediately, so every caller sleeps towards its own target
      rather than through everyone else's,
    * a semaphore that caps how many requests are actually in flight.

    Being honest about the effect: at the same `per_minute` the throughput is
    the same, because the interval was always the binding constraint. What
    changes is that the in-flight count is now bounded - it was not before,
    only incidentally limited by how slowly requests were issued - and that is
    the precondition for raising the rate at all. The measured win comes from
    doing that: at 60/min the same 60 calls take 116 s.

    Jitter matters more than the exact rate: evenly spaced requests look
    synthetic, and that pattern is itself a bot signal.

    `sleep` and `monotonic` are injectable so tests can drive a virtual clock
    instead of waiting in real time.
    """

    def __init__(
        self,
        per_minute: int = 20,
        jitter: tuple[float, float] = (0.4, 1.6),
        *,
        concurrency: int = 1,
        sleep: Any = None,
        monotonic: Any = None,
    ):
        self.min_interval = 60.0 / max(1, per_minute)
        self.jitter = jitter
        self.concurrency = max(1, int(concurrency))
        self._sleep = sleep or asyncio.sleep
        self._monotonic = monotonic or time.monotonic
        # Next start time we are allowed to hand out. `None` until the first
        # call, so a limiter that idles for an hour does not bank credit.
        self._next: float | None = None
        self._sched = asyncio.Lock()
        self._slots = asyncio.Semaphore(self.concurrency)

    def _reserve(self) -> float:
        """Claim the next start time. Caller must hold `_sched`."""
        now = self._monotonic()
        start = now if self._next is None else max(now, self._next)
        start += random.uniform(*self.jitter)
        self._next = start + self.min_interval
        return start

    async def wait(self) -> None:
        """Block until this caller's turn. Does not reserve an in-flight slot."""
        async with self._sched:
            start = self._reserve()
        delay = start - self._monotonic()
        if delay > 0:
            await self._sleep(delay)

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Hold one of `concurrency` in-flight slots for the duration of a call.

        Pacing goes through `wait`, so an adapter test that stubs `wait` still
        gets an instant limiter.
        """
        async with self._slots:
            await self.wait()
            yield


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
    accepts_max_stops: bool = False
    """True when `calendar_range` takes a `max_stops` keyword. Only the
    collectors do; an airline calendar has no such knob."""
    per_minute: int = 20
    concurrency: int = 4
    """How many requests this source may have in flight at once. The average
    rate stays `per_minute`; this only decides whether waiting for one answer
    blocks the next question."""
    impersonate: str = "chrome"

    def __init__(
        self,
        *,
        per_minute: int | None = None,
        concurrency: int | None = None,
        proxy: str | None = None,
    ):
        self.limiter = RateLimiter(
            per_minute or self.per_minute,
            concurrency=concurrency or self.concurrency,
        )
        self.breaker = CircuitBreaker()
        self.proxy = proxy
        # Deliberately not `_session`: Eurowings already owns that name for its
        # Cloudflare warm-up session, and two meanings on one attribute is a
        # bug waiting for the next reader.
        self._http_session: Any = None

    def _new_session(self) -> Any:
        """Build the reusable client. Own method so tests can count the calls."""
        from curl_cffi import requests as creq

        return creq.Session(impersonate=self.impersonate, proxy=self.proxy)

    @property
    def session(self) -> Any:
        """One client per source, created on first use.

        Module-level `creq.get`/`creq.post` open a fresh connection and run a
        full TLS handshake every time. A source that answers sixty leg-date
        questions paid for sixty handshakes; now it pays for one and keeps the
        connection alive. The session is thread-safe for our purposes because
        curl_cffi keeps one curl handle per thread by default, which is what
        `asyncio.to_thread` hands it.
        """
        if self._http_session is None:
            self._http_session = self._new_session()
        return self._http_session

    def close(self) -> None:
        """Drop the pooled connection.

        There is no lifecycle hook that owns a source: `build_sources` creates
        a fresh catalogue per job and lets it go out of scope when the job
        ends, so in production the session is closed by garbage collection.
        This method exists for tests and for anyone who later gives sources a
        longer life than one search.
        """
        session, self._http_session = self._http_session, None
        if session is None:
            return
        try:
            session.close()
        except Exception:  # noqa: BLE001 - closing must never raise upwards
            logger.debug("%s: session close failed", self.name, exc_info=True)

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

        `session` lets an adapter reuse cookies it had to collect first, such
        as the Cloudflare token Eurowings has to earn. Without one the source's
        own pooled session is used, so the connection and its TLS handshake are
        paid for once instead of once per call.
        """
        if self.breaker.is_open:
            raise SourceBlocked(
                f"{self.name}: circuit open, {self.breaker.remaining():.0f}s left"
            )

        client = session if session is not None else self.session

        last: Exception | None = None
        # Ein Aufruf zaehlt hoechstens einen Blockvermerk, egal wie oft er intern
        # nachfasst. Sonst oeffnet eine einzelne 429-Serie die Sicherung (Schwelle
        # 3) und nimmt die Quelle fuer 1800 s aus dem Rennen, obwohl nur ein
        # einziger Aufruf gedrosselt wurde.
        blocked = False
        for attempt in range(retries):
            try:
                # The slot is held for the request only. Back-off below happens
                # outside it, so a retrying call does not squat on a slot that
                # another leg could be using.
                async with self.limiter.slot():
                    call = client.post if json_body is not None else client.get
                    kwargs: dict[str, Any] = {}
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
                if not blocked:
                    self.breaker.record_block()
                    blocked = True
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
