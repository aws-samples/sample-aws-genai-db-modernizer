"""Guard test: an engine is never picked by name order, query count, set
iteration order, or a literal-engine preference outside the shared ADR-031
tie-break (#571).

ADR-031 Decision 2 allows exactly one rule for breaking a routing tie: the
source-compatible engine wins, otherwise an unresolved tie is reported and a
maintainer-decided placeholder is picked
(:func:`src.agents.referee.tie_break.break_tie`). Review has repeatedly found
code that decides an engine a different way instead -- by name order
(``sorted(engines)[0]``, #548), by raw query count
(``min(candidates, key=lambda e: (-counts[e], e))``, #549), by Python's
non-deterministic set iteration order (``max(a_set, key=score)``, #569), or by
a hardcoded engine literal (``winner = "aurora_postgresql"``, #550). Each one
was caught by hand, in a different review round. This test scans the AST of
every module that assigns an engine and fails on the same four shapes before
a reviewer has to find the fifth one.

Scope: ``src/agents/referee/`` is scanned in full (every module there can
influence a routing decision), plus any OTHER module under ``src/`` that
writes ``assigned_engine`` -- found by scanning for that attribute/key as an
assignment target, not hardcoded, so a future engine-assigning module outside
``referee/`` is picked up automatically (see
``test_every_engine_assigning_module_is_in_scope`` below).

Rules (one AST shape each):

1. ``sorted_index0`` -- ``sorted(X)[0]``, OR ``name = sorted(X)`` (directly,
   or through one same-module helper function all of whose own ``return``s
   are a bare ``sorted(...)``, e.g. ``tie_break._settled_workload_leaders``)
   followed anywhere later in the function by ``name[0]`` -- picks the first
   element of a name-sorted sequence. This second shape is the actual
   pre-#503 code: ``ranked = sorted(engines, key=...); winner, runner_up =
   ranked[0], ranked[1]`` in ``break_owner_tie``/``_pick_winner`` as they
   stood at commit ``0ae4b7d7`` -- see the regression tests parsing that
   exact historical source below.
2. ``minmax_count_or_name_key`` -- ``min``/``max`` with a ``key=`` lambda
   whose body reads a `*count*` collection, is the bare loop variable itself
   (alphabetical order with no key at all), or reads a fixed tuple position
   out of the loop variable (``kv[1]`` -- the ``dict.items()``/
   ``Counter.most_common()`` "key or value by position" idiom, indistinguishable
   at the AST level from a real score comparison, so every call shaped this
   way gets its own allowlist entry rather than being assumed safe either
   way); OR ``Counter(...).most_common(1)[0]``/``[0][0]`` -- the same
   "highest count wins" pick wearing a different idiom.
3. ``set_order_dependent`` -- ``next(iter(X))``, or ``min``/``max(X, key=...)``,
   where ``X`` is a set literal/comprehension/``set()`` call, or a name this
   scanner traced back (within the same function) to one -- Python does not
   guarantee set iteration order, so the "first" or "tied" element silently
   depends on ``PYTHONHASHSEED`` and insertion order (#569).
4. ``literal_engine_preference`` -- a decision variable (``winner``,
   ``primary*``, ``chosen``, ``target*``, ``fallback*``, ``owner*``)
   unconditionally assigned a hardcoded engine id, e.g.
   ``winner = "aurora_postgresql"`` (#550). A conditional pick -- an
   ``if``/ternary keyed on an actual score comparison -- is not this rule,
   even when one of its branches happens to be a literal engine name; only an
   *unconditional* literal assignment is.
5. ``first_seen_then_break`` -- ``for e in engines: winner = e; break`` --
   the loop body is exactly one assignment of the loop variable followed by
   an unconditional ``break``, so whichever element the iterable produces
   first wins with no comparison at all, list or set.

Out of scope by design: a bare ``if engine == "opensearch":`` branch that
special-cases one engine's own capability or display logic is not a
*preference among competing candidates* (ADR-031 is silent on it, because
nothing is being decided between engines there) and is not flagged -- adding
that shape would flag most of this directory's legitimate per-engine
business logic for no bias-detection value.

Allowlist: display-only ordering (ADR-031 Decision 1: a table's
``primary_engine`` is a summary, not a per-query decision; a customer-facing
sentence's engine ordering is prose, not routing) and biases already tracked
by an open issue are both allowed through ``_ALLOWLIST`` below, each with a
file, a qualified function name, the rule it trips, and a one-line reason.
``test_allowlist_has_no_stale_entries`` fails if an entry no longer matches
anything that was actually found, so the allowlist cannot silently outlive
the code it was written for.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SRC_DIR = _REPO_ROOT / "src"
_REFEREE_DIR = _SRC_DIR / "agents" / "referee"

_KNOWN_ENGINES = {
    "aurora_postgresql",
    "aurora_mysql",
    "aurora",
    "dynamodb",
    "documentdb",
    "opensearch",
    "elasticache",
}

_DECISION_VAR_RE = re.compile(r"winner|primary|chosen|target|fallback|owner", re.IGNORECASE)
_SET_BINOPS = (ast.BitAnd, ast.BitOr, ast.BitXor, ast.Sub)

RULE_SORTED_INDEX0 = "sorted_index0"
RULE_MINMAX_COUNT_OR_NAME = "minmax_count_or_name_key"
RULE_SET_ORDER = "set_order_dependent"
RULE_LITERAL_PREFERENCE = "literal_engine_preference"
RULE_FIRST_SEEN_BREAK = "first_seen_then_break"

ALL_RULES = (
    RULE_SORTED_INDEX0,
    RULE_MINMAX_COUNT_OR_NAME,
    RULE_SET_ORDER,
    RULE_LITERAL_PREFERENCE,
    RULE_FIRST_SEEN_BREAK,
)


@dataclass(frozen=True)
class Finding:
    file: str
    qualname: str
    rule: str
    lineno: int
    detail: str

    def key(self) -> tuple[str, str, str]:
        return (self.file, self.qualname, self.rule)


# ---------------------------------------------------------------------------
# Allowlist -- every entry is (file, qualified function name, rule) -> reason.
# ---------------------------------------------------------------------------

_ALLOWLIST: dict[tuple[str, str, str], str] = {
    # --- Display-only ordering (ADR-031 Decision 1: table_mappings' single
    # primary_engine is a summary, never a per-query decision) ---
    (
        "assignment_resolver.py",
        "derive_table_assignments",
        RULE_MINMAX_COUNT_OR_NAME,
    ): (
        "ADR-031 Decision 1: TableAssignment.primary_engine is a table-level "
        "summary field, not ownership or a per-query decision -- the per-query "
        "engine was already decided by the resolver/tie_break before this runs."
    ),
    (
        "assignment_resolver.py",
        "derive_table_assignments",
        RULE_LITERAL_PREFERENCE,
    ): (
        "global_fallback='aurora' only fires when the whole assignment has no "
        "owner engine anywhere (logged as a warning); a degenerate-input "
        "placeholder for the table summary field, not a competitive pick "
        "among engines that could serve the query."
    ),
    (
        "reality_check.py",
        "_detect_architectural_patterns",
        RULE_MINMAX_COUNT_OR_NAME,
    ): (
        "Labels an already-assigned engine as 'primary' for the CQRS/polyglot "
        "architectural-pattern narrative shown to the customer; never "
        "reassigns a query -- every engine here was already committed before "
        "this runs."
    ),
    (
        "assignment_overrides.py",
        "_propagate_codependent_overrides",
        RULE_SORTED_INDEX0,
    ): (
        "anchor=sorted(touched)[0] picks a representative query id for a "
        "log/exclusion message, not an engine -- display text only."
    ),
    (
        "assignment_overrides.py",
        "_propagate_codependent_overrides",
        RULE_SET_ORDER,
    ): (
        "target=next(iter(chosen)) runs only after 'if len(chosen) != 1: "
        "continue' on the line above, so chosen always has exactly one "
        "member here; order cannot matter with one element."
    ),
    (
        "synthesis_grounding.py",
        "check_summary_grounding.judge",
        RULE_SORTED_INDEX0,
    ): (
        "table=sorted(named)[0] names a table for a grounding-mismatch "
        "message, not an engine -- display text only."
    ),
    (
        "reality_check.py",
        "_suggest_lightweight_for_orphans",
        RULE_SET_ORDER,
    ): (
        "shared_cap=next(iter(all_caps)) runs only after 'if len(all_caps) != "
        "1: return None' above, so all_caps always has exactly one member "
        "here; it is a capability name, not an engine, and order cannot "
        "matter with one element."
    ),
    (
        "synthesis_report.py",
        "build_ranking",
        RULE_MINMAX_COUNT_OR_NAME,
    ): (
        "max(complexity_counts, key=...) picks the most common "
        "migration-complexity label (SIMPLE/MEDIUM/COMPLEX) across a "
        "table's recommendations for a confidence-weighting score -- no "
        "engine is involved."
    ),
    (
        "synthesis_report.py",
        "_coverage_gap_risk",
        RULE_MINMAX_COUNT_OR_NAME,
    ): (
        "min(gap.values(), key=lambda s: _SEVERITY_ORDER.get(s, 9)) ranks "
        "severity labels (low/medium/high) for a coverage-gap risk entry -- "
        "no engine is involved."
    ),
    (
        "synthesis_report.py",
        "_table_normaliser.resolve",
        RULE_SET_ORDER,
    ): (
        "next(iter(matches)) only runs when 'len(matches) == 1' (the "
        "ternary's own condition) -- a table-name lookup, not an engine; "
        "order cannot matter with one element."
    ),
    (
        "assignment_resolver.py",
        "retained_engine_for",
        RULE_SORTED_INDEX0,
    ): (
        "chosen=sorted(AURORA_ENGINES.intersection(used)); chosen[0] -- the "
        "function's own docstring states the invariant that makes this safe: "
        "by the time an assignment exists, choose_heterogeneous_engine "
        "already dropped the losing Aurora engine from scoring, so at most "
        "one of aurora_mysql/aurora_postgresql ever appears as an "
        "assigned_engine; chosen has at most one element by construction."
    ),
    (
        "aurora_choice.py",
        "choose_heterogeneous_engine",
        RULE_SORTED_INDEX0,
    ): (
        "engines=sorted(AURORA_ENGINES.intersection(pool)); engines[0] runs "
        "only after 'if len(engines) == 1' -- the one-Aurora-engine-left "
        "early return, not a competitive pick."
    ),
    (
        "aurora_choice.py",
        "pick_aurora_engine_with_tie",
        RULE_SORTED_INDEX0,
    ): (
        "pool=sorted(AURORA_ENGINES.intersection(candidates)); pool[0] runs "
        "only after 'if len(pool) == 1' -- same one-engine-left guard as "
        "choose_heterogeneous_engine above; the real two-engine case defers "
        "to that function a few lines down."
    ),
    (
        "cache_overlay.py",
        "overlay_summary",
        RULE_MINMAX_COUNT_OR_NAME,
    ): (
        "Counter(...).most_common(1)[0][0] reports which engine the cache "
        "most often sits in front of, for the customer-facing cache_overlay "
        "summary dict -- a display statistic, not a per-query assignment; "
        "every query here was already assigned before this runs."
    ),
    (
        "reality_check.py",
        "_settle_committed_engines",
        RULE_SORTED_INDEX0,
    ): (
        "decisive=sorted(c for c in tied if extra[c] == best); decisive[0] "
        "-- the preceding 'if len(decisive) > 1: ...; continue' leaves "
        "exactly one element by the time this runs (and the 'else: decisive "
        "= tied' branch only takes it when len(tied) == 1 already); extra's "
        "own ranking comes from _removal_tiebreak_key's data, not name order."
    ),
    (
        "reality_check.py",
        "_pick_winner",
        RULE_SORTED_INDEX0,
    ): (
        "tied[0] runs only after 'if len(tied) == 1' -- the no-tie-at-all "
        "case; a real tie defers to break_tie on the next line, same as "
        "every other caller."
    ),
    (
        "reality_check.py",
        "run_reality_check",
        RULE_SORTED_INDEX0,
    ): (
        "tied_group[0] runs only after 'if len(tied_group) == 1' -- a real "
        "tie on the group's score defers to break_tie, same as _pick_winner."
    ),
    (
        "reality_check_summary.py",
        "build_final_summary",
        RULE_SORTED_INDEX0,
    ): (
        "ranked[0] runs only after 'if len(ranked) == 1' and only feeds a "
        "customer-facing sentence ('the final architecture runs all of them "
        "on <engine>') -- display text, after every engine was already "
        "decided."
    ),
    (
        "synthesis_grounding.py",
        "_engine_refs",
        RULE_SORTED_INDEX0,
    ): (
        "aurora[0] runs only after 'if len(aurora) == 1' -- resolving a "
        "bare 'Aurora' text mention to the one Aurora engine actually in "
        "play, for grounding-mismatch detection, not an assignment."
    ),
    (
        "synthesis_report.py",
        "_capability_pin_reason",
        RULE_SORTED_INDEX0,
    ): (
        "names[0] only runs when 'len(names) == 1' (the ternary's own "
        "condition) -- capability display names, not engines."
    ),
    (
        "tie_break.py",
        "break_tie",
        RULE_SORTED_INDEX0,
    ): (
        "staying=sorted(set(tied) & current_engines); staying[0] only runs "
        "after 'len(staying) == 1' is checked -- the one remaining member is "
        "returned, never a choice among several. This IS the shared ADR-031 "
        "tie rule every other site is checked against; it is allowlisted "
        "for the same reason it exists -- there is nowhere further to defer "
        "to."
    ),
    (
        "tie_break.py",
        "pick_unresolved_tie_placeholder",
        RULE_SORTED_INDEX0,
    ): (
        "leaders[0] only runs after 'len(leaders) == 1' -- one element, no "
        "choice. ranked=sorted(leaders, key=hash(...)); ranked[0] picks by "
        "the stable hash digest (ADR-031 Decision 12's maintainer-decided "
        "placeholder rule), not by engine name or count -- the sort key is "
        "the hash, 'sorted' is just how Python finds its minimum. This is "
        "the shared rule itself; every other caller goes through it rather "
        "than reimplementing it."
    ),
    # --- Real bias, already tracked by an open issue (not fixed here) ---
    (
        "cache_overlay.py",
        "fallback_owner",
        RULE_SORTED_INDEX0,
    ): "singleton guard: tied[0] runs only when len(tied) == 1; real ties go through break_tie (#549).",
    (
        "reality_check.py",
        "_removal_tiebreak_key",
        RULE_SET_ORDER,
    ): (
        "#569: tracked, open -- best_alt=max(alts, key=...) over alts, which "
        "this scanner traces back to the 'survivors: set[str]' parameter via "
        "_group_eligible_engines; a tie's destination depends on set "
        "iteration order."
    ),
    (
        "consolidation_validator.py",
        "sanity_sweep",
        RULE_MINMAX_COUNT_OR_NAME,
    ): (
        "#579: tracked, open -- primary_engine=max(engine_counts, key=...) "
        "protects the engine with the most queries from the orphan sweep by "
        "raw count, not score."
    ),
    (
        "aurora_choice.py",
        "choose_heterogeneous_engine",
        RULE_LITERAL_PREFERENCE,
    ): (
        "#445: tracked, open -- Aurora PostgreSQL is the one-sided default "
        "on an exact tie and the undecided-features branch, pending "
        "dialect-aware scoring; belongs to the heterogeneous-source owner."
    ),
    (
        "migration_waves.py",
        "build_migration_waves",
        RULE_SORTED_INDEX0,
    ): (
        "#583: tracked, open -- chosen=sorted(AURORA_ENGINES & "
        "set(by_engine)); chosen[0] is a fallback-only path (no recorded "
        "aurora_engine_choice) that the surrounding comment already admits "
        "'is really alphabetical order' and would pick aurora_mysql over "
        "aurora_postgresql regardless of which one actually won."
    ),
}


# ---------------------------------------------------------------------------
# AST scanning
# ---------------------------------------------------------------------------


def _call_func_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _is_set_literal_like(node: ast.AST) -> bool:
    """``node`` is syntactically a set: a literal, a comprehension, or a
    ``set()``/``frozenset()`` call."""
    if isinstance(node, (ast.Set, ast.SetComp)):
        return True
    if isinstance(node, ast.Call) and _call_func_name(node) in ("set", "frozenset"):
        return True
    return False


def _annotation_is_set(annotation: ast.expr | None) -> bool:
    if annotation is None:
        return False
    text = ast.unparse(annotation).lower()
    return text.startswith("set[") or text.startswith("frozenset[") or text in ("set", "frozenset")


def _is_count_lookup(body: ast.expr, arg_name: str) -> bool:
    """``body`` is a dict/collection lookup keyed by the lambda's own
    argument -- ``counts[e]``, ``-counts.get(e, 0)``, ``counts[e].get(e)`` --
    i.e. "how much of this engine is there already", regardless of what the
    collection happens to be named."""
    if isinstance(body, ast.UnaryOp):
        return _is_count_lookup(body.operand, arg_name)
    if isinstance(body, ast.Subscript):
        idx = body.slice
        return isinstance(idx, ast.Name) and idx.id == arg_name
    if isinstance(body, ast.Call) and _call_func_name(body) == "get":
        return any(isinstance(a, ast.Name) and a.id == arg_name for a in body.args)
    return False


def _is_tuple_position_unpack(body: ast.expr, arg_name: str) -> bool:
    """``body`` picks a fixed position out of the lambda's own argument --
    ``kv[0]``/``kv[1]`` -- the ``dict.items()``/``Counter.most_common()``
    idiom for reading a ``(key, value)`` pair's key or value by position.
    Suspect for the same reason a bare count lookup is: it is usually
    reading "the count"/"the score" out of a pair whose other half is the
    engine, with nothing here to tell which (a real score comparison looks
    identical at this level), so every call shaped this way needs its own
    allowlist entry rather than being assumed safe."""
    return (
        isinstance(body, ast.Subscript)
        and isinstance(body.value, ast.Name)
        and body.value.id == arg_name
        and isinstance(body.slice, ast.Constant)
        and isinstance(body.slice.value, int)
    )


def _lambda_is_count_or_name_keyed(lam: ast.Lambda) -> bool:
    """``key=lambda e: ...`` reads a per-engine lookup keyed by ``e`` itself
    (a count, or anything else indexed by the candidate -- the shape, not
    the name, is what makes it suspect) by name alone -- ``*count*``
    anywhere in the body text catches the common ``counts``/``owner_counts``
    naming too -- or is just ``e`` itself (alphabetical order, no real key
    at all)."""
    if re.search(r"count", ast.unparse(lam.body), re.IGNORECASE):
        return True
    if len(lam.args.args) == 1:
        arg_name = lam.args.args[0].arg
        body = lam.body
        if isinstance(body, ast.Name) and body.id == arg_name:
            return True
        if (
            isinstance(body, ast.Tuple)
            and body.elts
            and isinstance(body.elts[0], ast.Name)
            and body.elts[0].id == arg_name
        ):
            return True
        if _is_count_lookup(body, arg_name) or _is_tuple_position_unpack(body, arg_name):
            return True
        if isinstance(body, ast.Tuple) and body.elts:
            first = body.elts[0]
            if _is_count_lookup(first, arg_name) or _is_tuple_position_unpack(first, arg_name):
                return True
    return False


def _walk_own_body(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    """Like ``ast.walk(func)``, but does not descend into a NESTED function's
    own body -- that nested function is scanned separately, under its own
    qualified name, so a line inside it is attributed exactly once."""
    seen: list[ast.AST] = []
    stack: list[ast.AST] = [func]
    root = True
    while stack:
        node = stack.pop()
        if not root and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        root = False
        seen.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return seen


def _taint_set_names(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Names, local to ``func``, this scanner traces back to a set: a
    parameter annotated ``set[...]``/``frozenset[...]``, a local assigned a
    set literal/comprehension/``set()`` call, a set binary op
    (``&``/``|``/``^``/``-``) on tainted operands, or the result of a call
    that was passed a tainted (or literal-set) argument -- a conservative,
    single-function heuristic that is enough to trace #569's
    ``_group_eligible_engines(..., survivors, ...)`` indirection without a
    real dataflow analysis.
    """
    tainted: set[str] = set()
    args = func.args
    for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
        if _annotation_is_set(arg.annotation):
            tainted.add(arg.arg)

    def _value_is_tainted(value: ast.expr) -> bool:
        if _is_set_literal_like(value):
            return True
        if isinstance(value, ast.Name):
            return value.id in tainted
        if isinstance(value, ast.BinOp) and isinstance(value.op, _SET_BINOPS):
            return _value_is_tainted(value.left) or _value_is_tainted(value.right)
        if isinstance(value, ast.Call):
            if _call_func_name(value) == "sorted":
                # sorted() always imposes a canonical order, regardless of
                # its input's own order -- it cures set-order-dependence,
                # it does not inherit it.
                return False
            call_args = list(value.args) + [kw.value for kw in value.keywords]
            return any(_value_is_tainted(a) for a in call_args)
        return False

    # Fixed-point over a few passes: a late-discovered taint can feed an
    # earlier-textually-listed call's argument check once more.
    for _ in range(4):
        changed = False
        for node in _walk_own_body(func):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                name = node.targets[0].id
                if name not in tainted and _value_is_tainted(node.value):
                    tainted.add(name)
                    changed = True
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and (
                    _annotation_is_set(node.annotation)
                    or (node.value is not None and _value_is_tainted(node.value))
                )
            ):
                if node.target.id not in tainted:
                    tainted.add(node.target.id)
                    changed = True
        if not changed:
            break
    return tainted


