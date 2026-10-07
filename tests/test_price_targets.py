"""Price targets only report complete, bookable profile itineraries."""

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from flightopt.domain.models import LegSpec, SearchSpec, StayRange
from flightopt.jobs.runner import specs_to_dict
from flightopt.storage import db


NOW = datetime(2027, 1, 1, 12)
DATES = ["2027-01-03", "2027-01-06"]


def spec(airports=("BER", "ATH", "BER"), currency="EUR"):
    return SearchSpec(
        legs=tuple(LegSpec(a, b) for a, b in zip(airports, airports[1:])),
        stays=(StayRange(2, 4),), window_start=date(2027, 1, 1),
        window_end=date(2027, 1, 10), currency=currency,
    )


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "targets.db")
    columns = {r["name"] for r in connection.execute("PRAGMA table_info(search_profile)")}
    if "price_target_minor" not in columns:
        connection.execute("ALTER TABLE search_profile ADD COLUMN price_target_minor INTEGER")
    yield connection
    connection.close()


def profile(conn, *, target=20000, enabled=1, specs=None):
    return conn.execute(
        "INSERT INTO search_profile(name,spec,enabled,created_at,next_run_at,price_target_minor) "
        "VALUES(?,?,?,?,?,?)",
        ("Athens", json.dumps(specs_to_dict(specs or [spec()])), enabled,
         NOW.isoformat(), NOW.isoformat(), target),
    ).lastrowid


def job(conn, profile_id, *, status="done", finished=None, specs=None):
    return conn.execute(
        "INSERT INTO search_job(profile_id,spec,status,created_at,finished_at) VALUES(?,?,?,?,?)",
        (profile_id, json.dumps(specs_to_dict(specs or [spec()])), status,
         (NOW - timedelta(hours=2)).isoformat(),
         (finished if finished is not None else NOW - timedelta(hours=1)).isoformat()),
    ).lastrowid


def legs(price=100.0, airports=("BER", "ATH", "BER"), dates=None):
    return [
        {"origin": a, "destination": b, "date": d, "price": price,
         "verified": True, "indicative": False, "source": "recorded",
         "deep_link": "https://booking.example.test/search?origin=" + a}
        for a, b, d in zip(airports, airports[1:], dates or DATES)
    ]


def result(conn, job_id, *, total=20000, rank=1, detail=None, dates=None,
           currency="EUR", estimate=0):
    return conn.execute(
        "INSERT INTO itinerary_result(job_id,rank,dates,price_total_minor,currency,is_estimate,detail) "
        "VALUES(?,?,?,?,?,?,?)",
        (job_id, rank, json.dumps(dates or DATES), total, currency, estimate,
         json.dumps(detail if detail is not None else legs(total / 200))),
    ).lastrowid


def module():
    from flightopt.jobs import price_targets
    return price_targets


def test_target_equality_snapshot_and_idempotent_acknowledgement(conn):
    pid = profile(conn)
    jid = job(conn, pid)
    result(conn, jid)
    pt = module()
    ids = pt.evaluate_job(conn, jid, now=NOW)
    assert len(ids) == 1
    alert = pt.list_alerts(conn)[0]
    assert alert == {
        "id": ids[0], "profile_id": pid, "profile_name": "Athens", "job_id": jid,
        "price_minor": 20000, "target_minor": 20000, "currency": "EUR",
        "route": "BER-ATH-BER", "dates": DATES, "legs": legs(),
        "created_at": NOW.isoformat(timespec="seconds"),
        "checked_at": (NOW - timedelta(hours=1)).astimezone(timezone.utc).isoformat(),
        "acknowledged_at": None, "delivery": "dry_run",
    }
    first = pt.acknowledge(conn, ids[0], now=NOW + timedelta(minutes=1))
    assert first["acknowledged_at"] == "2027-01-01T12:01:00"
    assert pt.acknowledge(conn, ids[0], now=NOW + timedelta(hours=1)) == first
    assert pt.list_alerts(conn, only_open=True) == []
    assert pt.list_alerts(conn) == [first]
    with pytest.raises(LookupError):
        pt.acknowledge(conn, 999999, now=NOW)


