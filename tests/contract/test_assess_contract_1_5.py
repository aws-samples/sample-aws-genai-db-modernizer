# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Contract 1.5 of the public ``assess()`` contract, and the 1.4 callers it keeps.

1.5 adds one optional result field, ``designs[].tables[].schema_name`` on an Aurora
design's table (#2478): the source schema a table is created in, when the design's
tables come from more than one schema, so ``sales.orders`` and ``archive.orders`` come
out as two tables a caller can tell apart. It is None for a design of one schema and for
a design saved before schema-qualified designs (#2455). The request is unchanged.

- 1.5 is read only when pinned until a release records it (the record is 1.4, written
  after v0.5.0-beta.1's tag), and is the default once one does;
- a 1.4 result leaves the field out, so a 1.4 caller's result validates against the
  schemas v0.5.0-beta.1 shipped (``tests/fixtures/assess_{request,result}_v1_4.schema.json``,
  the tag's bytes), and a 1.4 result that sets it is refused;
- a 1.5 result always writes it, null when the design names no schema.
"""

from __future__ import annotations

import hashlib
import json
import subprocess  # nosec B404 -- git show of this repository's own tag, fixed argv
import tempfile
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from scripts import render_contract_schemas as rcs
from scripts.contract_schema_snapshot import build_public_snapshot
from src.assessment import CONTRACT_VERSION, AssessmentRequest, AssessmentResult, assess, models
from src.assessment.models import (
    SUPPORTED_CONTRACT_VERSIONS,
    AuroraDesign,
    default_contract_version,
    pinned_only_contract_versions,
)
from src.assessment.result_builder import RunState, build_designs
from src.storage.local_store import LocalArtifactStore
from tests.assess_models import models_patched
from tests.released_record import recorded_as
from tests.unit.assessment.conftest import collection, sample_collection

REPO = Path(__file__).resolve().parents[2]
SHIPPED_TAG = "v0.5.0-beta.1"
_FIXTURES = REPO / "tests" / "fixtures"
#: The schemas v0.5.0-beta.1 shipped, copied byte for byte, with each file's SHA-256 (so
#: the copies are checked where the tag is not fetched).
SHIPPED_1_4 = {
    "assess_request.v1.schema.json": _FIXTURES / "assess_request_v1_4.schema.json",
    "assess_result.v1.schema.json": _FIXTURES / "assess_result_v1_4.schema.json",
}
SHIPPED_SHA256 = {
    "assess_request.v1.schema.json": "e7dfdb49bfaf4a9bc4f0dae8c2b566766930bb8624a843411a5fafab094a4a86",
    "assess_result.v1.schema.json": "af0f1d741440ab17aa917b75bad1513f11974df62ed9a1f2c4dfa4cd6025b250",
}
POSTGRESQL_SCOPE = ["aurora_postgresql", "dynamodb"]

#: An Aurora PostgreSQL design whose tables come from two schemas, each with an
#: ``orders`` table, as the generator writes it (#2455): each table names its schema.
TWO_SCHEMAS: dict[str, Any] = {
    "validation_passed": True,
    "table_definitions": [
        {"table_name": "orders", "schema_name": "sales", "primary_key": ["id"]},
        {
            "table_name": "orders",
            "schema_name": "archive",
            "primary_key": ["id"],
            "indexes": ['CREATE INDEX "o_day" ON "archive"."orders" ("day");'],
        },
        {"table_name": "customers", "schema_name": "sales", "primary_key": ["id"]},
    ],
}

#: A design of one schema: the generator writes no ``schema_name`` (#2455), as did every
#: release before it.
ONE_SCHEMA: dict[str, Any] = {
    "validation_passed": True,
    "table_definitions": [
        {"table_name": "orders", "primary_key": ["id"], "indexes": []},
        {"table_name": "customers", "primary_key": ["id"]},
    ],
}


def _request(version: str | None, run_through: str = "analysis") -> dict[str, Any]:
    request: dict[str, Any] = {
        "database_name": "wordpress",
        "collection": sample_collection("wordpress"),
        "options": {"run_through": run_through},
    }
    if version is not None:
        request["contract_version"] = version
    return request


@cache
def _result(version: str | None, run_through: str) -> str:
    return assess(_request(version, run_through)).model_dump_json()


@cache
def _designed(version: str) -> str:
    """A designed wordpress result (Aurora PostgreSQL and DynamoDB) for ``version``."""
    store = LocalArtifactStore(base_dir=tempfile.mkdtemp(prefix=f"assess-v{version}-"))
    with models_patched():
        result = assess(
            {
                "contract_version": version,
                "database_name": "wordpress",
                "collection": collection("wordpress"),
                "options": {
                    "run_through": "schema_design",
                    "deterministic": False,
                    "target_engines": POSTGRESQL_SCOPE,
                },
            },
            store=store,
            job_id=f"des{version.replace('.', '')}x",
        )
    return result.model_dump_json()


def _aurora(design: dict[str, Any], version: str) -> AuroraDesign:
    state = RunState(
        run_through="schema_design",
        collector={},
        triage={},
        schema_designs={"aurora_postgresql": design},
    )
    (out,) = build_designs(state, ["aurora_postgresql"], contract_version=version)
    assert isinstance(out, AuroraDesign)
    return out


def _shipped(name: str) -> Draft202012Validator:
    schema = json.loads(SHIPPED_1_4[name].read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _aurora_tables(document: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        t for d in document["designs"] if d["engine"].startswith("aurora_") for t in d["tables"]
    ]


@pytest.fixture
def recorded_1_4(tmp_path: Path) -> Iterator[None]:
    """The record v0.5.0-beta.1's release step wrote: it names 1.4."""
    with recorded_as("1.4", tmp_path, released=SHIPPED_TAG):
        yield


# --------------------------------------------------------------------------- the version


def test_1_5_is_read_only_when_pinned_until_a_release_records_it(recorded_1_4: None) -> None:
    assert CONTRACT_VERSION == SUPPORTED_CONTRACT_VERSIONS[-1] == "1.5"
    assert SUPPORTED_CONTRACT_VERSIONS == ("1.3", "1.4", "1.5")
    assert default_contract_version() == "1.4"
    assert pinned_only_contract_versions() == ("1.5",)
    assert AssessmentRequest.model_validate(_request(None)).contract_version == "1.4"
    assert AssessmentRequest.model_validate(_request("1.5")).contract_version == "1.5"
    with pytest.raises(ValueError) as caught:
        models.check_contract_version("1.6")
    assert str(caught.value).endswith(
        "this code reads contracts 1.3, 1.4 and 1.5 (read only when pinned)"
    )
    # The public shape is 1.5's: against the released 1.4 it is the MINOR rise.
    assert {e["version"] for e in build_public_snapshot().values()} == {"1.5"}


def test_once_a_release_records_1_5_it_is_the_default(tmp_path: Path) -> None:
    with recorded_as("1.5", tmp_path):
        assert default_contract_version() == "1.5"
        assert pinned_only_contract_versions() == ()
        assert assess(_request(None)).contract_version == "1.5"
        with pytest.raises(ValueError) as caught:
            models.check_contract_version("1.6")
        assert str(caught.value).endswith("this code reads contracts 1.3, 1.4 and 1.5")


@pytest.mark.parametrize("run_through", ["analysis", "reality_check"])
def test_a_1_5_request_gets_the_1_4_values_stamped_1_5(run_through: str) -> None:
    """1.5 changes only an Aurora design's tables: a result with no design is the 1.4
    one with its version."""
    old = json.loads(_result("1.4", run_through))
    new = json.loads(_result("1.5", run_through))
    assert old["contract_version"] == "1.4" and new["contract_version"] == "1.5"
    assert {**new, "contract_version": "1.4"} == old


# --------------------------------------------------------------------------- the schema


def test_a_two_schema_design_gives_each_table_with_its_schema() -> None:
    design = _aurora(TWO_SCHEMAS, "1.5")
    assert [(t.schema_name, t.name) for t in design.tables] == [
        ("sales", "orders"),
        ("archive", "orders"),
        ("sales", "customers"),
    ]
    # Each keeps its own keys and indexes: the two orders tables are not merged.
    assert design.tables[1].indexes == TWO_SCHEMAS["table_definitions"][1]["indexes"]
    assert design.tables[0].indexes == []


def _generated(engine: str, schemas: list[tuple[str | None, str]]) -> dict[str, Any]:
    """A design the Aurora design generator writes (``merge_design_delta``) for source
    tables of these ``(schema, table)`` pairs, each with a primary key and one index."""
    from src.contracts.schema_design_input import AgentColumn, AgentIndex, AgentTable
    from src.tools.schema.aurora_common.delta_merge import AuroraDesignBase, merge_design_delta

    tables = [
        AgentTable(
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
        )
        for schema, name in schemas
    ]
    base = AuroraDesignBase(
        engine=engine, job_id="job1", source_database="shop", source_engine="postgresql",
        tables=tables,
    )  # fmt: skip
    merged = merge_design_delta(base, {"delta_version": "1.0"})
    assert merged.errors == [] and merged.output is not None
    return dict(merged.output)


GENERATED_PAIRS: list[tuple[str | None, str]] = [
    ("sales", "orders"),
    ("archive", "orders"),
    ("sales", "customers"),
]


@pytest.mark.parametrize("engine", ["aurora_postgresql", "aurora_mysql"])
def test_a_design_the_generator_writes_for_two_schemas_gives_each_table_its_schema(
    engine: str,
) -> None:
    """The design generator's own output, not a hand-written one: each table carries its
    schema in 1.5 with its own schema-qualified index, and none in 1.4."""
    design = _generated(engine, GENERATED_PAIRS)
    state = RunState(
        run_through="schema_design", collector={}, triage={}, schema_designs={engine: design}
    )
    (entry,) = build_designs(state, [engine], contract_version="1.5")
    assert isinstance(entry, AuroraDesign)
    assert [(t.schema_name, t.name) for t in entry.tables] == GENERATED_PAIRS
    quote = '"' if engine == "aurora_postgresql" else "`"
    for table in entry.tables:
        (index,) = table.indexes
        assert f"{quote}{table.schema_name}{quote}.{quote}{table.name}{quote}" in index
    (old,) = build_designs(state, [engine], contract_version="1.4")
    assert isinstance(old, AuroraDesign)
    assert [t.schema_name for t in old.tables] == [None, None, None]
    # A design of one schema: the generator names no schema, so 1.5 gives null.
    one = _generated(engine, [("public", "orders"), ("public", "customers")])
    one_state = RunState(
        run_through="schema_design", collector={}, triage={}, schema_designs={engine: one}
    )
    (single,) = build_designs(one_state, [engine], contract_version="1.5")
    assert isinstance(single, AuroraDesign)
    assert [(t.schema_name, t.name) for t in single.tables] == [
        (None, "orders"),
        (None, "customers"),
    ]


def test_a_one_schema_design_keeps_its_tables_with_no_schema() -> None:
    design = _aurora(ONE_SCHEMA, "1.5")
    assert [t.model_dump() for t in design.tables] == [
        {"name": "orders", "primary_key": ["id"], "indexes": [], "schema_name": None},
        {"name": "customers", "primary_key": ["id"], "indexes": [], "schema_name": None},
    ]
    # The same tables as the 1.4 entry, which has no schema either.
    assert design.tables == _aurora(ONE_SCHEMA, "1.4").tables


@pytest.mark.parametrize("blank", [None, "", 7, ["sales"]])
def test_a_schema_that_is_not_a_name_reads_as_none(blank: Any) -> None:
    table = {"table_name": "orders", "schema_name": blank}
    (out,) = _aurora({"table_definitions": [table]}, "1.5").tables
    assert out.schema_name is None


@pytest.mark.parametrize("version", ["1.3", "1.4"])
def test_a_1_5_result_writes_the_schema_and_a_1_4_result_leaves_it_out(version: str) -> None:
    new = json.loads(_designed("1.5"))
    old = json.loads(_designed(version))
    assert new["contract_version"] == "1.5" and old["contract_version"] == version
    # The sample's design is of one schema: 1.5 writes null, 1.4 writes nothing.
    assert _aurora_tables(new) and all(t["schema_name"] is None for t in _aurora_tables(new))
    assert all("schema_name" not in t for t in _aurora_tables(old))
    # Apart from that key the two are the same tables.
    stripped = [{k: v for k, v in t.items() if k != "schema_name"} for t in _aurora_tables(new)]
    assert stripped == _aurora_tables(old)
    # Only the result document is version-shaped: a table dumped on its own has the key.
    read = AssessmentResult.model_validate_json(_designed(version))
    aurora = [d for d in read.designs or [] if isinstance(d, AuroraDesign)]
    assert aurora and aurora[0].tables[0].model_dump()["schema_name"] is None
    # Each reads back as written.
    for text in (_designed("1.5"), _designed(version)):
        assert json.loads(AssessmentResult.model_validate_json(text).model_dump_json()) == (
            json.loads(text)
        )


@pytest.mark.parametrize("version", ["1.3", "1.4"])
def test_a_1_4_run_never_sets_the_schema(version: str) -> None:
    design = _aurora(TWO_SCHEMAS, version)
    assert all("schema_name" not in t.model_fields_set for t in design.tables)
    assert [(t.schema_name, t.name) for t in design.tables] == [
        (None, "orders"),
        (None, "orders"),
        (None, "customers"),
    ]


@pytest.mark.parametrize("value", ["sales", None, ""], ids=["named", "null", "empty"])
@pytest.mark.parametrize("version", ["1.3", "1.4"])
def test_a_1_3_or_1_4_result_that_carries_a_schema_is_refused(version: str, value: Any) -> None:
    """Any ``schema_name`` key, null or empty included: a 1.3 or 1.4 result never has one
    (the schemas those versions shipped refuse the key)."""
    document = json.loads(_designed("1.5"))
    _aurora_tables(document)[0]["schema_name"] = value
    AssessmentResult.model_validate(document)
    document["contract_version"] = version
    with pytest.raises(ValidationError, match=r"schema_name is a field of contract 1\.5"):
        AssessmentResult.model_validate(document)
    # Without the key, the same document reads back as that version's result.
    for table in _aurora_tables(document):
        del table["schema_name"]
    assert AssessmentResult.model_validate(document).contract_version == version


def test_a_1_5_result_with_two_schemas_reads_back_with_both() -> None:
    document = json.loads(_designed("1.5"))
    (aurora,) = [d for d in document["designs"] if d["engine"] == "aurora_postgresql"]
    aurora["tables"] = [t.model_dump(mode="json") for t in _aurora(TWO_SCHEMAS, "1.5").tables]
    result = AssessmentResult.model_validate(document)
    again = json.loads(result.model_dump_json())
    (read,) = [d for d in again["designs"] if d["engine"] == "aurora_postgresql"]
    assert [(t["schema_name"], t["name"]) for t in read["tables"]] == [
        ("sales", "orders"),
        ("archive", "orders"),
        ("sales", "customers"),
    ]
    Draft202012Validator(json.loads(rcs.schema_path("assess_result").read_text("utf-8"))).validate(
        again
    )


# --------------------------------------------------------------------------- 1.4 callers


@pytest.mark.passes_on_main
@pytest.mark.parametrize("run_through", ["analysis", "reality_check"])
def test_a_1_4_caller_validates_with_the_schemas_1_4_shipped(run_through: str) -> None:
    _shipped("assess_request.v1.schema.json").validate(_request("1.4", run_through))
    _shipped("assess_result.v1.schema.json").validate(json.loads(_result("1.4", run_through)))


def test_a_1_4_designed_result_validates_with_the_schema_1_4_shipped() -> None:
    validator = _shipped("assess_result.v1.schema.json")
    validator.validate(json.loads(_designed("1.4")))
    # Why a 1.4 result leaves it out: the shipped schema refuses the key, even null.
    written = {**json.loads(_designed("1.5")), "contract_version": "1.4"}
    assert not validator.is_valid(written)
    for table in _aurora_tables(written):
        del table["schema_name"]
    validator.validate(written)


@pytest.mark.passes_on_main
@pytest.mark.parametrize("name", sorted(SHIPPED_1_4))
def test_the_shipped_1_4_schemas_are_the_tags_bytes(name: str) -> None:
    data = SHIPPED_1_4[name].read_bytes()
    assert hashlib.sha256(data).hexdigest() == SHIPPED_SHA256[name]
    shown = subprocess.run(  # nosec B603 B607 -- fixed git argv on this repository
        ["git", "-C", str(REPO), "show", f"{SHIPPED_TAG}:docs/contracts/schemas/{name}"],
        capture_output=True,
        check=False,
    )
    if shown.returncode == 0:  # where the tag is fetched, the bytes are the tag's own
        assert shown.stdout == data
    assert json.loads(data)["x-contract-version"] == "1.4"


def test_the_published_schemas_read_every_version_and_document_the_schema() -> None:
    request = json.loads(rcs.schema_path("assess_request").read_text(encoding="utf-8"))
    result = json.loads(rcs.schema_path("assess_result").read_text(encoding="utf-8"))
    for schema in (request, result):
        assert schema["x-contract-version"] == "1.5"
        assert schema["properties"]["contract_version"]["enum"] == ["1.3", "1.4", "1.5"]
        assert "default" not in schema["properties"]["contract_version"]
    table = result["$defs"]["AuroraTable"]
    assert "schema_name" in table["properties"]
    # Optional in the schema: a 1.3 or 1.4 result leaves it out.
    assert "schema_name" not in table["required"]
    assert {"name", "primary_key", "indexes"} <= set(table["required"])
    Draft202012Validator(result).validate(json.loads(_designed("1.4")))
    Draft202012Validator(result).validate(json.loads(_designed("1.5")))
