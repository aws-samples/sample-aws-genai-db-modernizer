# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A saved Aurora design's column types pass the DDL type allowlist on every load (#2155).

Generation checks each column type with ``validate_aurora_type``; a saved
``schema-aurora_*/[v<N>/]schema_output.json`` is the other way in. Every store read
(``check_artifact``: synthesis, the assess() result, revisions, the data movement step,
the graph, ATX), an uploaded contract (``read_contract_payload``), the contract model
itself (and so the API that feeds the UI, whose 409 is tested in
``tests/unit/api/test_contract_version_refusal_routes.py``) run the same allowlist for the design's own engine,
and refuse the design, naming no collector text, when one type fails it.
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from src.contracts.aurora_design_delta import validate_aurora_type
from src.contracts.aurora_mysql_model_output import AuroraMySQLModelOutputContract
from src.contracts.aurora_postgresql_model_output import AuroraPostgresqlModelOutputContract
from src.shared.contract_versioning import ContractVersionError, read_contract_payload
from src.storage.local_store import LocalArtifactStore
from src.tools.schema.aurora_common.ddl_generator import render_create_table
from src.tools.schema.aurora_common.saved_design import DIALECTS

REPO = Path(__file__).resolve().parents[2]
SAVED = (
    REPO / "tests/fixtures/real_runs/wordpress-both/job/schema-aurora_mysql/v2/schema_output.json"
)
DB, JOB = "wordpress", "job1"
INJECTED = "BIGINT); DROP TABLE users; --"
MODELS: dict[str, type[BaseModel]] = {
    "aurora_mysql": AuroraMySQLModelOutputContract,
    "aurora_postgresql": AuroraPostgresqlModelOutputContract,
}


def _ddl(engine: str, types: tuple[str, ...]) -> str:
    """The script the generator writes for ``types`` (#2199 rebuilds it on every read)."""
    dialect = DIALECTS[engine]

    def written(t: str) -> str:
        try:
            return validate_aurora_type(t, engine)
        except ValueError:
            return t  # refused by the column type check before the script is read

    fragments = [f"{dialect.q(f'c{i}')} {written(t)}" for i, t in enumerate(types)]
    return render_create_table(dialect, "users", fragments, [("c0", None)])


def _design(engine: str, *types: str) -> dict:
    return {
        "contract_version": "1.1",
        "job_id": JOB,
        "source_database": DB,
        "target_engine": engine,
        "migration_strategy": "carry_over",
        "table_definitions": [
            {
                "table_name": "users",
                "primary_key": ["c0"],
                "columns": [
                    {"name": f"c{i}", "aurora_type": t, "script_derived": True}
                    for i, t in enumerate(types)
                ],
            }
        ],
        "generated_ddl": _ddl(engine, types),
        "trade_offs": [{"description": "Carry over", "impact": "Same shape."}],
        "validation_passed": True,
    }


def _store(tmp_path: Path, engine: str, design: dict, rel: str = "v1/schema_output.json"):
    path = tmp_path / DB / JOB / f"schema-{engine}" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(design), encoding="utf-8")
    return LocalArtifactStore(str(tmp_path)), f"{DB}/{JOB}/schema-{engine}/{rel}"


def _types(design: dict) -> list[str]:
    return [c["aurora_type"] for t in design["table_definitions"] for c in t["columns"]]


# --- An old saved design that is valid still loads -------------------------------------


@pytest.mark.passes_on_main
def test_a_saved_design_from_an_older_release_loads_unchanged(tmp_path: Path) -> None:
    """The wordpress run's saved Aurora MySQL design (contract 1.0) reads as written."""
    saved = json.loads(SAVED.read_text(encoding="utf-8"))
    target = tmp_path / DB / JOB / "schema-aurora_mysql" / "v2" / "schema_output.json"
    target.parent.mkdir(parents=True)
    shutil.copy(SAVED, target)
    store = LocalArtifactStore(str(tmp_path))
    loaded = store.read_json(f"{DB}/{JOB}/schema-aurora_mysql/v2/schema_output.json")
    assert loaded == saved
    assert AuroraMySQLModelOutputContract.model_validate(loaded).table_definitions


# --- Every load path refuses a type the allowlist refuses -------------------------------

_REFUSED = [
    ("aurora_mysql", INJECTED),
    ("aurora_postgresql", INJECTED),
    ("aurora_mysql", "INT, `evil` TEXT"),
    ("aurora_postgresql", "pg_sleep(10)"),
    ("aurora_mysql", "INT NOT NULL"),
    # Unknown type names
    ("aurora_mysql", "FOOTYPE"),
    ("aurora_postgresql", "NVARCHAR2(10)"),
    # Legal on the other engine only: the design's own engine decides
    ("aurora_mysql", "JSONB"),
    ("aurora_mysql", "TEXT[]"),
    ("aurora_postgresql", "ENUM('a','b')"),
    ("aurora_postgresql", "MEDIUMTEXT"),
    ("aurora_mysql", '"public"."mood"'),
    # Aurora MySQL's DECIMAL limits
    ("aurora_mysql", "DECIMAL(66,0)"),
    # Blank
    ("aurora_postgresql", "   "),
]


