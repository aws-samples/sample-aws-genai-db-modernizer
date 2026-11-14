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
from pydantic import ValidationError

from src.contracts.synthesis_output import (
    CutCost,
    SliceMetrics,
    SlicePlanFacts,
    SliceStep,
    UnitMetrics,
)
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
        # The wave is the roadmap's; the share of calls against the copies and writes
        # only orders the wave's slices (slice_order_key).
        3: (
            "Step 3 moves 2 queries and 1 table to DocumentDB, 4.1% of calls, in wave 3 of "
            "the roadmap, ordered in that wave by those calls against its 1 table copied "
            "across and 1 write statement that changes engine; it shares 1 table with step 4."
        ),
        4: (
            "Step 4 moves 8 queries and 4 tables to DynamoDB, 67.3% of calls, in wave 3 of "
            "the roadmap, ordered in that wave by those calls against its 3 tables copied "
            "across and 5 write statements that change engine; it runs after step 3 of its "
            "wave, which is not shared by many parts; it shares 1 table with step 3."
        ),
    }


def _figures(step: dict[str, Any], plan: dict[str, Any]) -> list[float]:
    """Every figure a step's sentence may state: its own counts, its ties', the queries its
    risks hold back and the relational step's number."""
    ties = step["ties"]
    held = sum(st["query_count"] for st in step["step_stages"] if st["kind"] == "risk_held")
    values = [
        step["step_number"],
        step["query_count"],
        step["owned_table_count"],
        step["call_share_percent"],
        step["phase_wave"],
        ties["later_step_count"],
        ties["dependent_step_count"],
        ties["shared_table_count"],
        ties["fronted_step"],
        ties["fronted_table_count"],
        ties["replica_table_count"],
        ties["write_site_count"],
        *ties["shared_with_steps"],
        *ties["runs_after_steps"],
        held,
        *(s["step_number"] for s in plan["plan_steps"] if s["kind"] == "relational_step"),
    ]
    return [float(v) for v in values if v is not None]


def numbers_not_in_the_facts(step: dict[str, Any], plan: dict[str, Any]) -> list[float]:
    """The numbers ``step``'s sentence states that are none of its figures, exactly."""
    text = re.sub(r"\bRISK-\d+\b", "", step["why"]).replace(",", "")
    stated = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text)]
    return [n for n in stated if n not in _figures(step, plan)]


def test_every_number_in_a_sentence_is_a_figure_of_the_plan() -> None:
    risks = [{"risk_id": "RISK-007", "engine": "dynamodb", "query_ids": ["q12"]}]
    for plan in (_plan(), _plan(risks=risks)):
        for step in plan["plan_steps"]:
            assert numbers_not_in_the_facts(step, plan) == [], step["why"]
            assert step["ties"] == why.step_ties(plan)[step["step_number"]]
    slice_step = _plan()["plan_steps"][3]
    cut = slice_step["metrics"]["cut_cost"]
    assert (slice_step["ties"]["replica_table_count"], slice_step["ties"]["write_site_count"]) == (
        cut["replica_table_count"],
        cut["write_site_count"],
    )
    # A number that is no figure is caught.
    assert numbers_not_in_the_facts({**slice_step, "why": "Step 4 moves 99 queries."}, _plan()) == [
        99.0
    ]


def test_a_risk_that_holds_a_stage_is_named_with_the_queries_it_holds() -> None:
    risks = [{"risk_id": "RISK-007", "engine": "dynamodb", "query_ids": ["q12"]}]
    plan = _plan(risks=risks)
    held = _whys(plan)[4]
    assert held.endswith(", and RISK-007 holds back 2 queries until it is closed.")
    # No risk, no clause.
    assert " back " not in _whys(_plan())[4]


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
        "Step 3 moves 8 queries and 4 tables to DynamoDB, 70.3% of calls, in wave 3 of the "
        "roadmap, with its 2 tables copied across and 5 write statements that change "
        "engine; no step but step 1 shares its tables."
    )
    assert (step["ties"]["replica_table_count"], step["ties"]["write_site_count"]) == (2, 5)
    assert plan["plan_steps"][0]["why"].endswith(
        "and both later steps build on the tables it carries."
    )


def test_a_plan_with_one_step_says_no_step_follows() -> None:
    waves = job.waves()[:1]
    queries = tuple(q for q in job.inputs().queries if q.engine == "aurora_mysql")
    plan = build_slice_plan(
        job.REPORT, waves, job.inputs(queries=queries), {"aurora_mysql": 100.0}, {}, []
    )
    assert plan is not None
    assert plan["plan_steps"][0]["why"].endswith(", and no later step follows it.")


# --- Every branch, on hand-built steps ------------------------------------------------------


