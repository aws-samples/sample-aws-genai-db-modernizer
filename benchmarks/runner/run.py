"""Benchmark CLI: discover cases, run a stage (assignment or final_assignment), score, report."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import tempfile
from pathlib import Path

from benchmarks.runner.gate import GateError, check_gate, load_gate
from benchmarks.runner.loader import load_cases
from benchmarks.runner.models import Outcome
from benchmarks.runner.report import CaseRun, build_report
from benchmarks.runner.scoring import score_assignment
from benchmarks.runner.stage_assignment import run_assignment_case
from benchmarks.runner.stage_final_assignment import run_final_assignment_case

# Stage name (index.json's "stage", else the directory name) -> case runner.
# ``assignment`` scores the resolver's first pass from gold inputs;
# ``final_assignment`` runs a sample through the reality check (#1074).
STAGE_RUNNERS = {
    "assignment": run_assignment_case,
    "final_assignment": run_final_assignment_case,
}

# Bedrock throttling exception name (assignment is rule-based so this is future-proofing
# for LLM stages; kept here so the outcome categories are wired from day one).
_THROTTLE_MARKERS = ("ThrottlingException", "TooManyRequestsException", "Throttling")


def _is_throttle(exc: Exception) -> bool:
    return any(m in type(exc).__name__ or m in str(exc) for m in _THROTTLE_MARKERS)


def stage_runner(stage_dir: Path):
    """The case runner for ``stage_dir``, named by its index.json ``stage``."""
    manifest = json.loads((Path(stage_dir) / "index.json").read_text())
    stage = manifest.get("stage") or Path(stage_dir).name
    try:
        return STAGE_RUNNERS[stage]
    except KeyError:
        raise ValueError(f"unknown benchmark stage {stage!r} in {stage_dir}") from None


def run_benchmark(
    stage_dir: Path, work_root: Path | None = None, tags=None, run_unreviewed: bool = False
) -> dict:
    """Run and score every case. An unreviewed case is always ``skipped``: with
    ``run_unreviewed`` it still runs (to show the stage works on it, and how
    many queries it assigned), but it never enters the aggregate or the gate."""
    work_root = work_root or Path(tempfile.mkdtemp(prefix="benchmark_"))
    run_case = stage_runner(stage_dir)
    runs: list[CaseRun] = []
    for case in load_cases(stage_dir, tags=tags):
        if not case.reviewed and not run_unreviewed:
            runs.append(CaseRun(case.case_id, Outcome.SKIPPED, None))
            continue
        try:
            # The agent prints diagnostic '[assignment] ...' lines to stdout.
            # Redirect them to stderr so stdout stays clean for the report
            # (--json must emit only valid JSON). Logs remain visible on stderr.
            with contextlib.redirect_stdout(sys.stderr):
                actual = run_case(case, work_root / case.case_id)
        except Exception as exc:  # noqa: BLE001 — categorize, never let one case abort the batch
            if not case.reviewed:
                # An unreviewed case never gates (ADR-030 Amendment 1), not even by
                # failing: it stays skipped and the error goes to stderr.
                print(f"{case.case_id}: unreviewed case failed: {exc!r}", file=sys.stderr)
                runs.append(CaseRun(case.case_id, Outcome.SKIPPED, None))
                continue
            outcome = Outcome.THROTTLED if _is_throttle(exc) else Outcome.ERRORED
            runs.append(CaseRun(case.case_id, outcome, None))
            continue
        if not case.reviewed:
            runs.append(CaseRun(case.case_id, Outcome.SKIPPED, None, assigned=len(actual)))
            continue
        result = score_assignment(actual, case.expected)
        runs.append(CaseRun(case.case_id, Outcome.SCORED, result, assigned=len(actual)))
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
    parser.add_argument(
        "--run-unreviewed",
        action="store_true",
        help="Also run unreviewed cases (still reported as skipped, never scored or gated)",
    )
    parser.add_argument(
        "--gate",
        action="store_true",
        help="Check the stage's gate.json floors; exit 1 when one is broken",
    )
    args = parser.parse_args()

    stage_dir = Path(args.cases_dir) / args.stage
    gate = None
    if args.gate and args.tags:
        parser.error("--gate checks every gated case; run it without --tag")
    if args.gate:
        try:
            gate = load_gate(stage_dir)
        except GateError as exc:
            print(f"gate: {exc}", file=sys.stderr)
            sys.exit(2)
    report = run_benchmark(stage_dir, tags=args.tags, run_unreviewed=args.run_unreviewed)
    failures = check_gate(report, gate) if gate is not None else []
    if gate is not None:
        report["gate"] = {"pass": not failures, "failures": failures}

    if args.json:
        # stdout is reserved for machine-readable JSON; banner goes to stderr.
        if report["banner"]:
            print(report["banner"] + "\n", file=sys.stderr)
        print(json.dumps(report, indent=2))
    else:
        if report["banner"]:
            print(report["banner"] + "\n")
        print(f"stage={args.stage}  complete={report['complete']}")
        print(
            f"scored={report['scored_cases']}  throttled={report['throttled_cases']}  "
            f"errored={report['errored_cases']}  skipped={report['skipped_cases']}"
        )
        print(f"aggregate acceptable-accuracy: {report['aggregate_acceptable_accuracy']:.1%}")
        for c in report["per_case"]:
            acc = "-" if c["acceptable_accuracy"] is None else f"{c['acceptable_accuracy']:.1%}"
            ran = (
                ""
                if c.get("assigned_queries") is None
                else f"  ({c['assigned_queries']} queries assigned)"
            )
            print(f"  {c['case_id']:30} {c['outcome']:10} {acc}{ran}")
        if gate is not None:
            if failures:
                print("\nGATE FAILED:")
                for line in failures:
                    print(f"  - {line}")
            else:
                print("\ngate: pass")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
