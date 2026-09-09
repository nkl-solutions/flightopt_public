"""Waehrungsumrechnung.

Money bleibt einwaehrig; genau deshalb braucht es eine Stelle, an der eine
Fremdwaehrung sauber in die Suchwaehrung uebergeht. Wird hier falsch gerundet
oder ein Kurs verdreht, sind alle Summen still falsch.
"""

from __future__ import annotations

import copy
import json
import pickle
import sqlite3
from datetime import date, datetime
from pathlib import Path

import pytest

from flightopt.domain import fx
from flightopt.domain.models import Money

FIXTURES = Path(__file__).parent / "fixtures"


def ecb_xml() -> str:
    return (FIXTURES / "ecb_eurofxref_daily.xml").read_text(encoding="utf-8")


def rates() -> fx.Rates:
    return fx.Rates(
        base="EUR",
        rates={"JPY": 165.0, "USD": 1.10, "KRW": 1500.0},
        fetched_at=datetime(2026, 9, 5, 16, 0, 0),
    )


# --- EZB-XML -----------------------------------------------------------------


def test_parse_ecb_xml_reads_rates_and_the_reference_day():
    parsed = fx.parse_ecb_xml(ecb_xml())

    assert parsed.base == "EUR"
    assert len(parsed.rates) == 10
    assert parsed.rates["JPY"] == 164.85
    assert parsed.rates["TRY"] == 45.120
    assert parsed.fetched_at.date() == date(2026, 9, 5)


def test_parse_ecb_xml_refuses_a_document_type_declaration():
    evil = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><gesmes:Envelope/>'

    with pytest.raises(ValueError, match="DOCTYPE"):
        fx.parse_ecb_xml(evil)


def test_parse_ecb_xml_rejects_a_document_without_rates():
    with pytest.raises(ValueError, match="keine Kurse"):
        fx.parse_ecb_xml("<gesmes:Envelope xmlns:gesmes='x'><Cube/></gesmes:Envelope>")


# --- Umrechnung --------------------------------------------------------------


def test_same_currency_is_returned_untouched():
    money = Money(4999, "EUR")

    assert fx.convert(money, "EUR", rates()) is money


def test_conversion_rounds_to_minor_units():
    # 48.500 JPY / 165 = 293,9393... EUR
    assert fx.convert(Money(4850000, "JPY"), "EUR", rates()) == Money(29394, "EUR")
    # 110,00 USD / 1,10 = 100,00 EUR
    assert fx.convert(Money(11000, "USD"), "EUR", rates()) == Money(10000, "EUR")


def test_conversion_works_between_two_foreign_currencies():
    # 1.500 KRW = 1 EUR = 165 JPY
    assert fx.convert(Money(150000, "KRW"), "JPY", rates()) == Money(16500, "JPY")


def test_unknown_currency_is_an_error_not_a_guess():
    with pytest.raises(fx.UnknownCurrency, match="XYZ"):
        fx.convert(Money(1000, "XYZ"), "EUR", rates())


# --- Fallback und Abruf ------------------------------------------------------


def test_fallback_snapshot_covers_the_currencies_we_price_in():
    fallback = fx.load_fallback()

    assert fallback.base == "EUR"
    assert fallback.rates["JPY"] == 165.0
    assert fallback.rates["KRW"] == 1500.0
    assert {"USD", "GBP", "CHF", "TRY", "THB", "SGD", "PLN", "CZK", "SEK",
            "NOK", "DKK", "HUF", "ISK", "AED", "CAD", "AUD", "MXN",
            "BRL"} <= set(fallback.rates)


@pytest.mark.asyncio
async def test_fetch_ecb_rates_parses_what_the_client_returns():
    class FakeResponse:
        status_code = 200
        text = ecb_xml()

    class FakeClient:
        def __init__(self) -> None:
            self.seen: list[str] = []

        def get(self, url, **kw):
            self.seen.append(url)
            return FakeResponse()

    client = FakeClient()
    parsed = await fx.fetch_ecb_rates(client)

    assert client.seen == [fx.ECB_URL]
    assert parsed.rates["USD"] == 1.1012


@pytest.mark.asyncio
async def test_fetch_ecb_rates_reports_a_bad_status():
    class FakeResponse:
        status_code = 503
        text = ""

    class FakeClient:
        def get(self, url, **kw):
            return FakeResponse()

    with pytest.raises(fx.FxUnavailable, match="503"):
        await fx.fetch_ecb_rates(FakeClient())


# --- Speicher ----------------------------------------------------------------

