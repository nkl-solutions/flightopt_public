"""Die Beobachtungsliste: Strecken, deren Preiskalender taeglich mitgeschrieben wird.

Der Unterschied zur gespeicherten Suche steht im Schema (`watch_route` in
`flightopt.storage.db`) und ist der Grund fuer die eigene Tabelle: eine Suche
hat ein festes Reisefenster und laeuft den ganzen teuren Ablauf, eine
Beobachtung hat ein rollendes Vorlauf-Fenster und sammelt nur Kalender ein.

Hier steht nur die Buchfuehrung darueber. Wer die Kalender holt, steht in
`flightopt.jobs.watchlist`.
"""

from __future__ import annotations

import random
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from flightopt.hunt import cadence as hunt_cadence

DEFAULT_LEAD_MIN = 14
DEFAULT_LEAD_MAX = 73
"""Vorgabe-Fenster: 60 Tage, ab zwei Wochen Vorlauf.

Sechzig Tage sind rund sechzig Zeilen je Strecke und Quelle am Tag, und sie
decken alle sieben Wochentage sowie mehrere Vorlauf-Faecher ab. Weniger als
zwei Wochen Vorlauf ist der Bereich, in dem Preise sprunghaft werden - er
gehoert dazu, sobald ein Fenster hineinrollt, aber er ist ein schlechter
Anfang fuer eine Vergleichsbasis.
"""

MAX_LEAD_DAYS = 365
"""Weiter voraus verkauft kaum eine Airline; ein Kalender daraus ist leer."""

MAX_WINDOW_DAYS = 120
"""Die Spanne eines Eintrags. Ein Fenster kostet je Quelle einen Abruf, aber
die Antwort wird mit der Spanne groesser, und die Zeilen landen alle in
derselben Datei. Wer mehr braucht, traegt eine zweite Strecke ein."""

MIN_RECORDING_DAYS = 5
"""Ab wie vielen Aufzeichnungstagen die erste Aussage traegt.

Dieselbe Zahl wie `min_samples` in `refresh_baselines`, und aus demselben
Grund: eine Baseline gilt je Kombination aus Strecke, Wochentag und
Vorlauf-Fenster, und je Tag kommt genau eine Beobachtung je Kombination dazu.
Fuenf Tage Aufzeichnung sind damit fuenf Beobachtungen je Kombination.
"""

IATA = re.compile(r"^[A-Z]{3}$")

FLIGHT = "flight"


@dataclass(frozen=True, slots=True)
class WatchRoute:
    id: int
    origin: str
    destination: str
    lead_min_days: int
    lead_max_days: int
    currency: str
    enabled: bool
    created_at: str
    last_run_at: str | None
    cadence: str = hunt_cadence.DAILY
    last_hot_run_at: str | None = None

    @property
    def hot(self) -> bool:
        """Laeuft diese Strecke im heissen Takt?

        Eigenschaft statt eigener Spalte: der Takt ist eine Auswahl aus zwei
        Werten, und zwei Spalten, die dasselbe sagen, sagen es irgendwann
        verschieden.
        """
        return self.cadence == hunt_cadence.HOT

    @property
    def window_days(self) -> int:
        """Wie viele Reisetage das Fenster dieser Strecke umfasst."""
        return self.lead_max_days - self.lead_min_days + 1

    @property
    def entity_key(self) -> str:
        """Genau der Schluessel, den `build_grid` in die Historie schreibt."""
        return f"{self.origin}|{self.destination}"

    @property
    def label(self) -> str:
        return f"{self.origin}-{self.destination}"

    def window(self, today: date) -> tuple[date, date]:
        """Das Fenster dieses Tages. Morgen ist es dasselbe, einen Tag weiter."""
        return (
            today + timedelta(days=self.lead_min_days),
            today + timedelta(days=self.lead_max_days),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "origin": self.origin,
            "destination": self.destination,
            "route": self.label,
            "lead_min_days": self.lead_min_days,
            "lead_max_days": self.lead_max_days,
            "currency": self.currency,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "last_run_at": self.last_run_at,
            "cadence": self.cadence,
            "hot": self.hot,
            "last_hot_run_at": self.last_hot_run_at,
        }


