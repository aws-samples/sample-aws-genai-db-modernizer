# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#2455: two schemas' tables of one name are two tables in the Aurora design.

A design whose tables come from more than one source schema creates each table in its
schema (``"sales"."orders"``), opens the script with one ``CREATE SCHEMA IF NOT EXISTS``
per schema, stores each table's own statements and names the table ``schema.table`` in
its keys, notes and design view. A design of one schema reads exactly as before.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from src.contracts.aurora_postgresql_model_output import AuroraPostgresqlModelOutputContract
from src.contracts.schema_design_input import (
    AgentCollectorInput,
    AgentColumn,
    AgentForeignKey,
    AgentIndex,
    AgentQueriesInput,
    AgentQueryPattern,
    AgentTable,
    QueryType,
)
from src.storage.local_store import LocalArtifactStore
from src.tools.schema.aurora_common.access_patterns import derive_access_patterns
from src.tools.schema.aurora_common.delta_merge import (
    AuroraDesignBase,
    full_contract_to_delta,
    merge_design_delta,
)

REPO = Path(__file__).resolve().parents[5]
LEGACY = REPO / "tests/fixtures/aurora_postgresql_two_schemas_pre_2455.json"
DB, JOB = "shop", "job1"


def _table(
    name: str,
    schema: str | None,
    *,
    fks: list[AgentForeignKey] | None = None,
    index: str = "by_note",
) -> AgentTable:
    return AgentTable(
        table_id=f"{schema}.{name}" if schema else name,
        table_name=name,
        schema_name=schema,
        row_count=1,
        columns=[
            AgentColumn(column_name="id", data_type="int", nullable=False),
            AgentColumn(column_name="note", data_type="text", nullable=True),
        ],
        indexes=[AgentIndex(index_name=index, columns=["note"], is_unique=False)],
        primary_key=["id"],
        foreign_keys=fks,
    )


def _base(engine: str, tables: list[AgentTable], **kw) -> AuroraDesignBase:
    return AuroraDesignBase(
        engine=engine,
        job_id=JOB,
        source_database=DB,
        source_engine="postgresql",
        tables=tables,
        **kw,
    )


def _two_orders() -> list[AgentTable]:
    return [_table("orders", "sales"), _table("orders", "archive")]


def _merged(engine: str, tables: list[AgentTable], delta: dict | None = None) -> dict:
    result = merge_design_delta(_base(engine, tables), delta or {"delta_version": "1.0"})
    assert result.errors == []
    assert result.output is not None
    return result.output


def _collector(queries: list[AgentQueryPattern]) -> AgentCollectorInput:
    return AgentCollectorInput.model_construct(
        contract_version="1.0", job_id=JOB, collection_timestamp=datetime(2026, 1, 1),
        source_database_name=DB, source_database_engine="postgresql", tables=_two_orders(),
        queries=AgentQueriesInput(query_patterns=queries),
    )  # fmt: skip


def _store(tmp_path: Path, engine: str, design: dict) -> tuple[LocalArtifactStore, str]:
    rel = f"{DB}/{JOB}/schema-{engine}/v1/schema_output.json"
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(design), encoding="utf-8")
    return LocalArtifactStore(str(tmp_path)), rel


def test_two_schemas_tables_of_one_name_each_get_their_own_create_table() -> None:
    design = _merged("aurora_postgresql", _two_orders())
    tables = design["table_definitions"]
    assert [(t["table_name"], t["schema_name"]) for t in tables] == [
        ("orders", "sales"),
        ("orders", "archive"),
    ]
    assert tables[0]["ddl_statements"][0].startswith('CREATE TABLE "sales"."orders" (')
    assert tables[1]["ddl_statements"][0].startswith('CREATE TABLE "archive"."orders" (')
    assert tables[0]["indexes"] == ['CREATE INDEX "by_note" ON "sales"."orders" ("note");']
    assert tables[1]["indexes"] == ['CREATE INDEX "by_note" ON "archive"."orders" ("note");']
    script = design["generated_ddl"]
    assert script.startswith(
        'CREATE SCHEMA IF NOT EXISTS "sales";\n\nCREATE SCHEMA IF NOT EXISTS "archive";\n\n'
    )
    assert script.count("CREATE TABLE ") == 2
    AuroraPostgresqlModelOutputContract.model_validate(design)


