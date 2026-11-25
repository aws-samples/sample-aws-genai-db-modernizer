# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#2287: a gate's conditions, one per sentence, each with the table lists it names.

The results page shows each gate as a checklist of ``facts.waves[].gate_conditions`` (a
wave) and ``step_stages[].gate_conditions`` (a stage); the engineering report states the
same conditions as its gate sentence. The cut is made once, in the facts layer, from the
parts the producer wrote (ADR-032), and the conditions join back to the gate exactly.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from src.contracts.synthesis_output import SliceStage, SynthesisOutputContract, WaveFacts
from src.report.escaping import md_text
from src.report.facts import report_facts
from src.report.renderers import render_engineering_report_md
from src.report.roadmap import _gate_text
from src.shared.cutover_message import CUTOVER_MESSAGE, DECOMMISSION_MESSAGE
from src.shared.cutover_wording import cutover_claims
from src.shared.gate_conditions import conditions_for, gate_conditions, join_gate_conditions
from src.shared.report_facts import refresh_risk_facts, wave_facts, with_gate_conditions

REPO = Path(__file__).resolve().parents[3]
SAMPLE = REPO / "docs/examples/wordpress/sample-run/synthesis/v2/report.json"
OLD_JOB = REPO / "tests/fixtures/old_jobs"

# The gate the results page's Jest test cuts (MigrationWavePlan.test.js, CONDITIONS).
JS_GATE_PARTS = [
    {"text": "Dual-write validated. 3 tables", "items": ["wp.options", "wp.termmeta", "wp.keys"]},
    {
        "text": " stay dual-read by Aurora MySQL queries. 2 queries read 2 tables",
        "items": ["wp.posts", "wp.users"],
    },
    {
        "text": " Aurora MySQL owns (wave 1) and keeps owning. The cutover is your responsibility.",
        "items": [],
    },
]
JS_CONDITIONS = [
    {"kind": "check", "parts": [{"text": "Dual-write validated.", "items": []}]},
    {
        "kind": "check",
        "parts": [
            {"text": "3 tables", "items": ["wp.options", "wp.termmeta", "wp.keys"]},
            {"text": " stay dual-read by Aurora MySQL queries.", "items": []},
        ],
    },
    {
        "kind": "check",
        "parts": [
            {"text": "2 queries read 2 tables", "items": ["wp.posts", "wp.users"]},
            {"text": " Aurora MySQL owns (wave 1) and keeps owning.", "items": []},
        ],
    },
    {"kind": "note", "parts": [{"text": "The cutover is your responsibility.", "items": []}]},
]


def _joined(parts: list[dict[str, Any]]) -> str:
    return "".join(p["text"] + (f" ({', '.join(p['items'])})" if p["items"] else "") for p in parts)


def test_each_sentence_is_a_condition_with_the_lists_it_names() -> None:
    assert gate_conditions(JS_GATE_PARTS) == JS_CONDITIONS
    assert join_gate_conditions(JS_CONDITIONS) == _joined(JS_GATE_PARTS)


def test_whose_a_step_is_is_a_note_never_a_condition_to_check() -> None:
    parts = [{"text": f"Parity validated. {CUTOVER_MESSAGE} {DECOMMISSION_MESSAGE}", "items": []}]
    assert [c["kind"] for c in gate_conditions(parts)] == ["check", "note", "note"]


def test_a_sentence_that_mentions_the_cutover_among_other_words_is_a_check() -> None:
    parts = [
        {"text": "Parity validated. The cutover is your responsibility, after parity.", "items": []}
    ]
    assert [c["kind"] for c in gate_conditions(parts)] == ["check", "check"]


def test_a_decimal_a_version_and_a_bracket_do_not_end_a_sentence() -> None:
    text = "82.1% of calls hit the cache on Aurora MySQL 3.08 (version 8.0.39). Close RISK-001."
    conditions = gate_conditions([{"text": text, "items": []}])
    assert [_joined(c["parts"]) for c in conditions] == [
        "82.1% of calls hit the cache on Aurora MySQL 3.08 (version 8.0.39).",
        "Close RISK-001.",
    ]


def test_a_sentence_end_right_after_a_list_closes_that_condition() -> None:
    parts = [{"text": "2 tables", "items": ["a", "b"]}, {"text": ". Then check.", "items": []}]
    conditions = gate_conditions(parts)
    assert conditions == [
        {
            "kind": "check",
            "parts": [{"text": "2 tables", "items": ["a", "b"]}, {"text": ".", "items": []}],
        },
        {"kind": "check", "parts": [{"text": "Then check.", "items": []}]},
    ]


def test_a_sentence_end_between_two_text_runs_is_still_cut() -> None:
    parts = [{"text": "One.", "items": []}, {"text": " Two.", "items": []}]
    assert [_joined(c["parts"]) for c in gate_conditions(parts)] == ["One.", "Two."]


