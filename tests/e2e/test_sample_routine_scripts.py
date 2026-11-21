# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1 carries the bundled samples' routine sets (#1954, ADR-034 Decisions 4, 6 and 7).

``tests/e2e/test_sample_routines.py`` follows each synthetic routine through the routine
phase to the deliverables. This module takes the same runs on to step 1, on a copy of each
job: the transformation contract, the data movement plan and ``render_dms_step.py``, then
``run_dms_step.py routines`` (its one AWS read, the task's full load at 100%, answered by a
botocore Stubber, so nothing is sent) and ``run_dms_step.py triggers-scripts`` (no AWS
call). Each routine must land in its step 1 bucket (carried, left to the triggers-enable
script, or not carried with its reason); ``routines-deploy.sql`` and ``triggers-enable.sql``
must hold exactly the expected objects, rewritten and in order; the instructions and the
gate's routines check must name them; and the counts must agree with ``facts.routines``,
which the reports and the results page print.

The PostgreSQL sample also runs the part of the proof that needs no AWS: a PostgreSQL
started by ``pgserver`` on a Unix socket (no TCP listener) plays the Aurora target, with
the tables the routines name created from ``step-1.ddl.sql``. The deploy, the read-only
check, the triggers-enable script and its check run on it as ``routines.md`` and
``triggers.md`` say, and ``run_dms_step.py routines-check`` / ``triggers-check`` read the
output. The MySQL twin needs a local ``mysqld``, so it stays in ``tests/integration``.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import subprocess  # nosec B404 -- runs this repo's own scripts and the bundled psql, fixed argv
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.pipeline import REPO, PipelineResult, _env
from tests.e2e.test_sample_routines import EXPECTED, FAILED, all_routines

pytestmark = [pytest.mark.e2e, pytest.mark.deterministic]

# The Aurora target's own schema or database name (the step contract's target database).
TARGET_DATABASE = {"wordpress": "wordpress", "discourse": "discourse"}
# MySQL triggers and events are created by the triggers-enable script, never the deploy
# (MySQL can't create a trigger disabled; an enabled event would run during CDC).
DEFERRED_KINDS = {"wordpress": {"trigger", "event"}, "discourse": set()}
# A line of each sample's refused routine's body: it never reaches a step 1 script.
REFUSED_BODY = {"wordpress": "innodb_flush_log_at_trx_commit", "discourse": "pg_backup_start("}
# The tables the discourse routines name, created on the stand-in target from
# step-1.ddl.sql before the deploy (a SQL function's body is checked when it is created,
# a trigger needs its table): the synthetic set's and the collection's own (#2378).
DISCOURSE_TABLES = ("category_settings", "notifications", "posts", "topic_timers", "topics")


@dataclass
class Step1:
    run: PipelineResult
    root: Path
    step: Path
    contract: dict[str, Any]
    routines: dict[str, Any]  # run_dms_step.py routines' result line
    triggers: dict[str, Any]  # run_dms_step.py triggers-scripts' result line
    untested: dict[str, Any]  # routines' result line before any routine test (#2411)

    @property
    def common(self) -> list[str]:
        return ["--job-id", self.run.job_id, "--db", self.run.db, "--artifact-root", str(self.root)]

    def text(self, name: str) -> str:
        return (self.step / name).read_text(encoding="utf-8")

    def code(self, name: str) -> str:
        """A SQL file without its comment lines: the statements the customer runs."""
        return "\n".join(ln for ln in self.text(name).splitlines() if not ln.startswith("--"))


def _script(*args: str) -> dict[str, Any]:
    proc = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script args
        [sys.executable, *args], cwd=REPO, capture_output=True, text=True, env=_env(), timeout=900
    )
    assert proc.returncode == 0, f"{args[0]} failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
    payload: dict[str, Any] = json.loads(proc.stdout.strip().splitlines()[-1])
    return payload


