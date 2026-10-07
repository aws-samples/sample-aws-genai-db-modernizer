"""``run_assessment.py`` keeps stdout short when it is not a terminal (#278).

On the discourse sample the script used to print ~108 KB of progress to
stdout. A headless Claude Code session persists a tool result that large to
a file outside the repository and returns only a 2 KB preview, so the
``{"phase": ...}`` status lines /modernize needs were out of reach. Now,
unless ``--verbose`` is given or stdout is a TTY, stdout carries only the
status lines plus one final ``{"log": ...}`` line, and the progress goes to
``artifacts/<db>/<job>/_logs/run_assessment.log``.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess  # nosec B404 -- runs this repo's own run_assessment.py script with fixed argv
import sys
import zipfile
from pathlib import Path

import pytest

from scripts import run_assessment
from scripts._compact_output import CompactConsole

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "run_assessment.py"
DISCOURSE_ZIP = REPO_ROOT / "docs" / "examples" / "discourse" / "discourse.zip"

# Claude Code returns tool results inline only below roughly this size.
STDOUT_LIMIT = 20_000
ASSESSMENT_PHASES = ["collect", "triage", "analysis", "assignment", "reality_check"]


def _discourse(tmp_path: Path) -> Path:
    with zipfile.ZipFile(DISCOURSE_ZIP) as z:
        z.extractall(tmp_path / "input")
    return tmp_path / "input" / "discourse-collection.json"


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("MODERNIZER_CI_SANDBOX", None)
    env.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    return env


def _run_script(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 - fixed argv
        [sys.executable, str(SCRIPT), *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=_env(),
        timeout=300,
    )


@pytest.fixture(scope="module")
def discourse_compact(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("compact")
    src = _discourse(tmp_path)
    proc = _run_script(tmp_path, "--file", str(src), "--db", "discourse", "--mode", "chat")
    return tmp_path, proc


def _json_lines(stdout: str) -> list[dict]:
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


def test_discourse_stdout_is_bounded(discourse_compact):
    _, proc = discourse_compact
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert len(proc.stdout.encode()) < STDOUT_LIMIT
    # Everything the session sees is bounded, not just stdout.
    assert len((proc.stdout + proc.stderr).encode()) < STDOUT_LIMIT


def test_discourse_stdout_is_status_lines_then_log_pointer(discourse_compact):
    tmp_path, proc = discourse_compact
    lines = _json_lines(proc.stdout)  # every line parses as JSON
    *status, pointer = lines
    assert [s["phase"] for s in status] == ASSESSMENT_PHASES
    assert status[-1]["status"] == "awaiting_llm"
    job_id = status[0]["job_id"]

    assert set(pointer) == {"log", "log_offset", "log_lines"}
    assert pointer["log"] == f"artifacts/discourse/{job_id}/_logs/run_assessment.log"
    log = (tmp_path / pointer["log"]).read_text(encoding="utf-8").splitlines()
    assert pointer["log_offset"] == 1
    assert pointer["log_lines"] == len(log)


def test_discourse_log_has_the_progress(discourse_compact):
    tmp_path, proc = discourse_compact
    pointer = _json_lines(proc.stdout)[-1]
    log = (tmp_path / pointer["log"]).read_text(encoding="utf-8")
    assert "[triage] Starting triage for discourse" in log
    assert "Database Modernizer Assessment" in log  # the stderr header
    assert '{"phase": "assignment", "status": "complete"' in log
    assert "\x1b[" not in log  # no terminal colors in the file
    assert len(log.encode()) > 50_000  # nothing was dropped


def test_resume_appends_to_the_same_log(discourse_compact, tmp_path):
    root, proc = discourse_compact
    first = _json_lines(proc.stdout)
    job_id, pointer = first[0]["job_id"], first[-1]
    rerun = _run_script(root, "--job-id", job_id, "--db", "discourse", "--llm-mode", "none")
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    *status, second = _json_lines(rerun.stdout)
    assert [s["phase"] for s in status] == ASSESSMENT_PHASES[1:]
    assert second["log"] == pointer["log"]
    assert second["log_offset"] == pointer["log_lines"] + 1
    total = len((root / second["log"]).read_text(encoding="utf-8").splitlines())
    assert second["log_offset"] + second["log_lines"] - 1 == total


def test_verbose_prints_progress_to_stdout_and_writes_no_log(tmp_path):
    src = _discourse(tmp_path)
    proc = _run_script(tmp_path, "--file", str(src), "--db", "discourse", "--verbose")
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = proc.stdout
    assert "\x1b[36m[triage]\x1b[0m Starting triage for discourse" in out
    assert len(out.encode()) > 50_000
    phases = [json.loads(ln)["phase"] for ln in out.splitlines() if ln.startswith('{"phase"')]
    assert phases == ASSESSMENT_PHASES
    assert '"log"' not in out.splitlines()[-1]
    assert "Database Modernizer Assessment" in proc.stderr
    assert not list((tmp_path / "artifacts").glob("*/*/_logs"))


def test_error_before_the_job_exists_is_reported_inline(tmp_path):
    proc = _run_script(tmp_path, "--file", "missing.json", "--db", "discourse")
    assert proc.returncode == 1
    *status, pointer = _json_lines(proc.stdout)
    assert status == [
        {"phase": "collect", "status": "error", "message": "File not found: missing.json"}
    ]
    assert pointer["log"] is None
    assert "Database Modernizer Assessment" in pointer["output_tail"]
    assert len(proc.stdout.encode()) < STDOUT_LIMIT


def test_uncaught_exception_becomes_an_error_status_line(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        run_assessment, "phase_collect", lambda f, db, store: ("job-1", "wordpress")
    )

    def boom(*args, **kwargs):
        run_assessment._log("triage", "about to fail")
        raise RuntimeError("triage exploded")

    monkeypatch.setattr(run_assessment, "phase_triage", boom)
    monkeypatch.setattr(sys, "argv", ["run_assessment.py", "--file", "c.json", "--db", "wordpress"])
    with pytest.raises(RuntimeError):
        run_assessment.main()

    out = capsys.readouterr()
    *status, pointer = _json_lines(out.out)
    assert status == [
        {
            "phase": "triage",
            "status": "error",
            "message": "RuntimeError: triage exploded",
            "log": "artifacts/wordpress/job-1/_logs/run_assessment.log",
        }
    ]
    log = (tmp_path / pointer["log"]).read_text(encoding="utf-8")
    assert "about to fail" in log
    assert "Traceback" in log and "triage exploded" in log
    assert out.err == ""
    assert not isinstance(sys.stdout, CompactConsole)
    assert not isinstance(sys.stderr, CompactConsole)


def test_keyboard_interrupt_still_prints_the_phase_error_line(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        run_assessment, "phase_collect", lambda f, db, store: ("job-1", "wordpress")
    )

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(run_assessment, "phase_triage", interrupted)
    monkeypatch.setattr(sys, "argv", ["run_assessment.py", "--file", "c.json", "--db", "wordpress"])
    with pytest.raises(KeyboardInterrupt):
        run_assessment.main()
    *status, pointer = _json_lines(capsys.readouterr().out)
    assert status[-1]["phase"] == "triage"
    assert status[-1]["status"] == "error"
    assert status[-1]["message"].startswith("KeyboardInterrupt")
    assert pointer["log"] == "artifacts/wordpress/job-1/_logs/run_assessment.log"


def test_log_dir_is_not_listed_as_an_agent(discourse_compact):
    from src.api.services.local_s3 import LocalS3Service
    from src.storage.local_store import LocalArtifactStore

    tmp_path, proc = discourse_compact
    job_id = _json_lines(proc.stdout)[0]["job_id"]
    assert (tmp_path / "artifacts" / "discourse" / job_id / "_logs").is_dir()
    agents = LocalS3Service(LocalArtifactStore(str(tmp_path / "artifacts"))).list_agent_artifacts(
        "discourse", job_id
    )
    assert "referee-triage" in agents
    assert not any("logs" in a for a in agents)


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_verbose_when_flag_or_tty():
    assert run_assessment._want_verbose(False, _Tty())
    assert run_assessment._want_verbose(True, io.StringIO())
    assert not run_assessment._want_verbose(False, io.StringIO())


def test_modernize_reads_the_status_lines_and_the_log_pointer():
    text = (REPO_ROOT / ".claude" / "commands" / "modernize.md").read_text(encoding="utf-8")
    flat = " ".join(text.split())
    assert '"log_offset"' in text and '"log_lines"' in text
    assert "_logs/run_assessment.log" in text
    assert "do not add `--verbose`" in flat
    assert "`offset` = `log_offset` and `limit` = `log_lines`" in flat


# --- #283: every path a status line names is the real artifact -------------


def _printed_paths(status: list[dict]) -> list[str]:
    paths: list[str] = []
    for line in status:
        for key in ("artifact", "llm_request"):
            if key in line:
                paths.append(line[key])
        paths.extend((line.get("artifacts") or {}).values())
    return paths


def test_discourse_status_paths_exist_and_are_cwd_relative(discourse_compact):
    tmp_path, proc = discourse_compact
    *status, _ = _json_lines(proc.stdout)
    paths = _printed_paths(status)
    assert len(paths) >= 9  # collect, triage, 5 analyses, assignment, llm_request
    for path in paths:
        assert path.startswith("artifacts/discourse/"), path
        assert (tmp_path / path).is_file(), path
    by_phase = {s["phase"]: s for s in status}
    assert by_phase["assignment"]["assignment_version"] == 1
    assert by_phase["assignment"]["artifact"].endswith("/assignment/v1/assignment.json")
    assert by_phase["reality_check"]["input_version"] == 1


def _run_all_in_process(monkeypatch, tmp_path, capsys) -> list[dict]:
    """``--all -y --llm-mode none`` on wordpress, with schema design doing nothing
    (as with #284: no designer runs without a model) and synthesis deterministic."""
    from src.agents.referee.synthesis_handler import run_synthesis
    from src.orchestrator.local_orchestrator import LocalOrchestrator

    with zipfile.ZipFile(REPO_ROOT / "docs" / "examples" / "wordpress" / "wordpress.zip") as z:
        z.extractall(tmp_path / "input")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(LocalOrchestrator, "_run_schema_design", lambda self, *a, **k: None)
    monkeypatch.setattr(LocalOrchestrator, "_run_post_schema_routing", lambda self, *a: None)

    def synthesis(self, job_id, db):
        v = self._get_assignment_version(job_id, db)
        run_synthesis(job_id, db, self.store, assignment_version=v, llm_mode="none")

    monkeypatch.setattr(LocalOrchestrator, "_run_synthesis", synthesis)
    argv = ["run_assessment.py", "--file", "input/wordpress-collection.json", "--db", "wordpress"]
    monkeypatch.setattr(sys, "argv", [*argv, "--llm-mode", "none", "--all", "-y"])
    run_assessment.main()
    *status, _ = _json_lines(capsys.readouterr().out)
    return status


def test_all_status_lines_name_real_artifacts(monkeypatch, tmp_path, capsys):
    status = _run_all_in_process(monkeypatch, tmp_path, capsys)
    by_phase = {s["phase"]: s for s in status}
    assert list(by_phase) == [*ASSESSMENT_PHASES, "schema_design", "synthesis"]
    for path in _printed_paths(status):
        assert path.startswith("artifacts/wordpress/"), path
        assert (tmp_path / path).is_file(), path

    version = by_phase["synthesis"]["assignment_version"]
    assert by_phase["synthesis"]["status"] == "complete"
    assert by_phase["synthesis"]["artifact"].endswith(f"/synthesis/v{version}/report.json")

    schema = by_phase["schema_design"]
    assert schema["status"] == "skipped"
    assert schema["reason"] == "llm_mode=none: every schema designer needs a model"
    assert schema["artifacts"] == {}
    assert schema["skipped_engines"]  # every in-scope engine, none designed


# --- #329: the approval gate needs the final per-engine distribution ------


def test_reality_check_status_line_has_the_distribution_for_the_approval_gate(
    monkeypatch, tmp_path, capsys
):
    status = _run_all_in_process(monkeypatch, tmp_path, capsys)
    by_phase = {s["phase"]: s for s in status}
    rc = by_phase["reality_check"]
    assert rc["status"] == "complete"

    # v1 -> final per-engine query counts, so the gate can show the shift
    # instead of a maintainer having to open the artifact themselves.
    assert rc["before_distribution"] == {
        "dynamodb": 33,
        "aurora_mysql": 58,
        "opensearch": 10,
        "documentdb": 6,
    }
    assert sum(rc["before_distribution"].values()) == sum(rc["after_distribution"].values())
    assert "opensearch" not in rc["after_distribution"]  # consolidated away
    assert "documentdb" not in rc["after_distribution"]

    # What moved, how many queries, and why -- compact, not the full query lists.
    assert rc["consolidations"]
    for c in rc["consolidations"]:
        assert {"from", "to", "queries"} <= set(c)
        assert set(c) <= {"from", "to", "queries", "reason"}
        assert isinstance(c["queries"], int) and c["queries"] > 0
        if "reason" in c:
            assert len(c["reason"]) <= run_assessment._REASON_LIMIT
            assert not re.search(
                r"#\d+", c["reason"]
            ), f"bare issue reference leaked into a printed reason: {c['reason']!r}"

    # documentdb -> aurora_mysql and documentdb -> dynamodb share the exact
    # same reason text on this sample: the second entry must drop it rather
    # than repeat it (#329 review).
    documentdb_entries = [c for c in rc["consolidations"] if c["from"] == "documentdb"]
    assert len(documentdb_entries) == 2
    assert "reason" in documentdb_entries[0]
    assert "reason" not in documentdb_entries[1]

    # Cache coverage carries over from the input assignment (#296) so the gate
    # can show it alongside the engine distribution -- trimmed to just the
    # fields the gate names, not the full overlay object (#329 review).
    assert set(rc["cache_overlay"]) == {"query_count", "calls_per_second", "call_share_percent"}

    # #329 review (regression on PR #407): elasticache owns no row in
    # after_distribution (the cache layer never owns a query, #296), but it
    # still gets its own schema design -- schema_design_engines must name it
    # explicitly so the orchestrator never has to (mis-)infer the dispatch
    # list from after_distribution alone.
    assert "elasticache" not in rc["after_distribution"]
    assert set(rc["schema_design_engines"]) == {"dynamodb", "aurora_mysql", "elasticache"}


def test_reality_check_status_line_reports_unchanged_distribution(tmp_path, monkeypatch, capsys):
    # Issue #329: when reality check consolidates nothing, the status line
    # still carries before_distribution == after_distribution (equal dicts,
    # no consolidations) so the command can say "nothing changed" instead of
    # presenting an empty table.
    from src.storage.local_store import LocalArtifactStore

    monkeypatch.chdir(tmp_path)
    store = LocalArtifactStore("artifacts")
    store.write_json(
        "wordpress/job-1/reality-check/output.json",
        {
            "before_distribution": {"dynamodb": 10},
            "after_distribution": {"dynamodb": 10},
            "consolidations": [],
        },
    )
    store.write_json("wordpress/job-1/assignment/v1/assignment.json", {"version": 1})
    artifact = run_assessment._reality_check_artifact(store, "job-1", "wordpress")
    assert artifact["before_distribution"] == artifact["after_distribution"] == {"dynamodb": 10}
    assert artifact["consolidations"] == []
    assert "cache_overlay" not in artifact  # none on the assignment
    # No query_assignments on this bare fixture assignment -- nothing in scope.
    assert artifact["schema_design_engines"] == []


def test_schema_design_engines_includes_the_cache_layer_engine(tmp_path, monkeypatch):
    # Regression test for the #329 review (PR #407): a run where the cache
    # layer fronts hot reads for two owner engines. after_distribution only
    # ever lists owners, so schema_design_engines -- not after_distribution
    # -- is what the orchestrator must read to decide which engines to
    # dispatch Phase 6 for.
    from src.storage.local_store import LocalArtifactStore

    monkeypatch.chdir(tmp_path)
    store = LocalArtifactStore("artifacts")
    db, job = "wordpress", "job-cache"
    store.write_json(
        f"{db}/{job}/referee-triage/triage.json",
        {
            "selected_agents": [
                {"agent_type": "dynamodb"},
                {"agent_type": "elasticache"},
                {"agent_type": "aurora_mysql"},
            ]
        },
    )
    store.write_json(
        f"{db}/{job}/assignment/v1/assignment.json",
        {
            "version": 1,
            "query_assignments": [
                {"query_id": "q1", "assigned_engine": "dynamodb", "cache_engine": "elasticache"},
                {"query_id": "q2", "assigned_engine": "aurora_mysql"},
            ],
            "cache_overlay": {
                "engine": "elasticache",
                "query_count": 1,
                "calls_per_second": 5.0,
                "call_share_percent": 50.0,
            },
        },
    )
    store.write_json(
        f"{db}/{job}/reality-check/output.json",
        {
            "before_distribution": {"dynamodb": 1, "aurora_mysql": 1},
            "after_distribution": {"dynamodb": 1, "aurora_mysql": 1},
            "consolidations": [],
        },
    )
    artifact = run_assessment._reality_check_artifact(store, job, db)
    assert "elasticache" not in artifact["after_distribution"]
    assert set(artifact["schema_design_engines"]) == {"dynamodb", "aurora_mysql", "elasticache"}


def test_truncate_keeps_long_reasons_within_the_limit():
    long_reason = "x" * 500
    truncated = run_assessment._truncate(long_reason)
    assert len(truncated) <= run_assessment._REASON_LIMIT
    assert truncated.endswith("…")
    assert run_assessment._truncate("short") == "short"


def test_truncate_scrubs_bare_issue_references():
    # Issue #329 review: the mandatory-engine justification floor in
    # reality_check.py builds a draft reason with a bare issue tag, e.g.
    # "... does not meet its justification floor (#167) -- ...". It is
    # usually rewritten by reconcile_consolidations before the artifact is
    # written, but test_reality_check_status_line_never_leaks_an_issue_tag
    # (below) shows a real scenario where it is not. An internal issue
    # number must never reach a customer-facing approval gate, so
    # run_assessment.py scrubs it defensively regardless of upstream text.
    reason = "DocumentDB does not meet its justification floor (#167) — serves 1 of 40 queries"
    cleaned = run_assessment._truncate(reason)
    assert "#167" not in cleaned
    assert "justification floor — serves" in cleaned
    assert (
        run_assessment._scrub_issue_refs("flagged per #167 for review") == "flagged per for review"
    )
    assert run_assessment._scrub_issue_refs("no tags here") == "no tags here"


def test_reality_check_status_line_never_leaks_an_issue_tag(tmp_path, monkeypatch):
    # Regression test for #329 review: build the exact scenario from
    # tests/unit/agents/referee/test_mandatory_engine_cost_share_floor.py
    # (a mandatory engine with a tiny workload share and a high fixed cost)
    # through the real deterministic handler + write step -- the same path
    # /modernize's reality_check status line reads. Confirms the underlying
    # artifact still carries the draft "(#167)" tag (so this test is not
    # vacuous), and that the printed status line never does.
    from src.agents.referee.reality_check_handler import (
        run_reality_check_deterministic,
        write_reality_check_result,
    )
    from src.storage.local_store import LocalArtifactStore

    monkeypatch.chdir(tmp_path)
    store = LocalArtifactStore("artifacts")
    db, job = "wordpress", "job-floor"

    query_ids = [f"dq{i}" for i in range(39)] + ["doc1"]
    assignment = {
        "version": 1,
        "query_assignments": [
            {"query_id": f"dq{i}", "assigned_engine": "dynamodb", "assignment_reason": "t"}
            for i in range(39)
        ]
        + [
            {
                "query_id": "doc1",
                "assigned_engine": "documentdb",
                "assignment_reason": "signal override: nested_document -> documentdb",
                "signal_override": "nested_document",
            }
        ],
    }
    triage = {
        "selected_agents": [{"agent_type": "dynamodb"}, {"agent_type": "documentdb"}],
        "signals": [],
        "query_capabilities": {},
    }
    collector = {
        "queries": {
            "query_patterns": [
                {"query_id": qid, "tables_accessed": ["db.users"], "query_type": "SELECT"}
                for qid in query_ids
            ]
        }
    }
    recommendation = [{"table_id": "db.users", "confidence_score": 80}]
    store.write_json(f"{db}/{job}/assignment/v1/assignment.json", assignment)
    store.write_json(f"{db}/{job}/referee-triage/triage.json", triage)
    store.write_json(f"{db}/{job}/collector/output.json", collector)
    store.write_json(
        f"{db}/{job}/analysis-dynamodb/analysis.json", {"table_recommendations": recommendation}
    )
    store.write_json(
        f"{db}/{job}/analysis-documentdb/analysis.json", {"table_recommendations": recommendation}
    )

    det = run_reality_check_deterministic(job, db, store, assignment_version=1)
    write_reality_check_result(store, job, db, det, 1)

    raw = store.read_json(f"{db}/{job}/reality-check/output.json")
    raw_reason = next(
        c["reason"] for c in raw["consolidations"] if c["from_engine"] == "documentdb"
    )
    assert "#167" in raw_reason, "fixture no longer reproduces the underlying leak -- update it"

    printed = run_assessment._reality_check_artifact(store, job, db)
    for c in printed["consolidations"]:
        assert "reason" not in c or not re.search(
            r"#\d+", c["reason"]
        ), f"bare issue reference leaked into the printed reason: {c!r}"


def test_schema_design_lists_only_engines_with_output(monkeypatch, tmp_path, capsys):
    _run_all_in_process(monkeypatch, tmp_path, capsys)
    state = json.loads((tmp_path / run_assessment.STATE_FILE).read_text())
    job, db = state["job_id"], state["database_name"]
    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore("artifacts")
    status = run_assessment._schema_design_status(store, job, db, "bedrock")
    assert status["status"] == "skipped"
    assert status["reason"] == "no engine wrote a schema_output.json"

    engine = status["skipped_engines"][0]
    version = status["assignment_version"]
    store.write_json(f"{db}/{job}/schema-{engine}/v{version}/schema_output.json", {})
    status = run_assessment._schema_design_status(store, job, db, "bedrock")
    assert status["status"] == "complete"
    assert list(status["artifacts"]) == [engine]
    assert (tmp_path / status["artifacts"][engine]).is_file()
    assert engine not in status.get("skipped_engines", [])
