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
  // 0.15.0: a library upload travels base64-encoded (8 MB become about 11 MB), so its timeout grows with its size: a
  // second more per 100 kB (a slow phone connection), at most three minutes.
  var UPLOAD_CHARS_PER_SECOND = 100000;
  var UPLOAD_TIMEOUT_MAX_MS = 180000;
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
    // The agent's files: the list and the open file are loaded when the tab opens and on Refresh (0.26.0: and again
    // after each step of the agent's while the tab is open), never polled otherwise. model: the list as items; kind,
    // sort and view: the browser's choices (kept in this browser), folder, owner (the project or venture, "p:1", "v:2",
    // or "none") and query its others; order and recentOrder: the items as listed (the viewer steps through nav,
    // the list it was opened from); texts: texts once opened (by path, with their time); as: how a kind of text is
    // shown; seenBefore: the newest change seen before this visit; reveal: the list is brought into view once shown.
    ws: { list: null, loadedAt: null, busy: false, error: null, file: null, fileBusy: false, fileError: null, fileSeq: 0,
      model: null, stamp: null, kind: loadPref("ember-ws-kind", "all"), sort: loadPref("ember-ws-sort", "project"),
      view: loadPref("ember-ws-view", "list"), folder: "", owner: "", query: "", shown: 0, order: [], recentOrder: [], nav: [],
      opener: null, texts: {}, as: {}, seenBefore: null, visit: false, pending: null, keepFocus: false, reveal: false },
    // The venture tree: loaded while its tab is open, again whenever the dashboard's ventures_stamp changes.
    // files: venture id -> what Ember learned about it (its knowledge file), loaded when the owner opens it.
    vt: { data: null, byId: {}, stamp: null, loadedAt: null, busy: false, again: false, error: null, selected: null,
      files: {}, saving: false, reveal: null },
    // 0.19.4: the projects' filter (open, closed, all) and sort, kept in this browser. 0.27.0: in the Ventures tab's
    // Running view; reveal: the project to bring into view once the ventures are loaded (its card may be in its venture's).
    pj: { filter: loadPref("ember-projects-filter", "open"), sort: loadPref("ember-projects-sort", "status"), reveal: null },
    // 0.27.0: the Ventures tab's view: pipeline (the tree and the ventures being decided) or running (the backed and
    // live ones with their projects), kept in this browser.
    vtView: loadPref("ember-ventures-view", "pipeline"),
    // The roadmap (0.11.0): loaded while its tab is open, again whenever the dashboard's roadmap stamp changes.
    rm: { data: null, byId: {}, stamp: null, busy: false, again: false, error: null, selected: null, saving: false },
    // 0.34.0: the plan tree (a preview): loaded while its tab is open, again whenever the dashboard's plan stamp
    // changes; selected: the product opened below the tree; view: the network's zoom and position.
    pl: { data: null, stamp: null, busy: false, again: false, error: null, selected: null, view: null, size: null, dragged: false },
    // 0.29.0: the owner's goal: its form is being saved, its removal is being confirmed or sent
    goal: { saving: false, removing: false },
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
    // 0.15.0: the Inbox's older messages, loaded on request: those pages, the message the next one begins before (null:
    // no older ones) and the dashboard's page they follow (a new message moves it: they load again, or a gap would open).
    older: { messages: [], next: null, after: null },
    // 0.19.4: the conversation scrolls inside, kept at the newest while the owner is there (stick); the newest message
    // rendered, and whether one arrived below while the owner read further up.
    chat: { stick: true, lastId: null, newBelow: false },
  };

  // The phase-1 scenario switcher is gone; drop its stored choice.
  try { window.localStorage.removeItem("ember-scenario"); } catch (e) { /* storage unavailable */ }
  // 0.27.0: the Projects tab is the Ventures tab's Running view.
  if (ui.tab === "projects") { ui.tab = "ventures"; ui.vtView = "running"; }

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
    if (n < 1024 * 1024 * 1024) return oneDecimalFmt.format(n / (1024 * 1024)) + " MB";
    return oneDecimalFmt.format(n / (1024 * 1024 * 1024)) + " GB";
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

  var TRIGGERS = { schedule: "scheduled", owner: "woken by you", last_will: "last will", event: "woken by an event" };
  var PHASES = { review: "Daily review", study: "Studying the library", plan: "Plan", act: "Act", reflect: "Reflect", last_will: "Last will" };
  var PURPOSES = { review: "Daily review", plan: "Plan", work: "Work", reflect: "Reflect", research: "Research",
    workshop: "Workshop", draft: "Draft", brainstorm: "Brainstorm", study: "Library study", last_will: "Last will",
    consolidate: "Lessons consolidated", research_check: "Research model check", critic: "Critic" };

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
    upgrades: "Upgrades", mind: "Mind", ventures: "Ventures", roadmap: "Milestones", plan: "Plan", library: "Library",
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
    section("pinterest", [d.integrations, d.mode, minute], ["pinterest-facts", "pinterest-pins"], function () { renderPinterest(d); });
    section("bluesky", [d.integrations, d.mode, minute], ["bluesky-facts", "bluesky-posts"], function () { renderBluesky(d); });
    section("printify", [d.integrations, d.mode, minute], ["printify-facts", "printify-products", "printify-orders"], function () { renderPrintify(d); });
    section("site", [d.integrations, d.mode, minute], ["site-facts", "site-actions", "site-pages"], function () { renderSite(d); });
    section("blog", [d.integrations, d.mode, minute], ["blog-server", "blog-facts", "blog-posts"], function () { renderBlog(d); });
    section("live", [d.integrations, d.mode, minute], ["live-facts", "live-actions"], function () { renderLive(d); });
    safely("website", renderWebsiteCard);
    section("transitions", [d.transitions], ["transitions"], function () { renderTransitions(arr(d.transitions)); });
    section("events", [d.events], ["events"], function () { renderEvents(arr(d.events)); });
    section("header", [agent, d.system.version, d.mode, arr(d.lives).length, economy.simulated_note], null, function () { renderHeader(d, agent); });
    safely("controls", function () { renderControls(agent); });
    section("kpis", [d.agent, d.economy, d.mode, d.now && d.now.started_at, minute], ["kpis"], function () { renderKpis(d, agent); });
    section("goal", [d.roadmap && d.roadmap.goal, agent.name, minute], null, function () { renderGoalStrip(d); });  // 0.29.0
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

    // 0.27.0: renderProjects leaves each of its lists alone while the owner is busy in it.
    section("projects", projectsKey(d), null, function () { return renderProjects(d); });
    // Patched item by item, so an open cycle, its loaded details and their scroll positions survive the fast polls.
    section("activity", [d.activity, minute], null, function () { return renderActivity(arr(d.activity)); });
    // Owner queues: patched card by card, so an open decision form keeps what the owner typed.
    section("approvals", [d.approvals, projectTitles(d), coming, agent.name, emailLimits(d), minute], null, function () {
      return renderApprovals(arr(d.approvals), projectTitles(d), emailLimits(d));
    });
    section("audit", [d.audit, agent.name, agent.state, agent.killed, minute], ["audit-feed", "audit-take-back"], function () { renderAudit(d.audit, agent.state === "killed" || !!agent.killed); });
    section("instructions", [d.instructions, agent.name, coming, !!agent.unavailable, minute], ["instructions-view"], function () {
      renderInstructions(isObject(d.instructions) ? d.instructions : null, agent);
    });
    section("inbox", [d.inbox, d.inbox_before, d.promises_open, d.badges, agent.name, coming, d.mode, minute], ["inbox", "owed"], function () { renderInboxOf(d); });
    section("upgrades", [d.upgrades, coming, agent.name, minute], null, function () { return renderUpgrades(arr(d.upgrades)); });
    // The tree is loaded apart: again when it changed (a venture, or a cycle that ended), while its tab is open.
    if (ui.tab === "ventures" && !ui.vt.busy && d.ventures_stamp !== undefined && d.ventures_stamp !== ui.vt.stamp) loadVentures();
    if (ui.tab === "plan" && !ui.rm.busy && isObject(d.roadmap) && d.roadmap.stamp !== ui.rm.stamp) loadRoadmap();
    if (ui.tab === "plan" && !ui.pl.busy && d.plan_stamp !== undefined && d.plan_stamp !== ui.pl.stamp) loadPlan();
    if (ui.tab === "library" && !ui.lib.busy && isObject(d.library) && d.library.stamp !== ui.lib.stamp) loadLibrary();
    section("mind", [d.mind, ui.mind, minute], ["mind-body"], function () { renderMind(d.mind); });
    // 0.26.0: the workspace again after the agent's steps while its tab is open, and the requests its files are in.
    if (ui.tab === "workspace") safely("workspace", function () { workspaceOnPoll(d); });

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
      list.push({ kind: "error", icon: "✕", title: "Invalid app options. Running in safe mode (built-in defaults, dry run on; your owner user IDs and the kill switch are kept). Fix them in the app's Configuration tab.", items: sys.config_errors });
    }
    // 0.15.0: options that don't fit together are corrected (toward spending less) instead of starting safe mode.
    if (arr(sys.config_corrections).length) {
      list.push({ kind: "error", icon: "✕", title: "Some app options don't fit together, so " + name + " corrected them until you fix them in the app's Configuration tab.", items: sys.config_corrections });
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
        "Each comes down halfway to 1 after 3 calls that didn't need it, and every 7 days; reset them if you know why it happened.",
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
        (a.burn_mode && a.burn_mode !== "explore" ? " · burn mode " + a.burn_mode : "") +
        // 0.15.0: and when it moves down next at today's burn (named as the burn mode in explore too)
        (a.burn_next ? (a.burn_mode && a.burn_mode !== "explore" ? ", " : " · burn mode ") + asText(a.burn_next) : "");
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
    setBadge("badge-pipeline", c.ventures, "◔", "business cases waiting for your decision");  // 0.27.0
    setBadge("badge-roadmap", c.overdue, "▲", "overdue milestones");
    setBadge("badge-roadmap-proposals", c.proposals, "◔", "proposed dates waiting for your decision");
    var sys = d.system;
    setBadge("badge-system", arr(sys.config_errors).length + arr(sys.config_corrections).length + (isObject(sys.database) && sys.database.ok === false ? 1 : 0) + (sys.economy_broken ? 1 : 0), "!", "need attention");
  }

  function isUnread(m) { return isObject(m) && m.sender === "agent" && !m.read_at; }

  // 0.12.0: the owner's decision wakes the agent (the wake_on_decision option); when it acts on it, as the reply says.
  function decisionWake(res, name) {
    var wake = res && isObject(res.data) && typeof res.data.wake === "string" ? res.data.wake : "";
    if (wake === "now" || wake === "soon") ui.fastPollUntil = Date.now() + WAKE_FAST_POLL_MS;
    // 0.15.0: one cycle for what you send and decide, a few minutes after your last click
    return wake === "now" ? name + " is waking up to act on it."
      : wake === "after_cycle" ? name + " acts on it after the cycle it is working on, a few minutes after your last click."
      : wake === "soon" ? name + " wakes up for it soon (a few minutes, or up to 30 after the last such wake), with anything else you send or decide meanwhile."
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
  var LEDGER_NEWEST = 20;

  // 0.15.0: a page of entries (newest first, at most `size`) followed by the older ones kept under it, or null when
  // entries may lie between them. The older pages were merged with each poll's newest 20 as they came, so the entries
  // new ones pushed out of those 20 were in neither list, and the tab skipped them without a sign.
  function ledgerJoin(page, kept, size) {
    if (!kept.length) return page;
    var seen = {};
    page.forEach(function (e) { seen[String(e.id)] = true; });
    var meets = page.length < size || kept.some(function (e) { return seen[String(e.id)]; });
    return meets ? page.concat(kept.filter(function (e) { return !seen[String(e.id)]; })) : null;
  }

  // Loads the entries between the newest page and the older ones kept (the page right under the newest), then shows
  // them together; beyond a page, the older ones go (the button loads them again).
  function fillLedgerGap(newest) {
    if (ui.ledgerFilling) return;
    ui.ledgerFilling = true;
    var kept = arr(ui.ledgerOlder);
    request("GET", "api/ledger?limit=" + LEDGER_PAGE + "&before=" + encodeURIComponent(String(newest[newest.length - 1].id))).then(function (res) {
      if (!res.ok || !isObject(res.data)) throw httpError(res);
      var got = arr(res.data.entries);
      var joined = ledgerJoin(got, kept, LEDGER_PAGE);
      if (joined === null) ui.ledgerEnd = false;
      ui.ledgerOlder = newest.concat(joined === null ? got : joined);
    }).catch(function () {
      ui.ledgerOlder = [];
      ui.ledgerEnd = false;
    }).then(function () {
      ui.ledgerFilling = false;
      if (ui.data) safely("ledger", function () { renderLedger(ui.data); });
    });
  }

  function renderLedger(d) {
    var newest = arr(isObject(d.ledger) ? d.ledger.entries : null);
    var entries = ledgerJoin(newest, arr(ui.ledgerOlder), LEDGER_NEWEST);
    if (entries === null) {
      fillLedgerGap(newest);
      entries = newest;
    } else if (arr(ui.ledgerOlder).length) {
      ui.ledgerOlder = entries;  // everything shown stays together
    }
    ui.ledgerShown = entries;
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
    var more = newest.length >= LEDGER_NEWEST && ui.ledgerEnd !== true;
    if (ui.ledgerFilling) $("ledger-list").appendChild(h("li", { class: "ledger-more muted", text: "Loading the entries in between…" }));
    else if (more) $("ledger-list").appendChild(h("li", { class: "ledger-more" }, olderButton(entries[entries.length - 1].id)));
  }

  function olderButton(lastId) {
    var b = h("button", { type: "button", class: "btn", text: "Show older entries" });
    b.addEventListener("click", function () {
      b.disabled = true;
      b.textContent = "Loading…";
      request("GET", "api/ledger?limit=" + LEDGER_PAGE + "&before=" + encodeURIComponent(String(lastId))).then(function (res) {
        if (!res.ok || !isObject(res.data)) throw httpError(res);
        var got = arr(res.data.entries);
        ui.ledgerOlder = arr(ui.ledgerShown).concat(got);  // 0.15.0: with the entries shown above them
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

  // 0.19.4: the open projects as cards, the closed ones as rows that open; a filter and a sort kept in this browser.
  // The bar with them is outside the list, so a control with focus never holds back the list's next render.
  var PROJECT_FILTERS = ["open", "closed", "all"];
  var PROJECT_SORTS = ["status", "updated", "net", "earned", "spent", "started"];
  var PROJECT_CLOSED = { succeeded: true, failed: true, abandoned: true };

  function projectsKey(d) {
    return [d.projects, arr(d.activity).map(function (c) { return c.cycle_id; }), d.venture_choices, ui.pj.filter, ui.pj.sort,
      Object.keys(runningSlots()), agentName(), Math.floor(Date.now() / 60000)];
  }

  function renderProjectsNow() {
    if (ui.data) section("projects", projectsKey(ui.data), null, function () { return renderProjects(ui.data); });
  }

  function setProjectView(filter, sort) {
    if (filter) { ui.pj.filter = filter; savePref("ember-projects-filter", filter); }
    if (sort) { ui.pj.sort = sort; savePref("ember-projects-sort", sort); }
    syncProjectControls();
    renderProjectsNow();
  }

  function syncProjectControls() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-pj-filter]"), function (b) {
      b.setAttribute("aria-pressed", String(b.getAttribute("data-pj-filter") === ui.pj.filter));
    });
    $("projects-sort").value = ui.pj.sort;
  }

  function initProjects() {
    if (PROJECT_FILTERS.indexOf(ui.pj.filter) < 0) ui.pj.filter = "open";
    if (PROJECT_SORTS.indexOf(ui.pj.sort) < 0) ui.pj.sort = "status";
    Array.prototype.forEach.call(document.querySelectorAll("[data-pj-filter]"), function (b) {
      b.addEventListener("click", function () { setProjectView(b.getAttribute("data-pj-filter"), null); });
    });
    $("projects-sort").addEventListener("change", function () { setProjectView(null, $("projects-sort").value); });
    syncProjectControls();
  }

  function projectNet(p) { var v = num(p.net_usd); return isNaN(v) ? 0 : v; }

  // "+$12.31", "−$1.30", "$0.00": the sign says which way the project moved the balance.
  function netText(value) {
    var v = num(value);
    if (isNaN(v)) return "–";
    return (v > 0 ? "+" : v < 0 ? "−" : "") + usd(Math.abs(v));
  }

  function netTone(value) { var v = num(value); return v > 0 ? "good" : v < 0 ? "critical" : null; }

  function projectSorter(key) {
    function time(iso) { return new Date(iso).getTime() || 0; }
    function money(p, field) { var v = num(p[field]); return isNaN(v) ? 0 : v; }
    return function (x, y) {
      var by = key === "status" ? statusOrder(PROJECT_STATUS, x.status) - statusOrder(PROJECT_STATUS, y.status)
        : key === "net" ? projectNet(y) - projectNet(x)
        : key === "earned" ? money(y, "earned_usd") - money(x, "earned_usd")
        : key === "spent" ? money(y, "spent_usd") + money(y, "expenses_usd") - money(x, "spent_usd") - money(x, "expenses_usd")
        : key === "started" ? time(y.created_at) - time(x.created_at)
        : 0;
      return by || time(y.updated_at) - time(x.updated_at) || num(y.id) - num(x.id);
    };
  }

  // 0.27.0: the Ventures tab's Running view: the projects of each backed or live venture in its card (renderVentures
  // puts the cards there, each with an empty box for them), then the others. A list the owner is busy in (focus or a
  // selection) is left as it is and drawn on the next poll (false).
  function renderProjects(d) {
    var el = $("projects");
    var name = agentName();
    var projects = arr(d.projects).filter(function (p) { return isObject(p) && p.id !== undefined; });
    var slots = runningSlots();
    var running = Object.keys(slots);
    $("projects-bar").hidden = !projects.length;
    var ctx = { open: {}, cycles: {}, ventures: {} };
    Array.prototype.forEach.call($("vt-running").querySelectorAll("details[open][data-id]"), function (x) { ctx.open[x.getAttribute("data-id")] = true; });
    arr(d.activity).forEach(function (c) { if (isObject(c)) ctx.cycles[String(c.cycle_id)] = true; });
    arr(d.venture_choices).forEach(function (v) { if (isObject(v)) ctx.ventures[String(v.id)] = v.title; });
    var open = [];
    var closed = [];
    var byVenture = {};
    var rest = { open: [], closed: [] };
    projects.forEach(function (p) {
      var done = !!PROJECT_CLOSED[p.status];
      (done ? closed : open).push(p);
      var vid = p.venture_id === null || p.venture_id === undefined ? "" : String(p.venture_id);
      var group = slots[vid] ? (byVenture[vid] = byVenture[vid] || { open: [], closed: [] }) : rest;
      (done ? group.closed : group.open).push(p);
    });
    if (projects.length) {
      renderProjectStats(projects, open);
      $("pj-count-open").textContent = intFmt.format(open.length);
      $("pj-count-closed").textContent = intFmt.format(closed.length);
      $("pj-count-all").textContent = intFmt.format(projects.length);
    }
    var sorter = projectSorter(ui.pj.sort);
    var complete = true;
    var inCard = Object.assign({}, ctx, { inCard: true });
    running.forEach(function (vid) {
      var group = byVenture[vid] || { open: [], closed: [] };
      if (isBusy(slots[vid])) { complete = false; return; }
      replace(slots[vid], ventureProjects(group.open.sort(sorter), group.closed.sort(sorter), inCard));
    });
    if (isBusy(el)) return false;
    rest.open.sort(sorter);
    rest.closed.sort(sorter);
    var parts = [];
    if (!projects.length) {
      if (!running.length) parts.push(emptyState("div", "Nothing running yet.", "When you back a venture in Pipeline, " + name + "'s code opens its project, and it shows up here with its hypothesis, its next step, and what it cost and earned. " + name + " starts projects of its own too."));
    } else if (running.length) {
      var shown = projectParts(rest.open, rest.closed, ctx);
      var n = (ui.pj.filter !== "closed" ? rest.open.length : 0) + (ui.pj.filter !== "open" ? rest.closed.length : 0);
      if (n) {
        parts.push(h("h2", { class: "queue-head", text: "Other projects (" + intFmt.format(n) + ")" }),
          h("p", { class: "muted small", text: "Of no venture, or of a venture that isn't backed or live (its chip opens it in Pipeline)." }), shown);
      }
    } else {
      // No running venture (or the ventures aren't loaded): the list as it was before 0.27.0.
      var all = ui.pj.filter === "all";
      if (ui.pj.filter !== "closed") {
        parts.push(all && open.length ? projectsHead("Open", open.length) : null);
        parts.push(open.length ? h("div", { class: "pj-cards" }, open.sort(sorter).map(function (p) { return projectCard(p, ctx); }))
          : emptyState("div", "No open projects.", name + " opens one when it tests an idea." + (closed.length ? " The finished ones are under Closed." : "")));
      }
      if (ui.pj.filter !== "open") {
        parts.push(all && closed.length ? projectsHead("Closed", closed.length) : null);
        parts.push(closed.length ? h("ul", { class: "pj-rows", "aria-label": "Closed projects" }, closed.sort(sorter).map(function (p) { return projectRow(p, ctx); }))
          : all ? null : emptyState("div", "No closed projects yet.", "A project closes when it succeeds, fails or is abandoned."));
      }
    }
    if (ui.pj.filter === "open" && closed.length) parts.push(showClosedButton(closed));
    replace(el, parts);
    return complete;
  }

  // The open projects as cards and the closed ones as rows, as the filter says.
  function projectParts(open, closed, ctx) {
    return [
      ui.pj.filter !== "closed" && open.length ? h("div", { class: "pj-cards" }, open.map(function (p) { return projectCard(p, ctx); })) : null,
      ui.pj.filter !== "open" && closed.length ? h("ul", { class: "pj-rows", "aria-label": "Closed projects" }, closed.map(function (p) { return projectRow(p, ctx); })) : null,
    ].filter(Boolean);
  }

  // 0.27.0: a running venture's projects, in its card, and what the filter leaves out.
  function ventureProjects(open, closed, ctx) {
    var parts = projectParts(open, closed, ctx);
    var hidden = ui.pj.filter === "open" ? closed.length : ui.pj.filter === "closed" ? open.length : 0;
    var kind = ui.pj.filter === "open" ? "closed" : "open";
    var shows = (kind === "closed" ? "Closed" : "Open") + " or All shows " + (hidden === 1 ? "it" : "them");
    var note = !open.length && !closed.length ? "No project yet."
      : !hidden ? null
      : parts.length ? "And " + plural(hidden, kind + " project") + " (" + shows + ")."
      : "No " + ui.pj.filter + " project; " + plural(hidden, kind + " one") + " (" + shows + ").";
    return [h("h4", { class: "vt-projects-head" }, "Projects", h("span", { class: "pj-section-count", text: intFmt.format(open.length + closed.length) }))]
      .concat(parts, note ? h("p", { class: "muted small", text: note }) : []);
  }

  // The project boxes of the running ventures' cards (venture id -> box), once the ventures are loaded.
  function runningSlots() {
    var q = $("vt-running-list").ember;
    var slots = {};
    if (q) Object.keys(q.items).forEach(function (id) { if (q.items[id].projects) slots[id] = q.items[id].projects; });
    return slots;
  }

  function projectsHead(title, n) {
    return h("h2", { class: "pj-section-head" }, title, h("span", { class: "pj-section-count", text: intFmt.format(n) }));
  }

  function showClosedButton(closed) {
    var succeeded = closed.filter(function (p) { return p.status === "succeeded"; }).length;
    var b = h("button", { type: "button", class: "btn btn-ghost pj-show-closed" },
      "Show " + plural(closed.length, "closed project") + (succeeded ? " (" + succeeded + " succeeded)" : ""));
    b.addEventListener("click", function () {
      var seg = document.querySelector('[data-pj-filter="closed"]');
      seg.focus();  // before the render: focus inside the list would hold it back
      setProjectView("closed", null);
    });
    return b;
  }

  function renderProjectStats(projects, open) {
    var earned = 0;
    var cost = 0;
    var net = 0;
    var waiting = 0;
    var counts = {};
    projects.forEach(function (p) {
      earned += num(p.earned_usd) || 0;
      cost += (num(p.spent_usd) || 0) + (num(p.expenses_usd) || 0);
      net += projectNet(p);
    });
    open.forEach(function (p) {
      counts[p.status] = (counts[p.status] || 0) + 1;
      waiting += num(p.pending_approvals) || 0;
    });
    var tally = Object.keys(PROJECT_STATUS).filter(function (k) { return counts[k]; }).map(function (k) {
      return counts[k] + " " + PROJECT_STATUS[k].label.toLowerCase();
    });
    if (waiting) tally.push(plural(waiting, "approval") + " waiting");
    function stat(label, value, sub, tone) {
      return h("div", { class: "pj-stat", "data-tone": tone || null },
        h("dt", { text: label }), h("dd", { class: "pj-stat-value", text: value }), sub ? h("dd", { class: "pj-stat-sub", text: sub }) : null);
    }
    replace($("projects-stats"), [
      stat("Open projects", intFmt.format(open.length), tally.join(" · ") || "none right now"),
      stat("Earned", usd(earned), "revenue recorded for them"),
      stat("Cost", usd(cost), "API calls and expenses"),
      stat("Net", netText(net), "all " + plural(projects.length, "project"), netTone(net)),
    ]);
  }

  function projectCard(p, ctx) {
    var s = PROJECT_STATUS[p.status] || {};
    return h("article", { class: "card project", "data-id": String(p.id), "data-tone": s.tone || null, "aria-labelledby": "pj-title-" + p.id },
      h("div", { class: "pj-chips" }, chip(PROJECT_STATUS, p.status, sentence(p.status || "unknown")),
        num(p.pending_approvals) > 0 ? approvalsButton(p) : null, ctx.inCard ? null : ventureChip(p, ctx),
        num(p.files) > 0 ? filesButton("p:" + p.id, "this project") : null),
      // 0.27.0: in its venture's card, a level below the card's heading (a venture's own card says which it is)
      h(ctx.inCard ? "h5" : "h3", { class: "pj-title", id: "pj-title-" + p.id, text: p.title || "Untitled project" }),
      p.hypothesis ? h("p", { class: "hypothesis", text: p.hypothesis }) : null,
      p.plan_stage ? h("div", { class: "pj-next" }, h("p", { class: "pj-next-label", text: "In the plan" }), h("p", { class: "pj-next-text", text: String(p.plan_stage) })) : null,
      projectMoney(p),
      projectLog(p, ctx, true),
      h("p", { class: "pj-foot" }, plural(p.cycles, "cycle"), " · started ", timeEl(p.created_at), " · updated ", timeEl(p.updated_at)));
  }

  // A closed project: one line (how it ended, what it netted, when), its card's contents when opened.
  function projectRow(p, ctx) {
    var key = "row-" + p.id;
    var chips = [ctx.inCard ? null : ventureChip(p, ctx), num(p.files) > 0 ? filesButton("p:" + p.id, "this project") : null].filter(Boolean);
    return h("li", { class: "pj-row", "data-id": String(p.id) },
      h("details", { "data-id": key, open: ctx.open[key] },
        h("summary", null,
          chip(PROJECT_STATUS, p.status, sentence(p.status || "unknown")),
          h("span", { class: "pj-row-title", text: p.title || "Untitled project" }),
          h("span", { class: "pj-row-net", "data-tone": netTone(p.net_usd), title: "Net: earned less its expenses and API cost", text: netText(p.net_usd) }),
          h("span", { class: "pj-row-when" }, "closed ", timeEl(p.updated_at))),
        h("div", { class: "pj-row-body" },
          chips.length ? h("div", { class: "pj-chips" }, chips) : null,
          p.hypothesis ? h("p", { class: "hypothesis", text: p.hypothesis }) : null,
          projectMoney(p),
          projectLog(p, ctx, false),
          h("p", { class: "pj-foot" }, plural(p.cycles, "cycle"), " · started ", timeEl(p.created_at)))));
  }

  // Earned against cost (API calls plus its expenses), in the money chart's colors, and the net it comes to.
  function projectMoney(p) {
    var earned = num(p.earned_usd) || 0;
    var api = num(p.spent_usd) || 0;
    var expenses = num(p.expenses_usd) || 0;
    var cost = api + expenses;
    var top = Math.max(earned, cost);
    if (!(top > 0)) return null;  // nothing spent or earned yet
    function bar(value, cls) {
      var fill = h("span", { class: "pj-fill " + cls });
      fill.style.width = top > 0 && value > 0 ? Math.max(2, value / top * 100) + "%" : "0";
      return h("span", { class: "pj-track" }, fill);
    }
    return h("div", { class: "pj-money" },
      h("dl", { class: "pj-figures" },
        h("div", null, h("dt", null, h("span", { class: "swatch pj-swatch-earned", "aria-hidden": "true" }), "Earned"), h("dd", { text: usd(earned) })),
        h("div", null, h("dt", null, h("span", { class: "swatch pj-swatch-cost", "aria-hidden": "true" }), "Cost"), h("dd", { text: usd(cost) })),
        h("div", { class: "pj-net", "data-tone": netTone(p.net_usd) }, h("dt", { text: "Net" }), h("dd", { text: netText(p.net_usd) }))),
      top > 0 ? h("div", { class: "pj-bars", "aria-hidden": "true" }, bar(earned, "pj-fill-earned"), bar(cost, "pj-fill-cost")) : null,
      expenses > 0 ? h("p", { class: "pj-cost-note", text: "Cost: API calls " + usd(api) + " and expenses " + usd(expenses) + "." }) : null);
  }

  // A project's notes as the agent's log, newest first: Ember's code starts each note with the cycle that wrote it
  // ("[#c12] ..."). The notes keep their last 2,000 characters, so the oldest may begin without its cycle.
  function noteEntries(notes) {
    var entries = [];
    String(notes || "").split("\n").forEach(function (line) {
      var m = /^\s*\[#c(\d+)\]\s?(.*)$/.exec(line);
      if (m) entries.push({ cycle: m[1], text: m[2] });
      else if (entries.length) entries[entries.length - 1].text += "\n" + line;
      else if (line.trim()) entries.push({ cycle: null, text: line });
    });
    return entries.filter(function (e) { return e.text.trim(); }).reverse();
  }

  function projectLog(p, ctx, collapsible) {
    var entries = noteEntries(p.notes);
    if (!entries.length) return null;
    var list = h("ol", { class: "pj-log-list" }, entries.map(function (e) {
      return h("li", null,
        e.cycle ? h("span", { class: "pj-log-cycle" }, ctx.cycles[e.cycle] ? cycleLink(e.cycle) : "Cycle #" + e.cycle) : null,
        h("span", { class: "pj-log-text", text: e.text.trim() }));
    }));
    if (!collapsible) return h("div", { class: "pj-log" }, h("h4", { class: "small-head", text: "Log" }), list);
    var key = "log-" + p.id;
    return h("details", { class: "pj-log", "data-id": key, open: ctx.open[key] },
      h("summary", null, "Log ", h("span", { class: "pj-log-count", text: "· " + plural(entries.length, "note") })), list);
  }

  function cycleLink(id) {
    var b = h("button", { type: "button", class: "link-button pj-cycle-link", "aria-label": "Cycle #" + id + ": open it in Activity", text: "Cycle #" + id });
    b.addEventListener("click", function () { revealCycle(id); });
    return b;
  }

  function approvalsButton(p) {
    var n = num(p.pending_approvals);
    var b = h("button", { type: "button", class: "chip chip-button", "data-tone": "warning", "aria-label": plural(n, "approval") + " waiting for you: open " + (n === 1 ? "it" : "them") + " in Approvals" },
      h("span", { "aria-hidden": "true", text: "◔" }), plural(n, "approval") + " waiting", h("span", { class: "chip-arrow", "aria-hidden": "true", text: "→" }));
    b.addEventListener("click", function () { revealApproval(p.id); });
    return b;
  }

  function ventureChip(p, ctx) {
    if (p.venture_id === null || p.venture_id === undefined) return null;
    var title = ctx.ventures[String(p.venture_id)];
    if (!title) return h("span", { class: "chip", text: "Venture #" + p.venture_id });
    var b = h("button", { type: "button", class: "chip chip-button", "aria-label": "Venture: " + title + ". Open it in Ventures" },
      h("span", { "aria-hidden": "true", text: "◆" }), h("span", { class: "chip-text", text: title }), h("span", { class: "chip-arrow", "aria-hidden": "true", text: "→" }));
    b.addEventListener("click", function () { revealVenture(p.venture_id); });
    return b;
  }

  // Brings a card or row of another tab into view, marks it for a moment and gives it (or a part of it) focus.
  function revealEl(el, focusEl) {
    if (!el) return;
    var motion = !(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    el.scrollIntoView({ block: "center", behavior: motion ? "smooth" : "auto" });
    el.removeAttribute("data-flash");
    void el.offsetWidth;  // restart the mark when it is shown again
    el.setAttribute("data-flash", "true");
    window.setTimeout(function () { el.removeAttribute("data-flash"); }, 2400);
    var target = focusEl || el;
    if (!target.hasAttribute("tabindex") && !/^(BUTTON|A|SUMMARY|INPUT|SELECT|TEXTAREA)$/.test(target.tagName)) target.setAttribute("tabindex", "-1");
    target.focus({ preventScroll: true });
  }

  function revealApproval(projectId) {
    selectTab("approvals", false);
    var ids = {};
    arr(ui.data && ui.data.approvals).forEach(function (a) {
      if (isObject(a) && a.status === "pending" && num(a.project_id) === num(projectId)) ids[String(a.id)] = true;
    });
    var card = Array.prototype.filter.call($("approvals").querySelectorAll("article[data-id]"), function (c) { return ids[c.getAttribute("data-id")]; })[0];
    revealEl(card, card && card.querySelector("h3"));
  }

  function revealCycle(id) {
    selectTab("activity", false);
    var item = ui.cycles[String(id)];
    if (!item) return;
    var details = item.li.querySelector("details");
    if (details) details.open = true;
    revealEl(details || item.li, details ? details.querySelector("summary") : null);
  }

  // 0.26.0: a project's card (a closed one's row, opened), from the workspace: all projects shown if it is closed and
  // only the open ones were. 0.27.0: in the Ventures tab's Running view, once the ventures are loaded (a running
  // venture's projects are in its card; loadVentures shows it then).
  function revealProject(id) {
    ui.pj.reveal = String(id);
    selectTab("ventures", false);
    selectVentureView("running", false);
    if (ui.vt.data) showProject();
  }

  function showProject() {
    var id = ui.pj.reveal;
    ui.pj.reveal = null;
    if (id === null) return;
    function find() {
      return Array.prototype.filter.call($("vt-running").querySelectorAll(".project[data-id], .pj-row[data-id]"), function (c) {
        return c.getAttribute("data-id") === id;
      })[0];
    }
    var el = find();
    if (!el && ui.pj.filter !== "all") {
      setProjectView("all", null);
      el = find();
    }
    if (!el) return;
    var details = el.tagName === "LI" ? el.querySelector("details") : null;
    if (details) details.open = true;
    revealEl(el, details ? details.querySelector("summary") : el.querySelector(".pj-title"));
  }

  // 0.26.0: a project's or a venture's files, in the workspace.
  function filesButton(owner, what) {
    var b = h("button", { type: "button", class: "chip chip-button", "aria-label": "Files written for " + what + ": open them in Workspace" },
      h("span", { "aria-hidden": "true", text: "▤" }), "Files", h("span", { class: "chip-arrow", "aria-hidden": "true", text: "→" }));
    b.addEventListener("click", function () { showWorkspaceFor(owner); });
    return b;
  }

  function revealVenture(id) {
    ui.vt.selected = id;
    ui.vt.reveal = id;  // shown once the tree is loaded (selectTab loads it), in the view its card is in
    selectTab("ventures", false);
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

  // 0.28.0: each cycle is about one thing: a product line (an ordinary cycle), a line's buyers (a marketing cycle), a
  // venture, or an event.
  var CYCLE_KINDS = { ordinary: "Product line", marketing: "Marketing", venture: "Venture", event: "Event" };

  function cycleKindChip(c) {
    var about = isObject(c.about) ? c.about : null;
    var label = CYCLE_KINDS[c.kind] || "Cycle";
    if (about) {
      var title = about.title ? " " + String(about.title) : "";
      label += " · #" + about.id + (title.length > 41 ? title.slice(0, 40) + "…" : title);
    } else if (c.kind === "ordinary") {
      label = "No line";
    }
    return h("span", { class: "chip", text: label, title: "What cycle #" + c.cycle_id + " was about" });
  }

  function cycleSummary(c) {
    var took = c.ended_at ? duration(c.started_at, c.ended_at) : "";
    var meta = [triggerText(c.trigger), " · ", timeEl(c.started_at)];
    if (took) meta.push(" · took " + took);
    meta.push(" · " + plural(c.calls, "model call") + ", " + plural(c.tools, "tool call"));
    return [
      h("span", { class: "cycle-title" }, h("strong", { text: "Cycle #" + c.cycle_id }), " ", chip(CYCLE_STATUS, c.status, sentence(c.status || "unknown")), " ", cycleKindChip(c)),
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

  // Requests the agent's code carries out itself after approval ("email") or prepares for the owner ("reddit_link",
  // 0.25.0: "kdp_package"). Without a parsed action they are shown like any other request.
  function executorOf(a) {
    if (!isObject(a.action)) return null;
    return a.executor === "email" || a.executor === "reddit_link" || a.executor === "kdp_package" || a.executor === "etsy_listing" || a.executor === "etsy_edit" ||
      a.executor === "pinterest_pin" || a.executor === "pinterest_delete" || a.executor === "pinterest_test_pin" ||
      a.executor === "bluesky_post" || a.executor === "bluesky_delete" ||
      a.executor === "printify_product" || a.executor === "printify_delete" ||
      a.executor === "site_post" || a.executor === "site_links" || a.executor === "site_restore" ||
      a.executor === "live_will" ? a.executor : null;
  }

  // A new Etsy listing, or a change to a live one: Ember's code makes both after approval.
  function isEtsy(a) { var e = executorOf(a); return e === "etsy_listing" || e === "etsy_edit"; }

  // 0.13.0 (Phase E2): a pin, or the owner's Undo of one: Ember's code carries both out after approval. 0.30.2: and the
  // test pin of the owner's Standard access request, in Pinterest's API sandbox.
  function isPinterest(a) { var e = executorOf(a); return e === "pinterest_pin" || e === "pinterest_delete" || e === "pinterest_test_pin"; }

  // 0.19.0: a Bluesky post, or the owner's Undo of one: Ember's code carries both out after approval.
  function isBluesky(a) { var e = executorOf(a); return e === "bluesky_post" || e === "bluesky_delete"; }

  // 0.13.0 (Phase E4): a Printify product, or the owner's Undo of one: Ember's code carries both out after approval.
  function isPrintify(a) { var e = executorOf(a); return e === "printify_product" || e === "printify_delete"; }

  // 0.14.0: a page for the owner's website (a blog post, the link page), or the owner's Undo of an upload: Ember's code
  // uploads it after approval.
  function isSite(a) { var e = executorOf(a); return e === "site_post" || e === "site_links" || e === "site_restore"; }

  // The last will on the live page: Ember's code shows it, exactly as approved, at its next upload (never an unlock).
  function isLive(a) { return executorOf(a) === "live_will"; }

  // What Ember's code carries out exactly as approved: approve or reject, and cancel before it starts.
  function isAsIs(a) { return isPinterest(a) || isBluesky(a) || isPrintify(a) || isSite(a) || isLive(a); }

  var APPROVAL_GROUPS = [
    { key: "pending", title: "Waiting for your decision", match: function (s) { return s === "pending"; } },
    { key: "todo", title: "Approved, to carry out", match: function (s, a) { return isApproved(s) && !(a && (executorOf(a) === "email" || isEtsy(a) || isAsIs(a))); } },
    { key: "sending", label: "Approved emails", title: function () { return "Approved emails, " + agentName() + " sends them"; },
      match: function (s, a) { return isApproved(s) && !!a && executorOf(a) === "email"; } },
    { key: "listing", label: "Approved listings and changes", title: function () { return "Approved Etsy listings and changes, " + agentName() + " makes them"; },
      match: function (s, a) { return isApproved(s) && !!a && isEtsy(a); } },
    { key: "pinning", label: "Approved pins", title: function () { return "Approved pins, " + agentName() + " makes them"; },
      match: function (s, a) { return isApproved(s) && !!a && isPinterest(a); } },
    { key: "posting", label: "Approved posts", title: function () { return "Approved Bluesky posts, " + agentName() + " posts them"; },
      match: function (s, a) { return isApproved(s) && !!a && isBluesky(a); } },
    { key: "printing", label: "Approved products", title: function () { return "Approved Printify products, " + agentName() + " makes them"; },
      match: function (s, a) { return isApproved(s) && !!a && isPrintify(a); } },
    { key: "uploading", label: "Approved pages", title: function () { return "Approved pages for your website, " + agentName() + " uploads them"; },
      match: function (s, a) { return isApproved(s) && !!a && isSite(a); } },
    { key: "showing", label: "Approved for the live page", title: function () { return "Approved for your live page, " + agentName() + "'s code shows it"; },
      match: function (s, a) { return isApproved(s) && !!a && isLive(a); } },
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
      actionKey: function (a) { return [a.status, a.version, executorOf(a) || "", executionStatus(a), a.reddit_url || "", isObject(a.kdp) ? JSON.stringify(arr(a.kdp.files).map(function (f) { return f.unchanged; })) : ""].join("|"); },
      actions: approvalActions,
      panel: function (it, mode) { return approvalPanel(it, mode, email); },
    });
  }

  // Approvals → What Ember's code did (0.13.0): the action journal, the owner's Undo, the daily digest and the switch
  // that takes back every unlock.
  var AUDIT_STATUS = {
    running: { icon: "●", label: "Running", tone: "accent" },
    done: { icon: "✓", label: "Done", tone: "good" },
    simulated: { icon: "◌", label: "Dry run", tone: "" },
    partial: { icon: "!", label: "Partly done", tone: "warning" },
    failed: { icon: "✕", label: "Failed", tone: "critical" },
    unclear: { icon: "!", label: "Unclear", tone: "critical" },
  };

  function renderAudit(audit, killed) {
    var data = isObject(audit) ? audit : {};
    var items = arr(data.feed);
    var digest = isObject(data.digest) ? data.digest : null;
    var unlocks = num(data.unlocks) || 0;
    var held = num(data.held) || 0;
    $("audit-digest").hidden = !digest;
    $("audit-digest").textContent = digest ? "Daily digest for " + digest.text : "";
    $("audit-sub").textContent = (unlocks ? unlocks + (unlocks === 1 ? " unlock stands" : " unlocks stand")
      : "No unlock stands: every request waits for you") + (held ? " · " + held + " held for your veto" : "") +
      (data.unlocks_off ? " · Unlocks are off while " + data.unlocks_off + ": none approves anything, and none can be granted" : "");
    var takeBack = $("audit-take-back");
    takeBack.hidden = !unlocks;
    takeBack.disabled = false;
    takeBack.onclick = function () { takeBackUnlocks(takeBack, unlocks); };
    replace($("audit-feed"), items.length ? items.map(function (e) { return auditItem(e, killed); })
      : [h("li", { class: "muted", text: "Nothing yet: what " + agentName() + "'s code carries out (emails, listings, changes) shows here." })]);
  }

  function auditItem(e, killed) {
    var undo = isObject(e.undo) ? e.undo : {};
    // 0.15.0: an Undo approved before the kill switch went on waits until it is off
    var waits = killed && undone && /^(approved|pending)$/.test(String(undone.status)) ? " It waits until the kill switch is off." : "";
    var undone = isObject(undo.request) ? undo.request : null;
    var button = null;
    if (undo.label && !undo.why_not) {
      button = h("button", { type: "button", class: "btn small", text: "Undo: " + undo.label });
      button.addEventListener("click", function () { undoAction(button, e, undo); });
    }
    var changes = arr(e.changes).map(function (c) {
      return h("li", null, h("strong", { text: c.part + ": " }), String(c.before) + " → " + String(c.after));
    });
    var subject = e.subject ? (String(e["class"]).indexOf("etsy.") === 0 ? "listing #" : "") + e.subject : "";
    return h("li", { class: "audit-item" },
      h("div", { class: "item-head" },
        h("strong", { text: sentence(e.what) + (subject ? " · " + subject : "") }),
        chip(AUDIT_STATUS, e.status, String(e.status)), plainChip(sentence(e.by_text || e.by))),
      h("p", { class: "muted small", text: "Action #" + e.id + " · " + fmtDateTime(e.finished_at || e.started_at) + (e.approval_id ? " · request #" + e.approval_id + (e.request_title ? ": " + e.request_title : "") : "") }),
      changes.length ? h("ul", { class: "audit-changes" }, changes) : null,
      e.note ? h("p", { class: "small pre-line", text: e.note }) : null,
      undone ? h("p", { class: "small" }, h("strong", { text: "Your Undo: " }),
        "request #" + undone.approval_id + " (" + String(undone.status).replace(/_/g, " ") + ")" + (undone.note ? ": " + undone.note : "") + waits) : null,
      button ? h("p", null, button)
        : undo.label && !undone ? h("p", { class: "muted small", text: "Undo isn't possible now: " + undo.why_not + "." }) : null);
  }

  function auditSay(text, error) {
    var status = $("audit-status");
    status.textContent = text;
    if (error) status.setAttribute("data-kind", "error"); else status.removeAttribute("data-kind");
  }

  function undoAction(button, e, undo) {
    var cost = /renew/i.test(undo.label) ? " Etsy charges its listing fee for the renewal." : "";
    if (!window.confirm("Undo: " + undo.label.toLowerCase() + "? " + agentName() + "'s code carries it out in its next round, as a request you approved." + cost)) return;
    button.disabled = true;
    request("POST", "api/actions/" + e.id + "/undo", {}).then(function (res) {
      if (res.ok) {
        auditSay("Undo requested: request #" + (isObject(res.data) ? res.data.approval_id : "?") + ", carried out in the next round.", false);
        refresh();
        return;
      }
      ownerFailure(res, {}, function (msg) { auditSay(msg, true); }, null);
      button.disabled = false;
    }).catch(function (err) {
      if (!(err instanceof RequestError)) console.error(err);
      auditSay("Couldn't reach Ember, so the Undo may or may not be requested.", true);
      button.disabled = false;
    });
  }

  function takeBackUnlocks(button, n) {
    if (!window.confirm("Take back every unlock (" + n + ")? Every request waits for your click again, also those held for your veto and those approved that haven't run yet.")) return;
    button.disabled = true;
    request("POST", "api/autonomy/take_back", {}).then(function (res) {
      if (res.ok) {
        var taken = isObject(res.data) ? num(res.data.taken_back) : n;
        auditSay("Took back " + taken + (taken === 1 ? " unlock" : " unlocks") + ": every request waits for you again.", false);
        refresh();
        return;
      }
      ownerFailure(res, {}, function (msg) { auditSay(msg, true); }, null);
      button.disabled = false;
    }).catch(function (err) {
      if (!(err instanceof RequestError)) console.error(err);
      auditSay("Couldn't reach Ember, so the unlocks may or may not be taken back.", true);
      button.disabled = false;
    });
  }

  var EMAIL_APPROVED = {
    approved: { icon: "◔", label: "Approved, the agent sends it", tone: "accent" },
    approved_with_changes: { icon: "◔", label: "Approved with changes, the agent sends it", tone: "accent" },
  };

  // Where an approved email is ("" for other requests). Approved without an execution row yet: waiting.
  function executionStatus(a) {
    var executor = executorOf(a);
    if (executor !== "email" && executor !== "etsy_listing" && executor !== "etsy_edit" && !isAsIs(a)) return "";
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
    else if (executor === "kdp_package") content = kdpDraft(a);
    else if (executor === "etsy_listing") content = etsyDraft(action, final);
    else if (executor === "etsy_edit") content = etsyChangeDraft(action, payload, final);
    else {
      content = payload ? h("div", { class: "payload-wrap" },
        final ? h("h4", { class: "small-head", text: "The agent's original" }) : null,
        h("p", { class: "payload-note" }, h("span", { "aria-hidden": "true", text: "! " }), "Written by the agent; check it before acting."),
        h("pre", { class: "payload capped", tabindex: "0", text: payload }),
        workspaceButton(wsPathsIn(action))) : null;  // 0.26.0: a pin's picture, a product's design
    }
    var note = null;
    if (todo && !executor) note = "You approved this; carry it out, then mark it done or failed.";
    if (todo && executor === "reddit_link") note = "You approved this; post it on Reddit with the button below, then mark it done (with the link to your post) or failed.";
    if (todo && executor === "kdp_package") note = "You approved this; publish it at KDP with the button below (the files are linked above), then mark it done (with the book's link at Amazon) or failed.";
    return [
      h("div", { class: "item-head" },
        h("h3", { text: a.title || "Untitled request" }), plainChip(APPROVAL_TYPES[a.type] || sentence(String(a.type || "other").replace(/_/g, " "))),
        executor ? plainChip(executor === "email" ? "Email" : isEtsy(a) ? "Etsy" : isPinterest(a) ? "Pinterest" : isBluesky(a) ? "Bluesky" : isPrintify(a) ? "Printify" : isSite(a) ? "Website" : isLive(a) ? "Live page" : executor === "kdp_package" ? "KDP" : "Reddit") : null,
        statusChip, a.simulated ? testTag() : null),
      a.description ? h("p", { class: "pre-line", text: String(a.description) }) : null,
      actionFlags(a.action_class),
      a.veto_until && a.status === "pending" ? h("p", { class: "warn-box" }, h("span", { "aria-hidden": "true", text: "⏱ " }),
        h("strong", { text: "Your unlock: " }), name + "'s code approves it on " + fmtDateTime(a.veto_until) + " unless you decide first.") : null,
      a.status === "pending" && a.decision_comment ? h("p", { class: "warn-box" }, h("span", { "aria-hidden": "true", text: "↩ " }),
        String(a.decision_comment)) : null,  // 0.15.0: an unlock taken back before its approval ran
      a.unlock_ended && a.status === "pending" ? h("p", { class: "warn-box" }, h("span", { "aria-hidden": "true", text: "⏱ " }),
        h("strong", { text: "It waits for you: " }), String(a.unlock_ended) + ".") : null,
      arr(a.never).length && a.status === "pending" ? h("p", { class: "warn-box" }, h("span", { "aria-hidden": "true", text: "🔒 " }),
        h("strong", { text: "Never automatic: " }), arr(a.never).join("; ") + ". It waits for you, whatever you unlocked.") : null,
      arr(a.qa).length ? h("p", { class: "warn-box" }, h("span", { "aria-hidden": "true", text: "! " }),
        h("strong", { text: "QA (Ember's code): " }), arr(a.qa).join("; ") + ".") : null,
      executor === "email" && a.first_contact ? h("p", { class: "warn-box" }, h("span", { "aria-hidden": "true", text: "! " }),
        h("strong", { text: "First email to this address." }), " Cold advertising emails are illegal in Germany (§ 7 UWG). Approve only if this person asked to hear from you.") : null,
      note ? h("p", { class: "todo-note" }, h("span", { "aria-hidden": "true", text: "☐ " }), note) : null,
      a.status === "pending" && executor === "email" ? h("p", { class: "send-note", text: sendNote(email, name) }) : null,
      a.status === "pending" && executor === "reddit_link" ? h("p", { class: "send-note", text: "After you approve, you post it yourself: this card then offers a button that opens Reddit with it filled in, and Copy buttons." }) : null,
      a.status === "pending" && executor === "kdp_package" ? h("p", { class: "send-note", text: "After you approve, you publish it yourself at KDP from your account (Amazon has no API for it): this card then offers a button that opens your KDP Bookshelf, and Copy buttons. KDP charges nothing to publish; Amazon keeps its share of each sale." }) : null,
      a.status === "pending" && executor === "etsy_listing" ? h("p", { class: "send-note", text: "After you approve, " + name + " creates this listing in your Etsy shop itself: a draft, its photos and files, then live. Etsy charges USD 0.20 per listing." }) : null,
      a.status === "pending" && executor === "etsy_edit" ? h("p", { class: "send-note", text: "After you approve, " + name + " makes this change to the live listing itself. Etsy charges nothing for it." }) : null,
      a.status === "pending" && executor === "pinterest_pin" ? h("p", { class: "send-note", text: "After you approve, " + name + " makes this pin on your Pinterest account itself (a new board first, if it names one), exactly as shown. Pinterest charges nothing for it; your Undo deletes it." }) : null,
      a.status === "pending" && executor === "pinterest_test_pin" ? h("p", { class: "send-note", text: "After you approve, " + name + " makes this test pin itself, exactly as shown, on a new board in Pinterest's API sandbox: only you see them there, and your profile doesn't change. This card then shows the pin." }) : null,
      a.status === "pending" && executor === "bluesky_post" ? h("p", { class: "send-note", text: "After you approve, " + name + " posts this on its Bluesky account itself, exactly as shown (with the line saying an AI wrote it and you approved it). Bluesky charges nothing for it; your Undo deletes it." }) : null,
      a.status === "pending" && executor === "printify_product" ? h("p", { class: "send-note", text: "After you approve, " + name + " creates this product at Printify and publishes it to your Etsy shop, exactly as shown, if every price keeps 15% after Etsy's fees, making and shipping; otherwise it deletes it there and says what each price needs. Printify charges you for making and shipping each order; your Undo deletes the product." }) : null,
      a.status === "pending" && (executor === "site_post" || executor === "site_links") ? h("p", { class: "send-note", text: "After you approve, " + name + " uploads exactly the page the preview shows to your website over SFTP" + (executor === "site_post" ? ", and adds it to the blog's list" : "") + ". It touches nothing else on your server; your Undo puts back what it replaced." }) : null,
      a.status === "pending" && executor === "live_will" ? h("p", { class: "send-note", text: "After you approve, " + name + "'s code shows exactly this text on your live page (live.html and en/live.html) from its next upload on, while live_show_memorial is on. Rejected, it is never shown; an unlock never approves it." }) : null,
      executor === "site_post" || executor === "site_links" ? h("p", { class: "form-actions" }, h("a", { class: "btn btn-small", href: "api/blog/preview/" + encodeURIComponent(String(a.id)), target: "_blank", rel: "noopener", text: "Preview the page" }),
        h("span", { class: "muted small", text: " In a tab of its own, with your site's look (loaded from your site); it runs nothing." })) : null,
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

  // 0.25.0: a book for Amazon KDP, which the owner publishes from their account: every field as KDP asks for it, the
  // files as they are in the workspace now (a file changed since it was proposed is flagged), and KDP's AI question.
  function kdpDraft(a) {
    var action = a.action;
    var info = isObject(a.kdp) ? a.kdp : {};
    var paperback = action.format === "paperback";
    var files = arr(info.files);
    return h("div", { class: "draft" },
      h("p", { class: "payload-note" }, h("span", { "aria-hidden": "true", text: "! " }), "Written by the agent; check it before acting."),
      h("dl", { class: "draft-grid" },
        h("dt", { text: "Format" }), h("dd", { text: paperback ? "Paperback" : "Kindle eBook" }),
        h("dt", { text: "Title" }), h("dd", { "data-copy": "kdp-title", text: asText(action.title) || "–" }),
        action.subtitle ? [h("dt", { text: "Subtitle" }), h("dd", { "data-copy": "kdp-subtitle", text: asText(action.subtitle) })] : null,
        h("dt", { text: "Author" }), h("dd", { text: asText(action.author) || "Yours to enter (or set kdp_author in the Configuration tab)" }),
        h("dt", { text: "Language" }), h("dd", { text: asText(action.language) || "–" }),
        h("dt", { text: "Keywords" }), h("dd", null, h("ol", { class: "kdp-list" }, arr(action.keywords).map(function (k) { return h("li", { text: asText(k) }); }))),
        h("dt", { text: "Categories" }), h("dd", null, h("ol", { class: "kdp-list" }, arr(action.categories).map(function (c) { return h("li", { text: asText(c) }); }))),
        h("dt", { text: "Price" }), h("dd", { text: asText(action.price) + " USD at Amazon.com" + (info.royalty ? ": " + asText(info.royalty) : "") }),
        paperback ? [h("dt", { text: "Print" }), h("dd", { text: asText(info.print) + (action.low_content ? "; low-content book (no ISBN needed)" : "") })] : null,
        h("dt", { text: "Files" }), h("dd", null, files.map(function (f, i) {
          var role = f.role === "cover" ? "Cover" : paperback ? "Interior" : "Manuscript";
          return [i ? h("br") : null, role + ": ", h("a", { href: wsProductUrl(String(f.path), false), text: String(f.path) }), " (" + byteSize(num(f.bytes) || 0) + ")",
            f.unchanged === false ? h("strong", { class: "kdp-changed", text: " changed or deleted since it was proposed: check it, or reject and ask for it again" }) : null];
        }), files.length ? [h("br"), workspaceButton(files.map(function (f) { return f.path; }))] : null),
        h("dt", { text: "AI content" }), h("dd", { text: "KDP asks whether AI tools made the book: " + asText(info.ai_answer) })),
      !paperback && files.length > 1 ? h("div", { class: "etsy-photos kdp-cover" }, h("a", { href: wsProductUrl(String(files[1].path), true), target: "_blank", rel: "noopener" },
        h("img", { src: wsProductUrl(String(files[1].path), true), alt: "Cover: " + String(files[1].path), loading: "lazy" }))) : null,
      h("h4", { class: "small-head", text: "Description (as Amazon shows it, with the AI line)" }),
      h("pre", { class: "payload capped", tabindex: "0", "data-copy": "kdp-description", text: asText(info.description || action.description) }));
  }

  // 0.25.0: the owner's KDP Bookshelf, from the server: a real link only when it is a plain kdp.amazon.com https address.
  function kdpLink(value) {
    var url;
    try { url = new URL(String(value || "")); } catch (e) { url = null; }
    if (!url || !/^https:$/.test(url.protocol) || url.hostname !== "kdp.amazon.com" || url.port || url.username || url.password) return null;
    var a = document.createElement("a");
    a.className = "btn btn-primary";
    a.setAttribute("href", url.href);
    a.setAttribute("rel", "noopener noreferrer");
    a.setAttribute("target", "_blank");
    append(a, ["Open your KDP Bookshelf", h("span", { class: "visually-hidden", text: " (opens in a new tab)" })]);
    return a;
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
        }), photos.length || files.length ? [h("br"), workspaceButton(photos.concat(files).map(function (f) { return f.path; }))] : null)),
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
      photos.length || files.length ? h("p", null, workspaceButton(photos.concat(files).map(function (f) { return f.path; }))) : null,
      h("h4", { class: "small-head", text: final ? "The agent's original change" : "The change" }),
      h("pre", { class: "payload capped", tabindex: "0", text: payload }));
  }

  function executionView(a, email) {
    var st = executionStatus(a);
    if (!st) return null;
    if (executorOf(a) === "etsy_listing") return listingExecutionView(a, st);
    if (executorOf(a) === "etsy_edit") return changeExecutionView(a, st);
    if (isPinterest(a)) return pinExecutionView(a, st);
    if (isBluesky(a)) return postExecutionView(a, st);
    if (isPrintify(a)) return productExecutionView(a, st);
    if (isSite(a)) return siteExecutionView(a, st);
    if (isLive(a)) return liveExecutionView(a, st);
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

  // 0.13.0 (Phase E2): a pin, or the owner's Undo of one, as Ember's code carries it out.
  var PIN_EXECUTION = {
    waiting: { icon: "◔", label: "Waiting", tone: "accent" },
    waiting_limit: { icon: "◔", label: "Waiting for tomorrow's limit", tone: "warning" },
    running: { icon: "●", label: "At Pinterest now", tone: "accent" },
    active: { icon: "✓", label: "Pinned", tone: "good" },
    deleted: { icon: "–", label: "Deleted", tone: "" },
    failed: { icon: "✕", label: "Not done", tone: "critical" },
    unclear: { icon: "!", label: "Unclear: check Pinterest", tone: "critical" },
  };

  function pinExecutionView(a, st) {
    var ex = isObject(a.execution) ? a.execution : {};
    var name = agentName();
    var detail;
    if (st === "waiting") detail = [name + (executorOf(a) === "pinterest_delete" ? " deletes it" : " makes it") + " by itself shortly; it checks every few minutes."];
    else if (st === "waiting_limit") detail = [name + " has made its pins for today, so this one waits for tomorrow's limit."];
    else if (st === "running") detail = ex.started_at ? ["At Pinterest since ", timeEl(ex.started_at), "."] : ["At Pinterest now."];
    else if (st === "active") detail = ["Pinned ", timeEl(ex.finished_at || ex.started_at), ex.url && !a.simulated ? [": ", pinterestLink(ex.url, ex.url)] : "."];
    else if (ex.result) detail = [endSentence(sentence(ex.result))];
    else detail = [ex.error ? endSentence(String(ex.error)) : st === "deleted" ? "Deleted at Pinterest." : ""];
    return h("div", { class: "execution", "data-status": st },
      h("p", { class: "execution-head" }, chip(PIN_EXECUTION, st, sentence(st.replace(/_/g, " ")))),
      h("p", { class: "execution-detail" }, detail));
  }

  // 0.19.0: a Bluesky post, or the owner's Undo of one, as Ember's code carries it out.
  var POST_EXECUTION = {
    waiting: { icon: "◔", label: "Waiting", tone: "accent" },
    waiting_limit: { icon: "◔", label: "Waiting for tomorrow's limit", tone: "warning" },
    running: { icon: "●", label: "At Bluesky now", tone: "accent" },
    active: { icon: "✓", label: "Posted", tone: "good" },
    deleted: { icon: "–", label: "Deleted", tone: "" },
    failed: { icon: "✕", label: "Not done", tone: "critical" },
    unclear: { icon: "!", label: "Unclear: check Bluesky", tone: "critical" },
  };

  function postExecutionView(a, st) {
    var ex = isObject(a.execution) ? a.execution : {};
    var name = agentName();
    var detail;
    if (st === "waiting") detail = [name + (executorOf(a) === "bluesky_delete" ? " deletes it" : " posts it") + " by itself shortly; it checks every few minutes." + (ex.result ? " " + endSentence(sentence(ex.result)) : "")];
    else if (st === "waiting_limit") detail = [name + " has made its posts for today, so this one waits for tomorrow's limit."];
    else if (st === "running") detail = ex.started_at ? ["At Bluesky since ", timeEl(ex.started_at), "."] : ["At Bluesky now."];
    else if (st === "active") detail = ["Posted ", timeEl(ex.finished_at || ex.started_at), ex.url && !a.simulated ? [": ", blueskyLink(ex.url, ex.url)] : "."];
    else if (ex.result) detail = [endSentence(sentence(ex.result))];
    else detail = [ex.error ? endSentence(String(ex.error)) : st === "deleted" ? "Deleted at Bluesky." : ""];
    return h("div", { class: "execution", "data-status": st },
      h("p", { class: "execution-head" }, chip(POST_EXECUTION, st, sentence(st.replace(/_/g, " ")))),
      h("p", { class: "execution-detail" }, detail));
  }

  // 0.13.0 (Phase E4): a Printify product, or the owner's Undo of one, as Ember's code carries it out.
  var PRODUCT_EXECUTION = {
    waiting: { icon: "◔", label: "Waiting", tone: "accent" },
    waiting_limit: { icon: "◔", label: "Waiting for tomorrow's limit", tone: "warning" },
    running: { icon: "●", label: "At Printify now", tone: "accent" },
    publishing: { icon: "◔", label: "Published: the Etsy listing follows", tone: "accent" },
    active: { icon: "✓", label: "In the shop", tone: "good" },
    deleted: { icon: "–", label: "Deleted", tone: "" },
    failed: { icon: "✕", label: "Not published", tone: "critical" },
    unclear: { icon: "!", label: "Unclear: check Printify", tone: "critical" },
  };

  function productExecutionView(a, st) {
    var ex = isObject(a.execution) ? a.execution : {};
    var name = agentName();
    var detail;
    if (st === "waiting") detail = [name + (executorOf(a) === "printify_delete" ? " deletes it" : " creates it") + " by itself shortly; it checks every few minutes."];
    else if (st === "waiting_limit") detail = [name + " has made its products for today, so this one waits for tomorrow's limit."];
    else if (st === "running") detail = ex.started_at ? ["At Printify since ", timeEl(ex.started_at), "."] : ["At Printify now."];
    else if (st === "active") detail = ["In the shop", ex.url && !a.simulated ? [": ", etsyLink(ex.url, ex.url)] : "."];
    else if (ex.result) detail = [endSentence(sentence(ex.result))];
    else detail = [ex.error ? endSentence(String(ex.error)) : st === "deleted" ? "Deleted at Printify." : ""];
    return h("div", { class: "execution", "data-status": st },
      h("p", { class: "execution-head" }, chip(PRODUCT_EXECUTION, st, sentence(st.replace(/_/g, " ")))),
      h("p", { class: "execution-detail" }, detail));
  }

  // 0.14.0: a page for the owner's website, or their Undo of an upload, as Ember's code carries it out.
  var SITE_EXECUTION = {
    waiting: { icon: "◔", label: "Waiting", tone: "accent" },
    running: { icon: "●", label: "Uploading", tone: "accent" },
    done: { icon: "✓", label: "Done", tone: "good" },
    partial: { icon: "!", label: "Partly done", tone: "warning" },
    failed: { icon: "✕", label: "Not done", tone: "critical" },
    unclear: { icon: "!", label: "Unclear: check your site", tone: "critical" },
  };

  // A link only to an https address without a user name (the owner's own site, from their options).
  function siteLink(value, text) {
    var url;
    try { url = new URL(String(value)); } catch (e) { url = null; }
    if (!url || !/^https:$/.test(url.protocol) || url.username || url.password) {
      return h("span", { class: "link-text", text: text || String(value || "–") });
    }
    var a = document.createElement("a");
    a.setAttribute("href", url.href);
    a.setAttribute("rel", "noopener noreferrer");
    a.setAttribute("target", "_blank");
    append(a, [text || url.href, h("span", { class: "visually-hidden", text: " (opens in a new tab)" })]);
    return a;
  }

  function siteExecutionView(a, st) {
    var ex = isObject(a.execution) ? a.execution : {};
    var name = agentName();
    var detail;
    if (st === "waiting") detail = [name + (executorOf(a) === "site_restore" ? " undoes it" : " uploads it") + " by itself shortly; it checks every few minutes." + (ex.error ? " The last connection failed: " + endSentence(String(ex.error)) : "")];
    else if (st === "running") detail = ex.started_at ? ["Uploading since ", timeEl(ex.started_at), "."] : ["Uploading now."];
    else if (ex.result) detail = [endSentence(sentence(ex.result)), ex.url && !a.simulated ? [" ", siteLink(ex.url, "Open the page")] : null];
    else detail = [ex.error ? endSentence(String(ex.error)) : ""];
    return h("div", { class: "execution", "data-status": st },
      h("p", { class: "execution-head" }, chip(SITE_EXECUTION, st, sentence(st.replace(/_/g, " ")))),
      h("p", { class: "execution-detail" }, detail));
  }

  // The last will on the live page, as Ember's code carries out the owner's approval.
  var LIVE_EXECUTION = {
    waiting: { icon: "◔", label: "Waiting for the next upload", tone: "accent" },
    done: { icon: "✓", label: "On the live page", tone: "good" },
    failed: { icon: "✕", label: "Not shown", tone: "critical" },
  };

  function liveExecutionView(a, st) {
    var ex = isObject(a.execution) ? a.execution : {};
    var detail;
    if (st === "waiting") detail = [agentName() + "'s code shows it at its next upload of the live page (within 15 minutes, while the live view and live_show_memorial are on)."];
    else if (ex.result) detail = [endSentence(sentence(ex.result)), ex.url && !a.simulated ? [" ", siteLink(ex.url, "Open the page")] : null];
    else detail = [""];
    return h("div", { class: "execution", "data-status": st },
      h("p", { class: "execution-head" }, chip(LIVE_EXECUTION, st, sentence(st.replace(/_/g, " ")))),
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

  // A Reddit post's title or body; 0.25.0: or ``value``, which ``word`` names (a KDP book's fields).
  function copyButton(it, what, label, value, word) {
    var b = h("button", { type: "button", class: "btn", "data-copy-button": what, text: label });
    b.addEventListener("click", function () {
      var action = it.row && isObject(it.row.action) ? it.row.action : {};
      var text = value !== undefined ? asText(value) : asText(what === "title" ? action.title : action.body);
      word = word || (what === "title" ? "title" : action.kind === "comment" ? "comment" : "body");
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
      // 0.25.0: a KDP book's files can't take changes: the owner changes words as they enter them at KDP.
      if (executor === "kdp_package") return [panelButton(it, "approve", "Approve"), panelButton(it, "reject", "Reject", true)];
      // 0.13.0: a pin or a product is approved as it is (the agent proposes a better one after a rejection).
      if (isAsIs(a)) return [panelButton(it, "approve", "Approve"), panelButton(it, "reject", "Reject", true)];
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
      if (isAsIs(a)) {
        var ps = executionStatus(a);
        return ps === "waiting" || ps === "waiting_limit" ? [panelButton(it, "failed", "Cancel", true)] : [];
      }
      var tools = [];
      if (executor === "reddit_link") {
        tools.push(redditLink(a.reddit_url) || h("span", { class: "muted small no-link", text: "No Reddit link (it isn't a www.reddit.com address): copy the text instead." }));
        if (a.action.kind !== "comment") tools.push(copyButton(it, "title", "Copy title"));
        tools.push(copyButton(it, "body", a.action.kind === "comment" ? "Copy comment" : "Copy body"));
      }
      if (executor === "kdp_package") {
        var book = isObject(a.kdp) ? a.kdp : {};
        tools.push(kdpLink(book.bookshelf_url) || h("span", { class: "muted small no-link", text: "Open kdp.amazon.com yourself." }));
        tools.push(copyButton(it, "kdp-title", "Copy title", a.action.title, "title"));
        if (a.action.subtitle) tools.push(copyButton(it, "kdp-subtitle", "Copy subtitle", a.action.subtitle, "subtitle"));
        tools.push(copyButton(it, "kdp-description", "Copy description", book.description || a.action.description, "description"));
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
      } else if (executor === "kdp_package") {
        approveIntro = "After approving, you publish it yourself at KDP from your account: this card then offers a button that opens your KDP Bookshelf, Copy buttons and the files. Change words there if you like, and say so when you mark it done.";
      } else if (executor === "etsy_listing") {
        approveIntro = name + " then creates this listing in your Etsy shop itself, with the photos and files shown, and publishes it (Etsy charges USD 0.20). You hear the result on this card.";
      } else if (executor === "etsy_edit") {
        approveIntro = name + " then changes the live listing at Etsy itself, exactly as shown (Etsy charges nothing for it). You hear the result on this card.";
      } else if (executor === "pinterest_pin") {
        approveIntro = name + " then makes this pin on your Pinterest account itself, exactly as shown (a new board first, if it names one). You hear the result on this card; your Undo deletes it.";
      } else if (executor === "pinterest_test_pin") {
        approveIntro = name + " then makes this test pin itself, exactly as shown, on a new board in Pinterest's API sandbox, where only you see them. You see the pin on this card within a minute or two.";
      } else if (executor === "bluesky_post") {
        approveIntro = name + " then posts this on its Bluesky account itself, exactly as shown. You hear the result on this card; your Undo deletes it.";
      } else if (executor === "printify_product") {
        approveIntro = name + " then creates this product at Printify and publishes it to your Etsy shop, exactly as shown, if every price keeps its margin after what Printify charges to make and ship it. You hear the result on this card; your Undo deletes it.";
      } else if (executor === "site_post" || executor === "site_links") {
        approveIntro = name + " then uploads exactly the page you previewed to your website" + (executor === "site_post" ? " and adds it to the blog's list" : "") + ". You hear the result on this card; your Undo puts back what it replaced.";
      } else if (executor === "live_will") {
        approveIntro = name + "'s code then shows exactly this text on your live page at its next upload. To take it down later, switch live_show_memorial off.";
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
        if (executor === "kdp_package") return "Approved. Publish it at KDP with the button below, then mark it done (with the book's link) or failed.";
        if (executor === "etsy_listing") return (mode === "approve_with_changes" && st !== "approved" ? "Approved with your changes. " : "Approved. ") + name + " creates the listing itself; this card shows when it's live.";
        if (executor === "etsy_edit") return (mode === "approve_with_changes" && st !== "approved" ? "Approved with your changes. " : "Approved. ") + name + " changes the listing itself; this card shows when it's done.";
        if (executor === "pinterest_pin") return "Approved. " + name + " makes the pin itself; this card shows when it's live.";
        if (executor === "pinterest_test_pin") return "Approved. " + name + " makes the test pin in Pinterest's sandbox itself; this card shows it within a minute or two.";
        if (executor === "bluesky_post") return "Approved. " + name + " posts it itself; this card shows when it's live.";
        if (executor === "printify_product") return "Approved. " + name + " creates the product itself; this card shows when it's in the shop.";
        if (executor === "site_post" || executor === "site_links") return "Approved. " + name + " uploads the page itself; this card shows when it's online.";
        if (executor === "live_will") return "Approved. " + name + "'s code shows it on your live page at its next upload; this card shows when it's there.";
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
    if (isAsIs(a) && failed) {
      return {
        mode: mode, title: "Cancel this?", submit: "Cancel", danger: true, cancelLabel: "Keep it",
        intro: [h("p", { text: name + " won't carry it out. The request is marked failed, and " + name + " sees that on its next wake." })],
        fields: [{ name: "result_note", label: "Why (" + name + " reads it)", rows: 2, max: 2000, required: true, value: isPrintify(a) ? "Cancelled before it reached Printify." : isBluesky(a) ? "Cancelled before it reached Bluesky." : isSite(a) ? "Cancelled before it was uploaded." : isLive(a) ? "Cancelled before it was shown." : "Cancelled before it reached Pinterest.", missing: "Say why you cancel it." }],
        url: url + "close",
        body: function (v) { return { outcome: "failed", expected_version: version, result_note: v.result_note }; },
        done: function () { return "Cancelled. " + name + " won't carry it out."; },
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
        { name: "result_link", label: executor === "reddit_link" ? "Link to your Reddit post (optional)" : executor === "kdp_package" ? "Link to the book at Amazon (optional)" : "Link to the result (optional)", inputmode: "url", check: linkProblem,
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

  // 0.33.0: ``add``, one of the owner's messages to keep as a standing instruction (Keep as instruction), at the end
  function openInstructions(add) {
    if (ui.instructions.editing || laterTitle()) return;
    var adding = typeof add === "string" ? add.trim() : "";  // the Edit button passes its click event
    var current = currentInstructions();
    ui.instructions.editing = true;
    $("instructions-form").hidden = false;
    var box = $("instructions-text");
    if (adding) box.value = (current ? current.text + "\n" : "") + "- " + adding;
    else box.value = current ? current.text : INSTRUCTIONS_SUGGESTION;
    box.placeholder = INSTRUCTIONS_SUGGESTION;
    instructionsError("");
    instructionsCount();
    setInstructionsStatus(adding ? "Your message is added at the end: shorten it as you like. Nothing is saved until you press Save."
      : current ? "" : "Filled in with the suggestion: change it as you like. Nothing is saved until you press Save.", "");
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
  function promiseState(o) {
    if (o.status !== "open") return "closed";
    return o.due && o.due < todayIso() ? "overdue" : "open";
  }

  function promiseMeta(o) {
    var state = promiseState(o);
    return state === "closed" ? "Closed" + (o.result ? ": " + asText(o.result) : "")
      : (state === "overdue" ? "Overdue: it was due " : "Due ") + fmtDay(o.due);
  }

  function promiseItem(o) {
    var state = promiseState(o);
    return h("li", { class: "promise", "data-state": state },
      h("span", { class: "promise-icon", "aria-hidden": "true", text: state === "closed" ? "✓" : state === "overdue" ? "!" : "◌" }),
      h("span", { class: "promise-body" },
        h("span", { class: "promise-what", text: asText(o.what) }),
        h("span", { class: "promise-meta", text: promiseMeta(o) })));
  }

  // 0.15.0: the dashboard brings the newest messages and every one of yours still waiting for an answer; older ones
  // load on request, a page at a time.
  var INBOX_PAGE = 30;
  var RUN_GAP_MS = 10 * 60 * 1000;  // messages of one sender this close together read as one run

  function renderInboxOf(d) {
    renderInbox(arr(d.inbox), (d.agent || standInAgent(d)).name, badgeCounts(d).unread, isDryRun(d), d.inbox_before);
    renderOwed(d);
  }

  function localDay(date) {
    return date.getFullYear() + "-" + String(date.getMonth() + 1).padStart(2, "0") + "-" + String(date.getDate()).padStart(2, "0");
  }

  // "Today", "Yesterday", "Friday, October 2" (this year), "Fri, Oct 2, 2025".
  var dayHeadFmt = new Intl.DateTimeFormat(undefined, { weekday: "long", month: "long", day: "numeric" });
  function dayHead(date) {
    var today = new Date();
    var days = Math.round((parseDay(localDay(today)) - parseDay(localDay(date))) / 86400000);
    if (days === 0 || days === 1) return sentence(relFmt.format(-days, "day"));
    return date.getFullYear() === today.getFullYear() ? dayHeadFmt.format(date) : dayLongFmt.format(date);
  }

  function renderInbox(messages, agentName, unread, dry, before) {
    var el = $("inbox");
    var box = $("chat-scroll");
    var name = agentName || "Ember";
    if (ui.older.after !== null && ui.older.after !== before) ui.older = { messages: [], next: null, after: null };
    var seen = {};
    var all = messages.concat(ui.older.messages).filter(function (m) {
      if (!isObject(m) || seen[String(m.id)]) return false;
      seen[String(m.id)] = true;
      return true;
    });
    var next = num(ui.older.after === null ? before : ui.older.next);  // NaN: no older messages
    var sorted = all.sort(function (x, y) { return byDate("created_at")(x, y) || num(x.id) - num(y.id); });
    var unreadRows = sorted.filter(isUnread);
    var waiting = sorted.filter(function (m) { return m.sender === "owner" && !m.removed && !m.answered_by; }).length;
    $("inbox-sub").textContent = (unread ? plural(unread, "unread message") + " from " + name : "No unread messages") +
      (waiting ? " · " + plural(waiting, "message") + " of yours waiting for an answer" : "") + ".";
    var mark = $("inbox-mark-read");
    mark.hidden = !unreadRows.length || !!laterTitle();
    // Where the owner was reading: the first message in view and how far down it was, kept across the render.
    var anchor = null;
    if (box.clientHeight > 0 && !ui.chat.stick) {
      var first = Array.prototype.filter.call(el.children, function (li) {
        return li.hasAttribute("data-id") && li.offsetTop + li.offsetHeight > box.scrollTop;
      })[0];
      if (first) anchor = { id: first.getAttribute("data-id"), offset: first.offsetTop - box.scrollTop };
    }
    if (!sorted.length) {
      replace(el, emptyState("li", "No messages yet.", name + " writes here when it has a question or news for you. You can write first, too."));
    } else {
      var byId = {};
      sorted.forEach(function (m) { byId[String(m.id)] = m; });
      var items = [next > 0 ? olderMessagesButton(next) : null];
      var prev = null;
      var newShown = false;
      sorted.forEach(function (m) {
        var at = new Date(m.created_at);
        var known = validDate(at);
        if (known && (!prev || localDay(at) !== localDay(prev.at))) {
          items.push(h("li", { class: "chat-day" }, h("span", { text: dayHead(at) })));
          prev = null;
        }
        if (!newShown && isUnread(m)) {
          newShown = true;
          items.push(h("li", { class: "chat-new" }, h("span", { text: unreadRows.length > 1 ? plural(unreadRows.length, "new message") : "New message" })));
        }
        var run = !!(prev && prev.m.sender === m.sender && known && at - prev.at < RUN_GAP_MS);
        items.push(messageItem(m, name, byId, dry, run));
        prev = known ? { m: m, at: at } : null;
      });
      replace(el, items.filter(function (item) { return item; }));
    }
    var lastId = sorted.length ? String(sorted[sorted.length - 1].id) : null;
    if (ui.chat.stick) scrollChatToEnd();
    else if (anchor) {
      var li = el.querySelector('li[data-id="' + anchor.id + '"]');
      if (li) box.scrollTop = li.offsetTop - anchor.offset;
    }
    if (!ui.chat.stick && ui.chat.lastId !== null && lastId !== ui.chat.lastId) ui.chat.newBelow = true;
    ui.chat.lastId = lastId;
    syncChatJump();
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

  // One message: who and when (said once for a run of messages close together), the text, what it promised (the
  // agent's) or whether the agent has seen and answered it (the owner's).
  function messageItem(m, name, byId, dry, run) {
    var fromOwner = m.sender === "owner";
    var who = fromOwner ? (m.entered_by ? String(m.entered_by) : "You") : name;
    var at = new Date(m.created_at);
    var when = validDate(at) ? timeEl(m.created_at, shortTimeFmt.format(at)) : timeEl(m.created_at);
    return h("li", { class: "msg", "data-from": fromOwner ? "owner" : "agent", "data-id": String(m.id),
      "data-unread": isUnread(m) ? "true" : null, "data-run": run ? "true" : null },
      h("p", { class: "who" }, h("span", { class: "who-name", text: who }), " ", when,
        isUnread(m) ? [" ", h("span", { class: "chip", "data-tone": "accent" }, h("span", { "aria-hidden": "true", text: "●" }), "Unread")] : null,
        m.simulated && !dry ? [" ", testTag()] : null),
      h("div", { class: "bubble" },
        h("span", { class: "msg-text", "data-removed": m.removed ? "true" : null, text: asText(m.text) }),
        !fromOwner && arr(m.promises).length ? h("ul", { class: "msg-promises", "aria-label": "Promised in this message" },
          arr(m.promises).filter(isObject).map(promiseItem)) : null),
      fromOwner ? ownerStatus(m, name, byId) : null);
  }

  function ownerStatus(m, name, byId) {
    var parts = [m.seen_by_agent
      ? h("span", { class: "msg-state", "data-state": "done" }, h("span", { "aria-hidden": "true", text: "✓ " }), "Seen", h("span", { class: "visually-hidden", text: " by " + name }))
      : h("span", { class: "msg-state", "data-state": "waiting" }, h("span", { "aria-hidden": "true", text: "◌ " }), "Not yet seen by " + name)];
    if (m.seen_by_agent && !m.removed) {
      var answer = m.answered_by ? byId[String(m.answered_by)] : null;
      if (answer) {
        var b = h("button", { type: "button", class: "link-button msg-state", "data-state": "done", "aria-label": "Answered by " + name + ": show the answer" },
          h("span", { "aria-hidden": "true", text: "✓ " }), "Answered");
        b.addEventListener("click", function () { revealMessage(answer.id); });
        parts.push(b);
      } else if (m.answered_by) {
        parts.push(h("span", { class: "msg-state", "data-state": "done" }, h("span", { "aria-hidden": "true", text: "✓ " }), "Answered by " + name));
      } else {
        parts.push(h("span", { class: "msg-state", "data-state": "waiting", title: name + " keeps it in its plans until it answers" },
          h("span", { "aria-hidden": "true", text: "◌ " }), "Not answered yet"));
      }
    }
    if (!m.removed) parts.push(keepButton(m), removeButton(num(m.id)));
    return h("p", { class: "msg-status" }, parts);
  }

  // 0.33.0: one of the owner's messages kept as a standing instruction: a message leaves the agent's plans once it is
  // answered, the instructions are in every plan (live, "Bluesky in English only" was acknowledged, then forgotten).
  function keepButton(m) {
    var btn = h("button", { type: "button", class: "link-button", "data-keep": String(num(m.id)), text: "Keep as instruction",
      title: "Add it to your standing instructions: " + agentName() + " reads them in every plan, and a message only until it is answered" });
    btn.addEventListener("click", function () { openInstructions(asText(m.text)); });
    return btn;
  }

  // A message brought into view in the conversation, marked for a moment; focus goes to the conversation, so the
  // next update isn't held back by focus in the list.
  function revealMessage(id) {
    var li = $("inbox").querySelector('li[data-id="' + String(id) + '"]');
    if (!li) return;
    ui.chat.stick = false;
    revealEl(li.querySelector(".bubble") || li, $("chat-scroll"));
  }

  function scrollChatToEnd() {
    var box = $("chat-scroll");
    box.scrollTop = box.scrollHeight;
  }

  function syncChatJump() {
    var jump = $("chat-jump");
    jump.hidden = ui.chat.stick;
    jump.textContent = ui.chat.newBelow ? "↓ New messages" : "↓ Latest";
  }

  function chatScrolled() {
    var box = $("chat-scroll");
    if (!box.clientHeight) return;  // hidden: the Inbox tab isn't open
    ui.chat.stick = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    if (ui.chat.stick) ui.chat.newBelow = false;
    syncChatJump();
  }

  // Beside the conversation: the owner's messages the agent hasn't answered and what it promised and hasn't done.
  function renderOwed(d) {
    var name = (d.agent || standInAgent(d)).name || "Ember";
    var loaded = {};
    arr(d.inbox).concat(ui.older.messages).forEach(function (m) { if (isObject(m)) loaded[String(m.id)] = m; });
    var questions = Object.keys(loaded).map(function (k) { return loaded[k]; }).filter(function (m) {
      return m.sender === "owner" && !m.removed && !m.answered_by;
    }).sort(byDate("created_at"));
    var promises = arr(d.promises_open).filter(isObject).slice().sort(function (x, y) { return String(x.due).localeCompare(String(y.due)); });
    $("owed-title").textContent = "Waiting on " + name;
    $("owed-sub").textContent = questions.length || promises.length
      ? [questions.length ? plural(questions.length, "message") + " to answer" : null,
        promises.length ? plural(promises.length, "open promise") : null].filter(function (x) { return x; }).join(" · ") + "."
      : "Nothing: " + name + " has answered your messages and closed its promises.";
    function jumpItem(messageId, body, label) {
      if (!loaded[String(messageId)]) return h("div", { class: "owed-item" }, body);
      var b = h("button", { type: "button", class: "owed-item", "aria-label": label }, body);
      b.addEventListener("click", function () { revealMessage(messageId); });
      return b;
    }
    replace($("owed"), [
      questions.length ? [h("h3", { class: "small-head", text: "Your messages" }), h("ul", { class: "owed-list" }, questions.map(function (m) {
        var text = asText(m.text);
        var meta = (m.seen_by_agent ? "Seen, not answered yet" : "Not seen yet") + " · sent " + relTime(m.created_at);
        return h("li", null, jumpItem(m.id, [h("span", { class: "owed-text", text: text }), h("span", { class: "owed-meta", text: meta })],
          "Your message: " + text.slice(0, 80) + ". " + meta + ". Show it"));
      }))] : null,
      promises.length ? [h("h3", { class: "small-head", text: "Promises" }), h("ul", { class: "owed-list" }, promises.map(function (o) {
        var state = promiseState(Object.assign({ status: "open" }, o));
        var meta = promiseMeta(Object.assign({ status: "open" }, o));
        return h("li", { "data-state": state }, jumpItem(o.message_id,
          [h("span", { class: "owed-text", text: asText(o.what) }), h("span", { class: "owed-meta", text: meta })],
          "Promise: " + asText(o.what) + ". " + meta + ". Show the message"));
      }))] : null,
    ]);
  }

  function growComposer() {
    var box = $("composer-text");
    box.style.height = "auto";
    box.style.height = Math.min(box.scrollHeight + 2, Math.max(120, Math.round(window.innerHeight * 0.4))) + "px";
  }

  function olderMessagesButton(before) {
    var b = h("button", { type: "button", class: "btn", text: "Show older messages" });
    b.addEventListener("click", function () {
      var after = ui.older.after === null && ui.data ? ui.data.inbox_before : ui.older.after;
      b.disabled = true;
      b.textContent = "Loading…";
      request("GET", "api/inbox?limit=" + INBOX_PAGE + "&before=" + encodeURIComponent(String(before))).then(function (res) {
        if (!res.ok || !isObject(res.data)) throw httpError(res);
        ui.older = { messages: ui.older.messages.concat(arr(res.data.messages)), next: res.data.before, after: after };
        if (ui.data) safely("inbox", function () { renderInboxOf(ui.data); });  // at once, though the button has focus
      }).catch(function (err) {
        b.disabled = false;
        b.textContent = "Show older messages (failed: " + errorText(err) + ")";
      });
    });
    return h("li", { class: "inbox-more" }, b);
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
        growComposer();
        ui.chat.stick = true;  // the conversation shows the message just sent
        // With the wake_on_message option the message wakes the agent (0.15.0: one cycle a few minutes after your last
        // message or decision). Without it (or while paused, ...) it waits for the next wake.
        var wake = isObject(res.data) && typeof res.data.wake === "string" ? res.data.wake : "";
        var when = wake === "now" ? " is waking up to read it."
          : wake === "after_cycle" ? " reads it after the cycle it is working on, a few minutes after your last message."
          : wake === "soon" ? " wakes up for it soon (a few minutes, or up to 30 after the last such wake), with anything else you send meanwhile."
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
        ui.older = { messages: [], next: null, after: null };  // they held its text: they load again on request
        ui.rendered.inbox = null;
        refresh();
      }).catch(function (err) {
        status.textContent = "Couldn't remove the text (" + errorText(err) + ").";
        status.setAttribute("data-kind", "error");
        btn.disabled = false;
      });
    });
    return btn;
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
    } else if (ui.mind === "playbook") {
      renderPlaybook(body, mind.playbook);
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
        reviewLine("Milestones", r.roadmap),
        reviewLine("Lesson", r.lesson),
        r.scorecard ? h("details", { class: "review-numbers" }, h("summary", { text: "The numbers it judged" }),
          h("pre", { class: "mind-text", text: asText(r.scorecard) })) : null);
    })));
  }

  function reviewLine(label, text) {
    text = asText(text);
    return text ? h("p", { class: "review-line" }, h("strong", { text: label + ": " }), text) : null;
  }

  var CONFIDENCE = {
    established: { icon: "✓", label: "Established", tone: "good" },
    hypothesis: { icon: "?", label: "Hypothesis", tone: "" },
    disputed: { icon: "!", label: "Disputed", tone: "warning" },
  };

  // Mind → Playbook (0.30.0): the agent's rulebook, the principles Ember's code keeps from its own cases, each with
  // the cases for and against it, and this week's look; older servers send none.
  function renderPlaybook(body, playbook) {
    if (!isObject(playbook)) {
      replace(body, h("p", { class: "muted", text: "This version of Ember keeps no playbook yet." }));
      return;
    }
    var active = arr(playbook.principles);
    var retired = arr(playbook.retired);
    var cases = Number(playbook.cases) || 0;
    var early = Number(playbook.cases_too_early) || 0;
    var parts = [h("p", { class: "muted small", text: agentName() + "'s rulebook, drawn from its own cases. Each " +
      "day the lessons of its retrospectives join it as hypotheses; three cases that agree make a principle " +
      "established, a case against it makes it disputed, and Ember's code retires what no case confirms for weeks. " +
      "Once a week its weekly look confirms, merges and retires them, and chooses the week's focus lines." })];
    if (isObject(playbook.weekly)) parts.push(weeklyLook(playbook.weekly));
    if (!active.length) {
      parts.push(h("p", { class: "muted", text: "No principles yet: " + plural(cases, "case") + " so far" +
        (early ? " (" + early + " of them too early to be evidence)" : "") + ". A principle needs a case that " +
        "settled with a cause: a bet, a listing bar, a closed project, a parked venture or a request you rejected." }));
    } else {
      parts.push(h("ol", { class: "journal reviews" }, active.map(principleItem)));
    }
    if (retired.length) {
      parts.push(h("details", { class: "review-numbers" }, h("summary", { text: "Retired lately (" + retired.length + ")" }),
        h("ol", { class: "journal reviews" }, retired.map(principleItem))));
    }
    replace(body, parts);
  }

  function weeklyLook(w) {
    var focus = arr(w.focus).map(function (i) { return "#" + i; });
    return h("div", { class: "playbook-week" },
      h("p", { class: "when", text: "This week's look (" + fmtDay(w.day) + ")" }),
      reviewLine("Bottleneck", w.bottleneck),
      focus.length ? reviewLine("Focus lines", focus.join(", ")) : null,
      arr(w.start).length ? reviewLine("Start", arr(w.start).join("; ")) : null,
      arr(w.stop).length ? reviewLine("Stop", arr(w.stop).join("; ")) : null,
      arr(w.questions).length ? reviewLine("Questions", arr(w.questions).join(" · ")) : null);
  }

  function principleItem(p) {
    var supports = arr(p.supports);
    var against = arr(p.against);
    var retired = p.status === "retired";
    var count = plural(supports.length, "case") + " for" + (against.length ? ", " + against.length + " against" : "");
    return h("li", null,
      h("p", { class: "when" }, "Principle #" + p.id + " · " + (retired ? "retired " : "since "),
        timeEl(retired ? p.retired_at : p.created_at)),
      h("p", { class: "journal-summary" }, retired ? null : chip(CONFIDENCE, p.confidence, String(p.confidence || "?")),
        retired ? "" : " ", asText(p.text)),
      retired ? reviewLine("Why it was retired", p.retired_why) : null,
      supports.length || against.length ? h("details", { class: "review-numbers" }, h("summary", { text: count }),
        h("ul", { class: "verdicts" }, supports.map(function (c) { return caseItem(c, "for"); })
          .concat(against.map(function (c) { return caseItem(c, "against"); })))) : null);
  }

  function caseItem(c, side) {
    var cause = c.cause ? String(c.cause).replace(/_/g, " ") + ": " : "";
    return h("li", null, h("strong", { text: "Case #" + c.id + (c.subject ? " (" + asText(c.subject) + ")" : "") +
      ", " + side + ": " }), cause, asText(c.why), c.lesson ? " Lesson: " + asText(c.lesson) : "");
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
    var parts = ["plans " + m.planner, "venture plans, reviews and the critic " + m.strategy, "work " + m.worker,
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

  // 0.22.1: whose Authentication-Results header counts as the mail provider's verdict on a sender (email_authserv_id).
  var SENDER_CHECK = {
    complete: { icon: "✓", label: "Complete", tone: "good" },
    incomplete: { icon: "!", label: "Incomplete", tone: "warning" },
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
      isObject(e.sender_check) ? [h("dt", { text: "Sender check" }), h("dd", null,
        chip(SENDER_CHECK, e.sender_check.state, sentence(String(e.sender_check.state || "unknown"))), " ",
        String(e.sender_check.note || ""))] : null,
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
    sandbox: { icon: "◐", label: "Sandbox", tone: "accent" },  // 0.30.2: Pinterest's, for the Standard access request
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
          // 0.15.0: an order with many lines is kept without their titles
          h("td", { text: arr(o.items).map(function (i) { return asText(i.title || "#" + i.listing_id) + (num(i.quantity) > 1 ? " × " + i.quantity : ""); }).join("; ") }),
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
      // 0.15.0: a partial refund asks for a correction too (only a full refund or a cancellation did)
      var said = h("span", { class: "muted small", text: o.correction_due ? by + ", then " + o.status + ": correct entry #" + o.entry_id + " (Ember's lines earn " + asText(o.total) + " now)" : refunded ? by + ", then " + o.status + ", and corrected" : by });
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

  // 0.12.0: Ember's share of Etsy's fees on a recorded order (its processing fee, read from the payment, the 6.5%
  // transaction fee and, 0.15.0, the listing fee a sale renews and the VAT on fees), as an expense of the same project:
  // the form opens filled in, and its key records it once.
  function recordFeesButton(o, fake) {
    var cents = num(o.fees_cents);
    var amount = isNaN(cents) ? null : (cents / 100).toFixed(2);
    var b = h("button", { type: "button", class: "btn btn-small", text: "Record Etsy's fees" + (amount ? " (" + amount + " " + asText(o.currency) + ")" : "") });
    b.addEventListener("click", function () {
      openLedgerForm("expense", amount, {
        currency: o.currency,
        note: "Etsy's fees on order " + o.receipt_id + ": payment processing, the 6.5% transaction fee, USD 0.20 a unit sold and 19% VAT on those two",
        day: String(o.ordered_at || "").slice(0, 10),
        idKey: String(o.fee_key || ""),
        testMoney: fake,
        projectId: o.project_id || null,
        ventureId: o.venture_id || null,
      });
    });
    return b;
  }

  // Connecting is two steps: the service's page (in a new tab), then the address it sends you to, pasted here. The
  // controls are built once, so a refresh of the dashboard never loses a pasted address. 0.13.0: Etsy's and
  // Pinterest's (w: the words and addresses of each).
  function connectArea(key, e, w) {
    var c = ui[key];
    if (!c) {
      c = ui[key] = { url: null };
      c.status = h("p", { class: "form-status small", role: "status" });
      c.start = h("button", { type: "button", class: "btn btn-primary", text: w.start });
      c.linkBox = h("div", { class: "etsy-step", hidden: true });
      var addressId = w.id + "-address";
      c.address = h("input", { type: "url", id: addressId, autocomplete: "off", spellcheck: "false", placeholder: "The whole address, with its code and state" });
      c.finish = h("button", { type: "button", class: "btn btn-primary", text: "Finish connecting" });
      c.pasteBox = h("div", { class: "etsy-step field", hidden: true },
        h("label", { for: addressId, text: "2. Paste the address " + w.name + " sent you to" }),
        h("p", { class: "hint", text: "After you allow access, " + w.name + " opens your redirect address. It may show an error page: that's fine. Copy the whole address from the address bar and paste it here." }),
        c.address, h("div", { class: "form-actions" }, c.finish));
      c.disconnect = h("button", { type: "button", class: "btn btn-danger", text: "Disconnect" });
      c.start.addEventListener("click", function () {
        c.start.disabled = true;
        c.status.textContent = "Asking Ember for " + w.name + "'s page…";
        request("POST", w.api + "connect", {}).then(function (res) {
          if (!res.ok) throw httpError(res);
          c.url = String(res.data.authorize_url || "");
          replace(c.linkBox, [h("p", null, h("strong", { text: "1. Allow Ember's access at " + w.name + ": " }), w.link(c.url, "open " + w.name + "'s page")),
            h("p", { class: "muted small", text: "Log in as " + w.who + " and allow access. The page is valid for 15 minutes." })]);
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
        request("POST", w.api + "finish", { address: address }).then(function (res) {
          if (!res.ok) throw httpError(res);
          c.status.textContent = "Connected to " + w.connected(res.data) + ".";
          c.address.value = "";
          c.linkBox.hidden = c.pasteBox.hidden = true;
          refresh();
        }).catch(function (err) {
          c.status.textContent = "Not connected: " + errorText(err) + ".";
        }).then(function () { c.finish.disabled = false; });
      });
      c.disconnect.addEventListener("click", function () {
        if (!window.confirm(w.confirm)) return;
        request("POST", w.api + "disconnect", {}).then(function (res) {
          if (!res.ok) throw httpError(res);
          c.status.textContent = "Disconnected.";
          refresh();
        }).catch(function (err) { c.status.textContent = "Couldn't disconnect (" + errorText(err) + ")."; });
      });
      c.wrap = h("div", { class: "etsy-connect" });
    }
    // 0.30.2: Pinterest's sandbox (for the Standard access request) connects like the account itself
    var connected = e.status === "ok" || (e.status === "sandbox" && !!e.connected_at);
    c.start.textContent = connected ? "Connect again" : w.start;
    var canConnect = connected || e.status === "not_connected" || e.status === "sandbox";
    replace(c.wrap, [
      canConnect ? h("div", { class: "form-actions" }, c.start, connected ? c.disconnect : null) : null,
      c.linkBox, c.pasteBox, c.status,
    ]);
    return c.wrap;
  }

  function etsyConnectArea(e) {
    return connectArea("etsyConnect", e, {
      id: "etsy",
      name: "Etsy",
      api: "api/etsy/",
      start: "Connect your Etsy shop",
      who: "the shop's owner",
      link: etsyLink,
      connected: function (data) { return asText(data.shop_name); },
      confirm: "Disconnect the Etsy shop? Ember can't create listings until you connect it again. Listings already on Etsy stay there.",
    });
  }

  // ---- System -> Pinterest (0.13.0, Phase E2): the owner's account, connecting it, Ember's boards and pins.

  var PIN_STATUS = {
    running: { icon: "●", label: "Being made", tone: "accent" },
    active: { icon: "✓", label: "Live", tone: "good" },
    deleted: { icon: "–", label: "Deleted", tone: "" },
    failed: { icon: "✕", label: "Not pinned", tone: "critical" },
    unclear: { icon: "!", label: "Unclear", tone: "critical" },
  };

  // A link only to www.pinterest.com over https; anything else is shown as text.
  function pinterestLink(value, text) {
    var url;
    try { url = new URL(String(value)); } catch (e) { url = null; }
    if (!url || !/^https:$/.test(url.protocol) || url.hostname !== "www.pinterest.com" || url.port || url.username || url.password) {
      return h("span", { class: "link-text", text: text || String(value || "–") });
    }
    var a = document.createElement("a");
    a.setAttribute("href", url.href);
    a.setAttribute("rel", "noopener noreferrer");
    a.setAttribute("target", "_blank");
    append(a, [text || url.href, h("span", { class: "visually-hidden", text: " (opens in a new tab)" })]);
    return a;
  }

  function renderPinterest(d) {
    var p = isObject(d.integrations) && isObject(d.integrations.pinterest) ? d.integrations.pinterest : null;
    var shown = !!p && p.status !== "disabled";  // off (the default), or an older server: no card
    $("pinterest-card").hidden = !shown;
    if (!shown) { replace($("pinterest-facts"), []); replace($("pinterest-connect"), []); replace($("pinterest-pins"), []); return; }
    var fake = p.mode === "fake";
    var reason = p.reason ? String(p.reason) : null;
    if (!reason && p.status === "not_configured") reason = "Not set up: see the Documentation tab, 'Pinterest'.";
    var boards = arr(p.boards);
    replace($("pinterest-facts"), [
      h("dt", { text: "Status" }), h("dd", null, chip(ETSY_STATUS, p.status, sentence(String(p.status || "unknown").replace(/_/g, " ")))),
      reason && (p.status !== "ok" || fake) ? [h("dt", { text: fake ? "Mode" : "Why" }), h("dd", { class: "pre-line", text: reason })] : null,
      h("dt", { text: "Account" }), h("dd", null, p.username ? (fake ? h("span", { text: String(p.username) + " (fake)" }) : pinterestLink(p.profile_url, String(p.username))) : "–"),
      p.connected_at ? [h("dt", { text: "Connected" }), h("dd", null, timeEl(p.connected_at, fmtDateTime(p.connected_at)))] : null,
      h("dt", { text: "Boards" }), h("dd", { text: boards.length ? boards.map(function (b) { return asText(b.name); }).join(", ") : "None yet: the first waits for your decision" }),
      h("dt", { text: "Pins a day" }), h("dd", { text: "at most " + count(p.daily_limit) }),
      p.last_sync_at ? [h("dt", { text: "Numbers from" }), h("dd", null, timeEl(p.last_sync_at, fmtDateTime(p.last_sync_at) + " (" + relTime(p.last_sync_at) + ")"))] : null,
      p.last_error ? [h("dt", { text: "Last error" }), h("dd", { class: "fact-error" }, h("span", { "aria-hidden": "true", text: "✕ " }), String(p.last_error))] : null,
    ]);
    replace($("pinterest-connect"), fake ? [] : [connectArea("pinterestConnect", p, {
      id: "pinterest",
      name: "Pinterest",
      api: "api/pinterest/",
      start: "Connect your Pinterest account",
      who: "the account's owner",
      link: pinterestLink,
      connected: function (data) { return asText(data.username); },
      confirm: "Disconnect the Pinterest account? Ember can't make pins until you connect it again. Pins already on Pinterest stay there.",
    })]);
    var pins = arr(p.pins);
    replace($("pinterest-pins"), pins.length ? h("div", { class: "table-wrap" }, h("table", null,
      h("thead", null, h("tr", null, ["Pin", "Status", "Impressions", "Saves", "Clicks"].map(function (c) { return h("th", { scope: "col", text: c }); }))),
      h("tbody", null, pins.map(function (q) {
        var numbers = function (v) { return q.synced_at && v !== null && v !== undefined ? count(v) : "–"; };
        return h("tr", null,
          h("td", null, q.url && !fake ? pinterestLink(q.url, asText(q.title)) : h("span", { text: asText(q.title) }),
            h("span", { class: "muted small", text: " → " }), fake ? h("span", { class: "muted small", text: "a listing" }) : etsyLink(q.link, "its listing"),
            q.status !== "active" && q.result ? h("p", { class: "muted small pre-line", text: asText(q.result) }) : null),
          h("td", null, chip(PIN_STATUS, q.status, sentence(String(q.status || "?")))),
          h("td", { class: "num", text: numbers(q.impressions) }),
          h("td", { class: "num", text: numbers(q.saves) }),
          h("td", { class: "num", text: numbers(q.clicks) }));
      })))) : h("p", { class: "muted", text: "None yet. When you approve a pin the agent proposed, Ember makes it here." }));
  }

  // ---- System -> Bluesky (0.19.0): the account the owner made for Ember, and Ember's posts with their numbers.

  var POST_STATUS = {
    running: { icon: "●", label: "Being posted", tone: "accent" },
    active: { icon: "✓", label: "Live", tone: "good" },
    deleted: { icon: "–", label: "Deleted", tone: "" },
    failed: { icon: "✕", label: "Not posted", tone: "critical" },
    unclear: { icon: "!", label: "Unclear", tone: "critical" },
  };

  // A link only to bsky.app over https; anything else is shown as text.
  function blueskyLink(value, text) {
    var url;
    try { url = new URL(String(value)); } catch (e) { url = null; }
    if (!url || !/^https:$/.test(url.protocol) || url.hostname !== "bsky.app" || url.port || url.username || url.password) {
      return h("span", { class: "link-text", text: text || String(value || "–") });
    }
    var a = document.createElement("a");
    a.setAttribute("href", url.href);
    a.setAttribute("rel", "noopener noreferrer");
    a.setAttribute("target", "_blank");
    append(a, [text || url.href, h("span", { class: "visually-hidden", text: " (opens in a new tab)" })]);
    return a;
  }

  function renderBluesky(d) {
    var p = isObject(d.integrations) && isObject(d.integrations.bluesky) ? d.integrations.bluesky : null;
    var shown = !!p && p.status !== "disabled";  // off (the default), or an older server: no card
    $("bluesky-card").hidden = !shown;
    if (!shown) { replace($("bluesky-facts"), []); replace($("bluesky-posts"), []); return; }
    var fake = p.mode === "fake";
    var reason = p.reason ? String(p.reason) : null;
    if (!reason && p.status === "not_configured") reason = "Not set up: see the Documentation tab, 'Bluesky'.";
    var handle = p.handle ? "@" + String(p.handle) : null;
    replace($("bluesky-facts"), [
      h("dt", { text: "Status" }), h("dd", null, chip(ETSY_STATUS, p.status, sentence(String(p.status || "unknown").replace(/_/g, " ")))),
      reason && (p.status !== "ok" || fake) ? [h("dt", { text: fake ? "Mode" : "Why" }), h("dd", { class: "pre-line", text: reason })] : null,
      h("dt", { text: "Account" }), h("dd", null, handle ? (fake ? h("span", { text: handle + " (fake)" }) : blueskyLink(p.profile_url, handle)) : "–"),
      p.followers !== null && p.followers !== undefined ? [h("dt", { text: "Followers" }), h("dd", { text: count(p.followers) })] : null,
      p.automated === false ? [h("dt", { text: "Automation label" }), h("dd", { class: "fact-error" }, h("span", { "aria-hidden": "true", text: "! " }),
        "Off: turn it on in the Bluesky app (Settings, Automation label), so the account shows it is automated.")] : null,
      p.automated === true ? [h("dt", { text: "Automation label" }), h("dd", { text: "On: the account shows it is automated" })] : null,
      p.labels ? [h("dt", { text: "Moderation" }), h("dd", { class: "fact-error" }, h("span", { "aria-hidden": "true", text: "! " }), "Bluesky labelled the account: " + String(p.labels))] : null,
      h("dt", { text: "Posts a day" }), h("dd", { text: "at most " + count(p.daily_limit) }),
      p.last_sync_at ? [h("dt", { text: "Numbers from" }), h("dd", null, timeEl(p.last_sync_at, fmtDateTime(p.last_sync_at) + " (" + relTime(p.last_sync_at) + ")"))] : null,
      p.last_error ? [h("dt", { text: "Last error" }), h("dd", { class: "fact-error" }, h("span", { "aria-hidden": "true", text: "✕ " }), String(p.last_error))] : null,
    ]);
    var posts = arr(p.posts);
    replace($("bluesky-posts"), posts.length ? h("div", { class: "table-wrap" }, h("table", null,
      h("thead", null, h("tr", null, ["Post", "Status", "Likes", "Reposts", "Replies", "Quotes"].map(function (c) { return h("th", { scope: "col", text: c }); }))),
      h("tbody", null, posts.map(function (q) {
        var numbers = function (v) { return q.synced_at && v !== null && v !== undefined ? count(v) : "–"; };
        var words = asText(q.text).split("\n")[0];
        var linkTo = function (u, i) { return /^https:\/\/www\.etsy\.com\//.test(String(u)) ? etsyLink(u, i ? "a listing" : "its listing") : siteLink(u, i ? "a page" : "its page"); };
        var links = [q.link, q.second_link].filter(function (u) { return !!u; }).map(linkTo);  // 0.25.1: and its second link
        return h("tr", null,
          h("td", null, q.url && !fake ? blueskyLink(q.url, words) : h("span", { text: words }),
            links.length && !fake ? [h("span", { class: "muted small", text: " → " }), links[0], links[1] ? [h("span", { class: "muted small", text: " and " }), links[1]] : null] : null,
            q.labels ? h("p", { class: "fact-error small" }, "Labelled by moderation: " + String(q.labels)) : null,
            q.status !== "active" && q.result ? h("p", { class: "muted small pre-line", text: asText(q.result) }) : null),
          h("td", null, chip(POST_STATUS, q.status, sentence(String(q.status || "?")))),
          h("td", { class: "num", text: numbers(q.likes) }),
          h("td", { class: "num", text: numbers(q.reposts) }),
          h("td", { class: "num", text: numbers(q.replies) }),
          h("td", { class: "num", text: numbers(q.quotes) }));
      })))) : h("p", { class: "muted", text: "None yet. When you approve a post the agent proposed, Ember posts it here." }));
  }

  // ---- System -> Printify (0.13.0, Phase E4): the account's shop, Ember's products and what their orders cost.

  function renderPrintify(d) {
    var p = isObject(d.integrations) && isObject(d.integrations.printify) ? d.integrations.printify : null;
    var shown = !!p && p.status !== "disabled";  // off (the default), or an older server: no card
    $("printify-card").hidden = !shown;
    if (!shown) { replace($("printify-facts"), []); replace($("printify-products"), []); replace($("printify-orders"), []); return; }
    var fake = p.mode === "fake";
    var reason = p.reason ? String(p.reason) : null;
    if (!reason && p.status === "not_configured") reason = "Not set up: see the Documentation tab, 'Printify'.";
    var shop = isObject(p.shop) ? p.shop : null;
    var currency = asText(p.currency);
    replace($("printify-facts"), [
      h("dt", { text: "Status" }), h("dd", null, chip(ETSY_STATUS, p.status, sentence(String(p.status || "unknown").replace(/_/g, " ")))),
      reason && (p.status !== "ok" || fake) ? [h("dt", { text: fake ? "Mode" : "Why" }), h("dd", { class: "pre-line", text: reason })] : null,
      h("dt", { text: "Shop" }), h("dd", { text: shop ? asText(shop.title) + " (#" + shop.shop_id + ")" + (fake ? " (fake)" : "") : "Not found yet" }),
      h("dt", { text: "Prices in" }), h("dd", { text: currency || "–" }),
      h("dt", { text: "Products a day" }), h("dd", { text: "at most " + count(p.daily_limit) }),
      p.last_sync_at ? [h("dt", { text: "Numbers from" }), h("dd", null, timeEl(p.last_sync_at, fmtDateTime(p.last_sync_at) + " (" + relTime(p.last_sync_at) + ")"))] : null,
      p.last_error ? [h("dt", { text: "Last error" }), h("dd", { class: "fact-error" }, h("span", { "aria-hidden": "true", text: "✕ " }), String(p.last_error))] : null,
    ]);
    var products = arr(p.products);
    var money = function (cents, cur) { var n = num(cents); return isNaN(n) ? "–" : (n / 100).toFixed(2) + " " + asText(cur); };
    replace($("printify-products"), products.length ? h("div", { class: "table-wrap" }, h("table", null,
      h("thead", null, h("tr", null, ["Product", "Status", "Prices (making + shipping, kept)", "Views", "Favorites"].map(function (c) { return h("th", { scope: "col", text: c }); }))),
      h("tbody", null, products.map(function (q) {
        var prices = arr(q.prices).map(function (v) {
          return "#" + v[0] + ": " + money(v[1], q.currency) + " (" + money(v[2], q.currency) + " + " + money(v[3], q.currency) + ", kept " + money(v[4], q.currency) + ")";
        });
        return h("tr", null,
          h("td", null, q.url && !fake ? etsyLink(q.url, asText(q.title)) : h("span", { text: asText(q.title) }),
            q.status !== "active" && q.result ? h("p", { class: "muted small pre-line", text: asText(q.result) }) : null),
          h("td", null, chip(PRODUCT_EXECUTION, q.status, sentence(String(q.status || "?")))),
          h("td", { class: "small", text: prices.join("; ") || "–" }),
          h("td", { class: "num", text: q.views === null || q.views === undefined ? "–" : count(q.views) }),
          h("td", { class: "num", text: q.favorites === null || q.favorites === undefined ? "–" : count(q.favorites) }));
      })))) : h("p", { class: "muted", text: "None yet. When you approve a product the agent proposed, Ember creates it here." }));
    var orders = arr(p.orders);
    replace($("printify-orders"), orders.length ? h("div", { class: "table-wrap" }, h("table", null,
      h("thead", null, h("tr", null, ["Ordered", "Products", "Status", "Making and shipping"].map(function (c) { return h("th", { scope: "col", text: c }); }))),
      h("tbody", null, orders.map(function (o) {
        return h("tr", null,
          h("td", null, timeEl(o.created_at, fmtDateTime(o.created_at))),
          h("td", { text: asText(o.titles) + (num(o.quantity) > 1 ? " (" + count(o.quantity) + " items)" : "") }),
          h("td", { text: asText(o.status) || "–" }),
          h("td", null, h("span", { text: asText(o.cost) + " " }), printifyCostCell(o, fake)));
      })))) : h("p", { class: "muted", text: "No orders of Ember's products yet." }));
  }

  // 0.13.0 (Phase E3): the owner's website. Ember's code builds it; the owner previews it (in a tab of its own, where
  // it runs nothing) and downloads it to publish it at their host. Ember never publishes it.
  var SITE_STATUS = {
    ok: { icon: "✓", label: "Ready to publish", tone: "good" },
    not_ready: { icon: "○", label: "Not ready", tone: "warning" },
  };

  function renderSite(d) {
    var s = isObject(d.integrations) && isObject(d.integrations.site) ? d.integrations.site : null;
    var shown = !!s && s.status !== "disabled";  // off (the default), or an older server: no card
    $("site-part").hidden = !shown;
    if (!shown) { replace($("site-facts"), []); replace($("site-actions"), []); replace($("site-pages"), []); return; }
    var ready = s.status === "ok";
    var changed = arr(s.changed).map(asText);
    var pages = arr(s.pages);
    replace($("site-facts"), [
      h("dt", { text: "Status" }), h("dd", null, chip(SITE_STATUS, s.status, sentence(String(s.status || "unknown").replace(/_/g, " ")))),
      !ready && s.reason ? [h("dt", { text: "Why" }), h("dd", { class: "pre-line", text: sentence(String(s.reason)) + "." })] : null,
      arr(s.advice).length ? [h("dt", { text: "Advice" }), h("dd", { text: arr(s.advice).map(function (a) { return sentence(asText(a)) + "."; }).join(" ") })] : null,
      h("dt", { text: "Pages" }), h("dd", { text: count(pages.length) + " of " + count(s.max_pages) }),
      h("dt", { text: "Address" }), h("dd", { text: s.url ? asText(s.url) : "Not set (site_url): the site has no sitemap" }),
      h("dt", { text: "Downloaded" }), h("dd", null, s.downloaded_at ? timeEl(s.downloaded_at, fmtDateTime(s.downloaded_at) + " (" + relTime(s.downloaded_at) + ")") : h("span", { text: "Never" })),
      s.downloaded_at ? [h("dt", { text: "Changed since" }), h("dd", { text: changed.length ? changed.join(", ") : "Nothing: what you downloaded is up to date" })] : null,
    ]);
    var preview = ready ? h("a", { class: "btn", href: "api/site/preview/index.html", target: "_blank", rel: "noopener", text: "Preview" }) : null;
    var download = h("button", { type: "button", class: "btn btn-primary", text: "Download (zip)", disabled: !ready });
    download.addEventListener("click", function () {
      var link = h("a", { href: "api/site/download", download: "website.zip", hidden: true });
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      setStatusText("site-status", "Downloading website.zip (check your downloads). Upload its files to your host.", "ok");
      download.blur();  // the card shows the download once the dashboard has it (a focused button holds the card)
      window.setTimeout(refresh, 3000);
    });
    replace($("site-actions"), [h("div", { class: "form-actions" }, preview, download), h("p", { class: "form-status", id: "site-status", role: "status" })]);
    replace($("site-pages"), pages.length ? h("div", { class: "table-wrap" }, h("table", null,
      h("thead", null, h("tr", null, ["Page", "Title and description", "Written"].map(function (c) { return h("th", { scope: "col", text: c }); }))),
      h("tbody", null, pages.map(function (p) {
        var file = asText(p.slug) + ".html";
        return h("tr", null,
          h("td", null, ready ? h("a", { href: "api/site/preview/" + encodeURIComponent(file), target: "_blank", rel: "noopener", text: file }) : h("span", { text: file }),
            p.menu ? h("p", { class: "muted small", text: "Menu: " + asText(p.menu) }) : null),
          h("td", null, h("span", { text: asText(p.title) }), h("p", { class: "muted small", text: asText(p.description) })),
          h("td", null, timeEl(p.updated_at, fmtDateTime(p.updated_at))));
      })))) : h("p", { class: "muted", text: "None yet: the agent writes the home page first." }));
  }

  // 0.14.0: the blog on the owner's website. The agent proposes posts and the link page; each waits for the owner's
  // approval (with a preview), then Ember's code uploads exactly that page over SFTP. The password is never shown.
  var BLOG_STATUS = {
    ok: { icon: "✓", label: "Ready", tone: "good" },
    not_ready: { icon: "○", label: "Not ready", tone: "warning" },
  };

  function blogCheckArea() {
    var c = ui.blogCheck;
    if (!c) {
      c = ui.blogCheck = {};
      c.status = h("p", { class: "form-status small", role: "status" });
      c.button = h("button", { type: "button", class: "btn", text: "Check the connection" });
      c.button.addEventListener("click", function () {
        c.button.disabled = true;
        c.status.textContent = "Logging in to your server…";
        request("POST", "api/blog/check", {}).then(function (res) {
          if (!res.ok) throw httpError(res);
          var r = isObject(res.data) ? res.data : {};
          c.status.textContent = "It works" + (r.simulated ? " (dry run: the fake server)" : "") + ": " +
            (r.index ? plural(num(r.posts) || 0, "post") + " in the blog's list" : "no blog list on the server yet") +
            (r.links ? " and a link page" : "") + ". Server key: " + asText(r.fingerprint) + ".";
          refresh();
        }).catch(function (err) {
          c.status.textContent = "Not working: " + errorText(err) + ".";
        }).then(function () { c.button.disabled = false; });
      });
    }
    return [h("div", { class: "form-actions" }, c.button), c.status];
  }

  // 0.16.0: the Website card: the SFTP connection (the blog's and the live view's), the live view, the blog and the
  // pages to download, each part shown while it is on.
  function renderWebsiteCard() {
    $("website-card").hidden = ["server-part", "live-part", "blog-part", "site-part"].every(function (id) { return $(id).hidden; });
  }

  function renderBlog(d) {
    var b = isObject(d.integrations) && isObject(d.integrations.blog) ? d.integrations.blog : null;
    var shown = !!b && b.status !== "disabled";  // the blog and the live view off (the default), or an older server
    var blogOn = shown && b.enabled !== false;  // 0.16.0: the connection also serves the live view alone
    $("server-part").hidden = !shown;
    $("blog-part").hidden = !blogOn;
    if (!shown) { replace($("blog-server"), []); replace($("blog-facts"), []); replace($("blog-actions"), []); replace($("blog-posts"), []); return; }
    var posts = arr(b.posts);
    var server = b.host ? asText(b.user || "?") + " at " + asText(b.host) + ":" + asText(b.port) + (b.folder ? ", folder " + asText(b.folder) : "") : "Not set (blog_sftp_host)";
    var page = isObject(b.links_page) ? b.links_page : null;
    replace($("blog-server"), [
      h("dt", { text: "Website" }), h("dd", null, b.url ? siteLink(b.url, asText(b.url)) : h("span", { text: "Not set (site_url)" })),
      h("dt", { text: "Server" }), h("dd", { text: b.simulated ? "Dry run: a fake server, nothing leaves the app" : server }),
      b.simulated ? null : [h("dt", { text: "Password" }), h("dd", { text: b.password_set ? "Set (never shown)" : "Not set (blog_sftp_password)" })],
      h("dt", { text: "Server key" }), h("dd", { class: "mono", text: b.host_key ? asText(b.host_key) + (b.simulated ? "" : b.host_key_pinned_by_owner ? " (yours, from the options)" : " (kept since the first connection)") : "Not pinned yet: the first connection keeps the key it sees" }),
      b.last_error ? [h("dt", { text: "Last connection" }), h("dd", { class: "pre-line", text: "Failed: " + endSentence(String(b.last_error)) })] : null,
    ]);
    replace($("blog-actions"), blogCheckArea());
    if (!blogOn) { replace($("blog-facts"), []); replace($("blog-posts"), []); return; }
    replace($("blog-facts"), [
      h("dt", { text: "Status" }), h("dd", null, chip(BLOG_STATUS, b.status, sentence(String(b.status || "unknown").replace(/_/g, " ")))),
      b.status !== "ok" && b.reason ? [h("dt", { text: "Why" }), h("dd", { class: "pre-line", text: sentence(String(b.reason)) + "." })] : null,
      h("dt", { text: "Link page" }), h("dd", { text: page ? "Uploaded by " + agentName() + " on " + asText(page.day) + " (request #" + asText(page.approval_id) + ")" : "Yours: " + agentName() + " hasn't changed it" }),
    ]);
    replace($("blog-posts"), posts.length ? h("div", { class: "table-wrap" }, h("table", null,
      h("thead", null, h("tr", null, ["Post", "Date", "Published by"].map(function (c) { return h("th", { scope: "col", text: c }); }))),
      h("tbody", null, posts.map(function (q) {
        return h("tr", null,
          h("td", null, q.url && !b.simulated ? siteLink(q.url, asText(q.title)) : h("span", { text: asText(q.title) }), h("p", { class: "muted small", text: asText(q.slug) })),
          h("td", { text: asText(q.day) }),
          h("td", { text: q.approval_id ? agentName() + " (#" + asText(q.approval_id) + ")" : "You" }));
      })))) : h("p", { class: "muted", text: "None known yet: " + agentName() + "'s code reads the blog's list at the first upload, or when you check the connection." }));
  }

  // 0.16.0: the live view on the owner's website: what it shows, when it went up, the preview and the banner's HTML.
  var LIVE_STATUS = {
    ok: { icon: "✓", label: "On", tone: "good" },
    not_ready: { icon: "○", label: "Not ready", tone: "warning" },
    off: { icon: "–", label: "Off", tone: "" },
  };
  var LIVE_PARTS = [
    ["banner", "banner"], ["money", "balance and spending"], ["revenue", "revenue"], ["grants", "your money"],
    ["chart", "chart"], ["work", "work"], ["shop", "shop and blog"], ["record", "track record"], ["memorial", "memorial"],
  ];

  function liveSnippetArea(lang, text) {
    ui.liveSnippet = ui.liveSnippet || {};
    var c = ui.liveSnippet[lang];
    if (!c) {
      c = ui.liveSnippet[lang] = {};
      c.code = h("pre", { class: "snippet mono", tabindex: "0" });
      c.status = h("p", { class: "form-status small", role: "status" });
      c.button = h("button", { type: "button", class: "btn btn-small", text: "Copy the HTML" });
      c.button.addEventListener("click", function () {
        copyText(c.code.textContent, c.code, function () { c.status.textContent = "Copied. Paste it into that home page once."; }, function (selected) {
          c.status.textContent = selected ? "The browser didn't allow copying. The HTML is selected: press Ctrl+C (Cmd+C on a Mac)." : "The browser didn't allow copying: select the HTML and copy it.";
        });
      });
    }
    if (c.code.textContent !== text) c.code.textContent = text;
    var where = lang === "en" ? "English home page (en/index.html)" : "German home page (index.html)";
    return [h("h4", { class: "small-head", text: "The banner on your " + where }), c.code, h("div", { class: "form-actions" }, c.button), c.status];
  }

  // The agent's titles the work part would show: the owner shows each once, or keeps it off (their word holds for
  // that exact text). Ember's code never shows one with a long number, a web address, an @ or an IBAN.
  var LIVE_TITLE = {
    shown: { icon: "✓", label: "Shown", tone: "good" },
    waiting: { icon: "◔", label: "Waiting for you", tone: "accent" },
    off: { icon: "–", label: "Kept off", tone: "" },
    refused: { icon: "✕", label: "Never shown", tone: "critical" },
  };

  function liveTitleButton(t, show, label, status) {
    var b = h("button", { type: "button", class: "btn btn-small" + (show ? "" : " btn-ghost"), text: label });
    b.addEventListener("click", function () {
      b.disabled = true;
      status.textContent = "Saving…";
      request("POST", "api/live/titles/" + encodeURIComponent(String(t.id)), { show: show }).then(function (res) {
        if (!res.ok) throw httpError(res);
        status.textContent = show ? "Shown from the next upload on (in a minute or two)." : "Kept off the page.";
        refresh();
      }).catch(function (err) {
        status.textContent = "Not saved: " + errorText(err) + ".";
        b.disabled = false;
      });
    });
    return b;
  }

  function liveTitles(titles) {
    if (!titles.length) return null;
    var name = agentName();
    return [h("h4", { class: "small-head", text: name + "'s titles on the page" }),
      h("p", { class: "muted small", text: "The page shows a title only once you showed it: until then it only counts it. Your word holds for the exact text; a changed title waits for you again." }),
      h("div", { class: "table-wrap" }, h("table", null,
        h("thead", null, h("tr", null, ["Title", "Of", "On the page", ""].map(function (c) { return h("th", { scope: "col", text: c }); }))),
        h("tbody", null, titles.map(function (t) {
          var status = h("p", { class: "form-status small", role: "status" });
          var buttons = [];
          if (t.state !== "refused" && t.state !== "shown") buttons.push(liveTitleButton(t, true, "Show it", status));
          if (t.state !== "refused" && t.state !== "off") buttons.push(liveTitleButton(t, false, t.state === "shown" ? "Take it off" : "Keep it off", status));
          var of = (t.kind === "venture" ? "Venture #" : "Milestone #") + asText(t.ref) + (t.kind === "venture" ? " (" + asText(t.note) + ")" : " (due " + asText(t.note) + ")");
          return h("tr", null,
            h("td", null, h("span", { text: asText(t.text) }), t.why ? h("p", { class: "muted small", text: name + "'s code keeps it off: it holds " + asText(t.why) + "." }) : null),
            h("td", { text: of }),
            h("td", null, chip(LIVE_TITLE, t.state, sentence(asText(t.state)))),
            h("td", null, buttons.length ? h("div", { class: "form-actions" }, buttons) : null, status));
        }))))];
  }

  function liveWill(w) {
    if (!isObject(w)) return null;
    var request = w.approval_id ? "request #" + w.approval_id : null;
    var text = !request ? "Not asked yet: " + agentName() + "'s code asks you to approve it at its next upload, unless it holds what a public page never shows (then it stays off)."
      : w.status === "pending" ? "Waits for your decision: " + request + " under Approvals."
      : isApproved(w.status) ? "Approved (" + request + "): shown from the next upload on."
      : w.status === "done" ? "On the page (" + request + ")."
      : "Not shown (" + request + ", " + String(w.status).replace(/_/g, " ") + ").";
    return [h("dt", { text: "Last will" }), h("dd", { text: text })];
  }

  function renderLive(d) {
    var l = isObject(d.integrations) && isObject(d.integrations.live) ? d.integrations.live : null;
    var shown = !!l && l.status !== "disabled";
    $("live-part").hidden = !shown;
    if (!shown) { replace($("live-facts"), []); replace($("live-actions"), []); return; }
    var parts = isObject(l.parts) ? l.parts : {};
    var on = LIVE_PARTS.filter(function (p) { return parts[p[0]]; }).map(function (p) { return p[1]; });
    var off = LIVE_PARTS.filter(function (p) { return !parts[p[0]]; }).map(function (p) { return p[1]; });
    replace($("live-facts"), [
      h("dt", { text: "Status" }), h("dd", null, chip(LIVE_STATUS, l.status, sentence(String(l.status || "unknown").replace(/_/g, " ")))),
      l.status === "not_ready" && l.reason ? [h("dt", { text: "Why" }), h("dd", { class: "pre-line", text: sentence(String(l.reason)) + "." })] : null,
      h("dt", { text: "Pages" }), h("dd", null, l.url && !l.simulated ? [siteLink(l.url, asText(l.url)), h("br"), siteLink(l.url_en, asText(l.url_en))] : h("span", { text: l.simulated ? "Dry run: uploaded to the fake server" : "Not set (site_url)" })),
      h("dt", { text: "Uploaded" }), h("dd", null, l.uploaded_at ? timeEl(l.uploaded_at, fmtDateTime(l.uploaded_at) + " (" + relTime(l.uploaded_at) + ")") : h("span", { text: "Not yet" })),
      l.status === "ok" ? [h("dt", { text: "Shows" }), h("dd", { text: on.length ? sentence(on.join(", ")) + "." : "Only its state." })] : null,
      l.status === "ok" && off.length ? [h("dt", { text: "Hidden" }), h("dd", { text: sentence(off.join(", ")) + "." })] : null,
      l.last_error ? [h("dt", { text: "Last upload" }), h("dd", { class: "pre-line", text: "Failed: " + endSentence(String(l.last_error)) })] : null,
      liveWill(l.will),
    ]);
    if (l.status === "off") { replace($("live-actions"), h("p", { class: "muted small", text: "Off: the page on your site says so. Switch it on with live_enabled." })); return; }
    function previewLink(href, text) { return h("a", { class: "btn", href: href, target: "_blank", rel: "noopener", text: text }); }
    var preview = h("div", { class: "form-actions" },
      previewLink("api/live/preview/live.html", "Preview the page"), previewLink("api/live/preview/live-en.html", "English page"),
      parts.banner ? [previewLink("api/live/preview/banner.svg", "Banner"), previewLink("api/live/preview/banner-en.svg", "English banner")] : null);
    var how = l.snippet ? h("p", { class: "muted small", text: "Put each banner once into its home page (in the hero, under the facts) and add the banner's style to your stylesheet (see the Documentation tab). It links to the live page in the same language; the picture changes by itself." }) : null;
    replace($("live-actions"), [preview, how,
      l.snippet ? liveSnippetArea("de", asText(l.snippet)) : null,
      l.snippet_en ? liveSnippetArea("en", asText(l.snippet_en)) : null,
      liveTitles(arr(l.titles))]);
  }

  // What an order cost you at Printify is an expense only you record: the form opens filled in, and its key records it
  // once. Only EUR and USD can be recorded here. 0.15.0: as an expense of the product's project and venture, like its
  // sale's revenue (it was overhead), with the tax Printify bills.
  function printifyCostCell(o, fake) {
    if (o.recorded) return h("span", { class: "muted small", text: "Recorded" });
    if (o.currency !== "EUR" && o.currency !== "USD") return h("span", { class: "muted small", text: "In " + asText(o.currency) + ": convert it and record it yourself" });
    var cents = num(o.cost_cents);
    var b = h("button", { type: "button", class: "btn btn-small", text: "Record the cost" });
    b.addEventListener("click", function () {
      openLedgerForm("expense", isNaN(cents) ? null : (cents / 100).toFixed(2), {
        currency: o.currency,
        note: "Printify: making, shipping and tax of order " + o.order_id + " (" + asText(o.titles).slice(0, 80) + ")",
        day: String(o.created_at || "").slice(0, 10),
        idKey: String(o.key || ""),
        testMoney: fake,
        projectId: o.project_id || null,
        ventureId: o.venture_id || null,
      });
    });
    return b;
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
    $("kill-desc-1").textContent = "The kill switch stops " + name + " for good: no more model calls, and a running cycle ends at its next call. It takes back every unlock too. Unlike Pause, Resume doesn't undo it.";
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
  $("chat-scroll").addEventListener("scroll", chatScrolled, { passive: true });
  $("chat-jump").addEventListener("click", function () {
    ui.chat.stick = true;
    ui.chat.newBelow = false;
    scrollChatToEnd();
    syncChatJump();
  });
  $("composer-text").addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); sendMessage(); }
  });
  $("composer-text").addEventListener("input", function () {
    growComposer();
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
  // The files the agent wrote. Their text is only ever shown with textContent, never rendered as a page (an .html or
  // .svg file stays text, and Markdown is drawn element by element from its text, a link as its words and its
  // address), and the server sends them as downloads, so opening the URL itself never renders them either. Products
  // (PDF, Word, Excel, PNG) are made by Ember's code from the agent's text: they are shown as the PNG pictures made
  // with them and downloaded as files, never opened in the dashboard.
  // 0.26.0: an overview (what there is, how full it is, what waits for the owner, the latest work), every file as an
  // item (a product's files are one: shop/cv.pdf with shop/cv.docx and its page pictures), and a viewer in a dialog.
  // Each item is under the project or venture it was written for (Ember's code files it as the agent writes it): the
  // overview's projects, a filter, the list grouped by them, and the viewer's way to the project and the cycle.

  var WS_MONO = /\.(csv|tsv|json|ya?ml|xml|html|css|py)$/i;
  var WS_PAGE = 48;            // items listed before "Show more"
  var WS_RECENT = 8;           // items under Latest work
  var WS_OWNERS = 6;           // projects and ventures in the overview (the list groups them all)
  var WS_TEXTS_KEPT = 30;      // texts kept once opened, so stepping back and forth doesn't load them again
  var WS_TABLE_ROWS = 500;     // rows of a CSV or TSV file shown as a table (the plain text has them all)
  var WS_TABLE_COLUMNS = 40;
  // What a file's ending says it is: its label, and for a text file which kind of text.
  var WS_FILES = {
    pdf: { label: "PDF" }, docx: { label: "Word" }, pptx: { label: "PowerPoint" }, xlsx: { label: "Excel" },
    png: { label: "PNG" }, jpg: { label: "JPEG" },
    md: { label: "Markdown", text: "notes" }, txt: { label: "Text", text: "notes" },
    csv: { label: "CSV", text: "data" }, tsv: { label: "TSV", text: "data" }, json: { label: "JSON", text: "data" },
    yaml: { label: "YAML", text: "data" }, yml: { label: "YAML", text: "data" }, xml: { label: "XML", text: "data" },
    html: { label: "HTML", text: "web" }, css: { label: "CSS", text: "web" }, py: { label: "Python", text: "script" },
  };
  var WS_KINDS = {
    document: { one: "document", many: "documents" }, spreadsheet: { one: "spreadsheet", many: "spreadsheets" },
    picture: { one: "picture", many: "pictures" }, text: { one: "text file", many: "text files" },
  };
  var WS_TEXTS = {
    notes: { one: "note or draft", many: "notes and drafts" }, data: { one: "data file", many: "data files" },
    web: { one: "web file", many: "web files" }, script: { one: "script", many: "scripts" },
  };
  var WS_KIND_FILTERS = ["all", "document", "spreadsheet", "picture", "text"];
  var WS_SORTS = ["project", "folder", "newest", "name", "size"];
  var WS_VIEWS = ["list", "grid"];
  // The requests whose files the workspace marks, by how much they need the owner: waiting for them first.
  var WS_REQUEST_ORDER = { pending: 0, approved: 1, approved_with_changes: 1, done: 2 };
  // A product's pictures next to it: a document's pages (-page1.png ...), a spreadsheet's sheets (-preview.png, then
  // -sheet2.png ...), a KDP cover's preview (-preview.png), a cost statement's cover (-cover.png).
  var WS_PART = /^(.+)-(page(\d{1,3})|preview|sheet(\d{1,3})|cover)\.png$/i;
  var WS_PRODUCT_RANK = { pdf: 0, xlsx: 1, docx: 2, pptx: 3 };
  var wsCollator = typeof Intl.Collator === "function" ? new Intl.Collator(undefined, { numeric: true, sensitivity: "base" }) : null;

  function wsType(path) {
    var match = /\.([a-z]+)$/i.exec(String(path));
    return match ? match[1].toLowerCase() : "";
  }

  function wsProductUrl(path, inline) {
    return "api/workspace/product?path=" + encodeURIComponent(path) + (inline ? "&inline=1" : "");
  }

  // The file's time and size in the address: a changed picture loads again, an unchanged one may come from the cache.
  function wsVersion(f) { return "&v=" + encodeURIComponent(String(f.modified_at || "") + "~" + String(f.size || 0)); }
  function wsThumbUrl(f) { return "api/workspace/thumb?path=" + encodeURIComponent(f.path) + wsVersion(f); }
  function wsPictureUrl(f) { return wsProductUrl(f.path, true) + wsVersion(f); }

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

  function wsFolder(path) { var at = path.lastIndexOf("/"); return at < 0 ? "" : path.slice(0, at); }
  function wsTime(iso) { var t = new Date(iso).getTime(); return isNaN(t) ? 0 : t; }
  function wsCompare(a, b) { return wsCollator ? wsCollator.compare(a, b) : a < b ? -1 : a > b ? 1 : 0; }
  function wsCount(n, words) { return intFmt.format(n) + " " + (n === 1 ? words.one : words.many); }
  function wsFolderName(folder) { return folder ? folder + "/" : "the top folder"; }

  // "just now", "12 min ago", "5 h ago", "yesterday", "3 days ago", then the date: short enough for a list.
  function wsAgo(iso) {
    var t = wsTime(iso);
    if (!t) return "–";
    var seconds = (Date.now() - t) / 1000;
    if (seconds < 60) return "just now";
    if (seconds < 3600) return intFmt.format(Math.floor(seconds / 60)) + " min ago";
    if (seconds < 86400) return intFmt.format(Math.floor(seconds / 3600)) + " h ago";
    var then = new Date(t);
    var today = new Date();
    var days = Math.round((new Date(today.getFullYear(), today.getMonth(), today.getDate()) -
      new Date(then.getFullYear(), then.getMonth(), then.getDate())) / 86400000);
    return days < 30 ? relFmt.format(-Math.max(1, days), "day") : dateFmt.format(then);
  }

  // A time that says how long ago (short in lists, long in the overview), kept current while the tab is open.
  function wsWhen(iso, long) {
    return h("time", { datetime: iso, title: fmtDateTime(iso), "data-ws-rel": long ? "long" : "short", text: long ? relTime(iso) : wsAgo(iso) });
  }

  function wsTick() {
    Array.prototype.forEach.call(document.querySelectorAll("time[data-ws-rel]"), function (t) {
      var iso = t.getAttribute("datetime");
      var text = t.getAttribute("data-ws-rel") === "long" ? relTime(iso) : wsAgo(iso);
      if (t.textContent !== text) t.textContent = text;
    });
  }

  // ---- The list as items: a product with its Word copy and its pictures, a picture of its own, a text file.

  function wsModel(list) {
    var files = arr(list.files).filter(function (f) { return isObject(f) && typeof f.path === "string" && f.path; });
    var byPath = {};
    var groups = {};
    var items = [];
    files.forEach(function (f) {
      byPath[f.path] = f;
      var ext = wsType(f.path);
      if (WS_PRODUCT_RANK[ext] === undefined) return;
      var base = f.path.slice(0, -ext.length - 1);
      (groups[base] = groups[base] || { files: [], pictures: [] }).files.push(f);
    });
    files.forEach(function (f) {
      var ext = wsType(f.path);
      if (ext !== "png" && ext !== "jpg") return;
      var m = WS_PART.exec(f.path);
      var group = m ? groups[m[1]] : null;
      var part = m ? m[2].toLowerCase() : "";
      var sheet = !!group && group.files.some(function (g) { return wsType(g.path) === "xlsx"; });
      if (!group || (part === "cover" && !sheet)) {
        items.push(wsItem("picture", f.path, f, [f], [{ file: f, order: 0, label: null }]));
        return;
      }
      var n = m[3] || m[4];
      group.pictures.push({
        file: f,
        order: n ? Number(n) : part === "cover" ? -1 : sheet ? 1 : 0,
        label: m[3] ? "Page " + m[3] : m[4] ? "Sheet " + m[4] : part === "cover" ? "Cover" : sheet ? "Sheet 1" : "Preview",
      });
    });
    Object.keys(groups).forEach(function (base) {
      var g = groups[base];
      g.files.sort(function (a, b) { return WS_PRODUCT_RANK[wsType(a.path)] - WS_PRODUCT_RANK[wsType(b.path)]; });
      g.pictures.sort(function (a, b) { return a.order - b.order || wsCompare(a.file.path, b.file.path); });
      var item = wsItem(wsType(g.files[0].path) === "xlsx" ? "spreadsheet" : "document", base, g.files[0],
        g.files.concat(g.pictures.map(function (p) { return p.file; })), g.pictures);
      // The text it was most likely made from: written next to it, under the same name.
      ["md", "txt", "json"].forEach(function (ext) { if (byPath[base + "." + ext]) item.related.push(base + "." + ext); });
      items.push(item);
    });
    files.forEach(function (f) {
      var type = WS_FILES[wsType(f.path)];
      if (!type || type.text) items.push(wsItem("text", f.path, f, [f], []));
    });
    var byKey = {};
    items.forEach(function (item) { byKey[item.key] = item; });
    items.forEach(function (item) {
      item.related.forEach(function (path) { if (byKey[path]) byKey[path].made.push(item.key); });
    });
    var owners = wsOwners(list);
    items.forEach(function (item) {
      wsFiledUnder(item);
      if (item.owner !== "none") item.search += "\n" + wsOwner(owners, item.owner).title.toLowerCase();  // found by its project too
    });
    return { items: items, byKey: byKey, byPath: byPath, owners: owners, ownersKey: JSON.stringify(list.owners || null) };
  }

  function wsItem(kind, key, primary, files, pictures) {
    var size = 0;
    var time = 0;
    var modified = primary.modified_at;
    files.forEach(function (f) {
      size += num(f.size) || 0;
      var t = wsTime(f.modified_at);
      if (t > time) { time = t; modified = f.modified_at; }
    });
    var ext = wsType(primary.path);
    var parts = pictures.map(function (p) { return p.file; });
    var formats = files.filter(function (f) { return kind === "picture" || parts.indexOf(f) < 0; }).map(function (f) {
      var type = WS_FILES[wsType(f.path)];
      return type ? type.label : wsType(f.path).toUpperCase() || "File";
    });
    var sheets = pictures.filter(function (p) { return /^Sheet /.test(p.label || ""); }).length;
    return {
      key: key, kind: kind, ext: ext, text: kind === "text" ? (WS_FILES[ext] || {}).text || "notes" : null,
      primary: primary, files: files, pictures: pictures, thumb: pictures.length ? pictures[0].file : null,
      name: baseName(primary.path), folder: wsFolder(primary.path), size: size, time: time, modified: modified,
      formats: formats, sheets: sheets, related: [], made: [], requests: [], fresh: false,
      search: files.map(function (f) { return f.path.toLowerCase(); }).join("\n"),
    };
  }

  // "PDF + Word", "Excel, 3 sheets", "Markdown"
  function wsFormatText(item) { return item.formats.join(" + ") + (item.sheets > 1 ? ", " + item.sheets + " sheets" : ""); }

  // The item a file belongs to now (a picture that was on its own may have become a product's page since).
  function wsFindItem(key, path) {
    var model = ui.ws.model;
    if (!model) return null;
    if (key !== null && model.byKey[key]) return model.byKey[key];
    for (var i = 0; i < model.items.length; i++) {
      if (model.items[i].files.some(function (f) { return f.path === path; })) return model.items[i];
    }
    return null;
  }

  // ---- Whose an item is: the project or venture it was written for ("p:1", "v:2"; "none": written with
  // neither in focus), from its main file (else the first of its files that says), and the cycle that wrote it last.

  // An id the server sent (a whole number above 0) as text, else null.
  function wsId(value) { var n = num(value); return n > 0 && Math.floor(n) === n ? String(n) : null; }

  function wsOwners(list) {
    var owners = { projects: {}, ventures: {} };
    var o = isObject(list.owners) ? list.owners : {};
    arr(o.projects).forEach(function (p) { if (isObject(p) && wsId(p.id)) owners.projects[wsId(p.id)] = p; });
    arr(o.ventures).forEach(function (v) { if (isObject(v) && wsId(v.id)) owners.ventures[wsId(v.id)] = v; });
    return owners;
  }

  function wsFiledUnder(item) {
    var by = item.files.filter(function (f) { return wsId(f.project_id) || wsId(f.venture_id); })[0] || item.primary;
    item.project = wsId(by.project_id);
    item.venture = item.project ? null : wsId(by.venture_id);
    item.owner = item.project ? "p:" + item.project : item.venture ? "v:" + item.venture : "none";
    var wrote = null;
    item.files.forEach(function (f) {
      if (wsId(f.cycle_id) && (!wrote || wsTime(f.modified_at) > wsTime(wrote.modified_at))) wrote = f;
    });
    item.cycle = wrote ? wsId(wrote.cycle_id) : null;
    item.tool = wrote && typeof wrote.tool === "string" && wrote.tool ? wrote.tool : null;
  }

  // What an owner key stands for: its title, kind and state, and a project's venture.
  function wsOwner(owners, key) {
    var m = /^([pv]):(\d+)$/.exec(key || "");
    if (!m) return { key: "none", type: "none", id: null, known: true, title: "Not filed", state: "", icon: "", tone: "", venture: null };
    if (m[1] === "p") {
      var p = owners.projects[m[2]];
      var s = p ? PROJECT_STATUS[p.status] || {} : {};
      return { key: key, type: "project", id: m[2], known: !!p, title: p && p.title ? String(p.title) : "Project #" + m[2],
        state: p ? s.label || sentence(String(p.status || "unknown")) : "",
        icon: p && p.status === "abandoned" ? "⊘" : s.icon || "●", tone: s.tone || "",  // "–" would read as a dash
        order: p ? statusOrder(PROJECT_STATUS, p.status) : 10, venture: p ? wsId(p.venture_id) : null };
    }
    var v = owners.ventures[m[2]];
    var st = v ? VENTURE_STAGE[v.stage] || {} : {};
    return { key: key, type: "venture", id: m[2], known: !!v, title: v && v.title ? String(v.title) : "Venture #" + m[2],
      state: v ? st.label || sentence(String(v.stage || "unknown")) : "", icon: "◆", tone: st.tone || "", order: 0, venture: null };
  }

  // A venture's filter holds its own files and its projects'.
  function wsOwnedBy(item, owner, owners) {
    if (!owner || item.owner === owner) return true;
    if (owner.charAt(0) !== "v" || !item.project) return false;
    var p = owners.projects[item.project];
    return !!p && "v:" + wsId(p.venture_id) === owner;
  }

  // The venture an item's files count toward: its own, or its project's ("none" for neither).
  function wsCluster(item, owners) {
    if (item.owner === "none" || item.venture) return item.owner;
    var p = owners.projects[item.project];
    var v = p ? wsId(p.venture_id) : null;
    return v ? "v:" + v : item.owner;
  }

  // By project: a venture's own files, then each of its projects' (the one with the newest work first), the venture
  // or project of no venture with the newest work first, and the files of neither last. Newest first in each.
  function wsOwnerSorter(list, owners) {
    var newest = {};
    var cluster = {};
    list.forEach(function (item) {
      var c = wsCluster(item, owners);
      if (newest[item.owner] === undefined || item.time > newest[item.owner]) newest[item.owner] = item.time;
      if (cluster[c] === undefined || item.time > cluster[c]) cluster[c] = item.time;
    });
    return function (a, b) {
      var ca = wsCluster(a, owners);
      var cb = wsCluster(b, owners);
      if (ca !== cb) return (ca === "none") - (cb === "none") || cluster[cb] - cluster[ca] || wsCompare(ca, cb);
      if (a.owner !== b.owner) return (a.owner.charAt(0) === "p") - (b.owner.charAt(0) === "p") || newest[b.owner] - newest[a.owner] || wsCompare(a.owner, b.owner);
      return b.time - a.time || wsCompare(a.name, b.name) || wsCompare(a.key, b.key);
    };
  }

  // ---- The requests that name a file (a listing's photos and files, a book's manuscript and cover): only those the
  // owner has to decide or that are carried out, waiting first. Read from the dashboard's approvals on every poll.

  function wsRequestsByPath(d) {
    var named = {};
    arr(d && d.approvals).forEach(function (a) {
      if (!isObject(a) || !Object.prototype.hasOwnProperty.call(WS_REQUEST_ORDER, a.status)) return;
      wsPathsIn([a.action, isObject(a.kdp) ? a.kdp.files : null]).forEach(function (path) { (named[path] = named[path] || []).push(a); });
    });
    return named;
  }

  // The names of workspace files in a request's action (a listing's photos, a pin's picture), by their shape: folders
  // and a name the workspace allows, and an ending it holds.
  var WS_PATH = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}(?:\/[A-Za-z0-9][A-Za-z0-9._-]{0,63}){0,3}\.(?:pdf|docx|xlsx|pptx|png|jpg|md|txt|csv|tsv|json|ya?ml|xml|html|css|py)$/i;

  function wsPathsIn(value) {
    var found = [];
    (function walk(v, depth) {
      if (depth > 8) return;
      if (typeof v === "string") { if (v.length <= 200 && WS_PATH.test(v) && found.indexOf(v) < 0) found.push(v); }
      else if (Array.isArray(v)) v.forEach(function (x) { walk(x, depth + 1); });
      else if (isObject(v)) Object.keys(v).forEach(function (k) { walk(v[k], depth + 1); });
    })(value, 0);
    return found;
  }

  function wsItemRequests(item, named) {
    var found = {};
    var list = [];
    item.files.forEach(function (f) {
      arr(named[f.path]).forEach(function (a) {
        if (!found[a.id]) { found[a.id] = true; list.push(a); }
      });
    });
    return list.sort(function (x, y) { return WS_REQUEST_ORDER[x.status] - WS_REQUEST_ORDER[y.status] || num(y.id) - num(x.id); });
  }

  function wsRequestChip(item, long) {
    var a = item.requests[0];
    if (!a) return null;
    var s = APPROVAL_STATUS[a.status] || { icon: "", label: a.status, tone: "" };
    var label = a.status === "pending" ? "waits for you" : a.status === "done" ? "done" : "approved";
    return h("span", { class: "chip ws-req", "data-tone": s.tone || null, title: "Request #" + a.id + ": " + asText(a.title) + " (" + s.label.toLowerCase() + ")" },
      h("span", { "aria-hidden": "true", text: s.icon }), long ? "Request #" + a.id + " " + label : "#" + a.id,
      long ? null : h("span", { class: "visually-hidden", text: " " + label }));
  }

  function revealRequest(id) {
    if ($("ws-viewer").open) ui.ws.keepFocus = true;  // the request gets the focus, not what the dialog was opened with
    closeWorkspaceDialog();
    selectTab("approvals", false);
    var card = Array.prototype.filter.call($("approvals").querySelectorAll("article[data-id]"), function (c) {
      return c.getAttribute("data-id") === String(id);
    })[0];
    revealEl(card, card && card.querySelector("h3"));
  }

  // ---- What the owner saw last: items changed since are marked New (in this browser, per mode).

  function wsSeenKey(list) { return "ember-ws-seen-" + (list.mode === "dry_run" ? "dry_run" : "live"); }

  function wsNoteSeen(list, model) {
    var ws = ui.ws;
    if (ui.tab !== "workspace") return;
    var stored = num(loadPref(wsSeenKey(list), ""));
    if (ws.visit) {
      ws.seenBefore = isNaN(stored) ? null : stored;
      ws.visit = false;
    }
    var newest = 0;
    model.items.forEach(function (item) { if (item.time > newest) newest = item.time; });
    if (newest && (isNaN(stored) || newest > stored)) savePref(wsSeenKey(list), String(newest));
  }

  // ---- Loading

  function refreshWorkspace() {
    loadWorkspace();
    if (ui.ws.file) loadWorkspaceFile();
  }

  // Changes with each step of the agent's (a cycle writes files): the list loads again while the tab is open.
  function wsStamp(d) {
    var now = d && isObject(d.now) ? d.now : null;
    return now ? [now.cycle_id, now.status, now.phase, now.step].join("|") : "";
  }

  function loadWorkspace() {
    var ws = ui.ws;
    if (ws.busy) return;
    ws.busy = true;
    ws.stamp = wsStamp(ui.data);
    safely("workspace", renderWorkspace);
    request("GET", "api/workspace").then(function (res) {
      if (!res.ok) throw httpError(res);
      if (!isObject(res.data) || !Array.isArray(res.data.files)) throw new RequestError("malformed", res.data === undefined ? "not JSON" : "the file list is missing");
      ws.list = res.data;
      ws.model = wsModel(res.data);
      ws.loadedAt = new Date();
      ws.error = null;
      wsNoteSeen(ws.list, ws.model);
      // A file from the other mode's folder (dry run was switched) is not in this workspace.
      if (ws.file && ws.file.mode !== ws.list.mode) closeWorkspaceDialog();
    }).catch(function (err) {
      if (!(err instanceof RequestError)) console.error(err);
      ws.error = err;
    }).then(function () {
      ws.busy = false;
      safely("workspace", renderWorkspace);
      safely("workspaceFile", renderWorkspaceFile);
      var pending = ws.pending;
      ws.pending = null;
      if (pending) safely("workspaceFile", function () { wsOpenPending(pending); });
      // The open text changed since it was loaded: load it again.
      var file = ws.file;
      var item = file ? wsFindItem(file.key, file.path) : null;
      if (file && !file.product && item && file.text !== null && !ws.fileBusy && item.primary.modified_at !== file.modified) loadWorkspaceFile();
      safely("banners", renderBanners);
    });
  }

  // 0.26.0: a request's files (an Etsy listing's photos and files, a book's) in the viewer, from its card in Approvals:
  // a PDF's pages without downloading it. ← and → step through that request's files.
  function workspaceButton(paths) {
    var list = arr(paths).filter(function (p) { return typeof p === "string" && p; });
    if (!list.length) return null;
    return h("button", { type: "button", class: "btn btn-small ws-preview", "data-ws-preview": JSON.stringify(list) }, "View the files");
  }

  function previewWorkspaceFiles(paths, opener) {
    ui.ws.pending = { paths: paths, opener: opener };
    loadWorkspace();  // opens them once the list is in (a list on its way opens them too)
  }

  function wsOpenPending(pending) {
    var keys = [];
    pending.paths.forEach(function (path) {
      var item = ui.ws.list ? wsFindItem(null, path) : null;
      if (item && keys.indexOf(item.key) < 0) keys.push(item.key);
    });
    var b = pending.opener;
    if (!keys.length) {
      if (b && document.body.contains(b)) {
        b.textContent = ui.ws.error ? "Couldn't load the files: try again" : "Not in the workspace any more";
        b.disabled = !ui.ws.error;
      }
      return;
    }
    openWorkspaceItem(keys[0], keys, b);
  }

  // On each dashboard poll while the tab is open: the list again after the agent's steps; otherwise what changed with
  // time or the dashboard (the requests its files are in), each part rebuilt only when what it shows changed.
  function workspaceOnPoll(d) {
    var ws = ui.ws;
    if (ws.list && !ws.busy && wsStamp(d) !== ws.stamp) { loadWorkspace(); return; }
    if (ws.list) renderWorkspace();
    wsTick();
  }

  // ---- The overview

  function renderWorkspace() {
    var ws = ui.ws;
    var list = ws.list;
    var items = ws.model ? ws.model.items : [];
    var dry = list ? list.mode === "dry_run" : isDryRun(ui.data);
    $("ws-refresh").textContent = ws.busy ? "Refreshing…" : "Refresh";
    $("ws-sub").textContent = "What the agent wrote in its own folder, and the PDF, Word, Excel and picture files Ember's code " +
      "made from it. Read-only: check a file before you use it or sell it." +
      (dry ? " Dry run: this is the dry-run folder, which starts empty with every dry-run session." : "");
    setStatusText("ws-status", ws.error && !ws.busy ? "Couldn't load the files (" + errorText(ws.error) + ")." +
      (list ? " What you see was loaded at " + timeFmt.format(ws.loadedAt) + "." : " Try Refresh.") : "", ws.error && !ws.busy ? "error" : "");
    var shown = items.length > 0;
    var empty = $("ws-empty");
    empty.hidden = shown || (!list && !ws.busy);
    var emptyKey = list ? "empty" : "loading";
    if (!shown && empty.getAttribute("data-key") !== emptyKey) {
      empty.setAttribute("data-key", emptyKey);
      replace(empty, list ? [h("p", { class: "empty-title", text: "No files yet." }), h("p", { class: "muted", text: "Drafts, notes and research show up here " +
        "once the agent writes them, and its products once it makes them." })] : h("p", { class: "muted", text: "Loading the files…" }));
    }
    ["ws-stats", "ws-recent-part", "ws-browser"].forEach(function (id) { $(id).hidden = !shown; });
    if (!shown) {
      $("ws-alerts").hidden = true;
      $("ws-owners-part").hidden = true;
      return;
    }
    var named = wsRequestsByPath(ui.data);
    items.forEach(function (item) {
      item.requests = wsItemRequests(item, named);
      item.fresh = ws.seenBefore !== null && item.time > ws.seenBefore;
    });
    renderWsStats(list, items);
    renderWsAlerts(list, items);
    renderWsOwners(items);
    renderWsRecent(items);
    renderWsBrowser(items);
    wsRevealList();
  }

  function renderWsStats(list, items) {
    var kinds = { document: 0, spreadsheet: 0, picture: 0, text: 0 };
    var texts = {};
    var newest = null;
    items.forEach(function (item) {
      kinds[item.kind]++;
      if (item.text) texts[item.text] = (texts[item.text] || 0) + 1;
      if (!newest || item.time > newest.time) newest = item;
    });
    var products = kinds.document + kinds.spreadsheet + kinds.picture;
    var day = 0;
    var since = Date.now() - 86400000;
    items.forEach(function (item) { if (item.time > since) day++; });
    var limits = isObject(list.limits) ? list.limits : {};
    var el = $("ws-stats");
    var key = JSON.stringify([kinds, texts, newest.key, newest.modified, newest.folder, day, list.text_bytes, list.product_bytes,
      list.file_count, list.folder_count, limits]);
    if (el.getAttribute("data-key") === key || isBusy(el)) return;  // its link has focus: rebuilt with the next change
    el.setAttribute("data-key", key);
    function stat(label, value, sub, cls) {
      return h("div", { class: "ws-stat" + (cls ? " " + cls : "") }, h("dt", { text: label }),
        h("dd", { class: "ws-stat-value" }, value), sub ? h("dd", { class: "ws-stat-sub" }, sub) : null);
    }
    var made = ["document", "spreadsheet", "picture"].filter(function (k) { return kinds[k]; }).map(function (k) { return wsCount(kinds[k], WS_KINDS[k]); });
    var written = Object.keys(WS_TEXTS).filter(function (k) { return texts[k]; }).map(function (k) { return wsCount(texts[k], WS_TEXTS[k]); });
    var text = num(list.text_bytes) || 0;
    var product = num(list.product_bytes) || 0;
    replace(el, [
      stat("Products", intFmt.format(products), made.join(" · ") || "none yet"),
      stat("Text files", intFmt.format(kinds.text), written.join(" · ") || "none yet"),
      stat("Last change", wsWhen(newest.modified, true), [h("button", { type: "button", class: "link-button ws-link", "data-ws-open": newest.key, text: newest.name }),
        " in " + wsFolderName(newest.folder), day > 1 ? h("span", { class: "ws-stat-line", text: intFmt.format(day) + " items changed in the last 24 hours" }) : null]),
      h("div", { class: "ws-stat ws-space" }, h("dt", { text: "Space" }),
        h("dd", { class: "ws-stat-value", text: byteSize(text + product) }),
        h("dd", { class: "ws-meters" }, wsMeter("Text files", text, limits.text_bytes), wsMeter("Products", product, limits.product_bytes)),
        h("dd", { class: "ws-stat-sub", text: count(list.file_count) + (num(limits.files) ? " of " + count(limits.files) : "") + " files · " +
          count(list.folder_count) + (num(limits.folders) ? " of " + count(limits.folders) : "") + " folders" })),
    ]);
  }

  // How much of what it may hold a kind of file takes. A sliver shows as soon as anything is used.
  function wsMeter(label, used, cap) {
    var limit = num(cap);
    var ratio = limit > 0 ? used / limit : 0;
    var fill = h("div", { class: "meter-fill" });
    fill.style.width = (used > 0 ? Math.max(1.5, Math.min(100, ratio * 100)) : 0).toFixed(1) + "%";
    var text = byteSize(used) + (limit > 0 ? " of " + byteSize(limit) : "");
    return h("div", { class: "ws-meter" },
      h("span", { class: "ws-meter-label", text: label }),
      h("div", { class: "meter", role: "meter", "aria-label": label + ": space used", "aria-valuemin": "0", "aria-valuemax": "100",
        "aria-valuenow": String(Math.min(100, Math.round(ratio * 100))), "aria-valuetext": text + " (" + pct(ratio) + ")",
        "data-level": ratio >= 0.95 ? "critical" : ratio >= 0.8 ? "warning" : "normal" }, fill),
      h("span", { class: "ws-meter-value", text: text }));
  }

  function renderWsAlerts(list, items) {
    var alerts = [];
    var waiting = {};
    var order = [];
    items.forEach(function (item) {
      item.requests.forEach(function (a) {
        if (a.status !== "pending") return;
        if (!waiting[a.id]) { waiting[a.id] = { a: a, items: 0 }; order.push(a.id); }
        waiting[a.id].items++;
      });
    });
    if (order.length) {
      var n = 0;
      order.forEach(function (id) { n += waiting[id].items; });
      alerts.push(wsAlert("warning", "◔", [plural(n, "item") + (n === 1 ? " is" : " are") + " in " +
        (order.length === 1 ? "a request that waits" : order.length + " requests that wait") + " for your decision. Check " +
        (n === 1 ? "it" : "them") + " here before you approve."],
        order.map(function (id) {
          var a = waiting[id].a;
          return h("button", { type: "button", class: "chip chip-button ws-req-button", "data-tone": "warning", "data-ws-request": String(a.id),
            "aria-label": "Request #" + a.id + ": " + asText(a.title) + ". Open it in Approvals" },
            h("span", { class: "chip-text", text: "#" + a.id + " " + asText(a.title) }), h("span", { class: "chip-arrow", "aria-hidden": "true", text: "→" }));
        })));
    }
    var limits = isObject(list.limits) ? list.limits : {};
    [["text_bytes", "text_bytes", "Text files", true], ["product_bytes", "product_bytes", "Products", true],
      ["file_count", "files", "Files", false], ["folder_count", "folders", "Folders", false]].forEach(function (s) {
      var used = num(list[s[0]]) || 0;
      var cap = num(limits[s[1]]);
      if (!(cap > 0) || used / cap < 0.8) return;
      var amount = s[3] ? byteSize(used) + " of the " + byteSize(cap) : count(used) + " of the " + count(cap);
      alerts.push(wsAlert(used / cap >= 0.95 ? "error" : "warning", "▲", [s[2] + " take " + amount + " the workspace may hold. Once " +
        (s[3] ? "they fill it" : "it is full") + ", the agent's new files are refused until it deletes old ones; it hears so when it writes."]));
    });
    if (list.truncated) {
      alerts.push(wsAlert("warning", "!", ["The workspace holds more than the list shows: only its first " + count(arr(list.files).length) +
        " files are listed, by path."]));
    }
    var el = $("ws-alerts");
    el.hidden = !alerts.length;
    var key = JSON.stringify([order.map(function (id) { return [id, waiting[id].items, waiting[id].a.title]; }), list.text_bytes,
      list.product_bytes, list.file_count, list.folder_count, list.truncated, limits]);
    if (el.getAttribute("data-key") === key || isBusy(el)) return;  // a button with focus is rebuilt with the next change
    el.setAttribute("data-key", key);
    replace(el, alerts);
  }

  function wsAlert(kind, icon, text, actions) {
    return h("li", { class: "ws-alert", "data-kind": kind },
      h("span", { class: "ws-alert-icon", "aria-hidden": "true", text: icon }),
      h("div", { class: "ws-alert-body" }, h("p", null, text), actions && actions.length ? h("div", { class: "ws-alert-actions" }, actions) : null));
  }

  // The projects and ventures with files, the one with the newest work first (WS_OWNERS of them, then the
  // files of none): what each has, its latest items' pictures, and what of it waits for the owner or is new. Each
  // shows only its files in the list below (pressed again, all of them).
  function renderWsOwners(items) {
    var ws = ui.ws;
    var owners = ws.model.owners;
    var groups = {};
    var list = [];
    function add(key, item) {
      var g = groups[key];
      if (!g) list.push(g = groups[key] = { key: key, items: [], newest: null, products: 0, texts: 0, waiting: 0, fresh: 0, projects: {} });
      g.items.push(item);
      if (!g.newest || item.time > g.newest.time) g.newest = item;
      if (item.kind === "text") g.texts++;
      else g.products++;
      if (item.requests.some(function (a) { return a.status === "pending"; })) g.waiting++;
      if (item.fresh) g.fresh++;
      if (item.project && key !== item.owner) g.projects[item.project] = true;
    }
    items.forEach(function (item) {
      add(item.owner, item);
      var c = wsCluster(item, owners);
      if (c !== item.owner) add(c, item);  // a venture holds its projects' items too
    });
    var filed = list.filter(function (g) { return g.key !== "none"; });
    var part = $("ws-owners-part");
    part.hidden = !filed.length;
    if (!filed.length) return;
    filed.sort(function (a, b) { return b.newest.time - a.newest.time || wsCompare(a.key, b.key); });
    var shown = filed.slice(0, WS_OWNERS).concat(groups.none ? [groups.none] : []);
    var projects = filed.filter(function (g) { return g.key.charAt(0) === "p"; }).length;
    $("ws-owners-note").textContent = "· " + [projects ? plural(projects, "project") : "", filed.length - projects ? plural(filed.length - projects, "venture") : ""]
      .filter(Boolean).join(", ") + ", the newest work first";
    var el = $("ws-owners");
    var key = JSON.stringify([shown.map(function (g) {
      return [g.key, g.items.length, g.products, g.texts, g.waiting, g.fresh, g.newest.key, g.newest.modified,
        wsOwnerPictures(g).map(function (item) { return [item.key, item.thumb && item.thumb.modified_at]; })];
    }), filed.length, ws.owner, ws.model.ownersKey]);
    if (el.getAttribute("data-key") === key) return;
    var focused = el.contains(document.activeElement) ? document.activeElement.getAttribute("data-ws-owner") || "all" : null;
    el.setAttribute("data-key", key);
    replace(el, shown.map(function (g) { return wsOwnerCard(g, wsOwner(owners, g.key)); }).concat(filed.length > WS_OWNERS ? [
      h("li", { class: "ws-owners-all" }, h("button", { type: "button", class: "link-button", "data-ws-owners": "all" },
        "All " + intFmt.format(filed.length) + " in the list, grouped", h("span", { "aria-hidden": "true", text: " →" })))] : []));
    if (focused) {
      Array.prototype.forEach.call(el.querySelectorAll("button"), function (b) {
        if ((b.getAttribute("data-ws-owner") || "all") === focused) b.focus();
      });
    }
  }

  // Its newest items, three at most, those with a picture first.
  function wsOwnerPictures(g) {
    return g.items.slice().sort(function (a, b) { return !!b.thumb - !!a.thumb || b.time - a.time; }).slice(0, 3);
  }

  function wsOwnerCard(g, o) {
    var what = [g.products ? wsCount(g.products, { one: "product", many: "products" }) : "", g.texts ? wsCount(g.texts, WS_KINDS.text) : ""];
    return h("li", null, h("button", { type: "button", class: "ws-owner-card", "data-ws-owner": g.key, "data-type": o.type,
      "aria-pressed": String(ui.ws.owner === g.key) },
      h("span", { class: "ws-owner-pics", "aria-hidden": "true" }, wsOwnerPictures(g).map(function (item) { return wsThumb(item, "ws-owner-pic"); })),
      h("span", { class: "ws-owner-card-title" },
        o.icon ? h("span", { class: "ws-owner-icon", "data-tone": o.tone || null, "aria-hidden": "true", text: o.icon }) : null,
        h("span", { class: "visually-hidden", text: "Show the files of " }),
        h("span", { class: "ws-owner-name", text: o.type === "none" ? "Not filed" : o.title }), h("span", { class: "visually-hidden", text: ". " })),
      h("span", { class: "ws-owner-state", text: o.type === "none" ? "No project or venture in focus" : (o.type === "venture" ? "Venture" : "Project") +
        (o.state ? " · " + o.state : "") + (Object.keys(g.projects).length ? " · " + plural(Object.keys(g.projects).length, "project") : "") },
        h("span", { class: "visually-hidden", text: ". " })),
      h("span", { class: "ws-owner-what" }, what.filter(Boolean).join(" · "), h("span", { "aria-hidden": "true", text: " · " }),
        h("span", { class: "visually-hidden", text: ", changed " }), wsWhen(g.newest.modified)),
      g.waiting || g.fresh ? h("span", { class: "ws-owner-flags" }, h("span", { class: "visually-hidden", text: ". " }),
        g.fresh ? h("span", { class: "ws-new", text: intFmt.format(g.fresh) + " new" }) : null,
        g.waiting ? h("span", { class: "chip ws-req", "data-tone": "warning" }, h("span", { "aria-hidden": "true", text: "◔" }),
          intFmt.format(g.waiting) + " waiting for you") : null) : null));
  }

  function renderWsRecent(items) {
    var ws = ui.ws;
    var recent = items.slice().sort(function (a, b) { return b.time - a.time || wsCompare(a.name, b.name); }).slice(0, WS_RECENT);
    ws.recentOrder = recent.map(function (item) { return item.key; });
    var fresh = items.filter(function (item) { return item.fresh; }).length;
    $("ws-recent-note").textContent = fresh ? "· " + intFmt.format(fresh) + " new since you last looked" : "";
    var el = $("ws-recent");
    var open = ws.file ? ws.file.key : null;
    var key = JSON.stringify(recent.map(function (item) { return [item.key, item.modified, item.size, item.thumb && item.thumb.path, item.fresh, wsReqState(item)]; }));
    if (el.getAttribute("data-key") === key) return;
    var focused = el.contains(document.activeElement) ? document.activeElement.getAttribute("data-ws-open") : null;
    el.setAttribute("data-key", key);
    replace(el, recent.map(function (item) {
      return h("li", null, h("button", { type: "button", class: "ws-tile", "data-ws-open": item.key, "data-from": "recent", "aria-current": item.key === open ? "true" : null },
        wsThumb(item, "ws-tile-thumb"),
        item.fresh ? h("span", { class: "ws-new ws-tile-new", text: "New" }) : null,
        h("span", { class: "ws-tile-name", text: item.name }),
        h("span", { class: "ws-tile-meta" }, item.formats.slice(0, 2).join(" · "), " · ", wsWhen(item.modified)),
        wsRequestChip(item, false)));
    }));
    wsRefocus(el, focused);
  }

  function wsReqState(item) { return item.requests.map(function (a) { return [a.id, a.status]; }); }

  function wsRefocus(el, key) {
    if (key === null) return;
    Array.prototype.forEach.call(el.querySelectorAll("[data-ws-open]"), function (b) { if (b.getAttribute("data-ws-open") === key) b.focus(); });
  }

  // A small picture of the item (a page, a sheet, the picture itself), or its kind in letters.
  function wsThumb(item, cls) {
    var box = h("span", { class: "ws-thumb" + (cls ? " " + cls : ""), "data-kind": item.kind });
    var badge = h("span", { class: "ws-badge", "aria-hidden": "true", text: item.ext.toUpperCase() || "FILE" });
    if (!item.thumb) {
      append(box, badge);
      return box;
    }
    var img = h("img", { src: wsThumbUrl(item.thumb), alt: "", loading: "lazy", decoding: "async" });
    img.addEventListener("error", function () { box.removeAttribute("data-picture"); replace(box, badge); });
    box.setAttribute("data-picture", "true");
    append(box, img);
    return box;
  }

  // ---- The browser: search, kind, folder, sort and layout (the last three kept in this browser)

  function initWorkspace() {
    var ws = ui.ws;
    if (WS_KIND_FILTERS.indexOf(ws.kind) < 0) ws.kind = "all";
    if (WS_SORTS.indexOf(ws.sort) < 0) ws.sort = "project";
    if (WS_VIEWS.indexOf(ws.view) < 0) ws.view = "list";
    Array.prototype.forEach.call(document.querySelectorAll("[data-ws-kind]"), function (b) {
      b.addEventListener("click", function () { setWorkspaceView({ kind: b.getAttribute("data-ws-kind") }); });
    });
    Array.prototype.forEach.call(document.querySelectorAll("[data-ws-view]"), function (b) {
      b.addEventListener("click", function () { setWorkspaceView({ view: b.getAttribute("data-ws-view") }); });
    });
    $("ws-sort").addEventListener("change", function () { setWorkspaceView({ sort: $("ws-sort").value }); });
    $("ws-folder").addEventListener("change", function () { setWorkspaceView({ folder: $("ws-folder").value }); });
    $("ws-owner").addEventListener("change", function () { setWorkspaceView({ owner: $("ws-owner").value }); });
    var typing = null;
    $("ws-search").addEventListener("input", function () {
      window.clearTimeout(typing);
      typing = window.setTimeout(function () { setWorkspaceView({ query: $("ws-search").value }); }, 150);
    });
    $("ws-search").addEventListener("keydown", function (ev) {
      if (ev.key === "Escape" && $("ws-search").value) {
        ev.preventDefault();
        window.clearTimeout(typing);
        $("ws-search").value = "";
        setWorkspaceView({ query: "" });
      }
    });
    $("ws-refresh").addEventListener("click", refreshWorkspace);
    $("approvals").addEventListener("click", function (ev) {
      var b = ev.target.closest ? ev.target.closest("[data-ws-preview]") : null;
      if (!b) return;
      var paths;
      try { paths = JSON.parse(b.getAttribute("data-ws-preview")); } catch (e) { return; }
      previewWorkspaceFiles(arr(paths), b);
    });
    $("panel-workspace").addEventListener("click", function (ev) {
      var t = ev.target.closest ? ev.target.closest("[data-ws-open], [data-ws-request], [data-ws-more], [data-ws-clear], [data-ws-owner], [data-ws-owners], [data-ws-reveal]") : null;
      if (!t) return;
      if (t.hasAttribute("data-ws-open")) {
        openWorkspaceItem(t.getAttribute("data-ws-open"), t.getAttribute("data-from") === "recent" ? ws.recentOrder : ws.order, t);
      } else if (t.hasAttribute("data-ws-request")) {
        revealRequest(t.getAttribute("data-ws-request"));
      } else if (t.hasAttribute("data-ws-reveal")) {
        wsReveal(t.getAttribute("data-ws-reveal"));
      } else if (t.hasAttribute("data-ws-owner")) {
        // An overview's project: only its files below (pressed again, all of them).
        var owner = t.getAttribute("data-ws-owner");
        if (ws.owner === owner) { setWorkspaceView({ owner: "" }); return; }
        ws.reveal = Date.now();
        setWorkspaceView({ owner: owner });
        wsRevealList();
      } else if (t.hasAttribute("data-ws-owners")) {
        ws.reveal = Date.now();
        setWorkspaceView({ owner: "", sort: "project" });
        wsRevealList();
      } else if (t.hasAttribute("data-ws-more")) {
        var first = ws.order[ws.shown];
        ws.shown += WS_PAGE;
        renderWsBrowser(ws.model ? ws.model.items : []);
        wsRefocus($("ws-list"), first || null);
      } else {
        $("ws-search").value = "";
        setWorkspaceView({ query: "", folder: "", owner: "", kind: "all" });
        $("ws-search").focus();
      }
    });
    ws.shown = WS_PAGE;
    syncWorkspaceControls();
    initWorkspaceViewer();
  }

  function setWorkspaceView(change) {
    var ws = ui.ws;
    if (change.kind !== undefined && WS_KIND_FILTERS.indexOf(change.kind) >= 0) { ws.kind = change.kind; savePref("ember-ws-kind", ws.kind); }
    if (change.sort !== undefined && WS_SORTS.indexOf(change.sort) >= 0) { ws.sort = change.sort; savePref("ember-ws-sort", ws.sort); }
    if (change.view !== undefined && WS_VIEWS.indexOf(change.view) >= 0) { ws.view = change.view; savePref("ember-ws-view", ws.view); }
    if (change.folder !== undefined) ws.folder = String(change.folder);
    if (change.owner !== undefined) ws.owner = String(change.owner);
    if (change.query !== undefined) ws.query = String(change.query).trim().toLowerCase();
    ws.shown = WS_PAGE;
    syncWorkspaceControls();
    if (ws.model) safely("workspace", function () { renderWsBrowser(ws.model.items); renderWsOwners(ws.model.items); });
  }

  // From a project's card or a venture's: the workspace with only its files.
  function showWorkspaceFor(owner) {
    $("ws-search").value = "";
    ui.ws.reveal = Date.now();
    setWorkspaceView({ owner: owner, folder: "", kind: "all", query: "" });
    selectTab("workspace", false);
    wsRevealList();  // now if the list is in, else once it is
  }

  // The list brought into view (its heading gets the focus) after the owner asked for a project's files: once it is
  // shown, if that was a moment ago.
  function wsRevealList() {
    var ws = ui.ws;
    if (!ws.reveal || ui.tab !== "workspace" || $("ws-browser").hidden) return;
    var asked = ws.reveal;
    ws.reveal = false;
    if (Date.now() - asked > 15000) return;
    var motion = !(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    $("ws-browser").scrollIntoView({ block: "start", behavior: motion ? "smooth" : "auto" });
    $("ws-files-title").focus({ preventScroll: true });
  }

  // A project's or a venture's card in Ventures (from the list's groups and the viewer).
  function wsReveal(key) {
    var m = /^([pv]):(\d+)$/.exec(key || "");
    if (!m) return;
    if ($("ws-viewer").open) ui.ws.keepFocus = true;  // the card gets the focus, not what the dialog was opened with
    closeWorkspaceDialog();
    if (m[1] === "p") revealProject(m[2]);
    else revealVenture(Number(m[2]));
  }

  function wsRevealCycle(id) {
    if ($("ws-viewer").open) ui.ws.keepFocus = true;
    closeWorkspaceDialog();
    revealCycle(id);
  }

  function syncWorkspaceControls() {
    var ws = ui.ws;
    Array.prototype.forEach.call(document.querySelectorAll("[data-ws-kind]"), function (b) {
      b.setAttribute("aria-pressed", String(b.getAttribute("data-ws-kind") === ws.kind));
    });
    Array.prototype.forEach.call(document.querySelectorAll("[data-ws-view]"), function (b) {
      b.setAttribute("aria-pressed", String(b.getAttribute("data-ws-view") === ws.view));
    });
    $("ws-sort").value = ws.sort;
    $("ws-list").setAttribute("data-view", ws.view);
    wsShowKind();
  }

  // The kind shown stays in sight where its row scrolls sideways (a phone). Laid out only while the tab is shown.
  function wsShowKind() {
    var row = document.querySelector(".ws-kinds");
    var on = row.querySelector('[aria-pressed="true"]');
    if (!on || !row.clientWidth) return;
    if (on.offsetLeft < row.scrollLeft || on.offsetLeft + on.offsetWidth > row.scrollLeft + row.clientWidth) row.scrollLeft = on.offsetLeft - 4;
  }

  function wsInFolder(item, folder) { return !folder || item.folder === folder || item.folder.indexOf(folder + "/") === 0; }

  function wsMatches(item, query) {
    if (!query) return true;
    return query.split(/\s+/).every(function (term) { return item.search.indexOf(term) >= 0; });
  }

  function wsSorter(sort) {
    return function (a, b) {
      var by = sort === "newest" ? b.time - a.time : sort === "size" ? b.size - a.size : sort === "folder" ? wsCompare(a.folder, b.folder) : 0;
      return by || wsCompare(a.name, b.name) || wsCompare(a.key, b.key);
    };
  }

  function renderWsFolders(items) {
    var ws = ui.ws;
    var counts = {};
    items.forEach(function (item) {
      var parts = item.folder ? item.folder.split("/") : [];
      for (var i = 1; i <= parts.length; i++) {
        var folder = parts.slice(0, i).join("/");
        counts[folder] = (counts[folder] || 0) + 1;
      }
    });
    if (ws.folder && !counts[ws.folder]) ws.folder = "";
    var folders = Object.keys(counts).sort(wsCompare);
    var select = $("ws-folder");
    var key = JSON.stringify([folders, counts, items.length]);
    if (select.getAttribute("data-key") !== key) {
      select.setAttribute("data-key", key);
      replace(select, [h("option", { value: "", text: "All (" + intFmt.format(items.length) + ")" })].concat(folders.map(function (folder) {
        return h("option", { value: folder, text: folder + "/ (" + intFmt.format(counts[folder]) + ")" });
      })));
    }
    select.value = ws.folder;
    select.disabled = !folders.length;
  }

  // The projects and ventures with files (a venture counts its projects' too), open projects first; those not
  // filed under one; one asked for from elsewhere is kept with none.
  function renderWsOwnerSelect(items, owners) {
    var ws = ui.ws;
    var counts = {};
    items.forEach(function (item) {
      counts[item.owner] = (counts[item.owner] || 0) + 1;
      var c = wsCluster(item, owners);
      if (c !== item.owner) counts[c] = (counts[c] || 0) + 1;
    });
    var keys = Object.keys(counts).filter(function (k) { return k !== "none"; });
    if (/^[pv]:/.test(ws.owner) && keys.indexOf(ws.owner) < 0) keys.push(ws.owner);
    var list = keys.map(function (k) { return wsOwner(owners, k); });
    var projects = list.filter(function (o) { return o.type === "project"; }).sort(function (a, b) { return a.order - b.order || wsCompare(a.title, b.title); });
    var ventures = list.filter(function (o) { return o.type === "venture"; }).sort(function (a, b) { return wsCompare(a.title, b.title); });
    var select = $("ws-owner");
    var key = JSON.stringify([counts, list.map(function (o) { return [o.key, o.title, o.state]; }), items.length]);
    if (select.getAttribute("data-key") !== key) {
      select.setAttribute("data-key", key);
      var option = function (o) {
        var closed = o.type === "project" && o.known && o.order >= statusOrder(PROJECT_STATUS, "succeeded");
        return h("option", { value: o.key, text: o.title + (closed ? " · " + o.state : "") + " (" + intFmt.format(counts[o.key] || 0) + ")" });
      };
      replace(select, [h("option", { value: "", text: "All (" + intFmt.format(items.length) + ")" }),
        projects.length ? h("optgroup", { label: "Projects" }, projects.map(option)) : null,
        ventures.length ? h("optgroup", { label: "Ventures" }, ventures.map(option)) : null,
        counts.none && list.length || ws.owner === "none" ? h("option", { value: "none", text: "Not filed (" + intFmt.format(counts.none || 0) + ")" }) : null]);
    }
    select.value = ws.owner;
    select.disabled = !list.length && ws.owner !== "none";
  }

  function renderWsBrowser(items) {
    var ws = ui.ws;
    var owners = ws.model ? ws.model.owners : { projects: {}, ventures: {} };
    renderWsFolders(items);
    renderWsOwnerSelect(items, owners);
    wsShowKind();
    var inScope = items.filter(function (item) { return wsInFolder(item, ws.folder) && wsOwnedBy(item, ws.owner, owners) && wsMatches(item, ws.query); });
    var counts = { all: inScope.length, document: 0, spreadsheet: 0, picture: 0, text: 0 };
    inScope.forEach(function (item) { counts[item.kind]++; });
    WS_KIND_FILTERS.forEach(function (k) { $("ws-count-" + k).textContent = intFmt.format(counts[k]); });
    var shown = inScope.filter(function (item) { return ws.kind === "all" || item.kind === ws.kind; });
    shown.sort(ws.sort === "project" ? wsOwnerSorter(shown, owners) : wsSorter(ws.sort));
    ws.order = shown.map(function (item) { return item.key; });
    var files = 0;
    var filed = 0;
    items.forEach(function (item) { files += item.files.length; if (item.owner !== "none") filed++; });
    $("ws-files-sub").textContent = plural(items.length, "item") + " from " + plural(files, "file") +
      ": a product's PDF, Word copy and pictures are one item" + (filed ? ", filed under the project or venture it was written for." : ".");
    var owner = ws.owner ? wsOwner(owners, ws.owner) : null;
    var ownerText = !owner ? "" : owner.type === "none" ? " not filed under a project or venture" : " for " + owner.title;
    var filtered = ws.query || ws.folder || ws.owner || ws.kind !== "all";
    var result = filtered ? [intFmt.format(shown.length) + " of " + plural(items.length, "item"),
      ws.kind !== "all" ? " · " + WS_KINDS[ws.kind].many : "", ownerText, ws.folder ? " in " + ws.folder + "/" : "",
      ws.query ? " matching “" + ws.query + "”" : ""].join("") : "";
    var resultEl = $("ws-result");
    if (resultEl.getAttribute("data-text") !== result) {
      resultEl.setAttribute("data-text", result);
      replace(resultEl, filtered ? [result + " ", h("button", { type: "button", class: "link-button ws-clear", "data-ws-clear": "true", text: "Show everything" })] : []);
    }
    resultEl.hidden = !filtered;
    var el = $("ws-list");
    var page = shown.slice(0, Math.max(WS_PAGE, ws.shown || WS_PAGE));
    var open = ws.file ? ws.file.key : null;
    var total = 0;
    shown.forEach(function (item) { total += item.size; });
    // Each item says whose it is where the list doesn't: not grouped by project, nor only one project's.
    var withOwner = filed > 0 && ws.sort !== "project" && !/^p:|^none$/.test(ws.owner);
    var key = JSON.stringify([page.map(function (item) { return [item.key, item.modified, item.size, item.formats, item.fresh, item.owner, wsReqState(item)]; }),
      shown.length, total, ws.sort, withOwner, ws.model ? ws.model.ownersKey : null]);
    if (el.getAttribute("data-key") === key) return;
    var focused = el.contains(document.activeElement) ? document.activeElement.getAttribute("data-ws-open") : null;
    el.setAttribute("data-key", key);
    if (!shown.length) {
      replace(el, h("div", { class: "empty-state" }, h("p", { class: "empty-title", text: "Nothing matches." }),
        h("p", { class: "muted" }, "No " + (ws.kind === "all" ? "items" : WS_KINDS[ws.kind].many) + ownerText + (ws.folder ? " in " + ws.folder + "/" : "") +
          (ws.query ? " match “" + ws.query + "”" : "") + ". ", h("button", { type: "button", class: "link-button", "data-ws-clear": "true", text: "Show everything" }))));
      return;
    }
    var parts = [];
    if (ws.sort === "project") {
      parts = wsOwnerGroups(page, shown, owners, open);
    } else if (ws.sort === "folder") {
      var groups = [];
      page.forEach(function (item) {
        var last = groups[groups.length - 1];
        if (!last || last.key !== item.folder) groups.push(last = { key: item.folder, items: [] });
        last.items.push(item);
      });
      groups.forEach(function (g, i) {
        var id = "ws-group-" + i;
        parts.push(h("section", { class: "ws-group", "aria-labelledby": id },
          h("h3", { class: "ws-group-head", id: id }, h("span", { class: "ws-group-name", text: g.key ? g.key + "/" : "Top folder" }),
            h("span", { class: "ws-group-meta", text: wsGroupStats(shown, function (item) { return item.folder === g.key; }) })),
          h("ul", { class: "ws-items" }, g.items.map(function (item) { return wsItemEl(item, open, false, withOwner, owners); }))));
      });
    } else {
      parts.push(h("ul", { class: "ws-items" }, page.map(function (item) { return wsItemEl(item, open, true, withOwner, owners); })));
    }
    var left = shown.length - page.length;
    if (left > 0) {
      parts.push(h("button", { type: "button", class: "btn ws-more", "data-ws-more": "true" },
        "Show " + intFmt.format(Math.min(WS_PAGE, left)) + " more", h("span", { class: "muted", text: " · " + intFmt.format(left) + " not shown yet" })));
    }
    replace(el, parts);
    wsRefocus(el, focused);
  }

  // How many of the items listed a group has (all of them, not only those shown so far), and their size.
  function wsGroupStats(shown, test) {
    var n = 0;
    var size = 0;
    shown.forEach(function (item) { if (test(item)) { n++; size += item.size; } });
    return plural(n, "item") + " · " + byteSize(size);
  }

  // By project: a venture's section holds its own files, then a group for each of its projects; a project of no
  // venture is a group of its own, and the files of neither come last.
  function wsOwnerGroups(page, shown, owners, open) {
    var clusters = [];
    page.forEach(function (item) {
      var key = wsCluster(item, owners);
      var c = clusters[clusters.length - 1];
      if (!c || c.key !== key) clusters.push(c = { key: key, groups: [] });
      var g = c.groups[c.groups.length - 1];
      if (!g || g.key !== item.owner) c.groups.push(g = { key: item.owner, items: [] });
      g.items.push(item);
    });
    var n = 0;
    function list(items) { return h("ul", { class: "ws-items" }, items.map(function (item) { return wsItemEl(item, open, true, false, owners); })); }
    function own(key) { return function (item) { return item.owner === key; }; }
    return clusters.map(function (c) {
      var id = "ws-group-" + n++;
      if (c.key.charAt(0) !== "v") {
        return h("section", { class: "ws-group", "aria-labelledby": id },
          wsOwnerHead(wsOwner(owners, c.key), "h3", id, wsGroupStats(shown, own(c.key))), list(c.groups[0].items));
      }
      var projects = {};
      var inVenture = function (item) { return wsCluster(item, owners) === c.key; };
      shown.forEach(function (item) { if (item.project && inVenture(item)) projects[item.project] = true; });
      var count = Object.keys(projects).length;
      return h("section", { class: "ws-group ws-venture", "aria-labelledby": id },
        wsOwnerHead(wsOwner(owners, c.key), "h3", id, (count ? plural(count, "project") + " · " : "") + wsGroupStats(shown, inVenture)),
        h("div", { class: "ws-venture-body" }, c.groups.map(function (g) {
          if (g.key === c.key) return list(g.items);  // its own files: research, what it learned
          var gid = "ws-group-" + n++;
          return h("section", { class: "ws-group ws-subgroup", "aria-labelledby": gid },
            wsOwnerHead(wsOwner(owners, g.key), "h4", gid, wsGroupStats(shown, own(g.key))), list(g.items));
        })));
    });
  }

  // A project's group (a venture's): its title and state, and a way to it in Ventures.
  function wsOwnerHead(o, tag, id, stats) {
    var what = o.type === "none" ? "written with no project or venture in focus"
      : (o.type === "venture" ? "Venture" : "Project") + (o.state ? " · " + o.state : "");
    return h("div", { class: "ws-group-head ws-owner-head", "data-type": o.type },
      h(tag, { class: "ws-owner-title", id: id },
        o.icon ? h("span", { class: "ws-owner-icon", "data-tone": o.tone || null, "aria-hidden": "true", text: o.icon }) : null,
        h("span", { class: "ws-owner-name", text: o.type === "none" ? "Not filed under a project" : o.title })),
      h("span", { class: "ws-group-meta", text: what + " · " + stats }),
      o.type !== "none" && o.known ? h("button", { type: "button", class: "link-button ws-owner-go", "data-ws-reveal": o.key,
        "aria-label": o.title + ": open it in Ventures" }, "Open in Ventures", h("span", { "aria-hidden": "true", text: " →" })) : null);
  }

  function wsItemEl(item, open, withFolder, withOwner, owners) {
    var o = withOwner && item.owner !== "none" ? wsOwner(owners, item.owner) : null;
    var sub = [withFolder && item.folder ? h("span", { class: "ws-item-path", text: item.folder + "/" }) : null,
      o ? h("span", { class: "ws-item-owner", "data-type": o.type },
        h("span", { class: "ws-owner-icon", "data-tone": o.tone || null, "aria-hidden": "true", text: o.icon }),
        h("span", { class: "visually-hidden", text: o.type === "venture" ? "Venture: " : "Project: " }), o.title) : null].filter(Boolean);
    return h("li", { class: "ws-item" },
      h("button", { type: "button", class: "ws-open", "data-ws-open": item.key, "aria-current": item.key === open ? "true" : null },
        wsThumb(item, "ws-item-thumb"),
        h("span", { class: "ws-item-main" },
          h("span", { class: "ws-item-name", text: item.name }),
          sub.length ? h("span", { class: "ws-item-sub" }, sub) : null),
        h("span", { class: "ws-item-tags" },
          item.fresh ? h("span", { class: "ws-new", text: "New" }) : null,
          item.kind === "text" ? null : item.formats.map(function (f) { return h("span", { class: "ws-format", text: f }); }),
          item.sheets > 1 ? h("span", { class: "ws-format", text: item.sheets + " sheets" }) : null,
          wsRequestChip(item, true)),
        h("span", { class: "ws-item-size", text: byteSize(item.size) }),
        h("span", { class: "ws-item-when" }, wsWhen(item.modified))));
  }

  // ---- The viewer: one item in a dialog; ← and → step through the list it was opened from.

  function initWorkspaceViewer() {
    var dialog = $("ws-viewer");
    $("ws-close").addEventListener("click", closeWorkspaceDialog);
    $("ws-prev").addEventListener("click", function () { wsStep(-1); });
    $("ws-next").addEventListener("click", function () { wsStep(1); });
    // A click on the backdrop (outside the dialog's box) closes it too.
    dialog.addEventListener("click", function (ev) { if (ev.target === dialog) closeWorkspaceDialog(); });
    dialog.addEventListener("close", onWorkspaceDialogClosed);
    dialog.addEventListener("keydown", function (ev) {
      if (ev.altKey || ev.ctrlKey || ev.metaKey || ev.shiftKey || (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight")) return;
      // Where the arrow keys move something else: fields, and tables that scroll sideways.
      if (ev.target.closest && ev.target.closest("input, select, textarea, .table-wrap")) return;
      ev.preventDefault();
      wsStep(ev.key === "ArrowLeft" ? -1 : 1);
    });
    Array.prototype.forEach.call(document.querySelectorAll("[data-ws-as]"), function (b) {
      b.addEventListener("click", function () {
        var file = ui.ws.file;
        if (!file) return;
        ui.ws.as[wsType(file.path)] = b.getAttribute("data-ws-as");
        safely("workspaceFile", renderWorkspaceFile);
      });
    });
    $("ws-actions").addEventListener("click", function (ev) {
      var b = ev.target.closest ? ev.target.closest("[data-ws-download], [data-ws-copy]") : null;
      if (!b) return;
      if (b.hasAttribute("data-ws-copy")) copyWorkspaceText();
      else downloadWorkspaceFile(b.getAttribute("data-ws-download"));
    });
    $("ws-v-body").addEventListener("click", function (ev) {
      var b = ev.target.closest ? ev.target.closest("[data-ws-open], [data-ws-request], [data-ws-reveal], [data-ws-cycle]") : null;
      if (!b) return;
      if (b.hasAttribute("data-ws-request")) revealRequest(b.getAttribute("data-ws-request"));
      else if (b.hasAttribute("data-ws-reveal")) wsReveal(b.getAttribute("data-ws-reveal"));
      else if (b.hasAttribute("data-ws-cycle")) wsRevealCycle(b.getAttribute("data-ws-cycle"));
      else openWorkspaceItem(b.getAttribute("data-ws-open"), ui.ws.nav, null);
    });
  }

  function openWorkspaceItem(key, nav, opener) {
    var ws = ui.ws;
    var item = ws.model ? ws.model.byKey[key] : null;
    if (!item) return;
    ws.nav = arr(nav).indexOf(key) >= 0 ? nav.slice() : ws.order.indexOf(key) >= 0 ? ws.order.slice() : [key];
    if (opener) ws.opener = opener;
    showWorkspaceItem(key);
    var dialog = $("ws-viewer");
    if (!dialog.open) {
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
    }
    $("ws-file-title").focus();
  }

  function showWorkspaceItem(key) {
    var ws = ui.ws;
    var item = ws.model.byKey[key];
    if (!ws.file || ws.file.key !== key) {
      ws.fileSeq++;  // an answer for the item shown before is dropped
      ws.file = { key: key, path: item.primary.path, mode: ws.list.mode, product: item.kind !== "text", text: null, loadedAt: null, modified: null };
      ws.fileBusy = false;
      ws.fileError = null;
      var kept = ws.texts[item.primary.path];
      if (kept && kept.modified === item.primary.modified_at) {
        ws.file.text = kept.text;
        ws.file.loadedAt = kept.loadedAt;
        ws.file.modified = kept.modified;
      }
      $("ws-v-body").scrollTop = 0;
    }
    if (!ws.file.product && ws.file.text === null) loadWorkspaceFile();
    safely("workspaceFile", renderWorkspaceFile);
    wsMarkOpen();
  }

  // The open item, marked in the lists behind the dialog.
  function wsMarkOpen() {
    var open = ui.ws.file ? ui.ws.file.key : null;
    ["ws-list", "ws-recent"].forEach(function (id) {
      Array.prototype.forEach.call($(id).querySelectorAll("[data-ws-open]"), function (b) {
        if (b.getAttribute("data-ws-open") === open) b.setAttribute("aria-current", "true");
        else b.removeAttribute("aria-current");
      });
    });
  }

  function wsStep(delta) {
    var ws = ui.ws;
    if (!ws.file || !ws.model) return;
    var next = ws.nav[ws.nav.indexOf(ws.file.key) + delta];
    if (!next || !ws.model.byKey[next]) return;
    var hadFocus = document.activeElement;
    showWorkspaceItem(next);
    // A step button that is now at the end of the list is disabled: the focus goes to the title then.
    if (!hadFocus || hadFocus.disabled || !$("ws-viewer").contains(hadFocus)) $("ws-file-title").focus();
    else $("ws-announce").textContent = ws.model.byKey[next].name + ", " + $("ws-pos").textContent;  // the focus stays: say what is shown
  }

  function closeWorkspaceDialog() {
    var dialog = $("ws-viewer");
    if (dialog.open && typeof dialog.close === "function") dialog.close();
    else if (dialog.hasAttribute("open")) { dialog.removeAttribute("open"); onWorkspaceDialogClosed(); }
  }

  function onWorkspaceDialogClosed() {
    var ws = ui.ws;
    var key = ws.file ? ws.file.key : null;
    closeWorkspaceFile();
    wsMarkOpen();
    $("ws-announce").textContent = "";
    // Back to the item shown last, in the list it was opened from, or to what opened it (a request's card); after
    // Open in Approvals the request keeps the focus.
    var target = null;
    if (ui.tab === "workspace") {
      ["ws-list", "ws-recent"].forEach(function (id) {
        Array.prototype.forEach.call($(id).querySelectorAll("[data-ws-open]"), function (b) {
          if (!target && b.getAttribute("data-ws-open") === key) target = b;
        });
      });
    }
    var opener = ws.opener;
    ws.opener = null;
    if (ws.keepFocus) { ws.keepFocus = false; return; }
    if (!target && opener && document.body.contains(opener) && !opener.closest("[hidden]")) target = opener;
    if (target) target.focus();
    else if (ui.tab === "workspace") $("ws-files-title").focus();
  }

  function closeWorkspaceFile() {
    var ws = ui.ws;
    ws.file = null;
    ws.fileSeq++;  // an answer still on its way is dropped
    ws.fileBusy = false;
    ws.fileError = null;
  }

  function loadWorkspaceFile() {
    var ws = ui.ws;
    var file = ws.file;
    if (!file || file.product) return;
    var item = wsFindItem(file.key, file.path);
    var modified = item ? item.primary.modified_at : null;
    var seq = ++ws.fileSeq;  // only the latest request counts (the owner may step to another file meanwhile)
    ws.fileBusy = true;
    ws.fileError = null;
    safely("workspaceFile", renderWorkspaceFile);
    request("GET", "api/workspace/file?path=" + encodeURIComponent(file.path), null, { accept: "text/plain" }).then(function (res) {
      if (seq !== ws.fileSeq) return;
      if (!res.ok) throw httpError(res);
      if (typeof res.text !== "string") throw new RequestError("malformed", "no text");
      file.text = res.text;
      file.loadedAt = new Date();
      file.modified = modified;
      ws.texts[file.path] = { text: res.text, loadedAt: file.loadedAt, modified: modified };
      var kept = Object.keys(ws.texts);
      if (kept.length > WS_TEXTS_KEPT) delete ws.texts[kept[0]];
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

  // How a kind of text can be shown besides as it is: Markdown formatted, a table as a table, JSON indented.
  function wsTextViews(ext) {
    if (ext === "md") return "Formatted";
    if (ext === "csv" || ext === "tsv") return "Table";
    if (ext === "json") return "Indented";
    return null;
  }

  function renderWorkspaceFile() {
    var ws = ui.ws;
    var file = ws.file;
    if (!file) return;
    var item = wsFindItem(file.key, file.path);
    var ext = wsType(file.path);
    var has = typeof file.text === "string";
    $("ws-file-title").textContent = item ? item.name : baseName(file.path);
    var meta = [];
    if (item) {
      meta.push("In " + wsFolderName(item.folder), wsFormatText(item), byteSize(item.size), "changed " + fmtDateTime(item.modified));
    }
    if (has && !file.product) meta.push(ws.fileBusy ? "reloading…" : "loaded at " + timeFmt.format(file.loadedAt));
    setStatusText("ws-file-meta", meta.join(" · "), "");
    $("ws-v-body").setAttribute("aria-label", item ? item.name : baseName(file.path));
    var icon = $("ws-file-icon");
    var iconKey = item ? item.key + "|" + (item.thumb ? item.thumb.modified_at : "") : "";
    if (icon.getAttribute("data-key") !== iconKey) {
      icon.setAttribute("data-key", iconKey);
      replace(icon, item ? wsThumb(item, "ws-v-thumb") : []);
    }
    var at = ws.nav.indexOf(file.key);
    var prev = at > 0 ? ws.model.byKey[ws.nav[at - 1]] : null;
    var next = at >= 0 ? ws.model.byKey[ws.nav[at + 1]] : null;
    $("ws-prev").disabled = !prev;
    $("ws-next").disabled = !next;
    $("ws-prev").title = prev ? "Previous: " + prev.name + " (←)" : "";
    $("ws-next").title = next ? "Next: " + next.name + " (→)" : "";
    $("ws-prev").lastChild.textContent = prev ? "Previous file: " + prev.name : "Previous file";
    $("ws-next").firstChild.textContent = next ? "Next file: " + next.name : "Next file";
    setStatusText("ws-pos", at >= 0 && ws.nav.length > 1 ? intFmt.format(at + 1) + " of " + intFmt.format(ws.nav.length) : "", "");
    $("ws-file-note").textContent = file.product
      ? "Made by Ember's code from the agent's text. Check it before you use it or sell it."
      : "Written by the agent. Check it before you use it.";
    renderWsFileRequests(item);
    renderWsFileOwner(item);
    renderWsActions(item, file, has);
    var views = file.product ? null : wsTextViews(ext);
    var as = views && (ws.as[ext] || "formatted") === "formatted" ? "formatted" : "plain";
    $("ws-as").hidden = !views || !has || !file.text;
    if (views) {
      $("ws-as-formatted").textContent = views;
      Array.prototype.forEach.call(document.querySelectorAll("[data-ws-as]"), function (b) {
        b.setAttribute("aria-pressed", String(b.getAttribute("data-ws-as") === as));
      });
    }
    renderWorkspaceProduct(file, item);
    renderWsParts(item, file);
    if (file.product) {
      $("ws-doc").hidden = true;
      $("ws-text").hidden = true;
      setStatusText("ws-file-status", item ? "" : "This file is no longer in the workspace.", item ? "" : "error");
      return;
    }
    var status = "";
    if (!item) status = "This file is no longer in the workspace." + (has ? " The text below is the one loaded earlier." : "");
    else if (ws.fileError && !ws.fileBusy) status = "Couldn't open the file (" + errorText(ws.fileError) + ")." + (has ? " The text below is the one loaded earlier." : "");
    else if (!has && ws.fileBusy) status = "Loading the file…";
    else if (has && !file.text) status = "The file is empty.";
    setStatusText("ws-file-status", status, (!item || ws.fileError) && !ws.fileBusy ? "error" : "");
    var formatted = has && file.text && as === "formatted" ? wsFormatted(ext, file.text) : null;
    var doc = $("ws-doc");
    var pre = $("ws-text");
    doc.hidden = !formatted;
    pre.hidden = !has || !file.text || !!formatted;
    if (formatted) {
      var key = file.path + "|" + file.modified + "|" + file.text.length;
      if (doc.getAttribute("data-key") !== key) {  // unchanged, it keeps any selection
        doc.setAttribute("data-key", key);
        doc.className = "ws-doc ws-doc-" + ext;
        replace(doc, formatted);
      }
      return;
    }
    if (!has) return;
    pre.className = "ws-text" + (WS_MONO.test(file.path) ? " mono" : "");
    // Unchanged text keeps any selection.
    if (pre.getAttribute("data-path") !== file.path || pre.textContent !== file.text) {
      pre.textContent = file.text;
      pre.setAttribute("data-path", file.path);
    }
  }

  // Each request that names one of the item's files, with a way to it in Approvals.
  function renderWsFileRequests(item) {
    var el = $("ws-file-requests");
    var requests = item ? item.requests : [];
    el.hidden = !requests.length;
    var key = JSON.stringify(requests.map(function (a) { return [a.id, a.status, a.title]; }));
    if (el.getAttribute("data-key") === key) return;
    el.setAttribute("data-key", key);
    replace(el, requests.map(function (a) {
      var s = APPROVAL_STATUS[a.status] || {};
      return h("li", { class: "ws-v-request", "data-tone": s.tone || null },
        chip(APPROVAL_STATUS, a.status, sentence(String(a.status).replace(/_/g, " "))),
        h("span", { class: "ws-v-request-title" }, h("strong", { text: "#" + a.id + " " }), asText(a.title)),
        h("button", { type: "button", class: "btn btn-small", "data-ws-request": String(a.id) }, "Open in Approvals ", h("span", { "aria-hidden": "true", text: "→" })));
    }));
  }

  // The project or venture it was written for (a project's venture too), each a way to its card, and the
  // cycle that wrote it last (a way to it in Activity while Activity lists it).
  function renderWsFileOwner(item) {
    var el = $("ws-file-owner");
    el.hidden = !item;
    if (!item) return;
    var owners = ui.ws.model.owners;
    var o = wsOwner(owners, item.owner);
    var venture = o.venture ? wsOwner(owners, "v:" + o.venture) : null;
    var listed = item.cycle && ui.cycles[item.cycle];
    var key = JSON.stringify([item.key, o.key, o.title, o.state, venture && [venture.title, venture.state], item.cycle, item.tool, !!listed]);
    if (el.getAttribute("data-key") === key) return;
    var focused = el.contains(document.activeElement) ? document.activeElement.getAttribute("data-ws-reveal") || "cycle" : null;
    el.setAttribute("data-key", key);
    function to(owner) {
      if (!owner.known) return h("span", { class: "chip", text: owner.title });
      return h("button", { type: "button", class: "chip chip-button", "data-tone": owner.tone || null, "data-ws-reveal": owner.key,
        "aria-label": (owner.type === "venture" ? "Venture: " : "Project: ") + owner.title + (owner.state ? " (" + owner.state.toLowerCase() + ")" : "") +
          ". Open it in Ventures" },
        h("span", { "aria-hidden": "true", text: owner.icon }), h("span", { class: "chip-text", text: owner.title }),
        owner.state ? h("span", { class: "ws-chip-state", "aria-hidden": "true", text: owner.state }) : null,
        h("span", { class: "chip-arrow", "aria-hidden": "true", text: "→" }));
    }
    var parts = o.type === "none" ? [h("span", { class: "ws-v-owner-none", text: "Not filed under a project or venture: written with neither in focus." })]
      : [h("span", { class: "ws-v-owner-label", text: o.type === "venture" ? "For venture" : "For project" }), to(o),
        venture ? [h("span", { class: "ws-v-owner-label", text: "of venture" }), to(venture)] : null];
    if (item.cycle) {
      parts.push(h("span", { class: "ws-v-owner-cycle" }, "Last written by ",
        listed ? h("button", { type: "button", class: "link-button", "data-ws-cycle": item.cycle, "aria-label": "Cycle #" + item.cycle + ": open it in Activity" }, "cycle #" + item.cycle)
          : "cycle #" + item.cycle,
        item.tool ? [" with ", h("code", { text: item.tool })] : null));
    }
    replace(el, parts);
    if (focused) {
      Array.prototype.forEach.call(el.querySelectorAll("button"), function (b) {
        if ((b.getAttribute("data-ws-reveal") || "cycle") === focused) b.focus();
      });
    }
  }

  function renderWsActions(item, file, has) {
    var buttons = [];
    if (file.product) {
      if (item) {
        item.files.forEach(function (f, i) {
          var part = item.pictures.some(function (p) { return p.file === f; }) && item.kind !== "picture";
          if (part) return;
          var type = WS_FILES[wsType(f.path)];
          buttons.push(h("button", { type: "button", class: "btn" + (i === 0 ? " btn-primary" : ""), "data-ws-download": f.path },
            "Download " + (type ? type.label : "file"), h("span", { class: "muted ws-btn-size", text: " " + byteSize(num(f.size) || 0) })));
        });
      }
    } else {
      buttons.push(h("button", { type: "button", class: "btn btn-primary", "data-ws-download": file.path, disabled: !has }, "Download"));
      buttons.push(h("button", { type: "button", class: "btn", "data-ws-copy": "true", disabled: !has || !file.text }, "Copy text"));
    }
    var el = $("ws-actions");
    var key = JSON.stringify([file.path, item ? item.files.map(function (f) { return [f.path, f.size]; }) : null, has, !!(has && file.text)]);
    if (el.getAttribute("data-key") === key) return;
    var focused = el.contains(document.activeElement) ? document.activeElement.getAttribute("data-ws-download") || "copy" : null;
    el.setAttribute("data-key", key);
    replace(el, buttons);
    if (focused) {
      Array.prototype.forEach.call(el.querySelectorAll("button"), function (b) {
        if ((b.getAttribute("data-ws-download") || "copy") === focused) b.focus();
      });
    }
  }

  // A product's pictures: pages side by side (as many as fit), a single picture at full width. Each opens at full
  // size in a new tab (the server shows pictures inline, nothing else).
  function renderWorkspaceProduct(file, item) {
    var el = $("ws-product");
    var pictures = file.product && item ? item.pictures : [];
    var none = file.product && item && !pictures.length;
    el.hidden = !pictures.length && !none;
    var key = JSON.stringify([file.key, pictures.map(function (p) { return [p.file.path, p.file.modified_at, p.file.size]; }), none]);
    if (el.getAttribute("data-key") === key) return;
    el.setAttribute("data-key", key);
    el.className = "ws-product" + (pictures.length === 1 ? " single" : "");
    if (none) {
      replace(el, h("p", { class: "muted ws-no-pictures", text: item.ext === "pptx" ? "Ember's code makes no pictures of a presentation: download it to see it."
        : "There are no pictures of its pages (it was made without them): download it to see it." }));
      return;
    }
    replace(el, pictures.map(function (p, index) {
      var label = p.label ? p.label + " of " + item.name : item.name;
      var size = h("span", { class: "ws-figure-size" });
      var img = h("img", { class: "ws-image", src: wsPictureUrl(p.file), alt: label, loading: index < 4 ? "eager" : "lazy", decoding: "async" });
      img.addEventListener("load", function () {
        if (img.naturalWidth) size.textContent = intFmt.format(img.naturalWidth) + " × " + intFmt.format(img.naturalHeight) + " px";
      });
      return h("figure", { class: "ws-figure" },
        h("a", { class: "ws-figure-link", href: wsPictureUrl(p.file), target: "_blank", rel: "noopener" }, img,
          h("span", { class: "visually-hidden", text: " (opens at full size in a new tab)" })),
        h("figcaption", { class: "ws-figure-caption" }, h("span", { text: p.label || "Opens at full size in a new tab" }), size));
    }));
  }

  // The item's files (a product's formats and pictures), the text it was made from, and what was made from a text.
  function renderWsParts(item, file) {
    var section = $("ws-file-parts");
    var rows = [];
    if (item && item.files.length > 1) {
      item.files.forEach(function (f) {
        var picture = item.pictures.filter(function (p) { return p.file === f; })[0];
        var type = WS_FILES[wsType(f.path)];
        var role = picture ? wsPictureRole(picture.label) : f === item.primary ? (type ? type.label : "File")
          : type && type.label === "Word" ? "Word copy (editable)" : type ? type.label : "File";
        rows.push(h("li", { class: "ws-part" },
          h("span", { class: "ws-part-main" }, h("span", { class: "ws-part-name", text: baseName(f.path) }), h("span", { class: "ws-part-role", text: role })),
          h("span", { class: "ws-part-size", text: byteSize(num(f.size) || 0) }),
          h("a", { class: "btn btn-small", href: wsProductUrl(f.path, false), download: baseName(f.path), "aria-label": "Download " + baseName(f.path) }, "Download")));
      });
    }
    var links = [];
    if (item) {
      item.related.forEach(function (path) {
        if (ui.ws.model.byKey[path]) links.push([path, "The text it was made from, by its name"]);
      });
      item.made.forEach(function (key) {
        var made = ui.ws.model.byKey[key];
        if (made) links.push([key, "Made from this text: " + wsFormatText(made)]);
      });
    }
    links.forEach(function (l) {
      var other = ui.ws.model.byKey[l[0]];
      rows.push(h("li", { class: "ws-part" },
        h("span", { class: "ws-part-main" }, h("span", { class: "ws-part-name", text: other.name }), h("span", { class: "ws-part-role", text: l[1] })),
        h("span", { class: "ws-part-size", text: byteSize(other.size) }),
        h("button", { type: "button", class: "btn btn-small", "data-ws-open": other.key }, "Open")));
    });
    section.hidden = !rows.length;
    $("ws-file-parts-head").textContent = item && item.files.length > 1 ? "Its files" : "Related";
    var key = JSON.stringify([file.key, item ? item.files.map(function (f) { return [f.path, f.size]; }) : null, links]);
    if (section.getAttribute("data-key") === key) return;
    section.setAttribute("data-key", key);
    replace($("ws-parts"), rows);
  }

  function wsPictureRole(label) {
    if (/^(Page|Sheet) /.test(label || "")) return "Picture of " + lowerFirst(label);
    return (label || "Picture") === "Picture" ? "Picture" : label + " picture";
  }

  // Text is saved from the text already loaded (like the diagnostics report), never by opening the file's URL;
  // a product is downloaded from the server, which always sends it as an attachment.
  function downloadWorkspaceFile(path) {
    var file = ui.ws.file;
    if (!file) return;
    if (file.product) {
      var link = h("a", { href: wsProductUrl(path, false), download: baseName(path), hidden: true });
      $("ws-viewer").appendChild(link);  // the page behind the dialog is inert
      link.click();
      link.remove();
      setStatusText("ws-file-status", "Downloading " + baseName(path) + " (check your downloads).", "ok");
      return;
    }
    if (typeof file.text !== "string") return;
    var name = baseName(file.path);
    var url = window.URL.createObjectURL(new Blob([file.text], { type: "text/plain;charset=utf-8" }));
    var save = h("a", { href: url, download: name, hidden: true });
    $("ws-viewer").appendChild(save);
    save.click();
    save.remove();
    window.setTimeout(function () { window.URL.revokeObjectURL(url); }, 60000);
    setStatusText("ws-file-status", "Saved as " + name + " (check your downloads).", "ok");
  }

  // The text as it is, whichever way it is shown: without the clipboard API (Home Assistant over plain http), from
  // a copy of it selected out of sight.
  function copyWorkspaceText() {
    var file = ui.ws.file;
    if (!file || typeof file.text !== "string") return;
    var text = file.text;
    var holder = h("pre", { class: "ws-copy-holder", text: text });
    $("ws-viewer").appendChild(holder);
    var name = baseName(file.path);
    copyText(text, holder, function () {
      holder.remove();
      setStatusText("ws-file-status", "Copied the text of " + name + " (" + byteSize(new Blob([text]).size) + ").", "ok");
    }, function () {
      holder.remove();
      setStatusText("ws-file-status", "This browser didn't allow copying. Choose Plain text, select the text and copy it with Ctrl+C (Cmd+C on a Mac).", "error");
    });
  }

  // ---- A text formatted: Markdown, a table, JSON indented (null: shown as it is)

  function wsFormatted(ext, text) {
    if (ext === "md") return wsMarkdown(text, 0);
    if (ext === "csv" || ext === "tsv") return wsTable(wsSeparated(text, ext === "tsv" ? "\t" : ","));
    if (ext === "json") {
      try { return [h("pre", { class: "ws-text mono", text: JSON.stringify(JSON.parse(text), null, 2) })]; } catch (e) { return null; }
    }
    return null;
  }

  // Comma- or tab-separated values, with quoted fields ("a, b" and "say ""hi"""), as rows of cells.
  function wsSeparated(text, separator) {
    var rows = [];
    var row = [];
    var field = "";
    var quoted = false;
    for (var i = 0; i < text.length && rows.length <= WS_TABLE_ROWS + 1; i++) {
      var c = text.charAt(i);
      if (quoted) {
        if (c !== "\"") field += c;
        else if (text.charAt(i + 1) === "\"") { field += "\""; i++; }
        else quoted = false;
      } else if (c === "\"" && field === "") quoted = true;
      else if (c === separator) { row.push(field); field = ""; }
      else if (c === "\n" || c === "\r") {
        if (c === "\r" && text.charAt(i + 1) === "\n") i++;
        row.push(field);
        rows.push(row);
        row = [];
        field = "";
      } else field += c;
    }
    if (field || row.length) { row.push(field); rows.push(row); }
    return rows.filter(function (r) { return r.length > 1 || r[0] !== ""; });
  }

  function wsTable(rows) {
    if (rows.length < 2) return null;
    var head = rows[0].slice(0, WS_TABLE_COLUMNS);
    var body = rows.slice(1, WS_TABLE_ROWS + 1);
    var numeric = head.map(function (_, k) {
      var cells = body.map(function (r) { return (r[k] || "").trim(); }).filter(Boolean);
      return cells.length > 0 && cells.every(function (c) { return /^[-+]?[$€£]?\s?\d[\d\s.,']*%?\s?[$€£]?$/.test(c); });
    });
    var out = [h("div", { class: "table-wrap ws-table-wrap" }, h("table", { class: "md-table ws-csv" },
      h("thead", null, h("tr", null, head.map(function (c, k) { return h("th", { scope: "col", class: numeric[k] ? "md-right" : null, text: c }); }))),
      h("tbody", null, body.map(function (r) {
        return h("tr", null, head.map(function (_, k) { return h("td", { class: numeric[k] ? "md-right" : null, text: r[k] || "" }); }));
      }))))];
    if (rows.length > WS_TABLE_ROWS + 1 || rows[0].length > WS_TABLE_COLUMNS) {
      out.push(h("p", { class: "muted small", text: "The table shows the first " + intFmt.format(Math.min(body.length, WS_TABLE_ROWS)) +
        " rows and " + intFmt.format(head.length) + " columns: Plain text shows the whole file." }));
    }
    return out;
  }

  // Markdown drawn from its text, element by element (never parsed as HTML): headings, paragraphs, lists and
  // checklists, quotes, code, tables and rules, with **bold**, *italic*, `code` and links (their words, then their
  // address as text). Ember's settings block and layout lines (::: sidebar) stay visible as they are.
  // Each read in linear time, whatever the line (a pattern that splits a run of spaces two ways takes seconds on one).
  var MD_FENCE = /^\s{0,3}(`{3,}|~{3,})/;
  var MD_HEADING = /^ {0,3}(#{1,6})(?:[ \t]+(.*))?$/;
  var MD_RULE = /^\s{0,3}(?:(?:-\s*){3,}|(?:\*\s*){3,}|(?:_\s*){3,})$/;
  var MD_ITEM = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
  var MD_TASK = /^\[([ xX])\]\s+(.*)$/;
  var MD_QUOTE = /^\s{0,3}>/;
  // [words](address) | ***both*** | **bold** | *italic*, and the same with underscores. Emphasis closes within 500
  // characters, at its first closing mark: a line full of unclosed marks is read in linear time, not quadratic.
  var MD_INLINE = /\[([^\]\n]{1,300})\]\(\s*([^()\s]{1,800})(?:\s+"[^"\n]*")?\s*\)|(\*\*\*|___)(\S(?:.{0,500}?\S)??)\3|(\*\*|__)(\S(?:.{0,500}?\S)??)\5|(\*|_)(\S(?:.{0,500}?\S)??)\7/g;
  // Code spans and escaped marks, held aside before emphasis is read: what they hold is never emphasis (`a_b`, \*).
  var MD_HELD = /\\([\\`*_{}[\]()#+\-.!|~<>])|`([^`\n]+)`/g;
  var MD_SLOT = "\uE000";  // where a held part goes back, in order

  function wsMarkdown(text, depth) {
    var lines = text.replace(/\r\n?/g, "\n").split("\n");
    var out = [];
    var i = 0;
    if (!depth && /^---\s*$/.test(lines[0])) {
      var end = 1;
      while (end < lines.length && end <= 80 && !/^---\s*$/.test(lines[end])) end++;
      if (end < lines.length && end <= 80) {
        out.push(h("div", { class: "md-settings" }, h("p", { class: "md-settings-head", text: "Settings" }),
          h("pre", { class: "mono", text: lines.slice(1, end).join("\n") })));
        i = end + 1;
      }
    }
    while (i < lines.length) {
      var line = lines[i];
      var m;
      if (!line.trim()) { i++; continue; }
      if ((m = MD_FENCE.exec(line))) {
        var fence = m[1];
        var code = [];
        i++;
        while (i < lines.length && lines[i].trim().indexOf(fence) !== 0) { code.push(lines[i]); i++; }
        i++;  // the closing fence
        out.push(h("pre", { class: "md-code", text: code.join("\n") }));
      } else if (/^:::/.test(line.trim())) {
        out.push(h("p", { class: "md-layout", text: line.trim() }));
        i++;
      } else if ((m = MD_HEADING.exec(line))) {
        out.push(h("h" + Math.min(6, m[1].length + 2), { class: "md-h md-h" + m[1].length }, mdInline(mdHeading(m[2] || ""), 0)));
        i++;
      } else if (MD_RULE.test(line)) {
        out.push(h("hr", { class: "md-rule" }));
        i++;
      } else if (MD_QUOTE.test(line)) {
        var quote = [];
        while (i < lines.length && MD_QUOTE.test(lines[i])) { quote.push(lines[i].replace(/^\s{0,3}>\s?/, "")); i++; }
        out.push(h("blockquote", { class: "md-quote" }, depth < 3 ? wsMarkdown(quote.join("\n"), depth + 1) : h("p", { class: "pre-line", text: quote.join("\n") })));
      } else if (mdTableStart(lines, i)) {
        var table = mdTable(lines, i);
        out.push(table.el);
        i = table.next;
      } else if (MD_ITEM.test(line)) {
        var list = mdList(lines, i);
        out.push(list.el);
        i = list.next;
      } else {
        var para = [line];
        i++;
        while (i < lines.length && lines[i].trim() && !mdBlockStart(lines, i)) { para.push(lines[i]); i++; }
        out.push(h("p", { class: "md-p" }, mdLines(para)));
      }
    }
    return out;
  }

  // A heading's words, without the #s that may close it ("## Notes ##").
  function mdHeading(text) {
    var t = text.trim();
    var end = t.length;
    while (end > 0 && t.charAt(end - 1) === "#") end--;
    return end < t.length && (end === 0 || /\s/.test(t.charAt(end - 1))) ? t.slice(0, end).trim() : t;
  }

  // A table: a row with a | over a line of its columns' dashes (|---|:--:|).
  function mdTableStart(lines, i) {
    var rule = i + 1 < lines.length ? lines[i + 1] : "";
    if (lines[i].indexOf("|") < 0 || rule.indexOf("-") < 0) return false;
    return mdCells(rule).every(function (c) { return /^:?-+:?$/.test(c); });
  }

  function mdBlockStart(lines, i) {
    var line = lines[i];
    return MD_FENCE.test(line) || /^:::/.test(line.trim()) || MD_HEADING.test(line) || MD_RULE.test(line) || MD_QUOTE.test(line) ||
      MD_ITEM.test(line) || mdTableStart(lines, i);
  }

  // Lines of a paragraph: joined by spaces, or broken where a line ends in two spaces or a backslash.
  function mdLines(lines) {
    var out = [];
    var hard = false;
    lines.forEach(function (line, k) {
      if (k) out.push(hard ? h("br") : " ");
      var spaces = 0;
      while (spaces < 2 && line.charAt(line.length - 1 - spaces) === " ") spaces++;
      hard = spaces === 2 || line.charAt(line.length - 1) === "\\";
      out.push(mdInline(line.trim().replace(/\\$/, ""), 0));
    });
    return out;
  }

  function mdInline(text, depth, held) {
    if (!held) {
      held = { parts: [], next: 0 };
      text = text.split(MD_SLOT).join("\uFFFD").replace(MD_HELD, function (all, escaped, code) {
        held.parts.push(escaped !== undefined ? escaped : h("code", { class: "md-code-inline", text: code }));
        return MD_SLOT;
      });
    }
    if (depth > 3) return mdHeld(text, held);
    var out = [];
    var pos = 0;
    var m;
    var re = new RegExp(MD_INLINE.source, "g");  // its own: the parts inside call this again
    while ((m = re.exec(text))) {
      var marker = m[3] || m[5] || m[7] || "";
      // Underscores inside a word (snake_case) stay as they are.
      if (marker.charAt(0) === "_" && (/[A-Za-z0-9]/.test(text.charAt(m.index - 1)) || /[A-Za-z0-9]/.test(text.charAt(re.lastIndex)))) {
        re.lastIndex = m.index + 1;
        continue;
      }
      if (m.index > pos) out.push(mdHeld(text.slice(pos, m.index), held));
      if (m[1] !== undefined) {
        var words = mdInline(m[1], depth + 1, held);
        var address = mdHeld(m[2], held);
        out.push(h("span", { class: "md-link" }, words, m[2] !== m[1] ? h("span", { class: "md-url" }, " (", address, ")") : null));
      } else if (m[3]) out.push(h("strong", null, h("em", null, mdInline(m[4], depth + 1, held))));
      else if (m[5]) out.push(h("strong", null, mdInline(m[6], depth + 1, held)));
      else out.push(h("em", null, mdInline(m[8], depth + 1, held)));
      pos = re.lastIndex;
    }
    if (pos < text.length) out.push(mdHeld(text.slice(pos), held));
    return out;
  }

  // Text with the held parts back in their places, in the order they were held.
  function mdHeld(text, held) {
    var pieces = text.split(MD_SLOT);
    var out = [pieces[0]];
    for (var k = 1; k < pieces.length; k++) out.push(held.parts[held.next++], pieces[k]);
    return out;
  }

  // A list's lines: its items (an item indented below another is in a list inside it), lines that go on an item,
  // and blank lines between items.
  function mdList(lines, i) {
    var entries = [];
    var first = MD_ITEM.exec(lines[i]);
    var level = first[1].replace(/\t/g, "    ").length;
    var ordered = /\d/.test(first[2]);
    function another(m) { return m[1].replace(/\t/g, "    ").length <= level && /\d/.test(m[2]) !== ordered; }
    while (i < lines.length) {
      var line = lines[i];
      var m = MD_ITEM.exec(line);
      if (m) {
        if (entries.length && another(m)) break;  // another kind of list starts at its level
        entries.push({ indent: m[1].replace(/\t/g, "    ").length, ordered: /\d/.test(m[2]), start: parseInt(m[2], 10), text: m[3] });
        i++;
      } else if (!line.trim()) {
        var j = i;
        while (j < lines.length && !lines[j].trim()) j++;
        var next = j < lines.length ? MD_ITEM.exec(lines[j]) : null;
        if (next && !another(next)) i = j;
        else break;
      } else if (!mdBlockStart(lines, i) && lines[i - 1].trim()) {
        entries[entries.length - 1].text += "\n" + line.trim();
        i++;
      } else break;
    }
    var root = null;
    var stack = [];
    entries.forEach(function (e) {
      while (stack.length > 1 && e.indent < stack[stack.length - 1].indent) stack.pop();
      var top = stack[stack.length - 1];
      if (!top || (e.indent > top.indent && top.last && stack.length < 6)) {
        var el = e.ordered ? h("ol", { class: "md-list", start: e.start > 1 ? String(e.start) : null }) : h("ul", { class: "md-list" });
        if (top) top.last.appendChild(el);
        else root = el;
        stack.push(top = { indent: e.indent, el: el, last: null });
      }
      var task = MD_TASK.exec(e.text);
      var done = task && task[1] !== " ";
      var li = task ? h("li", { class: "md-task", "data-done": String(done) },
        h("span", { class: "md-box", "aria-hidden": "true", text: done ? "☑" : "☐" }), h("span", { class: "visually-hidden", text: done ? "Done: " : "To do: " }),
        mdLines(task[2].split("\n"))) : h("li", null, mdLines(e.text.split("\n")));
      top.el.appendChild(li);
      top.last = li;
    });
    return { el: root, next: i };
  }

  function mdCells(line) {
    var s = line.trim();
    if (s.charAt(0) === "|") s = s.slice(1);
    if (s.charAt(s.length - 1) === "|" && s.charAt(s.length - 2) !== "\\") s = s.slice(0, -1);
    var cells = [];
    var cell = "";
    for (var k = 0; k < s.length; k++) {
      var c = s.charAt(k);
      if (c === "\\" && s.charAt(k + 1) === "|") { cell += "|"; k++; }
      else if (c === "|") { cells.push(cell.trim()); cell = ""; }
      else cell += c;
    }
    cells.push(cell.trim());
    return cells;
  }

  function mdTable(lines, i) {
    var head = mdCells(lines[i]).slice(0, WS_TABLE_COLUMNS);
    var align = mdCells(lines[i + 1]).map(function (c) { return /^:-+:$/.test(c) ? "md-center" : /-:$/.test(c) ? "md-right" : null; });
    var rows = [];
    i += 2;
    while (i < lines.length && lines[i].trim() && lines[i].indexOf("|") >= 0) { rows.push(mdCells(lines[i])); i++; }
    var el = h("div", { class: "table-wrap md-table-wrap" }, h("table", { class: "md-table" },
      h("thead", null, h("tr", null, head.map(function (c, k) { return h("th", { scope: "col", class: align[k] || null }, mdInline(c, 0)); }))),
      h("tbody", null, rows.map(function (r) {
        return h("tr", null, head.map(function (_, k) { return h("td", { class: align[k] || null }, mdInline(r[k] || "", 0)); }));
      }))));
    return { el: el, next: i };
  }

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

  // 0.27.0: the ventures being decided (and the parked and killed ones) in Pipeline; the backed and live ones, with
  // their projects, in Running.
  var VENTURE_GROUPS = [
    { key: "proposed", title: "Business cases for your decision", match: function (s) { return s === "proposed"; } },
    { key: "researching", title: "Being researched", match: function (s) { return s === "researching"; } },
    { key: "idea", title: "Ideas, the heaviest first", match: function (s) { return s === "idea"; } },
    { key: "closed", title: "Parked and killed", match: function () { return true; } },
  ];
  var RUNNING_GROUPS = [
    { key: "building", title: "Building (you backed them)", match: function (s) { return s === "building"; } },
    { key: "live", title: "Live legs", match: function () { return true; } },
  ];
  var RUNNING_STAGES = { building: true, live: true };

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
      if (vt.reveal !== null && !vt.again) {  // a project's venture chip asked for this one
        var reveal = vt.reveal;
        vt.reveal = null;
        if (vt.byId[String(reveal)]) showVentureCard(reveal);
      }
      if (!vt.again) showProject();  // 0.27.0: the workspace asked for a project (revealProject)
      if (vt.again) { vt.again = false; loadVentures(); }
    });
  }

  function renderVentures() {
    var vt = ui.vt;
    var data = vt.data;
    var name = agentName();
    renderRunningStatus();
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
    renderVentureDesk(data);
    renderVentureLegend(data);
    renderVentureTree(items);
    fillParentSelect(items);
    var rows = items.map(function (v) { return Object.assign({}, v, { status: v.stage }); }).sort(ventureOrder);
    var views = [
      { view: "pipeline", root: $("ventures"), groups: VENTURE_GROUPS, rows: rows.filter(function (v) { return !RUNNING_STAGES[v.stage]; }),
        empty: emptyState("div", "No ventures yet.", name + " plants the first ideas when it starts; add your own with Add idea.") },
      { view: "running", root: $("vt-running-list"), groups: RUNNING_GROUPS, rows: rows.filter(function (v) { return RUNNING_STAGES[v.stage]; }), empty: [] },
    ];
    // A venture whose new stage puts it in the other view takes its card along: its status line says what the owner's
    // decision did, and while it has their focus the view follows it.
    var follow = null;
    views.forEach(function (to) {
      var from = views[1 - views.indexOf(to)];
      var q = queueRoot(to.root, to.groups, to.empty);
      var other = queueRoot(from.root, from.groups, from.empty);
      to.rows.forEach(function (v) {
        var it = other.items[String(v.id)];
        if (!it) return;
        if (it.card.contains(document.activeElement)) follow = { it: it, view: to.view, focus: document.activeElement };
        delete other.items[String(v.id)];
        q.items[String(v.id)] = it;
        if (it.projects) { it.projects.parentNode.removeChild(it.projects); it.projects = null; }
      });
    });
    var complete = true;
    views.forEach(function (v) {
      complete = renderQueue(v.root, {
        kind: "venture", rows: v.rows, groups: v.groups, empty: v.empty,
        view: ventureView,
        viewKey: data.criteria,
        actionKey: function (x) { return String(x.stage) + "|" + String(x.owner_version); },
        actions: ventureActions,
        panel: venturePanel,
      }) && complete;
    });
    // Each running venture's card holds its projects: renderProjects fills the box.
    var running = $("vt-running-list").ember;
    Object.keys(running.items).forEach(function (id) {
      var it = running.items[id];
      if (!it.projects) append(it.card, it.projects = h("div", { class: "vt-projects" }));
    });
    $("vt-running-list").hidden = !views[1].rows.length;
    renderProjectsNow();
    if (follow) {
      selectVentureView(follow.view, false);
      revealEl(follow.it.card, follow.it.card.contains(follow.focus) ? follow.focus
        : follow.it.status.textContent ? follow.it.status : follow.it.card.querySelector(".vt-card-title"));
    }
    return complete;
  }

  // 0.27.0: the Running view says when its ventures are missing (the projects are listed without them then).
  function renderRunningStatus() {
    var vt = ui.vt;
    setStatusText("rn-status", vt.busy && !vt.data ? "Loading the ventures…"
      : vt.error && !vt.busy ? "Couldn't load the ventures (" + errorText(vt.error) + ")." +
        (vt.data ? " What you see is from earlier." : " The projects are listed without them; Refresh in Pipeline tries again.") : "",
      vt.error && !vt.busy ? "error" : "");
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

  // 0.13.0: the decision desk: what Ember's code ranks as the ventures' next decisions (a venture cycle's plan takes one
  // or says why none), what the last venture plans took, and how many ventures were decided this week.
  function renderVentureDesk(data) {
    var name = agentName();
    var d = isObject(data.desk) ? data.desk : null;
    if (!d) { replace($("vt-desk"), []); return; }
    var ready = arr(d.ready);
    var picks = arr(d.picks);
    var week = num(d.decided_week) || 0;
    var list = ready.length ?
      h("ol", { class: "vt-ready" }, ready.map(function (item) {
        return h("li", null, h("strong", { text: String(item.key) }), " · " + String(item.text));
      })) :
      h("p", { class: "muted small", text: "Nothing to decide now" + (d.mode === "explore" ? "." : " in the " + d.mode + " burn mode: only backed ventures get venture cycles.") });
    var taken = picks.length ?
      h("ul", { class: "vt-picks muted small" }, picks.map(function (p) {
        var what = p.pick ? "took " + p.pick : "took none: " + String(p.why_not);
        return h("li", null, "Cycle #" + p.cycle_id + " " + what + " (of " + plural(num(p.shown) || 0, "item") + ") · ", timeEl(p.created_at));
      })) : null;
    replace($("vt-desk"), h("details", { class: "vt-desk-box" },
      h("summary", null, h("strong", { text: "Decision desk: " }),
        plural(ready.length, "decision") + " ready · " + plural(week, "venture") + " decided in the last 7 days (the aim: 2)"),
      h("p", { class: "muted small", text: "Ranked by " + name + "'s code: backed ventures without a project, your wishes, " +
        "deadlines, the critic's flags, then the expected net (the critic's where it is lower). Each venture cycle's plan takes one or says why none." }),
      d.forecasts ? h("p", { class: "muted small", text: name + "'s forecasts, settled by Ember's code: " + String(d.forecasts) + "." }) : null,
      list,
      taken));
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

  // A node of the tree was chosen: mark it, and bring its card into view. 0.27.0: a backed or live venture's card is
  // in the Running view, the others' in Pipeline.
  function showVentureCard(id) {
    ui.vt.selected = id;
    Array.prototype.forEach.call($("vt-tree").querySelectorAll(".vt-node"), function (g) {
      if (g.getAttribute("data-id") === String(id)) g.setAttribute("data-selected", "true");
      else g.removeAttribute("data-selected");
    });
    var card = $("panel-ventures").querySelector('article.venture[data-id="' + String(id) + '"]');
    if (!card) return;
    Array.prototype.forEach.call($("panel-ventures").querySelectorAll("article.venture[data-selected]"), function (c) { c.removeAttribute("data-selected"); });
    selectVentureView($("vt-running").contains(card) ? "running" : "pipeline", false);
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
      ventureNumbers(v),
      ventureCritique(v),
      isObject(v.first_sale) ? h("dl", { class: "item-grid" }, predictionRow(v.first_sale, "First sale, as its case said")) : null,
      ventureKnockouts(v),
      // 0.15.0: why Ember's code wouldn't back it; your Back confirms it anyway
      v.backing_problem ? h("p", { class: "muted small", text: "Ember's code wouldn't back it: " + String(v.backing_problem) + "." }) : null,
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
        // 0.27.0: a running venture's projects are in its card, as cards
        projects.length && !RUNNING_STAGES[v.stage] ? h("div", null, h("dt", { text: "Projects" }), h("dd", { text: projects.map(function (p) {
          return "#" + p.id + " " + p.title + " (" + p.status + ")";
        }).join(", ") })) : null),
      ventureWord(v),
      v.notes ? h("details", { class: "notes" }, h("summary", { text: "Notes" }), h("pre", { class: "notes-text", text: asText(v.notes) })) : null,
      lastDigest(v),
      ventureKnowledge(v),
      num(v.files) > 0 ? h("p", { class: "vt-files" }, filesButton("v:" + v.id, "this venture and its projects"),
        h("span", { class: "muted small", text: " Every file " + agentName() + " wrote for it or its projects, in Workspace." })) : null,
      h("p", { class: "muted small" }, (v.created_by === "owner" ? "Added by " + (v.entered_by || "you") : "Added by " + name) + " ",
        timeEl(v.created_at), " · updated ", timeEl(v.updated_at)),
    ];
  }

  // 0.13.0: its numbers: the agent's estimates and what Ember's code makes of them (fees, break-even, net a month).
  function ventureNumbers(v) {
    var n = isObject(v.numbers) ? v.numbers : null;
    if (!n) return null;
    var eur = function (x) { return x === null || x === undefined ? "–" : "€" + num(x).toFixed(2); };
    var sales = arr(n.sales);
    var net = arr(n.net);
    return h("div", { class: "vt-numbers" },
      h("p", null, h("strong", { text: "Numbers: " }), "case #" + n.id + " · ", timeEl(n.created_at)),
      h("dl", { class: "item-grid" },
        h("div", null, h("dt", { text: "A sale" }), h("dd", { text: eur(n.price_eur) + " keeps " + eur(n.net_eur) +
          " (fees " + eur(n.fees_eur) + ", cost " + eur(n.unit_cost_eur) + ")" })),
        h("div", null, h("dt", { text: "Break-even" }), h("dd", { text: n.break_even === null ? "none: a sale doesn't cover its costs" : num(n.break_even).toFixed(1) + " sales a month" })),
        h("div", { title: "Low, likely and high sales a month (the agent's P10, P50 and P90)" }, h("dt", { text: "A month" }),
          h("dd", { text: sales.join(" / ") + " sales: " + net.map(function (x) { return "€" + Math.round(num(x)); }).join(" / ") })),
        h("div", { title: "Expected net a month over six months, the months before the first sale earning nothing" },
          h("dt", { text: "Expected" }), h("dd", { text: "€" + Math.round(num(n.ev_eur)) + " a month" +
            (n.ev_per_hour === null ? "" : " · €" + num(n.ev_per_hour).toFixed(2) + " per hour of yours") })),
        h("div", null, h("dt", { text: "To start" }), h("dd", { text: eur(n.setup_eur) + " · " + num(n.owner_hours) + " h a month from you · first sale in " + n.first_sale_days + " day" + (n.first_sale_days === 1 ? "" : "s") }))));
  }

  // 0.13.0: the independent critic's review of the newest case: its verdict, the fatal flaw, its own numbers (worked out
  // by Ember's code like the agent's) and what would change its mind. The venture ranks by the lower expected net.
  var CRITIC_VERDICTS = {
    back: { icon: "✓", label: "Back it", tone: "good" },
    test: { icon: "?", label: "Test first", tone: "warning" },
    park: { icon: "■", label: "Park it", tone: "critical" },
  };

  function ventureCritique(v) {
    var k = isObject(v.critique) ? v.critique : null;
    if (!k) return null;
    if (!k.verdict) {
      return h("p", { class: "muted small", text: "The critic's review of this case failed " + k.failed + " time" +
        (k.failed === 1 ? "" : "s") + (k.gave_up ? ": it goes without one." : "; it is tried again at the next cycle.") });
    }
    var eur = function (x) { return x === null || x === undefined ? "–" : "€" + num(x).toFixed(2); };
    return h("div", { class: "vt-critique" },
      h("p", null, h("strong", { text: "Critic: " }), chip(CRITIC_VERDICTS, k.verdict), " on case #" + k.case_id + " · ",
        timeEl(k.created_at)),
      h("dl", { class: "item-grid" },
        h("div", null, h("dt", { text: "Fatal flaw" }), h("dd", { text: String(k.fatal_flaw) })),
        h("div", { title: "The critic's own numbers for the same case, worked out by Ember's code like the agent's" },
          h("dt", { text: "Its numbers" }), h("dd", { text: eur(k.price_eur) + " a sale keeps " + eur(k.net_eur) + " · " +
            arr(k.sales).join(" / ") + " sales a month · first sale in " + k.first_sale_months + " month" +
            (k.first_sale_months === 1 ? "" : "s") + " · expected €" + Math.round(num(k.ev_eur)) + " a month" })),
        h("div", null, h("dt", { text: "Would change its mind" }), h("dd", { text: String(k.change_mind) })),
        v.ranking_ev_eur === null || v.ranking_ev_eur === undefined ? null :
          h("div", { title: "What the venture ranks by: the lower of the agent's and the critic's expected net" },
            h("dt", { text: "Ranks by" }), h("dd", { text: "€" + Math.round(num(v.ranking_ev_eur)) + " a month" }))));
  }

  // 0.13.0: what rules it out, checked by Ember's code; you can lift a knock-out for this venture (and restore it).
  var KNOCKOUT_STATE = {
    standing: { icon: "✕", label: "Stands", tone: "critical" },
    lifted: { icon: "✓", label: "Lifted by you", tone: "" },
  };

  function ventureKnockouts(v) {
    var ko = arr(v.knockouts);
    if (!ko.length) return null;
    var status = h("p", { class: "muted small", role: "status" });
    return h("div", { class: "vt-knockouts" },
      h("p", null, h("strong", { text: "Knock-outs: " }), agentName() + " can't propose it while one stands. Lift one if you accept it for this venture."),
      h("ul", { class: "vt-evidence" }, ko.map(function (k) {
        var button = h("button", { type: "button", class: "btn btn-small", text: k.overridden ? "Restore" : "Lift" });
        button.addEventListener("click", function () { liftKnockout(button, status, v, k); });
        return h("li", null, chip(KNOCKOUT_STATE, k.overridden ? "lifted" : "standing"), " ",
          h("strong", { text: String(k.label) }), " · " + String(k.why) + " ", button);
      })),
      status);
  }

  function liftKnockout(button, status, v, k) {
    button.disabled = true;
    status.removeAttribute("data-kind");
    request("POST", "api/ventures/" + v.id + "/knockouts", { rule: k.rule, lift: !k.overridden }).then(function (res) {
      if (res.ok) {
        status.textContent = (k.overridden ? "Restored: " : "Lifted: ") + k.label + ". " + agentName() + " hears it on its next wake.";
        refresh();
        return;
      }
      ownerFailure(res, {}, function (msg) { status.textContent = msg; status.setAttribute("data-kind", "error"); }, null);
      button.disabled = false;
    }).catch(function (err) {
      if (!(err instanceof RequestError)) console.error(err);
      status.textContent = "Couldn't reach Ember, so the knock-out may or may not have changed.";
      status.setAttribute("data-kind", "error");
      button.disabled = false;
    });
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
      back: { title: "Back this venture", submit: it.row.backing_problem ? "Back it anyway" : "Back it",
        intro: [h("p", { text: "Backing tells " + name + " to build it: it plans the first test and asks you, one step at a time, for what only you can do (accounts, money, setup)." }),
          it.row.backing_problem ? h("p", { text: "Ember's code wouldn't back it: " + String(it.row.backing_problem) + ". Backing it anyway is your call." }) : null] },
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
      if (mode === "back" && it.row.backing_problem) body.confirm = true;  // 0.15.0: you saw why code wouldn't
      return body;
    };
    spec.done = function (res) {
      loadVentures();
      var notBacked = isObject(res.data) && res.data.not_backed;
      if (notBacked) return "Not backed: Ember's code put it back in researching (" + String(notBacked) + ").";
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

  // ------------------------------------------------------------------ milestones (0.11.0)
  // The milestones that lead to the goal, each with a date and a measure of done, listed as cards by horizon. The
  // owner adds milestones, leaves notes and drops them. 0.29.0: the owner's goal leads them (its card first, with its
  // form). 0.35.0: on the Plan tab, under the plan: the Roadmap tab, its timeline and its goal tree retired (the plan
  // tree is Ember's way to the goal).

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

  // Calendar days as whole numbers (days since 1970-01-01), so dates compare and space out exactly.
  function dayOf(y, m, d) { return Math.round(Date.UTC(y, m, d) / 86400000); }
  function dayNumber(iso) {
    var p = String(iso || "").split("-");
    return p.length === 3 ? dayOf(Number(p[0]), Number(p[1]) - 1, Number(p[2])) : NaN;
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
      if (!isObject(res.data) || !Array.isArray(res.data.items)) throw new RequestError("malformed", res.data === undefined ? "not JSON" : "the milestones are missing");
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
    setStatusText("rm-load-status", rm.error && !rm.busy ? "Couldn't load the milestones (" + errorText(rm.error) + ")." +
      (data ? " What you see are the milestones loaded earlier." : " Try Refresh.") : "", rm.error && !rm.busy ? "error" : "");
    $("rm-sub").textContent = "Yours, and those Ember's code sets (a venture's first test, the money goal), each with a " +
      "date, a measure of done and how far it got. " + name + "'s steps are the plan's above.";
    safely("goal", function () { renderGoalCard(data); });
    if (!data) return;
    var items = arr(data.items).filter(function (m) { return isObject(m) && m.id !== undefined; });
    renderRoadmapSummary(data, items);
    fillMilestoneParents(items);
    var rows = items.map(function (m) { return Object.assign({}, m, { status: m.horizon, state: m.status }); }).sort(milestoneOrder);
    return renderQueue($("roadmap"), {
      kind: "milestone", rows: rows, groups: ROADMAP_GROUPS,
      empty: emptyState("div", "No milestones yet.", "Add your own with Add milestone: " + name + "'s plan leads to them."),
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
    var name = agentName();
    var open = items.filter(function (m) { return m.status === "open" && !isGoal(m); });
    var counts = {};
    items.forEach(function (m) { if (!isGoal(m)) counts[m.horizon] = (counts[m.horizon] || 0) + 1; });
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
    if (data.forecasts) {  // 0.13.0: the prediction ledger's record, settled by Ember's code
      parts.push(name + "'s forecasts, settled by Ember's code: " + String(data.forecasts) + ".");
    }
    arr(data.autonomy_suggestions).forEach(function (sg) {  // 0.13.0: promotions Ember's code proposes; you decide
      parts.push("Suggestion: you approved " + sg.approved + " requests for " + sg.label + " unchanged (milestone #" +
        sg.milestone_id + "): unlock it with a veto window on that milestone's card?");
    });
    if (data.overhead_usd !== undefined && data.overhead_usd !== null) {
      parts.push("Overhead (plans, reviews, brainstorms, library study): " + usd(data.overhead_usd) + ".");
    }
    $("rm-summary").textContent = parts.join(" ");
  }

  // 0.12.0: a milestone the agent closed as done on its own word, not checked by Ember's code or by you.
  function selfReported(m) {
    return m.status === "done" && m.closed_by === "agent";
  }

  function showMilestoneCard(id) {
    ui.rm.selected = id;
    var card = $("roadmap").querySelector('article[data-id="' + String(id) + '"]');
    if (!card) return;
    Array.prototype.forEach.call($("roadmap").querySelectorAll("article[data-selected]"), function (c) { c.removeAttribute("data-selected"); });
    card.setAttribute("data-selected", "true");
    card.scrollIntoView({ block: "center", behavior: "smooth" });
    var title = card.querySelector(".rm-card-title");
    if (title) title.focus({ preventScroll: true });
  }

  var MILESTONE_RESULT = { done: "Evidence", missed: "Why, and what now", dropped: "Why it was dropped" };

  // 0.13.0: the owner's unlocks for a milestone (the policy engine): each rule manual, with a veto window, or auto,
  // with a daily limit and a budget of actions. Ember's code revokes one on an unclear result, a spent budget, a
  // missed milestone or a veto.
  var AUTONOMY_LEVELS = { manual: "Ask me (manual)", veto_window: "Run unless I veto within 12 h", auto: "Run at once (auto)" };

  // 0.15.0: an unlock carries only what belongs to its milestone, whatever the plan works on.
  function autonomyScope(m) {
    if (m.project_id) return "on the listings of its project (" + (m.project_title || "#" + m.project_id) + ")";
    if (m.venture_id) return "on the listings of its venture's projects (" + (m.venture_title || "#" + m.venture_id) + ")";
    return "email replies only: it names no project or venture, so no listing is its";
  }

  // 0.35.0: ``target`` (a product's box on the Plan tab): its URL, what it is and why unlocks are off.
  function milestoneAutonomy(m, target) {
    // 0.15.0: a rule the milestone never covers isn't offered (one still unlocked from before shows, to take back)
    var rules = arr(m.autonomy).filter(function (r) { return r.fits !== false || r.level !== "manual"; });
    if (!rules.length) return null;
    var off = target ? target.off : ui.rm.data && ui.rm.data.unlocks_off ? String(ui.rm.data.unlocks_off) : "";  // 0.15.0
    var what = target ? target.what : "this milestone";
    var on = rules.filter(function (r) { return r.level !== "manual"; }).length;
    var status = h("p", { class: "muted small", role: "status" });
    return h("div", { class: "rm-autonomy" }, h("details", null,
      h("summary", null, h("strong", { text: "Autonomy: " }), on ? plural(on, "rule") + " unlocked" : "all manual"),
      h("p", { class: "muted small", text: "What " + agentName() + "'s code may carry out for " + what + " without your click, " +
        autonomyScope(m) + ". It takes back an unlock itself on an unclear result, a spent budget, " +
        (target ? "the product's end" : "the milestone's end") + " or your veto." }),
      off ? h("p", { class: "warn-box", text: "Unlocks are off while " + off + ": " + agentName() + "'s code takes them back " +
        "and you can't grant one. Put your Home Assistant user ID in owner_user_ids (Configuration tab), outside safe mode." }) : null,
      h("ul", { class: "vt-evidence" }, rules.map(function (r) {
        var level = h("select", { "aria-label": "Level for " + r.label });
        (r.levels || Object.keys(AUTONOMY_LEVELS)).forEach(function (k) {  // 0.22.0: email replies at most veto_window
          var option = h("option", { value: k, text: AUTONOMY_LEVELS[k] });
          if (k === r.level) option.selected = true;
          level.appendChild(option);
        });
        var perDay = h("input", { type: "number", min: "1", max: "20", value: String(r.per_day), "aria-label": "At most a day", class: "num-small" });
        var budget = h("input", { type: "number", min: "1", max: "100", value: String(r.budget), "aria-label": "In all", class: "num-small" });
        var save = h("button", { type: "button", class: "btn btn-small", text: "Save" });
        save.addEventListener("click", function () {
          setAutonomy(save, status, m, r, { rule: r.rule, level: level.value, per_day: parseInt(perDay.value, 10), budget: parseInt(budget.value, 10) }, target);
        });
        var use = r.level !== "manual" ? " · used " + r.used + " of " + r.budget + " (" + r.used_today + " today)" : "";
        var why = r.why ? " · taken back: " + String(r.why) : "";
        return h("li", null, h("strong", { text: String(r.label) }), use + why, h("div", { class: "form-row" },
          level, " at most ", perDay, " a day, ", budget, " in all ", save));
      })),
      status));
  }

  function setAutonomy(button, status, m, r, body, target) {
    button.disabled = true;
    status.removeAttribute("data-kind");
    request("POST", target ? target.url : "api/milestones/" + m.id + "/autonomy", body).then(function (res) {
      if (res.ok) {
        status.textContent = "Saved: " + r.label + ", " + (AUTONOMY_LEVELS[body.level] || body.level) + ". " + agentName() + " hears it on its next wake.";
        refresh();
        if (target) loadPlan();
        return;
      }
      ownerFailure(res, {}, function (msg) { status.textContent = msg; status.setAttribute("data-kind", "error"); }, null);
      button.disabled = false;
    }).catch(function (err) {
      if (!(err instanceof RequestError)) console.error(err);
      status.textContent = "Couldn't reach Ember, so the unlock may or may not have changed.";
      status.setAttribute("data-kind", "error");
      button.disabled = false;
    });
  }

  // 0.13.0: a prediction Ember's code settles (the agent's odds on a milestone, or a backed venture's first sale).
  var PREDICTION_STATE = {
    open: { icon: "…", label: "Open", tone: "" },
    hit: { icon: "✓", label: "Came true", tone: "good" },
    miss: { icon: "✕", label: "Didn't", tone: "critical" },
    void: { icon: "–", label: "Void", tone: "" },
  };

  function predictionRow(p, label) {
    if (!isObject(p)) return null;
    return h("div", { title: "Ember's code settles it from its records: " + String(p.claim) },
      h("dt", { text: label }),
      h("dd", null, num(p.likely) + "% by " + fmtDay(p.due) + " ", chip(PREDICTION_STATE, p.status),
        p.result ? " " + String(p.result) : ""));
  }

  // 0.13.0: what an action is, in the connector protocol's words: what it reaches, costs and can undo.
  function actionFlags(c) {
    if (!isObject(c)) return null;
    var flags = [];
    if (c.reaches_people) flags.push("reaches people");
    if (c.first_contact) flags.push("can be a first contact");
    if (c.costs_money) flags.push("costs money");
    if (c.publishes_under_owner_identity) flags.push("appears under your name");
    flags.push(c.reversible ? "can be undone (" + String(c.undo) + ")" : "can't be undone");
    return h("p", { class: "muted small", title: "Action class " + String(c.name) },
      sentence(String(c.what)) + (c.by_owner ? " (you carry it out)" : "") + ": " + flags.join(", ") + ".");
  }

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
        m.owner_goal ? plainChip("Your goal") : m.kind === "money_goal" ? plainChip("Ember's own goal") :
          m.created_by === "owner" ? plainChip("Your milestone") : m.created_by === "code" ? plainChip("Set by Ember's code") : null,
        m.simulated ? testTag() : null),
      h("p", { class: "muted small", text: "#" + m.id + (parent ? " · leads to #" + parent.id + " " + parent.title : "") +
        (m.replaces_id ? " · replaces #" + m.replaces_id : "") }),
      h("dl", { class: "item-grid" },
        h("div", null, h("dt", { text: "Due" }), h("dd", { text: due })),
        h("div", null, h("dt", { text: "Done when" }), h("dd", { class: "pre-line", text: asText(m.measure) })),
        progressRow(m),  // 0.29.0
        m.checked ? h("div", null, h("dt", { text: "Checked by Ember's code" }), h("dd", { text: asText(m.checked) })) : null,
        predictionRow(m.prediction, name + "'s odds"),
        // 0.16.3 (analysis bug 1): a backed venture's first test can be met until a week after its date
        m.test_ends ? h("div", null, h("dt", { text: "Last day" }),
          h("dd", { text: fmtDay(m.test_ends) + ", a week after its date. If it is still unmet then, Ember's code closes it missed and " +
            "parks venture #" + m.venture_id + ", which ends the listing tests of its product lines." })) : null,
        // 0.16.3 (analysis bug 5): what stands unlocked, from your unlocks themselves (no longer a note that outlived them)
        m.unlocked ? h("div", null, h("dt", { text: "Unlocked" }), h("dd", { text: asText(m.unlocked) })) : null,
        milestoneAutonomy(m),
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
      drop: isGoal(it.row) ? { title: it.row.owner_goal ? "Remove your goal?" : "Drop " + name + "'s own goal?", submit: it.row.owner_goal ? "Remove goal" : "Drop", danger: true,
        intro: [h("p", { text: it.row.owner_goal ?
          "What leads to it goes on: " + name + "'s own goal (earn what it spends) stands in for it from its next wake, until you set another." :
          "What leads to it goes on, but " + name + "'s code sets no money goal of its own again. Setting your own goal takes its place instead." })] } :
        { title: "Drop this milestone?", submit: "Drop", danger: true,
        intro: [h("p", { text: name + " stops working toward it. It stays in the list, marked dropped, and so do the open milestones that lead to it: they are dropped with it." })] },
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

  // ---- The goal (0.29.0)
  // The owner's goal leads the roadmap: an amount to earn a month or in total, by a date. Ember's code checks it from
  // the books, and everything else leads to it. Until the owner sets one, the money goal Ember's code keeps stands in.

  var PACE = {
    ahead: { icon: "↑", label: "Ahead of pace", tone: "good" },
    "on pace": { icon: "→", label: "On pace", tone: "accent" },
    behind: { icon: "↓", label: "Behind pace", tone: "warning" },
  };

  // The goal at the root: the owner's, or the money goal Ember's code keeps in its place.
  function isGoal(m) { return isObject(m) && (m.owner_goal === true || m.kind === "money_goal"); }
  function progressOf(m) { return isObject(m) && isObject(m.progress) ? m.progress : null; }
  function measured(p) { return !!p && p.percent !== null && p.percent !== undefined && !isNaN(num(p.percent)); }
  function percentText(p) { return measured(p) ? num(p.percent) + "%" : "–"; }

  // How far it got, in words: "34% · $340 of $1,000 · behind pace (60% of its time gone)".
  function progressLine(p) {
    if (!p) return "–";
    if (p.basis === "done") return "Done";
    if (p.basis === "open") return "0%: measured when it is done (no metric and nothing leading to it yet)";
    if (p.basis === "dropped" || p.basis === "missed") return "–";
    var parts = [measured(p) ? percentText(p) : p.text ? "" : "–"];
    if (p.text) parts.push(asText(p.text));
    if (p.pace && PACE[p.pace]) parts.push(PACE[p.pace].label.toLowerCase() + (isNaN(num(p.elapsed)) ? "" : " (" + num(p.elapsed) + "% of its time gone)"));
    return parts.filter(Boolean).join(" · ");
  }

  // A bar (a meter): how far it got; for a paced one, a mark where a straight line to its date would be now.
  function progressBar(p, label, big) {
    var bar = h("div", { class: "pbar" + (big ? " pbar-big" : ""), role: "meter", "aria-label": label, "aria-valuemin": "0", "aria-valuemax": "100" });
    var fill = h("div", { class: "pbar-fill" });
    bar.appendChild(fill);
    if (!measured(p)) {
      bar.setAttribute("data-state", "none");
      bar.setAttribute("aria-valuenow", "0");
      bar.setAttribute("aria-valuetext", p && p.text ? asText(p.text) : "not measured");
      return bar;
    }
    var percent = Math.max(0, Math.min(100, num(p.percent)));
    fill.style.width = percent + "%";
    bar.setAttribute("aria-valuenow", String(percent));
    bar.setAttribute("aria-valuetext", progressLine(p));
    bar.setAttribute("data-state", p.basis === "done" || percent >= 100 ? "done" : p.pace === "behind" ? "behind" : "open");
    if (p.pace && !isNaN(num(p.elapsed))) {
      var mark = h("div", { class: "pbar-pace" });
      mark.style.left = Math.max(0, Math.min(100, num(p.elapsed))) + "%";
      bar.appendChild(mark);
    }
    return bar;
  }

  function progressRow(m) {
    var p = progressOf(m);
    if (!p || p.basis === "dropped") return null;
    return h("div", { class: "item-wide" }, h("dt", { text: "Progress" }),
      h("dd", null, progressBar(p, "How far #" + m.id + " got"), h("span", { class: "small", text: progressLine(p) })));
  }

  function currentGoal() { return ui.rm.data && isObject(ui.rm.data.goal) ? ui.rm.data.goal : null; }

  function renderGoalCard(data) {
    var name = agentName();
    var g = data && isObject(data.goal) ? data.goal : null;
    var last = data && isObject(data.last_goal) ? data.last_goal : null;
    var mine = !!(g && g.owner);
    $("rm-goal-eyebrow").textContent = mine ? "Your goal" : g ? name + "'s own goal, until you set yours" : data ? "No goal yet" : "Your goal";
    $("rm-goal-title").textContent = g ? g.title : data ? "Set the goal " + name + " works toward" : "–";
    var sub = " ";
    if (g) {
      sub = "By " + fmtDay(g.due) + " (" + whenText(g.days) + ") · " + (mine ? "checked by " + name + "'s code from the revenue less expenses you record" +
        (g.per === "total" ? (g.counts_from ? ", counted from " + fmtDay(g.counts_from) : "") : ", over the last " + roadmapLimit("window_days", 30) + " days") : asText(g.measure));
    } else if (data) {
      sub = "Everything in the plan leads to your goal: an amount to earn a month or in total, by a date.";
    }
    $("rm-goal-sub").textContent = sub;
    var p = g ? progressOf(g) : null;
    replace($("rm-goal-progress"), g ? [
      h("div", { class: "goal-figures" },
        h("span", { class: "goal-percent", text: percentText(p) }),
        h("span", { class: "goal-amount", text: p && p.text ? asText(p.text) : "" }),
        p && p.pace ? chip(PACE, p.pace, p.pace) : null),
      progressBar(p, "How far the goal got", true),
      p && p.pace && !isNaN(num(p.elapsed)) ? h("p", { class: "hint", text: num(p.elapsed) + "% of its time has gone; the mark on the bar is where a straight line to its date would be now." }) : null,
    ] : []);
    var note = "";
    if (last) {
      note = (last.status === "done" ? "You reached your goal “" + last.title + "”" : "Your goal “" + last.title + "” was missed") +
        (last.closed_at ? " on " + fmtDate(last.closed_at) : "") + ". " + (g ? name + "'s own goal stands in until you set the next one." : "Set the next one.");
    } else if (mine && g.comment) {
      note = "Why it matters: " + g.comment;
    } else if (g) {
      note = "Set your own goal to lead the plan: it takes the place of this one, and what leads to this one leads to yours.";
    }
    $("rm-goal-note").textContent = note;
    $("rm-goal-note").hidden = !note;
    $("rm-goal-set").textContent = mine ? "Change goal" : "Set your goal";
    $("rm-goal-set").disabled = !data;
    $("rm-goal-remove").hidden = !mine;
    if (!mine) showGoalConfirm(false);
  }

  function setGoalFieldError(id, message) { setMilestoneFieldError(id, message); }

  function openGoalForm(open) {
    var name = agentName();
    $("rm-goal-form").hidden = !open;
    $("rm-goal-set").setAttribute("aria-expanded", String(open));
    if (!open) { $("rm-goal-set").focus(); return; }
    showGoalConfirm(false);
    var g = currentGoal();
    var mine = !!(g && g.owner);
    $("rm-goal-form-intro").textContent = "What " + name + " works toward: everything in the plan leads to it. " + name +
      "'s code checks it from the revenue less expenses you record in the Ledger (in USD, like the books)." +
      (mine ? " Your new goal takes the place of the one standing; what leads to it stays." : "");
    ["amount", "due"].forEach(function (k) { setGoalFieldError("rm-goal-" + k, ""); });
    if (mine) {
      $("rm-goal-amount").value = String(g.target_usd);
      $("rm-goal-due").value = g.due;
      $("rm-goal-comment").value = g.comment || "";
    }
    var per = mine && g.per ? g.per : "month";
    Array.prototype.forEach.call(document.querySelectorAll('input[name="rm-goal-per"]'), function (r) { r.checked = r.value === per; });
    var today = dayNumber(ui.rm.data && ui.rm.data.today);
    if (!isNaN(today)) {
      $("rm-goal-due").min = isoOf(today + roadmapLimit("goal_min_days", 7));
      $("rm-goal-due").max = isoOf(today + roadmapLimit("ahead_days", 366));
    }
    $("rm-goal-amount").focus();
  }

  // An amount as the owner may write it ("$1,000", "1000.50", "1000,50"), as the server reads it ("1000.50"); "" if
  // it isn't one.
  function goalAmount(text) {
    var t = String(text || "").trim().replace(/^\$\s*/, "").replace(/\s*(usd|\$)$/i, "").replace(/\s/g, "");
    if (/^\d{1,3}(,\d{3})+(\.\d{1,2})?$/.test(t)) t = t.replace(/,/g, "");
    else if (/^\d+,\d{1,2}$/.test(t)) t = t.replace(",", ".");
    return /^\d+(\.\d{1,2})?$/.test(t) ? t : "";
  }

  function submitGoalForm() {
    if (ui.goal.saving) return;
    var problems = [];
    ["amount", "due"].forEach(function (k) { setGoalFieldError("rm-goal-" + k, ""); });
    var amount = goalAmount($("rm-goal-amount").value);
    var most = roadmapLimit("goal_max_usd", 100000);
    if (!amount || num(amount) < 1) problems.push(["amount", "Give the amount in USD, at least 1, like 1000 or 250.50."]);
    else if (num(amount) > most) problems.push(["amount", "Keep it at " + usd(most) + " or less."]);
    var due = dayNumber($("rm-goal-due").value);
    var today = dayNumber(ui.rm.data && ui.rm.data.today);
    var first = today + roadmapLimit("goal_min_days", 7);
    if (isNaN(due)) problems.push(["due", "Pick the date it is due."]);
    else if (!isNaN(today) && (due < first || due > today + roadmapLimit("ahead_days", 366))) {
      problems.push(["due", "Pick a date from " + fmtDay(isoOf(first)) + " to a year ahead."]);
    }
    if (problems.length) {
      problems.forEach(function (pr) { setGoalFieldError("rm-goal-" + pr[0], pr[1]); });
      setStatusText("rm-goal-status", problems.length === 1 ? "Please fix the marked field." : "Please fix the marked fields.", "error");
      $("rm-goal-" + problems[0][0]).focus();
      return;
    }
    var checked = document.querySelector('input[name="rm-goal-per"]:checked');
    var g = currentGoal();
    var body = { amount_usd: amount, per: checked ? checked.value : "month", due: $("rm-goal-due").value, replaces: g && g.owner ? g.id : null };
    var comment = $("rm-goal-comment").value.trim();
    if (comment) body.comment = comment.slice(0, roadmapLimit("comment", 1000));
    ui.goal.saving = true;
    $("rm-goal-save").disabled = true;
    setStatusText("rm-goal-status", "Saving…", "");
    request("POST", "api/roadmap/goal", body).then(function (res) {
      if (res.status === 200) {
        openGoalForm(false);
        setStatusText("rm-goal-status", "Your goal is set. " + decisionWake(res, agentName()), "ok");
        loadRoadmap();
        return;
      }
      var data = isObject(res.data) ? res.data : {};
      var msg = typeof data.error === "string" && data.error ? endSentence(sentence(data.error)) : "";
      var field = data.field === "amount_usd" ? "amount" : data.field;
      if ((res.status === 422 || res.status === 409) && msg && (field === "amount" || field === "due")) {
        setGoalFieldError("rm-goal-" + field, msg);
        setStatusText("rm-goal-status", "Please fix the marked field.", "error");
        $("rm-goal-" + field).focus();
        return;
      }
      if (res.status === 409) loadRoadmap();
      setStatusText("rm-goal-status", msg ? "Nothing was saved: " + lowerFirst(msg) : "Nothing was saved (" + httpError(res).message + ").", "error");
    }).catch(function (err) {
      if (!(err instanceof RequestError)) console.error(err);
      setStatusText("rm-goal-status", "Couldn't reach " + agentName() + ", so it's not clear whether your goal was saved. Refresh the plan before you try again.", "error");
    }).then(function () {
      ui.goal.saving = false;
      $("rm-goal-save").disabled = false;
    });
  }

  function showGoalConfirm(open) {
    var g = currentGoal();
    $("rm-goal-confirm").hidden = !open;
    if (!open) return;
    openGoalForm(false);
    $("rm-goal-confirm-text").textContent = "Remove your goal “" + (g ? g.title : "") + "”? What leads to it goes on: " +
      agentName() + "'s own goal (earn what it spends) stands in for it from its next wake, until you set another.";
    $("rm-goal-confirm-no").focus();
  }

  function removeGoal() {
    var g = currentGoal();
    if (ui.goal.removing || !g || !g.owner) return;
    var row = ui.rm.byId[String(g.id)];
    var body = { action: "drop" };
    if (row && !isNaN(num(row.owner_version))) body.expected_version = num(row.owner_version);
    ui.goal.removing = true;
    $("rm-goal-confirm-yes").disabled = true;
    setStatusText("rm-goal-status", "Removing…", "");
    request("POST", "api/roadmap/" + encodeURIComponent(String(g.id)) + "/decide", body).then(function (res) {
      if (res.ok) {
        showGoalConfirm(false);
        setStatusText("rm-goal-status", "Your goal is removed. " + decisionWake(res, agentName()), "ok");
        loadRoadmap();
        $("rm-goal-set").focus();
        return;
      }
      var data = isObject(res.data) ? res.data : {};
      var msg = typeof data.error === "string" && data.error ? endSentence(sentence(data.error)) : "";
      setStatusText("rm-goal-status", msg ? "Nothing was changed: " + lowerFirst(msg) : "Nothing was changed (" + httpError(res).message + ").", "error");
      loadRoadmap();
    }).catch(function (err) {
      if (!(err instanceof RequestError)) console.error(err);
      setStatusText("rm-goal-status", "Couldn't reach " + agentName() + ". Refresh the plan to see whether your goal stands.", "error");
    }).then(function () {
      ui.goal.removing = false;
      $("rm-goal-confirm-yes").disabled = false;
    });
  }

  // The Overview's strip: the goal and how far it got.
  function renderGoalStrip(d) {
    var strip = $("goal-strip");
    var known = isObject(d.roadmap);
    strip.hidden = !known;
    if (!known) return;
    var g = isObject(d.roadmap.goal) ? d.roadmap.goal : null;
    var name = agentName();
    var mine = !!(g && g.owner);
    $("goal-strip-label").textContent = mine ? "Your goal" : g ? name + "'s own goal, until you set yours" : "No goal yet";
    $("goal-strip-title").textContent = g ? g.title + " · by " + fmtDay(g.due) + " (" + whenText(g.days) + ")" :
      "Set the goal " + name + " works toward on the Plan tab.";
    var p = g ? progressOf(g) : null;
    replace($("goal-strip-bar"), g ? [progressBar(p, "How far the goal got"), h("p", { class: "tile-sub", text: progressLine(p) })] : []);
    $("goal-strip-open").textContent = mine ? "Plan" : "Set your goal";
  }

  function initGoal() {
    $("rm-goal-set").addEventListener("click", function () { openGoalForm($("rm-goal-form").hidden); });
    $("rm-goal-cancel").addEventListener("click", function () { openGoalForm(false); });
    $("rm-goal-form").addEventListener("submit", function (ev) { ev.preventDefault(); submitGoalForm(); });
    $("rm-goal-form").addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") { ev.preventDefault(); openGoalForm(false); }
      else if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); submitGoalForm(); }
    });
    $("rm-goal-amount").addEventListener("input", function () { setGoalFieldError("rm-goal-amount", ""); });
    $("rm-goal-due").addEventListener("input", function () { setGoalFieldError("rm-goal-due", ""); });
    $("rm-goal-remove").addEventListener("click", function () { showGoalConfirm(true); });
    $("rm-goal-confirm-no").addEventListener("click", function () { showGoalConfirm(false); $("rm-goal-remove").focus(); });
    $("rm-goal-confirm-yes").addEventListener("click", removeGoal);
    $("goal-strip-open").addEventListener("click", function () {
      selectTab("plan", true);
      var g = ui.data && isObject(ui.data.roadmap) && isObject(ui.data.roadmap.goal) ? ui.data.roadmap.goal : null;
      if (!(g && g.owner) && ui.rm.data) openGoalForm(true);
    });
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
    // 0.29.0: everything leads to the goal: without a choice, a milestone leads to it
    var top = open.filter(isGoal)[0] || null;
    var first = top ? "The goal: #" + top.id + " " + shortTitle(top.title, 50) : "Nothing (a goal of its own)";
    replace(select, [h("option", { value: "", text: first })].concat(open.filter(function (m) { return m !== top; }).map(function (m) {
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
        setStatusText("rm-status", "Added" + (id ? " as milestone #" + id : "") + ". " + agentName() + " sees it on its next wake.", "ok");
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
      setStatusText("rm-status", "Couldn't reach " + agentName() + ", so it's not clear whether the milestone was saved. Refresh the milestones before you try again.", "error");
    }).then(function () {
      ui.rm.saving = false;
      $("rm-form-save").disabled = false;
    });
  }

  function initRoadmap() {
    initGoal();  // 0.29.0
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
    if (d.study === "done") study = plural(num(d.learnings) || 0, "learning") + (num(d.study_usd) > 0 ? " · cost " + usd(d.study_usd) : "") +
      // 0.15.0: a study that ended when its learnings were full says which parts it didn't read
      (d.study_note ? " · " + asText(d.study_note) : "");
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

  function uploadTimeout(chars) {
    return Math.min(UPLOAD_TIMEOUT_MAX_MS, REQUEST_TIMEOUT_MS + Math.ceil(chars / UPLOAD_CHARS_PER_SECOND) * 1000);
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
    var unsure = [];  // no answer in time: the server may have stored it all the same
    ui.lib.saving = true;
    $("lib-form-save").disabled = true;
    var chain = Promise.resolve();
    jobs.forEach(function (job, index) {
      chain = chain.then(function () {
        setStatusText("lib-status", jobs.length > 1 ? "Adding " + (index + 1) + " of " + jobs.length + "…" : "Saving…", "");
        var ready = job.file ? readFileBase64(job.file).then(function (data) {
          return Object.assign({}, base, { file_name: job.file.name, file_data: data }, jobs.length === 1 && common.title ? { title: common.title } : {});
        }) : Promise.resolve(Object.assign({}, base, { text: job.text }, common.title ? { title: common.title } : {}));
        return ready.then(function (body) {
          return request("POST", "api/library", body, { timeout: uploadTimeout((body.file_data || body.text || "").length) });
        }).then(function (res) {
          var data = isObject(res.data) ? res.data : {};
          if (res.status === 201) { added.push("#" + data.id + " " + asText(data.title)); return; }
          var msg = typeof data.error === "string" && data.error ? endSentence(sentence(data.error)) : httpError(res).message;
          failed.push((job.file ? job.file.name + ": " : "") + msg);
        });
      }).catch(function (err) {
        if (!(err instanceof RequestError)) console.error(err);
        if (err instanceof RequestError && err.kind === "timeout") { unsure.push(job.file ? job.file.name : "the text"); return; }
        failed.push((job.file ? job.file.name + ": " : "") + "couldn't reach " + name + " (" + errorText(err) + ")");
      });
    });
    chain.then(function () {
      ui.lib.saving = false;
      $("lib-form-save").disabled = false;
      if (added.length) {  // the For choice stays: the next document is often for the same venture
        ["text", "files", "title", "source", "note"].forEach(function (k) { $("lib-form-" + k).value = ""; });
        if (!failed.length && !unsure.length) openLibraryForm(false);
      }
      if (added.length || unsure.length) loadLibrary();
      var parts = [];
      if (added.length) parts.push("Added " + added.join(", ") + ". " + name + " studies " + (added.length === 1 ? "it" : "them") + " in its next wake cycles.");
      if (failed.length) parts.push("Not added: " + failed.join("; ") + (/[.!?]$/.test(failed[failed.length - 1]) ? "" : "."));
      // A second copy is refused, so a retry can't store it twice; the list shows whether it arrived.
      if (unsure.length) parts.push("No answer in time for " + unsure.join(", ") + ": it may have been added all the same. Check the list below before you add it again.");
      setStatusText("lib-status", parts.join(" "), failed.length || unsure.length ? "error" : "ok");
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

  // ------------------------------------------------------------------ the plan tree (0.34.0)

  // The tree under the owner's goal as a floating node network: its projects (one colour each), their products (as
  // big as their worth), and the opened product's stages with their steps below, each stage's steps on a line in
  // their order. 0.35.0: it steers Ember's cycles (each takes its step from it); the owner closes, drops and keeps
  // products here, lifts Ember's hold, sets each product's unlocks, and sees what changed, the channels and upgrades.
  var PLAN_W = 960;
  var PLAN_COLOURS = 6;
  var PLAN_WAIT = {
    owner: "waits on you", channel: "its channel is off", upgrade: "waits on an upgrade",
    step: "after the one before", hold: "on hold",
  };
  var PLAN_DECIDED = {
    pin: "you pinned it", promise: "a promise or your decision is due", venture: "a venture cycle (the venture share)",
    weight: "the heaviest step", margin: "the product worked on last keeps it", none: "nothing is ready",
  };

  function loadPlan() {
    var pl = ui.pl;
    if (pl.busy) { pl.again = true; return; }
    pl.busy = true;
    safely("plan", renderPlan);
    request("GET", "api/plan").then(function (res) {
      if (!res.ok) throw httpError(res);
      if (!isObject(res.data) || !Array.isArray(res.data.projects)) throw new RequestError("malformed", res.data === undefined ? "not JSON" : "the plan is missing");
      pl.data = res.data;
      pl.stamp = res.data.stamp;
      pl.error = null;
    }).catch(function (err) {
      if (!(err instanceof RequestError)) console.error(err);
      pl.error = err;
    }).then(function () {
      pl.busy = false;
      safely("plan", renderPlan);
      if (pl.again) { pl.again = false; loadPlan(); }
    });
  }

  function planProducts(d) {
    var all = [];
    arr(d.projects).forEach(function (p, index) {
      arr(p.products).forEach(function (q) { all.push({ project: p, colour: index % PLAN_COLOURS, product: q }); });
    });
    return all;
  }

  function renderPlan() {
    var pl = ui.pl, d = pl.data;
    setStatusText("pl-load-status", pl.error ? "Couldn't load the plan: " + errorText(pl.error) : !d && pl.busy ? "Loading the plan…" : "", pl.error ? "error" : "");
    if (!d) { ["pl-now", "pl-net", "pl-detail", "pl-changes", "pl-channels", "pl-upgrades", "pl-picks"].forEach(function (id) { replace($(id), null); }); return; }
    var products = planProducts(d);
    var known = products.some(function (x) { return x.product.id === pl.selected; });
    if (!known) {
      var nowLine = isObject(d.now) ? d.now.line : null;
      var mine = products.filter(function (x) { return x.product.line === nowLine && nowLine !== null; })[0]
        || products.filter(function (x) { return x.product.status === "open"; })[0];
      pl.selected = mine ? mine.product.id : null;
    }
    var goal = isObject(d.goal) ? d.goal : null;
    $("pl-sub").textContent = (goal ? "Your goal: " + goal.title + " (by " + fmtDay(goal.due) + "). " : "")
      + "Each cycle takes its step from this plan: what you pinned, a promise or your decision due, then the heaviest step.";
    renderPlanNow(d);
    renderPlanNet(d, products);
    if (!isBusy($("pl-detail"))) renderPlanDetail(products);  // a reason being typed stays
    renderPlanChanges(d);
    renderPlanChannels(d);
    renderPlanUpgrades(d);
    renderPlanPicks(d);
  }

  function renderPlanNow(d) {
    var now = isObject(d.now) ? d.now : {};
    var next = arr(d.next), waiting = arr(d.waiting);
    replace($("pl-now"), [
      h("div", { class: "pl-now-card" },
        h("p", { class: "small-head", text: "The next cycle's step" }),
        h("p", { class: "pl-now-title", text: now.id ? now.title : sentence(PLAN_DECIDED[now.decided] || "nothing is ready") }),
        now.id ? h("p", { class: "muted", text: sentence(PLAN_DECIDED[now.decided] || now.decided) + (now.line ? " · line #" + now.line : "") }) : null,
        now.why ? h("p", { class: "pl-why muted", text: now.why }) : null),
      h("div", { class: "pl-now-card" },
        h("p", { class: "small-head", text: "Next" }),
        next.length
          ? h("ol", { class: "pl-list" }, next.map(function (s) {
            return h("li", null, s.title, h("span", { class: "muted", text: (s.line ? " · line #" + s.line : "") + (s.weight !== null && s.weight !== undefined ? " · weight " + s.weight : "") }));
          }))
          : h("p", { class: "muted", text: "Nothing else is ready." })),
      h("div", { class: "pl-now-card" },
        h("p", { class: "small-head", text: "Waiting on you" }),
        waiting.length
          ? h("ul", { class: "pl-list" }, waiting.map(function (s) { return h("li", { text: s.title + (s.line ? " (line #" + s.line + ")" : "") }); }))
          : h("p", { class: "muted", text: "Nothing." })),
    ]);
  }

  // A smooth link from a parent's foot to a child's head (vertical tangents at both ends).
  function planLink(x1, y1, x2, y2) {
    var m = (y1 + y2) / 2;
    return "M" + x1 + " " + y1 + "C" + x1 + " " + m + " " + x2 + " " + m + " " + x2 + " " + y2;
  }

  function planCut(text, chars) {
    text = String(text || "");
    return text.length > chars ? text.slice(0, Math.max(1, chars - 1)) + "…" : text;
  }

  // Two lines of at most ``chars`` characters each, broken after a space or a hyphen where it can.
  function planLines(text, chars) {
    text = String(text || "");
    if (text.length <= chars) return [text];
    var cut = Math.max(text.lastIndexOf(" ", chars), text.lastIndexOf("-", chars - 1) + 1);
    if (cut < chars / 3) return [planCut(text, chars)];
    return [text.slice(0, cut).trim(), planCut(text.slice(cut).trim(), chars)];
  }

  function renderPlanNet(d, products) {
    var pl = ui.pl, host = $("pl-net");
    var projects = arr(d.projects);
    if (!projects.length) {
      replace(host, emptyState("div", "No products yet", "Ember's code lays each open product line out here at the next cycle."));
      pl.size = null;
      return;
    }
    var W = PLAN_W, slot = W / projects.length, top = 200;
    var links = [], nodes = [], spines = [], placed = {}, bottom = top + 60;
    var nowId = isObject(d.now) ? d.now.id : null;
    var goal = { x: W / 2, y: 74, r: 44 };
    projects.forEach(function (p, i) {
      var colour = i % PLAN_COLOURS, px = slot * (i + 0.5), py = top + (i % 2 ? 24 : 0);
      links.push({ d: planLink(goal.x, goal.y + goal.r, px, py - 30), c: colour });
      nodes.push({ kind: "project", x: px, y: py, r: 30, c: colour, item: p, label: p.title });
      var items = arr(p.products), perRow = Math.max(1, Math.floor(slot / 112));
      items.forEach(function (q, j) {
        var row = Math.floor(j / perRow), col = j % perRow, inRow = Math.min(perRow, items.length - row * perRow);
        var width = slot / perRow;
        var qx = px + (col - (inRow - 1) / 2) * width, qy = py + 124 + row * 104 + (col % 2 ? 18 : 0);
        var worth = Math.max(0, Math.min(10, Number(q.worth) || 0)), r = 10 + worth;
        links.push({ d: planLink(px, py + 30, qx, qy - r), c: colour });
        nodes.push({ kind: "product", x: qx, y: qy, r: r, c: colour, item: q, label: q.title, chars: Math.max(8, Math.floor((width - 8) / 6.6)) });
        placed[q.id] = { x: qx, y: qy, r: r, c: colour };
        bottom = Math.max(bottom, qy + r + 40);
      });
    });
    var height = bottom + 24;
    var open = products.filter(function (x) { return x.product.id === pl.selected; })[0];
    if (open && placed[open.product.id]) {
      var at = placed[open.product.id], stages = arr(open.product.stages);
      var sy = height + 56, sslot = W / Math.max(1, stages.length), deepest = sy + 40, bus = sy - 34;
      // one trunk down from the opened product to a line above its stages, then a short drop into each: it passes
      // under every other product instead of across them
      if (stages.length) {
        links.push({ d: "M" + at.x + " " + (at.y + at.r) + "V" + bus, c: at.c, trunk: true });
        links.push({ d: "M" + Math.min(at.x, sslot / 2) + " " + bus + "H" + Math.max(at.x, sslot * (stages.length - 0.5)), c: at.c, trunk: true });
      }
      stages.forEach(function (s, k) {
        var sx = sslot * (k + 0.5);
        links.push({ d: "M" + sx + " " + bus + "V" + (sy - 12), c: at.c });
        nodes.push({ kind: "stage", x: sx, y: sy, r: 12, c: at.c, item: s, label: sentence(s.stage) + (s.total ? " " + s.done + "/" + s.total : "") });
        var steps = arr(s.steps), first = sy + 58, last = first + (steps.length - 1) * 40;
        var lineX = sx - sslot / 2 + 18;
        if (steps.length) spines.push({ x1: sx, y1: sy + 12, x: lineX, y2: last, c: at.c });
        steps.forEach(function (st, m) {
          nodes.push({ kind: "step", x: lineX, y: first + m * 40, r: 7, c: at.c, item: st, label: st.title, chars: Math.max(10, Math.floor((sslot - 36) / 6.4)), now: st.id === nowId });
        });
        if (steps.length) deepest = Math.max(deepest, last + 34);
      });
      height = deepest + 12;
    }
    pl.size = { w: W, h: height };
    if (!pl.view) pl.view = { k: 1, x: 0, y: 0 };
    var root = svg("svg", { class: "pl-svg", role: "group", "aria-label": "The plan tree: your goal, " + projects.length + " project" + (projects.length === 1 ? "" : "s") + " and their products" });
    var linkLayer = svg("g", { class: "pl-links", "aria-hidden": "true" });
    links.forEach(function (l) { linkLayer.appendChild(svg("path", { class: "pl-link pl-c" + l.c + (l.trunk ? " pl-trunk" : ""), d: l.d })); });
    spines.forEach(function (s) {
      linkLayer.appendChild(svg("path", { class: "pl-link pl-c" + s.c, d: planLink(s.x1, s.y1, s.x, s.y1 + 34) }));
      linkLayer.appendChild(svg("line", { class: "pl-spine pl-c" + s.c, x1: s.x, y1: s.y1 + 34, x2: s.x, y2: s.y2 }));
    });
    var nodeLayer = svg("g", { class: "pl-nodes" });
    nodeLayer.appendChild(svg("g", { class: "pl-node pl-goal", "aria-hidden": "true" },
      svg("circle", { class: "pl-halo", cx: goal.x, cy: goal.y, r: goal.r + 16 }),
      svg("circle", { class: "pl-dot", cx: goal.x, cy: goal.y, r: goal.r }),
      svg("text", { x: goal.x, y: goal.y - 2, "text-anchor": "middle" }, "Your goal"),
      isObject(d.goal) ? svg("text", { class: "pl-sub", x: goal.x, y: goal.y + 15, "text-anchor": "middle" }, "by " + fmtDay(d.goal.due)) : null));
    nodes.forEach(function (n) { nodeLayer.appendChild(planNode(n)); });
    root.appendChild(linkLayer);
    root.appendChild(nodeLayer);
    replace(host, root);
    planApplyView();
  }

  function planNode(n) {
    var it = n.item, takes = n.kind === "product" || n.kind === "step";
    var status = it.status || "open", waiting = it.waiting || null;
    var said = n.kind === "product"
      ? it.title + ", " + (it.template || "product") + (it.stage ? ", in " + it.stage : ", " + status) + ", worth " + it.worth + (n.item.id === ui.pl.selected ? ", opened" : "")
      : n.kind === "step" ? it.title + ", " + planStepState(it) : n.label;
    var g = svg("g", {
      class: "pl-node pl-" + n.kind + " pl-c" + n.c,
      "data-status": status, "data-waiting": waiting, "data-now": n.now ? "true" : null, "data-kind": n.kind === "step" ? it.kind : null,
      "data-selected": n.kind === "product" && it.id === ui.pl.selected ? "true" : null,
      "data-pinned": it.pinned ? "true" : null,
      tabindex: takes ? "0" : null, role: takes ? "button" : null, "aria-label": takes ? said : null,
      "aria-hidden": takes ? null : "true",
    }, svg("title", null, said));
    if (n.now || waiting === "owner" || waiting === "upgrade" || it.pinned) g.appendChild(svg("circle", { class: "pl-ring", cx: n.x, cy: n.y, r: n.r + 5 }));
    g.appendChild(svg("circle", { class: "pl-dot", cx: n.x, cy: n.y, r: n.r }));
    if (n.kind === "project") {
      g.appendChild(svg("text", { x: n.x, y: n.y + 4, "text-anchor": "middle" }, planCut(n.label, 9)));
    } else if (n.kind === "product") {
      planLines(n.label, n.chars).forEach(function (line, i) {
        g.appendChild(svg("text", { x: n.x, y: n.y + n.r + 16 + i * 14, "text-anchor": "middle" }, line));
      });
    } else if (n.kind === "stage") {
      g.appendChild(svg("text", { x: n.x + 18, y: n.y + 4 }, n.label));
    } else {
      g.appendChild(svg("text", { x: n.x + 14, y: n.y + 4 }, planCut(n.label, n.chars)));
    }
    if (takes) {
      var act = function () {
        if (n.kind === "product") { ui.pl.selected = it.id === ui.pl.selected ? null : it.id; safely("plan", renderPlan); planFocusNode("product", it.id); }
        else planShowStep(it.id);
      };
      g.addEventListener("click", function (ev) { if (!ui.pl.dragged) act(); ev.stopPropagation(); });
      g.addEventListener("keydown", function (ev) { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); act(); } });
      g.setAttribute("data-id", String(it.id));
    }
    return g;
  }

  function planFocusNode(kind, id) {
    var el = $("pl-net").querySelector(".pl-" + kind + "[data-id=\"" + String(id) + "\"]");
    if (el && typeof el.focus === "function") el.focus();
  }

  function planShowStep(id) {
    var row = $("pl-step-" + id);
    if (!row) return;
    row.setAttribute("data-flash", "true");
    window.setTimeout(function () { row.removeAttribute("data-flash"); }, 1600);
    row.focus();
  }

  function planApplyView() {
    var pl = ui.pl, root = $("pl-net").querySelector("svg");
    if (!root || !pl.size) return;
    var v = pl.view || { k: 1, x: 0, y: 0 };
    var w = pl.size.w / v.k, hgt = pl.size.h / v.k;
    root.setAttribute("viewBox", [v.x, v.y, w, hgt].map(function (n) { return Math.round(n * 10) / 10; }).join(" "));
    root.style.aspectRatio = pl.size.w + " / " + pl.size.h;
  }

  function planZoomAt(factor, clientX, clientY) {
    var pl = ui.pl, root = $("pl-net").querySelector("svg");
    if (!root || !pl.size) return;
    var v = pl.view || { k: 1, x: 0, y: 0 }, rect = root.getBoundingClientRect();
    var fx = rect.width ? (clientX - rect.left) / rect.width : 0.5, fy = rect.height ? (clientY - rect.top) / rect.height : 0.5;
    var k = Math.max(0.5, Math.min(4, v.k * factor));
    var px = v.x + fx * pl.size.w / v.k, py = v.y + fy * pl.size.h / v.k;
    pl.view = { k: k, x: px - fx * pl.size.w / k, y: py - fy * pl.size.h / k };
    planApplyView();
  }

  function planZoom(factor) {
    var rect = $("pl-net").getBoundingClientRect();
    planZoomAt(factor, rect.left + rect.width / 2, rect.top + rect.height / 2);
  }

  var PLAN_VERDICT = { met: "met", missed: "missed", retry: "too little reach: one more try", decide: "you decide", scale: "sold: scale it" };

  function renderPlanDetail(products) {
    var pl = ui.pl, host = $("pl-detail");
    var open = products.filter(function (x) { return x.product.id === pl.selected; })[0];
    if (!open) { replace(host, emptyState("div", "Choose a product", "Its stages and steps open below the tree, with its numbers and its worth.")); return; }
    var q = open.product, n = isObject(q.numbers) ? q.numbers : {};
    var audience = { en: "English", de: "German", both: "English and German" }[q.audience] || "";
    replace(host, [
      h("div", { class: "pl-detail-head" },
        h("h3", { class: "pl-detail-title", text: q.title + " · line #" + q.line }),
        h("p", { class: "hint", text: [q.template, q.stage ? "in " + q.stage : q.status, audience ? "speaks " + audience : ""].filter(Boolean).join(" · ") })),
      h("p", { class: "pl-numbers", text: "Views " + (n.views || 0) + " · favorites " + (n.favorites || 0) + " · orders " + (n.orders || 0) + " · pins " + (n.pins || 0) + " · Bluesky posts " + (n.posts || 0) + " · blog posts " + (n.blog || 0) }),
      planHold(q),
      planDecideBy(q),
      q.status === "open" ? planWorthForm(q) : null,
      q.status === "open" ? planEndActions(q) : null,
      q.status === "open" ? planAutonomy(q) : null,
      arr(q.stages).map(function (s) {
        var state = s.status !== "open" ? s.status : s.current ? "now" : "later";
        return h("section", { class: "pl-stage", "data-status": s.status, "data-current": s.current ? "true" : null },
          h("h4", { class: "pl-stage-title", text: s.title + " · " + state + (s.total ? " (" + s.done + " of " + s.total + ")" : "") }),
          arr(s.steps).length ? h("ul", { class: "pl-steps" }, arr(s.steps).map(function (st) { return planStepRow(st, q); })) : h("p", { class: "muted", text: s.stage === "maintain" ? "Its recurring steps come once the stages before it are done." : "No steps." }));
      }),
    ]);
  }

  // 0.35.0: Ember may hold a product to work elsewhere (with her reason); the owner lifts the hold here.
  function planHold(q) {
    if (!q.hold || q.status !== "open") return null;
    var lift = h("button", { type: "button", class: "btn btn-small", text: "Lift the hold" });
    lift.addEventListener("click", function () { planProductAction(q, "resume", null, lift, "The hold is lifted: its steps are weighed again."); });
    return h("div", { class: "pl-hold" },
      h("p", null, h("strong", { text: agentName() + " holds it: " }), String(q.hold)),
      lift);
  }

  // 0.35.0: a product's decide-by dates (from the day its first listing was seen live), in place of the listing test.
  function planDecideBy(q) {
    if (!q.live_since) return null;
    var found = isObject(q.decide_by) ? q.decide_by : {};
    var days = [["day7", "Day 7: 10 views"], ["day14", "Day 14: 30 views and 2 favorites"], ["day28", "Day 28: one more try"], ["day21", "Day 21: a first order"]];
    var parts = days.filter(function (x) { return x[0] !== "day28" || found.day28 || found.day14 === "retry"; }).map(function (x) {
      return x[1] + " · " + (found[x[0]] ? PLAN_VERDICT[found[x[0]]] || found[x[0]] : "not yet");
    });
    var pushed = q.pushed_until && q.pushed_until >= ui.pl.data.today ? "Its marketing comes first until " + fmtDay(q.pushed_until) + "." : "";
    return h("div", { class: "pl-decide" },
      h("p", { class: "small-head", text: "Its test, from " + fmtDay(q.live_since) }),
      h("ul", { class: "pl-list" }, parts.map(function (t) { return h("li", { text: t }); })),
      pushed ? h("p", { class: "muted", text: pushed }) : null);
  }

  // 0.35.0: only the owner closes a product (done: its line succeeded) or drops one, with an optional reason.
  function planEndActions(q) {
    var box = h("div", { class: "pl-end" });
    var why = h("input", { type: "text", maxlength: "300", class: "pl-why-input", "aria-label": "Why (optional)", placeholder: "Why (optional)" });
    var confirm = h("div", { class: "confirm", hidden: true });
    var buttons = h("div", { class: "form-actions" });
    function ask(done) {
      replace(confirm, [
        h("p", { text: done ? "Close “" + q.title + "” as done? Its open steps close with it and " + agentName() + " stops working on it." :
          "Drop “" + q.title + "”? Its open steps close with it and " + agentName() + " stops working on it. This can't be undone here." }),
        why,
        h("div", { class: "form-actions" },
          planButton(done ? "Close as done" : "Drop it", done ? "btn btn-primary" : "btn btn-danger", function (button) {
            planProductAction(q, done ? "done" : "drop", { why: why.value.trim() }, button, done ? "Closed as done." : "Dropped.");
          }),
          planButton("Cancel", "btn", function () { confirm.hidden = true; buttons.hidden = false; })),
      ]);
      confirm.hidden = false;
      buttons.hidden = true;
      why.focus();
    }
    replace(buttons, [
      planButton("Close as done", "btn", function () { ask(true); }),
      planButton("Drop it", "btn", function () { ask(false); }),
    ]);
    ui.pl.askEnd = ask;  // a decide-by step's Drop it asks the same
    replace(box, [h("p", { class: "small-head", text: "Your decision" }), buttons, confirm]);
    return box;
  }

  function planButton(text, cls, onClick) {
    var button = h("button", { type: "button", class: cls, text: text });
    button.addEventListener("click", function () { onClick(button); });
    return button;
  }

  // POST api/plan/products/{id}/{action} (done, drop, resume), then the plan again.
  function planProductAction(q, action, body, button, said) {
    button.disabled = true;
    request("POST", "api/plan/products/" + encodeURIComponent(String(q.id)) + "/" + action, body || {}).then(function (res) {
      if (!res.ok) {
        ownerFailure(res, {}, function (msg) { setStatusText("pl-status", msg, "error"); }, function (msg) { setStatusText("pl-status", msg, "error"); });
        button.disabled = false;
        return;
      }
      setStatusText("pl-status", said + " " + agentName() + " sees it on its next wake.", "ok");
      loadPlan();
      refresh();
    }).catch(function (err) {
      button.disabled = false;
      setStatusText("pl-status", "Couldn't reach " + agentName() + ": " + errorText(err) + ". Refresh the plan to see whether it changed.", "error");
    });
  }

  // 0.35.0: a product's Autonomy box (its unlocks stand on its own milestone, which Ember's code keeps).
  function planAutonomy(q) {
    if (!arr(q.autonomy).length) return null;
    var off = ui.pl.data && ui.pl.data.unlocks_off ? String(ui.pl.data.unlocks_off) : "";
    return milestoneAutonomy({ id: q.milestone, autonomy: q.autonomy, project_id: q.line, project_title: q.title },
      { url: "api/plan/products/" + encodeURIComponent(String(q.id)) + "/autonomy", what: "this product", off: off });
  }

  // A promise to the owner is no step Ember takes: the steps in front of it carry it until it is kept.
  function planStepState(st) {
    if (st.status !== "open") return st.status;
    if (st.kind === "promise") return "promised";
    return st.waiting ? PLAN_WAIT[st.waiting] || st.waiting : "ready";
  }

  function planStepRow(st, q) {
    var row = h("li", { class: "pl-step", id: "pl-step-" + st.id, tabindex: "-1", "data-status": st.status, "data-waiting": st.waiting || null, "data-kind": st.kind },
      h("span", { class: "pl-step-state", text: planStepState(st) }),
      h("span", { class: "pl-step-title", text: st.title }),
      st.weight !== null && st.weight !== undefined ? h("span", { class: "pl-step-weight", text: "weight " + st.weight }) : null,
      st.due ? h("span", { class: "muted", text: "due " + fmtDay(st.due) }) : null,
      st.stale ? h("span", { class: "pl-stale", text: "ready " + Math.floor(st.age_days) + " days" }) : null,
      planPinButton(st),
      planDecideButtons(st, q));
    if (st.why) row.appendChild(h("p", { class: "pl-why muted", text: st.why }));
    else if (st.status !== "open" && st.result) row.appendChild(h("p", { class: "pl-why muted", text: st.result }));
    return row;
  }

  // 0.35.0: at a decide-by date the owner keeps the product (the step closes) or drops it.
  function planDecideButtons(st, q) {
    if (st.status !== "open" || st.template !== "decide/owner" || !q) return null;
    var keep = planButton("Keep it", "btn btn-small", function (button) {
      button.disabled = true;
      request("POST", "api/plan/steps/" + encodeURIComponent(String(st.id)) + "/keep", {}).then(function (res) {
        if (!res.ok) throw httpError(res);
        setStatusText("pl-status", "Kept: " + agentName() + " goes on with it.", "ok");
        loadPlan();
      }).catch(function (err) {
        button.disabled = false;
        setStatusText("pl-status", "Couldn't save it: " + errorText(err), "error");
      });
    });
    var drop = planButton("Drop it", "btn btn-small", function () { if (ui.pl.askEnd) ui.pl.askEnd(false); });
    return h("span", { class: "pl-decide-buttons" }, keep, drop);
  }

  function planPinButton(st) {
    if (st.status !== "open" || st.kind === "promise" || st.kind === "owner") return null;
    var button = h("button", { type: "button", class: "btn btn-small", "aria-pressed": st.pinned ? "true" : "false", text: st.pinned ? "Unpin" : "Pin" });
    button.addEventListener("click", function () { pinPlanStep(st.id, !st.pinned, button); });
    return button;
  }

  function pinPlanStep(id, pinned, button) {
    button.disabled = true;
    request("POST", "api/plan/steps/" + encodeURIComponent(String(id)) + "/pin", { pinned: pinned }).then(function (res) {
      if (!res.ok) throw httpError(res);
      setStatusText("pl-status", pinned ? "Pinned: the next cycle takes it first." : "Unpinned.", "ok");
      loadPlan();
    }).catch(function (err) {
      button.disabled = false;
      setStatusText("pl-status", "Couldn't save it: " + errorText(err), "error");
    });
  }

  function planWorthForm(q) {
    var mine = q.owner_worth !== null && q.owner_worth !== undefined;
    var input = h("input", { type: "number", id: "pl-worth", min: "0.5", max: "10", step: "0.1", inputmode: "decimal", value: mine ? String(q.owner_worth) : "", placeholder: String(q.worth_code), "aria-describedby": "pl-worth-hint" });
    var save = h("button", { type: "button", class: "btn", text: "Set worth" });
    var clear = mine ? h("button", { type: "button", class: "btn btn-ghost", text: "Use Ember's code's" }) : null;
    save.addEventListener("click", function () {
      var value = input.value.trim() === "" ? null : Number(input.value);
      if (value !== null && !(value >= 0.5 && value <= 10)) { setStatusText("pl-status", "Give a worth from 0.5 to 10.", "error"); input.focus(); return; }
      setPlanWorth(q.id, value, save);
    });
    if (clear) clear.addEventListener("click", function () { setPlanWorth(q.id, null, clear); });
    return h("div", { class: "field pl-worth" },
      h("label", { for: "pl-worth", text: "Worth" + (mine ? " (yours)" : "") }),
      h("div", { class: "pl-worth-row" }, input, save, clear),
      h("p", { class: "hint", id: "pl-worth-hint", text: "Ember's code: " + q.worth_code + " (it could earn $" + q.could_earn + " a month, × its chance " + q.chance + "). Yours replaces it, from 0.5 to 10: $5 a month is about 2, $20 about 4.6, $60 about 7.4." }));
  }

  function setPlanWorth(id, worth, button) {
    button.disabled = true;
    request("POST", "api/plan/products/" + encodeURIComponent(String(id)) + "/worth", { worth: worth }).then(function (res) {
      if (!res.ok) throw httpError(res);
      setStatusText("pl-status", worth === null ? "Ember's code's worth counts again." : "Saved: worth " + worth + ".", "ok");
      loadPlan();
    }).catch(function (err) {
      button.disabled = false;
      setStatusText("pl-status", "Couldn't save it: " + errorText(err), "error");
    });
  }

  var PLAN_ACTORS = { agent: null, code: "Ember's code", owner: "You" };
  var PLAN_ACTIONS = {
    add: "added", split: "split", replace: "replaced", done: "said done", wait: "said it waits", unwait: "lifted the wait on",
    hold: "held", resume: "resumed", drop: "dropped", close: "closed",
  };

  // 0.35.0: today's changes to the plan that weren't a check passing, the newest first.
  function renderPlanChanges(d) {
    var changes = arr(d.changes), name = agentName();
    if (!changes.length) { replace($("pl-changes"), h("p", { class: "muted", text: "No changes today." })); return; }
    replace($("pl-changes"), h("ul", { class: "pl-list pl-changes-list" }, changes.map(function (c) {
      var who = PLAN_ACTORS[c.actor] || name;
      return h("li", null, timeEl(c.at), " · ", h("strong", { text: who + " " + (PLAN_ACTIONS[c.action] || c.action) + " " }),
        "#" + c.id + " " + c.title + (c.line ? " (line #" + c.line + ")" : ""),
        c.why ? h("span", { class: "muted", text: ": " + c.why }) : null);
    })));
  }

  var PLAN_CHANNELS = { pinterest: "Pinterest", bluesky: "Bluesky", blog: "Blog" };

  // 0.35.0: each channel as a mirror of the products' marketing: what it does next and did lately.
  function renderPlanChannels(d) {
    var channels = arr(d.channels);
    replace($("pl-channels"), h("div", { class: "pl-now" }, channels.map(function (c) {
      var open = arr(c.steps).filter(function (st) { return st.status === "open"; });
      var done = arr(c.steps).filter(function (st) { return st.status === "done"; });
      return h("div", { class: "pl-now-card" },
        h("p", { class: "small-head", text: (PLAN_CHANNELS[c.channel] || c.channel) + (c.factor !== 1 ? " · works " + c.factor + "×" : "") }),
        open.length ? h("ul", { class: "pl-list" }, open.map(function (st) {
          return h("li", null, st.title, h("span", { class: "muted", text: " · line #" + st.line + (st.due ? " · due " + fmtDay(st.due) : "") + (st.waiting ? " · " + (PLAN_WAIT[st.waiting] || st.waiting) : "") }));
        })) : h("p", { class: "muted", text: "Nothing to do now." }),
        done.length ? h("p", { class: "muted", text: plural(done.length, "step") + " done in the last 14 days." }) : null);
    })));
  }

  // 0.35.0: the upgrades open with the steps waiting on each and what they would weigh: what is worth building next.
  function renderPlanUpgrades(d) {
    var upgrades = arr(d.upgrades).filter(function (u) { return arr(u.steps).length; });
    if (!upgrades.length) { replace($("pl-upgrades"), h("p", { class: "muted", text: "No step waits on an upgrade." })); return; }
    replace($("pl-upgrades"), h("ul", { class: "pl-list" }, upgrades.map(function (u) {
      var steps = arr(u.steps);
      return h("li", null,
        h("strong", { text: u.id ? "Upgrade #" + u.id + " " + u.title + " (" + u.status + ")" : "No upgrade request yet" }),
        h("ul", { class: "pl-list" }, steps.map(function (st) {
          return h("li", null, st.title, h("span", { class: "muted", text: " · line #" + st.line + (st.weight !== null && st.weight !== undefined ? " · would weigh " + st.weight : "") }));
        })));
    })));
  }

  function renderPlanPicks(d) {
    var picks = arr(d.picks);
    if (!picks.length) { replace($("pl-picks"), h("p", { class: "muted", text: "No cycle has run with the plan yet." })); return; }
    replace($("pl-picks"), h("div", { class: "table-wrap" }, h("table", { class: "pl-table" },
      h("thead", null, h("tr", null, ["Cycle", "When", "Kind", "Its step", "Why"].map(function (t) { return h("th", { scope: "col", text: t }); }))),
      h("tbody", null, picks.map(function (p) {
        var step = p.step ? p.step_title + " · " + (p.product_title || "line #" + p.product) : p.kind === "venture" ? "a venture" : "none ready";
        return h("tr", null,
          h("td", { text: "#" + p.cycle }), h("td", { text: fmtDateTime(p.at) }), h("td", { text: p.kind }),
          h("td", { text: step }), h("td", { text: sentence(PLAN_DECIDED[p.decided] || p.decided) }));
      })))));
  }

  function initPlan() {
    var pl = ui.pl, host = $("pl-net");
    $("pl-refresh").addEventListener("click", loadPlan);
    $("pl-zoom-in").addEventListener("click", function () { planZoom(1.25); });
    $("pl-zoom-out").addEventListener("click", function () { planZoom(0.8); });
    $("pl-fit").addEventListener("click", function () { pl.view = { k: 1, x: 0, y: 0 }; planApplyView(); });
    // Ctrl (or Cmd) and the wheel zoom; the wheel alone scrolls the page, as everywhere else
    host.addEventListener("wheel", function (ev) {
      if (!(ev.ctrlKey || ev.metaKey) || !pl.size) return;
      ev.preventDefault();
      planZoomAt(ev.deltaY < 0 ? 1.15 : 0.87, ev.clientX, ev.clientY);
    }, { passive: false });
    var drag = null;
    host.addEventListener("pointerdown", function (ev) {
      // a mouse drags the network; a finger scrolls the box (a phone shows it wider than the screen) and the page
      if (!pl.size || ev.button !== 0 || ev.pointerType !== "mouse") return;
      drag = { id: ev.pointerId, x: ev.clientX, y: ev.clientY, view: pl.view || { k: 1, x: 0, y: 0 }, moved: false };
      pl.dragged = false;
    });
    host.addEventListener("pointermove", function (ev) {
      if (!drag || ev.pointerId !== drag.id) return;
      var root = host.querySelector("svg");
      if (!root) return;
      var rect = root.getBoundingClientRect(), dx = ev.clientX - drag.x, dy = ev.clientY - drag.y;
      if (!drag.moved && Math.abs(dx) + Math.abs(dy) < 4) return;
      if (!drag.moved) { drag.moved = true; pl.dragged = true; host.setPointerCapture(ev.pointerId); host.setAttribute("data-dragging", "true"); }
      var scale = rect.width ? pl.size.w / drag.view.k / rect.width : 1;
      pl.view = { k: drag.view.k, x: drag.view.x - dx * scale, y: drag.view.y - dy * scale };
      planApplyView();
    });
    var end = function (ev) {
      if (!drag || ev.pointerId !== drag.id) return;
      if (drag.moved && host.hasPointerCapture(ev.pointerId)) host.releasePointerCapture(ev.pointerId);
      host.removeAttribute("data-dragging");
      drag = null;
      window.setTimeout(function () { pl.dragged = false; }, 0);
    };
    host.addEventListener("pointerup", end);
    host.addEventListener("pointercancel", end);
  }

  // ------------------------------------------------------------------ tabs

  var TABS = ["overview", "ledger", "ventures", "plan", "library", "activity", "approvals", "inbox", "upgrades", "mind", "workspace", "system", "diagnostics"];
  var MIND_TABS = ["strategy", "playbook", "lessons", "identity", "journal", "reviews"];  // 0.30.0: the playbook
  var VENTURE_VIEWS = ["pipeline", "running"];  // 0.27.0

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
    if (name === "roadmap") name = "plan";  // 0.35.0: the Roadmap tab's goal and milestones are the Plan tab's
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
    if (name === "workspace") { ui.ws.visit = true; refreshWorkspace(); }
    if (name === "ventures") loadVentures();
    if (name === "plan") { loadPlan(); loadRoadmap(); }
    if (name === "library") loadLibrary();
    if (name === "inbox" && ui.chat.stick) scrollChatToEnd();  // it can't scroll while the tab is hidden
  }

  // 0.27.0: the Ventures tab's views (the Projects tab is its Running view), the one chosen last kept in this browser.
  function selectVentureView(name, focus) {
    if (VENTURE_VIEWS.indexOf(name) < 0) name = "pipeline";
    ui.vtView = name;
    savePref("ember-ventures-view", name);
    VENTURE_VIEWS.forEach(function (v) {
      var tab = $("vt-tab-" + v);
      var selected = v === name;
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
      $("vt-" + v).hidden = !selected;
    });
    if (focus) $("vt-tab-" + name).focus();
    // The tree's edges start where its labels end, which can't be measured while the view is hidden.
    var picture = $("vt-tree").querySelector(".vt-svg");
    if (name === "pipeline" && picture) fitEdges(picture);
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

  VENTURE_VIEWS.forEach(function (v, i) {
    var tab = $("vt-tab-" + v);
    tab.addEventListener("click", function () { selectVentureView(v, false); });
    tab.addEventListener("keydown", tabKeys(VENTURE_VIEWS, i, selectVentureView));
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
  initProjects();
  initVentures();
  initRoadmap();
  initPlan();
  initLibrary();
  initWorkspace();
  selectVentureView(ui.vtView, false);
  selectTab(ui.tab, false);
  selectMind(ui.mind, false);
  refresh();
})();
