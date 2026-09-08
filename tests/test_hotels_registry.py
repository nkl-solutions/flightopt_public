"""Booking kommt nur mit Schalter und mit Playwright in den Katalog."""

from __future__ import annotations

from flightopt.hotels.registry import BOOKING_FLAG, build_hotel_sources, source_report
from flightopt.hotels.sources.booking import BookingSource


def names(env) -> list[str]:
    return [source.name for source in build_hotel_sources(env)]


def test_without_the_switch_only_trivago_is_in_the_catalogue():
    assert names({}) == ["trivago"]
    assert names({BOOKING_FLAG: "0"}) == ["trivago"]


def test_with_the_switch_and_playwright_booking_joins():
    usable, _ = BookingSource.availability()

    assert names({BOOKING_FLAG: "1"}) == (["trivago", "booking"] if usable else ["trivago"])


def test_a_missing_playwright_keeps_booking_out_instead_of_failing_mid_run(monkeypatch):
    monkeypatch.setattr(
        "flightopt.hotels.sources.booking.importlib.util.find_spec", lambda name: None
    )

    assert names({BOOKING_FLAG: "1"}) == ["trivago"]
    report = {entry["name"]: entry for entry in source_report({BOOKING_FLAG: "1"})}
    assert report["booking"]["active"] is False
    assert "Playwright" in report["booking"]["reason"]


def test_the_report_says_why_booking_is_absent():
    report = {entry["name"]: entry for entry in source_report({})}

    assert report["trivago"]["active"] is True
    assert report["booking"]["active"] is False
    assert BOOKING_FLAG in report["booking"]["reason"]
