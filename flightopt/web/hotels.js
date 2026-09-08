/* Hotelseite. Dieselben Muster wie die Flugsuche, nur ohne Route:
   ein Ziel, ein Fenster, ein Preis je Nacht.

   Der Lauf haengt nicht mehr an der Anfrage: die Suche legt einen Job an, der
   Strom liefert jeden Tag einzeln nach. Abbrechen und Fortsetzen sind damit
   dasselbe wie auf der Flugseite, und die alte Grenze von 14 Tagen faellt.

   Sortieren und Filtern laufen im Browser. Wer die Ansicht dreht, startet
   keine zweite Suche und wartet nicht noch einmal. */
const $ = s => document.querySelector(s);
const esc = v => String(v ?? "").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const money = v => Number(v || 0).toLocaleString("de-DE",
  {minimumFractionDigits: 2, maximumFractionDigits: 2});
const grade = v => Number(v).toLocaleString("de-DE",
  {minimumFractionDigits: 1, maximumFractionDigits: 1});

const MODES = [
  {id:"single", label:"Einzeltag"},
  {id:"window", label:"Zeitraum"},
];
/* Vier Stufen plus "keine Basis". Die Reihenfolge ist die Sortierung im
   Zeitraum-Modus: ein Preisfehler steht oben, eine fehlende Basis unten. */
const SIGNALS = {
  error:     {label:"Preisfehler", rank:0},
  cheap:     {label:"günstig",     rank:1},
  normal:    {label:"normal",      rank:2},
  expensive: {label:"teuer",       rank:3},
  unknown:   {label:"keine Basis", rank:4},
};
const PHASE_LABELS = {
  planning:  "Lauf wird vorbereitet",
  day:       "Tage werden geholt",
  done:      "Fertig",
  failed:    "Fehler",
  cancelled: "Abgebrochen",
};
const TERMINAL = ["done", "failed", "cancelled"];
const MAX_DAYS = 400;

let mode = "single";
let stars = new Set();
let rows = [];
let lastScan = null;
let es = null;

/* ---------------- Formular ---------------- */

function drawModes(){
  const box = $("#modeoptions");
  if (!box.children.length){
    box.innerHTML = MODES.map(m => `
      <label class="segopt">
        <input type="radio" name="hmode" value="${m.id}"${m.id===mode?" checked":""}>
        <span>${esc(m.label)}</span>
      </label>`).join("");
    box.addEventListener("change", e => {
      if (e.target.name !== "hmode") return;
      mode = e.target.value;
      applyMode();
    });
  }
  applyMode();
}

function applyMode(){
  const single = mode === "single";
  $("#towrap").hidden = single;
  $("#to").disabled = single;
  $("#sort").value = defaultSort();
  sizeUp();
}

/* Ein Zeitraum bringt viele Zeilen; die Signalspalte ordnet sie. Ein Einzeltag
   hat nur eine Nacht, da hilft der Preis mehr als das Signal. */
function defaultSort(){ return mode === "window" ? "signal" : "price"; }

function drawStars(){
  const box = $("#stars");
  box.innerHTML = [5,4,3,2,1].map(n => `
    <button class="chip" type="button" data-star="${n}" aria-pressed="false">
      ${n} Sterne
    </button>`).join("");
  box.addEventListener("click", e => {
    const chip = e.target.closest("[data-star]");
    if (!chip) return;
    const n = Number(chip.dataset.star);
    if (stars.has(n)) stars.delete(n); else stars.add(n);
    chip.setAttribute("aria-pressed", stars.has(n) ? "true" : "false");
  });
}

function drawAges(){
  const count = Math.max(0, Number($("#kids").value || 0));
  const box = $("#ages");
  $("#agesrow").hidden = count === 0;
  const have = box.children.length;
  if (have === count) return;
  box.innerHTML = Array.from({length: count}, (_, i) => `
    <div>
      <label class="lab" for="age${i}">Kind ${i + 1}</label>
      <input type="number" id="age${i}" class="kidage" min="0" max="17" value="8">
    </div>`).join("");
}

function days(){
  const from = $("#from").value;
  const to = mode === "single" ? from : ($("#to").value || from);
  if (!from || !to) return 0;
  const span = (new Date(to) - new Date(from)) / 86400000;
  return span < 0 ? -1 : Math.round(span) + 1;
}

function sizeUp(){
  const n = days();
  const out = $("#sizing");
  if (!n){ out.textContent = ""; out.dataset.tone = ""; return; }
  if (n < 0){
    out.textContent = "Das Ende liegt vor dem Anfang.";
    out.dataset.tone = "err";
    return;
  }
  if (n > MAX_DAYS){
    out.textContent = `${n} Tage. Je Lauf sind ${MAX_DAYS} Tage vorgesehen, `
                    + "teile den Zeitraum auf.";
    out.dataset.tone = "err";
    return;
  }
  out.textContent = n === 1 ? "1 Tag, eine Anfrage je Quelle."
                            : `${n} Tage, ${n} Anfragen je Quelle.`;
  out.dataset.tone = n > 60 ? "warn" : "";
}

