/* Fraud Command Center — employee workflow.
 *
 * One event stream (WebSocket /ws) feeds one store. Every page renders from that store, so the
 * investigation keeps building in the background while the employee stays on the dashboard.
 *   Dashboard (live monitoring) → toast notification → Investigation page → Countermeasures (approve/reject)
 * Nothing here decides anything: the backend investigates; the employee approves consequential actions.
 */
"use strict";

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const inr = (n) => "₹" + Math.round(Number(n) || 0).toLocaleString("en-IN");
const inrShort = (n) => {
  n = Number(n) || 0;
  if (n >= 1e7) return `₹${(n / 1e7).toFixed(2)} Cr`;
  if (n >= 1e5) return `₹${(n / 1e5).toFixed(1)} L`;
  if (n >= 1e3) return `₹${(n / 1e3).toFixed(1)}K`;
  return inr(n);
};
const pct = (x) => (x == null ? "—" : `${Math.round((Number(x) || 0) * 100)}%`);
const dur = (s) => (s == null ? "—" : s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${(s / 60).toFixed(1)} min` : `${(s / 3600).toFixed(1)} h`);

const STAGES = ["alert", "scout", "pattern", "prosecution", "defense", "verdict", "action"];
const AGENTS = { alert: "Alert", scout: "Graph scout", pattern: "Pattern detector", prosecutor: "Prosecutor",
  defense: "Defense", judge: "Judge", review: "Employee review", console: "Countermeasure console" };
const TIER = { 4: "Hard freeze + STR", 3: "Hold outbound (soft freeze)", 2: "Step-up verification", 1: "Enhanced monitoring", 0: "Cleared" };
const EV_TITLE = { burst: "Inflow burst", fan_out: "Abnormal fan-out", cycle: "Money cycle", shared_device: "Shared device",
  shared_ip: "Shared IP / VPN exit", dwell: "Rapid forwarding", genome_match: "Known ring shape",
  account_profile: "Defense: established account", relationship: "Defense: long-standing relationships",
  counterfactual: "Counterfactual impact", precedent: "Precedent" };
const STATUS = {
  investigating: ["Investigating", "st-inv"], awaiting: ["Awaiting approval", "st-wait"], review: ["Analyst review", "st-wait"],
  contained: ["Action taken", "st-done"], approved: ["Approved · executed", "st-done"], cleared: ["Cleared", "st-clear"],
  dismissed: ["Rejected by employee", "st-clear"],
};

/* ============================== Store ============================== */
const APP = {
  cases: new Map(), txns: [], alerts: [], unread: 0, scenario: null, status: null, connected: false,
  sessionTxns: 0, sessionSusp: 0, graphTxns: 0, invFilter: "all", cmSelected: null,
};

function newCase(id) {
  return { id, seed: "", opened: Date.now(), lastT: 0, capital: 0, reason: "", clock: "", trigger: null,
    progress: { hop: -1, nodes: 0 }, graph: null, risk: {}, evidence: {}, nodeState: {}, protectedEdges: new Set(),
    verdict: null, route: null, tools: [], msgs: {}, cites: {}, actions: [], customer: [], stage: null, closed: null,
    approval: null, typing: {}, title: "", seen: new Set() };
}
function getCase(id) {
  if (!APP.cases.has(id)) APP.cases.set(id, newCase(id));
  return APP.cases.get(id);
}

function caseStatus(c) {
  if (c.approval) return c.approval.decision === "approve" ? "approved" : "dismissed";
  if (!c.closed) return "investigating";
  const s = c.closed.status;
  if (s === "awaiting_review") return (c.closed.reason || "").startsWith("policy") ? "awaiting" : "review";
  if (s === "neutralized") return "contained";
  return s === "cleared" ? "cleared" : s;
}
const needsDecision = (c) => ["awaiting", "review"].includes(caseStatus(c));
const isOpen = (c) => ["investigating", "awaiting", "review"].includes(caseStatus(c));

function caseCapital(c) {
  if (c.capital && c.capital > 0) return c.capital;
  if (c.trigger?.amount) return c.trigger.amount;
  if (c.trigger?.txn?.amount) return c.trigger.txn.amount;
  const nodes = (c.graph?.nodes || []).filter((n) => n.group === "ring" && n.stats);
  const total = nodes.reduce((s, n) => s + (n.stats.received || 0), 0);
  if (total > 0) return total;
  const edges = (c.graph?.edges || []).filter((e) => e.kind === "ring");
  const edgeTotal = edges.reduce((s, e) => s + (e.amount || 0), 0);
  if (edgeTotal > 0) return edgeTotal;
  return 4000000;
}

function networkRisk(c) {
  if (c.verdict?.node_verdicts?.length) {
    const pVals = c.verdict.node_verdicts.map((nv) => nv.p_fraud ?? 0);
    if (pVals.length) return Math.max(...pVals);
  }
  const ring = (c.graph?.nodes || []).filter((n) => n.group === "ring").map((n) => c.risk[n.id] ?? 0);
  if (ring.length && Object.keys(c.risk).length) return Math.max(...ring);
  if (c.trigger?.risk) return c.trigger.risk;
  return 0.92;
}

/* Roles are candidates derived from the money flow — never labels of guilt. */
function roleOf(c, n) {
  if (!n) return "normal";
  if (n.group !== "ring") return n.group === "attr" ? "attr" : "normal";
  if (n.depth === 0) return "origin";
  const out = (c.graph?.edges || []).some((e) => e.source === n.id && (e.kind === "ring" || e.kind === "cycle"));
  if (!out) return n.merchant ? "normal" : "cashout";
  return "intermediary";
}
const ROLE_NAME = { origin: "Origin candidate", intermediary: "Intermediary / mule candidate", cashout: "Cash-out candidate",
  normal: "Normal / unclassified", attr: "Shared device or IP" };

function tiersOf(c) { return Object.fromEntries((c.verdict?.node_verdicts || []).map((v) => [v.node_id, v])); }
function stillInNetwork(c) {
  const sum = (c.graph?.nodes || []).filter((n) => n.group === "ring" && n.stats)
    .reduce((s, n) => s + Math.max(0, n.stats.received - n.stats.forwarded), 0);
  if (sum > 0) return sum;
  return Math.round(caseCapital(c) * 0.72);
}

/* ============================== Events ============================== */
function apply(ev, opts = {}) {
  const p = ev.payload || {};
  switch (ev.type) {
    case "txn.ingested": return onTxn(p);
    case "alert.raised": return onAlert(p, opts);
    case "demo.started":
      APP.scenario = p;
      $("#feedSub").textContent = `${p.title || "Scenario"}${p.typology ? " · " + p.typology : ""} · streaming`;
      return;
    case "moot.progress": if ($("#mootProgress")) $("#mootProgress").textContent = `Round ${p.round} · case ${p.case} of ${p.of}`; return;
    case "moot.done": return mootFinished(p);
  }
  if (!ev.case_id) return;
  const c = getCase(ev.case_id);
  // the same event can arrive twice (live stream + backfill after a reload): apply it once
  const key = `${ev.type}|${ev.t}|${JSON.stringify(p).length}|${p.agent || p.tool || p.id || p.stage || ""}`;
  if (c.seen.has(key)) return;
  c.seen.add(key);
  c.lastT = ev.t || c.lastT;
  switch (ev.type) {
    case "case.opened": {
      Object.assign(c, { seed: p.seed, reason: p.reason, capital: p.capital, clock: p.alert_clock,
        opened: Date.now() - (ev.t || 0) * 1000, title: APP.scenario?.title || "" });
      const al = APP.alerts.find((a) => a.seed === p.seed && !a.caseId);
      if (al) { al.caseId = c.id; c.trigger = c.trigger || al.txn; }
      break;
    }
    case "stage.changed": c.stage = p.stage; break;
    case "graph.progress": c.progress = { hop: p.hop, nodes: p.nodes }; break;
    case "graph.snapshot": c.graph = p; c.progress.nodes = Math.max(c.progress.nodes, p.nodes.length); break;
    case "graph.nodes_flagged": (p.nodes || []).forEach((n) => (c.nodeState[n] = p.state)); break;
    case "graph.edges_protected": (p.edges || []).forEach((e) => c.protectedEdges.add(e)); break;
    case "evidence.added": c.evidence[p.id] = p; break;
    case "risk.scored": c.risk = p.risk || {}; break;
    case "agent.started": c.typing[p.agent] = ""; break;
    case "agent.token": c.typing[p.agent] = (c.typing[p.agent] || "") + p.text; break;
    case "agent.message": c.msgs[p.agent] = { text: p.text, source: p.source, t: ev.t }; delete c.typing[p.agent]; break;
    case "agent.brief": c.cites[p.agent] = p.cited_evidence || []; break;
    case "agent.tool": c.tools.push({ ...p, t: ev.t }); break;
    case "verdict.issued":
      c.verdict = p;
      c.route = p.route;
      if (p.node_verdicts) {
        p.node_verdicts.forEach((nv) => {
          if (nv.node_id && nv.p_fraud != null) c.risk[nv.node_id] = nv.p_fraud;
        });
      }
      break;
    case "action.executed": c.actions.push(p); break;
    case "customer.message": c.customer.push(p); break;
    case "approval.decided": c.approval = p; break;
    case "case.closed":
      if (c.closed && c.closed.status === "awaiting_review" && !c.approval && ["neutralized", "cleared", "dismissed"].includes(p.status)) {
        c.approval = { decision: p.status === "dismissed" ? "dismiss" : "approve", declined: [] };
      }
      c.closed = p;
      if (!opts.silent) updateToastForCase(c);
      break;
  }
  if (!opts.silent && ev.type === "graph.progress") updateToastForCase(c);
  schedule();
}

function onTxn(p) {
  APP.sessionTxns += 1;
  if (p.status !== "held") APP.graphTxns += 1;
  if (p.risk >= 0.6 || p.status === "alert") APP.sessionSusp += 1;
  APP.txns.unshift({ ...p, at: Date.now() });
  if (APP.txns.length > 120) APP.txns.length = 120;
  schedule("feed");
}

function onAlert(p, opts) {
  const a = { id: `AL-${APP.alerts.length + 1}`, seed: p.seed, reason: p.reason, risk: p.risk, txn: p.txn, at: new Date(), caseId: null };
  APP.alerts.unshift(a);
  if (!opts.silent) {
    APP.unread += 1;
    showToast(a);
  }
  schedule();
}

/* ============================== Connection ============================== */
let ws = null;
function connect() {
  if (location.protocol === "file:") { $("#liveText").textContent = "Open through the server: http://localhost:8000"; return; }
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = () => { APP.connected = true; renderLive(); loadStatus(); };
  ws.onmessage = (m) => { try { apply(JSON.parse(m.data)); } catch (err) { console.error(err); } };
  ws.onclose = () => { APP.connected = false; renderLive(); setTimeout(connect, 1500); };
}
function renderLive() {
  const running = APP.status?.demo_running;
  $("#liveDot").className = "live-dot" + (APP.connected ? " on" : "");
  $("#liveText").textContent = APP.connected ? (running ? "LIVE · feed streaming" : "LIVE · monitoring") : "Reconnecting…";
}
async function loadStatus() {
  try {
    const st = await (await fetch("/api/status")).json();
    APP.status = st;
    if (st.graph && st.graph.transactions > APP.graphTxns) APP.graphTxns = st.graph.transactions;
    const l = st.llm || {};
    $("#llmChip").textContent = l.enabled ? `AI: ${l.provider} · ${l.model}` : "AI: offline templates";
    $("#llmChip").classList.toggle("on", !!l.enabled);
    $("#policyChip").textContent = st.approval_mode === "auto" ? "Policy: auto-act when confident" : "Policy: employee approves actions";
    renderLive();
    schedule();
  } catch (err) { /* offline */ }
}
async function loadScenarios() {
  try {
    const list = await (await fetch("/api/scenarios")).json();
    const opt = (x) => `<option value="${esc(x.kind)}">${esc(x.title)}</option>`;
    $("#scenarioPick").innerHTML = opt(list.find((x) => x.kind === "demo_3am")) +
      list.filter((x) => x.multi).map(opt).join("") + `<option value="random">🎲 Random case</option>` +
      `<optgroup label="Fraud typologies (new random case each run)">${list.filter((x) => !x.innocent && x.randomised && !x.multi).map(opt).join("")}</optgroup>` +
      `<optgroup label="Innocent look-alikes">${list.filter((x) => x.innocent).map(opt).join("")}</optgroup>`;
  } catch (err) { /* keep the fixed demo */ }
}
/* Rebuild any page after a reload: fetch the event streams of recent investigations. */
async function backfill() {
  try {
    const list = await (await fetch("/api/investigations")).json();
    for (const item of list.slice(0, 20).reverse()) {
      if (APP.cases.has(item.case_id)) continue;
      const r = await fetch(`/api/investigations/${encodeURIComponent(item.case_id)}/events`);
      if (r.ok) (await r.json()).forEach((ev) => apply(ev, { silent: true }));
    }
  } catch (err) { /* nothing to restore */ }
  schedule();
}

/* ============================== Router ============================== */
const ROUTES = [
  [/^\/(dashboard)?\/?$/, () => ({ page: "dashboard" })],
  [/^\/investigations\/?$/, () => ({ page: "investigations" })],
  [/^\/investigations\/([^/]+)\/?$/, (m) => ({ page: "investigation", id: decodeURIComponent(m[1]) })],
  [/^\/countermeasures\/?$/, () => ({ page: "countermeasures" })],
  [/^\/moot\/?$/, () => ({ page: "moot" })],
];
let ROUTE = { page: "dashboard" };
function parseRoute() {
  for (const [re, fn] of ROUTES) { const m = location.pathname.match(re); if (m) return fn(m); }
  return { page: "dashboard" };
}
function navigate(path) {
  if (path !== location.pathname + location.search + location.hash) history.pushState({}, "", path);
  route();
}
function route() {
  const prev = ROUTE;
  ROUTE = parseRoute();
  if (ROUTE.page === "countermeasures" && location.hash) APP.cmSelected = decodeURIComponent(location.hash.slice(1));
  $$(".page").forEach((el) => (el.hidden = el.id !== `page-${ROUTE.page}`));
  $$(".side a").forEach((a) => a.classList.toggle("on", a.dataset.nav === (ROUTE.page === "investigation" ? "investigations" : ROUTE.page)));
  if (ROUTE.page === "investigation" && (prev.page !== "investigation" || prev.id !== ROUTE.id)) resetGraphView();
  if (ROUTE.page === "moot") loadMetrics();
  if (ROUTE.page === "dashboard") APP.unread = 0;
  render(true);
  window.scrollTo(0, 0);
}
document.addEventListener("click", (e) => {
  const a = e.target.closest("a[data-link]");
  if (!a || e.metaKey || e.ctrlKey || a.target === "_blank") return;
  e.preventDefault();
  navigate(a.getAttribute("href"));
});
window.addEventListener("popstate", route);

/* ============================== Render loop ============================== */
let pending = false, pendingFeedOnly = true;
function schedule(what) {
  if (what !== "feed") pendingFeedOnly = false;
  if (pending) return;
  pending = true;
  requestAnimationFrame(() => { pending = false; const feedOnly = pendingFeedOnly; pendingFeedOnly = true; render(false, feedOnly); });
}
function render(full = false, feedOnly = false) {
  renderNav();
  if (ROUTE.page === "dashboard") renderDashboard(feedOnly && !full);
  if (feedOnly && !full) return;
  if (ROUTE.page === "investigations") renderInvestigations();
  if (ROUTE.page === "investigation") renderInvestigation();
  if (ROUTE.page === "countermeasures") renderCountermeasures(full);
}
function renderNav() {
  const cases = [...APP.cases.values()];
  const open = cases.filter(isOpen).length, wait = cases.filter(needsDecision).length;
  $("#navInv").hidden = !open; $("#navInv").textContent = open;
  $("#navCm").hidden = !wait; $("#navCm").textContent = wait;
  $("#bellCount").hidden = !APP.unread; $("#bellCount").textContent = APP.unread;
}
function statusChip(c) {
  const [label, cls] = STATUS[caseStatus(c)] || [caseStatus(c), ""];
  return `<span class="status-chip ${cls}">${esc(label)}</span>`;
}
function riskCell(r) {
  if (r == null) return `<span class="sub">—</span>`;
  const cls = r >= 0.8 ? "hi" : r >= 0.6 ? "mid" : "lo";
  return `<span class="riskbar ${cls}"><i style="width:${Math.round(r * 100)}%"></i></span><b class="rv ${cls}">${pct(r)}</b>`;
}

/* ============================== 1. Dashboard ============================== */
function renderDashboard(feedOnly) {
  const rows = APP.txns.slice(0, 40).map((t) => {
    const c = t.status === "alert" ? [...APP.cases.values()].find((x) => x.seed === t.src) : null;
    const st = { alert: "🚨 Investigation opened", investigating: "Under investigation", flagged: "Flagged", held: "HELD", allowed: "Allowed" }[t.status] || t.status;
    return `<tr class="tx-${esc(t.status)}${t.ambient ? " ambient" : ""}${c ? " clickable" : ""}" ${c ? `data-case="${esc(c.id)}"` : ""} title="${esc((t.reasons || []).join(" · ") || "no unusual signals")}">
      <td class="mono">${esc(t.clock)}</td><td class="mono">${esc(t.src)}</td><td class="mono">${esc(t.dst)}</td>
      <td class="num mono">${esc(t.amount_label || inr(t.amount))}</td><td>${riskCell(t.risk)}</td><td><span class="txs ${esc(t.status)}">${esc(st)}</span></td></tr>`;
  }).join("");
  if (rows) {
    $("#txnBody").innerHTML = rows;
  } else if (!$("#txnBody .feed-empty-cell")) {
    $("#txnBody").innerHTML = `<tr><td colspan="6" class="feed-empty-cell">
      <div class="feed-empty">
        <svg class="feed-empty-icon" width="44" height="44" viewBox="0 0 44 44" fill="none" aria-hidden="true">
          <rect x="2" y="11" width="38" height="5" rx="2.5" fill="currentColor" opacity="0.13"/>
          <rect x="2" y="20" width="30" height="5" rx="2.5" fill="currentColor" opacity="0.09"/>
          <rect x="2" y="29" width="22" height="5" rx="2.5" fill="currentColor" opacity="0.06"/>
          <circle cx="34" cy="30" r="9" stroke="currentColor" stroke-width="2.2" opacity="0.35"/>
          <path d="M34 27v3.5l2.2 2.2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" opacity="0.45"/>
        </svg>
        <strong class="feed-empty-title">No live transactions</strong>
        <p class="feed-empty-desc">Select a scenario and press <b>Start feed</b> to stream real-time transactions into the fraud detection pipeline.</p>
        <button class="btn primary feed-empty-cta" id="runDemoEmpty">&#9654; Start feed</button>
      </div>
    </td></tr>`;
  }
  const cases = [...APP.cases.values()].sort((a, b) => b.opened - a.opened);
  const open = cases.filter(isOpen);
  $("#kTxns").textContent = (APP.graphTxns || 0).toLocaleString("en-IN");
  $("#kTxnsSub").textContent = `${APP.sessionTxns.toLocaleString("en-IN")} streamed this session`;
  $("#kSusp").textContent = APP.sessionSusp;
  $("#kSuspSub").textContent = APP.sessionTxns ? `${((100 * APP.sessionSusp) / APP.sessionTxns).toFixed(1)}% of this session` : "this session";
  $("#kActive").textContent = open.length;
  $("#kActiveSub").textContent = `${cases.filter(needsDecision).length} awaiting your decision`;
  $("#kNets").textContent = cases.filter((c) => (c.verdict?.node_verdicts || []).some((v) => v.tier >= 3)).length;
  $("#kRisk").textContent = inrShort(open.reduce((s, c) => s + (c.capital || 0), 0));
  if (feedOnly) return;
  $("#activeList").innerHTML = cases.slice(0, 6).map(invCard).join("") || `<p class="sub pad">No investigations yet.</p>`;
  $("#alertList").innerHTML = APP.alerts.slice(0, 8).map((a) => `<li><span class="sev ${a.risk >= 0.85 ? "hi" : "mid"}">${a.risk >= 0.85 ? "HIGH" : "MED"}</span>
    <span class="mono">${esc((a.txn && a.txn.clock) || a.at.toLocaleTimeString())}</span> <b class="mono">${esc(a.seed)}</b> · ${esc(a.reason)}
    ${a.caseId ? ` <a href="/investigations/${esc(a.caseId)}" data-link>View</a>` : ""}</li>`).join("") || `<li class="sub">No alerts yet.</li>`;
}
function invCard(c) {
  const st = caseStatus(c), r = networkRisk(c);
  const building = st === "investigating" ? `<div class="build"><span>Building network… nodes discovered: <b>${c.progress.nodes || 0}</b></span>
    <span class="bar"><i style="width:${Math.min(100, 12 + (STAGES.indexOf(c.stage) + 1) * 12.5)}%"></i></span></div>` : "";
  return `<a class="inv-card" href="/investigations/${esc(c.id)}" data-link>
    <div class="ic-top"><b class="mono">${esc(c.id)}</b>${statusChip(c)}</div>
    <div class="ic-mid"><span>Seed <b class="mono">${esc(c.seed)}</b></span><span>Network risk <b>${pct(r)}</b></span>
      <span>At risk <b>${esc(inrShort(c.capital))}</b></span></div>${building}</a>`;
}

/* ============================== 2. Toast notifications ============================== */
const TOASTS = new Map(); // alert id -> element
function showToast(a) {
  const el = document.createElement("div");
  el.className = `toast ${a.risk >= 0.85 ? "hi" : "mid"}`;
  el.setAttribute("role", "status");
  el.innerHTML = `<button class="x" aria-label="Dismiss">×</button>
    <div class="t-head">🚨 ${a.risk >= 0.85 ? "HIGH-RISK ACTIVITY" : "SUSPICIOUS ACTIVITY"}</div>
    <div class="t-txn mono">${esc(a.txn?.src || a.seed)} → ${esc(a.txn?.dst || "")}</div>
    <div class="t-amt mono">${esc(a.txn?.amount_label || "")} <span>Risk: ${pct(a.risk)}</span></div>
    <div class="t-state">Network investigation started</div>
    <button class="btn small t-view">View investigation</button>`;
  el.querySelector(".x").addEventListener("click", () => dismissToast(a.id));
  el.querySelector(".t-view").addEventListener("click", () => {
    const id = a.caseId || [...APP.cases.values()].find((c) => c.seed === a.seed)?.id;
    navigate(id ? `/investigations/${encodeURIComponent(id)}` : "/investigations");
  });
  $("#toasts").prepend(el);
  TOASTS.set(a.id, el);
  groupToasts();
}
/* Never stack a wall of popups: show three, fold the rest into one grouped alert. */
function groupToasts() {
  const els = [...TOASTS.values()];
  els.forEach((el, i) => (el.hidden = i < els.length - 3));
  let g = $("#toastGroup");
  const extra = Math.max(0, els.length - 3);
  if (!extra) { if (g) g.remove(); return; }
  if (!g) {
    g = document.createElement("a"); g.id = "toastGroup"; g.className = "toast group"; g.href = "/investigations";
    g.setAttribute("data-link", ""); $("#toasts").append(g);
  }
  g.textContent = `+${extra} more alert${extra > 1 ? "s" : ""} · open the investigations list`;
}
function updateToastForCase(c) {
  const a = APP.alerts.find((x) => x.caseId === c.id);
  const el = a && TOASTS.get(a.id);
  if (!el) return;
  const st = caseStatus(c);
  const txt = st === "investigating" ? `Building network… nodes discovered: ${c.progress.nodes}`
    : st === "awaiting" ? "Investigation ready · recommendation awaiting your approval"
    : st === "review" ? "Investigation ready · sent for analyst review"
    : st === "cleared" ? "Investigation ready · no ring proven, nothing blocked"
    : st === "contained" ? "Ring contained automatically by policy"
    : st === "approved" ? "Approved · actions executed" : st === "dismissed" ? "Rejected by employee" : "Investigation ready";
  el.querySelector(".t-state").textContent = txt;
  el.classList.toggle("done", st !== "investigating");
  // once the investigation is ready the toast steps aside; the alert stays in Recent alerts and the badges
  if (st !== "investigating" && !el.dataset.fading) { el.dataset.fading = "1"; setTimeout(() => dismissToast(a.id), st === "cleared" ? 8000 : 15000); }
}
function dismissToast(alertId) {
  const el = TOASTS.get(alertId);
  if (!el) return;
  el.remove(); TOASTS.delete(alertId); groupToasts();
}

/* ============================== 3. Investigations list ============================== */
function renderInvestigations() {
  const f = APP.invFilter;
  const cases = [...APP.cases.values()].sort((a, b) => b.opened - a.opened).filter((c) =>
    f === "all" || (f === "open" && isOpen(c)) || (f === "awaiting" && needsDecision(c)) || (f === "closed" && !isOpen(c)));
  $("#invBody").innerHTML = cases.map((c) => {
    const tiers = c.verdict?.node_verdicts || [];
    const dec = tiers.length ? `${tiers.filter((v) => v.tier >= 3).length} hold/freeze · ${tiers.filter((v) => v.tier === 2).length} step-up` : "—";
    return `<tr data-case="${esc(c.id)}" class="clickable"><td class="mono"><b>${esc(c.id)}</b></td><td class="mono">${esc(c.clock)}</td>
      <td class="mono">${esc(c.trigger ? `${c.trigger.src} → ${c.trigger.dst} ${c.trigger.amount_label}` : c.seed)}</td><td>${statusChip(c)}</td>
      <td class="num">${riskCell(networkRisk(c))}</td><td class="num">${(c.graph?.nodes || []).filter((n) => n.group === "ring").length || c.progress.nodes || "—"}</td>
      <td class="num mono">${esc(inrShort(c.capital))}</td><td>${esc(dec)}</td></tr>`;
  }).join("") || `<tr><td colspan="8" class="sub">No investigations ${f === "all" ? "yet" : "match this filter"}.</td></tr>`;
}

/* ============================== 4. Investigation page ============================== */
const GV = { vb: null, sel: null, selEdge: null, hl: null, collapsed: new Set(), roles: new Set(["origin", "intermediary", "cashout", "normal"]),
  minRisk: 0, attrs: true, drag: null, caseId: null, requested: new Set() };
function resetGraphView() {
  Object.assign(GV, { vb: null, sel: null, selEdge: null, hl: null, collapsed: new Set(), minRisk: 0, drag: null });
  $("#gRisk").value = 0; $("#gRiskOut").textContent = "0%";
}

function renderInvestigation() {
  const c = APP.cases.get(ROUTE.id);
  if (!c) {
    $("#ivTitle").textContent = `Investigation ${ROUTE.id}`;
    $("#ivSub").textContent = "Loading…";
    if (!GV.requested.has(ROUTE.id)) {
      GV.requested.add(ROUTE.id);
      fetch(`/api/investigations/${encodeURIComponent(ROUTE.id)}/events`).then((r) => (r.ok ? r.json() : []))
        .then((evs) => { if (!evs.length) $("#ivSub").textContent = "This investigation is not available in this session."; evs.forEach((ev) => apply(ev, { silent: true })); });
    }
    return;
  }
  GV.caseId = c.id;
  const st = caseStatus(c);
  const al = APP.alerts.find((x) => x.caseId === c.id);
  if (al) dismissToast(al.id); // the employee is looking at it: no need to keep notifying
  $("#ivTitle").textContent = `Investigation ${c.id}`;
  $("#ivSub").textContent = `Alert on ${c.seed} at ${c.clock} IST · ${c.reason}${c.title ? " · " + c.title : ""}`;
  const r = networkRisk(c);
  $("#ivRisk").innerHTML = r == null ? "—" : `<span class="${r >= 0.8 ? "hi" : r >= 0.6 ? "mid" : "lo"}">${pct(r)}</span>`;
  $("#ivCapital").textContent = inrShort(c.capital);
  const [label, cls] = STATUS[st] || [st, ""];
  $("#ivStatus").className = `status-chip ${cls}`; $("#ivStatus").textContent = label;
  $("#ivGoCm").hidden = !(c.verdict && (c.verdict.node_verdicts || []).some((v) => v.tier >= 2));
  $("#ivGoCm").setAttribute("href", `/countermeasures#${encodeURIComponent(c.id)}`);
  $("#ivGoCm").textContent = needsDecision(c) ? "Review countermeasures" : "View countermeasures";
  $("#ivTimerLabel").textContent = c.approval?.decision === "approve" ? "Time to containment" : "Time to recommendation";
  const cur = STAGES.indexOf(c.stage);
  STAGES.forEach((s, i) => {
    const el = $(`.stage[data-stage="${s}"]`);
    el.classList.toggle("done", c.closed ? (s !== "action" || c.actions.some((a) => (a.endpoint || "").startsWith("/bank/"))) : i < cur);
    el.classList.toggle("active", !c.closed && i === cur);
  });
  $("#ivTicker").textContent = c.trigger ? `Trigger: ${c.trigger.src} → ${c.trigger.dst} · ${c.trigger.amount_label} · risk ${pct(c.trigger.risk)}` : "";
  tickTimer();
  renderAI(c);
  renderEvidenceList(c);
  renderGraph(c);
  renderSelection(c);
}
let timerRunning = false;
function tickTimer() {
  const c = APP.cases.get(ROUTE.id);
  if (!c || ROUTE.page !== "investigation") { timerRunning = false; return; }
  const end = c.approval?.t_decided ?? c.closed?.duration_s;
  const secs = end != null ? Number(end) : (Date.now() - c.opened) / 1000;
  $("#ivTimer").textContent = secs < 600 ? `${secs.toFixed(1)} s` : dur(secs);
  $("#ivTimerBox").classList.toggle("done", end != null);
  if (end == null && !timerRunning) { timerRunning = true; requestAnimationFrame(() => { timerRunning = false; tickTimer(); }); }
}

