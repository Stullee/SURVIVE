"""Reddit, phase A: no Reddit API and no Reddit credentials.

New API apps need Reddit's explicit approval, so for now the agent researches Reddit through Anthropic's web
search (``research`` with ``site="reddit.com"``), and an approved post or comment becomes a link that opens
Reddit's submit page with the approved text filled in (a comment: the thread, to paste into). The owner posts
it from their own account. Every text ends with a line that says an AI wrote it.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

DISCLOSURE = "*Written by an AI agent (Ember) and posted by a human after review.*"
TITLE_CHARS = 300
# Reddit allows 10,000 characters, but an approval holds at most 8,000 (its payload), and the owner reads it all.
BODY_CHARS = 7_000
KINDS = ("post", "comment")
SUBREDDIT = re.compile(r"^[A-Za-z0-9_]{2,21}$")
THREAD = re.compile(
    r"^https://www\.reddit\.com/r/([A-Za-z0-9_]{2,21})/comments/[a-z0-9]{1,12}(?:/[A-Za-z0-9_%-]{1,120}){0,3}/?$"
)


class RedditError(ValueError):
    """An invalid proposal; the message is shown to the agent."""


def subreddit_name(text: str) -> str:
    """``r/Name``, ``/r/Name`` or ``Name`` as the bare name."""
    name = text.strip().removeprefix("/").removeprefix("r/")
    if not SUBREDDIT.match(name):
        raise RedditError("subreddit must be a name like 'SideProject' (2 to 21 letters, digits or _)")
    return name


def with_disclosure(body: str) -> str:
    """The body ending with the AI disclosure line (added if it isn't there)."""
    body = body.strip()
    return body if body.endswith(DISCLOSURE) else f"{body}\n\n{DISCLOSURE}"


def action(kind: str, subreddit: str, title: str | None, body: str, thread_url: str | None) -> dict[str, Any]:
    """The checked action of a proposal: what the owner will post, exactly."""
    if kind not in KINDS:
        raise RedditError("kind must be post or comment")
    name = subreddit_name(subreddit)
    title = (title or "").strip()
    thread = (thread_url or "").strip()
    if kind == "post":
        if not title:
            raise RedditError("a post needs a title")
        if len(title) > TITLE_CHARS:
            raise RedditError(f"the title is longer than {TITLE_CHARS} characters")
        if thread:
            raise RedditError("thread_url is only for comments")
    else:
        if title:
            raise RedditError("a comment has no title; leave it empty")
        match = THREAD.match(thread)
        if match is None:
            raise RedditError(
                "a comment needs thread_url: the thread's https://www.reddit.com/r/<name>/comments/… link"
            )
        if match[1].lower() != name.lower():
            raise RedditError(f"the thread is in r/{match[1]}, not r/{name}")
    text = with_disclosure(body)
    if len(text) > BODY_CHARS:
        raise RedditError(f"the text with its AI disclosure line is longer than {BODY_CHARS:,} characters")
    return {"kind": kind, "subreddit": name, "title": title or None, "body": text, "thread_url": thread or None}


def payload(act: dict[str, Any]) -> str:
    """The approval's payload: the whole text as the owner will post it."""
    if act["kind"] == "post":
        return f"r/{act['subreddit']} · post\nTitle: {act['title']}\n\n{act['body']}"
    return f"r/{act['subreddit']} · comment in {act['thread_url']}\n\n{act['body']}"


def prefilled_url(act: dict[str, Any], body: str | None = None) -> str:
    """Where the owner posts it: the submit page with title and text filled in, or the thread for a comment.

    ``body`` is the owner's own version (approved with changes), if any. Built here, from the checked action,
    never from text the agent wrote as a link.
    """
    if act.get("kind") == "comment":
        thread = str(act.get("thread_url") or "")
        return thread if THREAD.match(thread) else "https://www.reddit.com/"
    name = str(act.get("subreddit") or "")
    if not SUBREDDIT.match(name):
        return "https://www.reddit.com/"
    title = quote(str(act.get("title") or ""), safe="")
    text = quote(body if body is not None else str(act.get("body") or ""), safe="")
    return f"https://www.reddit.com/r/{name}/submit?title={title}&text={text}"
