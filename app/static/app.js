"use strict";

/* LLM Proxy console — vanilla JS, no build step.
 * Talks to the proxy's own endpoints: GET/POST /logging, GET /v1/models,
 * and the auth-gated GET /admin/logs, GET /admin/upstream-models,
 * GET/POST /admin/routing. The bearer key (if any) is kept in localStorage and
 * sent as Authorization: Bearer <key> on every call. */

const KEY_STORE = "llmproxy.key";
const LOG_POLL_MS = 1500;
const MAX_LOG_LINES = 3000;

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ── Auth + fetch ──────────────────────────────────────────── */
const getKey = () => localStorage.getItem(KEY_STORE) || "";
const setKey = (k) => localStorage.setItem(KEY_STORE, k);

function authHeaders(extra = {}) {
  const k = getKey();
  return k ? { ...extra, Authorization: "Bearer " + k } : { ...extra };
}

function setConn(ok) {
  const dot = $("#conn-dot");
  dot.classList.toggle("ok", ok === true);
  dot.classList.toggle("bad", ok === false);
  dot.title = ok == null ? "unknown" : ok ? "connected" : "error / unauthorized";
}

async function api(path, opts = {}) {
  try {
    const res = await fetch(path, { ...opts, headers: authHeaders(opts.headers) });
    setConn(res.ok);
    return res;
  } catch (e) {
    setConn(false);
    throw e;
  }
}

let toastTimer = null;
function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show " + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = "toast"; }, 3000);
}

/* ── Tabs ──────────────────────────────────────────────────── */
function activateTab(name) {
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
  if (name === "logging") startLogPolling(); else stopLogPolling();
  if (name === "models") loadCatalog();
  if (name === "routing") loadRouting();
}

/* ── Logging: flags ────────────────────────────────────────── */
async function loadLogFlags() {
  try {
    const res = await api("/logging");
    if (!res.ok) return;
    const d = await res.json();
    $("#log-input").checked = !!d.log_input;
    $("#log-output").checked = !!d.log_output;
  } catch { /* connection dot already reflects it */ }
}

async function setLogFlag(key, value) {
  const res = await api("/logging", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ [key]: value }),
  });
  if (res.status === 403) { toast("Unauthorized — set a valid bearer key", "bad"); await loadLogFlags(); return; }
  if (!res.ok) { toast("Failed to update " + key, "bad"); await loadLogFlags(); return; }
  const d = await res.json();
  $("#log-input").checked = !!d.log_input;
  $("#log-output").checked = !!d.log_output;
  toast(key + " = " + value);
}

/* ── Logging: live tail ────────────────────────────────────── */
let logTimer = null;
let lastSeq = 0;
let logLineCount = 0;
let logBlocked = false;

function startLogPolling() {
  if (logTimer) return;
  pollLogs();
  logTimer = setInterval(pollLogs, LOG_POLL_MS);
}
function stopLogPolling() {
  clearInterval(logTimer);
  logTimer = null;
}

async function pollLogs() {
  if ($("#log-pause").checked) return;
  const level = $("#log-level").value;
  let res;
  try { res = await api(`/admin/logs?since=${lastSeq}&level=${encodeURIComponent(level)}`); }
  catch { return; }
  if (res.status === 403) {
    if (!logBlocked) { renderLogNotice("Unauthorized — enter a valid bearer key above to view logs."); logBlocked = true; }
    return;
  }
  if (!res.ok) return;
  if (logBlocked) { logBlocked = false; $("#log-pane").innerHTML = ""; }
  const data = await res.json();
  lastSeq = data.last_seq;
  if (data.entries && data.entries.length) appendLogs(data.entries);
}

function renderLogNotice(text) {
  $("#log-pane").innerHTML = `<div class="notice">${escapeHtml(text)}</div>`;
}

const logQuery = () => ($("#log-search").value || "").toLowerCase().trim();

// Each line keeps its original (un-highlighted) text so the grep box can
// re-render highlights from scratch on every keystroke without losing data.
const logMeta = new WeakMap();

