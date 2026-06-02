"""Tests for triage output contract."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from src.contracts.triage_output import (
    DeferredAgent,
    SelectedAgent,
    SkippedAgent,
    TriageOutputContract,
    TriageSignalRecord,
)


class TestTriageSignalRecord:
    """Test individual signal record validation."""

    def test_valid_signal(self):
        sig = TriageSignalRecord(
            signal="text_search",
            targets=["opensearch"],
            evidence="3 queries use LIKE/ILIKE patterns",
            query_ids=["q1", "q2", "q3"],
            table_ids=["db.posts"],
            query_count=3,
        )
        assert sig.signal == "text_search"
        assert len(sig.targets) == 1
        assert sig.query_count == 3

    def test_signal_defaults(self):
        sig = TriageSignalRecord(
            signal="key_value_lookups",
            targets=["dynamodb", "elasticache"],
            evidence="PK reads detected",
        )
        assert sig.query_ids == []
        assert sig.table_ids == []
        assert sig.query_count == 0

    def test_signal_negative_query_count_rejected(self):
        with pytest.raises(ValidationError):
            TriageSignalRecord(
                signal="key_value_lookups",
                targets=["dynamodb"],
                evidence="PK reads",
                query_count=-1,
            )

    def test_signal_missing_required_field(self):
        with pytest.raises(ValidationError):
            TriageSignalRecord(
                signal="text_search",
                # targets missing
                evidence="queries use LIKE",
            )


class TestSelectedSkippedDeferred:
    """Test agent selection models."""

    def test_selected_agent(self):
        agent = SelectedAgent(
            agent_type="dynamodb",
            reasons=["Key-value lookups detected", "High-frequency reads"],
        )
        assert agent.agent_type == "dynamodb"
        assert len(agent.reasons) == 2

    def test_skipped_agent(self):
        agent = SkippedAgent(
            agent_type="opensearch",
            reason="No text search or aggregation signals",
        )
        assert agent.reason == "No text search or aggregation signals"

    def test_deferred_agent(self):
        agent = DeferredAgent(
            agent_type="elasticache",
            reasons=["Leaderboard pattern detected but low priority"],
        )
        assert len(agent.reasons) == 1


class TestTriageOutputContract:
    """Test the full triage output contract."""

    @pytest.fixture
    def valid_triage_data(self):
        return {
            "job_id": "abc123",
            "database_name": "test_db",
            "agent_type": "referee-triage",
            "selected_agents": [
                {"agent_type": "dynamodb", "reasons": ["Key-value lookups"]},
                {"agent_type": "documentdb", "reasons": ["Complex joins"]},
            ],
            "skipped_agents": [
                {"agent_type": "opensearch", "reason": "No text search signals"},
            ],
            "baseline": {"aurora": ["sql_compatibility"]},
            "deferred_agents": [],
            "signals": [
                {
                    "signal": "key_value_lookups",
                    "targets": ["dynamodb"],
                    "evidence": "15 queries with PK-only access",
                    "query_ids": ["q1", "q2"],
                    "table_ids": ["db.users"],
                    "query_count": 15,
                },
                {
                    "signal": "complex_joins",
                    "targets": ["documentdb"],
                    "evidence": "4 queries with 3+ table JOINs",
                    "query_ids": ["q10"],
                    "table_ids": [],
                    "query_count": 4,
                },
            ],
            "confidence_score": 75,
            "timestamp": "2026-04-23T12:00:00Z",
        }

    def test_valid_triage_validates(self, valid_triage_data):
        output = TriageOutputContract.model_validate(valid_triage_data)
        assert output.job_id == "abc123"
        assert len(output.selected_agents) == 2
        assert len(output.skipped_agents) == 1
        assert len(output.signals) == 2
        assert output.confidence_score == 75

    def test_agent_type_defaults(self, valid_triage_data):
        del valid_triage_data["agent_type"]
        output = TriageOutputContract.model_validate(valid_triage_data)
        assert output.agent_type == "referee-triage"

    def test_missing_job_id_fails(self, valid_triage_data):
        del valid_triage_data["job_id"]
        with pytest.raises(ValidationError):
            TriageOutputContract.model_validate(valid_triage_data)

    def test_missing_signals_fails(self, valid_triage_data):
        del valid_triage_data["signals"]
        with pytest.raises(ValidationError):
            TriageOutputContract.model_validate(valid_triage_data)

    def test_confidence_score_bounds(self, valid_triage_data):
        valid_triage_data["confidence_score"] = 101
        with pytest.raises(ValidationError):
            TriageOutputContract.model_validate(valid_triage_data)

        valid_triage_data["confidence_score"] = -1
        with pytest.raises(ValidationError):
            TriageOutputContract.model_validate(valid_triage_data)

    def test_confidence_score_edge_values(self, valid_triage_data):
        valid_triage_data["confidence_score"] = 0
        output = TriageOutputContract.model_validate(valid_triage_data)
        assert output.confidence_score == 0

        valid_triage_data["confidence_score"] = 100
        output = TriageOutputContract.model_validate(valid_triage_data)
        assert output.confidence_score == 100

    def test_empty_selected_agents_allowed(self, valid_triage_data):
        valid_triage_data["selected_agents"] = []
        output = TriageOutputContract.model_validate(valid_triage_data)
        assert output.selected_agents == []

    def test_baseline_defaults_to_empty(self, valid_triage_data):
        del valid_triage_data["baseline"]
        output = TriageOutputContract.model_validate(valid_triage_data)
        assert output.baseline == {}

    def test_deferred_agents_defaults_to_empty(self, valid_triage_data):
        del valid_triage_data["deferred_agents"]
        output = TriageOutputContract.model_validate(valid_triage_data)
        assert output.deferred_agents == []

    def test_roundtrip_serialization(self, valid_triage_data):
        """Validate that model_dump(mode='json') produces data that re-validates."""
        output = TriageOutputContract.model_validate(valid_triage_data)
        dumped = output.model_dump(mode="json")
        roundtrip = TriageOutputContract.model_validate(dumped)
        assert roundtrip.job_id == output.job_id
        assert len(roundtrip.signals) == len(output.signals)
        assert roundtrip.confidence_score == output.confidence_score

    def test_timestamp_parsing(self, valid_triage_data):
        output = TriageOutputContract.model_validate(valid_triage_data)
        assert isinstance(output.timestamp, datetime)

    def test_timestamp_as_datetime_object(self, valid_triage_data):
        valid_triage_data["timestamp"] = datetime.now(UTC)
        output = TriageOutputContract.model_validate(valid_triage_data)
        assert isinstance(output.timestamp, datetime)
