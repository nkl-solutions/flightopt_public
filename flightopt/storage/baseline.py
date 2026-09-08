"""Price baselines and simple anomaly signals.

Fluege rechnen gegen `price_baseline`, unveraendert seit dem ersten Tag. Hotels
brauchen zwei Dinge mehr, die dort nicht hineinpassen: die Belegung samt
Naechtezahl im Schluessel und eine zweite Ebene fuer duenne Historie. Beides
steht in `hotel_baseline` daneben statt in `price_baseline` drin. Der Grund ist
banal und trotzdem entscheidend: `price_baseline` hat einen zusammengesetzten
Primaerschluessel, und den erweitert SQLite nicht per `ALTER TABLE`. Eine neue
Tabelle legt `CREATE TABLE IF NOT EXISTS` dagegen auch in einer bestehenden
Datei an.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from statistics import median
from typing import Any

from flightopt.domain.fx import Rates
from flightopt.hotels.normalize import is_category_suspect, peer_key, stay_key
from flightopt.hotels.signals import (
    BAND_REASON,
    BASIS_OWN,
    BASIS_PEER,
    DEFAULT_LIMITS,
    THIN_HISTORY_N,
    Baseline,
    PlausibilityLimits,
    band_status,
    classify,
)

HOTEL = "hotel"

HOTEL_BASELINE_SCHEMA = """
-- Zwei Ebenen in einer Tabelle: 'own' rechnet je Objekt, 'peer' je Stadt,
-- Sternekategorie und Zeitfenster. `stay_key` haelt Belegung und Naechte
-- auseinander, sonst liegt ein Familienzimmer fuer drei Naechte in derselben
-- Verteilung wie ein Einzelzimmer fuer eine.
CREATE TABLE IF NOT EXISTS hotel_baseline (
    scope            TEXT NOT NULL,      -- 'own' | 'peer'
    group_key        TEXT NOT NULL,      -- own: entity_key, peer: '<cc>|<stadt>|<sterne>'
    weekday          INTEGER NOT NULL,
    leadtime_bucket  TEXT NOT NULL,
    stay_key         TEXT NOT NULL,      -- 'p<belegung>n<naechte>'
    currency         TEXT NOT NULL,
    median_minor     INTEGER NOT NULL,
    mad_minor        INTEGER NOT NULL,
    n                INTEGER NOT NULL,
    computed_at      TEXT NOT NULL,
    PRIMARY KEY(scope, group_key, weekday, leadtime_bucket, stay_key, currency)
);
"""


def leadtime_bucket(days: int) -> str:
    if days < 7:
        return "0-6"
    if days < 14:
        return "7-13"
    if days < 30:
        return "14-29"
    if days < 60:
        return "30-59"
    if days < 120:
        return "60-119"
    return "120+"


def _baseline_key(travel_date: date, observed_at: datetime) -> tuple[int, str]:
    leadtime = max(0, (travel_date - observed_at.date()).days)
    return travel_date.weekday(), leadtime_bucket(leadtime)


def refresh_baselines(conn: sqlite3.Connection, *, now: datetime | None = None,
                      entity_type: str = "flight", min_samples: int = 5) -> int:
    computed_at = (now or datetime.now()).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT observed_at, entity_type, entity_key, travel_date, currency, "
        "price_total_minor FROM price_observation WHERE entity_type=?",
        (entity_type,),
    ).fetchall()
    grouped: dict[tuple[str, str, int, str, str], list[int]] = {}
    for row in rows:
        observed_at = datetime.fromisoformat(row["observed_at"])
        travel_date = date.fromisoformat(row["travel_date"])
        weekday, bucket = _baseline_key(travel_date, observed_at)
        key = (row["entity_type"], row["entity_key"], weekday, bucket, row["currency"])
        grouped.setdefault(key, []).append(int(row["price_total_minor"]))

    written = 0
    for (etype, entity_key, weekday, bucket, currency), prices in grouped.items():
        if len(prices) < min_samples:
            continue
        med = int(median(prices))
        deviations = [abs(p - med) for p in prices]
        mad = int(median(deviations))
        conn.execute(
            "INSERT INTO price_baseline("
            "entity_type, entity_key, weekday, leadtime_bucket, currency, "
            "median_minor, mad_minor, n, computed_at"
            ") VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(entity_type, entity_key, weekday, leadtime_bucket, currency) "
            "DO UPDATE SET median_minor=excluded.median_minor, "
            "mad_minor=excluded.mad_minor, n=excluded.n, computed_at=excluded.computed_at",
            (etype, entity_key, weekday, bucket, currency, med, mad, len(prices), computed_at),
        )
        written += 1
    if entity_type == HOTEL:
        # Die Eigen- und die Peer-Ebene der Hotels haengen an derselben
        # Auffrischung, damit kein Aufrufer sie vergessen kann. Der Rueckgabewert
        # bleibt die Zahl der `price_baseline`-Zeilen: er bedeutet, was er
        # immer bedeutet hat.
        refresh_hotel_baselines(conn, now=now, min_samples=min_samples)
    return written


def ensure_hotel_baseline(conn: sqlite3.Connection) -> None:
    """Die Hoteltabelle anlegen, falls sie fehlt. Idempotent und billig."""
    conn.executescript(HOTEL_BASELINE_SCHEMA)


def _nights(value: Any) -> int:
    """Die Naechte aus `return_or_nights`. Bei Hotels steht dort eine Zahl."""
    try:
        return max(1, int(str(value)))
    except (TypeError, ValueError):
        return 1


def property_meta(conn: sqlite3.Connection) -> dict[str, tuple[str | None, str | None, int | None, str]]:
    """`property_key` auf Land, Stadt, Sterne und Name."""
    rows = conn.execute(
        "SELECT property_key, country_code, city, stars, name FROM hotel_property"
    ).fetchall()
    return {
        row["property_key"]: (
            row["country_code"], row["city"],
            int(row["stars"]) if row["stars"] is not None else None,
            row["name"] or "",
        )
        for row in rows
    }


def split_entity_key(entity_key: str) -> tuple[str, str]:
    """'<cc>|<property_key>' auseinandernehmen. `property_key` enthaelt kein '|'."""
    cc, _, property_key = str(entity_key).partition("|")
    return (cc or "XX"), (property_key or entity_key)


def refresh_hotel_baselines(conn: sqlite3.Connection, *, now: datetime | None = None,
                            min_samples: int = 5) -> int:
    """Beide Hotel-Ebenen neu rechnen und zurueckgeben, wie viele Zeilen entstanden.

    Die Peer-Ebene laesst Schlafsaal, Camping, Boot und Tageszimmer aus. Ein
    Bett fuer 18 Euro zieht den Median einer Vier-Sterne-Gruppe so weit nach
    unten, dass danach kein echter Fehler mehr auffaellt.
    """
    ensure_hotel_baseline(conn)
    computed_at = (now or datetime.now()).isoformat(timespec="seconds")
    meta = property_meta(conn)
    rows = conn.execute(
        "SELECT observed_at, entity_key, travel_date, currency, price_total_minor, "
        "party_size, return_or_nights FROM price_observation WHERE entity_type=?",
        (HOTEL,),
    ).fetchall()

    grouped: dict[tuple[str, str, int, str, str, str], list[int]] = {}
    for row in rows:
        weekday, bucket = _baseline_key(
            date.fromisoformat(row["travel_date"]),
            datetime.fromisoformat(row["observed_at"]),
        )
        stay = stay_key(int(row["party_size"]), _nights(row["return_or_nights"]))
        currency = row["currency"]
        price = int(row["price_total_minor"])
        entity_key = row["entity_key"]
        grouped.setdefault(
            (BASIS_OWN, entity_key, weekday, bucket, stay, currency), []
        ).append(price)

        cc, property_key = split_entity_key(entity_key)
        country, city, stars, name = meta.get(property_key, (cc, None, None, ""))
        if is_category_suspect(name):
            continue
        grouped.setdefault(
            (BASIS_PEER, peer_key(country or cc, city, stars), weekday, bucket, stay, currency),
            [],
        ).append(price)

    written = 0
    for (scope, group_key, weekday, bucket, stay, currency), prices in grouped.items():
        if len(prices) < min_samples:
            continue
        med = int(median(prices))
        mad = int(median([abs(p - med) for p in prices]))
        conn.execute(
            "INSERT INTO hotel_baseline("
            "scope, group_key, weekday, leadtime_bucket, stay_key, currency, "
            "median_minor, mad_minor, n, computed_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(scope, group_key, weekday, leadtime_bucket, stay_key, currency) "
            "DO UPDATE SET median_minor=excluded.median_minor, "
            "mad_minor=excluded.mad_minor, n=excluded.n, computed_at=excluded.computed_at",
            (scope, group_key, weekday, bucket, stay, currency,
             med, mad, len(prices), computed_at),
        )
        written += 1
    return written


def _hotel_baseline(conn: sqlite3.Connection, scope: str, group_key: str, weekday: int,
                    bucket: str, stay: str, currency: str) -> Baseline | None:
    row = conn.execute(
        "SELECT median_minor, mad_minor, n FROM hotel_baseline "
        "WHERE scope=? AND group_key=? AND weekday=? AND leadtime_bucket=? "
        "AND stay_key=? AND currency=?",
        (scope, group_key, weekday, bucket, stay, currency),
    ).fetchone()
    if row is None:
        return None
    return Baseline(int(row["median_minor"]), int(row["mad_minor"]), int(row["n"]), scope)


def pick_baseline(own: Baseline | None, peer: Baseline | None) -> Baseline | None:
    """Eigenhistorie, wenn sie traegt, sonst die Peer-Gruppe.

    Unter zehn eigenen Beobachtungen ist der Median wackelig und der MAD
    bricht zusammen; dann sagt die Gruppe mehr als das Objekt. Gibt es keine
    Gruppe, bleibt die duenne Eigenhistorie: sie ist immer noch besser als
    keine Aussage.
    """
    if own is not None and own.n >= THIN_HISTORY_N:
        return own
    if peer is not None:
        return peer
    return own


def _flight_signal(conn: sqlite3.Connection, entity_key: str, price_minor: int,
                   weekday: int, bucket: str, entity_type: str,
                   currency: str) -> dict[str, Any]:
    """Der Detektor der Flugsuche, Zeile fuer Zeile wie bisher.

    Nur die drei Stufen, kein `error`, keine Schranke, keine Peer-Gruppe.
    `tier` spiegelt hier `status`, damit ein gemeinsamer Aufrufer beide
    Domaenen gleich lesen kann, ohne dass sich am Urteil etwas aendert.
    """
    row = conn.execute(
        "SELECT median_minor, mad_minor, n FROM price_baseline "
        "WHERE entity_type=? AND entity_key=? AND weekday=? "
        "AND leadtime_bucket=? AND currency=?",
        (entity_type, entity_key, weekday, bucket, currency),
    ).fetchone()
    if row is None:
        return {
            "status": "unknown", "price_minor": price_minor,
            "tier": "unknown", "reason": "keine Baseline", "basis": "none", "n": 0,
        }
    baseline = Baseline(int(row["median_minor"]), int(row["mad_minor"]), int(row["n"]))
    status = band_status(price_minor, baseline)
    return {
        "status": status,
        "price_minor": price_minor,
        "median_minor": baseline.median_minor,
        "mad_minor": baseline.mad_minor,
        "n": baseline.n,
        "tier": status,
        "reason": BAND_REASON[status],
        "basis": BASIS_OWN,
    }


def detect_price_signal(conn: sqlite3.Connection, entity_key: str, travel_date: date,
                        price_minor: int, *, observed_at: datetime | None = None,
                        entity_type: str = "flight", currency: str = "EUR",
                        party_size: int = 1, nights: int = 1,
                        stars: int | None = None, name: str | None = None,
                        rates: Rates | None = None,
                        limits: PlausibilityLimits = DEFAULT_LIMITS) -> dict[str, Any]:
    """Ein Preis, ein Urteil.

    Der Rueckgabewert traegt weiterhin `status` mit seiner alten Bedeutung und
    zusaetzlich `tier`, `reason`, `basis` und `n`. Fuer `entity_type='flight'`
    bleibt alles beim Alten: dieselbe Tabelle, dieselben drei Stufen. Die
    vierte Stufe und die Vorfilter gelten nur fuer Hotels, wo sie gemessen
    wurden.
    """
    observed = observed_at or datetime.now()
    weekday, bucket = _baseline_key(travel_date, observed)
    if entity_type != HOTEL:
        return _flight_signal(
            conn, entity_key, price_minor, weekday, bucket, entity_type, currency
        )

    ensure_hotel_baseline(conn)
    stay = stay_key(party_size, nights)
    cc, property_key = split_entity_key(entity_key)
    row = conn.execute(
        "SELECT country_code, city, stars, name FROM hotel_property WHERE property_key=?",
        (property_key,),
    ).fetchone()
    country = (row["country_code"] if row else None) or cc
    city = row["city"] if row else None
    if stars is None and row is not None and row["stars"] is not None:
        stars = int(row["stars"])
    if name is None and row is not None:
        name = row["name"]

    own = _hotel_baseline(conn, BASIS_OWN, entity_key, weekday, bucket, stay, currency)
    peer = _hotel_baseline(
        conn, BASIS_PEER, peer_key(country, city, stars), weekday, bucket, stay, currency
    )
    signal = classify(
        price_minor,
        pick_baseline(own, peer),
        nights=nights,
        stars=stars,
        name=name,
        currency=currency,
        rates=rates,
        limits=limits,
    )
    return signal.as_dict()