def test_selects_cheapest_qualifying_result_below_estimated_rank_one(conn):
    jid = job(conn, profile(conn))
    result(conn, jid, total=1000, rank=1, estimate=1)
    result(conn, jid, total=19000, rank=2)
    result(conn, jid, total=18000, rank=3)
    result(conn, jid, total=17000, rank=4, detail=legs(85))
    detail = legs(85)
    detail[1]["indicative"] = True
    conn.execute("UPDATE itinerary_result SET detail=? WHERE job_id=? AND rank=4",
                 (json.dumps(detail), jid))
    assert len(module().evaluate_job(conn, jid, now=NOW)) == 1
    assert module().list_alerts(conn)[0]["price_minor"] == 18000


@pytest.mark.parametrize("target,enabled", [(None, 1), (0, 1), (-1, 1), (20000, 0)])
def test_missing_removed_or_paused_target_does_not_alert(conn, target, enabled):
    jid = job(conn, profile(conn, target=target, enabled=enabled))
    result(conn, jid)
    assert module().evaluate_job(conn, jid, now=NOW) == []


@pytest.mark.parametrize("status", ["pending", "running", "failed", "cancelled"])
def test_only_done_jobs_can_alert(conn, status):
    jid = job(conn, profile(conn), status=status)
    result(conn, jid)
    assert module().evaluate_job(conn, jid, now=NOW) == []
    if status in {"pending", "running"}:
        conn.execute("UPDATE search_job SET status='done' WHERE id=?", (jid,))
        assert len(module().evaluate_job(conn, jid, now=NOW)) == 1


@pytest.mark.parametrize("age,allowed", [(timedelta(hours=24), True),
    (timedelta(hours=24, seconds=1), False), (timedelta(seconds=-1), False),
    (timedelta(0), True)])
def test_finished_timestamp_boundaries(conn, age, allowed):
    jid = job(conn, profile(conn), finished=NOW - age)
    result(conn, jid)
    assert bool(module().evaluate_job(conn, jid, now=NOW)) is allowed


def test_manual_unknown_or_deleted_profile_jobs_do_not_alert(conn):
    pt = module()
    jid = job(conn, None)
    result(conn, jid)
    assert pt.evaluate_job(conn, jid, now=NOW) == []
    assert pt.evaluate_job(conn, 999999, now=NOW) == []
    pid = profile(conn)
    jid = job(conn, pid)
    result(conn, jid)
    conn.execute("DELETE FROM search_profile WHERE id=?", (pid,))
    assert pt.evaluate_job(conn, jid, now=NOW) == []


@pytest.mark.parametrize("field,value", [
    ("verified", False), ("verified", 1), ("verified", "true"), ("verified", None),
    ("indicative", True), ("indicative", 0), ("indicative", None),
    ("is_estimate", True), ("estimate", "yes"), ("estimated", 1),
    ("bag_fee", 1), ("bag_fee", "NaN"), ("bag_fee", -1),
    ("addon_fee", 5), ("inferred_addon", True), ("other_fee", 1),
    ("price", -1), ("price", "NaN"), ("price", "Infinity"),
    ("price", True), ("price", "100.001"), ("price", "1e1000000"),
    ("price", "100.00000000000000000000000000001"),
    ("currency", "USD"), ("date", "2027-01-04"), ("origin", "MUC"),
    ("destination", "MUC"), ("route", "MUC-ATH-MUC"),
])
def test_each_leg_must_pass_quality_money_and_route_gates(conn, field, value):
    detail = legs()
    detail[1][field] = value
    jid = job(conn, profile(conn))
    result(conn, jid, detail=detail)
    assert module().evaluate_job(conn, jid, now=NOW) == []


