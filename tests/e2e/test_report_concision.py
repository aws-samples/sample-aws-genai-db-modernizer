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

import pytest

from src.report.escaping import md_text
from tests.e2e.pipeline import PipelineResult
from tests.report_concision import (
    bare_ids,
    known_query_ids,
    md_sections,
    raw_labels,
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


def test_each_wave_states_each_sentence_and_table_list_once(run: PipelineResult) -> None:
    md = _md(run)
    report = json.loads(run.report_json().read_text())
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
            # Every table a gate list holds is still named in its wave, or in "Sources
            # of truth and replicas" when the gate refers there (#1219 review: a list
            # is referred to only where it is shown in full).
            named = body
            if 'listed under "Sources of truth and replicas"' in gate:
                named += section(md, "Sources of truth and replicas")
            for table in items:
                assert md_text(table) in named or f"`{table}`" in named, (heading, table)


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
    if run.db == "wordpress":
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
