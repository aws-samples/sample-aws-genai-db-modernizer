"""#992: a table has one writer on every sample; its replicas are stated everywhere.

ADR-031 Amendment 9 (maintainer decision, 2026-10-17): all of a table's write
paths land on one engine, its source of truth, in every assignment version the
pipeline writes (the first assignment and the reality check here; the customer
gate and the post-schema reroute are unit-tested). Reads stay per query: an
engine serving a table's reads off its source of truth is an eventually
consistent replica, and report.json and every deliverable state it.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.agents.referee.write_groups import collector_write_groups, table_sources
from src.report.escaping import md_text
from src.shared.engine_names import display_engine
from src.shared.report_facts import (
    replica_risk_text,
    shared_replica_risk_text,
    table_source_facts,
)
from tests.e2e.deliverable_figures import pdf_page_texts
from tests.e2e.pipeline import PipelineResult


def _versions(run: PipelineResult) -> list[dict]:
    root = run.job_dir() / "assignment"
    paths = sorted(root.glob("v*/assignment.json"), key=lambda p: int(p.parent.name[1:]))
    return [json.loads(p.read_text(encoding="utf-8")) for p in paths]


def _collector(run: PipelineResult) -> dict:
    out: dict = json.loads(
        (run.job_dir() / "collector" / "output.json").read_text(encoding="utf-8")
    )
    return out


def _report(run: PipelineResult) -> dict:
    out: dict = json.loads(Path(run.report_json()).read_text(encoding="utf-8"))
    return out


def test_no_table_has_writes_on_two_engines(run: PipelineResult) -> None:
    """Every written table's writes and locking reads sit on one engine, in every
    version: the invariant #992's split-write tables (wordpress 2, discourse 33,
    trailgear 3 before) broke."""
    groups = collector_write_groups(_collector(run))
    assert groups, run.db
    for assignment in _versions(run):
        assert assignment["write_groups"] == groups, (run.db, assignment["version"])
        engine = {qa["query_id"]: qa["assigned_engine"] for qa in assignment["query_assignments"]}
        split = {
            table: sorted({engine[q] for q in members})
            for table, members in groups.items()
            if len({engine[q] for q in members}) > 1
        }
        assert split == {}, (run.db, assignment["version"], split)
        for table in assignment["table_assignments"]:
            if table["table_id"] in groups:
                assert len(table["write_engines"]) == 1, (run.db, table)
                assert table["source_of_truth"] == table["write_engines"][0]


def test_trailgear_shipments_insert_and_update_share_one_engine(
    trailgear_run: PipelineResult,
) -> None:
    """The #992 repro: Shipment's INSERT sat on DynamoDB and its UPDATE on Aurora
    PostgreSQL."""
    run = trailgear_run
    queries = _collector(run)["queries"]["query_patterns"]
    shipment = {
        q["query_id"]: q["query_type"]
        for q in queries
        if q["query_type"] in {"INSERT", "UPDATE"}
        and "trailgear.Shipment" in (q.get("tables_accessed") or [])
    }
    assert sorted(shipment.values()) == ["INSERT", "UPDATE"]
    for assignment in _versions(run):
        engines = {
            qa["assigned_engine"]
            for qa in assignment["query_assignments"]
            if qa["query_id"] in shipment
        }
        assert len(engines) == 1, (assignment["version"], engines)


def test_report_json_states_each_tables_source_of_truth_and_replicas(run: PipelineResult) -> None:
    report = _report(run)
    final = _versions(run)[-1]
    assert report["table_sources"] == table_sources(final)
    assert report["facts"]["table_sources"] == table_source_facts(report)
    facts = report["facts"]["table_sources"]
    assert facts["split_write_table_count"] == 0
    assert facts["written_table_count"] == len(collector_write_groups(_collector(run)))
    engine = {qa["query_id"]: qa["assigned_engine"] for qa in final["query_assignments"]}
    for row in facts["replicated_tables"]:
        assert row["replica_engines"], row
        assert row["source_of_truth"] not in row["replica_engines"]
        for qid in row["replica_read_query_ids"]:
            assert engine[qid] in row["replica_engines"], (row["table"], qid)
        assert row["risk"].startswith("Eventually consistent: ")
        assert f"in sync from {display_engine(row['source_of_truth'])} via CDC" in row["risk"]


def test_every_deliverable_states_the_replicas(run: PipelineResult) -> None:
    """The engineering report lists the replicated tables with their CDC direction
    and risk, from the facts; the UI results page is checked in test_ui.py. The
    executive report names no table (#1020), so it leaves them out."""
    facts = _report(run)["facts"]["table_sources"]
    rows = facts["replicated_tables"]
    assert rows, run.db
    heading = f"Sources of truth and replicas ({facts['written_table_count']} written tables)"
    md = run.deliverable("engineering-report.md").read_text(encoding="utf-8")
    assert f"## {heading}" in md
    for row in rows:
        replicas = ", ".join(display_engine(e) for e in row["replica_engines"])
        cdc = f"{display_engine(row['source_of_truth'])} → {replicas}"
        assert f"| `{row['table']}` | {display_engine(row['source_of_truth'])} | {cdc} |" in md
        # #1170: each row's risk is the standard sentence, so the report states it
        # once for every row, naming the sources of truth.
        assert row["risk"] == replica_risk_text(
            row["source_of_truth"], row["replica_engines"], row["replica_read_count"]
        )
    sources = list(dict.fromkeys(row["source_of_truth"] for row in rows))
    assert md.count(md_text(shared_replica_risk_text(sources))) == 1
    pdf = " ".join(pdf_page_texts(str(run.deliverable("executive-report.pdf"))))
    assert "Sources of truth and replicas" not in pdf
    assert "Eventually consistent: " not in pdf
