"""The recorded PE flag (#740) through the headless path: review, reroute, redesign (#1059).

The same recording ``tests/fixtures/pe_flag/wordpress_dynamodb_pe_review.json``
as the Bedrock replay (#1055), driven through the scripts a headless
``/modernize`` runs, with a test standing in for each subagent:

- DynamoDB on the approved v2: ``run_schema_design.py --split``, one group
  draft per group (the committed wordpress design, one access pattern per
  query of the group), ``run_pe_review.py`` (one request per group), the
  recorded review as every group's answer, ``run_pe_review.py --finalize`` per
  group, then ``--merge``;
- ``run_post_schema_routing.py``: the router and the shared scorer move the
  flagged query to Aurora MySQL as v3 and list the two engines to design again;
  both are designed on v3 the headless way (DynamoDB as above, Aurora MySQL
  with an empty design delta) and the script runs again: nothing else moves.

Then the same flags and designs go through the Bedrock path
(``LocalOrchestrator._run_post_schema_routing``) on a copy of the same v2, and
both modes must reach the same v3 (ADR-031 Decision 11 Amendment 2 in both).
No test calls a model.
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from src.agents.referee.assignment_overrides import mark_assignment_customer_approved
from src.agents.referee.post_schema_routing_handler import PostSchemaRouting
from src.orchestrator.local_orchestrator import LocalOrchestrator
from src.storage.local_store import LocalArtifactStore
from tests.e2e.pipeline import REPO, PipelineResult, _run, run_pipeline
from tests.shared_runs import cached_run, copy_run

DB, JOB = "wordpress", "job-pe-flag-headless"
FIXTURE = REPO / "tests/fixtures/pe_flag/wordpress_dynamodb_pe_review.json"
FLAGGED_PREFIX = "413c99e4"
TEMPLATE = json.loads(
    (
        REPO / "docs/examples/wordpress/load-test-results/schema-dynamodb/v2/schema_output.json"
    ).read_text()
)


def _recorded_review(query_ids: list[str]) -> dict[str, Any]:
    """The recording with each id prefix expanded against ``query_ids``."""
    review: dict[str, Any] = json.loads(FIXTURE.read_text())
    review.pop("_comment", None)
    review["routing_flags"] = [
        {**flag, "query_ids": [q for p in flag["query_ids"] for q in query_ids if q.startswith(p)]}
        for flag in review["routing_flags"]
    ]
    return review


def _source(table: str) -> str:
    return table.rsplit(".", 1)[-1]


def _draft_from(queries: list[dict]) -> dict:
    """A group subagent's draft: one table per source table the group reads (the
    committed design's first table, renamed), one access pattern per query."""
    design: dict = copy.deepcopy(TEMPLATE)
    template = design["table_definitions"][0]
    sources = sorted({_source(t) for q in queries for t in q.get("tables_accessed") or []})
    design["table_definitions"] = [
        {**copy.deepcopy(template), "table_name": f"T_{name}", "source_tables": [name]}
        for name in sources
    ]
    design["hot_partition_analysis"] = []
    design["trade_offs"] = []
    pattern = design["access_patterns"][0]
    design["access_patterns"] = [
        {
            **pattern,
            "pattern_id": f"DDB-AP-{i + 1}",
            "query_ids": [q["query_id"]],
            "source_tables": sorted({_source(t) for t in q.get("tables_accessed") or []}),
            "table_name": f"T_{_source((q.get('tables_accessed') or sources)[0])}",
        }
        for i, q in enumerate(queries)
    ]
    return design


