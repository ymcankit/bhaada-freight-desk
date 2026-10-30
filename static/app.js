/* Bhaada console. Polls /api/state and renders it. All text from brokers and owners goes in as text, never as HTML. */
"use strict";
const S = { data: null, view: "desk", sel: null, open: null, chat: [], draft: {}, costsFilled: false, busy: false };
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
  hold_requested: "Asked to hold", held: "Holding", reconfirm: "Re-confirming", backed_out: "Backed out" };

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
  $("scope").textContent = (d.settings.mode === "live" ? "Freight desk · live owners" : "Freight desk · sandbox") + (d.readOnly ? " · view only" : d.publicView ? " · open demo" : "");
  $("dot-wa").classList.toggle("on", !!(d.whatsapp || d.telegram));
  $("conn-wa").textContent = [d.whatsapp ? "WhatsApp connected" : "WhatsApp not connected",
                              d.telegram ? `Telegram @${d.telegram}` : null].filter(Boolean).join(" · ");
  $("dot-llm").classList.toggle("on", d.llm);
  $("conn-llm").textContent = d.llm ? "Language model on" : "Language model off";
  for (const m of ["live", "sandbox"]) $("mode-" + m).setAttribute("aria-pressed", String(d.settings.mode === m));
  $("sandbox-banner").hidden = d.settings.mode !== "sandbox";
  const waiting = d.kpis.waiting;
  $("nav-waiting").hidden = !waiting;
  $("nav-waiting").textContent = waiting;
  $("nav-waiting").setAttribute("aria-label", `${waiting} waiting for approval`);
  if (S.view === "desk") renderDesk();
  if (S.view === "owners") renderOwners();
  if (S.view === "costs") renderCosts();
}

// ---------------- desk ----------------
function renderDesk() {
  const d = S.data, k = d.kpis, med = medianQuoteMin(d);
  renderFinding();
  const kp = [
    ["Loads today", k.received], ["Quoted", k.quoted], ["Booking-ready", k.booked], ["Waiting on you", k.waiting],
    ["Median time to quote", med === null ? null : [med, "min"]],
    ["Avg margin booked", k.avgMargin === null ? null : [k.avgMargin, "%"]],
    ["Quotes within ±3% of broker's own", k.shadowWithin3Pct == null ? null : [k.shadowWithin3Pct, `% of ${k.shadowCompared}`]],
  ];
  $("kpis").replaceChildren(...kp.map(([label, v]) => el("div", { title: label.startsWith("Median") ? "Real loads only: AI-broker demo loads are left out"
    : label.startsWith("Quotes within") ? "Shadow mode: the broker types 'mera rate BH-xxxx 27500' after quoting the shipper themselves" : null }, el("dt", { text: label }),
    v === null ? el("dd", { class: "dash", text: "–" }) : Array.isArray(v) ? el("dd", {}, String(v[0]), el("small", { text: v[1] })) : el("dd", { text: String(v) }))));

  const loads = d.loads;
  $("list-count").textContent = loads.length ? `${loads.length}` : "";
  if (!loads.length) {
    $("loads").replaceChildren(el("li", { class: "list-empty", text: "No loads yet today. Start one with New load, or send one to the desk's WhatsApp number." }));
  } else {
    $("loads").replaceChildren(...loads.map((l) => {
      const [word, cls] = statusOf(l);
      const f = l.load, lane = l.lane || {};
      let sub = `${f.weight_ton} T · ${l.searchType || f.truck_type}`;
      if (l.status === "sourcing") {
        const open = Object.values(l.threads).filter((t) => ["sent", "countered", "reconfirm"].includes(t.status)).length;
        sub += ` · ${open} owner${open === 1 ? "" : "s"} to reply`;
      } else if (l.status === "waiting_trucks") sub += ` · no truck free yet, kept open till ${clock(l.watchUntil).slice(0, 5)}`;
      else if (l.quote) sub += ` · ${rs(l.quote.sell_total)} · ${l.quote.margin_pct}%`;
      else if (l.reason) sub += ` · ${l.reason}`;
      return el("li", {}, el("button", { class: "load-row", "aria-current": String(S.sel === l.id), onclick: () => select(l.id) },
        el("span", { class: "route" }, el("span", { class: "id", text: l.id }), `${lane.origin || f.origin} → ${lane.destination || f.destination}`),
        el("span", { class: "when", text: ago(l.createdAt) }),
        el("span", { class: "sub", text: sub }),
        el("span", {}, el("span", { class: `pill ${cls}`, text: word }))));
    }));
  }
  if (!S.sel && loads.length && window.innerWidth > 760) S.sel = (loads.find((l) => l.status === "needs_approval") || loads[0]).id;
  const load = loads.find((l) => l.id === S.sel);
  document.querySelector(".triage").classList.toggle("has-open", !!load);
  $("open-empty").hidden = !!load;
  $("open").hidden = !load;
  if (load) { renderOpen(load); renderDetail(load); } else $("detail").replaceChildren();
}