// Append `text` to `parent`, wrapping case-insensitive matches of `q` in <mark>.
function appendHighlighted(parent, text, q) {
  if (!q) { parent.appendChild(document.createTextNode(text)); return; }
  const lower = text.toLowerCase();
  let i = 0, idx;
  while ((idx = lower.indexOf(q, i)) !== -1) {
    if (idx > i) parent.appendChild(document.createTextNode(text.slice(i, idx)));
    parent.appendChild(el("mark", "hl", text.slice(idx, idx + q.length)));
    i = idx + q.length;
  }
  if (i < text.length) parent.appendChild(document.createTextNode(text.slice(i)));
}

function renderHighlighted(span, text, q) {
  span.textContent = "";
  appendHighlighted(span, text, q);
}

// Re-render a matching line's logger + message with the query highlighted (plain
// when q is empty). The full message is highlighted even while collapsed, so
// expanding a hit found in a request/response body reveals the mark.
function highlightLine(line, q) {
  const meta = logMeta.get(line);
  if (!meta) return;
  const lg = line.querySelector(".lg");
  if (lg) renderHighlighted(lg, meta.logger, q);
  const lm = line.querySelector(".lm");
  if (lm) renderHighlighted(lm, meta.msg, q);
  const prev = line.querySelector(".lm-preview");
  if (prev) {
    const more = prev.querySelector(".more");
    prev.textContent = "";
    appendHighlighted(prev, meta.firstLine, q);
    if (more) prev.appendChild(more);
  }
}

// Build one log line. Multi-line messages (the curl-style Request/Response dumps
// from LOG_INPUT/LOG_OUTPUT) become collapsible: a ▶/▼ toggle, a one-line preview
// when collapsed, the full pre-wrapped text when expanded. Single-line entries
// render as-is. The full text is stashed on dataset.search so the grep box can
// match even content hidden inside a collapsed entry.
function makeLogLine(e) {
  const level = e.level || "INFO";
  const msg = e.msg || "";
  const nl = msg.indexOf("\n");
  const multiline = nl !== -1;
  const line = el("div", "logline lvl-" + level.toLowerCase() + (multiline ? " multiline collapsed" : ""));
  // Grep matches logger + message (level has its own dropdown), so what you can
  // search is exactly what gets highlighted.
  line.dataset.search = ((e.logger || "") + " " + msg).toLowerCase();
  logMeta.set(line, { logger: e.logger || "", msg, firstLine: multiline ? msg.slice(0, nl) : msg });

  let tog = null;
  if (multiline) {
    tog = el("button", "ltog", "▶");
    tog.title = "expand / collapse";
    tog.addEventListener("click", () => {
      const collapsed = line.classList.toggle("collapsed");
      tog.textContent = collapsed ? "▶" : "▼";
    });
    line.appendChild(tog);
  }
  line.append(
    el("span", "lt", (e.ts || "").replace("T", " ")),
    el("span", "lv", level),
    el("span", "lg", e.logger || "")
  );
  if (multiline) {
    const preview = el("span", "lm-preview");
    preview.appendChild(document.createTextNode(msg.slice(0, nl) || "(multi-line)"));
    preview.appendChild(el("span", "more", `⋯ +${msg.split("\n").length - 1} lines`));
    preview.addEventListener("click", () => { line.classList.remove("collapsed"); if (tog) tog.textContent = "▼"; });
    line.appendChild(preview);
  }
  line.appendChild(el("span", "lm", msg));
  return line;
}

function appendLogs(entries) {
  const pane = $("#log-pane");
  const atBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 40;
  const q = logQuery();
  const frag = document.createDocumentFragment();
  for (const e of entries) {
    const line = makeLogLine(e);
    if (q) {
      if (line.dataset.search.includes(q)) highlightLine(line, q);
      else line.style.display = "none";
    }
    frag.appendChild(line);
  }
  pane.appendChild(frag);
  logLineCount += entries.length;
  while (pane.childElementCount > MAX_LOG_LINES) pane.removeChild(pane.firstChild);
  $("#log-count").textContent = logLineCount.toLocaleString() + " lines";
  if ($("#log-autoscroll").checked && atBottom) pane.scrollTop = pane.scrollHeight;
}

