"""Poll for a human-in-the-loop decision artifact.

Exits 0 and prints JSON content if the decision file exists.
Exits 1 silently if it does not exist yet.
Exits 2 with ``{"status": "error", "message": ...}`` if an argument is refused
under MODERNIZER_CI_SANDBOX=1 (see scripts/_sandbox.py).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._sandbox import NAME_ARGS, sandbox_violation  # noqa: E402
from src.storage.local_store import LocalArtifactStore  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
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
        default="./artifacts",
        help="Root directory for artifacts (default: ./artifacts)",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    violation = sandbox_violation(args, name_args=(*NAME_ARGS, "decision"))
    if violation:
        print(json.dumps({"status": "error", "message": violation}))
        sys.exit(2)

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