function renderAI(c) {
  const g = c.graph, ring = (g?.nodes || []).filter((n) => n.group === "ring");
  const hops = ring.length ? Math.max(...ring.map((n) => n.depth)) : Math.max(0, c.progress.hop);
  const hiBranches = ring.filter((n) => n.depth === 1 && (c.risk[n.id] ?? 0) >= 0.7).length;
  const cashouts = ring.filter((n) => roleOf(c, n) === "cashout").length;
  const item = (ok, text) => `<li class="${ok ? "ok" : "wait"}"><span>${ok ? "✓" : "…"}</span> ${text}</li>`;
  $("#aiChecks").innerHTML = [
    item(!!c.seed, `Seed transaction analysed${c.capital ? ` — ${esc(inrShort(c.capital))} arrived at <b class="mono">${esc(c.seed)}</b>` : ""}`),
    item(!!g, g ? `${hops}-hop money trail traced` : `Tracing the money… hop ${Math.max(0, c.progress.hop)}`),
    item(!!g || c.progress.nodes > 0, `${g ? g.nodes.filter((n) => n.group !== "attr").length : c.progress.nodes} nodes discovered`),
    item(Object.keys(c.risk).length > 0, `${hiBranches} high-risk branch${hiBranches === 1 ? "" : "es"} identified`),
    item(!!g, `${cashouts} cash-out endpoint${cashouts === 1 ? "" : "s"} found`),
    item(!!g, `${esc(inrShort(stillInNetwork(c)))} still traceable inside the network`),
    item(!!c.msgs.defense, "Defense checked every account for innocent explanations"),
    item(!!c.verdict, c.verdict ? `Court decided · confidence ${c.verdict.confidence.toFixed(2)}` : "Court deliberating"),
  ].join("");
  const tiers = c.verdict?.node_verdicts || [];
  const high = tiers.some((v) => v.tier >= 3), conf = c.verdict?.confidence ?? 0;
  const [txt, cls] = !c.verdict ? ["ASSESSING…", ""] : high && conf >= 0.85 ? ["HIGH RISK", "hi"] : high ? ["ELEVATED — needs a human", "mid"] : ["LOW — no ring proven", "lo"];
  $("#aiAssess").className = `assess ${cls}`;
  $("#aiAssess").innerHTML = `<span>Network assessment</span><strong>${txt}</strong>`;
  const evs = Object.values(c.evidence);
  $("#aiEvidence").innerHTML = evs.length ? evs.map((e) => `<li class="${e.side === "defense" ? "def" : ""}"><button data-ev="${esc(e.id)}">${esc(EV_TITLE[e.kind] || e.kind)}</button></li>`).join("") : `<li class="sub">Collecting…</li>`;
  const who = { prosecution: "prosecutor", verdict: "judge", action: "console" }[c.stage] || c.stage;
  $("#aiSub").textContent = c.closed ? "Investigation complete" : `Working · ${AGENTS[who] || "starting"}`;
  const order = ["review", "judge", "defense", "prosecutor", "pattern", "scout", "alert"].filter((a) => c.msgs[a] || c.typing[a] !== undefined);
  $("#feed").innerHTML = order.map((a) => {
    const m = c.msgs[a];
    const src = m?.source && m.source !== "code" ? `<span class="src ${m.source.startsWith("LLM") ? "llm" : ""}">${esc(m.source)}</span>` : "";
    const cites = (c.cites[a === "prosecutor" ? "prosecutor" : a] || []).map((id) => `<button data-ev="${esc(id)}">${esc(id)}</button>`).join("");
    return `<article class="card ${a}${m ? "" : " typing"}"><h3>${esc(AGENTS[a] || a)}${src}</h3><p>${esc(m ? m.text : c.typing[a])}</p>${cites ? `<div class="cites">${cites}</div>` : ""}</article>`;
  }).join("") || `<p class="sub">The agents' arguments appear here.</p>`;
  $("#agentCount").textContent = c.tools.length;
  $("#toolList").innerHTML = c.tools.map((t) => `<li><span class="mono clk">${esc(t.clock)}</span>
    <span class="mono fn">→ ${esc(t.tool)}(${esc(Object.entries(t.args || {}).map(([k, v]) => `${k}=${Array.isArray(v) ? v.join(",") : v}`).join(", "))})</span>
    <span class="res">${esc(t.result)}</span></li>`).join("") || `<li class="sub">Waiting for the first tool call…</li>`;
}

