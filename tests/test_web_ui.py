"""Browser-form behavior that is too easy to break in the single-file UI."""

from __future__ import annotations

import subprocess
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
    return subprocess.run(
        ["node", "-e", code],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


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
const body = profilePayload();

assert.strictEqual(body.name, "Ost nach Athen");
assert.deepStrictEqual(body.airports, payload().airports);
assert.strictEqual(body.cadence_days, 1);
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
