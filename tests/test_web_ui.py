"""Browser-form behavior that is too easy to break in the single-file UI."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "flightopt" / "web" / "index.html"


def script_from_index() -> str:
    page = INDEX.read_text(encoding="utf-8")
    start = page.index("<script>") + len("<script>")
    end = page.index("</script>", start)
    return page[start:end]


def run_ui_assertion(assertion: str) -> subprocess.CompletedProcess[str]:
    harness = r"""
const assert = require("assert");

class ClassList {
  add() {}
  remove() {}
  toggle() {}
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
    this.disabled = false;
    this.innerHTML = "";
    this.textContent = "";
  }
  append(...items) { this.children.push(...items); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] || ""; }
  querySelectorAll() { return []; }
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
    code = harness + "\n" + script_from_index() + "\n" + assertion
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
$("#smin").value = "2";
$("#smax").value = "3";
if (typeof syncStayControls === "function") syncStayControls();
$("#stay-min-1").value = "7";
$("#stay-max-1").value = "9";

assert.deepStrictEqual(payload().stays, [[2, 3], [7, 9]]);
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
picked = new Set(["FR", "A3"]);

const g = routeGlance();

assert.strictEqual(g.route, "Berlin → Athen");
assert.strictEqual(g.bag, "2 Aufgabegepäckstücke");
assert.strictEqual(g.carriers, "FR, A3");
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
