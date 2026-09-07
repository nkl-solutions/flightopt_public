"""JetBlue Best Fare Finder: the cheapest fare per day, one month per call.

The booking front end on www.jetblue.com sits behind PerimeterX, but the REST
host it talks to answers a plain POST without a key, a token or a cookie. The
request shape is the one the site itself builds:

    {"origin", "destination", "fareType": "LOWEST", "month": "NOVEMBER 2026",
     "tripType": "ONE_WAY", "adult": 1, "child": 0, "infant": 0}

Two details that decide whether the numbers mean anything:
  - `month` is an English month name plus year, upper case. It is built here
    from a fixed table rather than strftime, because `%B` follows the process
    locale and a German locale would send "NOVEMBER" as "NOVEMBER 2026" only by
    accident.
  - the answer names its own currency in `currencyCode`, normally USD. It is
    reported as it comes and the grid converts; silently treating it as EUR
    would understate every JetBlue day by roughly a sixth.

JetBlue matters for the transatlantic grid: it is the cheap way on from a US
east coast arrival, and it is the only US carrier in the plan with an open
per-day endpoint.

Verified live 2026-09-07: JFK-LAX, November 2026, 29 priced days in USD.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from flightopt.domain.models import Money
from flightopt.sources.base import HttpSource, SourceError

logger = logging.getLogger(__name__)

ENDPOINT = "https://jbrest.jetblue.com/bff/bff-service/bestFares"
REFERER = "https://www.jetblue.com/best-fare-finder"
MAX_MONTHS = 13
"""Guard against a bad range walking the endpoint forever."""

MONTH_NAMES = (
    "JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE",
    "JULY", "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER",
)


def month_token(day: date) -> str:
    """`NOVEMBER 2026` - locale independent on purpose."""
    return f"{MONTH_NAMES[day.month - 1]} {day.year}"


class JetBlueSource(HttpSource):
    name = "jetblue"
    carrier = "B6"
    supports_calendar = True
    supports_search = False
    """The rows carry a date, an amount and a seat count, no flights."""
    per_minute = 12

    async def calendar(
        self, origin: str, destination: str, month: date, *, currency: str = "EUR"
    ) -> dict[date, Money]:
        body = {
            "origin": origin,
            "destination": destination,
            "fareType": "LOWEST",
            "month": month_token(month),
            "tripType": "ONE_WAY",
            "adult": 1,
            "child": 0,
            "infant": 0,
        }
        data = await self.fetch_json(
            ENDPOINT,
            json_body=body,
            headers={
                "Referer": REFERER,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        if not isinstance(data, dict):
            return {}

        got = str(data.get("currencyCode") or "").upper()
        if not got:
            # No currency means no way to know what the amounts are worth.
            return {}

        out: dict[date, Money] = {}
        for row in data.get("outboundFares") or []:
            if not isinstance(row, dict):
                continue
            amount = row.get("amount")
            if amount is None:
                continue
            try:
                day = date.fromisoformat(str(row.get("date")))
                money = Money.from_major(float(amount), got)
            except (TypeError, ValueError):
                continue
            if money.minor <= 0:
                continue
            if day not in out or money.minor < out[day].minor:
                out[day] = money
        return out

    async def calendar_range(
        self, origin: str, destination: str, start: date, end: date, *, currency: str = "EUR"
    ) -> dict[date, Money]:
        """Walk month by month; one call covers exactly one calendar month."""
        out: dict[date, Money] = {}
        cursor = start.replace(day=1)
        guard = 0
        while cursor <= end and guard < MAX_MONTHS:
            guard += 1
            try:
                month = await self.calendar(origin, destination, cursor, currency=currency)
            except SourceError as exc:
                logger.warning(
                    "jetblue: %s-%s %s failed: %s", origin, destination, cursor, exc
                )
                month = {}
            for day, money in month.items():
                if start <= day <= end and (
                    day not in out or money.minor < out[day].minor
                ):
                    out[day] = money
            cursor = (cursor + timedelta(days=32)).replace(day=1)
        return out
