"""Gemeinsame Vorbedingungen fuer die gesamte Testsuite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_basic_auth(monkeypatch):
    """Auf Maschinen mit gesetzten Zugangsdaten liefen sonst alle Routen in 401.

    Tests, die Basic-Auth ausdruecklich brauchen, setzen die Variablen im
    Testkoerper selbst und ueberschreiben damit diese Vorbedingung.
    """
    monkeypatch.delenv("FLIGHTOPT_BASIC_USER", raising=False)
    monkeypatch.delenv("FLIGHTOPT_BASIC_PASSWORD", raising=False)
