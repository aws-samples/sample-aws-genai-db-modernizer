# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An offline collection's capture settings reach ``metadata.query_capture`` (#384).

Each collection script writes ``metadata.query_capture`` (``query_limit``, null when
there is no cap, and ``min_executions``). The ingest records them with how many
statements the script wrote: an uncapped run is ``truncated: false``, and a capped
run is truncated when the script wrote as many statements as the cap. A collection
from an older script records no settings, so whether it was cut stays unknown
(``truncated: null``).

Settings that are not to be trusted (a cap or a floor that is not a positive whole
number, or a missing floor) record no capture: a failed settings block must never read
as a complete capture of nothing. A script that says how many statements it selected
(``statements``, MySQL) records an error when it wrote fewer, as when the server's
``max_allowed_packet`` turned the statement list into NULL.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404 -- runs this repo's own script with fixed argv
import sys
import zipfile
from pathlib import Path

import pytest

from src.contracts.collector_output import CollectorOutputContract, QueryCapture
from src.shared.query_capture import offline_query_capture

REPO = Path(__file__).resolve().parents[3]


def test_an_uncapped_script_run_is_not_truncated():
    meta = {"query_capture": {"query_limit": None, "min_executions": 10}}
    assert offline_query_capture(meta, 5) == QueryCapture(
        query_limit=None, min_executions=10, captured=5, available=None, truncated=False
    )


@pytest.mark.parametrize(("rows", "truncated"), [(5, True), (6, True), (4, False)])
def test_a_capped_run_is_truncated_when_it_reached_the_cap(rows, truncated):
    meta = {"query_capture": {"query_limit": 5, "min_executions": 3}}
    capture = offline_query_capture(meta, rows)
    assert capture is not None
    assert (capture.query_limit, capture.min_executions, capture.truncated) == (5, 3, truncated)


def test_numbers_a_json_writer_spells_as_floats_are_read():
    meta = {"query_capture": {"query_limit": 20000.0, "min_executions": 10.0}}
    capture = offline_query_capture(meta, 1)
    assert capture is not None and (capture.query_limit, capture.min_executions) == (20000, 10)


@pytest.mark.parametrize("meta", [{}, {"query_capture": None}, {"query_capture": "x"}, None, []])
def test_a_collection_without_settings_records_none(meta):
    assert offline_query_capture(meta, 5) is None


@pytest.mark.parametrize("limit", [0, -5, True, "20000", 1.5])
def test_a_cap_that_is_not_a_positive_count_records_no_capture(limit):
    # The settings are not to be trusted: whether the run was cut is unknown.
    meta = {"query_capture": {"query_limit": limit, "min_executions": 10}}
    assert offline_query_capture(meta, 5) is None


@pytest.mark.parametrize("floor", [0, -1, None, "10", False, 2.5])
def test_a_floor_that_is_not_a_positive_count_records_no_capture(floor):
    # An Oracle settings block that failed leaves both settings null, and zero rows.
    meta = {"query_capture": {"query_limit": None, "min_executions": floor}}
    assert offline_query_capture(meta, 0) is None


def test_a_settings_block_with_no_floor_records_no_capture():
    assert offline_query_capture({"query_capture": {"query_limit": None}}, 0) is None


def test_a_statement_list_that_came_back_empty_records_an_error():
    meta = {"query_capture": {"query_limit": None, "min_executions": 10, "statements": 12}}
    capture = offline_query_capture(meta, 0)
    assert capture is not None
    assert capture.error is not None and "12" in capture.error
    assert capture.truncated is None and capture.captured == 0


def test_a_statement_list_written_whole_records_no_error():
    meta = {"query_capture": {"query_limit": 5, "min_executions": 10, "statements": 5}}
    capture = offline_query_capture(meta, 5)
    assert capture is not None and capture.error is None and capture.truncated is True


def test_no_statements_selected_is_a_complete_capture_of_none():
    meta = {"query_capture": {"query_limit": None, "min_executions": 10, "statements": 0}}
    capture = offline_query_capture(meta, 0)
    assert capture is not None and capture.error is None and capture.truncated is False


