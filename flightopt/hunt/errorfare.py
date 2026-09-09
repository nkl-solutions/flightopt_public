"""Die vierte Stufe fuer Fluege: `error`.

Hotels haben sie schon (`flightopt/hotels/signals.py`). Nachbauen laesst sie
sich trotzdem nicht, und der Grund steht dort in einer einzigen Zeile: die
dritte Hotelbedingung ist eine **Plausibilitaetsschranke je
Sternekategorie**. Fluege haben keine Sterne. Sie haben etwas anderes, das
genauso wenig verhandelbar ist und das ohne jede Historie feststeht: eine
**Entfernung**. Die Koordinaten liegen im Repo, `airports.distance_km` rechnet
sie aus.

## Die Regel

Ein Flugpreis ist ein Fehltarif, wenn er nicht indikativ ist **und** eine der
beiden folgenden Lagen vorliegt:

1. **Beide Winkel zusammen.** Der Preis liegt unter der Schranke, die die
   Entfernung setzt, **und** er liegt statistisch ausserhalb dessen, was diese
   Strecke bisher gekostet hat.
2. **Die Entfernung allein, aber deutlich.** Der Preis liegt unter einer
   zweiten, viel tieferen Schranke, und die Strecke ist lang genug, dass ein
   Lockangebot als Erklaerung ausscheidet.

Alles andere bleibt `cheap`, `normal` oder `expensive`, wie bisher.

## Warum die Bedingungen zusammenkommen muessen

Weil jede fuer sich in die Irre fuehrt, und zwar in beide Richtungen.

**Die Statistik allein loest bei jedem Schlussverkauf aus.** Ryanair und Wizz
verkaufen regelmaessig zu einem Drittel des Ueblichen; das ist ihr
Geschaeftsmodell und kein Versehen. Eine reine Prozentregel - bei Hotels
dreissig Prozent des Medians - meldete auf Flugstrecken jeden Monat mehrmals,
und nach dem zweiten Fehlalarm schaltet ein Mensch die Meldung ab. Deshalb
liegt die Verhaeltnisschwelle hier bei fuenfundzwanzig statt dreissig Prozent
und verlangt zehn statt fuenf Vergleichspreise - und deshalb reicht sie
alleine nie.

**Die Schranke allein loest auf Kurzstrecken aus.** Neun Euro fuer 500
Kilometer sind bei einem Billigflieger der Normalfall. Ohne Historie laesst
sich das von einem Fehltarif nicht unterscheiden, und deshalb gilt die
Schranke ohne Historie erst ab `NO_HISTORY_MIN_KM`. Das ist eine bewusste
Luecke: **Fehltarife auf Kurzstrecken findet dieses Werkzeug erst, wenn die
Strecke eine Historie hat.** Sie zu schliessen hiesse, jeden Ryanair-Aktionstag
zu melden.

**Zusammen tragen sie.** Vier Euro nach Barcelona sind unter der Schranke
*und* unter einem Viertel des Medians; einen Markt, in dem beides zugleich
ehrlich zustande kommt, gibt es nicht.

## Die Schranken

Ein linearer Kilometerpreis taugt nicht: bei 500 Kilometern ist ein Cent je
Kilometer normal, bei 9000 Kilometern waere er absurd. Der Grund ist die
Kostenstruktur - kurz dominieren Gebuehren, lang dominiert Kerosin -, und die
Marktuntergrenze faellt je Kilometer mit der Entfernung. Deshalb Baender,
genau wie die Sternekategorien bei Hotels, und mit Werten, die **unter** dem
guenstigsten ehrlichen Markt liegen und nicht auf seinem Niveau:

| Entfernung      | Schranke | woran gemessen                              |
|-----------------|----------|---------------------------------------------|
| unter 1000 km   |   8 EUR  | Ryanair-Aktionen gehen bis 9,99 EUR          |
| 1000 - 2499 km  |  12 EUR  | Berlin-Barcelona im Angebot 13 bis 25 EUR    |
| 2500 - 4999 km  |  22 EUR  | Kanaren im Angebot ab rund 25 EUR            |
| 5000 - 7999 km  |  55 EUR  | Nordatlantik einfach nie unter 100 EUR       |
| ab 8000 km      |  75 EUR  | Fernost einfach ab rund 150 EUR              |

Die zweite Schranke - die, die ohne Historie traegt - sind sechzig Prozent
davon: 13, 33 und 45 Euro. Vierzig Euro nach Bangkok fallen darunter, hundert
Euro nicht.

Gerechnet wird **je Reisendem und je einfacher Strecke**. Eine Beobachtung
traegt den Preis der Buchung, und eine Buchung fuer vier ist kein Fehltarif,
nur weil sie viermal so teuer ist.

Ausserhalb des Euro gilt keine Schranke. Ein Kurs an dieser Stelle waere eine
zweite Stelle, an der umgerechnet wird, und die Schranken sind an
europaeischen Euro-Preisen geeicht - dieselbe Entscheidung wie bei
`PlausibilityLimits.currency` fuer Hotels.

## Was nie ein Fehltarif ist

Ein **indikativer** Preis. Kiwi traegt einen gemessenen Aufschlag von rund
dreizehn Prozent und preist zugleich ein anderes Produkt: jede Airline, bis
zu einem Umstieg. Ein Ein-Stopp-Preis unter jedem Direkttarif ist dort der
Normalfall, nicht der Fund. `is_indicative` steht an der Beobachtung, weil
spaeter niemand mehr weiss, wie eine Quelle damals eingestuft war - und aus
demselben Grund wird es hier gelesen und nicht aus dem Quellennamen erraten.

Ein **Kalenderpreis** ist dagegen zugelassen, obwohl er nur eine Schaetzung
ist. Anders ginge es nicht: die Beobachtungsliste holt ausschliesslich
Kalender, und ein Detektor, der nur gepruefte Preise annimmt, faende nie
etwas. Was er ist, steht in `population` und gehoert in jede Meldung.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from flightopt.domain import airports as airport_registry
from flightopt.hotels.signals import (
    BAND_REASON,
    TIER_ERROR,
    Baseline,
    band_status,
)

MAD_FACTOR = 6
MAD_MIN_N = 10
"""`preis <= median - 6 * mad` bei mindestens zehn Vergleichspreisen.

