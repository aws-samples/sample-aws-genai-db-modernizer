# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Data movement plans for the sample jobs (#1413, #1797).

``scripts/run_data_movement_plan.py`` runs on a copy of each finished job (wordpress,
discourse, trailgear, and the designed wordpress job) and writes one plan per step plus
the job's hub plan.
Each plan must be the registered contract, follow the job's steps (``facts.slice_plan``'s
``plan_steps``, each naming its phase wave, or the ``migration_waves`` without a slice plan;
#1698), move step 1 with DMS and every later step with Debezium, give every
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

from src.contracts.data_movement_plan import DataMovementPlan, HubPlan
from src.shared.catalog.prices import HOURS_PER_MONTH, price_table
from src.shared.contract_versioning import load_contract
from src.shared.engine_names import SOURCE_DATABASE, display_engine
from src.shared.migration_wave_engines import CACHE_ENGINES, RELATIONAL_ENGINES
from src.shared.placeholder_tables import saved_write_groups
from src.shared.report_facts import wave_retained_engine
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


def _plan_job(
    root: Path, db: str, job: str, seed: str | None = None
) -> tuple[list[DataMovementPlan], HubPlan | None]:
    out = _script(SCRIPT, "--job-id", job, "--db", db, "--artifact-root", str(root), seed=seed)
    assert out["status"] == "complete", out
    plans = []
    assert out["step_count"] == len(out["plans"])
    for rel in out["plans"]:
        payload = json.loads((root / rel).read_text())
        plans.append(load_contract(DataMovementPlan, payload, artifact=rel))
    assert out["hub"] == f"{db}/{job}/data-movement/hub/plan.json" or out["hub"] is None
    hub = None
    if out["hub"]:
        hub = load_contract(
            HubPlan, json.loads((root / out["hub"]).read_text()), artifact=out["hub"]
        )
    return plans, hub


def _report(root: Path, db: str, job: str) -> dict:
    reports = sorted((root / db / job / "synthesis").glob("v*/report.json"))
    data: dict = json.loads(reports[-1].read_text())
    return data


def _assignment(root: Path, db: str, job: str, report: dict) -> dict:
    version = report["assignment_summary"]["version"]
    path = root / db / job / "assignment" / f"v{version}" / "assignment.json"
    data: dict = json.loads(path.read_text())
    return data


