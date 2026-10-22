"""A listing's photos: the QA counts distinct photos, names the copies, and the owner's card says so.

Tests from 0.15.0 (formerly test_fixes_0140_factory.py)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from app.agent.sandbox import Jail
from app.integrations import etsy, qa
from app.products import images, make
from tests.channels.test_etsy import a_listing, listed
from tests.owner.test_owner_loop import owner
from tests.products.test_pictures import a_cv, three_sheets
from tests.products.test_product_tools import make_agent
from tests.products.test_products import jail

# --- From 0.15.0 (formerly test_fixes_0140_factory.py) ---


def uploads(ws: Jail, names: list[str]) -> tuple[etsy.Upload, ...]:
    return tuple(etsy.upload(n, ws.read_bytes(n), frozenset({".png", ".jpg"}), "photos") for n in names)


def test_qa_counts_distinct_photos_and_names_the_copies(tmp_path: Path) -> None:
    ws = jail(tmp_path)
    a_cv(ws)
    same = []
    for number, title in enumerate(("Modern CV", "Easy to edit", "Stand out", "Recruiter approved", "A4 + Letter")):
        make.image(ws, f"shop/same{number}.png", "shop/cv.pdf#1", title, badge="Instant download")
        same.append(f"shop/same{number}.png")
    photos = uploads(ws, same)
    looks = images.looks(ws.read_bytes, [(u.path, u.sha256) for u in photos])
    short = qa.defects("etsy.create_listing", a_listing(photos=photos), looks)
    assert short == [
        "1 distinct photo, fewer than 5 (Etsy shows up to 10); shop/same1.png repeats shop/same0.png, shop/same2.png "
        "repeats shop/same0.png, shop/same3.png repeats shop/same0.png, shop/same4.png repeats shop/same0.png: a copy "
        "adds no photo"
    ]
    # Five different photos of a one-page product: the page, two details, what is included and what it does.
    make.image(ws, "shop/top.png", "shop/cv.pdf#1@top", "Modern CV")
    make.image(ws, "shop/bottom.png", "shop/cv.pdf#1@bottom-left", "Modern CV")
    make.image(ws, "shop/included.png", "", "What's included", "CV in Word|CV in PDF", layout="text")
    make.image(ws, "shop/features.png", "", "Easy to edit", "Change colours|Free fonts", layout="text")
    distinct = uploads(
        ws, ["shop/same0.png", "shop/top.png", "shop/bottom.png", "shop/included.png", "shop/features.png"]
    )
    looks = images.looks(ws.read_bytes, [(u.path, u.sha256) for u in distinct])
    assert qa.defects("etsy.create_listing", a_listing(photos=distinct), looks) == []
    # A copy made elsewhere (smaller, as a JPEG) looks the same; the same file is a copy without looking.
    copy = io.BytesIO()
    Image.open(io.BytesIO(ws.read_bytes("shop/included.png"))).convert("RGB").resize((1500, 1125)).save(copy, "JPEG")
    ws.write_bytes("shop/copy.jpg", copy.getvalue())
    with_copy = (*distinct, *uploads(ws, ["shop/copy.jpg"]))
    looks = images.looks(ws.read_bytes, [(u.path, u.sha256) for u in with_copy])
    assert qa.repeats(with_copy, looks) == ["shop/copy.jpg repeats shop/included.png"]
    assert qa.repeats((photos[0], photos[0])) == ["shop/same0.png repeats shop/same0.png"]


def test_text_photos_with_other_words_are_other_photos(tmp_path: Path) -> None:
    ws = jail(tmp_path)
    names = []
    for number, title in enumerate(("Instant download", "Edit in Word", "Printable A4", "Made in Germany", "Support")):
        make.image(ws, f"shop/t{number}.png", "", title, layout="text")  # a title alone, on the same colours
        names.append(f"shop/t{number}.png")
    make.image(ws, "shop/again.png", "", "Instant download", layout="text", accent="#C0392B")  # the same words in red
    photos = uploads(ws, [*names, "shop/again.png"])
    looks = images.looks(ws.read_bytes, [(u.path, u.sha256) for u in photos])
    assert qa.repeats(photos, looks) == ["shop/again.png repeats shop/t0.png"]


def test_one_page_under_other_names_is_one_photo(tmp_path: Path) -> None:
    ws = jail(tmp_path)
    a_cv(ws)
    three_sheets(ws)
    make.spreadsheet(ws, "b.json", "shop/b.xlsx")
    ws.write_bytes("shop/cv-b.pdf", ws.read_bytes("shop/cv.pdf"))  # what workspace_write's copy makes
    refs = ["shop/cv.pdf#1", "shop/cv.pdf#01", "shop/cv.pdf#0001", "shop/cv-b.pdf#1", "shop/cv.pdf"]
    refs += ["shop/b.xlsx#Costs", "shop/b.xlsx#COSTS", "shop/b.xlsx#3"]
    names = []
    for number, ref in enumerate(refs):
        make.image(ws, f"shop/n{number}.png", ref, f"Title {number}", badge="Instant download")
        names.append(f"shop/n{number}.png")
    photos = uploads(ws, names)
    looks = images.looks(ws.read_bytes, [(u.path, u.sha256) for u in photos])
    assert qa.distinct(photos, looks) == 2
    # Another make_image photo of the same page that its note doesn't match (a PDF made again) looks the same.
    marks = {look.rpartition(".")[0] for look in looks}
    assert len(marks) == 2 and all(mark.startswith("photo-") for mark in marks)
    renoted = Image.open(io.BytesIO(ws.read_bytes(names[0])))
    out = io.BytesIO()
    renoted.save(out, "PNG")
    ws.write_bytes("shop/again.png", images.marked(out.getvalue(), "photo:another"))
    again = (photos[0], *uploads(ws, ["shop/again.png"]))
    assert qa.repeats(again, images.looks(ws.read_bytes, [(u.path, u.sha256) for u in again])) == [
        "shop/again.png repeats shop/n0.png"
    ]


@pytest.mark.parametrize("page", ["\u00b2", "\u0661", "1.0"])
def test_a_page_in_other_digits_is_refused_clearly(tmp_path: Path, page: str) -> None:
    ws = jail(tmp_path)
    a_cv(ws)
    three_sheets(ws)
    make.spreadsheet(ws, "b.json", "shop/b.xlsx")
    with pytest.raises(make.ProductError, match="a PDF's page is a number"):
        make.image(ws, "shop/x.png", f"shop/cv.pdf#{page}", "T")
    with pytest.raises(make.ProductError, match="the workbook has no sheet"):
        make.image(ws, "shop/x.png", f"shop/b.xlsx#{page}", "T")


def test_copies_of_one_photo_are_no_qa_fix_and_leave_the_listing_short(data_dir: Path) -> None:
    from app.integrations import etsy_publisher
    from tests.owner.test_policy import a_milestone, change, reject, work_on

    agent, listing_id = listed(data_dir)
    ws = agent.roots()[0]
    a_cv(ws)
    for number, title in enumerate(("Modern CV", "Easy to edit", "Stand out", "Recruiter approved", "A4 + Letter")):
        make.image(ws, f"shop/same{number}.png", "shop/cv.pdf#1", title)  # the same page under five titles
        ws.write_bytes(f"shop/copy{number}.png", ws.read_bytes("shop/same0.png"))  # one file, five times
    make.image(ws, "shop/top.png", "shop/cv.pdf#1@top", "Modern CV")
    make.image(ws, "shop/bottom.png", "shop/cv.pdf#1@bottom-left", "Modern CV")
    make.image(ws, "shop/included.png", "", "What's included", "CV in Word|CV in PDF", layout="text")
    make.image(ws, "shop/features.png", "", "Easy to edit", "Change colours|Free fonts", layout="text")
    goal = a_milestone(agent)
    assert owner(agent).set_autonomy(goal, {"rule": "qa_fix", "level": "auto"}, "Stefan").status == 200
    made = work_on(agent, goal, change(listing_id, photos=", ".join(f"shop/same{n}.png" for n in range(5))))
    assert made[-1]["status"] == "pending"  # not a QA fix: it waits for the owner, whose card names the repeats
    reject(agent, made[-1]["id"])
    made = work_on(agent, goal, change(listing_id, photos=", ".join(f"shop/copy{n}.png" for n in range(5))))
    assert made[-1]["status"] == "pending"
    assert owner(agent).decide(made[-1]["id"], {"decision": "approve"}, "Stefan").status == 200
    assert agent.execute_approved() == [(made[-1]["id"], "done")]
    with agent.db.connection() as conn:
        assert etsy_publisher.few_photos(conn, agent.scope()) == [(listing_id, 1)]  # the obligation stays open
    distinct = "shop/same0.png, shop/top.png, shop/bottom.png, shop/included.png, shop/features.png"
    made = work_on(agent, goal, change(listing_id, photos=distinct))
    assert made[-1]["status"] == "approved"  # five distinct photos: a QA fix the owner unlocked


def test_the_owners_card_names_repeated_photos(data_dir: Path) -> None:
    from app.agent import views

    agent, _ = make_agent(data_dir, [])
    ws = agent.roots()[0]
    a_cv(ws)
    for number in range(2):
        make.image(ws, f"shop/p{number}.png", "shop/cv.pdf#1", f"Title {number}")
    photos = uploads(ws, ["shop/p0.png", "shop/p1.png"])
    waiting, decided = {"status": "pending"}, {"status": "approved"}
    assert views._looks(agent, waiting, photos) == images.looks(ws.read_bytes, [(p.path, p.sha256) for p in photos])
    assert qa.repeats(photos, views._looks(agent, waiting, photos)) == ["shop/p1.png repeats shop/p0.png"]
    assert views._looks(agent, decided, photos) is None  # a decided request's photos aren't read again
