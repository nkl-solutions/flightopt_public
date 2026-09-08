"""Jede Quelle bekommt die Rate, die ihr Endpunkt vertraegt.

Ein pauschales per_minute=20 fuer alle war zu langsam fuer offene JSON-APIs
und schnell genug, um bei den WAF-gesicherten Endpunkten aufzufallen. Der Test
haelt beide Enden fest: nichts faellt auf null, und was hinter einem WAF liegt,
bleibt bei hoechstens 15 Abrufen je Minute.
"""

from __future__ import annotations

import asyncio

import pytest

from flightopt.sources.aegean import AegeanSource
from flightopt.sources.britishairways import BritishAirwaysSource
from flightopt.sources.eurowings import EurowingsSource
from flightopt.sources.kiwi import KiwiSource
from flightopt.sources.registry import build_sources
from flightopt.sources.ryanair import RyanairSource
from flightopt.sources.base import SourceError
from flightopt.sources.wizz import WizzSource

# Vor diesen Endpunkten sitzt ein WAF oder Cloudflare. Eine Salve dorthin ist
# teurer als die gesparte Zeit: ein Block gilt fuer Stunden, nicht Sekunden.
WAF_FRONTED = {"aegean", "britishairways", "eurowings"}
WAF_CEILING = 15


def test_every_registered_source_has_a_usable_pace():
    sources = build_sources()
    assert sources, "der Katalog ist leer"

    for source in sources:
        assert source.per_minute > 0, f"{source.name}: per_minute muss positiv sein"
        assert source.concurrency > 0, f"{source.name}: concurrency muss positiv sein"
        assert source.limiter.concurrency == source.concurrency
        assert source.limiter.min_interval == pytest.approx(60.0 / source.per_minute)


def test_waf_fronted_sources_stay_slow():
    for source in build_sources():
        if source.name in WAF_FRONTED:
            assert source.per_minute <= WAF_CEILING, (
                f"{source.name} liegt hinter einem WAF und darf nicht schneller werden"
            )
            assert source.concurrency <= 4


def test_the_open_apis_actually_got_faster():
    """Sonst waere die ganze Uebung wirkungslos geblieben."""
    by_name = {s.name: s for s in build_sources()}

    assert by_name["ryanair"].per_minute == 60
    assert by_name["ryanair"].concurrency == 6
    assert by_name["kiwi"].per_minute == 60
    assert by_name["kiwi"].concurrency == 6
    for name in ("icelandair", "jetblue", "airbaltic", "condor"):
        assert by_name[name].per_minute == 30, name
        assert by_name[name].concurrency == 4, name

    assert by_name["aegean"].concurrency == 2
    assert by_name["britishairways"].concurrency == 2


def test_the_warm_up_sources_kept_their_pace():
    assert AegeanSource().per_minute == 15
    assert BritishAirwaysSource().per_minute == 15
    assert EurowingsSource().per_minute == 15
    assert WizzSource().per_minute == 20


async def test_eurowings_warms_up_only_once_despite_parallel_legs(monkeypatch):
    """Cloudflare zaehlt Challenge-Abrufe. Vier parallele Beine, ein Warmlauf."""
    src = EurowingsSource()
    loads: list[str] = []

    class FakeSession:
        def get(self, url, **kw):
            loads.append(url)

            class R:
                status_code = 200

            return R()

    import flightopt.sources.eurowings as ew

    def slow_session(**kw):
        return FakeSession()

    monkeypatch.setattr("curl_cffi.requests.Session", slow_session)

    async def wait_a_tick():
        # Gibt den anderen Tasks Gelegenheit, in dieselbe Luecke zu laufen.
        await asyncio.sleep(0)

    monkeypatch.setattr(src.limiter, "wait", wait_a_tick)

    sessions = await asyncio.gather(*(src._warm_session() for _ in range(4)))

    assert len(loads) == 1
    assert len({id(s) for s in sessions}) == 1
    assert ew.WARMUP in loads


async def test_wizz_reads_the_api_version_only_once(monkeypatch):
    src = WizzSource()
    reads: list[str] = []

    class R:
        status_code = 200
        text = 'apiUrl:"https://be.wizzair.com/31.2.0/Api"'

    def fake_get(url, **kw):
        reads.append(url)
        return R()

    monkeypatch.setattr("curl_cffi.requests.get", fake_get)

    async def wait_a_tick():
        await asyncio.sleep(0)

    monkeypatch.setattr(src.limiter, "wait", wait_a_tick)

    bases = await asyncio.gather(*(src.base_url() for _ in range(4)))

    assert len(reads) == 1
    assert set(bases) == {"https://be.wizzair.com/31.2.0/Api"}


async def test_ryanair_repins_once_for_a_burst_of_409s(monkeypatch):
    """Sechs gleichzeitige Anfragen, eine veraltete Version, ein Seitenabruf."""
    src = RyanairSource()
    repins: list[str] = []
    attempts: list[str] = []

    async def no_wait():
        return None

    monkeypatch.setattr(src.limiter, "wait", no_wait)

    async def fake_fetch(url, **kw):
        attempts.append(url)
        if src._client_version is None:
            raise SourceError("ryanair: HTTP 409 Availability declined")
        return {"ok": True}

    async def fake_repin():
        repins.append("read")
        await asyncio.sleep(0)
        src._client_version = "9.9.9"

    monkeypatch.setattr(src, "fetch_json", fake_fetch)
    monkeypatch.setattr(src, "_read_client_version", fake_repin)

    out = await asyncio.gather(*(src._get(f"https://x/{i}") for i in range(6)))

    assert out == [{"ok": True}] * 6
    assert len(repins) == 1, "jeder 409 hat die Seite erneut gelesen"


def test_kiwi_keeps_its_pace_when_a_carrier_filter_is_applied():
    """build_sources baut Kiwi bei gesetztem Filter neu; die Rate muss mit."""
    filtered = build_sources({"FR"})
    kiwi = next(s for s in filtered if isinstance(s, KiwiSource))
    assert kiwi.per_minute == 60
    assert kiwi.concurrency == 6
