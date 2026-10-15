"use strict";
/* Clause UI: one page, live over SSE. State comes from GET /api/state; every change after that
   arrives as an event (decision, invoice, email, memory, clock, chat, toast) and is upserted by id. */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (p) => "£" + (Number(p || 0) / 100).toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const dayMs = 86400000;
const toDate = (iso) => new Date(String(iso).slice(0, 10) + "T12:00:00Z");
const longDate = (iso) => (iso ? toDate(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }) : "—");
const daysBetween = (a, b) => Math.round((toDate(b) - toDate(a)) / dayMs);

const S = {
  business: "", clock: null, modes: {},
  customers: new Map(), invoices: new Map(), decisions: new Map(), mail: new Map(), memory: [],
  glow: new Set(), ready: false, pendingChat: 0,
};

/* ---------------- API ---------------- */
async function api(method, path, body) {
  if (window.MOCK) return window.MOCK.handle(method, path, body);
  const res = await fetch(path, {
    method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let text = await res.text();
    try { text = JSON.parse(text).detail || text; } catch (_) { /* plain text */ }
    throw new Error(`${res.status}: ${typeof text === "string" ? text : JSON.stringify(text)}`);
  }
  return res.json();
}

/* ---------------- state ---------------- */
const custName = (key) => (S.customers.get(key) || {}).name || key || "—";
const remaining = (inv) => inv.remaining_pence ?? inv.amount_remaining ?? inv.total_pence ?? 0;
const isOverdue = (inv) => inv.status === "open" && (inv.overdue || (S.clock && inv.due_date && S.clock > inv.due_date.slice(0, 10)));
const daysOverdue = (inv) => (S.clock && inv.due_date ? Math.max(0, daysBetween(inv.due_date, S.clock)) : 0);

function laneOf(inv) {
  if (inv.status === "paid") return "paid";
  if (inv.status === "void" || inv.status === "uncollectible") return null;
  return isOverdue(inv) ? "overdue" : "open";
}

function decisionAmount(d) {
  const c = d.calc || {};
  if (c.total_pence != null) return c.total_pence;
  if (c.claim_total_pence != null) return c.claim_total_pence;
  if (c.interest_pence != null) return (c.interest_pence || 0) + (c.compensation_pence || 0);
  if (c.amount_pence != null) return c.amount_pence;
  return null;
}

function decisionFormula(d) {
  const c = d.calc || {};
  if (typeof c.formula === "string") return c.formula;
  if (Array.isArray(c.lines)) return c.lines.join(" · ");
  return "";
}

function load(state) {
  S.business = state.business || "";
  S.clock = state.clock_date || null;
  S.modes = state.modes || {};
  S.customers = new Map((state.customers || []).map((c) => [c.id, c]));
  S.invoices = new Map((state.invoices || []).map((i) => [i.id, i]));
  S.decisions = new Map((state.decisions || []).map((d) => [d.id, d]));
  for (const d of state.approvals || []) S.decisions.set(d.id, d);
  S.mail = new Map();
  for (const e of state.emails || []) S.mail.set("in:" + e.id, { ...e, direction: "in" });
  for (const o of state.outbox || []) S.mail.set("out:" + o.id, { ...o, direction: "out" });
  S.memory = (state.memory_events || []).slice();
}

/* ---------------- render ---------------- */
let renderQueued = false;
function render() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => {
    renderQueued = false;
    renderHeader(); renderKpis(); renderBoard(); renderTimeline(); renderMail(); renderMemory(); renderSelects();
  });
}

function renderHeader() {
  $("#business").textContent = S.business;
  $("#clock").textContent = S.clock ? longDate(S.clock) : "—";
  const m = S.modes;
  const badges = [
    ["stripe", m.stripe === "test" ? "stripe: test mode" : "stripe: offline", m.stripe === "test"],
    ["llm", String(m.llm || "").startsWith("fake") ? "claude: offline" : "claude", !String(m.llm || "fake").startsWith("fake")],
    ["memory", `memory: ${m.memory || "local"}`, m.memory === "cognee"],
    ["mail", m.mail === "gmail" ? "gmail" : "mail: outbox", m.mail === "gmail"],
  ];
  $("#modes").innerHTML = badges.map(([k, label, on]) => `<span class="mode ${on ? "on" : ""}" title="${esc(k)}">${esc(label)}</span>`).join("");
}

