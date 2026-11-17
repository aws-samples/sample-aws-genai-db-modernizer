#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Repair the routines the deterministic path can't carry, in the repair loop (#1940, #1941).

Reads the job's newest routine-migration artifact at or below the effective assignment
version (``--assignment-version`` to name one) and writes
``routines/v<N>/routine_repair.json`` (``src.agents.routine_repair.loop``):

    uv run python scripts/run_routine_repair.py --job-id <id> --db <name> --llm-mode none
    uv run python scripts/run_routine_repair.py --job-id <id> --db <name> --llm-mode bedrock --proof local
    uv run python scripts/run_routine_repair.py --job-id <id> --db <name> --llm-mode external --proof local
    uv run python scripts/run_routine_repair.py --job-id <id> --db <name> --finalize

``none`` runs no repair: the routines stay not carried, with their reasons. Each
candidate is rebuilt onto the exact text, checked again and, with ``--proof local``,
proved side by side against the original on two disposable engines of the source's
major (no network listener); only a candidate the proof accepts is ``model_repaired``.
With ``--proof none`` (the default) a candidate that passes every check is
``repaired_pending_proof``. A failed candidate is asked again, with its error, up to
three attempts. ``external`` writes round 1's request ``routines/v<N>/repair/
llm_input.json`` and prints ``awaiting_llm`` with ``round`` and the request and
response paths (under the artifact root, ``artifacts/<db>/<job>/...`` by default: the
paths to read and write as printed); each ``--finalize`` applies the answer written to
``routines/v<N>/repair/llm_response.json`` and prints ``awaiting_llm`` for the next
round or ``complete``. Pass the same ``--proof`` to each ``--finalize``: the proof target
is the first run's only when it is named again, else none, and every repair the saved state
carries is verified again before it is written. ``--proof auto`` (what ``/modernize`` passes,
#2385) is ``local`` when a disposable engine of the source's major is on this machine, else
``none``, never ``test-cluster``; the status line's ``proof`` says which.

``--proof test-cluster --approve`` (#2384, from ``/test-routines``): the original on a
disposable engine of the source's major, the repair on this job's throwaway Aurora test
cluster through the RDS Data API, run as a restricted runner made for the run, each
comparison in a scratch database dropped afterwards. It needs the customer's yes to the
repair question (``--approve``) and the job's test cluster deployed and available
(``run_dms_step.py`` reads the stack and its inputs file, as for ``proof-target-run``), and
it is refused under ``MODERNIZER_CI_SANDBOX=1``, so a headless run never reaches a cluster.
Each run and each ``--finalize`` opens the cluster again and cleans up after itself.

One JSON status line on stdout; nothing else.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import NoReturn

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_PHASE = "routine_repair"
_JOB: dict = {}


#: The status keys that name an artifact: printed as paths under the artifact root
#: (``artifacts/<db>/<job>/...`` by default), the way the PE review phase prints them,
#: so a session reads and writes the file the line names (#2385 review).
_PATH_KEYS = ("llm_request", "llm_response", "artifact")


def _output(data: dict) -> None:
    print(json.dumps({"phase": _PHASE, **data}))


def _paths_under_root(status: dict, artifact_root: str) -> dict:
    from pathlib import Path

    return {
        k: (str(Path(artifact_root) / v) if k in _PATH_KEYS and isinstance(v, str) else v)
        for k, v in status.items()
    }


def _error(message: str, code: int = 1) -> NoReturn:
    _output({"status": "error", "message": message})
    if _JOB:
        from scripts._job_status import write_failure_marker

        write_failure_marker(
            _JOB["store"], _JOB["db"], _JOB["job_id"], _PHASE, "ScriptError", message
        )
    sys.exit(code)


def main() -> None:
    parser = argparse.ArgumentParser(description="Repair the routines the checks can't carry.")
    parser.add_argument("--job-id", required=True, help="Job identifier")
    parser.add_argument("--db", required=True, help="Database name")
    parser.add_argument(
        "--llm-mode",
        default="none",
        choices=["bedrock", "external", "none"],
        help="Who repairs: Bedrock, Claude Code (external), or nobody (default: none)",
    )
    parser.add_argument(
        "--finalize",
        action="store_true",
        help="Apply the external session's answer and write the result",
    )
    from src.agents.routine_repair.loop import PROOF_CHOICES

    parser.add_argument(
        "--proof",
        default="none",
        choices=list(PROOF_CHOICES),
        help=(
            "Where the side-by-side proof runs: local (two disposable engines of the "
            "source's major), test-cluster (the repair on this job's test cluster; needs "
            "--approve), none (a candidate waits for the proof; default: none), or auto "
            "(local when those engines are on this machine, else none)"
        ),
    )
    parser.add_argument(
        "--approve",
        action="store_true",
        help="--proof test-cluster: the customer approved the repair rounds on the test cluster",
    )
    parser.add_argument(
        "--inputs",
        default=None,
        help="--proof test-cluster: the test cluster's inputs file (default: the job's)",
    )
    parser.add_argument(
        "--assignment-version",
        type=int,
        default=None,
        help="Read the routine-migration artifact at or below this version (default: effective)",
    )
    parser.add_argument(
        "--artifact-root",
        default=os.environ.get("ARTIFACT_DIR", "./artifacts"),
        help="Root directory for local artifacts (default: $ARTIFACT_DIR or ./artifacts)",
    )
    args = parser.parse_args()
    if args.llm_mode == "bedrock" and not args.finalize:
        from src.shared.bedrock_guardrail import exit_on_invalid_guardrail_config

        exit_on_invalid_guardrail_config()

    from scripts._sandbox import sandbox_violation

    violation = sandbox_violation(args)
    if violation:
        _error(violation)

    from src.shared.database_name import validate_database_name
    from src.shared.job_id import validate_job_id

    try:
        args.db = validate_database_name(args.db)
        args.job_id = validate_job_id(args.job_id)
    except ValueError as exc:
        _error(str(exc))

    from pydantic import ValidationError

    from scripts._job_status import clear_failure_marker
    from src.agents.routine_repair.handler import (
        StaleRepairRequest,
        finalize_routine_repair,
        run_routine_repair,
    )
    from src.agents.routine_repair.loop import proof_for_run
    from src.contracts.model_flag import validation_error_note
    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(base_dir=args.artifact_root)
    _JOB.update(store=store, db=args.db, job_id=args.job_id)
    clear_failure_marker(store, args.db, args.job_id, _PHASE)
    args.proof = proof_for_run(
        store, args.db, args.job_id, args.proof, assignment_version=args.assignment_version
    )

    def run(proof_options: dict | None = None) -> dict:
        if args.finalize:
            try:
                return finalize_routine_repair(
                    store,
                    args.db,
                    args.job_id,
                    assignment_version=args.assignment_version,
                    proof=args.proof,
                    proof_options=proof_options,
                )
            except (FileNotFoundError, StaleRepairRequest) as exc:
                _error(str(exc))
            except ValidationError as exc:
                _error(f"the answer is off RoutineRepairResponse: {validation_error_note(exc)}")
        return run_routine_repair(
            store,
            args.db,
            args.job_id,
            llm_mode=args.llm_mode,
            assignment_version=args.assignment_version,
            proof=args.proof,
            proof_options=proof_options,
        )

    if args.proof == "test-cluster":
        status = _on_test_cluster(args, store, run)
        if status.get("status") == "error":
            _error(str(status.get("message")))
    else:
        status = run()
    _output(_paths_under_root(status, args.artifact_root))


def _on_test_cluster(args, store, run) -> dict:
    """``run`` with the job's test cluster as the repair's side (#2384): the refusals of every
    test-cluster write (``run_dms_step.run_on_test_cluster``), then the cluster session
    (``proof_run.cluster_session``), which drops what it made when the round ends."""
    from scripts._sandbox import SANDBOX_ENV
    from scripts.run_dms_step import run_on_test_cluster

    if os.environ.get(SANDBOX_ENV) == "1":
        return {"status": "error", "message": "--proof test-cluster acts on the routine-proof "
                f"test cluster in AWS: refused under {SANDBOX_ENV}=1"}  # fmt: skip
    if args.llm_mode == "none" and not args.finalize:
        return dict(run())  # no repair runs, so no cluster is opened

    def on_cluster(*, cluster, data_api, secrets_client) -> dict:
        from src.storage.assignment_versioning import resolve_downstream_assignment_version
        from src.storage.routine_migration_store import latest_routine_migration
        from src.tools.routines.proof_run import cluster_session

        version = args.assignment_version or resolve_downstream_assignment_version(
            store, args.db, args.job_id
        )
        found = latest_routine_migration(store, args.db, args.job_id, version)
        if found is None:
            return dict(run())  # skipped: the job has no routine-migration artifact
        with cluster_session(found[1], data_api=data_api, cluster=cluster,
                             secrets_client=secrets_client, job_id=args.job_id) as opened:  # fmt: skip
            if opened.get("status") != "open":
                return opened
            status = run(opened["options"])
        left = opened.get("left_on_cluster") or []
        return {**status, "left_on_cluster": left} if left else status

    return run_on_test_cluster(artifact_root=args.artifact_root, db=args.db, job_id=args.job_id,
                               approve=args.approve, inputs=args.inputs, on_cluster=on_cluster)  # fmt: skip


def _job_ctx():
    if not _JOB:
        return None
    return (_JOB["store"], _JOB["db"], _JOB["job_id"], _PHASE, None, True)


if __name__ == "__main__":
    from scripts._job_status import run_main

    run_main(main, _job_ctx)