def _collect_sorted_returning_funcs(tree: ast.AST) -> frozenset[str]:
    """Function names (anywhere in ``tree``) all of whose ``return <value>``
    statements return a bare ``sorted(...)`` call, or call another function
    already found to be sorted-returning -- one level of same-module
    propagation, e.g. ``tie_break._settled_workload_leaders``, whose own
    ``return sorted(...)`` makes its CALLER's ``leaders = _settled_workload_
    leaders(...)`` just as order-significant as writing ``sorted(...)``
    inline would have been. Looked up by bare name (good enough within one
    module; this scanner never resolves cross-module calls anywhere)."""
    funcs: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs[node.name] = node

    sorted_returning: set[str] = set()
    for _ in range(4):
        changed = False
        for name, func in funcs.items():
            if name in sorted_returning:
                continue
            returns = [
                n for n in _walk_own_body(func) if isinstance(n, ast.Return) and n.value is not None
            ]
            if not returns:
                continue

            def _is_sorted_or_propagated(value: ast.expr | None) -> bool:
                if isinstance(value, ast.Call):
                    fname = _call_func_name(value)
                    return fname == "sorted" or fname in sorted_returning
                return False

            if all(_is_sorted_or_propagated(r.value) for r in returns):
                sorted_returning.add(name)
                changed = True
        if not changed:
            break
    return frozenset(sorted_returning)


