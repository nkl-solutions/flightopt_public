"""Die Hotelseite, ihre Endpunkte und der Umschalter im Kopf.

Kein Test startet hier eine Quelle: der Katalog wird ersetzt, damit die Suche
gegen eine Attrappe laeuft.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from datetime import date
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from flightopt.api import main
from flightopt.domain.models import Money
from flightopt.hotels.models import HotelOffer, HotelQuery
from flightopt.hotels.sources.base import HotelBatch, HotelSource

WEB = main.WEB_DIR
HOTELS_HTML = WEB / "hotels.html"
HOTELS_JS = WEB / "hotels.js"
INDEX = WEB / "index.html"
APP_CSS = WEB / "app.css"


class StubSource(HotelSource):
    name = "stub"

    def __init__(self, prices: list[int] | None = None) -> None:
        super().__init__()
        self.prices = prices or [12000, 24000]

    async def search(self, query: HotelQuery) -> HotelBatch:
        return HotelBatch(
            offers=[
                HotelOffer(
                    source=self.name,
                    property_key=f"stub:{index}",
                    name=f"Hotel {index}",
                    arrival=query.arrival,
                    departure=query.departure,
                    price_total=Money(minor, "EUR"),
                    stars=4 + (index % 2),
                    city="Athens",
                    country="Greece",
                    review_rating=8.5,
                    review_count=1200,
                    url="https://example.invalid/hotel",
                    party_size=query.party_size,
                )
                for index, minor in enumerate(self.prices)
            ]
        )


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main.runner, "db_path", str(tmp_path / "web.db"))
    monkeypatch.setattr(main.hotel_runner, "db_path", str(tmp_path / "web.db"))
    monkeypatch.setattr(main.hotel_runner, "_history", {})
    monkeypatch.setattr(main.hotel_runner, "_results", {})
    monkeypatch.setattr(main, "build_hotel_sources", lambda: [StubSource()])
    monkeypatch.setenv("FLIGHTOPT_DAILY_SCANS", "0")
    # Ohne den Kontextmanager bekommt jede Anfrage ihre eigene Ereignisschleife,
    # und der Hintergrundlauf stuerbe mit der Anfrage, die ihn gestartet hat.
    with TestClient(main.app) as ready:
        yield ready


def hotel_query(**kwargs) -> HotelQuery:
    base = {"destination": "Athen", "arrival": date(2026, 11, 10)}
    return HotelQuery(**{**base, **kwargs})


def test_the_hotel_page_is_served_and_carries_no_inline_code():
    page = HOTELS_HTML.read_text(encoding="utf-8")

    assert '<link rel="stylesheet" href="/static/app.css">' in page
    assert '<script src="/static/hotels.js"></script>' in page
    assert re.search(r"<style", page) is None
    assert re.search(r"<script(?![^>]*\ssrc=)", page) is None
    assert re.search(r"\sstyle=", page) is None


def test_both_pages_carry_the_same_switcher_and_mark_the_current_one():
    for page_path, current in ((INDEX, "/"), (HOTELS_HTML, "/hotels")):
        page = page_path.read_text(encoding="utf-8")
        assert '<nav class="domainnav" aria-label="Bereich">' in page
        assert f'<a href="{current}" aria-current="page">' in page
    # Der Umschalter benutzt nur vorhandene Tokens, keine neue Farbe.
    css = APP_CSS.read_text(encoding="utf-8")
    block = css[css.index(".domainnav"):css.index("/* --- Reiseart")]
    assert re.search(r"#[0-9a-fA-F]{3,8}\b", block) is None
    assert "oklch(" not in block


HOTEL_HARNESS = r"""
const assert = require("assert");

class ClassList {
  constructor() { this.set = new Set(); }
  add(...n) { n.forEach(x => this.set.add(String(x))); }
  remove(...n) { n.forEach(x => this.set.delete(String(x))); }
  contains(n) { return this.set.has(String(n)); }
  get value() { return [...this.set].join(" "); }
  set value(v) { this.set = new Set(String(v || "").split(/\s+/).filter(Boolean)); }
}

