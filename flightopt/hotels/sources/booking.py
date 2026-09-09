"""Booking.com ueber einen echten Browser, hinter einem Schalter.

Es gibt keinen offenen Zugang: die Demand API verlangt einen Vertrag und
verbietet ohnehin das Speichern von Preisen. Der oeffentliche Weg fuehrt durch
eine WAF, also durch einen echten Browser.

Gelesen wird die Seite in zwei Stufen. Die Suchergebnisse stehen im
ausgelieferten HTML bereits als JSON: Booking rendert serverseitig und legt den
Apollo-Cache seines eigenen Frontends in ein `<script>`. Das ist derselbe
Request, den der Browser ohnehin macht, also kostet dieser Weg **nichts**
zusaetzlich - er ist nur reichhaltiger (Koordinaten, Waehrung als Feld,
Gesamtzahl der Treffer) und deutlich weniger bruechig als eine Handvoll
`data-testid`-Selektoren. Die Karten im DOM bleiben als Rueckfallebene.

Vor der Seite steht eine AWS-WAF-Challenge. Die erste Antwort auf die
Ergebnisseite ist deshalb regelmaessig ein `202` mit dem Challenge-Dokument:
`challenge.js` (rund 1,3 MB) rechnet einen Proof-of-Work, setzt das Cookie
`aws-waf-token` und laedt danach die echte Seite. Ein echter Browser laeuft da
regulaer durch, also wartet dieser Adapter die Challenge ab, statt beim `202`
abzubrechen - abgebrochen wird erst, wenn danach immer noch nichts da ist.
Gewartet wird auf das, was die fertige Seite auszeichnet: der Apollo-Knoten
oder die Ergebniskarten.

Was dieser Adapter bewusst **nicht** tut, und zwar dauerhaft nicht:

* kein Stealth-Plugin, keine gefaelschten Automatisierungs-Merkmale,
* keine User-Agent-Rotation (ein fester, aktueller Chrome-Kennstring),
* keine Proxy-Rotation,
* kein Loesen von Captchas,
* kein Ernten des `aws-waf-token`, um es einem HTTP-Client unterzuschieben:
  die Challenge laeuft im Browser ab, der sie auch wirklich rechnet, oder gar
  nicht,
* keine Anfragen an `/alt_avail*` und `/monthly_minrates*` (robots.txt), und
  damit auch nicht an die GraphQL-Operation `AvailabilityCalendar`, die genau
  diese Daten liefert,
* kein Sammeln von Bewertungstexten oder Verfassern (personenbezogen). Die
  Gesamtnote und die Anzahl der Bewertungen sind aggregierte Kennzahlen und
  werden uebernommen.

Der Takt liegt bei 0,4 Anfragen pro Sekunde, mit hoechstens zwei gleichzeitig
offenen Seiten: mehr Chromium-Kontexte kosten Speicher, nicht Geschwindigkeit.
Bilder, Medien und Schriften werden abgebrochen, weil sie nur Bandbreite
beider Seiten kosten.

Playwright ist eine optionale Abhaengigkeit (`uv sync --group hotels`). Der
Adapter meldet ein fehlendes Playwright oder einen fehlenden Chromium als
klaren Fehler, statt beim Import zu knallen.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import urlencode, urljoin, urlsplit

from flightopt.hotels.browser import BrowserBudget, shared_pool
from flightopt.hotels.dom import Node, parse_html, testid, testid_in
from flightopt.hotels.models import HotelOffer, HotelQuery
from flightopt.hotels.prices import parse_count, parse_price, parse_rating, parse_stars
from flightopt.hotels.sources.base import (
    MAX_ERRORS,
    DayResult,
    HotelBatch,
    HotelSource,
    LayoutBroken,
    SourceBlocked,
    SourceError,
)

logger = logging.getLogger(__name__)

BASE = "https://www.booking.com/"
SEARCH_URL = "https://www.booking.com/searchresults.de.html"
AUTOCOMPLETE_URL = "https://accommodations.booking.com/autocomplete.json"
AUTOCOMPLETE_AID = 800210

# Ein fester, aktueller Chrome. Ein rotierender Kennstring waere eine
# Verschleierung, und verschleiert wird hier nichts.
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
# Von robots.txt gesperrt. Der Browser darf sie auch nicht nachladen.
FORBIDDEN_PATHS = ("/alt_avail", "/monthly_minrates")

CHALLENGE_STATUS = 202
"""Die erste Antwort der AWS-WAF: das Challenge-Dokument. Im Browser ist das
der Normalfall und kein Block - der Browser rechnet sie und wird danach auf die
echte Seite gelassen."""
REJECTING_STATUS = (403, 429)
"""Echte Ablehnungen. Daran ist nichts zu loesen, also sofort Schluss."""

READY_SELECTOR = (
    'script[data-capla-store-data="apollo"], div[data-testid="property-card"]'
)
"""Woran die fertige Ergebnisseite zu erkennen ist. Beides zaehlt, weil der
Apollo-Cache der Normalfall ist und die Karten die Rueckfallebene."""
CHALLENGE_TIMEOUT_MS = 45_000
"""Grosszuegig: die Challenge laedt ein Skript von rund 1,3 MB und rechnet
danach. Wer hier zu knapp misst, nennt jede langsame Runde einen Block."""
WAF_COOKIE = "aws-waf-token"
"""Nur Diagnose in der Fehlermeldung. Nie eine Bedingung, und schon gar nichts,
was diese Anwendung irgendwohin weiterreicht."""

PAGE_SIZE = 25
"""Treffer je Seite. Booking rechnet `offset` in genau diesen Schritten."""
MAX_RESULTS = 1000
"""Weiter blaettert Booking nicht. Darueber wird gemeldet, nicht geschnitten."""

ORDERS = (
    "popularity",
    "price",
    "class",
    "review_score_and_price",
    "distance_from_search",
)
"""Belegte Werte fuer `order`. Ein unbekannter waere ein stiller Rueckfall auf
die Standardsortierung, und der faellt niemandem auf."""

# Unterkunftsarten. Die Codes stammen aus dem Filterkatalog, den die
# Ergebnisseite selbst mitliefert (`filters`, Feld `ht_id`), nicht aus einer
# fremden Liste: `property_type_options` liest sie aus einer Aufzeichnung neu
# aus, wenn Booking den Katalog aendert.
HOTEL = 204
APARTMENT = 201
HOSTEL = 203
BED_AND_BREAKFAST = 208
VILLA = 213
BOAT = 215
GUESTHOUSE = 216
HOLIDAY_HOME = 220
LODGE = 221
PRIVATE_ROOM = 222
LOVE_HOTEL = 226

PROPERTY_TYPES: dict[str, int] = {
    "hotel": HOTEL,
    "ferienwohnung": APARTMENT,
    "hostel": HOSTEL,
    "bed_and_breakfast": BED_AND_BREAKFAST,
    "villa": VILLA,
    "boot": BOAT,
    "pension": GUESTHOUSE,
    "ferienhaus": HOLIDAY_HOME,
    "lodge": LODGE,
    "privatzimmer": PRIVATE_ROOM,
    "stundenhotel": LOVE_HOTEL,
}

HOTELS_ONLY = f"ht_id={HOTEL}"
"""Nur Hotels. Schlafsaal, Tageszimmer, Campingplatz und Boot gehoeren nicht in
dieselbe Preisverteilung wie ein Hotelzimmer."""

FREE_CANCELLATION = "fc=2"
AVAILABLE_ONLY = "oos=1"

REVIEW_BUCKETS = (60, 70, 80, 90)
"""Booking kennt keine freie Bewertungsschwelle, nur diese Stufen."""

PER_NIGHT = 1
"""Letzte Stelle im Preisfilter. Booking filtert den Preis pro Nacht."""

STARS_FROM_LABEL = re.compile(r"(\d+)\s+von\s+5")

APOLLO_SCRIPT = re.compile(
    r"<script[^>]*data-capla-store-data=[\"']apollo[\"'][^>]*>(.*?)</script>",
    re.DOTALL,
)
"""Der Apollo-Cache der Seite. Booking liefert ihn als `application/json`."""

ROOT_QUERY = "ROOT_QUERY"
SEARCH_QUERIES = "searchQueries"

NO_RESULTS_TESTIDS = (
    "no-results-message",
    "no_results_message",
    "search-no-results",
    "no-availability-message",
)
NO_RESULTS_TEXT = re.compile(
    r"keine\s+(?:passenden\s+)?(?:unterk(?:ü|ue|u)nfte|ergebnisse|treffer)"
    r"|no\s+properties\s+found",
    re.IGNORECASE,
)
"""Nur fuer die Rueckfallebene. Verlaesslich sagt der Apollo-Cache selbst, dass
eine Suche nichts ergab; im DOM ist es Text, und Text kann sich aendern."""


@dataclass(frozen=True, slots=True)
class Destination:
    """Ein aufgeloestes Ziel. Booking sucht nur ueber `dest_id` zuverlaessig."""

    dest_id: str
    dest_type: str
    label: str = ""


@dataclass(frozen=True, slots=True)
class PriceRange:
    """Der Preisfilter, zum Beispiel `price=EUR-50-200-1`.

    Booking meint damit den Preis **pro Nacht** ("Ihr Budget (pro Nacht)"), und
    die Waehrung steht im Filter selbst. Eine offene Seite schreibt Booking als
    `min` beziehungsweise `max`.
    """

    minimum: int | None = None
    maximum: int | None = None
    currency: str = "EUR"

    def __post_init__(self) -> None:
        if self.minimum is None and self.maximum is None:
            raise ValueError("Preisfilter ohne Grenze")
        if self.minimum is not None and self.minimum < 0:
            raise ValueError("Preisfilter mit negativer Untergrenze")
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.maximum <= self.minimum
        ):
            raise ValueError("Preisfilter: Obergrenze nicht ueber der Untergrenze")
        if len(self.currency) != 3:
            raise ValueError(f"Preisfilter mit unklarer Waehrung: {self.currency!r}")

    def as_filter(self) -> str:
        low = "min" if self.minimum is None else str(int(self.minimum))
        high = "max" if self.maximum is None else str(int(self.maximum))
        return f"price={self.currency.upper()}-{low}-{high}-{PER_NIGHT}"


def review_bucket(score: float | None) -> int | None:
    """Die Bewertungsstufe, die Booking kennt: 8,5 wird zu 80, 5,0 zu nichts.

    Aufgerundet wuerde der Filter mehr wegnehmen als gewuenscht, also wird
    abgerundet; die genauere Schwelle bleibt Sache des Aufrufers.
    """
    if score is None:
        return None
    value = int(round(float(score) * 10))
    usable = [bucket for bucket in REVIEW_BUCKETS if bucket <= value]
    return max(usable) if usable else None


def page_offsets(total: int, *, limit: int | None = None) -> list[int]:
    """Die Startpunkte in 25er-Schritten, hoechstens bis zur harten Grenze."""
    reachable = min(max(0, int(total)), MAX_RESULTS)
    offsets = list(range(0, reachable, PAGE_SIZE)) or [0]
    return offsets if limit is None else offsets[: max(0, int(limit))]


def pagination_note(total: int | None) -> str:
    """Der Satz fuer den Bericht, wenn Booking mehr hat als es hergibt."""
    if total is None or total <= MAX_RESULTS:
        return ""
    return (
        f"booking blaettert hoechstens {MAX_RESULTS} von {total} Treffern durch; "
        "engere Filter oder ein kleineres Gebiet zeigen den Rest"
    )


def build_filters(
    query: HotelQuery,
    *,
    property_types: Sequence[int] = (HOTEL,),
    price: PriceRange | None = None,
    free_cancellation: bool = False,
    available_only: bool = False,
) -> str:
    """Der `nflt`-Ausdruck: mit Semikolon getrennte Einzelfilter.

    Rein und ohne Netz, damit jede Kombination im Test nachlesbar ist statt
    erst an einer Ergebnisseite aufzufallen.
    """
    parts = [f"class={star}" for star in sorted(query.stars)]
    parts.extend(f"ht_id={int(kind)}" for kind in property_types)
    bucket = review_bucket(query.min_review_score)
    if bucket is not None:
        parts.append(f"review_score={bucket}")
    if price is not None:
        parts.append(price.as_filter())
    if free_cancellation:
        parts.append(FREE_CANCELLATION)
    if available_only:
        parts.append(AVAILABLE_ONLY)
    return ";".join(parts)


def build_search_url(
    query: HotelQuery,
    destination: Destination,
    *,
    offset: int = 0,
    order: str = "price",
    property_types: Sequence[int] = (HOTEL,),
    price: PriceRange | None = None,
    free_cancellation: bool = False,
    available_only: bool = False,
) -> str:
    """Die Ergebnisseite als URL. Rein, damit sie ohne Browser pruefbar ist."""
    if order not in ORDERS:
        raise ValueError(f"booking kennt die Sortierung {order!r} nicht: {ORDERS}")
    if offset < 0 or offset >= MAX_RESULTS:
        raise ValueError(f"offset {offset} liegt ausserhalb von 0 bis {MAX_RESULTS}")
    if offset % PAGE_SIZE:
        raise ValueError(f"offset {offset} ist kein Vielfaches von {PAGE_SIZE}")

    params: list[tuple[str, str]] = [
        ("ss", query.destination),
        ("ssne", query.destination),
        ("ssne_untouched", query.destination),
        ("dest_id", str(destination.dest_id)),
        ("dest_type", destination.dest_type),
        ("checkin", query.arrival.isoformat()),
        ("checkout", query.departure.isoformat()),
        ("group_adults", str(query.adults)),
        ("no_rooms", str(query.rooms)),
        ("group_children", str(len(query.children))),
    ]
    # Je Kind ein eigenes `age`, in der Reihenfolge der Alter.
    params.extend(("age", str(int(age))) for age in query.children)
    params.extend(
        [
            ("order", order),
            ("selected_currency", query.currency.upper()),
            ("lang", "de"),
            (
                "nflt",
                build_filters(
                    query,
                    property_types=property_types,
                    price=price,
                    free_cancellation=free_cancellation,
                    available_only=available_only,
                ),
            ),
        ]
    )
    if offset:
        params.append(("offset", str(offset)))
    return f"{SEARCH_URL}?{urlencode(params)}"


def autocomplete_body(text: str, *, size: int = 5) -> dict[str, Any]:
    return {"query": text.strip(), "aid": AUTOCOMPLETE_AID, "language": "de", "size": size}


def parse_autocomplete(payload: Any) -> Destination | None:
    """Das erste Ziel mit brauchbarer Kennung. Sonst `None`, nie geraten."""
    results = payload.get("results") if isinstance(payload, Mapping) else payload
    if not isinstance(results, list):
        return None
    for entry in results:
        if not isinstance(entry, Mapping):
            continue
        dest_id = entry.get("dest_id") or entry.get("id")
        dest_type = str(entry.get("dest_type") or entry.get("type") or "").lower()
        if dest_id is None or dest_type not in {"city", "region", "country"}:
            continue
        label = str(entry.get("label") or entry.get("name") or "").strip()
        return Destination(str(dest_id), dest_type, label)
    return None


# --------------------------------------------------------------------------
# Der Apollo-Cache: derselbe Request, mehr Inhalt
# --------------------------------------------------------------------------


def apollo_raw(html: str) -> str | None:
    """Der Rohtext des Apollo-Skripts, ohne ihn zu deuten."""
    match = APOLLO_SCRIPT.search(html)
    return match.group(1).strip() if match else None


def apollo_store(html: str) -> Mapping[str, Any] | None:
    """Der Apollo-Cache als Abbildung. `None`, wenn die Seite keinen traegt."""
    raw = apollo_raw(html)
    if raw is None:
        return None
    try:
        store = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LayoutBroken(f"booking: Apollo-Cache unlesbar: {exc}") from exc
    if not isinstance(store, Mapping):
        raise LayoutBroken("booking: Apollo-Cache ist kein Objekt")
    return store


def search_node(store: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """`ROOT_QUERY.searchQueries` und darin der Suchknoten.

    Der Schluessel des Knotens ist die komplette GraphQL-Anfrage samt Argumenten
    (`search({"input":{...}})`), also nichts, worauf man sich festnageln kann.
    Genommen wird der erste Eintrag, der kein `__typename` ist.
    """
    if not isinstance(store, Mapping):
        return None
    root = store.get(ROOT_QUERY)
    if not isinstance(root, Mapping):
        return None
    queries = root.get(SEARCH_QUERIES)
    if not isinstance(queries, Mapping):
        return None
    for key, value in queries.items():
        if key == "__typename":
            continue
        if isinstance(value, Mapping):
            return value
    return None


def property_type_options(store: Mapping[str, Any] | None) -> dict[int, str]:
    """Der Katalog der Unterkunftsarten, wie ihn die Seite selbst mitliefert.

    Damit die Codes nachpruefbar sind, statt aus einer fremden Liste
    abgeschrieben zu bleiben.
    """
    node = search_node(store)
    groups = node.get("filters") if isinstance(node, Mapping) else None
    if not isinstance(groups, list):
        return {}
    found: dict[int, str] = {}
    for group in groups:
        if not isinstance(group, Mapping) or group.get("field") != "ht_id":
            continue
        for option in group.get("options") or []:
            if not isinstance(option, Mapping):
                continue
            # In derselben Gruppe steht auch `privacy_type=3` ("ganze
            # Unterkunft"). Das ist keine Unterkunftsart, sondern ein zweiter
            # Filter, und er gehoert nicht in diese Tabelle.
            if not str(option.get("urlId") or "").startswith("ht_id="):
                continue
            code = option.get("id")
            label = _text(option.get("value"))
            if isinstance(code, int) and label:
                found[code] = label
    return found


def _text(value: Any) -> str:
    """Booking verpackt jeden Anzeigetext in `{text, translationTag}`."""
    if isinstance(value, Mapping):
        for key in ("text", "translation"):
            found = value.get(key)
            if isinstance(found, str) and found.strip():
                return found.strip()
        return ""
    return str(value).strip() if isinstance(value, str) else ""


def _mapping(value: Any, *path: str) -> Mapping[str, Any]:
    """Einen Pfad durch verschachtelte Abbildungen, ohne KeyError-Kaskade."""
    current: Any = value
    for step in path:
        if not isinstance(current, Mapping):
            return {}
        current = current.get(step)
    return current if isinstance(current, Mapping) else {}


def _coordinate(value: Any, limit: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if -limit <= float(value) <= limit else None


CHARGES_INCLUDED = re.compile(r"einschlie|inklusiv|includ", re.IGNORECASE)
"""Was Booking schreibt, wenn Steuern und Gebuehren im Preis stehen.

