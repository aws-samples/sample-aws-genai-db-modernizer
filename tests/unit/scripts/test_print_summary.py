"""``scripts/print_summary.py`` prints the gate block and the final summary (#1307).

/modernize quotes its stdout verbatim instead of composing the text, so it must be
complete, deterministic, free of anything but the block, and built from the artifacts.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 -- runs this repo's own print_summary.py script with fixed argv
import sys
from pathlib import Path

import pytest

from src.report.facts import report_facts
from tests.e2e.pipeline import run_pipeline
from tests.shared_runs import cached_run

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "print_summary.py"


@pytest.fixture(scope="module")
def run_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    def build(directory: Path) -> None:
        run_pipeline("wordpress", directory / "artifacts", "ps-job")

    return cached_run(tmp_path_factory, "print-summary-wordpress", build) / "artifacts"


def _run(
    root: Path, flag: str, *extra: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script
        [
            sys.executable,
            str(SCRIPT),
            "--job-id",
            "ps-job",
            "--db",
            "wordpress",
            flag,
            "--artifact-root",
            str(root),
            *extra,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        timeout=120,
    )


def _report(root: Path) -> dict:
    path = next((root / "wordpress" / "ps-job" / "synthesis").glob("v*/report.json"))
    report: dict = json.loads(path.read_text())
    return report


def test_the_gate_block_is_the_whole_stdout(run_root: Path) -> None:
    proc = _run(run_root, "--gate")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert out.startswith("## Assignment for approval: wordpress (job ps-job)\n")
    assert "| Engine | Queries before | Queries final | Share of final | Outcome |" in out
    assert "Output available at" not in out  # the artifact logger's line is not part of it
    assert "\x1b[" not in out
    assert not out.lstrip().startswith("{")


def test_the_gate_block_matches_the_reality_check_status_data(run_root: Path) -> None:
    from scripts import run_assessment
    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(base_dir=str(run_root))
    gate = run_assessment._reality_check_artifact(store, "ps-job", "wordpress")
    out = _run(run_root, "--gate").stdout
    for engine, after in gate["after_distribution"].items():
        row = next(line for line in out.splitlines() if f"| {_name(engine)} |" in line)
        assert f"| {gate['before_distribution'].get(engine, 0)} | {after} |" in row
    for engine in gate["eliminated_engines"]:
        row = next(line for line in out.splitlines() if f"| {_name(engine)} |" in line)
        assert row.endswith("| dropped |")
    for c in gate["consolidations"]:
        assert f"- {_name(c['from'])} to {_name(c['to'])}: {c['queries']} quer" in out
    for engine in gate["schema_design_engines"]:
        assert _name(engine) in out.split("Schema design runs next for:")[1]


def _name(engine: str) -> str:
    from src.shared.engine_names import display_engine

    return display_engine(engine)


def test_the_final_summary_states_the_report_facts(run_root: Path) -> None:
    proc = _run(run_root, "--final")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    facts = report_facts(_report(run_root))
    assert out.startswith("## Assessment: wordpress (job ps-job)\n")
    assert f"Projected monthly cost: ${facts['projected_monthly_cost_usd']:,.2f}" in out
    assert f"Open risks: {facts['open_risk_count']}" in out
    for engine in facts["engines"]:
        assert f"- {_name(engine['engine'])} ({engine['role']}" in out
    for name in ("executive-report.pdf", "engineering-report.md", "assessment-data.json"):
        assert f"/{name}`" in out
    assert "Not rendered this time" not in out


def test_each_flag_prints_the_same_text_every_time(run_root: Path) -> None:
    for flag in ("--gate", "--final"):
        first, second = _run(run_root, flag), _run(run_root, flag)
        assert first.returncode == second.returncode == 0
        assert first.stdout == second.stdout != ""


def test_the_deliverables_folder_is_named_by_its_absolute_path_outside_the_working_directory(
    run_root: Path,
) -> None:
    # The tmp root is outside the repo (the working directory), so the path stays absolute.
    out = _run(run_root, "--final").stdout
    assert f"- `{run_root}/wordpress/ps-job/deliverables/executive-report.pdf`:" in out


def test_a_job_without_a_synthesis_report_is_a_json_error(run_root: Path, tmp_path: Path) -> None:
    proc = _run(tmp_path, "--final")
    assert proc.returncode == 1
    assert json.loads(proc.stdout)["status"] == "error"
    assert "run /synthesize first" in json.loads(proc.stdout)["message"]


def test_a_job_without_reality_check_output_is_a_json_error(tmp_path: Path) -> None:
    proc = _run(tmp_path, "--gate")
    assert proc.returncode == 1
    assert "run reality check first" in json.loads(proc.stdout)["message"]


def test_one_of_gate_or_final_is_required(run_root: Path) -> None:
    both = subprocess.run(  # nosec B603
        [sys.executable, str(SCRIPT), "--job-id", "j", "--db", "d", "--gate", "--final"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    none = subprocess.run(  # nosec B603
        [sys.executable, str(SCRIPT), "--job-id", "j", "--db", "d"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert both.returncode == 2 and "not allowed with argument" in both.stderr
    assert none.returncode == 2 and "one of the arguments --gate --final is required" in none.stderr


@pytest.mark.parametrize("flag", ["--gate", "--final"])
def test_the_ci_sandbox_refuses_a_root_outside_the_repo(
    run_root: Path, tmp_path: Path, flag: str
) -> None:
    proc = _run(tmp_path, flag, env={"MODERNIZER_CI_SANDBOX": "1"})
    assert proc.returncode == 1
    assert json.loads(proc.stdout)["status"] == "error"


def test_a_bad_job_id_is_a_json_error(run_root: Path) -> None:
    proc = subprocess.run(  # nosec B603
        [
            sys.executable,
            str(SCRIPT),
            "--job-id",
            "../x",
            "--db",
            "wordpress",
            "--gate",
            "--artifact-root",
            str(run_root),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 1
    assert json.loads(proc.stdout)["status"] == "error"


def test_mode_adds_the_page_urls_to_the_final_summary_and_chat_adds_none(run_root: Path) -> None:
    both = _run(run_root, "--final", "--mode", "both").stdout
    assert "- Results page (the interactive report): http://localhost:" in both
    assert "/analysis/results/ps-job" in both and "/analysis/monitor/summary/ps-job" in both
    assert "/analysis/" not in _run(run_root, "--final").stdout


@pytest.mark.parametrize("mode", ["ui", "both"])
def test_mode_adds_the_summary_page_url_to_the_gate_block(run_root: Path, mode: str) -> None:
    # The gate block carries the summary-page URL itself (job_page_url, what
    # `start_local_ui.py --page summary` prints), so one quoted block shows both (#1307).
    from scripts.start_local_ui import job_page_url

    out = _run(run_root, "--gate", "--mode", mode).stdout
    url = job_page_url("ps-job", "summary")
    assert out.rstrip().endswith(f"- Summary page (the query flow and every query's engine): {url}")
    assert out.startswith("## Assignment for approval: wordpress (job ps-job)\n")


def test_chat_mode_adds_no_url_to_the_gate_block(run_root: Path) -> None:
    for extra in ((), ("--mode", "chat")):
        proc = _run(run_root, "--gate", *extra)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.startswith("## Assignment for approval: wordpress (job ps-job)\n")
        assert "/analysis/" not in proc.stdout and "Summary page" not in proc.stdout


def _copy_job(run_root: Path, tmp_path: Path) -> Path:
    import shutil

    root = tmp_path / "artifacts"
    shutil.copytree(run_root / "wordpress" / "ps-job", root / "wordpress" / "ps-job")
    return root


def test_the_gate_shows_the_reality_check_run_the_assignment_descends_from(
    run_root: Path, tmp_path: Path
) -> None:
    # Two runs: the lineage's record under reality-check/v<N>/ and, in the flat
    # output.json, a later run of another lineage. The gate shows the lineage's (#193, #215).
    from src.storage.assignment_versioning import reality_check_output_candidates
    from src.storage.local_store import LocalArtifactStore

    root = _copy_job(run_root, tmp_path)
    store = LocalArtifactStore(base_dir=str(root))
    lineage_key = reality_check_output_candidates(store, "wordpress", "ps-job")[0]
    assert "/reality-check/v" in lineage_key, lineage_key
    lineage = store.read_json(lineage_key)
    lineage["executive_summary"] = "The lineage run's summary."
    store.write_json(lineage_key, lineage)
    other = {
        **lineage,
        "executive_summary": "Another lineage's summary.",
        "architectural_patterns": [{"name": "Another lineage's pattern"}],
        "source_assignment_version": 90,
        "output_assignment_version": 91,
    }
    store.write_json("wordpress/ps-job/reality-check/output.json", other)
    out = _run(root, "--gate").stdout
    assert "The lineage run's summary." in out
    assert "Another lineage" not in out


def test_a_job_whose_only_reality_check_is_another_lineages_is_a_json_error(
    run_root: Path, tmp_path: Path
) -> None:
    from src.storage.assignment_versioning import reality_check_output_candidates
    from src.storage.local_store import LocalArtifactStore

    root = _copy_job(run_root, tmp_path)
    store = LocalArtifactStore(base_dir=str(root))
    lineage_key = reality_check_output_candidates(store, "wordpress", "ps-job")[0]
    other = {
        **store.read_json(lineage_key),
        "source_assignment_version": 90,
        "output_assignment_version": 91,
    }
    (root / lineage_key).unlink()
    store.write_json("wordpress/ps-job/reality-check/output.json", other)
    proc = _run(root, "--gate")
    assert proc.returncode == 1
    assert "run reality check first" in json.loads(proc.stdout)["message"]
