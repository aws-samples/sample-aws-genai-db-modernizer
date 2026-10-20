"""A recorded PE review flags a wordpress query, and the reroute runs end to end (#740).

The live path of a PE-review routing flag (#629) -- the reviewer's
``PEReviewResult.routing_flags``, the design trace, the post-schema router and
its shared scorer, a new assignment version, the redesign, and "moved after
approval" in every deliverable -- had only unit tests with flags written
straight into a design trace. Here the flag comes out of the real DynamoDB
schema design path instead: ``run_schema_design_auto`` splits the approved
wordpress assignment into groups and runs each group's designer and PE review
loop (``SchemaDesignRunner``), with the two model calls replayed:

- the designer calls the input tool it was given, as a model does, and
  returns a design built from the queries it read (the committed wordpress
  DynamoDB design, one access pattern per query);
- every group's PE reviewer returns the recorded review in
  ``tests/fixtures/pe_flag/wordpress_dynamodb_pe_review.json`` as it is, so
  the reviews of groups that did not design the flagged query flag it too, as
  a live reviewer may. It flags the ``wp_options`` autoload read
  (``413c99e4...``) as a full scan on DynamoDB, and the router still moves it
  once.

No test calls a model. The expected outcome, and the commands for a live run,
are in ``tests/fixtures/pe_flag/README.md``. The query is first assigned to
DocumentDB (79 against DynamoDB's 78; a tie at 78 before #1197, which bills its
DocumentDB reads at the source's buffer-cache hit ratio, bounded); the reality check removed
DocumentDB and consolidated the query onto DynamoDB. The flag gates DynamoDB, and the shared scorer
sends it to Aurora MySQL, the best engine the plan keeps: DocumentDB scores
higher but was removed, and a flag never brings back a removed engine
(maintainer decision, 2026-10-14). v3 records the move, both engines the move
touched are designed again on v3, the DynamoDB redesign drops the query, and
every deliverable says it moved after approval. The scores are read from the
run (the assignment and the router's ladder), never written here.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from src.agents.referee.assignment_overrides import mark_assignment_customer_approved
from src.agents.schema_design import handler
from src.contracts.dynamodb_model_output import DynamoDBModelOutputContract
from src.contracts.dynamodb_pe_review import PEReviewResult
from src.orchestrator.local_orchestrator import LocalOrchestrator
from src.report import executive_report
from src.storage.local_store import LocalArtifactStore
from tests.e2e.pipeline import REPO, PipelineResult, _run, run_pipeline
from tests.shared_runs import cached_run, copy_run

DB, JOB = "wordpress", "job-pe-flag"
FIXTURE = REPO / "tests/fixtures/pe_flag/wordpress_dynamodb_pe_review.json"
# The wp_options autoload read. The sample's ids are 64-character digests; the
# fixture and this test name it by its start, and the replay expands that to
# the one full id that starts with it (``flagged["id"]``).
FLAGGED_PREFIX = "413c99e4"
TEMPLATE = json.loads(
    (
        REPO / "docs/examples/wordpress/load-test-results/schema-dynamodb/v2/schema_output.json"
    ).read_text()
)


def _recorded_review() -> dict[str, Any]:
    review: dict[str, Any] = json.loads(FIXTURE.read_text())
    review.pop("_comment", None)
    return review


def _design_from(queries: list[dict]) -> DynamoDBModelOutputContract:
    """The designer's answer: the committed design, one access pattern per query."""
    design = copy.deepcopy(TEMPLATE)
    tables = [t["table_name"] for t in design["table_definitions"]]
    pattern = design["access_patterns"][0]
    design["access_patterns"] = [
        {
            **pattern,
            "pattern_id": f"DDB-AP-{i + 1}",
            "query_ids": [q["query_id"]],
            "source_tables": list(q.get("tables_accessed") or pattern.get("source_tables") or []),
            "table_name": tables[0],
        }
        for i, q in enumerate(queries)
    ]
    return DynamoDBModelOutputContract.model_validate(design)


def _expanded_review(query_ids: list[str]) -> PEReviewResult:
    """The recording with each id prefix expanded against ``query_ids``."""
    review = _recorded_review()
    review["routing_flags"] = [
        {**flag, "query_ids": [q for p in flag["query_ids"] for q in query_ids if q.startswith(p)]}
        for flag in review["routing_flags"]
    ]
    return PEReviewResult.model_validate(review)


def _replay_agent(review: PEReviewResult) -> type:
    """A stand-in for ``strands.Agent`` in the DynamoDB designer: no model call."""

    class _ReplayAgent:
        def __init__(self, *, structured_output_model: type, tools: Any = (), **_: Any) -> None:
            self.kind = structured_output_model
            self.tools = list(tools or [])

        def __call__(self, prompt: str) -> SimpleNamespace:
            if self.kind is PEReviewResult:
                return SimpleNamespace(structured_output=review.model_copy(deep=True))
            [load_input] = [t for t in self.tools if t.tool_name == "load_agent_input"]
            queries = load_input()["collector"]["queries"]["query_patterns"]
            return SimpleNamespace(structured_output=_design_from(queries))

    return _ReplayAgent


