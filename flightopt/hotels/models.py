"""Werteobjekte der Hotelsuche.

Preise stehen wie ueberall im Projekt in Minor Units, damit nichts durch
Float-Drift verrutscht. `HotelQuery` ist genau eine Suche fuer genau einen
Anreisetag; der Zeitraum-Durchlauf baut sich daraus je Tag eine neue.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any, Mapping

from flightopt.domain.models import Money

# Trivago nennt das Land im Klartext ("Athens, Greece"), Booking gar nicht. Ein
# Land, das fehlt, wird deshalb "XX" und nicht geraten. Im `entity_key` steht es
# seit dem 2026-09-09 nicht mehr: ein Schluessel darf nichts enthalten, was eine
# Antwort weglassen kann (siehe `HotelOffer.entity_key`).
_COUNTRY_CODES: dict[str, str] = {
    "albania": "AL", "albanien": "AL",
    "argentina": "AR", "argentinien": "AR",
    "armenia": "AM", "armenien": "AM",
    "australia": "AU", "australien": "AU",
    "austria": "AT", "oesterreich": "AT", "österreich": "AT",
    "azerbaijan": "AZ", "aserbaidschan": "AZ",
    "belarus": "BY",
    "belgium": "BE", "belgien": "BE",
    "bosnia and herzegovina": "BA", "bosnien und herzegowina": "BA",
    "brazil": "BR", "brasilien": "BR",
    "bulgaria": "BG", "bulgarien": "BG",
    "canada": "CA", "kanada": "CA",
    "chile": "CL",
    "china": "CN",
    "colombia": "CO", "kolumbien": "CO",
    "croatia": "HR", "kroatien": "HR",
    "cyprus": "CY", "zypern": "CY",
    "czechia": "CZ", "czech republic": "CZ", "tschechien": "CZ",
    "denmark": "DK", "daenemark": "DK", "dänemark": "DK",
    "egypt": "EG", "aegypten": "EG", "ägypten": "EG",
    "estonia": "EE", "estland": "EE",
    "finland": "FI", "finnland": "FI",
    "france": "FR", "frankreich": "FR",
    "georgia": "GE", "georgien": "GE",
    "germany": "DE", "deutschland": "DE",
    "greece": "GR", "griechenland": "GR",
    "hungary": "HU", "ungarn": "HU",
    "iceland": "IS", "island": "IS",
    "india": "IN", "indien": "IN",
    "indonesia": "ID", "indonesien": "ID",
    "ireland": "IE", "irland": "IE",
    "israel": "IL",
    "italy": "IT", "italien": "IT",
    "japan": "JP",
    "jordan": "JO", "jordanien": "JO",
    "kenya": "KE", "kenia": "KE",
    "latvia": "LV", "lettland": "LV",
    "lithuania": "LT", "litauen": "LT",
    "luxembourg": "LU", "luxemburg": "LU",
    "malaysia": "MY",
    "malta": "MT",
    "mexico": "MX", "mexiko": "MX",
    "moldova": "MD", "moldau": "MD",
    "montenegro": "ME",
    "morocco": "MA", "marokko": "MA",
    "netherlands": "NL", "niederlande": "NL",
    "new zealand": "NZ", "neuseeland": "NZ",
    "north macedonia": "MK", "nordmazedonien": "MK",
    "norway": "NO", "norwegen": "NO",
    "oman": "OM",
    "peru": "PE",
    "poland": "PL", "polen": "PL",
    "portugal": "PT",
    "qatar": "QA", "katar": "QA",
    "romania": "RO", "rumaenien": "RO", "rumänien": "RO",
    "russia": "RU", "russland": "RU",
    "saudi arabia": "SA", "saudi-arabien": "SA",
    "serbia": "RS", "serbien": "RS",
    "singapore": "SG", "singapur": "SG",
    "slovakia": "SK", "slowakei": "SK",
    "slovenia": "SI", "slowenien": "SI",
    "south africa": "ZA", "suedafrika": "ZA", "südafrika": "ZA",
    "south korea": "KR", "suedkorea": "KR", "südkorea": "KR",
    "spain": "ES", "spanien": "ES",
    "sweden": "SE", "schweden": "SE",
    "switzerland": "CH", "schweiz": "CH",
    "thailand": "TH",
    "tunisia": "TN", "tunesien": "TN",
    "turkey": "TR", "tuerkei": "TR", "türkei": "TR",
    "ukraine": "UA",
    "united arab emirates": "AE", "vereinigte arabische emirate": "AE",
    "united kingdom": "GB", "great britain": "GB", "england": "GB",
    "grossbritannien": "GB", "großbritannien": "GB",
    "united states": "US", "united states of america": "US", "usa": "US",
    "vereinigte staaten": "US",
    "vietnam": "VN",
}

UNKNOWN_COUNTRY = "XX"


def country_code(name: str | None) -> str:
    """ISO-2 zu einem Landesnamen, sonst 'XX'.

    Ein bereits zweistelliger Grossbuchstaben-Code wird durchgereicht, damit
    eine Quelle, die schon Codes liefert, nicht durch die Tabelle muss.
    """
    if not name:
        return UNKNOWN_COUNTRY
    text = str(name).strip()
    if len(text) == 2 and text.isalpha():
        return text.upper()
    return _COUNTRY_CODES.get(text.casefold(), UNKNOWN_COUNTRY)


@dataclass(frozen=True, slots=True)
class HotelQuery:
    """Eine Suche fuer einen Anreisetag.

    `children` traegt die Alter, nicht bloss die Anzahl: Booking will je Kind
    ein `age`, Trivago eine Kette wie "10-12-14". Eine reine Zahl waere fuer
    beide Quellen zu wenig Information.
    """

    destination: str
    arrival: date
    nights: int = 1
    adults: int = 2
    children: tuple[int, ...] = ()
    rooms: int = 1
    stars: tuple[int, ...] = ()
    country: str = "DE"
    currency: str = "EUR"
    min_review_score: float | None = None

    def __post_init__(self) -> None:
        if not str(self.destination).strip():
            raise ValueError("Hotelsuche ohne Ziel")
        if self.nights < 1:
            raise ValueError("Eine Uebernachtung ist das Minimum")
        if self.adults < 1:
            raise ValueError("Mindestens ein Erwachsener")
        if self.rooms < 1:
            raise ValueError("Mindestens ein Zimmer")
        bad = [s for s in self.stars if not 1 <= int(s) <= 5]
        if bad:
            raise ValueError(f"Sterne ausserhalb von 1 bis 5: {bad}")

    @property
    def departure(self) -> date:
        return self.arrival + timedelta(days=self.nights)

    @property
    def party_size(self) -> int:
        """Wie viele Personen der Preis abdeckt.

        Die Baseline vergleicht nur Beobachtungen gleicher Belegung, sonst
        steht ein Familienzimmer neben einem Einzelzimmer und jeder zweite
        Preis sieht nach Fehler aus.
        """
        return self.adults + len(self.children)

    def on(self, day: date) -> "HotelQuery":
        """Dieselbe Suche, anderer Anreisetag."""
        return replace(self, arrival=day)

    def as_dict(self) -> dict[str, Any]:
        return {
            "destination": self.destination,
            "arrival": self.arrival.isoformat(),
            "nights": self.nights,
            "adults": self.adults,
            "children": list(self.children),
            "rooms": self.rooms,
            "stars": list(self.stars),
            "country": self.country,
            "currency": self.currency,
            "min_review_score": self.min_review_score,
        }


@dataclass(frozen=True, slots=True)
class HotelOffer:
    """Ein Preis fuer ein Objekt an einem Datum, wie eine Quelle ihn nennt.

    `price_total` ist der Originalwert in der Waehrung der Quelle, `price_eur`
    die Umrechnung. Beide werden mitgefuehrt, damit die Oberflaeche einen
    umgerechneten Preis auch als solchen ausweisen kann.
    """

    source: str
    property_key: str
    name: str
    arrival: date
    departure: date
    price_total: Money
    price_eur: Money | None = None
    stars: int | None = None
    city: str | None = None
    country: str | None = None
    review_rating: float | None = None
    review_count: int | None = None
    url: str | None = None
    lat: float | None = None
    lon: float | None = None
    advertisers: str | None = None
    party_size: int = 1
    rooms: int = 1
    indicative: bool = True
    """Wahr, solange der Preis ein Richtwert ist und kein gebuchter Tarif.

    Trivago sortiert seinen prominentesten Preis monetarisiert; das
    australische Bundesgericht hat 2022 festgestellt, dass er in zwei von drei
    Faellen nicht der guenstigste war. Also nie als Bestpreis ausgeben.
    """

    @property
    def nights(self) -> int:
        return max(1, (self.departure - self.arrival).days)

    @property
    def country_code(self) -> str:
        return country_code(self.country)

    @property
    def entity_key(self) -> str:
        """Der `property_key`, damit die Baseline je Objekt rechnet.

        Frueher stand der Laendercode davor: '<cc>|<property_key>'. Eindeutig
        war der Schluessel dadurch nicht mehr - `property_key` traegt bereits
        Quelle und Objekt-ID -, aber instabil: liefert eine Quelle das Land
        einmal nicht, wandert dieselbe Unterkunft nach 'XX|...' und ihre
        Historie spaltet sich in zwei Grundgesamtheiten, von denen keine mehr
        die fuenf Beobachtungen erreicht, ab denen es eine Baseline gibt. Ein
        Schluessel darf nichts enthalten, was eine Antwort weglassen kann.

        Das Land bleibt, wo es hingehoert: am Angebot und in den Stammdaten.
        Von dort holt es die Peer-Gruppe, und dort ist es gegen fehlende
        Antworten geschuetzt.
        """
        return self.property_key

    @property
    def price(self) -> Money:
        """Der Preis, mit dem gerechnet wird: EUR, wenn er vorliegt."""
        return self.price_eur or self.price_total

    @property
    def price_per_night(self) -> Money:
        price = self.price
        return Money(round(price.minor / self.nights), price.currency)

    @property
    def converted(self) -> bool:
        """Wahr, wenn der angezeigte Preis nicht die Waehrung der Quelle ist."""
        return (
            self.price_eur is not None
            and self.price_eur.currency != self.price_total.currency
        )

    def with_eur(self, price_eur: Money) -> "HotelOffer":
        return replace(self, price_eur=price_eur)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "property_key": self.property_key,
            "name": self.name,
            "arrival": self.arrival.isoformat(),
            "departure": self.departure.isoformat(),
            "price_minor": self.price_total.minor,
            "currency": self.price_total.currency,
            "price_eur_minor": self.price_eur.minor if self.price_eur else None,
            "stars": self.stars,
            "city": self.city,
            "country": self.country,
            "review_rating": self.review_rating,
            "review_count": self.review_count,
            "url": self.url,
            "lat": self.lat,
            "lon": self.lon,
            "advertisers": self.advertisers,
            "party_size": self.party_size,
            "rooms": self.rooms,
            "indicative": self.indicative,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "HotelOffer":
        eur = raw.get("price_eur_minor")
        return cls(
            source=str(raw["source"]),
            property_key=str(raw["property_key"]),
            name=str(raw["name"]),
            arrival=date.fromisoformat(str(raw["arrival"])),
            departure=date.fromisoformat(str(raw["departure"])),
            price_total=Money(int(raw["price_minor"]), str(raw["currency"])),
            price_eur=Money(int(eur), "EUR") if eur is not None else None,
            stars=raw.get("stars"),
            city=raw.get("city"),
            country=raw.get("country"),
            review_rating=raw.get("review_rating"),
            review_count=raw.get("review_count"),
            url=raw.get("url"),
            lat=raw.get("lat"),
            lon=raw.get("lon"),
            advertisers=raw.get("advertisers"),
            party_size=int(raw.get("party_size", 1)),
            rooms=int(raw.get("rooms", 1)),
            indicative=bool(raw.get("indicative", True)),
        )
