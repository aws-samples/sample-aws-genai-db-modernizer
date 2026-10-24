#!/usr/bin/env python3
"""Render the customer deliverables for a completed local job.

Writes the job's latest synthesis version into the customer folder
``artifacts/<db>/<job>/deliverables/`` under stable names (the executive report, the
engineering report, the assessment data JSON, and the detailed engineering report with
the full routing detail, read from the job's saved routing records) with an ``index.md``
(``src.report.deliverables.FOLDER_ENTRIES``). Nothing is written next to the
synthesis ``report.json``: model I/O, traces and the versioned folders stay as
the pipeline left them. Deterministic, no LLM calls.

Usage:
    uv run python scripts/run_report.py --job-id <id> --db <name>
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _write_deliverables_folder(job_dir, db: str, job_id: str, version: int, rendered):
    """Write the customer files into ``<job_dir>/deliverables/`` under their stable
    names and write the index. A documented file this round did not render is
    removed, so the folder never mixes rounds, and so is a retired deliverable an
    older round wrote; any other file is left alone."""
    from src.report.deliverables import (
        DELIVERABLES_DIR,
        FOLDER_ENTRIES,
        INDEX_FILENAME,
        RETIRED_FOLDER_FILENAMES,
        render_folder_index,
    )

    folder = job_dir / DELIVERABLES_DIR
    folder.mkdir(parents=True, exist_ok=True)
    files = rendered.folder_files()
    written = {entry.name for entry, _ in files}
    for entry in FOLDER_ENTRIES:
        if entry.name not in written:
            (folder / entry.filename).unlink(missing_ok=True)
    for retired in RETIRED_FOLDER_FILENAMES:
        (folder / retired).unlink(missing_ok=True)
    for entry, item in files:
        (folder / entry.filename).write_bytes(item.content)
    (folder / INDEX_FILENAME).write_text(
        render_folder_index(db, job_id, version, [entry for entry, _ in files]),
        encoding="utf-8",
    )
    return folder


def run(job_id: str, db: str, artifact_root: str, assignment_version: int | None) -> dict:
    from pathlib import Path

    from src.report.deliverables import FOLDER_FILENAMES, synthesis_report_key
    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(base_dir=artifact_root)
    try:
        report_key = synthesis_report_key(store, db, job_id, assignment_version or 0)
        latest_key = synthesis_report_key(store, db, job_id, 0)
    except FileNotFoundError:
        return {
            "status": "error",
            "message": f"No synthesis report for {db}/{job_id}; run /synthesize first.",
        }
    m = re.search(r"/synthesis/v(\d+)/", report_key)
    version = int(m.group(1)) if m else 0
    if report_key != latest_key:
        # The folder holds the latest version only, and no copy is written next to an
        # older report (#987), so an older version has nowhere to go.
        m_latest = re.search(r"/synthesis/v(\d+)/", latest_key)
        return {
            "status": "error",
            "message": (
                f"deliverables/ holds the latest assignment version "
                f"(v{m_latest.group(1) if m_latest else '?'}); v{version} is not rendered. "
                "Its report.json stays in its synthesis folder."
            ),
        }

    try:
        from src.report.deliverables import render_deliverables

        rendered = render_deliverables(store, job_id, db, report_key, assignment_version=version)
        folder = _write_deliverables_folder(
            Path(artifact_root, db, job_id), db, job_id, version, rendered
        )
        return {
            "status": "partial" if rendered.errors else "complete",
            "report": report_key,
            "deliverables_dir": str(folder.resolve()),
            "deliverables": sorted(
                str(p.resolve()) for p in folder.iterdir() if p.name in FOLDER_FILENAMES
            ),
            "errors": rendered.errors,
        }
    except Exception as e:  # noqa: BLE001 - this script must always print JSON
        return {"status": "error", "message": f"{type(e).__name__}: {e}"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Render deliverables for a local job.")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument(
        "--assignment-version",
        type=int,
        default=None,
        help="Synthesis version to render; only the latest is rendered (default: the latest)",
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
    except ValueError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}))
        sys.exit(1)

    result = run(args.job_id, args.db, args.artifact_root, args.assignment_version)
    print(json.dumps(result))
    sys.exit(0 if result["status"] != "error" else 1)


if __name__ == "__main__":
    main()
