// Ember dashboard. Plain JS, no build step.
// Every URL is relative so it resolves against <base href>, i.e. through Home Assistant Ingress.
// All text from the API is inserted with textContent (never innerHTML).
"use strict";

(function () {
  var POLL_RUNNING_MS = 5000;
  var POLL_IDLE_MS = 30000;
  var POLL_ERROR_MS = 15000;

  var ui = {
    data: null,
    scenario: loadPref("ember-scenario", "alive"),
    tab: loadPref("ember-tab", "overview"),
    mind: "strategy",
    tableOpen: false,
    timer: null,
    fetchError: null,
    renderError: null,
    days: null,
    charts: { flow: null, balance: null },
    hiddenSeries: {},
  };

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

  // ------------------------------------------------------------------ formatting

  var usdSmall = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 4 });
  var usdLarge = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
  var usdTick = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 2 });
  var pctFmt = new Intl.NumberFormat(undefined, { style: "percent", maximumFractionDigits: 1 });
  var dayFmt = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });
  var dayLongFmt = new Intl.DateTimeFormat(undefined, { weekday: "short", month: "short", day: "numeric", year: "numeric" });
  var dateTimeFmt = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
  var timeFmt = new Intl.DateTimeFormat(undefined, { timeStyle: "medium" });
  var relFmt = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });

  function usd(value) {
    if (value === null || value === undefined || isNaN(value)) return "–";
    return Math.abs(value) < 1 && value !== 0 ? usdSmall.format(value) : usdLarge.format(value);
  }

  function pct(value) { return value === null || value === undefined ? "–" : pctFmt.format(value); }

  function parseDay(iso) {
    // "2026-09-27" as a local calendar day (not UTC midnight).
    var p = iso.split("-");
    return new Date(Number(p[0]), Number(p[1]) - 1, Number(p[2]));
  }

  function fmtDateTime(iso) { return iso ? dateTimeFmt.format(new Date(iso)) : "–"; }

  function relTime(iso) {
    if (!iso) return "–";
    var seconds = (new Date(iso).getTime() - Date.now()) / 1000;
    var abs = Math.abs(seconds);
    if (abs < 60) return relFmt.format(Math.round(seconds), "second");
    if (abs < 3600) return relFmt.format(Math.round(seconds / 60), "minute");
    if (abs < 86400 * 2) {
      var hours = Math.trunc(seconds / 3600);
      var minutes = Math.round((abs % 3600) / 60);
      if (hours === 0) return relFmt.format(Math.round(seconds / 60), "minute");
      var text = Math.abs(hours) + " h" + (minutes ? " " + minutes + " min" : "");
      return seconds > 0 ? "in " + text : text + " ago";
    }
    return relFmt.format(Math.round(seconds / 86400), "day");
  }

  function timeEl(iso, text) {
    return h("time", { datetime: iso, title: fmtDateTime(iso), text: text || relTime(iso) });
  }

  // ------------------------------------------------------------------ status vocabularies (icon + label, never color alone)

  var LIFE_STATES = {
    alive: { icon: "●", label: "Alive" },
    paused: { icon: "❚❚", label: "Paused" },
    critical: { icon: "▲", label: "Critical" },
    dead: { icon: "✝", label: "Dead" },
    unknown: { icon: "…", label: "Unknown" },
  };

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

  function chip(vocab, key, fallbackLabel) {
    var s = vocab[key] || { icon: "", label: fallbackLabel || key, tone: "" };
    return h("span", { class: "chip", "data-tone": s.tone || null },
      h("span", { "aria-hidden": "true", text: s.icon }), s.label);
  }

  function plainChip(text) { return h("span", { class: "chip", text: text }); }

  // ------------------------------------------------------------------ data loading

  function schedule(ms) {
    window.clearTimeout(ui.timer);
    ui.timer = window.setTimeout(refresh, ms);
  }

  function refresh() {
    var url = "api/dashboard?scenario=" + encodeURIComponent(ui.scenario);
    return fetch(url, { headers: { Accept: "application/json" }, cache: "no-store", credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (data) {
        ui.data = data;
        ui.fetchError = null;
        document.body.removeAttribute("data-stale");
        try {
          ui.renderError = null;
          render();
        } catch (err) {
          // A display bug must not look like a connection problem.
          ui.renderError = String(err && err.message ? err.message : err);
          console.error(err);
          renderBanners();
        }
        schedule(data.agent && data.agent.cycle_running ? POLL_RUNNING_MS : POLL_IDLE_MS);
      }, function (err) {
        ui.fetchError = String(err && err.message ? err.message : err);
        document.body.setAttribute("data-stale", "true");
        renderBanners();
        schedule(POLL_ERROR_MS);
      });
  }

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") refresh();
    else window.clearTimeout(ui.timer);
  });

  // ------------------------------------------------------------------ rendering

  function render() {
    var d = ui.data;
    if (!d) return;
    renderHeader(d);
    renderBanners();
    renderKpis(d);
    renderBadges(d);
    renderOverview(d);
    renderProjects(d.projects || []);
    renderActivity(d.activity || []);
    renderApprovals(d.approvals || []);
    renderInbox(d.inbox || [], d.agent.name);
    renderUpgrades(d.upgrades || []);
    renderMind(d.mind || {});
    renderSystem(d);
    $("updated").textContent = "Updated " + timeFmt.format(new Date());
  }

  function renderHeader(d) {
    var agent = d.agent;
    document.title = agent.name + " · " + (LIFE_STATES[agent.state] || LIFE_STATES.unknown).label;
    $("agent-name").textContent = agent.name;
    var pill = $("state-pill");
    var st = LIFE_STATES[agent.state] || LIFE_STATES.unknown;
    pill.setAttribute("data-state", agent.state in LIFE_STATES ? agent.state : "unknown");
    pill.querySelector(".state-icon").textContent = st.icon;
    pill.querySelector(".state-label").textContent = st.label;
    var parts = ["Age " + agent.age_days + (agent.age_days === 1 ? " day" : " days"), versionLabel(d.system.version)];
    if (d.system.dry_run) parts.push("Dry run");
    $("identity-meta").textContent = parts.join(" · ");
    $("footer-version").textContent = "Ember " + versionLabel(d.system.version) + (d.mock ? " · preview data" : "");
  }

  function versionLabel(version) { return /^\d/.test(version) ? "v" + version : version; }

  function banner(kind, icon, title, items) {
    return h("div", { class: "banner", "data-kind": kind, role: kind === "error" ? "alert" : null },
      h("span", { class: "banner-icon", "aria-hidden": "true", text: icon }),
      h("div", null, h("strong", { text: title }),
        items && items.length ? h("ul", null, items.map(function (t) { return h("li", { text: t }); })) : null));
  }

  function renderBanners() {
    var list = [];
    var d = ui.data;
    if (ui.fetchError) {
      list.push(banner("warning", "!", "Can't reach Ember right now (" + ui.fetchError + "). Retrying; the numbers below may be out of date."));
    }
    if (ui.renderError) {
      list.push(banner("error", "✕", "Part of the dashboard could not be displayed (" + ui.renderError + "). Please report this."));
    }
    if (d) {
      var sys = d.system;
      if (sys.database && !sys.database.ok) {
        list.push(banner("error", "✕", "The database could not be opened. The agent will not run until this is fixed.", [sys.database.error]));
      }
      if (sys.config_errors && sys.config_errors.length) {
        list.push(banner("error", "✕", "Invalid app options. Running in safe mode (built-in defaults, dry run on). Fix them in the app's Configuration tab.", sys.config_errors));
      }
      if (d.mock) {
        list.push(banner("info", "i", "Preview data. Phase 1 shows made-up numbers so you can review the layout. Nothing here is real and no money is being spent."));
      }
      if (d.agent.state === "critical") {
        list.push(banner("warning", "▲", "Runway is under 2 days. The agent is writing its last will."));
      }
    }
    replace($("banners"), list);
  }

  function renderKpis(d) {
    var a = d.agent;
    var e = d.economy;
    $("kpi-balance").textContent = usd(a.balance_usd);
    $("kpi-balance-sub").textContent = usd(e.totals.grant_usd) + " granted · " + usd(e.totals.revenue_usd) + " earned";

    var runway = $("kpi-runway");
    runway.textContent = a.runway_days === null || a.runway_days === undefined ? "–" : a.runway_days + (a.runway_days === 1 ? " day" : " days");
    runway.setAttribute("data-tone", a.runway_days !== null && a.runway_days < 2 ? "critical" : "");

    $("kpi-today").textContent = usd(a.today_spend_usd);
    setMeter($("kpi-today-meter"), a.daily_cap_usd ? a.today_spend_usd / a.daily_cap_usd : 0);
    $("kpi-today-sub").textContent = "of " + usd(a.daily_cap_usd) + " daily cap";

    $("kpi-ratio").textContent = pct(e.self_sufficiency_ratio);

    var wake = $("kpi-wake");
    var wakeSub = $("kpi-wake-sub");
    if (a.state === "dead") { wake.textContent = "Never"; wakeSub.textContent = "The agent has died"; }
    else if (a.state === "paused") { wake.textContent = "Paused"; wakeSub.textContent = "Resume to schedule the next cycle"; }
    else if (a.cycle_running) { wake.textContent = "Awake"; wakeSub.textContent = "Cycle started " + relTime(a.last_wake_at); }
    else { wake.textContent = relTime(a.next_wake_at); wakeSub.textContent = fmtDateTime(a.next_wake_at); }
  }

  function setMeter(meter, ratio) {
    var value = Math.max(0, ratio || 0);
    meter.querySelector(".meter-fill").style.width = Math.min(100, value * 100).toFixed(1) + "%";
    meter.setAttribute("aria-valuenow", String(Math.round(value * 100)));
    meter.setAttribute("data-level", value >= 1 ? "critical" : value >= 0.75 ? "warning" : "normal");
  }

  function setBadge(id, count) {
    var el = $(id);
    el.hidden = !count;
    el.textContent = count ? String(count) : "";
    el.setAttribute("aria-label", count ? count + " need attention" : "");
  }

  function renderBadges(d) {
    setBadge("badge-approvals", (d.approvals || []).filter(function (x) { return x.status === "pending" || x.status === "approved" || x.status === "approved_with_changes"; }).length);
    var inbox = (d.inbox || []).slice().sort(byDate("created_at"));
    var lastOwner = -1;
    inbox.forEach(function (m, i) { if (m.from === "owner") lastOwner = i; });
    setBadge("badge-inbox", inbox.slice(lastOwner + 1).filter(function (m) { return m.from === "agent"; }).length);
    setBadge("badge-upgrades", (d.upgrades || []).filter(function (x) { return x.status === "new"; }).length);
    var problems = (d.system.config_errors || []).length + (d.system.database && !d.system.database.ok ? 1 : 0);
    setBadge("badge-system", problems);
  }

  function byDate(key) {
    return function (x, y) { return new Date(x[key]).getTime() - new Date(y[key]).getTime(); };
  }

  // ---- Overview: memorial, now, charts

  function renderOverview(d) {
    var dead = d.agent.state === "dead" && d.memorial;
    $("memorial").hidden = !dead;
    $("now-card").hidden = !!dead;
    if (dead) renderMemorial(d.memorial);
    else renderNow(d.now, d.agent);
    renderCharts(d.economy);
    renderTable(d.economy);
  }

  function renderMemorial(m) {
    $("memorial-name").textContent = m.name;
    $("memorial-dates").textContent = fmtDateTime(m.born_at) + " – " + fmtDateTime(m.died_at);
    replace($("memorial-facts"), [
      h("dt", { text: "Lived" }), h("dd", { text: m.lifespan_days + " days, " + m.cycles + " wake cycles" }),
      h("dt", { text: "Spent" }), h("dd", { text: usd(m.total_cost_usd) }),
      h("dt", { text: "Earned" }), h("dd", { text: usd(m.total_revenue_usd) }),
    ]);
    $("memorial-will").textContent = m.last_will;
  }

  function renderNow(now, agent) {
    $("now-live").hidden = !now.running;
    var status;
    if (now.running) {
      status = "Cycle #" + now.cycle_id + " · " + now.phase + " phase · step " + now.step + " of " + now.max_steps + " · started " + relTime(now.started_at);
    } else if (agent.state === "paused") {
      status = "Paused by you. No model calls are made while paused.";
    } else {
      status = "Sleeping. Last cycle #" + now.cycle_id + " started " + relTime(now.started_at) + "; next wake " + relTime(agent.next_wake_at) + ".";
    }
    $("now-status").textContent = status;
    $("now-plan").textContent = now.plan || "No plan yet.";
    $("now-budget").textContent = usd(now.spent_usd) + " of " + usd(now.cycle_cap_usd) + " cycle cap";
    setMeter($("now-meter"), now.cycle_cap_usd ? now.spent_usd / now.cycle_cap_usd : 0);
    $("now-action").textContent = now.current_action || (now.running ? "Thinking…" : "Nothing, asleep.");
  }

  var FLOW_SERIES = [
    { key: "revenue_usd", label: "Revenue", color: "--series-1", stack: "in" },
    { key: "api_cost_usd", label: "API cost", color: "--series-2", stack: "out" },
    { key: "expense_usd", label: "Expenses", color: "--series-3", stack: "out" },
  ];

  function renderCharts(economy) {
    var boxes = [$("flow-chart"), $("balance-chart")];
    if (typeof window.Chart === "undefined") {
      boxes.forEach(function (c) { replace(c.parentNode, h("p", { class: "muted", text: "Chart library failed to load; use the table view." })); });
      return;
    }
    var days = economy.days;
    // Scriptable options and tooltips read the days from here (they can run while a chart is being built).
    ui.days = days;
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

  // Draws sparse direct labels: the latest balance and each owner grant.
  var directLabels = {
    id: "emberDirectLabels",
    afterDatasetsDraw: function (chart) {
      if (chart.canvas.id !== "balance-chart" || !ui.days) return;
      var meta = chart.getDatasetMeta(0);
      if (!meta || meta.hidden) return;
      var ctx = chart.ctx;
      var t = chartTheme();
      ctx.save();
      ctx.font = "12px system-ui, -apple-system, 'Segoe UI', sans-serif";
      ctx.fillStyle = t.text2;
      ctx.textBaseline = "bottom";
      ui.days.forEach(function (day, i) {
        var point = meta.data[i];
        if (!point) return;
        var isLast = i === ui.days.length - 1;
        var text = null;
        if (day.grant_usd > 0) text = "+" + usd(day.grant_usd) + " grant";
        if (isLast) text = (text ? text + " · " : "") + usd(day.balance_usd);
        if (!text) return;
        // Beside the marker (right of it, or left near the right edge), never on top of it.
        var width = ctx.measureText(text).width;
        var x = point.x + 10;
        if (x + width > chart.chartArea.right) x = point.x - 10 - width;
        var y = Math.max(point.y - 6, chart.chartArea.top + 12);
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
              title: function (items) { return dayLongFmt.format(parseDay(ui.days[items[0].dataIndex].date)); },
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
              title: function (items) { return dayLongFmt.format(parseDay(ui.days[items[0].dataIndex].date)); },
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
      var existing = window.Chart && window.Chart.getChart($(id));
      if (existing) existing.destroy();
    });
    ui.charts.flow = ui.charts.balance = null;
  }

  function renderTable(economy) {
    var wrap = $("economy-table");
    wrap.hidden = !ui.tableOpen;
    if (!ui.tableOpen) return;
    var cols = [
      ["api_cost_usd", "API cost"], ["expense_usd", "Expenses"], ["revenue_usd", "Revenue"],
      ["grant_usd", "Owner grants"], ["balance_usd", "Balance"],
    ];
    var rows = economy.days.slice().reverse().map(function (d) {
      return h("tr", null, h("td", { text: dayLongFmt.format(parseDay(d.date)) }),
        cols.map(function (c) { return h("td", { class: "num", text: usd(d[c[0]]) }); }));
    });
    replace(wrap, h("table", null,
      h("caption", { class: "visually-hidden", text: "Daily money flows, newest first" }),
      h("thead", null, h("tr", null, h("th", { scope: "col", text: "Day" }),
        cols.map(function (c) { return h("th", { scope: "col", class: "num", text: c[1] }); }))),
      h("tbody", null, rows)));
  }

  // ---- Projects

  function renderProjects(projects) {
    var sorted = projects.slice().sort(function (x, y) {
      return ((PROJECT_STATUS[x.status] || {}).order || 9) - ((PROJECT_STATUS[y.status] || {}).order || 9);
    });
    if (!sorted.length) { replace($("projects"), h("p", { class: "muted", text: "No projects yet." })); return; }
    replace($("projects"), sorted.map(function (p) {
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

  // ---- Activity

  function renderActivity(cycles) {
    if (!cycles.length) { replace($("activity"), h("li", { class: "muted", text: "Nothing has happened yet." })); return; }
    var openIds = {};
    Array.prototype.forEach.call($("activity").querySelectorAll("details[open]"), function (el) { openIds[el.getAttribute("data-id")] = true; });
    replace($("activity"), cycles.map(function (c) {
      var details = h("details", { "data-id": String(c.cycle_id), open: !!openIds[String(c.cycle_id)] },
        h("summary", null,
          h("strong", { text: "Cycle #" + c.cycle_id + (c.status === "running" ? " (running)" : "") }),
          h("span", { class: "cycle-cost", text: usd(c.cost_usd) }),
          h("span", { class: "cycle-meta" }, timeEl(c.started_at), " · ", c.summary)),
        h("ol", { class: "steps" }, (c.steps || []).map(function (s) {
          return h("li", null, h("span", { class: "kind", text: s.kind }), h("span", { text: s.summary }), h("span", { class: "cost", text: usd(s.cost_usd) }));
        })));
      return h("li", null, details);
    }));
  }

  // ---- Approvals

  var APPROVAL_TYPES = {
    publish: "Publish", contact: "Contact", create_account: "Create account",
    spend_money: "Spend money", sell: "Sell", other: "Other",
  };

  function renderApprovals(items) {
    var sorted = items.slice().sort(function (x, y) {
      var o = (APPROVAL_STATUS[x.status] || {}).order - (APPROVAL_STATUS[y.status] || {}).order;
      return o || new Date(y.created_at) - new Date(x.created_at);
    });
    if (!sorted.length) { replace($("approvals"), h("p", { class: "muted", text: "No requests." })); return; }
    replace($("approvals"), sorted.map(function (a) {
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

  // ---- Inbox

  function renderInbox(messages, agentName) {
    var sorted = messages.slice().sort(byDate("created_at"));
    if (!sorted.length) { replace($("inbox"), h("li", { class: "muted", text: "No messages yet." })); return; }
    replace($("inbox"), sorted.map(function (m) {
      return h("li", { "data-from": m.from },
        h("span", { class: "who" }, m.from === "owner" ? "You" : agentName, " · ", timeEl(m.created_at)),
        h("span", { text: m.text }));
    }));
  }

  // ---- Upgrades

  function renderUpgrades(items) {
    var sorted = items.slice().sort(function (x, y) {
      return (UPGRADE_STATUS[x.status] || {}).order - (UPGRADE_STATUS[y.status] || {}).order;
    });
    if (!sorted.length) { replace($("upgrades"), h("p", { class: "muted", text: "No upgrade requests." })); return; }
    replace($("upgrades"), sorted.map(function (u) {
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

  // ---- Mind

  function renderMind(mind) {
    Array.prototype.forEach.call(document.querySelectorAll(".subtab"), function (b) {
      b.setAttribute("aria-selected", String(b.getAttribute("data-mind") === ui.mind));
    });
    var body = $("mind-body");
    if (ui.mind === "journal") {
      var entries = (mind.journal || []);
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
    $("preview-card").hidden = !d.mock;
    Array.prototype.forEach.call(document.querySelectorAll("[data-scenario]"), function (b) {
      b.setAttribute("aria-pressed", String(b.getAttribute("data-scenario") === d.scenario));
    });
    var db = s.database || {};
    replace($("system-facts"), [
      h("dt", { text: "Version" }), h("dd", { text: s.version }),
      h("dt", { text: "Started" }), h("dd", null, timeEl(s.started_at, fmtDateTime(s.started_at))),
      h("dt", { text: "First started" }), h("dd", { text: s.born_at ? fmtDateTime(s.born_at) : "–" }),
      h("dt", { text: "Mode" }), h("dd", { text: (s.dry_run ? "Dry run (fake model, no API calls)" : "Live (real API calls)") + (s.dev_mode ? " · local development" : "") }),
      h("dt", { text: "Options" }), h("dd", { text: s.config_source + (s.safe_mode ? " · SAFE MODE" : "") }),
      h("dt", { text: "API key" }), h("dd", { text: s.options.anthropic_api_key_set ? "Set (hidden)" : "Not set" }),
      h("dt", { text: "Database" }), h("dd", { text: db.ok ? "OK, schema version " + db.schema_version : "Error: " + db.error }),
      h("dt", { text: "Sensor URL" }), h("dd", null, h("code", { text: s.sensor_url }),
        h("span", { class: "muted small", text: " for a Home Assistant REST sensor (see the app's Documentation tab)" })),
    ]);
    var events = d.events || [];
    replace($("events"), events.length ? events.map(function (e) {
      return h("li", null,
        h("span", { class: "when" }, timeEl(e.ts, fmtDateTime(e.ts))),
        h("span", { class: "level", "data-level": e.level, text: e.level }),
        h("span", { class: "msg", text: e.message }));
    }) : h("li", { class: "muted", text: "No events." }));
    replace($("options"), optionsTable(s.options));
  }

  function optionsTable(options) {
    var rows = Object.keys(options).filter(function (k) { return k !== "price_table"; }).map(function (k) {
      return h("tr", null, h("th", { scope: "row", text: k }), h("td", { text: String(options[k]) }));
    });
    var prices = (options.price_table || []).map(function (p) {
      return h("tr", null, h("td", { text: p.model }),
        ["input", "output", "cache_write_5m", "cache_write_1h", "cache_read"].map(function (k) {
          return h("td", { class: "num", text: usdTick.format(p[k]) });
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

  // ------------------------------------------------------------------ interaction

  var TABS = ["overview", "projects", "activity", "approvals", "inbox", "upgrades", "mind", "system"];

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

  TABS.forEach(function (t, i) {
    var tab = $("tab-" + t);
    tab.addEventListener("click", function () { selectTab(t, false); });
    tab.addEventListener("keydown", function (ev) {
      var next = null;
      if (ev.key === "ArrowRight") next = TABS[(i + 1) % TABS.length];
      else if (ev.key === "ArrowLeft") next = TABS[(i - 1 + TABS.length) % TABS.length];
      else if (ev.key === "Home") next = TABS[0];
      else if (ev.key === "End") next = TABS[TABS.length - 1];
      if (next) { ev.preventDefault(); selectTab(next, true); }
    });
  });

  Array.prototype.forEach.call(document.querySelectorAll(".subtab"), function (b) {
    b.addEventListener("click", function () {
      ui.mind = b.getAttribute("data-mind");
      if (ui.data) renderMind(ui.data.mind || {});
    });
  });

  Array.prototype.forEach.call(document.querySelectorAll("[data-scenario]"), function (b) {
    b.addEventListener("click", function () {
      ui.scenario = b.getAttribute("data-scenario");
      savePref("ember-scenario", ui.scenario);
      refresh();
    });
  });

  $("table-toggle").addEventListener("click", function () {
    ui.tableOpen = !ui.tableOpen;
    this.setAttribute("aria-expanded", String(ui.tableOpen));
    this.textContent = ui.tableOpen ? "Hide table" : "Show table";
    if (ui.data) renderTable(ui.data.economy);
  });

  $("composer").addEventListener("submit", function (ev) { ev.preventDefault(); });

  // Fragment links would resolve against <base href> and reload the page, so handle skip-link in JS.
  $("skip-link").addEventListener("click", function (ev) { ev.preventDefault(); $("main").focus(); });

  var THEMES = ["auto", "light", "dark"];

  function currentTheme() {
    var t = document.documentElement.getAttribute("data-theme");
    return t === "light" || t === "dark" ? t : "auto";
  }

  function applyTheme(theme) {
    if (theme === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", theme);
    savePref("ember-theme", theme === "auto" ? "" : theme);
    $("theme-toggle").textContent = "Theme: " + theme;
    destroyCharts();
    if (ui.data) renderOverview(ui.data);
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

  selectTab(ui.tab, false);
  refresh();
})();