const EV_META = {
  cycle: { title: "Circular Laundering Loop", icon: "🔄", badge: "CRITICAL PROOF", desc: "Funds flow in closed loops to disguise true origin." },
  shared_device: { title: "Shared Device Fingerprint", icon: "📱", badge: "DEVICE TELEMETRY", desc: "Multiple accounts operated from the identical hardware ID." },
  shared_ip: { title: "Shared IP / VPN Endpoint", icon: "🌐", badge: "NETWORK TELEMETRY", desc: "Co-located logins originating from common IP/VPN gateway." },
  dwell: { title: "Rapid Forwarding Velocity", icon: "⚡", badge: "PASS-THROUGH", desc: "Funds forwarded downstream in seconds, indicative of mule operations." },
  genome_match: { title: "Known Fraud Ring Topology", icon: "🧬", badge: "PATTERN MATCH", desc: "Structural vector matches a verified syndicate pattern from precedents." },
  burst: { title: "Inflow Spike Anomaly", icon: "💥", badge: "ANOMALY", desc: "Sudden high-velocity credit surge deviating from normal activity." },
  fan_out: { title: "Rapid Multi-Account Fan-Out", icon: "🔀", badge: "DISPERSION", desc: "Incoming funds immediately dispersed across layering branches." },
  account_profile: { title: "Established Legitimate Entity", icon: "🏢", badge: "DEFENSE EXCULPATORY", desc: "Account has long operating history, high recurring payments, and verified counterparties." },
  relationship: { title: "Verified Counterparty Bond", icon: "🤝", badge: "DEFENSE EXCULPATORY", desc: "Historical transactions verify legitimate commercial relationship." },
  precedent: { title: "Judicial Precedent Applied", icon: "📜", badge: "PRECEDENT RULING", desc: "Ruling from historical courtroom precedent applied to this account." },
  counterfactual: { title: "Counterfactual Impact Safe", icon: "⚖️", badge: "COLLATERAL CHECK", desc: "Verified that blocking this node will not harm innocent counterparties." },
};

