"""Local, dry-run price targets for completed explicitly linked profile jobs.

Events contain immutable snapshots. Evaluation markers and events share a
savepoint so failed writes can be retried without losing or duplicating alerts.
The caller retains ownership of any surrounding transaction.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, DecimalException, localcontext
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import uuid4


MAX_LIMIT = 500
_MAX_MINOR = 2**63 - 1
_COLUMNS = (
    "id,profile_id,profile_name,job_id,price_minor,target_minor,currency,"
    "route,dates,legs,created_at,checked_at,acknowledged_at,delivery"
)


@contextmanager
def _atomic(conn: sqlite3.Connection):
    name = "price_targets_" + uuid4().hex
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
        conn.execute(f"RELEASE SAVEPOINT {name}")
    except BaseException:
        conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
        conn.execute(f"RELEASE SAVEPOINT {name}")
        raise


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create only additive price-target tables, without implicit commits."""
    with _atomic(conn):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS price_target_evaluation ("
            "job_id INTEGER PRIMARY KEY, checked_at TEXT NOT NULL)"
        )
        # No cascading foreign keys: deleting a profile/job preserves its snapshot.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS price_target_event ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, profile_id INTEGER NOT NULL,"
            "profile_name TEXT NOT NULL, job_id INTEGER NOT NULL UNIQUE,"
            "price_minor INTEGER NOT NULL, target_minor INTEGER NOT NULL,"
            "currency TEXT NOT NULL, route TEXT NOT NULL, dates TEXT NOT NULL,"
            "legs TEXT NOT NULL, created_at TEXT NOT NULL, checked_at TEXT NOT NULL,"
            "acknowledged_at TEXT, delivery TEXT NOT NULL DEFAULT 'dry_run' "
            "CHECK(delivery='dry_run'))"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_price_target_chain "
            "ON price_target_event(profile_id,route,dates,currency,id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_price_target_recent "
            "ON price_target_event(created_at,id)"
        )


def _limit(value: int) -> int:
    if type(value) is not int:
        raise ValueError("limit must be an integer")
    return min(MAX_LIMIT, max(0, value))


def _rows(conn: sqlite3.Connection, sql: str, params=()) -> list[dict[str, Any]]:
    cursor = conn.execute(sql, params)
    keys = [column[0] for column in cursor.description]
    return [dict(zip(keys, row)) for row in cursor.fetchall()]


def _reject_constant(value: str):
    raise ValueError(f"non-finite JSON number: {value}")


def _unique_object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON key: {key}")
        out[key] = value
    return out


def _json(value: str):
    return json.loads(value, parse_float=Decimal, parse_constant=_reject_constant,
                      object_pairs_hook=_unique_object)


def _date(value: str) -> date:
    if not isinstance(value, str):
        raise ValueError("date must be an ISO date")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("date must be an ISO date")
    return parsed


def _variants(value: str) -> list[dict[str, Any]]:
    data = _json(value)
    if not isinstance(data, dict):
        raise ValueError("spec must be an object")
    variants = data.get("variants", [data])
    if not isinstance(variants, list) or not variants:
        raise ValueError("spec needs concrete variants")
    if "variant_count" in data and (
        type(data.get("variant_count")) is not int or data["variant_count"] != len(variants)
    ):
        raise ValueError("variant count mismatch")
    for variant in variants:
        if not isinstance(variant, dict):
            raise ValueError("variant must be an object")
        airports = variant.get("airports")
        if (not isinstance(airports, list) or len(airports) < 2 or
                any(not isinstance(a, str) or not re.fullmatch(r"[A-Z]{3}", a) for a in airports)):
            raise ValueError("invalid route")
        currency = variant.get("currency")
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("invalid currency")
        if _date(variant["window_start"]) > _date(variant["window_end"]):
            raise ValueError("invalid window")
        stays = variant.get("stays")
        if not isinstance(stays, list) or len(stays) != len(airports) - 2:
            raise ValueError("stay count mismatch")
        for stay in stays:
            if (not isinstance(stay, list) or len(stay) != 2 or
                    any(type(n) is not int for n in stay) or not 0 <= stay[0] <= stay[1]):
                raise ValueError("invalid stay")
    return variants


