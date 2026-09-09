"""Hotelbeobachtungen auf dieselbe Historie schreiben wie die Fluege.

`price_observation` ist domaenenneutral, also wird sie benutzt und nicht
umgebaut: `entity_type='hotel'`, `entity_key='<property_key>'`,
`return_or_nights` traegt die Naechte, `party_size` die Belegung. Ohne diese
beiden wuerde die Baseline ein Familienzimmer fuer drei Naechte neben ein
Einzelzimmer fuer eine legen und jeden zweiten Preis fuer einen Fehler halten.

Der Schluessel trug bis zum 2026-09-09 den Laendercode als Praefix. Er war
damit nicht eindeutiger - `property_key` traegt schon Quelle und Objekt-ID -,
aber instabil: eine Antwort ohne Land verschob dieselbe Unterkunft nach
'XX|...' und spaltete ihre Historie. Bestandszeilen liest
`storage.baseline.split_entity_key` weiter, umgeschrieben werden sie von
`scripts/migrate_hotel_entity_keys.py`.

Gespeichert wird in Euro. Eine Baseline rechnet je Waehrung, und eine
Unterkunft, die heute in USD und morgen in EUR ausgeliefert wird, haette sonst
zwei Historien mit je zu wenig Datenpunkten. Der Originalwert geht dabei nicht
verloren: er bleibt am `HotelOffer` haengen und wird in Tabelle und Oberflaeche
als umgerechnet ausgewiesen, genauso wie bei den Fluegen.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from flightopt.domain import fx
from flightopt.domain.fx import Rates, UnknownCurrency
from flightopt.hotels.models import UNKNOWN_COUNTRY, HotelOffer
from flightopt.hotels.registry import source_is_indicative
from flightopt.hotels.signals import with_population
from flightopt.storage import db
from flightopt.storage.baseline import detect_price_signal
from flightopt.storage.cache import SqliteHistory

ENTITY_TYPE = "hotel"

HOTEL_BASELINE_SPLITS_POPULATIONS = True
"""Trennt `hotel_baseline` Richtwerte und Haendlerpreise? Seit dem 2026-09-09 ja.

`flight_baseline` traegt `is_estimate` im Primaerschluessel und rechnet
deshalb je Grundgesamtheit. Die Hoteltabelle konnte das lange nicht: ein
zusammengesetzter Primaerschluessel laesst sich in SQLite nicht per ALTER
TABLE erweitern. Sie traegt jetzt `population` im Schluessel, angelegt beim
Neubau der Tabelle in `storage.baseline.ensure_hotel_baseline`.

Solange der Wert falsch war, hing an jedem Urteil ueber einen Haendlerpreis
der Zusatz "Vergleichsgruppe enthaelt auch Richtwerte". Er faellt jetzt von
selbst weg - `signals.with_population` haengt ihn nur an, solange nicht
getrennt wird.

