"""
Reusable Hypothesis strategies for DynamoDB analysis property-based testing.

Provides composable strategies for generating valid collector output dicts
with random tables, columns, PKs, FKs, indexes, and query patterns.
These strategies are used across all property tests in the enhanced
DynamoDB analysis spec.
"""

from __future__ import annotations

from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Primitive strategies
# ---------------------------------------------------------------------------

_column_name = st.from_regex(r"[a-z][a-z0-9_]{0,14}", fullmatch=True)
_schema_name = st.from_regex(r"[a-z]{2,6}", fullmatch=True)
_table_name = st.from_regex(r"[a-z][a-z0-9_]{1,12}", fullmatch=True)
_table_id = st.from_regex(r"[a-z]{2,6}\.[a-z_]{2,12}", fullmatch=True)
_index_name = st.from_regex(r"idx_[a-z]{2,10}", fullmatch=True)
_data_type = st.sampled_from(["int", "bigint", "varchar", "datetime", "decimal", "json", "text"])


# ---------------------------------------------------------------------------
# Table strategy
# ---------------------------------------------------------------------------


@st.composite
def table_strategy(
    draw: st.DrawFn,
    pk_column_count: st.SearchStrategy[int] = st.integers(min_value=0, max_value=4),
    extra_column_count: st.SearchStrategy[int] = st.integers(min_value=0, max_value=6),
    include_foreign_keys: bool = False,
    foreign_key_targets: list[str] | None = None,
    include_secondary_indexes: bool = False,
) -> dict:
    """Generate a single table dict with configurable PK column count.

    Args:
        pk_column_count: Strategy for the number of PK columns (0-4).
        extra_column_count: Strategy for additional non-PK columns.
        include_foreign_keys: Whether to generate FK constraints.
        foreign_key_targets: List of table names to reference in FKs.
        include_secondary_indexes: Whether to generate secondary indexes.
    """
    tid = draw(_table_id)
    tname = tid.split(".")[-1]
    n_pk = draw(pk_column_count)
    n_extra = draw(extra_column_count)

    # Generate unique column names
    all_col_names = draw(
        st.lists(
            _column_name, min_size=n_pk + max(n_extra, 1), max_size=n_pk + n_extra + 4, unique=True
        )
    )
    pk_cols = all_col_names[:n_pk]
    non_pk_cols = all_col_names[n_pk:]

    columns = []
    for i, c in enumerate(pk_cols + non_pk_cols):
        columns.append(
            {
                "column_name": c,
                "ordinal_position": i + 1,
                "data_type": draw(_data_type),
                "nullable": i >= n_pk and draw(st.booleans()),
            }
        )

    indexes = []
    if pk_cols:
        indexes.append(
            {
                "index_name": "PRIMARY",
                "columns": pk_cols,
                "is_unique": True,
                "is_primary": True,
                "index_type": "btree",
            }
        )

    # Optional secondary indexes on non-PK columns
    if include_secondary_indexes and non_pk_cols:
        n_sec = draw(st.integers(min_value=1, max_value=min(3, len(non_pk_cols))))
        for si in range(n_sec):
            idx_cols_count = draw(st.integers(min_value=1, max_value=min(3, len(non_pk_cols))))
            idx_cols = draw(
                st.lists(
                    st.sampled_from(non_pk_cols),
                    min_size=idx_cols_count,
                    max_size=idx_cols_count,
                    unique=True,
                )
            )
            indexes.append(
                {
                    "index_name": f"idx_{tname}_{si}",
                    "columns": idx_cols,
                    "is_unique": False,
                    "is_primary": False,
                    "index_type": "btree",
                }
            )

    table: dict = {
        "table_id": tid,
        "table_name": tname,
        "row_count": draw(st.integers(min_value=0, max_value=10_000_000)),
        "size_mb": round(draw(st.floats(min_value=0.01, max_value=1000.0)), 2),
        "columns": columns,
        "indexes": indexes,
        "primary_key": pk_cols,
    }

    # Optional foreign keys
    if include_foreign_keys and foreign_key_targets:
        fks = []
        # Pick 1-2 FK targets
        n_fks = draw(st.integers(min_value=1, max_value=min(2, len(foreign_key_targets))))
        chosen_targets = draw(
            st.lists(
                st.sampled_from(foreign_key_targets),
                min_size=n_fks,
                max_size=n_fks,
                unique=True,
            )
        )
        for target in chosen_targets:
            fk_col = draw(_column_name.filter(lambda c: c not in all_col_names))
            fks.append(
                {
                    "constraint_name": f"fk_{tname}_{target}",
                    "columns": [fk_col],
                    "referenced_table": target,
                    "referenced_columns": ["id"],
                }
            )
        table["foreign_keys"] = fks

    return table


# ---------------------------------------------------------------------------
# Query strategy
# ---------------------------------------------------------------------------


