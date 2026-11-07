# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Dependency checks and dependency order for one step's routines (#1937).

Every routine here is synthetic.
"""

from __future__ import annotations

import difflib
import itertools
import json
import random
from pathlib import Path
from typing import Any

import pytest

from src.shared.catalog.routine_rewrites import routine_rewrite_rules
from src.tools.routines.dependencies import (
    MovedObjects,
    RoutineNode,
    check_dependencies,
    dependency_order,
    mysql_node,
    postgresql_node,
    with_dependency_check,
)
from src.tools.routines.mysql_checks import MySqlRoutine, check_mysql_routine
from src.tools.routines.postgresql_checks import PostgresRoutine
from src.tools.routines.verdict import RoutineDependency
from tests.unit.tools.sql._timing import assert_linear

CORPUS = Path(__file__).resolve().parents[3] / "fixtures" / "routines" / "mysql"


def _node(
    name: str,
    calls: tuple[str, ...] = (),
    tables: tuple[str, ...] = (),
    *,
    schema: str = "shop",
    engine: str = "mysql",
    kind: str = "procedure",
    routine_id: str | None = None,
) -> RoutineNode:
    deps = [_dep("routine", c) for c in calls] + [_dep("table", t) for t in tables]
    return RoutineNode(
        routine_id=routine_id or f"{schema}.{name}",
        source_engine=engine,  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        schema_name=schema,
        name=name,
        dependencies=tuple(deps),
    )


def _dep(kind: str, qualified: str) -> RoutineDependency:
    """A dependency: ``qualified`` is ``[schema.]name``, a routine's optionally prefixed with
    its kind (``trigger:orders_bi``)."""
    routine_kind, _, qualified = qualified.rpartition(":")
    schema, _, name = qualified.rpartition(".")
    return RoutineDependency(
        kind=kind,  # type: ignore[arg-type]
        name=name,
        schema_name=schema or None,
        routine_kind=routine_kind or None,  # type: ignore[arg-type]
    )


def _order(nodes: list[RoutineNode]) -> tuple[str, ...]:
    return dependency_order(nodes).order


# --------------------------------------------------------------------------- the check


def _check(node: RoutineNode, moved: MovedObjects, target: str = "aurora_mysql") -> Any:
    return check_dependencies(node, moved, target_engine=target)


def test_a_dependency_the_step_does_not_move_fails_and_is_named() -> None:
    node = _node("p", calls=("audit.log_event", "order_tax"), tables=("orders", "billing.invoices"))
    moved = MovedObjects.named(
        tables=[("shop", "orders")], routines=[("procedure", "shop", "order_tax")]
    )
    result = _check(node, moved)
    assert result.rule_id == "mysql-routine-dependency-not-moved"
    assert result.check == "dependencies" and result.outcome == "fail"
    assert result.impact == "blocks_move" and result.reason and result.fix_hint
    assert result.findings == ("routine audit.log_event", "table billing.invoices")
    everything = MovedObjects.named(
        tables=[("shop", "orders"), ("billing", "invoices")],
        routines=[("procedure", "shop", "order_tax"), ("function", "audit", "log_event")],
    )
    passed = _check(node, everything)
    assert passed.outcome == "pass" and passed.findings == ()


def test_an_unqualified_name_is_the_routines_own_schema() -> None:
    node = _node("p", tables=("orders",), schema="shop")
    assert _check(node, MovedObjects.named(tables=[("shop", "orders")])).outcome == "pass"
    failed = _check(node, MovedObjects.named(tables=[("other", "orders")]))
    assert failed.findings == ("table shop.orders",)
    # Names are compared in lower case, however the step's objects are written.
    assert _check(node, MovedObjects.named(tables=[("SHOP", "Orders")])).outcome == "pass"


def test_an_incomplete_list_is_unknown_never_pass_and_fails_only_on_names() -> None:
    """An incomplete list (every PL/pgSQL function's) is never read as a pass: its outcome
    is ``unknown`` with a finding saying why, which does not block; a name it does hold
    that the step does not move still fails."""
    assert _check(_node("p"), MovedObjects()).outcome == "pass"
    node = _node("p", tables=("orders",)).model_copy(update={"dependencies_complete": False})
    unknown = _check(node, MovedObjects.named(tables=[("shop", "orders")]))
    assert unknown.outcome == "unknown"
    (finding,) = unknown.findings
    assert "not fully known" in finding and "confirmed when the routine loads" in finding
    failed = _check(node, MovedObjects())
    assert failed.outcome == "fail" and failed.findings == ("table shop.orders",)
    empty = _node("q").model_copy(update={"dependencies_complete": False})
    assert _check(empty, MovedObjects()).outcome == "unknown"


def test_the_dependency_check_shows_in_the_verdict_and_unknown_does_not_block() -> None:
    routine = MySqlRoutine(
        routine_id="shop.p",
        kind="procedure",
        create_sql="CREATE PROCEDURE p() BEGIN PREPARE s FROM @q; EXECUTE s; END",
    )
    # A same-major move, so no version-change rule (#1952) is in the verdict.
    verdict = check_mysql_routine(
        routine,
        target_engine="aurora_mysql",
        target_line="8.0",
        target_user="admin",
        source_version="8.0.36",
    )
    node = mysql_node(routine, verdict)
    assert not node.dependencies_complete
    checked = with_dependency_check(verdict, node, MovedObjects())
    (result,) = [c for c in checked.checks if c.check == "dependencies"]
    assert result.outcome == "unknown"
    assert checked.unknown_rules == ("mysql-routine-dependency-not-moved",)
    assert "mysql-routine-dependency-not-moved" not in checked.failed_rules
    # The dynamic SQL rule is what needs attention here, not the unknown dependencies.
    assert checked.failed_rules == verdict.failed_rules
    missing = _node("p", tables=("orders",))
    blocked = with_dependency_check(verdict, missing, MovedObjects())
    assert blocked.needs_attention and "mysql-routine-dependency-not-moved" in blocked.failed_rules
    # Checking again replaces the earlier result, it does not add a second one.
    again = with_dependency_check(blocked, missing, MovedObjects.named(tables=[("shop", "orders")]))
    assert [c.check for c in again.checks].count("dependencies") == 1
    assert with_dependency_check(verdict, node, MovedObjects(), rules=()) == verdict
    with pytest.raises(ValueError, match="not this routine"):
        with_dependency_check(verdict, _node("other"), MovedObjects())


def test_a_routine_dependency_matches_only_its_own_kind() -> None:
    """A procedure and a function of one name, or a trigger named like a routine, are
    never taken for each other (review of #1967)."""
    calls_procedure = _node("a", calls=("procedure:x",))
    moved_function = MovedObjects.named(routines=[("function", "shop", "x")])
    assert _check(calls_procedure, moved_function).findings == ("routine shop.x",)
    assert (
        _check(calls_procedure, MovedObjects.named(routines=[("procedure", "shop", "x")])).outcome
        == "pass"
    )
    untold = _node("a", calls=("x",))
    assert _check(untold, MovedObjects.named(routines=[("trigger", "shop", "x")])).outcome == "fail"
    assert _check(untold, moved_function).outcome == "pass"
    # No false cycle: a calls procedure x; function x calls a.
    nodes = [
        _node("a", calls=("procedure:x",)),
        _node("x", kind="procedure", routine_id="shop.x#p"),
        _node("x", calls=("function:a", "procedure:a"), kind="function", routine_id="shop.x#f"),
    ]
    result = dependency_order(nodes)
    assert result.cycles == ()
    assert result.order == ("shop.x#p", "shop.a", "shop.x#f")


def test_the_check_is_the_catalogs_rule_for_the_engine() -> None:
    pg = _node("f", tables=("orders",), engine="postgresql", kind="function", schema="public")
    result = _check(pg, MovedObjects(), target="aurora_postgresql")
    assert result.rule_id == "postgresql-routine-dependency-not-moved"
    assert _check(pg, MovedObjects(), target="aurora_mysql") is None
    assert (
        check_dependencies(pg, MovedObjects(), target_engine="aurora_postgresql", rules=()) is None
    )


def test_moved_objects_of_a_step_holds_its_routines() -> None:
    nodes = [_node("a"), _node("b", calls=("a",))]
    moved = MovedObjects.of(nodes, tables=[("shop", "t")])
    assert moved.routines == frozenset({("procedure", "shop", "a"), ("procedure", "shop", "b")})
    assert _check(nodes[1], moved).outcome == "pass"


def test_mysql_and_postgresql_nodes_carry_the_checkers_dependencies() -> None:
    text = (CORPUS / "clean-procedure.sql").read_text(encoding="utf-8")
    routine = MySqlRoutine(routine_id="shop.clean_procedure", kind="procedure", create_sql=text)
    verdict = check_mysql_routine(
        routine, target_engine="aurora_mysql", target_user="admin", source_version="8.0.36"
    )
    node = mysql_node(routine, verdict)
    assert (node.schema_name, node.name) == ("shop", "clean_procedure")
    assert node.dependencies == verdict.dependencies and node.dependencies_complete
    with pytest.raises(ValueError, match="not this routine"):
        mysql_node(routine.model_copy(update={"routine_id": "shop.other"}), verdict)

    trigger = PostgresRoutine(
        routine_id="public.orders.audit",
        kind="trigger",
        create_sql=(
            "CREATE TRIGGER audit AFTER UPDATE ON public.orders FOR EACH ROW "
            "EXECUTE FUNCTION audit.log_change()"
        ),
    )
    node = postgresql_node(trigger, schema_name="public", name="audit")
    assert [(d.kind, d.schema_name, d.name, d.routine_kind) for d in node.dependencies] == [
        ("routine", "audit", "log_change", "function"),
        ("table", "public", "orders", None),
    ]
    assert node.dependencies_complete
    function = PostgresRoutine(
        routine_id="public.f()",
        kind="function",
        create_sql="CREATE FUNCTION public.f() RETURNS int LANGUAGE sql AS $$ select 1 $$",
        language="sql",
        owner_is_superuser=False,
    )
    given = [_dep("table", "public.orders")]
    node = postgresql_node(function, schema_name="public", name="f", dependencies=given)
    assert node.dependencies == tuple(given) and not node.dependencies_complete
    node = postgresql_node(
        function, schema_name="public", name="f", dependencies=given, dependencies_complete=True
    )
    assert node.dependencies_complete
    broken = trigger.model_copy(update={"create_sql": "CREATE TRIGGER broken"})
    assert not postgresql_node(broken, schema_name="public", name="broken").dependencies_complete


# --------------------------------------------------------------------------- the order


def test_routines_follow_what_they_call_ties_in_id_order() -> None:
    nodes = [
        _node("report", calls=("totals", "tax")),
        _node("totals", calls=("tax",)),
        _node("tax"),
        _node("audit"),
        _node("zeta"),
        _node("alpha", calls=("zeta",)),
    ]
    expected = ("shop.audit", "shop.tax", "shop.totals", "shop.report", "shop.zeta", "shop.alpha")
    for permutation in itertools.permutations(nodes):
        assert _order(list(permutation)) == expected


def test_a_trigger_comes_after_its_function_and_after_the_trigger_it_follows() -> None:
    pg = [
        _node(
            "audit",
            calls=("audit.log_change",),
            tables=("orders",),
            engine="postgresql",
            kind="trigger",
            schema="public",
            routine_id="public.orders.audit",
        ),
        _node(
            "log_change",
            engine="postgresql",
            kind="function",
            schema="audit",
            routine_id="audit.log_change()",
        ),
    ]
    assert _order(pg) == ("audit.log_change()", "public.orders.audit")
    mysql = [
        _node("orders_bi", calls=("trigger:orders_bi_first",), kind="trigger"),
        _node("orders_bi_first", kind="trigger"),
    ]
    assert _order(mysql) == ("shop.orders_bi_first", "shop.orders_bi")


def test_an_overloaded_postgresql_name_orders_every_overload_first() -> None:
    nodes = [
        _node(
            "caller",
            calls=("public.f",),
            engine="postgresql",
            kind="function",
            schema="public",
            routine_id="public.caller()",
        ),
        _node(
            "f", engine="postgresql", kind="function", schema="public", routine_id="public.f(int)"
        ),
        _node(
            "f", engine="postgresql", kind="function", schema="public", routine_id="public.f(text)"
        ),
    ]
    assert _order(nodes) == ("public.f(int)", "public.f(text)", "public.caller()")


def test_cycles_are_reported_and_never_followed_round() -> None:
    nodes = [
        _node("b", calls=("a",)),
        _node("a", calls=("c",)),
        _node("c", calls=("b",)),
        _node("x", calls=("y",)),
        _node("y", calls=("x", "base")),
        _node("after", calls=("a", "x")),
        _node("base"),
        _node("selfish", calls=("selfish",)),
    ]
    result = dependency_order(nodes)
    assert result.cycles == (("shop.a", "shop.b", "shop.c"), ("shop.x", "shop.y"))
    assert result.order == (
        "shop.a",
        "shop.b",
        "shop.c",
        "shop.base",
        "shop.selfish",
        "shop.x",
        "shop.y",
        "shop.after",
    )
    assert sorted(result.order) == sorted(n.routine_id for n in nodes)
    # A routine calling itself is no cycle.
    assert all("shop.selfish" not in cycle for cycle in result.cycles)


def test_a_cycle_through_100k_routines_ends() -> None:
    n = 100_000
    nodes = [_node(f"r{i:06d}", calls=(f"r{(i + 1) % n:06d}",)) for i in range(n)]
    result = dependency_order(nodes)
    assert len(result.cycles) == 1 and len(result.cycles[0]) == n
    assert result.order == tuple(sorted(node.routine_id for node in nodes))


def test_a_routine_or_table_outside_the_step_adds_no_order() -> None:
    nodes = [_node("b", calls=("elsewhere.f", "a"), tables=("orders",)), _node("a")]
    assert _order(nodes) == ("shop.a", "shop.b")


def test_the_order_records_its_rule_and_a_diff_from_the_input_order() -> None:
    nodes = [_node("b", calls=("a",)), _node("a"), _node("c")]
    result = dependency_order(nodes)
    (rule,) = [r for r in routine_rewrite_rules() if r.match.rewrite == "dependency_order"]
    assert (result.rule_id, result.reason) == (rule.id, rule.reason)
    assert (
        result.diff
        == "--- source/order\n+++ target/order\n@@ -1 +0,0 @@\n-shop.b\n@@ -2,0 +2 @@\n+shop.b\n"
    )
    assert dependency_order([_node("a"), _node("b")]).diff == "unchanged\n"


@pytest.mark.parametrize("seed", range(40))
def test_the_order_diff_is_a_unified_diff_that_applies(seed: int) -> None:
    rng = random.Random(seed)
    ids = [f"s.r{i}" for i in range(rng.randint(2, 12))]
    shuffled = ids[:]
    rng.shuffle(shuffled)
    nodes = [_node(i.split(".")[1], schema="s") for i in shuffled]
    result = dependency_order(nodes)
    assert result.order == tuple(sorted(shuffled))
    if result.diff == "unchanged\n":
        assert shuffled == sorted(shuffled)
        return
    lines = result.diff.splitlines()
    assert lines[:2] == ["--- source/order", "+++ target/order"]
    # Applying the hunks to the input order gives the output order.
    out: list[str] = []
    position = 0
    for line in lines[2:]:
        if line.startswith("@@"):
            old = line.split()[1][1:]
            start, _, count = old.partition(",")
            length = int(count) if count else 1
            first = int(start) - 1 if length else int(start)
            out += shuffled[position:first]
            position = first + length
        elif line.startswith("+"):
            out.append(line[1:])
    out += shuffled[position:]
    assert out == list(result.order)
    # It moves no more routines than difflib's own diff of the two orders does.
    reference = list(difflib.unified_diff(shuffled, list(result.order), n=0, lineterm=""))

    def changed(diff: list[str]) -> int:
        return sum(1 for line in diff[2:] if line[:1] in "+-")

    assert changed(lines) <= changed(reference)


def test_two_routines_with_one_id_or_no_rule_are_value_errors() -> None:
    with pytest.raises(ValueError, match="same routine_id"):
        dependency_order([_node("a"), _node("a")])
    with pytest.raises(ValueError, match="no dependency_order rule"):
        dependency_order([_node("a")], rules=())


def test_the_same_input_gives_byte_identical_output() -> None:
    nodes = [_node("b", calls=("a", "c")), _node("a", calls=("b",)), _node("c")]
    first = json.dumps(dependency_order(nodes).model_dump(), sort_keys=True)
    assert json.dumps(dependency_order(nodes).model_dump(), sort_keys=True) == first
    # The order and cycles do not depend on the input order (only the diff from it does).
    backwards = dependency_order(list(reversed(nodes)))
    assert (backwards.order, backwards.cycles) == (
        dependency_order(nodes).order,
        dependency_order(nodes).cycles,
    )


# --------------------------------------------------------------------------- scale


def _chain(n: int) -> list[RoutineNode]:
    """``n`` routines, each calling the next and naming a table, listed in reverse."""
    return [
        _node(f"r{i:06d}", calls=(f"r{i + 1:06d}",) if i + 1 < n else (), tables=(f"t{i % 50}",))
        for i in range(n)
    ]


def test_100k_routines_order_and_check_in_linear_time() -> None:
    assert_linear(_chain, dependency_order, sizes=(12_500, 100_000))  # type: ignore[arg-type]

    def check_all(nodes: list[RoutineNode]) -> None:
        moved = MovedObjects.of(nodes, tables=[("shop", f"t{i}") for i in range(50)])
        for node in nodes:
            check_dependencies(node, moved, target_engine="aurora_mysql")

    assert_linear(_chain, check_all, sizes=(12_500, 100_000))  # type: ignore[arg-type]
