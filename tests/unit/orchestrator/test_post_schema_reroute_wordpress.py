"""#713 review round 2 repro: a PE flag on wordpress moves one query after approval.

The deterministic wordpress pipeline runs to the approved assignment (v2), the
customer approves it, DynamoDB gets a design (a stand-in designer that designs
exactly the engine's assigned queries), and DynamoDB's PE review flags
``b05af520...`` (an ``INSERT INTO wp_actionscheduler_logs``). The post-schema router
moves it to Aurora MySQL (51 against 90) and writes v3. Then synthesis and the reports run on v3, and every deliverable
must agree with the v3 assignment: the moved query appears once, on Aurora
MySQL; DynamoDB's counts match its own v3 queries; the move is listed as made
after approval, with the override path; and v3 never claims the customer
approved it.

#920: the query this test used to flag, ``d72966f8`` (a ``REPLACE INTO
wp_wc_product_meta_lookup``), is no longer on DynamoDB: DocumentDB's product-catalog
fired on its wide table by column count alone, and DynamoDB's call-rate fit is gone,
so it stays on Aurora MySQL from the first assignment.

#728: the INSERT is a key lookup by its own class, so it scores DynamoDB 90 (the
key-lookup class, 100, with the table's other dimensions) and Aurora MySQL 71 less
no-relational-need's 20 = 51, where its table's pattern scores gave 30 and 47 less 20.
"""

from __future__ import annotations

import copy
import json
import re
from unittest.mock import patch

import pytest

from src.agents.referee.assignment_overrides import mark_assignment_customer_approved
from src.orchestrator.local_orchestrator import LocalOrchestrator
from src.storage.local_store import LocalArtifactStore
from tests.e2e.pipeline import REPO, PipelineResult, _run, run_pipeline
from tests.shared_runs import cached_run, copy_run

DB, JOB = "wordpress", "job-reroute"
FLAGGED = "b05af520"
TEMPLATE = json.loads(
    (
        REPO / "docs/examples/wordpress/load-test-results/schema-dynamodb/v2/schema_output.json"
    ).read_text()
)


def _fake_design(job_id, database_name, engine, store, assignment_version=0, llm_mode=""):
    """Designs exactly the engine's in-scope queries on that version (DynamoDB
    only; Aurora MySQL is left undesigned, so synthesis builds its groups from
    the assignment)."""
    if engine != "dynamodb":
        return
    assignment = store.read_json(
        f"{database_name}/{job_id}/assignment/v{assignment_version}/assignment.json"
    )
    queries = [
        qa["query_id"]
        for qa in assignment["query_assignments"]
        if qa["assigned_engine"] == engine and qa.get("in_scope", True)
    ]
    design = copy.deepcopy(TEMPLATE)
    design["job_id"] = job_id
    template = TEMPLATE["access_patterns"][0]
    design["access_patterns"] = [
        {**template, "pattern_id": f"DDB-AP-{i + 1}", "query_ids": [qid]}
        for i, qid in enumerate(queries)
    ]
    base = f"{database_name}/{job_id}/schema-{engine}/v{assignment_version}"
    store.write_json(f"{base}/schema_output.json", design)
    store.write_json(f"{base}/design_trace.json", {"trace": []})


def _pipeline_copy(tmp_path_factory, name: str):
    """A private copy of the deterministic wordpress run (built once per session)."""
    cached = cached_run(
        tmp_path_factory,
        f"pipeline-wordpress-{JOB}",
        lambda d: run_pipeline("wordpress", d / "artifacts", JOB),
    )
    return copy_run(cached, tmp_path_factory.mktemp(name) / "run") / "artifacts"


@pytest.fixture(scope="module")
def rerouted(tmp_path_factory) -> dict:
    root = _pipeline_copy(tmp_path_factory, "reroute")
    store = LocalArtifactStore(base_dir=str(root))
    mark_assignment_customer_approved(store, DB, JOB)
    _fake_design(JOB, DB, "dynamodb", store, 2)
    v2 = store.read_json(f"{DB}/{JOB}/assignment/v2/assignment.json")
    flagged = next(
        qa["query_id"] for qa in v2["query_assignments"] if qa["query_id"].startswith(FLAGGED)
    )
    store.write_json(
        f"{DB}/{JOB}/schema-dynamodb/v2/design_trace.json",
        {
            "trace": [],
            "routing_flags": [
                {
                    "query_ids": [flagged],
                    "flagged_engine": "dynamodb",
                    "reason_category": "client_side_join",
                    "explanation": "needs a server-side join",
                }
            ],
        },
    )
    with patch("src.agents.schema_design.handler.run_schema_design_auto", side_effect=_fake_design):
        LocalOrchestrator(store, llm_mode="bedrock")._run_post_schema_routing(JOB, DB)
    result = PipelineResult(db=DB, job_id=JOB, artifact_root=root)
    common = ["--job-id", JOB, "--db", DB, "--artifact-root", str(root)]
    _run("synthesis", ["scripts/run_synthesis.py", *common, "--llm-mode", "none"], result)
    _run("report", ["scripts/run_report.py", *common], result)
    synthesis = root / DB / JOB / "synthesis" / "v3"
    folder = root / DB / JOB / "deliverables"
    return {
        "flagged": flagged,
        "v2": v2,
        "v3": store.read_json(f"{DB}/{JOB}/assignment/v3/assignment.json"),
        "v3_access_patterns": store.read_json(f"{DB}/{JOB}/assignment/v3/access_patterns.json"),
        "report": json.loads((synthesis / "report.json").read_text()),
        "pdf": _pdf_text(folder / "executive-report.pdf"),
        "engineering": (folder / "engineering-report.md").read_text(),
    }