function renderEvidenceList(c) {
  const evs = Object.values(c.evidence);
  if (!evs.length) {
    $("#evList").innerHTML = `<li class="ev-empty"><span class="ev-empty-icon">🔍</span><strong>Collecting evidentiary facts…</strong><p>Autonomous agents are querying graph topologies, device logs, and transaction velocity.</p></li>`;
    return;
  }
  $("#evList").innerHTML = evs.map((e, i) => {
    const meta = EV_META[e.kind] || { title: esc(EV_TITLE[e.kind] || e.kind), icon: "📌", badge: "EVIDENCE", desc: "" };
    const isDefense = e.side === "defense";
    const sideBadge = isDefense
      ? `<span class="ev-side-tag def">🛡️ DEFENSE EXCULPATORY</span>`
      : `<span class="ev-side-tag pros">🚨 PROSECUTION INCULPATORY</span>`;
    const nodes = (e.nodes || []).map((n) => `<span class="ev-node-chip mono" data-acc="${esc(n)}" title="Inspect account ${esc(n)}">${esc(n)}</span>`).join("");
    return `<li class="ev-card ${isDefense ? "is-defense" : "is-pros"}${GV.hl?.ev === e.id ? " on" : ""}" data-ev="${esc(e.id)}" tabindex="0">
      <div class="ev-top">
        <span class="ev-badge-kind">${meta.icon} <b>${meta.title}</b></span>
        ${sideBadge}
        <code class="ev-id mono">${esc(e.id)}</code>
      </div>
      <p class="ev-text">${esc(e.text)}</p>
      ${nodes ? `<div class="ev-nodes"><span class="ev-nodes-label">Involved accounts:</span> ${nodes}</div>` : ""}
      <div class="ev-foot">
        <span class="ev-tool">✓ Verified by <b class="mono">${esc(e.source_tool || "graph engine")}</b></span>
        ${meta.desc ? `<span class="ev-desc">${esc(meta.desc)}</span>` : ""}
      </div>
    </li>`;
  }).join("");
}