from datetime import timedelta  # noqa: E402

from flightopt.storage import db, fx_store  # noqa: E402


def test_rates_survive_a_round_trip(tmp_path):
    conn = db.connect(tmp_path / "fx.db")
    now = datetime(2026, 9, 5, 10, 0, 0)

    fx_store.save_rates(conn, rates(), now=now)
    loaded = fx_store.load_rates(conn, now=now)

    assert loaded is not None
    assert loaded.base == "EUR"
    assert loaded.rates["JPY"] == 165.0
    assert loaded.rates["KRW"] == 1500.0
    assert loaded.fetched_at == now


def test_an_empty_table_has_no_rates(tmp_path):
    conn = db.connect(tmp_path / "fx.db")

    assert fx_store.load_rates(conn) is None


def test_rates_older_than_the_ttl_are_ignored(tmp_path):
    conn = db.connect(tmp_path / "fx.db")
    now = datetime(2026, 9, 5, 10, 0, 0)
    fx_store.save_rates(conn, rates(), now=now)

    assert fx_store.load_rates(conn, now=now + timedelta(hours=23)) is not None
    assert fx_store.load_rates(conn, now=now + timedelta(hours=25)) is None


@pytest.mark.asyncio
async def test_current_rates_prefers_the_cache_over_the_network(tmp_path):
    conn = db.connect(tmp_path / "fx.db")
    now = datetime(2026, 9, 5, 10, 0, 0)
    fx_store.save_rates(conn, rates(), now=now)

    class ExplodingClient:
        def get(self, url, **kw):
            raise AssertionError("darf nicht aufgerufen werden")

    loaded = await fx_store.current_rates(conn, now=now, client=ExplodingClient())

    assert loaded.rates["JPY"] == 165.0


@pytest.mark.asyncio
async def test_current_rates_fetches_and_stores_when_the_cache_is_cold(tmp_path):
    conn = db.connect(tmp_path / "fx.db")
    now = datetime(2026, 9, 5, 10, 0, 0)

    class FakeResponse:
        status_code = 200
        text = ecb_xml()

    class FakeClient:
        def get(self, url, **kw):
            return FakeResponse()

    loaded = await fx_store.current_rates(conn, now=now, client=FakeClient())

    assert loaded.rates["USD"] == 1.1012
    assert fx_store.load_rates(conn, now=now).rates["USD"] == 1.1012


@pytest.mark.asyncio
async def test_current_rates_falls_back_to_the_snapshot(tmp_path):
    conn = db.connect(tmp_path / "fx.db")

    class DeadClient:
        def get(self, url, **kw):
            raise OSError("kein Netz")

    loaded = await fx_store.current_rates(conn, client=DeadClient())

    assert loaded.rates["JPY"] == 165.0
    # Ein Fallback wird nicht gespeichert, sonst blockiert er den naechsten Abruf.
    assert fx_store.load_rates(conn) is None


# --- Grid und Verify ---------------------------------------------------------

from flightopt.domain.models import (  # noqa: E402
    Cabin, LegSpec, Offer, Pax, SearchSpec,
)
from flightopt.search import grid as grid_mod  # noqa: E402
from flightopt.search.dp import Combination  # noqa: E402
from flightopt.search.verify import verify  # noqa: E402

DAY = date(2027, 3, 10)


def one_leg_spec() -> SearchSpec:
    return SearchSpec(
        legs=(LegSpec("BER", "NRT"),),
        stays=(),
        window_start=DAY,
        window_end=DAY,
    )


class JapaneseCalendar:
    name = "jpcal"
    carrier = ""
    carriers = ()
    supports_calendar = True
    supports_search = False
    indicative = True

    def supports_route(self, origin, destination):
        return True

    async def calendar_range(self, origin, destination, start, end, *, currency="EUR"):
        return {DAY: Money(4850000, "JPY")}


class JapaneseAirline:
    name = "jpair"
    carrier = "JL"
    carriers = ("JL",)
    supports_calendar = False
    supports_search = True
    indicative = False

    def supports_route(self, origin, destination):
        return True

    async def search_leg(self, origin, destination, day, *, pax=Pax(),
                         cabin=Cabin.ECONOMY, currency="EUR"):
        return [
            Offer(
                source=self.name,
                origin=origin,
                destination=destination,
                travel_date=day,
                price=Money(4850000, "JPY"),
                fetched_at=datetime(2026, 9, 5),
            )
        ]