def _taint_sorted_names(
    func: ast.FunctionDef | ast.AsyncFunctionDef, sorted_returning_funcs: frozenset[str]
) -> set[str]:
    """Names, local to ``func``, assigned from a bare ``sorted(...)`` call or
    from a call to a ``sorted_returning_funcs`` helper -- these are name-
    sorted sequences; indexing one at ``[0]`` later in the function is the
    same bias as ``sorted(X)[0]`` written inline, just one assignment away
    from it (the pre-#503 shape of ``break_owner_tie``/``_pick_winner``:
    ``ranked = sorted(engines, key=...); winner, runner_up = ranked[0],
    ranked[1]``)."""
    tainted: set[str] = set()

    def _value_is_sorted(value: ast.expr) -> bool:
        if isinstance(value, ast.Call):
            fname = _call_func_name(value)
            if fname == "sorted" or fname in sorted_returning_funcs:
                return True
        if isinstance(value, ast.Name):
            return value.id in tainted
        return False

    for _ in range(4):
        changed = False
        for node in _walk_own_body(func):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                name = node.targets[0].id
                if name not in tainted and _value_is_sorted(node.value):
                    tainted.add(name)
                    changed = True
        if not changed:
            break
    return tainted


def _is_most_common_first_pair(node: ast.AST) -> bool:
    """``Counter(...).most_common(1)[0]`` -- the single most-common
    ``(item, count)`` pair. ``[0][0]`` on top of it unwraps the item itself;
    either way the pick is "whichever key has the highest count", the same
    bias as ``max(counts, key=counts.get)`` wearing a different idiom."""
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == 0
        and isinstance(node.value, ast.Call)
        and _call_func_name(node.value) == "most_common"
        and len(node.value.args) > 0
        and isinstance(node.value.args[0], ast.Constant)
        and node.value.args[0].value == 1
    )


