"""PE reviewers flag queries; they never name a target engine (#629).

The DynamoDB, DocumentDB and OpenSearch PE reviewers report a query their engine
cannot serve as a structured routing flag (``PEReviewResult.routing_flags``,
the :class:`~src.contracts.model_flag.ModelFlag` contract). The flag is
recorded in the design trace, and the post-schema router picks the destination
by score (ADR-031 Decision 11, as amended 2026-10-14).
"""

from __future__ import annotations

import re
import types
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from src.contracts.dynamodb_pe_review import PEReviewResult as DynamoDBPEReview
from src.contracts.model_flag import ModelFlag, parse_model_flags
from src.orchestrator.local_orchestrator import LocalOrchestrator
from src.storage.local_store import LocalArtifactStore
from src.tools.schema import base_schema_agent as bsa
from src.tools.schema.documentdb_schema_agent import PEReviewResult as DocumentDBPEReview
from src.tools.schema.opensearch_schema_agent import PEReviewResult as OpenSearchPEReview

REPO = Path(__file__).resolve().parents[4]
PE_SKILLS = {
    "dynamodb": REPO / "src/skills/dynamodb-pe-review.md",
    "documentdb": REPO / "src/skills/documentdb-pe-review.md",
    "opensearch": REPO / "src/skills/opensearch-pe-review.md",
}
OTHER_ENGINE = re.compile(
    r"dynamodb|documentdb|opensearch|aurora|elasticache|neptune|keyspaces", re.IGNORECASE
)


def _flag(engine: str = "dynamodb") -> dict:
    return {
        "query_ids": ["q-1"],
        "flagged_engine": engine,
        "reason_category": "full_scan",
        "explanation": "no key to query",
    }


class TestPromptsCarryNoEnginePreference:
    @pytest.mark.parametrize("engine", sorted(PE_SKILLS))
    def test_no_free_text_routing_note_or_target_engine(self, engine: str) -> None:
        text = PE_SKILLS[engine].read_text(encoding="utf-8")
        assert "[ROUTING]" not in text
        assert not re.search(r"route to \w+", text, re.IGNORECASE)
        assert "→ " + engine not in text
        # Another engine's name never appears: the skill cannot steer a query there.
        others = {m.lower() for m in OTHER_ENGINE.findall(text)} - {engine}
        assert others == set(), others

    @pytest.mark.parametrize("engine", sorted(PE_SKILLS))
    def test_the_skill_asks_for_the_structured_flag(self, engine: str) -> None:
        text = PE_SKILLS[engine].read_text(encoding="utf-8")
        assert "routing_flags" in text
        assert f'"flagged_engine": "{engine}"' in text
        assert "never names another engine" in text or "Never name a target engine" in text


class TestPEReviewModelsCarryTheFlagContract:
    @pytest.mark.parametrize("model", [DynamoDBPEReview, DocumentDBPEReview, OpenSearchPEReview])
    def test_the_model_sees_the_flag_schema(self, model: type[BaseModel]) -> None:
        items = model.model_json_schema()["properties"]["routing_flags"]["items"]
        assert items["additionalProperties"] is False
        assert set(items["properties"]) == {
            "query_ids",
            "flagged_engine",
            "reason_category",
            "explanation",
        }

    @pytest.mark.parametrize("model", [DynamoDBPEReview, DocumentDBPEReview, OpenSearchPEReview])
    def test_a_bad_flag_is_dropped_and_the_review_kept(self, model: type[BaseModel]) -> None:
        """#713 review: one bad flag no longer fails the whole review; the
        router validates each entry, as the reality check does (#712)."""
        bad = {**_flag(), "target_engine": "opensearch"}
        review = model.model_validate(
            {"verdict": "APPROVED", "summary": "ok", "routing_flags": [_flag(), bad]}
        )
        flags, rejected = parse_model_flags(review.routing_flags)  # type: ignore[attr-defined]
        assert flags == [ModelFlag.model_validate(_flag())]
        assert len(rejected) == 1