const kpiShown = {};
function setKpi(id, pence) {
  const box = $("#" + id);
  const el = $(".kpi-value", box);
  const from = kpiShown[id] ?? 0;
  kpiShown[id] = pence;
  if (from === pence) { el.textContent = money(pence); return; }
  if (S.ready) { box.classList.remove("flash"); void box.offsetWidth; box.classList.add("flash"); }
  const start = performance.now();
  const step = (t) => {
    const k = Math.min(1, (t - start) / 700);
    const eased = 1 - Math.pow(1 - k, 3);
    el.textContent = money(Math.round(from + (pence - from) * eased));
    if (k < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function renderKpis() {
  let outstanding = 0, overdue = 0, collected = 0, interest = 0;
  for (const inv of S.invoices.values()) {
    if (inv.status === "open") { outstanding += remaining(inv); if (isOverdue(inv)) overdue += remaining(inv); }
    if (inv.status === "paid") collected += inv.total_pence || 0;
    if (inv.kind === "interest" && inv.status !== "void") interest += inv.total_pence || 0;
  }
  setKpi("kpi-outstanding", outstanding); setKpi("kpi-overdue", overdue);
  setKpi("kpi-collected", collected); setKpi("kpi-interest", interest);
}

const SOURCE = { voice: "Voice note", chat: "Chat", email: "Customer email", seed: "Contract", agent: "Agent" };

function invoiceCard(inv, compact = false) {
  const lane = laneOf(inv);
  const pill = inv.status === "paid" ? `<span class="pill paid">Paid</span>`
    : lane === "overdue" ? `<span class="pill overdue">${daysOverdue(inv)} days overdue</span>`
    : inv.status === "draft" ? `<span class="pill draft">Draft</span>` : `<span class="pill open">Open</span>`;
  const rem = remaining(inv);
  const amount = inv.status === "open" && rem !== inv.total_pence
    ? `${money(rem)} <span class="muted" style="font-size:13px">of ${money(inv.total_pence)}</span>` : money(inv.total_pence);
  const src = (inv.source || {}).type;
  const tags = [];
  if (inv.kind === "interest") tags.push(`<span class="tag">Interest · CC-5</span>`);
  else if (src && SOURCE[src]) tags.push(`<span class="tag">${SOURCE[src]}</span>`);
  const notes = [];
  if (inv.disputed) notes.push(`Disputed: “${esc(inv.dispute_quote || "in writing")}”. Chasing on hold (CC-3).`);
  if (inv.promise_date && inv.status === "open") notes.push(`Promised to pay by ${longDate(inv.promise_date)}${inv.promise_quote ? `: “${esc(inv.promise_quote)}”` : ""} (CC-4).`);
  if (inv.chases && inv.chases.length && inv.status === "open") notes.push(`${inv.chases.length} chase${inv.chases.length > 1 ? "s" : ""} sent (CC-2).`);
  const formula = inv.kind === "interest" && inv.lines && inv.lines[0] ? `<div class="formula">${esc(inv.lines[0].description)}</div>` : "";
  const parent = inv.parent_invoice && S.invoices.get(inv.parent_invoice);
  const links = compact ? "" : `<div class="card-links">
      <a href="https://dashboard.stripe.com/test/invoices/${encodeURIComponent(inv.id)}" target="_blank" rel="noopener">Stripe ↗</a>
      ${inv.hosted_invoice_url ? `<a href="${esc(inv.hosted_invoice_url)}" target="_blank" rel="noopener">Pay page ↗</a>` : ""}
      ${inv.status === "open" ? `<a href="#" data-pay="${esc(inv.id)}">Test pay</a>` : ""}
    </div>`;
  return `<div class="card ${S.glow.has("inv:" + inv.id) ? "glow" : ""}" data-key="inv:${esc(inv.id)}">
    <div class="card-top"><span class="card-cust">${esc(custName(inv.customer))}</span><span style="margin-left:auto">${pill}</span></div>
    <div class="card-amount">${amount}</div>
    <div class="card-meta"><span class="num">${esc(inv.number || inv.id)}</span><span>Due ${longDate(inv.due_date)}</span>${tags.join("")}</div>
    ${parent ? `<div class="card-meta" style="margin-top:4px">On ${esc(parent.number || parent.id)}</div>` : ""}
    ${formula}
    ${notes.map((n) => `<div class="card-note">${n}</div>`).join("")}
    ${links}
  </div>`;
}

function approvalCard(d) {
  const amt = decisionAmount(d);
  const formula = decisionFormula(d);
  return `<div class="card ${S.glow.has("dec:" + d.id) ? "glow" : ""}" data-key="dec:${esc(d.id)}">
    <div class="card-top"><span class="card-cust">${esc(custName(d.customer))}</span><span style="margin-left:auto"><span class="pill approval">Needs approval</span></span></div>
    ${amt != null ? `<div class="card-amount">${money(amt)}</div>` : ""}
    <div style="font-size:13px;font-weight:500">${esc(d.summary)}</div>
    <ul class="card-reasons">${(d.reasons || []).map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
    ${formula ? `<div class="formula">${esc(formula)}</div>` : ""}
    <div class="card-meta" style="margin-top:6px">${(d.clauses || []).map((c) => `<span class="chip needs_approval">${esc(c)}</span>`).join("")}</div>
    <div class="card-actions">
      ${d.proposal ? `<button class="btn btn-primary btn-sm" data-approve="${esc(d.id)}">Approve</button>` : `<button class="btn btn-primary btn-sm" data-approve="${esc(d.id)}">Mark resolved</button>`}
      <button class="btn btn-sm" data-reject="${esc(d.id)}">Reject</button>
    </div>
  </div>`;
}

function renderBoard() {
  const board = $("#board");
  const before = new Map($$(".card[data-key]", board).map((el) => [el.dataset.key, el.getBoundingClientRect()]));
  const lanes = { approval: [], open: [], overdue: [], paid: [] };
  for (const d of S.decisions.values()) if (d.status === "pending_approval") lanes.approval.push(d);
  const invs = [...S.invoices.values()].reverse();
  for (const inv of invs) { const l = laneOf(inv); if (l) lanes[l].push(inv); }
  lanes.overdue.sort((a, b) => daysOverdue(b) - daysOverdue(a));
  lanes.approval.reverse();
  for (const [name, items] of Object.entries(lanes)) {
    const lane = $(`.lane[data-lane="${name}"]`, board);
    const body = $("[data-body]", lane);
    body.innerHTML = items.length
      ? items.map((x) => (name === "approval" ? approvalCard(x) : invoiceCard(x))).join("")
      : `<div class="lane-empty">${{ approval: "Nothing waiting for you.", open: "No open invoices.", overdue: "Nothing overdue.", paid: "No payments yet." }[name]}</div>`;
    const total = name === "approval" ? items.reduce((s, d) => s + (decisionAmount(d) || 0), 0)
      : name === "paid" ? items.reduce((s, i) => s + (i.total_pence || 0), 0) : items.reduce((s, i) => s + remaining(i), 0);
    $("[data-total]", lane).textContent = items.length ? `${items.length} · ${money(total)}` : "";
  }
  for (const el of $$(".card[data-key]", board)) {
    const prev = before.get(el.dataset.key);
    if (!prev) continue;
    const now = el.getBoundingClientRect();
    const dx = prev.left - now.left, dy = prev.top - now.top;
    if (Math.abs(dx) > 2 || Math.abs(dy) > 2) {
      el.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: "none" }], { duration: 420, easing: "cubic-bezier(.2,.8,.2,1)" });
    }
  }
  S.glow.clear();
}