function renderFinding() {
  const loads = S.data.loads, f = $("finding");
  const wait = loads.find((l) => l.status === "needs_approval");
  const re = loads.find((l) => l.status === "sourcing" && l.phase === "replan");
  const src = loads.find((l) => l.status === "sourcing");
  const esc = loads.find((l) => l.status === "escalated" && S.data.now - (l.closedAt || 0) < 1800);
  let parts;
  if (wait) parts = [el("b", { text: `${wait.id} needs your approval: ` }), `${wait.lane.origin} → ${wait.lane.destination} at ${rs(wait.quote.sell_total)}, margin ${wait.quote.margin_pct}%.`];
  else if (re) parts = [el("b", { text: `${re.id}: an owner backed out. ` }), "Re-booking at the shipper's fixed price; nothing needed from you unless the floor is at risk."];
  else if (src) parts = [el("b", { text: `${src.id}: ` }), `messaging owners near ${src.lane ? src.lane.origin : src.load.origin}, `, cd(src.deadline), " left in this round."];
  else if (loads.some((l) => l.status === "waiting_trucks")) {
    const w = loads.filter((l) => l.status === "waiting_trucks");
    parts = [el("b", { text: `${w.map((l) => l.id).join(", ")}: ` }), `waiting for a truck to check in. The broker was told; nothing needed from you yet.`];
  }
  else if (esc) parts =[el("b", { text: `${esc.id} was escalated: ` }), esc.reason];
  else if (loads.length) parts = ["Nothing is waiting on you. ", `${S.data.kpis.booked} booking-ready today.`];
  else parts = [S.data.settings.mode === "live" && !S.data.liveOwners ? "No live owners yet: add them under Owners and pool, then send the morning check-in." : "No loads yet today."];
  f.replaceChildren(...parts);
}

function select(id) {
  S.sel = id; S.open = null;
  renderDesk();
  if (window.innerWidth <= 760) window.scrollTo(0, 0);
}

function renderOpen(l) {
  const d = S.data, f = l.load, lane = l.lane || {};
  const [word, cls] = statusOf(l);
  const head = el("div", { class: "open-head" },
    el("div", {},
      el("button", { class: "back", onclick: () => { S.sel = null; renderDesk(); }, text: "‹ All loads" }),
      el("h2", {}, `${lane.origin || f.origin} → ${lane.destination || f.destination}`),
      el("div", { class: "meta", text: [l.id, lane.distanceKm ? `${lane.distanceKm} km by road` : null, `${f.weight_ton} T`, l.searchType || f.truck_type,
        f.pickup_when || "kal", f.budget_rs ? `budget ${rs(f.budget_rs)}` : null, l.source === "whatsapp" ? "from WhatsApp" : l.source === "telegram" ? "from Telegram" : "from console",
        l.aiBroker ? "AI-broker demo" : null].filter(Boolean).join(" · ") }),
      el("div", { class: "meta" }, stopwatch(l))),
    el("span", { class: `pill ${cls}`, text: word }));
  const parts = [head];
  const threads = Object.values(l.threads);
  if (threads.length) parts.push(race(l, threads));
  const st = strategist(l);
  if (st) parts.push(st);
  parts.push(log(l));
  $("open").replaceChildren(...parts);
}

