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
  /* Der Hotelschalter steht in einem zugeklappten Bereich. Aus der letzten
     Sitzung wiederhergestellt waere er sonst voellig unsichtbar und wuerde
     stillschweigend die Laufzeit und die Summen aendern. */
  const hotels = $("#withHotels").checked === true ? "mit Hotelkosten" : "";
  return {route, from, to, carriers, bag, stops: maxStopsLabel(), hotels};
}
function updateRouteGlance(){
  const g = routeGlance();
  const parts = [g.bag, g.carriers];
  if (g.stops) parts.splice(1, 0, g.stops);
  if (g.hotels) parts.push(g.hotels);
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
      setVoiceStatus("Höre zu");
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
    /* Der Umsortier-Hinweis darf beim Oeffnen und Schliessen der Vorschlagsliste
       nicht verloren gehen, deshalb steht er als feste Grundlage davor. */
    const baseHelp = trip === "multi" ? "routehint" : "";
    const describe = extra => [baseHelp, extra].filter(Boolean).join(" ");
    inp.setAttribute("aria-describedby", describe(""));
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
                          inp.setAttribute("aria-describedby",describe(""));
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
      inp.setAttribute("aria-describedby", describe(found ? "" : empty.id));
      paintGhost();
      inp.setAttribute("aria-expanded", String(found));
      inp.setAttribute("aria-activedescendant", activeDescendant(i, sel));
      menu.querySelectorAll("b").forEach(el => el.onmousedown = ev => {
        ev.preventDefault(); choose(items[+el.dataset.n]);
      });
      /* Die Liste ist 264 px hoch und scrollt. Ohne das hier waehlt die
         Pfeiltaste ab dem sechsten Eintrag etwas aus, das niemand sieht. */
      const marked = menu.querySelector(`[aria-selected="true"]`);
      if (marked && typeof marked.scrollIntoView === "function"){
        marked.scrollIntoView({block:"nearest"});
      }
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
      // Umsortieren ging bisher nur per Ziehen. Das kennt weder die Tastatur
      // noch ein Touchscreen: HTML-Drag-and-drop feuert dort gar nicht.
      if (e.altKey && (e.key==="ArrowLeft" || e.key==="ArrowRight")){
        e.preventDefault(); moveHop(i, e.key==="ArrowLeft" ? -1 : 1); return;
      }
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
  // Nur bei mehreren Stopps gibt es ueberhaupt etwas umzusortieren.
  $("#routehint").hidden = trip !== "multi";
  wireDrag(box);
  syncStayControls();
}

/* Dieselbe Bewegung wie das Ziehen, nur ueber die Tastatur erreichbar.
   Der Fokus wandert mit, sonst weiss nach dem Neuaufbau niemand mehr, welches
   Feld gerade bewegt wurde. */
