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


@pytest.fixture(autouse=True)
def _no_alert_channel(monkeypatch):
    """Kein Test schickt je etwas nach Discord.

    Das ist keine Kosmetik: `run_hunt` ohne eigenes `env` liest die
    Prozessumgebung, und auf der Maschine des Betreibers steht dort die echte
    Webhook-URL. Ohne diese Vorbedingung wuerde ein Testlauf in einen Kanal
    schreiben, in dem Menschen mitlesen - und zwar mit erfundenen Preisen.
    """
    monkeypatch.delenv("FLIGHTOPT_DISCORD_WEBHOOK", raising=False)
