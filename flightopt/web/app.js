const $ = s => document.querySelector(s);
const esc = v => String(v ?? "").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const detail = d => Array.isArray(d) ? d.map(e=>e.msg||JSON.stringify(e)).join("; ")
                  : (typeof d==="string" ? d : "");
/* Preise stehen in der Copy, nicht in einer Tabellenspalte: deutsche Schreibweise. */
const money = v => Number(v || 0).toLocaleString("de-DE",
  {minimumFractionDigits: 2, maximumFractionDigits: 2});

const TRIPS = [
  {id:"return",  label:"Hin und zurück", min:2, stays:true},
  {id:"one_way", label:"Nur hin",         min:2, stays:false},
  {id:"multi",   label:"Mehrere Stopps",  min:3, stays:true},
];
const PHASES = [["planning","Planen"],["routes","Strecken"],["fetching","Preise"],
                ["solving","Kombinieren"],["verifying","Nachprüfen"],["done","Fertig"]];
const PROGRESS_LABELS = {
  planning:"Suche vorbereiten",
  routes:"Strecken prüfen",
  fetching:"Preise abrufen",
  solving:"Kombinieren",
  verifying:"Live prüfen",
  staying:"Hotelpreise holen",
  done:"Fertig",
  failed:"Fehler",
  cancelled:"Abgebrochen",
};
const PROGRESS_RANGES = {
  planning:[2, 10],
  routes:[12, 16],
  fetching:[30, 30],
  solving:[64, 16],
  verifying:[84, 8],
  staying:[92, 7],
  done:[100, 0],
  failed:[100, 0],
  cancelled:[100, 0],
};
const SEARCH_WARNINGS = {
  largeCombinations: 20000,
  hugeCombinations: 100000,
  manyCells: 500,
};
/* Rang, Preis, Gesamt, Status, Preislage, Daten, Nächte, Umstiege, Airlines.
   Eine Zahl an einer Stelle, sonst laufen Kopf und colspan auseinander. */
const RESULT_COLUMNS = 9;

let trip = "return";
/* `hops` holds everything ever typed. A tab only changes how many of them are
   shown, so switching back and forth never throws work away. */
let hops = [{code:"BER", label:"Berlin"}, {code:"ATH", label:"Athen"}];
let stayRanges = [[3, 10]];
const shown = () => Math.max(TRIPS.find(t=>t.id===trip).min,
                             trip==="multi" ? hops.length : 0);
const active = () => hops.slice(0, trip==="multi" ? hops.length : 2);
let AIRLINES = [], picked = new Set();
let axis = null, es = null, lastResults = [], currentJob = null;

/* ---------------- trip type ---------------- */
/* Drei sich ausschliessende Optionen sind eine Radiogruppe, keine Reiter.
   Reiter versprechen Panels, die es hier nicht gibt, und brauchen Pfeiltasten,
   Roving-Tabindex und aria-controls fuer nichts.
   Ein Neuaufbau wuerde genau das Radio wegwerfen, das gerade `change` gemeldet
   hat: der Fokus faellt dann auf <body> und die naechste Pfeiltaste bewegt
   nichts mehr. Also nur bauen, wenn die Gruppe fehlt, sonst bloss umhaken. */
function drawTrips(){
  const box = $("#tripoptions");
  let inputs = [...box.querySelectorAll("input")];
  if (inputs.length !== TRIPS.length){
    box.innerHTML = TRIPS.map(t =>
      `<label class="segopt"><input type="radio" name="trip" value="${t.id}"${
        t.id===trip?" checked":""}><span>${esc(t.label)}</span></label>`).join("");
    inputs = [...box.querySelectorAll("input")];
    inputs.forEach(r => r.onchange = () => setTrip(r.value));
  }
  inputs.forEach(r => { r.checked = r.value === trip; });
}
function setTrip(id){
  const next = TRIPS.find(t=>t.id===id);
  trip = id;
  // Grow the list if the new type needs more fields, but never shrink it:
  // a third stop typed under "Mehrere Stopps" is still there on the way back.
  while (hops.length < next.min) hops.push({code:"",label:""});
  setStayEnabled(next.stays);
  drawTrips(); drawRoute(); syncStayControls(); size(); saveForm();
}

function bagLabel(count){
  if (!count) return "ohne Aufgabegepäck";
  return count === 1 ? "1 Aufgabegepäckstück" : `${count} Aufgabegepäckstücke`;
}
/* "automatisch nach Entfernung" ist die Vorgabe und sagt dem Ueberblick nichts.
   Eine bewusst gesetzte Grenze dagegen entscheidet mit ueber den Preis. */
const STOPS_LABELS = {
  "0": "ohne Umstieg",
  "1": "bis 1 Umstieg",
  "2": "bis 2 Umstiege",
};
function maxStopsLabel(){
  return STOPS_LABELS[String($("#maxStops").value ?? "")] || "";
}
function routeGlance(){
  const route = active().map(h => h.label || h.code).filter(Boolean).join(" - ");
  const from = $("#from").value || "offenes Startdatum";
  const to = $("#to").value || "offenes Enddatum";
  const carriers = picked.size ? [...picked].join(", ") : "alle verfügbaren Airlines";
  const bag = bagLabel(Number($("#checkedBags").value || 0));
  return {route, from, to, carriers, bag, stops: maxStopsLabel()};
}
function updateRouteGlance(){
  const g = routeGlance();
  const parts = [g.bag, g.carriers];
  if (g.stops) parts.splice(1, 0, g.stops);
  $("#routeglance").innerHTML =
    `<b>${esc(g.route || "Route wählen")}</b> `+
    `<i>${esc(g.from)} bis ${esc(g.to)} · ${parts.map(esc).join(" · ")}</i>`;
}
function applyNaturalSearch(data){
  trip = data.trip || ((data.hops || []).length > 2 ? "multi" : "one_way");
  hops = (data.hops || []).map(h => ({code:h.code || "", label:h.label || h.code || ""}));
  while (hops.length < TRIPS.find(t=>t.id===trip).min) hops.push({code:"",label:""});
  $("#aiStatus").dataset.tone = "";
  $("#aiStatus").textContent = (data.warnings || []).length
    ? `Route übernommen. Nicht erkannt: ${data.warnings.join(", ")}`
    : "Route übernommen.";
  drawTrips(); drawRoute(); syncStayControls(); size(); updateRouteGlance(); saveForm();
}
async function parseNaturalSearch(){
  const text = $("#aiText").value.trim();
  if (text.length < 3){
    $("#aiStatus").dataset.tone = "err";
    $("#aiStatus").textContent = "Bitte eine Route eingeben.";
    return;
  }
  $("#aiApply").disabled = true;
  $("#aiStatus").dataset.tone = "";
  $("#aiStatus").textContent = "Route wird gelesen.";
  try {
    const r = await fetch("/api/parse-search", {
      method:"POST",
      headers:{"content-type":"application/json"},
      body:JSON.stringify({text}),
    });
    const d = await r.json();
    if (!r.ok) throw Error(detail(d.detail) || "Route nicht erkannt.");
    applyNaturalSearch(d);
  } catch(err) {
    $("#aiStatus").dataset.tone = "err";
    $("#aiStatus").textContent = err.message;
  } finally {
    $("#aiApply").disabled = false;
  }
}

/* ---------------- voice input ---------------- */
let voiceRecognition = null, voiceListening = false;
function voiceRecognitionCtor(){
  const w = typeof window === "undefined" ? {} : window;
  return w.SpeechRecognition || w.webkitSpeechRecognition || null;
}
function setVoiceStatus(text, kind=""){
  const status = $("#voiceStatus");
  if (!status) return;
  status.dataset.tone = kind === "err" ? "err" : "";
  status.textContent = text;
}
function setVoiceListening(on){
  voiceListening = on;
  const button = $("#voiceInput");
  if (!button) return;
  button.classList.toggle("on", on);
  button.setAttribute("aria-pressed", on ? "true" : "false");
  button.setAttribute("aria-label", on ? "Spracheingabe stoppen" : "Route per Sprache eingeben");
}
function spokenRouteText(text){
  return String(text || "").trim().replace(/\s+/g, " ");
}
function insertSpokenRoute(text){
  const clean = spokenRouteText(text);
  if (!clean) return "";
  const field = $("#aiText");
  const current = field.value.trim();
  field.value = current ? `${current} ${clean}` : clean;
  if (typeof field.focus === "function") field.focus();
  return clean;
}
function voiceErrorMessage(code){
  if (code === "not-allowed" || code === "service-not-allowed") return "Mikrofonzugriff wurde nicht erlaubt.";
  if (code === "no-speech") return "Keine Sprache erkannt.";
  if (code === "audio-capture") return "Kein Mikrofon gefunden.";
  if (code === "network") return "Spracherkennung ist gerade nicht erreichbar.";
  return "Spracheingabe konnte nicht gestartet werden.";
}
function setupVoiceInput(){
  const button = $("#voiceInput");
  if (!button) return;
  const Recognition = voiceRecognitionCtor();
  if (!Recognition){
    button.disabled = true;
    button.title = "Spracheingabe ist in diesem Browser nicht verfügbar.";
    setVoiceStatus("Spracheingabe funktioniert in Chrome oder Edge per HTTPS.");
    return;
  }
  button.disabled = false;
  button.onclick = () => {
    if (voiceListening && voiceRecognition){
      voiceRecognition.stop();
      return;
    }
    const recognition = new Recognition();
    voiceRecognition = recognition;
    recognition.lang = "de-DE";
    recognition.continuous = false;
    recognition.interimResults = true;
    recognition.onstart = () => {
      setVoiceListening(true);
      setVoiceStatus("Höre zu...");
    };
    recognition.onresult = event => {
      let finalText = "", interimText = "";
      for (let i = event.resultIndex; i < event.results.length; i++){
        const part = event.results[i][0].transcript;
        if (event.results[i].isFinal) finalText += part;
        else interimText += part;
      }
      if (interimText) setVoiceStatus(`Erkannt: ${spokenRouteText(interimText)}`);
      if (finalText){
        insertSpokenRoute(finalText);
        setVoiceStatus("Sprache übernommen. Route prüfen und übernehmen.");
      }
    };
    recognition.onerror = event => setVoiceStatus(voiceErrorMessage(event.error), "err");
    recognition.onend = () => setVoiceListening(false);
    try {
      recognition.start();
    } catch {
      setVoiceListening(false);
      setVoiceStatus("Spracheingabe läuft bereits.", "err");
    }
  };
}

