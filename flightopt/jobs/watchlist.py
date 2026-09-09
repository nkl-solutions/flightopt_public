"""Der Tageslauf der Beobachtungsliste.

Je eingetragener Strecke wird einmal am Tag der Preiskalender geholt und in
`price_observation` geschrieben. Mehr nicht: keine Kombinatorik, keine
Live-Pruefung, keine Ergebnisliste. Das ist der billige Teil des bestehenden
Ablaufs - ein Abruf je Quelle deckt ein ganzes Fenster ab - und genau der
Teil, der die Preishistorie fuellt.

Warum das ueberhaupt noetig ist: die Preislage-Spalte, die getrennten
Baselines und `detect_price_signal` messen gegen eine Grundgesamtheit, die
bisher nur nebenbei entstand - naemlich dann, wenn jemand von Hand suchte.
`min_samples` je Kombination aus Strecke, Wochentag und Vorlauf-Fenster wird so
nie erreicht. Ein Fenster von sechzig Tagen liefert dagegen rund sechzig Zeilen
je Strecke und Quelle am Tag, und die Aussage traegt nach gut einer Woche.

Hoeflichkeit ist hier kein Beiwerk: es sind dieselben Quellen, die auch die
Suche benutzt, und eine Sperre traefe beide. Deshalb laufen die Strecken
nacheinander, jede genau einmal am Tag, mit dem Rate-Limiter und der Sicherung
der Quellen (`build_grid` ruft sie ueber dieselben Adapter auf) und mit einer
Obergrenze je Durchgang.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Any

from flightopt.domain.fx import Rates
from flightopt.domain.models import LegSpec, SearchSpec
from flightopt.hunt.finder import find_error_fares, last_observation_id, report_finds
from flightopt.jobs.runner import preload_routes
from flightopt.search.grid import build_grid
from flightopt.sources.registry import build_sources
from flightopt.storage import fx_store, watchlist
from flightopt.storage.baseline import refresh_baselines
from flightopt.storage.cache import SqliteCache, SqliteHistory

logger = logging.getLogger(__name__)

MAX_ROUTES_PER_RUN = 8
"""Wie viele Strecken ein Durchgang anfasst.

