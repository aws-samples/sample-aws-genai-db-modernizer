#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Render step 1's delivery: the DMS stack, the suggested Aurora template and the DDL file (#1798).

Reads the job's ``data-movement/step-1/plan.json``, the transformation contract it pins and
the Aurora design, and writes ``aurora-target.yaml``, ``step-1.ddl.sql``,
``dms-step-1.yaml`` and, on Aurora PostgreSQL, ``step-1.sequences.sql`` under
``<db>/<job>/data-movement/step-1/``. No AWS call is made;
deploying is a separate step (``infrastructure/cloudformation/data-movement/README.md``).
Prints one JSON status line.

Usage:
    uv run python scripts/render_dms_step.py --job-id <id> --db <db>
    uv run python scripts/render_dms_step.py --job-id <id> --db <db> --bundle input.json   # the inputs, for an example
    uv run python scripts/render_dms_step.py --examples          # rewrite the examples
    uv run python scripts/render_dms_step.py --examples --check  # exit 1 if stale
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pydantic import ValidationError  # noqa: E402

from src.tools.data_movement.dms_step import (  # noqa: E402
    DmsStepBundle,
    DmsStepError,
    bundle_inputs,
    load_step1,
    render_files,
    write_step1_delivery,
)

EXAMPLES_DIR = (
    REPO_ROOT / "infrastructure" / "cloudformation" / "data-movement" / "examples" / "dms-step-1"
)


def example_dirs(examples_dir: Path = EXAMPLES_DIR) -> list[Path]:
    return sorted(p.parent for p in examples_dir.glob("*/input.json"))


def render_example(example: Path) -> dict[str, str]:
    bundle = DmsStepBundle.model_validate_json((example / "input.json").read_text())
    return render_files(bundle_inputs(bundle))


def _examples(check: bool) -> int:
    stale = []
    for example in example_dirs():
        for name, text in render_example(example).items():
            path = example / name
            if path.exists() and path.read_text() == text:
                continue
            if check:
                stale.append(path.relative_to(REPO_ROOT).as_posix())
            else:
                path.write_text(text)
    print(json.dumps({"status": "stale", "files": stale} if stale else {"status": "ok"}))
    return 1 if stale else 0


def _error(message: str) -> int:
    print(json.dumps({"status": "error", "message": message}))
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--job-id")
    parser.add_argument("--db")
    parser.add_argument("--artifact-root", default=os.environ.get("ARTIFACT_DIR", "./artifacts"))
    parser.add_argument("--bundle", type=Path, help="write the job's inputs to this file instead")
    parser.add_argument("--examples", action="store_true", help="render the committed examples")
    parser.add_argument("--check", action="store_true", help="with --examples: only compare")
    args = parser.parse_args(argv)
    if args.check and not args.examples:
        return _error("--check only works with --examples")
    if args.examples and (args.job_id or args.db or args.bundle):
        return _error("--examples renders the committed examples: drop --job-id, --db and --bundle")
    if args.examples and not args.check and os.environ.get("MODERNIZER_CI_SANDBOX") == "1":
        return _error(
            "--examples rewrites the committed examples: refused under MODERNIZER_CI_SANDBOX=1 "
            "(--examples --check only compares)"
        )
    if args.examples:
        try:
            return _examples(args.check)
        except (ValidationError, DmsStepError, OSError) as exc:
            print(json.dumps({"status": "error", "message": str(exc)}))
            return 1

    from scripts._sandbox import PATH_ARGS, sandbox_violation

    violation = sandbox_violation(args, path_args=(*PATH_ARGS, "bundle"))
    if violation:
        print(json.dumps({"status": "error", "message": violation}))
        return 1
    from src.shared.database_name import validate_database_name
    from src.shared.job_id import validate_job_id
    from src.storage.local_store import LocalArtifactStore

    try:
        if not args.job_id or not args.db:
            raise ValueError("--job-id and --db are required (or use --examples)")
        db, job_id = validate_database_name(args.db), validate_job_id(args.job_id)
        store = LocalArtifactStore(base_dir=args.artifact_root)
        if args.bundle:
            inputs = load_step1(store, db, job_id)
            bundle = DmsStepBundle(
                plan=inputs.plan,
                contract=inputs.contract,
                ddl=inputs.ddl,
                zero_date_defaults=list(inputs.zero_date_defaults),
                identity_columns=list(inputs.identity_columns),
                source_objects=inputs.source_objects,
                extensions=None if inputs.extensions is None else list(inputs.extensions),
            )
            args.bundle.write_text(bundle.model_dump_json(indent=2) + "\n")
            result: dict[str, object] = {"status": "complete", "bundle": str(args.bundle)}
        else:
            result = write_step1_delivery(store, db, job_id)
    except Exception as exc:  # noqa: BLE001 - this script must always print JSON
        result = {"status": "error", "message": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result))
    return 1 if result["status"] == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
