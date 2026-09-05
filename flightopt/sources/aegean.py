"""Aegean low fare calendar.

Aegean's website exposes a month-at-a-time cheapest-fare endpoint that answers
without a login or token. It needs a browser TLS fingerprint; a plain HTTP
client is refused outright, which is why every request goes through curl_cffi.

Dates arrive as the old Microsoft JSON format, double-escaped:
    "\\"\\\\/Date(1793491200000)\\\\/\\""
so the timestamp is pulled out with a regex rather than parsed as JSON.

Verified live 2026-09-04 from a German residential IP:
    GET .../sys/lowfares/RouteLowFares/?DepartureAirport=ATH&ArrivalAirport=SKG
    -> 200, priced days for the month
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone

from flightopt.domain.models import Cabin, Money, Offer, Pax
from flightopt.sources.base import HttpSource, SourceError

logger = logging.getLogger(__name__)

ENDPOINT = "https://en.aegeanair.com/en/sys/lowfares/RouteLowFares/"
MS_DATE = re.compile(r"/Date\((-?\d+)")


def parse_ms_date(raw: str | None) -> date | None:
    """Pull a date out of Microsoft's `/Date(1793491200000)/` wrapper."""
    if not raw:
        return None
    match = MS_DATE.search(raw)
    if not match:
        return None
    seconds = int(match.group(1)) / 1000
    return datetime.fromtimestamp(seconds, tz=timezone.utc).date()


class AegeanSource(HttpSource):
    name = "aegean"
    carrier = "A3"
    supports_calendar = True
    supports_search = True
    """The endpoint prices days rather than flights, so an offer from here has a
    price and a booking link but no times. That is still worth confirming: a
    result Aegean won would otherwise reach the user with no way to book it."""
    per_minute = 15
    impersonate = "chrome124"

    async def calendar(
        self, origin: str, destination: str, month: date, *, currency: str = "EUR"
    ) -> dict[date, Money]:
        params = {
            "DepartureAirport": origin,
            "ArrivalAirport": destination,
            "TripType": "O",
            "DepartureDate": f"{month.year:04d}-{month.month:02d}",
            "ReturnDate": "",
            "Type": "Fares",
        }
        data = await self.fetch_json(
            ENDPOINT, params=params, headers={"X-Requested-With": "XMLHttpRequest"}
        )
        out: dict[date, Money] = {}
        for row in data.get("Outbound") or []:
            if row.get("Error"):
                continue
            price = row.get("Price")
            if price is None:
                price = row.get("FullPrice")
            day = parse_ms_date(row.get("Date"))
            if price is None or day is None:
                continue
            total = float(price) + float(row.get("ServiceFee") or 0)
            money = Money.from_major(total, currency)
            if day not in out or money.minor < out[day].minor:
                out[day] = money
        return out

    async def calendar_range(
        self, origin: str, destination: str, start: date, end: date, *, currency: str = "EUR"
    ) -> dict[date, Money]:
        out: dict[date, Money] = {}
        cursor = start.replace(day=1)
        while cursor <= end:
            try:
                month = await self.calendar(origin, destination, cursor, currency=currency)
            except SourceError as exc:
                logger.warning(
                    "aegean: %s-%s %s failed: %s", origin, destination, cursor, exc
                )
                month = {}
            out.update({d: m for d, m in month.items() if start <= d <= end})
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
        """The day's fare with a link to Aegean's own booking page.

        There are no segments to report, so the itinerary shows a price and a
        way to book while stating plainly that no flight times are known.
        """
        prices = await self.calendar(origin, destination, day, currency=currency)
        money = prices.get(day)
        if money is None:
            return []
        return [
            Offer(
                source=self.name,
                origin=origin,
                destination=destination,
                travel_date=day,
                price=money,
                segments=(),
                deep_link=(
                    "https://en.aegeanair.com/booking/availability/"
                    f"?adults={pax.adults}&departureAirport={origin}"
                    f"&arrivalAirport={destination}&departureDate={day.isoformat()}"
                    "&tripType=O"
                ),
                is_estimate=False,
            )
        ]
