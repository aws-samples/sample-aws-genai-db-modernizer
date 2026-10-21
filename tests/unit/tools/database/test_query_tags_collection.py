"""Collectors fill ``QueryPattern.query_tags`` the same way (#1180).

The offline parser keeps the leading comment tags of every row merged into a
shape (not only the representative text's), the live collectors read them from
the text they hold the same way, and a collector output written before the field
existed still loads.
"""

from __future__ import annotations

import copy

import pytest

from src.agents.collector.mysql_collector import _build_queries, _merge_into_queries
from src.agents.collector.offline_ingest import build_offline_collector_output
from src.contracts.collector_output import CollectorOutputContract, QueryPattern
from src.shared.contract_versioning import current_version, load_contract
from src.tools.database.offline_parser import _transform_queries, parse_offline_collection
from src.tools.sql.query_tags import MAX_QUERY_TAGS
from tests.unit.tools.database.test_live_sql_parity import LIVE

SQL = "SELECT * FROM cart WHERE id = ?"


def _row(text: str, digest: str = "d1", total_time_ms: float = 1.0, **extra) -> dict:
    return {
        "digest": digest,
        "query_text": text,
        "execution_count": 1,
        "total_time_ms": total_time_ms,
        **extra,
    }


def test_every_merged_rows_tags_are_kept_in_first_seen_order() -> None:
    rows = [
        _row(f"/*controller='cart',action='add'*/ {SQL}", total_time_ms=1.0),
        _row(f"-- endpoint: CartController.Show\n{SQL}", total_time_ms=9.0),
        _row(f"/*controller='cart',action='add'*/ {SQL}", total_time_ms=2.0),
        _row(SQL, total_time_ms=3.0),  # a caller with no tag
    ]
    [p] = _transform_queries(rows, "shop", {"cart"}, source_engine="mysql")
    assert p["query_tags"] == ["controller='cart',action='add'", "endpoint: CartController.Show"]
    assert p["query_tags_dropped"] == 0
    # query_text (the costliest row's) and query_id are unchanged
    assert p["query_text"] == f"-- endpoint: CartController.Show\n{SQL}"
    assert p["query_id"] == "d1"
    assert p["query_type"] == "SELECT"


def test_rows_of_other_shapes_keep_their_own_tags() -> None:
    rows = [
        _row(f"/* a */ {SQL}", digest="d1"),
        _row(f"/* b */ {SQL}", digest="d2"),
        _row(SQL, "d3"),
    ]
    tags = {p["query_id"]: p["query_tags"] for p in _transform_queries(rows, "shop", {"cart"})}
    assert tags == {"d1": ["a"], "d2": ["b"], "d3": []}


def test_many_callers_of_one_shape_are_capped_and_counted() -> None:
    rows = [_row(f"-- endpoint: C{i}.Run\n{SQL}") for i in range(MAX_QUERY_TAGS + 5)]
    [p] = _transform_queries(rows, "shop", {"cart"}, source_engine="postgresql")
    assert p["query_tags"] == [f"endpoint: C{i}.Run" for i in range(MAX_QUERY_TAGS)]
    assert p["query_tags_dropped"] == 5


def test_a_trailing_sqlcommenter_comment_is_a_tag_and_free_text_is_not() -> None:
    [p] = _transform_queries([_row(f"{SQL} /*controller='cart'*/")], "shop", {"cart"})
    assert p["query_tags"] == ["controller='cart'"]
    assert p["query_text"] == f"{SQL} /*controller='cart'*/"
    [p] = _transform_queries([_row(f"{SQL} /* hot path */")], "shop", {"cart"})
    assert p["query_tags"] == []


def test_rows_merged_into_a_shape_keep_leading_and_trailing_tags_deduplicated() -> None:
    rows = [
        _row(f"/* batch */ {SQL} /*controller='cart'*/"),
        _row(f"{SQL} /*controller='cart'*/"),
        _row(f"{SQL} /*controller='cart',route='%2Fa'*/"),
    ]
    [p] = _transform_queries(rows, "shop", {"cart"}, source_engine="postgresql")
    assert p["query_tags"] == ["batch", "controller='cart'", "controller='cart',route='%2Fa'"]
    assert p["query_tags_dropped"] == 0


