"""Contract 1.3 of the public ``assess()`` result (#1632), as a library caller reads it.

- each ``summary.table_facts[].foreign_keys[]`` entry carries ``avg_children_per_parent``
  (the table's row count divided by the referenced table's) and ``basis``, per
  constraint, from ``run_through="analysis"`` on, checked here against the collection's
  own counts on the SQL Server and PostgreSQL samples;
- an unknown count (-1, NULL or none), an empty parent and an unresolved referenced
  table give ``null``, never an average computed from 0;
- a request pinned to 1.0, 1.1 or 1.2 gets no 1.3 field and the result it got before
  1.3; one that names no version gets 1.3.

The fields are statistics, never a verdict: no field says how a data model should
hold the two tables.
"""

from __future__ import annotations

import copy
import json
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError

from scripts import render_contract_schemas as rcs
from src.assessment import CONTRACT_VERSION, AssessmentResult, assess, load_collection

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures"
#: The foreign key fields 1.3 added (``ADDED_IN_1_3``), named here so the file reads on
#: any version's code.
NEW_FIELDS = ("avg_children_per_parent", "basis")
#: Every field 1.3 added, by model.
ADDED = {
    "TableForeignKey": NEW_FIELDS,
    "TableFacts": ("row_count_basis",),
    "StatementResult": ("parse_status",),
}

#: The published result schema each earlier version shipped (1.2: main before #1632).
EARLIER_SCHEMAS = {
    "1.0": FIXTURES / "assess_result_v1_0.schema.json",
    "1.1": FIXTURES / "assess_result_v1_1.schema.json",
    "1.2": FIXTURES / "assess_result_v1_2.schema.json",
}
#: A 1.2 result written by main's code before #1632: the trailgear sample through
#: "analysis", its statements cut to three (every table fact kept).
RESULT_1_2 = FIXTURES / "assess_result_v1_2_analysis.json"


@cache
def _collection(sample: str) -> dict[str, Any]:
    loaded: dict[str, Any] = load_collection(REPO / "docs" / "examples" / sample / f"{sample}.zip")
    return loaded


def _run(
    collection: dict[str, Any],
    database_name: str,
    run_through: str = "analysis",
    version: str | None = None,
) -> str:
    request: dict[str, Any] = {
        "database_name": database_name,
        "collection": collection,
        "options": {"run_through": run_through},
    }
    if version is not None:
        request["contract_version"] = version
    return assess(request).model_dump_json()


@cache
def _sample_json(sample: str, run_through: str = "analysis", version: str | None = None) -> str:
    return _run(_collection(sample), sample, run_through, version)


