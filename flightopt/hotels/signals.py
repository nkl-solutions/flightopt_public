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

EVIDENCE_MAD = "streuung"
EVIDENCE_RATIO = "anteil"
EVIDENCE_FLOOR = "schranke"
EVIDENCE_BAND = "band"
EVIDENCE_ENCODING = "encoding"
EVIDENCE_NONE = "keine"
"""Welche Regel das Urteil getragen hat, als Kennung statt als Satz.

`reason` ist Text fuer Menschen und aendert seine Formulierung; `evidence` ist
die Regel und aendert sich nicht. Wer auswerten will, wie oft die
Plausibilitaetsschranke allein entschieden hat - und das ist die Frage, solange
die Historie duenn ist -, braucht das zweite und nicht das erste.
"""

WITHOUT_HISTORY = "ohne Vergleichspreise"
"""Der Zusatz, der an ein Urteil ohne jede Historie gehoert.

Die Oberflaeche zeigt das Wort "Preisfehler" und daneben `reason`; auf einem
Telefon ist der Zusatz im `title` gar nicht erreichbar und bleibt das Wort
allein stehen. Ein `error`, hinter dem keine einzige Vergleichsbeobachtung
steht, sieht dort genauso aus wie einer aus zwanzig - und das ist der
Unterschied zwischen einer Aussage und einer Vermutung.
"""

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
    evidence: str = EVIDENCE_NONE
    """Die Regel, die entschieden hat. Siehe `EVIDENCE_*`."""
    thin: bool = False
    """Wahr, wenn die Vergleichsgruppe unter `THIN_HISTORY_N` Punkten liegt."""

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
            "evidence": self.evidence,
            "thin": self.thin,
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


def _error_reason(
    price_minor: int, baseline: Baseline | None
) -> tuple[str, str] | None:
    """Welche der beiden statistischen Bedingungen greift, wenn eine greift.

    Zurueck kommt (Regel, Satz): die Regel fuer die Auswertung, der Satz fuer
    die Oberflaeche.
    """
    if baseline is None:
        return None
    if (
        baseline.mad_minor > 0
        and baseline.n >= MAD_MIN_N
        and price_minor <= baseline.median_minor - MAD_FACTOR * baseline.mad_minor
    ):
        return EVIDENCE_MAD, f"{MAD_FACTOR}-fache Streuung unter dem Median"
    if baseline.n >= RATIO_MIN_N and price_minor <= round(baseline.median_minor * RATIO):
        return EVIDENCE_RATIO, f"unter {round(RATIO * 100)} Prozent des Medians"
    return None


def qualify(reason: str, baseline: Baseline | None) -> str:
    """Den Satz um das ergaenzen, was hinter ihm steht - oder eben nicht.

    Nur dann, wenn das Urteil schwaecher ist, als es aussieht. Ein Zusatz an
    jeder Zeile liest sich nach kurzer Zeit niemand mehr durch, und dann traegt
    er auch dort nichts, wo er noetig waere.
    """
    if baseline is None or baseline.n <= 0:
        return f"{reason}, {WITHOUT_HISTORY}"
    if baseline.thin:
        return f"{reason}, nur {baseline.n} Vergleichspreise"
    return reason


POPULATION_ESTIMATE = "estimate"
POPULATION_VERIFIED = "verified"
"""Die beiden Grundgesamtheiten, benannt wie bei den Fluegen.

`estimate` ist ein Richtwert, `verified` der Preis, den der Haendler selbst
anzeigt. Bei Fluegen trennt `flight_baseline` die beiden im Schluessel; bei
Hotels liegt beides in derselben Verteilung, und deshalb gibt es hier den
Zusatz unten statt einer zweiten Zahl.
"""

MIXED_GROUP = "Vergleichsgruppe enthaelt auch Richtwerte"

REPORTED_TIERS = (TIER_ERROR, TIER_CHEAP)
"""Die Stufen, auf die jemand hin handelt. Nur dort steht der Zusatz.

An jeder Zeile wuerde er zur Tapete, und Tapete liest niemand - auch nicht
dort, wo sie noetig waere."""


def with_population(
    signal: Mapping[str, Any], *, indicative: bool, split: bool = False
) -> dict[str, Any]:
    """Dem Urteil anhaengen, gegen welche Grundgesamtheit es gerechnet wurde.

    `split` sagt, ob die Baseline die beiden ueberhaupt trennt. Solange sie es
    nicht tut - und die Hoteltabelle tut es nicht -, ist ein Urteil ueber einen
    Haendlerpreis gegen eine gemischte Gruppe gerechnet, und das gehoert an die
    Zeile. "Dieser Preis weicht von den Richtwerten anderer ab" ist eine
    schwaechere Aussage als "er weicht von Haendlerpreisen ab", und nur die
    zweite waere ein Preisfehler im engeren Sinn.
    """
    out = dict(signal)
    out["population"] = POPULATION_ESTIMATE if indicative else POPULATION_VERIFIED
    out["population_split"] = bool(split)
    if indicative or split:
        return out
    if out.get("basis") in (BASIS_OWN, BASIS_PEER) and out.get("tier") in REPORTED_TIERS:
        reason = str(out.get("reason") or "")
        out["reason"] = f"{reason}, {MIXED_GROUP}" if reason else MIXED_GROUP
    return out


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
    reason: str | None = None
    evidence = EVIDENCE_NONE
    found = _error_reason(price_minor, baseline)
    if found is not None:
        evidence, reason = found
        tier = TIER_ERROR

    if tier is None and not suspect_category and currency.upper() == limits.currency:
        per_night = price_minor / max(1, nights)
        floor = limits.floor_for(stars)
        if per_night < floor:
            tier = TIER_ERROR
            evidence = EVIDENCE_FLOOR
            reason = f"unter der Schranke von {floor / 100:.0f} Euro je Nacht"

    # Steht ein `error` da, gehoert dazu, worauf er sich stuetzt. Genau hier
    # entsteht sonst die Sicherheit, die es nicht gibt: die Schranke greift am
    # ersten Tag und ohne eine einzige Vergleichsbeobachtung, und die Zeile
    # sieht danach aus wie eine aus zwanzig.
    if tier == TIER_ERROR and reason:
        reason = qualify(reason, baseline)

    # Das Encoding-Veto steht am Ende, weil es jedes Urteil ueberstimmt: ein
    # Faktor 100 im Datensatz ist kein Angebot. Es greift nur bei den Stufen,
    # die auch gemeldet werden. Auf `cheap` darf es nicht wirken, denn 60 Euro
    # bei Median 90 sind exakt 1/1,50 und damit zufaellig der Kanada-Kurs.
    if tier == TIER_ERROR or status == TIER_EXPENSIVE:
        check = encoding_check(
            price_minor, baseline.median_minor if baseline else None, rates=rates
        )
        if check.suspect:
            tier, reason, evidence = ENCODING_SUSPECT, check.reason, EVIDENCE_ENCODING

    if tier is None:
        tier = status
        reason = BAND_REASON[status]
        evidence = EVIDENCE_BAND if baseline is not None else EVIDENCE_NONE

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
        evidence=evidence,
        thin=bool(baseline.thin) if baseline else False,
    )
