"""The stable design block of a designed ``assess()`` result (#1493).

``designs`` carries, per designed engine, the target tables, keys and indexes a caller
builds on, copied from the engine's schema design; the full designs stay in
``artifacts``. Tests never call a model: the designer writes the fixture designs of
``tests/assess_models.py`` (the designed wordpress job's, plus a global secondary index
and DocumentDB, OpenSearch and Aurora PostgreSQL designs).
"""

from __future__ import annotations

import json
import tempfile
from functools import cache
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from scripts import render_contract_schemas as rcs
from src.assessment import AssessmentResult, assess
from src.assessment.models import (
    AuroraDesign,
    DocumentDBDesign,
    DynamoDBDesign,
    ElastiCacheDesign,
    EngineDesign,
)
from src.assessment.result_builder import RunState, _design_lines, build_designs
from src.storage.local_store import LocalArtifactStore
from tests.assess_models import FAKE_GSI, models_patched
from tests.unit.assessment.conftest import REPO, assessed, collection

DB = "wordpress"
OPENSEARCH_SCOPE = ("dynamodb", "opensearch")
POSTGRESQL_SCOPE = ("aurora_postgresql", "dynamodb")


@cache
def _designed(
    job_id: str, target_engines: tuple[str, ...] | None = None
) -> tuple[str, LocalArtifactStore]:
    store = LocalArtifactStore(base_dir=tempfile.mkdtemp(prefix=f"assess-{job_id}-"))
    options: dict[str, Any] = {"run_through": "schema_design", "deterministic": False}
    if target_engines:
        options["target_engines"] = list(target_engines)
    with models_patched():
        result = assess(
            {"database_name": DB, "collection": collection(DB), "options": options},
            store=store,
            job_id=job_id,
        )
    return result.model_dump_json(), store


def designed(
    job_id: str = "design01", target_engines: tuple[str, ...] | None = None
) -> tuple[AssessmentResult, LocalArtifactStore]:
    text, store = _designed(job_id, target_engines)
    return AssessmentResult.model_validate_json(text), store


def _schema_output(
    store: LocalArtifactStore, result: AssessmentResult, engine: str, job_id: str = "design01"
) -> Any:
    """The schema output the designer wrote, as the run read it."""
    version = result.summary.assignment_version
    return store.read_json(f"{DB}/{job_id}/schema-{engine}/v{version}/schema_output.json")


def _design(result: AssessmentResult, engine: str) -> Any:
    assert result.designs is not None
    matches = [d for d in result.designs if d.engine == engine]
    assert len(matches) == 1, engine
    return matches[0]


def _served(patterns: list[dict[str, Any]], **where: Any) -> list[str]:
    ids: dict[str, None] = {}
    for p in patterns:
        if p.get("in_scope", True) and all(p.get(k) == v for k, v in where.items()):
            for q in p.get("query_ids") or p.get("source_query_ids") or []:
                ids.setdefault(q, None)
    return list(ids)


# --------------------------------------------------------------------------- per engine


def test_a_designed_result_has_one_design_per_designed_engine_in_order() -> None:
    result, _ = designed()
    assert result.label == "designed"
    assert result.designs is not None
    assert [d.engine for d in result.designs] == result.summary.designed_engines
    assert result.summary.designed_engines == ["aurora_mysql", "dynamodb", "elasticache"]


def test_the_dynamodb_design_copies_tables_keys_gsis_and_statements() -> None:
    result, store = designed()
    source = _schema_output(store, result, "dynamodb")
    design = _design(result, "dynamodb")
    assert isinstance(design, DynamoDBDesign)
    assert design.validation_passed is source["validation_passed"] is True
    assert [t.name for t in design.tables] == [t["table_name"] for t in source["table_definitions"]]
    patterns = source["access_patterns"]
    for table, src in zip(design.tables, source["table_definitions"], strict=True):
        assert table.source_tables == src["source_tables"]
        assert table.partition_key.model_dump() == {
            "name": src["partition_key"]["attribute_name"],
            "type": src["partition_key"]["attribute_type"],
        }
        if src["sort_key"] is None:
            assert table.sort_key is None
        else:
            assert table.sort_key is not None
            assert table.sort_key.name == src["sort_key"]["attribute_name"]
        assert table.query_ids == _served(patterns, table_name=table.name, gsi_name=None)
    with_gsi = [t for t in design.tables if t.gsis]
    assert len(with_gsi) == 1
    gsi = with_gsi[0].gsis[0]
    assert gsi.model_dump() == {
        "name": FAKE_GSI["gsi_name"],
        "partition_key": [{"name": "status", "type": "S"}],
        "sort_key": [{"name": "updated_at", "type": "S"}],
        "projection": "INCLUDE",
        "projected_attributes": ["title"],
        "query_ids": patterns[0]["query_ids"],
    }
    # A statement served from the index is not listed on the table itself.
    assert not set(gsi.query_ids) & set(with_gsi[0].query_ids)


