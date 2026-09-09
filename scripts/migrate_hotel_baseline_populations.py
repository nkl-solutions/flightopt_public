"""Einmalige Datenkorrektur: die Hotel-Baselines je Grundgesamtheit trennen.

`hotel_baseline` legte Richtwerte und Haendlerpreise in dieselbe Verteilung.
Das sind zwei Produkte: ein Vergleichsportal nennt einen Richtwert, Booking
zeigt die Zahl, die der Haendler selbst nimmt. Der Median einer gemischten
Verteilung misst weder das eine noch das andere.

Bei den Fluegen hat genau diese Trennung die Mediane deutlich verschoben, und
der Fehler davor war monatelang unsichtbar: keine Ausnahme, kein Log, nur eine
Zahl, die daneben lag. Deshalb steht hier ein Bericht und nicht nur ein
"fertig" - was sich verschoben hat, gehoert vor Augen.

Was das Skript tut, in dieser Reihenfolge:

1. Das Vorher-Bild lesen, auf einer eigenen, schreibgeschuetzten Verbindung.
2. Die Tabelle neu anlegen, falls sie noch die alte Form ohne `population`
   hat, und ihren Inhalt verwerfen.
3. Aus den Beobachtungen neu rechnen, jetzt getrennt nach `is_estimate`.

**Die alten Zeilen werden nicht uebernommen, sondern verworfen.** Welcher
Grundgesamtheit eine gemischte Zeile angehoert, ist keine Frage mit einer
Antwort - sie gehoert beiden an. Sie einer davon zuzuschlagen waere genau die
Luege, die dieser Umbau abstellt. Gefahrlos ist es, weil `hotel_baseline`
abgeleitet ist: die Beobachtungen tragen `is_estimate` je Zeile, und daraus
entsteht in Sekunden alles neu.

**Alles in einer Transaktion.** `db.connect` stellt auf `isolation_level=None`,
jede Anweisung waere also sofort endgueltig. Brach das Neurechnen nach dem
Neubau der Tabelle ab, waeren die alten Baselines weg und die neuen nicht da -
und bis die Beobachtungen wieder zu Baselines geworden sind, misst kein Preis
mehr gegen irgendetwas.

**Der Preis der Trennung.** Jede Grundgesamtheit braucht ihre eigenen fuenf
Beobachtungen je Gruppe. Wo eine der beiden das nicht erreicht, gibt es fuer
sie danach keine Baseline und damit `unknown`. Das ist die richtige Antwort
und keine Luecke - dieselbe Entscheidung wie bei den geprueften Flug-Legs. Der
Bericht nennt die Zahl der Beobachtungen je Art, damit man sieht, wie lange
das dauern duerfte.

Idempotent: ein zweiter Lauf findet die Tabelle schon in der neuen Form,
verwirft dieselben abgeleiteten Zeilen und rechnet dieselben Baselines noch
einmal.

    uv run python -m scripts.migrate_hotel_baseline_populations --db data/flightopt.db
"""
from __future__ import annotations

import argparse
import contextlib
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flightopt.storage import db
from flightopt.storage.baseline import (
    HOTEL,
    POPULATION_ESTIMATE,
    POPULATION_VERIFIED,
    ensure_hotel_baseline,
    refresh_hotel_baselines,
)

GroupKey = tuple[str, str, int, str, str, str, str | None]
"""scope, group_key, weekday, leadtime_bucket, stay_key, currency, Grundgesamtheit.

Die Grundgesamtheit ist `None`, solange die Tabelle sie nicht kennt - also vor
der Migration. Danach steht dort `estimate` oder `verified`.
"""

BaselineMap = dict[GroupKey, tuple[int, int]]
"""Schluessel -> (Median in Minor Units, n)."""


