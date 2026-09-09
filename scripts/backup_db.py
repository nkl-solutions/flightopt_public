"""Sichert die Preisdatenbank, waehrend der Dienst weiterlaeuft.

    uv run python scripts/backup_db.py
    uv run python scripts/backup_db.py --target-dir D:/flightopt-backups

Der Code liegt in zwei Git-Repos und ist jederzeit wiederherstellbar. Die
Preisbeobachtungen sind es nicht: eine verlorene Beobachtung ist ein Tag, den
niemand nachholen kann. Deshalb gibt es dieses Skript und nicht nur ein `cp`.

Warum nicht kopieren: die Datenbank laeuft im WAL-Modus. Die zuletzt
geschriebenen Zeilen stehen dann nicht in der `.db`-Datei, sondern im
Write-Ahead-Log daneben. Wer waehrend eines Schreibvorgangs kopiert, bekommt
eine Datei, die sich oeffnen laesst und trotzdem nicht stimmt - der
unangenehmste Fehlerfall, weil er still ist. Die Backup-Schnittstelle von
SQLite (`Connection.backup`, Standardbibliothek, kein Zusatzpaket) loest genau
das: sie zieht die Kopie seitenweise unter einer Leseperre und bezieht das
Write-Ahead-Log mit ein.

Ablauf je Lauf:

1. Kopie unter `<name>.part` schreiben. Wer den Ordner ansieht, waehrend das
   Skript laeuft, soll eine halbe Sicherung nicht fuer eine ganze halten.
2. Kopie pruefen: `PRAGMA integrity_check` und die Zahl der Beobachtungen. Eine
   Sicherung, die niemand geprueft hat, ist keine.
3. Erst danach auf den endgueltigen Namen umbenennen.
4. Alte Sicherungen nach der Regel unten wegraeumen.

Rueckgabewert fuer eine Aufgabenplanung: 0 Sicherung steht und ist geprueft,
1 Sicherung fehlgeschlagen oder Pruefung nicht bestanden, 2 Bedienfehler
(Quelle fehlt, Ziel nicht beschreibbar).

Achtung beim Ablageort: die Vorgabe `data/backups` schuetzt gegen Versehen und
gegen eine kaputte Datenbank, nicht gegen einen Datentraegerausfall - sie liegt
auf derselben Platte. Fuer das zweite Exemplar `--target-dir` auf ein anderes
Laufwerk zeigen lassen. Wie die Sicherung des VPS-Volumes angestossen wird,
steht in `docs/DEPLOYMENT.md`.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flightopt.storage.db import DEFAULT_DB

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_TARGET_DIR = Path("data/backups")

PREFIX = "flightopt-"
SUFFIX = ".db"
STAMP_FORMAT = "%Y%m%d-%H%M%S"
NAME_PATTERN = re.compile(rf"^{re.escape(PREFIX)}(\d{{8}}-\d{{6}}){re.escape(SUFFIX)}$")
"""Nur was diesem Muster folgt, gilt als eigene Sicherung.

Das Aufraeumen loescht ausschliesslich Treffer. Wer eigene Dateien in den
Ordner legt, soll sie dort wiederfinden.
"""

KEEP_LATEST = 14
KEEP_MONTHS = 6
"""Aufbewahrungsregel: die letzten 14 Sicherungen, und zusaetzlich je die
juengste Sicherung der letzten 6 Kalendermonate.

