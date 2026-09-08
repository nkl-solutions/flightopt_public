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
"""How many days one request asks for.

`numberOfFlightDays` is a day count, not a month, and 31 is the size that was
verified live. It stays at 31: the endpoint's own window is about a month, so
asking for more is a guess. What changed is that the window is now tiled in
days rather than in calendar months, which is where the wasted requests were.
"""


class CondorSource(HttpSource):
    name = "condor"
    carrier = "DE"
    also_carriers = ("DI",)
    """Marabu sells through the same booking engine as Condor."""
    supports_calendar = True
    supports_search = False
    """The endpoint prices days, not flights; no times or numbers are returned."""
    # Offene Lowfare-API; nur die Monatsschleife kostet mehrere Abrufe.
    per_minute = 30

    async def calendar(
        self, origin: str, destination: str, month: date, *, currency: str = "EUR"
    ) -> dict[date, Money]:
        """The days around the first of `month`. Kept for the calendar protocol."""
        return await self._window(
            origin, destination, month.replace(day=1), DAYS_PER_CALL, currency
        )

    async def _window(
        self, origin: str, destination: str, anchor: date, days: int, currency: str
    ) -> dict[date, Money]:
        params = {
            "origin": origin,
            "destination": destination,
            "outboundDate": anchor.strftime("%Y%m%d"),
            "numberOfFlightDays": days,
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
        """Tile the window in 31-day steps, not in calendar months.

        The month loop this replaces paid for the calendar, not for the data: a
        60-day search starting mid-month spans three calendar months and cost
        three requests for a window two requests cover. Alignment decided the
        price, and a window shorter than a month could still cost two calls.

        The anchor only loosely controls which days come back - asking for
        1 October returned 2 October onwards - so the cursor starts a day early
        and the windows overlap on purpose. Results are deduplicated by date
        and the cheapest wins.
        """
        out: dict[date, Money] = {}
        anchor = start - timedelta(days=1)
        guard = 0
        while guard < 24:
            guard += 1
            try:
                chunk = await self._window(
                    origin, destination, anchor, DAYS_PER_CALL, currency
                )
            except SourceError as exc:
                logger.warning(
                    "condor: %s-%s %s failed: %s", origin, destination, anchor, exc
                )
                chunk = {}
            for day, money in chunk.items():
                if start <= day <= end and (
                    day not in out or money.minor < out[day].minor
                ):
                    out[day] = money
            # Der Abruf deckt anchor+1 bis anchor+DAYS_PER_CALL ab. Erst wenn
            # das Fensterende darueber hinausgeht, ist ein weiterer noetig -
            # sonst kostete ein Fenster von genau 31 Tagen zwei Abrufe.
            covered_through = anchor + timedelta(days=DAYS_PER_CALL)
            if covered_through >= end:
                break
            anchor = covered_through
        return out
