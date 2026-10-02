#!/usr/bin/env python3
"""Render the customer deliverables for a completed local job.

Writes, next to the job's synthesis ``report.json``: the decision report (HTML),
the engineering report (Markdown), the interactive analysis report (HTML), and the
executive summary (PPTX + PDF). Deterministic, no LLM calls.

Usage:
    uv run python scripts/run_report.py --job-id <id> --db <name>
    uv run python scripts/run_report.py --job-id <id> --db <name> --assignment-version 2
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run(job_id: str, db: str, artifact_root: str, assignment_version: int | None) -> dict:
    from src.report import analysis_report
    from src.report.deliverables import render_deliverables
    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(base_dir=artifact_root)
    try:
        report_key = analysis_report.synthesis_report_key(
            store, db, job_id, assignment_version or 0
        )
    except FileNotFoundError:
        return {
            "status": "error",
            "message": f"No synthesis report for {db}/{job_id}; run /synthesize first.",
        }
    m = re.search(r"/synthesis/v(\d+)/", report_key)
    version = int(m.group(1)) if m else 0

    # No graph_fetcher: the default (persisted graph, else rebuild) is right for a local store.
    rendered = render_deliverables(store, job_id, db, report_key, assignment_version=version)
    base = report_key.rsplit("/", 1)[0]
    files = []
    for d in rendered.items:
        if d.stage:
            key = f"{base}/{d.filename}"
            store.write_bytes(key, d.content)
            files.append(key)
    return {
        "status": "complete" if not rendered.errors else "partial",
        "report": report_key,
        "files": files,
        "errors": rendered.errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Render deliverables for a local job.")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument(
        "--assignment-version",
        type=int,
        default=None,
        help="Synthesis version to render (default: the highest present)",
    )
    parser.add_argument("--artifact-root", default="./artifacts")
    args = parser.parse_args()

    result = run(args.job_id, args.db, args.artifact_root, args.assignment_version)
    print(json.dumps(result))
    sys.exit(0 if result["status"] != "error" else 1)


if __name__ == "__main__":
    main()
