"""Der langlebige Chromium: was er haelt, wann er weggeht, was er kostet.

Kein echter Browser in diesem Modul. Der Zaehler ist ein Doppel, das mitzaehlt,
wie oft gestartet und geschlossen wurde - genau die beiden Zahlen, an denen
Dauerbetrieb scheitert oder nicht.
"""

from __future__ import annotations

import asyncio

import pytest

from flightopt.hotels.browser import (
    DEFAULT_BUDGET,
    BrowserBudget,
    BrowserPool,
    budget_from_env,
    close_shared_pools,
    reset_shared_pools,
    shared_pool,
)


class FakeBrowser:
    def __init__(self, pool: "FakeLauncher", number: int) -> None:
        self.pool = pool
        self.number = number
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1
        self.pool.closed += 1

    async def new_context(self, **kwargs: object) -> object:  # pragma: no cover
        raise AssertionError("die Pool-Tests oeffnen keine Kontexte")


class FakeLauncher:
    """Zaehlt Starts und Schliessungen. Mehr braucht der Pool nicht zu wissen."""

    def __init__(self) -> None:
        self.started = 0
        self.closed = 0
        self.browsers: list[FakeBrowser] = []

    async def __call__(self) -> FakeBrowser:
        self.started += 1
        browser = FakeBrowser(self, self.started)
        self.browsers.append(browser)
        return browser


