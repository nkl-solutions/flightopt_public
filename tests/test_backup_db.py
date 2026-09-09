"""Die Sicherung gegen eine Wegwerf-Datenbank in `tmp_path`, nie gegen die echte.

Die Preishistorie ist der einzige Teil des Projekts, den man nicht neu bauen
kann. Ein Fehler im Sicherungsskript faellt erst auf, wenn man die Sicherung
braucht, und dann ist es zu spaet. Deshalb bekommt jede der vier Aufgaben einen
Test: konsistent kopieren, im laufenden Betrieb kopieren, Schrott erkennen,
aufraeumen ohne fremde Dateien anzufassen.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

from flightopt.storage import db

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "backup_db.py"


def load_module():
    """Das Skript liegt ausserhalb des Pakets und wird direkt geladen."""
    spec = importlib.util.spec_from_file_location("backup_db", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


backup_db = load_module()


def observe(conn: sqlite3.Connection, key: str, at: str) -> None:
    conn.execute(
        "INSERT INTO price_observation (observed_at, source, entity_type, entity_key,"
        " travel_date, currency, price_total_minor) "
        "VALUES (?, 'test', 'flight', ?, '2026-10-01', 'EUR', 12345)",
        (at, key),
    )


@pytest.fixture
def live_db(tmp_path: Path) -> tuple[Path, sqlite3.Connection]:
    """Datenbank im WAL-Modus mit offener Verbindung, wie im laufenden Dienst."""
    path = tmp_path / "flightopt.db"
    conn = db.connect(path)
    for number in range(3):
        observe(conn, f"BER|ATH|{number}", f"2026-09-0{number + 1}T08:00:00")
    return path, conn


def test_kopie_enthaelt_die_beobachtungen(live_db, tmp_path: Path):
    source, conn = live_db
    target_dir = tmp_path / "backups"

    result = backup_db.run(source, target_dir)

    assert result.path.parent == target_dir
    assert result.observations == 3
    assert result.latest_observation == "2026-09-03T08:00:00"
    assert backup_db.stamp_of(result.path) is not None
    copy = sqlite3.connect(result.path)
    try:
        rows = copy.execute("SELECT count(*) FROM price_observation").fetchone()[0]
    finally:
        copy.close()
    assert rows == 3
    conn.close()


def test_kopie_holt_auch_was_noch_im_wal_steht(live_db, tmp_path: Path):
    """Der Kern der Sache: ein blosses `cp` haette diese Zeile verpasst.

    Die Verbindung bleibt offen, es wird kein Checkpoint erzwungen. Die neue
    Zeile steht damit im Write-Ahead-Log und noch nicht in der `.db`-Datei.
    """
    source, conn = live_db
    observe(conn, "BER|FCO", "2026-09-09T21:00:00")

    result = backup_db.run(source, tmp_path / "backups")

    assert result.observations == 4
    assert result.latest_observation == "2026-09-09T21:00:00"
    conn.close()


def test_ziel_wird_angelegt_und_nichts_bleibt_halb_liegen(live_db, tmp_path: Path):
    source, conn = live_db
    target_dir = tmp_path / "tief" / "backups"

    backup_db.run(source, target_dir)

    assert not list(target_dir.glob("*.part"))
    assert len(list(target_dir.glob("flightopt-*.db"))) == 1
    conn.close()


def test_kaputte_quelle_meldet_fehler_und_laesst_nichts_zurueck(tmp_path: Path):
    source = tmp_path / "kaputt.db"
    source.write_bytes(b"das ist keine datenbank" * 100)
    target_dir = tmp_path / "backups"

    with pytest.raises(backup_db.BackupError):
        backup_db.run(source, target_dir)

    assert list(target_dir.iterdir()) == []


def test_datenbank_ohne_beobachtungstabelle_gilt_als_fehlschlag(tmp_path: Path):
    """Heil, aber leer ist kein Erfolg: dann ist es nicht diese Datenbank."""
    source = tmp_path / "fremd.db"
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE etwas (x INTEGER)")
    conn.commit()
    conn.close()
    target_dir = tmp_path / "backups"

    with pytest.raises(backup_db.BackupError, match="price_observation"):
        backup_db.run(source, target_dir)

    assert list(target_dir.iterdir()) == []


def test_fehlende_quelle_liefert_rueckgabewert_zwei(tmp_path: Path, capsys):
    code = backup_db.main([
        "--db", str(tmp_path / "gibtsnicht.db"),
        "--target-dir", str(tmp_path / "backups"),
    ])
    assert code == 2
    assert "nicht gefunden" in capsys.readouterr().err


def test_erfolgreicher_lauf_liefert_rueckgabewert_null(live_db, tmp_path: Path, capsys):
    source, conn = live_db
    code = backup_db.main([
        "--db", str(source), "--target-dir", str(tmp_path / "backups"),
    ])
    assert code == 0
    assert "beobachtungen 3" in capsys.readouterr().out
    conn.close()


def make_backup(target_dir: Path, stamp: str) -> Path:
    path = target_dir / f"flightopt-{stamp}.db"
    path.write_bytes(b"platzhalter")
    return path


def test_aufraeumen_haelt_die_juengsten_und_je_einen_monatsstand(tmp_path: Path):
    target_dir = tmp_path / "backups"
    target_dir.mkdir()
    # Drei Monate mit je drei Staenden, aeltester Monat zuerst.
    stamps = [
        f"2026{month:02d}{day:02d}-030000"
        for month in (6, 7, 8)
        for day in (10, 20, 28)
    ]
    for stamp in stamps:
        make_backup(target_dir, stamp)

    removed = backup_db.prune(target_dir, keep_latest=2, keep_months=3)

    left = sorted(path.name for path in target_dir.iterdir())
    assert left == [
        "flightopt-20260628-030000.db",  # Monatsstand Juni
        "flightopt-20260728-030000.db",  # Monatsstand Juli
        "flightopt-20260820-030000.db",  # zweitjuengste
        "flightopt-20260828-030000.db",  # juengste, zugleich Monatsstand August
    ]
    assert len(removed) == 5


def test_aufraeumen_faesst_fremde_dateien_nicht_an(tmp_path: Path):
    target_dir = tmp_path / "backups"
    target_dir.mkdir()
    for day in range(1, 6):
        make_backup(target_dir, f"20260{day}01-030000")
    fremd = target_dir / "notiz.txt"
    fremd.write_text("haende weg", encoding="utf-8")
    handkopie = target_dir / "flightopt.db.bak-baselinequality"
    handkopie.write_bytes(b"handarbeit")

    backup_db.prune(target_dir, keep_latest=1, keep_months=1)

    assert fremd.exists()
    assert handkopie.exists()


def test_lauf_raeumt_direkt_mit_auf(live_db, tmp_path: Path):
    source, conn = live_db
    target_dir = tmp_path / "backups"
    target_dir.mkdir()
    alt = make_backup(target_dir, "20250101-030000")

    result = backup_db.run(
        source, target_dir, moment=datetime(2026, 9, 9, 17, 45, 0),
        keep_latest=1, keep_months=1,
    )

    assert result.path.name == "flightopt-20260909-174500.db"
    assert alt in result.removed
    assert not alt.exists()
    conn.close()


def test_read_only_uri_nimmt_kein_immutable(tmp_path: Path):
    """`immutable=1` wuerde das Write-Ahead-Log ueberspringen. Genau das nicht."""
    uri = backup_db.read_only_uri(tmp_path / "flightopt.db")
    assert "mode=ro" in uri
    assert "immutable" not in uri
