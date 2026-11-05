# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``database_size_gb`` from SQL Server is in GB, offline and live alike (#1872).

``sys.master_files.size`` counts 8 KB pages, so ``SUM(size) * 8.0 / 1024 / 1024``
is GB. The offline script divided once more (TB) and converted to BIGINT, so any
database under 1 TB read 0. There is no SQL Server here: the tests parse each
script's expression with sqlglot (tsql) and evaluate its arithmetic, with
``SUM(size)`` standing for a given page count.
"""

from __future__ import annotations

import json
import math
import re
import zipfile
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

from src.agents.collector.offline_ingest import build_offline_collector_output
from src.tools.data_movement.dms_sizing import database_gb
from src.tools.database.collection_text import extract_json_object
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.database.sqlserver_tools import SQLServerRemoteCollector

_REPO = Path(__file__).resolve().parents[3]
SCRIPT = _REPO / "scripts" / "collect-sqlserver.sql"
SAMPLE = _REPO / "docs" / "examples" / "trailgear" / "trailgear.zip"  # synthetic SQL Server
_INTEGER_TYPES = {
    exp.DataType.Type.BIGINT,
    exp.DataType.Type.INT,
    exp.DataType.Type.SMALLINT,
    exp.DataType.Type.TINYINT,
}
_GB = 1024 * 1024 / 8  # 8 KB pages in one GB


def _code(text: str) -> str:
    """``text`` without its ``--`` comments."""
    return "\n".join(line.split("--", 1)[0] for line in text.splitlines())


def _select_items(select_list: str) -> list[str]:
    """``select_list`` split at its top-level commas."""
    items, depth, start = [], 0, 0
    for i, ch in enumerate(select_list):
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if ch == "," and depth == 0:
            items.append(select_list[start:i])
            start = i + 1
    items.append(select_list[start:])
    return [item.strip() for item in items]


def _offline_expression() -> exp.Expression:
    """The offline script's ``database_size_gb`` projection, parsed as T-SQL."""
    code = _code(SCRIPT.read_text())
    body = re.search(
        r"SET @metadata = \(\s*SELECT(.*?)\n\s*FOR JSON PATH, WITHOUT_ARRAY_WRAPPER\s*\);",
        code,
        re.S,
    )
    assert body is not None
    (item,) = [i for i in _select_items(body.group(1)) if i.endswith("AS database_size_gb")]
    return sqlglot.parse_one(f"SELECT {item}", read="tsql").expressions[0]


def _live_expression() -> exp.Expression:
    """The live collector's ``size_gb`` projection, parsed as T-SQL."""
    seen: list[str] = []

    def query(sql: str) -> list[dict]:
        seen.append(sql)
        return []

    collector = SQLServerRemoteCollector.__new__(SQLServerRemoteCollector)
    collector._query = query  # type: ignore[method-assign]
    collector.get_database_size_gb()
    (sql,) = seen
    return sqlglot.parse_one(sql, read="tsql").expressions[0]


def _evaluate(node: exp.Expression, pages: int) -> float:
    """``node``'s value when ``SUM(size)`` is ``pages``, with T-SQL's integer truncation."""
    if isinstance(node, (exp.Alias, exp.Paren)):
        return _evaluate(node.this, pages)
    if isinstance(node, exp.Subquery):
        return _evaluate(node.this, pages)
    if isinstance(node, exp.Select):
        return _evaluate(node.expressions[0], pages)
    if isinstance(node, exp.Sum):
        assert node.this.name == "size", node.sql("tsql")
        return float(pages)
    if isinstance(node, exp.Literal):
        return float(node.this)
    if isinstance(node, exp.Mul):
        return _evaluate(node.this, pages) * _evaluate(node.expression, pages)
    if isinstance(node, exp.Div):
        return _evaluate(node.this, pages) / _evaluate(node.expression, pages)
    if isinstance(node, (exp.Cast, exp.Convert)):
        to = node.args["to"] if isinstance(node, exp.Cast) else node.this
        value = _evaluate(node.this if isinstance(node, exp.Cast) else node.expression, pages)
        if to.this in _INTEGER_TYPES:
            return float(math.trunc(value))
        if to.this == exp.DataType.Type.DECIMAL and len(to.expressions) == 2:
            return round(value, int(to.expressions[1].name))
        return value
    raise AssertionError(f"unexpected expression: {node.sql('tsql')}")


@pytest.mark.parametrize(
    ("pages", "gb"),
    [
        (int(2 * _GB), 2.0),
        (int(1.66 * _GB), 1.66),  # the DNN Platform sample's size
        (int(1536 * _GB), 1536.0),  # over 1 TB: still GB, not TB
    ],
)
def test_offline_database_size_is_gb(pages: int, gb: float) -> None:
    assert _evaluate(_offline_expression(), pages) == pytest.approx(gb, abs=1e-3)


def test_a_small_offline_database_is_not_zero() -> None:
    # 10 MB, under any whole number of GB: a fraction, never truncated to 0.
    assert _evaluate(_offline_expression(), 1280) == pytest.approx(10 / 1024)


@pytest.mark.parametrize("pages", [0, 1, 1280, 217_580, int(2 * _GB), int(5000 * _GB)])
def test_offline_and_live_report_the_same_size(pages: int) -> None:
    assert _evaluate(_offline_expression(), pages) == pytest.approx(
        _evaluate(_live_expression(), pages)
    )


@pytest.mark.passes_on_main
def test_live_database_size_is_gb() -> None:
    """The live collector was already right; it stays the reference."""
    assert _evaluate(_live_expression(), int(2 * _GB)) == pytest.approx(2.0)


@pytest.mark.passes_on_main
def test_a_for_json_float_reaches_the_dms_sizing() -> None:
    """FOR JSON writes a FLOAT in exponent notation: the offline ingest reads it as
    GB and the data-movement sizing uses it, with no Python change needed."""
    with zipfile.ZipFile(SAMPLE) as z:
        text = z.read("trailgear-collection.json").decode("utf-8")
    raw = re.sub(
        r'"database_size_gb":\s*27',
        '"database_size_gb":1.660156250000000e+000',
        extract_json_object(text),
    )
    assert "1.660156250000000e+000" in raw
    parsed = parse_offline_collection(json.loads(raw))
    output = build_offline_collector_output(
        parsed, job_id="t1872", database_name="trailgear", s3_key="trailgear-collection.json"
    )
    assert output["metadata"]["source_database"]["database_size_gb"] == pytest.approx(1.66015625)
    assert database_gb(output) == pytest.approx(1.66015625)
