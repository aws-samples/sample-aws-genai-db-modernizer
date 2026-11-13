# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The engineering report's "Adoption paths", the waves' stages and "One step" (#1131).

The facts come from the same hand-built job the slice plan's own tests use
(``slice_plan_jobs``: step 1 carries every query to Aurora MySQL, step 2 fronts two
reads with ElastiCache, step 3 moves DocumentDB's two queries, step 4 DynamoDB's eight),
priced by ``FakePricer`` so every path states its own sized estimate
(``cost_basis="per_path_sizing"``, #2214). What the renderer must do:

- state each offered path's engines, steps, queries, CDC copies, cost and first gate,
  and show only the paths the capped view names (``summary_path_ids``, #2172);
- print no difference from the relational move only when there is no relational step
  (``relational_monthly_cost_usd`` is None), and print no path's moving-table count at
  all while the headline ``facts.tables_migrate`` disagrees with it (#2166);
- give each wave its stages, with the pilot named and long lists capped, and wave 1 the
  relational step's table classes;
- state a gate once: a path's first gate is step 1's own gate, and two stages of
  different steps can carry the same filled gate;
- print an earlier path's cost only when each path was priced for itself
  (``cost_basis="per_path_sizing"``, ADR-033 Amendment 2); the full target's cost always;
- define step, stage, unit and pilot once, and say which steps each wave holds;
- share one said-once ledger with the roadmap, so step 1's gate is stated once;
- leave a report that states no path exactly as it was before this section existed
  (``test_adoption_paths_old_report.py`` compares it byte for byte).
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from src.contracts.synthesis_output import AdoptionFacts, SlicePlanFacts
from src.report import escaping
from src.report.adoption import (
    DEFINITIONS,
    ONE_STEP_DOES_NOT_SUIT,
    ONE_STEP_SUITS,
    adoption_paths_shown,
)
from src.report.adoption_paths_md import adoption_md, wave_stages_md
from src.report.formatting import _SaidOnce
from src.report.renderers import render_engineering_report_md
from src.shared.adoption_facts import adoption_facts
from src.shared.report_facts import compute_report_facts
from src.shared.slice_plan import build_slice_plan
from tests.unit.agents.referee import slice_plan_jobs as job
from tests.unit.shared.test_adoption_path_sizing import FakePricer

POINTER = "the target architecture in the Target engines table above"
PRE_2170 = Path(__file__).parent / "fixtures" / "wordpress_pre_2170_report.json"
# A figure no real report holds, written into every place the moving-table count lives.
SENTINEL = 9137


def _plan(**overrides: Any) -> dict[str, Any]:
    waves = overrides.pop("waves", None) or job.waves()
    plan = build_slice_plan(
        job.REPORT, waves, job.inputs(**overrides), job.COSTS, {"dynamodb": 1}, []
    )
    assert plan is not None
    return plan


def _facts(
    plan: dict[str, Any] | None = None,
    gated: dict[str, tuple[str, ...]] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """A facts block holding the job's slice plan and its priced adoption paths."""
    plan = plan if plan is not None else _plan(**overrides)
    inputs = job.inputs(relational_gate_failures=gated or {}, **overrides)
    adoption = adoption_facts(job.REPORT, plan, inputs, price=FakePricer())
    # Both blocks are exactly what the contract validates, so the renderer never formats
    # a shape synthesis cannot produce.
    assert AdoptionFacts.model_validate(adoption).model_dump(mode="json") == adoption
    assert SlicePlanFacts.model_validate(plan).model_dump(mode="json") == plan
    return {"slice_plan": plan, "adoption": adoption}


def _section(facts: dict[str, Any]) -> str:
    return "\n".join(adoption_md(facts, POINTER, _SaidOnce()))


def _block(section: str, heading: str) -> str:
    body = section.split(f"### {heading}\n", 1)
    assert len(body) == 2, f"no block for {heading!r} in\n{section}"
    return body[1].split("\n### ", 1)[0]


def _header(section: str) -> list[str]:
    line = next(line for line in section.splitlines() if line.startswith("| Path |"))
    return [c.strip() for c in line.strip().strip("|").split("|")]


def test_a_path_with_no_purpose_built_engine_states_its_engines_steps_and_queries() -> None:
    """The relational move only: Aurora and the cache, nothing moved, no CDC copy, and
    no difference from itself."""
    block = _block(_section(_facts()), "Relational move only")
    assert "- Engines: Aurora MySQL, ElastiCache" in block
    assert "- Steps 1 to 2, the last on ElastiCache in wave 2; 2 stages, 2 gates" in block
    assert "- Queries: 13 on Aurora MySQL" in block
    assert "- Monthly cost: $200.00" in block
    assert "read through a CDC copy" not in block
    assert "than the relational move only" not in block
    assert "- First gate: Full load complete and replication in progress" in block


def test_the_full_target_block_states_its_engines_copies_cost_and_difference() -> None:
    section = _section(_facts())
    block = _block(section, "Full target, the default")
    assert "- Engines: Aurora MySQL, ElastiCache, DocumentDB, DynamoDB" in block
    assert "- Steps 1 to 4, the last on DynamoDB in wave 3; 6 stages, 6 gates" in block
    assert "- Queries: 3 on Aurora MySQL, 2 on DocumentDB, 8 on DynamoDB" in block
    assert (
        "- 3 tables read through a CDC copy, in one direction or the other: `app.carts`, "
        "`app.events`, `app.products`" in block
    )
    assert "- 2 CDC copies back to an earlier engine, for reads that stay there" in block
    assert "- Monthly cost: $200.00, the same as the relational move only" in block
    # Step 1's gate is stated under the first path; the full target names that block,
    # never the section it is printed in.
    assert '- First gate: Stated under "Relational move only" above.' in block
    assert 'Stated in "Adoption paths"' not in section


def test_the_heading_and_the_table_name_every_offered_path_once() -> None:
    section = _section(_facts())
    assert section.startswith("## Adoption paths (3)")
    assert _header(section) == [
        "Path",
        "Queries off Aurora MySQL",
        "Monthly cost",
        "Against the relational move only",
        "Open critical and high risks",
    ]
    rows = [
        line for line in section.splitlines() if re.match(r"^\| (Relational|Through|Full)", line)
    ]
    assert [r.split("|")[1].strip() for r in rows] == [
        "Relational move only",
        "Through step 3",
        "Full target, the default",
    ]
    assert rows[0] == "| Relational move only | 0 | $200.00 | - | 0 |"
    assert rows[1] == "| Through step 3 | 2 | $220.00 | $20.00 more | 0 |"
    assert rows[2] == "| Full target, the default | 10 | $200.00 | the same | 1 |"


def test_no_path_states_the_tables_it_moves_while_the_headline_figure_disagrees() -> None:
    """#2166, the prerequisite recorded on #1131: ``facts.tables_migrate`` and
    ``paths[].tables_migrate`` count the same end state's moving tables differently, so
    the section prints neither a column nor a line for them."""
    facts = _facts()
    full = next(p for p in facts["adoption"]["paths"] if p["path_id"] == "full_target")
    assert full["tables_migrate"] == 5
    section = _section(facts)
    assert "Tables migrate" not in section
    assert "tables migrate" not in section
    assert not re.search(r"\b5 tables? (?:move|migrate)", section)


def _with_sentinel_moving_tables(report: dict[str, Any]) -> dict[str, Any]:
    """``report`` with every place the paths' moving-table count lives set to SENTINEL:
    each path's ``tables_migrate``, and the relational step's ``leaves`` (the tables a
    later step owns, the same count, #2237)."""
    out = copy.deepcopy(report)
    facts = out["facts"]
    for path in facts["adoption"]["paths"]:
        path["tables_migrate"] = SENTINEL
    for step in facts["slice_plan"]["plan_steps"]:
        classes = step.get("table_classes")
        if classes:
            classes["leaves_count"] = SENTINEL
            classes["leaves"] = [{"table": f"t{i}", "stage_label": "3c"} for i in range(3)]
    return out


def test_the_whole_report_states_no_path_s_moving_table_count() -> None:
    """Review of #2241 (Critical 1): the guard covers the whole report, not just
    "Adoption paths". Wave 1's table classes printed ``leaves_count``, the same number as
    ``full_target.tables_migrate``, which disagrees with the headline "tables migrate"
    until #2237 lands (discourse: 66 against 188)."""
    report = _with_sentinel_moving_tables(_renderable(facts=True))
    assert report["facts"]["tables_migrate"] != SENTINEL
    md = render_engineering_report_md(report)
    assert "## Adoption paths" in md and "- Table classes:" in md
    assert str(SENTINEL) not in md
    assert f"{SENTINEL:,}" not in md
    assert "owned by a later step" not in md


def test_with_no_relational_step_no_difference_from_the_relational_move_is_printed() -> None:
    """#2214: with no relational step ``relational_monthly_cost_usd`` is None, while
    ``cost_delta_vs_relational_usd`` still holds a number for contract compatibility."""
    queries = tuple(q for q in job.inputs().queries if q.query_id in {"q10", "q12", "q13"})
    plan = _plan(
        waves=[
            {"wave": 1, "engines": ["dynamodb"], "tables": ["app.sessions"]},
            {"wave": 2, "engines": ["documentdb"], "tables": ["app.profiles"]},
        ],
        queries=queries,
        writes_by_table={"app.sessions": ("q13", "q12"), "app.profiles": ("q10",)},
        co_dependency_groups=(),
        foreign_keys=(),
    )
    adoption = adoption_facts(job.REPORT, plan, job.inputs(), price=FakePricer())
    assert adoption["relational_monthly_cost_usd"] is None
    assert adoption["paths"][0]["cost_delta_vs_relational_usd"] != 0
    section = _section({"slice_plan": plan, "adoption": adoption})
    assert "Against the relational move only" not in _header(section)
    # With no relational engine there is nothing to count queries off.
    assert _header(section)[1] == "Queries moved"
    assert "than the relational move only" not in section
    assert (
        "*Relational move only is not offered: no step of this plan keeps the whole "
        "database on one relational engine. Full target is offered in its place.*" in section
    ), section


def test_a_path_the_relational_gates_refuse_says_why_and_names_its_closure() -> None:
    section = _section(_facts(gated={"aurora_mysql": ("q03", "q04")}))
    for label in ("Relational move only", "Through step 3"):
        assert (
            f"*{label} is not offered: it would leave 2 queries on the relational engine "
            "that the assignment review moved off it \\(DynamoDB serves them\\). Full "
            "target is offered in its place.*" in section
        ), section
    # Neither gets a row or a block of its own.
    assert "### Relational move only" not in section
    assert "| Relational move only |" not in section


def test_the_capped_view_shows_only_the_paths_summary_path_ids_names() -> None:
    """#2172 picks the paths a view of at most five rows shows; the renderer chooses
    none of them, and never prints a path the capped view leaves out."""
    facts = _facts()
    adoption = facts["adoption"]
    capped = {
        **adoption,
        "summary_path_ids": ["relational_only", "full_target"],
    }
    section = _section({**facts, "adoption": capped})
    assert "The table and the blocks below show 2 of the 3 paths" in section
    assert "Through step 3" not in section
    assert "Relational move only" in section and "Full target, the default" in section


def test_the_one_step_block_states_the_plan_s_counts_and_its_checks() -> None:
    section = _section(_facts())
    block = section.split("### One step\n", 1)[1]
    one = _facts()["adoption"]["one_step"]
    assert (one["staged_step_count"], one["staged_stage_count"], one["gate_count"]) == (4, 6, 6)
    assert one["staged_temporary_stream_count"] == 2
    assert "- The same end state in one step, in place of 4 steps and 6 stages." in block
    assert "- Its one gate holds all 6 stage gates." in block
    assert (
        "- It needs none of the 2 CDC copies back to an earlier engine that the staged "
        "form carries." in block
    )
    assert ONE_STEP_DOES_NOT_SUIT in block and ONE_STEP_SUITS not in block
    assert "| No open critical or high risk | 1 | 0 | no |" in block
    assert "| At most one purpose-built target | 2 | 1 | no |" in block
    assert f"To take the full target in one step, adopt {POINTER} directly." in block
    assert "no_open_critical_high_risk" not in block


def test_the_one_step_sentences_are_the_ones_the_executive_page_shares() -> None:
    """#2240 imports these two sentences, so the engineering and executive reports say
    the same thing about the one-step form."""
    assert ONE_STEP_SUITS == (
        "These checks describe the shape of this workload. Every check holds, so it suits a "
        "one-step move."
    )
    assert ONE_STEP_DOES_NOT_SUIT == (
        "These checks describe the shape of this workload. Not every check holds, so it does "
        "not suit a one-step move."
    )
    facts = _facts()
    one = facts["adoption"]["one_step"]
    passing = {
        **one,
        "suits_one_step": True,
        "one_step_checks": [{**c, "observed": 0, "passed": True} for c in one["one_step_checks"]],
    }
    block = _section({**facts, "adoption": {**facts["adoption"], "one_step": passing}})
    assert ONE_STEP_SUITS in block and ONE_STEP_DOES_NOT_SUIT not in block


def test_the_section_defines_step_stage_unit_and_pilot_once() -> None:
    md = render_engineering_report_md(_renderable(facts=True))
    assert md.count(DEFINITIONS) == 1
    section = md.split("## Adoption paths (", 1)[1].split("\n## ", 1)[0]
    assert DEFINITIONS in section.split("\n|", 1)[0], "the definitions come before the table"
    for word in ("step", "stage", "unit", "pilot"):
        assert re.search(rf"\b{word}\b", DEFINITIONS), word


def _rows(lines: list[str]) -> list[list[str]]:
    """The cells of every table row in ``lines`` (header and separator rows dropped)."""
    out = []
    for line in lines:
        if line.startswith("|") and not re.match(r"^\|[-| ]+\|$", line):
            cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
            if cells[0] != "Stage":
                out.append(cells)
    return out


def test_a_wave_gains_its_stages_with_the_pilot_named() -> None:
    facts = _facts()
    lines = wave_stages_md(facts, 3, _SaidOnce())
    # Which steps the wave holds, then its stages as rows, each with its gate.
    assert lines[0] == "- Steps 3 to 4 (4 stages):"
    assert "| Stage | What it moves | Units | Queries | Gate |" in lines
    rows = _rows(lines)
    stages = {r[0]: r for r in rows if len(r) == 5}
    assert list(stages) == ["3c", "4a", "4b", "4c"]
    assert stages["4a"][1:4] == ["reads on a CDC copy", "1", "1"]
    assert stages["4a"][4].startswith("CDC from Aurora MySQL to DynamoDB")
    assert stages["4c"][1:4] == ["ownership with a CDC copy back to an earlier engine", "2", "5"]
    # The units, each owning its tables and reading its copies, the pilot named.
    assert "| Stage | Unit | Queries | Owns | Reads a copy of | CDC copy back to |" in lines
    units = [r for r in rows if len(r) == 6]
    assert ["4a", "Pilot", "1", "-", "`app.products`", "-"] in units
    assert ["4b", "-", "2", "`app.sessions`", "-", "-"] in units
    assert ["4c", "-", "3", "`app.cart_items`, `app.carts`", "-", "Aurora MySQL (1 read)"] in units
    text = "\n".join(lines)
    assert not re.search(r"\d quer(?:y|ies), owns", text)
    # A unit is never shown by its key, which holds a raw engine id.
    assert "dynamodb:" not in text and "documentdb:" not in text


def test_wave_one_gains_the_relational_step_s_table_classes() -> None:
    lines = wave_stages_md(_facts(), 1, _SaidOnce())
    text = "\n".join(lines)
    assert text.startswith("- Step 1 (1 stage):")
    (stage,) = _rows(lines)
    assert stage[:4] == ["1", "the whole database at once", "1", "13"]
    assert "- Table classes:" in text
    # Each class agrees with its count's verb: "1 table stays", "1 table is".
    assert "  - 1 table stays on Aurora MySQL, read by no other engine" in text
    assert "  - 1 table stays on Aurora MySQL and is read as a CDC copy by another engine" in text
    assert "  - 1 table is read by no collected query" in text
    # The tables a later step owns are left out until #2237: that count disagrees with
    # the headline "tables migrate".
    assert "later step" not in text
    # The whole-database unit owns the schema the wave already lists: no unit table.
    assert "Units of each stage" not in text


def test_the_cache_wave_s_one_unit_adds_no_line_of_its_own() -> None:
    lines = wave_stages_md(_facts(), 2, _SaidOnce())
    assert [r[:4] for r in _rows(lines)] == [["2a", "the reads the cache fronts", "1", "2"]]
    assert "Units of each stage" not in "\n".join(lines)


def test_a_stage_shows_the_first_ten_units_then_how_many_more() -> None:
    facts = _facts()
    plan = copy.deepcopy(facts["slice_plan"])
    step = plan["plan_steps"][3]
    stage = step["step_stages"][0]
    units = {u["key"]: u for u in plan["move_units"]}
    sample = units[stage["unit_keys"][0]]
    for i in range(12):
        key = f"extra-{i}"
        plan["move_units"].append({**sample, "key": key, "pilot": False})
        stage["unit_keys"].append(key)
    rows = _rows(wave_stages_md({**facts, "slice_plan": plan}, 3, _SaidOnce()))
    of_stage = [r for r in rows if len(r) == 6 and r[0] == stage["label"]]
    assert len(of_stage) == 11
    assert of_stage[-1][:2] == [stage["label"], "+3 more units"]


def test_an_unknown_stage_kind_prints_no_empty_parentheses() -> None:
    facts = _facts()
    plan = copy.deepcopy(facts["slice_plan"])
    plan["plan_steps"][2]["step_stages"][0]["kind"] = "some_new_kind"
    lines = wave_stages_md({**facts, "slice_plan": plan}, 3, _SaidOnce())
    assert "()" not in "\n".join(lines)
    assert _rows(lines)[0][:4] == ["3c", "-", "1", "2"]


def test_a_gate_two_stages_share_is_stated_once() -> None:
    facts = _facts()
    plan = copy.deepcopy(facts["slice_plan"])
    first = plan["plan_steps"][2]["step_stages"][0]
    plan["plan_steps"][3]["step_stages"][0]["gate"] = first["gate"]
    lines = wave_stages_md({**facts, "slice_plan": plan}, 3, _SaidOnce())
    gates = [r[4] for r in _rows(lines) if len(r) == 5]
    assert gates.count(escaping.md_cell(first["gate"])) == 1
    assert f"Stated under Stage {first['label']}." in gates


def _renderable(facts: bool) -> dict[str, Any]:
    """A small report the engineering renderer accepts, with or without the facts the
    slice plan and the adoption paths need."""
    report: dict[str, Any] = {
        "database_name": "app",
        "job_id": "job-1",
        "timestamp": "2026-11-13T00:00:00Z",
        "engines_in_use": ["mysql"],
        "ranking": [
            {"target": "aurora_mysql", "confidence_score": 90, "assigned_queries": 3},
            {"target": "dynamodb", "confidence_score": 88, "assigned_queries": 8},
            {"target": "documentdb", "confidence_score": 80, "assigned_queries": 2},
            {
                "target": "elasticache",
                "confidence_score": 70,
                "assigned_queries": 0,
                "role": "cache_layer",
                "cache_overlay_queries": 2,
            },
        ],
        "recommended_architecture": {"databases": [{"service": "dynamodb", "table_count": 4}]},
        "schema_designs": {},
        "migration_waves": job.waves(),
        "tco_analysis": {
            "projected_monthly_cost": sum(job.COSTS.values()),
            "cost_breakdown": [
                {"database": engine, "monthly_cost_usd": cost} for engine, cost in job.COSTS.items()
            ],
        },
    }
    if facts:
        report["facts"] = compute_report_facts(
            report,
            waves=job.waves(),
            readiness=False,
            slice_inputs=job.inputs(),
            path_pricer=FakePricer(),
        )
    return report


def test_the_roadmap_shares_the_adoption_section_s_said_once_ledger() -> None:
    """Review of #2241 (5): with a ledger of its own, the roadmap prints step 1's gate in
    full a second time, under Stage 1 (discourse printed its 15-word gate twice)."""
    report = _renderable(facts=True)
    md = render_engineering_report_md(report)
    step1 = report["facts"]["slice_plan"]["plan_steps"][0]["step_stages"][0]["gate"]
    first_sentence = step1.split(". ", 1)[0]
    assert len(first_sentence.split()) >= 15, "long enough for the ledger to hold it"
    assert md.count(escaping.md_text(first_sentence)) == 1
    assert (
        '| 1 | the whole database at once | 1 | 13 | Stated under "Relational move only" '
        "above. The cutover is your responsibility. |" in md
    )


def test_the_report_puts_the_adoption_paths_before_the_roadmap_and_points_at_them() -> None:
    md = render_engineering_report_md(_renderable(facts=True))
    assert md.index("## Adoption paths (") < md.index("## Migration roadmap (")
    assert (
        "*This is one suggested adoption path. To modernize in one step instead, adopt "
        'the full target in "Adoption paths" above directly.*' in md
    )
    assert "### One step" in md
    assert "- Steps 3 to 4 (4 stages):" in md


def test_a_report_without_the_adoption_facts_renders_as_before() -> None:
    """ADR-032 Decision 4: a report written before the paths, or one rebuilt without the
    assignment, states the empty plan; the section is left out and the roadmap keeps the
    pointer it had."""
    report = _renderable(facts=False)
    report["facts"] = compute_report_facts(report, waves=job.waves(), readiness=False)
    assert report["facts"]["adoption"]["paths"] == []
    assert not adoption_paths_shown(report["facts"])
    md = render_engineering_report_md(report)
    assert "## Adoption paths" not in md
    assert "- Step" not in md and "### One step" not in md
    assert f"adopt {POINTER} directly" in md
    # And with no facts block at all, the same.
    bare = _renderable(facts=False)
    assert "## Adoption paths" not in render_engineering_report_md(bare)


@pytest.mark.parametrize("key", ["adoption", "slice_plan"])
def test_a_facts_block_missing_one_of_the_two_renders(key: str) -> None:
    facts = {k: v for k, v in _facts().items() if k != key}
    assert adoption_md(facts, POINTER, _SaidOnce()) or key == "adoption"
    assert wave_stages_md(facts, 3, _SaidOnce()) or key == "slice_plan"


def test_a_report_priced_before_per_path_sizing_states_only_the_full_target_s_cost() -> None:
    """Review of #2241 (Critical 2): ADR-033 Amendment 2 and the 2026-11-12 decision on
    #2170 let a deliverable print an earlier path's cost only with ``per_path_sizing``. A
    report written before it states each path's cost from the full target's estimates
    ($681.84 for the relational move only is Aurora's full-target estimate plus the
    cache's), so only the full target's cost, the headline, is printed."""
    report = json.loads(PRE_2170.read_text(encoding="utf-8"))
    adoption = report["facts"]["adoption"]
    assert adoption["cost_basis"] == "full_target_estimates"
    costs = {p["path_id"]: p["monthly_cost_usd"] for p in adoption["paths"]}
    assert costs == {"relational_only": 681.84, "full_target": 757.62}
    md = render_engineering_report_md(report)
    section = md.split("## Adoption paths (", 1)[1].split("\n## ", 1)[0]
    assert "$681.84" not in md
    assert "| Relational move only | 0 | - | 1 |" in section
    assert "| Full target, the default | 39 | $757.62 | 1 |" in section
    assert "Monthly cost" not in _block(section, "Relational move only")
    assert "- Monthly cost: $757.62" in _block(section, "Full target, the default")
    assert "Against the relational move only" not in section
    # The basis that note states is not the one the section prints by.
    assert adoption["cost_basis_note"] not in section
    assert "Only the full target's cost is stated" in section
