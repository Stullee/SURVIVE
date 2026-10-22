"""0.42.2 (analysis-0.40.1, 5.6): the checks of a tool call's input, split out of tools.py.

``validate`` checks a call against its Spec: each field's type, length and values (a text too long is cut with a note
where its Field says so, else refused). ``unleaked`` takes out of a journal's texts the calls of other tools the model
wrote inside them (0.19.2, 0.24.0), which tools.run runs only when they only read. ``_noted`` adds what a check cut to
the call's answer, and ``_clip`` cuts an answer to what a result holds.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from .tools_context import Outcome, ToolError
from .tools_limits import _BAD_CHARS, CALL_CHARS, MAX_RESULT_CHARS, WHOLE_MAX
from .tools_specs import SPECS, Field, Spec

# 0.19.2: a call's next text written inside the one before it, as the model's own markup ("...</entry>\n<parameter
# name="next">..."): live, five journals kept their handoff inside the entry, and the next plan had none.
_LEAKED = re.compile(r'<parameter name="([a-z_]{1,40})">')
# 0.24.0: another tool's whole call written inside a text, its last field's text beside this call's fields:
# '...</entry>\n<parameter name="next">...</parameter>\n</invoke>\n<invoke name="memory_update">\n<parameter
# name="file">lessons', with "content" and "mode" given as if they were this call's. Live, two reflections lost their
# journal to it ("unknown field 'content'", "unknown field 'next_step'"), with the lessons or the project update in it.
_INVOKE = re.compile(r'</invoke>\s*<invoke name="([a-z_]{1,40})">', re.IGNORECASE)
# 0.40.3 (analysis-0.40.1 codebase, T1): a closing tag inside a field's text (its own closes it, at its end), and one
# that ends a text (searched in its last 43 characters, the longest a closing tag is: _unclosed)
_CLOSED = re.compile(r"</[a-z_:]{1,40}>", re.IGNORECASE)
_CLOSED_END = re.compile(r"</[a-z_:]{1,40}>\Z", re.IGNORECASE)
# 0.39.4: why a call taken out of a journal that doesn't only read isn't run (run). The model can tell its own call from
# words it quoted; Ember's code can't.
TAKEN_OUT = (
    "only a call that reads runs from inside a journal, as a journal can quote what someone else wrote (an email, a "
    "web page). If you meant this call yourself, make it as a call of its own"
)


def unleaked(spec: Spec, raw: Any, notes: list[str], leaked: list[tuple[str, dict[str, Any]]] | None = None) -> Any:
    """The journal's input with each text field another one's markup holds moved to its own field (unless that field
    is given), when every such mark names another text field of the journal (0.40.3: and is the model's own slip, not
    a quote: _slipped); what was moved is noted. 0.24.0: with ``leaked`` (a list), a call of another tool written
    inside a text is taken out of it, with the fields it was given beside this call's, and added to ``leaked`` (run
    says whether it runs). 0.39.4: in the journal only, whose texts are the model's own words: any other tool's text
    can quote someone else word for word (a buyer's email saved to notes, a passage quoted to the owner), and markup in
    it is part of the quote, left as it is."""
    if not isinstance(raw, dict) or spec.name != "write_journal":
        return raw
    fixed = dict(raw)
    if leaked is not None:
        _take_out(spec, raw, fixed, notes, leaked)
    for name, value in list(fixed.items()):
        field = spec.fields.get(name)
        if field is None or field.type != "string" or not isinstance(value, str):
            continue
        marks = list(_LEAKED.finditer(value))
        if (
            not marks
            or any(m[1] == name or m[1] not in spec.fields or spec.fields[m[1]].type != "string" for m in marks)
            or not _slipped(value, marks)
        ):
            continue
        fixed[name] = _unclosed(value[: marks[0].start()]).rstrip()
        for mark, after in zip(marks, [*marks[1:], None], strict=True):
            text = _unclosed(value[mark.end() : after.start() if after else len(value)]).strip()
            given = raw.get(mark[1])
            if text and not (isinstance(given, str) and given.strip()):
                fixed[mark[1]] = text
                notes.append(f"Ember's code moved the {mark[1]} you wrote inside {name} to {mark[1]}")
    return fixed


def _slipped(value: str, marks: list[re.Match[str]]) -> bool:
    """0.40.3 (analysis-0.40.1 codebase, T1): whether the marks in a journal's text are the model's own slip at the top
    level ("...</entry>\\n<parameter name="next">..."): each right after a closing tag and outside any quotation or
    code block, each one's text holding no closing tag but at its end. 0.19.2 moved any mark: a buyer's email quoted
    word for word in the entry, holding '<parameter name="next">', became the handoff the next plan reads in Ember's
    own voice. Quoted markup stays in the text, as in every other tool's since 0.39.4."""
    for mark, after in zip(marks, [*marks[1:], None], strict=True):
        before = value[: mark.start()].rstrip()
        if not _CLOSED_END.search(before[-43:]) or _inside(before):
            return False
        if _CLOSED.search(_unclosed(value[mark.end() : after.start() if after else len(value)])):
            return False
    return True


def _unclosed(text: str) -> str:
    """0.42.0 (analysis-0.40.1 codebase, 9): ``text`` without the closing tags at its end and the spaces before them
    ('...</entry>\\n</parameter>\\n'), or as it is when it ends in none, in linear time. The pattern that took them off
    (0.19.2) was tried at each position of a run of spaces, which takes the run's length squared: 20,000 spaces after
    a field's mark took 0.6 s, and a journal's raw input is read before its lengths are checked."""
    end, found = len(text), False
    while True:
        while end and text[end - 1].isspace():
            end -= 1
        tag = _CLOSED_END.search(text, max(0, end - 43), end)
        if tag is None:
            return text[:end] if found else text
        end, found = tag.start(), True


def _inside(text: str) -> bool:
    """0.40.3 (T1): whether the end of ``text`` lies inside a quotation or a code block (opened and not closed)."""
    return (
        text.count('"') % 2 == 1
        or sum(text.count(c) for c in "„“”") % 2 == 1
        or text.count("«") != text.count("»")
        or text.count("```") % 2 == 1
    )


def _take_out(
    spec: Spec, raw: dict[str, Any], fixed: dict[str, Any], notes: list[str], leaked: list[tuple[str, dict[str, Any]]]
) -> None:
    """0.24.0: the first call of another tool written inside one of the journal's texts, taken out of ``fixed`` (the
    text cut where it starts, its fields the journal doesn't have removed) into ``leaked``."""
    for name, value in raw.items():
        field = spec.fields.get(name)
        if field is None or field.type != "string" or not isinstance(value, str):
            continue
        found = _INVOKE.search(value)
        if found is None or found[1] not in SPECS or found[1] == spec.name:
            continue
        other = SPECS[found[1]]
        call: dict[str, Any] = {}
        tail = value[found.end() :]
        marks = list(_LEAKED.finditer(tail))
        # 0.39.4: a call with no field written after it (all beside the journal's, or none) failed the journal
        for mark, after in zip(marks, [*marks[1:], None] if marks else [], strict=True):
            if mark[1] in other.fields:
                call[mark[1]] = _unclosed(tail[mark.end() : after.start() if after else len(tail)]).strip()
        for key in raw:
            if key not in spec.fields and key in other.fields:
                call[key] = fixed.pop(key)
        fixed[name] = value[: found.start()]
        leaked.append((found[1], _typed(other, call)))
        notes.append(f"Ember's code took the {found[1]} call you wrote inside {name} out of it")
        return


def _typed(spec: Spec, call: dict[str, Any]) -> dict[str, Any]:
    """A call taken out of a text, its whole numbers and true or false as their fields take them (markup holds text)."""
    for key, value in call.items():
        kind = spec.fields[key].type if key in spec.fields else ""
        if not isinstance(value, str):
            continue
        if kind == "integer" and re.fullmatch(r"-?\d{1,12}", value.strip()):
            call[key] = int(value.strip())
        elif kind == "boolean" and value.strip().lower() in ("true", "false"):
            call[key] = value.strip().lower() == "true"
    return call


def validate(spec: Spec, raw: Any, notes: list[str] | None = None) -> dict[str, Any]:
    """The checked arguments; a too-long ``cut`` field is shortened and described in ``notes``. 0.15.0: a call whose
    texts together are longer than one reply holds (CALL_CHARS) is refused, with their lengths."""
    args = _checked(spec.fields, raw, notes)
    sizes = {
        name: len(value)
        if isinstance(value, str)
        else sum(len(v) for item in value for v in item.values() if isinstance(v, str))
        for name, value in args.items()
        if isinstance(value, (str, list))
    }
    total = sum(sizes.values())
    if total > CALL_CHARS:
        longest = ", ".join(f"{name} {size:,}" for name, size in sorted(sizes.items(), key=lambda s: -s[1])[:3])
        raise ToolError(
            f"{spec.name}'s texts total {total:,} characters ({longest}): one call holds at most {CALL_CHARS:,}, or "
            "the reply is cut off. Shorten them, or split the work into two calls"
        )
    return args


def _checked(fields: dict[str, Field], raw: Any, notes: list[str] | None, where: str = "") -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ToolError(f"{where or 'the input'} must be an object")
    unknown = sorted(set(raw) - set(fields))
    if unknown:
        raise ToolError(f"unknown field {unknown[0]!r}" + (f" in {where}" if where else ""))
    args: dict[str, Any] = {}
    for name, f in fields.items():
        value = raw.get(name)
        # 0.40.3 (analysis-0.40.1 codebase, T7): a blank optional text is left out like an empty one (project_update's
        # hypothesis " " failed the tool with an IntegrityError, and no reason)
        blank = isinstance(value, str) and not (value if f.exact else value.strip())
        if value is None or (f.type == "string" and blank and not f.required):
            if f.required:
                raise ToolError(f"{name} is required")
            continue
        if f.type == "string":
            if not isinstance(value, str):
                raise ToolError(f"{name} must be text")
            if _BAD_CHARS.search(value):
                raise ToolError(f"{name} contains control or direction characters")
            if f.max_len and len(value) > f.max_len:
                if not f.cut:  # 0.15.0: with the numbers (and what to do), so the next try fits
                    remedy = f": {f.remedy}" if f.remedy else ""
                    # 0.39.1: and how much to cut (live, commits of 101 characters was refused, and a lesson kept)
                    over = f"(cut {len(value) - f.max_len:,})"
                    raise ToolError(f"{name} is too long: {len(value):,} of {f.max_len:,} characters {over}{remedy}")
                if notes is not None:  # 0.42.0: an item's text says which item ("steps item 2's title")
                    named = f"{where}'s {name}" if where else name
                    notes.append(f"{named} was cut to {f.max_len:,} of its {len(value):,} characters")
                value = value[: f.max_len - 1].rstrip() + "…"
            if f.enum and value not in f.enum:
                raise ToolError(f"{name} must be one of {', '.join(f.enum)}")
            if f.required and not value.strip():
                raise ToolError(f"{name} is empty")
        elif f.type == "boolean":
            if not isinstance(value, bool):
                raise ToolError(f"{name} must be true or false")
        elif f.type == "array":  # 0.12.0: each item checked like the input itself
            if not isinstance(value, list) or not value:
                raise ToolError(f"{name} must be a list of 1 to {f.max_len} objects")
            if len(value) > f.max_len:
                raise ToolError(f"{name} holds at most {f.max_len} items")
            value = [_checked(dict(f.items), item, notes, f"{name} item {i}") for i, item in enumerate(value, 1)]
        else:
            if not isinstance(value, int) or isinstance(value, bool):
                raise ToolError(f"{name} must be a whole number")
            if f.minimum is not None and value < f.minimum:
                raise ToolError(f"{name} must be at least {f.minimum}")
            if f.maximum is not None and value > f.maximum:
                raise ToolError(f"{name} must be at most {f.maximum}")
            if abs(value) > WHOLE_MAX:  # 0.40.3 (analysis-0.40.1 codebase, T7)
                raise ToolError(f"{name} must be between {-WHOLE_MAX:,} and {WHOLE_MAX:,}")
        args[name] = value
    return args


def _noted(outcome: Outcome, notes: list[str]) -> Outcome:
    """Say what was cut, so the model learns the limit without a refused call and a retry."""
    if not notes or not outcome.ok:
        return outcome
    return replace(outcome, text=f"{outcome.text} (Saved, but {'; '.join(notes)}.)")


def _clip(outcome: Outcome, limit: int = MAX_RESULT_CHARS) -> Outcome:
    if len(outcome.text) <= limit:
        return outcome
    return replace(outcome, text=outcome.text[: limit - 20] + "\n[… result cut]")
