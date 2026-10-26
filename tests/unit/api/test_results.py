"""Unit tests for results routes."""

import copy
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes import results
from src.shared.report_facts import compute_report_facts

client = TestClient(app)


@pytest.fixture(autouse=True)
def mock_services():
    """Inject mock services into the results router."""
    sfn = MagicMock()
    s3 = MagicMock()

    # Default: describe_execution returns valid input with database_name
    sfn.describe_execution.return_value = {
        "status": "SUCCEEDED",
        "started_at": "2026-02-23T14:00:00Z",
        "stopped_at": "2026-02-23T18:00:00Z",
        "input": {"database_name": "test_db", "source_database_type": "mysql"},
    }

    results.sfn_service = sfn
    results.s3_service = s3

    yield {"sfn": sfn, "s3": s3}

    results.sfn_service = None
    results.s3_service = None


class TestGetResults:
    """Tests for GET /api/v1/assessments/{job_id}/results."""

    def test_returns_synthesis(self, mock_services):
        mock_services["s3"].read_synthesis.return_value = {
            "ranking": [{"target": "dynamodb", "confidence": 0.92}],
            "needs_deeper_analysis": False,
        }
        mock_services["s3"].read_triage.return_value = {
            "selected_agents": [{"agent_type": "dynamodb"}],
            "confidence": 0.87,
        }
        response = client.get("/api/v1/assessments/job-1/results")
        assert response.status_code == 200
        data = response.json()
        assert "synthesis" in data
        assert "triage_summary" in data

    def test_404_when_no_synthesis(self, mock_services):
        mock_services["s3"].read_synthesis.return_value = None
        response = client.get("/api/v1/assessments/job-1/results")
        assert response.status_code == 404

    def test_404_when_job_not_found(self, mock_services):
        mock_services["sfn"].describe_execution.return_value = None
        response = client.get("/api/v1/assessments/nonexistent/results")
        assert response.status_code == 404

    def test_backfills_migration_waves_for_a_report_written_before_225(self, mock_services):
        # #225: the UI (and the HTML exports, which read this
        # same payload) must show the same fallback roadmap the decision report
        # and engineering report already derive via resolve_migration_waves,
        # not an empty roadmap just because this report predates #225.
        mock_services["s3"].read_synthesis.return_value = {
            "ranking": [
                {"target": "dynamodb", "workload_percent": 60.0, "assigned_queries": 60},
                {"target": "aurora_mysql", "workload_percent": 40.0, "assigned_queries": 40},
            ],
            "needs_deeper_analysis": False,
        }
        mock_services["s3"].read_triage.return_value = None
        response = client.get("/api/v1/assessments/job-1/results")
        assert response.status_code == 200
        waves = response.json()["synthesis"]["migration_waves"]
        # #321: the relational move comes first, even in the legacy fallback.
        assert [w["engines"] for w in waves] == [["aurora_mysql"], ["dynamodb"]]

    def test_backfills_facts_for_a_report_written_before_615(self, mock_services):
        """#615: an older report gets the facts block the deliverables read, so
        the UI exports print the merged wave's real move count (#581)."""
        mock_services["s3"].read_synthesis.return_value = {
            "ranking": [
                {"target": "dynamodb", "workload_percent": 60.0},
                {"target": "documentdb", "workload_percent": 40.0},
            ],
            "migration_waves": [
                {
                    "wave": 2,
                    "engines": ["dynamodb", "documentdb"],
                    "moves_from": ["aurora_mysql"],
                    "tables": ["t1", "t2", "t3", "t4"],
                    "table_count": 4,
                    "table_owners": [
                        {"table": "t2", "owner": "dynamodb", "sync": "CDC"},
                        {"table": "t4", "owner": "aurora_mysql", "sync": "CDC"},
                    ],
                    "table_counts_by_engine": {"dynamodb": 2, "documentdb": 3},
                    "owned_counts_by_engine": {"dynamodb": 2, "documentdb": 1},
                }
            ],
        }
        mock_services["s3"].read_triage.return_value = None
        facts = client.get("/api/v1/assessments/job-1/results").json()["synthesis"]["facts"]
        wave = facts["waves"][0]
        assert (wave["served_tables"], wave["moving_tables"], wave["kept_in_sync_tables"]) == (
            4,
            3,
            1,
        )

    def test_a_facts_block_before_1_26_gains_the_access_pattern_totals(self, mock_services):
        """#792: a stored block without ``access_pattern_totals`` gets it from
        ``query_groups``; every other stored figure stays as written."""
        stored = {"engines": [], "engine_count": 7, "tables_migrate": 3}
        mock_services["s3"].read_synthesis.return_value = {
            "ranking": [],
            "facts": stored,
            "query_groups": [
                {"access_patterns": [{"engine": "dynamodb", "pattern_id": "DDB-AP-1"}]}
            ],
        }
        mock_services["s3"].read_triage.return_value = None
        facts = client.get("/api/v1/assessments/job-1/results").json()["synthesis"]["facts"]
        assert facts["access_pattern_totals"]["owned_pattern_count"] == 1
        assert (facts["engine_count"], facts["tables_migrate"]) == (7, 3)

    def test_a_facts_block_before_1_28_gains_the_query_move_counts(self, mock_services):
        """#1020: a stored block without ``moving_query_count`` gets both query move
        counts from ``ranking`` and its stored engine roles; the rest stays as written."""
        stored = {
            "engines": [
                {"engine": "aurora_mysql", "role": "Retained", "cached_queries": None},
                {"engine": "dynamodb", "role": "Migration target", "cached_queries": None},
            ],
            "engine_count": 7,
            "access_pattern_totals": None,
        }
        mock_services["s3"].read_synthesis.return_value = {
            "ranking": [
                {"target": "aurora_mysql", "assigned_queries_in_scope": 70},
                {"target": "dynamodb", "assigned_queries_in_scope": 37},
            ],
            "facts": stored,
        }
        mock_services["s3"].read_triage.return_value = None
        facts = client.get("/api/v1/assessments/job-1/results").json()["synthesis"]["facts"]
        assert (facts["in_scope_query_count"], facts["moving_query_count"]) == (107, 37)
        assert facts["engine_count"] == 7

    def test_a_facts_block_before_1_28_gains_the_resolved_risks_by_severity(self, mock_services):
        """#1020: a stored block without ``resolved_risks_by_severity`` gets it from
        ``risk_assessment.resolved_risks``; the stored count stays as written."""
        stored = {
            "engines": [],
            "access_pattern_totals": None,
            "moving_query_count": 0,
            "resolved_risk_count": 3,
        }
        mock_services["s3"].read_synthesis.return_value = {
            "ranking": [],
            "facts": stored,
            "risk_assessment": {
                "risks": [],
                "resolved_risks": [
                    {"severity": "HIGH", "description": "[dynamodb] a"},
                    {"severity": "MEDIUM", "description": "[dynamodb] b"},
                    {"severity": "HIGH", "description": "[dynamodb] c"},
                ],
            },
        }
        mock_services["s3"].read_triage.return_value = None
        facts = client.get("/api/v1/assessments/job-1/results").json()["synthesis"]["facts"]
        assert facts["resolved_risks_by_severity"] == {"HIGH": 2, "MEDIUM": 1}
        assert facts["resolved_risk_count"] == 3

    def test_an_older_report_gains_the_flagged_query_risk(self, mock_services):
        # #1148: a report synthesized before routing.flagged existed shows the risk
        # its flagged_unservable rows raise, as the deliverables do, and its facts
        # count it.
        report = {
            "migration_waves": [{"wave": 2, "engines": ["dynamodb"], "tables": []}],
            "flagged_unservable": [
                {
                    "query_id": "q1",
                    "engine": "dynamodb",
                    "cause": "no_kept_engine",
                    "reason_category": "full_scan",
                }
            ],
            "risk_assessment": {
                "overall_risk_level": "LOW",
                "risks": [{"risk_id": "RISK-001", "severity": "LOW", "description": "[x] y"}],
                "mitigation_strategies": [],
            },
        }
        report["facts"] = compute_report_facts(report)
        mock_services["s3"].read_synthesis.return_value = report
        mock_services["s3"].read_triage.return_value = {}
        synthesis = client.get("/api/v1/assessments/job-1/results").json()["synthesis"]
        risks = synthesis["risk_assessment"]["risks"]
        assert [(r["risk_id"], r.get("rule")) for r in risks] == [
            ("RISK-001", None),
            ("RISK-002", "routing.flagged"),
        ]
        assert synthesis["risk_assessment"]["overall_risk_level"] == "HIGH"
        assert synthesis["facts"]["open_risk_count"] == 2

    def test_a_real_old_report_payload_states_the_backfilled_risk_count(self, mock_services):
        # Review of #1203: the results page prints synthesis.summary, which must state
        # the backfilled register's count and level (a 1.41 report main's code wrote).
        fixture = Path(__file__).resolve().parents[1] / "report/fixtures"
        old = json.loads((fixture / "wordpress_1_41_flagged_report.json").read_text())
        mock_services["s3"].read_synthesis.return_value = old
        mock_services["s3"].read_triage.return_value = {}
        synthesis = client.get("/api/v1/assessments/job-1/results").json()["synthesis"]
        assert "4 open migration risks (overall: HIGH)" in synthesis["summary"]
        assert synthesis["facts"]["open_risk_count"] == 4
        assert synthesis["risk_assessment"]["overall_risk_level"] == "HIGH"

    def test_an_older_report_gains_the_relational_gap_risks(self, mock_services):
        # #1149: a report synthesized before relational.gaps existed shows the risks its
        # stored facts.relational_gaps raise, as the deliverables do.
        report = {
            "migration_waves": [{"wave": 1, "engines": ["aurora_postgresql"], "tables": ["db.t"]}],
            "risk_assessment": {"overall_risk_level": "LOW", "risks": []},
        }
        gaps = {
            "source_engine": "sqlserver",
            "target_engine": "aurora_postgresql",
            "gaps": [
                {
                    "rule_id": "top",
                    "feature": "TOP row limits",
                    "impact": "needs_rewrite",
                    "reason": "TOP becomes LIMIT.",
                    "docs": "Playbook",
                    "tables": ["db.t"],
                    "queries": ["q1"],
                }
            ],
        }
        report["facts"] = compute_report_facts(report, relational_gaps=gaps)
        mock_services["s3"].read_synthesis.return_value = report
        mock_services["s3"].read_triage.return_value = {}
        synthesis = client.get("/api/v1/assessments/job-1/results").json()["synthesis"]
        [risk] = synthesis["risk_assessment"]["risks"]
        assert (risk["rule"], risk["severity"], risk["wave"]) == ("relational.gaps", "MEDIUM", 1)
        assert risk["details"][0].startswith("TOP row limits: TOP becomes LIMIT.")
        assert synthesis["facts"]["open_risk_count"] == 1

    def test_a_facts_block_before_1_40_gains_the_retired_in_use_engines(self, mock_services):
        """#1076: a report from before the field existed has no ``engines_in_use``; its stored block gets the
        retired engine from the report's ``retired_engine_savings`` rows."""
        stored = {
            "engines": [],
            "access_pattern_totals": None,
            "moving_query_count": 0,
            "resolved_risks_by_severity": {},
            "removed_engines": [{"engine": "opensearch", "moved_to": ["aurora_mysql"]}],
        }
        mock_services["s3"].read_synthesis.return_value = {
            "ranking": [],
            "facts": stored,
            "tco_analysis": {
                "retired_engine_savings": [{"database": "opensearch", "monthly_cost_usd": 240.96}]
            },
        }
        mock_services["s3"].read_triage.return_value = None
        facts = client.get("/api/v1/assessments/job-1/results").json()["synthesis"]["facts"]
        assert facts["retired_in_use_engines"] == [
            {
                "engine": "opensearch",
                "monthly_cost_usd": 240.96,
                "moved_to": ["aurora_mysql"],
                "reason": None,
                "no_saving_reason": None,
            }
        ]

    def test_keeps_stored_access_pattern_totals(self, mock_services):
        totals = {"owned_pattern_count": 9, "cache_layer_pattern_count": 0}
        mock_services["s3"].read_synthesis.return_value = {
            "ranking": [],
            "facts": {"engines": [], "access_pattern_totals": totals},
            "query_groups": [],
        }
        mock_services["s3"].read_triage.return_value = None
        facts = client.get("/api/v1/assessments/job-1/results").json()["synthesis"]["facts"]
        assert facts["access_pattern_totals"] == totals

    def test_keeps_the_stored_waves_when_present(self, mock_services):
        stored = [{"wave": 1, "engines": ["elasticache"], "moves_from": [], "serves_from": []}]
        mock_services["s3"].read_synthesis.return_value = {
            "ranking": [],
            "migration_waves": stored,
        }
        mock_services["s3"].read_triage.return_value = None
        response = client.get("/api/v1/assessments/job-1/results")
        assert response.json()["synthesis"]["migration_waves"] == stored


