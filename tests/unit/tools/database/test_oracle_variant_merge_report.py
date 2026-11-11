# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The Oracle literal-variant merge is reported, and `captured` stays the script's count (#2145).

An Oracle collection holds one row per literal variant of a statement; the parser merges
them into one query shape (#412), which routing counts. The merge loses no statement,
execution or elapsed time. It is reported on the collect status line and in the job log,
and ``metadata.query_capture.captured`` keeps the rows the script wrote, with the shapes
in ``query_shapes``, so a merge never reads as a capture cut short.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 -- runs this repo's own scripts with fixed argv
import sys
from pathlib import Path

import pytest

from src.agents.collector.offline_ingest import build_offline_collector_output
from src.contracts.collector_output import CollectorOutputContract
from src.shared.query_capture import cap_collection
from src.storage.local_store import LocalArtifactStore
from src.tools.database.offline_parser import parse_offline_collection

REPO = Path(__file__).resolve().parents[4]

# Three literal variants of one SELECT (one digest each, as a collection made without
# FORCE_MATCHING_SIGNATURE has), two of one UPDATE, and one statement that never varied.
_VARIANTS = [
    ("sql_a1", "SELECT id, total FROM orders WHERE id = 1", 10, 100.0),
    ("sql_a2", "SELECT id, total FROM orders WHERE id = 2", 20, 250.0),
    ("sql_a3", "SELECT id, total FROM orders WHERE id = 3", 30, 300.0),
    ("sql_b1", "UPDATE orders SET total = 5 WHERE id = 1", 40, 400.0),
    ("sql_b2", "UPDATE orders SET total = 6 WHERE id = 2", 50, 500.0),
    ("sql_c1", "SELECT name FROM customers WHERE active = 1", 60, 600.0),
]


def _row(digest: str, text: str, execs: int, total_ms: float, engine: str) -> dict:
    return {
        "digest": digest,
        "query_text": text,
        "execution_count": execs,
        "total_time_ms": total_ms,
        "avg_time_ms": total_ms / execs,
        "min_time_ms": 0,
        "max_time_ms": 0,
        "total_rows_sent": execs,
        "total_rows_examined": execs,
        "total_rows_affected": 0,
        "lock_time_ms": 0.0,
        "first_seen": "2026-01-10 00:00:00",
        "last_seen": "2026-01-11 00:00:00",
    }


def _collection(engine: str = "oracle", query_limit: int | None = None) -> dict:
    version = {
        "oracle": "Oracle Database 19c Enterprise Edition",
        "mysql": "8.0.36",
    }[engine]

    def column(table: str, name: str, position: int) -> dict:
        return {
            "table_name": table,
            "column_name": name,
            "data_type": "NUMBER",
            "column_type": "NUMBER",
            "is_nullable": "NO",
            "ordinal_position": position,
        }

    return {
        "metadata": {
            "engine": engine,
            "version": version,
            "database_name": "shop",
            "query_capture": {"query_limit": query_limit, "min_executions": 10},
        },
        "tables": [
            {"table_name": "orders", "schema_name": "shop", "row_count": 100},
            {"table_name": "customers", "schema_name": "shop", "row_count": 10},
        ],
        "columns": [
            column("orders", "id", 1),
            column("orders", "total", 2),
            column("customers", "id", 1),
            column("customers", "name", 2),
            column("customers", "active", 3),
        ],
        "queries": [_row(d, t, e, ms, engine) for d, t, e, ms in _VARIANTS],
    }


def _output(engine: str = "oracle") -> dict:
    parsed = parse_offline_collection(_collection(engine), database_name="shop")
    return build_offline_collector_output(parsed, job_id="j1", database_name="shop", s3_key="x")


def test_the_parser_counts_the_variants_it_merged() -> None:
    parsed = parse_offline_collection(_collection(), database_name="shop")
    assert parsed["script_query_rows"] == 6
    assert len(parsed["queries"]) == 3
    assert parsed["literal_variants_merged"] == 3


def test_captured_keeps_the_script_rows_and_the_shapes_are_recorded() -> None:
    out = _output()
    cap_collection(out)
    capture = out["metadata"]["query_capture"]
    assert capture["captured"] == 6
    assert capture["query_shapes"] == 3
    assert len(out["queries"]["query_patterns"]) == 3
    # A merge is not a cut: the capture is complete.
    assert capture["truncated"] is False
    assert capture["available"] is None
    CollectorOutputContract.model_validate(out)


@pytest.mark.passes_on_main  # guards totals the merge already kept
def test_executions_and_elapsed_time_survive_the_merge() -> None:
    parsed = parse_offline_collection(_collection(), database_name="shop")
    assert sum(q["execution_count"] for q in parsed["queries"]) == sum(v[2] for v in _VARIANTS)
    assert sum(q["total_time_ms"] for q in parsed["queries"]) == pytest.approx(
        sum(v[3] for v in _VARIANTS)
    )
    out = build_offline_collector_output(parsed, job_id="j1", database_name="shop", s3_key="x")
    patterns = out["queries"]["query_patterns"]
    assert sum(p["total_time_ms"] for p in patterns) == pytest.approx(sum(v[3] for v in _VARIANTS))


@pytest.mark.parametrize(
    ("limit", "cut", "kept"),
    [(3, False, 3), (2, True, 2), (4, False, 3)],
    ids=["limit-at-the-shape-count", "limit-below", "limit-above"],
)
def test_captured_counts_script_rows_whatever_the_cap(limit: int, cut: bool, kept: int) -> None:
    out = _output()
    result = cap_collection(out, limit)
    capture = out["metadata"]["query_capture"]
    assert result.cut is cut
    assert capture["captured"] == 6  # the rows the script wrote, with or without the cap
    assert capture["query_shapes"] == kept  # the shapes kept, which a cap cuts
    assert len(out["queries"]["query_patterns"]) == kept
    assert capture["truncated"] is cut
    assert capture["available"] == (3 if cut else None)


def _sqlserver_collection() -> dict:
    """Ten rows, four of them another database's; the six of ``shop`` hold two literal
    variants of one SELECT and two of one UPDATE (digest shared, as SQL Server's are)."""
    raw = _collection("oracle")
    raw["metadata"] = {
        "engine": "sqlserver",
        "version": "Microsoft SQL Server 2019",
        "database_name": "shop",
        "query_capture": {"query_limit": None, "min_executions": 10, "statements": 10},
    }
    own = [
        ("h1", "SELECT id FROM orders WHERE id = 1"),
        ("h1", "SELECT id FROM orders WHERE id = 2"),
        ("h2", "UPDATE orders SET total = 5 WHERE id = 1"),
        ("h2", "UPDATE orders SET total = 6 WHERE id = 2"),
        ("h3", "SELECT name FROM customers WHERE active = 1"),
        ("h4", "SELECT id, total FROM orders"),
    ]
    other = [(f"x{i}", f"SELECT {i} FROM elsewhere WHERE id = {i}") for i in range(4)]
    raw["queries"] = [
        {**_row(d, t, 10, 100.0, "sqlserver"), "database_name": "shop"} for d, t in own
    ] + [{**_row(d, t, 10, 100.0, "sqlserver"), "database_name": "other"} for d, t in other]
    return raw


def test_rows_of_other_databases_are_not_captured_statements() -> None:
    parsed = parse_offline_collection(_sqlserver_collection(), database_name="shop")
    out = build_offline_collector_output(parsed, job_id="j1", database_name="shop", s3_key="x")
    cap_collection(out)
    capture = out["metadata"]["query_capture"]
    assert capture["captured"] == 6  # shop's rows: the other database's four are not held
    assert capture["query_shapes"] == 4
    assert capture["truncated"] is False
    # The script's own selected count is still checked against everything it wrote.
    assert capture["error"] is None


def test_the_three_merge_counts_add_up_when_other_database_rows_are_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import scripts.run_assessment as run_assessment

    path = tmp_path / "shop-collection.json"
    path.write_text(json.dumps(_sqlserver_collection()), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    store = LocalArtifactStore(base_dir=str(tmp_path / "artifacts"))
    job_id, db = run_assessment.phase_collect(str(path), "shop", store)
    stdout = capsys.readouterr().out
    status = next(
        json.loads(line)
        for line in stdout.splitlines()
        if line.startswith("{") and '"phase": "collect"' in line
    )
    merge = status["query_merge"]
    assert merge["script_rows"] - merge["query_shapes"] == merge["literal_variants_merged"]
    assert (merge["script_rows"], merge["query_shapes"]) == (6, 4)
    assert status["queries_dropped_other_database"] == 4
    capture = store.read_json(f"{db}/{job_id}/collector/output.json")["metadata"]["query_capture"]
    assert capture["captured"] == merge["script_rows"]
    assert capture["query_shapes"] == merge["query_shapes"]
    assert "6 script rows -> 4 query shapes" in stdout


def test_the_atx_ingest_logs_the_merge(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    from src.atx_orchestrator.core import ingest_offline_collection

    store = LocalArtifactStore(base_dir=str(tmp_path / "artifacts"))
    with caplog.at_level("INFO", logger="src.atx_orchestrator.core"):
        out = ingest_offline_collection(store, "job1", "shop", _collection())
    assert "6 script rows -> 3 query shapes, 3 literal variants merged" in caplog.text
    assert out["metadata"]["query_capture"]["captured"] == 6


def test_mysql_without_variants_is_unaffected() -> None:
    raw = _collection("mysql")
    for i, row in enumerate(raw["queries"]):
        row["query_text"] = f"SELECT {i} FROM orders WHERE id = ?{' ' * i}"
        row["digest"] = f"d{i}"
    parsed = parse_offline_collection(raw, database_name="shop")
    assert parsed["literal_variants_merged"] == 0
    out = build_offline_collector_output(parsed, job_id="j1", database_name="shop", s3_key="x")
    cap_collection(out)
    capture = out["metadata"]["query_capture"]
    assert capture["captured"] == capture["query_shapes"] == 6


def test_the_summary_is_none_when_nothing_merged() -> None:
    from src.shared.query_capture import query_merge_summary

    assert query_merge_summary(6, 6) is None
    assert query_merge_summary(6, 3) == {
        "script_rows": 6,
        "query_shapes": 3,
        "literal_variants_merged": 3,
    }


def test_phase_collect_reports_the_merge_in_the_status_line_and_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import scripts.run_assessment as run_assessment

    path = tmp_path / "shop-collection.json"
    path.write_text(json.dumps(_collection()), encoding="utf-8")
    monkeypatch.chdir(tmp_path)  # phase_collect writes .modernizer-state.json here
    store = LocalArtifactStore(base_dir=str(tmp_path / "artifacts"))
    job_id, db = run_assessment.phase_collect(str(path), "shop", store)
    stdout = capsys.readouterr().out
    status = next(
        json.loads(line)
        for line in stdout.splitlines()
        if line.startswith("{") and '"phase": "collect"' in line
    )
    assert status["query_merge"] == {
        "script_rows": 6,
        "query_shapes": 3,
        "literal_variants_merged": 3,
    }
    assert status["queries"] == 3
    assert "6 script rows -> 3 query shapes, literal variants merged" in stdout
    capture = store.read_json(f"{db}/{job_id}/collector/output.json")["metadata"]["query_capture"]
    assert capture["captured"] == 6 and capture["query_shapes"] == 3


def test_run_collect_status_line_reports_the_merge(tmp_path: Path) -> None:
    path = tmp_path / "shop-collection.json"
    path.write_text(json.dumps(_collection()), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "MODERNIZER_CI_SANDBOX"}
    env.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    out = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script args
        [
            sys.executable,
            "scripts/run_collect.py",
            "--file",
            str(path),
            "--db",
            "shop",
            "--job-id",
            "merge0001",
            "--artifact-root",
            str(tmp_path / "artifacts"),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        env=env,
    )
    assert out.returncode == 0, out.stderr[-800:]
    status = json.loads(out.stdout.strip().splitlines()[-1])
    assert status["query_merge"]["literal_variants_merged"] == 3
    assert status["queries"] == 3
