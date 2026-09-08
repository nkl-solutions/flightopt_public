"""Trivago ueber den offenen MCP-Endpunkt.

    https://mcp.trivago.com/mcp

Streamable HTTP, kein Schluessel, kein Vertrag. Der Ablauf ist der des
Protokolls: `initialize` liefert im Header `Mcp-Session-Id`, danach die
Notification `notifications/initialized`, danach `tools/call`. Ab dem zweiten
Aufruf geht die Sitzungskennung wieder mit hinaus.

Grenzen der Quelle, die kein Adapter wegbauen kann: rund 50 Ergebnisse je
Suche, genau ein Preis je Unterkunft (kein Anbieter-Split) und kein
Tageskalender. Die Tagesschleife liegt deshalb ueber dem Adapter.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from typing import Any, Mapping

from flightopt.domain.models import Money
from flightopt.hotels.models import HotelOffer, HotelQuery
from flightopt.hotels.prices import parse_count, parse_price, parse_rating, parse_stars
from flightopt.hotels.sources.base import (
    HotelBatch,
    HotelSource,
    SourceBlocked,
    SourceError,
    header,
)

logger = logging.getLogger(__name__)

MCP_URL = "https://mcp.trivago.com/mcp"
PROTOCOL_VERSION = "2025-03-26"
TOOL_SEARCH = "trivago-accommodation-search"

# Das Protokoll erlaubt beide Antwortformen auf demselben Endpunkt, also muss
# der Client beide annehmen.
ACCEPT = "application/json, text/event-stream"

MESSAGE_LIMIT = 200
"""So viel einer Klartext-Meldung wird zitiert.

