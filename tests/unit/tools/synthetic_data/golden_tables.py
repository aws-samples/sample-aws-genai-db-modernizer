# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tables for the ``generate_rows`` golden (#1942): every column kind the generator
models, plus unmodelled ones, in both dialects' type names."""

from __future__ import annotations

from typing import Any


def _col(
    name: str, data_type: str, normalized: str | None = None, nullable: bool = False, **kw: Any
) -> dict[str, Any]:
    return {
        "column_name": name,
        "data_type": data_type,
        "normalized_data_type": normalized,
        "nullable": nullable,
        **kw,
    }


GOLDEN_TABLES: dict[str, dict[str, Any]] = {
    "mysql_kinds": {
        "table_id": "shop.kinds",
        "row_count": 30,
        "primary_key": ["id"],
        "columns": [
            _col("id", "bigint unsigned", "integer"),
            _col("i", "int", "integer", True),
            _col("ti", "tinyint(1)", "integer", True),
            _col("dec", "decimal(7,3)", "decimal", True, numeric_precision=7, numeric_scale=3),
            _col("f", "double", "float", True),
            _col("d", "date", "date"),
            _col("dt", "datetime", "datetime", True),
            _col("ts", "timestamp", "datetime"),
            _col("tm", "time", None, True),
            _col("y", "year", None, True),
            _col("b", "bit(1)", None, True),
            _col("j", "json", None, True),
            _col("s", "varchar(12)", "string", max_length=12),
            _col("c", "char(1)", "string", True, max_length=1),
            _col("t", "text", "text", True),
            _col("e", "enum('a','b')", "string", True),
            _col("g", "geometry", None, True),
        ],
    },
    "pg_kinds": {
        "table_id": "public.kinds",
        "row_count": None,
        "primary_key": ["code", "n"],
        "columns": [
            _col("code", "character varying(3)", "string", max_length=3),
            _col("n", "integer", "integer"),
            _col("big", "bigint", "integer", True),
            _col("num", "numeric", "decimal", True),
            _col("r", "real", "float", True),
            _col("ok", "boolean", None, True),
            _col("u", "uuid", None),
            _col("jb", "jsonb", None, True),
            _col("d", "date", "date", True),
            _col("ts", "timestamp without time zone", "datetime"),
            _col("tz", "timestamp with time zone", "datetime", True),
            _col("tt", "time without time zone", None, True),
            _col("txt", "text", "text", True),
            _col("iv", "interval", None, True),
            _col("arr", "integer[]", None, True),
        ],
    },
    "small_keys": {
        "table_id": "s.flags",
        "row_count": 100,
        "primary_key": ["flag"],
        "columns": [_col("flag", "boolean"), _col("v", "int", "integer", True)],
    },
}
# (dialect, seed, max_rows)
GOLDEN_CASES = [(d, s, m) for d in ("mysql", "postgresql") for s in (0, 1, 7) for m in (5, 20)]
