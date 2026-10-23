"""Data movement plans for the sample jobs (#1413).

``scripts/run_data_movement_plan.py`` runs on a copy of each finished job (wordpress,
discourse, trailgear, and the designed wordpress job) and writes one plan per wave.
Each plan must be the registered contract, follow the job's ``migration_waves`` (same
count and order), move wave 1 with DMS and every later wave with Debezium, give every
table a wave's targets read a hub topic or an unsupported entry, and make the cache
wave's target a ``cache`` replica of the engine it fronts. The plans are the same
bytes under any hash seed, and a job with no cache wave (wordpress scoped to Aurora
MySQL and DynamoDB) plans its waves too.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 -- runs this repo's own scripts/ entry points with fixed argv
import sys
from pathlib import Path

import pytest

from src.contracts.data_movement_plan import DataMovementPlan
from src.shared.contract_versioning import load_contract
from src.shared.migration_wave_engines import CACHE_ENGINES, RELATIONAL_ENGINES
from tests.e2e.pipeline import REPO, PipelineResult, _env, unzip_sample

SCRIPT = "scripts/run_data_movement_plan.py"


def _script(*args: str, seed: str | None = None) -> dict:
    env = _env() if seed is None else {**_env(), "PYTHONHASHSEED": seed}
    proc = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script args
        [sys.executable, *args], cwd=REPO, capture_output=True, text=True, env=env, timeout=900
    )
    assert proc.returncode == 0, f"{args[0]} failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
    payload: dict = json.loads(proc.stdout.strip().splitlines()[-1])
    return payload


def _copy_job(run: PipelineResult, dest: Path) -> Path:
    """A copy of the job's artifact root, so the plans never touch the shared run."""
    root = dest / "artifacts"
    shutil.copytree(run.job_dir(), root / run.db / run.job_id)
    return root


def _plan_job(root: Path, db: str, job: str, seed: str | None = None) -> list[DataMovementPlan]:
    out = _script(SCRIPT, "--job-id", job, "--db", db, "--artifact-root", str(root), seed=seed)
    assert out["status"] == "complete", out
    plans = []
    for rel in out["plans"]:
        payload = json.loads((root / rel).read_text())
        plans.append(load_contract(DataMovementPlan, payload, artifact=rel))
    return plans


def _report(root: Path, db: str, job: str) -> dict:
    reports = sorted((root / db / job / "synthesis").glob("v*/report.json"))
    data: dict = json.loads(reports[-1].read_text())
    return data


def _assignment(root: Path, db: str, job: str, report: dict) -> dict:
    version = report["assignment_summary"]["version"]
    path = root / db / job / "assignment" / f"v{version}" / "assignment.json"
    data: dict = json.loads(path.read_text())
    return data


def _check_plans(plans: list[DataMovementPlan], report: dict, assignment: dict) -> None:
    waves = report["migration_waves"]
    assert [p.wave for p in plans] == [w["wave"] for w in waves]
    assert [p.title for p in plans] == [w["title"] for w in waves]
    assert {p.wave_count for p in plans} == {len(waves)}
    assert {p.assignment_version for p in plans} == {report["assignment_summary"]["version"]}

    wave1, later = plans[0], plans[1:]
    assert wave1.cdc.tool == "dms"
    assert wave1.hub == []
    assert [t.engine for t in wave1.targets] == list(waves[0]["engines"])

    for plan, wave in zip(later, waves[1:], strict=True):
        assert plan.cdc.tool == "debezium"
        assert plan.cdc.connector_class, plan.wave
        read = {t for target in plan.targets for t in target.tables}
        assert read, plan.wave
        covered = {h.table for h in plan.hub} | {
            u.table for u in plan.unsupported if u.kind in {"no_key", "view", "unknown_table"}
        }
        assert read <= covered, (plan.wave, sorted(read - covered))
        assert all(h.key_columns and h.cleanup_policy == "compact" for h in plan.hub)
        cache = [t for t in plan.targets if t.engine in CACHE_ENGINES]
        if cache:
            assert {t.role for t in cache} == {"cache"}
            assert {t.source_of_truth for t in cache} == {wave["fronts"]}
            assert plan.source.engine == wave["fronts"]
        else:
            assert "cache" not in {t.role for t in plan.targets}

    # Every engine's in-scope queries per table, from the assignment the plans read.
    assigned: dict[tuple[str, str], set[str]] = {}
    for qa in assignment["query_assignments"]:
        if qa.get("in_scope", True):
            for t in qa.get("source_tables") or []:
                assigned.setdefault((qa["assigned_engine"], t), set()).add(qa["query_id"])
    for plan, wave in zip(later, waves[1:], strict=True):
        lagging = {(r.table, r.engine): set(r.read_query_ids) for r in plan.gates.lagging_reads}
        reverse = {(u.table, u.engine) for u in plan.unsupported if u.kind == "reverse_replication"}
        for (table, engine), ids in lagging.items():
            if engine not in CACHE_ENGINES:
                assert ids <= assigned[(engine, table)], (plan.wave, table, engine)
        owners = {row["table"]: row["owner"] for row in wave.get("table_owners") or []}
        for target in plan.targets:
            if target.role != "replica":
                continue
            for table in target.tables:
                # The plan's owner is the wave plan's, wherever the wave names one.
                if table in owners and owners[table] not in wave["engines"]:
                    assert target.source_of_truth == owners[table], (plan.wave, table)
                # A replica's reads lag, whether or not anything writes its table.
                if assigned.get((target.engine, table)):
                    assert (table, target.engine) in lagging, (plan.wave, table, target.engine)
                # A replica fed by a non-relational owner has no feed from the hub yet.
                if target.source_of_truth not in RELATIONAL_ENGINES | {plan.source.engine}:
                    assert (table, target.engine) in reverse, (plan.wave, table, target.engine)
    for plan in plans:
        assert plan.monthly_cost_usd is None
        assert any(u.kind == "cost" for u in plan.unsupported)