const ACTOR = { desk: "D", inbox: "@", stripe: "S", billing: "B", collections: "C", agent: "A", human: "H", system: "§" };
const OUTCOME = { auto: "Auto", needs_approval: "Needs approval", hold: "Hold", deny: "Denied", none: "Noted" };

function renderTimeline() {
  const items = [...S.decisions.values()].reverse();
  $("#count-timeline").textContent = items.length;
  $("#timeline").innerHTML = items.length ? items.map((d) => {
    const amt = decisionAmount(d);
    const quote = (d.evidence || []).find((e) => e.quote);
    const status = d.status && d.status !== "executed" ? `<span class="tag">${esc(d.status.replace("_", " "))}</span>` : "";
    return `<div class="event">
      <div class="event-top"><span class="actor" title="${esc(d.actor)}">${ACTOR[d.actor] || "·"}</span>
        <span class="event-sum">${esc(d.summary)}</span>
        <button class="btn btn-sm btn-ghost why-btn" data-why="${esc(d.id)}">Why?</button></div>
      <div class="event-meta"><span class="chip ${esc(d.outcome)}">${OUTCOME[d.outcome] || esc(d.outcome)}</span>
        ${(d.clauses || []).map((c) => `<span class="chip ${esc(d.outcome)}">${esc(c)}</span>`).join("")}
        ${amt != null ? `<span class="money" style="font-size:13px">${money(amt)}</span>` : ""}
        ${status}<span class="when">${longDate(d.clock_date)} · ${esc(d.actor)}</span></div>
      ${d.reasons && d.reasons.length ? `<div class="event-body">${esc(d.reasons.join(" · "))}</div>` : ""}
      ${decisionFormula(d) && d.action !== "raise_invoice" ? `<div class="event-body formula">${esc(decisionFormula(d))}</div>` : ""}
      ${quote ? `<div class="quote">“${esc(quote.quote)}”</div>` : ""}
    </div>`;
  }).join("") : `<div class="muted" style="font-size:14px">Every decision the agent takes appears here, with the policy clause, the evidence and the maths.</div>`;
}