def _row(row: sqlite3.Row) -> WatchRoute:
    keys = row.keys()
    return WatchRoute(
        id=int(row["id"]),
        origin=str(row["origin"]),
        destination=str(row["destination"]),
        lead_min_days=int(row["lead_min_days"]),
        lead_max_days=int(row["lead_max_days"]),
        currency=str(row["currency"]),
        enabled=bool(row["enabled"]),
        created_at=str(row["created_at"]),
        last_run_at=row["last_run_at"],
        # Eine Datei, in der die Spalten noch fehlen, liest weiter. Nachgetragen
        # werden sie von `db.add_missing_columns`, aber das laeuft erst beim
        # naechsten `connect` - und eine offene Verbindung darf davon nicht
        # abhaengen.
        cadence=str(row["cadence"]) if "cadence" in keys else hunt_cadence.DAILY,
        last_hot_run_at=row["last_hot_run_at"] if "last_hot_run_at" in keys else None,
    )


def _code(value: str, *, field: str) -> str:
    code = str(value or "").strip().upper()
    if not IATA.match(code):
        raise ValueError(f"{field}: {value!r} ist kein IATA-Code aus drei Buchstaben")
    return code


def _check_window(lead_min_days: int, lead_max_days: int) -> tuple[int, int]:
    lo, hi = int(lead_min_days), int(lead_max_days)
    if lo < 0:
        raise ValueError("Der Vorlauf faengt fruehestens heute an.")
    if hi < lo:
        raise ValueError("Das Ende des Vorlauf-Fensters liegt vor seinem Anfang.")
    if hi > MAX_LEAD_DAYS:
        raise ValueError(
            f"Mehr als {MAX_LEAD_DAYS} Tage Vorlauf verkauft kaum eine Airline."
        )
    if hi - lo + 1 > MAX_WINDOW_DAYS:
        raise ValueError(
            f"Das Fenster umfasst {hi - lo + 1} Tage. Je Strecke sind "
            f"{MAX_WINDOW_DAYS} Tage vorgesehen."
        )
    return lo, hi


def add_route(conn: sqlite3.Connection, origin: str, destination: str, *,
              lead_min_days: int = DEFAULT_LEAD_MIN,
              lead_max_days: int = DEFAULT_LEAD_MAX,
              currency: str = "EUR",
              now: datetime | None = None) -> int:
    """Eine Strecke eintragen. Eine schon eingetragene bekommt ihr neues Fenster.

    Kein zweiter Eintrag fuer dieselbe Strecke: das waeren zweimal dieselben
    Abrufe am selben Tag bei denselben Quellen. Wer die Strecke erneut
    eintraegt, meint das Fenster, das er diesmal mitschickt - und meint, dass
    sie wieder laufen soll, auch wenn sie abgeschaltet war.

    Den Takt meint er dagegen nicht: eine heisse Strecke bleibt heiss. Nur
    eine Ausnahme gibt es, und die ist keine Meinung, sondern Arithmetik - ein
    Fenster ueber `MAX_HOT_WINDOW_DAYS` kostet je Durchgang mehr Abrufe, als
    das Budget verbucht. Wer es aufzieht, faellt auf den Tagestakt zurueck.
    """
    code_from = _code(origin, field="Start")
    code_to = _code(destination, field="Ziel")
    if code_from == code_to:
        raise ValueError("Start und Ziel sind derselbe Flughafen.")
    lo, hi = _check_window(lead_min_days, lead_max_days)
    stamp = (now or datetime.now()).isoformat(timespec="seconds")
    hot_ok = int(hi - lo + 1 <= hunt_cadence.MAX_HOT_WINDOW_DAYS)
    conn.execute(
        "INSERT INTO watch_route("
        "origin, destination, lead_min_days, lead_max_days, currency, enabled, created_at"
        ") VALUES(?,?,?,?,?,1,?) "
        "ON CONFLICT(origin, destination) DO UPDATE SET "
        "lead_min_days=excluded.lead_min_days, lead_max_days=excluded.lead_max_days, "
        "currency=excluded.currency, enabled=1, "
        "cadence=CASE WHEN ?=1 THEN watch_route.cadence ELSE ? END",
        (code_from, code_to, lo, hi, currency.strip().upper() or "EUR", stamp,
         hot_ok, hunt_cadence.DAILY),
    )
    row = conn.execute(
        "SELECT id FROM watch_route WHERE origin=? AND destination=?",
        (code_from, code_to),
    ).fetchone()
    return int(row["id"])


