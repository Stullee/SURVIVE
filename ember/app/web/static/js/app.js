// Ember dashboard. Plain JS, no build step.
// Every URL is relative so it resolves against <base href>, i.e. through Home Assistant Ingress.
// All text from the API is inserted with textContent (never innerHTML).
"use strict";

(function () {
  var POLL_RUNNING_MS = 3000;
  var POLL_IDLE_MS = 30000;
  var POLL_ERROR_MS = 15000;
  // After "Wake now" the cycle starts within seconds; poll fast until it shows up (or this runs out).
  var WAKE_FAST_POLL_MS = 20000;
  // Neither Ingress hop times out on its own, so a hung backend would otherwise freeze the page.
  var REQUEST_TIMEOUT_MS = 10000;
  // The diagnostics report gathers the whole system, which may take longer than a dashboard poll.
  var DIAGNOSTICS_TIMEOUT_MS = 30000;
  // Narrower balance charts have no room for grant labels; the tooltip and the table still show them.
  var GRANT_LABEL_MIN_WIDTH = 480;

  var ui = {
    data: null,
    tab: loadPref("ember-tab", "overview"),
    mind: "strategy",
    tableOpen: false,
    timer: null,
    seq: 0,              // only the response to the latest dashboard request is used
    fetchError: null,
    rendered: {},        // section name -> JSON of the data it was last rendered from
    sectionErrors: {},
    bannerKey: null,
    days: null,
    charts: { flow: null, balance: null },
    chartFallback: false,
    hiddenSeries: {},
    forms: {},
    correction: null,
    ledgerById: {},
    controlBusy: false,
    wakeBusy: false,
    fastPollUntil: 0,    // Date.now() until which the dashboard is polled fast (after "Wake now")
    controlTimer: null,
    cycles: {},          // cycle id -> the Activity item built for it (patched in place on each poll)
    nowPlanKey: null,
    diag: { text: null, loadedAt: null, busy: false, full: false },
    // The agent's files: the list and the open file are loaded when the tab opens and on Refresh, never polled.
    ws: { list: null, loadedAt: null, busy: false, error: null, file: null, fileBusy: false, fileError: null, fileSeq: 0 },
    // The venture tree: loaded while its tab is open, again whenever the dashboard's ventures_stamp changes.
    // files: venture id -> what Ember learned about it (its knowledge file), loaded when the owner opens it.
    vt: { data: null, byId: {}, stamp: null, loadedAt: null, busy: false, again: false, error: null, selected: null,
      files: {}, saving: false },
    // The roadmap (0.11.0): loaded while its tab is open, again whenever the dashboard's roadmap stamp changes.
    rm: { data: null, byId: {}, stamp: null, busy: false, again: false, error: null, selected: null, saving: false },
    // The library (0.12.0): loaded while its tab is open, again whenever the dashboard's library stamp changes.
    // docs: document id -> its text and learnings, loaded when the owner opens them.
    lib: { data: null, stamp: null, busy: false, again: false, error: null, saving: false, docs: {} },
    refocus: null,       // after the owner's own action: the card status line to focus once the list is re-rendered
    sending: false,      // an inbox message is on its way
    // The standing instructions' editor: open while the owner edits (polls never touch it then), whether a save is on its
    // way, and the text last warned about as looking like a password (saved as it is if Save is pressed again).
    instructions: { editing: false, saving: false, secretWarned: null },
    markingRead: false,
    killBusy: false,
  };

  // The phase-1 scenario switcher is gone; drop its stored choice.
  try { window.localStorage.removeItem("ember-scenario"); } catch (e) { /* storage unavailable */ }

  // ------------------------------------------------------------------ helpers

  function $(id) { return document.getElementById(id); }

  // ---- Staying in step with the server

  // The build this page was served with. After an update of the app, a page that stayed open would run
  // the old script against the new server's data, so it reloads itself (once per new build).
  var PAGE_BUILD = (function () {
    var meta = document.querySelector('meta[name="ember-build"]');
    return meta ? meta.getAttribute("content") || "" : "";
  })();
  var RELOAD_MARK = "ember-reloaded-for:";

  function reloadedFor(build) {
    var mark = RELOAD_MARK + build;
    try { if (window.sessionStorage.getItem("ember-reloaded-for") === build) return true; } catch (e) { /* ignore */ }
    return window.name === mark;
  }

  function markReload(build) {
    try { window.sessionStorage.setItem("ember-reloaded-for", build); } catch (e) { /* window.name still works */ }
    window.name = RELOAD_MARK + build;
  }

  // Returns true when the page is reloading and the data must not be rendered.
  function followServerBuild(d) {
    var server = isObject(d.system) && typeof d.system.build === "string" ? d.system.build : "";
    ui.buildMismatch = false;
    if (!server || !PAGE_BUILD || server === PAGE_BUILD) return false;
    if (!reloadedFor(server)) {
      markReload(server);
      window.location.reload();
      return true;
    }
    ui.buildMismatch = true;  // reloading didn't help (for example a cached page): ask the owner
    return false;
  }

  function loadPref(key, fallback) {
    try { return window.localStorage.getItem(key) || fallback; } catch (e) { return fallback; }
  }

  function savePref(key, value) {
    try { window.localStorage.setItem(key, value); } catch (e) { /* storage unavailable */ }
  }

  // h("div", {class: "x", "data-state": "y"}, child, "text", ...) builds DOM safely.
  function h(tag, props) {
    var node = document.createElement(tag);
    if (props) {
      Object.keys(props).forEach(function (key) {
        var value = props[key];
        if (value === null || value === undefined || value === false) return;
        if (key === "text") node.textContent = value;
        else if (key === "class") node.className = value;
        else if (key === "hidden") node.hidden = true;
        else if (key === "disabled") node.disabled = true;
        else if (key === "open") node.open = true;
        else node.setAttribute(key, value === true ? "" : String(value));
      });
    }
    for (var i = 2; i < arguments.length; i++) append(node, arguments[i]);
    return node;
  }

  function append(node, child) {
    if (child === null || child === undefined || child === false) return;
    if (Array.isArray(child)) { child.forEach(function (c) { append(node, c); }); return; }
    node.appendChild(typeof child === "string" || typeof child === "number" ? document.createTextNode(String(child)) : child);
  }

  function replace(node, children) {
    while (node.firstChild) node.removeChild(node.firstChild);
    append(node, children);
  }

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function isObject(x) { return x !== null && typeof x === "object" && !Array.isArray(x); }
  function arr(x) { return Array.isArray(x) ? x : []; }
  function errorText(err) { return String(err && err.message ? err.message : err); }
  function sentence(text) { text = String(text); return text.charAt(0).toUpperCase() + text.slice(1); }

  // ------------------------------------------------------------------ formatting

  var usdSmall = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 4 });
  var usdLarge = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
  var usdTick = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 2 });
  // Ledger amounts and configured prices: every stored digit, so they can be checked exactly.
  var usdExact = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 6 });
  var pctFmt = new Intl.NumberFormat(undefined, { style: "percent", maximumFractionDigits: 1 });
  var dayFmt = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });
  var dayLongFmt = new Intl.DateTimeFormat(undefined, { weekday: "short", month: "short", day: "numeric", year: "numeric" });
  var dateFmt = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" });
  var dateTimeFmt = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
  var timeFmt = new Intl.DateTimeFormat(undefined, { timeStyle: "medium" });
  var shortTimeFmt = new Intl.DateTimeFormat(undefined, { timeStyle: "short" });
  var relFmt = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });

  function num(value) { return value === null || value === undefined || value === "" ? NaN : Number(value); }

  function usd(value) {
    var v = num(value);
    if (isNaN(v)) return "–";
    return Math.abs(v) < 1 && v !== 0 ? usdSmall.format(v) : usdLarge.format(v);
  }

  // "+$5.00" / "−$0.0213": the sign says which way the balance moves.
  function signedUsd(value) {
    var v = num(value);
    if (isNaN(v)) return "–";
    var text = usdExact.format(Math.abs(v));
    return v > 0 ? "+" + text : v < 0 ? "−" + text : text;
  }

  function pct(value) { var v = num(value); return isNaN(v) ? "–" : pctFmt.format(v); }

  function plural(n, word) {
    var v = num(n);
    if (isNaN(v)) return "– " + word + "s";
    var text = new Intl.NumberFormat(undefined, { maximumFractionDigits: v < 10 ? 1 : 0 }).format(v);
    return text + " " + word + (text === "1" ? "" : "s");
  }

  function parseDay(iso) {
    // "2026-09-27" as a local calendar day (not UTC midnight).
    var p = String(iso || "").split("-");
    return new Date(Number(p[0]), Number(p[1]) - 1, Number(p[2]));
  }

  function validDate(d) { return !isNaN(d.getTime()); }
  function fmtDay(iso) { var d = parseDay(iso); return iso && validDate(d) ? dayLongFmt.format(d) : "–"; }
  function fmtDate(iso) { var d = new Date(iso); return iso && validDate(d) ? dateFmt.format(d) : "–"; }
  function fmtDateTime(iso) { var d = new Date(iso); return iso && validDate(d) ? dateTimeFmt.format(d) : "–"; }

  function relTime(iso) {
    if (!iso) return "–";
    var t = new Date(iso).getTime();
    if (isNaN(t)) return "–";
    var seconds = (t - Date.now()) / 1000;
    var abs = Math.abs(seconds);
    if (Math.round(abs) < 60) return relFmt.format(Math.round(seconds), "second");
    // Round to whole minutes first so 3 h 59.8 min reads "4 h", never "3 h 60 min".
    var total = Math.round(abs / 60);
    if (total < 60) return relFmt.format(seconds < 0 ? -total : total, "minute");
    if (abs < 86400 * 2) {
      var text = Math.floor(total / 60) + " h" + (total % 60 ? " " + (total % 60) + " min" : "");
      return seconds > 0 ? "in " + text : text + " ago";
    }
    return relFmt.format(Math.round(seconds / 86400), "day");
  }

  function timeEl(iso, text) {
    return h("time", { datetime: iso, title: fmtDateTime(iso), text: text || relTime(iso) });
  }

  var intFmt = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
  var oneDecimalFmt = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });

  function count(value) { var v = num(value); return isNaN(v) ? "–" : intFmt.format(v); }

  // "42 s", "3 min", "1 h 5 min" between two ISO times (the second defaults to now).
  function duration(fromIso, toIso) {
    var a = new Date(fromIso).getTime();
    var b = toIso ? new Date(toIso).getTime() : Date.now();
    if (!fromIso || isNaN(a) || isNaN(b) || b < a) return "";
    var seconds = Math.round((b - a) / 1000);
    if (seconds < 60) return seconds + " s";
    var minutes = Math.round(seconds / 60);
    if (minutes < 60) return minutes + " min";
    return Math.floor(minutes / 60) + " h" + (minutes % 60 ? " " + (minutes % 60) + " min" : "");
  }

  function byteSize(n) {
    if (n < 1024) return intFmt.format(n) + " bytes";
    if (n < 1024 * 1024) return oneDecimalFmt.format(n / 1024) + " KB";
    return oneDecimalFmt.format(n / (1024 * 1024)) + " MB";
  }

  // Agent-written values are shown as plain text; anything that isn't a string is shown as JSON.
  function asText(value) {
    if (value === null || value === undefined) return "";
    if (typeof value === "string") return value;
    try { return JSON.stringify(value, null, 2); } catch (e) { return String(value); }
  }

  // Tool inputs arrive as JSON text: indent them for reading, or show them as they are.
  function prettyJson(value) {
    if (typeof value !== "string") return asText(value);
    try { return JSON.stringify(JSON.parse(value), null, 2); } catch (e) { return value; }
  }

  // Server texts may or may not end with a full stop.
  function endSentence(text) {
    text = String(text).trim();
    return /[.!?…]$/.test(text) ? text : text + ".";
  }

  function lowerFirst(text) { text = String(text); return text.charAt(0).toLowerCase() + text.slice(1); }

  function versionLabel(version) {
    if (version === null || version === undefined) return "";
    return /^\d/.test(String(version)) ? "v" + version : String(version);
  }

  // Owner-local today: the last day of the economy series, else the browser's today.
  function todayIso() {
    var days = ui.data && isObject(ui.data.economy) ? arr(ui.data.economy.days) : [];
    if (days.length && days[days.length - 1].date) return days[days.length - 1].date;
    var d = new Date();
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  }

  function isDryRun(d) {
    if (!d) return false;
    if (d.mode === "dry_run" || d.mode === "live") return d.mode === "dry_run";
    return !!(d.system && d.system.dry_run);
  }

  function agentName() { return (ui.data && ui.data.agent && ui.data.agent.name) || "Ember"; }

  // ------------------------------------------------------------------ status vocabularies (icon + label, never color alone)

  var LIFE_STATES = {
    alive: { icon: "●", label: "Alive" },
    critical: { icon: "▲", label: "Critical" },
    paused: { icon: "❚❚", label: "Paused" },
    unfunded: { icon: "○", label: "Waiting for money" },
    killed: { icon: "■", label: "Killed" },
    dead: { icon: "✝", label: "Dead" },
    dormant: { icon: "◌", label: "Dormant" },
    ended: { icon: "◌", label: "Session ended" },
    "new": { icon: "○", label: "Born" },
    unknown: { icon: "…", label: "Unknown" },
  };

  function stateLabel(state) {
    if (!state) return "Start";
    return (LIFE_STATES[state] || { label: sentence(state) }).label;
  }

  var PROJECT_STATUS = {
    active: { icon: "●", label: "Active", tone: "accent", order: 0 },
    waiting: { icon: "◔", label: "Waiting", tone: "warning", order: 1 },
    idea: { icon: "○", label: "Idea", tone: "", order: 2 },
    succeeded: { icon: "✓", label: "Succeeded", tone: "good", order: 3 },
    failed: { icon: "✕", label: "Failed", tone: "critical", order: 4 },
    abandoned: { icon: "–", label: "Abandoned", tone: "", order: 5 },
  };

  var APPROVAL_STATUS = {
    pending: { icon: "◔", label: "Waiting for you", tone: "warning", order: 0 },
    approved: { icon: "☐", label: "Approved, to do", tone: "accent", order: 1 },
    approved_with_changes: { icon: "☐", label: "Approved with changes, to do", tone: "accent", order: 1 },
    done: { icon: "✓", label: "Done", tone: "good", order: 2 },
    failed: { icon: "✕", label: "Failed", tone: "critical", order: 2 },
    rejected: { icon: "✕", label: "Rejected", tone: "", order: 2 },
    withdrawn: { icon: "–", label: "Withdrawn by the agent", tone: "", order: 2 },
    expired: { icon: "◌", label: "Expired", tone: "", order: 2 },
  };

  var UPGRADE_STATUS = {
    "new": { icon: "◔", label: "New", tone: "warning", order: 0 },
    accepted: { icon: "☐", label: "Accepted, to release", tone: "accent", order: 1 },
    released: { icon: "✓", label: "Released", tone: "good", order: 2 },
    declined: { icon: "✕", label: "Declined", tone: "", order: 2 },
    withdrawn: { icon: "–", label: "Withdrawn by the agent", tone: "", order: 2 },
  };

  // sign: how an entry's amount_usd moves the balance.
  var LEDGER_TYPES = {
    owner_grant: { icon: "▲", label: "Owner grant", sign: 1 },
    revenue: { icon: "↑", label: "Revenue", sign: 1 },
    expense: { icon: "↓", label: "Expense", sign: -1 },
    api_cost: { icon: "↓", label: "API cost", sign: -1 },
    api_cost_correction: { icon: "±", label: "API cost correction", sign: -1 },
    adjustment: { icon: "±", label: "Other correction", sign: 1 },
  };

  var CYCLE_STATUS = {
    running: { icon: "●", label: "Running", tone: "accent" },
    completed: { icon: "✓", label: "Completed", tone: "good" },
    idle: { icon: "◌", label: "Idle", tone: "" },
    refused: { icon: "⊘", label: "Refused", tone: "warning" },
    failed: { icon: "✕", label: "Failed", tone: "critical" },
    stopped: { icon: "■", label: "Stopped", tone: "" },
    interrupted: { icon: "↯", label: "Interrupted", tone: "warning" },
  };

  var CALL_STATUS = {
    ok: { icon: "✓", label: "OK", tone: "good" },
    pending: { icon: "◔", label: "In progress", tone: "accent" },
    refused: { icon: "⊘", label: "Refused", tone: "warning" },
    failed: { icon: "✕", label: "Failed", tone: "critical" },
    interrupted: { icon: "↯", label: "Interrupted", tone: "warning" },
  };

  var TOOL_STATUS = {
    ok: { icon: "✓", label: "OK", tone: "good" },
    started: { icon: "◔", label: "Running", tone: "accent" },
    error: { icon: "✕", label: "Error", tone: "critical" },
    skipped: { icon: "–", label: "Skipped", tone: "" },
    interrupted: { icon: "↯", label: "Interrupted", tone: "warning" },
  };

  var TRIGGERS = { schedule: "scheduled", owner: "woken by you", last_will: "last will" };
  var PHASES = { review: "Daily review", study: "Studying the library", plan: "Plan", act: "Act", reflect: "Reflect", last_will: "Last will" };
  var PURPOSES = { review: "Daily review", plan: "Plan", work: "Work", reflect: "Reflect", research: "Research",
    workshop: "Workshop", draft: "Draft", brainstorm: "Brainstorm", study: "Library study", last_will: "Last will",
    consolidate: "Lessons consolidated", research_check: "Research model check" };

  function triggerText(trigger) { return TRIGGERS[trigger] || (trigger ? String(trigger).replace(/_/g, " ") : "–"); }
  function purposeText(purpose) { return PURPOSES[purpose] || (purpose ? sentence(String(purpose).replace(/_/g, " ")) : "Model call"); }

  function statusOrder(vocab, key) { var s = vocab[key]; return s ? s.order : 9; }

  function chip(vocab, key, fallbackLabel) {
    var s = vocab[key] || { icon: "", label: fallbackLabel || key, tone: "" };
    return h("span", { class: "chip", "data-tone": s.tone || null },
      h("span", { "aria-hidden": "true", text: s.icon }), s.label);
  }

  function plainChip(text) { return h("span", { class: "chip", text: text }); }

  // Rows the agent made in a dry run: nothing real happened.
  function testTag() {
    return h("span", { class: "chip", "data-tone": "test", title: "Made in a dry run: nothing real happened" }, "test");
  }

  // The owner's actions are live since phase 4. A server that still lists them as coming later
  // (coming_in_phase.owner_actions) gets them shown but disabled, with this title.
  function laterTitle() {
    var coming = ui.data && isObject(ui.data.coming_in_phase) ? ui.data.coming_in_phase : {};
    var phase = num(coming.owner_actions);
    return phase > 0 ? "Arrives in phase " + phase : null;
  }

  function laterButton(label) {
    return h("button", { type: "button", class: "btn", disabled: true, title: laterTitle(), text: label });
  }

  // Scrollable, height-capped text. Focusable so keyboard users can scroll it.
  function capped(text, mono) {
    return h("pre", { class: "capped" + (mono ? " mono" : ""), tabindex: "0", text: text });
  }

  function emptyState(tag, title, text) {
    return h(tag, { class: "empty-state" }, h("p", { class: "empty-title", text: title }), text ? h("p", { class: "muted", text: text }) : null);
  }

  // ------------------------------------------------------------------ requests

  function RequestError(kind, message, status) {
    this.kind = kind;          // timeout | network | http | malformed
    this.message = message;
    this.status = status || 0;
  }

  // options: {accept: "text/plain", timeout: ms}
  function request(method, url, body, options) {
    options = options || {};
    var timeout = options.timeout || REQUEST_TIMEOUT_MS;
    var controller = typeof AbortController === "function" ? new AbortController() : null;
    var timedOut = false;
    var timer = window.setTimeout(function () { timedOut = true; if (controller) controller.abort(); }, timeout);
    var opts = { method: method, headers: { Accept: options.accept || "application/json" }, cache: "no-store", credentials: "same-origin" };
    if (controller) opts.signal = controller.signal;
    if (method !== "GET") {
      opts.headers["Content-Type"] = "application/json";
      opts.headers["X-Ember-Request"] = "1";
      opts.body = JSON.stringify(body || {});
    }
    // The timeout also covers reading the body: a server can send headers and then stall.
    return fetch(url, opts).then(function (response) {
      return response.text().then(function (text) {
        var data;
        try { data = text ? JSON.parse(text) : null; } catch (e) { data = undefined; }
        return { status: response.status, ok: response.ok, data: data, text: text };
      });
    }).then(function (res) {
      window.clearTimeout(timer);
      return res;
    }, function (err) {
      window.clearTimeout(timer);
      if (timedOut) throw new RequestError("timeout", "no answer within " + timeout / 1000 + " seconds");
      throw new RequestError("network", "no connection" + (err && err.message ? ": " + err.message : ""));
    });
  }

  function httpError(res) {
    var detail = "";
    if (isObject(res.data) && typeof res.data.error === "string") detail = res.data.error;
    else if (res.text && res.text.length <= 200 && res.text.indexOf("<") < 0) detail = res.text.trim();
    return new RequestError("http", "HTTP " + res.status + (detail ? ": " + detail : ""), res.status);
  }

  // ------------------------------------------------------------------ polling

  function schedule(ms) {
    window.clearTimeout(ui.timer);
    ui.timer = null;
    if (document.visibilityState === "hidden") return;
    ui.timer = window.setTimeout(refresh, ms);
  }

  function refresh() {
    window.clearTimeout(ui.timer);
    var seq = ++ui.seq;
    var next = POLL_ERROR_MS;
    return request("GET", "api/dashboard").then(function (res) {
      if (seq !== ui.seq) return;
      if (!res.ok) throw httpError(res);
      var d = res.data;
      // agent, economy, ledger and memorial are null while the economy is unavailable (the system part says why).
      if (!isObject(d) || !isObject(d.system) || !(isObject(d.agent) || d.agent === null)) {
        throw new RequestError("malformed", res.data === undefined ? "not JSON" : "agent or system data missing");
      }
      if (followServerBuild(d)) { next = null; return; }
      ui.data = d;
      ui.fetchError = null;
      document.body.removeAttribute("data-stale");
      render();
      var running = !!(d.agent && d.agent.cycle_running) || !!(isObject(d.now) && d.now.running);
      if (running) ui.fastPollUntil = 0;  // the woken cycle has started; from here on its running state decides
      next = running || Date.now() < ui.fastPollUntil ? POLL_RUNNING_MS : POLL_IDLE_MS;
    }).catch(function (err) {
      if (seq !== ui.seq) return;
      if (!(err instanceof RequestError)) {
        console.error(err);
        err = new RequestError("bug", errorText(err));
      }
      ui.fetchError = err;
      document.body.setAttribute("data-stale", "true");
      safely("banners", renderBanners);
      // The server answered, so the system log (which says what went wrong) is probably readable.
      if (err.kind === "http" || err.kind === "malformed") loadEvents(seq);
    }).then(function () {
      if (seq === ui.seq && next !== null) schedule(next);
    });
  }

  function loadEvents(seq) {
    request("GET", "api/events?limit=50").then(function (res) {
      if (seq !== ui.seq || !res.ok || !Array.isArray(res.data)) return;
      section("events", [res.data], ["events"], function () { renderEvents(res.data); });
    }).catch(function () { /* the banner already says the server is in trouble */ });
  }

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") refresh();
    else window.clearTimeout(ui.timer);
  });

  // ------------------------------------------------------------------ rendering

  var SECTION_LABELS = {
    banners: "Banners", system: "System status", transitions: "Life-state history", events: "System log",
    header: "Header", controls: "Controls", kpis: "Key numbers", badges: "Tab badges", memorial: "Memorial",
    now: "Now", lives: "Previous lives", charts: "Charts", table: "Table", ledger: "Ledger", forms: "Forms",
    projects: "Projects", activity: "Activity", approvals: "Approvals", inbox: "Inbox", instructions: "Standing instructions",
    upgrades: "Upgrades", mind: "Mind", ventures: "Ventures", roadmap: "Roadmap", library: "Library",
    cycleDetail: "Cycle details", diagnostics: "Diagnostics", email: "Email", workspace: "Workspace", workspaceFile: "Workspace file",
  };

  // True while the user has keyboard focus or selected text inside the element: rebuilding it would take them away.
  function isBusy(el) {
    if (!el) return false;
    var active = document.activeElement;
    if (active && active !== document.body && el.contains(active)) return true;
    return hasSelectionIn(el);
  }

  function hasSelectionIn(el) {
    var sel = window.getSelection ? window.getSelection() : null;
    if (!el || !sel || !sel.rangeCount || sel.isCollapsed) return false;
    var range = sel.getRangeAt(0);
    return el.contains(range.commonAncestorContainer) || range.intersectsNode(el);
  }

  function safely(name, fn) {
    try {
      fn();
      delete ui.sectionErrors[name];
      return true;
    } catch (err) {
      ui.sectionErrors[name] = errorText(err);
      console.error(err);
      return false;
    }
  }

  // Renders one section unless its data is unchanged or the user is busy inside it.
  // fn may return false when it left a part out (the user was busy there): the section is rendered again next time.
  function section(name, slice, guardIds, fn) {
    var key;
    try { key = JSON.stringify(slice); } catch (e) { key = null; }
    if (key !== null && ui.rendered[name] === key) return;
    if (guardIds && guardIds.some(function (id) { return isBusy($(id)); })) return;
    var complete = true;
    var ok = safely(name, function () { complete = fn() !== false; });
    ui.rendered[name] = ok && complete ? key : null;
  }

  function render() {
    var d = ui.data;
    if (!d) return;
    var agent = d.agent || standInAgent(d);
    var economy = isObject(d.economy) ? d.economy : {};
    var coming = isObject(d.coming_in_phase) ? d.coming_in_phase : {};
    // Sections showing relative times are refreshed once a minute even when the data is unchanged.
    var minute = Math.floor(Date.now() / 60000);

    safely("banners", renderBanners);
    section("system", [d.system, d.mode], ["system-facts", "options"], function () { renderSystem(d); });
    section("email", [d.integrations, minute], ["email-facts", "email-never"], function () { renderEmail(d); });
    section("etsy", [d.integrations, d.mode, minute], ["etsy-facts", "etsy-listings", "etsy-orders"], function () { renderEtsy(d); });
    section("transitions", [d.transitions], ["transitions"], function () { renderTransitions(arr(d.transitions)); });
    section("events", [d.events], ["events"], function () { renderEvents(arr(d.events)); });
    section("header", [agent, d.system.version, d.mode, arr(d.lives).length, economy.simulated_note], null, function () { renderHeader(d, agent); });
    safely("controls", function () { renderControls(agent); });
    section("kpis", [d.agent, d.economy, d.mode, d.now && d.now.started_at, minute], ["kpis"], function () { renderKpis(d, agent); });
    safely("badges", function () { renderBadges(d); });

    var dead = agent.state === "dead" && isObject(d.memorial);
    $("memorial").hidden = !dead;
    $("now-card").hidden = dead;
    if (dead) section("memorial", [d.memorial, agent.revive, agent.name], ["memorial"], function () { renderMemorial(d.memorial, agent); });
    // Updated in place (no focusable parts are rebuilt), so a running cycle stays live even while the owner reads it.
    else section("now", [d.now, agent, minute], null, function () { return renderNow(d.now, agent); });
    section("lives", [d.lives, d.memorial && d.memorial.life_id], ["lives-card"], function () { renderLives(arr(d.lives), d.memorial); });
    section("charts", [economy.days], null, function () { renderCharts(economy); });
    if (Array.isArray(economy.days)) section("table", [economy.days, ui.tableOpen], ["economy-table"], function () { renderTable(economy); });

    section("ledger", [d.ledger, d.mode], ["ledger-list"], function () { renderLedger(d); });
    safely("forms", function () { updateForms(d, agent); });

    section("projects", [d.projects, minute], ["projects"], function () { renderProjects(arr(d.projects)); });
    // Patched item by item, so an open cycle, its loaded details and their scroll positions survive the fast polls.
    section("activity", [d.activity, minute], null, function () { return renderActivity(arr(d.activity)); });
    // Owner queues: patched card by card, so an open decision form keeps what the owner typed.
    section("approvals", [d.approvals, projectTitles(d), coming, agent.name, emailLimits(d), minute], null, function () {
      return renderApprovals(arr(d.approvals), projectTitles(d), emailLimits(d));
    });
    section("instructions", [d.instructions, agent.name, coming, !!agent.unavailable, minute], ["instructions-view"], function () {
      renderInstructions(isObject(d.instructions) ? d.instructions : null, agent);
    });
    section("inbox", [d.inbox, d.badges, agent.name, coming, d.mode, minute], ["inbox"], function () { renderInbox(arr(d.inbox), agent.name, badgeCounts(d).unread, isDryRun(d)); });
    section("upgrades", [d.upgrades, coming, agent.name, minute], null, function () { return renderUpgrades(arr(d.upgrades)); });
    // The tree is loaded apart: again when it changed (a venture, or a cycle that ended), while its tab is open.
    if (ui.tab === "ventures" && !ui.vt.busy && d.ventures_stamp !== undefined && d.ventures_stamp !== ui.vt.stamp) loadVentures();
    if (ui.tab === "roadmap" && !ui.rm.busy && isObject(d.roadmap) && d.roadmap.stamp !== ui.rm.stamp) loadRoadmap();
    if (ui.tab === "library" && !ui.lib.busy && isObject(d.library) && d.library.stamp !== ui.lib.stamp) loadLibrary();
    section("mind", [d.mind, ui.mind, minute], ["mind-body"], function () { renderMind(d.mind); });

    ui.refocus = null;  // only for the render right after the owner's action
    // Again, now that section errors are known (only changed banners reach the DOM).
    safely("banners", renderBanners);
    $("updated").textContent = "Updated " + timeFmt.format(new Date());
  }

  // While the economy is unavailable the page still shows the agent's name and the system part.
  function standInAgent(d) {
    var options = isObject(d.system.options) ? d.system.options : {};
    return { name: options.agent_name || "Ember", state: "unknown", paused: false, killed: false, unavailable: true };
  }

  function renderHeader(d, agent) {
    var name = agent.name || "Ember";
    var st = LIFE_STATES[agent.state] || LIFE_STATES.unknown;
    document.title = name + " · " + st.label;
    $("agent-name").textContent = name;
    var pill = $("state-pill");
    pill.setAttribute("data-state", LIFE_STATES[agent.state] ? agent.state : "unknown");
    pill.querySelector(".state-icon").textContent = st.icon;
    pill.querySelector(".state-label").textContent = st.label;
    var badge = $("mode-badge");
    badge.hidden = !isDryRun(d);
    var note = isDryRun(d) && d.economy && d.economy.simulated_note ? String(d.economy.simulated_note) : "";
    if (note) badge.title = note; else badge.removeAttribute("title");
    $("simulated-note").hidden = !note;
    $("simulated-note").textContent = note;
    var parts = [];
    if (agent.state_reason) parts.push(sentence(agent.state_reason));
    if (typeof agent.age_days === "number") parts.push("Age " + plural(Math.floor(agent.age_days), "day"));
    if (agent.life_id && (agent.life_id > 1 || arr(d.lives).length)) parts.push("Life " + agent.life_id);
    var version = versionLabel(d.system.version);
    if (version) parts.push(version);
    $("identity-meta").textContent = parts.join(" · ") || " ";
    $("footer-version").textContent = "Ember " + version;
  }

  function renderControls(agent) {
    var kill = $("kill-button");
    var later = laterTitle();
    var killed = agent.state === "killed" || !!agent.killed;
    kill.disabled = !!later || killed || !!agent.unavailable;
    if (later) kill.title = later;
    else if (killed) kill.title = "The kill switch is already on";
    else if (agent.unavailable) kill.title = "The economy is not available";
    else kill.removeAttribute("title");
    if (!ui.wakeBusy) {
      var wake = $("wake-button");
      var canWake = agent.can_wake === true && !agent.unavailable;
      wake.disabled = !canWake;
      if (canWake) wake.removeAttribute("title");
      else wake.title = agent.unavailable ? "The economy is not available" : agent.wake_blocked_reason ? String(agent.wake_blocked_reason) : "The agent can't be woken right now";
    }
    if (ui.controlBusy) return;
    var btn = $("pause-button");
    btn.textContent = agent.paused ? "Resume" : "Pause";
    btn.disabled = !!agent.killed || !!agent.unavailable;
    if (agent.killed) btn.title = "The kill switch is on";
    else if (agent.unavailable) btn.title = "The economy is not available";
    else btn.removeAttribute("title");
  }

  // ---- Banners

  function bannerSpecs() {
    var list = [];
    if (ui.buildMismatch) {
      list.push({ kind: "warning", icon: "!", title: "Ember was updated. Reload this page (or open Ember again from the sidebar) to use the new version." });
    }
    var err = ui.fetchError;
    var retry = " Retrying; the numbers below may be out of date.";
    if (err) {
      if (err.kind === "http" && err.status >= 500) {
        list.push({ kind: "error", icon: "✕", title: "Ember had an error (" + err.message + ")." + retry + " The System log has the details." });
      } else if (err.kind === "http") {
        list.push({ kind: "error", icon: "✕", title: "Ember refused the request (" + err.message + "). Reload the page; if that doesn't help, open Ember again from the Home Assistant sidebar." });
      } else if (err.kind === "malformed") {
        list.push({ kind: "error", icon: "✕", title: "Ember sent a response the dashboard can't read (" + err.message + ")." + retry });
      } else if (err.kind === "timeout") {
        list.push({ kind: "warning", icon: "!", title: "Ember did not answer within " + REQUEST_TIMEOUT_MS / 1000 + " seconds." + retry });
      } else if (err.kind === "network") {
        list.push({ kind: "warning", icon: "!", title: "Can't reach Ember right now (" + err.message + ")." + retry });
      } else {
        list.push({ kind: "error", icon: "✕", title: "The dashboard failed to update (" + err.message + "). Please report this." });
      }
    }
    var failed = Object.keys(ui.sectionErrors);
    if (failed.length) {
      list.push({ kind: "error", icon: "✕", title: "Part of the dashboard could not be displayed. Please report this.",
        items: failed.map(function (n) { return (SECTION_LABELS[n] || n) + ": " + ui.sectionErrors[n]; }) });
    }
    var d = ui.data;
    if (!d) return list;
    var sys = isObject(d.system) ? d.system : {};
    var agent = isObject(d.agent) ? d.agent : {};
    var name = agent.name || "Ember";
    if (isObject(sys.database) && sys.database.ok === false) {
      list.push({ kind: "error", icon: "✕", title: "The database could not be opened. The agent will not run until this is fixed.", items: [sys.database.error] });
    }
    if (sys.economy_error) {
      list.push({ kind: "error", icon: "✕", title: "The economy could not be started, so money can't be recorded and the agent does not run. The System log has the details.", items: [String(sys.economy_error)] });
    } else if (!d.agent && !(isObject(sys.database) && sys.database.ok === false)) {
      list.push({ kind: "error", icon: "✕", title: "The economy is not available right now. The System log has the details." });
    }
    if (sys.economy_broken) {
      list.push({ kind: "error", icon: "✕", title: "Spending is stopped after a bookkeeping error. Restart the app; the System log has the details.", items: [String(sys.economy_broken)] });
    }
    if (arr(sys.config_errors).length) {
      list.push({ kind: "error", icon: "✕", title: "Invalid app options. Running in safe mode (built-in defaults, dry run on). Fix them in the app's Configuration tab.", items: sys.config_errors });
    }
    if (arr(sys.price_warnings).length) {
      list.push({ kind: "warning", icon: "!", title: "Some prices in the app options look too low, so costs would be under-counted. Check the price table in the app's Configuration tab.", items: sys.price_warnings });
    }
    // 0.11.2: until owner_user_ids names the owner, every Home Assistant user who can open the panel counts as one.
    var owner = isObject(sys.owner) ? sys.owner : null;
    if (owner && !owner.ids_set && !owner.dev_mode) {
      list.push({ kind: "warning", icon: "!", title: "Everyone who can open " + name + " in Home Assistant counts as its owner: they can grant money, approve emails and use the kill switch. To let only you in, put your Home Assistant user ID in the app's owner_user_ids option (Configuration tab) and restart the app.",
        items: owner.user_id ? ["Your user ID: " + String(owner.user_id)] : [] });
    }
    // economy_broken is also among the agent's warnings; it already has its own banner.
    var warnings = arr(agent.warnings).filter(function (w) { return !(sys.economy_broken && String(w).indexOf(String(sys.economy_broken)) >= 0); });
    if (warnings.length) {
      list.push({ kind: "warning", icon: "!", title: warnings.length === 1 ? String(warnings[0]) : "Please check:", items: warnings.length === 1 ? null : warnings });
    }
    // 0.12.0: estimates scaled up after a call cost more than estimated; they come down again, or the owner resets them.
    var factors = arr(agent.estimate_factors);
    if (factors.length) {
      list.push({ kind: "info", icon: "i", title: "Some cost estimates are scaled up because a call once cost more than estimated. " +
        "Each comes down by 0.05 after 25 calls that didn't need it; reset them if you know why it happened.",
        items: factors.map(function (f) { return sentence(String(f.purpose).replace(/_/g, " ")) + " calls on " + f.model + ": ×" + Number(f.factor).toFixed(2); }),
        action: { label: "Reset estimates", post: "api/economy/estimates/reset" } });
    }
    if (agent.state === "killed" || agent.killed) {
      list.push({ kind: "warning", icon: "■", title: "Stopped with the kill switch. To let " + name + " run again: Settings → Apps → Ember → Configuration → " +
        "change 'Kill switch reset' to any other number → Save → Restart the app." });
    } else if (agent.state === "unfunded") {
      list.push({ kind: "info", icon: "i", title: "Waiting for money: grant funds to start " + name + ".", action: { label: "Grant funds", form: "grant" } });
    }
    // critical stays true while the episode lasts, also when paused.
    if (agent.state === "critical" || (agent.critical && agent.state !== "dead" && agent.state !== "killed")) {
      list.push({ kind: "warning", icon: "▲", title: "Runway is under 2 days. Grant funds or record revenue." +
        (agent.last_will_due ? " " + name + " will write its last will on its next wake." : ""), action: { label: "Grant funds", form: "grant" } });
    }
    return list;
  }

  function buildBanner(spec) {
    var action = null;
    if (spec.action) {
      action = h("button", { type: "button", class: "btn banner-action", text: spec.action.label });
      action.addEventListener("click", function () {
        if (!spec.action.post) { openLedgerForm(spec.action.form); return; }
        var btn = this;
        btn.disabled = true;
        request("POST", spec.action.post, {}).then(function (res) {
          if (!res.ok) throw httpError(res);
        }).catch(function (err) {
          if (!(err instanceof RequestError)) console.error(err);
          btn.disabled = false;
          btn.textContent = spec.action.label + " (failed: " + errorText(err) + ")";
        }).then(refresh);
      });
    }
    return h("div", { class: "banner", "data-kind": spec.kind, role: spec.kind === "error" ? "alert" : null },
      h("span", { class: "banner-icon", "aria-hidden": "true", text: spec.icon }),
      h("div", { class: "banner-body" }, h("strong", { text: spec.title }),
        spec.items && spec.items.length ? h("ul", null, spec.items.map(function (t) { return h("li", { text: String(t) }); })) : null),
      action);
  }

  // The container is aria-live: only touch it when the banners really change, or screen readers re-read them.
  function renderBanners() {
    var specs = bannerSpecs();
    var key = JSON.stringify(specs);
    if (key === ui.bannerKey) return;
    ui.bannerKey = key;
    replace($("banners"), specs.map(buildBanner));
  }

  // ---- Key numbers

  function renderKpis(d, a) {
    var e = isObject(d.economy) ? d.economy : {};
    var totals = isObject(e.totals) ? e.totals : {};
    var dry = isDryRun(d);
    document.querySelector(".tile-hero .tile-label").textContent = dry ? "Test balance" : "Balance";
    $("kpi-balance").textContent = usd(a.balance_usd);
    $("kpi-balance-sub").textContent = (dry && a.real_balance_usd !== undefined ? "Real " + usd(a.real_balance_usd) + " · " : "") +
      usd(totals.grant_usd) + " granted · " + usd(totals.revenue_usd) + " earned";

    var runway = $("kpi-runway");
    var days = num(a.runway_days);
    if (a.unavailable) {
      runway.textContent = "–";
      $("kpi-runway-sub").textContent = "the economy is not available";
    } else if (isNaN(days)) {
      runway.textContent = "–";
      $("kpi-runway-sub").textContent = a.runway_note || "not enough spending to estimate";
    } else {
      // The server caps the runway at a year.
      runway.textContent = days >= 365 ? "365+ days" : plural(days, "day");
      var net = num(a.net_runway_days);
      // 0.12.0: and net of the revenue and expenses of the same days, when there were any
      $("kpi-runway-sub").textContent = (a.runway_note || "at the last 7 days' spending") + (num(a.runway_net_in_usd) ?
        " · net of revenue and expenses: " + (isNaN(net) ? (a.net_runway_note || "–").toLowerCase() : net >= 365 ? "365+ days" : plural(net, "day")) : "") +
        // 0.12.0: the burn mode Ember's code sets from the net runway, when it holds the agent back
        (a.burn_mode && a.burn_mode !== "explore" ? " · burn mode " + a.burn_mode : "");
    }
    runway.setAttribute("data-tone", !isNaN(days) && days < 2 ? "critical" : "");

    $("kpi-today").textContent = usd(a.today_spend_usd);
    setMeter($("kpi-today-meter"), a.today_spend_usd, a.daily_cap_usd);
    var sub = num(a.daily_cap_usd) > 0 ? "of " + usd(a.daily_cap_usd) + " daily cap" : "the daily cap is " + usd(a.daily_cap_usd);
    if (num(a.pending_usd) > 0) sub += " · " + usd(a.pending_usd) + " reserved for a call in progress";
    $("kpi-today-sub").textContent = sub;

    // In dry run the headline is the simulated figure; the live one stays next to it.
    var ratioValue = dry ? e.simulated_self_sufficiency_ratio : e.self_sufficiency_ratio;
    var ratio = num(ratioValue);
    $("kpi-ratio").textContent = pct(ratioValue);
    var ratioSub = isNaN(ratio) ? "no costs recorded yet" : "earned revenue ÷ total cost" + (dry ? ", simulated" : "");
    if (dry && !isNaN(num(e.self_sufficiency_ratio))) ratioSub += " · live " + pct(e.self_sufficiency_ratio);
    $("kpi-ratio-sub").textContent = ratioSub;

    if (a.unavailable) {
      $("kpi-balance-sub").textContent = "the economy is not available";
      $("kpi-today-sub").textContent = "\u00a0";
      $("kpi-ratio-sub").textContent = "\u00a0";
    }

    var wake = $("kpi-wake");
    var wakeSub = $("kpi-wake-sub");
    wake.removeAttribute("data-size");
    if (a.unavailable) { wake.textContent = "–"; wakeSub.textContent = "\u00a0"; }
    else if (a.state === "dead") { wake.textContent = "Never"; wakeSub.textContent = "The agent has died"; }
    else if (a.state === "killed" || a.killed) { wake.textContent = "Never"; wakeSub.textContent = "The kill switch is on"; }
    else if (a.paused) { wake.textContent = "Paused"; wakeSub.textContent = "Resume to schedule the next cycle"; }
    else if (a.state === "unfunded") { wake.textContent = "Waiting"; wakeSub.textContent = "Grant funds to start"; }
    else if (a.cycle_running) {
      var started = a.last_wake_at || (isObject(d.now) ? d.now.started_at : null);
      wake.textContent = "Awake";
      wakeSub.textContent = started ? "Cycle started " + relTime(started) : "A cycle is running";
    } else if (a.next_wake_at && validDate(new Date(a.next_wake_at))) {
      var reason = a.next_wake_reason ? " · " + a.next_wake_reason : "";
      if (new Date(a.next_wake_at).getTime() <= Date.now()) {
        wake.textContent = "Due now";
        wakeSub.textContent = "Was due " + relTime(a.next_wake_at) + reason;
      } else {
        wake.textContent = relTime(a.next_wake_at);
        if (wake.textContent.length > 10) wake.setAttribute("data-size", "text");  // "in 3 h 20 min" fits on one line
        wakeSub.textContent = fmtDateTime(a.next_wake_at) + reason;
      }
    } else {
      wake.textContent = a.cycles_enabled === false ? "Off" : "Not scheduled";
      wake.setAttribute("data-size", "text");
      wakeSub.textContent = a.wake_blocked_reason || a.next_wake_reason || "\u00a0";
    }
  }

  function setMeter(meter, spent, cap) {
    var s = Math.max(0, num(spent) || 0);
    var c = num(cap);
    var ratio = c > 0 ? s / c : (s > 0 ? Infinity : 0);
    var percent = isFinite(ratio) ? Math.round(ratio * 100) : null;
    meter.querySelector(".meter-fill").style.width = (isFinite(ratio) ? Math.min(100, ratio * 100) : 100).toFixed(1) + "%";
    // aria-valuenow must stay within min..max; the text carries the overspend.
    meter.setAttribute("aria-valuenow", String(percent === null ? 100 : Math.min(100, percent)));
    meter.setAttribute("aria-valuetext", usd(s) + " of " + usd(c) +
      (percent === null ? " (over cap)" : " (" + percent + "%" + (ratio > 1 ? ", over cap)" : ")")));
    meter.setAttribute("data-level", ratio >= 1 ? "critical" : ratio >= 0.75 ? "warning" : "normal");
  }

  // Icon + number (never color alone); the label says what the number counts.
  function setBadge(id, n, icon, what) {
    var el = $(id);
    el.hidden = !n;
    replace(el, n ? [h("span", { class: "badge-icon", "aria-hidden": "true", text: icon }), String(n)] : []);
    if (n) {
      el.setAttribute("aria-label", n + " " + what);
      el.title = n + " " + what;
    } else {
      el.removeAttribute("aria-label");
      el.removeAttribute("title");
    }
  }

  // The server's counts cover every row (the lists hold only the latest 30); without them, count the rows.
  function badgeCounts(d) {
    var b = isObject(d.badges) ? d.badges : {};
    var rows = function (list, test) { return arr(list).filter(function (x) { return isObject(x) && test(x); }).length; };
    var pick = function (key, fallback) { var v = num(b[key]); return isNaN(v) ? fallback() : v; };
    return {
      pending: pick("approvals_pending", function () { return rows(d.approvals, function (x) { return x.status === "pending"; }); }),
      todo: pick("approvals_todo", function () { return rows(d.approvals, function (x) { return x.status === "approved" || x.status === "approved_with_changes"; }); }),
      unread: pick("inbox_unread", function () { return rows(d.inbox, isUnread); }),
      upgrades: pick("upgrades_new", function () { return rows(d.upgrades, function (x) { return x.status === "new"; }); }),
      ventures: pick("ventures_proposed", function () { return 0; }),
      overdue: isObject(d.roadmap) && !isNaN(num(d.roadmap.overdue)) ? num(d.roadmap.overdue) : 0,
      proposals: isObject(d.roadmap) && !isNaN(num(d.roadmap.proposals)) ? num(d.roadmap.proposals) : 0,
    };
  }

  function renderBadges(d) {
    var c = badgeCounts(d);
    setBadge("badge-approvals", c.pending, "◔", "waiting for your decision");
    setBadge("badge-approvals-todo", c.todo, "☐", "approved, to carry out");
    setBadge("badge-inbox", c.unread, "●", "unread");
    setBadge("badge-upgrades", c.upgrades, "◔", "new");
    setBadge("badge-ventures", c.ventures, "◔", "business cases waiting for your decision");
    setBadge("badge-roadmap", c.overdue, "▲", "overdue milestones");
    setBadge("badge-roadmap-proposals", c.proposals, "◔", "proposed dates waiting for your decision");
    var sys = d.system;
    setBadge("badge-system", arr(sys.config_errors).length + (isObject(sys.database) && sys.database.ok === false ? 1 : 0) + (sys.economy_broken ? 1 : 0), "!", "need attention");
  }

  function isUnread(m) { return isObject(m) && m.sender === "agent" && !m.read_at; }

  // 0.12.0: the owner's decision wakes the agent (the wake_on_decision option); when it acts on it, as the reply says.
  function decisionWake(res, name) {
    var wake = res && isObject(res.data) && typeof res.data.wake === "string" ? res.data.wake : "";
    if (wake === "now" || wake === "soon") ui.fastPollUntil = Date.now() + WAKE_FAST_POLL_MS;
    return wake === "now" ? name + " is waking up to act on it."
      : wake === "after_cycle" ? name + " acts on it as soon as the cycle it is working on ends."
      : wake === "soon" ? name + " woke up less than a minute ago and wakes again for it in a moment."
      : name + " sees it on its next wake.";
  }

  function byDate(key) {
    return function (x, y) { return new Date(x[key]).getTime() - new Date(y[key]).getTime(); };
  }

  // ---- Overview: memorial, now, previous lives, charts

  function renderMemorial(m, agent) {
    var name = m.name || agent.name || "Ember";
    $("memorial-name").textContent = name;
    $("memorial-dates").textContent = fmtDateTime(m.born_at) + " – " + fmtDateTime(m.died_at) +
      (m.simulated || m.mode === "dry_run" ? " · dry run (test money)" : "");
    $("memorial-reason").textContent = m.reason ? "Cause of death: " + m.reason : "";
    replace($("memorial-facts"), [
      h("dt", { text: "Lived" }), h("dd", { text: plural(m.lifespan_days, "day") + ", " + plural(m.cycles, "wake cycle") }),
      h("dt", { text: "Spent" }), h("dd", { text: usd(m.total_cost_usd) }),
      h("dt", { text: "Earned" }), h("dd", { text: usd(m.total_revenue_usd) }),
    ]);
    var will = $("memorial-will");
    var hasWill = typeof m.last_will === "string" && m.last_will.trim() !== "";
    will.textContent = hasWill ? m.last_will : "No last will was written.";
    will.setAttribute("data-empty", hasWill ? "false" : "true");
    var willNote = $("memorial-will-note");
    willNote.hidden = !(hasWill && m.last_will_cut_off);
    willNote.textContent = hasWill && m.last_will_cut_off ? "The will was cut off before " + name + " could finish it." : "";
    var revive = isObject(agent.revive) ? agent.revive : null;
    // Owner amounts have whole cents, so round the threshold up.
    var needed = revive ? amountText(revive.needed_usd) : null;
    $("memorial-revive").textContent = needed
      ? "Grant at least " + usd(needed) + " to revive " + name + ". The revival is logged."
      : "Only an owner grant can revive " + name + ". The revival is logged.";
    var suggested = $("memorial-suggested");
    var amount = revive ? amountText(revive.suggested_usd) : null;
    suggested.hidden = !amount;
    if (amount) {
      suggested.textContent = "Grant " + usd(amount) + " (suggested)";
      suggested.setAttribute("data-amount", amount);
    }
  }

  // A dollar amount as the owner would type it, rounded up to whole cents.
  function amountText(value) {
    var v = num(value);
    if (isNaN(v) || v <= 0) return null;
    return (Math.ceil(Math.round(v * 1e6) / 1e4) / 100).toFixed(2);
  }

  var PHASE_ORDER = ["plan", "act", "reflect"];

  // One sentence about the next wake: when and why, or why there is none.
  function nextWakeText(agent) {
    if (agent.unavailable) return "The agent doesn't run while the economy is unavailable.";
    if (agent.killed) return "The kill switch is on, so the agent doesn't wake.";
    var reason = agent.next_wake_reason ? " (" + agent.next_wake_reason + ")" : "";
    if (agent.next_wake_at && validDate(new Date(agent.next_wake_at))) {
      if (new Date(agent.next_wake_at).getTime() <= Date.now()) return "Next wake: due now" + reason + ".";
      return "Next wake " + relTime(agent.next_wake_at) + ", " + fmtDateTime(agent.next_wake_at) + reason + ".";
    }
    if (agent.paused) return "No wake is scheduled while the agent is paused.";
    var why = agent.wake_blocked_reason || agent.next_wake_reason;
    if (agent.cycles_enabled === false) return why ? "Wake cycles are off: " + endSentence(lowerFirst(why)) : "Wake cycles are off.";
    return why ? "No wake is scheduled: " + endSentence(lowerFirst(why)) : "No wake is scheduled.";
  }

  function renderNow(now, agent) {
    var hasNow = isObject(now);
    var running = hasNow && now.running === true;
    $("now-live").hidden = !running;
    $("now-body").hidden = !hasNow;
    $("now-empty").hidden = hasNow;
    if (!hasNow) {
      replace($("now-empty"), [
        h("p", { class: "empty-title", text: agent.unavailable ? "The agent is not running." : "No wake cycle has run yet." }),
        h("p", { class: "muted", text: nextWakeText(agent) })]);
      ui.nowPlanKey = null;
      return true;
    }

    // Status line: what is happening (or what happened last).
    var status = [];
    if (running) {
      status.push(h("strong", { text: "Cycle #" + now.cycle_id }), " · " + triggerText(now.trigger) + " · started ", timeEl(now.started_at));
    } else {
      var took = duration(now.started_at, now.ended_at);
      status.push(chip(CYCLE_STATUS, now.status, sentence(now.status || "unknown")), " ",
        h("strong", { text: "Last cycle #" + now.cycle_id }), " · " + triggerText(now.trigger) + " · ",
        now.ended_at ? ["ended ", timeEl(now.ended_at)] : ["started ", timeEl(now.started_at)],
        took && now.ended_at ? " · took " + took : "");
    }
    replace($("now-status"), status);

    var notes = [];
    if (!running && agent.paused) notes.push(h("p", { text: "Paused by you. No model calls are made while paused." }));
    if (now.note) notes.push(h("p", { text: sentence(now.note) }));
    $("now-notes").hidden = !notes.length;
    replace($("now-notes"), notes);

    renderPhases(now, running);

    // Goal and plan
    $("now-plan-head").textContent = running ? "Goal for this cycle" : "Goal of the last cycle";
    var plan = $("now-plan");
    plan.textContent = now.plan ? String(now.plan) : running && (now.phase === "plan" || !now.phase) ? "Planning…" : "No plan was made.";
    plan.setAttribute("data-empty", now.plan ? "false" : "true");
    var complete = true;
    var detail = isObject(now.plan_detail) ? now.plan_detail : null;
    var detailKey = JSON.stringify(detail);
    $("now-plan-detail").hidden = !detail;
    if (detailKey !== ui.nowPlanKey) {
      if (hasSelectionIn($("now-plan-body"))) complete = false;
      else {
        replace($("now-plan-body"), detail ? planView(detail) : []);
        ui.nowPlanKey = detailKey;
      }
    }

    // Money
    $("now-budget-head").textContent = running ? "Cycle budget" : "Spent in the last cycle";
    $("now-budget").textContent = usd(now.spent_usd) + " of " + usd(now.cycle_cap_usd) + " cycle cap";
    setMeter($("now-meter"), now.spent_usd, now.cycle_cap_usd);
    var pending = num(now.pending_usd) > 0 && running;
    $("now-budget-sub").hidden = !pending;
    $("now-budget-sub").textContent = pending ? usd(now.pending_usd) + " reserved for the call in progress" : "";

    // What it is doing, or how acting ended
    var action = $("now-action");
    if (running) {
      $("now-action-wrap").hidden = false;
      $("now-action-head").textContent = "Doing";
      action.textContent = now.current_action ? String(now.current_action) : "Thinking…";
    } else {
      $("now-action-wrap").hidden = !now.act_end_reason;
      $("now-action-head").textContent = "Acting ended";
      action.textContent = now.act_end_reason ? sentence(now.act_end_reason) : "";
    }
    if (running && now.act_end_reason) action.textContent += " (acting ended: " + now.act_end_reason + ")";

    var next = $("now-next");
    next.hidden = running;
    next.textContent = running ? "" : nextWakeText(agent);
    return complete;
  }

  // Plan → Act → Reflect, with the current phase marked (icon + text, never color alone).
  function renderPhases(now, running) {
    var el = $("now-phases");
    if (!running || !now.phase) { el.hidden = true; replace(el, []); return; }
    var list = PHASE_ORDER.indexOf(now.phase) >= 0 ? PHASE_ORDER : [now.phase];
    var current = list.indexOf(now.phase);
    el.hidden = false;
    replace(el, list.map(function (phase, i) {
      var state = i < current ? "done" : i === current ? "current" : "next";
      var label = PHASES[phase] || sentence(String(phase).replace(/_/g, " "));
      if (state === "current" && num(now.max_steps) > 0 && (phase === "act" || num(now.step) > 0)) {
        // The worker's model calls, capped by the option "Tool steps per cycle": not the steps of the plan below.
        label += num(now.step) > 0 ? ", tool step " + count(now.step) + " (at most " + count(now.max_steps) + ")"
          : " (at most " + count(now.max_steps) + " tool steps)";
      }
      return h("li", { "data-state": state, "aria-current": state === "current" ? "step" : null },
        h("span", { class: "phase-icon", "aria-hidden": "true", text: state === "done" ? "✓" : state === "current" ? "●" : "○" }),
        h("span", { text: label }),
        h("span", { class: "visually-hidden", text: state === "done" ? " (done)" : state === "current" ? " (now)" : " (still to come)" }));
    }));
  }

  // {goal?, assessment, steps: [text]} as written by the agent; unknown parts are shown as text too.
  function planView(plan) {
    var parts = [];
    var goal = plan.goal || plan.plan;
    if (goal) parts.push(h("p", { class: "plan-goal pre-line", text: asText(goal) }));
    if (plan.assessment) parts.push(h("h4", { class: "small-head", text: "Assessment" }), h("p", { class: "pre-line", text: asText(plan.assessment) }));
    if (plan.money_path) parts.push(h("h4", { class: "small-head", text: "Path to money" }), h("p", { class: "pre-line", text: asText(plan.money_path) }));
    var steps = arr(plan.steps);
    if (steps.length) {
      parts.push(h("h4", { class: "small-head", text: "Steps" }),
        h("ol", { class: "plan-steps" }, steps.map(function (step) { return h("li", { class: "pre-line", text: asText(step) }); })));
    }
    var rest = Object.keys(plan).filter(function (k) {
      return ["goal", "plan", "assessment", "money_path", "steps"].indexOf(k) < 0 && plan[k] !== null && plan[k] !== undefined && plan[k] !== "";
    });
    if (rest.length) {
      parts.push(h("pre", { class: "capped mono", tabindex: "0", text: rest.map(function (k) { return k + ": " + asText(plan[k]); }).join("\n") }));
    }
    return parts.length ? parts : h("p", { class: "muted", text: "The plan is empty." });
  }

  function renderLives(lives, memorial) {
    // The current death is already on the memorial card.
    var list = lives.filter(function (l) { return !(isObject(memorial) && memorial.life_id !== undefined && l.id === memorial.life_id); });
    $("lives-card").hidden = !list.length;
    replace($("lives"), list.map(function (l) {
      return h("li", null,
        h("span", { class: "when", text: "Life " + l.id }),
        h("span", { class: "msg", text: fmtDate(l.born_at) + " – " + fmtDate(l.died_at) + (l.state ? " · " + stateLabel(l.state) : "") + (l.reason ? " · " + l.reason : "") }));
    }));
  }

  var FLOW_SERIES = [
    { key: "revenue_usd", label: "Revenue", color: "--series-1", stack: "in" },
    { key: "api_cost_usd", label: "API cost", color: "--series-2", stack: "out" },
    { key: "expense_usd", label: "Expenses", color: "--series-3", stack: "out" },
  ];

  function renderCharts(economy) {
    var available = Array.isArray(economy.days);
    $("chart-unavailable").hidden = available;
    document.querySelector(".chart-card .money-actions").hidden = !available;
    $("table-toggle").hidden = !available;
    if (!available) {
      document.querySelector(".chart-part").hidden = true;
      $("economy-table").hidden = true;
      destroyCharts();
      return;
    }
    if (!ui.chartFallback) document.querySelector(".chart-part").hidden = false;
    var days = arr(economy.days);
    // Scriptable options and tooltips read the days from here (they can run while a chart is being built).
    ui.days = days;
    if (typeof window.Chart === "undefined") {
      // Keep the canvases (a later load or theme change must still find them); show the table instead.
      if (!ui.chartFallback) {
        ui.chartFallback = true;
        $("chart-fallback").hidden = false;
        document.querySelector(".chart-part").hidden = true;
        setTableOpen(true);
      }
      return;
    }
    var labels = days.map(function (x) { return dayFmt.format(parseDay(x.date)); });
    if (ui.charts.flow && ui.charts.balance) {
      ui.charts.flow.data.labels = labels;
      ui.charts.flow.data.datasets.forEach(function (ds, i) {
        ds.data = days.map(function (x) { return x[FLOW_SERIES[i].key]; });
      });
      ui.charts.flow.update("none");
      ui.charts.balance.data.labels = labels;
      ui.charts.balance.data.datasets[0].data = days.map(function (x) { return x.balance_usd; });
      ui.charts.balance.update("none");
      return;
    }
    destroyCharts();
    buildCharts(days, labels);
  }

  function chartTheme() {
    return {
      surface: cssVar("--surface"), text: cssVar("--text"), text2: cssVar("--text-2"), muted: cssVar("--muted"),
      grid: cssVar("--grid"), axis: cssVar("--axis"), ring: cssVar("--ring"), balance: cssVar("--balance-line"),
      grant: cssVar("--series-4"),
    };
  }

  function tooltipBase(t) {
    return {
      backgroundColor: t.surface, titleColor: t.text, bodyColor: t.text, footerColor: t.text2,
      borderColor: t.axis, borderWidth: 1, padding: 10, usePointStyle: true, boxPadding: 4,
      titleFont: { weight: "600" }, bodyFont: { size: 13 },
    };
  }

  function axes(t) {
    return {
      x: {
        grid: { display: false }, border: { color: t.axis },
        ticks: { color: t.muted, maxRotation: 0, autoSkip: true, autoSkipPadding: 16, font: { size: 11 } },
      },
      y: {
        beginAtZero: true, grid: { color: t.grid }, border: { display: false },
        ticks: { color: t.muted, font: { size: 11 }, maxTicksLimit: 6, callback: function (v) { return usdTick.format(v); } },
      },
    };
  }

  // Draws sparse direct labels: the latest balance and (on wide charts) each owner grant.
  var directLabels = {
    id: "emberDirectLabels",
    afterDatasetsDraw: function (chart) {
      if (chart.canvas.id !== "balance-chart" || !ui.days) return;
      var meta = chart.getDatasetMeta(0);
      if (!meta || meta.hidden) return;
      var area = chart.chartArea;
      var showGrants = chart.width >= GRANT_LABEL_MIN_WIDTH;
      var step = meta.data.length > 1 ? (area.right - area.left) / (meta.data.length - 1) : 0;
      var ctx = chart.ctx;
      var t = chartTheme();
      ctx.save();
      ctx.font = "12px system-ui, -apple-system, 'Segoe UI', sans-serif";
      ctx.textBaseline = "bottom";
      ui.days.forEach(function (day, i) {
        var point = meta.data[i];
        if (!point) return;
        var parts = [];
        if (showGrants && day.grant_usd > 0) parts.push("+" + usd(day.grant_usd) + " grant");
        if (i === ui.days.length - 1) parts.push(usd(day.balance_usd));
        if (!parts.length) return;
        var text = parts.join(" · ");
        var width = ctx.measureText(text).width;
        // Beside the marker (right of it, or left near the right edge), never on top of it.
        var x = point.x + 10;
        if (x + width > area.right) x = point.x - 10 - width;
        x = Math.max(area.left, x);
        // Above every point (and the segments to its neighbours) under the label, so the line never crosses it.
        var top = point.y;
        meta.data.forEach(function (p) {
          if (p && p.x >= x - step - 2 && p.x <= x + width + step + 2) top = Math.min(top, p.y);
        });
        var y = Math.max(top - 8, area.top + 14);
        ctx.fillStyle = t.surface;
        ctx.fillRect(x - 3, y - 15, width + 6, 17);
        ctx.fillStyle = t.text2;
        ctx.fillText(text, x, y);
      });
      ctx.restore();
    },
  };

  function buildCharts(days, labels) {
    var t = chartTheme();
    var Chart = window.Chart;
    Chart.defaults.font.family = "system-ui, -apple-system, 'Segoe UI', sans-serif";

    var flow = new Chart($("flow-chart"), {
      type: "bar",
      data: {
        labels: labels,
        datasets: FLOW_SERIES.map(function (s) {
          return {
            label: s.label,
            data: days.map(function (x) { return x[s.key]; }),
            stack: s.stack,
            backgroundColor: cssVar(s.color),
            hidden: !!ui.hiddenSeries[s.key],
            maxBarThickness: 14,
            barPercentage: 0.9,
            categoryPercentage: 0.8,
            borderSkipped: "start",
            // Round only the top of each bar; the API cost segment stays square when
            // expenses sit on top of it, and a 2px surface gap separates the two.
            borderRadius: s.key === "api_cost_usd"
              ? function (c) { return hasExpense(c) ? 0 : 4; }
              : 4,
            borderWidth: s.key === "api_cost_usd"
              ? function (c) { return hasExpense(c) ? { top: 2, right: 0, bottom: 0, left: 0 } : 0; }
              : 0,
            borderColor: t.surface,
          };
        }),
      },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        interaction: { mode: "index", intersect: false },
        scales: (function () { var a = axes(t); a.x.stacked = true; a.y.stacked = true; return a; })(),
        plugins: {
          legend: { display: false },
          tooltip: Object.assign(tooltipBase(t), {
            callbacks: {
              title: function (items) { return fmtDay(ui.days[items[0].dataIndex].date); },
              label: function (c) { return usd(c.parsed.y) + "  " + c.dataset.label; },
              labelPointStyle: function () { return { pointStyle: "line", rotation: 0 }; },
              // Key each row with the series color (the bars' border is the surface gap color).
              labelColor: function (c) { return { borderColor: c.dataset.backgroundColor, backgroundColor: c.dataset.backgroundColor, borderWidth: 2 }; },
            },
          }),
        },
      },
    });

    var balance = new Chart($("balance-chart"), {
      type: "line",
      data: {
        labels: labels,
        datasets: [{
          label: "Balance",
          data: days.map(function (x) { return x.balance_usd; }),
          borderColor: t.balance,
          backgroundColor: t.balance,
          borderWidth: 2,
          borderJoinStyle: "round",
          borderCapStyle: "round",
          tension: 0,
          pointRadius: function (c) { return isGrantDay(c) ? 6 : 0; },
          pointHoverRadius: function (c) { return isGrantDay(c) ? 7 : 4; },
          pointHitRadius: 12,
          pointStyle: function (c) { return isGrantDay(c) ? "triangle" : "circle"; },
          pointBackgroundColor: function (c) { return isGrantDay(c) ? t.grant : t.balance; },
          pointBorderColor: t.surface,
          pointBorderWidth: 2,
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        interaction: { mode: "index", intersect: false },
        // Headroom above the highest point so its direct label never sits on the line.
        scales: (function () { var a = axes(t); a.y.grace = "15%"; return a; })(),
        plugins: {
          legend: { display: false },
          tooltip: Object.assign(tooltipBase(t), {
            callbacks: {
              title: function (items) { return fmtDay(ui.days[items[0].dataIndex].date); },
              label: function (c) { return usd(c.parsed.y) + "  Balance"; },
              afterLabel: function (c) {
                var g = ui.days[c.dataIndex].grant_usd;
                return g > 0 ? "+" + usd(g) + "  Owner grant" : "";
              },
              labelPointStyle: function () { return { pointStyle: "line", rotation: 0 }; },
              labelColor: function () { return { borderColor: t.balance, backgroundColor: t.balance, borderWidth: 2 }; },
            },
          }),
        },
      },
      plugins: [directLabels],
    });

    ui.charts.flow = flow;
    ui.charts.balance = balance;
    renderLegends();

    function hasExpense(c) { var d = ui.days && ui.days[c.dataIndex]; return !!d && d.expense_usd > 0; }
    function isGrantDay(c) { var d = ui.days && ui.days[c.dataIndex]; return !!d && d.grant_usd > 0; }
  }

  function renderLegends() {
    replace($("flow-legend"), FLOW_SERIES.map(function (s, i) {
      var pressed = !ui.hiddenSeries[s.key];
      var btn = h("button", { type: "button", "aria-pressed": String(pressed), title: "Show or hide " + s.label },
        swatch(cssVar(s.color), ""), s.label);
      btn.addEventListener("click", function () {
        ui.hiddenSeries[s.key] = !ui.hiddenSeries[s.key];
        ui.charts.flow.setDatasetVisibility(i, !ui.hiddenSeries[s.key]);
        ui.charts.flow.update("none");
        btn.setAttribute("aria-pressed", String(!ui.hiddenSeries[s.key]));
      });
      return h("li", null, btn);
    }));
    replace($("balance-legend"), [
      h("li", null, swatch(cssVar("--balance-line"), "swatch-line"), "Balance"),
      h("li", null, h("span", { class: "swatch swatch-triangle", "aria-hidden": "true" }), "Owner grant"),
    ]);
  }

  function swatch(color, extra) {
    var el = h("span", { class: "swatch " + (extra || ""), "aria-hidden": "true" });
    el.style.background = color;
    return el;
  }

  function destroyCharts() {
    // Also clears a chart left half-built by an earlier error ("Canvas is already in use").
    ["flow-chart", "balance-chart"].forEach(function (id) {
      var canvas = $(id);
      var existing = canvas && window.Chart && window.Chart.getChart(canvas);
      if (existing) existing.destroy();
    });
    ui.charts.flow = ui.charts.balance = null;
  }

  function setTableOpen(open) {
    ui.tableOpen = open;
    var btn = $("table-toggle");
    btn.setAttribute("aria-expanded", String(open));
    btn.textContent = open ? "Hide table" : "Show table";
  }

  function renderTable(economy) {
    var wrap = $("economy-table");
    wrap.hidden = !ui.tableOpen;
    if (!ui.tableOpen) return;
    var cols = [
      ["api_cost_usd", "API cost", usd], ["expense_usd", "Expenses", usd], ["revenue_usd", "Revenue", usd],
      ["grant_usd", "Owner grants", usd], ["adjustment_usd", "Corrections", signedUsd], ["balance_usd", "Balance", usd],
    ];
    var rows = arr(economy.days).slice().reverse().map(function (d) {
      return h("tr", null, h("td", { text: fmtDay(d.date) }),
        cols.map(function (c) { return h("td", { class: "num", text: num(d[c[0]]) === 0 && c[0] === "adjustment_usd" ? "–" : c[2](d[c[0]]) }); }));
    });
    replace(wrap, h("table", null,
      h("caption", { class: "visually-hidden", text: "Daily money flows, newest first" }),
      h("thead", null, h("tr", null, h("th", { scope: "col", text: "Day" }),
        cols.map(function (c) { return h("th", { scope: "col", class: "num", text: c[1] }); }))),
      h("tbody", null, rows)));
  }

  // ---- Ledger

  function correctionRemaining(e) {
    if (!isNaN(num(e.remaining_usd))) return Math.max(0, num(e.remaining_usd));
    return Math.max(0, Math.abs(num(e.amount_usd) || 0) - Math.abs(num(e.corrected_usd) || 0));
  }

  // 0.12.0: the dashboard brings the newest 20 entries; older ones load on request and stay until the page reloads.
  var LEDGER_PAGE = 100;

  function renderLedger(d) {
    var newest = arr(isObject(d.ledger) ? d.ledger.entries : null);
    var seen = {};
    newest.forEach(function (e) { seen[String(e.id)] = true; });
    var older = arr(ui.ledgerOlder).filter(function (e) { return !seen[String(e.id)]; });
    var entries = newest.concat(older);
    var dry = isDryRun(d);
    ui.ledgerById = {};
    if (!isObject(d.ledger)) {
      $("ledger-sub").textContent = "";
      replace($("ledger-list"), emptyState("li", "The ledger can't be read right now.", "The banner above and the System tab say why."));
      return;
    }
    $("ledger-sub").textContent = "Newest first." + (dry ? " Dry run: entries marked “test money” or “simulated” never count in live mode." : "");
    if (!entries.length) {
      replace($("ledger-list"), emptyState("li", "No entries yet.", "Grants, revenue, expenses and API costs will be listed here."));
      return;
    }
    replace($("ledger-list"), entries.map(function (e) {
      ui.ledgerById[String(e.id)] = e;
      var t = LEDGER_TYPES[e.type] || { icon: "•", label: sentence(String(e.type).replace(/_/g, " ")), sign: 1 };
      var effect = num(e.amount_usd) * t.sign;
      var marks = [];
      if (e.corrects_id !== null && e.corrects_id !== undefined) marks.push(plainChip("corrects #" + e.corrects_id));
      if (e.simulated) marks.push(plainChip(e.type === "api_cost" || e.type === "api_cost_correction" ? "simulated" : "test money"));
      var about = [e.source, e.note].filter(function (x) { return x; }).join(" · ");
      var extra = [];
      if (e.orig_currency && e.orig_amount !== null && e.orig_amount !== undefined) {
        extra.push(origAmountText(e));
      }
      if (num(e.corrected_usd) < 0) {
        extra.push("Corrected by " + usdExact.format(Math.abs(num(e.corrected_usd))) + "; " + usdExact.format(correctionRemaining(e)) + " left");
      }
      var meta = ["#" + e.id];
      if (e.occurred_on) meta.push(fmtDay(e.occurred_on));
      var recorded = new Date(e.ts);
      var sameDay = validDate(recorded) && e.occurred_on === recorded.getFullYear() + "-" + String(recorded.getMonth() + 1).padStart(2, "0") + "-" + String(recorded.getDate()).padStart(2, "0");
      meta.push("recorded " + (sameDay ? shortTimeFmt.format(recorded) : fmtDateTime(e.ts)));
      meta.push(e.entered_by ? "by " + e.entered_by : e.created_by === "owner" ? "by you" : e.created_by === "system" ? "by the system" : e.created_by === "etsy" ? "by Ember's code, from Etsy's numbers" : "by " + e.created_by);
      if (e.llm_call_id !== null && e.llm_call_id !== undefined) meta.push("call #" + e.llm_call_id);
      if (e.project_id) meta.push("for project #" + e.project_id);
      else if (e.venture_id) meta.push("for venture #" + e.venture_id);
      var correct = null;
      if (e.can_correct) {
        correct = h("button", { type: "button", class: "btn btn-small", "data-correct": String(e.id),
          "aria-label": "Correct entry #" + e.id + " (" + t.label + " " + signedUsd(effect) + ")", text: "Correct" });
      }
      return h("li", { "data-id": String(e.id) },
        h("div", { class: "l-head" },
          h("span", { class: "l-type" }, h("span", { class: "l-icon", "aria-hidden": "true", text: t.icon }), h("span", { class: "l-label", text: t.label }), marks),
          h("span", { class: "l-amount", text: signedUsd(effect) })),
        about ? h("p", { class: "l-about", text: about }) : null,
        extra.length ? h("p", { class: "l-extra", text: extra.join(" · ") }) : null,
        h("div", { class: "l-foot" }, h("p", { class: "l-meta", text: meta.join(" · ") }), correct));
    }));
    var more = newest.length >= 20 && ui.ledgerEnd !== true;
    if (more) $("ledger-list").appendChild(h("li", { class: "ledger-more" }, olderButton(entries[entries.length - 1].id)));
  }

  function olderButton(lastId) {
    var b = h("button", { type: "button", class: "btn", text: "Show older entries" });
    b.addEventListener("click", function () {
      b.disabled = true;
      b.textContent = "Loading…";
      request("GET", "api/ledger?limit=" + LEDGER_PAGE + "&before=" + encodeURIComponent(String(lastId))).then(function (res) {
        if (!res.ok || !isObject(res.data)) throw httpError(res);
        var got = arr(res.data.entries);
        ui.ledgerOlder = arr(ui.ledgerOlder).concat(got);
        if (got.length < LEDGER_PAGE) ui.ledgerEnd = true;
        if (ui.data) safely("ledger", function () { renderLedger(ui.data); });
      }).catch(function (err) {
        b.disabled = false;
        b.textContent = "Show older entries (failed: " + errorText(err) + ")";
      });
    });
    return b;
  }

  function origAmountText(e) {
    var amount = num(e.orig_amount);
    var text;
    try {
      text = new Intl.NumberFormat(undefined, { style: "currency", currency: String(e.orig_currency), minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(amount);
    } catch (err) {
      text = String(e.orig_amount) + " " + e.orig_currency;
    }
    return "Entered as " + text + (e.fx_rate ? " at " + e.fx_rate + " USD per " + e.orig_currency : "");
  }

  // ---- Projects, activity, approvals, inbox, upgrades, mind
  // Everything here is written by the agent: plain text only, and URLs are never turned into links.

  function projectTitles(d) {
    var titles = {};
    arr(d.projects).forEach(function (p) { if (isObject(p) && p.id !== undefined) titles[String(p.id)] = p.title; });
    return titles;
  }

  function renderProjects(projects) {
    var el = $("projects");
    if (!projects.length) {
      replace(el, emptyState("div", "No projects yet.", "When the agent starts a project, it shows up here with its hypothesis, its next step, and what it cost and earned."));
      return;
    }
    var openIds = {};
    Array.prototype.forEach.call(el.querySelectorAll("details[open]"), function (x) { openIds[x.getAttribute("data-id")] = true; });
    var sorted = projects.slice().sort(function (x, y) {
      return statusOrder(PROJECT_STATUS, x.status) - statusOrder(PROJECT_STATUS, y.status) ||
        (new Date(y.updated_at).getTime() || 0) - (new Date(x.updated_at).getTime() || 0);
    });
    var counts = {};
    var spent = 0;
    var earned = 0;
    sorted.forEach(function (p) {
      counts[p.status] = (counts[p.status] || 0) + 1;
      spent += num(p.spent_usd) || 0;
      earned += num(p.earned_usd) || 0;
    });
    var tally = Object.keys(PROJECT_STATUS).filter(function (k) { return counts[k]; }).map(function (k) {
      return counts[k] + " " + PROJECT_STATUS[k].label.toLowerCase();
    });
    replace(el, [
      h("p", { class: "panel-intro projects-summary", text: plural(sorted.length, "project") + (tally.length ? ": " + tally.join(", ") : "") +
        ". Spent " + usd(spent) + ", earned " + usd(earned) + " in total." }),
      sorted.map(function (p) { return projectCard(p, !!openIds[String(p.id)]); }),
    ]);
  }

  function projectCard(p, notesOpen) {
    var waiting = num(p.pending_approvals) > 0;
    return h("article", { class: "card project", "data-id": String(p.id) },
      h("div", { class: "project-head" }, chip(PROJECT_STATUS, p.status, sentence(p.status || "unknown")),
        waiting ? h("span", { class: "chip", "data-tone": "warning" }, h("span", { "aria-hidden": "true", text: "◔" }),
          plural(p.pending_approvals, "approval") + " waiting") : null),
      h("h3", { text: p.title || "Untitled project" }),
      p.hypothesis ? h("p", { class: "hypothesis", text: p.hypothesis }) : null,
      h("dl", { class: "money" },
        h("div", null, h("dt", { text: "Spent" }), h("dd", { text: usd(p.spent_usd) })),
        h("div", null, h("dt", { text: "Earned" }), h("dd", { text: usd(p.earned_usd) })),
        h("div", null, h("dt", { text: "Cycles" }), h("dd", { text: count(p.cycles) }))),
      h("p", { class: "next" }, h("strong", { text: "Next step: " }), p.next_step ? String(p.next_step) : "–"),
      p.notes ? h("details", { class: "notes", "data-id": String(p.id), open: notesOpen },
        h("summary", { text: "Notes" }), h("pre", { class: "notes-text", text: asText(p.notes) })) : null,
      h("p", { class: "muted small project-dates" }, "Started ", timeEl(p.created_at), " · updated ", timeEl(p.updated_at)));
  }

  // ---- Activity: one item per cycle, patched in place. Returns false when an item was left out
  // because the owner had text selected in it (it is updated on the next poll).
  function renderActivity(cycles) {
    var el = $("activity");
    $("activity-intro").hidden = !cycles.length;
    if (!cycles.length) {
      ui.cycles = {};
      replace(el, emptyState("li", "No wake cycles yet.", "Each wake cycle shows up here with its steps and what it cost."));
      return true;
    }
    var complete = true;
    var keep = {};
    var minute = Math.floor(Date.now() / 60000);
    var items = cycles.map(function (c) {
      var id = String(c.cycle_id);
      var item = ui.cycles[id] || buildCycleItem(id);
      keep[id] = item;
      item.running = c.status === "running";
      var key = JSON.stringify([c, minute]);
      if (item.key !== key) {
        if (hasSelectionIn(item.summary) || hasSelectionIn(item.steps)) complete = false;
        else {
          replace(item.summary, cycleSummary(c));
          replace(item.steps, cycleSteps(c));
          item.key = key;
          if (item.detail || item.detailError) renderCycleDetail(item);
        }
      }
      return item.li;
    });
    Array.prototype.slice.call(el.children).forEach(function (child) { if (items.indexOf(child) < 0) el.removeChild(child); });
    items.forEach(function (li, i) { if (el.children[i] !== li) el.insertBefore(li, el.children[i] || null); });
    ui.cycles = keep;
    return complete;
  }

  function buildCycleItem(id) {
    var item = { id: id, key: null, detail: null, detailError: null, loading: false, running: false };
    var panelId = "cycle-detail-" + id;
    item.summary = h("summary");
    item.steps = h("div", { class: "cycle-steps" });
    item.toggle = h("button", { type: "button", class: "btn btn-small", "aria-expanded": "false", "aria-controls": panelId,
      "aria-label": "Show details of cycle #" + id, text: "Show details" });
    item.reload = h("button", { type: "button", class: "btn btn-small btn-ghost", hidden: true, "aria-label": "Reload details of cycle #" + id, text: "Reload" });
    item.status = h("span", { class: "detail-status muted small", role: "status" });
    item.body = h("div", { class: "detail-body" });
    item.panel = h("div", { class: "cycle-detail", id: panelId, hidden: true }, item.body);
    item.li = h("li", { "data-id": id },
      h("details", { class: "cycle", "data-id": id }, item.summary,
        h("div", { class: "cycle-body" }, item.steps, h("div", { class: "detail-bar" }, item.toggle, item.reload, item.status), item.panel)));
    item.toggle.addEventListener("click", function () { toggleCycleDetail(item); });
    item.reload.addEventListener("click", function () { loadCycleDetail(item); });
    return item;
  }

  function cycleSummary(c) {
    var took = c.ended_at ? duration(c.started_at, c.ended_at) : "";
    var meta = [triggerText(c.trigger), " · ", timeEl(c.started_at)];
    if (took) meta.push(" · took " + took);
    meta.push(" · " + plural(c.calls, "model call") + ", " + plural(c.tools, "tool call"));
    return [
      h("span", { class: "cycle-title" }, h("strong", { text: "Cycle #" + c.cycle_id }), " ", chip(CYCLE_STATUS, c.status, sentence(c.status || "unknown"))),
      h("span", { class: "cycle-cost", text: usd(c.cost_usd) }),
      h("span", { class: "cycle-meta" }, meta),
      c.summary || c.note ? h("span", { class: "cycle-text" },
        c.summary ? String(c.summary) : null,
        c.note ? h("span", { class: "cycle-note", text: (c.summary ? " · " : "") + sentence(c.note) }) : null) : null,
    ];
  }

  function cycleSteps(c) {
    var steps = arr(c.steps);
    if (!steps.length) return h("p", { class: "muted small", text: c.status === "running" ? "No steps yet." : "No steps were recorded." });
    return h("ol", { class: "steps", "aria-label": "Steps of cycle #" + c.cycle_id }, steps.map(function (s) {
      return s.kind === "tool" ? toolStep(s) : llmStep(s);
    }));
  }

  function tokensText(x) {
    var parts = [count(x.input_tokens) + " in", count(x.output_tokens) + " out"];
    if (num(x.cache_write_tokens) > 0) parts.push(count(x.cache_write_tokens) + " cache write");
    if (num(x.cache_read_tokens) > 0) parts.push(count(x.cache_read_tokens) + " cache read");
    return parts.join(" · ") + " tokens";
  }

  function callProblems(x, cls) {
    return [
      x.guard_reason ? h("span", { class: cls, text: (x.status === "refused" ? "Not sent: " : "Guard: ") + x.guard_reason }) : null,
      x.error ? h("span", { class: cls, text: "Error: " + x.error }) : null,
    ];
  }

  function llmStep(s) {
    var pending = s.status === "pending";
    return h("li", { "data-kind": "llm" },
      h("span", { class: "kind", text: "Model" }),
      h("span", { class: "step-main" },
        h("span", { class: "step-title" }, h("strong", { text: purposeText(s.purpose) }), " ", chip(CALL_STATUS, s.status, sentence(s.status || "unknown"))),
        h("span", { class: "step-text muted", text: [s.model, pending ? null : tokensText(s), s.stop_reason ? "stopped: " + s.stop_reason : null]
          .filter(function (x) { return x; }).join(" · ") }),
        callProblems(s, "step-problem")),
      h("span", { class: "cost" },
        pending ? "…" : usd(s.cost_usd),
        isNaN(num(s.estimate_usd)) ? null : h("span", { class: "est", text: (pending ? "reserved " : "est. ") + usd(s.estimate_usd) })));
  }

  function toolStep(s) {
    return h("li", { "data-kind": "tool" },
      h("span", { class: "kind", text: "Tool" }),
      h("span", { class: "step-main" },
        h("span", { class: "step-title" }, h("strong", { text: s.name || "tool" }), s.origin === "server" ? " · server tool " : " ", chip(TOOL_STATUS, s.status, sentence(s.status || "unknown"))),
        s.summary ? h("span", { class: "step-text", text: String(s.summary) }) : null),
      h("span", { class: "cost" }));
  }

  function toggleCycleDetail(item) {
    var open = item.panel.hidden;
    item.panel.hidden = !open;
    item.toggle.setAttribute("aria-expanded", String(open));
    item.toggle.textContent = open ? "Hide details" : "Show details";
    item.toggle.setAttribute("aria-label", (open ? "Hide" : "Show") + " details of cycle #" + item.id);
    if (open && !item.detail && !item.loading) loadCycleDetail(item);
    else safely("cycleDetail", function () { renderCycleDetail(item); });
  }

  function loadCycleDetail(item) {
    if (item.loading) return;
    item.loading = true;
    safely("cycleDetail", function () { renderCycleDetail(item); });
    request("GET", "api/cycles/" + encodeURIComponent(item.id)).then(function (res) {
      if (res.status === 404) throw new RequestError("http", "this cycle is no longer stored", 404);
      if (!res.ok) throw httpError(res);
      if (!isObject(res.data)) throw new RequestError("malformed", "not JSON");
      item.detail = { data: res.data, loadedAt: new Date() };
      item.detailError = null;
    }).catch(function (err) {
      if (!(err instanceof RequestError)) console.error(err);
      item.detailError = err;
    }).then(function () {
      item.loading = false;
      safely("cycleDetail", function () { renderCycleDetail(item); });
      safely("banners", renderBanners);
    });
  }

  function renderCycleDetail(item) {
    item.reload.hidden = !(item.detail || item.detailError) || item.panel.hidden || item.loading && !item.detail;
    item.status.hidden = item.panel.hidden;
    var status;
    if (item.loading) status = "Loading…";
    else if (item.detailError) status = "Couldn't load the details (" + errorText(item.detailError) + ")." + (item.detailError.status === 404 ? "" : " Try Reload.");
    else if (item.detail) status = "Loaded " + timeFmt.format(item.detail.loadedAt) + (item.running ? ". The cycle is still running; reload for newer steps." : "");
    else status = "";
    // A live region: only touch it when the text changes, or screen readers repeat it on every poll.
    if (item.status.textContent !== status) item.status.textContent = status;
    item.status.setAttribute("data-kind", item.detailError && !item.loading ? "error" : "");
    if (item.detail && !hasSelectionIn(item.body) && item.body.getAttribute("data-loaded") !== String(item.detail.loadedAt.getTime())) {
      replace(item.body, cycleDetailView(item.detail.data));
      item.body.setAttribute("data-loaded", String(item.detail.loadedAt.getTime()));
    } else if (!item.detail && item.loading) {
      replace(item.body, h("p", { class: "muted", text: "Loading the plan, the model's texts and the tool calls…" }));
    } else if (!item.detail) {
      replace(item.body, []);
    }
  }

  // GET api/cycles/{id}: the plan, then each model call with its text and the tool calls it made.
  function cycleDetailView(data) {
    var calls = arr(data.calls);
    var tools = arr(data.tools).slice().sort(function (a, b) { return (num(a.seq) || 0) - (num(b.seq) || 0); });
    var parts = [
      // 0.12.0: the digest Ember's code wrote when the cycle ended: what it did and didn't do
      data.digest ? h("section", { class: "detail-part" }, h("h3", { text: "Digest (by Ember's code)" }),
        h("p", { class: "pre-line", text: asText(data.digest) })) : null,
      h("section", { class: "detail-part" }, h("h3", { text: "Plan" }),
        isObject(data.plan) ? planView(data.plan) : h("p", { class: "muted", text: "No plan was recorded." }))];
    var shown = {};
    calls.forEach(function (call) {
      var own = tools.filter(function (t) { return t.llm_call_id !== null && t.llm_call_id !== undefined && String(t.llm_call_id) === String(call.id); });
      own.forEach(function (t) { shown[String(t.id)] = true; });
      parts.push(callView(call, own));
    });
    var other = tools.filter(function (t) { return !shown[String(t.id)]; });
    if (other.length) parts.push(h("section", { class: "detail-part" }, h("h3", { text: "Other tool calls" }), toolList(other)));
    if (!calls.length && !tools.length) parts.push(h("p", { class: "muted", text: "No model or tool calls were recorded." }));
    return parts;
  }

  function callView(call, tools) {
    var money = call.status === "pending"
      ? "reserved " + usd(call.estimate_usd)
      : usd(call.cost_usd) + (isNaN(num(call.estimate_usd)) ? "" : " (estimated " + usd(call.estimate_usd) + ")");
    var meta = [call.model, tokensText(call), money, call.stop_reason ? "stopped: " + call.stop_reason : null,
      call.request_id ? "request " + call.request_id : null].filter(function (x) { return x; });
    return h("section", { class: "detail-part" },
      h("h3", null, "Model call #" + call.id + " · " + purposeText(call.purpose) + " ", chip(CALL_STATUS, call.status, sentence(call.status || "unknown"))),
      h("p", { class: "detail-meta", text: meta.join(" · ") }),
      h("p", { class: "detail-problems" }, callProblems(call, "step-problem")),
      h("h4", { class: "small-head", text: "Model text" }),
      call.text ? capped(String(call.text), false) : h("p", { class: "muted small", text: "No text." }),
      tools.length ? [h("h4", { class: "small-head", text: plural(tools.length, "tool call") }), toolList(tools)] : null);
  }

  function toolList(tools) {
    return h("ol", { class: "detail-tools" }, tools.map(function (t) {
      var where = [t.seq !== undefined && t.seq !== null ? "#" + t.seq : null, t.origin === "server" ? "server tool" : null,
        t.phase ? (PHASES[t.phase] || t.phase) + " phase" : null].filter(function (x) { return x; }).join(" · ");
      return h("li", null,
        h("p", { class: "detail-tool-head" }, h("strong", { text: t.name || "tool" }), " ", chip(TOOL_STATUS, t.status, sentence(t.status || "unknown")),
          where ? h("span", { class: "muted small", text: " " + where }) : null),
        t.summary ? h("p", { class: "pre-line", text: String(t.summary) }) : null,
        h("h5", { class: "small-head", text: "Input" }),
        t.input !== null && t.input !== undefined && t.input !== "" ? capped(prettyJson(t.input), true) : h("p", { class: "muted small", text: "No input." }),
        h("h5", { class: "small-head", text: "Result" }),
        t.result ? capped(asText(t.result), true) : h("p", { class: "muted small", text: t.status === "started" ? "Still running." : "No result." }));
    }));
  }

  var APPROVAL_TYPES = {
    publish: "Publish", contact: "Contact", create_account: "Create account",
    spend_money: "Spend money", sell: "Sell", other: "Other",
  };

  // The server's rule for owner-entered result links (http or https, no spaces, nothing before an @ in the host).
  var LINK_RE = /^https?:\/\/[^\s@\/]+(\/\S*)?$/;
  var VERSION_RE = /^\d{1,4}\.\d{1,4}\.\d{1,4}$/;

  function linkProblem(value) {
    if (value.length > 2048) return "Keep the link under 2,048 characters.";
    if (!/^https?:\/\//.test(value)) return "Use an http or https link (it starts with http:// or https://).";
    if (/\s/.test(value)) return "Remove the spaces from the link.";
    if (!LINK_RE.test(value)) return "Use a plain link without a user name or password (nothing before an @).";
    return null;
  }

  // An owner-entered link: a real link only after validation, opened in a new tab without a referrer. 0.12.0: a
  // label shows instead of the address (the whole address in its title), for the agent's sources.
  function ownerLink(value, label) {
    var text = String(value);
    var url = null;
    if (!linkProblem(text)) {
      try { url = new URL(text); } catch (e) { url = null; }
    }
    if (!url || !/^https?:$/.test(url.protocol) || url.username || url.password) return h("span", { class: "link-text", text: text });
    var a = document.createElement("a");
    a.setAttribute("href", url.href);
    a.setAttribute("rel", "noopener noreferrer");
    a.setAttribute("target", "_blank");
    a.className = "result-link";
    if (label) a.setAttribute("title", url.href);
    append(a, [label || text, h("span", { class: "visually-hidden", text: " (opens in a new tab)" })]);
    return a;
  }

  // Whether the agent answered one of the owner's messages it has seen: it stays in the agent's plans until then.
  function answeredLine(m, messages) {
    var answer = m.answered_by ? messages.filter(function (x) { return num(x.id) === num(m.answered_by); })[0] : null;
    return h("p", { class: "seen", "data-seen": m.answered_by ? "yes" : "no" },
      h("span", { "aria-hidden": "true", text: m.answered_by ? "✓ " : "◌ " }),
      m.answered_by ? ["Answered by " + agentName(), answer ? [" ", timeEl(answer.created_at)] : null]
        : "Not answered yet: " + agentName() + " keeps it in its plans until it answers");
  }

  function seenLine(seen) {
    return h("p", { class: "seen", "data-seen": seen ? "yes" : "no" },
      h("span", { "aria-hidden": "true", text: seen ? "✓ " : "◌ " }), (seen ? "Seen by " : "Not yet seen by ") + agentName());
  }

  // ---- Owner queues (approvals, upgrades): grouped lists of cards kept across polls.
  // Each card's view is rebuilt when its row changes; its buttons, an open action panel (with what the owner
  // typed) and its status line stay, so a poll never wipes the owner's input.

  function queueRoot(root, groups, empty) {
    if (root.ember) return root.ember;
    var q = { items: {}, groups: {}, empty: h("div", { class: "queue-empty", hidden: true }) };
    append(root, q.empty);
    groups.forEach(function (g) {
      var head = h("h2", { class: "queue-head" });
      var list = h("div", { class: "stack" });
      var sectionEl = h("section", { class: "queue-group", "data-group": g.key, "aria-label": g.label || g.title, hidden: true }, head, list);
      q.groups[g.key] = { spec: g, head: head, list: list, section: sectionEl };
      append(root, sectionEl);
    });
    replace(q.empty, empty);
    root.ember = q;
    return q;
  }

  function renderQueue(root, opts) {
    var q = queueRoot(root, opts.groups, opts.empty);
    var complete = true;
    var keep = {};
    var placed = {};
    opts.groups.forEach(function (g) { placed[g.key] = []; });
    opts.rows.forEach(function (row) {
      var id = String(row.id);
      var it = q.items[id] || newQueueItem(opts.kind, id, opts.panel);
      keep[id] = it;
      var actionKey = opts.actionKey(row) + "|" + (laterTitle() || "");
      if (it.actionKey !== null && it.actionKey !== actionKey) {
        // The row changed under an open panel: its input no longer applies.
        if (it.open && !it.expectChange) setItemStatus(it, "This request changed meanwhile, so the form was closed. Check it again below.", "error");
        closePanel(it, false);
        it.panels = {};
      }
      it.expectChange = false;
      it.row = row;
      if (it.actionKey !== actionKey) {
        replace(it.actions, opts.actions(it, row));
        it.actionKey = actionKey;
        syncPanelButtons(it);
      }
      var key = JSON.stringify([row, opts.viewKey || null, agentName(), Math.floor(Date.now() / 60000)]);
      if (it.key !== key) {
        if (isBusy(it.view)) complete = false;
        else {
          replace(it.view, opts.view(row));
          it.key = key;
        }
      }
      var group = opts.groups.filter(function (g) { return g.match(row.status, row); })[0] || opts.groups[opts.groups.length - 1];
      placed[group.key].push(it.card);
    });
    Object.keys(q.items).forEach(function (id) {
      if (!keep[id] && q.items[id].card.parentNode) q.items[id].card.parentNode.removeChild(q.items[id].card);
    });
    q.items = keep;
    opts.groups.forEach(function (g) {
      var grp = q.groups[g.key];
      var cards = placed[g.key];
      Array.prototype.slice.call(grp.list.children).forEach(function (c) { if (cards.indexOf(c) < 0) grp.list.removeChild(c); });
      cards.forEach(function (c, i) { if (grp.list.children[i] !== c) grp.list.insertBefore(c, grp.list.children[i] || null); });
      grp.section.hidden = !cards.length;
      grp.head.textContent = (typeof g.title === "function" ? g.title() : g.title) + " (" + cards.length + ")";
    });
    q.empty.hidden = opts.rows.length > 0;
    // After the owner's own action the card may have moved to another group, which drops focus: give it back.
    var lost = !document.activeElement || document.activeElement === document.body;
    if (ui.refocus && lost && root.contains(ui.refocus)) ui.refocus.focus();
    return complete;
  }

  function newQueueItem(kind, id, panelFor) {
    var it = { kind: kind, id: id, key: null, actionKey: null, row: null, open: null, panels: {}, panelFor: panelFor, expectChange: false };
    it.view = h("div", { class: "item-view" });
    it.actions = h("div", { class: "item-actions", role: "group", "aria-label": "Actions" });
    it.slot = h("div", { class: "panel-slot", id: kind + "-panel-" + id });
    it.status = h("p", { class: "form-status item-status", role: "status", tabindex: "-1" });
    it.card = h("article", { class: "card " + kind, "data-id": id }, it.view, it.actions, it.slot, it.status);
    return it;
  }

  function setItemStatus(it, text, kind) {
    it.status.textContent = text;
    it.status.setAttribute("data-kind", kind || "");
  }

  function panelButton(it, mode, label, danger) {
    var later = laterTitle();
    if (later) return laterButton(label);
    var b = h("button", { type: "button", class: "btn" + (danger ? " btn-danger" : ""), "data-mode": mode, "aria-expanded": "false", "aria-controls": it.slot.id, text: label });
    b.addEventListener("click", function () { togglePanel(it, mode); });
    return b;
  }

  function syncPanelButtons(it) {
    Array.prototype.forEach.call(it.actions.querySelectorAll("[data-mode]"), function (b) {
      b.setAttribute("aria-expanded", String(b.getAttribute("data-mode") === it.open));
    });
  }

  function togglePanel(it, mode) {
    if (it.open === mode) { closePanel(it, true); return; }
    var panel = it.panels[mode] || (it.panels[mode] = buildPanel(it, it.panelFor(it, mode)));
    replace(it.slot, panel.form);
    it.open = mode;
    syncPanelButtons(it);
    setItemStatus(it, "", "");
    var first = panel.form.querySelector("textarea, input");
    (first || panel.submit).focus();
  }

  function closePanel(it, returnFocus) {
    var mode = it.open;
    replace(it.slot, []);
    it.open = null;
    syncPanelButtons(it);
    if (returnFocus && mode) {
      var b = it.actions.querySelector('[data-mode="' + mode + '"]');
      if (b) b.focus();
    }
  }

  // spec: {title, intro: [nodes], fields: [{name, label, hint, rows, value, max, required, missing, check}], extra: [nodes],
  //        submit, danger, url, body(values) -> JSON, done(res, values) -> status text}
  function buildPanel(it, spec) {
    var base = it.slot.id + "-" + spec.mode;
    var panel = { spec: spec, fields: {}, busy: false };
    var title = h("h3", { id: base + "-title", text: spec.title });
    panel.status = h("p", { class: "form-status", role: "status" });
    panel.submit = h("button", { type: "submit", class: spec.danger ? "btn btn-danger-solid" : "btn btn-primary", text: spec.submit });
    var cancel = h("button", { type: "button", class: "btn btn-ghost", text: spec.cancelLabel || "Cancel" });
    var fields = spec.fields.map(function (f) {
      var id = base + "-" + f.name;
      var control = f.rows
        ? h("textarea", { id: id, rows: String(f.rows), spellcheck: f.mono ? "false" : null, class: f.mono ? "mono" : null })
        : h("input", { type: "text", id: id, autocomplete: "off", spellcheck: "false", inputmode: f.inputmode || null });
      control.value = f.value || "";
      var hint = f.hint ? h("p", { class: "hint", id: id + "-hint", text: f.hint }) : null;
      var counter = f.max ? h("p", { class: "counter", id: id + "-count" }) : null;
      var error = h("p", { class: "field-error", id: id + "-error", hidden: true });
      control.setAttribute("aria-describedby", [hint ? id + "-hint" : null, counter ? id + "-count" : null, id + "-error"].filter(function (x) { return x; }).join(" "));
      if (f.required) control.setAttribute("aria-required", "true");
      var field = { spec: f, control: control, error: error, counter: counter };
      panel.fields[f.name] = field;
      updateCounter(field);
      control.addEventListener("input", function () { clearPanelError(field); updateCounter(field); });
      return h("div", { class: "field" }, h("label", { for: id, text: f.label }), control, hint, counter, error);
    });
    panel.form = h("form", { class: "action-panel" + (spec.danger ? " action-panel-danger" : ""), novalidate: true, "aria-labelledby": base + "-title" },
      title, spec.intro && spec.intro.length ? h("div", { class: "form-intro" }, spec.intro) : null, fields, spec.extra || null,
      h("div", { class: "form-actions" }, panel.submit, cancel), panel.status);
    panel.form.addEventListener("submit", function (ev) { ev.preventDefault(); submitPanel(it, panel); });
    panel.form.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") { ev.preventDefault(); closePanel(it, true); }
      else if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); submitPanel(it, panel); }
    });
    cancel.addEventListener("click", function () { closePanel(it, true); });
    return panel;
  }

  function updateCounter(field) {
    if (!field.counter) return;
    var n = field.control.value.trim().length;
    field.counter.textContent = intFmt.format(n) + " / " + intFmt.format(field.spec.max) + " characters";
    field.counter.setAttribute("data-over", n > field.spec.max ? "true" : "false");
  }

  function showPanelError(field, message) {
    field.error.textContent = message;
    field.error.hidden = false;
    field.control.setAttribute("aria-invalid", "true");
  }

  function clearPanelError(field) {
    field.error.textContent = "";
    field.error.hidden = true;
    field.control.removeAttribute("aria-invalid");
  }

  function setPanelStatus(panel, text, kind) {
    panel.status.textContent = text;
    panel.status.setAttribute("data-kind", kind || "");
  }

  function fieldProblem(f, value) {
    if (!value) return f.required ? f.missing || "Please fill this in." : null;
    if (f.max && value.length > f.max) return "Keep it under " + intFmt.format(f.max) + " characters (it has " + intFmt.format(value.length) + ").";
    return f.check ? f.check(value) : null;
  }

  function submitPanel(it, panel) {
    if (panel.busy) return;
    var spec = panel.spec;
    var values = {};
    var problems = [];
    Object.keys(panel.fields).forEach(function (name) {
      var field = panel.fields[name];
      clearPanelError(field);
      values[name] = field.control.value.trim();
      var msg = fieldProblem(field.spec, values[name]);
      if (msg) problems.push([field, msg]);
    });
    if (problems.length) {
      problems.forEach(function (p) { showPanelError(p[0], p[1]); });
      setPanelStatus(panel, problems.length === 1 ? "Please fix the marked field." : "Please fix the marked fields.", "error");
      problems[0][0].control.focus();
      return;
    }
    panel.busy = true;
    panel.submit.disabled = true;
    var label = panel.submit.textContent;
    panel.submit.textContent = "Saving…";
    setPanelStatus(panel, "Saving…", "");
    request("POST", spec.url, spec.body(values)).then(function (res) {
      if (res.ok) {
        it.expectChange = true;
        it.panels = {};
        closePanel(it, false);
        setItemStatus(it, spec.done(res, values), "ok");
        ui.refocus = it.status;
        it.status.focus();
        refresh();
        return;
      }
      ownerFailure(res, panel.fields, function (text) { setPanelStatus(panel, text, "error"); }, function (text) {
        // Conflict or gone: the form no longer applies; say so on the card and update the list.
        closePanel(it, false);
        it.panels = {};
        setItemStatus(it, text, "error");
        ui.refocus = it.status;
        it.status.focus();
      });
    }).catch(function (err) {
      if (!(err instanceof RequestError)) console.error(err);
      setPanelStatus(panel, (err.kind === "timeout" ? "Ember did not answer in time" : "Couldn't reach Ember") +
        ", so it's not clear whether this was saved. The list updates in a moment; check it before you try again.", "error");
      refresh();
    }).then(function () {
      panel.busy = false;
      panel.submit.disabled = false;
      panel.submit.textContent = label;
    });
  }

  // {error, field} answers: 422 next to the named field, 409/404 close the form (it changed or is gone), 503 unavailable.
  function ownerFailure(res, fields, say, gone) {
    var data = isObject(res.data) ? res.data : {};
    var msg = typeof data.error === "string" && data.error ? endSentence(sentence(data.error)) : "";
    if (res.status === 422 && msg && typeof data.field === "string" && fields[data.field]) {
      showPanelError(fields[data.field], msg);
      say("Please fix the marked field.");
      fields[data.field].control.focus();
      return;
    }
    if ((res.status === 409 || res.status === 404) && gone) {
      var why = data.field === "expected_version"
        ? "This request changed meanwhile (for example in another browser tab), so nothing was saved."
        : msg ? "Nothing was saved: " + lowerFirst(msg) : "Nothing was saved: it changed meanwhile.";
      gone(why + " The list is updated now; check it again.");
      refresh();
      return;
    }
    if (res.status === 503) { say("Ember can't do this right now (" + (msg ? lowerFirst(msg).replace(/\.$/, "") : "unavailable") + "). Nothing was changed."); return; }
    say(msg && res.status === 422 ? msg : "Ember refused this (" + httpError(res).message + "). Nothing was changed.");
  }

  // ---- Approvals

  function isApproved(status) { return status === "approved" || status === "approved_with_changes"; }

  // Requests the agent's code carries out itself after approval ("email") or prepares for the owner ("reddit_link").
  // Without a parsed action they are shown like any other request.
  function executorOf(a) {
    if (!isObject(a.action)) return null;
    return a.executor === "email" || a.executor === "reddit_link" || a.executor === "etsy_listing" || a.executor === "etsy_edit" ? a.executor : null;
  }

  // A new Etsy listing, or a change to a live one: Ember's code makes both after approval.
  function isEtsy(a) { var e = executorOf(a); return e === "etsy_listing" || e === "etsy_edit"; }

  var APPROVAL_GROUPS = [
    { key: "pending", title: "Waiting for your decision", match: function (s) { return s === "pending"; } },
    { key: "todo", title: "Approved, to carry out", match: function (s, a) { return isApproved(s) && !(a && (executorOf(a) === "email" || isEtsy(a))); } },
    { key: "sending", label: "Approved emails", title: function () { return "Approved emails, " + agentName() + " sends them"; },
      match: function (s, a) { return isApproved(s) && !!a && executorOf(a) === "email"; } },
    { key: "listing", label: "Approved listings and changes", title: function () { return "Approved Etsy listings and changes, " + agentName() + " makes them"; },
      match: function (s, a) { return isApproved(s) && !!a && isEtsy(a); } },
    { key: "closed", title: "Closed", match: function () { return true; } },
  ];

  // What the approvals need to know about the email integration (not its fetch times, so polls don't redraw every card).
  function emailLimits(d) {
    var e = isObject(d.integrations) && isObject(d.integrations.email) ? d.integrations.email : null;
    return e ? { daily_limit: e.daily_limit, available: e.available, status: e.status } : null;
  }

  function renderApprovals(items, titles, email) {
    var sorted = items.slice().sort(function (x, y) {
      var o = statusOrder(APPROVAL_STATUS, x.status) - statusOrder(APPROVAL_STATUS, y.status);
      return o || (new Date(y.created_at).getTime() || 0) - (new Date(x.created_at).getTime() || 0) || num(y.id) - num(x.id);
    });
    return renderQueue($("approvals"), {
      kind: "approval", rows: sorted, groups: APPROVAL_GROUPS, viewKey: [titles, email],
      empty: emptyState("div", "No requests.", "Before the agent publishes, contacts someone or spends money, it asks you here."),
      view: function (a) { return approvalView(a, titles, email); },
      actionKey: function (a) { return [a.status, a.version, executorOf(a) || "", executionStatus(a), a.reddit_url || ""].join("|"); },
      actions: approvalActions,
      panel: function (it, mode) { return approvalPanel(it, mode, email); },
    });
  }

  var EMAIL_APPROVED = {
    approved: { icon: "◔", label: "Approved, the agent sends it", tone: "accent" },
    approved_with_changes: { icon: "◔", label: "Approved with changes, the agent sends it", tone: "accent" },
  };

  // Where an approved email is ("" for other requests). Approved without an execution row yet: waiting.
  function executionStatus(a) {
    var executor = executorOf(a);
    if (executor !== "email" && executor !== "etsy_listing" && executor !== "etsy_edit") return "";
    if (isObject(a.execution) && typeof a.execution.status === "string" && a.execution.status) return a.execution.status;
    return isApproved(a.status) ? "waiting" : "";
  }

  var EXECUTION = {
    waiting: { icon: "◔", label: "Waiting to be sent", tone: "accent" },
    waiting_limit: { icon: "◔", label: "Waiting for tomorrow's send limit", tone: "warning" },
    running: { icon: "●", label: "Sending now", tone: "accent" },
    sent: { icon: "✓", label: "Sent", tone: "good" },
    failed: { icon: "✕", label: "Not sent", tone: "critical" },
    unclear: { icon: "!", label: "Unclear whether it was sent", tone: "critical" },
    simulated: { icon: "◌", label: "Dry run: not really sent", tone: "" },
  };

  var LISTING_EXECUTION = {
    waiting: { icon: "◔", label: "Waiting to be listed", tone: "accent" },
    waiting_limit: { icon: "◔", label: "Waiting for tomorrow's listing limit", tone: "warning" },
    running: { icon: "●", label: "Being created at Etsy", tone: "accent" },
    active: { icon: "✓", label: "Live on Etsy", tone: "good" },
    draft: { icon: "!", label: "A draft at Etsy: finish it there", tone: "warning" },
    failed: { icon: "✕", label: "Not listed", tone: "critical" },
    unclear: { icon: "!", label: "Unclear whether it was created", tone: "critical" },
  };

  var CHANGE_EXECUTION = {
    waiting: { icon: "◔", label: "Waiting to be made", tone: "accent" },
    running: { icon: "●", label: "Being made at Etsy", tone: "accent" },
    done: { icon: "✓", label: "Changed at Etsy", tone: "good" },
    partial: { icon: "!", label: "Partly changed: check it at Etsy", tone: "warning" },
    failed: { icon: "✕", label: "Not changed", tone: "critical" },
    unclear: { icon: "!", label: "Unclear whether it was changed", tone: "critical" },
  };

  function limitText(email) {
    var limit = email ? num(email.daily_limit) : NaN;
    return isNaN(limit) ? null : limit;
  }

  function approvalView(a, titles, email) {
    var executor = executorOf(a);
    var action = executor ? a.action : null;
    var name = agentName();
    var todo = isApproved(a.status);
    var project = a.project_id !== null && a.project_id !== undefined
      ? (titles[String(a.project_id)] ? titles[String(a.project_id)] + " (#" + a.project_id + ")" : "#" + a.project_id) : null;
    var payload = asText(a.payload);
    var final = a.final_payload ? asText(a.final_payload) : "";
    var statusChip = executor === "email" && EMAIL_APPROVED[a.status] ? chip(EMAIL_APPROVED, a.status)
      : chip(APPROVAL_STATUS, a.status, sentence(String(a.status || "unknown").replace(/_/g, " ")));
    var content;
    if (executor === "email") content = emailDraft(action, final);
    else if (executor === "reddit_link") content = redditDraft(action);
    else if (executor === "etsy_listing") content = etsyDraft(action, final);
    else if (executor === "etsy_edit") content = etsyChangeDraft(action, payload, final);
    else {
      content = payload ? h("div", { class: "payload-wrap" },
        final ? h("h4", { class: "small-head", text: "The agent's original" }) : null,
        h("p", { class: "payload-note" }, h("span", { "aria-hidden": "true", text: "! " }), "Written by the agent; check it before acting."),
        h("pre", { class: "payload capped", tabindex: "0", text: payload })) : null;
    }
    var note = null;
    if (todo && !executor) note = "You approved this; carry it out, then mark it done or failed.";
    if (todo && executor === "reddit_link") note = "You approved this; post it on Reddit with the button below, then mark it done (with the link to your post) or failed.";
    return [
      h("div", { class: "item-head" },
        h("h3", { text: a.title || "Untitled request" }), plainChip(APPROVAL_TYPES[a.type] || sentence(String(a.type || "other").replace(/_/g, " "))),
        executor ? plainChip(executor === "email" ? "Email" : isEtsy(a) ? "Etsy" : "Reddit") : null,
        statusChip, a.simulated ? testTag() : null),
      a.description ? h("p", { class: "pre-line", text: String(a.description) }) : null,
      executor === "email" && a.first_contact ? h("p", { class: "warn-box" }, h("span", { "aria-hidden": "true", text: "! " }),
        h("strong", { text: "First email to this address." }), " Cold advertising emails are illegal in Germany (§ 7 UWG). Approve only if this person asked to hear from you.") : null,
      note ? h("p", { class: "todo-note" }, h("span", { "aria-hidden": "true", text: "☐ " }), note) : null,
      a.status === "pending" && executor === "email" ? h("p", { class: "send-note", text: sendNote(email, name) }) : null,
      a.status === "pending" && executor === "reddit_link" ? h("p", { class: "send-note", text: "After you approve, you post it yourself: this card then offers a button that opens Reddit with it filled in, and Copy buttons." }) : null,
      a.status === "pending" && executor === "etsy_listing" ? h("p", { class: "send-note", text: "After you approve, " + name + " creates this listing in your Etsy shop itself: a draft, its photos and files, then live. Etsy charges USD 0.20 per listing." }) : null,
      a.status === "pending" && executor === "etsy_edit" ? h("p", { class: "send-note", text: "After you approve, " + name + " makes this change to the live listing itself. Etsy charges nothing for it." }) : null,
      executionView(a, email),
      final ? h("div", { class: "final-wrap" },
        h("h4", { class: "small-head", text: executor === "email" ? "Your version of the body (" + name + " sends this)" : executor === "etsy_listing" ? "Your version (" + name + " lists this)" : executor === "etsy_edit" ? "Your version (" + name + " makes this change)" : "Your version (the agent must use this)" }),
        h("pre", { class: "payload final capped", tabindex: "0", text: final })) : null,
      content,
      h("dl", { class: "item-grid" },
        h("div", null, h("dt", { text: "Expected cost" }), h("dd", { text: asText(a.expected_cost) || "–" })),
        h("div", null, h("dt", { text: "Expected benefit" }), h("dd", { text: asText(a.expected_benefit) || "–" })),
        project ? h("div", null, h("dt", { text: "Project" }), h("dd", { text: project })) : null),
      decisionInfo(a),
      h("p", { class: "muted small" }, "Requested ", timeEl(a.created_at), " · #" + a.id,
        a.status === "pending" && a.expires_at ? [" · expires unanswered ", timeEl(a.expires_at, fmtDateTime(a.expires_at))] : null),
    ];
  }

  function sendNote(email, name) {
    var limit = limitText(email);
    if (limit === 0) return "After you approve, " + name + " sends this email itself, but its daily send limit is 0: raise email_daily_limit in the app's Configuration tab first.";
    var text = "After you approve, " + name + " sends this email itself (" + (limit === null ? "within its daily send limit" : "at most " + limit + " a day") + "). " +
      "It goes out once, as plain text, with a footer saying an AI agent wrote it.";
    if (email && email.available === false) text += " Email is not working right now, so it waits until it is (see System, Email).";
    return text;
  }

  // Everything the agent wrote is text: addresses and URLs are never links.
  function emailDraft(action, final) {
    return h("div", { class: "draft" },
      h("p", { class: "payload-note" }, h("span", { "aria-hidden": "true", text: "! " }), "Written by the agent; check it before acting."),
      h("dl", { class: "draft-grid" },
        h("dt", { text: "To" }), h("dd", { class: "link-text", text: asText(action.to) || "–" }),
        h("dt", { text: "Subject" }), h("dd", { text: asText(action.subject) || "–" }),
        action.in_reply_to ? [h("dt", { text: "In reply to" }), h("dd", { class: "muted small link-text", text: asText(action.in_reply_to) })] : null),
      h("h4", { class: "small-head", text: final ? "The agent's original body" : "Body" }),
      h("pre", { class: "payload capped", tabindex: "0", text: asText(action.body) }));
  }

  function redditDraft(action) {
    var comment = action.kind === "comment";
    return h("div", { class: "draft" },
      h("p", { class: "payload-note" }, h("span", { "aria-hidden": "true", text: "! " }), "Written by the agent; check it before acting."),
      h("dl", { class: "draft-grid" },
        h("dt", { text: "Subreddit" }), h("dd", { text: "r/" + asText(action.subreddit) }),
        h("dt", { text: "Kind" }), h("dd", { text: comment ? "Comment on a thread" : "New post" }),
        comment ? [h("dt", { text: "Thread" }), h("dd", { class: "link-text", text: asText(action.thread_url) || "–" })] : null,
        comment ? null : [h("dt", { text: "Title" }), h("dd", { "data-copy": "title", text: asText(action.title) || "–" })]),
      h("h4", { class: "small-head", text: comment ? "Comment" : "Body" }),
      h("pre", { class: "payload capped", tabindex: "0", "data-copy": "body", text: asText(action.body) }));
  }

  function listingExecutionView(a, st) {
    var ex = isObject(a.execution) ? a.execution : {};
    var name = agentName();
    var detail;
    if (st === "waiting") detail = [name + " creates it by itself shortly; it checks for approved listings every few minutes."];
    else if (st === "waiting_limit") detail = [name + " has created its listings for today, so this one waits for tomorrow's limit."];
    else if (st === "running") detail = ex.started_at ? ["Creating it at Etsy since ", timeEl(ex.started_at), "."] : ["Creating it now."];
    else if (st === "active") detail = ["Live since ", timeEl(ex.finished_at || ex.started_at), ex.url ? [": ", etsyLink(ex.url, ex.url)] : "."];
    else if (st === "draft") detail = [ex.result ? endSentence(sentence(ex.result)) : "It stayed a draft at Etsy: finish it there."];
    else if (st === "failed") detail = [ex.error ? "Not listed: " + endSentence(ex.error) : "Not listed."];
    else if (st === "unclear") detail = ["It is unclear whether Etsy created it" + (ex.error ? " (" + String(ex.error).replace(/\.$/, "") + ")" : "") + ". " + name +
      " won't try again; check your listings and drafts at Etsy."];
    else detail = [ex.error ? String(ex.error) : ""];
    return h("div", { class: "execution", "data-status": st },
      h("p", { class: "execution-head" }, chip(LISTING_EXECUTION, st, sentence(st.replace(/_/g, " ")))),
      h("p", { class: "execution-detail" }, detail));
  }

  // A listing the agent proposed: its words, and its photos and files as they are in the workspace now (Ember
  // uploads them only if they are still exactly the ones proposed).
  function etsyDraft(action, final) {
    var photos = arr(action.photos);
    var files = arr(action.files);
    return h("div", { class: "draft" },
      h("p", { class: "payload-note" }, h("span", { "aria-hidden": "true", text: "! " }), "Written by the agent; check it before acting."),
      photos.length ? h("div", { class: "etsy-photos" }, photos.map(function (p, i) {
        return h("a", { href: wsProductUrl(String(p.path), true), target: "_blank", rel: "noopener", title: String(p.path) },
          h("img", { src: wsProductUrl(String(p.path), true), alt: (i === 0 ? "Main photo: " : "Photo: ") + String(p.path), loading: "lazy" }));
      })) : null,
      h("dl", { class: "draft-grid" },
        h("dt", { text: "Title" }), h("dd", { text: asText(action.title) || "–" }),
        h("dt", { text: "Price" }), h("dd", { text: asText(action.price) + " " + asText(action.currency) }),
        h("dt", { text: "Tags" }), h("dd", { text: arr(action.tags).map(asText).join(", ") || "–" }),
        h("dt", { text: "Category" }), h("dd", { text: asText(action.category) || "–" }),
        h("dt", { text: "Files" }), h("dd", null, files.map(function (f, i) {
          return [i ? ", " : "", h("a", { href: wsProductUrl(String(f.path), false), text: String(f.path) }), " (" + byteSize(num(f.bytes) || 0) + ")"];
        }))),
      h("h4", { class: "small-head", text: final ? "The agent's original description" : "Description" }),
      h("pre", { class: "payload capped", tabindex: "0", text: asText(action.description) }),
      h("p", { class: "muted small", text: "Ember adds this line at the end: \"This digital product was designed with the help of AI and reviewed by the seller before listing.\"" }));
  }

  function changeExecutionView(a, st) {
    var ex = isObject(a.execution) ? a.execution : {};
    var name = agentName();
    var detail;
    if (st === "waiting") detail = [name + " makes it by itself shortly; it checks for approved changes every few minutes."];
    else if (st === "running") detail = ex.started_at ? ["Changing it at Etsy since ", timeEl(ex.started_at), "."] : ["Changing it now."];
    else if (st === "done") detail = ["Changed ", timeEl(ex.finished_at || ex.started_at), ex.url ? [": ", etsyLink(ex.url, ex.url)] : "."];
    else if (ex.result) detail = [endSentence(sentence(ex.result))];
    else detail = [ex.error ? "Not changed: " + endSentence(ex.error) : "Not changed."];
    return h("div", { class: "execution", "data-status": st },
      h("p", { class: "execution-head" }, chip(CHANGE_EXECUTION, st, sentence(st.replace(/_/g, " ")))),
      h("p", { class: "execution-detail" }, detail));
  }

  // A change to a live listing the agent proposed: the new photos (as they are in the workspace now; Ember uploads
  // them only if they are still exactly the ones proposed) and the change as the owner approves it, old values next
  // to the new ones.
  function etsyChangeDraft(action, payload, final) {
    var photos = arr(action.photos);
    var files = arr(action.files);
    return h("div", { class: "draft" },
      h("p", { class: "payload-note" }, h("span", { "aria-hidden": "true", text: "! " }), "Written by the agent; check it before acting."),
      photos.length ? h("div", { class: "etsy-photos" }, photos.map(function (p, i) {
        return h("a", { href: wsProductUrl(String(p.path), true), target: "_blank", rel: "noopener", title: String(p.path) },
          h("img", { src: wsProductUrl(String(p.path), true), alt: (i === 0 ? "New main photo: " : "New photo: ") + String(p.path), loading: "lazy" }));
      })) : null,
      files.length ? h("p", { class: "muted small" }, "New files: ", files.map(function (f, i) {
        return [i ? ", " : "", h("a", { href: wsProductUrl(String(f.path), false), text: String(f.path) }), " (" + byteSize(num(f.bytes) || 0) + ")"];
      })) : null,
      h("h4", { class: "small-head", text: final ? "The agent's original change" : "The change" }),
      h("pre", { class: "payload capped", tabindex: "0", text: payload }));
  }

  function executionView(a, email) {
    var st = executionStatus(a);
    if (!st) return null;
    if (executorOf(a) === "etsy_listing") return listingExecutionView(a, st);
    if (executorOf(a) === "etsy_edit") return changeExecutionView(a, st);
    var ex = isObject(a.execution) ? a.execution : {};
    var name = agentName();
    var limit = limitText(email);
    var detail;
    if (st === "waiting") detail = [name + " sends it by itself shortly; it checks for approved emails every few minutes."];
    else if (st === "waiting_limit") detail = [name + " has sent " + (limit === null ? "its emails" : "its " + plural(limit, "email")) + " for today, so this one waits for tomorrow's send limit."];
    else if (st === "running") detail = ex.started_at ? ["Sending since ", timeEl(ex.started_at), "."] : ["Sending now."];
    else if (st === "sent") detail = ["Sent ", timeEl(ex.finished_at || ex.started_at), ex.result ? ". " + endSentence(sentence(ex.result)) : "."];
    else if (st === "failed") detail = [ex.error ? "Not sent: " + endSentence(ex.error) : "Not sent."];
    else if (st === "unclear") detail = ["It is unclear whether it was sent" + (ex.error ? " (" + String(ex.error).replace(/\.$/, "") + ")" : "") + ". " + name +
      " won't send it again; check the Sent folder at your mail provider."];
    else if (st === "simulated") detail = ["Dry run: not really sent", ex.finished_at ? [" (", timeEl(ex.finished_at), ")"] : null, "."];
    else detail = [ex.error ? String(ex.error) : ""];
    return h("div", { class: "execution", "data-status": st },
      h("p", { class: "execution-head" }, chip(EXECUTION, st, sentence(st.replace(/_/g, " ")))),
      h("p", { class: "execution-detail" }, detail));
  }

  // The prefilled Reddit page from the server: a real link only when it is a plain www.reddit.com https address.
  var REDDIT_RE = /^https:\/\/www\.reddit\.com\//;

  function redditLink(value) {
    var text = typeof value === "string" ? value : "";
    if (!REDDIT_RE.test(text) || /\s/.test(text)) return null;
    var url;
    try { url = new URL(text); } catch (e) { return null; }
    if (!/^https:$/.test(url.protocol) || url.hostname !== "www.reddit.com" || url.port || url.username || url.password) return null;
    var a = document.createElement("a");
    a.className = "btn btn-primary reddit-open";
    a.setAttribute("href", url.href);
    a.setAttribute("rel", "noopener noreferrer");
    a.setAttribute("target", "_blank");
    append(a, ["Open Reddit with this filled in", h("span", { class: "visually-hidden", text: " (opens in a new tab)" })]);
    return a;
  }

  function copyButton(it, what, label) {
    var b = h("button", { type: "button", class: "btn", "data-copy-button": what, text: label });
    b.addEventListener("click", function () {
      var action = it.row && isObject(it.row.action) ? it.row.action : {};
      var text = asText(what === "title" ? action.title : action.body);
      var word = what === "title" ? "title" : action.kind === "comment" ? "comment" : "body";
      copyText(text, it.view.querySelector('[data-copy="' + what + '"]'), function () { setItemStatus(it, "Copied the " + word + ".", "ok"); }, function (selected) {
        setItemStatus(it, selected ? "The browser didn't allow copying. The " + word + " is selected: press Ctrl+C (Cmd+C on a Mac) to copy it."
          : "This browser can't copy for you. Select the " + word + " above and copy it with Ctrl+C (Cmd+C on a Mac).", "error");
      });
    });
    return b;
  }

  function decisionInfo(a) {
    var parts = [];
    if (a.decided_at && (a.status === "withdrawn" || a.status === "expired")) {  // 0.12.0: not the owner's decision
      parts.push(h("p", null, h("strong", { text: a.status === "withdrawn" ? "Withdrawn by " + agentName() : "Expired without a decision" }),
        " · ", timeEl(a.decided_at)));
      if (a.decision_comment) parts.push(h("p", { class: "pre-line" }, h("strong", { text: "Why: " }), String(a.decision_comment)));
    } else if (a.decided_at) {
      var verb = a.status === "rejected" ? "Rejected" : a.final_payload ? "Approved with changes" : "Approved";
      parts.push(h("p", null, h("strong", { text: verb }), " by " + (a.decided_by || "you") + " · ", timeEl(a.decided_at)));
      if (a.decision_comment) parts.push(h("p", { class: "pre-line" }, h("strong", { text: "Comment: " }), String(a.decision_comment)));
    }
    if (a.closed_at) {
      parts.push(h("p", null, h("strong", { text: a.status === "failed" ? "Marked failed" : "Marked done" }),
        a.closed_by ? " by " + String(a.closed_by) : "", " · ", timeEl(a.closed_at)));
      if (a.result_note) parts.push(h("p", { class: "pre-line" }, h("strong", { text: "Result: " }), String(a.result_note)));
      if (a.result_link) parts.push(h("p", { class: "result-line" }, h("strong", { text: "Link: " }), ownerLink(a.result_link)));
    }
    if (!parts.length) return null;
    parts.push(seenLine(!!a.seen_by_agent));
    return h("div", { class: "decision" }, parts);
  }

  function approvalActions(it, a) {
    var executor = executorOf(a);
    if (a.status === "pending") {
      // A Reddit draft is posted by the owner, who can still edit it on Reddit: no separate "with changes".
      if (executor === "reddit_link") return [panelButton(it, "approve", "Approve"), panelButton(it, "reject", "Reject", true)];
      // A change of photos, files or category only has no words to change.
      if (executor === "etsy_edit" && !a.editable) return [panelButton(it, "approve", "Approve"), panelButton(it, "reject", "Reject", true)];
      return [panelButton(it, "approve", "Approve"), panelButton(it, "approve_with_changes", "Approve with changes"), panelButton(it, "reject", "Reject", true)];
    }
    if (isApproved(a.status)) {
      if (executor === "email") {
        var st = executionStatus(a);
        return st === "waiting" || st === "waiting_limit" ? [panelButton(it, "failed", "Cancel sending", true)] : [];
      }
      if (executor === "etsy_listing") {
        var ls = executionStatus(a);
        return ls === "waiting" || ls === "waiting_limit" ? [panelButton(it, "failed", "Cancel listing", true)] : [];
      }
      if (executor === "etsy_edit") return executionStatus(a) === "waiting" ? [panelButton(it, "failed", "Cancel change", true)] : [];
      var tools = [];
      if (executor === "reddit_link") {
        tools.push(redditLink(a.reddit_url) || h("span", { class: "muted small no-link", text: "No Reddit link (it isn't a www.reddit.com address): copy the text instead." }));
        if (a.action.kind !== "comment") tools.push(copyButton(it, "title", "Copy title"));
        tools.push(copyButton(it, "body", a.action.kind === "comment" ? "Copy comment" : "Copy body"));
      }
      return tools.concat([panelButton(it, "done", "Mark done"), panelButton(it, "failed", "Mark failed")]);
    }
    return [];
  }

  var COMMENT_FIELD = { name: "comment", label: "Comment for the agent (optional)", rows: 2, max: 2000 };

  function approvalPanel(it, mode, email) {
    var a = it.row;
    var version = a.version;
    var url = "api/approvals/" + encodeURIComponent(String(a.id)) + "/";
    var name = agentName();
    var executor = executorOf(a);
    if (mode === "approve" || mode === "approve_with_changes" || mode === "reject") {
      // For an email only the body can be changed; the server stores the edited body as final_payload. For a
      // listing: its title, price, tags and description (the server's "editable" text), not its files.
      var original = executor === "email" ? asText(a.action.body) : isEtsy(a) ? asText(a.editable || a.payload) : asText(a.payload);
      var limit = limitText(email);
      var approveIntro = "After approving, carry it out yourself, then mark it done or failed here. " + name + " sees your decision on its next wake.";
      if (executor === "email") {
        approveIntro = name + " then sends this email itself, once, exactly as shown" + (limit === null ? "" : " (at most " + limit + " a day)") +
          ", with a footer saying an AI agent wrote it.";
      } else if (executor === "reddit_link") {
        approveIntro = "After approving, you post it yourself: this card then offers a button that opens Reddit with it filled in, and Copy buttons. You can still edit it on Reddit before you post.";
      } else if (executor === "etsy_listing") {
        approveIntro = name + " then creates this listing in your Etsy shop itself, with the photos and files shown, and publishes it (Etsy charges USD 0.20). You hear the result on this card.";
      } else if (executor === "etsy_edit") {
        approveIntro = name + " then changes the live listing at Etsy itself, exactly as shown (Etsy charges nothing for it). You hear the result on this card.";
      }
      var specs = {
        approve: { title: executor === "email" ? "Approve this email" : "Approve this request", submit: "Approve",
          intro: [h("p", { text: approveIntro })], fields: [COMMENT_FIELD] },
        approve_with_changes: executor === "etsy_listing"
          ? { title: "Approve with your changes to the listing", submit: "Approve with changes",
            intro: [h("p", { text: "Change the title, price, tags or description: " + name + " lists your version. Keep the Title:, Price: and Tags: lines, then an empty line, then the description. The photos, files and category stay as they are." })],
            fields: [{ name: "final_payload", label: "The listing (" + name + " lists your version)", rows: 14, value: original, max: 8000, required: true, missing: "Write the listing " + name + " should create." }, COMMENT_FIELD] }
          : executor === "etsy_edit"
          ? { title: "Approve with your changes to the change", submit: "Approve with changes",
            intro: [h("p", { text: "Change the new words or price: " + name + " uses your version. Keep the head lines (Title:, Price:, Tags:) that are there and, if there is one, the empty line before the description. New photos, files and a new category stay as they are." })],
            fields: [{ name: "final_payload", label: "The change (" + name + " makes your version)", rows: 12, value: original, max: 8000, required: true, missing: "Write the change " + name + " should make." }, COMMENT_FIELD] }
          : executor === "email"
          ? { title: "Approve with your changes to the body", submit: "Approve with changes",
            intro: [h("p", { text: "Edit the body: " + name + " sends your version. The recipient, the subject and the footer stay as they are. If you leave it as it is, this is recorded as a plain approval." })],
            fields: [{ name: "final_payload", label: "Body (" + name + " sends your version)", rows: 10, value: original, max: 8000, required: true, missing: "Write the body " + name + " should send." }, COMMENT_FIELD] }
          : { title: "Approve with your changes", submit: "Approve with changes",
            intro: [h("p", { text: "Edit the text: " + name + " must use your version. If you leave it as it is, this is recorded as a plain approval." })],
            fields: [{ name: "final_payload", label: "Your version", rows: 8, value: original, max: 8000, required: true, missing: "Write the version " + name + " must use." }, COMMENT_FIELD] },
        reject: { title: "Reject this request?", submit: "Reject", danger: true,
          intro: [h("p", { text: "A rejected request can't be approved later. " + name + " sees your decision on its next wake." })],
          fields: [{ name: "comment", label: "Why (optional, " + name + " reads it)", rows: 2, max: 2000 }] },
      };
      var spec = specs[mode];
      spec.mode = mode;
      spec.url = url + "decide";
      spec.body = function (v) {
        var decision = mode;
        var body = { decision: decision, expected_version: version };
        if (mode === "approve_with_changes") {
          // Unchanged text is a plain approval (also when only surrounding blank lines differ).
          if (v.final_payload === original.trim()) body.decision = "approve";
          else body.final_payload = v.final_payload;
        }
        if (v.comment) body.comment = v.comment;
        return body;
      };
      spec.done = function (res, v) {
        var st = isObject(res.data) && isObject(res.data.approval) ? res.data.approval.status : null;
        if (mode === "reject") return "Rejected. " + decisionWake(res, name);
        if (executor === "email") {
          if (mode === "approve_with_changes" && st !== "approved") return "Approved with your changes. " + name + " sends your version itself; this card shows when it's sent.";
          return "Approved. " + name + " sends it itself; this card shows when it's sent.";
        }
        if (executor === "reddit_link") return "Approved. Post it with the button below, then mark it done or failed.";
        if (executor === "etsy_listing") return (mode === "approve_with_changes" && st !== "approved" ? "Approved with your changes. " : "Approved. ") + name + " creates the listing itself; this card shows when it's live.";
        if (executor === "etsy_edit") return (mode === "approve_with_changes" && st !== "approved" ? "Approved with your changes. " : "Approved. ") + name + " changes the listing itself; this card shows when it's done.";
        if (mode === "approve_with_changes" && st === "approved") return "Approved as it was (the text was unchanged). Carry it out, then mark it done or failed.";
        if (mode === "approve_with_changes") return "Approved with your changes. Carry it out with your version, then mark it done or failed.";
        return "Approved. Carry it out, then mark it done or failed.";
      };
      return spec;
    }
    var failed = mode === "failed";
    if (executor === "etsy_listing" && failed) {
      return {
        mode: mode, title: "Cancel this listing?", submit: "Cancel listing", danger: true, cancelLabel: "Keep it",
        intro: [h("p", { text: name + " won't create it. The request is marked failed, and " + name + " sees that on its next wake." })],
        fields: [{ name: "result_note", label: "Why (" + name + " reads it)", rows: 2, max: 2000, required: true, value: "Cancelled before it was listed.", missing: "Say why you cancel it." }],
        url: url + "close",
        body: function (v) { return { outcome: "failed", expected_version: version, result_note: v.result_note }; },
        done: function () { return "Cancelled. " + name + " won't create this listing."; },
      };
    }
    if (executor === "etsy_edit" && failed) {
      return {
        mode: mode, title: "Cancel this change?", submit: "Cancel change", danger: true, cancelLabel: "Keep it",
        intro: [h("p", { text: name + " won't make it. The request is marked failed, and " + name + " sees that on its next wake." })],
        fields: [{ name: "result_note", label: "Why (" + name + " reads it)", rows: 2, max: 2000, required: true, value: "Cancelled before the listing was changed.", missing: "Say why you cancel it." }],
        url: url + "close",
        body: function (v) { return { outcome: "failed", expected_version: version, result_note: v.result_note }; },
        done: function () { return "Cancelled. " + name + " won't change the listing."; },
      };
    }
    if (executor === "email" && failed) {
      // "Cancel sending": closes the waiting email as failed, so the agent's code never sends it.
      return {
        mode: mode, title: "Cancel sending this email?", submit: "Cancel sending", danger: true, cancelLabel: "Keep it",
        intro: [h("p", { text: name + " won't send it. The request is marked failed, and " + name + " sees that on its next wake." })],
        fields: [{ name: "result_note", label: "Why (" + name + " reads it)", rows: 2, max: 2000, required: true, value: "Cancelled before it was sent.", missing: "Say why you cancel it." }],
        url: url + "close",
        body: function (v) { return { outcome: "failed", expected_version: version, result_note: v.result_note }; },
        done: function () { return "Cancelled. " + name + " won't send this email."; },
      };
    }
    // Close: done or failed
    var extra = null;
    if (a.type === "spend_money" || a.type === "sell") {
      var expense = a.type === "spend_money";
      var ledgerButton = h("button", { type: "button", class: "btn btn-small", text: expense ? "Open the expense form" : "Open the revenue form" });
      ledgerButton.addEventListener("click", function () { openLedgerForm(expense ? "expense" : "revenue"); });
      extra = h("div", { class: "ledger-hint" },
        h("p", { text: expense ? "If it cost money, record the expense in the ledger." : "If it earned money, record the revenue in the ledger." }), ledgerButton);
    }
    return {
      mode: mode, title: failed ? "Mark as failed" : "Mark as done", submit: failed ? "Mark failed" : "Mark done",
      intro: [h("p", { text: "Tell " + name + " how it went; it reads this on its next wake." })],
      fields: [
        { name: "result_note", label: failed ? "What went wrong" : "What happened (optional)", rows: 3, max: 2000, required: failed, missing: "Say what went wrong." },
        { name: "result_link", label: executor === "reddit_link" ? "Link to your Reddit post (optional)" : "Link to the result (optional)", inputmode: "url", check: linkProblem,
          hint: name + " will see this link and can read it; don't paste links containing access tokens." },
      ],
      extra: extra,
      url: url + "close",
      body: function (v) {
        var body = { outcome: failed ? "failed" : "done", expected_version: version };
        if (v.result_note) body.result_note = v.result_note;
        if (v.result_link) body.result_link = v.result_link;
        return body;
      },
      done: function (res) { return (failed ? "Marked failed. " : "Marked done. ") + decisionWake(res, name); },
    };
  }

  // ---- Upgrades

  var UPGRADE_GROUPS = [
    { key: "new", title: "Waiting for your decision", match: function (s) { return s === "new"; } },
    { key: "accepted", title: "Accepted, to release", match: function (s) { return s === "accepted"; } },
    { key: "closed", title: "Closed", match: function () { return true; } },
  ];

  function renderUpgrades(items) {
    var sorted = items.slice().sort(function (x, y) {
      return statusOrder(UPGRADE_STATUS, x.status) - statusOrder(UPGRADE_STATUS, y.status) ||
        (new Date(y.created_at).getTime() || 0) - (new Date(x.created_at).getTime() || 0) || num(y.id) - num(x.id);
    });
    return renderQueue($("upgrades"), {
      kind: "upgrade", rows: sorted, groups: UPGRADE_GROUPS,
      empty: emptyState("div", "No upgrade requests.", "The agent suggests changes to its own tools here, for you to accept or decline."),
      view: upgradeView,
      actionKey: function (u) { return String(u.status); },
      actions: function (it, u) {
        if (u.status === "new") return [panelButton(it, "accepted", "Accept"), panelButton(it, "released", "Mark released"), panelButton(it, "declined", "Decline", true)];
        if (u.status === "accepted") return [panelButton(it, "released", "Mark released"), panelButton(it, "declined", "Decline", true)];
        return [];
      },
      panel: upgradePanel,
    });
  }

  function upgradeView(u) {
    var decided = [];
    if (u.decided_at) {
      decided.push(h("p", null, h("strong", { text: (UPGRADE_STATUS[u.status] || { label: sentence(String(u.status)) }).label.replace(/, to release$/, "") }), " · ", timeEl(u.decided_at)));
    }
    if (u.released_version) decided.push(h("p", null, h("strong", { text: "Released in " }), versionLabel(u.released_version)));
    if (u.owner_note) decided.push(h("p", { class: "pre-line" }, h("strong", { text: "Your note: " }), String(u.owner_note)));
    return [
      h("div", { class: "item-head" },
        h("h3", { text: u.title || "Untitled request" }), u.priority ? plainChip("Priority: " + u.priority) : null,
        chip(UPGRADE_STATUS, u.status, sentence(String(u.status || "unknown"))), u.simulated ? testTag() : null),
      h("dl", { class: "item-grid" },
        h("div", null, h("dt", { text: "Problem" }), h("dd", { class: "pre-line", text: asText(u.problem) || "–" })),
        h("div", null, h("dt", { text: "Proposed change" }), h("dd", { class: "pre-line", text: asText(u.proposed_change) || "–" })),
        h("div", null, h("dt", { text: "Expected benefit" }), h("dd", { class: "pre-line", text: asText(u.expected_benefit) || "–" }))),
      decided.length ? h("div", { class: "decision" }, decided) : null,
      upgradeScript(u),
      h("p", { class: "muted small" }, "Requested ", timeEl(u.created_at), " · #" + u.id),
    ];
  }

  // A workshop script that proved itself: downloadable, and copied with the request as a task for Claude Code, which
  // builds it into Ember as one of the agent's own tools.
  function upgradeScript(u) {
    if (!u.script_path) return null;
    var url = "api/upgrades/" + encodeURIComponent(String(u.id)) + "/script";
    var status = h("p", { class: "form-status small", role: "status" });
    var task = h("pre", { class: "upgrade-task", tabindex: "0", hidden: true });
    var copy = h("button", { type: "button", class: "btn", text: "Copy as a task for Claude Code" });
    copy.addEventListener("click", function () {
      copy.disabled = true;
      status.textContent = "Loading the script…";
      request("GET", url, null, { accept: "text/plain" }).then(function (res) {
        if (!res.ok) throw httpError(res);
        if (typeof res.text !== "string") throw new RequestError("malformed", "no text");
        task.textContent = claudeTask(u, res.text);
        task.hidden = false;
        copyText(task.textContent, task, function () {
          status.textContent = "Copied. Paste it into Claude Code, in a session with Ember's repository.";
        }, function (selected) {
          status.textContent = selected ? "The task is selected below: press Ctrl+C (Cmd+C on a Mac) to copy it."
            : "Select the task below and copy it with Ctrl+C (Cmd+C on a Mac).";
        });
      }).catch(function (err) {
        if (!(err instanceof RequestError)) console.error(err);
        status.textContent = "Couldn't load the script (" + errorText(err) + ").";
      }).then(function () { copy.disabled = false; });
    });
    return h("div", { class: "upgrade-script" },
      h("p", null, h("strong", { text: "Workshop script: " }), h("code", { text: u.script_path }),
        " (" + byteSize(num(u.script_bytes) || 0) + ")"),
      h("p", { class: "muted small", text: "Code " + agentName() + " ran in its workshop and wants built in. Give it to " +
        "Claude Code with the request, and it can become one of " + agentName() + "'s own tools." }),
      h("div", { class: "form-actions" }, copy, h("a", { class: "btn", href: url, download: baseName(u.script_path), text: "Download script" })),
      status, task);
  }

  function claudeTask(u, script) {
    return [
      "Build this into Ember (the Home Assistant app in this repository, folder ember/) as one of the agent's own " +
        "tools, so it no longer needs its workshop for it. Follow the repository's conventions and tests.",
      "",
      "Upgrade request #" + u.id + ": " + asText(u.title),
      "Problem: " + asText(u.problem),
      "Proposed change: " + asText(u.proposed_change),
      "Expected benefit: " + asText(u.expected_benefit),
      "",
      "The workshop script that proved itself (" + u.script_path + "), written by a model and run in Anthropic's " +
        "code execution sandbox:",
      "```python",
      script.replace(/\s+$/, ""),
      "```",
    ].join("\n");
  }

  function upgradePanel(it, mode) {
    var name = agentName();
    var note = { name: "note", label: mode === "declined" ? "Why (optional, " + name + " reads it)" : "Note for the agent (optional)", rows: 2, max: 2000 };
    var specs = {
      accepted: { title: "Accept this upgrade request", submit: "Accept",
        intro: [h("p", { text: "Accepting tells " + name + " you plan to make this change. Mark it released once it's in a version of the app." })], fields: [note] },
      released: { title: "Mark as released", submit: "Mark released",
        intro: [h("p", { text: "Say in which version of the app the change is included." })],
        fields: [{ name: "version", label: "Released in version", hint: "Like 0.4.0.", required: true, missing: "Enter the version, like 0.4.0.", inputmode: "decimal",
          check: function (v) { return VERSION_RE.test(v) ? null : "Enter the version as three numbers, like 0.4.0."; } }, note] },
      declined: { title: "Decline this upgrade request?", submit: "Decline", danger: true,
        intro: [h("p", { text: name + " sees your decision on its next wake." })], fields: [note] },
    };
    var spec = specs[mode];
    spec.mode = mode;
    spec.url = "api/upgrades/" + encodeURIComponent(String(it.row.id));
    spec.body = function (v) {
      var body = { status: mode };
      if (v.note) body.note = v.note;
      if (mode === "released") body.version = v.version;
      return body;
    };
    spec.done = function (res, v) {
      if (mode === "released") return "Marked released in " + versionLabel(v.version) + ".";
      return (mode === "accepted" ? "Accepted. " : "Declined. ") + name + " sees this on its next wake.";
    };
    return spec;
  }

  // ---- Standing instructions (top of the Inbox)

  var INSTRUCTIONS_MAX = 1500;
  // Offered while there are none, never saved by itself: the owner saves it (changed or not) with Save.
  var INSTRUCTIONS_SUGGESTION = "Work on your own. Ask me only to approve something that leaves the container, or for money. " +
    "Keep 2-3 experiments going; when one waits for me, work on another. Spend your daily budget on experiments rather " +
    "than sleeping to save it.";

  function currentInstructions() {
    var instr = ui.data && isObject(ui.data.instructions) ? ui.data.instructions : null;
    return instr && typeof instr.text === "string" && instr.text ? instr : null;
  }

  function renderInstructions(instr, agent) {
    var name = agent.name || "Ember";
    var text = instr && typeof instr.text === "string" ? instr.text : "";
    var sub = $("instructions-sub");
    if (text) {
      replace(sub, ["Set by " + (instr.entered_by ? String(instr.entered_by) : "you") + " · ", timeEl(instr.updated_at)]);
      replace($("instructions-view"), h("p", { class: "instructions-text", text: text }));
    } else {
      sub.textContent = "None yet.";
      replace($("instructions-view"), h("div", { class: "instructions-empty" },
        h("p", { class: "muted", text: "A suggestion to start from (nothing is saved until you press Save):" }),
        h("p", { class: "instructions-text instructions-suggestion", text: INSTRUCTIONS_SUGGESTION })));
    }
    $("instructions-note").textContent = name + " reads these in every plan. Use them for lasting guidance; use messages for one-off things.";
    $("instructions-label").textContent = "Standing instructions for " + name;
    syncInstructionsEdit(agent);
  }

  function syncInstructionsEdit(agent) {
    var edit = $("instructions-edit");
    var later = laterTitle();
    var editing = ui.instructions.editing;
    edit.hidden = editing;
    edit.setAttribute("aria-expanded", editing ? "true" : "false");
    edit.textContent = currentInstructions() ? "Edit" : "Write instructions";
    edit.disabled = !!later || !!(agent && agent.unavailable);
    if (later) edit.title = later;
    else if (agent && agent.unavailable) edit.title = "The agent is not available";
    else edit.removeAttribute("title");
    $("instructions-view").hidden = editing;
  }

  function instructionsCount() {
    var n = $("instructions-text").value.trim().length;
    var el = $("instructions-count");
    el.textContent = intFmt.format(n) + " / 1,500 characters";
    el.setAttribute("data-over", n > INSTRUCTIONS_MAX ? "true" : "false");
  }

  function instructionsError(text) {
    var err = $("instructions-error");
    err.textContent = text || "";
    err.hidden = !text;
    if (text) $("instructions-text").setAttribute("aria-invalid", "true");
    else $("instructions-text").removeAttribute("aria-invalid");
  }

  function setInstructionsStatus(text, kind) {
    $("instructions-status").textContent = text;
    $("instructions-status").setAttribute("data-kind", kind || "");
  }

  function openInstructions() {
    if (ui.instructions.editing || laterTitle()) return;
    var current = currentInstructions();
    ui.instructions.editing = true;
    $("instructions-form").hidden = false;
    var box = $("instructions-text");
    box.value = current ? current.text : INSTRUCTIONS_SUGGESTION;
    box.placeholder = INSTRUCTIONS_SUGGESTION;
    instructionsError("");
    instructionsCount();
    setInstructionsStatus(current ? "" : "Filled in with the suggestion: change it as you like. Nothing is saved until you press Save.", "");
    syncInstructionsEdit(ui.data ? ui.data.agent || standInAgent(ui.data) : null);
    box.focus();
  }

  function closeInstructions() {
    ui.instructions.editing = false;
    $("instructions-form").hidden = true;
    instructionsError("");
    ui.rendered.instructions = null;  // show what changed while the editor was open
    if (ui.data) {
      var agent = ui.data.agent || standInAgent(ui.data);
      safely("instructions", function () { renderInstructions(currentInstructions(), agent); });
    }
    $("instructions-edit").focus();
  }

  function saveInstructions() {
    if (ui.instructions.saving || !ui.instructions.editing || laterTitle()) return;
    var box = $("instructions-text");
    var text = box.value.trim();
    instructionsError("");
    if (text.length > INSTRUCTIONS_MAX) {
      instructionsError("Keep the instructions under 1,500 characters (they have " + intFmt.format(text.length) + ").");
      box.focus();
      return;
    }
    // Unlike a message's text, a saved version can't be removed later, and every plan sends it to Anthropic.
    if (SECRET_HINT.test(text) && ui.instructions.secretWarned !== text) {
      ui.instructions.secretWarned = text;
      instructionsError("This looks like it contains a password. " + agentName() + " can't log in anywhere, and every " +
        "saved version of the instructions is kept for good and sent to Anthropic with every plan. Remove it, or press " +
        "Save again to save it anyway.");
      box.focus();
      return;
    }
    ui.instructions.saving = true;
    var save = $("instructions-save");
    save.disabled = true;
    save.textContent = "Saving…";
    setInstructionsStatus("Saving…", "");
    request("POST", "api/instructions", { text: text }).then(function (res) {
      var data = isObject(res.data) ? res.data : {};
      if (res.ok) {
        if (ui.data) ui.data.instructions = isObject(data.instructions) ? data.instructions : null;
        closeInstructions();
        setInstructionsStatus(data.changed === false ? "Nothing changed: these are already the instructions."
          : text ? "Saved. " + agentName() + " follows them from its next plan on."
          : "Cleared. " + agentName() + " has no standing instructions now.", "ok");
        refresh();
        return;
      }
      if (res.status === 422 && data.field === "text" && typeof data.error === "string" && data.error) {
        instructionsError(endSentence(sentence(data.error)));
        setInstructionsStatus("Nothing was saved.", "error");
        box.focus();
        return;
      }
      ownerFailure(res, {}, function (msg) { setInstructionsStatus(msg, "error"); }, null);
    }).catch(function (err) {
      if (!(err instanceof RequestError)) console.error(err);
      setInstructionsStatus((err.kind === "timeout" ? "Ember did not answer in time" : "Couldn't reach Ember") +
        ", so the instructions may or may not have been saved. Check them after the next update.", "error");
      refresh();
    }).then(function () {
      ui.instructions.saving = false;
      save.textContent = "Save";
      save.disabled = false;
    });
  }

  // ---- Inbox

  // 0.12.0: what a message of the agent's promised (Ember's code keeps it until the agent closes it)
  function promiseLine(o) {
    var open = o.status === "open";
    var late = open && o.due && o.due < todayIso();
    return h("p", { class: "seen", "data-seen": open ? "no" : "yes" },
      h("span", { "aria-hidden": "true", text: open ? "◌ " : "✓ " }),
      "Promised: " + asText(o.what) + " · due " + fmtDay(o.due) +
      (open ? (late ? " · overdue" : " · open") : " · closed" + (o.result ? ": " + asText(o.result) : "")));
  }

  function renderInbox(messages, agentName, unread, dry) {
    var el = $("inbox");
    var name = agentName || "Ember";
    var sorted = messages.slice().sort(function (x, y) { return byDate("created_at")(x, y) || num(x.id) - num(y.id); });
    var unreadRows = sorted.filter(isUnread);
    $("inbox-sub").textContent = unread ? plural(unread, "unread message") + " from " + name + "." : "No unread messages.";
    var mark = $("inbox-mark-read");
    mark.hidden = !unreadRows.length || !!laterTitle();
    if (!sorted.length) {
      replace(el, emptyState("li", "No messages yet.", name + " writes here when it has a question or news for you. You can write first, too."));
    } else {
      replace(el, sorted.map(function (m) {
        var fromOwner = m.sender === "owner";
        var who = fromOwner ? (m.entered_by ? String(m.entered_by) : "You") : name;
        return h("li", { "data-from": fromOwner ? "owner" : "agent", "data-id": String(m.id), "data-unread": isUnread(m) ? "true" : null },
          h("span", { class: "who" }, who, " · ", timeEl(m.created_at),
            isUnread(m) ? [" ", h("span", { class: "chip", "data-tone": "accent" }, h("span", { "aria-hidden": "true", text: "●" }), "Unread")] : null,
            m.simulated ? [" ", testTag()] : null),
          h("span", { class: "msg-text", "data-removed": m.removed ? "true" : null, text: asText(m.text) }),
          fromOwner ? seenLine(!!m.seen_by_agent) : null,
          fromOwner && m.seen_by_agent && !m.removed ? answeredLine(m, sorted) : null,
          fromOwner ? null : arr(m.promises).map(promiseLine),
          fromOwner && !m.removed ? removeButton(num(m.id)) : null);
      }));
    }
    var later = laterTitle();
    var text = $("composer-text");
    text.disabled = !!later;
    $("composer-send").disabled = !!later || ui.sending;
    if (later) {
      $("composer-send").title = later;
      text.placeholder = "Replying " + lowerFirst(later);
    } else {
      $("composer-send").removeAttribute("title");
      text.placeholder = "Write to " + name + "…";
    }
    $("composer-label").textContent = "Message to " + name;
    // In dry run the fake model answers: say so above the box, and to screen readers in it.
    $("inbox-dry-note").hidden = !dry;
    text.setAttribute("aria-describedby", (dry ? "inbox-dry-note " : "") + "composer-hint composer-count composer-error");
  }

  function composerCount() {
    var n = $("composer-text").value.trim().length;
    var el = $("composer-count");
    el.textContent = n ? intFmt.format(n) + " / 2,000 characters" : "";
    el.setAttribute("data-over", n > 2000 ? "true" : "false");
  }

  function composerError(text) {
    var err = $("composer-error");
    err.textContent = text || "";
    err.hidden = !text;
    if (text) $("composer-text").setAttribute("aria-invalid", "true");
    else $("composer-text").removeAttribute("aria-invalid");
  }

  function setComposerStatus(text, kind) {
    $("composer-status").textContent = text;
    $("composer-status").setAttribute("data-kind", kind || "");
  }

  function sendMessage() {
    if (ui.sending || laterTitle()) return;
    var box = $("composer-text");
    var text = box.value.trim();
    composerError("");
    if (!text) { composerError("Write a message first."); box.focus(); return; }
    if (text.length > 2000) { composerError("Keep the message under 2,000 characters (it has " + intFmt.format(text.length) + ")."); box.focus(); return; }
    if (SECRET_HINT.test(text) && ui.secretWarned !== text) {
      ui.secretWarned = text;
      composerError("This looks like it contains a password. " + agentName() + " can't log in anywhere, and messages are stored and sent to Anthropic. Remove it, or press Send again to send it anyway.");
      box.focus();
      return;
    }
    ui.sending = true;
    var send = $("composer-send");
    send.disabled = true;
    send.textContent = "Sending…";
    setComposerStatus("Sending…", "");
    // Replying reads the agent's messages, but only those this page has shown (not one written meanwhile).
    var shown = arr(ui.data && ui.data.inbox).filter(function (m) { return isObject(m) && m.sender === "agent"; }).map(function (m) { return num(m.id); }).filter(function (n) { return n > 0; });
    var readUpTo = shown.length ? Math.max.apply(null, shown) : 0;
    request("POST", "api/inbox", { text: text, read_up_to: readUpTo }).then(function (res) {
      if (res.status === 201 || res.ok) {
        box.value = "";
        composerCount();
        // With the wake_on_message option the message wakes the agent: now, as soon as the running cycle ends, or once
        // the minute between wake-ups has passed. Without it (or while paused, ...) it waits for the next wake.
        var wake = isObject(res.data) && typeof res.data.wake === "string" ? res.data.wake : "";
        var when = wake === "now" ? " is waking up to read it."
          : wake === "after_cycle" ? " reads it as soon as the cycle it is working on ends."
          : wake === "soon" ? " woke up less than a minute ago and wakes again for it in a moment."
          : " reads it on its next wake.";
        if (wake === "now" || wake === "soon") ui.fastPollUntil = Date.now() + WAKE_FAST_POLL_MS;
        setComposerStatus("Sent. " + agentName() + when, "ok");
        refresh();
        return;
      }
      var data = isObject(res.data) ? res.data : {};
      if (res.status === 422 && data.field === "text" && typeof data.error === "string" && data.error) {
        composerError(endSentence(sentence(data.error)));
        setComposerStatus("Nothing was sent.", "error");
        box.focus();
        return;
      }
      ownerFailure(res, {}, function (msg) { setComposerStatus(msg, "error"); }, null);
    }).catch(function (err) {
      if (!(err instanceof RequestError)) console.error(err);
      setComposerStatus((err.kind === "timeout" ? "Ember did not answer in time" : "Couldn't reach Ember") +
        ", so the message may or may not have been sent. Check the conversation before you send it again.", "error");
      refresh();
    }).then(function () {
      ui.sending = false;
      send.textContent = "Send";
      send.disabled = !!laterTitle();
    });
  }

  // Removing the text of one of the owner's own messages (a password sent by mistake): two clicks, no dialog.
  function removeButton(id) {
    var btn = h("button", { type: "button", class: "link-button", "data-remove": String(id), text: "Remove text" });
    btn.addEventListener("click", function () {
      if (btn.getAttribute("data-armed") !== "true") {
        btn.setAttribute("data-armed", "true");
        btn.textContent = "Click again to remove this message's text for good";
        window.setTimeout(function () { btn.removeAttribute("data-armed"); btn.textContent = "Remove text"; }, 6000);
        return;
      }
      btn.disabled = true;
      var status = $("inbox-status");
      status.setAttribute("data-kind", "");
      status.textContent = "Removing…";
      request("POST", "api/inbox/" + id + "/remove", {}).then(function (res) {
        if (!res.ok) {
          ownerFailure(res, {}, function (msg) { status.textContent = msg; status.setAttribute("data-kind", "error"); }, null);
          btn.disabled = false;
          return;
        }
        status.textContent = "The message's text was removed.";
        status.setAttribute("data-kind", "ok");
        refresh();
      }).catch(function (err) {
        status.textContent = "Couldn't remove the text (" + errorText(err) + ").";
        status.setAttribute("data-kind", "error");
        btn.disabled = false;
      });
    });
    return h("p", { class: "remove-line" }, btn);
  }

  // Looks like a login: the words, or a generated password such as abcdef-123abc-XyZabc.
  var SECRET_HINT = /(pass(wor[dt])?|kennwort|zugangsdaten|\bpwd?\b|app.?password)|\b[A-Za-z0-9]{5,}-[A-Za-z0-9]{5,}-[A-Za-z0-9]{5,}\b/i;

  function markAllRead() {
    if (ui.markingRead || !ui.data) return;
    var ids = arr(ui.data.inbox).filter(function (m) { return isObject(m) && m.sender === "agent"; }).map(function (m) { return num(m.id); }).filter(function (n) { return n > 0; });
    if (!ids.length) return;
    var btn = $("inbox-mark-read");
    ui.markingRead = true;
    btn.disabled = true;
    var status = $("inbox-status");
    status.setAttribute("data-kind", "");
    status.textContent = "Marking as read…";
    request("POST", "api/inbox/read", { up_to_id: Math.max.apply(null, ids) }).then(function (res) {
      if (!res.ok) {
        ownerFailure(res, {}, function (msg) { status.textContent = msg; status.setAttribute("data-kind", "error"); }, null);
        return;
      }
      var n = isObject(res.data) ? num(res.data.marked) : NaN;
      status.textContent = isNaN(n) ? "Marked as read." : n === 1 ? "Marked 1 message as read." : "Marked " + intFmt.format(n) + " messages as read.";
      status.setAttribute("data-kind", "ok");
      $("inbox-title").focus();  // the button disappears once nothing is unread
      refresh();
    }).catch(function (err) {
      status.textContent = "Couldn't mark the messages as read (" + errorText(err) + ").";
      status.setAttribute("data-kind", "error");
    }).then(function () {
      ui.markingRead = false;
      btn.disabled = false;
    });
  }

  var MIND_EMPTY = {
    strategy: "The agent hasn't written a strategy yet.",
    lessons: "No lessons yet.",
    identity: "The agent hasn't described itself yet.",
  };

  function renderMind(mind) {
    var empty = !isObject(mind);
    $("mind-tabs").hidden = empty;
    $("mind-note").hidden = empty;
    $("mind-body").hidden = empty;
    $("mind-empty").hidden = !empty;
    if (empty) {
      replace($("mind-empty"), [h("p", { class: "empty-title", text: "No notes yet." }),
        h("p", { class: "muted", text: "The agent's strategy, lessons, identity and journal show up here once it has run." })]);
      return;
    }
    var body = $("mind-body");
    if (ui.mind === "reviews") {
      renderReviews(body, mind.reviews);
    } else if (ui.mind === "journal") {
      var entries = arr(mind.journal).slice().sort(function (x, y) { return (new Date(y.created_at).getTime() || 0) - (new Date(x.created_at).getTime() || 0); });
      replace(body, entries.length ? h("ol", { class: "journal" }, entries.map(function (j) {
        var entry = asText(j.entry);
        return h("li", null,
          h("p", { class: "when" }, j.cycle_id !== null && j.cycle_id !== undefined ? "Cycle #" + j.cycle_id + " · " : "", timeEl(j.created_at),
            j.author === "system" ? " · written by the system" : ""),
          j.summary ? h("p", { class: "journal-summary", text: String(j.summary) }) : null,
          entry && entry !== j.summary ? h("pre", { class: "journal-entry", text: entry }) : null,
          j.handoff ? h("p", { class: "journal-summary" }, h("strong", { text: "Next: " }), asText(j.handoff)) : null);
      })) : h("p", { class: "muted", text: "The journal is empty." }));
    } else if (ui.mind === "lessons") {
      renderLessons(body, mind);
    } else {
      // Markdown written by the agent, shown as it is (never rendered).
      var text = asText(mind[ui.mind]);
      replace(body, text.trim() ? h("pre", { class: "mind-text", text: text }) : h("p", { class: "muted", text: MIND_EMPTY[ui.mind] || "Nothing written yet." }));
    }
  }

  // Mind → Lessons (0.12.0): the owner pins a lesson: it is never dropped, a rewrite of the lessons must keep it, and
  // every plan shows it first. The daily review's consolidation merges the others once there are many.
  var MAX_PINS = 6;

  function lessonText(line) { return String(line).replace(/^[-\s]*(\[#c\d+\]\s*)?/, "").replace(/\s+/g, " ").trim(); }

  function renderLessons(body, mind) {
    var lines = asText(mind.lessons).split("\n").filter(function (line) { return /^- /.test(line); });
    var pins = arr(mind.lesson_pins);
    if (!lines.length && !pins.length) {
      replace(body, h("p", { class: "muted", text: MIND_EMPTY.lessons }));
      return;
    }
    var byText = {};
    pins.forEach(function (p) { byText[lessonText(p.text).toLowerCase()] = p; });
    var full = pins.length >= MAX_PINS;
    var status = h("p", { class: "muted small", role: "status" });
    var items = lines.map(function (line) {
      var pin = byText[lessonText(line).toLowerCase()] || null;
      var button = h("button", {
        type: "button", class: "small", text: pin ? "Unpin" : "Pin", disabled: !pin && full,
        title: pin ? "Stop keeping this lesson for good"
          : full ? "At most " + MAX_PINS + " lessons are pinned: unpin one first"
          : "Keep this lesson for good: never dropped, and first in every plan",
      });
      button.addEventListener("click", function () { pinLesson(button, status, pin, line); });
      return h("li", { "data-pinned": pin ? "true" : null }, pin ? h("strong", { text: "Pinned: " }) : null,
        h("span", { text: lessonText(line) }), " ", button);
    });
    replace(body, [
      h("p", { class: "muted small", text: "Pin a lesson to keep it for good: " + agentName() + " never drops it, a " +
        "rewrite of its lessons must keep it, and every plan shows it first. After the daily review, lessons that " +
        "say the same are merged and outdated ones retired, never a pinned one or one with numbers." }),
      h("ul", { class: "lessons" }, items), status]);
  }

  function pinLesson(button, status, pin, line) {
    button.disabled = true;
    status.removeAttribute("data-kind");
    var call = pin ? request("POST", "api/lessons/pins/" + pin.id + "/unpin", {})
      : request("POST", "api/lessons/pins", { text: line });
    call.then(function (res) {
      if (res.ok) {
        status.textContent = pin ? "Unpinned." : "Pinned: " + agentName() + " keeps it from now on.";
        refresh();
        return;
      }
      ownerFailure(res, {}, function (msg) { status.textContent = msg; status.setAttribute("data-kind", "error"); }, null);
      button.disabled = false;
    }).catch(function (err) {
      if (!(err instanceof RequestError)) console.error(err);
      status.textContent = "Couldn't reach Ember, so the lesson may or may not be " + (pin ? "unpinned" : "pinned") + ".";
      status.setAttribute("data-kind", "error");
      button.disabled = false;
    });
  }

  var VERDICTS = {
    "continue": { icon: "→", label: "Continue", tone: "good" },
    change: { icon: "↻", label: "Change", tone: "warning" },
    stop: { icon: "■", label: "Stop", tone: "critical" },
  };

  // Mind → Daily reviews: once a day the agent judges its own numbers (0.7.1); older servers send none.
  function renderReviews(body, reviews) {
    var items = arr(reviews);
    if (!items.length) {
      replace(body, h("p", { class: "muted", text: "No daily review yet. " + agentName() + " reviews its own numbers " +
        "once a day, before its first plan, from its second day on." }));
      return;
    }
    replace(body, h("ol", { class: "journal reviews" }, items.map(function (r) {
      var verdicts = arr(r.verdicts);
      return h("li", null,
        h("p", { class: "when" }, "Review of " + fmtDay(r.day) + " · ", timeEl(r.created_at),
          r.cycle_id !== null && r.cycle_id !== undefined ? " · cycle #" + r.cycle_id : ""),
        r.status !== "ok" ? h("p", { class: "muted", text: "This review failed: " + asText(r.note || "no answer") + "." }) : null,
        verdicts.length ? h("ul", { class: "verdicts" }, verdicts.map(function (v) {
          return h("li", null, chip(VERDICTS, v.verdict, String(v.verdict || "?")), " ",
            h("strong", { text: "#" + v.project_id + (v.title ? " " + asText(v.title) : "") }),
            v.why ? ": " + asText(v.why) : "");
        })) : null,
        arr(r.milestones).length ? h("ul", { class: "verdicts" }, arr(r.milestones).map(function (v) {
          return h("li", null, h("strong", { text: "Milestone #" + v.milestone_id + ": " + String(v.verdict || "?") +
            (v.verdict === "extend" && v.new_due ? " to " + fmtDay(v.new_due) : "") }),
            v.why ? ": " + asText(v.why) : "", " · " + (v.applied ? "applied" : "not applied: " + asText(v.outcome)));
        })) : null,
        reviewLine("Focus", r.focus),
        reviewLine("Working", r.working),
        reviewLine("Not working", r.not_working),
        reviewLine("What your decisions tell it", r.owner_feedback),
        reviewLine("Ventures", r.ventures),
        reviewLine("Roadmap", r.roadmap),
        reviewLine("Lesson", r.lesson),
        r.scorecard ? h("details", { class: "review-numbers" }, h("summary", { text: "The numbers it judged" }),
          h("pre", { class: "mind-text", text: asText(r.scorecard) })) : null);
    })));
  }

  function reviewLine(label, text) {
    text = asText(text);
    return text ? h("p", { class: "review-line" }, h("strong", { text: label + ": " }), text) : null;
  }

  // ---- System

  function renderSystem(d) {
    var s = d.system;
    var db = isObject(s.database) ? s.database : {};
    var options = isObject(s.options) ? s.options : {};
    replace($("system-facts"), [
      h("dt", { text: "Version" }), h("dd", { text: s.version }),
      h("dt", { text: "Started" }), h("dd", null, timeEl(s.started_at, fmtDateTime(s.started_at))),
      h("dt", { text: "Installed" }), h("dd", { text: s.installed_at || s.born_at ? fmtDateTime(s.installed_at || s.born_at) : "–" }),
      h("dt", { text: "Mode" }), h("dd", { text: (isDryRun(d) ? "Dry run (fake model, simulated API costs)" : "Live (real API calls, real money)") + (s.dev_mode ? " · local development" : "") }),
      h("dt", { text: "Options" }), h("dd", { text: s.config_source + (s.safe_mode ? " · SAFE MODE" : "") }),
      isObject(d.models) ? [h("dt", { text: "Models" }), h("dd", { text: modelsText(d.models) })] : null,
      h("dt", { text: "API key" }), h("dd", { text: options.anthropic_api_key_set ? "Set (hidden)" : "Not set" }),
      h("dt", { text: "Database" }), h("dd", { text: db.ok ? "OK, schema version " + db.schema_version : "Error: " + db.error }),
      h("dt", { text: "Economy" }), h("dd", { text: s.economy_error ? "Not available: " + s.economy_error
        : s.economy_broken ? "Spending stopped until the app restarts: " + s.economy_broken : d.agent ? "OK" : "Not available" }),
      h("dt", { text: "Sensor URL" }), h("dd", null, h("code", { text: s.sensor_url }),
        h("span", { class: "muted small", text: " for a Home Assistant REST sensor (see the app's Documentation tab)" })),
    ]);
    replace($("options"), optionsTable(options));
  }

  // 0.12.0: which model does what, and the research model's check (its first questions go to both models)
  function modelsText(m) {
    var parts = ["plans " + m.planner, "venture plans and reviews " + m.strategy, "work " + m.worker,
      "research " + m.research];
    var check = isObject(m.research_check) ? m.research_check : null;
    return parts.join(" · ") + (check ? " (research model check " + check.text + ")" : "");
  }

  var EMAIL_STATUS = {
    ok: { icon: "✓", label: "Working", tone: "good" },
    error: { icon: "✕", label: "Error", tone: "critical" },
    not_configured: { icon: "○", label: "Not set up", tone: "warning" },
    disabled: { icon: "–", label: "Off", tone: "" },
  };

  // System → Email: the agent's own mailbox (integrations.email). Missing on older servers: the card stays hidden.
  function renderEmail(d) {
    var e = isObject(d.integrations) && isObject(d.integrations.email) ? d.integrations.email : null;
    $("email-card").hidden = !e;
    if (!e) { replace($("email-facts"), []); replace($("email-never"), []); return; }
    var reason = e.reason ? String(e.reason) : null;
    if (!reason && e.status === "not_configured") reason = "Not set up: see the Documentation tab, 'Ember's mailbox'.";
    if (!reason && e.status === "disabled") reason = "Off: see the Documentation tab, 'Ember's mailbox', to set it up.";
    var showReason = reason && (e.available === false || e.status !== "ok");
    var mode = e.mode === "fake" ? "Fake mailbox (dry run: nothing is really fetched or sent)" : e.mode === "live" ? "Live (IMAP and SMTP)" : "–";
    replace($("email-facts"), [
      h("dt", { text: "Status" }), h("dd", null, chip(EMAIL_STATUS, e.status, sentence(String(e.status || "unknown").replace(/_/g, " "))),
        e.available === false && e.status === "ok" ? " (not available)" : null),
      showReason ? [h("dt", { text: "Why" }), h("dd", { class: "pre-line", text: reason })] : null,
      h("dt", { text: "Address" }), h("dd", { class: "link-text", text: e.address ? String(e.address) : "–" }),
      h("dt", { text: "Mailbox" }), h("dd", { text: mode }),
      h("dt", { text: "Last fetch" }), h("dd", null, e.last_fetch_at ? timeEl(e.last_fetch_at, fmtDateTime(e.last_fetch_at) + " (" + relTime(e.last_fetch_at) + ")") : "Never"),
      e.last_error ? [h("dt", { text: "Last error" }), h("dd", { class: "fact-error" }, h("span", { "aria-hidden": "true", text: "✕ " }), String(e.last_error))] : null,
      h("dt", { text: "Not opened by the agent" }), h("dd", { text: plural(e.unread, "email") }),
      h("dt", { text: "Sent today" }), h("dd", { text: count(e.sent_today) + " (limit: " + count(e.daily_limit) + " a day)" }),
    ]);
    replace($("email-never"), e.available ? [emailNeverArea(e)] : []);
  }

  // 0.12.0: the addresses Ember never emails (they asked to stop, the agent marked them, or you added them), and a
  // form to add one. An opt-out is final: the list can't be shortened.
  function emailNeverArea(e) {
    var c = ui.emailNever;
    if (!c) {
      c = ui.emailNever = {};
      var inputId = "email-never-address";
      c.address = h("input", { type: "email", id: inputId, autocomplete: "off", spellcheck: "false", maxlength: "254", placeholder: "name@example.org" });
      c.add = h("button", { type: "button", class: "btn", text: "Never email" });
      c.status = h("p", { class: "form-status small", role: "status" });
      c.list = h("div");
      c.add.addEventListener("click", function () {
        var address = c.address.value.trim();
        if (!address) { c.status.textContent = "Type the address first."; c.address.focus(); return; }
        if (!window.confirm("Never let " + agentName() + " email " + address + " again? This can't be undone.")) return;
        c.add.disabled = true;
        c.status.textContent = "Saving…";
        request("POST", "api/email/suppressions", { address: address }).then(function (res) {
          var data = isObject(res.data) ? res.data : {};
          if (!res.ok) {
            c.status.textContent = res.status === 422 && typeof data.error === "string" ? endSentence(sentence(data.error)) : "Not saved (" + errorText(httpError(res)) + ").";
            return;
          }
          c.status.textContent = data.added === false ? "It was already on the list." : "Added: " + agentName() + " never emails it.";
          c.address.value = "";
          refresh();
        }).catch(function (err) {
          c.status.textContent = "Couldn't reach Ember (" + errorText(err) + "): check the list after the next update.";
        }).then(function () { c.add.disabled = false; });
      });
      c.wrap = h("div", { class: "email-never" },
        h("h3", { class: "small-head", text: "Never emailed" }), c.list,
        h("div", { class: "field" }, h("label", { for: inputId, text: "Add an address that asked you, in any way, not to be emailed" }),
          c.address, h("div", { class: "form-actions" }, c.add)), c.status);
    }
    var shown = arr(e.suppressed);
    var total = num(e.suppressed_count);
    replace(c.list, shown.length ? [
      h("ul", { class: "small" }, shown.map(function (s) {
        return h("li", null, h("span", { class: "link-text", text: asText(s.address) }),
          h("span", { class: "muted", text: " · " + fmtDate(s.since) + (s.reason ? " · " + asText(s.reason) : "") }));
      })),
      total > shown.length ? h("p", { class: "muted small", text: "and " + plural(total - shown.length, "older one") + "." }) : null,
    ] : [h("p", { class: "muted small", text: "None yet." })]);
    return c.wrap;
  }

  // ---- System -> Etsy (0.8.0): the shop, connecting it, what Ember listed and the orders it brought.

  var ETSY_STATUS = {
    ok: { icon: "✓", label: "Connected", tone: "good" },
    not_connected: { icon: "○", label: "Not connected", tone: "warning" },
    not_configured: { icon: "○", label: "Not set up", tone: "warning" },
    disabled: { icon: "–", label: "Off", tone: "" },
  };

  var LISTING_STATUS = {
    running: { icon: "●", label: "Being created", tone: "accent" },
    active: { icon: "✓", label: "Live", tone: "good" },
    draft: { icon: "!", label: "Draft at Etsy", tone: "warning" },
    failed: { icon: "✕", label: "Not listed", tone: "critical" },
    unclear: { icon: "!", label: "Unclear", tone: "critical" },
  };

  // A link only to www.etsy.com over https; anything else is shown as text.
  function etsyLink(value, text) {
    var url;
    try { url = new URL(String(value)); } catch (e) { url = null; }
    if (!url || !/^https:$/.test(url.protocol) || url.hostname !== "www.etsy.com" || url.port || url.username || url.password) {
      return h("span", { class: "link-text", text: text || String(value || "–") });
    }
    var a = document.createElement("a");
    a.setAttribute("href", url.href);
    a.setAttribute("rel", "noopener noreferrer");
    a.setAttribute("target", "_blank");
    append(a, [text || url.href, h("span", { class: "visually-hidden", text: " (opens in a new tab)" })]);
    return a;
  }

  function etsyInfo(d) {
    return isObject(d.integrations) && isObject(d.integrations.etsy) ? d.integrations.etsy : null;
  }

  function renderEtsy(d) {
    var e = etsyInfo(d);
    $("etsy-card").hidden = !e;
    if (!e) { replace($("etsy-facts"), []); replace($("etsy-listings"), []); replace($("etsy-orders"), []); return; }
    var fake = e.mode === "fake";
    // Etsy's trademark notice, which its API terms require wherever Ember shows Etsy (in dry run too).
    var notice = e.notice ? String(e.notice) : "";
    $("etsy-notice").hidden = !notice;
    $("etsy-notice").textContent = notice;
    var reason = e.reason ? String(e.reason) : null;
    if (!reason && e.status === "not_configured") reason = "Not set up: see the Documentation tab, 'Etsy'.";
    if (!reason && e.status === "disabled") reason = "Off: see the Documentation tab, 'Etsy', to set it up.";
    replace($("etsy-facts"), [
      h("dt", { text: "Status" }), h("dd", null, chip(ETSY_STATUS, e.status, sentence(String(e.status || "unknown").replace(/_/g, " ")))),
      reason && (e.status !== "ok" || fake) ? [h("dt", { text: fake ? "Mode" : "Why" }), h("dd", { class: "pre-line", text: reason })] : null,
      h("dt", { text: "Shop" }), h("dd", null, e.shop_name ? (fake ? h("span", { text: String(e.shop_name) + " (fake)" }) : etsyLink(e.shop_url, String(e.shop_name))) : "–"),
      e.connected_at ? [h("dt", { text: "Connected" }), h("dd", null, timeEl(e.connected_at, fmtDateTime(e.connected_at)))] : null,
      e.refresh_expires_at ? [h("dt", { text: "Connection valid until" }), h("dd", null, timeEl(e.refresh_expires_at, fmtDateTime(e.refresh_expires_at)),
        h("span", { class: "muted small", text: " (renewed while Ember uses it)" }))] : null,
      h("dt", { text: "Listings today" }), h("dd", { text: count(e.created_today) + " (limit: " + count(e.daily_limit) + " a day)" +
        (num(e.waiting) ? "; " + plural(num(e.waiting), "approved listing") + " waiting" : "") }),
      // When the shop's listings and orders were last read (every hour while Ember runs): how fresh the numbers are.
      e.last_sync_at ? [h("dt", { text: "Numbers from" }), h("dd", null, timeEl(e.last_sync_at, fmtDateTime(e.last_sync_at) + " (" + relTime(e.last_sync_at) + ")"))] : null,
      e.last_error ? [h("dt", { text: "Last error" }), h("dd", { class: "fact-error" }, h("span", { "aria-hidden": "true", text: "✕ " }), String(e.last_error))] : null,
    ]);
    replace($("etsy-connect"), fake ? [] : [etsyConnectArea(e)]);
    var listings = arr(e.listings);
    replace($("etsy-listings"), listings.length ? h("div", { class: "table-wrap" }, h("table", null,
      h("thead", null, h("tr", null, ["Listing", "Status", "Views", "Favorites", "Since"].map(function (c) { return h("th", { scope: "col", text: c }); }))),
      h("tbody", null, listings.map(function (l) {
        return h("tr", null,
          h("td", null, l.url && !fake ? etsyLink(l.url, asText(l.title)) : h("span", { text: asText(l.title) }),
            l.listing_id ? h("span", { class: "muted small", text: " #" + l.listing_id }) : null,
            l.error ? h("p", { class: "muted small pre-line", text: asText(l.error) }) : null),
          h("td", null, chip(LISTING_STATUS, l.status, sentence(String(l.status || "?"))), l.state && l.state !== l.status ? h("span", { class: "muted small", text: " (" + asText(l.state) + ")" }) : null,
            // 0.12.0: when it ends at Etsy, or that Etsy renews it
            l.status === "active" && l.auto_renew ? h("span", { class: "muted small", text: " · renews itself" }) : null,
            l.status === "active" && !l.auto_renew && l.ends_at ? h("span", { class: "muted small", text: (l.state === "expired" ? " · ended " : " · ends ") + fmtDate(l.ends_at) }) : null),
          h("td", { class: "num", text: l.views === null || l.views === undefined ? "–" : count(l.views) }),
          h("td", { class: "num", text: l.favorites === null || l.favorites === undefined ? "–" : count(l.favorites) }),
          h("td", null, timeEl(l.started_at, fmtDate(l.started_at))));
      })))) : h("p", { class: "muted", text: "None yet. When you approve a listing the agent proposed, Ember creates it here." }));
    var orders = arr(e.orders);
    // 0.12.0: Ember's code records the orders in the ledger itself when the owner turned that on
    var auto = e.auto_revenue ? h("p", { class: "muted small", text: "Ember's code records these orders in the ledger at each sync: their revenue, Etsy's fees and refunds" +
      (e.auto_revenue_since ? ", for the orders placed from " + fmtDay(e.auto_revenue_since) + " on" : "") +
      (num(e.usd_per_eur) > 0 ? " (EUR at " + num(e.usd_per_eur) + " USD)." : "; orders in EUR are yours to record (no exchange rate set).") }) : null;
    replace($("etsy-orders"), orders.length ? [auto, h("div", { class: "table-wrap" }, h("table", null,
      h("thead", null, h("tr", null, ["Ordered", "Ember's lines", "Listings", "Status", "Revenue"].map(function (c) { return h("th", { scope: "col", text: c }); }))),
      h("tbody", null, orders.map(function (o) {
        return h("tr", null,
          h("td", null, timeEl(o.ordered_at, fmtDateTime(o.ordered_at))),
          // 0.12.0: only Ember's lines, net of tax, shipping, the coupon and refunds (before, the whole receipt).
          h("td", { class: "num", text: asText(o.total) + (o.whole_receipt ? " (whole receipt)" : "") }),
          h("td", { text: arr(o.items).map(function (i) { return asText(i.title) + (num(i.quantity) > 1 ? " × " + i.quantity : ""); }).join("; ") }),
          h("td", { text: o.status ? asText(o.status) : "–" }),
          h("td", null, orderRevenueCell(o, fake)));
      }))))] : h("p", { class: "muted", text: "No orders with Ember's listings yet." }));
  }

  // What the owner can do about an order's revenue: record it (only a paid order in EUR or USD, whose net is known),
  // or see why not; an order refunded after it was recorded asks for a correction of that entry.
  function orderRevenueCell(o, fake) {
    var refunded = o.status === "fully refunded" || o.status === "canceled";
    if (o.recorded) {
      var by = o.recorded_by === "etsy" ? "Recorded by Ember's code" : "Recorded";  // 0.12.0: from Etsy's numbers
      var said = h("span", { class: "muted small", text: refunded ? by + ", then " + o.status + (o.corrected_in_full ? ", and corrected" : ": correct entry #" + o.entry_id) : by });
      if (o.fees_recordable) return [said, " ", recordFeesButton(o, fake)];  // 0.12.0: Etsy's fees on it
      return o.fees_recorded ? [said, h("span", { class: "muted small", text: " · fees recorded" })] : said;
    }
    if (o.recordable) return recordOrderButton(o, fake);
    if (refunded) return h("span", { class: "muted small", text: "Nothing to record" });
    if (o.whole_receipt) return h("span", { class: "muted small", text: "From before 0.12.0: check Ember's share in Etsy" });
    if (o.currency !== "EUR" && o.currency !== "USD") return h("span", { class: "muted small", text: "In " + asText(o.currency) + ": convert it and record it yourself" });
    return h("span", { class: "muted small", text: "Not paid yet" });
  }

  // Revenue is only ever recorded by the owner: this opens the revenue form filled in from the order (its key
  // makes sure one order is never recorded twice).
  function recordOrderButton(o, fake) {
    var b = h("button", { type: "button", class: "btn btn-small", text: "Record as revenue" });
    b.addEventListener("click", function () {
      var cents = num(o.total_cents);
      openLedgerForm("revenue", isNaN(cents) ? null : (cents / 100).toFixed(2), {
        currency: o.currency,  // only EUR and USD orders get this button
        source: "Etsy order " + o.receipt_id,
        day: String(o.ordered_at || "").slice(0, 10),
        idKey: String(o.revenue_key || ""),
        testMoney: fake,
        projectId: o.project_id || null,  // the project whose listing sold (0.12.0)
        ventureId: o.venture_id || null,
      });
    });
    return b;
  }

  // 0.12.0: Ember's share of Etsy's fees on a recorded order (its processing fee, read from the payment, and the 6.5%
  // transaction fee), as an expense of the same project: the form opens filled in, and its key records it once.
  function recordFeesButton(o, fake) {
    var cents = num(o.fees_cents);
    var amount = isNaN(cents) ? null : (cents / 100).toFixed(2);
    var b = h("button", { type: "button", class: "btn btn-small", text: "Record Etsy's fees" + (amount ? " (" + amount + " " + asText(o.currency) + ")" : "") });
    b.addEventListener("click", function () {
      openLedgerForm("expense", amount, {
        currency: o.currency,
        note: "Etsy's fees on order " + o.receipt_id + ": payment processing and the 6.5% transaction fee",
        day: String(o.ordered_at || "").slice(0, 10),
        idKey: String(o.fee_key || ""),
        testMoney: fake,
        projectId: o.project_id || null,
        ventureId: o.venture_id || null,
      });
    });
    return b;
  }

  // Connecting is two steps: Etsy's page (in a new tab), then the address it sends you to, pasted here. The
  // controls are built once, so a refresh of the dashboard never loses a pasted address.
  function etsyConnectArea(e) {
    var c = ui.etsyConnect;
    if (!c) {
      c = ui.etsyConnect = { url: null };
      c.status = h("p", { class: "form-status small", role: "status" });
      c.start = h("button", { type: "button", class: "btn btn-primary", text: "Connect your Etsy shop" });
      c.linkBox = h("div", { class: "etsy-step", hidden: true });
      var addressId = "etsy-address";
      c.address = h("input", { type: "url", id: addressId, autocomplete: "off", spellcheck: "false", placeholder: "The whole address, with its code and state" });
      c.finish = h("button", { type: "button", class: "btn btn-primary", text: "Finish connecting" });
      c.pasteBox = h("div", { class: "etsy-step field", hidden: true },
        h("label", { for: addressId, text: "2. Paste the address Etsy sent you to" }),
        h("p", { class: "hint", text: "After you allow access, Etsy opens your redirect address. It may show an error page: that's fine. Copy the whole address from the address bar and paste it here." }),
        c.address, h("div", { class: "form-actions" }, c.finish));
      c.disconnect = h("button", { type: "button", class: "btn btn-danger", text: "Disconnect" });
      c.start.addEventListener("click", function () {
        c.start.disabled = true;
        c.status.textContent = "Asking Ember for Etsy's page…";
        request("POST", "api/etsy/connect", {}).then(function (res) {
          if (!res.ok) throw httpError(res);
          c.url = String(res.data.authorize_url || "");
          replace(c.linkBox, [h("p", null, h("strong", { text: "1. Allow Ember's access at Etsy: " }), etsyLink(c.url, "open Etsy's page")),
            h("p", { class: "muted small", text: "Log in as the shop's owner and allow access. The page is valid for 15 minutes." })]);
          c.linkBox.hidden = false;
          c.pasteBox.hidden = false;
          c.status.textContent = "";
        }).catch(function (err) {
          c.status.textContent = "Couldn't start connecting (" + errorText(err) + ").";
        }).then(function () { c.start.disabled = false; });
      });
      c.finish.addEventListener("click", function () {
        var address = c.address.value.trim();
        if (!address) { c.status.textContent = "Paste the address first."; return; }
        c.finish.disabled = true;
        c.status.textContent = "Connecting…";
        request("POST", "api/etsy/finish", { address: address }).then(function (res) {
          if (!res.ok) throw httpError(res);
          c.status.textContent = "Connected to " + asText(res.data.shop_name) + ".";
          c.address.value = "";
          c.linkBox.hidden = c.pasteBox.hidden = true;
          refresh();
        }).catch(function (err) {
          c.status.textContent = "Not connected: " + errorText(err) + ".";
        }).then(function () { c.finish.disabled = false; });
      });
      c.disconnect.addEventListener("click", function () {
        if (!window.confirm("Disconnect the Etsy shop? Ember can't create listings until you connect it again. Listings already on Etsy stay there.")) return;
        request("POST", "api/etsy/disconnect", {}).then(function (res) {
          if (!res.ok) throw httpError(res);
          c.status.textContent = "Disconnected.";
          refresh();
        }).catch(function (err) { c.status.textContent = "Couldn't disconnect (" + errorText(err) + ")."; });
      });
      c.wrap = h("div", { class: "etsy-connect" });
    }
    var connected = e.status === "ok";
    c.start.textContent = connected ? "Connect again" : "Connect your Etsy shop";
    var canConnect = e.status === "ok" || e.status === "not_connected";
    replace(c.wrap, [
      canConnect ? h("div", { class: "form-actions" }, c.start, connected ? c.disconnect : null) : null,
      c.linkBox, c.pasteBox, c.status,
    ]);
    return c.wrap;
  }

  function renderTransitions(list) {
    replace($("transitions"), list.length ? list.map(function (t) {
      return h("li", null,
        h("span", { class: "when" }, timeEl(t.ts, fmtDateTime(t.ts))),
        h("span", { class: "level", text: (t.mode === "dry_run" ? "dry run" : t.mode) + (t.life_id ? " · life " + t.life_id : "") }),
        h("span", { class: "msg" },
          h("strong", null, stateLabel(t.from_state), h("span", { "aria-hidden": "true", text: " → " }), h("span", { class: "visually-hidden", text: " to " }), stateLabel(t.to_state)),
          t.reason ? " · " + t.reason : ""));
    }) : emptyState("li", "No changes yet."));
  }

  function renderEvents(events) {
    var el = $("events");
    var openIds = {};
    Array.prototype.forEach.call(el.querySelectorAll("details[open]"), function (x) { openIds[x.getAttribute("data-id")] = true; });
    replace(el, events.length ? events.map(function (e) {
      return h("li", null,
        h("span", { class: "when" }, timeEl(e.ts, fmtDateTime(e.ts))),
        h("span", { class: "level", "data-level": e.level, text: e.level }),
        h("span", { class: "msg", text: e.message }),
        eventDetails(e, !!openIds[String(e.id)]));
    }) : emptyState("li", "No events."));
  }

  function eventDetails(e, open) {
    var det = isObject(e.details) ? e.details : null;
    if (!det) return null;
    var parts = [];
    var suppressed = num(det.suppressed_before_this);
    if (suppressed > 0) {
      parts.push(h("p", { class: "small", text: suppressed + (suppressed === 1 ? " similar message was" : " similar messages were") + " left out of the log before this one." }));
    }
    if (det.traceback) parts.push(h("pre", { class: "traceback", text: String(det.traceback) }));
    var rest = Object.keys(det).filter(function (k) { return k !== "traceback" && k !== "suppressed_before_this"; });
    if (rest.length) {
      parts.push(h("pre", { text: rest.map(function (k) { return k + ": " + (typeof det[k] === "string" ? det[k] : JSON.stringify(det[k])); }).join("\n") }));
    }
    if (!parts.length) return null;
    return h("details", { class: "event-details", "data-id": String(e.id), open: open }, h("summary", { text: "Details" }), parts);
  }

  function optionsTable(options) {
    var rows = Object.keys(options).filter(function (k) { return k !== "price_table"; }).map(function (k) {
      var v = options[k];
      return h("tr", null, h("th", { scope: "row", text: k }), h("td", { text: typeof v === "object" && v !== null ? JSON.stringify(v) : String(v) }));
    });
    var prices = arr(options.price_table).map(function (p) {
      return h("tr", null, h("td", { text: p.model }),
        ["input", "output", "cache_write_5m", "cache_write_1h", "cache_read"].map(function (k) {
          return h("td", { class: "num", text: isNaN(num(p[k])) ? "–" : usdExact.format(num(p[k])) });
        }));
    });
    return [
      h("table", null, h("tbody", null, rows)),
      h("h3", { class: "chart-title", text: "Price table (USD per million tokens)" }),
      h("table", null,
        h("thead", null, h("tr", null, ["Model", "Input", "Output", "Cache write 5 min", "Cache write 1 h", "Cache read"].map(function (x, i) {
          return h("th", { scope: "col", class: i ? "num" : null, text: x });
        }))),
        h("tbody", null, prices)),
    ];
  }

  // ------------------------------------------------------------------ owner forms (money entry)
  // Built once and never re-rendered, so polling can't wipe what the owner is typing.

  var AMOUNT_RE = /^[0-9]{1,6}([.,][0-9]{1,2})?$/;
  var FX_RE = /^[0-9]([.,][0-9]{1,6})?$/;
  var DAY_RE = /^\d{4}-\d{2}-\d{2}$/;

  var FORMS = {
    revenue: {
      title: "Add revenue", endpoint: "api/ledger/revenue",
      intro: "Money the agent earned, for example a tip you received for its work.",
      fields: [
        { name: "amount", kind: "amount", currency: true },
        { name: "source", kind: "text", label: "Source", hint: "Where the money came from, for example Ko-fi.", required: true, missing: "Say where the money came from." },
        { name: "attribution", kind: "attribution" },
        { name: "day", kind: "day" },
        { name: "note", kind: "note", label: "Note (optional)" },
        { name: "test_money", kind: "test_money" },
      ],
    },
    grant: {
      title: "Grant funds", endpoint: "api/ledger/grant",
      intro: function () {
        return "This records money you allow " + agentName() + " to spend. It doesn't buy API credits; make sure your Anthropic account has at least this much.";
      },
      fields: [
        { name: "amount", kind: "amount", currency: true },
        { name: "day", kind: "day" },
        { name: "note", kind: "note", label: "Note (optional)" },
        { name: "test_money", kind: "test_money" },
      ],
    },
    expense: {
      title: "Record expense", endpoint: "api/ledger/expense",
      intro: "Money you paid for the agent outside the Anthropic API, for example a domain or a platform fee.",
      fields: [
        { name: "amount", kind: "amount", currency: true },
        { name: "note", kind: "note", label: "What it was for", required: true, missing: "Say what it was for." },
        { name: "attribution", kind: "attribution" },
        { name: "day", kind: "day" },
        { name: "test_money", kind: "test_money" },
      ],
    },
    "api-correction": {
      title: "Correct API cost", endpoint: "api/ledger/api-correction",
      intro: "Change the recorded API cost of a day, for example to match the Anthropic Console.",
      fields: [
        { name: "direction", kind: "direction", label: "Change", missing: "Choose whether to increase or decrease the API cost.",
          options: [["increase", "Increase the API cost"], ["decrease", "Decrease the API cost"]] },
        { name: "amount", kind: "amount", label: "By how much (USD)" },
        { name: "day", kind: "day", hint: "The day whose cost you correct. Leave empty for today." },
        { name: "note", kind: "note", label: "Reason", hint: "For example: to match the Anthropic Console.", required: true, missing: "Say why you correct the API cost." },
      ],
    },
    adjustment: {
      title: "Other correction", endpoint: "api/ledger/adjustment",
      intro: "Anything else that changes the balance.",
      fields: [
        { name: "direction", kind: "direction", label: "Change", missing: "Choose whether to add or subtract money.",
          options: [["add", "Add money"], ["subtract", "Subtract money"]] },
        { name: "amount", kind: "amount", currency: true },
        { name: "note", kind: "note", label: "Reason", required: true, missing: "Say why you make this correction." },
        { name: "day", kind: "day" },
        { name: "test_money", kind: "test_money" },
      ],
    },
  };

  var CORRECTION_FORM = {
    title: "Correct an entry", submitLabel: "Record correction",
    fields: [
      { name: "amount", kind: "amount", label: "Amount to take back (USD)", hint: "For example 5 or 12.50, at most what is left of the entry." },
      { name: "note", kind: "note", label: "Reason", required: true, missing: "Say why this entry is corrected." },
    ],
  };

  function newKey() {
    // crypto.getRandomValues works on plain http too (randomUUID needs a secure context).
    var bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    return Array.prototype.map.call(bytes, function (b) { return (b < 16 ? "0" : "") + b.toString(16); }).join("");
  }

  function introText(spec) {
    return typeof spec.intro === "function" ? spec.intro() : spec.intro;
  }

  function buildForm(key, spec) {
    var id = "form-" + key;
    var f = {
      key: key, spec: spec, fields: {}, targets: {},
      idKey: newKey(), busy: false, flags: {}, pendingFlag: null, unresolved: false,
      endpoint: function () { return spec.endpoint; },
    };
    f.form = h("form", { class: "money-form", id: id, novalidate: true, hidden: true, "aria-labelledby": id + "-title" },
      h("h3", { id: id + "-title", text: spec.title }),
      spec.intro ? (f.intro = h("p", { class: "form-intro", text: introText(spec) })) : null);
    f.lead = h("p", { class: "form-note", hidden: true });
    append(f.form, f.lead);
    spec.fields.forEach(function (fs) { append(f.form, buildField(f, fs)); });

    var msgId = id + "-confirm";
    f.confirmMsg = h("p", { class: "confirm-msg", id: msgId, tabindex: "-1" });
    f.confirmYes = h("button", { type: "button", class: "btn", text: "Record anyway" });
    f.confirmNo = h("button", { type: "button", class: "btn btn-ghost", text: "Cancel" });
    f.confirm = h("div", { class: "confirm", role: "group", "aria-labelledby": msgId, hidden: true },
      f.confirmMsg, h("div", { class: "form-actions" }, f.confirmYes, f.confirmNo));
    f.submit = h("button", { type: "submit", class: "btn btn-primary", text: spec.submitLabel || spec.title });
    f.close = h("button", { type: "button", class: "btn btn-ghost", text: "Close" });
    f.status = h("p", { class: "form-status", role: "status" });
    append(f.form, [f.confirm, h("div", { class: "form-actions" }, f.submit, f.close), f.status]);

    f.form.addEventListener("submit", function (ev) { ev.preventDefault(); submitForm(f); });
    f.form.addEventListener("input", function (ev) { onFormInput(f, ev); });
    f.form.addEventListener("change", function (ev) { onFormInput(f, ev); });
    f.confirmYes.addEventListener("click", function () {
      f.flags[f.pendingFlag] = f.pendingValue;
      submitForm(f);
    });
    f.confirmNo.addEventListener("click", function () {
      hideConfirm(f);
      f.flags = {};
      setStatus(f, "Nothing was recorded.", "");
      focusFirst(f);
    });
    f.close.addEventListener("click", function () { closeForm(f); });
    return f;
  }

  function fieldIds(f, name) {
    var id = "f-" + f.key + "-" + name;
    return { id: id, hint: id + "-hint", error: id + "-error" };
  }

  function errorEl(ids) { return h("p", { class: "field-error", id: ids.error, hidden: true }); }

  function buildField(f, fs) {
    var ids = fieldIds(f, fs.name);
    var err = errorEl(ids);
    var described = (fs.hint || fs.kind === "amount" || fs.kind === "day" || fs.kind === "test_money" ? ids.hint + " " : "") + ids.error;
    var field = { spec: fs, error: err };
    var wrap;
    if (fs.kind === "direction") {
      field.radios = fs.options.map(function (o) {
        return h("input", { type: "radio", name: ids.id, value: o[0], id: ids.id + "-" + o[0], "aria-describedby": ids.error, "data-field": fs.name });
      });
      wrap = h("fieldset", { class: "field field-choice" },
        h("legend", { text: fs.label }),
        h("div", { class: "choices" }, fs.options.map(function (o, i) {
          return h("label", { class: "choice", for: ids.id + "-" + o[0] }, field.radios[i], " ", o[1]);
        })),
        err);
      f.targets[fs.name] = { error: err, control: field.radios[0] };
    } else if (fs.kind === "test_money") {
      field.control = h("input", { type: "checkbox", id: ids.id, checked: true, "aria-describedby": ids.hint, "data-field": fs.name });
      wrap = h("div", { class: "field field-check" },
        h("label", { class: "choice", for: ids.id }, field.control, " Test money (dry run only)"),
        h("p", { class: "hint", id: ids.hint, text: "Test money only counts in dry run. Untick it to record real money, which also counts once the agent runs live." }),
        err);
      f.targets[fs.name] = { error: err, control: field.control };
    } else if (fs.kind === "attribution") {
      // 0.12.0: the project or venture the money belongs to, so it shows as what that project or venture earned.
      field.control = h("select", { id: ids.id, "aria-describedby": ids.hint + " " + ids.error, "data-field": fs.name });
      wrap = h("div", { class: "field" },
        h("label", { for: ids.id, text: "For (optional)" }), field.control,
        h("p", { class: "hint", id: ids.hint, text: "The project or venture this money belongs to. A project's venture counts it too." }),
        err);
      f.targets.project_id = f.targets.venture_id = { error: err, control: field.control };
      fillAttribution(field, "");
    } else if (fs.kind === "amount") {
      field.control = h("input", { type: "text", id: ids.id, inputmode: "decimal", autocomplete: "off", spellcheck: "false",
        "aria-describedby": described, "data-field": fs.name });
      var row = h("div", { class: "amount-row" }, field.control);
      var extra = [];
      f.targets.amount = { error: err, control: field.control };
      if (fs.currency) {
        var cur = fieldIds(f, "currency");
        field.currency = h("select", { id: cur.id, "data-field": "currency", "aria-label": "Currency" },
          h("option", { value: "USD", text: "USD" }), h("option", { value: "EUR", text: "EUR" }));
        append(row, field.currency);
        f.targets.currency = { error: err, control: field.currency };
        var fx = fieldIds(f, "fx_rate");
        var fxErr = errorEl(fx);
        field.fx = h("input", { type: "text", id: fx.id, inputmode: "decimal", autocomplete: "off", spellcheck: "false",
          "aria-describedby": fx.hint + " " + fx.error, "data-field": "fx_rate" });
        field.fxWrap = h("div", { class: "field", hidden: true },
          h("label", { for: fx.id, text: "Exchange rate (USD per 1 EUR)" }), field.fx,
          h("p", { class: "hint", id: fx.hint, text: "For example 1.08, from your bank statement." }),
          fxErr);
        field.preview = h("p", { class: "fx-preview", "aria-live": "polite", hidden: true });
        f.targets.fx_rate = { error: fxErr, control: field.fx };
        extra = [field.fxWrap, field.preview];
      }
      wrap = [h("div", { class: "field" },
        h("label", { for: ids.id, text: fs.label || "Amount" }), row,
        h("p", { class: "hint", id: ids.hint, text: fs.hint || "For example 5 or 12.50. At most 2 decimals, after a dot or a comma." }),
        err), extra];
    } else {
      var isDay = fs.kind === "day";
      // Notes and sources are single lines on the server (500 and 200 characters).
      field.control = h("input", { type: isDay ? "date" : "text", id: ids.id, autocomplete: "off", "aria-describedby": described, "data-field": fs.name,
        maxlength: isDay ? null : fs.kind === "note" ? "500" : "200", required: fs.required ? true : null });
      wrap = h("div", { class: "field" },
        h("label", { for: ids.id, text: fs.label || (isDay ? "Day (optional)" : fs.name) }), field.control,
        fs.hint || isDay ? h("p", { class: "hint", id: ids.hint, text: fs.hint || "The day the money moved. Leave empty for today." }) : null,
        err);
      f.targets[fs.name] = { error: err, control: field.control };
    }
    field.wrap = Array.isArray(wrap) ? wrap[0] : wrap;
    f.fields[fs.name] = field;
    return wrap;
  }

  function fillAttribution(field, chosen) {
    var d = ui.data || {};
    var options = [h("option", { value: "", text: "Nothing in particular" })];
    var projects = arr(d.projects).filter(function (p) { return p && p.id; });
    if (projects.length) {
      options.push(h("optgroup", { label: "Projects" }, projects.map(function (p) {
        return h("option", { value: "p:" + p.id, text: "#" + p.id + " " + asText(p.title) + " (" + asText(p.status) + ")" });
      })));
    }
    var ventures = arr(d.venture_choices).filter(function (v) { return v && v.id; });
    if (ventures.length) {
      options.push(h("optgroup", { label: "Ventures" }, ventures.map(function (v) {
        return h("option", { value: "v:" + v.id, text: "#" + v.id + " " + asText(v.title) + " (" + asText(v.stage) + ")" });
      })));
    }
    replace(field.control, options);
    field.control.value = chosen || "";
    if (field.control.value !== (chosen || "")) field.control.value = "";  // gone meanwhile
  }

  function readValues(f) {
    var v = {};
    Object.keys(f.fields).forEach(function (name) {
      var field = f.fields[name];
      var kind = field.spec.kind;
      if (kind === "direction") {
        v[name] = "";
        field.radios.forEach(function (r) { if (r.checked) v[name] = r.value; });
      } else if (kind === "test_money") {
        v[name] = field.wrap.hidden ? null : field.control.checked;
      } else {
        v[name] = field.control.value.trim();
      }
      if (kind === "amount" && field.currency) {
        v.currency = field.currency.value;
        v.fx_rate = field.fx.value.trim();
      }
    });
    return v;
  }

  function cents(text) {
    var m = /^([0-9]+)(?:[.,]([0-9]{1,2}))?$/.exec(text);
    return m ? Number(m[1]) * 100 + Number(((m[2] || "") + "00").slice(0, 2)) : NaN;
  }

  function amountProblem(text) {
    if (!text) return "Enter an amount.";
    if (/^[-+]/.test(text)) return "Enter the amount without a sign.";
    if (/^[$€]|[$€]$|\s(USD|EUR)$/i.test(text)) return "Enter just the number, without a currency sign.";
    if (/^[0-9]{1,3}([.,][0-9]{3})+([.,][0-9]{1,2})?$/.test(text) || /^[0-9]+[.,][0-9]{3,}$/.test(text)) {
      return "Use at most 2 decimals and no thousands separators. For one thousand, type 1000.";
    }
    if (!AMOUNT_RE.test(text)) return "Enter an amount like 12.50: up to 6 digits, and at most 2 decimals after a dot or a comma.";
    if (cents(text) === 0) return "The amount must be more than 0.";
    return null;
  }

  function fxProblem(text) {
    if (!text) return "Enter the exchange rate, for example 1.08.";
    if (!FX_RE.test(text)) return "Enter a rate like 1.08 (a dot or a comma, up to 6 decimals).";
    var rate = Number(text.replace(",", "."));
    if (rate < 0.5 || rate > 3) return "The rate must be between 0.5 and 3.0 USD per EUR.";
    return null;
  }

  function validate(f, v) {
    var problems = [];
    Object.keys(f.fields).forEach(function (name) {
      var fs = f.fields[name].spec;
      var msg = null;
      if (fs.kind === "amount") {
        msg = amountProblem(v.amount);
        if (!msg && f.remaining !== undefined && cents(v.amount) > Math.round(f.remaining * 100)) {
          msg = "At most " + usdExact.format(f.remaining) + " is left of this entry.";
        }
        if (msg) problems.push({ field: "amount", message: msg });
        if (fs.currency && v.currency === "EUR") {
          var fx = fxProblem(v.fx_rate);
          if (fx) problems.push({ field: "fx_rate", message: fx });
          else if (!msg && eurToUsdCents(v.amount, v.fx_rate) === 0) problems.push({ field: "amount", message: "The amount is too small." });
        }
        return;
      }
      if (fs.kind === "direction" && !v[name]) msg = fs.missing;
      else if (fs.kind === "day" && v[name]) {
        if (!DAY_RE.test(v[name])) msg = "Enter the day as YYYY-MM-DD.";
        else if (v[name] > todayIso()) msg = "The day can't be in the future.";
      } else if (fs.required && !v[name]) msg = fs.missing || "This field is required.";
      if (msg) problems.push({ field: name, message: msg });
    });
    return problems;
  }

  function buildBody(f, v) {
    var body = { amount: v.amount };
    if (f.fields.note) body.note = v.note;
    if (f.fields.source && v.source) body.source = v.source;
    if (f.fields.direction) body.direction = v.direction;
    if (f.fields.day && v.day) body.day = v.day;
    if (f.fields.amount.currency) {
      body.currency = v.currency;
      if (v.currency === "EUR") body.fx_rate = v.fx_rate.replace(",", ".");
    }
    if (f.fields.test_money && v.test_money !== null) body.test_money = v.test_money;
    if (f.fields.attribution && v.attribution) {
      var chosen = /^([pv]):(\d+)$/.exec(v.attribution);
      if (chosen) body[chosen[1] === "p" ? "project_id" : "venture_id"] = Number(chosen[2]);
    }
    body.idempotency_key = f.idKey;
    // The server checks the confirmation against the state it names, so it can't cover a worse outcome.
    if (f.flags.confirm_state_change) body.confirm_state_change = f.flags.confirm_state_change;
    if (f.flags.confirm_large) body.confirm_large = true;
    return body;
  }

  function setStatus(f, text, kind) {
    f.status.textContent = text;
    f.status.setAttribute("data-kind", kind || "");
  }

  function showFieldError(f, name, message) {
    var target = f.targets[name];
    if (!target) return false;
    target.error.textContent = message;
    target.error.hidden = false;
    var controls = name === "direction" ? f.fields.direction.radios : [target.control];
    controls.forEach(function (c) { c.setAttribute("aria-invalid", "true"); });
    return true;
  }

  function clearFieldError(f, name) {
    var target = f.targets[name];
    if (!target) return;
    // amount and currency share one message element.
    if (name === "currency" || name === "amount") ["amount", "currency"].forEach(function (n) { if (f.targets[n]) f.targets[n].control.removeAttribute("aria-invalid"); });
    target.error.textContent = "";
    target.error.hidden = true;
    var controls = name === "direction" ? f.fields.direction.radios : [target.control];
    controls.forEach(function (c) { c.removeAttribute("aria-invalid"); });
  }

  function clearErrors(f) { Object.keys(f.targets).forEach(function (n) { clearFieldError(f, n); }); }

  function focusField(f, name) {
    var target = f.targets[name];
    if (target) target.control.focus();
  }

  function focusFirst(f) {
    var first = f.form.querySelector("input:not([type=hidden]), select, textarea");
    if (first) first.focus();
  }

  function hideConfirm(f) {
    f.confirm.hidden = true;
    f.pendingFlag = null;
  }

  function showConfirm(f, flag, message, danger, value) {
    f.pendingFlag = flag;
    f.pendingValue = value === undefined ? true : value;
    f.confirmMsg.textContent = message;
    f.confirmYes.className = danger ? "btn btn-danger" : "btn";
    f.confirm.hidden = false;
    setStatus(f, "", "");
    f.confirmMsg.focus();
  }

  function onFormInput(f, ev) {
    var name = ev.target && ev.target.getAttribute ? ev.target.getAttribute("data-field") : null;
    if (name) clearFieldError(f, name);
    // A confirmation was for the values that were sent; any edit needs a new one.
    if (!f.confirm.hidden || f.flags.confirm_state_change || f.flags.confirm_large) {
      hideConfirm(f);
      f.flags = {};
    }
    if (f.status.getAttribute("data-kind") === "ok") setStatus(f, "", "");
    updatePreview(f);
  }

  // Shows the USD amount the ledger will store for a EUR entry.
  function updatePreview(f) {
    var field = f.fields.amount;
    if (!field || !field.currency) return;
    var eur = field.currency.value === "EUR";
    field.fxWrap.hidden = !eur;
    var amount = field.control.value.trim();
    var rate = field.fx.value.trim();
    if (!eur || amountProblem(amount) || fxProblem(rate)) {
      field.preview.hidden = true;
      field.preview.textContent = "";
      return;
    }
    field.preview.hidden = false;
    field.preview.textContent = "Recorded as " + usdLarge.format(eurToUsdCents(amount, rate) / 100) + ".";
  }

  // The USD cents the server stores for a EUR amount: amount × rate, rounded half-up to whole cents.
  function eurToUsdCents(amount, rate) {
    var m = /^([0-9])(?:[.,]([0-9]{1,6}))?$/.exec(rate);
    var rateMicros = Number(m[1]) * 1e6 + Number(((m[2] || "") + "000000").slice(0, 6));
    var product = cents(amount) * rateMicros; // cents × 1e6, exact below 2^53
    return Math.floor(product / 1e6) + (product % 1e6 >= 500000 ? 1 : 0);
  }

  function submitForm(f) {
    if (f.busy) return;
    clearErrors(f);
    hideConfirm(f);
    var v = readValues(f);
    var problems = validate(f, v);
    if (problems.length) {
      problems.forEach(function (p) { showFieldError(f, p.field, p.message); });
      setStatus(f, problems.length === 1 ? "Please fix the marked field." : "Please fix the marked fields.", "error");
      focusField(f, problems[0].field);
      return;
    }
    var body = buildBody(f, v);
    var hadFocus = document.activeElement === f.submit;
    f.busy = true;
    f.submit.disabled = true;
    f.submit.textContent = "Saving…";
    setStatus(f, "Saving…", "");
    request("POST", f.endpoint(), body).then(function (res) {
      handleFormResponse(f, res);
    }).catch(function (err) {
      handleFormFailure(f, err);
    }).then(function () {
      f.busy = false;
      f.submit.disabled = false;
      f.submit.textContent = f.spec.submitLabel || f.spec.title;
      // Disabling the focused button sent focus to <body>; return it unless something else took it.
      if (hadFocus && !f.form.hidden && (!document.activeElement || document.activeElement === document.body)) f.submit.focus();
    });
  }

  function stateChangeMessage(data) {
    var name = agentName();
    var after = usd(data.balance_after_usd);
    // In dry run, real money also counts for the sleeping live agent, which the server checks too.
    var live = data.mode === "live" && ui.data && ui.data.mode === "dry_run";
    var who = live ? "the live " + name : name;
    var real = live ? "This is real money, not test money. " : "";
    if (data.state_after === "dead") {
      return real + "This would kill " + who + ": the balance afterwards (" + after + ") is too little to stay alive. Record it anyway?";
    }
    return real + "This changes " + who + "'s state from " + stateLabel(data.state_before) + " to " +
      stateLabel(data.state_after) + " (balance afterwards: " + after + "). Record it anyway?";
  }

  function handleFormResponse(f, res) {
    var data = isObject(res.data) ? res.data : {};
    if (res.status === 201 || res.status === 200) {
      onSaved(f, data.entry, res.status === 200 || data.replay === true);
    } else if (res.status === 409 && data.code === "would_change_state") {
      showConfirm(f, "confirm_state_change", stateChangeMessage(data), data.state_after === "dead", String(data.state_after || ""));
    } else if (res.status === 409 && data.code === "unusually_large") {
      var typical = isNaN(num(data.typical_usd)) ? "" : ": typical entries are about " + usd(data.typical_usd);
      showConfirm(f, "confirm_large", (isNaN(num(data.amount_usd)) ? "This amount" : usd(data.amount_usd)) + " is much more than usual" + typical +
        ". Check the amount. Record it anyway?", false);
    } else if (res.status === 409 && data.code === "already_recorded") {
      // 0.12.0: Ember's code recorded this Etsy order from Etsy's numbers first; the same key keeps it that way
      setStatus(f, (typeof data.error === "string" ? data.error : "Ember's code already recorded this") + ". Nothing new was recorded.", "error");
    } else if (res.status === 409 && data.code === "duplicate_key_mismatch") {
      f.idKey = newKey();
      f.unresolved = false;
      f.flags = {};
      setStatus(f, "An earlier attempt of this form already reached Ember with different values and was recorded. Check the ledger below before you submit again; the next submission is recorded as a new entry.", "error");
    } else if (res.status === 422) {
      var msg = typeof data.error === "string" && data.error ? data.error : "Ember did not accept this entry.";
      if (typeof data.field === "string" && showFieldError(f, data.field, msg)) {
        setStatus(f, "Please fix the marked field.", "error");
        focusField(f, data.field);
      } else {
        setStatus(f, msg, "error");
      }
    } else if (res.status === 503) {
      setStatus(f, "Ember can't record money right now (" + httpError(res).message + "). Nothing was saved.", "error");
    } else if (res.status === 404 && f === ui.correction) {
      setStatus(f, "This entry can't be corrected: it was not found.", "error");
      refresh();
    } else if (res.status >= 500) {
      f.unresolved = true;
      setStatus(f, "Ember had an error (" + httpError(res).message + "). It may or may not have been saved. Submitting again is safe: it won't be recorded twice.", "error");
    } else {
      setStatus(f, "Ember refused this (" + httpError(res).message + ").", "error");
    }
  }

  function handleFormFailure(f, err) {
    if (!(err instanceof RequestError)) {
      console.error(err);
      setStatus(f, "Something went wrong in the dashboard (" + errorText(err) + ").", "error");
      return;
    }
    // Same key on the next try: if the first request did arrive, Ember returns that entry instead of a second one.
    f.unresolved = true;
    setStatus(f, (err.kind === "timeout" ? "Ember did not answer in time" : "Couldn't reach Ember") +
      ", so it's not clear whether this was saved. Submit again to retry: it's safe, it won't be recorded twice.", "error");
  }

  function describeEntry(entry) {
    if (!isObject(entry)) return "the entry";
    var t = LEDGER_TYPES[entry.type] || { label: String(entry.type), sign: 1 };
    return signedUsd(num(entry.amount_usd) * t.sign) + " " + t.label.toLowerCase() + (entry.simulated ? " (test money)" : "") +
      (entry.id !== undefined ? " as entry #" + entry.id : "");
  }

  function resetValues(f) {
    Object.keys(f.fields).forEach(function (name) {
      var field = f.fields[name];
      var kind = field.spec.kind;
      if (kind === "amount" || kind === "note" || kind === "text" || kind === "attribution") field.control.value = "";
      if (kind === "direction") field.radios.forEach(function (r) { r.checked = false; });
    });
    updatePreview(f);
  }

  function onSaved(f, entry, replay) {
    f.idKey = newKey();
    f.unresolved = false;
    f.flags = {};
    var text = (replay ? "Already recorded (this was a retry): " : "Recorded: ") + describeEntry(entry) + ".";
    resetValues(f);
    if (f.onSaved) f.onSaved(text);
    else setStatus(f, text, "ok");
    refresh();
  }

  function openForm(key, focus, amount) {
    Object.keys(FORMS).forEach(function (k) {
      var f = ui.forms[k];
      var open = k === key;
      // One key per opening of a form; a submission whose outcome is unknown keeps its key for the retry.
      if (open && f.form.hidden && !f.unresolved) f.idKey = newKey();
      f.form.hidden = !open;
      var toggle = document.querySelector("[data-form-toggle='" + k + "']");
      if (toggle) toggle.setAttribute("aria-expanded", String(open));
    });
    var f = ui.forms[key];
    if (!f) return;
    if (f.intro) f.intro.textContent = introText(f.spec);  // the agent's name may have loaded since
    if (f.fields.attribution) fillAttribution(f.fields.attribution, f.fields.attribution.control.value);
    if (amount) {
      f.fields.amount.control.value = amount;
      if (f.fields.amount.currency) f.fields.amount.currency.value = "USD";
      clearFieldError(f, "amount");
      updatePreview(f);
    }
    if (focus) focusFirst(f);
  }

  function closeForm(f) {
    f.form.hidden = true;
    hideConfirm(f);
    f.flags = {};
    var toggle = document.querySelector("[data-form-toggle='" + f.key + "']");
    if (toggle) { toggle.setAttribute("aria-expanded", "false"); toggle.focus(); }
    if (f === ui.correction) $("ledger-title").focus();
  }

  function openLedgerForm(key, amount, prefill) {
    selectTab("ledger", false);
    openForm(key, true, amount);
    var f = ui.forms[key];
    if (!f || !prefill) return;
    // An Etsy order: its currency, source and day, and a key of its own, so it can't be recorded twice.
    if (prefill.currency && f.fields.amount && f.fields.amount.currency) {
      f.fields.amount.currency.value = prefill.currency;
      f.fields.amount.currency.dispatchEvent(new Event("change", { bubbles: true }));
    }
    if (prefill.source && f.fields.source) f.fields.source.control.value = prefill.source;
    if (prefill.note && f.fields.note) f.fields.note.control.value = prefill.note;
    if (prefill.day && f.fields.day) f.fields.day.control.value = prefill.day;
    if (prefill.testMoney && f.fields.test_money && !f.fields.test_money.wrap.hidden) f.fields.test_money.control.checked = true;
    if (prefill.idKey) f.idKey = prefill.idKey;
    if (f.fields.attribution && (prefill.projectId || prefill.ventureId)) {
      fillAttribution(f.fields.attribution, prefill.projectId ? "p:" + prefill.projectId : "v:" + prefill.ventureId);
    }
    updatePreview(f);
  }

  function openCorrection(entry) {
    var f = ui.correction;
    if (!entry) return;
    var sameEntry = f.entry && String(f.entry.id) === String(entry.id);
    if (f.form.hidden || !sameEntry) {
      if (!(sameEntry && f.unresolved)) {
        f.idKey = newKey();
        f.unresolved = false;
        resetValues(f);
        clearErrors(f);
        setStatus(f, "", "");
      }
    }
    f.entry = entry;
    f.remaining = correctionRemaining(entry);
    var t = LEDGER_TYPES[entry.type] || { label: String(entry.type), sign: 1 };
    f.lead.hidden = false;
    f.lead.textContent = "Entry #" + entry.id + ": " + t.label + " " + signedUsd(num(entry.amount_usd) * t.sign) +
      (entry.occurred_on ? " on " + fmtDay(entry.occurred_on) : "") + ". Up to " + usdExact.format(f.remaining) + " can be taken back.";
    hideConfirm(f);
    f.flags = {};
    f.form.hidden = false;
    focusFirst(f);
  }

  function updateForms(d, agent) {
    var dry = isDryRun(d);
    var today = todayIso();
    var all = Object.keys(ui.forms).map(function (k) { return ui.forms[k]; }).concat([ui.correction]);
    all.forEach(function (f) {
      if (f.fields.test_money) f.fields.test_money.wrap.hidden = !dry;
      if (f.fields.day) f.fields.day.control.setAttribute("max", today);
    });
    var grant = ui.forms.grant;
    var revive = isObject(agent.revive) ? agent.revive : null;
    grant.lead.hidden = !(agent.state === "dead" && revive);
    if (agent.state === "dead" && revive) {
      grant.lead.textContent = (agent.name || "Ember") + " has died. Grant at least " + usd(amountText(revive.needed_usd) || revive.needed_usd) + " to revive it.";
    }
  }

  function initForms() {
    var holder = $("ledger-forms");
    Object.keys(FORMS).forEach(function (key) {
      var f = buildForm(key, FORMS[key]);
      ui.forms[key] = f;
      append(holder, f.form);
    });
    var c = buildForm("correction", CORRECTION_FORM);
    c.endpoint = function () { return "api/ledger/" + encodeURIComponent(String(c.entry.id)) + "/correct"; };
    c.onSaved = function (text) {
      c.form.hidden = true;
      $("ledger-status").textContent = text;
      $("ledger-title").focus();
    };
    ui.correction = c;
    append($("correction-form-slot"), c.form);

    Array.prototype.forEach.call(document.querySelectorAll("[data-form-toggle]"), function (btn) {
      btn.addEventListener("click", function () {
        var key = btn.getAttribute("data-form-toggle");
        if (btn.getAttribute("aria-expanded") === "true") closeForm(ui.forms[key]);
        else openForm(key, true);
      });
    });
    Array.prototype.forEach.call(document.querySelectorAll("[data-open-form]"), function (btn) {
      btn.addEventListener("click", function () { openLedgerForm(btn.getAttribute("data-open-form")); });
    });
    $("ledger-list").addEventListener("click", function (ev) {
      var btn = ev.target.closest ? ev.target.closest("[data-correct]") : null;
      if (!btn) return;
      $("ledger-status").textContent = "";
      openCorrection(ui.ledgerById[btn.getAttribute("data-correct")]);
    });
    $("memorial-grant").addEventListener("click", function () { openLedgerForm("grant"); });
    $("memorial-suggested").addEventListener("click", function () { openLedgerForm("grant", this.getAttribute("data-amount")); });
  }

  // ------------------------------------------------------------------ controls (pause / resume)

  function setControlStatus(text, isError) {
    window.clearTimeout(ui.controlTimer);
    var el = $("control-status");
    el.textContent = text;
    el.setAttribute("data-kind", isError ? "error" : "");
    if (text && !isError) ui.controlTimer = window.setTimeout(function () { el.textContent = ""; }, 6000);
  }

  $("pause-button").addEventListener("click", function () {
    if (!ui.data || !ui.data.agent || ui.controlBusy) return;
    var btn = this;
    var resume = !!ui.data.agent.paused;
    var hadFocus = document.activeElement === btn;
    ui.controlBusy = true;
    btn.disabled = true;
    setControlStatus(resume ? "Resuming…" : "Pausing…", false);
    request("POST", resume ? "api/control/resume" : "api/control/pause", {}).then(function (res) {
      if (!res.ok) throw httpError(res);
      var paused = isObject(res.data) && typeof res.data.paused === "boolean" ? res.data.paused : !resume;
      btn.textContent = paused ? "Resume" : "Pause";
      if (resume && isObject(res.data) && res.data.state === "killed") {
        setControlStatus("The pause is lifted, but the kill switch is still on, so " + agentName() + " stays stopped. The banner says how to reset it.", true);
      } else {
        setControlStatus(resume ? "Resumed." : "Paused. No model calls are made until you resume.", false);
      }
    }).catch(function (err) {
      setControlStatus((resume ? "Could not resume: " : "Could not pause: ") + errorText(err) + ".", true);
    }).then(function () {
      ui.controlBusy = false;
      btn.disabled = false;
      if (hadFocus && (!document.activeElement || document.activeElement === document.body)) btn.focus();
      refresh();
    });
  });

  // "Wake now": the server queues a cycle (202) or says why not (409, 429).
  $("wake-button").addEventListener("click", function () {
    if (!ui.data || !ui.data.agent || ui.wakeBusy) return;
    var btn = this;
    var hadFocus = document.activeElement === btn;
    ui.wakeBusy = true;
    btn.disabled = true;
    setControlStatus("Asking " + agentName() + " to wake up…", false);
    request("POST", "api/control/wake", {}).then(function (res) {
      var data = isObject(res.data) ? res.data : {};
      if (res.status === 202 || (res.ok && data.queued === true)) {
        ui.fastPollUntil = Date.now() + WAKE_FAST_POLL_MS;
        setControlStatus("Waking up…", false);
        return;
      }
      if ((res.status === 409 || res.status === 429) && typeof data.error === "string" && data.error) {
        setControlStatus(endSentence(sentence(data.error)), true);
        return;
      }
      if (res.status === 429) { setControlStatus("Too soon after the last wake. Try again in a little while.", true); return; }
      if (res.status === 409) { setControlStatus(agentName() + " can't be woken right now.", true); return; }
      throw httpError(res);
    }).catch(function (err) {
      if (!(err instanceof RequestError)) console.error(err);
      setControlStatus("Could not wake " + agentName() + ": " + errorText(err) + ".", true);
    }).then(function () {
      ui.wakeBusy = false;
      if (ui.data) safely("controls", function () { renderControls(ui.data.agent || standInAgent(ui.data)); });
      if (hadFocus && !btn.disabled && (!document.activeElement || document.activeElement === document.body)) btn.focus();
      refresh();
    });
  });

  // ------------------------------------------------------------------ kill switch
  // A modal dialog: the owner types the agent's name exactly before the confirm button works.

  function killFieldError(id, text) {
    var err = $(id + "-error");
    err.textContent = text || "";
    err.hidden = !text;
    if (text) $(id).setAttribute("aria-invalid", "true");
    else $(id).removeAttribute("aria-invalid");
  }

  function setKillStatus(text, kind) {
    $("kill-status").textContent = text;
    $("kill-status").setAttribute("data-kind", kind || "");
  }

  function syncKillConfirm() {
    $("kill-confirm").disabled = ui.killBusy || $("kill-name").value.trim() !== agentName();
  }

  function openKillDialog() {
    var dialog = $("kill-dialog");
    var name = agentName();
    $("kill-name-show").textContent = name;
    $("kill-title").textContent = "Stop " + name + " with the kill switch?";
    $("kill-desc-1").textContent = "The kill switch stops " + name + " for good: no more model calls, and a running cycle ends at its next call. Unlike Pause, Resume doesn't undo it.";
    $("kill-name").value = "";
    $("kill-reason").value = "";
    killFieldError("kill-name", "");
    killFieldError("kill-reason", "");
    setKillStatus("", "");
    syncKillConfirm();
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    $("kill-name").focus();
  }

  function closeKillDialog() {
    var dialog = $("kill-dialog");
    if (dialog.open && typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  }

  $("kill-button").addEventListener("click", function () {
    if (!ui.data || !ui.data.agent || this.disabled) return;
    openKillDialog();
  });
  $("kill-name").addEventListener("input", function () { killFieldError("kill-name", ""); syncKillConfirm(); });
  $("kill-reason").addEventListener("input", function () { killFieldError("kill-reason", ""); });
  $("kill-cancel").addEventListener("click", closeKillDialog);
  // Escape (the dialog's cancel event) and Cancel both return focus to the kill switch.
  $("kill-dialog").addEventListener("close", function () {
    var btn = $("kill-button");
    if (!btn.disabled) btn.focus();
    else $("main").focus();
  });
  $("kill-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    if (ui.killBusy) return;
    var name = $("kill-name").value.trim();
    var reason = $("kill-reason").value.trim();
    if (name !== agentName()) { killFieldError("kill-name", "Type the agent's name exactly: " + agentName() + "."); $("kill-name").focus(); return; }
    if (reason.length > 300) { killFieldError("kill-reason", "Keep the reason under 300 characters (it has " + intFmt.format(reason.length) + ")."); $("kill-reason").focus(); return; }
    ui.killBusy = true;
    syncKillConfirm();
    setKillStatus("Stopping…", "");
    var body = { confirm_name: name };
    if (reason) body.reason = reason;
    request("POST", "api/control/kill", body).then(function (res) {
      if (res.ok) {
        closeKillDialog();
        setControlStatus(agentName() + " was stopped with the kill switch.", false);
        refresh();
        return;
      }
      var data = isObject(res.data) ? res.data : {};
      if (res.status === 422 && (data.field === "confirm_name" || data.field === "reason") && typeof data.error === "string") {
        killFieldError(data.field === "reason" ? "kill-reason" : "kill-name", endSentence(sentence(data.error)));
        setKillStatus("Nothing was changed.", "error");
        $(data.field === "reason" ? "kill-reason" : "kill-name").focus();
        return;
      }
      ownerFailure(res, {}, function (msg) { setKillStatus(msg, "error"); }, null);
    }).catch(function (err) {
      if (!(err instanceof RequestError)) console.error(err);
      setKillStatus((err.kind === "timeout" ? "Ember did not answer in time" : "Couldn't reach Ember") +
        ", so it's not clear whether the kill switch is on. Check the header after the next update.", "error");
      refresh();
    }).then(function () {
      ui.killBusy = false;
      syncKillConfirm();
    });
  });

  // ------------------------------------------------------------------ inbox composer

  $("composer").addEventListener("submit", function (ev) { ev.preventDefault(); sendMessage(); });
  $("composer-text").addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); sendMessage(); }
  });
  $("composer-text").addEventListener("input", function () {
    composerError("");
    composerCount();
    if ($("composer-status").getAttribute("data-kind") === "ok") setComposerStatus("", "");
  });
  $("inbox-mark-read").addEventListener("click", markAllRead);

  // ------------------------------------------------------------------ standing instructions

  $("instructions-edit").addEventListener("click", openInstructions);
  $("instructions-cancel").addEventListener("click", function () { setInstructionsStatus("", ""); closeInstructions(); });
  $("instructions-form").addEventListener("submit", function (ev) { ev.preventDefault(); saveInstructions(); });
  $("instructions-text").addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); saveInstructions(); }
  });
  $("instructions-text").addEventListener("input", function () {
    instructionsError("");
    instructionsCount();
  });

  // ------------------------------------------------------------------ diagnostics

  function setDiagStatus(text, kind) {
    var el = $("diag-status");
    el.textContent = text;
    el.setAttribute("data-kind", kind || "");
  }

  function diagnosticsFileName(when, full) {
    var d = when || new Date();
    var two = function (n) { return String(n).padStart(2, "0"); };
    return "ember-diagnostics-" + d.getFullYear() + "-" + two(d.getMonth() + 1) + "-" + two(d.getDate()) +
      "-" + two(d.getHours()) + two(d.getMinutes()) + two(d.getSeconds()) + (full ? "-private" : "") + ".txt";
  }

  function reportBytes(text) {
    return typeof Blob === "function" ? new Blob([text]).size : text.length;
  }

  function showDiagnostics() {
    var diag = ui.diag;
    var has = typeof diag.text === "string";
    $("diag-report").hidden = !has;
    $("diag-meta").hidden = !has;
    $("diag-copy").disabled = !has;
    $("diag-download").disabled = !has;
    $("diag-load").textContent = diag.busy ? "Loading…" : has ? "Reload report" : "Load report";
    if (!has) return;
    var report = $("diag-report");
    var stamp = String(diag.loadedAt.getTime());
    if (report.getAttribute("data-loaded") !== stamp) {  // keeps scroll and selection while a reload is in flight
      report.textContent = diag.text;
      report.setAttribute("data-loaded", stamp);
    }
    var lines = diag.text.replace(/\n$/, "").split("\n").length;
    replace($("diag-meta"), [(diag.full ? "Private (with other people's text)" : "Shareable") + " · " +
      byteSize(reportBytes(diag.text)) + " · " + plural(lines, "line") + " · ",
      timeEl(diag.loadedAt.toISOString(), "loaded at " + timeFmt.format(diag.loadedAt))]);
  }

  function loadDiagnostics() {
    var diag = ui.diag;
    if (diag.busy) return;
    diag.busy = true;
    setDiagStatus("Loading the report…", "");
    safely("diagnostics", showDiagnostics);
    // Shareable unless the owner asks for other people's text too (0.11.2).
    var full = $("diag-full").checked;
    request("GET", "api/diagnostics" + (full ? "?full=1" : ""), null, { accept: "text/plain", timeout: DIAGNOSTICS_TIMEOUT_MS }).then(function (res) {
      if (!res.ok) throw httpError(res);
      if (typeof res.text !== "string" || !res.text.trim()) throw new RequestError("malformed", "the report is empty");
      diag.text = res.text;
      diag.full = full;
      diag.loadedAt = new Date();
      setDiagStatus("", "");
    }).catch(function (err) {
      if (!(err instanceof RequestError)) console.error(err);
      setDiagStatus("Couldn't load the report (" + errorText(err) + ")." + (diag.text ? " The report below is the one loaded earlier." : " Try again."), "error");
    }).then(function () {
      diag.busy = false;
      safely("diagnostics", showDiagnostics);
      safely("banners", renderBanners);
    });
  }

  // Copies text to the clipboard. The Clipboard API needs a secure context, and Home Assistant is often opened over
  // plain http: then the element showing the text is selected and copied with execCommand. If that fails too,
  // fail(true) leaves the text selected for Ctrl+C; fail(false) means nothing could be selected.
  function copyText(text, el, done, fail) {
    if (window.isSecureContext && navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      navigator.clipboard.writeText(text).then(done, function () { copyBySelection(el, done, fail); });
    } else {
      copyBySelection(el, done, fail);
    }
  }

  function copyBySelection(el, done, fail) {
    var sel = window.getSelection ? window.getSelection() : null;
    if (!el || !sel || !document.createRange) { fail(false); return; }
    var range = document.createRange();
    range.selectNodeContents(el);
    sel.removeAllRanges();
    sel.addRange(range);
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    if (ok) {
      sel.removeAllRanges();
      done();
    } else {
      fail(true);
    }
  }

  function copyDiagnostics() {
    var text = ui.diag.text;
    if (typeof text !== "string") return;
    var size = byteSize(reportBytes(text));
    copyText(text, $("diag-report"), function () { setDiagStatus("Copied the report (" + size + ") to the clipboard.", "ok"); }, function (selected) {
      setDiagStatus(selected ? "The browser didn't allow copying. The whole report is selected: press Ctrl+C (Cmd+C on a Mac) to copy it."
        : "This browser can't copy for you. Select the report below and copy it with Ctrl+C (Cmd+C on a Mac).", "error");
    });
  }

  function downloadDiagnostics() {
    var text = ui.diag.text;
    if (typeof text !== "string") return;
    var name = diagnosticsFileName(ui.diag.loadedAt, ui.diag.full);
    var url = window.URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
    var link = h("a", { href: url, download: name, hidden: true });
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    window.setTimeout(function () { window.URL.revokeObjectURL(url); }, 60000);
    setDiagStatus("Saved as " + name + " (check your downloads).", "ok");
  }

  $("diag-load").addEventListener("click", loadDiagnostics);
  $("diag-copy").addEventListener("click", copyDiagnostics);
  $("diag-download").addEventListener("click", downloadDiagnostics);

  // ------------------------------------------------------------------ workspace
  // The files the agent wrote. Their text is only ever shown with textContent, never rendered (an .html or .svg
  // file stays text), and the server sends them as downloads, so opening the URL itself never renders them either.
  // Products (PDF, Word, Excel, PNG) are made by Ember's code from the agent's text: they are shown as the PNG
  // pictures made with them and downloaded as files, never opened in the dashboard.

  var WS_MONO = /\.(csv|tsv|json|ya?ml|xml|html|css)$/i;
  var WS_KINDS = { pdf: "PDF", docx: "Word", xlsx: "Excel", pptx: "PowerPoint", png: "Picture", jpg: "Picture" };

  function wsType(path) {
    var match = /\.([a-z]+)$/i.exec(String(path));
    return match ? match[1].toLowerCase() : "";
  }

  function wsProductUrl(path, inline) {
    return "api/workspace/product?path=" + encodeURIComponent(path) + (inline ? "&inline=1" : "");
  }

  // The pictures that show a product: a PNG itself, a document's page pictures, a spreadsheet's table.
  function wsPictures(path) {
    var type = wsType(path);
    if (type === "png" || type === "jpg") return [path];
    var base = path.replace(/\.[a-z]+$/i, "");
    var wanted = type === "xlsx" ? [base + "-preview.png"] : [1, 2, 3, 4].map(function (n) { return base + "-page" + n + ".png"; });
    return wanted.filter(function (p) { return wsFileInfo(p) !== null; });
  }

  function baseName(path) {
    var parts = String(path).split("/");
    return parts[parts.length - 1] || "file.txt";
  }

  function setStatusText(id, text, kind) {
    var el = $(id);
    // A live region: only touch it when the text changes, or screen readers repeat it.
    if (el.textContent !== text) el.textContent = text;
    el.setAttribute("data-kind", kind || "");
  }

  function wsFileInfo(path) {
    var files = ui.ws.list ? arr(ui.ws.list.files) : [];
    for (var i = 0; i < files.length; i++) if (isObject(files[i]) && files[i].path === path) return files[i];
    return null;
  }

  function refreshWorkspace() {
    loadWorkspace();
    if (ui.ws.file) loadWorkspaceFile();
  }

  function loadWorkspace() {
    var ws = ui.ws;
    if (ws.busy) return;
    ws.busy = true;
    safely("workspace", renderWorkspace);
    request("GET", "api/workspace").then(function (res) {
      if (!res.ok) throw httpError(res);
      if (!isObject(res.data) || !Array.isArray(res.data.files)) throw new RequestError("malformed", res.data === undefined ? "not JSON" : "the file list is missing");
      ws.list = res.data;
      ws.loadedAt = new Date();
      ws.error = null;
      // A file from the other mode's folder (dry run was switched) is not in this workspace.
      if (ws.file && ws.file.mode !== ws.list.mode) closeWorkspaceFile();
    }).catch(function (err) {
      if (!(err instanceof RequestError)) console.error(err);
      ws.error = err;
    }).then(function () {
      ws.busy = false;
      safely("workspace", renderWorkspace);
      safely("banners", renderBanners);
    });
  }

  function renderWorkspace() {
    var ws = ui.ws;
    var list = ws.list;
    $("ws-refresh").textContent = ws.busy ? "Refreshing…" : "Refresh";
    var dry = list ? list.mode === "dry_run" : isDryRun(ui.data);
    $("ws-sub").textContent = "Drafts, notes and research the agent wrote in its own folder, and the PDF, Word, Excel and " +
      "picture files Ember made from them. Read-only." +
      (dry ? " Dry run: this is the dry-run folder, which starts empty with every dry-run session." : "");
    setStatusText("ws-status", ws.error && !ws.busy ? "Couldn't load the file list (" + errorText(ws.error) + ")." +
      (list ? " The list below is the one loaded earlier." : " Try Refresh.") : "", ws.error && !ws.busy ? "error" : "");
    var summary = $("ws-summary");
    summary.hidden = !list;
    var el = $("ws-list");
    if (!list) {
      replace(el, ws.busy ? h("p", { class: "muted", text: "Loading the file list…" }) : []);
      el.removeAttribute("data-key");
      return;
    }
    var files = arr(list.files).filter(function (f) { return isObject(f) && typeof f.path === "string" && f.path; });
    var parts = [plural(num(list.file_count) >= 0 ? list.file_count : files.length, "file"), byteSize(num(list.total_bytes) || 0)];
    if (list.truncated) parts.push("only the first " + count(files.length) + " are listed");
    parts.push(ws.busy ? "refreshing…" : "loaded at " + timeFmt.format(ws.loadedAt));
    summary.textContent = parts.join(" · ");
    var open = ws.file ? ws.file.path : null;
    var key = JSON.stringify([files, open, dry]);
    if (el.getAttribute("data-key") === key) return;
    el.setAttribute("data-key", key);
    // Keep keyboard focus on the same file when the list is rebuilt under it.
    var focused = el.contains(document.activeElement) ? document.activeElement.getAttribute("data-path") : null;
    if (!files.length) {
      replace(el, emptyState("div", "No files yet.", "Drafts, notes and research show up here once the agent writes them." +
        (dry ? " In dry run the folder starts empty with every session." : "")));
      return;
    }
    replace(el, h("table", { class: "ws-files" },
      h("caption", { class: "visually-hidden", text: "Files in the agent's workspace, by path" }),
      h("thead", null, h("tr", null, h("th", { scope: "col", text: "File" }), h("th", { scope: "col", class: "num", text: "Size" }),
        h("th", { scope: "col", text: "Modified" }))),
      h("tbody", null, files.map(function (f) {
        var slash = f.path.lastIndexOf("/");
        var current = f.path === open;
        return h("tr", { "data-open": current ? "true" : null },
          h("td", { class: "ws-path" }, h("button", { type: "button", class: "ws-open", "data-path": f.path, "aria-current": current ? "true" : null },
            slash >= 0 ? h("span", { class: "ws-dir", text: f.path.slice(0, slash + 1) }) : null,
            h("span", { class: "ws-name", text: f.path.slice(slash + 1) })),
            f.kind === "product" && WS_KINDS[wsType(f.path)] ? h("span", { class: "chip ws-kind", text: WS_KINDS[wsType(f.path)] }) : null),
          h("td", { class: "num", text: byteSize(num(f.size) || 0) }),
          h("td", { class: "ws-when" }, f.modified_at ? timeEl(f.modified_at, fmtDateTime(f.modified_at)) : "–"));
      }))));
    if (focused !== null) {
      Array.prototype.forEach.call(el.querySelectorAll("button[data-path]"), function (b) { if (b.getAttribute("data-path") === focused) b.focus(); });
    }
  }

  function openWorkspaceFile(path) {
    var ws = ui.ws;
    if (!ws.file || ws.file.path !== path) {
      var info = wsFileInfo(path);
      ws.file = { path: path, mode: ws.list ? ws.list.mode : null, text: null, loadedAt: null,
        product: !!(info && info.kind === "product") };
    }
    loadWorkspaceFile();
    safely("workspace", renderWorkspace);
    $("ws-file-title").focus();
  }

  function closeWorkspaceFile() {
    var ws = ui.ws;
    ws.file = null;
    ws.fileSeq++;  // an answer still on its way is dropped
    ws.fileBusy = false;
    ws.fileError = null;
    safely("workspaceFile", renderWorkspaceFile);
  }

  function loadWorkspaceFile() {
    var ws = ui.ws;
    var file = ws.file;
    if (!file) return;
    if (file.product) {  // nothing to load: its pictures load as images, the file itself only as a download
      ws.fileSeq++;
      ws.fileBusy = false;
      ws.fileError = null;
      safely("workspaceFile", renderWorkspaceFile);
      return;
    }
    var seq = ++ws.fileSeq;  // only the latest request counts (the owner may open another file meanwhile)
    ws.fileBusy = true;
    ws.fileError = null;
    safely("workspaceFile", renderWorkspaceFile);
    request("GET", "api/workspace/file?path=" + encodeURIComponent(file.path), null, { accept: "text/plain" }).then(function (res) {
      if (seq !== ws.fileSeq) return;
      if (!res.ok) throw httpError(res);
      if (typeof res.text !== "string") throw new RequestError("malformed", "no text");
      file.text = res.text;
      file.loadedAt = new Date();
    }).catch(function (err) {
      if (seq !== ws.fileSeq) return;
      if (!(err instanceof RequestError)) console.error(err);
      ws.fileError = err;
    }).then(function () {
      if (seq !== ws.fileSeq) return;
      ws.fileBusy = false;
      safely("workspaceFile", renderWorkspaceFile);
      safely("banners", renderBanners);
    });
  }

  function renderWorkspaceFile() {
    var ws = ui.ws;
    var file = ws.file;
    $("ws-viewer").hidden = !file;
    if (!file) return;
    var has = typeof file.text === "string";
    var info = wsFileInfo(file.path);
    $("ws-file-title").textContent = file.path;
    var meta = [];
    if (file.product && WS_KINDS[wsType(file.path)]) meta.push(WS_KINDS[wsType(file.path)]);
    if (info) meta.push(byteSize(num(info.size) || 0), "modified " + fmtDateTime(info.modified_at));
    if (has) meta.push(ws.fileBusy ? "reloading…" : "loaded at " + timeFmt.format(file.loadedAt));
    $("ws-file-meta").textContent = meta.join(" · ");
    $("ws-file-note").textContent = file.product
      ? "Made by Ember's code from the agent's text. Check it before you use it or sell it."
      : "Written by the agent. Check it before you use it.";
    renderWorkspaceProduct(file, info);
    if (file.product) {
      $("ws-download").disabled = !info;
      $("ws-text").hidden = true;
      setStatusText("ws-file-status", info ? "" : "This file is no longer in the workspace.", info ? "" : "error");
      return;
    }
    $("ws-download").disabled = !has;
    var status = "";
    if (ws.fileError && !ws.fileBusy) status = "Couldn't open the file (" + errorText(ws.fileError) + ")." + (has ? " The text below is the one loaded earlier." : "");
    else if (!has && ws.fileBusy) status = "Loading the file…";
    else if (has && !file.text) status = "The file is empty.";
    setStatusText("ws-file-status", status, ws.fileError && !ws.fileBusy ? "error" : "");
    var pre = $("ws-text");
    pre.hidden = !has || !file.text;
    if (!has) return;
    pre.className = "ws-text" + (WS_MONO.test(file.path) ? " mono" : "");
    // Unchanged text keeps its scroll position and any selection.
    if (pre.getAttribute("data-path") !== file.path) {
      pre.textContent = file.text;
      pre.setAttribute("data-path", file.path);
      pre.scrollTop = 0;
    } else if (pre.textContent !== file.text) {
      pre.textContent = file.text;
    }
  }

  function renderWorkspaceProduct(file, info) {
    var el = $("ws-product");
    var pictures = file.product && info ? wsPictures(file.path) : [];
    el.hidden = !pictures.length;
    var key = JSON.stringify(pictures.map(function (p) { var i = wsFileInfo(p); return [p, i ? i.modified_at : null]; }));
    if (el.getAttribute("data-key") === key) return;
    el.setAttribute("data-key", key);
    el.className = "ws-product" + (pictures.length === 1 ? " single" : "");
    replace(el, pictures.map(function (p, index) {
      var i = wsFileInfo(p);
      var label = p === file.path ? baseName(p) : wsType(file.path) === "xlsx" ? "The first sheet of " + baseName(file.path)
        : "Page " + (index + 1) + " of " + baseName(file.path);
      // The modification time makes a remade picture load again instead of an old copy.
      var src = wsProductUrl(p, true) + "&v=" + encodeURIComponent(i ? i.modified_at : "");
      return h("figure", { class: "ws-figure" }, h("img", { class: "ws-image", src: src, alt: label, loading: "lazy" }),
        pictures.length > 1 ? h("figcaption", { class: "muted small", text: label }) : null);
    }));
  }

  // Text is saved from the text already loaded (like the diagnostics report), never by opening the file's URL;
  // a product is downloaded from the server, which always sends it as an attachment.
  function downloadWorkspaceFile() {
    var file = ui.ws.file;
    if (file && file.product) {
      var link = h("a", { href: wsProductUrl(file.path, false), download: baseName(file.path), hidden: true });
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      setStatusText("ws-file-status", "Downloading " + baseName(file.path) + " (check your downloads).", "ok");
      return;
    }
    if (!file || typeof file.text !== "string") return;
    var name = baseName(file.path);
    var url = window.URL.createObjectURL(new Blob([file.text], { type: "text/plain;charset=utf-8" }));
    var link = h("a", { href: url, download: name, hidden: true });
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    window.setTimeout(function () { window.URL.revokeObjectURL(url); }, 60000);
    setStatusText("ws-file-status", "Saved as " + name + " (check your downloads).", "ok");
  }

  $("ws-refresh").addEventListener("click", refreshWorkspace);
  $("ws-download").addEventListener("click", downloadWorkspaceFile);
  $("ws-list").addEventListener("click", function (ev) {
    var btn = ev.target.closest ? ev.target.closest("button[data-path]") : null;
    if (btn) openWorkspaceFile(btn.getAttribute("data-path"));
  });

  // ------------------------------------------------------------------ ventures (0.10.0)
  // The venture tree: every idea Ember or the owner had, branching from the one it grew out of, weighted by its
  // scores. Drawn as an SVG tree (bigger = heavier), and listed below as cards with the owner's decisions.

  var VENTURE_STAGE = {
    proposed: { icon: "◔", label: "Business case for you", tone: "warning", order: 0 },
    building: { icon: "▲", label: "Building", tone: "accent", order: 1 },
    live: { icon: "●", label: "Live", tone: "good", order: 2 },
    researching: { icon: "◐", label: "Being researched", tone: "accent", order: 3 },
    idea: { icon: "○", label: "Idea", tone: "", order: 4 },
    parked: { icon: "–", label: "Parked", tone: "", order: 5 },
    killed: { icon: "✕", label: "Killed", tone: "critical", order: 6 },
  };

  var VENTURE_GROUPS = [
    { key: "proposed", title: "Business cases for your decision", match: function (s) { return s === "proposed"; } },
    { key: "building", title: "Building (you backed them)", match: function (s) { return s === "building"; } },
    { key: "live", title: "Live legs", match: function (s) { return s === "live"; } },
    { key: "researching", title: "Being researched", match: function (s) { return s === "researching"; } },
    { key: "idea", title: "Ideas, the heaviest first", match: function (s) { return s === "idea"; } },
    { key: "closed", title: "Parked and killed", match: function () { return true; } },
  ];

  var VT_LAYOUT = { col: 250, row: 30, left: 30, top: 26, labelChars: 30, charWidth: 6.6 };
  // The SVG namespace, from an empty <svg> in the page (a URL literal here would look like a request off Ingress).
  function svgNamespace() { return $("vt-svg-ns").namespaceURI; }

  function svg(tag, attrs) {
    var node = document.createElementNS(svgNamespace(), tag);
    Object.keys(attrs || {}).forEach(function (key) {
      var value = attrs[key];
      if (value !== null && value !== undefined && value !== false) node.setAttribute(key, String(value));
    });
    for (var i = 2; i < arguments.length; i++) append(node, arguments[i]);
    return node;
  }

  function ventureWeight(v) {
    var w = v && v.weight !== null && v.weight !== undefined ? num(v.weight) : NaN;
    return isNaN(w) ? null : w;
  }

  // Stage first (what needs the owner, then legs, then research), then the heaviest, then the oldest.
  function ventureOrder(x, y) {
    var wx = ventureWeight(x);
    var wy = ventureWeight(y);
    return statusOrder(VENTURE_STAGE, x.stage) - statusOrder(VENTURE_STAGE, y.stage) ||
      (wy === null ? -1 : wy) - (wx === null ? -1 : wx) || num(x.id) - num(y.id);
  }

  function loadVentures() {
    var vt = ui.vt;
    if (vt.busy) { vt.again = true; return; }
    vt.busy = true;
    safely("ventures", renderVentures);
    request("GET", "api/ventures").then(function (res) {
      if (!res.ok) throw httpError(res);
      if (!isObject(res.data) || !Array.isArray(res.data.items)) throw new RequestError("malformed", res.data === undefined ? "not JSON" : "the tree is missing");
      vt.data = res.data;
      vt.stamp = res.data.stamp;
      vt.loadedAt = new Date();
      vt.error = null;
      vt.byId = {};
      res.data.items.forEach(function (v) { if (isObject(v)) vt.byId[String(v.id)] = v; });
    }).catch(function (err) {
      if (!(err instanceof RequestError)) console.error(err);
      vt.error = err;
    }).then(function () {
      vt.busy = false;
      safely("ventures", renderVentures);
      safely("banners", renderBanners);
      if (vt.again) { vt.again = false; loadVentures(); }
    });
  }

  function renderVentures() {
    var vt = ui.vt;
    var data = vt.data;
    var name = agentName();
    $("vt-refresh").textContent = vt.busy ? "Refreshing…" : "Refresh";
    setStatusText("vt-load-status", vt.error && !vt.busy ? "Couldn't load the venture tree (" + errorText(vt.error) + ")." +
      (data ? " What you see is the tree loaded earlier." : " Try Refresh.") : "", vt.error && !vt.busy ? "error" : "");
    $("vt-sub").textContent = "Every way to earn that " + name + " or you came up with, branching from the idea it grew out of. " +
      name + " scores each from 1 to 5, researches the heaviest in its venture cycles and brings you business cases.";
    if (!data) {
      replace($("vt-tree"), vt.busy ? h("p", { class: "muted", text: "Loading the venture tree…" }) : []);
      return;
    }
    var items = arr(data.items).filter(function (v) { return isObject(v) && v.id !== undefined; });
    renderVentureSummary(data, items);
    renderVentureLegend(data);
    renderVentureTree(items);
    fillParentSelect(items);
    var rows = items.map(function (v) { return Object.assign({}, v, { status: v.stage }); }).sort(ventureOrder);
    return renderQueue($("ventures"), {
      kind: "venture", rows: rows, groups: VENTURE_GROUPS,
      empty: emptyState("div", "No ventures yet.", name + " plants the first ideas when it starts; add your own with Add idea."),
      view: ventureView,
      viewKey: data.criteria,
      actionKey: function (v) { return String(v.stage) + "|" + String(v.owner_version); },
      actions: ventureActions,
      panel: venturePanel,
    });
  }

  function renderVentureSummary(data, items) {
    var name = agentName();
    var counts = {};
    items.forEach(function (v) { counts[v.stage] = (counts[v.stage] || 0) + 1; });
    var words = { proposed: ["business case for you", "business cases for you"], idea: ["idea", "ideas"] };
    var tally = Object.keys(VENTURE_STAGE).filter(function (k) { return counts[k]; }).map(function (k) {
      var word = words[k] ? words[k][counts[k] === 1 ? 0 : 1] : VENTURE_STAGE[k].label.toLowerCase();
      return counts[k] + " " + word;
    });
    var today = isObject(data.today) ? data.today : {};
    var share = num(data.share);
    var parts = [];
    if (share > 0) {
      parts.push("Ventures get " + share + "% of " + name + "'s spending (the venture_share option): " + usd(today.ventures_usd) +
        " of today's " + usd(today.spent_usd) + " so far, in " + plural(num(data.venture_cycles) || 0, "venture cycle") + " in all.");
    } else {
      parts.push("Venture cycles are off: venture_share is 0 in the app's options. Your ideas still go into the tree.");
    }
    parts.push(plural(items.length, "venture") + " in the tree" + (tally.length ? ": " + tally.join(", ") : "") + ".");
    var legs = items.filter(function (v) { return v.stage === "live" || v.stage === "building"; });
    if (legs.length) {
      parts.push("Legs: " + legs.map(function (v) {
        return v.title + " (spent " + usd(v.spent_usd) + ", earned " + usd(v.earned_usd) + ")";
      }).join("; ") + ".");
    }
    $("vt-summary").textContent = parts.join(" ");
  }

  function renderVentureLegend(data) {
    var el = $("vt-legend");
    if (el.childNodes.length) return;
    replace(el, [
      Object.keys(VENTURE_STAGE).map(function (k) {
        return h("span", { class: "vt-key" }, h("span", { class: "vt-dot", "data-stage": k }), VENTURE_STAGE[k].label);
      }),
      h("span", { class: "vt-key muted", text: "Bigger = heavier (the scores, revenue counting double); dashed = first guess" }),
    ]);
  }

  // A tidy tree, left to right: every leaf gets a row, a parent sits between its first and last child.
  function ventureLayout(items) {
    var byId = {};
    items.forEach(function (v) { byId[String(v.id)] = v; });
    var kids = { root: [] };
    items.forEach(function (v) {
      var parent = v.parent_id !== null && v.parent_id !== undefined && byId[String(v.parent_id)] ? String(v.parent_id) : "root";
      (kids[parent] = kids[parent] || []).push(v);
    });
    Object.keys(kids).forEach(function (k) { kids[k].sort(ventureOrder); });
    var pos = {};
    var leaves = 0;
    var depth = 1;
    function place(v, d) {
      var id = String(v.id);
      if (pos[id]) return pos[id].y;  // never twice, whatever the data says
      pos[id] = { x: d * VT_LAYOUT.col, y: 0 };
      depth = Math.max(depth, d);
      var children = kids[id] || [];
      var y;
      if (!children.length) y = (leaves++) * VT_LAYOUT.row;
      else {
        var ys = children.map(function (c) { return place(c, d + 1); });
        y = (ys[0] + ys[ys.length - 1]) / 2;
      }
      pos[id].y = y;
      return y;
    }
    var rootYs = kids.root.map(function (v) { return place(v, 1); });
    var rootY = rootYs.length ? (rootYs[0] + rootYs[rootYs.length - 1]) / 2 : 0;
    return { pos: pos, kids: kids, rootY: rootY, leaves: Math.max(leaves, 1), depth: depth };
  }

  function ventureRadius(v) {
    var w = ventureWeight(v);
    return w === null ? 6 : 6 + Math.round(w / 10);
  }

  function shortTitle(text, chars) {
    text = String(text || "");
    return text.length <= chars ? text : text.slice(0, chars - 1).replace(/\s+$/, "") + "…";
  }

  function renderVentureTree(items) {
    var el = $("vt-tree");
    if (!items.length) {
      replace(el, emptyState("div", "The tree is empty.", "Add an idea, or let " + agentName() + " brainstorm in its next venture cycle."));
      return;
    }
    var L = ventureLayout(items);
    var top = VT_LAYOUT.top;
    var left = VT_LAYOUT.left;
    var width = left + L.depth * VT_LAYOUT.col + 260;
    var height = top * 2 + (L.leaves - 1) * VT_LAYOUT.row;
    var root = { x: left, y: top + L.rootY };
    function at(v) { var p = L.pos[String(v.id)]; return { x: left + p.x, y: top + p.y }; }
    // An edge leaves its parent where the parent's label ends (see fitEdges), so it never runs through the label.
    var edges = [];
    items.forEach(function (v) {
      if (!L.pos[String(v.id)]) return;
      var parent = v.parent_id !== null && v.parent_id !== undefined ? ui.vt.byId[String(v.parent_id)] : null;
      var placed = parent && L.pos[String(parent.id)];
      var from = placed ? at(parent) : root;
      var start = placed ? { x: from.x + ventureRadius(parent) + 6 + labelOf(parent).length * VT_LAYOUT.charWidth + 6, y: from.y } : from;
      var path = svg("path", { class: "vt-edge", "data-stage": v.stage, "data-from": placed ? parent.id : null, d: edgePath(start, at(v)) });
      path.emberEdge = { from: from, to: at(v), gap: placed ? ventureRadius(parent) + 12 : 0 };
      edges.push(path);
    });
    var nodes = items.filter(function (v) { return L.pos[String(v.id)]; }).map(function (v) {
      var p = at(v);
      var r = ventureRadius(v);
      var w = ventureWeight(v);
      var stage = (VENTURE_STAGE[v.stage] || { label: String(v.stage) }).label;
      var label = labelOf(v);
      var g = svg("g", {
        class: "vt-node", "data-stage": v.stage, "data-id": v.id, tabindex: "0", role: "button",
        "data-guess": v.scores_by === "brainstorm" ? "true" : null,
        "data-selected": String(ui.vt.selected) === String(v.id) ? "true" : null,
        "aria-label": v.title + ", " + stage + (w === null ? ", not scored yet" : ", weight " + w) + ". Show its card.",
      },
      svg("title", null, "#" + v.id + " " + v.title + " · " + stage + (w === null ? "" : " · weight " + w)),
      svg("circle", { cx: p.x, cy: p.y, r: r }),
      svg("text", { x: p.x + r + 6, y: p.y + 4 }, label));
      g.addEventListener("click", function () { showVentureCard(v.id); });
      g.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); showVentureCard(v.id); }
      });
      return g;
    });
    var picture = svg("svg", { class: "vt-svg", width: width, height: height, viewBox: "0 0 " + width + " " + height,
      role: "group", "aria-label": "The venture tree, " + plural(items.length, "venture") },
      svg("g", { class: "vt-edges" }, edges),
      svg("g", { class: "vt-root" }, svg("circle", { cx: root.x, cy: root.y, r: 9 }),
        svg("text", { x: root.x - 4, y: root.y - 14 }, agentName())),
      svg("g", { class: "vt-nodes" }, nodes));
    var scrollLeft = el.scrollLeft;
    var scrollTop = el.scrollTop;
    replace(el, picture);
    el.scrollLeft = scrollLeft;
    el.scrollTop = scrollTop;
    fitEdges(picture);
  }

  function edgePath(from, to) {
    var mid = (from.x + to.x) / 2;
    return "M" + from.x + " " + from.y + " C" + mid + " " + from.y + " " + mid + " " + to.y + " " + to.x + " " + to.y;
  }

  function labelOf(v) {
    var w = ventureWeight(v);
    return shortTitle(v.title, VT_LAYOUT.labelChars) + (w === null ? "" : " · " + w);
  }

  // With the picture on the page, labels have their real width: start each edge right after its parent's label.
  function fitEdges(picture) {
    var widths = {};
    Array.prototype.forEach.call(picture.querySelectorAll(".vt-node"), function (g) {
      var text = g.querySelector("text");
      var width = text && text.getComputedTextLength ? text.getComputedTextLength() : 0;
      if (width > 0) widths[g.getAttribute("data-id")] = width;
    });
    Array.prototype.forEach.call(picture.querySelectorAll(".vt-edge[data-from]"), function (path) {
      var width = widths[path.getAttribute("data-from")];
      var edge = path.emberEdge;
      if (!width || !edge) return;  // not measurable while the tab is hidden: the estimate stays
      path.setAttribute("d", edgePath({ x: edge.from.x + edge.gap + width, y: edge.from.y }, edge.to));
    });
  }

  // A node of the tree was chosen: mark it, and bring its card into view.
  function showVentureCard(id) {
    ui.vt.selected = id;
    Array.prototype.forEach.call($("vt-tree").querySelectorAll(".vt-node"), function (g) {
      if (g.getAttribute("data-id") === String(id)) g.setAttribute("data-selected", "true");
      else g.removeAttribute("data-selected");
    });
    var card = $("ventures").querySelector('article[data-id="' + String(id) + '"]');
    if (!card) return;
    Array.prototype.forEach.call($("ventures").querySelectorAll("article[data-selected]"), function (c) { c.removeAttribute("data-selected"); });
    card.setAttribute("data-selected", "true");
    card.scrollIntoView({ block: "center", behavior: "smooth" });
    var title = card.querySelector(".vt-card-title");
    if (title) title.focus({ preventScroll: true });
  }

  function ventureView(v) {
    var name = agentName();
    var data = ui.vt.data || {};
    var parent = v.parent_id !== null && v.parent_id !== undefined ? ui.vt.byId[String(v.parent_id)] : null;
    var w = ventureWeight(v);
    var scores = isObject(v.scores) ? v.scores : {};
    var theCase = isObject(v.case) ? v.case : {};
    var caseSpec = arr(data.case);
    var filled = caseSpec.filter(function (c) { return theCase[c.name]; });
    var scored = arr(data.criteria).some(function (c) { return scores[c.name]; });
    var projects = arr(v.projects);
    var researched = Number(v.researched) || 0;
    var toPropose = Number(data.research_to_propose) || 2;
    var needs = (researched < toPropose ? ["research that found something (" + researched + " of " + toPropose + " calls)"] : [])
      .concat(arr(v.missing));
    return [
      h("div", { class: "item-head" },
        h("h3", { class: "vt-card-title", tabindex: "-1", text: v.title || "Untitled venture" }),
        chip(VENTURE_STAGE, v.stage, sentence(String(v.stage || "unknown"))),
        v.stage === "parked" && v.parked_by === "owner" ? plainChip("Parked by you") :
          v.stage === "parked" && v.parked_by === "code" ? plainChip("Parked by Ember's code") : null,
        plainChip(w === null ? "Not scored yet" : "Weight " + w + (v.scores_by === "brainstorm" ? ", a first guess" : "")),
        v.created_by === "owner" ? plainChip("Your idea") : null,
        v.simulated ? testTag() : null),
      h("p", { class: "muted small", text: "#" + v.id + (parent ? " · branch of #" + parent.id + " " + parent.title : "") }),
      h("p", { class: "pre-line", text: asText(v.pitch) }),
      scored ? h("dl", { class: "vt-scores" }, arr(data.criteria).map(function (c) {
        var value = scores[c.name];
        return h("div", { title: "1: " + c.low + " · 5: " + c.high },
          h("dt", { text: c.label }), h("dd", { text: value ? value + "/5" : "–" }));
      })) : null,
      v.stage_rule ? h("p", null, h("strong", { text: "Stage rule: " }), String(v.stage_rule)) : null,
      v.next_question ? h("p", null, h("strong", { text: "Next question: " }), String(v.next_question)) : null,
      filled.length ? h("dl", { class: "item-grid" }, caseSpec.map(function (c) {
        return h("div", null, h("dt", { text: c.label }), h("dd", { class: "pre-line", text: asText(theCase[c.name]) || "–" }));
      })) : null,
      filled.length && needs.length && (v.stage === "researching" || v.stage === "idea") ?
        h("p", { class: "muted small", text: "For a business case " + name + " still needs: " + needs.join(", ") + "." }) : null,
      ventureEvidence(v),
      h("dl", { class: "money" },
        h("div", { title: "Research calls for this venture that found web pages: its scores need one, a business case " + toPropose + "." },
          h("dt", { text: "Research" }),
          h("dd", { text: researched ? researched + " call" + (researched === 1 ? "" : "s") + " with web results" : "None yet" })),
        // 0.12.0: what its research may still cost before the agent decides it (none once it is backed)
        v.research_left_usd !== null && v.research_left_usd !== undefined ? h("div", {
          title: "What research for it may still cost before " + name + " decides it: a business case or parked. Research next, more or again gives it a new budget of " + usd(data.research_budget_usd) + "." },
          h("dt", { text: "Research budget" }),
          h("dd", { text: num(v.research_left_usd) > 0 ? usd(v.research_left_usd) + " of " + usd(data.research_budget_usd) + " left" : "Used: " + name + " decides it now" })) : null,
        h("div", { title: "What the model calls that worked for it cost: its research and the work of the cycles aimed at it." },
          h("dt", { text: "Spent" }), h("dd", { text: usd(v.spent_usd) })),
        // 0.12.0: its P&L: revenue less refunds, its expenses (Etsy's fees, say) and what it nets
        h("div", { title: "Revenue recorded for it or its projects, less refunds." }, h("dt", { text: "Earned" }),
          h("dd", { text: usd(v.earned_usd) + (num(v.refunds_usd) > 0 ? " (" + usd(v.revenue_usd) + " less " + usd(v.refunds_usd) + " refunded)" : "") })),
        num(v.expenses_usd) ? h("div", { title: "Etsy's fees and other expenses recorded for it or its projects." },
          h("dt", { text: "Expenses" }), h("dd", { text: usd(v.expenses_usd) })) : null,
        num(v.earned_usd) || num(v.expenses_usd) ? h("div", { title: "Earned, less its expenses and what it spent." },
          h("dt", { text: "Net" }), h("dd", { text: signedUsd(v.net_usd), "data-tone": num(v.net_usd) < 0 ? "critical" : "" })) : null,
        projects.length ? h("div", null, h("dt", { text: "Projects" }), h("dd", { text: projects.map(function (p) {
          return "#" + p.id + " " + p.title + " (" + p.status + ")";
        }).join(", ") })) : null),
      ventureWord(v),
      v.notes ? h("details", { class: "notes" }, h("summary", { text: "Notes" }), h("pre", { class: "notes-text", text: asText(v.notes) })) : null,
      lastDigest(v),
      ventureKnowledge(v),
      h("p", { class: "muted small" }, (v.created_by === "owner" ? "Added by " + (v.entered_by || "you") : "Added by " + name) + " ",
        timeEl(v.created_at), " · updated ", timeEl(v.updated_at)),
    ];
  }

  // 0.12.0: the claims its research found, each with the grade Ember's code gave its page.
  var EVIDENCE_GRADE = {
    independent: { icon: "✓", label: "Independent", tone: "good" },
    marketing: { icon: "$", label: "Marketing", tone: "warning" },
    unchecked: { icon: "?", label: "Unchecked", tone: "" },
  };

  function ventureEvidence(v) {
    var ev = isObject(v.evidence) ? v.evidence : {};
    var counts = isObject(ev.counts) ? ev.counts : {};
    var items = arr(ev.items);
    var grades = ["independent", "marketing", "unchecked"];
    var total = grades.reduce(function (sum, g) { return sum + (num(counts[g]) || 0); }, 0);
    if (!total) return null;
    return h("details", { class: "notes" },
      h("summary", { text: "Evidence: " + plural(total, "claim") + " (" + grades.map(function (g) {
        return (num(counts[g]) || 0) + " " + g;
      }).join(", ") + ")" }),
      h("p", { class: "muted small", text: "Ember's code grades each page: independent, marketing (a vendor's or an affiliate's page) or unchecked (not a page " +
        agentName() + "'s research found)." }),
      h("ul", { class: "vt-evidence" }, items.map(function (e) {
        var host = "";
        try { host = new URL(String(e.url)).hostname.replace(/^www\./, ""); } catch (err) { host = ""; }
        return h("li", null, chip(EVIDENCE_GRADE, e.source, sentence(String(e.source))), " ",
          h("strong", { text: String(e.value) }), " · " + String(e.claim) + " · ", ownerLink(e.url, host || null), " · #" + e.id);
      })),
      items.length < total ? h("p", { class: "muted small", text: "The newest " + items.length + " of " + total + "." }) : null);
  }

  var VENTURE_WORDS = { added: "You added this idea", research: "You asked for research next", back: "You backed it",
    park: "You parked it", kill: "You killed it", note: "Your note" };

  function ventureWord(v) {
    if (!v.owner_action) return null;
    var name = agentName();
    return h("div", { class: "decision" },
      h("p", null, h("strong", { text: VENTURE_WORDS[v.owner_action] || sentence(String(v.owner_action)) }), " · ", timeEl(v.owner_at),
        " · " + (v.seen_by_agent ? name + " has seen it." : name + " sees it on its next wake.")),
      v.owner_comment ? h("p", { class: "pre-line", text: asText(v.owner_comment) }) : null);
  }

  // What Ember learned (its knowledge file): loaded when the owner opens it (again on every opening, so it is
  // current), and kept open across polls. The box updates itself: a card the owner is using isn't redrawn.
  function ventureKnowledge(v) {
    if (v.file_bytes === null || v.file_bytes === undefined) {
      return h("p", { class: "muted small", text: "What " + agentName() + " learns about it goes to " + v.file + " (nothing yet)." });
    }
    var box = h("div", { class: "vt-file" });
    fillKnowledge(box, v, false);
    return box;
  }

  function fillKnowledge(box, v, focus) {
    var state = ui.vt.files[String(v.id)] || {};
    var toggle = h("button", { type: "button", class: "btn btn-small", "aria-expanded": state.open ? "true" : "false",
      text: (state.open ? "Hide" : "Show") + " what " + agentName() + " learned (" + byteSize(num(v.file_bytes) || 0) + ")" });
    toggle.addEventListener("click", function () { toggleKnowledge(box, v); });
    var body = null;
    if (state.open) {
      if (state.busy) body = h("p", { class: "muted small", role: "status", text: "Loading " + v.file + "…" });
      else if (state.error) body = h("p", { class: "field-error", text: "Couldn't load " + v.file + " (" + errorText(state.error) + ")." });
      else body = h("pre", { class: "capped vt-knowledge", tabindex: "0", text: state.text || "" });
    }
    replace(box, [toggle, body]);
    if (focus) toggle.focus();
  }

  function toggleKnowledge(box, v) {
    var key = String(v.id);
    var state = ui.vt.files[key] || (ui.vt.files[key] = {});
    state.open = !state.open;
    if (state.open) {
      var seq = state.seq = (state.seq || 0) + 1;
      state.busy = true;
      state.error = null;
      request("GET", "api/workspace/file?path=" + encodeURIComponent(v.file), null, { accept: "text/plain" }).then(function (res) {
        if (!res.ok) throw httpError(res);
        if (typeof res.text !== "string") throw new RequestError("malformed", "no text");
        if (seq === state.seq) state.text = res.text;
      }).catch(function (err) {
        if (!(err instanceof RequestError)) console.error(err);
        if (seq === state.seq) state.error = err;
      }).then(function () {
        if (seq !== state.seq) return;
        state.busy = false;
        if (box.isConnected) fillKnowledge(box, v, box.contains(document.activeElement));
      });
    }
    fillKnowledge(box, v, true);
  }

  function ventureActions(it, v) {
    var s = v.stage;
    var list = [];
    if (s === "idea" || s === "researching" || s === "proposed" || s === "parked") list.push(panelButton(it, "back", "Back it"));
    if (s === "idea" || s === "researching") list.push(panelButton(it, "research", "Research next"));
    if (s === "proposed") list.push(panelButton(it, "research", "Research more"));
    if (s === "parked" || s === "killed") list.push(panelButton(it, "research", "Research again"));
    if (s !== "parked" && s !== "killed") list.push(panelButton(it, "park", "Park"));
    list.push(panelButton(it, "note", "Note"));
    if (s !== "killed") list.push(panelButton(it, "kill", "Kill", true));
    return list;
  }

  function venturePanel(it, mode) {
    var name = agentName();
    var comment = { name: "comment", label: mode === "note" ? "Your note" : "Comment for " + name + " (optional)", rows: 2, max: 1000,
      required: mode === "note", missing: "Write the note." };
    var specs = {
      back: { title: "Back this venture", submit: "Back it",
        intro: [h("p", { text: "Backing tells " + name + " to build it: it plans the first test and asks you, one step at a time, for what only you can do (accounts, money, setup)." })] },
      research: { title: "Research this next", submit: "Research next",
        intro: [h("p", { text: name + " researches it in its next venture cycle, with a new research budget of " + usd((ui.vt.data || {}).research_budget_usd) +
          ", scores it from the evidence and brings you a business case or tells you why not." })] },
      park: { title: "Park this venture?", submit: "Park",
        intro: [h("p", { text: name + " stops working on it. It stays in the tree, and you can have it researched again later." })] },
      kill: { title: "Kill this venture?", submit: "Kill", danger: true,
        intro: [h("p", { text: name + " stops all work on it. It stays in the tree, dimmed, so it isn't started again." })] },
      note: { title: "A note for " + name, submit: "Send note",
        intro: [h("p", { text: name + " reads it with the venture on its next wake." })] },
    };
    var done = { back: "Backed.", research: "Asked for research.", park: "Parked.", kill: "Killed.", note: "Note sent." };
    var spec = specs[mode];
    spec.mode = mode;
    spec.fields = [comment];
    spec.url = "api/ventures/" + encodeURIComponent(String(it.row.id)) + "/decide";
    spec.body = function (values) {
      var body = { action: mode };
      var version = num(it.row.owner_version);
      if (!isNaN(version)) body.expected_version = version;
      if (values.comment) body.comment = values.comment;
      return body;
    };
    spec.done = function (res) {
      loadVentures();
      return done[mode] + " " + decisionWake(res, name);
    };
    return spec;
  }

  // ---- Add idea

  function fillParentSelect(items) {
    var select = $("vt-form-parent");
    if (isBusy(select)) return;
    var current = select.value;
    var options = [h("option", { value: "", text: "The top of the tree" })].concat(items.filter(function (v) {
      return v.stage !== "killed";
    }).slice().sort(function (x, y) { return num(x.id) - num(y.id); }).map(function (v) {
      return h("option", { value: String(v.id), text: "#" + v.id + " " + shortTitle(v.title, 60) });
    }));
    replace(select, options);
    select.value = current && ui.vt.byId[current] ? current : "";
  }

  function ventureLimit(key, fallback) {
    var limits = ui.vt.data && isObject(ui.vt.data.limits) ? ui.vt.data.limits : {};
    var n = num(limits[key]);
    return isNaN(n) ? fallback : n;
  }

  function ventureCounter(id, max) {
    var n = $(id).value.trim().length;
    var counter = $(id + "-count");
    counter.textContent = intFmt.format(n) + " / " + intFmt.format(max) + " characters";
    counter.setAttribute("data-over", n > max ? "true" : "false");
  }

  function setVentureFieldError(id, message) {
    var error = $(id + "-error");
    error.textContent = message || "";
    error.hidden = !message;
    if (message) $(id).setAttribute("aria-invalid", "true");
    else $(id).removeAttribute("aria-invalid");
  }

  function openVentureForm(open) {
    $("vt-form").hidden = !open;
    $("vt-add").setAttribute("aria-expanded", String(open));
    if (open) {
      ventureCounter("vt-form-title", ventureLimit("title", 80));
      ventureCounter("vt-form-pitch", ventureLimit("pitch", 600));
      $("vt-form-title").focus();
    } else {
      $("vt-add").focus();
    }
  }

  function submitVentureForm() {
    if (ui.vt.saving) return;
    var title = $("vt-form-title").value.trim();
    var pitch = $("vt-form-pitch").value.trim();
    var parent = $("vt-form-parent").value;
    var problems = 0;
    setVentureFieldError("vt-form-title", "");
    setVentureFieldError("vt-form-pitch", "");
    if (!title) { setVentureFieldError("vt-form-title", "Name the idea."); problems++; }
    else if (title.length > ventureLimit("title", 80)) { setVentureFieldError("vt-form-title", "Keep it under " + ventureLimit("title", 80) + " characters."); problems++; }
    if (!pitch) { setVentureFieldError("vt-form-pitch", "Say in a sentence what it is and who would pay."); problems++; }
    else if (pitch.length > ventureLimit("pitch", 600)) { setVentureFieldError("vt-form-pitch", "Keep it under " + ventureLimit("pitch", 600) + " characters."); problems++; }
    if (problems) {
      setStatusText("vt-status", problems === 1 ? "Please fix the marked field." : "Please fix the marked fields.", "error");
      (title && title.length <= ventureLimit("title", 80) ? $("vt-form-pitch") : $("vt-form-title")).focus();
      return;
    }
    var body = { title: title, pitch: pitch };
    if (parent) body.parent_id = num(parent);
    ui.vt.saving = true;
    $("vt-form-save").disabled = true;
    setStatusText("vt-status", "Saving…", "");
    request("POST", "api/ventures", body).then(function (res) {
      if (res.status === 201) {
        var id = isObject(res.data) ? res.data.id : null;
        $("vt-form-title").value = "";
        $("vt-form-pitch").value = "";
        openVentureForm(false);
        setStatusText("vt-status", "Added to the tree" + (id ? " as #" + id : "") + ". " + agentName() + " sees it on its next wake.", "ok");
        ui.vt.selected = id;
        loadVentures();
        return;
      }
      var data = isObject(res.data) ? res.data : {};
      var msg = typeof data.error === "string" && data.error ? endSentence(sentence(data.error)) : "";
      if (res.status === 422 && msg && (data.field === "title" || data.field === "pitch")) {
        setVentureFieldError("vt-form-" + data.field, msg);
        setStatusText("vt-status", "Please fix the marked field.", "error");
        $("vt-form-" + data.field).focus();
        return;
      }
      setStatusText("vt-status", msg ? "Nothing was saved: " + lowerFirst(msg) : "Nothing was saved (" + httpError(res).message + ").", "error");
    }).catch(function (err) {
      if (!(err instanceof RequestError)) console.error(err);
      setStatusText("vt-status", "Couldn't reach " + agentName() + ", so it's not clear whether the idea was saved. Refresh the tree before you try again.", "error");
    }).then(function () {
      ui.vt.saving = false;
      $("vt-form-save").disabled = false;
    });
  }

  function initVentures() {
    $("vt-refresh").addEventListener("click", loadVentures);
    $("vt-add").addEventListener("click", function () { openVentureForm($("vt-form").hidden); });
    $("vt-form-cancel").addEventListener("click", function () { openVentureForm(false); });
    $("vt-form").addEventListener("submit", function (ev) { ev.preventDefault(); submitVentureForm(); });
    $("vt-form").addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") { ev.preventDefault(); openVentureForm(false); }
      else if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); submitVentureForm(); }
    });
    $("vt-form-title").addEventListener("input", function () { setVentureFieldError("vt-form-title", ""); ventureCounter("vt-form-title", ventureLimit("title", 80)); });
    $("vt-form-pitch").addEventListener("input", function () { setVentureFieldError("vt-form-pitch", ""); ventureCounter("vt-form-pitch", ventureLimit("pitch", 600)); });
  }

  // ------------------------------------------------------------------ roadmap (0.11.0)
  // Where Ember is heading: goals for the next months, the milestones that lead to them and this week's steps, each
  // with a date and a measure of done. Drawn as a timeline (a row per milestone, under the goal it leads to; today
  // marked) and listed below as cards by horizon. The owner adds milestones, leaves notes and drops them.

  var MILESTONE_STATE = {
    overdue: { icon: "▲", label: "Overdue", tone: "critical", order: 0 },
    week: { icon: "◆", label: "Due this week", tone: "warning", order: 1 },
    month: { icon: "◆", label: "This month", tone: "accent", order: 2 },
    quarter: { icon: "◆", label: "Next three months", tone: "", order: 3 },
    later: { icon: "◆", label: "Later", tone: "", order: 4 },
    done: { icon: "●", label: "Done", tone: "good", order: 5 },
    missed: { icon: "✕", label: "Missed", tone: "", order: 6 },
    dropped: { icon: "–", label: "Dropped", tone: "", order: 7 },
  };

  var ROADMAP_GROUPS = [
    { key: "overdue", title: "Overdue", match: function (s) { return s === "overdue"; } },
    { key: "week", title: "Due this week", match: function (s) { return s === "week"; } },
    { key: "month", title: "This month", match: function (s) { return s === "month"; } },
    { key: "quarter", title: "Next three months", match: function (s) { return s === "quarter"; } },
    { key: "later", title: "Later", match: function (s) { return s === "later"; } },
    { key: "closed", title: "Done, missed and dropped", match: function () { return true; } },
  ];

  // The timeline's marks: the state (open, overdue, done, missed, dropped) has its own shape and color, and the row
  // its label, so no state is told by color alone.
  var MARK_STATES = [
    { key: "open", label: "Open" }, { key: "overdue", label: "Overdue" }, { key: "done", label: "Done" },
    { key: "missed", label: "Missed" }, { key: "dropped", label: "Dropped" },
  ];
  var RM = { row: 30, axis: 38, pad: 12, label: 250, narrowLabel: 150, minPlot: 360, before: 21, after: 91, charWidth: 6.6 };
  var monthFmt = new Intl.DateTimeFormat(undefined, { month: "short", timeZone: "UTC" });

  // Calendar days as whole numbers (days since 1970-01-01), so dates compare and space out exactly.
  function dayOf(y, m, d) { return Math.round(Date.UTC(y, m, d) / 86400000); }
  function dayNumber(iso) {
    var p = String(iso || "").split("-");
    return p.length === 3 ? dayOf(Number(p[0]), Number(p[1]) - 1, Number(p[2])) : NaN;
  }
  function stampDay(iso) {  // a timestamp's day on the owner's calendar
    var t = new Date(iso);
    return iso && validDate(t) ? dayOf(t.getFullYear(), t.getMonth(), t.getDate()) : NaN;
  }
  function dayParts(n) { var d = new Date(n * 86400000); return { y: d.getUTCFullYear(), m: d.getUTCMonth() }; }

  function markState(m) {
    if (m.status !== "open") return m.status;
    return m.horizon === "overdue" ? "overdue" : "open";
  }

  function whenText(days) {
    var d = num(days);
    if (isNaN(d)) return "";
    if (d === 0) return "today";
    if (d === 1) return "tomorrow";
    return d > 0 ? "in " + d + " days" : plural(-d, "day") + " late";
  }

  function loadRoadmap() {
    var rm = ui.rm;
    if (rm.busy) { rm.again = true; return; }
    rm.busy = true;
    safely("roadmap", renderRoadmap);
    request("GET", "api/roadmap").then(function (res) {
      if (!res.ok) throw httpError(res);
      if (!isObject(res.data) || !Array.isArray(res.data.items)) throw new RequestError("malformed", res.data === undefined ? "not JSON" : "the roadmap is missing");
      rm.data = res.data;
      rm.stamp = res.data.stamp;
      rm.error = null;
      rm.byId = {};
      res.data.items.forEach(function (m) { if (isObject(m)) rm.byId[String(m.id)] = m; });
    }).catch(function (err) {
      if (!(err instanceof RequestError)) console.error(err);
      rm.error = err;
    }).then(function () {
      rm.busy = false;
      safely("roadmap", renderRoadmap);
      if (rm.again) { rm.again = false; loadRoadmap(); }
    });
  }

  function renderRoadmap() {
    var rm = ui.rm;
    var data = rm.data;
    var name = agentName();
    $("rm-refresh").textContent = rm.busy ? "Refreshing…" : "Refresh";
    setStatusText("rm-load-status", rm.error && !rm.busy ? "Couldn't load the roadmap (" + errorText(rm.error) + ")." +
      (data ? " What you see is the roadmap loaded earlier." : " Try Refresh.") : "", rm.error && !rm.busy ? "error" : "");
    $("rm-sub").textContent = "Where " + name + " is heading: goals for the next months, the milestones that lead to them and " +
      "this week's steps, each with a date and a measure of done. " + name + " plans every cycle toward the one due first.";
    if (!data) {
      replace($("rm-chart"), rm.busy ? h("p", { class: "muted rm-loading", text: "Loading the roadmap…" }) : []);
      return;
    }
    var items = arr(data.items).filter(function (m) { return isObject(m) && m.id !== undefined; });
    renderRoadmapSummary(data, items);
    renderRoadmapLegend();
    renderRoadmapChart(data, items);
    fillMilestoneParents(items);
    var rows = items.map(function (m) { return Object.assign({}, m, { status: m.horizon, state: m.status }); }).sort(milestoneOrder);
    return renderQueue($("roadmap"), {
      kind: "milestone", rows: rows, groups: ROADMAP_GROUPS,
      empty: emptyState("div", "No milestones yet.", name + " lays out its roadmap in its next wake cycle; add your own with Add milestone."),
      view: milestoneView,
      viewKey: data.today,
      actionKey: function (m) { return String(m.state) + "|" + String(m.owner_version); },
      actions: milestoneActions,
      panel: milestonePanel,
    });
  }

  // Open ones by date, the first due first; closed ones the latest first.
  function milestoneOrder(x, y) {
    var closedX = x.state !== "open";
    var closedY = y.state !== "open";
    if (closedX !== closedY) return closedX ? 1 : -1;
    if (closedX) return String(y.closed_at || "").localeCompare(String(x.closed_at || "")) || num(y.id) - num(x.id);
    return dayNumber(x.due) - dayNumber(y.due) || num(x.id) - num(y.id);
  }

  function renderRoadmapSummary(data, items) {
    var open = items.filter(function (m) { return m.status === "open"; });
    var counts = {};
    items.forEach(function (m) { counts[m.horizon] = (counts[m.horizon] || 0) + 1; });
    var parts = [];
    if (open.length) {
      var tally = ["overdue", "week", "month", "quarter", "later"].filter(function (k) { return counts[k]; }).map(function (k) {
        return counts[k] + " " + MILESTONE_STATE[k].label.toLowerCase();
      });
      parts.push(plural(open.length, "open milestone") + ": " + tally.join(", ") + ".");
    } else {
      parts.push("No open milestones.");
    }
    var ended = ["done", "missed", "dropped"].filter(function (k) { return counts[k]; }).map(function (k) { return counts[k] + " " + k; });
    if (ended.length) parts.push("Closed: " + ended.join(", ") + ".");
    var moved = open.filter(function (m) { return num(m.moves) > 0; }).length;
    if (moved) parts.push(plural(moved, "open milestone") + " moved from " + (moved === 1 ? "its" : "their") + " first date.");
    var waiting = open.filter(function (m) { return m.waiting; }).length;
    if (waiting) parts.push(plural(waiting, "open milestone") + " waiting.");
    if (data.overhead_usd !== undefined && data.overhead_usd !== null) {
      parts.push("Overhead (plans, reviews, brainstorms, library study): " + usd(data.overhead_usd) + ".");
    }
    $("rm-summary").textContent = parts.join(" ");
  }

  function renderRoadmapLegend() {
    var el = $("rm-legend");
    if (el.childNodes.length) return;
    var keys = MARK_STATES.map(function (s) {
      return h("span", { class: "rm-key", "data-state": s.key }, legendMark(markPath(s.key, 7, 7)), s.label);
    });
    keys.push(h("span", { class: "rm-key", "data-state": "open" }, legendMark(diamondPath(7, 7, 4.5), "rm-first"), "First date, when it moved"));
    keys.push(h("span", { class: "rm-key" }, svg("svg", { width: 14, height: 14, "aria-hidden": "true" },
      svg("line", { class: "rm-today", x1: 7, x2: 7, y1: 0, y2: 14 })), "Today"));
    replace(el, keys);
  }

  function legendMark(d, cls) {
    return svg("svg", { width: 14, height: 14, viewBox: "0 0 14 14", "aria-hidden": "true" }, svg("path", { class: cls || "rm-mark", d: d }));
  }

  function diamondPath(cx, cy, r) {
    return "M" + cx + " " + (cy - r) + " L" + (cx + r) + " " + cy + " L" + cx + " " + (cy + r) + " L" + (cx - r) + " " + cy + " Z";
  }

  // Open: a diamond. Overdue: a triangle. Done: a disc. Missed: a cross. Dropped: a dash.
  function markPath(state, cx, cy) {
    if (state === "overdue") return "M" + cx + " " + (cy - 6.5) + " L" + (cx + 6.5) + " " + (cy + 5) + " L" + (cx - 6.5) + " " + (cy + 5) + " Z";
    if (state === "done") return "M" + (cx - 5.5) + " " + cy + " a5.5 5.5 0 1 0 11 0 a5.5 5.5 0 1 0 -11 0 Z";
    if (state === "missed") return "M" + (cx - 4.5) + " " + (cy - 4.5) + " L" + (cx + 4.5) + " " + (cy + 4.5) + " M" + (cx + 4.5) + " " + (cy - 4.5) + " L" + (cx - 4.5) + " " + (cy + 4.5);
    if (state === "dropped") return "M" + (cx - 5) + " " + cy + " L" + (cx + 5) + " " + cy;
    return diamondPath(cx, cy, 6.5);
  }

  // The rows: every milestone under the one it leads to (each goal followed by its steps), by date.
  function roadmapRows(items, start) {
    var shown = items.filter(function (m) { return m.status === "open" || stampDay(m.closed_at) >= start; });
    var byId = {};
    shown.forEach(function (m) { byId[String(m.id)] = m; });
    var kids = {};
    var roots = [];
    shown.forEach(function (m) {
      var parent = m.parent_id !== null && m.parent_id !== undefined && byId[String(m.parent_id)] ? String(m.parent_id) : null;
      if (parent) (kids[parent] = kids[parent] || []).push(m);
      else roots.push(m);
    });
    var order = function (x, y) { return dayNumber(x.due) - dayNumber(y.due) || num(x.id) - num(y.id); };
    // The plan first: open goals by date; what ended lately below them.
    var closedLast = function (x, y) {
      var cx = x.status !== "open";
      var cy = y.status !== "open";
      return cx !== cy ? (cx ? 1 : -1) : order(x, y);
    };
    var rows = [];
    var seen = {};
    function walk(list, depth) {
      list.slice().sort(depth ? order : closedLast).forEach(function (m) {
        if (seen[String(m.id)]) return;
        seen[String(m.id)] = true;
        rows.push({ m: m, depth: depth });
        walk(kids[String(m.id)] || [], depth + 1);
      });
    }
    walk(roots, 0);
    return rows;
  }

  function renderRoadmapChart(data, items) {
    var el = $("rm-chart");
    hideMilestoneTip();
    var name = agentName();
    var today = dayNumber(data.today);
    var open = items.filter(function (m) { return m.status === "open"; });
    if (isNaN(today) || !items.length) {
      replace(el, emptyState("div", "Nothing planned yet.", name + " lays out its roadmap itself: a goal for the next three months, " +
        "this month's milestones toward it and this week's steps."));
      return;
    }
    var lastDue = open.reduce(function (last, m) { var d = dayNumber(m.due); return isNaN(d) ? last : Math.max(last, d); }, today);
    var start = today - RM.before;
    var end = Math.max(today + RM.after, lastDue + 7);
    var rows = roadmapRows(items, start);
    if (!rows.length) {
      replace(el, emptyState("div", "Nothing open or closed lately.", "Older milestones are in the list below."));
      return;
    }
    var width = el.clientWidth || 720;
    var labelW = width < 640 ? RM.narrowLabel : RM.label;
    var plotW = Math.max(RM.minPlot, width - labelW - RM.pad * 3);
    var totalW = RM.pad + labelW + plotW + RM.pad * 2;
    var height = RM.axis + rows.length * RM.row + RM.pad;
    var x0 = RM.pad + labelW + RM.pad;
    function x(day) { return x0 + (Math.min(Math.max(day, start), end) - start) / (end - start) * plotW; }
    function inside(day) { return !isNaN(day) && day >= start && day <= end; }

    var grid = [];
    var first = true;
    for (var n = dayOf(dayParts(start).y, dayParts(start).m + 1, 1); n <= end; n = dayOf(dayParts(n).y, dayParts(n).m + 1, 1)) {
      var gx = x(n);
      var parts = dayParts(n);
      grid.push(svg("line", { class: "rm-grid", x1: gx, x2: gx, y1: RM.axis - 8, y2: height - RM.pad }));
      grid.push(svg("text", { class: "rm-month", x: gx + 4, y: RM.axis - 12 },
        monthFmt.format(new Date(n * 86400000)) + (first || parts.m === 0 ? " " + parts.y : "")));
      first = false;
    }
    var tx = x(today);
    var now = [
      svg("line", { class: "rm-today", x1: tx, x2: tx, y1: RM.axis - 26, y2: height - RM.pad }),
      svg("text", { class: "rm-today-label", x: tx, y: 11, "text-anchor": "middle" }, "Today"),
    ];

    var marks = rows.map(function (r, i) {
      var m = r.m;
      var state = markState(m);
      var y = RM.axis + i * RM.row + RM.row / 2;
      var due = dayNumber(m.due);
      var planned = stampDay(m.created_at);
      var until = m.status === "open" ? due : stampDay(m.closed_at);
      var indent = Math.min(r.depth, 4) * 12;
      var chars = Math.max(6, Math.floor((labelW - indent - 18) / RM.charWidth));
      var label = MILESTONE_STATE[m.horizon] || MILESTONE_STATE[m.status] || { label: String(m.status) };
      var parts = [
        svg("rect", { class: "rm-hit", x: 0, y: y - RM.row / 2, width: totalW, height: RM.row }),
        svg("text", { class: "rm-glyph", x: RM.pad + indent, y: y + 4 }, (MILESTONE_STATE[m.horizon] || MILESTONE_STATE[m.status] || { icon: "" }).icon),
        svg("text", { class: "rm-label", x: RM.pad + indent + 14, y: y + 4 }, shortTitle(m.title, chars)),
      ];
      // From when it was planned to its date (to when it ended, once closed).
      if (!isNaN(planned) && !isNaN(until) && Math.max(planned, until) >= start) {
        var a = x(Math.min(planned, until));
        var b = x(Math.max(planned, until));
        if (b - a >= 2) parts.push(svg("rect", { class: "rm-bar", x: a, y: y - 3, width: b - a, height: 6, rx: 3 }));
      }
      // Where it was first due, when its date moved.
      var firstDue = dayNumber(m.first_due);
      if (num(m.moves) > 0 && inside(firstDue) && firstDue !== due && inside(due)) {
        parts.push(svg("line", { class: "rm-slip", x1: x(firstDue), x2: x(due), y1: y, y2: y }));
        parts.push(svg("path", { class: "rm-first", d: diamondPath(x(firstDue), y, 4.5) }));
      }
      if (inside(due)) parts.push(svg("path", { class: "rm-mark", d: markPath(state, x(due), y) }));
      var g = svg("g", {
        class: "rm-row", "data-state": state, "data-id": m.id, tabindex: "0", role: "button",
        "data-selected": String(ui.rm.selected) === String(m.id) ? "true" : null,
        "aria-label": "#" + m.id + " " + m.title + ", " + label.label + ", due " + fmtDay(m.due) +
          (m.status === "open" ? " (" + whenText(m.days) + ")" : "") + ". Show its card.",
      }, parts);
      g.addEventListener("pointerenter", function () { showMilestoneTip(m, g); });
      g.addEventListener("pointerleave", hideMilestoneTip);
      g.addEventListener("focus", function () { showMilestoneTip(m, g); });
      g.addEventListener("blur", hideMilestoneTip);
      g.addEventListener("click", function () { showMilestoneCard(m.id); });
      g.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); showMilestoneCard(m.id); }
        else if (ev.key === "Escape") hideMilestoneTip();
      });
      return g;
    });
    var picture = svg("svg", { class: "rm-svg", width: totalW, height: height, viewBox: "0 0 " + totalW + " " + height,
      role: "group", "aria-label": "Roadmap timeline, " + plural(rows.length, "milestone") + ", today " + fmtDay(data.today) },
      svg("g", { class: "rm-axis" }, grid), svg("g", { class: "rm-rows" }, marks), svg("g", { class: "rm-now" }, now));
    var scrollLeft = el.scrollLeft;
    replace(el, picture);
    el.scrollLeft = scrollLeft;
  }

  // 0.12.0: a milestone the agent closed as done on its own word, not checked by Ember's code or by you.
  function selfReported(m) {
    return m.status === "done" && m.closed_by === "agent";
  }

  function showMilestoneTip(m, row) {
    var tip = $("rm-tip");
    var state = MILESTONE_STATE[m.horizon] || MILESTONE_STATE[m.status] || { icon: "", label: String(m.status) };
    var due = "Due " + fmtDay(m.due) + (m.status === "open" ? " (" + whenText(m.days) + ")" : "");
    var ended = { done: selfReported(m) ? "Done (self-reported)" : "Done", missed: "Missed", dropped: "Dropped" }[m.status];
    replace(tip, [
      h("p", { class: "rm-tip-title", text: m.title }),
      h("p", { class: "rm-tip-state" }, h("span", { "aria-hidden": "true", text: state.icon + " " }), state.label + " · " + due),
      num(m.moves) > 0 ? h("p", { class: "muted", text: "Moved " + plural(m.moves, "time") + "; first due " + fmtDay(m.first_due) }) : null,
      m.proposed_due ? h("p", { class: "muted", text: agentName() + " proposes " + fmtDay(m.proposed_due) + ": your decision" }) : null,
      h("p", null, h("strong", { text: "Done when: " }), asText(m.measure)),
      ended && m.result ? h("p", null, h("strong", { text: ended + ": " }), asText(m.result)) : null,
    ]);
    tip.hidden = false;
    var card = $("rm-card").getBoundingClientRect();
    var mark = row.querySelector(".rm-mark") || row;
    var at = mark.getBoundingClientRect();
    var left = Math.min(Math.max(8, at.left - card.left - 16), Math.max(8, card.width - tip.offsetWidth - 8));
    tip.style.left = left + "px";
    tip.style.top = (at.bottom - card.top + 8) + "px";
  }

  function hideMilestoneTip() { var tip = $("rm-tip"); if (tip) tip.hidden = true; }

  function showMilestoneCard(id) {
    ui.rm.selected = id;
    hideMilestoneTip();
    Array.prototype.forEach.call($("rm-chart").querySelectorAll(".rm-row"), function (g) {
      if (g.getAttribute("data-id") === String(id)) g.setAttribute("data-selected", "true");
      else g.removeAttribute("data-selected");
    });
    var card = $("roadmap").querySelector('article[data-id="' + String(id) + '"]');
    if (!card) return;
    Array.prototype.forEach.call($("roadmap").querySelectorAll("article[data-selected]"), function (c) { c.removeAttribute("data-selected"); });
    card.setAttribute("data-selected", "true");
    card.scrollIntoView({ block: "center", behavior: "smooth" });
    var title = card.querySelector(".rm-card-title");
    if (title) title.focus({ preventScroll: true });
  }

  var MILESTONE_RESULT = { done: "Evidence", missed: "Why, and what now", dropped: "Why it was dropped" };

  function milestoneView(m) {
    var name = agentName();
    var parent = m.parent_id !== null && m.parent_id !== undefined ? ui.rm.byId[String(m.parent_id)] : null;
    var closed = m.state !== "open";
    var due = fmtDay(m.due) + (closed ? "" : " (" + whenText(m.days) + ")");
    if (num(m.moves) > 0) due += " · moved " + plural(m.moves, "time") + ", first due " + fmtDay(m.first_due);
    return [
      h("div", { class: "item-head" },
        h("h3", { class: "rm-card-title", tabindex: "-1", text: m.title || "Untitled milestone" }),
        chip(MILESTONE_STATE, m.horizon, sentence(String(m.horizon || "unknown"))),
        m.created_by === "owner" ? plainChip("Your milestone") : m.created_by === "code" ? plainChip("Set by Ember's code") : null,
        m.simulated ? testTag() : null),
      h("p", { class: "muted small", text: "#" + m.id + (parent ? " · leads to #" + parent.id + " " + parent.title : "") +
        (m.replaces_id ? " · replaces #" + m.replaces_id : "") }),
      h("dl", { class: "item-grid" },
        h("div", null, h("dt", { text: "Due" }), h("dd", { text: due })),
        h("div", null, h("dt", { text: "Done when" }), h("dd", { class: "pre-line", text: asText(m.measure) })),
        m.checked ? h("div", null, h("dt", { text: "Checked by Ember's code" }), h("dd", { text: asText(m.checked) })) : null,
        closed ? h("div", null, h("dt", { text: (MILESTONE_RESULT[m.state] || "Result") + (selfReported(m) ? " (self-reported)" :
          m.closed_by === "code" && m.metric ? " (checked by Ember's code)" : "") }),
          h("dd", { class: "pre-line", text: asText(m.result) || "–" })) : null,
        m.venture_id ? h("div", null, h("dt", { text: "Venture" }),
          h("dd", { text: "#" + m.venture_id + (m.venture_title ? " " + m.venture_title : "") })) : null,
        m.project_id ? h("div", null, h("dt", { text: "Project" }),
          h("dd", { text: "#" + m.project_id + (m.project_title ? " " + m.project_title : "") })) : null,
        num(m.cycles) > 0 || m.budget_usd ? h("div", null, h("dt", { text: "Worked on" }),
          h("dd", { text: plural(num(m.cycles), "wake cycle") + " · " + usd(m.spent_usd) +
            (m.budget_usd ? " of its budget of " + usd(m.budget_usd) : "") })) : null,
        m.cash_eur || m.owner_hours ? h("div", null, h("dt", { text: "Needs from you" }),
          h("dd", { text: [m.cash_eur ? m.cash_eur + " EUR" : "", m.owner_hours ? m.owner_hours + " h of your time" : ""]
            .filter(Boolean).join(" and ") })) : null,
        m.wait_for && !closed ? h("div", null, h("dt", { text: m.waiting ? "Waiting for" : "Waited for (check due)" }),
          h("dd", { text: asText(m.wait_for) + " · check on " + fmtDay(m.check_at) })) : null),
      milestoneProposal(m),
      milestoneWord(m),
      m.notes ? h("details", { class: "notes" }, h("summary", { text: "Notes" }), h("pre", { class: "notes-text", text: asText(m.notes) })) : null,
      lastDigest(m),
      h("p", { class: "muted small" }, (m.created_by === "owner" ? "Added by " + (m.entered_by || "you") : m.created_by === "code" ? "Set by Ember's code" : "Planned by " + name) + " ",
        timeEl(m.created_at), closed && m.closed_at ? [" · closed ", timeEl(m.closed_at)] : [" · updated ", timeEl(m.updated_at)]),
    ];
  }

  // 0.12.0: the digest Ember's code wrote of the last cycle aimed at a milestone or venture
  function lastDigest(x) {
    if (!x.last_digest) return null;
    return h("details", { class: "notes" }, h("summary", { text: "Its last cycle" }), h("pre", { class: "notes-text", text: asText(x.last_digest) }));
  }

  var MILESTONE_WORDS = { added: "You added this milestone", note: "Your note", drop: "You dropped it",
    accept: "You accepted the new date", reject: "You kept the date" };

  // 0.12.0: the agent can't move the date of your milestone: it proposes one, and you accept it or keep yours.
  function milestoneProposal(m) {
    if (!m.proposed_due || m.state !== "open") return null;
    var name = agentName();
    var days = daysFromToday(m.proposed_due);
    return h("div", { class: "decision" },
      h("p", null, h("strong", { text: name + " proposes a new date: " + fmtDay(m.proposed_due) + (days === null ? "" : " (" + whenText(days) + ")") }),
        " · ", timeEl(m.proposed_at)),
      m.proposed_note ? h("p", { class: "pre-line", text: asText(m.proposed_note) }) : null,
      h("p", { class: "muted small", text: "It stays due " + fmtDay(m.due) + " unless you accept. Moves so far: " + num(m.moves) +
        " of " + roadmapLimit("moves", 2) + "." }));
  }

  function daysFromToday(day) {
    var data = ui.rm.data;
    var today = data && typeof data.today === "string" ? Date.parse(data.today + "T00:00:00Z") : NaN;
    var then = typeof day === "string" ? Date.parse(day + "T00:00:00Z") : NaN;
    return isNaN(today) || isNaN(then) ? null : Math.round((then - today) / 86400000);
  }

  function milestoneWord(m) {
    if (!m.owner_action) return null;
    var name = agentName();
    return h("div", { class: "decision" },
      h("p", null, h("strong", { text: MILESTONE_WORDS[m.owner_action] || sentence(String(m.owner_action)) }), " · ", timeEl(m.owner_at),
        " · " + (m.seen_by_agent ? name + " has seen it." : name + " sees it on its next wake.")),
      m.owner_comment ? h("p", { class: "pre-line", text: asText(m.owner_comment) }) : null);
  }

  function milestoneActions(it, m) {
    var list = [];
    if (m.state === "open" && m.proposed_due) list.push(panelButton(it, "accept", "Accept new date"), panelButton(it, "reject", "Keep the date"));
    list.push(panelButton(it, "note", "Note"));
    if (m.state === "open") list.push(panelButton(it, "drop", "Drop", true));
    return list;
  }

  function milestonePanel(it, mode) {
    var name = agentName();
    var specs = {
      note: { title: "A note for " + name, submit: "Send note",
        intro: [h("p", { text: name + " reads it with the milestone on its next wake." })] },
      drop: { title: "Drop this milestone?", submit: "Drop", danger: true,
        intro: [h("p", { text: name + " stops working toward it. It stays on the roadmap, marked dropped, and so do the open milestones that lead to it: they are dropped with it." })] },
      accept: { title: "Move it to " + fmtDay(it.row.proposed_due) + "?", submit: "Accept new date",
        intro: [h("p", { text: name + " proposed it" + (it.row.proposed_note ? ": " + asText(it.row.proposed_note) : ".") }),
          h("p", { class: "muted small", text: "It is due " + fmtDay(it.row.due) + " now. A date moves " + roadmapLimit("moves", 2) + " times at most." })] },
      reject: { title: "Keep the date " + fmtDay(it.row.due) + "?", submit: "Keep the date",
        intro: [h("p", { text: name + " proposed " + fmtDay(it.row.proposed_due) + (it.row.proposed_note ? ": " + asText(it.row.proposed_note) : ".") })] },
    };
    var spec = specs[mode];
    spec.mode = mode;
    spec.fields = [{ name: "comment", label: mode === "note" ? "Your note" : mode === "drop" ? "Why (optional)" : "A word for " + name + " (optional)",
      rows: 2, max: roadmapLimit("comment", 1000), required: mode === "note", missing: "Write the note." }];
    spec.url = "api/roadmap/" + encodeURIComponent(String(it.row.id)) + "/decide";
    spec.body = function (values) {
      var body = { action: mode };
      var version = num(it.row.owner_version);
      if (!isNaN(version)) body.expected_version = version;
      if ((mode === "accept" || mode === "reject") && it.row.proposed_due) body.proposed_due = it.row.proposed_due;
      if (values.comment) body.comment = values.comment;
      return body;
    };
    spec.done = function (res) {
      loadRoadmap();
      var others = res && isObject(res.data) ? arr(res.data.dropped_with) : [];
      var said = {
        note: "Note sent.",
        drop: "Dropped" + (others.length ? ", and with it " + others.map(function (i) { return "#" + i; }).join(", ") : "") + ".",
        accept: "It is due " + fmtDay(it.row.proposed_due) + " now.",
        reject: "It stays due " + fmtDay(it.row.due) + ".",
      }[mode];
      return said + " " + decisionWake(res, name);
    };
    return spec;
  }

  // ---- Add milestone

  function roadmapLimit(key, fallback) {
    var limits = ui.rm.data && isObject(ui.rm.data.limits) ? ui.rm.data.limits : {};
    var n = num(limits[key]);
    return isNaN(n) ? fallback : n;
  }

  function isoOf(dayN) { return new Date(dayN * 86400000).toISOString().slice(0, 10); }

  function fillMilestoneParents(items) {
    var select = $("rm-form-parent");
    if (isBusy(select)) return;
    var current = select.value;
    var open = items.filter(function (m) { return m.status === "open"; }).sort(function (x, y) {
      return dayNumber(x.due) - dayNumber(y.due) || num(x.id) - num(y.id);
    });
    replace(select, [h("option", { value: "", text: "Nothing (a goal of its own)" })].concat(open.map(function (m) {
      return h("option", { value: String(m.id), text: "#" + m.id + " " + shortTitle(m.title, 50) + " (due " + m.due + ")" });
    })));
    select.value = current && ui.rm.byId[current] && ui.rm.byId[current].status === "open" ? current : "";
    var today = dayNumber(ui.rm.data && ui.rm.data.today);
    if (!isNaN(today)) {
      $("rm-form-due").min = isoOf(today);
      $("rm-form-due").max = isoOf(today + roadmapLimit("ahead_days", 366));
    }
  }

  function setMilestoneFieldError(id, message) {
    var error = $(id + "-error");
    error.textContent = message || "";
    error.hidden = !message;
    if (message) $(id).setAttribute("aria-invalid", "true");
    else $(id).removeAttribute("aria-invalid");
  }

  function milestoneCounter(id, max) {
    var n = $(id).value.trim().length;
    var counter = $(id + "-count");
    counter.textContent = intFmt.format(n) + " / " + intFmt.format(max) + " characters";
    counter.setAttribute("data-over", n > max ? "true" : "false");
  }

  function openMilestoneForm(open) {
    $("rm-form").hidden = !open;
    $("rm-add").setAttribute("aria-expanded", String(open));
    if (open) {
      milestoneCounter("rm-form-title", roadmapLimit("title", 100));
      milestoneCounter("rm-form-measure", roadmapLimit("measure", 300));
      $("rm-form-title").focus();
    } else {
      $("rm-add").focus();
    }
  }

  function submitMilestoneForm() {
    if (ui.rm.saving) return;
    var fields = { title: $("rm-form-title").value.trim(), measure: $("rm-form-measure").value.trim(), due: $("rm-form-due").value };
    var parent = $("rm-form-parent").value;
    var problems = [];
    ["title", "measure", "due"].forEach(function (k) { setMilestoneFieldError("rm-form-" + k, ""); });
    var limits = { title: roadmapLimit("title", 100), measure: roadmapLimit("measure", 300) };
    if (!fields.title) problems.push(["title", "Name the milestone."]);
    else if (fields.title.length > limits.title) problems.push(["title", "Keep it under " + limits.title + " characters."]);
    if (!fields.measure) problems.push(["measure", "Say how " + agentName() + " will know it is reached."]);
    else if (fields.measure.length > limits.measure) problems.push(["measure", "Keep it under " + limits.measure + " characters."]);
    var due = dayNumber(fields.due);
    var today = dayNumber(ui.rm.data && ui.rm.data.today);
    if (isNaN(due)) problems.push(["due", "Pick the date it is due."]);
    else if (!isNaN(today) && (due < today || due > today + roadmapLimit("ahead_days", 366))) {
      problems.push(["due", "Pick a date from today to a year ahead."]);
    }
    if (problems.length) {
      problems.forEach(function (p) { setMilestoneFieldError("rm-form-" + p[0], p[1]); });
      setStatusText("rm-status", problems.length === 1 ? "Please fix the marked field." : "Please fix the marked fields.", "error");
      $("rm-form-" + problems[0][0]).focus();
      return;
    }
    var body = { title: fields.title, measure: fields.measure, due: fields.due };
    if (parent) body.parent_id = num(parent);
    ui.rm.saving = true;
    $("rm-form-save").disabled = true;
    setStatusText("rm-status", "Saving…", "");
    request("POST", "api/roadmap", body).then(function (res) {
      if (res.status === 201) {
        var id = isObject(res.data) ? res.data.id : null;
        ["title", "measure", "due"].forEach(function (k) { $("rm-form-" + k).value = ""; });
        openMilestoneForm(false);
        setStatusText("rm-status", "Added to the roadmap" + (id ? " as #" + id : "") + ". " + agentName() + " sees it on its next wake.", "ok");
        ui.rm.selected = id;
        loadRoadmap();
        return;
      }
      var data = isObject(res.data) ? res.data : {};
      var msg = typeof data.error === "string" && data.error ? endSentence(sentence(data.error)) : "";
      var field = data.field === "parent_id" ? "parent" : data.field;
      if ((res.status === 422 || res.status === 409) && msg && ["title", "measure", "due"].indexOf(field) >= 0) {
        setMilestoneFieldError("rm-form-" + field, msg);
        setStatusText("rm-status", "Please fix the marked field.", "error");
        $("rm-form-" + field).focus();
        return;
      }
      setStatusText("rm-status", msg ? "Nothing was saved: " + lowerFirst(msg) : "Nothing was saved (" + httpError(res).message + ").", "error");
    }).catch(function (err) {
      if (!(err instanceof RequestError)) console.error(err);
      setStatusText("rm-status", "Couldn't reach " + agentName() + ", so it's not clear whether the milestone was saved. Refresh the roadmap before you try again.", "error");
    }).then(function () {
      ui.rm.saving = false;
      $("rm-form-save").disabled = false;
    });
  }

  function initRoadmap() {
    $("rm-refresh").addEventListener("click", loadRoadmap);
    $("rm-add").addEventListener("click", function () { openMilestoneForm($("rm-form").hidden); });
    $("rm-form-cancel").addEventListener("click", function () { openMilestoneForm(false); });
    $("rm-form").addEventListener("submit", function (ev) { ev.preventDefault(); submitMilestoneForm(); });
    $("rm-form").addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") { ev.preventDefault(); openMilestoneForm(false); }
      else if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); submitMilestoneForm(); }
    });
    $("rm-form-title").addEventListener("input", function () { setMilestoneFieldError("rm-form-title", ""); milestoneCounter("rm-form-title", roadmapLimit("title", 100)); });
    $("rm-form-measure").addEventListener("input", function () { setMilestoneFieldError("rm-form-measure", ""); milestoneCounter("rm-form-measure", roadmapLimit("measure", 300)); });
    $("rm-form-due").addEventListener("input", function () { setMilestoneFieldError("rm-form-due", ""); });
    // The timeline fits the page's width: drawn again when the window changes size.
    var pending = null;
    window.addEventListener("resize", function () {
      window.clearTimeout(pending);
      pending = window.setTimeout(function () {
        if (ui.tab === "roadmap" && ui.rm.data) safely("roadmap", function () { renderRoadmapChart(ui.rm.data, arr(ui.rm.data.items)); });
      }, 150);
    });
  }

  // ------------------------------------------------------------------ library (0.12.0)
  // Reference material the owner hands the agent (pasted text, or files) and what the agent learned from it: it
  // studies each document once, within the daily study budget, and keeps the learnings, shown on the document's card.

  var LIBRARY_GROUPS = [
    { key: "waiting", title: "Waiting to be studied", match: function (s) { return s === "waiting"; } },
    { key: "failed", title: "Study stopped", match: function (s) { return s === "failed"; } },
    { key: "done", title: "Studied", match: function () { return true; } },
  ];
  var STUDY_LABELS = { waiting: "Waiting to be studied", done: "Studied", failed: "Study stopped" };

  function libraryLimit(key, fallback) {
    var limits = ui.lib.data && isObject(ui.lib.data.limits) ? ui.lib.data.limits : {};
    var v = num(limits[key]);
    return isNaN(v) ? fallback : v;
  }

  function loadLibrary() {
    var lib = ui.lib;
    if (lib.busy) { lib.again = true; return; }
    lib.busy = true;
    safely("library", renderLibrary);
    request("GET", "api/library").then(function (res) {
      if (!res.ok) throw httpError(res);
      if (!isObject(res.data) || !Array.isArray(res.data.items)) throw new RequestError("malformed", res.data === undefined ? "not JSON" : "the library is missing");
      lib.data = res.data;
      lib.stamp = res.data.stamp;
      lib.error = null;
    }).catch(function (err) {
      if (!(err instanceof RequestError)) console.error(err);
      lib.error = err;
    }).then(function () {
      lib.busy = false;
      safely("library", renderLibrary);
      if (lib.again) { lib.again = false; loadLibrary(); }
    });
  }

  function librarySummary(data) {
    var name = agentName();
    var t = isObject(data.totals) ? data.totals : {};
    var st = isObject(data.study) ? data.study : {};
    var budget = num(st.budget_usd);
    var parts = [
      plural(num(t.documents) || 0, "document") + " (" + intFmt.format(num(t.chars) || 0) + " of " +
        intFmt.format(libraryLimit("library_chars", 5000000)) + " characters)",
      plural(num(t.learnings) || 0, "learning"),
      budget > 0 ? "study budget " + usd(budget) + " a day, " + usd(num(st.spent_today_usd) || 0) + " spent today"
        : "studying is off (the daily study budget in the app's options is 0)",
    ];
    return "Pages and files you give " + name + ". It studies each one once and keeps what it learned, so nothing has to " +
      "be read twice; its work steps get the learnings that fit what they do. " + parts.join(" · ") + ".";
  }

  function renderLibrary() {
    var lib = ui.lib;
    var data = lib.data;
    var name = agentName();
    $("lib-refresh").textContent = lib.busy ? "Refreshing…" : "Refresh";
    if (lib.error && !lib.busy) {
      setStatusText("lib-load-status", "Couldn't load the library (" + errorText(lib.error) + ")." +
        (data ? " What you see is the library loaded earlier." : " Try Refresh."), "error");
    } else {
      setStatusText("lib-load-status", lib.busy && !data ? "Loading the library…" : "", "");
    }
    $("lib-sub").textContent = data ? librarySummary(data) : "Pages and files you give " + name + ": it studies each one once and keeps what it learned.";
    if (!data) return;
    var rows = arr(data.items).filter(function (d) { return isObject(d) && d.id !== undefined; })
      .map(function (d) { return Object.assign({}, d, { status: d.study }); });
    return renderQueue($("library"), {
      kind: "document", rows: rows, groups: LIBRARY_GROUPS,
      empty: emptyState("div", "The library is empty.", "Add pages you find, like Etsy's guides to listings, titles and tags: " +
        name + " studies each one once and keeps what it learned."),
      view: documentView,
      actionKey: function (d) { return String(d.study); },
      actions: documentActions,
      panel: documentPanel,
    });
  }

  function documentView(d) {
    var name = agentName();
    var study;
    if (d.study === "done") study = plural(num(d.learnings) || 0, "learning") + (num(d.study_usd) > 0 ? " · cost " + usd(d.study_usd) : "");
    else if (d.study === "failed") study = "Stopped after " + plural(num(d.studied_parts) || 0, "part") + " of " + (num(d.parts) || 0) + (d.study_note ? ": " + asText(d.study_note) : ".");
    else if (num(d.studied_parts) > 0) study = "Studied " + d.studied_parts + " of " + plural(num(d.parts) || 0, "part") + " so far (" + plural(num(d.learnings) || 0, "learning") + ")";
    else study = name + " studies it in its next wake cycles, within the daily study budget.";
    var serves = [];
    if (d.venture_id) serves.push("Venture #" + d.venture_id + (d.venture_title ? " " + asText(d.venture_title) : ""));
    if (d.project_id) serves.push("Project #" + d.project_id + (d.project_title ? " " + asText(d.project_title) : ""));
    var source = String(d.source || "");
    var sourceNode = /^https?:\/\/\S+$/i.test(source) ? h("a", { href: source, target: "_blank", rel: "noopener noreferrer", text: source }) : source;
    return [
      h("div", { class: "item-head" },
        h("h3", { text: d.title || "Untitled document" }),
        plainChip(STUDY_LABELS[d.study] || sentence(String(d.study))),
        d.simulated ? testTag() : null),
      h("p", { class: "muted small", text: "#" + d.id + " · " + intFmt.format(num(d.chars) || 0) + " characters, " +
        plural(num(d.parts) || 0, "part") + (d.file_name ? " · from " + asText(d.file_name) : "") }),
      h("dl", { class: "item-grid" },
        source ? h("div", null, h("dt", { text: "Source" }), h("dd", null, sourceNode)) : null,
        serves.length ? h("div", null, h("dt", { text: "For" }), h("dd", { text: serves.join(" · ") })) : null,
        d.note ? h("div", null, h("dt", { text: "Your note" }), h("dd", { class: "pre-line", text: asText(d.note) })) : null,
        h("div", null, h("dt", { text: "Study" }), h("dd", { text: study })),
        d.summary ? h("div", null, h("dt", { text: "Summary" }), h("dd", { class: "pre-line", text: asText(d.summary) })) : null),
      documentBox(d),
      h("p", { class: "muted small" }, "Added by " + (d.added_by || "you") + " ", timeEl(d.added_at)),
    ];
  }

  // The document's learnings and text: loaded when the owner opens them (again on every opening, so they are
  // current), and kept open across polls, like a venture's knowledge file.
  function documentBox(d) {
    var box = h("div", { class: "vt-file" });
    fillDocument(box, d, false);
    return box;
  }

  function fillDocument(box, d, focus) {
    var state = ui.lib.docs[String(d.id)] || {};
    var name = agentName();
    var label = num(d.learnings) > 0 ? "what " + name + " learned (" + d.learnings + ") and the text" : "the text";
    var toggle = h("button", { type: "button", class: "btn btn-small", "aria-expanded": state.open ? "true" : "false",
      text: (state.open ? "Hide " : "Show ") + label });
    toggle.addEventListener("click", function () { toggleDocument(box, d); });
    var body = null;
    if (state.open) {
      if (state.busy) body = h("p", { class: "muted small", role: "status", text: "Loading…" });
      else if (state.error) body = h("p", { class: "field-error", text: "Couldn't load it (" + errorText(state.error) + ")." });
      else if (isObject(state.data)) {
        var learned = arr(state.data.learnings).filter(isObject);
        body = [
          learned.length ? h("ul", { class: "lib-learnings" }, learned.map(function (l) {
            return h("li", null, h("strong", { text: asText(l.topic) + ": " }), asText(l.text), h("span", { class: "muted small", text: " (part " + l.part + ")" }));
          })) : null,
          h("details", { class: "notes" }, h("summary", { text: "The text" }), h("pre", { class: "capped notes-text", tabindex: "0", text: asText(state.data.text) })),
        ];
      }
    }
    replace(box, [toggle, body]);
    if (focus) toggle.focus();
  }

  function toggleDocument(box, d) {
    var key = String(d.id);
    var state = ui.lib.docs[key] || (ui.lib.docs[key] = {});
    state.open = !state.open;
    if (state.open) {
      var seq = state.seq = (state.seq || 0) + 1;
      state.busy = true;
      state.error = null;
      request("GET", "api/library/" + encodeURIComponent(key)).then(function (res) {
        if (!res.ok) throw httpError(res);
        if (!isObject(res.data)) throw new RequestError("malformed", "not JSON");
        if (seq === state.seq) state.data = res.data;
      }).catch(function (err) {
        if (!(err instanceof RequestError)) console.error(err);
        if (seq === state.seq) state.error = err;
      }).then(function () {
        if (seq !== state.seq) return;
        state.busy = false;
        if (box.isConnected) fillDocument(box, d, box.contains(document.activeElement));
      });
    }
    fillDocument(box, d, true);
  }

  function documentActions(it, d) {
    var list = [];
    if (d.study === "failed") list.push(panelButton(it, "study", "Study again"));
    list.push(panelButton(it, "remove", "Remove", true));
    return list;
  }

  function documentPanel(it, mode) {
    var name = agentName();
    var specs = {
      remove: { title: "Remove this document?", submit: "Remove", danger: true,
        intro: [h("p", { text: "Its text goes, and " + name + " no longer sees what it learned from it." })] },
      study: { title: "Study it again?", submit: "Study again",
        intro: [h("p", { text: name + " goes on from the part where it stopped, in its next wake cycles, within the daily study budget." })] },
    };
    var spec = specs[mode];
    spec.mode = mode;
    spec.fields = [];
    spec.url = "api/library/" + encodeURIComponent(String(it.row.id)) + "/" + (mode === "remove" ? "remove" : "study");
    spec.body = function () { return {}; };
    spec.done = function () {
      loadLibrary();
      return mode === "remove" ? "Removed." : name + " studies it again in its next wake cycles.";
    };
    return spec;
  }

  // ---- Add to the library

  var LIB_FIELDS = ["text", "files", "title", "source", "note"];

  function setLibraryFieldError(field, message) { setMilestoneFieldError("lib-form-" + field, message); }

  function openLibraryForm(open) {
    $("lib-form").hidden = !open;
    $("lib-add").setAttribute("aria-expanded", String(open));
    if (open) {
      fillAttribution({ control: $("lib-form-for") }, $("lib-form-for").value);
      milestoneCounter("lib-form-text", libraryLimit("document_chars", 300000));
      $("lib-form-text").focus();
    } else {
      $("lib-add").focus();
    }
  }

  function readFileBase64(file) {
    return new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.onload = function () {
        var url = String(reader.result || "");
        resolve(url.slice(url.indexOf(",") + 1));
      };
      reader.onerror = function () { reject(reader.error || new Error("the file can't be read")); };
      reader.readAsDataURL(file);
    });
  }

  function submitLibraryForm() {
    if (ui.lib.saving) return;
    var name = agentName();
    var text = $("lib-form-text").value;
    var files = Array.prototype.slice.call($("lib-form-files").files || []);
    var common = { title: $("lib-form-title").value.trim(), source: $("lib-form-source").value.trim(), note: $("lib-form-note").value.trim() };
    var target = $("lib-form-for").value;
    LIB_FIELDS.forEach(function (k) { setLibraryFieldError(k, ""); });
    var problems = [];
    if (!text.trim() && !files.length) problems.push(["text", "Paste the text, or choose files below."]);
    if (text.trim() && files.length) problems.push(["files", "Add the pasted text and the files one after the other."]);
    if (text.length > libraryLimit("document_chars", 300000) * 2) problems.push(["text", "That is too long for one document: split it."]);
    var tooBig = files.filter(function (f) { return f.size > libraryLimit("file_bytes", 8000000); });
    if (tooBig.length) problems.push(["files", tooBig.map(function (f) { return f.name; }).join(", ") + ": larger than " + byteSize(libraryLimit("file_bytes", 8000000)) + "."]);
    if (common.title.length > libraryLimit("title", 200)) problems.push(["title", "Keep it under " + libraryLimit("title", 200) + " characters."]);
    if (common.source.length > libraryLimit("source", 500)) problems.push(["source", "Keep it under " + libraryLimit("source", 500) + " characters."]);
    if (common.note.length > libraryLimit("note", 1000)) problems.push(["note", "Keep it under " + libraryLimit("note", 1000) + " characters."]);
    if (problems.length) {
      problems.forEach(function (p) { setLibraryFieldError(p[0], p[1]); });
      setStatusText("lib-status", problems.length === 1 ? "Please fix the marked field." : "Please fix the marked fields.", "error");
      $("lib-form-" + problems[0][0]).focus();
      return;
    }
    var base = {};
    if (common.source) base.source = common.source;
    if (common.note) base.note = common.note;
    if (target.indexOf("v:") === 0) base.venture_id = num(target.slice(2));
    if (target.indexOf("p:") === 0) base.project_id = num(target.slice(2));
    var jobs = files.length ? files.map(function (f) { return { file: f }; }) : [{ text: text }];
    var added = [];
    var failed = [];
    ui.lib.saving = true;
    $("lib-form-save").disabled = true;
    var chain = Promise.resolve();
    jobs.forEach(function (job, index) {
      chain = chain.then(function () {
        setStatusText("lib-status", jobs.length > 1 ? "Adding " + (index + 1) + " of " + jobs.length + "…" : "Saving…", "");
        var ready = job.file ? readFileBase64(job.file).then(function (data) {
          return Object.assign({}, base, { file_name: job.file.name, file_data: data }, jobs.length === 1 && common.title ? { title: common.title } : {});
        }) : Promise.resolve(Object.assign({}, base, { text: job.text }, common.title ? { title: common.title } : {}));
        return ready.then(function (body) { return request("POST", "api/library", body); }).then(function (res) {
          var data = isObject(res.data) ? res.data : {};
          if (res.status === 201) { added.push("#" + data.id + " " + asText(data.title)); return; }
          var msg = typeof data.error === "string" && data.error ? endSentence(sentence(data.error)) : httpError(res).message;
          failed.push((job.file ? job.file.name + ": " : "") + msg);
        });
      }).catch(function (err) {
        if (!(err instanceof RequestError)) console.error(err);
        failed.push((job.file ? job.file.name + ": " : "") + "couldn't reach " + name + " (" + errorText(err) + ")");
      });
    });
    chain.then(function () {
      ui.lib.saving = false;
      $("lib-form-save").disabled = false;
      if (added.length) {  // the For choice stays: the next document is often for the same venture
        ["text", "files", "title", "source", "note"].forEach(function (k) { $("lib-form-" + k).value = ""; });
        if (!failed.length) openLibraryForm(false);
        loadLibrary();
      }
      var parts = [];
      if (added.length) parts.push("Added " + added.join(", ") + ". " + name + " studies " + (added.length === 1 ? "it" : "them") + " in its next wake cycles.");
      if (failed.length) parts.push("Not added: " + failed.join("; ") + (/[.!?]$/.test(failed[failed.length - 1]) ? "" : "."));
      setStatusText("lib-status", parts.join(" "), failed.length ? "error" : "ok");
    });
  }

  function initLibrary() {
    $("lib-refresh").addEventListener("click", loadLibrary);
    $("lib-add").addEventListener("click", function () { openLibraryForm($("lib-form").hidden); });
    $("lib-form-cancel").addEventListener("click", function () { openLibraryForm(false); });
    $("lib-form").addEventListener("submit", function (ev) { ev.preventDefault(); submitLibraryForm(); });
    $("lib-form").addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") { ev.preventDefault(); openLibraryForm(false); }
      else if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); submitLibraryForm(); }
    });
    $("lib-form-text").addEventListener("input", function () { setLibraryFieldError("text", ""); milestoneCounter("lib-form-text", libraryLimit("document_chars", 300000)); });
    $("lib-form-files").addEventListener("change", function () { setLibraryFieldError("files", ""); });
  }

  // ------------------------------------------------------------------ tabs

  var TABS = ["overview", "ledger", "projects", "ventures", "roadmap", "library", "activity", "approvals", "inbox", "upgrades", "mind", "workspace", "system", "diagnostics"];
  var MIND_TABS = ["strategy", "lessons", "identity", "journal", "reviews"];

  // Arrow keys, Home and End move between tabs; focus follows the selection.
  function tabKeys(names, index, select) {
    return function (ev) {
      var next = null;
      if (ev.key === "ArrowRight") next = names[(index + 1) % names.length];
      else if (ev.key === "ArrowLeft") next = names[(index - 1 + names.length) % names.length];
      else if (ev.key === "Home") next = names[0];
      else if (ev.key === "End") next = names[names.length - 1];
      if (next) { ev.preventDefault(); select(next, true); }
    };
  }

  function selectTab(name, focus) {
    if (TABS.indexOf(name) < 0) name = "overview";
    ui.tab = name;
    savePref("ember-tab", name);
    TABS.forEach(function (t) {
      var tab = $("tab-" + t);
      var selected = t === name;
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
      $("panel-" + t).hidden = !selected;
    });
    if (focus) $("tab-" + name).focus();
    if (name === "overview" && ui.charts.flow) { ui.charts.flow.resize(); ui.charts.balance.resize(); }
    if (name === "workspace") refreshWorkspace();
    if (name === "ventures") loadVentures();
    if (name === "roadmap") loadRoadmap();
    if (name === "library") loadLibrary();
  }

  function selectMind(name, focus) {
    ui.mind = name;
    MIND_TABS.forEach(function (m) {
      var tab = $("mind-tab-" + m);
      var selected = m === name;
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
    });
    $("mind-body").setAttribute("aria-labelledby", "mind-tab-" + name);
    if (focus) $("mind-tab-" + name).focus();
    if (ui.data) {
      ui.rendered.mind = null;
      section("mind", [ui.data.mind, ui.mind], null, function () { renderMind(ui.data.mind); });
    }
  }

  TABS.forEach(function (t, i) {
    var tab = $("tab-" + t);
    tab.addEventListener("click", function () { selectTab(t, false); });
    tab.addEventListener("keydown", tabKeys(TABS, i, selectTab));
  });

  MIND_TABS.forEach(function (m, i) {
    var tab = $("mind-tab-" + m);
    tab.addEventListener("click", function () { selectMind(m, false); });
    tab.addEventListener("keydown", tabKeys(MIND_TABS, i, selectMind));
  });

  $("table-toggle").addEventListener("click", function () {
    setTableOpen(!ui.tableOpen);
    if (ui.data) section("table", [ui.data.economy && ui.data.economy.days, ui.tableOpen], null, function () { renderTable(ui.data.economy || {}); });
  });


  // Fragment links would resolve against <base href> and reload the page, so handle skip-link in JS.
  $("skip-link").addEventListener("click", function (ev) { ev.preventDefault(); $("main").focus(); });

  // ------------------------------------------------------------------ theme

  var THEMES = ["auto", "light", "dark"];

  function currentTheme() {
    var t = document.documentElement.getAttribute("data-theme");
    return t === "light" || t === "dark" ? t : "auto";
  }

  function applyTheme(theme) {
    if (theme === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", theme);
    savePref("ember-theme", theme === "auto" ? "" : theme);
    // The visible label is the accessible name, so it carries the current theme.
    $("theme-toggle").textContent = "Theme: " + theme;
    destroyCharts();
    ui.rendered.charts = null;
    if (ui.data) section("charts", [ui.data.economy && ui.data.economy.days], null, function () { renderCharts(ui.data.economy || {}); });
  }

  $("theme-toggle").textContent = "Theme: " + currentTheme();
  $("theme-toggle").addEventListener("click", function () {
    applyTheme(THEMES[(THEMES.indexOf(currentTheme()) + 1) % THEMES.length]);
  });

  if (window.matchMedia) {
    var media = window.matchMedia("(prefers-color-scheme: dark)");
    var onChange = function () { if (currentTheme() === "auto") applyTheme("auto"); };
    if (media.addEventListener) media.addEventListener("change", onChange);
  }

  initForms();
  initVentures();
  initRoadmap();
  initLibrary();
  selectTab(ui.tab, false);
  selectMind(ui.mind, false);
  refresh();
})();
