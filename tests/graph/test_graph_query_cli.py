# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``scripts/graph_query.py``: the graph query catalog from the command line (#307).

``list``, ``params <id>`` and ``run <id> --param k=v --job-id <id>`` each print
one compact JSON line (or Markdown with ``--format md``). The job's graph is
opened read-only; a stale or missing one is rebuilt into the CLI's own copy
under a file lock, never into the file the API serves. Most cases call
``main()`` in-process; the sandbox and concurrency cases run real processes.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess  # nosec B404 -- runs this repo's own script with fixed argv
import sys
import threading
from pathlib import Path

import pytest

from scripts import graph_query
from src.graph import catalog, job_graph
from src.storage.local_store import LocalArtifactStore

REPO = Path(__file__).resolve().parents[2]
REAL_RUN = REPO / "tests" / "fixtures" / "real_runs" / "wordpress-both" / "job"
OLD_JOB = REPO / "tests" / "fixtures" / "old_jobs" / "v0.4.0-beta.2" / "wordpress" / "aa4e840a"
DB, JOB = "wordpress", "job-1"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    base = tmp_path / "artifacts"
    shutil.copytree(REAL_RUN, base / DB / JOB)
    return base


def _main(capsys, *argv: str) -> tuple[int, dict | str]:
    code = graph_query.main(list(argv))
    out = capsys.readouterr().out
    lines = out.strip().splitlines()
    if "--format" in argv and argv[argv.index("--format") + 1] == "md":
        return code, out
    assert len(lines) == 1, out  # one compact JSON line, as headless scripts print
    return code, json.loads(lines[0])


def _job_args(root: Path) -> list[str]:
    return ["--job-id", JOB, "--artifact-root", str(root)]


# ---------------------------------------------------------------------------
# list / params / run
# ---------------------------------------------------------------------------


def test_list_names_every_entry_without_a_job(capsys):
    code, out = _main(capsys, "list")
    assert code == 0 and out["status"] == "ok"
    assert [e["id"] for e in out["entries"]] == [e.id for e in catalog.entries()]
    blast = next(e for e in out["entries"] if e["id"] == "table_blast_radius")
    assert blast["params"] == ["table_id"] and blast["max_rows"] == 1000
    assert blast["question"].endswith("?")


def test_params_lists_choices_from_the_job(capsys, root):
    code, out = _main(capsys, "params", "table_blast_radius", *_job_args(root))
    assert code == 0
    (param,) = out["params"]
    assert param["name"] == "table_id" and param["kind"] == "table"
    assert "wordpress.wp_posts" in param["choices"] and param["truncated"] is False


def test_params_without_a_job_lists_no_choices(capsys):
    code, out = _main(capsys, "params", "engine_destinations")
    assert code == 0
    (param,) = out["params"]
    assert param["example"] == "dynamodb" and "choices" not in param


def test_run_returns_bounded_rows_and_finds_the_database(capsys, root):
    code, out = _main(
        capsys,
        "run",
        "table_blast_radius",
        "--param",
        "table_id=wordpress.wp_posts",
        *_job_args(root),
    )
    assert code == 0 and out["status"] == "ok"
    assert out["entry"] == "table_blast_radius" and out["db"] == DB
    assert out["params"] == {"table_id": "wordpress.wp_posts"}
    assert out["row_count"] == len(out["rows"]) > 0 and out["truncated"] is False
    assert out["columns"][:2] == ["table_id", "query_id"]
    assert {r["table_id"] for r in out["rows"]} == {"wordpress.wp_posts"}
    assert (out["graph"]["source"], out["graph"]["rebuilt"]) == ("cli", True)
    assert out["graph"]["path"] == job_graph.cli_graph_path(str(root), DB, JOB)


def test_a_result_exactly_at_the_bound_is_not_truncated(capsys, root):
    code, out = _main(capsys, "run", "top_traffic_queries", *_job_args(root))
    assert code == 0 and out["row_count"] == 20 and out["truncated"] is False


def test_run_reports_truncation_when_the_bound_is_hit(capsys, root, monkeypatch):
    entry = catalog.get_entry("table_engines")
    monkeypatch.setitem(catalog._BY_ID, entry.id, dataclasses.replace(entry, max_rows=3))
    code, out = _main(capsys, "run", "table_engines", *_job_args(root))
    assert code == 0 and out["row_count"] == 3 and out["truncated"] is True
    assert out["max_rows"] == 3


def test_an_empty_result_names_the_missing_stage(capsys, root):
    code, out = _main(capsys, "run", "risk_register", *_job_args(root))
    assert code == 0 and out["row_count"] == 0
    assert [n["id"] for n in out["unmet_needs"]] == ["synthesis"]
    assert "Referee synthesis" in out["explanation"]


def test_run_as_markdown(capsys, root):
    code, out = _main(capsys, "run", "engine_distribution", *_job_args(root), "--format", "md")
    assert code == 0
    assert out.startswith("### 2. Engine distribution")
    assert "| engine | queries |" in out and "| dynamodb |" in out


def test_a_second_run_reuses_the_cli_copy(capsys, root):
    _main(capsys, "run", "node_census", *_job_args(root))
    _, out = _main(capsys, "run", "node_census", *_job_args(root))
    assert (out["graph"]["source"], out["graph"]["rebuilt"]) == ("cli", False)


