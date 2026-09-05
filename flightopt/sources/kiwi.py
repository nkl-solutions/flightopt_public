"""Kiwi price calendar.

The one source that covers routes no single airline can: it answers for any
airport pair, returns roughly three months of daily prices per request, and can
be narrowed to one carrier. That makes it the fallback for airlines whose own
sites are closed to us, and the only way to price Berlin to Antalya today.

The catch, measured against Ryanair's own fares on the same days: Kiwi's number
runs about 13 percent high (median 1.134, range 1.12 to 1.28). Every offer from
here is therefore marked as an estimate, so it can rank and shortlist but never
reaches the user as a final price.

Verified live 2026-09-04:
    POST api.skypicker.com/umbrella/v2/graphql  BER-AYT
    -> 91 priced days in one request
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from flightopt.domain.models import Money
from flightopt.sources.base import HttpSource, SourceError

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.skypicker.com/umbrella/v2/graphql?featureName=PriceCalendarQuery"

# Kiwi answers for windows far longer than three months, but the calendar it
# returns is capped, so long searches are walked in chunks.
CHUNK_DAYS = 90

# Ryanair fares on the same days ran this much below Kiwi's number. Recorded so
# the bias is visible in code rather than folklore; prices are not adjusted by
# it, because a corrected estimate is still an estimate.
OBSERVED_MARKUP = 1.134

QUERY = """
query PriceCalendarQuery(
  $search: SearchPricesCalendarInput
  $filter: ItinerariesFilterInput
  $options: ItinerariesOptionsInput
) {
  itineraryPricesCalendar(search: $search, filter: $filter, options: $options) {
    __typename
    ... on ItineraryPricesCalendar {
      currency { code }
      calendar { date ratedPrice { rating price { amount currency { code } } } }
    }
    ... on AppError { code message }
  }
}
"""


class KiwiSource(HttpSource):
    name = "kiwi"
    carrier = ""
    """Not an airline. Left blank so it never claims a tail in the results."""
    supports_calendar = True
    supports_search = False
    """Prices here are indicative, so they are never used to confirm a booking."""
    indicative = True
    per_minute = 20

    def __init__(self, *, carriers: list[str] | None = None, max_stops: int = 1, **kw) -> None:
        """Allows one connection by default.

        This is the fallback for pairs no airline serves directly: Antalya to
        Athens has no non-stop service at all, so a direct-only filter returns
        an empty calendar and the whole search fails on that leg.
        """
        super().__init__(**kw)
        self.carrier_filter = [c.upper() for c in (carriers or [])]
        self.max_stops = max_stops

    async def _calendar_chunk(
        self, origin: str, destination: str, start: date, end: date, currency: str
    ) -> dict[date, Money]:
        filt: dict = {
            "maxStopsCount": self.max_stops,
            "transportTypes": ["FLIGHT"],
            "contentProviders": ["KIWI"],
        }
        if self.carrier_filter:
            filt["carriers"] = self.carrier_filter

        variables = {
            "search": {
                "source": {"ids": [f"Station:airport:{origin}"]},
                "destination": {"ids": [f"Station:airport:{destination}"]},
                "dates": {
                    "start": f"{start.isoformat()}T00:00:00",
                    "end": f"{end.isoformat()}T00:00:00",
                },
                "passengers": {"adults": 1, "children": 0, "infants": 0},
                "cabinClass": {"cabinClass": "ECONOMY", "applyMixedClasses": False},
            },
            "filter": filt,
            "options": {
                "currency": currency.lower(),
                "locale": "en",
                "partner": "skypicker",
                "partnerMarket": "de",
                "market": "de",
            },
        }
        data = await self.fetch_json(
            ENDPOINT,
            json_body={"query": QUERY, "variables": variables},
            headers={"Content-Type": "application/json"},
        )

        if data.get("errors"):
            first = data["errors"][0].get("message", "unknown")
            raise SourceError(f"kiwi: {first}")

        node = ((data.get("data") or {}).get("itineraryPricesCalendar")) or {}
        kind = node.get("__typename")
        if kind == "AppError":
            raise SourceError(f"kiwi: {node.get('code')}: {node.get('message')}")
        if kind != "ItineraryPricesCalendar":
            raise SourceError(f"kiwi: unexpected response {kind!r}")

        out: dict[date, Money] = {}
        for row in node.get("calendar") or []:
            rated = row.get("ratedPrice")
            raw_date = row.get("date")
            if not rated or not raw_date:
                continue
            price = rated.get("price") or {}
            amount = price.get("amount")
            if amount is None:
                continue
            got = ((price.get("currency") or {}).get("code") or currency).upper()
            if got != currency.upper():
                continue
            day = datetime.fromisoformat(raw_date).date()
            # The amount arrives as a string, so a plain int() would crash on
            # any route that ever returns a fractional fare.
            out[day] = Money.from_major(float(amount), got)
        return out

    async def calendar_range(
        self, origin: str, destination: str, start: date, end: date, *, currency: str = "EUR"
    ) -> dict[date, Money]:
        out: dict[date, Money] = {}
        cursor = start
        while cursor <= end:
            chunk_end = min(end, cursor + timedelta(days=CHUNK_DAYS - 1))
            try:
                chunk = await self._calendar_chunk(
                    origin, destination, cursor, chunk_end, currency
                )
            except SourceError as exc:
                logger.warning(
                    "kiwi: %s-%s %s..%s failed: %s",
                    origin, destination, cursor, chunk_end, exc,
                )
                chunk = {}
            out.update({d: m for d, m in chunk.items() if start <= d <= end})
            cursor = chunk_end + timedelta(days=1)
        return out

    async def calendar(
        self, origin: str, destination: str, month: date, *, currency: str = "EUR"
    ) -> dict[date, Money]:
        first = month.replace(day=1)
        nxt = (first + timedelta(days=32)).replace(day=1)
        return await self.calendar_range(
            origin, destination, first, nxt - timedelta(days=1), currency=currency
        )