def test_the_two_schema_script_applies_on_postgresql(tmp_path_factory) -> None:
    """Both CREATE TABLE statements, and both indexes of one name, apply."""
    pgserver = pytest.importorskip("pgserver")
    design = _merged("aurora_postgresql", _two_orders())
    try:
        server = pgserver.get_server(tmp_path_factory.mktemp("pg2455"), cleanup_mode="delete")
    except Exception as exc:  # no bundled server for this platform, or it cannot start
        pytest.skip(f"pgserver cannot start a PostgreSQL here: {exc}")
    try:
        server.psql("\\set ON_ERROR_STOP on\n" + design["generated_ddl"] + "\n")
        out = server.psql(
            "\\t on\n\\a\nSELECT schemaname || '.' || tablename FROM pg_tables "
            "WHERE tablename = 'orders' ORDER BY 1;\n"
            "SELECT count(*) FROM pg_indexes WHERE indexname = 'by_note';\n"
        )
    finally:
        server.cleanup()
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    assert lines[-3:] == ["archive.orders", "sales.orders", "2"]


def test_on_aurora_mysql_the_schema_is_the_database() -> None:
    design = _merged("aurora_mysql", _two_orders())
    script = design["generated_ddl"]
    assert script.startswith(
        "CREATE SCHEMA IF NOT EXISTS `sales`;\n\nCREATE SCHEMA IF NOT EXISTS `archive`;\n\n"
    )
    assert "CREATE TABLE `sales`.`orders` (" in script
    assert "CREATE TABLE `archive`.`orders` (" in script
    assert "\n\nCREATE INDEX `by_note` ON `archive`.`orders` (" in script


@pytest.mark.passes_on_main
@pytest.mark.parametrize("schema", [None, "public"], ids=["no-schema", "one-schema"])
def test_a_design_of_one_schema_reads_as_before(schema: str | None) -> None:
    design = _merged("aurora_postgresql", [_table("a", schema), _table("b", schema)])
    assert all("schema_name" not in t for t in design["table_definitions"])
    assert design["generated_ddl"].startswith('CREATE TABLE "a" (')
    assert "CREATE SCHEMA" not in design["generated_ddl"]


def test_a_foreign_key_references_the_table_in_its_own_schema() -> None:
    fk = AgentForeignKey(
        constraint_name="fk_parent", columns=["id"], referenced_table="parents",
        referenced_columns=["id"],
    )  # fmt: skip
    other = AgentForeignKey(
        constraint_name="fk_only", columns=["id"], referenced_table="lookups",
        referenced_columns=["id"],
    )  # fmt: skip
    tables = [
        _table("orders", "sales", fks=[fk, other]),
        _table("parents", "sales"),
        _table("parents", "archive"),
        _table("lookups", "ref"),
    ]
    design = _merged("aurora_postgresql", tables)
    assert design["table_definitions"][0]["foreign_keys"] == [
        'ALTER TABLE "sales"."orders" ADD CONSTRAINT "fk_parent" FOREIGN KEY ("id") '
        'REFERENCES "sales"."parents" ("id");',
        'ALTER TABLE "sales"."orders" ADD CONSTRAINT "fk_only" FOREIGN KEY ("id") '
        'REFERENCES "ref"."lookups" ("id");',
    ]


def test_a_delta_names_one_schemas_table_and_a_bare_shared_name_is_refused() -> None:
    delta: dict = {
        "delta_version": "1.0",
        "tables": [{"table_name": "archive.orders", "add_indexes": [
            {"index_name": "by_id_note", "columns": ["id", "note"]}]}],
    }  # fmt: skip
    design = _merged("aurora_postgresql", _two_orders(), delta)
    sales, archive = design["table_definitions"]
    assert len(sales["indexes"]) == 1
    assert archive["indexes"][-1] == (
        'CREATE INDEX "by_id_note" ON "archive"."orders" ("id", "note");'
    )
    bare = {**delta, "tables": [{**delta["tables"][0], "table_name": "orders"}]}
    result = merge_design_delta(_base("aurora_postgresql", _two_orders()), bare)
    assert result.output is None
    assert result.errors == [
        "tables[0]: table 'orders' is in more than one schema (archive, sales); name it as "
        "<schema>.<table>, as the design view does"
    ]


