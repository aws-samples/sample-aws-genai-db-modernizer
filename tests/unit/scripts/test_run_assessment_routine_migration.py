# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``run_assessment.py`` writes ``routines/v<N>/routine_migration.json`` (#1934).

Each run here is the real script on a raw collection in ``--llm-mode none`` (no model
call), so every assertion reads what a customer's run writes: one row per routine with
its id, kind, dependencies, deploy order, status, rewrites and check results, and never
a routine's text. The external-mode path (``--resume-reality-check``) is run in process
with the reality check's finalize stubbed: the phase runs after it, on the version the
reality check kept.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 -- runs this repo's own run_assessment.py with fixed argv
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts import run_assessment
from src.storage.local_store import LocalArtifactStore
from tests.shared_runs import cached_run, copy_run
from tests.unit.agents.routine_collections import SEED, pg_collection

REPO = Path(__file__).resolve().parents[3]
# A name only the routine bodies hold: it may be in the redacted collection (bodies keep
# their identifiers) and the routine sources, and nowhere else in the job.
MARKER = "zz_body_marker_1934"
SCRIPT = REPO / "scripts" / "run_assessment.py"


def _pg_function(name: str, args: str, body: str, **row: Any) -> dict[str, Any]:
    language = row.pop("language", "PLPGSQL")
    create = (
        f"CREATE OR REPLACE FUNCTION public.{name}({args})\n RETURNS integer\n"
        f" LANGUAGE {language.lower()}\nAS $function${body}$function$\n"
    )
    return {
        "routine_name": name, "schema_name": "public", "specific_name": row.pop("specific"),
        "routine_type": "FUNCTION", "return_type": "integer", "language": language,
        "owner": "app_owner", "security_definer": row.pop("security_definer", False),
        "depends_on": row.pop("depends_on", []), "body_chars": len(body),
        "body_status": "collected", "definition": create, **row,
    }  # fmt: skip


def _pg_collection() -> dict[str, Any]:
    raw = pg_collection()
    raw["procedures"] = [
        # Overloads: one name, two argument types.
        _pg_function("set_status", "p_id integer",
                     f"BEGIN PERFORM '{SEED}'; PERFORM {MARKER}(); RETURN p_id; END;", specific="set_status_16390",
                     security_definer=True),
        _pg_function("set_status", "p_id text", "BEGIN RETURN 1; END;",
                     specific="set_status_16391"),
        # A cycle: each names the other.
        _pg_function("f_a", "", "SELECT 1", specific="f_a_16400", language="SQL",
                     depends_on=[{"kind": "function", "name": "public.f_b()"}]),
        _pg_function("f_b", "", "SELECT 1", specific="f_b_16401", language="SQL",
                     depends_on=[{"kind": "function", "name": "public.f_a()"}]),
        # A table the move does not have.
        _pg_function("archive_it", "", "SELECT 1", specific="archive_it_16402", language="SQL",
                     depends_on=[{"kind": "table", "name": "public.archive"}]),
        # A language no Aurora PostgreSQL line runs.
        _pg_function("py_fn", "", "return 1", specific="py_fn_16403", language="PLPYTHONU"),
        # The trigger's function.
        _pg_function("audit_orders", "", "BEGIN RETURN 1; END;", specific="audit_orders_16392"),
    ]  # fmt: skip
    return raw


def _mysql_procedure(name: str, kind: str, body: str | None, **row: Any) -> dict[str, Any]:
    return {
        "routine_name": name, "schema_name": "shop", "specific_name": name, "routine_type": kind,
        "return_type": "int" if kind == "FUNCTION" else "", "definer": "app@%",
        "security_type": "DEFINER", "is_deterministic": "NO", "sql_data_access": "CONTAINS SQL",
        "routine_comment": "", "external_language": "SQL", "sql_mode": "STRICT_TRANS_TABLES",
        "character_set_client": "utf8mb4", "collation_connection": "utf8mb4_0900_ai_ci",
        "database_collation": "utf8mb4_0900_ai_ci", "body_chars": len(body) if body else None,
        "body_status": row.pop("body_status", "collected"), "definition": body, **row,
    }  # fmt: skip