def _foreign_keys(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every foreign key entry by constraint name, with its table's id and row count."""
    out: dict[str, dict[str, Any]] = {}
    for table in document["summary"]["table_facts"]:
        for fk in table["foreign_keys"]:
            assert fk["constraint_name"] not in out
            out[fk["constraint_name"]] = {**fk, "table_id": table["table_id"]}
    return out


def _raw_counts(collection: dict[str, Any]) -> dict[str, Any]:
    """The collection's own row count per table name, as its script wrote it."""
    return {t["table_name"]: t.get("row_count", "missing") for t in collection["tables"]}


def _expected(child: Any, parent: Any) -> float | None:
    if not (isinstance(child, int) and isinstance(parent, int)):
        return None
    return child / parent if child >= 0 and parent > 0 else None


def _check_against_the_collection(sample: str) -> dict[str, dict[str, Any]]:
    collection = _collection(sample)
    counts = _raw_counts(collection)
    parents = {
        fk["constraint_name"]: fk["referenced_table_name"] for fk in collection["foreign_keys"]
    }
    children = {fk["constraint_name"]: fk["table_name"] for fk in collection["foreign_keys"]}
    fks = _foreign_keys(json.loads(_sample_json(sample)))
    assert set(fks) == set(parents)
    for name, fk in fks.items():
        expected = _expected(counts[children[name]], counts[parents[name]])
        assert fk["avg_children_per_parent"] == expected, name
        assert fk["basis"] == (None if expected is None else "catalogue_estimate"), name
    return fks


# --------------------------------------------------------------------------- the samples


def test_trailgear_averages_are_the_collected_counts() -> None:
    """SQL Server: every count is known, so all 28 foreign keys have an average."""
    fks = _check_against_the_collection("trailgear")
    assert len(fks) == 28
    assert all(fk["avg_children_per_parent"] is not None for fk in fks.values())
    # SalesOrder 2,740,000 rows, Customer 412,000 (the collection's estimates).
    assert fks["FK_SalesOrder_Customer"]["avg_children_per_parent"] == 2_740_000 / 412_000
    assert fks["FK_StockLevel_Warehouse"]["avg_children_per_parent"] == 410_000 / 12


def test_discourse_unknown_counts_give_null() -> None:
    """PostgreSQL: 265 of 311 tables were never analysed (``reltuples`` -1), so only the
    foreign keys between two counted tables, with a non-empty parent, have an average."""
    fks = _check_against_the_collection("discourse")
    counts = _raw_counts(_collection("discourse"))
    assert len(fks) == 23
    with_average = sorted(n for n, fk in fks.items() if fk["avg_children_per_parent"] is not None)
    assert with_average == ["fk_rails_38ea484ed4", "fk_rails_ed33506dbd"]
    # user_profiles 53 rows per badges 55; javascript_caches 99 per theme_fields 155.
    assert fks["fk_rails_38ea484ed4"]["avg_children_per_parent"] == 53 / 55
    assert fks["fk_rails_ed33506dbd"]["avg_children_per_parent"] == 99 / 155
    # A table with a count of -1 is a parent here: its keys have no average.
    assert counts["uploads"] == -1
    assert fks["fk_rails_84f2496311"]["avg_children_per_parent"] is None


def test_analysis_and_reality_check_give_the_same_values() -> None:
    analysis = json.loads(_sample_json("trailgear", "analysis"))["summary"]["table_facts"]
    placed = json.loads(_sample_json("trailgear", "reality_check"))["summary"]["table_facts"]
    assert analysis == placed
    fks = [fk for table in placed for fk in table["foreign_keys"]]
    assert len(fks) == 28
    assert all(fk["basis"] == "catalogue_estimate" for fk in fks)


def test_a_self_reference_divides_the_table_by_itself() -> None:
    fks = _foreign_keys(json.loads(_sample_json("trailgear")))
    for name in ("FK_ProductCategory_Parent", "FK_TicketMessage_ReplyTo"):
        assert fks[name]["referenced_table"] == fks[name]["table_id"]
        assert fks[name]["avg_children_per_parent"] == 1.0


# --------------------------------------------------------------------------- edited collections


def _fk(table: str, name: str, columns: list[str], parent: str, schema: str = "dbo") -> list[dict]:
    return [
        {
            "table_name": table,
            "schema_name": schema,
            "constraint_name": name,
            "column_name": column,
            "referenced_table_name": parent,
            "referenced_column_name": column,
            "on_update": "NO ACTION",
            "on_delete": "NO ACTION",
            "constraint_column_id": i,
        }
        for i, column in enumerate(columns, 1)
    ]


def _column(table: str, name: str, schema: str, position: int) -> dict[str, Any]:
    return {
        "table_name": table,
        "schema_name": schema,
        "column_name": name,
        "ordinal_position": position,
        "data_type": "int",
        "udt_name": "int",
        "is_nullable": "YES",
    }


@cache
def _edited_collection() -> dict[str, Any]:
    """The trailgear collection with unknown and empty counts, unknown column
    nullability and execution counts, an empty statement text, a composite key, a
    second key to the same parent, and a key to a table name two other schemas hold."""
    collection = copy.deepcopy(_collection("trailgear"))
    tables = {t["table_name"]: t for t in collection["tables"]}
    tables["Customer"]["row_count"] = -1
    tables["ProductCategory"]["row_count"] = None
    del tables["Brand"]["row_count"]
    tables["TicketMessage"]["row_count"] = -1
    tables["Promotion"]["row_count"] = 0
    tables["Shipment"]["row_count"] = 0
    keys = collection["foreign_keys"]
    keys += _fk(
        "StockMovement", "FK_StockMovement_StockLevel", ["WarehouseID", "ProductID"], "StockLevel"
    )
    collection["columns"].append(_column("SalesOrder", "BillToAddressID", "dbo", 9))
    keys += _fk("SalesOrder", "FK_SalesOrder_BillTo", ["BillToAddressID"], "CustomerAddress")
    for schema, rows in (("Geo", 10), ("Legacy", 20), ("Retail", 100)):
        name = "Store" if schema == "Retail" else "Region"
        collection["tables"].append(
            {
                "schema_name": schema,
                "table_name": name,
                "table_id": f"{schema}.{name}",
                "row_count": rows,
            }
        )
    collection["columns"] += [
        _column("Region", "RegionID", "Geo", 1),
        _column("Store", "StoreID", "Retail", 1),
        _column("Store", "RegionID", "Retail", 2),
    ]
    keys += _fk("Store", "FK_Store_Region", ["RegionID"], "Region", schema="Retail")
    # Nullability: missing, NULL and a value no script writes.
    columns = {(c["table_name"], c["column_name"]): c for c in collection["columns"]}
    del columns[("Brand", "Name")]["is_nullable"]
    columns[("Brand", "Country")]["is_nullable"] = None
    columns[("Brand", "BrandID")]["is_nullable"] = "unknown"
    # Execution counts: missing and NULL; one row with no text.
    queries = collection["queries"]
    del queries[0]["execution_count"]
    queries[1]["execution_count"] = None
    queries[3]["execution_count"] = 0
    queries.append({**queries[2], "digest": "0x00000000000000AA", "query_text": ""})
    return collection


@cache
def _edited_json(version: str | None = None) -> str:
    return _run(_edited_collection(), "trailgear", "analysis", version)


def _edited_trailgear() -> dict[str, dict[str, Any]]:
    return _foreign_keys(json.loads(_edited_json()))


@pytest.mark.parametrize(
    ("constraint", "why"),
    [
        ("FK_SalesOrder_Customer", "parent count -1"),
        ("FK_Product_Category", "parent count NULL"),
        ("FK_Product_Brand", "parent count missing"),
        ("FK_TicketMessage_Ticket", "child count -1"),
        ("FK_ProductCategory_Parent", "self-reference, count NULL"),
        ("FK_SalesOrder_Promotion", "empty parent (0 rows)"),
    ],
)
def test_an_unknown_count_or_an_empty_parent_gives_null(constraint: str, why: str) -> None:
    fk = _edited_trailgear()[constraint]
    assert fk["referenced_table"] is not None, why
    assert fk["avg_children_per_parent"] is None, why
    assert fk["basis"] is None, why


def test_an_empty_child_table_averages_zero() -> None:
    fk = _edited_trailgear()["FK_Shipment_Order"]
    assert fk["avg_children_per_parent"] == 0.0
    assert fk["basis"] == "catalogue_estimate"


def test_a_composite_key_has_one_entry_with_its_columns() -> None:
    fk = _edited_trailgear()["FK_StockMovement_StockLevel"]
    assert fk["columns"] == fk["referenced_columns"] == ["WarehouseID", "ProductID"]
    assert fk["referenced_table"] == "trailgear.StockLevel"
    assert fk["avg_children_per_parent"] == 14_200_000 / 410_000


def test_a_second_key_to_the_same_parent_has_its_own_entry() -> None:
    fks = _edited_trailgear()
    first, second = fks["FK_SalesOrder_Address"], fks["FK_SalesOrder_BillTo"]
    assert first["referenced_table"] == second["referenced_table"] == "trailgear.CustomerAddress"
    assert (first["columns"], second["columns"]) == (["ShipToAddressID"], ["BillToAddressID"])
    assert (
        first["avg_children_per_parent"] == second["avg_children_per_parent"] == 2_740_000 / 530_000
    )
    assert first["basis"] == second["basis"] == "catalogue_estimate"


def test_an_ambiguous_cross_schema_parent_gives_null() -> None:
    """Retail.Store references Region, which Geo and Legacy both hold and Retail does not:
    the referenced table is not resolved (assess.md, Table facts), so no average."""
    fk = _edited_trailgear()["FK_Store_Region"]
    assert fk["referenced_table"] is None
    assert fk["avg_children_per_parent"] is None
    assert fk["basis"] is None


# --------------------------------------------------------------------------- versions


def _rows(document: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    tables = document["summary"].get("table_facts") or []
    return [
        *((t, "TableFacts") for t in tables),
        *((fk, "TableForeignKey") for t in tables for fk in t["foreign_keys"]),
        *((st, "StatementResult") for st in document["statements"]),
    ]


def _has_a_1_3_field(document: dict[str, Any]) -> bool:
    return any(set(ADDED[model]) & set(row) for row, model in _rows(document))


def _as_1_2(document: dict[str, Any]) -> dict[str, Any]:
    """A 1.3 document less the 1.3 fields, marked 1.2."""
    stripped = copy.deepcopy(document)
    stripped["contract_version"] = "1.2"
    for row, model in _rows(stripped):
        for name in ADDED[model]:
            del row[name]
    return stripped


@pytest.mark.parametrize("version", ["1.0", "1.1", "1.2"])
def test_a_request_pinned_before_1_3_gets_no_1_3_field(version: str) -> None:
    """A 1.0, 1.1 or 1.2 request's result has no 1.3 field, validates against the schema
    file that version shipped, and reads back as the same document."""
    text = _sample_json("trailgear", "analysis", version)
    document = json.loads(text)
    assert document["contract_version"] == version
    assert not _has_a_1_3_field(document)
    Draft202012Validator(json.loads(EARLIER_SCHEMAS[version].read_text("utf-8"))).validate(document)
    assert AssessmentResult.model_validate_json(text).model_dump_json() == text
    latest = json.loads(_sample_json("trailgear"))
    assert _has_a_1_3_field(latest)
    if version == "1.2":
        # The 1.2 result is the latest one less the two fields, and nothing else.
        assert document == _as_1_2(latest)


def test_a_1_2_result_has_the_table_facts_main_wrote_before_1_3() -> None:
    """Byte for byte, a 1.2 request's table facts are the ones main's code wrote before
    #1632 for the same collection (``RESULT_1_2``)."""
    before = json.loads(RESULT_1_2.read_text("utf-8"))
    now = json.loads(_sample_json("trailgear", "analysis", "1.2"))
    as_text = json.dumps  # one serializer for both sides
    assert as_text(now["summary"]["table_facts"]) == as_text(before["summary"]["table_facts"])
    assert _has_a_1_3_field(json.loads(_sample_json("trailgear")))


def test_a_1_2_result_written_before_1_3_loads_and_writes_back_unchanged() -> None:
    text = RESULT_1_2.read_text("utf-8")
    document = json.loads(text)
    assert document["contract_version"] == "1.2"
    published = json.loads(rcs.schema_path("assess_result").read_text("utf-8"))
    Draft202012Validator(published).validate(document)
    Draft202012Validator(json.loads(EARLIER_SCHEMAS["1.2"].read_text("utf-8"))).validate(document)
    assert json.loads(AssessmentResult.model_validate_json(text).model_dump_json()) == document
    # Read as 1.3, the same document has the new fields, unknown.
    document["contract_version"] = "1.3"
    relabelled = json.loads(AssessmentResult.model_validate(document).model_dump_json())
    fks = [fk for t in relabelled["summary"]["table_facts"] for fk in t["foreign_keys"]]
    assert fks and all(fk["avg_children_per_parent"] is fk["basis"] is None for fk in fks)


@pytest.mark.parametrize("version", ["1.3", None])
def test_a_1_3_request_or_one_with_no_version_gets_the_latest(version: str | None) -> None:
    document = json.loads(_sample_json("trailgear", "analysis", version))
    assert document["contract_version"] == CONTRACT_VERSION == "1.3"
    assert _has_a_1_3_field(document)
    published = json.loads(rcs.schema_path("assess_result").read_text("utf-8"))
    assert published["x-contract-version"] == "1.3"
    Draft202012Validator(published).validate(document)
    # The 1.2 schema file refuses it: it has fields 1.2 lacks.
    document["contract_version"] = "1.2"
    with pytest.raises(ValidationError, match="was unexpected"):
        Draft202012Validator(json.loads(EARLIER_SCHEMAS["1.2"].read_text("utf-8"))).validate(
            document
        )


def test_the_fields_carry_no_verdict() -> None:
    """No field, value or description of the foreign key entry names a data-model
    choice; the descriptions say the values are statistics."""
    schema = json.loads(rcs.schema_path("assess_result").read_text("utf-8"))
    entry = schema["$defs"]["TableForeignKey"]
    text = json.dumps(entry).lower()
    for word in ("embed", "fold", "denormali", "verdict", "bounded_"):
        assert word not in text, word
    note = "an average cannot show whether a child set is bounded"
    assert note in entry["description"].replace("\n", " ")
    assert note in entry["properties"]["avg_children_per_parent"]["description"]
    assert entry["properties"]["basis"]["anyOf"][0] == {
        "enum": ["catalogue_estimate", "catalogue_exact"],
        "type": "string",
    }
    doc = (REPO / "docs" / "contracts" / "assess.md").read_text("utf-8")
    assert "They are statistics, not a design decision: " + note in doc
    assert 'the recommended path is `run_through: "schema_design"`' in doc


def test_the_models_name_the_same_1_3_fields() -> None:
    from src.assessment import models

    assert getattr(models, "ADDED_IN_1_3", None) == ADDED


# --------------------------------------------------------------------------- unknown is null


def _value(document: dict[str, Any], where: tuple[str, ...]) -> Any:
    """The value at ``where``: ("table", id, field), ("column", table, column), or
    ("statement", index into the collection's rows, field)."""
    kind = where[0]
    if kind == "table":
        tables = {t["table_id"]: t for t in document["summary"]["table_facts"]}
        return tables[where[1]].get(where[2])
    if kind == "column":
        tables = {t["table_id"]: t for t in document["summary"]["table_facts"]}
        return {c["name"]: c for c in tables[where[1]]["columns"]}[where[2]]["nullable"]
    digest = _edited_collection()["queries"][int(where[1])]["digest"]
    return {st["query_id"]: st for st in document["statements"]}[digest][where[2]]


@pytest.mark.parametrize(
    ("where", "before"),
    [
        (("table", "trailgear.Customer", "row_count"), 0),  # -1
        (("table", "trailgear.ProductCategory", "row_count"), 0),  # NULL
        (("table", "trailgear.Brand", "row_count"), 0),  # missing
        (("column", "trailgear.Brand", "Name"), True),  # missing: read as YES
        (("column", "trailgear.Brand", "Country"), False),  # NULL: read as NO
        (("column", "trailgear.Brand", "BrandID"), False),  # a value no script writes
        (("statement", "0", "calls_per_second"), 1 / 86400),  # missing: one call a day
        (("statement", "1", "calls_per_second"), 1 / 86400),  # NULL
        (("statement", "-1", "query_text"), ""),  # empty text
    ],
)
def test_an_unknown_value_is_null_in_1_3_and_as_before_in_1_2(
    where: tuple[str, ...], before: Any
) -> None:
    assert _value(json.loads(_edited_json()), where) is None
    assert _value(json.loads(_edited_json("1.2")), where) == before


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    ("where", "value"),
    [
        (("table", "trailgear.Promotion", "row_count"), 0),
        (("table", "trailgear.Shipment", "row_count"), 0),
        (("column", "trailgear.Customer", "CustomerID"), False),
        (("statement", "2", "calls_per_second"), None),  # the row's own count a day
    ],
)
def test_a_known_value_is_the_same_in_1_3_and_1_2(where: tuple[str, ...], value: Any) -> None:
    if value is None:
        value = _edited_collection()["queries"][2]["execution_count"] / 86400
    for version in (None, "1.2"):
        got = _value(json.loads(_edited_json(version)), where)
        assert got == pytest.approx(value) if isinstance(value, float) else got == value


@pytest.mark.passes_on_main
def test_the_1_2_result_of_the_edited_collection_is_the_latest_as_1_2_wrote_it() -> None:
    """The edited collection's 1.2 result, with its 1.2 values restored, validates
    against the 1.2 schema and reads back as the same document."""
    text = _edited_json("1.2")
    assert AssessmentResult.model_validate_json(text).model_dump_json() == text
    Draft202012Validator(json.loads(EARLIER_SCHEMAS["1.2"].read_text("utf-8"))).validate(
        json.loads(text)
    )


# --------------------------------------------------------------------------- other inputs


@pytest.mark.parametrize("sample", ["trailgear", "discourse"])
def test_a_collector_output_as_the_collection_gives_the_same_averages(sample: str) -> None:
    """A collector output passed back as the collection (the run's own) gives the
    averages and table facts the raw collection gives: it records an unknown count as
    unknown (None), so discourse's keys stay null."""
    collection = _collection(sample)
    run = assess(
        {
            "database_name": sample,
            "collection": collection,
            "options": {"run_through": "analysis", "include_artifacts": True},
        }
    )
    assert run.artifacts is not None
    collector_output = run.artifacts.model_dump(mode="json")["collector_output"]
    again = json.loads(_run(collector_output, sample))
    raw = json.loads(_sample_json(sample))
    assert _foreign_keys(again) == _foreign_keys(raw)
    with_average = [fk for fk in _foreign_keys(again).values() if fk["basis"]]
    assert len(with_average) == {"trailgear": 28, "discourse": 2}[sample]
    assert again["summary"]["table_facts"] == raw["summary"]["table_facts"]


def test_a_table_with_no_schema_name_is_found_by_its_name() -> None:
    """MySQL and MariaDB collections give no schema_name; the collector fills in the
    database name, and the counts are still found by table name."""
    collection = copy.deepcopy(_collection("wordpress"))
    assert not any(t.get("schema_name") for t in collection["tables"])
    collection["foreign_keys"] = _fk("wp_posts", "fk_posts_author", ["post_author"], "wp_users")
    for fk in collection["foreign_keys"]:
        fk["referenced_column_name"] = "ID"
        del fk["schema_name"]
    fk = _foreign_keys(json.loads(_run(collection, "wordpress")))["fk_posts_author"]
    assert fk["referenced_table"] == "wordpress.wp_users"
    assert fk["avg_children_per_parent"] == 11990 / 1
    assert fk["basis"] == "catalogue_estimate"


# --------------------------------------------------------------------------- the builder


def _facts(*tables: dict[str, Any]) -> dict[str, Any]:
    from src.assessment.result_builder import RunState, build_table_facts

    column = {"column_name": "id", "data_type": "int", "nullable": False}
    state = RunState(
        run_through="analysis",
        collector={
            "database_schema": {
                "tables": [
                    {"table_name": t["table_id"].split(".")[1], "columns": [column], **t}
                    for t in tables
                ]
            }
        },
        triage={},
    )
    return {f.table_id: f for f in build_table_facts(state)}


def test_an_unknown_size_is_null_in_1_3_and_0_before() -> None:
    facts = _facts(
        {"table_id": "db.a", "row_count": 5, "size_mb": None},
        {"table_id": "db.b", "row_count": 0, "size_mb": 0.0},
    )
    assert facts["db.a"].size_mb is None
    assert facts["db.a"].written_before_1_3() == {"size_mb": 0.0}
    assert facts["db.b"].size_mb == 0.0 and facts["db.b"].row_count == 0
    assert facts["db.b"].written_before_1_3() == {}


def test_an_average_of_two_exact_counts_says_so() -> None:
    fk = {
        "constraint_name": "fk",
        "columns": ["p"],
        "referenced_table": "p",
        "referenced_columns": ["id"],
    }
    facts = _facts(
        {"table_id": "db.p", "row_count": 4, "row_count_basis": "exact"},
        {"table_id": "db.c", "row_count": 0, "row_count_basis": "exact", "foreign_keys": [fk]},
        {"table_id": "db.e", "row_count": 8, "row_count_basis": "estimate", "foreign_keys": [fk]},
        {"table_id": "db.u", "row_count": None, "foreign_keys": [fk]},
    )
    exact, estimate, unknown = (facts[t].foreign_keys[0] for t in ("db.c", "db.e", "db.u"))
    # 0 only when both counts are known.
    assert (exact.avg_children_per_parent, exact.basis) == (0.0, "catalogue_exact")
    assert (estimate.avg_children_per_parent, estimate.basis) == (2.0, "catalogue_estimate")
    assert (unknown.avg_children_per_parent, unknown.basis) == (None, None)
    assert facts["db.u"].row_count is None and facts["db.u"].row_count_basis is None
    assert facts["db.u"].written_before_1_3() == {"row_count": 0}
    assert facts["db.p"].row_count_basis == "exact"


# --------------------------------------------------------------------------- parse status


def test_parse_status_marks_the_statements_the_parser_could_not_read() -> None:
    """trailgear: the parser reads 90 statements and falls back on 5 (4 that are no
    statement it knows, 1 command), as its stored readings record."""
    from collections import Counter

    statements = json.loads(_sample_json("trailgear"))["statements"]
    assert Counter(st["parse_status"] for st in statements) == {"parsed": 90, "fallback": 5}
    pinned = json.loads(_sample_json("trailgear", "analysis", "1.2"))["statements"]
    assert not any("parse_status" in st for st in pinned)


def test_a_statement_with_no_text_has_no_parse_status() -> None:
    document = json.loads(_edited_json())
    text_less = [st for st in document["statements"] if st["query_text"] is None]
    assert text_less and all(st["parse_status"] is None for st in text_less)


def test_a_table_with_no_size_runs_and_is_null_in_1_3() -> None:
    """The edited collection's Geo/Legacy Region and Retail Store tables give no data or
    index size: the run completes, 1.3 writes their size as null and 1.2 as 0."""
    latest = {t["table_id"]: t for t in json.loads(_edited_json())["summary"]["table_facts"]}
    pinned = {t["table_id"]: t for t in json.loads(_edited_json("1.2"))["summary"]["table_facts"]}
    for table_id in ("trailgear.Region", "trailgear.Store"):
        assert latest[table_id]["size_mb"] is None
        assert pinned[table_id]["size_mb"] == 0.0
    assert latest["trailgear.Brand"]["size_mb"] == pinned["trailgear.Brand"]["size_mb"] > 0


def test_a_count_with_no_basis_is_null_in_1_3_and_absent_before() -> None:
    latest = {t["table_id"]: t for t in json.loads(_edited_json())["summary"]["table_facts"]}
    pinned = {t["table_id"]: t for t in json.loads(_edited_json("1.2"))["summary"]["table_facts"]}
    assert "row_count_basis" in latest["trailgear.Customer"]
    assert latest["trailgear.Customer"]["row_count_basis"] is None  # no count, no basis
    assert latest["trailgear.Product"]["row_count_basis"] == "estimate"
    assert "row_count_basis" not in pinned["trailgear.Customer"]


def test_an_execution_count_of_0_is_0_calls_in_1_3_and_one_a_day_before() -> None:
    """A real 0 is 0.0 on the stable surface from 1.3; the parser, and so routing, still
    counts it as one call a day, which 1.2 shows."""
    digest = _edited_collection()["queries"][3]["digest"]

    def calls(version: str | None) -> Any:
        statements = json.loads(_edited_json(version))["statements"]
        return {st["query_id"]: st for st in statements}[digest]["calls_per_second"]

    assert calls(None) == 0.0
    assert calls("1.2") == 1 / 86400


def test_a_collector_output_carries_the_collectors_nullable_and_calls() -> None:
    """Documented (assess.md, From 1.2 to 1.3): a collector output records no unknown
    nullability or execution count, so passed back as the collection it gives the
    collector's values (true, false, one call a day) where the raw collection gives
    null."""
    run = assess(
        {
            "database_name": "trailgear",
            "collection": _edited_collection(),
            "options": {"run_through": "analysis", "include_artifacts": True},
        }
    )
    assert run.artifacts is not None
    collector_output = run.artifacts.model_dump(mode="json")["collector_output"]
    again = json.loads(_run(collector_output, "trailgear"))
    raw = json.loads(_edited_json())
    for where, collector_value in (
        (("column", "trailgear.Brand", "Name"), True),
        (("column", "trailgear.Brand", "Country"), False),
        (("statement", "0", "calls_per_second"), 1 / 86400),
        (("statement", "1", "calls_per_second"), 1 / 86400),
    ):
        assert _value(raw, where) is None, where
        assert _value(again, where) == collector_value, where