def test_each_sample_job_gets_one_valid_plan_per_wave(run: PipelineResult, tmp_path: Path) -> None:
    root = _copy_job(run, tmp_path)
    plans = _plan_job(root, run.db, run.job_id)
    report = _report(root, run.db, run.job_id)
    _check_plans(plans, report, _assignment(root, run.db, run.job_id, report))


def test_the_plans_ignore_the_hash_seed(run: PipelineResult, tmp_path: Path) -> None:
    outputs = []
    for seed in ("0", "1", "2", "3"):
        root = _copy_job(run, tmp_path / f"seed-{seed}")
        _plan_job(root, run.db, run.job_id, seed=seed)
        folder = root / run.db / run.job_id / "data-movement"
        outputs.append(
            {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*.json")}
        )
    assert outputs[0]
    assert all(o == outputs[0] for o in outputs[1:])


@pytest.mark.deterministic
def test_the_designed_wordpress_job_points_each_writer_at_its_design(
    designed_run: PipelineResult, tmp_path: Path
) -> None:
    root = _copy_job(designed_run, tmp_path)
    plans = _plan_job(root, designed_run.db, designed_run.job_id)
    report = _report(root, designed_run.db, designed_run.job_id)
    _check_plans(plans, report, _assignment(root, designed_run.db, designed_run.job_id, report))
    later_targets = [t for p in plans[1:] for t in p.targets]
    assert later_targets
    for target in later_targets:
        assert target.transformation is not None, target.engine
        assert target.transformation.artifact.startswith(f"schema-{target.engine}/")
        assert (
            root / designed_run.db / designed_run.job_id / target.transformation.artifact
        ).is_file()
    assert not [u for p in plans for u in p.unsupported if u.kind == "no_schema_design"]


def test_a_wordpress_job_with_no_cache_wave_plans_its_waves(tmp_path: Path) -> None:
    from src.agents.referee.triage_handler import run_triage
    from src.storage.local_store import LocalArtifactStore

    db, job = "wordpress", "nocache"
    root = tmp_path / "artifacts"
    common = ["--job-id", job, "--db", db, "--artifact-root", str(root)]
    _script("scripts/run_collect.py", "--file", str(unzip_sample(db, tmp_path / "input")), *common)
    run_triage(
        job, db, LocalArtifactStore(base_dir=str(root)), engine_scope=["aurora_mysql", "dynamodb"]
    )
    triage = json.loads((root / db / job / "referee-triage" / "triage.json").read_text())
    for selected in triage["selected_agents"]:
        engine = selected["agent_type"]
        _script("scripts/run_analysis.py", *common, "--engine", engine, "--llm-mode", "none")
    _script("scripts/run_assignment.py", *common)
    _script("scripts/run_reality_check.py", *common, "--llm-mode", "none")
    _script("scripts/run_synthesis.py", *common, "--llm-mode", "none")

    report = _report(root, db, job)
    assert not any(set(w["engines"]) & CACHE_ENGINES for w in report["migration_waves"])
    plans = _plan_job(root, db, job)
    assert len(plans) >= 2
    _check_plans(plans, report, _assignment(root, db, job, report))
