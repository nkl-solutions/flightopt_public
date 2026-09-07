"""Der eine Quellen-Katalog.

Vorher stand dieselbe Liste im Job-Runner und ein zweites Mal, auf Ryanair
verkuerzt, im Routen-Endpunkt der API. Jede neue Airline waere an mehreren
Stellen einzutragen gewesen, und genau eine davon vergisst man.

Kiwi ist kein Anbieter, sondern ein Vergleichsportal: ein Airline-Filter wird
an Kiwi durchgereicht, statt Kiwi aus dem Katalog zu nehmen. Sammler ohne
eigenen Carrier, die keinen Filter kennen (SerpApi), fallen bei gesetztem
Filter dagegen raus - sonst schmuggeln sie andere Airlines zurueck herein.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from flightopt.sources.aegean import AegeanSource
from flightopt.sources.airbaltic import AirBalticSource
from flightopt.sources.base import HttpSource
from flightopt.sources.britishairways import BritishAirwaysSource
from flightopt.sources.condor import CondorSource
from flightopt.sources.eurowings import EurowingsSource
from flightopt.sources.icelandair import IcelandairSource
from flightopt.sources.jetblue import JetBlueSource
from flightopt.sources.kiwi import KiwiSource
from flightopt.sources.ryanair import RyanairSource
from flightopt.sources.serpapi_google import from_env as serpapi_from_env
from flightopt.sources.wizz import WizzSource


def _airline_sources() -> list[HttpSource]:
    """Adapter, die genau eine Marke (oder eine Markenfamilie) bepreisen."""
    return [
        RyanairSource(),
        WizzSource(),
        AegeanSource(),
        CondorSource(),
        EurowingsSource(),
        BritishAirwaysSource(),
        IcelandairSource(),
        AirBalticSource(),
        JetBlueSource(),
    ]


def build_sources(
    carriers: set[str] | None = None,
    *,
    conn: Any = None,
    env: Mapping[str, str] | None = None,
) -> list:
    """Jede Quelle, die ein Suchlauf nutzen darf, gefiltert nach Airline.

    Eine Liste fuer alle Aufrufer: zwei Kopien eines Katalogs laufen
    auseinander, und eine Quelle, die es nur in einer davon gibt, ist ein
    Fehler, den niemand sieht.

    `conn` ist die Datenbankverbindung des Jobs. SerpApi kommt nur mit
    angehaengtem Monatsbudget in den Katalog, und das Budget liegt in SQLite.
    Ohne Verbindung (etwa im Routen-Endpunkt, der nur Streckennetze liest)
    bleibt die bezahlte Quelle deshalb draussen.

    `env` wird uebergeben statt gelesen, damit Tests den Schluessel setzen
    koennen, ohne die Prozessumgebung anzufassen.
    """
    env = os.environ if env is None else env
    wanted = {c.strip().upper() for c in (carriers or set()) if c and c.strip()}

    catalogue: list = [*_airline_sources(), KiwiSource()]
    serpapi = serpapi_from_env(env, conn=conn) if conn is not None else None
    if serpapi is not None:
        catalogue.append(serpapi)

    if not wanted:
        return catalogue

    sources: list = []
    for source in catalogue:
        if isinstance(source, KiwiSource):
            # Kiwi has no carrier of its own, but it takes a carrier filter.
            sources.append(KiwiSource(carriers=sorted(wanted)))
        elif not source.carriers:
            # A collector we cannot narrow would smuggle other airlines back in.
            continue
        elif wanted & set(source.carriers):
            sources.append(source)
    return sources
