"""Run the full modernizer pipeline with no LLM, the way a developer would locally."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SAMPLES = {"wordpress": "wordpress", "discourse": "discourse"}


@dataclass
class PipelineResult:
    db: str
    job_id: str
    artifact_root: Path
    steps: dict[str, dict] = field(default_factory=dict)  # step name -> last-line JSON

    @property
    def report(self) -> dict:
        return self.steps["report"]

    def job_dir(self) -> Path:
        return self.artifact_root / self.db / self.job_id

    def deliverable(self, suffix: str) -> Path:
        """First file under the synthesis dir whose name ends with ``suffix``."""
        for p in self.report.get("paths", []):
            if p.endswith(suffix):
                return Path(p)
        raise FileNotFoundError(f"no deliverable ending {suffix!r} in {self.report.get('paths')}")

    def html(self, marker: str) -> Path:
        """First HTML deliverable whose filename contains ``marker`` (e.g. ``"decision-report"``)."""
        for p in self.report.get("paths", []):
            if p.endswith(".html") and marker in p.rsplit("/", 1)[-1]:
                return Path(p)
        raise FileNotFoundError(
            f"no html deliverable matching {marker!r} in {self.report.get('paths')}"
        )


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = "0"  # reality_check uses list(set(...))
    env.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    for k in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        env.pop(k, None)  # prove no AWS access is needed
    return env


def _run(step: str, args: list[str], result: PipelineResult) -> dict:
    proc = subprocess.run(
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
    result = PipelineResult(db=db, job_id=job_id, artifact_root=artifact_root)
    src = unzip_sample(sample, artifact_root.parent / f"input-{sample}")
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
