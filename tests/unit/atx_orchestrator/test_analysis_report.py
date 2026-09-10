"""Assembler + renderer for the interactive analysis report.

The point of these tests is that ``build_export_data`` reproduces the shape
``AnalysisResults-02.js`` builds from four REST calls, without going through the
API — which cannot serve an ATX job at all (every route resolves the database name
through Step Functions, and an A2A-orchestrated job has no execution).
"""

from __future__ import annotations

import json
import time

import pytest

from src.atx_orchestrator.runtime import analysis_report as ar
from src.atx_orchestrator.runtime.artifacts import artifact_stem, provenance

DB = "discourse"
JOB = "29d77e81-6675-4942-9c34-4c5070d77860"


class FakeStore:
    """Minimal ArtifactStore: read_json / exists / list_prefix."""

    def __init__(self, objects: dict[str, dict]):
        self.objects = objects
        self.reads: list[str] = []

    def read_json(self, path: str) -> dict:
        self.reads.append(path)
        return json.loads(json.dumps(self.objects[path]))

    def exists(self, path: str) -> bool:
        return path in self.objects

    def list_prefix(self, prefix: str) -> list[str]:
        return [k for k in self.objects if k.startswith(prefix)]


def _report(**overrides) -> dict:
    base = {
        "contract_version": "1.0.0",
        "job_id": JOB,
        "database_name": DB,
        "agent_type": "referee-synthesis",
        "summary": "Discourse splits cleanly across four engines.",
        "summary_deterministic": "Deterministic summary.",
        "ranking": [
            {
                "target": "elasticache",
                "workload_percent": 29.4,
                "assigned_queries": 487,
                "schema_design_available": True,
            },
            {
                "target": "dynamodb",
                "workload_percent": 24.1,
                "assigned_queries": 399,
                "schema_design_available": True,
            },
        ],
        "tco_analysis": {
            "cost_breakdown": [
                {"database": "documentdb", "monthly_cost_usd": 271.8},
                {"database": "elasticache", "monthly_cost_usd": 165.1},
            ]
        },
        "risk_assessment": {"overall_risk_level": "HIGH", "risks": []},
        "recommended_architecture": {"databases": []},
        "table_mappings": [],
        "query_groups": [],
        "schema_designs": {},
        "trade_offs": [],
        "assignment_summary": {"version": 1},
    }
    base.update(overrides)
    return base


def _journey(qid: str, qtype: str, engine: str | None, cps: float) -> dict:
    return {
        "query_id": qid,
        "source": {
            "query_text": f"SELECT * FROM t WHERE id = {qid}",
            "query_type": qtype,
            "tables_accessed": ["discourse.posts"],
            "frequency_per_hour": 100,
            "calls_per_second": cps,
            "performance": {"execution_time_ms_avg": 3.2},
            "characteristics": {"has_joins": False},
        },
        "assignment": (
            {
                "assigned_engine": engine,
                "confidence": 88,
                "in_scope": True,
                "assignment_reason": "dropped by projection",
                "warnings": [],
            }
            if engine
            else None
        ),
        "design": {"engine": engine, "schema_version": 1, "status": "designed"},
        "load_test": {"p95_ms": 4.0},
        "sdk_code": None,
    }


def _objects(**extra) -> dict[str, dict]:
    objects: dict[str, dict] = {
        f"{DB}/{JOB}/synthesis/v1/report.json": _report(),
        f"{DB}/{JOB}/referee-triage/triage.json": {"selected_engines": ["dynamodb"]},
        f"{DB}/{JOB}/collector/output.json": {
            "database_name": DB,
            "queries": {
                "query_patterns": [
                    {
                        "query_id": "q1",
                        "query_text": "SELECT 1",
                        "query_type": "SELECT",
                        "tables_accessed": ["discourse.posts"],
                        "calls_per_second": 12.0,
                        "frequency_per_hour": 43200,
                        "execution_time_ms_avg": 2.0,
                    }
                ]
            },
            "schema": {"tables": [{"table_name": "posts"}]},
        },
        f"{DB}/{JOB}/schema-dynamodb/v1/schema_output.json": {
            "access_patterns": [{"pattern_id": "DDB-AP-1", "query_ids": ["q1"]}],
            "table_definitions": [{"table_name": "Users"}],
            "trade_offs": [{"description": "denormalised", "query_ids": ["q1"]}],
        },
        f"{DB}/{JOB}/query-journeys/q1.json": _journey("q1", "SELECT", "dynamodb", 12.0),
        f"{DB}/{JOB}/query-journeys/q2.json": _journey("q2", "UPDATE", "elasticache", 3.0),
        f"{DB}/{JOB}/query-journeys/q3.json": _journey("q3", "SELECT", None, 1.0),
    }
    objects.update(extra)
    return objects


