"""Die vierte Stufe: ein Preisfehler ist etwas anderes als ein guenstiges Zimmer.

Der Detektor der Flugsuche kennt drei Stufen und ein Band von
`max(1500, mad * 3)`. Gemessen an echten Hoteldaten reicht das nicht: bei
Median 89,65 Euro landen 60 Euro und 9 Euro beide auf `cheap`. Das eine ist
ein Angebot, das andere ein Fehler.

`error` greift, wenn mindestens eine von drei Bedingungen zutrifft:

1. `preis <= median - 6 * mad` bei `n >= 10` und `mad > 0`.
2. `preis <= 0,3 * median` bei `n >= 5`. Traegt auch, wenn `mad` klein oder
   null ist, und deckt damit genau die Faelle ab, in denen Bedingung 1
   aussteigt.
3. Preis pro Nacht unter der Plausibilitaetsschranke der Sternekategorie.
   Braucht keine Historie und ist am ersten Tag die einzige Bedingung, die
   ueberhaupt etwas findet.

Nirgends wird durch `mad` geteilt. Ein Modified-Z-Score waere bei `mad == 0`
undefiniert, und `mad == 0` ist bei Hotels der Normalfall und nicht die
Ausnahme: liegt ueber die Haelfte der Beobachtungen auf dem Median, ist die
mittlere absolute Abweichung exakt null. Deshalb steht `mad` immer als
Summand und nie als Nenner.

Rangfolge: `error` schlaegt `cheap` schlaegt `normal` schlaegt `expensive`.
`unknown` bleibt, wenn weder Baseline noch Schranke etwas sagen.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from flightopt.domain.fx import Rates
from flightopt.hotels.normalize import ENCODING_SUSPECT, encoding_check, is_category_suspect

TIER_ERROR = "error"
TIER_CHEAP = "cheap"
TIER_NORMAL = "normal"
TIER_EXPENSIVE = "expensive"
TIER_UNKNOWN = "unknown"

TIERS: tuple[str, ...] = (
    TIER_ERROR, TIER_CHEAP, TIER_NORMAL, TIER_EXPENSIVE, ENCODING_SUSPECT, TIER_UNKNOWN
)

TIER_RANK: Mapping[str, int] = MappingProxyType({
    TIER_ERROR: 0,
    TIER_CHEAP: 1,
    TIER_NORMAL: 2,
    TIER_EXPENSIVE: 3,
    ENCODING_SUSPECT: 4,
    TIER_UNKNOWN: 5,
})
"""Sortierreihenfolge der Ergebnistabelle. Aussortiertes steht unten."""

BAND_FLOOR_MINOR = 1500
"""Das Band des Drei-Stufen-Detektors, unveraendert uebernommen."""

MAD_FACTOR = 6
MAD_MIN_N = 10
RATIO = 0.30
RATIO_MIN_N = 5
THIN_HISTORY_N = 10
"""Unter zehn eigenen Beobachtungen traegt die Eigenhistorie nicht."""

BASIS_OWN = "own"
BASIS_PEER = "peer"
BASIS_NONE = "none"

BAND_REASON: Mapping[str, str] = MappingProxyType({
    TIER_CHEAP: "unter dem Median-Band",
    TIER_NORMAL: "im Median-Band",
    TIER_EXPENSIVE: "ueber dem Median-Band",
    TIER_UNKNOWN: "keine Baseline",
})


@dataclass(frozen=True, slots=True)
class PlausibilityLimits:
    """Mindestpreis je Nacht und Sternekategorie, in Minor Units.

    Die Werte liegen bewusst *unter* dem guenstigsten ehrlichen Markt und
    nicht auf dessen Niveau. In Bulgarien, Albanien oder in der tuerkischen
    Provinz kostet ein Doppelzimmer der mittleren Kategorien in der Nebensaison
    real rund 25 bis 35 Euro; BookingX hat genau dort seine feste Schwelle
    angesetzt und deshalb jeden Tag Fehlalarm produziert. Fuenfzehn Euro fuer
    ein Hotelzimmer sind dagegen in keinem europaeischen Markt ein Angebot,
    sondern ein Datenfehler, und siebzig Euro fuer fuenf Sterne ebenso.

    Ohne Sterneangabe gilt die niedrigste Schranke: lieber einen Fehler
    verpassen als ein unbewertetes Objekt falsch melden. Fuer Schlafsaal,
    Camping und Boot gilt sie gar nicht, dafuer gibt es den Kategoriefilter.

    Gerechnet wird pro Nacht und pro Buchung, nicht pro Zimmer. Bei mehreren
    Zimmern liegt die Schranke damit noch weiter unten, also noch
    konservativer.
    """

    per_stars: Mapping[int, int]
    default_minor: int
    currency: str = "EUR"

    def floor_for(self, stars: int | None) -> int:
        """Die Schranke fuer diese Sternekategorie."""
        if stars is None:
            return self.default_minor
        try:
            return self.per_stars.get(int(stars), self.default_minor)
        except (TypeError, ValueError):
            return self.default_minor


DEFAULT_LIMITS = PlausibilityLimits(
    per_stars=MappingProxyType({1: 1500, 2: 2000, 3: 3000, 4: 4500, 5: 7000}),
    default_minor=1500,
)


@dataclass(frozen=True, slots=True)
class Baseline:
    """Median und Streuung einer Vergleichsgruppe."""

    median_minor: int
    mad_minor: int
    n: int
    basis: str = BASIS_OWN

    @property
    def thin(self) -> bool:
        return self.n < THIN_HISTORY_N


@dataclass(frozen=True, slots=True)
class Signal:
    """Das Urteil zu einem Preis."""

    tier: str
    status: str
    reason: str
    basis: str
    n: int
    price_minor: int
    median_minor: int | None = None
    mad_minor: int | None = None
    category_suspect: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "tier": self.tier,
            "reason": self.reason,
            "basis": self.basis,
            "n": self.n,
            "price_minor": self.price_minor,
            "median_minor": self.median_minor,
            "mad_minor": self.mad_minor,
            "category_suspect": self.category_suspect,
        }


def band_status(price_minor: int, baseline: Baseline | None) -> str:
    """Die drei alten Stufen, Wort fuer Wort wie bisher.

    `status` behaelt seine heutige Bedeutung, damit die Flugsuche und die
    bestehende Oberflaeche weiterlesen koennen, was sie immer gelesen haben.
    """
    if baseline is None:
        return TIER_UNKNOWN
    band = max(BAND_FLOOR_MINOR, baseline.mad_minor * 3)
    if price_minor <= baseline.median_minor - band:
        return TIER_CHEAP
    if price_minor >= baseline.median_minor + band:
        return TIER_EXPENSIVE
    return TIER_NORMAL


def _error_reason(price_minor: int, baseline: Baseline | None) -> str | None:
    """Welche der beiden statistischen Bedingungen greift, wenn eine greift."""
    if baseline is None:
        return None
    if (
        baseline.mad_minor > 0
        and baseline.n >= MAD_MIN_N
        and price_minor <= baseline.median_minor - MAD_FACTOR * baseline.mad_minor
    ):
        return f"{MAD_FACTOR}-fache Streuung unter dem Median"
    if baseline.n >= RATIO_MIN_N and price_minor <= round(baseline.median_minor * RATIO):
        return f"unter {round(RATIO * 100)} Prozent des Medians"
    return None


def classify(
    price_minor: int,
    baseline: Baseline | None = None,
    *,
    nights: int = 1,
    stars: int | None = None,
    name: str | None = None,
    currency: str = "EUR",
    rates: Rates | None = None,
    limits: PlausibilityLimits = DEFAULT_LIMITS,
) -> Signal:
    """Preis, Vergleichsgruppe und Stammdaten zu einer Stufe verrechnen."""
    status = band_status(price_minor, baseline)
    suspect_category = is_category_suspect(name)
    tier: str | None = None
    reason = _error_reason(price_minor, baseline)
    if reason is not None:
        tier = TIER_ERROR

    if tier is None and not suspect_category and currency.upper() == limits.currency:
        per_night = price_minor / max(1, nights)
        floor = limits.floor_for(stars)
        if per_night < floor:
            tier = TIER_ERROR
            reason = f"unter der Schranke von {floor / 100:.0f} Euro je Nacht"

    # Das Encoding-Veto steht am Ende, weil es jedes Urteil ueberstimmt: ein
    # Faktor 100 im Datensatz ist kein Angebot. Es greift nur bei den Stufen,
    # die auch gemeldet werden. Auf `cheap` darf es nicht wirken, denn 60 Euro
    # bei Median 90 sind exakt 1/1,50 und damit zufaellig der Kanada-Kurs.
    if tier == TIER_ERROR or status == TIER_EXPENSIVE:
        check = encoding_check(
            price_minor, baseline.median_minor if baseline else None, rates=rates
        )
        if check.suspect:
            tier, reason = ENCODING_SUSPECT, check.reason

    if tier is None:
        tier = status
        reason = BAND_REASON[status]

    return Signal(
        tier=tier,
        status=status,
        reason=reason or "",
        basis=baseline.basis if baseline else BASIS_NONE,
        n=baseline.n if baseline else 0,
        price_minor=price_minor,
        median_minor=baseline.median_minor if baseline else None,
        mad_minor=baseline.mad_minor if baseline else None,
        category_suspect=suspect_category,
    )
