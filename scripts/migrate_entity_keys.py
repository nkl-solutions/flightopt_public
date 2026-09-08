"""Einmalige Datenkorrektur: dreiteilige Flug-Schluessel auf zwei Teile kuerzen.

`search/grid.py` schrieb `ORIGIN|DEST|quelle`, `jobs/daily.py` und die Preislage
lesen `ORIGIN|DEST`. Ergebnis: `price_baseline` blieb leer und jede Preisaussage
stand auf `unknown`. Die Quelle geht nicht verloren, sie steht in `source`.

Idempotent: ein zweiter Lauf findet nichts mehr.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flightopt.storage import db


def migrate(conn) -> int:
    rows = conn.execute(
        "SELECT id, entity_key FROM price_observation "
        "WHERE entity_type='flight' AND entity_key LIKE '%|%|%'"
    ).fetchall()
    for row in rows:
        origin, destination, *_ = str(row["entity_key"]).split("|")
        conn.execute(
            "UPDATE price_observation SET entity_key=? WHERE id=?",
            (f"{origin}|{destination}", row["id"]),
        )
    conn.commit()
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/flightopt.db")
    args = parser.parse_args()
    if not Path(args.db).exists():
        print(f"keine Datenbank unter {args.db}")
        return 1
    conn = db.connect(args.db)
    changed = migrate(conn)
    print(f"gekuerzt: {changed} Beobachtungen")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
