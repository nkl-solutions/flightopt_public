"""Browser-form behavior that is too easy to break in the single-file UI."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

from flightopt.domain import airlines as airline_registry


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "flightopt" / "web"
INDEX = WEB / "index.html"
APP_JS = WEB / "app.js"
APP_CSS = WEB / "app.css"


def script_from_index() -> str:
    """Der Node-Harness fuehrt den Rohtext von app.js aus, nicht mehr das Markup."""
    return APP_JS.read_text(encoding="utf-8")


@pytest.fixture
def airline_colours() -> str:
    """Die echten Markenfarben als Konstante fuer den Harness."""
    colours = [a.color for a in airline_registry.AIRLINES.values()]
    return "const REGISTRY_COLOURS = " + json.dumps(colours) + ";"


def run_ui_assertion(assertion: str, setup: str = "") -> subprocess.CompletedProcess[str]:
    harness = r"""
const assert = require("assert");

/* Eine Klassenliste, die nichts tut, macht jede klassenabhaengige Verzweigung
   untestbar. Diese hier haelt wirklich Klassen und spiegelt sie in `className`. */
class ClassList {
  constructor() { this.set = new Set(); }
  /* Der echte DOM wirft bei "" einen SyntaxError. Ein Harness, der stattdessen
     stillschweigend nichts tut, versteckt genau diesen Fehler. */
  static named(n) {
    const name = String(n);
    if (!name) throw new SyntaxError("leerer Klassenname");
    return name;
  }
  add(...names) { names.forEach(n => this.set.add(ClassList.named(n))); }
  remove(...names) { names.forEach(n => this.set.delete(ClassList.named(n))); }
  toggle(name, force) {
    const on = force === undefined ? !this.set.has(name) : Boolean(force);
    if (on) this.set.add(String(name)); else this.set.delete(String(name));
    return on;
  }
  contains(name) { return this.set.has(String(name)); }
  get value() { return [...this.set].join(" "); }
  set value(v) {
    this.set = new Set(String(v == null ? "" : v).split(/\s+/).filter(Boolean));
  }
}

/* `innerHTML` ist im Browser ein Parser, kein Textfeld. Der Harness braucht davon
   genau so viel, dass eine geschriebene Radiogruppe danach wirklich als Elemente
   dasteht: sonst faellt nicht auf, wenn ein Neuaufbau die fokussierte Eingabe
   wegwirft. */
function parseInputs(html) {
  return [...String(html).matchAll(/<input\b([^>]*)>/g)].map(m => {
    const node = new Element("input");
    for (const attr of m[1].matchAll(/([A-Za-z-]+)(?:="([^"]*)")?/g)) {
      const name = attr[1], value = attr[2] === undefined ? "" : attr[2];
      if (name === "value") node.value = value;
      else if (name === "checked") node.checked = true;
      else node.setAttribute(name, value);
    }
    return node;
  });
}

class Element {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.dataset = {};
    this.style = {};
    this.classList = new ClassList();
    this.attributes = {};
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.innerHTML = "";
    this.textContent = "";
  }
  get className() { return this.classList.value; }
  set className(v) { this.classList.value = v; }
  get innerHTML() { return this._html || ""; }
  set innerHTML(v) {
    this._html = String(v == null ? "" : v);
    // Wer beim Neuschreiben im Kasten stand, verliert den Fokus an <body>.
    const here = typeof document === "undefined" ? null : document.activeElement;
    if (here && (this._inputs || []).includes(here)) document.activeElement = document.body;
    this._inputs = parseInputs(this._html);
  }
  append(...items) { this.children.push(...items); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] || ""; }
  focus() { document.activeElement = this; }
  querySelectorAll(selector) { return selector === "input" ? (this._inputs || []) : []; }
  querySelector() { return new Element(); }
}

const elements = new Map();
function el(id) {
  if (!elements.has(id)) elements.set(id, new Element());
  return elements.get(id);
}

global.document = {
  querySelector(selector) { return el(selector); },
  querySelectorAll() { return []; },
  createElement(tag) { return new Element(tag); },
};
global.localStorage = { getItem() { return null; }, setItem() {} };
global.fetch = async () => ({ json: async () => ({ airlines: [], results: [] }) });
global.EventSource = function() {};
"""
    # Einige Zusicherungen muessen `await` benutzen. Die Huelle kostet nichts und
    # laesst jeden bestehenden Block unveraendert laufen.
    code = (
        harness
        + "\n"
        + setup
        + "\n"
        + script_from_index()
        + "\n(async () => {\n"
        + assertion
        + "\n})().catch(err => { console.error(err); process.exit(1); });\n"
    )
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        suffix=".cjs",
        delete=False,
    ) as script:
        script.write(code)
        path = Path(script.name)
    try:
        return subprocess.run(
            ["node", str(path)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
    finally:
        path.unlink(missing_ok=True)


def test_multi_stop_payload_keeps_each_stop_stay_range_separate():
    result = run_ui_assertion(
        r"""
trip = "multi";
hops = [
  {code:"BER", label:"Berlin"},
  {code:"FCO", label:"Rom"},
  {code:"ATH", label:"Athen"},
  {code:"BER", label:"Berlin"},
];
$("#from").value = "2026-10-01";
$("#to").value = "2026-11-30";
$("#stay-min-0").value = "2";
$("#stay-max-0").value = "3";
if (typeof syncStayControls === "function") syncStayControls();
$("#stay-min-1").value = "7";
$("#stay-max-1").value = "9";