def test_the_aurora_design_copies_tables_indexes_and_points_at_the_ddl() -> None:
    result, store = designed()
    source = _schema_output(store, result, "aurora_mysql")
    design = _design(result, "aurora_mysql")
    assert isinstance(design, AuroraDesign)
    assert design.validation_passed is True
    assert [t.model_dump() for t in design.tables] == [
        {"name": t["table_name"], "primary_key": t["primary_key"], "indexes": t["indexes"]}
        for t in source["table_definitions"]
    ]
    assert design.ddl_artifact_path == "artifacts.schema_designs.aurora_mysql.generated_ddl"


def test_the_aurora_ddl_pointer_resolves_in_the_artifacts() -> None:
    store = LocalArtifactStore(base_dir=tempfile.mkdtemp(prefix="assess-ddl-"))
    options = {"run_through": "schema_design", "deterministic": False, "include_artifacts": True}
    with models_patched():
        result = assess(
            {"database_name": DB, "collection": collection(DB), "options": options},
            store=store,
            job_id="design03",
        )
    design = _design(result, "aurora_mysql")
    node: Any = json.loads(result.model_dump_json())
    for part in design.ddl_artifact_path.split("."):
        node = node[part]
    assert isinstance(node, str) and node.startswith("CREATE TABLE")


def test_the_elasticache_design_copies_key_patterns_ttls_and_statements() -> None:
    result, store = designed()
    source = _schema_output(store, result, "elasticache")
    design = _design(result, "elasticache")
    assert isinstance(design, ElastiCacheDesign)
    assert design.validation_passed is True
    assert [k.model_dump() for k in design.keys] == [
        {
            "pattern": k["key_pattern"],
            "data_type": k["data_type"],
            "ttl_seconds": k["ttl_seconds"],
            "source_tables": k["source_tables"],
            "query_ids": _served(source["access_patterns"], key_pattern=k["key_pattern"]),
        }
        for k in source["key_designs"]
    ]
    assert design.keys[0].query_ids


def test_the_documentdb_design_copies_collections_embeds_and_indexes() -> None:
    result, store = designed("design02", ("aurora_mysql", "documentdb"))
    assert result.summary.designed_engines == ["aurora_mysql", "documentdb"]
    source = _schema_output(store, result, "documentdb", "design02")
    design = _design(result, "documentdb")
    assert isinstance(design, DocumentDBDesign)
    assert design.validation_passed is True
    assert [c.name for c in design.collections] == [
        c["collection_name"] for c in source["collections"]
    ]
    first, src = design.collections[0], source["collections"][0]
    assert first.source_tables == src["source_tables"]
    assert [e.model_dump() for e in first.embedded] == [
        {"source_table": e["source_table"], "path": e["embed_path"], "strategy": e["strategy"]}
        for e in src["embedded_entities"]
    ]
    assert first.embedded and first.referenced_collections == src["referenced_collections"]
    index = first.indexes[0]
    # Key order is the index's: a list, so it survives any JSON reader.
    assert [(k.field, k.direction) for k in index.keys] == [("status", 1), ("updated_at", -1)]
    # A null index type is the contract's compound.
    assert index.type == "compound"
    assert index.query_ids == src["indexes"][0]["source_query_ids"]
    for collection_, c in zip(design.collections, source["collections"], strict=True):
        assert collection_.query_ids == _served(
            source["access_patterns"], collection_name=c["collection_name"]
        )
    assert any(c.query_ids for c in design.collections)