/* ---------------- stay ranges ---------------- */
function stayCount(){
  if (trip === "one_way") return 0;
  if (trip === "return") return 1;
  return Math.max(0, active().length - 2);
}
function stayInputId(i, kind){ return `stay-${kind}-${i}`; }
function stayLabel(i){
  const list = active();
  const hop = list[i + 1] || {};
  if (trip === "return") return hop.label || hop.code || "Ziel";
  return hop.label || hop.code || `Stopp ${i + 1}`;
}
/* Lesen darf nicht schreiben. Wer "12" tippt, hat nach der ersten Ziffer eine
   1 im Feld; ein sofort korrigiertes Maximum frisst dann die zweite Ziffer. */
function readStayControls(){
  const count = stayCount();
  for (let i = 0; i < count; i++){
    const min = document.querySelector(`#${stayInputId(i, "min")}`);
    const max = document.querySelector(`#${stayInputId(i, "max")}`);
    if (!min || !max) continue;
    const range = clampStay([min.value, max.value]);
    stayRanges[i] = range;
    if (max.min !== String(range[0])) max.min = String(range[0]);
  }
}
/* Erst wenn das Feld verlassen wird, rueckt der sichtbare Wert nach. */
function commitStayControls(){
  readStayControls();
  const count = stayCount();
  for (let i = 0; i < count; i++){
    const min = document.querySelector(`#${stayInputId(i, "min")}`);
    const max = document.querySelector(`#${stayInputId(i, "max")}`);
    const range = stayRanges[i];
    if (!range) continue;
    // Beide Felder werden geklemmt, also muessen auch beide nachziehen.
    if (min && String(min.value) !== String(range[0])) min.value = String(range[0]);
    if (max && String(max.value) !== String(range[1])) max.value = String(range[1]);
  }
}
function syncStayControls(){
  readStayControls();
  const count = stayCount();
  while (stayRanges.length < count) {
    const last = stayRanges[stayRanges.length - 1] || [3, 10];
    stayRanges.push([last[0], last[1]]);
  }
  const enabled = count > 0;
  $("#staylist").innerHTML = Array.from({length: count}, (_, i) => {
    const [min, max] = stayRanges[i] || [3, 10];
    const minId = stayInputId(i, "min"), maxId = stayInputId(i, "max");
    return `<div class="stayitem">
      <span class="stayname">${esc(stayLabel(i))}</span>
      <div><label class="lab" for="${minId}">min</label>
        <input type="number" id="${minId}" value="${esc(min)}" min="0" max="60"></div>
      <div><label class="lab" for="${maxId}">max</label>
        <input type="number" id="${maxId}" value="${esc(max)}" min="0" max="60"></div>
    </div>`;
  }).join("");
  $("#staylist").querySelectorAll("input").forEach(input => {
    input.oninput = () => { readStayControls(); size(); saveForm(); };
    input.onchange = () => { commitStayControls(); size(); saveForm(); };
    input.onblur = () => { commitStayControls(); size(); saveForm(); };
  });
  setStayEnabled(enabled);
}

/* ---------------- route with type-ahead ---------------- */
/* ---------------- Combobox ---------------- */
/* Ein Textfeld mit Vorschlagsliste ist kein Textfeld. Ohne diese Rollen weiss
   ein Screenreader nicht, dass unter dem Feld eine Liste aufgeht. */
function comboboxAttributes(i){
  const listboxId = `ac-${i}`;
  return {
    role: "combobox",
    listboxId,
    optionId: n => `${listboxId}-opt-${n}`,
  };
}
function activeDescendant(i, sel){
  return sel >= 0 ? comboboxAttributes(i).optionId(sel) : "";
}
/* Eine Gruppe muss sagen, welche Flughaefen sie enthaelt, sonst waehlt sie niemand.
   /api/airports liefert fuer Gruppen kind:"group" und airports:[...]. */
function groupLabel(entry){
  const a = entry || {};
  const city = a.city || a.name || a.code || "";
  if (a.kind === "group" && Array.isArray(a.airports) && a.airports.length){
    /* "Ostflughaefen" allein sagt niemandem, welche Staedte gemeint sind. */
    const head = a.name && a.name !== city ? `${city}: ${a.name}` : city;
    return `${head} (${a.airports.join(", ")})`;
  }
  return a.name && a.name !== city ? `${city} · ${a.name}` : city;
}
/* Nach einer Suche springt der Fokus auf die Ergebnisueberschrift, damit der
   naechste Tab in den Ergebnissen landet und nicht wieder im Formular.
   Wer waehrenddessen weitertippt, verloere dabei aber seinen Cursor. Also nur
   springen, wenn gerade niemand in einem Feld steht. */
