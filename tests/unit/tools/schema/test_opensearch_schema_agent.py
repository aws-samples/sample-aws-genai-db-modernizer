"""Tests for OpenSearch schema design agent."""

import json
import os
from unittest.mock import MagicMock, patch

from src.contracts.opensearch_model_output import (
    CustomAnalyzer,
    DataStreamConfig,
    FieldMapping,
    IndexMapping,
    IndexSettings,
    IndexTemplate,
    ISMPolicy,
    OpenSearchModelOutputContract,
)
from src.contracts.schema_design_output import TradeOff


class TestOutputContract:
    def test_minimal_contract_validates(self):
        contract = OpenSearchModelOutputContract(
            job_id="test-job",
            source_database="testdb",
            index_designs=[],
            data_stream_designs=[],
            access_patterns=[],
            unsupported_patterns=[],
            trade_offs=[],
            validation_passed=True,
        )
        assert contract.contract_version == "1.0"

    def test_contract_with_index_mapping(self):
        mapping = IndexMapping(
            index_name="products",
            source_tables=["public.products"],
            settings=IndexSettings(
                number_of_shards=3,
                assumed_node_count=3,
                shard_sizing_rationale="15GB data / 3 nodes = 5GB per shard",
            ),
            field_mappings=[
                FieldMapping(
                    field_name="description",
                    field_type="text",
                    source_column="description",
                    analyzer="standard",
                    multi_field=True,
                ),
            ],
        )
        contract = OpenSearchModelOutputContract(
            job_id="test-job",
            source_database="testdb",
            index_designs=[mapping],
            data_stream_designs=[],
            access_patterns=[],
            unsupported_patterns=[],
            trade_offs=[],
            validation_passed=True,
        )
        assert len(contract.index_designs) == 1
        assert contract.index_designs[0].index_name == "products"

    def test_contract_with_data_stream(self):
        ds = DataStreamConfig(
            data_stream_name="application-logs",
            source_tables=["public.application_logs"],
            timestamp_field="log_time",
            index_template=IndexTemplate(
                template_name="application-logs-template",
                index_patterns=["application-logs-*"],
                settings=IndexSettings(
                    number_of_shards=6,
                    assumed_node_count=3,
                    shard_sizing_rationale="2TB data, 50GB/shard target, 40 shards rounded to 42 (multiple of 3) — using 6 for initial rollover",
                    refresh_interval="30s",
                ),
                field_mappings=[
                    FieldMapping(
                        field_name="@timestamp", field_type="date", source_column="log_time"
                    ),
                    FieldMapping(field_name="message", field_type="text", source_column="message"),
                ],
            ),
            ism_policy=ISMPolicy(
                policy_name="logs-lifecycle",
                hot_phase_days=7,
                warm_phase_days=30,
                delete_after_days=90,
            ),
        )
        contract = OpenSearchModelOutputContract(
            job_id="test-job",
            source_database="testdb",
            index_designs=[],
            data_stream_designs=[ds],
            access_patterns=[],
            unsupported_patterns=[],
            trade_offs=[],
            validation_passed=True,
        )
        assert len(contract.data_stream_designs) == 1
        assert contract.data_stream_designs[0].ism_policy.hot_phase_days == 7

    def test_contract_with_custom_analyzer(self):
        settings = IndexSettings(
            number_of_shards=3,
            assumed_node_count=3,
            shard_sizing_rationale="small dataset",
            custom_analyzers=[
                CustomAnalyzer(
                    name="autocomplete",
                    tokenizer="edge_ngram",
                    filter=["lowercase"],
                ),
            ],
        )
        assert len(settings.custom_analyzers) == 1
        assert settings.custom_analyzers[0].tokenizer == "edge_ngram"

    def test_contract_roundtrip_serialization(self):
        contract = OpenSearchModelOutputContract(
            job_id="test-job",
            source_database="testdb",
            index_designs=[],
            data_stream_designs=[],
            access_patterns=[],
            unsupported_patterns=[],
            trade_offs=[
                TradeOff(
                    description="Some trade-off",
                    impact="Some practical impact",
                    source_tables=["db.products"],
                    target_tables=["products"],
                    query_ids=["q1"],
                    engine="opensearch",
                )
            ],
            validation_passed=True,
        )
        json_str = contract.model_dump_json()
        restored = OpenSearchModelOutputContract.model_validate_json(json_str)
        assert restored.job_id == "test-job"
        assert restored.trade_offs[0].description == "Some trade-off"


class TestSkillLoading:
    def test_designer_skill_exists(self):
        from pathlib import Path

        skill_path = Path("src/skills/opensearch-index-modeling.md")
        assert skill_path.exists(), f"Skill file not found: {skill_path}"
        content = skill_path.read_text()
        assert len(content) > 100
        assert "OpenSearch" in content

    def test_pe_reviewer_skill_exists(self):
        from pathlib import Path

        skill_path = Path("src/skills/opensearch-pe-review.md")
        assert skill_path.exists(), f"Skill file not found: {skill_path}"
        content = skill_path.read_text()
        assert len(content) > 50
        assert "PEReviewResult" in content


class TestLoadAgentInput:
    """Test the load_agent_input function (mocked file reads)."""

    def test_loads_collector_and_analysis(self, tmp_path):
        # Create minimal collector output
        collector = {
            "contract_version": "3.0",
            "job_id": "test-job",
            "metadata": {
                "collection_timestamp": "2026-01-01",
                "collector_version": "1.0.0",
                "source_database": {"engine": "mysql", "version": "8.0", "hostname": "test"},
            },
            "database_schema": {
                "tables": [
                    {
                        "table_id": "t1",
                        "table_name": "t1",
                        "row_count": 100,
                        "size_mb": 1.0,
                        "columns": [{"column_name": "id", "data_type": "int", "nullable": False}],
                        "primary_key": ["id"],
                    }
                ]
            },
            "queries": {"query_patterns": []},
            "metrics": {"performance_metrics": {}},
        }
        # Create minimal analysis output
        analysis = {
            "contract_version": "2.1",
            "agent_metadata": {
                "agent_name": "opensearch-analysis-agent",
                "agent_version": "1.0.0",
                "target_database": "opensearch",
                "analysis_timestamp": "2026-01-01T00:00:00",
                "analysis_duration_seconds": 1.0,
            },
            "table_recommendations": [],
            "workload_analysis": {"patterns_detected": []},
            "cost_estimate": {
                "monthly_cost_usd": 0,
                "cost_components": {},
                "pricing_assumptions": [],
            },
        }
        # Create decision trace
        trace = {"trace_version": "1.0", "workload_classifications": []}

        collector_path = tmp_path / "collector.json"
        analysis_path = tmp_path / "analysis.json"
        trace_path = tmp_path / "trace.json"
        collector_path.write_text(json.dumps(collector))
        analysis_path.write_text(json.dumps(analysis))
        trace_path.write_text(json.dumps(trace))

        with patch.dict(
            os.environ,
            {
                "COLLECTOR_OUTPUT_PATH": str(collector_path),
                "ANALYSIS_OUTPUT_PATH": str(analysis_path),
                "DECISION_TRACE_PATH": str(trace_path),
            },
        ):
            from src.tools.schema.opensearch_schema_agent import load_agent_input

            result = load_agent_input()

        assert "collector" in result
        assert "analysis" in result
        assert "context" in result
        assert "decision_trace" in result
        assert result["decision_trace"]["trace_version"] == "1.0"

    def test_raises_without_env_vars(self):
        import importlib

        import pytest

        with patch.dict(os.environ, {}, clear=True):
            import src.tools.schema.opensearch_schema_agent as agent_mod

            importlib.reload(agent_mod)
            with pytest.raises(ValueError, match="COLLECTOR_OUTPUT_PATH"):
                agent_mod.load_agent_input()


