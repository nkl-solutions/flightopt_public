"""Aufenthalte ableiten, entdoppeln, bepreisen und zur Gesamtsumme addieren.

Der Spar-Hebel steckt im Entdoppeln. Ein Kandidatenfeld von zwanzig Zeilen mit
je zwei Aufenthalten waeren vierzig Abfragen; weil der Optimierer dieselben
Datumspaare immer wieder vorschlaegt, bleiben nach dem Entdoppeln meist eine
Handvoll uebrig. Blind einen Zeitraum zu scannen kostet ein Vielfaches davon
und liefert Termine, die niemand vorgeschlagen hat.

Zwei Regeln stehen ueber allem:

* **Nichts erfinden.** Fehlt fuer einen Aufenthalt ein Preis, bleibt die
  Gesamtsumme leer. Eine Zeile mit Luecke darf nicht billiger aussehen als
  eine vollstaendige.
* **"ab", nicht "Preis".** Genommen wird der guenstigste Treffer je Aufenthalt.
  Ein Median waere ehrlicher gegenueber dem Markt, aber gesucht wird der
  Bestpreis; also heisst er in der Oberflaeche auch so.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Mapping, Sequence

from flightopt.domain import airports as airport_registry
from flightopt.hotels.models import HotelOffer, HotelQuery
from flightopt.hotels.sources.base import HotelSource

logger = logging.getLogger(__name__)

STAY_BUDGET = 40
"""Abfragen je Lauf. Zwanzig Zeilen mit je zwei Aufenthalten sind die
Obergrenze vor dem Entdoppeln; danach bleibt fast immer viel Luft. Wer die
Grenze trotzdem reisst, erfaehrt es, statt eine gekuerzte Liste fuer eine
vollstaendige zu halten."""

DEFAULT_ADULTS = 2
DEFAULT_ROOMS = 1

SourceCatalogue = Callable[[], Sequence[HotelSource]] | Sequence[HotelSource]


@dataclass(frozen=True, slots=True)
class Stay:
    """Eine Uebernachtungsstrecke zwischen zwei Fluegen.

    `code` ist der Flughafen, `city` der Ort, unter dem eine Quelle ihn kennt.
    Zwei Flughaefen derselben Stadt fallen ueber `city` zusammen, und das ist
    beabsichtigt: gesucht wird ein Bett, kein Terminal.
    """

    code: str
    city: str
    arrival: date
    nights: int

    @property
    def key(self) -> tuple[str, str, int]:
        """Das Tripel, ueber das entdoppelt wird: Stadt, Anreise, Naechte."""
        return (self.city.casefold(), self.arrival.isoformat(), self.nights)

    @property
    def departure(self) -> date:
        return self.arrival + timedelta(days=self.nights)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "city": self.city,
            "arrival": self.arrival.isoformat(),
            "departure": self.departure.isoformat(),
            "nights": self.nights,
        }


@dataclass(frozen=True, slots=True)
class StayPrice:
    """Der guenstigste Treffer eines Aufenthalts, samt Herkunft."""

    minor: int
    currency: str
    source: str
    name: str = ""


@dataclass(slots=True)
class StayQuote:
    """Was die Abfrage ergab und was sie gekostet hat."""

    prices: dict[tuple[str, str, int], StayPrice] = field(default_factory=dict)
    asked: int = 0
    """Aufenthalte, nach denen wirklich gefragt wurde."""
    calls: int = 0
    """Einzelabrufe: Aufenthalte mal Quellen. Der Preis in Netzverkehr."""
    notes: list[str] = field(default_factory=list)
    skipped: list[Stay] = field(default_factory=list)
    """Aufenthalte, die das Budget nicht mehr hergab."""


@dataclass(frozen=True, slots=True)
class StayOptions:
    """Der Schalter samt Belegung. Ohne `sources` laeuft der Schritt nicht.

    Standard ist also aus: eine Flugsuche ohne diese Angaben verhaelt sich
    Zeile fuer Zeile wie bisher und ruft keine einzige Unterkunft ab.
    """

    sources: SourceCatalogue | None = None
    adults: int = DEFAULT_ADULTS
    rooms: int = DEFAULT_ROOMS
    currency: str = "EUR"
    country: str = "DE"
    budget: int = STAY_BUDGET


def city_of(code: str) -> str:
    """Der Stadtname zu einem IATA-Code, sonst der Code selbst.

    Eine Quelle sucht nach "Athen", nicht nach "ATH". Kennt die Registry den
    Code nicht, geht der Code hinaus: falsch raten waere schlimmer als eine
    Suche, die nichts findet.
    """
    airport = airport_registry.by_code(str(code or ""))
    city = (airport.city if airport is not None else "") or ""
    return city.strip() or str(code or "").strip().upper()


def _leg_date(leg: Mapping[str, Any]) -> date | None:
    try:
        return date.fromisoformat(str(leg.get("date")))
    except (TypeError, ValueError):
        return None


def stays_of_legs(legs: Sequence[Mapping[str, Any]]) -> list[Stay]:
    """Die Aufenthalte einer Kandidatenzeile, aus ihren Legs.

    Ort ist das Ziel des vorherigen Legs, Anreise sein Datum, Naechte die
    Differenz zum naechsten. Beim letzten Leg endet die Reise, dort gibt es
    keinen Aufenthalt; ein Einweg-Flug hat deshalb gar keinen. Faellt der
    Anschluss auf denselben Tag, wird auch nicht uebernachtet.
    """
    rows = [leg for leg in legs or () if isinstance(leg, Mapping)]
    out: list[Stay] = []
    for here, following in zip(rows, rows[1:]):
        arrival, leaves = _leg_date(here), _leg_date(following)
        if arrival is None or leaves is None:
            continue
        nights = (leaves - arrival).days
        if nights <= 0:
            continue
        code = str(here.get("destination") or "").strip().upper()
        if not code:
            continue
        out.append(Stay(code=code, city=city_of(code), arrival=arrival, nights=nights))
    return out


def unique_stays(stays: Sequence[Stay]) -> list[Stay]:
    """Jedes Tripel genau einmal, in der Reihenfolge des ersten Auftretens."""
    seen: dict[tuple[str, str, int], Stay] = {}
    for stay in stays:
        seen.setdefault(stay.key, stay)
    return list(seen.values())


def stays_of_rows(rows: Sequence[Mapping[str, Any]]) -> list[Stay]:
    """Alle Aufenthalte der Bestenliste, entdoppelt."""
    found: list[Stay] = []
    for row in rows:
        found.extend(stays_of_legs(row.get("legs") or []))
    return unique_stays(found)


def query_for(stay: Stay, options: StayOptions) -> HotelQuery:
    return HotelQuery(
        destination=stay.city,
        arrival=stay.arrival,
        nights=stay.nights,
        adults=max(1, int(options.adults)),
        rooms=max(1, int(options.rooms)),
        country=options.country,
        currency=options.currency,
    )


def _fan_width(sources: Sequence[HotelSource]) -> int:
    """So breit wie die nebenlaeufigste Quelle, wie im Zeitraum-Durchlauf."""
    return max(
        [1] + [int(getattr(source.limiter, "concurrency", 1) or 1) for source in sources]
    )


def _keep_cheapest(quote: StayQuote, stay: Stay, offer: HotelOffer,
                   currency: str) -> None:
    """Nur der guenstigste Treffer zaehlt, und nur in der gefragten Waehrung.

    Ein Betrag in fremder Waehrung wuerde ungerechnet zur Flugsumme addiert und
    aus 28700 JPY ein Aufschlag von 287 Euro machen. Also lieber keine Zahl.
    """
    price = offer.price
    if price.minor <= 0:
        return
    if price.currency != currency:
        quote.notes.append(
            f"{stay.city} {stay.arrival}: {offer.source} rechnet in "
            f"{price.currency}, nicht in {currency}"
        )
        return
    current = quote.prices.get(stay.key)
    if current is None or price.minor < current.minor:
        quote.prices[stay.key] = StayPrice(
            minor=price.minor,
            currency=price.currency,
            source=offer.source,
            name=offer.name,
        )


async def price_stays(
    stays: Sequence[Stay],
    sources: Sequence[HotelSource],
    *,
    options: StayOptions | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    should_stop: Callable[[], None] | None = None,
) -> StayQuote:
    """Je Aufenthalt den guenstigsten Preis holen. Abbrechbar wie der Rest.

    Gefragt werden nur die entdoppelten Tripel, und zwar in Fenstern, die so
    breit sind wie die nebenlaeufigste Quelle. Zwischen zwei Fenstern greift
    `should_stop`: ein halb fertiges Fenster ist billiger als ein hart
    abgerissener Abruf.
    """
    settings = options or StayOptions()
    quote = StayQuote()
    wanted = unique_stays(stays)
    if not wanted or not sources:
        return quote

    budget = max(0, int(settings.budget))
    if len(wanted) > budget:
        quote.skipped = wanted[budget:]
        quote.notes.append(
            f"{len(wanted)} Aufenthalte, abgefragt werden {budget}. "
            f"Die uebrigen Zeilen bleiben ohne Gesamtsumme."
        )
        wanted = wanted[:budget]
    if not wanted:
        return quote

    total = len(wanted)
    width = _fan_width(sources)
    done = 0
    for start in range(0, total, width):
        if should_stop is not None:
            should_stop()
        window = wanted[start : start + width]
        queries = [query_for(stay, settings) for stay in window]
        quote.calls += len(queries) * len(sources)
        answers = await asyncio.gather(
            *(source.search_many(queries) for source in sources),
            return_exceptions=True,
        )
        for source, answer in zip(sources, answers):
            if isinstance(answer, BaseException):
                logger.warning("stays: %s scheiterte: %s", source.name, answer)
                quote.notes.append(f"{source.name}: {answer}")
                continue
            for stay, result in zip(window, answer):
                if result.error:
                    quote.notes.append(
                        f"{stay.city} {stay.arrival}: {source.name}: {result.error}"
                    )
                for offer in result.offers:
                    _keep_cheapest(quote, stay, offer, settings.currency)
        done += len(window)
        quote.asked = done
        if on_progress is not None:
            on_progress(done, total)
        # Noch einmal nach der Meldung: wer waehrenddessen abgebrochen hat,
        # soll nicht noch ein ganzes Fenster abgerufen bekommen.
        if should_stop is not None:
            should_stop()
    return quote


def apply_stay_costs(rows: Sequence[dict[str, Any]], quote: StayQuote, *,
                     currency: str = "EUR") -> list[dict[str, Any]]:
    """Aufenthalte und Gesamtsumme an die Zeilen haengen.

    Fehlt auch nur ein Preis, bleibt `grand_total` leer. Die Aufenthalte selbst
    stehen trotzdem in der Zeile: wer die Luecke sieht, versteht die leere
    Summe.
    """
    for row in rows:
        stays = stays_of_legs(row.get("legs") or [])
        detail: list[dict[str, Any]] = []
        stay_minor = 0
        complete = str(row.get("currency") or currency) == currency
        for stay in stays:
            item = stay.as_dict()
            price = quote.prices.get(stay.key)
            if price is None:
                complete = False
                item["price"] = None
                item["source"] = None
            else:
                stay_minor += price.minor
                item["price"] = price.minor / 100
                item["source"] = price.source
                item["name"] = price.name
            detail.append(item)
        row["stays"] = detail
        row["stay_total"] = stay_minor / 100 if complete else None
        row["grand_total"] = (
            round(float(row.get("total") or 0) + stay_minor / 100, 2)
            if complete
            else None
        )
    return list(rows)
