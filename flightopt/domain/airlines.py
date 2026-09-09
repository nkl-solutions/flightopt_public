"""Airline registry: who we can price today, and who is worth adding next.

The `status` field is the honest part. A search that only queries Ryanair will
miss most of a route's real options, so the UI shows which carriers are covered
and which are still open, rather than implying the result is the whole market.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Status = Literal["live", "planned", "blocked"]
Kind = Literal["airline", "comparison"]


@dataclass(frozen=True, slots=True)
class Airline:
    code: str
    name: str
    color: str
    """Brand colour, used for the marker in the results list."""
    status: Status
    source: str | None = None
    note: str = ""
    kind: Kind = "airline"
    """A comparison site is not an airline and must not be shown as one."""
    checked_bag_minor: int = 3500
    """Transparent estimate for one checked bag on one leg, in cents."""

    @property
    def initials(self) -> str:
        return self.code


AIRLINES: dict[str, Airline] = {
    a.code: a
    for a in [
        # --- pricing works today ---
        # Note: the note text is shown in the UI, so it says what each source
        # can and cannot do rather than just that it works.
        Airline("FR", "Ryanair", "#073590", "live", "ryanair",
                "Monatskalender und Tagessuche, beides bestätigt.",
                checked_bag_minor=4000),

        # Not an airline. Kept in the registry so results that rely on it can
        # say so, instead of inventing a carrier that does not fly the route.
        Airline("KIWI", "Kiwi (Vergleich)", "#00a991", "live", "kiwi",
                "Vergleichsportal, deckt jede Strecke ab. Preise liegen etwa "
                "13 Prozent über dem echten Tarif und gelten als Richtwert.",
                kind="comparison", checked_bag_minor=4500),

        Airline("W6", "Wizz Air", "#c6007e", "live", "wizz",
                "Tagespreise aus dem Flugplan. Preise folgen der Währung des "
                "Abflugmarkts und werden zum EZB-Kurs umgerechnet.",
                checked_bag_minor=4500),
        Airline("A3", "Aegean", "#00594f", "live", "aegean",
                "Monatskalender mit günstigsten Preisen. Liefert keine "
                "Flugzeiten, daher ohne Live-Nachprüfung.",
                checked_bag_minor=3000),
        Airline("EW", "Eurowings", "#a4147a", "live", "eurowings",
                "Bis zu 15 Monate Tagespreise in einem Aufruf. Braucht einen "
                "Seitenaufruf vorab für das Cloudflare-Cookie.",
                checked_bag_minor=3500),
        # Die Notiz sagte lange "Griechenland und die Türkei ab Deutschland".
        # Gemessen liefert dieselbe Quelle FRA-JFK mit Tagespreisen zwischen
        # 529,99 und 629,99 EUR, und damit ist sie die einzige eigene
        # Airline-Quelle für Transatlantik ab Deutschland. Die Notiz steht in
        # der Oberfläche und steuert, wo jemand eine Quelle vermutet; wer
        # Nordamerika sucht, hätte hier nie nachgesehen.
        Airline("DE", "Condor", "#ffad00", "live", "condor",
                "Monatskalender ohne Anmeldung. Mittelmeer ab Deutschland und "
                "als einzige eigene Airline-Quelle auch Nordamerika, etwa "
                "Frankfurt nach New York. Keine Flugzeiten.",
                checked_bag_minor=3500),
        Airline("DI", "Marabu", "#e8112d", "live", "condor",
                "Läuft über dieselbe Buchungsmaschine wie Condor.",
                checked_bag_minor=3500),
        Airline("BA", "British Airways", "#2e5c99", "live", "britishairways",
                "Offene Suchmaschine mit Tagespreisen. Nur Strecken, die "
                "British Airways ab dem Abflugmarkt selbst verkauft.",
                checked_bag_minor=4500),
        Airline("FI", "Icelandair", "#003366", "live", "icelandair",
                "Tagespreise mit rund einem Jahr Vorlauf in einem Aufruf.",
                checked_bag_minor=4000),
        Airline("BT", "airBaltic", "#8ac53f", "live", "airbaltic",
                "Tagespreise für rund ein Jahr in einem einzigen Aufruf. "
                "Antwortet nur in EUR, ohne Währungsfeld.",
                checked_bag_minor=4000),
        Airline("B6", "JetBlue", "#003876", "live", "jetblue",
                "Monatskalender mit günstigstem Tagespreis, Antwort in USD und "
                "zum EZB-Kurs umgerechnet. Keine Flugzeiten.",
                checked_bag_minor=4000),
        Airline("TK", "Turkish Airlines", "#c70a0c", "planned", "turkish",
                "Offizielles Entwicklerportal, Registrierung nötig."),

        # --- reachable in principle, but not worth the fight yet ---
        Airline("XQ", "SunExpress", "#00a1e0", "blocked", None,
                "Eigene Seite antwortet mit 403. Preise kommen ersatzweise "
                "über die Vergleichsquelle."),
        Airline("PC", "Pegasus", "#ffc800", "blocked", None,
                "Preisendpunkt antwortet nur noch mit 403."),
        Airline("U2", "easyJet", "#ff6600", "blocked", None,
                "Akamai lässt reine HTTP-Abfragen nicht durch."),
        Airline("LH", "Lufthansa", "#05164d", "blocked", None,
                "Offene Schnittstelle führt keine Preise."),
        Airline("VF", "AJet", "#e4002b", "blocked", None, "Bot-Schutz."),
        Airline("HV", "Transavia", "#00a94f", "blocked", None,
                "Probe am 07.09.2026: kein offener Tagespreis-Endpunkt "
                "gefunden. Beleg in docs/AIRLINE_PLAN.md.",
                checked_bag_minor=4000),
        Airline("V7", "Volotea", "#a51890", "blocked", None,
                "Probe am 07.09.2026: kein offener Tagespreis-Endpunkt "
                "gefunden. Beleg in docs/AIRLINE_PLAN.md.",
                checked_bag_minor=4500),
        Airline("VY", "Vueling", "#ffcc00", "blocked", None,
                "Probe am 07.09.2026: kein offener Tagespreis-Endpunkt "
                "gefunden. Beleg in docs/AIRLINE_PLAN.md.",
                checked_bag_minor=4000),
        Airline("X3", "TUIfly", "#009cdc", "blocked", None,
                "Probe am 07.09.2026: kein offener Tagespreis-Endpunkt "
                "gefunden. Beleg in docs/AIRLINE_PLAN.md.",
                checked_bag_minor=3500),
        Airline("XC", "Corendon", "#004b93", "blocked", None,
                "Probe am 07.09.2026: kein offener Tagespreis-Endpunkt "
                "gefunden. Beleg in docs/AIRLINE_PLAN.md.",
                checked_bag_minor=3500),
        Airline("DY", "Norwegian", "#d81939", "blocked", None,
                "Kalender-Endpunkt hinter Cloudflare-Challenge, auch im Browser."),
        Airline("LO", "LOT", "#0f2c7d", "blocked", None, "Akamai, 403 auf allen Pfaden."),
        Airline("AY", "Finnair", "#0b1560", "blocked", None, "Akamai, API-Gateway mit Key."),
        Airline("EI", "Aer Lingus", "#00a65a", "blocked", None,
                "Imperva mit CAPTCHA, auch im Browser."),
        Airline("TP", "TAP", "#00a19a", "blocked", None,
                "Buchungs-Session 403, Kalender nur mit Session."),
        Airline("ZG", "ZIPAIR", "#6cbe45", "blocked", None, "Cloudflare-Sperre."),
        Airline("N0", "Norse Atlantic", "#d0021b", "blocked", None,
                "Cloudflare Turnstile vor dem Lowfare-Endpunkt."),
        Airline("4Y", "Discover", "#0a2a5e", "blocked", None,
                "Nur ueber die partner-gebundene Lufthansa-API."),
    ]
}

LIVE = [a for a in AIRLINES.values() if a.status == "live"]
PLANNED = [a for a in AIRLINES.values() if a.status == "planned"]


def get(code: str) -> Airline | None:
    return AIRLINES.get(code.upper())


def name_of(code: str) -> str:
    airline = get(code)
    return airline.name if airline else code


def color_of(code: str) -> str:
    airline = get(code)
    # Dieselbe neutrale Flaeche wie `tailMark` im Frontend, damit ein
    # unbekannter Code auf beiden Wegen gleich aussieht.
    return airline.color if airline else "#69625d"


def checked_bag_minor(code: str) -> int:
    airline = get(code)
    return airline.checked_bag_minor if airline else 3500


def as_dicts() -> list[dict]:
    return [
        {
            "code": a.code,
            "name": a.name,
            "color": a.color,
            "status": a.status,
            "note": a.note,
            "kind": a.kind,
            "checked_bag_price": a.checked_bag_minor / 100,
        }
        for a in AIRLINES.values()
    ]
