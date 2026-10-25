"""A query is listed as served by one query group, its owner's (#1522).

After #226 the cache layer's groups stand apart; three other ways a query could still be
listed by two groups remain: two of one design's own access patterns citing it under
different ``pattern_group`` names, a design citing a query the assignment routed to
another engine, and an assignment with several rows for one query (one per table).
Every other pattern that cites the query still carries it in ``query_ids``: the link.
"""

from __future__ import annotations

from collections import Counter

import pytest

from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_report import build_query_groups

QUERIES = [
    {
        "query_id": "q1",
        "query_text": "SELECT * FROM wp_options WHERE option_name = ?",
        "query_type": "SELECT",
        "calls_per_second": 9.0,
        "tables_accessed": ["wp.wp_options"],
    },
    {
        "query_id": "q-aurora",
        "query_text": "SELECT * FROM wp_posts WHERE ID = ?",
        "query_type": "SELECT",
        "calls_per_second": 4.0,
        "tables_accessed": ["wp.wp_posts"],
    },
    {
        "query_id": "q-join",
        "query_text": "SELECT * FROM wp_posts p JOIN wp_postmeta m ON m.post_id = p.ID",
        "query_type": "SELECT",
        "calls_per_second": 2.0,
        "tables_accessed": ["wp.wp_posts", "wp.wp_postmeta"],
    },
]

ROWS = [
    {"query_id": "q1", "assigned_engine": "dynamodb", "source_tables": ["wp.wp_options"]},
    {"query_id": "q-aurora", "assigned_engine": "aurora_mysql", "source_tables": ["wp.wp_posts"]},
]


def _pattern(pid: str, qid: str, group: str | None = None, table: str = "wp.wp_options"):
    ap = {"pattern_id": pid, "source_query_ids": [qid], "source_tables": [table]}
    if group:
        ap["pattern_group"] = group
    return {**ap, "design_rps": 1.0}


def _data(designs: dict[str, list[dict]], rows: list[dict] = ROWS) -> SynthesisData:
    return SynthesisData(
        job_id="test",
        database_name="wp",
        collector={"queries": {"query_patterns": QUERIES}},
        engines={
            e: EngineArtifacts(engine=e, schema_design={"access_patterns": aps})
            for e, aps in designs.items()
        },
        assignment={"query_assignments": [{"in_scope": True, **r} for r in rows]},
    )


def _served(groups: list[dict]) -> Counter[str]:
    return Counter(
        sq["query_id"] for g in groups for sq in g["source_queries"] if not sq.get("cached_read")
    )


def _listing(groups: list[dict], qid: str) -> list[dict]:
    return [g for g in groups if any(sq["query_id"] == qid for sq in g["source_queries"])]


def _citing(groups: list[dict], qid: str) -> list[str]:
    return sorted(
        g["group_name"]
        for g in groups
        if any(qid in (ap.get("query_ids") or []) for ap in g["access_patterns"])
    )


def test_two_pattern_groups_of_one_design_list_the_query_once() -> None:
    groups = build_query_groups(
        _data({"dynamodb": [_pattern("DDB-AP-1", "q1", "A"), _pattern("DDB-AP-2", "q1", "B")]})
    )
    assert _served(groups)["q1"] == 1
    (home,) = _listing(groups, "q1")
    assert home["group_name"] == "A"
    # The other pattern still cites it, and the listing links to both patterns.
    assert _citing(groups, "q1") == ["A", "B"]
    (sq,) = [sq for sq in home["source_queries"] if sq["query_id"] == "q1"]
    assert sq["linked_patterns"] == ["DDB-AP-1", "DDB-AP-2"]


def test_a_query_another_engine_owns_is_listed_by_its_owner_only() -> None:
    """DocumentDB's design cites the Aurora query; Aurora has no design yet, so its
    group comes from the assignment (the relational branch)."""
    groups = build_query_groups(
        _data(
            {
                "dynamodb": [_pattern("DDB-AP-1", "q1")],
                "documentdb": [_pattern("DOC-AP-1", "q-aurora", "Posts", "wp.wp_posts")],
            }
        )
    )
    assert _served(groups)["q-aurora"] == 1
    (home,) = _listing(groups, "q-aurora")
    assert home["engines"] == ["aurora_mysql"]
    posts = next(g for g in groups if g["group_name"] == "Posts")
    assert posts["source_queries"] == []
    assert posts["access_patterns"][0]["query_ids"] == ["q-aurora"]


def test_a_designed_owner_lists_it_whichever_design_comes_first() -> None:
    groups = build_query_groups(
        _data(
            {
                "documentdb": [_pattern("DOC-AP-1", "q1", "Docs")],
                "dynamodb": [_pattern("DDB-AP-1", "q1", "Options")],
            }
        )
    )
    (home,) = _listing(groups, "q1")
    assert home["group_name"] == "Options"
    (sq,) = home["source_queries"]
    # Only the owner's patterns are linked: DocumentDB does not serve it.
    assert sq["linked_patterns"] == ["DDB-AP-1"]


def test_a_query_no_owner_group_lists_stays_listed_once() -> None:
    """The owner's design does not cite it: it is never dropped from every group."""
    groups = build_query_groups(
        _data(
            {
                "dynamodb": [
                    _pattern("DDB-AP-1", "q-aurora", "X"),
                    _pattern("DDB-AP-2", "q-aurora", "Y"),
                ],
                "aurora_mysql": [_pattern("AUR-AP-1", "q1", "Z")],
            }
        )
    )
    assert _served(groups)["q-aurora"] == 1
    assert [g["group_name"] for g in _listing(groups, "q-aurora")] == ["X"]


def test_several_assignment_rows_for_one_query_make_one_relational_entry() -> None:
    """One row per table the query touches (older AdventureWorks runs): one group named
    by every table, the query and its calls per second counted once."""
    rows = ROWS + [
        {"query_id": "q-join", "assigned_engine": "aurora_mysql", "source_tables": [t]}
        for t in ("wp.wp_posts", "wp.wp_postmeta")
    ]
    groups = build_query_groups(_data({"dynamodb": [_pattern("DDB-AP-1", "q1")]}, rows))
    assert _served(groups)["q-join"] == 1
    (home,) = _listing(groups, "q-join")
    assert home["group_name"] == "wp.wp_postmeta, wp.wp_posts"
    assert [ap["query_ids"] for ap in home["access_patterns"]] == [["q-join"]]
    assert home["total_design_rps"] == 2.0
    assert _citing(groups, "q-join") == ["wp.wp_postmeta, wp.wp_posts"]


@pytest.mark.passes_on_main  # guards what must not change
def test_queries_with_one_listing_keep_their_groups() -> None:
    groups = build_query_groups(
        _data({"dynamodb": [_pattern("DDB-AP-1", "q1", "A"), _pattern("DDB-AP-2", "q1", "A")]})
    )
    assert _served(groups) == {"q1": 1, "q-aurora": 1}
    (home,) = _listing(groups, "q1")
    assert home["source_queries"][0]["linked_patterns"] == ["DDB-AP-1", "DDB-AP-2"]
