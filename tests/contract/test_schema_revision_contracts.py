"""Tests for schema revision loop contracts.

Covers round-trip serialization, required field validation,
optional fields, and rejection of invalid values.
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from src.contracts.schema_revision_models import (
    ChangelogEntry,
    NewPattern,
    PatternAction,
    PatternModification,
    SchemaConfirmation,
    SchemaRevisionRequest,
    SchemaVersionMeta,
    TableModification,
    VerificationIssue,
    VerificationResult,
)

# ---------------------------------------------------------------------------
# PatternAction enum
# ---------------------------------------------------------------------------


class TestPatternAction:
    def test_drop_value(self):
        assert PatternAction.DROP == "DROP"

    def test_note_value(self):
        assert PatternAction.NOTE == "NOTE"

    def test_reassign_value(self):
        assert PatternAction.REASSIGN == "REASSIGN"

    def test_invalid_value_rejected(self):
        with pytest.raises(ValueError):
            PatternAction("INVALID")

    def test_all_members(self):
        members = {a.value for a in PatternAction}
        assert members == {"DROP", "NOTE", "REASSIGN"}


# ---------------------------------------------------------------------------
# PatternModification
# ---------------------------------------------------------------------------


class TestPatternModification:
    def test_minimal_valid(self):
        mod = PatternModification(pattern_id="AP-1", action=PatternAction.DROP)
        assert mod.pattern_id == "AP-1"
        assert mod.action == PatternAction.DROP
        assert mod.note is None
        assert mod.target_engine is None

    def test_with_note(self):
        mod = PatternModification(
            pattern_id="AP-2", action=PatternAction.NOTE, note="Low priority query"
        )
        assert mod.note == "Low priority query"

    def test_reassign_with_target_engine(self):
        mod = PatternModification(
            pattern_id="AP-3",
            action=PatternAction.REASSIGN,
            target_engine="opensearch",
        )
        assert mod.target_engine == "opensearch"

    def test_missing_pattern_id_fails(self):
        with pytest.raises(ValidationError, match="pattern_id"):
            PatternModification(action=PatternAction.DROP)

    def test_missing_action_fails(self):
        with pytest.raises(ValidationError, match="action"):
            PatternModification(pattern_id="AP-1")

    def test_invalid_action_fails(self):
        with pytest.raises(ValidationError):
            PatternModification(pattern_id="AP-1", action="UNKNOWN")

    def test_roundtrip_serialization(self):
        mod = PatternModification(
            pattern_id="AP-10",
            action=PatternAction.REASSIGN,
            note="Move to search",
            target_engine="opensearch",
        )
        json_str = mod.model_dump_json()
        restored = PatternModification.model_validate_json(json_str)
        assert restored.pattern_id == mod.pattern_id
        assert restored.action == mod.action
        assert restored.note == mod.note
        assert restored.target_engine == mod.target_engine


# ---------------------------------------------------------------------------
# TableModification
# ---------------------------------------------------------------------------


class TestTableModification:
    def test_valid_drop(self):
        mod = TableModification(table_id="orders", action="drop")
        assert mod.table_id == "orders"
        assert mod.action == "drop"

    def test_missing_table_id_fails(self):
        with pytest.raises(ValidationError, match="table_id"):
            TableModification(action="drop")

    def test_missing_action_fails(self):
        with pytest.raises(ValidationError, match="action"):
            TableModification(table_id="orders")

    def test_invalid_action_rejected(self):
        with pytest.raises(ValidationError):
            TableModification(table_id="orders", action="rename")

    def test_roundtrip_serialization(self):
        mod = TableModification(table_id="sessions", action="drop")
        json_str = mod.model_dump_json()
        restored = TableModification.model_validate_json(json_str)
        assert restored.table_id == mod.table_id
        assert restored.action == mod.action


# ---------------------------------------------------------------------------
# NewPattern
# ---------------------------------------------------------------------------


class TestNewPattern:
    def test_minimal_valid(self):
        pattern = NewPattern(
            description="Fetch recent orders by customer",
            target_engine="dynamodb",
            source_tables=["orders", "customers"],
        )
        assert pattern.description == "Fetch recent orders by customer"
        assert pattern.target_engine == "dynamodb"
        assert pattern.source_tables == ["orders", "customers"]
        assert pattern.estimated_reads_per_second is None
        assert pattern.estimated_writes_per_second is None
        assert pattern.context is None

    def test_with_all_optional_fields(self):
        pattern = NewPattern(
            description="Search products by keyword",
            target_engine="opensearch",
            source_tables=["products"],
            estimated_reads_per_second=500.0,
            estimated_writes_per_second=10.0,
            context="Full-text search needed for product catalog",
        )
        assert pattern.estimated_reads_per_second == 500.0
        assert pattern.estimated_writes_per_second == 10.0
        assert pattern.context == "Full-text search needed for product catalog"

    def test_missing_description_fails(self):
        with pytest.raises(ValidationError, match="description"):
            NewPattern(target_engine="dynamodb", source_tables=["t1"])

    def test_missing_target_engine_fails(self):
        with pytest.raises(ValidationError, match="target_engine"):
            NewPattern(description="Test pattern", source_tables=["t1"])

    def test_missing_source_tables_fails(self):
        with pytest.raises(ValidationError, match="source_tables"):
            NewPattern(description="Test pattern", target_engine="dynamodb")

    def test_roundtrip_serialization(self):
        pattern = NewPattern(
            description="Get user profile",
            target_engine="dynamodb",
            source_tables=["users"],
            estimated_reads_per_second=1000.0,
        )
        json_str = pattern.model_dump_json()
        restored = NewPattern.model_validate_json(json_str)
        assert restored.description == pattern.description
        assert restored.estimated_reads_per_second == pattern.estimated_reads_per_second


# ---------------------------------------------------------------------------
# SchemaRevisionRequest
# ---------------------------------------------------------------------------


class TestSchemaRevisionRequest:
    def test_minimal_valid(self):
        req = SchemaRevisionRequest(
            base_version=1,
            pattern_modifications=[],
            table_modifications=[],
            new_patterns=[],
        )
        assert req.base_version == 1
        assert req.pattern_modifications == []
        assert req.table_modifications == []
        assert req.new_patterns == []

    def test_with_modifications(self):
        req = SchemaRevisionRequest(
            base_version=2,
            pattern_modifications=[
                PatternModification(pattern_id="AP-5", action=PatternAction.DROP)
            ],
            table_modifications=[TableModification(table_id="logs", action="drop")],
            new_patterns=[
                NewPattern(
                    description="New search pattern",
                    target_engine="opensearch",
                    source_tables=["products"],
                )
            ],
        )
        assert req.base_version == 2
        assert len(req.pattern_modifications) == 1
        assert len(req.table_modifications) == 1
        assert len(req.new_patterns) == 1

    def test_missing_base_version_fails(self):
        with pytest.raises(ValidationError, match="base_version"):
            SchemaRevisionRequest(pattern_modifications=[], table_modifications=[], new_patterns=[])

    def test_invalid_base_version_type_fails(self):
        with pytest.raises(ValidationError):
            SchemaRevisionRequest(
                base_version="v1",
                pattern_modifications=[],
                table_modifications=[],
                new_patterns=[],
            )

    def test_roundtrip_serialization(self):
        req = SchemaRevisionRequest(
            base_version=3,
            pattern_modifications=[
                PatternModification(
                    pattern_id="AP-1", action=PatternAction.NOTE, note="Review later"
                )
            ],
            table_modifications=[],
            new_patterns=[],
        )
        json_str = req.model_dump_json()
        restored = SchemaRevisionRequest.model_validate_json(json_str)
        assert restored.base_version == req.base_version
        assert restored.pattern_modifications[0].note == "Review later"


# ---------------------------------------------------------------------------
# VerificationIssue
# ---------------------------------------------------------------------------


class TestVerificationIssue:
    def test_minimal_valid(self):
        issue = VerificationIssue(
            category="coverage",
            severity="error",
            message="Table orders has no access pattern",
            affected_patterns=[],
            affected_tables=["orders"],
        )
        assert issue.category == "coverage"
        assert issue.severity == "error"
        assert issue.cost_delta is None
        assert issue.suggested_resolutions == []

    def test_all_valid_categories(self):
        for cat in ("coverage", "consistency", "conflict", "cost"):
            issue = VerificationIssue(
                category=cat,
                severity="warning",
                message="Test",
                affected_patterns=[],
                affected_tables=[],
            )
            assert issue.category == cat

    def test_all_valid_severities(self):
        for sev in ("error", "warning"):
            issue = VerificationIssue(
                category="coverage",
                severity=sev,
                message="Test",
                affected_patterns=[],
                affected_tables=[],
            )
            assert issue.severity == sev

    def test_invalid_category_fails(self):
        with pytest.raises(ValidationError):
            VerificationIssue(
                category="unknown",
                severity="error",
                message="Test",
                affected_patterns=[],
                affected_tables=[],
            )

    def test_invalid_severity_fails(self):
        with pytest.raises(ValidationError):
            VerificationIssue(
                category="coverage",
                severity="critical",
                message="Test",
                affected_patterns=[],
                affected_tables=[],
            )

    def test_with_optional_fields(self):
        issue = VerificationIssue(
            category="cost",
            severity="warning",
            message="Cost increase detected",
            affected_patterns=["AP-1", "AP-2"],
            affected_tables=["orders"],
            cost_delta=150.75,
            suggested_resolutions=["Remove AP-2", "Merge AP-1 with AP-3"],
        )
        assert issue.cost_delta == 150.75
        assert len(issue.suggested_resolutions) == 2

    def test_roundtrip_serialization(self):
        issue = VerificationIssue(
            category="conflict",
            severity="error",
            message="Conflicting key conditions",
            affected_patterns=["AP-5"],
            affected_tables=["users"],
            cost_delta=-20.0,
            suggested_resolutions=["Drop AP-5"],
        )
        json_str = issue.model_dump_json()
        restored = VerificationIssue.model_validate_json(json_str)
        assert restored.category == issue.category
        assert restored.cost_delta == issue.cost_delta
        assert restored.suggested_resolutions == issue.suggested_resolutions


# ---------------------------------------------------------------------------
# VerificationResult
# ---------------------------------------------------------------------------


class TestVerificationResult:
    def test_passed_with_no_issues(self):
        result = VerificationResult(passed=True, hard_errors=[], warnings=[])
        assert result.passed is True
        assert result.hard_errors == []
        assert result.warnings == []

    def test_failed_with_errors(self):
        error = VerificationIssue(
            category="coverage",
            severity="error",
            message="Missing pattern",
            affected_patterns=[],
            affected_tables=["t1"],
        )
        result = VerificationResult(passed=False, hard_errors=[error], warnings=[])
        assert result.passed is False
        assert len(result.hard_errors) == 1

    def test_with_warnings(self):
        warning = VerificationIssue(
            category="cost",
            severity="warning",
            message="Cost increase",
            affected_patterns=["AP-1"],
            affected_tables=[],
        )
        result = VerificationResult(passed=True, hard_errors=[], warnings=[warning])
        assert len(result.warnings) == 1

    def test_missing_passed_field_fails(self):
        with pytest.raises(ValidationError, match="passed"):
            VerificationResult(hard_errors=[], warnings=[])

    def test_roundtrip_serialization(self):
        result = VerificationResult(
            passed=False,
            hard_errors=[
                VerificationIssue(
                    category="consistency",
                    severity="error",
                    message="Inconsistent engine assignment",
                    affected_patterns=["AP-3"],
                    affected_tables=[],
                )
            ],
            warnings=[],
        )
        json_str = result.model_dump_json()
        restored = VerificationResult.model_validate_json(json_str)
        assert restored.passed == result.passed
        assert len(restored.hard_errors) == 1
        assert restored.hard_errors[0].message == "Inconsistent engine assignment"


# ---------------------------------------------------------------------------
# ChangelogEntry
# ---------------------------------------------------------------------------


class TestChangelogEntry:
    def test_minimal_valid(self):
        entry = ChangelogEntry(
            change_type="added",
            entity_type="access_pattern",
            entity_id="AP-NEW-1",
            description="Added search pattern for product catalog",
        )
        assert entry.change_type == "added"
        assert entry.entity_type == "access_pattern"
        assert entry.from_engine is None
        assert entry.to_engine is None

    def test_all_valid_change_types(self):
        for ct in ("added", "removed", "modified", "reassigned"):
            entry = ChangelogEntry(
                change_type=ct,
                entity_type="table",
                entity_id="orders",
                description="Test",
            )
            assert entry.change_type == ct

    def test_all_valid_entity_types(self):
        for et in ("access_pattern", "table", "index", "collection"):
            entry = ChangelogEntry(
                change_type="modified",
                entity_type=et,
                entity_id="some-id",
                description="Test",
            )
            assert entry.entity_type == et

    def test_invalid_change_type_fails(self):
        with pytest.raises(ValidationError):
            ChangelogEntry(
                change_type="deleted",
                entity_type="table",
                entity_id="t1",
                description="Test",
            )

    def test_invalid_entity_type_fails(self):
        with pytest.raises(ValidationError):
            ChangelogEntry(
                change_type="added",
                entity_type="view",
                entity_id="v1",
                description="Test",
            )

    def test_reassigned_with_engine_fields(self):
        entry = ChangelogEntry(
            change_type="reassigned",
            entity_type="access_pattern",
            entity_id="AP-7",
            description="Moved from DynamoDB to OpenSearch",
            from_engine="dynamodb",
            to_engine="opensearch",
        )
        assert entry.from_engine == "dynamodb"
        assert entry.to_engine == "opensearch"

    def test_roundtrip_serialization(self):
        entry = ChangelogEntry(
            change_type="reassigned",
            entity_type="access_pattern",
            entity_id="AP-7",
            description="Moved to OpenSearch",
            from_engine="dynamodb",
            to_engine="opensearch",
        )
        json_str = entry.model_dump_json()
        restored = ChangelogEntry.model_validate_json(json_str)
        assert restored.change_type == entry.change_type
        assert restored.from_engine == entry.from_engine
        assert restored.to_engine == entry.to_engine


# ---------------------------------------------------------------------------
# SchemaVersionMeta
# ---------------------------------------------------------------------------


_NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)

_PASSING_VERIFICATION = VerificationResult(passed=True, hard_errors=[], warnings=[])


class TestSchemaVersionMeta:
    def test_minimal_system_initiated(self):
        meta = SchemaVersionMeta(
            version=1,
            initiated_by="system",
            timestamp=_NOW,
            redesigned_groups=[],
            verification=_PASSING_VERIFICATION,
            changelog=[],
        )
        assert meta.version == 1
        assert meta.base_version is None
        assert meta.initiated_by == "system"
        assert meta.modifications is None
        assert meta.redesigned_groups == []

    def test_customer_initiated_with_modifications(self):
        rev_request = SchemaRevisionRequest(
            base_version=1,
            pattern_modifications=[
                PatternModification(pattern_id="AP-1", action=PatternAction.DROP)
            ],
            table_modifications=[],
            new_patterns=[],
        )
        meta = SchemaVersionMeta(
            version=2,
            base_version=1,
            initiated_by="customer",
            timestamp=_NOW,
            modifications=rev_request,
            redesigned_groups=["dynamodb"],
            verification=_PASSING_VERIFICATION,
            changelog=[
                ChangelogEntry(
                    change_type="removed",
                    entity_type="access_pattern",
                    entity_id="AP-1",
                    description="Dropped per customer request",
                )
            ],
        )
        assert meta.version == 2
        assert meta.base_version == 1
        assert meta.initiated_by == "customer"
        assert meta.modifications is not None
        assert len(meta.changelog) == 1

    def test_invalid_initiated_by_fails(self):
        with pytest.raises(ValidationError):
            SchemaVersionMeta(
                version=1,
                initiated_by="admin",
                timestamp=_NOW,
                redesigned_groups=[],
                verification=_PASSING_VERIFICATION,
                changelog=[],
            )

    def test_missing_version_fails(self):
        with pytest.raises(ValidationError, match="version"):
            SchemaVersionMeta(
                initiated_by="system",
                timestamp=_NOW,
                redesigned_groups=[],
                verification=_PASSING_VERIFICATION,
                changelog=[],
            )

    def test_missing_verification_fails(self):
        with pytest.raises(ValidationError, match="verification"):
            SchemaVersionMeta(
                version=1,
                initiated_by="system",
                timestamp=_NOW,
                redesigned_groups=[],
                changelog=[],
            )

    def test_roundtrip_serialization(self):
        meta = SchemaVersionMeta(
            version=3,
            base_version=2,
            initiated_by="customer",
            timestamp=_NOW,
            redesigned_groups=["dynamodb", "opensearch"],
            verification=VerificationResult(
                passed=False,
                hard_errors=[
                    VerificationIssue(
                        category="coverage",
                        severity="error",
                        message="Uncovered table",
                        affected_patterns=[],
                        affected_tables=["archive"],
                    )
                ],
                warnings=[],
            ),
            changelog=[],
        )
        json_str = meta.model_dump_json()
        restored = SchemaVersionMeta.model_validate_json(json_str)
        assert restored.version == meta.version
        assert restored.base_version == meta.base_version
        assert restored.initiated_by == meta.initiated_by
        assert restored.verification.passed is False
        assert len(restored.redesigned_groups) == 2


# ---------------------------------------------------------------------------
# SchemaConfirmation
# ---------------------------------------------------------------------------


class TestSchemaConfirmation:
    def test_valid_confirmation(self):
        conf = SchemaConfirmation(
            confirmed_version=2,
            confirmed_at=_NOW,
            engine="dynamodb",
        )
        assert conf.confirmed_version == 2
        assert conf.engine == "dynamodb"

    def test_missing_confirmed_version_fails(self):
        with pytest.raises(ValidationError, match="confirmed_version"):
            SchemaConfirmation(confirmed_at=_NOW, engine="dynamodb")

    def test_missing_confirmed_at_fails(self):
        with pytest.raises(ValidationError, match="confirmed_at"):
            SchemaConfirmation(confirmed_version=1, engine="dynamodb")

    def test_missing_engine_fails(self):
        with pytest.raises(ValidationError, match="engine"):
            SchemaConfirmation(confirmed_version=1, confirmed_at=_NOW)

    def test_roundtrip_serialization(self):
        conf = SchemaConfirmation(
            confirmed_version=5,
            confirmed_at=_NOW,
            engine="opensearch",
        )
        json_str = conf.model_dump_json()
        restored = SchemaConfirmation.model_validate_json(json_str)
        assert restored.confirmed_version == conf.confirmed_version
        assert restored.engine == conf.engine
        assert restored.confirmed_at == conf.confirmed_at