def _pdf_text(path) -> str:
    from pypdf import PdfReader

    return " ".join(" ".join((p.extract_text() or "").split()) for p in PdfReader(path).pages)


def _in_scope(assignment: dict, engine: str) -> int:
    return sum(
        1
        for qa in assignment["query_assignments"]
        if qa["assigned_engine"] == engine and qa.get("in_scope", True)
    )


class TestTheRerouteOnWordpress:
    def test_the_query_moves_to_the_best_kept_engine_on_a_new_version(self, rerouted):
        engine_of = {
            qa["query_id"]: qa["assigned_engine"] for qa in rerouted["v3"]["query_assignments"]
        }
        assert engine_of[rerouted["flagged"]] == "aurora_mysql"
        # The reroute's version carries its access-pattern record too (#728).
        assert rerouted["v3_access_patterns"]["queries"]
        (reroute,) = rerouted["v3"]["post_schema_reroutes"]
        # #494 slice 2: the INSERT's cost dimension is its own, not its table's cost band:
        # DynamoDB 100 (its writes cost $0.16 a month) instead of 65, Aurora MySQL 98 (its
        # $250 standing cost shared by 107 queries) instead of 55; the scores were 90 and 51.
        assert (reroute["score_before"], reroute["score_after"]) == (95, 55)

    def test_the_rerouted_version_never_claims_customer_approval(self, rerouted):
        assert rerouted["v2"]["status"] == "customer_approved"
        assert rerouted["v3"]["status"] == "rerouted_after_approval"
        assert rerouted["report"]["assignment_summary"]["status"] == "rerouted_after_approval"


class TestEveryDeliverableAgreesWithTheNewVersion:
    def test_the_moved_query_is_in_exactly_one_query_group_on_aurora(self, rerouted):
        groups = rerouted["report"]["query_groups"]
        holders = [
            g["engines"]
            for g in groups
            for ap in g["access_patterns"]
            if rerouted["flagged"] in (ap.get("query_ids") or [])
        ]
        assert holders == [["aurora_mysql"]]
        total = sum(len(ap.get("query_ids") or []) for g in groups for ap in g["access_patterns"])
        assert total == sum(
            1 for qa in rerouted["v3"]["query_assignments"] if qa.get("in_scope", True)
        )

    def test_dynamodb_counts_match_its_own_queries(self, rerouted):
        expected = _in_scope(rerouted["v3"], "dynamodb")
        assert expected == _in_scope(rerouted["v2"], "dynamodb") - 1
        # The summary is the engineering report's since #1020 (the executive report
        # prints no summary: it names tables).
        text = " ".join(re.sub(r"\\(.)", r"\1", rerouted["engineering"]).split())
        assert f"DynamoDB handles {expected} queries" in text
        assert f"{expected} in-scope access patterns)" in text
        assert f"DynamoDB: {_in_scope(rerouted['v2'], 'dynamodb')}" not in text

    def test_the_reroute_records_pass_the_customer_text_guard(self, rerouted):
        from src.tools.validation.customer_text_guard import scan_json

        findings = scan_json({"post_schema_reroutes": rerouted["report"]["post_schema_reroutes"]})
        assert findings == []

    def test_no_risk_or_unsupported_pattern_names_the_moved_query_on_dynamodb(self, rerouted):
        risks = rerouted["report"].get("risk_assessment", {}).get("risks", [])
        assert not [r for r in risks if rerouted["flagged"][:12] in json.dumps(r)]
        assert "Unsupported patterns" not in rerouted["engineering"].split("## Moved after")[0]

    def test_the_move_after_approval_and_the_override_path_are_in_the_report(self, rerouted):
        from src.report.engine_choices import REROUTE_OVERRIDE_TEXT

        assert "## Moved after approval (1 query)" in rerouted["engineering"]
        assert REROUTE_OVERRIDE_TEXT in rerouted["engineering"]
        assert rerouted["flagged"] in rerouted["engineering"]
        assert rerouted["report"]["post_schema_reroutes"][0]["approved_version"] == 2