function payload(scanId){
  const ages = [...document.querySelectorAll(".kidage")].map(i => Number(i.value || 0));
  const review = $("#minReview").value;
  return {
    destination: $("#destination").value.trim(),
    arrival: $("#from").value,
    window_end: mode === "single" ? $("#from").value : ($("#to").value || $("#from").value),
    nights: Number($("#nights").value || 1),
    adults: Number($("#adults").value || 1),
    children: ages,
    rooms: Number($("#rooms").value || 1),
    stars: [...stars].sort(),
    min_review_score: review === "" ? null : Number(review),
    currency: $("#currency").value,
    scan_id: scanId || null,
  };
}

/* ---------------- Fortschritt ---------------- */

function progressPercent(p){
  const phase = (p && p.phase) || "planning";
  if (TERMINAL.includes(phase)) return 100;
  const total = Number(p && p.total) || 0;
  const done = Number(p && p.done) || 0;
  if (!total) return 0;
  return Math.max(0, Math.min(99, Math.round((done / total) * 100)));
}

function setProgress(p){
  const phase = (p && p.phase) || "planning";
  const pct = progressPercent(p);
  const bar = $("#progressbar");
  bar.className = "progressbar" + (phase === "failed" ? " failed" : "");
  bar.setAttribute("aria-valuenow", String(pct));
  $("#progressfill").style.width = pct + "%";
  $("#progresslabel").textContent = PHASE_LABELS[phase] || PHASE_LABELS.planning;
  const total = Number(p && p.total) || 0;
  const done = Number(p && p.done) || 0;
  const at = p && p.detail && p.detail.date ? `, ${p.detail.date}` : "";
  $("#progresscount").textContent = total ? `${done} von ${total} Tagen${at}` : "";
}

function setRunning(on){
  $("#hgo").disabled = on;
  $("#hgo").textContent = on ? "Suche läuft" : "Suchen";
  if (on) $("#hgo").classList.add("busy"); else $("#hgo").classList.remove("busy");
  // Eigenschaft statt Attribut: der Node-Harness kennt kein removeAttribute,
  // im Browser entfernt `hidden = false` das Attribut genauso.
  $("#hcancel").hidden = !on;
  if (on) $("#resume").hidden = true;
}

/* Nach dem Lauf springt der Fokus auf die Ergebnisueberschrift, damit der
   naechste Tab in den Ergebnissen landet. Wer waehrenddessen tippt, verloere
   dabei seinen Cursor, also nur springen, wenn gerade niemand in einem Feld
   steht. */
function focusIsIdle(){
  const here = document.activeElement;
  return !here || here === document.body || here === $("#hgo") || here === $("#hcancel");
}

function focusResults(){
  const heading = $("#outtitle");
  if (!heading || typeof heading.focus !== "function") return false;
  if (!focusIsIdle()) return false;
  heading.focus();
  return true;
}

/* ---------------- Lauf ---------------- */

async function search(scanId){
  const body = payload(scanId);
  if (!body.destination){
    $("#destmsg").textContent = "Ohne Ziel keine Suche.";
    $("#destmsg").dataset.tone = "err";
    return;
  }
  if (es){ es.close(); es = null; }
  $("#destmsg").textContent = "";
  $("#hlog").classList.add("on");
  $("#note").dataset.tone = "";
  $("#note").textContent = "Suche startet";
  setRunning(true);
  setProgress({phase:"planning"});
  // Eine Wiederaufnahme setzt den Lauf fort, also bleiben die Zeilen stehen,
  // die er vor dem Stopp schon gefunden hat.
  if (!scanId){ rows = []; $("#sort").value = defaultSort(); draw(); }

  let data;
  try {
    const response = await fetch("/api/hotels/search", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    });
    data = await response.json();
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  } catch (err) {
    setProgress({phase:"failed"});
    $("#note").textContent = String(err.message || err);
    $("#note").dataset.tone = "err";
    setRunning(false);
    return;
  }

  lastScan = data.scan_id;
  $("#sourcehint").textContent = (data.sources || [])
    .map(s => `${s.name}: ${s.active ? "aktiv" : s.reason}`).join("   ");
  listen(data.scan_id);
}

