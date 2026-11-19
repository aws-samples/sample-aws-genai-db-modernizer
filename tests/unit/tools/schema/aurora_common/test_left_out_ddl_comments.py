# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#2484: the generated script names each object the Aurora design leaves out.

A table the merge does not create (two tables whose keys fold to one, a name SQL may not
quote) and a foreign key it cannot resolve each get one ``-- Not created: <name>:
<reason>`` comment after the script's last statement, written from the design's own
notes. The saved-design check accepts exactly those comments, and a design that leaves
nothing out keeps the script it had, byte for byte.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.contracts.schema_design_input import AgentColumn, AgentForeignKey, AgentIndex, AgentTable
from src.shared.contract_versioning import ContractVersionError
from src.storage.local_store import LocalArtifactStore
from src.tools.schema.aurora_common.ddl_generator import assemble_full_ddl
from src.tools.schema.aurora_common.delta_merge import AuroraDesignBase, merge_design_delta

DB, JOB = "shop", "job1"
REASON = (
    "its schema and name match another table of the design once case and quote marks are "
    "ignored, so neither is designed; rename one, or design them by hand."
)


def _table(name: str, schema: str | None, fks: list[AgentForeignKey] | None = None) -> AgentTable:
    return AgentTable(
        table_id=f"{schema}.{name}" if schema else name,
        table_name=name,
        schema_name=schema,
        row_count=1,
        columns=[
            AgentColumn(column_name="id", data_type="int", nullable=False),
            AgentColumn(column_name="note", data_type="text", nullable=True),
        ],
        indexes=[AgentIndex(index_name="by_note", columns=["note"], is_unique=False)],
        primary_key=["id"],
        foreign_keys=fks,
    )


def _base(engine: str, tables: list[AgentTable]) -> AuroraDesignBase:
    return AuroraDesignBase(
        engine=engine, job_id=JOB, source_database=DB, source_engine="postgresql", tables=tables
    )


def _merged(engine: str, tables: list[AgentTable]) -> tuple[dict, AuroraDesignBase]:
    base = _base(engine, tables)
    result = merge_design_delta(base, {"delta_version": "1.0"})
    assert result.errors == [] and result.output is not None
    return result.output, base


def _colliding() -> list[AgentTable]:
    return [_table("orders", "Sales"), _table("orders", "sales"), _table("items", "crm")]


def _read_back(tmp_path: Path, engine: str, design: dict) -> dict:
    rel = f"{DB}/{JOB}/schema-{engine}/v1/schema_output.json"
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(design), encoding="utf-8")
    return LocalArtifactStore(str(tmp_path)).read_json(rel)


@pytest.mark.parametrize("engine", ["aurora_postgresql", "aurora_mysql"])
def test_each_colliding_table_gets_one_comment_at_the_end_of_the_script(engine: str) -> None:
    design, base = _merged(engine, _colliding())
    lines = design["generated_ddl"].splitlines()
    assert lines[-2:] == [
        f"-- Not created: Sales.orders: {REASON}",
        f"-- Not created: sales.orders: {REASON}",
    ]
    # The statements before them are the script the design always had.
    assert design["generated_ddl"].startswith(assemble_full_ddl(base.generate().tables) + "\n\n--")
    assert design["generated_ddl"].count("CREATE TABLE ") == 1


def test_a_colliding_table_note_names_the_table_once() -> None:
    design, _ = _merged("aurora_postgresql", _colliding())
    notes = [n for n in design["app_layer_notes"] if n["feature"] == "colliding_table_name"]
    assert [n["source_object"] for n in notes] == ["Sales.orders", "sales.orders"]


def test_an_ambiguous_foreign_key_gets_its_comment() -> None:
    fk = AgentForeignKey(
        constraint_name="fk_customer", columns=["id"], referenced_table="customers",
        referenced_columns=["id"],
    )  # fmt: skip
    tables = [_table("lines", "sales", [fk]), _table("customers", "crm"),
              _table("customers", "archive")]  # fmt: skip
    design, _ = _merged("aurora_postgresql", tables)
    last = design["generated_ddl"].splitlines()[-1]
    assert last.startswith("-- Not created: sales.lines.fk_customer: its referenced table ")
    assert "(archive, crm)" in last


def test_a_name_with_a_newline_stays_inside_its_comment() -> None:
    """A table whose schema holds a line break cannot be quoted, so it is left out; its
    comment is one line, and nothing of the name reaches the script as SQL."""
    hostile = "x\nDROP TABLE users; --"
    design, _ = _merged("aurora_postgresql", [_table("orders", hostile), _table("items", "crm")])
    script = design["generated_ddl"]
    drops = [line for line in script.splitlines() if "DROP" in line]
    assert drops == ["-- Not created: x DROP TABLE users; --.orders: " + drops[0].split(": ", 2)[2]]
    assert script.splitlines()[-1] == drops[0]


@pytest.mark.parametrize("engine", ["aurora_postgresql", "aurora_mysql"])
def test_a_design_with_its_comments_loads_unchanged(tmp_path: Path, engine: str) -> None:
    design, _ = _merged(engine, _colliding())
    assert design["generated_ddl"].endswith(f"-- Not created: sales.orders: {REASON}")
    assert _read_back(tmp_path, engine, design) == design


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(
            lambda s: s + "\nDROP TABLE crm.items;",
            id="statement",
            marks=pytest.mark.passes_on_main,
        ),
        pytest.param(
            lambda s: s.replace("-- Not created: Sales.orders:", "-- Not created: other:"),
            id="renamed",
        ),
        pytest.param(
            lambda s: s + "\n-- Not created: extra: why.",
            id="extra",
            marks=pytest.mark.passes_on_main,
        ),
    ],
)
def test_an_edited_comment_block_is_refused(tmp_path: Path, edit) -> None:
    design, _ = _merged("aurora_postgresql", _colliding())
    design["generated_ddl"] = edit(design["generated_ddl"])
    with pytest.raises(ContractVersionError):
        _read_back(tmp_path, "aurora_postgresql", design)


@pytest.mark.passes_on_main
def test_a_design_saved_before_2484_with_no_comment_still_loads(tmp_path: Path) -> None:
    design, base = _merged("aurora_postgresql", _colliding())
    design["generated_ddl"] = assemble_full_ddl(base.generate().tables)
    assert _read_back(tmp_path, "aurora_postgresql", design) == design


@pytest.mark.passes_on_main
@pytest.mark.parametrize("engine", ["aurora_postgresql", "aurora_mysql"])
def test_a_one_schema_design_with_nothing_left_out_keeps_its_script(engine: str) -> None:
    design, base = _merged(engine, [_table("orders", None), _table("items", None)])
    assert design["generated_ddl"] == assemble_full_ddl(base.generate().tables)
    assert "-- Not created" not in design["generated_ddl"]
