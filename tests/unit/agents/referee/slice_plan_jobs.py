# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A small hand-built job for the slice plan's unit tests (#1129, #1698).

Aurora MySQL keeps ``app.orders``; DynamoDB owns ``app.carts`` and ``app.cart_items``
(one unit: a query joins them, and Aurora still reads ``app.carts``), ``app.sessions``
(self-contained) and ``app.events`` (DocumentDB reads it); a DynamoDB read of
``app.products`` (nobody writes it) reads a copy; DocumentDB owns ``app.profiles``.
"""

from __future__ import annotations

from typing import Any

from src.agents.referee.write_groups import sources_of_truth
from src.shared.move_units import SliceInputs, SliceQuery

# query id -> (engine, tables, calls per second, cache engine)
QUERIES: dict[str, tuple[str, tuple[str, ...], float, str | None]] = {
    "q01": ("aurora_mysql", ("app.orders",), 10.0, None),
    "q02": ("aurora_mysql", ("app.orders",), 2.0, None),
    "q03": ("dynamodb", ("app.carts",), 4.0, None),
    "q04": ("dynamodb", ("app.cart_items", "app.carts"), 8.0, "elasticache"),
    "q05": ("dynamodb", ("app.cart_items",), 1.0, None),
    "q06": ("dynamodb", ("app.products",), 6.0, "elasticache"),
    "q07": ("dynamodb", ("app.events",), 3.0, None),
    "q08": ("dynamodb", ("app.events",), 1.0, None),
    "q09": ("aurora_mysql", ("app.carts",), 2.0, None),
    "q10": ("documentdb", ("app.profiles",), 1.0, None),
    "q11": ("documentdb", ("app.events", "app.profiles"), 1.0, None),
    "q12": ("dynamodb", ("app.sessions",), 5.0, None),
    "q13": ("dynamodb", ("app.sessions",), 5.0, None),
}
WRITE_GROUPS = {
    "app.orders": ("q02",),
    "app.carts": ("q03",),
    "app.cart_items": ("q05",),
    "app.events": ("q08",),
    "app.profiles": ("q10",),
    "app.sessions": ("q13", "q12"),
}
TABLES = [
    "app.cart_items",
    "app.carts",
    "app.events",
    "app.orders",
    "app.products",
    "app.profiles",
    "app.sessions",
    "app.unused",
]


def inputs(**overrides: Any) -> SliceInputs:
    queries = tuple(
        SliceQuery(query_id=q, engine=e, tables=t, calls_per_second=c, cache_engine=cache)
        for q, (e, t, c, cache) in QUERIES.items()
    )
    fields: dict[str, Any] = {
        "queries": queries,
        "writes_by_table": WRITE_GROUPS,
        "co_dependency_groups": (("q04", "q05"),),
        "foreign_keys": (("app.cart_items", "app.carts"),),
    }
    fields.update(overrides)
    engine_of = {q.query_id: q.engine for q in fields["queries"]}
    fields.setdefault("owners", sources_of_truth(fields["writes_by_table"], engine_of))
    return SliceInputs(**fields)


def waves(*, homogeneity: str = "homogeneous") -> list[dict[str, Any]]:
    return [
        {
            "wave": 1,
            "engines": ["aurora_mysql"],
            "moves_from": ["mysql"],
            "homogeneity": homogeneity,
            "tables": TABLES,
            "query_count": 3,
            "cutover_query_count": len(QUERIES),
        },
        {"wave": 2, "engines": ["elasticache"], "fronts": "aurora_mysql", "tables": []},
        {"wave": 3, "engines": ["dynamodb", "documentdb"], "tables": TABLES[:7]},
    ]


COSTS = {"aurora_mysql": 100.0, "elasticache": 50.0, "dynamodb": 20.0, "documentdb": 30.0}
REPORT: dict[str, Any] = {"engines_in_use": ["mysql"]}
