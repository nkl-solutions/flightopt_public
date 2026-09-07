"""Foreign exchange, so a JPY calendar can rank next to a EUR one.

`Money` stays single-currency on purpose: adding two currencies raises rather
than inventing a number. That guarantee only holds if conversion happens in
exactly one place, which is this module plus its callers in `search/grid.py`.

Rates come from the ECB daily reference file (no key, XML, around 30
currencies). It is a reference rate, not the card rate a bank would charge, so
converted prices are labelled as converted in the UI.
"""

from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from flightopt.domain.models import Money

logger = logging.getLogger(__name__)

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
FALLBACK_DATA = Path(__file__).resolve().parent.parent / "data" / "fx_fallback.json"


class UnknownCurrency(ValueError):
    """No rate for this currency, so no honest way to convert it."""


class FxUnavailable(RuntimeError):
    """The rate source could not be reached or did not answer with rates."""


@dataclass(frozen=True, slots=True)
class Rates:
    base: str = "EUR"
    rates: Mapping[str, float] = field(default_factory=dict)
    """Units of the currency per one unit of `base`. Read-only after building."""
    fetched_at: datetime = field(default_factory=datetime.now)

    def __post_init__(self) -> None:
        # `frozen=True` only protects the attribute, not the dict behind it.
        # A caller that kept the dict it passed in could still edit rates that
        # the whole search already priced against, so hand out a view instead.
        object.__setattr__(self, "rates", MappingProxyType(dict(self.rates)))

    def __hash__(self) -> int:
        # The generated hash would hash the mapping and raise. Rates are
        # small and read-only, so hashing the sorted pairs is honest and cheap.
        return hash((self.base, tuple(sorted(self.rates.items())), self.fetched_at))

    def __reduce__(self) -> tuple[Any, tuple[Any, ...]]:
        # A MappingProxyType cannot be pickled, and `copy.deepcopy` pickles
        # what it cannot copy atomically. Rebuilding through the constructor
        # keeps the object serialisable without handing out a mutable table:
        # `__post_init__` wraps the plain dict again on the way back in.
        return (self.__class__, (self.base, dict(self.rates), self.fetched_at))

    def rate(self, currency: str) -> float:
        currency = currency.upper()
        if currency == self.base:
            return 1.0
        try:
            return self.rates[currency]
        except KeyError as exc:
            raise UnknownCurrency(f"kein Kurs fuer {currency}") from exc

    def factor(self, source: str, target: str) -> float:
        """How many units of `target` one unit of `source` buys."""
        return self.rate(target) / self.rate(source)


def convert(money: Money, to: str, rates: Rates) -> Money:
    """Convert to `to`, rounded to minor units. Same currency passes through."""
    to = to.upper()
    if money.currency.upper() == to:
        return money
    return Money(round(money.minor * rates.factor(money.currency, to)), to)


def parse_ecb_xml(xml_text: str) -> Rates:
    """Read the daily reference file.

    The document nests three Cubes: the outer container, one per day, one per
    currency. Reading them by attribute rather than by path keeps the parser
    alive when the namespace prefix changes.

    Only the first dated Cube is read. The daily file has exactly one, but the
    90-day file uses the same shape and lists the newest day first; scanning
    the whole tree would mix days and let an old currency outlive its removal.
    """
    if "<!DOCTYPE" in xml_text:
        # Entity expansion is the one attack this parser is open to; a
        # reference-rate file never carries a DOCTYPE.
        raise ValueError("EZB-Antwort enthaelt eine DOCTYPE-Deklaration")

    root = ET.fromstring(xml_text)
    dated = next((el for el in root.iter() if el.get("time")), None)
    day: datetime | None = None
    if dated is not None:
        try:
            day = datetime.fromisoformat(str(dated.get("time")))
        except ValueError:
            day = None

    rates: dict[str, float] = {}
    for element in (dated if dated is not None else root).iter():
        currency, rate = element.get("currency"), element.get("rate")
        if currency and rate:
            try:
                rates[currency.upper()] = float(rate)
            except ValueError:
                continue
    if not rates:
        raise ValueError("EZB-Antwort enthaelt keine Kurse")
    return Rates(base="EUR", rates=rates, fetched_at=day or datetime.now())


async def fetch_ecb_rates(client: Any = None) -> Rates:
    """Load today's reference rates.

    `client` is anything with a `get(url, timeout=...)` that answers with
    `status_code` and `text`; without one, curl_cffi is used directly.
    """
    import asyncio

    if client is None:
        from curl_cffi import requests as creq

        client = creq

    try:
        resp = await asyncio.to_thread(client.get, ECB_URL, timeout=20)
    except Exception as exc:  # noqa: BLE001 - network layer
        raise FxUnavailable(f"EZB nicht erreichbar: {exc}") from exc

    if getattr(resp, "status_code", 0) != 200:
        raise FxUnavailable(f"EZB antwortete HTTP {getattr(resp, 'status_code', '?')}")
    try:
        return parse_ecb_xml(resp.text)
    except (ValueError, ET.ParseError) as exc:
        # ParseError derives from SyntaxError, not from ValueError: an HTML
        # error page or an empty body would otherwise escape past FxUnavailable
        # and take the whole search down with it.
        raise FxUnavailable(f"EZB-Antwort unlesbar: {exc}") from exc


def load_fallback() -> Rates:
    """The snapshot taken when this file was written.

    Months old rates are wrong by a few percent. That is still better than
    dropping every foreign-currency calendar, and the UI says the price was
    converted.
    """
    raw = json.loads(FALLBACK_DATA.read_text(encoding="utf-8"))
    stamp = raw.get("date")
    return Rates(
        base=raw.get("base", "EUR"),
        rates={k.upper(): float(v) for k, v in raw["rates"].items()},
        fetched_at=datetime.fromisoformat(stamp) if stamp else datetime.now(),
    )