@pytest.mark.parametrize("field", ["verified", "indicative", "price", "date", "origin",
                                        "destination", "deep_link"])
def test_missing_required_leg_fields_fail_closed(conn, field):
    detail = legs()
    del detail[0][field]
    jid = job(conn, profile(conn))
    result(conn, jid, detail=detail)
    assert module().evaluate_job(conn, jid, now=NOW) == []


@pytest.mark.parametrize("link", [None, "", "/search", "//booking.example.test/search",
    "javascript:alert(1)", "ftp://booking.example.test", "https://user:pass@example.test",
    "https://example.test\\@evil.test", "https://example.test/\nsearch",
    "https://example.test/\tsearch", "https://example.test:bad/search", "https://",
    "https://example.test/%0asearch", "https://example.test/%5csearch",
    "https://example.test/ search"])
def test_unsafe_or_missing_deep_links_fail_closed(conn, link):
    detail = legs()
    detail[0]["deep_link"] = link
    jid = job(conn, profile(conn))
    result(conn, jid, detail=detail)
    assert module().evaluate_job(conn, jid, now=NOW) == []


@pytest.mark.parametrize("detail", [None, {}, [], [None], [{"verified": True}], "garbage"])
def test_malformed_leg_payloads_fail_closed(conn, detail):
    jid = job(conn, profile(conn))
    result(conn, jid)
    conn.execute("UPDATE itinerary_result SET detail=? WHERE job_id=?", (json.dumps(detail), jid))
    assert module().evaluate_job(conn, jid, now=NOW) == []


@pytest.mark.parametrize("table,field,value", [
    ("search_job", "spec", "{"), ("search_job", "spec", "null"),
    ("search_profile", "spec", "[]"), ("search_job", "finished_at", "yesterday"),
    ("search_job", "finished_at", None), ("itinerary_result", "dates", "[null]"),
    ("itinerary_result", "dates", "[]"), ("itinerary_result", "dates", "[\"bad\"]"),
    ("itinerary_result", "detail", "{"), ("itinerary_result", "price_total_minor", -1),
    ("itinerary_result", "price_total_minor", 20001),
    ("itinerary_result", "price_total_minor", 19999),
    ("itinerary_result", "currency", "USD"), ("itinerary_result", "is_estimate", 1),
])
def test_malformed_stored_metadata_fails_closed(conn, table, field, value):
    pid = profile(conn)
    jid = job(conn, pid)
    result(conn, jid)
    key, identity = ("id", pid) if table == "search_profile" else (
        ("id", jid) if table == "search_job" else ("job_id", jid))
    conn.execute(f"UPDATE {table} SET {field}=? WHERE {key}=?", (value, identity))
    assert module().evaluate_job(conn, jid, now=NOW) == []


@pytest.mark.parametrize("dates", [["2026-12-31", "2027-01-03"],
    ["2027-01-06", "2027-01-03"], ["2027-01-03", "2027-01-10"],
    ["2027-01-09", "2027-01-12"], ["2027-01-03"]])
def test_dates_must_be_future_in_window_and_match_stays_and_leg_count(conn, dates):
    jid = job(conn, profile(conn))
    result(conn, jid, dates=dates, detail=legs(dates=dates))
    assert module().evaluate_job(conn, jid, now=NOW) == []


def test_variant_job_validates_the_matching_concrete_route_and_currency(conn):
    alternatives = [spec(), spec(("MUC", "ATH", "MUC"))]
    pid = profile(conn, specs=alternatives)
    jid = job(conn, pid, specs=alternatives)
    result(conn, jid, detail=legs(airports=("MUC", "ATH", "MUC")))
    assert len(module().evaluate_job(conn, jid, now=NOW)) == 1
    assert module().list_alerts(conn)[0]["route"] == "MUC-ATH-MUC"
    jid = job(conn, pid, specs=[spec(currency="USD")])
    result(conn, jid)
    assert module().evaluate_job(conn, jid, now=NOW) == []


