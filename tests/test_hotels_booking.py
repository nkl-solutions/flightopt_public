"""Der Booking-Adapter gegen aufgezeichnete Ergebnisseiten.

Zwei echte Aufzeichnungen einer Suche nach Athen am 10.11.2026, beide mit
`scripts/record_hotel_fixtures.py` aus der Originalseite geschnitten:

* `booking_apollo_athen.html` traegt den Apollo-Cache **und** die ersten sechs
  Ergebniskarten. Das ist der Normalfall.
* `booking_searchresults_athen.html` ist die aeltere Aufzeichnung, in der nur
  die Karten stehen. Sie bleibt liegen, weil sie die Rueckfallebene beweist.

Diese Tests starten keinen Browser und fassen kein Netz an.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

import pytest

from flightopt.domain.models import Money
from flightopt.hotels.models import HotelQuery
from flightopt.hotels.sources.base import LayoutBroken, SourceBlocked, SourceError
from flightopt.hotels.sources.booking import (
    APARTMENT,
    CHALLENGE_STATUS,
    FORBIDDEN_PATHS,
    HOTEL,
    MAX_RESULTS,
    ORDERS,
    PAGE_SIZE,
    READY_SELECTOR,
    WAF_COOKIE,
    BookingSource,
    Destination,
    PriceRange,
    _gate,
    apollo_store,
    build_filters,
    build_search_url,
    parse_apollo,
    page_offsets,
    pagination_note,
    parse_autocomplete,
    parse_result_html,
    parse_search_page,
    property_type_options,
    review_bucket,
    search_node,
)
from flightopt.sources.base import RateLimiter

FIXTURES = Path(__file__).parent / "fixtures"
ATHENS = Destination("13914", "city", "Athen, Attika, Griechenland")


def recorded() -> str:
    return (FIXTURES / "booking_searchresults_athen.html").read_text(encoding="utf-8")


def recorded_apollo() -> str:
    return (FIXTURES / "booking_apollo_athen.html").read_text(encoding="utf-8")


def query(**kwargs) -> HotelQuery:
    return HotelQuery(destination="Athen", arrival=date(2026, 11, 10), **kwargs)


def test_the_recorded_page_becomes_offers_with_name_price_stars_and_url():
    batch = parse_result_html(recorded(), query(adults=2))

    assert len(batch.offers) == 6
    assert batch.skipped == []
    first = batch.offers[0]
    assert first.name == "Sparta Team Hotel"
    # Die Objekt-URL ist das einzige Stueck, das ueber Suchen hinweg gleich
    # bleibt; alles ab dem Fragezeichen traegt Datum und Belegung.
    assert first.property_key == "booking:gr/sparta"
    assert first.price_total == Money(2600, "EUR")
    assert first.stars == 1
    assert first.url.startswith("https://www.booking.com/hotel/gr/sparta.de.html")
    assert first.party_size == 2
    assert first.indicative is True
    # Sterne kommen aus dem Vorlesetext "N von 5", sonst aus der Symbolzahl.
    assert [offer.stars for offer in batch.offers] == [1, None, 2, 3, 3, 3]
    assert [offer.price_total.minor for offer in batch.offers] == [
        2600, 3100, 3400, 3500, 3800, 3900
    ]


def test_no_review_text_is_carried_out_of_the_page():
    """Bewertungen sind personenbezogen und werden nicht gespeichert."""
    batch = parse_result_html(recorded(), query())

    for offer in batch.offers:
        assert offer.review_rating is None
        assert offer.review_count is None


def test_a_star_filter_drops_the_cards_that_do_not_match_and_says_so():
    batch = parse_result_html(recorded(), query(stars=(3,)))

    assert {offer.stars for offer in batch.offers} == {3}
    assert len(batch.skipped) == 3


def test_the_search_url_carries_the_window_the_occupancy_and_the_filters():
    url = build_search_url(
        query(nights=2, adults=2, children=(4, 9), rooms=2, stars=(4, 5),
              min_review_score=8.0),
        ATHENS,
    )
    params = parse_qsl(urlsplit(url).query)

    assert urlsplit(url).path == "/searchresults.de.html"
    assert ("checkin", "2026-11-10") in params
    assert ("checkout", "2026-11-12") in params
    assert ("dest_id", "13914") in params and ("dest_type", "city") in params
    assert ("group_adults", "2") in params and ("no_rooms", "2") in params
    assert ("group_children", "2") in params
    # Je Kind ein eigenes `age`, in der Reihenfolge der Alter.
    assert [value for key, value in params if key == "age"] == ["4", "9"]
    assert ("order", "price") in params
    assert ("selected_currency", "EUR") in params
    assert ("nflt", "class=4;class=5;ht_id=204;review_score=80") in params


def test_without_filters_only_the_hotel_category_stays():
    # Schlafsaal, Tageszimmer und Campingplatz gehoeren nicht in dieselbe
    # Preisverteilung wie ein Hotelzimmer.
    assert build_filters(query()) == "ht_id=204"


def test_every_filter_booking_knows_lands_in_one_nflt_expression():
    text = build_filters(
        query(stars=(4, 5), min_review_score=8.0),
        property_types=(HOTEL, APARTMENT),
        price=PriceRange(50, 200, "EUR"),
        free_cancellation=True,
        available_only=True,
    )

    assert text == (
        "class=4;class=5;ht_id=204;ht_id=201;review_score=80;"
        "price=EUR-50-200-1;fc=2;oos=1"
    )


def test_an_open_price_range_says_min_and_max_instead_of_a_number():
    assert PriceRange(maximum=120).as_filter() == "price=EUR-min-120-1"
    assert PriceRange(minimum=80, currency="chf").as_filter() == "price=CHF-80-max-1"
    with pytest.raises(ValueError):
        PriceRange()
    with pytest.raises(ValueError):
        PriceRange(200, 100)


def test_the_review_filter_snaps_to_a_step_booking_actually_has():
    # Booking kennt 60, 70, 80, 90 - nichts dazwischen.
    assert review_bucket(8.5) == 80
    assert review_bucket(9.0) == 90
    assert review_bucket(5.0) is None
    assert review_bucket(None) is None


def test_the_sort_order_is_one_of_the_documented_values():
    url = build_search_url(query(), ATHENS, order="review_score_and_price")

    assert ("order", "review_score_and_price") in parse_qsl(urlsplit(url).query)
    assert set(ORDERS) == {
        "popularity", "price", "class", "review_score_and_price", "distance_from_search"
    }
    with pytest.raises(ValueError, match="Sortierung"):
        build_search_url(query(), ATHENS, order="cheapest")


def test_pagination_counts_in_pages_of_25_and_stops_at_1000():
    assert page_offsets(60) == [0, 25, 50]
    assert page_offsets(60, limit=2) == [0, 25]
    assert len(page_offsets(5000)) == MAX_RESULTS // PAGE_SIZE
    assert ("offset", "50") in parse_qsl(urlsplit(
        build_search_url(query(), ATHENS, offset=50)
    ).query)
    with pytest.raises(ValueError, match="Vielfaches"):
        build_search_url(query(), ATHENS, offset=30)
    with pytest.raises(ValueError, match="ausserhalb"):
        build_search_url(query(), ATHENS, offset=1000)


def test_more_hits_than_booking_hands_out_are_reported_not_cut():
    node = {"results": [], "pagination": {"nbResultsTotal": 4200}}

    batch = parse_apollo(node, query())

    assert any("4200" in note and "1000" in note for note in batch.skipped)
    assert pagination_note(900) == ""


def test_the_url_never_touches_the_paths_robots_forbids():
    url = build_search_url(query(), ATHENS)

    for path in FORBIDDEN_PATHS:
        assert path not in url


async def test_the_browser_drops_images_fonts_and_the_forbidden_paths():
    class Route:
        def __init__(self, url: str, kind: str) -> None:
            self.request = type("R", (), {"url": url, "resource_type": kind})()
            self.action = None

        async def abort(self) -> None:
            self.action = "abort"

        async def continue_(self) -> None:
            self.action = "continue"

    cases = [
        Route("https://www.booking.com/searchresults.de.html?ss=Athen", "document"),
        Route("https://cf.bstatic.com/x/hotel.jpg", "image"),
        Route("https://cf.bstatic.com/x/font.woff2", "font"),
        Route("https://www.booking.com/alt_avail?hotel=1", "xhr"),
        Route("https://www.booking.com/monthly_minrates?hotel=1", "xhr"),
    ]
    for route in cases:
        await _gate(route)

    assert [route.action for route in cases] == [
        "continue", "abort", "abort", "abort", "abort"
    ]


def test_the_first_usable_destination_wins_and_nothing_is_guessed():
    payload = {
        "results": [
            {"dest_id": 900, "dest_type": "airport", "label": "Athen Flughafen"},
            {"dest_id": 13914, "dest_type": "city", "label": "Athen, Griechenland"},
        ]
    }

    assert parse_autocomplete(payload) == Destination("13914", "city", "Athen, Griechenland")
    assert parse_autocomplete({"results": []}) is None
    assert parse_autocomplete("kaputt") is None


async def test_a_missing_playwright_is_a_clear_message_and_not_an_import_crash(monkeypatch):
    # Der Adapter darf ohne Playwright importierbar bleiben; erst der Aufruf
    # meldet, was fehlt.
    monkeypatch.setitem(sys.modules, "playwright.async_api", None)
    source = BookingSource()

    with pytest.raises(SourceError, match="uv sync --group hotels"):
        await source.render("https://www.booking.com/searchresults.de.html")


def test_the_recorded_page_carries_the_apollo_cache_with_the_search_node():
    """Der Suchknoten haengt unter `ROOT_QUERY.searchQueries`, zweiter Schluessel."""
    store = apollo_store(recorded_apollo())
    node = search_node(store)

    assert set(store["ROOT_QUERY"]["searchQueries"]) > {"__typename"}
    assert node is not None
    assert node["pagination"]["nbResultsTotal"] == 642
    assert len(node["results"]) == 6


def test_the_apollo_cache_gives_id_name_stars_reviews_price_place_and_url():
    batch = parse_apollo(search_node(apollo_store(recorded_apollo())), query(adults=2))

    assert batch.parser == "apollo"
    assert batch.total_results == 642
    assert batch.empty is False
    assert len(batch.offers) == 6
    first = batch.offers[0]
    assert first.property_key == "booking:gr/sparta"
    assert first.name == "Sparta Team Hotel"
    assert first.stars == 1
    assert first.review_rating == 5.4 and first.review_count == 2086
    # 26,3001 EUR aus `amountUnformatted`, nicht aus dem Anzeigetext "26,30".
    assert first.price_total == Money(2630, "EUR")
    assert first.price_per_night == Money(2630, "EUR")
    assert (round(first.lat, 4), round(first.lon, 4)) == (37.982, 23.7246)
    assert first.city == "Athens" and first.country_code == "GR"
    assert first.url == "https://www.booking.com/hotel/gr/sparta.de.html"
    assert first.indicative is True


def test_the_currency_comes_from_the_field_and_never_from_a_symbol():
    node = {
        "results": [
            {
                "displayName": {"text": "Hotel Zuerich"},
                "basicPropertyData": {
                    "pageName": "zuerich",
                    "location": {"countryCode": "ch"},
                },
                "priceDisplayInfoIrene": {
                    "displayPrice": {
                        "amountPerStay": {
                            "amount": "$ 1'234.50",
                            "amountUnformatted": 1234.5,
                            "currency": "CHF",
                        }
                    }
                },
            }
        ]
    }

    batch = parse_apollo(node, query())

    assert batch.offers[0].price_total == Money(123450, "CHF")


def test_the_apollo_way_wins_and_the_cards_are_only_the_fallback():
    both = parse_search_page(recorded_apollo(), query())
    cards_only = parse_search_page(recorded(), query())

    assert both.parser == "apollo"
    assert cards_only.parser == "dom"
    assert len(cards_only.offers) == 6


def test_the_cache_carries_no_review_text_only_the_aggregate():
    batch = parse_search_page(recorded_apollo(), query())

    for offer in batch.offers:
        # Note und Anzahl sind Kennzahlen, kein personenbezogener Text.
        assert offer.review_rating is None or 0.0 <= offer.review_rating <= 10.0
        assert offer.review_count is None or offer.review_count >= 0
        assert not any(
            field and len(str(field)) > 120
            for field in (offer.name, offer.city, offer.advertisers)
        )


def test_the_page_ships_the_property_type_catalogue_the_codes_come_from():
    catalogue = property_type_options(apollo_store(recorded_apollo()))

    assert catalogue[204] == "Hotels"
    assert 203 in catalogue and 201 in catalogue
    # `privacy_type=3` steht in derselben Gruppe, ist aber keine Unterkunftsart.
    assert 3 not in catalogue


def test_a_renamed_layout_is_an_error_and_not_an_empty_result():
    """`booking_kaputt_athen.html`: kein Apollo-Knoten, alle Testids umbenannt."""
    broken = (FIXTURES / "booking_kaputt_athen.html").read_text(encoding="utf-8")

    with pytest.raises(LayoutBroken, match="nichts wiedererkannt"):
        parse_search_page(broken, query())


def test_a_search_without_hits_stays_a_result_and_says_so():
    """`booking_leer_athen.html`: Cache da, Trefferliste leer, nbResultsTotal 0."""
    empty = (FIXTURES / "booking_leer_athen.html").read_text(encoding="utf-8")

    batch = parse_search_page(empty, query())

    assert batch.empty is True
    assert batch.total_results == 0
    assert batch.offers == []


def test_a_cache_that_no_longer_parses_falls_back_to_the_cards():
    page = '<script data-capla-store-data="apollo" type="application/json">{kaputt</script>'

    batch = parse_search_page(page + recorded(), query())

    assert batch.parser == "dom"
    assert len(batch.offers) == 6
    assert any("Apollo-Cache nicht gelesen" in note for note in batch.skipped)


def test_a_page_that_says_it_found_nothing_is_not_a_break():
    page = "<html><body><h1>Keine Unterkuenfte gefunden</h1></body></html>"

    batch = parse_search_page(page, query())

    assert batch.empty is True and batch.offers == []


def test_a_search_node_without_a_result_list_is_a_break():
    with pytest.raises(LayoutBroken, match="Trefferliste"):
        parse_apollo({"pagination": {"nbResultsTotal": 3}}, query())


# --------------------------------------------------------------------------
# Die WAF-Challenge im Browser
# --------------------------------------------------------------------------


class FakePage:
    """Eine Seite, die einen Status liefert und entweder fertig wird oder nicht."""

    def __init__(self, *, status: int, html: str, ready: bool = True) -> None:
        self.status = status
        self.html = html
        self.ready = ready
        self.waited: list[tuple[str, object, object]] = []

    async def route(self, _pattern, _handler) -> None:
        return None

    async def goto(self, _url, **_kwargs):
        return SimpleNamespace(status=self.status)

    async def wait_for_selector(self, selector, **kwargs):
        self.waited.append((selector, kwargs.get("state"), kwargs.get("timeout")))
        if not self.ready:
            raise TimeoutError("Zeit abgelaufen")
        return object()

    async def content(self) -> str:
        return self.html


class FakeContext:
    def __init__(self, page: FakePage, cookies=()) -> None:
        self.page = page
        self._cookies = list(cookies)
        self.closed = False

    async def new_page(self) -> FakePage:
        return self.page

    async def cookies(self) -> list[dict]:
        return list(self._cookies)

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self, context: FakeContext) -> None:
        self.context = context

    async def new_context(self, **_kwargs) -> FakeContext:
        return self.context


def browser_for(page: FakePage, cookies=()) -> tuple[BookingSource, FakeBrowser, FakeContext]:
    """Der echte Adapter, nur ohne Chromium und ohne Wartezeit im Takt."""
    source = BookingSource()
    source.limiter = RateLimiter(60, jitter=(0.0, 0.0))
    context = FakeContext(page, cookies)
    return source, FakeBrowser(context), context


async def test_a_202_is_the_challenge_and_not_a_block():
    """AWS WAF liefert die Challenge mit 202 aus. Ein Browser rechnet sie.

    Genau hier lag der Fehler: abgebrochen wurde in dem Moment, in dem sich die
    Challenge aufgeloest haette.
    """
    page = FakePage(status=CHALLENGE_STATUS, html=recorded_apollo())
    source, browser, context = browser_for(page)

    html = await source._page(browser, "https://www.booking.com/searchresults.de.html")

    assert parse_search_page(html, query()).offers
    assert context.closed is True
    # Gewartet wird auf den Apollo-Knoten *und* auf die Karten, und zwar mit
    # `attached`: ein `<script>` wird nie sichtbar.
    selector, state, timeout = page.waited[0]
    assert 'data-capla-store-data="apollo"' in selector
    assert 'data-testid="property-card"' in selector
    assert selector == READY_SELECTOR
    assert state == "attached"
    assert timeout >= 30_000


async def test_a_403_and_a_429_stay_immediate_rejections():
    for status in (403, 429):
        page = FakePage(status=status, html=recorded_apollo())
        source, browser, _ = browser_for(page)

        with pytest.raises(SourceBlocked, match=f"HTTP {status}"):
            await source._page(browser, "https://www.booking.com/searchresults.de.html")

        # Eine Ablehnung wird nicht ausgesessen: gar nicht erst gewartet.
        assert page.waited == []


async def test_a_challenge_that_never_resolves_is_a_block_with_a_clear_message():
    page = FakePage(
        status=CHALLENGE_STATUS,
        html="<html><body><script>challenge.js</script></body></html>",
        ready=False,
    )
    source, browser, _ = browser_for(page, cookies=[{"name": WAF_COOKIE, "value": "x"}])

    with pytest.raises(SourceBlocked) as caught:
        await source._page(browser, "https://www.booking.com/searchresults.de.html")

    message = str(caught.value)
    assert "Challenge" in message
    # Das Cookie ist Diagnose in der Meldung, nie eine Bedingung.
    assert f"{WAF_COOKIE} gesetzt" in message
    assert source.breaker.failures == 1


async def test_a_day_without_hits_survives_the_wait_instead_of_becoming_a_block():
    """Null Treffer ist ein Ergebnis. Es gibt dann weder Cache noch Karte."""
    page = FakePage(
        status=200,
        html="<html><body><h1>Keine Unterkuenfte gefunden</h1></body></html>",
        ready=False,
    )
    source, browser, _ = browser_for(page)

    html = await source._page(browser, "https://www.booking.com/searchresults.de.html")

    assert parse_search_page(html, query()).empty is True
    assert source.breaker.failures == 0


def test_availability_names_what_is_missing(monkeypatch):
    monkeypatch.setattr(
        "flightopt.hotels.sources.booking.importlib.util.find_spec", lambda name: None
    )

    usable, reason = BookingSource.availability()

    assert usable is False
    assert "Playwright" in reason