class TestGetCollectorOutput:
    """Tests for GET /api/v1/assessments/{job_id}/collector."""

    def test_returns_collector_data(self, mock_services):
        mock_services["s3"].read_collector.return_value = {
            "job_id": "job-1",
            "agent_type": "collector",
            "status": "completed",
        }
        response = client.get("/api/v1/assessments/job-1/collector")
        assert response.status_code == 200
        assert response.json()["agent_type"] == "collector"

    def test_404_when_not_available(self, mock_services):
        mock_services["s3"].read_collector.return_value = None
        response = client.get("/api/v1/assessments/job-1/collector")
        assert response.status_code == 404


class TestGetTriageOutput:
    """Tests for GET /api/v1/assessments/{job_id}/triage."""

    def test_returns_triage_data(self, mock_services):
        mock_services["s3"].read_triage.return_value = {
            "selected_agents": [{"agent_type": "dynamodb", "reason": "key-value patterns"}],
            "skipped_agents": [{"agent_type": "neptune", "reason": "no graph patterns"}],
            "confidence": 0.87,
        }
        response = client.get("/api/v1/assessments/job-1/triage")
        assert response.status_code == 200
        data = response.json()
        assert len(data["selected_agents"]) == 1
        assert data["confidence"] == 0.87

    def test_404_when_not_available(self, mock_services):
        mock_services["s3"].read_triage.return_value = None
        response = client.get("/api/v1/assessments/job-1/triage")
        assert response.status_code == 404


