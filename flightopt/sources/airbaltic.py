"""airBaltic flight search finder: a cheapest fare per day, a year ahead.

The date picker on www.airbaltic.com/en/book-flight is fed by /api/fsf/outbound.
It answers without a key, a token or a session, and one call covers roughly
twelve months, so a whole grid row costs a single request.

Three things the payload does not advertise:
  - with `flightMode=oneway` the date range is ignored: the answer always starts
    today and runs about 358 days. Asking for a window only narrows what we keep,
    not what the server sends.
  - `price` is null for days without a bookable fare, and today's own row is
    normally null. Those days are dropped, not priced as zero.
  - there is no currency field and no currency parameter. The endpoint answers in
    the currency of the point of sale, which is EUR for the /en/ market from
    Europe. The value is therefore reported as EUR and the grid converts.

`flightMode=return` exists and is cheaper, but those are return-trip fares split
over two legs; a one-way grid must not quote them.

Verified live 2026-09-07: BER-RIX returned 358 days.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from flightopt.domain.models import Money
from flightopt.sources.base import HttpSource, SourceError

logger = logging.getLogger(__name__)

ENDPOINT = "https://www.airbaltic.com/api/fsf/outbound"
REFERER = "https://www.airbaltic.com/en/index"
NATIVE_CURRENCY = "EUR"
"""The /en/ market prices in EUR and the payload carries no currency at all."""
WINDOW_DAYS = 358
"""One call covers about twelve months, counted from today."""


class AirBalticSource(HttpSource):
    name = "airbaltic"
    carrier = "BT"
    supports_calendar = True
    supports_search = False
    """The rows carry a date and a price, no flight numbers and no times."""
    per_minute = 15

    async def calendar_range(
        self, origin: str, destination: str, start: date, end: date, *, currency: str = "EUR"
    ) -> dict[date, Money]:
        params = {
            "flightMode": "oneway",
            "origin": origin,
            "destin": destination,
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
        }
        try:
            data = await self.fetch_json(
                ENDPOINT,
                params=params,
                headers={"Referer": REFERER, "Accept": "*/*"},
            )
        except SourceError as exc:
            logger.warning("airbaltic: %s-%s failed: %s", origin, destination, exc)
            return {}

        if not isinstance(data, dict) or data.get("success") is not True:
            return {}

        out: dict[date, Money] = {}
        for row in data.get("data") or []:
            if not isinstance(row, dict):
                continue
            price = row.get("updatedPrice")
            if price is None:
                price = row.get("price")
            if price is None:
                # No bookable fare that day, and today's own row is always null.
                continue
            try:
                day = date.fromisoformat(str(row.get("date")))
                money = Money.from_major(float(price), NATIVE_CURRENCY)
            except (TypeError, ValueError):
                continue
            if money.minor <= 0 or not (start <= day <= end):
                continue
            if day not in out or money.minor < out[day].minor:
                out[day] = money
        return out

    async def calendar(
        self, origin: str, destination: str, month: date, *, currency: str = "EUR"
    ) -> dict[date, Money]:
        first = month.replace(day=1)
        nxt = (first + timedelta(days=32)).replace(day=1)
        return await self.calendar_range(
            origin, destination, first, nxt - timedelta(days=1), currency=currency
        )