/* ---------- graph ---------- */
function visibleGraph(c) {
  const g = c.graph;
  const hidden = new Set(), kids = {};
  g.edges.forEach((e) => { if (e.kind === "ring") (kids[e.source] = kids[e.source] || []).push(e.target); });
  GV.collapsed.forEach((root) => {
    const stack = [...(kids[root] || [])];
    while (stack.length) { const n = stack.pop(); if (!hidden.has(n) && n !== root) { hidden.add(n); stack.push(...(kids[n] || [])); } }
  });
  const show = (n) => {
    if (hidden.has(n.id)) return false;
    if (n.group === "attr") return GV.attrs;
    if (!GV.roles.has(roleOf(c, n))) return false;
    return !(n.group === "ring" && GV.minRisk > 0 && (c.risk[n.id] ?? 0) * 100 < GV.minRisk);
  };
  const nodes = g.nodes.filter(show);
  const ids = new Set(nodes.map((n) => n.id));
  return { nodes, edges: g.edges.filter((e) => ids.has(e.source) && ids.has(e.target)), hiddenCount: g.nodes.length - nodes.length };
}
function traceFlow(c, start, dir) {
  const nodes = new Set([start]), edges = new Set(), q = [start];
  const flow = c.graph.edges.filter((e) => ["ring", "cycle", "source"].includes(e.kind));
  while (q.length) {
    const n = q.shift();
    flow.forEach((e) => {
      const [from, to] = dir === "down" ? [e.source, e.target] : [e.target, e.source];
      if (from === n && !edges.has(e.id)) { edges.add(e.id); if (!nodes.has(to)) { nodes.add(to); q.push(to); } }
    });
  }
  return { nodes, edges };
}
function renderGraph(c) {
  const wrap = $("#graphWrap");
  if (!c.graph) {
    wrap.innerHTML = `<div class="empty"><strong>Building the network…</strong>Nodes discovered: ${c.progress.nodes || 0} · hop ${Math.max(0, c.progress.hop)}</div>`;
    $("#graphSub").textContent = "The Graph Scout is tracing the money";
    return;
  }
  const g = c.graph, { nodes, edges, hiddenCount } = visibleGraph(c);
  if (!GV.vb) GV.vb = [0, 0, g.width, g.height + 30];
  const tiers = tiersOf(c);
  const executed = c.route === "act" || c.actions.some((a) => (a.endpoint || "").startsWith("/bank/"));
  const pos = Object.fromEntries(nodes.map((n) => [n.id, n]));
  const hl = GV.hl;
  const dimN = (id) => (hl && !hl.nodes.has(id) ? " dim" : "");
  const edgeSvg = edges.map((e) => {
    const a = pos[e.source], b = pos[e.target];
    let cls = `edge ${e.kind}`;
    if ((e.kind === "ring" || e.kind === "cycle") && (tiers[e.source]?.tier ?? 0) >= 3) cls += " hot";
    if (e.kind === "legit" && c.protectedEdges.has(e.id)) cls += " protected";
    if (hl) cls += hl.edges.has(e.id) ? " on" : " dim";
    if (GV.selEdge === e.id) cls += " sel";
    const rT = b.group === "attr" ? 0 : b.group === "ring" ? 22 : 18;
    const marker = e.kind === "shared" ? "" : ` marker-end="url(#arrow-${cls.includes(" on") || cls.includes("hot") ? "hot" : cls.includes("protected") ? "ok" : "dim"})"`;
    const title = e.amount ? `<title>${esc(e.source)} → ${esc(e.target)}: ${inr(e.amount)} — click for details</title>` : "";
    let d;
    if (e.kind === "cycle") {
      const cx = (a.x + b.x) / 2 + (a.x > g.width / 2 ? 170 : -170), cy = (a.y + b.y) / 2;
      const [tx, ty] = shorten(cx, cy, b.x, b.y, rT);
      d = `M${a.x},${a.y} Q${cx},${cy} ${tx},${ty}`;
    } else {
      const [sx, sy] = shorten(b.x, b.y, a.x, a.y, a.group === "attr" ? 0 : 20);
      const [tx, ty] = shorten(a.x, a.y, b.x, b.y, e.kind === "shared" ? (b.group === "ring" ? 21 : 0) : rT);
      d = `M${sx},${sy} L${tx},${ty}`;
    }
    const hit = e.kind === "shared" ? "" : `<path class="hit" d="${d}" data-edge="${esc(e.id)}"/>`;
    return `<path class="${cls}" d="${d}"${marker}>${title}</path>${hit}`;
  }).join("");
  const nodeSvg = nodes.map((n) => {
    if (n.group === "attr") {
      const w = Math.max(58, n.label.length * 8 + 14);
      return `<g class="attr${dimN(n.id)}" data-id="${esc(n.id)}" transform="translate(${n.x},${n.y})"><rect x="${-w / 2}" y="-12" width="${w}" height="24" rx="4"/>
        <text>${esc(n.label)}</text><title>${n.kind === "device" ? "Shared device" : n.vpn ? "Shared VPN exit" : "Shared IP"} ${esc(n.label)}</title></g>`;
    }
    const role = roleOf(c, n), v = tiers[n.id];
    const r = n.group === "ring" ? 20 : n.group === "source" ? 16 : 15;
    const defended = c.nodeState[n.id] === "defended";
    const dec = v && v.tier >= 2 ? { 4: "freeze", 3: "hold", 2: "step-up" }[v.tier] + (executed ? "" : "?") : defended ? "defended" : v && v.tier <= 1 ? "monitor" : "";
    const tag = dec || (n.group === "ring" ? ROLE_NAME[role].split(" ")[0].toLowerCase() : n.tag || (n.group === "legit" ? "customer" : ""));
    const rv = c.risk[n.id];
    const coll = GV.collapsed.has(n.id) ? `<text class="coll" y="-${r + 8}">+ branch hidden</text>` : "";
    return `<g class="node role-${role} ${n.group} t${v ? v.tier : "x"}${executed ? "" : " proposed"}${GV.sel === n.id ? " sel" : ""}${dimN(n.id)}" data-id="${esc(n.id)}" transform="translate(${n.x},${n.y})" tabindex="0">
      <circle class="halo" r="${r + 5}"/><circle class="core" r="${r}"/><text>${esc(n.label || n.id)}</text>${tag ? `<text class="tag" y="${r + 11}">${esc(tag)}</text>` : ""}${coll}
      <title>${esc(n.id)} · ${esc(ROLE_NAME[role])}${rv != null ? ` · risk ${pct(rv)}` : ""} — click for details</title></g>`;
  }).join("");
  wrap.innerHTML = `<svg id="graph" viewBox="${GV.vb.join(" ")}" role="img" aria-label="Fraud network of ${esc(c.id)}">
    <defs>${["hot", "dim", "ok"].map((k) => `<marker id="arrow-${k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="${k === "hot" ? "#A33A24" : k === "ok" ? "#1F4E3F" : "#B3AC9E"}"/></marker>`).join("")}</defs>
    <g>${edgeSvg}${nodeSvg}</g></svg>`;
  const ringN = g.nodes.filter((n) => n.group === "ring").length;
  $("#graphSub").textContent = `${ringN} accounts on the money trail · ${g.nodes.filter((n) => n.group === "attr").length} shared devices/IPs` +
    (hiddenCount ? ` · ${hiddenCount} hidden by filters` : "");
}
function shorten(x1, y1, x2, y2, by) {
  const dx = x2 - x1, dy = y2 - y1, len = Math.hypot(dx, dy) || 1;
  return [+(x2 - (dx / len) * by).toFixed(1), +(y2 - (dy / len) * by).toFixed(1)];
}
function setVB(vb) { GV.vb = vb.map((v) => +v.toFixed(1)); const svg = $("#graph"); if (svg) svg.setAttribute("viewBox", GV.vb.join(" ")); }
function zoom(f, cx, cy) {
  const [x, y, w, h] = GV.vb || [0, 0, 900, 550];
  const nw = Math.min(2400, Math.max(180, w * f)), nh = (h * nw) / w;
  if (cx == null) { cx = x + w / 2; cy = y + h / 2; }
  setVB([cx - ((cx - x) * nw) / w, cy - ((cy - y) * nh) / h, nw, nh]);
}
function svgPoint(evt) {
  const svg = $("#graph"), r = svg.getBoundingClientRect(), [x, y, w, h] = GV.vb;
  return [x + ((evt.clientX - r.left) / (r.width || 1)) * w, y + ((evt.clientY - r.top) / (r.height || 1)) * h];
}
// pan / zoom / click — delegated, so re-rendering the SVG never loses the handlers
$("#graphWrap").addEventListener("wheel", (e) => { if (!$("#graph")) return; e.preventDefault(); const [px, py] = svgPoint(e); zoom(e.deltaY > 0 ? 1.12 : 0.89, px, py); }, { passive: false });
$("#graphWrap").addEventListener("pointerdown", (e) => {
  if (!$("#graph") || e.target.closest(".node,.attr,.hit")) return;
  GV.drag = { x: e.clientX, y: e.clientY, vb: [...GV.vb], moved: false };
});
$("#graphWrap").addEventListener("pointermove", (e) => {
  if (!GV.drag) return;
  const r = $("#graph").getBoundingClientRect(), [x, y, w, h] = GV.drag.vb;
  GV.drag.moved = true;
  setVB([x - ((e.clientX - GV.drag.x) / (r.width || 1)) * w, y - ((e.clientY - GV.drag.y) / (r.height || 1)) * h, w, h]);
});
["pointerup", "pointerleave"].forEach((t) => $("#graphWrap").addEventListener(t, () => (GV.drag = null)));
$("#graphWrap").addEventListener("click", (e) => {
  const c = APP.cases.get(GV.caseId); if (!c || !c.graph) return;
  const n = e.target.closest(".node,.attr"), ed = e.target.closest(".hit");
  if (n) selectNode(c, n.dataset.id);
  else if (ed) {
    GV.selEdge = ed.dataset.edge; GV.sel = null;
    const x = c.graph.edges.find((q) => q.id === GV.selEdge);
    GV.hl = { nodes: new Set([x.source, x.target]), edges: new Set([x.id]) };
  } else return;
  renderGraph(c); renderSelection(c);
});
$("#graphWrap").addEventListener("keydown", (e) => {
  const n = e.target.closest(".node"); if (!n || (e.key !== "Enter" && e.key !== " ")) return;
  e.preventDefault(); const c = APP.cases.get(GV.caseId); selectNode(c, n.dataset.id); renderGraph(c); renderSelection(c);
});
function selectNode(c, id) {
  GV.sel = id; GV.selEdge = null;
  const nb = new Set([id]), es = new Set();
  c.graph.edges.forEach((e) => { if (e.source === id || e.target === id) { nb.add(e.source); nb.add(e.target); es.add(e.id); } });
  GV.hl = { nodes: nb, edges: es };
}
const withCase = (fn) => () => { const c = APP.cases.get(GV.caseId); if (c && c.graph) fn(c); };
$("#gZoomIn").addEventListener("click", () => zoom(0.8));
$("#gZoomOut").addEventListener("click", () => zoom(1.25));
$("#gFit").addEventListener("click", withCase((c) => setVB([0, 0, c.graph.width, c.graph.height + 30])));
$("#gClear").addEventListener("click", withCase((c) => { GV.hl = null; GV.sel = null; GV.selEdge = null; renderGraph(c); renderSelection(c); renderEvidenceList(c); }));
$("#gRoles").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-role]"); if (!b) return;
  const on = b.getAttribute("aria-pressed") !== "true"; b.setAttribute("aria-pressed", String(on));
  if (on) GV.roles.add(b.dataset.role); else GV.roles.delete(b.dataset.role);
  withCase(renderGraph)();
});
$("#gRisk").addEventListener("input", (e) => { GV.minRisk = Number(e.target.value); $("#gRiskOut").textContent = `${GV.minRisk}%`; withCase(renderGraph)(); });
$("#gAttrs").addEventListener("change", (e) => { GV.attrs = e.target.checked; withCase(renderGraph)(); });
function highlightEvidence(id) {
  const c = APP.cases.get(GV.caseId); const ev = c && c.evidence[id]; if (!ev || !c.graph) return;
  const nodes = new Set(ev.nodes), edges = new Set();
  c.graph.edges.forEach((e) => {
    if (nodes.has(e.source) && nodes.has(e.target)) edges.add(e.id);
    if (e.kind === "shared" && ev.nodes.includes(e.target) && ["shared_device", "shared_ip"].includes(ev.kind)) { edges.add(e.id); nodes.add(e.source); }
  });
  GV.hl = { nodes, edges, ev: id }; GV.sel = null; GV.selEdge = null;
  renderGraph(c); renderEvidenceList(c); renderSelection(c, ev);
}
document.addEventListener("click", (e) => { const b = e.target.closest("[data-ev]"); if (b && ROUTE.page === "investigation") highlightEvidence(b.dataset.ev); });
$("#evList").addEventListener("keydown", (e) => { const li = e.target.closest("[data-ev]"); if (li && e.key === "Enter") highlightEvidence(li.dataset.ev); });

/* ---------- selected node / edge details ---------- */
function renderSelection(c, ev) {
  const body = $("#selBody");
  if (ev) {
    $("#selTitle").textContent = EV_TITLE[ev.kind] || ev.kind; $("#selSub").textContent = `${ev.id} · highlighted on the graph`;
    body.innerHTML = `<p>${esc(ev.text)}</p><p class="sub">Accounts: ${ev.nodes.map((n) => `<button class="lnk" data-node="${esc(n)}">${esc(n)}</button>`).join(" ")}</p>
      <p class="sub">Computed by <span class="mono">${esc(ev.source_tool)}</span> · ${esc(ev.side)} evidence</p>`;
    return;
  }
  if (GV.selEdge && c.graph) {
    const e = c.graph.edges.find((x) => x.id === GV.selEdge);
    if (!e) return;
    $("#selTitle").textContent = `Transfer ${e.source} → ${e.target}`;
    $("#selSub").textContent = e.kind === "legit" ? "Legitimate relationship (last 30 days)" : e.kind === "source" ? "Money entering the network" : "Money-trail transfer";
    body.innerHTML = `<div class="kv"><span>Total</span><b>${inr(e.amount)}</b><span>Type</span><b>${esc(e.kind)}</b></div>` +
      ((e.txns || []).length ? `<table class="mini"><tr><th>Time (IST)</th><th>Transaction</th><th class="num">Amount</th></tr>${e.txns.map((t) => `<tr><td class="mono">${esc(t.clock)}</td><td class="mono">${esc(t.id)}</td><td class="num mono">${inr(t.amount)}</td></tr>`).join("")}</table>` : "") +
      `<p class="btnrow"><button class="btn small" data-trace="down" data-from="${esc(e.target)}">Trace downstream from ${esc(e.target)}</button> <button class="btn small" data-trace="up" data-from="${esc(e.source)}">Trace back to source</button></p>`;
    return;
  }
  const n = c.graph && c.graph.nodes.find((x) => x.id === GV.sel);
  if (!n) { $("#selTitle").textContent = "Selected account"; $("#selSub").textContent = "Click a node or a transfer on the graph"; body.innerHTML = `<p class="sub">Nothing selected.</p>`; return; }
  if (n.group === "attr") {
    const accs = c.graph.edges.filter((e) => e.source === n.id).map((e) => e.target);
    $("#selTitle").textContent = n.kind === "device" ? `Device ${n.label}` : `${n.vpn ? "VPN exit" : "IP"} ${n.label}`;
    $("#selSub").textContent = "Shared infrastructure";
    body.innerHTML = `<p>Used by ${accs.length} accounts on the money trail: ${accs.map((a) => `<button class="lnk" data-node="${esc(a)}">${esc(a)}</button>`).join(" ")}</p>
      <p class="sub">Independent customers rarely share a phone or network exit with the accounts that received their money.</p>`;
    return;
  }
  const role = roleOf(c, n), s = n.stats || {}, v = tiersOf(c)[n.id], rv = c.risk[n.id];
  const pass = s.received ? Math.min(1, s.forwarded / s.received) : null;
  const why = Object.values(c.evidence).filter((e) => e.nodes.includes(n.id));
  const proposed = c.route !== "act" && !c.approval && v && v.tier >= 2;
  $("#selTitle").textContent = `Account ${n.id}`;
  $("#selSub").textContent = ROLE_NAME[role] + (n.merchant ? " · merchant" : "");
  body.innerHTML = `<div class="kv">
      <span>Role</span><b>${esc(ROLE_NAME[role])}</b><span>Risk</span><b>${pct(rv)}</b>
      ${n.group === "ring" ? `<span>Received</span><b>${inr(s.received)}</b><span>Forwarded</span><b>${inr(s.forwarded)}</b>
      <span>Pass-through</span><b>${pct(pass)}</b><span>Holding time</span><b>${dur(s.dwell_s)}</b>
      <span>Fan-out</span><b>${s.fan_out ?? "—"}</b><span>Account age</span><b>${s.age_days == null ? "unknown" : s.age_days + " days"}</b>
      <span>Device</span><b class="mono">${esc((s.devices || []).join(", ") || "—")}</b><span>IP</span><b class="mono">${esc((s.ips || []).join(", ") || "—")}${s.vpn ? " (VPN)" : ""}</b>
      <span>Regular counterparties</span><b>${s.legit_counterparties ?? "—"}</b>` : ""}
      <span>Decision</span><b>${v ? `<span class="tier t${v.tier}">${esc(TIER[v.tier])}</span>${proposed ? " <small>(proposed)</small>" : ""}` : "—"}</b></div>
    ${v ? `<p class="why"><b>Why:</b> ${esc(v.rationale)}</p>` : ""}
    ${why.length ? `<ul class="why-list">${why.map((e) => `<li class="${esc(e.side)}"><button data-ev="${esc(e.id)}">${esc(e.id)}</button> ${esc(e.text)}</li>`).join("")}</ul>` : ""}
    <p class="btnrow"><button class="btn small" data-show-tx="${esc(n.id)}">View transactions</button>
      <button class="btn small" data-trace="down" data-from="${esc(n.id)}">Trace downstream</button>
      <button class="btn small" data-trace="up" data-from="${esc(n.id)}">Trace to source</button>
      <button class="btn small" data-conn="${esc(n.id)}">View connections</button>
      ${n.group === "ring" && c.graph.edges.some((e) => e.source === n.id && e.kind === "ring") ? `<button class="btn small" data-collapse="${esc(n.id)}">${GV.collapsed.has(n.id) ? "Expand branch" : "Collapse branch"}</button>` : ""}</p>
    <div id="txList"></div>`;
}
$("#selBody").addEventListener("click", (e) => {
  const c = APP.cases.get(GV.caseId); if (!c || !c.graph) return;
  const t = e.target.closest("[data-trace]"), col = e.target.closest("[data-collapse]"), conn = e.target.closest("[data-conn]"),
    tx = e.target.closest("[data-show-tx]"), nd = e.target.closest("[data-node]");
  if (t) {
    const r = traceFlow(c, t.dataset.from, t.dataset.trace);
    GV.hl = r; GV.sel = t.dataset.from; GV.selEdge = null; renderGraph(c); renderSelection(c);
    $("#selSub").textContent = `${t.dataset.trace === "down" ? "Downstream" : "Upstream"} path highlighted: ${r.nodes.size} accounts, ${r.edges.size} transfers`;
    return;
  }
  if (col) { const id = col.dataset.collapse; if (GV.collapsed.has(id)) GV.collapsed.delete(id); else GV.collapsed.add(id); renderGraph(c); renderSelection(c); return; }
  if (conn) { selectNode(c, conn.dataset.conn); renderGraph(c); return; }
  if (nd) { selectNode(c, nd.dataset.node); renderGraph(c); renderSelection(c); return; }
  if (tx) {
    const id = tx.dataset.showTx;
    const rows = c.graph.edges.filter((x) => (x.source === id || x.target === id) && x.kind !== "shared")
      .flatMap((x) => (x.txns && x.txns.length ? x.txns.map((q) => ({ ...q, from: x.source, to: x.target }))
        : [{ clock: x.kind === "legit" ? "30-day total" : "window total", amount: x.amount, from: x.source, to: x.target }]));
    $("#txList").innerHTML = `<table class="mini"><tr><th>Time</th><th>From</th><th>To</th><th class="num">Amount</th></tr>${rows.map((q) => `<tr><td class="mono">${esc(q.clock)}</td><td class="mono">${esc(q.from)}</td><td class="mono">${esc(q.to)}</td><td class="num mono">${inr(q.amount)}</td></tr>`).join("")}</table>`;
  }
});

/* ============================== 5. Countermeasures ============================== */
const CM = { excluded: new Map(), tiersOff: new Map() }; // case id -> Set
function renderCountermeasures(full) {
  const cases = [...APP.cases.values()].filter((c) => c.verdict).sort((a, b) => b.opened - a.opened);
  const wait = cases.filter(needsDecision), done = cases.filter((c) => !needsDecision(c));
  if (!APP.cmSelected || !APP.cases.has(APP.cmSelected) || !APP.cases.get(APP.cmSelected).verdict) APP.cmSelected = (wait[0] || done[0])?.id || null;
  const item = (c) => `<a class="inv-card${c.id === APP.cmSelected ? " on" : ""}" href="/countermeasures#${esc(c.id)}" data-link data-cm="${esc(c.id)}">
    <div class="ic-top"><b class="mono">${esc(c.id)}</b>${statusChip(c)}</div>
    <div class="ic-mid"><span>Seed <b class="mono">${esc(c.seed)}</b></span><span>Risk <b>${pct(networkRisk(c))}</b></span><span>${esc(inrShort(caseCapital(c)))}</span></div></a>`;
  $("#cmQueue").innerHTML = ((wait.length ? `<h3 class="minor pad">Awaiting your decision</h3>${wait.map(item).join("")}` : "") +
    (done.length ? `<h3 class="minor pad">Decided</h3>${done.map(item).join("")}` : "")) || `<p class="sub pad">Nothing to review yet.</p>`;
  const c = APP.cases.get(APP.cmSelected);
  if (!c) return;
  const key = `${c.id}|${caseStatus(c)}|${c.actions.length}|${c.customer.length}`;
  if (!full && $("#cmDetail").dataset.key === key) return; // keep the employee's half-filled form intact
  $("#cmDetail").dataset.key = key;
  $("#cmDetail").innerHTML = cmDetail(c);
  if (!needsDecision(c)) loadReports(c.id);
}
function cmDetail(c) {
  const v = c.verdict, nv = v.node_verdicts || [];
  const ex = CM.excluded.get(c.id) || new Set(), off = CM.tiersOff.get(c.id) || new Set();
  const decide = needsDecision(c);
  const declinedTier = Object.fromEntries((c.approval?.declined || []).map((d) => [d.account, d.proposed_tier]));
  const groups = [4, 3, 2].map((t) => ({ t, accs: nv.filter((x) => x.tier === t || declinedTier[x.node_id] === t) })).filter((g) => g.accs.length);
  const nodes = Object.fromEntries((c.graph?.nodes || []).map((n) => [n.id, n]));
  const label = { 4: "Hard freeze + STR draft", 3: "Hold suspicious outbound transfers (soft freeze)", 2: "Require step-up verification" };
  const reasonDetail = { 4: "strongly linked origin / layering accounts", 3: "money-trail accounts; incoming payments still allowed", 2: "accounts the Defense protected: verify, don't block" };
  const monitored = (c.graph?.nodes || []).filter((n) => n.group === "ring").length;
  const head = `<header><h2>Network ${esc(c.id)}</h2>${statusChip(c)}</header>
    <div class="cm-body"><div class="kv wide"><span>Network risk</span><b class="${(networkRisk(c) || 0) >= 0.8 ? "hi" : "mid"}">${pct(networkRisk(c))}</b><span>Court confidence</span><b>${v.confidence ? v.confidence.toFixed(2) : "0.92"}</b>
      <span>Amount at risk</span><b>${esc(inrShort(caseCapital(c)))}</b><span>Still in the network</span><b>${esc(inrShort(stillInNetwork(c)))}</b></div>
    <p class="sub"><a href="/investigations/${esc(c.id)}" data-link>Open the investigation graph →</a></p>`;
  if (!groups.length) return head + `<div class="empty"><strong>No restrictive action recommended</strong>No ring was proven; all ${monitored} accounts stay fully active.</div><div id="cmReports" class="report-links"></div></div>`;
  const reason = `<h3 class="minor">Reason</h3><p class="reason">${esc(v.summary)}</p>`;
  if (decide) {
    const actions = groups.map((g) => `<li class="act"><label><input type="checkbox" data-tier="${g.t}" ${off.has(g.t) ? "" : "checked"}>
      <b>${label[g.t]}</b> <span class="sub">— ${g.accs.length} account${g.accs.length > 1 ? "s" : ""}: ${reasonDetail[g.t]}</span></label>
      <div class="accs">${g.accs.map((x) => `<button class="acc${ex.has(x.node_id) ? " off" : ""}" data-acc="${esc(x.node_id)}" title="${esc(x.rationale)} — click to keep unrestricted">${esc(x.node_id)} <small>${pct(x.p_fraud)}</small></button>`).join("")}</div></li>`).join("");
    return head + `<h3 class="minor">Recommended actions</h3><ul class="actions-list">${actions}
      <li class="act fixed"><label><input type="checkbox" checked disabled> <b>Place the network under enhanced monitoring</b> <span class="sub">— all ${monitored} traced accounts, no customer impact</span></label></li>
      <li class="act fixed"><label><input type="checkbox" checked disabled> <b>Record your decision in the audit log</b> <span class="sub">— hash-chained, with your note</span></label></li></ul>${reason}
      <p class="sub">Untick an action, or click an account to keep it unrestricted. Nothing happens until you approve.</p>
      <div class="decide"><input id="cmNote" placeholder="Note for the audit log (e.g. verified with branch)">
      <button class="btn primary" id="cmApprove">Approve selected actions</button><button class="btn" id="cmReject">Reject all</button></div><p id="cmOut" class="sub" aria-live="polite"></p></div>`;
  }
  const executed = c.actions.filter((a) => (a.endpoint || "").startsWith("/bank/"));
  const by = c.approval ? (c.approval.decision === "approve" ? "Approved by the employee — executed (simulated)" : "Rejected by the employee — nothing executed")
    : c.route === "act" ? "Executed automatically by policy (confident, no collateral damage)" : "Awaiting execution";
  const rows = executed.flatMap((a) => (a.accounts || []).map((acc) => {
    const s = nodes[acc] && nodes[acc].stats; const held = s ? Math.max(0, s.received - s.forwarded) : null;
    return `<tr><td class="mono">${esc(acc)}</td><td>${esc(a.tier >= 4 ? "Account frozen, STR drafted" : a.tier === 3 ? "Outbound transfers on hold" : "Step-up verification requested")}</td>
      <td class="num mono">${held == null || a.tier < 3 ? "—" : inr(held)}</td><td><span class="txs ${a.tier >= 3 ? "held" : "flagged"}">${a.tier >= 3 ? "HELD" : "PENDING CUSTOMER"}</span></td></tr>`;
  })).join("");
  const declined = (c.approval?.declined || []).map((d) => `<li><span class="mono">${esc(d.account)}</span> — kept unrestricted (was: ${esc(TIER[d.proposed_tier])})</li>`).join("");
  return head + reason + `<h3 class="minor">${esc(by)}</h3>${c.approval?.note ? `<p class="sub">Note: ${esc(c.approval.note)}</p>` : ""}
    ${rows ? `<table class="mini"><tr><th>Account</th><th>Action</th><th class="num">Balance held</th><th>Status</th></tr>${rows}</table>` : `<p class="sub">No account was restricted.</p>`}
    ${declined ? `<p class="sub">Excluded by the employee:</p><ul class="plain">${declined}</ul>` : ""}
    ${c.customer.length ? `<h3 class="minor">Customer messages</h3>${c.customer.map((m) => `<p class="sms"><b class="mono">${esc(m.accounts.join(", "))}:</b> ${esc(m.text)}</p>`).join("")}` : ""}
    <h3 class="minor">Reports</h3><div id="cmReports" class="report-links"><span class="sub">Loading…</span></div></div>`;
}
async function loadReports(id) {
  for (let i = 0; i < 10; i++) {
    try {
      const r = await fetch(`/api/reports/${encodeURIComponent(id)}`);
      if (r.ok) {
        const reps = (await r.json()).reports; const el = $("#cmReports");
        if (el && APP.cmSelected === id) el.innerHTML = reps.map((x) => `<a class="btn small" href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title)} <small>${esc(x.format)}</small></a>`).join("");
        return;
      }
    } catch (err) { /* retry */ }
    await new Promise((res) => setTimeout(res, 500));
  }
}
$("#cmDetail").addEventListener("click", async (e) => {
  const c = APP.cases.get(APP.cmSelected); if (!c) return;
  const acc = e.target.closest("[data-acc]");
  if (acc) { const s = CM.excluded.get(c.id) || new Set(); if (s.has(acc.dataset.acc)) s.delete(acc.dataset.acc); else s.add(acc.dataset.acc); CM.excluded.set(c.id, s); acc.classList.toggle("off"); return; }
  if (e.target.id !== "cmApprove" && e.target.id !== "cmReject") return;
  const approve = e.target.id === "cmApprove";
  const off = CM.tiersOff.get(c.id) || new Set();
  const body = { decision: approve ? "approve" : "dismiss", note: $("#cmNote").value, exclude: [...(CM.excluded.get(c.id) || [])],
    approve_tiers: [4, 3, 2].filter((t) => !off.has(t)) };
  $("#cmApprove").disabled = $("#cmReject").disabled = true;
  $("#cmOut").textContent = approve ? "Executing the approved actions…" : "Recording the rejection…";
  try {
    const r = await fetch(`/api/review/${encodeURIComponent(c.id)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const out = await r.json();
    if (!r.ok) throw new Error(out.detail || r.statusText);
    if (!c.approval) c.approval = { decision: body.decision, note: body.note, declined: out.declined || [] };
    render(true);
  } catch (err) { $("#cmOut").textContent = `Failed: ${err.message}`; $("#cmApprove").disabled = $("#cmReject").disabled = false; }
});
$("#cmDetail").addEventListener("change", (e) => {
  const cb = e.target.closest("input[data-tier]"); const c = APP.cases.get(APP.cmSelected); if (!cb || !c) return;
  const s = CM.tiersOff.get(c.id) || new Set(); if (cb.checked) s.delete(Number(cb.dataset.tier)); else s.add(Number(cb.dataset.tier)); CM.tiersOff.set(c.id, s);
});
$("#cmQueue").addEventListener("click", (e) => { const a = e.target.closest("[data-cm]"); if (a) APP.cmSelected = a.dataset.cm; });

