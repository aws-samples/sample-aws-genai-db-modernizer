"""Print the access-pattern shadow diff for the bundled samples (#728).

The first assignment writes ``assignment/v1/access_pattern_shadow.json``
(:mod:`src.agents.referee.access_pattern_shadow`): each query's access-pattern
class, its current engine and scores, and the engine and scores the per-query
class model would give. Routing does not read it. This helper runs the
deterministic pipeline on every bundled sample (or reads existing job
directories) and prints, per sample, the queries per class, how many would
change engine and between which engines, and the moved queries themselves::

    uv run python -m tests.e2e.access_pattern_shadow
    uv run python -m tests.e2e.access_pattern_shadow --job-dir artifacts/wordpress/<job> --show 50
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
SHADOW = Path("assignment") / "v1" / "access_pattern_shadow.json"


def load(job_dir: Path) -> dict[str, Any]:
    shadow: dict[str, Any] = json.loads((job_dir / SHADOW).read_text())
    return shadow


def _score(scores: dict[str, Any], engine: str) -> str:
    value = scores.get(engine, "-")
    return str(value)


def moved(shadow: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in shadow["queries"] if r["current"]["engine"] != r["shadow"]["engine"]]


def format_shadow(
    sample: str, shadow: dict[str, Any], query_text: dict[str, str] | None = None, show: int = 20
) -> str:
    s = shadow["summary"]
    lines = [
        f"## {sample}: {s['engine_changes']} of {s['queries']} queries would change "
        "first-assignment engine",
        "",
        "Queries per class: " + ", ".join(f"{c} {n}" for c, n in s["by_class"].items()),
        "Distribution now: " + ", ".join(f"{e} {n}" for e, n in s["current_distribution"].items()),
        "Distribution in shadow: "
        + ", ".join(f"{e} {n}" for e, n in s["shadow_distribution"].items()),
    ]
    if s["moves"]:
        lines.append("Moves: " + ", ".join(f"{m} {n}" for m, n in s["moves"].items()))
        if s.get("control_moves"):
            lines.append(
                "Control run (own tables' pattern scores, per-query table rule only) moves: "
                + ", ".join(f"{m} {n}" for m, n in s["control_moves"].items())
            )
        if s.get("rule_moves"):
            lines.append(
                "Shadow moves of queries the control run moves too (kept out of the per-class counts): "
                + ", ".join(f"{m} {n}" for m, n in s["rule_moves"].items())
            )
        lines.append("Moves per class:")
        for cls, pairs in s["moves_by_class"].items():
            lines.append(f"- {cls}: " + ", ".join(f"{m} {n}" for m, n in pairs.items()))
    rows = moved(shadow)
    if rows:
        lines += [
            "",
            "| query | class | tables | engine | score now (from / to) | shadow (from / to) "
            "| shadow rule | sql |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for row in rows[:show]:
            before, after = row["current"]["engine"], row["shadow"]["engine"]
            tables = ", ".join(t.rsplit(".", 1)[-1] for t in row["tables"])
            text = " ".join((query_text or {}).get(row["query_id"], "").split())[:90]
            text = text.replace("|", "\\|")
            lines.append(
                f"| `{row['query_id'][:16]}` | {row['class']} | {tables} | {before} -> {after} "
                f"| {_score(row['current']['scores'], before)} / "
                f"{_score(row['current']['scores'], after)} "
                f"| {_score(row['shadow']['scores'], before)} / "
                f"{_score(row['shadow']['scores'], after)} "
                f"| {row['shadow']['rule']} | `{text}` |"
            )
        if len(rows) > show:
            lines.append(f"... and {len(rows) - show} more")
    return "\n".join(lines)


def _query_text(job_dir: Path) -> dict[str, str]:
    collector = json.loads((job_dir / "collector" / "output.json").read_text())
    return {
        q["query_id"]: q.get("query_text") or ""
        for q in collector.get("queries", {}).get("query_patterns", [])
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--job-dir", action="append", default=[], help="an existing job directory")
    parser.add_argument("--show", type=int, default=20, help="moved queries to list per sample")
    args = parser.parse_args()
    if args.job_dir:
        for job_dir in map(Path, args.job_dir):
            print(
                format_shadow(job_dir.parent.name, load(job_dir), _query_text(job_dir), args.show)
            )
            print()
        return
    from tests.e2e.pipeline import SAMPLES, run_pipeline

    with tempfile.TemporaryDirectory() as tmp:
        for sample in SAMPLES:
            result = run_pipeline(sample, Path(tmp) / "artifacts", job_id=f"shadow-{sample}")
            job_dir = result.job_dir()
            print(format_shadow(sample, load(job_dir), _query_text(job_dir), args.show))
            print()


if __name__ == "__main__":
    sys.path.insert(0, str(REPO))
    main()
