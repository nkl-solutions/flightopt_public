"""Booking actions stay visible, scoped to a leg and safe to navigate."""

import json

import pytest

from tests.test_web_ui import run_ui_assertion


SETUP = r"""
global.window = {
  location: {hash: "#search"},
  addEventListener() {},
};
"""

RESULT = r"""
const row = {
  route: "BER-ATH-FCO", dates: ["2026-10-01", "2026-10-06"],
  total: 160, currency: "EUR", verified: true, estimate: 160, drift: 0,
  legs: [
    {origin:"BER", destination:"ATH", date:"2026-10-01", price:79,
     verified:true, source:"ryanair", deep_link:"https://example.invalid/out?date=2026-10-01&adults=1"},
    {origin:"ATH", destination:"FCO", date:"2026-10-06", price:81,
     verified:true, source:"wizzair", deep_link:"http://example.invalid/onward"},
  ],
};
axis = {start:new Date("2026-10-01"), end:new Date("2026-10-06")};
const visible = html => html.split('<tr class="detrow')[0];
"""


def check(assertion: str) -> None:
    result = run_ui_assertion(RESULT + assertion, setup=SETUP)
    assert result.returncode == 0, result.stderr or result.stdout


def test_collapsed_results_offer_each_leg_without_a_fake_total_booking():
    check(r"""
const html = visible(rowMarkup(row, 0, true));
assert.ok(html.includes('class="bookinglinks"'), html);
assert.ok(html.includes("Getrennte Tickets"), html);
assert.ok(html.includes("BER - ATH buchen"), html);
assert.ok(html.includes("ATH - FCO buchen"), html);
assert.ok(html.includes('href="https://example.invalid/out?date=2026-10-01&amp;adults=1"'), html);
assert.ok(html.includes('href="http://example.invalid/onward"'), html);
assert.strictEqual((html.match(/class="bookinglink"/g) || []).length, 2);
assert.strictEqual((html.match(/target="_blank" rel="noopener noreferrer"/g) || []).length, 2);
assert.ok(!html.includes("Gesamtreise buchen"), html);
""")


def test_one_way_result_has_one_direct_action():
    check(r"""
row.legs = row.legs.slice(0, 1);
row.dates = row.dates.slice(0, 1);
const html = visible(rowMarkup(row, 0));
assert.ok(html.includes("BER - ATH buchen"), html);
assert.ok(!html.includes("Getrennte Tickets"), html);
assert.strictEqual((html.match(/class="bookinglink"/g) || []).length, 1);
""")


@pytest.mark.parametrize("url", [
    None, "", "   ", 123, {}, "javascript:alert(1)",
    "JaVaScRiPt:alert(1)", "data:text/html,<script>alert(1)</script>",
    "vbscript:alert(1)", "file:///C:/booking.html", "ftp://example.invalid/",
    "//example.invalid/book", "/book", "https://", "http:example.invalid",
    "https://[invalid", "https://user:password@example.invalid/book",
    "https://example.invalid/\nbook", "https://example.invalid/\tbook",
    "https:\\example.invalid\\book",
])
def test_unsafe_or_missing_links_are_unavailable_in_rows_and_details(url):
    check("row.legs[0].deep_link = " + json.dumps(url) + r""";
row.legs = row.legs.slice(0, 1);
row.dates = row.dates.slice(0, 1);
for (const html of [visible(rowMarkup(row, 0)), detailRows(row)]) {
  assert.ok(!html.includes("<a "), html);
  assert.ok(html.includes("Buchungslink fehlt"), html);
}
assert.ok(!unverifiedNote(row.legs[0]).includes("mit Buchungslink"));
""")


def test_missing_leg_link_keeps_other_leg_action_and_does_not_use_row_url():
    check(r"""
delete row.legs[1].deep_link;
row.deep_link = "https://example.invalid/fake-total";
const html = visible(rowMarkup(row, 0));
assert.ok(html.includes("BER - ATH buchen"), html);
assert.ok(html.includes("ATH - FCO: Buchungslink fehlt"), html);
assert.ok(!html.includes("fake-total"), html);
assert.strictEqual((html.match(/class="bookinglink"/g) || []).length, 1);
assert.ok(detailRows(row).includes("Buchungslink fehlt"));
""")


def test_candidates_with_links_keep_their_quality_and_show_honest_actions():
    check(r"""
row.verified = false;
row.legs[0].verified = false;
const html = visible(rowMarkup(row, 0));
assert.ok(html.includes("BER - ATH Angebot öffnen"), html);
assert.ok(html.includes('class="rowstatus">Schätzung'), html);
assert.strictEqual(resultQuality(row), "estimate");
assert.ok(unverifiedNote(row.legs[0]).includes("mit Buchungslink"));
row.verified = true;
row.legs[0].verified = true;
row.legs[0].indicative = true;
const indicative = visible(rowMarkup(row, 0));
assert.ok(indicative.includes("BER - ATH Richtwert öffnen"), indicative);
assert.ok(indicative.includes('class="rowstatus">Richtwert'), indicative);
assert.strictEqual(resultQuality(row), "indicative");
assert.ok(!detailRows(row).includes("Live geprüft"));
""")


def test_link_attributes_escape_provider_values_and_include_leg_context():
    check(r"""
row.legs[0].deep_link = 'https://example.invalid/book?q=" onclick="alert(1)&token=<x>';
row.legs[0].origin = 'B"<ER';
const html = visible(rowMarkup(row, 0));
assert.ok(html.includes('q=&quot; onclick=&quot;alert(1)&amp;token=&lt;x&gt;'), html);
assert.ok(!html.includes(' onclick="'), html);
assert.ok(html.includes('B&quot;&lt;ER - ATH buchen'), html);
assert.ok(html.includes('aria-label="Teilstrecke 1:'), html);
assert.ok(html.includes("2026-10-01"), html);
""")


def test_live_update_adds_links_without_changing_favorite_or_open_details():
    check(r"""
const candidate = {...row, verified:false,
  legs:row.legs.map(l => ({...l, verified:false, deep_link:null}))};
applyPartial([candidate]);
assert.ok(visible($("#rows").innerHTML).includes("Buchungslink fehlt"));
assert.ok(!$("#rows").innerHTML.includes("Favorit"));
openRows.add(resultKey(candidate));
applyVerified(row);
const html = visible($("#rows").innerHTML);
assert.ok(html.includes("BER - ATH buchen"), html);
assert.ok(html.includes("Favorit"), html);
assert.ok(html.includes('aria-expanded="true"'), html);
assert.ok($("#rows").innerHTML.includes("Geprüfte Routen"));
""")


def test_clicks_on_booking_link_children_do_not_toggle_result_details():
    check(r"""
let toggles = 0;
const tr = {
  dataset: {n:"0", key:"booking-row"},
  classList: {toggle() { toggles++; return true; }},
  querySelector() { return null; },
};
wireRows({querySelectorAll() { return [tr]; }, querySelector() { return null; }});
tr.onclick({target:{tagName:"SPAN", closest(selector) {
  return selector.includes("a") ? {} : null;
}}});
assert.strictEqual(toggles, 0);
tr.onclick({target:{tagName:"TD", closest() { return null; }}});
assert.strictEqual(toggles, 1);
""")