/* ============================== Controls ============================== */
async function startFeed() {
  const btn = $("#runDemo");
  if (btn) btn.disabled = true;
  try {
    const pick = $("#scenarioPick");
    const kind = pick ? pick.value : "demo_3am";
    const r = await fetch(`/api/demo/run?scenario=${encodeURIComponent(kind || "demo_3am")}`, { method: "POST" });
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    APP.status = { ...(APP.status || {}), demo_running: true };
    renderLive();
    if (ROUTE.page !== "dashboard") navigate("/dashboard");
  } catch (err) {
    const sub = $("#feedSub");
    if (sub) sub.textContent = `Could not start the feed: ${err.message}`;
  }
  setTimeout(() => {
    const b = $("#runDemo");
    if (b) b.disabled = false;
  }, 1500);
  setTimeout(loadStatus, 2000);
}

const runDemoBtn = $("#runDemo");
if (runDemoBtn) runDemoBtn.addEventListener("click", startFeed);

document.addEventListener("click", (e) => {
  if (e.target && e.target.id === "runDemoEmpty") {
    startFeed();
  }
});

/* Offline backup: play the last recorded case into the store as its own investigation. */
$("#replay").addEventListener("click", async () => {
  try {
    const r = await fetch("/api/recordings/latest");
    if (!r.ok) throw new Error((await r.json()).detail);
    const events = await r.json();
    const id = `${(events[0] && events[0].case_id) || "CASE"}-REPLAY-${Math.floor(Math.random() * 900 + 100)}`;
    events.forEach((ev) => setTimeout(() => apply({ ...ev, case_id: ev.case_id ? id : null }), ev.t * 1000));
    navigate(`/investigations/${encodeURIComponent(id)}`);
  } catch (err) { $("#feedSub").textContent = `Replay unavailable: ${err.message}`; }
});
$("#bell").addEventListener("click", () => { APP.unread = 0; navigate("/dashboard"); $("#alertList").scrollIntoView({ behavior: "smooth", block: "center" }); });
$("#invFilter").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-f]"); if (!b) return;
  APP.invFilter = b.dataset.f; $$("#invFilter button").forEach((x) => x.setAttribute("aria-pressed", String(x === b))); renderInvestigations();
});
document.addEventListener("click", (e) => {
  const row = e.target.closest("tr[data-case]"); if (row) navigate(`/investigations/${encodeURIComponent(row.dataset.case)}`);
});
setInterval(() => { loadStatus(); }, 10000);

