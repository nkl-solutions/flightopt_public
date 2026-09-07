"""Google Flights through SerpApi: the verify source for the rest of the world.

Google Flights itself stays out of scope (see docs/AIRLINE_PLAN.md). SerpApi is
a paid, documented API with terms that allow this, which is a different thing
from scraping the site.

Used only where no airline adapter serves the route, because the free tier is
250 searches per month. The budget guard in `storage/budget.py` enforces that.

Offen: ein Airline-Filter wirft SerpApi aktuell aus dem Katalog, weil der
Adapter keinen Carrier fuehrt (siehe `jobs/runner.build_catalogue`). Die API
kann serverseitig nach Airline filtern (`include_airlines`), also ist das eine
fehlende Durchreichung, keine Grenze der Quelle. Bis dahin ist eine gefilterte
Suche ausserhalb Europas ohne Preisquelle.

Response shape (see tests/fixtures/serpapi_google_flights_BER_NRT.json):
    best_flights[] / other_flights[]
      flights[]  -> departure_airport {id, time}, arrival_airport {id, time},
                    airline, flight_number ("TK 1724"), travel_class
      layovers[] -> duration, name, id
      price      -> whole number in the requested currency
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Mapping

from flightopt.domain.models import Cabin, Money, Offer, Pax, Segment
from flightopt.sources.base import HttpSource, SourceBlocked, SourceError
from flightopt.storage.budget import MonthlyBudget

logger = logging.getLogger(__name__)

ENDPOINT = "https://serpapi.com/search.json"
DEFAULT_CAP = 200
"""Below SerpApi's 250 free searches, so a miscount never costs money."""

TIME_FORMAT = "%Y-%m-%d %H:%M"

CABIN_CLASS = {
    Cabin.ECONOMY: 1,
    Cabin.PREMIUM_ECONOMY: 2,
    Cabin.BUSINESS: 3,
    Cabin.FIRST: 4,
}


AIRLINE_CODES = {
    "turkish airlines": "TK",
    "lufthansa": "LH",
    "air france": "AF",
    "klm": "KL",
    "british airways": "BA",
    "emirates": "EK",
    "qatar airways": "QR",
    "finnair": "AY",
    "japan airlines": "JL",
    "all nippon airways": "NH",
    "korean air": "KE",
}
"""Only names whose code is beyond doubt. Anything else keeps its name: a
guessed code would filter and label the wrong airline."""


def carrier_code(flight_number: str) -> str:
    """'TK 1724' -> 'TK'. The payload names the airline, not its code."""
    token = (flight_number or "").strip().split(" ")[0]
    return token[:2].upper()


def carrier_of(leg: Mapping[str, Any]) -> str:
    """The flight number's code, or the airline name when there is no number.

    Some itineraries arrive without a number (code-shares, sold-out legs).
    Dropping them would leave a route unpriced for a missing label, so the
    airline name carries the segment instead.
    """
    number = str(leg.get("flight_number") or "").strip()
    if number:
        return carrier_code(number)
    airline = str(leg.get("airline") or "").strip()
    return AIRLINE_CODES.get(airline.casefold(), airline)


def _time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, TIME_FORMAT)
    except ValueError:
        return None


def _segments(flights: list[dict]) -> tuple[Segment, ...]:
    out: list[Segment] = []
    for leg in flights:
        departure = _time((leg.get("departure_airport") or {}).get("time"))
        arrival = _time((leg.get("arrival_airport") or {}).get("time"))
        number = str(leg.get("flight_number") or "").strip()
        carrier = carrier_of(leg)
        if departure is None or arrival is None or not carrier:
            return ()
        out.append(
            Segment(
                # Without a number the carrier stands in, the way the Wizz
                # adapter does it: a made-up number would look bookable.
                carrier=carrier,
                flight_number=number or carrier,
                origin=(leg.get("departure_airport") or {}).get("id", ""),
                destination=(leg.get("arrival_airport") or {}).get("id", ""),
                departure=departure,
                arrival=arrival,
            )
        )
    return tuple(out)


