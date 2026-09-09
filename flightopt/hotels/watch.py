"""Die Hotelsuche laeuft von selbst: gespeicherte Suchen, taeglich, mit Historie.

Bisher war ein Hotellauf etwas, das ein Mensch startet. Damit findet er nie
einen Preisfehler: die Erkennung braucht fuenf Beobachtungen je Haus,
Wochentag und Vorlauf, und fuenf Beobachtungen entstehen nur, wenn dieselbe
Suche fuenfmal laeuft. Ein Mensch, der fuenfmal dieselbe Suche klickt, ist
kein Betriebsmodell.

Der Zuschnitt ist der der Flug-Beobachtungsliste (`storage/watchlist.py` und
`jobs/watchlist.py`), und zwar mit Absicht bis in die Details:

* **Rollendes Vorlauf-Fenster, kein festes Datum.** Eine gespeicherte Suche
  mit festem Reisetag hoert nach dem Reisetag auf, Sinn zu ergeben. Eine
  Beobachtung laeuft weiter, bis jemand sie abschaltet.
* **Faellig ist ein Kalendertag, kein Stundenabstand.** Sonst wandert "einmal
  am Tag" jeden Tag ein Stueck nach hinten, und ein Neustart des Dienstes
  loest einen zweiten Abruf aus.
* **`mark_ran` steht im `finally`.** Gelaufen heisst gelaufen, auch wenn
  nichts herauskam. Sonst laeuft dieselbe kaputte Suche im Takt des Planers
  immer wieder gegen dieselbe Wand.
* **Ein Deckel je Durchgang.** Ein Hotelfenster kostet ein Vielfaches eines
  Flugkalenders: je Anreisetag eine Anfrage je Quelle. Alle faelligen auf
  einmal zu fahren hiesse, den Takt der Quellen minutenlang zu halten,
  waehrend nebenan die Flugsuche wartet.
* **Ein Schreibweg.** Geschrieben wird ueber `run_scan` und damit ueber
  `store.record_offers` - derselbe Weg wie beim Handbetrieb. Zwei Schreibwege
  in dieselbe Tabelle waeren zwei Wahrheiten.

Die Tabelle wird hier angelegt und nicht in `storage/db.py`, wie schon
`hotel_baseline`: `CREATE TABLE IF NOT EXISTS` legt sie auch in einer
bestehenden Datei an, und der Bereich bleibt beisammen.

**Wie lange es dauert, bis etwas gefunden wird - die unbequeme Rechnung.**
Die Baseline gruppiert je Haus, Wochentag, Vorlauf-Stufe, Belegung und
Waehrung und braucht fuenf Beobachtungen je Gruppe. Ein Fenster von einem
einzigen Anreisetag liefert je Durchgang genau eine Beobachtung in genau eine
Gruppe, und die naechste in dieselbe Gruppe erst eine Woche spaeter: der
Wochentag wandert mit. Fuenf Beobachtungen heissen dann **fuenf Wochen**.

Ein breites Fenster ist deshalb nicht Luxus, sondern der Unterschied zwischen
Wochen und Tagen. Vierzehn Anreisetage innerhalb derselben Vorlauf-Stufe
decken jeden Wochentag zweimal ab; dieselbe Gruppe hat nach drei Durchgaengen
sechs Punkte. Die Voreinstellung (14 bis 20 Tage Vorlauf) liegt bewusst
vollstaendig in der Stufe "14-29" und deckt jeden Wochentag genau einmal - ein
Kompromiss zwischen Anfragen und Anlaufzeit, und `test_hotels_watch.py`
haelt beide Rechnungen fest.

Bis dahin traegt genau eine Bedingung: die Plausibilitaetsschranke je
Sternekategorie. Sie braucht keine Historie und findet nur, was in keinem
europaeischen Markt ein Angebot ist. Das ist wenig, und die Zeile sagt es auch
(`signals.qualify`).

Was dieses Modul **nicht** tut: es meldet nichts. Es liefert mit
`hotel_findings` die fertigen Zeilen und ueberlaesst das Verschicken dem, der
es auch fuer die Fluege tut. Zwei Meldewege waeren zwei Formate.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Sequence

from flightopt.domain.fx import Rates
from flightopt.domain.models import Money
from flightopt.hotels.models import HotelOffer, HotelQuery
from flightopt.hotels.registry import build_hotel_sources
from flightopt.hotels.scan import create_scan, run_scan, scan_days
from flightopt.hotels.signals import TIER_ERROR, TIER_RANK
from flightopt.hotels.sources.base import HotelSource
from flightopt.hotels.store import signal_for
from flightopt.storage import fx_store
from flightopt.storage.baseline import refresh_baselines

logger = logging.getLogger(__name__)

DEFAULT_LEAD_MIN = 14
DEFAULT_LEAD_MAX = 20
"""Eine Woche Anreisetage, zwei bis drei Wochen im Voraus.