def _ties(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "later_step_count": 0,
        "dependent_steps": [],
        "dependent_step_count": 0,
        "shared_with_steps": [],
        "shared_tables": [],
        "shared_table_count": 0,
        "fronted_step": None,
        "fronted_tables": [],
        "fronted_table_count": 0,
        "replica_table_count": None,
        "write_site_count": None,
        "ordered_with_steps": [],
        "runs_after_steps": [],
    }
    base.update(fields)
    return base


def _step(kind: str, number: int = 2, risks: tuple[str, ...] = (), **ties: Any) -> dict[str, Any]:
    stages = [{"kind": "replica_reads", "query_count": 3, "holding_risk_ids": []}]
    if risks:
        stages.append({"kind": "risk_held", "query_count": 4, "holding_risk_ids": list(risks)})
    return {
        "step_number": number,
        "kind": kind,
        "engine": "aurora_mysql" if kind == "relational_step" else "dynamodb",
        "phase_wave": 3,
        "query_count": 7,
        "owned_table_count": 2,
        "call_share_percent": 10.0,
        "step_stages": stages,
        "ties": _ties(**ties),
    }


def _plan_of(*steps: dict[str, Any]) -> dict[str, Any]:
    return {"plan_steps": list(steps)}


FIRST = _step("relational_step", 1)


@pytest.mark.parametrize(
    ("step", "ending"),
    [
        (_step("relational_step", 1, later_step_count=0), "and no later step follows it."),
        (
            _step("relational_step", 1, later_step_count=3),
            "and no later step builds on the tables it carries.",
        ),
        (
            _step(
                "relational_step",
                1,
                later_step_count=1,
                dependent_steps=[2],
                dependent_step_count=1,
            ),
            "and the later step builds on the tables it carries.",
        ),
        (
            _step(
                "relational_step",
                1,
                later_step_count=2,
                dependent_steps=[2, 3],
                dependent_step_count=2,
            ),
            "and both later steps build on the tables it carries.",
        ),
        (
            _step(
                "relational_step",
                1,
                later_step_count=3,
                dependent_steps=[2, 3, 4],
                dependent_step_count=3,
            ),
            "and all 3 later steps build on the tables it carries.",
        ),
        (
            _step(
                "relational_step",
                1,
                later_step_count=3,
                dependent_steps=[3],
                dependent_step_count=1,
            ),
            "and 1 of the 3 later steps builds on the tables it carries.",
        ),
        (
            _step(
                "relational_step",
                1,
                later_step_count=3,
                dependent_steps=[2, 3],
                dependent_step_count=2,
            ),
            "and 2 of the 3 later steps build on the tables it carries.",
        ),
    ],
)
def test_the_relational_step_names_the_later_steps_that_build_on_it(
    step: dict[str, Any], ending: str
) -> None:
    sentence = why.step_why(_plan_of(step), step)
    assert sentence == (
        "Step 1 comes first: it moves the whole database to Aurora MySQL, 7 queries and 2 "
        f"tables, {ending}"
    )
    SliceStep.model_validate({**_contract_step(step), "why": sentence})


def _contract_step(step: dict[str, Any]) -> dict[str, Any]:
    """A hand-built step the contract accepts, so each branch's ties are valid ties."""
    from src.contracts.synthesis_output import StepTies

    StepTies.model_validate(step["ties"])
    return {
        "step_number": 1,
        "kind": "relational_step",
        "engine": "aurora_mysql",
        "new_engine": True,
        "movement": "homogeneous",
        "step_stages": [
            {
                "label": "1",
                "kind": "whole_database",
                "unit_keys": ["aurora_mysql:whole-database"],
                "unit_count": 1,
                "query_count": 7,
                "gate": "g",
            }
        ],
        "stage_count": 1,
        "unit_count": 1,
        "query_count": 7,
        "ties": step["ties"],
    }


@pytest.mark.parametrize(
    ("ties", "ending"),
    [
        ({}, "and moves no data."),
        (
            {"fronted_step": 1, "fronted_tables": ["a", "b"], "fronted_table_count": 2},
            "and moves no data; it caches 2 tables that step 1 carries on Aurora MySQL.",
        ),
        (
            {"fronted_step": 1, "fronted_tables": ["a"], "fronted_table_count": 1},
            "and moves no data; it caches 1 table that step 1 carries on Aurora MySQL.",
        ),
    ],
)
def test_a_cache_step_names_what_it_fronts_only_when_it_fronts_one_step(
    ties: dict[str, Any], ending: str
) -> None:
    step = {**_step("cache"), "engine": "elasticache", "ties": _ties(**ties)}
    sentence = why.step_why(_plan_of(FIRST, step), step)
    assert sentence == f"Step 2 puts ElastiCache in front of 7 reads, 10% of calls, {ending}"


