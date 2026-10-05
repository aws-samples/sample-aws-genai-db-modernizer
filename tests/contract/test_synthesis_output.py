"""Tests for synthesis output contract."""

from datetime import datetime

import pytest
from pydantic import ValidationError

from src.contracts.synthesis_output import (
    AssignmentSummary,
    CostBreakdown,
    EngineRanking,
    MigrationWave,
    Risk,
    RiskAssessment,
    SynthesisOutputContract,
    TableMapping,
    TCOAnalysis,
)


class TestEngineRanking:
    """Test engine ranking model."""

    def test_valid_ranking(self):
        r = EngineRanking(target="dynamodb", confidence_score=85)
        assert r.target == "dynamodb"
        assert r.confidence_score == 85

    def test_full_ranking(self):
        r = EngineRanking(
            target="dynamodb",
            confidence_score=85,
            pattern_score=90,
            complexity_score=70,
            performance_score=95,
            cost_score=80,
            migration_complexity_avg="MEDIUM",
            assigned_queries=42,
            workload_percent=65.5,
        )
        assert r.assigned_queries == 42

    def test_confidence_bounds(self):
        with pytest.raises(ValidationError):
            EngineRanking(target="dynamodb", confidence_score=101)
        with pytest.raises(ValidationError):
            EngineRanking(target="dynamodb", confidence_score=-1)

    def test_routed_confidence_fields_are_optional(self):
        """1.3 (#152): routed confidence next to the analysis average, both optional."""
        legacy = EngineRanking(target="opensearch", confidence_score=2)
        assert legacy.routed_confidence is None
        assert legacy.analysis_confidence is None
        r = EngineRanking(
            target="opensearch",
            confidence_score=2,
            analysis_confidence=2,
            routed_confidence=60,
            routed_confidence_basis="owned_queries",
            routed_queries=3,
            routed_tables=0,
            routed_confidence_evidence="signal_only",
            routed_queries_without_table_evidence=3,
            routed_lead="text_search",
            routed_lead_count=3,
            weight=0.212,
        )
        assert r.routed_confidence_evidence == "signal_only"
        assert (r.routed_lead, r.routed_lead_count) == ("text_search", 3)
        assert (r.analysis_confidence, r.routed_confidence) == (2, 60)
        assert r.routed_confidence_basis == "owned_queries"

    def test_routed_confidence_evidence_values(self):
        with pytest.raises(ValidationError):
            EngineRanking(target="x", confidence_score=1, routed_confidence_evidence="guess")

    def test_routed_confidence_bounds(self):
        with pytest.raises(ValidationError):
            EngineRanking(target="dynamodb", confidence_score=50, routed_confidence=101)
        with pytest.raises(ValidationError):
            EngineRanking(
                target="dynamodb", confidence_score=50, routed_confidence_basis="all_tables"
            )

    def test_extra_fields_allowed(self):
        r = EngineRanking(target="dynamodb", confidence_score=85, custom_field="value")
        assert r.model_extra["custom_field"] == "value"


class TestTableMapping:
    """Test table mapping model."""

    def test_valid_mapping(self):
        m = TableMapping(
            source_table="db.users",
            recommended_database="dynamodb",
            confidence_score=90,
        )
        assert m.source_table == "db.users"

    def test_with_alternatives(self):
        m = TableMapping(
            source_table="db.users",
            recommended_database="dynamodb",
            confidence_score=90,
            alternatives=[{"database": "documentdb", "score": 60}],
        )
        assert len(m.alternatives) == 1


class TestMigrationWave:
    """#225: one step of the incremental migration roadmap."""

    def test_valid_wave(self):
        w = MigrationWave(
            wave=1,
            title="Cache hot reads with ElastiCache",
            engines=["elasticache"],
            table_count=0,
            query_count=20,
            workload_share_percent=83.4,
            share_basis="calls",
            rationale="no data migration, fully reversible",
            gate="cache hit rate verified",
        )
        assert w.wave == 1
        assert w.tables == []  # defaults to empty, not required
        assert w.moves_from == []
        assert w.table_groups is None

    def test_share_basis_rejects_an_unknown_value(self):
        with pytest.raises(ValidationError):
            MigrationWave(
                wave=1,
                title="x",
                engines=["dynamodb"],
                table_count=0,
                query_count=1,
                workload_share_percent=1.0,
                share_basis="rows",
                rationale="x",
                gate="x",
            )

    def test_wave_must_be_at_least_one(self):
        with pytest.raises(ValidationError):
            MigrationWave(
                wave=0,
                title="x",
                engines=["dynamodb"],
                table_count=0,
                query_count=1,
                workload_share_percent=1.0,
                rationale="x",
                gate="x",
            )

    def test_table_groups_for_the_dynamodb_wave(self):
        w = MigrationWave(
            wave=2,
            title="Move key-value and point-lookup queries to DynamoDB",
            engines=["dynamodb"],
            tables=["db.users", "db.sessions"],
            table_count=2,
            table_groups=[{"tables": ["db.users"], "query_count": 10, "kind": "independent"}],
            query_count=10,
            workload_share_percent=50.0,
            rationale="x",
            gate="x",
        )
        # table_groups is typed list[TableGroup] (review finding 15), not raw dicts.
        assert [g.model_dump() for g in w.table_groups] == [
            {"tables": ["db.users"], "query_count": 10, "kind": "independent"}
        ]


