"""Each mapped table's owner, the engines that also read it and its cache (#212).

On the designed wordpress job (a schema design for every kept engine, no model), the
deliverables state one line per mapped source table: its owner, the engine that takes
its writes (ADR-031 Amendment 9; a table no query writes, the engine serving most of
its queries; never a cache), every other engine whose queries read it through CDC
replicas, and the cache in front of its reads. ``report.json``'s ``table_mappings``
rows, ``facts.table_owner_lines``, the engineering report's Migration map and the
executive report state the same lines and counts. The UI results page is checked in
``tests/e2e/test_ui.py``.

The cases are the ones #212 found: ``wp_postmeta``, mapped to one engine while another
reads it; ``wp_usermeta``, which no query writes; and a table the cache fronts, which
must keep its system-of-record owner and count where that owner's tables count.
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
from src.shared.report_facts import compute_report_facts
from tests.e2e.deliverable_figures import md_plain, pdf_page_texts
from tests.e2e.pipeline import PipelineResult

pytestmark = pytest.mark.deterministic

POSTMETA = "wordpress.wp_postmeta"
USERMETA = "wordpress.wp_usermeta"
OPTIONS = "wordpress.wp_options"


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


def test_wp_usermeta_has_a_system_of_record_owner_though_no_query_writes_it(
    designed_run: PipelineResult,
) -> None:
    """#212's second case: no query writes ``wp_usermeta``, so its owner is the engine
    serving most of its queries, here not the engine the map recommends."""
    report = _report(designed_run)
    assignment = _assignment(designed_run, report)
    assert USERMETA not in (assignment.get("write_groups") or {})
    reads = _engines_reading(assignment, USERMETA)
    assert reads == {"aurora_mysql": 3, "dynamodb": 1}
    row = _mapping(report, USERMETA)
    assert row["recommended_database"] == "dynamodb"
    assert (row["owner"], row["owner_basis"], row["also_read_by"]) == (
        "aurora_mysql",
        "most_queries",
        ["dynamodb"],
    )
    assert row["owner"] not in CACHE_ENGINES
    cells = _map_rows(designed_run)[USERMETA]
    assert cells[1:4] == ["Aurora MySQL", "DynamoDB", "—"], cells


def test_a_cached_table_keeps_its_owner_and_counts_as_migrating(
    designed_run: PipelineResult,
) -> None:
    """The "N tables migrate" against "Migration map (N tables)" mismatch (#212): a
    table the cache fronts has a system-of-record owner and is counted where that
    owner's tables are, never on a cache-layer line of its own."""
    report = _report(designed_run)
    row = _mapping(report, OPTIONS)
    assert row["cached_by"] == "elasticache" and row["owner"] == "dynamodb"
    cells = _map_rows(designed_run)[OPTIONS]
    assert cells[1] == "DynamoDB" and cells[3] == "ElastiCache", cells
    dynamodb_wave = next(w for w in report["migration_waves"] if w["engines"] == ["dynamodb"])
    assert OPTIONS in dynamodb_wave["tables"]
    split = report["facts"]["table_basis"]["mapped_tables_by_move"]
    assert "cache_layer" not in split
    assert sum(split.values()) == len(report["table_mappings"])
    heading = next(
        line
        for line in designed_run.deliverable("engineering-report.md").read_text().splitlines()
        if line.startswith("## Migration map")
    )
    assert "cache layer" not in heading
    assert f"{split['migrate']} migrate" in heading
    assert split["migrate"] == report["facts"]["tables_migrate"]


def test_every_line_follows_the_owner_rule(designed_run: PipelineResult) -> None:
    """A written table's owner is its source of truth and its replicas are among the
    engines that also read it; no line names a cache as its owner or a reader."""
    report = _report(designed_run)
    sources = {r["table"]: r for r in report["table_sources"]}
    lines = report["facts"]["table_owner_lines"]["table_lines"]
    # The wave plan's owner lines name the same owner (#212 review: one owner rule).
    wave_owner = {
        o["table"]: o["owner"]
        for w in report["migration_waves"]
        for o in w.get("table_owners") or []
    }
    assert wave_owner, "the designed job keeps some table in sync from its owner"
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
            assert line["owner_basis"] == "most_queries", line


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
    is: the facts reader's backfill gives every line synthesis gave."""
    report = _report(designed_run)
    old = copy.deepcopy(report)
    for m in old["table_mappings"]:
        for key in ("owner", "owner_basis", "also_read_by", "cached_by"):
            m.pop(key)
    old["facts"].pop("table_owner_lines")
    assert report_facts(old)["table_owner_lines"] == report["facts"]["table_owner_lines"]
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