function renderMail() {
  const items = [...S.mail.values()].sort((a, b) => String(b.received_at || b.sent_at || "").localeCompare(String(a.received_at || a.sent_at || "")));
  $("#count-mail").textContent = items.length;
  $("#mail").innerHTML = items.map((m) => {
    const inbound = m.direction === "in";
    const cls = m.classification || {};
    const chips = inbound ? (cls.intents || []).map((i) => `<span class="chip ${i === "dispute" ? "hold" : i === "billing_request" ? "auto" : "none"}">${esc(i.replace(/_/g, " "))}</span>`).join("")
      + (cls.contains_instructions_to_agent ? `<span class="chip deny">injection</span>` : "") : "";
    const who = inbound ? `From ${esc(m.from)}` : `To ${esc(m.to)} · ${esc(m.kind || "")} · ${m.mode === "gmail" ? "sent via Gmail" : "outbox only"}`;
    return `<div class="mail">
      <div class="mail-top"><span class="dir ${inbound ? "in" : "out"}">${inbound ? "IN" : "OUT"}</span><span class="mail-subj">${esc(m.subject)}</span></div>
      <div class="mail-who">${who}</div>
      ${chips ? `<div class="event-meta" style="margin-left:0">${chips}</div>` : ""}
      <div class="mail-body" title="Click to expand">${esc(m.body)}</div>
    </div>`;
  }).join("") || `<div class="muted" style="font-size:14px">Invoices, chases and customer replies appear here.</div>`;
}

function renderMemory() {
  $("#count-memory").textContent = S.memory.length;
  $("#memory").innerHTML = S.memory.slice().reverse().map((e) => `<div class="mem">
      <span class="op ${esc(e.op)}">${esc(e.op)}</span>
      <div><span class="mem-cust">${esc(custName(e.customer))}</span> <span class="tag">${esc(e.backend || S.modes.memory || "")}</span>
      <div class="mem-detail">${esc(e.detail)}</div></div></div>`).join("")
    || `<div class="muted" style="font-size:14px;margin-top:10px">Each customer has its own memory. Every email, decision and payment is remembered, recalled when the agent explains itself, improved by your approvals, and can be forgotten on request.</div>`;
}

