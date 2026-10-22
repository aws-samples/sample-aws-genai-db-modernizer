"""The designed wordpress job's DynamoDB design is one a real run would produce (#1176).

``tests/e2e/designed_run.py`` builds the one deterministic job whose deliverables
show the designed figures. Its DynamoDB table map used to be copied from the
committed load-test design, written for an older routing: it mapped 7 tables no
DynamoDB query reads and missed 7 tables DynamoDB's wave moves. The map is now
built from the assignment, like the access patterns. A design whose map leaves
tables out is still a case the deliverables must handle, so it has its own
explicit job here (``dynamodb_leave_out``).
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from src.storage.assignment_versioning import resolve_downstream_assignment_version
from src.storage.local_store import LocalArtifactStore
from tests.e2e.deliverable_figures import report_invariant_violations
from tests.e2e.designed_run import (
    PARTIAL_EXTRA,
    PARTIAL_LEAVE_OUT,
    dynamodb_served_tables,
    write_dynamodb_design,
)
from tests.e2e.pipeline import PipelineResult

pytestmark = pytest.mark.deterministic


def _report(r: PipelineResult) -> dict[str, Any]:
    report: dict[str, Any] = json.loads(r.report_json().read_text())
    return report


def _served(r: PipelineResult) -> list[str]:
    store = LocalArtifactStore(base_dir=str(r.artifact_root))
    version = resolve_downstream_assignment_version(store, r.db, r.job_id)
    return dynamodb_served_tables(
        store.read_json(f"{r.db}/{r.job_id}/assignment/v{version}/assignment.json")
    )


def _mapped(report: dict[str, Any]) -> list[str]:
    return sorted(
        m["source_table"]
        for m in report["table_mappings"]
        if m["recommended_database"] == "dynamodb" and m.get("target_table")
    )


def _map_rows(md: str) -> list[str]:
    section = md.split("## Migration map", 1)[1].split("\n## ", 1)[0]
    return sorted(re.findall(r"^\| `([^`]+)` \| DynamoDB \|", section, flags=re.M))


def _moving(report: dict[str, Any]) -> set[str]:
    """The tables DynamoDB's migrating wave moves: the ones it serves, less the
    ones it keeps in sync from an owner outside the wave."""
    moving: set[str] = set()
    for w in report["migration_waves"]:
        if "dynamodb" in w["engines"]:
            synced = {
                o["table"] for o in w.get("table_owners") or [] if o["owner"] not in w["engines"]
            }
            ws = {t for g in w["table_groups"] for t in g["tables"]}
            moving |= ws - synced
    return moving


def test_the_designed_map_is_the_tables_dynamodb_reads(designed_run: PipelineResult) -> None:
    report = _report(designed_run)
    served = _served(designed_run)
    assert served, "no query routed to DynamoDB"
    # Every mapped table is one a DynamoDB query reads, and every one it reads is mapped.
    assert _mapped(report) == served
    md = designed_run.deliverable("engineering-report.md").read_text(encoding="utf-8")
    assert _map_rows(md) == served
    assert _moving(report) <= set(served)
    basis = report["facts"]["table_basis"]
    assert basis["mapped_tables"] == len(served) and basis["design_coverage"] == "complete"


def test_the_designed_runs_design_trace_is_the_contract(designed_run: PipelineResult) -> None:
    """The trace the designed job carries is a versioned ``DesignTrace`` (#1258), and the
    post-schema router reads its flags through the contract."""
    from src.agents.referee.post_schema_router import routing_flags_from_trace
    from src.agents.schema_design.design_trace import read_design_trace
    from src.contracts.schema_design_trace import DesignTrace
    from src.shared.contract_versioning import current_version

    store = LocalArtifactStore(base_dir=str(designed_run.artifact_root))
    version = resolve_downstream_assignment_version(store, designed_run.db, designed_run.job_id)
    key = f"{designed_run.db}/{designed_run.job_id}/schema-dynamodb/v{version}/design_trace.json"
    raw = json.loads((designed_run.artifact_root / key).read_text())
    assert raw["contract_version"] == current_version(DesignTrace)
    trace = read_design_trace(store, key)
    assert trace.trace == []
    assert routing_flags_from_trace(trace) == []


def test_a_design_whose_map_leaves_moving_tables_out(
    designed_partial_run: PipelineResult,
) -> None:
    """The explicit case the full fixture no longer covers: the design maps fewer
    tables than DynamoDB's wave moves. The map shows only the designed rows, its
    heading counts them, and report.json still meets every invariant."""
    partial = designed_partial_run
    report = _report(partial)
    served = _served(partial)
    assert set(PARTIAL_LEAVE_OUT) <= _moving(report), "the left-out tables are ones the wave moves"
    assert not set(PARTIAL_EXTRA) & set(served), "the extra table is one DynamoDB does not serve"
    kept = sorted({*(t for t in served if t not in PARTIAL_LEAVE_OUT), *PARTIAL_EXTRA})
    assert _mapped(report) == kept
    md = partial.deliverable("engineering-report.md").read_text(encoding="utf-8")
    assert f"## Migration map ({len(kept)} tables" in md
    assert _map_rows(md) == kept
    assert report["facts"]["table_basis"]["mapped_tables"] == len(kept)
    assert not report_invariant_violations(report)


def test_a_map_that_leaves_every_table_out_writes_no_pattern(
    designed_partial_run: PipelineResult, tmp_path: Path
) -> None:
    """No division by zero and no access pattern pointing at an unrelated table: a
    design that maps none of DynamoDB's tables serves none of its queries."""
    copy = tmp_path / "artifacts"
    shutil.copytree(designed_partial_run.job_dir(), copy / "wordpress" / "part-copy")
    store = LocalArtifactStore(base_dir=str(copy))
    version = resolve_downstream_assignment_version(store, "wordpress", "part-copy")
    served = dynamodb_served_tables(
        store.read_json(f"wordpress/part-copy/assignment/v{version}/assignment.json")
    )
    assert write_dynamodb_design(store, "wordpress", "part-copy", version, served) == 0
    design = store.read_json(f"wordpress/part-copy/schema-dynamodb/v{version}/schema_output.json")
    assert design["table_definitions"] == [] and design["access_patterns"] == []


def test_new_target_tables_key_on_the_source_tables_primary_key(
    designed_run: PipelineResult,
) -> None:
    store = LocalArtifactStore(base_dir=str(designed_run.artifact_root))
    version = resolve_downstream_assignment_version(store, "wordpress", designed_run.job_id)
    design = store.read_json(
        f"wordpress/{designed_run.job_id}/schema-dynamodb/v{version}/schema_output.json"
    )
    [table] = [t for t in design["table_definitions"] if t["table_name"] == "WcReservedStock"]
    assert table["partition_key"]["attribute_name"] == "order_id"
    assert table["sort_key"]["attribute_name"] == "product_id"


def test_the_migration_map_states_what_the_partial_map_misses(
    designed_run: PipelineResult, designed_partial_run: PipelineResult
) -> None:
    """#1173's two Migration map sentences: the partial job prints both (3 moving
    tables with no target table, one mapped table DynamoDB does not serve); the full
    job, whose map is exactly what DynamoDB serves, prints neither."""
    basis = _report(designed_partial_run)["facts"]["table_basis"]
    assert basis["migrating_tables_not_mapped"] == len(PARTIAL_LEAVE_OUT)
    assert basis["mapped_tables_not_served"] == {"dynamodb": list(PARTIAL_EXTRA)}
    md = designed_partial_run.deliverable("engineering-report.md").read_text(encoding="utf-8")
    assert f"no target table yet for {len(PARTIAL_LEAVE_OUT)} of the" in md
    assert "that no query routed to DynamoDB reads" in md
    full = _report(designed_run)["facts"]["table_basis"]
    assert full["migrating_tables_not_mapped"] == 0 and not full["mapped_tables_not_served"]
    md = designed_run.deliverable("engineering-report.md").read_text(encoding="utf-8")
    assert "no target table yet" not in md and "that no query routed to DynamoDB reads" not in md
