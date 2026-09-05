"""Offline tests for the Google consent handling.

The live path cannot be exercised in CI (and hammering Google is what caused
the P0 block), so the parsing is pinned against a captured consent page.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from flightopt.sources.google_consent import (
    ConsentError,
    _parse_reject_form,
    is_blocked,
    is_consent_wall,
)

FIXTURE = Path(__file__).parent / "fixtures" / "google_consent_wall.html"


@pytest.fixture(scope="module")
def consent_html() -> str:
    if not FIXTURE.exists():
        pytest.skip("consent fixture not captured")
    return FIXTURE.read_text(encoding="utf-8")


def test_detects_consent_wall(consent_html):
    assert is_consent_wall(consent_html)


def test_normal_page_is_not_a_consent_wall():
    assert not is_consent_wall("<html><body>flight results</body></html>")


def test_reject_form_is_the_one_with_set_eom(consent_html):
    fields = _parse_reject_form(consent_html)
    # set_eom=true is the reject-all variant; without it we would be accepting.
    assert fields["set_eom"] == "true"
    assert fields["continue"].startswith("https://www.google.com/")
    assert "bl" in fields and "escs" in fields


def test_reject_form_unescapes_continue_url(consent_html):
    fields = _parse_reject_form(consent_html)
    assert "&amp;" not in fields["continue"]


def test_missing_reject_form_raises():
    html = (
        '<form action="https://consent.google.com/save">'
        '<input name="gl" value="DE"><input name="app" value="0"></form>'
    )
    with pytest.raises(ConsentError, match="no reject-all form"):
        _parse_reject_form(html)


def test_never_selects_the_accept_form(consent_html):
    # The live page carries both buttons; picking the wrong one would opt the
    # user into tracking cookies, so this is the test that matters most.
    fields = _parse_reject_form(consent_html)
    assert fields.get("set_sc") is None
    assert fields.get("set_aps") is None
    assert fields["set_eom"] == "true"


def test_accept_shaped_form_is_rejected():
    html = """
    <form action="https://consent.google.com/save">
      <input name="continue" value="https://x/"><input name="set_eom" value="false">
      <input name="set_sc" value="true"><input name="set_aps" value="true">
    </form>
    """
    with pytest.raises(ConsentError):
        _parse_reject_form(html)


def test_form_with_extra_opt_in_flag_is_rejected():
    # set_eom=true alone is not enough if the page also opts into a category.
    html = """
    <form action="https://consent.google.com/save">
      <input name="continue" value="https://x/"><input name="set_eom" value="true">
      <input name="set_ytc" value="true">
    </form>
    """
    with pytest.raises(ConsentError):
        _parse_reject_form(html)


@pytest.mark.parametrize(
    "html,status,expected",
    [
        ("", 429, True),
        ("Our systems have detected unusual traffic", 200, True),
        ("<a href='/sorry/index?continue=x'>", 200, True),
        ("normal page", 200, False),
    ],
)
def test_block_detection(html, status, expected):
    assert is_blocked(html, status) is expected