class _Out(BaseModel):
    ok: bool = True


class TestTheRunnerRecordsTheFlagsInTheTrace:
    def test_the_last_reviews_routing_flags_reach_the_trace(self) -> None:
        review = DynamoDBPEReview.model_validate(
            {"verdict": "APPROVED", "summary": "ok", "routing_flags": [_flag()]}
        )
        runner = bsa.SchemaDesignRunner(
            target_type="dynamodb",
            output_model=_Out,
            model=types.SimpleNamespace(),  # type: ignore[arg-type]
            designer_agent=lambda _prompt: types.SimpleNamespace(structured_output=_Out()),  # type: ignore[arg-type]
            pe_reviewer_fn=lambda *_args: review,
        )

        _, trace = runner.run("design it")

        assert trace["routing_flags"] == [_flag()]

    def test_a_later_review_that_errors_leaves_no_stale_flags(self) -> None:
        """#713 review: flags from a review of an earlier design never reach the
        trace when the review of the accepted design failed."""
        flagged = DynamoDBPEReview.model_validate(
            {
                "verdict": "CHANGES_REQUESTED",
                "summary": "fix",
                "change_requests": [
                    {
                        "category": "key_design",
                        "severity": "major",
                        "target": "t",
                        "current_state": "a",
                        "requested_change": "b",
                        "rationale": "c",
                    }
                ],
                "routing_flags": [_flag()],
            }
        )
        reviews = iter([flagged])

        def reviewer(*_args):
            try:
                return next(reviews)
            except StopIteration:
                raise RuntimeError("review failed") from None

        runner = bsa.SchemaDesignRunner(
            target_type="dynamodb",
            output_model=_Out,
            model=types.SimpleNamespace(),  # type: ignore[arg-type]
            designer_agent=lambda _prompt: types.SimpleNamespace(structured_output=_Out()),  # type: ignore[arg-type]
            pe_reviewer_fn=reviewer,
            format_pe_feedback_fn=lambda _review: "fix it",
        )

        _, trace = runner.run("design it")

        assert "routing_flags" not in trace

    def test_no_flags_means_no_trace_key(self) -> None:
        review = DynamoDBPEReview.model_validate({"verdict": "APPROVED", "summary": "ok"})
        runner = bsa.SchemaDesignRunner(
            target_type="dynamodb",
            output_model=_Out,
            model=types.SimpleNamespace(),  # type: ignore[arg-type]
            designer_agent=lambda _prompt: types.SimpleNamespace(structured_output=_Out()),  # type: ignore[arg-type]
            pe_reviewer_fn=lambda *_args: review,
        )

        _, trace = runner.run("design it")

        assert "routing_flags" not in trace


