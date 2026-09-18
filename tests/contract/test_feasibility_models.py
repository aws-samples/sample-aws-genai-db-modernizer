"""Contract test for the feasibility-review finding model (ADR-029 Layer C)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.contracts.feasibility_models import FeasibilityFinding, FindingKind, FindingSeverity


def test_round_trips_through_json() -> None:
    finding = FeasibilityFinding(
        kind=FindingKind.READ_WRITE_SPLIT,
        severity=FindingSeverity.BLOCKING,
        table="t.orders",
        engines=["dynamodb", "opensearch"],
        query_ids=["q1", "q2"],
        message="reads on opensearch depend on writes on dynamodb",
    )
    restored = FeasibilityFinding.model_validate(finding.model_dump(mode="json"))
    assert restored == finding


def test_recommended_pattern_defaults_none_and_round_trips() -> None:
    default = FeasibilityFinding(
        kind=FindingKind.CO_DEPENDENCY_SPLIT,
        severity=FindingSeverity.BLOCKING,
        message="x",
    )
    assert default.recommended_pattern is None

    with_pattern = FeasibilityFinding(
        kind=FindingKind.READ_WRITE_SPLIT,
        severity=FindingSeverity.ADVISORY,
        table="t.orders",
        engines=["dynamodb", "opensearch"],
        message="needs replication",
        recommended_pattern="CDC / zero-ETL to OpenSearch",
    )
    restored = FeasibilityFinding.model_validate(with_pattern.model_dump(mode="json"))
    assert restored == with_pattern
    assert restored.recommended_pattern == "CDC / zero-ETL to OpenSearch"


def test_table_defaults_to_none_for_query_group_findings() -> None:
    finding = FeasibilityFinding(
        kind=FindingKind.CO_DEPENDENCY_SPLIT,
        severity=FindingSeverity.ADVISORY,
        query_ids=["q1", "q2"],
        engines=["aurora_postgresql", "aurora_mysql"],
        message="cross-engine join",
    )
    assert finding.table is None


def test_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        FeasibilityFinding.model_validate(
            {
                "kind": "read_write_split",
                "severity": "blocking",
                "message": "x",
                "bogus": "nope",
            }
        )


def test_rejects_unknown_enum_values() -> None:
    with pytest.raises(ValidationError):
        FeasibilityFinding.model_validate(
            {"kind": "teleport", "severity": "blocking", "message": "x"}
        )