Der Text ist uebersetzt und damit fuer sich genommen bruechig. Er ist deshalb
auch nicht die Bedingung, sondern nur ihre letzte Haelfte: entschieden wird an
den Zahlen und Listen daneben, die keine Sprache haben.
"""


@dataclass(frozen=True, slots=True)
class ChargeEvidence:
    """Was die Antwort selbst ueber ihren Preis sagt.

    `inclusive` ist wahr, wenn die Seite den Preis als vollstaendig ausweist:
    keine herausgerechneten Posten, keine Steuerausnahmen, ein benanntes
    Zimmer, und die Auskunft daneben nennt Steuern und Gebuehren als
    enthalten. Fehlt eines davon, bleibt es beim Richtwert.

    Die Richtung ist Absicht. Schweigen ist kein Beleg: eine Seite, die nichts
    ueber ihre Gebuehren sagt, hat damit nicht gesagt, dass keine anfallen.
    """

    inclusive: bool
    reason: str


def _excluded_amount(prices: Mapping[str, Any]) -> float:
    aggregated = _mapping(
        prices, "excludedCharges", "excludeChargesAggregated", "amountPerStay"
    )
    value = aggregated.get("amountUnformatted")
    try:
        return abs(float(value))
    except (TypeError, ValueError):
        # Ein unlesbarer Betrag ist kein Beweis fuer null. Er zaehlt als
        # vorhanden, damit der Zweifel gegen die staerkere Aussage laeuft.
        return float("inf") if value is not None else 0.0


def charge_evidence(result: Mapping[str, Any]) -> ChargeEvidence:
    """Ist dieser Preis der Preis der Seite - oder nur eine Hausnummer?

    Vier Merkmale, alle aus derselben Antwort, drei davon ohne Sprache:

    1. `excludedCharges.excludeChargesList` ist leer,
    2. der herausgerechnete Betrag ist null,
    3. `taxExceptions` ist leer,
    4. `blocks[0].blockId.roomId` benennt ein Zimmer, und
    5. `chargesInfo` nennt Steuern und Gebuehren als enthalten.

    In der Aufzeichnung vom 10.11.2026 (Athen, sechs Treffer) treffen alle
    fuenf zu, und der angezeigte Gesamtpreis ist auf die vierte Nachkommastelle
    derselbe Betrag wie `blocks[0].finalPrice`. Das ist kein Von-Preis und kein
    Mittelwert ueber Anbieter, sondern der Tarif eines benannten Zimmers.

    Gegengeprobt an einer heutigen Antwort (Barcelona, dreissig Tage Vorlauf,
    zwei Ergebnisseiten zu je 25 Treffern, `spike/probe_booking_live.py`):
    24 von 25 tragen "Einschliesslich Steuern und Gebuehren", je einer traegt
    einen herausgerechneten Posten und bleibt damit Richtwert. Die Regel
    unterscheidet also, statt pauschal umzuschalten - und genau das war der
    Punkt.
    """
    prices = _mapping(result, "priceDisplayInfoIrene")
    if not prices:
        return ChargeEvidence(False, "keine Preisauskunft in der Antwort")

    blocks = result.get("blocks")
    first = blocks[0] if isinstance(blocks, list) and blocks else None
    if not str(_mapping(first, "blockId").get("roomId") or "").strip():
        return ChargeEvidence(False, "kein benanntes Zimmer am Preis")

    excluded_list = _mapping(prices, "excludedCharges").get("excludeChargesList")
    if isinstance(excluded_list, list) and excluded_list:
        return ChargeEvidence(
            False, f"{len(excluded_list)} Posten sind aus dem Preis herausgerechnet"
        )
    amount = _excluded_amount(prices)
    if amount:
        return ChargeEvidence(False, f"{amount} herausgerechnet")

    exceptions = prices.get("taxExceptions")
    if isinstance(exceptions, list) and exceptions:
        return ChargeEvidence(False, f"{len(exceptions)} Steuerausnahmen genannt")

    info = _text(prices.get("chargesInfo"))
    if not info:
        return ChargeEvidence(False, "keine Auskunft zu Steuern und Gebuehren")
    if not CHARGES_INCLUDED.search(info):
        return ChargeEvidence(False, f"Auskunft der Seite: {info!r}")
    return ChargeEvidence(True, f"Auskunft der Seite: {info!r}")


def _apollo_price(result: Mapping[str, Any]) -> tuple[Any, str]:
    """Gesamtpreis und Waehrung, beide aus Feldern und nie aus einem Symbol.

    `amountUnformatted` ist die Zahl ohne Tausenderpunkt und ohne Symbol,
    `currency` steht daneben. Der angezeigte Text (`amount`) traegt je nach
    Sprache ein anderes Format und wird nicht angefasst.
    """
    stay = _mapping(result, "priceDisplayInfoIrene", "displayPrice", "amountPerStay")
    if not stay:
        # Zweite Quelle derselben Seite: der Preis des passenden Zimmers.
        blocks = result.get("blocks")
        first = blocks[0] if isinstance(blocks, list) and blocks else None
        stay = _mapping(first, "finalPrice")
    amount = stay.get("amountUnformatted")
    if amount is None:
        amount = stay.get("amount")
    currency = str(stay.get("currency") or "").strip().upper()
    return amount, currency


def _apollo_offer(
    result: Mapping[str, Any], query: HotelQuery
) -> tuple[HotelOffer | None, str, bool]:
    """Eine Zeile des Caches zu einem Angebot.

    Rueckgabe ist (Angebot, Grund, lesbar). "Lesbar" trennt die Zeile, die wir
    absichtlich weglassen (Sterne passen nicht), von der Zeile, die wir nicht
    verstanden haben - nur die zweite ist ein Hinweis auf einen Umbau.
    """
    basic = _mapping(result, "basicPropertyData")
    name = _text(result.get("displayName"))
    if not name:
        return None, "Treffer ohne Namen", False

    amount, currency = _apollo_price(result)
    # Die Waehrung kommt aus dem Feld daneben. Aus dem Symbol im Anzeigetext
    # waere sie geraten: "$" traegt ein Dutzend Waehrungen.
    price = parse_price(amount, currency) if currency else None
    if price is None:
        if _mapping(result, "soldOutInfo").get("isSoldOut") is True:
            return None, f"{name}: ausgebucht", True
        if not currency:
            return None, f"{name}: Preis ohne Waehrungsangabe ({amount!r})", False
        return None, f"{name}: Preis unlesbar ({amount!r})", False

    location = _mapping(basic, "location")
    country = str(location.get("countryCode") or "").strip().lower()
    page_name = str(basic.get("pageName") or "").strip()
    url = (
        urljoin(BASE, f"hotel/{country}/{page_name}.de.html")
        if country and page_name
        else None
    )
    stars = parse_stars(_mapping(basic, "starRating").get("value"))
    if query.stars and stars not in query.stars:
        return None, f"{name}: {stars or 'ohne'} Sterne, gesucht {sorted(query.stars)}", True

    reviews = _mapping(basic, "reviews")
    property_id = basic.get("id")
    if country and page_name:
        key = f"booking:{country}/{page_name}"
    elif property_id is not None:
        key = f"booking:id/{property_id}"
    else:
        key = _property_key(url, name)

    return (
        HotelOffer(
            source="booking",
            property_key=key,
            name=name,
            arrival=query.arrival,
            departure=query.departure,
            price_total=price,
            stars=stars,
            city=str(location.get("city") or "").strip() or query.destination,
            country=country.upper() or None,
            # Aggregat, kein Bewertungstext: die Note und wie viele Gaeste sie
            # gebildet haben. Verfasser und Freitext bleiben in der Seite.
            review_rating=parse_rating(reviews.get("totalScore")),
            review_count=parse_count(reviews.get("reviewsCount")),
            url=url,
            lat=_coordinate(location.get("latitude"), 90.0),
            lon=_coordinate(location.get("longitude"), 180.0),
            party_size=query.party_size,
            rooms=query.rooms,
            # Nicht fest wahr, sondern aus der Antwort abgeleitet. Der Beleg
            # steht in `charge_evidence`; das Feld heisst weiterhin
            # "Richtwert", und genau das ist dieser Preis eben nicht, sobald
            # die Seite ihn als vollstaendig und zimmerbezogen ausweist.
            indicative=not charge_evidence(result).inclusive,
        ),
        "",
        True,
    )


def parse_apollo(node: Mapping[str, Any], query: HotelQuery) -> HotelBatch:
    """Den Suchknoten zu Angeboten. Wirft, wenn er nicht wiederzuerkennen ist."""
    results = node.get("results")
    if not isinstance(results, list):
        raise LayoutBroken("booking: Suchknoten ohne Trefferliste")

    total = _mapping(node, "pagination").get("nbResultsTotal")
    batch = HotelBatch(
        parser="apollo",
        total_results=int(total) if isinstance(total, int) else None,
        empty=not results,
    )
    note = pagination_note(batch.total_results)
    if note:
        batch.skipped.append(note)

    unreadable = 0
    for result in results:
        if not isinstance(result, Mapping):
            unreadable += 1
            continue
        offer, reason, readable = _apollo_offer(result, query)
        if offer is not None:
            batch.offers.append(offer)
            continue
        if reason:
            batch.skipped.append(reason)
        if not readable:
            unreadable += 1

    if results and unreadable == len(results):
        raise LayoutBroken(
            f"booking: {len(results)} Treffer im Apollo-Cache, keiner davon lesbar"
        )
    return batch


def parse_search_page(html: str, query: HotelQuery) -> HotelBatch:
    """Erst der Apollo-Cache, dann die Karten, sonst ein Fehler.

    Der Cache ist reicher und aendert sich seltener als die Klassennamen im
    DOM; die Karten bleiben, weil eine Rueckfallebene, die nie geuebt wird,
    keine ist.

    Genau hier liegt die Gesundheitspruefung: eine Seite, die geladen wurde,
    aber weder im Cache noch im DOM etwas hergibt und auch nicht sagt, dass sie
    nichts gefunden hat, ist ein Umbau und kein leerer Tag.
    """
    reason = ""
    try:
        store = apollo_store(html)
        if store is None:
            reason = "kein Apollo-Cache in der Seite"
        else:
            node = search_node(store)
            if node is None:
                reason = "Apollo-Cache ohne Suchknoten"
            else:
                return parse_apollo(node, query)
    except LayoutBroken as exc:
        reason = str(exc).removeprefix("booking: ")

    dom = parse_result_html(html, query)
    if dom.offers or dom.skipped:
        logger.info("booking: Apollo-Weg ausgefallen (%s), Karten gelesen", reason)
        dom.skipped.append(f"Apollo-Cache nicht gelesen: {reason}")
        return dom
    if says_no_results(html):
        return HotelBatch(parser="dom", total_results=0, empty=True)
    raise LayoutBroken(
        f"booking: Seite geladen, aber nichts wiedererkannt ({reason}, "
        "keine Ergebniskarte im DOM, kein Hinweis auf null Treffer)"
    )


# --------------------------------------------------------------------------
# Die Karten im DOM, als Rueckfallebene
# --------------------------------------------------------------------------


def _property_key(url: str | None, name: str) -> str:
    """Aus der Objekt-URL eine stabile Kennung.

    `/hotel/gr/melia-athens.de.html` wird zu `booking:gr/melia-athens`. Der
    Pfad ist das einzige Stueck der URL, das ueber Suchen hinweg gleich bleibt;
    alles ab dem Fragezeichen traegt Datum und Belegung.
    """
    path = urlsplit(url or "").path
    match = re.search(r"/hotel/([^/]+)/([^/]+?)(?:\.[a-z-]{2,7})*\.html$", path)
    if match:
        return f"booking:{match.group(1)}/{match.group(2)}"
    slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    return f"booking:name/{slug}" if slug else "booking:unbekannt"


def _stars(card: Node) -> int | None:
    """Sterne aus dem Vorlesetext, sonst aus der Zahl der Symbole."""
    for node in card.walk():
        label = node.attrs.get("aria-label")
        if not label:
            continue
        match = STARS_FROM_LABEL.search(label)
        if match:
            value = int(match.group(1))
            if 1 <= value <= 5:
                return value
    container = card.find(testid_in("rating-stars", "rating-squares", "rating-circles"))
    if container is not None:
        count = len(container.children)
        if 1 <= count <= 5:
            return count
    return None


def parse_result_html(html: str, query: HotelQuery) -> HotelBatch:
    """Die Ergebniskarten zu Angeboten.

    Bewertungstexte werden nicht angefasst. Gelesen werden Name, Objekt-URL,
    Sterne und der angezeigte Preis, mehr gibt eine Karte nicht her.
    """
    document = parse_html(html)
    batch = HotelBatch(parser="dom")

    for card in document.find_all(testid("property-card")):
        link = card.find(testid("title-link"))
        title = card.find(testid("title"))
        name = (title.text if title is not None else "") or (
            link.attrs.get("aria-label", "") if link is not None else ""
        )
        name = name.strip()
        if not name:
            batch.skipped.append("Karte ohne Namen")
            continue

        price_node = card.find(testid("price-and-discounted-price"))
        raw_price = price_node.text if price_node is not None else ""
        price = parse_price(raw_price, query.currency)
        if price is None:
            batch.skipped.append(f"{name}: Preis unlesbar ({raw_price!r})")
            continue

        url = None
        if link is not None and link.attrs.get("href"):
            url = urljoin(BASE, link.attrs["href"])
        stars = _stars(card)
        if query.stars and stars not in query.stars:
            batch.skipped.append(f"{name}: {stars or 'ohne'} Sterne, gesucht {sorted(query.stars)}")
            continue

        batch.offers.append(
            HotelOffer(
                source="booking",
                property_key=_property_key(url, name),
                name=name,
                arrival=query.arrival,
                departure=query.departure,
                price_total=price,
                stars=stars,
                city=query.destination,
                url=url,
                party_size=query.party_size,
                rooms=query.rooms,
                # Eine Ergebniskarte sagt nicht, was im Preis steckt. Auf der
                # Rueckfallebene bleibt es deshalb beim Richtwert, auch wenn
                # derselbe Preis ueber den Apollo-Cache belegbar waere.
                indicative=True,
            )
        )
    return batch


def says_no_results(html: str) -> bool:
    """Sagt die Seite selbst, dass sie nichts gefunden hat?

    Nur fuer den Fall ohne Apollo-Cache. Ein Treffer hier ist ein Ergebnis,
    kein Defekt - und der Unterschied ist der ganze Punkt.
    """
    document = parse_html(html)
    if document.find(testid_in(*NO_RESULTS_TESTIDS)) is not None:
        return True
    return bool(NO_RESULTS_TEXT.search(html))


async def wait_until_ready(page: Any, *, timeout_ms: int = CHALLENGE_TIMEOUT_MS) -> bool:
    """Warten, bis die fertige Seite da ist. Wahr, wenn sie es wurde.

    `state="attached"` ist kein Detail: der Apollo-Knoten ist ein `<script>`
    und damit nie sichtbar. Mit der Voreinstellung (`visible`) wuerde hier
    jede Seite in den Zeitablauf laufen, auch die heile.

    Ein Zeitablauf ist hier ausdruecklich noch kein Fehler, sondern eine
    Auskunft: der Aufrufer sieht danach im HTML nach, ob die Seite vielleicht
    einfach nichts gefunden hat.
    """
    try:
        await page.wait_for_selector(READY_SELECTOR, state="attached", timeout=timeout_ms)
    except Exception:  # noqa: BLE001 - Zeitablauf ist eine Antwort, kein Defekt
        return False
    return True


async def has_waf_token(context: Any) -> bool:
    """Steht nach der Navigation ein `aws-waf-token` im Kontext?

    Ausschliesslich fuer die Fehlermeldung: "Challenge lief, aber die Seite
    kam trotzdem nicht" liest sich anders als "die Challenge fing gar nicht
    erst an". Der Wert selbst wird nicht gelesen und nirgends hingetragen.
    """
    reader = getattr(context, "cookies", None)
    if reader is None:
        return False
    try:
        cookies = await reader()
    except Exception:  # noqa: BLE001 - Diagnose darf nie den Lauf kippen
        return False
    return any(
        isinstance(cookie, Mapping) and cookie.get("name") == WAF_COOKIE
        for cookie in cookies or ()
    )


ENV_BROWSER_ARGS = "FLIGHTOPT_HOTELS_BROWSER_ARGS"
"""Schalter fuer Chromium, mit Komma getrennt.

