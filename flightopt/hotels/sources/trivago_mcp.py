"""Trivago ueber den offenen MCP-Endpunkt.

    https://mcp.trivago.com/mcp

Streamable HTTP, kein Schluessel, kein Vertrag. Der Ablauf ist der des
Protokolls: `initialize` liefert im Header `Mcp-Session-Id`, danach die
Notification `notifications/initialized`, danach `tools/call`. Ab dem zweiten
Aufruf geht die Sitzungskennung wieder mit hinaus.

Grenzen der Quelle, die kein Adapter wegbauen kann: rund 50 Ergebnisse je
Suche, genau ein Preis je Unterkunft (kein Anbieter-Split) und kein
Tageskalender. Die Tagesschleife liegt deshalb ueber dem Adapter.

Der Endpunkt ist unzuverlaessig, nicht kaputt: gemessen an derselben Anfrage
antwortete er um 13:40 viermal hintereinander mit einem blossen Satz ("An
error occurred while searching for accommodations. Please try again.") und um
13:44 sechsmal hintereinander mit Daten. Deshalb gibt dieser Adapter bei genau
diesem Fehlerbild nicht sofort auf, sondern fragt nach einer kurzen Pause noch
zweimal nach. Was dabei nicht wiederholt wird, steht bei `search`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from typing import Any, Mapping, Sequence

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

RETRY_ATTEMPTS = 2
"""Neuversuche nach einer Klartext-Stoerung. Zwei, dann ist der Tag verloren.

Mehr waere kein Nachfassen mehr, sondern Klopfen: die beobachtete Stoerung
dauert Minuten, nicht Sekunden, und die kauft keine dritte Wiederholung.
"""

RETRY_BACKOFF: tuple[float, ...] = (2.0, 5.0)
"""Grundpausen je Neuversuch, in Sekunden. Ueberschreibbar im Konstruktor."""

RETRY_JITTER = 0.25
"""Streuung nach oben, als Anteil der Grundpause.