class TestTheOrchestratorRoutesAFlagByScore:
    DB, JOB = "shop", "job-1"

    def _seed(self, store: LocalArtifactStore) -> None:
        prefix = f"{self.DB}/{self.JOB}"
        store.write_json(
            f"{prefix}/referee-triage/triage.json",
            {
                "selected_agents": [
                    {"agent_type": "dynamodb"},
                    {"agent_type": "documentdb"},
                    {"agent_type": "aurora_mysql"},
                ],
                "signals": [],
                "query_capabilities": {},
            },
        )
        store.write_json(
            f"{prefix}/collector/output.json",
            {
                "metadata": {"source_database": {"engine": "mysql"}},
                "queries": {
                    "query_patterns": [
                        {
                            "query_id": qid,
                            "query_text": f"SELECT * FROM {table} WHERE id = ?",
                            "query_type": "SELECT",
                            "tables_accessed": [table],
                        }
                        for qid, table in (
                            ("q-1", "t_q1"),
                            ("q-2", "t_q2"),
                            ("q-3", "t_q3"),
                            ("q-4", "t_q4"),
                        )
                    ]
                },
            },
        )
        for engine, score in (("dynamodb", 90), ("documentdb", 55), ("aurora_mysql", 70)):
            store.write_json(
                f"{prefix}/analysis-{engine}/analysis.json",
                {"table_recommendations": [{"table_id": "t_q1", "confidence_score": score}]},
            )
        store.write_json(
            f"{prefix}/assignment/v1/assignment.json",
            {
                "version": 1,
                "query_assignments": [
                    {"query_id": "q-1", "assigned_engine": "dynamodb"},
                    {"query_id": "q-2", "assigned_engine": "aurora_mysql"},
                    {"query_id": "q-3", "assigned_engine": "documentdb"},
                    {"query_id": "q-4", "assigned_engine": "dynamodb"},
                ],
            },
        )
        for engine in ("dynamodb", "documentdb", "aurora_mysql"):
            store.write_json(f"{prefix}/schema-{engine}/v1/schema_output.json", {})
        # The flag is written the way a grouped DynamoDB design trace carries it.
        store.write_json(
            f"{prefix}/schema-dynamodb/v1/design_trace.json",
            {"total_groups": 1, "groups": [{"routing_flags": [_flag()]}]},
        )

    @staticmethod
    def _fake_design(calls: list):
        """A stand-in for ``run_schema_design_auto``: designs exactly the
        engine's in-scope queries on that assignment version, with a trace."""

        def design(job_id, database_name, engine, store, assignment_version=0, llm_mode=""):
            calls.append((engine, assignment_version))
            prefix = f"{database_name}/{job_id}"
            assignment = store.read_json(
                f"{prefix}/assignment/v{assignment_version}/assignment.json"
            )
            designed = sorted(
                qa["query_id"]
                for qa in assignment["query_assignments"]
                if qa["assigned_engine"] == engine and qa.get("in_scope", True)
            )
            base = f"{prefix}/schema-{engine}/v{assignment_version}"
            store.write_json(f"{base}/schema_output.json", {"designed": designed})
            store.write_json(f"{base}/design_trace.json", {"trace": [], "designed": designed})

        return design

    def _run(self, store, calls: list) -> None:
        orch = LocalOrchestrator(store, llm_mode="bedrock")
        with patch(
            "src.agents.schema_design.handler.run_schema_design_auto",
            side_effect=self._fake_design(calls),
        ):
            orch._run_post_schema_routing(self.JOB, self.DB)

    @pytest.mark.usefixtures("schema_contract_off")
    def test_the_flagged_query_goes_to_the_best_remaining_engine(self, tmp_path) -> None:
        store = LocalArtifactStore(base_dir=str(tmp_path))
        self._seed(store)
        calls: list = []
        self._run(store, calls)

        v2 = store.read_json(f"{self.DB}/{self.JOB}/assignment/v2/assignment.json")
        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in v2["query_assignments"]}
        # DynamoDB (90) is gated by the flag; Aurora MySQL (70) beats DocumentDB (55).
        assert engine_of["q-1"] == "aurora_mysql"
        # Both engines the move touched are redesigned on v2; DocumentDB is copied.
        assert sorted(calls) == [("aurora_mysql", 2), ("dynamodb", 2)]
        # The next pass reads the redesigned traces (no flags), so nothing more moves.
        assert not store.exists(f"{self.DB}/{self.JOB}/assignment/v3/assignment.json")

    def test_a_removed_engine_is_never_a_destination(self, tmp_path) -> None:
        """Maintainer decision (2026-10-14): PE review routes only among the
        engines the approved assignment keeps. Aurora MySQL is triage-selected
        but has no in-scope query (the reality check removed it)."""
        store = LocalArtifactStore(base_dir=str(tmp_path))
        self._seed(store)
        prefix = f"{self.DB}/{self.JOB}"
        store.write_json(
            f"{prefix}/assignment/v1/assignment.json",
            {
                "version": 1,
                "query_assignments": [
                    {"query_id": "q-1", "assigned_engine": "dynamodb"},
                    {"query_id": "q-2", "assigned_engine": "documentdb"},
                ],
            },
        )
        self._run(store, [])

        v2 = store.read_json(f"{prefix}/assignment/v2/assignment.json")
        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in v2["query_assignments"]}
        assert engine_of["q-1"] == "documentdb"

    @pytest.mark.usefixtures("schema_contract_off")
    def test_the_new_version_and_every_design_agree(self, tmp_path) -> None:
        """#713 round 2: the engine a query left is redesigned on the new
        version (never copied forward with the moved query), the engine it
        joined covers it, and the untouched engine's design is carried over."""
        store = LocalArtifactStore(base_dir=str(tmp_path))
        self._seed(store)
        prefix = f"{self.DB}/{self.JOB}"
        store.write_json(
            f"{prefix}/schema-dynamodb/v1/schema_output.json", {"designed": ["q-1", "q-4"]}
        )
        store.write_json(f"{prefix}/schema-documentdb/v1/schema_output.json", {"designed": ["q-3"]})
        self._run(store, [])

        v2 = store.read_json(f"{prefix}/assignment/v2/assignment.json")
        for engine in ("dynamodb", "documentdb", "aurora_mysql"):
            design = store.read_json(f"{prefix}/schema-{engine}/v2/schema_output.json")
            assigned = sorted(
                qa["query_id"] for qa in v2["query_assignments"] if qa["assigned_engine"] == engine
            )
            assert design["designed"] == assigned, engine
        # The engine q-1 left no longer lists it.
        assert store.read_json(f"{prefix}/schema-dynamodb/v2/schema_output.json") == {
            "designed": ["q-4"]
        }

    @pytest.mark.usefixtures("table_confidence_scores")  # #728: scores per table
    @pytest.mark.usefixtures("schema_contract_off")
    def test_the_rerouted_version_never_claims_customer_approval(self, tmp_path) -> None:
        """#713 round 2: the customer approved v1, not the move; v2 has its own
        status and lists the move with the version that was approved."""
        store = LocalArtifactStore(base_dir=str(tmp_path))
        self._seed(store)
        prefix = f"{self.DB}/{self.JOB}"
        v1 = store.read_json(f"{prefix}/assignment/v1/assignment.json")
        store.write_json(
            f"{prefix}/assignment/v1/assignment.json", {**v1, "status": "customer_approved"}
        )
        self._run(store, [])

        v2 = store.read_json(f"{prefix}/assignment/v2/assignment.json")
        assert v2["status"] == "rerouted_after_approval"
        assert v2["source"] == "post_schema_routing"
        assert v2["previous_version"] == 1
        (reroute,) = v2["post_schema_reroutes"]
        assert (reroute["query_id"], reroute["from_engine"], reroute["to_engine"]) == (
            "q-1",
            "dynamodb",
            "aurora_mysql",
        )
        assert reroute["approved_version"] == 1
        assert (reroute["score_before"], reroute["score_after"]) == (90, 70)

    @pytest.mark.usefixtures("table_confidence_scores")  # #728: scores per table
    @pytest.mark.usefixtures("schema_contract_off")
    def test_every_pass_is_kept_in_the_router_output(self, tmp_path) -> None:
        store = LocalArtifactStore(base_dir=str(tmp_path))
        self._seed(store)
        self._run(store, [])

        prefix = f"{self.DB}/{self.JOB}/post-schema-router"
        cumulative = store.read_json(f"{prefix}/router_output.json")
        assert [r["query_id"] for r in cumulative["routings"]] == ["q-1"]
        assert cumulative["routings"][0]["score_after"] == 70
        assert store.read_json(f"{prefix}/pass-0/router_output.json")["routings"]
        assert store.read_json(f"{prefix}/pass-1/router_output.json")["routings"] == []

    @pytest.mark.usefixtures("schema_contract_off")
    def test_a_failed_redesign_rolls_back_to_the_approved_routing(self, tmp_path) -> None:
        """#713 round 2: the effective version never pairs the move with a
        pre-move design. v3 restates v1 and carries its designs."""
        store = LocalArtifactStore(base_dir=str(tmp_path))
        self._seed(store)
        prefix = f"{self.DB}/{self.JOB}"
        orch = LocalOrchestrator(store, llm_mode="bedrock")
        with (
            patch(
                "src.agents.schema_design.handler.run_schema_design_auto",
                side_effect=RuntimeError("designer down"),
            ),
            pytest.raises(RuntimeError, match="designer down"),
        ):
            orch._run_post_schema_routing(self.JOB, self.DB)

        v3 = store.read_json(f"{prefix}/assignment/v3/assignment.json")
        v1 = store.read_json(f"{prefix}/assignment/v1/assignment.json")
        assert v3["query_assignments"] == v1["query_assignments"]
        assert v3["previous_version"] == 2
        # #713 review round 3: its own status, a fresh date, the abandoned version.
        assert v3["status"] == "reroute_rolled_back"
        assert v3["rolled_back_version"] == 2
        v2 = store.read_json(f"{prefix}/assignment/v2/assignment.json")
        assert v3["timestamp"] >= v2["timestamp"]
        for engine in ("dynamodb", "documentdb", "aurora_mysql"):
            assert store.exists(f"{prefix}/schema-{engine}/v3/schema_output.json")