Sie stehen in der Umgebung und nicht im Code, weil sie eine Betriebs- und
keine Programmentscheidung sind: auf einem Arbeitsrechner behaelt Chromium
seine eigene Sandbox, im Container laeuft alles als root und er startet nur
ohne sie - dort ist der Container die Grenze. Das Image setzt den Wert, der
Arbeitsrechner laesst ihn leer.
"""

PAGE_DEADLINE_S = 120.0
"""Frist fuer eine ganze Seite: Navigation, Challenge, Inhalt.

Playwright hat je Schritt eine Frist, aber nicht fuer den Vorgang. Im
Dauerbetrieb ist genau das der Unterschied zwischen einem verlorenen Tag und
einer Quelle, die nie wieder etwas liefert: eine haengende Seite haelt sonst
ihren Platz im Fenster fuer immer.
"""

_PLAYWRIGHT: Any = None
"""Der laufende Playwright-Treiber dieses Prozesses.

Er lebt neben dem Browser und nicht in ihm: `async_playwright()` als Block zu
fahren hiesse, den Treiber zu jedem Start neu hochzuziehen, und das ist genau
der Prozessstart, den der lange Browser sparen soll.
"""


def browser_args(env: Mapping[str, str] | None = None) -> list[str]:
    """Die Chromium-Schalter aus der Umgebung, leere Eintraege weg."""
    env = os.environ if env is None else env
    raw = str(env.get(ENV_BROWSER_ARGS, "") or "")
    return [part.strip() for part in raw.split(",") if part.strip()]


async def launch_chromium(env: Mapping[str, str] | None = None) -> Any:
    """Einen headless Chromium starten. Der Treiber bleibt danach stehen."""
    global _PLAYWRIGHT
    if _PLAYWRIGHT is None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise SourceError(
                "booking: Playwright fehlt, `uv sync --group hotels`"
            ) from exc
        _PLAYWRIGHT = await async_playwright().start()
    try:
        return await _PLAYWRIGHT.chromium.launch(headless=True, args=browser_args(env))
    except Exception as exc:  # noqa: BLE001 - fehlender Browser
        raise SourceError(
            "booking: Chromium fehlt, `uv run playwright install chromium`"
        ) from exc


class BookingSource(HotelSource):
    """Zweite Quelle, standardmaessig aus. Braucht Playwright und Chromium."""

    name = "booking"
    indicative = False
    """Booking ist kein Vergleichsportal, sondern der Haendler selbst.

    Das Kennzeichen an der Quelle beantwortet eine andere Frage als das am
    Angebot: hier steht, ob die Zahlen dieser Quelle systematisch neben dem
    Direktpreis liegen, wie bei einem monetarisiert sortierenden
    Vergleichsportal. Booking zeigt seinen eigenen Preis, also nein - ob eine
    einzelne Zahl trotzdem nur ein Richtwert ist, entscheidet `charge_evidence`
    je Treffer.
    """
    # 0,4 Anfragen pro Sekunde.
    per_minute = 24
    # Zwei gleichzeitige Seiten. Nicht mehr Anfragen pro Sekunde, sondern
    # weniger Warten auf die Antwort - und zwei Chromium-Kontexte sind noch
    # bezahlbar, vier nicht.
    concurrency = 2

    def __init__(
        self,
        *,
        launcher: Any = None,
        budget: BrowserBudget | None = None,
        env: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._http: Any = None
        self._destinations: dict[str, Destination] = {}
        self._destination_lock = asyncio.Lock()
        self._env = env
        # Der Pool ist gemeinsam und ueberlebt diese Instanz. Der Katalog wird
        # je Durchlauf neu gebaut; ein Browser an der Instanz waere ein Start
        # je Durchgang, und genau den soll der lange Browser sparen.
        self._pool = shared_pool(self.name, launcher or launch_chromium, budget=budget)

    @classmethod
    def availability(cls, env: Mapping[str, str] | None = None) -> tuple[bool, str]:
        if importlib.util.find_spec("playwright") is None:
            return False, "Playwright fehlt, `uv sync --group hotels`"
        return True, ""

    def _new_session(self) -> Any:
        from curl_cffi import requests as creq

        return creq.Session(impersonate="chrome")

    @property
    def session(self) -> Any:
        if self._http is None:
            self._http = self._new_session()
        return self._http

    def close(self) -> None:
        """Die HTTP-Sitzung freigeben - den Browser ausdruecklich nicht.

        Der Pool ist gemeinsam und ueberlebt diese Instanz mit Absicht; ihn
        hier zu schliessen hiesse, ihn nach jedem Durchlauf neu zu starten.
        Wann der Chromium geht, entscheidet sein Budget: nach dem
        Seitenbudget, nach der Hoechstdauer, oder wenn zwei Minuten lang
        niemand mehr eine Seite wollte. Zum Prozessende geht er ohnehin.
        """
        session, self._http = self._http, None
        if session is None:
            return
        try:
            session.close()
        except Exception:  # noqa: BLE001 - Schliessen darf nie nach oben werfen
            logger.debug("booking: Sitzung liess sich nicht schliessen", exc_info=True)

    async def resolve_destination(self, text: str) -> Destination:
        """Freitext zu `dest_id` und `dest_type`, einmal je Ziel.

        Das Schloss ist noetig, seit mehrere Tage gleichzeitig laufen: sonst
        fragen zehn Tage dasselbe Ziel zehnmal nach, bevor der erste antwortet.
        """
        cached = self._destinations.get(text.casefold())
        if cached is not None:
            return cached
        async with self._destination_lock:
            cached = self._destinations.get(text.casefold())
            if cached is not None:
                return cached
            destination = await self._ask_destination(text)
            self._destinations[text.casefold()] = destination
            return destination

    async def _ask_destination(self, text: str) -> Destination:
        async with self.limiter.slot():
            response = await asyncio.to_thread(
                self.session.post,
                AUTOCOMPLETE_URL,
                json=autocomplete_body(text),
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=30,
            )
        status = int(getattr(response, "status_code", 0))
        if status in (CHALLENGE_STATUS, *REJECTING_STATUS):
            # Hier laeuft kein Browser, also rechnet niemand die Challenge:
            # ein 202 bleibt auf diesem Weg ein Abbruchgrund. Ein Token aus dem
            # Browser hierher zu tragen waere genau das Umgehen, das wir nicht
            # tun.
            self.breaker.record_block()
            raise SourceBlocked(f"booking: HTTP {status} bei der Zielsuche")
        if status != 200:
            raise SourceError(f"booking: Zielsuche HTTP {status}")
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise SourceError(f"booking: Zielsuche unlesbar: {exc}") from exc
        destination = parse_autocomplete(payload)
        if destination is None:
            raise SourceError(f"booking: kein Ziel zu {text!r}")
        return destination

    async def fetch_html(self, query: HotelQuery, *, offset: int = 0) -> str:
        """Die rohe Ergebnisseite. Fuer die Aufzeichnung der Testdaten."""
        destination = await self.resolve_destination(query.destination)
        return await self.render(build_search_url(query, destination, offset=offset))

    @property
    def pool(self) -> Any:
        """Der gemeinsame Browser-Pool dieser Quelle."""
        return self._pool

    @asynccontextmanager
    async def browser(self) -> AsyncIterator[Any]:
        """Ein Chromium fuer die Dauer dieses Blocks, aus dem gemeinsamen Pool.

        Frueher war das ein eigener Prozess je Faecher: gestartet, benutzt,
        weggeworfen. Fuer den Handbetrieb ging das; fuer eine Quelle, die alle
        paar Minuten von selbst laeuft, ist der Start je Durchgang nur Verlust.
        Jetzt entscheidet der Pool, wie lange der Prozess bleibt - und faellt
        dieser Block mit einer Ausnahme aus, wirft er ihn weg.
        """
        async with self._pool.page() as browser:
            yield browser

    async def render(self, url: str, *, browser: Any = None) -> str:
        """Die Seite in einem echten, unveraenderten Chromium laden."""
        if browser is not None:
            return await self._page(browser, url)
        async with self.browser() as own:
            return await self._page(own, url)

    async def _page(
        self, browser: Any, url: str, *, deadline: float = PAGE_DEADLINE_S
    ) -> str:
        """Eine Seite in eigenem Kontext. Haelt einen Platz des Limiters.

        Der Status der ersten Antwort entscheidet hier **nicht** allein. `403`
        und `429` sind Ablehnungen und damit sofort Schluss; `202` ist die
        WAF-Challenge, also der erwartete Anfang und kein Ergebnis. Entschieden
        wird erst, wenn die Challenge Zeit hatte durchzulaufen: entweder steht
        dann die fertige Seite da, oder sie sagt selbst, dass sie nichts
        gefunden hat, oder es war doch eine Sperre.

        Ueber allem liegt eine Frist fuer den ganzen Vorgang. Playwright misst
        je Schritt; wer nur so misst, hat fuer den Fall, dass ein Schritt gar
        nicht zurueckkehrt, keine Grenze. Im Dauerbetrieb waere das kein
        verlorener Tag, sondern eine Quelle, die nie wieder etwas liefert.
        """
        async with self.limiter.slot():
            context = await browser.new_context(
                user_agent=CHROME_UA,
                locale="de-DE",
                viewport={"width": 1366, "height": 900},
            )
            try:
                async with asyncio.timeout(deadline):
                    return await self._read(context, url)
            except TimeoutError as exc:
                # Kein Vermerk bei der Sicherung: eine Frist, die reisst, sagt
                # etwas ueber diese Seite und nicht darueber, ob Booking uns
                # abweist. Der Browser wird trotzdem weggeworfen, dafuer sorgt
                # der Pool beim Verlassen von `browser()`.
                raise SourceError(
                    f"booking: Seite nach {deadline:.0f}s Frist abgebrochen ({url})"
                ) from exc
            finally:
                await context.close()

    async def _read(self, context: Any, url: str) -> str:
        """Der eigentliche Ladevorgang. Den Kontext schliesst `_page`."""
        page = await context.new_page()
        await page.route("**/*", _gate)
        response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        status = int(getattr(response, "status", 200) or 200)
        if status in REJECTING_STATUS:
            self.breaker.record_block()
            raise SourceBlocked(f"booking: HTTP {status} auf der Ergebnisseite")
        if status == CHALLENGE_STATUS:
            logger.info("booking: WAF-Challenge auf %s, warte sie ab", url)

        ready = await wait_until_ready(page)
        html = str(await page.content())
        if ready:
            self.breaker.record_success()
            return html
        # Kein Apollo-Knoten und keine Karte. Das kann immer noch ein
        # ehrlicher Null-Treffer-Tag sein, und der ist ein Ergebnis.
        if says_no_results(html):
            logger.info("booking: null Treffer auf %s", url)
            return html
        # Haelt eine Notiz fuer die Fehlermeldung, keinen Schluessel.
        # Der Name sagt das jetzt auch, sonst schlaegt der Secret-Scan
        # in scripts/sync_public.py bei jedem Abgleich an.
        waf_note = (
            "aws-waf-token gesetzt" if await has_waf_token(context) else "kein aws-waf-token"
        )
        self.breaker.record_block()
        raise SourceBlocked(
            f"booking: HTTP {status}, aber nach "
            f"{CHALLENGE_TIMEOUT_MS // 1000}s weder Apollo-Cache noch "
            f"Ergebniskarte noch ein Hinweis auf null Treffer ({waf_note}): "
            "die WAF-Challenge ist nicht durchgelaufen"
        )

    async def search(self, query: HotelQuery, *, pages: int = 1) -> HotelBatch:
        async with self.browser() as browser:
            return await self.search_with(browser, query, pages=pages)

    async def search_with(
        self, browser: Any, query: HotelQuery, *, pages: int = 1
    ) -> HotelBatch:
        """Eine Suche im schon offenen Browser, standardmaessig die erste Seite.

        Weitere Seiten kosten je eine Navigation im Takt der Quelle, deshalb
        holt der Durchlauf sie nur, wenn er sie ausdruecklich bestellt.
        """
        destination = await self.resolve_destination(query.destination)
        batch: HotelBatch | None = None
        for offset in page_offsets(MAX_RESULTS, limit=max(1, int(pages))):
            html = await self.render(
                build_search_url(query, destination, offset=offset), browser=browser
            )
            page = parse_search_page(html, query)
            batch = page if batch is None else batch.extend(page)
            total = page.total_results
            if page.empty or total is None:
                break
            # Weiter als `MAX_RESULTS` blaettert Booking nicht; dass dahinter
            # noch etwas liegt, hat `parse_apollo` bereits vermerkt.
            if offset + PAGE_SIZE >= min(total, MAX_RESULTS):
                break
        return batch if batch is not None else HotelBatch()

    async def search_many(
        self,
        queries: Sequence[HotelQuery],
        *,
        max_errors: int = MAX_ERRORS,
        pages: int = 1,
    ) -> list[DayResult]:
        """Mehrere Anreisetage im Takt dieser Quelle.

        Jeder Tag holt sich den Browser einzeln aus dem Pool - und bekommt
        ueber das Fenster hinweg denselben. Frueher hing ein Browser am ganzen
        Faecher; damit zaehlte das Budget Faecher statt Seiten, und ein
        haengender Tag riss den Prozess mit, den die uebrigen noch brauchten.
        Wie viele Seiten gleichzeitig offen sind, entscheidet weiterhin der
        Limiter (zwei).
        """
        if not queries:
            return []

        async def one(query: HotelQuery) -> HotelBatch:
            async with self.browser() as browser:
                return await self.search_with(browser, query, pages=pages)

        return await self.run_many(queries, one, max_errors=max_errors)


async def _gate(route: Any) -> None:
    """Bilder, Medien, Schriften und gesperrte Pfade gar nicht erst holen."""
    request = route.request
    url = str(request.url)
    if any(part in url for part in FORBIDDEN_PATHS):
        await route.abort()
        return
    if str(request.resource_type) in BLOCKED_RESOURCE_TYPES:
        await route.abort()
        return
    await route.continue_()
