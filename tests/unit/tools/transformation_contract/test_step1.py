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


def test_two_schemas_sharing_a_table_name_both_get_valid_rules_with_their_fields():
    """#2494: step 1 keeps each table's source schema, so ``a.users`` and ``b.users`` are
    two targets: each rule is valid with its own table's fields."""
    tables = [_table("a", "users", [_col("id", "int", 1), _col("name", "text", 2)]),
              _table("b", "users", [_col("id", "bigint", 1)]),
              _table("a", "solo", [_col("id", "int", 1)])]  # fmt: skip
    c = build_step1(_inputs(tables=tables))
    assert c.status == "valid" and c.blocked_rule_count == 0
    assert {r.source_table: (r.status, r.target_table, [f.target_type for f in r.fields])
            for r in c.rules} == {
        "a.users": ("valid", "users", ["INT", "TEXT"]), "b.users": ("valid", "users", ["BIGINT"]),
        "a.solo": ("valid", "solo", ["INT"])}  # fmt: skip


@pytest.mark.passes_on_main  # main kept these fields too; #2479 review, #2494 keys by schema
def test_two_schemas_with_no_shared_name_keep_their_rule_fields():
    """A collector with two schemas keys its targets ``schema.table`` (#2494): every rule
    keeps its fields, as with bare keys before."""
    tables = [
        _table("a", "users", [_col("id", "int", 1)]),
        _table("b", "orders", [_col("id", "int", 1)]),
    ]
    c = build_step1(_inputs(tables=tables))
    assert [(r.source_table, r.status, [f.target_type for f in r.fields]) for r in c.rules] == [
        ("a.users", "valid", ["INT"]), ("b.orders", "valid", ["INT"])]  # fmt: skip


def test_tables_whose_names_fold_to_one_block_both_rules():
    """#2478: ``Users`` and ``users`` in one schema share the bare key the base folds to,
    so the design base leaves both out; step 1 blocks both rules, never a valid rule with
    no fields. The other table keeps its fields."""
    tables = [_table("a", "Users", [_col("id", "int", 1)]),
              _table("a", "users", [_col("id", "int", 1)]),
              _table("a", "solo", [_col("id", "int", 1)])]  # fmt: skip
    c = build_step1(_inputs(tables=tables))
    assert c.status == "has_blocked_rules" and c.blocked_rule_count == 2
    for table_id in ("a.Users", "a.users"):
        rule = _rule(c, table_id)
        assert rule.status == "blocked" and rule.fields == []
        assert [f.code for f in rule.findings] == ["colliding_table_name"]
        assert "once case and quote marks are ignored" in rule.findings[0].message
        assert "in schema a" in rule.findings[0].message
    solo = _rule(c, "a.solo")
    assert solo.status == "valid" and [f.target_type for f in solo.fields] == ["INT"]


def test_two_schemas_tables_whose_names_differ_only_by_case_keep_their_fields():
    """#2478 maintainer decision: ``sales.Orders`` and ``archive.orders`` are both designed,
    so step 1 keeps valid rules with their fields. #2494: so are ``sales.orders`` and
    ``archive.orders``, each with its own table's fields."""
    tables = [_table("sales", "Orders", [_col("id", "int", 1), _col("total", "int", 2)]),
              _table("archive", "orders", [_col("id", "int", 1)])]  # fmt: skip
    c = build_step1(_inputs(tables=tables))
    assert c.blocked_rule_count == 0
    orders = _rule(c, "sales.Orders")
    assert orders.status == "valid" and orders.findings == []
    assert [f.target_column for f in orders.fields] == ["id", "total"]
    archived = _rule(c, "archive.orders")
    assert archived.status == "valid" and archived.findings == []
    assert [f.target_column for f in archived.fields] == ["id"]
    same = [_table("sales", "orders", [_col("id", "int", 1), _col("total", "int", 2)]),
            _table("archive", "orders", [_col("id", "int", 1)])]  # fmt: skip
    c = build_step1(_inputs(tables=same))
    assert {r.source_table: (r.status, [f.target_column for f in r.fields]) for r in c.rules} == {
        "sales.orders": ("valid", ["id", "total"]), "archive.orders": ("valid", ["id"])}  # fmt: skip