def _write(path: Path, data: dict) -> None:
    """A subagent's Write tool call (it creates the parent directory)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


class Headless:
    """The scripts a headless run calls, with the test as every subagent."""

    def __init__(self, root: Path, review: dict) -> None:
        self.root = root
        self.review = review
        self.result = PipelineResult(db=DB, job_id=JOB, artifact_root=root)
        self.common = ["--job-id", JOB, "--db", DB, "--artifact-root", str(root)]
        self.lines: list[tuple[str, dict]] = []

    def run(self, step: str, script: str, *args: str) -> dict:
        line = _run(step, [f"scripts/{script}", *self.common, *args], self.result)
        self.lines.append((step, line))
        return line

    def design_dynamodb(self, review: bool = True) -> None:
        split = self.run("split", "run_schema_design.py", "--engine", "dynamodb", "--split")
        for group in split["groups"]:
            group_input = json.loads(Path(group["input_file"]).read_text())
            queries = group_input["collector_output"]["queries"]["query_patterns"]
            _write(Path(group["draft"]), _draft_from(queries))
        if review:
            self.review_all()
        self.merge()

    def merge(self) -> None:
        merged = self.run("merge", "run_schema_design.py", "--engine", "dynamodb", "--merge")
        assert merged["status"] == "complete", merged

    def design_aurora_mysql(self, engine: str = "aurora_mysql") -> None:
        request = self.run("aurora", "run_schema_design.py", "--engine", engine)
        assert request["status"] == "awaiting_llm", request
        response = self.root / DB / JOB / "llm_responses" / f"schema_design_{engine}.json"
        _write(response, {"delta_version": "1.0"})
        done = self.run("aurora", "run_schema_design.py", "--engine", engine, "--finalize")
        assert done["status"] == "complete", done

    def redesign(self, engines: list[str]) -> None:
        """6e: design every listed engine again, then review what has no review."""
        for engine in engines:
            if engine == "dynamodb":
                self.design_dynamodb()
            elif engine.startswith("aurora_"):
                self.design_aurora_mysql(engine)
            else:
                raise AssertionError(f"no stand-in designer for {engine}")
        self.review_all()

    def review_all(self) -> dict:
        """``run_pe_review.py``, the recording as every answer, one finalize each."""
        prepared = self.run("pe-review", "run_pe_review.py")
        for review in prepared["reviews"]:
            _write(Path(review["response"]), self.review)
            group = [] if review["group"] is None else ["--group", str(review["group"])]
            done = self.run(
                "pe-review-finalize",
                "run_pe_review.py",
                "--engine",
                review["engine"],
                *group,
                "--finalize",
            )
            assert done["status"] == "complete", done
        return prepared

    def route(self) -> dict:
        return self.run("route", "run_post_schema_routing.py")


def _approved_copy(tmp_path_factory, name: str) -> Path:
    cached = cached_run(
        tmp_path_factory,
        f"pipeline-wordpress-{JOB}",
        lambda d: run_pipeline("wordpress", d / "artifacts", JOB),
    )
    root = copy_run(cached, tmp_path_factory.mktemp(name) / "run") / "artifacts"
    mark_assignment_customer_approved(LocalArtifactStore(base_dir=str(root)), DB, JOB)
    return root


def _engine_of(assignment: dict) -> dict[str, str]:
    return {qa["query_id"]: qa["assigned_engine"] for qa in assignment["query_assignments"]}


def _comparable(assignment: dict) -> dict:
    """An assignment without the fields that differ by run time."""
    out = {k: v for k, v in assignment.items() if k not in ("timestamp",)}
    out["moves"] = [{k: v for k, v in m.items() if k != "at"} for m in out.get("moves") or []]
    return out


@pytest.fixture(scope="module")
def headless(tmp_path_factory) -> dict:
    root = _approved_copy(tmp_path_factory, "pe-flag-headless")
    store = LocalArtifactStore(base_dir=str(root))
    prefix = f"{DB}/{JOB}"
    v2 = store.read_json(f"{prefix}/assignment/v2/assignment.json")
    review = _recorded_review([qa["query_id"] for qa in v2["query_assignments"]])
    run = Headless(root, review)

    run.design_dynamodb()
    trace_v2 = store.read_json(f"{prefix}/schema-dynamodb/v2/design_trace.json")
    # The Bedrock comparison starts from this same v2: designs and review flags.
    v2_root = tmp_path_factory.mktemp("pe-flag-v2") / "artifacts"
    shutil.copytree(root, v2_root)

    first = run.route()
    early = run.route()  # before any redesign: no pass may run
    has_pass_1_early = store.exists(f"{prefix}/post-schema-router/pass-1/router_output.json")
    run.design_dynamodb(review=False)
    run.design_aurora_mysql()
    unreviewed = run.route()  # designed, not reviewed: still no pass
    has_pass_1_unreviewed = store.exists(f"{prefix}/post-schema-router/pass-1/router_output.json")
    run.review_all()
    second_reviews = [line for step, line in run.lines if step == "pe-review"][-1]
    run.merge()  # the reviews landed after the merge: merge again to consolidate them
    second = run.route()
    again = run.route()

    common = ["--job-id", JOB, "--db", DB, "--artifact-root", str(root)]
    _run("synthesis", ["scripts/run_synthesis.py", *common, "--llm-mode", "none"], run.result)
    _run("report", ["scripts/run_report.py", *common], run.result)
    [full_id] = [q for q in _engine_of(v2) if q.startswith(FLAGGED_PREFIX)]
    return {
        "root": root,
        "v2_root": v2_root,
        "id": full_id,
        "v2": v2,
        "lines": run.lines,
        "trace_v2": trace_v2,
        "first": first,
        "early": early,
        "has_pass_1_early": has_pass_1_early,
        "unreviewed": unreviewed,
        "has_pass_1_unreviewed": has_pass_1_unreviewed,
        "trace_v3": store.read_json(f"{prefix}/schema-dynamodb/v3/design_trace.json"),
        # The run's own group counts (the split decides them, not this test).
        "groups_v2": len(
            store.read_json(f"{prefix}/schema-dynamodb/v2/groups_manifest.json")["groups"]
        ),
        "groups_v3": len(
            store.read_json(f"{prefix}/schema-dynamodb/v3/groups_manifest.json")["groups"]
        ),
        "second_reviews": second_reviews,
        "second": second,
        "again": again,
        "router": store.read_json(f"{prefix}/post-schema-router/router_output.json"),
        "v3": store.read_json(f"{prefix}/assignment/v3/assignment.json"),
        "has_v4": store.exists(f"{prefix}/assignment/v4/assignment.json"),
        "design_v3": store.read_json(f"{prefix}/schema-dynamodb/v3/schema_output.json"),
        "report": json.loads((root / DB / JOB / "synthesis" / "v3" / "report.json").read_text()),
        "engineering": (root / DB / JOB / "deliverables" / "engineering-report.md").read_text(),
    }


@pytest.fixture(scope="module")
def bedrock(headless, tmp_path_factory) -> dict:
    """The same recorded flags and designs through the Bedrock path."""
    headless_store = LocalArtifactStore(base_dir=str(headless["root"]))
    root = tmp_path_factory.mktemp("pe-flag-bedrock") / "artifacts"
    shutil.copytree(headless["v2_root"], root)
    store = LocalArtifactStore(base_dir=str(root))
    calls: list[tuple[str, int]] = []

    def redesign(job_id, database_name, engine, store_, assignment_version=0, llm_mode=""):
        """Bedrock's redesign, replayed: the headless run's design of that version."""
        calls.append((engine, assignment_version))
        src = f"{database_name}/{job_id}/schema-{engine}/v{assignment_version}/"
        for key in headless_store.list_prefix(src):
            store_.write_json(key, headless_store.read_json(key))

    with patch("src.agents.schema_design.handler.run_schema_design_auto", side_effect=redesign):
        LocalOrchestrator(store, llm_mode="bedrock")._run_post_schema_routing(JOB, DB)
    prefix = f"{DB}/{JOB}"
    return {
        "calls": calls,
        "v3": store.read_json(f"{prefix}/assignment/v3/assignment.json"),
        "has_v4": store.exists(f"{prefix}/assignment/v4/assignment.json"),
        "router": store.read_json(f"{prefix}/post-schema-router/router_output.json"),
    }


@pytest.fixture(scope="module")
def rolled(headless, tmp_path_factory) -> dict:
    """A failed redesign in both modes, from the same v2."""
    prefix = f"{DB}/{JOB}"
    out: dict = {}
    for mode in ("external", "bedrock"):
        root = tmp_path_factory.mktemp(f"rollback-{mode}") / "artifacts"
        shutil.copytree(headless["v2_root"], root)
        store = LocalArtifactStore(base_dir=str(root))
        run = Headless(root, {})
        if mode == "external":
            run.route()
            out["rollback_line"] = run.run("rollback", "run_post_schema_routing.py", "--rollback")
            out["after"] = run.route()
        else:
            with (
                patch(
                    "src.agents.schema_design.handler.run_schema_design_auto",
                    side_effect=RuntimeError("redesign failed"),
                ),
                pytest.raises(RuntimeError),
            ):
                LocalOrchestrator(store, llm_mode="bedrock")._run_post_schema_routing(JOB, DB)
        out[mode] = store.read_json(f"{prefix}/assignment/v4/assignment.json")
        out[f"{mode}_designs"] = sorted(store.list_prefix(f"{prefix}/schema-dynamodb/v4/"))
    return out


class TestTheReviewIsAnExternalStep:
    def test_one_request_per_dynamodb_group_and_aurora_is_skipped(self, headless) -> None:
        [first_prepare] = [line for step, line in headless["lines"] if step == "pe-review"][:1]
        groups = [r["group"] for r in first_prepare["reviews"]]
        assert first_prepare["status"] == "awaiting_llm"
        assert sorted(groups) == list(range(headless["groups_v2"]))
        assert headless["groups_v2"] > 1  # wordpress has more DynamoDB queries than one group holds
        assert {r["engine"] for r in first_prepare["reviews"]} == {"dynamodb"}
        for r in first_prepare["reviews"]:
            assert r["skill_prompt"] == "src/skills/dynamodb-pe-review.md"
            assert r["finalize"].startswith("uv run python scripts/run_pe_review.py ")
            assert r["finalize"].endswith(f"--engine dynamodb --group {r['group']} --finalize")
            request = json.loads(Path(r["request"]).read_text())
            assert request["output_schema"]["title"] == "PEReviewResult"
            assert request["query_ids"] and request["design"]["access_patterns"]

    def test_every_groups_review_reaches_the_merged_trace(self, headless) -> None:
        """Where the Bedrock path records it: the router reads it from here."""
        from src.agents.referee.post_schema_router import routing_flags_from_trace

        n = headless["groups_v2"]
        assert len(headless["trace_v2"]["groups"]) == n
        flags = routing_flags_from_trace(headless["trace_v2"])
        assert [(f["query_ids"], f["reason_category"]) for f in flags] == [
            ([headless["id"]], "full_scan")
        ] * n

    def test_the_redesign_is_reviewed_and_aurora_mysql_is_skipped(self, headless) -> None:
        reviews = headless["second_reviews"]
        assert [r["engine"] for r in reviews["reviews"]] == ["dynamodb"] * len(reviews["reviews"])
        assert all(r["response"].count("/llm_responses/") == 1 for r in reviews["reviews"])


class TestTheRerouteRunsTheSharedRouter:
    def test_the_first_call_writes_v3_and_names_both_touched_engines(self, headless) -> None:
        first = headless["first"]
        assert first["status"] == "awaiting_redesign"
        assert (first["previous_version"], first["assignment_version"]) == (2, 3)
        assert first["redesign_engines"] == ["aurora_mysql", "dynamodb"]
        assert first["missing_designs"] == ["aurora_mysql", "dynamodb"]
        assert (first["pass"], first["pass_rerouted"], first["rerouted"]) == (0, 1, 1)

    def test_no_pass_runs_before_the_redesign(self, headless) -> None:
        early = headless["early"]
        assert early["status"] == "awaiting_redesign" and early["assignment_version"] == 3
        assert early["missing_designs"] == ["aurora_mysql", "dynamodb"]
        assert not headless["has_pass_1_early"]

    def test_no_pass_runs_before_the_redesigns_are_reviewed(self, headless) -> None:
        """The redesigned DynamoDB groups are designed but not reviewed: wait."""
        line = headless["unreviewed"]
        assert line["status"] == "awaiting_redesign" and line["missing_designs"] == []
        assert line["missing_reviews"] == [
            f"dynamodb group {g}" for g in range(headless["groups_v3"])
        ]
        assert not headless["has_pass_1_unreviewed"]
        # Reviewed after the merge and merged again: the trace has them.
        assert len(headless["trace_v3"]["groups"]) == headless["groups_v3"]

    def test_the_move_is_a_post_approval_flag_move_on_a_new_version(self, headless) -> None:
        v3, v2 = headless["v3"], headless["v2"]
        assert v3["status"] == "rerouted_after_approval" and v3["previous_version"] == 2
        assert v3["source"] == "post_schema_routing"
        before, after = _engine_of(v2), _engine_of(v3)
        assert {q for q, e in after.items() if before[q] != e} == {headless["id"]}
        assert after[headless["id"]] == "aurora_mysql"
        [move] = v3["moves"]
        assert (move["query_id"], move["pass"], move["reason_code"], move["actor"]) == (
            headless["id"],
            "post_schema_flag",
            "flag.capability_gap",
            "model_flag",
        )
        [reroute] = v3["post_schema_reroutes"]
        assert reroute["approved_version"] == 2
        [routing] = headless["router"]["routings"]
        ladder = {c["engine"]: c for c in routing["evidence"]["candidates"]}
        assert ladder["dynamodb"]["gate"] == "model_flag"
        # DocumentDB outscores Aurora MySQL but the reality check removed it.
        assert ladder["documentdb"]["score"] > ladder["aurora_mysql"]["score"]
        assert routing["score_after"] == ladder["aurora_mysql"]["score"]

    def test_after_the_redesign_nothing_else_moves(self, headless) -> None:
        for line in (headless["second"], headless["again"]):
            assert line["status"] == "complete" and line["assignment_version"] == 3
            assert (line["rerouted"], line["terminal_queries"]) == (1, 0)
        assert not headless["has_v4"]
        designed = {q for ap in headless["design_v3"]["access_patterns"] for q in ap["query_ids"]}
        assert headless["id"] not in designed

    def test_every_line_is_compact_json(self, headless) -> None:
        """Headless stdout: one status line per call, the router's prints in the log."""
        for step, line in headless["lines"]:
            if step == "route":
                assert set(line) >= {"status", "assignment_version", "log"}
                assert Path(line["log"]).name == "run_post_schema_routing.log"

    def test_the_deliverables_say_it_moved_after_approval(self, headless) -> None:
        [reroute] = headless["report"]["post_schema_reroutes"]
        assert (reroute["query_id"], reroute["to_engine"]) == (headless["id"], "aurora_mysql")
        assert "## Moved after approval (1 query)" in headless["engineering"]


