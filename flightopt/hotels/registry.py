"""Der eine Quellen-Katalog der Hotelsuche.

Eine Liste fuer alle Aufrufer, aus demselben Grund wie bei den Fluegen: zwei
Kopien laufen auseinander, und eine Quelle, die es nur in einer davon gibt, ist
ein Fehler, den niemand sieht.

Trivago ist immer dabei. Booking kommt nur dazu, wenn beides stimmt: der
Schalter `FLIGHTOPT_HOTELS_BOOKING=1` ist gesetzt **und** Playwright ist
installiert. Fehlt eines, bleibt die Quelle draussen und der Grund steht im
Bericht, statt dass ein Lauf mitten in der Tagesschleife an einem fehlenden
Browser scheitert.
"""

from __future__ import annotations

import os
from typing import Any, Mapping

from flightopt.hotels.sources.base import HotelSource, env_flag
from flightopt.hotels.sources.booking import BookingSource
from flightopt.hotels.sources.trivago_mcp import TrivagoMcpSource

BOOKING_FLAG = "FLIGHTOPT_HOTELS_BOOKING"


def booking_enabled(env: Mapping[str, str] | None = None) -> bool:
    return env_flag(os.environ if env is None else env, BOOKING_FLAG)


def build_hotel_sources(env: Mapping[str, str] | None = None) -> list[HotelSource]:
    env = os.environ if env is None else env
    sources: list[HotelSource] = [TrivagoMcpSource()]
    if booking_enabled(env) and BookingSource.availability(env)[0]:
        sources.append(BookingSource())
    return sources


def source_report(env: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Was im Katalog steht und was nicht, jeweils mit Grund.

    Die Oberflaeche soll sagen koennen, warum nur eine Quelle geantwortet hat.
    "Keine Ergebnisse" und "die zweite Quelle war gar nicht dabei" sind zwei
    verschiedene Aussagen.
    """
    env = os.environ if env is None else env
    usable, reason = BookingSource.availability(env)
    if not booking_enabled(env):
        booking_reason = f"{BOOKING_FLAG}=1 schaltet sie ein"
    elif not usable:
        booking_reason = reason
    else:
        booking_reason = ""
    return [
        {"name": TrivagoMcpSource.name, "active": True, "reason": ""},
        {
            "name": BookingSource.name,
            "active": not booking_reason,
            "reason": booking_reason,
        },
    ]
