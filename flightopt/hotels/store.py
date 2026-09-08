"""Hotelbeobachtungen auf dieselbe Historie schreiben wie die Fluege.

`price_observation` ist domaenenneutral, also wird sie benutzt und nicht
umgebaut: `entity_type='hotel'`, `entity_key='<cc>|<property_key>'`,
`return_or_nights` traegt die Naechte, `party_size` die Belegung. Ohne diese
beiden wuerde die Baseline ein Familienzimmer fuer drei Naechte neben ein
Einzelzimmer fuer eine legen und jeden zweiten Preis fuer einen Fehler halten.

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
from flightopt.hotels.models import HotelOffer
from flightopt.storage import db
from flightopt.storage.baseline import detect_price_signal
from flightopt.storage.cache import SqliteHistory

ENTITY_TYPE = "hotel"


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
        "country_code=COALESCE(excluded.country_code, hotel_property.country_code), "
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
            is_estimate=priced.indicative,
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
    """
    price = offer.price_eur or offer.price_total
    return detect_price_signal(
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
    )