def test_offer_has_no_native_price_by_default():
    offer = Offer(source="x", origin="BER", destination="NRT",
                  travel_date=DAY, price=Money(1000, "EUR"))

    assert offer.price_native is None


def test_convert_prices_splits_converted_and_native():
    converted, native = grid_mod.convert_prices(
        {DAY: Money(4850000, "JPY"), DAY + timedelta(days=1): Money(9900, "EUR")},
        "EUR",
        rates(),
    )

    assert converted[DAY] == Money(29394, "EUR")
    assert converted[DAY + timedelta(days=1)] == Money(9900, "EUR")
    assert native == {DAY: Money(4850000, "JPY")}


def test_convert_prices_drops_what_it_cannot_convert():
    converted, native = grid_mod.convert_prices({DAY: Money(1000, "JPY")}, "EUR", None)

    assert converted == {}
    assert native == {}


def test_convert_offer_keeps_the_original_price():
    offer = Offer(source="x", origin="BER", destination="NRT",
                  travel_date=DAY, price=Money(4850000, "JPY"))
    out = grid_mod.convert_offer(offer, "EUR", rates())

    assert out.price == Money(29394, "EUR")
    assert out.price_native == Money(4850000, "JPY")
    assert out.origin == "BER"


@pytest.mark.asyncio
async def test_build_grid_converts_a_foreign_calendar():
    spec = one_leg_spec()
    built, report = await grid_mod.build_grid(spec, [JapaneseCalendar()], rates=rates())

    assert built[0][DAY] == Money(29394, "EUR")
    assert report.native[(0, DAY)] == Money(4850000, "JPY")
    assert report.filled[0] == 1


@pytest.mark.asyncio
async def test_build_grid_without_rates_skips_a_foreign_calendar():
    spec = one_leg_spec()
    built, report = await grid_mod.build_grid(spec, [JapaneseCalendar()], rates=None)

    assert built[0] == {}
    assert report.native == {}


@pytest.mark.asyncio
async def test_a_calendar_dropped_for_want_of_a_rate_says_so_in_the_report():
    """Ein fehlender Kurs sah aus wie ein Tag ohne Angebot.

    Die Tage verschwanden nur ins Log. Ist gar kein Kurssatz geladen, verdampft
    damit jeder Fremdwaehrungs-Kalender vollstaendig und lautlos: die Suche
    laeuft mit einem Bruchteil des Gitters weiter und meldet null Fehler.
    """
    spec = one_leg_spec()
    _, report = await grid_mod.build_grid(spec, [JapaneseCalendar()], rates=None)

    assert any("Umrechnungskurs" in note for note in report.errors)
    assert any("jpcal" in note for note in report.errors)


@pytest.mark.asyncio
async def test_a_calendar_that_answers_beside_the_window_says_so_too():
    """Preise ausserhalb des gefragten Fensters sind keine Antwort auf die Frage.

    Eine Quelle, die `lo` und `hi` ignoriert, sah im Bericht genauso aus wie
    eine, die korrekt nichts gefunden hat: ein Abruf, kein Fehler, kein
    Beitrag.
    """

    class Elsewhere(JapaneseCalendar):
        name = "danebenkalender"

        async def calendar_range(self, origin, destination, start, end, *, currency="EUR"):
            return {end + timedelta(days=30): Money(9900, "EUR")}

    spec = one_leg_spec()
    built, report = await grid_mod.build_grid(spec, [Elsewhere()], rates=rates())

    assert built[0] == {}
    assert any("Fenster" in note for note in report.errors)


@pytest.mark.asyncio
async def test_verify_converts_a_foreign_offer():
    spec = one_leg_spec()
    combo = Combination(dates=(DAY,), total=Money(30000, "EUR"))
    verified, _ = await verify(spec, [combo], [JapaneseAirline()], rates=rates())

    assert verified[0].offers[0].price == Money(29394, "EUR")
    assert verified[0].offers[0].price_native == Money(4850000, "JPY")
    assert verified[0].total == Money(29394, "EUR")


# --- Robustheit gegen kaputte Antworten --------------------------------------

TWO_DAY_XML = """<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
  <Cube>
    <Cube time="2026-09-05">
      <Cube currency="USD" rate="1.1012"/>
      <Cube currency="JPY" rate="164.85"/>
    </Cube>
    <Cube time="2026-09-04">
      <Cube currency="USD" rate="1.2000"/>
      <Cube currency="JPY" rate="150.00"/>
      <Cube currency="HRK" rate="7.5345"/>
    </Cube>
  </Cube>
</gesmes:Envelope>"""


