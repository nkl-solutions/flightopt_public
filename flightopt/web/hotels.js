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
/* Ein Validierungsfehler kommt als Liste von Objekten. `data.detail` direkt in
   eine Meldung zu schreiben ergab dann woertlich "[object Object]". */
const detail = d => Array.isArray(d) ? d.map(e => e.msg || JSON.stringify(e)).join("; ")
                  : (typeof d === "string" ? d : "");
/* Dieselbe Schreibweise wie auf der Flugseite: ein ISO-Datum in einer deutschen
   Tabelle liest sich wie ein Datenbankfeld. */
function fmtDay(s){
  const d = new Date(String(s) + "T00:00:00");
  if (Number.isNaN(d.getTime())) return String(s || "");
  return d.toLocaleDateString("de-DE", {weekday:"short", day:"2-digit", month:"short"});
}
function fmtMoment(iso){
  const d = new Date(String(iso || ""));
  if (Number.isNaN(d.getTime())) return "";
  // Mit Jahr: gespeicherte Laeufe koennen aus einer laengst vergangenen Saison
  // stammen, und "08.09." allein sagt dann nicht, aus welcher.
  return d.toLocaleString("de-DE", {day:"2-digit", month:"2-digit",
    year:"numeric", hour:"2-digit", minute:"2-digit"});
}

const MODES = [
  {id:"single", label:"Festes Datum"},
  {id:"window", label:"Zeitraum"},
];
/* Fuenf Stufen plus "keine Basis". Die Reihenfolge ist die Sortierung im
   Zeitraum-Modus: ein Preisfehler steht oben, eine fehlende Basis unten.
   `encoding_suspect` kam vom Detektor, war hier aber nicht eingetragen: ein
   als kaputt erkannter Preis fiel damit auf "keine Basis" und sah aus wie eine
   fehlende Aussage statt wie ein Befund. */
const SIGNALS = {
  error:             {label:"Preis auffällig", rank:0},
  encoding_suspect:  {label:"Preis unklar",  rank:1},
  cheap:             {label:"günstig",       rank:2},
  normal:            {label:"normal",        rank:3},
  expensive:         {label:"teuer",         rank:4},
  unknown:           {label:"Kein Vergleich", rank:5},
};
/* Woher die Einordnung kommt. Ohne das wirkt jede Stufe gleich belastbar,
   egal ob drei oder dreihundert Vergleichspreise dahinterstehen. */
const BASIS_LABELS = {own:"bisherige Preise dieses Hotels", peer:"ähnliche Hotels"};
const PHASE_LABELS = {
  planning:  "Suche startet",
  day:       "Hotelpreise werden geladen",
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
let hotelViewGeneration = 0;

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
    out.textContent = "Enddatum liegt vor dem Startdatum.";
    out.dataset.tone = "err";
    return;
  }
  if (n > MAX_DAYS){
    out.textContent = `${n} Anreisetage. Maximal ${MAX_DAYS} pro Suche.`;
    out.dataset.tone = "err";
    return;
  }
  out.textContent = n === 1 ? "1 Anreisetag" : `${n} Anreisetage`;
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
  const total = Number(p && p.total) || 0;
  const done = Number(p && p.done) || 0;
  // Ohne Gesamtzahl ist jeder Prozentwert geraten. Ein wanderndes Stueck sagt
  // stattdessen "laeuft, Umfang noch offen" und behauptet keine Zahl.
  const pending = !TERMINAL.includes(phase) && !total;
  const bar = $("#progressbar");
  bar.className = "progressbar" + (phase === "failed" ? " failed" : "")
                + (pending ? " pending" : "");
  bar.setAttribute("aria-valuenow", String(pct));
  bar.setAttribute("aria-valuetext",
    pending ? "Läuft, Umfang noch offen" : `${pct} Prozent`);
  $("#progressfill").style.width = pending ? "" : pct + "%";
  $("#progresslabel").textContent = PHASE_LABELS[phase] || PHASE_LABELS.planning;
  const at = p && p.detail && p.detail.date ? `, ${fmtDay(p.detail.date)}` : "";
  $("#progresscount").textContent = total ? `${done} von ${total} Tagen${at}` : "";
}