class TestPEReviewModels:
    """Test PE review contract models."""

    def test_review_result_approved(self):
        from src.tools.schema.opensearch_schema_agent import PEReviewResult, ReviewVerdict

        result = PEReviewResult(verdict=ReviewVerdict.APPROVED, summary="Looks good")
        assert result.verdict == ReviewVerdict.APPROVED

    def test_review_result_with_changes(self):
        from src.tools.schema.opensearch_schema_agent import (
            ChangeCategory,
            ChangeRequest,
            ChangeSeverity,
            PEReviewResult,
            ReviewVerdict,
        )

        result = PEReviewResult(
            verdict=ReviewVerdict.CHANGES_REQUESTED,
            change_requests=[
                ChangeRequest(
                    category=ChangeCategory.SHARD_SIZING,
                    severity=ChangeSeverity.BLOCKER,
                    target="products index",
                    requested_change="Use 3 shards instead of 1",
                    rationale="3-node cluster needs shards in multiples of 3",
                )
            ],
            summary="Shard sizing issue",
        )
        assert len(result.change_requests) == 1
        assert result.change_requests[0].category == ChangeCategory.SHARD_SIZING


class TestHandlerDispatch:
    """Test that handler.py routes opensearch correctly."""

    @patch("boto3.client")
    def test_opensearch_dispatches_to_schema_agent(self, mock_boto):
        """Verify the opensearch case exists and calls the right function."""
        from src.agents.schema_design.handler import _dispatch_schema_agent

        # Mock the schema agent to return a minimal valid output
        with patch(
            "src.tools.schema.opensearch_schema_agent.run_opensearch_schema_agent"
        ) as mock_run:
            mock_output = MagicMock()
            mock_output.model_dump_json.return_value = '{"contract_version": "1.0"}'
            mock_run.return_value = (mock_output, {"trace": "data"})

            output_json, trace_json = _dispatch_schema_agent("opensearch")

        mock_run.assert_called_once()
        assert '"contract_version"' in output_json
        assert trace_json is not None


# ---------------------------------------------------------------------------
# Helpers shared across new test classes
# ---------------------------------------------------------------------------


def _minimal_output() -> OpenSearchModelOutputContract:
    return OpenSearchModelOutputContract(
        job_id="test",
        source_database="testdb",
        index_designs=[],
        data_stream_designs=[],
        access_patterns=[],
        unsupported_patterns=[],
        trade_offs=[],
        validation_passed=True,
    )


# ---------------------------------------------------------------------------
# SchemaDesignTrace
# ---------------------------------------------------------------------------


class TestSchemaDesignTrace:
    """Tests for SchemaDesignTrace accumulation helpers."""

    def test_get_or_create_creates_entries_in_order(self):
        from src.tools.schema.opensearch_schema_agent import SchemaDesignTrace

        trace = SchemaDesignTrace()
        entry0 = trace._get_or_create(0)
        assert entry0["iteration"] == 1
        assert len(trace.iterations) == 1

        # Requesting index 2 should backfill 0,1,2
        entry2 = trace._get_or_create(2)
        assert entry2["iteration"] == 3
        assert len(trace.iterations) == 3

    def test_log_designer_populates_entry(self):
        from src.tools.schema.opensearch_schema_agent import SchemaDesignTrace

        trace = SchemaDesignTrace()
        output = _minimal_output()
        trace.log_designer(0, 1.5, output)

        entry = trace.iterations[0]
        assert "designer" in entry
        assert entry["designer"]["duration_seconds"] == 1.5
        assert entry["designer"]["index_designs"] == 0
        assert entry["designer"]["data_stream_designs"] == 0
        assert entry["designer"]["access_patterns"] == 0
        assert entry["designer"]["validation_passed"] is True

    def test_log_pe_review_populates_entry(self):
        from src.tools.schema.opensearch_schema_agent import (
            ChangeCategory,
            ChangeRequest,
            ChangeSeverity,
            PEReviewResult,
            ReviewVerdict,
            SchemaDesignTrace,
        )

        trace = SchemaDesignTrace()
        review = PEReviewResult(
            verdict=ReviewVerdict.CHANGES_REQUESTED,
            change_requests=[
                ChangeRequest(
                    category=ChangeCategory.SHARD_SIZING,
                    severity=ChangeSeverity.BLOCKER,
                    target="products",
                    requested_change="Use 3 shards",
                    rationale="cluster size",
                )
            ],
            strengths=["good field mapping"],
            pe_notes=["consider aliases"],
            summary="shard issue",
        )
        trace.log_pe_review(0, 2.25, review)

        pe = trace.iterations[0]["pe_review"]
        assert pe["verdict"] == "CHANGES_REQUESTED"
        assert len(pe["change_requests"]) == 1
        assert pe["change_requests"][0]["category"] == "shard_sizing"
        assert pe["change_requests"][0]["severity"] == "blocker"
        assert pe["change_requests"][0]["target"] == "products"
        assert pe["strengths"] == ["good field mapping"]
        assert pe["pe_notes"] == ["consider aliases"]
        assert pe["summary"] == "shard issue"
        assert pe["duration_seconds"] == 2.25

    def test_log_pe_error_populates_entry(self):
        from src.tools.schema.opensearch_schema_agent import SchemaDesignTrace

        trace = SchemaDesignTrace()
        trace.log_pe_error(0, "timeout error")

        pe = trace.iterations[0]["pe_review"]
        assert pe == {"error": "timeout error"}

    def test_to_dict_structure(self):
        from src.tools.schema.opensearch_schema_agent import SchemaDesignTrace

        trace = SchemaDesignTrace()
        output = _minimal_output()
        trace.log_designer(0, 1.0, output)

        result = trace.to_dict()
        assert "total_duration_seconds" in result
        assert result["total_iterations"] == 1
        assert len(result["iterations"]) == 1
        assert isinstance(result["total_duration_seconds"], float)

    def test_to_dict_multiple_iterations(self):
        from src.tools.schema.opensearch_schema_agent import (
            PEReviewResult,
            ReviewVerdict,
            SchemaDesignTrace,
        )

        trace = SchemaDesignTrace()
        output = _minimal_output()
        trace.log_designer(0, 1.0, output)
        review = PEReviewResult(verdict=ReviewVerdict.APPROVED, summary="looks good")
        trace.log_pe_review(0, 0.5, review)
        trace.log_designer(1, 1.2, output)

        result = trace.to_dict()
        assert result["total_iterations"] == 2


# ---------------------------------------------------------------------------
# _build_model
# ---------------------------------------------------------------------------


class TestLoadSkill:
    """Tests for the _load_skill file-reading helper."""

    def test_reads_file_contents(self, tmp_path):
        from src.tools.schema.opensearch_schema_agent import _load_skill

        skill_file = tmp_path / "skill.md"
        skill_file.write_text("# My Skill\nSome content", encoding="utf-8")

        content = _load_skill(str(skill_file))
        assert content == "# My Skill\nSome content"


class TestBuildModel:
    """Tests for _build_model BedrockModel construction."""

    @patch("src.tools.schema.opensearch_schema_agent.BedrockModel")
    def test_build_model_default_uses_opus(self, mock_bedrock_model):
        from src.tools.schema.opensearch_schema_agent import _build_model

        with patch.dict(
            os.environ,
            {
                "BEDROCK_MODEL_ID": "us.anthropic.claude-opus-4-6-v1",
                "SCHEMA_AGENT_MAX_TOKENS": "16384",
                "AWS_REGION": "us-east-1",
            },
        ):
            _build_model()

        mock_bedrock_model.assert_called_once()
        call_kwargs = mock_bedrock_model.call_args.kwargs
        assert call_kwargs["model_id"] == "us.anthropic.claude-opus-4-6-v1"
        assert call_kwargs["max_tokens"] == 16384
        assert call_kwargs["temperature"] == 1.0
        assert call_kwargs["region_name"] == "us-east-1"
        # Opus → thinking enabled
        assert call_kwargs["additional_request_fields"]["thinking"]["type"] == "enabled"

    @patch("src.tools.schema.opensearch_schema_agent.BedrockModel")
    def test_build_model_non_opus_no_thinking(self, mock_bedrock_model):
        from src.tools.schema.opensearch_schema_agent import _build_model

        with patch.dict(
            os.environ,
            {
                "BEDROCK_MODEL_ID": "us.anthropic.claude-sonnet-4-5-v1",
                "SCHEMA_AGENT_MAX_TOKENS": "8192",
                "AWS_REGION": "eu-west-1",
            },
        ):
            _build_model()

        call_kwargs = mock_bedrock_model.call_args.kwargs
        assert call_kwargs["model_id"] == "us.anthropic.claude-sonnet-4-5-v1"
        assert call_kwargs["max_tokens"] == 8192
        assert call_kwargs["region_name"] == "eu-west-1"
        # Non-opus → no thinking field
        assert call_kwargs["additional_request_fields"] == {}

    @patch("src.tools.schema.opensearch_schema_agent.BedrockModel")
    def test_build_model_uses_boto_config(self, mock_bedrock_model):
        """Verify a botocore Config is passed (read_timeout=300)."""
        from botocore.config import Config

        from src.tools.schema.opensearch_schema_agent import _build_model

        with patch.dict(os.environ, {"BEDROCK_MODEL_ID": "us.anthropic.claude-opus-4-6-v1"}):
            _build_model()

        call_kwargs = mock_bedrock_model.call_args.kwargs
        assert isinstance(call_kwargs["boto_client_config"], Config)


