# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The schema design resolves a foreign key to the schema the collection reports (#2531).

``AgentForeignKey.referenced_schema`` comes from the collection scripts. With it, the key
references that schema's table; without it (a collection written before #2531) the
bare-name rule of #2455 and #2479 applies, as ``test_schema_qualified_tables.py`` checks.
"""

from __future__ import annotations

import pytest

from src.contracts.collector_output import ForeignKey
from src.contracts.schema_design_input import AgentForeignKey
from src.tools.schema.aurora_common.delta_merge import AuroraDesignBase, merge_design_delta
from tests.unit.tools.schema.aurora_common.test_schema_qualified_tables import _merged, _table


def _fk(referenced_schema: str | None) -> AgentForeignKey:
    return AgentForeignKey(constraint_name="fk_c", columns=["id"], referenced_table="customers",
                           referenced_schema=referenced_schema, referenced_columns=["id"])  # fmt: skip


def _notes(design: dict) -> list[dict]:
    return [n for n in design["app_layer_notes"] if n["source_object"].endswith(".fk_c")]


def test_a_key_references_the_schema_the_collection_reports() -> None:
    """Both ``sales`` (the key's own) and ``crm`` have ``customers``: the bare-name rule
    takes ``sales``; the collection says ``crm``."""
    tables = [_table("orders", "sales", fks=[_fk("crm")]), _table("customers", "sales"),
              _table("customers", "crm")]  # fmt: skip
    orders = _merged("aurora_postgresql", tables)["table_definitions"][0]
    assert orders["foreign_keys"] == [
        'ALTER TABLE "sales"."orders" ADD CONSTRAINT "fk_c" FOREIGN KEY ("id") '
        'REFERENCES "crm"."customers" ("id");'
    ]


def test_a_key_two_other_schemas_could_reference_is_kept_with_its_schema() -> None:
    tables = [_table("orders", "sales", fks=[_fk("archive")]), _table("customers", "crm"),
              _table("customers", "archive")]  # fmt: skip
    design = _merged("aurora_postgresql", tables)
    assert design["table_definitions"][0]["foreign_keys"] == [
        'ALTER TABLE "sales"."orders" ADD CONSTRAINT "fk_c" FOREIGN KEY ("id") '
        'REFERENCES "archive"."customers" ("id");'
    ]
    assert _notes(design) == []


def test_a_key_onto_a_schema_the_design_lacks_is_left_out_with_a_note() -> None:
    tables = [_table("orders", "sales", fks=[_fk("billing")]), _table("customers", "sales"),
              _table("customers", "crm")]  # fmt: skip
    design = _merged("aurora_postgresql", tables)
    assert design["table_definitions"][0]["foreign_keys"] == []
    [note] = _notes(design)
    assert note["feature"] == "ambiguous_reference"
    assert "customers in schema billing is not designed" in note["recommendation"]


@pytest.mark.parametrize("engine", ["aurora_postgresql", "aurora_mysql"])
def test_a_one_schema_design_leaves_out_a_key_onto_another_schema(engine) -> None:
    """A design of one schema holds no table of ``billing``: the key would point at
    ``sales.customers``, which only shares the name."""
    tables = [_table("orders", "sales", fks=[_fk("billing")]), _table("customers", "sales")]
    design = _merged(engine, tables)
    orders = next(t for t in design["table_definitions"] if t["table_name"] == "orders")
    assert orders["foreign_keys"] == []
    assert "FOREIGN KEY" not in design["generated_ddl"]
    [note] = _notes(design)
    assert note["feature"] == "ambiguous_reference"


@pytest.mark.passes_on_main  # the key's own schema reported: the design as before
@pytest.mark.parametrize("schema", ["sales", None])
def test_a_one_schema_design_keeps_a_key_onto_its_own_schema(schema) -> None:
    tables = [_table("orders", "sales", fks=[_fk(schema)]), _table("customers", "sales")]
    orders = _merged("aurora_postgresql", tables)["table_definitions"][0]
    assert orders["foreign_keys"] == [
        'ALTER TABLE "orders" ADD CONSTRAINT "fk_c" FOREIGN KEY ("id") '
        'REFERENCES "customers" ("id");'
    ]


def test_the_design_input_carries_the_schema_only_when_reported() -> None:
    """The design input of a collection written before #2531 keeps its bytes."""
    assert "referenced_schema" not in _fk(None).model_dump()
    assert _fk("crm").model_dump()["referenced_schema"] == "crm"
    old = ForeignKey(constraint_name="fk_c", columns=["id"], referenced_table="customers",
                     referenced_columns=["id"])  # fmt: skip
    assert "referenced_schema" not in old.model_dump(mode="json")


def _merged_from(source_engine: str, engine: str, tables) -> dict:
    base = AuroraDesignBase(engine=engine, job_id="job1", source_database="shop",
                            source_engine=source_engine, tables=tables)  # fmt: skip
    result = merge_design_delta(base, {"delta_version": "1.0"})
    assert result.errors == [] and result.output is not None
    return result.output


@pytest.mark.passes_on_main  # main's bare-name rule keeps it; guards the #2531 review bug
@pytest.mark.parametrize("several", [False, True], ids=["one-database", "two-databases"])
def test_a_mysql_key_whose_database_differs_only_by_case_is_kept(several) -> None:
    """#2531 review repro: lower_case_table_names=2 reports the table's database as stored
    (``casedb``) and the key's as the DDL spelled it (``CASEDB``). One database: kept."""
    tables = [_table("orders", "casedb", fks=[_fk("CASEDB")]), _table("customers", "casedb")]
    if several:
        tables.append(_table("customers", "archive"))
    design = _merged_from("mysql", "aurora_mysql", tables)
    orders = next(t for t in design["table_definitions"] if t["table_name"] == "orders")
    target = "`casedb`.`customers`" if several else "`customers`"
    assert orders["foreign_keys"] == [
        f"ALTER TABLE {'`casedb`.`orders`' if several else '`orders`'} ADD CONSTRAINT `fk_c` "
        f"FOREIGN KEY (`id`) REFERENCES {target} (`id`);"
    ]
    assert _notes(design) == []


@pytest.mark.passes_on_main  # main's bare-name rule keeps it; guards the #2531 review bug
def test_a_mysql_key_takes_the_spelling_of_another_database_of_the_design() -> None:
    tables = [_table("orders", "sales", fks=[_fk("ARCHIVE")]), _table("customers", "archive")]
    orders = _merged_from("mysql", "aurora_mysql", tables)["table_definitions"][0]
    assert orders["foreign_keys"] == [
        "ALTER TABLE `sales`.`orders` ADD CONSTRAINT `fk_c` FOREIGN KEY (`id`) "
        "REFERENCES `archive`.`customers` (`id`);"
    ]


def test_a_postgresql_key_onto_a_schema_of_another_case_is_left_out() -> None:
    """PostgreSQL compares schema names as spelled: ``SALES`` is not ``sales``."""
    tables = [_table("orders", "sales", fks=[_fk("SALES")]), _table("customers", "sales")]
    design = _merged_from("postgresql", "aurora_postgresql", tables)
    assert design["table_definitions"][0]["foreign_keys"] == []
    [note] = _notes(design)
    assert "would point at sales's own table of that name" in note["recommendation"]