function race(l, threads) {
  const d = S.data;
  const t0 = Math.min(...threads.map((t) => (t.messages[0] || {}).t || d.now));
  const ends = [d.now, l.deadline || 0, ...threads.flatMap((t) => t.messages.map((m) => m.t))];
  const t1 = Math.max(t0 + 30, ...ends.filter((x) => x && x < d.now + 3600));
  const x = (t) => ((t - t0) / (t1 - t0)) * 100;
  const open = ["sent", "countered", "reconfirm", "hold_requested"];
  const rows = threads.map((t) => {
    const ms = t.messages;
    const svg = el("svg:svg", { viewBox: "0 0 100 24", preserveAspectRatio: "none", role: "img",
      "aria-label": `${t.name}: ${ms.length} messages, ${THREAD[t.status] || t.status}` });
    const first = ms.length ? x(ms[0].t) : 0;
    const last = open.includes(t.status) ? x(d.now) : ms.length ? x(ms[ms.length - 1].t) : first;
    svg.append(el("svg:line", { x1: first, x2: Math.max(first, last), y1: 12, y2: 12, stroke: "#c9d0cc", "stroke-width": 2, "vector-effect": "non-scaling-stroke",
      "stroke-dasharray": open.includes(t.status) ? "3 3" : null }));
    return { t, svg, ms };
  });
  const box = el("div", { class: "race" },
    el("div", { class: "race-head" }, el("h3", { text: "Owners on this load" }),
      l.status === "sourcing" ? el("span", { class: "muted" }, `Round ${l.rounds} · `, cd(l.deadline), " left") : el("span", { class: "muted", text: `${threads.length} asked` })));
  for (const { t, svg, ms } of rows) {
    // markers drawn as HTML dots over the svg track so they stay round at any width
    const track = el("div", { class: "race-track", style: "position:relative" }, svg);
    for (const m of ms) {
      const left = x(m.t);
      track.append(el("span", { style: `position:absolute;top:7px;left:calc(${left}% - 5px);width:10px;height:10px;border-radius:50%;` +
        (m.dir === "in" ? "background:#5b605d" : "background:#fff;border:2px solid #c9d0cc"), title: `${clock(m.t)} ${m.dir === "in" ? t.name : "Bhaada"}: ${m.text}` }));
    }
    const endColour = { deal: "var(--accent)", held: "var(--accent)", declined: "var(--negative)", backed_out: "var(--negative)" }[t.status];
    if (endColour && ms.length) {
      const left = x(ms[ms.length - 1].t);
      track.append(el("span", { style: `position:absolute;top:5px;left:calc(${left}% - 7px);width:14px;height:14px;border-radius:50%;background:${endColour}` }));
    }
    const rate = t.agreedBuy && l.lane ? `₹${(t.agreedBuy / l.lane.distanceKm).toFixed(1)}/km` : t.ask ? `asked ₹${t.ask}` : "–";
    const exp = S.open === t.ownerKey;
    const who = [t.city, t.deadheadKm ? `${t.deadheadKm} km away` : null, `${t.reliability}/5`].filter(Boolean).join(" · ");
    box.append(el("button", { class: "race-row", "aria-expanded": String(exp), onclick: () => { S.open = exp ? null : t.ownerKey; renderDesk(); } },
      el("span", { class: "race-who" }, el("b", { text: t.name }), el("span", { text: who })),
      track,
      el("span", { class: "race-rate" }, rate, el("span", { class: "st", text: (load_winner(l) === t.ownerKey ? "Chosen · " : "") + (THREAD[t.status] || t.status) }))));
    if (exp) box.append(el("div", { class: "thread" }, ...ms.map((m) => el("div", { class: `msg ${m.dir}` }, m.text, el("time", { text: clock(m.t) })))));
  }
  const span = t1 - t0;
  box.append(el("div", { class: "race-axis" }, el("span"), el("span", { class: "ticks" }, ...[0, 0.5, 1].map((p) => el("span", { text: mmss(span * p) }))), el("span")));
  return box;
}
const load_winner = (l) => l.winner;

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
const badge = (by) => el("span", { class: `by by-${by.toLowerCase()}`, title: BY_TITLE[by] || null, text: by });

