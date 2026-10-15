"""The facade's per-query query-shape facts match the committed snapshot (#796).

A sqlglot bump or a facade change that alters any query's facts fails here;
regenerate with ``uv run python -m tests.unit.tools.sql.sql_facts_snapshot
--write`` and list the changed queries in the PR.
"""

import json

from tests.unit.tools.sql.sql_facts_snapshot import BASELINE, dump_snapshot, measure


def test_sql_facts_match_the_committed_snapshot():
    assert BASELINE.read_text(encoding="utf-8") == dump_snapshot(measure()), (
        "facade query facts changed; regenerate with "
        "`uv run python -m tests.unit.tools.sql.sql_facts_snapshot --write` "
        "and explain every changed query in the PR"
    )


def test_every_regex_difference_on_the_samples_is_a_known_correction():
    """Only the 21 discourse queries whose facts the regex misread (#796) differ."""
    snapshot = json.loads(BASELINE.read_text(encoding="utf-8"))
    differs = {
        (sample, qid): tuple(row["regex_differs"])
        for sample, queries in snapshot.items()
        for qid, row in queries.items()
        if row["regex_differs"]
    }
    assert {sample for sample, _ in differs} == {"discourse"}
    assert len(differs) == 21
