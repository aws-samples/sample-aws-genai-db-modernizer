"""Each contract model's ``extra`` setting is a recorded choice (ADR-008 Decision 5, #976).

ADR-008's forward compatibility (a newer file read by older code) relies on
unknown fields being accepted. Some contracts set ``extra="forbid"``, and an
older reader then rejected a newer ``Assignment.moves`` entry outright. The
maintainer decision on #976:

- a model's answer keeps ``extra="forbid"``: an unknown key there is the model
  saying something the contract does not allow (a flag naming a destination
  engine, ADR-031 Decision 11), and the answer is refused;
- an artifact written to disk and read back across versions accepts unknown
  fields. Where the convention is to keep them on a round trip (the DynamoDB
  decision trace, the synthesis report, the move ledger), it sets
  ``extra="allow"``; everything else uses pydantic's default, ``"ignore"``.

``EXTRA_POLICY`` pins every model in ``src/contracts/`` (and the modules of the
registered contract files) whose setting is not the default, so changing one is
a deliberate edit here. No model reachable from a registered contract file may
forbid unknown fields.
"""

from __future__ import annotations

import importlib
import typing
from pathlib import Path

import pytest
from pydantic import BaseModel

from src.shared import contract_versioning as versioning

REPO = Path(__file__).resolve().parents[2]

_ANSWER = "a model's answer: an unknown key is refused (#976)"
_ROUND_TRIP = "a read-back artifact that keeps unknown keys on a round trip (#976)"
_TRACE = "a DynamoDB decision trace section: unknown keys kept (#973)"
_TRACE_BASE = "a shared decision trace section: unknown keys kept (#1187)"
_DOCDB_TRACE = "a DocumentDB decision trace section: unknown keys kept (#1188)"
_REPORT = "the synthesis report keeps unknown keys on a round trip"

# "module.Class" -> (extra setting, why). Every other model uses "ignore".
EXTRA_POLICY: dict[str, tuple[str, str]] = {
    # Model answers.
    "executive_summary_response.ExecutiveSummaryResponse": ("forbid", _ANSWER),
    "reality_check_model_response.RealityCheckModelResponse": ("forbid", _ANSWER),
    "model_flag.ModelFlag": (
        "forbid",
        "a model's flag: an entry naming a destination (target_engine, ...) is dropped "
        "whole (ADR-031 Decision 11)",
    ),
    "aurora_design_delta.AuroraDesignDeltaContract": ("forbid", _ANSWER),
    "aurora_design_delta.ColumnTypeChange": ("forbid", _ANSWER),
    "aurora_design_delta.IndexSpec": ("forbid", _ANSWER),
    "aurora_design_delta.TableDelta": ("forbid", _ANSWER),
    "aurora_design_delta.TypeRule": ("forbid", _ANSWER),
    # Read-back artifacts that keep unknown keys.
    "move_ledger.Move": ("allow", _ROUND_TRIP),
    "move_ledger.MoveEvidence": ("allow", _ROUND_TRIP),
    "move_ledger.EngineScore": ("allow", _ROUND_TRIP),
    "move_ledger.CandidateScore": ("allow", _ROUND_TRIP),
    "move_ledger.GroupGate": ("allow", _ROUND_TRIP),
    "move_ledger.CapabilityWaiver": ("allow", _ROUND_TRIP),
    "feasibility_models.FeasibilityFinding": ("allow", _ROUND_TRIP),
    "schema_design_input.SchemaDesignGroupsManifest": ("allow", _ROUND_TRIP),
    "design_metrics.DesignMetricsContract": ("allow", _ROUND_TRIP),
    "design_metrics.QueryDesignMetrics": ("allow", _ROUND_TRIP),
    "design_metrics.TableDesignMetrics": ("allow", _ROUND_TRIP),
    "design_metrics.CoverageMetrics": ("allow", _ROUND_TRIP),
    "design_metrics.DynamoDBPartitionLimits": ("allow", _ROUND_TRIP),
    "design_metrics.EmbeddingSizeMetrics": ("allow", _ROUND_TRIP),
    "design_metrics.OpenSearchShardMetrics": ("allow", _ROUND_TRIP),
    "design_metrics.OpenSearchShardRules": ("allow", _ROUND_TRIP),
    **{
        f"decision_trace_base.{name}": ("allow", _TRACE_BASE)
        for name in (
            "DecisionTraceBase",
            "TraceSummary",
            "QueryMatch",
            "PatternSummary",
            "DerivationScores",
            "RecommendationDerivation",
            "TableGroup",
            "TimestampNameFallback",
            "TimestampColumnsTrace",
            "LlmAdvisorTrace",
            "ManyToManyRelationship",
            "ForeignKeyRelationship",
        )
    },
    **{
        f"dynamodb_decision_trace.{name}": ("allow", _TRACE)
        for name in (
            "DynamoDBDecisionTrace",
            "TransactionShapeTrace",
            "QueryMatch",
            "RecommendationDerivation",
            "PkClassificationTrace",
            "AggregateTrace",
            "GsiCandidateTrace",
            "DenormalizationOpportunityTrace",
            "SecondaryIndexDominanceTrace",
            "LlmAdvisorTrace",
        )
    },
    **{
        f"documentdb_decision_trace.{name}": ("allow", _DOCDB_TRACE)
        for name in (
            "DocumentDBDecisionTrace",
            "EmbeddingCandidateTrace",
            "DenormalizationStrategyTrace",
            "DocumentDBCompatibilityTrace",
        )
    },
    "decision_record.DecisionRecord": (
        "allow",
        "a recorded approval: a hand-written record keeps its other keys (#1257)",
    ),
    "elasticache_decision_trace.ElastiCacheDecisionTrace": (
        "allow",
        "the ElastiCache decision trace: unknown keys kept (#1191)",
    ),
    **{
        f"synthesis_output.{name}": ("allow", _REPORT)
        for name in (
            "SynthesisOutputContract",
            "ConsideredEngine",
            "CostBreakdown",
            "EngineRanking",
            "MigrationWave",
            "QueryGroup",
            "Risk",
            "RiskAssessment",
            "TCOAnalysis",
            "TableMapping",
        )
    },
}