# ---------------------------------------------------------------------------
# Synthesis key resolution — Defect 1
# ---------------------------------------------------------------------------


def test_finds_the_versioned_synthesis_report():
    """The orchestrated flow always writes synthesis/v1/, never referee-synthesis/."""
    store = FakeStore(_objects())
    data = ar.build_export_data(store, JOB, DB)
    assert data["sourceArtifact"] == f"{DB}/{JOB}/synthesis/v1/report.json"


def test_falls_back_to_the_unversioned_report():
    objects = _objects()
    del objects[f"{DB}/{JOB}/synthesis/v1/report.json"]
    objects[f"{DB}/{JOB}/referee-synthesis/report.json"] = _report()
    data = ar.build_export_data(FakeStore(objects), JOB, DB, assignment_version=0)
    assert data["sourceArtifact"] == f"{DB}/{JOB}/referee-synthesis/report.json"


def test_prefers_the_highest_version_when_the_requested_one_is_absent():
    objects = _objects()
    del objects[f"{DB}/{JOB}/synthesis/v1/report.json"]
    objects[f"{DB}/{JOB}/synthesis/v2/report.json"] = _report()
    objects[f"{DB}/{JOB}/synthesis/v3/report.json"] = _report()
    data = ar.build_export_data(FakeStore(objects), JOB, DB, assignment_version=1)
    assert data["sourceArtifact"] == f"{DB}/{JOB}/synthesis/v3/report.json"


def test_missing_report_names_what_it_looked_for():
    with pytest.raises(FileNotFoundError, match="No synthesis report found"):
        ar.build_export_data(FakeStore({}), JOB, DB)


# ---------------------------------------------------------------------------
# DATA shape parity with the React exporter
# ---------------------------------------------------------------------------


def test_export_data_has_the_keys_the_client_code_destructures():
    data = ar.build_export_data(FakeStore(_objects()), JOB, DB)
    assert set(data) >= {
        "results",
        "schemaDesigns",
        "collector",
        "jobId",
        "exportDate",
        "queryJourneys",
    }
    assert set(data["results"]) == {"job_id", "status", "synthesis", "triage_summary"}
    assert data["results"]["status"] == "COMPLETED"
    assert data["jobId"] == JOB


def test_schema_designs_use_the_api_response_shape():
    """report.schema_designs is a dict of summaries and cannot substitute for this."""
    data = ar.build_export_data(FakeStore(_objects()), JOB, DB)
    designs = data["schemaDesigns"]
    assert len(designs) == 1
    assert set(designs[0]) == {"target_type", "artifact_path", "content"}
    assert designs[0]["target_type"] == "dynamodb"
    # The fields the Access Pattern Explorer and Trade-offs tabs are built from.
    assert designs[0]["content"]["access_patterns"]
    assert designs[0]["content"]["trade_offs"]


def test_schema_designs_take_the_latest_version_per_engine():
    objects = _objects()
    objects[f"{DB}/{JOB}/schema-dynamodb/v2/schema_output.json"] = {"access_patterns": [1, 2]}
    data = ar.build_export_data(FakeStore(objects), JOB, DB)
    ddb = next(d for d in data["schemaDesigns"] if d["target_type"] == "dynamodb")
    assert ddb["artifact_path"].endswith("/v2/schema_output.json")


def test_no_schema_designs_degrades_instead_of_raising(caplog):
    objects = {k: v for k, v in _objects().items() if "/schema-" not in k}
    data = ar.build_export_data(FakeStore(objects), JOB, DB)
    assert data["schemaDesigns"] == []
    assert "Access Pattern Explorer" in caplog.text


