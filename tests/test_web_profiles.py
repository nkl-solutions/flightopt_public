"""Saved searches stay manageable without altering the current flight search."""

from tests.test_web_ui import INDEX, run_ui_assertion


def assert_ui(code):
    result = run_ui_assertion(code)
    assert result.returncode == 0, result.stderr


def test_profile_rows_show_fixed_window_status_and_safe_names():
    assert_ui(r'''
const base = {id:5, name:'<script>bad</script>', routes:['BER-ATH-BER'],
  variants:1, window_start:'2026-10-01', window_end:'2026-10-10',
  enabled:true, cadence_days:1, next_run_at:'2026-10-06T08:00:00'};
const expired = profileRowMarkup({...base, status:'expired', expired:true});
assert(expired.includes('Abgelaufen'));
assert(expired.includes('&lt;script&gt;bad&lt;/script&gt;'));
assert(!expired.includes('<script>'));
assert(expired.includes('01.10.'));
assert(/data-profile-action="run"[^>]*disabled/.test(expired));
const paused = profileRowMarkup({...base, enabled:false, status:'paused'});
assert(paused.includes('Pausiert'));
assert(/data-profile-action="run"[^>]*disabled/.test(paused));
assert(profileRowMarkup({...base, status:'running'}).includes('Suche läuft'));
assert(profileRowMarkup({...base, status:'due'}).includes('Fällig'));
''')


def test_profile_pause_applies_response_even_when_reload_fails():
    assert_ui(r'''
savedProfiles = [{id:5, name:'Athen', status:'due', enabled:true, routes:[], cadence_days:1}];
const calls = [];
fetch = async (url, opts) => {
  calls.push([url, opts]);
  if (opts && opts.method === 'PATCH') return {ok:true,json:async()=>({profile:{
    ...savedProfiles[0], enabled:false, status:'paused'}})};
  throw Error('offline');
};
await changeProfile(5, {enabled:false});
assert.equal(calls[0][0], '/api/profiles/5');
assert.deepEqual(JSON.parse(calls[0][1].body), {enabled:false});
assert.equal(savedProfiles[0].enabled, false);
assert(el('#profileList').innerHTML.includes('Pausiert'));
assert.equal(el('#profilesmsg').dataset.tone, 'err');
assert.equal(profileMutationBusy, false);
''')


def test_profile_request_generation_discards_old_success_and_error():
    assert_ui(r'''
await Promise.resolve();
let rejectOld, resolveOld;
fetch = () => new Promise(resolve => {resolveOld=resolve;});
const old = loadProfiles();
fetch = async () => ({ok:true,json:async()=>({profiles:[{id:2,name:'Neu',status:'paused',routes:[]}]})});
await loadProfiles();
resolveOld({ok:true,json:async()=>({profiles:[{id:1,name:'Alt',routes:[]}]})});
await old;
assert.equal(savedProfiles[0].id, 2);
fetch = () => new Promise((resolve,reject) => {rejectOld=reject;});
const staleError = loadProfiles();
fetch = async () => ({ok:true,json:async()=>({profiles:[{id:3,name:'Noch neuer',routes:[]}]})});
await loadProfiles();
const currentMessage = el('#profilesmsg').textContent;
rejectOld(Error('old failure')); await staleError;
assert.equal(el('#profilesmsg').textContent, currentMessage);
assert.equal(savedProfiles[0].id, 3);
''')


def test_profile_mutation_guards_double_click_and_retains_search():
    assert_ui(r'''
savedProfiles = [{id:5,name:'Athen',enabled:true,status:'due',routes:[]}];
let resolveRequest, count=0;
const currentRoute = JSON.stringify(hops);
fetch = () => { count++; return new Promise(resolve => {resolveRequest=resolve;}); };
loadProfiles = async () => {};
loadScanner = async () => {};
loadDeals = async () => {};
const first = changeProfile(5, {}, {run:true});
await changeProfile(5, {}, {run:true});
assert.equal(count, 1);
resolveRequest({ok:true,json:async()=>({job:{job_id:10},profile:{...savedProfiles[0],status:'running'}})});
await first;
assert.equal(JSON.stringify(hops), currentRoute);
assert.equal(profileMutationBusy, false);
assert.equal(savedProfiles[0].status, 'running');
''')


