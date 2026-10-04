"""The per-engine rationale reaches the reports readers (and the judge) open (#152).

Synthesis builds it from the routed workload ("90% mean fit across 98 queries (22
tables), led by key-value lookups (36 of 98)"); the decision report's
recommended-architecture table and the engineering report's Target engines table
show it.
"""

from __future__ import annotations

from typing import Any

from src.report import renderers

DDB = "90% mean fit across 98 queries (22 tables), led by key-value lookups (36 of 98)."
AURORA = "87% mean fit across 5 queries (7 tables)."
OS = "60% mean fit across 3 queries (no table-level evidence), led by full-text search (3 of 3)."


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


def test_decision_report_shows_each_engines_rationale() -> None:
    html = renderers.render_decision_report_html(_report())
    assert "<th>Why</th>" in html
    for text in (DDB, AURORA, OS):  # no HTML-special characters, so verbatim
        assert f"<td class=why>{text}</td>" in html


def test_engineering_report_lists_the_target_engines_with_their_rationale() -> None:
    md = renderers.render_engineering_report_md(_report())
    assert "## Target engines" in md
    assert "| Engine | Role | Workload | Why |" in md
    for text in (DDB, AURORA, OS):
        assert text in md


def test_rationale_is_escaped_in_html() -> None:
    rep = _report()
    rep["ranking"][0]["rationale"] = "<script>x</script>"
    rep["recommended_architecture"]["databases"][0]["rationale"] = "<script>x</script>"
    html = renderers.render_decision_report_html(rep)
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;x&lt;/script&gt;" in html


def test_legacy_list_rationale_reads_as_text() -> None:
    rep = _report()
    del rep["ranking"][1]["rationale"]
    rep["ranking"][1]["assignment_reason_summary"] = ["highest confidence"]
    rows = {e["engine"]: e for e in renderers._architecture_engines(rep)}
    assert rows["aurora_mysql"]["rationale"] == ""
    assert renderers._rationale_text(["a", "b"]) == "a; b"
