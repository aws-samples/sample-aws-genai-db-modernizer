"""Renderers only format; the facts layer computes (#615, ADR-032).

Synthesis computes every customer-facing figure once
(``src.shared.report_facts.compute_report_facts``) into ``report.json``'s
``facts``; the deliverables read it through ``src.report.facts.report_facts``.
These guards keep it that way:

- nothing under ``src/report/`` imports ``src.agents`` (the renderers format
  ``report.json``, they never re-run a pipeline step), and the facts layer
  imports neither ``src.agents`` nor ``src.report``;
- no renderer counts, totals or does arithmetic on a figure the facts layer
  owns.

The arithmetic guard is a small taint check over each renderer function's
AST, not a text search, so it follows the data rather than the spelling:

- the *owned keys* are every figure field of the facts contract
  (``ReportFacts`` and the models it nests, walked recursively: numbers,
  number maps, nested records and lists of records), so a new fact is owned
  the moment it is added to the contract, plus the ``report.json`` inputs the
  facts are computed from (``INPUT_KEYS``) and the architecture row keys
  built from facts (``ROW_KEYS``);
- a *source* is a read of an owned key (``x["table_count"]``,
  ``x.get("owned_counts_by_engine")``), a call to a helper that returns a
  facts-owned list (``filtered_risks(report)``, ...), any name assigned from
  an expression holding one (tuple unpacking included), and the target of a
  ``for`` loop or comprehension over one;
- a *sink* is ``len``/``sum``/``max``/``min``/``Counter``/
  ``count_by_severity``/``.count()`` over a source, ``+``/``-``/``*``/``/``
  with a source on either side, an augmented assignment of a source, and any
  arithmetic inside a ``for`` loop over a source (``n += 1``,
  ``d[k] = d.get(k, 0) + 1``: counting what the loop walks).

The facts block itself is ``report_facts(report)``, ``report["facts"]`` or
``.get("facts")``, a name assigned from either (``dict(...)`` of one
included), or a function parameter named ``facts``.

Known limits, deliberately not traced: ``functools.reduce``, ``math.fsum``,
``%`` and ``**``, the ``enumerate`` counter used as a count, and a helper that
takes a fact *list* under another name as a parameter (only
``count_by_severity`` is a sink wherever it is called).

Counting what a list *shows* (``"+N more"`` after the first 20 names) does
not touch an owned key and is not flagged. A line that genuinely computes on
a fact for presentation only (chart geometry, verb agreement) says so with
``# facts: presentation (<reason>)``; a pragma with no reason is itself a
violation, and the exemptions in use are pinned in ``PINNED_EXEMPTIONS`` so
a new one is reviewed. The legacy wave builder (``src/report/waves.py``, for
a report synthesized before ``migration_waves`` existed) produces data, not
output, and is exempt, like the facts reader itself.
"""

from __future__ import annotations

import ast
import re
import types
import typing
from pathlib import Path

from pydantic import BaseModel

from src.contracts.synthesis_output import ReportFacts

REPO = Path(__file__).resolve().parents[3]
REPORT = REPO / "src" / "report"
FACTS_LAYER = REPO / "src" / "shared" / "report_facts.py"

# Modules that compute data rather than format it.
EXEMPT = {"facts.py", "waves.py"}

# report.json fields the facts are computed from: reading one is fine,
# computing on it is the facts layer's job.
INPUT_KEYS = frozenset(
    {
        "table_count",
        "table_owners",
        "owned_counts_by_engine",
        "table_counts_by_engine",
        "cost_breakdown",
        "monthly_cost_usd",
        "workload_percent",
        "cache_overlay_queries",
        "cache_call_share_percent",
        "tied_queries",
        "risks",
        "resolved_risks",
        "table_mappings",
        "post_schema_reroutes",
        "ranking",
        "trade_offs",
        "query_traces",
    }
)
# report.json inputs that are records of lists (#339 review): every field read
# off one, at any depth, is owned too -- ``report["query_traces"]["queries"]``,
# and ``digest["queries"]`` after ``digest = report.get("query_traces")`` --
# so a renderer cannot recount the listed traces behind ``facts.query_trace``.
RECORD_INPUT_KEYS = frozenset({"query_traces"})
# The architecture rows ``architecture._architecture_engines`` builds from facts.
ROW_KEYS = frozenset({"migrates", "workload", "cost", "cached_queries", "cached_call_share"})
# Facts fields whose name renderers also use for an unrelated value, so owning
# the name everywhere would flag that value instead: ``engines`` (also every
# wave's and the no-migration step's engine-id lists), ``waves`` (also the
# deck's own wave cards, layout geometry) and ``wave`` (the wave number). Read
# from the facts block itself they are still owned (``_is_facts``): counting
# ``facts["engines"]`` is the ``engine_count`` fact, ``facts["waves"]`` the
# ``migration_wave_count`` fact.
NOT_A_FIGURE = frozenset({"engines", "waves", "wave"})


