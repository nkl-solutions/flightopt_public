"""Eurowings low fare calendar.

An earlier probe of `apps.eurowings.com/flightsearch/v1/...` found only which
days are flown, with no prices, and Eurowings was written off. That was the
wrong path. The site's own fare calendar sits on a different host and encodes
its arguments as dot-separated selectors instead of a query string:

    /services/centrallowfare.version1.ccde.originDUS.destinationPMI.json

One request returns up to fifteen months, outbound and inbound together.

Cloudflare sits in front. A plain client is refused, and even a browser-like
one is challenged now and then, so the adapter first loads the public fare
calendar page once to pick up the `__cf_bm` cookie and reuses that session.

Verified live 2026-09-04: DUS-PMI returned 15 bookable months with daily
prices, BER-AYT answered as well.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

from flightopt.domain.models import Money
from flightopt.sources.base import HttpSource, SourceError

logger = logging.getLogger(__name__)

SERVICE = "https://www.eurowings.com/services/centrallowfare.version1"
WARMUP = "https://www.eurowings.com/de/buchen/fluege/sparkalender.html"


class EurowingsSource(HttpSource):
    name = "eurowings"
    carrier = "EW"
    supports_calendar = True
    supports_search = False
    """Prices come per day; flight times are not part of this payload."""
    per_minute = 15

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self._session = None

    async def _warm_session(self):
        """One page load to earn the Cloudflare cookie the service expects."""
        if self._session is not None:
            return self._session

        from curl_cffi import requests as creq

        session = creq.Session(impersonate=self.impersonate)
        await self.limiter.wait()
        try:
            await asyncio.to_thread(session.get, WARMUP, timeout=40)
        except Exception as exc:  # noqa: BLE001
            raise SourceError(f"eurowings: warm-up failed: {exc}") from exc
        self._session = session
        return session

    async def _fetch(self, origin: str, destination: str) -> dict:
        session = await self._warm_session()
        url = (
            f"{SERVICE}.ccde.origin{origin}.destination{destination}"
            ".showalternativesfalse.json"
        )
        return await self.fetch_json(url, session=session)

    @staticmethod
    def _read_outbound(data: dict, currency: str) -> dict[date, Money]:
        """Pull daily prices out of the outbound section.

        The payload nests months inside sections, and each day carries the
        price twice: a raw number and a formatted string. Only the raw one is
        safe to read, since the formatted one is localised.
        """
        out: dict[date, Money] = {}
        for section in data.get("sections") or []:
            if section.get("type") != "outbound":
                continue
            for month in section.get("bookableMonths") or []:
                year, mon = month.get("year"), month.get("month")
                if not year or not mon:
                    continue
                # The list is called expandedDates, not days.
                for day in month.get("expandedDates") or []:
                    number = day.get("date")
                    price = (day.get("price") or {}).get("raw")
                    if not number or price is None:
                        continue
                    got = day.get("currency", currency)
                    if got != currency:
                        continue
                    try:
                        when = date(int(year), int(mon), int(number))
                    except ValueError:
                        continue
                    money = Money.from_major(float(price), got)
                    if when not in out or money.minor < out[when].minor:
                        out[when] = money
        return out

    async def calendar_range(
        self, origin: str, destination: str, start: date, end: date, *, currency: str = "EUR"
    ) -> dict[date, Money]:
        """One request covers the whole window, so the range is just filtered."""
        try:
            data = await self._fetch(origin, destination)
        except SourceError as exc:
            logger.warning("eurowings: %s-%s failed: %s", origin, destination, exc)
            return {}
        prices = self._read_outbound(data, currency)
        return {d: m for d, m in prices.items() if start <= d <= end}

    async def calendar(
        self, origin: str, destination: str, month: date, *, currency: str = "EUR"
    ) -> dict[date, Money]:
        first = month.replace(day=1)
        nxt = (first + timedelta(days=32)).replace(day=1)
        return await self.calendar_range(
            origin, destination, first, nxt - timedelta(days=1), currency=currency
        )
