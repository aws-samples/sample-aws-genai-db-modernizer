"""The DynamoDB decision trace is a versioned contract (ADR-008 Decision 1, #973).

``analysis-dynamodb/decision-trace.json`` used to be a plain dict: a renamed field
or a new denormalization ``subtype`` passed every guard, and the file carried no
``contract_version`` a reader could check. It is now ``DynamoDBDecisionTrace``:
the builder builds it, every writer writes through it, and a read checks its
version (``check_artifact`` in the store, ``load_contract`` on the ``--finalize``
resume). A trace written before it had a version still loads.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import typing
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import BaseModel, ValidationError

from src.agents.analysis.dynamodb_analysis_agent import analyze_for_dynamodb_deterministic
from src.agents.analysis.handler import run_analysis
from src.agents.referee import transaction_shape as shapes
from src.atx_orchestrator.core import run_analysis_core
from src.atx_orchestrator.runtime.store import upgrade_store
from src.contracts.analysis_input import AnalysisInput, TargetDatabase
from src.contracts.dynamodb_decision_trace import (
    AggregateTrace,
    DenormalizationOpportunityTrace,
    DynamoDBDecisionTrace,
    GsiCandidateTrace,
    PkClassificationTrace,
    SecondaryIndexDominanceTrace,
    TransactionShapeKind,
)
from src.shared.contract_versioning import (
    ContractVersionError,
    contract_for_artifact,
    current_version,
    load_contract,
)
from src.storage.local_store import LocalArtifactStore
from src.tools.analysis import dynamodb_analysis_tools as tools
from src.tools.analysis.scoring import Aggregate
from tests.fixtures.ecommerce_collector_output import get_ecommerce_collector_output

DB, JOB = "mydb", "job-1"
TRACE_KEY = f"{DB}/{JOB}/analysis-dynamodb/decision-trace.json"
CURRENT = current_version(DynamoDBDecisionTrace)


def _built_trace() -> dict:
    analysis_input = AnalysisInput(
        job_id=JOB,
        collector_output=get_ecommerce_collector_output(),
        target_database=TargetDatabase.DYNAMODB,
    )
    _, trace, _ = analyze_for_dynamodb_deterministic(analysis_input)
    return trace


def _store(tmp_path: Path) -> LocalArtifactStore:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(f"{DB}/{JOB}/collector/output.json", get_ecommerce_collector_output())
    return store


def _raw_file(tmp_path: Path, key: str) -> dict:
    data: dict = json.loads((tmp_path / key).read_text())
    return data


# --- the model and the builder ------------------------------------------------


def test_the_trace_declares_a_checked_contract_version() -> None:
    field = DynamoDBDecisionTrace.model_fields["contract_version"]
    assert field.default == CURRENT
    assert tuple(map(int, CURRENT.split("."))) >= (1, 1)  # #973 moved it past 1.0
    assert any(getattr(m, "pattern", None) == r"^\d+\.\d+$" for m in field.metadata)


def test_the_builder_writes_the_current_contract_version() -> None:
    assert _built_trace()["contract_version"] == CURRENT


def test_the_built_trace_round_trips_through_its_model_unchanged() -> None:
    trace = _built_trace()
    as_written = json.loads(json.dumps(trace))
    assert DynamoDBDecisionTrace.model_validate(as_written).model_dump(mode="json") == as_written


def test_the_trace_keeps_its_sections_and_their_order() -> None:
    """Apart from the new version field, the file has the keys, in the order, it had."""
    assert list(_built_trace()) == [
        "contract_version",
        "trace_version",
        "agent",
        "summary",
        "query_matches",
        "pattern_summaries",
        "recommendation_derivations",
        "table_groups",
        "timestamp_columns",
        "pk_classifications",
        "aggregates",
        "gsi_candidates",
        "denormalization_opportunities",
        "secondary_index_dominance",
        "llm_advisor",
        "relationships",
    ]


def test_a_query_with_no_transaction_shape_has_no_shape_key() -> None:
    """``transaction_shape`` is written only for a write that has one, never as null."""
    matches = _built_trace()["query_matches"]
    assert any("transaction_shape" in m for m in matches)
    without = [m for m in matches if "transaction_shape" not in m]
    assert without, "the fixture should have reads with no transaction shape"
    assert all(m.get("transaction_shape", "absent") is not None for m in matches)


def test_the_builder_without_relationships_writes_an_empty_list() -> None:
    analysis_input = AnalysisInput(
        job_id=JOB,
        collector_output=get_ecommerce_collector_output(),
        target_database=TargetDatabase.DYNAMODB,
    )
    contract, _, _ = analyze_for_dynamodb_deterministic(analysis_input)
    trace = tools.build_decision_trace(
        analysis_input.collector_output, contract.workload_analysis, contract.table_recommendations
    )
    assert trace["relationships"] == []
    assert trace["contract_version"] == CURRENT


@pytest.mark.parametrize(
    ("model", "source"),
    [
        (PkClassificationTrace, tools.PkClassification),
        (GsiCandidateTrace, tools.GsiCandidate),
        (DenormalizationOpportunityTrace, tools.DenormalizationOpportunity),
        (SecondaryIndexDominanceTrace, tools.SecondaryIndexDominance),
        (AggregateTrace, Aggregate),
    ],
)
def test_every_trace_section_mirrors_the_dataclass_it_records(
    model: type[BaseModel], source: type
) -> None:
    """A field added to or renamed in the dataclass must reach the contract too."""
    assert list(model.model_fields) == [f.name for f in dataclasses.fields(source)]


def test_the_transaction_shapes_are_the_ones_the_classifier_writes() -> None:
    assert typing.get_args(TransactionShapeKind) == (
        shapes.SINGLE_ITEM,
        shapes.BOUNDED,
        shapes.KEY_SCOPED,
        shapes.UNBOUNDED,
    )


def test_the_denormalization_subtypes_are_published_by_the_contract() -> None:
    """The enum lives in the contract, so the value-set snapshot sees a new subtype."""
    assert tools.DenormSubtype.__module__ == "src.contracts.dynamodb_decision_trace"


def test_an_unknown_denormalization_subtype_is_refused() -> None:
    trace = json.loads(json.dumps(_built_trace()))
    assert trace["denormalization_opportunities"], "the fixture should have opportunities"
    trace["denormalization_opportunities"][0]["subtype"] = "nested-document"
    with pytest.raises(ValidationError, match="subtype"):
        DynamoDBDecisionTrace.model_validate(trace)


def test_a_renamed_section_is_refused() -> None:
    trace = json.loads(json.dumps(_built_trace()))
    trace["gsi_suggestions"] = trace.pop("gsi_candidates")
    with pytest.raises(ValidationError, match="gsi_candidates"):
        DynamoDBDecisionTrace.model_validate(trace)


def test_a_renamed_field_inside_a_section_is_refused() -> None:
    trace = json.loads(json.dumps(_built_trace()))
    entry = trace["pk_classifications"][0]
    entry["partition_key"] = entry.pop("partition_key_candidate")
    with pytest.raises(ValidationError, match="partition_key_candidate"):
        DynamoDBDecisionTrace.model_validate(trace)


@pytest.mark.parametrize(
    "relationship",
    [
        {
            "type": "M:N",
            "junction_table": "s.post_tags",
            "table_a": "posts",
            "table_b": "tags",
            "columns_a": ["post_id"],
            "columns_b": ["tag_id"],
            "description": "Many-to-many via post_tags: posts <-> tags",
        },
        {
            "type": "1:M",
            "from_table": "s.orders",
            "to_table": "customers",
            "from_columns": ["customer_id"],
            "to_columns": ["id"],
            "description": "1:M: orders.customer_id -> customers.id",
        },
    ],
)
def test_both_relationship_shapes_round_trip(relationship: dict) -> None:
    trace = json.loads(json.dumps(_built_trace()))
    trace["relationships"] = [relationship]
    assert DynamoDBDecisionTrace.model_validate(trace).model_dump(mode="json") == trace


def test_a_relationship_of_an_unknown_type_is_refused() -> None:
    trace = json.loads(json.dumps(_built_trace()))
    trace["relationships"] = [{"type": "N:1", "from_table": "a", "to_table": "b"}]
    with pytest.raises(ValidationError):
        DynamoDBDecisionTrace.model_validate(trace)


# --- reading -------------------------------------------------------------------


def test_the_trace_file_is_a_registered_contract_file() -> None:
    assert contract_for_artifact(TRACE_KEY) is DynamoDBDecisionTrace


def test_a_trace_written_before_the_version_still_loads(tmp_path: Path) -> None:
    legacy = json.loads(json.dumps(_built_trace()))
    del legacy["contract_version"]  # what every trace before #973 looked like
    path = tmp_path / TRACE_KEY
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(legacy))

    store = LocalArtifactStore(base_dir=str(tmp_path))
    read = store.read_json(TRACE_KEY)
    assert read == legacy
    loaded = load_contract(DynamoDBDecisionTrace, read, artifact=TRACE_KEY)
    assert loaded.contract_version == CURRENT
    assert loaded.model_dump(mode="json") == {"contract_version": CURRENT, **legacy}


def test_a_saved_pre_973_trace_still_loads() -> None:
    """A trace file as the pipeline wrote it before #973 (no version, typed by nothing,
    and from before #935, so no ``timestamp_columns``: it is not written back as null)."""
    fixture = Path(__file__).resolve().parents[2] / "fixtures" / "dynamodb_decision_trace_1_1.json"
    legacy = json.loads(fixture.read_text())
    assert "contract_version" not in legacy and legacy["trace_version"] == "1.1"
    assert "timestamp_columns" not in legacy
    loaded = load_contract(DynamoDBDecisionTrace, legacy, artifact=TRACE_KEY)
    assert loaded.model_dump(mode="json") == {"contract_version": CURRENT, **legacy}


