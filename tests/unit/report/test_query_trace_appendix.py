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
from pydantic import ValidationError

from src.contracts.synthesis_output import QueryTraceGroup, SynthesisOutputContract
from src.report.escaping import md_text
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
        assert f"- {md_text(moved['outscored_by'][0]['explanation'])}" in appendix


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


# ---------------------------------------------------------------------------
# #1108: queries whose trace reads the same share one appendix entry
# ---------------------------------------------------------------------------


def _clones(count: int, *, prefix: str = "c") -> dict[str, Any]:
    """The fixture's first trace repeated for ``count`` queries with their own ids and
    tables: what a reality check that removes an engine writes for every query it moves."""
    digest = _digest()
    base = digest["queries"][0]
    clones = []
    for i in range(count):
        clone = copy.deepcopy(base)
        clone["query_id"] = f"{prefix}{i:03d}{'0' * 12}"
        clone["source_tables"] = [f"wordpress.t{i}"]
        clone["query_excerpt"] = f"SELECT {i} FROM t{i}"
        clones.append(clone)
    digest["queries"] = clones
    digest["query_count"] = count
    return digest


def _groups(report: dict[str, Any]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = report["facts"]["query_trace"]["trace_groups"]
    return groups


def test_near_identical_entries_become_one_entry_with_a_count() -> None:
    report = _report(_clones(25))
    facts = report["facts"]["query_trace"]
    assert facts["listed_query_count"] == 25
    assert facts["trace_group_count"] == 1
    assert _groups(report)[0]["query_count"] == 25
    appendix = _appendix(render_engineering_report_md(report))
    assert appendix.count("\n### ") == 1
    assert "### 25 queries (moved): " in appendix
    assert "1 entry below cover 25 queries" in appendix
    # The story is told once.
    assert appendix.count("- Step 1, version 1") == 1
    assert appendix.count("- Final engine: ") == 1


def test_every_query_is_findable_once_by_its_short_id() -> None:
    report = _report(_clones(25))
    appendix = _appendix(render_engineering_report_md(report))
    for trace in report["query_traces"]["queries"]:
        short = trace["query_id"][:8]
        assert appendix.count(f"`{short}`") == 1, short
    group = _groups(report)[0]
    assert group["short_ids"] == [q[:8] for q in group["query_ids"]]


def test_no_query_is_lost_from_the_facts_and_the_per_query_list_stays() -> None:
    digest = _clones(7)
    other = copy.deepcopy(digest["queries"][0])
    other["query_id"] = "z" * 20
    other["final_score"] = 12.0
    digest["queries"].append(other)
    report = _report(digest)
    facts = report["facts"]["query_trace"]
    grouped = [q for g in facts["trace_groups"] for q in g["query_ids"]]
    in_full = [r["query_id"] for r in facts["listed_queries"] if r["in_full"]]
    assert sorted(grouped) == sorted(in_full) and len(set(grouped)) == len(grouped) == 8
    assert sum(g["query_count"] for g in facts["trace_groups"]) == 8
    # The UI's query explorer reads listed_queries: still one row per query.
    assert len(facts["listed_queries"]) == facts["listed_query_count"] == 8


def test_a_different_score_path_or_final_engine_is_its_own_entry() -> None:
    digest = _clones(4)
    digest["queries"][1]["final_score"] = 41.0
    digest["queries"][2]["first_assignment"]["candidates"][0]["score"] = 1.5
    digest["queries"][3]["final_engine"] = "aurora_postgresql"
    report = _report(digest)
    assert [g["query_count"] for g in _groups(report)] == [1, 1, 1, 1]
    appendix = _appendix(render_engineering_report_md(report))
    assert appendix.count("\n### ") == 4
    assert "1 entry" not in appendix and "entries below cover" not in appendix


def test_the_kinds_of_story_must_match_to_share_an_entry() -> None:
    digest = _clones(2)
    digest["queries"][1]["tied_engines"] = ["dynamodb", "aurora_mysql"]
    report = _report(digest)
    assert [g["query_count"] for g in _groups(report)] == [1, 1]


def test_grouping_is_deterministic_whatever_the_input_order() -> None:
    digest = _clones(6)
    digest["queries"][3]["final_score"] = 41.0
    digest["queries"][4]["final_score"] = 41.0
    forward = _report(copy.deepcopy(digest))
    digest["queries"].reverse()
    backward = _report(digest)
    assert forward["facts"]["query_trace"]["trace_groups"] == (
        backward["facts"]["query_trace"]["trace_groups"]
    )
    assert render_engineering_report_md(forward) == render_engineering_report_md(backward)
    assert [g["query_count"] for g in _groups(forward)] == [4, 2]


def test_a_short_id_that_two_queries_share_is_written_in_full() -> None:
    digest = _clones(3)
    for i, trace in enumerate(digest["queries"]):
        trace["query_id"] = f"abcdefgh{i}-rest" if i < 2 else "zzzzzzzz9-rest"
    report = _report(digest)
    group = _groups(report)[0]
    short = dict(zip(group["query_ids"], group["short_ids"], strict=True))
    assert short["abcdefgh0-rest"] == "abcdefgh0-rest"
    assert short["abcdefgh1-rest"] == "abcdefgh1-rest"
    assert short["zzzzzzzz9-rest"] == "zzzzzzzz"


def test_the_renderer_only_formats_the_stored_groups() -> None:
    """ADR-032: it neither counts nor regroups; the stored group is what it prints."""
    report = _report(_clones(5))
    group = _groups(report)[0]
    group["query_count"] = 99
    group["short_ids"] = ["feedface"] * 5
    appendix = _appendix(render_engineering_report_md(report))
    assert "### 99 queries" in appendix
    assert appendix.count("`feedface`") == 5


@pytest.mark.parametrize("stored", ["missing", "empty"])
def test_a_report_without_stored_groups_gets_them_from_the_facts_layer(stored: str) -> None:
    """A report.json written before the groups existed (contract 1.40), or one
    re-dumped through the contract, whose default is ``[]``: report_facts fills
    the groups in (ADR-032), so the appendix neither goes blank nor regroups."""
    report = _report(_clones(3))
    trace = report["facts"]["query_trace"]
    if stored == "missing":
        del trace["trace_groups"]
        del trace["trace_group_count"]
    else:
        trace.update(trace_groups=[], trace_group_count=0)
    appendix = _appendix(render_engineering_report_md(report))
    assert appendix.count("\n### ") == 1
    assert "### 3 queries (moved): " in appendix
    assert "1 entry below cover 3 queries" in appendix


def test_an_old_report_loaded_through_the_contract_still_prints_its_entries() -> None:
    """A 1.40 report.json, validated by today's contract (``trace_groups``
    defaults to ``[]``) and dumped: every query with a full trace still has an entry."""
    report = _report(_clones(3))
    report["facts"]["query_trace"].pop("trace_groups")
    report["facts"]["query_trace"].pop("trace_group_count")
    old = {
        **report,
        "contract_version": "1.40",
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
    loaded = SynthesisOutputContract.model_validate(old).model_dump(mode="json", by_alias=True)
    assert loaded["facts"]["query_trace"]["trace_groups"] == []
    appendix = _appendix(render_engineering_report_md(loaded))
    assert appendix.count("\n### ") == 1
    assert "### 3 queries (moved): " in appendix


def test_a_lone_query_keeps_its_tables_and_sql() -> None:
    report = _report(_clones(1))
    appendix = _appendix(render_engineering_report_md(report))
    assert "on `wordpress.t0`" in appendix and "`SELECT 0 FROM t0`" in appendix


def test_a_different_runner_up_or_outscoring_engine_is_its_own_entry() -> None:
    digest = _clones(3)
    digest["queries"][1]["runner_up"] = {
        **(digest["queries"][1]["runner_up"] or {}),
        "explanation": "Another engine came closest.",
    }
    digest["queries"][2]["outscored_by"] = [
        {**o, "explanation": "A different engine scored higher."}
        for o in digest["queries"][2]["outscored_by"]
    ] or [
        {
            "engine": "documentdb",
            "engine_name": "DocumentDB",
            "score": 90.0,
            "final_score": 70.0,
            "in_plan": False,
            "decided_by_version": 2,
            "decided_by_seq": 1,
            "decided_by_step": 2,
            "decided_by_rule": "reality_check.consolidate",
            "left_it": True,
            "explanation": "A different engine scored higher.",
        }
    ]
    report = _report(digest)
    assert [g["query_count"] for g in _groups(report)] == [1, 1, 1]


def test_a_group_heading_carries_its_open_tie_count() -> None:
    """The appendix's headings add up to "Unresolved engine ties": a group of
    queries still tied states how many, from the stored ``open_tie_count``."""
    digest = _clones(4)
    for trace in digest["queries"]:
        trace["tied_engines"] = ["dynamodb", "aurora_mysql"]
        trace["tied_engine_names"] = ["DynamoDB", "Aurora MySQL"]
    report = _report(digest)
    facts = report["facts"]["query_trace"]
    (group,) = _groups(report)
    assert group["open_tie_count"] == 4 == facts["open_tie_query_count"]
    appendix = _appendix(render_engineering_report_md(report))
    assert "### 4 queries (moved, 4 unresolved ties): " in appendix
    assert "each query is listed under the final engine" in appendix
    # The renderer prints the stored count, never its own.
    group["open_tie_count"] = 7
    assert "7 unresolved ties" in _appendix(render_engineering_report_md(report))


def test_a_group_without_open_ties_counts_none() -> None:
    report = _report(_clones(3))
    assert _groups(report)[0]["open_tie_count"] == 0
    assert "unresolved tie" not in _appendix(render_engineering_report_md(report))


def test_a_different_runner_up_basis_is_its_own_entry() -> None:
    """The appendix prints "Compared on the co-dependency group's mean scores"
    from the runner-up's basis, so the basis is part of what must read the same."""
    digest = _clones(2)
    for trace in digest["queries"]:
        trace["runner_up"] = {**(trace["runner_up"] or {}), "score": 70.0, "basis": "score"}
    digest["queries"][1]["runner_up"]["basis"] = "group_mean"
    report = _report(digest)
    assert [g["query_count"] for g in _groups(report)] == [1, 1]


def test_a_short_id_is_unique_among_every_query_of_the_report() -> None:
    """An 8-character prefix shared with a query beyond the cap or one that is
    not listed at all would name two queries in the JSON and the UI."""
    digest = _clones(3)
    ids = [t["query_id"] for t in digest["queries"]]
    digest["more_listed"] = [
        {"query_id": ids[0][:8] + "-more", "moved": True, "tied": False, "open_tie": False}
    ]
    report = _report(digest)
    report["query_groups"] = [{"access_patterns": [{"query_ids": [ids[1][:8] + "-unlisted"]}]}]
    report["facts"] = compute_report_facts(report)
    short = dict(zip(_groups(report)[0]["query_ids"], _groups(report)[0]["short_ids"], strict=True))
    assert short[ids[0]] == ids[0]
    assert short[ids[1]] == ids[1]
    assert short[ids[2]] == ids[2][:8]


def _group(**overrides: Any) -> dict[str, Any]:
    return {
        "representative_id": "q1",
        "query_ids": ["q1", "q2"],
        "short_ids": ["q1", "q2"],
        "query_count": 2,
        "open_tie_count": 1,
        **overrides,
    }


def test_a_valid_group_passes_the_contract() -> None:
    assert QueryTraceGroup.model_validate(_group()).query_count == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {"query_count": 0, "query_ids": [], "short_ids": [], "representative_id": "q1"},
        {"query_count": 3},
        {"short_ids": ["q1"]},
        {"open_tie_count": 3},
        {"open_tie_count": -1},
        {"representative_id": "q9"},
    ],
    ids=["empty", "count", "short-ids", "open-ties-above-size", "negative-open-ties", "rep"],
)
def test_the_contract_rejects_an_inconsistent_group(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        QueryTraceGroup.model_validate(_group(**overrides))
