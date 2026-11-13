# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Each mapped table's owner, the engines that also read it and its cache (#212).

On the designed wordpress job (a schema design for every kept engine, no model), the
deliverables state one line per mapped source table: its owner, the engine that takes
its writes (ADR-031 Amendment 9; a table no query writes stays with the relational
step, #2166; never a cache), every other engine whose queries read it through CDC
replicas, and the cache in front of its reads. ``report.json``'s ``table_mappings``
rows, ``facts.table_owner_lines``, the engineering report's Migration map and the
executive report state the same lines and counts. The UI results page is checked in
``tests/e2e/test_ui.py``.

The cases are the ones #212 found: ``wp_postmeta``, mapped to one engine while another
reads it; a table no query writes (``wp_usermeta`` until #1893, then ``wp_users`` until
the refreshed sample of #1955 wrote it, now ``wp_wc_tax_rate_classes``); and a table the
cache fronts, which must keep its system-of-record owner and count where that owner's
tables count.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

import pytest

from src.report.facts import report_facts
from src.shared.cache_engines import CACHE_ENGINES
from src.shared.engine_names import display_engine
from src.shared.report_facts import compute_report_facts, wave_retained_engine
from tests.e2e.deliverable_figures import md_plain, pdf_page_texts
from tests.e2e.pipeline import PipelineResult

pytestmark = pytest.mark.deterministic

POSTMETA = "wordpress.wp_postmeta"
USERS = "wordpress.wp_users"
OPTIONS = "wordpress.wp_options"
TAX_RATE_CLASSES = "wordpress.wp_wc_tax_rate_classes"
# The bundled sample's designed job, and the same job on the collection bundled before
# #1955, whose table shapes the refreshed sample lacks (main's checks, on main's data).
JOBS = [
    "designed_run",
    pytest.param("designed_pre_1955", marks=pytest.mark.passes_on_main),
]


def _report(job: PipelineResult) -> dict[str, Any]:
    report: dict[str, Any] = json.loads(job.report_json().read_text())
    return report


def _mapping(report: dict[str, Any], table: str) -> dict[str, Any]:
    return next(m for m in report["table_mappings"] if m["source_table"] == table)


def _line(report: dict[str, Any], table: str) -> dict[str, Any]:
    return next(
        r for r in report["facts"]["table_owner_lines"]["table_lines"] if r["table"] == table
    )


def _assignment(job: PipelineResult, report: dict[str, Any]) -> dict[str, Any]:
    version = int(re.search(r"/synthesis/v(\d+)/", str(job.report_json())).group(1))
    path = job.job_dir() / f"assignment/v{version}/assignment.json"
    assignment: dict[str, Any] = json.loads(path.read_text())
    return assignment


def _map_rows(job: PipelineResult) -> dict[str, list[str]]:
    """The engineering report's Migration map: source table -> its row's cells."""
    md = job.deliverable("engineering-report.md").read_text()
    body = md.split("## Migration map", 1)[1].split("\n## ", 1)[0]
    rows: dict[str, list[str]] = {}
    for line in body.splitlines():
        if not line.startswith("| `"):
            continue
        cells = [md_plain(c.strip()) for c in re.split(r"(?<!\\)\|", line)[1:-1]]
        rows[cells[0].strip("`")] = cells
    return rows


def _engines_reading(assignment: dict[str, Any], table: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for qa in assignment["query_assignments"]:
        if qa.get("in_scope", True) and table in (qa.get("source_tables") or []):
            out[qa["assigned_engine"]] = out.get(qa["assigned_engine"], 0) + 1
    return out


def test_wp_postmeta_is_owned_by_its_writer_and_also_read_by_the_other_engine(
    designed_run: PipelineResult,
) -> None:
    """#212's first case. The table's writes sit on one engine; the other engine with
    in-scope queries on it reads a copy kept in sync from it. Asserted by name and
    against the assignment, so a routing change shows up as a changed name here."""
    report = _report(designed_run)
    assignment = _assignment(designed_run, report)
    writers = {
        qa["assigned_engine"]
        for qa in assignment["query_assignments"]
        if qa["query_id"] in assignment["write_groups"][POSTMETA]
    }
    assert writers == {"dynamodb"}
    assert set(_engines_reading(assignment, POSTMETA)) == {"dynamodb", "aurora_mysql"}
    row = _mapping(report, POSTMETA)
    assert (row["owner"], row["owner_basis"], row["also_read_by"]) == (
        "dynamodb",
        "writes",
        ["aurora_mysql"],
    )
    assert row["cached_by"] == "elasticache"
    cells = _map_rows(designed_run)[POSTMETA]
    assert cells[1:4] == ["DynamoDB", "Aurora MySQL", "ElastiCache"], cells


def test_wp_users_is_owned_by_its_writer_and_a_table_no_query_writes_by_the_relational_step(
    designed_run: PipelineResult,
) -> None:
    """#212's second case: a table no query writes stays with the relational step (#2166,
    ADR-033 Amendment 1), whichever engine reads it; the engines that read it read a copy.

    #1893: this was ``wp_usermeta``, then ``wp_users``. #1955: the refreshed sample's
    workload creates customers, so queries write ``wp_users`` (its writes are on
    DynamoDB, which owns it; Aurora MySQL reads a copy), and the one mapped table no
    query writes is ``wp_wc_tax_rate_classes``, read only on DynamoDB: Aurora MySQL keeps
    it and DynamoDB reads a copy. A table no query writes that two engines read is covered
    by tests/unit/shared/test_table_owner_facts.py
    (``test_a_table_no_query_writes_stays_with_the_relational_step``)."""
    report = _report(designed_run)
    assignment = _assignment(designed_run, report)
    writers = {
        qa["assigned_engine"]
        for qa in assignment["query_assignments"]
        if qa["query_id"] in assignment["write_groups"][USERS]
    }
    assert writers == {"dynamodb"}
    assert set(_engines_reading(assignment, USERS)) == {"dynamodb", "aurora_mysql"}
    row = _mapping(report, USERS)
    assert (row["owner"], row["owner_basis"], row["also_read_by"]) == (
        "dynamodb",
        "writes",
        ["aurora_mysql"],
    )
    assert row["owner"] not in CACHE_ENGINES
    cells = _map_rows(designed_run)[USERS]
    assert cells[1:4] == ["DynamoDB", "Aurora MySQL", "ElastiCache"], cells
    assert TAX_RATE_CLASSES not in (assignment.get("write_groups") or {})
    assert set(_engines_reading(assignment, TAX_RATE_CLASSES)) == {"dynamodb"}
    row = _mapping(report, TAX_RATE_CLASSES)
    assert (row["owner"], row["owner_basis"], row["also_read_by"]) == (
        "aurora_mysql",
        "retained",
        ["dynamodb"],
    )


def test_a_table_no_query_writes_and_two_engines_read_stays_with_the_relational_step(
    designed_pre_1955: PipelineResult,
) -> None:
    """#212's second case with two readers, on the collection bundled before #1955
    (``designed_pre_1955``): no query writes ``wp_users``, so the relational step keeps it
    (#2166), here not the engine the map recommends.

    #1893: this was ``wp_usermeta`` (3 reads on Aurora MySQL, 1 on DynamoDB). Over its own
    window ``3a74580e`` reads it at 1.24 calls/s (0.95 over 24 hours) and ties DynamoDB and
    Aurora MySQL at 73, a tie the reality check gives the source-compatible engine, so all
    four reads are on Aurora MySQL and the table is no longer mapped. ``wp_users`` is the
    same case, and the cache now fronts it too: ``ed65b0b6`` (``SELECT * FROM wp_users
    WHERE ID = ?``) reads it at 1.35 calls/s, over the 1 call/s hot-read floor."""
    job = designed_pre_1955
    report = _report(job)
    assignment = _assignment(job, report)
    assert USERS not in (assignment.get("write_groups") or {})
    reads = _engines_reading(assignment, USERS)
    assert reads == {"aurora_mysql": 3, "dynamodb": 2}
    row = _mapping(report, USERS)
    assert row["recommended_database"] == "dynamodb"
    assert (row["owner"], row["owner_basis"], row["also_read_by"]) == (
        "aurora_mysql",
        "retained",
        ["dynamodb"],
    )
    assert row["owner"] not in CACHE_ENGINES
    cells = _map_rows(job)[USERS]
    assert cells[1:4] == ["Aurora MySQL", "DynamoDB", "ElastiCache"], cells


@pytest.mark.parametrize("job_name", JOBS)
def test_a_cached_table_keeps_its_owner_and_counts_as_migrating(
    job_name: str, request: pytest.FixtureRequest
) -> None:
    """The "N tables migrate" against "Migration map (N tables)" mismatch (#212): a
    table the cache fronts has a system-of-record owner and is counted where that
    owner's tables are, never on a cache-layer line of its own.

    #1955: on the refreshed sample every mapped table but one migrates (its writes are on
    DynamoDB); #2166: the one no query writes, ``wp_wc_tax_rate_classes``, stays on Aurora
    MySQL and DynamoDB reads a copy, so the heading names the split ("N migrate, 1 kept
    current by CDC"). The collection bundled before #1955 keeps tables in sync from Aurora
    MySQL too."""
    job: PipelineResult = request.getfixturevalue(job_name)
    report = _report(job)
    row = _mapping(report, OPTIONS)
    assert row["cached_by"] == "elasticache" and row["owner"] == "dynamodb"
    cells = _map_rows(job)[OPTIONS]
    assert cells[1] == "DynamoDB" and cells[3] == "ElastiCache", cells
    dynamodb_wave = next(w for w in report["migration_waves"] if w["engines"] == ["dynamodb"])
    assert OPTIONS in dynamodb_wave["tables"]
    split = report["facts"]["table_basis"]["mapped_tables_by_move"]
    assert "cache_layer" not in split
    assert sum(split.values()) == len(report["table_mappings"])
    heading = next(
        line
        for line in job.deliverable("engineering-report.md").read_text().splitlines()
        if line.startswith("## Migration map")
    )
    assert "cache layer" not in heading
    if job_name == "designed_pre_1955":
        assert split.get("kept_in_sync"), split
    else:
        assert split == {"migrate": len(report["table_mappings"]) - 1, "kept_in_sync": 1}
    assert f"{split['migrate']} migrate" in heading
    assert split["migrate"] == report["facts"]["tables_migrate"]


@pytest.mark.parametrize("job_name", JOBS)
def test_every_line_follows_the_owner_rule(job_name: str, request: pytest.FixtureRequest) -> None:
    """A written table's owner is its source of truth and its replicas are among the
    engines that also read it; no line names a cache as its owner or a reader."""
    job: PipelineResult = request.getfixturevalue(job_name)
    report = _report(job)
    sources = {r["table"]: r for r in report["table_sources"]}
    lines = report["facts"]["table_owner_lines"]["table_lines"]
    # The wave plan's owner lines name the same owner (#212 review: one owner rule).
    wave_owner = {
        o["table"]: o["owner"]
        for w in report["migration_waves"]
        for o in w.get("table_owners") or []
    }
    # #1955: on the refreshed sample's designed job every mapped table's writes are on
    # DynamoDB but one; #2166: that one, wp_wc_tax_rate_classes, no query writes, so
    # Aurora MySQL keeps it and DynamoDB's wave names it as its only owner line. The job
    # on the collection bundled before #1955 keeps wp_term_relationships and wp_users in
    # sync from Aurora MySQL.
    if job_name == "designed_pre_1955":
        assert wave_owner, "the designed job keeps some table in sync from its owner"
    else:
        assert wave_owner == {TAX_RATE_CLASSES: "aurora_mysql"}, wave_owner
    for line in lines:
        if line["table"] in wave_owner:
            assert line["owner"] == wave_owner[line["table"]], line
    assert len(lines) == len(report["table_mappings"])
    for line in lines:
        assert line["owner"] and line["owner"] not in CACHE_ENGINES, line
        assert not set(line["also_read_by"]) & CACHE_ENGINES, line
        assert line["owner"] not in line["also_read_by"], line
        written = sources.get(line["table"])
        if written:
            assert line["owner"] == written["source_of_truth"], line
            assert line["owner_basis"] == "writes", line
            assert {r["engine"] for r in written["replicas"]} <= set(line["also_read_by"]), line
        else:
            # Nothing writes it: the relational step keeps it (#2166, ADR-033 Amendment 1).
            assert line["owner_basis"] == "retained", line
            assert line["owner"] == wave_retained_engine(report["migration_waves"]), line


def test_report_json_rows_and_facts_agree(designed_run: PipelineResult) -> None:
    report = _report(designed_run)
    for m in report["table_mappings"]:
        line = _line(report, m["source_table"])
        for key in ("owner", "owner_basis", "also_read_by", "cached_by"):
            assert line[key] == m[key], (m["source_table"], key)
    facts = report["facts"]["table_owner_lines"]
    assert facts == compute_report_facts(report)["table_owner_lines"]


def test_an_old_copy_of_the_report_gets_the_same_lines_from_the_facts_reader(
    designed_run: PipelineResult,
) -> None:
    """The same report with the #212 fields taken out, as a report written before them
    is: the facts reader's backfill gives every owner, reader and cache synthesis gave,
    from the report's own waves. A report written before #212 keeps its own basis
    (#2166 review): a table nothing writes that some engine queries is ``most_queries``
    there, and the rule it prints is that one."""
    report = _report(designed_run)
    old = copy.deepcopy(report)
    for m in old["table_mappings"]:
        for key in ("owner", "owner_basis", "also_read_by", "cached_by"):
            m.pop(key)
    old["facts"].pop("table_owner_lines")
    filled = report_facts(old)["table_owner_lines"]
    stored = report["facts"]["table_owner_lines"]
    assert stored["unwritten_owner_rule"] == "relational_step"
    keep = ("table", "owner", "also_read_by", "cached_by", "target_engine", "target_table")
    assert [{k: r.get(k) for k in keep} for r in filled["table_lines"]] == [
        {k: r.get(k) for k in keep} for r in stored["table_lines"]
    ]
    for key in ("table_count", "owned_table_counts", "no_owner_table_count", "cached_table_count"):
        assert filled[key] == stored[key], key
    for was, now in zip(stored["table_lines"], filled["table_lines"], strict=True):
        if was["owner_basis"] == "retained":
            # Queried: the old rule's basis; queried by no owner engine: retained still.
            assert now["owner_basis"] in ("most_queries", "retained"), now
        else:
            assert now["owner_basis"] == was["owner_basis"], now
    # So this stripped copy states the old rule although the report it came from follows
    # the new one. That pair cannot occur in practice -- a report written now carries both
    # its lines and its rule, and one written before #212 carries neither -- and it is the
    # price of reproducing the old labelling faithfully, which the pre-#212 guarantee
    # needs (#2166 review round 2, minor 3). It is not the rule for a report written now:
    # ``test_the_results_route_hands_the_ui_the_designed_jobs_own_rule`` states that.
    assert filled["unwritten_owner_rule"] == "most_queries"
    assert report_facts(old)["table_basis"] == report["facts"]["table_basis"]


def _summary(facts: dict[str, Any]) -> str:
    """The counts sentence, from report.json's fields only (not the renderer's helper)."""
    owned = [f"{display_engine(e)} owns {n}" for e, n in facts["owned_table_counts"].items()]
    owners = ", ".join(owned[:-1]) + f" and {owned[-1]}" if len(owned) > 1 else owned[0]
    caches = " and ".join(display_engine(e) for e in facts["cache_engines"])
    return (
        f"Of the {facts['table_count']} mapped tables, {owners}; "
        f"{facts['also_read_table_count']} tables are also read by another engine through CDC "
        f"replicas; {facts['cached_table_count']} tables are cached by {caches}."
    )


def test_the_engineering_and_executive_reports_state_the_same_counts(
    designed_run: PipelineResult,
) -> None:
    report = _report(designed_run)
    facts = report["facts"]["table_owner_lines"]
    owners: dict[str, int] = {}
    cached = also = 0
    for m in report["table_mappings"]:
        owners[m["owner"]] = owners.get(m["owner"], 0) + 1
        cached += bool(m["cached_by"])
        also += bool(m["also_read_by"])
    assert facts["owned_table_counts"] == owners
    assert (facts["cached_table_count"], facts["also_read_table_count"]) == (cached, also)
    sentence = _summary(facts)
    md = md_plain(designed_run.deliverable("engineering-report.md").read_text())
    assert sentence in md
    rows = _map_rows(designed_run)
    assert len(rows) == facts["table_count"]
    for engine, n in owners.items():
        assert sum(1 for cells in rows.values() if cells[1] == display_engine(engine)) == n
    assert sum(1 for cells in rows.values() if cells[3] != "—") == cached
    assert sum(1 for cells in rows.values() if cells[2] != "—") == also
    pdf = " ".join(pdf_page_texts(str(designed_run.deliverable("executive-report.pdf"))))
    assert f"Table owners: {sentence}" in pdf


def test_the_results_route_hands_the_ui_the_designed_jobs_own_rule(
    designed_run: PipelineResult,
) -> None:
    """#2166 review round 2: the UI reads `facts.table_owner_lines` from the results
    payload, so the route must carry this job's own `unwritten_owner_rule` (ADR-032).
    The saved designed job goes through the real route here; `TableOwnersCallout`'s
    `ruleKey` then picks the sentence from this value (its own tests cover the mapping).
    A job main saved carries no such field and gets the rule its lines follow, which is
    `most_queries` while any line names a reader as the owner of an unwritten table.
    """
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    from src.api.main import app
    from src.api.routes import results as route

    report = _report(designed_run)
    stored = report["facts"]["table_owner_lines"]
    assert stored["unwritten_owner_rule"] == "relational_step"
    main_shape = copy.deepcopy(report)
    main_shape["facts"]["table_owner_lines"].pop("unwritten_owner_rule")
    for row in main_shape["table_mappings"]:
        if row.get("owner_basis") == "retained":
            row.update(owner="dynamodb", owner_basis="most_queries")
    for line in main_shape["facts"]["table_owner_lines"]["table_lines"]:
        if line.get("owner_basis") == "retained":
            line.update(owner="dynamodb", owner_basis="most_queries")

    sfn, s3 = MagicMock(), MagicMock()
    sfn.describe_execution.return_value = {
        "status": "SUCCEEDED",
        "input": {"database_name": designed_run.db, "source_database_type": "mysql"},
    }
    s3.read_triage.return_value = None
    route.sfn_service, route.s3_service = sfn, s3
    try:
        client = TestClient(app)
        for payload, expected in ((report, "relational_step"), (main_shape, "most_queries")):
            s3.read_synthesis.return_value = copy.deepcopy(payload)
            body = client.get(f"/api/v1/assessments/{designed_run.job_id}/results").json()
            lines = body["synthesis"]["facts"]["table_owner_lines"]
            assert lines["unwritten_owner_rule"] == expected
            assert lines["table_count"] == stored["table_count"]
    finally:
        route.sfn_service, route.s3_service = None, None