def test_a_column_type_and_an_index_statement_reach_only_their_schemas_table() -> None:
    delta = {
        "delta_version": "1.0",
        "tables": [{
            "table_name": '"sales"."orders"',
            "column_types": [{"column": "note", "aurora_type": "VARCHAR(40)"}],
            "add_indexes": ['CREATE INDEX "ix_id" ON sales.orders ("id");'],
        }],
    }  # fmt: skip
    sales, archive = _merged("aurora_postgresql", _two_orders(), delta)["table_definitions"]
    assert [c["aurora_type"] for c in sales["columns"]] == ["INTEGER", "VARCHAR(40)"]
    assert [c["aurora_type"] for c in archive["columns"]] == ["INTEGER", "TEXT"]
    assert sales["indexes"][-1] == 'CREATE INDEX "ix_id" ON "sales"."orders" ("id");'
    assert len(archive["indexes"]) == 1
    wrong = {**delta, "tables": [{
        "table_name": "sales.orders", "add_indexes": ['CREATE INDEX "x" ON archive.orders ("id");']}]}  # fmt: skip
    result = merge_design_delta(_base("aurora_postgresql", _two_orders()), wrong)
    assert result.errors and "is not ON table 'sales.orders'" in result.errors[0]


def test_a_full_contract_response_finds_each_table_by_its_schema() -> None:
    base = _base("aurora_postgresql", _two_orders())
    response = {
        "table_definitions": [
            {"table_name": "orders", "schema_name": "archive",
             "columns": [{"name": "note", "aurora_type": "VARCHAR(10)"}]},
        ]
    }  # fmt: skip
    delta, errors = full_contract_to_delta(base, response)
    assert errors == []
    assert delta is not None
    assert [t["table_name"] for t in delta["tables"]] == ["archive.orders"]


def test_the_design_view_names_each_table_with_its_schema() -> None:
    from src.tools.schema.aurora_common.design_view import build_design_view

    base = _base("aurora_postgresql", _two_orders())
    query = AgentQueryPattern(
        query_id="q1", query_text="SELECT * FROM archive.orders WHERE note = $1",
        query_type=QueryType.SELECT, frequency_per_hour=3600, tables_accessed=["archive.orders"],
    )  # fmt: skip
    collector = _collector([query])
    view = build_design_view(base, collector)
    assert [(t["table_name"], t["query_count"]) for t in view["tables"]] == [
        ("sales.orders", 0),
        ("archive.orders", 1),
    ]


def test_an_access_pattern_resolves_the_index_of_its_schemas_table() -> None:
    tables = [_table("orders", "sales", index="sales_ix"), _table("orders", "archive",
              index="archive_ix")]  # fmt: skip
    query = AgentQueryPattern(
        query_id="q1", query_text="SELECT * FROM archive.orders WHERE note = $1",
        query_type=QueryType.SELECT, frequency_per_hour=3600, tables_accessed=["archive.orders"],
        filter_columns=["note"],
    )  # fmt: skip
    derived = derive_access_patterns([query], tables, "aurora_postgresql", qualified=True)
    assert derived.patterns[0]["index_used"] == "archive_ix"
    assert derived.patterns[0]["needs_judgment"] is False


def test_a_new_two_schema_design_loads_unchanged(tmp_path: Path) -> None:
    fk = AgentForeignKey(
        constraint_name="fk_parent", columns=["id"], referenced_table="orders",
        referenced_columns=["id"],
    )  # fmt: skip
    for engine in ("aurora_postgresql", "aurora_mysql"):
        design = _merged(engine, [*_two_orders(), _table("lines", "sales", fks=[fk])])
        assert [t["schema_name"] for t in design["table_definitions"]] == [
            "sales",
            "archive",
            "sales",
        ]
        store, key = _store(tmp_path / engine, engine, design)
        assert store.read_json(key) == design