def list_routes(conn: sqlite3.Connection) -> list[WatchRoute]:
    return [
        _row(row)
        for row in conn.execute(
            "SELECT * FROM watch_route ORDER BY origin, destination"
        )
    ]


def get_route(conn: sqlite3.Connection, route_id: int) -> WatchRoute | None:
    row = conn.execute("SELECT * FROM watch_route WHERE id=?", (route_id,)).fetchone()
    return _row(row) if row is not None else None


def set_enabled(conn: sqlite3.Connection, route_id: int, enabled: bool) -> WatchRoute:
    """Der Schalter. Eine abgeschaltete Zeile bleibt stehen, samt Historie."""
    cur = conn.execute(
        "UPDATE watch_route SET enabled=? WHERE id=?", (int(bool(enabled)), route_id)
    )
    if not cur.rowcount:
        raise ValueError(f"Unbekannte Strecke: {route_id}")
    route = get_route(conn, route_id)
    assert route is not None  # gerade aktualisiert, also vorhanden
    return route


def set_cadence(conn: sqlite3.Connection, route_id: int, cadence: str) -> WatchRoute:
    """Den Takt einer Strecke umstellen.

    Zwei Werte, mehr nicht. Ein Zwischenwert ("alle fuenf Minuten") waere eine
    Zahl, die keine Obergrenze mehr haelt: das Budget in `flightopt.hunt`
    rechnet mit genau einem heissen Takt, und aus dem folgt, wie viele
    Strecken gleichzeitig heiss laufen koennen.

    Das Fenster wird beim Umschalten geprueft und nicht beim Laufen. Ein
    Fehler beim Umschalten kann ein Mensch lesen; ein Fenster, das im
    Durchgang zu teuer wird, faellt niemandem auf, ausser der Quelle.
    """
    if cadence not in hunt_cadence.CADENCES:
        raise ValueError(
            f"Unbekannter Takt: {cadence!r}. Erlaubt sind "
            f"{', '.join(hunt_cadence.CADENCES)}."
        )
    route = get_route(conn, route_id)
    if route is None:
        raise ValueError(f"Unbekannte Strecke: {route_id}")
    if cadence == hunt_cadence.HOT and route.window_days > hunt_cadence.MAX_HOT_WINDOW_DAYS:
        raise ValueError(
            f"Das Fenster umfasst {route.window_days} Tage. Im heissen Takt sind "
            f"hoechstens {hunt_cadence.MAX_HOT_WINDOW_DAYS} vorgesehen, weil ein "
            f"breiteres Fenster je Durchgang mehr Abrufe kostet, als das Budget "
            f"verbucht."
        )
    conn.execute("UPDATE watch_route SET cadence=? WHERE id=?", (cadence, route_id))
    updated = get_route(conn, route_id)
    assert updated is not None  # gerade aktualisiert, also vorhanden
    return updated


