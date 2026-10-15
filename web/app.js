"use strict";
/* Clause UI. Calm lists up front; every detail (lines, maths, evidence, clauses, Stripe IDs, "why")
   one click away in the side sheet. State from GET /api/state, then live over SSE (/api/events). */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (p) => "£" + (Number(p || 0) / 100).toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const toDate = (iso) => new Date(String(iso).slice(0, 10) + "T12:00:00Z");
const fmtDate = (iso) => (iso ? toDate(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }) : "—");
const shortDate = (iso) => (iso ? toDate(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short" }) : "—");
const daysBetween = (a, b) => Math.round((toDate(b) - toDate(a)) / 86400000);

const S = {
  business: "", clock: null, modes: {},
  customers: new Map(), invoices: new Map(), decisions: new Map(), mail: new Map(), memory: [],
  glow: new Set(), ready: false, pendingChat: 0, filter: "all", drawer: null,
};

/* ---------------- API ---------------- */
async function api(method, path, body) {
  if (window.MOCK) return window.MOCK.handle(method, path, body);
  const res = await fetch(path, { method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
  if (!res.ok) {
    let text = await res.text();
    try { text = JSON.parse(text).detail || text; } catch (_) { /* plain text */ }
    throw new Error(typeof text === "string" ? text : JSON.stringify(text));
  }
  return res.json();
}

/* ---------------- derived ---------------- */
const cust = (key) => S.customers.get(key) || { name: key || "Unknown" };
const custName = (key) => cust(key).name || key;
const remaining = (inv) => inv.remaining_pence ?? inv.total_pence ?? 0;
const isOverdue = (inv) => inv.status === "open" && (inv.overdue || (S.clock && inv.due_date && S.clock > inv.due_date.slice(0, 10)));
const daysOverdue = (inv) => (S.clock && inv.due_date ? Math.max(0, daysBetween(inv.due_date, S.clock)) : 0);
const stateOf = (inv) => (inv.status === "paid" ? "paid" : inv.status === "void" || inv.status === "uncollectible" ? "void" : isOverdue(inv) ? "overdue" : "open");

function initials(name) {
  const w = String(name || "?").replace(/\b(ltd|limited|co|plc|llp)\b\.?/gi, "").trim().split(/\s+/);
  return ((w[0] || "?")[0] + (w[1] ? w[1][0] : "")).toUpperCase();
}
const PALETTE = [["#E6FFF0", "#00794A"], ["#EAF0FF", "#1F4FD1"], ["#FFF2D9", "#8F4200"], ["#FFECF1", "#B3093C"], ["#F1EAFF", "#5B2DB3"]];
function avatar(key, small = false, label = null) {
  const name = label ?? custName(key);
  let h = 0;
  for (const ch of String(key || name)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  const [bg, fg] = PALETTE[h % PALETTE.length];
  return `<span class="avatar ${small ? "sm" : ""}" style="background:${bg};color:${fg}" aria-hidden="true">${esc(label ?? initials(name))}</span>`;
}

function decisionAmount(d) {
  const c = d.calc || {};
  if (c.total_pence != null) return c.total_pence;
  if (c.interest_pence != null) return (c.interest_pence || 0) + (c.compensation_pence || 0);
  if (c.amount_pence != null) return c.amount_pence;
  return null;
}
const decisionFormula = (d) => {
  const c = d.calc || {};
  return typeof c.formula === "string" ? c.formula : Array.isArray(c.lines) ? c.lines.join("\n") : "";
};
const OUTCOME = { auto: "Auto", needs_approval: "Approval", hold: "On hold", deny: "Refused", none: "Noted" };
const SOURCE = { voice: "Voice note", chat: "Chat", email: "Customer email", seed: "Contract", agent: "Agent" };

function load(state) {
  S.business = state.business || "";
  S.scenario = state.scenario || "full";
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
  if (S.scenario === "demo") S.step = Math.max(S.step || 0, demoProgress());
}

/* How far the one-customer demo has got, read from the data (survives a page reload). */
function demoProgress() {
  const mine = [...S.invoices.values()].filter((i) => i.customer === FOCUS);
  const work = mine.filter((i) => (i.kind || "work") === "work");
  if (!work.length) return 0;
  const interest = mine.some((i) => i.kind === "interest");
  if (!interest && !work.some(isOverdue)) return 1;
  if (!work.some((i) => i.promise_date)) return 2;
  if (mine.some((i) => i.status === "open")) return 3;
  return 4;
}

/* ---------------- demo focus: one customer, start to finish ---------------- */
const FOCUS = "bright";
const inFocus = (key) => S.scenario !== "demo" || key === FOCUS;

/* ---------------- render ---------------- */
let queued = false;
function render() {
  if (queued) return;
  queued = true;
  requestAnimationFrame(() => {
    queued = false;
    renderTop(); renderDemo(); renderKpis(); renderBoard(); renderActivity(); renderMail(); renderMemory(); renderSelects();
    if (S.drawer) openDrawer(S.drawer.type, S.drawer.id, true);
    S.glow.clear();
  });
}

function renderTop() {
  $("#business").textContent = S.business;
  $("#clock").textContent = S.clock ? fmtDate(S.clock) : "—";
  const m = S.modes;
  const row = (k, v, ok) => `<div>${esc(k)}: <b style="color:${ok ? "var(--green)" : "var(--ink)"}">${esc(v)}</b></div>`;
  $("#status").innerHTML = row("Stripe", m.stripe === "test" ? "test mode" : "offline stand-in", m.stripe === "test")
    + row("Claude", String(m.llm || "").startsWith("fake") ? "offline stand-in" : m.llm, !String(m.llm || "fake").startsWith("fake"))
    + row("Memory", m.memory === "cognee" ? "Cognee" : "local", m.memory === "cognee")
    + row("Email", m.mail === "gmail" ? "Gmail" : "outbox only", m.mail === "gmail");
}

const shown = {};
function setMoney(id, pence) {
  const el = $("#" + id);
  const from = shown[id] ?? 0;
  shown[id] = pence;
  if (from === pence) { el.textContent = money(pence); return; }
  if (S.ready) { el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash"); }
  const t0 = performance.now();
  const step = (t) => {
    const k = Math.min(1, (t - t0) / 700), e = 1 - Math.pow(1 - k, 3);
    el.textContent = money(Math.round(from + (pence - from) * e));
    if (k < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function renderKpis() {
  let outstanding = 0, overdue = 0, collected = 0, interest = 0;
  for (const inv of S.invoices.values()) {
    if (!inFocus(inv.customer)) continue;
    if (inv.status === "open") { outstanding += remaining(inv); if (isOverdue(inv)) overdue += remaining(inv); }
    if (inv.status === "paid") collected += inv.total_pence || 0;
    if (inv.kind === "interest" && inv.status !== "void") interest += inv.total_pence || 0;
  }
  setMoney("kpi-outstanding", outstanding); setMoney("kpi-overdue", overdue);
  setMoney("kpi-collected", collected); setMoney("kpi-interest", interest);
}

function invoiceRow(inv, inChat = false) {
  const st = stateOf(inv);
  const label = st === "paid" ? "Paid" : st === "overdue" ? `${daysOverdue(inv)} days overdue` : st === "void" ? "Void" : `Due ${shortDate(inv.due_date)}`;
  const what = inv.kind === "interest" ? "Late-payment interest" : SOURCE[(inv.source || {}).type] || "Invoice";
  const flags = [inv.disputed ? "Disputed" : "", inv.promise_date && inv.status === "open" ? `Promised ${shortDate(inv.promise_date)}` : ""].filter(Boolean);
  return `<button class="row ${!inChat && S.glow.has("inv:" + inv.id) ? "glow" : ""}" data-open="inv:${esc(inv.id)}">
    ${avatar(inv.customer)}
    <span style="min-width:0"><span class="row-title">${esc(custName(inv.customer))}</span>
      <span class="row-sub">${esc(inv.number || inv.id)} · ${esc(what)}${flags.length ? " · " + esc(flags.join(" · ")) : ""}</span></span>
    <span class="row-end"><span class="row-amt">${money(inv.status === "open" ? remaining(inv) : inv.total_pence)}</span><span class="st ${st}">${esc(label)}</span></span>
  </button>`;
}

function approvalTile(d) {
  const amt = decisionAmount(d);
  const why = (d.reasons || []).slice(-1).join("");
  return `<div class="tile approval ${S.glow.has("dec:" + d.id) ? "glow" : ""}">
    <span class="tile-top">${avatar(d.customer, true)}<span class="tile-name">${esc(custName(d.customer))}</span>
      <span class="grow"></span>${amt != null ? `<span class="tile-amt" style="margin:0">${money(amt)}</span>` : ""}</span>
    <span class="st approval">Needs your approval: ${esc(why)}</span>
    <span class="btns"><button class="btn primary sm" data-approve="${esc(d.id)}">${d.proposal ? "Approve" : "Resolve"}</button>
      <button class="btn sm" data-reject="${esc(d.id)}">Reject</button>
      <button class="linkbtn" style="padding:6px 2px" data-open="dec:${esc(d.id)}">Details</button></span>
  </div>`;
}

function invoiceTile(inv) {
  const st = stateOf(inv);
  const label = st === "paid" ? "Paid" : st === "overdue" ? `${daysOverdue(inv)} days overdue` : `Due ${shortDate(inv.due_date)}`;
  const what = inv.kind === "interest" ? "Interest" : SOURCE[(inv.source || {}).type] || "Invoice";
  const flag = inv.disputed ? " · Disputed" : inv.promise_date && inv.status === "open" ? ` · Promised ${shortDate(inv.promise_date)}` : "";
  return `<button class="tile ${S.glow.has("inv:" + inv.id) ? "glow" : ""}" data-key="inv:${esc(inv.id)}" data-open="inv:${esc(inv.id)}">
    <span class="tile-top">${avatar(inv.customer, true)}<span class="tile-name">${esc(custName(inv.customer))}</span></span>
    <span class="tile-amt">${money(inv.status === "open" ? remaining(inv) : inv.total_pence)}</span>
    <span class="st ${st}">${esc(label)}</span>
    <span class="tile-sub">${esc(inv.number || inv.id)} · ${esc(what)}${esc(flag)}</span>
  </button>`;
}

function renderBoard() {
  const board = $("#board");
  const before = new Map($$("[data-key]", board).map((el) => [el.dataset.key, el.getBoundingClientRect()]));
  const lanes = { open: [], overdue: [], paid: [] };
  const pending = [...S.decisions.values()].filter((d) => d.status === "pending_approval" && inFocus(d.customer)).reverse();
  $("#approvals").innerHTML = pending.map(approvalTile).join("");
  for (const inv of [...S.invoices.values()].reverse().filter((i) => inFocus(i.customer))) { const st = stateOf(inv); if (lanes[st]) lanes[st].push(inv); }
  lanes.overdue.sort((a, b) => daysOverdue(b) - daysOverdue(a));
  const empty = { open: "No open invoices", overdue: "Nothing overdue", paid: "No payments yet" };
  for (const [name, items] of Object.entries(lanes)) {
    const lane = $(`.lane[data-lane="${name}"]`, board);
    $("[data-body]", lane).innerHTML = items.length
      ? items.map(invoiceTile).join("")
      : `<div class="lane-empty">${empty[name]}</div>`;
    $("[data-n]", lane).textContent = items.length;
  }
  for (const el of $$("[data-key]", board)) {
    const prev = before.get(el.dataset.key);
    if (!prev) continue;
    const now = el.getBoundingClientRect();
    const dx = prev.left - now.left, dy = prev.top - now.top;
    if (Math.abs(dx) > 2 || Math.abs(dy) > 2) el.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: "none" }], { duration: 420, easing: "cubic-bezier(.2,.8,.2,1)" });
  }
}

const ACTOR_LABEL = { desk: "Desk", inbox: "Inbox", stripe: "Stripe", billing: "Billing", collections: "Collections", agent: "Collections", human: "You", system: "Setup" };
function renderActivity() {
  const items = [...S.decisions.values()].reverse().filter((d) => inFocus(d.customer));
  $("#n-activity").textContent = items.length || "";
  $("#activity").innerHTML = items.map((d) => {
    const amt = decisionAmount(d);
    const who = ACTOR_LABEL[d.actor] || d.actor;
    return `<li><button class="row" data-open="dec:${esc(d.id)}">
      ${avatar(d.actor, true, who.slice(0, 2))}
      <span style="min-width:0"><span class="row-title clamp">${esc(d.summary)}</span>
        <span class="row-sub"><span class="st ${esc(d.status === "pending_approval" ? "needs_approval" : d.outcome)}" style="display:inline">${esc(d.status === "rejected" ? "Rejected" : d.status === "failed" ? "Failed" : OUTCOME[d.outcome] || d.outcome)}</span> · ${esc((d.clauses || []).join(", "))} · ${shortDate(d.clock_date)}</span></span>
      <span class="row-end">${amt != null ? `<span class="row-amt">${money(amt)}</span>` : ""}</span>
    </button></li>`;
  }).join("") || `<li class="empty">Every decision appears here with its policy clause, evidence and maths.</li>`;
}

function renderMail() {
  const items = [...S.mail.entries()].filter(([, m]) => inFocus(m.customer)).sort((a, b) => String(b[1].received_at || b[1].sent_at || "").localeCompare(String(a[1].received_at || a[1].sent_at || "")));
  $("#n-mail").textContent = items.length || "";
  $("#mail").innerHTML = items.map(([key, m]) => {
    const inbound = m.direction === "in";
    const intents = inbound ? ((m.classification || {}).intents || []).map((i) => i.replace(/_/g, " ")).join(", ") : (m.kind || "");
    return `<li><button class="row" data-open="mail:${esc(key)}">
      ${avatar(m.customer, true, inbound ? "In" : "Out")}
      <span style="min-width:0"><span class="row-title">${esc(m.subject)}</span>
        <span class="row-sub">${inbound ? "From" : "To"} ${esc(custName(m.customer) || (inbound ? m.from : m.to))}${intents ? " · " + esc(intents) : ""}</span></span>
      <span class="row-end"><span class="st ${inbound ? "open" : "auto"}">${inbound ? "Received" : m.mode === "gmail" ? "Sent" : "Outbox"}</span></span>
    </button></li>`;
  }).join("") || `<li class="empty">Invoices, reminders and customer replies appear here.</li>`;
}

function renderMemory() {
  const mem = S.memory.filter((e) => inFocus(e.customer));
  $("#n-memory").textContent = mem.length || "";
  const label = { remember: "Remembered", recall: "Recalled", improve: "Learned", forget: "Forgot" };
  $("#memory").innerHTML = mem.slice().reverse().slice(0, 120).map((e) => `<li><div class="row" style="cursor:default">
      ${avatar(e.customer, true)}
      <span style="min-width:0"><span class="row-title">${esc(label[e.op] || e.op)} · ${esc(custName(e.customer))}</span>
        <span class="row-sub clamp">${esc(e.detail)}</span></span>
      <span class="row-end"><span class="st none">${esc(e.backend || S.modes.memory || "")}</span></span></div></li>`).join("")
    || `<li class="empty">Each customer has its own memory: every email, decision and payment.</li>`;
}

function renderSelects() {
  const opts = [...S.customers.values()].filter((c) => inFocus(c.id)).map((c) => `<option value="${esc(c.id)}" data-email="${esc(c.email)}">${esc(c.name)}</option>`).join("");
  for (const sel of [$("#inject-from"), $("#ask-cust")]) {
    if (sel.dataset.sig === opts) continue;
    const keep = sel.value;
    sel.innerHTML = opts; sel.dataset.sig = opts;
    if (keep) sel.value = keep;
  }
}

/* ---------------- drawer (all the detail) ---------------- */
function relatedDecisions(inv) {
  return [...S.decisions.values()].filter((d) => d.invoice === inv.id || (d.stripe_ids || []).includes(inv.id)
    || ((d.proposal || {}).args || {}).invoice_id === inv.id).reverse();
}

function decisionBlock(d) {
  return `<button class="row" data-open="dec:${esc(d.id)}">${avatar(d.actor, true, (ACTOR_LABEL[d.actor] || d.actor).slice(0, 2))}
    <span style="min-width:0"><span class="row-title clamp">${esc(d.summary)}</span><span class="row-sub">${esc((d.clauses || []).join(", "))}</span></span>
    <span class="row-end"><span class="st ${esc(d.outcome)}">${esc(OUTCOME[d.outcome] || d.outcome)}</span></span></button>`;
}

function invoiceDrawer(inv) {
  const st = stateOf(inv);
  const c = cust(inv.customer);
  const src = inv.source || {};
  const parent = inv.parent_invoice && S.invoices.get(inv.parent_invoice);
  const decs = relatedDecisions(inv);
  const notes = [];
  if (inv.disputed) notes.push(`Disputed${inv.dispute_quote ? `: “${esc(inv.dispute_quote)}”` : ""}. Chasing and interest paused until you resolve it (CC-3).`);
  if (inv.promise_date) notes.push(`Promised to pay by ${fmtDate(inv.promise_date)}${inv.promise_quote ? `: “${esc(inv.promise_quote)}”` : ""} (CC-4).`);
  if ((inv.chases || []).length) notes.push(`${inv.chases.length} reminder${inv.chases.length > 1 ? "s" : ""} sent: ${inv.chases.map((x) => fmtDate(x.sent_on)).join(", ")} (CC-2).`);
  return {
    title: `${inv.number || inv.id} · ${inv.kind === "interest" ? "Late-payment interest" : "Invoice"}`,
    html: `<div class="d-title">${esc(c.name)}</div>
      <div class="d-amount">${money(inv.status === "open" ? remaining(inv) : inv.total_pence)}</div>
      <div class="st ${st}" style="font-size:15px">${st === "paid" ? `Paid${inv.paid_at ? " " + fmtDate(inv.paid_at) : ""}` : st === "overdue" ? `${daysOverdue(inv)} days overdue` : `Due ${fmtDate(inv.due_date)}`}</div>
      <div class="d-section links">
        <button class="btn neon sm" data-why-inv="${esc(inv.id)}">Why this amount?</button>
        ${inv.status === "open" ? `<button class="btn sm" data-pay="${esc(inv.id)}">Pay with test card</button>` : ""}
        ${inv.hosted_invoice_url ? `<a class="btn sm" href="${esc(inv.hosted_invoice_url)}" target="_blank" rel="noopener">Pay page ↗</a>` : ""}
        <a class="btn sm" href="https://dashboard.stripe.com/test/invoices/${encodeURIComponent(inv.id)}" target="_blank" rel="noopener">Stripe ↗</a>
      </div>
      <div id="why-out"></div>
      <div class="d-section"><h3>Lines</h3><div class="lines">${(inv.lines || []).map((l) => `<div class="line"><span>${esc(l.description)}</span><span>${money(l.amount_pence)}</span></div>`).join("")}
        <div class="line" style="background:none;padding-top:2px"><span><b>Total</b></span><span>${money(inv.total_pence)}</span></div></div></div>
      ${notes.length ? `<div class="d-section"><h3>Status notes</h3>${notes.map((n) => `<p style="margin:0 0 6px">${n}</p>`).join("")}</div>` : ""}
      <div class="d-section"><h3>Where it came from</h3>
        <dl class="kv"><dt>Source</dt><dd>${esc(SOURCE[src.type] || src.type || "—")}</dd>
        ${parent ? `<dt>Interest on</dt><dd>${esc(parent.number)} (${money(parent.total_pence)})</dd>` : ""}
        <dt>Issued to</dt><dd>${esc(c.email || "")}</dd><dt>Stripe ID</dt><dd class="num">${esc(inv.id)}</dd></dl>
        ${src.quote ? `<div class="quote">“${esc(src.quote)}”</div>` : ""}</div>
      ${decs.length ? `<div class="d-section"><h3>Decisions</h3><ul class="list">${decs.map((d) => `<li>${decisionBlock(d)}</li>`).join("")}</ul></div>` : ""}`,
  };
}

function decisionDrawer(d) {
  const amt = decisionAmount(d);
  const formula = decisionFormula(d);
  const inv = d.invoice && S.invoices.get(d.invoice);
  return {
    title: `${ACTOR_LABEL[d.actor] || d.actor} · ${fmtDate(d.clock_date)}`,
    html: `<div class="d-title">${esc(d.summary)}</div>
      ${amt != null ? `<div class="d-amount">${money(amt)}</div>` : ""}
      <div class="chips" style="margin-top:8px"><span class="chip ${esc(d.outcome)}">${esc(OUTCOME[d.outcome] || d.outcome)}</span>${(d.clauses || []).map((c) => `<span class="chip ${esc(d.outcome)}">${esc(c)}</span>`).join("")}${d.status && d.status !== "executed" ? `<span class="chip">${esc(d.status.replace("_", " "))}</span>` : ""}</div>
      ${d.status === "pending_approval" ? `<div class="d-section links"><button class="btn primary sm" data-approve="${esc(d.id)}">${d.proposal ? "Approve" : "Mark resolved"}</button><button class="btn sm" data-reject="${esc(d.id)}">Reject</button></div>` : ""}
      <div class="d-section links"><button class="btn neon sm" data-why-dec="${esc(d.id)}">Ask why</button></div>
      <div id="why-out"></div>
      ${(d.reasons || []).length ? `<div class="d-section"><h3>Because</h3>${d.reasons.map((r) => `<p style="margin:0 0 6px">${esc(r)}</p>`).join("")}</div>` : ""}
      ${formula ? `<div class="d-section"><h3>The maths</h3><div class="formula">${esc(formula)}</div></div>` : ""}
      ${(d.evidence || []).filter((e) => e.quote).length ? `<div class="d-section"><h3>Evidence</h3>${d.evidence.filter((e) => e.quote).map((e) => `<div class="quote">“${esc(e.quote)}”<div class="d-sub" style="font-style:normal">${esc(e.type)} ${esc(e.ref || "")}</div></div>`).join("")}</div>` : ""}
      <div class="d-section"><h3>Record</h3><dl class="kv"><dt>Customer</dt><dd>${esc(custName(d.customer))}</dd>
        ${inv ? `<dt>Invoice</dt><dd><button class="linkbtn" style="padding:0" data-open="inv:${esc(inv.id)}">${esc(inv.number)}</button></dd>` : ""}
        ${(d.stripe_ids || []).length ? `<dt>Stripe</dt><dd class="num">${esc(d.stripe_ids.join(", "))}</dd>` : ""}
        <dt>Policy</dt><dd>v${esc(d.policy_version || "1.0")}</dd><dt>Decision ID</dt><dd class="num">${esc(d.id)}</dd></dl></div>`,
  };
}

function mailDrawer(m) {
  const inbound = m.direction === "in";
  const cls = m.classification || {};
  return {
    title: inbound ? "Received email" : `Sent email · ${m.kind || ""}`,
    html: `<div class="d-title">${esc(m.subject)}</div>
      <div class="d-sub">${inbound ? `From ${esc(m.from)}` : `To ${esc(m.to)} · ${m.mode === "gmail" ? "sent via Gmail" : "outbox only"}`}</div>
      ${inbound && (cls.intents || []).length ? `<div class="chips" style="margin-top:10px">${cls.intents.map((i) => `<span class="chip ${i === "dispute" ? "hold" : "auto"}">${esc(i.replace(/_/g, " "))}</span>`).join("")}${cls.contains_instructions_to_agent ? `<span class="chip deny">instructions to the agent: refused</span>` : ""}</div>` : ""}
      <div class="d-section"><div class="mailbody">${esc(m.body)}</div></div>`,
  };
}

function openDrawer(type, id, refresh = false) {
  const obj = type === "inv" ? S.invoices.get(id) : type === "dec" ? S.decisions.get(id) : S.mail.get(id);
  if (!obj) return;
  if (refresh && $("#why-out") && $("#why-out").innerHTML) return; // keep an answer on screen
  const view = type === "inv" ? invoiceDrawer(obj) : type === "dec" ? decisionDrawer(obj) : mailDrawer(obj);
  S.drawer = { type, id };
  $("#drawer-title").textContent = view.title;
  $("#drawer-body").innerHTML = view.html;
  if (!refresh) {
    $("#drawer").hidden = false; $("#scrim").hidden = false;
    $("#drawer-body").scrollTop = 0;
    $("#drawer-close").focus();
  }
}
function closeDrawer() { S.drawer = null; $("#drawer").hidden = true; $("#scrim").hidden = true; }

async function askWhy(question, customer) {
  const out = $("#why-out");
  out.innerHTML = `<div class="d-section"><div class="answer wait">Checking the decision records and ${S.modes.memory === "cognee" ? "Cognee memory" : "memory"}…</div></div>`;
  try {
    const res = await api("POST", "/api/why", { question, customer });
    const mem = (res.memory || []).slice(0, 4);
    out.innerHTML = `<div class="d-section"><h3>Answer</h3><div class="answer">${esc(res.answer)}</div>
      ${mem.length ? `<h3 style="margin-top:14px">Recalled from ${S.modes.memory === "cognee" ? "Cognee" : "memory"}</h3>${mem.map((m) => `<div class="quote">${esc(typeof m === "string" ? m : m.text)}</div>`).join("")}` : ""}</div>`;
  } catch (err) {
    out.innerHTML = `<div class="d-section"><div class="answer" style="background:var(--red-bg);color:var(--red)">${esc(err.message)}</div></div>`;
  }
}

/* ---------------- chat + voice ---------------- */
function addMsg(role, html, extra = "") {
  const list = $("#chat");
  const hint = $(".hint", list);
  if (hint) hint.remove();
  const el = document.createElement("div");
  el.className = `msg ${role} ${extra}`;
  el.innerHTML = html;
  list.appendChild(el);
  list.parentElement.scrollTop = list.parentElement.scrollHeight;
  return el;
}

function hint() {
  const ex = ["Just finished at Bright Bakery: four hours labour, two RCDs and the call-out.", "Who owes me money?", "Why did you charge Acme £93.18?"];
  $("#chat").innerHTML = `<div class="hint">Tell VOIZ what you did. It prices the work from your rate card, checks your policy, raises the Stripe invoice and emails it.
    ${ex.map((x) => `<button type="button" data-example="${esc(x)}">${esc(x)}</button>`).join("")}</div>`;
}

async function sendChat(message, mode = "text") {
  message = message.trim();
  if (!message) return;
  addMsg("user", `<small>${mode === "voice" ? "Voice note" : "Typed"}</small>${esc(message)}`);
  const wait = addMsg("agent", "Working on it…", "wait");
  S.pendingChat++;
  try {
    showReply(wait, await api("POST", "/api/chat", { message, mode }));
  } catch (err) {
    showError(wait, err);
  } finally { S.pendingChat--; }
}

function showReply(el, res) {
  el.classList.remove("wait");
  const rows = (res.invoice_ids || []).map((id) => S.invoices.get(id)).filter(Boolean).map((inv) => `<div style="margin-top:10px">${invoiceTile(inv).replace(/data-key="[^"]*"/, "")}</div>`).join("");
  el.innerHTML = esc(res.reply) + rows;
}

function showError(el, err) {
  el.classList.remove("wait");
  el.innerHTML = `<span style="color:var(--red)">${esc(err.message)}</span>`;
}

/* The transcript shows in the input while you speak (browser speech API). Browsers without it record
   the audio and the server transcribes it with Whisper. Either way /api/voice/prompt summarises the
   note into one prompt for the desk; the chat shows the transcript, the prompt sent, and the reply. */
async function sendVoice(transcript) {
  transcript = transcript.trim();
  if (!transcript) return;
  if (window.MOCK) return sendChat(transcript, "voice");
  const said = addMsg("user", `<small>Voice note</small>${esc(transcript)}`);
  const wait = addMsg("agent", "Summarising your voice note…", "wait");
  S.pendingChat++;
  try {
    const res = await api("POST", "/api/voice/prompt", { transcript });
    if (res.summarised) said.insertAdjacentHTML("beforeend", `<div class="voice-prompt"><small>Prompt sent</small>${esc(res.prompt)}</div>`);
    showReply(wait, res);
  } catch (err) {
    showError(wait, err);
  } finally { S.pendingChat--; }
}

const micOn = (on) => { $("#mic").classList.toggle("on", on); $("#act-voice").classList.toggle("on", on); };

let rec = null;
async function toggleVoice() {
  const R = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (rec) { rec.stop(); return; }
  if (!R) return recordVoice();
  rec = new R();
  rec.lang = "en-GB"; rec.interimResults = true; rec.continuous = true;
  let finalText = "";
  rec.onresult = (e) => {
    let interim = "";
    for (let i = e.resultIndex; i < e.results.length; i++) {
      if (e.results[i].isFinal) finalText += e.results[i][0].transcript; else interim += e.results[i][0].transcript;
    }
    $("#input").value = (finalText + interim).trim();
  };
  rec.onerror = (e) => { if (e.error !== "no-speech") toast(`Microphone: ${e.error}`, "error"); };
  rec.onend = () => {
    micOn(false); rec = null;
    const text = $("#input").value.trim();
    if (finalText.trim() && text) { $("#input").value = ""; sendVoice(text); }
  };
  micOn(true);
  rec.start();
}

/* No speech API in this browser (e.g. Firefox): record, and let the server transcribe if it can. */
async function recordVoice() {
  const unsupported = () => { toast("Voice needs Chrome or Edge. Type instead.", "error"); $("#input").focus(); };
  if (window.MOCK || !navigator.mediaDevices || !window.MediaRecorder) return unsupported();
  try {
    if (!(await api("GET", "/api/voice/status")).server_transcription) return unsupported();
  } catch (_) { return unsupported(); }
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); } catch (err) { toast(`Microphone: ${err.message}`, "error"); return; }
  const chunks = [];
  rec = new MediaRecorder(stream);
  rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
  rec.onstop = async () => {
    const type = rec.mimeType || "audio/webm";
    stream.getTracks().forEach((t) => t.stop());
    micOn(false); rec = null;
    const input = $("#input");
    input.value = "Transcribing…"; input.disabled = true;
    try {
      const res = await fetch("/api/voice/transcribe", { method: "POST", headers: { "Content-Type": type }, body: new Blob(chunks, { type }) });
      if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
      const { transcript } = await res.json();
      input.value = "";
      if (transcript) sendVoice(transcript); else toast("Didn't catch that", "error");
    } catch (err) {
      input.value = "";
      toast(`Transcription: ${err.message}`, "error");
    } finally { input.disabled = false; }
  };
  micOn(true);
  rec.start();
}

/* ---------------- toasts ---------------- */
function toast(text, level = "info") {
  const el = document.createElement("div");
  el.className = `toast ${level}`;
  el.textContent = text;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), level === "money" ? 6000 : 4200);
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
    case "toast": if (!/paid/i.test(data.text || "")) toast(data.text, data.level === "error" ? "error" : "info"); break;
    default: return;
  }
  render();
}

function connect() {
  const src = window.MOCK ? window.MOCK.events() : new EventSource("/api/events");
  for (const type of ["decision", "invoice", "email", "memory", "clock", "chat", "toast"]) {
    src.addEventListener(type, (e) => { try { onEvent(type, JSON.parse(e.data)); } catch (err) { console.error(type, err); } });
  }
  if (!window.MOCK) {
    let lost = false;
    src.onerror = () => { if (!lost) { lost = true; toast("Live updates paused, reconnecting…", "error"); } };
    src.onopen = async () => { if (lost) { lost = false; await refresh(); } };
  }
}

async function refresh() { load(await api("GET", "/api/state")); render(); }

async function busy(btns, fn) {
  btns.forEach((b) => (b.disabled = true));
  try { return await fn(); } catch (err) { toast(err.message, "error"); } finally { btns.forEach((b) => (b.disabled = false)); }
}

function showTab(name) {
  for (const t of $$("#tabs [data-tab]")) t.setAttribute("aria-selected", String(t.dataset.tab === name));
  for (const p of $$("[data-pane]")) p.hidden = p.dataset.pane !== name;
}

function closeMenu() { $("#menu").hidden = true; $("#menu-btn").setAttribute("aria-expanded", "false"); }


/* ---------------- guided demo ---------------- */
const isoPlus = (iso, days) => { const d = toDate(iso); d.setUTCDate(d.getUTCDate() + days); return d.toISOString().slice(0, 10); };
const emailOf = (key) => (S.customers.get(key) || {}).email;
const findInv = (key, kind = "work", pred = () => true) => [...S.invoices.values()].find((i) => i.customer === key && (i.kind || "work") === kind && pred(i));

async function injectEmail(key, body, subject = "Re: invoice") {
  const res = await api("POST", "/api/mail/inject", { from: emailOf(key), subject, body });
  await refresh();
  return res;
}
async function doAdvance(days) {
  toast(`Skipping ahead ${days} days on the Stripe test clock…`);
  const res = await api("POST", "/api/sim/advance", { days });
  await refresh();
  const n = ((res && res.decisions) || []).length;
  toast(`${fmtDate(S.clock)}: ${n} decision${n === 1 ? "" : "s"} taken on their own`);
}

const DEMO = [
  { label: "Voice note → invoice", run: () => sendChat("Just finished at Bright Bakery: four hours labour, two RCDs and the call-out.", "voice") },
  { label: "Skip 45 days → overdue", run: async () => { await doAdvance(45); showTab("mail"); } },
  { label: "Customer replies", run: async () => {
      await injectEmail(FOCUS, `Sorry for the delay, we will pay by ${isoPlus(S.clock, 5)}.`);
      showTab("activity"); toast("Promise recorded. Reminders paused (CC-4).");
  } },
  { label: "Customer pays", run: async () => {
      const open = [...S.invoices.values()].filter((i) => i.customer === FOCUS && i.status === "open");
      if (!open.length) return toast("Nothing open to pay");
      for (const inv of open) await api("POST", `/api/sim/pay/${encodeURIComponent(inv.id)}`);
      await refresh();
  } },
  { label: "Why this interest?", run: async () => {
      const inv = findInv(FOCUS, "interest");
      if (!inv) return toast("No interest invoice yet: skip ahead first");
      openDrawer("inv", inv.id);
      await askWhy(`Why did you charge ${custName(inv.customer)} ${money(inv.total_pence)} on ${inv.number}?`, inv.customer);
  } },
];
S.step = 0;

function renderDemo() {
  if (S.scenario !== "demo") {
    $("#demo").innerHTML = `<span class="demo-label">Demo</span><button class="step current" data-demo-start><span class="step-n">▶</span>Start demo: one customer, start to finish</button>`;
    return;
  }
  $("#demo").innerHTML = `<span class="demo-label" title="Press the right-arrow key for the next step">Demo</span>` + DEMO.map((st, i) => {
    const cls = i < S.step ? "done" : i === S.step ? "current" : "";
    return `<button class="step ${cls}" data-step="${i}" ${i === S.step ? 'aria-current="step"' : ""}><span class="step-n">${i < S.step ? "✓" : i + 1}</span>${esc(st.label)}</button>`;
  }).join("") + `<span class="grow"></span><button class="linkbtn" data-demo-exit>Exit demo</button>`;
}

async function setScenario(scenario) {
  await busy([], async () => {
    toast(scenario === "demo" ? "Setting up a clean case study on Stripe…" : "Loading all customers and invoices…");
    await api("POST", "/api/sim/reset", { scenario });
    Object.keys(shown).forEach((k) => delete shown[k]);
    S.step = 0; hint(); closeDrawer();
    await refresh();
    toast(scenario === "demo" ? "Demo ready: Bright Bakery, start to finish" : "Full view");
  });
}

let demoBusy = false;
async function runStep(i) {
  if (demoBusy || i < 0 || i >= DEMO.length) return;
  demoBusy = true;
  $$("#demo .step").forEach((b) => (b.disabled = true));
  try { await DEMO[i].run(); S.step = Math.max(S.step, i + 1); } catch (err) { toast(err.message, "error"); }
  finally { demoBusy = false; renderDemo(); }
}

/* ---------------- wiring ---------------- */
function wire() {
  $("#composer").addEventListener("submit", (e) => { e.preventDefault(); const v = $("#input").value; $("#input").value = ""; sendChat(v, "text"); });
  $("#mic").addEventListener("click", toggleVoice);
  $("#act-voice").addEventListener("click", toggleVoice);
  $("#act-email").addEventListener("click", () => { showTab("mail"); $("#inject-body").focus(); });
  $("#tabs").addEventListener("click", (e) => { const t = e.target.closest("[data-tab]"); if (t) showTab(t.dataset.tab); });
  $("#menu-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    const open = $("#menu").hidden;
    $("#menu").hidden = !open; $("#menu-btn").setAttribute("aria-expanded", String(open));
  });
  document.addEventListener("click", (e) => { if (!e.target.closest("#menu")) closeMenu(); });

  for (const b of $$("[data-advance]")) b.addEventListener("click", () => {
    closeMenu();
    busy($$("[data-advance]"), async () => {
      const days = Number(b.dataset.advance);
      toast(`Skipping ahead ${days} days on the Stripe test clock…`);
      const res = await api("POST", "/api/sim/advance", { days });
      const n = ((res && res.decisions) || []).length;
      await refresh();
      toast(`${fmtDate(S.clock)}: ${n} decision${n === 1 ? "" : "s"} taken`);
    });
  });
  $("#m-inbox").addEventListener("click", () => { closeMenu(); busy([], async () => {
    const res = await api("POST", "/api/mail/poll");
    const n = (res.emails || []).length;
    toast(n ? `${n} new email${n === 1 ? "" : "s"}` : "No new email");
  }); });
  $("#m-reset").addEventListener("click", () => {
    closeMenu();
    if (!confirm("Reset the demo? This wipes invoices, decisions and memory and starts a fresh Stripe test clock.")) return;
    busy([], async () => { await api("POST", "/api/sim/reset", { scenario: S.scenario || "full" }); Object.keys(shown).forEach((k) => delete shown[k]); hint(); closeDrawer(); S.step = 0; renderDemo(); await refresh(); toast("Demo reset"); });
  });

  document.addEventListener("click", async (e) => {
    const t = e.target.closest("[data-open],[data-approve],[data-reject],[data-pay],[data-why-inv],[data-why-dec],[data-example]");
    if (!t) return;
    if (t.dataset.example) { e.preventDefault(); return sendChat(t.dataset.example, "text"); }
    if (t.dataset.open) { const [type, ...rest] = t.dataset.open.split(":"); return openDrawer(type, rest.join(":")); }
    if (t.dataset.whyInv) { const inv = S.invoices.get(t.dataset.whyInv); return askWhy(`Why did you charge ${custName(inv.customer)} ${money(inv.total_pence)} on ${inv.number}?`, inv.customer); }
    if (t.dataset.whyDec) { const d = S.decisions.get(t.dataset.whyDec); return askWhy(`Why: ${d.summary}?`, d.customer); }
    if (t.dataset.pay) return busy([t], async () => { await api("POST", `/api/sim/pay/${encodeURIComponent(t.dataset.pay)}`); await refresh(); });
    const id = t.dataset.approve || t.dataset.reject;
    const approve = !!t.dataset.approve;
    await busy([t], async () => {
      const d = await api("POST", `/api/approvals/${encodeURIComponent(id)}`, { approve, note: "" });
      onEvent("decision", d);
      await refresh();
      toast(d.status === "failed" ? `Failed: ${d.error}` : `${approve ? "Approved" : "Rejected"}: ${custName(d.customer)}`, d.status === "failed" ? "error" : "info");
    });
  });

  $("#inject").addEventListener("submit", (e) => {
    e.preventDefault();
    const sel = $("#inject-from");
    const from = sel.selectedOptions[0] && sel.selectedOptions[0].dataset.email;
    const body = $("#inject-body").value.trim();
    if (!from || !body) return;
    busy([$("#inject button")], async () => {
      const res = await api("POST", "/api/mail/inject", { from, subject: "Re: invoice", body });
      $("#inject-body").value = "";
      const n = (res.decisions || []).length;
      toast(`Email from ${sel.selectedOptions[0].textContent}: ${n} decision${n === 1 ? "" : "s"}`);
    });
  });

  $("#ask").addEventListener("submit", (e) => {
    e.preventDefault();
    const c = $("#ask-cust").value, q = $("#ask-q").value.trim();
    if (!c || !q) return;
    busy([], async () => {
      const res = await api("GET", `/api/memory/${encodeURIComponent(c)}?q=${encodeURIComponent(q)}`);
      const hits = res.hits || [];
      $("#ask-hits").innerHTML = hits.length ? hits.map((h) => `<div class="hit">${esc(typeof h === "string" ? h : h.text)}</div>`).join("")
        : `<div class="hit">No memory of that for ${esc(custName(c))}.</div>`;
    });
  });

  $("#demo").addEventListener("click", (e) => {
    if (e.target.closest("[data-demo-start]")) return setScenario("demo");
    if (e.target.closest("[data-demo-exit]")) return setScenario("full");
    const b = e.target.closest("[data-step]");
    if (b) runStep(Number(b.dataset.step));
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowRight" || S.scenario !== "demo" || e.target.closest("input, textarea, select") || !$("#drawer").hidden) return;
    e.preventDefault(); runStep(S.step);
  });
  $("#drawer-close").addEventListener("click", closeDrawer);
  $("#scrim").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeDrawer(); closeMenu(); } });
}

(async function boot() {
  wire();
  hint();
  renderDemo();
  try { await refresh(); } catch (err) { toast(`Backend not reachable: ${err.message}`, "error"); }
  connect();
  S.ready = true;
})();