def old_hotel_baselines(path: Path) -> BaselineMap:
    """Die Hotel-Baselines, bevor irgendetwas die Datei anfasst.

    Eigene, schreibgeschuetzte Verbindung, und das ist kein Beiwerk:
    `db.connect` legt Tabellen an und traegt Spalten nach, und
    `ensure_hotel_baseline` wirft eine Tabelle der alten Form weg, sobald der
    erste Aufruf sie sieht. Ein Vorher-Bild, das danach entsteht, waere leer,
    und der Bericht meldete jede Zeile als neu.

    `population` wird mitgelesen, wenn es die Spalte schon gibt. Vor der
    Migration gibt es sie nicht, dann steht im Schluessel `None`. Ohne diese
    Fallunterscheidung behielte das Vorher-Bild eines zweiten Laufs von den
    zwei Zeilen je Gruppe nur eine, und der Bericht verglich danach den
    Haendlerpreis-Median gegen den der Richtwerte: eine Verschiebung, die es
    nicht gab.
    """
    if not path.exists():
        return {}
    # `as_uri()` und nicht der Pfad in einen f-String: eine SQLite-URI liest
    # `?`, `#` und `%` als Trennzeichen. Ein Ordner mit einem davon im Namen
    # zeigte damit auf eine andere Datei - die es nicht gibt, und dann waere
    # das Vorher-Bild still leer.
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='hotel_baseline'"
        ).fetchone()
        if exists is None:
            # Die Tabelle gibt es noch gar nicht. Leer ist hier eine Aussage.
            # Genau danach wird gefragt, statt jeden Lesefehler abzufangen:
            # eine gesperrte oder beschaedigte Datei liefert sonst dasselbe
            # leere Bild, und der Bericht meldet danach jede Gruppe als neu.
            return {}
        split = "population" in {
            row["name"] for row in conn.execute("PRAGMA table_info(hotel_baseline)")
        }
        rows = conn.execute(
            "SELECT scope, group_key, weekday, leadtime_bucket, stay_key, currency, "
            f"{'population' if split else 'NULL AS population'}, "
            "median_minor, n FROM hotel_baseline"
        ).fetchall()
    finally:
        conn.close()
    return {
        (r["scope"], r["group_key"], int(r["weekday"]), r["leadtime_bucket"],
         r["stay_key"], r["currency"], r["population"]):
            (int(r["median_minor"]), int(r["n"]))
        for r in rows
    }


def new_hotel_baselines(conn: sqlite3.Connection) -> dict[tuple, tuple[int, int, str]]:
    """Schluessel samt Grundgesamtheit -> (Median, n, Grundgesamtheit)."""
    rows = conn.execute(
        "SELECT scope, group_key, weekday, leadtime_bucket, stay_key, currency, "
        "population, median_minor, n FROM hotel_baseline"
    ).fetchall()
    return {
        (r["scope"], r["group_key"], int(r["weekday"]), r["leadtime_bucket"],
         r["stay_key"], r["currency"], r["population"]):
            (int(r["median_minor"]), int(r["n"]), r["population"])
        for r in rows
    }


