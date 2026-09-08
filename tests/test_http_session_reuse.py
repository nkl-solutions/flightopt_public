"""Eine Quelle haelt genau eine HTTP-Verbindung offen.

Vorher rief `fetch_json` ohne uebergebene Session die Modulfunktionen
`creq.get`/`creq.post` auf: neue Verbindung, neuer TLS-Handshake, jedes Mal.
Bei sechzig Bein-Datum-Fragen an dieselbe Airline sind das sechzig
Handshakes fuer eine Strecke, die eine einzige Verbindung bedient haette.
"""

from __future__ import annotations

from typing import Any

import pytest

from flightopt.sources.base import HttpSource, SourceError


class FakeResponse:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status
        self.text = "fake"

    def json(self) -> Any:
        return self._payload


class FakeSession:
    """Zaehlt, was ueber sie lief, und ob sie geschlossen wurde."""

    def __init__(self, impersonate: str | None = None, proxy: str | None = None) -> None:
        self.impersonate = impersonate
        self.proxy = proxy
        self.gets: list[str] = []
        self.posts: list[str] = []
        self.closed = False

    def get(self, url: str, **kw: Any) -> FakeResponse:
        self.gets.append(url)
        return FakeResponse({"url": url, "kind": "get"})

    def post(self, url: str, **kw: Any) -> FakeResponse:
        self.posts.append(url)
        return FakeResponse({"url": url, "kind": "post"})

    def close(self) -> None:
        self.closed = True


class Demo(HttpSource):
    name = "demo"
    per_minute = 6000
    impersonate = "chrome131"


@pytest.fixture
def src(monkeypatch) -> Demo:
    source = Demo()
    monkeypatch.setattr(source.limiter, "wait", _no_wait)
    return source


async def _no_wait() -> None:
    return None


async def test_two_calls_share_one_session(src, monkeypatch):
    built: list[FakeSession] = []

    def factory() -> FakeSession:
        session = FakeSession(src.impersonate, src.proxy)
        built.append(session)
        return session

    monkeypatch.setattr(src, "_new_session", factory)

    first = await src.fetch_json("https://example.invalid/one")
    second = await src.fetch_json("https://example.invalid/two", json_body={"a": 1})

    assert first["url"].endswith("/one")
    assert second["kind"] == "post"
    assert len(built) == 1, "die Session wurde ein zweites Mal aufgebaut"
    assert built[0].gets == ["https://example.invalid/one"]
    assert built[0].posts == ["https://example.invalid/two"]


async def test_session_carries_impersonation_and_proxy(monkeypatch):
    seen: dict[str, Any] = {}

    class Recorder:
        def __init__(self, **kw: Any) -> None:
            seen.update(kw)

    import curl_cffi.requests as creq

    monkeypatch.setattr(creq, "Session", Recorder)
    source = Demo(proxy="http://127.0.0.1:9")
    assert isinstance(source.session, Recorder)
    assert seen == {"impersonate": "chrome131", "proxy": "http://127.0.0.1:9"}


async def test_a_passed_session_still_wins(src, monkeypatch):
    """Eurowings und Wizz bringen ihre eigene Session mit; die bleibt unberuehrt."""
    built: list[FakeSession] = []
    monkeypatch.setattr(src, "_new_session", lambda: built.append(FakeSession()) or built[-1])

    own = FakeSession()
    payload = await src.fetch_json("https://example.invalid/warm", session=own)

    assert payload["url"].endswith("/warm")
    assert own.gets == ["https://example.invalid/warm"]
    assert built == [], "die eigene Session der Quelle wurde gar nicht erst gebaut"


async def test_close_drops_the_session(src, monkeypatch):
    monkeypatch.setattr(src, "_new_session", FakeSession)

    first = src.session
    src.close()

    assert first.closed is True
    assert src.session is not first, "nach close entsteht eine frische Session"


async def test_close_survives_a_session_that_refuses(src, monkeypatch):
    class Grumpy(FakeSession):
        def close(self) -> None:
            raise RuntimeError("nope")

    monkeypatch.setattr(src, "_new_session", Grumpy)
    src.session
    src.close()  # darf nicht hochschlagen
    assert src._http_session is None


async def test_errors_still_come_through_the_pooled_session(src, monkeypatch):
    class Broken(FakeSession):
        def get(self, url: str, **kw: Any) -> FakeResponse:
            return FakeResponse(None, status=404)

    monkeypatch.setattr(src, "_new_session", Broken)
    with pytest.raises(SourceError):
        await src.fetch_json("https://example.invalid/missing")