@pytest.fixture(scope="module")
def overridden(tmp_path_factory) -> dict:
    """#713 review round 3 probe: v3 moves the query to Aurora MySQL; the
    customer moves it back to DynamoDB at the review (v4, customer_override);
    DynamoDB is redesigned on v4 and its PE review flags the query again; the
    router runs again. The customer's choice must hold: no v5."""
    from src.agents.referee.assignment_overrides import (
        QueryOverrideInput,
        apply_assignment_overrides,
    )

    root = _pipeline_copy(tmp_path_factory, "override")
    store = LocalArtifactStore(base_dir=str(root))
    mark_assignment_customer_approved(store, DB, JOB)
    _fake_design(JOB, DB, "dynamodb", store, 2)
    v2 = store.read_json(f"{DB}/{JOB}/assignment/v2/assignment.json")
    flagged = next(
        qa["query_id"] for qa in v2["query_assignments"] if qa["query_id"].startswith(FLAGGED)
    )
    flag = {
        "query_ids": [flagged],
        "flagged_engine": "dynamodb",
        "reason_category": "client_side_join",
        "explanation": "needs a server-side join",
    }
    store.write_json(
        f"{DB}/{JOB}/schema-dynamodb/v2/design_trace.json", {"trace": [], "routing_flags": [flag]}
    )
    orch = LocalOrchestrator(store, llm_mode="bedrock")
    with patch("src.agents.schema_design.handler.run_schema_design_auto", side_effect=_fake_design):
        orch._run_post_schema_routing(JOB, DB)
        apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput(query_id=flagged, assigned_engine="dynamodb")]
        )
        for engine in ("dynamodb", "aurora_mysql"):
            _fake_design(JOB, DB, engine, store, 4)
        store.write_json(
            f"{DB}/{JOB}/schema-dynamodb/v4/design_trace.json",
            {"trace": [], "routing_flags": [flag]},
        )
        for engine in ("documentdb", "elasticache", "opensearch"):
            orch._copy_schema_artifacts(JOB, DB, engine, 3, 4)
        orch._run_post_schema_routing(JOB, DB)
    result = PipelineResult(db=DB, job_id=JOB, artifact_root=root)
    common = ["--job-id", JOB, "--db", DB, "--artifact-root", str(root)]
    _run("synthesis", ["scripts/run_synthesis.py", *common, "--llm-mode", "none"], result)
    _run("report", ["scripts/run_report.py", *common], result)
    synthesis = root / DB / JOB / "synthesis" / "v4"
    return {
        "store": store,
        "flagged": flagged,
        "v4": store.read_json(f"{DB}/{JOB}/assignment/v4/assignment.json"),
        "report": json.loads((synthesis / "report.json").read_text()),
        "engineering": (root / DB / JOB / "deliverables" / "engineering-report.md").read_text(),
    }


class TestTheCustomersMoveBackHolds:
    def test_a_later_flag_never_reroutes_the_customers_choice(self, overridden):
        store = overridden["store"]
        assert not store.exists(f"{DB}/{JOB}/assignment/v5/assignment.json")
        engine_of = {qa["query_id"]: qa for qa in overridden["v4"]["query_assignments"]}
        assert engine_of[overridden["flagged"]]["assigned_engine"] == "dynamodb"
        assert engine_of[overridden["flagged"]]["customer_override"] is True

    def test_the_undone_move_is_not_listed(self, overridden):
        assert overridden["v4"]["post_schema_reroutes"] == []
        assert overridden["report"]["post_schema_reroutes"] == []
        assert "Moved after approval" not in overridden["engineering"]


def test_a_design_trace_off_the_contract_stops_the_post_schema_pass(tmp_path_factory) -> None:
    """The pass reads the PE flags through the design-trace contract (#1258): a
    ``routing_flags`` of the wrong type fails the pass, never reads as no flags."""
    from src.agents.schema_design.design_trace import DesignTraceShapeError

    root = _pipeline_copy(tmp_path_factory, "offcontract")
    store = LocalArtifactStore(base_dir=str(root))
    mark_assignment_customer_approved(store, DB, JOB)
    _fake_design(JOB, DB, "dynamodb", store, 2)
    store.write_json(
        f"{DB}/{JOB}/schema-dynamodb/v2/design_trace.json", {"trace": [], "routing_flags": "q-1"}
    )
    with patch("src.agents.schema_design.handler.run_schema_design_auto", side_effect=_fake_design):
        with pytest.raises(DesignTraceShapeError, match="routing_flags"):
            LocalOrchestrator(store, llm_mode="bedrock")._run_post_schema_routing(JOB, DB)
    assert not store.exists(f"{DB}/{JOB}/assignment/v3/assignment.json")