function focusIsIdle(){
  const here = document.activeElement;
  // #cancel wird nach dem Lauf versteckt; wer dort steht, verliert den Fokus ohnehin.
  return !here || here === document.body || here === $("#go") || here === $("#cancel");
}
function focusResults(){
  const heading = $("#outtitle");
  if (!heading || typeof heading.focus !== "function") return false;
  if (!focusIsIdle()) return false;
  heading.focus();
  return true;
}
function drawRoute(){  const box = $("#route"); box.innerHTML = "";
  const cfg = TRIPS.find(t=>t.id===trip);
  const list = active();
  list.forEach((hop,i) => {
    // Der Trenner ist reine Dekoration und steht als CSS-Pseudoelement im Papier,
    // damit kein Screenreader ihn vorliest und kein Pfeil in der Copy landet.
    if (i){ const s=document.createElement("span"); s.className="sep";
            s.setAttribute("aria-hidden","true"); box.append(s); }
    const combo = comboboxAttributes(i);
    const cell = document.createElement("span");
    cell.className = "hop" + (trip==="multi" && list.length > cfg.min ? "" : " nokill");
    const inp = document.createElement("input");
    inp.type="text"; inp.autocomplete="off"; inp.spellcheck=false;
    inp.value = hop.label || hop.code || "";
    inp.placeholder = i===0 ? "Von, z.B. Berlin" : "Nach, z.B. Athen";
    inp.setAttribute("aria-label", i===0 ? "Startflughafen" : `Ziel ${i}`);
    inp.setAttribute("role", combo.role);
    inp.setAttribute("aria-autocomplete", "list");
    inp.setAttribute("aria-expanded", "false");
    inp.setAttribute("aria-controls", combo.listboxId);
    cell.draggable = true; cell.dataset.i = i;
    const badge = document.createElement("span"); badge.className="code";
    badge.textContent = hop.code || "";
    const menu = document.createElement("div"); menu.className="ac";
    menu.id = combo.listboxId;
    menu.setAttribute("role","listbox");
    menu.setAttribute("aria-label", "Flughafenvorschläge");
    /* Ein <p> ist kein erlaubtes Kind von role="listbox". Die Fehlanzeige
       bekommt deshalb ein eigenes Kaestchen neben der Liste. */
    const empty = document.createElement("div"); empty.className="ac";
    empty.id = `${combo.listboxId}-empty`;
    empty.setAttribute("role","status");
    empty.innerHTML = `<p class="none">Kein Flughafen gefunden</p>`;
    const ghost = document.createElement("span"); ghost.className="ghost";
    const grip = document.createElement("span"); grip.className="grip";
    grip.setAttribute("aria-hidden","true");
    cell.append(grip, inp, ghost, badge, menu, empty);

    let items=[], sel=-1, timer;
    const close = () => { menu.classList.remove("on"); empty.classList.remove("on");
                          menu.innerHTML=""; items=[]; sel=-1;
                          ghost.innerHTML="";
                          inp.setAttribute("aria-expanded","false");
                          inp.setAttribute("aria-describedby","");
                          inp.setAttribute("aria-activedescendant",""); };
    /* Show the rest of the top suggestion in grey behind what was typed, so
       the Tab key has something visible to accept. */
    const paintGhost = () => {
      const typed = inp.value, top = items[Math.max(sel,0)];
      if (!top || !typed) { ghost.innerHTML=""; return; }
      const city = top.city;
      if (city.toLowerCase().startsWith(typed.toLowerCase()) && city.length > typed.length){
        ghost.innerHTML = `<i>${esc(typed)}</i>${esc(city.slice(typed.length))}`;
      } else { ghost.innerHTML=""; }
    };
    const paint = () => {
      const found = items.length > 0;
      menu.innerHTML = found
        ? items.map((a,n)=>`<b role="option" id="${combo.optionId(n)}" data-n="${n}"
             aria-selected="${n===sel}">
             <span class="c">${esc(a.code)}</span>
             <span class="n">${esc(groupLabel(a))}</span>
             <span class="k">${esc(a.country)}</span></b>`).join("")
        : "";
      if (found){ menu.classList.add("on"); empty.classList.remove("on"); }
      else { menu.classList.remove("on"); empty.classList.add("on"); }
      /* Der Leerkasten haengt nur am Feld, solange er auch sichtbar ist. */
      inp.setAttribute("aria-describedby", found ? "" : empty.id);
      paintGhost();
      inp.setAttribute("aria-expanded", String(found));
      inp.setAttribute("aria-activedescendant", activeDescendant(i, sel));
      menu.querySelectorAll("b").forEach(el => el.onmousedown = ev => {
        ev.preventDefault(); choose(items[+el.dataset.n]);
      });
    };
    const choose = a => {
      hops[i] = {code:a.code, label:a.city};
      inp.value = a.city; badge.textContent = a.code; close(); size(); updateRouteGlance(); saveForm();
      const all = $("#route").querySelectorAll("input");
      if (all[i+1] && !(hops[i+1]||{}).code) all[i+1].focus();
    };
    inp.oninput = () => {
      hops[i] = {code:"", label:inp.value}; badge.textContent=""; size(); updateRouteGlance();
      clearTimeout(timer);
      const q = inp.value.trim();
      if (q.length < 2){ close(); return; }
      timer = setTimeout(async () => {
        try {
          const r = await fetch(`/api/airports?q=${encodeURIComponent(q)}&limit=8`);
          items = (await r.json()).results || []; sel = items.length?0:-1; paint();
        } catch { close(); }
      }, 140);
    };
    inp.onkeydown = e => {
      // Escape zuerst: ohne Treffer steht nur der Leerkasten offen, und der
      // muss sich genauso schliessen lassen wie die Liste.
      if (e.key==="Escape"){ close(); return; }
      if (!menu.classList.contains("on")) return;
      if (e.key==="ArrowDown"){ e.preventDefault(); sel=Math.min(sel+1,items.length-1); paint(); }
      else if (e.key==="ArrowUp"){ e.preventDefault(); sel=Math.max(sel-1,0); paint(); }
      else if (e.key==="Enter" && sel>=0){ e.preventDefault(); choose(items[sel]); }
      else if (e.key==="Tab" && sel>=0 && !e.shiftKey){
        // Tab takes the highlighted airport and moves on, so a whole route can
        // be typed without ever reaching for the mouse.
        e.preventDefault(); choose(items[sel]);
      }
    };
    inp.onblur = () => setTimeout(async () => {
      close();
      // Leaving a field with text but no airport picked would dead-end the
      // form, so resolve it to the best match instead of blaming the person.
      const typed = inp.value.trim();
      if (!typed || hops[i].code) return;
      try {
        const r = await fetch(`/api/airports?q=${encodeURIComponent(typed)}&limit=1`);
        const top = ((await r.json()).results || [])[0];
        if (top){ hops[i] = {code:top.code, label:top.city};
                  inp.value = top.city; badge.textContent = top.code; size(); updateRouteGlance(); }
      } catch { /* leave it as typed; the form will say what is missing */ }
    }, 140);

    if (trip==="multi" && list.length > cfg.min){
      const x = document.createElement("button");
      x.type="button"; x.className="kill"; x.textContent="×";
      x.setAttribute("aria-label", `Stopp ${hop.label || hop.code || i + 1} entfernen`);
      x.setAttribute("title", "Stopp entfernen");
      x.onclick = () => { hops.splice(i,1); drawRoute(); size(); updateRouteGlance(); };
      cell.append(x);
    }
    box.append(cell);
  });

  if (trip==="multi" && list.length < 6){
    const add = document.createElement("button");
    add.type="button"; add.className="addhop"; add.textContent="+ Stopp";
    add.onclick = () => { hops.splice(hops.length-1,0,{code:"",label:""}); drawRoute();
      updateRouteGlance(); $("#route").querySelectorAll("input")[hops.length-2].focus(); };
    box.append(add);
  }
  wireDrag(box);
  syncStayControls();
}

/* Reorder stops by dragging a field onto another one. */
function wireDrag(box){
  let from = null;
  box.querySelectorAll(".hop").forEach(cell => {
    cell.ondragstart = e => {
      from = +cell.dataset.i;
      cell.classList.add("drag");
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", String(from));
    };
    cell.ondragend = () => {
      cell.classList.remove("drag");
      box.querySelectorAll(".hop").forEach(c=>c.classList.remove("over"));
    };
    cell.ondragover = e => {
      if (from === null) return;
      e.preventDefault(); e.dataTransfer.dropEffect = "move";
      if (+cell.dataset.i !== from) cell.classList.add("over");
    };
    cell.ondragleave = () => cell.classList.remove("over");
    cell.ondrop = e => {
      e.preventDefault();
      const to = +cell.dataset.i;
      if (from === null || to === from) return;
      const [moved] = hops.splice(from,1);
      hops.splice(to,0,moved);
      from = null; drawRoute(); size(); updateRouteGlance(); saveForm();
    };
  });
}

/* ---------------- airlines ---------------- */
async function loadAirlines(){
  try { AIRLINES = (await (await fetch("/api/airlines")).json()).airlines || []; }
  catch { AIRLINES = []; }
  const box = $("#chips");
  box.innerHTML = AIRLINES.filter(a => a.kind !== "comparison").map(a => `
    <button type="button" class="chip" data-code="${esc(a.code)}" aria-pressed="false"
      ${a.status==="live"?"":"disabled"} title="${esc(a.note)}">
      <i class="tail" style="background:${esc(a.color)};color:${badgeTextColor(a.color)}">${esc(a.code)}</i>
      <span>${esc(a.name)}</span>
      ${a.status==="live" ? "" : `<span class="st">${a.status==="planned"?"geplant":"blockiert"}</span>`}
    </button>`).join("");
  box.querySelectorAll(".chip:not(:disabled)").forEach(c => c.onclick = () => {
    const on = c.getAttribute("aria-pressed")==="true";
    c.setAttribute("aria-pressed", String(!on));
    if (on) picked.delete(c.dataset.code); else picked.add(c.dataset.code);
    airHint(); updateRouteGlance(); saveForm();
  });
  airHint();
}
/* Die Kopfzeile des Aufklappbereichs sagt, wie viele Airlines wirklich abgefragt
   werden. Vergleichsportale sind keine Airline und zaehlen nicht mit. */
function airlinesSummary(){
  const live = AIRLINES.filter(a => a.status === "live" && a.kind !== "comparison").length;
  return picked.size ? `Eingegrenzt auf ${[...picked].join(", ")}` : `Alle ${live} Airlines`;
}
function airHint(){
  const live = AIRLINES.filter(a=>a.status==="live").map(a=>a.name);
  const plan = AIRLINES.filter(a=>a.status==="planned").map(a=>a.name);
  const sel = picked.size ? `Eingeschränkt auf ${[...picked].join(", ")}.`
                          : "Es werden alle verfügbaren Airlines abgefragt.";
  $("#airhint").textContent =
    `${sel} Preise liefert derzeit ${live.join(", ")}. `
  + `Als nächstes anschließbar: ${plan.join(", ")}. `
  + `Ausgegraute Airlines lassen sich technisch nicht abfragen.`;
  $("#airlinescount").textContent = airlinesSummary();
}

/* ---------------- sizing ---------------- */
/* ---------------- Eingabeschutz ---------------- */
/* `toISOString` rechnet in UTC. Wer abends in Berlin sucht, bekaeme damit den
   Vortag als frueheste Auswahl. Also das lokale Datum zusammensetzen. */
function isoDay(date){
  const pad = n => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}
function todayIso(){ return isoDay(new Date()); }

/* Ein Fenster, das rueckwaerts laeuft, hat keine Kombination. Das gehoert vor
   den Server gesagt, nicht danach. */
function checkWindow(){
  const msg = $("#windowmsg");
  const from = $("#from").value, to = $("#to").value;
  if (from && to && to < from){
    msg.dataset.tone = "err";
    msg.textContent = "Das Ende des Fensters liegt vor dem Anfang.";
    return false;
  }
  msg.dataset.tone = "";
  msg.textContent = "";
  return true;
}

/* Ein Maximum unter dem Minimum ergibt keine Nacht. Das Maximum zieht mit. */
function clampStay(range){
  const min = Math.max(0, Math.min(60, Number((range || [])[0]) || 0));
  const max = Math.max(min, Math.min(60, Number((range || [])[1]) || 0));
  return [min, max];
}

/* Eine Reise ohne Aufenthalt hat keine Aufenthaltsfelder: `syncStayControls`
   leert die Liste. Es gibt also nichts abzuschalten, nur den Block zu daempfen. */
function setStayEnabled(on){
  $("#stayrow").classList.toggle("off", !on);
}

/* Belegung und Zimmer sagen nur etwas, wenn ueberhaupt Hotels gefragt werden. */
function setStayOptionsEnabled(on){
  $("#hotelparty").classList.toggle("off", !on);
  $("#hotelAdults").disabled = !on;
  $("#hotelRooms").disabled = !on;
}

