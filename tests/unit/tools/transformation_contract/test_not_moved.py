# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's contract flags a table DMS cannot move as not moved (#1918).

The DMS guide lists TSVECTOR as a PostgreSQL source type it does not migrate, and in a live
run the source read truncated long tsvector values and the load failed. The contract keeps
the rule (the DDL still creates the table, so the application works after cutover) with a
warning finding and ``moves_rows`` false, instead of a rule that fails the task.
"""

from __future__ import annotations

import pytest

from src.contracts.transformation_contract import NOT_MOVED, Finding, Rule
from src.shared.catalog.dms_sizing import dms_sizing
from src.tools.transformation_contract.step1 import build_step1
from src.tools.transformation_contract.validate import validate_step1

from .conftest import col, step1_inputs, table

WHY = (
    "DMS does not migrate tsvector; rebuilding it is yours (e.g. the application's search reindex)"
)


def _pg_tables(search_type: str = "tsvector"):
    return [
        table("public", "posts", [col("id", "int4", 1), col("raw", "text", 2)], pk=["id"]),
        table(
            "public",
            "post_search_data",
            [
                col("post_id", "int4", 1),
                col("search_data", search_type, 2),
                col("raw_data", "text", 3),
            ],
            pk=["post_id"],
        ),
    ]


def _validated(engine: str = "postgresql", tables=None):
    inputs = step1_inputs(engine=engine, tables=tables if tables is not None else _pg_tables())
    return validate_step1(build_step1(inputs), inputs)


def _rule(contract, table_id):
    return next(r for r in contract.rules if r.source_table == table_id)


def test_the_catalog_reason_is_the_maintainers_wording():
    assert dms_sizing().not_moved.why == WHY
    assert dms_sizing().not_moved.postgresql == ("tsvector",)


def test_a_postgresql_table_with_a_tsvector_column_is_flagged_not_moved():
    c = _validated()
    rule = _rule(c, "public.post_search_data")
    assert rule.moves_rows is False and rule.status == "valid"
    [finding] = [f for f in rule.findings if f.code == NOT_MOVED]
    assert finding.severity == "warning" and finding.column == "search_data"
    assert finding.message.startswith(WHY)
    assert "step 1 creates post_search_data and does not move its rows" in finding.message
    # The step stays valid: a not-moved table is a known exception, not a blocked rule.
    assert c.status == "valid" and c.blocked_rule_count == 0


def test_a_tsvector_array_is_flagged_too():
    rule = _rule(_validated(tables=_pg_tables("_tsvector")), "public.post_search_data")
    assert rule.moves_rows is False


def test_a_table_without_tsvector_still_moves():
    rule = _rule(_validated(), "public.posts")
    assert rule.moves_rows is True and not any(f.code == NOT_MOVED for f in rule.findings)


@pytest.mark.parametrize("engine", ["mysql", "mariadb"])
def test_a_mysql_family_source_never_gets_the_flag(engine):
    # A type named tsvector on a MySQL-family source (it has none) is not judged by the
    # PostgreSQL list: MySQL is unchanged.
    c = _validated(
        engine=engine,
        tables=[table("wp", "t", [col("id", "int", 1), col("s", "tsvector", 2)], pk=["id"])],
    )
    assert all(r.moves_rows for r in c.rules)


def test_a_rule_that_moves_no_rows_must_say_why():
    with pytest.raises(ValueError, match="not_moved finding"):
        Rule(input_schema="collector", source_table="public.t", target_table="t", moves_rows=False)
    finding = Finding(code=NOT_MOVED, severity="warning", message=WHY, table_id="public.t")
    rule = Rule(input_schema="collector", source_table="public.t", target_table="t",
                moves_rows=False, findings=[finding])  # fmt: skip
    assert rule.moves_rows is False


def test_an_older_contract_without_the_field_moves_every_rule():
    rule = Rule.model_validate(
        {"input_schema": "collector", "source_table": "public.t", "target_table": "t"}
    )
    assert rule.moves_rows is True


def test_a_rule_that_moves_its_rows_keeps_its_bytes_and_hash():
    # Plans pin the contract's SHA-256: a contract without a not-moved rule must hash as
    # it did before the field existed.
    rule = Rule(input_schema="collector", source_table="public.t", target_table="t")
    assert "moves_rows" not in rule.model_dump(mode="json")
    finding = Finding(code=NOT_MOVED, severity="warning", message=WHY, table_id="public.t")
    flagged = Rule(input_schema="collector", source_table="public.t", target_table="t",
                   moves_rows=False, findings=[finding])  # fmt: skip
    assert flagged.model_dump(mode="json")["moves_rows"] is False
    assert Rule.model_validate(flagged.model_dump(mode="json")).moves_rows is False
