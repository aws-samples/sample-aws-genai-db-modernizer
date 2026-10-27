"""A wave's gate as structured parts (#1028).

The results page collapses each table list in a wave's gate to an inline
"(…)" that expands in place. It must not parse the gate's prose to find those
lists, so the wave builder emits ``gate_parts`` next to ``gate``: each part is
a run of text followed by an optional item list. Joined back (text, then
" (a, b, c)" when there are items), the parts are exactly ``gate``, which the
Markdown and PDF renderers keep printing unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.agents.referee.migration_wave_tables import (
    _table_group_coverage_note,
    _table_group_coverage_parts,
    gate_parts_text,
)
from src.contracts.synthesis_output import MigrationWave, SynthesisOutputContract
from src.report.engineering_report import render_engineering_report_md
from src.report.escaping import md_text
from tests.unit.agents.referee.test_migration_waves import (
    KNOWN_TABLES,
    QUERY_ASSIGNMENTS,
    RANKING,
    TABLE_ASSIGNMENTS,
    _build,
)

SAVED_REPORT = (
    Path(__file__).resolve().parents[2]
    / "report"
    / "fixtures"
    / "wordpress_no_schema_design_report.json"
)


def _items(wave):
    return [p["items"] for p in wave["gate_parts"] if p["items"]]


def test_every_wave_carries_parts_that_join_back_to_its_gate():
    for wave in _build():
        assert wave["gate_parts"], wave["wave"]
        assert gate_parts_text(wave["gate_parts"]) == wave["gate"], wave["wave"]


def test_a_gate_with_no_list_is_one_text_part():
    aurora = _build()[0]
    assert aurora["gate_parts"] == [{"text": aurora["gate"], "items": []}]


def test_the_forward_dual_read_list_is_its_own_items_not_text():
    query_assignments = [
        *QUERY_ASSIGNMENTS,
        {"query_id": "q5", "assigned_engine": "aurora_mysql", "source_tables": ["wp_posts"]},
    ]
    wave = _build(query_assignments=query_assignments)[2]
    assert gate_parts_text(wave["gate_parts"]) == wave["gate"]
    lists = _items(wave)
    assert ["wp_posts"] in lists
    # The part before a list ends with its count and noun; no part's text
    # repeats the list in parentheses.
    before = next(p for p in wave["gate_parts"] if p["items"] == ["wp_posts"])
    assert before["text"].endswith("tables moving to DynamoDB")
    assert "(wp_posts)" not in before["text"]
    assert all("(wp_posts" not in p["text"] for p in wave["gate_parts"])


def test_the_reverse_dual_read_list_is_its_own_items():
    query_assignments = [
        *QUERY_ASSIGNMENTS,
        {"query_id": "q8", "assigned_engine": "dynamodb", "source_tables": ["wp_options"]},
    ]
    table_assignments = [
        t if t["table_id"] != "wp_options" else {**t, "engines": ["aurora_mysql", "dynamodb"]}
        for t in TABLE_ASSIGNMENTS
    ]
    wave = _build(table_assignments=table_assignments, query_assignments=query_assignments)[2]
    assert gate_parts_text(wave["gate_parts"]) == wave["gate"]
    before = next(p for p in wave["gate_parts"] if p["items"] == ["wp_options"])
    assert before["text"].endswith("1 query read 1 table")
    after = wave["gate_parts"][wave["gate_parts"].index(before) + 1]
    assert after["text"].startswith(" Aurora MySQL owns (wave 1) and keeps owning")


def test_the_merged_dynamodb_documentdb_wave_joins_back_to_its_gate():
    ranking = [*RANKING, {"target": "documentdb", "assigned_queries": 2, "workload_percent": 1.0}]
    table_assignments = [
        *TABLE_ASSIGNMENTS,
        {
            "table_id": "wp_docs",
            "primary_engine": "documentdb",
            "engines": ["documentdb"],
            "query_count": 2,
        },
    ]
    query_assignments = [
        *QUERY_ASSIGNMENTS,
        {"query_id": "q5", "assigned_engine": "aurora_mysql", "source_tables": ["wp_posts"]},
    ]
    waves = _build(
        ranking=ranking,
        table_assignments=table_assignments,
        query_assignments=query_assignments,
        known_tables=KNOWN_TABLES | {"wp_docs"},
    )
    merged = waves[2]
    assert merged["engines"] == ["dynamodb", "documentdb"]
    assert gate_parts_text(merged["gate_parts"]) == merged["gate"]
    assert ["wp_posts"] in _items(merged)
    assert merged["gate_parts"][0]["text"].startswith("DynamoDB: ")


@pytest.mark.parametrize(
    "parts, text",
    [
        ([], ""),
        ([{"text": "Parity.", "items": []}], "Parity."),
        (
            [{"text": "Check. 2 tables", "items": ["a", "b"]}, {"text": " stay.", "items": []}],
            "Check. 2 tables (a, b) stay.",
        ),
    ],
)
def test_gate_parts_text_joins_text_then_the_parenthesised_list(parts, text):
    assert gate_parts_text(parts) == text


def test_the_contract_takes_gate_parts_and_still_loads_a_wave_without_them():
    base = {
        "wave": 1,
        "title": "Move",
        "engines": ["aurora_mysql"],
        "table_count": 2,
        "query_count": 3,
        "workload_share_percent": 50.0,
        "rationale": "Why.",
        "gate": "Check. 2 tables (a, b) stay.",
    }
    legacy = MigrationWave.model_validate(base)
    assert legacy.gate_parts is None
    new = MigrationWave.model_validate(
        {
            **base,
            "gate_parts": [
                {"text": "Check. 2 tables", "items": ["a", "b"]},
                {"text": " stay.", "items": []},
            ],
        }
    )
    assert gate_parts_text([p.model_dump() for p in new.gate_parts]) == base["gate"]


_WAVE = {
    "wave": 3,
    "title": "Move",
    "engines": ["dynamodb"],
    "table_count": 2,
    "query_count": 3,
    "workload_share_percent": 50.0,
    "rationale": "Why.",
    "gate": "Check. 2 tables (a, b) stay.",
}


@pytest.mark.parametrize(
    "parts",
    [
        [{"text": "Check. 2 tables", "items": ["a"]}, {"text": " stay.", "items": []}],
        [{"text": "Check. 2 tables (a, b) stay", "items": []}],
        [{"text": "Something else.", "items": []}],
    ],
)
def test_the_contract_rejects_parts_that_do_not_join_back_to_the_gate(parts):
    with pytest.raises(ValidationError, match="gate_parts must join back to gate"):
        MigrationWave.model_validate({**_WAVE, "gate_parts": parts})


def test_the_contract_accepts_empty_or_absent_parts_only_as_it_should():
    assert MigrationWave.model_validate({**_WAVE, "gate_parts": None}).gate_parts is None
    with pytest.raises(ValidationError):
        MigrationWave.model_validate({**_WAVE, "gate_parts": []})


def test_a_saved_report_written_before_gate_parts_still_loads_and_renders_its_gates():
    """A real wordpress report saved before #1028 (contract 1.7, no gate_parts):
    it validates, its waves carry no parts, and the engineering report prints
    each gate exactly as saved."""
    report = json.loads(SAVED_REPORT.read_text())
    waves = report["migration_waves"]
    assert waves and all("gate_parts" not in w for w in waves)
    contract = SynthesisOutputContract.model_validate(report)
    assert [w.gate_parts for w in contract.migration_waves] == [None] * len(waves)
    md = render_engineering_report_md(report)
    for w in waves:
        assert f"- Gate before the next wave: {md_text(w['gate'])}" in md, w["wave"]


def test_tables_kept_in_sync_from_two_owners_state_the_sync_once():
    """#1170: the reverse clauses for two owners name each owner's tables, then
    say once that this wave's engine keeps a copy of all of them in sync."""
    synced = [
        {"table": "t_a", "owner": "aurora_postgresql", "sync": "CDC"},
        {"table": "t_d", "owner": "documentdb", "sync": "CDC"},
    ]
    queries = [
        {"query_id": "q1", "assigned_engine": "dynamodb", "source_tables": ["t_a"]},
        {"query_id": "q2", "assigned_engine": "dynamodb", "source_tables": ["t_d"]},
    ]
    note = _table_group_coverage_note(
        {"t_own"}, synced, queries, "dynamodb", {"aurora_postgresql": 1, "documentdb": 3}, 3
    )
    assert note == (
        " 1 query read 1 table (t_a) Aurora PostgreSQL owns (wave 1) and keeps owning, and "
        "1 query read 1 table (t_d) DocumentDB owns (wave 3) and keeps owning; keep a "
        "DynamoDB copy of all of them in sync via CDC."
    )
    one = _table_group_coverage_note(
        {"t_own"}, synced[:1], queries[:1], "dynamodb", {"aurora_postgresql": 1}, 3
    )
    assert one.endswith("and keeps owning; keep a DynamoDB copy of them in sync via CDC.")


