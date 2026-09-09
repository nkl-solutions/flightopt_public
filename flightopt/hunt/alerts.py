"""Der Fund: aufschreiben, entdoppeln, melden.

`alert_rule` und `alert_event` standen im Masterplan (3.5) seit Anfang an als
Entwurf. Gebaut sind sie jetzt, und zwar in dieser Aufteilung:

* **`alert_rule`** sagt, wofuer gemeldet wird. Ohne eine einzige Zeile gilt
  `DEFAULT_RULE`: jede Strecke, Stufe `error`. Eine Regel ist Feineinstellung
  und keine Voraussetzung - waere sie eine, liefe ein frisch aufgesetzter
  Dienst still und niemand wuesste warum.
* **`alert_event`** haelt jeden Fund fest, auch den nicht gemeldeten.
  `delivery` sagt, was mit ihm geschehen ist: `sent`, `dry_run`, `suppressed`
  oder `failed`. Ohne diese Zeile liesse sich der Kanal nicht beobachten,
  bevor er reden darf.

## Die Entdopplung

Derselbe Fund darf nicht alle zwanzig Minuten erneut melden. Entdoppelt wird
ueber **Strecke, Reisetag und Preisstufe** - genau die drei Angaben, die einen
Fund ausmachen. Der Preis gehoert bewusst *nicht* in den Schluessel: dieselbe
Verbindung fuer 39,00 statt 39,50 Euro ist derselbe Fund und keine Nachricht.

Die Ruhezeit ist **sechs Stunden**, und die Zahl kommt aus dem Takt. Bei
zwanzig Minuten Abstand meldete ein Fund, der einen Vormittag lang buchbar
bleibt, ohne Ruhezeit achtzehnmal, bevor die erste Stunde um ist. Sechs
Stunden sind lang genug, dass daraus eine Meldung wird, und kurz genug, dass
ein Tarif, der am Abend noch steht, noch einmal erinnert. Hoechstens vier
Meldungen je Strecke, Tag und Stufe an einem Tag.

Eine Ausnahme gibt es, und die ist ein echter Zugewinn an Information: faellt
der Preis um mindestens `UNDERCUT_SHARE` unter den zuletzt gemeldeten, ist das
ein neuer Fund und keine Wiederholung. Von 39 auf 25 Euro will man wissen,
von 39 auf 38,50 nicht.

Gezaehlt wird die Ruhezeit ab der letzten **gemeldeten** Zeile, nicht ab der
letzten geschriebenen. Sonst schoebe jede unterdrueckte Meldung die Ruhezeit
vor sich her, und bei einem Takt von zwanzig Minuten waere dieselbe Zeile nie
wieder zu hoeren.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from flightopt.hunt import discord

logger = logging.getLogger(__name__)

SENT = "sent"
DRY_RUN = "dry_run"
SUPPRESSED = "suppressed"
FAILED = "failed"
DELIVERIES: tuple[str, ...] = (SENT, DRY_RUN, SUPPRESSED, FAILED)

REPORTED: tuple[str, ...] = (SENT, DRY_RUN)
"""Zustaende, die als "gemeldet" zaehlen und die Ruhezeit starten.

Ein Trockenlauf zaehlt mit: er ist die Probe fuer den spaeteren Betrieb, und
eine Probe, die anders entdoppelt als der Ernstfall, probt das Falsche.
`failed` zaehlt nicht mit - ein Versand, der nicht ankam, hat niemanden
geweckt und darf den naechsten nicht blockieren.
"""

QUIET = timedelta(hours=6)
UNDERCUT_SHARE = 0.20
"""Um so viel muss ein Preis den zuletzt gemeldeten unterbieten, um innerhalb
der Ruhezeit als neuer Fund zu gelten. Zwanzig Prozent sind mehr als das
uebliche Auf und Ab eines Tarifs und weniger als ein zweiter Fehler."""

CHANNEL_DISCORD = "discord"

TIER_ERROR = "error"
DEFAULT_TIERS: tuple[str, ...] = (TIER_ERROR,)
"""Was ohne eigene Regel gemeldet wird. Nur die vierte Stufe.