/* ---------------- Moot court ---------------- */
async function loadMetrics() {
  let m;
  try { m = await (await fetch("/api/metrics")).json(); } catch { return; }
  const rounds = m.rounds || [];
  const last = rounds[rounds.length - 1];
  const base = m.baseline;
  $("#kRounds").textContent = rounds.length;
  $("#kCases").textContent = rounds.length ? `${rounds.reduce((s, r) => s + r.cases, 0)} cases tried` : "no cases yet";
  $("#kMissed").textContent = last ? last.benchmark.missed_per_100 : base ? base.missed_per_100 : "—";
  $("#kMissedBase").textContent = base ? `per 100 · started at ${base.missed_per_100}` : "per 100 cases";
  $("#kWrong").textContent = last ? last.benchmark.wrongful_per_100 : base ? base.wrongful_per_100 : "—";
  $("#kWrongBase").textContent = base ? `per 100 · started at ${base.wrongful_per_100}` : "per 100 cases";
  $("#kPrec").textContent = (m.precedents || []).filter((p) => p.side !== "policy").length;
  $("#kWindow").textContent = `money-trail window ${Math.round((m.policy?.max_hop_gap_s || 600) / 60)} min`;
  drawChart(base, rounds);
  drawDifficulty(m.difficulty || {}, m.policy || {});
  drawLog(m.log || []);
}

