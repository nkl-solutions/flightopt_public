"""Das Hauptbuch der Jagd: was eine Quelle abbekommen hat, und wer gesperrt hat.

Zwei Fragen, zwei Tabellen, beide klein und beide Betriebsmittel statt Archiv.

**`hunt_call`** zaehlt verbuchte Abrufe je Quelle. Gefragt wird immer nach der
**rollenden** Stunde und nie nach der Kalenderstunde: eine Kalenderstunde
erlaubt das volle Budget um 11:59 und noch einmal um 12:00, und genau diesen
Doppelschlag sieht die Quelle. Eine Zeile je Durchgang und Quelle ist bei
fuenfzehn Durchgaengen in der Stunde und zehn Quellen rund hundertfuenfzig
Zeilen; `purge` raeumt sie wieder weg.

**`hunt_pause`** haelt fest, dass eine Quelle uns abgewiesen hat. Solange
dort eine Zeile in der Zukunft steht, laeuft **die ganze Jagd** wieder im
Tagestakt - nicht nur die Quelle, die abgewiesen hat. Das sieht auf den
ersten Blick zu streng aus, ist aber die einzige Lesart, die zum Anlass
passt: ein Block ist der Hinweis, dass unser Fussabdruck auffaellt. Die
anderen Quellen in dieser Lage weiter dreimal die Stunde zu fragen waere
genau die Gier, die den naechsten Block holt. Ein Fehltarif, der waehrend
einer halben Stunde durchrutscht, ist billiger als eine Quelle, die auch der
Suche fehlt.

Der `CircuitBreaker` der Quelle bleibt davon unberuehrt und macht weiter
seine Arbeit im laufenden Prozess. Diese Tabelle ist sein Gedaechtnis ueber
den Prozess hinaus: der Quellen-Katalog wird je Durchgang neu gebaut, also
faengt jeder Durchgang sonst mit einer frischen, geschlossenen Sicherung an
und lernt nichts aus dem letzten.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from flightopt.hunt.cadence import MAX_CALLS_PER_SOURCE_HOUR

WINDOW = timedelta(hours=1)
"""Die rollende Stunde, gegen die verrechnet wird."""

KEEP = timedelta(hours=2)
"""Wie lange eine Buchung stehen bleibt. Doppelt so lang wie das Fenster,
damit ein Lauf ohne Aufraeumen nicht sofort falsch rechnet."""

PAUSE_COOLDOWN = timedelta(seconds=1800)
"""Wie lange die Jagd nach einem Block im Tagestakt bleibt.

