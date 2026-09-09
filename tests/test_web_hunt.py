"""Die Fehltarif-Jagd in der Oberflaeche: Funde, Kurve, Takt und Kanal.

Derselbe Node-Harness wie fuer den Rest des Formulars; er wird importiert und
nicht kopiert, sonst laufen zwei Attrappen des DOM auseinander.

Die Leitfrage jeder Zusicherung hier ist dieselbe: sieht eine duenne Aussage
auch duenn aus. Dieses Projekt hat eine Reihe von Fehlern hinter sich, bei
denen die Oberflaeche sicherer aussah als die Daten waren.
"""

from __future__ import annotations

from pathlib import Path

from tests.test_web_ui import run_ui_assertion
from tests.test_web_price_band import token_hex

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "flightopt" / "web"
INDEX = WEB / "index.html"
APP_JS = WEB / "app.js"
APP_CSS = WEB / "app.css"


# Ein Fund, wie ihn `GET /api/hunt/finds` liefert. Einmal mit Historie, einmal
# ohne: das sind die beiden Faelle, die nicht gleich aussehen duerfen.
FIXTURES = r"""
const RICH = {
  id: 12, created_at: "2026-09-09T12:00:00", route: "BER-BKK",
  entity_key: "BER|BKK", travel_date: "2026-11-20", tier: "error",
  source: "ryanair", currency: "EUR", price: 39.0, price_minor: 3900,
  median: 500.0, n: 12, population: "estimate",
  reason: "unter 25 Prozent des Medians von 500,00 Euro (n=12) und unter der"
    + " Schranke von 22 Euro fuer 8622 km",
  delivery: "dry_run", delivered_at: null, error: null, acknowledged_at: null,
  booking_url: "https://example.invalid/buchen", distance_km: 8622.0,
  thin: false,
};
const BARE = Object.assign({}, RICH, {
  id: 13, median: null, n: 0, thin: false,
  reason: "39,00 Euro fuer 8622 km, also unter der Schranke von 45 Euro, die"
    + " diese Entfernung auch ohne Historie setzt",
});
const THIN = Object.assign({}, RICH, {id: 14, median: 200.0, n: 6, thin: true});
"""


