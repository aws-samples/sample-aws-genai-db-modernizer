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
from typing import Any

import pytest

from src.storage.assignment_versioning import resolve_downstream_assignment_version
from src.storage.local_store import LocalArtifactStore
from tests.e2e.deliverable_figures import report_invariant_violations
from tests.e2e.designed_run import dynamodb_served_tables, run_designed_wordpress
from tests.e2e.pipeline import PipelineResult

pytestmark = pytest.mark.deterministic

# Three of the tables DynamoDB's wave moves, left out of the partial design's map.
LEFT_OUT = [
    "wordpress.wp_wc_product_meta_lookup",
    "wordpress.wp_wc_reserved_stock",
    "wordpress.wp_wc_tax_rate_classes",
]


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


@pytest.fixture(scope="module")
def partial(tmp_path_factory: pytest.TempPathFactory) -> PipelineResult:
    return run_designed_wordpress(
        tmp_path_factory.mktemp("designed-partial"), "part-desi", dynamodb_leave_out=LEFT_OUT
    )


def test_a_design_whose_map_leaves_moving_tables_out(partial: PipelineResult) -> None:
    """The explicit case the full fixture no longer covers: the design maps fewer
    tables than DynamoDB's wave moves. The map shows only the designed rows, its
    heading counts them, and report.json still meets every invariant."""
    report = _report(partial)
    served = _served(partial)
    assert set(LEFT_OUT) <= _moving(report), "the left-out tables must be ones the wave moves"
    kept = [t for t in served if t not in LEFT_OUT]
    assert _mapped(report) == kept
    md = partial.deliverable("engineering-report.md").read_text(encoding="utf-8")
    assert f"## Migration map ({len(kept)} tables" in md
    assert _map_rows(md) == kept
    assert report["facts"]["table_basis"]["mapped_tables"] == len(kept)
    assert not report_invariant_violations(report)
