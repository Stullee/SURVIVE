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
    diag: { text: null, loadedAt: null, busy: false },
    refocus: null,       // after the owner's own action: the card status line to focus once the list is re-rendered
    sending: false,      // an inbox message is on its way
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
  var PHASES = { plan: "Plan", act: "Act", reflect: "Reflect", last_will: "Last will" };
  var PURPOSES = { plan: "Plan", work: "Work", reflect: "Reflect", research: "Research", last_will: "Last will" };

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
    projects: "Projects", activity: "Activity", approvals: "Approvals", inbox: "Inbox", upgrades: "Upgrades", mind: "Mind",
    cycleDetail: "Cycle details", diagnostics: "Diagnostics",
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
    section("approvals", [d.approvals, projectTitles(d), coming, agent.name, minute], null, function () { return renderApprovals(arr(d.approvals), projectTitles(d)); });
    section("inbox", [d.inbox, d.badges, agent.name, coming, minute], ["inbox"], function () { renderInbox(arr(d.inbox), agent.name, badgeCounts(d).unread); });
    section("upgrades", [d.upgrades, coming, agent.name, minute], null, function () { return renderUpgrades(arr(d.upgrades)); });
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
    // economy_broken is also among the agent's warnings; it already has its own banner.
    var warnings = arr(agent.warnings).filter(function (w) { return !(sys.economy_broken && String(w).indexOf(String(sys.economy_broken)) >= 0); });
    if (warnings.length) {
      list.push({ kind: "warning", icon: "!", title: warnings.length === 1 ? String(warnings[0]) : "Please check:", items: warnings.length === 1 ? null : warnings });
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
      action.addEventListener("click", function () { openLedgerForm(spec.action.form); });
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
      $("kpi-runway-sub").textContent = a.runway_note || "at the last 7 days' spending";
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
    };
  }

  function renderBadges(d) {
    var c = badgeCounts(d);
    setBadge("badge-approvals", c.pending, "◔", "waiting for your decision");
    setBadge("badge-approvals-todo", c.todo, "☐", "approved, to carry out");
    setBadge("badge-inbox", c.unread, "●", "unread");
    setBadge("badge-upgrades", c.upgrades, "◔", "new");
    var sys = d.system;
    setBadge("badge-system", arr(sys.config_errors).length + (isObject(sys.database) && sys.database.ok === false ? 1 : 0) + (sys.economy_broken ? 1 : 0), "!", "need attention");
  }

  function isUnread(m) { return isObject(m) && m.sender === "agent" && !m.read_at; }

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
        label += ", step " + count(now.step) + " of " + count(now.max_steps);
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
    var steps = arr(plan.steps);
    if (steps.length) {
      parts.push(h("h4", { class: "small-head", text: "Steps" }),
        h("ol", { class: "plan-steps" }, steps.map(function (step) { return h("li", { class: "pre-line", text: asText(step) }); })));
    }
    var rest = Object.keys(plan).filter(function (k) {
      return ["goal", "plan", "assessment", "steps"].indexOf(k) < 0 && plan[k] !== null && plan[k] !== undefined && plan[k] !== "";
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

  function renderLedger(d) {
    var entries = arr(isObject(d.ledger) ? d.ledger.entries : null);
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
      meta.push(e.entered_by ? "by " + e.entered_by : e.created_by === "owner" ? "by you" : e.created_by === "system" ? "by the system" : "by " + e.created_by);
      if (e.llm_call_id !== null && e.llm_call_id !== undefined) meta.push("call #" + e.llm_call_id);
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
    var parts = [h("section", { class: "detail-part" }, h("h3", { text: "Plan" }),
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

  // An owner-entered link: a real link only after validation, opened in a new tab without a referrer.
  function ownerLink(value) {
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
    append(a, [text, h("span", { class: "visually-hidden", text: " (opens in a new tab)" })]);
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
      var sectionEl = h("section", { class: "queue-group", "data-group": g.key, "aria-label": g.title, hidden: true }, head, list);
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
      var group = opts.groups.filter(function (g) { return g.match(row.status); })[0] || opts.groups[opts.groups.length - 1];
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
      grp.head.textContent = g.title + " (" + cards.length + ")";
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
    var cancel = h("button", { type: "button", class: "btn btn-ghost", text: "Cancel" });
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

  var APPROVAL_GROUPS = [
    { key: "pending", title: "Waiting for your decision", match: function (s) { return s === "pending"; } },
    { key: "todo", title: "Approved, to carry out", match: function (s) { return s === "approved" || s === "approved_with_changes"; } },
    { key: "closed", title: "Closed", match: function () { return true; } },
  ];

  function renderApprovals(items, titles) {
    var sorted = items.slice().sort(function (x, y) {
      var o = statusOrder(APPROVAL_STATUS, x.status) - statusOrder(APPROVAL_STATUS, y.status);
      return o || (new Date(y.created_at).getTime() || 0) - (new Date(x.created_at).getTime() || 0) || num(y.id) - num(x.id);
    });
    return renderQueue($("approvals"), {
      kind: "approval", rows: sorted, groups: APPROVAL_GROUPS, viewKey: titles,
      empty: emptyState("div", "No requests.", "Before the agent publishes, contacts someone or spends money, it asks you here."),
      view: function (a) { return approvalView(a, titles); },
      actionKey: function (a) { return a.status + "|" + a.version; },
      actions: approvalActions,
      panel: approvalPanel,
    });
  }

  function approvalView(a, titles) {
    var todo = a.status === "approved" || a.status === "approved_with_changes";
    var project = a.project_id !== null && a.project_id !== undefined
      ? (titles[String(a.project_id)] ? titles[String(a.project_id)] + " (#" + a.project_id + ")" : "#" + a.project_id) : null;
    var payload = asText(a.payload);
    var final = a.final_payload ? asText(a.final_payload) : "";
    return [
      h("div", { class: "item-head" },
        h("h3", { text: a.title || "Untitled request" }), plainChip(APPROVAL_TYPES[a.type] || sentence(String(a.type || "other").replace(/_/g, " "))),
        chip(APPROVAL_STATUS, a.status, sentence(String(a.status || "unknown").replace(/_/g, " "))), a.simulated ? testTag() : null),
      a.description ? h("p", { class: "pre-line", text: String(a.description) }) : null,
      todo ? h("p", { class: "todo-note" }, h("span", { "aria-hidden": "true", text: "☐ " }), "You approved this; carry it out, then mark it done or failed.") : null,
      final ? h("div", { class: "final-wrap" },
        h("h4", { class: "small-head", text: "Your version (the agent must use this)" }),
        h("pre", { class: "payload final capped", tabindex: "0", text: final })) : null,
      payload ? h("div", { class: "payload-wrap" },
        final ? h("h4", { class: "small-head", text: "The agent's original" }) : null,
        h("p", { class: "payload-note" }, h("span", { "aria-hidden": "true", text: "! " }), "Written by the agent; check it before acting."),
        h("pre", { class: "payload capped", tabindex: "0", text: payload })) : null,
      h("dl", { class: "item-grid" },
        h("div", null, h("dt", { text: "Expected cost" }), h("dd", { text: asText(a.expected_cost) || "–" })),
        h("div", null, h("dt", { text: "Expected benefit" }), h("dd", { text: asText(a.expected_benefit) || "–" })),
        project ? h("div", null, h("dt", { text: "Project" }), h("dd", { text: project })) : null),
      decisionInfo(a),
      h("p", { class: "muted small" }, "Requested ", timeEl(a.created_at), " · #" + a.id),
    ];
  }

  function decisionInfo(a) {
    var parts = [];
    if (a.decided_at) {
      var verb = a.status === "rejected" ? "Rejected" : a.final_payload ? "Approved with changes" : "Approved";
      parts.push(h("p", null, h("strong", { text: verb }), " by " + (a.decided_by || "you") + " · ", timeEl(a.decided_at)));
      if (a.decision_comment) parts.push(h("p", { class: "pre-line" }, h("strong", { text: "Comment: " }), String(a.decision_comment)));
    }
    if (a.closed_at) {
      parts.push(h("p", null, h("strong", { text: a.status === "failed" ? "Marked failed" : "Marked done" }), " · ", timeEl(a.closed_at)));
      if (a.result_note) parts.push(h("p", { class: "pre-line" }, h("strong", { text: "Result: " }), String(a.result_note)));
      if (a.result_link) parts.push(h("p", { class: "result-line" }, h("strong", { text: "Link: " }), ownerLink(a.result_link)));
    }
    if (!parts.length) return null;
    parts.push(seenLine(!!a.seen_by_agent));
    return h("div", { class: "decision" }, parts);
  }

  function approvalActions(it, a) {
    if (a.status === "pending") {
      return [panelButton(it, "approve", "Approve"), panelButton(it, "approve_with_changes", "Approve with changes"), panelButton(it, "reject", "Reject", true)];
    }
    if (a.status === "approved" || a.status === "approved_with_changes") {
      return [panelButton(it, "done", "Mark done"), panelButton(it, "failed", "Mark failed")];
    }
    return [];
  }

  var COMMENT_FIELD = { name: "comment", label: "Comment for the agent (optional)", rows: 2, max: 2000 };

  function approvalPanel(it, mode) {
    var a = it.row;
    var version = a.version;
    var url = "api/approvals/" + encodeURIComponent(String(a.id)) + "/";
    var name = agentName();
    if (mode === "approve" || mode === "approve_with_changes" || mode === "reject") {
      var original = asText(a.payload);
      var specs = {
        approve: { title: "Approve this request", submit: "Approve",
          intro: [h("p", { text: "After approving, carry it out yourself, then mark it done or failed here. " + name + " sees your decision on its next wake." })],
          fields: [COMMENT_FIELD] },
        approve_with_changes: { title: "Approve with your changes", submit: "Approve with changes",
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
        if (mode === "reject") return "Rejected. " + name + " sees this on its next wake.";
        if (mode === "approve_with_changes" && st === "approved") return "Approved as it was (the text was unchanged). Carry it out, then mark it done or failed.";
        if (mode === "approve_with_changes") return "Approved with your changes. Carry it out with your version, then mark it done or failed.";
        return "Approved. Carry it out, then mark it done or failed.";
      };
      return spec;
    }
    // Close: done or failed
    var failed = mode === "failed";
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
        { name: "result_link", label: "Link to the result (optional)", inputmode: "url", check: linkProblem,
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
      done: function () { return (failed ? "Marked failed. " : "Marked done. ") + name + " sees this on its next wake."; },
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
      h("p", { class: "muted small" }, "Requested ", timeEl(u.created_at), " · #" + u.id),
    ];
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

  // ---- Inbox

  function renderInbox(messages, agentName, unread) {
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
          h("span", { class: "msg-text", text: asText(m.text) }),
          fromOwner ? seenLine(!!m.seen_by_agent) : null);
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
    ui.sending = true;
    var send = $("composer-send");
    send.disabled = true;
    send.textContent = "Sending…";
    setComposerStatus("Sending…", "");
    request("POST", "api/inbox", { text: text }).then(function (res) {
      if (res.status === 201 || res.ok) {
        box.value = "";
        composerCount();
        setComposerStatus("Sent. " + agentName() + " reads it on its next wake.", "ok");
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
    if (ui.mind === "journal") {
      var entries = arr(mind.journal).slice().sort(function (x, y) { return (new Date(y.created_at).getTime() || 0) - (new Date(x.created_at).getTime() || 0); });
      replace(body, entries.length ? h("ol", { class: "journal" }, entries.map(function (j) {
        var entry = asText(j.entry);
        return h("li", null,
          h("p", { class: "when" }, j.cycle_id !== null && j.cycle_id !== undefined ? "Cycle #" + j.cycle_id + " · " : "", timeEl(j.created_at),
            j.author === "system" ? " · written by the system" : ""),
          j.summary ? h("p", { class: "journal-summary", text: String(j.summary) }) : null,
          entry && entry !== j.summary ? h("pre", { class: "journal-entry", text: entry }) : null);
      })) : h("p", { class: "muted", text: "The journal is empty." }));
    } else {
      // Markdown written by the agent, shown as it is (never rendered).
      var text = asText(mind[ui.mind]);
      replace(body, text.trim() ? h("pre", { class: "mind-text", text: text }) : h("p", { class: "muted", text: MIND_EMPTY[ui.mind] || "Nothing written yet." }));
    }
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
      h("dt", { text: "API key" }), h("dd", { text: options.anthropic_api_key_set ? "Set (hidden)" : "Not set" }),
      h("dt", { text: "Database" }), h("dd", { text: db.ok ? "OK, schema version " + db.schema_version : "Error: " + db.error }),
      h("dt", { text: "Economy" }), h("dd", { text: s.economy_error ? "Not available: " + s.economy_error
        : s.economy_broken ? "Spending stopped until the app restarts: " + s.economy_broken : d.agent ? "OK" : "Not available" }),
      h("dt", { text: "Sensor URL" }), h("dd", null, h("code", { text: s.sensor_url }),
        h("span", { class: "muted small", text: " for a Home Assistant REST sensor (see the app's Documentation tab)" })),
    ]);
    replace($("options"), optionsTable(options));
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
      if (kind === "amount" || kind === "note" || kind === "text") field.control.value = "";
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

  function openLedgerForm(key, amount) {
    selectTab("ledger", false);
    openForm(key, true, amount);
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

  // ------------------------------------------------------------------ diagnostics

  function setDiagStatus(text, kind) {
    var el = $("diag-status");
    el.textContent = text;
    el.setAttribute("data-kind", kind || "");
  }

  function diagnosticsFileName(when) {
    var d = when || new Date();
    var two = function (n) { return String(n).padStart(2, "0"); };
    return "ember-diagnostics-" + d.getFullYear() + "-" + two(d.getMonth() + 1) + "-" + two(d.getDate()) +
      "-" + two(d.getHours()) + two(d.getMinutes()) + two(d.getSeconds()) + ".txt";
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
    replace($("diag-meta"), [byteSize(reportBytes(diag.text)) + " · " + plural(lines, "line") + " · ",
      timeEl(diag.loadedAt.toISOString(), "loaded at " + timeFmt.format(diag.loadedAt))]);
  }

  function loadDiagnostics() {
    var diag = ui.diag;
    if (diag.busy) return;
    diag.busy = true;
    setDiagStatus("Loading the report…", "");
    safely("diagnostics", showDiagnostics);
    request("GET", "api/diagnostics", null, { accept: "text/plain", timeout: DIAGNOSTICS_TIMEOUT_MS }).then(function (res) {
      if (!res.ok) throw httpError(res);
      if (typeof res.text !== "string" || !res.text.trim()) throw new RequestError("malformed", "the report is empty");
      diag.text = res.text;
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

  // The Clipboard API needs a secure context, and Home Assistant is often opened over plain http.
  function copyDiagnostics() {
    var text = ui.diag.text;
    if (typeof text !== "string") return;
    var size = byteSize(reportBytes(text));
    var done = function () { setDiagStatus("Copied the report (" + size + ") to the clipboard.", "ok"); };
    if (window.isSecureContext && navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      navigator.clipboard.writeText(text).then(done, function () { copyBySelection(done); });
    } else {
      copyBySelection(done);
    }
  }

  function copyBySelection(done) {
    var pre = $("diag-report");
    var sel = window.getSelection ? window.getSelection() : null;
    if (!sel || !document.createRange) {
      setDiagStatus("This browser can't copy for you. Select the report below and copy it with Ctrl+C (Cmd+C on a Mac).", "error");
      return;
    }
    var range = document.createRange();
    range.selectNodeContents(pre);
    sel.removeAllRanges();
    sel.addRange(range);
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    if (ok) {
      sel.removeAllRanges();
      done();
    } else {
      setDiagStatus("The browser didn't allow copying. The whole report is selected: press Ctrl+C (Cmd+C on a Mac) to copy it.", "error");
    }
  }

  function downloadDiagnostics() {
    var text = ui.diag.text;
    if (typeof text !== "string") return;
    var name = diagnosticsFileName(ui.diag.loadedAt);
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

  // ------------------------------------------------------------------ tabs

  var TABS = ["overview", "ledger", "projects", "activity", "approvals", "inbox", "upgrades", "mind", "system", "diagnostics"];
  var MIND_TABS = ["strategy", "lessons", "identity", "journal"];

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
  selectTab(ui.tab, false);
  selectMind(ui.mind, false);
  refresh();
})();