let running = false;
function setRunning(on){
  running = on;
  $("#hgo").disabled = on;
  $("#hgo").textContent = on ? "Suche läuft" : "Hotels suchen";
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
  heading.focus({preventScroll:true});
  return true;
}

/* ---------------- Lauf ---------------- */

async function search(scanId){
  const body = payload(scanId);
  if (!body.destination){
    $("#destmsg").textContent = "Bitte Reiseziel eingeben.";
    $("#destmsg").dataset.tone = "err";
    return;
  }
  const generation = ++hotelViewGeneration;
  if (es){ es.close(); es = null; }
  $("#destmsg").textContent = "";
  $("#hlog").classList.add("on");
  $("#note").dataset.tone = "";
  $("#note").textContent = "Suche startet";
  setRunning(true);
  setProgress({phase:"planning"});
  // Eine Wiederaufnahme setzt den Lauf fort, also bleiben die Zeilen stehen,
  // die er vor dem Stopp schon gefunden hat.
  if (!scanId){ rows = []; $("#sort").value = defaultSort(); resetRunNotes(); draw(); }

  let data;
  try {
    const response = await fetch("/api/hotels/search", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    });
    data = await response.json();
    if (generation !== hotelViewGeneration) return;
    if (!response.ok) throw new Error(detail(data.detail) || `HTTP ${response.status}`);
  } catch (err) {
    if (generation !== hotelViewGeneration) return;
    setProgress({phase:"failed"});
    $("#note").textContent = String(err.message || err);
    $("#note").dataset.tone = "err";
    setRunning(false);
    return;
  }

  lastScan = data.scan_id;
  drawSources(data.sources || []);
  listen(data.scan_id);
}

/* Der Quellenstatus stand bisher als eine Textzeile in einem zugeklappten
   Optionsbereich. Wer dort nicht hineinsah, erfuhr nie, dass eine Quelle
   abgeschaltet war, und hielt ein halbes Ergebnis fuer ein ganzes. */
function drawSources(list){
  const items = Array.isArray(list) ? list : [];
  $("#sourcehint").innerHTML = items.map(s => {
    const on = s.active === true;
    const why = on ? "aktiv" : (s.reason || "abgeschaltet");
    return `<li data-active="${on}"><b>${esc(s.name)}</b> <span>${esc(why)}</span></li>`;
  }).join("");
  return items;
}

/* Was der Lauf nebenbei gemeldet hat. Die Kommandozeile zeigt genau diese
   Angaben seit jeher, der Browser warf sie weg. */
let runNotes = [];
function resetRunNotes(){
  runNotes = [];
  $("#runnotes").textContent = "";
  $("#runnotes").dataset.tone = "";
}
function addRunNotes(list){
  const items = (Array.isArray(list) ? list : []).map(v => String(v)).filter(Boolean);
  items.forEach(item => { if (!runNotes.includes(item)) runNotes.push(item); });
  if (!runNotes.length) return runNotes;
  $("#runnotes").dataset.tone = "warn";
  $("#runnotes").textContent = runNotes.join("\n");
  return runNotes;
}
function skippedNote(count){
  const n = Number(count) || 0;
  if (n <= 0) return "";
  return n === 1
    ? "1 Tag aus dem heutigen Zwischenspeicher."
    : `${n} Tage aus dem heutigen Zwischenspeicher.`;
}