@pytest.mark.parametrize(("engine", "bad"), _REFUSED)
@pytest.mark.parametrize("rel", ["v1/schema_output.json", "schema_output.json"])
def test_a_store_read_refuses_a_saved_type_outside_the_allowlist(
    tmp_path: Path, engine: str, bad: str, rel: str
) -> None:
    store, key = _store(tmp_path, engine, _design(engine, "BIGINT", bad), rel)
    with pytest.raises(ContractVersionError) as caught:
        store.read_json(key)
    exc = caught.value
    assert type(exc).__name__ == "UnsafeArtifactError"
    # The log detail names the file and the column's position, never the whole value.
    assert key in str(exc)
    assert "table_definitions[0].columns[1].aurora_type" in str(exc)
    # The user is told what is wrong in plain words, with no path and no file content.
    message = exc.public_message
    assert "column type" in message
    assert not bad.strip() or bad.strip() not in message
    assert "/" not in message and "schema_output" not in message


@pytest.mark.parametrize("bad", [None, 7, ["BIGINT"], {"t": "BIGINT"}])
def test_a_saved_type_that_is_not_a_string_is_refused(tmp_path: Path, bad: object) -> None:
    design = _design("aurora_postgresql", "BIGINT")
    design["table_definitions"][0]["columns"].append(
        {"name": "x", "aurora_type": bad, "script_derived": True}
    )
    store, key = _store(tmp_path, "aurora_postgresql", design)
    with pytest.raises(ContractVersionError):
        store.read_json(key)


def test_an_injected_type_in_a_later_table_is_refused(tmp_path: Path) -> None:
    design = _design("aurora_mysql", "BIGINT")
    design["table_definitions"].append(copy.deepcopy(design["table_definitions"][0]))
    design["table_definitions"][1]["columns"][0]["aurora_type"] = INJECTED
    store, key = _store(tmp_path, "aurora_mysql", design)
    with pytest.raises(ContractVersionError, match=r"table_definitions\[1\]\.columns\[0\]"):
        store.read_json(key)


def test_the_wordpress_saved_design_with_one_injected_type_is_refused(tmp_path: Path) -> None:
    """The issue's reproduction, on the real saved file."""
    saved = json.loads(SAVED.read_text(encoding="utf-8"))
    saved["table_definitions"][3]["columns"][2]["aurora_type"] = INJECTED
    store, key = _store(tmp_path, "aurora_mysql", saved, "v2/schema_output.json")
    with pytest.raises(ContractVersionError):
        store.read_json(key)


@pytest.mark.parametrize("engine", sorted(MODELS))
def test_an_uploaded_design_is_refused_like_a_stored_one(engine: str) -> None:
    """ATX and the CLI read an uploaded contract through ``read_contract_payload``."""
    with pytest.raises(ContractVersionError):
        read_contract_payload(
            _design(engine, INJECTED), MODELS[engine], artifact=f"{DB}/{JOB} upload"
        )


@pytest.mark.parametrize(("engine", "bad"), _REFUSED)
def test_the_contract_model_refuses_a_type_outside_the_allowlist(engine: str, bad: str) -> None:
    with pytest.raises(ValidationError, match="aurora_type"):
        MODELS[engine].model_validate(_design(engine, "BIGINT", bad))


# --- Legal types in odd case or spacing load, normalized as generation does ----------

_LEGAL = [
    ("aurora_mysql", "  bigint   unsigned ", "bigint unsigned"),
    ("aurora_mysql", "VarChar( 255 )", "VarChar( 255 )"),
    ("aurora_mysql", "ENUM('a','b')", "ENUM('a','b')"),
    ("aurora_postgresql", "double\tprecision", "double precision"),
    ("aurora_postgresql", "Timestamp  With Time Zone", "Timestamp With Time Zone"),
    ("aurora_postgresql", "text[]", "text[]"),
    ("aurora_postgresql", '"public"."mood"', '"public"."mood"'),
]


@pytest.mark.parametrize(("engine", "raw", "normalized"), _LEGAL)
def test_a_legal_type_in_odd_case_or_spacing_loads_normalized(
    tmp_path: Path, engine: str, raw: str, normalized: str
) -> None:
    store, key = _store(tmp_path, engine, _design(engine, "BIGINT", raw))
    assert _types(store.read_json(key)) == ["BIGINT", normalized]
    model = MODELS[engine].model_validate(_design(engine, "BIGINT", raw))
    assert _types(model.model_dump()) == ["BIGINT", normalized]


@pytest.mark.passes_on_main
def test_a_skipped_placeholder_and_another_engines_design_are_not_checked(tmp_path: Path) -> None:
    """Only an Aurora design carries Aurora column types."""
    skipped = {"status": "skipped", "reason": "no queries", "target_type": "aurora_mysql"}
    store, key = _store(tmp_path, "aurora_mysql", skipped)
    assert store.read_json(key)["status"] == "skipped"
    other = {"contract_version": "1.0", "table_definitions": [{"table_name": "t", "gsis": []}]}
    store, key = _store(tmp_path, "dynamodb", other)
    assert store.read_json(key) == other


def test_aurora_column_types_in_another_engines_folder_are_checked(tmp_path: Path) -> None:
    """#2199: Aurora column types in another engine's folder are refused, whatever engine
    the file names (round 5: that folder allows no Aurora-only field; the #2155 version of
    this test read them unchecked)."""
    other = {"contract_version": "1.0", "table_definitions": [{"columns": [{"aurora_type": "x;"}]}]}
    store, key = _store(tmp_path, "dynamodb", other)
    with pytest.raises(ContractVersionError, match="Aurora DDL content"):
        store.read_json(key)
    store, key = _store(tmp_path, "dynamodb", {**other, "target_engine": "aurora_mysql"})
    with pytest.raises(ContractVersionError, match="Aurora DDL content"):
        store.read_json(key)
