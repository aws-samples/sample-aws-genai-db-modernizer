"""The engineering report says each thing once, across sections (#1410).

The rubric judge scored concision 2 of 5 on every wordpress model run this week
(the latest is saved as ``fixtures/wordpress_model_run_1_60_report.json``): the
risk register copied every design trade-off and unsupported cache pattern that
"Migration trade-offs" and "Target schemas by engine" already list, 17 column
defaults repeated two notes word for word, the Migration map restated what
Target engines had just defined, three resolved risks repeated one reason, and
the "Query groups" heading counted a pointer ("plus 18 Aurora groups below")
that the section below did not show. Each test here guards one of those, on the
judged run and on edge cases built from it.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from src.report.architecture import KEPT_IN_SYNC_NOTE, KEPT_IN_SYNC_SOURCES_NOTE, MAP_SPLIT_NOTE
from src.report.engine_choices import pending_choices_md
from src.report.engineering_report import (
    MAP_SPLIT_DEFINED_ABOVE,
    _app_layer_notes_md,
    _unsupported_pattern_md,
)
from src.report.escaping import md_cell, md_text
from src.report.facts import report_facts
from src.report.renderers import render_engineering_report_md
from src.shared.report_facts import query_group_facts
from tests.report_concision import repeated_paragraphs, repeated_statements, section

FIXTURE = Path(__file__).parent / "fixtures" / "wordpress_model_run_1_60_report.json"


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


def _risks(report: dict[str, Any], rule: str) -> list[dict[str, Any]]:
    return [r for r in report["risk_assessment"]["risks"] if r.get("rule") == rule]


# --- the guard -------------------------------------------------------------------


def test_no_long_statement_is_printed_twice_in_the_judged_report(md: str) -> None:
    assert repeated_paragraphs(md) == []
    assert repeated_statements(md) == []


@pytest.mark.passes_on_main  # a self-test of the guard helpers, not of the report
def test_the_guards_find_what_they_look_for() -> None:
    """A sentence printed as a risk detail and again as a trade-off's bold title is
    one repeat; two rows that differ only in a figure or a code span are not; the
    per-query appendix is a record, not prose, and is not compared."""
    sentence = (
        "Comments and their metadata share one item collection keyed by the comment id and sorted"
    )
    md = (
        f"## Risk register\n\n- RISK-1 · x\n  - {sentence}\n\n"
        f"## Migration trade-offs\n\n- **{sentence}.**: Reading is one Query.\n"
    )
    assert repeated_statements(md) == [(sentence, 2)]
    assert repeated_paragraphs(md) == []
    rows = (
        "- `a.x`: the default is a zero date the target does not accept, so it is dropped here.\n"
        "- `a.y`: the default is a zero date the target does not accept, so it is dropped here.\n"
    )
    assert repeated_paragraphs(rows) == [] and repeated_statements(rows) == []
    line = "- Step 2, version 2 (tie settled): the tie with DocumentDB is settled, the query stays on DynamoDB."
    appendix = f"## Appendix: per-query routing decisions\n\n{line}\n{line}\n"
    assert repeated_paragraphs(appendix) == [] and repeated_statements(appendix) == []
    assert repeated_paragraphs(appendix.replace("Appendix: per-query", "Other")) != []


# --- (1) the risk register refers to the lists other sections show ---------------


def test_the_risk_register_lists_no_trade_off_or_unsupported_pattern_again(
    md: str, saved: dict[str, Any]
) -> None:
    register = section(md, "Risk register")
    trade_offs = section(md, "Migration trade-offs")
    schemas = section(md, "Target schemas by engine")
    details = [
        d
        for rule in ("design.trade_offs", "cache.unsupported")
        for r in _risks(saved, rule)
        for d in r["details"]
    ]
    assert len(details) == 33  # 25 + 5 + 1 trade-offs, 2 unsupported patterns
    for detail in details:
        assert f"  - {md_text(detail)}" not in register.splitlines(), detail[:40]
    # Each one is still in the section the risk names.
    for t in saved["trade_offs"]:
        if t.get("engine") != "reality-check":
            assert md_text(t["description"].strip()) in trade_offs
    assert schemas.count("(query `4481cc0c`).") == 1
    # The other risks keep their details: the redesign's evidence is listed nowhere else.
    redesign = _risks(saved, "design.redesign")[0]
    assert f"  - {md_text(redesign['details'][0])}" in register.splitlines()


def test_an_older_risk_text_gets_a_pointer_naming_the_section(md: str, saved) -> None:
    """The 1.60 run's risks say only "each one is in the engineering report"; in the
    engineering report itself the register names the section that lists them."""
    register = section(md, "Risk register").splitlines()
    pointers = {
        "design.trade_offs": "  - Each one is listed under Migration trade-offs below.",
        "cache.unsupported": (
            "  - Each one is listed under Target schemas by engine (ElastiCache) above."
        ),
    }
    for rule, pointer in pointers.items():
        for risk in _risks(saved, rule):
            assert "engineering report" in risk["description"]
            start = next(i for i, ln in enumerate(register) if f"**{risk['risk_id']}**" in ln)
            entry = register[start + 1 : start + 4]
            assert pointer in entry, (risk["risk_id"], entry)


def test_a_risk_that_names_the_section_needs_no_pointer(report: dict[str, Any]) -> None:
    for risk in _risks(report, "design.trade_offs"):
        risk["description"] = risk["description"].replace(
            "each one is in the engineering report", "listed under Migration trade-offs"
        )
    register = section(render_engineering_report_md(report), "Risk register")
    assert "listed under Migration trade-offs below." not in register
    assert "  - Each one is listed under Target schemas by engine (ElastiCache) above." in register


def test_cache_patterns_the_design_does_not_list_stay_in_the_register(
    report: dict[str, Any],
) -> None:
    """Matched by text, not by count (#1461 review): a design whose unsupported
    patterns are a bare string, or as many but other patterns, does not list the
    risk's patterns, so the register keeps them and every one is printed once."""
    cache = _risks(report, "cache.unsupported")[0]
    design = report["schema_designs"]["elasticache"]
    for shape in (
        "Some other note the designer wrote.",
        [{"reason": f"Another pattern {i}.", "query_ids": ["x" * 64]} for i in range(2)],
    ):
        design["unsupported_patterns"] = shape
        md = render_engineering_report_md(report)
        register = section(md, "Risk register")
        for detail in cache["details"]:
            assert f"  - {md_text(detail)}" in register.splitlines()
            assert md.count(md_text(detail)) == 1
        assert "listed under Target schemas" not in register


def test_partly_listed_trade_offs_keep_the_rest_in_the_register(report: dict[str, Any]) -> None:
    ddb = next(r for r in _risks(report, "design.trade_offs") if "[dynamodb]" in r["description"])
    missing = ddb["details"][0]
    report["trade_offs"] = [
        t
        for t in report["trade_offs"]
        if not (t.get("engine") == "dynamodb" and t["description"].startswith(missing[:40]))
    ]
    register = section(render_engineering_report_md(report), "Risk register").splitlines()
    assert f"  - {md_text(missing)}" in register
    for detail in ddb["details"][1:]:
        assert f"  - {md_text(detail)}" not in register
    assert "  - The others are listed under Migration trade-offs below." in register


def test_details_no_other_section_lists_stay_in_the_register(report: dict[str, Any]) -> None:
    """With no trade-off listed for DynamoDB (an older report) and no completed cache
    design, the register is the only place these texts appear, so it keeps them."""
    report["trade_offs"] = [t for t in report["trade_offs"] if t.get("engine") != "dynamodb"]
    report["schema_designs"]["elasticache"]["status"] = "failed"
    register = section(render_engineering_report_md(report), "Risk register")
    ddb = next(r for r in _risks(report, "design.trade_offs") if "[dynamodb]" in r["description"])
    for detail in ddb["details"]:
        assert f"  - {md_text(detail)}" in register.splitlines()
    for detail in _risks(report, "cache.unsupported")[0]["details"]:
        assert f"  - {md_text(detail)}" in register.splitlines()
    cache = next(
        r for r in _risks(report, "design.trade_offs") if "[elasticache]" in r["description"]
    )
    # ElastiCache's trade-offs are still listed under Migration trade-offs.
    assert f"  - {md_text(cache['details'][0])}" not in register.splitlines()


def test_a_mitigation_stated_above_is_named_by_its_risk(report: dict[str, Any]) -> None:
    risks = _risks(report, "design.trade_offs")
    risks[1]["mitigation"] = risks[0]["mitigation"]
    register = section(render_engineering_report_md(report), "Risk register")
    assert register.count(f"  - Mitigation: {md_text(risks[0]['mitigation'])}") == 1
    # The one printed first states it; the other names that risk.
    first, second = sorted(risks[:2], key=lambda r: register.index(f"**{r['risk_id']}**"))
    assert f"  - Mitigation: as for {first['risk_id']}." in register.splitlines()
    assert f"  - Mitigation: as for {second['risk_id']}." not in register.splitlines()


# --- (2) identical column-default notes share one bullet -------------------------


def test_identical_column_default_notes_collapse_into_one_bullet(md: str, saved) -> None:
    aurora = section(section(md, "Target schemas by engine"), "Aurora MySQL", 3)
    notes = [
        n
        for n in saved["schema_designs"]["aurora_mysql"]["app_layer_notes"]
        if n.get("feature") == "column_default"
    ]
    assert len(notes) == 17
    assert "**Column defaults changed or dropped (17):**" in aurora
    bullets = [ln for ln in aurora.splitlines() if "is a MySQL zero date" in ln]
    assert len(bullets) == 2
    for n in notes:
        assert sum(f"`{n['source_object']}`" in b for b in bullets) == 1, n["source_object"]
    assert all(b.endswith(".") and " Columns: `" in b for b in bullets)


def test_a_column_default_with_its_own_note_keeps_its_line() -> None:
    design = {
        "app_layer_notes": [
            {"feature": "column_default", "source_object": "t.a", "recommendation": "Note one."},
            {"feature": "column_default", "source_object": "t.b", "recommendation": "Note two"},
            {"feature": "column_default", "source_object": "t.c", "recommendation": "Note two"},
        ]
    }
    lines = _app_layer_notes_md(design)
    assert "- `t.a`: Note one." in lines
    assert "- Note two. Columns: `t.b`, `t.c`." in lines
    assert "**Column defaults changed or dropped (3):** set these by hand" in lines[0]


# --- (3) the ownership and served explanation is stated once ---------------------


def test_the_migration_map_refers_to_the_definitions_target_engines_gave(md: str) -> None:
    targets = section(md, "Target engines")
    mapping = section(md, "Migration map")
    assert md_text(KEPT_IN_SYNC_NOTE) in targets and md_text(KEPT_IN_SYNC_SOURCES_NOTE) in targets
    assert MAP_SPLIT_DEFINED_ABOVE in mapping
    assert MAP_SPLIT_NOTE not in mapping
    assert "Sources of truth and replicas below" not in mapping
    assert md.count("“served” is the source tables") == 1


def test_without_the_definition_above_the_map_defines_its_split(report: dict[str, Any]) -> None:
    """When Target engines defines no kept-in-sync split (no migration target serves a
    table it does not move), the Migration map states its own definition."""
    for e in report["facts"]["engines"]:
        e["kept_in_sync_tables"], e["moves_to_tables"] = 0, {}
    md = render_engineering_report_md(report)
    assert md_text(KEPT_IN_SYNC_NOTE) not in section(md, "Target engines")
    mapping = section(md, "Migration map")
    assert MAP_SPLIT_NOTE in mapping and MAP_SPLIT_DEFINED_ABOVE not in mapping
    assert "Sources of truth and replicas below lists each written table" in mapping


# --- (4) machine-sounding fragments ----------------------------------------------


def test_an_unsupported_pattern_states_its_description_before_its_query() -> None:
    u = {"query_ids": ["4481cc0c" + "0" * 56], "reason": "No stable key.", "workaround": ""}
    assert _unsupported_pattern_md(u).startswith("No stable key")
    assert _unsupported_pattern_md(u).endswith(" (query `4481cc0c`).")
    two = {**u, "query_ids": ["a" * 64, "b" * 64]}
    assert _unsupported_pattern_md(two).endswith(" (queries `aaaaaaaa`, `bbbbbbbb`).")
    assert _unsupported_pattern_md({"query_ids": ["c" * 64]}) == "Query `cccccccc`"
    five = {**u, "query_ids": [c * 64 for c in "abcde"]}
    assert _unsupported_pattern_md(five).endswith(
        " (queries `aaaaaaaa`, `bbbbbbbb`, `cccccccc` and 2 more)."
    )


def test_no_machine_fragment_survives(md: str) -> None:
    assert "plus 18 Aurora groups below" not in md
    assert "Served counts" not in md
    assert not [
        ln
        for ln in section(md, "Target schemas by engine").splitlines()
        if ln.startswith("- Query `")
    ]


# --- (5) every count from one fact, labelled once --------------------------------


def test_the_query_groups_heading_and_pointer_add_up(md: str, saved) -> None:
    counts = report_facts(copy.deepcopy(saved))["query_group_counts"]
    assert {k: counts[k] for k in ("group_count", "listed_group_count")} == {
        "group_count": 51,
        "listed_group_count": 33,
    }
    assert counts["relational_only_group_count"] == 18
    assert counts["relational_only_engines"] == ["aurora_mysql"]
    assert len(counts["listed_group_indexes"]) == counts["listed_group_count"]
    groups = section(md, "Query groups")
    assert "## Query groups (33 of 51)" in md.splitlines()
    assert "The other 18 groups run only on Aurora MySQL; Queries on Aurora by table" in groups
    rows = [
        ln
        for ln in groups.splitlines()
        if ln.startswith("| ") and not ln.startswith("| Group | Engines |")
    ]
    # The rows are the facts' listed groups, in their order.
    names = [saved["query_groups"][i]["group_name"] for i in counts["listed_group_indexes"]]
    assert [r.split(" | ", 1)[0].removeprefix("| ") for r in rows] == [md_cell(n) for n in names]


def test_the_table_rows_follow_the_facts_list(report: dict[str, Any]) -> None:
    """The heading and the rows read one fact: drop a group from its listed indexes
    and its row goes too."""
    facts = report_facts(report)
    counts = facts["query_group_counts"]
    dropped = counts["listed_group_indexes"].pop(0)
    report["facts"] = facts
    groups = section(render_engineering_report_md(report), "Query groups")
    name = md_cell(report["query_groups"][dropped]["group_name"])
    assert not [ln for ln in groups.splitlines() if ln.startswith(f"| {name} |")]


def test_queries_on_aurora_by_table_are_the_facts_rows(md: str, saved) -> None:
    counts = report_facts(copy.deepcopy(saved))["query_group_counts"]
    rows = counts["relational_rows"]
    assert counts["relational_query_counts"] == {"aurora_mysql": 67}
    assert sum(r["query_count"] for r in rows) == 67
    body = section(md, "Queries on Aurora by table")
    assert "### Aurora MySQL (67 queries)" in body
    table = [
        ln
        for ln in body.splitlines()
        if ln.startswith("| ") and not ln.startswith("| Table | Queries |")
    ]
    assert len(table) == len(rows) == 27
    for line, row in zip(table, rows, strict=True):
        name = saved["query_groups"][row["group_index"]]["group_name"]
        assert line.startswith(f"| {md_cell(name)} | {row['query_count']} | ")
    # The two blame groups come last.
    assert table[-1].startswith("| Utility and session statements |")
    assert table[-2].startswith("| Table not identified by the collector |")


def test_query_group_facts_cover_every_group() -> None:
    assert query_group_facts({}) is None
    assert query_group_facts({"query_groups": []}) == {
        "group_count": 0,
        "listed_group_count": 0,
        "relational_only_group_count": 0,
        "relational_only_engines": [],
        "listed_group_indexes": [],
        "relational_rows": [],
        "relational_query_counts": {},
    }
    pg = "aurora_postgresql"

    def ap(qids: list[str], rps: float, engine: str = pg) -> dict[str, Any]:
        return {"engine": engine, "query_ids": qids, "design_rps": rps}

    facts = query_group_facts(
        {
            "query_groups": [
                {"group_name": "t1", "engines": [pg], "access_patterns": [ap(["a", "b"], 1.0)]},
                "not a group",
                {
                    "group_name": "t2",
                    "engines": [pg, "elasticache"],
                    "access_patterns": [
                        ap(["c"], 2.5),
                        ap(["c"], 0.5),
                        ap(["d"], 9, "elasticache"),
                    ],
                },
                {
                    "group_name": "Utility and session statements",
                    "engines": [pg],
                    "access_patterns": [ap(["u"], 50.0)],
                },
                {"engines": []},
            ]
        }
    )
    assert facts == {
        "group_count": 4,
        "listed_group_count": 1,
        "relational_only_group_count": 3,
        "relational_only_engines": [pg],
        "listed_group_indexes": [2],
        # Busiest first by the engine's own calls/s; the utility group last.
        "relational_rows": [
            {"engine": pg, "group_index": 2, "query_count": 1, "calls_per_second": 3.0},
            {"engine": pg, "group_index": 0, "query_count": 2, "calls_per_second": 1.0},
            {"engine": pg, "group_index": 3, "query_count": 1, "calls_per_second": 50.0},
        ],
        "relational_query_counts": {pg: 4},
    }


@pytest.mark.passes_on_main  # guards main's single-count heading when nothing is left out
def test_with_every_group_listed_the_heading_has_one_count(report: dict[str, Any]) -> None:
    report["query_groups"] = [
        g for g in report["query_groups"] if set(g.get("engines") or []) - {"aurora_mysql"}
    ]
    report["facts"].pop("query_group_counts", None)
    md = render_engineering_report_md(report)
    assert "## Query groups (33)" in md.splitlines()
    assert "run only on" not in section(md, "Query groups")


def test_resolved_risks_sharing_a_reason_state_it_once(md: str, saved) -> None:
    resolved = section(md, "Resolved by the assignment")
    reason = "the Aurora MySQL analysis recommended DynamoDB for these queries and the assignment moved them there"
    assert resolved.count(f"Resolved because {md_text(reason)}.") == 1
    assert resolved.count("  - Resolved for the same reason as the risk above.") == 2
    entries = [ln for ln in resolved.splitlines() if ln.startswith("- ")]
    assert len(entries) == report_facts(copy.deepcopy(saved))["resolved_risk_count"] == 5
    # The three sharing the reason are listed together, the first naming it.
    first = next(
        i
        for i, ln in enumerate(resolved.splitlines())
        if ln.startswith("  - Resolved because the Aurora MySQL")
    )
    following = resolved.splitlines()[first:]
    assert [ln for ln in following if ln.startswith("  - Resolved")][:3] == [
        f"  - Resolved because {md_text(reason)}.",
        "  - Resolved for the same reason as the risk above.",
        "  - Resolved for the same reason as the risk above.",
    ]


# --- pending engine choices ------------------------------------------------------


def _choice(table: str, why: str, low: str, decide: str) -> dict[str, Any]:
    return {
        "table": f"db.{table}",
        "query_ids": [f"q-{table}"],
        "why_tied": why,
        "options": [
            {
                "engine": "documentdb",
                "score": 78,
                "trade_off": f"Serves 166 settled queries. {low}",
            },
            {"engine": "dynamodb", "score": 78, "trade_off": "Serves 682 settled queries."},
        ],
        "decide_for_me_engine": "dynamodb",
        "decide_for_me_reason": decide,
        "current_engine": "dynamodb",
    }


def test_a_pending_choice_worded_as_an_earlier_one_refers_to_it(report: dict[str, Any]) -> None:
    report["pending_engine_choices"] = [
        _choice("a", "Both score 78.", "Lower by 0.05.", "DynamoDB by 0.05."),
        _choice("b", "Both score 78.", "Lower by 0.05.", "DynamoDB by 0.05."),
        _choice("c", "Both score 75.", "Lower by 0.05.", "DynamoDB by 0.25."),
    ]
    report["facts"].update(pending_choice_count=3, pending_choice_query_count=3)
    lines = pending_choices_md(report)
    text = "\n".join(lines)
    assert "The same tie and options as `a`." in lines
    assert text.count("Serves 682 settled queries.") == 1
    # The third differs in its tie and pick; its matching options refer back.
    assert "- DocumentDB: as for `a`." in lines and "- DynamoDB: as for `a`." in lines
    assert "Both score 75." in lines and "- Decide for me: DynamoDB by 0.25." in lines
    # Every table keeps its heading and its queries.
    for t in "abc":
        assert f"- Queries: `q-{t}`" in lines
        assert any(ln.startswith(f"### `{t}` (1 query)") for ln in lines)


def test_the_blame_group_labels_match_synthesis() -> None:
    """The facts layer orders "Queries on Aurora by table" by synthesis's two labels;
    a renamed label would silently put a blame group among the busiest tables."""
    from src.agents.referee import synthesis_report
    from src.shared import report_facts

    assert report_facts.UTILITY_GROUP_LABEL == synthesis_report.UTILITY_GROUP_LABEL
    assert (
        report_facts.UNRESOLVED_TABLE_GROUP_LABEL == synthesis_report.UNRESOLVED_TABLE_GROUP_LABEL
    )
