"""Ryanair fare finder.

The only source that answered cleanly in the P0 probe, and the only way to see
Ryanair fares at all: Google Flights does not carry them. One call returns a
whole month of cheapest-per-day fares, which is exactly the shape the date
optimizer wants.

Verified live 2026-09-04 from a German residential IP:
    GET /farfnd/v4/oneWayFares/BER/ATH/cheapestPerDay?outboundMonthOfDate=2026-10-01
    -> 200, 10 priced days, cheapest 46.97 EUR
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from flightopt.domain.models import Cabin, Money, Offer, Pax, Segment
from flightopt.sources.base import HttpSource, SourceError

logger = logging.getLogger(__name__)

API = "https://services-api.ryanair.com"
WEB = "https://www.ryanair.com/api"
"""Route metadata lives on the website host, fares on the services host."""


class RyanairSource(HttpSource):
    name = "ryanair"
    carrier = "FR"
    supports_calendar = True
    supports_search = True
    per_minute = 20

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self._routes: dict[str, set[str]] | None = None

    # -- routes ---------------------------------------------------------------

    async def load_routes(self, origin: str) -> set[str]:
        """Destinations Ryanair actually serves from `origin`.

        Without this the optimizer would spend calls on routes that cannot
        exist, and a missing route is indistinguishable from a sold-out month.
        """
        if self._routes is None:
            self._routes = {}
        if origin in self._routes:
            return self._routes[origin]

        url = f"{WEB}/views/locate/searchWidget/routes/en/airport/{origin}"
        try:
            data = await self.fetch_json(url)
        except SourceError as exc:
            logger.warning("ryanair: route lookup for %s failed: %s", origin, exc)
            self._routes[origin] = set()
            return set()

        dests: set[str] = set()
        for entry in data or []:
            arrival = entry.get("arrivalAirport") or {}
            # The widget endpoint calls it "code"; other endpoints use "iataCode".
            code = arrival.get("code") or arrival.get("iataCode")
            if code:
                dests.add(code)
        self._routes[origin] = dests
        logger.info("ryanair: %s serves %d destinations", origin, len(dests))
        return dests

    def supports_route(self, origin: str, destination: str) -> bool:
        if self._routes is None or origin not in self._routes:
            return True  # unknown until load_routes ran; let the call decide
        return destination in self._routes[origin]

    # -- prices ---------------------------------------------------------------

    async def calendar(
        self, origin: str, destination: str, month: date, *, currency: str = "EUR"
    ) -> dict[date, Money]:
        """Cheapest fare per day for the calendar month containing `month`."""
        first = month.replace(day=1)
        url = (
            f"{API}/farfnd/v4/oneWayFares/{origin}/{destination}/cheapestPerDay"
            f"?outboundMonthOfDate={first.isoformat()}&currency={currency}"
        )
        data = await self.fetch_json(url)
        out: dict[date, Money] = {}
        for fare in (data.get("outbound") or {}).get("fares") or []:
            if fare.get("soldOut") or fare.get("unavailable"):
                continue
            price = fare.get("price")
            day = fare.get("day")
            if not price or not day:
                continue
            value = price.get("value")
            if value is None:
                continue
            out[date.fromisoformat(day)] = Money.from_major(
                float(value), price.get("currencyCode", currency)
            )
        return out

    async def calendar_range(
        self, origin: str, destination: str, start: date, end: date, *, currency: str = "EUR"
    ) -> dict[date, Money]:
        """Cover an arbitrary window by walking the months it spans."""
        out: dict[date, Money] = {}
        cursor = start.replace(day=1)
        while cursor <= end:
            try:
                month_prices = await self.calendar(origin, destination, cursor, currency=currency)
            except SourceError as exc:
                logger.warning(
                    "ryanair: %s-%s %s failed: %s", origin, destination, cursor, exc
                )
                month_prices = {}
            out.update({d: m for d, m in month_prices.items() if start <= d <= end})
            cursor = (cursor + timedelta(days=32)).replace(day=1)
        return out

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
        """Actual departures for one date, with times and flight numbers."""
        url = (
            f"{API}/farfnd/v4/oneWayFares"
            f"?departureAirportIataCode={origin}"
            f"&arrivalAirportIataCode={destination}"
            f"&outboundDepartureDateFrom={day.isoformat()}"
            f"&outboundDepartureDateTo={day.isoformat()}"
            f"&currency={currency}&limit=30&offset=0"
        )
        data = await self.fetch_json(url)
        offers: list[Offer] = []
        for fare in data.get("fares") or []:
            outbound = fare.get("outbound") or {}
            price = outbound.get("price") or {}
            value = price.get("value")
            if value is None:
                continue
            dep_raw = outbound.get("departureDate")
            arr_raw = outbound.get("arrivalDate")
            if not dep_raw or not arr_raw:
                continue
            departure = datetime.fromisoformat(dep_raw)
            arrival = datetime.fromisoformat(arr_raw)
            flight_no = (outbound.get("flightNumber") or "FR").replace(" ", "")
            segment = Segment(
                carrier="FR",
                flight_number=flight_no,
                origin=(outbound.get("departureAirport") or {}).get("iataCode", origin),
                destination=(outbound.get("arrivalAirport") or {}).get("iataCode", destination),
                departure=departure,
                arrival=arrival,
            )
            offers.append(
                Offer(
                    source=self.name,
                    origin=origin,
                    destination=destination,
                    travel_date=departure.date(),
                    price=Money.from_major(
                        float(value), price.get("currencyCode", currency)
                    ),
                    segments=(segment,),
                    deep_link=(
                        f"https://www.ryanair.com/gb/en/trip/flights/select"
                        f"?adults={pax.adults}&dateOut={day.isoformat()}"
                        f"&originIata={origin}&destinationIata={destination}"
                    ),
                    is_estimate=False,
                )
            )
        offers.sort(key=lambda o: o.price.minor)
        return offers