_REAL_DESIGN = handler.run_schema_design_auto


def _designer(calls: list[tuple[str, int]]) -> Any:
    """``run_schema_design_auto`` as the orchestrator calls it: records every
    (engine, version), runs the real DynamoDB design with the model calls
    replayed and leaves the other engines undesigned (synthesis builds their
    groups from the assignment)."""

    def design(job_id, database_name, engine, store, assignment_version=0, llm_mode=""):
        calls.append((engine, assignment_version))
        if engine != "dynamodb":
            return None
        return _REAL_DESIGN(
            job_id, database_name, engine, store, assignment_version, llm_mode="bedrock"
        )

    return design


@pytest.fixture(scope="module")
def flagged(tmp_path_factory) -> dict:
    cached = cached_run(
        tmp_path_factory,
        f"pipeline-wordpress-{JOB}",
        lambda d: run_pipeline("wordpress", d / "artifacts", JOB),
    )
    root = copy_run(cached, tmp_path_factory.mktemp("pe-flag") / "run") / "artifacts"
    store = LocalArtifactStore(base_dir=str(root))
    mark_assignment_customer_approved(store, DB, JOB)
    prefix = f"{DB}/{JOB}"
    v2 = store.read_json(f"{prefix}/assignment/v2/assignment.json")
    calls: list[tuple[str, int]] = []
    design = _designer(calls)
    review = _expanded_review([qa["query_id"] for qa in v2["query_assignments"]])
    with (
        patch("src.tools.schema.dynamodb_schema_agent.Agent", _replay_agent(review)),
        patch("src.tools.schema.dynamodb_schema_agent._build_model", return_value=object()),
    ):
        design(JOB, DB, "dynamodb", store, 2)
        trace_v2 = store.read_json(f"{prefix}/schema-dynamodb/v2/design_trace.json")
        manifest_v2 = store.read_json(f"{prefix}/schema-dynamodb/v2/groups_manifest.json")
        with patch("src.agents.schema_design.handler.run_schema_design_auto", side_effect=design):
            LocalOrchestrator(store, llm_mode="bedrock")._run_post_schema_routing(JOB, DB)
    result = PipelineResult(db=DB, job_id=JOB, artifact_root=root)
    common = ["--job-id", JOB, "--db", DB, "--artifact-root", str(root)]
    _run("synthesis", ["scripts/run_synthesis.py", *common, "--llm-mode", "none"], result)
    _run("report", ["scripts/run_report.py", *common], result)
    folder = root / DB / JOB / "deliverables"
    [full_id] = [
        qa["query_id"]
        for qa in v2["query_assignments"]
        if qa["query_id"].startswith(FLAGGED_PREFIX)
    ]
    return {
        "id": full_id,
        "design_calls": calls,
        "trace_v2": trace_v2,
        "manifest_v2": manifest_v2,
        "router": store.read_json(f"{prefix}/post-schema-router/router_output.json"),
        "v2": v2,
        "v3": store.read_json(f"{prefix}/assignment/v3/assignment.json"),
        "has_v4": store.exists(f"{prefix}/assignment/v4/assignment.json"),
        "design_v3": store.read_json(f"{prefix}/schema-dynamodb/v3/schema_output.json"),
        "report": json.loads((root / DB / JOB / "synthesis" / "v3" / "report.json").read_text()),
        "engineering": (folder / "engineering-report.md").read_text(),
        "pdf": _pdf_text(folder / executive_report.FILENAME),
    }


def _pdf_text(path: Path) -> str:
    """The executive report's text, page furniture removed (a card may continue)."""
    from tests.executive_pdf import body_text

    return body_text(path, DB)


def _engine_of(assignment: dict) -> dict[str, str]:
    return {qa["query_id"]: qa["assigned_engine"] for qa in assignment["query_assignments"]}


class TestTheReviewFlagsTheQuery:
    def test_the_fixture_is_a_valid_review_flagging_one_dynamodb_query(self) -> None:
        review = PEReviewResult.model_validate(_recorded_review())
        [flag] = review.routing_flags
        assert flag["query_ids"] == [FLAGGED_PREFIX] and flag["flagged_engine"] == "dynamodb"

    def test_the_query_is_on_dynamodb_in_the_approved_version(self, flagged) -> None:
        assert flagged["v2"]["status"] == "customer_approved"
        assert _engine_of(flagged["v2"])[flagged["id"]] == "dynamodb"

    def test_every_groups_review_carries_the_flag_into_the_trace(self, flagged) -> None:
        """Each group's review flags the query, the groups that did not design it
        too: the trace keeps what each review returned. The group count is the
        run's own (its groups manifest): 4 since #992, whose write groups put 39
        wordpress queries on DynamoDB."""
        from src.agents.referee.post_schema_router import routing_flags_from_trace

        groups = flagged["trace_v2"].get("groups") or []
        # wordpress has more DynamoDB queries than one group holds.
        assert len(groups) == len(flagged["manifest_v2"]["groups"]) > 1
        flags = routing_flags_from_trace(flagged["trace_v2"])
        assert [(f["query_ids"], f["reason_category"]) for f in flags] == [
            ([flagged["id"]], "full_scan")
        ] * len(groups)


