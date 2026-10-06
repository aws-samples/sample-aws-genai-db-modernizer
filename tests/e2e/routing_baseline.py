"""Routing regression guard: the shape of the recommended modernization per sample.

The modernizer should recommend incremental, wave-based modernization, not a full
decomposition. This module measures, from one deterministic pipeline run, the
facts that would show a drift toward decomposition:

- the owner distribution after Reality Check (share of in-scope queries per engine),
- the share kept on the source-compatible engine (Aurora MySQL for MySQL,
  Aurora PostgreSQL for PostgreSQL),
- the number of owner engines and the queries each owns,
- the cache overlay (queries, share of calls), which is never an owner share,
- the migration wave plan (``report.json``'s ``migration_waves``, #225): engines,
  workload (or cache share) and table count per wave, every deliverable's source.

``tests/e2e/test_routing_baseline.py`` compares a run against
``tests/e2e/baselines/routing_baseline.json``. Update the baseline deliberately,
in the PR that changes routing, with a reason line:

    uv run python -m tests.e2e.routing_baseline --write --reason "why it moved (#NNN)"
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
BASELINE = Path(__file__).parent / "baselines" / "routing_baseline.json"

# A share (percentage points of in-scope queries, of calls, or of a wave's
# workload) may move this much before the guard fails.
TOLERANCE_PP = 5.0

# A query count (owned by an engine, or cached) may move by at most
# max(MIN_QUERY_DELTA, QUERY_DELTA_RATIO x baseline count). The share rule alone
# misses small engines: OpenSearch going from 3 to 83 of discourse's 1,654
# queries is 0.2% -> 5.0%, under 5 pp, but 80 queries is far over max(5, 0).
MIN_QUERY_DELTA = 5
QUERY_DELTA_RATIO = 0.25


def query_tolerance(baseline_count: int) -> int:
    """How many queries a count may move from ``baseline_count`` before the guard fails."""
    return max(MIN_QUERY_DELTA, int(QUERY_DELTA_RATIO * baseline_count))


def _latest(job_dir: Path, sub: str, name: str) -> Path:
    paths = sorted(job_dir.glob(f"{sub}/v*/{name}"), key=lambda p: int(p.parent.name.lstrip("v")))
    if not paths:
        raise FileNotFoundError(f"no {sub}/v*/{name} under {job_dir}")
    return paths[-1]


def measure(job_dir: Path) -> dict[str, Any]:
    """The routing shape of one job (see module docstring)."""
    from src.agents.referee.triage import SOURCE_ENGINE_TO_AURORA
    from src.agents.referee.utility_statements import is_utility_statement
    from src.report.renderers import resolve_migration_waves

    report = json.loads(_latest(job_dir, "synthesis", "report.json").read_text())
    version = (report.get("assignment_summary") or {}).get("version")
    assignment_path = (
        job_dir / "assignment" / f"v{version}" / "assignment.json"
        if version
        else _latest(job_dir, "assignment", "assignment.json")
    )
    assignment = json.loads(assignment_path.read_text())
    collector = json.loads((job_dir / "collector" / "output.json").read_text())
    source = str(
        ((collector.get("metadata") or {}).get("source_database") or {}).get("engine") or ""
    ).lower()
    source_compatible = SOURCE_ENGINE_TO_AURORA.get(source, "")

    in_scope = [qa for qa in assignment["query_assignments"] if qa.get("in_scope", True)]
    owners = Counter(qa["assigned_engine"] for qa in in_scope)
    total = len(in_scope) or 1
    overlay = assignment.get("cache_overlay") or {}

    # #327 finding 1: a utility/metadata statement must stay on the
    # source-compatible engine all the way to the final assignment, not just
    # survive the initial resolver pass -- the reality check used to have no
    # guard and sent every one of these to DynamoDB.
    query_by_id = {q["query_id"]: q for q in collector.get("queries", {}).get("query_patterns", [])}
    utility_off_aurora = [
        qa["query_id"]
        for qa in in_scope
        if qa["assigned_engine"] != source_compatible
        and is_utility_statement(
            query_by_id.get(qa["query_id"], {}).get("query_text"),
            query_by_id.get(qa["query_id"], {}).get("tables_accessed"),
        )
    ]

    waves = [
        {
            "engines": list(w.get("engines") or []),
            "workload_percent": (
                round(float(w.get("workload_share_percent") or 0), 1)
                if w.get("share_basis") != "calls"
                else 0.0
            ),
            "cached_call_share_percent": (
                round(float(w.get("workload_share_percent") or 0), 1)
                if w.get("share_basis") == "calls"
                else 0.0
            ),
            "table_count": int(w.get("table_count") or 0),
        }
        for w in resolve_migration_waves(report)
    ]
    return {
        "source_engine": source,
        "source_compatible_engine": source_compatible,
        "assignment_version": assignment.get("version"),
        "queries_in_scope": len(in_scope),
        "owner_share_percent": {e: round(n / total * 100, 1) for e, n in sorted(owners.items())},
        "owner_queries": dict(sorted(owners.items())),
        "source_compatible_share_percent": round(owners.get(source_compatible, 0) / total * 100, 1),
        "owner_engines": len(owners),
        "cache_overlay": {
            "queries": overlay.get("query_count", 0),
            "call_share_percent": overlay.get("call_share_percent", 0.0),
        },
        "waves": waves,
        "utility_queries_off_aurora": len(utility_off_aurora),
    }


def compare(
    baseline: dict[str, Any], actual: dict[str, Any], tol: float = TOLERANCE_PP
) -> list[str]:
    """Differences beyond tolerance between a sample's baseline and a run; [] if none."""
    problems: list[str] = []
    engines = set(baseline["owner_share_percent"]) | set(actual["owner_share_percent"])
    for e in sorted(engines):
        b = baseline["owner_share_percent"].get(e, 0.0)
        a = actual["owner_share_percent"].get(e, 0.0)
        if abs(a - b) > tol:
            problems.append(f"owner share of {e}: {b}% -> {a}% (tolerance {tol} pp)")
    base_counts = baseline.get("owner_queries", {})
    for e in sorted(set(base_counts) | set(actual.get("owner_queries", {}))):
        b_n, a_n = base_counts.get(e, 0), actual.get("owner_queries", {}).get(e, 0)
        if abs(a_n - b_n) > query_tolerance(b_n):
            problems.append(
                f"queries owned by {e}: {b_n} -> {a_n} (tolerance {query_tolerance(b_n)})"
            )
    b_c, a_c = baseline["cache_overlay"]["queries"], actual["cache_overlay"]["queries"]
    if abs(a_c - b_c) > query_tolerance(b_c):
        problems.append(f"cached reads: {b_c} -> {a_c} (tolerance {query_tolerance(b_c)})")
    b, a = baseline["source_compatible_share_percent"], actual["source_compatible_share_percent"]
    if abs(a - b) > tol:
        problems.append(f"source-compatible share: {b}% -> {a}% (tolerance {tol} pp)")
    if baseline["owner_engines"] != actual["owner_engines"]:
        problems.append(f"owner engines: {baseline['owner_engines']} -> {actual['owner_engines']}")
    b, a = (
        baseline["cache_overlay"]["call_share_percent"],
        actual["cache_overlay"]["call_share_percent"],
    )
    if abs(a - b) > tol:
        problems.append(f"cache overlay call share: {b}% -> {a}% (tolerance {tol} pp)")
    bw = [w["engines"] for w in baseline["waves"]]
    aw = [w["engines"] for w in actual["waves"]]
    if bw != aw:
        problems.append(f"wave structure: {bw} -> {aw}")
    else:
        for i, (bwave, awave) in enumerate(zip(baseline["waves"], actual["waves"], strict=True)):
            for key in ("workload_percent", "cached_call_share_percent"):
                if abs(awave[key] - bwave[key]) > tol:
                    problems.append(
                        f"wave {i + 1} {key}: {bwave[key]} -> {awave[key]} (tolerance {tol} pp)"
                    )
            b_tc, a_tc = bwave.get("table_count", 0), awave.get("table_count", 0)
            if abs(a_tc - b_tc) > query_tolerance(b_tc):
                problems.append(
                    f"wave {i + 1} table_count: {b_tc} -> {a_tc} (tolerance "
                    f"{query_tolerance(b_tc)})"
                )
    # #327 finding 1: zero tolerance. A utility/metadata statement off the
    # source-compatible engine is a bug (DynamoDB cannot run SHOW/SET/DDL),
    # not a routing-shape drift to tolerate.
    a_u = actual.get("utility_queries_off_aurora", 0)
    if a_u:
        problems.append(f"utility statements off the source-compatible engine: {a_u} (want 0)")
    return problems