@pytest.mark.parametrize("engine,system", [("postgresql", "pg_catalog"), ("mysql", "mysql")])
def test_a_system_schemas_table_is_blocked_and_the_others_keep_their_fields(engine, system):
    """#2478: the design base leaves a system schema's table out, so step 1 blocks its rule
    with the reason; two user schemas' tables keep their fields."""
    tables = [_table(system, "audit", [_col("id", "int", 1)]),
              _table("a", "users", [_col("id", "int", 1)]),
              _table("b", "orders", [_col("id", "int", 1)])]  # fmt: skip
    c = build_step1(_inputs(engine=engine, tables=tables))
    rule = _rule(c, f"{system}.audit")
    assert rule.status == "blocked" and rule.fields == []
    assert [f.code for f in rule.findings] == ["system_schema"]
    assert f"system schema {system}" in rule.findings[0].message
    for table_id in ("a.users", "b.orders"):
        assert _rule(c, table_id).status == "valid" and _rule(c, table_id).fields


@pytest.mark.parametrize("engine,system", [("postgresql", "pg_catalog"), ("mysql", "mysql")])
def test_a_system_table_never_makes_a_user_tables_name_ambiguous(engine, system):
    """#2478 review: ``pg_catalog.audit`` is left out of the design base, so it shares no
    target with ``public.audit``. The user table keeps a valid rule with its fields and
    moves; only the system table is blocked."""
    tables = [_table("public", "audit", [_col("id", "int", 1), _col("msg", "text", 2)]),
              _table(system, "audit", [_col("id", "int", 1)]),
              _table("public", "users", [_col("id", "int", 1)])]  # fmt: skip
    c = build_step1(_inputs(engine=engine, tables=tables))
    audit = _rule(c, "public.audit")
    assert audit.status == "valid" and audit.findings == []
    assert [f.target_column for f in audit.fields] == ["id", "msg"]
    assert [f.code for f in _rule(c, f"{system}.audit").findings] == ["system_schema"]
    assert c.blocked_rule_count == 1


@pytest.mark.parametrize("engine,schema", [("postgresql", "sys"), ("mysql", "pg_app")])
def test_a_schema_only_another_engine_reserves_moves(engine, schema):
    """#2478 review: ``sys`` is a user schema on PostgreSQL and ``pg_app`` a user database
    on MySQL, so their tables get valid rules with their fields. #2494: two such schemas'
    tables of one name both move, each into its own schema, never ``system_schema``."""
    tables = [_table("public", "orders", [_col("id", "int", 1)]),
              _table(schema, "items", [_col("id", "int", 1)])]  # fmt: skip
    c = build_step1(_inputs(engine=engine, tables=tables))
    for table_id in ("public.orders", f"{schema}.items"):
        rule = _rule(c, table_id)
        assert rule.status == "valid" and [f.target_column for f in rule.fields] == ["id"]
    same = [_table("public", "orders", [_col("id", "int", 1)]),
            _table(schema, "orders", [_col("id", "int", 1)])]  # fmt: skip
    c = build_step1(_inputs(engine=engine, tables=same))
    assert c.blocked_rule_count == 0 and {f.code for r in c.rules for f in r.findings} == set()
    assert all(r.status == "valid" and r.fields for r in c.rules)


def test_the_input_is_not_changed():
    inputs = _inputs()
    before = copy.deepcopy(inputs.collector)
    build_step1(inputs)
    assert inputs.collector == before


def test_a_wave_entry_spelled_differently_resolves_like_the_plan_does():
    c = build_step1(_inputs(wave_tables=["posts", "WP.LOG"]))
    assert {r.source_table for r in c.rules} == {"wp.posts", "wp.log"}
    assert not [f for r in c.rules for f in r.findings if f.code == "unknown_table"]


