"""Icelandair best price per day.

A plain calendar endpoint that answers about a year ahead in one request, with
no key and no session. Relevant here because Icelandair is the cheap way from
Germany to Iceland and onward to North America.

One trap worth naming: the correlation id must be sent as a header. Passing the
same value as a query parameter trips the bot check instead.

Verified live 2026-09-04: BER-KEF returned daily prices in EUR.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, timedelta

from flightopt.domain.models import Money
from flightopt.sources.base import HttpSource, SourceError

logger = logging.getLogger(__name__)

ENDPOINT = "https://www.icelandair.com/api/instantSearch/v2/bestPrice/byDay/oneway"
MAX_PERIOD_DAYS = 350


class IcelandairSource(HttpSource):
    name = "icelandair"
    carrier = "FI"
    supports_calendar = True
    supports_search = False
    """Prices are per day; the payload carries no flight numbers or times."""
    per_minute = 15

    async def calendar_range(
        self, origin: str, destination: str, start: date, end: date, *, currency: str = "EUR"
    ) -> dict[date, Money]:
        period = min(MAX_PERIOD_DAYS, max(1, (end - start).days + 1))
        params = {
            "departure": origin,
            "arrival": destination,
            "locale": "en-US",
            "period": period,
            "fromDate": start.isoformat(),
            "fallbackToRouteCurrency": "true",
        }
        try:
            data = await self.fetch_json(
                ENDPOINT,
                params=params,
                # Must be a header. As a query parameter this triggers a bot
                # challenge instead of an answer.
                headers={"X-Correlation-Id": str(uuid.uuid4())},
            )
        except SourceError as exc:
            logger.warning("icelandair: %s-%s failed: %s", origin, destination, exc)
            return {}

        out: dict[date, Money] = {}
        # The payload is a flat map of ISO date to price, not a list.
        for raw_day, row in (data or {}).items():
            if not isinstance(row, dict):
                continue
            price = row.get("bestPrice")
            if price is None:
                continue
            got = (row.get("currency") or currency).upper()
            if got != currency.upper():
                # `fallbackToRouteCurrency` can hand back the route's own
                # currency; converting silently would corrupt the total.
                continue
            try:
                day = date.fromisoformat(raw_day)
            except ValueError:
                continue
            if start <= day <= end:
                out[day] = Money.from_major(float(price), got)
        return out

    async def calendar(
        self, origin: str, destination: str, month: date, *, currency: str = "EUR"
    ) -> dict[date, Money]:
        first = month.replace(day=1)
        nxt = (first + timedelta(days=32)).replace(day=1)
        return await self.calendar_range(
            origin, destination, first, nxt - timedelta(days=1), currency=currency
        )