def test_contiguous_route_must_exist_in_job_spec(conn):
    jid = job(conn, profile(conn))
    result(conn, jid, detail=legs(airports=("MUC", "ATH", "MUC")))
    assert module().evaluate_job(conn, jid, now=NOW) == []


def test_decimal_price_sum_is_exact_and_zero_fee_and_today_are_allowed(conn):
    jid = job(conn, profile(conn, target=30))
    detail = legs(dates=["2027-01-01", "2027-01-04"])
    detail[0].update(price=0.1, bag_fee=0)
    detail[1].update(price=0.2, is_estimate=False)
    result(conn, jid, total=30, detail=detail, dates=["2027-01-01", "2027-01-04"])
    assert len(module().evaluate_job(conn, jid, now=NOW)) == 1


def test_restart_and_persistent_five_percent_deduplication(conn):
    pt = module()
    pid = profile(conn)
    first = job(conn, pid)
    result(conn, first)
    assert len(pt.evaluate_job(conn, first, now=NOW)) == 1
    path = conn.execute("PRAGMA database_list").fetchone()["file"]
    with sqlite3.connect(path) as restarted:
        restarted.row_factory = sqlite3.Row
        assert pt.evaluate_job(restarted, first, now=NOW) == []
        assert pt.evaluate_pending(restarted, now=NOW) == []
    for amount, expected in [(20000, False), (21000, False), (19001, False),
                             (19000, True), (18051, False), (18050, True)]:
        jid = job(conn, pid)
        result(conn, jid, total=amount)
        assert bool(pt.evaluate_job(conn, jid, now=NOW)) is expected
    assert [a["price_minor"] for a in pt.list_alerts(conn)] == [18050, 19000, 20000]


def test_deduplication_keeps_identical_profiles_and_different_dates_separate(conn):
    pt = module()
    pids = [profile(conn), profile(conn)]
    for pid in pids:
        jid = job(conn, pid)
        result(conn, jid)
        assert len(pt.evaluate_job(conn, jid, now=NOW)) == 1
    jid = job(conn, pids[0])
    dates = ["2027-01-04", "2027-01-07"]
    result(conn, jid, dates=dates, detail=legs(dates=dates))
    assert len(pt.evaluate_job(conn, jid, now=NOW)) == 1


def test_target_name_edits_and_profile_deletion_preserve_event_snapshot(conn):
    pt = module()
    pid = profile(conn)
    jid = job(conn, pid)
    result(conn, jid)
    pt.evaluate_job(conn, jid, now=NOW)
    snapshot = pt.list_alerts(conn)
    conn.execute("UPDATE search_profile SET name='Changed',price_target_minor=NULL WHERE id=?", (pid,))
    assert pt.list_alerts(conn) == snapshot
    conn.execute("DELETE FROM search_profile WHERE id=?", (pid,))
    conn.execute("DELETE FROM search_job WHERE id=?", (jid,))
    assert pt.list_alerts(conn) == snapshot


def test_pending_recovery_is_bounded_and_marks_nonqualifying_done_jobs(conn):
    pt = module()
    pid = profile(conn)
    jobs = [job(conn, pid) for _ in range(4)]
    for jid in jobs:
        result(conn, jid, estimate=1)
    extra = job(conn, profile(conn))
    result(conn, extra)
    assert pt.evaluate_pending(conn, limit=2, now=NOW) == []
    assert pt.evaluate_pending(conn, limit=2, now=NOW) == []
    assert len(pt.evaluate_pending(conn, limit=2, now=NOW)) == 1
    assert pt.evaluate_pending(conn, now=NOW) == []


def test_pending_filters_old_future_manual_and_unfinished_jobs(conn):
    pt = module()
    pid = profile(conn)
    for finished, status, linked in [
        (NOW - timedelta(days=2), "done", pid),
        (NOW + timedelta(seconds=1), "done", pid),
        (NOW, "done", None), (NOW, "running", pid),
    ]:
        jid = job(conn, linked, finished=finished, status=status)
        result(conn, jid)
    jid = job(conn, pid)
    result(conn, jid)
    assert len(pt.evaluate_pending(conn, limit=1, now=NOW)) == 1