class TestBothModesReachTheSameAssignment:
    """The same recorded flags and designs, through the Bedrock path."""

    def test_the_same_v3(self, headless, bedrock) -> None:
        assert _comparable(bedrock["v3"]) == _comparable(headless["v3"])
        assert not bedrock["has_v4"] and not headless["has_v4"]

    def test_the_same_router_output(self, headless, bedrock) -> None:
        assert bedrock["router"] == headless["router"]

    def test_the_same_engines_designed_again(self, headless, bedrock) -> None:
        assert sorted(bedrock["calls"]) == [("aurora_mysql", 3), ("dynamodb", 3)]
        assert sorted(e for e, _ in bedrock["calls"]) == headless["first"]["redesign_engines"]


class TestRollback:
    """A redesign that fails: the same rollback version as the Bedrock path."""

    def test_v4_restates_v2_in_both_modes(self, rolled, headless) -> None:
        assert rolled["external"]["status"] == "reroute_rolled_back"
        assert _engine_of(rolled["external"]) == _engine_of(headless["v2"])
        assert _comparable(rolled["external"]) == _comparable(rolled["bedrock"])
        names = [Path(k).name for k in rolled["external_designs"]]
        assert names == [Path(k).name for k in rolled["bedrock_designs"]] and names

    def test_the_rollback_line_and_no_new_pass_after_it(self, rolled) -> None:
        line = rolled["rollback_line"]
        assert line["status"] == "rolled_back"
        assert (line["assignment_version"], line["restates"], line["rolled_back_version"]) == (
            4,
            2,
            3,
        )
        assert rolled["after"]["status"] == "rolled_back"