def _types(c):
    return {r.source_table: {f.target_column: f.target_type for f in r.fields} for r in c.rules}


def test_a_saved_design_of_several_schemas_gives_each_schemas_table_its_own_types():
    """#2494: a design of several schemas names each table's schema (#2455), so its
    ``sales.orders`` override reaches that table only, never ``archive.orders``."""
    tables = [_table("sales", "orders", [_col("id", "int", 1), _col("total", "int", 2)]),
              _table("archive", "orders", [_col("id", "int", 1), _col("total", "int", 2)])]  # fmt: skip
    design = [{"table_name": "orders", "schema_name": "sales",
               "columns": [{"name": "total", "aurora_type": "BIGINT"}]}]  # fmt: skip
    c = build_step1(_inputs(tables=tables, design=design))
    assert _types(c) == {"sales.orders": {"id": "INT", "total": "BIGINT"},
                         "archive.orders": {"id": "INT", "total": "INT"}}  # fmt: skip


def test_a_bare_saved_table_reaches_the_one_schema_that_has_it_and_a_shared_name_none():
    """#2494: a design of one schema names its tables bare (only that schema's tables were
    routed to Aurora). A bare name one schema of the step has is that schema's table; a
    bare name two schemas share, as in a design saved before #2455, reaches neither, so
    both keep the generator's types, stay valid and get a warning."""
    tables = [_table("a", "solo", [_col("id", "int", 1)]),
              _table("a", "users", [_col("id", "int", 1)]),
              _table("b", "users", [_col("id", "int", 1)])]  # fmt: skip
    design = [{"table_name": "solo", "columns": [{"name": "id", "aurora_type": "BIGINT"}]},
              {"table_name": "users", "columns": [{"name": "id", "aurora_type": "BIGINT"}]},
              {"table_name": "users", "columns": [{"name": "id", "aurora_type": "SMALLINT"}]}]  # fmt: skip
    inputs = _inputs(tables=tables, design=design)
    c = build_step1(inputs)
    assert c.blocked_rule_count == 0
    assert _types(c) == {"a.solo": {"id": "BIGINT"}, "a.users": {"id": "INT"},
                         "b.users": {"id": "INT"}}  # fmt: skip
    # #2525 review: both tables say their saved types were not applied, as warnings that
    # survive validation (once each) and block nothing; the table one schema has does not.
    from src.tools.transformation_contract.validate import validate_step1

    for contract in (
        c,
        validate_step1(c, inputs),
        validate_step1(validate_step1(c, inputs), inputs),
    ):
        codes = {r.source_table: [f.code for f in r.findings if f.code.startswith("saved_")]
                 for r in contract.rules}  # fmt: skip
        assert codes["a.users"] == codes["b.users"] == ["saved_design_types_not_applied"]
        assert codes["a.solo"] == [] and contract.blocked_rule_count == 0
    users = next(r for r in c.rules if r.source_table == "b.users")
    assert "names users with no schema" in " ".join(f.message for f in users.findings)


def test_each_schemas_table_keeps_its_own_schemas_enum_type():
    """#2494: two schemas' ``orders`` each keep the enum of their own schema (#1834), keyed
    by table id: neither takes the other's type."""
    tables = [_table("sales", "orders", [_col("id", "int", 1), _col("state", "state_t", 2)]),
              _table("archive", "orders", [_col("id", "int", 1), _col("state", "state_t", 2)])]  # fmt: skip
    inputs = _inputs(engine="postgresql", tables=tables)
    inputs.collector["database_schema"]["enum_types"] = [
        {"schema_name": "sales", "type_name": "state_t", "labels": ["new", "paid"]},
        {"schema_name": "archive", "type_name": "state_t", "labels": ["old"]},
    ]
    c = build_step1(inputs)
    assert {t: f["state"] for t, f in _types(c).items()} == {
        "sales.orders": '"sales"."state_t"', "archive.orders": '"archive"."state_t"'}  # fmt: skip