def _mysql_collection() -> dict[str, Any]:
    return {
        "metadata": {
            "database_name": "shop", "version": "8.0.36",
            "settings": {"sql_mode": "STRICT_TRANS_TABLES"},
            "routine_capture": {"include_bodies": True, "routines_counted": 7,
                                "parameters_counted": 1, "privileges_not_seen": []},
        },  # fmt: skip
        "tables": [{"table_name": "orders", "row_count": 10}],
        "columns": [{"table_name": "orders", "column_name": "id", "data_type": "int",
                     "is_nullable": "NO", "ordinal_position": 1}],  # fmt: skip
        "queries": [{"digest": "d1", "query_text": "SELECT id FROM orders WHERE id = ?",
                     "execution_count": 20, "total_time_ms": 4}],  # fmt: skip
        "procedures": [
            # A named DEFINER the target lacks; a procedure and a function of one name.
            _mysql_procedure("place_order", "PROCEDURE", f"BEGIN\n  SELECT p_id, {MARKER};\nEND"),
            _mysql_procedure("place_order", "FUNCTION", "RETURN 1"),
            # A cycle: each calls the other.
            _mysql_procedure("p1", "PROCEDURE", "BEGIN\n  CALL p2();\nEND"),
            _mysql_procedure("p2", "PROCEDURE", "BEGIN\n  CALL p1();\nEND"),
            # A statement Aurora MySQL refuses.
            _mysql_procedure("set_cap", "PROCEDURE", "BEGIN\n  SET GLOBAL max_connections = 10;\nEND"),
            # A body around a literal: the script's rebuild of it is not exact.
            _mysql_procedure("note", "PROCEDURE", f"BEGIN\n  SELECT '{SEED}';\nEND"),
            # A body the collection user could not read.
            _mysql_procedure("secret_proc", "PROCEDURE", None, body_status="not_readable"),
        ],  # fmt: skip
        "routine_parameters": [
            {"routine_name": "place_order", "schema_name": "shop", "routine_type": "PROCEDURE",
             "ordinal": 1, "mode": "IN", "parameter_name": "p_id", "data_type": "int",
             "dtd_identifier": "int"},
            {"routine_name": "place_order", "schema_name": "shop", "routine_type": "FUNCTION",
             "ordinal": 0, "mode": None, "parameter_name": None, "data_type": "int",
             "dtd_identifier": "int"},
        ],  # fmt: skip
        "triggers": [],
    }


def _no_routines() -> dict[str, Any]:
    raw = _mysql_collection()
    raw["procedures"] = []
    raw.pop("routine_parameters")
    return raw


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    env.pop("MODERNIZER_CI_SANDBOX", None)
    env["AWS_DEFAULT_REGION"] = "us-east-1"
    return env


