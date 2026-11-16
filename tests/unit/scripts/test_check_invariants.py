# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#2143: ``scripts/check_invariants.py``, the ADR invariants as queries, on saved runs.

Each invariant passes on the saved real-model runs (``tests/fixtures/real_runs``), on a
design split of one, and on a job an older release wrote (``tests/fixtures/old_jobs``).
Each invariant is then broken on its own copy of a saved run, and the script must name
that check as failing. The first four breaks are the ones the 2026-11-11 claims audit
found went unnoticed: a co-dependency group on two engines, an engine change with no
ledger entry, a facts count off by one and a design naming another engine's statement.

The script runs as a subprocess and its JSON line is read, as ``ci/e2e-llm.sh`` reads
it; nothing here imports the query layer.
"""

from __future__ import annotations

import copy
import gzip
import json
import shutil
import subprocess  # nosec B404 -- runs this repo's own script with fixed argv
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ci.llm import replay

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "check_invariants.py"
REAL_RUNS = REPO / "tests" / "fixtures" / "real_runs"
OLD_JOB = REPO / "tests" / "fixtures" / "old_jobs" / "v0.4.0-beta.2" / "wordpress" / "aa4e840a"
SPLIT_ENGINES = ("dynamodb", "documentdb", "opensearch", "elasticache")

CHECKS = (
    "co_dependency_group_one_engine",
    "write_group_one_engine",
    "routine_unit_one_engine",
    "one_writer_per_table",
    "cache_never_owns",
    "no_unrecorded_unit_cut",
    "no_write_outside_owner_part",
    "ledger_explains_every_engine",
    "facts_recount",
    "design_names_only_own_statements",
    "design_covers_its_statements",
)
# No saved run has a routine unit; a job folder without a design split has no part to
# check; the old job designed nothing (--llm-mode none).
SPLIT_ONLY = {"no_unrecorded_unit_cut", "no_write_outside_owner_part"}
NOTHING_TO_CHECK = {
    "wordpress-both": {"routine_unit_one_engine", *SPLIT_ONLY},
    "discourse-both": {"routine_unit_one_engine", *SPLIT_ONLY},
    "wordpress-both-split": {"routine_unit_one_engine"},
    "old-v0.4.0-beta.2": {
        "routine_unit_one_engine",
        *SPLIT_ONLY,
        "design_names_only_own_statements",
        "design_covers_its_statements",
    },
}

_JOBS: dict[str, Path] = {}
_VERDICTS: dict[str, dict[str, Any]] = {}


def check(job_dir: Path) -> tuple[int, dict[str, Any]]:
    """The script's exit code and its one JSON line."""
    proc = subprocess.run(  # nosec B603 -- fixed interpreter and this repo's script
        [sys.executable, str(SCRIPT), str(job_dir)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=300,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert lines, f"no output (exit {proc.returncode}): {proc.stderr[-2000:]}"
    return proc.returncode, json.loads(lines[-1])


def _lay_out(name: str, root: Path) -> Path:
    """A saved real run laid out as a job folder: its job, compressed files unpacked,
    and its report.json in the synthesis version it came from (as ``replay prepare``
    lays it out, without rendering)."""
    fixture = REAL_RUNS / name
    run = json.loads((fixture / "run.json").read_text(encoding="utf-8"))
    job = root / str(run["db"]) / str(run["job_id"])
    replay._unpack_tree(fixture / "job", job)
    synthesis = job / "synthesis" / f"v{int(run['synthesis_version'])}"
    synthesis.mkdir(parents=True)
    (synthesis / "report.json").write_text(replay.fixture_text(fixture, "report.json"))
    return job


def _split(job: Path) -> Path:
    """A copy of ``job`` with every engine of every assignment version split into design
    parts (``--split`` needs no model)."""
    from src.agents.schema_design.handler import run_schema_split
    from src.storage.local_store import LocalArtifactStore

    root = job.parents[2] / "split"
    copy_dir = root / job.parent.name / job.name
    shutil.copytree(job, copy_dir)
    store = LocalArtifactStore(str(root))
    for version in sorted(int(p.name[1:]) for p in job.glob("assignment/v*")):
        for engine in SPLIT_ENGINES:
            run_schema_split(job.name, job.parent.name, engine, store, assignment_version=version)
    return copy_dir


def saved_job(name: str, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The saved job ``name``, laid out once per test process. Never edit it: copy it."""
    if name not in _JOBS:
        root = tmp_path_factory.mktemp(f"saved-{name}")
        if name == "old-v0.4.0-beta.2":
            _JOBS[name] = root / OLD_JOB.parent.name / OLD_JOB.name
            shutil.copytree(OLD_JOB, _JOBS[name])
        elif name == "wordpress-both-split":
            _JOBS[name] = _split(saved_job("wordpress-both", tmp_path_factory))
        else:
            _JOBS[name] = _lay_out(name, root / "artifacts")
    return _JOBS[name]


def verdict(name: str, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    if name not in _VERDICTS:
        _, _VERDICTS[name] = check(saved_job(name, tmp_path_factory))
    return _VERDICTS[name]


def _cases() -> list[Any]:
    cases = []
    for name in NOTHING_TO_CHECK:
        for check_name in CHECKS:
            marks: list[Any] = []
            if (name, check_name) == ("discourse-both", "design_covers_its_statements"):
                # The saved discourse run predates #2153's coverage check: its DynamoDB
                # design (v3 and v4) leaves 4582415969089977699, the parenthesized UNION
                # #2159 typed OTHER, unnamed, and v3 also 8521344751852874806, which it
                # lists only as unsupported. #2378 re-records the fixture; this strict
                # mark then fails until it is removed.
                marks.append(
                    pytest.mark.xfail(
                        strict=True,
                        reason="#2378: the saved discourse design predates #2153's coverage check",
                    )
                )
            cases.append(pytest.param(name, check_name, marks=marks, id=f"{name}-{check_name}"))
    return cases


@pytest.mark.parametrize(("name", "check_name"), _cases())
def test_every_invariant_holds_on_the_saved_runs(
    name: str, check_name: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Every check passes on every saved run, or (for the checks listed for it) has
    nothing to check there; none is skipped by mistake."""
    result = verdict(name, tmp_path_factory)
    status = result["checks"][check_name]
    expected = "na" if check_name in NOTHING_TO_CHECK[name] else "pass"
    assert status["status"] == expected, (status, result["failures"].get(check_name))
    if expected == "pass":
        assert status["checked"] > 0 and status["failed"] == 0


def test_the_script_runs_every_check_and_reports_a_pass_as_exit_0(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    code, result = check(saved_job("wordpress-both", tmp_path_factory))
    assert code == 0 and result["status"] == "pass"
    assert tuple(result["checks"]) == CHECKS
    assert result["failures"] == {}
    assert result["job"] == "wordpress/099e6873"


# ---------------------------------------------------------------------------
# Broken copies: each invariant broken once, and the script names it
# ---------------------------------------------------------------------------


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _copy(name: str, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    src = saved_job(name, tmp_path_factory)
    dest = tmp_path / "artifacts" / src.parent.name / src.name
    shutil.copytree(src, dest)
    return dest


def _latest(job: Path, sub: str = "assignment", filename: str = "assignment.json") -> Path:
    return max((job / sub).glob(f"v*/{filename}"), key=lambda p: int(p.parent.name[1:]))


def _move(a: dict, qid: str, to: str, *, record: bool = True) -> None:
    """Set ``qid``'s engine to ``to``, recording a Move for it unless ``record`` is false."""
    qa = next(q for q in a["query_assignments"] if q["query_id"] == qid)
    if record:
        move = copy.deepcopy(a["moves"][0])
        move.update(
            seq=max(m["seq"] for m in a["moves"]) + 1,
            query_id=qid,
            from_engine=qa["assigned_engine"],
            to_engine=to,
            tied_engines=[],
        )
        a["moves"].append(move)
    qa["assigned_engine"] = to
    qa["tied_engines"] = []


def _engine_of(a: dict) -> dict[str, str]:
    return {q["query_id"]: q["assigned_engine"] for q in a["query_assignments"]}


def _other(engine: str) -> str:
    return "dynamodb" if engine != "dynamodb" else "aurora_mysql"


def _fails(job: Path, check_name: str) -> dict[str, Any]:
    code, result = check(job)
    assert code == 1 and result["status"] == "fail", result
    assert result["checks"][check_name]["status"] == "fail", result["checks"]
    assert result["failures"][check_name]
    return result


def _break_assignment(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, edit: Callable[[dict], None]
) -> Path:
    job = _copy("wordpress-both", tmp_path, tmp_path_factory)
    path = _latest(job)
    a = _read(path)
    edit(a)
    _write(path, a)
    return job


def test_a_co_dependency_group_on_two_engines_fails(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Audit break 1: one member of a co-dependency group moved (with its Move) to
    another engine."""

    def edit(a: dict) -> None:
        group = next(g for g in a["co_dependency_groups"] if len(g) >= 2)
        _move(a, group[0], _other(_engine_of(a)[group[0]]))

    result = _fails(
        _break_assignment(tmp_path, tmp_path_factory, edit), "co_dependency_group_one_engine"
    )
    assert (
        "co-dependency group 0 is on aurora_mysql, dynamodb"
        in result["failures"]["co_dependency_group_one_engine"][0]
    )
    assert result["checks"]["ledger_explains_every_engine"]["status"] == "pass"


def test_an_engine_change_with_no_ledger_entry_fails(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Audit break 2: a query's engine changed and no Move recorded for it."""
    changed: list[str] = []

    def edit(a: dict) -> None:
        grouped = {q for g in a["co_dependency_groups"] for q in g}
        grouped |= {q for m in a["write_groups"].values() for q in m}
        qid = next(q for q in _engine_of(a) if q not in grouped)
        changed.append(qid)
        _move(a, qid, _other(_engine_of(a)[qid]), record=False)

    result = _fails(
        _break_assignment(tmp_path, tmp_path_factory, edit), "ledger_explains_every_engine"
    )
    (line,) = result["failures"]["ledger_explains_every_engine"]
    assert line.startswith(f"v2: query {changed[0]} is on ")


@pytest.mark.parametrize(
    "figure",
    [
        "query_total_count",
        "in_scope_query_count",
        "engine_count",
        "migration_wave_count",
        "moving_query_count",
        "engine_cost_total_usd",
        "workload_total_percent",
        "tied_query_count",
        "open_risk_count",
        "critical_high_risk_count",
        "pending_choice_count",
        "auto_decided_choice_count",
        "post_schema_reroute_count",
        "no_engine_in_scope_query_count",
    ],
)
def test_a_facts_figure_off_by_one_fails(
    figure: str, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Audit break 3: one report.json facts figure one more than what it counts."""
    job = _copy("wordpress-both", tmp_path, tmp_path_factory)
    path = _latest(job, "synthesis", "report.json")
    report = _read(path)
    report["facts"][figure] += 1
    _write(path, report)
    result = _fails(job, "facts_recount")
    assert all(f"facts.{figure}" in line for line in result["failures"]["facts_recount"])


def test_a_ranking_count_that_disagrees_with_the_assignment_fails(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    job = _copy("wordpress-both", tmp_path, tmp_path_factory)
    path = _latest(job, "synthesis", "report.json")
    report = _read(path)
    row = next(r for r in report["ranking"] if r["target"] == "dynamodb")
    row["assigned_queries_in_scope"] += 1
    _write(path, report)
    result = _fails(job, "facts_recount")
    assert (
        "facts.ranking.dynamodb.assigned_queries_in_scope" in result["failures"]["facts_recount"][0]
    )


def test_a_design_naming_another_engines_statement_fails(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Audit break 4: the DynamoDB design names a statement Aurora MySQL owns."""
    job = _copy("wordpress-both", tmp_path, tmp_path_factory)
    a = _read(_latest(job))
    foreign = next(q for q, e in _engine_of(a).items() if e == "aurora_mysql")
    path = _latest(job, "schema-dynamodb", "schema_output.json")
    design = _read(path)
    design["access_patterns"][0]["query_ids"].append(foreign)
    _write(path, design)
    result = _fails(job, "design_names_only_own_statements")
    (line,) = result["failures"]["design_names_only_own_statements"]
    assert line == f"schema-dynamodb/v2: names query {foreign}, assigned to aurora_mysql"


def test_a_design_that_leaves_a_statement_unnamed_fails(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    job = _copy("wordpress-both", tmp_path, tmp_path_factory)
    path = _latest(job, "schema-dynamodb", "schema_output.json")
    design = _read(path)
    pattern = next(ap for ap in design["access_patterns"] if len(ap["query_ids"]) == 1)
    dropped = pattern["query_ids"].pop()
    _write(path, design)
    result = _fails(job, "design_covers_its_statements")
    assert result["failures"]["design_covers_its_statements"] == [
        f"schema-dynamodb/v2: query {dropped} is assigned to dynamodb and no access pattern names it"
    ]


def test_a_write_group_on_two_engines_fails(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    def edit(a: dict) -> None:
        members = a["write_groups"]["wordpress.wp_posts"]
        _move(a, members[0], "aurora_mysql")

    result = _fails(_break_assignment(tmp_path, tmp_path_factory, edit), "write_group_one_engine")
    assert (
        "write group of table wordpress.wp_posts is on aurora_mysql, dynamodb"
        in result["failures"]["write_group_one_engine"][0]
    )
    assert result["checks"]["one_writer_per_table"]["status"] == "fail"


def test_a_split_write_the_saved_write_groups_miss_fails(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """One writer per table, recounted from the collected statements: a table's writes
    split across engines while the saved write groups no longer list the table."""

    def edit(a: dict) -> None:
        members = a["write_groups"].pop("wordpress.wp_posts")
        _move(a, members[0], "aurora_mysql")

    result = _fails(_break_assignment(tmp_path, tmp_path_factory, edit), "one_writer_per_table")
    assert result["checks"]["write_group_one_engine"]["status"] == "pass"
    assert any(
        "table wordpress.wp_posts is written on aurora_mysql, dynamodb" in line
        for line in result["failures"]["one_writer_per_table"]
    )


def test_a_table_saved_with_two_write_engines_fails(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    def edit(a: dict) -> None:
        row = next(t for t in a["table_assignments"] if t.get("write_engines"))
        row["write_engines"] = sorted({*row["write_engines"], "aurora_mysql", "dynamodb"})

    result = _fails(_break_assignment(tmp_path, tmp_path_factory, edit), "one_writer_per_table")
    assert "table_assignments gives" in result["failures"]["one_writer_per_table"][0]


def test_a_routine_unit_on_two_engines_fails(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """No saved run has a routine unit, so the break adds one whose two statements are
    on two engines. Both versions are stamped 1.26 (whole units): in the saved 1.24 run
    the two statements already sat on two engines in v1, which is a carried split, a
    warning (``test_a_split_carried_from_before_whole_units_is_a_warning``)."""

    def edit(a: dict) -> None:
        a["contract_version"] = "1.26"
        engine = _engine_of(a)
        first = next(q for q, e in engine.items() if e == "aurora_mysql")
        second = next(q for q, e in engine.items() if e == "dynamodb")
        a["routine_units"] = [
            {
                "routine_id": "wordpress.audit_proc",
                "kind": "procedure",
                "query_ids": [first, second],
                "fired_by": [],
            }
        ]

    job = _break_assignment(tmp_path, tmp_path_factory, edit)
    v1 = job / "assignment" / "v1" / "assignment.json"
    _write(v1, {**_read(v1), "contract_version": "1.26"})
    result = _fails(job, "routine_unit_one_engine")
    assert result["failures"]["routine_unit_one_engine"] == [
        "v2: routine unit wordpress.audit_proc is on aurora_mysql, dynamodb"
    ]


def test_a_cache_owning_a_query_fails(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    moved: list[str] = []

    def edit(a: dict) -> None:
        qid = next(q for q, e in _engine_of(a).items() if e == "dynamodb")
        moved.append(qid)
        _move(a, qid, "elasticache")

    result = _fails(_break_assignment(tmp_path, tmp_path_factory, edit), "cache_never_owns")
    assert f"v2: query {moved[0]} is owned by elasticache" in result["failures"]["cache_never_owns"]


def test_a_query_tied_to_a_cache_fails(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    def edit(a: dict) -> None:
        qa = a["query_assignments"][0]
        qa["tied_engines"] = sorted([qa["assigned_engine"], "elasticache"])
        move = copy.deepcopy(a["moves"][0])
        move.update(
            seq=max(m["seq"] for m in a["moves"]) + 1,
            query_id=qa["query_id"],
            to_engine=qa["assigned_engine"],
            tied_engines=qa["tied_engines"],
        )
        a["moves"].append(move)

    result = _fails(_break_assignment(tmp_path, tmp_path_factory, edit), "cache_never_owns")
    assert "tied to a cache" in result["failures"]["cache_never_owns"][0]
    assert result["checks"]["ledger_explains_every_engine"]["status"] == "pass"


def test_an_unrecorded_unit_cut_fails(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """One statement of a unit that sits in one DynamoDB part moved to another part's
    input, with no ``unit_cuts`` entry (the #2141 break)."""
    from tests.e2e.test_design_split_units import _move_entry

    job = _copy("wordpress-both-split", tmp_path, tmp_path_factory)
    _move_entry(job, 2, "dynamodb")
    result = _fails(job, "no_unrecorded_unit_cut")
    assert any(
        line.startswith("schema-dynamodb/v2: unit:") and "does not record the cut" in line
        for line in result["failures"]["no_unrecorded_unit_cut"]
    )


def test_a_cut_recorded_with_other_parts_fails(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    from tests.e2e.test_design_split_units import _move_entry

    job = _copy("wordpress-both-split", tmp_path, tmp_path_factory)
    moved = _move_entry(job, 2, "dynamodb")
    manifest_path = job / "schema-dynamodb" / "v2" / "groups_manifest.json"
    manifest = _read(manifest_path)
    _, broken = check(job)
    first = next(
        line for line in broken["failures"]["no_unrecorded_unit_cut"] if "does not record" in line
    )
    unit_id = first.split(": ", 1)[1].split(" (", 1)[0]
    manifest["unit_cuts"] = [
        {
            "unit_id": unit_id,
            "kind": "write_group",
            "query_ids": [moved],
            "parts": [0],
            "reason": "over_part_size",
        }
    ]
    _write(manifest_path, manifest)
    result = _fails(job, "no_unrecorded_unit_cut")
    assert any(
        unit_id in line and "unit_cuts records 0" in line
        for line in result["failures"]["no_unrecorded_unit_cut"]
    )


def test_a_table_written_outside_its_owner_part_fails(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The tables part 0 owns handed to part 1 in the manifest: part 0's writes of them
    are then owned in one part and written in another."""
    job = _copy("wordpress-both-split", tmp_path, tmp_path_factory)
    path = job / "schema-dynamodb" / "v2" / "groups_manifest.json"
    manifest = _read(path)
    groups = sorted(manifest["groups"], key=lambda g: g["group_index"])
    groups[1]["primary_tables"] = [*groups[1]["primary_tables"], *groups[0]["primary_tables"]]
    groups[0]["primary_tables"] = []
    _write(path, manifest)
    result = _fails(job, "no_write_outside_owner_part")
    assert result["checks"]["no_unrecorded_unit_cut"]["status"] == "pass"
    assert all(
        line.startswith("schema-dynamodb/v2: ")
        and f"part {groups[0]['group_index']}, owned by part" in line
        for line in result["failures"]["no_write_outside_owner_part"]
    )


# ---------------------------------------------------------------------------
# Older artifacts load
# ---------------------------------------------------------------------------


def test_older_artifact_shapes_load_and_say_what_they_lack(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """An assignment saved before the move ledger, write groups and routine units, with
    engine ids spelt the old way, and files kept compressed, still load and pass: the
    ledger has nothing to check on the version without moves, and ``legacy`` says so."""
    job = _copy("wordpress-both", tmp_path, tmp_path_factory)
    path = _latest(job)
    a = _read(path)
    # A file from before contract versions (review of #2370): with a contract_version,
    # a missing ``moves`` is a break, not the legacy shape.
    for key in ("moves", "write_groups", "routine_units", "contract_version"):
        a.pop(key, None)
    for qa in a["query_assignments"]:
        qa["assigned_engine"] = {"aurora_mysql": "Aurora MySQL", "dynamodb": "dynamodb"}.get(
            qa["assigned_engine"], qa["assigned_engine"]
        )
    _write(path, a)
    report = _latest(job, "synthesis", "report.json")
    report.with_name("report.json.gz").write_bytes(gzip.compress(report.read_bytes()))
    report.unlink()
    code, result = check(job)
    assert code == 0, result
    assert result["checks"]["facts_recount"]["status"] == "pass"
    assert result["checks"]["ledger_explains_every_engine"]["checked"] == len(
        _read(job / "assignment" / "v1" / "assignment.json")["query_assignments"]
    )
    assert "assignment v2: no moves (saved before the move ledger)" in result["legacy"]
    assert "assignment v2: no write_groups (saved before write groups)" in result["legacy"]


def test_a_folder_with_no_assignment_is_an_error(tmp_path: Path) -> None:
    code, result = check(tmp_path)
    assert code == 2 and result["status"] == "error"
    assert "no assignment" in result["message"]


# ---------------------------------------------------------------------------
# Review of #2370
# ---------------------------------------------------------------------------


def _ungrouped(a: dict, engine: str) -> str:
    grouped = {q for g in a["co_dependency_groups"] for q in g}
    grouped |= {q for m in (a.get("write_groups") or {}).values() for q in m}
    return next(q for q, e in _engine_of(a).items() if e == engine and q not in grouped)


def test_a_versioned_assignment_with_no_moves_is_a_ledger_break(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The reviewer's break: discourse v4 (contract 1.26) with its ``moves`` dropped and
    one statement moved from DynamoDB to Aurora PostgreSQL. Only a file saved before the
    move ledger (no contract version, or one below 1.15) may lack ``moves``."""
    job = _copy("discourse-both", tmp_path, tmp_path_factory)
    path = job / "assignment" / "v4" / "assignment.json"
    a = _read(path)
    assert a["contract_version"] >= "1.15"
    a.pop("moves")
    qid = _ungrouped(a, "dynamodb")
    _move(a, qid, "aurora_postgresql", record=False)
    _write(path, a)
    result = _fails(job, "ledger_explains_every_engine")
    lines = result["failures"]["ledger_explains_every_engine"]
    assert f"v4: contract {a['contract_version']} records no moves" in lines
    assert any(line.startswith(f"v4: query {qid} is on aurora_postgresql") for line in lines)
    assert not any(line.startswith("assignment v4: no moves") for line in result["legacy"])


def test_a_versioned_assignment_with_no_moves_names_the_missing_ledger(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Without its moves, v2 (contract 1.24) is read as recording none: the check names
    the missing ledger, and v2's reality-check moves then replay to nothing."""

    def edit(a: dict) -> None:
        a.pop("moves")

    result = _fails(
        _break_assignment(tmp_path, tmp_path_factory, edit), "ledger_explains_every_engine"
    )
    lines = result["failures"]["ledger_explains_every_engine"]
    assert lines[0] == "v2: contract 1.24 records no moves"
    assert not any("no moves (saved before the move ledger)" in n for n in result["legacy"])


def _co_group_members(a: dict) -> list[str]:
    return next(g for g in a["co_dependency_groups"] if len(g) >= 2)


def test_a_customers_own_split_of_a_co_dependency_group_stands(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """ADR-031 Amendment 21 as the assignment validator applies it: two members the
    customer picked onto two engines are the customer's split, which stands."""

    def edit(a: dict) -> None:
        first, second = _co_group_members(a)[:2]
        _move(a, first, "aurora_mysql")
        _move(a, second, "dynamodb")
        for qa in a["query_assignments"]:
            if qa["query_id"] in (first, second):
                qa["customer_override"] = True

    code, result = check(_break_assignment(tmp_path, tmp_path_factory, edit))
    assert result["checks"]["co_dependency_group_one_engine"]["status"] == "pass", result
    assert result["checks"]["ledger_explains_every_engine"]["status"] == "pass"


def test_a_split_the_customer_picked_only_one_side_of_fails(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    def edit(a: dict) -> None:
        first = _co_group_members(a)[0]
        _move(a, first, "dynamodb")
        for qa in a["query_assignments"]:
            if qa["query_id"] == first:
                qa["customer_override"] = True

    _fails(_break_assignment(tmp_path, tmp_path_factory, edit), "co_dependency_group_one_engine")


def test_a_customer_pick_never_excuses_a_split_write_group(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A table's writes never split, picked or not (ADR-031 Amendment 9)."""

    def edit(a: dict) -> None:
        first, second = a["write_groups"]["wordpress.wp_posts"][:2]
        _move(a, first, "aurora_mysql")
        for qa in a["query_assignments"]:
            if qa["query_id"] in (first, second):
                qa["customer_override"] = True

    _fails(_break_assignment(tmp_path, tmp_path_factory, edit), "write_group_one_engine")


def test_an_out_of_scope_member_on_another_engine_is_not_a_split(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    def edit(a: dict) -> None:
        first = _co_group_members(a)[0]
        _move(a, first, "dynamodb")
        for qa in a["query_assignments"]:
            if qa["query_id"] == first:
                qa["in_scope"] = False

    code, result = check(_break_assignment(tmp_path, tmp_path_factory, edit))
    assert result["checks"]["co_dependency_group_one_engine"]["status"] == "pass", result


def _split_in_both_versions(job: Path, contract: str | None) -> str:
    """Split co-dependency group 0 in v1 and carry it unchanged into v2."""
    v1, v2 = (job / "assignment" / f"v{n}" / "assignment.json" for n in (1, 2))
    a1, a2 = _read(v1), _read(v2)
    qid = _co_group_members(a1)[0]
    for a in (a1, a2):
        if contract is not None:
            a["contract_version"] = contract
        for qa in a["query_assignments"]:
            if qa["query_id"] == qid:
                qa["assigned_engine"] = "dynamodb"
                qa["tied_engines"] = []
    last = max((m for m in a1["moves"] if m["query_id"] == qid), key=lambda m: m["seq"])
    last.update(to_engine="dynamodb", tied_engines=[])
    a2["moves"] = [m for m in a2["moves"] if m["query_id"] != qid]
    _write(v1, a1)
    _write(v2, a2)
    return qid


def test_a_split_carried_from_before_whole_units_is_a_warning(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A split some version saved before contract 1.26 (whole placement units) already
    had, kept on the engines the members already had, is a warning, as the validator's
    legacy baseline makes it."""
    job = _copy("wordpress-both", tmp_path, tmp_path_factory)
    _split_in_both_versions(job, None)  # the saved run's own 1.24
    code, result = check(job)
    status = result["checks"]["co_dependency_group_one_engine"]
    assert status["status"] == "pass" and status["failed"] == 0, result
    assert status["warned"] == 2
    assert all(
        "saved before whole units" in w
        for w in result["warnings"]["co_dependency_group_one_engine"]
    )


def test_the_same_split_on_versions_after_whole_units_fails(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    job = _copy("wordpress-both", tmp_path, tmp_path_factory)
    _split_in_both_versions(job, "1.26")
    result = _fails(job, "co_dependency_group_one_engine")
    assert len(result["failures"]["co_dependency_group_one_engine"]) == 2


def _documentdb_v4(job: Path) -> tuple[Path, dict]:
    path = job / "schema-documentdb" / "v4" / "schema_output.json"
    return path, _read(path)


def test_a_documentdb_statement_named_only_by_an_index_is_covered(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The reviewer's break: a DocumentDB statement named only by a collection's index,
    which the product's coverage check (``scope.covered_query_ids``) counts as designed."""
    job = _copy("discourse-both", tmp_path, tmp_path_factory)
    path, design = _documentdb_v4(job)
    qid = "7025230785254451127"
    for ap in design["access_patterns"]:
        ap["source_query_ids"] = [q for q in ap.get("source_query_ids") or [] if q != qid]
    collection = next(c for c in design["collections"] if c.get("indexes"))
    collection["indexes"][0].setdefault("source_query_ids", []).append(qid)
    _write(path, design)
    _, result = check(job)
    lines = result["failures"].get("design_covers_its_statements") or []
    assert not any("schema-documentdb" in line for line in lines), lines


def test_a_documentdb_index_naming_another_engines_statement_fails(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    job = _copy("discourse-both", tmp_path, tmp_path_factory)
    a = _read(job / "assignment" / "v4" / "assignment.json")
    foreign = next(q for q, e in _engine_of(a).items() if e == "aurora_postgresql")
    path, design = _documentdb_v4(job)
    collection = next(c for c in design["collections"] if c.get("indexes"))
    collection["indexes"][0].setdefault("source_query_ids", []).append(foreign)
    _write(path, design)
    result = _fails(job, "design_names_only_own_statements")
    assert result["failures"]["design_names_only_own_statements"] == [
        f"schema-documentdb/v4: names query {foreign}, assigned to aurora_postgresql"
    ]


@pytest.mark.parametrize("name", ["wordpress-both", "discourse-both"])
def test_the_loaded_design_names_are_what_the_products_coverage_reads(
    name: str, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The statements the checks read as designed are ``scope.covered_query_ids``'s, for
    every saved design: the check never disagrees with what finalize accepts."""
    import duckdb

    from src.agents.schema_design.scope import covered_query_ids

    job = saved_job(name, tmp_path_factory)
    database = tmp_path / "job.duckdb"
    proc = subprocess.run(  # nosec B603 -- fixed interpreter and this repo's script
        [sys.executable, str(SCRIPT), str(job), "--duckdb", str(database)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert database.is_file(), proc.stdout + proc.stderr
    con = duckdb.connect(str(database), read_only=True)
    try:
        loaded: dict[tuple[str, int], set[str]] = {}
        for engine, version, qid in con.execute(
            "SELECT engine, version, query_id FROM design_names"
        ).fetchall():
            loaded.setdefault((engine, version), set()).add(qid)
        designs = con.execute("SELECT engine, version FROM designs").fetchall()
    finally:
        con.close()
    assert designs
    for engine, version in designs:
        design = _read(job / f"schema-{engine}" / f"v{version}" / "schema_output.json")
        assert loaded.get((engine, version), set()) == covered_query_ids(engine, design), (
            engine,
            version,
        )


@pytest.mark.parametrize("edit", ["null", "missing"])
def test_a_facts_figure_that_is_null_or_missing_fails(
    edit: str, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    job = _copy("wordpress-both", tmp_path, tmp_path_factory)
    path = _latest(job, "synthesis", "report.json")
    report = _read(path)
    if edit == "null":
        report["facts"]["in_scope_query_count"] = None
    else:
        del report["facts"]["engine_count"]
    _write(path, report)
    result = _fails(job, "facts_recount")
    figure = "in_scope_query_count" if edit == "null" else "engine_count"
    assert f"synthesis v2: facts.{figure} is missing" in result["failures"]["facts_recount"]