function moveHop(from, delta){
  const list = active();
  const to = from + delta;
  if (to < 0 || to >= list.length) return false;
  const [moved] = hops.splice(from, 1);
  hops.splice(to, 0, moved);
  drawRoute(); size(); updateRouteGlance(); saveForm();
  const fields = $("#route").querySelectorAll("input");
  if (fields[to] && typeof fields[to].focus === "function") fields[to].focus();
  return true;
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
let airlinesFailed = false;
async function loadAirlines(){
  try {
    AIRLINES = (await (await fetch("/api/airlines")).json()).airlines || [];
    airlinesFailed = false;
  }
  catch { AIRLINES = []; airlinesFailed = true; }
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
  if (airlinesFailed) return "Airlines nicht abrufbar";
  const live = AIRLINES.filter(a => a.status === "live" && a.kind !== "comparison").length;
  return picked.size ? `Eingegrenzt auf ${[...picked].join(", ")}` : `Alle ${live} Airlines`;
}
/* Eine leere Liste ergab bisher Saetze wie "Preise liefert derzeit ." und
   behauptete ausserdem, jede ausgegraute Airline sei technisch blockiert,
   obwohl die meisten schlicht noch nicht angeschlossen sind. */
function airHint(){
  if (airlinesFailed){
    $("#airhint").textContent = "Die Airlineliste ist gerade nicht abrufbar. "
      + "Die Suche läuft trotzdem, sie kann nur nicht eingegrenzt werden.";
    $("#airlinescount").textContent = airlinesSummary();
    return;
  }
  const live = AIRLINES.filter(a=>a.status==="live").map(a=>a.name);
  const plan = AIRLINES.filter(a=>a.status==="planned").map(a=>a.name);
  const blocked = AIRLINES.filter(a=>a.status!=="live" && a.status!=="planned").map(a=>a.name);
  const parts = [picked.size ? `Eingeschränkt auf ${[...picked].join(", ")}.`
                             : "Es werden alle verfügbaren Airlines abgefragt."];
  if (live.length) parts.push(`Preise liefert derzeit ${live.join(", ")}.`);
  else parts.push("Derzeit liefert keine Quelle Preise.");
  if (plan.length) parts.push(`Angeschlossen wird als nächstes ${plan.join(", ")}.`);
  if (blocked.length) parts.push(`Nicht abfragbar: ${blocked.join(", ")}.`);
  $("#airhint").textContent = parts.join(" ");
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
    // Die Meldung haengt per aria-describedby an beiden Feldern. Ohne
    // aria-invalid weiss ein Screenreader trotzdem nicht, welches Feld klemmt.
    $("#to").setAttribute("aria-invalid", "true");
    return false;
  }
  msg.dataset.tone = "";
  msg.textContent = "";
  $("#to").setAttribute("aria-invalid", "false");
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

/* `hops` haelt auch die Stopps, die eine andere Reiseart einmal gebraucht hat.
   Gesucht wird aber nur, was gerade im Formular steht: sonst schickt eine
   Rueckreise die dritte Station von vorhin mit, und der Fehler "Bitte alle
   Flughaefen waehlen" zeigt auf ein Feld, das niemand sieht. */
function payload(){  readStayControls();
  const count = stayCount();
  return {
    airports: active().map(h=>h.code), trip,
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
  } catch {
    // Ein leerer Kasten sieht aus wie "passt schon". Er heisst aber: der Server
    // hat nicht geantwortet, und wie gross die Suche wird, weiss gerade niemand.
    el.textContent = "Größe der Suche nicht abrufbar. Starten geht trotzdem.";
    el.dataset.tone = "warn";
  }
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
  const d = new Date(s+"T00:00:00");
  // Ein "Invalid Date" in einer Tabellenzelle ist schlimmer als eine leere Zelle.
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString("de-DE", {weekday:"short",day:"2-digit",month:"short"});
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
/* Vier Stufen. "Fehltarif" ist nicht die Steigerung von "günstig", sondern
   eine andere Aussage: günstig heißt, der Preis liegt unter dem Üblichen;
   Fehltarif heißt, er liegt so weit darunter, dass er vermutlich nicht
   gewollt ist. Dasselbe Wort wie in der Discord-Meldung, die derselbe Fund
   auslöst, damit niemand zwei Namen für eine Sache lernt. */
const BAND_LABELS = {error:"Fehltarif", cheap:"günstig", normal:"normal",
                     expensive:"teuer"};
function bandLabel(tier){
  return BAND_LABELS[String(tier || "")] || "keine Basis";
}
/* Gemessen mit contrastRatio weiter unten: --ok und --signal liegen bei
   1,02:1 zueinander. Farbe allein trennt "günstig" und "Fehltarif" also für
   niemanden, der Rot und Grün nicht auseinanderhält. Den Unterschied trägt
   die Form: eine gefüllte Marke statt farbiger Schrift. */
function bandCell(tier){
  const label = bandLabel(tier);
  return String(tier) === "error"
    ? `<b class="tiermark">${esc(label)}</b>`
    : esc(label);
}
function bandOf(leg){
  return (leg && leg.band) || {};
}
/* Der Server schreibt Begründungen und Fehlertexte in ASCII, weil dieselben
   Sätze auch in Logs und in der Discord-Meldung stehen. Der Schirm ist der
   einzige Ort, an dem "fuer" schlicht falsch geschrieben ist. Ersetzt werden
   ganze Wörter aus einer kurzen Liste, umformuliert wird nichts: der Satz
   bleibt der des Servers, nur die Schreibung wird die des Bildschirms.
   Ein Wort, das hier fehlt, geht unverändert durch. */
const GERMAN_WORDS = {
  fuer:"für", hoechstens:"höchstens", heissen:"heißen", groesser:"größer",
  naechsten:"nächsten", moeglich:"möglich", waehrung:"Währung",
};
function readableGerman(text){
  return String(text || "").replace(/[A-Za-zÄÖÜäöüß]+/g,
    w => GERMAN_WORDS[w] || w);
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
/* Ein Fehltarif ohne Historie trägt keine Abweichung, weil es nichts gibt,
   wovon er abweicht. Nach Abweichung sortiert wäre er von jeder gewöhnlichen
   Teilstrecke verdrängt worden, die zufällig zehn Prozent daneben liegt.
   Deshalb entscheidet zuerst die Stufe und erst dann die Abweichung. */
const BAND_RANK = {error:0, cheap:1, expensive:1, normal:1};
function bandRank(tier){
  const rank = BAND_RANK[String(tier || "")];
  return rank === undefined ? 2 : rank;
}
function rowBand(o){
  let best = null;
  ((o && o.legs) || []).forEach(leg => {
    const band = bandOf(leg);
    if (!band.tier || band.tier === "unknown") return;
    const off = Math.abs(Number(band.deviation_pct) || 0);
    const rank = bandRank(band.tier);
    const better = !best || rank < best.rank || (rank === best.rank && off > best.off);
    if (better) best = {rank, off, band, leg};
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
  return (basis ? `${head} (${basis})` : head) + bandWhy(pick.band);
}
/* Nur die vierte Stufe nennt ihren Grund. Bei "günstig" wäre er die
   Wiederholung der Abweichung, die schon dasteht; bei einem Fehltarif steht
   in ihm die Schranke, an der er gemessen wurde, und ohne die ist die Stufe
   eine Behauptung. */
function bandWhy(band){
  const b = band || {};
  if (String(b.tier) !== "error" || !b.reason) return "";
  return `. Warum: ${readableGerman(b.reason)}`;
}
/* In der Detailzeile steht die Preislage je Leg, denn dort gilt sie. */
function legBandNote(leg){
  const band = bandOf(leg);
  if (!band.tier || band.tier === "unknown") return "Preislage: keine Basis";
  const off = deviationLabel(band.deviation_pct);
  const head = off ? `Preislage: ${bandLabel(band.tier)}, ${off}`
                   : `Preislage: ${bandLabel(band.tier)}`;
  const basis = bandBasis(band);
  return (basis ? `${head} (${basis})` : head) + bandWhy(band);
}
function legBandMarkup(leg){
  const band = bandOf(leg);
  const tier = band.tier || "unknown";
  return ` <i class="band" data-signal="${esc(tier)}">${esc(legBandNote(leg))}</i>`;
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
  // Der reine Uebernachtungsanteil kommt vom Server mit, wurde aber nie gezeigt.
  const stayOnly = (o.stay_total === null || o.stay_total === undefined)
    ? "" : ` Davon <b>${money(o.stay_total)} €</b> Übernachtung.`;
  return `Gesamt ab <b>${money(total)} €</b> für Flug und Übernachtung.${stayOnly}`;
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
/* Wer den Preis geliefert hat, ist nicht dasselbe wie wer fliegt. Ein Ryanair-Flug
   aus dem Kiwi-Kalender ist ein anderer Beleg als einer vom Ryanair-Kalender. */
function legSourceNote(leg){
  const src = (leg && leg.source) || "";
  if (!src) return "";
  // Das Leerzeichen steht im Markup, nicht im Rand: auf schmalen Geraeten faellt
  // der Rand weg und die Notizen klebten aneinander.
  return ` <i class="src">Quelle ${esc(src)}</i>`;
}
/* Warum dieser Preis nicht live geprueft ist. Drei Faelle, nicht zwei: ein
   Richtwert, ein blosser Tagesbestpreis aus dem Kalender, und dazwischen das
   Angebot, das eine Quelle wirklich geliefert hat, nur ohne Flugzeiten. Das
   dritte hat einen Buchungslink, und "kein konkreter Flug geprueft" wuerde
   darueber hinwegreden. Erkennbar ist es genau daran: eine benannte Quelle
   und ein Link auf ein Angebot. */
function unverifiedNote(leg){
  const l = leg || {};
  if (l.indicative)
    return "Richtwert eines Vergleichsportals, echter Flugpreis liegt meist darunter";
  if (l.source && l.deep_link)
    return "Angebot der Quelle mit Buchungslink, aber ohne Flugzeiten und nicht"
      + " live nachgeprüft";
  return "Tagesbestpreis, kein konkreter Flug geprüft";
}
function detailRows(o){
  const rows = o.legs.map(l => {
    /* Der Link haengt am Angebot, nicht am Pruefstand. Er stand bisher nur im
       geprueften Zweig, und damit fiel er ausgerechnet dort weg, wo er das
       einzige ist, was weiterhilft: bei einem Tagesangebot ohne Flugzeiten. */
    const link = l.deep_link
      ? `<a href="${esc(l.deep_link)}" target="_blank" rel="noopener noreferrer">buchen</a>`
      : `<span></span>`;
    if (!l.verified) {
      const why = unverifiedNote(l);
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
          ${legSourceNote(l)}${legBandMarkup(l)}</span>
        <span class="fare">${money(l.price)} €</span>${link}</div>`;
    }
    const c = (l.carriers||[])[0] || "";
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
      <span class="times">${times}${bag}${native}${legSourceNote(l)}${legBandMarkup(l)}</span>
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
  /* Worauf sich die Preislage der Zeile stuetzt, stand bisher nur in einem
     title-Attribut. Ein Telefon zeigt das nie. */
  const band = `<p class="dsum">Preislage der Zeile: ${esc(bandTitle(o))}</p>`;
  return rows + stayRowsMarkup(o)
    + `<p class="dsum">${foot}${nightNote}${stay ? " " + stay : ""}</p>` + band;
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
/* Waehrend einer laufenden Suche wird die Tabelle staendig neu geschrieben.
   Ein jedes Mal neu gebautes <select> wirft die Auswahl weg, auf der gerade
   jemand steht. Also nur bauen, wenn sich die Liste wirklich geaendert hat. */
let carrierOptions = "";
function updateResultFilters(results){
  const sel = $("#resultCarrier");
  const current = sel.value;
  const codes = [...new Set(results.flatMap(carriersOf))].sort((a,b) => {
    const an = (AIRLINES.find(x=>x.code===a)||{}).name || a;
    const bn = (AIRLINES.find(x=>x.code===b)||{}).name || b;
    return an.localeCompare(bn, "de");
  });
  const signature = codes.join(",");
  if (signature !== carrierOptions){
    carrierOptions = signature;
    sel.innerHTML = `<option value="">Alle</option>` + codes.map(c => {
      const a = AIRLINES.find(x=>x.code===c);
      return `<option value="${esc(c)}">${esc(a ? a.name : c)}</option>`;
    }).join("");
  }
  sel.value = codes.includes(current) ? current : "";
}
/* Ein Filter, der alles wegnimmt, braucht einen sichtbaren Weg zurueck. */
function filtersActive(){
  return Boolean($("#resultCarrier").value) || Boolean($("#resultQuality").value)
      || $("#directOnly").checked === true;
}
function resetResultFilters(){
  $("#resultCarrier").value = "";
  $("#resultQuality").value = "";
  $("#directOnly").checked = false;
  saveForm();
  if (lastResults.length) renderTable(lastResults);
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
/* Welche Zeilen aufgeklappt sind und welche schon einmal dastanden. Beides
   haengt am Routen-Datums-Schluessel, nicht am Rang: waehrend des Nachpruefens
   wird die Tabelle mehrmals pro Sekunde neu geschrieben, und dabei darf weder
   ein geoeffnetes Detail zuklappen noch die ganze Liste neu aufblinken. */
let openRows = new Set(), seenRows = new Set();
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
  const key = resultKey(o);
  const open = openRows.has(key);
  const fresh = seenRows.has(key) ? "" : " fresh";
  const band = rowBand(o);
  const status = statusLabel(o);
  return `<tr class="opt${n === 0 ? " best" : ""}${open ? " open" : ""}${fresh}"
      data-n="${n}" data-key="${esc(key)}" style="animation-delay:${n * 18}ms">
    <td class="c-rank"><button type="button" class="rowtoggle" data-n="${n}"
      aria-expanded="${open}" aria-controls="det-${n}"
      >${esc(resultRankLabel(n))}</button></td>
    <td class="c-price">${money(o.total)}<small>€</small
      ><small class="rowstatus">${esc(status)}</small></td>
    <td class="c-price c-grand">${grandCell(o)}</td>
    <td class="c-status">${esc(status)}</td>
    <td class="c-status c-band" data-signal="${esc(band.tier)}"
      >${bandCell(band.tier)}</td>
    <td class="c-rail"><span class="rail"><i class="line"
      style="left:${railLeft(first)};width:${railWidth(last - first)}"></i>${dots}${gaps}</span></td>
    <td class="c-num c-nights">${nightsOf(o)}</td>
    <td class="c-num c-stops">${esc(stopsLabel(stopsOf(o)))}</td>
    <td class="c-air">${routeBadge(o)}${carriersOf(o).map(tailMark).join("")}</td>
  </tr>
  <tr class="detrow${open ? " open" : ""}" id="det-${n}" data-n="${n}">
    <td colspan="${RESULT_COLUMNS}"><div class="detgrid"><div class="detinner">${
      detailRows(o)}</div></div></td>
  </tr>`;
}

/* Die Tastatur bedient den Knopf in der Rangspalte, die Maus darf weiter die
   ganze Zeile treffen. Der Knopf traegt den Zustand, damit beide Wege dieselbe
   Wahrheit melden. */
function wireRows(body){
  body.querySelectorAll("tr.opt").forEach(tr => {
    const det = body.querySelector(`#det-${tr.dataset.n}`);
    const button = tr.querySelector(".rowtoggle");
    const toggle = () => {
      const open = tr.classList.toggle("open");
      if (det) det.classList.toggle("open", open);
      if (button) button.setAttribute("aria-expanded", String(open));
      if (tr.dataset.key){
        if (open) openRows.add(tr.dataset.key); else openRows.delete(tr.dataset.key);
      }
    };
    if (button) button.onclick = e => { e.stopPropagation(); toggle(); };
    tr.onclick = e => {
      // Der Knopf hat schon umgeschaltet, ein Link fuehrt woanders hin.
      if (e.target.tagName === "A" || e.target.closest(".rowtoggle")) return;
      toggle();
    };
  });
}

/* Eine Live-Region, die bei jedem Zwischenstand denselben Satz noch einmal
   bekommt, laesst einen Screenreader waehrend der Suche durchgehend reden. */
function announceSummary(text){
  const el = $("#outsummary");
  if (el.textContent !== text) el.textContent = text;
}
/* Eine Suche ohne Treffer ist ein Ergebnis, kein Nichts. Frueher verschwand
   der ganze Bereich und uebrig blieb die letzte Zeile im Verlauf. */
function renderNoResults(message){
  lastResults = [];
  openRows = new Set(); seenRows = new Set();
  $("#rows").innerHTML = `<tr class="empty"><td colspan="${RESULT_COLUMNS}">${
    esc(message)}</td></tr>`;
  $("#usedby").textContent = "";
  $("#ruler").innerHTML = "";
  $("#resetfilters").hidden = true;
  $("#outtitle").textContent = "Keine Treffer";
  announceSummary(message);
  $("#out").classList.add("on");
}
/* Dieselben Ergebnisse, anders gelesen: Sortieren und Filtern bleiben lokal. */
function renderTable(results){
  lastResults = results;
  const body = $("#rows");
  body.innerHTML = "";
  $("#usedby").textContent = "";
  announceSummary("");
  if (!results.length){
    $("#out").classList.remove("on");
    openRows = new Set(); seenRows = new Set();
    return;
  }
  updateResultFilters(results);
  const found = results.length;
  const visible = filterResults(results);
  $("#resetfilters").hidden = !filtersActive();
  // Die Spalte "Mit Hotel" gibt es nur, wenn wirklich Uebernachtungen gerechnet
  // wurden. Sonst belegt sie 136 px und bleibt in jeder Zeile leer.
  const grand = results.some(hasStayCosts);
  $("#resulttable").classList.toggle("withgrand", grand);
  const sortGrand = $("#sort").querySelector('option[value="grand"]');
  if (sortGrand){
    sortGrand.disabled = !grand;
    if (!grand && $("#sort").value === "grand") $("#sort").value = "price";
  }
  if (!visible.length){
    $("#outtitle").textContent = "Keine passenden Kandidaten";
    announceSummary(resultSummaryLine(0, found, 0));
    $("#ruler").innerHTML = "";
    body.innerHTML = `<tr class="empty"><td colspan="${RESULT_COLUMNS}">`
      + `Die Suche hat ${found === 1 ? "ein Ergebnis" : `${found} Ergebnisse`}, `
      + `aber davon passt keines zu den aktuellen Filtern.</td></tr>`;
    $("#out").classList.add("on");
    return;
  }
  const rows = sortResults(visible);
  airlineSummary(rows);
  const all = rows.flatMap(o => o.dates.map(d => new Date(d + "T00:00:00")));
  axis = {start:new Date(Math.min(...all)), end:new Date(Math.max(...all))};
  drawRuler();
  $("#outtitle").textContent = resultTitle(rows);
  announceSummary(
    resultSummaryLine(rows.length, found, Math.min(...rows.map(o => o.total))));
  body.innerHTML = rows.map(rowMarkup).join("");
  rows.forEach(o => seenRows.add(resultKey(o)));
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
  const total = Number(p && p.total) || 0;
  const done = Number(p && p.done) || 0;
  const terminal = phase === "done" || phase === "failed" || phase === "cancelled";
  // Solange die Gesamtzahl fehlt, ist jeder Prozentwert geraten. Dann sagt ein
  // wanderndes Stueck die Wahrheit: es laeuft, wie weit weiss noch niemand.
  const pending = !terminal && total <= 0;
  const bar = $("#progressbar");
  bar.className = "progressbar" + (phase === "failed" ? " failed" : "")
                + (pending ? " pending" : "");
  bar.setAttribute("aria-valuenow", value);
  if (pending) bar.setAttribute("aria-valuetext", "Läuft, Umfang noch offen");
  else bar.setAttribute("aria-valuetext", `${value} Prozent`);
  $("#progressfill").style.width = pending ? "" : `${value}%`;
  $("#progresslabel").textContent = PROGRESS_LABELS[phase] || PROGRESS_LABELS.planning;
  $("#progresscount").textContent = total > 0 ? `${done} / ${total}` : "";
}
/* Der Lauf meldet Teilausfaelle mit: erschoepfte Budgets, geblockte Quellen,
   uebersprungene Tage. Bisher landeten die im Nichts, und das Ergebnis war
   einfach duenner, ohne dass jemand den Grund erfuhr. */
let runNotes = [];
function resetRunNotes(){
  runNotes = [];
  $("#runnotes").textContent = "";
  $("#runnotes").dataset.tone = "";
}
function addRunNotes(list, head){
  const items = (Array.isArray(list) ? list : []).map(v => String(v)).filter(Boolean);
  if (!items.length) return runNotes;
  items.forEach(item => {
    const line = head ? `${head}: ${item}` : item;
    if (!runNotes.includes(line)) runNotes.push(line);
  });
  $("#runnotes").dataset.tone = "warn";
  $("#runnotes").textContent = runNotes.join("\n");
  return runNotes;
}
function skippedNote(count){
  const n = Number(count) || 0;
  if (n <= 0) return "";
  return n === 1 ? "1 Abruf wurde übersprungen." : `${n} Abrufe wurden übersprungen.`;
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
  if (active().some(h=>!h.code)){
    $("#log").classList.add("on"); $("#note").dataset.tone="err";
    setProgress({phase:"failed"});
    $("#note").textContent="Bitte alle Flughäfen aus der Vorschlagsliste wählen."; return;
  }
  setSearching(true); $("#out").classList.remove("on");
  $("#rows").innerHTML=""; $("#legs").innerHTML="";
  openRows = new Set(); seenRows = new Set(); resetRunNotes();
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
      // Ohne das blieb der Balken auf dem Stand vor dem Hotelschritt stehen.
      setProgress(p);
      addRunNotes((p.detail&&p.detail.notes)||[], "Hotelkosten");
      $("#note").dataset.tone = ""; $("#note").textContent = p.message;
      return;
    }
    const phase = progressPhase(p.phase);
    drawSteps(phase);
    setProgress(Object.assign({}, p, {phase}));
    $("#note").textContent = p.message;
    $("#note").dataset.tone = p.phase === "failed" ? "err" : "";
    if (p.detail && p.detail.legs) paintLegMeters(p.detail.legs);
    // Quellen, die nicht geantwortet haben, stehen im Ereignis. Der Nutzer sah
    // davon bisher nur das Ergebnis: weniger Preise, ohne Begruendung.
    if (p.detail && p.detail.errors) addRunNotes(p.detail.errors, "Preisabruf");
    if (p.phase==="done"){
      const results = (p.detail&&p.detail.results)||[];
      if (results.length) renderTable(results);
      else renderNoResults("Für dieses Fenster hat keine Quelle eine vollständige "
        + "Reise gefunden. Ein größeres Fenster oder ein anderer Aufenthalt hilft meist.");
      focusResults();
      es.close(); es=null; setSearching(false); currentJob=null;
    }
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
      direct_only: $("#directOnly").checked === true,
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
  $("#directOnly").checked = saved.direct_only === true;
  $("#withHotels").checked = saved.with_hotels === true;
  if (saved.hotel_adults) $("#hotelAdults").value = String(saved.hotel_adults);
  if (saved.hotel_rooms) $("#hotelRooms").value = String(saved.hotel_rooms);
  setStayOptionsEnabled($("#withHotels").checked);
  // Eine wiederhergestellte Einstellung, die in einem zugeklappten Bereich
  // liegt, wirkt auf jede Suche und ist trotzdem nicht zu sehen.
  if (optionsChanged()) $("#optionsdetails").open = true;
  return true;
}
/* Weicht in den Optionen etwas von der Vorgabe ab? */
function optionsChanged(){
  return Number($("#checkedBags").value || 0) > 0
      || $("#maxStops").value !== ""
      || $("#withHotels").checked === true;
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

/* Dieselbe Aussage traegt ueberall denselben Namen. "keine Baseline" in den
   Scans und "keine Basis" in der Ergebnistabelle waren zwei Woerter fuer
   denselben Zustand. Seit der vierten Stufe gibt es dafuer nur noch eine
   Liste: zwei Tabellen auf einer Seite duerfen sie nicht getrennt pflegen.
   Der Kettenpreis der Scans erreicht `error` heute nicht, aber wenn er es
   je tut, heisst er hier nicht ploetzlich "keine Basis". */
function signalLabel(status){
  return bandLabel(status);
}

function scanTime(iso){
  if (!iso) return "";
  const d = new Date(String(iso));
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleString("de-DE",
    {day:"2-digit", month:"2-digit", hour:"2-digit", minute:"2-digit"});
}

/* Ein Scan ohne die Reisedaten sagt nicht, worauf sich der Preis bezieht: zwei
   Zeilen desselben Profils sehen dann gleich aus und meinen andere Tage. */
function dealDates(row){
  const dates = (row && row.dates) || [];
  if (!dates.length) return "";
  const first = dates[0], last = dates[dates.length - 1];
  return first === last ? fmtDay(first) : `${fmtDay(first)} bis ${fmtDay(last)}`;
}
function dealPriceNote(row){
  const usual = (row && row.median !== null && row.median !== undefined)
    ? ` üblich ${money(row.median)} €` : "";
  return `${row && row.verified ? "geprüft" : "Schätzung"}${usual}`;
}
function dealRowMarkup(row){
  return `<tr class="opt" data-job="${esc(row.job_id)}">
    <td><button type="button" class="dealopen" data-job="${esc(row.job_id)}"
      >${esc(row.profile || "Ohne Namen")}</button></td>
    <td class="mono c-route">${esc(row.route || "")}</td>
    <td class="c-dates">${esc(dealDates(row))}</td>
    <td class="mono c-scan">${esc(scanTime(row.scanned_at))}</td>
    <td class="c-price">${money(row.price)}<small>€</small
      ><small class="pricenote">${esc(dealPriceNote(row))}</small></td>
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
  // Der Knopf in der Profilspalte ist der Bedienweg, die Zeile die Trefferflaeche.
  body.querySelectorAll(".dealopen").forEach(button => {
    button.onclick = e => { e.stopPropagation(); openDeal(Number(button.dataset.job)); };
  });
  body.querySelectorAll("tr.opt").forEach(tr => {
    tr.onclick = e => {
      if (e.target.closest(".dealopen")) return;
      openDeal(Number(tr.dataset.job));
    };
  });
  return list;
}

/* Ein Klick auf einen Scan zeigt dieselben Datumsketten in derselben Tabelle
   wie eine frische Suche. Zwei Darstellungen fuer dieselbe Sache waeren eine
   Einladung, sie unterschiedlich zu lesen. */
async function openDeal(jobId){
  try {
    // Ohne diese Pruefung wurde aus einem 404 ein stilles renderTable([]):
    // die Tabelle verschwand und niemand sagte, warum.
    const r = await fetch(`/api/jobs/${jobId}`);
    const d = await r.json();
    if (!r.ok) throw new Error(detail(d.detail) || `HTTP ${r.status}`);
    const results = d.results || [];
    openRows = new Set(); seenRows = new Set();
    if (!results.length){
      renderNoResults("Für diesen Scan sind keine Ergebnisse mehr gespeichert.");
    } else {
      renderTable(results);
    }
    $("#dealssummary").dataset.tone = "";
    focusResults();
  } catch (err) {
    $("#dealssummary").dataset.tone = "err";
    $("#dealssummary").textContent =
      `Der Scan konnte nicht geladen werden: ${err.message || err}`;
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

/* ---------------- Beobachtungsliste ---------------- */
const WATCH_COLUMNS = 8;
const WATCH_MIN_DAYS = 5;
/* Rueckfall, falls die Zusammenfassung die Zahl nicht mitschickt. Die
   verbindliche Zahl steht im Server, nicht hier. */
const WATCH_MAX_HOT = 5;
let lastWatchSummary = null;

/* Ohne Eintrag zeichnet niemand etwas auf, und die leere Tabelle sieht dann
   aus wie eine Aufzeichnung ohne Treffer. Der Text sagt deshalb, was ein
   Eintrag bewirkt und ab wann er etwas wert ist. */
function watchlistSummary(body){
  const rows = (body && body.routes) || [];
  const sum = (body && body.summary) || {};
  const need = Number(sum.min_days || WATCH_MIN_DAYS);
  if (!rows.length){
    return `Noch wird nichts aufgezeichnet. Trage eine Strecke ein: ab dann `
      + `wird ihr Preiskalender täglich mitgeschrieben, und nach etwa ${need} `
      + `Tagen trägt die erste Aussage zur Preislage.`;
  }
  const active = Number(sum.active || 0);
  const head = rows.length === 1 ? "1 Strecke" : `${rows.length} Strecken`;
  const state = active === rows.length
    ? (rows.length === 1 ? "aktiv" : "alle aktiv")
    : `${active} aktiv`;
  const obs = Number(sum.observations || 0).toLocaleString("de-DE");
  const last = sum.last_run_at ? ` Zuletzt ${scanTime(sum.last_run_at)}.` : "";
  return `${head}, ${state}. ${obs} Beobachtungen gesammelt.${last}`;
}

/* Die Frage der Spalte ist nicht "wie viel", sondern "ab wann traegt das".
   Beobachtungen zaehlen Reisetage, Aufzeichnungstage zaehlen Messpunkte je
   Kombination aus Strecke, Wochentag und Vorlauf. Nur die zweite Zahl
   entscheidet, wann eine Baseline steht. */
function watchReadiness(row){
  const days = Number((row && row.days_recorded) || 0);
  const need = Number((row && row.min_days) || WATCH_MIN_DAYS);
  if (!days) return "noch nichts aufgezeichnet";
  if (days < need) return `noch ${need - days} von ${need} Tagen`;
  return `trägt, ${days} Tage aufgezeichnet`;
}

function watchLeadLabel(row){
  const lo = Number((row && row.lead_min_days) || 0);
  const hi = Number((row && row.lead_max_days) || 0);
  return `${lo} bis ${hi} Tage`;
}

function watchPayload(){
  return {
    origin: String($("#watchFrom").value || "").trim(),
    destination: String($("#watchTo").value || "").trim(),
    lead_min_days: Number($("#watchLeadMin").value || 0),
    lead_max_days: Number($("#watchLeadMax").value || 0),
  };
}

/* Zwei Taktarten, zwei Woerter. "heiß" ist dasselbe Wort, das der Server
   benutzt und mit dem die Jagd beschrieben ist. */
const CADENCE_LABELS = {daily:"täglich", hot:"heiß"};
function cadenceLabel(row){
  return CADENCE_LABELS[String((row || {}).cadence || "daily")] || "täglich";
}
/* Die Obergrenze ist keine Sperre, sondern ein Budget: der Server lehnt eine
   sechste heiße Strecke nicht ab, sie kommt nur seltener dran. Genau das muss
   dastehen. Eine erfundene Fehlermeldung waere die eine Auskunft, die schlimmer
   ist als gar keine, weil sie stimmen koennte und es nicht tut. */
function hotBudgetLine(summary){
  const s = summary || {};
  const max = Number(s.max_hot_routes || WATCH_MAX_HOT);
  const hot = Number(s.hot || 0);
  const every = Math.max(1, Math.round(Number(s.hot_interval_seconds || 1200) / 60));
  if (hot > max){
    return `${hot} Strecken heiß, getragen sind ${max}. Keine wird abgelehnt,`
      + ` alle teilen sich dasselbe Budget und kommen damit seltener dran als`
      + ` alle ${every} Minuten.`;
  }
  const head = `${hot} von ${max} Strecken heiß, alle ${every} Minuten abgefragt.`;
  return hot >= max
    ? `${head} Eine weitere wäre nicht abgelehnt, sondern langsamer: das Budget`
      + ` bleibt gleich groß.`
    : head;
}
function watchCadenceCell(row){
  const hot = String((row || {}).cadence || "daily") === "hot";
  return `<td class="c-cadence" data-cadence="${hot ? "hot" : "daily"}"
    ><span class="cadencenow">${esc(cadenceLabel(row))}</span
    ><button type="button" class="cadenceswitch" data-route="${esc(row.id)}"
      data-cadence="${hot ? "daily" : "hot"}">${
      hot ? "auf täglich" : "heiß schalten"}</button></td>`;
}

function watchRowMarkup(row){
  const on = row.enabled !== false;
  return `<tr data-route="${esc(row.id)}"${on ? "" : ' class="off"'}>
    <td class="mono c-route">${esc(row.route || "")}</td>
    <td class="c-lead">${esc(watchLeadLabel(row))}</td>
    <td class="mono c-scan">${esc(row.last_run_at ? scanTime(row.last_run_at) : "noch nie")}</td>
    <td class="c-num c-count">${Number(row.observations || 0).toLocaleString("de-DE")}</td>
    <td class="c-num c-days">${Number(row.days_recorded || 0)}</td>
    <td class="c-status" data-signal="${row.ready ? "normal" : "unknown"}">${
      esc(watchReadiness(row))}</td>
    ${watchCadenceCell(row)}
    <td><button type="button" class="watchtoggle" data-route="${esc(row.id)}"
      data-enabled="${on ? "1" : "0"}">${on ? "Abschalten" : "Einschalten"}</button>${
      on ? "" : ' <small class="pricenote">aus</small>'
      }<button type="button" class="watchcurve" data-origin="${esc(row.origin || "")}"
      data-destination="${esc(row.destination || "")}">Verlauf</button></td>
  </tr>`;
}

function renderWatchlist(body){
  const rows = (body && body.routes) || [];
  lastWatchSummary = (body && body.summary) || {};
  $("#watchsummary").dataset.tone = "";
  $("#watchsummary").textContent = watchlistSummary(body);
  $("#watchhot").dataset.tone = "";
  $("#watchhot").textContent = hotBudgetLine(lastWatchSummary);
  const table = $("#watchrows");
  table.innerHTML = rows.length
    ? rows.map(watchRowMarkup).join("")
    : `<tr class="empty"><td colspan="${WATCH_COLUMNS}">Keine Strecke wird `
      + `beobachtet. Das Formular darüber trägt die erste ein.</td></tr>`;
  table.querySelectorAll(".watchtoggle").forEach(button => {
    button.onclick = () => toggleWatchRoute(
      Number(button.dataset.route), button.dataset.enabled !== "1"
    );
  });
  table.querySelectorAll(".cadenceswitch").forEach(button => {
    button.onclick = () => setWatchCadence(
      Number(button.dataset.route), button.dataset.cadence);
  });
  table.querySelectorAll(".watchcurve").forEach(button => {
    button.onclick = () => loadHistory(
      button.dataset.origin, button.dataset.destination);
  });
  return rows;
}

async function loadWatchlist(){
  try {
    const d = await (await fetch("/api/watchlist")).json();
    renderWatchlist(d);
  } catch {
    $("#watchsummary").dataset.tone = "err";
    $("#watchsummary").textContent = "Die Beobachtungsliste ist gerade nicht abrufbar.";
  }
}

async function addWatchRoute(){
  const msg = $("#watchmsg");
  msg.dataset.tone = "";
  msg.textContent = "Trage Strecke ein";
  try {
    const r = await fetch("/api/watchlist", {method:"POST",
      headers:{"content-type":"application/json"}, body:JSON.stringify(watchPayload())});
    const d = await r.json();
    if (!r.ok) throw new Error(detail(d.detail) || "Strecke konnte nicht eingetragen werden");
    msg.dataset.tone = "ok";
    msg.textContent = `${d.route} wird beobachtet`;
    $("#watchFrom").value = ""; $("#watchTo").value = "";
    await loadWatchlist();
  } catch (err) {
    msg.dataset.tone = "err";
    msg.textContent = err.message || "Strecke konnte nicht eingetragen werden";
  }
}

async function toggleWatchRoute(id, enabled){
  const msg = $("#watchmsg");
  msg.dataset.tone = "";
  msg.textContent = enabled ? "Schalte ein" : "Schalte ab";
  try {
    const r = await fetch(`/api/watchlist/${id}`, {method:"PATCH",
      headers:{"content-type":"application/json"}, body:JSON.stringify({enabled})});
    const d = await r.json();
    if (!r.ok) throw new Error(detail(d.detail) || "Schalter blieb wirkungslos");
    msg.dataset.tone = "ok";
    /* Abgeschaltet heisst nicht geloescht: die Zeile und ihre Historie bleiben,
       nur wird nichts mehr dazugeschrieben. */
    msg.textContent = d.enabled
      ? `${d.route} wird wieder aufgezeichnet`
      : `${d.route} pausiert, die Historie bleibt`;
    await loadWatchlist();
  } catch (err) {
    msg.dataset.tone = "err";
    msg.textContent = err.message || "Schalter blieb wirkungslos";
  }
}

/* Nach dem Schalten steht die frische Zahl da, nicht die geratene: erst die
   Liste neu holen, dann den Satz bilden. */
function cadenceSwitchLabel(route, summary){
  const r = route || {};
  if (!r.hot) return `${r.route} läuft wieder im Tagestakt`;
  const s = summary || {};
  const max = Number(s.max_hot_routes || WATCH_MAX_HOT);
  const hot = Number(s.hot || 0);
  return hot > max
    ? `${r.route} ist heiß. Das sind ${hot} heiße Strecken bei einem Budget für`
      + ` ${max}: abgelehnt wird keine, jede kommt seltener dran.`
    : `${r.route} ist heiß, ${hot} von ${max}`;
}

async function setWatchCadence(id, cadence){
  const msg = $("#watchmsg");
  msg.dataset.tone = "";
  msg.textContent = cadence === "hot" ? "Schalte heiß" : "Schalte auf täglich";
  try {
    const r = await fetch(`/api/watchlist/${id}`, {method:"PATCH",
      headers:{"content-type":"application/json"}, body:JSON.stringify({cadence})});
    const d = await r.json();
    if (!r.ok) throw new Error(readableGerman(detail(d.detail))
      || "Der Takt blieb, wie er war");
    await loadWatchlist();
    msg.dataset.tone = "ok";
    msg.textContent = cadenceSwitchLabel(d, lastWatchSummary);
    await loadHuntHealth();
  } catch (err) {
    msg.dataset.tone = "err";
    msg.textContent = err.message || "Der Takt blieb, wie er war";
  }
}

/* Was der Lauf gebracht hat, in einem Satz. Der Rest, der faellig blieb,
   gehoert dazu: sonst sieht ein halber Durchgang aus wie ein ganzer. */
function watchRunLabel(report){
  const routes = Number((report && report.routes) || 0);
  if (!routes) return "Heute ist schon alles aufgezeichnet.";
  const noun = routes === 1 ? "Strecke" : "Strecken";
  const obs = Number((report && report.observations) || 0).toLocaleString("de-DE");
  const left = Number((report && report.due_left) || 0);
  const rest = left ? `, ${left} bleiben fällig` : "";
  return `${routes} ${noun} abgefragt, ${obs} Beobachtungen${rest}`;
}

async function runWatchlistNow(){
  const msg = $("#watchmsg");
  msg.dataset.tone = "";
  msg.textContent = "Zeichne auf";
  try {
    const d = await (await fetch("/api/watchlist/run-once",{method:"POST"})).json();
    msg.dataset.tone = Number(d.routes || 0) ? "ok" : "";
    msg.textContent = watchRunLabel(d);
    await loadWatchlist();
  } catch {
    msg.dataset.tone = "err";
    msg.textContent = "Die Aufzeichnung konnte nicht gestartet werden.";
  }
}

/* ---------------- Fehltarif-Jagd ---------------- */
/* Was die Jagd gefunden hat, wie belastbar es ist, ob es hinausging und wo
   man bucht. Die Reihenfolge der Fragen ist die Reihenfolge der Spalten. */
const HUNT_COLUMNS = 5;

/* Der Weg einer Meldung, in den vier Worten, die dazu gehoeren. `dry_run`
   heisst nicht "Fehler" und nicht "erledigt": erkannt und aufgeschrieben,
   aber nicht gesendet, weil kein Kanal eingerichtet ist. */
const DELIVERY_LABELS = {
  sent: "gemeldet",
  dry_run: "erkannt, nicht gesendet",
  suppressed: "zurückgehalten",
  failed: "Meldung fehlgeschlagen",
};
function deliveryLabel(find){
  // "offen" waere hier das falsche Wort: es heisst in dieser Ansicht schon
  // "noch nicht abgehakt".
  return DELIVERY_LABELS[String((find || {}).delivery || "")] || "unbekannt";
}
/* Warum eine Meldung nicht hinausging, steht am Fund und nicht im Log. Ohne
   das sieht eine zurueckgehaltene Doppelmeldung aus wie ein Ausfall. */
function deliveryNote(find){
  const f = find || {};
  if (f.delivery === "sent") return "";
  if (f.error) return String(f.error);
  if (f.delivery === "dry_run") return "Kein Kanal eingerichtet.";
  return "";
}

/* Worauf ein Fund steht. Drei Stufen, weil es drei wirklich verschiedene
   Lagen gibt: eine Historie, die traegt; eine, die noch zu duenn ist; und
   gar keine, wo allein die Entfernungsschranke urteilt. Die dritte ist die
   schwaechste Aussage, die dieses Werkzeug macht, und sie muss auch so
   aussehen. */
const STRENGTH_STEPS = {floor:1, thin:2, solid:3};
function findStrength(find){
  const f = find || {};
  const n = Number(f.n) || 0;
  if (n <= 0) return "floor";
  return (f.thin || n < 10) ? "thin" : "solid";
}
function strengthMark(find){
  const level = findStrength(find);
  const filled = STRENGTH_STEPS[level];
  const pips = [1, 2, 3].map(i => `<i${i <= filled ? ' class="on"' : ""}></i>`).join("");
  // Die Marke sagt nichts, was der Satz daneben nicht sagt: sie ist der Blick,
  // er ist die Aussage. Deshalb wird sie nicht vorgelesen.
  return `<span class="strength" data-strength="${esc(level)}" aria-hidden="true"
    >${pips}</span>`;
}
/* Derselbe Satzbau wie die Preislage in der Ergebnistabelle, weil es
   dieselbe Frage ist. `bandBasis` nimmt genau die Felder, die ein Fund
   ohnehin traegt. */
function findBasisLine(find){
  const f = find || {};
  const basis = bandBasis(f);
  if (!basis) return "Keine Vergleichspreise. Diesen Fund trägt allein die"
    + " Entfernungsschranke.";
  const usual = (f.median === null || f.median === undefined)
    ? "Kein Median" : `Üblich ${money(f.median)} €`;
  return `${usual} ${basis}.`;
}
function findReason(find){
  return readableGerman((find || {}).reason);
}
/* Ein Fehltarif hält selten lange. "09.09., 19:02" beantwortet die Frage
   "ist das noch aktuell" nicht, das Alter beantwortet sie. Nachgeprüft wird
   nichts: was hier steht, ist das Alter des Fundes und nicht die Aussage,
   dass der Preis noch steht. Genau so steht es auch in der Legende. */
function findAge(iso, now){
  const then = new Date(String(iso || "")).getTime();
  if (!Number.isFinite(then)) return "";
  const mins = Math.max(0, Math.round(
    ((now ? now.getTime() : Date.now()) - then) / 60000));
  if (mins < 1) return "gerade eben";
  if (mins < 60) return `vor ${mins} ${mins === 1 ? "Minute" : "Minuten"}`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `vor ${hours} ${hours === 1 ? "Stunde" : "Stunden"}`;
  const days = Math.floor(hours / 24);
  return `vor ${days} ${days === 1 ? "Tag" : "Tagen"}`;
}
/* Ab wann ein Fund alt aussieht. Die Schwelle ist nicht erfunden: es ist die
   Ruhezeit, nach der die Jagd denselben Fund erneut melden würde. Wer so
   lange nicht hingesehen hat, sieht wahrscheinlich einen Preis, den es nicht
   mehr gibt. */
function findIsStale(find, quietHours, now){
  const then = new Date(String((find || {}).created_at || "")).getTime();
  if (!Number.isFinite(then)) return false;
  const limit = Number(quietHours) > 0 ? Number(quietHours) : 6;
  return ((now ? now.getTime() : Date.now()) - then) > limit * 3600000;
}
function findDistance(find){
  const km = Number((find || {}).distance_km);
  return Number.isFinite(km) && km > 0
    ? `${Math.round(km).toLocaleString("de-DE")} km` : "";
}

/* Ohne Webhook laeuft alles trocken. Das ist weder ein Fehler noch ein
   Erfolg, sondern der vorgesehene erste Betriebszustand, und genau so muss
   die Zeile klingen. Deshalb traegt sie nie einen Ton. */
function channelLine(summary){
  const s = summary || {};
  if (s.channel_configured) return "Discord-Kanal eingerichtet. Ein Fund geht als"
    + " Meldung hinaus.";
  return "Kein Discord-Kanal eingerichtet. Funde werden erkannt und aufgezeichnet,"
    + " aber nicht gesendet.";
}

function findsWord(n){
  return n === 1 ? "1 Fund" : `${Number(n || 0).toLocaleString("de-DE")} Funde`;
}
function huntSummary(body){
  const s = (body && body.summary) || {};
  const events = Number(s.events || 0);
  if (!events) return "Noch kein Fehltarif gefunden. Die Jagd läuft auf den"
    + " Strecken, die in der Beobachtungsliste heiß geschaltet sind.";
  const open = Number(s.open || 0);
  const offen = open ? `${open} noch offen` : "alle abgehakt";
  const last = s.last_find_at ? ` Zuletzt ${scanTime(s.last_find_at)}.` : "";
  const sent = Number(s.sent || 0);
  const dry = Number(s.dry_run || 0);
  const failed = Number(s.failed || 0);
  const parts = [];
  if (sent) parts.push(`${sent} gemeldet`);
  if (dry) parts.push(`${dry} nur erkannt`);
  if (failed) parts.push(`${failed} fehlgeschlagen`);
  const ways = parts.length ? ` Davon ${parts.join(", ")}.` : "";
  return `${findsWord(events)}, ${offen}.${last}${ways}`;
}

function findRowMarkup(row, quietHours){
  const f = row || {};
  const open = !f.acknowledged_at;
  const book = f.booking_url
    ? `<a class="findbook" href="${esc(f.booking_url)}" target="_blank"
        rel="noopener noreferrer">buchen</a>`
    : `<span class="pricenote">kein Link</span>`;
  const note = deliveryNote(f);
  const km = findDistance(f);
  const stale = findIsStale(f, quietHours);
  const age = findAge(f.created_at);
  return `<tr data-find="${esc(f.id)}" class="${open ? "open" : "done"}${
      stale ? " stale" : ""}">
    <td class="c-route"><b class="mono">${esc(f.route || "")}</b>
      <small class="pricenote">${esc(fmtDay(f.travel_date))}${
        km ? `, ${esc(km)}` : ""}</small>
      <small class="findage">gefunden ${esc(age || scanTime(f.created_at))}</small>
      <small class="findstate">${esc(deliveryLabel(f))}</small></td>
    <td class="c-price">${money(f.price)}<small>€</small
      ><small class="pricenote">${esc(f.source || "ohne Quelle")}</small></td>
    <td class="c-why" data-strength="${esc(findStrength(f))}">${strengthMark(f)
      }<b class="findbasis">${esc(findBasisLine(f))}</b>
      <small class="findwhy">${esc(findReason(f))}</small></td>
    <td class="c-status">${esc(deliveryLabel(f))}${
      note ? `<small class="findwhy">${esc(note)}</small>` : ""}</td>
    <td class="c-act">${book}<button type="button" class="findcurve"
        data-origin="${esc(f.entity_key ? String(f.entity_key).split("|")[0] : "")}"
        data-destination="${esc(f.entity_key ? String(f.entity_key).split("|")[1] || "" : "")}"
        >Verlauf</button><button type="button" class="findack"
        data-find="${esc(f.id)}" data-open="${open ? "1" : "0"}"
        >${open ? "Abhaken" : "Wieder öffnen"}</button></td>
  </tr>`;
}

/* Null Zeilen bei gesetztem Filter heisst etwas anderes als null Zeilen
   ueberhaupt. Ohne den Unterschied sucht jemand einen Fehler, wo keiner ist. */
function huntEmptyLine(summary, openOnly){
  if (!Number((summary || {}).events || 0))
    return "Noch nichts gefunden. Ein Fehltarif ist selten, und das ist der"
      + " Sinn der engen Schwelle.";
  return openOnly
    ? "Keine offenen Funde. Der Filter darüber zeigt auch die abgehakten."
    : "Keine Funde in dieser Liste.";
}

function renderHunt(body){
  const rows = (body && body.finds) || [];
  const summary = (body && body.summary) || {};
  $("#huntsummary").dataset.tone = "";
  $("#huntsummary").textContent = huntSummary(body);
  // Nie "ok", nie "err": ein Trockenlauf ist beides nicht.
  $("#huntchannel").dataset.tone = "";
  $("#huntchannel").textContent = channelLine(summary);
  const table = $("#huntrows");
  table.innerHTML = rows.length
    ? rows.map(row => findRowMarkup(row, summary.quiet_hours)).join("")
    : `<tr class="empty"><td colspan="${HUNT_COLUMNS}">${esc(huntEmptyLine(
        summary, $("#huntOpenOnly").checked === true))}</td></tr>`;
  table.querySelectorAll(".findack").forEach(button => {
    button.onclick = () => acknowledgeFind(
      Number(button.dataset.find), button.dataset.open === "1");
  });
  table.querySelectorAll(".findcurve").forEach(button => {
    button.onclick = () => loadHistory(
      button.dataset.origin, button.dataset.destination);
  });
  return rows;
}

async function loadHunt(){
  const open = $("#huntOpenOnly").checked === true;
  try {
    const d = await (await fetch(`/api/hunt/finds?limit=50&open_only=${open}`)).json();
    renderHunt(d);
  } catch {
    $("#huntsummary").dataset.tone = "err";
    $("#huntsummary").textContent = "Die Funde sind gerade nicht abrufbar.";
  }
}

async function acknowledgeFind(id, acknowledged){
  const msg = $("#huntmsg");
  msg.dataset.tone = "";
  msg.textContent = acknowledged ? "Hake ab" : "Öffne wieder";
  try {
    const r = await fetch(`/api/hunt/finds/${id}`, {method:"PATCH",
      headers:{"content-type":"application/json"},
      body:JSON.stringify({acknowledged})});
    const d = await r.json();
    if (!r.ok) throw new Error(detail(d.detail) || "Der Fund blieb, wie er war");
    msg.dataset.tone = "ok";
    msg.textContent = d.acknowledged_at
      ? `${d.route} abgehakt`
      : `${d.route} wieder offen`;
    await loadHunt();
  } catch (err) {
    msg.dataset.tone = "err";
    msg.textContent = err.message || "Der Fund blieb, wie er war";
  }
}

/* Was ein Durchgang gebracht hat. Aufgeschobene Strecken gehoeren dazu:
   sonst sieht ein Durchgang, den das Budget halbiert hat, aus wie ein
   vollstaendiger. */
function huntRunLabel(report){
  const r = report || {};
  const routes = Number(r.routes || 0);
  if (!routes) return "Gerade ist keine heiße Strecke fällig.";
  const noun = routes === 1 ? "Strecke" : "Strecken";
  const finds = Number(r.finds || 0);
  const found = finds ? `, ${findsWord(finds)}` : ", nichts gefunden";
  const left = (r.deferred || []).length;
  const rest = left ? `, ${left} aufgeschoben` : "";
  const paused = (r.paused || []).length
    ? `. Sicherung offen bei ${(r.paused || []).join(", ")}` : "";
  return `${routes} ${noun} abgefragt${found}${rest}${paused}`;
}

async function runHuntNow(){
  const msg = $("#huntmsg");
  msg.dataset.tone = "";
  msg.textContent = "Jage";
  try {
    const d = await (await fetch("/api/hunt/run-once", {method:"POST"})).json();
    msg.dataset.tone = Number((d || {}).finds || 0) ? "ok" : "";
    msg.textContent = huntRunLabel(d);
    await loadHunt();
    await loadHuntHealth();
  } catch {
    msg.dataset.tone = "err";
    msg.textContent = "Der Durchgang konnte nicht gestartet werden.";
  }
}

/* ---------------- Preisverlauf ---------------- */
/* Handgezeichnet, wie der Rest hier auch: keine Bibliothek, kein Aufbauschritt.
   Der Kasten ist in Prozent gerechnet und wird gestreckt, deshalb steht kein
   Text darin. Beschriftet wird daneben, genau wie die Datumsschiene ueber der
   Ergebnistabelle. Striche behalten ihre Staerke ueber `non-scaling-stroke`. */
const CURVE_W = 100, CURVE_H = 100;
/* Der Zaehlbalken darunter belegt das untere Fuenftel. */
const CURVE_BARS = 20;
/* Ab wann ein Tag die Linie traegt. Dieselbe Fuenf, ab der ueberhaupt eine
   Baseline entsteht: ein Tag mit einer einzigen Beobachtung ist kein Preis
   dieses Tages, sondern ein Preis. */
const CURVE_SOLID_N = 5;

function dayStamp(day){
  return new Date(String(day || "") + "T00:00:00").getTime();
}
/* `Number(null)` ist 0 und damit endlich. Wer nur auf `Number.isFinite`
   prueft, liest ein fehlendes Minimum als Preis von null Euro und zieht die
   ganze Achse auf den Nullpunkt. Eine Luecke ist keine Null. */
function finiteNumber(value){
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}
/* Ein Punkt zaehlt nur, wenn alle drei Zahlen und der Tag wirklich Zahlen
   sind. Ein fehlendes Minimum als Null zu lesen zoege die ganze Achse auf
   den Nullpunkt: die Kurve zeigte dann einen Sturz, den es nie gab. Sortiert
   wird selbst, statt sich auf die Reihenfolge der Antwort zu verlassen -
   eine verdrehte Reihenfolge spiegelte die Kurve, ohne dass etwas auffiele. */
function curveDays(points){
  return (points || []).filter(p => p
      && Number.isFinite(dayStamp(p.day))
      && finiteNumber(p.median) !== null
      && finiteNumber(p.min) !== null
      && finiteNumber(p.max) !== null)
    .slice()
    .sort((a, b) => dayStamp(a.day) - dayStamp(b.day));
}
function curveScale(points){
  const rows = curveDays(points);
  if (!rows.length) return null;
  const lows = rows.map(p => Number(p.min));
  const highs = rows.map(p => Number(p.max));
  const lo = Math.min(...lows), hi = Math.max(...highs);
  const first = dayStamp(rows[0].day);
  const last = dayStamp(rows[rows.length - 1].day);
  return {rows, lo, hi, first, last, span: last - first,
          maxN: Math.max(1, ...rows.map(p => Number(p.n) || 0))};
}
function round2(v){ return Math.round(v * 100) / 100; }
function curveX(day, s){
  // Alle Punkte an einem Tag: dann gibt es keine Strecke, nur eine Mitte.
  if (!(s.span > 0)) return CURVE_W / 2;
  return round2((dayStamp(day) - s.first) / s.span * CURVE_W);
}
/* Der Preis waechst nach oben, die Bildkante nach unten. Der Zeichenbereich
   endet ueber den Zaehlbalken, sonst ueberschreibt die Linie sie. */
function curveY(price, s){
  const room = CURVE_H - CURVE_BARS;
  if (!(s.hi > s.lo)) return round2(room / 2);
  return round2(room - (Number(price) - s.lo) / (s.hi - s.lo) * room);
}

/* Ein Tag mit einer Beobachtung sagt weniger als einer mit dreissig. Sichtbar
   wird das zweimal: die duenn belegte Strecke der Linie ist gestrichelt, und
   unter der Kurve steht je Tag ein Balken mit der Anzahl. Ohne das erzaehlt
   eine glatte Linie aus lauter Einzelmessungen eine Sicherheit, die es nicht
   gibt. */
function curveSegments(s){
  const solid = [], thin = [];
  for (let i = 0; i < s.rows.length - 1; i++){
    const a = s.rows[i], b = s.rows[i + 1];
    const line = `M${curveX(a.day, s)} ${curveY(a.median, s)}`
      + `L${curveX(b.day, s)} ${curveY(b.median, s)}`;
    const carries = (Number(a.n) || 0) >= CURVE_SOLID_N
      && (Number(b.n) || 0) >= CURVE_SOLID_N;
    (carries ? solid : thin).push(line);
  }
  return {solid: solid.join(""), thin: thin.join("")};
}
function curveBand(s){
  if (s.rows.length < 2) return "";
  const top = s.rows.map(p => `${curveX(p.day, s)} ${curveY(p.max, s)}`);
  const bottom = s.rows.slice().reverse()
    .map(p => `${curveX(p.day, s)} ${curveY(p.min, s)}`);
  return `M${top.join("L")}L${bottom.join("L")}Z`;
}
function curveCounts(s){
  const step = s.rows.length > 1 ? CURVE_W / (s.rows.length - 1) : CURVE_W;
  const w = round2(Math.min(6, Math.max(0.8, step * 0.5)));
  return s.rows.map(p => {
    const n = Number(p.n) || 0;
    const h = round2(Math.max(n ? 1.5 : 0, n / s.maxN * (CURVE_BARS - 4)));
    const x = round2(Math.min(CURVE_W - w, Math.max(0, curveX(p.day, s) - w / 2)));
    return `<rect class="cbar" x="${x}" y="${round2(CURVE_H - h)}" width="${w}"`
      + ` height="${h}" data-n="${n}"></rect>`;
  }).join("");
}
/* Ohne die Funde ist die Kurve nur huebsch. Sie stehen als senkrechte Marke
   an dem Tag, nach dem gerade gruppiert wird: nach Reisetag am Reisetag, nach
   Beobachtungstag an dem Tag, an dem der Fund entstand. Beides zu mischen
   waere die eine Marke, die immer daneben steht. Ein Fund ausserhalb des
   Fensters faellt weg, statt an den Rand geschoben zu werden. */
function findDay(find, by){
  const f = find || {};
  return by === "travel" ? String(f.travel_date || "")
                         : String(f.created_at || "").slice(0, 10);
}
function curveFinds(finds, s, by){
  return (finds || []).map(f => findDay(f, by)).filter(day => {
    const t = dayStamp(day);
    return Number.isFinite(t) && t >= s.first && t <= s.last;
  }).map(day => {
    const x = curveX(day, s);
    return `<line class="cfind" x1="${x}" y1="0" x2="${x}"`
      + ` y2="${CURVE_H - CURVE_BARS}"></line>`;
  }).join("");
}
/* Ein Bild ohne Text ist fuer einen Screenreader nichts. Der Name nennt
   deshalb, was die Kurve zeigt: Zeitraum, Spanne und wie viele Tage die Linie
   nicht traegt. */
function curveLabel(s){
  const thin = s.rows.filter(p => (Number(p.n) || 0) < CURVE_SOLID_N).length;
  const weak = thin ? `, ${thin} davon mit weniger als ${CURVE_SOLID_N}`
    + " Beobachtungen" : "";
  return `Preisverlauf über ${s.rows.length} ${s.rows.length === 1
    ? "Tag" : "Tage"}${weak}. Die Preise liegen zwischen ${money(s.lo)}`
    + ` und ${money(s.hi)} Euro.`;
}
function curvePath(cls, d){
  return d ? `<path class="${cls}" d="${d}"></path>` : "";
}
/* Ein einziger Tag ergibt keine Linie, sondern eine Marke: die Spanne als
   senkrechter Strich, der Median als Querstrich darauf. Ohne die Spanne
   fiele bei genau einem Tag die halbe Aussage weg, denn `curveBand` braucht
   zwei Punkte. Und die Schwelle gilt hier genauso wie zwischen zwei Tagen:
   die Legende verspricht "gestrichelt heisst wenig Beobachtungen", also darf
   auch die einzelne Marke nicht durchgezogen dastehen, wenn eine einzige
   Beobachtung dahintersteht. */
function curveLoneDay(s){
  const p = s.rows[0];
  const mid = CURVE_W / 2;
  const thin = (Number(p.n) || 0) < CURVE_SOLID_N ? " thin" : "";
  const y = curveY(p.median, s);
  const spread = curveY(p.min, s) === curveY(p.max, s) ? ""
    : `<line class="cband" x1="${mid}" y1="${curveY(p.max, s)}"`
      + ` x2="${mid}" y2="${curveY(p.min, s)}"></line>`;
  return `${spread}<line class="cmed${thin}" x1="${mid - 6}" y1="${y}"`
    + ` x2="${mid + 6}" y2="${y}"></line>`;
}
function historyChart(points, finds, by){
  const s = curveScale(points);
  if (!s) return "";
  const seg = curveSegments(s);
  const dot = s.rows.length === 1 ? curveLoneDay(s) : "";
  return `<svg class="curve" viewBox="0 0 ${CURVE_W} ${CURVE_H}"
      preserveAspectRatio="none" role="img" focusable="false"
      aria-label="${esc(curveLabel(s))}">
    ${curvePath("cband", curveBand(s))}
    ${curvePath("cmed", seg.solid)}
    ${curvePath("cmed thin", seg.thin)}
    ${dot}${curveCounts(s)}${curveFinds(finds, s, by)}
  </svg>`;
}
/* Beschriftet wird ausserhalb des Kastens, sonst zieht die Streckung die
   Schrift mit. Dieselbe Machart wie die Datumsschiene ueber der Tabelle. */
function curveAxis(points){
  const s = curveScale(points);
  if (!s) return {days: "", prices: ""};
  const last = s.rows[s.rows.length - 1];
  const ends = s.rows.length > 1 ? [s.rows[0], last] : [s.rows[0]];
  const days = ends.map((p, i) => {
    const align = ends.length === 1 ? "translateX(-50%)"
      : (i === 0 ? "translateX(0)" : "translateX(-100%)");
    return `<span style="left:${curveX(p.day, s)}%;transform:${align}">${
      esc(fmtDay(p.day))}</span>`;
  }).join("");
  const prices = `<span class="chi">${money(s.hi)} €</span>`
    + `<span class="clo">${money(s.lo)} €</span>`;
  return {days, prices};
}
function historyHeadline(body){
  const b = body || {};
  const points = curveDays(b.points);
  const span = b.by === "travel" ? "nach Reisetag" : "nach Beobachtungstag";
  if (!points.length) return `${b.route || ""}: für die letzten ${
    Number(b.days || 0)} Tage ist nichts aufgezeichnet.`;
  const obs = points.reduce((sum, p) => sum + (Number(p.n) || 0), 0);
  const days = points.length === 1 ? "1 Tag" : `${points.length} Tage`;
  const thin = points.filter(p => (Number(p.n) || 0) < CURVE_SOLID_N).length;
  const weak = thin
    ? ` An ${thin === 1 ? "einem Tag" : `${thin} Tagen`} stehen weniger als ${
        CURVE_SOLID_N} Beobachtungen dahinter; dort ist die Linie gestrichelt.`
    : "";
  return `${b.route || ""} ${span}: ${days}, ${
    obs.toLocaleString("de-DE")} Beobachtungen.${weak}`;
}
/* Was der Takt dieser Strecke ist, gehoert an die Kurve: die Dichte der
   Punkte ist seine unmittelbare Folge. */
function historyCadenceLine(body){
  const b = body || {};
  if (!b.cadence) return "Diese Strecke steht nicht in der Beobachtungsliste."
    + " Aufgezeichnet wird nur, was eine gewöhnliche Suche nebenbei mitschreibt.";
  const stats = b.stats || {};
  const ready = stats.ready
    ? `${Number(stats.days_recorded || 0)} Tage aufgezeichnet, die Basis trägt`
    : `noch ${Math.max(0, Number(stats.min_days || 5)
        - Number(stats.days_recorded || 0))} von ${
        Number(stats.min_days || 5)} Tagen bis zur ersten Aussage`;
  return `Takt ${b.hot ? "heiß" : "täglich"}, ${ready}.`;
}

function renderHistory(body){
  const b = body || {};
  const box = $("#huntcurve");
  box.hidden = false;
  $("#curvehead").textContent = historyHeadline(b);
  $("#curvecadence").textContent = historyCadenceLine(b);
  const axis = curveAxis(b.points);
  const chart = historyChart(b.points, b.finds, b.by);
  /* Ein leerer Kasten mit Achsen sieht aus wie eine kaputte Kurve. Ohne einen
     einzigen Punkt gibt es keinen Kasten, nur den Satz darueber. */
  $("#curvefield").hidden = !chart;
  $("#curvebox").innerHTML = chart;
  $("#curvedays").innerHTML = axis.days;
  $("#curveprices").innerHTML = axis.prices;
  return b;
}

let historyRoute = null;
async function loadHistory(origin, destination){
  const msg = $("#huntmsg");
  const from = String(origin || "").trim(), to = String(destination || "").trim();
  if (!from || !to){
    msg.dataset.tone = "err";
    msg.textContent = "Zu diesem Fund fehlt die Strecke.";
    return null;
  }
  msg.dataset.tone = "";
  msg.textContent = "Lade Verlauf";
  historyRoute = {origin: from, destination: to};
  try {
    const by = $("#curveBy").value || "observed";
    const days = Number($("#curveDays").value || 30);
    const r = await fetch(`/api/hunt/history/${encodeURIComponent(from)}/`
      + `${encodeURIComponent(to)}?days=${days}&by=${by}`);
    const d = await r.json();
    if (!r.ok) throw new Error(detail(d.detail) || "Der Verlauf ist nicht abrufbar");
    msg.dataset.tone = "";
    msg.textContent = "";
    return renderHistory(d);
  } catch (err) {
    msg.dataset.tone = "err";
    msg.textContent = err.message || "Der Verlauf ist nicht abrufbar";
    return null;
  }
}
function reloadHistory(){
  if (!historyRoute) return null;
  return loadHistory(historyRoute.origin, historyRoute.destination);
}

/* ---------------- Zustand der Jagd ---------------- */
/* Der Takt und seine Sicherungen stehen dort, wo der Takt gestellt wird. Wer
   sich fragt, warum eine heisse Strecke nicht alle zwanzig Minuten laeuft,
   sucht bei dem Schalter, der ihm "heiss" versprochen hat. */
function pauseLine(hunt){
  const h = hunt || {};
  const paused = h.paused || [];
  if (paused.length){
    const names = paused.map(p => String((p || {}).source || "")).filter(Boolean);
    const until = paused.map(p => (p || {}).until).filter(Boolean).sort().pop();
    return `Die Jagd läuft im Tagestakt: bei ${names.join(", ")} ist eine`
      + ` Sicherung offen${until ? `, bis ${scanTime(until)}` : ""}. Aufgezeichnet`
      + ` wird weiter, nur nicht alle 20 Minuten.`;
  }
  const want = Number(h.interval_seconds || 0);
  const real = Number(h.effective_interval_seconds || 0);
  if (want && real && real > want){
    return `Der heiße Takt ist auf ${Math.round(want / 60)} Minuten ausgelegt,`
      + ` der Planer ruft die Jagd aber nur alle ${Math.round(real / 60)} Minuten`
      + ` auf. Schneller als das wird sie nicht.`;
  }
  return "";
}
async function loadHuntHealth(){
  try {
    const d = await (await fetch("/api/health/detail")).json();
    const hunt = (d && d.hunt) || {};
    $("#watchpause").dataset.tone = (hunt.paused || []).length ? "warn" : "";
    $("#watchpause").textContent = pauseLine(hunt);
    return hunt;
  } catch {
    $("#watchpause").dataset.tone = "";
    $("#watchpause").textContent = "";
    return null;
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
  updateRouteGlance();
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
$("#resetfilters").onclick = resetResultFilters;
$("#saveProfile").onclick = saveProfileNow;
$("#profileName").onkeydown = profileNameKeydown;
$("#watchAdd").onclick = addWatchRoute;
$("#watchRun").onclick = runWatchlistNow;
$("#huntRun").onclick = runHuntNow;
$("#huntOpenOnly").onchange = loadHunt;
["#curveBy", "#curveDays"].forEach(s => $(s).onchange = reloadHistory);
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
loadWatchlist();
loadHunt();
loadHuntHealth();
$("#cancel").onclick = cancelSearch;
updateRouteGlance();