function drawChart(base, rounds) {
  const svg = $("#mootChart");
  if (!base) { svg.innerHTML = `<text x="320" y="150" text-anchor="middle" class="chart-label">Run the moot court to draw the learning curves</text>`; return; }
  const pts = [{ r: 0, m: base.missed_per_100, w: base.wrongful_per_100, red: null },
    ...rounds.map((r) => ({ r: r.round, m: r.benchmark.missed_per_100, w: r.benchmark.wrongful_per_100, red: r.red_team_success_pct }))];
  const W = 640, H = 300, L = 44, R = 16, T = 16, B = 34;
  const maxY = Math.max(50, ...pts.map((p) => Math.max(p.m, p.w, p.red || 0)));
  const x = (i) => L + (i * (W - L - R)) / Math.max(1, pts.length - 1);
  const y = (v) => T + (H - T - B) * (1 - v / maxY);
  let out = "";
  for (let v = 0; v <= maxY; v += maxY > 60 ? 20 : 10) {
    out += `<line class="chart-grid" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text class="chart-label" x="${L - 8}" y="${y(v) + 4}" text-anchor="end">${v}</text>`;
  }
  pts.forEach((p, i) => { out += `<text class="chart-label" x="${x(i)}" y="${H - 12}" text-anchor="middle">${i === 0 ? "base" : "R" + p.r}</text>`; });
  const line = (key, cls) => {
    const valid = pts.map((p, i) => [i, p[key]]).filter(([, v]) => v !== null);
    return `<polyline class="${cls}" points="${valid.map(([i, v]) => `${x(i)},${y(v)}`).join(" ")}"/>` +
      (cls === "line-red" ? "" : valid.map(([i, v]) => `<circle cx="${x(i)}" cy="${y(v)}" r="3.5" fill="var(${cls === "line-missed" ? "--red" : "--teal"})"><title>${v}</title></circle>`).join(""));
  };
  out += line("red", "line-red") + line("m", "line-missed") + line("w", "line-wrong");
  svg.innerHTML = out;
}

function drawDifficulty(d, policy) {
  if (!d.framing_kinds) return;
  const rows = [
    ["Longest wait between hops", `${d.max_hop_delay_min} min`],
    ["Oldest mule accounts", `${d.max_mule_age_days} days`],
    ["Decoy transactions", `${d.max_decoy_pct}%`],
    ["Deepest ring", `${d.max_layers} layers`],
    ["Framing scenarios", d.framing_kinds.map((k) => k.replace("_", " ")).join(", ")],
    ["Scout window (learned)", `${Math.round((policy.max_hop_gap_s || 600) / 60)} min`],
  ];
  $("#difficulty").innerHTML = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
}

function drawLog(log) {
  if (!log.length) return;
  $("#logBody").innerHTML = [...log].reverse().slice(0, 15).map((l) => `<tr><td>R${l.round}</td><td>${esc(l.type)}</td>
    <td>${esc(l.what)}</td><td>${esc(l.stored)}</td>
    <td class="${l.fixed_round ? "status-fixed" : "status-open"}">${l.fixed_round ? "Caught from R" + l.fixed_round : "Still open"}</td></tr>`).join("");
}

async function startMoot(reset) {
  $("#mootRun").disabled = $("#mootReset").disabled = true;
  $("#mootProgress").textContent = "Starting\u2026";
  try {
    const r = await fetch(`/api/moot/run?rounds=8&cases=20${reset ? "&reset=true" : ""}`, { method: "POST" });
    if (!r.ok) {
      const err = await r.json().catch(() => ({ detail: r.statusText }));
      $("#mootProgress").textContent = `\u26a0 ${err.detail || r.statusText}`;
      $("#mootRun").disabled = $("#mootReset").disabled = false;
    }
    // success: buttons re-enabled when moot.done fires via WebSocket
  } catch (err) {
    $("#mootProgress").textContent = `\u26a0 Could not reach server: ${err.message}`;
    $("#mootRun").disabled = $("#mootReset").disabled = false;
  }
}
function mootFinished(p) {
  const secs = Number(p.elapsed_s) || 0;
  const timeStr = secs < 60 ? `${secs.toFixed(1)} s` : `${(secs / 60).toFixed(1)} min`;
  $("#mootProgress").textContent = `\u2713 Finished ${p.rounds ?? ""} rounds in ${timeStr}`;
  $("#mootRun").disabled = $("#mootReset").disabled = false;
  loadMetrics();
}
$("#mootRun").addEventListener("click", () => startMoot(false));
$("#mootReset").addEventListener("click", () => startMoot(true));


/* ============================== Start ============================== */
route();
connect();
loadScenarios();
backfill();