def test_with_no_design_or_no_flag_nothing_moves(tmp_path_factory) -> None:
    """No schema output yet, then a design whose review flags nothing: no new version.

    The first call (too early: nothing designed) does not block the next one."""
    root = _approved_copy(tmp_path_factory, "pe-flag-none")
    store = LocalArtifactStore(base_dir=str(root))
    run = Headless(root, {})
    line = run.route()
    assert (line["status"], line["assignment_version"], line["rerouted"]) == ("complete", 2, 0)
    v2 = store.read_json(f"{DB}/{JOB}/assignment/v2/assignment.json")
    run.review = _recorded_review([]) | {"routing_flags": []}
    run.design_dynamodb()
    line = run.route()
    assert (line["status"], line["assignment_version"], line["rerouted"]) == ("complete", 2, 0)
    assert not store.exists(f"{DB}/{JOB}/assignment/v3/assignment.json")
    assert store.read_json(f"{DB}/{JOB}/assignment/v2/assignment.json") == v2


def _flag_review(query_ids: list[str]) -> dict:
    """The recording, flagging ``query_ids`` (full ids) on DynamoDB instead."""
    review = _recorded_review([])
    [flag] = review["routing_flags"]
    review["routing_flags"] = [{**flag, "query_ids": list(query_ids)}] if query_ids else []
    return review


