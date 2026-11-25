# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The step figures' two 0-to-1 scores, readable (#2288).

"How tightly tied" (gravity) and "shared by many parts" (hub score) were printed as raw
values such as 0.37 and 0, which a reader cannot place. The facts give each a scale once
(``facts.slice_plan.score_scales``): a sentence on how it reads and, for shared by many
parts alone, where it turns high and the band words (the maintainer's decision on #2288:
only the score the plan acts on, at ``HUB_HEAVY``, has a band). Each step and move unit
gets its band (``score_bands``), computed by ``step_explanations``. The results page and
the engineering report print the band in the facts' words with the value after it,
"Low (0.37)", and how tightly tied as its value; neither compares a value with a
threshold. A plan stored before #2288 gains the scales and the bands at read time, from
the same function, and a plan saved with a band how tightly tied no longer has reads it
as a plain value.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.contracts.synthesis_output import SCORED_METRICS, SliceMetrics, SlicePlanFacts
from src.report.adoption_paths_md import wave_stages_md
from src.report.facts import report_facts
from src.report.formatting import _SaidOnce
from src.shared import step_explanations as why
from src.shared.dependency_graph import HUB_HEAVY
from src.shared.figure_format import fmt_num
from tests.unit.report.test_adoption_paths_rendering import _facts
from tests.unit.shared.test_step_explanations import PRE_1699, _plan, customer_text_findings

OLD_JOBS = Path(__file__).resolve().parents[2] / "fixtures" / "old_jobs"
SAMPLE_RUN = Path(__file__).resolve().parents[3] / "docs" / "examples" / "wordpress" / "sample-run"


