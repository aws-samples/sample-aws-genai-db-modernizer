"""Every call that strips SQL comments and literals names the source engine (#1114).

MySQL ``#`` comments and PostgreSQL ``$$`` strings change what is code and what is
text, so a call without the engine silently reads another dialect's SQL (AGENTS.md
checklist item 10). The helpers below all end in ``strip_comments_and_literals``, or
pass the engine on to one that does; each call in ``src/`` must pass the engine,
positionally or as a keyword, and must not pass a literal ``None`` or ``""``. A call
that really has no engine to give is allowed with a ``# engine unknown: <why>``
comment on one of its lines.

What the guard does not do: it matches calls by function name, so an import alias, a
bare reference (``partial(can_own, engine)``) or a call through ``getattr`` is not
seen; it checks that an engine is passed, not that it is the right one (a variable
holding the wrong value passes); and it reads ``src/`` only.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "src"

# function name -> (number of positional arguments up to and including the engine, or None
# when the engine is keyword-only; the keyword that names it)
_ENGINE = "source_engine"
ENGINES = {
    "strip_comments_and_literals": (2, _ENGINE),
    "non_scalar_function_names": (2, _ENGINE),
    "is_dedup_only_group_by": (2, _ENGINE),
    "_normalized_sql": (2, _ENGINE),
    "readable_sql": (2, _ENGINE),
    "locking_clause": (2, _ENGINE),
    "skips_locked_rows": (2, _ENGINE),
    "primary_key_ordered_limit": (3, _ENGINE),
    "_primary_key_ordered_row_limit": (3, _ENGINE),
    "is_primary_key_first_row_read": (4, _ENGINE),
    # The ownership gate reads a locking clause, so it needs the dialect too.
    "is_locking_read": (2, _ENGINE),
    "is_write_query": (2, _ENGINE),  # the write check reads # comments and $$ strings (#1144)
    "write_heavy_tables": (2, _ENGINE),
    "QueryCostModel": (None, _ENGINE),
    "build_migration_risks": (None, _ENGINE),
    "can_own": (3, _ENGINE),
    "owner_candidates": (3, _ENGINE),
    "may_absorb": (6, _ENGINE),
    "_ownership_failure": (3, _ENGINE),
    # The reality-check and consolidation helpers that carry the engine down to the gate.
    "_group_eligible_engines": (None, _ENGINE),
    "_query_eligible_engines": (None, _ENGINE),
    "_eligible_destinations": (None, _ENGINE),
    "_evaluate_engine_removal": (None, _ENGINE),
    "_removal_tiebreak_key": (None, _ENGINE),
    "_settle_committed_engines": (None, _ENGINE),
    "_place_settled_queries": (None, _ENGINE),
    "_find_best_absorber_for_query": (None, "source_dialect"),
}
ALLOW_COMMENT = "# engine unknown:"


def _called_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_blank(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value in (None, "")


def calls_without_engine(source: str) -> list[tuple[int, str]]:
    """``(line, function)`` of each call that names no engine, or names a literal
    ``None`` / ``""`` without an ``# engine unknown:`` comment on its lines."""
    lines = source.splitlines()
    bad = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = _called_name(node)
        if name not in ENGINES:
            continue
        position, keyword = ENGINES[name]
        if any(isinstance(a, ast.Starred) for a in node.args) or any(
            k.arg is None for k in node.keywords
        ):
            continue
        passed = None
        for k in node.keywords:
            if k.arg == keyword:
                passed = k.value
        if passed is None and position is not None and len(node.args) >= position:
            passed = node.args[position - 1]
        end = getattr(node, "end_lineno", node.lineno) or node.lineno
        allowed = any(ALLOW_COMMENT in line for line in lines[node.lineno - 1 : end])
        if passed is None or (_is_blank(passed) and not allowed):
            bad.append((node.lineno, name))
    return bad


def test_every_stripper_call_in_src_passes_the_source_engine():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        offenders += [
            f"{path.relative_to(SRC.parent)}:{line} {name}"
            for line, name in calls_without_engine(path.read_text())
        ]
    assert not offenders, "calls without the source engine:\n" + "\n".join(offenders)


def test_the_guard_flags_a_call_without_the_engine():
    assert calls_without_engine("strip_comments_and_literals(sql)") == [
        (1, "strip_comments_and_literals")
    ]
    assert calls_without_engine("m.skips_locked_rows(t)") == [(1, "skips_locked_rows")]
    assert calls_without_engine("can_own(engine, query)") == [(1, "can_own")]
    assert calls_without_engine("is_write_query(query)") == [(1, "is_write_query")]
    assert calls_without_engine("write_heavy_tables(queries)") == [(1, "write_heavy_tables")]
    assert calls_without_engine("primary_key_ordered_limit(t, pk)") == [
        (1, "primary_key_ordered_limit")
    ]


def test_the_guard_accepts_positional_keyword_and_forwarded_engines():
    assert calls_without_engine("strip_comments_and_literals(sql, engine)") == []
    assert calls_without_engine("non_scalar_function_names(sql, source_engine=e)") == []
    assert calls_without_engine("is_dedup_only_group_by(*args)") == []
    assert calls_without_engine("can_own(engine, query, ctx.source_engine)") == []
    assert calls_without_engine("is_primary_key_first_row_read(t, pk, None, e)") == []
    assert calls_without_engine("_group_eligible_engines(q, source_engine=e)") == []


def test_the_guard_flags_the_helpers_that_carry_the_engine_down():
    assert calls_without_engine("_group_eligible_engines(q, t, c)") == [
        (1, "_group_eligible_engines")
    ]
    # keyword-only: a positional extra is not the engine
    assert calls_without_engine("_query_eligible_engines(a, b, c, d, e, f, g, h, i)") == [
        (1, "_query_eligible_engines")
    ]
    assert calls_without_engine("_find_best_absorber_for_query(q, source_engine=e)") == [
        (1, "_find_best_absorber_for_query")
    ]


def test_the_guard_flags_a_blank_engine_unless_the_call_says_why():
    assert calls_without_engine("can_own(e, q, None)") == [(1, "can_own")]
    assert calls_without_engine('can_own(e, q, source_engine="")') == [(1, "can_own")]
    assert calls_without_engine('can_own(e, q, "")  # engine unknown: no collector at hand') == []
    assert calls_without_engine("can_own(e, q, ctx.source_engine)") == []