def test_the_opensearch_design_copies_indexes_fields_aliases_and_statements() -> None:
    from src.assessment.models import OpenSearchDesign

    result, store = designed("design04", OPENSEARCH_SCOPE)
    assert result.summary.designed_engines == ["dynamodb", "opensearch"]
    source = _schema_output(store, result, "opensearch", "design04")
    design = _design(result, "opensearch")
    assert isinstance(design, OpenSearchDesign)
    assert design.validation_passed is source["validation_passed"] is True
    patterns = source["access_patterns"]
    assert [i.model_dump() for i in design.indexes] == [
        {
            "name": i["index_name"],
            "source_tables": i["source_tables"],
            "fields": [
                {"name": f["field_name"], "type": f["field_type"]} for f in i["field_mappings"]
            ],
            "aliases": i["aliases"],
            "query_ids": _served(patterns, index_or_stream=i["index_name"]),
        }
        for i in source["index_designs"]
    ]
    assert [s.model_dump() for s in design.data_streams] == [
        {
            "name": s["data_stream_name"],
            "source_tables": s["source_tables"],
            "fields": [
                {"name": f["field_name"], "type": f["field_type"]}
                for f in s["index_template"]["field_mappings"]
            ],
            "query_ids": _served(patterns, index_or_stream=s["data_stream_name"]),
        }
        for s in source["data_stream_designs"]
    ]
    # The fixture reaches every part: an alias, a mapped field and a served statement on
    # an index, and a data stream with its @timestamp field and a served statement.
    assert any(i.aliases and i.fields and i.query_ids for i in design.indexes)
    assert any(s.query_ids and s.fields[0].name == "@timestamp" for s in design.data_streams)


@pytest.mark.passes_on_main
def test_the_aurora_postgresql_design_copies_tables_indexes_and_points_at_the_ddl() -> None:
    result, store = designed("design05", POSTGRESQL_SCOPE)
    assert result.summary.designed_engines == ["aurora_postgresql", "dynamodb"]
    source = _schema_output(store, result, "aurora_postgresql", "design05")
    design = _design(result, "aurora_postgresql")
    assert isinstance(design, AuroraDesign)
    assert design.validation_passed is True
    assert [t.model_dump() for t in design.tables] == [
        {"name": t["table_name"], "primary_key": t["primary_key"], "indexes": t["indexes"]}
        for t in source["table_definitions"]
    ]
    assert design.ddl_artifact_path == "artifacts.schema_designs.aurora_postgresql.generated_ddl"


@pytest.mark.passes_on_main
def test_a_run_with_no_opensearch_design_has_no_opensearch_entry() -> None:
    """A designed result with no OpenSearch design (the placement keeps no OpenSearch
    statement, or its designer had nothing to design) has no ``opensearch`` entry, as
    before the entry existed."""
    result, _ = designed()
    assert "opensearch" not in result.summary.designed_engines
    assert result.designs is not None
    assert "opensearch" not in {d.engine for d in result.designs}
    state = _state(
        {
            "opensearch": {"status": "skipped", "target_type": "opensearch"},
            "elasticache": {"validation_passed": True, "key_designs": []},
        }
    )
    designed_engines = _design_lines(state)["designed_engines"]
    assert designed_engines == ["elasticache"]
    assert [d.engine for d in build_designs(state, designed_engines)] == ["elasticache"]


# --------------------------------------------------------------------------- phases


@pytest.mark.parametrize("run_through", ["analysis", "reality_check"])
def test_no_design_block_before_schema_design(run_through: str) -> None:
    result = assessed(DB, run_through)
    assert result.designs is None
    assert json.loads(result.model_dump_json())["designs"] is None


def test_a_model_phase_with_nothing_to_design_has_no_design_block() -> None:
    options = {
        "run_through": "schema_design",
        "deterministic": False,
        "target_engines": ["elasticache"],
    }
    with models_patched():
        result = assess({"database_name": DB, "collection": collection(DB), "options": options})
    assert result.label == "recommendation"
    assert result.designs is None


def test_designs_is_set_exactly_when_the_result_is_designed() -> None:
    result, _ = designed()
    document = json.loads(result.model_dump_json())
    document["designs"] = None
    with pytest.raises(ValidationError, match="designs is set exactly when"):
        AssessmentResult.model_validate(document)
    placed = json.loads(assessed(DB, "reality_check").model_dump_json())
    placed["designs"] = []
    with pytest.raises(ValidationError, match="designs is set exactly when"):
        AssessmentResult.model_validate(placed)


# --------------------------------------------------------------------------- schema


