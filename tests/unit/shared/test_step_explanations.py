# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Each step's "why" sentence and the order metrics' plain names (#1699).

The sentences are fixed templates filled with the plan's own figures; the templates are
guarded as customer text: no graph terms, no internal terms, no cutover or decommission
advice and no ranking word. A report stored before #1699 gets the same sentences at read
time, from the same function.
"""

from __future__ import annotations

import copy
import json
import re
import string
from pathlib import Path
from typing import Any

import pytest

from src.contracts.synthesis_output import CutCost, SliceMetrics, SlicePlanFacts, UnitMetrics
from src.report.facts import report_facts
from src.shared import step_explanations as why
from src.shared.adoption_facts import slice_plan_facts
from src.shared.slice_plan import build_slice_plan
from src.tools.validation.customer_text_guard import scan_text
from tests.unit.agents.referee import slice_plan_jobs as job
from tests.unit.docs.test_no_cutover_advice import _findings

PRE_1699 = (
    Path(__file__).resolve().parents[1] / "report" / "fixtures" / "wordpress_pre_2170_report.json"
)

#: Words of the dependency graph and its metrics: the order's machinery, not the reader's.
GRAPH_TERMS = (
    "graph",
    "node",
    "edge",
    "community",
    "communities",
    "louvain",
    "betweenness",
    "centrality",
    "hub",
    "gravity",
    "blast radius",
    "cut cost",
    "quotient",
    "slice",
    "weight",
)
#: The plan's internal names: its field values and vocabulary.
INTERNAL_TERMS = (
    "unit",
    "kept whole",
    "kept_whole",
    "standalone",
    "engine_wave",
    "relational_step",
    "step_basis",
    "risk_held",
    "replica_reads",
    "with_reverse_replica",
    "self_contained",
)
#: Ranking words: a step's place is stated by its figures, never judged.
RANKING_TERMS = (
    "best",
    "better",
    "worse",
    "worst",
    "top",
    "rank",
    "ranked",
    "ranking",
    "score",
    "winner",
    "recommend",
    "recommended",
    "prefer",
    "preferred",
    "highest",
    "lowest",
    "most",
    "least",
    "ideal",
    "optimal",
    "safest",
    "riskiest",
)
DECOMMISSION = re.compile(
    r"\b(?:decommission\w*|retir\w*|switch(?:es|ed|ing)?\s+off|turn(?:s|ed|ing)?\s+off|"
    r"shut\s*down\w*|cut\s*over|cutover|go[\s-]live)\b",
    re.I,
)


def _plan(**kwargs: Any) -> dict[str, Any]:
    waves = kwargs.pop("waves", None) or job.waves()
    risks = kwargs.pop("risks", [])
    plan = build_slice_plan(job.REPORT, waves, job.inputs(**kwargs), job.COSTS, {}, risks)
    assert plan is not None
    return plan


def _whys(plan: dict[str, Any]) -> dict[int, str]:
    return {s["step_number"]: s["why"] for s in plan["plan_steps"]}


def _words(text: str, terms: tuple[str, ...]) -> list[str]:
    return [t for t in terms if re.search(rf"\b{re.escape(t)}\b", text, re.I)]


def customer_text_findings(text: str) -> list[str]:
    """Every reason ``text`` may not be printed to a customer (#1699)."""
    found = [f"graph term: {t}" for t in _words(text, GRAPH_TERMS)]
    found += [f"internal term: {t}" for t in _words(text, INTERNAL_TERMS)]
    found += [f"ranking: {t}" for t in _words(text, RANKING_TERMS)]
    found += [f"decommission or cutover: {m.group(0)}" for m in DECOMMISSION.finditer(text)]
    found += _findings("why", text, deliverable=True)
    found += [f"{f.kind}: {f.excerpt}" for f in scan_text(text)]
    return found


def test_the_plan_states_why_each_step_sits_where_it_does() -> None:
    plan = _plan()
    SlicePlanFacts.model_validate(plan)
    assert _whys(plan) == {
        1: (
            "Step 1 comes first: it moves the whole database to Aurora MySQL, 13 queries "
            "and 8 tables, and 2 of the 3 later steps build on the tables it carries."
        ),
        2: (
            "Step 2 puts ElastiCache in front of 2 reads, 28.6% of calls, and moves no "
            "data; it caches 3 tables that step 1 carries on Aurora MySQL."
        ),
        3: (
            "Step 3 moves 2 queries and 1 table to DocumentDB, 4.1% of calls, placed in "
            "wave 3 by those calls against its 1 table copied across and 1 write statement "
            "that changes engine; it shares 1 table with step 4."
        ),
        4: (
            "Step 4 moves 8 queries and 4 tables to DynamoDB, 67.3% of calls, placed in "
            "wave 3 by those calls against its 3 tables copied across and 5 write "
            "statements that change engine; as it is shared by many parts, it runs after "
            "every step in wave 3 that is not; it shares 1 table with step 3."
        ),
    }


def test_every_number_in_a_sentence_is_a_figure_of_the_plan() -> None:
    plan = _plan()
    for step in plan["plan_steps"]:
        figures = {
            step["step_number"],
            step["query_count"],
            step["owned_table_count"],
            step["call_share_percent"],
            step["phase_wave"],
        }
        if step["metrics"]:
            cut = step["metrics"]["cut_cost"]
            figures |= {cut["replica_table_count"], cut["write_site_count"]}
        numbers = {float(n) for n in re.findall(r"\d+(?:\.\d+)?", step["why"])}
        # The rest are counts of the plan's own lists: steps, tables on its dependencies.
        rest = numbers - {float(f) for f in figures}
        assert all(n == int(n) and n <= len(plan["plan_steps"]) + 3 for n in rest), (
            step["step_number"],
            rest,
        )
    slice_step = plan["plan_steps"][3]
    cut = slice_step["metrics"]["cut_cost"]
    assert f"{cut['replica_table_count']} tables copied across" in slice_step["why"]
    assert f"{cut['write_site_count']} write statements" in slice_step["why"]


def test_a_risk_that_holds_a_stage_is_named_with_the_queries_it_holds() -> None:
    risks = [{"risk_id": "RISK-007", "engine": "dynamodb", "query_ids": ["q12"]}]
    plan = _plan(risks=risks)
    held = _whys(plan)[4]
    assert held.endswith(", and RISK-007 holds back 2 queries until it is closed.")
    # No risk, no clause.
    assert "holds back" not in _whys(_plan())[4]


def test_another_source_s_step_follows_its_wave_and_shares_nothing() -> None:
    one_engine = job.waves(homogeneity="cross_engine")
    one_engine[2]["engines"] = ["dynamodb"]
    plan = _plan(
        waves=one_engine,
        queries=tuple(q for q in job.inputs().queries if q.engine != "documentdb"),
    )
    step = plan["plan_steps"][2]
    assert step["kind"] == "engine_wave" and step["metrics"] is None
    assert step["why"] == (
        "Step 3 moves 8 queries and 4 tables to DynamoDB, 70.3% of calls, as wave 3 of the "
        "roadmap, with its 2 tables copied across and 5 write statements that change "
        "engine; no step but step 1 shares its tables."
    )
    assert plan["plan_steps"][0]["why"].endswith(
        "and all 2 later steps build on the tables it carries."
    )


def test_a_plan_with_one_step_says_no_step_follows() -> None:
    waves = job.waves()[:1]
    queries = tuple(q for q in job.inputs().queries if q.engine == "aurora_mysql")
    plan = build_slice_plan(
        job.REPORT, waves, job.inputs(queries=queries), {"aurora_mysql": 100.0}, {}, []
    )
    assert plan is not None
    assert plan["plan_steps"][0]["why"].endswith(", and no later step follows it.")


@pytest.mark.parametrize("template", why.TEMPLATES)
def test_no_template_carries_a_graph_term_an_internal_term_cutover_advice_or_a_rank(
    template: str,
) -> None:
    text = string.Formatter().vformat(
        template, (), {f: "X" for _, f, _, _ in string.Formatter().parse(template) if f}
    )
    assert customer_text_findings(text) == []


def test_every_filled_sentence_passes_the_customer_text_guard() -> None:
    risks = [{"risk_id": "RISK-007", "engine": "dynamodb", "query_ids": ["q12"]}]
    for plan in (_plan(), _plan(risks=risks)):
        for step, sentence in _whys(plan).items():
            assert customer_text_findings(sentence) == [], (step, sentence)


def test_the_guard_catches_each_class_of_word() -> None:
    """The guard is not vacuous: one sentence per class it refuses."""
    for bad in (
        "Its hub score is high.",
        "Each move unit runs on its own.",
        "This is the best step to take.",
        "Decommission the source after this step.",
        "Step 3 is ready to cut over.",
        "It reads dynamodb tables.",
    ):
        assert customer_text_findings(bad), bad


def test_every_order_metric_has_one_plain_name() -> None:
    fields = {*UnitMetrics.model_fields, *SliceMetrics.model_fields, *CutCost.model_fields}
    assert set(why.METRIC_LABELS) == fields
    labels = list(why.METRIC_LABELS.values())
    assert len(set(labels)) == len(labels)
    for label in labels:
        assert customer_text_findings(label) == [], label
    # The sentences use the labels' own words.
    sentence = _whys(_plan())[4]
    assert why.METRIC_LABELS["replica_table_count"] in sentence
    assert why.METRIC_LABELS["write_site_count"] in sentence
    assert why.METRIC_LABELS["hub_score"] in sentence


def test_the_plan_carries_the_metric_labels_even_when_empty() -> None:
    assert _plan()["metric_labels"] == why.METRIC_LABELS
    report = {**job.REPORT, "migration_waves": job.waves()}
    empty = slice_plan_facts(report)
    assert empty["plan_steps"] == [] and empty["metric_labels"] == why.METRIC_LABELS


def test_a_report_saved_before_the_sentences_gets_them_at_read_time() -> None:
    report = json.loads(PRE_1699.read_text(encoding="utf-8"))
    stored = report["facts"]["slice_plan"]
    assert stored["plan_steps"] and "metric_labels" not in stored
    assert all("why" not in s for s in stored["plan_steps"])
    plan = report_facts(report)["slice_plan"]
    SlicePlanFacts.model_validate(plan)
    assert plan == why.with_step_explanations(stored)
    assert plan["metric_labels"] == why.METRIC_LABELS
    assert [s["why"] for s in plan["plan_steps"]] == [
        why.step_why(stored, s) for s in stored["plan_steps"]
    ]
    # Every stored figure is kept as it was.
    for old, new in zip(stored["plan_steps"], plan["plan_steps"], strict=True):
        assert {k: v for k, v in new.items() if k != "why"} == old


def test_a_plan_re_dumped_through_the_contract_is_filled_too() -> None:
    report = json.loads(PRE_1699.read_text(encoding="utf-8"))
    dumped = SlicePlanFacts.model_validate(report["facts"]["slice_plan"]).model_dump(mode="json")
    assert all(s["why"] is None for s in dumped["plan_steps"]) and dumped["metric_labels"] == {}
    assert why.needs_step_explanations(dumped)
    report["facts"]["slice_plan"] = dumped
    plan = report_facts(report)["slice_plan"]
    assert all(s["why"] for s in plan["plan_steps"])
    assert not why.needs_step_explanations(plan)


def test_a_stored_plan_with_no_steps_is_read_as_stored() -> None:
    assert not why.needs_step_explanations({"step_basis": "engine_waves", "plan_steps": []})


def test_a_current_plan_is_read_back_unchanged() -> None:
    report = json.loads(PRE_1699.read_text(encoding="utf-8"))
    filled = why.with_step_explanations(report["facts"]["slice_plan"])
    # A sentence edited by hand stays as stored: the reader fills, never rewrites.
    filled["plan_steps"][0]["why"] = "Stored sentence."
    report["facts"]["slice_plan"] = copy.deepcopy(filled)
    assert report_facts(report)["slice_plan"] == filled