function listen(scanId){
  const stream = new EventSource(`/api/hotels/scan/${scanId}/events`);
  es = stream;
  stream.onmessage = ev => {
    if (es !== stream) return;
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
    // Ausgefallene Tage und Quellen stehen im Abschlussereignis und blieben
    // bisher unsichtbar: das Ergebnis war einfach duenner.
    addRunNotes([skippedNote(p.detail && p.detail.skipped)]
      .concat((p.detail && p.detail.errors) || []));
    if (es){ es.close(); es = null; }
    setRunning(false);
    draw();
    if (p.phase === "done"){ focusResults(); }
    else { $("#resume").hidden = false; }
    loadScans();
  };
  stream.onerror = () => {
    if (es !== stream) return;
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
  $("#note").textContent = "Suche abgebrochen";
  $("#resume").hidden = lastScan === null;
  if (lastScan === null) return;
  try {
    const r = await fetch(`/api/hotels/scan/${lastScan}/cancel`, {method:"POST"});
    if (!r.ok) throw new Error(String(r.status));
  } catch {
    $("#note").dataset.tone = "warn";
    $("#note").textContent = "Abbruch vom Server nicht bestätigt.";
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
    if (how === "date") return String(a.date || "").localeCompare(String(b.date || ""))
                            || price(a) - price(b);
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

/* Eine Bewertung von 9,2 aus acht Stimmen ist etwas anderes als 9,2 aus
   dreitausend. Die Zahl kommt vom Server mit und stand bisher nirgends. */
function ratingCell(r){
  if (r.review_rating == null) return "-";
  const n = Number(r.review_count);
  const count = Number.isFinite(n) && n > 0
    ? `<small class="sub">${n.toLocaleString("de-DE")} ${n === 1 ? "Bewertung" : "Bewertungen"}</small>` : "";
  return `${grade(r.review_rating)}${count}`;
}
/* Worauf das Signal steht. Der Server liefert `basis` und `n`, die Oberflaeche
   liess beides fallen und jede Stufe sah gleich belastbar aus. */
function signalBasis(r){
  const parts = [];
  const n = Number(r && r.n);
  if (Number.isFinite(n) && n > 0) parts.push(`${n} Vergleichspreise`);
  const basis = BASIS_LABELS[String((r || {}).basis || "")];
  if (basis) parts.push(basis);
  return parts.join(", ");
}
function signalTitle(r){
  const why = (r && r.reason) || "";
  const basis = signalBasis(r);
  return [why, basis].filter(Boolean).join(" · ");
}
function rowHtml(r){
  const signal = SIGNALS[signalKey(r)];
  const url = safeHotelUrl(r.url);
  const name = esc(r.name);
  const book = url ? `<a class="hotelbook" href="${esc(url)}" target="_blank" rel="noopener noreferrer"`
    + ` aria-label="${esc(r.name)}: ${r.indicative === false ? "buchen" : "Angebot öffnen"}">`
    + `${r.indicative === false ? "Buchen" : "Angebot öffnen"}</a>`
    : `<small class="bookingmissing">Buchungslink fehlt</small>`;
  const native = r.native
    ? `<small class="native">umgerechnet aus ${money(r.native.amount)} ${esc(r.native.currency)}</small>`
    : "";
  /* Bei mehreren Naechten ist der Nachtpreis nicht das, was abgebucht wird.
     Der Gesamtpreis kam schon immer mit und wurde nie gezeigt. */
  const nights = Number(r.nights) || 1;
  const total = (nights > 1 && r.price_total != null)
    ? `<small class="native">${money(r.price_total)} ${esc(r.currency)} für ${nights} Nächte</small>`
    : "";
  const why = signalTitle(r) ? ` title="${esc(signalTitle(r))}"` : "";
  /* Auf schmalen Geraeten fallen Sterne, Bewertung und Quelle als Spalten weg.
     Die Angaben bleiben, sie ruecken unter den Namen. */
  const facts = [r.stars == null ? "" : `${r.stars} Sterne`,
                 r.review_rating == null ? "" : `Bewertung ${grade(r.review_rating)}`,
                 r.source].filter(Boolean).join(" · ");
  return `<tr>
      <td class="c-object">${name}${r.city ? ` <small>${esc(r.city)}</small>` : ""}<small
        class="facts">${esc(facts)}</small><small class="hoteldate">${esc(fmtDay(r.date))}`
        + `${nights > 1 ? ` · ${nights} Nächte` : ""}</small>${book}</td>
      <td class="c-num c-stars">${r.stars == null ? "-" : r.stars}</td>
      <td class="c-rail">${esc(fmtDay(r.date))}${nights > 1 ? ` (${nights} Nächte)` : ""}</td>
      <td class="c-price">${money(r.price_per_night)} <small>${esc(r.currency)}</small>${native}${total}
        <small class="sub">${r.indicative === false ? "Anbieterpreis" : "Richtwert"}</small>
        <small class="hotelsignal"${why}>${signal.label}</small></td>
      <td class="c-num c-rating">${ratingCell(r)}</td>
      <td class="c-status" data-signal="${esc(signalKey(r))}"${why}>${signal.label}</td>
      <td class="c-source">${esc(r.source)}</td>
    </tr>`;
}
function safeHotelUrl(value){
  if (typeof value !== "string" || /[\u0000-\u001f\u007f\\]/.test(value)) return "";
  const link = value.trim();
  if (!/^https:\/\//i.test(link)) return "";
  try {
    const url = new URL(link);
    return url.hostname && !url.username && !url.password ? link : "";
  } catch { return ""; }
}

function filtersActive(){
  return Boolean($("#filterSignal").value) || Boolean($("#filterStars").value)
      || Boolean($("#filterSource").value);
}
function resetFilters(){
  $("#filterSignal").value = "";
  $("#filterStars").value = "";
  $("#filterSource").value = "";
  draw();
}
/* Eine Live-Region, die bei jedem geholten Tag denselben Satz wiederholt, laesst
   einen Screenreader den ganzen Lauf lang reden. */
function announce(text){
  if ($("#outsummary").textContent !== text) $("#outsummary").textContent = text;
}
function draw(){
  drawSourceFilter();
  const list = visible();
  $("#hout").classList.add("on");
  $("#resetfilters").hidden = !filtersActive();
  $("#filtercount").textContent = list.length === rows.length
    ? `${rows.length} ${rows.length === 1 ? "Angebot" : "Angebote"}` : `${list.length} von ${rows.length} Angeboten`;
  announce(rows.length
    ? (rows.every(r => r.indicative !== false)
      ? "Richtwerte. Endpreis beim Anbieter prüfen."
      : "Anbieterpreise und Richtwerte sind getrennt markiert. Endpreis beim Anbieter prüfen.")
    : "");

  if (!list.length){
    /* Ein leerer Tabellenkopf ueber nichts sieht aus wie ein Fehler. Beide
       Faelle sagen jetzt, was los ist und was als naechstes hilft. */
    if (rows.length){
      $("#hotelrows").innerHTML = `<tr class="empty"><td colspan="7">Keine Angebote für diese Filter.</td></tr>`;
    } else if (running){
      // Waehrend der Lauf noch Tage holt, ist "nichts gefunden" eine Falschaussage.
      $("#hotelrows").innerHTML = `<tr class="empty"><td colspan="7">Hotelpreise werden geladen.</td></tr>`;
    } else {
      $("#hotelrows").innerHTML = `<tr class="empty"><td colspan="7">Keine Angebote erhalten.</td></tr>`;
    }
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
  const span = start === end ? fmtDay(start) : `${fmtDay(start)} bis ${fmtDay(end)}`;
  return `${scan.destination}, ${span}`;
}

function scanSummary(scan){
  const state = SCAN_STATUS[scan.status] || scan.status;
  const when = fmtMoment(scan.created_at);
  const found = scan.offers_found === 1 ? "1 Treffer" : `${scan.offers_found} Treffer`;
  return `${state}, ${found}, `
       + `${scan.days_done} von ${scan.days_total} Tagen, ${when}`;
}

function drawScans(list){
  $("#scanlist").innerHTML = list.length
    ? list.map(scan => `<li>
        <button type="button" data-scan="${scan.id}">
          <b>${esc(scanTitle(scan))}</b>
          <small>${esc(scanSummary(scan))}</small>
        </button></li>`).join("")
    : `<li class="hint">Noch keine Hotelsuche gespeichert.</li>`;
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
  if (running) return;
  const generation = ++hotelViewGeneration;
  try {
    const response = await fetch(`/api/hotels/scan/${scanId}/rows`);
    const data = await response.json();
    if (!response.ok) throw new Error(detail(data.detail) || `HTTP ${response.status}`);
    if (running || generation !== hotelViewGeneration) return;
    lastScan = data.scan_id;
    rows = data.rows || [];
    $("#hlog").classList.add("on");
    setProgress({phase: data.status === "done" ? "done" : "cancelled",
                 done: data.days_done, total: data.days_total});
    $("#note").dataset.tone = "";
    $("#note").textContent = rows.length
      ? `${scanTitle(data)}: ${rows.length} gespeicherte ${rows.length === 1 ? "Angebot" : "Angebote"}`
      : `${scanTitle(data)}: keine Angebote gespeichert`;
    $("#resume").hidden = data.status === "done";
    draw();
    focusResults();
  } catch (err) {
    if (running || generation !== hotelViewGeneration) return;
    $("#note").dataset.tone = "err";
    $("#note").textContent = String(err.message || err);
  }
}

/* ---------------- Daily hotel monitoring ---------------- */

let hotelWatches = [];
let hotelWatchBusy = false;
let hotelWatchLoad = 0;

function hotelWatchMessage(text, tone=""){
  $("#hwatchstatus").textContent = text;
  $("#hwatchstatus").dataset.tone = tone;
}
function hotelWatchControls(){
  $("#hwatchsave").disabled = hotelWatchBusy;
  $("#hwatchreload").disabled = hotelWatchBusy;
  $("#hwatchrun").disabled = hotelWatchBusy || !hotelWatches.some(w => w.enabled && w.due);
  document.querySelectorAll("[data-hwatch]").forEach(b => { b.disabled = hotelWatchBusy; });
}
function hotelWatchPayload(){
  const p = payload(null);
  const low = Number($("#hleadmin").value), high = Number($("#hleadmax").value);
  if (!p.destination) throw new Error("Bitte ein Reiseziel eingeben.");
  if (!$("#hleadmin").value || !$("#hleadmax").value
      || !Number.isInteger(low) || !Number.isInteger(high) || low < 0 || high > 365 || high < low)
    throw new Error("Vorlauf: ganze Tage von 0 bis 365, Ende nicht vor dem Anfang.");
  if (high - low + 1 > 31) throw new Error("Maximal 31 Anreisetage je Überwachung.");
  const {arrival, window_end, scan_id, ...preferences} = p;
  return {...preferences, country:"DE", lead_min_days:low, lead_max_days:high};
}
function updateHotelWatchSelection(){
  const p = payload(null);
  $("#hwatchselection").textContent = [p.destination || "Reiseziel fehlt",
    `${p.nights} Nächte`, `${p.adults + p.children.length} Gäste`, `${p.rooms} Zimmer`].join(" · ");
}
function drawHotelWatches(data){
  hotelWatches = Array.isArray(data.watches) ? data.watches : [];
  const summary = data.summary || {};
  $("#hwatchsummary").textContent = `${hotelWatches.length} gespeichert, ${Number(summary.due || 0)} fällig`;
  $("#hwatchlist").innerHTML = hotelWatches.length ? `<ul class="hotelwatchlist">`
    + hotelWatches.map(w => {
      const state = !w.enabled ? "Pausiert" : w.due ? "Heute fällig" : "Heute geprüft";
      const filters = [w.stars && w.stars.length ? `${w.stars.join(", ")} Sterne` : "",
        w.min_review_score ? `Bewertung ab ${grade(w.min_review_score)}` : ""].filter(Boolean).join(" · ");
      return `<li><div><b>${esc(w.destination)}</b><small>${Number(w.nights)} Nächte · `
        + `${Number(w.adults) + (w.children || []).length} Gäste · ${Number(w.rooms)} Zimmer · `
        + `${Number(w.lead_min_days)} bis ${Number(w.lead_max_days)} Tage Vorlauf</small>`
        + `${filters ? `<small>${esc(filters)}</small>` : ""}<small>${state}`
        + `${w.last_run_at ? " · " + esc(fmtMoment(w.last_run_at)) : ""}</small></div>`
        + `<button class="watchtoggle" type="button" data-hwatch="${Number(w.id)}"`
        + ` aria-pressed="${Boolean(w.enabled)}" aria-label="${esc(w.destination)}: Überwachung ${w.enabled ? "pausieren" : "aktivieren"}">`
        + `${w.enabled ? "Pausieren" : "Aktivieren"}</button></li>`;
    }).join("") + "</ul>" : `<p class="hint">Noch keine Hotelüberwachung gespeichert.</p>`;
  hotelWatchControls();
}
async function hotelWatchRequest(url, options){
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(detail(data.detail) || `HTTP ${response.status}`);
  return data;
}
async function loadHotelWatches(){
  const version = ++hotelWatchLoad;
  $("#hwatchlist").setAttribute("aria-busy", "true");
  try {
    const data = await hotelWatchRequest("/api/hotels/watchlist");
    if (version !== hotelWatchLoad) return;
    drawHotelWatches(data);
    try {
      const health = await hotelWatchRequest("/api/health/detail");
      if (version !== hotelWatchLoad) return;
      const sources = (health.sources && health.sources.hotel) || [];
      $("#hwatchsources").innerHTML = sources.map(s => `<li data-active="${Boolean(s.active)}">`
        + `<b>${esc(s.name)}</b> ${s.active ? "aktiv" : esc(s.reason || "nicht verfügbar")}</li>`).join("");
      const notes = [];
      if (!health.scheduler || !health.scheduler.running) notes.push("Automatische Suche nicht aktiv.");
      if (!sources.some(s => s.active)) notes.push("Keine Hotelquelle aktiv.");
      $("#hwatchnote").textContent = notes.join(" ");
      $("#hwatchnote").dataset.tone = notes.length ? "warn" : "";
    } catch {
      if (version !== hotelWatchLoad) return;
      $("#hwatchnote").textContent = "Status der automatischen Suche nicht verfügbar.";
      $("#hwatchnote").dataset.tone = "warn";
    }
  } catch (err){
    if (version === hotelWatchLoad) hotelWatchMessage(String(err.message || err), "err");
  } finally {
    if (version === hotelWatchLoad) $("#hwatchlist").setAttribute("aria-busy", "false");
  }
}
async function saveHotelWatch(){
  if (hotelWatchBusy) return;
  let p;
  try { p = hotelWatchPayload(); } catch (err){ hotelWatchMessage(err.message, "err"); return; }
  hotelWatchBusy = true; hotelWatchControls();
  hotelWatchMessage("Überwachung wird gespeichert.");
  try {
    await hotelWatchRequest("/api/hotels/watchlist", {method:"POST",
      headers:{"Content-Type":"application/json"}, body:JSON.stringify(p)});
    hotelWatchMessage("Hotelüberwachung gespeichert.", "ok");
    await loadHotelWatches();
  } catch (err){ hotelWatchMessage(String(err.message || err), "err"); }
  finally { hotelWatchBusy = false; hotelWatchControls(); }
}
async function toggleHotelWatch(id){
  if (hotelWatchBusy) return;
  const watch = hotelWatches.find(w => Number(w.id) === id);
  if (!watch) return;
  const focused = document.activeElement;
  const restoreFocus = focused && focused.dataset && Number(focused.dataset.hwatch) === id;
  hotelWatchBusy = true; hotelWatchControls();
  try {
    const updated = await hotelWatchRequest(`/api/hotels/watchlist/${id}`, {method:"PATCH",
      headers:{"Content-Type":"application/json"}, body:JSON.stringify({enabled:!watch.enabled})});
    const watches = hotelWatches.map(w => Number(w.id) === id ? {...w, ...updated} : w);
    drawHotelWatches({watches, summary:{due:watches.filter(w => w.enabled && w.due).length}});
    hotelWatchMessage(watch.enabled ? "Überwachung pausiert." : "Überwachung aktiviert.", "ok");
    await loadHotelWatches();
  } catch (err){ hotelWatchMessage(String(err.message || err), "err"); }
  finally {
    hotelWatchBusy = false; hotelWatchControls();
    const here = document.activeElement;
    if (restoreFocus && (!here || here === document.body || here === focused)){
      const button = document.querySelector(`[data-hwatch="${id}"]`);
      if (button) button.focus({preventScroll:true});
    }
  }
}
async function runHotelWatches(){
  if (hotelWatchBusy) return;
  hotelWatchBusy = true; hotelWatchControls();
  hotelWatchMessage("Fällige Hotelsuchen werden geprüft.");
  try {
    const data = await hotelWatchRequest("/api/hotels/watchlist/run-once", {method:"POST"});
    const errors = Array.isArray(data.errors) ? data.errors : [];
    hotelWatchMessage(`${Number(data.watches || 0)} Suchen geprüft, ${Number(data.observations || 0)} Preise gespeichert.`
      + (data.due_left ? ` ${Number(data.due_left)} noch fällig.` : "")
      + (errors.length ? " " + errors.slice(0, 3).map(String).join("; ") : ""), errors.length ? "warn" : "ok");
    await loadHotelWatches();
    await loadScans();
  } catch (err){ hotelWatchMessage(String(err.message || err), "err"); }
  finally { hotelWatchBusy = false; hotelWatchControls(); }
}

/* ---------------- Start ---------------- */

/* `toISOString` rechnet in UTC. Wer abends in Berlin sucht, bekam damit den
   Vortag: dieselbe Falle, die die Flugseite mit `isoDay` schon umgeht. */
function isoDay(date){
  const pad = n => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}
function today(offset){
  const d = new Date();
  d.setDate(d.getDate() + offset);
  return isoDay(d);
}

function checkWindow(){
  const msg = $("#windowmsg");
  const from = $("#from").value, to = $("#to").value;
  if (mode === "window" && from && to && to < from){
    msg.dataset.tone = "err";
    msg.textContent = "Enddatum liegt vor dem Startdatum.";
    $("#to").setAttribute("aria-invalid", "true");
    return false;
  }
  msg.dataset.tone = "";
  msg.textContent = "";
  $("#to").setAttribute("aria-invalid", "false");
  return true;
}

function boot(){
  drawModes();
  drawStars();
  // Ein Hotel fuer gestern gibt es nicht. Der Waehler darf ihn gar nicht anbieten.
  $("#from").min = today(0);
  $("#to").min = today(0);
  $("#from").value = today(60);
  $("#to").value = today(66);
  drawAges();
  sizeUp();
  updateHotelWatchSelection();

  $("#kids").addEventListener("input", drawAges);
  ["#from", "#to", "#nights"].forEach(id =>
    $(id).addEventListener("input", () => { checkWindow(); sizeUp(); }));
  ["#sort", "#filterSignal", "#filterStars", "#filterSource"].forEach(id =>
    $(id).addEventListener("change", draw));
  $("#resetfilters").addEventListener("click", resetFilters);
  $("#hf").addEventListener("submit", e => { e.preventDefault(); search(null); });
  $("#hcancel").addEventListener("click", cancelScan);
  $("#resume").addEventListener("click", () => search(lastScan));
  $("#scanlist").addEventListener("click", e => {
    const button = e.target.closest("[data-scan]");
    if (button) openScan(Number(button.dataset.scan));
  });
  $("#hwatchform").addEventListener("submit", e => { e.preventDefault(); saveHotelWatch(); });
  $("#hwatchrun").addEventListener("click", runHotelWatches);
  $("#hwatchreload").addEventListener("click", loadHotelWatches);
  $("#hwatchlist").addEventListener("click", e => {
    const button = e.target.closest("[data-hwatch]");
    if (button) toggleHotelWatch(Number(button.dataset.hwatch));
  });
  $("#hf").addEventListener("input", updateHotelWatchSelection);
  $("#hf").addEventListener("change", updateHotelWatchSelection);
  loadHotelWatches();
  loadScans();
}

if (typeof document !== "undefined" && typeof document.getElementById === "function"
    && document.getElementById("hf")) boot();
