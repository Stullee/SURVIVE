"""0.42.2 (analysis-0.40.1, 5.6): the tools that ask or tell the owner, and the mailbox, split out of tools.py.

request_approval, withdraw_request, message_owner (and the promises it records: obligations.py), obligation_done,
request_upgrade, set_sleep and write_journal; the mailbox (email_inbox, email_read, mark_opt_out, inquiry_done,
propose_email) and propose_reddit_post. ``_new_request`` makes every approval request a tool proposes, and
``_refuse`` refuses by a precondition (preconditions.py), which the plan reads too.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

from ..economy.clock import to_iso
from ..integrations import connectors, mail, mailstore, qa, reddit
from . import obligations, policy, preconditions, roadmap, store, ventures
from .store import AgentScope
from .tools_checks import _noted, unleaked, validate
from .tools_context import MailAccess, Outcome, ToolContext, ToolError, _cut, _local, _numbers, _quoted, wrap
from .tools_limits import (
    EMAIL_READ_CHARS,
    FIRST_CONTACT,
    INBOX_CHARS,
    INBOX_SIZE,
    MAX_ACTION_CHARS,
    MAX_NEW_UPGRADES,
    MAX_RESULT_CHARS,
    REDDIT_NOTE,
)
from .tools_lines import _line, _listing_line
from .tools_projects import EVIDENCE
from .tools_specs import Spec


def _request_approval(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    existing = store.pending_approval_by_payload(conn, ctx.scope, store.sha256(args["payload"]))
    if existing is not None:
        return Outcome(
            True, f"Approval request #{existing} with this payload is already waiting.", f"duplicate of #{existing}"
        )
    own = _own_tool(ctx, args)  # 0.37.8
    if own:
        raise ToolError(own)
    _room_for_request(ctx, conn, args["type"])
    project_id = args.get("project_id")
    if project_id is not None and store.project(conn, ctx.scope, project_id) is None:
        raise ToolError(f"there is no project #{project_id}")
    _line(ctx, conn, project_id, "request")  # 0.28.0
    approval_id = store.insert_approval(conn, ctx.scope, ctx.cycle_id, ctx.now(), **args)
    return Outcome(
        True,
        f"Approval request #{approval_id} is waiting for your owner. Nothing has been done yet.",
        f"#{approval_id} {args['type']}: {args['title'][:60]}",
        project_id,
    )


# 0.37.8: the publishing that has a tool of its own, which Ember's code carries out in its slot and the plan counts:
# what it names, the words of a request for it (its title says what it is), what it is, the tool and what Ember's code
# does then. Live, a Bluesky post went to the owner through request_approval (#91): approved, nothing posted it, and
# its step went on asking for two posts.
OWN_TOOLS = (
    (r"\b(?:bluesky|bsky)\b", r"\bpost", "a Bluesky post", "propose_bluesky_post", "bluesky", "posts it in its slot"),
    (r"\b(?:pinterest|pins?)\b", r"\bpins?\b", "a pin", "propose_pin", "pinterest", "makes it in its slot"),
    (r"\bblog\b", r"\b(?:blog ?posts?|articles?|beitrag)\b", "a blog post", "propose_blog_post", "blog", "uploads it"),
)


def _own_tool(ctx: ToolContext, args: dict[str, Any]) -> str:
    """0.37.8: why a request to publish what has a tool of its own is refused ("" when it isn't such a request, or its
    channel isn't set up: then only the owner's hand can publish it)."""
    if args["type"] != "publish":
        return ""
    words = f"{args['title']}\n{args['payload']}"
    ready = {"bluesky": ctx.bluesky is not None, "pinterest": ctx.pinterest is not None, "blog": ctx.blog is not None}
    for channel, thing, what, tool, name, does in OWN_TOOLS:
        if ready[name] and re.search(channel, words, re.IGNORECASE) and re.search(thing, args["title"], re.IGNORECASE):
            # 0.39.2 (analysis-0.39.1, 3.4): and how to get there from a cycle without it: live, a step to send pins
            # was a "ship" step, an ordinary cycle took it and closed it with the pins unsent
            elsewhere = (
                f"; this cycle has no {tool}: plan_step add a step of kind market for it, which a marketing cycle takes"
                if ctx.marketing_apart and not ctx.marketing
                else ""
            )
            return (
                f"{what} goes through {tool} (a marketing cycle's tool): Ember's code {does} once your owner approves"
                " it, and your plan counts it; a request_approval only asks your owner to publish it by hand"
                + elsewhere
            )
    return ""


def _room_for_request(ctx: ToolContext, conn: Any, kind: str) -> None:
    """0.12.0: a cap per type of request (preconditions.ROOM; one cap of 10 for all let waiting listings block an email
    reply)."""
    _refuse(ctx, conn, preconditions.ROOM[kind])


def _facts(ctx: ToolContext, conn: Any, **call: Any) -> preconditions.Facts:
    """0.42.0: what a precondition reads of this call (preconditions.Facts), on the tool's clock."""
    day_start = to_iso(ctx.clock.day_start(ctx.clock.today()))
    return preconditions.Facts(conn, ctx.scope, ctx.clock.now(), day_start, ctx.clock.tz, **call)


