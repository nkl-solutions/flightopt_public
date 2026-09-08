"""Der Pacer laesst mehrere Anfragen gleichzeitig laufen, haelt aber die Rate.

Alle Zeitmessungen laufen ueber eine virtuelle Uhr. Ein Test, der echte
Sekunden abwartet, misst die Testmaschine mit und wird auf einem langsamen
Rechner rot, ohne dass sich am Verhalten etwas geaendert hat.
"""

from __future__ import annotations

import asyncio

from flightopt.sources.base import HttpSource, RateLimiter


class VirtualClock:
    """`monotonic`/`sleep`-Ersatz, der nur springt, wenn alle Tasks warten.

    Ein Schlaefer traegt seinen Weckzeitpunkt ein und blockiert. Sobald nichts
    mehr laufen kann, zieht `run` die Uhr auf den naechsten faelligen Termin
    vor. Damit ist die Reihenfolge der Aufwecker exakt die, die eine echte Uhr
    ergaebe - nur ohne Wartezeit.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self._waiters: list[tuple[float, asyncio.Event]] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        if delay <= 0:
            await asyncio.sleep(0)
            return
        due = self.now + delay
        event = asyncio.Event()
        self._waiters.append((due, event))
        await event.wait()

    async def run(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        while not task.done():
            # Erst alles laufen lassen, was ohne Zeitfortschritt laufen kann.
            for _ in range(50):
                await asyncio.sleep(0)
                if task.done():
                    break
            if task.done() or not self._waiters:
                break
            due = min(d for d, _ in self._waiters)
            self.now = max(self.now, due)
            pending: list[tuple[float, asyncio.Event]] = []
            for moment, event in self._waiters:
                if moment <= self.now:
                    event.set()
                else:
                    pending.append((moment, event))
            self._waiters = pending
        await task


async def test_concurrency_lets_four_calls_run_at_once():
    """Acht Aufrufe, vier Slots: hoechstens vier gleichzeitig, aber nie nur einer."""
    clock = VirtualClock()
    limiter = RateLimiter(
        60, concurrency=4, sleep=clock.sleep, monotonic=clock.monotonic
    )

    in_flight = 0
    peak = 0

    async def call() -> None:
        nonlocal in_flight, peak
        async with limiter.slot():
            in_flight += 1
            peak = max(peak, in_flight)
            # Deutlich laenger als das Taktintervall, damit wirklich der
            # Semaphor die Grenze setzt und nicht die Rate. Genau diese
            # Antwortzeit hielt der alte Limiter unter seinem Lock fest.
            await clock.sleep(100.0)
            in_flight -= 1

    await clock.run(asyncio.gather(*(call() for _ in range(8))))

    assert peak == 4
    assert in_flight == 0


async def test_serial_limiter_never_overlaps():
    """Gegenprobe: mit concurrency=1 bleibt es beim alten Reihum-Verhalten."""
    clock = VirtualClock()
    limiter = RateLimiter(60, sleep=clock.sleep, monotonic=clock.monotonic)

    in_flight = 0
    peak = 0

    async def call() -> None:
        nonlocal in_flight, peak
        async with limiter.slot():
            in_flight += 1
            peak = max(peak, in_flight)
            await clock.sleep(100.0)
            in_flight -= 1

    await clock.run(asyncio.gather(*(call() for _ in range(4))))

    assert peak == 1


async def test_average_rate_survives_the_concurrency():
    """Zehn Aufrufe bei 60/min starten ueber mindestens neun Intervalle verteilt."""
    clock = VirtualClock()
    limiter = RateLimiter(
        60, concurrency=4, sleep=clock.sleep, monotonic=clock.monotonic
    )
    starts: list[float] = []

    async def call() -> None:
        async with limiter.slot():
            starts.append(clock.monotonic())

    await clock.run(asyncio.gather(*(call() for _ in range(10))))

    assert len(starts) == 10
    starts.sort()
    span = starts[-1] - starts[0]
    # min_interval ist 1,0 s bei 60/min; neun Luecken zwischen zehn Starts.
    assert span >= 9 * limiter.min_interval
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert all(gap >= limiter.min_interval for gap in gaps)


async def test_jitter_is_still_applied():
    """Ohne Jitter waeren die Abstaende exakt gleich - genau das faellt auf."""
    clock = VirtualClock()
    limiter = RateLimiter(
        60, concurrency=4, sleep=clock.sleep, monotonic=clock.monotonic
    )
    starts: list[float] = []

    async def call() -> None:
        async with limiter.slot():
            starts.append(clock.monotonic())

    await clock.run(asyncio.gather(*(call() for _ in range(12))))

    starts.sort()
    gaps = [round(b - a, 6) for a, b in zip(starts, starts[1:])]
    assert len(set(gaps)) > 1


def test_old_positional_signature_still_works():
    limiter = RateLimiter(30, (0.0, 0.0))
    assert limiter.min_interval == 2.0
    assert limiter.jitter == (0.0, 0.0)
    assert limiter.concurrency == 1


def test_http_source_hands_its_concurrency_to_the_limiter():
    class Demo(HttpSource):
        name = "demo"
        per_minute = 42
        concurrency = 3

    src = Demo()
    assert src.limiter.concurrency == 3
    assert src.limiter.min_interval == 60.0 / 42

    override = Demo(per_minute=10, concurrency=1)
    assert override.limiter.concurrency == 1
    assert override.limiter.min_interval == 6.0