# ---------------------------------------------------------------------------
# Errors: one JSON line, exit 1
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv,message",
    [
        (["params", "no_such_entry"], "unknown catalog entry"),
        (["run", "no_such_entry", "--job-id", JOB], "unknown catalog entry"),
        (["run", "table_blast_radius", "--job-id", JOB], "missing parameter"),
        (["run", "node_census", "--param", "x=y", "--job-id", JOB], "unknown parameter"),
        (["run", "node_census", "--param", "novalue", "--job-id", JOB], "--param must be"),
        (["run", "node_census", "--job-id", "../etc"], "job_id must"),
        (["run", "node_census", "--job-id", "absent"], "no job"),
        (["run", "node_census", "--job-id", JOB, "--db", "../x"], "database"),
    ],
)
def test_bad_input_is_one_json_error(capsys, root, argv, message):
    code, out = _main(capsys, *argv, "--artifact-root", str(root))
    assert code == 1 and out["status"] == "error"
    assert message in out["message"], out


@pytest.mark.parametrize(
    "argv,message",
    [
        (["run", "node_census"], "required: --job-id"),
        (["list", "--format", "xml"], "invalid choice"),
        ([], "required"),
        (["nope"], "invalid choice"),
    ],
)
def test_usage_errors_are_the_json_error_line(capsys, argv, message):
    code, out = _main(capsys, *argv)
    assert code == 1 and out["status"] == "error" and message in out["message"], out


def test_clean_removes_the_cli_copy(capsys, root):
    code, out = _main(capsys, "run", "node_census", *_job_args(root), "--clean")
    assert code == 0 and out["cleaned"] is True and out["row_count"] > 0
    cli_dir = Path(job_graph.cli_graph_path(str(root), DB, JOB)).parent
    assert not cli_dir.exists()


def test_an_ambiguous_job_id_needs_db(capsys, root):
    shutil.copytree(OLD_JOB, root / "otherdb" / JOB)
    code, out = _main(capsys, "run", "node_census", *_job_args(root))
    assert code == 1 and "--db" in out["message"]
    code, out = _main(capsys, "run", "node_census", *_job_args(root), "--db", "otherdb")
    assert code == 0 and out["db"] == "otherdb"


# ---------------------------------------------------------------------------
# Real processes: the sandbox and concurrency
# ---------------------------------------------------------------------------


def _cli(*argv: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(  # nosec B603 -- fixed argv
        [sys.executable, str(REPO / "scripts" / "graph_query.py"), *argv],
        cwd=REPO,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        timeout=180,
        check=False,
    )


def test_the_sandbox_refuses_an_artifact_root_outside_the_repo(tmp_path):
    proc = _cli(
        "run", "node_census", "--job-id", JOB, "--artifact-root", str(tmp_path),
        env={"MODERNIZER_CI_SANDBOX": "1"},
    )  # fmt: skip
    assert proc.returncode == 1
    out = json.loads(proc.stdout.strip())
    assert out["status"] == "error" and "outside the repository root" in out["message"]


def test_concurrent_cli_runs_build_the_copy_once(root):
    """Two processes, no graph yet: the file lock lets one build and the other reuse it."""
    procs = [
        subprocess.Popen(  # nosec B603 -- fixed argv
            [
                sys.executable, str(REPO / "scripts" / "graph_query.py"),
                "run", "node_census", *_job_args(root),
            ],
            cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )  # fmt: skip
        for _ in range(2)
    ]
    outs = []
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=180)
        assert proc.returncode == 0, stderr
        outs.append(json.loads(stdout.strip()))
    assert sorted(o["graph"]["rebuilt"] for o in outs) == [False, True]
    assert outs[0]["rows"] == outs[1]["rows"]


def test_cli_reads_while_the_api_rebuilds_the_same_file(root):
    """The API rebuilds its file (temp file, then rename) in a loop while CLI processes
    read it: every read succeeds with the same answer, and the API's file stays a
    current graph. The CLI only ever opens that file read-only."""
    store = LocalArtifactStore(base_dir=str(root))
    path = job_graph.api_graph_path(str(root), DB, JOB)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    job_graph.build_graph_file(DB, JOB, path, store)
    stop = threading.Event()
    rebuilds: list[int] = []
    errors: list[BaseException] = []

    def api_rebuilds():
        try:
            while not stop.is_set():
                job_graph.build_graph_file(DB, JOB, path, store)
                rebuilds.append(1)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    worker = threading.Thread(target=api_rebuilds)
    worker.start()
    try:
        outs = []
        for _ in range(3):
            proc = _cli("run", "engine_distribution", *_job_args(root))
            assert proc.returncode == 0, proc.stderr
            outs.append(json.loads(proc.stdout.strip()))
    finally:
        stop.set()
        worker.join()
    assert not errors, errors
    assert rebuilds, "the API never rebuilt during the reads"
    assert all(o["rows"] == outs[0]["rows"] for o in outs)
    # The CLI read the API's own file (not a copy) while it was being replaced.
    sources = [o["graph"]["source"] for o in outs]
    assert "api" in sources, sources
    assert {o["graph"]["path"] for o in outs if o["graph"]["source"] == "api"} == {path}
    assert job_graph.is_current_graph_file(path)
