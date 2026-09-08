"""Price baselines and simple anomaly signals.

Fluege rechnen gegen `flight_baseline`, Hotels gegen `hotel_baseline`, und
`price_baseline` traegt weiter die Hotelzeilen, die es immer getragen hat.
Drei Tabellen fuer eine Idee sehen nach zu viel aus; der Grund ist jedes Mal
derselbe und banal: der Primaerschluessel ist zusammengesetzt, und den
erweitert SQLite nicht per `ALTER TABLE`. Eine neue Tabelle legt
`CREATE TABLE IF NOT EXISTS` dagegen auch in einer bestehenden Datei an.

Hotels brauchen zwei Dinge mehr: die Belegung samt Naechtezahl im Schluessel
und eine zweite Ebene fuer duenne Historie. Das steht in `hotel_baseline`.

Fluege brauchen ein Drittes: die Grundgesamtheit. Zwei Regeln entscheiden, was
ueberhaupt in eine Flug-Baseline darf, und beide sagen dasselbe - Gleiches
gehoert zu Gleichem:

1. Beobachtungen mit `is_indicative=1` bleiben draussen. Ein Vergleichsportal
   mit bekanntem systematischem Aufschlag preist ein anderes Produkt als der
   Direkttarif einer Airline (jede Airline, bis zu einem Umstieg, plus
   Marge). Der Median einer gemischten Verteilung misst weder das eine noch
   das andere.
2. `is_estimate` steht im Schluessel. Ein Kalenderpreis ist der Tagesbestpreis
   irgendeines Flugs, ein geprueter Preis gehoert zu einem bestimmten Flug und
   enthaelt Gepaeck- und Zuschlagsanteile anders. Die eine Zahl gegen die
   andere zu halten vergleicht Aepfel mit Birnen.

Was uebrig bleibt, ist weniger - aber es misst etwas. Wo nach beiden Regeln zu
wenig uebrig bleibt, gibt es keine Baseline und damit `unknown`. Das ist die
richtige Antwort und keine Luecke.
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
FLIGHT = "flight"

POPULATION_ESTIMATE = "estimate"
POPULATION_VERIFIED = "verified"


def population(is_estimate: bool) -> str:
    """Der Name der Grundgesamtheit, wie ihn die Oberflaeche zeigt."""
    return POPULATION_ESTIMATE if is_estimate else POPULATION_VERIFIED

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


def _median_and_mad(prices: list[int]) -> tuple[int, int]:
    med = int(median(prices))
    return med, int(median([abs(p - med) for p in prices]))


def refresh_baselines(conn: sqlite3.Connection, *, now: datetime | None = None,
                      entity_type: str = FLIGHT, min_samples: int = 5) -> int:
    """Die Baselines eines Bereichs neu rechnen. Zurueck kommt die Zeilenzahl.

    Fluege und Hotels teilen sich die Beobachtungen, aber nicht die Rechnung.
    Fuer Hotels bleibt hier alles, wie es war - dieselbe Tabelle, dieselben
    Gruppen, dieselbe Zahl. Fuer Fluege gelten die beiden Regeln aus dem
    Modulkopf, und das Ergebnis steht in `flight_baseline`.
    """
    if entity_type != HOTEL:
        return refresh_flight_baselines(
            conn, now=now, entity_type=entity_type, min_samples=min_samples
        )

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
        med, mad = _median_and_mad(prices)
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
    # Die Eigen- und die Peer-Ebene der Hotels haengen an derselben
    # Auffrischung, damit kein Aufrufer sie vergessen kann. Der Rueckgabewert
    # bleibt die Zahl der `price_baseline`-Zeilen: er bedeutet, was er
    # immer bedeutet hat.
    refresh_hotel_baselines(conn, now=now, min_samples=min_samples)
    return written


def refresh_flight_baselines(conn: sqlite3.Connection, *, now: datetime | None = None,
                             entity_type: str = FLIGHT, min_samples: int = 5) -> int:
    """Je Strecke, Wochentag, Vorlauf, Waehrung und Grundgesamtheit ein Median.

    Richtwert-Beobachtungen bleiben schon in der Abfrage draussen, nicht erst
    beim Rechnen: sie sollen weder den Median noch die Streuung noch `n`
    beruehren. `n` zaehlt danach genau die Preise, die tatsaechlich verglichen
    wurden - eine Baseline aus acht Punkten darf nicht wie eine aus zwanzig
    aussehen.
    """
    computed_at = (now or datetime.now()).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT observed_at, entity_key, travel_date, currency, price_total_minor, "
        "is_estimate FROM price_observation "
        "WHERE entity_type=? AND is_indicative=0",
        (entity_type,),
    ).fetchall()
    grouped: dict[tuple[str, int, str, str, int], list[int]] = {}
    for row in rows:
        observed_at = datetime.fromisoformat(row["observed_at"])
        travel_date = date.fromisoformat(row["travel_date"])
        weekday, bucket = _baseline_key(travel_date, observed_at)
        key = (
            row["entity_key"], weekday, bucket, row["currency"], int(row["is_estimate"])
        )
        grouped.setdefault(key, []).append(int(row["price_total_minor"]))

    written = 0
    for (entity_key, weekday, bucket, currency, is_estimate), prices in grouped.items():
        if len(prices) < min_samples:
            continue
        med, mad = _median_and_mad(prices)
        conn.execute(
            "INSERT INTO flight_baseline("
            "entity_key, weekday, leadtime_bucket, currency, is_estimate, "
            "median_minor, mad_minor, n, computed_at"
            ") VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(entity_key, weekday, leadtime_bucket, currency, is_estimate) "
            "DO UPDATE SET median_minor=excluded.median_minor, "
            "mad_minor=excluded.mad_minor, n=excluded.n, computed_at=excluded.computed_at",
            (entity_key, weekday, bucket, currency, is_estimate,
             med, mad, len(prices), computed_at),
        )
        written += 1
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
                   weekday: int, bucket: str, currency: str,
                   is_estimate: bool) -> dict[str, Any]:
    """Der Detektor der Flugsuche: drei Stufen, jetzt gegen die eigene Klasse.

    Weiterhin kein `error`, keine Schranke, keine Peer-Gruppe. Neu ist einzig,
    dass die Baseline zur Art des Preises passt. Findet sich fuer diese Art
    keine, bleibt es bei `unknown` - die Baseline der anderen Art zu nehmen
    waere eine Antwort auf eine Frage, die niemand gestellt hat.
    """
    row = conn.execute(
        "SELECT median_minor, mad_minor, n FROM flight_baseline "
        "WHERE entity_key=? AND weekday=? AND leadtime_bucket=? "
        "AND currency=? AND is_estimate=?",
        (entity_key, weekday, bucket, currency, int(is_estimate)),
    ).fetchone()
    group = population(is_estimate)
    if row is None:
        return {
            "status": "unknown", "price_minor": price_minor,
            "tier": "unknown", "reason": "keine Baseline", "basis": "none", "n": 0,
            "population": group, "thin": False,
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
        "population": group,
        # Fuenf Punkte reichen fuer einen Median und sind trotzdem duenn. Wer
        # das Urteil liest, soll sehen, worauf es steht.
        "thin": baseline.thin,
    }


def detect_price_signal(conn: sqlite3.Connection, entity_key: str, travel_date: date,
                        price_minor: int, *, observed_at: datetime | None = None,
                        entity_type: str = FLIGHT, currency: str = "EUR",
                        party_size: int = 1, nights: int = 1,
                        stars: int | None = None, name: str | None = None,
                        is_estimate: bool = True,
                        rates: Rates | None = None,
                        limits: PlausibilityLimits = DEFAULT_LIMITS) -> dict[str, Any]:
    """Ein Preis, ein Urteil.

    Der Rueckgabewert traegt weiterhin `status` mit seiner alten Bedeutung und
    zusaetzlich `tier`, `reason`, `basis` und `n`. Fuer Fluege kommen
    `population` und `thin` dazu: gegen welche Grundgesamtheit gerechnet wurde
    und ob sie duenn ist.

    `is_estimate` sagt, welcher Art der uebergebene Preis ist - eine Schaetzung
    aus dem Kalender oder ein gepruefter Live-Preis. Danach richtet sich, gegen
    welche Baseline er gehalten wird. Bei Hotels bleibt der Parameter ohne
    Wirkung: dort steckt dieselbe Unterscheidung in `hotel_baseline` gar nicht
    erst drin, und die vierte Stufe und die Vorfilter gelten weiterhin nur
    dort, wo sie gemessen wurden.
    """
    observed = observed_at or datetime.now()
    weekday, bucket = _baseline_key(travel_date, observed)
    if entity_type != HOTEL:
        return _flight_signal(
            conn, entity_key, price_minor, weekday, bucket, currency, is_estimate
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