def _figure_fields(model: type[BaseModel], seen: set[type[BaseModel]] | None = None) -> set[str]:
    """Every field of ``model`` (recursively) whose value is a figure: a
    number, a map of numbers, a nested record, or a list of records."""
    seen = seen if seen is not None else set()
    seen.add(model)
    out: set[str] = set()

    def kinds(ann: object) -> list[object]:
        origin = typing.get_origin(ann)
        if origin in (typing.Union, types.UnionType):
            return [k for a in typing.get_args(ann) for k in kinds(a)]
        return [ann]

    for name, field in model.model_fields.items():
        for ann in kinds(field.annotation):
            origin, args = typing.get_origin(ann), typing.get_args(ann)
            nested = None
            if ann in (int, float):
                out.add(name)
            elif origin is dict and args and args[1] in (int, float):
                out.add(name)
            elif isinstance(ann, type) and issubclass(ann, BaseModel):
                out.add(name)
                nested = ann
            elif origin is list and args and isinstance(args[0], type):
                if issubclass(args[0], BaseModel):
                    out.add(name)
                    nested = args[0]
            if nested is not None and nested not in seen:
                out |= _figure_fields(nested, seen)
    return out


FACT_KEYS = frozenset(_figure_fields(ReportFacts)) - NOT_A_FIGURE
OWNED_KEYS = FACT_KEYS | INPUT_KEYS | ROW_KEYS
LIST_HELPERS = frozenset(
    {
        "filtered_risks",
        "resolved_risks",
        "open_risks",
        "resolved_risk_list",
        "resolve_migration_waves",
        "_reroutes",
    }
)
# Calls that return the facts block: every field read from it is owned.
FACTS_READERS = frozenset({"report_facts", "compute_report_facts"})
# Collecting from a fact list into another one, to count it afterwards.
COLLECTING = frozenset({"append", "add", "extend", "update", "insert"})
COUNTING_CALLS = frozenset({"len", "sum", "max", "min", "Counter", "count_by_severity", "count"})
# Helpers whose only job is counting risks: a sink wherever a renderer calls them,
# whatever they are passed (a renderer's parameter is not traced to its caller).
ALWAYS_COUNTING = frozenset({"count_by_severity"})
ARITHMETIC = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv)
PRAGMA = re.compile(r"#\s*facts:\s*presentation\b(?:\s*\((?P<reason>[^)]*)\))?")

# Every ``# facts: presentation`` exemption in use, by module and reason.
PINNED_EXEMPTIONS: frozenset[tuple[str, str]] = frozenset(
    {
        # #1020: the executive report's architecture bar, one segment per owner
        # engine as wide as its stored workload share.
        ("src/report/executive_report.py", "chart geometry"),
    }
)


def _call_name(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)


def _is_facts_block_read(node: ast.AST) -> bool:
    """``report["facts"]`` or ``report.get("facts")``: the stored facts block."""
    if isinstance(node, ast.Subscript):
        key = node.slice
        return isinstance(key, ast.Constant) and key.value == "facts"
    return (
        isinstance(node, ast.Call)
        and _call_name(node) == "get"
        and bool(node.args)
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "facts"
    )


def _is_facts(node: ast.AST, facts_names: set[str]) -> bool:
    """The facts block: ``report_facts(report)``, ``report["facts"]`` or
    ``.get("facts")``, a name assigned from either (or ``dict(...)`` of
    one), or a parameter named ``facts``."""
    if isinstance(node, ast.Name):
        return node.id in facts_names
    if isinstance(node, ast.Call) and _call_name(node) == "dict" and node.args:
        return _is_facts(node.args[0], facts_names)
    if _is_facts_block_read(node):
        return True
    return isinstance(node, ast.Call) and _call_name(node) in FACTS_READERS