// Re-evaluate every buffered line against the grep box (skips the notice div).
function applyLogFilter() {
  const q = logQuery();
  for (const ln of $("#log-pane").children) {
    if (!ln.dataset || ln.dataset.search === undefined) continue;
    const match = !q || ln.dataset.search.includes(q);
    ln.style.display = match ? "" : "none";
    if (match) highlightLine(ln, q);
  }
}

function resetLogTail() {
  lastSeq = 0;
  logLineCount = 0;
  logBlocked = false;
  $("#log-pane").innerHTML = "";
  $("#log-count").textContent = "";
}

/* ── Models ────────────────────────────────────────────────── */
async function loadCatalog() {
  const wrap = $("#catalog");
  let res;
  try { res = await api("/v1/models"); }
  catch { wrap.innerHTML = '<div class="notice">Cannot reach the proxy.</div>'; return; }
  if (!res.ok) { wrap.innerHTML = '<div class="notice">Failed to load catalog.</div>'; return; }
  const data = await res.json();
  const items = (data.data || []).slice().sort((a, b) => a.id.localeCompare(b.id));
  $("#models-count").textContent = items.length + " models";
  wrap.innerHTML = "";
  if (!items.length) { wrap.innerHTML = '<div class="notice">No models listed.</div>'; return; }
  for (const m of items) {
    const row = el("div", "mrow");
    row.append(el("span", "mid", m.id), el("span", "mowner", m.owned_by || ""));
    wrap.appendChild(row);
  }
}

