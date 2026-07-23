"""Benchmark CLI: discover cases, run the assignment stage, score, report."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from benchmarks.runner.loader import load_cases
from benchmarks.runner.models import Outcome
from benchmarks.runner.report import CaseRun, build_report
from benchmarks.runner.scoring import score_assignment
from benchmarks.runner.stage_assignment import run_assignment_case

# Bedrock throttling exception name (assignment is rule-based so this is future-proofing
# for LLM stages; kept here so the outcome categories are wired from day one).
_THROTTLE_MARKERS = ("ThrottlingException", "TooManyRequestsException", "Throttling")


def _is_throttle(exc: Exception) -> bool:
    return any(m in type(exc).__name__ or m in str(exc) for m in _THROTTLE_MARKERS)


def run_benchmark(stage_dir: Path, work_root: Path | None = None, tags=None) -> dict:
    work_root = work_root or Path(tempfile.mkdtemp(prefix="benchmark_"))
    runs: list[CaseRun] = []
    for case in load_cases(stage_dir, tags=tags):
        if not case.reviewed:
            runs.append(CaseRun(case.case_id, Outcome.SKIPPED, None))
            continue
        try:
            actual = run_assignment_case(case, work_root / case.case_id)
        except Exception as exc:  # noqa: BLE001 — categorize, never let one case abort the batch
            outcome = Outcome.THROTTLED if _is_throttle(exc) else Outcome.ERRORED
            runs.append(CaseRun(case.case_id, outcome, None))
            continue
        result = score_assignment(actual, case.expected)
        runs.append(CaseRun(case.case_id, Outcome.SCORED, result))
    return build_report(runs)


def main() -> None:
    parser = argparse.ArgumentParser(description="Assessment quality benchmark")
    parser.add_argument("--stage", default="assignment")
    parser.add_argument("--tag", action="append", dest="tags")
    parser.add_argument(
        "--cases-dir",
        default=str(Path(__file__).resolve().parents[1] / "cases"),
        help="Root of the cases/ directory",
    )
    parser.add_argument("--json", action="store_true", help="Print report as JSON")
    args = parser.parse_args()

    stage_dir = Path(args.cases_dir) / args.stage
    report = run_benchmark(stage_dir, tags=args.tags)

    if report["banner"]:
        print(report["banner"] + "\n")
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"stage={args.stage}  complete={report['complete']}")
        print(
            f"scored={report['scored_cases']}  throttled={report['throttled_cases']}  "
            f"errored={report['errored_cases']}  skipped={report['skipped_cases']}"
        )
        print(f"aggregate acceptable-accuracy: {report['aggregate_acceptable_accuracy']:.1%}")
        for c in report["per_case"]:
            acc = "-" if c["acceptable_accuracy"] is None else f"{c['acceptable_accuracy']:.1%}"
            print(f"  {c['case_id']:30} {c['outcome']:10} {acc}")


if __name__ == "__main__":
    main()