# ---------------------------------------------------------------------------
# _invoke_designer
# ---------------------------------------------------------------------------


class TestInvokeDesigner:
    """Tests for _invoke_designer extraction logic."""

    def test_extracts_structured_output_directly(self):
        from src.tools.schema.opensearch_schema_agent import _invoke_designer

        output = _minimal_output()
        mock_result = MagicMock()
        mock_result.structured_output = output

        mock_agent = MagicMock()
        mock_agent.return_value = mock_result

        result = _invoke_designer(mock_agent, "design prompt")
        assert result is output
        mock_agent.assert_called_once_with("design prompt")

    def test_falls_back_to_json_string_parsing(self):
        from src.tools.schema.opensearch_schema_agent import _invoke_designer

        output = _minimal_output()
        json_str = output.model_dump_json()

        mock_result = MagicMock()
        mock_result.structured_output = None  # not a contract instance
        mock_result.__str__ = MagicMock(return_value=json_str)

        mock_agent = MagicMock()
        mock_agent.return_value = mock_result

        result = _invoke_designer(mock_agent, "design prompt")
        assert isinstance(result, OpenSearchModelOutputContract)
        assert result.job_id == "test"

    def test_structured_output_wrong_type_triggers_fallback(self):
        from src.tools.schema.opensearch_schema_agent import _invoke_designer

        output = _minimal_output()
        json_str = output.model_dump_json()

        mock_result = MagicMock()
        mock_result.structured_output = {"not": "a contract"}  # wrong type
        mock_result.__str__ = MagicMock(return_value=json_str)

        mock_agent = MagicMock()
        mock_agent.return_value = mock_result

        result = _invoke_designer(mock_agent, "design prompt")
        assert isinstance(result, OpenSearchModelOutputContract)


# ---------------------------------------------------------------------------
# _invoke_pe_reviewer
# ---------------------------------------------------------------------------


class TestInvokePeReviewer:
    """Tests for _invoke_pe_reviewer agent creation and result extraction."""

    @patch("src.tools.schema.opensearch_schema_agent.Agent")
    @patch("src.tools.schema.opensearch_schema_agent._load_skill")
    def test_extracts_structured_output_directly(self, mock_load_skill, mock_agent_cls):
        from src.tools.schema.opensearch_schema_agent import (
            PEReviewResult,
            ReviewVerdict,
            _invoke_pe_reviewer,
        )

        mock_load_skill.return_value = "pe skill text"
        approved = PEReviewResult(verdict=ReviewVerdict.APPROVED, summary="good")

        mock_result = MagicMock()
        mock_result.structured_output = approved
        mock_pe_agent = MagicMock(return_value=mock_result)
        mock_agent_cls.return_value = mock_pe_agent

        mock_model = MagicMock()
        output = _minimal_output()
        result = _invoke_pe_reviewer(mock_model, output, {"table_count": 0})

        assert result is approved
        mock_agent_cls.assert_called_once()
        # Verify structured_output_model passed as PEReviewResult
        call_kwargs = mock_agent_cls.call_args.kwargs
        assert call_kwargs["structured_output_model"] is PEReviewResult

    @patch("src.tools.schema.opensearch_schema_agent.Agent")
    @patch("src.tools.schema.opensearch_schema_agent._load_skill")
    def test_falls_back_to_json_string(self, mock_load_skill, mock_agent_cls):
        from src.tools.schema.opensearch_schema_agent import (
            PEReviewResult,
            ReviewVerdict,
            _invoke_pe_reviewer,
        )

        mock_load_skill.return_value = "pe skill"
        approved = PEReviewResult(verdict=ReviewVerdict.APPROVED, summary="fine")
        json_str = approved.model_dump_json()

        mock_result = MagicMock()
        mock_result.structured_output = None
        mock_result.__str__ = MagicMock(return_value=json_str)
        mock_pe_agent = MagicMock(return_value=mock_result)
        mock_agent_cls.return_value = mock_pe_agent

        mock_model = MagicMock()
        output = _minimal_output()
        result = _invoke_pe_reviewer(mock_model, output, {"table_count": 0})

        assert isinstance(result, PEReviewResult)
        assert result.verdict == ReviewVerdict.APPROVED

    @patch("src.tools.schema.opensearch_schema_agent.Agent")
    @patch("src.tools.schema.opensearch_schema_agent._load_skill")
    def test_uses_custom_pe_skill_path(self, mock_load_skill, mock_agent_cls):
        from src.tools.schema.opensearch_schema_agent import (
            PEReviewResult,
            ReviewVerdict,
            _invoke_pe_reviewer,
        )

        mock_load_skill.return_value = "custom pe skill"
        approved = PEReviewResult(verdict=ReviewVerdict.APPROVED, summary="ok")
        mock_result = MagicMock()
        mock_result.structured_output = approved
        mock_agent_cls.return_value = MagicMock(return_value=mock_result)

        mock_model = MagicMock()
        output = _minimal_output()
        _invoke_pe_reviewer(mock_model, output, {"table_count": 2}, pe_skill_path="/custom/pe.md")

        mock_load_skill.assert_called_once_with("/custom/pe.md")

    @patch("src.tools.schema.opensearch_schema_agent.Agent")
    @patch("src.tools.schema.opensearch_schema_agent._load_skill")
    def test_prints_verdict_for_structured_output(self, mock_load_skill, mock_agent_cls, capsys):
        from src.tools.schema.opensearch_schema_agent import (
            ChangeCategory,
            ChangeRequest,
            ChangeSeverity,
            PEReviewResult,
            ReviewVerdict,
            _invoke_pe_reviewer,
        )

        mock_load_skill.return_value = "pe skill"
        review = PEReviewResult(
            verdict=ReviewVerdict.CHANGES_REQUESTED,
            change_requests=[
                ChangeRequest(
                    category=ChangeCategory.FIELD_TYPE,
                    severity=ChangeSeverity.WARNING,
                    target="idx",
                    requested_change="change it",
                    rationale="reason",
                )
            ],
            strengths=["nice mapping"],
            summary="needs work",
        )
        mock_result = MagicMock()
        mock_result.structured_output = review
        mock_agent_cls.return_value = MagicMock(return_value=mock_result)

        _invoke_pe_reviewer(MagicMock(), _minimal_output(), {"table_count": 1})
        captured = capsys.readouterr()
        assert "CHANGES_REQUESTED" in captured.out


# ---------------------------------------------------------------------------
# _format_pe_feedback
# ---------------------------------------------------------------------------