def test_dual_read_count_says_it_is_a_subset_of_the_tables_the_wave_moves():
    """#551: "3 tables stay dual-read" next to a headline of 5 moving tables read as
    a conflict. The gate names the whole (the moving tables) and says what happens to
    the rest."""
    queries = [
        {"query_id": "q1", "assigned_engine": "aurora_mysql", "source_tables": ["t_a", "t_b"]},
        {"query_id": "q2", "assigned_engine": "aurora_mysql", "source_tables": ["t_c"]},
        {"query_id": "q3", "assigned_engine": "dynamodb", "source_tables": ["t_d", "t_e"]},
    ]
    owned = {"t_a", "t_b", "t_c", "t_d", "t_e"}
    parts = _table_group_coverage_parts(owned, [], queries, "dynamodb", {"aurora_mysql": 1}, 3)
    assert [(p["text"], p["items"]) for p in parts if p["text"] != " "] == [
        (" 3 of the 5 tables moving to DynamoDB", ["t_a", "t_b", "t_c"]),
        (
            " stay dual-read by Aurora MySQL queries, which remain on Aurora MySQL; DynamoDB "
            "owns them, so keep the Aurora MySQL copy in sync from DynamoDB via CDC. The other 2 "
            "tables",
            ["t_d", "t_e"],
        ),
        (" move with no further Aurora MySQL reads.", []),
    ]


