"""Get past Google's EU consent interstitial by rejecting non-essential cookies.

From a German IP, Google serves a consent wall instead of flight data, which is
why both Google Flights libraries return empty results here. The wall offers
"Reject all" and "Accept all"; this module always submits **Reject all**
(`set_eom=true`), which is the privacy-preserving choice and is enough to reach
the page. Nothing is accepted on the user's behalf.

The resulting SOCS cookie is cached on disk so one submission covers many runs;
hard-coding a scraped cookie value from elsewhere is what triggered a 429
during the P0 spike, so the cookie is always obtained fresh from the live form.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONSENT_SAVE = "https://consent.google.com/save"
PROBE_URL = "https://www.google.com/travel/flights?hl=en-US&curr=EUR"
CACHE_PATH = Path("data/google_consent.json")
CONSENT_TTL = timedelta(days=14)

_CONSENT_MARKERS = ("consent.google.com/save", "before you continue")
_BLOCK_MARKERS = ("unusual traffic", "/sorry/index", "recaptcha")


class ConsentError(RuntimeError):
    pass


class GoogleBlocked(RuntimeError):
    """Google is rate limiting this IP. Back off; do not retry in a loop."""


def is_consent_wall(html: str) -> bool:
    low = html.lower()
    return any(m in low for m in _CONSENT_MARKERS)


def is_blocked(html: str, status: int) -> bool:
    if status == 429:
        return True
    low = html.lower()
    return any(m in low for m in _BLOCK_MARKERS)


def _parse_reject_form(html: str) -> dict[str, str]:
    """Extract the hidden fields of the 'Reject all' form.

    Both buttons post to the same endpoint. Observed on the live page:
        Reject all -> set_eom=true,  no set_sc / set_aps
        Accept all -> set_eom=false, set_sc=true, set_aps=true
    Both conditions are checked, so a page change that flips one flag makes the
    parser fail loudly rather than silently submit the accept variant.
    """
    forms = re.findall(
        r'<form[^>]*action="https://consent\.google\.com/save".*?</form>',
        html,
        flags=re.S,
    )
    for form in forms:
        fields = {
            name: value
            for name, value in re.findall(
                r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', form
            )
        }
        opts_in = {k for k in fields if k.startswith("set_") and k != "set_eom"}
        if fields.get("set_eom") == "true" and not opts_in:
            return {k: v.replace("&amp;", "&") for k, v in fields.items()}
    raise ConsentError(
        f"no reject-all form found on the consent page ({len(forms)} forms seen)"
    )


def _load_cached() -> dict[str, str] | None:
    if not CACHE_PATH.exists():
        return None
    try:
        blob = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        saved = datetime.fromisoformat(blob["saved_at"])
    except Exception:
        return None
    if datetime.now() - saved > CONSENT_TTL:
        return None
    cookies = blob.get("cookies") or {}
    return cookies or None


def _store(cookies: dict[str, str]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(
        json.dumps({"saved_at": datetime.now().isoformat(), "cookies": cookies}),
        encoding="utf-8",
    )


def acquire_consent_cookies(*, force: bool = False, timeout: int = 30) -> dict[str, str]:
    """Return cookies that let Google serve content instead of the consent wall."""
    if not force:
        cached = _load_cached()
        if cached:
            logger.debug("using cached Google consent cookies")
            return cached

    from curl_cffi import requests as creq

    session = creq.Session(impersonate="chrome")
    resp = session.get(PROBE_URL, timeout=timeout)

    if is_blocked(resp.text, resp.status_code):
        raise GoogleBlocked(f"blocked before consent (HTTP {resp.status_code})")

    if not is_consent_wall(resp.text):
        cookies = dict(session.cookies)
        _store(cookies)
        logger.info("no consent wall shown; cookies captured")
        return cookies

    fields = _parse_reject_form(resp.text)
    logger.info("submitting Google consent form: reject non-essential cookies")

    saved = session.post(
        CONSENT_SAVE,
        data=fields,
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://consent.google.com",
            "referer": "https://consent.google.com/",
        },
        timeout=timeout,
        allow_redirects=True,
    )
    if is_blocked(saved.text, saved.status_code):
        raise GoogleBlocked(f"blocked while saving consent (HTTP {saved.status_code})")

    cookies = dict(session.cookies)
    if "SOCS" not in cookies:
        raise ConsentError(f"consent save did not set SOCS (got {sorted(cookies)})")

    _store(cookies)
    logger.info("consent stored, cookies: %s", sorted(cookies))
    return cookies


def verify(cookies: dict[str, str], *, timeout: int = 30) -> tuple[bool, str]:
    """Check whether these cookies actually get real content."""
    from curl_cffi import requests as creq

    resp = creq.get(PROBE_URL, impersonate="chrome", cookies=cookies, timeout=timeout)
    if is_blocked(resp.text, resp.status_code):
        return False, f"blocked (HTTP {resp.status_code})"
    if is_consent_wall(resp.text):
        return False, "still showing consent wall"
    return True, f"ok (HTTP {resp.status_code}, {len(resp.text)} bytes)"