# ---------------------------------------------------------------------------
# reality_check — Defect 3
# ---------------------------------------------------------------------------


def test_reality_check_is_derived_when_absent():
    """The contract has no reality_check key, so after_distribution would be empty."""
    data = ar.build_export_data(FakeStore(_objects()), JOB, DB)
    rc = data["results"]["synthesis"]["reality_check"]
    assert rc["after_distribution"] == {"elasticache": 29.4, "dynamodb": 24.1}
    assert rc["assigned_query_counts"] == {"elasticache": 487, "dynamodb": 399}


def test_existing_reality_check_is_left_alone():
    objects = _objects()
    objects[f"{DB}/{JOB}/synthesis/v1/report.json"] = _report(
        reality_check={"after_distribution": {"opensearch": 100.0}}
    )
    data = ar.build_export_data(FakeStore(objects), JOB, DB)
    rc = data["results"]["synthesis"]["reality_check"]
    assert rc == {"after_distribution": {"opensearch": 100.0}}


# ---------------------------------------------------------------------------
# Journeys, projection, budget — Defects 4 and 5
# ---------------------------------------------------------------------------


def test_all_journeys_are_read_with_no_page_clamp():
    data = ar.build_export_data(FakeStore(_objects()), JOB, DB)
    qj = data["queryJourneys"]
    assert qj["total"] == 3
    assert len(qj["items"]) == 3
    assert "truncated" not in qj


def test_journeys_are_projected_to_the_fields_the_report_reads():
    data = ar.build_export_data(FakeStore(_objects()), JOB, DB)
    item = next(i for i in data["queryJourneys"]["items"] if i["query_id"] == "q1")
    assert set(item) == {"query_id", "source", "assignment", "design"}
    assert set(item["assignment"]) == {"assigned_engine", "confidence", "in_scope"}
    assert "assignment_reason" not in item["assignment"]
    assert set(item["design"]) == {"engine", "status"}


def test_budget_keeps_the_busiest_queries_and_says_so():
    data = ar.build_export_data(FakeStore(_objects()), JOB, DB, journey_budget=2)
    qj = data["queryJourneys"]
    assert qj["total"] == 3
    assert [i["query_id"] for i in qj["items"]] == ["q1", "q2"]
    assert qj["truncated"] == {"kept": 2, "total": 3, "criterion": "calls_per_second desc"}


def test_flow_aggregate_covers_every_journey_even_when_truncated():
    """The Sankey must never be silently sampled — that is Defect 4's failure mode."""
    data = ar.build_export_data(FakeStore(_objects()), JOB, DB, journey_budget=1)
    assert sum(row["count"] for row in data["flowAggregate"]) == 3
    assert {"query_type": "SELECT", "assigned_engine": "unassigned", "count": 1} in data[
        "flowAggregate"
    ]


def test_journeys_are_read_concurrently():
    """1,654 serial S3 GETs is ~2 min of latency inside the synthesis step."""
    objects = _objects()
    for i in range(200):
        objects[f"{DB}/{JOB}/query-journeys/gen{i}.json"] = _journey(
            f"gen{i}", "SELECT", "dynamodb", float(i)
        )

    store = FakeStore(objects)
    max_concurrent = 0
    live = 0
    import threading

    lock = threading.Lock()
    inner = store.read_json

    def slow(path: str) -> dict:
        nonlocal live, max_concurrent
        with lock:
            live += 1
            max_concurrent = max(max_concurrent, live)
        try:
            time.sleep(0.005)
            return inner(path)
        finally:
            with lock:
                live -= 1

    store.read_json = slow  # type: ignore[method-assign]
    data = ar.build_export_data(store, JOB, DB)
    assert data["queryJourneys"]["total"] == 203
    assert max_concurrent > 1, "journeys were read serially"


def test_one_unreadable_journey_does_not_lose_the_rest():
    objects = _objects()
    store = FakeStore(objects)
    inner = store.read_json

    def flaky(path: str) -> dict:
        if path.endswith("q2.json"):
            raise OSError("transient S3 failure")
        return inner(path)

    store.read_json = flaky  # type: ignore[method-assign]
    data = ar.build_export_data(store, JOB, DB)
    assert [i["query_id"] for i in data["queryJourneys"]["items"]] == ["q1", "q3"]