def parse_offers(
    data: dict,
    origin: str,
    destination: str,
    day: date,
    *,
    currency: str = "EUR",
    source_name: str = "serpapi",
) -> list[Offer]:
    """Map both result buckets onto offers, cheapest first."""
    link = (data.get("search_metadata") or {}).get("google_flights_url")
    offers: list[Offer] = []
    for bucket in ("best_flights", "other_flights"):
        for row in data.get(bucket) or []:
            price = row.get("price")
            segments = _segments(row.get("flights") or [])
            if price is None or not segments:
                continue
            offers.append(
                Offer(
                    source=source_name,
                    origin=origin,
                    destination=destination,
                    travel_date=day,
                    price=Money.from_major(float(price), currency.upper()),
                    segments=segments,
                    deep_link=link,
                    is_estimate=False,
                )
            )
    offers.sort(key=lambda o: o.price.minor)
    return offers


class SerpApiGoogleFlights(HttpSource):
    name = "serpapi"
    carrier = ""
    """A search engine, not an airline: it must never claim a tail."""
    supports_calendar = False
    supports_search = True
    indicative = False
    per_minute = 10

    def __init__(self, *, api_key: str, budget: MonthlyBudget | None = None, **kw) -> None:
        super().__init__(**kw)
        self.api_key = api_key
        self.budget = budget

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
        if self.breaker.is_open:
            # Vor dem Budget: ein Abruf, der die Sicherung ohnehin nicht
            # passiert, darf keinen der bezahlten Monatsabrufe kosten.
            raise SourceBlocked(
                f"{self.name}: circuit open, {self.breaker.remaining():.0f}s left"
            )
        if self.budget is not None and not self.budget.consume():
            raise SourceBlocked(
                f"serpapi: Budget von {self.budget.cap} Abrufen fuer diesen Monat "
                f"aufgebraucht"
            )
        params: dict[str, Any] = {
            "engine": "google_flights",
            "departure_id": origin,
            "arrival_id": destination,
            "outbound_date": day.isoformat(),
            "type": "2",  # one way
            "travel_class": CABIN_CLASS.get(cabin, 1),
            "adults": pax.adults,
            "currency": currency.upper(),
            "hl": "de",
            "gl": "de",
            "api_key": self.api_key,
        }
        if pax.children:
            params["children"] = pax.children
        if pax.infants:
            params["infants_in_seat"] = pax.infants

        # One attempt: every call is booked against the monthly budget before
        # it leaves, so a retry inside the HTTP layer would spend quota the
        # guard already counted.
        data = await self.fetch_json(ENDPOINT, params=params, retries=1)
        error = str(data.get("error") or "")
        if error:
            if "hasn't returned any results" in error:
                # A route with no flights on that day is an answer, not a
                # failure; raising would knock the whole leg out of the search.
                logger.info("serpapi: %s-%s am %s ohne Ergebnis", origin, destination, day)
                return []
            raise SourceError(f"serpapi: {error}")
        status = str((data.get("search_metadata") or {}).get("status") or "")
        if status and status.casefold() != "success":
            raise SourceError(f"serpapi: Suche endete mit Status {status}")
        return parse_offers(
            data, origin, destination, day,
            currency=currency, source_name=self.name,
        )


def from_env(env: Mapping[str, str], *, conn: Any) -> SerpApiGoogleFlights | None:
    """Build the adapter, or None when no key is configured.

    Without a key the source must not appear in the catalogue at all, so a
    missing key is a quiet absence rather than a failing search.

    `conn` is required and must be the connection the job runs on: the monthly
    budget lives in that database, and an adapter built without one would
    happily spend the paid quota unmetered. Passing None with a key set is
    therefore an error, not a fallback.
    """
    key = (env.get("SERPAPI_KEY") or "").strip()
    if not key:
        return None
    if conn is None:
        raise ValueError(
            "serpapi: conn fehlt - ohne Datenbank gibt es kein Monatsbudget"
        )
    raw_cap = (env.get("SERPAPI_MONTHLY_CAP") or "").strip()
    try:
        cap = int(raw_cap) if raw_cap else DEFAULT_CAP
    except ValueError:
        logger.warning("SERPAPI_MONTHLY_CAP=%r ist keine Zahl, nutze %d",
                       raw_cap, DEFAULT_CAP)
        cap = DEFAULT_CAP
    return SerpApiGoogleFlights(api_key=key, budget=MonthlyBudget(conn, "serpapi", cap))