def _cents(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        raise ValueError("invalid price")
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0 or amount > Decimal(_MAX_MINOR) / 100:
        raise ValueError("price out of range")
    # Precision follows the input, so a tiny fractional cent cannot round away.
    with localcontext() as context:
        context.prec = max(28, len(amount.as_tuple().digits) + 2)
        minor = amount * 100
        if minor != minor.to_integral_value() or (amount != 0 and minor == 0):
            raise ValueError("fractional cent")
        return int(minor)


def _safe_link(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if any(c.isspace() for c in value):
        return False
    decoded = value
    for _ in range(5):
        if any(ord(c) < 32 or 127 <= ord(c) <= 159 or
               0xD800 <= ord(c) <= 0xDFFF or c == "\\" for c in decoded):
            return False
        newer = unquote(decoded)
        if newer == decoded:
            break
        decoded = newer
    else:
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme in {"http", "https"} and bool(parsed.hostname) and
                "%" not in parsed.netloc and
                parsed.username is None and parsed.password is None and
                (parsed.port is None or 0 < parsed.port <= 65535))
    except ValueError:
        return False


def _qualifying(row: dict[str, Any], variants: list[dict[str, Any]],
                currency: str, target: int, today: date) -> dict[str, Any] | None:
    try:
        total = row["price_total_minor"]
        if (type(total) is not int or not 0 <= total <= target or
                row["currency"] != currency or type(row["is_estimate"]) is not int or
                row["is_estimate"] != 0):
            return None
        dates = _json(row["dates"])
        legs = _json(row["detail"])
        if (not isinstance(dates, list) or not isinstance(legs, list) or
                not legs or len(dates) != len(legs) or any(not isinstance(l, dict) for l in legs)):
            return None
        days = [_date(d) for d in dates]
        if any(d < today for d in days):
            return None
        airports = [legs[0]["origin"]] + [leg["destination"] for leg in legs]
        route = "-".join(airports)
        matching = [s for s in variants if s["airports"] == airports and s["currency"] == currency]
        if not any(
            all(_date(s["window_start"]) <= d <= _date(s["window_end"]) for d in days) and
            all(a <= (days[i + 1] - days[i]).days <= b for i, (a, b) in enumerate(s["stays"]))
            for s in matching
        ):
            return None
        summed = 0
        for i, leg in enumerate(legs):
            if (leg.get("verified") is not True or leg.get("indicative") is not False or
                    leg["origin"] != airports[i] or leg["date"] != dates[i] or
                    not _safe_link(leg.get("deep_link")) or
                    ("currency" in leg and leg["currency"] != currency) or
                    ("route" in leg and leg["route"] != route)):
                return None
            if isinstance(leg["price"], bool) or not isinstance(leg["price"], (int, Decimal)):
                return None
            for key, value in leg.items():
                if ("estimat" in key or "inferred" in key) and value:
                    return None
                if any(part in key for part in ("fee", "addon", "add_on", "surcharge")):
                    if _cents(value) != 0:
                        return None
            minor = _cents(leg["price"])
            if "base_price" in leg and _cents(leg["base_price"]) != minor:
                return None
            summed += minor
        if summed != total:
            return None
        return {"price_minor": total, "currency": currency, "route": route,
                "dates": json.dumps(dates, separators=(",", ":")), "legs": row["detail"]}
    except (ValueError, TypeError, KeyError, DecimalException, OverflowError, RecursionError):
        return None


def evaluate_job(conn: sqlite3.Connection, job_id: int,
                 now: datetime | None = None) -> list[int]:
    """Record at most one cheapest qualifying event; never perform delivery."""
    current = now or datetime.now()
    ts = current.isoformat(timespec="seconds")
    ensure_schema(conn)
    with _atomic(conn):
        jobs = _rows(conn, "SELECT * FROM search_job WHERE id=? AND status='done'", (job_id,))
        if not jobs:
            return []
        marked = conn.execute(
            "INSERT OR IGNORE INTO price_target_evaluation(job_id,checked_at) VALUES(?,?)",
            (job_id, ts),
        )
        if marked.rowcount != 1:
            return []
        job = jobs[0]
        try:
            finished = job["finished_at"]
            if not isinstance(finished, str) or len(finished) < 19:
                return []
            age = current.timestamp() - datetime.fromisoformat(finished).timestamp()
            if not 0 <= age <= timedelta(hours=24).total_seconds() or job["profile_id"] is None:
                return []
            profiles = _rows(conn, "SELECT * FROM search_profile WHERE id=?", (job["profile_id"],))
            if not profiles:
                return []
            profile = profiles[0]
            target = profile.get("price_target_minor")
            if profile["enabled"] != 1 or type(target) is not int or not 0 < target <= _MAX_MINOR:
                return []
            profile_variants = _variants(profile["spec"])
            currencies = {s["currency"] for s in profile_variants}
            if len(currencies) != 1:
                return []
            currency = currencies.pop()
            variants = _variants(job["spec"])
            candidates = (
                _qualifying(row, variants, currency, target, current.date())
                for row in _rows(conn, "SELECT * FROM itinerary_result WHERE job_id=? "
                                "ORDER BY price_total_minor,id", (job_id,))
            )
            candidate = next((c for c in candidates if c is not None), None)
        except (ValueError, TypeError, KeyError, DecimalException, OverflowError, RecursionError, OSError):
            return []
        if candidate is None:
            return []
        last = conn.execute(
            "SELECT price_minor FROM price_target_event "
            "WHERE profile_id=? AND route=? AND dates=? AND currency=? ORDER BY id DESC LIMIT 1",
            (profile["id"], candidate["route"], candidate["dates"], currency),
        ).fetchone()
        if last is not None and (
            candidate["price_minor"] >= last[0] or candidate["price_minor"] * 100 > last[0] * 95
        ):
            return []
        cursor = conn.execute(
            "INSERT INTO price_target_event(profile_id,profile_name,job_id,price_minor,"
            "target_minor,currency,route,dates,legs,created_at,checked_at,delivery) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,'dry_run')",
            (profile["id"], profile["name"], job_id, candidate["price_minor"], target,
             currency, candidate["route"], candidate["dates"], candidate["legs"], ts, finished),
        )
        return [int(cursor.lastrowid)]


def evaluate_pending(conn: sqlite3.Connection, limit: int = 100,
                     now: datetime | None = None) -> list[int]:
    """Recover at most 500 recent done jobs, including unsuccessful evaluations."""
    bound = _limit(limit)
    current = now or datetime.now()
    ensure_schema(conn)
    # SQLite's utc modifier interprets naive engine timestamps as local time;
    # explicit offsets already describe an instant and remain UTC-normalized.
    upper = current.astimezone(timezone.utc).isoformat()
    lower = (current.astimezone(timezone.utc) - timedelta(hours=24)).isoformat()
    jobs = conn.execute(
        "SELECT j.id FROM search_job j LEFT JOIN price_target_evaluation e ON e.job_id=j.id "
        "WHERE j.status='done' AND j.profile_id IS NOT NULL AND e.job_id IS NULL "
        "AND julianday(j.finished_at,'utc') BETWEEN julianday(?) AND julianday(?) "
        "ORDER BY julianday(j.finished_at,'utc'),j.id LIMIT ?",
        (lower, upper, bound),
    ).fetchall()
    ids = []
    for row in jobs:
        ids.extend(evaluate_job(conn, row[0], now=current))
    return ids


def _snapshot(row: dict[str, Any]) -> dict[str, Any]:
    row["dates"] = json.loads(row["dates"])
    row["legs"] = json.loads(row["legs"])
    row["checked_at"] = datetime.fromisoformat(row["checked_at"]).astimezone(timezone.utc).isoformat()
    return row


def list_alerts(conn: sqlite3.Connection, limit: int = 50,
                only_open: bool = False) -> list[dict[str, Any]]:
    """Return immutable snapshots, newest first, with a bounded result count."""
    bound = _limit(limit)
    ensure_schema(conn)
    where = " WHERE acknowledged_at IS NULL" if only_open else ""
    return [_snapshot(row) for row in _rows(
        conn, f"SELECT {_COLUMNS} FROM price_target_event{where} ORDER BY id DESC LIMIT ?", (bound,)
    )]


def acknowledge(conn: sqlite3.Connection, alert_id: int,
                now: datetime | None = None) -> dict[str, Any]:
    """Acknowledge once and return the preserved snapshot; unknown IDs raise."""
    ensure_schema(conn)
    with _atomic(conn):
        conn.execute(
            "UPDATE price_target_event SET acknowledged_at=COALESCE(acknowledged_at,?) WHERE id=?",
            ((now or datetime.now()).isoformat(timespec="seconds"), alert_id),
        )
        rows = _rows(conn, f"SELECT {_COLUMNS} FROM price_target_event WHERE id=?", (alert_id,))
        if not rows:
            raise LookupError(f"unknown price target alert: {alert_id}")
        return _snapshot(rows[0])