class TestTheRouterMovesItByScore:
    def test_it_moves_to_the_best_kept_engine_on_a_new_version(self, flagged) -> None:
        engine_of = _engine_of(flagged["v3"])
        assert engine_of[flagged["id"]] == "aurora_mysql"
        # Nothing else moved.
        before = _engine_of(flagged["v2"])
        assert {q for q, e in engine_of.items() if before[q] != e} == {flagged["id"]}
        assert flagged["v3"]["status"] == "rerouted_after_approval"

    # #1197 re-anchored where DynamoDB's score is read; the behaviour is main's.
    @pytest.mark.passes_on_main
    def test_the_move_is_a_post_schema_flag_move_with_the_scorers_scores(self, flagged) -> None:
        [routing] = flagged["router"]["routings"]
        assert (routing["query_id"], routing["from_engine"], routing["to_engine"]) == (
            flagged["id"],
            "dynamodb",
            "aurora_mysql",
        )
        assert routing["reason_category"] == "full_scan"
        ladder = {c["engine"]: c for c in routing["evidence"]["candidates"]}
        assert ladder["dynamodb"]["gate"] == "model_flag"
        # The scores are the run's own: DynamoDB's is the approved assignment's
        # (ignoring the flag), the score the reality check's Move onto DynamoDB
        # recorded (#1197: the query reached DynamoDB by consolidation from DocumentDB,
        # so its ``confidence`` still shows DocumentDB's 79); Aurora MySQL's is the
        # router's ladder entry.
        [onto] = [
            m
            for m in flagged["v2"]["moves"]
            if m["query_id"] == flagged["id"] and m["to_engine"] == "dynamodb"
        ]
        assert routing["score_before"] == onto["score_after"]
        assert routing["score_after"] == ladder["aurora_mysql"]["score"]
        assert routing["score_after"] < routing["score_before"]
        [move] = [m for m in flagged["v3"]["moves"] if m["query_id"] == flagged["id"]]
        assert (move["pass"], move["reason_code"], move["actor"]) == (
            "post_schema_flag",
            "flag.capability_gap",
            "model_flag",
        )
        assert flagged["router"]["terminal_queries"] == []

    def test_a_removed_engine_never_comes_back(self, flagged) -> None:
        """DocumentDB outscores Aurora MySQL for the query but the reality check
        removed it; the flag re-routes only among kept engines."""
        [routing] = flagged["router"]["routings"]
        ladder = {c["engine"]: c["score"] for c in routing["evidence"]["candidates"]}
        assert ladder["documentdb"] > ladder["aurora_mysql"]
        kept = {qa["assigned_engine"] for qa in flagged["v2"]["query_assignments"]}
        assert "documentdb" not in kept
        assert "documentdb" not in set(_engine_of(flagged["v3"]).values())

    def test_both_engines_the_move_touched_are_designed_again(self, flagged) -> None:
        """Amendment 2: the engine the query left and the one it joined."""
        assert {e for e, v in flagged["design_calls"] if v == 3} == {"dynamodb", "aurora_mysql"}

    def test_the_redesign_drops_it_from_dynamodb_and_no_second_move_follows(self, flagged) -> None:
        designed = {q for ap in flagged["design_v3"]["access_patterns"] for q in ap["query_ids"]}
        assert flagged["id"] not in designed
        # The redesign on v3 covers exactly DynamoDB's v3 queries.
        assert designed == {
            qa["query_id"]
            for qa in flagged["v3"]["query_assignments"]
            if qa["assigned_engine"] == "dynamodb" and qa.get("in_scope", True)
        }
        assert not flagged["has_v4"]


class TestEveryDeliverableSaysItMovedAfterApproval:
    def test_report_json_lists_the_move(self, flagged) -> None:
        [reroute] = flagged["report"]["post_schema_reroutes"]
        assert (reroute["query_id"], reroute["from_engine"], reroute["to_engine"]) == (
            flagged["id"],
            "dynamodb",
            "aurora_mysql",
        )
        assert reroute["approved_version"] == 2
        assert flagged["report"]["assignment_summary"]["status"] == "rerouted_after_approval"

    def test_the_engineering_report_and_the_pdf_state_it(self, flagged) -> None:
        from src.report.engine_choices import REROUTE_OVERRIDE_TEXT

        assert "## Moved after approval (1 query)" in flagged["engineering"]
        assert REROUTE_OVERRIDE_TEXT in flagged["engineering"]
        assert flagged["id"][:12] in flagged["engineering"]
        assert "Moved after approval" in flagged["pdf"]
        assert " ".join(REROUTE_OVERRIDE_TEXT.split()) in flagged["pdf"]
