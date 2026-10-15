"""Per-query regression snapshot of the sqlglot facade's query-shape facts (#796).

For every query in the bundled samples (wordpress, discourse, trailgear) this
records the facade's facts (:class:`src.tools.sql.facade.SqlFacts`), how they
were read (``parsed_by``, the fallback reason) and which of them differ from
the regex scans the collectors used before #796 (``regex_differs``).
``test_sql_facts_snapshot.py`` compares a fresh run with
``baselines/sql_facts_snapshot.json``, so a sqlglot version bump, or a facade
change, shows exactly which queries' facts changed. Regenerate it (never
hand-merge it) with::

    uv run python -m tests.unit.tools.sql.sql_facts_snapshot --write

The file holds one query per line, sorted keys, so a diff lists only the
queries that changed. It shares its format and sample loading with the table
references snapshot (``table_refs_snapshot.py``).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.tools.database.offline_parser import detect_source_engine, parse_offline_collection
from src.tools.sql import facade, regex_facts
from tests.unit.tools.sql.table_refs_snapshot import REPO, SAMPLES, dump_snapshot, load_sample

BASELINE = Path(__file__).resolve().parent / "baselines" / "sql_facts_snapshot.json"

_REGEX = {
    "join_count": regex_facts.regex_join_count,
    "has_joins": regex_facts.regex_has_joins,
    "has_aggregation": regex_facts.regex_has_aggregation,
    "has_subquery": regex_facts.regex_has_subquery,
    "wildcard_leading": regex_facts.regex_wildcard_leading,
}


def measure_sample(name: str) -> dict[str, dict]:
    """``{query_id: facts}`` for one sample, read the way the offline parser reads it."""
    data = load_sample(name)
    engine, _port = detect_source_engine(data.get("metadata"))
    rows: dict[str, dict] = {}
    for query in parse_offline_collection(data)["queries"]:
        text = query["query_text"]
        facts = facade.sql_facts(text, engine)
        # The collector fields are these facts, read once by the parser.
        assert (
            query["has_joins"],
            query["join_count"],
            query["has_aggregations"],
            query["has_subqueries"],
        ) == (facts.has_joins, facts.join_count, facts.has_aggregation, facts.has_subquery)
        rows[str(query["query_id"])] = {
            "facts": {name: getattr(facts, name) for name in _REGEX},
            "parsed_by": facts.parsed_by,
            "fallback": facts.fallback_reason,
            "regex_differs": sorted(n for n, fn in _REGEX.items() if fn(text) != getattr(facts, n)),
        }
    return rows


def measure() -> dict[str, dict[str, dict]]:
    return {name: measure_sample(name) for name in SAMPLES}


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
