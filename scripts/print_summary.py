#!/usr/bin/env python3
"""Print the text /modernize quotes at the approval gate and at completion.

``--gate`` prints the assignment block (engine table, consolidations, kept small engines,
the cache layer, the engines designed next) from the reality-check artifacts, the same data
``run_assessment.py`` prints on its ``reality_check`` line. ``--final`` prints the completion
summary (engines, cost, open risks with mitigations, deliverables) from the synthesis
``report.json`` facts. Both print Markdown the command quotes verbatim, deterministic for a
job; a line whose figure is missing is left out. They mirror what the UI shows (the assignment
page, the top of the results page) and point to the artifacts for the detail. With
``--mode ui|both`` the gate block ends with the summary page's URL and the final summary lists
the job pages' URLs (``job_page_url``, the URL ``start_local_ui.py --page`` prints), so the
command quotes one block and runs no second command for a pointer.

The gate reads the record of the reality-check run the assignment descends from
(``read_reality_check_output``), never ``reality-check/output.json`` directly, which holds only
the latest run and can belong to another lineage (#193, #215).

Usage:
    uv run python scripts/print_summary.py --job-id <id> --db <name> --gate [--mode chat|ui|both]
    uv run python scripts/print_summary.py --job-id <id> --db <name> --final [--mode chat|ui|both]

On failure it prints one JSON line ``{"status": "error", "message": ...}`` and exits 1.
"""

import argparse
import contextlib
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class SummaryError(Exception):
    pass


def gate_text(store, job_id: str, db: str, mode: str = "chat") -> str:
    from scripts import run_assessment
    from src.report.printed_summary import render_gate_block
    from src.storage.assignment_versioning import (
        read_reality_check_output,
        resolve_downstream_assignment_version,
    )

    # The record of the run the assignment descends from (#193, #215), the one the
    # status data below comes from: never the latest run of another lineage.
    version = resolve_downstream_assignment_version(store, db, job_id)
    rc = read_reality_check_output(store, db, job_id, version)
    if rc is None:
        raise SummaryError(f"No reality-check output for {db}/{job_id}; run reality check first.")
    # The helper logs the artifact path to stdout; this script prints only the block.
    with contextlib.redirect_stdout(io.StringIO()):
        gate = run_assessment._reality_check_artifact(store, job_id, db)
    # What the assignment page shows besides the status data: the reality check's own summary
    # and the architectural patterns it detected.
    gate["executive_summary"] = rc.get("executive_summary")
    gate["architectural_patterns"] = rc.get("architectural_patterns") or []
    return render_gate_block(
        gate,
        database_name=db,
        job_id=job_id,
        summary_url=_page_url(job_id, "summary", mode),
    )


def _page_url(job_id: str, page: str, mode: str) -> str | None:
    if mode == "chat":
        return None
    from scripts.start_local_ui import job_page_url

    return job_page_url(job_id, page)


def final_text(store, job_id: str, db: str, artifact_root: str, mode: str) -> str:
    from pathlib import Path

    from src.report.deliverables import DELIVERABLES_DIR, FOLDER_ENTRIES, synthesis_report_key
    from src.report.printed_summary import render_final_summary

    try:
        report = store.read_json(synthesis_report_key(store, db, job_id, 0))
    except FileNotFoundError as exc:
        raise SummaryError(
            f"No synthesis report for {db}/{job_id}; run /synthesize first."
        ) from exc
    folder = Path(artifact_root, db, job_id, DELIVERABLES_DIR)
    present = [e for e in FOLDER_ENTRIES if (folder / e.filename).is_file()]
    from scripts._compact_output import display_path

    return render_final_summary(
        report,
        database_name=db,
        job_id=job_id,
        deliverables_dir=display_path(str(folder)).rstrip("/") + "/",
        deliverables=present,
        summary_url=_page_url(job_id, "summary", mode),
        results_url=_page_url(job_id, "results", mode),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Print the gate block or the final summary.")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--db", required=True)
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--gate", action="store_true", help="Print the approval-gate block")
    which.add_argument("--final", action="store_true", help="Print the completion summary")
    parser.add_argument(
        "--mode",
        choices=["chat", "ui", "both"],
        default="chat",
        help=(
            "With ui or both, --gate ends with the summary page's URL and --final with the "
            "job pages' URLs (chat starts no UI)"
        ),
    )
    parser.add_argument(
        "--artifact-root",
        default=os.environ.get("ARTIFACT_DIR", "./artifacts"),
        help="Root directory for local artifacts (default: $ARTIFACT_DIR or ./artifacts)",
    )
    args = parser.parse_args()

    from scripts._sandbox import sandbox_violation

    violation = sandbox_violation(args)
    if violation:
        print(json.dumps({"status": "error", "message": violation}))
        sys.exit(1)

    from src.shared.database_name import validate_database_name
    from src.shared.job_id import validate_job_id

    try:
        args.db = validate_database_name(args.db)
        args.job_id = validate_job_id(args.job_id)
        from src.storage.local_store import LocalArtifactStore

        store = LocalArtifactStore(base_dir=args.artifact_root)
        if args.gate:
            text = gate_text(store, args.job_id, args.db, args.mode)
        else:
            text = final_text(store, args.job_id, args.db, args.artifact_root, args.mode)
    except (ValueError, SummaryError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}))
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - this script must fail with a JSON line
        print(json.dumps({"status": "error", "message": f"{type(exc).__name__}: {exc}"}))
        sys.exit(1)
    sys.stdout.write(text)


if __name__ == "__main__":
    main()