function log(l) {
  return el("div", { class: "log" },
    el("div", { class: "log-head" }, el("h3", { text: "What the agents did" }), el("span", { class: "muted", text: "Who decided: AI · Code · Human" })),
    el("ol", {}, ...l.events.map((e) => el("li", {}, el("time", { text: clock(e.t) }), el("span", { class: "actor", text: e.actor }),
      el("span", {}, badge(byOf(e))), el("span", { text: e.text })))));
}

// The strategist's decisions: every move the code offered, the one chosen, and why.
const MOVE = { widen: "Widen the search", bigger_truck: "Bigger truck", final_offer: "Final offer", ask_broker: "Ask the broker", escalate: "Hand over",
  source_nearby: "Source nearby", source_wide: "Search wide from the start", regulars_first: "Lane regulars first" };
const DECIDER = (e) => e.actor === "Strategist" || e.actor === "Coordinator";
function stratOf(e) {
  if (e.chosen) return { chosen: e.chosen, moves: e.moves, why: e.why };
  const m = /^\((?:AI|rules)\) ([^:]+): (.*?)(?:\. Why: (.*))?$/.exec(e.text) || [];
  const chosen = (m[1] || "").replace(/ /g, "_");
  return { chosen, moves: chosen ? { [chosen]: m[2] } : null, why: m[3] || "" };
}
function strategist(l) {
  const evs = l.events.filter(DECIDER);
  if (!evs.length) return null;
  const coord = evs.some((e) => e.actor === "Coordinator");
  return el("div", { class: "strat" },
    el("div", { class: "log-head" }, el("h3", { text: coord ? "Coordinator agent" : "Strategist" }), el("span", { class: "muted", text: `${evs.length} decision${evs.length === 1 ? "" : "s"}` })),
    el("ol", {}, ...evs.map((e) => {
      const s = stratOf(e);
      return el("li", {},
        el("div", { class: "strat-head" }, el("time", { text: clock(e.t) }), el("b", { text: MOVE[s.chosen] || (s.moves && s.moves[s.chosen] ? s.moves[s.chosen].split(":")[0] : "") || "Decision" }), badge(byOf(e))),
        s.moves ? el("ul", { class: "moves", "aria-label": "Moves considered" }, ...Object.entries(s.moves).map(([k, v]) =>
          el("li", { class: k === s.chosen ? "chosen" : null }, el("span", { class: "mv", text: (k === s.chosen ? "Chosen · " : "") + (MOVE[k] || String(v).split(":")[0]) }),
            el("span", { text: v })))) : el("p", { class: "muted", text: e.text }),
        s.why ? el("p", { class: "why", text: `Why: ${s.why}` }) : null);
    })));
}

// Inquiry → booking-ready: live while the load is open, frozen once an owner holds the truck.
function stopwatch(l) {
  const ready = ["booked", "recovered"].includes(l.status) && l.bookedAt;
  if (ready) return el("span", { class: "sw done", title: "Inquiry to booking-ready" }, "Booking-ready in ", el("b", { text: mmss(l.bookedAt - l.createdAt) }));
  if (["escalated", "rejected"].includes(l.status)) return el("span", { class: "sw", text: "Not booked" });
  return el("span", { class: "sw", title: "Inquiry to booking-ready, still running" }, "Open for ",
    el("b", { class: "sw-live", "data-start": l.createdAt, text: mmss(nowEst() - l.createdAt) }));
}

// Median time to quote over today's real loads: the AI broker's demo loads would flatter it.
function medianQuoteMin(d) {
  const day = (t) => new Date(t * 1000).toLocaleDateString("en-IN", { timeZone: "Asia/Kolkata" });
  const mins = d.loads.filter((l) => !l.aiBroker && l.quoteAt && day(l.createdAt) === day(d.now))
    .map((l) => (l.quoteAt - l.createdAt) / 60).sort((a, b) => a - b);
  return mins.length ? Math.round(mins[Math.floor(mins.length / 2)] * 10) / 10 : null;
}