Enger als bei den Fluegen (14 bis 73), und der Grund ist der Preis: ein
Flugkalender liefert sechzig Tage in einer Anfrage, ein Hotelfenster braucht
eine Anfrage je Tag und Quelle. Sieben Tage sind vierzehn Anfragen je
Durchgang - taeglich, dauerhaft, gegen zwei fremde Server.
"""

MAX_LEAD_DAYS = 365
MAX_WINDOW_DAYS = 31
"""Anreisetage je Beobachtung. Mehr ist ein Vertipper und keine Absicht."""

MAX_WATCHES_PER_RUN = 3
"""Beobachtungen je Durchgang. Der Rest wartet auf den naechsten Takt."""

MIN_RECORDING_DAYS = 5
"""Ab so vielen aufgezeichneten Tagen traegt die Baseline ueberhaupt etwas.

Dieselbe Zahl wie `refresh_baselines(min_samples=5)`. Sie steht hier noch
einmal, damit die Oberflaeche sagen kann, wie weit eine Beobachtung ist -
und nicht so tut, als waere sie schon fertig.
"""

REPORTED_TIERS: tuple[str, ...] = (TIER_ERROR,)
"""Was gemeldet wird. `cheap` ist ein Angebot und keine Meldung wert."""

HOTEL_WATCH_SCHEMA = """
-- Eine Zeile je beobachteter Suche. Der `fingerprint` haelt sie eindeutig:
-- zweimal dasselbe Ziel mit derselben Belegung und denselben Filtern waeren
-- zweimal dieselben Abrufe am selben Tag bei denselben Quellen.
CREATE TABLE IF NOT EXISTS hotel_watch (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint      TEXT NOT NULL UNIQUE,
    destination      TEXT NOT NULL,
    nights           INTEGER NOT NULL DEFAULT 1,
    adults           INTEGER NOT NULL DEFAULT 2,
    children         TEXT NOT NULL DEFAULT '',   -- Alter, mit Komma getrennt
    rooms            INTEGER NOT NULL DEFAULT 1,
    stars            TEXT NOT NULL DEFAULT '',   -- Sterne, mit Komma getrennt
    min_review_score REAL,
    currency         TEXT NOT NULL DEFAULT 'EUR',
    country          TEXT NOT NULL DEFAULT 'DE',
    lead_min_days    INTEGER NOT NULL,
    lead_max_days    INTEGER NOT NULL,
    enabled          INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL,
    last_run_at      TEXT
);
CREATE INDEX IF NOT EXISTS ix_hotel_watch_due ON hotel_watch(enabled, last_run_at);
"""


def ensure_hotel_watch(conn: sqlite3.Connection) -> None:
    """Die Tabelle anlegen, falls sie fehlt. Idempotent und billig."""
    conn.executescript(HOTEL_WATCH_SCHEMA)


def _numbers(raw: str | None) -> tuple[int, ...]:
    if not raw:
        return ()
    return tuple(int(part) for part in str(raw).split(",") if part.strip())


def _text(values: Iterable[int]) -> str:
    return ",".join(str(int(value)) for value in values)


@dataclass(frozen=True, slots=True)
class HotelWatch:
    """Eine beobachtete Suche samt rollendem Vorlauf-Fenster."""

    id: int
    destination: str
    nights: int
    adults: int
    children: tuple[int, ...]
    rooms: int
    stars: tuple[int, ...]
    min_review_score: float | None
    currency: str
    country: str
    lead_min_days: int
    lead_max_days: int
    enabled: bool
    created_at: str
    last_run_at: str | None

    @property
    def label(self) -> str:
        party = self.adults + len(self.children)
        return f"{self.destination} ({self.nights}N, {party}P)"

    def window(self, today: date) -> tuple[date, date]:
        return (
            today + timedelta(days=self.lead_min_days),
            today + timedelta(days=self.lead_max_days),
        )

    def query(self, arrival: date) -> HotelQuery:
        return HotelQuery(
            destination=self.destination,
            arrival=arrival,
            nights=self.nights,
            adults=self.adults,
            children=self.children,
            rooms=self.rooms,
            stars=self.stars,
            country=self.country,
            currency=self.currency,
            min_review_score=self.min_review_score,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "destination": self.destination,
            "nights": self.nights,
            "adults": self.adults,
            "children": list(self.children),
            "rooms": self.rooms,
            "stars": list(self.stars),
            "min_review_score": self.min_review_score,
            "currency": self.currency,
            "country": self.country,
            "lead_min_days": self.lead_min_days,
            "lead_max_days": self.lead_max_days,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "last_run_at": self.last_run_at,
        }


def _row(row: sqlite3.Row) -> HotelWatch:
    return HotelWatch(
        id=int(row["id"]),
        destination=str(row["destination"]),
        nights=int(row["nights"]),
        adults=int(row["adults"]),
        children=_numbers(row["children"]),
        rooms=int(row["rooms"]),
        stars=_numbers(row["stars"]),
        min_review_score=row["min_review_score"],
        currency=str(row["currency"]),
        country=str(row["country"]),
        lead_min_days=int(row["lead_min_days"]),
        lead_max_days=int(row["lead_max_days"]),
        enabled=bool(row["enabled"]),
        created_at=str(row["created_at"]),
        last_run_at=row["last_run_at"],
    )


def fingerprint(
    destination: str,
    *,
    nights: int,
    adults: int,
    children: Sequence[int],
    rooms: int,
    stars: Sequence[int],
    min_review_score: float | None,
    currency: str,
    country: str,
) -> str:
    """Alles, was die Antwort veraendert, in einen Schluessel.

    Das Vorlauf-Fenster gehoert ausdruecklich **nicht** dazu: es aendert nur,
    welche Tage gefragt werden, nicht welche Frage. Wer das Fenster
    verschiebt, verschiebt dieselbe Beobachtung und legt keine zweite an.
    """
    return "|".join(
        [
            destination.strip().casefold(),
            str(int(nights)),
            str(int(adults)),
            _text(sorted(children)),
            str(int(rooms)),
            _text(sorted(stars)),
            "" if min_review_score is None else f"{float(min_review_score):.1f}",
            currency.upper(),
            country.upper(),
        ]
    )


def _check_window(lead_min_days: int, lead_max_days: int) -> tuple[int, int]:
    low, high = int(lead_min_days), int(lead_max_days)
    if low < 0 or high < 0:
        raise ValueError("Vorlauf kann nicht negativ sein")
    if high < low:
        raise ValueError(f"Vorlauf {low} bis {high} laeuft rueckwaerts")
    if high > MAX_LEAD_DAYS:
        raise ValueError(f"Vorlauf ueber {MAX_LEAD_DAYS} Tage ist keine Absicht")
    if high - low + 1 > MAX_WINDOW_DAYS:
        raise ValueError(
            f"{high - low + 1} Anreisetage je Beobachtung, hoechstens "
            f"{MAX_WINDOW_DAYS}: bitte aufteilen"
        )
    return low, high


def add_watch(
    conn: sqlite3.Connection,
    destination: str,
    *,
    nights: int = 1,
    adults: int = 2,
    children: Sequence[int] = (),
    rooms: int = 1,
    stars: Sequence[int] = (),
    min_review_score: float | None = None,
    currency: str = "EUR",
    country: str = "DE",
    lead_min_days: int = DEFAULT_LEAD_MIN,
    lead_max_days: int = DEFAULT_LEAD_MAX,
    now: datetime | None = None,
) -> int:
    """Eine Beobachtung anlegen oder ihr Fenster aendern. Zurueck kommt die Kennung."""
    ensure_hotel_watch(conn)
    place = str(destination).strip()
    if not place:
        raise ValueError("Beobachtung ohne Ziel")
    low, high = _check_window(lead_min_days, lead_max_days)
    # Ein Griff durch `HotelQuery`, damit Belegung, Sterne und Naechte hier
    # dieselben Regeln sehen wie in einer Suche - und nicht erst im Durchgang.
    probe = HotelQuery(
        destination=place,
        arrival=(now or datetime.now()).date(),
        nights=nights,
        adults=adults,
        children=tuple(int(age) for age in children),
        rooms=rooms,
        stars=tuple(int(star) for star in stars),
        country=country,
        currency=currency,
        min_review_score=min_review_score,
    )
    mark = fingerprint(
        place,
        nights=probe.nights,
        adults=probe.adults,
        children=probe.children,
        rooms=probe.rooms,
        stars=probe.stars,
        min_review_score=probe.min_review_score,
        currency=probe.currency,
        country=probe.country,
    )
    conn.execute(
        "INSERT INTO hotel_watch("
        "fingerprint, destination, nights, adults, children, rooms, stars, "
        "min_review_score, currency, country, lead_min_days, lead_max_days, "
        "enabled, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?) "
        "ON CONFLICT(fingerprint) DO UPDATE SET "
        "lead_min_days=excluded.lead_min_days, "
        "lead_max_days=excluded.lead_max_days, enabled=1",
        (
            mark,
            place,
            probe.nights,
            probe.adults,
            _text(probe.children),
            probe.rooms,
            _text(probe.stars),
            probe.min_review_score,
            probe.currency.upper(),
            probe.country.upper(),
            low,
            high,
            (now or datetime.now()).isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM hotel_watch WHERE fingerprint=?", (mark,)
    ).fetchone()
    return int(row["id"])


def list_watches(conn: sqlite3.Connection) -> list[HotelWatch]:
    ensure_hotel_watch(conn)
    rows = conn.execute("SELECT * FROM hotel_watch ORDER BY destination, id").fetchall()
    return [_row(row) for row in rows]


def get_watch(conn: sqlite3.Connection, watch_id: int) -> HotelWatch | None:
    ensure_hotel_watch(conn)
    row = conn.execute("SELECT * FROM hotel_watch WHERE id=?", (watch_id,)).fetchone()
    return _row(row) if row else None


def set_enabled(conn: sqlite3.Connection, watch_id: int, enabled: bool) -> HotelWatch:
    ensure_hotel_watch(conn)
    conn.execute(
        "UPDATE hotel_watch SET enabled=? WHERE id=?", (int(bool(enabled)), watch_id)
    )
    conn.commit()
    watch = get_watch(conn, watch_id)
    if watch is None:
        raise ValueError(f"Beobachtung {watch_id} gibt es nicht")
    return watch


def due_watches(
    conn: sqlite3.Connection, *, now: datetime | None = None
) -> list[HotelWatch]:
    """Was heute noch nicht gelaufen ist.

    Verglichen wird der Kalendertag und nicht der Abstand. Sonst wandert
    "einmal am Tag" jeden Tag ein Stueck nach hinten, und ein Neustart des
    Dienstes loest einen zweiten Abruf aus.
    """
    ensure_hotel_watch(conn)
    today = (now or datetime.now()).date().isoformat()
    rows = conn.execute(
        "SELECT * FROM hotel_watch WHERE enabled=1 "
        "AND (last_run_at IS NULL OR substr(last_run_at, 1, 10) < ?) "
        "ORDER BY COALESCE(last_run_at, ''), id",
        (today,),
    ).fetchall()
    return [_row(row) for row in rows]


def mark_ran(
    conn: sqlite3.Connection, watch_id: int, *, now: datetime | None = None
) -> None:
    conn.execute(
        "UPDATE hotel_watch SET last_run_at=? WHERE id=?",
        ((now or datetime.now()).isoformat(timespec="seconds"), watch_id),
    )
    conn.commit()


# --------------------------------------------------------------------------
# Der Durchgang
# --------------------------------------------------------------------------


async def run_hotel_watches(
    conn: sqlite3.Connection,
    *,
    sources: Sequence[HotelSource] | None = None,
    rates: Rates | None = None,
    now: datetime | None = None,
    env: Any = None,
) -> dict[str, Any]:
    """Die faelligen Beobachtungen abarbeiten. Ein Durchgang, ein Ergebnis.

    Gedacht als der eine Aufruf, den ein Planer je Takt braucht - neben
    `run_watchlist` fuer die Fluege und mit demselben Rueckgabewert-Zuschnitt.
    """
    moment = now or datetime.now()
    due = due_watches(conn, now=moment)
    report: dict[str, Any] = {
        "watches": 0, "days": 0, "observations": 0, "errors": [], "due_left": 0
    }
    if not due:
        return report

    batch, rest = due[:MAX_WATCHES_PER_RUN], due[MAX_WATCHES_PER_RUN:]
    report["due_left"] = len(rest)
    live = list(sources) if sources is not None else build_hotel_sources(env)
    if not live:
        report["errors"].append("kein Quellen-Katalog")
        return report
    if rates is None:
        rates = await fx_store.current_rates(conn)

    errors: list[str] = []
    observations = 0
    days = 0
    try:
        for watch in batch:
            start, end = watch.window(moment.date())
            try:
                scan_id = create_scan(
                    conn,
                    watch.query(start),
                    window_start=start,
                    window_end=end,
                    now=moment.isoformat(timespec="seconds"),
                )
                result = await run_scan(
                    conn,
                    watch.query(start),
                    window_start=start,
                    window_end=end,
                    sources=live,
                    rates=rates,
                    scan_id=scan_id,
                    observed_at=moment,
                )
                observations += len(result.offers)
                days += result.days_done
                errors.extend(f"{watch.label}: {line}" for line in result.errors)
                if result.error:
                    errors.append(f"{watch.label}: {result.error}")
            except Exception as exc:  # noqa: BLE001 - eine Suche, nicht der Durchgang
                logger.warning("hotels: Beobachtung %s scheiterte: %s", watch.label, exc)
                errors.append(f"{watch.label}: {exc}")
            finally:
                # Gelaufen heisst gelaufen. Sonst laeuft dieselbe kaputte
                # Suche im Takt des Planers immer wieder gegen dieselbe Wand.
                mark_ran(conn, watch.id, now=moment)
                report["watches"] += 1
    finally:
        if sources is None:
            for source in live:
                try:
                    source.close()
                except Exception:  # noqa: BLE001 - ein Aufraeumer bricht nichts ab
                    logger.warning("hotels: %s liess sich nicht schliessen", source.name)

    if observations:
        # Einmal je Durchgang und nicht je Tag: die Auffrischung liest die
        # ganze Historie, und ohne sie waechst sie zwar, aber kein Urteil
        # folgt ihr.
        refresh_baselines(conn, entity_type="hotel", now=moment)
    conn.commit()

    report["observations"] = observations
    report["days"] = days
    report["errors"] = errors[:10]
    return report


# --------------------------------------------------------------------------
# Was gemeldet wird
# --------------------------------------------------------------------------


def _offer_from_row(row: sqlite3.Row, *, nights: int, party: int) -> HotelOffer:
    arrival = date.fromisoformat(str(row["travel_date"]))
    price = Money(int(row["price_total_minor"]), str(row["currency"]))
    return HotelOffer(
        source=str(row["source"]),
        property_key=str(row["entity_key"]).split("|", 1)[-1],
        name=str(row["name"] or row["entity_key"]),
        arrival=arrival,
        departure=arrival + timedelta(days=nights),
        price_total=price,
        price_eur=price if price.currency == "EUR" else None,
        stars=row["stars"],
        city=row["city"],
        country=row["country"],
        review_rating=row["review_rating"],
        review_count=row["review_count"],
        url=row["url"],
        party_size=party or 1,
        indicative=bool(row["is_estimate"]),
    )


def hotel_findings(
    conn: sqlite3.Connection,
    *,
    since: datetime | None = None,
    now: datetime | None = None,
    limit: int = 20,
    tiers: Sequence[str] = REPORTED_TIERS,
) -> list[dict[str, Any]]:
    """Die auffaelligen Zeilen des juengsten Zeitraums, fertig gerechnet.

    Der Anschluss fuer eine Meldung, und zwar nur der Anschluss: verschickt
    wird hier nichts.

    Die Feldnamen sind die von `flightopt.hunt.alerts.Find` - `entity_key`,
    `travel_date`, `tier`, `price_minor`, `currency`, `source`,
    `median_minor`, `n`, `population`, `reason`, `thin` -, damit die
    Fehltarif-Jagd der Fluege und die Hotels denselben Melder fuellen koennen.
    Was nur Hotels haben (Name, Stadt, Sterne, Naechte, Verweis), steht unter
    `detail`, genau wie das Feld gleichen Namens am `Find`. Ein Fund wird
    daraus mit

        Find(entity_key=row["entity_key"],
             travel_date=date.fromisoformat(row["travel_date"]), ...)

    Ein Rueckgabetyp `Find` waere die Alternative gewesen, haette aber diesen
    Bereich an das Meldemodul gebunden: `Find.route` und `Find.booking_url`
    rechnen mit einem Streckenschluessel `origin|destination`, und ein
    Hotelschluessel ist keiner. Die Nachricht fuer Hotels gehoert dorthin, wo
    auch die fuer Fluege steht.

    Gezeigt wird je Haus und Anreisetag die juengste Beobachtung. Aeltere
    Zeilen sind Historie und keine Meldung.
    """
    moment = now or datetime.now()
    start = (since or (moment - timedelta(days=1))).isoformat(timespec="seconds")
    wanted = {str(tier) for tier in tiers}
    rows = conn.execute(
        "SELECT o.entity_key, o.source, o.travel_date, o.currency, "
        "o.price_total_minor, o.is_estimate, o.observed_at, o.party_size, "
        "o.return_or_nights, "
        "p.name, p.city, p.country, p.stars, p.review_rating, p.review_count, p.url "
        "FROM price_observation o "
        "LEFT JOIN hotel_property p ON p.property_key = "
        "  substr(o.entity_key, instr(o.entity_key, '|') + 1) "
        "WHERE o.entity_type='hotel' AND o.observed_at >= ? "
        "ORDER BY o.observed_at",
        (start,),
    ).fetchall()

    latest: dict[tuple[str, str, str, int], sqlite3.Row] = {}
    for row in rows:
        key = (
            str(row["entity_key"]),
            str(row["travel_date"]),
            str(row["return_or_nights"]),
            int(row["party_size"]),
        )
        latest[key] = row

    found: list[dict[str, Any]] = []
    for (_, _, raw_nights, party), row in latest.items():
        try:
            nights = max(1, int(raw_nights))
        except (TypeError, ValueError):
            nights = 1
        offer = _offer_from_row(row, nights=nights, party=party)
        signal = signal_for(conn, offer, observed_at=moment)
        if signal.get("tier") not in wanted:
            continue
        median = signal.get("median_minor")
        price_minor = offer.price.minor
        found.append(
            {
                # Die Felder, die `alerts.Find` erwartet.
                "entity_key": offer.entity_key,
                "travel_date": offer.arrival.isoformat(),
                "tier": signal.get("tier", "unknown"),
                "price_minor": price_minor,
                "currency": offer.price.currency,
                "source": offer.source,
                "median_minor": median,
                "n": signal.get("n", 0),
                "population": signal.get("population", ""),
                "reason": signal.get("reason", ""),
                "thin": bool(signal.get("thin")),
                # Alles, was nur Hotels haben.
                "detail": {
                    "kind": "hotel",
                    "name": offer.name,
                    "city": offer.city,
                    "stars": offer.stars,
                    "nights": nights,
                    "url": offer.url,
                    "observed_at": str(row["observed_at"]),
                    "price": price_minor / 100,
                    "price_per_night": offer.price_per_night.minor / 100,
                    "median": median / 100 if median else None,
                    "deviation_pct": (
                        round((price_minor - median) / median * 100, 1)
                        if median
                        else None
                    ),
                    "signal": signal.get("status", "unknown"),
                    "basis": signal.get("basis", "none"),
                    "evidence": signal.get("evidence", "keine"),
                    # Wie bei den Fluegen: `verified` heisst "kein Richtwert".
                    "verified": not offer.indicative,
                },
            }
        )
    found.sort(
        key=lambda row: (
            TIER_RANK.get(str(row["tier"]), 9),
            row["detail"]["deviation_pct"] if row["detail"]["deviation_pct"] is not None else 0.0,
            row["price_minor"],
        )
    )
    return found[: max(0, int(limit))]


# --------------------------------------------------------------------------
# Auskunft
# --------------------------------------------------------------------------


def watch_stats(conn: sqlite3.Connection, watch: HotelWatch) -> dict[str, Any]:
    """Wie weit diese Beobachtung ist. Ohne Historie ist nichts messbar."""
    row = conn.execute(
        "SELECT count(*) AS observations, "
        "count(DISTINCT substr(observed_at, 1, 10)) AS days, "
        "max(observed_at) AS last_observation "
        "FROM price_observation "
        "WHERE entity_type='hotel' AND return_or_nights=? AND party_size=?",
        (str(watch.nights), watch.adults + len(watch.children)),
    ).fetchone()
    days = int(row["days"] or 0)
    return {
        "observations": int(row["observations"] or 0),
        "days_recorded": days,
        "last_observation": row["last_observation"],
        "min_days": MIN_RECORDING_DAYS,
        # "Bereit" heisst nicht "findet etwas", sondern nur: ab hier kann
        # ueberhaupt eine Baseline entstehen. Vorher sagt jede Stufe ausser
        # der Plausibilitaetsschranke nichts.
        "ready": days >= MIN_RECORDING_DAYS,
    }


def watch_report(
    conn: sqlite3.Connection, *, now: datetime | None = None
) -> dict[str, Any]:
    """Was beobachtet wird, seit wann, und was heute noch ansteht."""
    moment = now or datetime.now()
    watches = list_watches(conn)
    due = {watch.id for watch in due_watches(conn, now=moment)}
    rows = []
    for watch in watches:
        data = watch.as_dict()
        data.update(watch_stats(conn, watch))
        data["due"] = watch.id in due
        data["window"] = [day.isoformat() for day in watch.window(moment.date())]
        data["days_per_run"] = len(scan_days(*watch.window(moment.date())))
        rows.append(data)
    return {
        "watches": len(watches),
        "due": len(due),
        "max_per_run": MAX_WATCHES_PER_RUN,
        "rows": rows,
    }
