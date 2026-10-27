"""0.45.0 (evaluation 2026-10-22, C1): what a public text of Ember's may not claim, which every request tool refuses.

Live, in the week 10-12 to 10-18, 13 of her 39 requests said a person made what she made ("Designed by the shop owner,
AI-assisted", "Von einem Menschen gestaltet", "designed by the author with the help of an AI tool"), promised legal
certainty ("rechtssicher", "§ 556 BGB konform", for a template her own critic had faulted) or "no refunds". Her owner
approved every one: the approval is the last check, not the first. So the request tools refuse them, with what to
write instead (``refusal``), and nothing reaches the owner:

* authorship (AUTHORSHIP): a verb of making ("designed", "made", "gestaltet", ...) with a person as its maker ("by a
  human", "by the shop owner", "by a solo creator", "von einem Menschen"). Her constitution: always disclose that you
  are an AI; her work is made by her, an AI agent. "Checked by the seller" is no such claim, nor is "for solo creators".
* legal certainty (LEGAL): "rechtssicher", "rechtskonform", a law's "-konform" ("GoBD-konform", "§ 556 BGB konform",
  "DSGVO-konform"), "legally compliant". A format norm's ("DIN 5008-konform") is a fact about the file she can check,
  and passes.
* no refunds (REFUNDS): "no refunds", "keine Rückerstattung", "Widerruf ausgeschlossen". For a digital product in
  Germany the law on withdrawal and warranty decides that (§§ 327 ff., 356 (5) BGB), never a line of the seller's.

Refused, not flagged for the owner: the owner's eye is the one that let the 13 through, none of these claims has a
true form Ember could check herself, and a refusal costs one rewrite in the same cycle. What a request tool reads is
the public text it sends (its action's words, a blog post's article, a book's cover texts): never the old words a
change quotes ("was: ..."), so a request that corrects a claim goes through. A request_approval is the owner's to
carry out, and its words in quotation marks are the ones it quotes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

_U = "(?:ü|ue|u)"
_A = "(?:ä|ae|a)"
# English: a verb of making, at most four words (none of them "by" or "and"), then "by" and a person
_MADE = r"(?:designed|made|created|crafted|written|drawn|illustrated|developed|built|produced|curated)"
_GAP = r"(?:,?\s+(?!by\b|and\b)[\w'’-]+){0,4}?"
_PERSON = (
    r"(?:(?:a|an|the|one|our|my|your)\s+)?"
    r"(?:(?:real|actual|human|solo|independent|indie|small|local|single|german|fellow)\s+)*"
    r"(?:humans?|persons?|people|(?:shop|store|etsy)[\s-]*owners?|owners?|sellers?|authors?|creators?|designers?"
    r"|artists?|makers?|teams?|teachers?|moms?|mums?|dads?|mothers?|fathers?)\b"
)
# German: "von" and a person, then the verb at most three words on ("von einem Menschen liebevoll gestaltet"), or the
# verb first ("gestaltet von einem Menschen")
_VERB_DE = r"(?:gestaltet|entworfen|erstellt|gemacht|geschrieben|entwickelt|gezeichnet|designt|kreiert|produziert)"
_PERSON_DE = (
    r"von\s+(?:(?:einem|einer|einen|der|dem|den|unserem|unserer|meinem|meiner)\s+)?"
    r"(?:(?:echten|realen|kleinen|selbstst" + _A + r"ndigen|unabh" + _A + r"ngigen)\s+)*"
    r"(?:menschen?|person(?:en)?|(?:shop|laden)?-?inhaber(?:in|innen)?|verk" + _A + r"ufer(?:in|innen)?"
    r"|autor(?:in|innen|en)?|designer(?:in|innen)?|k" + _U + r"nstler(?:in|innen)?|gestalter(?:in|innen)?|team"
    r"|mama|mutter|lehrer(?:in|innen)?)\b"
)
AUTHORSHIP = re.compile(
    rf"\b{_MADE}\b{_GAP}\s+by\s+{_PERSON}"
    rf"|\b{_PERSON_DE}(?:\s+[\w-]+){{0,3}}?\s+{_VERB_DE}\b"
    rf"|\b{_VERB_DE}\s+(?:[\w-]+\s+){{0,2}}?{_PERSON_DE}",
    re.IGNORECASE,
)
_LAWS = r"(?:gobd|dsgvo|gdpr|bgb|hgb|ustg|estg|heizkostenv|betrkv|geg|uwg|ddg|tmg|agb)"
# Every repetition bounded: a long run of one character takes linear time (tests/products/test_regex_timing.py)
LEGAL = re.compile(
    r"\brechts(?:sicher|konform|g" + _U + r"ltig)"
    r"|\b(?:\w{0,24}rechts|gesetz(?:es)?)[\s-]{0,3}konform"
    rf"|\b{_LAWS}[\s-]{{0,3}}(?:konform|compliant)"
    r"|§{1,2}\s{0,3}\d{1,4}[a-z]?(?:\s{1,3}(?:abs\.?|absatz)\s{0,3}\d{1,3})?(?:\s{1,3}\w{1,12})?[\s-]{0,3}"
    r"(?:konform|compliant)"
    r"|\blegally\s+(?:compliant|safe|binding|watertight|sound|valid|checked)\b"
    r"|\b(?:lawyer|attorney|anwalt)[\s-]*(?:approved|checked|reviewed|verified|gepr" + _U + r"ft)\b"
    r"|\banwaltlich\s+gepr" + _U + r"ft\b",
    re.IGNORECASE,
)
REFUNDS = re.compile(
    r"\bno\s+refunds?\b|\bnon[\s-]*refundable\b|\ball\s+sales\s+(?:are\s+)?final\b"
    r"|\brefunds?\s+(?:are\s+|is\s+)?(?:not\s+(?:possible|available|accepted|offered|given)|excluded)\b"
    r"|\bkeine\s+(?:r" + _U + r"ck)?erstattung(?:en)?\b"
    r"|\bkein(?:e|en)?\s+(?:umtausch|r" + _U + r"ckgabe|widerruf(?:srecht)?)\b"
    r"|\b(?:umtausch|r" + _U + r"ckgabe|widerruf(?:srecht)?|(?:r" + _U + r"ck)?erstattung)\s+(?:ist\s+|sind\s+)?"
    r"ausgeschlossen\b",
    re.IGNORECASE,
)
# A request_approval's quotations: what it quotes is no claim of its own
_QUOTED = re.compile(r"\"[^\"\n]{1,400}\"|“[^”\n]{1,400}”|„[^“”\n]{1,400}[“”]|«[^»\n]{1,400}»|‘[^’\n]{1,400}’")
INSTEAD = "Made by Ember, an AI agent, and checked by the seller"


def texts(value: Any) -> list[str]:
    """Every text in a request's action (its dicts and lists opened), the words it publishes."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in texts(item)]
    if isinstance(value, list | tuple):
        return [text for item in value for text in texts(item)]
    return []


