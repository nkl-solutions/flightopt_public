"""Aegean low fare calendar.

Aegean's website exposes a month-at-a-time cheapest-fare endpoint that answers
without a login or token. It needs a browser TLS fingerprint; a plain HTTP
client is refused outright, which is why every request goes through curl_cffi.

Dates arrive as the old Microsoft JSON format, double-escaped:
    "\\"\\\\/Date(1793491200000)\\\\/\\""
so the timestamp is pulled out with a regex rather than parsed as JSON.

Die Waehrung steht einmal je Antwort in `CurrencySymbol`, als Zeichen und nicht
als Code. Einen Waehrungsparameter kennt der Endpunkt nicht.

Verified live 2026-09-04 from a German residential IP:
    GET .../sys/lowfares/RouteLowFares/?DepartureAirport=ATH&ArrivalAirport=SKG
    -> 200, priced days for the month

Nachgemessen 2026-09-09, dieselbe Strecke: `CurrencySymbol` ist "€", und eine
Zeile fuehrt genau `Date`, `FullPrice`, `Price`, `Class`, `Difference`,
`Error`, `Updated`, `ServiceFee`. Flugnummern oder Zeiten stehen nirgends in
der Antwort.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone

from flightopt.domain.models import Money
from flightopt.sources.base import HttpSource, SourceError

logger = logging.getLogger(__name__)

ENDPOINT = "https://en.aegeanair.com/en/sys/lowfares/RouteLowFares/"
MS_DATE = re.compile(r"/Date\((-?\d+)")

CURRENCY_BY_SYMBOL = {
    "€": "EUR",
    "£": "GBP",
    "₺": "TRY",
    "₪": "ILS",
}
"""Nur Zeichen, deren Code ausser Frage steht.

Das Dollarzeichen fehlt mit Absicht: es tragen USD, CAD, AUD und weitere. Ein
geratener Code waere ein Preis, der um ein Drittel danebenliegt und die Zelle
trotzdem gewinnt - genau der Fehler, den diese Tabelle verhindern soll.
"""


def parse_ms_date(raw: str | None) -> date | None:
    """Pull a date out of Microsoft's `/Date(1793491200000)/` wrapper."""
    if not raw:
        return None
    match = MS_DATE.search(raw)
    if not match:
        return None
    seconds = int(match.group(1)) / 1000
    return datetime.fromtimestamp(seconds, tz=timezone.utc).date()


def currency_of(symbol: str | None) -> str | None:
    """Der Waehrungscode zum Zeichen der Antwort, sonst None.

    Aegean nennt seine Waehrung nur als Symbol (`CurrencySymbol`). Ein
    dreistelliger Buchstabencode geht unveraendert durch, damit ein Markt, der
    schon den Code liefert, nicht durch die Tabelle muss.
    """
    text = str(symbol or "").strip()
    if not text:
        return None
    if len(text) == 3 and text.isalpha():
        return text.upper()
    return CURRENCY_BY_SYMBOL.get(text)


class AegeanSource(HttpSource):
    name = "aegean"
    carrier = "A3"
    supports_calendar = True
    supports_search = False
    """Der Endpunkt bepreist Tage, nicht Fluege - eine Antwortzeile fuehrt weder
    Flugnummer noch Zeiten (nachgemessen 2026-09-09, siehe Modulkopf).

    Bis hierher gab es trotzdem eine `search_leg`: sie holte denselben
    Tagespreis ein zweites Mal und gab ihn mit `is_estimate=False` und null
    Segmenten zurueck. Damit galt ein Preis als geprueft, der keine Verbindung
    beschreibt. Zwei Folgen, beide schlecht: die Ergebniszeile trug den Stempel
    "geprueft" ohne Deckung, und weil die Nachpruefung Airlines vor Sammlern
    fragt und beim ersten Treffer aufhoert, verdraengte dieser leere Treffer
    genau die Quelle, die echte Flugzeiten geliefert haette.

    Der Kalender bleibt und speist das Gitter weiter als Schaetzung. Damit
    steht Aegean dort, wo Eurowings, Condor, British Airways und Icelandair
    schon stehen: Kalender ja, Nachpruefung nein."""
    # Bleibt niedrig: vor dem Endpunkt sitzt ein WAF, das eine Salve bestraft.
    per_minute = 15
    concurrency = 2
    impersonate = "chrome124"

    async def calendar(
        self, origin: str, destination: str, month: date, *, currency: str = "EUR"
    ) -> dict[date, Money]:
        """Die Tagespreise des Monats, in der Waehrung der Antwort.

        `currency` sagt, was die Suche rechnen will, nicht was der Endpunkt
        liefert: der kennt keinen Waehrungsparameter und antwortet in der
        Waehrung seines Marktes. Gemeldet wird deshalb die Nativwaehrung, und
        umgerechnet wird an der einen Stelle, die das darf -
        `grid.convert_prices`. Genauso halten es Wizz und JetBlue.
        """
        params = {
            "DepartureAirport": origin,
            "ArrivalAirport": destination,
            "TripType": "O",
            "DepartureDate": f"{month.year:04d}-{month.month:02d}",
            "ReturnDate": "",
            "Type": "Fares",
        }
        data = await self.fetch_json(
            ENDPOINT, params=params, headers={"X-Requested-With": "XMLHttpRequest"}
        )
        symbol = data.get("CurrencySymbol")
        native = currency_of(symbol)
        if native is None:
            # Ohne lesbare Waehrung ist der Betrag keine Zahl, mit der sich
            # rechnen laesst. Die angefragte Waehrung daraufzustempeln war der
            # Fehler: ein Euro-Betrag als USD ausgegeben liegt rund 15 Prozent
            # zu tief und gewinnt die Zelle gegen ehrlich gerechnete Angebote.
            # Ein Monat weniger ist besser als ein falscher Sieger.
            logger.warning(
                "aegean: %s-%s %s: Waehrungszeichen %r nicht zuzuordnen, "
                "Monat entfaellt",
                origin, destination, month, symbol,
            )
            return {}
        out: dict[date, Money] = {}
        for row in data.get("Outbound") or []:
            if row.get("Error"):
                continue
            price = row.get("Price")
            if price is None:
                price = row.get("FullPrice")
            day = parse_ms_date(row.get("Date"))
            if price is None or day is None:
                continue
            total = float(price) + float(row.get("ServiceFee") or 0)
            money = Money.from_major(total, native)
            if day not in out or money.minor < out[day].minor:
                out[day] = money
        return out

    async def calendar_range(
        self, origin: str, destination: str, start: date, end: date, *, currency: str = "EUR"
    ) -> dict[date, Money]:
        """Ein Abruf je Kalendermonat - mehr gibt der Endpunkt nicht her.

        `DepartureDate` ist ein Monat (`YYYY-MM`), kein Zeitraum. Ein Fenster
        ueber drei Monate kostet deshalb drei Abrufe, und daran ist nichts zu
        sparen, solange die Airline keinen Bereichsparameter anbietet.
        """
        out: dict[date, Money] = {}
        cursor = start.replace(day=1)
        while cursor <= end:
            try:
                month = await self.calendar(origin, destination, cursor, currency=currency)
            except SourceError as exc:
                logger.warning(
                    "aegean: %s-%s %s failed: %s", origin, destination, cursor, exc
                )
                month = {}
            out.update({d: m for d, m in month.items() if start <= d <= end})
            cursor = (cursor + timedelta(days=32)).replace(day=1)
        return out