@pytest.mark.parametrize(
    "metadata, text, expected",
    [
        ({"version": "8.0.36"}, f"# svc: ordering\n{SQL}", ["svc: ordering"]),
        ({"engine": "postgresql"}, f"# svc: ordering\n{SQL}", []),
        ({"engine": "postgresql"}, f"/* a /* b */ c */ {SQL}", ["a /* b */ c"]),
        ({"version": "8.0.36"}, f"/* a /* b */ {SQL}", ["a /* b"]),
    ],
)
def test_the_collection_dialect_decides_what_a_comment_is(
    metadata: dict, text: str, expected: list[str]
) -> None:
    raw = {
        "metadata": {"database_name": "shop", **metadata},
        "tables": [{"table_name": "cart"}],
        "queries": [_row(text)],
    }
    [p] = parse_offline_collection(raw)["queries"]
    assert p["query_tags"] == expected


_PARITY = [
    ("mysql", f"/*controller='cart',action='add'*/ {SQL}"),
    ("mysql", f"# svc: ordering\n-- request: 42\n{SQL} /* trailing */"),
    ("mariadb", f"# svc: ordering\n{SQL}"),
    ("postgresql", f"-- event = hourly_job\n/* a /* b */ c */ {SQL}"),
    ("sqlserver", f"-- endpoint: CartController.Add\n/* x /* y */ z */ {SQL}"),
    ("oracle", f"/* insert com.shop.Cart */ {SQL}"),
    ("mysql", f"{SQL} /*controller='cart',route='%2Fa'*/"),
    ("postgresql", f"/* a */ {SQL} /*x='y /* z */'*/"),
    ("sqlserver", f"{SQL} /*x='y'*/"),
    ("oracle", f"{SQL} /*x='y'*/"),
    ("mysql", SQL),
]


@pytest.mark.parametrize(
    "engine, sql, live",
    [(e, s, live) for e, s in _PARITY for live in LIVE[e]],
    ids=lambda v: getattr(v, "__name__", None) or getattr(v, "__qualname__", None) or str(v)[:30],
)
def test_live_and_offline_collectors_read_the_same_tags(engine: str, sql: str, live) -> None:
    offline = _transform_queries([_row(sql)], "db", {"cart"}, source_engine=engine)[0]
    got = live(sql)
    assert bool(offline["query_tags"]) == (sql != SQL)
    assert (got["query_tags"], got["query_tags_dropped"]) == (
        offline["query_tags"],
        offline["query_tags_dropped"],
    )


def test_the_pattern_carries_the_tags_and_a_pi_merge_keeps_both() -> None:
    [p] = _build_queries(
        _transform_queries([_row(f"/* a */ {SQL}")], "shop", {"cart"}, source_engine="mysql")
    ).query_patterns
    assert (p.query_tags, p.query_tags_dropped) == (["a"], 0)
    live = _build_queries(
        _transform_queries([_row(f"/* a */ {SQL}")], "shop", {"cart"}, source_engine="mysql")
    )
    merged = _merge_into_queries(
        live,
        [
            {
                "query_id": "d1",
                "query_text": f"/* b */ /* a */ {SQL}",
                "query_tags": ["b", "a"],
                "query_tags_dropped": 1,
            }
        ],
    )
    [m] = merged.query_patterns
    assert (m.query_tags, m.query_tags_dropped) == (["a", "b"], 1)


def test_an_old_query_pattern_without_tags_loads() -> None:
    p = QueryPattern.model_validate(
        {"query_id": "q", "query_text": SQL, "frequency_per_hour": 1, "tables_accessed": ["cart"]}
    )
    assert (p.query_tags, p.query_tags_dropped) == ([], 0)


def test_a_collector_output_from_the_previous_minor_loads() -> None:
    """A saved collector output written before ``query_tags`` (the previous MINOR,
    no tag fields) loads, with no tags; the same file with them keeps them."""
    raw = {
        "metadata": {"database_name": "shop", "version": "8.0.36"},
        "tables": [{"table_name": "cart", "table_rows": 1}],
        "columns": [
            {"table_name": "cart", "column_name": "id", "data_type": "int", "is_nullable": "NO"}
        ],
        "queries": [_row(f"/* a */ {SQL}")],
    }
    new = build_offline_collector_output(
        parse_offline_collection(raw), job_id="job-1", database_name="shop", s3_key="k"
    )
    [q] = new["queries"]["query_patterns"]
    assert (q["query_tags"], q["query_tags_dropped"]) == (["a"], 0)

    major, minor = current_version(CollectorOutputContract).split(".")
    old = copy.deepcopy(new)
    old["contract_version"] = f"{major}.{int(minor) - 1}"
    for pattern in old["queries"]["query_patterns"]:
        del pattern["query_tags"], pattern["query_tags_dropped"]
    loaded = load_contract(CollectorOutputContract, old, artifact="collector/output.json")
    [p] = loaded.queries.query_patterns
    assert (p.query_tags, p.query_tags_dropped) == ([], 0)
    assert p.query_text == f"/* a */ {SQL}"
