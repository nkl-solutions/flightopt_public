"""Budget input and local price-target findings do not invent live booking claims."""

import pytest

from tests.test_web_ui import run_ui_assertion


def assert_ui(code):
    result = run_ui_assertion(code)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("raw,minor", [("", None), (" 250,99 ", 25099), ("180.29", 18029), ("0,01", 1), ("1000000", 100000000)])
def test_target_input_uses_exact_minor_units(raw, minor):
    import json
    assert_ui(f"assert.equal(parsePriceTarget({json.dumps(raw)}), {json.dumps(minor)});")


@pytest.mark.parametrize("raw", ["0", "-1", "1,234", "1e3", "NaN", "1.000,00", "1000000,01"])
def test_invalid_target_input_is_rejected(raw):
    import json
    assert_ui(f"assert.throws(() => parsePriceTarget({json.dumps(raw)}), /Preisziel/);")


def test_saved_profile_payload_includes_optional_target_and_editor_can_remove_it():
    assert_ui(r'''
el('#profileTarget').value='250,29';
assert.equal(profilePayload().price_target_minor,25029);
savedProfiles=[{id:5,name:'Athen',cadence_days:3,currency:'EUR',price_target_minor:25029}];
editProfile(5);
assert.equal(el('#profileEditTarget').value,'250,29');
assert(el('#profileTargetLabel').textContent.includes('EUR'));
el('#profileEditTarget').value='';
let payload;
changeProfile=async(id,changes)=>{payload=changes;return true;};
await saveProfileEdit();
assert.equal(payload.price_target_minor,null);
''')


def test_alert_markup_keeps_links_and_price_check_age_explicit():
    assert_ui(r'''
const row={id:7,profile_name:'<script>bad</script>',route:'BER-ATH-BER',
  price_minor:18029,target_minor:25000,currency:'EUR',checked_at:'2020-01-01T08:00:00',
  dates:['2027-01-05','2027-01-10'],legs:[
    {origin:'BER',destination:'ATH',date:'2027-01-05',verified:true,indicative:false,deep_link:'https://booking.example/out'},
    {origin:'ATH',destination:'BER',date:'2027-01-10',verified:true,indicative:false,deep_link:'javascript:bad'},
  ]};
const html=targetAlertMarkup(row);
assert(html.includes('&lt;script&gt;bad&lt;/script&gt;'));
assert(html.includes('180,29'));
assert(html.includes('250,00'));
assert(html.includes('Geprüft'));
assert(html.includes('Älterer Fund'));
assert(html.includes('Angebot öffnen'));
assert(html.includes('Buchungslink fehlt'));
assert(!html.includes('href="javascript:'));
assert(html.includes('Getrennte Tickets'));
''')


def test_alert_load_discards_old_success_and_old_error():
    assert_ui(r'''
await Promise.resolve();
let resolveOld,rejectOld;
fetch=()=>new Promise(resolve=>{resolveOld=resolve;});
const old=loadTargetAlerts();
fetch=async()=>({ok:true,json:async()=>({alerts:[{id:8,profile_name:'Neu',legs:[]}]})});
await loadTargetAlerts();
resolveOld({ok:true,json:async()=>({alerts:[{id:7,profile_name:'Alt',legs:[]}]})});await old;
assert.equal(targetAlerts[0].id,8);
fetch=()=>new Promise((resolve,reject)=>{rejectOld=reject;});
const oldError=loadTargetAlerts();
fetch=async()=>({ok:true,json:async()=>({alerts:[{id:9,profile_name:'Neuer',legs:[]}]})});
await loadTargetAlerts();
const text=el('#targetAlertsMsg').textContent;
rejectOld(Error('old'));await oldError;
assert.equal(el('#targetAlertsMsg').textContent,text);
assert.equal(targetAlerts[0].id,9);
''')


def test_acknowledgement_keeps_successful_state_when_refresh_fails():
    assert_ui(r'''
targetAlerts=[{id:7,profile_name:'Athen',legs:[],acknowledged_at:null}];
el('#targetAlertsOpenOnly').checked=true;
let requests=0;
fetch=async(url,opts)=>{
  if(opts&&opts.method==='POST'){requests++;return {ok:true,json:async()=>({alert:{
    ...targetAlerts[0],acknowledged_at:'2026-10-05T08:00:00'}})};}
  throw Error('offline');
};
await acknowledgeTargetAlert(7);
assert.equal(targetAlerts[0].acknowledged_at,'2026-10-05T08:00:00');
assert.equal(el('#targetAlertsList').innerHTML,'');
assert.equal(el('#targetAlertsMsg').dataset.tone,'err');
assert.equal(targetAlertsBusy,false);
assert.equal(requests,1);
''')


