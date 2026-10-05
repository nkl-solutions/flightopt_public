"""The search remains usable independently of background monitoring."""

import re

from tests.test_web_ui import INDEX, run_ui_assertion
from tests.test_hotels_web import HOTELS_HTML, PageStructure


def assert_ui(assertion, setup=""):
    result = run_ui_assertion(assertion, setup)
    assert result.returncode == 0, result.stderr


def test_live_updates_do_not_move_the_workspace_or_auto_scroll_results():
    css = (INDEX.parent / "app.css").read_text(encoding="utf-8")
    assert "scrollbar-gutter:stable" in css
    assert ".log,.out{overflow-anchor:none}" in css
    assert_ui(r'''
document.activeElement = document.body;
let options;
$("#outtitle").focus = value => {options = value;};
assert.equal(focusResults(), true);
assert.deepEqual(options, {preventScroll:true});
''')


def test_workspace_views_are_separate_before_javascript_runs():
    html = INDEX.read_text(encoding="utf-8")
    assert 'aria-label="Hauptnavigation"' in html
    assert re.search(r'id="view-search"[^>]*>', html)
    for name in ("saved", "radar"):
        assert re.search(rf'id="view-{name}"[^>]*\bhidden\b', html)
        assert f'href="#{name}"' in html
    structure = PageStructure(html)
    ancestors = {attrs["id"]: parents for _, attrs, parents in structure.elements if "id" in attrs}
    for control in ("f", "log", "out"):
        assert ("div", "view-search") in ancestors[control]
    assert ("div", "view-saved") in ancestors["deals"]
    for control in ("hunt", "watch"):
        assert ("div", "view-radar") in ancestors[control]
    assert ("form", "f") not in ancestors["profileName"]


def test_flight_and_hotel_pages_share_navigation_and_asset_version():
    nav_labels = []
    css_versions = []
    for path in (INDEX, HOTELS_HTML):
        html = path.read_text(encoding="utf-8")
        nav = re.search(r'<nav class="worknav"[^>]*>(.*?)</nav>', html, re.S).group(1)
        nav_labels.append(re.findall(r'<a[^>]*>([^<]+)</a>', nav))
        css_versions.append(re.search(r'app\.css\?v=(\d+)', html).group(1))
    assert nav_labels[0] == nav_labels[1] == ["Flugsuche", "Hotels", "Gespeicherte Suchen", "Preisradar"]
    assert css_versions[0] == css_versions[1]


def test_view_switch_preserves_form_results_and_running_search():
    assert_ui(r'''
const stream = {close() { throw Error("navigation must not cancel searches"); }};
es = stream;
lastResults = [{total: 145}];
const originalHops = JSON.stringify(hops);
assert.equal(setWorkspaceView("saved"), "saved");
assert.equal(el("#view-search").hidden, true);
assert.equal(el("#view-saved").hidden, false);
assert.equal(el("#view-radar").hidden, true);
assert.equal(es, stream);
assert.equal(lastResults[0].total, 145);
assert.equal(JSON.stringify(hops), originalHops);
assert.equal(el("#nav-saved").getAttribute("aria-current"), "page");
assert.equal(el("#nav-search").getAttribute("aria-current"), "false");
assert.equal(setWorkspaceView("invalid"), "search");
assert.equal(el("#view-search").hidden, false);
assert.equal(workspaceViewFromHash("#radar"), "radar");
assert.equal(workspaceViewFromHash("#unknown"), "search");
''')


def test_hash_navigation_handles_history_and_restores_initial_view():
    assert_ui(r'''
assert.equal(el("#view-radar").hidden, false);
window.location.hash = "#saved";
listeners.hashchange();
assert.equal(el("#view-saved").hidden, false);
assert.equal(el("#view-radar").hidden, true);
setWorkspaceView("search", {updateHash:true});
assert.equal(window.location.hash, "#search");
''', setup=r'''
const listeners = {};
global.window = {
  location:{hash:"#radar"},
  addEventListener(name, callback) { listeners[name] = callback; }
};
''')


def test_saved_scan_opens_in_search_view():
    assert_ui(r'''
setWorkspaceView("saved");
global.fetch = async () => ({ok:true, json:async () => ({results:[]})});
await openDeal(7);
assert.equal(el("#view-search").hidden, false);
assert.equal(el("#view-saved").hidden, true);
assert.match(el("#outsummary").textContent, /keine Ergebnisse/);
''')


def test_route_fields_have_visible_origin_and_destination_labels():
    assert_ui(r'''
trip = "multi";
hops = [{code:"BER",label:"Berlin"}, {code:"ATH",label:"Athen"}, {code:"BER",label:"Berlin"}];
el("#route").children = [];
drawRoute();
const labels = el("#route").children.filter(node => node.classList.contains("hop"))
  .flatMap(node => node.children.filter(child => child.tagName === "LABEL"));
assert.deepEqual(labels.map(label => label.textContent), ["Start", "Stopp 1", "Rückkehr"]);
labels.forEach(label => assert.ok(label.getAttribute("for")));
const inputs = el("#route").children.filter(node => node.classList.contains("hop"))
  .flatMap(node => node.children.filter(child => child.tagName === "INPUT"));
labels.forEach((label, i) => assert.ok(inputs[i].getAttribute("aria-label").includes(label.textContent)));
''')


def test_background_results_do_not_move_focus_out_of_other_views():
    assert_ui(r'''
setWorkspaceView("radar");
document.activeElement = el("#watchFrom");
assert.equal(focusResults(), false);
assert.equal(document.activeElement, el("#watchFrom"));
''')


def test_saved_results_cannot_replace_a_running_search():
    assert_ui(r'''
es = {close() { throw Error("must not close a running stream"); }};
global.fetch = async () => { throw Error("must not fetch a stored job while running"); };
setWorkspaceView("saved");
await openDeal(7);
assert.equal(el("#savedmsg").dataset.tone, "warn");
assert.match(el("#savedmsg").textContent, /läuft/);
assert.equal(el("#view-saved").hidden, false);
''')


def test_empty_monitoring_views_do_not_show_empty_table_headers():
    assert_ui(r'''
renderDeals([]);
renderWatchlist({routes:[], summary:{}});
renderHunt({finds:[], summary:{}});
assert.equal(el("#dealstable").hidden, true);
assert.equal(el("#watchtable").hidden, true);
assert.equal(el("#hunttable").hidden, true);
renderDeals([{job_id:7, price:100, profile:"Athen"}]);
renderWatchlist({routes:[{id:1,route:"BER-ATH"}], summary:{}});
renderHunt({finds:[{id:2}], summary:{events:1}});
assert.equal(el("#dealstable").hidden, false);
assert.equal(el("#watchtable").hidden, false);
assert.equal(el("#hunttable").hidden, false);
''')