class TestTCOAnalysis:
    """Test TCO analysis model."""

    def test_valid_tco(self):
        tco = TCOAnalysis(
            current_monthly_cost=1200.0,
            projected_monthly_cost=450.0,
            savings_percent=62.5,
        )
        assert tco.savings_percent == 62.5

    def test_with_breakdown(self):
        tco = TCOAnalysis(
            current_monthly_cost=1200.0,
            projected_monthly_cost=450.0,
            savings_percent=62.5,
            cost_breakdown=[
                CostBreakdown(database="dynamodb", monthly_cost_usd=300.0),
                CostBreakdown(database="opensearch", monthly_cost_usd=150.0),
            ],
        )
        assert len(tco.cost_breakdown) == 2

    def test_negative_savings_allowed(self):
        """Migration can cost more than current setup."""
        tco = TCOAnalysis(
            current_monthly_cost=500.0,
            projected_monthly_cost=800.0,
            savings_percent=-60.0,
        )
        assert tco.savings_percent == -60.0


class TestRiskAssessment:
    """Test risk assessment model."""

    def test_valid_assessment(self):
        ra = RiskAssessment(
            overall_risk_level="MEDIUM",
            risks=[
                Risk(severity="HIGH", description="Complex joins not fully supported"),
                Risk(severity="LOW", description="Minor schema changes needed"),
            ],
        )
        assert len(ra.risks) == 2
        assert ra.overall_risk_level == "MEDIUM"


class TestAssignmentSummary:
    """Test assignment summary model."""

    def test_valid_summary(self):
        s = AssignmentSummary(
            version=1,
            status="completed",
            query_count=100,
            in_scope_count=95,
            co_dependency_groups=3,
        )
        assert s.in_scope_count == 95

    def test_optional_fields(self):
        s = AssignmentSummary(query_count=50, in_scope_count=50)
        assert s.version is None
        assert s.status is None
        assert s.source is None
        assert s.co_dependency_groups == 0

    def test_source_provenance_roundtrips(self):
        # ADR-028: the report records which stage produced the assignment it used.
        s = AssignmentSummary(
            version=2,
            status="customer_modified",
            source="customer_gate",
            query_count=10,
            in_scope_count=9,
        )
        assert s.source == "customer_gate"
        assert AssignmentSummary.model_validate(s.model_dump()).source == "customer_gate"


