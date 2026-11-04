# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1 rules: whole-table mirrors into Aurora (#1764)."""

from __future__ import annotations

import copy

import pytest

from src.tools.data_movement.plan import JobInputs
from src.tools.transformation_contract.step1 import build_step1

from .conftest import col as _col
from .conftest import step1_inputs as _inputs
from .conftest import table as _table


def _rule(c, table_id):
    return next(r for r in c.rules if r.source_table == table_id)


def _ops(rule):
    return {f.target_column: f.op for f in rule.fields}


def test_one_rule_per_table_with_every_column_in_ordinal_order():
    c = build_step1(_inputs())
    assert [f.target_column for f in _rule(c, "wp.posts").fields] == ["ID", "title", "flag", "n"]
    assert c.step == 1 and c.target.engine == "aurora_mysql" and c.assignment_version == 2
    assert _rule(c, "wp.posts").primary_key == ["ID"]


def test_columns_without_ordinal_position_keep_collector_order_after_numbered_ones():
    tables = [
        _table(
            "wp",
            "t",
            [_col("c", "int"), _col("b", "int", 2), _col("a", "int", 1), _col("d", "int")],
        )
    ]
    c = build_step1(_inputs(tables=tables))
    assert [f.target_column for f in _rule(c, "wp.t").fields] == ["a", "b", "c", "d"]


def test_without_a_design_the_generator_gives_exact_types_and_ops():
    posts = _rule(build_step1(_inputs()), "wp.posts")
    types = {f.target_column: f.target_type for f in posts.fields}
    ops = _ops(posts)
    assert types["ID"] == "BIGINT UNSIGNED" and ops["ID"].op == "copy"
    assert types["title"] == "VARCHAR(20)" and ops["title"].op == "copy"
    assert types["n"] == "INT" and ops["n"].op == "copy"  # int(11) is INT, not a cast
    assert types["flag"] == "TINYINT(1)" and ops["flag"].op == "copy"  # the generator keeps it


def test_postgresql_canonical_spellings_are_copies():
    tables = [
        _table(
            "public",
            "events",
            [
                _col("id", "integer", 1),
                _col("created", "timestamp without time zone", 2),
                _col("name", "character varying", 3, max_length=40),
            ],
        )
    ]
    c = build_step1(_inputs(engine="postgresql", tables=tables))
    rule = _rule(c, "public.events")
    assert c.target.engine == "aurora_postgresql"
    assert {f.target_column: f.target_type for f in rule.fields} == {
        "id": "INTEGER",
        "created": "TIMESTAMP",
        "name": "VARCHAR(40)",
    }
    assert {o.op for o in _ops(rule).values()} == {"copy"}


def test_a_design_override_is_a_transform_from_the_canonical_source_type():
    design = [{"table_name": "posts", "columns": [{"name": "title", "aurora_type": "TEXT"}]}]
    posts = _rule(build_step1(_inputs(design=design)), "wp.posts")
    op = _ops(posts)["title"]
    assert op.op == "transform" and (op.from_type, op.to_type) == ("VARCHAR(20)", "TEXT")


def test_a_partial_design_keeps_the_tables_it_lacks_and_overrides_the_ones_it_has():
    design = [{"table_name": "posts", "columns": [{"name": "ID", "aurora_type": "NUMERIC(20)"}]}]
    c = build_step1(_inputs(design=design))
    log = _rule(c, "wp.log")
    assert [(f.target_column, f.target_type) for f in log.fields] == [("msg", "TEXT")]
    # The design's column replaces the generated one; the columns it lacks keep the generator's.
    posts = _rule(c, "wp.posts")
    assert [f.target_column for f in posts.fields] == ["ID", "title", "flag", "n"]
    assert posts.fields[0].target_type == "NUMERIC(20)"
    assert posts.fields[1].target_type == "VARCHAR(20)"