function rail(q, settings) {
  const s = q.band || settings;                     // the lane's own band: short hauls run 5/8/12%
  const max = Math.max(s.ceilingPct + 4, q.margin_pct + 2), w = 300, x = (p) => 8 + (Math.max(0, p) / max) * (w - 16);
  const svg = el("svg:svg", { viewBox: `0 0 ${w} 46`, role: "img", "aria-label": `Margin ${q.margin_pct}% against floor ${s.floorPct}%, target ${s.targetPct}%, ceiling ${s.ceilingPct}%` });
  svg.append(el("svg:rect", { x: 8, y: 14, width: w - 16, height: 8, rx: 4, fill: "#eef1ef" }));
  svg.append(el("svg:rect", { x: x(s.floorPct), y: 14, width: x(s.ceilingPct) - x(s.floorPct), height: 8, fill: "#dbe7e1" }));
  for (const [p] of [[s.floorPct], [s.targetPct], [s.ceilingPct]]) {
    svg.append(el("svg:line", { x1: x(p), x2: x(p), y1: 10, y2: 26, stroke: "#5b605d", "stroke-width": 1 }));
    svg.append(el("svg:text", { x: x(p), y: 40, "text-anchor": "middle", "font-size": 11, fill: "#5b605d", class: "rail-label" }, `${p}%`));
  }
  const ok = q.margin_pct >= s.floorPct;
  svg.append(el("svg:rect", { x: x(q.margin_pct) - 3, y: 6, width: 6, height: 24, rx: 2, fill: ok ? "#1f6b4f" : "#c4242a" }));
  return el("div", { class: "rail" }, svg);
}