Die Konstante bleibt stehen und wird nicht geloescht: die Oberflaeche liest
sie als `population_split` an jeder Zeile, und sie ist die eine Stelle, an
der nachlesbar steht, wie stark ein `error` ueberhaupt ist. Was der Preis der
Trennung ist, steht in `docs/PRICE_HISTORY.md`, Abschnitt 8: jede
Grundgesamtheit braucht ihre eigenen fuenf Beobachtungen, und bis die
zusammen sind, gibt es fuer sie kein Urteil.
"""


@dataclass(slots=True)
class ObservationReport:
    written: int = 0
    offers: list[HotelOffer] = field(default_factory=list)
    """Die geschriebenen Angebote, jeweils mit Original- und Europreis."""
    skipped: list[str] = field(default_factory=list)


def normalise(offer: HotelOffer, rates: Rates) -> HotelOffer | None:
    """Den Europreis anhaengen. `None`, wenn es fuer die Waehrung keinen Kurs gibt.

    Eine Beobachtung ohne belastbaren Kurs wird nicht geschrieben. Sie mit dem
    Fremdwaehrungsbetrag in eine Euro-Baseline zu legen waere schlimmer als sie
    wegzulassen: aus 28700 JPY wuerde ein Preisfehler von 287 Euro.
    """
    if offer.price_eur is not None:
        return offer
    try:
        return offer.with_eur(fx.convert(offer.price_total, "EUR", rates))
    except UnknownCurrency:
        return None


def upsert_property(
    conn: sqlite3.Connection, offer: HotelOffer, *, now: str | None = None
) -> None:
    """Stammdaten anlegen oder auffrischen.

    `first_seen` bleibt stehen, `last_seen` wandert mit. Leere Felder einer
    spaeteren Antwort ueberschreiben keine gefuellten: eine Quelle, die die
    Koordinaten heute weglaesst, soll sie nicht loeschen.

    Beim Landescode heisst "leer" nicht NULL, sondern der Platzhalter
    `UNKNOWN_COUNTRY`. Ohne das `NULLIF` liess COALESCE ihn durch, und eine
    Antwort ohne Land ersetzte einen richtigen ISO-Code - danach zeigt der
    Nachbarschluessel der Baseline auf eine andere Grundgesamtheit.
    """
    stamp = now or db.now()
    conn.execute(
        "INSERT INTO hotel_property("
        "property_key, source, name, city, country, country_code, stars, lat, lon, "
        "review_rating, review_count, url, first_seen, last_seen) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(property_key) DO UPDATE SET "
        "source=excluded.source, name=excluded.name, "
        "city=COALESCE(excluded.city, hotel_property.city), "
        "country=COALESCE(excluded.country, hotel_property.country), "
        f"country_code=COALESCE(NULLIF(excluded.country_code, '{UNKNOWN_COUNTRY}'), "
        "hotel_property.country_code), "
        "stars=COALESCE(excluded.stars, hotel_property.stars), "
        "lat=COALESCE(excluded.lat, hotel_property.lat), "
        "lon=COALESCE(excluded.lon, hotel_property.lon), "
        "review_rating=COALESCE(excluded.review_rating, hotel_property.review_rating), "
        "review_count=COALESCE(excluded.review_count, hotel_property.review_count), "
        "url=COALESCE(excluded.url, hotel_property.url), "
        "last_seen=excluded.last_seen",
        (
            offer.property_key,
            offer.source,
            offer.name,
            offer.city,
            offer.country,
            offer.country_code,
            offer.stars,
            offer.lat,
            offer.lon,
            offer.review_rating,
            offer.review_count,
            offer.url,
            stamp,
            stamp,
        ),
    )


async def record_offers(
    conn: sqlite3.Connection,
    offers: Iterable[HotelOffer],
    *,
    rates: Rates,
    observed_at: datetime | None = None,
) -> ObservationReport:
    """Jede Zeile als Beobachtung wegschreiben, nicht nur die Treffer.

    BookingX speicherte ausschliesslich Preisfehler und brauchte deshalb feste
    Schwellen je Sternekategorie. Wer jede Beobachtung behaelt, bekommt Median
    und MAD geschenkt und braucht die Schwelle nicht.
    """
    history = SqliteHistory(conn)
    stamp = observed_at or datetime.now()
    report = ObservationReport()

    for offer in offers:
        priced = normalise(offer, rates)
        if priced is None or priced.price_eur is None:
            report.skipped.append(
                f"{offer.name}: kein Kurs fuer {offer.price_total.currency}"
            )
            continue
        upsert_property(conn, priced, now=stamp.isoformat(timespec="seconds"))
        await history.record(
            source=priced.source,
            entity_type=ENTITY_TYPE,
            entity_key=priced.entity_key,
            travel_date=priced.arrival,
            price=priced.price_eur,
            observed_at=stamp,
            return_or_nights=str(priced.nights),
            party_size=priced.party_size,
            # Zwei Kennzeichen, zwei Fragen. `is_estimate` gilt dem Preis:
            # Richtwert oder die Zahl, die der Haendler selbst anzeigt.
            # `is_indicative` gilt der Quelle: Vergleichsportal mit bekanntem
            # Aufschlag oder nicht. Bisher stand in der zweiten Spalte bei
            # jeder Hotelzeile eine Null - nicht "nein", sondern nichts. Wer
            # spaeter zwei Grundgesamtheiten trennen will, braucht sie, und
            # eine fortschreibende Historie laesst sich nicht nachtragen.
            is_estimate=priced.indicative,
            is_indicative=source_is_indicative(priced.source),
        )
        report.offers.append(priced)
        report.written += 1
    return report


def signal_for(
    conn: sqlite3.Connection, offer: HotelOffer, *, observed_at: datetime | None = None
) -> dict[str, Any]:
    """Das Preissignal zu einem Angebot.

    Derselbe Detektor wie bei den Fluegen, nur mit `entity_type='hotel'`.
    Verglichen wird der Gesamtpreis in Euro, also genau das, was auch
    geschrieben wurde. Ohne Baseline steht hier 'unknown' und nicht 'normal':
    keine Aussage ist etwas anderes als die Aussage, alles sei in Ordnung.

    Belegung, Naechte, Sterne und Name gehen mit: ohne sie vergleicht der
    Detektor ein Familienzimmer fuer drei Naechte mit einem Einzelzimmer fuer
    eine und haelt jeden zweiten Preis fuer einen Fehler.

    Zum Schluss kommt die Grundgesamtheit dazu. `is_estimate` waehlt seit dem
    2026-09-09 auch bei Hotels die Verteilung aus, gegen die gerechnet wird -
    `hotel_baseline` traegt `population` im Schluessel. Gibt es fuer die Art
    dieses Preises keine Baseline, bleibt es bei `unknown`; die andere zu
    nehmen waere eine Antwort auf eine Frage, die niemand gestellt hat.

    An der Zeile steht sie trotzdem weiter, und zwar aus einem zweiten Grund:
    die Oberflaeche soll sagen koennen, ob ein `error` gegen Richtwerte oder
    gegen Haendlerpreise gemessen wurde. Das sind zwei verschieden starke
    Aussagen, auch wenn beide sauber getrennt gerechnet sind.
    """
    price = offer.price_eur or offer.price_total
    signal = detect_price_signal(
        conn,
        offer.entity_key,
        offer.arrival,
        price.minor,
        observed_at=observed_at,
        entity_type=ENTITY_TYPE,
        currency=price.currency,
        party_size=offer.party_size,
        nights=offer.nights,
        stars=offer.stars,
        name=offer.name,
        is_estimate=offer.indicative,
    )
    return with_population(
        signal, indicative=offer.indicative, split=HOTEL_BASELINE_SPLITS_POPULATIONS
    )