@pytest.mark.parametrize(
    ("job_id", "scope"), [("design01", None), ("design02", ("aurora_mysql", "documentdb"))]
)
def test_a_designed_result_round_trips_through_the_json_schema(
    job_id: str, scope: tuple[str, ...] | None
) -> None:
    text, _ = _designed(job_id, scope)
    schema = json.loads(rcs.schema_path("assess_result").read_text("utf-8"))
    document = json.loads(text)
    assert document["designs"]
    Draft202012Validator(schema).validate(document)
    assert AssessmentResult.model_validate_json(text).model_dump_json() == text


def test_the_schema_refuses_an_unknown_engine_or_field_in_a_design() -> None:
    schema = json.loads(rcs.schema_path("assess_result").read_text("utf-8"))
    text, _ = _designed("design01", None)
    for change in (
        lambda d: d["designs"][0].update(engine="neptune"),
        lambda d: d["designs"][1]["tables"][0].update(colour="blue"),
        lambda d: d["designs"][1]["tables"][0]["partition_key"].update(type="string"),
    ):
        document = json.loads(text)
        change(document)
        assert list(Draft202012Validator(schema).iter_errors(document))


# --------------------------------------------------------------------------- partial designs


def _state(designs: dict[str, dict[str, Any]]) -> RunState:
    return RunState(run_through="schema_design", collector={}, triage={}, schema_designs=designs)


def test_a_partial_design_reads_its_missing_optional_fields_as_empty() -> None:
    """An older or partial design (no GSIs, sort keys, access patterns, TTLs, index
    types or validation result) still gives a stable entry; the missing parts are empty."""
    designs: dict[str, dict[str, Any]] = {
        "dynamodb": {
            "table_definitions": [
                {
                    "table_name": "Orders",
                    "partition_key": {"attribute_name": "id", "attribute_type": "N"},
                },
                {"table_name": "NoKey"},
            ],
        },
        "aurora_postgresql": {"table_definitions": [{"table_name": "orders"}]},
        "documentdb": {
            "collections": [
                {"collection_name": "orders", "indexes": [{"index_name": "idx", "keys": {"a": 1}}]}
            ]
        },
        "elasticache": {"key_designs": [{"key_pattern": "order:{id}", "data_type": "string"}]},
    }
    out = build_designs(_state(designs), list(designs))
    as_json = [d.model_dump(mode="json") for d in out]
    assert as_json == [
        {
            "validation_passed": False,
            "engine": "dynamodb",
            "tables": [
                {
                    "name": "Orders",
                    "source_tables": [],
                    "partition_key": {"name": "id", "type": "N"},
                    "sort_key": None,
                    "gsis": [],
                    "query_ids": [],
                }
            ],
        },
        {
            "validation_passed": False,
            "engine": "aurora_postgresql",
            "tables": [{"name": "orders", "primary_key": [], "indexes": []}],
            "ddl_artifact_path": "artifacts.schema_designs.aurora_postgresql.generated_ddl",
        },
        {
            "validation_passed": False,
            "engine": "documentdb",
            "collections": [
                {
                    "name": "orders",
                    "source_tables": [],
                    "embedded": [],
                    "referenced_collections": [],
                    "indexes": [
                        {
                            "name": "idx",
                            "keys": [{"field": "a", "direction": 1}],
                            "type": "compound",
                            "query_ids": [],
                        }
                    ],
                    "query_ids": [],
                }
            ],
        },
        {
            "validation_passed": False,
            "engine": "elasticache",
            "keys": [
                {
                    "pattern": "order:{id}",
                    "data_type": "string",
                    "ttl_seconds": None,
                    "source_tables": [],
                    "query_ids": [],
                }
            ],
        },
    ]
    # Each entry validates as the discriminated union reads it back.
    adapter: TypeAdapter[list[EngineDesign]] = TypeAdapter(list[EngineDesign])
    assert [d.model_dump() for d in adapter.validate_python(as_json)] == [
        d.model_dump() for d in out
    ]


def test_a_failed_design_is_kept_with_its_validation_result() -> None:
    failed = {
        "validation_passed": False,
        "validation_failures": ["names a statement outside its scope"],
        "key_designs": [{"key_pattern": "k", "data_type": "string", "ttl_seconds": 60}],
    }
    (design,) = build_designs(_state({"elasticache": failed}), ["elasticache"])
    assert design.validation_passed is False
    assert isinstance(design, ElastiCacheDesign) and design.keys[0].ttl_seconds == 60


