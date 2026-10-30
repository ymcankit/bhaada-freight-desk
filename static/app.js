/* Bhaada console. Polls /api/state and renders it. All text from brokers and owners goes in as text, never as HTML. */
"use strict";
const S = { data: null, view: "desk", sel: null, tab: "story", chat: [], draft: {}, costsFilled: false, busy: false };
const $ = (id) => document.getElementById(id);
const NS = "http://www.w3.org/2000/svg";

function el(tag, attrs = {}, ...kids) {
  const n = tag.startsWith("svg:") ? document.createElementNS(NS, tag.slice(4)) : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") n.setAttribute("class", v);
    else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined && k !== false) n.append(k.nodeType ? k : document.createTextNode(String(k)));
  return n;
}
const rs = (n) => "₹" + Math.round(n).toLocaleString("en-IN");
const clock = (t) => new Date(t * 1000).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: "Asia/Kolkata" });
const mmss = (s) => { s = Math.max(0, Math.round(s)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
function ago(t) {
  const s = S.data.now - t;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  return clock(t).slice(0, 5);
}
// The server may ask for a console password (CONSOLE_PASSWORD); it is asked once and kept in this browser.
let KEY = "";
try { KEY = localStorage.getItem("bhaada-console-key") || ""; } catch (e) {}
function askKey() {
  const k = window.prompt("Console password");
  if (k === null) return false;
  KEY = k.trim();
  try { localStorage.setItem("bhaada-console-key", KEY); } catch (e) {}
  return true;
}
async function api(path, body, retry = true) {
  const headers = KEY ? { "X-Console-Key": KEY } : {};
  const r = await fetch(path, body === undefined ? { headers } : { method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (r.status === 401 && retry && askKey()) return api(path, body, false);
  if (!r.ok) throw new Error(j.detail || `Request failed (${r.status})`);
  return j;
}

const STATUS = {
  routing: ["Finding the route", "neutral"], sourcing: ["Waiting on owners", "neutral"],
  waiting_trucks: ["Waiting for a truck", "wait"], needs_call: ["Needs your call", "wait"], needs_approval: ["Needs your approval", "wait"],
  holding: ["Waiting for the hold", "neutral"], booked: ["Booking-ready", "ok"], recovered: ["Re-booked", "ok"], lost: ["Shipper said no", "neutral"],
  escalated: ["Escalated to you", "bad"], rejected: ["Rejected", "neutral"],
};
function statusOf(l) {
  if (l.status === "sourcing" && l.phase === "replan") return ["Re-planning", "wait"];
  return STATUS[l.status] || [l.status, "neutral"];
}
const THREAD = { sent: "Waiting", countered: "Countered", deal: "Agreed", declined: "Declined", silent: "No reply",
  hold_requested: "Asked to hold", held: "Holding", reconfirm: "Re-confirming", backed_out: "Backed out", released: "Released" };

// ---------------- polling ----------------
// Redraw only when the desk changed, so focus, hover and clicks survive; countdowns tick in place.
function nowEst() { return S.data.now + (Date.now() / 1000 - S.fetchedAt); }
function cd(deadline) { return el("span", { class: "cd", "data-deadline": deadline || 0, text: mmss((deadline || 0) - nowEst()) }); }
function tickCountdowns() {
  if (!S.data) return;
  document.querySelectorAll(".cd").forEach((n) => { n.textContent = mmss(+n.dataset.deadline - nowEst()); });
  document.querySelectorAll(".sw-live").forEach((n) => { n.textContent = mmss(nowEst() - +n.dataset.start); });
}
setInterval(tickCountdowns, 1000);

async function poll() {
  try {
    const d = await api("/api/state");
    if (d.locked) {
      if (!S.askedKey) { S.askedKey = true; if (askKey()) return poll(); }
      $("finding").textContent = "This console is locked. Reload the page and enter the console password.";
      return;
    }
    S.askedKey = false;
    S.fetchedAt = Date.now() / 1000;
    const key = JSON.stringify({ ...d, now: 0 });
    S.data = d;
    if (key === S.lastKey) return tickCountdowns();
    S.lastKey = key;
    render();
  } catch (e) {
    $("finding").textContent = "Can't reach the desk server. Retrying…";
  }
}
setInterval(poll, 1500);
poll();

function render() {
  const d = S.data;
  $("scope").textContent = (d.settings.mode === "live" ? "Live owners" : "Sandbox owners") + (d.readOnly ? " · view only" : d.publicView ? " · open demo" : "");
  $("dot-wa").classList.toggle("on", !!(d.whatsapp || d.telegram));
  $("conn-wa").textContent = [d.telegram ? `Telegram @${d.telegram}` : null, d.whatsapp ? "WhatsApp connected" : "WhatsApp not connected"].filter(Boolean).join(" · ");
  $("dot-llm").classList.toggle("on", d.llm);
  $("conn-llm").textContent = d.llm ? "Language model on" : "Language model off";
  $("agents-on").hidden = !(d.llm && d.settings.agentMode);
  for (const m of ["live", "sandbox"]) $("mode-" + m).setAttribute("aria-pressed", String(d.settings.mode === m));
  $("sandbox-banner").hidden = d.settings.mode !== "sandbox";
  const waiting = d.kpis.waiting;
  $("nav-waiting").hidden = !waiting;
  $("nav-waiting").textContent = waiting;
  $("nav-waiting").setAttribute("aria-label", `${waiting} waiting for approval`);
  const load = S.view === "desk" && S.sel ? d.loads.find((l) => l.id === S.sel) : null;
  if (S.view === "desk" && S.sel && !load) S.sel = null;            // the load left the list (archived, or another mode)
  $("view-desk").hidden = !(S.view === "desk" && !load);
  $("view-load").hidden = !load;
  crumb(load);
  if (S.view === "desk") load ? renderLoad(load) : renderDesk();
  if (S.view === "owners") renderOwners();
  if (S.view === "costs") renderCosts();
  lockDemo(d);                                      // after the views, so their own enabling never unlocks a control
}

// The open demo console: judges start and approve their own loads; settings, owners and the mode are the broker's.
function lockDemo(d) {
  const locked = !!(d.publicView || d.readOnly);
  document.body.classList.toggle("demo-locked", locked);
  $("demo-note").hidden = !locked;
  for (const m of ["live", "sandbox"]) $("mode-" + m).disabled = locked;
  document.querySelector(".mode").title = locked ? "The broker switches this. Your loads always use demo owners, so you can try every step." : "";
  $("send-checkin").disabled = $("send-checkin").disabled || locked;
  if (locked) $("send-checkin").title = "The broker sends the morning check-in";
  for (const id of ["owner-form", "cost-form"]) for (const n of $(id).elements) n.disabled = locked;
  for (const id of ["owner-lock", "cost-lock"]) $(id).hidden = !locked;
}

function crumb(load) {
  const names = { desk: "Live desk", owners: "Owners and pool", costs: "Pricing defaults" };
  $("crumb").replaceChildren(...(load
    ? [el("button", { onclick: () => select(null), text: "Live desk" }), el("span", { text: "›", "aria-hidden": "true" }), el("b", { text: load.id })]
    : [el("b", { text: names[S.view] })]));
}

// ---------------- icons (fixed strings, never data) ----------------
const ICON = {
  coord: '<path d="M4 12h4l3-7 3 14 3-7h3"/>', search: '<circle cx="11" cy="11" r="6"/><path d="m20 20-4-4"/>',
  swap: '<path d="M7 11 3 7l4-4M3 7h12M17 13l4 4-4 4M21 17H9"/>', code: '<path d="m8 8-4 4 4 4M16 8l4 4-4 4"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21c1-4 4-6 8-6s7 2 8 6"/>', read: '<path d="M4 5h16v11H8l-4 4z"/>',
  truck: '<path d="M2 16V7h11v9M13 10h4l3 3v3h-7"/><circle cx="6" cy="17.5" r="1.8"/><circle cx="17" cy="17.5" r="1.8"/>',
  flag: '<path d="M5 21V4h11l-2 4 2 4H5"/>', clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  rupee: '<path d="M6 4h12M6 9h12M10 4c5 0 5 10 0 10H7l8 6"/>', up: '<path d="M4 18 10 11l4 4 6-8"/><path d="M15 7h5v5"/>',
  wait: '<path d="M7 3h10M7 21h10M8 3c0 5 8 5 8 9s-8 4-8 9M16 3c0 5-8 5-8 9"/>', check: '<path d="m5 12 5 5 9-10"/>',
  box: '<path d="M12 3 4 7v10l8 4 8-4V7z"/><path d="m4 7 8 4 8-4M12 11v10"/>', cal: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>',
  arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>', chart: '<path d="M4 19V5M4 19h16M8 15l3-4 3 2 5-6"/>',
};
function icon(name) {
  const s = document.createElementNS(NS, "svg");
  s.setAttribute("class", "i"); s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("aria-hidden", "true");
  s.innerHTML = ICON[name];
  return s;
}
const hm = (t) => clock(t).slice(0, 5);
const total = (n) => rs(n);
function tile(ic, value, label, key) {
  return el("div", { class: key ? "tile key" : "tile" }, el("span", { class: "ic" }, icon(ic)),
    el("div", {}, value === null ? el("b", { class: "dash", text: "–" }) : Array.isArray(value) ? el("b", {}, String(value[0]), el("small", { text: value[1] })) : el("b", { text: String(value) }),
      el("span", { class: "l", text: label })));
}

// ---------------- desk: today's loads ----------------
function renderDesk() {
  const d = S.data, k = d.kpis, med = medianQuoteMin(d);
  renderFinding();
  const t = [
    tile("wait", k.waiting, k.waiting === 1 ? "Waiting on you" : "Waiting on you", k.waiting > 0),
    tile("truck", k.received, `Loads today · ${k.quoted} quoted`),
    tile("check", k.booked, "Booking-ready"),
    tile("clock", med === null ? null : [med, "min"], "Median time to quote", k.waiting === 0 && med !== null),
    tile("up", k.avgMargin === null ? null : [k.avgMargin, "%"], "Average margin booked"),
  ];
  if (k.shadowWithin3Pct != null) t.push(tile("rupee", [k.shadowWithin3Pct, `% of ${k.shadowCompared}`], "Quotes within ±3% of the broker's own"));
  $("kpis").className = "tiles"; $("kpis").replaceChildren(...t);

  const loads = d.loads;
  $("list-count").textContent = loads.length ? `${loads.length} load${loads.length === 1 ? "" : "s"}` : "";
  if (!loads.length) {
    $("loads").replaceChildren(el("tr", { class: "list-empty" }, el("td", { colspan: 7, text: "No loads yet today. Start one with New load, or send one to the desk's bot." })));
    return;
  }
  $("loads").replaceChildren(...loads.map((l) => {
    const [word, cls] = statusOf(l);
    const f = l.load, lane = l.lane || {};
    let sub = null;
    if (l.status === "sourcing") {
      const open = Object.values(l.threads).filter((x) => ["sent", "countered", "reconfirm"].includes(x.status)).length;
      sub = `${open} owner${open === 1 ? "" : "s"} to reply`;
    } else if (l.status === "waiting_trucks") sub = `kept open till ${hm(l.watchUntil)}`;
    else if (!l.quote && l.reason) sub = l.reason;
    const goods = f.material && f.material !== "general" ? ` ${f.material}` : "";
    return el("tr", { class: l.status === "needs_approval" || l.status === "needs_call" ? "urgent" : null, onclick: () => select(l.id) },
      el("td", { class: "lid c-id", text: l.id }),
      el("td", { class: "route c-route" }, el("button", { onclick: (e) => { e.stopPropagation(); select(l.id); }, text: `${lane.origin || f.origin} → ${lane.destination || f.destination}` }),
        lane.distanceKm ? el("span", { class: "sub", text: `${lane.distanceKm} km` }) : null),
      el("td", { class: "c-truck" }, `${f.weight_ton} T${goods}`, el("span", { class: "sub", text: l.searchType || f.truck_type })),
      el("td", { class: "c-pickup", text: f.pickup_when || "kal" }),
      el("td", { class: "r money c-quote" }, l.quote ? [el("b", { text: rs(l.quote.sell_total) }), el("small", { text: `${l.quote.margin_pct}% margin` })] : el("span", { class: "muted", text: "–" })),
      el("td", { class: "c-status" }, el("span", { class: `pill ${cls}`, text: word }), sub ? el("span", { class: "sub", text: sub }) : null),
      el("td", { class: "r muted c-when", text: ago(l.createdAt) }));
  }));
}

function renderFinding() {
  const loads = S.data.loads, f = $("finding");
  const wait = loads.find((l) => l.status === "needs_approval");
  const call = loads.find((l) => l.status === "needs_call");
  const re = loads.find((l) => l.status === "sourcing" && l.phase === "replan");
  const src = loads.find((l) => l.status === "sourcing");
  const esc = loads.find((l) => l.status === "escalated" && S.data.now - (l.closedAt || 0) < 1800);
  const open = (l, text) => el("a", { href: `#${l.id}`, onclick: (e) => { e.preventDefault(); select(l.id); }, text });
  let parts;
  if (wait) parts = [el("b", { text: `${wait.id} needs your approval: ` }), `${wait.lane.origin} → ${wait.lane.destination} at ${rs(wait.quote.sell_total)}, margin ${wait.quote.margin_pct}%. `, open(wait, "Open it")];
  else if (call) parts = [el("b", { text: `${call.id} needs your call: ` }), "no owner cleared your margin floor. ", open(call, "See the options")];
  else if (re) parts = [el("b", { text: `${re.id}: an owner backed out. ` }), "Re-booking at the shipper's fixed price; nothing needed from you unless the floor is at risk."];
  else if (src) parts = [el("b", { text: `${src.id}: ` }), `messaging owners near ${src.lane ? src.lane.origin : src.load.origin}, `, cd(src.deadline), " left in this round. ", open(src, "Watch it")];
  else if (loads.some((l) => l.status === "waiting_trucks")) {
    const w = loads.filter((l) => l.status === "waiting_trucks");
    parts = [el("b", { text: `${w.map((l) => l.id).join(", ")}: ` }), "waiting for a truck to check in. The broker was told; nothing needed from you yet."];
  }
  else if (esc) parts = [el("b", { text: `${esc.id} was handed to you: ` }), esc.reason];
  else if (loads.length) parts = ["Nothing is waiting on you. ", `${S.data.kpis.booked} booking-ready today.`];
  else parts = [S.data.settings.mode === "live" && !S.data.liveOwners ? "No live owners yet: add them under Owners and pool, then send the morning check-in." : "No loads yet today. Start one with New load."];
  f.replaceChildren(...parts);
}

function select(id) {
  S.sel = id; S.tab = "story";
  try { history.replaceState(null, "", id ? `#${id}` : location.pathname); } catch (e) {}
  render();
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", () => { const id = location.hash.slice(1); if (/^BH-\d+$/.test(id) && id !== S.sel) { S.view = "desk"; select(id); } });

// ---------------- one load ----------------
const AGENT_WHO = ["Coordinator agent", "Sourcing agent", "Negotiation agent", "Booking agent"];
function renderLoad(l) {
  const f = l.load, lane = l.lane || {}, q = l.quote;
  const [word, cls] = statusOf(l);
  const goods = f.material && f.material !== "general" ? ` ${f.material}` : "";
  const head = el("div", { class: "head" }, el("div", {},
    el("h1", {}, `${lane.origin || f.origin} → ${lane.destination || f.destination}`, el("span", { class: `pill big ${cls}`, text: word })),
    el("div", { class: "facts" },
      el("span", { class: "id", text: l.id }),
      lane.distanceKm ? el("span", {}, icon("arrow"), `${lane.distanceKm} km by road`) : null,
      el("span", {}, icon("box"), `${f.weight_ton} T${goods}`),
      el("span", {}, icon("truck"), l.searchType || f.truck_type),
      el("span", {}, icon("cal"), `Pickup ${f.pickup_when || "kal"}`),
      f.budget_rs ? el("span", {}, icon("rupee"), `Shipper budget ${rs(f.budget_rs)}`) : null,
      el("span", { class: "muted", text: l.source === "telegram" ? "from Telegram" : l.source === "whatsapp" ? "from WhatsApp" : "from console" }),
      l.aiBroker ? el("span", { class: "muted", text: "AI-broker demo" }) : null)));

  const story = storyOf(l);
  const threads = Object.values(l.threads);
  const agreed = threads.filter((t) => t.agreedBuy);
  const agents = new Set(story.filter((s) => s.by === "AI" && AGENT_WHO.includes(s.who)).map((s) => s.who)).size;
  const secs = l.quoteAt ? Math.round(l.quoteAt - l.createdAt) : null;
  const tiles = el("div", { class: "tiles four" },
    secs !== null ? tile("clock", secs < 120 ? [secs, " sec"] : [Math.round(secs / 6) / 10, " min"], agents ? `Load to quote, ${agents} AI agent${agents === 1 ? "" : "s"}` : "Load to quote", true)
      : el("div", { class: "tile key" }, el("span", { class: "ic" }, icon("clock")), el("div", {}, el("b", { class: "sw-live", "data-start": l.createdAt, text: mmss(nowEst() - l.createdAt) }), el("span", { class: "l", text: "Open for" }))),
    tile("truck", threads.length ? `${agreed.length} of ${threads.length}` : null, "Owners agreed"),
    tile("rupee", agreed.length ? rs(Math.min(...agreed.map((t) => t.agreedBuy))) : null, "Best owner rate"),
    tile("up", q ? [q.margin_pct, "%"] : null, q ? `Your margin, ${rs(q.margin_rs)}` : "Your margin"));

  const tabs = [["story", "How the agents worked it", null], ["chats", "Owner chats", threads.length], ["log", "Full log", l.events.length]];
  const tab = S.tab || "story";
  const main = el("div", { class: "card" },
    el("div", { class: "tabs", role: "tablist" }, ...tabs.map(([k, label, n]) => el("button", { role: "tab", "aria-selected": String(tab === k),
      onclick: () => { S.tab = k; renderLoad(l); } }, label, n ? el("span", { class: "count", text: String(n) }) : null))),
    tab === "story" ? storyList(l, story) : tab === "chats" ? chats(l, threads) : log(l));

  $("view-load").replaceChildren(head, stepper(l), tiles,
    el("div", { class: "load-grid" },
      el("div", {}, main, threads.length ? ownersTable(l, threads) : null),
      el("aside", { class: "rail-col", "aria-label": "Quote and lane" }, ...railCards(l))));
}

function stepper(l) {
  const st = l.status, ev = l.events, threads = Object.values(l.threads);
  const sourced = ev.find((e) => e.actor === "Sourcing" && /^Messaged /.test(e.text));
  const agreed = threads.filter((t) => t.agreedBuy).length;
  const approved = ["holding", "booked", "recovered", "lost"].includes(st) || ev.some((e) => e.text === "Approved the quote.");
  const booked = ["booked", "recovered"].includes(st);
  const src = l.source === "telegram" ? "Telegram" : l.source === "whatsapp" ? "WhatsApp" : "console";
  const steps = [
    ["Load received", `${hm(l.createdAt)} · from ${src}`, "done"],
    ["Owners sourced", sourced ? `${hm(sourced.t)} · ${threads.length} messaged` : st === "waiting_trucks" ? "Waiting for a truck" : "Finding owners", sourced ? "done" : "now"],
    ["Negotiated", l.quoteAt ? `${hm(l.quoteAt)} · ${agreed} agreed` : sourced ? `${agreed} agreed so far` : "", l.quoteAt ? "done" : sourced ? "now" : "next"],
    ["Your approval", approved ? "Approved" : st === "needs_approval" ? "Quote ready" : st === "needs_call" ? "Needs your call" : "", approved ? "done" : ["needs_approval", "needs_call"].includes(st) ? "now" : "next"],
    ["Booking", booked ? (l.bookedAt ? `${hm(l.bookedAt)} · truck confirmed` : "Booked") : st === "holding" ? "Owner holding the truck" : "Truck held after approval", booked ? "done" : st === "holding" ? "now" : "next"],
  ];
  const stop = { escalated: "Handed to you", rejected: "Rejected", lost: "Shipper said no" }[st];
  if (stop) { const i = steps.findIndex((s) => s[2] !== "done"); if (i >= 0) { steps[i][1] = stop; steps[i][2] = "stop"; for (let j = i + 1; j < steps.length; j++) steps[j][1] = ""; } }
  const box = el("div", { class: "card steps", "aria-label": "Load progress" });
  steps.forEach(([label, sub, state], i) => {
    if (i) box.append(el("span", { class: state === "done" ? "bar done" : "bar" }));
    box.append(el("div", { class: "step" },
      el("span", { class: `dot ${state}` }, state === "done" ? icon("check") : state === "stop" ? "!" : String(i + 1)),
      el("div", {}, el("b", { text: label }), el("small", { text: sub || " " }))));
  });
  return box;
}

// The agents' story: the load's events, read into steps, each marked AI, Code or Human (owners and the broker).
function storyOf(l) {
  const steps = [], ev = l.events;
  const why = (x) => { const m = /\. Why: (.*)$/.exec(x); return m ? m[1] : ""; };
  const sandboxOwner = (name) => (Object.values(l.threads).find((t) => t.name === name) || {}).sandbox;
  const group = (kind, t, base, name, extra) => {
    const last = steps[steps.length - 1];
    if (last && last.kind === kind && t - last.t < 30) { if (!last.names.includes(name)) last.names.push(name); Object.assign(last.vals, extra || {}); return last; }
    const s = { kind, t, names: [name], vals: { ...(extra || {}) }, ...base };
    steps.push(s);
    return s;
  };
  for (const e of ev) {
    const x = e.text, by = byOf(e);
    let m;
    if (e.actor === "Intake" && by === "AI" && !steps.length) steps.push({ t: e.t, who: "Intake", by, icon: "read", did: `Read the broker's message: ${x}.` });
    else if (DECIDER(e)) {
      const s = stratOf(e);
      if (/^\(AI\) plan: /.test(x)) steps.push({ t: e.t, who: "Coordinator agent", by: "AI", icon: "coord",
        did: "Planned the load: " + x.replace(/^\(AI\) plan: /, "").replace(/\. Why: .*$/, "").replace(/\.$/, "") + ".", say: why(x), opts: e.moves, chosen: e.chosen });
      else if (/^\(AI\) winner: /.test(x)) steps.push({ t: e.t, who: "Coordinator agent", by: "AI", icon: "coord",
        did: "Chose " + x.replace(/^\(AI\) winner: /, "").replace(/\. Why: .*$/, "").replace(/\.$/, "") + "; the others stay as backups.", say: why(x) });
      else steps.push({ t: e.t, who: e.actor === "Coordinator" ? "Coordinator agent" : "Strategist", by: e.actor === "Coordinator" || by === "AI" ? by : "Code", icon: "coord",
        did: `No owner cleared the margin floor. Next move: ${(MOVE[s.chosen] || s.chosen || "").toLowerCase()}.`, say: s.why, opts: s.moves, chosen: s.chosen });
    }
    else if (e.actor === "Sourcing" && (m = /^Sourcing agent picked (\d+) of (\d+) owners:? ?(.*)$/.exec(x)))
      steps.push({ t: e.t, who: "Sourcing agent", by: "AI", icon: "search", did: `Picked ${m[1]} of the ${m[2]} best-ranked owners to message.`, say: m[3] });
    else if (e.actor === "Sourcing" && /^Messaged /.test(x)) {
      const last = steps[steps.length - 1], short = x.replace(/ Waiting up to.*$/, "");
      if (last && last.who === "Sourcing agent" && e.t - last.t < 10) last.did = last.did.replace(/ to message\.$/, ", and messaged them at once.");
      else steps.push({ t: e.t, who: "Sourcing", by: "Code", icon: "search", did: short });
    }
    else if (e.actor === "Sourcing" && /^No .* within/.test(x)) steps.push({ t: e.t, who: "Sourcing", by: "Code", icon: "search", did: x });
    else if (e.actor === "Negotiation") {
      if ((m = /^Negotiation agent chose to (\w+) with (.+?)(?:: “(.*)”)?\.?$/.exec(x))) {
        const s = group("agent-" + m[1], e.t, { who: "Negotiation agent", by: "AI", icon: "swap" }, m[2]);
        if (m[3] && !s.say) { s.say = m[3]; s.sayTo = m[2]; }
      } else if ((m = /^Countered (.+?): ₹(.+?)\/km → ₹(.+?)\/km\.$/.exec(x))) {
        const last = steps[steps.length - 1];
        if (last && last.kind === "agent-counter" && last.names.includes(m[1])) { last.vals[m[1]] = m[3]; last.asks = { ...(last.asks || {}), [m[1]]: m[2] }; }
        else group("rules-counter", e.t, { who: "Negotiation", by: "Code", icon: "swap" }, m[1], { [m[1]]: m[3] });
      } else if ((m = /^(.+?) agreed at ₹(.+?)\/km \(₹(.+?)\)\.$/.exec(x))) {
        const acc = steps.find((s) => s.kind === "agent-accept" && s.names.includes(m[1]) && e.t - s.t < 5);
        if (acc) acc.vals[m[1]] = m[3];
        else group("agreed", e.t, { who: "Owners", by: sandboxOwner(m[1]) ? "Demo" : "Human", icon: "truck" }, m[1], { [m[1]]: m[3] });
      } else if (/ is above the ceiling| Walked away/.test(x)) group("declined", e.t, { who: "Negotiation", by: "Code", icon: "swap" }, x.split(" is above")[0]);
      else if (/ asked \d+% advance/.test(x)) steps.push({ t: e.t, who: "Negotiation", by: "Code", icon: "swap", did: x });
    }
    else if (e.actor === "Pricing") steps.push({ t: e.t, who: "Pricing", by: "Code", icon: "code", did: `${x} Code sets every price; agents can't.` });
    else if (e.actor === "Broker") steps.push({ t: e.t, who: l.aiBroker ? "Demo broker" : "Broker", by, icon: "user", did: x });
    else if (e.actor === "Booking") steps.push({ t: e.t, who: /^Booking agent/.test(x) ? "Booking agent" : "Booking", by, icon: "truck", did: x });
    else if (e.actor === "Orchestrator" && /^(Escalated|Quote expired|Warning|.+ dropped out)/.test(x)) steps.push({ t: e.t, who: "Desk", by: "Code", icon: "flag", did: x });
  }
  const list = (s) => { const n = s.names; return n.length <= 2 ? n.join(" and ") : `${n.slice(0, -1).join(", ")} and ${n[n.length - 1]}`; };
  const same = (vals) => { const v = Object.values(vals); return v.length && v.every((a) => a === v[0]) ? v[0] : null; };
  for (const s of steps) {
    if (!s.kind) continue;
    const one = same(s.vals);
    if (s.kind === "agent-accept") s.did = `Accepted the ask of ${list(s)}` + (one ? ` (₹${one}).` : ".");
    else if (s.kind === "agent-counter") s.did = `Countered ${list(s)}` + (one ? ` at ₹${one}/km.` : ".");
    else if (s.kind === "agent-final_offer") s.did = `Made a last offer to ${list(s)}.`;
    else if (s.kind === "agent-decline") s.did = `Walked away from ${list(s)}: above the broker's limit.`;
    else if (s.kind === "agent-ask") s.did = `Asked ${list(s)} for a rate.`;
    else if (s.kind === "rules-counter") s.did = `Countered ${list(s)}` + (one ? ` at ₹${one}/km` : "") + " (the rules' concession ladder).";
    else if (s.kind === "agreed") s.did = s.names.length === 1 ? `${s.names[0]} agreed at ₹${s.vals[s.names[0]]}.` : `${list(s)} agreed` + (one ? ` at ₹${one} each.` : ".");
    else if (s.kind === "declined") s.did = `${list(s)} stayed above the broker's limit; walked away.`;
    if (s.say && s.sayTo && s.names.length > 1) s.sayNote = `to ${s.sayTo}` + (s.asks && s.asks[s.sayTo] ? `, who had asked ₹${s.asks[s.sayTo]}/km` : "");
  }
  if (l.status === "needs_approval") {
    const w = l.threads[l.winner];
    steps.push({ now: true, who: "You", by: "Human", icon: "user", did: `Approve to hold ${w ? w.name.split(" ")[0] : "the owner"}'s truck, or reject.` });
  } else if (l.status === "needs_call") steps.push({ now: true, who: "You", by: "Human", icon: "user", did: "Decide on the desk's proposal: quote at that rate, or take the load yourself." });
  return steps;
}

function storyList(l, steps) {
  if (!steps.length) return el("p", { class: "story-empty", text: "The agents' steps show here as the load moves." });
  return el("ol", { class: "story" }, ...steps.map((s) => el("li", {},
    el("time", { text: s.now ? "now" : clock(s.t) }),
    el("span", { class: `node ${s.by.toLowerCase()}` }, icon(s.icon)),
    el("div", {},
      el("div", { class: "who-row" }, el("b", { text: s.who }), s.who === "You" ? null : el("span", { class: `tag ${s.by.toLowerCase()}`, title: BY_TITLE[s.by] || null, text: s.by === "Human" ? (s.who === "Owners" ? "Owner" : "Human") : s.by })),
      el("p", { class: "did", text: s.did }),
      s.say ? el("p", { class: "say" }, `“${s.say}”`, s.sayNote ? el("small", { text: s.sayNote }) : null) : null,
      s.opts && Object.keys(s.opts).length > 1 ? el("ul", { class: "opts", "aria-label": "Options the code offered" }, ...Object.entries(s.opts).map(([k, v]) =>
        el("li", { class: k === s.chosen ? "chosen" : null }, el("b", { text: (k === s.chosen ? "Chosen · " : "") + (MOVE[k] || String(v).split(":")[0]) }), el("span", { text: String(v) })))) : null))));
}

function chats(l, threads) {
  if (!threads.length) return el("p", { class: "story-empty", text: "No owner has been messaged yet." });
  return el("div", { class: "chats" }, ...threads.map((t) => el("div", { class: "chat-owner" },
    el("h3", {}, t.name, el("span", { text: THREAD[t.status] || t.status })),
    el("div", { class: "bubbles" }, ...t.messages.map((m) => el("div", { class: `msg ${m.dir}` }, m.text, el("time", { text: `${m.dir === "out" ? "Bhaada" : t.name.split(" ")[0]} · ${clock(m.t)}` })))))));
}

// Who decided each step: the server marks it (e.by); older saved events are read from their actor and text.
const BY_TITLE = { AI: "A language model read or chose this; code checked it", Code: "Decided by code: pricing, guardrails, routing, timers, fixed rules",
  Human: "A person: the broker or an owner", Demo: "A simulated demo owner or broker (rule-based, not AI)" };
function byOf(e) {
  if (e.by) return e.by;
  if (e.actor === "Broker") return "Human";
  if (e.actor === "Strategist") return e.text.startsWith("(AI)") ? "AI" : "Code";
  if (e.actor === "Intake") return e.text.startsWith("Budget") ? "Code" : "AI";
  if (/ (cancelled|backed out|confirmed the|is no longer free)|^Dispatch:/.test(e.text)) return "Human";
  return "Code";
}
function log(l) {
  return el("ol", { class: "log" }, ...l.events.map((e) => el("li", {}, el("time", { text: clock(e.t) }), el("span", { class: "actor", text: e.actor }),
    el("span", {}, el("span", { class: `tag ${byOf(e).toLowerCase()}`, title: BY_TITLE[byOf(e)] || null, text: byOf(e) })), el("span", { text: e.text }))));
}
const MOVE = { widen: "Widen the search", bigger_truck: "Bigger truck", final_offer: "Final offer", ask_broker: "Ask the broker", escalate: "Hand over",
  source_nearby: "Source nearby", source_wide: "Search wide from the start", regulars_first: "Lane regulars first" };
const DECIDER = (e) => e.actor === "Strategist" || e.actor === "Coordinator";
function stratOf(e) {
  if (e.chosen) return { chosen: e.chosen, moves: e.moves, why: e.why };
  const m = /^\((?:AI|rules)\) ([^:]+): (.*?)(?:\. Why: (.*))?$/.exec(e.text) || [];
  const chosen = (m[1] || "").replace(/ /g, "_");
  return { chosen, moves: chosen ? { [chosen]: m[2] } : null, why: m[3] || "" };
}

function initials(name) {
  return name.replace(/[^A-Za-z ]/g, "").split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("") || "?";
}
function ownersTable(l, threads) {
  const km = (l.lane || {}).distanceKm || 1;
  const rank = (t) => (t.ownerKey === l.winner ? 0 : t.agreedBuy ? 1 : t.ask ? 2 : 3);
  const rows = threads.slice().sort((a, b) => rank(a) - rank(b) || (a.agreedBuy || a.ask * km || 1e9) - (b.agreedBuy || b.ask * km || 1e9));
  const pick = /^(Coordinator chose|Best margin|.* is rated )/.test(l.reason || "") ? l.reason.replace(/^Coordinator chose [^:]+: /, "") : "";
  const open = l.status === "sourcing" ? el("span", { class: "muted" }, `Round ${l.rounds} · `, cd(l.deadline), " left") :
    el("span", { class: "muted", text: `${threads.length} messaged` });
  return el("div", { class: "card owners-card" },
    el("div", { class: "card-head" }, el("h2", { text: "Owners on this load" }), open),
    el("div", { class: "table-wrap" }, el("table", { class: "table" },
      el("thead", {}, el("tr", {}, el("th", { text: "Owner" }), el("th", { class: "r", text: "First ask" }), el("th", { class: "r", text: "Settled at" }),
        el("th", { text: "Status" }), el("th", { text: "Role" }))),
      el("tbody", {}, ...rows.map((t, i) => {
        const first = t.firstAsk || (t.agreedBuy ? null : t.ask);
        const firstRs = first ? Math.round(first * km) : null;
        const drop = firstRs && t.agreedBuy && firstRs > t.agreedBuy ? Math.round((1 - t.agreedBuy / firstRs) * 1000) / 10 : null;
        const chosen = t.ownerKey === l.winner;
        const where = [t.city, t.deadheadKm ? `${t.deadheadKm} km from pickup` : t.deadheadKm === 0 ? "at pickup" : null].filter(Boolean).join(", ");
        const st = THREAD[t.status] || t.status;
        const stCls = t.status === "released" ? "neutral" : t.agreedBuy || t.status === "held" ? "ok" : ["declined", "backed_out"].includes(t.status) ? "bad" : ["sent", "silent"].includes(t.status) ? "neutral" : "wait";
        return el("tr", { class: chosen ? "chosen" : null },
          el("td", {}, el("div", { class: "owner" }, el("span", { class: `av av-${i % 6}`, text: initials(t.name) }),
            el("div", {}, el("b", {}, t.name, t.sandbox ? el("span", { class: "demo", title: "A simulated demo owner (rule-based, not AI)", text: "demo" }) : null),
              el("small", {}, el("span", { class: "star", text: "★" }), ` ${t.reliability}` + (where ? ` · ${where}` : ""))))),
          el("td", { class: "r", text: firstRs ? rs(firstRs) : "–" }),
          el("td", { class: "r" }, t.agreedBuy ? [el("span", { class: "rate", text: rs(t.agreedBuy) }),
            drop ? el("span", { class: "drop", text: `−${drop}%` }) : el("span", { class: "drop flat", text: "first ask" })] : el("span", { class: "muted", text: "–" })),
          el("td", {}, el("span", { class: `pill ${stCls}`, text: st })),
          el("td", { class: "role" }, chosen ? [el("b", { text: ["booked", "recovered", "holding"].includes(l.status) ? "Booked" : "Chosen" }), pick ? el("small", { text: pick }) : null]
            : t.agreedBuy && l.winner ? el("small", { text: "Backup" }) : el("small", { text: "–" })));
      })))));
}

// ---------------- right rail: the quote, the call, the lane ----------------
function marginBar(q) {
  const b = q.band || S.data.settings, max = Math.max(b.ceilingPct, q.margin_pct) * 1.08, x = (p) => `${Math.max(0, Math.min(100, (p / max) * 100))}%`;
  return [el("div", { class: "mbar", role: "img", "aria-label": `Margin ${q.margin_pct}%: floor ${b.floorPct}%, target ${b.targetPct}%, ceiling ${b.ceilingPct}%` },
      el("div", { class: q.margin_pct >= b.floorPct ? "fill" : "fill low", style: `width:${x(q.margin_pct)}` }),
      el("span", { class: "t", style: `left:${x(b.floorPct)}` }), el("span", { class: "t", style: `left:${x(b.targetPct)}` })),
    el("div", { class: "mlab" }, el("span", { style: `left:${x(b.floorPct)}`, text: `floor ${b.floorPct}%` }), el("span", { style: `left:${x(b.targetPct)}`, text: `target ${b.targetPct}%` }),
      el("span", { style: `left:${x(b.ceilingPct)}`, text: `${b.ceilingPct}%` }))];
}
function railCards(l) {
  const s = S.data.settings, q = l.quote, cards = [];
  if (q) {
    const w = l.threads[l.winner];
    const card = el("div", { class: "card side-card quote" },
      el("h2", { text: ["booked", "recovered"].includes(l.status) ? "Booked quote" : "Quote to the shipper" }),
      el("b", { class: "big", text: rs(q.sell_total) }),
      el("p", { class: "muted" }, `₹${q.rate_per_km}/km · margin `, el("span", { class: "m", text: `${q.margin_pct}% (${rs(q.margin_rs)})` })),
      ...marginBar(q),
      el("div", { class: "qline", style: "margin-top:12px" }, el("span", { text: `Buy from ${w ? w.name.split(" ")[0] : "owner"}` }), el("b", { text: rs(q.buy_total) })),
      q.market ? el("div", { class: "qline" }, el("span", { text: `Market rate, ${l.lane.distanceKm} km` }), el("b", { text: rs(q.market) })) : null,
      el("div", { class: "qline", title: `Fuel at ₹${s.dieselPerLitre}/L, tolls, driver and helper, EMI and maintenance, handling` },
        el("span", { text: "Owner's running cost" }), el("b", { text: rs(q.owner_cost || (q.fuel + q.toll + q.driver + q.misc)) })),
      el("div", { class: "qline total" }, el("span", { text: "Sell to shipper" }), el("b", { text: rs(q.sell_total) })));
    if (q.note) card.append(el("p", { class: "note", text: q.note }));
    if (l.status === "needs_approval") {
      card.append(el("button", { class: "btn go", onclick: () => act(l.id, "approve"), text: "Approve and hold the truck" }),
        el("button", { class: "btn", onclick: () => act(l.id, "reject"), text: "Reject" }),
        el("p", { class: "note", text: `Worked out by code, not AI. ${w ? w.name.split(" ")[0] + "'s" : "The owner's"} truck is held once you approve` +
          (Object.values(l.threads).filter((t) => t.agreedBuy).length > 1 ? "; the backups stay on call." : ".") }));
    } else if (["booked", "holding", "recovered"].includes(l.status)) {
      if (l.status !== "holding" && !l.shipper) {
        card.append(el("p", { class: "note", text: "Truck held. Waiting for the shipper's answer to the quote." }),
          el("button", { class: "btn go", onclick: () => act(l.id, "ship_yes"), text: "Shipper said yes" }),
          el("button", { class: "btn", onclick: () => act(l.id, "ship_no"), text: "Shipper said no" }));
      } else if (l.shipper === "yes") card.append(el("p", { class: "note", text: "Shipper confirmed." }));
      if (l.status === "holding") card.append(el("p", { class: "note", text: `Waiting for ${w ? w.name : "the owner"} to confirm the hold with the vehicle number.` }));
      if (w && w.vehicle) card.append(el("a", { class: "btn", href: `/api/loads/${l.id}/lr.pdf`, text: "Download LR (bilty) draft" }));
      card.append(el("button", { class: "btn", onclick: () => act(l.id, "backout"), text: l.mode === "sandbox" ? "Rehearse: owner backs out" : "Owner cancelled by phone" }));
    } else if (l.reason && !/^Coordinator chose|^Best margin|rated .*worth/.test(l.reason)) card.append(el("p", { class: "note", text: l.reason }));
    cards.push(card);
  } else if (l.status === "needs_call") {
    const c = l.brokerCall || {}, th = l.threads[c.owner] || {};
    cards.push(el("div", { class: "card side-card quote ask-card" }, el("h2", { text: "The desk needs your call" }),
      el("p", { class: "flag", text: ((l.events.filter(DECIDER).pop() || {}).text || "").replace(/^\((AI|rules)\) /, "") }),
      el("p", { class: "note", text: `${th.name || "Owner"} asks ₹${c.ask}/km.` }),
      el("button", { class: "btn go", onclick: () => act(l.id, "call_yes"), text: l.phase === "replan" ? "Book below floor" : "Quote at that rate" }),
      el("button", { class: "btn", onclick: () => act(l.id, "call_no"), text: "No, hand it to me" })));
  } else if (l.status === "escalated") {
    cards.push(el("div", { class: "card side-card quote" }, el("h2", { text: "Not quoted" }), el("p", { class: "flag", text: l.reason }),
      el("p", { class: "note", text: "Nothing was sent to the shipper. Call an owner you trust or quote by hand." })));
  } else {
    cards.push(el("div", { class: "card side-card quote" }, el("h2", { text: "Quote to the shipper" }),
      el("p", { class: "note", text: l.status === "waiting_trucks"
        ? `No ${l.searchType} is free near ${l.lane.origin} yet. The load stays open till ${hm(l.watchUntil)}; any owner who checks in with one is asked at once.`
        : "The price appears when an owner agrees a rate that leaves your margin." }),
      el("dl", { class: "kv" }, el("dt", { text: "Floor · target · ceiling" }), el("dd", { text: `${s.floorPct}% · ${s.targetPct}% · ${s.ceilingPct}%` }),
        el("dt", { text: "Diesel" }), el("dd", { text: `₹${s.dieselPerLitre}/L` }))));
  }
  const learned = l.events.find((e) => e.actor === "Learning");
  const lane = l.lane;
  if (lane || learned) {
    const items = learned ? learned.text.replace(/^Lane playbook for [^:]+: /, "").replace(/\.$/, "").split("; ") : [];
    cards.push(el("div", { class: "card side-card learn" },
      el("h2", {}, icon("chart"), learned ? "Learned on this lane" : "This lane"),
      lane ? el("dl", { class: "kv" }, el("dt", { text: "Road distance" }), el("dd", { text: `${lane.distanceKm} km` }),
        el("dt", { text: "Driving time" }), el("dd", { text: lane.hours ? `${lane.hours} h` : "–" })) : null,
      learned ? el("p", { text: "From the desk's own past deals on this lane. The agents read this before deciding." }) : null,
      learned ? el("ul", {}, ...items.map((x) => el("li", { text: x[0].toUpperCase() + x.slice(1) }))) : null));
  }
  return cards;
}

// Median time to quote over today's real loads: the AI broker's demo loads would flatter it.
function medianQuoteMin(d) {
  const day = (t) => new Date(t * 1000).toLocaleDateString("en-IN", { timeZone: "Asia/Kolkata" });
  const mins = d.loads.filter((l) => !l.aiBroker && l.quoteAt && day(l.createdAt) === day(d.now))
    .map((l) => (l.quoteAt - l.createdAt) / 60).sort((a, b) => a - b);
  return mins.length ? Math.round(mins[Math.floor(mins.length / 2)] * 10) / 10 : null;
}

async function act(id, action) {
  try { await api(`/api/loads/${id}/${action}`, {}); S.lastKey = null; setTimeout(poll, 300); } catch (e) { alert(e.message); }
}

// ---------------- owners ----------------
function renderOwners() {
  const d = S.data;
  const live = d.settings.mode === "live";
  $("owners-finding").textContent = live
    ? (d.liveOwners ? `${d.liveOwners} owner${d.liveOwners === 1 ? "" : "s"} registered · ${d.poolSize} truck${d.poolSize === 1 ? "" : "s"} offered today` +
       (d.checkin.sentAt ? ` · check-in sent at ${clock(d.checkin.sentAt).slice(0, 5)}` : " · check-in not sent yet") : "No live owners yet. Add them here; each gets a WhatsApp welcome.")
      + (d.aiOwners ? ` · ${d.aiOwners} simulated demo owners also bid (marked "demo owner" in quotes; rule-based, not AI)` : "")
    : `Sandbox: ${d.owners.length} persona owners with ${d.poolSize} trucks. Switch to Live owners to work with real people.`;
  $("send-checkin").textContent = live ? "Send morning check-in" : "Refresh sandbox pool";
  $("send-checkin").disabled = live && (!(d.whatsapp || d.telegram) || !d.liveOwners);
  $("pool-count").textContent = `${d.poolSize} trucks`;
  const tb = $("owners-table").querySelector("tbody");
  if (!d.owners.length) {
    tb.replaceChildren(el("tr", {}, el("td", { colspan: 4, class: "muted", text: "No owners yet." })));
    return;
  }
  tb.replaceChildren(...d.owners.map((o) => el("tr", {},
    el("td", {}, o.name, el("div", { class: "sub", text: o.phone || (o.sandbox ? "sandbox persona" : "") })),
    el("td", { text: o.baseCity || "–" }),
    el("td", {}, o.trucks.length ? o.trucks.map((t) => el("div", { text: `${t.type} · ${t.city} · ${t.available}${t.ratePerKm ? ` · ₹${t.ratePerKm}/km` : ""}` }))
      : el("span", { class: "muted", text: live ? "No reply to today's check-in" : "–" })),
    el("td", { class: "r", text: `★ ${o.reliability}` }))));
}

$("owner-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target), msg = $("owner-msg");
  try {
    await api("/api/owners", Object.fromEntries(f));
    msg.className = "form-msg"; msg.textContent = `${f.get("name")} added.`;
    e.target.reset(); poll();
  } catch (err) { msg.className = "form-msg err"; msg.textContent = err.message; }
});
$("send-checkin").addEventListener("click", async () => {
  try { const r = await api("/api/checkin", {}); $("owners-finding").textContent = r.mode === "live" ? `Check-in sent to ${r.asked} owners.` : "Sandbox pool refreshed."; poll(); }
  catch (e) { alert(e.message); }
});

// ---------------- costs ----------------
function renderCosts() {
  const s = S.data.settings, f = $("cost-form");
  $("toll-note").textContent = s.tollNote;
  if (S.costsFilled || f.contains(document.activeElement)) return;
  for (const k of ["dieselPerLitre", "dieselSource", "dieselAsOf", "floorPct", "targetPct", "ceilingPct", "maxDeadheadKm"]) f.elements[k].value = s[k];
  f.elements.agentMode.checked = !!s.agentMode;
  S.costsFilled = true;
}
$("cost-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("cost-msg");
  const body = Object.fromEntries(new FormData(e.target));
  body.agentMode = e.target.elements.agentMode.checked;      // an unticked box sends nothing, so say it explicitly
  try { await api("/api/settings", body); msg.className = "form-msg"; msg.textContent = "Saved. New quotes use these numbers."; S.costsFilled = false; poll(); }
  catch (err) { msg.className = "form-msg err"; msg.textContent = err.message; }
});

$("sign-in").addEventListener("click", () => { if (askKey()) { S.lastKey = null; S.costsFilled = false; poll(); } });

// ---------------- navigation and mode ----------------
document.querySelectorAll(".nav-item").forEach((b) => b.addEventListener("click", () => {
  S.view = b.dataset.view; S.sel = null;
  try { history.replaceState(null, "", location.pathname); } catch (e) {}
  document.querySelectorAll(".nav-item").forEach((n) => (n === b ? n.setAttribute("aria-current", "page") : n.removeAttribute("aria-current")));
  for (const v of ["desk", "owners", "costs"]) $("view-" + v).hidden = v !== S.view;
  $("sidebar").classList.remove("open"); $("menu-btn").setAttribute("aria-expanded", "false");
  S.costsFilled = false;
  if (S.data) render();
  window.scrollTo(0, 0);
}));
$("menu-btn").addEventListener("click", () => {
  const open = $("sidebar").classList.toggle("open");
  $("menu-btn").setAttribute("aria-expanded", String(open));
});
document.querySelectorAll(".mode button").forEach((b) => b.addEventListener("click", async () => {
  try { await api("/api/mode", { mode: b.dataset.mode }); S.sel = null; poll(); } catch (e) { alert(e.message); }
}));
if (/^#BH-\d+$/.test(location.hash)) S.sel = location.hash.slice(1);

// ---------------- composer: a short chat that becomes a load ----------------
const FIELDS = [["origin", "From"], ["destination", "To"], ["weight_ton", "Weight"], ["truck_type", "Truck"], ["pickup_when", "Pickup"], ["budget_rs", "Budget"]];
function renderComposer() {
  $("chat").replaceChildren(...(S.chat.length ? S.chat : [{ role: "bot", text: "Paste or type the shipper's request, in Hindi, English or Hinglish. I'll ask for anything missing." }])
    .map((m) => el("div", { class: `msg ${m.role === "user" ? "user" : ""}`, text: m.text })));
  $("chat").scrollTop = 1e6;
  $("draft").replaceChildren(...FIELDS.map(([k, label]) => {
    const v = S.draft[k];
    const shown = v === undefined ? null : k === "weight_ton" ? `${v} T` : k === "budget_rs" ? rs(v) : v;
    return el("span", { class: shown ? "chip" : "chip missing", text: shown ? `${label}: ${shown}` : label + (["pickup_when", "budget_rs"].includes(k) ? " (optional)" : "") });
  }));
}
$("new-load").addEventListener("click", () => { S.chat = []; S.draft = {}; renderComposer(); $("composer").showModal(); $("composer-text").focus(); });
$("composer-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("composer-text"), text = input.value.trim();
  if (!text || S.busy) return;
  S.busy = true; input.value = "";
  const prior = S.chat.slice();
  S.chat.push({ role: "user", text }); renderComposer();
  try {
    const r = await api("/api/intake", { chat: prior, load: S.draft, text });
    S.chat.push({ role: "bot", text: r.reply }); S.draft = r.load || S.draft; renderComposer();
    if (r.loadId) { setTimeout(() => { $("composer").close(); S.sel = r.loadId; poll(); }, 700); }
  } catch (err) { S.chat.push({ role: "bot", text: err.message }); renderComposer(); }
  S.busy = false; input.focus();
});