def _records(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [*plan["plan_steps"], *plan["move_units"]]


# --- The scale, defined once --------------------------------------------------------------


def test_each_0_to_1_order_metric_has_one_scale() -> None:
    """The scored metrics are SliceMetrics' two fields bounded to 0 to 1, and each has a
    scale with its sentence; only shared by many parts has a band, turning high where a
    slice turns hub-heavy."""
    bounded = {
        name
        for name, field in SliceMetrics.model_fields.items()
        if any(getattr(m, "le", None) == 1 for m in field.metadata)
    }
    assert set(SCORED_METRICS) == bounded
    assert why.SCORE_HIGH_FROM == {"hub_score": HUB_HEAVY}
    scales = why.score_scales()
    assert set(scales) == bounded
    hub = scales["hub_score"]
    assert hub["high_from"] == HUB_HEAVY and hub["band_labels"] == {"low": "low", "high": "high"}
    threshold = fmt_num(HUB_HEAVY)
    assert f"below {threshold}" in hub["note"] and f"from {threshold}" in hub["note"]
    # How tightly tied: a sentence, no threshold and no band words.
    gravity = scales["gravity"]
    assert gravity["high_from"] is None and gravity["band_labels"] is None
    assert "0 to 1" in gravity["note"]
    assert not {"low", "high", "below"} & set(gravity["note"].lower().rstrip(".").split())
    for field, scale in scales.items():
        assert scale["note"].lower().startswith(why.METRIC_LABELS[field])


def test_how_tightly_tied_has_no_band() -> None:
    assert why.score_band("gravity", 0.99) is None
    assert why.score_bands({"gravity": 0.99, "hub_score": 0.6}) == {"hub_score": "high"}


@pytest.mark.parametrize("field", sorted(why.SCORE_HIGH_FROM))
def test_a_score_reads_high_from_its_threshold_and_low_below_it(field: str) -> None:
    high_from = why.SCORE_HIGH_FROM[field]
    assert why.score_band(field, high_from) == "high"
    assert why.score_band(field, 1) == "high"
    assert why.score_band(field, round(high_from - 0.01, 2)) == "low"
    assert why.score_band(field, 0) == "low"
    assert why.score_band(field, None) is None
    assert why.score_band("blast_radius", 3) is None


def test_every_scale_note_passes_the_customer_text_guard() -> None:
    for scale in why.score_scales().values():
        assert customer_text_findings(scale["note"]) == [], scale["note"]


# --- The bands, computed once ---------------------------------------------------------------


def test_each_step_and_unit_states_the_band_of_each_score_it_has() -> None:
    plan = _plan()
    assert plan["score_scales"] == why.score_scales()
    banded = 0
    for record in _records(plan):
        metrics = record.get("metrics") or {}
        expected = {
            f: why.score_band(f, metrics[f])
            for f in why.SCORE_HIGH_FROM
            if metrics.get(f) is not None
        }
        assert record["score_bands"] == expected, record.get("key", record.get("step_number"))
        assert "gravity" not in record["score_bands"]
        banded += len(expected)
    # The job has both bands, so the test is not vacuous.
    bands = {b for r in _records(plan) for b in r["score_bands"].values()}
    assert banded and bands == {"low", "high"}


def test_the_band_says_what_the_step_s_sentence_says_about_sharing() -> None:
    """A step that runs after its wave's other steps for being shared by many parts reads
    high, and every step it runs after reads low."""
    plan = _plan()
    by_number = {s["step_number"]: s for s in plan["plan_steps"]}
    late = [s for s in plan["plan_steps"] if s["ties"]["runs_after_steps"]]
    assert late
    for step in late:
        assert step["score_bands"]["hub_score"] == "high"
        assert "shared by many parts" in step["why"]
        for n in step["ties"]["runs_after_steps"]:
            assert by_number[n]["score_bands"]["hub_score"] == "low"


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        (lambda p: p["plan_steps"][3]["score_bands"].update(hub_score="low"), "band is not"),
        (lambda p: p["move_units"][1]["score_bands"].update(hub_score="high"), "band is not"),
        (lambda p: p["plan_steps"][0]["score_bands"].update(hub_score="high"), "no scale or value"),
        (lambda p: p["score_scales"].pop("hub_score"), "no scale or value"),
        (lambda p: p["move_units"][1]["score_bands"].update(gravity="high"), "has no band"),
        (lambda p: p["score_scales"].update(blast_radius=p["score_scales"]["gravity"]), "only"),
        (lambda p: p["score_scales"]["gravity"].update(high_from=0.5), "together"),
    ],
)
def test_the_contract_refuses_a_band_off_its_scale(change: Any, rule: str) -> None:
    plan = copy.deepcopy(_plan())
    SlicePlanFacts.model_validate(plan)
    assert plan["plan_steps"][3]["score_bands"]["hub_score"] == "high"
    assert plan["move_units"][1]["score_bands"] == {"hub_score": "low"}
    assert "metrics" not in plan["plan_steps"][0] or not plan["plan_steps"][0]["metrics"]
    change(plan)
    with pytest.raises(ValidationError, match=rule):
        SlicePlanFacts.model_validate(plan)


# --- An older report ------------------------------------------------------------------------


def test_a_plan_stored_before_the_bands_gains_them_at_read_time() -> None:
    report = json.loads(PRE_1699.read_text(encoding="utf-8"))
    stored = report["facts"]["slice_plan"]
    assert "score_scales" not in stored
    plan = report_facts(report)["slice_plan"]
    SlicePlanFacts.model_validate(plan)
    assert plan["score_scales"] == why.score_scales()
    assert not why.needs_step_explanations(plan)
    for record in _records(plan):
        assert record["score_bands"] == why.score_bands(record.get("metrics"))
    assert any(r["score_bands"] for r in _records(plan))
    # Every stored unit figure is kept as it was.
    for old, new in zip(stored["move_units"], plan["move_units"], strict=True):
        assert {k: v for k, v in new.items() if k != "score_bands"} == old


def test_a_plan_re_dumped_through_the_contract_before_the_bands_is_filled() -> None:
    """Re-dumped through the contract, a plan stored before #2288 reads empty bands and
    scales; the reader fills them."""
    report = json.loads(PRE_1699.read_text(encoding="utf-8"))
    filled = why.with_step_explanations(report["facts"]["slice_plan"])
    dumped = SlicePlanFacts.model_validate(filled).model_dump(mode="json")
    dumped["score_scales"] = {}
    for record in _records(dumped):
        record["score_bands"] = {}
    SlicePlanFacts.model_validate(dumped)
    assert why.needs_step_explanations(dumped)
    report["facts"]["slice_plan"] = dumped
    plan = report_facts(report)["slice_plan"]
    assert plan["score_scales"] == why.score_scales()
    assert [r["score_bands"] for r in _records(plan)] == [
        why.score_bands(r.get("metrics")) for r in _records(dumped)
    ]