Dieselbe halbe Stunde, die `CircuitBreaker.cooldown` fuer die Quelle selbst
ansetzt. Zwei verschiedene Zahlen fuer dieselbe Abkuehlung waeren zwei
Meinungen darueber, wann eine Quelle sich beruhigt hat.
"""


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def used(conn: sqlite3.Connection, source: str, *,
         now: datetime | None = None) -> int:
    """Verbuchte Abrufe dieser Quelle in der letzten Stunde."""
    moment = now or datetime.now()
    row = conn.execute(
        "SELECT COALESCE(SUM(calls), 0) AS n FROM hunt_call "
        "WHERE source=? AND charged_at > ?",
        (source, _stamp(moment - WINDOW)),
    ).fetchone()
    return int(row["n"] or 0)


def remaining(conn: sqlite3.Connection, source: str, *,
              now: datetime | None = None) -> int:
    return max(0, MAX_CALLS_PER_SOURCE_HOUR - used(conn, source, now=now))


def affordable(conn: sqlite3.Connection, sources: list[str], calls: int, *,
               now: datetime | None = None) -> bool:
    """Haben **alle** genannten Quellen noch Platz fuer diesen Durchgang?

    Alle, nicht die meisten: ein Durchgang fragt jede Kalenderquelle. Wer eine
    ueberspringt, weil ihr Budget leer ist, holt fuer diese Strecke einen
    Kalender ohne sie - und die Preishistorie bekaeme eine Luecke, die spaeter
    niemand mehr von einem echten Angebotsmangel unterscheiden kann.
    """
    return all(remaining(conn, name, now=now) >= calls for name in sources)


def charge(conn: sqlite3.Connection, sources: list[str], calls: int, *,
           now: datetime | None = None) -> None:
    """Einen Durchgang verbuchen. Vor dem Abruf, nicht danach.

    Vorher, weil ein Abruf, der in einem Zeitablauf endet, die Quelle genauso
    erreicht hat wie einer, der antwortet. Wer erst nach der Antwort verbucht,
    verbucht ausgerechnet die Abrufe nicht, die schiefgehen.
    """
    moment = _stamp(now or datetime.now())
    conn.executemany(
        "INSERT INTO hunt_call(charged_at, source, calls) VALUES(?,?,?)",
        [(moment, name, int(calls)) for name in sources],
    )


def purge(conn: sqlite3.Connection, *, now: datetime | None = None) -> int:
    """Alte Buchungen wegraeumen. Zurueck kommt die Zahl der Zeilen."""
    cur = conn.execute(
        "DELETE FROM hunt_call WHERE charged_at <= ?",
        (_stamp((now or datetime.now()) - KEEP),),
    )
    return int(cur.rowcount or 0)


def report(conn: sqlite3.Connection, sources: list[str], *,
           now: datetime | None = None) -> list[dict[str, Any]]:
    """Je Quelle: verbraucht, uebrig, Grenze. Fuer `/api/health/detail`."""
    moment = now or datetime.now()
    rows: list[dict[str, Any]] = []
    for name in sorted(sources):
        spent = used(conn, name, now=moment)
        rows.append(
            {
                "source": name,
                "used": spent,
                "remaining": max(0, MAX_CALLS_PER_SOURCE_HOUR - spent),
                "cap": MAX_CALLS_PER_SOURCE_HOUR,
            }
        )
    return rows


def pause(conn: sqlite3.Connection, sources: list[str], *, reason: str,
          now: datetime | None = None) -> None:
    """Eine Quelle hat abgewiesen. Die Jagd faellt auf den Tagestakt zurueck.

    Ein zweiter Block derselben Quelle verlaengert die Sperre, statt eine
    zweite Zeile anzulegen: es geht um "seit wann ruhig", nicht um "wie oft
    abgewiesen".
    """
    moment = now or datetime.now()
    until = _stamp(moment + PAUSE_COOLDOWN)
    for name in sources:
        conn.execute(
            "INSERT INTO hunt_pause(source, since, until, reason) VALUES(?,?,?,?) "
            "ON CONFLICT(source) DO UPDATE SET until=excluded.until, "
            "reason=excluded.reason",
            (name, _stamp(moment), until, reason),
        )


def paused_sources(conn: sqlite3.Connection, *,
                   now: datetime | None = None) -> list[dict[str, Any]]:
    """Wer gerade sperrt. Leere Liste heisst: der heisse Takt ist erlaubt."""
    moment = _stamp(now or datetime.now())
    return [
        {
            "source": str(row["source"]),
            "since": str(row["since"]),
            "until": str(row["until"]),
            "reason": str(row["reason"]),
        }
        for row in conn.execute(
            "SELECT source, since, until, reason FROM hunt_pause "
            "WHERE until > ? ORDER BY source",
            (moment,),
        )
    ]


def hot_allowed(conn: sqlite3.Connection, *, now: datetime | None = None) -> bool:
    """Darf ueberhaupt jemand im heissen Takt laufen?"""
    return not paused_sources(conn, now=now)


def clear_pauses(conn: sqlite3.Connection, *, now: datetime | None = None) -> int:
    """Abgelaufene Sperren loeschen. Zurueck kommt die Zahl der Zeilen.

    Abgelaufen heisst abgelaufen; `paused_sources` liest ohnehin nur die
    gueltigen. Geraeumt wird trotzdem, damit die Tabelle nicht als Chronik der
    Sperren missverstanden wird - dafuer gibt es das Log.
    """
    cur = conn.execute(
        "DELETE FROM hunt_pause WHERE until <= ?",
        (_stamp(now or datetime.now()),),
    )
    return int(cur.rowcount or 0)