Eine Fehlermeldung ist ein Satz. Alles, was deutlich laenger ist, ist kein
Satz mehr, sondern ein Block - und ein Anweisungsblock an ein Sprachmodell ist
genau das, was hier nicht durchgereicht werden soll. Zitiert wird deshalb
gekuerzt, und zitiert wird als Daten.
"""

WHITESPACE = re.compile(r"\s+")


def source_message(text: str, *, limit: int = MESSAGE_LIMIT) -> str:
    """Aus einem Textblock ohne JSON die Meldung, die die Quelle geschickt hat.

    Sie wird zu einer Zeile zusammengezogen und gekuerzt. Sie bleibt dabei
    Daten: sie landet im Text einer Ausnahme und wird nirgends befolgt.
    """
    line = WHITESPACE.sub(" ", str(text)).strip()
    if len(line) <= limit:
        return line
    return line[:limit].rstrip() + " ..."


def decode_body(text: str) -> Any:
    """Den Antwortkoerper lesen, ob JSON oder Server-Sent Events.

    Im SSE-Fall steht die Nutzlast in einer Zeile mit dem Praefix `data: `.
    Es wird die erste Zeile genommen, die sich als JSON lesen laesst; ein
    Kommentar-Keepalive (`: ping`) und ein `[DONE]` gehen dabei vorbei.
    """
    stripped = text.strip()
    if not stripped:
        raise SourceError("trivago: leere Antwort")
    if stripped[0] in "{[":
        try:
            return json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise SourceError(f"trivago: unlesbares JSON: {exc}") from exc

    for line in stripped.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[len("data:"):].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            return json.loads(chunk)
        except json.JSONDecodeError:
            continue
    raise SourceError("trivago: Antwort ohne lesbare JSON-Nutzlast")


def build_arguments(query: HotelQuery) -> dict[str, Any]:
    """Die Argumente von `trivago-accommodation-search`.

    Eine Sternefilterung kennt das Werkzeug nicht; sie passiert deshalb nach
    dem Parsen und steht als solche im Bericht, statt so zu tun, als haette
    die Quelle schon gefiltert.
    """
    args: dict[str, Any] = {
        "query": query.destination.strip(),
        "arrival": query.arrival.isoformat(),
        "departure": query.departure.isoformat(),
        "adults": max(1, query.adults),
        "rooms": max(1, query.rooms),
        "country": query.country.upper(),
        "currency": query.currency.upper(),
    }
    if query.children:
        args["children"] = len(query.children)
        args["children_ages"] = "-".join(str(int(age)) for age in query.children)
    return args


def _first_text_block(result: Mapping[str, Any]) -> str:
    """Der erste `text`-Block. Die `image`-Bloecke daneben tragen keine Preise."""
    for block in result.get("content") or []:
        if isinstance(block, Mapping) and block.get("type") == "text":
            return str(block.get("text") or "")
    return ""


def _envelope_from_text(text: str) -> Mapping[str, Any]:
    """Das JSON-Objekt aus dem Textblock holen.

    Vor dem Objekt steht Fliesstext ("IMPORTANT: Read the ... You MUST follow
    them exactly."). Auch dieser Vorspann ist an ein Sprachmodell gerichtet und
    wird wie jede andere Werkzeug-Ausgabe als Daten behandelt: er wird
    uebersprungen, nicht befolgt. Gelesen wird ab der ersten geschweiften
    Klammer, und auch nur so weit, wie ein Objekt reicht.

    Steht gar kein Objekt darin, sondern nur ein Satz, dann hat die Quelle
    selbst geantwortet ("An error occurred while searching for
    accommodations."). Das ist ihre Stoerung und nicht unser Parse-Problem,
    also wird sie woertlich weitergegeben statt uns zugeschoben.
    """
    start = text.find("{")
    if start < 0:
        raise SourceError(f"trivago meldet: {source_message(text)}")
    try:
        envelope, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        raise SourceError(f"trivago: Textblock ist kein JSON: {exc}") from exc
    if not isinstance(envelope, Mapping):
        raise SourceError("trivago: Textblock ist kein Objekt")
    return envelope


def parse_tool_result(result: Mapping[str, Any], query: HotelQuery) -> HotelBatch:
    """Die Werkzeug-Antwort zu Angeboten.

    SICHERHEITSREGEL, nicht verhandelbar: die Antwort traegt neben `output` ein
    Feld `system_message` und davor einen Vorspann, die beide an ein
    Sprachmodell gerichtete Anweisungen enthalten ("You are a helpful travel
    assistant ... You MUST follow them exactly"). Werkzeug-Ausgabe ist Daten,
    niemals Anweisung. Dieser Parser liest ausschliesslich `output`.
    `system_message` wird nicht gelesen, nicht geloggt, nicht weitergereicht und
    nirgends ausgefuehrt. Wer das Feld spaeter doch anfassen will: nein.

    Drei Arten von Schieflage werden auseinandergehalten, weil sie an drei
    verschiedenen Stellen liegen: die Quelle meldet eine Stoerung (ihr Text,
    woertlich zitiert), die Antwort ist da, aber ohne `output` (Aufbau
    geaendert), oder es kommt gar kein Textblock (Protokoll passt nicht).
    """
    text = _first_text_block(result)
    if result.get("isError"):
        # Der Server sagt selbst, dass es schiefging. Sein Wortlaut ist die
        # bessere Auskunft als jede Vermutung von uns.
        raise SourceError(
            f"trivago meldet: {source_message(text)}"
            if text.strip()
            else f"trivago: {TOOL_SEARCH} meldet einen Fehler ohne Text"
        )
    if not text:
        raise SourceError("trivago: Antwort ohne Textblock, das Protokoll passt nicht")
    envelope = _envelope_from_text(text)

    raw = envelope.get("output")
    if isinstance(raw, str):
        if not raw.strip():
            raise SourceError("trivago: Feld 'output' ist leer")
        # `output` ist ein JSON-Array als Zeichenkette, also zweimal auspacken.
        try:
            records = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SourceError(f"trivago: 'output' ist kein JSON: {exc}") from exc
    elif isinstance(raw, list):
        records = raw
    else:
        raise SourceError("trivago: Antwort ohne Feld 'output'")
    if not isinstance(records, list):
        raise SourceError("trivago: 'output' ist keine Liste")

    batch = HotelBatch()
    for record in records:
        if not isinstance(record, Mapping):
            batch.skipped.append("Datensatz ist kein Objekt")
            continue
        offer, reason = _to_offer(record, query)
        if offer is None:
            batch.skipped.append(reason)
        else:
            batch.offers.append(offer)
    return batch


def _to_offer(record: Mapping[str, Any], query: HotelQuery) -> tuple[HotelOffer | None, str]:
    name = str(record.get("accommodation_name") or "").strip()
    ident = str(record.get("accommodation_id") or "").strip()
    if not ident or not name:
        return None, "Datensatz ohne Name oder ID"

    currency = str(record.get("currency") or query.currency).strip().upper() or "EUR"
    nights = query.nights
    price = parse_price(record.get("price_per_stay"), currency)
    if price is None:
        # Ein Preis pro Nacht ist ebenfalls belastbar, solange die Naechtezahl
        # aus der Anfrage stammt und nicht aus dem Datensatz geraten wird.
        per_night = parse_price(record.get("price_per_night"), currency)
        if per_night is not None:
            price = Money(per_night.minor * nights, currency)
    if price is None:
        return None, f"{name}: Preis unlesbar ({record.get('price_per_stay')!r})"

    city, country = _split_country_city(record.get("country_city"))
    stars = parse_stars(record.get("hotel_rating"))
    rating = parse_rating(record.get("review_rating"))

    if query.stars and stars not in query.stars:
        return None, f"{name}: {stars or 'ohne'} Sterne, gesucht {sorted(query.stars)}"
    if query.min_review_score is not None and (rating is None or rating < query.min_review_score):
        return None, f"{name}: Bewertung {rating} unter {query.min_review_score}"

    return (
        HotelOffer(
            source="trivago",
            property_key=f"trivago:{ident}",
            name=name,
            arrival=query.arrival,
            departure=query.departure,
            price_total=price,
            stars=stars,
            city=city,
            country=country,
            review_rating=rating,
            review_count=parse_count(record.get("review_count")),
            url=str(record.get("accommodation_url") or "") or None,
            lat=_as_float(record.get("latitude")),
            lon=_as_float(record.get("longitude")),
            advertisers=str(record.get("advertisers") or "") or None,
            party_size=query.party_size,
            rooms=query.rooms,
            indicative=True,
        ),
        "",
    )


def _split_country_city(raw: Any) -> tuple[str | None, str | None]:
    """"Athens, Greece" zu ("Athens", "Greece")."""
    text = str(raw or "").strip()
    if not text:
        return None, None
    if "," not in text:
        return text, None
    city, _, country = text.rpartition(",")
    return city.strip() or None, country.strip() or None


def _as_float(raw: Any) -> float | None:
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def retry_after_seconds(raw: str | None, attempt: int) -> float:
    """`Retry-After` in Sekunden, sonst eine wachsende Wartezeit.

    Gedeckelt auf eine Minute: ein Server, der eine Stunde verlangt, bekommt
    trotzdem keinen Prozess, der eine Stunde lang blockiert.
    """
    if raw:
        try:
            return max(1.0, min(60.0, float(str(raw).strip())))
        except ValueError:
            pass
    return min(60.0, random.uniform(1.0, 2.0 * (2**attempt)))


class TrivagoMcpSource(HotelSource):
    """Primaerquelle. Preise sind Richtwerte, nie Bestpreise."""

    name = "trivago"
    indicative = True
    # 2 Anfragen pro Sekunde ist die Obergrenze, die wir uns geben.
    per_minute = 120
    concurrency = 1
    impersonate = "chrome"

    def __init__(self, *, url: str = MCP_URL, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.url = url
        self._http: Any = None
        self._mcp_session_id: str | None = None
        self._rpc_id = 0
        self._handshake = asyncio.Lock()

    def _new_session(self) -> Any:
        from curl_cffi import requests as creq

        return creq.Session(impersonate=self.impersonate)

    @property
    def session(self) -> Any:
        if self._http is None:
            self._http = self._new_session()
        return self._http

    def close(self) -> None:
        session, self._http = self._http, None
        self._mcp_session_id = None
        if session is None:
            return
        try:
            session.close()
        except Exception:  # noqa: BLE001 - Schliessen darf nie nach oben werfen
            logger.debug("trivago: Sitzung liess sich nicht schliessen", exc_info=True)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": ACCEPT}
        if self._mcp_session_id:
            headers["Mcp-Session-Id"] = self._mcp_session_id
        return headers

    async def _post(self, body: Mapping[str, Any], *, retries: int = 3) -> Any:
        if self.breaker.is_open:
            raise SourceBlocked(
                f"trivago: Sicherung offen, noch {self.breaker.remaining():.0f}s"
            )
        last: Exception | None = None
        for attempt in range(retries):
            try:
                async with self.limiter.slot():
                    response = await asyncio.to_thread(
                        self.session.post,
                        self.url,
                        json=dict(body),
                        headers=self._headers(),
                        timeout=45,
                    )
            except Exception as exc:  # noqa: BLE001 - Netzwerkschicht
                last = exc
                await asyncio.sleep(retry_after_seconds(None, attempt))
                continue

            session_id = header(response, "mcp-session-id")
            if session_id:
                self._mcp_session_id = session_id

            status = int(getattr(response, "status_code", 0))
            if status == 429:
                self.breaker.record_block()
                last = SourceBlocked("trivago: HTTP 429")
                await asyncio.sleep(
                    retry_after_seconds(header(response, "retry-after"), attempt)
                )
                continue
            if status == 403:
                self.breaker.record_block()
                raise SourceBlocked("trivago: HTTP 403")
            if status >= 500:
                last = SourceError(f"trivago: HTTP {status}")
                await asyncio.sleep(retry_after_seconds(None, attempt))
                continue
            if status >= 400:
                raise SourceError(f"trivago: HTTP {status} {str(response.text)[:200]}")

            self.breaker.record_success()
            return response
        raise last or SourceError(f"trivago: {retries} Versuche ohne Antwort")

    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    async def _rpc(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        body: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
        }
        if params is not None:
            body["params"] = dict(params)
        response = await self._post(body)
        message = decode_body(str(response.text))
        if isinstance(message, Mapping) and message.get("error"):
            error = message["error"]
            detail = error.get("message") if isinstance(error, Mapping) else error
            # Werkzeug-Drift ist der wahrscheinlichste Grund: der Server hat
            # zwischen Versionen schon eine Operation entfernt. Dann muss hier
            # eine klare Meldung stehen und kein Ratespiel.
            raise SourceError(f"trivago: {method} scheiterte: {detail}")
        if isinstance(message, Mapping):
            return message.get("result", {})
        raise SourceError(f"trivago: {method} lieferte kein Objekt")

    async def _notify(self, method: str) -> None:
        await self._post({"jsonrpc": "2.0", "method": method})

    async def handshake(self) -> None:
        """`initialize`, dann `notifications/initialized`. Genau einmal."""
        async with self._handshake:
            if self._mcp_session_id:
                return
            await self._rpc(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "flightopt", "version": "0.1.0"},
                },
            )
            if not self._mcp_session_id:
                raise SourceError("trivago: initialize lieferte keine Mcp-Session-Id")
            await self._notify("notifications/initialized")

    async def search(self, query: HotelQuery) -> HotelBatch:
        await self.handshake()
        result = await self._rpc(
            "tools/call",
            {"name": TOOL_SEARCH, "arguments": build_arguments(query)},
        )
        if not isinstance(result, Mapping):
            raise SourceError("trivago: tools/call lieferte kein Objekt")
        # `isError` und die drei Schieflagen daneben liegen in `parse_tool_result`,
        # damit sie ohne Netz gegen eine aufgezeichnete Antwort pruefbar sind.
        return parse_tool_result(result, query)