@pytest.mark.parametrize(
    ("plan_has_first", "risks", "ties", "tail"),
    [
        (
            True,
            (),
            {"replica_table_count": 1, "write_site_count": 1},
            ", with its 1 table copied across and 1 write statement that changes engine; "
            "no step but step 1 shares its tables.",
        ),
        (
            False,
            (),
            {"replica_table_count": 0, "write_site_count": 2},
            ", with its 0 tables copied across and 2 write statements that change engine; "
            "no other step shares its tables.",
        ),
        (
            True,
            (),
            {
                "replica_table_count": 3,
                "write_site_count": 5,
                "ordered_with_steps": [3, 4],
                "shared_with_steps": [3, 4, 5],
                "shared_tables": ["a", "b"],
                "shared_table_count": 2,
            },
            ", ordered in that wave by those calls against its 3 tables copied across and 5 "
            "write statements that change engine; it shares 2 tables with steps 3, 4 and 5.",
        ),
        (
            True,
            ("RISK-001",),
            {"replica_table_count": 3, "write_site_count": 5, "ordered_with_steps": [1]},
            ", ordered in that wave by those calls against its 3 tables copied across and 5 "
            "write statements that change engine; no step but step 1 shares its tables, and "
            "RISK-001 holds back 4 queries until it is closed.",
        ),
        (
            True,
            ("RISK-001", "RISK-004"),
            {"replica_table_count": 3, "write_site_count": 5},
            ", with its 3 tables copied across and 5 write statements that change engine; no "
            "step but step 1 shares its tables, and RISK-001 and RISK-004 hold back 4 queries "
            "until they are closed.",
        ),
        (
            True,
            (),
            {
                "replica_table_count": 3,
                "write_site_count": 5,
                "ordered_with_steps": [1, 3],
                "runs_after_steps": [1],
            },
            ", ordered in that wave by those calls against its 3 tables copied across and 5 "
            "write statements that change engine; it runs after step 1 of its wave, which is "
            "not shared by many parts; no step but step 1 shares its tables.",
        ),
        (
            True,
            (),
            {
                "replica_table_count": 3,
                "write_site_count": 5,
                "ordered_with_steps": [1, 3],
                "runs_after_steps": [1, 3],
            },
            ", ordered in that wave by those calls against its 3 tables copied across and 5 "
            "write statements that change engine; it runs after steps 1 and 3 of its wave, "
            "which are not shared by many parts; no step but step 1 shares its tables.",
        ),
        (
            True,
            (),
            {},
            "; no step but step 1 shares its tables.",
        ),
    ],
)
def test_a_moving_step_states_each_clause_in_its_own_number(
    plan_has_first: bool, risks: tuple[str, ...], ties: dict[str, Any], tail: str
) -> None:
    step = _step("slice", 2, risks, **ties)
    plan = _plan_of(FIRST, step) if plan_has_first else _plan_of(step)
    sentence = why.step_why(plan, step)
    assert sentence == (
        "Step 2 moves 7 queries and 2 tables to DynamoDB, 10% of calls, in wave 3 of the "
        f"roadmap{tail}"
    )
    assert customer_text_findings(sentence) == [], sentence


def test_a_slice_alone_in_its_wave_states_no_order_and_no_late_clause() -> None:
    # A slice shared by many parts with no other slice in its wave: nothing to order.
    plan = _plan(
        waves=job.waves(),
        queries=tuple(q for q in job.inputs().queries if q.engine != "documentdb"),
    )
    last = plan["plan_steps"][-1]
    assert last["ties"]["ordered_with_steps"] == [] and last["ties"]["runs_after_steps"] == []
    assert ", with its " in last["why"] and "ordered" not in last["why"]
    assert "shared by many parts" not in last["why"]


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


def test_metric_labels_name_only_order_metrics() -> None:
    plan = _plan()
    with pytest.raises(ValidationError, match="metric_labels names only order metrics"):
        SlicePlanFacts.model_validate({**plan, "metric_labels": {**why.METRIC_LABELS, "x": "y"}})


def test_a_plan_saved_before_a_metric_was_added_loads_and_gains_its_name() -> None:
    """A plan saved now names today's metrics; once a metric is added, its labels are a
    subset of the fields: it still loads, and the reader fills the new name."""
    report = json.loads(PRE_1699.read_text(encoding="utf-8"))
    filled = why.with_step_explanations(report["facts"]["slice_plan"])
    fewer = {k: v for k, v in filled["metric_labels"].items() if k != "transactions_split"}
    saved = {**filled, "metric_labels": fewer}
    SlicePlanFacts.model_validate(saved)
    assert why.needs_step_explanations(saved)
    report["facts"]["slice_plan"] = saved
    read = report_facts(report)["slice_plan"]
    assert read["metric_labels"] == why.METRIC_LABELS
    assert read["plan_steps"] == filled["plan_steps"]