# -- through the ingest ------------------------------------------------------------------


def _wordpress(dest: Path, capture: dict | None) -> tuple[Path, int]:
    with zipfile.ZipFile(REPO / "docs" / "examples" / "wordpress" / "wordpress.zip") as z:
        z.extractall(dest)
    path = dest / "wordpress-collection.json"
    text = path.read_text()
    raw = json.loads(text[text.index("{") :])
    if capture is not None:
        raw["metadata"]["query_capture"] = capture
    else:
        # An older collector wrote no query_capture; the refreshed sample has one (#1955).
        raw["metadata"].pop("query_capture", None)
    path.write_text(json.dumps(raw))
    return path, len(raw["queries"])


def _collect(src: Path, root: Path) -> dict:
    proc = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script args
        [
            sys.executable,
            "scripts/run_collect.py",
            "--file",
            str(src),
            "--db",
            "sample",
            "--job-id",
            "off00001",
            "--artifact-root",
            str(root),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    stored: dict = json.loads(
        (root / "sample" / "off00001" / "collector" / "output.json").read_text()
    )
    return stored


def test_an_uncapped_script_collection_records_no_cap_and_no_truncation(tmp_path):
    src, _ = _wordpress(tmp_path / "in", {"query_limit": None, "min_executions": 10})
    stored = _collect(src, tmp_path / "a")
    capture = stored["metadata"]["query_capture"]
    assert capture["query_limit"] is None and capture["min_executions"] == 10
    assert capture["truncated"] is False
    assert capture["captured"] == len(stored["queries"]["query_patterns"])
    CollectorOutputContract.model_validate(stored)


def test_a_script_collection_that_reached_its_cap_is_truncated(tmp_path):
    _, rows = _wordpress(tmp_path / "probe", None)
    src, _ = _wordpress(tmp_path / "in", {"query_limit": rows, "min_executions": 2})
    capture = _collect(src, tmp_path / "a")["metadata"]["query_capture"]
    assert (capture["query_limit"], capture["min_executions"], capture["truncated"]) == (
        rows,
        2,
        True,
    )


@pytest.mark.passes_on_main
def test_an_older_collection_still_loads_with_its_truncation_unknown(tmp_path):
    src, _ = _wordpress(tmp_path / "in", None)
    capture = _collect(src, tmp_path / "a")["metadata"]["query_capture"]
    assert capture["query_limit"] is None and capture["truncated"] is None
    assert capture["min_executions"] is None


def test_the_offline_collector_mode_records_the_script_settings():
    from src.agents.collector.offline_ingest import build_offline_collector_output
    from src.tools.database.offline_parser import parse_offline_collection

    raw = {
        "metadata": {
            "version": "8.0.36",
            "database_name": "app",
            "query_capture": {"query_limit": None, "min_executions": 7},
        },
        "tables": [{"table_name": "t", "row_count": 1}],
        "columns": [
            {"table_name": "t", "column_name": "id", "data_type": "int", "is_nullable": "NO"}
        ],
        "queries": [{"digest": "d1", "query_text": "SELECT id FROM t", "execution_count": 9}],
    }
    out = build_offline_collector_output(
        parse_offline_collection(raw), job_id="j", database_name="app", s3_key="k"
    )
    assert out["metadata"]["query_capture"]["min_executions"] == 7
    assert out["metadata"]["query_capture"]["truncated"] is False


def test_a_mysql_collection_whose_statement_list_overflowed_records_the_error(tmp_path):
    src, _ = _wordpress(
        tmp_path / "in", {"query_limit": None, "min_executions": 10, "statements": 40}
    )
    raw = json.loads(src.read_text())
    raw["queries"] = []  # IFNULL(NULL, JSON_ARRAY()) after the overflow
    src.write_text(json.dumps(raw))
    stored = _collect(src, tmp_path / "a")
    capture = stored["metadata"]["query_capture"]
    assert "40" in capture["error"] and capture["truncated"] is None
    assert stored["queries"]["query_patterns"] == []
