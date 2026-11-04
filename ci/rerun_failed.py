#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The failed test ids of a pull request's last CI run, to re-run them locally (#1849).

The same failing tests were pushed again on about one red shard run in eight.
``ci/rerun_failed.sh`` runs what this prints; ``ci/pre_merge.sh`` asks ``status``
and prints that command when the last run was red. Standard library and ``gh``::

    python3 ci/rerun_failed.py ids [PR]      # one failed test id per line
    python3 ci/rerun_failed.py status [PR]   # one line: red, green, none or unknown

PR is a number; without one, ``gh pr view`` finds the current branch's pull request.
The run is the newest *completed* run of the ``CI`` workflow (``ci.yml``) for the
pull request's head branch; a run still in progress is passed over. Its failed jobs'
logs (``gh run view --log-failed``) give the ids: pytest's ``FAILED <id>`` and
``ERROR <id>`` lines that start a log message. A step that fails without such a line (lint, a contract check)
gives no id; ``ids`` then says so and names the failed jobs.

Exit codes: 0 with ids, or with nothing to re-run (no pull request, no completed run,
the last run ended other than ``failure`` or ``timed_out``, or it failed with no test
id); 2 when it cannot tell (``gh``
missing, not authenticated, or an API call failed). ``status`` prints its one line
and exits 0 unless it cannot tell.

Details. Only the runs of the pull request's own commits count: a branch name such as
``main`` or ``patch-1`` is not unique across forks, and a commit a force-push dropped no
longer counts. A ``FAILED``/``ERROR`` line is read after the log's job, step and timestamp
prefix and xdist's ``[gwN] [ NN%]``; one inside captured output is not read. Each id is
kept once; a parametrized id keeps nested brackets and spaces, and one whose bracket never
closes is dropped. ``ci/rerun_failed.sh`` runs the unit, contract and property ids with
``uv run pytest -q -n 4``, the per-test time limit and the Hypothesis profile (``ci``,
unless ``HYPOTHESIS_PROFILE`` is set) of a pull request's ``ci/test.sh`` run, both from
``ci/lib.sh`` (``PYTEST_TIME_LIMIT``, ``use_pull_request_hypothesis_profile``); it exits
with pytest's code when a re-run test fails.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess  # nosec B404 -- runs gh with fixed argv
import sys
from collections.abc import Sequence

WORKFLOW = "ci.yml"
# The conclusions that count as a red run.
RED = frozenset({"failure", "timed_out"})
# A pytest outcome at the start of a log message: `gh run view --log-failed` prefixes
# each line with `job<TAB>step<TAB>timestamp `, and xdist's verbose lines add
# `[gw1] [ 58%] `. Plain pytest output, with no prefix, reads the same way.
_OUTCOME = re.compile(
    r"^(?:[^\t\n]*\t[^\t\n]*\t\S+ )?(?:\[gw\d+\] \[\s*\d+%\] )?(?:FAILED|ERROR) (tests/.*)$"
)


class CannotTell(Exception):
    """``gh`` is missing, not authenticated, or an API call failed."""


def _gh(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 B607 -- fixed argv
        ["gh", *args], capture_output=True, text=True, check=False
    )


def _node_id(rest: str) -> str | None:
    """The node id at the start of ``rest`` (``tests/a.py::t[p] - message``): it ends at
    the first space outside brackets, so a parameter keeps nested brackets and spaces.
    ``None`` when a bracket never closes or the id names no ``.py`` file."""
    depth = 0
    for i, char in enumerate(rest):
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        elif char.isspace() and depth == 0:
            rest = rest[:i]
            break
    path = rest.split("::", 1)[0]
    return rest if depth == 0 and path.endswith(".py") else None


def parse_failed_ids(log: str) -> list[str]:
    """Failed and errored test ids in ``log``, first seen first, each once. Only a
    ``FAILED``/``ERROR`` that starts a log message counts, not one inside captured output."""
    seen: dict[str, None] = {}
    for line in log.splitlines():
        match = _OUTCOME.match(line)
        node = _node_id(match.group(1)) if match else None
        if node:
            seen.setdefault(node, None)
    return list(seen)


def pick_run(runs: list[dict]) -> dict | None:
    """The newest completed run, by ``createdAt``; ``None`` when none has completed."""
    done = [r for r in runs if r.get("status") == "completed"]
    return max(done, key=lambda r: r.get("createdAt", "")) if done else None


def _check_gh() -> None:
    if shutil.which("gh") is None:
        raise CannotTell("gh is not installed (https://cli.github.com)")
    if _gh(["auth", "status"]).returncode != 0:
        raise CannotTell("gh is not authenticated: run gh auth login")


