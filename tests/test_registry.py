"""Ein Katalog fuer alle Quellen, an einer Stelle gebaut.

Der Test haelt fest, was sonst still auseinanderlaeuft: kein Adapter doppelt,
kein Carrier von zwei Adaptern beansprucht, SerpApi nur mit Schluessel und
Budget, und weder Runner noch API bauen sich einen eigenen Katalog.
"""

from __future__ import annotations

from pathlib import Path

from flightopt.sources.kiwi import KiwiSource
from flightopt.sources.registry import build_sources
from flightopt.sources.serpapi_google import SerpApiGoogleFlights
from flightopt.storage import db


def test_catalogue_has_no_duplicate_adapters():
    names = [s.name for s in build_sources(None, env={})]

    assert len(names) == len(set(names))


def test_every_source_can_price_something():
    for source in build_sources(None, env={}):
        assert source.supports_calendar or source.supports_search


def test_each_carrier_is_claimed_by_exactly_one_adapter():
    seen: dict[str, str] = {}
    for source in build_sources(None, env={}):
        if isinstance(source, KiwiSource):
            continue  # Kiwi fuehrt einen Filter, keine eigene Marke.
        for code in source.carriers:
            assert code not in seen, f"{code}: {seen.get(code)} und {source.name}"
            seen[code] = source.name


def test_carrier_filter_keeps_only_matching_airlines():
    names = {s.name for s in build_sources({"FR"}, env={})}

    assert "ryanair" in names
    assert "wizz" not in names


def test_carrier_filter_reaches_kiwi_instead_of_excluding_it():
    # Kiwi hat keinen eigenen Carrier. Ein Filter wird deshalb an Kiwi
    # durchgereicht, statt Kiwi aus dem Katalog zu werfen.
    kiwi = [s for s in build_sources({"XQ"}, env={}) if isinstance(s, KiwiSource)]

    assert len(kiwi) == 1
    assert kiwi[0].carrier_filter == ["XQ"]


def test_one_adapter_can_serve_several_brands():
    names = {s.name for s in build_sources({"DI"}, env={})}

    assert "condor" in names


def test_serpapi_is_absent_without_a_key(tmp_path):
    conn = db.connect(tmp_path / "cat.db")

    assert not any(
        isinstance(s, SerpApiGoogleFlights) for s in build_sources(None, conn=conn, env={})
    )


def test_serpapi_joins_when_the_key_is_set(tmp_path):
    conn = db.connect(tmp_path / "cat.db")
    sources = build_sources(None, conn=conn, env={"SERPAPI_KEY": "test-key"})

    assert any(isinstance(s, SerpApiGoogleFlights) for s in sources)


def test_an_empty_key_does_not_count_as_a_key(tmp_path):
    conn = db.connect(tmp_path / "cat.db")
    sources = build_sources(None, conn=conn, env={"SERPAPI_KEY": "   "})

    assert not any(isinstance(s, SerpApiGoogleFlights) for s in sources)


def test_without_a_database_the_paid_source_stays_out():
    # Der Routen-Endpunkt hat keine Job-Verbindung. Ohne Budget darf SerpApi
    # nicht mitlaufen - und der Aufruf darf auch nicht scheitern.
    sources = build_sources(None, env={"SERPAPI_KEY": "test-key"})

    assert not any(isinstance(s, SerpApiGoogleFlights) for s in sources)


def test_the_runner_has_no_catalogue_of_its_own():
    import flightopt.jobs.runner as runner_module

    text = Path(runner_module.__file__).read_text(encoding="utf-8")

    assert "catalogue: list = [" not in text
    assert "build_sources" in text


def test_the_api_does_not_hardwire_ryanair_for_routes():
    import flightopt.api.main as main_module

    text = Path(main_module.__file__).read_text(encoding="utf-8")

    assert "RyanairSource()" not in text
    assert "build_sources" in text


async def test_routes_endpoint_unions_the_sources_and_survives_one_failure(monkeypatch):
    """Eine gescheiterte Quelle darf die Liste der anderen nicht leeren."""
    import flightopt.api.main as main

    class WithRoutes:
        async def load_routes(self, code):
            assert code == "BER"
            return {"ATH", "PMI"}

    class Broken:
        async def load_routes(self, code):
            raise RuntimeError("kaputt")

    class NoRoutes:
        pass

    monkeypatch.setattr(main, "build_sources", lambda *a, **kw: [WithRoutes(), Broken(), NoRoutes()])

    assert await main.routes("ber") == {"origin": "BER", "destinations": ["ATH", "PMI"]}