def test_stored_bands_and_scales_are_read_as_stored() -> None:
    """The reader fills what a plan lacks and never rewrites what it states."""
    plan = copy.deepcopy(_plan())
    plan["score_scales"]["hub_score"]["note"] = "Stored note."
    assert not why.needs_step_explanations(plan)
    assert why.with_step_explanations(plan) == plan


def test_a_plan_saved_with_a_how_tightly_tied_band_reads_it_as_a_plain_value() -> None:
    """A plan saved while how tightly tied had a band (its scale with a 0.5 threshold,
    a band on each step and unit) still loads, and the reader drops that band and takes
    today's scale for it; the shared by many parts bands it stored are kept."""
    report = json.loads(PRE_1699.read_text(encoding="utf-8"))
    plan = why.with_step_explanations(report["facts"]["slice_plan"])
    plan["score_scales"]["gravity"] = {
        "high_from": 0.5,
        "band_labels": {"low": "low", "high": "high"},
        "note": "An older note with low and high bands.",
    }
    for record in _records(plan):
        value = (record.get("metrics") or {}).get("gravity")
        if value is not None:
            record["score_bands"]["gravity"] = "high" if value >= 0.5 else "low"
    assert any("gravity" in r["score_bands"] for r in _records(plan))
    SlicePlanFacts.model_validate(plan)
    assert why.needs_step_explanations(plan)
    report["facts"]["slice_plan"] = plan
    for read in (why.with_step_explanations(plan), report_facts(report)["slice_plan"]):
        SlicePlanFacts.model_validate(read)
        assert read["score_scales"]["gravity"] == why.score_scales()["gravity"]
        assert all("gravity" not in r["score_bands"] for r in _records(read))
        assert [r["score_bands"].get("hub_score") for r in _records(read)] == [
            r["score_bands"].get("hub_score") for r in _records(plan)
        ]
        assert not why.needs_step_explanations(read)


def test_the_old_release_s_job_reads_with_scales_and_bands() -> None:
    """tests/fixtures/old_jobs: a real older release's report, read through the facts
    reader, states a scale for each score. Its plan has no steps or units, so the bands on
    an older report rest on the recorded sample run below and the pre-#1699 fixture."""
    reports = sorted(OLD_JOBS.glob("*/*/*/synthesis/v*/report.json"))
    assert reports
    for path in reports:
        report = json.loads(path.read_text(encoding="utf-8"))
        plan = report_facts(report)["slice_plan"]
        SlicePlanFacts.model_validate(plan)
        assert plan["score_scales"] == why.score_scales(), path
        for record in _records(plan):
            assert record.get("score_bands", {}) == why.score_bands(record.get("metrics")), path


# --- The engineering report -------------------------------------------------------------------


def _wave(facts: dict[str, Any], wave: int) -> list[str]:
    return wave_stages_md(facts, wave, _SaidOnce())


def test_the_engineering_report_prints_each_score_as_its_band_and_value() -> None:
    facts = _facts()
    plan = facts["slice_plan"]
    lines = _wave(facts, 3)
    for step in [s for s in plan["plan_steps"] if s["phase_wave"] == 3 and s.get("metrics")]:
        row = next(line for line in lines if line.startswith(f"| Step {step['step_number']} |"))
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        # How tightly tied is its value; shared by many parts its band, then its value.
        assert cells[1] == fmt_num(step["metrics"]["gravity"])
        band = plan["score_scales"]["hub_score"]["band_labels"][step["score_bands"]["hub_score"]]
        assert cells[2] == f"{band.capitalize()} ({fmt_num(step['metrics']['hub_score'])})"
    # Each score column's sentence follows the table, once, in the facts' words.
    for field in SCORED_METRICS:
        note = plan["score_scales"][field]["note"]
        assert sum(1 for line in lines if note in line) == 1, field