function listen(scanId){
  es = new EventSource(`/api/hotels/scan/${scanId}/events`);
  es.onmessage = ev => {
    const p = JSON.parse(ev.data);
    setProgress(p);
    if (p.message){
      $("#note").textContent = p.message;
      $("#note").dataset.tone = p.phase === "failed" ? "err" : "";
    }
    if (p.phase === "day"){ addRows((p.detail && p.detail.rows) || []); return; }
    if (!TERMINAL.includes(p.phase)) return;
    const finalRows = (p.detail && p.detail.rows) || [];
    if (finalRows.length) rows = finalRows;
    draw();
    if (es){ es.close(); es = null; }
    setRunning(false);
    if (p.phase === "done"){ focusResults(); }
    else { $("#resume").hidden = false; }
    loadScans();
  };
  es.onerror = () => {
    if (!es) return;
    setProgress({phase:"failed"});
    $("#note").dataset.tone = "err";
    $("#note").textContent = "Verbindung zum Server verloren";
    es.close(); es = null;
    setRunning(false);
    $("#resume").hidden = false;
  };
}

function addRows(list){
  if (!list.length) return;
  rows = rows.concat(list);
  draw();
}

/* Abbrechen schliesst zuerst den Strom und meldet dann dem Server, dass er
   zwischen zwei Tagen aufhoeren soll. Andersherum kaeme noch ein Ereignis an. */
async function cancelScan(){
  if (es){ es.close(); es = null; }
  setRunning(false);
  setProgress({phase:"cancelled"});
  $("#note").dataset.tone = "";
  $("#note").textContent = "Durchlauf abgebrochen";
  $("#resume").hidden = lastScan === null;
  if (lastScan === null) return;
  try {
    const r = await fetch(`/api/hotels/scan/${lastScan}/cancel`, {method:"POST"});
    if (!r.ok) throw new Error(String(r.status));
  } catch {
    $("#note").dataset.tone = "warn";
    $("#note").textContent = "Lauf lokal gestoppt, der Server hat den Abbruch nicht bestätigt.";
  }
  loadScans();
}

/* ---------------- Tabelle ---------------- */

/* Die Stufe schlaegt den Status: liefert der Detektor eine, wird sie benutzt,
   sonst bleibt es beim Status. Unbekanntes faellt auf "keine Basis". */
function signalKey(row){
  const key = (row && (row.tier || row.signal)) || "unknown";
  return SIGNALS[key] ? key : "unknown";
}

function sortRows(list, how){
  const rank = r => SIGNALS[signalKey(r)].rank;
  const price = r => Number(r.price_per_night || 0);
  return list.slice().sort((a, b) => {
    if (how === "price") return price(a) - price(b);
    if (how === "date") return a.date.localeCompare(b.date) || price(a) - price(b);
    if (how === "stars") return (b.stars || 0) - (a.stars || 0) || price(a) - price(b);
    if (how === "rating") return (b.review_rating || 0) - (a.review_rating || 0) || price(a) - price(b);
    if (how === "source") return String(a.source || "").localeCompare(String(b.source || ""))
                              || price(a) - price(b);
    return rank(a) - rank(b) || price(a) - price(b);
  });
}

function visible(){
  const wantSignal = $("#filterSignal").value;
  const wantStars = $("#filterStars").value;
  const wantSource = $("#filterSource").value;
  const list = rows.filter(r =>
    (!wantSignal || signalKey(r) === wantSignal) &&
    (!wantStars || String(r.stars) === wantStars) &&
    (!wantSource || r.source === wantSource));
  return sortRows(list, $("#sort").value);
}

function drawSourceFilter(){
  const box = $("#filterSource");
  const names = [...new Set(rows.map(r => r.source).filter(Boolean))].sort();
  const keep = box.value;
  box.innerHTML = ['<option value="">Alle</option>']
    .concat(names.map(n => `<option value="${esc(n)}">${esc(n)}</option>`)).join("");
  box.value = names.includes(keep) ? keep : "";
}

function rowHtml(r){
  const signal = SIGNALS[signalKey(r)];
  /* Nur https, und nur nach esc. Eine Quelle, die eine javascript:-Adresse
     liefert, bekommt hier keinen Link, sondern nur ihren Namen. */
  const safe = typeof r.url === "string" && r.url.startsWith("https://");
  const name = safe
    ? `<a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">${esc(r.name)}</a>`
    : esc(r.name);
  const native = r.native
    ? `<small class="native">umgerechnet aus ${money(r.native.amount)} ${esc(r.native.currency)}</small>`
    : "";
  const why = r.reason ? ` title="${esc(r.reason)}"` : "";
  return `<tr>
      <td>${name}${r.city ? ` <small>${esc(r.city)}</small>` : ""}</td>
      <td class="c-num">${r.stars == null ? "-" : r.stars}</td>
      <td class="c-rail mono">${esc(r.date)}${r.nights > 1 ? ` (${r.nights} Nächte)` : ""}</td>
      <td class="c-price">${money(r.price_per_night)} <small>${esc(r.currency)}</small>${native}</td>
      <td class="c-num">${r.review_rating == null ? "-" : grade(r.review_rating)}</td>
      <td class="c-status" data-signal="${esc(signalKey(r))}"${why}>${signal.label}</td>
      <td class="c-source">${esc(r.source)}</td>
    </tr>`;
}

