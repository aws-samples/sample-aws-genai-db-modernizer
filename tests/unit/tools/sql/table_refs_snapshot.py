"""Per-query regression snapshot of the sqlglot facade's table references (#557).

For every query in the bundled samples (wordpress, discourse, trailgear) this
records what the facade reports -- the resolved table ids, ``parsed_by`` and
the fallback reason -- and whether that agrees with the regex extractor (the
facade's fallback; the facade's answer drives routing since #553).
``test_table_refs_snapshot.py`` compares a fresh run with
``baselines/table_refs_snapshot.json``, so a sqlglot version bump, or a facade
change, shows exactly which queries changed. Regenerate it (never
hand-merge it) with::

    uv run python -m tests.unit.tools.sql.table_refs_snapshot --write

The file holds one query per line, sorted keys, so a diff lists only the
queries that changed.
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

from src.tools.database.collection_text import extract_json_object
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.sql.shadow import TableShadow

REPO = Path(__file__).resolve().parents[4]
SAMPLES = ("wordpress", "discourse", "trailgear")
BASELINE = Path(__file__).resolve().parent / "baselines" / "table_refs_snapshot.json"


def load_sample(name: str) -> dict:
    with zipfile.ZipFile(REPO / "docs" / "examples" / name / f"{name}.zip") as z:
        text = z.read(f"{name}-collection.json").decode("utf-8")
    data: dict = json.loads(extract_json_object(text, source=name))
    return data


def measure_sample(name: str) -> tuple[dict, dict[str, dict]]:
    """``(parsed collection, {query_id: facade facts})`` for one sample."""
    shadow = _RecordingShadow()
    parsed = parse_offline_collection(load_sample(name), shadow=shadow)
    return parsed, shadow.facts


def measure() -> dict[str, dict[str, dict]]:
    return {name: measure_sample(name)[1] for name in SAMPLES}


def dump_snapshot(snapshot: dict[str, dict[str, dict]]) -> str:
    lines = ["{"]
    for i, name in enumerate(sorted(snapshot)):
        lines.append(f"  {json.dumps(name)}: {{")
        rows = sorted(snapshot[name].items())
        for j, (qid, facts) in enumerate(rows):
            comma = "," if j < len(rows) - 1 else ""
            lines.append(f"    {json.dumps(qid)}: {json.dumps(facts, sort_keys=True)}{comma}")
        lines.append("  }" + ("," if i < len(snapshot) - 1 else ""))
    lines.append("}")
    return "\n".join(lines) + "\n"


class _RecordingShadow(TableShadow):
    """A TableShadow that also keeps every query's facade facts."""

    def __init__(self) -> None:
        super().__init__()
        self.facts: dict[str, dict] = {}

    def record(self, query_id, query_text, regex, facade, refs) -> None:  # type: ignore[no-untyped-def]
        super().record(query_id, query_text, regex, facade, refs)
        self.facts[query_id] = {
            "tables": facade,
            "parsed_by": refs.parsed_by,
            "fallback": refs.fallback_reason,
            "agrees": set(regex) == set(facade),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="rewrite the committed baseline")
    args = parser.parse_args(argv)
    text = dump_snapshot(measure())
    if args.write:
        BASELINE.write_text(text, encoding="utf-8")
        print(f"wrote {BASELINE.relative_to(REPO)}")
        return 0
    same = BASELINE.exists() and BASELINE.read_text(encoding="utf-8") == text
    print("snapshot unchanged" if same else "snapshot differs (run with --write)")
    return 0 if same else 1


if __name__ == "__main__":
    sys.exit(main())