function payload(){  readStayControls();
  const count = stayCount();
  return {
    airports: hops.map(h=>h.code), trip,
    window_start: $("#from").value, window_end: $("#to").value,
    stays: Array.from({length: count}, (_, i) => stayRanges[i] || [3, 10]),
    checked_bags: Number($("#checkedBags").value || 0),
    max_stops: $("#maxStops").value === "" ? null : Number($("#maxStops").value),
    with_hotels: $("#withHotels").checked === true,
    hotel_adults: Number($("#hotelAdults").value || 2),
    hotel_rooms: Number($("#hotelRooms").value || 1),
    airlines: [...picked], adults:1, cabin:"economy", currency:"EUR"
  };
}
function profilePayload(){
  const p = payload();
  return Object.assign({}, p, {
    name: ($("#profileName").value || `${p.airports.join("-")} täglich`).trim(),
    cadence_days: 1,
  });
}
let sizeTimer;
function validPlaceCode(code){ return /^[A-Z0-9-]{2,12}$/.test(code || ""); }
function estimateWarning(d, tripId){
  const combinations = Number(d.combinations) || 0;
  const cells = Number(d.cells) || 0;
  if (tripId !== "one_way" && combinations >= SEARCH_WARNINGS.hugeCombinations) {
    return {warn:true, suffix: ". Sehr großer Suchraum, die Suche kann deutlich länger dauern."};
  }
  if (tripId !== "one_way" && combinations >= SEARCH_WARNINGS.largeCombinations) {
    return {warn:true, suffix: ". Großer Suchraum, bitte etwas Wartezeit einplanen."};
  }
  if (cells >= SEARCH_WARNINGS.manyCells) {
    return {warn:true, suffix: ". Viele Teilstrecken-Tage, der Abruf kann länger laufen."};
  }
  return {warn:false, suffix:""};
}
function estimateSummary(d, tripId){
  const variants = d.variants > 1 ? ` über <b>${d.variants}</b> Routenvarianten` : "";
  const warning = estimateWarning(d, tripId);
  const main = tripId==="one_way"
    ? `<b>${d.cells}</b> Abflugtage werden bepreist${variants}`
    : `<b>${d.combinations.toLocaleString("de-DE")}</b> Kombinationen aus <b>${d.cells}</b> Leg-Tagen${variants}`;
  return {html: main + warning.suffix, warn: warning.warn};
}
function size(){
  clearTimeout(sizeTimer);
  sizeTimer = setTimeout(async () => {
    const p = payload(), el = $("#sizing");
    el.dataset.tone = "";
    if (p.airports.some(a=>!validPlaceCode(a)) || !p.window_start || !p.window_end){
      el.textContent = "Flughäfen aus der Vorschlagsliste wählen"; return;
    }
    try {
      const r = await fetch("/api/estimate",{method:"POST",
        headers:{"content-type":"application/json"}, body:JSON.stringify(p)});
      const d = await r.json();
      if (!r.ok){ el.textContent = detail(d.detail)||"Eingabe prüfen"; el.dataset.tone="warn"; return; }
      if (!d.combinations){ el.textContent="Keine Kombination passt in dieses Fenster";
        el.dataset.tone="warn"; return; }
      // A one-way search has no combinations to weigh up, only days to price.
      const summary = estimateSummary(d, trip);
      el.innerHTML = summary.html;
      el.dataset.tone = summary.warn ? "warn" : "";
  } catch { el.textContent=""; }
  }, 200);
}

async function saveProfileNow(){
  const msg = $("#savedmsg");
  msg.dataset.tone = "";
  msg.textContent = "Speichere Profil";
  try {
    const r = await fetch("/api/profiles",{method:"POST",
      headers:{"content-type":"application/json"}, body:JSON.stringify(profilePayload())});
    const d = await r.json();
    if (!r.ok) throw new Error(detail(d.detail) || "Profil konnte nicht gespeichert werden");
    msg.dataset.tone = "ok";
    msg.textContent = `${d.name} gespeichert`;
    await loadDeals();
  } catch (err) {
    msg.dataset.tone = "err";
    msg.textContent = err.message || "Profil konnte nicht gespeichert werden";
  }
}

/* Enter im Namensfeld soll das Profil sichern, nicht die Suche starten. Das
   Feld steht heute noch in <form id="f">, deshalb haelt preventDefault das
   Absenden auf. */
async function profileNameKeydown(e){
  if (e.key !== "Enter") return;
  e.preventDefault();
  await saveProfileNow();
}

function scannerSummary(state){
  const running = state.scanner && state.scanner.running;
  const profiles = (state.due && state.due.profiles) || [];
  const head = running ? "Scanner läuft." : "Scanner bereit.";
  if (!profiles.length) return `${head} Keine fälligen Profile.`;
  return `${head} Fällig: ${profiles.map(p=>p.name).join(", ")}`;
}
async function loadScanner(){
  try {
    const [scanner, due] = await Promise.all([
      fetch("/api/scanner").then(r=>r.json()),
      fetch("/api/profiles/due").then(r=>r.json()),
    ]);
    $("#scanstate").textContent = scannerSummary({scanner, due});
  } catch {
    $("#scanstate").textContent = "Scannerstatus nicht verfügbar.";
  }
}
$("#runScanner").onclick = async () => {
  $("#scanstate").textContent = "Starte fällige Scans";
  try {
    const d = await fetch("/api/scanner/run-once",{method:"POST"}).then(r=>r.json());
    const names = (d.jobs || []).map(j=>j.name);
    $("#scanstate").textContent = names.length
      ? `Gestartet: ${names.join(", ")}`
      : "Keine fälligen Profile.";
    await loadDeals();
  } catch {
    $("#scanstate").textContent = "Scans konnten nicht gestartet werden.";
  }
};

/* ---------------- results ---------------- */
const pad = 8;
function pos(ds){
  const d = new Date(ds+"T00:00:00"), span=(axis.end-axis.start)||1;
  return ((d-axis.start)/span)*100;
}
/* Die Schiene ist prozentual, die Punkte sind es nicht: sie brauchen an beiden
   Enden Platz fuer ihren eigenen Radius. */
function railLeft(percent){
  return `calc(${pad}px + (100% - ${pad * 2}px) * ${percent / 100})`;
}
function railWidth(percent){
  return `calc((100% - ${pad * 2}px) * ${percent / 100})`;
}
/* Ein Leg wird nicht eingefaerbt, sondern unterschiedlich stark getuscht.
   Anteil an der Summe mal Anzahl Legs: 1.0 ist der Durchschnitt und ergibt 0.64,
   halber Durchschnitt oder weniger bleibt bei 0.28, ab dem Anderthalbfachen des
   Durchschnitts ist es volle Tinte. */
function inkDensity(price, total, legs){
  const share = (total > 0 && legs > 0) ? ((Number(price) || 0) / total) * legs : 1;
  const t = Math.max(0, Math.min(1, share - 0.5));
  return Math.round((0.28 + 0.72 * t) * 100) / 100;
}
function inkShade(price, total, legs){
  const pct = Math.round(inkDensity(price, total, legs) * 100);
  return `color-mix(in oklab, var(--ink) ${pct}%, var(--bg))`;
}
function fmtDay(s){
  return new Date(s+"T00:00:00").toLocaleDateString("de-DE",
    {weekday:"short",day:"2-digit",month:"short"});
}

/* ---------------- Beschriftungen ---------------- */
function stopsLabel(n){
  if (n === null || n === undefined) return "";
  const v = Number(n);
  if (Number.isNaN(v)) return "";
  if (v <= 0) return "Direktflug";
  return v === 1 ? "1 Umstieg" : `${v} Umstiege`;
}

/* Ein Nachtflug landet am naechsten Tag. Ohne diesen Hinweis liest sich eine
   Ankunft um 06:15 wie ein Vormittagsflug. */
function arrivalLabel(dep, arr){
  if (!arr || !dep || arr === dep) return "";
  const d = new Date(String(arr) + "T00:00:00");
  if (Number.isNaN(d.getTime())) return "";
  return `an ${String(d.getDate()).padStart(2,"0")}.${String(d.getMonth()+1).padStart(2,"0")}.`;
}

/* Yen und Won haben keine Nachkommastellen. Ein Yen-Betrag mit zwei Stellen
   waere schlicht falsch. Der Betrag kommt in ganzen Einheiten. */
const MINOR_DIGITS = {JPY:0, KRW:0, VND:0, ISK:0, CLP:0, HUF:0};
function nativePriceLabel(offer){
  const native = offer && offer.price_native;
  if (!native || typeof native.amount !== "number" || !native.currency) return "";
  const digits = MINOR_DIGITS[native.currency] === undefined ? 2 : MINOR_DIGITS[native.currency];
  const shown = Number(native.amount).toLocaleString("de-DE", {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });
  return `umgerechnet aus ${shown} ${native.currency}`;
}

function nightsOf(o){
  const dates = (o && o.dates) || [];
  if (dates.length < 2) return 0;
  const first = new Date(dates[0] + "T00:00:00");
  const last = new Date(dates[dates.length - 1] + "T00:00:00");
  return Math.round((last - first) / 864e5);
}

/* Nur behaupten, was jede Teilstrecke gemeldet hat. Eine Quelle ohne Umstiegszahl
   macht die Summe unbrauchbar, dann bleibt die Spalte leer. */
function stopsOf(o){
  const legs = (o && o.legs) || [];
  if (!legs.length) return null;
  if (legs.some(l => l.stops === undefined || l.stops === null)) return null;
  return legs.reduce((sum, l) => sum + (Number(l.stops) || 0), 0);
}

function statusLabel(o){
  const quality = resultQuality(o);
  if (quality === "verified") return "geprüft";
  return quality === "indicative" ? "Richtwert" : "Schätzung";
}