class TestSynthesisOutputContract:
    """Test the full synthesis output contract."""

    @pytest.fixture
    def valid_synthesis_data(self):
        return {
            "job_id": "abc123",
            "database_name": "test_db",
            "agent_type": "referee-synthesis",
            "status": "completed",
            "timestamp": "2026-04-23T12:00:00Z",
            "needs_deeper_analysis": False,
            "ranking": [
                {"target": "dynamodb", "confidence_score": 85, "assigned_queries": 60},
                {"target": "documentdb", "confidence_score": 70, "assigned_queries": 40},
            ],
            "summary": "This database is a good candidate for DynamoDB + DocumentDB.",
            "summary_deterministic": "2 engines selected. DynamoDB: 60 queries. DocumentDB: 40 queries.",
            "recommended_architecture": {
                "architecture_type": "MULTI_DATABASE",
                "primary_database": "dynamodb",
                "databases": [
                    {"engine": "dynamodb", "role": "primary"},
                    {"engine": "documentdb", "role": "secondary"},
                ],
            },
            "table_mappings": [
                {
                    "source_table": "db.users",
                    "recommended_database": "dynamodb",
                    "confidence_score": 90,
                },
                {
                    "source_table": "db.posts",
                    "recommended_database": "documentdb",
                    "confidence_score": 75,
                },
            ],
            "query_groups": [
                {
                    "group_name": "user_lookups",
                    "engines": ["dynamodb"],
                    "access_patterns": [{"pattern": "GetItem by PK"}],
                },
            ],
            "tco_analysis": {
                "current_monthly_cost": 1200.0,
                "projected_monthly_cost": 450.0,
                "savings_percent": 62.5,
            },
            "risk_assessment": {
                "overall_risk_level": "MEDIUM",
                "risks": [
                    {"severity": "HIGH", "description": "Complex joins require refactoring"},
                ],
            },
            "schema_designs": {
                "dynamodb": {"status": "completed", "validation_passed": True},
            },
            "trade_offs": [
                {
                    "description": "GSI cost increase for flexible queries",
                    "impact": "Each GSI adds write amplification. For high-write tables this increases cost.",
                    "source_tables": ["db.orders"],
                    "target_tables": ["Orders"],
                    "query_ids": ["q1"],
                    "engine": "dynamodb",
                }
            ],
        }

    def test_valid_synthesis_validates(self, valid_synthesis_data):
        output = SynthesisOutputContract.model_validate(valid_synthesis_data)
        assert output.job_id == "abc123"
        assert len(output.ranking) == 2
        assert output.needs_deeper_analysis is False

    def test_contract_version_defaults(self, valid_synthesis_data):
        output = SynthesisOutputContract.model_validate(valid_synthesis_data)
        assert output.contract_version == "1.5"

    def test_missing_job_id_fails(self, valid_synthesis_data):
        del valid_synthesis_data["job_id"]
        with pytest.raises(ValidationError):
            SynthesisOutputContract.model_validate(valid_synthesis_data)

    def test_missing_ranking_fails(self, valid_synthesis_data):
        del valid_synthesis_data["ranking"]
        with pytest.raises(ValidationError):
            SynthesisOutputContract.model_validate(valid_synthesis_data)

    def test_needs_deeper_analysis_true(self, valid_synthesis_data):
        valid_synthesis_data["needs_deeper_analysis"] = True
        output = SynthesisOutputContract.model_validate(valid_synthesis_data)
        assert output.needs_deeper_analysis is True

    def test_with_assignment_summary(self, valid_synthesis_data):
        valid_synthesis_data["assignment_summary"] = {
            "version": 1,
            "status": "completed",
            "query_count": 100,
            "in_scope_count": 95,
            "co_dependency_groups": 3,
        }
        output = SynthesisOutputContract.model_validate(valid_synthesis_data)
        assert output.assignment_summary is not None
        assert output.assignment_summary.version == 1

    def test_without_assignment_summary(self, valid_synthesis_data):
        output = SynthesisOutputContract.model_validate(valid_synthesis_data)
        assert output.assignment_summary is None

    def test_empty_trade_offs(self, valid_synthesis_data):
        valid_synthesis_data["trade_offs"] = []
        output = SynthesisOutputContract.model_validate(valid_synthesis_data)
        assert output.trade_offs == []

    def test_schema_designs_defaults_to_empty(self, valid_synthesis_data):
        del valid_synthesis_data["schema_designs"]
        output = SynthesisOutputContract.model_validate(valid_synthesis_data)
        assert output.schema_designs == {}

    def test_roundtrip_serialization(self, valid_synthesis_data):
        """Validate that model_dump(mode='json') produces data that re-validates."""
        output = SynthesisOutputContract.model_validate(valid_synthesis_data)
        dumped = output.model_dump(mode="json")
        roundtrip = SynthesisOutputContract.model_validate(dumped)
        assert roundtrip.job_id == output.job_id
        assert len(roundtrip.ranking) == len(output.ranking)
        assert roundtrip.tco_analysis.savings_percent == output.tco_analysis.savings_percent

    def test_timestamp_parsing(self, valid_synthesis_data):
        output = SynthesisOutputContract.model_validate(valid_synthesis_data)
        assert isinstance(output.timestamp, datetime)

    def test_extra_fields_allowed_on_root(self, valid_synthesis_data):
        """Step Functions may add extra fields we don't know about."""
        valid_synthesis_data["synthesis_iteration"] = 2
        output = SynthesisOutputContract.model_validate(valid_synthesis_data)
        assert output.model_extra["synthesis_iteration"] == 2

    def test_empty_ranking_allowed(self, valid_synthesis_data):
        valid_synthesis_data["ranking"] = []
        output = SynthesisOutputContract.model_validate(valid_synthesis_data)
        assert output.ranking == []

    def test_migration_waves_defaults_to_none(self, valid_synthesis_data):
        """1.4 (#225): absent on a report synthesized before the roadmap existed."""
        output = SynthesisOutputContract.model_validate(valid_synthesis_data)
        assert output.migration_waves is None

    def test_with_migration_waves(self, valid_synthesis_data):
        valid_synthesis_data["migration_waves"] = [
            {
                "wave": 1,
                "title": "Cache hot reads with ElastiCache",
                "engines": ["elasticache"],
                "tables": [],
                "table_count": 0,
                "query_count": 20,
                "workload_share_percent": 83.4,
                "share_basis": "calls",
                "rationale": "no data migration, fully reversible",
                "gate": "cache hit rate verified",
            },
            {
                "wave": 2,
                "title": "Move key-value and point-lookup queries to DynamoDB",
                "engines": ["dynamodb"],
                "tables": ["db.users"],
                "table_count": 1,
                "query_count": 60,
                "workload_share_percent": 60.0,
                "share_basis": "queries",
                "rationale": "pattern DynamoDB fits best",
                "gate": "query parity confirmed",
            },
        ]
        output = SynthesisOutputContract.model_validate(valid_synthesis_data)
        assert output.migration_waves is not None
        assert [w.wave for w in output.migration_waves] == [1, 2]
        assert output.migration_waves[0].share_basis == "calls"
        assert output.migration_waves[1].engines == ["dynamodb"]
        dumped = output.model_dump(mode="json")
        roundtrip = SynthesisOutputContract.model_validate(dumped)
        assert len(roundtrip.migration_waves) == 2

    def test_status_defaults(self, valid_synthesis_data):
        del valid_synthesis_data["status"]
        output = SynthesisOutputContract.model_validate(valid_synthesis_data)
        assert output.status == "completed"