# Models whose stored form is read back from a file that is not in the contract
# file registry: design metrics, in the request a schema designer was given.
# Finalize reads them back as a raw dict (``handler._requested_design_metrics``),
# not through the model; they are walked here so the model never forbids what a
# later reader may parse.
READ_BACK_UNREGISTERED: tuple[str, ...] = ("design_metrics.DesignMetricsContract",)


def _modules() -> list[str]:
    names = {
        f"src.contracts.{p.stem}"
        for p in (REPO / "src" / "contracts").glob("*.py")
        if p.name != "__init__.py"
    }
    names |= {m.__module__ for m in versioning.registered_contracts()}
    return sorted(names)


def _models() -> dict[str, type[BaseModel]]:
    found: dict[str, type[BaseModel]] = {}
    for module_name in _modules():
        # module_name comes from the repo's own file list and registry, never input.
        # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
        module = importlib.import_module(module_name)
        for name, obj in vars(module).items():
            if (
                isinstance(obj, type)
                and issubclass(obj, BaseModel)
                and obj is not BaseModel
                and obj.__module__ == module_name
            ):
                found[f"{module_name.rsplit('.', 1)[-1]}.{name}"] = obj
    return found


MODELS = _models()


def _extra(model: type[BaseModel]) -> str:
    return str(model.model_config.get("extra") or "ignore")


def _reachable(model: type[BaseModel], seen: set[type[BaseModel]]) -> None:
    if model in seen:
        return
    seen.add(model)
    for field in model.model_fields.values():
        stack: list[object] = [field.annotation]
        while stack:
            annotation = stack.pop()
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                _reachable(annotation, seen)
            else:
                stack.extend(typing.get_args(annotation))


def _key(model: type[BaseModel]) -> str:
    return f"{model.__module__.rsplit('.', 1)[-1]}.{model.__qualname__}"


def test_the_scan_finds_the_models() -> None:
    assert len(MODELS) > 150
    assert "assignment_models.Assignment" in MODELS
    assert "load_test_models.LoadTestOutput" in MODELS


