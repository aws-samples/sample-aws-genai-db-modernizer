# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The engineering report states each thing once, on every sample (#1170).

The deterministic sections a rubric judge found repeating (the per-table risk
in "Sources of truth and replicas", the wave plan's table lists, the reality
check's trade-offs and its reason in the appendix) state each long sentence once
per section; no query id of the report is left bare outside a code span (only
known ids count, so a hash in model prose is not one); and no raw label
("**unsupported pattern**", "(was tied)") reaches the report. In the
external-job mode the same checks run on a model run's report.
"""

from __future__ import annotations

import json
import re

import pytest

from src.report.escaping import md_code, md_text
from src.report.executive_report import (
    _names_pattern,
    card_mitigation,
    table_names,
    without_table_names,
)
from src.report.renderers import render_engineering_report_md
from src.report.risks import _risk_engine_and_body, filtered_risks, shared_mitigation_refs
from src.shared.gate_facts import GATE_TABLES_SHOWN
from src.shared.migration_risk_rules import GATE_IDS_NAMED
from tests.e2e.deliverable_figures import pdf_page_texts
from tests.e2e.pipeline import PipelineResult
from tests.report_concision import (
    bare_ids,
    cross_section_repeats,
    gate_restatements,
    gate_risk_ids,
    known_query_ids,
    md_sections,
    raw_labels,
    remedy_copies,
    remedy_restatements,
    repeated_paragraphs,
    repeated_sentences,
    repeated_statements,
    section,
)


def _md(run: PipelineResult) -> str:
    return run.deliverable("engineering-report.md").read_text(encoding="utf-8")


def test_the_sources_of_truth_state_each_risk_once(run: PipelineResult) -> None:
    body = section(_md(run), "Sources of truth and replicas")
    assert repeated_sentences(body) == []


# The pointer a capped gate list ends with (#1642, ``roadmap._gate_text``).
MAP_POINTER = "the Migration map above lists each with its owner and readers"


def _check_wave_lists(md: str, report: dict) -> None:
    """Each wave section states each sentence once, and every table a gate list holds
    is still named: in its wave, in "Sources of truth and replicas" when the gate refers
    there (#1219 review: a list is referred to only where it is shown in full), or, for
    a list capped at ``GATE_TABLES_SHOWN`` names (#1642), its first names in the gate
    and every table in the Migration map the gate points to."""
    waves = {int(w["wave"]): w for w in report.get("migration_waves") or []}
    for heading, body in md_sections(section(md, "Migration roadmap"), 3):
        assert repeated_sentences(body) == [], heading
        n = int(heading.split(":", 1)[0].removeprefix("Wave "))
        kept = [o["table"] for o in waves[n].get("table_owners") or []]
        gate = next(ln for ln in body.splitlines() if ln.startswith("- Gate before"))
        for part in waves[n].get("gate_parts") or []:
            items = part.get("items") or []
            if items and set(items) == set(kept):
                # The kept-in-sync bullet names them; the gate refers to it.
                assert md_text(", ".join(items)) not in gate, heading
            capped = (
                len(items) > GATE_TABLES_SHOWN
                and md_text(
                    f"{items[GATE_TABLES_SHOWN - 1]} and {len(items) - GATE_TABLES_SHOWN} more"
                )
                in gate
            )
            if capped:
                assert md_text(MAP_POINTER) in gate, heading
                first = ", ".join(items[:GATE_TABLES_SHOWN])
                assert md_text(first) in gate, (heading, first)
                mapped = section(md, "Migration map")
                for table in items[GATE_TABLES_SHOWN:]:
                    assert f"`{md_code(table)}`" in mapped, (heading, table)
                continue
            named = body
            if 'listed under "Sources of truth and replicas"' in gate:
                named += section(md, "Sources of truth and replicas")
            for table in items:
                assert md_text(table) in named or f"`{table}`" in named, (heading, table)


def test_each_wave_states_each_sentence_and_table_list_once(run: PipelineResult) -> None:
    _check_wave_lists(_md(run), json.loads(run.report_json().read_text()))


@pytest.mark.deterministic
def test_a_capped_gate_list_names_its_first_tables_and_the_map_names_the_rest(
    designed_run: PipelineResult,
) -> None:
    """The deterministic samples' gate lists stay at 5 tables or fewer, so the cap only
    ran on a model run (the scoped discourse wave 2 gate: 70 tables). Force one here:
    a gate list of every Migration map row that another engine reads is capped in the
    wave section, and the check above finds each table in the map."""
    report = json.loads(designed_run.report_json().read_text())
    lines = report["facts"]["table_owner_lines"]["table_lines"]
    tables = [r["table"] for r in lines][: GATE_TABLES_SHOWN + 4]
    assert len(tables) > GATE_TABLES_SHOWN
    wave = report["migration_waves"][-1]
    wave["gate_parts"] = [
        {"text": f"Check. {len(tables)} tables", "items": tables},
        {"text": " stay dual-read.", "items": []},
    ]
    wave["gate"] = f"Check. {len(tables)} tables ({', '.join(tables)}) stay dual-read."
    md = render_engineering_report_md(report)
    assert md_text(MAP_POINTER) in md
    _check_wave_lists(md, report)


def test_the_reality_check_reason_is_stated_once(run: PipelineResult) -> None:
    md = _md(run)
    trade_offs = section(md, "Migration trade-offs")
    for heading, body in md_sections(trade_offs, 3):
        if heading == "Reality check":
            assert repeated_sentences(body) == []
    assert "### reality-check" not in md
    appendix = section(md, "Appendix: per-query routing decisions")
    assert appendix.count('the reason given under "Migration trade-offs" (Reality check)') <= 1


def test_no_bare_id_and_no_raw_label(run: PipelineResult) -> None:
    md = _md(run)
    ids = known_query_ids(json.loads(run.report_json().read_text()))
    assert bare_ids(md, ids) == []
    assert raw_labels(md) == []


@pytest.mark.deterministic
def test_the_designed_job_has_no_raw_label(designed_run: PipelineResult) -> None:
    md = _md(designed_run)
    ids = known_query_ids(json.loads(designed_run.report_json().read_text()))
    assert bare_ids(md, ids) == []
    assert raw_labels(md) == []
    assert repeated_sentences(section(md, "Sources of truth and replicas")) == []


def test_no_long_paragraph_is_printed_twice(run: PipelineResult) -> None:
    md = _md(run)
    assert repeated_paragraphs(md) == []
    assert repeated_statements(md) == []


@pytest.mark.deterministic
def test_the_designed_job_states_each_long_sentence_once(designed_run: PipelineResult) -> None:
    md = _md(designed_run)
    assert repeated_paragraphs(md) == [] and repeated_statements(md) == []
    report = json.loads(designed_run.report_json().read_text())
    register = section(md, "Risk register")
    for risk in report["risk_assessment"]["risks"]:
        if risk.get("rule") in ("design.trade_offs", "cache.unsupported"):
            for detail in risk.get("details") or []:
                assert f"  - {md_text(detail)}" not in register.splitlines(), detail[:40]
    counts = report["facts"]["query_group_counts"]
    listed, total = counts["listed_group_count"], counts["group_count"]
    assert listed + counts["relational_only_group_count"] == total
    heading = (
        f"## Query groups ({listed} of {total})"
        if total > listed
        else f"## Query groups ({listed})"
    )
    assert heading in md.splitlines()


def test_each_item_is_listed_in_one_section(run: PipelineResult) -> None:
    """No item one section lists is listed again in another (#1220), and each pointer
    names a section that holds it."""
    md = _md(run)
    report = json.loads(run.report_json().read_text())
    repeats = cross_section_repeats(md)
    if any(
        "utility/metadata statement" in str(reason)
        for g in report.get("query_groups") or []
        for reason in g.get("reasons") or []
    ):
        # A report synthesized before #1220 keeps its stored reason, as the UI shows it.
        repeats.pop("raw_utility_reason")
    assert set(repeats.values()) <= {0}, repeats
    if "listed by engine pair under Close calls" in section(md, "Risk register"):
        assert section(md, "Close calls")
    pending = section(md, "Pending engine choices")
    for tables in re.findall(r"the choice is under Pending engine choices \((.+?)\)\.", md):
        for table in re.findall(r"`([^`]+)`", tables):
            assert f"### `{table}` (" in pending, table
    strategies = section(md, "Mitigation strategies")
    register = section(md, "Risk register")
    mitigations = {
        r.get("risk_id"): str(r.get("mitigation") or "") for r in report["risk_assessment"]["risks"]
    }
    referred = re.findall(
        r"^- (RISK-\d+) .*its mitigation is under its Risk register", strategies, re.M
    )
    for rid in referred:
        # The register entry it refers to prints that mitigation (or its own text says it).
        entry = register.split(f"**{rid}**", 1)[1].split("\n- **", 1)[0]
        assert "  - Mitigation: " in entry or md_text(mitigations[rid]) in entry, rid


@pytest.mark.replay_stale
def test_each_risk_remedy_and_wave_gate_is_stated_once(run: PipelineResult) -> None:
    """#1510: the Risk register owns each risk's remedy and the roadmap owns each wave
    gate. No mitigation strategy restates a remedy (``remedy_restatements``, a shared
    phrase, not a sentence match); neither the engineering report's Mitigation strategies
    nor the executive report's Next steps print one; no mitigation or strategy states a
    wave gate, and each wave's gate names the risks to close before it. Marked
    ``replay_stale``: the replay's saved report.json was synthesized before #1510 and its
    stored strategies are rendered as written; fresh e2e-llm runs check it."""
    report = json.loads(run.report_json().read_text())
    assert remedy_restatements(report) == []
    assert gate_restatements(report) == []
    md = _md(run)
    assert remedy_copies(section(md, "Mitigation strategies"), report) == []
    pdf = " ".join(pdf_page_texts(str(run.deliverable("executive-report.pdf"))))
    at = pdf.find("Next steps 1. ")
    assert remedy_copies(pdf[at:] if at >= 0 else "", report) == []
    _check_cards_state_what_strategies_name(report, pdf)
    named = gate_risk_ids(report)
    by_wave: dict[int, list[str]] = {}
    for risk in report["risk_assessment"]["risks"]:
        if risk.get("rule") and risk.get("wave") in named:
            by_wave.setdefault(risk["wave"], []).append(risk["risk_id"])
    for wave, ids in by_wave.items():
        gate = report["migration_waves"][wave - 1]["gate"]
        assert "before this gate" in gate, wave
        if len(ids) <= GATE_IDS_NAMED:
            assert set(ids) <= named[wave], (wave, ids)


def _check_cards_state_what_strategies_name(report: dict, pdf: str) -> None:
    """Every risk a mitigation strategy (so Next steps) names shows its mitigation on its
    executive card (#1510): its Mitigation line, "as for" a card that does, or its own
    description, which states it, still with table names taken out."""
    strategies = " ".join(report["risk_assessment"].get("mitigation_strategies") or [])
    named = set(re.findall(r"\bRISK-\d+\b", strategies))
    names = _names_pattern(table_names(report))
    refs = shared_mitigation_refs(report)
    by_id = {r["risk_id"]: r for r in filtered_risks(report)}
    for rid in sorted(named & set(by_id)):
        risk = by_id[rid]
        if not risk.get("mitigation"):
            continue
        line = card_mitigation(risk, names, refs)
        if line and line.startswith("as for "):
            assert refs[rid] in by_id, rid
            risk, line = by_id[refs[rid]], card_mitigation(by_id[refs[rid]], names, refs)
        stated = line or without_table_names(_risk_engine_and_body(risk["description"])[1], names)
        lead = " ".join(stated.split()[:6])
        assert lead and lead in pdf, (rid, lead)