/* ---------------- Preislage ---------------- */
/* Status und Preislage sind zwei verschiedene Aussagen. Der Status sagt, wie
   sicher ein Preis ist; die Preislage sagt, ob er fuer diese Strecke und diese
   Saison gut ist. Beides steht nebeneinander, keines ersetzt das andere.
   Ohne Baseline wird nichts behauptet: dann steht dort "keine Basis". */
const BAND_LABELS = {cheap:"günstig", normal:"normal", expensive:"teuer"};
function bandLabel(tier){
  return BAND_LABELS[String(tier || "")] || "keine Basis";
}
function bandOf(leg){
  return (leg && leg.band) || {};
}
/* Worauf die Stufe steht. Geschaetzte und geprüfte Preise sind zwei getrennte
   Grundgesamtheiten, und fünf Vergleichspreise sind etwas anderes als zwanzig.
   Beides gehört sichtbar dazu, sonst wirkt jede Stufe gleich belastbar. */
const BAND_POPULATIONS = {estimate:"Schätzpreisen", verified:"geprüften Preisen"};
function bandBasis(band){
  const n = Number((band || {}).n);
  if (!Number.isFinite(n) || n <= 0) return "";
  const kind = BAND_POPULATIONS[String((band || {}).population || "")] || "Preisen";
  return `aus ${n} ${kind}${band.thin ? ", dünne Basis" : ""}`;
}
/* Eine Baseline gibt es je Teilstrecke, nicht fuer die ganze Route. In der
   Zeile steht deshalb die Teilstrecke mit der groessten Abweichung: sie sagt
   am meisten. Worauf sie sich bezieht, steht im Titel. */
function rowBand(o){
  let best = null;
  ((o && o.legs) || []).forEach(leg => {
    const band = bandOf(leg);
    if (!band.tier || band.tier === "unknown") return;
    const off = Math.abs(Number(band.deviation_pct) || 0);
    if (!best || off > best.off) best = {off, band, leg};
  });
  if (!best) return {tier:"unknown", band:{}, leg:null};
  return {tier: best.band.tier, band: best.band, leg: best.leg};
}
function bandTitle(o){
  const pick = rowBand(o);
  if (!pick.leg) return "Für keine Teilstrecke gibt es genug Preishistorie.";
  const where = `${pick.leg.origin}-${pick.leg.destination} am ${fmtDay(pick.leg.date)}`;
  const off = deviationLabel(pick.band.deviation_pct);
  const usual = (pick.band.median === null || pick.band.median === undefined)
    ? "" : ` gegenüber üblichen ${money(pick.band.median)} €`;
  const basis = bandBasis(pick.band);
  const head = off ? `${where}: ${off}${usual}` : `${where}: ${bandLabel(pick.tier)}`;
  return basis ? `${head} (${basis})` : head;
}
/* In der Detailzeile steht die Preislage je Leg, denn dort gilt sie. */
function legBandNote(leg){
  const band = bandOf(leg);
  if (!band.tier || band.tier === "unknown") return "Preislage: keine Basis";
  const off = deviationLabel(band.deviation_pct);
  const head = off ? `Preislage: ${bandLabel(band.tier)}, ${off}`
                   : `Preislage: ${bandLabel(band.tier)}`;
  const basis = bandBasis(band);
  return basis ? `${head} (${basis})` : head;
}
function legBandMarkup(leg){
  const band = bandOf(leg);
  const tier = band.tier || "unknown";
  return `<i class="band" data-signal="${esc(tier)}">${esc(legBandNote(leg))}</i>`;
}

/* ---------------- Gesamtreise ---------------- */
/* Flug plus Übernachtung. Das ist die eine Zahl, die kein Portal nennt, und
   deshalb darf sie nie geraten sein: fehlt fuer einen Aufenthalt ein Preis,
   bleibt die Spalte ohne Summe, statt eine Luecke wie einen Rabatt aussehen
   zu lassen. */
function nightsLabel(n){
  const v = Number(n) || 0;
  return v === 1 ? "1 Nacht" : `${v} Nächte`;
}
function hasStayCosts(o){
  return Boolean(o) && Array.isArray(o.stays);
}
function grandTotal(o){
  const v = o && o.grand_total;
  return (v === null || v === undefined) ? null : Number(v);
}
function grandCell(o){
  // Ohne Schalter gibt es diese Angabe nicht. Dann steht dort auch nichts.
  if (!hasStayCosts(o)) return "";
  const total = grandTotal(o);
  if (total === null){
    return `<span class="nogrand" title="Für mindestens einen Aufenthalt liegt`
      + ` kein Übernachtungspreis vor. Ohne ihn gibt es keine ehrliche Summe.`
      + `">offen</span>`;
  }
  return `${money(total)}<small>€</small>`;
}
function stayRowsMarkup(o){
  const stays = (o && o.stays) || [];
  if (!stays.length) return "";
  return stays.map(s => {
    const fare = (s.price === null || s.price === undefined)
      ? `<span class="miss">kein Preis</span>`
      : `ab ${money(s.price)} €`;
    const where = s.name ? `${s.city}, ${s.name}` : s.city;
    const src = s.source ? ` <i>${esc(s.source)}</i>` : "";
    return `<div class="fl stay">
      <span class="staymark" aria-hidden="true"></span>
      <span class="pair">${esc(s.code)}</span>
      <span class="when">${fmtDay(s.arrival)}</span>
      <span class="times">${esc(where)} <i>${esc(nightsLabel(s.nights))}</i>${src}</span>
      <span class="fare">${fare}</span><span></span></div>`;
  }).join("");
}
function staySummaryLine(o){
  if (!hasStayCosts(o)) return "";
  const total = grandTotal(o);
  if (total === null){
    return "Für mindestens einen Aufenthalt gibt es keinen Preis, deshalb bleibt"
      + " die Gesamtsumme offen.";
  }
  return `Gesamt ab <b>${money(total)} €</b> für Flug und Übernachtung.`;
}

/* ---------------- Zwischenstaende ---------------- */
/* Route plus Datumskette ist derselbe Schluessel, mit dem der Server die
   Routenvarianten zusammenfuehrt. Rang taugt nicht: er verschiebt sich, sobald
   ein geprueftes Ergebnis billiger wird. */
function resultKey(row){
  return `${(row && row.route) || ""}|${((row && row.dates) || []).join(",")}`;
}
function rerank(rows){
  const out = rows.slice().sort((a, b) => (a.total || 0) - (b.total || 0)).slice(0, 20);
  out.forEach((row, i) => { row.rank = i + 1; });
  return out;
}
function applyPartial(results){
  const byKey = new Map(lastResults.map(r => [resultKey(r), r]));
  (results || []).forEach(row => {
    const key = resultKey(row);
    const current = byKey.get(key);
    // Ein geprueftes Ergebnis wird von einer nachlaufenden Schaetzung nicht zurueckgesetzt.
    if (current && current.verified) return;
    byKey.set(key, Object.assign({}, current || {}, row));
  });
  renderTable(rerank([...byKey.values()]));
  return lastResults;
}
function applyVerified(row){
  if (!row || !row.dates) return lastResults;
  const byKey = new Map(lastResults.map(r => [resultKey(r), r]));
  const key = resultKey(row);
  byKey.set(key, Object.assign({}, byKey.get(key) || {}, row));
  renderTable(rerank([...byKey.values()]));
  return lastResults;
}
/* Die Hotelkosten kommen nach den Flugpreisen und ergaenzen die Zeilen an Ort
   und Stelle, genau wie ein geprueftes Ergebnis es tut. */
function applyStays(results){
  const byKey = new Map(lastResults.map(r => [resultKey(r), r]));
  (results || []).forEach(row => {
    const key = resultKey(row);
    byKey.set(key, Object.assign({}, byKey.get(key) || {}, row));
  });
  renderTable(rerank([...byKey.values()]));
  return lastResults;
}
function carriersOf(o){
  const out=[];
  o.legs.forEach(l => (l.carriers||[]).forEach(c => { if(!out.includes(c)) out.push(c); }));
  return out;
}
/* WCAG 2.1 relative Luminanz. Die Markenfarben kommen aus der Registry und
   reichen von Lufthansa-Nachtblau bis Pegasus-Gelb; eine feste Textfarbe
   waere fuer die eine Haelfte immer falsch. */
