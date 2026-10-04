from tests.test_web_ui import run_ui_assertion
from flightopt.domain.airports import search


def test_single_letter_lookup_suggests_berlin_without_changing_metro_search():
    assert search("B", 1)[0].code == "BER"
    assert search("Tok", 1)[0].code == "TYO"


def test_verified_favorite_survives_date_sort_and_cheaper_candidates():
    result = run_ui_assertion(r'''
$("#resultCarrier").value = "";
$("#resultQuality").value = "";
$("#sort").value = "depart";
const row = (total, date, verified) => ({total, verified, dates:[date],
  legs:[{origin:"BER", destination:"ATH", date, price:total, carriers:["FR"]}]});
renderTable([row(80, "2026-11-01", false), row(300, "2026-11-02", true),
  row(250, "2026-11-05", true)]);
const html = $("#rows").innerHTML;
assert.ok(html.indexOf("Geprüfte Routen") < html.indexOf("Kandidaten"));
assert.strictEqual((html.match(/class="opt best/g) || []).length, 1);
assert.ok(html.includes('>#1</button>'));
assert.ok(html.includes('>Favorit</button>'));
const favorite = html.slice(html.indexOf('class="opt best'));
assert.ok(favorite.indexOf("250,00") < favorite.indexOf("80,00"));
assert.ok($("#outsummary").textContent.includes("geprüfte Flüge ab 250,00 €"));
assert.ok($("#outsummary").textContent.includes("Kandidaten ab 80,00 €"));
assert.ok($("#usedby").innerHTML.includes("geprüft ab 250,00 €"));
assert.ok(!$("#usedby").innerHTML.includes("80,00"));
assert.strictEqual(resultQuality({verified:true, legs:[{indicative:true}]}), "indicative");
$("#resultQuality").value = "estimate";
renderTable(lastResults);
assert.ok(!$("#rows").innerHTML.includes("Favorit"));
assert.ok(!$("#rows").innerHTML.includes("Geprüfte Routen"));
assert.ok(itineraryDates(row(80, "2026-11-01", false)).includes('datetime="2026-11-01"'));
assert.ok(itineraryDates(row(80, "2026-11-01", false)).includes("BER - ATH"));
''')
    assert result.returncode == 0, result.stderr or result.stdout


def test_single_letter_tab_completion_and_stale_autocomplete_response():
    result = run_ui_assertion(r'''
const made = [];
const create = document.createElement;
document.createElement = tag => {const node=create(tag); made.push(node); return node;};
trip = "return";
hops = [{code:"", label:""}, {code:"ATH", label:"Athen"}];
drawRoute();
document.createElement = create;
const inputs = made.filter(n => n.tagName === "INPUT" && n.attributes.role === "combobox");
const input = inputs[0];
const menu = made.find(n => n.className === "ac");
const pending = [];
global.fetch = url => url.startsWith("/api/airports")
  ? new Promise(resolve => pending.push({url, resolve}))
  : Promise.resolve({ok:true, json:async()=>({})});
input.value = "B";
input.oninput();
await new Promise(done => setTimeout(done, 170));
assert.ok(pending[0].url.includes("q=B&"));
input.value = "Ath";
input.oninput();
await new Promise(done => setTimeout(done, 170));
const athens = {code:"ATH", city:"Athen", name:"Athen", country:"Griechenland"};
pending[1].resolve({json:async()=>({results:[athens]})});
await new Promise(done => setTimeout(done, 0));
pending[0].resolve({json:async()=>({results:[{code:"BER", city:"Berlin", country:"Deutschland"}]})});
await new Promise(done => setTimeout(done, 0));
assert.ok(menu.innerHTML.includes("ATH"));
assert.ok(!menu.innerHTML.includes("BER"));
document.querySelector = selector => selector === "#route"
  ? {querySelectorAll:()=>inputs} : el(selector);
input.onkeydown({key:"Tab", preventDefault(){}});
assert.strictEqual(input.value, "Athen");
assert.strictEqual(hops[0].code, "ATH");
assert.strictEqual(document.activeElement, inputs[1]);
assert.strictEqual(menu.classList.contains("on"), false);
''')
    assert result.returncode == 0, result.stderr or result.stdout
