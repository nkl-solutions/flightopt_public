"""British Airways low price finder.

BA leaves the search index behind its "Low Price Finder" open: a plain Solr
core, no key, no cookie. One query returns a month of lowest fares per
departure date.

Two things the payload does not tell you:
  - the city fields want CITY codes, not airport codes. `arrival_city:LHR`
    finds nothing; `LON` finds Heathrow, Gatwick and City. The airport is in
    the answer, not the question.
  - coverage is limited to pairs BA actually markets from that departure
    market. Berlin to Athens returns zero, which is correct rather than broken.

Verified live 2026-09-04: BER-LON one-way November returned 60 priced dates.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from flightopt.domain.models import Money
from flightopt.sources.base import HttpSource, SourceError

logger = logging.getLogger(__name__)

ENDPOINT = "https://www.britishairways.com/solr/lpbd/lpbdcalendar"

# Airports whose city code differs from the airport code. Anything absent is
# passed through unchanged, which is right for the many one-airport cities.
CITY_OF = {
    "LHR": "LON", "LGW": "LON", "LCY": "LON", "STN": "LON", "LTN": "LON",
    "JFK": "NYC", "EWR": "NYC", "LGA": "NYC",
    "CDG": "PAR", "ORY": "PAR", "BVA": "PAR",
    "FCO": "ROM", "CIA": "ROM",
    "MXP": "MIL", "LIN": "MIL", "BGY": "MIL",
    "HND": "TYO", "NRT": "TYO",
    "ORD": "CHI", "MDW": "CHI",
    "IAD": "WAS", "DCA": "WAS", "BWI": "WAS",
    "SVO": "MOW", "DME": "MOW",
    "GRU": "SAO", "CGH": "SAO",
    "YYZ": "YTO", "YTZ": "YTO",
    "TXL": "BER", "SXF": "BER",
    "SAW": "IST",
    "BCN": "BCN", "MAD": "MAD",
}


def city_code(airport: str) -> str:
    return CITY_OF.get(airport.upper(), airport.upper())


class BritishAirwaysSource(HttpSource):
    name = "britishairways"
    carrier = "BA"
    supports_calendar = True
    supports_search = False
    """Lowest price per date; no flight numbers or times in this index."""
    per_minute = 15

    async def calendar(
        self, origin: str, destination: str, month: date, *, currency: str = "EUR"
    ) -> dict[date, Money]:
        params = [
            ("fq", f"month_year:({month.year:04d}{month.month:02d})"),
            ("fq", "number_of_nights:0"),  # 0 nights means one-way
            ("fq", f"departure_city:{city_code(origin)}"),
            ("fq", f"arrival_city:{city_code(destination)}"),
            ("fq", "cabin:M"),
            ("fq", "trip_type:OW"),
            ("fq", f"currency_code:{currency.upper()}"),
            ("fl", "outbound_date,lowest_price,currency_code,departure_airport,arrival_airport"),
            ("wt", "json"),
            ("rows", "80"),
        ]
        data = await self.fetch_json(ENDPOINT, params=params)

        docs = ((data or {}).get("response") or {}).get("docs") or []
        out: dict[date, Money] = {}
        for doc in docs:
            raw = doc.get("outbound_date")
            price = doc.get("lowest_price")
            if not raw or price is None:
                continue
            got = (doc.get("currency_code") or currency).upper()
            if got != currency.upper():
                continue
            try:
                day = datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
            except ValueError:
                continue
            money = Money.from_major(float(price), got)
            if day not in out or money.minor < out[day].minor:
                out[day] = money
        return out

    async def calendar_range(
        self, origin: str, destination: str, start: date, end: date, *, currency: str = "EUR"
    ) -> dict[date, Money]:
        out: dict[date, Money] = {}
        cursor = start.replace(day=1)
        guard = 0
        while cursor <= end and guard < 24:
            guard += 1
            try:
                month = await self.calendar(origin, destination, cursor, currency=currency)
            except SourceError as exc:
                logger.warning(
                    "britishairways: %s-%s %s failed: %s", origin, destination, cursor, exc
                )
                month = {}
            out.update({d: m for d, m in month.items() if start <= d <= end})
            cursor = (cursor + timedelta(days=32)).replace(day=1)
        return out
