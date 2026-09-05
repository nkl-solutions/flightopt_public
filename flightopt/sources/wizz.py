"""Wizz Air timetable.

Wizz rotates the version segment of its API base URL, so the old published
path is a dead end. The current one is embedded in the website's own page
config and is read from there on first use.

The timetable endpoint answers without cookies or tokens and returns a price
per day, which is the shape the optimizer wants. Only the deep availability
search behind it is bot-gated, and we never call that.

Verified live 2026-09-04 from a German residential IP:
    POST https://be.wizzair.com/29.15.0/Api/search/timetable  BER-BEG
    -> 200, priced days with departure times
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

from flightopt.domain.models import Cabin, Money, Offer, Pax, Segment
from flightopt.sources.base import HttpSource, SourceError

logger = logging.getLogger(__name__)

SITE = "https://www.wizzair.com/en-gb"
BASE_RE = re.compile(r'apiUrl:"(https://be\.wizzair\.com/[\d.]+/Api)"')

# The timetable refuses windows longer than this, so longer searches are split.
MAX_WINDOW_DAYS = 42


class WizzSource(HttpSource):
    name = "wizz"
    carrier = "W6"
    supports_calendar = True
    supports_search = True
    per_minute = 20

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self._base: str | None = None
        self._routes: dict[str, set[str]] | None = None

    # -- discovery ------------------------------------------------------------

    async def base_url(self) -> str:
        """Read the current API base out of the site's page config.

        Pinning a version here would break the adapter on Wizz's next release,
        which is exactly what happened to the previously documented path.
        """
        if self._base:
            return self._base

        import asyncio

        from curl_cffi import requests as creq

        await self.limiter.wait()
        try:
            resp = await asyncio.to_thread(
                creq.get, SITE, impersonate=self.impersonate, timeout=40, proxy=self.proxy
            )
        except Exception as exc:  # noqa: BLE001
            raise SourceError(f"wizz: site unreachable: {exc}") from exc
        if resp.status_code != 200:
            raise SourceError(f"wizz: site returned HTTP {resp.status_code}")

        match = BASE_RE.search(resp.text)
        if not match:
            raise SourceError("wizz: API base URL not found in page config")
        self._base = match.group(1)
        logger.info("wizz: API base %s", self._base)
        return self._base

    async def load_routes(self, origin: str) -> set[str]:
        """Wizz publishes its whole network in one call, so fetch it once."""
        if self._routes is None:
            base = await self.base_url()
            try:
                data = await self.fetch_json(
                    f"{base}/asset/map", params={"languageCode": "en-gb"}
                )
            except SourceError as exc:
                logger.warning("wizz: route map failed: %s", exc)
                self._routes = {}
                return set()

            self._routes = {}
            for city in data.get("cities") or []:
                code = city.get("iata")
                if not code:
                    continue
                self._routes[code] = {
                    c.get("iata")
                    for c in (city.get("connections") or [])
                    if c.get("iata")
                }
            logger.info("wizz: network has %d airports", len(self._routes))
        return self._routes.get(origin, set())

    def supports_route(self, origin: str, destination: str) -> bool:
        if self._routes is None:
            return True  # not loaded yet; let the call decide
        return destination in self._routes.get(origin, set())

    # -- prices ---------------------------------------------------------------

    async def _timetable(
        self, origin: str, destination: str, start: date, end: date
    ) -> list[dict]:
        base = await self.base_url()
        body = {
            "flightList": [
                {
                    "departureStation": origin,
                    "arrivalStation": destination,
                    "from": start.isoformat(),
                    "to": end.isoformat(),
                }
            ],
            "priceType": "regular",
            "adultCount": 1,
            "childCount": 0,
            "infantCount": 0,
        }
        data = await self.fetch_json(f"{base}/search/timetable", json_body=body)
        return data.get("outboundFlights") or []

    async def calendar_range(
        self, origin: str, destination: str, start: date, end: date, *, currency: str = "EUR"
    ) -> dict[date, Money]:
        """Cheapest fare per day, walking the window in allowed-size chunks.

        Wizz prices in the departure market's currency and ignores any currency
        parameter, so a mismatch is reported rather than silently converted.
        """
        out: dict[date, Money] = {}
        cursor = start
        while cursor <= end:
            chunk_end = min(end, cursor + timedelta(days=MAX_WINDOW_DAYS - 1))
            try:
                flights = await self._timetable(origin, destination, cursor, chunk_end)
            except SourceError as exc:
                logger.warning(
                    "wizz: %s-%s %s..%s failed: %s", origin, destination,
                    cursor, chunk_end, exc,
                )
                flights = []
            for f in flights:
                price = f.get("price") or {}
                amount = price.get("amount")
                raw_date = f.get("departureDate")
                if amount is None or not raw_date:
                    continue
                got = price.get("currencyCode", currency)
                if got != currency:
                    logger.info(
                        "wizz: %s-%s prices in %s, not %s; skipping",
                        origin, destination, got, currency,
                    )
                    continue
                day = datetime.fromisoformat(raw_date).date()
                money = Money.from_major(float(amount), got)
                if day not in out or money.minor < out[day].minor:
                    out[day] = money
            cursor = chunk_end + timedelta(days=1)
        return {d: m for d, m in out.items() if start <= d <= end}

    async def calendar(
        self, origin: str, destination: str, month: date, *, currency: str = "EUR"
    ) -> dict[date, Money]:
        first = month.replace(day=1)
        nxt = (first + timedelta(days=32)).replace(day=1)
        return await self.calendar_range(
            origin, destination, first, nxt - timedelta(days=1), currency=currency
        )

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
        """One date, with the departure times the timetable reports.

        The timetable gives times but no arrival or flight number, so segments
        carry what is known and nothing is invented.
        """
        flights = await self._timetable(origin, destination, day, day)
        offers: list[Offer] = []
        for f in flights:
            price = f.get("price") or {}
            amount = price.get("amount")
            if amount is None:
                continue
            got = price.get("currencyCode", currency)
            if got != currency:
                continue
            raw_date = f.get("departureDate")
            if not raw_date or datetime.fromisoformat(raw_date).date() != day:
                continue

            segments: tuple[Segment, ...] = ()
            times = f.get("departureDates") or []
            if times:
                departure = datetime.fromisoformat(times[0])
                segments = (
                    Segment(
                        carrier="W6",
                        flight_number="W6",
                        origin=f.get("departureStation", origin),
                        destination=f.get("arrivalStation", destination),
                        departure=departure,
                        # No arrival in this payload; leaving it equal to the
                        # departure would fake a zero-length flight.
                        arrival=departure,
                    ),
                )
            offers.append(
                Offer(
                    source=self.name,
                    origin=origin,
                    destination=destination,
                    travel_date=day,
                    price=Money.from_major(float(amount), got),
                    segments=segments,
                    deep_link=(
                        "https://www.wizzair.com/en-gb/booking/select-flight/"
                        f"{origin}/{destination}/{day.isoformat()}"
                    ),
                    is_estimate=False,
                )
            )
        offers.sort(key=lambda o: o.price.minor)
        return offers