def _for_keeps_first_then_breaks(node: ast.For) -> bool:
    """``for e in engines: winner = e; break`` -- the loop body is exactly
    one assignment of the loop variable followed by an unconditional
    ``break``, so whichever element the iterable happens to produce first
    is "the" answer, with no comparison against any other element at all --
    the most literal possible case of order deciding the outcome, set or
    not."""
    body = node.body
    if len(body) != 2:
        return False
    assign, brk = body
    if not isinstance(brk, ast.Break):
        return False
    if not (
        isinstance(assign, ast.Assign)
        and len(assign.targets) == 1
        and isinstance(assign.targets[0], ast.Name)
    ):
        return False
    value = assign.value
    if isinstance(node.target, ast.Name):
        return isinstance(value, ast.Name) and value.id == node.target.id
    return False


def _scan_function(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    file_name: str,
    qualname: str,
    sorted_returning_funcs: frozenset[str] = frozenset(),
) -> list[Finding]:
    findings: list[Finding] = []
    tainted = _taint_set_names(func)
    sorted_tainted = _taint_sorted_names(func, sorted_returning_funcs)

    for node in _walk_own_body(func):
        # Rule 1: sorted(X)[0], or name=sorted(...) (possibly through one
        # sorted-returning helper) indexed at [0] later in the function.
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == 0
            and (
                (isinstance(node.value, ast.Call) and _call_func_name(node.value) == "sorted")
                or (isinstance(node.value, ast.Name) and node.value.id in sorted_tainted)
            )
        ):
            findings.append(
                Finding(
                    file_name,
                    qualname,
                    RULE_SORTED_INDEX0,
                    node.lineno,
                    ast.unparse(node),
                )
            )

        # Rule 2 (continued): Counter(...).most_common(1)[0] / [0][0] --
        # "whichever key has the highest count", a count-keyed pick wearing
        # a different idiom than min/max.
        if isinstance(node, ast.Subscript) and _is_most_common_first_pair(node):
            findings.append(
                Finding(
                    file_name,
                    qualname,
                    RULE_MINMAX_COUNT_OR_NAME,
                    node.lineno,
                    ast.unparse(node),
                )
            )

        # Rule 5: for e in engines: winner = e; break
        if isinstance(node, ast.For) and _for_keeps_first_then_breaks(node):
            findings.append(
                Finding(
                    file_name,
                    qualname,
                    RULE_FIRST_SEEN_BREAK,
                    node.lineno,
                    ast.unparse(node),
                )
            )

        # Rule 4: winner = "aurora_postgresql" (unconditional literal only --
        # an if/ternary keyed on a real score comparison is not this rule).
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and _DECISION_VAR_RE.search(node.targets[0].id)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and node.value.value in _KNOWN_ENGINES
        ):
            findings.append(
                Finding(
                    file_name,
                    qualname,
                    RULE_LITERAL_PREFERENCE,
                    node.lineno,
                    ast.unparse(node),
                )
            )

        if not (isinstance(node, ast.Call) and _call_func_name(node) in ("min", "max")):
            continue

        key_kw = next((kw for kw in node.keywords if kw.arg == "key"), None)
        iterable = node.args[0] if node.args else None

        # Rule 3: min/max(<set>, key=...) -- a tie's "first" element depends
        # on set iteration order, however the key itself is computed.
        if iterable is not None and (
            _is_set_literal_like(iterable)
            or (isinstance(iterable, ast.Name) and iterable.id in tainted)
        ):
            findings.append(
                Finding(
                    file_name,
                    qualname,
                    RULE_SET_ORDER,
                    node.lineno,
                    ast.unparse(node),
                )
            )
            continue  # one rule per call site -- rule 3 already explains it

        # Rule 2: min/max(X, key=lambda e: ...count... or lambda e: e)
        if key_kw is not None and isinstance(key_kw.value, ast.Lambda):
            if _lambda_is_count_or_name_keyed(key_kw.value):
                findings.append(
                    Finding(
                        file_name,
                        qualname,
                        RULE_MINMAX_COUNT_OR_NAME,
                        node.lineno,
                        ast.unparse(node),
                    )
                )

    # Rule 3 (continued): next(iter(<set>))
    for node in _walk_own_body(func):
        if not (isinstance(node, ast.Call) and _call_func_name(node) == "next"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Call):
            continue
        inner = node.args[0]
        if _call_func_name(inner) != "iter" or not inner.args:
            continue
        target = inner.args[0]
        if _is_set_literal_like(target) or (isinstance(target, ast.Name) and target.id in tainted):
            findings.append(
                Finding(file_name, qualname, RULE_SET_ORDER, node.lineno, ast.unparse(node))
            )

    return findings