def find_pr(pr: str | None) -> dict | None:
    """The pull request (``number``, ``headRefName``, ``headRefOid``, ``commits``); ``None``
    when the current branch has none."""
    fields = "number,headRefName,headRefOid,commits"
    args = ["pr", "view", *([pr] if pr else []), "--json", fields]
    result = _gh(args)
    if result.returncode != 0:
        text = (result.stderr + result.stdout).lower()
        if "no pull requests found" in text or "could not resolve to a pullrequest" in text:
            return None
        raise CannotTell(f"gh pr view failed: {result.stderr.strip() or result.stdout.strip()}")
    found: dict = json.loads(result.stdout)
    return found


def last_run(pr: dict) -> dict | None:
    result = _gh(
        [
            "run",
            "list",
            "--workflow",
            WORKFLOW,
            "--branch",
            pr["headRefName"],
            "--event",
            "pull_request",
            "--limit",
            "20",
            "--json",
            "databaseId,status,conclusion,headSha,createdAt,url",
        ]
    )
    if result.returncode != 0:
        raise CannotTell(f"gh run list failed: {result.stderr.strip()}")
    # A branch name is not unique across forks (`main`, `patch-1`): keep only the runs
    # of this pull request's own commits. A commit a force-push dropped no longer counts.
    shas = {c.get("oid") for c in pr.get("commits") or []} | {pr.get("headRefOid")}
    runs = [r for r in json.loads(result.stdout or "[]") if r.get("headSha") in shas]
    return pick_run(runs)


def failed_jobs(run_id: int) -> list[str]:
    result = _gh(["run", "view", str(run_id), "--json", "jobs"])
    if result.returncode != 0:
        return []
    jobs = json.loads(result.stdout or "{}").get("jobs", [])
    return [j["name"] for j in jobs if j.get("conclusion") == "failure"]


def failed_ids(run_id: int) -> list[str]:
    result = _gh(["run", "view", str(run_id), "--log-failed"])
    if result.returncode != 0:
        raise CannotTell(f"gh run view --log-failed failed: {result.stderr.strip()}")
    return parse_failed_ids(result.stdout)


def _where(pr: dict, run: dict) -> str:
    older = "" if run.get("headSha") == pr.get("headRefOid") else ", for an older commit"
    return f"{run.get('url', run['databaseId'])}{older}"


def _describe(pr: dict, run: dict) -> str:
    return f"PR #{pr['number']}'s last CI run ({_where(pr, run)})"


def status(pr_arg: str | None) -> str:
    """``red <PR> <url>[, for an older commit]``, ``green <PR> success``, ``other <PR>
    <conclusion>`` (cancelled, skipped, ...), ``none <why>``; raises ``CannotTell``."""
    _check_gh()
    pr = find_pr(pr_arg)
    if pr is None:
        return "none no pull request for this branch"
    run = last_run(pr)
    if run is None:
        return f"none PR #{pr['number']} has no completed CI run"
    if run.get("conclusion") in RED:
        return f"red {pr['number']} {_where(pr, run)}"
    if run.get("conclusion") == "success":
        return f"green {pr['number']} success"
    return f"other {pr['number']} {run.get('conclusion')}"


def ids(pr_arg: str | None) -> tuple[list[str], str]:
    """``(ids, message)``: the ids to re-run and a line saying where they came from, or
    why there are none; raises ``CannotTell``."""
    _check_gh()
    pr = find_pr(pr_arg)
    if pr is None:
        return [], "no pull request for this branch: nothing to re-run"
    run = last_run(pr)
    if run is None:
        return [], f"PR #{pr['number']} has no completed CI run: nothing to re-run"
    where = _describe(pr, run)
    if run.get("conclusion") not in RED:
        return [], f"{where} ended {run.get('conclusion')}: nothing to re-run"
    found = failed_ids(run["databaseId"])
    if not found:
        jobs = ", ".join(failed_jobs(run["databaseId"])) or "unknown"
        return [], f"{where} failed with no failed test id (failed jobs: {jobs}): nothing to re-run"
    return found, f"{where} failed {len(found)} test(s)"


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("ids", "status") or len(argv) > 2:
        print(__doc__, file=sys.stderr)
        return 2
    pr_arg = argv[1] if len(argv) == 2 else None
    if pr_arg is not None and not pr_arg.lstrip("#").isdigit():
        print(f"error: PR must be a number, got {pr_arg!r}", file=sys.stderr)
        return 2
    pr_arg = pr_arg.lstrip("#") if pr_arg else None
    try:
        if argv[0] == "status":
            print(status(pr_arg))
            return 0
        found, message = ids(pr_arg)
    except CannotTell as exc:
        if argv[0] == "status":
            print(f"unknown {exc}")
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    print(message, file=sys.stderr)
    for node in found:
        print(node)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