def test_parse_ecb_xml_takes_the_first_day_of_a_multi_day_file():
    parsed = fx.parse_ecb_xml(TWO_DAY_XML)

    assert parsed.fetched_at.date() == date(2026, 9, 5)
    assert parsed.rates["USD"] == 1.1012
    # Der zweite Tag darf nicht durchschlagen, sonst mischt der Parser Kurse.
    assert "HRK" not in parsed.rates


class _FixedClient:
    """Answers every request with the same status and body."""

    def __init__(self, text: str, status: int = 200) -> None:
        self.text = text
        self.status = status

    def get(self, url, **kw):
        client = self

        class Response:
            status_code = client.status
            text = client.text

        return Response()


@pytest.mark.asyncio
async def test_fetch_ecb_rates_survives_an_error_page_instead_of_xml():
    # Eine Fehlerseite mit HTTP 200 ist der Normalfall bei Proxys und Captive
    # Portals; ET wirft dann ParseError, keinen ValueError.
    with pytest.raises(fx.FxUnavailable):
        await fx.fetch_ecb_rates(_FixedClient("<html><body>oops</body></html>"))


@pytest.mark.asyncio
async def test_fetch_ecb_rates_survives_an_empty_body():
    with pytest.raises(fx.FxUnavailable):
        await fx.fetch_ecb_rates(_FixedClient(""))


@pytest.mark.asyncio
async def test_current_rates_survives_an_unreadable_stored_row(tmp_path):
    """Ein kaputtes fetched_at darf keine Suche abbrechen, nur die Stufe kosten."""
    conn = db.connect(tmp_path / "fx.db")
    conn.execute(
        "INSERT INTO fx_rate(currency, rate, fetched_at) VALUES(?,?,?)",
        ("JPY", 999.0, "gestern"),
    )

    loaded = await fx_store.current_rates(conn, client=_FixedClient("<html>nope</html>"))

    # Kein Absturz, und der Snapshot statt des unlesbaren Satzes.
    assert loaded.rates["JPY"] == 165.0


@pytest.mark.asyncio
async def test_current_rates_falls_back_when_the_ecb_sends_html(tmp_path):
    conn = db.connect(tmp_path / "fx.db")

    loaded = await fx_store.current_rates(conn, client=_FixedClient("<html>nope</html>"))

    assert loaded.rates["JPY"] == 165.0
    assert fx_store.load_rates(conn) is None


# --- Speicher: alte Waehrungen verschwinden ----------------------------------


def test_saving_rates_forgets_a_currency_the_ecb_dropped(tmp_path):
    conn = db.connect(tmp_path / "fx.db")
    now = datetime(2026, 9, 5, 10, 0, 0)
    fx_store.save_rates(
        conn, fx.Rates(rates={"USD": 1.10, "HRK": 7.5345}, fetched_at=now), now=now
    )

    fx_store.save_rates(conn, fx.Rates(rates={"USD": 1.20}, fetched_at=now), now=now)
    loaded = fx_store.load_rates(conn, now=now)

    assert loaded is not None
    assert loaded.rates == {"USD": 1.20}


def test_the_ttl_boundary_of_exactly_24_hours_still_counts(tmp_path):
    conn = db.connect(tmp_path / "fx.db")
    now = datetime(2026, 9, 5, 10, 0, 0)
    fx_store.save_rates(conn, rates(), now=now)

    assert fx_store.load_rates(conn, now=now + timedelta(hours=24)) is not None


# --- Rates ist unveraenderlich -----------------------------------------------


def test_the_rate_table_cannot_be_edited_through_the_dataclass():
    table = rates()

    with pytest.raises(TypeError):
        table.rates["JPY"] = 1.0


def test_rates_can_be_used_as_a_dictionary_key():
    # frozen=True verspricht Hashbarkeit; ohne eigenen __hash__ wirft das.
    assert {rates(): "ok"}[rates()] == "ok"


# --- Bisher ungetestete Pfade ------------------------------------------------


class _MemoryCache:
    def __init__(self, seed: dict | None = None) -> None:
        self.store = dict(seed or {})
        self.written: dict = {}

    async def get(self, key, *, max_age=None, now=None):
        return self.store.get(key)

    async def put(self, key, payload, ttl, *, source="", now=None):
        self.written[key] = payload