def test_white_space_the_cut_cannot_keep_leaves_the_gate_whole() -> None:
    parts = [{"text": "One.  Two.\nThree.", "items": []}]
    conditions = gate_conditions(parts)
    assert conditions == [{"kind": "check", "parts": parts}]
    assert join_gate_conditions(conditions) == parts[0]["text"]


def test_a_gate_with_no_parts_is_its_string_and_no_gate_is_no_condition() -> None:
    assert [_joined(c["parts"]) for c in gate_conditions(None, "A. B.")] == ["A.", "B."]
    assert gate_conditions(None, None) == []
    assert gate_conditions([], "") == []
    assert gate_conditions([{"text": "  ", "items": []}]) == []


def test_every_gate_of_the_sample_joins_back_from_its_conditions() -> None:
    report = json.loads(SAMPLE.read_text())
    facts = report_facts(report)
    gates = {w["wave"]: w["gate"] for w in report["migration_waves"]}
    assert facts["waves"]
    for row in facts["waves"]:
        assert join_gate_conditions(row["gate_conditions"]) == gates[row["wave"]], row["wave"]
        WaveFacts.model_validate(row)
    stages = [s for st in facts["slice_plan"]["plan_steps"] for s in st["step_stages"]]
    assert stages
    for stage in stages:
        assert join_gate_conditions(stage["gate_conditions"]) == stage["gate"], stage["label"]
        SliceStage.model_validate(stage)
    # Wave 3 lists its tables; wave 1 states whose the cutover is, as a note.
    wave3 = next(r for r in facts["waves"] if r["wave"] == 3)
    assert any(p["items"] for c in wave3["gate_conditions"] for p in c["parts"])
    wave1 = next(r for r in facts["waves"] if r["wave"] == 1)
    assert {"kind": "note", "parts": [{"text": CUTOVER_MESSAGE, "items": []}]} in (
        wave1["gate_conditions"]
    )


def test_wave_facts_cut_the_wave_s_gate() -> None:
    wave = {
        "wave": 2,
        "engines": ["dynamodb"],
        "gate": _joined(JS_GATE_PARTS),
        "gate_parts": JS_GATE_PARTS,
    }
    assert wave_facts(wave)["gate_conditions"] == JS_CONDITIONS
    assert wave_facts({"wave": 2, "engines": []})["gate_conditions"] == []


def test_a_block_stored_before_the_conditions_gains_them_when_read() -> None:
    report = json.loads(SAMPLE.read_text())
    for row in report["facts"]["waves"]:
        row.pop("gate_conditions", None)
    for step in report["facts"]["slice_plan"]["plan_steps"]:
        for stage in step["step_stages"]:
            stage["gate_conditions"] = None  # as a block re-dumped through the contract
    facts = report_facts(report)
    by_wave = {w["wave"]: w for w in report["migration_waves"]}
    for row in facts["waves"]:
        w = by_wave[row["wave"]]
        assert row["gate_conditions"] == gate_conditions(w["gate_parts"], w["gate"])
    for step in facts["slice_plan"]["plan_steps"]:
        for stage in step["step_stages"]:
            assert stage["gate_conditions"] == gate_conditions(
                stage["stage_gate_parts"], stage["gate"]
            )


def test_an_old_release_s_job_reads_with_conditions_that_join_back() -> None:
    reports = sorted(OLD_JOB.rglob("synthesis/v*/report.json"))
    assert reports, "the old job fixture holds a saved report"
    report = json.loads(reports[-1].read_text())
    facts = report_facts(report)
    gates = {w["wave"]: w.get("gate") or "" for w in report.get("migration_waves") or []}
    for row in facts.get("waves") or []:
        assert join_gate_conditions(row["gate_conditions"]) == gates.get(row["wave"], "")


def test_a_gate_rewritten_with_the_register_s_risks_cuts_its_conditions_again() -> None:
    report = json.loads(SAMPLE.read_text())
    wave = next(w for w in report["migration_waves"] if w["wave"] == 2)
    wave["gate"] += " Close RISK-099 before this gate (a new risk)."
    wave["gate_parts"][-1]["text"] += " Close RISK-099 before this gate (a new risk)."
    facts = refresh_risk_facts(report["facts"], report)
    row = next(r for r in facts["waves"] if r["wave"] == 2)
    assert join_gate_conditions(row["gate_conditions"]) == wave["gate"]
    assert _joined(row["gate_conditions"][-1]["parts"]) == (
        "Close RISK-099 before this gate (a new risk)."
    )


