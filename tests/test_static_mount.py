"""Die aufgeteilte Oberflaeche muss auch ausgeliefert werden."""

from __future__ import annotations

import base64
import re

from starlette.testclient import TestClient

from flightopt.api import main


def client() -> TestClient:
    # Ohne `with` laeuft der Lifespan nicht, der Tages-Scanner startet also nicht.
    return TestClient(main.app)


def read_css() -> str:
    return (main.WEB_DIR / "app.css").read_text(encoding="utf-8")


def test_static_mount_serves_the_split_assets():
    c = client()

    css = c.get("/static/app.css")
    js = c.get("/static/app.js")

    assert css.status_code == 200
    assert js.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    # Ohne festgenagelten MIME-Typ meldet mancher Linux-Container
    # application/javascript, der Charset-Zusatz darf aber bleiben.
    assert js.headers["content-type"].split(";")[0].strip() == "text/javascript"
    assert "function payload()" in js.text


def test_index_references_the_split_assets_and_has_no_inline_code():
    page = (main.WEB_DIR / "index.html").read_text(encoding="utf-8")

    assert '<link rel="stylesheet" href="/static/app.css">' in page
    assert '<script src="/static/app.js"></script>' in page
    # Auch `<style media=...>` waere Inline-CSS, deshalb nicht auf `<style>` pruefen.
    assert re.search(r"<style", page) is None
    # Nur Script-Tags mit src sind erlaubt, jedes andere waere Inline-Code.
    assert re.search(r"<script(?![^>]*\ssrc=)", page) is None


def test_index_route_still_returns_the_page():
    response = client().get("/")

    assert response.status_code == 200
    assert "flightopt" in response.text


def test_static_assets_need_basic_auth_when_credentials_are_set(monkeypatch):
    monkeypatch.setenv("FLIGHTOPT_BASIC_USER", "dev")
    monkeypatch.setenv("FLIGHTOPT_BASIC_PASSWORD", "secret")
    c = client()

    denied = c.get("/static/app.js")

    assert denied.status_code == 401
    assert denied.headers["www-authenticate"] == 'Basic realm="flightopt"'

    encoded = base64.b64encode(b"dev:secret").decode("ascii")
    allowed = c.get("/static/app.js", headers={"Authorization": f"Basic {encoded}"})

    assert allowed.status_code == 200


def test_css_and_fonts_directory_declare_exactly_the_same_files():
    declared = sorted(set(re.findall(r"/static/fonts/([\w.-]+\.woff2)", read_css())))
    shipped = sorted(p.name for p in (main.WEB_DIR / "fonts").glob("*.woff2"))

    # Beide Richtungen: keine tote Referenz und keine unbenutzte Datei im Ordner.
    assert declared == shipped


def test_every_declared_font_file_exists_and_is_served():
    c = client()
    declared = sorted(set(re.findall(r"/static/fonts/([\w.-]+\.woff2)", read_css())))

    assert declared, "app.css referenziert keine einzige Schriftdatei"

    for name in declared:
        path = main.WEB_DIR / "fonts" / name
        assert path.exists(), f"fehlt: {name}"
        # woff2 beginnt immer mit der Signatur 'wOF2'.
        assert path.read_bytes()[:4] == b"wOF2", f"kein woff2: {name}"

        response = c.get(f"/static/fonts/{name}")

        assert response.status_code == 200, f"nicht ausgeliefert: {name}"
        assert response.headers["content-type"] == "font/woff2", name


def test_font_face_blocks_swap_and_match_their_file_name():
    blocks = re.findall(r"@font-face\s*\{(.*?)\}", read_css(), re.DOTALL)

    assert len(blocks) == 5

    for block in blocks:
        file_name = re.search(r"/static/fonts/([\w.-]+\.woff2)", block)
        assert file_name, f"kein woff2-Verweis im Block: {block!r}"
        name = file_name.group(1)

        assert re.search(r"font-display\s*:\s*swap", block), f"kein swap: {name}"

        declared_weight = re.search(r"font-weight\s*:\s*(\d+)", block)
        assert declared_weight, f"kein font-weight: {name}"
        weight_in_name = re.search(r"-(\d+)\.woff2$", name)
        assert weight_in_name, f"kein Gewicht im Dateinamen: {name}"
        assert declared_weight.group(1) == weight_in_name.group(1), name


def test_font_licences_are_shipped():
    for name in ("OFL-ArchivoNarrow.txt", "OFL-SometypeMono.txt"):
        text = (main.WEB_DIR / "fonts" / name).read_text(encoding="utf-8")
        assert "SIL OPEN FONT LICENSE" in text.upper()


def test_no_external_font_request_in_the_page():
    css = read_css()
    page = (main.WEB_DIR / "index.html").read_text(encoding="utf-8")

    for needle in ("fonts.googleapis.com", "fonts.gstatic.com", "gwfh.mranftl.com"):
        assert needle not in css
        assert needle not in page
