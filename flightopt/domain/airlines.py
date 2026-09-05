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
                "Monatskalender und Tagessuche, beides bestätigt."),

        # Not an airline. Kept in the registry so results that rely on it can
        # say so, instead of inventing a carrier that does not fly the route.
        Airline("KIWI", "Kiwi (Vergleich)", "#00a991", "live", "kiwi",
                "Vergleichsportal, deckt jede Strecke ab. Preise liegen etwa "
                "13 Prozent über dem echten Tarif und gelten als Richtwert.",
                kind="comparison"),

        Airline("W6", "Wizz Air", "#c6007e", "live", "wizz",
                "Tagespreise aus dem Flugplan. Preise folgen der Währung des "
                "Abflugmarkts, abweichende Währungen werden übersprungen."),
        Airline("A3", "Aegean", "#00594f", "live", "aegean",
                "Monatskalender mit günstigsten Preisen. Liefert keine "
                "Flugzeiten, daher ohne Live-Nachprüfung."),
        Airline("EW", "Eurowings", "#a4147a", "live", "eurowings",
                "Bis zu 15 Monate Tagespreise in einem Aufruf. Braucht einen "
                "Seitenaufruf vorab für das Cloudflare-Cookie."),
        Airline("DE", "Condor", "#ffad00", "live", "condor",
                "Monatskalender ohne Anmeldung. Deckt Griechenland und die "
                "Türkei ab Deutschland ab. Keine Flugzeiten."),
        Airline("DI", "Marabu", "#e8112d", "live", "condor",
                "Läuft über dieselbe Buchungsmaschine wie Condor."),
        Airline("BA", "British Airways", "#2e5c99", "live", "britishairways",
                "Offene Suchmaschine mit Tagespreisen. Nur Strecken, die "
                "British Airways ab dem Abflugmarkt selbst verkauft."),
        Airline("FI", "Icelandair", "#003366", "live", "icelandair",
                "Tagespreise mit rund einem Jahr Vorlauf in einem Aufruf."),
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
    return airline.color if airline else "#585f73"


def as_dicts() -> list[dict]:
    return [
        {
            "code": a.code,
            "name": a.name,
            "color": a.color,
            "status": a.status,
            "note": a.note,
            "kind": a.kind,
        }
        for a in AIRLINES.values()
    ]
