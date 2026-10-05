"""Hotel monitoring uses existing APIs without inventing price confirmations."""

from tests.test_hotels_web import HOTELS_HTML, PageStructure, run_hotel_assertion


def check(script):
    result = run_hotel_assertion(script)
    assert result.returncode == 0, result.stderr or result.stdout


SETUP = r'''
$("#destination").value = "Athen";
$("#nights").value = "3";
$("#adults").value = "2";
$("#rooms").value = "1";
$("#currency").value = "EUR";
$("#hleadmin").value = "14";
$("#hleadmax").value = "20";
'''


def test_monitoring_form_is_not_nested_in_search_and_has_labels():
    structure = PageStructure(HOTELS_HTML.read_text(encoding="utf-8"))
    controls = {attrs["id"]: (tag, attrs, parents)
                for tag, attrs, parents in structure.elements if "id" in attrs}
    for name in ("hleadmin", "hleadmax", "hwatchsave"):
        assert ("form", "hwatchform") in controls[name][2]
        assert ("form", "hf") not in controls[name][2]
    labels = {attrs.get("for") for tag, attrs, _ in structure.elements if tag == "label"}
    assert {"hleadmin", "hleadmax"} <= labels


def test_watch_payload_uses_rolling_days_and_preserves_hotel_preferences():
    check(SETUP + r'''
stars = new Set([4, 5]);
$("#minReview").value = "8";
const p = hotelWatchPayload();
assert.equal(p.destination, "Athen");
assert.equal(p.nights, 3);
assert.equal(p.lead_min_days, 14);
assert.equal(p.lead_max_days, 20);
assert.equal(p.min_review_score, 8);
assert.deepEqual(p.stars, [4, 5]);
assert.ok(!("arrival" in p));
assert.ok(!("scan_id" in p));
assert.ok(!("window_end" in p));
for (const [low, high] of [[21,14], [0,31], [-1,3], [350,366], [14.5,20]]) {
  $("#hleadmin").value = String(low);
  $("#hleadmax").value = String(high);
  assert.throws(hotelWatchPayload);
}
''')


def test_watch_rows_show_due_and_paused_without_claiming_destination_baseline():
    check(r'''
drawHotelWatches({watches:[
  {id:1, destination:'Athen <Hotel>', nights:3, adults:2, rooms:1,
   enabled:true, due:true, lead_min_days:14, lead_max_days:20,
   days_recorded:5, ready:true},
  {id:2, destination:'Berlin', nights:1, adults:1, rooms:1,
   enabled:false, due:false, lead_min_days:7, lead_max_days:10},
], summary:{due:1,max_per_run:3}});
const html = $("#hwatchlist").innerHTML;
assert.ok(html.includes("Athen &lt;Hotel&gt;"));
assert.ok(html.includes("Heute fällig"));
assert.ok(html.includes("Pausiert"));
assert.ok(html.includes('data-hwatch="1"'));
assert.ok(html.includes('aria-pressed="true"'));
assert.ok(!html.includes("Baseline bereit"));
assert.ok(!html.includes("Löschen"));
assert.ok($("#hwatchsummary").textContent.includes("1 fällig"));
''')


def test_save_uses_watch_endpoint_not_manual_search():
    check(SETUP + r'''
const requests = [];
global.fetch = async (url, options) => {
  requests.push({url, options});
  return {ok:true, json:async()=> url.includes("health")
    ? {scheduler:{running:true}, sources:{hotel:[]}}
    : {watches:[], summary:{due:0}}};
};
await saveHotelWatch();
const request = requests.find(r => r.options && r.options.method === "POST");
assert.equal(request.url, "/api/hotels/watchlist");
assert.equal(JSON.parse(request.options.body).lead_max_days, 20);
assert.ok(!requests.some(r => r.url === "/api/hotels/search"));
assert.equal($("#hwatchsave").disabled, false);
assert.ok($("#hwatchstatus").textContent.includes("gespeichert"));
''')


def test_run_once_blocks_double_clicks_and_reports_errors():
    check(r'''
let release;
let calls = 0;
global.fetch = async url => {
  if (url.endsWith("run-once")) {
    calls++;
    return new Promise(resolve => {release = () => resolve({ok:true,
      json:async()=>({watches:1,days:7,observations:0,errors:["Quelle nicht erreichbar"],due_left:2})});});
  }
  return {ok:true, json:async()=> url.includes("health")
    ? {scheduler:{running:false}, sources:{hotel:[]}}
    : {watches:[],summary:{due:2}}};
};
const first = runHotelWatches();
await runHotelWatches();
assert.equal(calls, 1);
assert.equal($("#hwatchrun").disabled, true);
release();
await first;
assert.ok($("#hwatchstatus").textContent.includes("Quelle nicht erreichbar"));
assert.equal($("#hwatchstatus").dataset.tone, "warn");
assert.ok($("#hwatchnote").textContent.includes("nicht aktiv"));
''')


