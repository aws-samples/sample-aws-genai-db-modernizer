"""Run the full modernizer pipeline with no LLM, the way a developer would locally."""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 -- runs this repo's own scripts/ entry points with fixed argv
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
# trailgear is the generated, synthetic SQL Server sample (scripts/generate_sqlserver_sample.py, #618):
# the one heterogeneous source, so the only sample that reaches the Aurora engine choice.
SAMPLES = {"wordpress": "wordpress", "discourse": "discourse", "trailgear": "trailgear"}


@dataclass
class PipelineResult:
    db: str
    job_id: str
    artifact_root: Path
    steps: dict[str, dict] = field(default_factory=dict)  # step name -> last-line JSON
    external: bool = False  # produced outside the deterministic runner (e.g. a headless run)

    @property
    def report(self) -> dict:
        return self.steps["report"]

    def job_dir(self) -> Path:
        return self.artifact_root / self.db / self.job_id

    def deliverable(self, filename: str) -> Path:
        """One file of the job's ``deliverables/`` folder, by its stable name
        (``src.report.deliverables.FOLDER_FILENAMES``, e.g. ``"executive-report.pdf"``)."""
        for p in self.report.get("deliverables", []):
            if p.rsplit("/", 1)[-1] == filename:
                return Path(p)
        raise FileNotFoundError(
            f"no deliverable named {filename!r} in {self.report.get('deliverables')}"
        )

    def report_json(self) -> Path:
        """The synthesis ``report.json`` the deliverables were rendered from."""
        return self.artifact_root / str(self.report["report"])


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    for k in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        env.pop(k, None)  # prove no AWS access is needed
    return env


def _run(step: str, args: list[str], result: PipelineResult) -> dict:
    proc = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script args
        [sys.executable, *args], cwd=REPO, capture_output=True, text=True, env=_env(), timeout=600
    )
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    try:
        payload = json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError:
        payload = {}
    if proc.returncode != 0 or not payload:
        raise RuntimeError(
            f"{step} failed (exit {proc.returncode})\nstdout tail:\n{proc.stdout[-2000:]}\n"
            f"stderr tail:\n{proc.stderr[-2000:]}"
        )
    result.steps[step] = payload
    return payload


def unzip_sample(name: str, dest: Path) -> Path:
    with zipfile.ZipFile(REPO / "docs" / "examples" / name / f"{name}.zip") as z:
        z.extractall(dest)
    return dest / f"{name}-collection.json"


def run_pipeline(sample: str, artifact_root: Path, job_id: str) -> PipelineResult:
    db = SAMPLES[sample]
    src = unzip_sample(sample, artifact_root.parent / f"input-{sample}")
    return run_pipeline_from_file(src, db, artifact_root, job_id)


def run_pipeline_from_file(src: Path, db: str, artifact_root: Path, job_id: str) -> PipelineResult:
    """Run the deterministic pipeline on an already-extracted ``collection.json``.

    Shared by ``run_pipeline`` (the bundled sample zips) and
    ``tests/e2e/routing_snapshot.py``'s ``ROUTING_SNAPSHOT_EXTRA`` fixture — a
    local, never-committed collection for a sample not bundled in the repo
    (e.g. AdventureWorks).
    """
    result = PipelineResult(db=db, job_id=job_id, artifact_root=artifact_root)
    common = ["--job-id", job_id, "--db", db, "--artifact-root", str(artifact_root)]

    _run("collect", ["scripts/run_collect.py", "--file", str(src), *common], result)
    triage = _run("triage", ["scripts/run_triage.py", *common], result)
    for engine in triage["selected"]:
        _run(
            f"analysis-{engine}",
            ["scripts/run_analysis.py", *common, "--engine", engine, "--llm-mode", "none"],
            result,
        )
    _run("assignment", ["scripts/run_assignment.py", *common], result)
    _run("reality-check", ["scripts/run_reality_check.py", *common, "--llm-mode", "none"], result)
    _run("synthesis", ["scripts/run_synthesis.py", *common, "--llm-mode", "none"], result)
    _run("report", ["scripts/run_report.py", *common], result)
    return result


def from_existing_job(db: str, job_id: str, artifact_root: Path) -> PipelineResult:
    """Wrap a job produced elsewhere (e.g. a headless Claude run) and re-render its deliverables."""
    result = PipelineResult(db=db, job_id=job_id, artifact_root=artifact_root, external=True)
    common = ["--job-id", job_id, "--db", db, "--artifact-root", str(artifact_root)]
    _run("report", ["scripts/run_report.py", *common], result)
    return result