def test_acknowledgement_guards_double_click():
    assert_ui(r'''
targetAlerts=[{id:7,profile_name:'Athen',legs:[],acknowledged_at:null}];
let resolveRequest,requests=0;
fetch=()=>{requests++;return new Promise(resolve=>{resolveRequest=resolve;});};
loadTargetAlerts=async()=>{};
const first=acknowledgeTargetAlert(7);
await acknowledgeTargetAlert(7);
assert.equal(requests,1);
resolveRequest({ok:true,json:async()=>({alert:{...targetAlerts[0],acknowledged_at:'done'}})});
await first;
assert.equal(targetAlertsBusy,false);
''')


def test_alert_freshness_refresh_is_local_and_stops_outside_saved_view():
    assert_ui(r'''
let callback,delay,cleared=0;
global.window={setTimeout(fn,ms){callback=fn;delay=ms;return 1;},clearTimeout(){cleared++;}};
const day=new Date(Date.now()+86400000).toISOString().slice(0,10),now=Date.now();
targetAlerts=[{id:7,checked_at:new Date(now-86400000+2000).toISOString(),dates:[day],legs:[]}];
workspaceView='saved';
let renders=0;renderTargetAlerts=()=>{renders++;};
scheduleTargetAlertRefresh();
assert(delay>0 && delay<=2001);
callback();assert.equal(renders,1);
workspaceView='search';callback=null;
scheduleTargetAlertRefresh();
assert.equal(callback,null);assert(cleared>=2);
''')


def test_alerts_request_open_filter_before_applying_limit():
    assert_ui(r'''
let url;
fetch=async path=>{url=String(path);return {ok:true,json:async()=>({alerts:[]})};};
el('#targetAlertsOpenOnly').checked=true;
await loadTargetAlerts();
assert(url.includes('only_open=true'));
el('#targetAlertsOpenOnly').checked=false;
await el('#targetAlertsOpenOnly').onchange();
assert(url.includes('only_open=false'));
''')


def test_ambiguous_timestamps_never_make_a_fresh_booking_claim():
    assert_ui(r'''
const row={checked_at:'2027-01-01T12:00:00',dates:['2027-01-10']};
assert.equal(targetAlertFresh(row,Date.parse('2027-01-01T13:00:00Z')),false);
row.checked_at='2027-01-01T12:00:00Z';
assert.equal(targetAlertFresh(row,Date.parse('2027-01-01T13:00:00Z')),true);
assert.equal(targetAlertFresh(row,Date.parse('2027-01-02T13:00:00Z')),false);
''')


def test_booking_link_focus_survives_alert_refresh():
    assert_ui(r'''
targetAlerts=[{id:7,profile_name:'Athen',legs:[]}];
const original={dataset:{focusKey:'7-link-0'}};
const replacement={dataset:{focusKey:'7-link-0'},focus(opts){
  assert.deepEqual(opts,{preventScroll:true});document.activeElement=this;
}};
document.body={};document.activeElement=original;
el('#targetAlertsList').querySelector=selector=>selector.includes('7-link-0')?replacement:null;
el('#targetAlertsList')._inputs=[original];
renderTargetAlerts();
assert.equal(document.activeElement,replacement);
''')


def test_failed_acknowledgement_does_not_steal_focus_from_booking_link():
    assert_ui(r'''
targetAlerts=[{id:7,profile_name:'Athen',legs:[]}];
document.body={};
const original={dataset:{targetAlertId:'7',focusKey:'7-ack'}};
const link={dataset:{focusKey:'7-link-0'}};
const replacement={dataset:{focusKey:'7-link-0'},focus(){document.activeElement=this;}};
document.activeElement=original;
el('#targetAlertsList')._inputs=[original];
el('#targetAlertsList').querySelector=selector=>selector.includes('7-link-0')?replacement:null;
let rejectRequest;
fetch=()=>new Promise((resolve,reject)=>{rejectRequest=reject;});
const pending=acknowledgeTargetAlert(7);
document.activeElement=link;el('#targetAlertsList')._inputs=[link];
rejectRequest(Error('offline'));await pending;
assert.equal(document.activeElement,replacement);
assert.equal(targetAlertsBusy,false);
''')