function renderSelects() {
  const opts = [...S.customers.values()].map((c) => `<option value="${esc(c.id)}" data-email="${esc(c.email)}">${esc(c.name)}</option>`).join("");
  for (const sel of [$("#inject-from"), $("#ask-cust")]) {
    if (sel.dataset.sig === opts) continue;
    const keep = sel.value;
    sel.innerHTML = opts; sel.dataset.sig = opts;
    if (keep) sel.value = keep;
  }
}

/* ---------------- chat ---------------- */
function addMsg(role, html, extra = "") {
  const list = $("#chat");
  const empty = $(".empty-chat", list);
  if (empty) empty.remove();
  const el = document.createElement("div");
  el.className = `msg ${role} ${extra}`;
  el.innerHTML = html;
  list.appendChild(el);
  list.parentElement.scrollTop = list.parentElement.scrollHeight;
  return el;
}

function emptyChat() {
  const examples = [
    "Just finished at Bright Bakery: four hours labour, two RCDs and the call-out.",
    "Who owes me money?",
    "Why did you charge Acme £93.18?",
  ];
  $("#chat").innerHTML = `<div class="empty-chat">Tell Clause what you did, by voice or text. It prices the work from your rate card, checks your policy, raises the Stripe invoice and emails it.
    <div style="display:grid;gap:6px;margin-top:10px">${examples.map((x) => `<a href="#" data-example="${esc(x)}"><code>${esc(x)}</code></a>`).join("")}</div></div>`;
}

async function sendChat(message, mode = "text") {
  message = message.trim();
  if (!message) return;
  addMsg("user", `<span class="via">${mode === "voice" ? "Voice note" : "Typed"}</span>${esc(message)}`);
  const thinking = addMsg("agent", "Working…", "thinking");
  S.pendingChat++;
  try {
    const res = await api("POST", "/api/chat", { message, mode });
    thinking.classList.remove("thinking");
    const cards = (res.invoice_ids || []).map((id) => S.invoices.get(id)).filter(Boolean)
      .map((inv) => `<div class="inline-card">${invoiceCard(inv, true)}</div>`).join("");
    thinking.innerHTML = esc(res.reply) + cards;
  } catch (err) {
    thinking.classList.remove("thinking");
    thinking.innerHTML = `<span style="color:var(--red-text)">${esc(err.message)}</span>`;
  } finally {
    S.pendingChat--;
  }
}

/* ---------------- voice ---------------- */
function setupVoice() {
  const R = window.SpeechRecognition || window.webkitSpeechRecognition;
  const mic = $("#mic");
  if (!R) { mic.classList.add("hidden"); return; }
  let rec = null;
  mic.addEventListener("click", () => {
    if (rec) { rec.stop(); return; }
    rec = new R();
    rec.lang = "en-GB"; rec.interimResults = true; rec.continuous = false; rec.maxAlternatives = 1;
    let finalText = "";
    rec.onresult = (e) => {
      let interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        if (e.results[i].isFinal) finalText += e.results[i][0].transcript; else interim += e.results[i][0].transcript;
      }
      $("#input").value = (finalText + interim).trim();
    };
    rec.onerror = (e) => toast(`Mic: ${e.error}`, "error");
    rec.onend = () => {
      mic.classList.remove("on"); rec = null;
      const text = $("#input").value.trim();
      if (finalText.trim() && text) { $("#input").value = ""; sendChat(text, "voice"); }
    };
    mic.classList.add("on");
    rec.start();
  });
}