function draw(){
  drawSourceFilter();
  const list = visible();
  $("#hout").classList.add("on");
  $("#filtercount").textContent = list.length === rows.length
    ? `${rows.length} Zeilen` : `${list.length} von ${rows.length}`;
  $("#outsummary").textContent = rows.length
    ? "Preise sind Richtwerte der Quelle. Je Haus nennt sie genau einen Preis."
    : "";

  if (!list.length){
    $("#hotelrows").innerHTML = rows.length
      ? `<tr class="empty"><td colspan="7">Keine Zeile passt zu diesen Filtern.</td></tr>`
      : "";
    return;
  }
  $("#hotelrows").innerHTML = list.map(rowHtml).join("");
}

/* ---------------- Gespeicherte Laeufe ---------------- */

const SCAN_STATUS = {
  pending:   "wartet",
  running:   "läuft",
  done:      "fertig",
  failed:    "gescheitert",
  cancelled: "abgebrochen",
};

/* Die Liste liefert window_start/window_end, ein einzelner Lauf ein
   verschachteltes window. Beides ergibt dieselbe Zeile. */
function scanTitle(scan){
  const start = scan.window_start || (scan.window && scan.window.start) || "";
  const end = scan.window_end || (scan.window && scan.window.end) || start;
  const span = start === end ? start : `${start} bis ${end}`;
  return `${scan.destination}, ${span}`;
}

function scanSummary(scan){
  const state = SCAN_STATUS[scan.status] || scan.status;
  const when = String(scan.created_at || "").slice(0, 16).replace("T", " ");
  return `${state}, ${scan.offers_found} Treffer, `
       + `${scan.days_done} von ${scan.days_total} Tagen, ${when}`;
}

function drawScans(list){
  $("#scanlist").innerHTML = list.length
    ? list.map(scan => `<li>
        <button type="button" data-scan="${scan.id}">
          <b>${esc(scanTitle(scan))}</b>
          <small>${esc(scanSummary(scan))}</small>
        </button></li>`).join("")
    : `<li class="hint">Noch kein Lauf gespeichert.</li>`;
}

async function loadScans(){
  try {
    const response = await fetch("/api/hotels/scans?limit=12");
    if (!response.ok) return;
    const data = await response.json();
    drawScans(data.scans || []);
  } catch {
    /* Die Liste ist Beiwerk. Ohne sie bleibt das Ergebnis trotzdem stehen. */
  }
}

async function openScan(scanId){
  try {
    const response = await fetch(`/api/hotels/scan/${scanId}/rows`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    lastScan = data.scan_id;
    rows = data.rows || [];
    $("#hlog").classList.add("on");
    setProgress({phase: data.status === "done" ? "done" : "cancelled",
                 done: data.days_done, total: data.days_total});
    $("#note").dataset.tone = "";
    $("#note").textContent = rows.length
      ? `${scanTitle(data)}: ${rows.length} Angebote aus einem früheren Lauf`
      : `${scanTitle(data)}: für diesen Lauf sind keine Zeilen mehr gespeichert`;
    $("#resume").hidden = data.status === "done";
    draw();
    focusResults();
  } catch (err) {
    $("#note").dataset.tone = "err";
    $("#note").textContent = String(err.message || err);
  }
}

/* ---------------- Start ---------------- */

function today(offset){
  const d = new Date();
  d.setDate(d.getDate() + offset);
  return d.toISOString().slice(0, 10);
}

function boot(){
  drawModes();
  drawStars();
  $("#from").value = today(60);
  $("#to").value = today(66);
  drawAges();
  sizeUp();

  $("#kids").addEventListener("input", drawAges);
  ["#from", "#to", "#nights"].forEach(id =>
    $(id).addEventListener("input", sizeUp));
  ["#sort", "#filterSignal", "#filterStars", "#filterSource"].forEach(id =>
    $(id).addEventListener("change", draw));
  $("#hf").addEventListener("submit", e => { e.preventDefault(); search(null); });
  $("#hcancel").addEventListener("click", cancelScan);
  $("#resume").addEventListener("click", () => search(lastScan));
  $("#scanlist").addEventListener("click", e => {
    const button = e.target.closest("[data-scan]");
    if (button) openScan(Number(button.dataset.scan));
  });
  loadScans();
}

if (typeof document !== "undefined" && typeof document.getElementById === "function"
    && document.getElementById("hf")) boot();
