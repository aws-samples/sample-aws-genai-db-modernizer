"""The per-engine rationale reaches the reports readers (and the judge) open (#152).

Synthesis builds it from the routed workload ("90% mean fit across 98 queries (22
rated tables), led by key-value lookups (36 of 98)"); the engineering report's
Target engines table shows it, and the PDF report prints the same table.
"""

from __future__ import annotations

from typing import Any

from src.report import architecture, renderers
from src.shared import report_facts

DDB = "90% mean fit across 98 queries (22 rated tables), led by key-value lookups (36 of 98)."
AURORA = "87% mean fit across 5 queries (7 rated tables)."
OS = "60% mean fit across 3 queries (signal only — no table-level evidence), led by full-text search (3 of 3)."


def _report() -> dict[str, Any]:
    return {
        "database_name": "wordpress",
        "ranking": [
            {"target": "dynamodb", "workload_percent": 91.6, "rationale": DDB},
            # retained engine: not in databases, so its rationale comes from the ranking
            {"target": "aurora_mysql", "workload_percent": 4.7, "rationale": AURORA},
            {"target": "opensearch", "workload_percent": 3.7, "rationale": OS},
        ],
        "recommended_architecture": {
            "databases": [
                {"service": "dynamodb", "table_count": 20, "rationale": DDB},
                {"service": "opensearch", "table_count": 0, "rationale": OS},
            ]
        },
        "schema_designs": {"dynamodb": {"tables": [{}]}, "opensearch": {"tables": [{}]}},
    }


def test_the_md_shows_each_engines_rationale() -> None:
    from tests.executive_pdf import engineering_text

    text = engineering_text(_report())
    for rationale in (DDB, AURORA):
        assert " ".join(rationale.split()) in text


def test_engineering_report_lists_the_target_engines_with_their_rationale() -> None:
    md = renderers.render_engineering_report_md(_report())
    assert "## Target engines" in md
    assert "| Engine | Role | Workload | Scope | Why |" in md
    for text in (DDB, AURORA, OS):
        assert text in md


def test_rationale_is_escaped_in_markdown() -> None:
    rep = _report()
    rep["ranking"][0]["rationale"] = "<script>x</script> | y"
    rep["recommended_architecture"]["databases"][0]["rationale"] = "<script>x</script> | y"
    md = renderers.render_engineering_report_md(rep)
    assert "<script>x</script>" not in md
    assert "&lt;script&gt;x&lt;/script&gt; \\| y" in md


def test_legacy_list_rationale_reads_as_text() -> None:
    rep = _report()
    del rep["ranking"][1]["rationale"]
    rep["ranking"][1]["assignment_reason_summary"] = ["highest confidence"]
    rows = {e["engine"]: e for e in architecture._architecture_engines(rep)}
    assert rows["aurora_mysql"]["rationale"] == ""
    assert report_facts.rationale_text(["a", "b"]) == "a; b"
