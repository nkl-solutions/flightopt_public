"""Vorfilter der Preisfehler-Erkennung: was gar nicht erst in die Statistik darf.

Drei Pruefungen, alle rein und ohne Datenbank, damit sie einzeln testbar sind:

1. **Encoding.** Ein Preis, der genau um Faktor 100 oder genau um einen
   Tageskurs neben dem Median liegt, ist ein Fehler in den Daten und kein
   Fehler im Angebot. Gemeldet wird er trotzdem nicht: er wird aussortiert und
   gezaehlt.
2. **Vergleichbarkeit.** Nur gleiche Belegung und gleiche Naechtezahl gehoeren
   in dieselbe Verteilung. Preis pro Person gegen Preis pro Zimmer ist der
   haeufigste Faktor-Zwei-Fehlalarm.
3. **Kategorie.** Schlafsaal, Campingplatz, Boot und Tageszimmer sind keine
   Hotelzimmer. Booking filtert seit Phase 1 auf `ht_id=204`, Trivago liefert
   die Kategorie gar nicht mit, also bleibt der Name als einziges Merkmal.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

from flightopt.domain import fx
from flightopt.domain.fx import Rates

ENCODING_SUSPECT = "encoding_suspect"

DECIMAL_FACTORS: tuple[float, ...] = (0.01, 100.0)
"""Ein verrutschtes Dezimalkomma verschiebt exakt um zwei Stellen."""

CURRENCY_BAND: tuple[float, float] = (0.85, 1.7)
"""Kurse in diesem Band sind gefaehrlich, weil ihr Ergebnis plausibel aussieht.

JPY oder KRW faellt sofort auf: aus 90 Euro werden 15000. USD, CHF, GBP, CAD,
AUD und SGD liegen dagegen so nah an eins, dass die Verwechslung wie ein
gutes Angebot wirkt. Welche Waehrungen das gerade sind, entscheidet der
Kurszettel und nicht eine feste Liste in diesem Modul.
"""

TOLERANCE = 0.025
"""Wie nah am Faktor es sein muss. Bewusst eng.

Bei 0,3 waere jedes Angebot mit einem Drittel Rabatt ein Kandidat fuer die
Waehrungsverwechslung; 60 Euro bei Median 90 traefe zufaellig 1/1,50 (CAD).
Zweieinhalb Prozent trifft nur, was wirklich auf dem Kurs sitzt.
"""

CATEGORY_TOKENS: tuple[str, ...] = (
    "hostel", "hostels", "dorm", "dorms", "dormitory", "dormitorio",
    "schlafsaal", "mehrbettzimmer", "backpacker", "backpackers",
    "camping", "campingplatz", "campground", "campsite", "glamping",
    "boat", "boot", "houseboat", "hausboot", "boatel", "botel",
    "dayuse", "day use", "tageszimmer", "tagesraum",
    "capsule", "capsulehotel", "kapsel", "kapselhotel",
)
"""Wortmarken, keine Teilzeichenketten.

