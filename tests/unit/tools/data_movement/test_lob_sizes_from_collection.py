# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A MySQL collection that measured its LOB columns sizes step 1 with no lob-sizes.sql (#2123).

Since #2128 ``collect-mysql.sql`` prints a second object, ``{"lob_sizes": [...]}``, which the
ingest merges into ``columns`` as ``max_value_bytes``. From the collection file to step 1's
files: every measured MySQL LOB table plans as ``measured``, no ``lob-sizes.sql`` is written and
preflight's ``lob_sizes`` check passes; a column the collection could not measure (null) is
unmeasured, so the script is written for it and preflight stops.
"""

from __future__ import annotations

import json
from typing import Any

from src.agents.collector import mysql_collector
from src.contracts.collector_input import CollectorInput
from src.storage.local_store import LocalArtifactStore
from src.tools.data_movement.dms_preflight import check_lob_sizes
from src.tools.data_movement.dms_step import load_step1, write_step1_delivery
from src.tools.data_movement.plan import write_plans
from src.tools.database.offline_collection_check import read_collection_text
from src.tools.transformation_contract.write import write_step1
from tests.unit.assessment.conftest import sample_collection

LOB_TYPES = {"mediumtext", "longtext", "mediumblob", "longblob", "json"}


class _Checkpoints:
    """Every stage but the AWS metrics (no AWS)."""

    def load_or_run(self, name: str, fn: Any) -> Any:
        return {} if name == "aws_metrics" else fn()


def _collection_text(null_for: tuple[str, str] | None = None) -> tuple[str, list[tuple[str, str]]]:
    """The WordPress sample as collect-mysql.sql prints it now: the collection, then the
    ``lob_sizes`` object; ``null_for`` (table, column) is a column it could not measure."""
    raw = sample_collection("wordpress")
    lobs = [c for c in raw["columns"] if str(c.get("data_type")).lower() in LOB_TYPES]
    sizes = [{"table_name": c["table_name"], "ordinal_position": c["ordinal_position"],
              "max_value_bytes": None if null_for == (c["table_name"], c["column_name"]) else 79_934}
             for c in lobs]  # fmt: skip
    text = json.dumps(raw) + "\n" + json.dumps({"lob_sizes": sizes}) + "\n"
    return text, [(c["table_name"], c["column_name"]) for c in lobs]


def _job(tmp_path, text: str) -> LocalArtifactStore:
    inp = CollectorInput.model_validate({
        "job_id": "job1", "engine": "mysql", "cluster_endpoint": "offline", "port": 3306,
        "database_name": "wordpress", "mode": "offline", "offline_config": {"s3_bucket": "b", "s3_key": "k"},
    })  # fmt: skip
    raw = read_collection_text(text, source="wordpress-collection.json")
    collector = mysql_collector._collect_offline(inp, _Checkpoints(), raw=raw).model_dump(
        mode="json"
    )
    tables = [t["table_id"] for t in collector["database_schema"]["tables"]]
    wave = {"wave": 1, "title": "Move to Aurora MySQL", "engines": ["aurora_mysql"], "moves_from": ["mysql"],
            "homogeneity": "homogeneous", "tables": tables, "gate": "Aurora MySQL serves every query."}  # fmt: skip
    store = LocalArtifactStore(str(tmp_path))
    store.write_json("wordpress/job1/synthesis/v2/report.json",
                     {"assignment_summary": {"version": 2}, "migration_waves": [wave]})  # fmt: skip
    store.write_json(
        "wordpress/job1/assignment/v2/assignment.json", {"version": 2, "query_assignments": []}
    )
    store.write_json("wordpress/job1/collector/output.json", collector)
    write_step1(store, "wordpress", "job1")
    write_plans(store, "wordpress", "job1")
    write_step1_delivery(store, "wordpress", "job1")
    return store


def _step_files(tmp_path) -> set[str]:
    return {
        p.name for p in (tmp_path / "wordpress" / "job1" / "data-movement" / "step-1").iterdir()
    }


def test_a_measured_mysql_collection_plans_every_lob_table_as_measured(tmp_path):
    text, lobs = _collection_text()
    assert lobs  # the sample has LONGTEXT columns
    store = _job(tmp_path, text)
    step = load_step1(store, "wordpress", "job1", with_ddl=False)
    sources = {t.size_source for t in step.plan.replication.lob_tables}
    assert step.plan.replication.lob_tables and sources == {"measured"}
    assert check_lob_sizes(step, None, None, {}).status == "pass"
    files = _step_files(tmp_path)
    assert "lob-sizes.sql" not in files and "dms-step-1.yaml" in files


def test_a_column_the_collection_could_not_measure_needs_lob_sizes_sql(tmp_path):
    text, lobs = _collection_text()
    table, column = lobs[0]
    text, _ = _collection_text(null_for=(table, column))
    store = _job(tmp_path, text)
    step = load_step1(store, "wordpress", "job1", with_ddl=False)
    unmeasured = [t for t in step.plan.replication.lob_tables if t.size_source == "unmeasured"]
    assert [t.table.rpartition(".")[2] for t in unmeasured] == [table]
    assert check_lob_sizes(step, None, None, {}).status == "fail"
    files = _step_files(tmp_path)
    assert "lob-sizes.sql" in files and "dms-step-1.yaml" not in files
    script = store.read_bytes("wordpress/job1/data-movement/step-1/lob-sizes.sql").decode()
    assert (
        f"MAX(LENGTH(`{column}`))" in script and script.count("START TRANSACTION READ ONLY;") == 1
    )