def _routines_after_the_full_load(root: Path, run: PipelineResult) -> dict[str, Any]:
    """``run_dms_step.py routines`` as /move runs it once status shows the full load at
    100%. Its one AWS read (the stack, the endpoints' connections and the task) is answered
    by a botocore Stubber, the way the unit tests answer it: nothing is sent."""
    import scripts.run_dms_step as dms
    from tests.unit.tools.aws import dms_move_stubs as stubs
    from tests.unit.tools.aws.aws_stubs import StubbedCredMgr

    step = root / run.db / run.job_id / "data-movement" / "step-1"
    name = dms.stack_name(dms.load_inputs(step / "inputs.json"), run.job_id)
    cred = StubbedCredMgr()
    stubs.full_load_done(cred, name)
    saved = os.environ.get("AWS_PROFILE")
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            code = dms.main(
                ["routines", "--job-id", run.job_id, "--db", run.db, "--artifact-root", str(root)],
                clients=stubs.clients(cred),
                sleep=lambda _s: None,
            )
    finally:  # the helper sets AWS_PROFILE from the inputs file: put the caller's back
        os.environ.pop("AWS_PROFILE", None)
        if saved is not None:
            os.environ["AWS_PROFILE"] = saved
    line: dict[str, Any] = json.loads(out.getvalue().strip().splitlines()[-1])
    assert code == 0, line
    for stubber in cred.stubbers.values():
        stubber.assert_no_pending_responses()
    return line


# Each sample's step 1, or the reason building it failed: a failed build fails each of
# the sample's tests at once with that reason, instead of running again for each.
_STEP1: dict[str, Step1 | str] = {}


