"""Die Gesamtreise in der Oberflaeche: Spalte, Sortierung, Detailzeile.

Derselbe Node-Harness wie fuer den Rest des Formulars, importiert statt kopiert.
"""

from __future__ import annotations

from pathlib import Path

from tests.test_web_ui import run_ui_assertion

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "flightopt" / "web"


def test_the_switch_rides_along_in_the_search_payload():
    result = run_ui_assertion(
        r"""
$("#withHotels").checked = false;
assert.strictEqual(payload().with_hotels, false);

$("#withHotels").checked = true;
$("#hotelAdults").value = "3";
$("#hotelRooms").value = "2";
const p = payload();
assert.strictEqual(p.with_hotels, true);
assert.strictEqual(p.hotel_adults, 3);
assert.strictEqual(p.hotel_rooms, 2);

// Standard ist zwei Erwachsene in einem Zimmer.
$("#hotelAdults").value = "";
$("#hotelRooms").value = "";
assert.strictEqual(payload().hotel_adults, 2);
assert.strictEqual(payload().hotel_rooms, 1);
$("#withHotels").checked = false;
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_total_column_stays_empty_without_the_switch():
    result = run_ui_assertion(
        r"""
// Ohne Schalter gibt es keine Aufenthalte und damit auch keine Angabe.
assert.strictEqual(grandCell({total: 200}), "");
assert.strictEqual(grandCell(null), "");

// Mit Preis steht die Summe da.
assert.ok(grandCell({total: 200, stays: [], grand_total: 284}).includes("284,00"));

// Mit Luecke steht dort "offen", nicht nichts und schon gar keine Zahl.
const gap = grandCell({total: 200, stays: [{price: null}], grand_total: null});
assert.ok(gap.includes("offen"), gap);
assert.ok(!gap.includes("200,00"), gap);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_a_row_without_a_total_never_sorts_ahead_of_a_complete_one():
    result = run_ui_assertion(
        r"""
const rows = [
  {total: 100, dates:["2026-10-01"], stays:[{price:null}], grand_total: null},
  {total: 300, dates:["2026-10-02"], stays:[{price:60}], grand_total: 360},
  {total: 200, dates:["2026-10-03"], stays:[{price:90}], grand_total: 290},
];

$("#sort").value = "grand";
assert.deepStrictEqual(sortResults(rows).map(r => r.total), [200, 300, 100]);

$("#sort").value = "price";
assert.deepStrictEqual(sortResults(rows).map(r => r.total), [100, 200, 300]);
$("#sort").value = "price";
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_detail_names_city_nights_price_and_source():
    result = run_ui_assertion(
        r"""
const html = stayRowsMarkup({stays: [
  {code:"ATH", city:"Athen", arrival:"2026-10-01", departure:"2026-10-05",
   nights:4, price:84, source:"trivago", name:"Hotel Attalos"},
  {code:"IST", city:"Istanbul", arrival:"2026-10-05", departure:"2026-10-06",
   nights:1, price:null, source:null},
]});

assert.ok(html.includes("Athen"), html);
assert.ok(html.includes("Hotel Attalos"), html);
assert.ok(html.includes("4 Nächte"), html);
// Es ist ein Bestpreis, also heisst er "ab" und nicht "Preis".
assert.ok(html.includes("ab 84,00 €"), html);
assert.ok(html.includes("trivago"), html);
assert.ok(html.includes("1 Nacht"), html);
assert.ok(html.includes("kein Preis"), html);
assert.ok(!html.includes("→"), html);

assert.strictEqual(nightsLabel(1), "1 Nacht");
assert.strictEqual(nightsLabel(4), "4 Nächte");
assert.strictEqual(stayRowsMarkup({}), "");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_detail_footer_says_ab_and_admits_a_gap():
    result = run_ui_assertion(
        r"""
assert.strictEqual(staySummaryLine({total: 200}), "");
assert.ok(staySummaryLine({stays: [], grand_total: 284}).includes("Gesamt ab <b>284,00 €</b>"));
assert.ok(staySummaryLine({stays: [{price:null}], grand_total: null})
  .includes("bleibt die Gesamtsumme offen"));
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_stay_costs_land_on_the_rows_that_are_already_there():
    result = run_ui_assertion(
        r"""
function row(dates, total, extra){
  return Object.assign({
    route: "BER-ATH-BER", dates, total, currency: "EUR",
    verified: true, estimate: total, drift: null,
    legs: [
      {origin:"BER", destination:"ATH", date:dates[0], price:total/2, stops:0},
      {origin:"ATH", destination:"BER", date:dates[1], price:total/2, stops:0},
    ],
  }, extra || {});
}

$("#resultCarrier").value = "";
$("#resultQuality").value = "";
$("#directOnly").checked = false;
$("#sort").value = "price";
lastResults = [];
applyPartial([
  row(["2026-10-01","2026-10-05"], 210),
  row(["2026-10-01","2026-10-06"], 230),
]);
assert.strictEqual($("#rows").innerHTML.includes("offen"), false);

// Der Hotelschritt ergaenzt dieselben Zeilen, er ersetzt sie nicht.
applyStays([
  row(["2026-10-01","2026-10-05"], 210,
      {stays: [{code:"ATH", city:"Athen", arrival:"2026-10-01", nights:4, price:84,
                source:"trivago"}], stay_total: 84, grand_total: 294}),
  row(["2026-10-01","2026-10-06"], 230,
      {stays: [{code:"ATH", city:"Athen", arrival:"2026-10-01", nights:5, price:null,
                source:null}], stay_total: null, grand_total: null}),
]);

assert.strictEqual(lastResults.length, 2);
assert.strictEqual(lastResults[0].grand_total, 294);
assert.strictEqual(lastResults[1].grand_total, null);
// Die Flugpreise bleiben unangetastet.
assert.deepStrictEqual(lastResults.map(r => r.total), [210, 230]);
const html = $("#rows").innerHTML;
assert.ok(html.includes("294,00"), html);
assert.ok(html.includes("offen"), html);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_progress_meter_knows_the_stay_phase():
    result = run_ui_assertion(
        r"""
setProgress({phase:"staying", done:2, total:4, message:"Übernachtungspreise 2/4"});

assert.strictEqual($("#progresslabel").textContent, "Hotelpreise holen");
assert.strictEqual($("#progresscount").textContent, "2 / 4");
const value = Number($("#progressbar").attributes["aria-valuenow"]);
// Nach dem Nachpruefen, vor dem Ende.
assert.ok(value > 90 && value < 100, String(value));
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_form_offers_the_switch_and_the_table_the_column():
    page = (WEB / "index.html").read_text(encoding="utf-8")
    script = (WEB / "app.js").read_text(encoding="utf-8")

    assert '<input type="checkbox" id="withHotels" aria-describedby="hotelhint">' in page
    assert '<input type="number" id="hotelAdults" min="1" max="12" value="2">' in page
    assert '<input type="number" id="hotelRooms" min="1" max="8" value="1">' in page
    # "Gesamt" neben "Preis" liess offen, was da zusammengezaehlt wird.
    assert '<th scope="col" class="c-price c-grand">Mit Hotel</th>' in page
    assert '<option value="grand">Gesamt mit Hotel</option>' in page
    # Ohne Uebernachtungen bleibt die Spalte in jeder Zeile leer. Dann gibt es
    # sie gar nicht erst, sonst kostet sie auf dem Telefon 136 px fuer nichts.
    assert "#resulttable:not(.withgrand) .c-grand{display:none}" in (
        WEB / "app.css"
    ).read_text(encoding="utf-8")
    # Der Schalter steht im Formular, nicht in den Ergebnisfiltern.
    assert page.index('id="withHotels"') < page.index("</form>")

    heads = page.count('<th scope="col"', page.index('id="resulttable"'),
                       page.index('<tbody id="rows">'))
    assert f"const RESULT_COLUMNS = {heads};" in script