def test_every_moving_table_dual_read_says_all_and_names_no_remainder():
    queries = [
        {"query_id": "q1", "assigned_engine": "aurora_mysql", "source_tables": ["t_a", "t_b"]}
    ]
    note = _table_group_coverage_note(
        {"t_a", "t_b"}, [], queries, "dynamodb", {"aurora_mysql": 1, "dynamodb": 3}, 3
    )
    assert note.startswith(" All 2 tables moving to DynamoDB (t_a, t_b) stay dual-read by Aurora")
    assert "The other" not in note


def test_one_dual_read_table_of_several_uses_singular_wording():
    queries = [{"query_id": "q1", "assigned_engine": "aurora_mysql", "source_tables": ["t_a"]}]
    note = _table_group_coverage_note(
        {"t_a", "t_b"}, [], queries, "dynamodb", {"aurora_mysql": 1}, 3
    )
    assert note.startswith(" 1 of the 2 tables moving to DynamoDB (t_a) stays dual-read")
    assert note.endswith(" The other table (t_b) moves with no further Aurora MySQL reads.")


def test_the_remainder_sentence_follows_every_dual_read_clause_once():
    """A future and a permanent engine both read some tables: the remainder is the
    tables neither reads, stated once after both clauses."""
    queries = [
        {"query_id": "q1", "assigned_engine": "aurora_mysql", "source_tables": ["t_a"]},
        {"query_id": "q2", "assigned_engine": "opensearch", "source_tables": ["t_b"]},
    ]
    note = _table_group_coverage_note(
        {"t_a", "t_b", "t_c"}, [], queries, "dynamodb", {"aurora_mysql": 1, "opensearch": 4}, 3
    )
    assert note.count("The other") == 1
    assert note.count("1 of the 3 tables moving to DynamoDB") == 2
    assert note.endswith(
        " The other table (t_c) moves with no further Aurora MySQL or OpenSearch reads."
    )