def test_additive_schema_preserves_global_alerts_and_caller_transaction(conn):
    pt = module()
    conn.execute("BEGIN")
    conn.execute("INSERT INTO alert_event(created_at,entity_key,travel_date,tier,price_minor) "
                 "VALUES('now','BER|ATH','2027-01-03','error',100)")
    pt.ensure_schema(conn)
    pt.ensure_schema(conn)
    assert conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM alert_event").fetchone()[0] == 1
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM alert_event").fetchone()[0] == 0
    pt.ensure_schema(conn)


def test_event_and_evaluation_marker_roll_back_together_on_error(conn):
    pt = module()
    pt.ensure_schema(conn)
    pid = profile(conn)
    jid = job(conn, pid)
    result(conn, jid)
    conn.execute("CREATE TRIGGER fail_price_target BEFORE INSERT ON price_target_event "
                 "BEGIN SELECT RAISE(ABORT, 'interrupted'); END")
    with pytest.raises(sqlite3.IntegrityError, match="interrupted"):
        pt.evaluate_job(conn, jid, now=NOW)
    assert pt.list_alerts(conn) == []
    conn.execute("DROP TRIGGER fail_price_target")
    assert len(pt.evaluate_job(conn, jid, now=NOW)) == 1


def test_outer_rollback_allows_recovery_and_does_not_commit_other_writes(conn):
    pt = module()
    pid = profile(conn)
    jid = job(conn, pid)
    result(conn, jid)
    conn.execute("BEGIN")
    conn.execute("UPDATE search_profile SET name='Pending edit' WHERE id=?", (pid,))
    assert len(pt.evaluate_job(conn, jid, now=NOW)) == 1
    assert conn.in_transaction
    conn.rollback()
    assert pt.list_alerts(conn) == []
    assert conn.execute("SELECT name FROM search_profile WHERE id=?", (pid,)).fetchone()[0] == "Athens"
    assert len(pt.evaluate_pending(conn, now=NOW)) == 1


def test_limits_do_not_expand_negative_values_or_allow_unbounded_reads(conn):
    pt = module()
    pid = profile(conn)
    jid = job(conn, pid)
    result(conn, jid)
    assert pt.evaluate_pending(conn, limit=0, now=NOW) == []
    assert pt.evaluate_pending(conn, limit=-1, now=NOW) == []
    assert len(pt.evaluate_pending(conn, limit=1, now=NOW)) == 1
    assert pt.list_alerts(conn, limit=0) == []
    assert pt.list_alerts(conn, limit=-1) == []
    assert len(pt.list_alerts(conn, limit=1)) == 1


def test_aware_finished_timestamp_is_compared_as_an_instant(conn):
    current = NOW.replace(tzinfo=timezone.utc)
    jid = job(conn, profile(conn), finished=current.astimezone(timezone(timedelta(hours=2))))
    result(conn, jid)
    assert len(module().evaluate_pending(conn, now=current)) == 1


@pytest.mark.parametrize("offset", [None, 2, -8])
def test_snapshot_checked_at_is_utc_and_preserves_finished_instant(conn, offset):
    finished = NOW if offset is None else NOW.replace(
        tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=offset)))
    current = finished + timedelta(hours=1)
    jid = job(conn, profile(conn), finished=finished)
    result(conn, jid)
    pt = module()
    assert len(pt.evaluate_job(conn, jid, now=current)) == 1
    snapshot = pt.list_alerts(conn)[0]
    expected = datetime.fromtimestamp(finished.timestamp(), timezone.utc)
    assert snapshot["checked_at"] == expected.isoformat()
    assert snapshot["checked_at"].endswith("+00:00")
    assert current.astimezone(timezone.utc) - datetime.fromisoformat(
        snapshot["checked_at"]) == timedelta(hours=1)
    assert snapshot["created_at"] == current.isoformat(timespec="seconds")
    assert pt.acknowledge(conn, snapshot["id"], now=current)["checked_at"] == expected.isoformat()
    stored = conn.execute(
        "SELECT checked_at,created_at FROM price_target_event WHERE id=?", (snapshot["id"],)
    ).fetchone()
    assert tuple(stored) == (finished.isoformat(), current.isoformat(timespec="seconds"))
    assert conn.execute("SELECT finished_at FROM search_job WHERE id=?", (jid,)).fetchone()[0] == (
        finished.isoformat())