function srgbChannel(v){ return v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }
function relativeLuminance(color){
  const m = /^#?([0-9a-f]{6})$/i.exec(String(color || ""));
  if (!m) return 0;
  const n = parseInt(m[1], 16);
  return 0.2126 * srgbChannel(((n >> 16) & 255) / 255)
       + 0.7152 * srgbChannel(((n >> 8) & 255) / 255)
       + 0.0722 * srgbChannel((n & 255) / 255);
}
function contrastRatio(a, b){
  const hi = Math.max(a, b), lo = Math.min(a, b);
  return (hi + 0.05) / (lo + 0.05);
}
// Luminanz von --ink und --paper, aus den Tokens gerechnet statt abgeschrieben:
// eine geaenderte Farbe darf keine falsche Konstante hinterlassen.
const INK_LUMINANCE = relativeLuminance("#1e1a16");
const PAPER_LUMINANCE = relativeLuminance("#fefdfa");
function badgeTextColor(color){
  const l = relativeLuminance(color);
  return contrastRatio(l, INK_LUMINANCE) > contrastRatio(l, PAPER_LUMINANCE)
    ? "var(--ink)"
    : "var(--paper)";
}
function tailMark(code){
  const a = AIRLINES.find(x=>x.code===code);
  const label = a && a.kind === "comparison" ? "~" : code;
  const fill = a ? a.color : "#69625d";      /* --ink-subtle als neutrale Flaeche */
  return `<i class="tail" style="background:${esc(fill)};color:${badgeTextColor(fill)}"
            title="${esc(a?a.name:code)}">${esc(label)}</i>`;
}
function drawRuler(){
  const r = $("#ruler"); r.innerHTML="";
  const days = Math.max(1, Math.round((axis.end-axis.start)/864e5));
  const ticks = Math.min(6, Math.max(1, Math.round(days/10)));
  for (let i=0;i<=ticks;i++){
    const d = new Date(axis.start.getTime()+days*864e5*(i/ticks));
    const s = document.createElement("span");
    s.style.left = (i/ticks*100)+"%";
    s.textContent = d.toLocaleDateString("de-DE",{day:"2-digit",month:"short"});
    if (i===0) s.style.transform="translateX(0)";
    if (i===ticks) s.style.transform="translateX(-100%)";
    r.append(s);
  }
}
function detailRows(o){
  const rows = o.legs.map(l => {
    if (!l.verified) {
      const why = l.indicative
        ? "Richtwert eines Vergleichsportals, echter Flugpreis liegt meist darunter"
        : "Tagesbestpreis, kein konkreter Flug geprüft";
      const src = (l.carriers||[])[0];
      const bag = l.bag_fee
        ? ` Grundtarif ${money(l.base_price)} € + Gepäck ${money(l.bag_fee)} €.`
        : "";
      const nativeNote = nativePriceLabel(l);
      return `<div class="fl">
        <span>${src?tailMark(src):""}</span>
        <span class="pair">${esc(l.origin)}-${esc(l.destination)}</span>
        <span class="when">${fmtDay(l.date)}</span>
        <span class="miss">${why}.${bag}${nativeNote?" "+esc(nativeNote)+".":""}
          ${legBandMarkup(l)}</span>
        <span class="fare">${money(l.price)} €</span><span></span></div>`;
    }
    const c = (l.carriers||[])[0] || "";
    const link = l.deep_link
      ? `<a href="${esc(l.deep_link)}" target="_blank" rel="noopener noreferrer">buchen</a>`
      : `<span></span>`;
    // Some sources price a day without naming a flight; say that rather than
    // printing an empty time range.
    const arrival = arrivalLabel(l.date, l.arrival_date);
    const stops = stopsLabel(l.stops);
    const times = l.depart
      ? `${esc(l.depart)} bis ${esc(l.arrive||"")}
         <i>${esc(l.flights||"")}${l.duration?" · "+esc(l.duration):""}`
         + `${stops?" · "+esc(stops):""}${arrival?" · "+esc(arrival):""}</i>`
      : `<i>Tagespreis bestätigt, Flugzeiten nennt diese Quelle nicht</i>`;
    const bag = l.bag_fee
      ? `<i>Grundtarif ${money(l.base_price)} € + Gepäck ${money(l.bag_fee)} €</i>`
      : "";
    const nativeNote = nativePriceLabel(l);
    const native = nativeNote ? `<i>${esc(nativeNote)}</i>` : "";
    return `<div class="fl">
      <span>${c?tailMark(c):""}</span>
      <span class="pair">${esc(l.origin)}-${esc(l.destination)}</span>
      <span class="when">${fmtDay(l.date)}</span>
      <span class="times">${times}${bag}${native}${legBandMarkup(l)}</span>
      <span class="fare">${money(l.price)} €</span>${link}</div>`;
  }).join("");
  let foot;
  if (o.verified){
    const d = o.drift;
    foot = (d===null||d===undefined||Math.abs(d)<0.01)
      ? "Live geprüft, Preis wie geschätzt."
      : `Live geprüft. <b>${d>0?"+":""}${money(d)} €</b> gegenüber der Schätzung von ${money(o.estimate)} €.`;
  } else {
    foot = "Nur die vordersten Varianten werden live nachgeprüft.";
  }
  // Naechte werden aus Abflugdaten gerechnet. Bei einem Nachtflug ist das
  // erklaerungsbeduerftig, sonst zaehlt jemand eine Nacht zu wenig.
  const overnight = (o.legs || []).some(l => arrivalLabel(l.date, l.arrival_date));
  const nightNote = overnight ? " Nächte zählen ab Abflugtag." : "";
  const stay = staySummaryLine(o);
  return rows + stayRowsMarkup(o)
    + `<p class="dsum">${foot}${nightNote}${stay ? " " + stay : ""}</p>`;
}
/* Re-ordering is a view concern: the same results, read a different way.
   Sorting locally avoids running the whole search again. */
/* Eine Zeile ohne Gesamtsumme steht am Ende, nie vorn: sonst sieht eine Luecke
   aus wie der bessere Preis. */
function compareGrand(a, b){
  const av = grandTotal(a), bv = grandTotal(b);
  if (av === null && bv === null) return a.total - b.total;
  if (av === null) return 1;
  if (bv === null) return -1;
  return av - bv || a.total - b.total;
}
function sortResults(results){
  const how = $("#sort").value;
  const copy = [...results];
  if (how === "grand") copy.sort(compareGrand);
  else if (how === "depart") copy.sort((a,b) => a.dates[0].localeCompare(b.dates[0]) || a.total-b.total);
  else if (how === "length") copy.sort((a,b) => {
    const len = o => (new Date(o.dates[o.dates.length-1]) - new Date(o.dates[0])) / 864e5;
    return len(a)-len(b) || a.total-b.total;
  });
  else copy.sort((a,b) => a.total-b.total);
  return copy;
}
function resultQuality(o){
  if (o.verified) return "verified";
  return (o.legs || []).some(l => l.indicative) ? "indicative" : "estimate";
}
function filterResults(results){
  const carrier = $("#resultCarrier").value;
  const quality = $("#resultQuality").value;
  // Ohne gemeldete Umstiegszahl wird nichts weggefiltert, sondern nichts behauptet.
  const directOnly = $("#directOnly").checked === true;
  return results.filter(o =>
    (!carrier || carriersOf(o).includes(carrier)) &&
    (!quality || resultQuality(o) === quality) &&
    (!directOnly || stopsOf(o) === 0)
  );
}
function resultFilterSummary(visible, total){
  if (visible === total) return `${visible} Varianten sichtbar`;
  return `${visible} von ${total} Varianten sichtbar`;
}
function updateResultFilters(results){
  const sel = $("#resultCarrier");
  const current = sel.value;
  const codes = [...new Set(results.flatMap(carriersOf))].sort((a,b) => {
    const an = (AIRLINES.find(x=>x.code===a)||{}).name || a;
    const bn = (AIRLINES.find(x=>x.code===b)||{}).name || b;
    return an.localeCompare(bn, "de");
  });
  sel.innerHTML = `<option value="">Alle</option>` + codes.map(c => {
    const a = AIRLINES.find(x=>x.code===c);
    return `<option value="${esc(c)}">${esc(a ? a.name : c)}</option>`;
  }).join("");
  sel.value = codes.includes(current) ? current : "";
}
function airlineSummary(results){
  const cheapest = new Map();
  results.forEach(o => carriersOf(o).forEach(c => {
    if (!cheapest.has(c) || o.total < cheapest.get(c)) cheapest.set(c, o.total);
  }));
  if (!cheapest.size){ $("#usedby").textContent = ""; return; }
  const nameOf = c => {
    const a = AIRLINES.find(x => x.code === c);
    return a ? a.name : c;
  };
  const parts = [...cheapest.entries()].sort((a,b) => a[1]-b[1]).map(([c,p]) =>
    `${tailMark(c)}<span>${esc(nameOf(c))} ab ${money(p)} €</span>`);
  $("#usedby").innerHTML = `<span>Beteiligte Airlines:</span>` + parts.join("");
}
function resultTitle(results){
  if (results.length <= 1) return `${results.length} Favorit`;
  return `Favorit + ${results.length - 1} Kandidaten`;
}
function resultRankLabel(index){ return index === 0 ? "Favorit" : `#${index + 1}`; }
/* Eine Live-Region um die ganze Tabelle liest bei jeder Sortierung alles neu
   vor. Angesagt wird deshalb nur dieser eine Satz. */
