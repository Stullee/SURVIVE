// Ember dashboard. Plain JS, no build step.
// Every URL is relative so it resolves against <base href>, i.e. through Home Assistant Ingress.
// All text from the API is inserted with textContent (never innerHTML).
"use strict";

(function () {
  var POLL_RUNNING_MS = 5000;
  var POLL_IDLE_MS = 30000;
  var POLL_ERROR_MS = 15000;
  // Neither Ingress hop times out on its own, so a hung backend would otherwise freeze the page.
  var REQUEST_TIMEOUT_MS = 10000;
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
    controlTimer: null,
  };

  // The phase-1 scenario switcher is gone; drop its stored choice.
  try { window.localStorage.removeItem("ember-scenario"); } catch (e) { /* storage unavailable */ }

  // ------------------------------------------------------------------ helpers

  function $(id) { return document.getElementById(id); }

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
    approved: { icon: "✓", label: "Approved, to do", tone: "accent", order: 1 },
    approved_with_changes: { icon: "✓", label: "Approved with changes, to do", tone: "accent", order: 1 },
    done: { icon: "✓", label: "Done", tone: "good", order: 2 },
    rejected: { icon: "✕", label: "Rejected", tone: "critical", order: 3 },
  };

  var UPGRADE_STATUS = {
    "new": { icon: "◔", label: "New", tone: "warning", order: 0 },
    accepted: { icon: "✓", label: "Accepted", tone: "accent", order: 1 },
    released: { icon: "✓", label: "Released", tone: "good", order: 2 },
    declined: { icon: "✕", label: "Declined", tone: "", order: 3 },
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

  function statusOrder(vocab, key) { var s = vocab[key]; return s ? s.order : 9; }

  function chip(vocab, key, fallbackLabel) {
    var s = vocab[key] || { icon: "", label: fallbackLabel || key, tone: "" };
    return h("span", { class: "chip", "data-tone": s.tone || null },
      h("span", { "aria-hidden": "true", text: s.icon }), s.label);
  }

  function plainChip(text) { return h("span", { class: "chip", text: text }); }

  function emptyState(tag, title, text) {
    return h(tag, { class: "empty-state" }, h("p", { class: "empty-title", text: title }), text ? h("p", { class: "muted", text: text }) : null);
  }

  // ------------------------------------------------------------------ requests

  function RequestError(kind, message, status) {
    this.kind = kind;          // timeout | network | http | malformed
    this.message = message;
    this.status = status || 0;
  }

  function request(method, url, body) {
    var controller = typeof AbortController === "function" ? new AbortController() : null;
    var timedOut = false;
    var timer = window.setTimeout(function () { timedOut = true; if (controller) controller.abort(); }, REQUEST_TIMEOUT_MS);
    var opts = { method: method, headers: { Accept: "application/json" }, cache: "no-store", credentials: "same-origin" };
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
      if (timedOut) throw new RequestError("timeout", "no answer within " + REQUEST_TIMEOUT_MS / 1000 + " seconds");
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
      ui.data = d;
      ui.fetchError = null;
      document.body.removeAttribute("data-stale");
      render();
      next = d.agent && d.agent.cycle_running ? POLL_RUNNING_MS : POLL_IDLE_MS;
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
      if (seq === ui.seq) schedule(next);
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
  };

  // True while the user has keyboard focus or selected text inside the element: rebuilding it would take them away.
  function isBusy(el) {
    if (!el) return false;
    var active = document.activeElement;
    if (active && active !== document.body && el.contains(active)) return true;
    var sel = window.getSelection ? window.getSelection() : null;
    if (sel && sel.rangeCount && !sel.isCollapsed) {
      var range = sel.getRangeAt(0);
      if (el.contains(range.commonAncestorContainer) || range.intersectsNode(el)) return true;
    }
    return false;
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
  function section(name, slice, guardIds, fn) {
    var key;
    try { key = JSON.stringify(slice); } catch (e) { key = null; }
    if (key !== null && ui.rendered[name] === key) return;
    if (guardIds && guardIds.some(function (id) { return isBusy($(id)); })) return;
    ui.rendered[name] = safely(name, fn) ? key : null;
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
    section("kpis", [d.agent, d.economy, d.mode, coming.now, minute], ["kpis"], function () { renderKpis(d, agent, coming); });
    safely("badges", function () { renderBadges(d, coming); });

    var dead = agent.state === "dead" && isObject(d.memorial);
    $("memorial").hidden = !dead;
    $("now-card").hidden = dead;
    if (dead) section("memorial", [d.memorial, agent.revive, agent.name], ["memorial"], function () { renderMemorial(d.memorial, agent); });
    else section("now", [d.now, coming.now, agent.state, agent.next_wake_at, !!d.agent, minute], ["now-card"], function () { renderNow(d.now, agent, coming.now); });
    section("lives", [d.lives, d.memorial && d.memorial.life_id], ["lives-card"], function () { renderLives(arr(d.lives), d.memorial); });
    section("charts", [economy.days], null, function () { renderCharts(economy); });
    if (Array.isArray(economy.days)) section("table", [economy.days, ui.tableOpen], ["economy-table"], function () { renderTable(economy); });

    section("ledger", [d.ledger, d.mode], ["ledger-list"], function () { renderLedger(d); });
    safely("forms", function () { updateForms(d, agent); });

    section("projects", [d.projects, coming.projects], ["projects"], function () { renderProjects(arr(d.projects), coming.projects); });
    section("activity", [d.activity, coming.activity, minute], ["activity"], function () { renderActivity(arr(d.activity), coming.activity); });
    section("approvals", [d.approvals, coming.approvals, minute], ["approvals"], function () { renderApprovals(arr(d.approvals), coming.approvals); });
    section("inbox", [d.inbox, coming.inbox, agent.name, minute], ["inbox"], function () { renderInbox(arr(d.inbox), agent.name, coming.inbox); });
    section("upgrades", [d.upgrades, coming.upgrades, minute], ["upgrades"], function () { renderUpgrades(arr(d.upgrades), coming.upgrades); });
    section("mind", [d.mind, coming.mind, ui.mind, minute], ["mind-body"], function () { renderMind(d.mind, coming.mind); });

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
    if (agent.state === "killed") {
      list.push({ kind: "warning", icon: "■", title: "The kill switch is on. " + name + " makes no model calls." });
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

  function renderKpis(d, a, coming) {
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
    else if (a.cycle_running) { wake.textContent = "Awake"; wakeSub.textContent = "Cycle started " + relTime(a.last_wake_at); }
    else if (a.next_wake_at) { wake.textContent = relTime(a.next_wake_at); wakeSub.textContent = fmtDateTime(a.next_wake_at); }
    else {
      wake.textContent = "Not scheduled yet";
      wake.setAttribute("data-size", "text");
      wakeSub.textContent = coming.now ? "Wake cycles arrive in phase " + coming.now : " ";
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

  function setBadge(id, count) {
    var el = $(id);
    el.hidden = !count;
    el.textContent = count ? String(count) : "";
    if (count) el.setAttribute("aria-label", count + " need attention");
    else el.removeAttribute("aria-label");
  }

  function renderBadges(d, coming) {
    setBadge("badge-approvals", coming.approvals ? 0 : arr(d.approvals).filter(function (x) {
      return x.status === "pending" || x.status === "approved" || x.status === "approved_with_changes";
    }).length);
    var inbox = coming.inbox ? [] : arr(d.inbox).slice().sort(byDate("created_at"));
    var lastOwner = -1;
    inbox.forEach(function (m, i) { if (m.from === "owner") lastOwner = i; });
    setBadge("badge-inbox", inbox.slice(lastOwner + 1).filter(function (m) { return m.from === "agent"; }).length);
    setBadge("badge-upgrades", coming.upgrades ? 0 : arr(d.upgrades).filter(function (x) { return x.status === "new"; }).length);
    var sys = d.system;
    setBadge("badge-system", arr(sys.config_errors).length + (isObject(sys.database) && sys.database.ok === false ? 1 : 0) + (sys.economy_broken ? 1 : 0));
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
    will.textContent = m.last_will ? m.last_will : "No last will was written.";
    will.setAttribute("data-empty", m.last_will ? "false" : "true");
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

  function renderNow(now, agent, comingPhase) {
    var hasNow = isObject(now);
    $("now-live").hidden = !(hasNow && now.running);
    $("now-grid").hidden = !hasNow;
    $("now-status").hidden = !hasNow;
    $("now-empty").hidden = hasNow;
    if (!hasNow) {
      replace($("now-empty"), comingPhase
        ? [h("p", { class: "empty-title", text: "Wake cycles arrive in phase " + comingPhase + "." }),
          h("p", { class: "muted", text: "Until then the agent doesn't run." + (agent.unavailable ? "" : " The money figures on this page are real.") })]
        : [h("p", { class: "empty-title", text: "No wake cycle has run yet." })]);
      return;
    }
    var status;
    if (now.running) {
      status = "Cycle #" + now.cycle_id + " · " + now.phase + " phase · step " + now.step + " of " + now.max_steps + " · started " + relTime(now.started_at);
    } else if (agent.paused) {
      status = "Paused by you. No model calls are made while paused.";
    } else {
      status = "Sleeping. Last cycle #" + now.cycle_id + " started " + relTime(now.started_at) +
        (agent.next_wake_at ? "; next wake " + relTime(agent.next_wake_at) + "." : ".");
    }
    $("now-status").textContent = status;
    $("now-plan").textContent = now.plan || "No plan yet.";
    $("now-budget").textContent = usd(now.spent_usd) + " of " + usd(now.cycle_cap_usd) + " cycle cap";
    setMeter($("now-meter"), now.spent_usd, now.cycle_cap_usd);
    $("now-action").textContent = now.current_action || (now.running ? "Thinking…" : "Nothing, asleep.");
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

  // ---- Projects, activity, approvals, inbox, upgrades, mind (real from phase 3/4 on)

  function renderProjects(projects, comingPhase) {
    var el = $("projects");
    if (comingPhase) {
      replace(el, emptyState("div", "Projects arrive in phase " + comingPhase + ".", "Once the agent runs, each project it tries shows up here with what it cost and what it earned."));
      return;
    }
    var sorted = projects.slice().sort(function (x, y) { return statusOrder(PROJECT_STATUS, x.status) - statusOrder(PROJECT_STATUS, y.status); });
    if (!sorted.length) { replace(el, emptyState("div", "No projects yet.")); return; }
    replace(el, sorted.map(function (p) {
      return h("article", { class: "card project" },
        chip(PROJECT_STATUS, p.status),
        h("h3", { text: p.title }),
        h("p", { class: "hypothesis", text: p.hypothesis }),
        h("div", { class: "money" },
          h("div", null, h("span", { text: "Spent" }), usd(p.spent_usd)),
          h("div", null, h("span", { text: "Earned" }), usd(p.earned_usd))),
        h("p", { class: "next" }, h("strong", { text: "Next: " }), p.next_step || "–"));
    }));
  }

  function renderActivity(cycles, comingPhase) {
    var el = $("activity");
    if (comingPhase) {
      replace(el, emptyState("li", "Activity arrives in phase " + comingPhase + ".", "Every wake cycle and each step in it will be listed here, with what it cost."));
      return;
    }
    if (!cycles.length) { replace(el, emptyState("li", "Nothing has happened yet.")); return; }
    var openIds = {};
    Array.prototype.forEach.call(el.querySelectorAll("details[open]"), function (x) { openIds[x.getAttribute("data-id")] = true; });
    replace(el, cycles.map(function (c) {
      var details = h("details", { "data-id": String(c.cycle_id), open: !!openIds[String(c.cycle_id)] },
        h("summary", null,
          h("strong", { text: "Cycle #" + c.cycle_id + (c.status === "running" ? " (running)" : "") }),
          h("span", { class: "cycle-cost", text: usd(c.cost_usd) }),
          h("span", { class: "cycle-meta" }, timeEl(c.started_at), " · ", c.summary)),
        h("ol", { class: "steps" }, arr(c.steps).map(function (s) {
          return h("li", null, h("span", { class: "kind", text: s.kind }), h("span", { text: s.summary }), h("span", { class: "cost", text: usd(s.cost_usd) }));
        })));
      return h("li", null, details);
    }));
  }

  var APPROVAL_TYPES = {
    publish: "Publish", contact: "Contact", create_account: "Create account",
    spend_money: "Spend money", sell: "Sell", other: "Other",
  };

  function renderApprovals(items, comingPhase) {
    var el = $("approvals");
    if (comingPhase) {
      replace(el, emptyState("div", "Approvals arrive in phase " + comingPhase + ".", "Before the agent publishes, contacts someone or spends money, it will ask you here."));
      return;
    }
    var sorted = items.slice().sort(function (x, y) {
      var o = statusOrder(APPROVAL_STATUS, x.status) - statusOrder(APPROVAL_STATUS, y.status);
      return o || new Date(y.created_at) - new Date(x.created_at);
    });
    if (!sorted.length) { replace(el, emptyState("div", "No requests.")); return; }
    replace(el, sorted.map(function (a) {
      var actions = null;
      if (a.status === "pending") {
        actions = h("div", { class: "item-actions" },
          h("button", { type: "button", class: "btn", disabled: true, title: "Available in phase 4", text: "Approve" }),
          h("button", { type: "button", class: "btn", disabled: true, title: "Available in phase 4", text: "Approve with changes" }),
          h("button", { type: "button", class: "btn", disabled: true, title: "Available in phase 4", text: "Reject" }));
      } else if (a.status === "approved" || a.status === "approved_with_changes") {
        actions = h("div", { class: "item-actions" },
          h("button", { type: "button", class: "btn", disabled: true, title: "Available in phase 4", text: "Mark done" }));
      }
      return h("article", { class: "card" },
        h("div", { class: "item-head" },
          h("h3", { text: a.title }), plainChip(APPROVAL_TYPES[a.type] || a.type), chip(APPROVAL_STATUS, a.status)),
        h("p", { text: a.description }),
        h("pre", { class: "payload", text: a.payload }),
        h("dl", { class: "item-grid" },
          h("div", null, h("dt", { text: "Expected cost" }), h("dd", { text: a.expected_cost })),
          h("div", null, h("dt", { text: "Expected benefit" }), h("dd", { text: a.expected_benefit }))),
        a.decision_comment || a.result_note ? h("div", { class: "decision" },
          a.decision_comment ? h("p", null, h("strong", { text: "Your comment: " }), a.decision_comment) : null,
          a.result_note ? h("p", null, h("strong", { text: "Result: " }), a.result_note) : null) : null,
        h("p", { class: "muted small" }, "Requested ", timeEl(a.created_at)),
        actions);
    }));
  }

  function renderInbox(messages, agentName, comingPhase) {
    var el = $("inbox");
    $("composer").hidden = !!comingPhase;
    if (comingPhase) {
      replace(el, emptyState("li", "The inbox arrives in phase " + comingPhase + ".", "You and the agent will be able to write to each other here."));
      return;
    }
    var sorted = messages.slice().sort(byDate("created_at"));
    if (!sorted.length) { replace(el, emptyState("li", "No messages yet.")); return; }
    replace(el, sorted.map(function (m) {
      return h("li", { "data-from": m.from },
        h("span", { class: "who" }, m.from === "owner" ? "You" : agentName, " · ", timeEl(m.created_at)),
        h("span", { text: m.text }));
    }));
  }

  function renderUpgrades(items, comingPhase) {
    var el = $("upgrades");
    if (comingPhase) {
      replace(el, emptyState("div", "Upgrade requests arrive in phase " + comingPhase + ".", "The agent will suggest changes to its own tools here, for you to accept or decline."));
      return;
    }
    var sorted = items.slice().sort(function (x, y) { return statusOrder(UPGRADE_STATUS, x.status) - statusOrder(UPGRADE_STATUS, y.status); });
    if (!sorted.length) { replace(el, emptyState("div", "No upgrade requests.")); return; }
    replace(el, sorted.map(function (u) {
      return h("article", { class: "card" },
        h("div", { class: "item-head" },
          h("h3", { text: u.title }), plainChip("Priority: " + u.priority),
          chip(UPGRADE_STATUS, u.status, u.status)),
        h("dl", { class: "item-grid" },
          h("div", null, h("dt", { text: "Problem" }), h("dd", { text: u.problem })),
          h("div", null, h("dt", { text: "Proposed change" }), h("dd", { text: u.proposed_change })),
          h("div", null, h("dt", { text: "Expected benefit" }), h("dd", { text: u.expected_benefit }))),
        u.decision_comment ? h("p", { class: "decision" }, h("strong", { text: "Your comment: " }), u.decision_comment) : null,
        h("p", { class: "muted small" }, "Requested ", timeEl(u.created_at)),
        u.status === "new" ? h("div", { class: "item-actions" },
          h("button", { type: "button", class: "btn", disabled: true, title: "Available in phase 4", text: "Accept" }),
          h("button", { type: "button", class: "btn", disabled: true, title: "Available in phase 4", text: "Decline" })) : null);
    }));
  }

  function renderMind(mind, comingPhase) {
    var empty = !!comingPhase || !isObject(mind);
    $("mind-tabs").hidden = empty;
    $("mind-note").hidden = empty;
    $("mind-body").hidden = empty;
    $("mind-empty").hidden = !empty;
    if (empty) {
      replace($("mind-empty"), comingPhase
        ? [h("p", { class: "empty-title", text: "The agent's notes arrive in phase " + comingPhase + "." }),
          h("p", { class: "muted", text: "Its strategy, lessons, identity and journal will be readable here." })]
        : h("p", { class: "empty-title", text: "No notes yet." }));
      return;
    }
    var body = $("mind-body");
    if (ui.mind === "journal") {
      var entries = arr(mind.journal);
      replace(body, entries.length ? h("ol", { class: "journal" }, entries.map(function (j) {
        return h("li", null, h("p", { class: "when" }, "Cycle #" + j.cycle_id + " · ", timeEl(j.created_at)), h("p", { text: j.summary }));
      })) : h("p", { class: "muted", text: "The journal is empty." }));
    } else {
      replace(body, h("pre", { text: mind[ui.mind] || "(empty)" }));
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
      f.flags[f.pendingFlag] = true;
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
    if (f.flags.confirm_state_change) body.confirm_state_change = true;
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

  function showConfirm(f, flag, message, danger) {
    f.pendingFlag = flag;
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
    if (data.state_after === "dead") {
      return "This would kill " + name + ": the balance afterwards (" + after + ") is too little to stay alive. Record it anyway?";
    }
    return "This changes " + name + "'s state from " + stateLabel(data.state_before) + " to " + stateLabel(data.state_after) +
      " (balance afterwards: " + after + "). Record it anyway?";
  }

  function handleFormResponse(f, res) {
    var data = isObject(res.data) ? res.data : {};
    if (res.status === 201 || res.status === 200) {
      onSaved(f, data.entry, res.status === 200 || data.replay === true);
    } else if (res.status === 409 && data.code === "would_change_state") {
      showConfirm(f, "confirm_state_change", stateChangeMessage(data), data.state_after === "dead");
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
      setControlStatus(resume ? "Resumed." : "Paused. No model calls are made until you resume.", false);
    }).catch(function (err) {
      setControlStatus((resume ? "Could not resume: " : "Could not pause: ") + errorText(err) + ".", true);
    }).then(function () {
      ui.controlBusy = false;
      btn.disabled = false;
      if (hadFocus && (!document.activeElement || document.activeElement === document.body)) btn.focus();
      refresh();
    });
  });

  // ------------------------------------------------------------------ tabs

  var TABS = ["overview", "ledger", "projects", "activity", "approvals", "inbox", "upgrades", "mind", "system"];
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
      var coming = isObject(ui.data.coming_in_phase) ? ui.data.coming_in_phase : {};
      ui.rendered.mind = null;
      section("mind", [ui.data.mind, coming.mind, ui.mind], null, function () { renderMind(ui.data.mind, coming.mind); });
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

  $("composer").addEventListener("submit", function (ev) { ev.preventDefault(); });

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