def test_safe_booking_actions_are_explicit_and_do_not_confirm_indicative_prices():
    check(r'''
const row = {name:"Hotel",date:"2026-11-10",source:"trivago",price_per_night:80,
  currency:"EUR",indicative:true,url:"https://example.invalid/hotel"};
let html = rowHtml(row);
assert.ok(html.includes('class="hotelbook"'));
assert.ok(html.includes("Angebot öffnen"));
assert.ok(html.includes("Richtwert"));
row.indicative = false;
html = rowHtml(row);
assert.ok(html.includes(">Buchen</a>"));
for (const url of ["javascript:alert(1)", "https://", "https://user:secret@example.invalid/",
                   "https://example.invalid/\nbook", "//example.invalid/"]) {
  row.url = url;
  html = rowHtml(row);
  assert.ok(!html.includes("<a "), html);
  assert.ok(html.includes("Buchungslink fehlt"));
}
''')


def test_failed_load_does_not_show_empty_watchlist_as_success():
    check(r'''
global.fetch = async () => ({ok:false,status:503,json:async()=>({detail:"Datenbank nicht erreichbar"})});
await loadHotelWatches();
assert.equal($("#hwatchstatus").dataset.tone, "err");
assert.ok($("#hwatchstatus").textContent.includes("Datenbank nicht erreichbar"));
assert.equal($("#hwatchlist").getAttribute("aria-busy"), "false");
''')


def test_pause_and_resume_send_explicit_enabled_values():
    check(r'''
hotelWatches = [{id:7,destination:"Athen",enabled:true,due:true}];
const requests = [];
global.fetch = async (url, options) => {
  if (options && options.method === "PATCH") {
    requests.push({url, enabled:JSON.parse(options.body).enabled});
    return {ok:true,json:async()=>({id:7,enabled:requests.at(-1).enabled,due:true})};
  }
  return {ok:true,json:async()=> url.includes("health") ? {sources:{hotel:[]}}
    : {watches:[{id:7,destination:"Athen",enabled:requests.at(-1).enabled,due:true}],summary:{due:1}}};
};
await toggleHotelWatch(7);
await toggleHotelWatch(7);
assert.deepEqual(requests, [
  {url:"/api/hotels/watchlist/7",enabled:false},
  {url:"/api/hotels/watchlist/7",enabled:true},
]);
assert.equal(hotelWatchBusy, false);
''')


def test_loading_saved_scan_cannot_replace_an_active_search():
    check(r'''
let calls = 0;
global.fetch = async () => {calls++;throw Error("must not fetch during search");};
setRunning(true);
rows = [{name:"Current"}];
await openScan(12);
assert.equal(calls, 0);
assert.equal(rows[0].name, "Current");
setRunning(false);
document.activeElement = document.body;
let options;
$("#outtitle").focus = o => {options = o;};
assert.equal(focusResults(), true);
assert.deepEqual(options, {preventScroll:true});
''')


def test_confirmed_switch_survives_a_failed_reload_and_restores_focus():
    check(r'''
hotelWatches = [{id:7,destination:"Athen",enabled:true,due:true}];
const oldButton = el("old-watch");
oldButton.dataset.hwatch = "7";
document.activeElement = oldButton;
Object.defineProperty($("#hwatchlist"), "innerHTML", {set() {document.activeElement = document.body;}});
let requested;
global.fetch = async (url, options) => {
  if (options && options.method === "PATCH") {
    requested = JSON.parse(options.body).enabled;
    return {ok:true,json:async()=>({id:7,enabled:requested,due:false})};
  }
  return {ok:false,status:503,json:async()=>({detail:"Nachladen gescheitert"})};
};
await toggleHotelWatch(7);
assert.equal(requested, false);
assert.equal(hotelWatches[0].enabled, false);
assert.equal(document.activeElement, $('[data-hwatch="7"]'));
await toggleHotelWatch(7);
assert.equal(requested, true);
''')


def test_saved_scan_response_and_errors_are_ignored_after_new_search():
    check(SETUP + r'''
$("#from").value = "2026-11-10";
for (const ok of [true, false]) {
  let finish;
  global.fetch = async url => {
    if (url.includes("/rows")) return new Promise(resolve => {finish = () => resolve({ok,
      json:async()=>({scan_id:1,status:"done",rows:[{name:"Old"}],detail:"Old error"})});});
    return {ok:true,json:async()=>({scan_id:42,sources:[]})};
  };
  const old = openScan(1);
  await search(null);
  setRunning(false);
  rows = [{name:"New"}];
  $("#note").textContent = "New complete";
  finish();
  await old;
  assert.equal(lastScan, 42);
  assert.equal(rows[0].name, "New");
  assert.equal($("#note").textContent, "New complete");
}
''')


def test_old_health_error_cannot_replace_newer_status():
    check(r'''
let rejectOld;
let healthCalls = 0;
global.fetch = async url => {
  if (url.includes("health")) {
    if (++healthCalls === 1) return new Promise((resolve,reject)=>{rejectOld=reject;});
    return {ok:true,json:async()=>({scheduler:{running:true},sources:{hotel:[{name:"booking",active:true}]}})};
  }
  return {ok:true,json:async()=>({watches:[],summary:{due:0}})};
};
const old = loadHotelWatches();
while (!rejectOld) await new Promise(resolve => setImmediate(resolve));
await loadHotelWatches();
assert.equal($("#hwatchnote").textContent, "");
rejectOld(Error("old request"));
await old;
assert.equal($("#hwatchnote").textContent, "");
''')