def test_a_row_with_no_wave_to_cut_from_is_kept_as_stored() -> None:
    rows = [{"wave": 9, "gate_conditions": None}, "not a record"]
    assert with_gate_conditions(rows, [{"wave": 1, "gate": "A."}]) == rows


def test_a_stage_whose_conditions_do_not_join_back_is_refused() -> None:
    report = json.loads(SAMPLE.read_text())
    stage = copy.deepcopy(report_facts(report)["slice_plan"]["plan_steps"][0]["step_stages"][0])
    stage["gate_conditions"][0]["parts"][0]["text"] = "Something else."
    with pytest.raises(ValueError, match="gate_conditions must join back to gate"):
        SliceStage.model_validate(stage)


def _gate_line(md: str, wave: int) -> str:
    body = md.split(f"### Wave {wave}:", 1)[1]
    return next(line for line in body.splitlines() if line.startswith("- Gate before"))


def test_the_engineering_report_states_the_facts_conditions() -> None:
    """The engineering report's gate line is the facts' conditions joined, the same
    sentences the results page lists and the gate the executive report prints."""
    report = json.loads(SAMPLE.read_text())
    report["facts"] = report_facts(report)
    wave = next(w for w in report["migration_waves"] if w["wave"] == 2)
    row = next(r for r in report["facts"]["waves"] if r["wave"] == 2)
    assert join_gate_conditions(row["gate_conditions"]) == wave["gate"]
    md = render_engineering_report_md(report)
    assert _gate_line(md, 2) == f"- Gate before the next wave: {md_text(wave['gate'])}"


def test_conditions_that_do_not_join_back_to_the_gate_are_refused_or_cut_again() -> None:
    """Deliverables agree: a facts block whose conditions say something other than the
    wave's gate fails the report contract, and the reader states the gate cut again, so
    no deliverable prints the disagreeing words."""
    report = json.loads(SAMPLE.read_text())
    report["facts"] = report_facts(report)
    wave = next(w for w in report["migration_waves"] if w["wave"] == 2)
    row = next(r for r in report["facts"]["waves"] if r["wave"] == 2)
    row["gate_conditions"] = [
        {"kind": "check", "parts": [{"text": "Something the gate does not say.", "items": []}]}
    ]
    with pytest.raises(ValueError, match="gate_conditions must join back"):
        SynthesisOutputContract.model_validate(report)
    read = next(r for r in report_facts(report)["waves"] if r["wave"] == 2)
    assert read["gate_conditions"] == gate_conditions(wave["gate_parts"], wave["gate"])
    md = render_engineering_report_md(report)
    assert "Something the gate does not say" not in md
    assert _gate_line(md, 2) == f"- Gate before the next wave: {md_text(wave['gate'])}"


def test_the_sample_report_with_its_conditions_passes_the_contract() -> None:
    report = json.loads(SAMPLE.read_text())
    report["facts"] = report_facts(report)
    SynthesisOutputContract.model_validate(report)


def test_an_empty_condition_list_reads_as_the_gate_in_every_deliverable() -> None:
    """With no condition stored for a wave that has a gate, the reader cuts the gate, and
    the engineering report's fallback is the gate itself, as the page's is
    (migrationWaves.js gateConditionsOf: the gate as one condition)."""
    report = json.loads(SAMPLE.read_text())
    report["facts"] = report_facts(report)
    wave = next(w for w in report["migration_waves"] if w["wave"] == 1)
    row = next(r for r in report["facts"]["waves"] if r["wave"] == 1)
    row["gate_conditions"] = []
    assert (
        join_gate_conditions(
            next(r for r in report_facts(report)["waves"] if r["wave"] == 1)["gate_conditions"]
        )
        == wave["gate"]
    )
    assert _gate_text(wave, {"wave": 1, "gate_conditions": []}, {}) == wave["gate"]


def test_stored_conditions_that_join_back_survive_the_reader_for_waves_and_stages() -> None:
    """The wave and the stage backfill follow one rule: stored conditions that join back
    to the gate are kept as stored (here one condition holding two sentences)."""
    report = json.loads(SAMPLE.read_text())
    report["facts"] = report_facts(report)
    row = next(r for r in report["facts"]["waves"] if r["wave"] == 1)
    parts = [p for c in row["gate_conditions"] for p in _spaced(c)]
    parts[0] = {**parts[0], "text": parts[0]["text"].lstrip()}
    whole: list[dict[str, Any]] = [{"kind": "check", "parts": parts}]
    assert join_gate_conditions(whole) == join_gate_conditions(row["gate_conditions"])
    row["gate_conditions"] = whole
    stage = report["facts"]["slice_plan"]["plan_steps"][0]["step_stages"][0]
    assert len(stage["gate_conditions"]) == 2
    merged = [{"kind": "check", "parts": [{"text": stage["gate"], "items": []}]}]
    stage["gate_conditions"] = merged
    facts = report_facts(report)
    assert next(r for r in facts["waves"] if r["wave"] == 1)["gate_conditions"] == whole
    assert facts["slice_plan"]["plan_steps"][0]["step_stages"][0]["gate_conditions"] == merged