def test_out_of_scope_access_patterns_serve_no_table() -> None:
    dynamodb = {
        "validation_passed": True,
        "table_definitions": [
            {"table_name": "T", "partition_key": {"attribute_name": "id", "attribute_type": "S"}}
        ],
        "access_patterns": [
            {"table_name": "T", "gsi_name": None, "query_ids": ["q1", "q2"]},
            {"table_name": "T", "gsi_name": None, "query_ids": ["q2", "q3"], "in_scope": True},
            {"table_name": "T", "gsi_name": None, "query_ids": ["q4"], "in_scope": False},
        ],
    }
    (design,) = build_designs(_state({"dynamodb": dynamodb}), ["dynamodb"])
    assert isinstance(design, DynamoDBDesign)
    assert design.tables[0].query_ids == ["q1", "q2", "q3"]


def test_only_designed_engines_are_listed_in_their_order() -> None:
    """An engine missing from ``designed_engines`` (a skipped placeholder) has no entry;
    every designed engine, OpenSearch included, has one, in ``designed_engines``' order."""
    designs: dict[str, dict[str, Any]] = {
        "elasticache": {"validation_passed": True, "key_designs": []},
        "opensearch": {"validation_passed": True, "index_designs": []},
        "dynamodb": {"status": "skipped"},
        "aurora_mysql": {"validation_passed": True, "table_definitions": []},
    }
    out = build_designs(_state(designs), ["aurora_mysql", "opensearch", "elasticache"])
    assert [d.engine for d in out] == ["aurora_mysql", "opensearch", "elasticache"]


def test_a_partial_opensearch_design_reads_its_missing_fields_as_empty() -> None:
    """An older or partial OpenSearch design (no fields, aliases, template, access
    patterns or validation result) still gives a stable entry; an index, data stream or
    field with no name, and a field with no type, is left out, and an out-of-scope access
    pattern serves nothing."""
    opensearch: dict[str, Any] = {
        "index_designs": [
            {
                "index_name": "posts",
                "field_mappings": [
                    {"field_type": "text"},
                    {"field_name": "untyped"},
                    {"field_name": "t", "field_type": "keyword"},
                ],
            },
            {"source_tables": ["shop.nameless"]},
        ],
        "data_stream_designs": [
            {"data_stream_name": "events", "index_template": None},
            {"data_stream_name": "logs", "index_template": {"field_mappings": None}},
            {"source_tables": ["shop.nameless"]},
        ],
        "access_patterns": [
            {"index_or_stream": "posts", "query_ids": ["q1", "q2"]},
            {"index_or_stream": "posts", "query_ids": ["q2", "q3"], "in_scope": True},
            {"index_or_stream": "posts", "query_ids": ["q4"], "in_scope": False},
            {"index_or_stream": "posts-live", "query_ids": ["q5"]},
            {"index_or_stream": "events", "query_ids": ["q6"]},
        ],
    }
    (design,) = build_designs(_state({"opensearch": opensearch}), ["opensearch"])
    as_json = design.model_dump(mode="json")
    assert as_json == {
        "validation_passed": False,
        "engine": "opensearch",
        "indexes": [
            {
                "name": "posts",
                "source_tables": [],
                # A field with no name or no type is left out, never given one.
                "fields": [{"name": "t", "type": "keyword"}],
                "aliases": [],
                # Only the patterns naming the index itself, never an alias of it.
                "query_ids": ["q1", "q2", "q3"],
            }
        ],
        "data_streams": [
            {"name": "events", "source_tables": [], "fields": [], "query_ids": ["q6"]},
            {"name": "logs", "source_tables": [], "fields": [], "query_ids": []},
        ],
    }
    adapter: TypeAdapter[list[EngineDesign]] = TypeAdapter(list[EngineDesign])
    assert adapter.validate_python([as_json])[0] == design


# --------------------------------------------------------------------------- older results

#: A designed result written by the code before the OpenSearch entry (#1513): a
#: wordpress run with the engine scope OpenSearch, model phases patched, its
#: ``statements`` cut to the first three. OpenSearch is in ``summary.designed_engines``
#: with no ``designs`` entry, as that code wrote it.
PRE_OPENSEARCH_RESULT = (
    REPO / "tests" / "fixtures" / "assess_result_v1_0_opensearch_without_entry.json"
)


