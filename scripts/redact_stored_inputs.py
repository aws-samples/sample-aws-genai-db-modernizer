#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Redact credentials stored by Assessments API versions before #723.

Older versions copied a submitted ``connection.password`` into each job's
``artifacts/<db>/<job>/_meta.json``. This rewrites every stored request input that still
holds a credential (``_meta.json`` and agent ``answers.json``) and prints the rewritten
and skipped file paths as JSON, never their contents. It is idempotent and runs whatever
the start-up marker says; the API also runs it once, in the background, after start-up.

Usage:
    uv run python scripts/redact_stored_inputs.py                      # $ARTIFACT_DIR or ./artifacts
    uv run python scripts/redact_stored_inputs.py --artifact-dir DIR
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.stored_input_redaction import record_marker, redact_stored_inputs  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--artifact-dir",
        default=os.environ.get("ARTIFACT_DIR", "./artifacts"),
        help="artifact root (default: $ARTIFACT_DIR or ./artifacts)",
    )
    args = parser.parse_args(argv)
    run = redact_stored_inputs(args.artifact_dir)
    if not run.skipped and os.path.isdir(args.artifact_dir):
        record_marker(args.artifact_dir, run)
    # Only file paths and counts are printed, never file contents.
    report = {
        "rewritten_files": list(run.rewritten),
        "skipped_files": list(run.skipped),
        "rewritten_count": len(run.rewritten),
    }
    print(json.dumps(report))
    return 1 if run.skipped else 0


if __name__ == "__main__":
    sys.exit(main())