function renderDetail(l) {
  const s = S.data.settings, q = l.quote, parts = [];
  if (q) {
    const w = l.threads[l.winner];
    const card = el("div", { class: "qcard" },
      el("h2", { text: ["booked", "recovered"].includes(l.status) ? "Booked quote" : "Quote to the shipper" }),
      el("div", { class: "big" }, rs(q.sell_total), el("small", { text: ` · ₹${q.rate_per_km}/km` })),
      el("div", { class: "muted", text: `Margin ${q.margin_pct}% · ${rs(q.margin_rs)}` }),
      rail(q, s),
      el("div", { class: "muted", style: "font-size:12px;margin:-2px 0 10px", text: `Floor ${(q.band || s).floorPct}% · target ${(q.band || s).targetPct}% · ceiling ${(q.band || s).ceilingPct}%` }),
      el("dl", { class: "kv" },
        el("dt", { text: "Owner" }), el("dd", { text: w ? `${w.name} · ${w.reliability}/5` : "–" }),
        ...(q.market ? [el("dt", { text: `Market rate (shipper), ${l.lane.distanceKm} km` }), el("dd", { text: rs(q.market) })] : []),
        el("dt", { text: `Owner's running cost: fuel at ₹${s.dieselPerLitre}/L, tolls, driver and helper, EMI/maintenance, handling` }),
        el("dd", { text: rs(q.owner_cost || (q.fuel + q.toll + q.driver + q.misc)) }),
        el("dt", { class: "tot", text: q.basis === "cost" ? "Cost basis (owner's running cost)" : "Cost basis (market less a broker margin)" }),
        el("dd", { class: "tot", text: rs(q.est_cost) }),
        el("dt", { text: "Buy from owner" }), el("dd", { text: rs(q.buy_total) }),
        el("dt", { class: "tot", text: "Sell to shipper" }), el("dd", { class: "tot", text: rs(q.sell_total) })));
    if (q.note) card.append(el("p", { class: "note", text: q.note }));
    if (l.reason) card.append(el("p", { class: "note", text: l.reason }));
    if (l.status === "needs_approval") {
      card.append(el("div", { class: "actions" },
        el("button", { class: "btn primary", onclick: () => act(l.id, "approve"), text: "Approve and hold" }),
        el("button", { class: "btn", onclick: () => act(l.id, "reject"), text: "Reject" })));
    } else if (["booked", "holding", "recovered"].includes(l.status)) {
      if (l.status !== "holding" && !l.shipper) {
        card.append(el("p", { class: "note", text: "Truck held. Waiting for the shipper's answer to the quote." }),
          el("div", { class: "actions" },
            el("button", { class: "btn primary", onclick: () => act(l.id, "ship_yes"), text: "Shipper said yes" }),
            el("button", { class: "btn", onclick: () => act(l.id, "ship_no"), text: "Shipper said no" })));
      } else if (l.shipper === "yes") {
        card.append(el("p", { class: "note", text: "Shipper confirmed." }));
      }
      card.append(el("div", { class: "actions" }, el("button", { class: "btn", style: "grid-column:1/-1", onclick: () => act(l.id, "backout"),
        text: l.mode === "sandbox" ? "Rehearse: owner backs out" : "Owner cancelled by phone" })));
    }
    parts.push(card);
  } else if (l.status === "needs_call") {
    const c = l.brokerCall || {}, th = l.threads[c.owner] || {};
    parts.push(el("div", { class: "qcard" }, el("h2", { text: "The desk needs your call" }),
      el("p", { class: "flag", text: (l.events.filter(DECIDER).pop() || {}).text || "" }),
      el("p", { class: "muted", text: `${th.name || "Owner"} asks ₹${c.ask}/km.` }),
      el("div", { class: "actions" },
        el("button", { class: "btn primary", onclick: () => act(l.id, "call_yes"), text: l.phase === "replan" ? "Book below floor" : "Quote at that rate" }),
        el("button", { class: "btn", onclick: () => act(l.id, "call_no"), text: "No, hand it to me" }))));
  } else if (l.status === "escalated") {
    parts.push(el("div", { class: "qcard" }, el("h2", { text: "Not quoted" }), el("p", { class: "flag", text: l.reason }),
      el("p", { class: "note", text: "Nothing was sent to the shipper. Call an owner you trust or quote by hand." })));
  } else {
    parts.push(el("div", { class: "qcard" }, el("h2", { text: "Quote" }),
      el("p", { class: "muted", text: l.status === "waiting_trucks"
        ? `No ${l.searchType} is free near ${l.lane.origin} yet. The load stays open till ${clock(l.watchUntil).slice(0, 5)}; any owner who checks in with one is asked at once.`
        : "The price appears when at least one owner agrees a rate above your floor." }),
      el("dl", { class: "kv", style: "margin-top:10px" },
        el("dt", { text: "Floor · target · ceiling" }), el("dd", { text: `${s.floorPct}% · ${s.targetPct}% · ${s.ceilingPct}%` }),
        el("dt", { text: "Diesel" }), el("dd", { text: `₹${s.dieselPerLitre}/L` }))));
  }
  if (l.lane) parts.push(el("div", { class: "qcard" }, el("h2", { text: "Lane" }), el("dl", { class: "kv" },
    el("dt", { text: "Road distance" }), el("dd", { text: `${l.lane.distanceKm} km` }),
    el("dt", { text: "Source" }), el("dd", { text: l.lane.distanceSrc }),
    el("dt", { text: "Driving time" }), el("dd", { text: l.lane.hours ? `${l.lane.hours} h` : "–" }))));
  $("detail").replaceChildren(...parts);
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
    el("td", { class: "num", text: `${o.reliability}/5` }))));
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

// ---------------- navigation and mode ----------------
document.querySelectorAll(".nav-item").forEach((b) => b.addEventListener("click", () => {
  S.view = b.dataset.view;
  document.querySelectorAll(".nav-item").forEach((n) => n.toggleAttribute("aria-current", n === b));
  document.querySelectorAll(".nav-item[aria-current]").forEach((n) => n.setAttribute("aria-current", "page"));
  for (const v of ["desk", "owners", "costs"]) $("view-" + v).hidden = v !== S.view;
  $("crumb").textContent = b.textContent.trim();
  $("sidebar").classList.remove("open"); $("menu-btn").setAttribute("aria-expanded", "false");
  S.costsFilled = false;
  if (S.data) render();
}));
$("menu-btn").addEventListener("click", () => {
  const open = $("sidebar").classList.toggle("open");
  $("menu-btn").setAttribute("aria-expanded", String(open));
});
document.querySelectorAll(".mode button").forEach((b) => b.addEventListener("click", async () => {
  try { await api("/api/mode", { mode: b.dataset.mode }); S.sel = null; poll(); } catch (e) { alert(e.message); }
}));

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