@pytest.mark.parametrize("key", sorted(MODELS))
def test_every_model_has_its_recorded_extra_setting(key: str) -> None:
    expected = EXTRA_POLICY.get(key, ("ignore", ""))[0]
    assert _extra(MODELS[key]) == expected, (
        f"{key} sets extra={_extra(MODELS[key])!r}, recorded as {expected!r}. A model's "
        "answer forbids unknown keys; an artifact read back across versions accepts them "
        "(ADR-008 Decision 5, #976). Change EXTRA_POLICY only with that decision."
    )


def test_no_stale_policy_entries() -> None:
    assert not set(EXTRA_POLICY) - set(MODELS), set(EXTRA_POLICY) - set(MODELS)


def test_no_read_back_artifact_refuses_unknown_fields() -> None:
    seen: set[type[BaseModel]] = set()
    for model in versioning.registered_contracts():
        _reachable(model, seen)
    for key in READ_BACK_UNREGISTERED:
        _reachable(MODELS[key], seen)
    forbidding = sorted(_key(m) for m in seen if _extra(m) == "forbid")
    assert not forbidding, (
        f"read back across versions but refuse unknown fields: {forbidding} (ADR-008 "
        "Decision 5): an older reader would reject a newer file"
    )


def test_every_model_answer_refuses_unknown_fields() -> None:
    answers = [k for k, (_, why) in EXTRA_POLICY.items() if why == _ANSWER]
    assert answers and all(_extra(MODELS[k]) == "forbid" for k in answers)


UNKNOWN_FIELD = "x_added_by_a_later_minor"


def with_unknown_fields(model: type[BaseModel], data: object) -> object:
    """``data`` (a stored file of ``model``) with an unknown field added to every object
    the model types, as a file a later MINOR wrote would have. A field typed by a union
    of several models is left alone (which member an object is is not known here)."""
    if not isinstance(data, dict):
        return data
    out = {**data, UNKNOWN_FIELD: {"kept": True}}
    for name, field in model.model_fields.items():
        key = field.alias if field.alias in out else name
        if key not in out:
            continue
        annotation = field.annotation
        models: set[type[BaseModel]] = set()
        stack: list[object] = [annotation]
        while stack:
            current = stack.pop()
            if isinstance(current, type) and issubclass(current, BaseModel):
                models.add(current)
            else:
                stack.extend(typing.get_args(current))
        if len(models) != 1:
            continue
        (inner,) = models
        value = out[key]
        if isinstance(value, list):
            out[key] = [with_unknown_fields(inner, item) for item in value]
        elif isinstance(value, dict) and typing.get_origin(annotation) is dict:
            out[key] = {k: with_unknown_fields(inner, v) for k, v in value.items()}
        elif isinstance(value, dict) and not any(
            typing.get_origin(a) is dict for a in typing.get_args(annotation)
        ):
            out[key] = with_unknown_fields(inner, value)
    return out


def test_the_injection_reaches_nested_models() -> None:
    from src.contracts.feasibility_models import FindingKind, FindingSeverity
    from src.contracts.move_ledger import MovePass, ReasonCode
    from tests.unit.agents.referee.test_cache_safety_net_notes_reset import ASSIGNMENT_V2

    assignment = MODELS["assignment_models.Assignment"]
    move = MODELS["move_ledger.Move"](
        seq=1,
        query_id="hot1",
        pass_=MovePass.SCORE,
        to_engine="dynamodb",
        reason_code=ReasonCode.HIGHEST_SCORE,
    )
    finding = MODELS["feasibility_models.FeasibilityFinding"](
        kind=FindingKind.READ_WRITE_SPLIT, severity=FindingSeverity.ADVISORY, message="x"
    )
    data = assignment.model_validate(
        {**ASSIGNMENT_V2, "moves": [move], "accepted_feasibility_findings": [finding]}
    ).model_dump(mode="json")
    injected = with_unknown_fields(assignment, data)
    assert isinstance(injected, dict)
    assert UNKNOWN_FIELD in injected
    assert UNKNOWN_FIELD in injected["moves"][0]
    assert UNKNOWN_FIELD in injected["accepted_feasibility_findings"][0]
    assert UNKNOWN_FIELD in injected["query_assignments"][0]
    assignment.model_validate(injected)
