# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Triggers outside the DML list never change routing (#2023, ADR-034 Decision 8).

The discourse sample, with a TRUNCATE-only trigger and two event triggers added, runs
through the deterministic pipeline; its per-query routing snapshot must equal the
committed discourse baseline, query for query, while its collection keeps the three
triggers in ``non_dml_triggers``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.e2e.pipeline import run_pipeline_from_file, unzip_sample
from tests.e2e.routing_snapshot import BASELINE, compare, format_diff, measure

pytestmark = pytest.mark.e2e


def _with_non_dml_triggers(path: Path) -> None:
    raw = json.loads(path.read_text())
    table = raw["triggers"][0]["table_name"]
    raw["triggers"].append({
        "trigger_name": "audit_truncate", "schema_name": "public", "table_name": table,
        "timing": "BEFORE", "event_type": "TRUNCATE", "for_each": "STATEMENT",
        "is_enabled": "O", "function_signature": "public.audit_truncate()",
        "body_status": "collected",
        "definition": f"CREATE TRIGGER audit_truncate BEFORE TRUNCATE ON public.{table} "
                      "FOR EACH STATEMENT EXECUTE FUNCTION audit_truncate()",
    })  # fmt: skip
    raw["event_triggers"] = [
        {"trigger_name": name, "quoted_name": name, "event": event, "tags": tags,
         "is_enabled": "O", "owner": "discourse", "function_signature": "public.guard_ddl()",
         "quoted_function": "public.guard_ddl", "body_status": "collected"}
        for name, event, tags in (("guard_ddl", "ddl_command_start", ["DROP TABLE"]),
                                  ("log_drop", "sql_drop", []))
    ]  # fmt: skip
    path.write_text(json.dumps(raw))


@pytest.mark.deterministic
def test_the_discourse_routing_is_unchanged_with_them(tmp_path: Path) -> None:
    src = unzip_sample("discourse", tmp_path / "input")
    _with_non_dml_triggers(src)
    result = run_pipeline_from_file(src, "discourse", tmp_path / "artifacts", "nondml0001")

    collector = json.loads((result.job_dir() / "collector" / "output.json").read_text())
    kept = {t["trigger_name"] for t in collector["database_schema"]["non_dml_triggers"]}
    assert kept == {"audit_truncate", "guard_ddl", "log_drop"}
    baseline = json.loads(BASELINE.read_text())["samples"]["discourse"]
    diff = compare(baseline, measure(result.job_dir()))
    assert not diff, format_diff("discourse", diff)
