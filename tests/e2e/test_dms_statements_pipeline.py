# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A collection taken during an AWS DMS task: the whole pipeline leaves DMS's statements
out and says so where you read the results (#2375).

The WordPress (MySQL) sample gets two synthetic writes to DMS's heartbeat table. The run
must route only the sample's own statements, and the ``collect`` line, ``triage.json``,
the approval gate and both reports must state the two left out, once. The Discourse
(PostgreSQL) sample, with a role id on every statement, gets DMS's heartbeat update, a
slot read and a full-load cursor pair under DMS's role: the role rules leave out all four
and keep every statement of the sample.
"""

from __future__ import annotations

import copy
import json
import subprocess  # nosec B404 -- runs this repo's own print_summary.py with fixed argv
import sys
import zipfile
from typing import Any

import pytest

from src.report.facts import report_facts
from src.tools.database.collection_text import extract_json_object
from src.tools.database.offline_parser import parse_offline_collection
from tests.e2e.deliverable_figures import md_plain
from tests.e2e.pipeline import REPO, PipelineResult, run_pipeline_from_file
from tests.executive_pdf import executive_text

pytestmark = pytest.mark.deterministic

STATEMENT = (
    "2 statements from AWS DMS, with 30 executions, were left out of the workload; "
    "referee-triage/triage.json lists them"
)


def _collection() -> dict[str, Any]:
    with zipfile.ZipFile(REPO / "docs" / "examples" / "wordpress" / "wordpress.zip") as z:
        member = next(n for n in z.namelist() if n.endswith("-collection.json"))
        data: dict[str, Any] = json.loads(extract_json_object(z.read(member).decode("utf-8")))
    template = data["queries"][0]
    for digest, text, calls in (
        ("dms-heartbeat-1", "UPDATE `awsdms_heartbeat` SET `hb_timestamp` = ? WHERE `hb_id` = ?", 20),
        ("dms-heartbeat-2", "INSERT INTO `awsdms_heartbeat` ( `hb_timestamp` ) VALUES ( ? )", 10),
    ):  # fmt: skip
        row = copy.deepcopy(template)
        row.update({"digest": digest, "query_text": text, "execution_count": calls})
        data["queries"].append(row)
    return data


@pytest.fixture(scope="module")
def dms_run(tmp_path_factory: pytest.TempPathFactory) -> PipelineResult:
    path = tmp_path_factory.mktemp("dms-collection") / "wordpress-collection.json"
    path.write_text(json.dumps(_collection()))
    root = tmp_path_factory.mktemp("dms-artifacts")
    return run_pipeline_from_file(path, "wordpress", root, "dmsrun01")


def test_the_pipeline_routes_only_the_application(dms_run: PipelineResult) -> None:
    assert dms_run.steps["collect"]["dms_statements_excluded"] == 2
    collector = json.loads((dms_run.job_dir() / "collector" / "output.json").read_text())
    ids = {q["query_id"] for q in collector["queries"]["query_patterns"]}
    assert not ids & {"dms-heartbeat-1", "dms-heartbeat-2"}
    triage = json.loads((dms_run.job_dir() / "referee-triage" / "triage.json").read_text())
    assert triage["excluded_statements"]["rules"][0]["query_ids"] == [
        "dms-heartbeat-1",
        "dms-heartbeat-2",
    ]


def test_the_gate_and_both_reports_state_it_once(dms_run: PipelineResult) -> None:
    gate = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script
        [sys.executable, "scripts/print_summary.py", "--job-id", dms_run.job_id, "--db",
         dms_run.db, "--gate", "--artifact-root", str(dms_run.artifact_root)],
        cwd=REPO, capture_output=True, text=True, check=True, timeout=120,
    ).stdout  # fmt: skip
    assert gate.count(f"{STATEMENT}.") == 1
    report = json.loads(dms_run.report_json().read_text())
    assert report_facts(report)["excluded_statements"]["statement"] == STATEMENT
    line = f"Statements left out: {STATEMENT}."
    md = md_plain(dms_run.deliverable("engineering-report.md").read_text(encoding="utf-8"))
    assert md.count(line) == 1
    assert " ".join(executive_text(report).split()).count(line) == 1


# --- PostgreSQL: the role rules end to end -------------------------------------------

APP_ROLE, DMS_ROLE = "16384", "16385"
PG_DMS_ROWS = (
    ("dms-pg-1", 'UPDATE "public"."awsdms_heartbeat" SET "hb_timestamp" = $1 WHERE "hb_id" = $2', 20),
    ("dms-pg-2", "select * from pg_logical_slot_get_changes($1, NULL, $2, $3, $4)", 30),
    ("dms-pg-3", 'declare "SQL_CUR0x5a1b2c3d4e50" cursor with hold for select "id", "name" '
                 'from "public"."badges"', 1),
    ("dms-pg-4", 'fetch 10000 in "SQL_CUR0x5a1b2c3d4e50"', 29),
)  # fmt: skip


def _discourse() -> dict[str, Any]:
    with zipfile.ZipFile(REPO / "docs" / "examples" / "discourse" / "discourse.zip") as z:
        member = next(n for n in z.namelist() if n.endswith("-collection.json"))
        data: dict[str, Any] = json.loads(extract_json_object(z.read(member).decode("utf-8")))
    for row in data["queries"]:
        row["role_id"] = APP_ROLE
    template = data["queries"][0]
    for digest, text, calls in PG_DMS_ROWS:
        row = copy.deepcopy(template)
        row.update({"digest": digest, "query_text": text, "execution_count": calls})
        row["role_id"] = DMS_ROLE
        data["queries"].append(row)
    return data


@pytest.fixture(scope="module")
def pg_dms_run(tmp_path_factory: pytest.TempPathFactory) -> PipelineResult:
    path = tmp_path_factory.mktemp("pg-dms-collection") / "discourse-collection.json"
    path.write_text(json.dumps(_discourse()))
    root = tmp_path_factory.mktemp("pg-dms-artifacts")
    return run_pipeline_from_file(path, "discourse", root, "pgdmsrun")


def test_the_postgresql_role_rules_leave_out_dms_and_keep_the_sample(
    pg_dms_run: PipelineResult,
) -> None:
    assert pg_dms_run.steps["collect"]["dms_statements_excluded"] == 4
    triage = json.loads((pg_dms_run.job_dir() / "referee-triage" / "triage.json").read_text())
    rules = {r["rule"]: r["query_ids"] for r in triage["excluded_statements"]["rules"]}
    assert rules == {
        "dms_object": ["dms-pg-1"],
        "dms_replication_slot": ["dms-pg-2"],
        "dms_cursor": ["dms-pg-3", "dms-pg-4"],
    }
    collector = json.loads((pg_dms_run.job_dir() / "collector" / "output.json").read_text())
    ids = [q["query_id"] for q in collector["queries"]["query_patterns"]]
    # Every statement of the sample stays: the same shapes the sample alone parses to.
    sample = _discourse()
    sample["queries"] = [q for q in sample["queries"] if q["role_id"] == APP_ROLE]
    expected = {q["query_id"] for q in parse_offline_collection(sample)["queries"]}
    # Compared as sets with a short message: a diff of ~1,600 ids takes pytest minutes.
    missing, extra = expected - set(ids), set(ids) - expected
    assert (len(missing), len(extra)) == (0, 0), (sorted(missing)[:5], sorted(extra)[:5])
    # Discourse has a column named role_id, so the key is checked, not the text.
    assert not any("role_id" in q for q in collector["queries"]["query_patterns"])
    report = json.loads(pg_dms_run.report_json().read_text())
    assert report_facts(report)["excluded_statements"]["statement_count"] == 4