class TestFormatPeFeedback:
    """Tests for _format_pe_feedback string construction."""

    def test_with_change_requests_and_strengths(self):
        from src.tools.schema.opensearch_schema_agent import (
            ChangeCategory,
            ChangeRequest,
            ChangeSeverity,
            PEReviewResult,
            ReviewVerdict,
            _format_pe_feedback,
        )

        review = PEReviewResult(
            verdict=ReviewVerdict.CHANGES_REQUESTED,
            change_requests=[
                ChangeRequest(
                    category=ChangeCategory.SHARD_SIZING,
                    severity=ChangeSeverity.BLOCKER,
                    target="products",
                    requested_change="Use 3 shards",
                    rationale="cluster alignment",
                )
            ],
            strengths=["well chosen analyzers"],
            summary="mostly good, one blocker",
        )
        text = _format_pe_feedback(review)

        assert "PE Review Summary" in text
        assert "mostly good, one blocker" in text
        assert "Change Requests" in text
        assert "shard_sizing" in text
        assert "blocker" in text
        assert "products" in text
        assert "Use 3 shards" in text
        assert "cluster alignment" in text
        assert "Strengths" in text
        assert "well chosen analyzers" in text

    def test_with_no_change_requests(self):
        from src.tools.schema.opensearch_schema_agent import (
            PEReviewResult,
            ReviewVerdict,
            _format_pe_feedback,
        )

        review = PEReviewResult(
            verdict=ReviewVerdict.APPROVED,
            change_requests=[],
            strengths=["great design"],
            summary="approved",
        )
        text = _format_pe_feedback(review)

        assert "Change Requests" not in text
        assert "Strengths" in text
        assert "great design" in text

    def test_with_no_strengths(self):
        from src.tools.schema.opensearch_schema_agent import (
            ChangeCategory,
            ChangeRequest,
            ChangeSeverity,
            PEReviewResult,
            ReviewVerdict,
            _format_pe_feedback,
        )

        review = PEReviewResult(
            verdict=ReviewVerdict.CHANGES_REQUESTED,
            change_requests=[
                ChangeRequest(
                    category=ChangeCategory.ISM_POLICY,
                    severity=ChangeSeverity.WARNING,
                    target="logs-stream",
                    requested_change="Add cold phase",
                    rationale="cost reduction",
                )
            ],
            strengths=[],
            summary="minor fix needed",
        )
        text = _format_pe_feedback(review)

        assert "Change Requests" in text
        assert "Strengths" not in text

    def test_with_no_change_requests_and_no_strengths(self):
        from src.tools.schema.opensearch_schema_agent import (
            PEReviewResult,
            ReviewVerdict,
            _format_pe_feedback,
        )

        review = PEReviewResult(
            verdict=ReviewVerdict.APPROVED,
            summary="perfect",
        )
        text = _format_pe_feedback(review)

        assert "PE Review Summary" in text
        assert "perfect" in text
        assert "Change Requests" not in text
        assert "Strengths" not in text


# ---------------------------------------------------------------------------
# run_opensearch_schema_agent (full orchestration)
# ---------------------------------------------------------------------------