def _check_plans(
    plans: list[DataMovementPlan],
    hub: HubPlan | None,
    report: dict,
    assignment: dict,
    synthesized_now: bool = True,
) -> None:
    """``synthesized_now`` is False for a saved real-model run (``run.external``): its
    stored waves were written by the code of its day, so the checks of a rule newer
    than the saved run (#2166) are not read from it."""
    waves = report["migration_waves"]
    by_wave = {w["wave"]: w for w in waves}
    # One plan per step, numbered as facts.slice_plan numbers it (#1698, ADR-033 Decision
    # 1); without a slice plan, one per migration wave.
    steps = ((report.get("facts") or {}).get("slice_plan") or {}).get("plan_steps") or []
    expected = [(s["step_number"], s["phase_wave"]) for s in steps] or [
        (w["wave"], w["wave"]) for w in waves
    ]
    assert [(p.step, p.wave) for p in plans] == expected
    shared = {w for w in by_wave if [st["phase_wave"] for st in steps].count(w) > 1}
    units = {
        u["key"]: u
        for u in ((report.get("facts") or {}).get("slice_plan") or {}).get("move_units") or []
    }
    for plan, step in zip(plans, steps, strict=True) if steps else ():
        if plan.wave in shared:
            # A wave of several steps: each step's plan holds its own engine only, under
            # its own title.
            assert {t.engine for t in plan.targets} == {step["engine"]}, plan.step
            assert plan.title != by_wave[plan.wave]["title"], plan.step
            assert display_engine(step["engine"]) in plan.title, plan.step
        else:
            assert plan.title == by_wave[plan.wave]["title"], plan.step
        if step["kind"] not in ("slice", "engine_wave"):
            continue
        # plan.json and facts.slice_plan name the same owners (ADR-031 Amendment 9): the
        # step owns exactly its units' tables, and every copy comes from its writer.
        members = [units[k] for st in step["step_stages"] for k in st["unit_keys"]]
        owned = {t for u in members for t in u["owned_tables"]}
        copied = {r["table"]: r["source_of_truth"] for u in members for r in u["replicated_in"]}
        targets = [t for t in plan.targets if t.engine == step["engine"]]
        assert {t for tg in targets if tg.role == "owner" for t in tg.tables} == owned, plan.step
        for tg in targets:
            if tg.role == "replica":
                for table in tg.tables:
                    if table in copied:
                        assert tg.source_of_truth == copied[table], (plan.step, table)
    # Wordpress and the other samples have later waves, so the job has a hub plan.
    assert hub is not None and len(waves) > 1
    assert hub.topics and hub.cdc.tool == "debezium"
    assert all(t.key_columns and t.cleanup_policy == "compact" for t in hub.topics)
    assert all(t.topic.startswith(f"{hub.cdc.topic_prefix}.") for t in hub.topics)
    assert {p.wave_count for p in plans} == {len(waves)}
    assert {p.assignment_version for p in plans} == {report["assignment_summary"]["version"]}

    wave1, later = plans[0], plans[1:]
    assert wave1.cdc.tool == "dms"
    assert wave1.hub == []
    assert [t.engine for t in wave1.targets] == list(waves[0]["engines"])

    for plan in later:
        wave = by_wave[plan.wave]
        assert plan.cdc.tool == "debezium"
        assert plan.cdc == hub.cdc and plan.hub == []
        read = {t for target in plan.targets for t in target.tables}
        assert read, plan.wave
        by_topic = {t.topic: t.table for t in hub.topics}
        assert set(plan.hub_topics) <= set(by_topic), plan.wave
        covered = {by_topic[t] for t in plan.hub_topics} | {
            u.table for u in plan.unsupported if u.kind in {"no_key", "view", "unknown_table"}
        }
        assert read <= covered, (plan.wave, sorted(read - covered))
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
    for plan in later:
        wave = by_wave[plan.wave]
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
    # A table nothing writes stays with the relational step (#2166, ADR-033 Amendment 1):
    # no wave names a reader its owner, so no plan asks a non-relational engine to feed
    # a copy of it.
    written = set(saved_write_groups(assignment))
    relational = wave_retained_engine(waves)
    for wave in waves if synthesized_now else []:
        for row in wave.get("table_owners") or []:
            if row["table"] not in written:
                assert row["owner"] == (relational or SOURCE_DATABASE), (wave["wave"], row)
    for plan in later if synthesized_now else []:
        unwritten = sorted(
            u.table
            for u in plan.unsupported
            if u.kind == "reverse_replication" and u.table not in written
        )
        assert not unwritten, (plan.wave, len(unwritten), unwritten[:5])
    # The committed price table carries the DMS, MSK and MSK Connect sections (#1424), so a
    # plan states its cost from them, or names the one thing it could not price: a hub
    # table the collector did not measure.
    prices = price_table()
    dms = prices.section("dms.replication")
    hub_gaps = [u for u in hub.unsupported if u.kind == "cost"]
    assert (hub.monthly_cost_usd is not None) != bool(hub_gaps)
    assert all(u.item == "hub_storage" for u in hub_gaps)
    for plan in plans:
        gaps = [u for u in plan.unsupported if u.kind == "cost"]
        if plan.cdc.tool == "dms":
            # The step's own sizing, Multi-AZ as the stack deploys it (#1798).
            assert plan.replication is not None, plan.wave
            s = plan.replication.sizing
            month = round(
                dms["multi_az_instance_hourly"][s.instance_class] * HOURS_PER_MONTH
                + dms["multi_az_storage_price_per_gb_month"]
                * max(0, s.allocated_storage_gb - s.included_storage_gb),
                2,
            )
            assert plan.monthly_cost_usd == month, plan.wave
            assert not gaps, plan.wave
        else:
            # A step's cost is its writers; the hub plan carries the cluster.
            assert plan.monthly_cost_usd is not None and not gaps, plan.wave


