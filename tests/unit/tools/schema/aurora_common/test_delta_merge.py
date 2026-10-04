"""Merging a model-written delta into the deterministic Aurora draft (issue #273)."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from src.contracts.aurora_mysql_model_output import AuroraMySQLModelOutputContract
from src.contracts.aurora_postgresql_model_output import AuroraPostgresqlModelOutputContract
from src.contracts.schema_design_input import AgentTable
from src.tools.schema.aurora_common.delta_merge import (
    AuroraDesignBase,
    index_name_of,
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
# Indexes
# ---------------------------------------------------------------------------


def test_add_index():
    stmt = 'CREATE INDEX "idx_orders_total" ON "orders" ("total_cents")'
    output = _ok(_delta(tables=[{"table_name": "orders", "add_indexes": [stmt]}]))

    assert _table(output, "orders")["indexes"][-1] == stmt + ";"
    assert stmt + ";" in output["generated_ddl"]
    assert 'CREATE INDEX "idx_orders_user"' in output["generated_ddl"]  # draft index kept


def test_modify_index():
    new = 'CREATE INDEX "idx_orders_user" ON "orders" ("user_id", "id");'
    output = _ok(
        _delta(
            tables=[
                {
                    "table_name": "orders",
                    "modify_indexes": [{"index_name": "idx_orders_user", "statement": new}],
                }
            ]
        )
    )

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
                    "add_indexes": ['CREATE INDEX "i2" ON "orders" ("id")'],
                }
            ]
        ),
    )
    assert result.summary["indexes_added"] == 1
    assert result.summary["indexes_removed"] == 1
    assert result.summary["tables_changed"] == 1


def test_mysql_backtick_index_is_accepted():
    stmt = "CREATE INDEX `idx_orders_total` ON `orders` (`total_cents`);"
    output = _ok(_delta(tables=[{"table_name": "orders", "add_indexes": [stmt]}]), "aurora_mysql")
    assert stmt in _table(output, "orders")["indexes"]


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"remove_indexes": ["idx_missing"]}, "has no index 'idx_missing'"),
        (
            {
                "modify_indexes": [
                    {"index_name": "idx_missing", "statement": "CREATE INDEX x ON orders (id)"}
                ]
            },
            "has no index 'idx_missing'",
        ),
        ({"add_indexes": ['CREATE INDEX "idx_orders_user" ON "orders" ("id")']}, "already exists"),
        ({"add_indexes": ['CREATE INDEX "i" ON "users" ("id")']}, "is not ON table 'orders'"),
        ({"add_indexes": ["ALTER TABLE orders ADD COLUMN x INT"]}, "not a CREATE"),
        (
            {"add_indexes": ['CREATE INDEX "a" ON "orders" ("id"); DROP TABLE "users"']},
            "one CREATE INDEX statement per entry",
        ),
    ],
)
def test_invalid_index_changes_fail(change, fragment):
    result = merge_design_delta(_base(), _delta(tables=[{"table_name": "orders", **change}]))

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


@pytest.mark.parametrize(
    ("statement", "name"),
    [
        ('CREATE UNIQUE INDEX "a b" ON "t" ("c");', "a b"),
        ("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx ON ONLY public.t (c)", "idx"),
        ("create index `m` on `t` (`c`)", "m"),
        ("DROP INDEX x", None),
    ],
)
def test_index_name_of(statement, name):
    assert index_name_of(statement) == name