class Clock:
    """Eine Uhr, die nur vorgestellt wird, damit Tests keine Zeit absitzen."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        # Ein Kontextwechsel, damit der Aufrufer weiterlaufen kann.
        await asyncio.sleep(0)


def pool(launcher: FakeLauncher, clock: Clock, **budget: object) -> BrowserPool:
    return BrowserPool(
        launcher,
        budget=BrowserBudget(**budget) if budget else DEFAULT_BUDGET,
        clock=clock,
        sleep=clock.sleep,
    )


async def test_one_browser_carries_many_pages() -> None:
    """Der Punkt der Uebung: ein Start, viele Seiten."""
    launcher, clock = FakeLauncher(), Clock()
    browsers = pool(launcher, clock, max_pages=100, max_age_seconds=3600, idle_seconds=0)
    for _ in range(10):
        async with browsers.page() as browser:
            assert browser.number == 1
    await browsers.close()
    assert launcher.started == 1
    assert launcher.closed == 1


async def test_the_browser_restarts_after_its_page_budget() -> None:
    """Speicher, der langsam waechst, wird von einem Neustart abgeschnitten."""
    launcher, clock = FakeLauncher(), Clock()
    browsers = pool(launcher, clock, max_pages=3, max_age_seconds=3600, idle_seconds=0)
    seen = []
    for _ in range(7):
        async with browsers.page() as browser:
            seen.append(browser.number)
    await browsers.close()
    assert seen == [1, 1, 1, 2, 2, 2, 3]
    assert launcher.started == 3


async def test_the_browser_restarts_after_its_maximum_age() -> None:
    """Auch ein Browser mit wenigen Seiten wird irgendwann alt."""
    launcher, clock = FakeLauncher(), Clock()
    browsers = pool(launcher, clock, max_pages=1000, max_age_seconds=100, idle_seconds=0)
    async with browsers.page() as first:
        assert first.number == 1
    clock.now += 101
    async with browsers.page() as second:
        assert second.number == 2
    await browsers.close()
    assert launcher.started == 2


async def test_an_idle_browser_closes_itself() -> None:
    """Zwischen zwei Laeufen darf kein Chromium Speicher belegen.

    Genau das ist der Unterschied zwischen "langlebig" und "immer da": im
    Dauerbetrieb liegen zwischen zwei Durchgaengen Minuten, und in denen soll
    der Container so viel Speicher brauchen wie ohne Booking.
    """
    launcher, clock = FakeLauncher(), Clock()
    browsers = pool(launcher, clock, max_pages=1000, max_age_seconds=3600, idle_seconds=30)
    async with browsers.page():
        pass
    assert browsers.running
    for _ in range(20):
        await asyncio.sleep(0)
        if not browsers.running:
            break
    assert not browsers.running
    assert launcher.closed == 1
    await browsers.close()


async def test_a_hanging_page_throws_the_browser_away() -> None:
    """Nach einem Abbruch faengt der naechste Tag mit einem frischen Chromium an."""
    launcher, clock = FakeLauncher(), Clock()
    browsers = pool(launcher, clock, max_pages=1000, max_age_seconds=3600, idle_seconds=0)
    with pytest.raises(RuntimeError):
        async with browsers.page():
            raise RuntimeError("Challenge haengt")
    async with browsers.page() as browser:
        assert browser.number == 2
    await browsers.close()
    assert launcher.started == 2


async def test_two_concurrent_pages_share_one_browser() -> None:
    """Zwei Kontexte sind billig, zwei Chromium-Prozesse sind es nicht."""
    launcher, clock = FakeLauncher(), Clock()
    browsers = pool(launcher, clock, max_pages=1000, max_age_seconds=3600, idle_seconds=0)
    seen: list[int] = []
    ready = asyncio.Event()

    async def one(wait: bool) -> None:
        async with browsers.page() as browser:
            seen.append(browser.number)
            if wait:
                ready.set()
                await asyncio.sleep(0.01)
            else:
                await ready.wait()

    await asyncio.gather(one(True), one(False))
    await browsers.close()
    assert seen == [1, 1]
    assert launcher.started == 1


async def test_a_restart_waits_for_the_page_still_open() -> None:
    """Ein Browser, der unter einer offenen Seite weggeschlossen wird, ist ein Absturz."""
    launcher, clock = FakeLauncher(), Clock()
    browsers = pool(launcher, clock, max_pages=1, max_age_seconds=3600, idle_seconds=0)
    started = asyncio.Event()

    async def slow() -> None:
        async with browsers.page() as browser:
            started.set()
            await asyncio.sleep(0.02)
            # Waehrend dieser Seite darf niemand den Browser schliessen.
            assert browser.closed == 0

    async def fast() -> None:
        await started.wait()
        async with browsers.page():
            pass

    await asyncio.gather(slow(), fast())
    await browsers.close()
    assert launcher.started >= 1


async def test_close_closes_exactly_once() -> None:
    launcher, clock = FakeLauncher(), Clock()
    browsers = pool(launcher, clock, max_pages=10, max_age_seconds=3600, idle_seconds=0)
    async with browsers.page():
        pass
    await browsers.close()
    await browsers.close()
    assert launcher.closed == 1


async def test_a_browser_that_fails_to_close_breaks_nothing() -> None:
    """Aufraeumen darf nie nach oben werfen, sonst stirbt der Lauf am Ende."""
    launcher, clock = FakeLauncher(), Clock()
    browsers = pool(launcher, clock, max_pages=1, max_age_seconds=3600, idle_seconds=0)

    async def boom() -> None:
        raise RuntimeError("Chromium liess sich nicht schliessen")

    async with browsers.page() as browser:
        browser.close = boom  # type: ignore[method-assign]
    async with browsers.page() as second:
        assert second.number == 2
    await browsers.close()


async def test_the_stats_count_pages_starts_and_recycles() -> None:
    """Ohne Zahlen ist Dauerbetrieb Glaubenssache."""
    launcher, clock = FakeLauncher(), Clock()
    browsers = pool(launcher, clock, max_pages=2, max_age_seconds=3600, idle_seconds=0)
    for _ in range(5):
        async with browsers.page():
            pass
    stats = browsers.stats
    await browsers.close()
    assert stats["pages"] == 5
    assert stats["starts"] == 3
    assert stats["recycles"] == 2


def test_a_budget_without_pages_is_an_error() -> None:
    with pytest.raises(ValueError):
        BrowserBudget(max_pages=0)


def test_the_default_budget_limits_all_three_axes() -> None:
    """Kein `0` als Voreinstellung: unbegrenzt ist auf einem VPS keine Option."""
    assert DEFAULT_BUDGET.max_pages > 0
    assert DEFAULT_BUDGET.max_age_seconds > 0
    assert DEFAULT_BUDGET.idle_seconds > 0


# --------------------------------------------------------------------------
# Der gemeinsame Pool: einer je Prozess, nicht einer je Lauf
# --------------------------------------------------------------------------


async def test_the_shared_pool_is_one_per_process_and_name() -> None:
    """Zwei Chromium-Prozesse fuer dieselbe Quelle waeren doppelte Kosten.

    Der Durchlauf baut sich den Quellen-Katalog bei jedem Start neu. Haenge der
    Browser an der Quelleninstanz, startete jeder Durchgang im Dauerbetrieb
    einen eigenen - genau das, was der lange Browser vermeiden soll.
    """
    reset_shared_pools()
    launcher = FakeLauncher()
    first = shared_pool("booking", launcher)
    second = shared_pool("booking", launcher)

    assert first is second
    async with first.page():
        pass
    async with second.page():
        pass
    assert launcher.started == 1
    await close_shared_pools()
    assert launcher.closed == 1


async def test_closing_the_shared_pools_forgets_them() -> None:
    reset_shared_pools()
    launcher = FakeLauncher()
    pool_one = shared_pool("booking", launcher)
    async with pool_one.page():
        pass
    await close_shared_pools()
    pool_two = shared_pool("booking", launcher)

    assert pool_two is not pool_one
    await close_shared_pools()


def test_the_budget_comes_from_the_environment() -> None:
    budget = budget_from_env(
        {
            "FLIGHTOPT_HOTELS_BROWSER_PAGES": "7",
            "FLIGHTOPT_HOTELS_BROWSER_MAX_AGE": "60",
            "FLIGHTOPT_HOTELS_BROWSER_IDLE": "5",
        }
    )

    assert (budget.max_pages, budget.max_age_seconds, budget.idle_seconds) == (7, 60.0, 5.0)


def test_a_broken_environment_value_falls_back_instead_of_failing() -> None:
    """Ein Tippfehler in einer Stack-Variablen darf keinen Container hinlegen."""
    budget = budget_from_env({"FLIGHTOPT_HOTELS_BROWSER_PAGES": "viele"})

    assert budget.max_pages == DEFAULT_BUDGET.max_pages


def test_the_page_budget_keeps_the_measured_drift_small() -> None:
    """Das Seitenbudget steht auf einer Messung, nicht auf einem Gefuehl.

    Gemessen mit `spike/probe_browser_memory.py` ueber sechzig Ladevorgaenge
    der aufgezeichneten Ergebnisseite: rund 55 kB Zuwachs je Seite im
    Dauerlauf. Eine echte Booking-Seite laeuft mehr Javascript, also rechnet
    dieser Test mit dem Zwanzigfachen - und selbst dann bleibt der Zuwachs bis
    zum Neustart unter siebzig Megabyte.
    """
    measured_kib_per_page = 55
    worst_case = measured_kib_per_page * 20

    assert DEFAULT_BUDGET.max_pages * worst_case / 1024 < 70