assert.deepStrictEqual(payload().stays, [[2, 3], [7, 9]]);
assert.strictEqual(stayInputId(0, "min"), "stay-min-0");
assert.strictEqual(stayInputId(0, "max"), "stay-max-0");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_progress_meter_shows_current_phase_and_count():
    result = run_ui_assertion(
        r"""
assert.strictEqual(typeof setProgress, "function");

setProgress({phase:"fetching", done:3, total:6, message:"Preise werden geladen"});

assert.strictEqual($("#progresslabel").textContent, "Preise abrufen");
assert.strictEqual($("#progresscount").textContent, "3 / 6");
assert.ok(parseFloat($("#progressfill").style.width) > 30);
assert.strictEqual($("#progressbar").attributes["aria-valuenow"], "45");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_results_copy_separates_favorite_from_candidates():
    result = run_ui_assertion(
        r"""
assert.strictEqual(resultTitle([{}, {}, {}]), "Favorit + 2 Kandidaten");
assert.strictEqual(resultRankLabel(0), "Favorit");
assert.strictEqual(resultRankLabel(1), "#2");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_profile_payload_reuses_current_search():
    result = run_ui_assertion(
        r"""
$("#profileName").value = "Ost nach Athen";
$("#checkedBags").value = "1";
const body = profilePayload();

assert.strictEqual(body.name, "Ost nach Athen");
assert.deepStrictEqual(body.airports, payload().airports);
assert.strictEqual(body.cadence_days, 1);
assert.strictEqual(body.checked_bags, 1);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_scanner_summary_names_state_and_due_profiles():
    result = run_ui_assertion(
        r"""
assert.strictEqual(
  scannerSummary({scanner:{running:true}, due:{profiles:[{name:"Athen"}]}}),
  "Scanner läuft. Fällig: Athen"
);
assert.strictEqual(
  scannerSummary({scanner:{running:false}, due:{profiles:[]}}),
  "Scanner bereit. Keine fälligen Profile."
);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_estimate_summary_warns_when_search_space_is_large():
    result = run_ui_assertion(
        r"""
const summary = estimateSummary({combinations: 120000, cells: 820, variants: 3}, "multi");

assert.strictEqual(summary.warn, true);
assert.ok(summary.html.includes("120.000"));
assert.ok(summary.html.includes("3</b> Routenvarianten"));
assert.ok(summary.html.includes("Sehr großer Suchraum"));
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_route_glance_summarises_current_choices():
    result = run_ui_assertion(
        r"""
trip = "return";
hops = [{code:"BER", label:"Berlin"}, {code:"ATH", label:"Athen"}];
$("#from").value = "2026-10-01";
$("#to").value = "2026-11-30";
$("#checkedBags").value = "2";
$("#maxStops").value = "";
picked = new Set(["FR", "A3"]);

const g = routeGlance();

// Kein Pfeil in sichtbarer Copy, nur ein Bindestrich.
assert.strictEqual(g.route, "Berlin - Athen");
assert.strictEqual(g.bag, "2 Aufgabegepäckstücke");
assert.strictEqual(g.carriers, "FR, A3");
// "automatisch nach Entfernung" ist die Vorgabe und sagt dem Ueberblick nichts.
assert.strictEqual(g.stops, "");
updateRouteGlance();
assert.ok(!$("#routeglance").innerHTML.includes("Umstieg"), $("#routeglance").innerHTML);

$("#maxStops").value = "0";
assert.strictEqual(routeGlance().stops, "ohne Umstieg");
$("#maxStops").value = "1";
assert.strictEqual(routeGlance().stops, "bis 1 Umstieg");
$("#maxStops").value = "2";
assert.strictEqual(routeGlance().stops, "bis 2 Umstiege");

updateRouteGlance();
const glance = $("#routeglance").innerHTML;
assert.ok(glance.includes("bis 2 Umstiege"), glance);
assert.ok(glance.includes("2 Aufgabegepäckstücke"), glance);
$("#maxStops").value = "";
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_search_button_names_running_state():
    result = run_ui_assertion(
        r"""
setSearching(true);
assert.strictEqual($("#go").disabled, true);
assert.strictEqual($("#go").textContent, "Suche läuft");

setSearching(false);
assert.strictEqual($("#go").disabled, false);
assert.strictEqual($("#go").textContent, "Suchen");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_result_filters_keep_matching_airline_and_quality():
    result = run_ui_assertion(
        r"""
const rows = [
  {total: 100, verified: true, legs: [{carriers:["FR"], indicative:false}]},
  {total: 120, verified: false, legs: [{carriers:["A3"], indicative:false}]},
  {total: 140, verified: false, legs: [{carriers:["XQ"], indicative:true}]},
];

$("#resultCarrier").value = "FR";
$("#resultQuality").value = "verified";
assert.deepStrictEqual(filterResults(rows), [rows[0]]);

$("#resultCarrier").value = "";
$("#resultQuality").value = "indicative";
assert.deepStrictEqual(filterResults(rows), [rows[2]]);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_result_filter_summary_names_visible_count():
    result = run_ui_assertion(
        r"""
assert.strictEqual(resultFilterSummary(1, 4), "1 von 4 Varianten sichtbar");
assert.strictEqual(resultFilterSummary(4, 4), "4 Varianten sichtbar");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_apply_natural_search_prefills_route_without_running_search():
    result = run_ui_assertion(
        r"""
applyNaturalSearch({
  trip: "multi",
  hops: [
    {code:"BER", label:"Berlin"},
    {code:"IST", label:"Istanbul"},
    {code:"ATH", label:"Athen"},
    {code:"BER", label:"Berlin"},
  ],
  warnings: [],
});

assert.strictEqual(trip, "multi");
assert.deepStrictEqual(hops.map(h => h.code), ["BER", "IST", "ATH", "BER"]);
assert.strictEqual($("#aiStatus").textContent, "Route übernommen.");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_voice_input_adds_spoken_route_to_ai_field():
    result = run_ui_assertion(
        r"""
let started = false;
class FakeRecognition {
  constructor() {
    this.lang = "";
    this.continuous = true;
    this.interimResults = false;
  }
  start() {
    started = true;
    this.onstart();
    this.onresult({
      resultIndex: 0,
      results: [
        {0: {transcript: "Berlin nach Istanbul"}, isFinal: true},
      ],
    });
    this.onend();
  }
  stop() { this.onend(); }
}

global.window = {SpeechRecognition: FakeRecognition};
setupVoiceInput();
$("#aiText").value = "";
$("#voiceInput").onclick();

assert.strictEqual(started, true);
assert.strictEqual($("#aiText").value, "Berlin nach Istanbul");
assert.strictEqual($("#voiceStatus").textContent, "Sprache übernommen. Route prüfen und übernehmen.");
assert.strictEqual($("#voiceInput").attributes["aria-pressed"], "false");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_ink_density_replaces_the_colour_ramp():
    result = run_ui_assertion(
        r"""
assert.strictEqual(typeof hue, "undefined");

// Vier Legs, alle gleich teuer: Durchschnittsanteil.
assert.strictEqual(inkDensity(50, 200, 4), 0.64);
// Ein sehr billiges Leg bleibt am hellen Ende.
assert.strictEqual(inkDensity(10, 200, 4), 0.28);
// Ab dem Anderthalbfachen des Durchschnitts ist es volle Tinte.
assert.strictEqual(inkDensity(75, 200, 4), 1);
assert.strictEqual(inkDensity(100, 200, 4), 1);
// Ein Zwischenwert wird auf zwei Stellen gerundet.
assert.strictEqual(inkDensity(65, 200, 4), 0.86);
assert.strictEqual(inkDensity(125, 200, 2), 0.82);
// Ohne Summe kein Division-durch-null.
assert.strictEqual(inkDensity(0, 0, 0), 0.64);

assert.strictEqual(
  inkShade(50, 200, 4),
  "color-mix(in oklab, var(--ink) 64%, var(--bg))"
);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_status_messages_use_one_tone_attribute():
    result = run_ui_assertion(
        r"""
setVoiceStatus("Kein Mikrofon gefunden.", "err");
assert.strictEqual($("#voiceStatus").dataset.tone, "err");
assert.strictEqual($("#voiceStatus").textContent, "Kein Mikrofon gefunden.");

setVoiceStatus("Höre zu.");
assert.strictEqual($("#voiceStatus").dataset.tone, "");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_autocomplete_marks_the_selection_with_aria_selected():
    result = run_ui_assertion(
        r"""
(async () => {
  // drawRoute baut die Vorschlagsliste in einer Closure. Wir merken uns alles,
  // was dabei entsteht, und lesen danach das erzeugte Markup.
  const made = [];
  const create = document.createElement;
  document.createElement = tag => { const node = create(tag); made.push(node); return node; };

  trip = "return";
  hops = [{code:"", label:""}, {code:"", label:""}];
  drawRoute();
  document.createElement = create;

  const input = made.find(n => n.tagName === "INPUT");
  const menu = made.find(n => n.className === "ac");

  global.fetch = async () => ({ok: true, json: async () => ({results: [
    {code:"BER", city:"Berlin", name:"Berlin Brandenburg", country:"DE"},
    {code:"BRE", city:"Bremen", name:"Bremen", country:"DE"},
  ]})});

  input.value = "Ber";
  input.oninput();
  await new Promise(done => setTimeout(done, 400));

  assert.ok(menu.innerHTML.includes("BER"), menu.innerHTML);
  assert.ok(menu.innerHTML.includes('aria-selected="true"'), menu.innerHTML);
  assert.ok(menu.innerHTML.includes('aria-selected="false"'), menu.innerHTML);
  assert.ok(!menu.innerHTML.includes('class="sel"'), menu.innerHTML);
  assert.ok(!/<b[^>]*class=/.test(menu.innerHTML), menu.innerHTML);
})().catch(err => { console.error(err); process.exit(1); });
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_status_elements_never_get_a_class_instead_of_a_tone():
    """Meldungen tragen ihren Ton in data-tone, sonst laufen CSS und Test auseinander."""
    source = APP_JS.read_text(encoding="utf-8")

    assert not re.search(r'classList\.(add|toggle)\("(err|warn)"', source)
    assert not re.search(r'className\s*=\s*"(quickhint|sizing|savedmsg|note)', source)


def test_cancel_button_closes_the_stream_and_names_the_state():
    result = run_ui_assertion(
        r"""
let closed = false;
let posted = null;
global.fetch = async (url, options) => {
  posted = {url, method: (options || {}).method};
  return {ok: true, json: async () => ({job_id: 7, status: "cancelled"})};
};

es = {close(){ closed = true; }};
currentJob = 7;
setSearching(true);

await cancelSearch();

assert.strictEqual(closed, true);
assert.strictEqual(es, null);
assert.strictEqual(posted.url, "/api/jobs/7/cancel");
assert.strictEqual(posted.method, "POST");
assert.strictEqual($("#go").disabled, false);
assert.strictEqual($("#note").textContent, "Suche abgebrochen");
assert.strictEqual($("#cancel").hidden, true);

setSearching(true);
assert.strictEqual($("#cancel").hidden, false);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_cancel_warns_when_the_server_refuses_the_abort():
    """Ein 500 vom Server ist kein bestaetigter Abbruch und muss auffallen."""
    result = run_ui_assertion(
        r"""
global.fetch = async () => ({ok: false, status: 500, json: async () => ({})});

es = {close(){}};
currentJob = 7;
setSearching(true);

await cancelSearch();

assert.strictEqual($("#note").dataset.tone, "warn");
assert.ok($("#note").textContent.includes("nicht bestätigt"), $("#note").textContent);
assert.strictEqual(currentJob, null);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_stream_error_forgets_the_running_job():
    """Ohne Job-Nummer laeuft ein spaeterer Abbruch sonst ins Leere."""
    result = run_ui_assertion(
        r"""
let closed = false;
global.EventSource = function(){ this.close = () => { closed = true; }; };
global.fetch = async () => ({ok: true, json: async () => ({job_id: 42})});

trip = "return";
hops = [{code:"BER", label:"Berlin"}, {code:"ATH", label:"Athen"}];
$("#from").value = "2026-10-01";
$("#to").value = "2026-11-30";

await $("#f").onsubmit({preventDefault(){}});
assert.strictEqual(currentJob, 42);

es.onerror();

assert.strictEqual(closed, true);
assert.strictEqual(es, null);
assert.strictEqual(currentJob, null);
assert.strictEqual($("#note").dataset.tone, "err");
assert.strictEqual($("#note").textContent, "Verbindung zum Server verloren");
assert.strictEqual($("#go").disabled, false);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout

def test_progress_labels_cover_the_cancelled_phase():
    result = run_ui_assertion(
        r"""
setProgress({phase:"cancelled"});

assert.strictEqual($("#progresslabel").textContent, "Abgebrochen");
assert.strictEqual($("#progressbar").attributes["aria-valuenow"], "100");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_badge_text_colour_follows_the_measured_contrast(airline_colours):
    result = run_ui_assertion(
        r"""
// Dunkle Markenfarben tragen Papier, helle tragen Ink.
assert.strictEqual(badgeTextColor("#073590"), "var(--paper)");   // Ryanair
assert.strictEqual(badgeTextColor("#05164d"), "var(--paper)");   // Lufthansa
assert.strictEqual(badgeTextColor("#ffad00"), "var(--ink)");     // Condor
assert.strictEqual(badgeTextColor("#ffc800"), "var(--ink)");     // Pegasus
assert.strictEqual(badgeTextColor("#00a991"), "var(--ink)");     // Kiwi
assert.strictEqual(badgeTextColor(""), "var(--paper)");          // unbekannt

// Die gerechneten Werte selbst muessen stimmen.
assert.ok(Math.abs(relativeLuminance("#ffffff") - 1) < 1e-9);
assert.ok(Math.abs(relativeLuminance("#000000")) < 1e-9);
assert.ok(Math.abs(contrastRatio(1, 0) - 21) < 1e-9);

// Die Schwellen kommen aus den Tokens, nicht aus abgeschriebenen Zahlen.
assert.strictEqual(INK_LUMINANCE, relativeLuminance("#1e1a16"));
assert.strictEqual(PAPER_LUMINANCE, relativeLuminance("#fefdfa"));

// Jede Farbe der echten Registry muss gegen Ink oder Papier lesbar sein.
REGISTRY_COLOURS.concat(["#69625d"]).forEach(colour => {
  const l = relativeLuminance(colour);
  const best = Math.max(contrastRatio(l, INK_LUMINANCE), contrastRatio(l, PAPER_LUMINANCE));
  assert.ok(best >= 4.5, `${colour} erreicht nur ${best.toFixed(2)}:1`);
  const chosen = badgeTextColor(colour) === "var(--ink)" ? INK_LUMINANCE : PAPER_LUMINANCE;
  assert.ok(contrastRatio(l, chosen) >= 4.5,
    `${colour} waehlt die schlechtere Textfarbe`);
});
""",
        setup=airline_colours,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_tail_mark_writes_the_chosen_text_colour_into_the_badge():
    result = run_ui_assertion(
        r"""
AIRLINES = [
  {code:"DE", name:"Condor", color:"#ffad00", kind:"airline"},
  {code:"FR", name:"Ryanair", color:"#073590", kind:"airline"},
  {code:"KIWI", name:"Kiwi (Vergleich)", color:"#00a991", kind:"comparison"},
];

assert.ok(tailMark("DE").includes("color:var(--ink)"));
assert.ok(tailMark("FR").includes("color:var(--paper)"));
// Ein Vergleichsportal ist keine Airline und traegt deshalb kein Kuerzel.
assert.ok(tailMark("KIWI").includes(">~<"));
// Unbekannte Codes bekommen die neutrale Flaeche.
assert.ok(tailMark("ZZ").includes("background:#69625d"));
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_window_check_rejects_an_end_before_the_start():
    result = run_ui_assertion(
        r"""
$("#from").value = "2026-10-10";
$("#to").value = "2026-10-01";
assert.strictEqual(checkWindow(), false);
assert.strictEqual($("#windowmsg").dataset.tone, "err");
assert.strictEqual($("#windowmsg").textContent,
  "Das Ende des Fensters liegt vor dem Anfang.");

$("#to").value = "2026-11-30";
assert.strictEqual(checkWindow(), true);
assert.strictEqual($("#windowmsg").textContent, "");
assert.strictEqual($("#windowmsg").dataset.tone, "");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_stay_maximum_follows_the_minimum_upwards():
    result = run_ui_assertion(
        r"""
assert.deepStrictEqual(clampStay([9, 4]), [9, 9]);
assert.deepStrictEqual(clampStay([2, 8]), [2, 8]);
assert.deepStrictEqual(clampStay([-3, 900]), [0, 60]);
assert.deepStrictEqual(clampStay(["", ""]), [0, 0]);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_profile_name_enter_saves_and_never_starts_a_search():
    result = run_ui_assertion(
        r"""
let saved = false;
let prevented = false;
saveProfileNow = async () => { saved = true; };

await profileNameKeydown({
  key: "Enter",
  preventDefault(){ prevented = true; },
});

assert.strictEqual(prevented, true);
assert.strictEqual(saved, true);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_a_trip_without_a_stay_has_no_stay_fields_at_all():
    """Nicht abgeschaltete Felder, sondern gar keine: sonst bleiben sie im Tab-Lauf."""
    result = run_ui_assertion(
        r"""
trip = "return";
hops = [{code:"BER", label:"Berlin"}, {code:"ATH", label:"Athen"}];
syncStayControls();
assert.strictEqual(stayCount(), 1);
assert.ok($("#staylist").innerHTML.includes("stay-min-0"), $("#staylist").innerHTML);
assert.strictEqual($("#stayrow").classList.contains("off"), false);

setTrip("one_way");
assert.strictEqual(stayCount(), 0);
assert.strictEqual($("#staylist").innerHTML, "");
assert.strictEqual($("#stayrow").classList.contains("off"), true);
assert.deepStrictEqual(payload().stays, []);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_date_fields_never_offer_a_day_in_the_past():
    source = APP_JS.read_text(encoding="utf-8")

    assert '$("#from").min = todayIso();' in source
    assert '$("#to").min = todayIso();' in source


def test_page_head_names_the_product_and_carries_an_inline_favicon():
    page = INDEX.read_text(encoding="utf-8")

    assert "<title>flightopt: Multi-Stop-Flugpreise</title>" in page
    assert '<meta name="description"' in page
    assert 'rel="icon" href="data:image/svg+xml' in page


def live_region_ids(page: str) -> set[str]:
    """Jedes Element, das von sich aus spricht: `aria-live` oder `role="status"`."""
    found = set()
    for tag in re.findall(r"<[^>]+>", page):
        if 'aria-live="polite"' not in tag and 'role="status"' not in tag:
            continue
        match = re.search(r'id="([^"]+)"', tag)
        found.add(match.group(1) if match else tag)
    return found


def test_live_regions_sit_only_where_they_belong():
    page = INDEX.read_text(encoding="utf-8")

    # Genau diese Elemente sprechen, kein Element mehr. Die Ergebnisliste selbst
    # ist keine Live-Region, sonst liest sie sich bei jeder Sortierung neu.
    # `aiStatus`, `savedmsg`, `scanstate`, `watchmsg` und `huntmsg` sind die
    # Antwort auf einen Knopf, den jemand gerade gedrueckt hat: ohne Ansage
    # bleibt der Erfolg stumm. Die Zusammenfassung der Beobachtungsliste
    # spricht dagegen nicht, genauso wenig wie die der gespeicherten Scans und
    # die der Jagd: sie wird bei jedem Neuladen der Liste geschrieben, auch
    # ohne Zutun. Der Kanalstand spricht ebenfalls nicht: er aendert sich nur,
    # wenn jemand am Server eine Umgebungsvariable setzt.
    assert live_region_ids(page) == {
        "voiceStatus", "progresslabel", "note", "outsummary",
        "aiStatus", "savedmsg", "scanstate", "watchmsg", "huntmsg",
    }
    assert '<section class="log" id="log" aria-label="Verlauf der Suche">' in page
    assert '<h2 id="outtitle" tabindex="-1">' in page
    assert '<section class="out" id="out">' in page
    assert '<p class="outsummary" id="outsummary" aria-live="polite"></p>' in page


def test_route_field_is_a_combobox_with_an_active_option():
    result = run_ui_assertion(
        r"""
const marks = comboboxAttributes(0);

assert.strictEqual(marks.role, "combobox");
assert.strictEqual(marks.listboxId, "ac-0");
assert.strictEqual(marks.optionId(2), "ac-0-opt-2");
assert.strictEqual(activeDescendant(0, -1), "");
assert.strictEqual(activeDescendant(0, 2), "ac-0-opt-2");

assert.strictEqual(typeof focusResults, "function");
focusResults();
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_empty_suggestion_note_stays_outside_the_listbox():
    """Ein <p> als Kind von role="listbox" ist kein erlaubtes Kind."""
    result = run_ui_assertion(
        r"""
(async () => {
  const made = [];
  const create = document.createElement;
  document.createElement = tag => { const node = create(tag); made.push(node); return node; };

  trip = "return";
  hops = [{code:"", label:""}, {code:"", label:""}];
  drawRoute();
  document.createElement = create;

  const input = made.find(n => n.tagName === "INPUT");
  const boxes = made.filter(n => n.className === "ac");
  const menu = boxes[0];
  const empty = boxes[1];

  global.fetch = async () => ({ok: true, json: async () => ({results: []})});

  input.value = "Zzz";
  input.oninput();
  await new Promise(done => setTimeout(done, 400));

  assert.strictEqual(menu.attributes["role"], "listbox");
  assert.strictEqual(menu.innerHTML, "");
  assert.ok(empty.innerHTML.includes("Kein Flughafen gefunden"), empty.innerHTML);
  // Der Kasten ist kein Listeneintrag, sondern eine Statusmeldung am Feld.
  assert.strictEqual(empty.attributes["role"], "status");
  assert.strictEqual(empty.id, "ac-0-empty");
  assert.strictEqual(input.attributes["aria-describedby"], "ac-0-empty");
  assert.strictEqual(input.attributes["aria-expanded"], "false");
  assert.strictEqual(input.attributes["aria-controls"], "ac-0");
  assert.strictEqual(input.attributes["role"], "combobox");
})().catch(err => { console.error(err); process.exit(1); });
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_group_entries_name_the_airports_they_stand_for():
    result = run_ui_assertion(
        r"""
assert.strictEqual(
  groupLabel({kind:"group", city:"London", airports:["LHR","LGW","STN"]}),
  "London (LHR, LGW, STN)"
);
// Traegt die Gruppe einen eigenen Namen, steht er mit in der Zeile.
assert.strictEqual(
  groupLabel({kind:"group", city:"Ostflughäfen", name:"Berlin, Leipzig/Halle, Dresden",
              airports:["BER","LEJ","DRS"]}),
  "Ostflughäfen: Berlin, Leipzig/Halle, Dresden (BER, LEJ, DRS)"
);
assert.strictEqual(
  groupLabel({city:"Berlin", name:"Berlin Brandenburg"}),
  "Berlin · Berlin Brandenburg"
);
assert.strictEqual(groupLabel({city:"Athen", name:"Athen"}), "Athen");
assert.strictEqual(groupLabel(null), "");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_escape_closes_the_empty_box_too():
    """Ohne Treffer steht nur der Leerkasten offen. Escape muss ihn trotzdem schliessen."""
    result = run_ui_assertion(
        r"""
(async () => {
  const made = [];
  const create = document.createElement;
  document.createElement = tag => { const node = create(tag); made.push(node); return node; };

  trip = "return";
  hops = [{code:"", label:""}, {code:"", label:""}];
  drawRoute();
  document.createElement = create;

  const input = made.find(n => n.tagName === "INPUT");
  const boxes = made.filter(n => n.classList.contains("ac"));
  const menu = boxes[0], empty = boxes[1];

  global.fetch = async () => ({ok: true, json: async () => ({results: []})});

  input.value = "Zzz";
  input.oninput();
  await new Promise(done => setTimeout(done, 400));

  assert.strictEqual(empty.classList.contains("on"), true);
  assert.strictEqual(menu.classList.contains("on"), false);
  assert.strictEqual(input.attributes["aria-describedby"], "ac-0-empty");

  input.onkeydown({key:"Escape", preventDefault(){}});

  assert.strictEqual(empty.classList.contains("on"), false);
  assert.strictEqual(menu.classList.contains("on"), false);
  assert.strictEqual(input.attributes["aria-expanded"], "false");
  assert.strictEqual(input.attributes["aria-describedby"], "");
})().catch(err => { console.error(err); process.exit(1); });
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_result_summary_speaks_one_line_instead_of_the_whole_table():
    result = run_ui_assertion(
        r"""
assert.strictEqual(resultSummaryLine(0, 0, 0), "");
assert.strictEqual(resultSummaryLine(0, 4, 0),
  "Keine Variante passt zu den aktuellen Filtern.");
// Preise in der Copy stehen deutsch: Komma als Trenner, Punkt als Tausender.
assert.strictEqual(resultSummaryLine(4, 4, 219.5),
  "4 Varianten sichtbar, günstigste ab 219,50 €");
assert.strictEqual(resultSummaryLine(1, 4, 219.5),
  "1 von 4 Varianten sichtbar, günstigste ab 219,50 €");
assert.strictEqual(resultSummaryLine(1, 4, 1234.5),
  "1 von 4 Varianten sichtbar, günstigste ab 1.234,50 €");

$("#resultCarrier").value = "";
$("#resultQuality").value = "";
$("#sort").value = "price";
renderTable([
  {total: 300, verified: true, route: "", dates: ["2026-10-01"],
   legs: [{origin:"BER", destination:"ATH", date:"2026-10-01", price: 300, carriers:["FR"]}]},
  {total: 220, verified: false, route: "", dates: ["2026-10-02"],
   legs: [{origin:"BER", destination:"ATH", date:"2026-10-02", price: 220, carriers:["FR"]}]},
]);

assert.strictEqual($("#outsummary").textContent,
  "2 Varianten sichtbar, günstigste ab 220,00 €");

renderTable([]);
assert.strictEqual($("#outsummary").textContent, "");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_focus_never_jumps_out_of_a_field_someone_is_typing_in():
    result = run_ui_assertion(
        r"""
let focused = 0;
$("#outtitle").focus = () => { focused += 1; };
document.body = {tagName:"BODY"};

document.activeElement = document.body;
assert.strictEqual(focusResults(), true);
assert.strictEqual(focused, 1);

document.activeElement = $("#go");
assert.strictEqual(focusResults(), true);
assert.strictEqual(focused, 2);

// Der Abbrechen-Knopf wird nach dem Lauf versteckt; dort haelt niemand den Fokus.
document.activeElement = $("#cancel");
assert.strictEqual(focusResults(), true);
assert.strictEqual(focused, 3);

// Wer gerade in einem Textfeld steht, behaelt seinen Cursor.
document.activeElement = $("#profileName");
assert.strictEqual(focusResults(), false);
assert.strictEqual(focused, 3);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_typing_a_two_digit_maximum_is_not_eaten_by_the_clamp():
    """Nach der ersten Ziffer waere "1" ungueltig; ein sofortiger Fix frisst die zweite."""
    result = run_ui_assertion(
        r"""
trip = "return";
hops = [{code:"BER", label:"Berlin"}, {code:"ATH", label:"Athen"}];
stayRanges = [[3, 10]];
syncStayControls();

$("#stay-min-0").value = "3";
$("#stay-max-0").value = "1";
readStayControls();
assert.strictEqual($("#stay-max-0").value, "1");

$("#stay-max-0").value = "12";
readStayControls();
assert.strictEqual($("#stay-max-0").value, "12");
assert.deepStrictEqual(stayRanges[0], [3, 12]);
assert.deepStrictEqual(payload().stays, [[3, 12]]);

// Beim Verlassen des Feldes rueckt ein zu kleines Maximum dann doch nach.
$("#stay-max-0").value = "1";
commitStayControls();
assert.strictEqual($("#stay-max-0").value, "3");

// Das Minimum wird genauso geklemmt und muss genauso zurueckgeschrieben werden.
$("#stay-min-0").value = "99";
readStayControls();
assert.strictEqual($("#stay-min-0").value, "99");
commitStayControls();
assert.strictEqual($("#stay-min-0").value, "60");
assert.strictEqual($("#stay-max-0").value, "60");
assert.deepStrictEqual(stayRanges[0], [60, 60]);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_today_is_the_local_day_not_the_utc_day():
    result = run_ui_assertion(
        r"""
const now = new Date();
const pad = n => String(n).padStart(2, "0");
assert.strictEqual(todayIso(),
  `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`);
assert.strictEqual(isoDay(new Date(2026, 0, 5)), "2026-01-05");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_end_date_keeps_a_static_minimum():
    """Ein mitwanderndes `min` doppelt die Meldung mit der Browser-Blase."""
    source = APP_JS.read_text(encoding="utf-8")

    assert 'new Date().toISOString()' not in source
    assert '$("#to").min = $("#from")' not in source
    assert '$("#to").min = todayIso();' in source


def test_trip_picker_is_a_radio_group_not_a_tablist():
    page = INDEX.read_text(encoding="utf-8")
    result = run_ui_assertion(
        r"""
trip = "multi";
// Ein leerer Kasten erzwingt den Neuaufbau; sonst wird nur die Marke umgesetzt.
$("#tripoptions").innerHTML = "";
drawTrips();
const html = $("#tripoptions").innerHTML;

assert.ok(html.includes('type="radio"'), html);
assert.ok(html.includes('name="trip"'), html);
assert.ok(html.includes('value="multi" checked'), html);
assert.ok(!html.includes('role="tab"'), html);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert 'role="tablist"' not in page
    assert '<div class="segset" id="tripoptions"></div>' in page


def test_switching_the_trip_type_keeps_the_radio_focused():
    """Ein Neuaufbau der Gruppe wirft genau das Radio weg, das gerade gemeldet hat."""
    result = run_ui_assertion(
        r"""
document.body = {tagName:"BODY"};
document.activeElement = document.body;

const box = $("#tripoptions");
const before = [...box.querySelectorAll("input")];
assert.strictEqual(before.length, 3, box.innerHTML);

// So kommt der Wechsel im Browser an: das Radio hat den Fokus, dann feuert change.
const multi = before.find(r => r.value === "multi");
multi.focus();
multi.checked = true;
multi.onchange();

const after = [...box.querySelectorAll("input")];
assert.strictEqual(after.length, 3);
after.forEach((r, i) => assert.strictEqual(r, before[i], "Radio " + i + " neu gebaut"));
assert.strictEqual(document.activeElement, multi);

assert.strictEqual(trip, "multi");
assert.strictEqual(after.find(r => r.value === "multi").checked, true);
assert.strictEqual(after.find(r => r.value === "return").checked, false);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_airline_disclosure_names_how_many_are_covered():
    result = run_ui_assertion(
        r"""
AIRLINES = [
  {code:"FR", status:"live", kind:"airline", name:"Ryanair"},
  {code:"W6", status:"live", kind:"airline", name:"Wizz Air"},
  {code:"KIWI", status:"live", kind:"comparison", name:"Kiwi (Vergleich)"},
  {code:"TK", status:"planned", kind:"airline", name:"Turkish Airlines"},
];

picked = new Set();
assert.strictEqual(airlinesSummary(), "Alle 2 Airlines");

picked = new Set(["FR"]);
assert.strictEqual(airlinesSummary(), "Eingegrenzt auf FR");

airHint();
assert.strictEqual($("#airlinescount").textContent, "Eingegrenzt auf FR");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_max_stops_rides_along_in_the_search_payload():
    """Teilprojekt A wertet das Feld aus; die UI muss es bis dahin sauber senden."""
    result = run_ui_assertion(
        r"""
$("#maxStops").value = "";
assert.strictEqual(payload().max_stops, null);

$("#maxStops").value = "0";
assert.strictEqual(payload().max_stops, 0);

$("#maxStops").value = "2";
assert.strictEqual(payload().max_stops, 2);
assert.strictEqual(profilePayload().max_stops, 2);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_form_is_ordered_by_weight():
    """Route zuerst, Freitext und Optionen eingeklappt, Profil unter den Ergebnissen."""
    page = INDEX.read_text(encoding="utf-8")

    order = [
        '<div class="segset" id="tripoptions"></div>',
        '<div class="route" id="route"',
        '<details class="fold" id="freetext">',
        '<input type="date" id="from" required aria-describedby="windowmsg">',
        '<div class="stayrow" id="stayrow">',
        '<details class="fold" id="optionsdetails">',
        '<select id="maxStops">',
        '<details class="fold" id="airlinesdetails">',
        '<button class="go" id="go" type="submit">Suchen</button>',
        '</form>',
        '<section class="out" id="out">',
        '<section class="deals" id="deals">',
        '<input id="profileName" type="text"',
        '<div class="scanbar">',
    ]
    seen = [page.index(mark) for mark in order if mark in page]
    assert len(seen) == len(order), [m for m in order if m not in page]
    assert seen == sorted(seen), order

    # Profilname steht ausserhalb des Formulars: Enter kann dort keine Suche mehr starten.
    assert page.index('<input id="profileName"') > page.index("</form>")
    # Der alte Reiter-Block und die alte Airline-Sektion sind weg.
    assert '<div class="tabs"' not in page
    assert '<div class="air">' not in page


def test_visible_copy_has_no_arrows_and_no_dashes():
    page = INDEX.read_text(encoding="utf-8")
    script = APP_JS.read_text(encoding="utf-8")
    style = APP_CSS.read_text(encoding="utf-8")

    for forbidden in ("\u2192", "\u2014", "\u2013"):
        assert forbidden not in page, f"verbotenes Zeichen im Markup: {forbidden!r}"
        assert forbidden not in script, f"verbotenes Zeichen im Skript: {forbidden!r}"
        # `content:` und Kommentare im Stylesheet landen genauso auf dem Schirm.
        assert forbidden not in style, f"verbotenes Zeichen im Stil: {forbidden!r}"


def test_result_labels_speak_plain_german():
    result = run_ui_assertion(
        r"""
assert.strictEqual(stopsLabel(0), "Direktflug");
assert.strictEqual(stopsLabel(1), "1 Umstieg");
assert.strictEqual(stopsLabel(2), "2 Umstiege");
assert.strictEqual(stopsLabel(null), "");
assert.strictEqual(stopsLabel(undefined), "");

assert.strictEqual(arrivalLabel("2026-03-11", "2026-03-12"), "an 12.03.");
assert.strictEqual(arrivalLabel("2026-03-11", "2026-03-11"), "");
assert.strictEqual(arrivalLabel("2026-03-11", null), "");

// Das Backend schickt den Originalpreis in ganzen Einheiten, nicht in Cent.
assert.strictEqual(
  nativePriceLabel({price_native: {amount: 48500, currency: "JPY"}}),
  "umgerechnet aus 48.500 JPY"
);
assert.strictEqual(
  nativePriceLabel({price_native: {amount: 129, currency: "USD"}}),
  "umgerechnet aus 129,00 USD"
);
assert.strictEqual(nativePriceLabel({}), "");
assert.strictEqual(nativePriceLabel(null), "");

assert.strictEqual(statusLabel({verified: true, legs: []}), "geprüft");
assert.strictEqual(statusLabel({verified: false, legs: [{indicative: true}]}), "Richtwert");
assert.strictEqual(statusLabel({verified: false, legs: [{indicative: false}]}), "Schätzung");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_nights_and_stops_are_read_from_the_payload():
    result = run_ui_assertion(
        r"""
const o = {
  dates: ["2026-10-01", "2026-10-08", "2026-10-15"],
  legs: [{stops: 1}, {stops: 0}, {stops: 2}],
};

assert.strictEqual(nightsOf(o), 14);
assert.strictEqual(stopsOf(o), 3);
assert.strictEqual(stopsLabel(stopsOf(o)), "3 Umstiege");

// Solange eine Quelle keine Umstiege meldet, wird nichts behauptet.
assert.strictEqual(stopsOf({dates: ["2026-10-01"], legs: [{stops: null}]}), null);
assert.strictEqual(stopsLabel(stopsOf({dates: [], legs: []})), "");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_partial_rows_fill_the_table_and_verified_rows_replace_them():
    result = run_ui_assertion(
        r"""
function row(dates, total, extra){
  return Object.assign({
    route: "BER-ATH-BER", dates, total, currency: "EUR",
    verified: false, estimate: total, drift: null,
    legs: [
      {origin:"BER", destination:"ATH", date:dates[0], price:total/2, stops:0},
      {origin:"ATH", destination:"BER", date:dates[1], price:total/2, stops:0},
    ],
  }, extra || {});
}

$("#resultCarrier").value = "";
$("#resultQuality").value = "";
$("#sort").value = "price";
lastResults = [];
applyPartial([
  row(["2026-10-01","2026-10-05"], 210),
  row(["2026-10-01","2026-10-06"], 230),
]);

assert.strictEqual(lastResults.length, 2);
assert.deepStrictEqual(lastResults.map(r => r.rank), [1, 2]);
assert.strictEqual(lastResults[0].verified, false);
assert.strictEqual(statusLabel(lastResults[0]), "Schätzung");

// Der zweite Kandidat wird geprueft und faellt dabei unter den ersten.
applyVerified(row(["2026-10-01","2026-10-06"], 180, {verified: true, drift: -50}));

assert.strictEqual(lastResults.length, 2);
assert.strictEqual(lastResults[0].total, 180);
assert.strictEqual(lastResults[0].verified, true);
assert.strictEqual(lastResults[0].rank, 1);
assert.strictEqual(statusLabel(lastResults[0]), "geprüft");
assert.strictEqual(lastResults[1].total, 210);

// Eine spaet eintreffende Schaetzung darf ein geprueftes Ergebnis nicht ueberschreiben.
applyPartial([row(["2026-10-01","2026-10-06"], 230)]);

assert.strictEqual(lastResults[0].total, 180);
assert.strictEqual(lastResults[0].verified, true);
// Preise stehen in der Tabelle deutsch.
assert.ok($("#rows").innerHTML.includes("180,00"), $("#rows").innerHTML);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_an_unverified_leg_keeps_its_booking_link():
    """Der Link haengt am Angebot, nicht am Pruefstand.

    Ein Tagesangebot ohne Flugzeiten ist nicht live geprueft und hatte trotzdem
    einen Link. Der Zweig warf ihn weg und schrieb ein leeres <span> hin: genau
    dort, wo der Link das einzige ist, was weiterhilft.
    """
    result = run_ui_assertion(
        r"""
const html = detailRows({
  verified: false, drift: null, estimate: 120,
  legs: [{origin:"BER", destination:"ATH", date:"2026-10-01", price:120,
          verified:false, indicative:false, source:"ryanair",
          deep_link:"https://example.invalid/buchen"}],
});

assert.ok(html.includes('href="https://example.invalid/buchen"'), html);
assert.ok(html.includes(">buchen</a>"), html);

// Ein Kalendertag ohne Angebot hat keinen Link und behaelt die leere Spalte.
const bare = detailRows({
  verified: false, legs: [{origin:"BER", destination:"ATH", date:"2026-10-01",
                           price:120, verified:false}],
});
assert.ok(!bare.includes("<a href"), bare);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_a_live_offer_without_times_is_not_called_a_day_best_price():
    """Drei Faelle, nicht zwei. Ein geliefertes Angebot ohne Flugzeiten ist
    etwas anderes als ein blosser Tagesbestpreis aus dem Kalender."""
    result = run_ui_assertion(
        r"""
assert.strictEqual(unverifiedNote({indicative: true}),
  "Richtwert eines Vergleichsportals, echter Flugpreis liegt meist darunter");
assert.strictEqual(unverifiedNote({}),
  "Tagesbestpreis, kein konkreter Flug geprüft");
// Ohne Link bleibt es ein Tagesbestpreis: eine Quelle allein liefert noch
// kein Angebot, auf das jemand klicken koennte.
assert.strictEqual(unverifiedNote({source: "ryanair"}),
  "Tagesbestpreis, kein konkreter Flug geprüft");
assert.strictEqual(
  unverifiedNote({source: "ryanair", deep_link: "https://example.invalid/x"}),
  "Angebot der Quelle mit Buchungslink, aber ohne Flugzeiten und nicht live nachgeprüft"
);
// Ein Richtwert bleibt ein Richtwert, auch mit Link.
assert.strictEqual(
  unverifiedNote({indicative: true, source: "kiwi", deep_link: "https://x.invalid"}),
  "Richtwert eines Vergleichsportals, echter Flugpreis liegt meist darunter"
);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_result_area_is_a_table_with_a_sticky_head():
    page = INDEX.read_text(encoding="utf-8")
    css = (WEB / "app.css").read_text(encoding="utf-8")

    assert '<table class="kursbuch" id="resulttable">' in page
    assert '<tbody id="rows"></tbody>' in page
    assert '<ol id="list"></ol>' not in page
    assert "table.kursbuch thead th{position:sticky" in css
    assert "grid-template-rows:0fr" in css


def test_leg_meters_render_one_bar_per_leg():
    result = run_ui_assertion(
        r"""
const html = legMeters([
  {origin:"BER", destination:"NRT", dates: 61},
  {origin:"NRT", destination:"ICN", dates: 0},
]);

assert.ok(html.includes('BER-NRT'));
assert.ok(html.includes('NRT-ICN'));
// Der laengste Balken ist voll, ein Leg ohne Preise wird als Null markiert.
assert.ok(html.includes('style="width:100%"'));
assert.ok(html.includes('style="width:0%"'));
assert.ok(html.includes('legmeter zero'));
assert.ok(html.includes('data-days="61"'));
// Kein Pfeil in der Beschriftung.
assert.ok(!html.includes("\u2192"));

assert.strictEqual(legMeters([]), "");
assert.strictEqual(legMeters(null), "");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_count_up_lands_on_the_target_without_animation_support():
    result = run_ui_assertion(
        r"""
// Im Harness gibt es weder matchMedia noch requestAnimationFrame: der Wert
// steht sofort, genau wie bei prefers-reduced-motion.
assert.strictEqual(motionOn(), false);

const cell = $("#legdaystest");
cell.textContent = "0";
assert.strictEqual(countUp(cell, 61), 61);
assert.strictEqual(cell.textContent, "61");
assert.strictEqual(countUp(cell, "nicht zahl"), 0);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_direct_only_filter_drops_rows_with_stops():
    result = run_ui_assertion(
        r"""
const rows = [
  {total: 100, verified: true, dates:["2026-10-01"], legs: [{carriers:["FR"], indicative:false, stops:0}]},
  {total: 120, verified: true, dates:["2026-10-02"], legs: [{carriers:["FR"], indicative:false, stops:1}]},
  {total: 140, verified: true, dates:["2026-10-03"], legs: [{carriers:["FR"], indicative:false}]},
];

$("#resultCarrier").value = "";
$("#resultQuality").value = "";
$("#directOnly").checked = false;
assert.strictEqual(filterResults(rows).length, 3);

$("#directOnly").checked = true;
assert.deepStrictEqual(filterResults(rows), [rows[0]]);
$("#directOnly").checked = false;
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_motion_is_opt_in_and_hover_does_not_flicker():
    css = (WEB / "app.css").read_text(encoding="utf-8")

    assert "prefers-reduced-motion:no-preference" in css
    assert "prefers-reduced-motion:reduce" not in css
    assert "transition:background .12s" in css
    assert "transition:grid-template-rows" in css

    # Jede Laengen- und Breitenbewegung gehoert in den Opt-in-Block. Ausserhalb
    # laeuft sie auch dann, wenn jemand Bewegung abbestellt hat.
    opt_in = css.split("prefers-reduced-motion:no-preference", 1)[1]
    for moving in ("transition:width .22s ease", "animation:sweep", "animation:fade"):
        assert moving in opt_in, moving
        assert moving not in css.split("prefers-reduced-motion:no-preference", 1)[0]


def test_deals_summary_counts_what_is_below_the_usual_price():
    result = run_ui_assertion(
        r"""
assert.strictEqual(dealsSummary([]), "Noch keine gespeicherten Scans.");
assert.strictEqual(dealsSummary(null), "Noch keine gespeicherten Scans.");
assert.strictEqual(
  dealsSummary([{signal:"normal"}]),
  "1 Scan, keiner unter dem üblichen Preis."
);
assert.strictEqual(
  dealsSummary([{signal:"cheap"}, {signal:"normal"}]),
  "2 Scans, einer unter dem üblichen Preis."
);
assert.strictEqual(
  dealsSummary([{signal:"cheap"}, {signal:"cheap"}, {signal:"expensive"}]),
  "3 Scans, davon 2 unter dem üblichen Preis."
);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_deviation_and_signal_labels_stay_in_german():
    result = run_ui_assertion(
        r"""
assert.strictEqual(deviationLabel(-27.3), "-27,3 %");
assert.strictEqual(deviationLabel(12), "+12,0 %");
assert.strictEqual(deviationLabel(0), "0,0 %");
assert.strictEqual(deviationLabel(null), "");
assert.strictEqual(deviationLabel(undefined), "");

assert.strictEqual(signalLabel("cheap"), "günstig");
assert.strictEqual(signalLabel("expensive"), "teuer");
assert.strictEqual(signalLabel("normal"), "normal");
// Dasselbe Wort wie in der Preislage-Spalte: zwei Namen fuer denselben Zustand
// zwingen sonst jeden, sie einmal gegeneinander zu pruefen.
assert.strictEqual(signalLabel("unknown"), "keine Basis");
assert.strictEqual(signalLabel("unknown"), bandLabel(null));
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_deal_row_carries_the_job_for_the_click():
    result = run_ui_assertion(
        r"""
const html = dealRowMarkup({
  job_id: 42, profile: "Athen Oktober", route: "BER-ATH-BER",
  scanned_at: "2026-09-06T07:00:00", price: 160, currency: "EUR",
  median: 220, deviation_pct: -27.3, signal: "cheap",
});

assert.ok(html.includes('data-job="42"'));
assert.ok(html.includes("Athen Oktober"));
assert.ok(html.includes("BER-ATH-BER"));
// Preise stehen deutsch, auch hier.
assert.ok(html.includes("160,00"));
assert.ok(html.includes("-27,3 %"));
assert.ok(html.includes("günstig"));
assert.ok(!html.includes("\u2192"));
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_opening_a_deal_loads_the_stored_job_into_the_result_table():
    result = run_ui_assertion(
        r"""
let asked = null;
global.fetch = async url => {
  asked = url;
  return {ok: true, json: async () => ({
    status: "done",
    results: [{
      rank: 1, route: "BER-ATH-BER",
      dates: ["2026-10-01", "2026-10-06"],
      total: 160, currency: "EUR", verified: true, estimate: 160, drift: 0,
      legs: [
        {origin:"BER", destination:"ATH", date:"2026-10-01", price:79, verified:true, stops:0},
        {origin:"ATH", destination:"BER", date:"2026-10-06", price:81, verified:true, stops:0},
      ],
    }],
  })};
};

$("#resultCarrier").value = "";
$("#resultQuality").value = "";
$("#directOnly").checked = false;
lastResults = [];
await openDeal(42);

assert.strictEqual(asked, "/api/jobs/42");
assert.strictEqual(lastResults.length, 1);
assert.strictEqual(lastResults[0].total, 160);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_a_hidden_stop_from_another_trip_type_never_rides_along():
    """`hops` merkt sich alles Getippte. Gesucht wird nur, was sichtbar ist."""
    result = run_ui_assertion(
        r"""
trip = "multi";
hops = [
  {code:"BER", label:"Berlin"},
  {code:"IST", label:"Istanbul"},
  {code:"BER", label:"Berlin"},
];
assert.deepStrictEqual(payload().airports, ["BER", "IST", "BER"]);

// Zurueck auf die Rueckreise: der dritte Stopp bleibt gemerkt, aber er wird
// weder mitgeschickt noch als fehlende Eingabe angemahnt.
setTrip("return");
assert.strictEqual(hops.length, 3);
assert.deepStrictEqual(payload().airports, ["BER", "IST"]);

// Ein halb getippter Stopp aus der Mehr-Stopp-Ansicht blockiert die Suche nicht.
hops.push({code:"", label:"Athe"});
assert.strictEqual(active().some(h => !h.code), false);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_a_search_without_a_hit_says_so_instead_of_vanishing():
    result = run_ui_assertion(
        r"""
renderNoResults("Keine Reise gefunden.");

assert.strictEqual($("#outtitle").textContent, "Keine Treffer");
assert.strictEqual($("#outsummary").textContent, "Keine Reise gefunden.");
assert.ok($("#rows").innerHTML.includes("Keine Reise gefunden."), $("#rows").innerHTML);
assert.strictEqual($("#out").classList.contains("on"), true);
assert.deepStrictEqual(lastResults, []);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_an_open_row_survives_a_verified_result_arriving():
    """Waehrend des Nachpruefens wird die Tabelle neu geschrieben. Ein geoeffnetes
    Detail darf dabei nicht zuklappen, und alte Zeilen blenden nicht neu ein."""
    result = run_ui_assertion(
        r"""
function row(dates, total, extra){
  return Object.assign({
    route: "BER-ATH-BER", dates, total, currency: "EUR", verified: false,
    legs: [{origin:"BER", destination:"ATH", date:dates[0], price:total, stops:0}],
  }, extra || {});
}

$("#resultCarrier").value = ""; $("#resultQuality").value = "";
$("#directOnly").checked = false; $("#sort").value = "price";
lastResults = []; openRows = new Set(); seenRows = new Set();

applyPartial([row(["2026-10-01","2026-10-05"], 210),
              row(["2026-10-01","2026-10-06"], 230)]);
// Beim ersten Auftauchen blendet jede Zeile ein.
assert.ok($("#rows").innerHTML.includes("fresh"), $("#rows").innerHTML);

const key = resultKey(lastResults[0]);
openRows.add(key);
applyVerified(row(["2026-10-01","2026-10-05"], 180, {verified: true}));

const html = $("#rows").innerHTML;
assert.ok(html.includes('aria-expanded="true"'), html);
assert.ok(html.includes("detrow open"), html);
// Bekannte Zeilen blenden nicht noch einmal ein.
assert.ok(!html.includes("fresh"), html);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_result_row_stays_a_table_row_and_the_button_does_the_toggling():
    """`role="button"` auf einem <tr> nimmt den Zellen ihren Platz in der Tabelle."""
    result = run_ui_assertion(
        r"""
$("#resultCarrier").value = ""; $("#resultQuality").value = "";
$("#directOnly").checked = false; $("#sort").value = "price";
openRows = new Set(); seenRows = new Set();
renderTable([{
  route: "BER-ATH-BER", dates: ["2026-10-01", "2026-10-06"], total: 220,
  currency: "EUR", verified: true,
  legs: [{origin:"BER", destination:"ATH", date:"2026-10-01", price:220, stops:0}],
}]);

const html = $("#rows").innerHTML;
assert.ok(!html.includes('role="button"'), html);
assert.ok(!/<tr[^>]*tabindex/.test(html), html);
assert.ok(html.includes('class="rowtoggle"'), html);
assert.ok(html.includes('aria-controls="det-0"'), html);
// Der Status steht auch dann noch da, wenn die Spalte auf dem Telefon wegfaellt.
assert.ok(html.includes('class="rowstatus">geprüft'), html);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_total_column_appears_only_with_real_stay_costs():
    result = run_ui_assertion(
        r"""
function row(extra){
  return Object.assign({
    route: "BER-ATH-BER", dates: ["2026-10-01", "2026-10-06"], total: 220,
    currency: "EUR", verified: true,
    legs: [{origin:"BER", destination:"ATH", date:"2026-10-01", price:220, stops:0}],
  }, extra || {});
}
$("#resultCarrier").value = ""; $("#resultQuality").value = "";
$("#directOnly").checked = false; $("#sort").value = "price";

renderTable([row()]);
assert.strictEqual($("#resulttable").classList.contains("withgrand"), false);

renderTable([row({stays: [{code:"ATH", city:"Athen", nights:5, price:300,
                           arrival:"2026-10-01"}], grand_total: 520, stay_total: 300})]);
assert.strictEqual($("#resulttable").classList.contains("withgrand"), true);
// Der reine Uebernachtungsanteil steht in der Detailzeile.
assert.ok(staySummaryLine({stays: [], grand_total: 520, stay_total: 300})
  .includes("300,00 €</b> Übernachtung"));
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_airline_hint_survives_an_empty_or_missing_registry():
    result = run_ui_assertion(
        r"""
AIRLINES = [];
picked = new Set();
airlinesFailed = false;
airHint();
// Kein Satz, der auf einem Doppelpunkt und einem Punkt endet.
assert.ok(!$("#airhint").textContent.includes(": ."), $("#airhint").textContent);
assert.ok($("#airhint").textContent.includes("keine Quelle"), $("#airhint").textContent);

// Eine blockierte Airline heisst blockiert, eine geplante heisst geplant.
AIRLINES = [
  {code:"FR", status:"live", kind:"airline", name:"Ryanair"},
  {code:"TK", status:"planned", kind:"airline", name:"Turkish Airlines"},
  {code:"U2", status:"blocked", kind:"airline", name:"easyJet"},
];
airHint();
const text = $("#airhint").textContent;
assert.ok(text.includes("Angeschlossen wird als nächstes Turkish Airlines."), text);
assert.ok(text.includes("Nicht abfragbar: easyJet."), text);

airlinesFailed = true;
airHint();
assert.strictEqual($("#airlinescount").textContent, "Airlines nicht abrufbar");
assert.ok($("#airhint").textContent.includes("läuft trotzdem"), $("#airhint").textContent);
airlinesFailed = false;
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_partial_failures_of_the_run_reach_the_screen():
    """Erschoepfte Budgets und geblockte Quellen stehen im Ereignis und blieben
    bisher unsichtbar: uebrig war nur ein duenneres Ergebnis."""
    result = run_ui_assertion(
        r"""
resetRunNotes();
assert.strictEqual($("#runnotes").textContent, "");

addRunNotes(["serpapi: Budget von 250 Abrufen ist erschöpft"], "Preisabruf");
assert.strictEqual($("#runnotes").dataset.tone, "warn");
assert.ok($("#runnotes").textContent.includes("Preisabruf: serpapi"),
  $("#runnotes").textContent);

// Dieselbe Meldung zweimal ergibt eine Zeile, nicht zwei.
addRunNotes(["serpapi: Budget von 250 Abrufen ist erschöpft"], "Preisabruf");
assert.strictEqual(runNotes.length, 1);

addRunNotes(["booking: HTTP 403"], "Hotelkosten");
assert.strictEqual(runNotes.length, 2);

assert.strictEqual(skippedNote(0), "");
assert.strictEqual(skippedNote(1), "1 Abruf wurde übersprungen.");
assert.strictEqual(skippedNote(4), "4 Abrufe wurden übersprungen.");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_an_unknown_total_shows_movement_instead_of_a_made_up_percentage():
    result = run_ui_assertion(
        r"""
setProgress({phase:"planning"});
assert.strictEqual($("#progressbar").className, "progressbar pending");
assert.strictEqual($("#progressbar").attributes["aria-valuetext"],
  "Läuft, Umfang noch offen");

setProgress({phase:"fetching", done:3, total:6});
assert.strictEqual($("#progressbar").className, "progressbar");
assert.strictEqual($("#progressbar").attributes["aria-valuetext"], "45 Prozent");

// Ein beendeter Lauf ohne Zaehlung ist fertig, nicht unbestimmt.
setProgress({phase:"done"});
assert.strictEqual($("#progressbar").className, "progressbar");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_filters_that_hide_everything_offer_a_way_back():
    result = run_ui_assertion(
        r"""
$("#resultCarrier").value = ""; $("#resultQuality").value = "";
$("#directOnly").checked = false;
assert.strictEqual(filtersActive(), false);

$("#resultQuality").value = "verified";
assert.strictEqual(filtersActive(), true);
$("#directOnly").checked = true;

resetResultFilters();
assert.strictEqual($("#resultQuality").value, "");
assert.strictEqual($("#directOnly").checked, false);
assert.strictEqual(filtersActive(), false);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_a_saved_scan_names_the_days_it_stands_for():
    result = run_ui_assertion(
        r"""
assert.strictEqual(
  dealDates({dates: ["2026-10-01", "2026-10-06"]}),
  "Do., 01. Okt. bis Di., 06. Okt.");
assert.strictEqual(dealDates({dates: ["2026-10-01"]}), "Do., 01. Okt.");
assert.strictEqual(dealDates({}), "");

assert.strictEqual(dealPriceNote({verified: true, median: 220}),
  "geprüft üblich 220,00 €");
assert.strictEqual(dealPriceNote({verified: false}), "Schätzung");

const html = dealRowMarkup({job_id: 7, profile: "Athen", route: "BER-ATH-BER",
  dates: ["2026-10-01", "2026-10-06"], price: 160, signal: "cheap",
  verified: true, median: 220});
assert.ok(html.includes("Do., 01. Okt."), html);
assert.ok(html.includes('class="dealopen" data-job="7"'), html);
assert.ok(!html.includes('role="button"'), html);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_stops_can_be_reordered_without_a_mouse():
    """Ziehen gibt es auf einem Touchscreen nicht und auf der Tastatur erst recht."""
    result = run_ui_assertion(
        r"""
trip = "multi";
hops = [
  {code:"BER", label:"Berlin"},
  {code:"IST", label:"Istanbul"},
  {code:"ATH", label:"Athen"},
  {code:"BER", label:"Berlin"},
];
drawRoute();

assert.strictEqual(moveHop(1, 1), true);
assert.deepStrictEqual(hops.map(h => h.code), ["BER", "ATH", "IST", "BER"]);
// Istanbul steht jetzt an Position 2 und wandert von dort zurueck.
assert.strictEqual(moveHop(2, -1), true);
assert.deepStrictEqual(hops.map(h => h.code), ["BER", "IST", "ATH", "BER"]);

// Ueber die Enden hinaus bewegt sich nichts.
assert.strictEqual(moveHop(0, -1), false);
assert.strictEqual(moveHop(3, 1), false);
assert.deepStrictEqual(hops.map(h => h.code), ["BER", "IST", "ATH", "BER"]);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_page_carries_the_landmarks_and_names_its_scroll_areas():
    page = INDEX.read_text(encoding="utf-8")
    css = APP_CSS.read_text(encoding="utf-8")

    # Ohne Sprungmarke tabbt sich jeder taeglich durch Kopf und Formular.
    assert '<a class="skip" href="#main">' in page
    assert '<main id="main">' in page and "</main>" in page

    # Ein seitlich scrollbarer Bereich ohne Tabstopp ist mit der Tastatur
    # nicht zu bewegen, und ohne Namen weiss niemand, was dort scrollt.
    # Ergebnisse, gespeicherte Scans, Funde der Jagd, Beobachtungsliste.
    assert page.count('<div class="tablewrap" tabindex="0" role="region"') == 4
    assert ".tablewrap:focus-visible" in css

    # Ein Fortschrittsbalken ohne Namen wird als "Fortschritt" angesagt, sonst nichts.
    assert 'aria-labelledby="progresslabel"' in page
    # Die Schritteleiste wiederholt nur, was das Label schon sagt.
    assert '<div class="steps" id="steps" aria-hidden="true"></div>' in page
    # Die Fehlermeldung zum Fenster haengt an beiden Datumsfeldern.
    assert page.count('aria-describedby="windowmsg"') == 2


def test_the_two_status_columns_are_explained_somewhere_visible():
    """"Status" und "Preislage" nebeneinander erklaeren sich nicht von selbst,
    und ein title-Attribut zeigt ein Telefon nie an."""
    page = INDEX.read_text(encoding="utf-8")
    script = APP_JS.read_text(encoding="utf-8")

    assert '<p class="legend">' in page
    assert "wie sicher der" in page
    assert "keine Basis" in page

    # Die Erklaerung der Zeile steht im aufgeklappten Detail, nicht in `title`.
    assert "Preislage der Zeile:" in script
    assert 'title="${esc(bandTitle(o))}"' not in script


def test_deals_section_replaces_the_admin_block():
    page = INDEX.read_text(encoding="utf-8")

    assert '<section class="deals" id="deals">' in page
    assert '<tbody id="dealsrows"></tbody>' in page
    assert '<p class="msg" id="dealssummary" data-tone=""></p>' in page
    assert '<section class="admin">' not in page
    # Profil und Scanner sitzen in der Kopfzeile dieses Bereichs.
    assert page.index('id="deals"') < page.index('id="profileName"')
    assert page.index('id="deals"') < page.index('id="runScanner"')


def test_watchlist_section_carries_list_form_and_switch():
    page = INDEX.read_text(encoding="utf-8")

    assert '<section class="watch" id="watch">' in page
    assert '<tbody id="watchrows"></tbody>' in page
    assert '<p class="msg" id="watchsummary" data-tone=""></p>' in page
    # Eintragen, ansehen, abschalten: ein Formular, eine Liste, ein Schalter.
    assert '<input id="watchFrom"' in page
    assert '<input id="watchTo"' in page
    assert 'id="watchAdd"' in page
    assert 'id="watchRun"' in page
    # Das Formular steht ausserhalb der Suche, Enter startet dort keine Suche.
    assert page.index('<input id="watchFrom"') > page.index("</form>")


def test_an_empty_watchlist_explains_itself_instead_of_being_blank():
    """Null Zeilen sind eine Aussage, und sie sieht sonst aus wie ein Fehler."""
    result = run_ui_assertion(
        r"""
const text = watchlistSummary({routes: [], summary: {routes: 0, active: 0,
  observations: 0, last_run_at: null, min_days: 5}});

assert.ok(text.includes("Noch wird nichts aufgezeichnet"), text);
assert.ok(text.includes("täglich"), text);
assert.ok(text.includes("5"), text);

renderWatchlist({routes: [], summary: {routes: 0, active: 0, observations: 0,
  last_run_at: null, min_days: 5}});
const body = $("#watchrows").innerHTML;
assert.ok(body.includes("Keine Strecke wird beobachtet"), body);
assert.ok(body.includes("colspan"), body);
assert.strictEqual($("#watchsummary").textContent, text);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_watchlist_summary_counts_routes_and_observations():
    result = run_ui_assertion(
        r"""
const text = watchlistSummary({
  routes: [{id: 1}, {id: 2}],
  summary: {routes: 2, active: 1, observations: 1240,
            last_run_at: "2026-09-05T08:00:00", min_days: 5},
});

assert.ok(text.includes("2 Strecken"), text);
assert.ok(text.includes("1 aktiv"), text);
assert.ok(text.includes("1.240"), text);
assert.ok(text.includes("Zuletzt"), text);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_readiness_says_how_far_the_recording_got():
    """"Ab wann eine Aussage traegt" ist die Frage, die die Spalte beantwortet."""
    result = run_ui_assertion(
        r"""
assert.strictEqual(watchReadiness({days_recorded: 0, min_days: 5}),
  "noch nichts aufgezeichnet");
assert.strictEqual(watchReadiness({days_recorded: 2, min_days: 5}),
  "noch 3 von 5 Tagen");
assert.strictEqual(watchReadiness({days_recorded: 5, min_days: 5, ready: true}),
  "trägt, 5 Tage aufgezeichnet");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_watch_payload_reads_the_watch_form():
    result = run_ui_assertion(
        r"""
$("#watchFrom").value = " berlin ";
$("#watchTo").value = "Athen";
$("#watchLeadMin").value = "10";
$("#watchLeadMax").value = "60";

assert.deepStrictEqual(watchPayload(), {
  origin: "berlin", destination: "Athen", lead_min_days: 10, lead_max_days: 60,
});
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_a_watch_row_carries_the_switch_and_stays_dash_free():
    result = run_ui_assertion(
        r"""
const on = watchRowMarkup({id: 4, route: "BER-ATH", origin: "BER",
  destination: "ATH", lead_min_days: 14, lead_max_days: 73, enabled: true,
  last_run_at: "2026-09-05T08:00:00", observations: 120, days_recorded: 2,
  min_days: 5, ready: false});

assert.ok(on.includes("BER-ATH"), on);
assert.ok(on.includes('data-route="4"'), on);
// Der Knopf sagt, was er tut, nicht in welchem Zustand die Zeile ist.
assert.ok(on.includes("Abschalten"), on);
assert.ok(on.includes("noch 3 von 5 Tagen"), on);
assert.ok(!on.includes("→") && !on.includes("—") && !on.includes("–"), on);

const off = watchRowMarkup({id: 5, route: "BER-FCO", enabled: false,
  last_run_at: null, observations: 0, days_recorded: 0, min_days: 5});

assert.ok(off.includes("Einschalten"), off);
assert.ok(off.includes("aus"), off);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_recording_report_counts_in_german():
    """"1 Strecken" liest sich wie ein Zaehlfehler, und danach sieht es aus."""
    result = run_ui_assertion(
        r"""
assert.strictEqual(watchRunLabel({routes: 1, observations: 8}),
  "1 Strecke abgefragt, 8 Beobachtungen");
assert.strictEqual(watchRunLabel({routes: 3, observations: 180}),
  "3 Strecken abgefragt, 180 Beobachtungen");
assert.strictEqual(watchRunLabel({routes: 0, observations: 0}),
  "Heute ist schon alles aufgezeichnet.");
// Was liegen blieb, gehoert dazu: sonst sieht ein halber Lauf aus wie ein ganzer.
assert.ok(watchRunLabel({routes: 8, observations: 400, due_left: 2})
  .includes("2 bleiben fällig"));
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_watchlist_keeps_its_verdict_column_on_a_phone():
    """`.c-status` faellt unter 760 px global weg, und genau diese Spalte ist
    die, wegen der jemand die Beobachtungsliste ueberhaupt aufschlaegt."""
    css = APP_CSS.read_text(encoding="utf-8")

    assert "#watchtable .c-status{display:table-cell}" in css
    # Der rohe Zaehler wandert dafuer weg: die Aussage sagt dasselbe in Worten.
    assert "#watchtable .c-count" in css


def test_a_watch_label_never_wraps_away_from_its_field():
    """Auf 375 px stand "Nach" am Ende der Zeile ueber seinem eigenen Feld.

    Wer das liest, ordnet die Beschriftung dem falschen Kasten zu.
    """
    page = INDEX.read_text(encoding="utf-8")
    css = APP_CSS.read_text(encoding="utf-8")

    pairs = re.findall(
        r'<span class="watchfield">\s*<label class="lab" for="(\w+)">[^<]+</label>'
        r'\s*<input id="(\w+)"',
        page,
    )
    assert [a for a, _ in pairs] == [b for _, b in pairs]
    assert {a for a, _ in pairs} == {
        "watchFrom", "watchTo", "watchLeadMin", "watchLeadMax"
    }
    assert ".watchfield{display:flex" in css