def _assess(root: Path, collection: dict[str, Any], *args: str) -> None:
    (root / "collection.json").write_text(json.dumps(collection))
    proc = subprocess.run(  # nosec B603 -- fixed interpreter and this repo's script
        [sys.executable, str(SCRIPT), "--file", str(root / "collection.json"), "--db", "shop",
         "--artifact-root", str(root / "a"), *args],
        cwd=root, capture_output=True, text=True, timeout=300, env=_env(),
    )  # fmt: skip
    (root / "proc.json").write_text(
        json.dumps({"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr})
    )


def _job(root: Path) -> tuple[Path, dict[str, Any]]:
    proc = json.loads((root / "proc.json").read_text())
    assert proc["returncode"] == 0, proc["stdout"][-2000:] + proc["stderr"][-2000:]
    (job,) = (root / "a" / "shop").iterdir()
    return job, proc


def _artifact(job: Path) -> dict[str, Any]:
    path = job / "routines" / "v1" / "routine_migration.json"
    assert path.is_file(), f"the run wrote no routine migration: {sorted(job.iterdir())}"
    artifact: dict[str, Any] = json.loads(path.read_text())
    return artifact


def _rows(artifact: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(r["kind"], r["routine_id"]): r for r in artifact["routines"]}


@pytest.fixture(scope="module")
def pg_run(tmp_path_factory):
    root = cached_run(tmp_path_factory, "routine-migration-pg-1934",
                      lambda r: _assess(r, _pg_collection(), "--llm-mode", "none"))  # fmt: skip
    return _job(root)


@pytest.fixture(scope="module")
def mysql_run(tmp_path_factory):
    root = cached_run(tmp_path_factory, "routine-migration-mysql-1934",
                      lambda r: _assess(r, _mysql_collection(), "--llm-mode", "none"))  # fmt: skip
    return _job(root)


@pytest.fixture(scope="module")
def opted_out_run(tmp_path_factory):
    root = cached_run(
        tmp_path_factory, "routine-migration-opted-out-1934",
        lambda r: _assess(r, pg_collection(include_bodies=False), "--llm-mode", "none"),
    )  # fmt: skip
    return _job(root)


# -- what every run writes ------------------------------------------------------------


def test_the_run_writes_one_row_per_routine_keyed_on_the_assignment_version(pg_run):
    job, proc = pg_run
    artifact = _artifact(job)
    assert artifact["assignment_version"] == 1 and artifact["job_id"] == job.name
    assert (artifact["source_engine"], artifact["target_engine"]) == (
        "postgresql",
        "aurora_postgresql",
    )
    assert artifact["total"] == 8 and artifact["routine_sources"] is True
    assert sum(artifact["counts"].values()) == 8 and artifact["counts"]["model_repaired"] == 0
    assert [r["deploy_order"] for r in artifact["routines"]] == list(range(1, 9))
    # stdout keeps only the status lines the commands read.
    phases = [json.loads(x)["phase"] for x in proc["stdout"].splitlines() if '"phase"' in x]
    assert "routine_migration" not in phases
    from src.tools.routines.migration import read_routine_migration

    store = LocalArtifactStore(base_dir=str(job.parents[1]))
    loaded = read_routine_migration(store, "shop", job.name, 1)
    assert loaded is not None and loaded.total == 8


def test_overloaded_functions_get_distinct_ids_by_signature(pg_run):
    rows = _rows(_artifact(pg_run[0]))
    one, two = (
        rows[("function", "public.set_status(integer)")],
        rows[("function", "public.set_status(text)")],
    )
    assert (one["signature"], two["signature"]) == ("integer", "text")
    assert (one["specific_name"], two["specific_name"]) == ("set_status_16390", "set_status_16391")
    assert one["returns"] == "integer"


def test_a_dependency_cycle_needs_attention_and_is_named(pg_run, mysql_run):
    for run, members in (
        (pg_run, [("function", "public.f_a()"), ("function", "public.f_b()")]),
        (mysql_run, [("procedure", "shop.p1"), ("procedure", "shop.p2")]),
    ):
        artifact = _artifact(run[0])
        rows = _rows(artifact)
        assert [[(m["kind"], m["routine_id"]) for m in c] for c in artifact["cycles"]] == [members]
        for key, other in zip(members, reversed(members), strict=True):
            assert rows[key]["status"] == "needs_attention"
            assert rows[key]["reasons"][-2:] == ["dependency_cycle", "depends_on_needs_attention"]
            assert rows[key]["cycle"] == 0
            assert rows[key]["blocked_by"] == [{"kind": other[0], "routine_id": other[1]}]
        orders = sorted(rows[key]["deploy_order"] for key in members)
        assert orders[1] == orders[0] + 1  # placed together


def test_a_routine_on_a_table_the_collection_lacks_needs_attention(pg_run):
    row = _rows(_artifact(pg_run[0]))[("function", "public.archive_it()")]
    assert row["status"] == "needs_attention"
    assert row["reasons"] == [
        "postgresql-routine-dependency-not-moved",
        "depends_on_table_not_collected",
    ]
    assert row["dependencies"] == [
        {"kind": "table", "name": "public.archive", "routine_kind": None, "found": False,
         "routines": []}
    ]  # fmt: skip
    (check,) = [c for c in row["checks"] if c["outcome"] == "fail"]
    assert check["findings"] == ["table public.archive"]


def test_a_routine_whose_check_fails_names_the_rule(pg_run, mysql_run):
    py = _rows(_artifact(pg_run[0]))[("function", "public.py_fn()")]
    assert py["status"] == "needs_attention" and py["reasons"] == ["postgresql-routine-language"]
    cap = _rows(_artifact(mysql_run[0]))[("procedure", "shop.set_cap")]
    assert cap["status"] == "needs_attention" and "mysql-routine-set-global" in cap["reasons"]
    rules = {r["rule_id"]: r for r in _artifact(mysql_run[0])["rules"]}
    assert rules["mysql-routine-set-global"]["rule"] == "check"
    assert rules["mysql-routine-set-global"]["fix_hint"]


def test_a_trigger_is_created_after_its_function(pg_run):
    rows = _rows(_artifact(pg_run[0]))
    trigger = rows[("trigger", "public.orders.tg_orders")]
    function = rows[("function", "public.audit_orders()")]
    assert trigger["deploy_order"] > function["deploy_order"]
    # Step 1 mirrors the whole database: wherever routing sends orders in the end state,
    # the trigger on it is carried; a move off Aurora is recorded, never a reason.
    assert (trigger["status"], trigger["reasons"]) == ("as_is", [])
    for table in trigger["tables_leaving_aurora"]:
        assert table["table"] == "public.orders"
        assert "aurora_postgresql" not in table["engines"]
    assert {"kind": "function", "routine_id": "public.audit_orders()"} in [
        ref for dep in trigger["dependencies"] for ref in dep["routines"]
    ]


def test_a_superuser_check_the_collection_cannot_decide_is_unknown(pg_run):
    row = _rows(_artifact(pg_run[0]))[("function", "public.set_status(integer)")]
    (check,) = [c for c in row["checks"] if c["rule_id"].endswith("superuser-security-definer")]
    assert check["outcome"] == "unknown"


def test_a_mysql_definer_is_rewritten_to_the_target_user(mysql_run):
    artifact = _artifact(mysql_run[0])
    rows = _rows(artifact)
    assert artifact["target_user"] == "admin" and artifact["target_line"] == "8.0"
    for kind in ("procedure", "function"):
        row = rows[(kind, "shop.place_order")]
        assert row["status"] == "rewritten" and row["reasons"] == []
        assert [w["rule_id"] for w in row["rewrites"]] == ["mysql-routine-definer-to-target-user"]
        assert row["notes"] == ["runs_as_target_user"]
    assert rows[("procedure", "shop.place_order")]["signature"] == "IN int"
    assert rows[("function", "shop.place_order")]["returns"] == "int"


def test_text_not_exact_or_not_collected_is_never_carried(mysql_run):
    rows = _rows(_artifact(mysql_run[0]))
    note = rows[("procedure", "shop.note")]
    assert note["source_text"] == "not_exact" and note["status"] == "needs_attention"
    assert note["reasons"][-1] == "source_text_not_exact"
    secret = rows[("procedure", "shop.secret_proc")]
    assert secret["source_text"] == "not_collected" and secret["body_status"] == "not_readable"
    assert secret["reasons"] == ["source_text_not_collected"]


def test_missing_routine_sources_leave_every_routine_without_exact_text(opted_out_run):
    artifact = _artifact(opted_out_run[0])
    assert artifact["routine_sources"] is False and artifact["total"] == 4
    assert artifact["counts"]["needs_attention"] == 4
    for row in artifact["routines"]:
        assert row["source_text"] == "not_collected" and row["body_status"] == "opted_out"
        assert "source_text_not_collected" in row["reasons"]
        assert row["rewrites"] == [] and row["rules_passed"] == 0


@pytest.mark.parametrize("run", ["pg_run", "mysql_run"])
def test_no_routine_text_or_literal_reaches_the_artifact(run, request):
    job, _ = request.getfixturevalue(run)
    text = (job / "routines" / "v1" / "routine_migration.json").read_text()
    assert SEED not in text and "sha256" not in text
    rows = json.dumps(json.loads(text)["routines"])
    for piece in ("CREATE", "$function$", "BEGIN", "SELECT", "DEFINER=", "@@"):
        assert piece not in rows, piece


def test_a_collection_with_no_routines_writes_none(tmp_path):
    _assess(tmp_path, _no_routines(), "--llm-mode", "none")
    job, _ = _job(tmp_path)
    assert (job / "reality-check").is_dir()  # the run got past the reality check
    assert not (job / "routines").exists()
    assert "skipped: no routines" in (job / "_logs" / "run_assessment.log").read_text()


# -- external mode: after the resumed reality check ----------------------------------


def test_the_resumed_reality_check_writes_the_routine_migration(tmp_path, monkeypatch):
    _assess(tmp_path, _pg_collection(), "--llm-mode", "external")
    job, proc = _job(tmp_path)
    assert '"awaiting_llm"' in proc["stdout"] and not (job / "routines").exists()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(run_assessment, "_JOB", {})
    monkeypatch.setattr(run_assessment, "_CONSOLE", None)
    monkeypatch.setattr(run_assessment, "phase_reality_check_finalize", lambda *a, **k: None)
    monkeypatch.setattr(run_assessment, "_surviving_engines", lambda *a: ["aurora_postgresql"])
    monkeypatch.setattr(
        sys, "argv",
        ["run_assessment.py", "--artifact-root", str(tmp_path / "a"), "--job-id", job.name,
         "--db", "shop", "--resume-reality-check", "--verbose"],
    )  # fmt: skip
    run_assessment.main()
    assert _artifact(job)["total"] == 8


# -- a post-schema reroute version --------------------------------------------------


def test_a_post_schema_reroute_version_gets_its_routine_migration(tmp_path, monkeypatch):
    from scripts import run_post_schema_routing
    from src.orchestrator.local_orchestrator import LocalOrchestrator

    _assess(tmp_path, _pg_collection(), "--llm-mode", "none")
    job, _ = _job(tmp_path)
    v1 = job / "assignment" / "v1" / "assignment.json"
    assert _artifact(job)["assignment_version"] == 1

    def reroute(self, job_id, db):  # a pass that writes v2, as the router does
        (job / "assignment" / "v2").mkdir()
        (job / "assignment" / "v2" / "assignment.json").write_text(v1.read_text())
        return {"status": "awaiting_redesign", "assignment_version": 2}

    monkeypatch.setattr(LocalOrchestrator, "run_post_schema_routing_external", reroute)
    monkeypatch.setattr(
        sys, "argv",
        ["run_post_schema_routing.py", "--job-id", job.name, "--db", "shop",
         "--artifact-root", str(tmp_path / "a")],
    )  # fmt: skip
    run_post_schema_routing.main()
    v2 = job / "routines" / "v2" / "routine_migration.json"
    assert v2.is_file(), sorted((job / "routines").iterdir())
    assert json.loads(v2.read_text())["assignment_version"] == 2


# -- what the rest of the job holds --------------------------------------------------


def _job_files(job: Path) -> list[Path]:
    return [p for p in job.rglob("*") if p.is_file() and "routine_sources" not in p.name
            and "routine_sources" not in p.parent.name]  # fmt: skip


# A guard of behaviour main already has (no phase there writes routine text either),
# kept so the routine-migration phase can never start to.
@pytest.mark.passes_on_main
@pytest.mark.parametrize("run", ["pg_run", "mysql_run"])
def test_no_literal_or_body_text_reaches_the_logs_stdout_or_any_other_job_file(run, request):
    job, proc = request.getfixturevalue(run)
    assert MARKER in (job / "collector" / "output.json").read_text()  # the fixture holds it
    for path in _job_files(job):
        text = path.read_bytes().decode("utf-8", errors="ignore")
        assert SEED not in text, path
        rel = path.relative_to(job).as_posix()
        if not (rel == "collector/output.json" or rel.startswith("uploads/")):
            assert MARKER not in text, path
    for stream in (proc["stdout"], proc["stderr"]):
        assert SEED not in stream and MARKER not in stream


def _private_job(run: tuple[Path, dict[str, Any]], tmp_path: Path, monkeypatch) -> Path:
    job, _ = run
    root = copy_run(job.parents[2], tmp_path / "run")
    monkeypatch.chdir(root)
    monkeypatch.setattr(run_assessment, "_JOB", {})
    monkeypatch.setattr(run_assessment, "_CONSOLE", None)
    return root / "a" / "shop" / job.name


def _new_version(job: Path, version: int) -> None:
    """A step that writes an assignment version, as the real one does (a copy of v1)."""
    target = job / "assignment" / f"v{version}"
    target.mkdir()
    (target / "assignment.json").write_text(
        (job / "assignment" / "v1" / "assignment.json").read_text()
    )


def _main(monkeypatch, job: Path, *args: str) -> None:
    monkeypatch.setattr(sys, "argv", ["run_assessment.py", "--artifact-root", str(job.parents[1]),
                                      "--verbose", *args])  # fmt: skip
    run_assessment.main()


@pytest.mark.parametrize("args", [["--decide-for-me"], ["--choose", "orders=dynamodb"]])
def test_a_version_the_engine_choices_write_gets_its_routine_migration(
    pg_run, tmp_path, monkeypatch, args
):
    job = _private_job(pg_run, tmp_path, monkeypatch)
    monkeypatch.setattr(
        run_assessment, "phase_engine_choices", lambda *a, **k: _new_version(job, 2)
    )
    monkeypatch.setattr(run_assessment, "_surviving_engines", lambda *a: ["aurora_postgresql"])
    _main(monkeypatch, job, "--job-id", job.name, "--db", "shop", *args)
    assert _artifact_at(job, 2)["assignment_version"] == 2


def _artifact_at(job: Path, version: int) -> dict[str, Any]:
    path = job / "routines" / f"v{version}" / "routine_migration.json"
    assert path.is_file(), sorted(p.name for p in (job / "routines").iterdir())
    artifact: dict[str, Any] = json.loads(path.read_text())
    return artifact


def test_an_all_run_writes_it_for_the_choices_and_the_post_schema_reroute(tmp_path, monkeypatch):
    # --all -y: the choices write v2, schema design's in-process reroute (Bedrock mode)
    # writes v3; synthesis reads v3, so both get the artifact before it runs.
    (tmp_path / "collection.json").write_text(json.dumps(_pg_collection()))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(run_assessment, "_JOB", {})
    monkeypatch.setattr(run_assessment, "_CONSOLE", None)
    seen: list[list[str]] = []
    root: Path = tmp_path / "a" / "shop"

    def job() -> Path:
        (only,) = root.iterdir()
        return only

    def choices(*a, **k):
        _new_version(job(), 2)

    def schema_design(*a, **k):
        _new_version(job(), 3)
        return {"status": "skipped", "reason": "stub"}

    def synthesis(*a, **k):
        seen.append(sorted(p.name for p in (job() / "routines").iterdir()))

    monkeypatch.setattr(run_assessment, "phase_engine_choices", choices)
    monkeypatch.setattr(run_assessment, "phase_schema_design", schema_design)
    monkeypatch.setattr(run_assessment, "phase_synthesis", synthesis)
    monkeypatch.setattr(run_assessment, "phase_wave_redesign", lambda *a, **k: {})
    monkeypatch.setattr(run_assessment, "phase_report", lambda *a, **k: {"status": "complete"})
    monkeypatch.setattr(sys, "argv", ["run_assessment.py", "--file", str(tmp_path / "collection.json"),
                                      "--db", "shop", "--artifact-root", str(tmp_path / "a"),
                                      "--llm-mode", "none", "--all", "-y", "--verbose"])  # fmt: skip
    run_assessment.main()
    assert seen == [["v1", "v2", "v3"]]


def test_a_phase_failure_is_a_status_line_without_routine_text(
    pg_run, tmp_path, monkeypatch, capsys
):
    from src.tools.routines import migration

    job = _private_job(pg_run, tmp_path, monkeypatch)
    for path in (job / "routines").rglob("*"):
        if path.is_file():
            path.unlink()

    def boom(routine, **_):
        raise ValueError(f"cannot read {routine.create_sql}")

    monkeypatch.setattr(migration, "check_postgresql_routine", boom)
    store = LocalArtifactStore(base_dir=str(job.parents[1]))
    with pytest.raises(SystemExit):
        run_assessment.phase_routine_migration(store, job.name, "shop")
    out = capsys.readouterr()
    (line,) = [json.loads(x) for x in out.out.splitlines() if '"routine_migration"' in x]
    assert line["status"] == "error"
    assert line["message"].startswith(
        "routine migration failed: ValueError while checking function "
    )
    assert SEED not in out.out + out.err and "CREATE" not in out.out + out.err


def test_a_reroute_that_fails_the_phase_prints_its_error_line(
    pg_run, tmp_path, monkeypatch, capsys
):
    from scripts import run_post_schema_routing
    from src.orchestrator.local_orchestrator import LocalOrchestrator
    from src.tools.routines import migration

    job = _private_job(pg_run, tmp_path, monkeypatch)
    monkeypatch.setattr(LocalOrchestrator, "run_post_schema_routing_external",
                        lambda self, j, d: {"status": "complete"})  # fmt: skip

    def boom(*a, **k):
        raise ValueError(f"text {SEED}")

    monkeypatch.setattr(migration, "ensure_routine_migration", boom)
    monkeypatch.setattr(sys, "argv", ["run_post_schema_routing.py", "--job-id", job.name,
                                      "--db", "shop", "--artifact-root", str(job.parents[1])])  # fmt: skip
    with pytest.raises(SystemExit) as stop:
        run_post_schema_routing.main()
    assert stop.value.code == 1
    out = capsys.readouterr()
    (line,) = [json.loads(x) for x in out.out.splitlines() if x.startswith("{")]
    assert line == {"status": "error", "message": "routine migration failed: ValueError"}
    assert SEED not in out.out + out.err and "Traceback" not in out.err