def _spaced(condition: dict[str, Any]) -> list[dict[str, Any]]:
    parts = [dict(p) for p in condition["parts"]]
    parts[0]["text"] = " " + parts[0]["text"]
    return parts


@pytest.mark.parametrize(
    "path",
    [
        "tests/fixtures/old_jobs/v0.4.0-beta.2/wordpress/aa4e840a/synthesis/v2/report.json",
        "tests/fixtures/real_runs/wordpress-both/report.json",
    ],
)
def test_older_cutover_wording_is_never_a_condition_to_check(path: str) -> None:
    """A gate saved before #2107 and #2265 gives cutover and decommissioning advice in its
    own words ("decommission the legacy source once replication lag is zero"). The reader
    keeps those words, so every deliverable still prints the stored gate, but such a
    sentence is a note: the checklist never shows it as a step to do."""
    report = json.loads((REPO / path).read_text())
    facts = report_facts(report)
    checks = [
        "".join(p["text"] for p in c["parts"])
        for row in facts["waves"]
        for c in row["gate_conditions"]
        if c["kind"] == "check"
    ]
    assert checks
    assert [t for t in checks if cutover_claims(t)] == []
    notes = [
        "".join(p["text"] for p in c["parts"])
        for row in facts["waves"]
        for c in row["gate_conditions"]
        if c["kind"] == "note"
    ]
    assert any("decommission" in t for t in notes), notes


def test_a_stored_cutover_sentence_marked_as_a_check_reads_as_a_note() -> None:
    parts = [{"text": "Parity validated. Decommission the source once lag is zero.", "items": []}]
    stored = [
        {"kind": "check", "parts": [{"text": "Parity validated.", "items": []}]},
        {
            "kind": "check",
            "parts": [{"text": "Decommission the source once lag is zero.", "items": []}],
        },
    ]
    assert [c["kind"] for c in conditions_for(stored, parts, None)] == ["check", "note"]


_SAVED_REPORTS = sorted(
    {
        *REPO.glob("docs/examples/*/sample-run/synthesis/v*/report.json"),
        *REPO.glob("tests/fixtures/old_jobs/*/*/*/synthesis/v*/report.json"),
        *REPO.glob("tests/fixtures/real_runs/*/report.json"),
    }
)


@pytest.mark.parametrize("path", _SAVED_REPORTS, ids=lambda p: str(p.relative_to(REPO)))
def test_every_saved_gate_cuts_into_whole_sentences_that_name_its_tables(path: Path) -> None:
    """The cut is made only at ". " (a full stop, then a space). A gate template that wrote
    an abbreviation ("e.g. ") or a question would cut a sentence in two; each condition
    of every saved gate ends with a full stop, no gate holds such a hazard, and every item
    a gate lists is a table of the report's waves (the page labels the lists "tables")."""
    report = json.loads(path.read_text())
    facts = report_facts(report)
    tables = {str(t) for w in report.get("migration_waves") or [] for t in w.get("tables") or []}
    gates = [w.get("gate") or "" for w in report.get("migration_waves") or []]
    stages = [
        st for step in facts["slice_plan"]["plan_steps"] for st in step.get("step_stages") or []
    ]
    gates += [st["gate"] for st in stages]
    for gate in gates:
        for hazard in ("e.g. ", "i.e. ", "etc. ", "vs. ", "?"):
            assert hazard not in gate, (hazard, gate)
    conditions = [c for row in facts["waves"] for c in row["gate_conditions"]]
    conditions += [c for st in stages for c in st["gate_conditions"]]
    assert conditions
    for c in conditions:
        text = "".join(p["text"] for p in c["parts"]).strip()
        assert text.endswith("."), text
        for p in c["parts"]:
            assert set(p["items"]) <= tables, p["items"]


def test_the_map_pointer_ends_the_condition_that_holds_the_last_long_list() -> None:
    from src.report.roadmap import GATE_LIST_POINTER

    long = [f"t{i:02d}" for i in range(20)]
    wf = {
        "wave": 3,
        "gate_conditions": [
            {
                "kind": "check",
                "parts": [{"text": "20 tables", "items": long}, {"text": " move.", "items": []}],
            },
            {"kind": "check", "parts": [{"text": "Close RISK-001 before this gate.", "items": []}]},
        ],
    }
    gate = _gate_text({"wave": 3, "engines": [], "tables": []}, wf, {}, frozenset(long))
    assert gate == f"20 tables move. {GATE_LIST_POINTER} Close RISK-001 before this gate."
