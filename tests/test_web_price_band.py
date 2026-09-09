"""Die Preislage in der Oberflaeche: eigene Spalte, eigene Aussage.

Derselbe Node-Harness wie fuer den Rest des Formulars; er wird importiert und
nicht kopiert, sonst laufen zwei Attrappen des DOM auseinander.
"""

from __future__ import annotations

from pathlib import Path

import re

from tests.test_web_ui import run_ui_assertion

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "flightopt" / "web"


def token_hex(name: str) -> str:
    """Der Hex-Rueckfall eines Tokens, so wie er im Stylesheet steht.

    Die Autorenwerte sind OKLCH; der Hex daneben ist die Zahl, gegen die sich
    jede Kontrastangabe in diesem Projekt rechnet. Abgelesen statt abgetippt,
    sonst misst der Test eine Farbe, die so nirgends mehr steht.
    """
    css = (WEB / "app.css").read_text(encoding="utf-8")
    match = re.search(rf"--{name}:[^;]+;\s*/\*\s*(#[0-9a-f]{{6}})", css)
    assert match, f"kein Hex-Rueckfall fuer --{name}"
    return match.group(1)


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
// Und in den Deals genauso. Zwei Woerter fuer denselben Zustand, in zwei
// Tabellen auf derselben Seite, zwingen jeden einmal zu pruefen, ob damit auch
// wirklich dasselbe gemeint ist. Die Hotelseite sagt ohnehin "keine Basis".
assert.strictEqual(signalLabel("unknown"), "keine Basis");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_fourth_tier_has_a_word_of_its_own():
    """"Fehltarif" ist nicht die Steigerung von "guenstig".

    Faellt `error` auf "keine Basis" zurueck, sieht der eine Fund, wegen dem
    das ganze Werkzeug gebaut wurde, aus wie eine fehlende Aussage.
    """
    result = run_ui_assertion(
        r"""
assert.strictEqual(bandLabel("error"), "Fehltarif");
assert.notStrictEqual(bandLabel("error"), bandLabel("cheap"));
// Die anderen drei bleiben, wie sie waren.
assert.strictEqual(bandLabel("cheap"), "günstig");
assert.strictEqual(bandLabel("normal"), "normal");
assert.strictEqual(bandLabel("expensive"), "teuer");
assert.strictEqual(bandLabel("unknown"), "keine Basis");
// Und die Scans sprechen dieselbe Liste, nicht eine zweite daneben.
assert.strictEqual(signalLabel("error"), "Fehltarif");
assert.strictEqual(signalLabel("unknown"), bandLabel(null));
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_the_fourth_tier_is_not_told_apart_by_colour_alone():
    """Gemessen, nicht geschaetzt, und mit dem Werkzeug, das schon da ist.

    --ok und --signal liegen in der Luminanz fast aufeinander. Wer Rot und
    Gruen nicht trennt, sieht zwischen "guenstig" und "Fehltarif" also gar
    nichts, solange nur die Farbe den Unterschied traegt.
    """
    css = (WEB / "app.css").read_text(encoding="utf-8")
    setup = "const T = " + repr(
        {
            name: token_hex(name)
            for name in ("bg", "wash", "paper", "ok", "warn", "signal", "ink-subtle")
        }
    ).replace("'", '"') + ";"

    result = run_ui_assertion(
        r"""
// Jede Stufenfarbe muss auf Papier und auf der offenen Zeile lesbar sein.
[["ok", 4.5], ["warn", 4.5], ["signal", 4.5], ["ink-subtle", 4.5]].forEach(
  ([token, need]) => {
    const l = relativeLuminance(T[token]);
    ["bg", "wash"].forEach(ground => {
      const ratio = contrastRatio(l, relativeLuminance(T[ground]));
      assert.ok(ratio >= need,
        `--${token} auf --${ground}: ${ratio.toFixed(2)}:1`);
    });
  });

// Der Befund, wegen dem die Marke gefuellt ist: guenstig und Fehltarif
// unterscheiden sich farblich um fast nichts.
const between = contrastRatio(relativeLuminance(T["ok"]),
                              relativeLuminance(T["signal"]));
assert.ok(between < 1.2, `--ok gegen --signal: ${between.toFixed(2)}:1`);

// Also traegt die Marke den Unterschied, und ihre Schrift ist darauf lesbar.
const onMark = contrastRatio(relativeLuminance("#ffffff"),
                             relativeLuminance(T["signal"]));
assert.ok(onMark >= 4.5, `Weiss auf --signal: ${onMark.toFixed(2)}:1`);

// Und die Marke steht wirklich in der Zelle, nicht nur eine Farbe.
assert.ok(bandCell("error").includes('class="tiermark"'), bandCell("error"));
assert.ok(bandCell("error").includes("Fehltarif"), bandCell("error"));
// Die drei alten Stufen bleiben schlichter Text.
assert.strictEqual(bandCell("cheap"), "günstig");
assert.strictEqual(bandCell("unknown"), "keine Basis");
""",
        setup=setup,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    # Die Marke ist gefuellt und traegt die Signalschrift, nicht nur eine Farbe.
    assert ".tiermark{" in css
    assert "background:var(--signal);color:var(--signal-fg)" in css
    assert '--signal-fg:#ffffff' in css


def test_an_error_fare_wins_the_row_even_without_a_deviation():
    """Ein Fehltarif ohne Historie hat keine Abweichung, von der er abweicht.

    Nach Abweichung sortiert verdraengte ihn jede gewoehnliche Teilstrecke,
    die zufaellig weiter danebenliegt.
    """
    result = run_ui_assertion(
        r"""
const row = {legs: [
  {origin:"BER", destination:"BKK", date:"2026-11-20", price:39,
   band:{tier:"error", reason:"39,00 Euro fuer 8622 km, also unter der Schranke"
         + " von 45 Euro, die diese Entfernung auch ohne Historie setzt", n:0}},
  {origin:"BKK", destination:"BER", date:"2026-12-04", price:300,
   band:{tier:"expensive", deviation_pct:66.7, median:180, n:14,
         population:"estimate"}},
]};

assert.strictEqual(rowBand(row).tier, "error");
assert.strictEqual(rowBand(row).leg.destination, "BKK");

// Ohne Fehltarif entscheidet weiter die groesste Abweichung.
const plain = {legs: [
  {origin:"BER", destination:"ATH", band:{tier:"cheap", deviation_pct:-12}},
  {origin:"ATH", destination:"BER", band:{tier:"expensive", deviation_pct:66.7}},
]};
assert.strictEqual(rowBand(plain).tier, "expensive");
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_an_error_fare_names_the_threshold_it_was_measured_against():
    """Ohne den Grund ist die Stufe eine Behauptung. Mit ihm steht die Schranke
    da, und der Leser sieht, worauf sie sich stuetzt."""
    result = run_ui_assertion(
        r"""
const leg = {band:{tier:"error", n:0, reason:
  "39,00 Euro fuer 8622 km, also unter der Schranke von 45 Euro, die diese"
  + " Entfernung auch ohne Historie setzt"}};

const note = legBandNote(leg);
assert.ok(note.startsWith("Preislage: Fehltarif"), note);
assert.ok(note.includes("Warum: 39,00 Euro für 8622 km"), note);
// Der Detektor schreibt ASCII, der Schirm nicht.
assert.ok(!note.includes("fuer"), note);
// Ohne Vergleichspreise wird auch keine Zahl behauptet.
assert.ok(!note.includes("aus 0"), note);
assert.ok(legBandMarkup(leg).includes('data-signal="error"'));

// Die drei alten Stufen nennen keinen Grund: er waere die Wiederholung der
// Abweichung, die schon davor steht.
assert.strictEqual(legBandNote({band:{tier:"cheap", deviation_pct:-23.4,
  reason:"unter dem Median"}}), "Preislage: günstig, -23,4 %");

// Mit Historie steht beides da: Abweichung, Basis und Grund.
const rich = {band:{tier:"error", deviation_pct:-80.5, n:12,
  population:"estimate", reason:"unter 25 Prozent des Medians von 200,00 Euro"
  + " (n=12) und unter der Schranke von 12 Euro fuer 1797 km"}};
const full = legBandNote(rich);
assert.ok(full.includes("-80,5 %"), full);
assert.ok(full.includes("aus 12 Schätzpreisen"), full);
assert.ok(full.includes("Schranke von 12 Euro für 1797 km"), full);
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


def test_the_band_names_how_many_prices_and_of_which_kind_it_rests_on():
    """Fuenf Vergleichspreise sind etwas anderes als zwanzig, und geschaetzte
    etwas anderes als geprüfte. Beides steht dran, sonst wirkt jede Stufe
    gleich belastbar."""
    result = run_ui_assertion(
        r"""
assert.strictEqual(
  legBandNote({band:{tier:"cheap", deviation_pct:-23.4, n:22, population:"estimate"}}),
  "Preislage: günstig, -23,4 % (aus 22 Schätzpreisen)"
);
// Unter zehn Punkten sagt die Zeile das dazu.
assert.strictEqual(
  legBandNote({band:{tier:"normal", n:6, population:"verified", thin:true}}),
  "Preislage: normal (aus 6 geprüften Preisen, dünne Basis)"
);
// Ohne Baseline gibt es keine Zahl zu nennen.
assert.strictEqual(bandBasis({tier:"unknown", n:0}), "");
assert.strictEqual(bandBasis({}), "");

const row = {legs: [
  {origin:"BER", destination:"ATH", date:"2026-10-01", price:99,
   band:{tier:"cheap", deviation_pct:-12.0, median:112.5, n:8,
         population:"verified", thin:true}},
]};
assert.ok(bandTitle(row).includes("aus 8 geprüften Preisen, dünne Basis"), bandTitle(row));
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