class TestRunOpensearchSchemaAgent:
    """Integration-style tests for run_opensearch_schema_agent with all LLM calls mocked."""

    def _make_approved_review(self):
        from src.tools.schema.opensearch_schema_agent import PEReviewResult, ReviewVerdict

        return PEReviewResult(verdict=ReviewVerdict.APPROVED, summary="great")

    def _make_changes_requested_review(self):
        from src.tools.schema.opensearch_schema_agent import (
            ChangeCategory,
            ChangeRequest,
            ChangeSeverity,
            PEReviewResult,
            ReviewVerdict,
        )

        return PEReviewResult(
            verdict=ReviewVerdict.CHANGES_REQUESTED,
            change_requests=[
                ChangeRequest(
                    category=ChangeCategory.SHARD_SIZING,
                    severity=ChangeSeverity.BLOCKER,
                    target="idx",
                    requested_change="3 shards",
                    rationale="cluster alignment",
                )
            ],
            pe_notes=["remember replication factor"],
            summary="needs shard fix",
        )

    @patch("src.tools.schema.opensearch_schema_agent._invoke_pe_reviewer")
    @patch("src.tools.schema.opensearch_schema_agent._invoke_designer")
    @patch("src.tools.schema.opensearch_schema_agent.Agent")
    @patch("src.tools.schema.opensearch_schema_agent._load_skill")
    @patch("src.tools.schema.opensearch_schema_agent._build_model")
    def test_pe_approved_on_first_iteration(
        self, mock_build, mock_load, mock_agent_cls, mock_designer, mock_pe
    ):
        from src.tools.schema.opensearch_schema_agent import run_opensearch_schema_agent

        mock_build.return_value = MagicMock()
        mock_load.return_value = "skill text"
        mock_agent_cls.return_value = MagicMock()
        mock_designer.return_value = _minimal_output()
        mock_pe.return_value = self._make_approved_review()

        output, trace = run_opensearch_schema_agent()

        assert isinstance(output, OpenSearchModelOutputContract)
        assert trace["total_iterations"] >= 1
        # Designer called once for the initial design only
        assert mock_designer.call_count == 1
        # PE called once
        assert mock_pe.call_count == 1

    @patch("src.tools.schema.opensearch_schema_agent._invoke_pe_reviewer")
    @patch("src.tools.schema.opensearch_schema_agent._invoke_designer")
    @patch("src.tools.schema.opensearch_schema_agent.Agent")
    @patch("src.tools.schema.opensearch_schema_agent._load_skill")
    @patch("src.tools.schema.opensearch_schema_agent._build_model")
    def test_pe_approved_with_notes_extends_trade_offs(
        self, mock_build, mock_load, mock_agent_cls, mock_designer, mock_pe
    ):
        from src.tools.schema.opensearch_schema_agent import (
            PEReviewResult,
            ReviewVerdict,
            run_opensearch_schema_agent,
        )

        mock_build.return_value = MagicMock()
        mock_load.return_value = "skill text"
        mock_agent_cls.return_value = MagicMock()
        mock_designer.return_value = _minimal_output()
        mock_pe.return_value = PEReviewResult(
            verdict=ReviewVerdict.APPROVED,
            pe_notes=["consider warmers", "tune replicas"],
            summary="approved with notes",
        )

        output, trace = run_opensearch_schema_agent()

        trade_off_descriptions = [t.description for t in output.trade_offs]
        assert "[PE note] consider warmers" in trade_off_descriptions
        assert "[PE note] tune replicas" in trade_off_descriptions

    @patch("src.tools.schema.opensearch_schema_agent._invoke_pe_reviewer")
    @patch("src.tools.schema.opensearch_schema_agent._invoke_designer")
    @patch("src.tools.schema.opensearch_schema_agent.Agent")
    @patch("src.tools.schema.opensearch_schema_agent._load_skill")
    @patch("src.tools.schema.opensearch_schema_agent._build_model")
    def test_pe_review_exception_accepts_design(
        self, mock_build, mock_load, mock_agent_cls, mock_designer, mock_pe
    ):
        from src.tools.schema.opensearch_schema_agent import run_opensearch_schema_agent

        mock_build.return_value = MagicMock()
        mock_load.return_value = "skill text"
        mock_agent_cls.return_value = MagicMock()
        mock_designer.return_value = _minimal_output()
        mock_pe.side_effect = RuntimeError("bedrock timeout")

        output, trace = run_opensearch_schema_agent()

        assert isinstance(output, OpenSearchModelOutputContract)
        # PE error should be logged in trace
        assert "pe_review" in trace["iterations"][0]
        assert "error" in trace["iterations"][0]["pe_review"]
        assert "bedrock timeout" in trace["iterations"][0]["pe_review"]["error"]
        # Designer invoked only once (no revision since PE failed)
        assert mock_designer.call_count == 1

    @patch("src.tools.schema.opensearch_schema_agent._invoke_pe_reviewer")
    @patch("src.tools.schema.opensearch_schema_agent._invoke_designer")
    @patch("src.tools.schema.opensearch_schema_agent.Agent")
    @patch("src.tools.schema.opensearch_schema_agent._load_skill")
    @patch("src.tools.schema.opensearch_schema_agent._build_model")
    def test_max_iterations_reached_accepts_with_notes(
        self, mock_build, mock_load, mock_agent_cls, mock_designer, mock_pe
    ):
        from src.tools.schema.opensearch_schema_agent import (
            MAX_PE_ITERATIONS,
            run_opensearch_schema_agent,
        )

        mock_build.return_value = MagicMock()
        mock_load.return_value = "skill text"
        mock_agent_cls.return_value = MagicMock()
        mock_designer.return_value = _minimal_output()
        # Always return CHANGES_REQUESTED to exhaust iterations
        mock_pe.return_value = self._make_changes_requested_review()

        output, trace = run_opensearch_schema_agent()

        assert isinstance(output, OpenSearchModelOutputContract)
        # Designer: 1 initial + (MAX_PE_ITERATIONS - 1) revisions
        assert mock_designer.call_count == MAX_PE_ITERATIONS
        # PE reviewer called MAX_PE_ITERATIONS times
        assert mock_pe.call_count == MAX_PE_ITERATIONS
        # PE notes appended on final iteration
        trade_off_descriptions = [t.description for t in output.trade_offs]
        assert "[PE note] remember replication factor" in trade_off_descriptions

    @patch("src.tools.schema.opensearch_schema_agent._invoke_pe_reviewer")
    @patch("src.tools.schema.opensearch_schema_agent._invoke_designer")
    @patch("src.tools.schema.opensearch_schema_agent.Agent")
    @patch("src.tools.schema.opensearch_schema_agent._load_skill")
    @patch("src.tools.schema.opensearch_schema_agent._build_model")
    def test_changes_requested_then_approved(
        self, mock_build, mock_load, mock_agent_cls, mock_designer, mock_pe
    ):
        from src.tools.schema.opensearch_schema_agent import run_opensearch_schema_agent

        mock_build.return_value = MagicMock()
        mock_load.return_value = "skill text"
        mock_agent_cls.return_value = MagicMock()
        mock_designer.return_value = _minimal_output()
        # First PE review requests changes, second approves
        mock_pe.side_effect = [
            self._make_changes_requested_review(),
            self._make_approved_review(),
        ]

        output, trace = run_opensearch_schema_agent()

        assert isinstance(output, OpenSearchModelOutputContract)
        # Designer: 1 initial + 1 revision
        assert mock_designer.call_count == 2
        # PE reviewer: 2 calls
        assert mock_pe.call_count == 2
        assert trace["total_iterations"] >= 2

    @patch("src.tools.schema.opensearch_schema_agent._invoke_pe_reviewer")
    @patch("src.tools.schema.opensearch_schema_agent._invoke_designer")
    @patch("src.tools.schema.opensearch_schema_agent.Agent")
    @patch("src.tools.schema.opensearch_schema_agent._load_skill")
    @patch("src.tools.schema.opensearch_schema_agent._build_model")
    def test_custom_skill_paths_passed_through(
        self, mock_build, mock_load, mock_agent_cls, mock_designer, mock_pe
    ):
        from src.tools.schema.opensearch_schema_agent import run_opensearch_schema_agent

        mock_build.return_value = MagicMock()
        mock_load.return_value = "skill text"
        mock_agent_cls.return_value = MagicMock()
        mock_designer.return_value = _minimal_output()
        mock_pe.return_value = self._make_approved_review()

        run_opensearch_schema_agent(
            skill_path="/custom/designer.md",
            pe_skill_path="/custom/pe.md",
        )

        mock_load.assert_called_once_with("/custom/designer.md")
        # pe_skill_path is passed as 4th positional arg to _invoke_pe_reviewer
        pe_call_args = mock_pe.call_args
        # args[3] is pe_skill_path (model, design_output, input_summary, pe_skill_path)
        assert pe_call_args.args[3] == "/custom/pe.md"

    @patch("src.tools.schema.opensearch_schema_agent._invoke_pe_reviewer")
    @patch("src.tools.schema.opensearch_schema_agent._invoke_designer")
    @patch("src.tools.schema.opensearch_schema_agent.Agent")
    @patch("src.tools.schema.opensearch_schema_agent._load_skill")
    @patch("src.tools.schema.opensearch_schema_agent._build_model")
    def test_returns_trace_dict_with_expected_keys(
        self, mock_build, mock_load, mock_agent_cls, mock_designer, mock_pe
    ):
        from src.tools.schema.opensearch_schema_agent import run_opensearch_schema_agent

        mock_build.return_value = MagicMock()
        mock_load.return_value = "skill text"
        mock_agent_cls.return_value = MagicMock()
        mock_designer.return_value = _minimal_output()
        mock_pe.return_value = self._make_approved_review()

        _, trace = run_opensearch_schema_agent()

        assert "total_duration_seconds" in trace
        assert "total_iterations" in trace
        assert "iterations" in trace
        assert isinstance(trace["iterations"], list)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge case coverage for contract models and agent orchestrator helpers."""

    # ------------------------------------------------------------------
    # Contract edge cases
    # ------------------------------------------------------------------

    def test_index_mapping_all_optional_fields(self):
        """IndexMapping with aliases, custom analyzers, and all FieldMapping flags set."""

        mapping = IndexMapping(
            index_name="orders",
            source_tables=["public.orders"],
            settings=IndexSettings(
                number_of_shards=3,
                assumed_node_count=3,
                shard_sizing_rationale="10GB / 3 nodes",
                custom_analyzers=[
                    CustomAnalyzer(
                        name="autocomplete",
                        tokenizer="edge_ngram",
                        filter=["lowercase", "stop"],
                        char_filter=["html_strip"],
                    )
                ],
            ),
            field_mappings=[
                FieldMapping(
                    field_name="notes",
                    field_type="text",
                    source_column="notes",
                    analyzer="english",
                    search_analyzer="standard",
                    multi_field=True,
                    doc_values=False,
                    index=False,
                )
            ],
            aliases=["orders_read", "orders_search"],
        )
        assert mapping.aliases == ["orders_read", "orders_search"]
        fm = mapping.field_mappings[0]
        assert fm.search_analyzer == "standard"
        assert fm.multi_field is True
        assert fm.doc_values is False
        assert fm.index is False
        assert mapping.settings.custom_analyzers[0].char_filter == ["html_strip"]

    def test_ism_policy_all_phases(self):
        """ISMPolicy with hot, warm, cold, and delete all populated."""
        policy = ISMPolicy(
            policy_name="full-lifecycle",
            hot_phase_days=7,
            warm_phase_days=30,
            cold_phase_days=60,
            delete_after_days=365,
            rollover_size_gb=100,
            rollover_age_hours=48,
        )
        assert policy.hot_phase_days == 7
        assert policy.warm_phase_days == 30
        assert policy.cold_phase_days == 60
        assert policy.delete_after_days == 365
        assert policy.rollover_size_gb == 100
        assert policy.rollover_age_hours == 48

    def test_ism_policy_minimal_only_hot(self):
        """ISMPolicy with only hot_phase_days; all other phases default to None."""
        policy = ISMPolicy(
            policy_name="hot-only",
            hot_phase_days=14,
        )
        assert policy.hot_phase_days == 14
        assert policy.warm_phase_days is None
        assert policy.cold_phase_days is None
        assert policy.delete_after_days is None
        # Defaults should still be valid
        assert policy.rollover_size_gb == 50
        assert policy.rollover_age_hours == 24

    def test_contract_with_both_index_and_data_stream_designs(self):
        """OpenSearchModelOutputContract accepts mixed index + data stream workload."""
        index = IndexMapping(
            index_name="products",
            source_tables=["public.products"],
            settings=IndexSettings(
                number_of_shards=3,
                assumed_node_count=3,
                shard_sizing_rationale="3-node cluster",
            ),
        )
        ds = DataStreamConfig(
            data_stream_name="events",
            source_tables=["public.events"],
            timestamp_field="event_time",
            index_template=IndexTemplate(
                template_name="events-template",
                index_patterns=["events-*"],
                settings=IndexSettings(
                    number_of_shards=6,
                    assumed_node_count=3,
                    shard_sizing_rationale="high-throughput time-series",
                    refresh_interval="30s",
                ),
            ),
            ism_policy=ISMPolicy(
                policy_name="events-lifecycle",
                hot_phase_days=3,
            ),
        )
        contract = OpenSearchModelOutputContract(
            job_id="mixed-job",
            source_database="mixeddb",
            index_designs=[index],
            data_stream_designs=[ds],
            validation_passed=True,
        )
        assert len(contract.index_designs) == 1
        assert len(contract.data_stream_designs) == 1
        assert contract.index_designs[0].index_name == "products"
        assert contract.data_stream_designs[0].data_stream_name == "events"

    def test_access_pattern_opensearch_dsl_roundtrip(self):
        """AccessPattern.opensearch_dsl is a plain string that round-trips through JSON."""
        from src.contracts.opensearch_model_output import AccessPattern

        dsl_payload = json.dumps(
            {
                "query": {
                    "bool": {
                        "must": [{"match": {"description": "widget"}}],
                        "filter": [{"term": {"status": "active"}}],
                    }
                }
            }
        )
        pattern = AccessPattern(
            pattern_id="AP-1",
            name="Search active widgets",
            description="Search for active widgets by description keyword",
            query_ids=["q-1"],
            source_tables=["public.products"],
            source_query="SELECT * FROM products WHERE status='active' AND description LIKE '%widget%'",
            opensearch_dsl=dsl_payload,
            index_or_stream="products",
            operation="search",
            design_rps=10.0,
        )
        assert pattern.opensearch_dsl == dsl_payload
        # Verify the DSL parses back to a dict intact
        parsed = json.loads(pattern.opensearch_dsl)
        assert parsed["query"]["bool"]["must"][0]["match"]["description"] == "widget"

    def test_unsupported_pattern_all_fields(self):
        """UnsupportedPattern stores source_query, reason, and recommendation."""
        from src.contracts.opensearch_model_output import UnsupportedPattern

        up = UnsupportedPattern(
            source_query="SELECT COUNT(*) FROM orders GROUP BY ROLLUP(region, product)",
            reason="OpenSearch does not support SQL ROLLUP aggregation natively",
            recommendation="Use a multi-level terms aggregation or pre-aggregate in the ETL layer",
        )
        assert "ROLLUP" in up.source_query
        assert "ROLLUP" in up.reason
        assert "terms aggregation" in up.recommendation

    def test_contract_validation_failed_with_messages(self):
        """Contract with validation_passed=False carries failure messages."""
        contract = OpenSearchModelOutputContract(
            job_id="failing-job",
            source_database="testdb",
            index_designs=[],
            data_stream_designs=[],
            validation_passed=False,
            validation_failures=[
                "shard count must be a multiple of node count",
                "ISM policy delete_after_days must exceed hot_phase_days",
            ],
        )
        assert contract.validation_passed is False
        assert len(contract.validation_failures) == 2
        assert "shard count" in contract.validation_failures[0]

    def test_empty_source_tables_is_accepted_by_contract(self):
        """source_tables has no min_length constraint — empty list passes model validation."""
        mapping = IndexMapping(
            index_name="empty-src",
            source_tables=[],  # no min_length enforced at model level
            settings=IndexSettings(
                number_of_shards=1,
                assumed_node_count=1,
                shard_sizing_rationale="single node test",
            ),
        )
        assert mapping.source_tables == []

    def test_custom_analyzer_empty_filter_lists(self):
        """CustomAnalyzer defaults filter and char_filter to empty lists."""
        analyzer = CustomAnalyzer(
            name="bare_standard",
            tokenizer="standard",
        )
        assert analyzer.filter == []
        assert analyzer.char_filter == []
        # Explicit empty lists also accepted
        analyzer2 = CustomAnalyzer(
            name="explicit_empty",
            tokenizer="whitespace",
            filter=[],
            char_filter=[],
        )
        assert analyzer2.filter == []
        assert analyzer2.char_filter == []

    # ------------------------------------------------------------------
    # Agent orchestrator edge cases
    # ------------------------------------------------------------------

    def test_load_agent_input_without_decision_trace_path(self, tmp_path):
        """load_agent_input returns empty dict for decision_trace when DECISION_TRACE_PATH unset."""
        collector_data = {
            "contract_version": "3.0",
            "job_id": "edge-job",
            "metadata": {
                "collection_timestamp": "2026-01-01",
                "collector_version": "1.0.0",
                "source_database": {"engine": "mysql", "version": "8.0", "hostname": "edge-host"},
            },
            "database_schema": {
                "tables": [
                    {
                        "table_id": "t1",
                        "table_name": "t1",
                        "row_count": 0,
                        "size_mb": 0.0,
                        "columns": [{"column_name": "id", "data_type": "int", "nullable": False}],
                        "primary_key": ["id"],
                    }
                ]
            },
            "queries": {"query_patterns": []},
            "metrics": {"performance_metrics": {}},
        }
        analysis_data = {
            "contract_version": "2.1",
            "agent_metadata": {
                "agent_name": "opensearch-analysis-agent",
                "agent_version": "1.0.0",
                "target_database": "opensearch",
                "analysis_timestamp": "2026-01-01T00:00:00",
                "analysis_duration_seconds": 1.0,
            },
            "table_recommendations": [],
            "workload_analysis": {"patterns_detected": []},
            "cost_estimate": {
                "monthly_cost_usd": 0,
                "cost_components": {},
                "pricing_assumptions": [],
            },
        }
        collector_path = tmp_path / "collector.json"
        analysis_path = tmp_path / "analysis.json"
        collector_path.write_text(json.dumps(collector_data))
        analysis_path.write_text(json.dumps(analysis_data))

        env = {
            "COLLECTOR_OUTPUT_PATH": str(collector_path),
            "ANALYSIS_OUTPUT_PATH": str(analysis_path),
        }
        # Explicitly remove DECISION_TRACE_PATH if it happens to be set
        with patch.dict(os.environ, env):
            os.environ.pop("DECISION_TRACE_PATH", None)
            from src.tools.schema.opensearch_schema_agent import load_agent_input

            result = load_agent_input()

        assert result["decision_trace"] == {}

    def test_load_agent_input_nonexistent_trace_path(self, tmp_path):
        """load_agent_input returns empty dict when DECISION_TRACE_PATH points to missing file."""
        collector_data = {
            "contract_version": "3.0",
            "job_id": "edge-job2",
            "metadata": {
                "collection_timestamp": "2026-01-01",
                "collector_version": "1.0.0",
                "source_database": {"engine": "mysql", "version": "8.0", "hostname": "edge-host2"},
            },
            "database_schema": {
                "tables": [
                    {
                        "table_id": "t1",
                        "table_name": "t1",
                        "row_count": 0,
                        "size_mb": 0.0,
                        "columns": [{"column_name": "id", "data_type": "int", "nullable": False}],
                        "primary_key": ["id"],
                    }
                ]
            },
            "queries": {"query_patterns": []},
            "metrics": {"performance_metrics": {}},
        }
        analysis_data = {
            "contract_version": "2.1",
            "agent_metadata": {
                "agent_name": "opensearch-analysis-agent",
                "agent_version": "1.0.0",
                "target_database": "opensearch",
                "analysis_timestamp": "2026-01-01T00:00:00",
                "analysis_duration_seconds": 1.0,
            },
            "table_recommendations": [],
            "workload_analysis": {"patterns_detected": []},
            "cost_estimate": {
                "monthly_cost_usd": 0,
                "cost_components": {},
                "pricing_assumptions": [],
            },
        }
        collector_path = tmp_path / "collector.json"
        analysis_path = tmp_path / "analysis.json"
        collector_path.write_text(json.dumps(collector_data))
        analysis_path.write_text(json.dumps(analysis_data))

        with patch.dict(
            os.environ,
            {
                "COLLECTOR_OUTPUT_PATH": str(collector_path),
                "ANALYSIS_OUTPUT_PATH": str(analysis_path),
                "DECISION_TRACE_PATH": str(tmp_path / "does_not_exist.json"),
            },
        ):
            from src.tools.schema.opensearch_schema_agent import load_agent_input

            result = load_agent_input()

        assert result["decision_trace"] == {}

    def test_format_pe_feedback_multiple_change_requests_numbered(self):
        """_format_pe_feedback numbers multiple change requests sequentially."""
        from src.tools.schema.opensearch_schema_agent import (
            ChangeCategory,
            ChangeRequest,
            ChangeSeverity,
            PEReviewResult,
            ReviewVerdict,
            _format_pe_feedback,
        )

        review = PEReviewResult(
            verdict=ReviewVerdict.CHANGES_REQUESTED,
            change_requests=[
                ChangeRequest(
                    category=ChangeCategory.SHARD_SIZING,
                    severity=ChangeSeverity.BLOCKER,
                    target="products",
                    requested_change="Use 3 shards",
                    rationale="cluster alignment",
                ),
                ChangeRequest(
                    category=ChangeCategory.ISM_POLICY,
                    severity=ChangeSeverity.WARNING,
                    target="logs-stream",
                    requested_change="Add warm phase",
                    rationale="cost reduction",
                ),
                ChangeRequest(
                    category=ChangeCategory.ANALYZER,
                    severity=ChangeSeverity.WARNING,
                    target="search-index",
                    requested_change="Use english analyzer",
                    rationale="better stemming",
                ),
            ],
            summary="multiple issues found",
        )
        text = _format_pe_feedback(review)

        # Verify numbered prefix for each change request
        assert "1. [blocker]" in text
        assert "2. [warning]" in text
        assert "3. [warning]" in text
        # Verify all targets appear
        assert "products" in text
        assert "logs-stream" in text
        assert "search-index" in text

    def test_schema_design_trace_multiple_iterations(self):
        """SchemaDesignTrace correctly accumulates 3 designer + 3 PE review iterations."""
        from src.tools.schema.opensearch_schema_agent import (
            ChangeCategory,
            ChangeRequest,
            ChangeSeverity,
            PEReviewResult,
            ReviewVerdict,
            SchemaDesignTrace,
        )

        trace = SchemaDesignTrace()
        output = _minimal_output()

        changes_review = PEReviewResult(
            verdict=ReviewVerdict.CHANGES_REQUESTED,
            change_requests=[
                ChangeRequest(
                    category=ChangeCategory.DATA_STREAM,
                    severity=ChangeSeverity.WARNING,
                    target="events-stream",
                    requested_change="Add cold phase",
                    rationale="archival cost",
                )
            ],
            summary="minor issues",
        )
        approved_review = PEReviewResult(
            verdict=ReviewVerdict.APPROVED,
            summary="looks great now",
        )

        # Iteration 0: designer + PE requests changes
        trace.log_designer(0, 2.0, output)
        trace.log_pe_review(0, 1.0, changes_review)

        # Iteration 1: designer revision + PE requests changes again
        trace.log_designer(1, 1.8, output)
        trace.log_pe_review(1, 0.9, changes_review)

        # Iteration 2: final designer revision + PE approves
        trace.log_designer(2, 1.5, output)
        trace.log_pe_review(2, 0.7, approved_review)

        result = trace.to_dict()
        assert result["total_iterations"] == 3
        assert len(result["iterations"]) == 3

        # Each iteration has both designer and pe_review entries
        for i, entry in enumerate(result["iterations"]):
            assert "designer" in entry, f"iteration {i} missing designer entry"
            assert "pe_review" in entry, f"iteration {i} missing pe_review entry"

        # Final PE review verdict is APPROVED
        assert result["iterations"][2]["pe_review"]["verdict"] == "APPROVED"
        # First two PE reviews were CHANGES_REQUESTED
        assert result["iterations"][0]["pe_review"]["verdict"] == "CHANGES_REQUESTED"
        assert result["iterations"][1]["pe_review"]["verdict"] == "CHANGES_REQUESTED"

    def test_invoke_designer_invalid_json_raises(self):
        """_invoke_designer raises when agent returns non-JSON and no structured output."""
        import pytest

        from src.tools.schema.opensearch_schema_agent import _invoke_designer

        mock_result = MagicMock()
        mock_result.structured_output = None
        mock_result.__str__ = MagicMock(return_value="this is not valid JSON {{{{")

        mock_agent = MagicMock(return_value=mock_result)

        with pytest.raises((ValueError, json.JSONDecodeError)):
            _invoke_designer(mock_agent, "design prompt")

    def test_pe_reviewer_changes_requested_empty_change_requests(self):
        """PEReviewResult accepts CHANGES_REQUESTED verdict with empty change_requests list."""
        from src.tools.schema.opensearch_schema_agent import (
            PEReviewResult,
            ReviewVerdict,
            _format_pe_feedback,
        )

        review = PEReviewResult(
            verdict=ReviewVerdict.CHANGES_REQUESTED,
            change_requests=[],
            summary="changes needed but unspecified",
        )
        assert review.verdict == ReviewVerdict.CHANGES_REQUESTED
        assert review.change_requests == []

        # _format_pe_feedback should not emit a "Change Requests" section
        text = _format_pe_feedback(review)
        assert "Change Requests" not in text
        assert "changes needed but unspecified" in text


# ---------------------------------------------------------------------------
# Robustness tests (iteration 3)
# ---------------------------------------------------------------------------


class TestRobustness:
    """Robustness and boundary tests for the OpenSearch schema design agent."""

    # ------------------------------------------------------------------
    # 1. Malformed collector output in load_agent_input → ValidationError
    # ------------------------------------------------------------------

    def test_load_agent_input_malformed_collector_raises_validation_error(self, tmp_path):
        """Invalid JSON structure for collector output raises pydantic ValidationError."""
        import pytest
        from pydantic import ValidationError

        # Collector JSON is syntactically valid but structurally wrong
        bad_collector = {"this_is": "not a valid collector", "random_key": 42}
        analysis_data = {
            "contract_version": "2.1",
            "agent_metadata": {
                "agent_name": "opensearch-analysis-agent",
                "agent_version": "1.0.0",
                "target_database": "opensearch",
                "analysis_timestamp": "2026-01-01T00:00:00",
                "analysis_duration_seconds": 1.0,
            },
            "table_recommendations": [],
            "workload_analysis": {"patterns_detected": []},
            "cost_estimate": {
                "monthly_cost_usd": 0,
                "cost_components": {},
                "pricing_assumptions": [],
            },
        }
        collector_path = tmp_path / "collector.json"
        analysis_path = tmp_path / "analysis.json"
        collector_path.write_text(json.dumps(bad_collector))
        analysis_path.write_text(json.dumps(analysis_data))

        with patch.dict(
            os.environ,
            {
                "COLLECTOR_OUTPUT_PATH": str(collector_path),
                "ANALYSIS_OUTPUT_PATH": str(analysis_path),
            },
        ):
            from src.tools.schema.opensearch_schema_agent import load_agent_input

            with pytest.raises(ValidationError):
                load_agent_input()

    # ------------------------------------------------------------------
    # 2. Unicode in field names — FieldMapping with unicode field_name
    # ------------------------------------------------------------------

    def test_field_mapping_unicode_field_name(self):
        """FieldMapping accepts unicode field names without crashing."""
        fm = FieldMapping(
            field_name="données_utilisateur",
            field_type="text",
            source_column="user_data",
        )
        assert fm.field_name == "données_utilisateur"
        # Verify it round-trips through JSON serialization
        dumped = fm.model_dump(mode="json")
        assert dumped["field_name"] == "données_utilisateur"

    # ------------------------------------------------------------------
    # 3. Very long trade_offs list — 100 trade-off objects
    # ------------------------------------------------------------------

    def test_contract_with_very_long_trade_offs_list(self):
        """OpenSearchModelOutputContract with 100 trade-off objects serializes correctly."""
        trade_offs = [
            TradeOff(
                description=f"Trade-off decision #{i}: some rationale text",
                impact=f"Impact #{i}",
                engine="opensearch",
            )
            for i in range(100)
        ]
        contract = OpenSearchModelOutputContract(
            job_id="large-job",
            source_database="bigdb",
            index_designs=[],
            data_stream_designs=[],
            validation_passed=True,
            trade_offs=trade_offs,
        )
        assert len(contract.trade_offs) == 100
        # Full JSON serialization should preserve all entries
        json_str = contract.model_dump_json()
        restored = OpenSearchModelOutputContract.model_validate_json(json_str)
        assert len(restored.trade_offs) == 100
        assert restored.trade_offs[0].description == "Trade-off decision #0: some rationale text"
        assert restored.trade_offs[99].description == "Trade-off decision #99: some rationale text"

    # ------------------------------------------------------------------
    # 4. ISMPolicy with all None optional phases — only hot_phase_days set
    # ------------------------------------------------------------------

    def test_ism_policy_only_hot_phase_is_valid(self):
        """ISMPolicy with only hot_phase_days and all optional phases as None is valid."""
        policy = ISMPolicy(
            policy_name="hot-only-policy",
            hot_phase_days=3,
        )
        assert policy.hot_phase_days == 3
        assert policy.warm_phase_days is None
        assert policy.cold_phase_days is None
        assert policy.delete_after_days is None
        # Defaults preserved
        assert policy.rollover_size_gb == 50
        assert policy.rollover_age_hours == 24
        # Model validation passes (no crash during construction)
        dumped = policy.model_dump(mode="json")
        assert dumped["warm_phase_days"] is None
        assert dumped["cold_phase_days"] is None
        assert dumped["delete_after_days"] is None

    # ------------------------------------------------------------------
    # 5. IndexSettings with 0 replicas — valid for single-node dev
    # ------------------------------------------------------------------

    def test_index_settings_zero_replicas_valid(self):
        """IndexSettings with number_of_replicas=0 is valid (single-node dev cluster)."""
        settings = IndexSettings(
            number_of_shards=3,
            number_of_replicas=0,
            assumed_node_count=1,
            shard_sizing_rationale="single-node dev — no replicas needed",
        )
        assert settings.number_of_replicas == 0
        assert settings.number_of_shards == 3

    # ------------------------------------------------------------------
    # 6. IndexSettings with large shard count — no upper bound in contract
    # ------------------------------------------------------------------

    def test_index_settings_large_shard_count_valid(self):
        """IndexSettings with number_of_shards=100 is valid (no upper bound enforced)."""
        settings = IndexSettings(
            number_of_shards=100,
            assumed_node_count=10,
            shard_sizing_rationale="very large dataset requiring 100 shards across 10 nodes",
        )
        assert settings.number_of_shards == 100

    # ------------------------------------------------------------------
    # 7. Multiple CustomAnalyzers on same index — all preserved in serialization
    # ------------------------------------------------------------------

    def test_index_settings_multiple_custom_analyzers_all_preserved(self):
        """IndexSettings with 3 custom analyzers — all are preserved after serialization."""
        analyzers = [
            CustomAnalyzer(
                name="autocomplete_analyzer",
                tokenizer="edge_ngram",
                filter=["lowercase"],
            ),
            CustomAnalyzer(
                name="english_analyzer",
                tokenizer="standard",
                filter=["lowercase", "english_stop", "english_stemmer"],
            ),
            CustomAnalyzer(
                name="html_strip_analyzer",
                tokenizer="standard",
                filter=["lowercase"],
                char_filter=["html_strip"],
            ),
        ]
        settings = IndexSettings(
            number_of_shards=3,
            assumed_node_count=3,
            shard_sizing_rationale="3-node cluster",
            custom_analyzers=analyzers,
        )
        assert len(settings.custom_analyzers) == 3
        # Serialize and restore
        dumped = settings.model_dump(mode="json")
        restored = IndexSettings.model_validate(dumped)
        assert len(restored.custom_analyzers) == 3
        assert restored.custom_analyzers[0].name == "autocomplete_analyzer"
        assert restored.custom_analyzers[1].name == "english_analyzer"
        assert restored.custom_analyzers[2].name == "html_strip_analyzer"
        assert restored.custom_analyzers[2].char_filter == ["html_strip"]

    # ------------------------------------------------------------------
    # 8. DataStreamConfig with very long data_stream_name — 200 chars
    # ------------------------------------------------------------------

    def test_data_stream_config_very_long_name(self):
        """DataStreamConfig with a 200-character data_stream_name does not crash."""
        long_name = "application-logs-" + "x" * 183  # total 200 chars
        assert len(long_name) == 200

        ds = DataStreamConfig(
            data_stream_name=long_name,
            source_tables=["public.application_logs"],
            timestamp_field="created_at",
            index_template=IndexTemplate(
                template_name=f"{long_name}-template",
                index_patterns=[f"{long_name}-*"],
                settings=IndexSettings(
                    number_of_shards=3,
                    assumed_node_count=3,
                    shard_sizing_rationale="standard sizing",
                ),
            ),
            ism_policy=ISMPolicy(
                policy_name="long-name-policy",
                hot_phase_days=7,
            ),
        )
        assert ds.data_stream_name == long_name
        # Survives serialization
        dumped = ds.model_dump(mode="json")
        assert dumped["data_stream_name"] == long_name

    # ------------------------------------------------------------------
    # 9. SchemaDesignTrace.to_dict with zero iterations — empty trace
    # ------------------------------------------------------------------

    def test_schema_design_trace_to_dict_zero_iterations(self):
        """SchemaDesignTrace.to_dict returns a valid dict when no iterations were logged."""
        from src.tools.schema.opensearch_schema_agent import SchemaDesignTrace

        trace = SchemaDesignTrace()
        result = trace.to_dict()

        assert isinstance(result, dict)
        assert result["total_iterations"] == 0
        assert result["iterations"] == []
        assert "total_duration_seconds" in result
        assert isinstance(result["total_duration_seconds"], float)
        assert result["total_duration_seconds"] >= 0.0

    # ------------------------------------------------------------------
    # 10. run_opensearch_schema_agent with designer producing non-empty designs
    # ------------------------------------------------------------------

    @patch("src.tools.schema.opensearch_schema_agent._invoke_pe_reviewer")
    @patch("src.tools.schema.opensearch_schema_agent._invoke_designer")
    @patch("src.tools.schema.opensearch_schema_agent.Agent")
    @patch("src.tools.schema.opensearch_schema_agent._load_skill")
    @patch("src.tools.schema.opensearch_schema_agent._build_model")
    def test_run_with_non_empty_designs_sets_input_summary_table_count(
        self, mock_build, mock_load, mock_agent_cls, mock_designer, mock_pe
    ):
        """run_opensearch_schema_agent with 2 index_designs + 1 data_stream → table_count = 3."""
        from src.tools.schema.opensearch_schema_agent import (
            PEReviewResult,
            ReviewVerdict,
            run_opensearch_schema_agent,
        )

        mock_build.return_value = MagicMock()
        mock_load.return_value = "skill text"
        mock_agent_cls.return_value = MagicMock()

        # Build a designer output with 2 index_designs and 1 data_stream
        index1 = IndexMapping(
            index_name="products",
            source_tables=["public.products"],
            settings=IndexSettings(
                number_of_shards=3,
                assumed_node_count=3,
                shard_sizing_rationale="3-node cluster",
            ),
        )
        index2 = IndexMapping(
            index_name="orders",
            source_tables=["public.orders"],
            settings=IndexSettings(
                number_of_shards=3,
                assumed_node_count=3,
                shard_sizing_rationale="3-node cluster",
            ),
        )
        ds = DataStreamConfig(
            data_stream_name="events",
            source_tables=["public.events"],
            timestamp_field="event_time",
            index_template=IndexTemplate(
                template_name="events-template",
                index_patterns=["events-*"],
                settings=IndexSettings(
                    number_of_shards=6,
                    assumed_node_count=3,
                    shard_sizing_rationale="high-throughput",
                    refresh_interval="30s",
                ),
            ),
            ism_policy=ISMPolicy(
                policy_name="events-lifecycle",
                hot_phase_days=7,
            ),
        )
        rich_output = OpenSearchModelOutputContract(
            job_id="rich-job",
            source_database="richdb",
            index_designs=[index1, index2],
            data_stream_designs=[ds],
            validation_passed=True,
        )

        mock_designer.return_value = rich_output
        mock_pe.return_value = PEReviewResult(verdict=ReviewVerdict.APPROVED, summary="all good")

        output, trace = run_opensearch_schema_agent()

        assert isinstance(output, OpenSearchModelOutputContract)
        # 2 index_designs + 1 data_stream = table_count of 3
        # The PE reviewer is called with input_summary containing table_count
        pe_call_args = mock_pe.call_args
        input_summary = pe_call_args.args[2]  # (model, design_output, input_summary, ...)
        assert input_summary["table_count"] == 3
        # Designs are preserved in output
        assert len(output.index_designs) == 2
        assert len(output.data_stream_designs) == 1