def _qualify(stack: list[str]) -> str:
    return ".".join(stack)


def _scan_module(path: Path) -> list[Finding]:
    """Every finding in ``path``, keyed to its innermost enclosing function
    (dotted through any enclosing class/function, e.g.
    ``TableNameResolver.resolve`` or ``check_summary_grounding.judge``)."""
    tree = ast.parse(path.read_text())
    file_name = path.name
    sorted_returning_funcs = _collect_sorted_returning_funcs(tree)
    findings: list[Finding] = []

    def visit(node: ast.AST, stack: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = _qualify([*stack, child.name])
                findings.extend(_scan_function(child, file_name, qualname, sorted_returning_funcs))
                visit(child, [*stack, child.name])
            elif isinstance(child, ast.ClassDef):
                visit(child, [*stack, child.name])
            else:
                visit(child, stack)

    visit(tree, [])
    return findings


@lru_cache(maxsize=1)
def _discover_other_engine_assigning_files() -> frozenset[Path]:
    """Any ``.py`` file under ``src/`` (outside ``src/agents/referee/``) that
    writes ``assigned_engine`` -- an attribute store (``qa.assigned_engine =
    ...``) or a dict-key store (``qa["assigned_engine"] = ...``) -- found by
    AST, not a hardcoded list, so a future module that starts assigning
    engines is picked up automatically instead of silently sitting outside
    this guard's scope.
    """
    found: set[Path] = set()
    for path in _SRC_DIR.rglob("*.py"):
        try:
            rel = path.relative_to(_REFEREE_DIR)
            is_in_referee = ".." not in rel.parts
        except ValueError:
            is_in_referee = False
        if is_in_referee:
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        if "assigned_engine" not in text:
            # Cheap substring pre-filter: ast.parse-ing all of src/ (every
            # test run) is the slow part, and a file that never even
            # mentions "assigned_engine" as text cannot contain it as an
            # AST store target either.
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            hit = (
                isinstance(node, ast.Attribute)
                and node.attr == "assigned_engine"
                and isinstance(node.ctx, ast.Store)
            ) or (
                isinstance(node, ast.Subscript)
                and isinstance(node.slice, ast.Constant)
                and node.slice.value == "assigned_engine"
                and isinstance(node.ctx, ast.Store)
            )
            if hit:
                found.add(path)
                break
    return frozenset(found)


def _scanned_files() -> list[Path]:
    referee_files = sorted(p for p in _REFEREE_DIR.glob("*.py") if p.name != "__init__.py")
    other_files = sorted(_discover_other_engine_assigning_files())
    return [*referee_files, *other_files]


@lru_cache(maxsize=1)
def _all_findings() -> list[Finding]:
    findings: list[Finding] = []
    for path in _scanned_files():
        findings.extend(_scan_module(path))
    return findings


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_every_engine_assigning_module_is_in_scope() -> None:
    """Self-check: every module that writes ``assigned_engine`` today is
    already inside ``src/agents/referee/`` -- if a future change moves (or
    adds) one outside it, ``_discover_other_engine_assigning_files`` must
    pick it up, and this test documents that expectation rather than letting
    the scanner's scope silently drift from the real routing code."""
    assert _discover_other_engine_assigning_files() == set()


def test_no_unallowlisted_engine_bias_in_referee() -> None:
    findings = _all_findings()
    unallowlisted = [f for f in findings if f.key() not in _ALLOWLIST]
    assert not unallowlisted, "Unallowlisted engine-bias pattern(s) found:\n" + "\n".join(
        f"  {f.file}:{f.lineno} in {f.qualname}() [{f.rule}]: {f.detail}" for f in unallowlisted
    )


def test_allowlist_has_no_stale_entries() -> None:
    """Every allowlist entry must still match something real -- otherwise it
    is silently exempting code that no longer exists (or never matched),
    which is worse than no allowlist at all."""
    findings = _all_findings()
    found_keys = {f.key() for f in findings}
    stale = [key for key in _ALLOWLIST if key not in found_keys]
    assert not stale, "Stale allowlist entries (no longer match anything found):\n" + "\n".join(
        f"  {file}::{qualname} [{rule}]" for file, qualname, rule in stale
    )


# ---------------------------------------------------------------------------
# Fixture proofs -- each rule fires on a small synthetic snippet, independent
# of whatever main's code currently looks like.
# ---------------------------------------------------------------------------


def _scan_source(source: str) -> list[Finding]:
    tree = ast.parse(source)
    sorted_returning_funcs = _collect_sorted_returning_funcs(tree)
    findings: list[Finding] = []

    def visit(node: ast.AST, stack: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = _qualify([*stack, child.name])
                findings.extend(
                    _scan_function(child, "fixture.py", qualname, sorted_returning_funcs)
                )
                visit(child, [*stack, child.name])
            elif isinstance(child, ast.ClassDef):
                visit(child, [*stack, child.name])
            else:
                visit(child, stack)

    visit(tree, [])
    return findings


def test_rule_catches_sorted_index0_name_order_pick() -> None:
    source = """
def pick_engine(engines):
    return sorted(engines)[0]
"""
    findings = _scan_source(source)
    assert [f.rule for f in findings] == [RULE_SORTED_INDEX0]


def test_rule_catches_min_max_keyed_by_count() -> None:
    source = """
def pick_engine(candidates, counts):
    return min(candidates, key=lambda e: (-counts.get(e, 0), e))
"""
    findings = _scan_source(source)
    assert [f.rule for f in findings] == [RULE_MINMAX_COUNT_OR_NAME]


def test_rule_catches_min_max_keyed_by_bare_name() -> None:
    source = """
def pick_engine(engines):
    return max(engines, key=lambda e: e)
"""
    findings = _scan_source(source)
    assert [f.rule for f in findings] == [RULE_MINMAX_COUNT_OR_NAME]


def test_rule_catches_next_iter_over_a_set_literal() -> None:
    source = """
def pick_engine():
    return next(iter({"aurora_mysql", "aurora_postgresql"}))
"""
    findings = _scan_source(source)
    assert [f.rule for f in findings] == [RULE_SET_ORDER]


def test_rule_catches_max_over_a_set_typed_parameter() -> None:
    source = """
def pick_engine(survivors: set[str], scores):
    return max(survivors, key=lambda e: scores[e])
"""
    findings = _scan_source(source)
    assert [f.rule for f in findings] == [RULE_SET_ORDER]


def test_rule_traces_a_set_through_one_level_of_indirection() -> None:
    """Proves the #569 shape: the set never reaches min/max directly -- it
    is handed to another call first, and the RESULT of that call is what
    min/max sees."""
    source = """
def pick_engine(survivors: set[str], scores):
    eligible = list(survivors)
    return max(eligible, key=lambda e: scores[e])
"""
    findings = _scan_source(source)
    assert [f.rule for f in findings] == [RULE_SET_ORDER]


def test_rule_catches_literal_engine_preference_assignment() -> None:
    source = """
def choose(mysql_total, pg_total):
    if mysql_total == pg_total:
        winner = "aurora_postgresql"
    return winner
"""
    findings = _scan_source(source)
    assert [f.rule for f in findings] == [RULE_LITERAL_PREFERENCE]


def test_score_conditioned_ternary_is_not_flagged_as_literal_preference() -> None:
    """A real score comparison deciding between two literal engine names is
    not an unconditional preference, even though one branch is a literal."""
    source = """
def choose(mysql_total, pg_total):
    winner = "aurora_mysql" if mysql_total > pg_total else "aurora_postgresql"
    return winner
"""
    findings = _scan_source(source)
    assert findings == []


def test_plain_capability_branch_is_not_flagged() -> None:
    """if engine == "opensearch": ... is a per-engine capability/display
    branch, not a preference among competing candidates -- out of scope by
    design (see module docstring)."""
    source = """
def describe(engine):
    if engine == "opensearch":
        return "search"
    return "other"
"""
    findings = _scan_source(source)
    assert findings == []


def test_guarded_singleton_set_is_not_flagged() -> None:
    """next(iter(x)) immediately after a len(x) == 1 guard is not flagged by
    this scanner's structural rules -- it is still allowlisted individually
    in the real sites that do this (see _ALLOWLIST), because this scanner
    cannot verify the guard itself; this fixture documents the shape the
    allowlist exists for, not a rule that silently exempts it."""
    source = """
def resolve(candidates: set[str]):
    if len(candidates) == 1:
        return next(iter(candidates))
    return None
"""
    findings = _scan_source(source)
    assert [f.rule for f in findings] == [RULE_SET_ORDER]


def test_rule_catches_sorted_name_assigned_then_indexed_later() -> None:
    """The actual pre-#503 shape: sorted() is not indexed inline -- it is
    assigned to a name first, and that name is indexed several lines
    later."""
    source = """
def pick_engine(engines, counts):
    ranked = sorted(engines, key=lambda e: (-counts.get(e, 0), e))
    winner, runner_up = ranked[0], ranked[1]
    return winner
"""
    findings = _scan_source(source)
    assert [f.rule for f in findings] == [RULE_SORTED_INDEX0]


def test_rule_traces_sorted_through_a_sorted_returning_helper() -> None:
    """tie_break.py's own shape: a helper function whose only return is a
    bare sorted(...) call; a caller's local assigned from calling it is just
    as order-significant as sorted(...) written inline would have been."""
    source = """
def _leaders(tied, counts):
    top = max(counts.values())
    return sorted(e for e in tied if counts[e] == top)


def pick(tied, counts):
    leaders = _leaders(tied, counts)
    return leaders[0]
"""
    findings = _scan_source(source)
    assert [(f.qualname, f.rule) for f in findings] == [("pick", RULE_SORTED_INDEX0)]


def test_rule_catches_min_max_keyed_by_tuple_position() -> None:
    """max(d.items(), key=lambda kv: kv[1])[0] -- the dict.items() "key or
    value by position" idiom; indistinguishable from a real score
    comparison at the AST level, so every instance needs its own
    allowlist entry rather than being assumed safe."""
    source = """
def pick_engine(counts):
    return max(counts.items(), key=lambda kv: kv[1])[0]
"""
    findings = _scan_source(source)
    assert [f.rule for f in findings] == [RULE_MINMAX_COUNT_OR_NAME]


def test_rule_catches_counter_most_common_first_pair() -> None:
    source = """
def pick_engine(assigned_engines):
    from collections import Counter
    return Counter(assigned_engines).most_common(1)[0][0]
"""
    findings = _scan_source(source)
    assert [f.rule for f in findings] == [RULE_MINMAX_COUNT_OR_NAME]


def test_rule_catches_for_loop_keeps_first_then_breaks() -> None:
    source = """
def pick_engine(engines):
    for e in engines:
        winner = e
        break
    return winner
"""
    findings = _scan_source(source)
    assert [f.rule for f in findings] == [RULE_FIRST_SEEN_BREAK]


def test_for_loop_that_compares_before_breaking_is_not_flagged() -> None:
    """A loop that actually compares candidates before deciding is not this
    rule, even though it still breaks -- the shape this rule catches is
    specifically "no comparison happened at all"."""
    source = """
def pick_engine(engines, scores):
    best = None
    for e in engines:
        if best is None or scores[e] > scores[best]:
            best = e
    return best
"""
    findings = _scan_source(source)
    assert findings == []


# ---------------------------------------------------------------------------
# Regression: the exact pre-#503 source of break_owner_tie and _pick_winner,
# at commit 0ae4b7d7c80ff7178d3bc0a9ebb9a9913a5eb912 (the parent of #503's own
# fix, d7b90757) -- embedded verbatim so this scanner is proven against the
# real historical bug, not just a shape resembling it.
# ---------------------------------------------------------------------------

_PRE_503_BREAK_OWNER_TIE = '''
def break_owner_tie(
    engines: list[str],
    qids: list[str],
    query_by_id: Mapping[str, dict],
    source_aurora: str | None,
    table_traffic: Mapping[tuple[str, str], float],
    engine_counts: Mapping[str, int],
) -> tuple[str, str]:
    """Pick the owner among engines with the same score, and say why (#296).

    1. the source-compatible engine (the Aurora engine of the source's dialect);
    2. the engine already serving more of the queries' tables' traffic (calls/s);
    3. the engine already serving more queries;
    4. the engine name, so the pick never depends on dict or set order.

    Cache engines are never in ``engines`` (they own nothing), so a tie can never
    tip a query, hot or not, onto the cache.
    """
    if source_aurora in engines:
        return source_aurora, "source-compatible engine"
    tables = sorted(
        {t for qid in qids for t in (query_by_id.get(qid) or {}).get("tables_accessed") or []}
    )

    def traffic(e: str) -> float:
        return round(sum(table_traffic.get((t, e), 0.0) for t in tables), 6)

    ranked = sorted(engines, key=lambda e: (-traffic(e), -engine_counts.get(e, 0), e))
    winner, runner_up = ranked[0], ranked[1]
    if traffic(winner) > traffic(runner_up):
        return winner, f"serves more of these tables' traffic ({traffic(winner):.2f} calls/s)"
    if engine_counts.get(winner, 0) > engine_counts.get(runner_up, 0):
        return winner, "serves more queries"
    return winner, "name order"
'''

_PRE_503_PICK_WINNER = '''
def _pick_winner(
    scores: dict[str, float],
    current_engines: set[str],
    source_compatible_engine: str | None,
) -> tuple[str, str | None]:
    """Highest score wins. Returns ``(winner, tie_kind)``; ``tie_kind`` is
    ``None`` when one engine strictly scores highest (no tie to resolve).

    Tie order (#502 review round 3), none of it alphabetical:
    1. the source-compatible engine (ADR-031), when it is tied for the top
       score -- ``tie_kind="source_compatible"``;
    2. otherwise, the query's own current engine (or, for a co-dependency
       group, any member's), when IT is tied for the top -- a tie is not
       "strictly higher," so it is never an opportunity for some other
       engine to pick up queries it did not already have;
       ``tie_kind="stability"``;
    3. otherwise there is no ADR-031 rule for this tie yet (#503): a
       deterministic choice is still returned (engines are sorted by name
       only so two runs on the same input agree, not because the name
       decides anything), but it is reported honestly as
       ``tie_kind="tie_unresolved"`` rather than presented as a real rule.
    """
    top = max(scores.values())
    tied = sorted(e for e, s in scores.items() if s == top)
    if len(tied) == 1:
        return tied[0], None
    if source_compatible_engine and source_compatible_engine in tied:
        return source_compatible_engine, "source_compatible"
    staying = sorted(set(tied) & current_engines)
    if staying:
        return staying[0], "stability"
    return tied[0], "tie_unresolved"
'''


def test_regression_pre_503_break_owner_tie_is_caught() -> None:
    """At 0ae4b7d7 (the commit immediately before #503's fix, d7b90757),
    break_owner_tie's real tie-break was ``ranked = sorted(engines, key=...);
    winner, runner_up = ranked[0], ranked[1]`` -- traffic, then query count,
    then name order (see the function's own docstring above). This is the
    shape rule 1's taint tracking exists to catch without needing the
    `[0]` written inline."""
    findings = _scan_source(_PRE_503_BREAK_OWNER_TIE)
    assert [(f.qualname, f.rule) for f in findings] == [("break_owner_tie", RULE_SORTED_INDEX0)]


def test_regression_pre_503_pick_winner_is_caught() -> None:
    """Same commit, same bug, reality_check.py's copy: ``tied =
    sorted(...)``/``staying = sorted(...)`` each indexed at [0] (twice for
    ``tied``, once for ``staying``) once no ADR-031 rule actually resolves
    the tie."""
    findings = _scan_source(_PRE_503_PICK_WINNER)
    assert [f.rule for f in findings] == [RULE_SORTED_INDEX0] * 3