class Element {
  constructor() {
    this.children = [];
    this.dataset = {};
    this.style = {};
    this.classList = new ClassList();
    this.attributes = {};
    this.value = "";
    this.hidden = false;
    this.disabled = false;
    this.innerHTML = "";
    this.textContent = "";
  }
  get className() { return this.classList.value; }
  set className(v) { this.classList.value = v; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] || ""; }
  addEventListener() {}
  focus() { document.activeElement = this; }
  querySelectorAll() { return []; }
}

const elements = new Map();
function el(id) {
  if (!elements.has(id)) elements.set(id, new Element());
  return elements.get(id);
}
global.document = {
  body: new Element(),
  activeElement: null,
  querySelector(selector) { return el(selector); },
  querySelectorAll() { return []; },
  createElement() { return new Element(); },
};
global.EventSource = function () { this.close = () => {}; };
global.fetch = async () => ({ ok: true, json: async () => ({}) });
"""


def run_hotel_assertion(assertion: str) -> subprocess.CompletedProcess[str]:
    """Die Funktionen aus hotels.js im Node-Harness, ohne Browser."""
    code = (
        HOTEL_HARNESS
        + "\n"
        + HOTELS_JS.read_text(encoding="utf-8")
        + "\n(async () => {\n"
        + assertion
        + "\n})().catch(err => { console.error(err); process.exit(1); });\n"
    )
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".cjs", delete=False
    ) as script:
        script.write(code)
        path = Path(script.name)
    try:
        return subprocess.run(
            ["node", str(path)], text=True, capture_output=True, check=False
        )
    finally:
        path.unlink(missing_ok=True)


def test_the_signal_column_prefers_the_tier_and_falls_back_to_the_status():
    result = run_hotel_assertion(
        r"""
