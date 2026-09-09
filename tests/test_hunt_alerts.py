"""Der Fund, seine Entdopplung und der Weg nach Discord."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

import pytest

from flightopt.hunt import alerts, discord
from flightopt.storage import db

TRAVEL = date(2026, 11, 20)
NOW = datetime(2026, 9, 9, 12, 0, 0)


def a_find(**overrides) -> alerts.Find:
    base = {
        "entity_key": "BER|BKK",
        "travel_date": TRAVEL,
        "tier": "error",
        "price_minor": 3900,
        "currency": "EUR",
        "source": "ryanair",
        "median_minor": 50000,
        "n": 12,
        "population": "estimate",
        "reason": "unter 25 Prozent des Medians von 500,00 Euro (n=12)",
        "distance_km": 8622.0,
        "thin": False,
    }
    base.update(overrides)
    return alerts.Find(**base)


# -- Regeln -------------------------------------------------------------------


def test_without_any_rule_the_built_in_default_applies(tmp_path):
    """Eine Regel ist Feineinstellung und keine Voraussetzung.

    Waere sie eine, liefe ein frisch aufgesetzter Dienst still und niemand
    wuesste warum.
    """
    conn = db.connect(tmp_path / "rules.db")

    rule = alerts.matching_rule(conn, a_find())

    assert rule is not None
    assert rule.id is None
    assert rule.entity_key == "*"
    assert "error" in rule.tiers


def test_a_rule_can_narrow_the_hunt_to_one_route(tmp_path):
    conn = db.connect(tmp_path / "narrow.db")
    alerts.add_rule(conn, "BER|ATH", tiers=["error"], now=NOW)

    assert alerts.matching_rule(conn, a_find(entity_key="BER|ATH")) is not None
    assert alerts.matching_rule(conn, a_find(entity_key="BER|BKK")) is None


def test_a_switched_off_rule_does_not_match(tmp_path):
    conn = db.connect(tmp_path / "off.db")
    rule_id = alerts.add_rule(conn, "*", tiers=["error"], now=NOW)
    alerts.set_rule_active(conn, rule_id, False)

    assert alerts.matching_rule(conn, a_find()) is None


def test_a_tier_outside_the_rule_is_ignored(tmp_path):
    conn = db.connect(tmp_path / "tier.db")

    assert alerts.matching_rule(conn, a_find(tier="cheap")) is None


# -- Entdopplung --------------------------------------------------------------


def test_the_same_find_is_not_reported_twice_within_the_quiet_period(tmp_path):
    conn = db.connect(tmp_path / "quiet.db")
    find = a_find()
    alerts.record(conn, find, delivery=alerts.DRY_RUN, now=NOW)

    blocked, why = alerts.suppressed(conn, find, now=NOW + timedelta(minutes=20))

    assert blocked is True
    assert "Ruhezeit" in why


def test_after_the_quiet_period_the_same_find_speaks_again(tmp_path):
    conn = db.connect(tmp_path / "again.db")
    find = a_find()
    alerts.record(conn, find, delivery=alerts.DRY_RUN, now=NOW)

    later = NOW + alerts.QUIET + timedelta(minutes=1)

    assert alerts.suppressed(conn, find, now=later)[0] is False


def test_a_clearly_lower_price_is_a_new_find_not_a_repeat(tmp_path):
    """Von 39 auf 25 Euro ist eine Nachricht und keine Wiederholung."""
    conn = db.connect(tmp_path / "drop.db")
    alerts.record(conn, a_find(price_minor=3900), delivery=alerts.DRY_RUN, now=NOW)

    soon = NOW + timedelta(minutes=20)

    assert alerts.suppressed(conn, a_find(price_minor=2500), now=soon)[0] is False
    # Ein paar Cent weniger sind dagegen derselbe Fund.
    assert alerts.suppressed(conn, a_find(price_minor=3850), now=soon)[0] is True


def test_another_travel_day_is_another_find(tmp_path):
    conn = db.connect(tmp_path / "day.db")
    alerts.record(conn, a_find(), delivery=alerts.DRY_RUN, now=NOW)

    other = a_find(travel_date=TRAVEL + timedelta(days=1))

    assert alerts.suppressed(conn, other, now=NOW + timedelta(minutes=1))[0] is False


def test_a_suppressed_event_does_not_extend_the_quiet_period(tmp_path):
    """Sonst schoebe jede unterdrueckte Meldung die Ruhezeit vor sich her.

    Bei einem Takt von zwanzig Minuten und sechs Stunden Ruhe waere dieselbe
    Zeile damit nie wieder zu hoeren.
    """
    conn = db.connect(tmp_path / "creep.db")
    find = a_find()
    alerts.record(conn, find, delivery=alerts.DRY_RUN, now=NOW)
    alerts.record(conn, find, delivery=alerts.SUPPRESSED,
                  now=NOW + timedelta(hours=5))

    later = NOW + alerts.QUIET + timedelta(minutes=1)

    assert alerts.suppressed(conn, find, now=later)[0] is False


# -- Die Meldung selbst -------------------------------------------------------


def test_the_message_says_what_why_and_on_what_it_rests():
    text = alerts.message(a_find())

    assert "BER-BKK" in text
    assert "20.11.2026" in text
    assert "39,00 Euro" in text
    assert "25 Prozent des Medians" in text
    assert "n=12" in text
    assert "https://" in text


def test_the_message_names_a_calendar_price_as_such():
    """Ein Richtwert ist kein Tarif, und ein Kalenderpreis ist kein Angebot."""
    assert "Kalenderpreis" in alerts.message(a_find())
    assert "geprueft" in alerts.message(a_find(population="verified"))


def test_the_message_carries_no_arrows_and_no_dashes():
    text = alerts.message(a_find(median_minor=None, n=0, population="estimate"))

    for forbidden in ("→", "—", "–", "->"):
        assert forbidden not in text


def test_the_message_stays_inside_the_discord_limit():
    long_reason = "Grund " * 500
    text = alerts.message(a_find(reason=long_reason))

    assert len(text) <= discord.MAX_CONTENT


# -- Der Kanal ----------------------------------------------------------------


async def test_without_a_url_nothing_is_sent_but_everything_is_logged(caplog):
    caplog.set_level(logging.INFO, logger="flightopt.hunt.discord")

    result = await discord.send("Fehltarif BER-BKK", env={})

    assert result.dry_run is True
    assert result.delivered is False
    assert any("Fehltarif BER-BKK" in record.message for record in caplog.records)


async def test_a_configured_webhook_is_posted_to():
    seen: list = []

    def post(url, **kwargs):
        seen.append((url, kwargs))
        return FakeResponse(204)

    result = await discord.send(
        "Fehltarif", env={discord.ENV_WEBHOOK: "https://discord.test/hook"}, post=post
    )

    assert result.delivered is True
    assert seen[0][0] == "https://discord.test/hook"
    assert seen[0][1]["json"]["content"] == "Fehltarif"


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None,
                 headers: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


async def test_a_rate_limit_is_waited_out_and_retried():
    """Discord bremst mit 429 und sagt selbst, wie lange."""
    slept: list[float] = []
    answers = [FakeResponse(429, {"retry_after": 1.5}), FakeResponse(204)]

    async def sleep(seconds):
        slept.append(seconds)

    def post(url, **kwargs):
        return answers.pop(0)

    result = await discord.send(
        "Fehltarif", env={discord.ENV_WEBHOOK: "https://discord.test/hook"},
        post=post, sleep=sleep,
    )

    assert result.delivered is True
    assert slept == [1.5]
    assert result.attempts == 2


async def test_a_rate_limit_wait_is_capped():
    """Eine globale Sperre darf den Scan nicht anhalten."""
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)

    def post(url, **kwargs):
        return FakeResponse(429, {"retry_after": 9000})

    await discord.send(
        "Fehltarif", env={discord.ENV_WEBHOOK: "https://discord.test/hook"},
        post=post, sleep=sleep,
    )

    assert all(seconds <= discord.MAX_RETRY_WAIT for seconds in slept)


async def test_a_broken_channel_never_raises():
    def post(url, **kwargs):
        raise OSError("Netz weg")

    result = await discord.send(
        "Fehltarif", env={discord.ENV_WEBHOOK: "https://discord.test/hook"}, post=post
    )

    assert result.delivered is False
    assert result.dry_run is False
    assert "Netz weg" in (result.error or "")


async def test_a_rejected_webhook_is_reported_but_not_retried_forever():
    calls: list = []

    def post(url, **kwargs):
        calls.append(url)
        return FakeResponse(404, {"message": "Unknown Webhook"})

    result = await discord.send(
        "Fehltarif", env={discord.ENV_WEBHOOK: "https://discord.test/hook"}, post=post
    )

    assert result.delivered is False
    assert len(calls) == 1
    assert "404" in (result.error or "")


# -- Zusammenspiel ------------------------------------------------------------


async def test_a_dry_run_writes_the_event_and_says_it_was_dry(tmp_path):
    conn = db.connect(tmp_path / "dry.db")

    event_id = await alerts.deliver(conn, a_find(), env={}, now=NOW)

    row = alerts.get_event(conn, event_id)
    assert row["delivery"] == alerts.DRY_RUN
    assert row["entity_key"] == "BER|BKK"
    assert row["acknowledged_at"] is None


async def test_a_failing_channel_still_leaves_the_find_on_record(tmp_path):
    conn = db.connect(tmp_path / "fail.db")

    def post(url, **kwargs):
        raise OSError("Netz weg")

    event_id = await alerts.deliver(
        conn, a_find(), env={discord.ENV_WEBHOOK: "https://discord.test/x"},
        post=post, now=NOW,
    )

    row = alerts.get_event(conn, event_id)
    assert row["delivery"] == alerts.FAILED
    assert "Netz weg" in row["error"]


async def test_a_repeat_is_written_as_suppressed_and_not_sent(tmp_path):
    conn = db.connect(tmp_path / "rep.db")
    sent: list = []

    def post(url, **kwargs):
        sent.append(url)
        return FakeResponse(204)

    env = {discord.ENV_WEBHOOK: "https://discord.test/x"}
    await alerts.deliver(conn, a_find(), env=env, post=post, now=NOW)
    await alerts.deliver(conn, a_find(), env=env, post=post,
                         now=NOW + timedelta(minutes=20))

    assert len(sent) == 1
    deliveries = [row["delivery"] for row in alerts.list_events(conn)]
    assert sorted(deliveries) == [alerts.SENT, alerts.SUPPRESSED]


def test_a_find_can_be_switched_off_by_hand(tmp_path):
    conn = db.connect(tmp_path / "ack.db")
    event_id = alerts.record(conn, a_find(), delivery=alerts.DRY_RUN, now=NOW)

    row = alerts.acknowledge(conn, event_id, True, now=NOW)

    assert row["acknowledged_at"] == NOW.isoformat(timespec="seconds")
    assert alerts.acknowledge(conn, event_id, False)["acknowledged_at"] is None
    assert [r["id"] for r in alerts.list_events(conn, open_only=True)] == [event_id]


def test_switching_an_unknown_find_says_so(tmp_path):
    conn = db.connect(tmp_path / "nope.db")

    with pytest.raises(ValueError, match="Unbekannter Fund"):
        alerts.acknowledge(conn, 4711, True)


def test_the_summary_counts_what_the_channel_did(tmp_path):
    conn = db.connect(tmp_path / "sum.db")
    alerts.record(conn, a_find(), delivery=alerts.SENT, now=NOW)
    alerts.record(conn, a_find(travel_date=TRAVEL + timedelta(days=1)),
                  delivery=alerts.DRY_RUN, now=NOW)
    alerts.record(conn, a_find(travel_date=TRAVEL + timedelta(days=2)),
                  delivery=alerts.SUPPRESSED, now=NOW)

    summary = alerts.summary(conn, now=NOW)

    assert summary["events"] == 3
    assert summary["sent"] == 1
    assert summary["dry_run"] == 1
    assert summary["open"] == 3
    assert summary["quiet_hours"] == alerts.QUIET.total_seconds() / 3600