def has_old_shape(conn: sqlite3.Connection) -> bool:
    """Steht die Tabelle noch ohne `population` da?

    Eine leere Antwort heisst "Tabelle gibt es nicht" - dann ist nichts
    umzubauen, und `ensure_hotel_baseline` legt sie gleich richtig an.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(hotel_baseline)")}
    return bool(columns) and "population" not in columns


def observations_by_population(conn: sqlite3.Connection) -> dict[str, int]:
    """Wie viele Hotelbeobachtungen je Art vorliegen.

    Die Zahl sagt, wie lange es dauern duerfte, bis die kleinere der beiden
    Gruppen wieder Baselines traegt. Ohne sie sieht ein Bestand, der zu 99
    Prozent aus Richtwerten besteht, genauso aus wie ein ausgewogener.
    """
    counts = {POPULATION_ESTIMATE: 0, POPULATION_VERIFIED: 0}
    for row in conn.execute(
        "SELECT is_estimate, COUNT(*) AS c FROM price_observation "
        "WHERE entity_type=? GROUP BY is_estimate",
        (HOTEL,),
    ):
        name = POPULATION_ESTIMATE if int(row["is_estimate"]) else POPULATION_VERIFIED
        counts[name] = int(row["c"])
    return counts


def report(before: BaselineMap, after: dict[tuple, tuple[int, int, str]]) -> list[str]:
    """Was sich an den Medianen geaendert hat, Gruppe fuer Gruppe.

    Verglichen wird zuerst mit der Zeile derselben Grundgesamtheit. Gibt es
    die nicht, war die Tabelle noch ungeteilt, und dann ist die gemischte
    Zeile das richtige Vorher - genau ihr Median ist ja der, der daneben lag.
    """
    lines: list[str] = []
    shifts: list[float] = []
    matched: set[GroupKey] = set()
    for key in sorted(after):
        median_minor, n, group = after[key]
        scope, group_key, weekday, bucket, stay, currency, _ = key
        old_key = key if key in before else key[:6] + (None,)
        old = before.get(old_key)
        head = f"  {scope} {group_key} wd{weekday} {bucket} {stay} {group}:"
        if old is None:
            lines.append(f"{head} neu {median_minor / 100:.2f} {currency} (n={n})")
            continue
        matched.add(old_key)
        shift = (median_minor - old[0]) / old[0] * 100 if old[0] else 0.0
        shifts.append(shift)
        lines.append(
            f"{head} {old[0] / 100:.2f} (n={old[1]}) -> {median_minor / 100:.2f} "
            f"(n={n}), {shift:+.1f}%"
        )

    # Die Gruppen, die es vorher gab und danach nicht mehr. Sie sind die
    # eigentliche Wirkung der Trennung: gemischt kamen sie ueber fuenf
    # Beobachtungen, getrennt keine der beiden Haelften. Ohne diese Zeilen
    # steht die Hauptfolge des Laufs nur als Differenz zweier Zaehlerstaende
    # da, und niemand weiss, welche Haeuser jetzt ohne Urteil sind.
    for key in sorted(before.keys() - matched, key=lambda k: tuple(map(str, k))):
        scope, group_key, weekday, bucket, stay, currency, group = key
        median_minor, n = before[key]
        kind = f" {group}" if group else ""
        lines.append(
            f"  {scope} {group_key} wd{weekday} {bucket} {stay}{kind}: "
            f"{median_minor / 100:.2f} (n={n}) -> keine Basis mehr"
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
    """Neubau und Neurechnung in einer Klammer, oder gar keine."""
    rebuilt = has_old_shape(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Legt die Tabelle in der neuen Form an und wirft eine alte weg.
        ensure_hotel_baseline(conn)
        # Und raeumt auch den zweiten Lauf ab, bei dem die Form schon stimmt:
        # abgeleitete Zeilen, die keine Beobachtung mehr traegt, blieben sonst
        # als Leichen stehen.
        conn.execute("DELETE FROM hotel_baseline")
        written = refresh_hotel_baselines(conn)
        conn.execute("COMMIT")
    except Exception:
        # Auch ein gescheitertes COMMIT laesst die Transaktion offen. Ein
        # Fehler beim Zuruecknehmen darf den urspruenglichen nicht verdecken.
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise

    return {
        "rebuilt": rebuilt,
        "written": written,
        "observations": observations_by_population(conn),
        "before": before,
        "after": new_hotel_baselines(conn),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/flightopt.db")
    args = parser.parse_args()
    path = Path(args.db)
    if not path.exists():
        print(f"keine Datenbank unter {path}")
        return 1

    before = old_hotel_baselines(path)
    conn = db.connect(path)
    try:
        result = migrate(conn, before)
    finally:
        # `migrate` wirft nach dem Zuruecknehmen weiter. Ohne `finally` bliebe
        # die Verbindung dabei offen und mit ihr die Sperre auf der Datei.
        conn.close()

    counts = result["observations"]
    print(
        "Tabelle: "
        + ("neu angelegt (alte Form verworfen)" if result["rebuilt"] else "stand schon richtig")
    )
    print(
        f"Hotelbeobachtungen: {counts[POPULATION_ESTIMATE]} Richtwerte, "
        f"{counts[POPULATION_VERIFIED]} Haendlerpreise"
    )
    print(f"hotel_baseline: {len(before)} vorher -> {result['written']} nachher")
    for line in report(result["before"], result["after"]):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