def test_the_engineering_report_prints_the_facts_band_words() -> None:
    """A band word is the facts', never a copy in the renderer."""
    facts = copy.deepcopy(_facts())
    for scale in facts["slice_plan"]["score_scales"].values():
        if scale["band_labels"]:
            scale["band_labels"] = {"low": "sentinel-low", "high": "sentinel-high"}
    text = "\n".join(_wave(facts, 3))
    assert "Sentinel-high (" in text and "Sentinel-low (" in text
    assert "| High (" not in text and "| Low (" not in text


def test_a_plan_with_no_scales_prints_the_raw_values() -> None:
    facts = copy.deepcopy(_facts())
    facts["slice_plan"]["score_scales"] = {}
    text = "\n".join(_wave(facts, 3))
    assert "High (" not in text and "Low (" not in text
    for scale in why.score_scales().values():
        assert scale["note"] not in text


def test_the_engineering_report_s_unit_table_prints_each_score_as_its_band_and_value() -> None:
    """The unit table states each unit's scores as the step table does: shared by many
    parts as "Low (0)", how tightly tied as its value."""
    facts = _facts()
    plan = facts["slice_plan"]
    lines = _wave(facts, 3)
    header_at = next(i for i, line in enumerate(lines) if line.startswith("| Stage | Pilot"))
    header = [c.strip() for c in lines[header_at].strip().strip("|").split("|")]
    rows = [
        [c.strip() for c in line.strip().strip("|").split("|")]
        for line in lines[header_at + 2 :]
        if line.startswith("| ")
    ]
    columns = {
        field: header.index(
            plan["metric_labels"][field][:1].upper() + plan["metric_labels"][field][1:]
        )
        for field in SCORED_METRICS
    }
    stated = {
        (row[columns["gravity"]], row[columns["hub_score"]])
        for row in rows
        if len(row) == len(header)
    }
    expected = set()
    for unit in plan["move_units"]:
        metrics, bands = unit.get("metrics"), unit.get("score_bands") or {}
        if not metrics or not bands:
            continue
        word = plan["score_scales"]["hub_score"]["band_labels"][bands["hub_score"]]
        expected.add(
            (fmt_num(metrics["gravity"]), f"{word.capitalize()} ({fmt_num(metrics['hub_score'])})")
        )
    shown = expected & stated
    assert shown, (expected, stated)
    # Every unit row's shared by many parts cell is banded; how tightly tied never is.
    for gravity, hub in stated:
        assert not gravity.endswith(")") and hub.endswith(")"), (gravity, hub)


def test_a_band_filled_for_a_stored_plan_sits_on_the_scale_it_stores() -> None:
    """A plan that stores its scales but lacks a band gains the band on its own stored
    threshold, never on today's, so the contract keeps loading it."""
    plan = copy.deepcopy(_plan())
    plan["score_scales"]["hub_score"]["high_from"] = 0.7
    for record in _records(plan):
        record["score_bands"] = {}
    assert why.needs_step_explanations(plan)
    filled = why.with_step_explanations(plan)
    SlicePlanFacts.model_validate(filled)
    step = next(s for s in filled["plan_steps"] if s["step_number"] == 4)
    assert step["metrics"]["hub_score"] == 0.67 and step["score_bands"]["hub_score"] == "low"
    assert why.score_bands(step["metrics"])["hub_score"] == "high"


def test_the_recorded_sample_run_gains_its_bands_at_read_time() -> None:
    """The help site's sample run, recorded by a release before #2288, stores slice
    metrics but no bands; read back, each step and unit with a score has its band."""
    path = SAMPLE_RUN / "synthesis" / "v2" / "report.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    stored = report["facts"]["slice_plan"]
    assert "score_scales" not in stored
    measured = [r for r in [*stored["plan_steps"], *stored["move_units"]] if r.get("metrics")]
    assert measured and all("score_bands" not in r for r in measured)
    plan = report_facts(report)["slice_plan"]
    SlicePlanFacts.model_validate(plan)
    banded = [r for r in _records(plan) if r.get("metrics")]
    assert banded and all(r["score_bands"] == why.score_bands(r["metrics"]) for r in banded)
    assert all(r["score_bands"] for r in banded)