async function probeUpstreams() {
  const btn = $("#models-probe");
  const wrap = $("#upstreams");
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = "Probing…";
  try {
    const res = await api("/admin/upstream-models");
    if (res.status === 403) { wrap.innerHTML = '<div class="notice">Unauthorized — set a valid bearer key.</div>'; return; }
    if (!res.ok) { wrap.innerHTML = '<div class="notice">Probe failed.</div>'; return; }
    const data = await res.json();
    wrap.classList.remove("hint");
    wrap.innerHTML = "";
    for (const p of data.providers || []) {
      const card = el("div", "ucard" + (p.ok ? "" : " err"));
      const head = el("div", "uhead");
      head.appendChild(el("span", "uname", p.provider));
      head.appendChild(p.ok ? el("span", "ucount", p.ids.length + " ids") : el("span", "ubad", "unreachable"));
      card.appendChild(head);
      if (p.ok) {
        const list = el("div", "ulist");
        if (!p.ids.length) list.appendChild(el("span", "muted", "(empty)"));
        for (const id of p.ids) list.appendChild(el("span", "uid", id));
        card.appendChild(list);
      } else {
        card.appendChild(el("div", "uerr", p.error || "error"));
      }
      wrap.appendChild(card);
    }
  } catch {
    wrap.innerHTML = '<div class="notice">Probe failed.</div>';
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

/* ── Routing ───────────────────────────────────────────────── */
async function loadRouting() {
  let res;
  try { res = await api("/admin/routing"); }
  catch { $("#logical").innerHTML = '<div class="notice">Cannot reach the proxy.</div>'; return; }
  if (res.status === 403) {
    $("#providers").innerHTML = "";
    $("#aliases").innerHTML = "";
    $("#logical").innerHTML = '<div class="notice">Unauthorized — set a valid bearer key to view routing.</div>';
    return;
  }
  if (!res.ok) { $("#logical").innerHTML = '<div class="notice">Failed to load routing.</div>'; return; }
  const data = await res.json();
  $("#routing-autogroup").textContent = "auto_group: " + data.auto_group;
  const cfg = $("#routing-config");
  if (data.config_writable) {
    cfg.textContent = "config: writable";
    cfg.style.color = "";
    cfg.title = "priority changes are written back to the config file";
  } else {
    cfg.textContent = "config: read-only — changes won't survive a restart";
    cfg.style.color = "var(--amber)";
    cfg.title = "the config file mount is read-only; drop :ro on the volume to persist changes";
  }
  const downSet = new Set((data.providers || []).filter((p) => p.is_down).map((p) => p.name));
  renderProviders(data.providers || []);
  renderLogical(data.logical_models || [], downSet);
  renderAliases(data.aliases || {});
}

function renderProviders(providers) {
  const wrap = $("#providers");
  wrap.innerHTML = "";
  wrap.appendChild(el("h2", null, "Providers"));
  const grid = el("div", "pgrid");
  for (const p of providers) {
    const chip = el("div", "pchip" + (p.is_down ? " down" : ""));
    chip.title = p.base_url || "";
    chip.appendChild(el("span", "pname", p.name));
    chip.appendChild(el("span", "pslot", p.slots == null ? "∞" : `${p.in_use}/${p.slots}`));
    if (p.require_permission) { const f = el("span", "pflag lock", "🔒"); f.title = "require_permission"; chip.appendChild(f); }
    if (p.lists_all) { const f = el("span", "pflag", "live"); f.title = "live-discovers all models"; chip.appendChild(f); }
    if (p.is_down) chip.appendChild(el("span", "pflag downflag", "down"));
    grid.appendChild(chip);
  }
  wrap.appendChild(grid);
}

function renderLogical(models, downSet) {
  const wrap = $("#logical");
  wrap.innerHTML = "";
  wrap.appendChild(el("h2", null, "Model routing"));
  if (!models.length) { wrap.appendChild(el("div", "hint", "No explicit logical models configured.")); return; }
  for (const m of models) wrap.appendChild(modelCard(m, downSet));
}

function modelCard(model, downSet) {
  const card = el("div", "mcard");
  let targets = model.targets.map((t) => ({ ...t })); // working copy

  const head = el("div", "mhead");
  head.appendChild(el("span", "mname", model.name));
  head.appendChild(el("span", "mtcount muted", targets.length + " targets"));
  const actions = el("div", "mactions");
  const resetBtn = el("button", "btn", "Reset");
  const saveBtn = el("button", "btn primary", "Save");
  saveBtn.disabled = true;
  actions.append(resetBtn, saveBtn);
  head.appendChild(actions);
  card.appendChild(head);

  const list = el("div", "tlist");
  card.appendChild(list);

  const markDirty = () => { saveBtn.disabled = false; };

  // Display in priority order; JS sort is stable so equal priorities keep order.
  function refresh() {
    targets.sort((a, b) => a.priority - b.priority);
    renderRows();
  }

  // Up/down reorders array position and renumbers 1..N (a clean strict order).
  // Manual priority edits stay as typed, so ties remain expressible there.
  function move(i, dir) {
    const j = i + dir;
    if (j < 0 || j >= targets.length) return;
    const [item] = targets.splice(i, 1);
    targets.splice(j, 0, item);
    targets.forEach((t, idx) => { t.priority = idx + 1; });
    markDirty();
    renderRows();
  }

  function renderRows() {
    list.innerHTML = "";
    targets.forEach((t, i) => {
      const down = downSet.has(t.provider);
      const row = el("div", "trow" + (down ? " down" : ""));

      const ctrls = el("div", "tctrls");
      const up = el("button", "mini", "↑"); up.disabled = i === 0; up.onclick = () => move(i, -1);
      const dn = el("button", "mini", "↓"); dn.disabled = i === targets.length - 1; dn.onclick = () => move(i, 1);
      ctrls.append(up, dn);

      const info = el("div", "tinfo");
      info.append(el("span", "tprov", t.provider), el("span", "tmid", t.model));

      const badges = el("div", "tbadges");
      if (down) badges.appendChild(el("span", "bdown", "down"));
      if (t.known_provider === false) { const w = el("span", "bwarn", "unknown"); w.title = "provider not in config"; badges.appendChild(w); }

      const prioWrap = el("label", "tprio");
      prioWrap.appendChild(el("span", null, "priority"));
      const prio = el("input"); prio.type = "number"; prio.min = "1"; prio.value = t.priority;
      prio.addEventListener("change", () => {
        const v = parseInt(prio.value, 10);
        t.priority = Number.isFinite(v) && v > 0 ? v : 1;
        prio.value = t.priority;
        markDirty();
      });
      prioWrap.appendChild(prio);

      row.append(ctrls, info, badges, prioWrap);
      list.appendChild(row);
    });
  }

  resetBtn.onclick = () => {
    targets = model.targets.map((t) => ({ ...t }));
    saveBtn.disabled = true;
    refresh();
  };

  saveBtn.onclick = async () => {
    saveBtn.disabled = true;
    const label = saveBtn.textContent;
    saveBtn.textContent = "Saving…";
    try {
      const res = await api("/admin/routing/" + encodeURIComponent(model.name), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          targets: targets.map((t) => ({ provider: t.provider, model: t.model, priority: t.priority })),
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        toast(err.error || `Save failed (${res.status})`, "bad");
        saveBtn.disabled = false;
        saveBtn.textContent = label;
        return;
      }
      const updated = await res.json();
      model.targets = updated.targets.map((t) => ({ ...t })); // new baseline for Reset
      targets = updated.targets.map((t) => ({ ...t }));
      refresh();
      saveBtn.textContent = "Saved ✓";
      if (updated.persisted) {
        toast("Saved " + model.name + " (live + config)");
      } else {
        toast("Saved " + model.name + " live only — not persisted: " + (updated.persist_error || "unknown"), "bad");
      }
      setTimeout(() => { saveBtn.textContent = label; }, 1200);
    } catch {
      toast("Save error", "bad");
      saveBtn.disabled = false;
      saveBtn.textContent = label;
    }
  };

  refresh();
  return card;
}

function renderAliases(aliases) {
  const wrap = $("#aliases");
  wrap.innerHTML = "";
  const keys = Object.keys(aliases);
  if (!keys.length) { wrap.appendChild(el("span", "muted", "none")); return; }
  for (const k of keys) {
    const row = el("div", "arow");
    row.append(el("span", "aname", k), el("span", "aarrow", "→"), el("span", "atarget", aliases[k]));
    wrap.appendChild(row);
  }
}

/* ── Init ──────────────────────────────────────────────────── */
function initKey() {
  $("#api-key").value = getKey();
  const save = () => {
    setKey($("#api-key").value.trim());
    toast("Key saved");
    const active = ($(".tab.active") || {}).dataset?.tab;
    loadLogFlags();
    if (active === "logging") resetLogTail();
    if (active === "models") { loadCatalog(); }
    if (active === "routing") loadRouting();
  };
  $("#save-key").addEventListener("click", save);
  $("#api-key").addEventListener("keydown", (e) => { if (e.key === "Enter") save(); });
  // No key yet → put the cursor in the field so it's obvious where to start.
  if (!getKey()) $("#api-key").focus();
}

window.addEventListener("DOMContentLoaded", () => {
  initKey();
  $$(".tab").forEach((b) => b.addEventListener("click", () => activateTab(b.dataset.tab)));

  $("#log-input").addEventListener("change", (e) => setLogFlag("log_input", e.target.checked));
  $("#log-output").addEventListener("change", (e) => setLogFlag("log_output", e.target.checked));
  $("#log-level").addEventListener("change", resetLogTail);
  $("#log-search").addEventListener("input", applyLogFilter);
  $("#log-clear").addEventListener("click", () => { $("#log-pane").innerHTML = ""; logLineCount = 0; $("#log-count").textContent = ""; });

  $("#models-refresh").addEventListener("click", loadCatalog);
  $("#models-probe").addEventListener("click", probeUpstreams);
  $("#routing-refresh").addEventListener("click", loadRouting);

  loadLogFlags();
  activateTab("logging");
});
