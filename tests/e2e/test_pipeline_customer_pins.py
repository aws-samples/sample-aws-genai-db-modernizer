"""A customer pin keeps its engine, but never pulls a query onto it for the pin's sake (#860).

The first reality check removes OpenSearch on discourse and trailgear and moves
its queries off. The customer then pins one of them back on OpenSearch and the
check runs again on that version. The pin keeps OpenSearch in the plan and
pays its standing cost (ADR-031 Decision 7), so a query leaves it or joins it
only by score -- but a query with no real search depth (the per-query #326
check) never joins it: discourse's ``SELECT to_tsvector($1, $2)`` and
trailgear's ``LIKE '%' + @Term + '%'`` stay where the first check put them.
Whatever the check writes replays exactly from the customer's version plus
its own Moves.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 -- runs this repo's own scripts/ entry points with fixed argv
import sys
from pathlib import Path

import pytest

from tests.e2e.pipeline import REPO, PipelineResult, _env

JOB = "pins"


def _script(*args: str) -> dict:
    proc = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script args
        [sys.executable, *args], cwd=REPO, capture_output=True, text=True, env=_env(), timeout=600
    )
    assert proc.returncode == 0, f"{args[0]} failed:\n{proc.stderr[-3000:]}"
    payload: dict = json.loads(proc.stdout.strip().splitlines()[-1])
    return payload


def _assignment(job: Path, version: int) -> dict:
    data: dict = json.loads((job / "assignment" / f"v{version}" / "assignment.json").read_text())
    return data


def _pin_search_back(run: PipelineResult, root: Path) -> dict:
    from src.agents.referee.assignment_overrides import (
        QueryOverrideInput,
        apply_assignment_overrides,
    )
    from src.storage.local_store import LocalArtifactStore

    job = root / run.db / JOB
    shutil.copytree(run.job_dir(), job)
    search = sorted(
        str(qa["query_id"])
        for qa in _assignment(job, 1)["query_assignments"]
        if qa["assigned_engine"] == "opensearch"
    )
    assert len(search) >= 2, "the first assignment puts at least two queries on OpenSearch"
    pin, others = search[-1], search[:-1]
    result = apply_assignment_overrides(
        LocalArtifactStore(base_dir=str(root)),
        run.db,
        JOB,
        [QueryOverrideInput(query_id=pin, assigned_engine="opensearch")],
    )
    common = ["--job-id", JOB, "--db", run.db, "--artifact-root", str(root)]
    # Re-run the reality check on the customer's version, as the issue's flow does.
    version = result.assignment.version
    step = _script(
        "scripts/run_reality_check.py",
        *common,
        "--llm-mode",
        "none",
        "--assignment-version",
        str(version),
    )
    collector = json.loads((job / "collector" / "output.json").read_text())
    texts = {
        str(q["query_id"]): q.get("query_text") for q in collector["queries"]["query_patterns"]
    }
    return {
        "job": job,
        "pin": pin,
        "others": others,
        "texts": texts,
        "customer_version": version,
        "final_version": step["output_version"] or version,
        "step": step,
    }


@pytest.fixture(scope="module")
def pinned(
    discourse_run: PipelineResult,
    trailgear_run: PipelineResult,
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, dict]:
    return {
        run.db: _pin_search_back(run, Path(tmp_path_factory.mktemp(f"pins-{run.db}")) / "artifacts")
        for run in (discourse_run, trailgear_run)
    }


@pytest.mark.parametrize("sample", ["discourse", "trailgear"])
def test_the_pin_keeps_opensearch_and_shallow_queries_stay_off(
    pinned: dict[str, dict], sample: str
) -> None:
    from src.agents.referee.reality_check_floors import _query_has_search_depth

    case = pinned[sample]
    job = case["job"]
    assert case["step"]["input_version"] == case["customer_version"]
    before = {
        str(qa["query_id"]): qa
        for qa in _assignment(job, case["customer_version"])["query_assignments"]
    }
    final = {
        str(qa["query_id"]): qa
        for qa in _assignment(job, case["final_version"])["query_assignments"]
    }
    assert final[case["pin"]]["assigned_engine"] == "opensearch"
    for qid in case["others"]:
        if _query_has_search_depth(case["texts"].get(qid)):
            continue
        assert final[qid]["assigned_engine"] != "opensearch", qid
        assert final[qid]["assigned_engine"] == before[qid]["assigned_engine"], qid

    output = json.loads((job / "reality-check" / "output.json").read_text())
    assert "opensearch" not in output["eliminated_engines"]
    assert output["after_distribution"].get("opensearch")


@pytest.mark.parametrize("sample", ["discourse", "trailgear"])
def test_the_check_replays_from_the_customer_version(pinned: dict[str, dict], sample: str) -> None:
    from src.agents.referee.move_ledger import check_replay, state_of
    from src.agents.referee.reality_check_floors import _query_has_search_depth
    from src.contracts.move_ledger import Move

    case = pinned[sample]
    job = case["job"]
    before = _assignment(job, case["customer_version"])["query_assignments"]
    after = _assignment(job, case["final_version"])
    moves = (
        [Move.model_validate(m) for m in after.get("moves") or []]
        if case["step"]["output_version"]
        else []
    )
    check_replay(moves, state_of(before), state_of(after["query_assignments"]), "test")
    # Any Move onto OpenSearch is a query with real search depth, at a higher score.
    for m in moves:
        if m.to_engine == "opensearch":
            assert _query_has_search_depth(case["texts"].get(m.query_id)), m.query_id
            assert m.score_after is not None and m.score_before is not None
            assert m.score_after > m.score_before