Ohne Streuung laufen mehrere Tage, die gleichzeitig auf dieselbe Stoerung
gelaufen sind, auch gleichzeitig wieder los.
"""


def retry_pause(
    attempt: int,
    backoff: Sequence[float] = RETRY_BACKOFF,
    *,
    jitter: float = RETRY_JITTER,
) -> float:
    """Die Pause vor dem Neuversuch nach `attempt` (nullbasiert).

    Waechst mit dem Versuch und streut nur nach oben. So bleibt die zweite
    Pause auch bei unguenstiger Streuung laenger als die erste, statt dass
    Zufall aus "wachsend" gelegentlich "schrumpfend" macht.
    """
    if not backoff:
        return 0.0
    base = float(backoff[min(attempt, len(backoff) - 1)])
    return base * random.uniform(1.0, 1.0 + max(0.0, jitter))


class SourceUnstable(SourceError):
    """Trivago hat geantwortet - mit einem Satz statt mit Daten.

    Das ist etwas anderes als ein kaputter Aufbau und etwas anderes als eine
    Sperre: die Quelle sagt selbst "Please try again". Nur dieses Bild wird
    wiederholt.
    """


class SessionExpired(SourceUnstable):
    """Der Server kennt unsere `Mcp-Session-Id` nicht mehr.

    Auch das ist ein Grund zum Nachfassen, aber erst nach neuem Handshake -
    sonst laeuft jeder Neuversuch in dieselbe verworfene Sitzung.
    """


def source_message(text: str, *, limit: int = MESSAGE_LIMIT) -> str:
    """Aus einem Textblock ohne JSON die Meldung, die die Quelle geschickt hat.

    Sie wird zu einer Zeile zusammengezogen und gekuerzt. Sie bleibt dabei
    Daten: sie landet im Text einer Ausnahme und wird nirgends befolgt.
    """
    line = WHITESPACE.sub(" ", str(text)).strip()
    if len(line) <= limit:
        return line
    return line[:limit].rstrip() + " ..."


def _same_id(left: Any, right: Any) -> bool:
    """Ob zwei JSON-RPC-Kennungen dieselbe Anfrage meinen.

    Das Protokoll verlangt denselben Wert samt Typ zurueck. Der Vergleich ueber
    `str` ist die Nachsicht fuer einen Server, der die Zahl als Zeichenkette
    zurueckgibt - das ist ein Schoenheitsfehler seinerseits und kein Grund,
    eine ansonsten richtige Antwort wegzuwerfen.
    """
    return left == right or str(left) == str(right)


def _matching_message(messages: Sequence[Any], request_id: Any) -> Any:
    """Aus den gelesenen Nachrichten die Antwort auf `request_id`.

    Benachrichtigungen (JSON-RPC ohne `id`) werden uebersprungen: sie gehoeren
    zu keiner Anfrage. Bleibt danach nichts Passendes uebrig, ist das ein
    Protokollfehler mit Namen und keine leere Antwort - genau dieser stille
    leere Umschlag hat frueher einen Tag gekostet, ohne dass etwas kaputt war.
    """
    seen: list[str] = []
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        found = message.get("id")
        if found is None:
            continue
        if _same_id(found, request_id):
            return message
        seen.append(repr(found))
    if seen:
        raise SourceError(
            f"trivago: keine Antwort auf Anfrage {request_id!r}, "
            f"gelesen wurden {', '.join(seen)}"
        )
    raise SourceError(
        f"trivago: keine Antwort auf Anfrage {request_id!r}, "
        "der Strom enthielt nur Benachrichtigungen"
    )


def decode_body(text: str, *, request_id: Any = None) -> Any:
    """Den Antwortkoerper lesen, ob JSON oder Server-Sent Events.

    Im SSE-Fall steht die Nutzlast in Zeilen mit dem Praefix `data: `. Gelesen
    werden alle; ein Kommentar-Keepalive (`: ping`) und ein `[DONE]` gehen
    dabei vorbei.

    Mit `request_id` wird die Nachricht zurueckgegeben, deren `id` zur
    gestellten Anfrage passt. Ohne `request_id` - der Handshake-Fall und die
    reine Formatpruefung - bleibt es bei der ersten lesbaren Nachricht.
    """
    stripped = text.strip()
    if not stripped:
        raise SourceError("trivago: leere Antwort")
    if stripped[0] in "{[":
        try:
            messages: list[Any] = [json.loads(stripped)]
        except json.JSONDecodeError as exc:
            raise SourceError(f"trivago: unlesbares JSON: {exc}") from exc
    else:
        messages = []
        for line in stripped.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            chunk = line[len("data:"):].strip()
            if not chunk or chunk == "[DONE]":
                continue
            try:
                messages.append(json.loads(chunk))
            except json.JSONDecodeError:
                continue
        if not messages:
            raise SourceError("trivago: Antwort ohne lesbare JSON-Nutzlast")

    if request_id is None:
        return messages[0]
    return _matching_message(messages, request_id)


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
    also wird sie woertlich weitergegeben statt uns zugeschoben - und als
    `SourceUnstable`, weil genau dieser Satz einen Neuversuch wert ist.
    """
    start = text.find("{")
    if start < 0:
        raise SourceUnstable(f"trivago meldet: {source_message(text)}")
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
        if text.strip():
            raise SourceUnstable(f"trivago meldet: {source_message(text)}")
        # Ohne Text gibt es nichts zu beurteilen. Ein Fehler ohne Aussage ist
        # kein Grund zum Nachfassen, sondern ein Grund, es stehen zu lassen.
        raise SourceError(f"trivago: {TOOL_SEARCH} meldet einen Fehler ohne Text")
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
    # Null Treffer ist eine Auskunft der Quelle und kein Lesefehler. Gemeint ist
    # nur die leere Liste selbst - nicht eine, die erst unser Filter geleert hat.
    batch.empty = not records
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

    def __init__(
        self,
        *,
        url: str = MCP_URL,
        retry_attempts: int = RETRY_ATTEMPTS,
        retry_backoff: Sequence[float] = RETRY_BACKOFF,
        sleep: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.url = url
        self.retry_attempts = max(0, int(retry_attempts))
        self.retry_backoff = tuple(retry_backoff)
        # Injizierbar, damit Tests die Pausen messen statt sie abzusitzen.
        self._sleep = sleep or asyncio.sleep
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
        """Eine Anfrage, notfalls mehrmals gestellt - aber einmal gezaehlt.

        Die Sicherung zaehlt abgewiesene **Aufrufe**, nicht die Wiederholungen
        innerhalb eines Aufrufs. Sonst reichte eine einzige gedrosselte Anfrage
        aus, um mit drei internen Versuchen die Schwelle zu reissen und den
        Rest des Laufs eine halbe Stunde lang zu sperren. 429 ist bei Trivago
        eine Bremse und keine Sperre; die Sicherung soll anschlagen, wenn
        mehrere Aufrufe hintereinander abgewiesen werden.
        """
        if self.breaker.is_open:
            raise SourceBlocked(
                f"trivago: Sicherung offen, noch {self.breaker.remaining():.0f}s"
            )
        last: Exception | None = None
        blocked = False
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
                await self._sleep(retry_after_seconds(None, attempt))
                continue

            session_id = header(response, "mcp-session-id")
            if session_id:
                self._mcp_session_id = session_id

            status = int(getattr(response, "status_code", 0))
            if status == 429:
                # Bleibt Sache dieser Schleife: `Retry-After` und die Sicherung.
                # Der Neuversuch in `search` fasst das ausdruecklich nicht an.
                if not blocked:
                    self.breaker.record_block()
                    blocked = True
                last = SourceBlocked("trivago: HTTP 429")
                await self._sleep(
                    retry_after_seconds(header(response, "retry-after"), attempt)
                )
                continue
            if status == 403:
                if not blocked:
                    self.breaker.record_block()
                raise SourceBlocked("trivago: HTTP 403")
            if status == 404 and self._mcp_session_id:
                # Streamable HTTP meldet so eine verworfene Sitzung. Kein
                # Defekt: die Kennung wegwerfen, dann begruesst der naechste
                # `handshake` neu, statt in dieselbe tote Sitzung zu laufen.
                self._mcp_session_id = None
                raise SessionExpired("trivago: Sitzung verworfen (HTTP 404)")
            if status >= 500:
                last = SourceError(f"trivago: HTTP {status}")
                await self._sleep(retry_after_seconds(None, attempt))
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
        """Eine Anfrage, und die Antwort dazu - nicht irgendeine.

        Der Endpunkt darf vor der Antwort Benachrichtigungen und
        Fortschrittsmeldungen schicken. Die erste lesbare Nachricht ist deshalb
        nicht zwangslaeufig unsere; zugeordnet wird ueber die `id`.
        """
        rpc_id = self._next_id()
        body: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": method,
        }
        if params is not None:
            body["params"] = dict(params)
        response = await self._post(body)
        message = decode_body(str(response.text), request_id=rpc_id)
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

    async def _search_once(self, query: HotelQuery) -> HotelBatch:
        """Ein Versuch. Beginnt mit dem Handshake, der nur beim ersten Mal einer ist.

        `handshake` kehrt sofort zurueck, solange eine Sitzungskennung steht.
        Hat der Server sie verworfen, ist sie hier schon geloescht - und dann
        ist dieser Aufruf genau der noetige neue Handshake.
        """
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

    async def search(self, query: HotelQuery) -> HotelBatch:
        """Eine Suche, notfalls mit Nachfassen.

        Wiederholt wird ausschliesslich `SourceUnstable`: die Quelle hat
        geantwortet, aber mit einem Satz statt mit Daten. Ausdruecklich nicht
        wiederholt werden

        * HTTP 429 - dafuer gibt es `Retry-After` und die Sicherung in `_post`,
          und wer auf eine Bremse hin haeufiger fragt, hat sie nicht verstanden,
        * Protokoll- und Aufbaufehler - die gehen nicht davon weg, dass man
          nochmal fragt, und eine Wiederholung wuerde nur den Bericht trueben,
        * eine leere, aber gueltige Trefferliste - null Hotels ist ein
          Ergebnis. Sie wirft gar nichts und kommt hier nie an.

        Die Neuversuche laufen durch dieselbe Kette wie der erste Versuch, also
        durch `limiter.slot()`: nachfassen heisst nicht vordraengeln.
        """
        attempts = 1 + self.retry_attempts
        for attempt in range(attempts):
            try:
                batch = await self._search_once(query)
            except SourceUnstable as exc:
                # Die Zahl haengt an der Ausnahme, damit auch ein am Ende
                # verlorener Tag im Bericht zeigt, was er gekostet hat.
                exc.retries = attempt
                if attempt + 1 >= attempts:
                    raise
                pause = retry_pause(attempt, self.retry_backoff)
                logger.info(
                    "trivago: Versuch %d fuer %s scheiterte (%s), neuer Versuch in %.1fs",
                    attempt + 1,
                    query.arrival,
                    exc,
                    pause,
                )
                await self._sleep(pause)
            else:
                batch.retries = attempt
                return batch
        raise SourceError("trivago: Suche ohne Versuch")  # pragma: no cover
