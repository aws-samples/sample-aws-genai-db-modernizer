"""The gate block and final summary /modernize quotes mirror the UI, from facts (#1307, ADR-032).

The snapshots under ``snapshots/`` pin the full printed text of one synthetic job. After an
intended wording change, rewrite them with
``PRINTED_SUMMARY_WRITE_SNAPSHOTS=1 uv run pytest tests/unit/report/test_printed_summary.py``
and read the diff.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

from src.report.deliverables import FOLDER_ENTRIES
from src.report.printed_summary import (
    MIN_LISTED_RISKS,
    listed_risks,
    ordered_risks,
    prose,
    render_final_summary,
    render_gate_block,
)

SNAPSHOTS = Path(__file__).parent / "snapshots"

GATE: dict[str, Any] = {
    "before_distribution": {"aurora_mysql": 59, "dynamodb": 38, "documentdb": 9},
    "after_distribution": {"aurora_mysql": 67, "dynamodb": 40, "documentdb": 0},
    "eliminated_engines": ["documentdb"],
    "consolidations": [
        {"from": "documentdb", "to": "aurora_mysql", "queries": 7, "reason": "No unique features"},
        {"from": "documentdb", "to": "dynamodb", "queries": 1},
    ],
    "sweep_kept": [{"engine": "opensearch", "reason": "Full-text search stays"}],
    "cache_overlay": {"query_count": 20, "call_share_percent": 83.4},
    "schema_design_engines": ["aurora_mysql", "dynamodb", "elasticache"],
    "pending_choices": [],
    "executive_summary": "Reality check removed DocumentDB and kept the rest.",
    "architectural_patterns": [
        {
            "name": "Event-Driven Sync",
            "when": "Several engines need the same data",
            "example": "Streams feed the cache.",
            "applies_to": {},
        }
    ],
}

PENDING = {
    "choice": "c1",
    "table": "theme_fields",
    "queries": 13,
    "writes": 2,
    "engines": [
        {
            "engine": "documentdb",
            "score": 71,
            "unrounded_score": 70.8,
            "trade_off": "Flexible docs",
        },
        {"engine": "dynamodb", "score": 71, "unrounded_score": 70.6, "trade_off": "Key access"},
    ],
    "why_tied": "Both score 71 for these queries.",
    "listed_under": "dynamodb",
    "decide_for_me": "dynamodb",
    "decide_for_me_reason": "Higher unrounded score.",
}


def _gate(**changes: Any) -> str:
    return render_gate_block({**GATE, **changes}, database_name="wordpress", job_id="job1")


def _check_snapshot(name: str, text: str) -> None:
    path = SNAPSHOTS / name
    if os.environ.get("PRINTED_SUMMARY_WRITE_SNAPSHOTS"):
        path.write_text(text, encoding="utf-8")
    assert text == path.read_text(encoding="utf-8")


def test_the_gate_block_snapshot() -> None:
    _check_snapshot("gate.txt", _gate(pending_choices=[PENDING]))


def test_the_gate_block_follows_the_assignment_page() -> None:
    text = _gate()
    assert "Recommended architecture: Aurora MySQL, DynamoDB and ElastiCache" in text
    assert "Reality check removed DocumentDB and kept the rest." in text
    assert "| Aurora MySQL | 59 | 67 | 62.6% | kept |" in text
    assert "| DocumentDB | 9 | 0 | 0% | dropped |" in text
    assert "Total: 107 queries." in text
    assert "- DocumentDB to Aurora MySQL: 7 queries. No unique features" in text
    assert "- DocumentDB to DynamoDB: 1 query" in text
    assert "- OpenSearch: Full-text search stays" in text
    assert (
        "Cache layer: ElastiCache has 20 cache candidates (83.4% of calls) and owns no queries."
        in text
    )
    assert "- Event-Driven Sync\n  - When: Several engines need the same data" in text
    assert "Schema design runs next for: Aurora MySQL, DynamoDB, ElastiCache." in text
    assert "changed nothing" not in text


def test_the_gate_block_says_when_reality_check_changed_nothing() -> None:
    same = {"aurora_mysql": 4, "dynamodb": 1}
    text = _gate(
        before_distribution=same,
        after_distribution=same,
        eliminated_engines=[],
        consolidations=[],
        sweep_kept=[],
        cache_overlay=None,
        architectural_patterns=[],
        executive_summary=None,
        schema_design_engines=["aurora_mysql", "dynamodb"],
    )
    assert "Reality check changed nothing" in text
    for absent in ("Consolidations", "Cache layer", "Small engines kept", "patterns"):
        assert absent not in text


def test_tied_engine_details_are_printed_in_full() -> None:
    text = _gate(pending_choices=[PENDING])
    assert "- `theme_fields` (13 queries): Both score 71 for these queries." in text
    assert "  - DocumentDB, score 71: Flexible docs" in text
    assert "  - DynamoDB, score 71: Key access" in text
    assert "  - Decide for me picks DynamoDB: Higher unrounded score." in text
    # The engines designed next change once the choices are settled, so none is promised.
    assert "Schema design runs next" not in text


def test_the_gate_block_carries_no_url_and_escapes_untrusted_text() -> None:
    text = _gate(sweep_kept=[{"engine": "opensearch", "reason": "see [x](http://evil/x.png)"}])
    assert "[x](" not in text


def test_the_gate_block_ends_with_the_summary_page_url_when_given() -> None:
    # The URL the user follows before approving is part of the block itself, so quoting
    # the block quotes it: no second command at the gate (#1307).
    url = "http://localhost:3000/analysis/monitor/summary/job1"
    text = render_gate_block(GATE, database_name="wordpress", job_id="job1", summary_url=url)
    assert text.rstrip().endswith(
        f"- Summary page (the query flow and every query's engine): {url}"
    )
    assert text.count(url) == 1
    assert "/analysis/" not in _gate()


def _report(**changes: Any) -> dict[str, Any]:
    risks = [
        {
            "risk_id": f"RISK-00{i}",
            "severity": "HIGH" if i < 3 else "MEDIUM",
            "risk_type": "DATA_CONSISTENCY",
            "description": f"[dynamodb] Risk number {i}",
            "mitigation": f"Mitigation number {i}",
        }
        for i in range(1, 8)
    ]
    report: dict[str, Any] = {
        "summary": "Analyzed 50 tables.\nA second paragraph.",
        "summary_deterministic": "Analyzed 50 tables.\nA second paragraph.",
        "summary_source": "deterministic",
        "ranking": [],
        "facts": {
            "projected_monthly_cost_usd": 582.76,
            "engine_count": 3,
            "migration_wave_count": 3,
            "open_risk_count": 7,
            "open_risks_by_severity": {"HIGH": 2, "MEDIUM": 5},
            "resolved_risk_count": 2,
            "resolved_risks_by_severity": {"MEDIUM": 2},
            "engines": [
                {
                    "engine": "aurora_mysql",
                    "role": "Retained",
                    "workload_percent": 62.6,
                    "tables_migrate": 0,
                    "monthly_cost_usd": 318.8,
                    "rationale": "73% mean fit across 67 queries (2 queries without table-level evidence).",
                },
                {
                    "engine": "elasticache",
                    "role": "Cache layer",
                    "workload_percent": None,
                    "cached_queries": 20,
                    "cached_call_share_percent": 83.4,
                    "tables_migrate": 0,
                    "monthly_cost_usd": 165.55,
                    "rationale": "Cache layer for 20 hot reads.",
                },
            ],
            "retired_in_use_engines": [],
            "query_pattern_counts": {"query_count": 107, "pattern_count": 97},
        },
        "tco_analysis": {
            "current_monthly_cost": 0.0,
            "current_cost_known": False,
            "projected_monthly_cost": 582.76,
            "savings_percent": 0.0,
        },
        "risk_assessment": {"risks": risks, "overall_risk_level": "HIGH"},
        "cache_overlay": {
            "engine": "elasticache",
            "query_count": 12,
            "call_share_percent": 70.0,
            "candidate_query_count": 15,
            "candidate_call_share_percent": 75.0,
        },
    }
    report.update(changes)
    return report


def _final(report: dict[str, Any], entries=FOLDER_ENTRIES, **urls: str) -> str:
    return render_final_summary(
        report,
        database_name="wordpress",
        job_id="job1",
        deliverables_dir="artifacts/wordpress/job1/deliverables/",
        deliverables=entries,
        **urls,
    )


def test_the_final_summary_snapshot() -> None:
    text = _final(
        _report(),
        summary_url="http://localhost:3000/analysis/monitor/summary/job1",
        results_url="http://localhost:3000/analysis/results/job1",
    )
    _check_snapshot("final.txt", text)


def test_the_final_summary_follows_the_results_page() -> None:
    text = _final(_report())
    assert text.index("### Executive summary") < text.index("### At a glance")
    assert text.index("### At a glance") < text.index("### Engines")
    assert text.index("### Engines") < text.index("### Risks")
    assert text.index("### Risks") < text.index("### Where to look next")
    assert "- Workload: 107 queries in 97 distinct query patterns" in text
    assert "- Target engines: Aurora MySQL, ElastiCache (cache layer)" in text
    assert "- Projected monthly cost: $582.76" in text
    assert "- Migration: 3 waves" in text
    assert (
        "- Cache layer note: 15 cache candidates, 12 designed; 3 stay on their owner engine."
        in text
    )
    assert "Overall risk: HIGH" in text
    assert "Open risks: 7 (2 high, 5 medium)" in text
    assert "Resolved by the assignment: 2 (2 medium)" in text


def test_the_executive_summary_is_verbatim_and_withheld_notes_are_kept() -> None:
    text = _final(_report())
    assert "### Executive summary\n\nAnalyzed 50 tables.\n\nA second paragraph.\n" in text
    report = _report(summary_source="deterministic_fallback", summary="a generated text")
    report["summary_validation_warnings"] = ["contradicted the records"]
    assert "*The generated narrative was withheld" in _final(report)
    assert "No executive summary was generated" in _final(
        _report(summary="", summary_deterministic="")
    )


def test_one_line_per_engine_with_why_and_the_cache_layer_never_a_workload_share() -> None:
    lines = _final(_report()).splitlines()
    aurora = next(line for line in lines if line.startswith("- Aurora MySQL ("))
    assert aurora == (
        "- Aurora MySQL (Retained, 62.6% of queries, $318.80/mo): 73% mean fit across 67 "
        "queries \\(2 queries without table-level evidence\\)."
    )
    cache = next(line for line in lines if line.startswith("- ElastiCache ("))
    assert cache.startswith(
        "- ElastiCache (Cache layer, 20 designed cached reads · 83.4% of calls, $165.55/mo)"
    )
    assert "of queries" not in cache


def test_no_source_cost_leaves_the_current_cost_and_savings_lines_out() -> None:
    text = _final(_report())
    assert "Current monthly cost" not in text and "Savings" not in text
    assert "not provided" not in text and "not available" not in text


def test_a_known_source_cost_adds_the_current_cost_and_savings() -> None:
    tco = {
        "current_monthly_cost": 900.0,
        "current_cost_known": True,
        "projected_monthly_cost": 582.76,
        "savings_percent": 35.2,
    }
    text = _final(_report(tco_analysis=tco))
    assert "- Current monthly cost (source): $900.00" in text
    assert "- Savings: 35.2%" in text


def test_a_retired_engine_the_source_runs_today_is_stated() -> None:
    report = _report()
    report["facts"]["retired_in_use_engines"] = [
        {"engine": "documentdb", "monthly_cost_usd": 367.39}
    ]
    assert "- Retired by this plan: DocumentDB (runs today), $367.39/mo" in _final(report)


def test_risks_are_sorted_by_severity_and_every_critical_and_high_is_listed_in_full() -> None:
    report = _report()
    risks = report["risk_assessment"]["risks"]
    for r in risks:
        r["severity"] = "LOW"
    risks[5]["severity"] = "CRITICAL"
    risks[1]["severity"] = "high"
    for i in (0, 2, 3, 4):  # four more HIGH: six serious risks in all
        risks[i]["severity"] = "HIGH"
    report["facts"]["open_risk_count"] = 7
    report["facts"]["open_risks_by_severity"] = {"CRITICAL": 1, "HIGH": 5, "LOW": 1}
    risks[0]["description"] = "[dynamodb] " + "long text " * 80
    risks[0]["mitigation"] = "a long mitigation " * 40
    text = _final(report)
    listed = [line for line in text.splitlines() if line.startswith("- ") and ", " in line[:20]]
    assert listed[0].startswith("- CRITICAL, DynamoDB (RISK-006): Risk number 6")
    assert [line.split(",")[0] for line in listed] == ["- CRITICAL"] + ["- HIGH"] * 5
    assert len(listed) == 6 > MIN_LISTED_RISKS  # all serious ones, past the minimum
    assert ("long text " * 80).strip().replace("(", "") in text  # not cut
    assert "a long mitigation " * 39 in text
    assert "Risk number 7" not in text  # the one LOW risk is the only one left out
    assert "list every open risk" in text


def test_the_list_is_topped_up_to_the_minimum_with_the_next_most_severe() -> None:
    report = _report()
    for r in report["risk_assessment"]["risks"]:
        r["severity"] = "MEDIUM"
    report["risk_assessment"]["risks"][6]["severity"] = "LOW"
    assert len(listed_risks(report)) == MIN_LISTED_RISKS
    assert [r["risk_id"] for r in listed_risks(report)] == [f"RISK-00{i}" for i in range(1, 6)]
    assert [r["risk_id"] for r in ordered_risks(report)][-1] == "RISK-007"


def test_fewer_risks_than_the_minimum_are_all_listed_without_the_pointer() -> None:
    report = _report()
    report["risk_assessment"]["risks"] = report["risk_assessment"]["risks"][:3]
    report["facts"]["open_risk_count"] = 3
    text = _final(report)
    assert text.count("  - Mitigation:") == 3
    assert "list every open risk" not in text


def test_an_unrated_risk_sorts_after_low() -> None:
    report = _report()
    report["risk_assessment"]["risks"] = [
        {"severity": "weird", "description": "[dynamodb] odd one"},
        {"severity": "LOW", "description": "[dynamodb] low one"},
    ]
    report["facts"]["open_risk_count"] = 2
    lines = [
        line
        for line in _final(report).splitlines()
        if line.startswith("- UNRATED") or line.startswith("- LOW")
    ]
    assert "low one" in lines[0] and lines[1].startswith("- UNRATED")


def test_a_mitigation_that_repeats_the_description_is_not_stated_twice() -> None:
    report = _report()
    report["risk_assessment"]["risks"][0]["mitigation"] = "Risk number 1"
    assert "Mitigation: Risk number 1" not in _final(report)


def test_no_open_risks_states_the_caveat_not_a_zero_risk_claim() -> None:
    report = _report()
    report["risk_assessment"]["risks"] = []
    report["facts"]["open_risk_count"] = 0
    report["facts"]["open_risks_by_severity"] = {}
    text = _final(report)
    assert "Open risks: none. No open risks is not the same as no migration risk" in text
    assert "Most severe first" not in text


def test_a_code_span_in_risk_text_shows_no_backslashes_and_text_outside_it_is_escaped() -> None:
    assert prose("Table `wp_api_keys` (5 ties) [x](http://evil/x.png)") == (
        "Table `wp_api_keys` \\(5 ties\\) \\[x\\]\\(http://evil/x.png\\)"
    )


def test_where_to_look_next_lists_the_files_and_the_page_urls() -> None:
    text = _final(
        _report(), results_url="http://localhost:3000/r", summary_url="http://localhost:3000/s"
    )
    tail = text.split("### Where to look next")[1]
    assert (
        "`artifacts/wordpress/job1/deliverables/executive-report.pdf`: Executive report (PDF)"
        in tail
    )
    assert "`artifacts/wordpress/job1/deliverables/engineering-report.md`" in tail
    assert "`artifacts/wordpress/job1/deliverables/index.md`" in tail
    assert "- Results page (the interactive report): http://localhost:3000/r" in tail
    assert "- Summary page (the query flow and the assignment): http://localhost:3000/s" in tail
    assert "Results page" not in _final(_report())  # chat mode lists no URL


def test_a_file_that_was_not_rendered_is_named_and_none_rendered_is_stated() -> None:
    text = _final(_report(), entries=FOLDER_ENTRIES[1:])
    assert "- Not rendered this time: `executive-report.pdf`" in text
    assert "deliverables/executive-report.pdf`:" not in text
    assert "No deliverable is rendered yet." in _final(_report(), entries=())


def test_the_output_is_the_same_every_time_and_does_not_mutate_its_input() -> None:
    report = _report()
    before = copy.deepcopy(report)
    assert _final(report) == _final(report)
    assert report == before
    assert _gate() == _gate()


def test_the_summary_never_narrates_the_run() -> None:
    text = (_final(_report()) + _gate()).lower()
    for phrase in ("i haven't", "i did not", "didn't pull", "subagent", "phase ", "step "):
        assert phrase not in text
