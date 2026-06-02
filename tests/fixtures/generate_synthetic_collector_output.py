"""
Synthetic collector output generator for benchmarking Redis analysis at scale.

Generates realistic collector output data with configurable table/query counts
and deterministic seeding for reproducibility.

Usage as library:
    from tests.fixtures.generate_synthetic_collector_output import generate_collector_output
    data = generate_collector_output(num_tables=5000, num_queries=20000, seed=42)

Usage as CLI:
    python -m tests.fixtures.generate_synthetic_collector_output --tables 5000 --queries 20000
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from random import Random
from typing import Any

from tests.contract.conftest import create_column, create_query_pattern, create_table

# ---------------------------------------------------------------------------
# Query template factories (one per pattern type)
# ---------------------------------------------------------------------------

_SCHEMA_NAMES = ["app", "store", "analytics", "auth", "catalog"]
_ENGINES = ["mysql", "postgresql"]

_CACHING_TEMPLATES = [
    "SELECT {cols} FROM {table} WHERE {pk} = ?",
    "SELECT {cols} FROM {table} WHERE {pk} = ? AND is_active = 1",
    "SELECT {cols} FROM {table} WHERE {pk} IN (?, ?, ?)",
]

_SESSION_TEMPLATES = [
    "SELECT session_id, user_id, session_data FROM {table} WHERE session_id = ?",
    "SELECT * FROM {table} WHERE user_id = ? AND expires_at > NOW()",
    "INSERT INTO {table} (session_id, user_id, session_data) VALUES (?, ?, ?) ON DUPLICATE KEY UPDATE session_data = VALUES(session_data)",
]

_LEADERBOARD_TEMPLATES = [
    "SELECT {cols} FROM {table} ORDER BY score DESC LIMIT 50",
    "SELECT {cols} FROM {table} WHERE category_id = ? ORDER BY rating DESC LIMIT 25",
    "SELECT {cols} FROM {table} ORDER BY total_sold DESC LIMIT 100",
]

_TIMESERIES_TEMPLATES = [
    "SELECT DATE(created_at) AS d, COUNT(*) FROM {table} WHERE created_at >= ? GROUP BY DATE(created_at)",
    "SELECT DATE(updated_at) AS d, SUM(amount) FROM {table} WHERE updated_at >= ? GROUP BY DATE(updated_at)",
    "SELECT DATE(timestamp) AS d, AVG(value) FROM {table} WHERE timestamp >= ? GROUP BY DATE(timestamp)",
]

_ANTIPATTERN_TEMPLATES = [
    "SELECT * FROM {table}",
    "SELECT {cols} FROM {table} WHERE is_active = 1",
]

_PLAIN_SELECT_TEMPLATES = [
    "SELECT {cols} FROM {table} WHERE {pk} = ?",
    "SELECT {cols} FROM {table} WHERE status = ?",
    "SELECT COUNT(*) FROM {table} WHERE {pk} > ?",
]

_WRITE_TEMPLATES = [
    "INSERT INTO {table} ({cols}) VALUES (?)",
    "UPDATE {table} SET status = ? WHERE {pk} = ?",
    "DELETE FROM {table} WHERE {pk} = ? AND expired = 1",
]


def _zipf_weights(n: int, s: float = 1.0) -> list[float]:
    """Return Zipf-distributed weights for n items."""
    raw = [1.0 / math.pow(k, s) for k in range(1, n + 1)]
    total = sum(raw)
    return [w / total for w in raw]


def _weighted_choices(rng: Random, population: list, weights: list[float], k: int) -> list:
    """Weighted sampling with replacement using the given RNG."""
    cum = []
    running = 0.0
    for w in weights:
        running += w
        cum.append(running)
    results = []
    for _ in range(k):
        r = rng.random()
        for i, c in enumerate(cum):
            if r <= c:
                results.append(population[i])
                break
        else:
            results.append(population[-1])
    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_collector_output(
    num_tables: int = 100,
    num_queries: int = 1000,
    seed: int = 42,
    pattern_distribution: dict[str, float] | None = None,
) -> dict[str, Any]:
    """
    Generate a synthetic collector output dict matching CollectorOutputContract v3.0.

    Args:
        num_tables: Number of tables to generate.
        num_queries: Number of query patterns to generate.
        seed: Random seed for deterministic output.
        pattern_distribution: Mapping of pattern type to fraction of queries.
            Defaults to: caching=0.25, session=0.10, leaderboard=0.08,
            timeseries=0.05, antipattern=0.02, plain_select=0.30, write=0.20

    Returns:
        dict compatible with CollectorOutputContract v3.0 schema.
    """
    rng = Random(seed)  # nosec B311 — synthetic test data, not security/crypto

    dist = pattern_distribution or {
        "caching": 0.25,
        "session": 0.10,
        "leaderboard": 0.08,
        "timeseries": 0.05,
        "antipattern": 0.02,
        "plain_select": 0.30,
        "write": 0.20,
    }

    # --- Tables ---
    tables = _generate_tables(rng, num_tables)
    table_ids = [t["table_id"] for t in tables]

    # Zipf popularity distribution for table assignment
    zipf = _zipf_weights(num_tables, s=1.2)

    # --- Queries ---
    queries = _generate_queries(rng, num_queries, table_ids, zipf, dist)

    return {
        "contract_version": "3.0",
        "job_id": f"synthetic-{seed}",
        "metadata": {
            "collection_timestamp": "2026-02-16T00:00:00Z",
            "collector_version": "1.0.0",
            "collection_duration_seconds": 60.0,
            "source_database": {
                "engine": rng.choice(_ENGINES),
                "version": "8.0.35",
                "hostname": "synthetic-bench.local",
                "database_name": "bench_db",
                "database_size_gb": round(num_tables * 0.05, 1),
            },
        },
        "database_schema": {"tables": tables},
        "queries": {
            "query_patterns": queries,
            "total_queries_analyzed": num_queries * 100,
            "query_log_source": "performance_insights",
            "collection_start_time": "2026-02-15T00:00:00Z",
            "collection_end_time": "2026-02-16T00:00:00Z",
        },
        "metrics": {"performance_metrics": {}},
    }


# ---------------------------------------------------------------------------
# Internal generators
# ---------------------------------------------------------------------------


def _generate_tables(rng: Random, n: int) -> list[dict[str, Any]]:
    tables = []
    for i in range(n):
        schema = _SCHEMA_NAMES[i % len(_SCHEMA_NAMES)]
        tname = f"table_{i:05d}"
        tid = f"{schema}.{tname}"
        row_count = rng.randint(100, 5_000_000)
        size_mb = round(row_count * rng.uniform(0.0001, 0.001), 1)

        cols = [
            create_column(name="id", data_type="integer", nullable=False),
            create_column(
                name="name",
                data_type="varchar(255)",
                nullable=False,
                normalized_data_type="string",
                max_length=255,
            ),
            create_column(
                name="created_at",
                data_type="timestamp",
                nullable=False,
                normalized_data_type="timestamp",
            ),
            create_column(name="status", data_type="varchar(50)", nullable=True),
        ]

        tables.append(
            create_table(
                table_id=tid,
                table_name=tname,
                row_count=row_count,
                columns=cols,
                schema_name=schema,
                size_mb=size_mb,
                indexes=[
                    {
                        "index_name": "PRIMARY",
                        "columns": ["id"],
                        "is_unique": True,
                        "is_primary": True,
                        "index_type": "btree",
                    }
                ],
                primary_key=["id"],
            )
        )
    return tables


def _generate_queries(
    rng: Random,
    n: int,
    table_ids: list[str],
    zipf: list[float],
    dist: dict[str, float],
) -> list[dict[str, Any]]:
    """Generate n query patterns distributed across pattern types."""
    # Compute counts per pattern type
    pattern_types = list(dist.keys())
    fractions = [dist[p] for p in pattern_types]
    # Normalize fractions
    total_frac = sum(fractions)
    fractions = [f / total_frac for f in fractions]

    counts: dict[str, int] = {}
    remaining = n
    for i, pt in enumerate(pattern_types):
        if i == len(pattern_types) - 1:
            counts[pt] = remaining
        else:
            c = round(n * fractions[i])
            counts[pt] = c
            remaining -= c

    queries: list[dict[str, Any]] = []
    qid_counter = 0

    for pt in pattern_types:
        for _ in range(counts[pt]):
            table_id = _weighted_choices(rng, table_ids, zipf, k=1)[0]
            tname = table_id.split(".")[-1]
            qid_counter += 1
            qid = f"q{qid_counter:06d}"

            query_data = _make_query(rng, pt, qid, tname, table_id)
            queries.append(query_data)

    rng.shuffle(queries)
    return queries


def _make_query(
    rng: Random,
    pattern_type: str,
    qid: str,
    table_name: str,
    table_id: str,
) -> dict[str, Any]:
    """Build a single query pattern dict for the given pattern type."""
    cols = "id, name, status"
    pk = "id"
    fmt = {"table": table_name, "cols": cols, "pk": pk}

    if pattern_type == "caching":
        template = rng.choice(_CACHING_TEMPLATES).format(**fmt)
        return create_query_pattern(
            query_id=qid,
            query_text=template,
            frequency=rng.uniform(3600, 72000),
            tables=[table_id],
            query_type="SELECT",
            calls_per_second=round(rng.uniform(1.5, 20.0), 2),
            rows_returned_avg=round(rng.uniform(1, 50), 1),
            execution_time_ms_avg=round(rng.uniform(0.3, 5.0), 2),
        )

    if pattern_type == "session":
        template = rng.choice(_SESSION_TEMPLATES).format(**fmt)
        qt = "INSERT" if template.startswith("INSERT") else "SELECT"
        return create_query_pattern(
            query_id=qid,
            query_text=template,
            frequency=rng.uniform(1800, 54000),
            tables=[table_id],
            query_type=qt,
            calls_per_second=round(rng.uniform(0.5, 15.0), 2),
            rows_returned_avg=round(rng.uniform(1, 5), 1),
            execution_time_ms_avg=round(rng.uniform(0.2, 3.0), 2),
        )

    if pattern_type == "leaderboard":
        template = rng.choice(_LEADERBOARD_TEMPLATES).format(**fmt)
        return create_query_pattern(
            query_id=qid,
            query_text=template,
            frequency=rng.uniform(360, 7200),
            tables=[table_id],
            query_type="SELECT",
            calls_per_second=round(rng.uniform(0.1, 2.0), 2),
            rows_returned_avg=round(rng.uniform(10, 100), 1),
            execution_time_ms_avg=round(rng.uniform(2.0, 50.0), 2),
        )

    if pattern_type == "timeseries":
        template = rng.choice(_TIMESERIES_TEMPLATES).format(**fmt)
        return create_query_pattern(
            query_id=qid,
            query_text=template,
            frequency=rng.uniform(100, 3600),
            tables=[table_id],
            query_type="SELECT",
            calls_per_second=round(rng.uniform(0.05, 1.0), 2),
            rows_returned_avg=round(rng.uniform(10, 365), 1),
            execution_time_ms_avg=round(rng.uniform(5.0, 100.0), 2),
        )

    if pattern_type == "antipattern":
        template = rng.choice(_ANTIPATTERN_TEMPLATES).format(**fmt)
        return create_query_pattern(
            query_id=qid,
            query_text=template,
            frequency=rng.uniform(10, 100),
            tables=[table_id],
            query_type="SELECT",
            calls_per_second=round(rng.uniform(0.01, 0.1), 2),
            rows_returned_avg=round(rng.uniform(15000, 100000), 0),
            execution_time_ms_avg=round(rng.uniform(50.0, 500.0), 2),
        )

    if pattern_type == "write":
        template = rng.choice(_WRITE_TEMPLATES).format(**fmt)
        qt = "INSERT" if "INSERT" in template else ("UPDATE" if "UPDATE" in template else "DELETE")
        return create_query_pattern(
            query_id=qid,
            query_text=template,
            frequency=rng.uniform(100, 18000),
            tables=[table_id],
            query_type=qt,
            calls_per_second=round(rng.uniform(0.05, 5.0), 2),
            rows_returned_avg=0,
            execution_time_ms_avg=round(rng.uniform(1.0, 20.0), 2),
        )

    # plain_select (default)
    template = rng.choice(_PLAIN_SELECT_TEMPLATES).format(**fmt)
    return create_query_pattern(
        query_id=qid,
        query_text=template,
        frequency=rng.uniform(100, 5000),
        tables=[table_id],
        query_type="SELECT",
        calls_per_second=round(rng.uniform(0.05, 0.9), 2),
        rows_returned_avg=round(rng.uniform(1, 500), 1),
        execution_time_ms_avg=round(rng.uniform(1.0, 30.0), 2),
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate synthetic collector output for benchmarking"
    )
    parser.add_argument("--tables", type=int, default=100, help="Number of tables")
    parser.add_argument("--queries", type=int, default=1000, help="Number of query patterns")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    args = parser.parse_args()

    data = generate_collector_output(
        num_tables=args.tables,
        num_queries=args.queries,
        seed=args.seed,
    )
    json.dump(data, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
