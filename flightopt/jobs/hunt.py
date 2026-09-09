"""Der heisse Durchgang: dieselbe Aufzeichnung, nur oft genug fuer Fehltarife.

Ein Fehltarif lebt Minuten bis Stunden. `run_watchlist` fragt einmal am Tag
und findet ihn deshalb nie. Hier steht derselbe Abruf mit drei Unterschieden,
und alle drei sind Vorsichtsmassnahmen und keine Erweiterungen:

1. Vor jeder Strecke wird **verbucht**, was sie kostet, und zwar bevor der
   Abruf laeuft. Reicht das Stundenbudget einer einzigen Quelle nicht, wird
   die Strecke **verschoben** statt abgerufen. Verschieben verliert einen
   Durchgang, das Reissen der Grenze verliert die Quelle.
2. Der Cache wird mit einer **Frischegrenze** gelesen. Ohne sie fragte der
   Durchgang gar nicht die Quelle, sondern seine eigene Antwort von vorhin -
   die TTL eines Kalenders ist vierundzwanzig Stunden, der Takt zwanzig
   Minuten.
3. Geht bei einer Quelle die **Sicherung** zu, faellt die ganze Jagd auf den
   Tagestakt zurueck, bis die Abkuehlung um ist. Nicht nur diese Quelle: ein
   Block ist der Hinweis, dass unser Fussabdruck auffaellt, und die anderen
   weiter dreimal die Stunde zu fragen holt den naechsten.

Was der Durchgang **nicht** tut: live pruefen, kombinieren, Ergebnisse
rechnen. Er holt Kalender und schreibt sie weg, genau wie der Tageslauf. Die
Jagd ist ein Takt und kein zweiter Suchweg.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

from flightopt.domain.fx import Rates
from flightopt.domain.models import LegSpec
from flightopt.hunt import budget, cadence
from flightopt.hunt.finder import (
    MAX_FINDS_PER_ROUTE,
    find_error_fares,
    last_observation_id,
    report_finds,
)
from flightopt.jobs.runner import preload_routes
from flightopt.jobs.watchlist import collect_route
from flightopt.sources.registry import build_sources
from flightopt.storage import fx_store, watchlist
from flightopt.storage.baseline import refresh_baselines

logger = logging.getLogger(__name__)

__all__ = ["MAX_FINDS_PER_ROUTE", "MAX_ROUTES_PER_RUN", "run_hunt"]

MAX_ROUTES_PER_RUN = cadence.MAX_HOT_ROUTES
"""Wie viele Strecken ein einzelner Durchgang hoechstens anfasst.

Das Stundenbudget allein reicht dafuer nicht. Es sichert die **Summe** einer
Stunde, nicht ihre Verteilung: bei zwanzig heissen Strecken haette ein
einziger Aufruf fuenfzehn Durchgaenge hintereinander gefahren, also das ganze
Budget in wenigen Minuten verbraucht, und danach waere eine Stunde Stille
gewesen. Genau dieses Muster - Salve, Pause, Salve - faellt einer Quelle auf.

