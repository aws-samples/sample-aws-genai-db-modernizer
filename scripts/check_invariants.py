#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Check the ADR invariants on a job's saved artifacts, as queries (#2143).

Usage:
    uv run python scripts/check_invariants.py <job dir> [--duckdb FILE] [--out FILE]

``<job dir>`` is ``<artifact root>/<db>/<job id>``. The job's saved artifacts (every
assignment version, design, design split and synthesis report) are read once into an
in-process DuckDB database (``src/tools/invariants/load.py``), and each invariant is
one SQL query over it (``src/tools/invariants/checks.py``). The queries check; they
decide nothing.

Prints one compact JSON line: ``status`` (``pass`` or ``fail``), the job, each check's
``status`` / ``checked`` / ``failed`` / ``warned`` counts, the first lines of each failing
check, the first lines of each warning (a legacy exception the ADR allows, which does not
fail the job), and
``legacy`` (what an older artifact did not record). ``--out`` also writes that object to
a file, and ``--duckdb`` keeps the loaded database in a file for ad-hoc queries. Exits 0
when every check passes or has nothing to check, 1 when one fails, 2 when the job
cannot be read or holds a contract version this code cannot read (the line is then ``{"status": "error", "message": ...}``, also
written to ``--out``).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import duckdb  # noqa: E402

from src.shared.contract_versioning import ContractVersionError  # noqa: E402
from src.tools.invariants.checks import run_checks  # noqa: E402
from src.tools.invariants.load import load_job  # noqa: E402


def check_job(job_dir: Path, database: str = ":memory:") -> dict:
    """The verdict object for ``job_dir`` (what the script prints)."""
    tables = load_job(job_dir, database)
    try:
        results = run_checks(tables.con)
    finally:
        tables.con.close()
    failing = [r for r in results if r["status"] == "fail"]
    return {
        "status": "fail" if failing else "pass",
        "job": f"{job_dir.parent.name}/{job_dir.name}",
        "checks": {
            r["name"]: {
                "status": r["status"],
                "checked": r["checked"],
                "failed": r["failed"],
                "warned": r["warned"],
            }
            for r in results
        },
        "failures": {r["name"]: r["examples"] for r in failing},
        "warnings": {r["name"]: r["warnings"] for r in results if r["warnings"]},
        "legacy": tables.legacy,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("job_dir", type=Path, help="<artifact root>/<db>/<job id>")
    parser.add_argument("--duckdb", default=":memory:", help="keep the database in this file")
    parser.add_argument("--out", type=Path, default=None, help="also write the verdict here")
    args = parser.parse_args(argv)
    try:
        verdict = check_job(args.job_dir.resolve(), args.duckdb)
    except (OSError, ValueError, duckdb.Error, ContractVersionError) as exc:
        verdict = {"status": "error", "message": f"{type(exc).__name__}: {exc}"}
    line = json.dumps(verdict, separators=(",", ":"))
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(line + "\n", encoding="utf-8")
    print(line)
    if verdict["status"] == "pass":
        return 0
    return 1 if verdict["status"] == "fail" else 2


if __name__ == "__main__":
    sys.exit(main())
