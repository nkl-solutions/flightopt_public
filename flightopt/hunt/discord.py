"""Der Meldekanal: ein HTTP-POST auf einen Discord-Webhook, mehr nicht.

Bewusst ein **Webhook** und kein Bot-Konto: eine URL, kein OAuth, keine
Berechtigungen, keine Sitzung, die ablaufen kann. Wer den Kanal anschalten
will, traegt eine Umgebungsvariable ein und startet den Dienst neu. Hier
entsteht kein Token, hier steht keiner, und danach gefragt wird auch nicht.

**Ohne URL laeuft alles trocken.** Der Fund wird erkannt, in `alert_event`
geschrieben und protokolliert, was gesendet **wuerde**. Das ist kein
Notbehelf, sondern der vorgesehene erste Betriebszustand: so laesst sich
beobachten, was das System melden will, bevor es reden darf. Ein Detektor,
den man erst nach dem Einrichten eines Kanals beurteilen kann, wird nie
beurteilt.

Keine neue Abhaengigkeit: `curl_cffi` ist fuer die Preisquellen ohnehin da.
Der `RateLimiter` und der `CircuitBreaker` aus `sources/base.py` bleiben
dagegen aussen vor - sie schuetzen den Zugang zu Preisquellen, und Discord
ist keine. Die Bremse hier ist die, die Discord selbst vorgibt: HTTP 429 mit
`retry_after`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

ENV_WEBHOOK = "FLIGHTOPT_DISCORD_WEBHOOK"
"""Name der Umgebungsvariablen. Steht in `deploy/portainer.env.example`."""

MAX_CONTENT = 2000
"""Discord nimmt hoechstens zweitausend Zeichen je Nachricht an."""

MAX_ATTEMPTS = 3
"""Wie oft ein Versand hoechstens angefasst wird, Erstversuch mitgezaehlt."""

MAX_RETRY_WAIT = 30.0
"""Laengste Wartezeit, die wir Discord zugestehen.

Bei einer globalen Sperre nennt Discord gelegentlich Werte im Bereich von
Minuten. So lange zu warten hiesse, den ganzen Durchgang anzuhalten - und der
Durchgang hat eine wichtigere Aufgabe als das Melden: das Aufzeichnen.
"""

TIMEOUT = 15


@dataclass(frozen=True, slots=True)
class Result:
    """Was aus einem Versandversuch geworden ist."""

    delivered: bool
    dry_run: bool = False
    status: int | None = None
    error: str | None = None
    attempts: int = 0


def webhook_url(env: Mapping[str, str] | None = None) -> str:
    import os

    source = os.environ if env is None else env
    return str(source.get(ENV_WEBHOOK, "")).strip()


def configured(env: Mapping[str, str] | None = None) -> bool:
    """Darf der Kanal reden? Beantwortet `/api/health/detail`."""
    return bool(webhook_url(env))


def _default_post(url: str, **kwargs: Any) -> Any:
    from curl_cffi import requests as creq

    return creq.post(url, **kwargs)


def _retry_after(response: Any) -> float:
    """Wie lange Discord warten laesst, in Sekunden.

    Der Wert steht im Rumpf (`retry_after`, seit der v10-API in Sekunden) und
    ausserdem im Kopf `Retry-After`. Gelesen werden beide, weil ein Fehler an
    dieser Stelle teuer ist: zu kurz gewartet holt den naechsten 429, zu lang
    gewartet haelt den Durchgang an.
    """
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - ein 429 ohne JSON ist keine Ausnahme wert
        body = {}
    raw = None
    if isinstance(body, dict):
        raw = body.get("retry_after")
    if raw is None:
        raw = (getattr(response, "headers", None) or {}).get("Retry-After")
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        seconds = 1.0
    return max(0.0, min(seconds, MAX_RETRY_WAIT))


def clip(content: str) -> str:
    """Auf die Laenge bringen, die Discord annimmt.

    Gekuerzt wird hinten und mit sichtbarem Vermerk: eine stumm abgeschnittene
    Meldung sieht aus wie eine vollstaendige.
    """
    text = str(content)
    if len(text) <= MAX_CONTENT:
        return text
    mark = " [gekuerzt]"
    return text[: MAX_CONTENT - len(mark)] + mark


async def send(content: str, *, url: str | None = None,
               env: Mapping[str, str] | None = None,
               post: Any = None, sleep: Any = None) -> Result:
    """Eine Meldung abschicken. Wirft nie.

    Ein Fehlschlag im Kanal darf den Durchgang nicht abbrechen: die
    Aufzeichnung ist unwiederbringlich, die Meldung nicht. Deshalb kommt jeder
    Ausgang als `Result` zurueck und keiner als Ausnahme.
    """
    text = clip(content)
    target = url if url is not None else webhook_url(env)
    if not target:
        # Trockenlauf. `info` und nicht `debug`: das ist der normale Zustand
        # eines frisch aufgesetzten Dienstes, und man soll ihn im Log sehen.
        logger.info("discord: kein Webhook gesetzt, wuerde senden: %s", text)
        return Result(delivered=False, dry_run=True)

    caller = post or _default_post
    napper = sleep or asyncio.sleep
    last_error: str | None = None
    status: int | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = await asyncio.to_thread(
                caller, target, json={"content": text}, timeout=TIMEOUT
            )
        except Exception as exc:  # noqa: BLE001 - der Kanal reisst nichts mit
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("discord: Versand gescheitert (%s)", last_error)
            return Result(delivered=False, error=last_error, attempts=attempt)

        status = int(getattr(response, "status_code", 0) or 0)
        if 200 <= status < 300:
            return Result(delivered=True, status=status, attempts=attempt)
        if status == 429:
            wait = _retry_after(response)
            logger.warning("discord: gebremst, warte %.1f s", wait)
            last_error = f"HTTP 429, retry_after={wait:.1f}"
            if attempt < MAX_ATTEMPTS:
                await napper(wait)
                continue
            break
        # Alles andere ist ein Einrichtungsfehler und keine Bremse: eine
        # geloeschte oder falsch abgetippte Webhook-URL antwortet mit 404 und
        # tut das beim zweiten Versuch genauso. Nachfassen hiesse, denselben
        # Fehler dreimal zu machen.
        body = str(getattr(response, "text", ""))[:200]
        last_error = f"HTTP {status} {body}".strip()
        logger.warning("discord: %s", last_error)
        break

    return Result(delivered=False, status=status, error=last_error,
                  attempts=min(attempt, MAX_ATTEMPTS))