class TestKeptEnginesAreReReadEveryPass:
    @pytest.mark.usefixtures("schema_contract_off")
    def test_an_engine_an_earlier_pass_emptied_is_never_a_destination(self, tmp_path) -> None:
        """#713 review round 3: pass 0 moves DocumentDB's only query away, so on
        pass 1 DocumentDB (best for q-2 at 99) is no longer a kept engine."""
        case = TestTheOrchestratorRoutesAFlagByScore()
        store = LocalArtifactStore(base_dir=str(tmp_path))
        case._seed(store)
        prefix = f"{case.DB}/{case.JOB}"
        scores = {
            "dynamodb": {"t_q1": 90, "t_q2": 40, "t_q3": 60},
            "documentdb": {"t_q1": 55, "t_q2": 99, "t_q3": 30},
            "aurora_mysql": {"t_q1": 70, "t_q2": 20, "t_q3": 80},
        }
        for engine, by_table in scores.items():
            store.write_json(
                f"{prefix}/analysis-{engine}/analysis.json",
                {
                    "table_recommendations": [
                        {"table_id": t, "confidence_score": v} for t, v in by_table.items()
                    ]
                },
            )
        store.write_json(f"{prefix}/schema-dynamodb/v1/design_trace.json", {"trace": []})
        store.write_json(
            f"{prefix}/schema-documentdb/v1/design_trace.json",
            {"trace": [], "routing_flags": [_flag("documentdb") | {"query_ids": ["q-3"]}]},
        )
        flags_on_v2 = {"aurora_mysql": [_flag("aurora_mysql") | {"query_ids": ["q-2"]}]}
        inner = case._fake_design([])

        def design(job_id, database_name, engine, store, assignment_version=0, llm_mode=""):
            inner(job_id, database_name, engine, store, assignment_version, llm_mode)
            if assignment_version == 2 and engine in flags_on_v2:
                store.write_json(
                    f"{database_name}/{job_id}/schema-{engine}/v2/design_trace.json",
                    {"trace": [], "routing_flags": flags_on_v2[engine]},
                )

        orch = LocalOrchestrator(store, llm_mode="bedrock")
        with patch("src.agents.schema_design.handler.run_schema_design_auto", side_effect=design):
            orch._run_post_schema_routing(case.JOB, case.DB)

        router = store.read_json(f"{prefix}/post-schema-router/router_output.json")
        destination = {r["query_id"]: r["to_engine"] for r in router["routings"]}
        # Pass 0: q-3 leaves DocumentDB (its only query) for Aurora MySQL, whose
        # redesign on v2 flags q-2. Pass 1: DocumentDB is no longer kept.
        assert destination["q-3"] == "aurora_mysql"
        assert destination["q-2"] == "dynamodb"