"Hostellerie du Cerf" ist ein Hotel und kein Schlafsaal, "Bootshaus" kein
Boot. Deshalb Wortgrenzen: sonst filtert die Liste mehr weg, als sie findet.
"""

_CATEGORY_RE = re.compile(
    r"\b(" + "|".join(sorted(CATEGORY_TOKENS, key=len, reverse=True)) + r")\b"
)

_UMLAUTS = str.maketrans({"ä": "a", "ö": "o", "ü": "u", "é": "e", "è": "e", "ß": "s"})


@dataclass(frozen=True, slots=True)
class EncodingCheck:
    """Das Urteil der Encoding-Pruefung."""

    suspect: bool
    kind: str | None = None
    """'dezimal' oder 'waehrung', sonst None."""
    detail: str | None = None
    """Der getroffene Faktor: 'x100', '/100' oder der Waehrungscode."""
    factor: float = 0.0
    reason: str = ""


@lru_cache(maxsize=1)
def default_rates() -> Rates:
    """Der eingecheckte Kurs-Schnappschuss, einmal geladen.

    Die Pruefung darf niemals ins Netz: sie laeuft pro Beobachtung. Ein
    fehlender Schnappschuss schaltet nur die Waehrungspruefung ab, die
    Dezimalpruefung braucht ihn nicht.
    """
    try:
        return fx.load_fallback()
    except Exception:  # noqa: BLE001 - ohne Kurse bleibt die Dezimalpruefung
        return Rates(base="EUR", rates={})


def suspect_currencies(
    rates: Rates | None = None, *, band: tuple[float, float] = CURRENCY_BAND
) -> tuple[tuple[str, float], ...]:
    """Die Waehrungen, deren Kurs nahe genug an eins liegt, um zu taeuschen."""
    table = rates if rates is not None else default_rates()
    low, high = band
    return tuple(
        sorted(
            (code.upper(), float(rate))
            for code, rate in table.rates.items()
            if low <= float(rate) <= high
        )
    )


def _near(value: float, target: float, tolerance: float) -> bool:
    return abs(value - target) <= abs(target) * tolerance


def encoding_check(
    price_minor: int,
    median_minor: int | None,
    *,
    rates: Rates | None = None,
    tolerance: float = TOLERANCE,
) -> EncodingCheck:
    """Sitzt der Preis auf einem Umrechnungsfaktor statt auf einem Angebot?

    Geprueft wird beides: der Kurs und sein Kehrwert. Eine in USD gelieferte
    Zeile, die als Euro verbucht wurde, steht um den Kurs zu hoch; eine
    Euro-Zeile gegen eine in USD gebaute Historie um den Kehrwert zu tief.
    """
    if not median_minor or median_minor <= 0 or price_minor <= 0:
        return EncodingCheck(False)
    ratio = price_minor / median_minor

    for factor in DECIMAL_FACTORS:
        if _near(ratio, factor, tolerance):
            detail = "x100" if factor > 1 else "/100"
            return EncodingCheck(
                True, "dezimal", detail, ratio,
                f"Dezimalfehler, Faktor {detail} zum Median",
            )

    for code, rate in suspect_currencies(rates):
        for candidate in (rate, 1.0 / rate):
            if _near(ratio, candidate, tolerance):
                return EncodingCheck(
                    True, "waehrung", code, ratio,
                    f"Waehrungsverwechslung, Faktor entspricht {code}",
                )
    return EncodingCheck(False)


def stay_key(party_size: int, nights: int) -> str:
    """'p<Belegung>n<Naechte>'. Der Schluessel, der Verteilungen trennt.

    Ohne ihn liegt ein Familienzimmer fuer drei Naechte in derselben
    Verteilung wie ein Einzelzimmer fuer eine, und jeder zweite Preis sieht
    nach Fehler aus.
    """
    return f"p{max(1, int(party_size))}n{max(1, int(nights))}"


def comparison_key(
    entity_key: str,
    *,
    party_size: int,
    nights: int,
    weekday: int,
    leadtime_bucket: str,
    currency: str,
) -> tuple[str, int, str, str, str]:
    """Der vollstaendige Vergleichsschluessel einer Beobachtung."""
    return (
        entity_key,
        int(weekday),
        str(leadtime_bucket),
        stay_key(party_size, nights),
        currency.upper(),
    )


def peer_key(country_code: str | None, city: str | None, stars: int | None) -> str:
    """'<cc>|<stadt>|<sterne>'. Die Gruppe, gegen die duenne Historie rechnet.

    Fehlt die Stadt, bleibt der Laendercode. Das ist grob, aber ehrlich: eine
    Peer-Gruppe aus ganz Griechenland ist immer noch belastbarer als sechs
    eigene Beobachtungen.
    """
    cc = (country_code or "XX").upper()
    place = (city or "").strip().casefold() or "-"
    return f"{cc}|{place}|{stars if stars else '-'}"


def normalise_name(name: str | None) -> str:
    """Klein, ohne Umlaute, Satzzeichen zu Leerzeichen."""
    if not name:
        return ""
    folded = str(name).casefold().translate(_UMLAUTS)
    return re.sub(r"[^a-z0-9]+", " ", folded).strip()


def category_token(name: str | None) -> str | None:
    """Das gefundene Merkmal, sonst None."""
    match = _CATEGORY_RE.search(normalise_name(name))
    return match.group(1) if match else None


def is_category_suspect(name: str | None) -> bool:
    """Wahr, wenn der Name auf Schlafsaal, Camping, Boot oder Tageszimmer deutet."""
    return category_token(name) is not None


def count_verdicts(tiers: Iterable[str]) -> dict[str, int]:
    """Wie oft welche Stufe vorkam. Fuer die Zeile 'x aussortiert' im Report."""
    return dict(Counter(str(tier) for tier in tiers))