`cheap` waere die naheliegende zweite Wahl und ist die falsche: eine
Beobachtungsliste mit fuenf heissen Strecken erzeugt jeden Tag Dutzende
guenstiger Tage, und eine Meldung, die jeden Tag kommt, liest niemand.
"""

ANY_ROUTE = "*"

BOOKING_LINKS: Mapping[str, str] = {
    "ryanair": (
        "https://www.ryanair.com/gb/en/trip/flights/select"
        "?adults=1&dateOut={day}&originIata={origin}&destinationIata={destination}"
    ),
    "wizz": "https://www.wizzair.com/en-gb/booking/select-flight/{origin}/{destination}/{day}",
}
"""Direkte Buchungsstrecken, soweit sie bekannt sind.

Nur zwei, und das ist Absicht: ein Kalenderpreis nennt keinen Flug, sondern
den guenstigsten Preis eines Tages. Eine Buchungsstrecke, die auf einen
bestimmten Flug zeigt, waere geraten. Diese beiden zeigen auf die Tagesliste
der Airline, und das ist genau das, was der Preis beschreibt.
"""

FALLBACK_LINK = (
    "https://www.google.com/travel/flights"
    "?q=Flights%20from%20{origin}%20to%20{destination}%20on%20{day}"
)
"""Fuer alle anderen Quellen eine Suche statt eines Tarifs. Sie enthaelt nur
Strecke und Datum - nichts, was jemanden identifiziert."""

WEEKDAYS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")


@dataclass(frozen=True, slots=True)
class Rule:
    """Wofuer gemeldet wird. `id is None` heisst: die eingebaute Vorgabe."""

    entity_key: str
    tiers: tuple[str, ...]
    channel: str = CHANNEL_DISCORD
    id: int | None = None

    def matches(self, entity_key: str, tier: str) -> bool:
        if self.entity_key not in (ANY_ROUTE, entity_key):
            return False
        return tier in self.tiers


DEFAULT_RULE = Rule(entity_key=ANY_ROUTE, tiers=DEFAULT_TIERS)


@dataclass(frozen=True, slots=True)
class Find:
    """Ein Fund, so wie ihn die Meldung braucht.

    Bewusst eine eigene Form und nicht das Urteil aus `detect_price_signal`:
    dort steht, was der Detektor gerechnet hat, hier steht, was in der
    Nachricht landet. Die eine Form fuer beides zu benutzen hiesse, jede
    Aenderung an der Nachricht am Detektor vorzunehmen.
    """

    entity_key: str
    travel_date: date
    tier: str
    price_minor: int
    currency: str = "EUR"
    source: str = ""
    median_minor: int | None = None
    n: int = 0
    population: str = "estimate"
    reason: str = ""
    distance_km: float | None = None
    thin: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def origin(self) -> str:
        return str(self.entity_key).partition("|")[0]

    @property
    def destination(self) -> str:
        return str(self.entity_key).partition("|")[2]

    @property
    def route(self) -> str:
        return f"{self.origin}-{self.destination}"

    @property
    def booking_url(self) -> str:
        template = BOOKING_LINKS.get(self.source, FALLBACK_LINK)
        return template.format(
            origin=self.origin,
            destination=self.destination,
            day=self.travel_date.isoformat(),
        )


def _stamp(moment: datetime | None) -> str:
    return (moment or datetime.now()).isoformat(timespec="seconds")


def _euro(minor: int | None) -> str:
    """Betrag in deutscher Schreibweise. Kein Tausenderpunkt, kein Symbol."""
    if minor is None:
        return "unbekannt"
    return f"{minor / 100:.2f}".replace(".", ",") + " Euro"


def _german_date(day: date) -> str:
    return f"{WEEKDAYS[day.weekday()]}, {day.strftime('%d.%m.%Y')}"


# -- Regeln -------------------------------------------------------------------


def add_rule(conn: sqlite3.Connection, entity_key: str, *,
             tiers: list[str] | None = None, channel: str = CHANNEL_DISCORD,
             now: datetime | None = None) -> int:
    """Eine Regel anlegen oder ihre Bedingung ersetzen."""
    condition = json.dumps({"tiers": list(tiers or DEFAULT_TIERS)})
    conn.execute(
        "INSERT INTO alert_rule(entity_key, condition, channel, active, created_at) "
        "VALUES(?,?,?,1,?) ON CONFLICT(entity_key, channel) DO UPDATE SET "
        "condition=excluded.condition, active=1",
        (entity_key, condition, channel, _stamp(now)),
    )
    row = conn.execute(
        "SELECT id FROM alert_rule WHERE entity_key=? AND channel=?",
        (entity_key, channel),
    ).fetchone()
    return int(row["id"])


def set_rule_active(conn: sqlite3.Connection, rule_id: int, active: bool) -> None:
    cur = conn.execute(
        "UPDATE alert_rule SET active=? WHERE id=?", (int(bool(active)), rule_id)
    )
    if not cur.rowcount:
        raise ValueError(f"Unbekannte Regel: {rule_id}")


def _rule(row: sqlite3.Row) -> Rule:
    try:
        condition = json.loads(row["condition"] or "{}")
    except json.JSONDecodeError:
        # Eine unlesbare Bedingung ist ein Konfigurationsfehler und darf die
        # ganze Jagd nicht anhalten. Sie faellt auf die Vorgabe zurueck, und
        # das steht im Log.
        logger.warning("alert_rule %s: Bedingung unlesbar", row["id"])
        condition = {}
    tiers = condition.get("tiers") or list(DEFAULT_TIERS)
    return Rule(
        entity_key=str(row["entity_key"]),
        tiers=tuple(str(tier) for tier in tiers),
        channel=str(row["channel"]),
        id=int(row["id"]),
    )


def active_rules(conn: sqlite3.Connection) -> list[Rule]:
    return [
        _rule(row)
        for row in conn.execute(
            "SELECT * FROM alert_rule WHERE active=1 ORDER BY entity_key, channel"
        )
    ]


def matching_rule(conn: sqlite3.Connection, find: Find) -> Rule | None:
    """Die erste Regel, die auf diesen Fund passt, oder die Vorgabe.

    Die Vorgabe greift nur, solange **gar keine** Regel eingetragen ist.
    Sobald jemand eine anlegt, hat er eine Meinung geaeussert, und eine
    eingebaute Vorgabe daneben waere eine zweite. Auch eine abgeschaltete
    Regel ist eine Meinung: wer sie abschaltet, will Ruhe und keine
    Ersatzregel.
    """
    known = conn.execute("SELECT COUNT(*) AS n FROM alert_rule").fetchone()
    if not int(known["n"] or 0):
        rule = DEFAULT_RULE
        return rule if rule.matches(find.entity_key, find.tier) else None
    for rule in active_rules(conn):
        if rule.matches(find.entity_key, find.tier):
            return rule
    return None


# -- Entdopplung --------------------------------------------------------------


def last_reported(conn: sqlite3.Connection, find: Find) -> sqlite3.Row | None:
    """Die letzte tatsaechlich gemeldete Zeile zu Strecke, Tag und Stufe."""
    marks = ",".join("?" for _ in REPORTED)
    return conn.execute(
        f"SELECT * FROM alert_event WHERE entity_key=? AND travel_date=? AND tier=? "
        f"AND delivery IN ({marks}) ORDER BY created_at DESC, id DESC LIMIT 1",
        (find.entity_key, find.travel_date.isoformat(), find.tier, *REPORTED),
    ).fetchone()


def suppressed(conn: sqlite3.Connection, find: Find, *,
               now: datetime | None = None) -> tuple[bool, str]:
    """Darf dieser Fund melden? Zurueck kommt das Urteil samt Begruendung."""
    previous = last_reported(conn, find)
    if previous is None:
        return False, ""
    moment = now or datetime.now()
    try:
        last = datetime.fromisoformat(str(previous["created_at"]))
    except ValueError:
        # Ein unlesbarer Zeitstempel darf keine Meldung verhindern. Im Zweifel
        # lieber einmal zu viel melden als eine Zeile fuer immer stummschalten.
        logger.warning("alert_event %s: created_at unlesbar", previous["id"])
        return False, ""
    if moment - last >= QUIET:
        return False, ""
    threshold = round(int(previous["price_minor"]) * (1 - UNDERCUT_SHARE))
    if find.price_minor <= threshold:
        return False, ""
    hours = QUIET.total_seconds() / 3600
    return True, (
        f"Ruhezeit von {hours:.0f} Stunden laeuft noch, zuletzt gemeldet "
        f"{previous['created_at']} zu {_euro(int(previous['price_minor']))}"
    )


# -- Die Nachricht ------------------------------------------------------------


def _population_label(population: str) -> str:
    return "live geprueft" if population == "verified" else "Kalenderpreis"


def message(find: Find) -> str:
    """Die Meldung, wie sie in Discord steht. Deutsch, ohne Pfeile und Striche.

    Fuenf Zeilen und immer dieselben fuenf: Was, Wieviel, Warum, Worauf, Wohin.
    Eine Meldung, deren Form sich je nach Fund aendert, muss jedes Mal neu
    gelesen werden.
    """
    lines = [
        f"Fehltarif {find.route} am {_german_date(find.travel_date)}",
        f"{_euro(find.price_minor)} ({find.source or 'unbekannte Quelle'}, "
        f"{_population_label(find.population)})",
        f"Warum: {find.reason or 'ohne Begruendung'}",
    ]
    if find.median_minor:
        basis = (
            f"Basis: Median {_euro(find.median_minor)} aus {find.n} Vergleichspreisen "
            f"({_population_label(find.population)})"
        )
        if find.thin:
            basis += ", duenne Basis"
    else:
        basis = "Basis: keine Historie, das Urteil steht allein auf der Entfernung"
    lines.append(basis)
    if find.distance_km:
        lines.append(f"Strecke: {find.distance_km:.0f} km")
    lines.append(f"Buchen: {find.booking_url}")
    return discord.clip("\n".join(lines))


# -- Ereignisse ---------------------------------------------------------------


def record(conn: sqlite3.Connection, find: Find, *, delivery: str,
           rule_id: int | None = None, error: str | None = None,
           now: datetime | None = None) -> int:
    """Einen Fund festhalten. Auch den unterdrueckten und den gescheiterten."""
    moment = _stamp(now)
    cur = conn.execute(
        "INSERT INTO alert_event("
        "rule_id, created_at, entity_key, travel_date, tier, source, currency, "
        "price_minor, median_minor, n, population, reason, detail, delivery, "
        "delivered_at, error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            rule_id, moment, find.entity_key, find.travel_date.isoformat(),
            find.tier, find.source, find.currency, int(find.price_minor),
            find.median_minor, int(find.n), find.population, find.reason,
            json.dumps({**find.detail, "distance_km": find.distance_km,
                        "thin": find.thin, "booking_url": find.booking_url}),
            delivery,
            moment if delivery == SENT else None,
            error,
        ),
    )
    return int(cur.lastrowid)


def _event(row: sqlite3.Row) -> dict[str, Any]:
    try:
        detail = json.loads(row["detail"] or "{}")
    except json.JSONDecodeError:
        detail = {}
    return {
        "id": int(row["id"]),
        "created_at": str(row["created_at"]),
        "route": f"{str(row['entity_key']).replace('|', '-')}",
        "entity_key": str(row["entity_key"]),
        "travel_date": str(row["travel_date"]),
        "tier": str(row["tier"]),
        "source": str(row["source"] or ""),
        "currency": str(row["currency"]),
        "price": int(row["price_minor"]) / 100,
        "price_minor": int(row["price_minor"]),
        "median": (int(row["median_minor"]) / 100) if row["median_minor"] else None,
        "n": int(row["n"] or 0),
        "population": str(row["population"] or ""),
        "reason": str(row["reason"] or ""),
        "delivery": str(row["delivery"]),
        "delivered_at": row["delivered_at"],
        "error": row["error"],
        "acknowledged_at": row["acknowledged_at"],
        "booking_url": detail.get("booking_url", ""),
        "distance_km": detail.get("distance_km"),
        "thin": bool(detail.get("thin")),
    }


def get_event(conn: sqlite3.Connection, event_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM alert_event WHERE id=?", (event_id,)).fetchone()
    return _event(row) if row is not None else None


def list_events(conn: sqlite3.Connection, *, limit: int = 50,
                open_only: bool = False,
                tier: str | None = None) -> list[dict[str, Any]]:
    """Die juengsten Funde. `open_only` blendet abgehakte aus."""
    clauses: list[str] = []
    params: list[Any] = []
    if open_only:
        clauses.append("acknowledged_at IS NULL")
    if tier:
        clauses.append("tier=?")
        params.append(tier)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(int(limit))
    return [
        _event(row)
        for row in conn.execute(
            f"SELECT * FROM alert_event {where} "
            f"ORDER BY created_at DESC, id DESC LIMIT ?",
            params,
        )
    ]


def acknowledge(conn: sqlite3.Connection, event_id: int, acknowledged: bool,
                *, now: datetime | None = None) -> dict[str, Any]:
    """Einen Fund abhaken oder wieder aufmachen. Geloescht wird nichts."""
    cur = conn.execute(
        "UPDATE alert_event SET acknowledged_at=? WHERE id=?",
        (_stamp(now) if acknowledged else None, event_id),
    )
    if not cur.rowcount:
        raise ValueError(f"Unbekannter Fund: {event_id}")
    event = get_event(conn, event_id)
    assert event is not None  # gerade aktualisiert, also vorhanden
    return event


def summary(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict[str, Any]:
    """Der kurze Stand fuer Betriebsbericht und Oberflaeche."""
    counts = {
        str(row["delivery"]): int(row["n"])
        for row in conn.execute(
            "SELECT delivery, COUNT(*) AS n FROM alert_event GROUP BY delivery"
        )
    }
    row = conn.execute(
        "SELECT COUNT(*) AS n, MAX(created_at) AS last FROM alert_event "
        "WHERE acknowledged_at IS NULL"
    ).fetchone()
    return {
        "events": sum(counts.values()),
        "sent": counts.get(SENT, 0),
        "dry_run": counts.get(DRY_RUN, 0),
        "suppressed": counts.get(SUPPRESSED, 0),
        "failed": counts.get(FAILED, 0),
        "open": int(row["n"] or 0),
        "last_find_at": row["last"],
        "quiet_hours": QUIET.total_seconds() / 3600,
        "channel_configured": discord.configured(),
    }


# -- Der ganze Weg ------------------------------------------------------------


async def deliver(conn: sqlite3.Connection, find: Find, *,
                  env: Mapping[str, str] | None = None,
                  post: Any = None, sleep: Any = None,
                  now: datetime | None = None) -> int | None:
    """Einen Fund pruefen, festhalten und - wenn er darf - melden.

    Zurueck kommt die Kennung der geschriebenen Zeile, oder None, wenn keine
    Regel den Fund haben wollte. Geschrieben wird in jedem anderen Fall, auch
    wenn nichts hinausging: `alert_event` ist die Chronik der Jagd und nicht
    das Versandprotokoll.

    Wirft nie. Ein Fehlschlag im Kanal beendet die Zeile mit `failed`, und der
    Durchgang laeuft weiter - die Aufzeichnung ist unwiederbringlich, die
    Meldung nicht.
    """
    rule = matching_rule(conn, find)
    if rule is None:
        return None
    blocked, why = suppressed(conn, find, now=now)
    if blocked:
        logger.info("Fund %s %s unterdrueckt: %s", find.route, find.travel_date, why)
        return record(conn, find, delivery=SUPPRESSED, rule_id=rule.id,
                      error=why, now=now)

    result = await discord.send(message(find), env=env, post=post, sleep=sleep)
    if result.dry_run:
        return record(conn, find, delivery=DRY_RUN, rule_id=rule.id, now=now)
    if result.delivered:
        return record(conn, find, delivery=SENT, rule_id=rule.id, now=now)
    return record(conn, find, delivery=FAILED, rule_id=rule.id,
                  error=result.error, now=now)
