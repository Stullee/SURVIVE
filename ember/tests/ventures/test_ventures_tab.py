"""0.27.0: the Projects and Ventures tabs are one, Ventures, with two views: Pipeline (the tree and the ventures still
being decided) and Running (the backed and live ventures, each with its projects in its card, then the other
projects). The owner saw the same work twice: a backed venture and the project Ember's code opens for it. 0.37.0: the
Ventures tab is the Plan tab's (each venture being explored is a node of the plan), its views Ventures and Product
lines."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app import paths

SCRIPT = (paths.WEB_DIR / "static" / "js" / "app.js").read_text(encoding="utf-8")
INDEX = (paths.WEB_DIR / "index.html").read_text(encoding="utf-8")


def panel(html: str, panel_id: str) -> str:
    start = html.index(f'<section class="panel" id="{panel_id}"')
    return html[start : html.index("</section>", start)]


def test_one_tab_holds_the_ventures_and_their_projects(ingress_client: TestClient) -> None:
    html = ingress_client.get("/").text
    assert 'id="tab-projects"' not in html and 'id="panel-projects"' not in html
    assert 'id="tab-ventures"' not in html and 'id="panel-ventures"' not in html  # 0.37.0: the Plan tab's
    tabs = re.findall(r'role="tab" class="tab" id="tab-(\w+)"', html)
    assert "ventures" not in tabs and "projects" not in tabs and tabs.count("plan") == 1
    ventures = panel(html, "panel-plan")
    # Two views, each a tab panel its view's tab controls; Pipeline is shown until the script says otherwise.
    for view, hidden in (("pipeline", ""), ("running", " hidden")):
        assert f'id="vt-tab-{view}" aria-controls="vt-{view}"' in ventures
        assert f'id="vt-{view}" role="tabpanel" aria-labelledby="vt-tab-{view}"{hidden}>' in ventures
    pipeline = ventures[ventures.index('id="vt-pipeline"') : ventures.index('id="vt-running"')]
    running = ventures[ventures.index('id="vt-running"') :]
    for part in ('id="vt-card"', 'id="vt-tree"', 'id="vt-add"', 'id="ventures"', 'id="vt-svg-ns"'):
        assert part in pipeline, part
    for part in ('id="projects-bar"', 'data-pj-filter="open"', 'id="projects-sort"', 'id="vt-running-list"'):
        assert part in running, part
    assert 'id="projects"' in running
    # The business cases waiting for the owner are counted on the tab (0.37.0: Plan) and on Pipeline (Ventures).
    plan_tab = html[html.index('id="tab-plan"') : html.index("</button>", html.index('id="tab-plan"'))]
    assert 'id="badge-ventures"' in plan_tab and 'id="badge-pipeline"' in ventures
    assert '>Ventures <span class="badge" id="badge-pipeline"' in ventures and ">Product lines</button>" in ventures


def test_the_backed_and_live_ventures_are_in_running_the_others_in_pipeline() -> None:
    assert '  var TABS = ["overview", "ledger", "plan", "library",' in SCRIPT  # 0.37.0: Plan holds the ventures
    assert '  var VENTURE_VIEWS = ["pipeline", "running"];' in SCRIPT
    assert "  var RUNNING_STAGES = { building: true, live: true };" in SCRIPT
    groups = SCRIPT[SCRIPT.index("  var VENTURE_GROUPS = [") : SCRIPT.index("  var RUNNING_STAGES")]
    pipeline, running = groups.split("  var RUNNING_GROUPS = [")
    assert re.findall(r'key: "(\w+)"', pipeline) == ["proposed", "researching", "idea", "closed"]
    assert re.findall(r'key: "(\w+)"', running) == ["building", "live"]
    render = SCRIPT[SCRIPT.index("  function renderVentures() {") : SCRIPT.index("  function renderRunningStatus() {")]
    rows = "rows: rows.filter(function (v) { return %sRUNNING_STAGES[v.stage]; })"
    assert 'root: $("ventures"), groups: VENTURE_GROUPS, ' + rows % "!" in render
    assert 'root: $("vt-running-list"), groups: RUNNING_GROUPS, ' + rows % "" in render
    # A card moving to the other view is the same card: its status line says what the owner's decision did.
    assert "q.items[String(v.id)] = it;" in render and "delete other.items[String(v.id)];" in render


def test_a_running_venture_card_holds_its_projects() -> None:
    render = SCRIPT[SCRIPT.index("  function renderProjects(d) {") : SCRIPT.index("  function projectParts(")]
    # Each running venture's projects go to the box in its card; the rest to the list below, each list skipped (and
    # drawn on the next poll) while the owner is busy in it.
    assert "var group = slots[vid] ? (byVenture[vid] = byVenture[vid] || { open: [], closed: [] }) : rest;" in render
    assert "if (isBusy(slots[vid])) { complete = false; return; }" in render
    assert "if (isBusy(el)) return false;" in render
    assert 'section("projects", projectsKey(d), null, function () { return renderProjects(d); });' in SCRIPT
    # In its venture's card a project needs no chip naming that venture.
    assert SCRIPT.count("ctx.inCard ? null : ventureChip(p, ctx)") == 2


def test_the_old_projects_tab_and_its_links_lead_to_running() -> None:
    assert (
        '  if (ui.tab === "projects") ui.vtView = "running";\n'
        '  if (ui.tab === "projects" || ui.tab === "ventures") ui.tab = "plan";'
    ) in SCRIPT  # 0.37.0: both old tabs are the Plan tab's views
    assert 'if (name === "ventures" || name === "projects") name = "plan";' in SCRIPT
    reveal = SCRIPT[SCRIPT.index("  function revealProject(id) {") : SCRIPT.index("  function showProject() {")]
    assert 'selectTab("plan", false);' in reveal and 'selectVentureView("running", false);' in reveal
    assert 'selectTab("projects"' not in SCRIPT and 'selectTab("ventures"' not in SCRIPT
    assert '"Projects")' not in SCRIPT and '"Open in Plan"' in SCRIPT  # the workspace's links, for both
