"""Merging a model-written delta into the deterministic Aurora draft (issue #273)."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from src.contracts.aurora_mysql_model_output import AuroraMySQLModelOutputContract
from src.contracts.aurora_postgresql_model_output import AuroraPostgresqlModelOutputContract
from src.contracts.schema_design_input import AgentTable
from src.tools.schema.aurora_common.delta_merge import (
    AuroraDesignBase,
    merge_design_delta,
)
from src.tools.schema.aurora_common.draft_builder import build_mysql_draft, build_pg_draft

_TABLES = [
    {
        "table_id": "shop.users",
        "table_name": "users",
        "row_count": 1000,
        "primary_key": ["id"],
        "columns": [
            {"column_name": "id", "normalized_data_type": "integer", "nullable": False},
            # residual: string with no max length
            {"column_name": "email", "normalized_data_type": "string", "nullable": False},
            # residual: not normalized (raw data_type "integer")
            {"column_name": "score", "nullable": True},
        ],
        "indexes": [
            {"index_name": "idx_users_email", "columns": ["email"], "is_unique": True},
        ],
    },
    {
        "table_id": "shop.orders",
        "table_name": "orders",
        "row_count": 5000,
        "primary_key": ["id"],
        "columns": [
            {"column_name": "id", "normalized_data_type": "integer", "nullable": False},
            {"column_name": "user_id", "normalized_data_type": "integer", "nullable": False},
            # residual: not normalized (raw data_type "bigint")
            {"column_name": "total_cents", "nullable": False},
        ],
        "indexes": [
            {"index_name": "idx_orders_user", "columns": ["user_id"], "is_unique": False},
        ],
        "foreign_keys": [
            {
                "constraint_name": "fk_orders_user",
                "columns": ["user_id"],
                "referenced_table": "users",
                "referenced_columns": ["id"],
            }
        ],
    },
]

_RAW_TYPES = {
    ("users", "id"): "integer",
    ("users", "email"): "character varying",
    ("users", "score"): "integer",
    ("orders", "id"): "integer",
    ("orders", "user_id"): "integer",
    ("orders", "total_cents"): "bigint",
}

_CONTRACTS: dict[str, type[BaseModel]] = {
    "aurora_postgresql": AuroraPostgresqlModelOutputContract,
    "aurora_mysql": AuroraMySQLModelOutputContract,
}
_DRAFTS = {"aurora_postgresql": build_pg_draft, "aurora_mysql": build_mysql_draft}


def _base(engine: str = "aurora_postgresql", source_engine: str = "postgresql"):
    return AuroraDesignBase(
        engine=engine,
        job_id="job-1",
        source_database="shop",
        source_engine=source_engine,
        tables=[AgentTable.model_validate(t) for t in _TABLES],
        source_data_types=dict(_RAW_TYPES),
    )


def _delta(**fields) -> dict:
    return {"delta_version": "1.0", **fields}


def _table(output: dict, name: str) -> dict:
    return next(t for t in output["table_definitions"] if t["table_name"] == name)


def _column(output: dict, table: str, column: str) -> dict:
    return next(c for c in _table(output, table)["columns"] if c["name"] == column)


def _ok(delta: dict, engine: str = "aurora_postgresql") -> dict:
    result = merge_design_delta(_base(engine), delta)
    assert result.errors == []
    assert result.output is not None
    _CONTRACTS[engine].model_validate(result.output)
    return result.output


# ---------------------------------------------------------------------------
# Empty delta = draft
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("engine", sorted(_CONTRACTS))
def test_empty_delta_reproduces_the_draft(engine):
    base = _base(engine)
    draft, strategy = _DRAFTS[engine](base.tables, base.source_engine)

    output = _ok(_delta(), engine)

    assert output["migration_strategy"] == strategy
    assert output["generated_ddl"] == draft["full_ddl"]
    assert output["target_engine"] == engine
    assert output["job_id"] == "job-1" and output["source_database"] == "shop"
    for out_t, draft_t in zip(output["table_definitions"], draft["tables"], strict=True):
        assert out_t["table_name"] == draft_t["table_name"]
        assert out_t["primary_key"] == draft_t["primary_key"]
        assert out_t["indexes"] == draft_t["indexes"]
        assert out_t["foreign_keys"] == draft_t["foreign_keys"]
        for out_c, draft_c in zip(out_t["columns"], draft_t["columns"], strict=True):
            for key in ("name", "aurora_type", "source_type", "script_derived", "needs_judgment"):
                assert out_c[key] == draft_c[key]
    # Contract needs at least one trade-off; an empty delta gets a deterministic one.
    assert len(output["trade_offs"]) == 1
    assert output["validation_passed"] is True


def test_delta_trade_offs_replace_the_default():
    output = _ok(_delta(trade_offs=[{"description": "Keep 1:1", "impact": "Low risk"}]))
    assert [t["description"] for t in output["trade_offs"]] == ["Keep 1:1"]


# ---------------------------------------------------------------------------
# Indexes (structured, rendered by the generator)
# ---------------------------------------------------------------------------


def test_add_structured_index():
    output = _ok(
        _delta(
            tables=[
                {
                    "table_name": "orders",
                    "add_indexes": [{"index_name": "idx_orders_total", "columns": ["total_cents"]}],
                }
            ]
        )
    )

    stmt = 'CREATE INDEX "idx_orders_total" ON "orders" ("total_cents");'
    assert _table(output, "orders")["indexes"][-1] == stmt
    assert stmt in output["generated_ddl"]
    assert 'CREATE INDEX "idx_orders_user"' in output["generated_ddl"]  # draft index kept


def test_add_postgres_index_with_method_include_and_where():
    output = _ok(
        _delta(
            tables=[
                {
                    "table_name": "orders",
                    "add_indexes": [
                        {
                            "index_name": "idx_orders_big",
                            "columns": ["USER_ID"],
                            "unique": True,
                            "method": "btree",
                            "include": ["total_cents"],
                            "where": "total_cents > 1000 AND user_id IS NOT NULL",
                        }
                    ],
                }
            ]
        )
    )
    assert _table(output, "orders")["indexes"][-1] == (
        'CREATE UNIQUE INDEX "idx_orders_big" ON "orders" USING btree ("user_id") '
        'INCLUDE ("total_cents") WHERE "total_cents" > 1000 AND "user_id" IS NOT NULL;'
    )


def test_legacy_string_index_is_parsed_and_re_rendered():
    output = _ok(
        _delta(
            tables=[
                {
                    "table_name": "orders",
                    "add_indexes": ["create index idx_orders_total on shop.orders (total_cents)"],
                }
            ]
        )
    )
    assert _table(output, "orders")["indexes"][-1] == (
        'CREATE INDEX "idx_orders_total" ON "orders" ("total_cents");'
    )


def test_modify_index_by_name():
    output = _ok(
        _delta(
            tables=[
                {
                    "table_name": "orders",
                    "modify_indexes": [
                        {"index_name": "idx_orders_user", "columns": ["user_id", "id"]}
                    ],
                }
            ]
        )
    )

    new = 'CREATE INDEX "idx_orders_user" ON "orders" ("user_id", "id");'
    assert _table(output, "orders")["indexes"] == [new]
    assert new in output["generated_ddl"]
    assert '("user_id");' not in output["generated_ddl"]


def test_remove_index():
    output = _ok(_delta(tables=[{"table_name": "orders", "remove_indexes": ["idx_orders_user"]}]))

    assert _table(output, "orders")["indexes"] == []
    assert "idx_orders_user" not in output["generated_ddl"]
    assert "fk_orders_user" in output["generated_ddl"]  # foreign keys untouched


def test_index_changes_are_counted():
    result = merge_design_delta(
        _base(),
        _delta(
            tables=[
                {
                    "table_name": "orders",
                    "remove_indexes": ["idx_orders_user"],
                    "add_indexes": [{"index_name": "i2", "columns": ["id"]}],
                }
            ]
        ),
    )
    assert result.summary["indexes_added"] == 1
    assert result.summary["indexes_removed"] == 1
    assert result.summary["tables_changed"] == 1


def test_mysql_index_is_rendered_with_backticks():
    output = _ok(
        _delta(
            tables=[
                {
                    "table_name": "orders",
                    "add_indexes": [
                        {"index_name": "idx_orders_total", "columns": ["total_cents"]},
                        "CREATE UNIQUE INDEX `idx_orders_u` ON `orders` (`user_id`, `id`);",
                    ],
                }
            ]
        ),
        "aurora_mysql",
    )
    assert _table(output, "orders")["indexes"][-2:] == [
        "CREATE INDEX `idx_orders_total` ON `orders` (`total_cents`);",
        "CREATE UNIQUE INDEX `idx_orders_u` ON `orders` (`user_id`, `id`);",
    ]


_PG_INVALID = [
    ({"remove_indexes": ["idx_missing"]}, "has no index 'idx_missing'"),
    (
        {"modify_indexes": [{"index_name": "idx_missing", "columns": ["id"]}]},
        "has no index 'idx_missing'",
    ),
    ({"add_indexes": [{"index_name": "idx_orders_user", "columns": ["id"]}]}, "already exists"),
    ({"add_indexes": [{"index_name": "i", "columns": ["nope"]}]}, "has no column 'nope'"),
    ({"add_indexes": [{"index_name": "i", "columns": ["id"], "include": ["x"]}]}, "no column 'x'"),
    ({"add_indexes": ['CREATE INDEX "i" ON "users" ("id")']}, "is not ON table 'orders'"),
    ({"add_indexes": ["ALTER TABLE orders ADD COLUMN x INT"]}, "not a plain"),
    ({"add_indexes": ["CREATE INDEX i ON other_schema.orders (id)"]}, "schema prefix"),
    ({"add_indexes": ["CREATE INDEX `i` ON `orders` (`id`)"]}, "backticks"),
    ({"add_indexes": ["CREATE INDEX i ON orders USING rtree (id)"]}, "unknown index method"),
    ({"add_indexes": ["CREATE INDEX i ON orders (lower(id))"]}, "not a plain"),
    # C2 attack strings: a second statement, a psql meta-command, a comment
    (
        {"add_indexes": ['CREATE INDEX "a" ON "orders" ("id");\nDROP TABLE "users";']},
        "one line",
    ),
    ({"add_indexes": ['CREATE INDEX "a" ON "orders" ("id")\n\\! touch /tmp/x']}, "one line"),
    ({"add_indexes": ['CREATE INDEX "a" ON "orders" ("id"); DROP TABLE "users"']}, "one line"),
    ({"add_indexes": ['CREATE INDEX "a" ON "orders" ("id") -- x']}, "one line"),
    ({"add_indexes": ['CREATE INDEX "a" ON "orders" ("id") WHERE 1=1']}, "not a plain"),
    (
        {"add_indexes": [{"index_name": "a", "columns": ["id"], "where": "id = 1; DROP TABLE x"}]},
        "where must not contain",
    ),
    (
        {"add_indexes": [{"index_name": "a", "columns": ["id"], "where": "pg_sleep(10) IS NULL"}]},
        "'pg_sleep' is not a column",
    ),
    (
        {"add_indexes": [{"index_name": "a", "columns": ["id"], "where": "id = 1)"}]},
        "unbalanced",
    ),
    (
        {"add_indexes": [{"index_name": 'a"; DROP TABLE x; --', "columns": ["id"]}]},
        "plain identifier",
    ),
]


@pytest.mark.parametrize(("change", "fragment"), _PG_INVALID)
def test_invalid_index_changes_fail(change, fragment):
    result = merge_design_delta(_base(), _delta(tables=[{"table_name": "orders", **change}]))

    assert result.output is None
    assert any(fragment in e for e in result.errors), result.errors


@pytest.mark.parametrize(
    ("entry", "fragment"),
    [
        ("CREATE INDEX CONCURRENTLY i ON orders (id)", "CONCURRENTLY"),
        ('CREATE INDEX "i" ON "orders" ("id")', "backticks, not"),
        ("CREATE INDEX i ON orders USING btree (id)", "USING"),
        ({"index_name": "i", "columns": ["id"], "method": "gin"}, "PostgreSQL only"),
        ({"index_name": "i", "columns": ["id"], "where": "id > 1"}, "PostgreSQL only"),
    ],
)
def test_mysql_rejects_postgres_index_syntax(entry, fragment):
    result = merge_design_delta(
        _base("aurora_mysql", "mysql"),
        _delta(tables=[{"table_name": "orders", "add_indexes": [entry]}]),
    )
    assert result.output is None
    assert any(fragment in e for e in result.errors), result.errors


# ---------------------------------------------------------------------------
# Tables and columns
# ---------------------------------------------------------------------------


def test_unknown_table_fails_validation():
    result = merge_design_delta(
        _base(), _delta(tables=[{"table_name": "invoices", "add_indexes": []}])
    )

    assert result.output is None
    assert any("unknown table 'invoices'" in e for e in result.errors)


def test_table_listed_twice_fails():
    result = merge_design_delta(
        _base(), _delta(tables=[{"table_name": "orders"}, {"table_name": "ORDERS"}])
    )
    assert result.output is None
    assert any("more than once" in e for e in result.errors)


def test_schema_qualified_and_quoted_table_names_match():
    output = _ok(
        _delta(
            tables=[
                {
                    "table_name": 'shop."orders"',
                    "column_types": [{"column": "total_cents", "aurora_type": "BIGINT"}],
                }
            ]
        )
    )
    assert _column(output, "orders", "total_cents")["aurora_type"] == "BIGINT"


def test_unknown_column_fails_validation():
    result = merge_design_delta(
        _base(),
        _delta(
            tables=[
                {"table_name": "orders", "column_types": [{"column": "nope", "aurora_type": "INT"}]}
            ]
        ),
    )
    assert result.output is None
    assert any("has no column 'nope'" in e for e in result.errors)


def test_column_type_override_regenerates_ddl_and_provenance():
    output = _ok(
        _delta(
            tables=[
                {
                    "table_name": "users",
                    "column_types": [{"column": "email", "aurora_type": "VARCHAR(320)"}],
                }
            ]
        )
    )

    email = _column(output, "users", "email")
    assert email["aurora_type"] == "VARCHAR(320)"
    assert email["script_derived"] is False and email["needs_judgment"] is True
    assert '"email" VARCHAR(320) NOT NULL' in output["generated_ddl"]
    # Untouched script-derived column keeps its provenance.
    user_id = _column(output, "orders", "user_id")
    assert user_id["script_derived"] is True and user_id["needs_judgment"] is False


# ---------------------------------------------------------------------------
# Residual rules by source data type
# ---------------------------------------------------------------------------


def test_type_rules_resolve_residuals_by_source_data_type():
    result = merge_design_delta(
        _base(),
        _delta(
            type_rules=[
                {"source_data_type": "INTEGER", "aurora_type": "INTEGER"},
                {"source_data_type": "bigint", "aurora_type": "BIGINT"},
            ]
        ),
    )
    output = result.output
    assert output is not None
    assert _column(output, "users", "score")["aurora_type"] == "INTEGER"
    assert _column(output, "orders", "total_cents")["aurora_type"] == "BIGINT"
    # A rule never touches a script-derived column with the same source type.
    assert _column(output, "users", "id")["script_derived"] is True
    assert result.summary["residuals_resolved_by_rule"] == 2
    assert result.summary["residuals_unresolved"] == 1  # users.email (character varying)


def test_column_override_wins_over_type_rule():
    output = _ok(
        _delta(
            type_rules=[{"source_data_type": "integer", "aurora_type": "INTEGER"}],
            tables=[
                {
                    "table_name": "users",
                    "column_types": [{"column": "score", "aurora_type": "SMALLINT"}],
                }
            ],
        )
    )
    assert _column(output, "users", "score")["aurora_type"] == "SMALLINT"


# ---------------------------------------------------------------------------
# Contract handling
# ---------------------------------------------------------------------------


def test_invalid_delta_shape_is_a_validation_error():
    result = merge_design_delta(_base(), _delta(tables=[{"table_name": "orders", "add_index": []}]))
    assert result.output is None
    assert result.errors and result.errors[0].startswith("Invalid design delta")


def test_wrong_delta_version_is_rejected():
    result = merge_design_delta(_base(), {"delta_version": "2.0"})
    assert result.output is None


def test_lenient_merge_skips_invalid_entries_and_records_them():
    result = merge_design_delta(
        _base(),
        _delta(
            tables=[
                {"table_name": "invoices"},
                {"table_name": "orders", "remove_indexes": ["idx_orders_user"]},
            ]
        ),
        strict=False,
    )

    assert result.output is not None
    assert result.output["validation_passed"] is False
    assert any("invoices" in f for f in result.output["validation_failures"])
    assert _table(result.output, "orders")["indexes"] == []
    AuroraPostgresqlModelOutputContract.model_validate(result.output)


def test_optimizations_and_app_layer_notes_carry_through():
    output = _ok(
        _delta(
            optimizations=[
                {
                    "category": "partitioning",
                    "target": "orders",
                    "recommendation": "Range-partition by created_at",
                    "rationale": "Hot recent rows",
                }
            ],
            app_layer_notes=[
                {"feature": "trigger", "source_object": "trg_x", "recommendation": "Move to app"}
            ],
        )
    )
    assert output["optimizations"][0]["category"] == "partitioning"
    assert output["app_layer_notes"][0]["source_object"] == "trg_x"


# ---------------------------------------------------------------------------
# Types reaching DDL (C1), rule hygiene, warnings, fingerprint
# ---------------------------------------------------------------------------

_TYPE_ATTACKS = [
    "BIGINT); DROP TABLE users; --",
    "INT, `evil` TEXT",
    'INT, "evil" TEXT',
    "INT NOT NULL DEFAULT 1",
    "INT REFERENCES users",
    "TEXT\n); DROP TABLE x",
    "",
    "   ",
]


@pytest.mark.parametrize("attack", _TYPE_ATTACKS)
def test_column_type_attacks_never_reach_ddl(attack):
    for delta in (
        _delta(
            tables=[
                {
                    "table_name": "users",
                    "column_types": [{"column": "email", "aurora_type": attack}],
                }
            ]
        ),
        _delta(type_rules=[{"source_data_type": "integer", "aurora_type": attack}]),
    ):
        result = merge_design_delta(_base(), delta)
        assert result.output is None
        assert result.errors[0].startswith("Invalid design delta")


@pytest.mark.parametrize(
    ("engine", "aurora_type", "fragment"),
    [
        ("aurora_postgresql", "ENUM('a','b')", "Aurora MySQL only"),
        ("aurora_mysql", "TEXT[]", "array types are not aurora_mysql types"),
        ("aurora_postgresql", "INT UNSIGNED", "'unsigned' is not a valid aurora_postgresql"),
        ("aurora_mysql", "TIMESTAMP WITH TIME ZONE", "not a valid aurora_mysql type modifier"),
    ],
)
def test_dialect_specific_types_are_checked(engine, aurora_type, fragment):
    result = merge_design_delta(
        _base(engine),
        _delta(
            tables=[
                {
                    "table_name": "users",
                    "column_types": [{"column": "email", "aurora_type": aurora_type}],
                }
            ]
        ),
    )
    assert result.output is None
    assert any(fragment in e for e in result.errors)


def test_mysql_enum_type_is_rendered():
    output = _ok(
        _delta(
            tables=[
                {
                    "table_name": "users",
                    "column_types": [{"column": "email", "aurora_type": "ENUM('a','b')"}],
                }
            ]
        ),
        "aurora_mysql",
    )
    assert "`email` ENUM('a','b') NOT NULL" in output["generated_ddl"]


def test_types_are_normalized_before_rendering():
    output = _ok(
        _delta(
            tables=[
                {
                    "table_name": "users",
                    "column_types": [
                        {"column": "email", "aurora_type": "  character   varying(320) "}
                    ],
                }
            ]
        )
    )
    assert '"email" character varying(320) NOT NULL' in output["generated_ddl"]


def test_duplicate_type_rules_after_case_folding_fail():
    result = merge_design_delta(
        _base(),
        _delta(
            type_rules=[
                {"source_data_type": "integer", "aurora_type": "INTEGER"},
                {"source_data_type": " INTEGER ", "aurora_type": "BIGINT"},
            ]
        ),
    )
    assert result.output is None
    assert any("listed more than once" in e for e in result.errors)


def test_unmatched_type_rule_is_a_warning_not_an_error():
    result = merge_design_delta(
        _base(), _delta(type_rules=[{"source_data_type": "money", "aurora_type": "NUMERIC(19,4)"}])
    )
    assert result.errors == []
    assert result.output is not None
    assert any("'money' matched no residual" in w for w in result.warnings)


def test_unresolved_residuals_are_a_warning():
    result = merge_design_delta(_base(), _delta())
    assert result.output is not None and result.errors == []
    assert result.summary["residuals_unresolved"] == 3
    assert any("3 residual column(s) keep the draft's fallback type" in w for w in result.warnings)


def test_fingerprint_tracks_the_draft_inputs():
    base = _base()
    same = _base()
    assert base.fingerprint() == same.fingerprint()
    same.source_data_types[("users", "score")] = "bigint"
    assert base.fingerprint() != same.fingerprint()
    other_engine = _base("aurora_mysql")
    assert base.fingerprint() != other_engine.fingerprint()


def test_full_contract_converts_to_an_equivalent_delta():
    from src.tools.schema.aurora_common.delta_merge import full_contract_to_delta

    base = _base()
    full = {
        "table_definitions": [
            {
                "table_name": "orders",
                "columns": [
                    {"name": "id", "aurora_type": "BIGINT"},  # same as draft: no change
                    {"name": "total_cents", "aurora_type": "bigint"},  # residual resolved
                ],
                "indexes": [
                    'CREATE INDEX "idx_orders_user" ON "orders" ("user_id");',  # unchanged
                    'CREATE INDEX "idx_orders_user_id" ON "orders" ("user_id", "id");',  # new
                ],
                "foreign_keys": ["ignored; DROP TABLE users"],
            },
            {
                "table_name": "users",
                "columns": [],
                # draft index redefined under the same name
                "indexes": ['CREATE INDEX "idx_users_email" ON "users" ("email", "id")'],
            },
        ],
        "trade_offs": [{"description": "d", "impact": "i"}],
    }

    delta, errors = full_contract_to_delta(base, full)

    assert errors == []
    orders, users = delta["tables"]
    assert orders["column_types"] == [{"column": "total_cents", "aurora_type": "bigint"}]
    assert [i["index_name"] for i in orders["add_indexes"]] == ["idx_orders_user_id"]
    assert orders["modify_indexes"] == []
    assert users["modify_indexes"] == [
        {"index_name": "idx_users_email", "columns": ["email", "id"], "unique": False}
    ]
    output = _ok(delta)
    assert "ignored" not in output["generated_ddl"]
    assert 'CONSTRAINT "fk_orders_user"' in output["generated_ddl"]  # from the draft