def _write(reason: str) -> None:
    from tests.e2e.pipeline import run_pipeline

    samples: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for sample in ("wordpress", "discourse"):
            result = run_pipeline(sample, Path(tmp) / "artifacts", job_id=f"baseline-{sample}")
            samples[sample] = measure(result.job_dir())
    previous = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    history = [*previous.get("history", []), reason]
    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE.write_text(
        json.dumps(
            {
                "about": (
                    "Routing regression guard (tests/e2e/test_routing_baseline.py). Update "
                    "deliberately with: uv run python -m tests.e2e.routing_baseline --write "
                    '--reason "..."'
                ),
                "tolerance_pp": TOLERANCE_PP,
                "query_tolerance": (
                    f"max({MIN_QUERY_DELTA}, {QUERY_DELTA_RATIO} x baseline count) queries"
                ),
                "reason": reason,
                "history": history,
                "samples": samples,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"wrote {BASELINE}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="regenerate the baseline")
    parser.add_argument("--reason", default="", help="why the baseline moved (required)")
    args = parser.parse_args()
    if not args.write:
        parser.error("nothing to do: pass --write --reason '...'")
    if not args.reason.strip():
        parser.error("--reason is required: say why the routing shape moved")
    _write(args.reason.strip())


if __name__ == "__main__":
    sys.path.insert(0, str(REPO))
    main()