@pytest.mark.parametrize(
    "edit",
    [
        lambda d: d["table_definitions"][1].update(schema_name="sales"),
        lambda d: d["table_definitions"][1].pop("schema_name"),
        lambda d: d.update(
            generated_ddl=d["generated_ddl"].replace(
                'CREATE SCHEMA IF NOT EXISTS "archive";\n\n', ""
            )
        ),
        lambda d: d["table_definitions"][0].update(schema_name=""),
    ],
    ids=["moved", "dropped", "no-create-schema", "empty"],
)
def test_an_edited_schema_is_refused(tmp_path: Path, edit) -> None:
    from src.shared.contract_versioning import ContractVersionError

    design = _merged("aurora_postgresql", _two_orders())
    edit(design)
    store, key = _store(tmp_path, "aurora_postgresql", design)
    with pytest.raises(ContractVersionError):
        store.read_json(key)


def test_a_two_schema_design_saved_before_2455_still_loads(tmp_path: Path) -> None:
    """The file main wrote for ``sales.orders`` and ``archive.orders``: one bare name for
    both tables, ``CREATE TABLE "orders"`` twice. It loads as stored, and both tables read
    as one bare-named table, as they did."""
    saved = json.loads(LEGACY.read_text(encoding="utf-8"))
    assert [t["table_name"] for t in saved["table_definitions"]] == ["orders", "orders"]
    assert all("schema_name" not in t for t in saved["table_definitions"])
    store, key = _store(tmp_path, "aurora_postgresql", saved)
    loaded = store.read_json(key)
    assert loaded == saved
    model = AuroraPostgresqlModelOutputContract.model_validate(loaded)
    assert [t.schema_name for t in model.table_definitions] == [None, None]


def test_the_fingerprint_marks_a_qualified_draft_only() -> None:
    one = _base("aurora_postgresql", [_table("a", "s")])
    two = _base("aurora_postgresql", _two_orders())
    unqualified = _base("aurora_postgresql", _two_orders(), schema_qualified=False)
    assert two.qualified and not one.qualified and not unqualified.qualified
    assert two.fingerprint() != unqualified.fingerprint()


def test_from_inputs_keys_each_schemas_source_types_apart() -> None:
    """The raw collector's ``data_type`` per column is keyed on the draft's name, so two
    schemas' ``orders.note`` keep their own source type."""
    raw = {
        "database_schema": {
            "tables": [
                {"table_name": "orders", "schema_name": "sales",
                 "columns": [{"column_name": "note", "data_type": "text"}]},
                {"table_name": "orders", "schema_name": "archive",
                 "columns": [{"column_name": "note", "data_type": "character varying"}]},
            ]
        }
    }  # fmt: skip
    collector = _collector([])
    base = AuroraDesignBase.from_inputs("aurora_postgresql", collector, raw)
    assert base.qualified
    assert base.source_data_types == {
        ("sales.orders", "note"): "text",
        ("archive.orders", "note"): "character varying",
    }


# #2479 review: names that fold to one key, unknown schemas, ambiguous foreign keys.