Der Rest bleibt faellig und kommt im naechsten Durchgang dran - der
Tagesplaner laeuft alle paar Minuten, eine lange Liste ist damit nach kurzer
Zeit durch. Ohne diese Grenze haelt ein einziger Durchgang die Verbindung und
die Quellen beliebig lange besetzt, und ein Neustart mittendrin verliert alles
Angefangene auf einmal.
"""


def route_spec(route: watchlist.WatchRoute, today) -> SearchSpec:
    """Die Beobachtung als Suchauftrag ueber genau eine Teilstrecke.

    `build_grid` bleibt damit unveraendert benutzbar: es kennt nur SearchSpec,
    und ein Auftrag ohne Aufenthalte und ohne zweite Teilstrecke ist der
    kleinste, den es gibt. Umstiege bleiben auf `None`, also auf dem, was die
    Entfernung nahelegt - dieselbe Vorgabe wie in der Suche, sonst misst die
    Historie ein anderes Produkt als die Ergebnisliste.
    """
    start, end = route.window(today)
    return SearchSpec(
        legs=(LegSpec(route.origin, route.destination),),
        stays=(),
        window_start=start,
        window_end=end,
        currency=route.currency,
    )


async def collect_route(conn, route: watchlist.WatchRoute, sources: list, *,
                        rates: Rates | None = None,
                        now: datetime | None = None,
                        max_cache_age: timedelta | None = None) -> dict[str, Any]:
    """Eine Strecke einsammeln. Geschrieben wird in `build_grid`, nicht hier.

    Der Rueckgabewert zaehlt nur nach; die Zeilen selbst legt `SqliteHistory`
    an, mit demselben Schluessel und demselben Kennzeichen wie bei einer
    Suche. Zwei Schreibwege in dieselbe Tabelle waeren zwei Wahrheiten.

    `max_cache_age` gibt der Jagd, was sie braucht: eine Antwort, die juenger
    ist als ihr Takt. Der Tageslauf laesst es leer und liest damit wie bisher
    gegen die TTL - ein Kalender von heute Morgen beantwortet seine Frage.
    """
    moment = now or datetime.now()
    spec = route_spec(route, moment.date())
    grid, report = await build_grid(
        spec,
        sources,
        cache=SqliteCache(conn),
        history=SqliteHistory(conn),
        rates=rates,
        max_cache_age=max_cache_age,
        now=moment,
    )
    return {
        "route": route.label,
        "window": [spec.window_start.isoformat(), spec.window_end.isoformat()],
        "observations": sum(report.per_source.values()),
        "calls": report.calls,
        "cache_hits": report.cache_hits,
        "errors": [f"{route.label}: {note}" for note in report.errors],
    }


async def run_watchlist(conn, *, sources: list | None = None,
                        rates: Rates | None = None,
                        now: datetime | None = None,
                        env: Any = None,
                        post: Any = None) -> dict[str, Any]:
    """Alle heute faelligen Strecken abarbeiten, nacheinander.

    Nacheinander und nicht nebeneinander: `build_grid` faechert bereits ueber
    alle Quellen einer Strecke auf, und zwei Strecken gleichzeitig hiesse
    doppelt so viele offene Anfragen an denselben Host. Der Gewinn waere
    Sekunden, der Preis eine Sperre, die auch die Suche trifft.

    Eine Strecke gilt als gelaufen, sobald sie an der Reihe war - auch wenn
    keine Quelle etwas hergab. Andernfalls bliebe sie faellig und der naechste
    Durchgang faenge dieselbe erfolglose Runde von vorn an.

    Gemeldet wird auch hier. Ein Fehltarif faellt nicht nur auf heissen
    Strecken vom Himmel, und die Zeilen liegen ohnehin schon geschrieben da -
    sie nicht anzusehen waere die einzige Ersparnis, die nichts spart.
    """
    moment = now or datetime.now()
    due = watchlist.due_routes(conn, now=moment)
    if not due:
        return {"routes": 0, "observations": 0, "calls": 0, "errors": [],
                "due_left": 0, "finds": 0, "alerts": []}

    batch, rest = due[:MAX_ROUTES_PER_RUN], due[MAX_ROUTES_PER_RUN:]
    own_sources = sources is None
    if own_sources:
        sources = build_sources(None, conn=conn, env=env or os.environ)
    if rates is None:
        rates = await fx_store.current_rates(conn)

    observations = calls = 0
    errors: list[str] = []
    ran: list[watchlist.WatchRoute] = []
    first_new_id = last_observation_id(conn)
    try:
        # Einmal fuer den ganzen Durchgang, nicht je Strecke: `preload_routes`
        # fragt je Startflughafen einmal, und zwei Strecken ab Berlin sind ein
        # Streckennetz. Nicht jede Quelle merkt sich ihre Antwort, und der
        # Abruf zaehlt bei denselben Servern wie die Preisabfrage.
        await preload_routes(
            sources, [LegSpec(route.origin, route.destination) for route in batch]
        )
        for route in batch:
            try:
                result = await collect_route(
                    conn, route, sources, rates=rates, now=moment
                )
            except Exception as exc:  # noqa: BLE001 - eine Strecke reisst keine andere mit
                logger.exception("Beobachtung %s gescheitert", route.label)
                errors.append(f"{route.label}: {type(exc).__name__}: {exc}")
            else:
                observations += result["observations"]
                calls += result["calls"]
                errors.extend(result["errors"])
                ran.append(route)
            finally:
                watchlist.mark_ran(conn, route.id, now=moment)
    finally:
        if own_sources:
            for source in sources:
                source.close()

    alert_ids: list[int] = []
    finds_total = 0
    if observations:
        # Ohne Nachrechnen bleibt die Preislage leer, obwohl die Zeilen da
        # sind. Einmal je Durchgang, nicht je Strecke: die Rechnung geht
        # ohnehin ueber die ganze Tabelle.
        refresh_baselines(conn, entity_type="flight")
        for route in ran:
            finds = find_error_fares(conn, route.entity_key, since_id=first_new_id)
            finds_total += len(finds)
            alert_ids.extend(
                await report_finds(conn, finds, env=env, post=post, now=moment)
            )
    conn.commit()
    return {
        "routes": len(batch),
        "observations": observations,
        "calls": calls,
        "errors": errors[:10],
        "due_left": len(rest),
        "finds": finds_total,
        "alerts": alert_ids,
    }