/* ---------------- why ---------------- */
async function why(decisionId) {
  const d = S.decisions.get(decisionId);
  if (!d) return;
  const question = `Why: ${d.summary}?`;
  $("#why-q").textContent = question;
  $("#why-body").innerHTML = `<div class="answer muted">Checking the decision records and asking ${esc(S.modes.memory === "cognee" ? "Cognee" : "memory")}…</div>`;
  $("#why").showModal();
  try {
    const res = await api("POST", "/api/why", { question, customer: d.customer });
    const recs = (res.records || []).filter((r) => r.summary).slice(0, 6);
    const mem = res.memory || [];
    $("#why-body").innerHTML = `<div class="answer">${esc(res.answer)}</div>
      ${recs.length ? `<div class="dlg-section">Decision records</div>${recs.map((r) => `<div class="rec">${(r.clauses || []).map((c) => `<span class="chip ${esc(r.outcome || "none")}">${esc(c)}</span> `).join("")}${esc(r.summary)}${(r.stripe_ids || []).length ? ` <span class="muted">· ${esc(r.stripe_ids.join(", "))}</span>` : ""}</div>`).join("")}` : ""}
      ${mem.length ? `<div class="dlg-section">Recalled from ${esc(S.modes.memory === "cognee" ? "Cognee" : "memory")}</div>${mem.slice(0, 6).map((m) => `<div class="hit" style="margin-top:6px">${esc(typeof m === "string" ? m : m.text)}</div>`).join("")}` : ""}`;
  } catch (err) {
    $("#why-body").innerHTML = `<div class="answer" style="color:var(--red-text)">${esc(err.message)}</div>`;
  }
}

/* ---------------- toasts ---------------- */
function toast(text, level = "info") {
  const el = document.createElement("div");
  el.className = `toast ${level}`;
  el.textContent = text;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), level === "money" ? 6000 : 4500);
}

/* ---------------- events ---------------- */
function onEvent(type, data) {
  switch (type) {
    case "decision": {
      const prev = S.decisions.get(data.id);
      S.decisions.set(data.id, { ...(prev || {}), ...data });
      if (!prev && data.status === "pending_approval") S.glow.add("dec:" + data.id);
      break;
    }
    case "invoice": {
      const prev = S.invoices.get(data.id);
      S.invoices.set(data.id, { ...(prev || {}), ...data });
      if (!prev || (prev.status !== "paid" && data.status === "paid")) S.glow.add("inv:" + data.id);
      if (prev && prev.status !== "paid" && data.status === "paid") toast(`${custName(data.customer)} paid ${money(data.total_pence)}`, "money");
      break;
    }
    case "email": {
      const dir = data.direction || (data.received_at ? "in" : "out");
      S.mail.set(`${dir}:${data.id}`, { ...data, direction: dir });
      break;
    }
    case "memory": S.memory.push(data); if (S.memory.length > 300) S.memory.shift(); break;
    case "clock": S.clock = data.clock_date; break;
    case "chat": if (S.pendingChat === 0) addMsg("agent", esc(data.text)); break;
    case "toast": toast(data.text, data.level === "error" ? "error" : data.level === "money" ? "money" : "info"); break;
    case "customer": S.customers.set(data.id, data); break;
    default: return;
  }
  render();
}

function connect() {
  const src = window.MOCK ? window.MOCK.events() : new EventSource("/api/events");
  for (const type of ["decision", "invoice", "email", "memory", "clock", "chat", "toast", "customer"]) {
    src.addEventListener(type, (e) => { try { onEvent(type, JSON.parse(e.data)); } catch (err) { console.error(type, err); } });
  }
  if (!window.MOCK) {
    let lost = false;
    src.onerror = () => { if (!lost) { lost = true; toast("Live updates lost; reconnecting…", "error"); } };
    src.onopen = async () => { if (lost) { lost = false; await refresh(); toast("Live again"); } };
  }
}

async function refresh() {
  load(await api("GET", "/api/state"));
  render();
}

/* ---------------- actions ---------------- */
async function busy(btns, fn) {
  btns.forEach((b) => (b.disabled = true));
  $("#progress").classList.add("on");
  try { return await fn(); } catch (err) { toast(err.message, "error"); } finally {
    btns.forEach((b) => (b.disabled = false));
    $("#progress").classList.remove("on");
  }
}