@pytest.mark.asyncio
async def test_a_legacy_cache_entry_without_a_currency_counts_as_search_currency():
    from flightopt.storage.cache import cache_key

    spec = one_leg_spec()
    key = cache_key(
        "jpcal", "calendar", "BER", "NRT", DAY, until=DAY,
        pax=spec.pax.total, cabin=spec.cabin.value, currency=spec.currency,
    )
    # Alte Eintraege hielten nur die Minor Units, ohne Waehrung daneben.
    cache = _MemoryCache({key: {DAY.isoformat(): 12345}})

    built, report = await grid_mod.build_grid(
        spec, [JapaneseCalendar()], cache=cache, rates=rates()
    )

    assert built[0][DAY] == Money(12345, "EUR")
    assert report.cache_hits == 1
    assert report.calls == 0


@pytest.mark.asyncio
async def test_verify_without_rates_notes_the_foreign_offer_and_carries_on():
    spec = one_leg_spec()
    combo = Combination(dates=(DAY,), total=Money(30000, "EUR"))

    verified, report = await verify(spec, [combo], [JapaneseAirline()], rates=None)

    # Das Angebot faellt weg, die Suche laeuft weiter: die Kombination bleibt
    # unbestaetigt in der Liste und der Grund steht im Report.
    assert len(verified) == 1
    assert verified[0].complete is False
    assert report.partial == 1
    assert any("JPY" in message for message in report.errors)


# --- ... und trotzdem transportierbar ----------------------------------------


def test_the_rate_table_can_be_written_as_json():
    table = rates()

    assert json.loads(json.dumps(dict(table.rates)))["JPY"] == 165.0


def test_rates_survive_a_deep_copy_and_a_pickle():
    """Ein Job reicht die Kurse weiter; eine Mappingproxy allein tut das nicht."""
    table = rates()

    copied = copy.deepcopy(table)
    unpickled = pickle.loads(pickle.dumps(table))

    for clone in (copied, unpickled):
        assert clone == table
        assert hash(clone) == hash(table)
        assert clone.rate("JPY") == 165.0
        assert dict(clone.rates) == dict(table.rates)
        with pytest.raises(TypeError):
            clone.rates["JPY"] = 1.0


def test_a_copy_does_not_share_the_table_with_its_original():
    table = fx.Rates(base="EUR", rates={"JPY": 165.0}, fetched_at=datetime(2026, 9, 5))
    copied = copy.deepcopy(table)

    assert dict(copied.rates) == {"JPY": 165.0}
    assert copied.rates is not table.rates


# --- Leere Kurstabellen ------------------------------------------------------


def test_saving_nothing_leaves_the_stored_rates_alone(tmp_path):
    """Ein leerer Abruf darf die letzte gute Tabelle nicht loeschen."""
    conn = db.connect(tmp_path / "fx.db")
    fx_store.save_rates(conn, rates(), now=datetime(2026, 9, 5, 16, 0, 0))

    fx_store.save_rates(
        conn, fx.Rates(base="EUR", rates={}, fetched_at=datetime(2026, 9, 6)),
        now=datetime(2026, 9, 6, 16, 0, 0),
    )
    stored = fx_store.load_rates(conn, now=datetime(2026, 9, 6, 10, 0, 0))

    assert stored is not None
    assert stored.rate("JPY") == 165.0


class FailingCommit:
    """Reicht alles durch, nur COMMIT nicht."""

    def __init__(self, conn) -> None:
        self.conn = conn
        self.rolled_back = False

    def execute(self, sql, *args):
        if sql == "COMMIT":
            raise sqlite3.OperationalError("disk I/O error")
        if sql == "ROLLBACK":
            self.rolled_back = True
        return self.conn.execute(sql, *args)

    def executemany(self, sql, rows):
        return self.conn.executemany(sql, rows)


def test_a_failed_commit_does_not_leave_the_transaction_open(tmp_path):
    """COMMIT stand ausserhalb des try.

    Scheitert es, blieb die Transaktion offen: die Verbindung hielt bis zu
    ihrem Ende die Schreibsperre, und jeder weitere Schreibvorgang desselben
    Laufs lief in den busy_timeout. Der Aufrufer sah nur "Kurse nicht
    gespeichert".
    """
    conn = db.connect(tmp_path / "commit.db")
    flaky = FailingCommit(conn)

    with pytest.raises(sqlite3.OperationalError):
        fx_store.save_rates(flaky, rates(), now=datetime(2026, 9, 5, 10, 0, 0))

    assert flaky.rolled_back is True
    assert conn.in_transaction is False
    # Die Verbindung ist danach wieder benutzbar.
    conn.execute("INSERT INTO fx_rate(currency, rate, fetched_at) VALUES('USD',1.1,?)",
                 (db.now(),))
