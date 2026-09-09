"""Der Bestand wandert auf getrennte Grundgesamtheiten.

Die Migration fasst echte Daten an, die niemand zurueckholen kann. Sie muss
deshalb zweimal laufen duerfen, ohne beim zweiten Mal etwas anderes zu tun als
beim ersten - und sie muss ganz oder gar nicht laufen: eine leere
`hotel_baseline` heisst ueberall `unknown`, und die Zeilen zurueckzurechnen
dauert.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

from flightopt.storage import db

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "migrate_hotel_baseline_populations.py"

TRAVEL = date(2026, 11, 10)
OBSERVED = datetime(2026, 9, 8, 12, 0)

OLD_HOTEL_BASELINE_SCHEMA = """
-- Die Tabelle, wie sie bis zum 2026-09-09 aussah: ohne `population`.
CREATE TABLE IF NOT EXISTS hotel_baseline (
    scope            TEXT NOT NULL,
    group_key        TEXT NOT NULL,
    weekday          INTEGER NOT NULL,
    leadtime_bucket  TEXT NOT NULL,
    stay_key         TEXT NOT NULL,
    currency         TEXT NOT NULL,
    median_minor     INTEGER NOT NULL,
    mad_minor        INTEGER NOT NULL,
    n                INTEGER NOT NULL,
    computed_at      TEXT NOT NULL,
    PRIMARY KEY(scope, group_key, weekday, leadtime_bucket, stay_key, currency)
);
"""


def load_module():
    spec = importlib.util.spec_from_file_location(
        "migrate_hotel_baseline_populations", SCRIPT
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def observe(conn, prices, *, is_estimate: int, key="trivago:melia") -> None:
    for price in prices:
        conn.execute(
            "INSERT INTO price_observation("
            "observed_at, source, entity_type, entity_key, travel_date, "
            "return_or_nights, party_size, currency, price_total_minor, is_estimate) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (OBSERVED.isoformat(timespec="seconds"), "trivago", "hotel", key,
             TRAVEL.isoformat(), "1", 2, "EUR", int(price), int(is_estimate)),
        )


def mixed_history(path: Path) -> None:
    """Eine Datei aus der Zeit vor der Trennung: alte Tabelle, gemischte Zeilen.

    Fuenf Richtwerte um 120 Euro und fuenf Haendlerpreise um 90 Euro. Der
    gemeinsame Median liegt bei 120, und damit misst er keine der beiden
    Gruppen.
    """
    conn = db.connect(path)
    conn.execute(
        "INSERT INTO hotel_property(property_key, source, name, city, country, "
        "country_code, stars, first_seen, last_seen) VALUES(?,?,?,?,?,?,?,?,?)",
        ("trivago:melia", "trivago", "Melia Athens", "Athens", "Greece", "GR", 4,
         "2026-09-01T10:00:00", "2026-09-08T10:00:00"),
    )
    observe(conn, [11800, 11900, 12000, 12100, 12200], is_estimate=1)
    observe(conn, [8800, 8900, 9000, 9100, 9200], is_estimate=0)
    conn.executescript(OLD_HOTEL_BASELINE_SCHEMA)
    conn.execute(
        "INSERT INTO hotel_baseline(scope, group_key, weekday, leadtime_bucket, "
        "stay_key, currency, median_minor, mad_minor, n, computed_at) "
        "VALUES('own','trivago:melia',1,'60-119','p2n1','EUR',12000,1550,10,'x')"
    )
    conn.commit()
    conn.close()


def baselines(path: Path) -> dict[tuple[str, str], tuple[int, int]]:
    conn = db.connect(path)
    try:
        return {
            (row["scope"], row["population"]): (row["median_minor"], row["n"])
            for row in conn.execute(
                "SELECT scope, population, median_minor, n FROM hotel_baseline"
            )
        }
    finally:
        conn.close()


def test_the_mixed_median_is_replaced_by_one_per_population(tmp_path):
    """Genau der Fehler, der bei den Fluegen monatelang unsichtbar war.

    Der gemeinsame Median lag bei 120 Euro und maass damit weder die
    Richtwerte noch die Haendlerpreise. Danach steht neben jeder Gruppe ihre
    eigene Zahl.
    """
    path = tmp_path / "hotels.db"
    mixed_history(path)
    module = load_module()

    before = module.old_hotel_baselines(path)
    conn = db.connect(path)
    result = module.migrate(conn, before)
    conn.close()

    assert baselines(path) == {
        ("own", "estimate"): (12000, 5),
        ("own", "verified"): (9000, 5),
        ("peer", "estimate"): (12000, 5),
        ("peer", "verified"): (9000, 5),
    }
    assert result["rebuilt"] is True
    assert result["written"] == 4
    assert result["observations"] == {"estimate": 5, "verified": 5}


def test_the_report_says_which_medians_moved_and_by_how_much(tmp_path):
    """Ein Skript, das nur "fertig" sagt, laesst sich nicht nachpruefen."""
    path = tmp_path / "hotels.db"
    mixed_history(path)
    module = load_module()

    before = module.old_hotel_baselines(path)
    conn = db.connect(path)
    result = module.migrate(conn, before)
    conn.close()
    lines = module.report(result["before"], result["after"])

    text = "\n".join(lines)
    # Der Haendlerpreis-Median faellt von 120 auf 90 Euro, also um 25 Prozent.
    assert "-25.0%" in text
    assert "verified" in text and "estimate" in text
    assert "Verschiebung" in text


def test_a_second_run_changes_nothing(tmp_path):
    path = tmp_path / "hotels.db"
    mixed_history(path)
    module = load_module()

    conn = db.connect(path)
    module.migrate(conn, module.old_hotel_baselines(path))
    conn.close()
    once = baselines(path)

    conn = db.connect(path)
    second = module.migrate(conn, module.old_hotel_baselines(path))
    conn.close()

    assert baselines(path) == once
    # Beim zweiten Mal steht die Tabelle schon richtig da.
    assert second["rebuilt"] is False


def test_the_second_run_reports_no_shift_instead_of_a_made_up_one(tmp_path):
    """Ein Bericht, der beim zweiten Lauf etwas meldet, ist ein Fehlalarm.

    Nach der Trennung gibt es je Gruppe zwei Zeilen und nicht mehr eine. Ein
    Vorher-Bild, das die Grundgesamtheit nicht mitliest, behaelt davon nur
    eine - und vergleicht danach den Haendlerpreis-Median gegen den der
    Richtwerte. Das sieht aus wie eine Verschiebung und ist keine.
    """
    path = tmp_path / "hotels.db"
    mixed_history(path)
    module = load_module()

    conn = db.connect(path)
    module.migrate(conn, module.old_hotel_baselines(path))
    conn.close()

    conn = db.connect(path)
    second = module.migrate(conn, module.old_hotel_baselines(path))
    conn.close()
    lines = module.report(second["before"], second["after"])

    assert lines, "der Bericht darf nicht leer sein"
    for line in lines:
        assert "-" not in line.rsplit(",", 1)[-1], line
    assert "Verschiebung: Median +0.0%, von +0.0% bis +0.0%" in lines[-1]


def test_a_failure_leaves_the_old_baselines_alone(tmp_path, monkeypatch):
    """Ganz oder gar nicht.

    Ohne die Klammer waeren nach einem Fehler beim Neurechnen die alten
    Baselines weg und die neuen nicht da - und bis die Beobachtungen wieder
    zu Baselines geworden sind, misst kein Preis mehr gegen irgendetwas.
    """
    path = tmp_path / "hotels.db"
    mixed_history(path)
    module = load_module()

    def boom(conn, **kwargs):
        raise RuntimeError("Neurechnen abgebrochen")

    monkeypatch.setattr(module, "refresh_hotel_baselines", boom)
    conn = db.connect(path)
    with pytest.raises(RuntimeError):
        module.migrate(conn, module.old_hotel_baselines(path))
    conn.close()

    rows = db.connect(path).execute(
        "SELECT median_minor, n FROM hotel_baseline"
    ).fetchall()
    assert [(r["median_minor"], r["n"]) for r in rows] == [(12000, 10)]


def test_a_group_that_lost_its_baseline_is_named_and_not_only_counted(tmp_path):
    """Das ist die Hauptwirkung der Migration, nicht ein Nebeneffekt.

    Eine Gruppe, die gemischt zehn Beobachtungen hatte, kann getrennt zweimal
    unter fuenf liegen. Danach steht dort `unknown`, und das gehoert
    ausdruecklich in den Bericht - als Zeilenzahl allein sieht es aus wie ein
    Rundungsfehler.
    """
    path = tmp_path / "hotels.db"
    conn = db.connect(path)
    conn.execute(
        "INSERT INTO hotel_property(property_key, source, name, city, country, "
        "country_code, stars, first_seen, last_seen) VALUES(?,?,?,?,?,?,?,?,?)",
        ("trivago:melia", "trivago", "Melia Athens", "Athens", "Greece", "GR", 4,
         "2026-09-01T10:00:00", "2026-09-08T10:00:00"),
    )
    # Sechs Beobachtungen gemischt: gemeinsam reichen sie fuer eine Baseline,
    # getrennt reicht keine der beiden Haelften.
    observe(conn, [11800, 11900, 12000], is_estimate=1)
    observe(conn, [8800, 8900, 9000], is_estimate=0)
    conn.executescript(OLD_HOTEL_BASELINE_SCHEMA)
    conn.execute(
        "INSERT INTO hotel_baseline(scope, group_key, weekday, leadtime_bucket, "
        "stay_key, currency, median_minor, mad_minor, n, computed_at) "
        "VALUES('own','trivago:melia',1,'60-119','p2n1','EUR',10400,1550,6,'x')"
    )
    conn.commit()
    conn.close()
    module = load_module()

    before = module.old_hotel_baselines(path)
    conn = db.connect(path)
    result = module.migrate(conn, before)
    conn.close()
    text = "\n".join(module.report(result["before"], result["after"]))

    assert result["written"] == 0
    assert "trivago:melia" in text
    assert "keine Basis mehr" in text


def test_an_unreadable_table_is_not_reported_as_an_empty_before_picture(tmp_path):
    """Stumm leer ist die gefaehrlichste Antwort, die dieses Skript geben kann.

    Ein leeres Vorher-Bild laesst den Bericht jede Gruppe als "neu" melden -
    und der Bericht ist der einzige Grund, warum das Skript ueberhaupt einen
    hat. Fehlt die Tabelle, ist leer die richtige Antwort. Geht das Lesen aus
    einem anderen Grund schief, gehoert der Fehler nach oben.
    """
    path = tmp_path / "hotels.db"
    conn = db.connect(path)
    conn.execute("CREATE TABLE hotel_baseline(scope TEXT)")
    conn.commit()
    conn.close()
    module = load_module()

    with pytest.raises(sqlite3.OperationalError):
        module.old_hotel_baselines(path)


def test_a_missing_table_is_an_empty_before_picture_and_no_error(tmp_path):
    path = tmp_path / "hotels.db"
    db.connect(path).close()
    module = load_module()

    assert module.old_hotel_baselines(path) == {}


def test_the_before_picture_is_read_before_anything_touches_the_file(tmp_path):
    """Sonst faerbt der Neubau der Tabelle das Vorher-Bild.

    `ensure_hotel_baseline` wirft eine Tabelle der alten Form weg, sobald der
    erste Aufruf sie sieht. Ein Vorher-Bild, das danach entsteht, ist keines -
    es waere leer, und der Bericht meldete jede Zeile als neu.
    """
    path = tmp_path / "hotels.db"
    mixed_history(path)
    module = load_module()

    before = module.old_hotel_baselines(path)

    assert len(before) == 1
    assert list(before.values()) == [(12000, 10)]
    # Und die Verbindung darf nur lesen: sie legt nichts an und traegt nichts
    # nach, sonst waere sie selbst der erste Zugriff.
    read_only = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            read_only.execute("CREATE TABLE probe(a)")
    finally:
        read_only.close()
