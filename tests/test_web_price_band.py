"""Die Preislage in der Oberflaeche: eigene Spalte, eigene Aussage.

Derselbe Node-Harness wie fuer den Rest des Formulars; er wird importiert und
nicht kopiert, sonst laufen zwei Attrappen des DOM auseinander.
"""

from __future__ import annotations

from pathlib import Path

from tests.test_web_ui import run_ui_assertion

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "flightopt" / "web"


def test_band_labels_name_the_missing_baseline_plainly():
    result = run_ui_assertion(
        r"""
assert.strictEqual(bandLabel("cheap"), "günstig");
assert.strictEqual(bandLabel("normal"), "normal");
assert.strictEqual(bandLabel("expensive"), "teuer");
// In dieser Spalte heisst die fehlende Baseline "keine Basis".
assert.strictEqual(bandLabel("unknown"), "keine Basis");
assert.strictEqual(bandLabel(""), "keine Basis");
assert.strictEqual(bandLabel(undefined), "keine Basis");
// Die Deals-Spalte behaelt ihre eigene, laengere Beschriftung.
assert.strictEqual(signalLabel("unknown"), "keine Baseline");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_row_shows_the_leg_with_the_largest_deviation():
    result = run_ui_assertion(
        r"""
const row = {legs: [
  {origin:"BER", destination:"ATH", date:"2026-10-01", price:99,
   band:{tier:"cheap", deviation_pct:-12.0, median:112.5}},
  {origin:"ATH", destination:"BER", date:"2026-10-05", price:300,
   band:{tier:"expensive", deviation_pct:66.7, median:180}},
]};

const pick = rowBand(row);
assert.strictEqual(pick.tier, "expensive");
assert.strictEqual(pick.leg.origin, "ATH");
// Der Titel sagt, auf welche Teilstrecke sich die Stufe bezieht.
const title = bandTitle(row);
assert.ok(title.includes("ATH-BER"), title);
assert.ok(title.includes("+66,7 %"), title);
assert.ok(title.includes("180,00"), title);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_a_row_without_any_baseline_claims_nothing():
    result = run_ui_assertion(
        r"""
const row = {legs: [
  {origin:"BER", destination:"ATH", date:"2026-10-01", price:99, band:{tier:"unknown"}},
  {origin:"ATH", destination:"BER", date:"2026-10-05", price:120},
]};

assert.strictEqual(rowBand(row).tier, "unknown");
assert.strictEqual(rowBand(row).leg, null);
assert.strictEqual(bandLabel(rowBand(row).tier), "keine Basis");
assert.strictEqual(bandTitle(row), "Für keine Teilstrecke gibt es genug Preishistorie.");
assert.strictEqual(rowBand({}).tier, "unknown");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_detail_row_carries_the_band_of_its_own_leg():
    result = run_ui_assertion(
        r"""
assert.strictEqual(
  legBandNote({band:{tier:"cheap", deviation_pct:-23.4}}),
  "Preislage: günstig, -23,4 %"
);
assert.strictEqual(legBandNote({band:{tier:"normal"}}), "Preislage: normal");
assert.strictEqual(legBandNote({}), "Preislage: keine Basis");
assert.ok(legBandMarkup({band:{tier:"cheap"}}).includes('data-signal="cheap"'));
assert.ok(legBandMarkup({}).includes('data-signal="unknown"'));
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_table_shows_price_band_next_to_the_status_not_instead_of_it():
    result = run_ui_assertion(
        r"""
$("#resultCarrier").value = "";
$("#resultQuality").value = "";
$("#directOnly").checked = false;
$("#sort").value = "price";

renderTable([{
  total: 219.5, currency: "EUR", verified: true, route: "", dates: ["2026-10-01"],
  legs: [{origin:"BER", destination:"ATH", date:"2026-10-01", price:219.5,
          carriers:["FR"], stops:0, band:{tier:"cheap", deviation_pct:-31.2, median:319}}],
}]);

const html = $("#rows").innerHTML;
// Beide Aussagen stehen nebeneinander.
assert.ok(html.includes("geprüft"), html);
assert.ok(html.includes('data-signal="cheap"'), html);
assert.ok(html.includes("günstig"), html);
assert.ok(html.includes("BER-ATH"), html);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_table_head_and_the_colspan_stay_in_step():
    page = (WEB / "index.html").read_text(encoding="utf-8")
    script = (WEB / "app.js").read_text(encoding="utf-8")

    heads = page.count('<th scope="col"', page.index('id="resulttable"'),
                       page.index('<tbody id="rows">'))
    assert '<th scope="col" class="c-status c-band">Preislage</th>' in page
    assert f"const RESULT_COLUMNS = {heads};" in script
    # Kein fest getippter colspan mehr, sonst laeuft er beim naechsten Umbau weg.
    assert 'colspan="7"' not in script