def hot_routes(conn: sqlite3.Connection, *, now: datetime | None = None,
               interval_seconds: int = hunt_cadence.HOT_INTERVAL_SECONDS,
               jitter_seconds: int = hunt_cadence.HOT_JITTER_SECONDS,
               rng: random.Random | None = None) -> list[WatchRoute]:
    """Heisse Strecken, deren letzter Durchgang lang genug her ist.

    Die am laengsten wartende zuerst, die noch nie gelaufene ganz vorn. Das
    Budget verschiebt Strecken, sobald zu viele heiss sind; ohne feste
    Reihenfolge verschoebe es immer dieselben, und eine Strecke am Ende der
    Liste kaeme nie dran.

    Anders als beim Tageslauf zaehlt hier der Abstand in Sekunden und nicht
    der Kalendertag: "alle zwanzig Minuten" ist ein Abstand, "einmal am Tag"
    ist ein Tag.

    Die Streuung wirkt **nur nach hinten**: gewartet wird zwischen
    `interval_seconds` und `interval_seconds + jitter_seconds`. Ein
    gleichmaessiger Schlag auf die Sekunde ist selbst ein Bot-Merkmal - das
    ist dieselbe Ueberlegung, aus der der `RateLimiter` seine Abstaende
    streut. Nach vorn darf sie nicht wirken, sonst haelt der Takt die
    Rechnung nicht mehr ein, aus der die Obergrenze folgt.
    """
    moment = now or datetime.now()
    dice = rng or random
    # Die Abfrage holt jeden Kandidaten, der fruehestens dran sein *koennte*;
    # ueber die Streuung entscheidet danach jede Zeile fuer sich. In SQL waere
    # es ein Wurf fuer den ganzen Stapel, und ein gemeinsamer Versatz ist
    # keine Streuung, sondern eine Verschiebung.
    earliest = (moment - timedelta(seconds=interval_seconds)).isoformat(
        timespec="seconds"
    )
    picked: list[WatchRoute] = []
    for row in conn.execute(
        "SELECT * FROM watch_route WHERE enabled=1 AND cadence=? "
        "AND (last_hot_run_at IS NULL OR last_hot_run_at <= ?) "
        "ORDER BY last_hot_run_at IS NOT NULL, last_hot_run_at, origin, destination",
        (hunt_cadence.HOT, earliest),
    ):
        route = _row(row)
        if route.last_hot_run_at is None:
            picked.append(route)
            continue
        try:
            waited = (moment - datetime.fromisoformat(route.last_hot_run_at)).total_seconds()
        except ValueError:
            # Ein unlesbarer Zeitstempel heisst "unbekannt, wann zuletzt".
            # Faellig zu sein ist dann die vorsichtigere Annahme als nie
            # wieder dranzukommen.
            picked.append(route)
            continue
        if waited >= interval_seconds + dice.uniform(0, jitter_seconds):
            picked.append(route)
    return picked


def mark_hot_ran(conn: sqlite3.Connection, route_id: int, *,
                 now: datetime | None = None) -> None:
    """Ein heisser Durchgang zaehlt auch als der Tageslauf dieser Strecke.

    Er holt denselben Kalender ueber dasselbe Fenster und schreibt dieselben
    Zeilen. Ihn nicht mitzuzaehlen hiesse, dass eine heisse Strecke ihren
    Tageslauf ein zweites Mal bezahlt - bei denselben Quellen, fuer dieselbe
    Antwort.
    """
    stamp = (now or datetime.now()).isoformat(timespec="seconds")
    conn.execute(
        "UPDATE watch_route SET last_hot_run_at=?, last_run_at=? WHERE id=?",
        (stamp, stamp, route_id),
    )


def due_routes(conn: sqlite3.Connection, *,
               now: datetime | None = None) -> list[WatchRoute]:
    """Was heute noch nicht gelaufen ist.

    Der Vergleich laeuft ueber den Kalendertag und nicht ueber einen Abstand
    in Stunden: "einmal am Tag" soll sich nicht verschieben, nur weil ein Lauf
    einmal spaeter dran war. Ein Neustart des Dienstes loest damit auch keinen
    zweiten Abruf aus.
    """
    today = (now or datetime.now()).date().isoformat()
    return [
        _row(row)
        for row in conn.execute(
            "SELECT * FROM watch_route WHERE enabled=1 "
            "AND (last_run_at IS NULL OR substr(last_run_at, 1, 10) < ?) "
            "ORDER BY origin, destination",
            (today,),
        )
    ]