Sechs mittlere absolute Abweichungen sind bei normalverteilten Werten rund
vier Standardabweichungen (Sigma ist etwa 1,4826 mal MAD). Auch mit den
schweren Raendern echter Preisverteilungen bleibt das selten genug, um eine
Meldung zu rechtfertigen. Geteilt wird nirgends durch `mad`: bei `mad == 0`
waere ein Modified-Z-Score undefiniert, und `mad == 0` kommt auf duenn
besetzten Strecken vor.
"""

RATIO = 0.25
RATIO_MIN_N = 10
"""`preis <= 25 Prozent des Medians` bei mindestens zehn Vergleichspreisen.

Hotels stehen hier bei dreissig Prozent und fuenf Punkten. Flugpreise
schwanken staerker und werden regelmaessig als Aktion verramscht; beide Werte
sind deshalb strenger. Diese Bedingung traegt die Faelle, in denen `mad` klein
oder null ist und die MAD-Bedingung aussteigt.
"""

NO_HISTORY_MIN_KM = 2500.0
"""Ab hier darf die Schranke ohne jede Historie ein Urteil tragen.

Darunter ist der einstellige Preis ein Produkt und kein Versehen. Die Grenze
liegt am Uebergang von der europaeischen Kurzstrecke zur Mittelstrecke, also
dort, wo die Billigflieger aufhoeren, Sitze zu verschenken.
"""


@dataclass(frozen=True, slots=True)
class Plausibility:
    """Mindestpreise je Entfernungsband, in Minor Units und je Reisendem."""

    bands: tuple[tuple[float | None, int], ...]
    """`(obere Grenze in km oder None, Schranke)`, aufsteigend sortiert."""

    absurd_share: float = 0.6
    """Anteil der Schranke, unter dem es auch ohne Historie ein Fehltarif ist.

    Sechzig Prozent: 45 Euro nach Fernost, 33 Euro ueber den Nordatlantik. Der
    Wert ist so gewaehlt, dass der Fall, an dem sich die Anforderung
    aufhaengt - ein Langstreckenflug fuer vierzig Euro -, gerade darunter
    faellt, ein Angebot fuer hundert Euro dagegen deutlich darueber.
    """

    currency: str = "EUR"

    def floor_minor(self, km: float | None) -> int | None:
        """Die Schranke fuer diese Entfernung, oder None ohne Entfernung."""
        if km is None:
            return None
        for limit, floor in self.bands:
            if limit is None or km < limit:
                return floor
        return self.bands[-1][1]

    def absurd_minor(self, km: float | None) -> int | None:
        """Die tiefere Schranke, die ohne Historie traegt."""
        floor = self.floor_minor(km)
        return None if floor is None else int(round(floor * self.absurd_share))


DEFAULT_PLAUSIBILITY = Plausibility(
    bands=(
        (1000.0, 800),
        (2500.0, 1200),
        (5000.0, 2200),
        (8000.0, 5500),
        (None, 7500),
    ),
)


@dataclass(frozen=True, slots=True)
class FlightSignal:
    """Das Urteil zu einem Flugpreis, samt der Begruendung fuer die Meldung."""

    tier: str
    status: str
    reason: str
    price_minor: int
    distance_km: float | None = None
    floor_minor: int | None = None
    per_traveller_minor: int | None = None

    @property
    def is_error(self) -> bool:
        return self.tier == TIER_ERROR

    def as_dict(self) -> dict[str, Any]:
        """Nur die Felder, die zum Urteil gehoeren.

        Wer sie in die Antwort von `detect_price_signal` mischt, mischt sie in
        eine Antwort, die schon `status`, `median_minor` und `n` traegt.
        Deshalb hier nur der Zusatz und nicht das Ganze.
        """
        return {
            "distance_km": self.distance_km,
            "floor_minor": self.floor_minor,
            "per_traveller_minor": self.per_traveller_minor,
        }


def route_distance_km(entity_key: str) -> float | None:
    """Die Entfernung zu einem Beobachtungsschluessel `ORIGIN|DESTINATION`.

    None, wenn der Schluessel nicht diese Form hat oder einer der beiden
    Flughaefen keine Koordinaten traegt. None heisst dann durchgehend: keine
    Schranke, also auch kein Fehltarif. Lieber einen Fund verpassen als einen
    erfinden.
    """
    origin, separator, destination = str(entity_key).partition("|")
    if not separator or not origin or not destination:
        return None
    return airport_registry.distance_km(origin, destination)


def euro(minor: int) -> str:
    """Betrag in deutscher Schreibweise. Die Begruendung wird vorgelesen.

    Steht hier und nicht nur in `alerts`, weil die Begruendung dort in eine
    Nachricht wandert, in der jede andere Zahl ein Komma hat. Ein Punkt
    mittendrin liest sich wie ein Tippfehler.
    """
    return f"{minor / 100:.2f}".replace(".", ",") + " Euro"


def _statistical_reason(price_minor: int, baseline: Baseline | None) -> str | None:
    """Welche statistische Bedingung greift, wenn eine greift."""
    if baseline is None:
        return None
    if (
        baseline.mad_minor > 0
        and baseline.n >= MAD_MIN_N
        and price_minor <= baseline.median_minor - MAD_FACTOR * baseline.mad_minor
    ):
        return (
            f"{MAD_FACTOR}-fache Streuung unter dem Median von "
            f"{euro(baseline.median_minor)} (n={baseline.n})"
        )
    if baseline.n >= RATIO_MIN_N and price_minor <= round(baseline.median_minor * RATIO):
        return (
            f"unter {round(RATIO * 100)} Prozent des Medians von "
            f"{euro(baseline.median_minor)} (n={baseline.n})"
        )
    return None


def classify_flight(
    price_minor: int,
    baseline: Baseline | None = None,
    *,
    entity_key: str,
    currency: str = "EUR",
    party_size: int = 1,
    is_indicative: bool = False,
    plausibility: Plausibility = DEFAULT_PLAUSIBILITY,
) -> FlightSignal:
    """Preis, Vergleichsgruppe und Strecke zu einer Stufe verrechnen.

    `status` behaelt genau seine bisherige Bedeutung - drei Stufen aus dem
    Median-Band - damit die Suchergebnisliste weiterlesen kann, was sie immer
    gelesen hat. Neu ist `tier`, und `tier` weicht von `status` nur nach
    unten ab: `error` statt `cheap`.
    """
    status = band_status(price_minor, baseline)
    km = route_distance_km(entity_key)
    per_traveller = int(round(price_minor / max(1, int(party_size or 1))))

    floor = None
    if currency.upper() == plausibility.currency:
        floor = plausibility.floor_minor(km)
    absurd = plausibility.absurd_minor(km) if floor is not None else None

    signal = FlightSignal(
        tier=status,
        status=status,
        reason=BAND_REASON.get(status, ""),
        price_minor=price_minor,
        distance_km=km,
        floor_minor=floor,
        per_traveller_minor=per_traveller,
    )
    if is_indicative:
        # Vor allen Bedingungen und nicht als Nachbesserung: ein Richtwert soll
        # gar nicht erst in die Naehe eines Urteils kommen, das jemanden weckt.
        return signal

    below_floor = floor is not None and per_traveller < floor
    statistical = _statistical_reason(per_traveller, baseline)

    if below_floor and statistical is not None:
        return FlightSignal(
            tier=TIER_ERROR,
            status=status,
            reason=(
                f"{statistical} und unter der Schranke von "
                f"{floor // 100} Euro fuer {km:.0f} km"
            ),
            price_minor=price_minor,
            distance_km=km,
            floor_minor=floor,
            per_traveller_minor=per_traveller,
        )

    if (
        absurd is not None
        and km is not None
        and km >= NO_HISTORY_MIN_KM
        and per_traveller < absurd
    ):
        return FlightSignal(
            tier=TIER_ERROR,
            status=status,
            reason=(
                f"{euro(per_traveller)} fuer {km:.0f} km, also unter "
                f"der Schranke von {absurd / 100:.0f} Euro, die diese Entfernung "
                f"auch ohne Historie setzt"
            ),
            price_minor=price_minor,
            distance_km=km,
            floor_minor=floor,
            per_traveller_minor=per_traveller,
        )

    return signal


__all__ = [
    "DEFAULT_PLAUSIBILITY",
    "MAD_FACTOR",
    "MAD_MIN_N",
    "NO_HISTORY_MIN_KM",
    "RATIO",
    "RATIO_MIN_N",
    "FlightSignal",
    "Plausibility",
    "classify_flight",
    "route_distance_km",
]