function resultSummaryLine(visible, total, cheapest){
  if (!total) return "";
  if (!visible) return "Keine Variante passt zu den aktuellen Filtern.";
  return `${resultFilterSummary(visible, total)}, günstigste ab ${money(cheapest)} €`;
}
function routeBadge(o){
  return o.route ? `<span class="routepill">${esc(o.route)}</span>` : "";
}
function rowMarkup(o, n){
  const dots = o.dates.map((d, i) => {
    const l = o.legs[i] || {};
    const t = esc(`${l.origin||""}-${l.destination||""} ${fmtDay(d)}: ${money(l.price)} €`);
    return `<i class="dot" style="left:${railLeft(pos(d))};`
         + `background:${inkShade(l.price||0, o.total, o.dates.length)}" title="${t}"></i>`;
  }).join("");
  const gaps = o.dates.slice(0, -1).map((d, i) => {
    const nights = Math.round((new Date(o.dates[i+1]) - new Date(d)) / 864e5);
    return `<i class="gap" style="left:${railLeft((pos(d) + pos(o.dates[i+1])) / 2)}">${nights}N</i>`;
  }).join("");
  const first = pos(o.dates[0]), last = pos(o.dates[o.dates.length - 1]);
  return `<tr class="opt${n === 0 ? " best" : ""}" data-n="${n}" tabindex="0" role="button"
      aria-expanded="false" aria-controls="det-${n}" style="animation-delay:${n * 18}ms">
    <td class="c-rank">${esc(resultRankLabel(n))}</td>
    <td class="c-price">${money(o.total)}<small>€</small></td>
    <td class="c-price c-grand">${grandCell(o)}</td>
    <td class="c-status">${esc(statusLabel(o))}</td>
    <td class="c-status c-band" data-signal="${esc(rowBand(o).tier)}"
      title="${esc(bandTitle(o))}">${esc(bandLabel(rowBand(o).tier))}</td>
    <td class="c-rail"><span class="rail"><i class="line"
      style="left:${railLeft(first)};width:${railWidth(last - first)}"></i>${dots}${gaps}</span></td>
    <td class="c-num">${nightsOf(o)}</td>
    <td class="c-num">${esc(stopsLabel(stopsOf(o)))}</td>
    <td class="c-air">${routeBadge(o)}${carriersOf(o).map(tailMark).join("")}</td>
  </tr>
  <tr class="detrow" id="det-${n}" data-n="${n}">
    <td colspan="${RESULT_COLUMNS}"><div class="detgrid"><div class="detinner">${
      detailRows(o)}</div></div></td>
  </tr>`;
}

function wireRows(body){
  body.querySelectorAll("tr.opt").forEach(tr => {
    const det = body.querySelector(`#det-${tr.dataset.n}`);
    const toggle = () => {
      const open = tr.classList.toggle("open");
      if (det) det.classList.toggle("open", open);
      tr.setAttribute("aria-expanded", String(open));
    };
    tr.onclick = e => { if (e.target.tagName !== "A") toggle(); };
    tr.onkeydown = e => {
      // Leertaste auf einem Link im Detail darf die Zeile nicht zuklappen.
      if (e.target.tagName === "A") return;
      if (e.key === "Enter" || e.key === " "){ e.preventDefault(); toggle(); }
    };
  });
}

/* Dieselben Ergebnisse, anders gelesen: Sortieren und Filtern bleiben lokal. */
function renderTable(results){
  lastResults = results;
  const body = $("#rows");
  body.innerHTML = "";
  $("#usedby").textContent = "";
  $("#filtercount").textContent = "";
  $("#outsummary").textContent = "";
  if (!results.length){ $("#out").classList.remove("on"); return; }
  updateResultFilters(results);
  const found = results.length;
  const visible = filterResults(results);
  $("#filtercount").textContent = resultFilterSummary(visible.length, found);
  if (!visible.length){
    $("#outtitle").textContent = "Keine passenden Kandidaten";
    $("#outsummary").textContent = resultSummaryLine(0, found, 0);
    $("#ruler").innerHTML = "";
    body.innerHTML = `<tr class="empty"><td colspan="${RESULT_COLUMNS}">`
      + `Die Suche hat Ergebnisse, aber keiner passt zu den aktuellen Filtern.</td></tr>`;
    $("#out").classList.add("on");
    return;
  }
  const rows = sortResults(visible);
  airlineSummary(rows);
  const all = rows.flatMap(o => o.dates.map(d => new Date(d + "T00:00:00")));
  axis = {start:new Date(Math.min(...all)), end:new Date(Math.max(...all))};
  drawRuler();
  $("#outtitle").textContent = resultTitle(rows);
  $("#outsummary").textContent =
    resultSummaryLine(rows.length, found, Math.min(...rows.map(o => o.total)));
  body.innerHTML = rows.map(rowMarkup).join("");
  wireRows(body);
  $("#out").classList.add("on");
}

/* ---------------- run ---------------- */
/* ---------------- Warte-Choreografie ---------------- */
/* Opt-in, nicht Opt-out: wer nichts eingestellt hat, bekommt keine Bewegung,
   und der Node-Harness ohne matchMedia faellt auf denselben Zweig. */
function motionOn(){
  return typeof matchMedia === "function"
      && matchMedia("(prefers-reduced-motion: no-preference)").matches;
}

/* Eine Zahl, die von 0 auf 61 springt, liest niemand mit. Eine, die hochlaeuft,
   sagt nebenbei, dass gerade etwas passiert. */