def unquoted(text: str) -> str:
    """A request_approval's words without what it quotes."""
    return _QUOTED.sub(" ", text)


def _first(pattern: re.Pattern[str], words: str) -> str:
    found = pattern.search(words)
    return " ".join(found[0].split()) if found else ""


def refusal(words: Iterable[str]) -> str:
    """Why a public text may not go out as it is ("" when it may): each kind of claim it makes, with its first phrase
    and what to write instead."""
    text = "\n".join(words)
    said = []
    made = _first(AUTHORSHIP, text)
    if made:
        said.append(
            f'it says a person made it ("{made}"), but you made it, an AI agent (your constitution: always disclose '
            f'that you are an AI, and no misleading marketing): say so, e.g. "{INSTEAD}"'
        )
    legal = _first(LEGAL, text)
    if legal:
        said.append(
            f'it promises legal certainty ("{legal}"), which no one checked (you can\'t), and a wrong promise is '
            "misleading advertising your owner answers for (§ 5 UWG): say what it calculates or contains instead"
        )
    refunds = _first(REFUNDS, text)
    if refunds:
        said.append(
            f'it says "{refunds}": for a digital product the law on withdrawal and warranty decides that (§§ 327 ff., '
            "356 (5) BGB), not a line of yours: leave it out"
        )
    if not said:
        return ""
    return "; ".join(said) + ". Nothing went to your owner: write it again without that, and propose it again"


# 0.53.0 (evaluation 2026-10-27, C6): a sentence of the agent's own that names Ember as the maker ("Made by Ember, an AI
# agent, and checked by the seller before publication.", "This listing was made by Ember, an AI agent, ...", "Vorlage
# von Ember, einem KI-Agenten, erstellt."), which the line Ember's code adds to a description says already. Live, #144
# and #146 named the maker twice, once in her words and once in the code's. Each repetition is bounded (a long run of
# one character takes linear time, tests/products/test_regex_timing.py).
_MAKER = re.compile(
    r"(?:(?<=^)|(?<=[.!?]\s)|(?<=\n))[ \t]*"
    r"(?:(?:this|the)\s+[\w-]{1,30}\s+(?:was|is|has\s+been)\s+|(?:design|vorlage|buch|dieses\s+buch)\s+(?:wurde\s+)?)?"
    r"(?:(?:made|created|designed|written)\s+by\s+ember\b(?!['’])|von\s+ember,\s+einem\s+ki-agenten,)"
    r"[^.!?\n]{0,240}[.!?]?[ \t]*",
    re.IGNORECASE,
)


def without_maker(description: str) -> str:
    """0.53.0 (C6): a description without the agent's own sentences that name Ember its maker (``_MAKER``): Ember's
    code adds its own line, and one is enough. Her other words stay as they are."""
    kept = _MAKER.sub("", description)
    if kept == description:
        return description
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line.rstrip() for line in kept.splitlines())).strip()
