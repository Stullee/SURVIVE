"""0.46.0 (vision/generations.md): every query of the world's tables reads the world's session, and every query of her
mind's tables her mind's. Read from the code itself, as tests/cycle/test_rule_audit.py reads the rules: a query that
filters a world table by her mind's session reads nothing after a generation change (the Etsy sync asked Etsy for no
listing, the opt-outs were gone), and nothing else would notice until then.

What it reads: every SQL f-string in ``app/`` whose clause comes from ``AgentScope.where`` (her mind),
``world_where`` (the world) or ``minds_where`` (every mind of the world, for a platform's own limit), the table each
clause filters (by its alias, else the FROM before it), the parameters each execute passes, and the session a
statement on a world table names. A clause it can't place (a table named by a variable) isn't checked here: those
are covered by tests/generations/test_world.py."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from app.agent.store import WORLD_TABLES

APP = Path(__file__).resolve().parents[2] / "app"
WORLD = frozenset(WORLD_TABLES)
CLAUSES = ("where", "world_where", "minds_where")
TABLE = re.compile(r"\b(FROM|JOIN|INTO|UPDATE)\s+([a-z_]+)(?:\s+(?:AS\s+)?([a-z]\w*))?", re.I)
SAME_SESSION = re.compile(r"\b([a-z]\w*)\.session\s*=\s*([a-z]\w*)\.session\b")
# Clauses that read across the line on purpose: KDP's weekly titles and the mailbox's day count every mind's requests
# (minds_where); a listing's and the shop's daily numbers are the world's, a milestone's the mind's (observations).
CROSSING = {("approvals", "minds_where"), ("observations", "world_where")}


def _sql(node: ast.JoinedStr) -> tuple[str, list[tuple[int, str]]]:
    text, holes = "", []
    for part in node.values:
        if isinstance(part, ast.Constant):
            text += str(part.value)
        elif isinstance(part, ast.FormattedValue):
            name = ast.unparse(part.value)
            holes.append((len(text), name))
            text += "{" + name + "}"
    return text, holes


def _tables(text: str) -> list[tuple[int, str, str]]:
    return [(m.start(), m.group(2).lower(), (m.group(3) or "").lower()) for m in TABLE.finditer(text)]


def _filtered(text: str, at: int, alias: str | None) -> str | None:
    """The table a clause at ``at`` filters: by its alias, else the nearest FROM, JOIN or UPDATE before it."""
    found = _tables(text)
    if alias:
        named = [t for _, t, a in found if a == alias] or [t for _, t, _ in found if t == alias]
        return named[0] if named else None
    before = [t for start, t, _ in found if start < at]
    return before[-1] if before else None


def _clauses(fn: ast.AST) -> dict[str, list[tuple[int, str, str | None, str | None]]]:
    """Each clause variable's assignments in a function: (line, kind, alias, its parameters' variable)."""
    found: dict[str, list[tuple[int, str, str | None, str | None]]] = {}
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
            continue
        call = node.value
        if not (isinstance(call.func, ast.Attribute) and call.func.attr in CLAUSES):
            continue
        alias = str(call.args[0].value) if call.args and isinstance(call.args[0], ast.Constant) else None
        target = node.targets[0]
        if isinstance(target, ast.Tuple) and target.elts and isinstance(target.elts[0], ast.Name):
            params = target.elts[1].id if len(target.elts) > 1 and isinstance(target.elts[1], ast.Name) else None
            found.setdefault(target.elts[0].id, []).append((node.lineno, call.func.attr, alias, params))
    return found


def _functions() -> list[tuple[Path, ast.AST]]:
    out = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        out += [(path, fn) for fn in ast.walk(tree) if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))]
    return out


def _at(path: Path, line: int) -> str:
    return f"{path.relative_to(APP)}:{line}"


def test_each_clause_filters_a_table_of_its_own_kind() -> None:
    wrong = []
    for path, fn in _functions():
        clauses = _clauses(fn)
        for node in ast.walk(fn):
            if not isinstance(node, ast.JoinedStr):
                continue
            text, holes = _sql(node)
            for at, name in holes:
                made = [c for c in clauses.get(name, []) if c[0] <= node.lineno]
                if not made:
                    continue
                _, kind, alias, _ = max(made)
                table = _filtered(text, at, alias)
                if table is None or (table, kind) in CROSSING:
                    continue
                if (table in WORLD) != (kind == "world_where"):
                    wrong.append(f"{_at(path, node.lineno)}: {table} filtered by {kind}()")
    assert wrong == [], "a world table is read with world_where, a table of her mind's with where:\n" + "\n".join(wrong)


def test_a_statement_on_a_world_table_names_the_worlds_session() -> None:
    wrong = []
    for path in sorted(APP.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args):
                continue
            if node.func.attr not in ("execute", "executemany"):
                continue
            first = node.args[0]
            if isinstance(first, ast.JoinedStr):
                text = _sql(first)[0]
            elif isinstance(first, ast.Constant) and isinstance(first.value, str):
                text = first.value
            else:
                continue
            if not {t for _, t, _ in _tables(text)} & WORLD or not re.search(r"\bsession\b", text):
                continue
            passed = " ".join(ast.unparse(a) for a in node.args[1:])
            if re.search(r"\.session\b", passed):
                wrong.append(f"{_at(path, node.lineno)}: passes {passed[:80]}")
    assert wrong == [], "a world table's rows take scope.world_session, never the mind's:\n" + "\n".join(wrong)


def test_a_statement_passes_its_own_clauses_parameters() -> None:
    wrong = []
    for path, fn in _functions():
        clauses = _clauses(fn)
        pairs = {name: c[3] for name, made in clauses.items() for c in made if c[3]}
        if not pairs:
            continue
        carried: dict[str, set[str]] = {}  # a text built from a clause (joins, a condition) carries it on
        for node in sorted(
            (n for n in ast.walk(fn) if isinstance(n, ast.Assign)), key=lambda n: (n.lineno, n.col_offset)
        ):
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                names = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
                held = {n for n in names if n in pairs}
                for n in names:
                    held |= carried.get(n, set())
                if held:
                    carried[node.targets[0].id] = held
        params = set(pairs.values())
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args):
                continue
            if node.func.attr not in ("execute", "executemany"):
                continue
            used_names = {n.id for n in ast.walk(node.args[0]) if isinstance(n, ast.Name)}
            holes = {n for n in used_names if n in pairs}
            for n in used_names:
                holes |= carried.get(n, set())
            passed = {n.id for a in node.args[1:] for n in ast.walk(a) if isinstance(n, ast.Name) and n.id in params}
            if holes and {pairs[h] for h in holes} != passed:
                wrong.append(f"{_at(path, node.lineno)}: clauses {sorted(holes)}, parameters {sorted(passed)}")
    assert wrong == [], "each clause's parameters go with it:\n" + "\n".join(wrong)


def test_no_join_ties_a_world_row_to_a_minds_session() -> None:
    """A world row keeps its world's session and a mind's row its mind's: joined, the world's is at or below the
    mind's (a listing of the shop a later generation works in), never equal by rule."""
    wrong = []
    texts: list[tuple[str, str]] = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parts = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values}
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                texts.append((_at(path, node.lineno), _sql(node)[0]))
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in parts:
                texts.append((_at(path, node.lineno), node.value))
    for where, text in texts:
        aliases = {a or t: t for _, t, a in _tables(text)}
        for left, right in SAME_SESSION.findall(text):
            if left not in aliases or right not in aliases:
                continue  # a table it can't place
            kinds = {aliases[left] in WORLD, aliases[right] in WORLD}
            if kinds == {True, False}:
                wrong.append(f"{where}: {left}.session = {right}.session")
    assert wrong == [], "a world row joins a mind's at or below its session:\n" + "\n".join(wrong)


def test_the_databases_views_read_the_world_at_or_below_a_requests_session() -> None:
    """Migration 0102: NEVER's first contact and first publication, and an unlock's scope."""
    text = (APP / "migrations" / "0102_world_scope.sql").read_text(encoding="utf-8")
    for table, alias, request in (
        ("emails", "e", "r"),
        ("etsy_listings", "l", "r"),
        ("pinterest_boards", "b", "r"),
        ("printify_products", "p", "r"),
        ("etsy_listings", "l", "a"),
    ):
        assert f"FROM {table} {alias} WHERE {alias}.mode = {request}.mode AND {alias}.session <= {request}.session" in (
            text
        )