@st.composite
def query_strategy(
    draw: st.DrawFn,
    table_ids: list[str] | None = None,
    filter_columns: list[str] | None = None,
    sort_columns: list[str] | None = None,
    frequency_range: tuple[float, float] = (1.0, 10000.0),
    multi_table: bool = False,
) -> dict:
    """Generate a single query dict with configurable properties.

    Args:
        table_ids: Pool of table_ids to reference. If None, generates one.
        filter_columns: Pool of column names for filter_columns. If None, generates them.
        sort_columns: Pool of column names for sort_columns. If None, empty.
        frequency_range: (min, max) for frequency_per_hour.
        multi_table: If True, query accesses 2+ tables (co-access).
    """
    if table_ids is None:
        table_ids = [draw(_table_id)]

    if multi_table and len(table_ids) >= 2:
        n_tables = draw(st.integers(min_value=2, max_value=min(4, len(table_ids))))
        accessed = draw(
            st.lists(st.sampled_from(table_ids), min_size=n_tables, max_size=n_tables, unique=True)
        )
    else:
        accessed = [draw(st.sampled_from(table_ids))]

    freq = draw(st.floats(min_value=frequency_range[0], max_value=frequency_range[1]))

    # Filter columns
    if filter_columns is not None and filter_columns:
        n_filters = draw(st.integers(min_value=1, max_value=min(4, len(filter_columns))))
        f_cols = draw(
            st.lists(
                st.sampled_from(filter_columns),
                min_size=n_filters,
                max_size=n_filters,
                unique=True,
            )
        )
    else:
        f_cols = draw(st.lists(_column_name, min_size=0, max_size=3, unique=True))

    # Sort columns
    if sort_columns is not None and sort_columns:
        n_sorts = draw(st.integers(min_value=0, max_value=min(3, len(sort_columns))))
        s_cols = draw(
            st.lists(
                st.sampled_from(sort_columns),
                min_size=n_sorts,
                max_size=n_sorts,
                unique=True,
            )
        )
    else:
        s_cols = []

    query_type = draw(st.sampled_from(["SELECT", "INSERT", "UPDATE", "DELETE"]))
    query_id = f"q-{draw(st.integers(min_value=0, max_value=999999)):06d}"

    return {
        "query_id": query_id,
        "query_text": f"{query_type} ... FROM ...",
        "query_type": query_type,
        "frequency_per_hour": freq,
        "calls_per_second": freq / 3600.0,
        "tables_accessed": accessed,
        "rows_returned_avg": draw(st.floats(min_value=0.0, max_value=100000.0)),
        "filter_columns": f_cols,
        "sort_columns": s_cols,
        "execution_time_ms_avg": draw(st.floats(min_value=0.1, max_value=5000.0)),
    }


# ---------------------------------------------------------------------------
# Collector output strategy
# ---------------------------------------------------------------------------


@st.composite
def collector_output_strategy(
    draw: st.DrawFn,
    table_count: st.SearchStrategy[int] = st.integers(min_value=1, max_value=5),
    pk_column_count: st.SearchStrategy[int] = st.integers(min_value=0, max_value=4),
    query_count: st.SearchStrategy[int] = st.integers(min_value=0, max_value=8),
    include_foreign_keys: bool = False,
    include_secondary_indexes: bool = False,
    include_co_access_queries: bool = False,
) -> dict:
    """Generate a valid collector output dict with random tables and queries.

    Args:
        table_count: Strategy for number of tables.
        pk_column_count: Strategy for PK column count per table.
        query_count: Strategy for number of query patterns.
        include_foreign_keys: Whether tables may have FK constraints.
        include_secondary_indexes: Whether tables may have secondary indexes.
        include_co_access_queries: Whether to include multi-table queries.
    """
    n_tables = draw(table_count)

    # Pre-generate unique table IDs, then build tables around them
    unique_ids = draw(st.lists(_table_id, min_size=n_tables, max_size=n_tables, unique=True))

    tables: list[dict] = []
    for tid in unique_ids:
        t = draw(
            table_strategy(
                pk_column_count=pk_column_count,
                include_secondary_indexes=include_secondary_indexes,
            )
        )
        # Override with the pre-generated unique ID
        t["table_id"] = tid
        t["table_name"] = tid.split(".")[-1]
        tables.append(t)

    table_ids = [t["table_id"] for t in tables]

    # Optionally add FK relationships between tables
    if include_foreign_keys and len(tables) >= 2:
        for i in range(1, len(tables)):
            if draw(st.booleans()):
                target_name = tables[draw(st.integers(min_value=0, max_value=i - 1))]["table_name"]
                fk_col = f"fk_{target_name}_id"
                # Add FK column if not already present
                col_names = {c["column_name"] for c in tables[i]["columns"]}
                if fk_col not in col_names:
                    tables[i]["columns"].append(
                        {
                            "column_name": fk_col,
                            "ordinal_position": len(tables[i]["columns"]) + 1,
                            "data_type": "int",
                            "nullable": False,
                        }
                    )
                fks = tables[i].get("foreign_keys", [])
                fks.append(
                    {
                        "constraint_name": f"fk_{tables[i]['table_name']}_{target_name}",
                        "columns": [fk_col],
                        "referenced_table": target_name,
                        "referenced_columns": ["id"],
                    }
                )
                tables[i]["foreign_keys"] = fks

    # Generate queries
    n_queries = draw(query_count)
    all_non_pk_cols = []
    for t in tables:
        pk_set = set(t.get("primary_key", []))
        for c in t.get("columns", []):
            if c["column_name"] not in pk_set:
                all_non_pk_cols.append(c["column_name"])

    queries: list[dict] = []
    for _ in range(n_queries):
        q = draw(
            query_strategy(
                table_ids=table_ids,
                filter_columns=all_non_pk_cols if all_non_pk_cols else None,
                multi_table=include_co_access_queries
                and len(table_ids) >= 2
                and draw(st.booleans()),
            )
        )
        queries.append(q)

    return {
        "job_id": f"test-{draw(st.integers(min_value=1, max_value=999999)):06d}",
        "database_schema": {"tables": tables},
        "queries": {"query_patterns": queries},
    }
