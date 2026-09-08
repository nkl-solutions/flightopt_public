"""Einmalige Datenkorrektur: die Flug-Baselines auf eine Grundgesamtheit stellen.

Die 27 Baselines, die nach der Schluessel-Reparatur entstanden, mischten zwei
Dinge, die nicht zusammengehoeren:

* Preise eines Vergleichsportals mit bekanntem systematischem Aufschlag lagen in
  derselben Verteilung wie Direkttarife einzelner Airlines,
* Kalenderschaetzungen lagen in derselben Verteilung wie gepruefte Live-Preise.

Was das Skript tut, in dieser Reihenfolge:

1. `price_observation.is_indicative` anlegen, falls die Spalte fehlt (macht
   `db.connect`), und fuer bestehende Flugzeilen aus dem Quellenkatalog fuellen.
2. `flight_baseline` aus den verbliebenen Beobachtungen neu rechnen, getrennt
   nach Schaetzung und gepruefter Messung.
3. Die alten Flugzeilen aus `price_baseline` entfernen. Niemand liest sie mehr,
   und eine Zahl, die niemand mehr liest, aber jeder noch findet, ist eine
   Falle.

Hotels bleiben unberuehrt: ihre Zeilen in `price_baseline` und `hotel_baseline`
werden weder gefiltert noch geteilt noch geloescht.

Idempotent: ein zweiter Lauf kennzeichnet nichts mehr, loescht nichts mehr und
rechnet dieselben Baselines noch einmal.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flightopt.sources.registry import indicative_source_names
from flightopt.storage import db
from flightopt.storage.baseline import FLIGHT, refresh_flight_baselines

BaselineMap = dict[tuple, tuple[int, int]]
"""Schluessel -> (Median in Minor Units, n)."""


def old_flight_baselines(path: Path) -> BaselineMap:
    """Die Flugzeilen aus `price_baseline`, bevor irgendetwas angefasst wird.

    Eigene, schreibgeschuetzte Verbindung: `db.connect` legt Tabellen an und
    traegt Spalten nach, und ein Vorher-Bild, das schon nachgebessert wurde,
    ist keines.
    """
    if not path.exists():
        return {}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT entity_key, weekday, leadtime_bucket, currency, median_minor, n "
            "FROM price_baseline WHERE entity_type=?",
            (FLIGHT,),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()
    return {
        (r["entity_key"], int(r["weekday"]), r["leadtime_bucket"], r["currency"]):
            (int(r["median_minor"]), int(r["n"]))
        for r in rows
    }


def new_flight_baselines(conn: sqlite3.Connection) -> dict[tuple, tuple[int, int, int]]:
    """Schluessel samt Grundgesamtheit -> (Median, n, is_estimate)."""
    rows = conn.execute(
        "SELECT entity_key, weekday, leadtime_bucket, currency, is_estimate, "
        "median_minor, n FROM flight_baseline"
    ).fetchall()
    return {
        (r["entity_key"], int(r["weekday"]), r["leadtime_bucket"], r["currency"],
         int(r["is_estimate"])):
            (int(r["median_minor"]), int(r["n"]), int(r["is_estimate"]))
        for r in rows
    }


def mark_indicative(conn: sqlite3.Connection, names: set[str]) -> int:
    """Bestehende Flugzeilen indikativer Quellen kennzeichnen.

    Nur Fluege: Hotelquellen tragen ihre Naeherung in `is_estimate`, und ihre
    Baselines haengen daran. Ein Kennzeichen hier wuerde ihnen jede Vergleichs-
    gruppe nehmen, ohne dass irgendwer danach gefragt haette.
    """
    if not names:
        return 0
    # Die Namen kommen aus dem Katalog, nicht von aussen; trotzdem gehen sie
    # als Parameter hinein und nur die Zahl der Platzhalter in den Text.
    placeholders = ",".join("?" for _ in names)
    cur = conn.execute(
        "UPDATE price_observation SET is_indicative=1 "
        f"WHERE entity_type=? AND is_indicative=0 AND source IN ({placeholders})",
        (FLIGHT, *sorted(names)),
    )
    return int(cur.rowcount or 0)


def drop_stale_flight_rows(conn: sqlite3.Connection) -> int:
    """Die alten Flugzeilen aus `price_baseline` wegraeumen."""
    cur = conn.execute("DELETE FROM price_baseline WHERE entity_type=?", (FLIGHT,))
    return int(cur.rowcount or 0)


def report(before: BaselineMap, after: dict[tuple, tuple[int, int, int]]) -> list[str]:
    """Was sich an den Medianen geaendert hat, Strecke fuer Strecke."""
    lines: list[str] = []
    shifts: list[float] = []
    for key in sorted(after):
        median_minor, n, is_estimate = after[key]
        old = before.get(key[:4])
        kind = "schaetzung" if is_estimate else "geprueft"
        entity, weekday, bucket, currency, _ = key
        if old is None:
            lines.append(
                f"  {entity} wd{weekday} {bucket} {kind}: "
                f"neu {median_minor / 100:.2f} {currency} (n={n})"
            )
            continue
        shift = (median_minor - old[0]) / old[0] * 100 if old[0] else 0.0
        shifts.append(shift)
        lines.append(
            f"  {entity} wd{weekday} {bucket} {kind}: "
            f"{old[0] / 100:.2f} (n={old[1]}) -> {median_minor / 100:.2f} "
            f"(n={n}), {shift:+.1f}%"
        )
    if shifts:
        shifts.sort()
        middle = shifts[len(shifts) // 2]
        lines.append(
            f"  Verschiebung: Median {middle:+.1f}%, "
            f"von {shifts[0]:+.1f}% bis {shifts[-1]:+.1f}%"
        )
    return lines


def migrate(conn: sqlite3.Connection, before: BaselineMap) -> dict[str, object]:
    names = indicative_source_names()
    marked = mark_indicative(conn, names)
    conn.execute("DELETE FROM flight_baseline")
    written = refresh_flight_baselines(conn)
    dropped = drop_stale_flight_rows(conn)
    conn.commit()

    after = new_flight_baselines(conn)
    total = conn.execute(
        "SELECT COUNT(*) c FROM price_observation WHERE entity_type=?", (FLIGHT,)
    ).fetchone()["c"]
    indicative = conn.execute(
        "SELECT COUNT(*) c FROM price_observation WHERE entity_type=? AND is_indicative=1",
        (FLIGHT,),
    ).fetchone()["c"]
    return {
        "sources": sorted(names),
        "marked": marked,
        "dropped": dropped,
        "written": written,
        "observations": int(total),
        "indicative": int(indicative),
        "before": before,
        "after": after,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/flightopt.db")
    args = parser.parse_args()
    path = Path(args.db)
    if not path.exists():
        print(f"keine Datenbank unter {path}")
        return 1

    before = old_flight_baselines(path)
    conn = db.connect(path)
    result = migrate(conn, before)
    conn.close()

    print(f"indikative Quellen: {', '.join(result['sources']) or 'keine'}")
    print(f"gekennzeichnet: {result['marked']} Beobachtungen")
    print(
        f"Flugbeobachtungen: {result['observations']}, "
        f"davon indikativ {result['indicative']}, "
        f"verwertbar {result['observations'] - result['indicative']}"
    )
    print(f"price_baseline: {result['dropped']} alte Flugzeilen entfernt")
    print(f"flight_baseline: {len(before)} vorher -> {result['written']} nachher")
    for line in report(result["before"], result["after"]):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
