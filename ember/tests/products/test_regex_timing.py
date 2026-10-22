"""0.42.2 (regex): every regular expression of app/ takes linear time on long runs (analysis-0.40.1 audit).

0.40.3 timed the patterns of the products and the integrations (6.2 of the codebase analysis of 0.40.1). The workshop's
_NAMED, which reads the file names in a helper's whole answer, was in neither package: 10,000 dots took it 3.6 s, twice
as many four times as long, holding the app. This check looks at every module of app/: the patterns it keeps (a
constant, or one in a tuple or list or dict of them) and each pattern a re function in it is given as it is written. A
pattern put together while Ember runs (from a word list, a title, an id) isn't seen.

Each pattern gets long runs after what leads into each of its repetitions, at 5,000 and at 20,000 characters: four
times the text must take less than ten times as long (linear takes four times as long, quadratic sixteen). Timed by how
the time grows, as 0.40.3's emphasis test is, not by a bound in seconds: a busy CI runner is slower at both lengths.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
import re
import string
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from re import _constants as sre  # the parser's names for the parts of a pattern
from re import _parser
from typing import Any

import pytest

import app
from app.agent import tools
from app.integrations import bluesky, mail
from app.products import blog

RUNS = " %[#-|:*\n\t1a"  # the analysis's runs, a digit and a letter
LONG = 20_000
SLACK = 0.02  # seconds a pause of a busy runner may add to the longer text's time
# Matched at one place (match), never searched: timed with match. A pattern given to re.match or re.fullmatch as it is
# written is timed so without being listed.
ANCHORED = {
    "app.products.checks._LINE_END": "where a stream keyword ends (checks._pdf counts what it reads)",
    "app.products.sheets._TOKENS": "at a formula's next token (sheets._tokens)",
    "app.products.site._STREET": "on each line of the owner's address",
    "app.integrations.mail._CONTAINER": "at the start of a part's header",
}
# Applied only to short texts: not timed (test_the_texts_of_the_bounded_patterns_are_short checks the bounds).
BOUNDED = {
    "app.products.blog._EMAIL": "a post's texts, at most blog.SOURCE_MAX (24,000) characters: about 1 s at worst",
    "app.integrations.bluesky._TAG_END": "a hashtag in a post's words, at most bluesky.WORDS_CHARS characters",
    "app.integrations.qa._THREAD": "an email's subject, at most mail.SUBJECT_MAX (150) characters",
    # 0.42.2: EVIDENCE starts a path after each '/' of 'a/a/a/...', QUOTED looks for the closing quote after each
    # opening one: no pattern with the same matches avoids that. tools.validate refuses or cuts a longer text first.
    "app.agent.tools.EVIDENCE": "a done's result, at most 600 characters (its maxLength): 2 ms at worst",
    "app.agent.tools.QUOTED": "a message to the owner, at most 2,000 characters (its maxLength): 25 ms at worst",
}
_CATEGORIES = {
    sre.CATEGORY_DIGIT: str.isdigit,
    sre.CATEGORY_NOT_DIGIT: lambda c: not c.isdigit(),
    sre.CATEGORY_SPACE: str.isspace,
    sre.CATEGORY_NOT_SPACE: lambda c: not c.isspace(),
    sre.CATEGORY_WORD: lambda c: c.isalnum() or c == "_",
    sre.CATEGORY_NOT_WORD: lambda c: not (c.isalnum() or c == "_"),
}
# The re functions a pattern is given to, and where their flags go when they aren't named
_FLAGS_AT = {"compile": 1, "search": 2, "match": 2, "fullmatch": 2, "findall": 2, "finditer": 2, "split": 3}
_FLAGS_AT |= {"sub": 4, "subn": 4}


def _fits(op: Any, av: Any, char: str) -> bool:
    """Whether a character is one a single-character part of a pattern matches."""
    if op is sre.LITERAL:
        return ord(char) == av
    if op is sre.NOT_LITERAL:
        return ord(char) != av
    if op is sre.ANY:
        return char != "\n"
    if op is sre.RANGE:
        return av[0] <= ord(char) <= av[1]
    if op is sre.CATEGORY:
        return _CATEGORIES[av](char)
    if op is sre.IN:
        negate = bool(av) and av[0][0] is sre.NEGATE
        return negate != any(_fits(o, a, char) for o, a in av[negate:])
    return False


def _leads(pattern: re.Pattern[Any]) -> set[tuple[str, str]]:
    """What leads into each repetition of a pattern, and one repetition of it: texts its parts match, sampled ('['
    and '/a' for '\\[(?:/\\w+)*', '# ' and 'a' for a heading's '^(#{1,6})\\s+(.*?)...'). A long run after the lead is
    what makes a pattern backtrack: P1 needed the '[', PR2 the '# a'."""
    found = {("", "")}

    def walk(items: Any, text: str | None) -> str | None:
        for op, av in items:
            if text is None:
                return None
            if op in (sre.MAX_REPEAT, sre.MIN_REPEAT, sre.POSSESSIVE_REPEAT):
                once = walk(av[2], text)
                found.update({(text, once[len(text) :]), (once, "")} if once is not None else {(text, "")})
                text = None if once is None else text + once[len(text) :] * av[0]
            elif op is sre.SUBPATTERN:
                text = walk(av[3], text)
            elif op is sre.ATOMIC_GROUP:
                text = walk(av, text)
            elif op is sre.BRANCH:
                text = [walk(branch, text) for branch in av[1]][0]
            elif op in (sre.LITERAL, sre.NOT_LITERAL, sre.ANY, sre.IN):
                char = next((c for c in "a1 " + RUNS + string.printable if _fits(op, av, c)), None)
                text = None if char is None else text + char
            elif op not in (sre.AT, sre.ASSERT, sre.ASSERT_NOT):
                return None
        return text

    walk(_parser.parse(pattern.pattern, pattern.flags), "")
    return found


def _inputs(pattern: re.Pattern[Any]) -> set[tuple[str, str]]:
    """(lead, unit): a text to time is the lead, then the unit again and again (a run), then '\\x01'."""
    source = pattern.pattern if isinstance(pattern.pattern, str) else pattern.pattern.decode("latin-1")
    found = {("", c) for c in set(source) - set(RUNS)}  # the pattern's own characters
    for lead, unit in _leads(pattern):
        found |= {(lead, c) for c in RUNS} | ({(lead, unit)} if unit else set())
    return found


def _text(pattern: re.Pattern[Any], lead: str, unit: str, size: int) -> Any:
    text = lead + unit * (size // len(unit)) + "\x01"
    return text.encode("latin-1", "replace") if isinstance(pattern.pattern, bytes) else text


def _flags(node: ast.expr) -> int:
    """The flags a re function is given (re.IGNORECASE | re.M); ValueError for ones worked out while Ember runs."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _flags(node.left) | _flags(node.right)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "re":
        return int(getattr(re, node.attr))
    raise ValueError(ast.unparse(node))


def _calls(node: ast.AST, where: str) -> Iterator[tuple[ast.Call, str]]:
    """The re functions called in a module's code, each with the function or class it is called in."""
    for inner in ast.iter_child_nodes(node):
        if (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and isinstance(inner.func.value, ast.Name)
            and inner.func.value.id == "re"
            and inner.func.attr in _FLAGS_AT
        ):
            yield inner, where
        named = isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        yield from _calls(inner, inner.name if named else where)


def _given(module: Any) -> Iterator[tuple[str, re.Pattern[Any], str]]:
    """The patterns a module's code gives a re function as they are written: (where, the pattern, the function). One
    put together from other texts (an f-string, a join) isn't seen."""
    for call, where in _calls(ast.parse(Path(module.__file__).read_text(encoding="utf-8")), ""):
        if not call.args or not isinstance(call.args[0], ast.Constant):
            continue
        flags = [k.value for k in call.keywords if k.arg == "flags"] or call.args[_FLAGS_AT[call.func.attr] :][:1]
        try:
            pattern = re.compile(call.args[0].value, _flags(flags[0]) if flags else 0)
        except ValueError:
            continue
        name = f"{module.__name__}.{where or '<module>'}: re.{call.func.attr}({call.args[0].value!r})"
        yield name, pattern, call.func.attr


def _kept(value: Any) -> list[tuple[str, re.Pattern[Any]]]:
    """A module constant's patterns: itself, or those in a tuple, list or dict of them (or of tuples holding one)."""
    if isinstance(value, re.Pattern):
        return [("", value)]
    items = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, (tuple, list)) else []
    return [
        (f"[{key!r}]", part)
        for key, item in items
        for part in (item if isinstance(item, tuple) else (item,))
        if isinstance(part, re.Pattern)
    ]


def _patterns() -> dict[str, tuple[re.Pattern[Any], bool]]:
    """Every pattern of app/'s modules, once, by a name (the one ANCHORED or BOUNDED gives it, if they do), and whether
    it is only ever matched at one place: listed in ANCHORED, and given to no re function that searches."""
    kept: dict[tuple[Any, int], tuple[str, re.Pattern[Any], bool]] = {}
    modules = [importlib.import_module(info.name) for info in pkgutil.walk_packages(app.__path__, "app.")]
    for module in modules:
        for constant, value in vars(module).items():
            for key, pattern in _kept(value):
                name = f"{module.__name__}.{constant}{key}"  # one imported from another module: the same pattern
                first, _, anchored = kept.get((pattern.pattern, pattern.flags), (name, pattern, False))
                listed = name in ANCHORED or name in BOUNDED
                kept[(pattern.pattern, pattern.flags)] = (
                    name if listed else first,
                    pattern,
                    anchored or name in ANCHORED,
                )
    for module in modules:
        for name, pattern, function in _given(module):
            if function == "compile" and (pattern.pattern, pattern.flags) in kept:
                continue  # a constant's: as ANCHORED says
            first, _, before = kept.get((pattern.pattern, pattern.flags), (name, pattern, True))
            kept[(pattern.pattern, pattern.flags)] = (first, pattern, before and function in ("match", "fullmatch"))
    return {name: (pattern, anchored) for name, pattern, anchored in kept.values()}


def _took(apply: Callable[[Any], Any], text: Any) -> float:
    started = time.perf_counter()
    apply(text)
    return time.perf_counter() - started


def test_every_pattern_of_the_app_takes_linear_time() -> None:
    """P1 and PR2 (0.40.3) and _NAMED (0.42.2) were patterns that backtracked on a long run of one character, holding
    the app: each pattern of app/ gets long runs after what leads into each of its repetitions, and four times the text
    must take less than ten times as long. A pattern only ever matched at one place is timed so; one only applied to
    short texts is listed in BOUNDED, with why."""
    patterns = _patterns()
    assert len(patterns) > 300 and set(ANCHORED) | set(BOUNDED) <= set(patterns)
    slow = []
    for name, (pattern, anchored) in sorted(patterns.items()):
        if name in BOUNDED:
            continue
        apply = pattern.match if anchored else pattern.search
        for lead, unit in sorted(_inputs(pattern)):
            short, long = (_text(pattern, lead, unit, size) for size in (LONG // 4, LONG))
            for _ in range(3):  # a pause of a busy runner can fall on the longer text once, not three times
                after = _took(apply, long)
                before = _took(apply, short) if after >= SLACK else 0.0  # quicker than SLACK: linear or not, it's fine
                if after < 10 * before + SLACK:
                    break
            else:
                slow.append(f"{name}: {before:.2f} s, then {after:.2f} s on {long[:24]!r}... ({LONG:,} characters)")
                break  # one text shows it
    assert not slow, "\n".join(slow)


def test_the_texts_of_the_bounded_patterns_are_short() -> None:
    """BOUNDED's bounds, as the code holds them: a longer text is refused or cut before the pattern reads it."""
    assert blog.SOURCE_MAX <= 24_000 and max(bluesky.WORDS_CHARS.values()) <= 300 and mail.SUBJECT_MAX <= 150
    done = [tools.SPECS[name].fields["result"].max_len for name in ("milestone_update", "obligation_done")]
    assert 0 < max(done) <= 600  # EVIDENCE
    assert 0 < tools.SPECS["message_owner"].fields["text"].max_len <= 2_000  # QUOTED
    with pytest.raises(tools.ToolError, match="too long"):
        tools.validate(tools.SPECS["message_owner"], {"text": "„" * 2_001})
