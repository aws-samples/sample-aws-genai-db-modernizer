"""The facade's per-query table references match the committed snapshot (#557).

A sqlglot bump or a facade change that alters any query's answer fails here;
regenerate with ``uv run python -m tests.unit.tools.sql.table_refs_snapshot
--write`` and list the changed queries in the PR.
"""

from tests.unit.tools.sql.table_refs_snapshot import BASELINE, dump_snapshot, measure


def test_table_refs_match_the_committed_snapshot():
    assert BASELINE.read_text(encoding="utf-8") == dump_snapshot(measure()), (
        "facade table references changed; regenerate with "
        "`uv run python -m tests.unit.tools.sql.table_refs_snapshot --write` "
        "and explain every changed query in the PR"
    )