class TestGetAnalysisOutput:
    """Tests for GET /api/v1/assessments/{job_id}/analysis/{agent_type}."""

    def test_returns_analysis_data(self, mock_services):
        mock_services["s3"].read_analysis.return_value = {
            "agent_type": "dynamodb",
            "confidence": 0.92,
            "status": "completed",
        }
        response = client.get("/api/v1/assessments/job-1/analysis/dynamodb")
        assert response.status_code == 200
        assert response.json()["agent_type"] == "dynamodb"

    def test_404_when_not_available(self, mock_services):
        mock_services["s3"].read_analysis.return_value = None
        response = client.get("/api/v1/assessments/job-1/analysis/neptune")
        assert response.status_code == 404


class TestGetSchemaDesigns:
    """Tests for GET /api/v1/assessments/{job_id}/schema-designs."""

    def test_returns_schema_designs(self, mock_services):
        mock_services["s3"].read_all_schema_designs.return_value = [
            {"target_type": "dynamodb", "content": {"tables": []}},
        ]
        response = client.get("/api/v1/assessments/job-1/schema-designs")
        assert response.status_code == 200
        data = response.json()
        assert len(data["schema_designs"]) == 1
        assert data["schema_designs"][0]["target_type"] == "dynamodb"

    def test_trade_off_prose_goes_through_the_style_list(self, mock_services):
        raw = "Reads typically hit the cache, roughly 2x faster."
        mock_services["s3"].read_collector.return_value = {}
        mock_services["s3"].read_all_schema_designs.return_value = [
            {
                "target_type": "dynamodb",
                "content": {
                    "trade_offs": [
                        {"description": "Posts keyed by topic.", "impact": raw},
                        {"description": "Counts are kept.", "impact": "Counts update on write."},
                    ]
                },
            }
        ]
        response = client.get("/api/v1/assessments/job-1/schema-designs")
        trade_offs = response.json()["schema_designs"][0]["content"]["trade_offs"]
        assert "typically" not in trade_offs[0]["impact"]
        assert trade_offs[1]["impact"] == "Counts update on write."

    def test_returns_empty_when_none(self, mock_services):
        mock_services["s3"].read_all_schema_designs.return_value = []
        response = client.get("/api/v1/assessments/job-1/schema-designs")
        assert response.status_code == 200
        assert len(response.json()["schema_designs"]) == 0