def test_a_find_without_history_looks_weaker_than_one_with_twenty_prices():
    """Der Kern der ganzen Ansicht.

    Ein Fund, den allein die Entfernungsschranke traegt, ist eine schwaechere
    Aussage als einer mit zwanzig Vergleichspreisen. Sehen beide gleich aus,
    ist die Ansicht eine Behauptung.
    """
    result = run_ui_assertion(
        r"""
assert.strictEqual(findStrength(RICH), "solid");
assert.strictEqual(findStrength(THIN), "thin");
assert.strictEqual(findStrength(BARE), "floor");

// Der Satz nennt die Zahl und die Art der Vergleichspreise.
assert.strictEqual(findBasisLine(RICH), "Üblich 500,00 € aus 12 Schätzpreisen.");
assert.strictEqual(findBasisLine(THIN),
  "Üblich 200,00 € aus 6 Schätzpreisen, dünne Basis.");
// Ohne Historie wird keine Zahl behauptet, sondern gesagt, was fehlt.
assert.strictEqual(findBasisLine(BARE),
  "Keine Vergleichspreise. Diesen Fund trägt allein die Entfernungsschranke.");
assert.ok(!findBasisLine(BARE).includes("0 "), findBasisLine(BARE));

// Und die Marke daneben zaehlt anders, damit der Unterschied auch ohne Lesen
// auffaellt: drei Striche gegen einen.
const marks = f => (strengthMark(f).match(/class="on"/g) || []).length;
assert.strictEqual(marks(RICH), 3);
assert.strictEqual(marks(THIN), 2);
assert.strictEqual(marks(BARE), 1);
// Sie wiederholt nur den Satz und wird deshalb nicht vorgelesen.
assert.ok(strengthMark(BARE).includes('aria-hidden="true"'));
""",
        setup=FIXTURES,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_a_find_row_says_why_it_stands_out():
    result = run_ui_assertion(
        r"""
const html = findRowMarkup(RICH);

assert.ok(html.includes("BER-BKK"), html);
assert.ok(html.includes("Fr., 20. Nov."), html);
assert.ok(html.includes("39,00"), html);
assert.ok(html.includes("ryanair"), html);
assert.ok(html.includes("8.622 km"), html);
assert.ok(html.includes("Üblich 500,00 € aus 12 Schätzpreisen."), html);
// Die Begruendung des Detektors steht dran, mit Umlaut statt ASCII.
assert.ok(html.includes("Schranke von 22 Euro für 8622 km"), html);
assert.ok(!html.includes("fuer"), html);
// Wo man bucht, und der Weg zum Abhaken.
assert.ok(html.includes('href="https://example.invalid/buchen"'), html);
assert.ok(html.includes('rel="noopener noreferrer"'), html);
assert.ok(html.includes('class="findack"'), html);
assert.ok(html.includes(">Abhaken<"), html);
assert.ok(html.includes('data-open="1"'), html);
// Und der Weg zur Kurve derselben Strecke.
assert.ok(html.includes('data-origin="BER"'), html);
assert.ok(html.includes('data-destination="BKK"'), html);

// Ein abgehakter Fund verschwindet nicht, er wird ruhig und laesst sich
// wieder oeffnen.
const done = findRowMarkup(Object.assign({}, RICH,
  {acknowledged_at: "2026-09-09T18:00:00"}));
assert.ok(/class="[^"]*\bdone\b/.test(done), done);
assert.ok(done.includes("Wieder öffnen"), done);
assert.ok(done.includes('data-open="0"'), done);
assert.ok(/class="[^"]*\bopen\b/.test(findRowMarkup(RICH)), findRowMarkup(RICH));

// Ein Fehltarif ist zeitkritisch: buchen steht vor nachsehen und abhaken.
assert.ok(html.indexOf("findbook") < html.indexOf("findcurve"), html);
assert.ok(html.indexOf("findcurve") < html.indexOf("findack"), html);

// Ohne Buchungslink wird keiner erfunden.
const nolink = findRowMarkup(Object.assign({}, RICH, {booking_url: ""}));
assert.ok(!nolink.includes("<a "), nolink);
assert.ok(nolink.includes("kein Link"), nolink);

assert.ok(!findRowMarkup(RICH).includes("→"));
assert.ok(!findRowMarkup(RICH).includes("—"));
""",
        setup=FIXTURES,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_a_find_says_how_old_it_is_and_never_that_it_still_stands():
    """Ein Fehltarif haelt selten lange, und nachgeprueft wird keiner.

    "09.09., 19:02" beantwortet die Frage "ist das noch aktuell" nicht. Das
    Alter beantwortet sie, und mehr als das Alter darf die Zeile nicht
    behaupten.
    """
    result = run_ui_assertion(
        r"""
const now = new Date("2026-09-09T18:00:00");
const at = iso => findAge(iso, now);

assert.strictEqual(at("2026-09-09T18:00:00"), "gerade eben");
assert.strictEqual(at("2026-09-09T17:59:00"), "vor 1 Minute");
assert.strictEqual(at("2026-09-09T17:20:00"), "vor 40 Minuten");
assert.strictEqual(at("2026-09-09T17:00:00"), "vor 1 Stunde");
assert.strictEqual(at("2026-09-09T09:00:00"), "vor 9 Stunden");
assert.strictEqual(at("2026-09-07T18:00:00"), "vor 2 Tagen");
// Ein Zeitstempel, den niemand lesen kann, wird nicht geraten.
assert.strictEqual(at("kein Zeitpunkt"), "");
assert.strictEqual(at(null), "");

// Alt heisst: aelter als die Ruhezeit, nach der derselbe Fund erneut
// gemeldet wuerde. Die Zahl kommt vom Server, nicht aus dem Kopf.
assert.strictEqual(
  findIsStale({created_at:"2026-09-09T13:00:00"}, 6, now), false);
assert.strictEqual(
  findIsStale({created_at:"2026-09-09T11:00:00"}, 6, now), true);
// Ohne Angabe gilt der Vorgabewert des Servers von sechs Stunden.
assert.strictEqual(
  findIsStale({created_at:"2026-09-09T11:00:00"}, null, now), true);
assert.strictEqual(findIsStale({}, 6, now), false);
""",
        setup=FIXTURES,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_an_old_find_steps_back_without_becoming_unreadable():
    result = run_ui_assertion(
        r"""
const old = Object.assign({}, RICH, {created_at: "2020-01-01T00:00:00"});
const fresh = Object.assign({}, RICH, {created_at: new Date().toISOString()});

assert.ok(findRowMarkup(old, 6).includes(" stale"), findRowMarkup(old, 6));
assert.ok(!findRowMarkup(fresh, 6).includes(" stale"));
// Und das Alter steht dran, nicht der Zeitstempel.
assert.ok(findRowMarkup(fresh, 6).includes("gefunden gerade eben"),
  findRowMarkup(fresh, 6));
assert.ok(/gefunden vor \d+ Tagen/.test(findRowMarkup(old, 6)),
  findRowMarkup(old, 6));
""",
        setup=FIXTURES,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    css = APP_CSS.read_text(encoding="utf-8")
    # Zuruecktreten heisst nicht ausgrauen: --ink-subtle liegt bei 5,58:1.
    assert "#hunttable tr.stale{color:var(--ink-subtle)}" in css
    assert "opacity:" not in css.split("#hunttable tr.stale{")[1].split("}")[0]
    page = INDEX.read_text(encoding="utf-8")
    # Die Seite behauptet nirgends, ein Fund sei noch buchbar.
    assert "prüft die Jagd nicht nach" in page


def test_a_dry_run_is_neither_a_failure_nor_a_success():
    """"Erkannt, nicht gesendet, weil kein Kanal eingerichtet ist" ist die
    ehrliche Aussage. Ein Fehlerton waere falsch, ein Erfolgston auch."""
    result = run_ui_assertion(
        r"""
assert.strictEqual(deliveryLabel({delivery:"sent"}), "gemeldet");
assert.strictEqual(deliveryLabel({delivery:"dry_run"}), "erkannt, nicht gesendet");
assert.strictEqual(deliveryLabel({delivery:"suppressed"}), "zurückgehalten");
assert.strictEqual(deliveryLabel({delivery:"failed"}), "Meldung fehlgeschlagen");

// Warum etwas nicht hinausging, steht am Fund und nicht nur im Log.
assert.strictEqual(deliveryNote({delivery:"dry_run"}), "Kein Kanal eingerichtet.");
assert.strictEqual(deliveryNote({delivery:"suppressed", error:"innerhalb der Ruhezeit"}),
  "innerhalb der Ruhezeit");
assert.strictEqual(deliveryNote({delivery:"sent", error:"egal"}), "");

// Der Kanalstand selbst: eine Feststellung, kein Urteil.
assert.strictEqual(channelLine({channel_configured: true}),
  "Discord-Kanal eingerichtet. Ein Fund geht als Meldung hinaus.");
assert.strictEqual(channelLine({channel_configured: false}),
  "Kein Discord-Kanal eingerichtet. Funde werden erkannt und aufgezeichnet,"
  + " aber nicht gesendet.");
assert.strictEqual(channelLine({}), channelLine({channel_configured: false}));

// Und er traegt nie einen Ton, auch nicht nach dem Rendern.
renderHunt({finds: [], summary: {events: 0, channel_configured: false}});
assert.strictEqual($("#huntchannel").dataset.tone, "");
assert.ok($("#huntchannel").textContent.includes("nicht gesendet"),
  $("#huntchannel").textContent);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_hunt_summary_counts_finds_and_names_the_last_one():
    result = run_ui_assertion(
        r"""
assert.ok(huntSummary({summary:{events:0}}).includes("Noch kein Fehltarif"),
  huntSummary({summary:{events:0}}));

const many = huntSummary({summary:{events:12, open:3, sent:2, dry_run:9,
  failed:1, last_find_at:"2026-09-09T12:00:00"}});
assert.ok(many.includes("12 Funde"), many);
assert.ok(many.includes("3 noch offen"), many);
assert.ok(many.includes("Zuletzt"), many);
assert.ok(many.includes("2 gemeldet"), many);
assert.ok(many.includes("9 nur erkannt"), many);
assert.ok(many.includes("1 fehlgeschlagen"), many);

// Deutsch gezaehlt: "1 Funde" liest sich wie ein Zaehlfehler.
const one = huntSummary({summary:{events:1, open:0, dry_run:1}});
assert.ok(one.includes("1 Fund,"), one);
assert.ok(one.includes("alle abgehakt"), one);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_an_empty_find_list_says_which_kind_of_empty_it_is():
    """Null Zeilen bei aktivem Filter heisst etwas anderes als null Zeilen
    ueberhaupt. Ohne den Unterschied sucht jemand einen Fehler, wo keiner ist."""
    result = run_ui_assertion(
        r"""
assert.ok(huntEmptyLine({events: 0}, true).includes("Noch nichts gefunden"));
assert.ok(huntEmptyLine({events: 0}, false).includes("Noch nichts gefunden"));
assert.ok(huntEmptyLine({events: 8}, true).includes("Keine offenen Funde"));
// Ohne Filter waere "keine offenen" die falsche Auskunft.
assert.strictEqual(huntEmptyLine({events: 8}, false), "Keine Funde in dieser Liste.");

$("#huntOpenOnly").checked = false;
renderHunt({finds: [], summary: {events: 0}});
assert.ok($("#huntrows").innerHTML.includes("Noch nichts gefunden"),
  $("#huntrows").innerHTML);
assert.ok($("#huntrows").innerHTML.includes("colspan"), $("#huntrows").innerHTML);

$("#huntOpenOnly").checked = true;
renderHunt({finds: [], summary: {events: 8, open: 0}});
assert.ok($("#huntrows").innerHTML.includes("Keine offenen Funde"),
  $("#huntrows").innerHTML);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_a_thin_day_does_not_get_a_solid_line():
    """Ein Tag mit einer Beobachtung sagt weniger als einer mit dreissig.

    Eine glatte Linie ueber lauter Einzelmessungen erzaehlt eine Sicherheit,
    die es nicht gibt.
    """
    result = run_ui_assertion(
        r"""
const points = [
  {day:"2026-09-01", min:90,  median:105, max:120, n:12},
  {day:"2026-09-02", min:95,  median:110, max:130, n:1},
  {day:"2026-09-03", min:100, median:115, max:140, n:30},
  {day:"2026-09-04", min:40,  median:60,  max:200, n:22},
];

const svg = historyChart(points, [], "observed");
// Genau eine durchgezogene und eine gestrichelte Linie, beide belegt.
assert.ok(svg.includes('class="cmed"'), svg);
assert.ok(svg.includes('class="cmed thin"'), svg);
// Die beiden Strecken, die den duennen Tag beruehren, sind gestrichelt; die
// dritte ist es nicht.
const thin = /class="cmed thin" d="([^"]*)"/.exec(svg)[1];
const solid = /class="cmed" d="([^"]*)"/.exec(svg)[1];
assert.strictEqual((thin.match(/M/g) || []).length, 2, thin);
assert.strictEqual((solid.match(/M/g) || []).length, 1, solid);

// Und die Anzahl steht zusaetzlich als Balken da, nicht nur als Strichart.
assert.ok(svg.includes('data-n="1"'), svg);
assert.ok(svg.includes('data-n="30"'), svg);

// Traegt jeder Tag, gibt es keine gestrichelte Linie.
const solidOnly = historyChart(points.map(p => Object.assign({}, p, {n: 20})),
  [], "observed");
assert.ok(!solidOnly.includes("cmed thin"), solidOnly);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_a_single_recorded_day_gets_the_same_thin_treatment():
    """Die Legende verspricht: gestrichelt heisst weniger als fuenf
    Beobachtungen. Bei genau einem aufgezeichneten Tag stand die Marke
    durchgezogen da, egal ob eine oder dreissig Beobachtungen dahinter lagen.
    Ein Versprechen, das nur meistens gilt, ist keins.
    """
    result = run_ui_assertion(
        r"""
const lone = n => historyChart(
  [{day:"2026-09-01", min:90, median:105, max:120, n}], [], "observed");

// Eine einzige Beobachtung traegt nicht, und das sieht man.
assert.ok(/class="cmed thin"/.test(lone(1)), lone(1));
assert.ok(/class="cmed thin"/.test(lone(4)), lone(4));
// Ab der Schwelle traegt sie.
assert.ok(!/cmed thin/.test(lone(5)), lone(5));
assert.ok(!/cmed thin/.test(lone(30)), lone(30));

// Und die Spanne des Tages faellt nicht weg, nur weil es ein Tag ist:
// `curveBand` braucht zwei Punkte, die Aussage nicht.
assert.ok(lone(30).includes('class="cband"'), lone(30));

// Ein Tag ohne jede Spanne bekommt keinen Strich der Laenge null.
const flat = historyChart([{day:"2026-09-01", min:99, median:99, max:99, n:9}],
  [], "observed");
assert.ok(!flat.includes("cband"), flat);
assert.ok(flat.includes("cmed"), flat);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_curve_is_handwritten_svg_without_text_inside():
    """Der Kasten wird in der Breite gestreckt. Text darin waere mitgestreckt,
    und Striche waeren waagerecht duenner als senkrecht."""
    result = run_ui_assertion(
        r"""
const points = [
  {day:"2026-09-01", min:90, median:105, max:120, n:12},
  {day:"2026-09-04", min:40, median:60,  max:200, n:22},
];
const svg = historyChart(points, [], "observed");

assert.ok(svg.includes('preserveAspectRatio="none"'), svg);
assert.ok(svg.includes('viewBox="0 0 100 100"'), svg);
// Kein <text>, kein <tspan>: beschriftet wird daneben in HTML.
assert.ok(!/<text|<tspan/.test(svg), svg);
// Der Name sagt, was zu sehen ist.
assert.ok(svg.includes('role="img"'), svg);
assert.ok(/aria-label="[^"]*Preisverlauf[^"]*"/.test(svg), svg);
assert.ok(/aria-label="[^"]*40,00 und 200,00 Euro/.test(svg), svg);

// Ohne einen einzigen Punkt wird kein leerer Kasten gezeichnet.
assert.strictEqual(historyChart([], []), "");
assert.strictEqual(historyChart(null, null), "");
// Ein einzelner Tag ist eine Marke, keine Linie.
const one = historyChart([{day:"2026-09-01", min:90, median:105, max:120, n:2}],
  [], "observed");
assert.ok(one.includes("<line"), one);
assert.ok(!one.includes("<path"), one);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout
    css = APP_CSS.read_text(encoding="utf-8")
    # Ohne das waeren waagerechte Striche duenner als senkrechte.
    assert "vector-effect:non-scaling-stroke" in css
    # Keine Bibliothek, kein Aufbauschritt, kein externer Request.
    page = INDEX.read_text(encoding="utf-8")
    assert "<script" not in page.replace('<script src="/static/app.js"></script>', "")


def test_a_gap_in_the_data_never_becomes_a_price_of_zero():
    """`Number(null)` ist 0 und damit endlich.

    Wer nur auf `Number.isFinite` prueft, liest ein fehlendes Minimum als null
    Euro, zieht die Achse auf den Nullpunkt und zeichnet einen Preissturz, den
    es nie gab. Genau die Sorte Fehler, bei der die Oberflaeche sicherer
    aussieht als die Daten sind.
    """
    result = run_ui_assertion(
        r"""
assert.strictEqual(finiteNumber(null), null);
assert.strictEqual(finiteNumber(undefined), null);
assert.strictEqual(finiteNumber(""), null);
assert.strictEqual(finiteNumber("keine Zahl"), null);
assert.strictEqual(finiteNumber(0), 0);
assert.strictEqual(finiteNumber("12.5"), 12.5);

// Ein Punkt ohne Minimum faellt heraus, statt die Achse zu verziehen.
const holes = [
  {day:"2026-09-01", min:null, median:100, max:null, n:5},
  {day:"2026-09-02", min:90, median:100, max:110, n:5},
];
assert.deepStrictEqual(curveDays(holes).map(p => p.day), ["2026-09-02"]);
assert.strictEqual(curveScale(holes).lo, 90);

// Ein unlesbarer Tag genauso.
assert.deepStrictEqual(
  curveDays([{day:"kein-tag", min:90, median:100, max:110, n:5},
             {day:"2026-09-02", min:90, median:100, max:110, n:5}]).map(p => p.day),
  ["2026-09-02"]);

// Und wenn nichts uebrig bleibt, wird kein Kasten gezeichnet.
assert.strictEqual(historyChart([{day:"x", min:null, median:null, max:null}], []), "");
assert.strictEqual(curveScale([]), null);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_curve_sorts_its_own_days_and_survives_a_single_one():
    """Eine verdrehte Reihenfolge spiegelte die Kurve, ohne dass etwas
    auffiele: die Linie saehe genauso glatt aus und meinte das Gegenteil."""
    result = run_ui_assertion(
        r"""
const wrongWay = [
  {day:"2026-09-05", min:90, median:100, max:110, n:5},
  {day:"2026-09-01", min:80, median:90,  max:100, n:5},
];
assert.deepStrictEqual(curveDays(wrongWay).map(p => p.day),
  ["2026-09-01", "2026-09-05"]);
const s = curveScale(wrongWay);
assert.ok(s.span > 0, String(s.span));
assert.strictEqual(curveX("2026-09-01", s), 0);
assert.strictEqual(curveX("2026-09-05", s), 100);

// Alles an einem Tag: keine Strecke, eine Mitte, und keine Division durch null.
const oneDay = curveScale([{day:"2026-09-01", min:90, median:100, max:110, n:5}]);
assert.strictEqual(oneDay.span, 0);
assert.strictEqual(curveX("2026-09-01", oneDay), 50);

// Alle Preise gleich: kein Nenner von null in der Hoehe.
const flat = curveScale([{day:"2026-09-01", min:100, median:100, max:100, n:5},
                         {day:"2026-09-02", min:100, median:100, max:100, n:5}]);
assert.ok(Number.isFinite(curveY(100, flat)), String(curveY(100, flat)));

// Nirgends eine NaN-Koordinate im fertigen Bild.
[wrongWay, [{day:"2026-09-01", min:100, median:100, max:100, n:1}]].forEach(pts => {
  const svg = historyChart(pts, [{created_at:"2026-09-01T08:00:00"}], "observed");
  assert.ok(!/NaN|Infinity|undefined/.test(svg), svg);
});
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_every_part_of_the_curve_carries_enough_contrast():
    """Gemessen, nicht geschaetzt, mit demselben Werkzeug wie die Marken.

    Fuer Grafik, die etwas bedeutet, gilt 3:1. Die erste Fassung nahm
    --rule-2 fuer die Zaehlbalken (2,14:1) und --wash fuer die Tagesspanne
    (1,06:1): beides sagte etwas und war praktisch nicht zu sehen.
    """
    css = APP_CSS.read_text(encoding="utf-8")
    setup = "const T = " + repr({
        name: token_hex(name)
        for name in ("bg", "wash", "ink", "ink-subtle", "rule-1", "rule-3", "signal")
    }).replace("'", '"') + ";"

    result = run_ui_assertion(
        r"""
function ratio(a, b){
  return contrastRatio(relativeLuminance(T[a]), relativeLuminance(T[b]));
}
// Grafik, die eine Aussage traegt: 3:1.
[["rule-3", "bg", "Kante der Tagesspanne"],
 ["ink-subtle", "bg", "Zählbalken"],
 ["signal", "rule-1", "Fundmarke auf der Fläche"],
].forEach(([a, b, what]) => {
  const r = ratio(a, b);
  assert.ok(r >= 3, `${what}: ${r.toFixed(2)}:1`);
});

// Die Linie selbst liegt auf der Flaeche der Spanne und muss dort lesbar
// bleiben, durchgezogen wie gestrichelt.
assert.ok(ratio("ink", "rule-1") >= 4.5, ratio("ink", "rule-1").toFixed(2));
assert.ok(ratio("ink-subtle", "rule-1") >= 3,
  ratio("ink-subtle", "rule-1").toFixed(2));

// Der Befund, der die Farben ueberhaupt geaendert hat: die alten Werte
// waeren durchgefallen.
assert.ok(ratio("wash", "bg") < 3, "--wash gegen --bg war nie zu sehen");
""",
        setup=setup,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert "svg.curve .cband{fill:var(--rule-1);stroke:var(--rule-3)" in css
    assert "svg.curve .cbar{fill:var(--ink-subtle)}" in css


def test_the_finds_sit_on_the_curve_at_the_day_it_is_grouped_by():
    """Ohne die Marken ist die Kurve nur huebsch. Mit der falschen Marke ist
    sie schlimmer als ohne."""
    result = run_ui_assertion(
        r"""
const points = [
  {day:"2026-09-01", min:90, median:105, max:120, n:12},
  {day:"2026-09-05", min:40, median:60,  max:200, n:22},
];
const find = {travel_date:"2026-11-20", created_at:"2026-09-03T08:00:00"};

// Nach Beobachtungstag gruppiert zaehlt der Tag des Fundes.
assert.strictEqual(findDay(find, "observed"), "2026-09-03");
assert.strictEqual(findDay(find, "travel"), "2026-11-20");

const observed = historyChart(points, [find], "observed");
assert.ok(observed.includes('class="cfind"'), observed);
// Der 03.09. liegt genau in der Mitte zwischen dem 01. und dem 05.
assert.ok(observed.includes('x1="50"'), observed);

// Nach Reisetag liegt derselbe Fund ausserhalb des Fensters und wird
// weggelassen, statt an den Rand geschoben zu werden.
const travel = historyChart(points, [find], "travel");
assert.ok(!travel.includes("cfind"), travel);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_curve_headline_names_how_thin_the_days_are():
    result = run_ui_assertion(
        r"""
const points = [
  {day:"2026-09-01", min:90, median:105, max:120, n:12},
  {day:"2026-09-02", min:95, median:110, max:130, n:1},
  {day:"2026-09-03", min:95, median:110, max:130, n:2},
];
const head = historyHeadline({route:"BER-ATH", by:"observed", days:30, points});
assert.ok(head.includes("BER-ATH"), head);
assert.ok(head.includes("nach Beobachtungstag"), head);
assert.ok(head.includes("3 Tage"), head);
assert.ok(head.includes("15 Beobachtungen"), head);
assert.ok(head.includes("2 Tagen stehen weniger als 5"), head);

// Ohne Aufzeichnung wird keine Kurve behauptet.
assert.ok(historyHeadline({route:"BER-ATH", by:"travel", days:90, points:[]})
  .includes("ist nichts aufgezeichnet"));

// Der Takt gehoert an die Kurve: die Dichte der Punkte ist seine Folge.
assert.ok(historyCadenceLine({cadence:"hot", hot:true,
  stats:{days_recorded:9, min_days:5, ready:true}}).includes("Takt heiß"));
assert.ok(historyCadenceLine({cadence:"daily", hot:false,
  stats:{days_recorded:2, min_days:5, ready:false}}).includes("noch 3 von 5"));
// Eine Strecke ausserhalb der Liste hat trotzdem eine Historie, und das
// steht dann auch da.
assert.ok(historyCadenceLine({cadence:null})
  .includes("nicht in der Beobachtungsliste"));
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_cadence_switch_tells_the_truth_about_the_sixth_route():
    """Der Server lehnt eine sechste heisse Strecke nicht ab, sie kommt nur
    seltener dran. Eine erfundene Fehlermeldung waere die eine Auskunft, die
    schlimmer ist als gar keine: sie koennte stimmen und tut es nicht."""
    result = run_ui_assertion(
        r"""
assert.strictEqual(cadenceLabel({cadence:"hot"}), "heiß");
assert.strictEqual(cadenceLabel({cadence:"daily"}), "täglich");
assert.strictEqual(cadenceLabel({}), "täglich");

const room = hotBudgetLine({hot:2, max_hot_routes:5, hot_interval_seconds:1200});
assert.strictEqual(room, "2 von 5 Strecken heiß, alle 20 Minuten abgefragt.");

// Voll: die Grenze steht da, und was jenseits von ihr wirklich passiert.
const full = hotBudgetLine({hot:5, max_hot_routes:5, hot_interval_seconds:1200});
assert.ok(full.includes("5 von 5"), full);
assert.ok(full.includes("nicht abgelehnt"), full);
assert.ok(full.includes("langsamer"), full);

// Darueber: keine Beschoenigung und keine erfundene Sperre.
const over = hotBudgetLine({hot:7, max_hot_routes:5, hot_interval_seconds:1200});
assert.ok(over.includes("7 Strecken heiß"), over);
assert.ok(over.includes("getragen sind 5"), over);
assert.ok(over.includes("seltener dran"), over);

// Der Schalter sagt, was er tut, nicht in welchem Zustand die Zeile ist.
const cold = watchCadenceCell({id:4, cadence:"daily"});
assert.ok(cold.includes("heiß schalten"), cold);
assert.ok(cold.includes('data-cadence="hot"'), cold);
assert.ok(cold.includes('data-route="4"'), cold);
const hot = watchCadenceCell({id:4, cadence:"hot"});
assert.ok(hot.includes("auf täglich"), hot);
assert.ok(hot.includes('data-cadence="daily"'), hot);

// Nach dem Schalten steht die frische Zahl da.
assert.strictEqual(
  cadenceSwitchLabel({route:"BER-ATH", hot:true},
    {hot:3, max_hot_routes:5}), "BER-ATH ist heiß, 3 von 5");
assert.ok(cadenceSwitchLabel({route:"BER-ATH", hot:true},
  {hot:6, max_hot_routes:5}).includes("abgelehnt wird keine"));
assert.strictEqual(
  cadenceSwitchLabel({route:"BER-ATH", hot:false}, {}),
  "BER-ATH läuft wieder im Tagestakt");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_a_watch_row_carries_the_cadence_next_to_the_recording_switch():
    result = run_ui_assertion(
        r"""
const html = watchRowMarkup({id:4, route:"BER-ATH", origin:"BER",
  destination:"ATH", lead_min_days:14, lead_max_days:60, enabled:true,
  cadence:"hot", hot:true, last_run_at:"2026-09-05T08:00:00",
  observations:120, days_recorded:9, min_days:5, ready:true});

assert.ok(html.includes('class="cadenceswitch"'), html);
assert.ok(html.includes('data-cadence="hot"'), html);
assert.ok(html.includes("auf täglich"), html);
// Der Schalter fuer die Aufzeichnung bleibt daneben stehen: Takt und
// Aufzeichnung sind zwei verschiedene Fragen.
assert.ok(html.includes('class="watchtoggle"'), html);
assert.ok(html.includes("Abschalten"), html);
// Und der Weg zur Kurve dieser Strecke.
assert.ok(html.includes('class="watchcurve"'), html);
assert.ok(html.includes('data-origin="BER"'), html);
assert.ok(!html.includes("→") && !html.includes("—"), html);

renderWatchlist({routes: [], summary: {routes:0, active:0, observations:0,
  last_run_at:null, min_days:5, hot:2, max_hot_routes:5,
  hot_interval_seconds:1200}});
assert.ok($("#watchhot").textContent.includes("2 von 5"), $("#watchhot").textContent);
assert.strictEqual($("#watchhot").dataset.tone, "");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_an_open_breaker_is_reported_where_the_cadence_is_set():
    """Wer sich fragt, warum eine heisse Strecke nicht alle zwanzig Minuten
    laeuft, sucht bei dem Schalter, der ihm "heiss" versprochen hat."""
    result = run_ui_assertion(
        r"""
const paused = pauseLine({paused: [
  {source:"aegean", since:"2026-09-09T11:00:00", until:"2026-09-09T11:30:00",
   reason:"HTTP 429"},
]});
assert.ok(paused.includes("Tagestakt"), paused);
assert.ok(paused.includes("aegean"), paused);
assert.ok(paused.includes("Aufgezeichnet"), paused);

// Kein offener Breaker, aber ein Planer, der langsamer laeuft als der Takt:
// dann ist der heisse Takt eine Absichtserklaerung.
const slow = pauseLine({paused: [], interval_seconds: 1200,
  effective_interval_seconds: 3600});
assert.ok(slow.includes("20 Minuten"), slow);
assert.ok(slow.includes("60 Minuten"), slow);

// Alles in Ordnung heisst: kein Satz. Eine Entwarnung, die jeden Tag dasteht,
// liest niemand mehr.
assert.strictEqual(pauseLine({paused: [], interval_seconds: 1200,
  effective_interval_seconds: 1200}), "");
assert.strictEqual(pauseLine({}), "");
assert.strictEqual(pauseLine(null), "");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_run_report_names_what_the_budget_postponed():
    """Ein Durchgang, den das Budget halbiert hat, darf nicht aussehen wie ein
    vollstaendiger."""
    result = run_ui_assertion(
        r"""
assert.strictEqual(huntRunLabel({routes:0}),
  "Gerade ist keine heiße Strecke fällig.");
assert.strictEqual(huntRunLabel({routes:1, finds:0, deferred:[], paused:[]}),
  "1 Strecke abgefragt, nichts gefunden");
const busy = huntRunLabel({routes:5, finds:2, deferred:["BER-FCO", "BER-ATH"],
  paused:["aegean"]});
assert.ok(busy.includes("5 Strecken"), busy);
assert.ok(busy.includes("2 Funde"), busy);
assert.ok(busy.includes("2 aufgeschoben"), busy);
assert.ok(busy.includes("Sicherung offen bei aegean"), busy);
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_server_german_reaches_the_screen_with_its_umlauts():
    """Der Server schreibt ASCII, weil dieselben Saetze in Logs und in der
    Discord-Meldung stehen. Auf dem Schirm ist das schlicht falsch
    geschrieben. Umformuliert wird nichts, nur die Schreibung.

    Beide Saetze hier sind echt: der erste ist die Begruendung des Detektors,
    der zweite die Absage des Servers an ein zu breites Fenster im heissen
    Takt. Beide landen unveraendert in der Oberflaeche.
    """
    result = run_ui_assertion(
        r"""
assert.strictEqual(
  readableGerman("unter der Schranke von 22 Euro fuer 8622 km"),
  "unter der Schranke von 22 Euro für 8622 km");
assert.strictEqual(
  readableGerman("Das Fenster umfasst 86 Tage. Im heissen Takt sind hoechstens"
    + " 62 vorgesehen."),
  "Das Fenster umfasst 86 Tage. Im heißen Takt sind höchstens 62 vorgesehen.");

// Ein Wort, das nicht in der Liste steht, geht unveraendert durch: hier wird
// nichts geraten.
assert.strictEqual(readableGerman("Unbekannte Strecke: 4711"),
  "Unbekannte Strecke: 4711");
// Und nur ganze Woerter, nie ein Stueck aus einem anderen.
assert.strictEqual(readableGerman("Fuerst"), "Fuerst");
assert.strictEqual(readableGerman("gefuerchtet"), "gefuerchtet");
assert.strictEqual(readableGerman(null), "");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_hunt_table_head_and_the_colspan_stay_in_step():
    page = INDEX.read_text(encoding="utf-8")
    script = APP_JS.read_text(encoding="utf-8")

    heads = page.count('<th scope="col"', page.index('id="hunttable"'),
                       page.index('<tbody id="huntrows">'))
    assert f"const HUNT_COLUMNS = {heads};" in script

    watch = page.count('<th scope="col"', page.index('id="watchtable"'),
                       page.index('<tbody id="watchrows">'))
    assert f"const WATCH_COLUMNS = {watch};" in script


def test_the_hunt_view_keeps_its_reason_column_on_a_phone():
    """375 px ist der Arbeitsplatz. Eine Fundzeile ohne Begruendung waere
    genau die Sicherheit, die hier nicht behauptet werden darf."""
    import re

    css = APP_CSS.read_text(encoding="utf-8")

    assert "@media(max-width:560px)" in css
    # Die Spalte wird schmaler, nicht abgeschaltet. Die genaue Zahl darf sich
    # aendern, das Verschwinden nicht.
    assert re.search(r"#hunttable \.c-why\{min-width:\d+px\}", css)
    assert not re.search(r"#hunttable \.c-why\{[^}]*display:none", css)
    # Auf 375 px muessen die Knoepfe in der Tabelle mit dem Daumen zu treffen
    # sein. 32 px reichen fuer eine Maus, nicht fuer drei gestapelte Knoepfe.
    assert re.search(r"#hunttable \.findack[^{]*\{min-height:44px\}", css)
    # Die Meldespalte faellt mit `.c-status` weg, ihre Aussage wandert unter
    # die Strecke: dasselbe Muster wie der Status unter dem Preis.
    assert "#hunttable .findstate{display:block" in css
    assert ".findstate{display:none}" in css


def test_the_new_copy_has_no_arrows_and_no_dashes():
    """Dieselbe Regel wie fuer den Rest der Seite, hier noch einmal fuer die
    Texte, die es vorher nicht gab."""
    page = INDEX.read_text(encoding="utf-8")
    script = APP_JS.read_text(encoding="utf-8")
    style = APP_CSS.read_text(encoding="utf-8")

    for forbidden in ("→", "—", "–"):
        assert forbidden not in page
        assert forbidden not in script
        assert forbidden not in style


def test_the_hunt_section_sits_between_the_finds_and_their_configuration():
    """Was gefunden wurde, steht ueber dem, was beobachtet wird: "was ist da"
    kommt vor "was wird beobachtet"."""
    page = INDEX.read_text(encoding="utf-8")

    order = [
        '<section class="out" id="out">',
        '<section class="deals" id="deals">',
        '<section class="hunt" id="hunt">',
        '<div class="curvewrap" id="huntcurve" hidden>',
        '<section class="watch" id="watch">',
        '<th scope="col" class="c-cadence">Takt</th>',
    ]
    seen = [page.index(mark) for mark in order if mark in page]
    assert len(seen) == len(order), [m for m in order if m not in page]
    assert seen == sorted(seen), order