function wire() {
  $("#composer").addEventListener("submit", (e) => { e.preventDefault(); const v = $("#input").value; $("#input").value = ""; sendChat(v, "text"); });
  $("#chat").addEventListener("click", (e) => { const a = e.target.closest("[data-example]"); if (a) { e.preventDefault(); sendChat(a.dataset.example, "text"); } });

  $$("[data-advance]").forEach((b) => b.addEventListener("click", () => busy($$("[data-advance]"), async () => {
    const days = Number(b.dataset.advance);
    const res = await api("POST", "/api/sim/advance", { days });
    if (res && res.clock_date) S.clock = res.clock_date;
    const n = (res && res.decisions || []).length;
    toast(`Clock moved ${days} days to ${longDate(S.clock)}. ${n} decision${n === 1 ? "" : "s"} taken.`);
    await refresh();
  })));

  $("#poll").addEventListener("click", () => busy([$("#poll")], async () => {
    const res = await api("POST", "/api/mail/poll");
    const n = (res.emails || []).length;
    toast(n ? `${n} new email${n === 1 ? "" : "s"}` : "No new email");
  }));

  $("#reset").addEventListener("click", () => {
    if (!confirm("Reset the demo? This wipes invoices, decisions and memory and reseeds the Stripe test clock.")) return;
    busy([$("#reset")], async () => {
      await api("POST", "/api/sim/reset");
      Object.keys(kpiShown).forEach((k) => delete kpiShown[k]);
      emptyChat();
      await refresh();
      toast("Demo reset");
    });
  });

  document.addEventListener("click", async (e) => {
    const t = e.target.closest("[data-approve],[data-reject],[data-why],[data-pay],.mail-body");
    if (!t) return;
    if (t.classList.contains("mail-body")) { t.classList.toggle("open"); return; }
    e.preventDefault();
    if (t.dataset.why) return why(t.dataset.why);
    if (t.dataset.pay) {
      return busy([], async () => { await api("POST", `/api/sim/pay/${encodeURIComponent(t.dataset.pay)}`); });
    }
    const id = t.dataset.approve || t.dataset.reject;
    const approve = !!t.dataset.approve;
    await busy([t], async () => {
      const d = await api("POST", `/api/approvals/${encodeURIComponent(id)}`, { approve, note: "" });
      onEvent("decision", d);
      toast(d.status === "failed" ? `Failed: ${d.error}` : `${approve ? "Approved" : "Rejected"}: ${d.summary}`, d.status === "failed" ? "error" : "info");
    });
  });

  $("#tabs").addEventListener("click", (e) => {
    const tab = e.target.closest("[data-tab]");
    if (!tab) return;
    $$(".tab").forEach((t) => t.classList.toggle("on", t === tab));
    $$("[data-pane]").forEach((p) => p.classList.toggle("hidden", p.dataset.pane !== tab.dataset.tab));
  });

  $("#inject").addEventListener("submit", (e) => {
    e.preventDefault();
    const sel = $("#inject-from");
    const from = sel.selectedOptions[0] && sel.selectedOptions[0].dataset.email;
    const body = $("#inject-body").value.trim();
    if (!from || !body) return;
    busy([$("#inject button")], async () => {
      const res = await api("POST", "/api/mail/inject", { from, subject: $("#inject-subject").value.trim() || "Re: invoice", body });
      $("#inject-body").value = ""; $("#inject-subject").value = "";
      const n = (res.decisions || []).length;
      toast(`Email from ${sel.selectedOptions[0].textContent} handled: ${n} decision${n === 1 ? "" : "s"}`);
    });
  });

  $("#ask").addEventListener("submit", (e) => {
    e.preventDefault();
    const cust = $("#ask-cust").value, q = $("#ask-q").value.trim();
    if (!cust || !q) return;
    busy([], async () => {
      const res = await api("GET", `/api/memory/${encodeURIComponent(cust)}?q=${encodeURIComponent(q)}`);
      const hits = res.hits || [];
      $("#ask-hits").innerHTML = hits.length ? hits.map((h) => `<div class="hit">${esc(typeof h === "string" ? h : h.text)}</div>`).join("")
        : `<div class="muted" style="font-size:13px">No memory of that for ${esc(custName(cust))}.</div>`;
    });
  });

  $("#why-close").addEventListener("click", () => $("#why").close());
  $("#why").addEventListener("click", (e) => { if (e.target === $("#why")) $("#why").close(); });
}

/* ---------------- boot ---------------- */
(async function boot() {
  wire();
  setupVoice();
  emptyChat();
  try {
    await refresh();
  } catch (err) {
    toast(`Backend not reachable: ${err.message}`, "error");
  }
  connect();
  S.ready = true;
})();