@pytest.mark.passes_on_main
def test_a_result_written_before_the_opensearch_entry_still_loads() -> None:
    text = PRE_OPENSEARCH_RESULT.read_text(encoding="utf-8")
    document = json.loads(text)
    assert document["summary"]["designed_engines"] == ["opensearch"]
    assert document["designs"] == []
    schema = json.loads(rcs.schema_path("assess_result").read_text("utf-8"))
    Draft202012Validator(schema).validate(document)
    loaded = AssessmentResult.model_validate_json(text)
    assert loaded.label == "designed" and loaded.designs == []
    assert json.loads(loaded.model_dump_json()) == document


# --------------------------------------------------------------------------- the entry guard


def test_a_designed_engine_with_an_entry_in_its_version_cannot_lose_it() -> None:
    """A 1.1 result lists one entry per designed engine, in order: dropping the
    OpenSearch entry, or any other, or reordering them, is refused."""
    result, _ = designed("design04", OPENSEARCH_SCOPE)
    document = json.loads(result.model_dump_json())
    assert document["contract_version"] == "1.1"
    assert [d["engine"] for d in document["designs"]] == ["dynamodb", "opensearch"]
    for change in (
        lambda d: d["designs"].pop(),
        lambda d: d["designs"].pop(0),
        lambda d: d["designs"].reverse(),
    ):
        changed = json.loads(json.dumps(document))
        change(changed)
        with pytest.raises(ValidationError, match="one entry per designed engine"):
            AssessmentResult.model_validate(changed)


def test_a_1_0_result_is_not_checked_for_one_entry_per_engine() -> None:
    """Contract 1.0 had no OpenSearch entry and no entry rule, so a 1.0 result keeps
    exactly 1.0's checks: one with OpenSearch designed and no entry is valid, and so is
    one with an engine's entry missing; marked 1.1, the same result is refused."""
    document = json.loads(PRE_OPENSEARCH_RESULT.read_text(encoding="utf-8"))
    assert document["contract_version"] == "1.0"
    AssessmentResult.model_validate(document)
    document["summary"]["designed_engines"] = ["opensearch", "elasticache"]
    AssessmentResult.model_validate(document)
    document["contract_version"] = "1.1"
    with pytest.raises(ValidationError, match="one entry per designed engine"):
        AssessmentResult.model_validate(document)


#: The shapes contract 1.0 accepts for a designed result with three designed engines
#: and that 1.1 refuses: no entry at all, one dropped, reordered, one repeated.
_ENTRY_SHAPES = {
    "empty": lambda designs: [],
    "dropped": lambda designs: designs[1:],
    "reordered": lambda designs: designs[::-1],
    "repeated": lambda designs: [*designs, designs[0]],
}


@pytest.mark.parametrize("shape", sorted(_ENTRY_SHAPES))
def test_the_entry_rule_applies_from_1_1_and_1_0_keeps_its_checks(shape: str) -> None:
    result, _ = designed()
    document = json.loads(result.model_dump_json())
    assert document["contract_version"] == "1.1"
    assert len(document["designs"]) == 3
    document["designs"] = _ENTRY_SHAPES[shape](document["designs"])
    with pytest.raises(ValidationError, match="one entry per designed engine"):
        AssessmentResult.model_validate(document)
    document["contract_version"] = "1.0"
    assert AssessmentResult.model_validate(document).designs is not None


def test_every_engine_with_an_entry_has_a_reader_and_a_model() -> None:
    """build_designs cannot skip an engine silently: the engines the current contract
    version gives an entry, the readers and the modelled entries are the same set, and
    it holds every active catalog engine."""
    from typing import get_args

    from src.assessment.models import CONTRACT_VERSION, DESIGN_ENTRY_ENGINES, ContractVersion
    from src.assessment.result_builder import _DESIGN_READERS
    from src.shared.catalog.engines import ACTIVE, engine_ids

    union = get_args(get_args(EngineDesign)[0])
    modelled = {e for m in union for e in get_args(m.model_fields["engine"].annotation)}
    assert set(_DESIGN_READERS) == modelled == DESIGN_ENTRY_ENGINES[CONTRACT_VERSION]
    assert engine_ids(status=ACTIVE) <= set(_DESIGN_READERS)
    # Every version the models read but 1.0 (which is not checked) names its engines.
    assert set(DESIGN_ENTRY_ENGINES) == set(get_args(ContractVersion)) - {"1.0"}
