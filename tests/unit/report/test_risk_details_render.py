"""#950: a risk's design texts (``Risk.details``) are listed in full in the
engineering report, and the executive report states only their count, so no
clipped quote ("…") ever reaches the PDF (the e2e-llm PDF check found one)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from src.agents.referee.migration_risks import build_migration_risks
from src.report.escaping import md_text
from src.report.executive_report import render_executive_report_pdf
from src.report.facts import report_facts
from src.report.renderers import render_engineering_report_md
from tests.executive_pdf import text

FIXTURE = Path(__file__).parent / "fixtures" / "wordpress_report.json"

LONG = (
    "Lookup by post title text with a status list, a sort and a limit. Titles are free text "
    "and the cache keys only on the post id, so the design cannot serve this read from a key, "
    "and the status filter changes with every publish."
)
WAVES = [
    {"wave": 1, "engines": ["aurora_mysql"], "homogeneity": "homogeneous", "moves_from": ["mysql"]},
    {"wave": 2, "engines": ["elasticache"], "moves_from": []},
    {"wave": 3, "engines": ["dynamodb"], "moves_from": ["aurora_mysql"]},
]


def _report() -> tuple[dict, list[dict]]:
    risks = build_migration_risks(
        migration_waves=WAVES,
        query_assignments=[],
        source_queries=[],
        schema_designs={
            "elasticache": {
                "unsupported_patterns": [
                    {"source_query_ids": [f"q{i}"], "reason": f"{LONG} ({i})", "workaround": LONG}
                    for i in range(6)
                ]
            }
        },
        cache_only_engines={"elasticache"},
        trade_offs=[{"description": f"{LONG} [{i}]", "engine": "dynamodb"} for i in range(8)],
        start_id=50,
        source_engine="mysql",
    )
    report = copy.deepcopy(json.loads(FIXTURE.read_text()))
    report["risk_assessment"]["risks"] = [*report["risk_assessment"]["risks"], *risks]
    report.pop("facts", None)
    report["facts"] = report_facts(report)
    return report, risks


def test_long_design_texts_never_reach_the_pdf_clipped() -> None:
    report, risks = _report()
    assert {r["rule"] for r in risks} == {"cache.unsupported", "design.trade_offs"}
    pdf = text(render_executive_report_pdf(report))
    assert "…" not in pdf and "..." not in pdf
    for r in risks:
        assert r["risk_id"] in pdf
        assert "…" not in r["description"] and "…" not in r["mitigation"]
    assert "Lookup by post title" not in pdf


def test_the_engineering_report_lists_every_design_text_in_full() -> None:
    report, risks = _report()
    md = render_engineering_report_md(report)
    for r in risks:
        for detail in r["details"]:
            assert f"  - {md_text(detail)}" in md, detail[:40]
    assert md.count(md_text(f"{LONG} [7]")) == 1