@pytest.mark.parametrize("offset", [None, 2, -8])
def test_legacy_checked_at_is_utc_and_stays_stale_in_another_timezone(conn, offset):
    finished = NOW if offset is None else NOW.replace(
        tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=offset)))
    pt = module()
    pt.ensure_schema(conn)
    alert_id = conn.execute(
        "INSERT INTO price_target_event(profile_id,profile_name,job_id,price_minor,"
        "target_minor,currency,route,dates,legs,created_at,checked_at) "
        "VALUES(1,'Athens',1,100,200,'EUR','BER-ATH-BER','[]','[]',?,?)",
        (NOW.isoformat(), finished.isoformat()),
    ).lastrowid
    expected = datetime.fromtimestamp(finished.timestamp(), timezone.utc)
    browser_now = (expected + timedelta(hours=25)).astimezone(timezone(timedelta(hours=-8)))
    snapshot = pt.list_alerts(conn)[0]
    assert snapshot["checked_at"] == expected.isoformat()
    assert browser_now - datetime.fromisoformat(snapshot["checked_at"]) == timedelta(hours=25)
    assert snapshot["created_at"] == NOW.isoformat()
    acknowledged = pt.acknowledge(conn, alert_id, now=browser_now)
    assert acknowledged["checked_at"] == snapshot["checked_at"]
    assert acknowledged["created_at"] == snapshot["created_at"]
    stored = conn.execute(
        "SELECT checked_at,created_at FROM price_target_event WHERE id=?", (alert_id,)
    ).fetchone()
    assert tuple(stored) == (finished.isoformat(), NOW.isoformat())


def test_price_must_be_numeric_not_a_string(conn):
    jid = job(conn, profile(conn))
    detail = legs()
    detail[0]["price"] = "100.00"
    result(conn, jid, detail=detail)
    assert module().evaluate_job(conn, jid, now=NOW) == []


def test_ambiguous_duplicate_quality_keys_fail_closed(conn):
    jid = job(conn, profile(conn))
    result(conn, jid)
    ambiguous = json.dumps(legs()).replace('"verified": true', '"verified": false, "verified": true', 1)
    conn.execute("UPDATE itinerary_result SET detail=? WHERE job_id=?", (ambiguous, jid))
    assert module().evaluate_job(conn, jid, now=NOW) == []


def test_variant_count_is_optional_but_if_present_must_be_consistent(conn):
    alternatives = [spec(), spec(("MUC", "ATH", "MUC"))]
    pid = profile(conn, specs=alternatives)
    jid = job(conn, pid, specs=alternatives)
    data = specs_to_dict(alternatives)
    del data["variant_count"]
    conn.execute("UPDATE search_job SET spec=? WHERE id=?", (json.dumps(data), jid))
    result(conn, jid)
    assert len(module().evaluate_job(conn, jid, now=NOW)) == 1
    jid = job(conn, pid, specs=alternatives)
    data["variant_count"] = 5
    conn.execute("UPDATE search_job SET spec=? WHERE id=?", (json.dumps(data), jid))
    result(conn, jid)
    assert module().evaluate_job(conn, jid, now=NOW) == []


