"""VOTEBOT-75 (PLAN-state-specific-handling, the guard): no new state rule in VoteBot's code.

One canonical list of jurisdictions (ddp-open-states `jurisdictions.yaml`, Ramon 2026-11-10: "every service must read from
the canonical manifest of jurisdictions") and one home for state rules (the ETL: openstates-scrapers / openstates-core).
This test reads every module under src/ (never imports it) and fails on

  * a module- or class-level table (dict, set, frozenset, list, tuple) whose keys or members are two or more state codes;
  * a comparison (==, !=, in, not in; each link of a chain) with a state-code literal: `code == "fl"`, `x in ("ne", "la")`.

"us"/"usa" are federal, not states: federal branches stay (plan section 4.3). What is allowed today is named by code (a
table's name, a comparison's function), never by state, each with its reason, so adding one is a decision someone
writes down. The broker (BROKER-493) and ddp-sync (SYNC-159) have the same guard.
"""

from __future__ import annotations

import ast
import pathlib
from collections import Counter

SRC = pathlib.Path(__file__).resolve().parents[2] / "src"
CODES = set(
    "al ak az ar ca co ct de fl ga hi id il in ia ks ky la me md ma mi mn ms mo mt ne nv nh nj nm ny nc nd oh ok or "  # noqa: SIM905
    "pa ri sc sd tn tx ut vt va wa wv wi wy dc pr us usa".split()
)
FEDERAL = {"us", "usa"}

# (module path under src/, table name): why it is not a hand-kept list of the states we serve
ALLOWED_TABLES = {
    ("votebot/utils/jurisdictions.py", "STATE_NAME_TO_CODE"): (
        "reference data for EVERY state (name to code); it does not change when a state is added"
    ),
    ("votebot/core/retrieval.py", "_CODES_READ_IN_ANY_CASE"): (
        "a rule for reading what users type: these nine codes are read even in lower case ('fl hb 363'); every other "
        "code needs capitals because it is also an English word ('in', 'or', 'me'). It is the nine original states, so "
        "a new state is read only when capitalised; changing that is a product decision, not a manifest list"
    ),
}

# (module path under src/, enclosing scope as Class.function, code): (exact count, why it is allowed). None today.
ALLOWED_COMPARISONS: dict[tuple[str, str, str], tuple[int, str]] = {}


def _strings(node) -> list[str]:
    if isinstance(node, ast.Dict):
        return [n.value for n in (*node.keys, *node.values) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        return [n.value for n in node.elts if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    if isinstance(node, ast.Call) and getattr(node.func, "id", "") in ("frozenset", "set", "tuple", "list") and node.args:
        return _strings(node.args[0])
    return []


def tables(source: str, rel: str) -> list[tuple[str, str]]:
    """(module, name) for each module- or class-level literal table keyed by two or more distinct state codes."""
    tree = ast.parse(source)
    found = []
    for body in [tree.body, *[c.body for c in tree.body if isinstance(c, ast.ClassDef)]]:
        for node in body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                states = {("us" if s.lower() == "usa" else s.lower()) for s in _strings(node.value) if s.lower() in CODES}
                if len(states) >= 2:
                    found += [(rel, t.id) for t in targets if isinstance(t, ast.Name)]
    return found


def _literals(node) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return [n.value for n in node.elts if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    return []


def comparisons(source: str, rel: str) -> list[tuple[str, str, str]]:
    """(module, Class.function, code) for each comparison with a non-federal state-code literal."""
    found = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.stack = []

        def _scope(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scope  # noqa: N815 (ast.NodeVisitor names)

        def visit_Compare(self, node):
            operands = [node.left, *node.comparators]
            codes = {
                value.lower()
                for op, left, right in zip(node.ops, operands, operands[1:], strict=False)
                if isinstance(op, (ast.Eq, ast.NotEq, ast.In, ast.NotIn))
                for side in (left, right)
                for value in _literals(side)
                if value.lower() in CODES and value.lower() not in FEDERAL
            }
            scope = ".".join(self.stack) or "<module>"
            found.extend((rel, scope, code) for code in sorted(codes))
            self.generic_visit(node)

    Visitor().visit(ast.parse(source))
    return found


def _modules():
    for path in sorted(SRC.rglob("*.py")):
        yield str(path.relative_to(SRC)), path.read_text(encoding="utf-8-sig")


def _comparison_offenders(found):
    counts = Counter(found)
    return sorted((key, n) for key, n in counts.items() if n > ALLOWED_COMPARISONS.get(key, (0, ""))[0])


def test_no_module_keeps_a_table_of_states():
    offenders = [t for rel, source in _modules() for t in tables(source, rel) if t not in ALLOWED_TABLES]
    assert offenders == [], f"a per-state table in VoteBot: read the manifest instead (VOTEBOT-75): {offenders}"


def test_no_module_compares_with_a_state_code():
    found = [c for rel, source in _modules() for c in comparisons(source, rel)]
    assert _comparison_offenders(found) == [], (
        "a comparison with a state code in VoteBot: a state rule belongs in the ETL or the manifest (VOTEBOT-75)"
    )


def test_the_guard_catches_a_planted_table_and_comparison():
    planted = '''
LIVE_STATES = {"fl", "ma"}
TIME_LIMIT = {"MA": 2, "NE": 1, "default": 3}
NAMES = {"Florida": "FL"}
FEDERAL = ("us", "usa")
class Prompts:
    EXTRA = ["pa", "ne"]
    def line(self, code, status):
        if code == "fl":
            return "funding"
        if code in ("ne", "la") or "" < code == "WA":
            return "one house"
        if code == "us" or status == "done":
            return "federal or not a code"
'''
    assert tables(planted, "new.py") == [("new.py", "LIVE_STATES"), ("new.py", "TIME_LIMIT"), ("new.py", "EXTRA")]
    assert comparisons(planted, "new.py") == [
        ("new.py", "Prompts.line", "fl"), ("new.py", "Prompts.line", "la"), ("new.py", "Prompts.line", "ne"),
        ("new.py", "Prompts.line", "wa"),
    ]


def test_an_allowed_comparison_covers_only_its_count():
    key = ("votebot/x.py", "f", "fl")
    ALLOWED_COMPARISONS[key] = (1, "test only")
    try:
        assert _comparison_offenders([key]) == []
        assert _comparison_offenders([key, key]) == [(key, 2)]
    finally:
        del ALLOWED_COMPARISONS[key]


def test_every_allowance_still_matches_something():
    """An allowance for a table or comparison that is gone should be removed, not left to excuse a new one."""
    seen_tables = {t for rel, source in _modules() for t in tables(source, rel)}
    assert set(ALLOWED_TABLES) - seen_tables == set()
    counts = Counter(c for rel, source in _modules() for c in comparisons(source, rel))
    assert {k: counts.get(k, 0) for k in ALLOWED_COMPARISONS} == {k: n for k, (n, _) in ALLOWED_COMPARISONS.items()}
