"""The pipeline finishes after a customer pick on every sample (#859).

A customer edit at the assignment gate is a normal flow: the customer pins
queries, the reality check re-runs on that version, then synthesis and the
reports. On a copy of each sample's deterministic job, pin the first query
(by id) of every engine the first assignment used onto that engine, then run
the rest of the pipeline. Pinning every first-version engine keeps engines the
reality check would otherwise remove, so the routed workloads (and their
evidence labels) differ from the plain run's.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 -- runs this repo's own scripts/ entry points with fixed argv
import sys
from pathlib import Path

import pytest

from tests.e2e.pipeline import REPO, PipelineResult, _env

JOB = "pick"


def _script(*args: str) -> dict:
    proc = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script args
        [sys.executable, *args], cwd=REPO, capture_output=True, text=True, env=_env(), timeout=600
    )
    assert proc.returncode == 0, f"{args[0]} failed:\n{proc.stderr[-3000:]}"
    payload: dict = json.loads(proc.stdout.strip().splitlines()[-1])
    return payload


def _first_query_per_engine(assignment: dict) -> dict[str, str]:
    """The first in-scope query id of every engine that owns a query, by id."""
    picks: dict[str, str] = {}
    for qa in sorted(assignment["query_assignments"], key=lambda q: str(q["query_id"])):
        if qa.get("in_scope", True) and qa.get("assigned_engine"):
            picks.setdefault(qa["assigned_engine"], str(qa["query_id"]))
    return picks


@pytest.fixture(scope="module")
def picked(run: PipelineResult, tmp_path_factory: pytest.TempPathFactory) -> dict:
    from src.agents.referee.assignment_overrides import (
        QueryOverrideInput,
        apply_assignment_overrides,
    )
    from src.storage.local_store import LocalArtifactStore

    if run.external:
        pytest.skip("needs the deterministic job's first assignment version")
    root = Path(tmp_path_factory.mktemp(f"pick-{run.db}")) / "artifacts"
    shutil.copytree(run.job_dir(), root / run.db / JOB)
    first = json.loads((root / run.db / JOB / "assignment" / "v1" / "assignment.json").read_text())
    picks = _first_query_per_engine(first)
    result = apply_assignment_overrides(
        LocalArtifactStore(base_dir=str(root)),
        run.db,
        JOB,
        [QueryOverrideInput(query_id=q, assigned_engine=e) for e, q in sorted(picks.items())],
    )
    common = ["--job-id", JOB, "--db", run.db, "--artifact-root", str(root)]
    _script("scripts/run_reality_check.py", *common, "--llm-mode", "none")
    _script("scripts/run_synthesis.py", *common, "--llm-mode", "none")
    report_step = _script("scripts/run_report.py", *common)
    return {
        "picks": picks,
        "customer_version": result.assignment.version,
        "report": json.loads((root / report_step["report"]).read_text()),
        "deliverables": report_step.get("deliverables", []),
    }


@pytest.mark.deterministic
def test_synthesis_and_reports_finish_after_a_customer_pick(picked: dict) -> None:
    report = picked["report"]
    assert report["assignment_summary"]["version"] >= picked["customer_version"]
    assert any(p.endswith("/executive-report.pdf") for p in picked["deliverables"])
    # Every pinned engine stays in the plan: a pin is the customer's choice.
    owners = {r["target"] for r in report["ranking"] if r.get("role") != "cache_layer"}
    assert set(picked["picks"]) <= owners


# #1151: a customer pick that moves a table's writes off DynamoDB takes the key-scoped
# COUNT the waiver put there with it (ADR-031 Decision 2 Amendment 12). Per sample, the
# pick (a write of the counted table), the counts on DynamoDB that must follow it, and
# the engine they land on; named by id, so the test does not judge with the rule itself.
# INSERT INTO wp_comments ...; the count: COUNT(*) FROM wp_comments WHERE
# comment_post_ID = ? AND comment_approved = ?
WP_WRITE = (
    "de9a7ba3598eaa8b41839c2fce3889985e5636fc7489653be3b59378bf4779da"  # pragma: allowlist secret
)
WP_COUNT = (
    "8c66eaee9125d49703cd068cdba72c305ac38bcbb8e1dab7f399e224cb40a7d6"  # pragma: allowlist secret
)
COUNT_FOLLOWS_WRITES: dict[str, tuple[str, list[str], str] | None] = {
    "wordpress": (WP_WRITE, [WP_COUNT], "aurora_mysql"),
    "discourse": (
        # A write of user_actions; the two COUNT(*) FROM user_actions WHERE action_type = $1
        "-7300874177885411526",
        ["-271909517620911052", "-888320679945847940"],
        "aurora_postgresql",
    ),
    # trailgear places no COUNT on DynamoDB.
    "trailgear": None,
}


@pytest.mark.deterministic
def test_a_pick_moving_a_tables_writes_takes_its_waived_counts(
    run: PipelineResult, tmp_path_factory: pytest.TempPathFactory
) -> None:
    from src.agents.referee.assignment_overrides import (
        QueryOverrideInput,
        apply_assignment_overrides,
    )
    from src.storage.assignment_versioning import resolve_effective_assignment_version
    from src.storage.local_store import LocalArtifactStore

    root = Path(tmp_path_factory.mktemp(f"count-{run.db}")) / "artifacts"
    shutil.copytree(run.job_dir(), root / run.db / JOB)
    store = LocalArtifactStore(base_dir=str(root))
    version = resolve_effective_assignment_version(store, run.db, JOB)
    before = store.read_json(f"{run.db}/{JOB}/assignment/v{version}/assignment.json")
    engine = {qa["query_id"]: qa["assigned_engine"] for qa in before["query_assignments"]}
    expected = COUNT_FOLLOWS_WRITES[run.db]
    if expected is None:
        queries = json.loads((run.job_dir() / "collector" / "output.json").read_text())
        on_ddb = [
            q["query_id"]
            for q in queries["queries"]["query_patterns"]
            if engine.get(q["query_id"]) == "dynamodb"
            and "count(" in q.get("query_text", "").lower()
        ]
        assert on_ddb == [], run.db
        return
    write, counts, target = expected
    assert engine[write] == "dynamodb" and all(engine[c] == "dynamodb" for c in counts)
    result = apply_assignment_overrides(
        store, run.db, JOB, [QueryOverrideInput(query_id=write, assigned_engine=target)]
    )
    after = {qa.query_id: qa.assigned_engine for qa in result.assignment.query_assignments}
    assert {c: after[c] for c in counts} == {c: target for c in counts}
    assert sorted(result.replaced_count_query_ids) == sorted(counts)
    count_moves = [m for m in result.assignment.moves if m.query_id in counts]
    assert [(m.pass_.value, m.reason_code.value, m.actor.value) for m in count_moves] == [
        ("customer_group_propagation", "gate.key_scoped_count", "customer")
    ] * len(counts)