Fuenf ist dieselbe Zahl wie `MAX_HOT_ROUTES` und aus demselben Grund: so viele
Strecken passen bei drei Durchgaengen je Stunde unter die Grenze. Der
Tagesplaner ruft alle zehn Minuten, also sechs Mal je Stunde; fuenf Strecken je
Aufruf sind damit hoechstens fuenfzehn Abrufe je Quelle und Aufruf und bleiben
unter der Grenze, ohne sie in einer Salve auszuschoepfen.
"""


def empty_report(**extra: Any) -> dict[str, Any]:
    """Dieselbe Form fuer jeden Ausgang, damit niemand zwei Formen liest."""
    report: dict[str, Any] = {
        "routes": 0,
        "observations": 0,
        "calls": 0,
        "finds": 0,
        "alerts": [],
        "deferred": [],
        "paused": [],
        "errors": [],
        "hot_due_left": 0,
    }
    report.update(extra)
    return report


def calendar_sources_for(sources: list, origin: str, destination: str) -> list[str]:
    """Die Quellen, die diese Strecke ueberhaupt bepreisen wuerden.

    Dieselbe Frage, die `build_grid` gleich noch einmal stellt. Verbucht wird
    nur, wer wirklich gefragt wird - eine Quelle, die die Strecke gar nicht
    fliegt, wuerde sonst ihr Budget fuer nichts verlieren.
    """
    return [
        source.name
        for source in sources
        if getattr(source, "supports_calendar", False)
        and source.supports_route(origin, destination)
    ]


def blocked_sources(sources: list) -> list[str]:
    """Quellen, deren Sicherung offen steht.

    Der `CircuitBreaker` lebt im Quellenobjekt und damit nur so lange wie der
    Durchgang: `build_sources` legt den Katalog je Lauf neu an. Was er
    gelernt hat, muss deshalb hier abgeholt und in `hunt_pause` geschrieben
    werden, sonst faengt der naechste Durchgang wieder bei null an.
    """
    return sorted(
        source.name for source in sources
        if getattr(getattr(source, "breaker", None), "is_open", False)
    )


async def run_hunt(conn, *, sources: list | None = None,
                   rates: Rates | None = None,
                   now: datetime | None = None,
                   env: Any = None,
                   post: Any = None) -> dict[str, Any]:
    """Einen heissen Durchgang fahren. Wirft nicht wegen einer einzelnen Strecke."""
    moment = now or datetime.now()
    budget.purge(conn, now=moment)
    budget.clear_pauses(conn, now=moment)

    paused = [row["source"] for row in budget.paused_sources(conn, now=moment)]
    if paused:
        # Kein Abruf, kein Vermerk, keine Strecke gilt als gelaufen: die
        # Strecken bleiben faellig und laufen im Tagestakt ueber
        # `run_watchlist` weiter. Die Aufzeichnung haelt also nie an, nur ihr
        # Takt wird wieder langsam.
        logger.info("Jagd pausiert, Sicherung offen bei: %s", ", ".join(paused))
        conn.commit()
        return empty_report(paused=paused)

    all_due = watchlist.hot_routes(conn, now=moment)
    if not all_due:
        return empty_report()
    due, waiting = all_due[:MAX_ROUTES_PER_RUN], all_due[MAX_ROUTES_PER_RUN:]

    own_sources = sources is None
    if own_sources:
        sources = build_sources(None, conn=conn, env=env or os.environ)
    if rates is None:
        rates = await fx_store.current_rates(conn)

    observations = calls = 0
    ran: list[watchlist.WatchRoute] = []
    deferred: list[str] = []
    errors: list[str] = []
    blocked: list[str] = []
    first_new_id = last_observation_id(conn)

    try:
        await preload_routes(
            sources, [LegSpec(route.origin, route.destination) for route in due]
        )
        for route in due:
            names = calendar_sources_for(sources, route.origin, route.destination)
            if not names:
                # Keine Quelle bepreist die Strecke. Sie gilt trotzdem als
                # gelaufen, sonst kommt sie in jedem Durchgang wieder dran und
                # bleibt genauso stumm.
                watchlist.mark_hot_ran(conn, route.id, now=moment)
                continue
            if not budget.affordable(conn, names, cadence.CALLS_PER_PASS, now=moment):
                deferred.append(route.label)
                continue
            # Vor dem Abruf verbuchen: ein Abruf, der in einem Zeitablauf
            # endet, hat die Quelle genauso erreicht wie einer, der antwortet.
            budget.charge(conn, names, cadence.CALLS_PER_PASS, now=moment)
            try:
                result = await collect_route(
                    conn, route, sources, rates=rates, now=moment,
                    max_cache_age=cadence.MAX_CACHE_AGE,
                )
            except Exception as exc:  # noqa: BLE001 - eine Strecke reisst keine andere mit
                logger.exception("Heisser Durchgang %s gescheitert", route.label)
                errors.append(f"{route.label}: {type(exc).__name__}: {exc}")
            else:
                observations += result["observations"]
                calls += result["calls"]
                errors.extend(result["errors"])
                ran.append(route)
            finally:
                watchlist.mark_hot_ran(conn, route.id, now=moment)

        blocked = blocked_sources(sources)
        if blocked:  # noqa: SIM102 - der Vermerk gehoert vor das Schliessen
            budget.pause(
                conn, blocked,
                reason=f"Sicherung offen ({', '.join(blocked)})", now=moment,
            )
    finally:
        if own_sources:
            for source in sources:
                source.close()

    alert_ids: list[int] = []
    finds_total = 0
    if observations:
        # Erst nachrechnen, dann urteilen: ohne das bleibt die Preislage leer,
        # obwohl die Zeilen da sind.
        refresh_baselines(conn, entity_type="flight")
        for route in ran:
            finds = find_error_fares(conn, route.entity_key, since_id=first_new_id)
            finds_total += len(finds)
            alert_ids.extend(
                await report_finds(conn, finds, env=env, post=post, now=moment)
            )

    conn.commit()
    return empty_report(
        routes=len(ran),
        observations=observations,
        calls=calls,
        finds=finds_total,
        alerts=alert_ids,
        deferred=deferred,
        paused=blocked,
        errors=errors[:10],
        # Verschoben und noch nicht drangekommen sind dasselbe fuer den, der
        # zusieht: beide sind faellig und laufen im naechsten Durchgang.
        hot_due_left=len(deferred) + len(waiting),
    )