_FACTS_NAMES: set[str] = set()
_RECORD_NAMES: set[str] = set()


def _is_record(node: ast.AST) -> bool:
    """A record input (``RECORD_INPUT_KEYS``), a field of one, or a name bound to either."""
    if isinstance(node, ast.Name):
        return node.id in _RECORD_NAMES
    if isinstance(node, ast.BoolOp):
        return _is_record(node.values[0])
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        return node.slice.value in RECORD_INPUT_KEYS or _is_record(node.value)
    if (
        isinstance(node, ast.Call)
        and _call_name(node) == "get"
        and isinstance(node.func, ast.Attribute)
        and node.args
        and isinstance(node.args[0], ast.Constant)
    ):
        return node.args[0].value in RECORD_INPUT_KEYS or _is_record(node.func.value)
    return False


def _is_source(node: ast.AST, tainted: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in tainted
    # Any field read off a record input, at any depth (#339 review).
    if isinstance(node, (ast.Subscript, ast.Call)) and _is_record(node):
        return True
    # Any field read straight off the facts block is a fact.
    if isinstance(node, ast.Subscript) and _is_facts(node.value, _FACTS_NAMES):
        return True
    if (
        isinstance(node, ast.Call)
        and _call_name(node) == "get"
        and isinstance(node.func, ast.Attribute)
        and _is_facts(node.func.value, _FACTS_NAMES)
    ):
        return True
    if isinstance(node, _COMPREHENSIONS):
        # Its own loop names are local to it: the comprehension is a source
        # when what it walks is (``[r for r in risks]``).
        return any(_contains_source(g.iter, tainted) for g in node.generators)
    if isinstance(node, ast.Subscript):
        key = node.slice
        return isinstance(key, ast.Constant) and key.value in OWNED_KEYS
    if isinstance(node, ast.Call):
        name = _call_name(node)
        if name in LIST_HELPERS:
            return True
        if (
            name == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in OWNED_KEYS
        ):
            return True
    return False


def _constant_key(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, (str, int))


def _walk_data(node: ast.AST):
    """``ast.walk`` minus lookup keys and plain fields: in ``x[k]`` and
    ``x.get(k, ...)`` the key ``k`` selects a value, it is not the value, so a
    tainted key does not make the value a fact (``ap_scope.get(eng, (0, 0))``);
    and a named field that is not owned (``risk.get("affected_tables")``) is
    not a fact even on a record that came from a fact list -- an owned field is
    a source by itself (``_is_source``)."""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        if isinstance(n, ast.Subscript):
            if not _constant_key(n.slice):
                stack.append(n.value)
        elif isinstance(n, ast.Call) and _call_name(n) == "get" and n.args:
            if not _constant_key(n.args[0]):
                stack.append(n.func)
            stack += n.args[1:]
        else:
            stack += list(ast.iter_child_nodes(n))


def _contains_source(node: ast.AST, tainted: set[str]) -> bool:
    return any(_is_source(n, tainted) for n in _walk_data(node))


def _names(target: ast.AST) -> set[str]:
    """Every plain name a target binds (``a``, ``a, b``, ``(i, (k, v))``)."""
    return {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}


def _not_a_number(node: ast.AST) -> bool:
    """A string, f-string, list or join: ``+`` on it is formatting, not arithmetic."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, (ast.JoinedStr, ast.List, ast.ListComp, ast.Tuple, ast.Set, ast.SetComp)):
        return True
    if isinstance(node, ast.IfExp):
        return _not_a_number(node.body) or _not_a_number(node.orelse)
    if isinstance(node, ast.Call):
        return _call_name(node) in {
            "join",
            "format",
            "str",
            "md_text",
            "set",
            "frozenset",
            "list",
            "sorted",
        }
    if isinstance(node, ast.BinOp):
        return _not_a_number(node.left) or _not_a_number(node.right)
    return False


def _taint(fn: ast.AST) -> set[str]:
    _FACTS_NAMES.clear()
    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        # A helper handed the facts block by its caller.
        _FACTS_NAMES.update(a.arg for a in fn.args.args + fn.args.kwonlyargs if a.arg == "facts")
    # Repeat until stable: ``f = dict(facts)`` follows a name assigned above it.
    while True:
        before = len(_FACTS_NAMES)
        _FACTS_NAMES.update(
            n.id
            for node in ast.walk(fn)
            if isinstance(node, ast.Assign) and _is_facts(node.value, _FACTS_NAMES)
            for t in node.targets
            for n in ast.walk(t)
            if isinstance(n, ast.Name)
        )
        if len(_FACTS_NAMES) == before:
            break
    _RECORD_NAMES.clear()
    while True:
        before = len(_RECORD_NAMES)
        _RECORD_NAMES.update(
            n.id
            for node in ast.walk(fn)
            if isinstance(node, ast.Assign) and _is_record(node.value)
            for t in node.targets
            for n in ast.walk(t)
            if isinstance(n, ast.Name)
        )
        if len(_RECORD_NAMES) == before:
            break
    _LIST_NAMES.clear()
    _LIST_NAMES.update(
        t.id
        for node in ast.walk(fn)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and isinstance(node.value, (ast.List, ast.ListComp))
        for t in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(t, ast.Name)
    )
    tainted: set[str] = set()
    # Repeat until stable: a name assigned late in the function can feed an
    # assignment above it inside a loop.
    while True:
        before = len(tainted)
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and _contains_source(node.value, tainted):
                for t in node.targets:
                    tainted |= _names(t)
            elif (
                isinstance(node, (ast.AnnAssign, ast.NamedExpr))
                and node.value is not None
                and _contains_source(node.value, tainted)
            ):
                tainted |= _names(node.target)
            elif (
                isinstance(node, ast.AugAssign)
                and _arithmetic(node)
                and _contains_source(node.value, tainted)
            ):
                tainted |= _names(node.target)
            elif isinstance(node, ast.For) and _contains_source(node.iter, tainted):
                # A list or set filled from what the loop walks is counted
                # like the loop itself (``highs.append(r)`` then ``len(highs)``).
                for n in (n for stmt in node.body for n in ast.walk(stmt)):
                    if (
                        isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute)
                        and n.func.attr in COLLECTING
                        and isinstance(n.func.value, ast.Name)
                    ):
                        tainted.add(n.func.value.id)
                target = node.target
                if (
                    isinstance(node.iter, ast.Call)
                    and _call_name(node.iter) == "enumerate"
                    and isinstance(target, ast.Tuple)
                    and target.elts
                ):
                    # enumerate's counter is a position, not data from the list.
                    target = ast.Tuple(elts=target.elts[1:], ctx=ast.Store())
                tainted |= _names(target)
        if len(tainted) == before:
            return tainted


_LIST_NAMES: set[str] = set()


def _arithmetic(node: ast.AST) -> bool:
    if isinstance(node, ast.AugAssign):
        # ``out += [...]`` / ``out += helper(...)`` on a list is concatenation.
        if isinstance(node.target, ast.Name) and node.target.id in _LIST_NAMES:
            return False
        return isinstance(node.op, ARITHMETIC) and not _not_a_number(node.value)
    if isinstance(node, ast.BinOp):
        return isinstance(node.op, ARITHMETIC) and not (
            _not_a_number(node.left) or _not_a_number(node.right)
        )
    return False


def _accumulates(node: ast.AST) -> bool:
    if isinstance(node, ast.AugAssign):
        return _arithmetic(node)
    if (
        isinstance(node, ast.Assign)
        and isinstance(node.value, ast.BinOp)
        and _arithmetic(node.value)
    ):
        targets = {ast.unparse(t) for t in node.targets}
        reads = {
            ast.unparse(n) for n in ast.walk(node.value) if isinstance(n, (ast.Name, ast.Subscript))
        }
        gets = [
            n for n in ast.walk(node.value) if isinstance(n, ast.Call) and _call_name(n) == "get"
        ]
        return bool(targets & reads) or (
            bool(gets) and any(isinstance(t, ast.Subscript) for t in node.targets)
        )
    return False


def _function_violations(fn: ast.AST, lines: list[str]) -> list[tuple[int, str]]:
    tainted = _taint(fn)
    hits: list[ast.AST] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and _call_name(node).lstrip("_") in ALWAYS_COUNTING:
            hits.append(node)
        elif isinstance(node, ast.Call) and _call_name(node) in COUNTING_CALLS:
            receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
            if any(_contains_source(a, tainted) for a in node.args) or (
                _call_name(node) == "count"
                and receiver is not None
                and _contains_source(receiver, tainted)
            ):
                hits.append(node)
        elif isinstance(node, ast.BinOp) and _arithmetic(node):
            if _contains_source(node.left, tainted) or _contains_source(node.right, tainted):
                hits.append(node)
        elif isinstance(node, ast.AugAssign) and _arithmetic(node):
            if _contains_source(node.value, tainted):
                hits.append(node)
        elif isinstance(node, ast.For) and _contains_source(node.iter, tainted):
            # Counting what the loop walks: an accumulator in its body
            # (``n += 1``, ``d[k] = d.get(k, 0) + 1``).
            hits += [n for stmt in node.body for n in ast.walk(stmt) if _accumulates(n)]
    out: list[tuple[int, str]] = []
    for node in hits:
        line = getattr(node, "lineno", 0)
        pragma = PRAGMA.search(lines[line - 1])
        if pragma and (pragma.group("reason") or "").strip():
            continue
        out.append((line, ast.unparse(node)[:100]))
    return out


def _violations(path: Path) -> list[str]:
    source = path.read_text()
    lines = source.splitlines()
    tree = ast.parse(source)
    found: set[tuple[int, str]] = set()
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found |= set(_function_violations(fn, lines))
    name = path.relative_to(REPO) if path.is_relative_to(REPO) else path.name
    return [f"{name}:{line}: {expr}" for line, expr in sorted(found)]


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module)
    return out


def _renderer_modules() -> list[Path]:
    return sorted(p for p in REPORT.rglob("*.py") if p.name not in EXEMPT)


def test_report_imports_nothing_from_agents() -> None:
    bad = {
        f"{p.relative_to(REPO)} imports {m}"
        for p in sorted(REPORT.rglob("*.py"))
        for m in _imports(p)
        if m == "src.agents" or m.startswith("src.agents.")
    }
    assert not bad, sorted(bad)


def test_the_facts_layer_imports_neither_agents_nor_report() -> None:
    bad = {
        m
        for m in _imports(FACTS_LAYER)
        if m.split(".")[:2] in (["src", "agents"], ["src", "report"])
    }
    assert not bad, sorted(bad)


def test_every_figure_field_of_the_facts_contract_is_owned() -> None:
    """The owned keys come from the contract, so a new fact is owned at once."""
    for key in (
        "engine_count",
        "migration_wave_count",
        "engine_source_tables",
        "basis_table_count",
        "projected_monthly_cost_usd",
        "critical_high_risk_count",
        "source_view_count",
        "views",
        "tables_only",
        "table_basis",
        "no_migration",
        "moving_tables",
        "kept_in_sync",
        "access_pattern_totals",
        "owned_pattern_count",
        "cache_layer_pattern_count",
        "listed_pattern_count",
    ):
        assert key in OWNED_KEYS, key


def test_no_renderer_does_arithmetic_on_a_fact() -> None:
    found = [v for p in _renderer_modules() for v in _violations(p)]
    assert not found, (
        "A renderer computes a figure the facts layer owns (#615, ADR-032); add it to "
        "src/shared/report_facts.py and read it through src.report.facts.report_facts:\n"
        + "\n".join(found)
    )


def test_presentation_exemptions_carry_a_reason_and_are_pinned() -> None:
    """A ``# facts: presentation`` line needs its reason, and a new one is
    reviewed: add it to PINNED_EXEMPTIONS together with the code."""
    found: set[tuple[str, str]] = set()
    for p in sorted(REPORT.rglob("*.py")):
        for i, line in enumerate(p.read_text().splitlines(), start=1):
            m = PRAGMA.search(line)
            if m:
                reason = (m.group("reason") or "").strip()
                assert reason, f"{p.relative_to(REPO)}:{i}: '# facts: presentation' needs a reason"
                found.add((str(p.relative_to(REPO)), reason))
    assert found == PINNED_EXEMPTIONS


PROBE = """
def render(report, w, costs, param_list, rep):
    risks = filtered_risks(report)
    a = len(risks)
    b = w["table_count"] - len(w.get("table_owners") or [])
    c = sum(e["migrates"] for e in report["rows"])
    n = 0
    for r in risks:
        n += 1
    total = 0
    for c2 in report["cost_breakdown"]:
        total += c2
    d = {}
    for r in filtered_risks(report):
        d[r["k"]] = d.get(r["k"], 0) + 1
    highs = [r["severity"] for r in risks].count("HIGH")
    by_sev = Counter(r["severity"] for r in risks)
    x, y = w["table_count"], w["owned_counts_by_engine"]
    diff = x - y
    sev = count_by_severity(risks)
    sev2 = count_by_severity(param_list)
    shown = ", ".join(w["tables"][:20])
    more = len(w["tables"]) - 20
    label = "Wave " + str(w["wave"])
    facts = report_facts(report)
    n_engines = len(facts["engines"])
    n_waves = len(report_facts(report).get("waves") or [])
    n_roadmap = len(resolve_migration_waves(report))
    highs = []
    for r in filtered_risks(report):
        highs.append(r)
    n_highs = len(highs)
    seen = set()
    for r in risks:
        seen.add(r["risk_type"])
    n_types = len(seen)
    wave_no = "Wave " + str(w["wave"])
    n_stored_waves = len(report["facts"]["waves"])
    stored = report.get("facts")
    n_stored_engines = len(stored["engines"])
    copy = dict(report_facts(report))
    n_copy_engines = len(copy["engines"])
    ranking = rep.get("ranking")
    n_ranked = len(ranking)
    traces = rep.get("query_traces") or {}
    n_full = len(rep["query_traces"]["queries"])
    n_digest = len(traces["queries"])
    by_id = {t["query_id"]: t for t in traces.get("queries") or []}
    n_moved = sum(1 for t in by_id.values() if t.get("moves"))
    shown_trace = by_id.get("q1")
    bar = len(risks) / 4  # facts: presentation (bar width)
    bare = len(risks)  # facts: presentation
    return a, b, c, n, total, d, highs, by_sev, diff, sev, sev2, shown, more, label, bar, bare


def helper(facts, owned_only):
    n_param_engines = len(facts["engines"])
    n_param_waves = len(facts.get("waves"))
    n_trade_offs = facts["trade_off_count"] + len(owned_only)
    return n_param_engines, n_param_waves, n_trade_offs
"""


def test_the_guard_flags_every_counting_shape(tmp_path: Path) -> None:
    """The guard itself: each counting shape is caught (the review's seven
    bypasses among them), formatting is not, and a pragma with no reason does
    not exempt its line."""
    sample = tmp_path / "sample.py"
    sample.write_text(PROBE)
    flagged = {int(v.split(":")[1]) for v in _violations(sample)}
    lines = PROBE.splitlines()

    def line_of(text: str) -> int:
        return next(i for i, line in enumerate(lines, start=1) if text in line)

    expected = {
        line_of("a = len(risks)"),
        line_of('b = w["table_count"]'),
        line_of('c = sum(e["migrates"]'),
        line_of("n += 1"),
        line_of("total += c2"),
        line_of('d.get(r["k"], 0) + 1'),
        line_of('.count("HIGH")'),
        line_of("Counter("),
        line_of("diff = x - y"),
        line_of("count_by_severity(risks)"),
        line_of("count_by_severity(param_list)"),
        line_of("bare = len(risks)"),
        line_of('n_engines = len(facts["engines"])'),
        line_of("n_waves = len(report_facts(report)"),
        line_of("n_roadmap = len(resolve_migration_waves"),
        line_of("n_highs = len(highs)"),
        line_of("n_types = len(seen)"),
        line_of("n_param_engines = len("),
        line_of("n_param_waves = len("),
        line_of("n_trade_offs ="),
        line_of('len(report["facts"]["waves"])'),
        line_of('len(stored["engines"])'),
        line_of('len(copy["engines"])'),
        line_of("n_ranked = len(ranking)"),
        line_of('n_full = len(rep["query_traces"]["queries"])'),
        line_of('n_digest = len(traces["queries"])'),
        line_of("n_moved = sum(1 for t in by_id.values()"),
    }
    assert flagged == expected, sorted(flagged ^ expected)


def test_a_helper_handed_the_facts_block_is_guarded(tmp_path: Path) -> None:
    """``_trade_offs_md(report, facts)`` gets the facts block as a parameter:
    reading its owned fields passes, counting one is flagged (round 3 of #735)."""
    from src.report import engineering_report

    source = Path(engineering_report.__file__).read_text()
    assert _violations(Path(engineering_report.__file__)) == []
    needle = '    out = [f"## Migration trade-offs ({facts.get(\'trade_off_count\', 0)})", ""]\n'
    assert needle in source
    sample = tmp_path / "engineering_report.py"
    sample.write_text(source.replace(needle, needle + '    n_engines = len(facts["engines"])\n', 1))
    found = _violations(sample)
    assert len(found) == 1 and "len(facts['engines'])" in found[0], found


def test_a_recount_of_the_listed_traces_is_caught(tmp_path: Path) -> None:
    """#339 review: the appendix renderer passes, and each way of recounting the
    listed traces behind ``facts.query_trace`` planted into it is flagged."""
    from src.report import query_traces

    source = Path(query_traces.__file__).read_text()
    assert _violations(Path(query_traces.__file__)) == []
    needle = '    listed = int(facts.get("listed_query_count") or 0)\n'
    assert needle in source
    plants = [
        '    n_full = len(report["query_traces"]["queries"])\n',
        '    n_digest = len(digest["queries"])\n',
        '    n_moved = sum(1 for t in by_id.values() if t.get("moves"))\n',
    ]
    for plant in plants:
        sample = tmp_path / "query_traces.py"
        sample.write_text(source.replace(needle, needle + plant, 1))
        found = _violations(sample)
        line = source[: source.index(needle)].count("\n") + 2
        assert [v.split(":")[1] for v in found] == [str(line)], (plant, found)


def test_a_recount_of_the_access_pattern_totals_is_caught(tmp_path: Path) -> None:
    """#792: each way of adding up ``facts.access_pattern_totals`` in a renderer is
    flagged; reading one of its figures is not."""
    plants = [
        'n = sum(facts["access_pattern_totals"].values())\n',
        'n = facts["access_pattern_totals"]["owned_pattern_count"] + 1\n',
        'n = max(facts["access_pattern_totals"]["owned_pattern_count"], 2)\n',
        'n = facts["access_pattern_totals"]["owned_pattern_count"]'
        ' + facts["access_pattern_totals"]["cache_layer_pattern_count"]\n',
    ]
    for i, plant in enumerate(plants):
        sample = tmp_path / f"sample{i}.py"
        sample.write_text("def render(facts):\n    " + plant + "    return n\n")
        assert [v.split(":")[1] for v in _violations(sample)] == ["2"], plant
    ok = tmp_path / "ok.py"
    ok.write_text(
        'def render(facts):\n    return facts["access_pattern_totals"]["owned_pattern_count"]\n'
    )
    assert _violations(ok) == []


JS_GUARD = REPO / "src" / "ui" / "src" / "utils" / "__tests__" / "rendererOnlyFormat.test.js"


def _js_set(name: str) -> set[str]:
    """The string members of ``const <name> = new Set([...])`` in the jest guard."""
    m = re.search(rf"const {name} = new Set\(\[(.*?)\]\);", JS_GUARD.read_text(), re.S)
    assert m, f"{name} not found in {JS_GUARD.relative_to(REPO)}"
    return set(re.findall(r"'([^']+)'", m.group(1)))


def test_the_javascript_guard_owns_what_the_python_guard_owns() -> None:
    """The UI side has its own guard (#748, jest: Python cannot parse JavaScript).
    Its input keys are this guard's, and its excepted names are this guard's plus
    ``index`` (a list position, which a JS ``.index`` read would otherwise own);
    its owned fact keys come from the contract snapshot, so they follow
    ``ReportFacts`` the same way ``FACT_KEYS`` does."""
    assert _js_set("INPUT_KEYS") == set(INPUT_KEYS)
    assert _js_set("NOT_A_FIGURE") == set(NOT_A_FIGURE) | {"index"}
    assert "../../../../.." in JS_GUARD.read_text()  # reads tests/contract/schema_snapshot.json