function countUp(el, to, ms = 260){
  const target = Number(to) || 0;
  if (!el) return target;
  const start = Number(String(el.textContent || "").replace(/[^0-9]/g, "")) || 0;
  if (!motionOn() || typeof requestAnimationFrame !== "function" || start === target){
    el.textContent = String(target);
    return target;
  }
  const started = Date.now();
  const step = () => {
    const p = Math.min(1, (Date.now() - started) / ms);
    el.textContent = String(Math.round(start + (target - start) * p));
    if (p < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
  return target;
}

/* Ein Balken je Teilstrecke. Der laengste ist voll, alles andere ist relativ
   dazu; ein Leg ohne einen einzigen Preis wird markiert, weil daran die ganze
   Suche scheitert. */
function legMeters(legs){
  const rows = Array.isArray(legs) ? legs : [];
  if (!rows.length) return "";
  const max = Math.max(1, ...rows.map(l => Number(l.dates) || 0));
  return rows.map(l => {
    const days = Number(l.dates) || 0;
    const pct = Math.round((days / max) * 100);
    return `<div class="legmeter${days ? "" : " zero"}">`
      + `<span class="legname">${esc(l.origin)}-${esc(l.destination)}</span>`
      + `<span class="legbar"><i style="width:${pct}%"></i></span>`
      + `<span class="legdays" data-days="${days}">0</span></div>`;
  }).join("");
}

function paintLegMeters(legs){
  const box = $("#legs");
  box.innerHTML = legMeters(legs);
  box.querySelectorAll(".legdays").forEach(el => countUp(el, Number(el.dataset.days) || 0));
}

function drawSteps(active){
  const i = PHASES.findIndex(p=>p[0]===active);
  $("#steps").innerHTML = PHASES.map(([k,l],n)=>
    `<span class="step ${k===active?"now":(i>n?"was":"")}">${l}</span>`).join("");
}
function progressPhase(phase){ return phase === "fetched" ? "fetching" : (phase || "planning"); }
function progressValue(p){
  const phase = progressPhase(p && p.phase);
  const [base, span] = PROGRESS_RANGES[phase] || PROGRESS_RANGES.planning;
  if (phase === "done" || phase === "failed" || phase === "cancelled") return 100;
  const total = Number(p && p.total) || 0;
  const done = Number(p && p.done) || 0;
  const ratio = total > 0 ? Math.max(0, Math.min(1, done / total)) : 0;
  return Math.max(0, Math.min(99, Math.round(base + span * ratio)));
}
function setProgress(p){
  const phase = progressPhase(p && p.phase);
  const value = progressValue(Object.assign({}, p, {phase}));
  const bar = $("#progressbar");
  bar.className = "progressbar" + (phase === "failed" ? " failed" : "");
  bar.setAttribute("aria-valuenow", value);
  $("#progressfill").style.width = `${value}%`;
  $("#progresslabel").textContent = PROGRESS_LABELS[phase] || PROGRESS_LABELS.planning;
  const total = Number(p && p.total) || 0;
  const done = Number(p && p.done) || 0;
  $("#progresscount").textContent = total > 0 ? `${done} / ${total}` : "";
}
function setSearching(on){
  const button = $("#go");
  button.disabled = on;
  button.textContent = on ? "Suche läuft" : "Suchen";
  // Eigenschaft statt Attribut: der Node-Harness kennt kein removeAttribute,
  // im Browser entfernt `hidden = false` das Attribut genauso.
  $("#cancel").hidden = !on;
}
/* Abbrechen schliesst zuerst den Stream und meldet dann dem Server, dass er
   zwischen den Phasen aufhoeren soll. Andersherum kaeme noch ein Ereignis an. */
async function cancelSearch(){
  if (es){ es.close(); es = null; }
  setSearching(false);
  setProgress({phase:"cancelled"});
  drawSteps("cancelled");
  $("#note").dataset.tone = "";
  $("#note").textContent = "Suche abgebrochen";
  if (currentJob === null) return;
  try {
    const r = await fetch(`/api/jobs/${currentJob}/cancel`, {method:"POST"});
    if (!r.ok) throw new Error(String(r.status));
  } catch {
    $("#note").dataset.tone = "warn";
    $("#note").textContent = "Suche lokal gestoppt, der Server hat den Abbruch nicht bestätigt.";
  }
  currentJob = null;
}
$("#f").onsubmit = async e => {
  e.preventDefault();
  if (es){ es.close(); es=null; }
  // checkWindow schreibt die Meldung selbst, hier reicht der Abbruch.
  if (!checkWindow()) return;
  if (hops.some(h=>!h.code)){
    $("#log").classList.add("on"); $("#note").dataset.tone="err";
    setProgress({phase:"failed"});
    $("#note").textContent="Bitte alle Flughäfen aus der Vorschlagsliste wählen."; return;
  }
  setSearching(true); $("#out").classList.remove("on");
  $("#rows").innerHTML=""; $("#legs").innerHTML="";
  $("#log").classList.add("on"); $("#note").dataset.tone="";
  $("#note").textContent="Suche startet"; drawSteps("planning"); setProgress({phase:"planning"});

  let job;
  try {
    const r = await fetch("/api/search",{method:"POST",
      headers:{"content-type":"application/json"}, body:JSON.stringify(payload())});
    const d = await r.json();
    if (!r.ok) throw new Error(detail(d.detail) || "Suche konnte nicht gestartet werden");
    job = d.job_id;
    currentJob = job;
  } catch(err){
    $("#note").dataset.tone="err"; $("#note").textContent=err.message;
    setSearching(false); return;
  }

  es = new EventSource(`/api/jobs/${job}/events`);
  es.onmessage = ev => {
    const p = JSON.parse(ev.data);
    if (p.phase==="partial"){ applyPartial((p.detail&&p.detail.results)||[]); return; }
    if (p.phase==="verified"){ applyVerified((p.detail&&p.detail.result)||null); return; }
    // Der Hotelschritt laeuft nach den Flugergebnissen. Die Schritteleiste
    // bleibt deshalb auf "Nachprüfen" stehen, der Balken laeuft weiter.
    if (p.phase==="staying"){
      setProgress(p); $("#note").dataset.tone = ""; $("#note").textContent = p.message;
      return;
    }
    if (p.phase==="stays"){
      applyStays((p.detail&&p.detail.results)||[]);
      $("#note").dataset.tone = ""; $("#note").textContent = p.message;
      return;
    }
    const phase = progressPhase(p.phase);
    drawSteps(phase);
    setProgress(Object.assign({}, p, {phase}));
    $("#note").textContent = p.message;
    $("#note").dataset.tone = p.phase === "failed" ? "err" : "";
    if (p.detail && p.detail.legs) paintLegMeters(p.detail.legs);
    if (p.phase==="done"){ renderTable((p.detail&&p.detail.results)||[]); focusResults();
                           es.close(); es=null; setSearching(false); currentJob=null; }
    if (p.phase==="failed"||p.phase==="cancelled"){ es.close(); es=null;
                           setSearching(false); currentJob=null; }
  };
  es.onerror = () => {
    if (!es) return;
    setProgress({phase:"failed"});
    $("#note").dataset.tone="err"; $("#note").textContent="Verbindung zum Server verloren";
    es.close(); es=null; setSearching(false); currentJob=null;
  };
};

/* ---------------- remembering the last search ---------------- */
const STORE = "flightopt.last";
function saveForm(){
  readStayControls();
  try {
    localStorage.setItem(STORE, JSON.stringify({
      trip, hops, airlines: [...picked],
      from: $("#from").value, to: $("#to").value,
      stays: stayRanges,
      checked_bags: Number($("#checkedBags").value || 0),
      max_stops: $("#maxStops").value,
      sort: $("#sort").value,
      with_hotels: $("#withHotels").checked === true,
      hotel_adults: $("#hotelAdults").value,
      hotel_rooms: $("#hotelRooms").value,
    }));
  } catch { /* private mode or full quota: not worth interrupting anyone over */ }
}
function loadForm(){
  let saved;
  try { saved = JSON.parse(localStorage.getItem(STORE) || "null"); } catch { return false; }
  if (!saved || !Array.isArray(saved.hops) || !saved.hops.length) return false;
  trip = TRIPS.some(t => t.id === saved.trip) ? saved.trip : trip;
  hops = saved.hops.filter(h => h && typeof h === "object")
                   .map(h => ({code: String(h.code||""), label: String(h.label||"")}));
  picked = new Set(Array.isArray(saved.airlines) ? saved.airlines : []);
  // A window from a previous session may already be in the past.
  const today = todayIso();
  if (saved.from && saved.from >= today) $("#from").value = saved.from;
  if (saved.to && saved.to > (saved.from || today)) $("#to").value = saved.to;
  if (Array.isArray(saved.stays) && saved.stays.length) {
    stayRanges = saved.stays
      .filter(r => Array.isArray(r) && r.length >= 2)
      .map(r => [+r[0], +r[1]]);
  } else if (saved.smin && saved.smax) {
    stayRanges = [[+saved.smin, +saved.smax]];
  }
  if (saved.checked_bags !== undefined) $("#checkedBags").value = String(saved.checked_bags);
  if (saved.max_stops !== undefined) $("#maxStops").value = String(saved.max_stops);
  if (saved.sort) $("#sort").value = saved.sort;
  $("#withHotels").checked = saved.with_hotels === true;
  if (saved.hotel_adults) $("#hotelAdults").value = String(saved.hotel_adults);
  if (saved.hotel_rooms) $("#hotelRooms").value = String(saved.hotel_rooms);
  setStayOptionsEnabled($("#withHotels").checked);
  return true;
}

/* ---------------- Deals ---------------- */
function dealsSummary(rows){
  const list = rows || [];
  if (!list.length) return "Noch keine gespeicherten Scans.";
  const cheap = list.filter(r => r.signal === "cheap").length;
  const head = list.length === 1 ? "1 Scan" : `${list.length} Scans`;
  if (!cheap) return `${head}, keiner unter dem üblichen Preis.`;
  if (cheap === 1) return `${head}, einer unter dem üblichen Preis.`;
  return `${head}, davon ${cheap} unter dem üblichen Preis.`;
}

function deviationLabel(pct){
  if (pct === null || pct === undefined) return "";
  const v = Number(pct);
  if (Number.isNaN(v)) return "";
  return `${v > 0 ? "+" : ""}${v.toFixed(1).replace(".", ",")} %`;
}

function signalLabel(status){
  if (status === "cheap") return "günstig";
  if (status === "expensive") return "teuer";
  if (status === "normal") return "normal";
  return "keine Baseline";
}

function scanTime(iso){
  if (!iso) return "";
  const d = new Date(String(iso));
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleString("de-DE",
    {day:"2-digit", month:"2-digit", hour:"2-digit", minute:"2-digit"});
}

function dealRowMarkup(row){
  return `<tr class="opt" data-job="${esc(row.job_id)}" tabindex="0" role="button">
    <td>${esc(row.profile || "")}</td>
    <td class="mono">${esc(row.route || "")}</td>
    <td class="mono">${esc(scanTime(row.scanned_at))}</td>
    <td class="c-price">${money(row.price)}<small>€</small></td>
    <td class="c-num">${esc(deviationLabel(row.deviation_pct))}</td>
    <td class="c-status" data-signal="${esc(row.signal || "unknown")}">${
      esc(signalLabel(row.signal))}</td>
  </tr>`;
}

function renderDeals(rows){
  const list = rows || [];
  $("#dealssummary").dataset.tone = "";
  $("#dealssummary").textContent = dealsSummary(list);
  const body = $("#dealsrows");
  body.innerHTML = list.map(dealRowMarkup).join("");
  body.querySelectorAll("tr.opt").forEach(tr => {
    const job = Number(tr.dataset.job);
    tr.onclick = () => openDeal(job);
    tr.onkeydown = e => {
      if (e.key === "Enter" || e.key === " "){ e.preventDefault(); openDeal(job); }
    };
  });
  return list;
}

/* Ein Klick auf einen Scan zeigt dieselben Datumsketten in derselben Tabelle
   wie eine frische Suche. Zwei Darstellungen fuer dieselbe Sache waeren eine
   Einladung, sie unterschiedlich zu lesen. */
async function openDeal(jobId){
  try {
    const d = await (await fetch(`/api/jobs/${jobId}`)).json();
    renderTable(d.results || []);
    focusResults();
  } catch {
    $("#dealssummary").dataset.tone = "err";
    $("#dealssummary").textContent = "Der Scan konnte nicht geladen werden.";
  }
}

async function loadDeals(){
  try {
    const d = await (await fetch("/api/deals?limit=50")).json();
    renderDeals(d.deals || []);
  } catch {
    $("#dealssummary").dataset.tone = "err";
    $("#dealssummary").textContent = "Gespeicherte Scans sind gerade nicht abrufbar.";
  }
}

/* ---------------- boot ---------------- */
$("#from").min = todayIso();
$("#to").min = todayIso();
$("#from").value = isoDay(new Date(Date.now()+30*864e5));
$("#to").value   = isoDay(new Date(Date.now()+87*864e5));
const restored = loadForm();
setStayEnabled(TRIPS.find(t=>t.id===trip).stays);
setStayOptionsEnabled($("#withHotels").checked === true);
$("#withHotels").onchange = () => {
  setStayOptionsEnabled($("#withHotels").checked === true);
  saveForm();
};
["#hotelAdults","#hotelRooms"].forEach(s => $(s).oninput = saveForm);
["#from","#to","#checkedBags","#maxStops"].forEach(s => $(s).oninput = () => {
  // `min` bleibt bei heute. Ein mitwanderndes `min` liesse den Browser eine
  // eigene Blase zeigen, und checkWindow saegte dieselbe Meldung noch einmal.
  checkWindow(); size(); updateRouteGlance(); saveForm();
});
["#sort","#resultCarrier","#resultQuality","#directOnly"].forEach(s => $(s).onchange = () => {
  saveForm(); if (lastResults.length) renderTable(lastResults);
});
$("#aiApply").onclick = parseNaturalSearch;
$("#saveProfile").onclick = saveProfileNow;
$("#profileName").onkeydown = profileNameKeydown;
setupVoiceInput();
drawTrips(); drawRoute(); loadAirlines().then(() => {
  // Restore the chips only after the registry has rendered them.
  if (restored) {
    document.querySelectorAll(".chip").forEach(c => {
      if (picked.has(c.dataset.code) && !c.disabled) c.setAttribute("aria-pressed","true");
    });
    airHint();
  }
});
size();
loadScanner();
loadDeals();
$("#cancel").onclick = cancelSearch;
updateRouteGlance();
