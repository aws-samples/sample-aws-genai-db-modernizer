"""The engineering report states each thing once (#1170).

A rubric judge scored concision 2 of 5 on a wordpress model run (contract 1.39,
saved as ``fixtures/wordpress_model_run_1_39_report.json``): the same risk
sentence for each of 13 tables, the wave 3 gate repeating table lists its
section had just given, raw labels such as "**unsupported pattern**
(`f31a66f5`)" and "(was tied): on DynamoDB", and the reality check's
consolidation reason once per moved query. Referring back to a list shown in
full and stating a shared risk once is presentation (ADR-032): the stored facts
and explanations are printed as stored, and only generated headings and
pointers change.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from src.report.escaping import md_text
from src.report.renderers import render_engineering_report_md
from src.shared.engine_names import display_engine
from src.shared.move_reasons import consolidation_reason
from src.shared.report_facts import replica_risk_text, shared_replica_risk_text
from tests.report_concision import (
    bare_ids,
    known_query_ids,
    md_sections,
    raw_labels,
    repeated_sentences,
    repeated_statements,
    section,
)

FIXTURE = Path(__file__).parent / "fixtures" / "wordpress_model_run_1_39_report.json"
POINTER = (
    'A step marked "engine consolidated away" moved its query for the reason given under '
    '"Migration trade-offs" (Reality check).'
)


@pytest.fixture(scope="module")
def saved() -> dict[str, Any]:
    report: dict[str, Any] = json.loads(FIXTURE.read_text())
    return report


@pytest.fixture
def report(saved: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(saved)


@pytest.fixture(scope="module")
def md(saved: dict[str, Any]) -> str:
    return render_engineering_report_md(copy.deepcopy(saved))


def _wave(md: str, n: int) -> str:
    return next(body for h, body in md_sections(md, 3) if h.startswith(f"Wave {n}:"))


def _gate(wave_md: str) -> str:
    return next(line for line in wave_md.splitlines() if line.startswith("- Gate before"))


def _wave3(report: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    wave = next(w for w in report["migration_waves"] if w["wave"] == 3)
    facts = next(w for w in report["facts"]["waves"] if w["wave"] == 3)
    return wave, facts


# --- the guard, on the judged run ------------------------------------------------


@pytest.mark.parametrize(
    "heading", ["Sources of truth and replicas", "Cache layer", "Migration roadmap"]
)
def test_no_long_sentence_is_stated_twice_in_a_section(md: str, heading: str) -> None:
    assert section(md, heading), heading
    assert repeated_sentences(section(md, heading)) == []


def test_no_long_sentence_is_stated_twice_in_the_reality_check_trade_offs(md: str) -> None:
    trade_offs = section(md, "Migration trade-offs")
    rc = next(body for h, body in md_sections(trade_offs, 3) if h == "Reality check")
    assert repeated_sentences(rc) == []


def test_no_bare_query_id_and_no_raw_label(md: str, saved) -> None:
    assert bare_ids(md, known_query_ids(saved)) == []
    assert raw_labels(md) == []


def test_the_guards_find_what_they_look_for() -> None:
    """The per-table risk differed only in its table and count; it is caught with
    or without a ';' in it. A known id outside a code span is caught; a hash a
    designer writes in prose is not a query id."""
    rows = [
        "- `wp_posts`: Eventually consistent, 28 reads on Aurora read a copy kept in sync.",
        "- `wp_users`: Eventually consistent, 2 reads on Aurora read a copy kept in sync.",
    ]
    assert len(repeated_sentences("\n".join(rows))) == 1
    ids = {"f31a66f5" + "0" * 56}
    assert bare_ids("see f31a66f5 for it", ids) == ["f31a66f5"]
    assert bare_ids("see `f31a66f5` and the UUID 3f2a1b4c-9d8e as a key", ids) == []


# --- Sources of truth and replicas ---------------------------------------------


def test_the_risk_is_stated_once_for_every_table(md: str, saved) -> None:
    body = section(md, "Sources of truth and replicas")
    risk = md_text(shared_replica_risk_text(["dynamodb"]))
    assert f"Every table above has this risk. {risk}" in body.splitlines()
    assert body.count("Eventually consistent") == 1
    # No "Tables:" line repeating the rows of the table above.
    assert "Tables:" not in body and "The risk of each" not in body
    # The read counts are the table's column; the facts keep each row's own risk.
    rows = saved["facts"]["table_sources"]["replicated_tables"]
    assert len(rows) == 13
    for row in rows:
        assert row["risk"] == replica_risk_text(
            row["source_of_truth"], row["replica_engines"], row["replica_read_count"]
        )
        assert f"| `{row['table']}` |" in body


def test_two_sources_share_one_sentence_naming_both(report) -> None:
    row = report["facts"]["table_sources"]["replicated_tables"][0]
    row["source_of_truth"], row["replica_engines"] = "aurora_mysql", ["dynamodb"]
    row["risk"] = replica_risk_text("aurora_mysql", ["dynamodb"], row["replica_read_count"])
    body = section(render_engineering_report_md(report), "Sources of truth and replicas")
    assert body.count("Eventually consistent") == 1
    assert (
        "its source of truth \\(Aurora MySQL or DynamoDB\\) via CDC" in body
        and "should read from the source of truth." in body
    )
    assert repeated_sentences(body) == []


def test_a_risk_worded_otherwise_is_printed_as_stored(report) -> None:
    rows = report["facts"]["table_sources"]["replicated_tables"]
    rows[2]["risk"] = "Stored risk for this table only."
    body = section(render_engineering_report_md(report), "Sources of truth and replicas")
    lines = body.splitlines()
    assert any(line.startswith("Every other table above has this risk. ") for line in lines)
    assert "The risk of each table below:" in lines
    assert f"- `{rows[2]['table']}`: Stored risk for this table only." in lines


# --- the wave plan ---------------------------------------------------------------


def test_the_gate_refers_to_the_lists_its_section_shows_in_full(md: str, saved) -> None:
    wave = _wave(md, 3)
    gate = _gate(wave)
    parts = _wave3(saved)[0]["gate_parts"]
    kept, written = parts[1]["items"], parts[0]["items"]
    assert len(kept) == 3 and len(written) == 13
    # The kept-in-sync bullet names the 3 tables; the gate refers to it.
    assert "- 3 tables kept in sync from their owner: " in wave
    assert "3 tables \\(the tables kept in sync above\\) Aurora MySQL owns" in gate
    # The 13 tables DynamoDB writes are the rows of "Sources of truth and replicas".
    assert '13 tables \\(listed under "Sources of truth and replicas" below\\) stay' in gate
    for table in kept + written:
        assert md_text(table) not in gate
    # The one table group is the wave's table list (18 tables, shown in full).
    assert "  - 39 queries, independent: the wave's source tables above" in wave.splitlines()


def test_a_kept_list_longer_than_the_line_shows_is_listed_once_in_full(report) -> None:
    """The kept-in-sync line shows 15 names, then "+N more". When the gate lists the
    same tables, the line shows them all and the gate refers to it (#1574): the gate
    used to print the whole list after the line's 15, so those 15 were named twice
    (the scoped discourse run's wave 2: 64 tables). Every table is still named once."""
    wave, facts = _wave3(report)
    extra = [f"wordpress.wp_extra_{i:02d}" for i in range(14)]
    facts["kept_in_sync"] = facts["kept_in_sync"] + [
        {"table": t, "owner": "aurora_mysql", "sync": "CDC"} for t in extra
    ]
    facts["kept_in_sync_tables"] = len(facts["kept_in_sync"])
    items = [o["table"] for o in facts["kept_in_sync"]]
    wave["gate_parts"][1]["items"] = items
    wave["gate"] = "".join(
        p["text"] + (f" ({', '.join(p['items'])})" if p.get("items") else "")
        for p in wave["gate_parts"]
    )
    body = _wave(render_engineering_report_md(report), 3)
    gate = _gate(body)
    assert "(+2 more)" not in body
    assert "\\(the tables kept in sync above\\)" in gate
    kept_line = next(ln for ln in body.splitlines() if "kept in sync from their owner" in ln)
    for table in items:
        assert f"`{table}`" in kept_line, table
        assert md_text(table) not in gate, table
    assert repeated_statements(body) == []


@pytest.mark.passes_on_main  # the negative case: the cap stays without a gate list
def test_a_kept_list_the_gate_does_not_list_stays_capped(report) -> None:
    # The negative case: with no gate list of those tables, the line keeps its cap.
    wave, facts = _wave3(report)
    extra = [f"wordpress.wp_extra_{i:02d}" for i in range(14)]
    facts["kept_in_sync"] = facts["kept_in_sync"] + [
        {"table": t, "owner": "aurora_mysql", "sync": "CDC"} for t in extra
    ]
    facts["kept_in_sync_tables"] = len(facts["kept_in_sync"])
    del wave["gate_parts"]
    body = _wave(render_engineering_report_md(report), 3)
    assert "(+2 more)" in body


def test_a_wave_table_list_longer_than_shown_is_not_referred_to(report) -> None:
    wave, _ = _wave3(report)
    extra = [f"wordpress.wp_extra_{i:02d}" for i in range(5)]
    wave["tables"] = wave["tables"] + extra
    wave["table_groups"][0]["tables"] = list(wave["tables"])
    body = _wave(render_engineering_report_md(report), 3)
    assert "(+3 more)" in body
    assert "the wave's source tables above" not in body


def test_a_gate_list_shown_nowhere_else_is_printed_whole(report) -> None:
    wave, _ = _wave3(report)
    items = ["wordpress.wp_a", "wordpress.wp_b"]
    wave["gate_parts"] = [{"text": "Check. 2 tables", "items": items}, {"text": " stay."}]
    wave["gate"] = "Check. 2 tables (wordpress.wp_a, wordpress.wp_b) stay."
    gate = _gate(_wave(render_engineering_report_md(report), 3))
    assert gate == f"- Gate before the next wave: {md_text(wave['gate'])}"


def test_the_replicated_tables_are_referred_to_only_when_listed_in_full(report) -> None:
    report["facts"]["table_sources"]["more_replicated_count"] = 1
    gate = _gate(_wave(render_engineering_report_md(report), 3))
    assert "Sources of truth and replicas" not in gate
    assert md_text("wordpress.wp_woocommerce_order_items") in gate


def test_a_gate_with_no_parts_is_printed_as_stored(report) -> None:
    wave, _ = _wave3(report)
    del wave["gate_parts"]
    gate = _gate(_wave(render_engineering_report_md(report), 3))
    assert gate == f"- Gate before the next wave: {md_text(wave['gate'])}"


# --- labels and headings -----------------------------------------------------------


def test_the_reality_check_trade_offs_have_a_readable_heading(md: str) -> None:
    headings = [h for h, _ in md_sections(section(md, "Migration trade-offs"), 3)]
    assert "Reality check" in headings and "reality-check" not in headings


def test_model_written_trade_offs_are_printed_as_stored(md: str, saved) -> None:
    """ADR-032 Decision 6: no grouping is read out of model text."""
    for t in saved["trade_offs"]:
        line = f"- **{md_text(t['description'].strip())}**"
        assert line in md.splitlines() or any(
            ln.startswith(line + ":") for ln in md.splitlines()
        ), t["description"][:40]


def test_an_unsupported_pattern_with_no_type_names_its_query(md: str) -> None:
    """#1410: the description leads, the query it is about follows it."""
    cache = section(md, "Target schemas by engine")
    line = next(ln for ln in cache.splitlines() if "The query filters wp\\_posts by parent" in ln)
    assert line.startswith("- The query filters wp\\_posts by parent")
    assert line.endswith(" (query `f31a66f5`).")
    assert "unsupported pattern**" not in cache


def test_appendix_headings_are_sentences(md: str) -> None:
    appendix = section(md, "Appendix: per-query routing decisions")
    heads = [line for line in appendix.splitlines() if line.startswith("### ")]
    assert "### 3 queries: moved from DocumentDB to Aurora MySQL" in heads
    assert any(
        h.startswith("### `413c99e4") and h.endswith(": stays on DynamoDB; settled tie")
        for h in heads
    )
    assert not [h for h in heads if "(moved)" in h or "(was tied)" in h or "): on " in h]


# --- the appendix and the reality check ------------------------------------------


def test_the_appendix_points_once_to_the_consolidation_reason(md: str, saved) -> None:
    appendix = section(md, "Appendix: per-query routing decisions")
    assert appendix.count(POINTER) == 1
    assert "DocumentDB provides no unique capabilities over Aurora MySQL" in md
    # Every stored step explanation is printed as stored, except a consolidation's
    # reason, which the pointer states once (#1220): its move and scores stay.
    for trace in saved["query_traces"]["queries"]:
        for step in [trace["first_assignment"], *trace["moves"]]:
            text = step["explanation"]
            if step.get("reason_code") == "consolidate.no_unique_value":
                reason = consolidation_reason(display_engine(step["from_engine"]))
                assert reason in text
                text = text.replace(f": {reason}", "")
            assert md_text(text) in appendix
    assert "which adds too little over the engines kept" not in appendix


def test_the_engine_a_consolidation_step_removed_is_not_named_again(md: str) -> None:
    appendix = section(md, "Appendix: per-query routing decisions")
    assert "but the query left it in version 2 \\(engine consolidated away\\)" not in appendix


def test_without_its_trade_off_there_is_no_pointer(report) -> None:
    """A consolidation no longer listed under Migration trade-offs (an edit
    reversed it) has no line to point to."""
    report["facts"]["reality_check_decisions"] = [
        d for d in report["facts"]["reality_check_decisions"] if d.get("kind") != "consolidation"
    ]
    appendix = section(render_engineering_report_md(report), "Appendix: per-query routing")
    assert POINTER not in appendix
    # With no pointer, each consolidation step keeps its own reason (#1220).
    assert "which adds too little over the engines kept" in appendix


# --- a sentence a migration note states is not stated again (#1574) -----------------

_NO_TRIGGERS = (
    "DynamoDB has no triggers: the application write path for CategorySettings rejects "
    "that attribute, or the attribute is dropped from the item."
)


def _trigger_note(name: str, column: str) -> dict[str, Any]:
    return {
        "object_name": name,
        "object_type": "trigger",
        "source_table": "discourse.category_settings",
        "application_logic_required": (
            "The BEFORE INSERT and BEFORE UPDATE triggers raise an error when the "
            f"deprecated {column} column is written. {_NO_TRIGGERS}"
        ),
    }


def test_a_sentence_two_notes_and_a_mitigation_share_is_stated_once(report) -> None:
    """The scoped discourse run printed the model's "DynamoDB has no triggers: ..."
    sentence 3 times: in two migration notes (the model wrote it for both triggers)
    and in the risk mitigation synthesis builds from the first note. Each long sentence
    is stated once; a later line points at the first."""
    notes = [
        _trigger_note("cs_reply_readonly", "require_reply_approval"),
        _trigger_note("cs_topic_readonly", "require_topic_approval"),
    ]
    report["schema_designs"]["dynamodb"]["migration_notes"] = notes
    report["risk_assessment"]["risks"] = [
        {
            "risk_id": "RISK-003",
            "risk_type": "OPERATIONAL_RISK",
            "severity": "MEDIUM",
            "description": "[dynamodb] trigger: cs_reply_readonly needs application-side logic on DynamoDB.",
            "mitigation": f"Implement as application logic: {notes[0]['application_logic_required']}",
            "affected_tables": ["discourse.category_settings"],
        }
    ]
    md = render_engineering_report_md(report)
    assert [s for s, _ in repeated_statements(md) if "has no triggers" in s] == []
    assert md.count(md_text(_NO_TRIGGERS).rstrip(".")) == 1
    second = next(ln for ln in md.splitlines() if "cs\\_topic\\_readonly:" in ln)
    assert "Stated in the cs\\_reply\\_readonly migration note." in second
    mitigation = next(ln for ln in md.splitlines() if "Mitigation: Implement as application" in ln)
    assert "Stated in the cs\\_reply\\_readonly migration note." in mitigation
    # The sentence each note states on its own is kept.
    assert "deprecated require\\_topic\\_approval column is written." in second