def test_profile_edit_validation_and_payload():
    assert_ui(r'''
savedProfiles = [{id:5,name:'Athen',enabled:true,status:'due',cadence_days:3,routes:[]}];
editProfile(5);
assert.equal(el('#profileEditor').hidden, false);
assert.equal(el('#profileEditName').value, 'Athen');
assert.equal(el('#profileCadence').value, 3);
let changes;
changeProfile = async (id, payload) => {changes = [id,payload]; return true;};
el('#profileEditName').value = ' Neue Suche ';
el('#profileCadence').value = '7';
await saveProfileEdit();
assert.deepEqual(changes, [5,{name:'Neue Suche',cadence_days:7,price_target_minor:null}]);
assert.equal(el('#profileEditor').hidden, true);
editProfile(5);
el('#profileCadence').value='1.5'; changes=null;
await saveProfileEdit();
assert.equal(changes, null);
assert.equal(el('#profileEditor').hidden, false);
assert.equal(el('#profilesmsg').dataset.tone, 'err');
''')


def test_profile_load_rejects_http_failure_without_erasing_rows():
    assert_ui(r'''
savedProfiles = [{id:5,name:'Athen',routes:[]}]; renderProfiles();
const before = el('#profileList').innerHTML;
fetch = async () => ({ok:false,status:401,json:async()=>({detail:'Anmeldung erforderlich'})});
await loadProfiles();
assert.equal(el('#profileList').innerHTML, before);
assert.equal(el('#profilesmsg').dataset.tone, 'err');
assert(el('#profilesmsg').textContent.includes('Anmeldung erforderlich'));
''')


def test_profile_editor_is_not_nested_in_search_form():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="profileList"' in html
    assert 'id="profileEditor"' in html
    assert 'id="profileEditName"' in html
    assert 'id="profileCadence"' in html
    assert 'id="reloadProfiles"' in html


def test_profile_toggle_restores_focus_without_stealing_new_focus():
    assert_ui(r'''
savedProfiles = [{id:5,name:'Athen',enabled:true,status:'due',routes:[]}];
loadProfiles = async () => {}; loadScanner = async () => {};
const original = {dataset:{profileId:'5',profileAction:'toggle'}};
document.body = {}; document.activeElement = original;
let restored = 0, options;
const replacement = {disabled:false, focus(opts){ restored++; options=opts; document.activeElement=this; }};
el('#profileList').querySelector = () => replacement;
fetch = async () => {
  document.activeElement = document.body;
  restored = 0;
  return {ok:true,json:async()=>({profile:{...savedProfiles[0],enabled:false,status:'paused'}})};
};
await changeProfile(5,{enabled:false});
assert(restored >= 1);
assert.deepEqual(options,{preventScroll:true});
document.activeElement=original; restored=0;
const otherField = {};
fetch = async () => {
  document.activeElement=otherField;
  return {ok:true,json:async()=>({profile:{...savedProfiles[0],enabled:true,status:'due'}})};
};
await changeProfile(5,{enabled:true});
assert.equal(document.activeElement,otherField);
''')


def test_global_scanner_refreshes_profile_state_and_rejects_http_errors():
    assert_ui(r'''
let reloads=0, deals=0;
loadProfiles=async()=>{reloads++;}; loadDeals=async()=>{deals++;};
fetch=async()=>({ok:true,json:async()=>({jobs:[{name:'Athen'}]})});
await el('#runScanner').onclick();
assert.equal(reloads,1);
assert.equal(deals,1);
assert.equal(el('#runScanner').disabled,false);
fetch=async()=>({ok:false,status:401,json:async()=>({detail:'Anmeldung erforderlich'})});
await el('#runScanner').onclick();
assert(el('#scanstate').textContent.includes('Anmeldung erforderlich'));
assert.equal(reloads,1);
assert.equal(el('#runScanner').disabled,false);
''')


def test_saved_view_refreshes_on_entry_and_detects_automatic_starts():
    assert_ui(r'''
let loads=0, deals=0, callback, interval;
loadProfiles=async()=>{loads++;}; loadDeals=async()=>{deals++;}; loadScanner=async()=>{};
global.window={setTimeout(fn,ms){callback=fn;interval=ms;return 1;},clearTimeout(){}};
workspaceView='search'; setWorkspaceView('saved');
assert.equal(loads,1);
savedProfiles=[{id:5,enabled:true,status:'due'}];
scheduleProfileRefresh();
assert.equal(interval,30000);
await callback();
assert.equal(loads,2);
assert.equal(deals,2);
savedProfiles=[{id:5,enabled:true,status:'running'}]; scheduleProfileRefresh();
assert.equal(interval,5000);
workspaceView='search'; callback=null; scheduleProfileRefresh();
assert.equal(callback,null);
''')