def test_missing_journeys_degrade_to_empty():
    objects = {k: v for k, v in _objects().items() if "/query-journeys/" not in k}
    data = ar.build_export_data(FakeStore(objects), JOB, DB)
    assert data["queryJourneys"]["items"] == []
    assert data["flowAggregate"] == []


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


@pytest.fixture
def rendered() -> str:
    data = ar.build_export_data(FakeStore(_objects()), JOB, DB)
    return ar.render_analysis_report_html(data, filename="discourse_analysis-report_29d77e81.html")


def test_render_leaves_no_unfilled_placeholders(rendered):
    import re

    assert not re.findall(r"__[A-Z_]+__", rendered)


def test_unfilled_placeholder_in_the_template_raises(monkeypatch):
    """A placeholder added by the sync script but not by the renderer must fail loudly."""
    real = ar._read_template

    def fake(name: str) -> str:
        text = real(name)
        return text + "\n<!-- __BRAND_NEW__ -->" if name.endswith(".tpl") else text

    monkeypatch.setattr(ar, "_read_template", fake)
    data = ar.build_export_data(FakeStore(_objects()), JOB, DB)
    with pytest.raises(ValueError, match="__BRAND_NEW__"):
        ar.render_analysis_report_html(data)


def test_upper_snake_tokens_in_customer_data_are_not_mistaken_for_placeholders():
    """Real captured SQL contains things like __NULL__; that must not abort the render."""
    objects = _objects()
    objects[f"{DB}/{JOB}/query-journeys/q1.json"]["source"][
        "query_text"
    ] = "SELECT COALESCE(x, '__NULL__') FROM posts WHERE k = '__SENTINEL_VALUE__'"
    html = ar.render_analysis_report_html(ar.build_export_data(FakeStore(objects), JOB, DB))
    assert "__NULL__" in html and "__SENTINEL_VALUE__" in html


def test_render_keeps_every_report_section(rendered):
    for header in (
        "Database Modernization Analysis Report",
        "Executive Summary",
        "Cost Breakdown",
        "Query Flow",
        "Access Pattern Explorer",
        "Trade-offs and Design Decisions",
        "Principal Engineer Notes",
    ):
        assert header in rendered


def test_render_embeds_the_data_object_and_the_client_code(rendered):
    assert "const DATA = " in rendered
    assert "const ENGINE_LABELS = " in rendered
    assert "function extractPatterns" in rendered
    assert "DOMContentLoaded" in rendered


def test_embedded_data_carries_only_what_the_client_code_reads(rendered):
    """The WebApp exporter ships `collector` unused -- 1.06 MB of dead JSON."""
    used = ar._data_keys_used_by_template()
    assert {"results", "schemaDesigns", "queryJourneys"} <= used
    assert "collector" not in used

    payload = json.loads(_embedded_data(rendered))
    assert set(payload) == used | set(ar._ALWAYS_EMBED)
    assert "collector" not in payload


def test_a_template_that_starts_reading_a_new_key_fails_loudly(monkeypatch):
    monkeypatch.setattr(ar, "_data_keys_used_by_template", lambda: {"results", "somethingNew"})
    data = ar.build_export_data(FakeStore(_objects()), JOB, DB)
    with pytest.raises(ValueError, match="somethingNew"):
        ar.render_analysis_report_html(data)


def _embedded_data(html_text: str) -> str:
    """Slice the `const DATA = {...};` literal out of a rendered report."""
    start = html_text.index("const DATA = ") + len("const DATA = ")
    depth, i = 0, start
    while True:
        c = html_text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return html_text[start : i + 1]
        elif c == '"':
            i += 1
            while html_text[i] != '"':
                i += 2 if html_text[i] == "\\" else 1
        i += 1


def test_render_fills_the_header_and_stat_cards(rendered):
    assert JOB in rendered
    assert "Discourse splits cleanly across four engines." in rendered
    assert "436.90" in rendered  # 271.8 + 165.1
    # Engine badges carry the engine as data, not a colour class: the palette lives
    # only in the CSS, so this side never names a colour.
    assert 'class="badge" data-engine="elasticache">Elasticache</span>' in rendered
    assert 'class="badge" data-engine="dynamodb">DynamoDB</span>' in rendered


