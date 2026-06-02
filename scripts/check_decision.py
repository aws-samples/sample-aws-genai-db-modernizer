"""Poll for a human-in-the-loop decision artifact.

Exits 0 and prints JSON content if the decision file exists.
Exits 1 silently if it does not exist yet.
"""

from __future__ import annotations

import argparse
import json
import sys

from src.storage.local_store import LocalArtifactStore


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check whether a decision artifact exists for a given job."
    )
    parser.add_argument("--job-id", required=True, help="Job identifier")
    parser.add_argument("--db", required=True, help="Database / engine name")
    parser.add_argument(
        "--decision",
        required=True,
        help="Decision name (e.g. assignment_approval, triage_approval)",
    )
    parser.add_argument(
        "--artifact-root",
        default=".artifacts",
        help="Root directory for artifacts (default: .artifacts)",
    )
    args = parser.parse_args()

    store = LocalArtifactStore(base_dir=args.artifact_root)
    path = f"{args.db}/{args.job_id}/decisions/{args.decision}.json"

    if store.exists(path):
        data = store.read_json(path)
        print(json.dumps(data, indent=2))
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