def test_the_store_refuses_a_trace_at_another_major(tmp_path: Path) -> None:
    newer = {**json.loads(json.dumps(_built_trace())), "contract_version": "2.0"}
    path = tmp_path / TRACE_KEY
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(newer))
    with pytest.raises(ContractVersionError, match="DynamoDBDecisionTrace"):
        LocalArtifactStore(base_dir=str(tmp_path)).read_json(TRACE_KEY)


def test_the_store_reads_a_trace_at_a_newer_minor(tmp_path: Path) -> None:
    newer = {**json.loads(json.dumps(_built_trace())), "contract_version": "1.7"}
    path = tmp_path / TRACE_KEY
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(newer))
    assert LocalArtifactStore(base_dir=str(tmp_path)).read_json(TRACE_KEY) == newer


# --- writing -------------------------------------------------------------------


def test_the_analysis_handler_writes_the_trace_through_its_model(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run_analysis(JOB, DB, "dynamodb", store, llm_mode="none")
    written = _raw_file(tmp_path, TRACE_KEY)
    assert written["contract_version"] == CURRENT
    assert DynamoDBDecisionTrace.model_validate(written).model_dump(mode="json") == written


def test_the_transform_core_writes_the_trace_through_its_model(tmp_path: Path) -> None:
    store = upgrade_store(_store(tmp_path))
    run_analysis_core("dynamodb", JOB, DB, store)
    written = _raw_file(tmp_path, TRACE_KEY)
    assert written["contract_version"] == CURRENT


def _with_bad_subtype(*args, **kwargs):
    contract, trace, mermaid = analyze_for_dynamodb_deterministic(args[0])
    trace = copy.deepcopy(trace)
    trace["denormalization_opportunities"][0]["subtype"] = "nested-document"
    return contract, trace, mermaid


def test_the_handler_refuses_to_write_a_trace_its_model_refuses(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with patch(
        "src.agents.analysis.dynamodb_analysis_agent.analyze_for_dynamodb", _with_bad_subtype
    ):
        with pytest.raises(ValidationError, match="subtype"):
            run_analysis(JOB, DB, "dynamodb", store, llm_mode="none")
    assert not (tmp_path / TRACE_KEY).exists()


def test_the_handler_stamps_the_current_version_over_a_stale_one(tmp_path: Path) -> None:
    def stale(*args, **kwargs):
        contract, trace, mermaid = analyze_for_dynamodb_deterministic(args[0])
        return (
            contract,
            {**trace, "contract_version": "1.0" if CURRENT != "1.0" else "1.9"},
            mermaid,
        )

    store = _store(tmp_path)
    with patch("src.agents.analysis.dynamodb_analysis_agent.analyze_for_dynamodb", stale):
        run_analysis(JOB, DB, "dynamodb", store, llm_mode="none")
    assert _raw_file(tmp_path, TRACE_KEY)["contract_version"] == CURRENT


def test_the_analysis_script_writes_the_trace_through_its_model(tmp_path: Path) -> None:
    from scripts import run_analysis as script

    store = _store(tmp_path)
    script.run_standard(store, JOB, DB, "dynamodb", "none")
    written = _raw_file(tmp_path, TRACE_KEY)
    assert written["contract_version"] == CURRENT
    assert DynamoDBDecisionTrace.model_validate(written).model_dump(mode="json") == written


_EMPTY_ADVICE: dict[str, list] = {"aggregate_recommendations": [], "denormalization_strategies": []}


def _external_partial(tmp_path: Path) -> tuple[LocalArtifactStore, str]:
    from scripts import run_analysis as script

    store = _store(tmp_path)
    script.run_external(store, JOB, DB, "dynamodb")
    store.write_json(f"{DB}/{JOB}/llm_responses/analysis_dynamodb.json", _EMPTY_ADVICE)
    return store, f"{DB}/{JOB}/analysis-dynamodb/partial.json"


def test_the_finalize_resume_writes_the_trace_through_its_model(tmp_path: Path) -> None:
    from scripts import run_analysis as script

    store, partial_key = _external_partial(tmp_path)
    partial = _raw_file(tmp_path, partial_key)
    del partial["decision_trace"]["contract_version"]  # a partial saved before #973
    store.write_json(partial_key, partial)

    script.run_finalize(store, JOB, DB, "dynamodb")
    written = _raw_file(tmp_path, TRACE_KEY)
    assert written["contract_version"] == CURRENT
    assert written == {**partial["decision_trace"], "contract_version": CURRENT}


def test_the_finalize_resume_refuses_a_partial_trace_at_another_major(tmp_path: Path) -> None:
    from scripts import run_analysis as script

    store, partial_key = _external_partial(tmp_path)
    partial = _raw_file(tmp_path, partial_key)
    partial["decision_trace"]["contract_version"] = "2.0"
    store.write_json(partial_key, partial)

    with pytest.raises(ContractVersionError, match="partial.json"):
        script.run_finalize(store, JOB, DB, "dynamodb")
    assert not (tmp_path / TRACE_KEY).exists()


def _with_unknown_keys() -> dict:
    """A built trace with a key no model declares at four levels (a later writer's
    addition, or a newer MINOR's field)."""
    trace: dict = json.loads(json.dumps(_built_trace()))
    trace["future_section"] = {"kept": True}
    trace["summary"]["queries_sampled"] = 3
    shaped = next(m for m in trace["query_matches"] if "transaction_shape" in m)
    shaped["transaction_shape"]["isolation"] = "serializable"
    trace["relationships"] = [
        {
            "type": "1:M",
            "from_table": "s.orders",
            "to_table": "customers",
            "from_columns": ["customer_id"],
            "to_columns": ["id"],
            "description": "1:M: orders.customer_id -> customers.id",
            "on_delete": "cascade",
        }
    ]
    return trace


def test_a_key_the_model_does_not_declare_is_kept() -> None:
    """Unknown keys are neither refused (older and newer traces must load, ADR-008
    Decision 5) nor dropped (a round trip through the model loses nothing)."""
    trace = _with_unknown_keys()
    assert DynamoDBDecisionTrace.model_validate(trace).model_dump(mode="json") == trace


def test_an_unknown_key_survives_a_write_and_a_load(tmp_path: Path) -> None:
    from src.agents.analysis.decision_trace import write_decision_trace

    trace = _with_unknown_keys()
    store = LocalArtifactStore(base_dir=str(tmp_path))
    write_decision_trace(store, TRACE_KEY, trace)
    loaded = load_contract(DynamoDBDecisionTrace, store.read_json(TRACE_KEY), artifact=TRACE_KEY)
    dumped = loaded.model_dump(mode="json")
    assert dumped == trace
    assert dumped["future_section"] == {"kept": True}
    assert dumped["relationships"][0]["on_delete"] == "cascade"