@pytest.fixture(scope="module")
def cascade(headless, tmp_path_factory) -> dict:
    """Three rounds: each round's DynamoDB reviews flag a different query.

    Round 0 (v2) flags the #740 query, round 1 (v3) a second DynamoDB query,
    round 2 (v4) a third. The second flag moves on pass 1 (v4); the third is
    reached on pass 2, the cascade bound, so it is terminal and no v5 follows.
    The same flags and designs then go through the Bedrock path.
    """
    prefix = f"{DB}/{JOB}"
    v2 = headless["v2"]
    on_dynamodb = sorted(q for q, e in _engine_of(v2).items() if e == "dynamodb")
    grouped = {m for g in v2.get("co_dependency_groups") or [] if len(g) > 1 for m in g}
    singles = [q for q in on_dynamodb if q != headless["id"] and q not in grouped]
    second, third = singles[0], singles[1]
    rounds = [[headless["id"]], [second], [third]]

    root = tmp_path_factory.mktemp("cascade-headless") / "artifacts"
    shutil.copytree(headless["v2_root"], root)
    store = LocalArtifactStore(base_dir=str(root))
    run = Headless(root, _flag_review(rounds[0]))
    lines = []
    for flags in rounds[1:] + [[]]:
        line = run.route()
        lines.append(line)
        if line["status"] != "awaiting_redesign":
            break
        run.review = _flag_review(flags)
        run.redesign(line["missing_designs"])
    else:
        lines.append(run.route())

    bedrock_root = tmp_path_factory.mktemp("cascade-bedrock") / "artifacts"
    shutil.copytree(headless["v2_root"], bedrock_root)
    bedrock_store = LocalArtifactStore(base_dir=str(bedrock_root))
    calls: list[tuple[str, int]] = []

    def redesign(job_id, database_name, engine, store_, assignment_version=0, llm_mode=""):
        calls.append((engine, assignment_version))
        src = f"{database_name}/{job_id}/schema-{engine}/v{assignment_version}/"
        for key in store.list_prefix(src):
            store_.write_json(key, store.read_json(key))

    with patch("src.agents.schema_design.handler.run_schema_design_auto", side_effect=redesign):
        LocalOrchestrator(bedrock_store, llm_mode="bedrock")._run_post_schema_routing(JOB, DB)

    def versions(s: LocalArtifactStore) -> dict[int, dict]:
        return {
            n: _comparable(s.read_json(f"{prefix}/assignment/v{n}/assignment.json"))
            for n in range(1, 8)
            if s.exists(f"{prefix}/assignment/v{n}/assignment.json")
        }

    def passes(s: LocalArtifactStore) -> dict[int, dict]:
        return {
            n: s.read_json(f"{prefix}/post-schema-router/pass-{n}/router_output.json")
            for n in range(4)
            if s.exists(f"{prefix}/post-schema-router/pass-{n}/router_output.json")
        }

    return {
        "rounds": rounds,
        "lines": lines,
        "calls": calls,
        "headless_versions": versions(store),
        "bedrock_versions": versions(bedrock_store),
        "headless_passes": passes(store),
        "bedrock_passes": passes(bedrock_store),
        "headless_router": store.read_json(f"{prefix}/post-schema-router/router_output.json"),
        "bedrock_router": bedrock_store.read_json(
            f"{prefix}/post-schema-router/router_output.json"
        ),
    }