def test_a_type_aurora_rejects_is_unsupported():
    design = [{"table_name": "log", "columns": [{"name": "msg", "aurora_type": "NOT A TYPE; X"}]}]
    c = build_step1(_inputs(design=design))
    assert _ops(_rule(c, "wp.log"))["msg"].op == "unsupported" and c.unsupported_count == 1


def test_a_wave_table_the_collector_lacks_is_blocked_not_a_view():
    views = [{"view_id": "wp.v_posts", "view_name": "v_posts", "definition": "x"}]
    ids = ["wp.posts", "wp.log", "wp.v_posts", "wp.ghost"]
    c = build_step1(_inputs(views=views, wave_tables=ids))
    assert c.views == ["wp.v_posts"]
    assert {r.source_table for r in c.rules} == {"wp.log", "wp.posts", "wp.ghost"}
    ghost = _rule(c, "wp.ghost")
    assert ghost.fields == [] and ghost.status == "blocked"
    assert [f.code for f in ghost.findings] == ["unknown_table"]
    assert c.status == "has_blocked_rules" and c.blocked_rule_count == 1


def test_cross_engine_and_no_anchor_get_no_rules():
    c = build_step1(_inputs("cross_engine"))
    assert (
        c.status == "staged_move_pending"
        and c.status_reason == "staged relational move pending (#1702)"
    )
    for shape in ("heterogeneous", "no_move", None):
        c = build_step1(_inputs(shape))
        assert (
            c.status == "no_relational_anchor"
            and c.rules == []
            and c.status_reason == "no relational anchor"
        )


@pytest.mark.parametrize(
    ("engine", "aurora"),
    [("mariadb", "aurora_mysql"), ("postgresql", "aurora_postgresql"), ("mysql", "aurora_mysql")],
)
def test_the_target_engine_follows_the_source_family(engine, aurora):
    tables = [_table("s", "t", [_col("a", "int", 1)])]
    assert build_step1(_inputs(engine=engine, tables=tables)).target.engine == aurora


def test_an_older_sparse_collector_loads_through_its_migration():
    tables = [_table("wp", "t", [_col("a", "int", 1)])]
    del tables[0]["row_count"]  # a 4.x collector may carry no count: the migration reads one
    c = build_step1(_inputs(tables=tables, version="4.0"))
    assert _rule(c, "wp.t").fields[0].target_type == "INT"


def test_the_assignment_version_is_the_one_load_inputs_resolved():
    inputs = _inputs()
    inputs.report["assignment_summary"].pop("version")
    with pytest.raises(ValueError, match="assignment version"):
        build_step1(inputs)
    resolved = JobInputs(**{**inputs.__dict__, "assignment_version": 4})
    assert build_step1(resolved).assignment_version == 4


def test_two_schemas_sharing_a_table_name_block_both_rules():
    tables = [_table("a", "users", [_col("id", "int", 1)]), _table("b", "users", [_col("id", "int", 1)]),
              _table("a", "solo", [_col("id", "int", 1)])]  # fmt: skip
    c = build_step1(_inputs(tables=tables))
    assert c.status == "has_blocked_rules" and c.blocked_rule_count == 2
    for table_id in ("a.users", "b.users"):
        rule = _rule(c, table_id)
        assert rule.status == "blocked" and [f.code for f in rule.findings] == ["ambiguous_target"]
        assert rule.findings[0].severity == "error" and rule.findings[0].table_id == table_id
    assert _rule(c, "a.solo").status == "valid"


def test_the_input_is_not_changed():
    inputs = _inputs()
    before = copy.deepcopy(inputs.collector)
    build_step1(inputs)
    assert inputs.collector == before


def test_a_wave_entry_spelled_differently_resolves_like_the_plan_does():
    c = build_step1(_inputs(wave_tables=["posts", "WP.LOG"]))
    assert {r.source_table for r in c.rules} == {"wp.posts", "wp.log"}
    assert not [f for r in c.rules for f in r.findings if f.code == "unknown_table"]