def test_the_gate_whole_is_the_facts_moving_table_count_and_the_remainder_is_named():
    """#551: the "of the N" in the gate is the headline's moving count (facts), so the
    card's two figures cannot drift apart."""
    from src.shared.report_facts import wave_facts

    query_assignments = [
        *QUERY_ASSIGNMENTS,
        {"query_id": "q5", "assigned_engine": "aurora_mysql", "source_tables": ["wp_posts"]},
    ]
    wave = _build(query_assignments=query_assignments)[2]
    moving = wave_facts(wave)["moving_tables"]
    assert moving and moving > 1
    assert f"1 of the {moving} tables moving to DynamoDB" in wave["gate"]
    assert "The other" in wave["gate"] and "moves with no further" in wave["gate"]


def test_a_merged_wave_states_each_engines_own_moving_count():
    """#551: in a DynamoDB/DocumentDB wave the "of the N" is that engine's own count,
    the one `facts.waves[].moving_by_engine` carries, not the wave's total."""
    from src.shared.report_facts import wave_facts

    ranking = [*RANKING, {"target": "documentdb", "assigned_queries": 2, "workload_percent": 1.0}]
    table_assignments = [
        *TABLE_ASSIGNMENTS,
        *(
            {"table_id": t, "primary_engine": "documentdb", "engines": ["documentdb"]}
            for t in ("wp_docs", "wp_docs2")
        ),
    ]
    query_assignments = [
        *QUERY_ASSIGNMENTS,
        {"query_id": "q5", "assigned_engine": "aurora_mysql", "source_tables": ["wp_posts"]},
        {"query_id": "q6", "assigned_engine": "aurora_mysql", "source_tables": ["wp_docs"]},
        {"query_id": "q7", "assigned_engine": "documentdb", "source_tables": ["wp_docs2"]},
    ]
    wave = _build(
        ranking=ranking,
        table_assignments=table_assignments,
        query_assignments=query_assignments,
        known_tables=KNOWN_TABLES | {"wp_docs", "wp_docs2"},
    )[2]
    by_engine = wave_facts(wave)["moving_by_engine"]
    assert by_engine["dynamodb"] != by_engine["documentdb"]
    assert f"1 of the {by_engine['dynamodb']} tables moving to DynamoDB" in wave["gate"]
    assert f"1 of the {by_engine['documentdb']} tables moving to DocumentDB" in wave["gate"]


def test_a_long_remainder_is_counted_not_listed():
    """A wave that moves many tables with few dual reads (discourse: 82 left over)
    states the count of the others and does not repeat every name."""
    owned = {f"t_{i:02d}" for i in range(12)}
    queries = [{"query_id": "q1", "assigned_engine": "aurora_mysql", "source_tables": ["t_00"]}]
    parts = _table_group_coverage_parts(owned, [], queries, "dynamodb", {"aurora_mysql": 1}, 3)
    text = gate_parts_text(parts)
    assert "1 of the 12 tables moving to DynamoDB" in text
    assert "The other 11 tables move with no further Aurora MySQL reads." in text
    assert all(len(p["items"]) <= 1 for p in parts)
    few = _table_group_coverage_note(
        {"a", "b", "c"},
        [],
        [{**queries[0], "source_tables": ["a"]}],
        "dynamodb",
        {"aurora_mysql": 1},
        3,
    )
    assert "The other 2 tables (b, c) move with" in few