class TestTwoMovingPassesAndTheCascadeBound:
    """Persisted run state across passes matches the Bedrock loop's."""

    def test_two_passes_move_and_the_third_marks_terminal(self, cascade) -> None:
        statuses = [line["status"] for line in cascade["lines"]]
        assert statuses == ["awaiting_redesign", "awaiting_redesign", "complete"]
        assert [line["assignment_version"] for line in cascade["lines"]] == [3, 4, 4]
        passes = cascade["headless_passes"]
        assert sorted(passes) == [0, 1, 2]
        assert [r["query_id"] for r in passes[0]["routings"]] == cascade["rounds"][0]
        assert [r["query_id"] for r in passes[1]["routings"]] == cascade["rounds"][1]
        # Pass 2 is the cascade bound: the third flag is terminal, never moved.
        assert passes[2]["routings"] == []
        assert passes[2]["terminal_queries"] == cascade["rounds"][2]
        assert cascade["lines"][-1]["terminal_queries"] == 1
        assert 5 not in cascade["headless_versions"]

    def test_bedrock_writes_the_same_versions_and_passes(self, cascade) -> None:
        assert cascade["bedrock_versions"] == cascade["headless_versions"]
        assert cascade["bedrock_passes"] == cascade["headless_passes"]
        assert cascade["bedrock_router"] == cascade["headless_router"]

    def test_bedrock_redesigns_what_the_headless_run_was_told_to(self, cascade) -> None:
        told = [
            (engine, line["assignment_version"])
            for line in cascade["lines"]
            if line["status"] == "awaiting_redesign"
            for engine in line["redesign_engines"]
        ]
        assert sorted(cascade["calls"]) == sorted(told)


def test_a_failed_carry_forward_is_saved_as_rolled_back(headless, tmp_path_factory) -> None:
    """A retry after the copy failed finds the rollback, never a fresh reroute."""
    root = tmp_path_factory.mktemp("carry-forward") / "artifacts"
    shutil.copytree(headless["v2_root"], root)
    store = LocalArtifactStore(base_dir=str(root))
    orch = LocalOrchestrator(store, llm_mode="external")
    with (
        patch.object(PostSchemaRouting, "_carry_forward_designs", side_effect=OSError("disk")),
        pytest.raises(OSError),
    ):
        orch.run_post_schema_routing_external(JOB, DB)
    again = orch.run_post_schema_routing_external(JOB, DB)
    assert (again["status"], again["assignment_version"], again["restates"]) == (
        "rolled_back",
        4,
        2,
    )
    assert store.read_json(f"{DB}/{JOB}/assignment/v4/assignment.json")["status"] == (
        "reroute_rolled_back"
    )
    assert not store.exists(f"{DB}/{JOB}/assignment/v5/assignment.json")