Bei einem taeglichen Lauf sind das zwei Wochen in voller Aufloesung plus ein
halbes Jahr Monatsstaende. Nur die letzten 14 zu behalten waere zu wenig: ein
Schaden, den jemand erst nach drei Wochen bemerkt, waere dann nicht mehr
zurueckzuholen.
"""


class BackupError(RuntimeError):
    """Sicherung nicht zustande gekommen oder Pruefung nicht bestanden."""


@dataclass
class Result:
    path: Path
    observations: int
    latest_observation: str | None
    size_bytes: int
    removed: list[Path]


def read_only_uri(path: Path) -> str:
    """`file:`-URI der Quelle, lesend.

    Kein `immutable=1`: das erlaubt SQLite, das Write-Ahead-Log zu ignorieren,
    und dort stehen genau die Beobachtungen, die noch niemand gesichert hat.
    Eine Sicherung, die den juengsten Tag stillschweigend auslaesst, ist
    schlimmer als keine.
    """
    return "file:" + urllib.request.pathname2url(str(path)) + "?mode=ro"


def open_source(path: Path) -> sqlite3.Connection:
    """Quelle oeffnen, ohne dem laufenden Dienst dazwischenzufunken."""
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(read_only_uri(path), uri=True)
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.Error:
        if conn is not None:
            conn.close()
        # WAL braucht einen Shared-Memory-Index neben der Datei. Liegt die
        # Datenbank still und fehlt die `-shm`-Datei, darf eine rein lesende
        # Verbindung sie nicht anlegen und das Oeffnen scheitert. Dann normal
        # oeffnen: die Backup-Schnittstelle nimmt trotzdem nur eine Leseperre.
        conn = sqlite3.connect(path)
    # Der Dienst schreibt weiter. Warten ist richtig, Abbrechen waere falsch.
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def copy_database(source: Path, target: Path) -> None:
    """Konsistente Kopie ueber die Backup-Schnittstelle von SQLite."""
    conn = open_source(source)
    try:
        copy = sqlite3.connect(target)
        try:
            conn.backup(copy)
        finally:
            copy.close()
    except sqlite3.Error as exc:
        raise BackupError(f"Kopie fehlgeschlagen: {exc}") from exc
    finally:
        conn.close()


def verify(path: Path) -> tuple[int, str | None]:
    """Kopie zurueckgelesen: heil, und wie viele Beobachtungen stehen drin?

    Eigene Verbindung auf die fertige Datei, nicht das Handle aus dem Kopieren.
    Was nur die schreibende Seite bestaetigt, ist nicht geprueft.
    """
    conn = sqlite3.connect(path)
    try:
        verdict = conn.execute("PRAGMA integrity_check").fetchone()
        if not verdict or verdict[0] != "ok":
            raise BackupError(
                f"integrity_check schlaegt an: {verdict[0] if verdict else 'keine Antwort'}"
            )
        try:
            count, latest = conn.execute(
                "SELECT count(*), max(observed_at) FROM price_observation"
            ).fetchone()
        except sqlite3.OperationalError as exc:
            # Ohne diese Tabelle ist es nicht die Datenbank, um die es geht.
            raise BackupError(f"price_observation nicht lesbar: {exc}") from exc
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"Kopie nicht lesbar: {exc}") from exc
    finally:
        conn.close()
    return int(count), latest


def stamp_of(path: Path) -> datetime | None:
    """Zeitstempel aus dem Dateinamen, oder None wenn der Name nicht passt."""
    match = NAME_PATTERN.match(path.name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), STAMP_FORMAT)
    except ValueError:
        return None


def existing_backups(target_dir: Path) -> list[tuple[datetime, Path]]:
    """Eigene Sicherungen im Ordner, juengste zuerst."""
    found: list[tuple[datetime, Path]] = []
    if not target_dir.is_dir():
        return found
    for entry in target_dir.iterdir():
        if not entry.is_file():
            continue
        stamp = stamp_of(entry)
        if stamp is not None:
            found.append((stamp, entry))
    found.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    return found


def survivors(
    backups: Sequence[tuple[datetime, Path]],
    keep_latest: int = KEEP_LATEST,
    keep_months: int = KEEP_MONTHS,
) -> set[Path]:
    """Welche Sicherungen bleiben. Siehe Aufbewahrungsregel oben."""
    keep = {path for _, path in backups[:keep_latest]}
    newest_per_month: dict[tuple[int, int], Path] = {}
    for stamp, path in backups:  # juengste zuerst, also gewinnt der erste Treffer
        newest_per_month.setdefault((stamp.year, stamp.month), path)
    for month in sorted(newest_per_month, reverse=True)[:keep_months]:
        keep.add(newest_per_month[month])
    return keep


def prune(
    target_dir: Path,
    keep_latest: int = KEEP_LATEST,
    keep_months: int = KEEP_MONTHS,
) -> list[Path]:
    """Loescht, was die Regel nicht mehr haelt. Fremde Dateien bleibt es fern."""
    backups = existing_backups(target_dir)
    keep = survivors(backups, keep_latest, keep_months)
    removed: list[Path] = []
    for _, path in backups:
        if path in keep:
            continue
        try:
            path.unlink()
        except OSError:
            # Eine Datei, die sich nicht loeschen laesst, kostet Platz. Die
            # frische Sicherung deswegen als Fehlschlag zu melden waere falsch.
            continue
        removed.append(path)
    return removed


def run(
    source: Path,
    target_dir: Path,
    *,
    moment: datetime | None = None,
    keep_latest: int = KEEP_LATEST,
    keep_months: int = KEEP_MONTHS,
) -> Result:
    """Ein vollstaendiger Lauf: kopieren, pruefen, benennen, aufraeumen."""
    if not source.is_file():
        raise BackupError(f"Quelldatenbank nicht gefunden: {source}")
    target_dir.mkdir(parents=True, exist_ok=True)

    stamp = (moment or datetime.now()).strftime(STAMP_FORMAT)
    final = target_dir / f"{PREFIX}{stamp}{SUFFIX}"
    partial = final.with_name(final.name + ".part")
    partial.unlink(missing_ok=True)

    try:
        copy_database(source, partial)
        observations, latest = verify(partial)
    except BackupError:
        # Eine unfertige Sicherung darf nicht liegenbleiben: beim naechsten
        # Ernstfall wuerde sie nach einer Sicherung aussehen.
        partial.unlink(missing_ok=True)
        raise

    size = partial.stat().st_size
    final.unlink(missing_ok=True)
    partial.rename(final)
    removed = prune(target_dir, keep_latest, keep_months)
    return Result(final, observations, latest, size, removed)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backup_db",
        description="Preisdatenbank sichern, waehrend der Dienst laeuft.",
    )
    parser.add_argument("--db", default=None, help=f"Quelldatenbank, Vorgabe {DEFAULT_DB}")
    parser.add_argument(
        "--target-dir", default=None, help=f"Zielordner, Vorgabe {DEFAULT_TARGET_DIR}"
    )
    parser.add_argument(
        "--keep-latest", type=int, default=KEEP_LATEST,
        help=f"Wieviele der juengsten Sicherungen bleiben, Vorgabe {KEEP_LATEST}",
    )
    parser.add_argument(
        "--keep-months", type=int, default=KEEP_MONTHS,
        help=f"Fuer wieviele Kalendermonate ein Stand bleibt, Vorgabe {KEEP_MONTHS}",
    )
    return parser


def _resolve(value: str | None, fallback: Path) -> Path:
    """Relative Angaben zaehlen ab Projektwurzel, nicht ab dem Arbeitsordner.

    Sonst haengt das Ergebnis eines geplanten Laufs davon ab, wo der Planer
    steht, und das merkt man erst, wenn man die Sicherung sucht.
    """
    path = Path(value) if value else fallback
    return path if path.is_absolute() else (ROOT / path).resolve()


def _human(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in ("KB", "MB", "GB"):
        value /= 1024
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
    return f"{value:.1f} GB"


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    source = _resolve(args.db, DEFAULT_DB)
    target_dir = _resolve(args.target_dir, DEFAULT_TARGET_DIR)

    if not source.is_file():
        print(f"Abbruch: Quelldatenbank nicht gefunden: {source}", file=sys.stderr)
        return 2

    try:
        result = run(
            source, target_dir,
            keep_latest=args.keep_latest, keep_months=args.keep_months,
        )
    except OSError as exc:
        print(f"Abbruch: Zielordner nicht nutzbar: {exc}", file=sys.stderr)
        return 2
    except BackupError as exc:
        print(f"Sicherung fehlgeschlagen: {exc}", file=sys.stderr)
        return 1

    print(f"quelle    {source}")
    print(f"kopie     {result.path}  {_human(result.size_bytes)}")
    print(
        f"geprueft  integrity_check ok"
        f"  beobachtungen {result.observations}"
        f"  juengste {result.latest_observation or '-'}"
    )
    if result.removed:
        print(f"entfernt  {len(result.removed)}")
        for path in result.removed:
            print(f"  weg  {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