def _refuse(ctx: ToolContext, conn: Any, rule: preconditions.Rule, **call: Any) -> None:
    """0.42.0 (analysis-0.40.1 codebase, 6.2.1): a refusal by a rule the plan can know before a cycle is the tool's
    precondition (preconditions.py), which the plan reads too: a step of the tool waits on it with these words, and
    takes no cycle that would end in the refusal. ``call``: what the call is about (preconditions.Facts)."""
    said = rule.check(_facts(ctx, conn, **call))
    if said:
        raise ToolError(said)


def _withdraw_request(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    """0.12.0: the agent takes back one of its pending requests."""
    request_id = args["request_id"]
    where, params = ctx.scope.where()
    row = conn.execute(f"SELECT * FROM approvals WHERE id = ? AND {where}", (request_id, *params)).fetchone()
    if row is None:
        raise ToolError(f"there is no request #{request_id}")
    if row["status"] != "pending":
        raise ToolError(f"request #{request_id} is {row['status'].replace('_', ' ')} already; only a waiting one")
    reason = " ".join(args["reason"].split())
    if not reason:
        raise ToolError("say why in reason")
    store.withdraw_request(conn, ctx.scope, request_id, reason, ctx.cycle_id, ctx.now())
    return Outcome(True, f"Request #{request_id} is withdrawn: it left your owner's queue.", f"withdrew #{request_id}")


def _message_owner(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    _refuse(ctx, conn, preconditions.UNREAD)  # 0.40.3: the plan reads it too
    named = list(dict.fromkeys(int(n) for n in re.findall(r"\d{1,9}", args.get("answers") or "")))[:20]
    promised = _promise(ctx, args)
    about = _promised_line(ctx, conn, args.get("project_id"), promised is not None)
    if about is None and promised is not None:  # 0.35.1: the line its words name (a listing's number, KDP)
        about = obligations.promised_line(conn, ctx.scope, promised[0])
    if promised is None:  # 0.33.0
        _unrecorded_promise(ctx, conn, args["text"])
    # 0.12.0: at most MESSAGES_PER_DAY a day that answer none of the owner's (the prompt's "once a day" was prose)
    reports = promised is None and _reports_promise(ctx, conn, args["text"])
    _refuse(ctx, conn, preconditions.MESSAGES, answers=store.answerable(conn, ctx.scope, named) or reports)
    message_id = store.insert_message(conn, ctx.scope, ctx.cycle_id, args["text"].strip(), ctx.now())
    answered = store.mark_answered(conn, ctx.scope, named, message_id)
    text = f"Message #{message_id} is in your owner's inbox."
    again = obligations.repeated_promise(conn, ctx.scope, *promised) if promised is not None else None
    on = f" on line #{about}" if about is not None else ""
    if again is not None:  # 0.24.0: no second obligation for the same promise
        named_now = about is not None and obligations.name_line(conn, int(again["id"]), about)  # 0.33.0
        text += (
            f" Its promise repeats your open promise #{again['id']} (due {again['due']}"
            f"{', now' + on if named_now else ''}): no new obligation; close #{again['id']} with obligation_done once"
            " kept."
        )
    elif promised is not None:
        made = obligations.promise(
            conn, ctx.scope, ctx.cycle_id, message_id, promised[0], promised[1], ctx.now(), about
        )
        text += f" Your promise is obligation #{made}, due {promised[1]}{on}: close it with obligation_done once kept."
    if answered:
        text += f" It answers {_numbers(answered)}: they leave FROM YOUR OWNER."
    wrong = [n for n in named if n not in answered]
    if wrong:
        text += f" Not an open message from your owner that you were shown, so still as it was: {_numbers(wrong)}."
    waiting = [r["id"] for r in store.open_messages(conn, ctx.scope) if r["seen_cycle_id"] is not None]
    if waiting:
        text += f" Still waiting for your answer: {_numbers(waiting)}."
    summary = f"message #{message_id}" + (f", answers {_numbers(answered)}" if answered else "")
    return Outcome(True, text, summary)


# 0.33.0: words that promise later work. Live, "Next ordinary cycle I'll finish the Haushaltsbuch 2027 interior",
# "coming next cycle" and "Today's focus: finishing and sending the ... proposal" were said in three messages of
# 2026-10-07 with no commits: no obligation kept them, and nothing was sent.
PROMISE_WORDS = re.compile(
    r"\bnext (?:ordinary |marketing |venture )?cycle\b|\b(?:coming|comes) (?:today|tomorrow|tonight|this week)\b"
    r"|\btoday'?s focus\b|\bwill (?:re)?(?:check|send|publish|post|finish|propose|report|deliver|ship|fix)\b"
    r"|\bn[äa]chste[nr]? (?:zyklus|runde)\b|\bkommt (?:heute|morgen)\b",
    re.IGNORECASE,
)
QUOTED = re.compile(r'"[^"]*"|“[^”]*”|„[^“”]*[“”]')


def _unrecorded_promise(ctx: ToolContext, conn: Any, text: str) -> None:
    """0.33.0: a message whose words promise later work but whose promise isn't recorded (commits) goes back once;
    sent again unchanged, it goes out (its words promised nothing after all). A message that reports on an open
    promise (#n) goes out."""
    said = PROMISE_WORDS.search(QUOTED.sub(" ", text))  # what it quotes (its owner's words) promises nothing
    key = " ".join(text.split())
    if said is None or key in ctx.state.bounced or _reports_promise(ctx, conn, text):
        return
    ctx.state.bounced.add(key)
    raise ToolError(
        f'your message promises later work ("{said.group(0)}") but records no promise: put what you promise in '
        "commits, with due and project_id, so OBLIGATIONS keeps it until it is kept; send it again unchanged if it "
        "promises nothing"
    )


def _promised_line(ctx: ToolContext, conn: Any, project_id: int | None, promises: bool) -> int | None:
    """0.33.0: the open project a message's promise is about (project_id), or None; raises ToolError. Live, a
    promise had no line: the KDP book promised three times never got a cycle of its line."""
    if project_id is None:
        return None
    if not promises:
        raise ToolError("project_id names the project a promise is about: give commits and due with it")
    row = store.project(conn, ctx.scope, project_id)
    if row is None or row["status"] not in store.OPEN_STATUSES:
        raise ToolError(f"project #{project_id} isn't one of your open projects")
    return int(project_id)


# 0.39.1 (analysis-0.38.0, 3.10): what a message's commits says when it promises nothing ("none - informational
# only"): live, three messages were refused for a promise without its due day, each meaning none.
NO_PROMISE = re.compile(r"^(?:none|n/?a|nothing|no promise|-+|—|–)\s*(?:$|[-—–:;,(.].*$)", re.IGNORECASE)


def _promise(ctx: ToolContext, args: dict[str, Any]) -> tuple[str, str] | None:
    """(what, due day) of the promise a message makes (0.12.0), or None; raises ToolError."""
    what = " ".join((args.get("commits") or "").split())
    due = args.get("due")
    if NO_PROMISE.match(what):
        what = ""
    if isinstance(due, str) and NO_PROMISE.match(due.strip()):
        due = None
    if not what and not due:
        return None
    if not what or not due:
        raise ToolError("a promise needs both: what you promise (commits) and when it is due (due)")
    today = ctx.clock.today()
    day = roadmap.parse_day(due)
    last = today + timedelta(days=obligations.PROMISE_DAYS)
    if day is None or not today <= day <= last:
        raise ToolError(f"due is a day from today to {obligations.PROMISE_DAYS} days ahead ({last.isoformat()})")
    return what, day.isoformat()


def _reports_promise(ctx: ToolContext, conn: Any, text: str) -> bool:
    """0.15.0: whether the message names an open promise (#n) that the owner hasn't heard about since it was made
    (preconditions.untold, which the plan reads of a step keeping one): the daily cap lets it through. Only a message
    that makes no promise itself, so one report can't open the way for the next."""
    numbers = dict.fromkeys(int(n) for n in re.findall(r"#(\d{1,9})", text))
    return any(preconditions.untold(conn, ctx.scope, number) for number in numbers)


def _obligation_done(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    """0.12.0: close obligations the agent met, with what it did (a promise only once the owner heard from it)."""
    result = " ".join(args["result"].split())
    if not EVIDENCE.search(result):
        raise ToolError("result names what you did: a reference (message #, request #, milestone #) or a file")
    named = list(dict.fromkeys(int(n) for n in re.findall(r"\d{1,9}", args["numbers"])))[:10]
    if not named:
        raise ToolError("numbers names the obligations from OBLIGATIONS, e.g. '3, 5'")
    where, params = ctx.scope.where()
    closed, refused, answered = [], [], []
    for number in named:
        row = conn.execute(f"SELECT * FROM obligations WHERE id = ? AND {where}", (number, *params)).fetchone()
        if row is None and (by := _answered_by(conn, ctx.scope, number)) is not None:
            # 0.32.0: nothing to close, and no error: live, 4 of 12 cycles named the message they had just answered
            answered.append(f"#{number} is your owner's message, which your message #{by} answered: nothing to close")
        elif row is None or row["status"] != "open":
            refused.append(_not_open(conn, ctx.scope, number, row))
        elif row["kind"] == "promise" and not obligations.told_since(conn, ctx.scope, row["message_id"]):
            refused.append(
                f"#{number} is a promise: tell your owner it is kept (or why not) with message_owner first, naming "
                f"#{number} (the daily limit lets it through if it promises nothing new)"
            )
        else:
            obligations.close_one(conn, number, result, "agent", ctx.cycle_id, ctx.now())
            closed.append(number)
    if not closed and not answered:
        raise ToolError("; ".join(refused))
    text = " ".join(
        [*([f"Closed {_numbers(closed)}."] if closed else []), *(f"{a}." for a in answered)]
        + ([f"Not closed: {'; '.join(refused)}."] if refused else [])
    )
    return Outcome(True, text, f"closed {_numbers(closed)}" if closed else "nothing to close")


def _answered_by(conn: Any, scope: AgentScope, number: int) -> int | None:
    """0.32.0: the agent's message that answered owner's message #number, or None (not one, or not answered)."""
    where, params = scope.where()
    found = conn.execute(
        f"SELECT answered_by FROM messages WHERE id = ? AND {where} AND sender = 'owner'", (number, *params)
    ).fetchone()
    return int(found["answered_by"]) if found is not None and found["answered_by"] else None


def _not_open(conn: Any, scope: AgentScope, number: int, row: Any) -> str:
    """Why obligation_done didn't close #number. 0.24.0: it says when that is a message of your owner's (live, three
    cycles named their owner's messages, which their answer had already taken out of FROM YOUR OWNER)."""
    if row is not None:
        return f"#{number} is not an open obligation of yours (it is {row['status']})"
    where, params = scope.where()
    message = conn.execute(
        f"SELECT 1 FROM messages WHERE id = ? AND {where} AND sender = 'owner'", (number, *params)
    ).fetchone()
    if message is None:
        return f"#{number} is not an open obligation of yours"
    return (  # 0.32.0: one already answered is no refusal (_answered_by)
        f"#{number} is your owner's message, not an obligation: a message of yours that names it in answers is all "
        "it needs"
    )


def _request_upgrade(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    if store.count_rows(conn, "upgrades", ctx.scope, "status = 'new'") >= MAX_NEW_UPGRADES:
        raise ToolError(f"{MAX_NEW_UPGRADES} upgrade requests are already waiting")
    fields = {k: v for k, v in args.items() if k != "workshop_script"}
    script = args.get("workshop_script")
    if script is not None:
        if not script.endswith(".py"):
            raise ToolError("workshop_script must be a .py script the workshop kept, e.g. 'workshop/scripts/x-3.py'")
        code = ctx.workspace.read(script)
        earlier = store.script_requests(conn, ctx.scope, script, code)
        if earlier:
            raise ToolError(_script_asked(earlier[0], script))
        fields.update(script_path=script, script_text=code)
    upgrade_id = store.insert_upgrade(conn, ctx.scope, ctx.cycle_id, ctx.now(), **fields)
    sent = f" with {script}" if script else ""
    return Outcome(True, f"Upgrade request #{upgrade_id} filed{sent}.", f"#{upgrade_id} {args['title'][:60]}")


def _script_asked(row: Any, script: str) -> str:
    """0.22.2: why a workshop script an earlier upgrade request carried is not asked for again (live, request #8 asked
    again for the script that 0.20.0 built in as make_cost_statement, from request #7)."""
    request = f"upgrade request #{row['id']}"
    same = "" if row["script_path"] == script else f" (the same code as {row['script_path']})"
    note = row["owner_note"]
    note = f" Your owner's note: {json.dumps(note[:300], ensure_ascii=False)}." if note else ""
    if row["status"] == "released":
        return (
            f"{request} carried {script}{same}, and your owner released it in {row['released_version']}: it is built "
            f"in. The release notes of {row['released_version']} name the tool that does it, free: use that. Ask "
            "again, without workshop_script, only for what that tool can't do." + note
        )
    if row["status"] == "declined":
        return f"your owner declined {request}, which carried {script}{same}." + note
    return f"{request} already carries {script}{same} and waits for your owner ({row['status']})." + note


def _set_sleep(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    asked = args["minutes"]
    minutes = max(ctx.min_sleep, min(ctx.max_sleep, asked))
    ctx.state.sleep_minutes = minutes
    ctx.state.sleep_reason = args["reason"].strip()[:200]
    note = "" if minutes == asked else f" (asked {asked}; allowed {ctx.min_sleep}–{ctx.max_sleep})"
    return Outcome(True, f"Next wake in {minutes} min{note}.", f"sleep {minutes} min")


JOURNAL_DRAFT = (
    "Kept as your journal's draft: your work steps end here, and your reflection comes next. Ember's code saves this "
    "draft as the cycle's journal unless your reflection calls write_journal again: do that only to correct it (what "
    "wasn't done, a better next)."
)


def _journal_draft(ctx: ToolContext, spec: Spec, raw: Any, leaked: list[tuple[str, dict[str, Any]]] | None) -> Outcome:
    """0.24.0: a journal written in a work step, kept for the reflection (loop._keep_draft saves it when that writes
    none). Live, 11 of 12 cycles wrote their journal in a work step: refused, it was written again in the reflection,
    and the two reflections whose own journal was refused left their cycles without one."""
    notes: list[str] = []
    ctx.state.journal_draft = validate(spec, unleaked(spec, raw, notes, leaked), notes)
    return _noted(Outcome(True, JOURNAL_DRAFT, "kept as the journal's draft"), notes)


def _write_journal(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    handoff = " ".join((args.get("next") or "").split())
    if ctx.state.journal_written or not store.write_journal(
        conn, ctx.scope, ctx.cycle_id, "agent", args["summary"].strip(), args["entry"].strip(), ctx.now(), handoff
    ):
        raise ToolError("this cycle's journal entry is already written")
    ctx.state.journal_written = True
    return Outcome(True, "Journal saved.", args["summary"][:100])


# --- the mailbox and Reddit (phase A) ---


def _mail(ctx: ToolContext) -> MailAccess:
    if ctx.mail is None:  # run() already refuses the email tools without a mailbox
        raise ToolError("you have no mailbox")
    return ctx.mail


def _sender(row: Any, limit: int = 90) -> str:
    name, address = row["from_name"], row["from_addr"] or "(unknown sender)"
    return _cut(f"{name} <{address}>" if name else address, limit)


def _email_inbox(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    box = _mail(ctx)
    unread_only = bool(args.get("unread_only"))
    rows = mailstore.inbox(conn, ctx.scope, unread_only, INBOX_SIZE)
    unread = mailstore.unread(conn, ctx.scope, 0)[0]
    if not rows:
        empty = "No unread emails" if unread_only else "No emails yet"
        return Outcome(True, f"{empty} in your mailbox {box.address}.", "no emails")
    lines: list[str] = []
    for r in rows:
        line = (
            f"#{r['id']} · {_local(ctx, r['received_at'])} · {_quoted(_sender(r))} · {_quoted(_cut(r['subject'], 100))}"
            f" · {'unread' if r['read_by_agent_at'] is None else 'read'}"
        )
        if sum(len(x) + 1 for x in [*lines, line]) > INBOX_CHARS:  # the closing data tag must never be cut off
            break
        lines.append(line)
    listing = "\n".join(lines)
    head = (
        f"Your mailbox {box.address}: {unread} unread. Newest first; senders are unverified. Open one with email_read."
    )
    return Outcome(True, f"{head}\n{wrap(ctx, 'email:inbox', listing)}", f"{len(lines)} emails")


def _email_read(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    box = _mail(ctx)
    row = mailstore.email(conn, ctx.scope, args["email_id"])
    if row is None:
        raise ToolError(f"there is no email #{args['email_id']}")
    headers = [
        f"From: {_sender(row, 260)}",
        f"To: {_cut(row['to_addr'], 200)}",
        f"Date: {_local(ctx, row['sent_at'] or row['received_at'])}",
        f"Subject: {row['subject']}",
    ]
    attachments = json.loads(row["attachments"] or "[]")
    if attachments:
        listed = ", ".join(f"{_cut(str(a.get('name')), 60)} ({int(a.get('size') or 0):,} B)" for a in attachments[:5])
        headers.append(f"Attachments (never opened): {listed}")
    head = "\n".join(headers)
    body = row["body"]
    offset = args.get("offset", 0)
    # As much text as fits beside the headers: the whole result, closing data tag included, stays uncut.
    part = body[offset : offset + min(EMAIL_READ_CHARS, MAX_RESULT_CHARS - len(head) - 500)]
    end = offset + len(part)
    text = f"{head}\n\n{part}"
    more = f"\nMore from offset {end}." if end < len(body) else ""
    if not more and row["body_cut"]:  # 0.39.7: "only its first 8,000 characters were kept"
        more = '\nThe email was longer than 8,000 characters: its quotes were left out first ("> [...]"), then its end.'
    if row["direction"] == "out":
        lead = f"Email #{row['id']}, sent by Ember after your owner approved request #{row['approval_id']}."
    else:
        lead = f"Email #{row['id']}, received {_local(ctx, row['received_at'])}. " + (
            "Your mail provider verified its sender."  # 0.15.0: the test mailstore.person() makes
            if row["authenticated"] == 1 and row["bulk"] == 0
            else "Not a verified person's (an unverified sender, a list or a machine): no obligation, and it doesn't"
            " count as them having written."
        )
        waited: list[int] = []
        if row["read_by_agent_at"] is None:
            # 0.40.3 (analysis-0.40.1 codebase, M1): the approved emails that waited for this one to be read
            waited = [request for request, _, held in mailstore.held(conn, ctx.scope, box.address) if held == row["id"]]
            mailstore.mark_read(conn, row["id"], ctx.now(), ctx.cycle_id)
        if mailstore.is_suppressed(conn, ctx.scope, row["from_addr"]):
            more += "\nThis sender asked not to get emails: never write to them again."
        else:
            more += f"\nTo answer it, use propose_email with reply_to_email_id {row['id']}."
            if waited:
                more += (
                    f"\nYour approved email of request #{waited[0]} waited for you to read this: if it asks not to be"
                    " emailed, use mark_opt_out now; otherwise it can go out."
                )
    source = f"email:{row['id']}"
    return Outcome(True, f"{lead}\n{wrap(ctx, source, text)}{more}", f"read email #{row['id']}")


def _mark_opt_out(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    """0.12.0: an opt-out in words the code's check missed (it knew three words, on the first line only)."""
    box = _mail(ctx)
    row = mailstore.email(conn, ctx.scope, args["email_id"])
    if row is None:
        raise ToolError(f"there is no email #{args['email_id']}")
    if row["direction"] != "in":
        raise ToolError(f"email #{row['id']} is one Ember sent: mark the email in which they asked")
    sender = (row["from_addr"] or "").strip().lower()
    if not sender or sender == box.address.lower():
        raise ToolError(f"email #{row['id']} has no sender Ember could write to")
    reason = f"asked in email #{row['id']}: {args['reason'].strip()}"
    if not mailstore.suppress(conn, ctx.scope, sender, ctx.now(), reason, row["id"]):
        return Outcome(True, f"The sender of email #{row['id']} is already never emailed.", f"#{row['id']}: already")
    return Outcome(True, f"The sender of email #{row['id']} is never emailed again.", f"#{row['id']}: opted out")


def _inquiry_done(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    """0.13.0 (Phase E1): a person's email that needs no answer leaves OBLIGATIONS."""
    _mail(ctx)
    row = mailstore.email(conn, ctx.scope, args["email_id"])
    if row is None:
        raise ToolError(f"there is no email #{args['email_id']}")
    if not mailstore.is_inquiry(conn, ctx.scope, row["id"]):
        raise ToolError(f"email #{row['id']} doesn't wait for an answer (OBLIGATIONS lists those that do)")
    mailstore.close_inquiry(conn, row["id"], args["reason"].strip(), "agent", ctx.now())
    return Outcome(True, f"Email #{row['id']} needs no answer: closed.", f"#{row['id']}: closed")


def _new_request(ctx: ToolContext, conn: Any, payload: str, action: dict[str, Any], **fields: Any) -> int | str:
    """An approval request that Ember's code or the owner's click carries out; the text of a duplicate instead."""
    action_json = store.canonical(action)
    if len(action_json) > MAX_ACTION_CHARS:
        raise ToolError("the text is too long; make it shorter")
    existing = store.pending_approval_by_payload(conn, ctx.scope, store.sha256(payload))
    if existing is not None:
        return f"Approval request #{existing} with this text is already waiting."
    _room_for_request(ctx, conn, fields["type"])
    made = store.insert_approval(
        conn, ctx.scope, ctx.cycle_id, ctx.now(), payload=payload, action=action_json, **fields
    )
    # the owner's unlocks (0.15.0: none while they are off; photos that repeat one another count once)
    ctx.state.policy_note = policy.apply(conn, ctx.scope, made, ctx.clock, ctx.unlocks_off, ctx.workspace.read_bytes)
    return made


def _unlocked(ctx: ToolContext) -> str:
    """What the owner's unlock did with the request just made ("" when it waits for them as before)."""
    note, ctx.state.policy_note = ctx.state.policy_note, ""
    return note


def _at_once(conn: Any, made: int, note: str, then: str) -> str | None:
    """0.15.0: the answer for a request an unlock approved at once (None if it waits): what happened first, not that it
    waits for the owner."""
    row = conn.execute("SELECT status FROM approvals WHERE id = ?", (made,)).fetchone()
    return f"Approval request #{made}:{note}{then}" if row is not None and row["status"] == "approved" else None


def _propose_email(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    box = _mail(ctx)
    to = (args.get("to") or "").strip()
    in_reply_to = references = None
    reply_id = args.get("reply_to_email_id")
    if reply_id is not None:
        row = mailstore.email(conn, ctx.scope, reply_id)
        if row is None or row["direction"] != "in":
            raise ToolError(f"#{reply_id} is not an email you received")
        sender = row["from_addr"]
        if not mail.valid_address(sender):
            raise ToolError(f"email #{reply_id} has no sender address you can answer")
        if to and to.lower() != sender.lower():
            raise ToolError(f"a reply goes to the sender of #{reply_id} ({sender}); leave to empty")
        to = sender
        in_reply_to, references = mailstore.thread_headers(row)
    elif not to:
        raise ToolError("to is required unless you answer an email with reply_to_email_id")
    try:
        action = mail.email_action(to, args["subject"], args["body"], in_reply_to, references)
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    if to.lower() == box.address.lower():
        raise ToolError("that is your own address")
    if mailstore.is_suppressed(conn, ctx.scope, to):
        raise ToolError(f"{to} asked not to get emails from you; never write to them again")
    first = not mailstore.has_written(conn, ctx.scope, to)
    reason = args["reason"].strip()
    made = _new_request(
        ctx,
        conn,
        f"To: {to}\nSubject: {action['subject']}\n\n{action['body']}",
        action,
        type="contact",
        title=_cut(f"Email to {to}: {action['subject']}", 120),
        description=f"{reason}\n\n{FIRST_CONTACT}" if first else reason,
        expected_cost="none",
        expected_benefit=reason,
        executor="email",
    )
    if isinstance(made, str):
        return Outcome(True, made, "duplicate email")
    note = _unlocked(ctx)
    then = f" It is sent once, with its AI footer (at most {box.daily_limit} emails a day), and you hear the result."
    done = _at_once(conn, made, note, then)
    if done is not None:
        return Outcome(True, done, f"#{made} email to {_cut(to, 60)}")
    text = (
        f"Approval request #{made} is waiting for your owner. Nothing has been sent. If they approve it, Ember's code "
        f"sends it once, with its AI footer (at most {box.daily_limit} emails a day), and you hear the result."
    )
    if first:
        text += " No verified email from this person reached you, so your owner is warned that it is a first contact."
    short = qa.defects(connectors.class_of("email", action).name, action)  # 0.13.0: an answer's checks
    if short:
        text += f" QA (Ember's code): {'; '.join(short)}; your owner sees it too."
    text += note
    return Outcome(True, text, f"#{made} email to {_cut(to, 60)}")


def _propose_reddit_post(ctx: ToolContext, args: dict[str, Any], conn: Any) -> Outcome:
    try:
        action = reddit.action(args["kind"], args["subreddit"], args.get("title"), args["body"], args.get("thread_url"))
    except reddit.RedditError as exc:
        raise ToolError(str(exc)) from None
    for word in f"{action.get('title') or ''} {action['body']}".split():  # 0.28.0: the listings it links
        linked = ventures.LISTING_LINK.search(word.strip("()[]<>.,;\"'"))
        if linked:
            _listing_line(ctx, conn, int(linked[1]), "Reddit post linking it")
    where = f"r/{action['subreddit']}"
    title = f"Reddit post in {where}: {action['title']}" if action["kind"] == "post" else f"Reddit comment in {where}"
    reason = args["reason"].strip()
    made = _new_request(
        ctx,
        conn,
        reddit.payload(action),
        action,
        type="publish",
        title=_cut(title, 120),
        description=f"{reason}\n\n{REDDIT_NOTE}",
        expected_cost="none",
        expected_benefit=reason,
        executor="reddit_link",
    )
    if isinstance(made, str):
        return Outcome(True, made, "duplicate post")
    return Outcome(
        True,
        f"Approval request #{made} is waiting for your owner. Nothing has been posted. If they approve it, they post "
        "it from their own Reddit account and report back with the link.",
        f"#{made} {action['kind']} in {where}",
    )