def test_a_slice_step_s_ties_must_state_its_cut_cost() -> None:
    plan = copy.deepcopy(_plan())
    plan["plan_steps"][3]["ties"]["write_site_count"] += 1
    with pytest.raises(ValidationError, match="ties state its cut cost"):
        SlicePlanFacts.model_validate(plan)


def test_the_plan_carries_the_metric_labels_even_when_empty() -> None:
    assert _plan()["metric_labels"] == why.METRIC_LABELS
    report = {**job.REPORT, "migration_waves": job.waves()}
    empty = slice_plan_facts(report)
    assert empty["plan_steps"] == [] and empty["metric_labels"] == why.METRIC_LABELS


def test_a_report_saved_before_the_sentences_gets_them_at_read_time() -> None:
    report = json.loads(PRE_1699.read_text(encoding="utf-8"))
    stored = report["facts"]["slice_plan"]
    assert stored["plan_steps"] and "metric_labels" not in stored
    assert all("why" not in s and "ties" not in s for s in stored["plan_steps"])
    plan = report_facts(report)["slice_plan"]
    SlicePlanFacts.model_validate(plan)
    assert plan == why.with_step_explanations(stored)
    assert plan["metric_labels"] == why.METRIC_LABELS
    for step in plan["plan_steps"]:
        assert step["why"] and numbers_not_in_the_facts(step, plan) == [], step["why"]
    # Every stored figure is kept as it was.
    for old, new in zip(stored["plan_steps"], plan["plan_steps"], strict=True):
        assert {k: v for k, v in new.items() if k not in ("why", "ties")} == old


def test_a_plan_re_dumped_through_the_contract_is_filled_too() -> None:
    report = json.loads(PRE_1699.read_text(encoding="utf-8"))
    dumped = SlicePlanFacts.model_validate(report["facts"]["slice_plan"]).model_dump(mode="json")
    assert all(s["why"] is None for s in dumped["plan_steps"]) and dumped["metric_labels"] == {}
    assert why.needs_step_explanations(dumped)
    report["facts"]["slice_plan"] = dumped
    plan = report_facts(report)["slice_plan"]
    assert all(s["why"] for s in plan["plan_steps"])
    assert not why.needs_step_explanations(plan)


def test_a_plan_with_its_labels_but_a_step_with_no_why_is_filled() -> None:
    report = json.loads(PRE_1699.read_text(encoding="utf-8"))
    filled = why.with_step_explanations(report["facts"]["slice_plan"])
    assert not why.needs_step_explanations(filled)
    gap = copy.deepcopy(filled)
    gap["plan_steps"][1]["why"] = None
    assert why.needs_step_explanations(gap)
    report["facts"]["slice_plan"] = gap
    assert report_facts(report)["slice_plan"] == filled


def test_a_stored_plan_with_no_steps_gains_only_the_labels() -> None:
    stored = {"step_basis": "engine_waves", "plan_steps": [], "marker": True}
    assert why.needs_step_explanations(stored)
    assert why.with_step_explanations(stored) == {**stored, "metric_labels": why.METRIC_LABELS}


def test_a_current_plan_is_read_back_unchanged() -> None:
    report = json.loads(PRE_1699.read_text(encoding="utf-8"))
    filled = why.with_step_explanations(report["facts"]["slice_plan"])
    # A sentence edited by hand stays as stored: the reader fills, never rewrites.
    filled["plan_steps"][0]["why"] = "Stored sentence."
    report["facts"]["slice_plan"] = copy.deepcopy(filled)
    assert report_facts(report)["slice_plan"] == filled


def _slice(number: int, hub: float) -> dict[str, Any]:
    cut = {"replica_table_count": 1, "write_site_count": 1, "transactions_split": None}
    return {
        "step_number": number,
        "kind": "slice",
        "phase_wave": 3,
        "metrics": {"hub_score": hub, "cut_cost": cut},
        "step_stages": [],
    }


def test_a_slice_shared_by_many_parts_runs_after_only_the_wave_s_steps_that_are_not() -> None:
    plan = {"plan_steps": [_slice(3, 0.1), _slice(4, 0.8), _slice(5, 0.6), _slice(6, 0.2)]}
    ties = why.step_ties(plan)
    assert [ties[n]["ordered_with_steps"] for n in (3, 4)] == [[4, 5, 6], [3, 5, 6]]
    # Step 5 runs after step 3, not after step 4, which is shared by many parts too.
    assert {n: ties[n]["runs_after_steps"] for n in (3, 4, 5, 6)} == {3: [], 4: [3], 5: [3], 6: []}
    # The first slice of its wave runs after nothing, so it states no late clause.
    first = {"plan_steps": [_slice(3, 0.9), _slice(4, 0.1)]}
    assert why.step_ties(first)[3]["runs_after_steps"] == []