def test_each_sample_job_gets_one_valid_plan_per_step(run: PipelineResult, tmp_path: Path) -> None:
    root = _copy_job(run, tmp_path)
    plans, hub = _plan_job(root, run.db, run.job_id)
    report = _report(root, run.db, run.job_id)
    _check_plans(
        plans, hub, report, _assignment(root, run.db, run.job_id, report), not run.external
    )


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


def test_priced_plans_state_each_waves_monthly_cost(run: PipelineResult, tmp_path: Path) -> None:
    """With DMS, MSK and MSK Connect prices in the table every wave states a cost: the
    committed table has none yet (they arrive with the first online refresh, #1424), so
    the prices here are round test inputs, not AWS prices."""
    import datetime as dt

    from src.shared.catalog.prices import PriceTable
    from src.storage.local_store import LocalArtifactStore
    from src.tools.data_movement.dms_sizing import size_from_collector
    from src.tools.data_movement.plan import build_step_plans, load_inputs

    root = _copy_job(run, tmp_path)
    inputs = load_inputs(LocalArtifactStore(base_dir=str(root)), run.db, run.job_id)
    cls = size_from_collector(inputs.collector).instance_class
    table = PriceTable(
        region="us-east-1",
        retrieved_on=dt.date(2026, 11, 2),
        published={"AmazonMSK": dt.date(2026, 11, 1)},
        sections={
            "dms": {
                "replication": {
                    "instance_hourly": {cls: 0.5},
                    "storage_price_per_gb_month": 0.1,
                    # The DMS model prices the sized class Multi-AZ (#1798).
                    "multi_az_instance_hourly": {cls: 1.0},
                    "multi_az_storage_price_per_gb_month": 0.2,
                }
            },
            "msk": {
                "provisioned": {
                    "instance_hourly": {"kafka.m5.large": 0.2},
                    "storage_price_per_gb_month": 0.1,
                }
            },
            "msk_connect": {"workers": {"mcu_price_per_hour": 0.1}},
        },
    )
    plans, hub = build_step_plans(inputs, prices=table)
    # The sized class, Multi-AZ: 1.0 x 730 hours + 0.2 x the row's storage.
    assert plans[0].replication is not None
    sizing = plans[0].replication.sizing
    storage = max(0, sizing.allocated_storage_gb - sizing.included_storage_gb)
    assert plans[0].monthly_cost_usd == round(1.0 * HOURS_PER_MONTH + 0.2 * storage, 2)
    assert hub is not None and hub.monthly_cost_usd is not None, hub.unsupported
    for plan in plans:
        costs = [u for u in plan.unsupported if u.kind == "cost"]
        assert plan.monthly_cost_usd is not None, (plan.wave, costs)
        assert not costs
    # Only the hub plan carries the brokers (0.2 x 3 x 730 = 438); a step is its writers.
    free = table.model_copy(
        update={
            "sections": {
                **table.sections,
                "msk": {
                    "provisioned": {
                        **table.sections["msk"]["provisioned"],
                        "instance_hourly": {"kafka.m5.large": 0.0},
                    }
                },
            }
        }
    )
    unbrokered, free_hub = build_step_plans(inputs, prices=free)
    assert free_hub is not None and free_hub.monthly_cost_usd is not None
    assert round(hub.monthly_cost_usd - free_hub.monthly_cost_usd, 2) == 438.0
    assert [p.monthly_cost_usd for p in plans] == [p.monthly_cost_usd for p in unbrokered]


@pytest.mark.deterministic
def test_the_designed_wordpress_job_points_each_writer_at_its_design(
    designed_run: PipelineResult, tmp_path: Path
) -> None:
    root = _copy_job(designed_run, tmp_path)
    plans, hub = _plan_job(root, designed_run.db, designed_run.job_id)
    report = _report(root, designed_run.db, designed_run.job_id)
    _check_plans(
        plans, hub, report, _assignment(root, designed_run.db, designed_run.job_id, report)
    )
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
    plans, hub = _plan_job(root, db, job)
    assert len(plans) >= 2
    _check_plans(plans, hub, report, _assignment(root, db, job, report))