def test_render_is_self_identifying(rendered):
    assert "discourse_analysis-report_29d77e81.html" in rendered
    assert '<meta name="x-dbmod-artifact" content="analysis-report">' in rendered
    assert f'<meta name="x-dbmod-job-id" content="{JOB}">' in rendered
    # The template already carries the "Analysis Report - " prefix; don't repeat it.
    assert f"<title>Analysis Report - {DB} — 29d77e81</title>" in rendered


def test_script_close_tag_in_data_cannot_break_the_report():
    """A captured SQL string containing </script> must not terminate the element."""
    objects = _objects()
    objects[f"{DB}/{JOB}/query-journeys/q1.json"] = _journey("q1", "SELECT", "dynamodb", 9.0)
    objects[f"{DB}/{JOB}/query-journeys/q1.json"]["source"][
        "query_text"
    ] = "SELECT '</script><script>alert(1)</script>'"
    html = ar.render_analysis_report_html(ar.build_export_data(FakeStore(objects), JOB, DB))
    assert "</script><script>alert(1)" not in html
    # Only "<" needs escaping to keep the element from closing early.
    assert "\\u003c/script>" in html
    # Exactly one script element from our own emission plus the Chart.js tag.
    assert html.count("</script>") <= 2


def test_truncation_banner_appears_and_is_honest():
    data = ar.build_export_data(FakeStore(_objects()), JOB, DB, journey_budget=1)
    html = ar.render_analysis_report_html(data)
    assert "Partial query detail" in html
    assert "busiest of 3 query journeys" in html


def test_no_banner_when_nothing_was_truncated(rendered):
    assert "Partial query detail" not in rendered


# ---------------------------------------------------------------------------
# Naming convention
# ---------------------------------------------------------------------------


def test_artifact_stem_shape():
    from datetime import UTC, datetime

    when = datetime(2026, 9, 2, tzinfo=UTC)
    assert artifact_stem(DB, "decision-report", JOB, when) == (
        "discourse_decision-report_29d77e81_20260902"
    )


def test_artifact_stem_sanitises_the_database_name():
    from datetime import UTC, datetime

    when = datetime(2026, 9, 2, tzinfo=UTC)
    assert artifact_stem("My DB (prod)!", "analysis-report", JOB, when) == (
        "my-db-prod_analysis-report_29d77e81_20260902"
    )
    assert artifact_stem("...", "x", JOB, when).startswith("database_x_")


def test_provenance_carries_identity_and_filename():
    prov = provenance(_report(), "analysis-report", "html", job_id=JOB, source_artifact="k")
    assert prov["artifact"] == "analysis-report"
    assert prov["database"] == DB
    assert prov["job_id"] == JOB
    assert prov["source_artifact"] == "k"
    assert prov["filename"].startswith("discourse_analysis-report_29d77e81_")
    assert prov["filename"].endswith(".html")


def test_decision_report_and_engineering_report_carry_provenance():
    from src.atx_orchestrator.runtime.artifacts import (
        render_decision_report_html,
        render_engineering_report_md,
    )

    report = _report()
    html_prov = provenance(report, "decision-report", "html", job_id=JOB)
    md_prov = provenance(report, "engineering-report", "md", job_id=JOB)

    html = render_decision_report_html(report, prov=html_prov)
    assert '<meta name="x-dbmod-artifact" content="decision-report">' in html
    assert "29d77e81" in html
    assert html_prov["filename"] in html

    md = render_engineering_report_md(report, prov=md_prov)
    assert md.startswith("---\n")
    assert f"job_id: {JOB}" in md
    assert md_prov["filename"] in md


def test_renderers_still_work_without_provenance():
    """Provenance is additive; the existing call shape must keep working."""
    from src.atx_orchestrator.runtime.artifacts import (
        render_decision_report_html,
        render_engineering_report_md,
    )

    report = _report()
    assert "<title>Decision Report" in render_decision_report_html(report)
    assert render_engineering_report_md(report).startswith("# Database Modernization")