def mark_ran(conn: sqlite3.Connection, route_id: int, *,
             now: datetime | None = None) -> None:
    """Gelaufen heisst gelaufen, auch wenn keine Quelle etwas hergab.

    Sonst versucht derselbe Eintrag es im naechsten Durchgang wieder, und aus
    einer stummen Strecke wird ein Dauerfeuer gegen eine Quelle, die ohnehin
    nichts liefert.
    """
    conn.execute(
        "UPDATE watch_route SET last_run_at=? WHERE id=?",
        ((now or datetime.now()).isoformat(timespec="seconds"), route_id),
    )


def route_stats(conn: sqlite3.Connection, route: WatchRoute) -> dict[str, Any]:
    """Was die Aufzeichnung dieser Strecke bisher gebracht hat.

    `days_recorded` zaehlt Kalendertage mit mindestens einer Beobachtung, nicht
    Beobachtungen: fuenfzig Zeilen an einem Tag sind fuenfzig Reisetage und
    trotzdem nur ein Messpunkt je Kombination. Genau daran haengt, ab wann eine
    Aussage traegt, und deshalb steht diese Zahl neben der grossen.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n, COUNT(DISTINCT substr(observed_at, 1, 10)) AS days, "
        "MAX(observed_at) AS last FROM price_observation "
        "WHERE entity_type=? AND entity_key=?",
        (FLIGHT, route.entity_key),
    ).fetchone()
    observations = int(row["n"] or 0)
    days = int(row["days"] or 0)
    # Die fertig gerechneten Baselines dieser Strecke. Sie entstehen erst,
    # wenn `refresh_baselines` gelaufen ist; steht hier eine Null bei vielen
    # Aufzeichnungstagen, fehlt die Rechnung und nicht die Grundlage.
    baselines = conn.execute(
        "SELECT COUNT(*) AS n FROM flight_baseline WHERE entity_key=?",
        (route.entity_key,),
    ).fetchone()
    return {
        "observations": observations,
        "days_recorded": days,
        "last_observation": row["last"] if observations else None,
        "baselines": int(baselines["n"] or 0),
        "min_days": MIN_RECORDING_DAYS,
        "ready": days >= MIN_RECORDING_DAYS,
    }


def routes_with_stats(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Die Liste, wie sie API und Oberflaeche brauchen: Zeile samt Ertrag."""
    return [{**route.as_dict(), **route_stats(conn, route)} for route in list_routes(conn)]


def watchlist_report(conn: sqlite3.Connection, *,
                     now: datetime | None = None) -> dict[str, Any]:
    """Der kurze Stand fuer den Betriebsbericht.

    Null Strecken ist die wichtigste Zahl davon: sie sagt, dass gerade nichts
    aufgezeichnet wird - und das sieht sonst genauso aus wie eine Aufzeichnung
    ohne Treffer.
    """
    routes = list_routes(conn)
    keys = [route.entity_key for route in routes]
    observations = 0
    if keys:
        marks = ",".join("?" for _ in keys)
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM price_observation "
            f"WHERE entity_type=? AND entity_key IN ({marks})",
            (FLIGHT, *keys),
        ).fetchone()
        observations = int(row["n"] or 0)
    runs = [route.last_run_at for route in routes if route.last_run_at]
    return {
        "routes": len(routes),
        "active": sum(1 for route in routes if route.enabled),
        "due": len(due_routes(conn, now=now)),
        "observations": observations,
        "last_run_at": max(runs) if runs else None,
        "min_days": MIN_RECORDING_DAYS,
        # Null heisse Strecken sieht sonst genauso aus wie eine Jagd, die
        # nichts findet - und das ist ein ganz anderer Befund.
        "hot": sum(1 for route in routes if route.enabled and route.hot),
        "hot_due": len(hot_routes(conn, now=now)),
        "hot_interval_seconds": hunt_cadence.HOT_INTERVAL_SECONDS,
        "max_hot_routes": hunt_cadence.MAX_HOT_ROUTES,
    }