def test_recovery_hard_limit_leaves_more_than_500_jobs_for_next_call(conn):
    pid = profile(conn)
    for _ in range(500):
        job(conn, pid)
    jid = job(conn, pid)
    result(conn, jid)
    pt = module()
    assert pt.evaluate_pending(conn, limit=10**9, now=NOW) == []
    assert len(pt.evaluate_pending(conn, limit=1, now=NOW)) == 1


def test_list_hard_limit_and_ordering_with_plain_sqlite_connection(conn):
    pt = module()
    pt.ensure_schema(conn)
    for i in range(501):
        conn.execute(
            "INSERT INTO price_target_event(profile_id,profile_name,job_id,price_minor,"
            "target_minor,currency,route,dates,legs,created_at,checked_at) "
            "VALUES(1,'Athens',?,100,200,'EUR','BER-ATH-BER','[]','[]',?,?)",
            (i, NOW.isoformat(), NOW.isoformat()),
        )
    path = conn.execute("PRAGMA database_list").fetchone()["file"]
    with sqlite3.connect(path) as plain:
        alerts = pt.list_alerts(plain, limit=10**9)
        assert len(alerts) == 500
        assert alerts[0]["job_id"] == 500
        assert alerts[-1]["job_id"] == 1


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
def test_nonstandard_nonfinite_json_price_fails_closed(conn, raw):
    jid = job(conn, profile(conn))
    result(conn, jid)
    detail = json.dumps(legs()).replace('"price": 100.0', '"price": ' + raw, 1)
    conn.execute("UPDATE itinerary_result SET detail=? WHERE job_id=?", (detail, jid))
    assert module().evaluate_job(conn, jid, now=NOW) == []


@pytest.mark.parametrize("link", ["https://booking.example.test/%25250asearch",
    "https://user%3apass%40booking.example.test/search",
    "https://booking.example.test/\ud800search"])
def test_encoded_controls_credentials_and_invalid_unicode_links_fail_closed(conn, link):
    jid = job(conn, profile(conn))
    detail = legs()
    detail[0]["deep_link"] = link
    result(conn, jid, detail=detail)
    assert module().evaluate_job(conn, jid, now=NOW) == []


@pytest.mark.parametrize("link", ["http://booking.example.test/search?city=Athens%20Center",
                                 "https://booking.example.test/search?return=%2Fflights"])
def test_safe_encoded_query_links_are_preserved(conn, link):
    jid = job(conn, profile(conn))
    detail = legs()
    detail[0]["deep_link"] = link
    result(conn, jid, detail=detail)
    assert len(module().evaluate_job(conn, jid, now=NOW)) == 1
    assert module().list_alerts(conn)[0]["legs"][0]["deep_link"] == link


def test_timestamp_outside_platform_epoch_range_fails_closed(conn):
    jid = job(conn, profile(conn))
    result(conn, jid)
    conn.execute("UPDATE search_job SET finished_at='0001-01-01T00:00:00' WHERE id=?", (jid,))
    assert module().evaluate_job(conn, jid, now=NOW) == []


def test_restart_recovery_keeps_23_hour_old_price_timestamp_for_ui_freshness(conn):
    finished = NOW - timedelta(hours=23)
    jid = job(conn, profile(conn), finished=finished)
    result(conn, jid)
    pt = module()
    assert len(pt.evaluate_pending(conn, now=NOW)) == 1
    snapshot = pt.list_alerts(conn)[0]
    assert snapshot["created_at"] == "2027-01-01T12:00:00"
    assert snapshot["checked_at"] == finished.astimezone(timezone.utc).isoformat()
    assert NOW.astimezone(timezone.utc) - datetime.fromisoformat(
        snapshot["checked_at"]) == timedelta(hours=23)
    assert pt.evaluate_pending(conn, now=NOW + timedelta(minutes=30)) == []
    assert pt.list_alerts(conn) == [snapshot]
    acknowledged = pt.acknowledge(conn, snapshot["id"], now=NOW + timedelta(minutes=30))
    assert acknowledged["checked_at"] == snapshot["checked_at"]
    assert acknowledged["created_at"] == snapshot["created_at"]