class TestResultsPageWording:
    """#996: wording the retired HTML report showed, now carried by the /results payload."""

    def test_the_summary_is_the_engineering_reports(self, mock_services):
        """#1403: the results page shows the summary the engineering report shows, one
        paragraph per part, worded from the report's facts."""
        from src.report.engineering_report import summary_paragraphs
        from tests.unit.report.test_executive_summary_paragraphs import _report

        report = _report()
        expected, _ = summary_paragraphs(copy.deepcopy(report))
        mock_services["s3"].read_synthesis.return_value = report
        mock_services["s3"].read_triage.return_value = None
        body = client.get("/api/v1/assessments/job-1/results").json()
        assert body["synthesis"]["summary"] == "\n\n".join(expected)
        assert body["synthesis"]["summary"].startswith("Recommendation: ")

    def test_older_summary_names_other_targets_by_display_name(self, mock_services):
        text = (
            "Aurora MySQL serves 67.5% of queries. Other targets evaluated: documentdb "
            "(consolidated into aurora_mysql by the reality check), opensearch (no queries "
            "assigned). The plan has three waves."
        )
        mock_services["s3"].read_synthesis.return_value = {
            "summary": text,
            "summary_deterministic": text,
        }
        mock_services["s3"].read_triage.return_value = None
        body = client.get("/api/v1/assessments/job-1/results").json()
        assert body["synthesis"]["summary"] == (
            "Aurora MySQL serves 67.5% of queries. Other targets evaluated: DocumentDB "
            "(consolidated into Aurora MySQL by the reality check), OpenSearch (no queries "
            "assigned). The plan has three waves."
        )

    def test_older_risk_count_phrase_reads_as_the_engineering_report_does(self, mock_services):
        text = "8 risk(s) identified (overall: LOW; 4 resolved by the assignment)."
        mock_services["s3"].read_synthesis.return_value = {
            "summary": text,
            "summary_deterministic": text,
        }
        mock_services["s3"].read_triage.return_value = None
        body = client.get("/api/v1/assessments/job-1/results").json()
        assert body["synthesis"]["summary"] == (
            "8 open risk(s) (overall: LOW); 4 more were resolved by the assignment."
        )

    def test_generated_summary_is_left_as_written(self, mock_services):
        generated = "Other targets evaluated: documentdb (no queries assigned)."
        mock_services["s3"].read_synthesis.return_value = {
            "summary": generated,
            "summary_deterministic": "A different deterministic summary.",
        }
        mock_services["s3"].read_triage.return_value = None
        body = client.get("/api/v1/assessments/job-1/results").json()
        assert body["synthesis"]["summary"] == generated

    def test_summary_without_the_clause_or_absent_is_untouched(self, mock_services):
        text = "documentdb is mentioned here, outside the clause."
        mock_services["s3"].read_synthesis.return_value = {
            "summary": text,
            "summary_deterministic": text,
        }
        mock_services["s3"].read_triage.return_value = None
        body = client.get("/api/v1/assessments/job-1/results").json()
        assert body["synthesis"]["summary"] == text
        mock_services["s3"].read_synthesis.return_value = {"ranking": []}
        body = client.get("/api/v1/assessments/job-1/results").json()
        assert "summary" not in body["synthesis"]

    def test_payload_notes_a_report_written_before_utility_pins_left_the_mean(self, mock_services):
        from src.shared.ranking import PIN_RULE_PREDATES_NOTE

        mock_services["s3"].read_triage.return_value = None
        mock_services["s3"].read_synthesis.return_value = {
            "ranking": [{"target": "aurora_mysql", "routed_queries": 5}]
        }
        body = client.get("/api/v1/assessments/job-1/results").json()
        assert body["pin_rule_note"] == PIN_RULE_PREDATES_NOTE
        mock_services["s3"].read_synthesis.return_value = {
            "ranking": [{"target": "aurora_mysql", "routed_queries": 5, "routed_pinned_queries": 0}]
        }
        body = client.get("/api/v1/assessments/job-1/results").json()
        assert body["pin_rule_note"] is None

    def test_payload_carries_the_shared_no_open_risks_sentence(self, mock_services):
        from src.report.risks import NO_OPEN_RISKS_CAVEAT

        mock_services["s3"].read_synthesis.return_value = {"ranking": []}
        mock_services["s3"].read_triage.return_value = None
        body = client.get("/api/v1/assessments/job-1/results").json()
        assert body["no_open_risks_caveat"] == NO_OPEN_RISKS_CAVEAT


def test_results_routes_never_block_the_event_loop() -> None:
    """#1593: these handlers read artifacts and clean prose synchronously. As
    ``async def`` they ran on the API's event loop, so on a large job every other
    request of the results page waited behind GET /schema-designs. Plain ``def``
    handlers run in FastAPI's thread pool."""
    import inspect

    from fastapi.routing import APIRoute

    blocking = [
        route.path
        for route in results.router.routes
        if isinstance(route, APIRoute) and inspect.iscoroutinefunction(route.endpoint)
    ]
    assert blocking == []
