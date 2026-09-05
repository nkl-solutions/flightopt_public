"""Condor and Marabu low fare calendar.

Both airlines run on the same booking engine, so one adapter prices both. They
matter because they fly Germany to Greece and Turkey, which is exactly where
Ryanair and Wizz do not go.

Two quirks the payload does not advertise:
  - prices are in minor units, so 25199 means 251.99 EUR
  - `outboundDate` is an anchor, not the start of the window: asking for
    1 October with 31 days returned 2 October to 15 November

Verified live 2026-09-04.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from flightopt.domain.models import Money
from flightopt.sources.base import HttpSource, SourceError

logger = logging.getLogger(__name__)

ENDPOINT = "https://www.condor.com/tca/rest/de/vacancies/lowFareInformation"
DAYS_PER_CALL = 31


class CondorSource(HttpSource):
    name = "condor"
    carrier = "DE"
    also_carriers = ("DI",)
    """Marabu sells through the same booking engine as Condor."""
    supports_calendar = True
    supports_search = False
    """The endpoint prices days, not flights; no times or numbers are returned."""
    per_minute = 25

    async def calendar(
        self, origin: str, destination: str, month: date, *, currency: str = "EUR"
    ) -> dict[date, Money]:
        anchor = month.replace(day=1)
        params = {
            "origin": origin,
            "destination": destination,
            "outboundDate": anchor.strftime("%Y%m%d"),
            "numberOfFlightDays": DAYS_PER_CALL,
            "currency": currency,
            "oneway": "true",
            "adults": 1,
            "isOutbound": "true",
        }
        data = await self.fetch_json(
            ENDPOINT, params=params, headers={"Referer": "https://www.condor.com/de/"}
        )

        rows = data.get("data") or []
        # The payload nests one list per requested leg; flatten defensively so a
        # shape change produces no prices rather than a crash.
        flat: list[dict] = []
        for entry in rows:
            if isinstance(entry, list):
                flat.extend(x for x in entry if isinstance(x, dict))
            elif isinstance(entry, dict):
                flat.append(entry)

        out: dict[date, Money] = {}
        for row in flat:
            raw_day = row.get("date")
            raw_price = row.get("price")
            if not raw_day or raw_price is None:
                continue
            if row.get("offer") is False:
                continue
            try:
                day = datetime.strptime(str(raw_day), "%Y%m%d").date()
            except ValueError:
                continue
            money = Money(int(raw_price), currency)  # already minor units
            if day not in out or money.minor < out[day].minor:
                out[day] = money
        return out

    async def calendar_range(
        self, origin: str, destination: str, start: date, end: date, *, currency: str = "EUR"
    ) -> dict[date, Money]:
        """Walk month by month and merge.

        Because the anchor date only loosely controls which days come back, the
        windows overlap on purpose and results are deduplicated by date.
        """
        out: dict[date, Money] = {}
        cursor = start.replace(day=1)
        guard = 0
        while cursor <= end and guard < 24:
            guard += 1
            try:
                month = await self.calendar(origin, destination, cursor, currency=currency)
            except SourceError as exc:
                logger.warning(
                    "condor: %s-%s %s failed: %s", origin, destination, cursor, exc
                )
                month = {}
            for day, money in month.items():
                if start <= day <= end and (
                    day not in out or money.minor < out[day].minor
                ):
                    out[day] = money
            cursor = (cursor + timedelta(days=32)).replace(day=1)
        return out