@pytest.mark.parametrize("schemas", [("Sales", "sales"), ('a"b', "ab")], ids=["case", "quote"])
def test_two_tables_whose_keys_fold_to_one_are_left_out_with_a_note(schemas) -> None:
    """A key folds case and drops quote marks, so ``Sales.orders`` and ``sales.orders``
    share one. Neither is designed (a delta on one could edit the other); each gets a
    note, and a delta that names one finds no table."""
    tables = [_table("orders", schemas[0]), _table("orders", schemas[1]), _table("items", "crm")]
    base = _base("aurora_postgresql", tables)
    assert base.qualified
    design = _merged("aurora_postgresql", tables)
    assert [(t["schema_name"], t["table_name"]) for t in design["table_definitions"]] == [
        ("crm", "items")
    ]
    assert design["generated_ddl"].count("CREATE TABLE ") == 1
    notes = [n for n in design["app_layer_notes"] if n["feature"] == "colliding_table_name"]
    # #2484: a left-out table's note names it once.
    assert [n["source_object"] for n in notes] == [f"{schemas[0]}.orders", f"{schemas[1]}.orders"]
    assert all("another table of the design" in n["recommendation"] for n in notes)
    delta = {"delta_version": "1.0", "tables": [{"table_name": f"{schemas[0]}.orders",
             "add_indexes": [{"index_name": "ix_x", "columns": ["id", "note"]}]}]}  # fmt: skip
    result = merge_design_delta(base, delta)
    assert result.output is None and "unknown table" in result.errors[0]


def test_case_only_different_schemas_keep_distinct_tables_apart() -> None:
    """``Sales.a`` and ``sales.b`` do not collide: each is created in its own schema, as
    the source spells it, and a delta on ``Sales.a`` reaches only that table."""
    tables = [_table("a", "Sales"), _table("b", "sales")]
    delta = {"delta_version": "1.0", "tables": [{"table_name": "Sales.a",
             "add_indexes": [{"index_name": "ix_x", "columns": ["id", "note"]}]}]}  # fmt: skip
    a, b = _merged("aurora_postgresql", tables, delta)["table_definitions"]
    assert a["ddl_statements"][0].startswith('CREATE TABLE "Sales"."a" (')
    assert b["ddl_statements"][0].startswith('CREATE TABLE "sales"."b" (')
    assert a["indexes"][-1] == 'CREATE INDEX "ix_x" ON "Sales"."a" ("id", "note");'
    assert len(b["indexes"]) == 1


def test_a_name_of_a_schema_the_design_lacks_reads_as_no_table() -> None:
    from src.shared.table_keys import DesignTableKeys

    keys = DesignTableKeys([("orders", "sales"), ("customers", "archive")], source_database=DB)
    assert keys.key_of("crm.orders") is None
    assert keys.key_of("db.crm.orders") is None
    assert keys.key_of("sales.orders") == "sales.orders"
    assert keys.key_of("orders") == "sales.orders"
    # The source database's prefix is no schema: the name reads as its bare name.
    assert keys.key_of(f"{DB}.orders") == "sales.orders"
    assert DesignTableKeys([("orders", "sales"), ("x", "a")]).key_of("shop.orders") is None


def test_keys_two_tables_share_read_as_no_table() -> None:
    from src.shared.table_keys import DesignTableKeys

    keys = DesignTableKeys([("orders", "Sales"), ("orders", "sales"), ("items", "crm")])
    assert keys.key_of("sales.orders") is None and keys.key_of("Sales.orders") is None
    assert keys.key_of("orders") is None and keys.key_of("crm.items") == "crm.items"


def test_an_access_pattern_on_a_schema_the_design_lacks_resolves_no_index() -> None:
    query = AgentQueryPattern(
        query_id="q1", query_text="SELECT * FROM crm.orders WHERE note = $1",
        query_type=QueryType.SELECT, frequency_per_hour=3600, tables_accessed=["crm.orders"],
        filter_columns=["note"],
    )  # fmt: skip
    tables = [_table("orders", "sales"), _table("customers", "archive")]
    derived = derive_access_patterns([query], tables, "aurora_postgresql", qualified=True)
    pattern = derived.patterns[0]
    assert pattern["index_used"] == "__seq_scan__" and pattern["load_testable"] is False
    assert "None of this query's tables could be identified" in pattern["judgment_reason"]


def _customers_fk() -> AgentForeignKey:
    return AgentForeignKey(constraint_name="fk_c", columns=["id"], referenced_table="customers",
                           referenced_columns=["id"])  # fmt: skip


def _ambiguous_fk_tables() -> list[AgentTable]:
    return [_table("orders", "sales", fks=[_customers_fk()]), _table("customers", "crm"),
            _table("customers", "archive")]  # fmt: skip


