"""The engineering report's per-query decision-trace appendix (#339).

``report.json``'s ``query_traces`` holds the full trace of every query whose
routing has a story (moved after its first assignment, ever left tied,
overridden) and counts the rest by their first rule; ``facts.query_trace``
holds the figures (ADR-032). The engineering report and the PDF report render
the same appendix from both; neither counts anything.
The traces come from the wordpress sample's real v1/v2 ledger, trimmed
(``tests/fixtures/wordpress_move_trace.json``).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from src.contracts.synthesis_output import SynthesisOutputContract
from src.report.renderers import render_engineering_report_md
from src.shared.query_trace import query_trace_digest
from src.shared.report_facts import compute_report_facts
from tests.executive_pdf import engineering_text

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "wordpress_move_trace.json"
APPENDIX = "## Appendix: per-query routing decisions"


def _digest() -> dict[str, Any]:
    sample = json.loads(FIXTURE.read_text())
    versions = {1: sample["v1"], 2: sample["v2"]}
    digest = query_trace_digest(sample["v2"], versions.get, 2)
    return digest.model_dump(mode="json", by_alias=True)


def _report(query_traces: dict[str, Any] | None) -> dict[str, Any]:
    report: dict[str, Any] = {
        "database_name": "wordpress",
        "job_id": "job-1",
        "timestamp": "2026-10-15T00:00:00Z",
        "ranking": [
            {"target": "dynamodb", "confidence_score": 70, "workload_percent": 60.0},
            {"target": "aurora_mysql", "confidence_score": 80, "workload_percent": 40.0},
        ],
        "recommended_architecture": {"databases": [{"service": "dynamodb", "table_count": 1}]},
        "schema_designs": {},
        "tied_queries": [],
    }
    if query_traces is not None:
        report["query_traces"] = query_traces
    report["facts"] = compute_report_facts(report)
    return report


@pytest.fixture
def report() -> dict[str, Any]:
    return _report(_digest())


def _appendix(md: str) -> str:
    start = md.index(APPENDIX)
    end = md.find("\n## ", start + 1)
    end = md.find("\n---\n", start) if end == -1 else end
    return md[start:end]


def test_the_digest_validates_against_the_synthesis_contract(report) -> None:
    contract = SynthesisOutputContract.model_validate(
        {
            **report,
            "summary": "s",
            "summary_deterministic": "s",
            "query_groups": [],
            "table_mappings": [],
            "tco_analysis": {
                "current_monthly_cost": 0.0,
                "projected_monthly_cost": 0.0,
                "savings_percent": 0.0,
            },
            "risk_assessment": {"overall_risk_level": "LOW", "risks": []},
        }
    )
    assert contract.query_traces is not None
    dumped = contract.model_dump(mode="json")
    assert dumped["query_traces"]["queries"][0]["first_assignment"]["pass"]


def test_the_appendix_lists_each_listed_query_with_every_step(report) -> None:
    md = render_engineering_report_md(report)
    facts = report["facts"]["query_trace"]
    appendix = _appendix(md)
    assert f"{APPENDIX} ({facts['listed_query_count']} " in appendix
    for trace in report["query_traces"]["queries"]:
        assert f"### `{trace['query_id']}`" in appendix
        # Every step's sentence, the final engine, the runner-up and the summary.
        for step in [trace["first_assignment"], *trace["moves"]]:
            assert step["explanation"].replace("(", "\\(").replace(")", "\\)") in appendix
        if trace["runner_up"]:
            assert "- Runner-up: " in appendix
    # The first assignment's every engine, with its score or its gate.
    first = report["query_traces"]["queries"][0]["first_assignment"]
    for c in first["candidates"]:
        assert c["engine_name"] in appendix


def test_a_moved_query_states_where_it_started_and_ended(report) -> None:
    moved = next(
        t
        for t in report["query_traces"]["queries"]
        if t["first_assignment"]["to_engine"] != t["final_engine"]
    )
    appendix = _appendix(render_engineering_report_md(report))
    head = next(
        line for line in appendix.splitlines() if line.startswith(f"### `{moved['query_id']}`")
    )
    assert "moved" in head
    assert f"{moved['first_assignment']['to_engine_name']} to {moved['final_engine_name']}" in head
    # The engine that scored higher and the step that decided against it.
    if moved["outscored_by"]:
        assert "- Scored higher: " in appendix


def test_the_rest_are_summarised_by_rule_not_listed(report) -> None:
    appendix = _appendix(render_engineering_report_md(report))
    facts = report["facts"]["query_trace"]
    assert f"The other {facts['unlisted_query_count']} queries" in appendix
    for rule in report["facts"]["query_trace"]["unlisted_rules"]:
        assert f"- {rule['label']}: {rule['rule_query_count']} " in appendix
    assert appendix.count("### `") == facts["listed_query_count"]


def test_the_figures_are_the_stored_facts(report) -> None:
    """ADR-032: change the stored fact, every figure the deliverables print follows."""
    stored = report["facts"]["query_trace"]
    stored.update(
        listed_query_count=42,
        unlisted_query_count=1000,
        moved_query_count=31,
        ever_tied_query_count=9,
        open_tie_query_count=4,
        overridden_query_count=6,
        pinned_query_count=5,
    )
    stored["unlisted_rules"][0]["rule_query_count"] = 777
    md = render_engineering_report_md(report)
    assert f"{APPENDIX} (42 queries)" in md
    assert "The other 1000 queries" in md
    assert (
        "(31 changed engine after the first assignment, 9 tied at some step (4 still open), "
        "6 placed by the customer or a design review, 5 pinned by a listed rule)"
    ) in md
    assert f"- {stored['unlisted_rules'][0]['label']}: 777 queries" in md
    text = engineering_text(report)
    assert "(42 queries)" in text and "The other 1000 queries" in text


def test_the_md_carries_the_same_appendix(report) -> None:
    text = engineering_text(report)
    n = report["facts"]["query_trace"]["listed_query_count"]
    assert f"({n} queries)" in text
    for trace in report["query_traces"]["queries"]:
        assert trace["query_id"] in text


def test_a_large_workload_stays_readable() -> None:
    """Discourse-sized (1654 queries): only the listed queries get a block."""
    digest = _digest()
    digest["query_count"] = 1654
    digest["queries"] = digest["queries"][:2]
    digest["unlisted_rules"] = [
        {"rule": "highest_score", "label": "highest score", "query_count": 1238},
        {
            "rule": "tie.source_compatible",
            "label": "tie settled by source compatibility",
            "query_count": 214,
        },
        {"rule": "group.mean", "label": "co-dependency group", "query_count": 200},
    ]
    report = _report(digest)
    appendix = _appendix(render_engineering_report_md(report))
    assert appendix.count("### `") == 2
    assert "The other 1652 queries" in appendix
    assert len(appendix.splitlines()) < 80


def test_no_appendix_without_traces_or_without_a_ledger() -> None:
    for traces in (None, {"has_ledger": False, "query_count": 7, "queries": []}):
        report = _report(traces)
        assert APPENDIX not in render_engineering_report_md(report)


def test_nothing_moved_still_says_how_every_query_was_placed() -> None:
    digest = _digest()
    digest["queries"] = []
    report = _report(digest)
    appendix = _appendix(render_engineering_report_md(report))
    assert "No query changed engine after its first assignment" in appendix


def test_trace_text_is_escaped(report) -> None:
    """A tampered report.json cannot inject Markdown or HTML through a trace."""
    evil = "<script>x</script> | [click](http://evil) ![i](http://evil/i.png)"
    tampered = copy.deepcopy(report)
    trace = tampered["query_traces"]["queries"][0]
    trace["query_id"] = "q`1<b>"
    trace["first_assignment"]["explanation"] = evil
    trace["first_assignment"]["candidates"][0]["engine_name"] = evil
    trace["summary"] = evil
    if trace["runner_up"]:
        trace["runner_up"]["explanation"] = evil
    tampered["query_traces"]["unlisted_rules"][0]["label"] = evil
    tampered["facts"] = compute_report_facts({k: v for k, v in tampered.items() if k != "facts"})
    md = render_engineering_report_md(tampered)
    assert "<script>" not in md
    assert "](http" not in md
    # The id stays inside its code span (where markup is inert), its backtick stripped.
    assert "### `q1<b>`" in md and "q`1" not in md


def test_the_tie_wording_on_the_trailgear_numbers(report) -> None:
    """#339 review, on trailgear's figures: 8 queries tied at some step, 5 of them
    settled in v2, 3 still open ("Unresolved engine ties (3 queries)"). Only the
    open ones may read "unresolved"; a settled one "was tied"."""
    stored = report["facts"]["query_trace"]
    stored.update(
        listed_query_count=25,
        traced_query_count=95,
        unlisted_query_count=70,
        moved_query_count=17,
        ever_tied_query_count=8,
        open_tie_query_count=3,
        overridden_query_count=0,
        pinned_query_count=0,
    )
    md = render_engineering_report_md(report)
    assert "(17 changed engine after the first assignment, 8 tied at some step (3 still open))" in (
        md
    )
    assert "left in an unresolved tie" not in md
    # The wordpress query whose placeholder tie the reality check settled.
    settled = next(r for r in stored["listed_queries"] if r["tied"])
    assert not settled["open_tie"]
    head = next(line for line in md.splitlines() if line.startswith(f"### `{settled['query_id']}`"))
    assert "(was tied)" in head and "unresolved" not in head
    # The same query with its tie still open.
    settled["open_tie"] = True
    md = render_engineering_report_md(report)
    head = next(line for line in md.splitlines() if line.startswith(f"### `{settled['query_id']}`"))
    assert "(unresolved tie)" in head


def test_steps_carry_the_querys_own_number(report) -> None:
    appendix = _appendix(render_engineering_report_md(report))
    assert "- Step 1, version 1 (first assignment, " in appendix
    assert "- Step 2, version 2 (" in appendix
    assert "seq" not in appendix


def test_each_block_names_its_tables_and_the_start_of_its_sql(report) -> None:
    trace = report["query_traces"]["queries"][0]
    trace["source_tables"] = ["wordpress.wp_wc_product_meta_lookup", "a", "b", "c"]
    trace["query_excerpt"] = "REPLACE INTO wp_wc_product_meta_lookup (`product_id`, sku) VALUES"
    appendix = _appendix(render_engineering_report_md(report))
    head = next(
        line for line in appendix.splitlines() if line.startswith(f"### `{trace['query_id']}`")
    )
    assert "on `wordpress.wp_wc_product_meta_lookup`, `a`, `b` (+1 more)" in head
    # The SQL sits in a code span right under the heading, its backticks stripped.
    assert "`REPLACE INTO wp_wc_product_meta_lookup (product_id, sku) VALUES`" in appendix


def test_listed_queries_beyond_the_cap_are_summarised(report) -> None:
    stored = report["facts"]["query_trace"]
    stored["listed_queries"][1]["in_full"] = False
    stored.update(full_trace_count=stored["listed_query_count"] - 1, not_in_full_count=1)
    appendix = _appendix(render_engineering_report_md(report))
    hidden = stored["listed_queries"][1]["query_id"]
    assert f"### `{hidden}`" not in appendix
    assert (
        "1 more listed query is in the Assessment Data (JSON) under `query_traces.more_listed`"
        in appendix
    )
    assert "Why this engine" in appendix


def test_the_group_mean_is_printed_and_the_basis_named_only_with_a_score(report) -> None:
    trace = report["query_traces"]["queries"][0]
    trace["final_group_mean"] = 80.1
    trace["runner_up"].update(basis="group_mean", score=None, gate="group_owner")
    appendix = _appendix(render_engineering_report_md(report))
    assert "its co-dependency group's mean score there is 80.1" in appendix
    block = appendix[appendix.index(f"### `{trace['query_id']}`") :].split("\n### ")[0]
    assert "Compared on the co-dependency group's mean scores" not in block
    trace["runner_up"]["score"] = 70.0
    appendix = _appendix(render_engineering_report_md(report))
    block = appendix[appendix.index(f"### `{trace['query_id']}`") :].split("\n### ")[0]
    assert "Compared on the co-dependency group's mean scores" in block
