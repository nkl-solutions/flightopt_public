"""Einmalige Datenkorrektur: den Laendercode aus Hotel-Schluesseln entfernen.

`hotels/models.py` schrieb `<cc>|<property_key>`. Eindeutiger wurde der
Schluessel dadurch nicht - `property_key` ist `<quelle>:<id>` und traegt beides
schon -, aber instabil: liefert eine Quelle das Land einmal nicht, wandert
dieselbe Unterkunft nach `XX|...`. Ihre Historie zerfaellt dann in zwei
Grundgesamtheiten, von denen keine mehr die fuenf Beobachtungen erreicht, ab
denen es ueberhaupt eine Baseline gibt.

Seit dem 2026-09-09 ist der Schluessel der `property_key` allein. Dieses Skript
zieht den Bestand nach:

1. `price_observation` bekommt die neue Form. `GR|trivago:abc` und
   `XX|trivago:abc` fallen dabei auf denselben Schluessel zusammen - genau das
   ist der Sinn.
2. Die `own`-Zeilen von `hotel_baseline` tragen den alten Schluessel als
   `group_key` und liest danach niemand mehr. Sie fliegen raus statt zu
   verwaisen; die Peer-Zeilen bleiben, ihr `group_key` ist ein anderer.
3. Danach werden die Hotel-Baselines neu gerechnet, damit die
   zusammengefuehrte Historie sofort zaehlt.

Das Land geht nicht verloren: es steht in `hotel_property.country_code` und
wird von dort gelesen.

Idempotent: ein zweiter Lauf findet keine Zeile mit `|` mehr und rechnet
dieselben Baselines noch einmal.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flightopt.storage import db
from flightopt.storage.baseline import refresh_hotel_baselines


def migrate(conn) -> tuple[int, int]:
    """Beobachtungen umschreiben, verwaiste Eigen-Baselines loeschen.

    Zurueck kommen die Zahl der geaenderten Beobachtungen und die Zahl der
    geloeschten Baseline-Zeilen.
    """
    rows = conn.execute(
        "SELECT id, entity_key FROM price_observation "
        "WHERE entity_type='hotel' AND entity_key LIKE '%|%'"
    ).fetchall()
    for row in rows:
        # Genau einmal trennen: ein `property_key` enthaelt kein '|', der
        # Laendercode steht davor.
        _, _, property_key = str(row["entity_key"]).partition("|")
        if not property_key:
            continue
        conn.execute(
            "UPDATE price_observation SET entity_key=? WHERE id=?",
            (property_key, row["id"]),
        )

    dropped = 0
    has_baseline = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='hotel_baseline'"
    ).fetchone()
    if has_baseline is not None:
        cur = conn.execute(
            "DELETE FROM hotel_baseline WHERE scope='own' AND group_key LIKE '%|%'"
        )
        dropped = cur.rowcount

    refresh_hotel_baselines(conn)
    conn.commit()
    return len(rows), dropped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/flightopt.db")
    args = parser.parse_args()
    if not Path(args.db).exists():
        print(f"keine Datenbank unter {args.db}")
        return 1
    conn = db.connect(args.db)
    changed, dropped = migrate(conn)
    print(f"umgeschrieben: {changed} Beobachtungen, verworfen: {dropped} Baselines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