@pytest.fixture(params=sorted(EXPECTED))
def step1(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Step1:
    """Step 1 on a copy of the sample's job, built once per sample per session."""
    sample: str = request.param
    if sample not in _STEP1:
        try:
            _STEP1[sample] = _build_step1(request, tmp_path_factory, sample)
        except Exception as exc:  # noqa: BLE001 -- kept as text, so each test fails on it once
            _STEP1[sample] = f"{type(exc).__name__}: {exc}"
            raise
    built = _STEP1[sample]
    if isinstance(built, str):
        pytest.fail(f"step 1 for {sample} failed to build earlier in this session: {built}")
    return built


def _build_step1(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory, sample: str
) -> Step1:
    run: PipelineResult = request.getfixturevalue(f"{sample}_run")
    root = tmp_path_factory.mktemp(f"step1-{sample}") / "artifacts"
    shutil.copytree(run.job_dir(), root / run.db / run.job_id)
    common = ["--job-id", run.job_id, "--db", run.db, "--artifact-root", str(root)]
    contract_line = _script("scripts/run_transformation_contract.py", *common)
    assert contract_line["contract_status"] == "valid", contract_line
    _script("scripts/run_data_movement_plan.py", *common)
    _script("scripts/render_dms_step.py", *common)
    step = root / run.db / run.job_id / "data-movement" / "step-1"
    from tests.unit.tools.aws.dms_move_stubs import INPUTS

    inputs = {**INPUTS, "target_database_name": TARGET_DATABASE[sample]}
    (step / "inputs.json").write_text(json.dumps(inputs))
    [path] = sorted((root / run.db / run.job_id / "transformation-contract").glob("v*/step-1.json"))
    contract = json.loads(path.read_text())
    # #2411: before any routine test the scripts hold every carried routine back; then the
    # job records a /test-routines result with no difference on any carried routine, on the
    # sample's routines file, and the scripts carry them.
    untested = _routines_after_the_full_load(root, run)
    from src.storage.local_store import LocalArtifactStore
    from tests.unit.tools.data_movement.routine_fixtures import mark_tested

    mark_tested(LocalArtifactStore(base_dir=str(root)), db=run.db, job_id=run.job_id,
                version=contract["routines"]["routine_migration_version"])  # fmt: skip
    routines = _routines_after_the_full_load(root, run)
    triggers = _script("scripts/run_dms_step.py", "triggers-scripts", *common)
    return Step1(run, root, step, contract, routines, triggers, untested)


def _bucket(sample: str, kind: str, status: str) -> str:
    if status == "needs_attention":
        return "not_carried"
    return "deferred_to_cutover" if kind in DEFERRED_KINDS[sample] else "carried"


def _name(routine_id: str) -> str:
    """The object's own name: the last dotted part, without a signature."""
    return routine_id.split("(", 1)[0].rsplit(".", 1)[-1]


def test_each_routine_lands_in_its_step_1_bucket(step1: Step1) -> None:
    sample = step1.run.db
    section = step1.contract["routines"]
    rows = {(r["kind"], r["routine_id"]): r for r in section["routines"]}
    for (kind, rid), status in all_routines(sample).items():
        row = rows.pop((kind, rid))
        assert row["carry"] == _bucket(sample, kind, status), (kind, rid, row)
        assert row["reasons"] == ([FAILED[sample]] if status == "needs_attention" else [])
    assert rows == {}
    carried = sum(r["carry"] == "carried" for r in section["routines"])
    deferred = sum(r["carry"] == "deferred_to_cutover" for r in section["routines"])
    assert (section["carried"], section["deferred"]) == (carried, deferred)
    # The command's line says the same, and names each routine it does not carry.
    line = step1.routines
    assert line["status"] == "ready" and line["label"] == "tested on a test Aurora"
    assert (line["carried"], line["deferred"]) == (carried, deferred)
    assert line["in_script"] == carried and line["refused"] == 0
    assert (line["proof"], line["held_back"]) == ("current", 0)
    # Before the test (#2411): none in the script, each held back as not tested.
    before = step1.untested
    assert (before["in_script"], before["proof"], before["held_back"]) == (0, "not_run", carried)
    assert {r["reason"] for r in before["held_back_routines"]} == {"not_tested"}
    assert (before["label"], before["refused"]) == ("checked, not tested yet", 0)  # #2429
    not_carried = {(r["kind"], r["routine_id"]): r["reasons"] for r in line["not_carried_routines"]}
    [(kind, rid)] = [k for k, s in EXPECTED[sample].items() if s == "needs_attention"]
    assert not_carried.pop((kind, rid)) == [FAILED[sample]]
    assert not_carried == {}


def _creates(sql: str) -> list[tuple[str, str]]:
    """Each object the script creates, as (kind, name), in order."""
    pattern = re.compile(
        r"^CREATE (?:OR REPLACE )?(?:DEFINER=\S+ )?(FUNCTION|PROCEDURE|TRIGGER|EVENT) "
        r"[`\"]?(?:[\w]+[`\"]?\.[`\"]?)?(\w+)",
        re.M,
    )
    return [(k.lower(), n) for k, n in pattern.findall(sql)]


def test_the_deploy_script_creates_the_carried_routines_in_order(step1: Step1) -> None:
    sample = step1.run.db
    sql = step1.code("routines-deploy.sql")
    carried = [
        (kind, _name(rid))
        for (kind, rid), status in all_routines(sample).items()
        if _bucket(sample, kind, status) == "carried"
    ]
    made = _creates(sql)
    assert sorted(made) == sorted(carried), made
    # Dependency order: a routine follows what it calls.
    if sample == "wordpress":
        assert made.index(("function", "wp_post_comment_count")) < made.index(
            ("procedure", "wp_refresh_comment_count")
        )
        # The rewrite is applied again: the definer that won't exist names the target user.
        assert "wp_user" not in sql and "localhost" not in sql
        assert sql.count("CREATE DEFINER=`admin`@`%`") == len(carried)
    else:
        assert made.index(("function", "posts_bump_topic")) < made.index(
            ("trigger", "posts_bump_topic")
        )
        # The collection's own triggers follow the guard functions they call (#2378).
        for table, column in (
            ("category_settings", "require_reply_approval"),
            ("category_settings", "require_topic_approval"),
            ("topic_timers", "topic_id"),
        ):
            assert made.index(("function", f"raise_{table}_{column}_readonly")) < made.index(
                ("trigger", f"{table}_{column}_readonly")
            )
        # One transaction; the trigger is created disabled in it (DMS applies its effects).
        assert sql.count("BEGIN;") == 1 and sql.rstrip().endswith("COMMIT;")
        trigger = sql.index("CREATE TRIGGER posts_bump_topic")
        disable = sql.index('ALTER TABLE "public"."posts" DISABLE TRIGGER "posts_bump_topic";')
        assert trigger < disable < sql.index("COMMIT;")
    # Nothing that is not carried reaches the deploy, and the routine left out keeps its body.
    assert REFUSED_BODY[sample] not in step1.text("routines-deploy.sql")
    for (kind, rid), status in all_routines(sample).items():
        if _bucket(sample, kind, status) != "carried":
            assert (kind, _name(rid)) not in made, (kind, rid)


def test_the_triggers_enable_script_turns_on_what_step_1_left_off(step1: Step1) -> None:
    sample = step1.run.db
    sql = step1.code("triggers-enable.sql")
    line = step1.triggers
    assert line["status"] == "ready" and line["runs_it"] == "the customer; /move never runs it"
    if sample == "wordpress":
        # The trigger and the event step 1 left to it, created as the deploy writes them.
        assert sorted(_creates(sql)) == [
            ("event", "wp_purge_action_logs"),
            ("trigger", "wp_comments_count_after_insert"),
        ]
        assert sql.count("CREATE DEFINER=`admin`@`%`") == 2
        assert " ENABLE DO " in sql  # the event keeps the status its source gives
        assert (line["expected"], line["in_script"], line["events_enabled"]) == (2, 2, 1)
        assert line["excluded"] == 0
    else:
        # The triggers the deploy created disabled are enabled, in one transaction: the
        # synthetic set's and the collection's own 3 (#2378).
        assert _creates(sql) == []
        assert sorted(re.findall(r"^ALTER TABLE .*$", sql, re.M)) == [
            'ALTER TABLE "public"."category_settings" ENABLE TRIGGER '
            '"category_settings_require_reply_approval_readonly";',
            'ALTER TABLE "public"."category_settings" ENABLE TRIGGER '
            '"category_settings_require_topic_approval_readonly";',
            'ALTER TABLE "public"."posts" ENABLE TRIGGER "posts_bump_topic";',
            'ALTER TABLE "public"."topic_timers" ENABLE TRIGGER "topic_timers_topic_id_readonly";',
        ]
        assert sql.count("BEGIN;") == 1 and sql.rstrip().endswith("COMMIT;")
        assert (line["expected"], line["in_script"], line["excluded"]) == (4, 4, 0)
    assert REFUSED_BODY[sample] not in step1.text("triggers-enable.sql")


def test_the_instructions_and_the_gate_name_them(step1: Step1) -> None:
    from src.storage.local_store import LocalArtifactStore
    from src.tools.data_movement.dms_gate import routines_check
    from src.tools.data_movement.dms_step import load_step1

    sample = step1.run.db
    section = step1.contract["routines"]
    md = step1.text("routines.md")
    n = section["carried"]
    assert f"Carried by `routines-deploy.sql`: {n} of {n} routines, in dependency order." in md
    not_carried = section["total"] - section["carried"] - section["deferred"]
    assert f"- Not carried: {not_carried} " in md
    assert "The cutover is your responsibility." in md
    triggers_md = step1.text("triggers.md")
    assert (
        "triggers-enable.sql" in triggers_md
        and "The cutover is your responsibility." in triggers_md
    )
    # Before the check has run, the gate's routines check already fails on each routine
    # step 1 does not carry, named with its reason.
    store = LocalArtifactStore(base_dir=str(step1.root))
    inputs = load_step1(store, sample, step1.run.job_id, with_ddl=False)
    check = routines_check(inputs, None)
    assert check is not None and check.status == "fail"
    [(kind, rid)] = [k for k, s in EXPECTED[sample].items() if s == "needs_attention"]
    reasons = json.dumps(check.model_dump(mode="json"))
    assert f"routines: {kind} {rid}: not carried: {FAILED[sample]}" in reasons
    assert "the check output has not been read yet" in reasons


def test_the_facts_agree_with_step_1(step1: Step1) -> None:
    """The figures the reports and the results page print (``facts.routines``, ADR-032)
    are the ones step 1 carries: every routine carried as it is or rewritten is in the
    deploy or the triggers-enable script, and every routine to migrate yourself is one
    step 1 does not carry."""
    report = json.loads(step1.run.report_json().read_text())
    facts = report["facts"]["routines"]
    section = step1.contract["routines"]
    assert facts["routine_count"] == section["total"]
    assert facts["as_is_count"] + facts["rewritten_count"] == (
        section["carried"] + section["deferred"]
    )
    # The deploy creates the carried ones; on Aurora MySQL the triggers-enable script
    # creates the deferred ones, on Aurora PostgreSQL it enables the carried triggers.
    assert step1.routines["in_script"] == section["carried"]
    triggers = sum(r["kind"] == "trigger" for r in section["routines"] if r["carry"] == "carried")
    assert step1.triggers["in_script"] == section["deferred"] + triggers
    not_carried = {
        (r["kind"], r["routine_id"]) for r in section["routines"] if r["carry"] == "not_carried"
    }
    assert facts["migrate_yourself_count"] == len(not_carried)
    assert {(r["kind"], r["routine_id"]) for r in facts["to_do_routines"]} == not_carried


# --- The PostgreSQL proof without AWS: a local PostgreSQL plays the Aurora target ---------


@pytest.fixture
def stand_in_target(tmp_path: Path) -> Iterator[Any]:
    """A PostgreSQL started by ``pgserver`` in a temp folder, on a Unix socket only, and
    stopped (its data folder deleted) when the test ends."""
    pgserver = pytest.importorskip("pgserver")
    server = pgserver.get_server(tmp_path / "pgdata", cleanup_mode="delete")
    try:
        yield server
    finally:
        server.cleanup()


def _psql(server: Any, *args: str) -> subprocess.CompletedProcess[str]:
    import pgserver

    psql = Path(pgserver.__file__).parent / "pginstall" / "bin" / "psql"
    return subprocess.run(  # nosec B603 -- the bundled psql with fixed argv
        [str(psql), server.get_uri(), "-X", *args], capture_output=True, text=True, check=False
    )


def _tables(ddl: str, names: tuple[str, ...]) -> str:
    blocks = []
    for name in names:
        match = re.search(rf'^CREATE TABLE "{name}" \(\n.*?^\);$', ddl, re.M | re.S)
        assert match, name
        blocks.append(match.group(0))
    return "\n\n".join(blocks)


@pytest.mark.parametrize("step1", ["discourse"], indirect=True)
def test_the_postgresql_scripts_deploy_and_check_on_a_stand_in_target(
    step1: Step1, stand_in_target: Any
) -> None:
    server = stand_in_target
    tables = _tables(step1.text("step-1.ddl.sql"), DISCOURSE_TABLES)
    # psql stops on the first error and server.psql raises, so no output is read here; the
    # tables the DDL created are listed instead.
    server.psql('\\set ON_ERROR_STOP on\nSET search_path TO "public";\n' + tables)
    listed = server.psql(
        "\\set QUIET on\n\\t on\nSELECT string_agg(relname, ',' ORDER BY relname) FROM pg_class"
        " WHERE relnamespace = 'public'::regnamespace AND relkind = 'r';"
    )
    assert tuple(listed.strip().split(",")) == DISCOURSE_TABLES, listed

    deployed = _psql(server, "-f", str(step1.step / "routines-deploy.sql"))
    assert deployed.returncode == 0, deployed.stderr
    state = server.psql(
        "\\set QUIET on\n\\t on\nSELECT tgenabled FROM pg_trigger WHERE tgname = 'posts_bump_topic';"
    )
    assert state.strip() == "D"  # created disabled while DMS replicates
    ran = _psql(server, "-f", str(step1.step / "routines-check.sql"), "-o",
                str(step1.step / "routines-check.tsv"))  # fmt: skip
    assert ran.returncode == 0, ran.stderr
    check = _script("scripts/run_dms_step.py", "routines-check", *step1.common)
    carried = step1.contract["routines"]["carried"]
    assert (check["confirmed"], check["carried"], check["mismatches"]) == (carried, 13, []), check
    # Every carried routine is confirmed; the gate still fails on the ones not carried.
    from src.contracts.routine_check import RoutineCheckResult
    from src.storage.local_store import LocalArtifactStore
    from src.tools.data_movement.dms_gate import routines_check
    from src.tools.data_movement.dms_step import load_step1

    result = RoutineCheckResult.model_validate_json(Path(check["result"]).read_text())
    store = LocalArtifactStore(base_dir=str(step1.root))
    gate = routines_check(load_step1(store, "discourse", step1.run.job_id, with_ddl=False), result)
    assert gate is not None and gate.status == "fail"
    said = json.dumps(gate.model_dump(mode="json"))
    assert "13 of 13 carried routines confirmed" in said, said
    assert "procedure public.begin_upload_snapshot(text): not carried" in said

    enabled = _psql(server, "-f", str(step1.step / "triggers-enable.sql"))
    assert enabled.returncode == 0, enabled.stderr
    state = server.psql(
        "\\set QUIET on\n\\t on\nSELECT tgenabled FROM pg_trigger WHERE tgname = 'posts_bump_topic';"
    )
    assert state.strip() == "O"  # its source state (the collection's tgenabled letter)
    ran = _psql(server, "-f", str(step1.step / "triggers-enable-check.sql"), "-o",
                str(step1.step / "triggers-enable-check.tsv"))  # fmt: skip
    assert ran.returncode == 0, ran.stderr
    triggers = _script("scripts/run_dms_step.py", "triggers-check", *step1.common)
    # The carried triggers are confirmed in their source state, and none is left out.
    assert (triggers["expected"], triggers["confirmed"], triggers["mismatches"]) == (4, 4, [])
    assert triggers["status"] == "pass" and triggers["not_carried"] == 0, triggers
    # The check output holds no routine text.
    for name in ("routines-check.tsv", "triggers-enable-check.tsv"):
        assert "bumped_at" not in step1.text(name), name


# --- After /test-routines: the reports state the test's result (#2429) --------------------

# The outcomes of the real runs on #2411 (2026-11-17): on Aurora PostgreSQL every carried
# routine showed no difference; on Aurora MySQL the rewritten procedure could not be tested
# (its original names another DEFINER, #2426), so step 1 holds it back.
TESTED_OUTCOMES = {
    "discourse": {},
    "wordpress": {"wordpress.wp_refresh_comment_count": "not_proved"},
}


@dataclass
class Tested:
    run: PipelineResult
    report: dict[str, Any]  # report.json after synthesis ran again
    engineering: str  # engineering-report.md, plain
    executive: str  # executive-report.pdf, its page text
    routines: dict[str, Any]  # run_dms_step.py routines' line after the test
    results: dict[str, Any]  # the results page's payload, GET /assessments/{job}/results


_TESTED: dict[str, Tested | str] = {}


@pytest.fixture(params=sorted(TESTED_OUTCOMES))
def tested(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Tested:
    """A copy of the sample's job after /test-routines with the real runs' outcomes, then
    the scripts its Step 8 lists: synthesis again and the deliverables."""
    sample: str = request.param
    if sample not in _TESTED:
        try:
            _TESTED[sample] = _build_tested(request, tmp_path_factory, sample)
        except Exception as exc:  # noqa: BLE001 -- kept as text, so each test fails on it once
            _TESTED[sample] = f"{type(exc).__name__}: {exc}"
            raise
    built = _TESTED[sample]
    if isinstance(built, str):
        pytest.fail(f"the tested copy of {sample} failed to build earlier in this session: {built}")
    return built


def _build_tested(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory, sample: str
) -> Tested:
    from src.storage.local_store import LocalArtifactStore
    from tests.e2e.deliverable_figures import md_plain, pdf_page_texts
    from tests.unit.tools.aws.dms_move_stubs import INPUTS
    from tests.unit.tools.data_movement.routine_fixtures import mark_tested

    run: PipelineResult = request.getfixturevalue(f"{sample}_run")
    root = tmp_path_factory.mktemp(f"tested-{sample}") / "artifacts"
    shutil.copytree(run.job_dir(), root / run.db / run.job_id)
    meta = run.artifact_root / ".meta" / f"{run.job_id}.json"
    if meta.exists():  # the results route finds the job by it
        (root / ".meta").mkdir(parents=True, exist_ok=True)
        shutil.copy2(meta, root / ".meta" / meta.name)
    common = ["--job-id", run.job_id, "--db", run.db, "--artifact-root", str(root)]
    for prepare in PREPARE_SCRIPTS:
        _script(prepare, *common)
    step = root / run.db / run.job_id / "data-movement" / "step-1"
    (step / "inputs.json").write_text(
        json.dumps({**INPUTS, "target_database_name": TARGET_DATABASE[sample]})
    )
    [path] = sorted((root / run.db / run.job_id / "transformation-contract").glob("v*/step-1.json"))
    version = json.loads(path.read_text())["routines"]["routine_migration_version"]
    mark_tested(LocalArtifactStore(base_dir=str(root)), TESTED_OUTCOMES[sample], db=run.db,
                job_id=run.job_id, version=version)  # fmt: skip
    routines = _routines_after_the_full_load(root, run)
    # /test-routines Step 8, deterministic here (the e2e runs use --llm-mode none).
    synthesis = _script("scripts/run_synthesis.py", *common)
    assert synthesis["status"] == "complete", synthesis
    rendered = _script("scripts/run_report.py", *common)
    assert rendered["status"] == "complete", rendered
    files = {p.rsplit("/", 1)[-1]: Path(p) for p in rendered["deliverables"]}
    return Tested(
        run=run,
        report=json.loads((root / str(rendered["report"])).read_text()),
        engineering=" ".join(md_plain(files["engineering-report.md"].read_text()).split()),
        executive=" ".join(" ".join(pdf_page_texts(str(files["executive-report.pdf"]))).split()),
        routines=routines,
        results=_results_payload(root, run.job_id),
    )


def _results_payload(root: Path, job_id: str) -> dict[str, Any]:
    """What the results page reads: the local API's results route on ``root``."""
    from fastapi.testclient import TestClient

    from src.api.main import app
    from src.api.routes import results
    from src.api.services.local_execution import LocalExecutionService
    from src.api.services.local_s3 import LocalS3Service
    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(base_dir=str(root))
    saved = (results.sfn_service, results.s3_service)
    results.sfn_service, results.s3_service = LocalExecutionService(store), LocalS3Service(store)
    try:
        response = TestClient(app).get(f"/api/v1/assessments/{job_id}/results")
    finally:
        results.sfn_service, results.s3_service = saved
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


PREPARE_SCRIPTS = (
    "scripts/run_transformation_contract.py",
    "scripts/run_data_movement_plan.py",
    "scripts/render_dms_step.py",
)


def _carried_risks(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in report["risk_assessment"]["risks"] if r.get("rule") == "routines.carried"]


def test_after_the_test_every_report_states_its_result(tested: Tested) -> None:
    facts = tested.report["facts"]["routines"]
    assert facts["proof"] == "tested on a test Aurora" == tested.routines["label"]
    assert facts["line"] in tested.engineering
    assert facts["executive_line"] in tested.executive
    for text in (tested.engineering, tested.executive, facts["line"], facts["executive_line"]):
        assert "not tested yet" not in text and "/test-routines command" not in text
    if tested.run.db == "discourse":
        assert facts["line"].endswith(
            "On a test Aurora, of 13 carried routines: 13 showed no difference on the calls made."
        )
        assert facts["executive_line"].endswith(
            "Tested on a throwaway Aurora: step 1 deploys all 13."
        )
    else:
        assert facts["line"].endswith(
            "On a test Aurora, of 4 carried routines: 3 showed no difference on the calls made; "
            "1 could not be tested. Step 1 deploys only those 3; the other one is held back."
        )
        assert facts["executive_line"].endswith(
            "Tested on a throwaway Aurora: step 1 deploys 3; 1 is held back."
        )


def test_after_the_test_the_risk_and_the_gate_ask_only_for_what_is_held(tested: Tested) -> None:
    risks = _carried_risks(tested.report)
    gate = tested.report["migration_waves"][0]["gate"]
    assert "Test the carried routines on a throwaway Aurora" not in tested.engineering
    if tested.run.db == "discourse":  # all 13 showed no difference: nothing left to close
        assert risks == [] and "carried routines to check" not in gate
        return
    [risk] = risks
    assert risk["description"].endswith(
        "1 of the 4 routines carried to Aurora MySQL (1 procedure) is held back after the test on a test Aurora, so "
        "step 1 does not deploy it. Why: the test could not run or compare it, or a call failed (1)."
    )
    assert f"Close {risk['risk_id']} before this gate (carried routines to check)" in gate
    # The engineering report's risk card says the same, with no ask to test it before.
    held = "1 of the 4 routines carried to Aurora MySQL (1 procedure) is held back after the test on a test Aurora"
    assert held in tested.engineering
    assert "Fix what /test-routines reported for it" in tested.engineering


def test_after_the_test_the_results_page_reads_the_same(tested: Tested) -> None:
    """The results page renders the payload as it is (renderers only format): it carries
    the same routine sentences and, on wordpress, the held risk; on discourse no carried
    risk."""
    synthesis = tested.results["synthesis"]
    facts = tested.report["facts"]["routines"]
    assert synthesis["facts"]["routines"]["line"] == facts["line"]
    assert synthesis["facts"]["routines"]["executive_line"] == facts["executive_line"]
    assert _carried_risks(synthesis) == _carried_risks(tested.report)
    if tested.run.db == "discourse":
        assert _carried_risks(synthesis) == []
    else:
        [risk] = _carried_risks(synthesis)
        assert "held back after the test on a test Aurora" in risk["description"]


def test_after_the_test_a_held_routine_is_listed_once(tested: Tested) -> None:
    line = tested.routines
    held = [(r["routine_id"], r["reason"]) for r in line["held_back_routines"]]
    assert (line["refused"], line["refused_routines"]) == (0, [])
    if tested.run.db == "discourse":
        assert (line["in_script"], held) == (13, [])
    else:
        assert held == [("wordpress.wp_refresh_comment_count", "not_proved")]
        assert line["in_script"] == 1 and line["held_back"] == 1