assert.strictEqual(signalKey({signal:"normal", tier:"error"}), "error");
assert.strictEqual(signalKey({signal:"cheap"}), "cheap");
assert.strictEqual(signalKey({signal:"was-auch-immer"}), "unknown");
assert.strictEqual(signalKey({}), "unknown");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_window_mode_sorts_by_signal_with_the_price_errors_first():
    result = run_hotel_assertion(
        r"""
mode = "window";
assert.strictEqual(defaultSort(), "signal");
mode = "single";
assert.strictEqual(defaultSort(), "price");

const list = [
  {name:"C", signal:"normal", price_per_night:80, date:"2026-11-11", source:"trivago"},
  {name:"A", tier:"error",    price_per_night:200, date:"2026-11-12", source:"booking"},
  {name:"B", signal:"cheap",  price_per_night:90, date:"2026-11-10", source:"trivago"},
  {name:"D", signal:"normal", price_per_night:70, date:"2026-11-13", source:"booking"},
];

assert.deepStrictEqual(sortRows(list, "signal").map(r => r.name), ["A","B","D","C"]);
assert.deepStrictEqual(sortRows(list, "price").map(r => r.name), ["D","C","B","A"]);
assert.deepStrictEqual(sortRows(list, "date").map(r => r.name), ["B","C","A","D"]);
assert.deepStrictEqual(sortRows(list, "source").map(r => r.name), ["D","A","C","B"]);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_progress_meter_names_the_day_count_and_the_current_date():
    result = run_hotel_assertion(
        r"""
setProgress({phase:"day", done:3, total:12, detail:{date:"2026-11-12"}});

assert.strictEqual($("#progresslabel").textContent, "Tage werden geholt");
// Datum in deutscher Schreibweise wie in der Tabelle, nicht als ISO-Feld.
assert.strictEqual($("#progresscount").textContent, "3 von 12 Tagen, Do., 12. Nov.");
assert.strictEqual($("#progressbar").attributes["aria-valuenow"], "25");
assert.strictEqual($("#progressfill").style.width, "25%");

setProgress({phase:"failed", done:3, total:12});
assert.strictEqual($("#progressbar").className, "progressbar failed");
assert.strictEqual($("#progressbar").attributes["aria-valuenow"], "100");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_table_shows_the_signal_and_the_source_per_row():
    result = run_hotel_assertion(
        r"""
rows = [
  {name:"Hotel A", stars:4, date:"2026-11-10", nights:1, price_per_night:120,
   currency:"EUR", review_rating:8.5, signal:"normal", tier:"error",
   reason:"weit unter der Basis", source:"trivago", url:"https://example.invalid/a"},
];
draw();
const html = $("#hotelrows").innerHTML;

assert.ok(html.includes('data-signal="error"'), html);
assert.ok(html.includes("Preisfehler"), html);
assert.ok(html.includes('class="c-source">trivago'), html);
assert.ok(html.includes('href="https://example.invalid/a"'), html);
assert.strictEqual($("#filtercount").textContent, "1 Zeilen");

rows[0].url = "javascript:alert(1)";
draw();
assert.ok(!$("#hotelrows").innerHTML.includes("javascript:"), $("#hotelrows").innerHTML);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_running_state_offers_a_cancel_button_and_keeps_the_focus_put():
    result = run_hotel_assertion(
        r"""
setRunning(true);
assert.strictEqual($("#hgo").disabled, true);
assert.strictEqual($("#hgo").textContent, "Suche läuft");
assert.strictEqual($("#hcancel").hidden, false);

setRunning(false);
assert.strictEqual($("#hcancel").hidden, true);
assert.strictEqual($("#hgo").textContent, "Suchen");

// Wer gerade in einem Feld tippt, behaelt seinen Cursor.
document.activeElement = $("#destination");
assert.strictEqual(focusResults(), false);
document.activeElement = document.body;
assert.strictEqual(focusResults(), true);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_saved_run_list_names_state_hits_and_time():
    result = run_hotel_assertion(
        r"""
const scan = {id:7, destination:"Athen", window_start:"2026-11-10",
  window_end:"2026-11-20", status:"cancelled", days_done:4, days_total:11,
  offers_found:18, created_at:"2026-09-08T11:20:31"};

// Daten und Uhrzeiten stehen deutsch, wie ueberall sonst auf beiden Seiten.
assert.strictEqual(scanTitle(scan), "Athen, Di., 10. Nov. bis Fr., 20. Nov.");
assert.strictEqual(scanSummary(scan),
  "abgebrochen, 18 Treffer, 4 von 11 Tagen, 08.09.2026, 11:20");
// Ein einzelner Lauf liefert das Fenster verschachtelt, die Zeile bleibt gleich.
assert.strictEqual(
  scanTitle({destination:"Athen", window:{start:"2026-11-10", end:"2026-11-10"}}),
  "Athen, Di., 10. Nov.");

drawScans([scan]);
assert.ok($("#scanlist").innerHTML.includes('data-scan="7"'), $("#scanlist").innerHTML);
drawScans([]);
assert.ok($("#scanlist").innerHTML.includes("Noch kein Lauf"));
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_no_arrow_or_dash_typography_in_the_visible_text():
    for path in (HOTELS_HTML, HOTELS_JS):
        text = path.read_text(encoding="utf-8")
        for forbidden in ("→", "←", "—", "–", "➡"):
            assert forbidden not in text, f"{path.name} enthaelt {forbidden!r}"


def test_the_page_and_its_script_are_served_by_the_static_mount(client):
    page = client.get("/hotels")
    script = client.get("/static/hotels.js")

    assert page.status_code == 200
    assert "Hotelpreise" in page.text
    assert script.status_code == 200
    assert script.headers["content-type"].split(";")[0].strip() == "text/javascript"


def events(client, scan_id: int) -> list[dict]:
    """Den Ereignisstrom bis zur terminalen Phase mitlesen."""
    body = client.get(f"/api/hotels/scan/{scan_id}/events").text
    return [
        json.loads(line[len("data: "):])
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


def test_a_search_starts_a_job_and_answers_with_the_scan_id(client):
    response = client.post(
        "/api/hotels/search",
        json={"destination": "Athen", "arrival": "2026-11-10", "adults": 2},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "running"
    assert body["days_total"] == 1
    assert "rows" not in body
    assert body["sources"][0]["name"] == "trivago"

    stream = events(client, body["scan_id"])
    assert [event["phase"] for event in stream] == ["planning", "day", "done"]
    rows = stream[-1]["detail"]["rows"]
    assert [row["name"] for row in rows] == ["Hotel 0", "Hotel 1"]
    assert rows[0]["price_per_night"] == 120.0
    assert rows[0]["currency"] == "EUR"
    assert rows[0]["signal"] == "unknown"
    assert rows[0]["review_rating"] == 8.5

    state = client.get(f"/api/hotels/scan/{body['scan_id']}")
    assert state.status_code == 200
    assert state.json()["status"] == "done"
    assert state.json()["current_day"] == "2026-11-10"


def test_a_window_reports_one_day_at_a_time(client):
    started = client.post(
        "/api/hotels/search",
        json={"destination": "Athen", "arrival": "2026-11-10",
              "window_end": "2026-11-12", "nights": 2},
    ).json()

    stream = events(client, started["scan_id"])

    days = [event for event in stream if event["phase"] == "day"]
    assert [event["detail"]["date"] for event in days] == [
        "2026-11-10", "2026-11-11", "2026-11-12"
    ]
    assert [(event["done"], event["total"]) for event in days] == [(1, 3), (2, 3), (3, 3)]
    rows = stream[-1]["detail"]["rows"]
    assert all(row["nights"] == 2 for row in rows)


def test_a_long_window_is_no_longer_refused(client, monkeypatch):
    started: list[int] = []
    monkeypatch.setattr(
        main.hotel_runner, "start", lambda scan_id, *a, **k: started.append(scan_id)
    )

    response = client.post(
        "/api/hotels/search",
        json={"destination": "Athen", "arrival": "2026-11-10", "window_end": "2027-08-31"},
    )

    assert response.status_code == 200
    assert response.json()["days_total"] == 295
    assert started == [response.json()["scan_id"]]


def test_an_oversized_or_reversed_window_is_refused_with_a_sentence(client):
    too_big = client.post(
        "/api/hotels/search",
        json={"destination": "Athen", "arrival": "2026-11-10", "window_end": "2028-11-10"},
    )
    backwards = client.post(
        "/api/hotels/search",
        json={"destination": "Athen", "arrival": "2026-11-10", "window_end": "2026-11-01"},
    )

    assert too_big.status_code == 400
    assert "732 Tage" in too_big.json()["detail"]
    assert f"{main.MAX_HOTEL_DAYS} Tage vorgesehen" in too_big.json()["detail"]
    assert backwards.status_code == 400
    assert "vor dem Anfang" in backwards.json()["detail"]


def test_an_unknown_scan_is_a_404_and_not_an_empty_row(client):
    assert client.get("/api/hotels/scan/9999").status_code == 404
    assert client.get("/api/hotels/scan/9999/rows").status_code == 404
    assert client.get("/api/hotels/scan/9999/events").status_code == 404
    assert client.post("/api/hotels/scan/9999/cancel").status_code == 404


def test_saved_runs_are_listed_and_their_rows_can_be_loaded_again(client):
    started = client.post(
        "/api/hotels/search",
        json={"destination": "Athen", "arrival": "2026-11-10",
              "window_end": "2026-11-11", "nights": 2},
    ).json()
    events(client, started["scan_id"])

    listed = client.get("/api/hotels/scans").json()["scans"]
    assert [scan["id"] for scan in listed] == [started["scan_id"]]
    assert listed[0]["destination"] == "Athen"
    assert listed[0]["window_start"] == "2026-11-10"
    assert listed[0]["status"] == "done"
    assert listed[0]["days_total"] == 2
    assert listed[0]["created_at"]

    stored = client.get(f"/api/hotels/scan/{started['scan_id']}/rows").json()
    assert stored["destination"] == "Athen"
    assert stored["window"] == {"start": "2026-11-10", "end": "2026-11-11"}
    assert {row["name"] for row in stored["rows"]} == {"Hotel 0", "Hotel 1"}
    assert {row["date"] for row in stored["rows"]} == {"2026-11-10", "2026-11-11"}
    assert all(row["nights"] == 2 for row in stored["rows"])
    assert all(row["source"] == "stub" for row in stored["rows"])


def test_the_api_says_how_often_a_source_had_to_be_asked_again(client, monkeypatch):
    """Ohne diese Zahl sieht ein wackliger Endpunkt aus wie ein gesunder.

    Der Zaehler haengt seit jeher am Ergebnis der Quelle, kam aber nirgends
    nach draussen. Ein Lauf, der nur mit Nachfassen gruen wurde, war von einem
    reibungslosen nicht zu unterscheiden.
    """

    class Wobbly(StubSource):
        async def search(self, query: HotelQuery) -> HotelBatch:
            batch = await super().search(query)
            batch.retries = 1
            return batch

    monkeypatch.setattr(main, "build_hotel_sources", lambda: [Wobbly()])
    started = client.post(
        "/api/hotels/search",
        json={"destination": "Athen", "arrival": "2026-11-10",
              "window_end": "2026-11-11"},
    ).json()
    events(client, started["scan_id"])
    scan_id = started["scan_id"]

    assert client.get(f"/api/hotels/scan/{scan_id}").json()["retries"] == 2
    assert client.get(f"/api/hotels/scan/{scan_id}/rows").json()["retries"] == 2
    assert client.get("/api/hotels/scans").json()["scans"][0]["retries"] == 2


def test_the_cancel_endpoint_reports_the_new_status(client):
    scan_id = main.hotel_runner.create(
        hotel_query(), window_start=date(2026, 11, 10), window_end=date(2026, 11, 20)
    )

    response = client.post(f"/api/hotels/scan/{scan_id}/cancel")

    assert response.status_code == 200
    assert response.json() == {"scan_id": scan_id, "status": "cancelled"}
    assert client.get(f"/api/hotels/scan/{scan_id}").json()["status"] == "cancelled"


def test_the_event_stream_replays_a_terminal_run_without_history(client):
    """Nach einem Neustart hat der Lauf keinen Verlauf mehr, nur seinen Stand."""
    scan_id = main.hotel_runner.create(
        hotel_query(), window_start=date(2026, 11, 10), window_end=date(2026, 11, 20)
    )
    main.hotel_runner.cancel(scan_id)
    main.hotel_runner._history.pop(scan_id, None)
    main.hotel_runner._results.pop(scan_id, None)

    body = client.get(f"/api/hotels/scan/{scan_id}/events").text

    assert '"phase": "cancelled"' in body
    assert "abgebrochen" in body
    assert "fertig" not in body


def test_a_validation_error_reads_as_german_instead_of_object_object():
    """Ein 422 liefert `detail` als Liste. Direkt in eine Meldung geschrieben
    ergab das woertlich "[object Object]"."""
    result = run_hotel_assertion(
        r"""
assert.strictEqual(detail("Unbekannter Durchlauf"), "Unbekannter Durchlauf");
assert.strictEqual(
  detail([{msg: "arrival: Datum liegt in der Vergangenheit"}, {msg: "rooms: zu viele"}]),
  "arrival: Datum liegt in der Vergangenheit; rooms: zu viele");
assert.strictEqual(detail(undefined), "");
assert.strictEqual(detail(null), "");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_a_price_the_detector_calls_broken_is_not_called_missing():
    """`encoding_suspect` fehlte in der Tabelle und fiel auf "keine Basis":
    aus einem Befund wurde damit eine Nicht-Aussage."""
    result = run_hotel_assertion(
        r"""
assert.strictEqual(signalKey({tier: "encoding_suspect"}), "encoding_suspect");
assert.strictEqual(SIGNALS.encoding_suspect.label, "Preis unklar");
// Er steht knapp unter dem Preisfehler und weit ueber "keine Basis".
assert.ok(SIGNALS.encoding_suspect.rank > SIGNALS.error.rank);
assert.ok(SIGNALS.encoding_suspect.rank < SIGNALS.cheap.rank);
assert.strictEqual(signalKey({tier: "voellig neu"}), "unknown");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_source_list_names_why_a_source_did_not_run():
    """Der Grund stand bisher als eine Textzeile in einem zugeklappten Bereich."""
    result = run_hotel_assertion(
        r"""
drawSources([
  {name: "booking", active: true, reason: ""},
  {name: "trivago", active: false, reason: "FLIGHTOPT_HOTELS_BOOKING=1 schaltet sie ein"},
]);

const html = $("#sourcehint").innerHTML;
assert.ok(html.includes("booking"), html);
assert.ok(html.includes("läuft"), html);
assert.ok(html.includes("FLIGHTOPT_HOTELS_BOOKING=1"), html);
assert.ok(html.includes('data-active="false"'), html);

// Ohne Grund bleibt es bei einer klaren Aussage, nicht bei einer leeren.
drawSources([{name: "trivago", active: false}]);
assert.ok($("#sourcehint").innerHTML.includes("abgeschaltet"), $("#sourcehint").innerHTML);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_skipped_days_and_source_errors_reach_the_screen():
    result = run_hotel_assertion(
        r"""
resetRunNotes();
assert.strictEqual($("#runnotes").textContent, "");
assert.strictEqual(skippedNote(0), "");
assert.ok(skippedNote(1).startsWith("1 Tag wurde"));
assert.ok(skippedNote(3).startsWith("3 Tage wurden"));

addRunNotes([skippedNote(2), "2026-01-05: booking: HTTP 403 auf der Ergebnisseite"]);
assert.strictEqual($("#runnotes").dataset.tone, "warn");
assert.ok($("#runnotes").textContent.includes("HTTP 403"), $("#runnotes").textContent);
assert.strictEqual(runNotes.length, 2);

// Dieselbe Meldung zweimal ergibt eine Zeile.
addRunNotes(["2026-01-05: booking: HTTP 403 auf der Ergebnisseite"]);
assert.strictEqual(runNotes.length, 2);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_a_row_names_the_stay_total_and_how_many_voices_rated_it():
    result = run_hotel_assertion(
        r"""
const html = rowHtml({
  source: "booking", name: "Hotel Grande", city: "Athen", stars: 5,
  date: "2026-11-12", nights: 3, price_per_night: 212.4, price_total: 637.2,
  currency: "EUR", review_rating: 9.2, review_count: 3184,
  tier: "cheap", n: 24, basis: "peer", url: "https://example.invalid/x",
});

// Bei mehreren Naechten ist der Nachtpreis nicht das, was abgebucht wird.
assert.ok(html.includes("637,20 EUR für 3 Nächte"), html);
// 9,2 aus acht Stimmen ist etwas anderes als 9,2 aus dreitausend.
assert.ok(html.includes("3.184 Stimmen"), html);
// Worauf das Signal steht, haengt am Feld statt nirgends.
assert.ok(html.includes("24 Vergleichspreise"), html);
assert.ok(html.includes("vergleichbare Häuser"), html);
// Datum deutsch, nicht als ISO-Feld.
assert.ok(html.includes("Do., 12. Nov."), html);

// Eine einzelne Nacht bekommt keine doppelte Summe.
const one = rowHtml({source: "booking", name: "X", date: "2026-11-12", nights: 1,
  price_per_night: 61, price_total: 61, currency: "EUR"});
assert.ok(!one.includes("für 1 Nächte"), one);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_an_empty_result_says_what_to_change_instead_of_showing_nothing():
    result = run_hotel_assertion(
        r"""
$("#filterSignal").value = ""; $("#filterStars").value = ""; $("#filterSource").value = "";
rows = [];
running = false;
draw();
assert.ok($("#hotelrows").innerHTML.includes("keine Quelle ein Angebot"),
  $("#hotelrows").innerHTML);

// Waehrend der Lauf noch Tage holt, ist "nichts gefunden" eine Falschaussage.
running = true;
draw();
assert.ok($("#hotelrows").innerHTML.includes("werden gerade geholt"),
  $("#hotelrows").innerHTML);
running = false;

rows = [{source:"booking", name:"X", date:"2026-11-12", nights:1,
         price_per_night:61, currency:"EUR", stars:3}];
$("#filterStars").value = "5";
draw();
assert.ok($("#hotelrows").innerHTML.includes("keine passt zu diesen Filtern"),
  $("#hotelrows").innerHTML);
assert.strictEqual($("#resetfilters").hidden, false);

resetFilters();
assert.strictEqual($("#filterStars").value, "");
assert.strictEqual($("#resetfilters").hidden, true);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_hotel_page_uses_the_local_day_not_the_utc_day():
    """`toISOString` haette abends in Berlin den Vortag geliefert."""
    result = run_hotel_assertion(
        r"""
const now = new Date();
const pad = n => String(n).padStart(2, "0");
assert.strictEqual(today(0),
  `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`);
assert.strictEqual(isoDay(new Date(2026, 0, 5)), "2026-01-05");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout
    # Der Aufruf ist weg, der erklaerende Kommentar darf das Wort behalten.
    assert ".toISOString()" not in HOTELS_JS.read_text(encoding="utf-8")