def test_a_foreign_key_two_other_schemas_could_reference_is_left_out_with_a_note() -> None:
    design = _merged("aurora_postgresql", _ambiguous_fk_tables())
    orders = design["table_definitions"][0]
    assert orders["foreign_keys"] == []
    assert "FOREIGN KEY" not in design["generated_ddl"]
    notes = [n for n in design["app_layer_notes"] if n["source_object"] == "sales.orders.fk_c"]
    assert len(notes) == 1 and notes[0]["feature"] == "ambiguous_reference"
    assert "archive, crm" in notes[0]["recommendation"]


def test_a_foreign_key_finds_its_one_table_in_a_schema_of_another_case() -> None:
    tables = [_table("orders", "sales", fks=[_customers_fk()]), _table("customers", "Sales")]
    orders = _merged("aurora_postgresql", tables)["table_definitions"][0]
    assert orders["foreign_keys"] == [
        'ALTER TABLE "sales"."orders" ADD CONSTRAINT "fk_c" FOREIGN KEY ("id") '
        'REFERENCES "Sales"."customers" ("id");'
    ]


def test_the_script_without_an_ambiguous_foreign_key_applies_on_postgresql(
    tmp_path_factory,
) -> None:
    pgserver = pytest.importorskip("pgserver")
    design = _merged("aurora_postgresql", _ambiguous_fk_tables())
    try:
        server = pgserver.get_server(tmp_path_factory.mktemp("pg2479"), cleanup_mode="delete")
    except Exception as exc:  # no bundled server for this platform, or it cannot start
        pytest.skip(f"pgserver cannot start a PostgreSQL here: {exc}")
    try:
        server.psql("\\set ON_ERROR_STOP on\n" + design["generated_ddl"] + "\n")
        out = server.psql(
            "\\t on\n\\a\nSELECT schemaname || '.' || tablename FROM pg_tables "
            "WHERE tablename IN ('orders', 'customers') ORDER BY 1;\n"
        )
    finally:
        server.cleanup()
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    assert lines[-3:] == ["archive.customers", "crm.customers", "sales.orders"]


def test_the_bedrock_reviewer_gets_the_touched_tables_of_a_design_of_several_schemas() -> None:
    from src.contracts.aurora_design_delta import AuroraDesignDeltaContract
    from src.tools.schema.aurora_common.bedrock_delta import pe_review_evidence, pe_review_prompt

    view = {"migration_strategy": "homogeneous", "tables": [
        {"table_name": "sales.orders"}, {"table_name": "archive.orders"}, {"table_name": "plain"}]}  # fmt: skip
    delta = AuroraDesignDeltaContract.model_validate({"delta_version": "1.0", "tables": [
        {"table_name": '"sales"."orders"'}, {"table_name": "plain"}, {"table_name": "orders"},
        {"table_name": "crm.orders"}]})  # fmt: skip
    touched = pe_review_evidence(delta, view)["touched_tables"]
    # A bare name two schemas share, or a schema the design lacks, touches no table.
    assert [t["table_name"] for t in touched] == ["sales.orders", "plain"]
    prompt = pe_review_prompt("aurora_postgresql", delta, {"design_view": view})
    evidence = prompt.split("## Design Delta")[0]
    assert '"table_name": "sales.orders"' in evidence
    assert '"table_name": "archive.orders"' not in evidence


@pytest.mark.parametrize("engine", ["aurora_mysql", "aurora_postgresql"])
def test_the_designer_is_told_a_multi_schema_design_names_tables_schema_table(engine) -> None:
    from src.tools.schema.aurora_common.bedrock_delta import designer_prompt

    command = (REPO / f".claude/commands/design-schema-{engine.replace('_', '-')}.md").read_text()
    skill = (REPO / f"src/skills/{engine}-data-modeling.md").read_text()
    assert '`schema.table` (`"table_name": "sales.orders"`)' in command
    assert "names each table `schema.table`" in skill
    assert "names them schema.table" in designer_prompt(engine, {"migration_strategy": "x"})
