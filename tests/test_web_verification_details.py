"""Verification reasons stay separate from offer quality and price ranking."""

from tests.test_web_ui import run_ui_assertion


def assert_ui(code):
    result = run_ui_assertion(code)
    assert result.returncode == 0, result.stderr


def test_route_check_keeps_errors_partial_and_unsupported_distinct():
    assert_ui(r'''
const leg=status=>({verified:status==='verified',indicative:status==='indicative',
  verification:{status,method:status==='verified'?'cache':null}});
const row=statuses=>({verified:statuses.every(s=>s==='verified'),legs:statuses.map(leg)});
assert.equal(routeVerification(row(['verified','verified'])).status,'verified');
assert.equal(routeVerification(row(['verified','error'])).status,'partial');
assert.equal(routeVerification(row(['unsupported'])).status,'unsupported');
assert.equal(routeVerification(row(['unavailable'])).status,'unavailable');
assert.equal(routeVerification(row(['error','unavailable'])).status,'error');
assert.equal(routeVerification(row(['indicative'])).status,'candidate');
assert.equal(routeVerification({verified:true,legs:[{verified:true}]}).status,'unknown');
const bad=row(['verified']);bad.legs[0].indicative=true;
assert.notEqual(routeVerification(bad).status,'verified');
''')


def test_price_top_ten_uses_flight_price_and_counts_real_check_evidence():
    assert_ui(r'''
const row=(total,status)=>({total,verified:status==='verified',estimate:total-10,drift:10,
  legs:[{verified:status==='verified',indicative:false,verification:{status}}]});
const rows=Array.from({length:11},(_,i)=>row(i+100,i===10?'error':'verified'));
const text=verificationOverview(rows.reverse());
assert(text.includes('Preis-Top 10'));
assert(text.includes('10 geprüft'));
assert(!text.includes('gestört'));
assert(text.includes('10 Preisänderungen'));
assert.equal(verificationOverview([]),'');
assert(verificationOverview([{total:1,legs:[{}]}]).includes('nicht dokumentiert'));
''')


def test_leg_details_name_cache_and_failure_sources_without_raw_errors():
    assert_ui(r'''
const text=legVerificationMarkup({verification:{status:'error',checked_sources:['<script>'],
  failed_sources:['<script>'],method:null}});
assert(text.includes('Quelle gestört'));
assert(text.includes('&lt;script&gt;'));
assert(!text.includes('<script>'));
assert(legVerificationMarkup({verification:{status:'verified',method:'cache'}}).includes('Zwischenspeicher'));
assert(legVerificationMarkup({verification:{status:'unsupported'}}).includes('Keine Tagesprüfung'));
''')


def test_legacy_result_never_claims_price_unchanged():
    assert_ui(r'''
const row={total:80,verified:true,estimate:null,drift:null,dates:['2027-01-03'],
  legs:[{origin:'BER',destination:'ATH',date:'2027-01-03',price:80,verified:true}]};
const text=detailRows(row);
assert(!text.includes('Preis wie geschätzt'));
assert(!text.includes('Live geprüft'));
assert(text.includes('Prüfung nicht dokumentiert'));
''')


def test_top_ten_summary_survives_local_filters_and_clears_with_new_results():
    assert_ui(r'''
const row={total:80,verified:true,estimate:60,drift:20,dates:['2027-01-03'],
  legs:[{origin:'BER',destination:'ATH',date:'2027-01-03',price:80,verified:true,indicative:false,
         carriers:['FR'],verification:{status:'verified',method:'live'}}]};
renderTable([row]);
const text=el('#verificationSummary').textContent;
assert(text.includes('1 geprüft'));
el('#resultQuality').value='estimate';renderTable([row]);
assert.equal(el('#verificationSummary').textContent,text);
renderNoResults('Keine Treffer');
assert.equal(el('#verificationSummary').textContent,'');
''')
