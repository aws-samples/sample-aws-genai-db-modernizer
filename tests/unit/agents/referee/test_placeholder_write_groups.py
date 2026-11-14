# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#1817: a placeholder or dummy table is never a written or owned table.

The collector names "no table" in several spellings (``unknown``, ``UNKNOWN``,
``None``, ``null``) and a statement with no ``FROM`` names a dummy table (``DUAL``,
``SYSIBM.SYSDUMMY1``). None of them is a source table, so none forms or joins a
write group (ADR-031 Amendment 9) or a table ownership row. The one list is
``placeholder_tables.PLACEHOLDER_TABLE_NAMES`` (re-exported by ``report_facts``),
matched exactly, plus the empty string: a real table named ``none``, ``Null`` or
``Unknown`` is never dropped.
"""

from __future__ import annotations

import pytest

from src.agents.referee.routed_confidence import routed_fits
from src.agents.referee.table_assignments import derive_table_assignments
from src.agents.referee.table_resolution import TableNameResolver
from src.agents.referee.write_groups import (
    assignment_placement_groups,
    assignment_sources_of_truth,
    assignment_unit_weights,
    build_write_groups,
    collector_write_groups,
    inserted_tables,
    placement_groups,
    table_sources,
    written_tables,
)
from src.contracts.assignment_models import QueryAssignment
from src.shared.report_facts import PLACEHOLDER_TABLE_NAMES
from tests.unit.agents.referee.test_one_writer_per_table import _q, _resolve

# The spellings main did not skip; main already skipped lowercase ``unknown`` and the
# empty string, which the ``passes_on_main`` tests below pin.
PLACEHOLDERS = ["UNKNOWN", "None", "null", "DUAL", "dual", "SYSIBM.SYSDUMMY1"]
ALREADY_SKIPPED = ["unknown", ""]
# Real tables whose names only differ from a placeholder in case (#1817 review).
REAL_LOOKALIKES = ["none", "NONE", "Null", "NULL", "Unknown", "Dual", "sysibm.sysdummy1"]


def is_placeholder_table(name: object) -> bool:
    # Imported here, not at the top, so the rest of this file runs on code without it.
    from src.shared.report_facts import is_placeholder_table as shared

    return shared(name)


class TestIsPlaceholderTable:
    @pytest.mark.parametrize("name", PLACEHOLDERS + ALREADY_SKIPPED + [" "])
    def test_every_spelling_of_no_table_is_a_placeholder(self, name: str) -> None:
        assert is_placeholder_table(name)

    def test_a_missing_name_is_a_placeholder(self) -> None:
        assert is_placeholder_table(None)

    @pytest.mark.parametrize(
        "name", ["db.a", "orders", "unknown_orders", "dual_ledger", "nulls"] + REAL_LOOKALIKES
    )
    def test_a_real_table_is_not(self, name: str) -> None:
        assert not is_placeholder_table(name)

    def test_report_facts_re_exports_the_one_list(self) -> None:
        from src.shared import placeholder_tables, report_facts

        assert report_facts.PLACEHOLDER_TABLE_NAMES is placeholder_tables.PLACEHOLDER_TABLE_NAMES
        assert report_facts.is_placeholder_table is placeholder_tables.is_placeholder_table

    def test_the_shared_list_is_the_one_it_reads(self) -> None:
        assert all(is_placeholder_table(n) for n in PLACEHOLDER_TABLE_NAMES)


class TestWrittenTables:
    @pytest.mark.parametrize("placeholder", PLACEHOLDERS)
    def test_a_write_on_a_placeholder_writes_no_table(self, placeholder: str) -> None:
        q = _q("w1", "UPDATE mystery SET x = $1 WHERE id = $2", [placeholder])
        assert written_tables(q, "postgresql") == []

    @pytest.mark.parametrize("placeholder", PLACEHOLDERS)
    def test_a_write_with_no_text_keeps_only_its_real_tables(self, placeholder: str) -> None:
        q = {"query_id": "w1", "query_type": "UPDATE", "tables_accessed": [placeholder, "db.a"]}
        assert written_tables(q) == ["db.a"]

    @pytest.mark.parametrize("placeholder", ["DUAL", "dual", "SYSIBM.SYSDUMMY1"])
    def test_a_locking_read_on_a_dummy_table_locks_nothing(self, placeholder: str) -> None:
        q = _q("r1", f"SELECT 1 FROM {placeholder} FOR UPDATE", [placeholder], "SELECT")
        assert written_tables(q, "oracle") == []

    def test_a_locking_read_keeps_its_real_tables(self) -> None:
        q = _q("r1", "SELECT * FROM a WHERE id = $1 FOR UPDATE", ["db.a", "UNKNOWN"])
        assert written_tables(q, "postgresql") == ["db.a"]

    @pytest.mark.passes_on_main  # main never skipped these; the exact match must not either
    @pytest.mark.parametrize("name", REAL_LOOKALIKES)
    def test_a_write_on_a_real_lookalike_table_still_writes_it(self, name: str) -> None:
        q = {"query_id": "w1", "query_type": "UPDATE", "tables_accessed": [name]}
        assert written_tables(q) == [name]

    @pytest.mark.passes_on_main  # main already skipped these two; pinned as regressions
    @pytest.mark.parametrize("placeholder", ALREADY_SKIPPED)
    def test_the_spellings_main_already_skipped_still_write_no_table(self, placeholder) -> None:
        q = _q("w1", "UPDATE mystery SET x = $1 WHERE id = $2", [placeholder])
        assert written_tables(q, "postgresql") == []
        q = {"query_id": "w1", "query_type": "UPDATE", "tables_accessed": [placeholder, "db.a"]}
        assert written_tables(q) == ["db.a"]

    def test_an_insert_on_a_placeholder_appends_to_no_table(self) -> None:
        q = {"query_id": "w1", "query_type": "INSERT", "tables_accessed": ["UNKNOWN"]}
        assert inserted_tables(q) == []


TWO_UNRELATED_WRITES = [
    {"query_id": "w1", "query_type": "UPDATE", "tables_accessed": ["UNKNOWN"]},
    {"query_id": "w2", "query_type": "DELETE", "tables_accessed": ["UNKNOWN"]},
]


class TestWriteGroups:
    def test_two_unrelated_writes_on_unknown_form_no_write_group(self) -> None:
        groups = build_write_groups(TWO_UNRELATED_WRITES)
        assert groups == {}
        assert placement_groups([], groups) == []

    @pytest.mark.parametrize("placeholder", PLACEHOLDERS)
    def test_no_write_group_is_keyed_on_a_placeholder(self, placeholder: str) -> None:
        queries = [
            {"query_id": "w1", "query_type": "UPDATE", "tables_accessed": [placeholder, "db.a"]},
            {"query_id": "w2", "query_type": "UPDATE", "tables_accessed": [placeholder, "db.b"]},
        ]
        assert build_write_groups(queries) == {"db.a": ["w1"], "db.b": ["w2"]}

    @pytest.mark.passes_on_main  # main already skipped these two; pinned as regressions
    @pytest.mark.parametrize("placeholder", ALREADY_SKIPPED)
    def test_the_spellings_main_already_skipped_key_no_group(self, placeholder: str) -> None:
        queries = [
            {"query_id": "w1", "query_type": "UPDATE", "tables_accessed": [placeholder, "db.a"]},
            {"query_id": "w2", "query_type": "UPDATE", "tables_accessed": [placeholder, "db.b"]},
        ]
        assert build_write_groups(queries) == {"db.a": ["w1"], "db.b": ["w2"]}

    def test_a_collection_builds_no_placeholder_group(self) -> None:
        collector = {
            "metadata": {"source_database": {"engine": "oracle"}},
            "queries": {
                "query_patterns": [
                    _q("r1", "SELECT 1 FROM DUAL FOR UPDATE", ["DUAL"], "SELECT"),
                    _q("r2", "SELECT 2 FROM dual FOR UPDATE", ["dual"], "SELECT"),
                    _q("w1", "UPDATE a SET x = :1", ["APP.A"]),
                ]
            },
        }
        assert collector_write_groups(collector) == {"APP.A": ["w1"]}


# An assignment saved before #1817 can carry a write group keyed on a placeholder.
LEGACY = {
    "co_dependency_groups": [],
    "write_groups": {"UNKNOWN": ["w1", "w2"], "db.a": ["w3", "w4"]},
    "query_assignments": [
        {"query_id": q, "assigned_engine": e, "source_tables": t}
        for q, e, t in (
            ("w1", "dynamodb", ["UNKNOWN"]),
            ("w2", "dynamodb", ["UNKNOWN"]),
            ("w3", "aurora_postgresql", ["db.a"]),
            ("w4", "aurora_postgresql", ["db.a"]),
        )
    ],
}


class TestASavedPlaceholderGroup:
    def test_the_filter_drops_placeholder_keys_only(self) -> None:
        from src.agents.referee.write_groups import real_write_groups

        assert real_write_groups(LEGACY["write_groups"]) == {"db.a": ["w3", "w4"]}  # type: ignore[arg-type]
        assert real_write_groups(None) == {}

    def test_its_placement_units_ignore_the_placeholder(self) -> None:
        assert assignment_placement_groups(LEGACY) == [["w3", "w4"]]

    def test_its_sources_of_truth_name_real_tables_only(self) -> None:
        assert assignment_sources_of_truth(LEGACY) == {"db.a": "aurora_postgresql"}

    def test_its_unit_weights_cover_real_units_only(self) -> None:
        rates = {q: {"calls_per_second": 1.0} for q in ("w1", "w2", "w3", "w4")}
        assert set(assignment_unit_weights(LEGACY, rates)) == {"w3", "w4"}

    def test_its_table_sources_name_real_tables_only(self) -> None:
        assert [row["table"] for row in table_sources(LEGACY)] == ["db.a"]


class TestTheFirstAssignment:
    def test_two_unrelated_writes_on_unknown_keep_their_own_engines(self, monkeypatch) -> None:
        """On main both writes formed one write group keyed on ``UNKNOWN`` and were
        placed as one unit; each now takes its own best engine."""
        scores = {
            ("w1", "dynamodb"): 90.0,
            ("w1", "aurora_postgresql"): 40.0,
            ("w2", "dynamodb"): 40.0,
            ("w2", "aurora_postgresql"): 90.0,
        }
        result, _ = _resolve(monkeypatch, TWO_UNRELATED_WRITES, scores)
        engines = {qa.query_id: qa.assigned_engine for qa in result.query_assignments}
        assert engines == {"w1": "dynamodb", "w2": "aurora_postgresql"}
        assert result.write_groups == {}


class TestOwnership:
    @pytest.mark.parametrize("placeholder", ["UNKNOWN", "None", "null", "dual"])
    def test_a_placeholder_is_no_table_ownership_row(self, placeholder: str) -> None:
        qas = [
            QueryAssignment(
                query_id="q1",
                assigned_engine="dynamodb",
                confidence=80,
                source_tables=[placeholder, "db.a"],
                assignment_reason="t",
            )
        ]
        tables, _ = derive_table_assignments(qas)
        assert [t.table_id for t in tables] == ["db.a"]
        # Resolved against the schema, a placeholder is dropped, never reported as
        # an unresolved name.
        known = TableNameResolver.from_known_ids(["db.a"])
        tables, unresolved = derive_table_assignments(qas, known_tables=known)
        assert [t.table_id for t in tables] == ["db.a"]
        assert unresolved.count == 0


class TestRoutedConfidence:
    def test_placeholders_never_count_as_routed_tables(self) -> None:
        queries = [
            {"query_id": "k1", "query_type": "SELECT", "tables_accessed": ["users", "UNKNOWN"]},
            {"query_id": "k2", "query_type": "SELECT", "tables_accessed": ["None", "DUAL"]},
        ]
        assignment = {
            "version": 2,
            "query_assignments": [
                {"query_id": "k1", "assigned_engine": "dynamodb"},
                {"query_id": "k2", "assigned_engine": "dynamodb"},
            ],
        }
        analysis = {
            "dynamodb": {"table_recommendations": [{"table_id": "users", "confidence_score": 90}]}
        }
        fit = routed_fits(assignment, {"signals": []}, queries, analysis)["dynamodb"]
        assert fit.tables == 1


class TestTheSynthesisReport:
    def test_a_broad_gap_never_starts_with_a_dummy_table(self) -> None:
        from src.agents.referee.synthesis_report import BROAD_FLAGGED_QUERIES, _start_with

        ids = [f"q{i}" for i in range(BROAD_FLAGGED_QUERIES + 1)]
        tables = {q: {"DUAL", "unknown", "null", "db.a"} for q in ids}
        sentence = _start_with(ids, tables)
        assert "db.a" in sentence
        for placeholder in ("DUAL", "unknown", "null"):
            assert placeholder not in sentence

    def test_a_coverage_gap_names_no_placeholder_table(self) -> None:
        from src.agents.referee.synthesis_report import _coverage_gap_risk

        risk = _coverage_gap_risk(
            "R1",
            "dynamodb",
            {"q1": "HIGH", "q2": "HIGH"},
            {"q1": "SELECT 1 FROM DUAL", "q2": "SELECT * FROM a"},
            {"q1": {"DUAL", "None"}, "q2": {"db.a", "UNKNOWN"}},
        )
        assert risk["affected_tables"] == ["db.a"]

    def test_the_risk_table_normaliser_drops_every_placeholder(self) -> None:
        from src.agents.referee.synthesis_data import SynthesisData
        from src.agents.referee.synthesis_report import _table_normaliser

        data = SynthesisData(
            job_id="j",
            database_name="db",
            collector={"database_schema": {"tables": [{"table_id": "db.a"}]}},
        )
        normalise = _table_normaliser(data)
        assert normalise(["A", "SYSIBM.SYSDUMMY1", "null", "", "dual"]) == {"db.a"}
        # A real table that only looks like a placeholder is kept.
        assert normalise(["A", "Null"]) == {"db.a", "Null"}


def _synthesis_data():
    from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData

    queries = [
        {
            "query_id": "q1",
            "query_text": "SELECT 1 FROM DUAL",
            "query_type": "SELECT",
            "tables_accessed": ["DUAL"],
        },
        {
            "query_id": "q2",
            "query_text": "SELECT * FROM a WHERE id = 1",
            "query_type": "SELECT",
            "tables_accessed": ["db.a"],
        },
    ]
    analysis = {
        "workload_analysis": {
            "anti_patterns_detected": [
                {
                    "anti_pattern_type": "hot-partition",
                    "query_ids": ["q1"],
                    "table_ids": ["DUAL"],
                    "severity_weight": 0.8,
                    "description": "hot key",
                }
            ]
        }
    }
    design = {
        "access_patterns": [
            {
                "pattern_id": "DDB-AP-1",
                "source_query_ids": ["q1"],
                "source_tables": ["DUAL"],
                "sql_text": "SELECT 1 FROM DUAL",
            },
            {
                "pattern_id": "DDB-AP-2",
                "source_query_ids": ["q2"],
                "source_tables": ["dual", "db.a"],
            },
        ]
    }
    return SynthesisData(
        job_id="t",
        database_name="db",
        collector={
            "queries": {"query_patterns": queries},
            "database_schema": {"tables": [{"table_id": "db.a"}]},
        },
        engines={
            "dynamodb": EngineArtifacts(engine="dynamodb", analysis=analysis, schema_design=design)
        },
        assignment={
            "query_assignments": [
                {"query_id": q, "assigned_engine": "dynamodb", "source_tables": t, "in_scope": True}
                for q, t in (("q1", ["DUAL"]), ("q2", ["db.a"]))
            ]
        },
    )


class TestTheSynthesisGroupsAndRisks:
    def test_a_query_group_is_never_named_after_a_dummy_table(self) -> None:
        from src.agents.referee.synthesis_report import (
            UNRESOLVED_TABLE_GROUP_LABEL,
            build_query_groups,
        )

        names = sorted(g["group_name"] for g in build_query_groups(_synthesis_data()))
        assert names == sorted([UNRESOLVED_TABLE_GROUP_LABEL, "db.a"])

    def test_an_anti_pattern_risk_names_no_dummy_table(self) -> None:
        from src.agents.referee.synthesis_report import build_risk_assessment

        risks = build_risk_assessment(_synthesis_data())["risks"]
        (risk,) = [r for r in risks if "hot key" in r.get("description", "")]
        assert risk["affected_tables"] == []


class TestTheNarrowerFiltersUseTheSharedList:
    """The relational query groups and the schema-design splitter read the shared list,
    not the resolver's narrower ``PSEUDO_TABLES`` (#1817 review)."""

    @pytest.mark.parametrize("placeholder", ["UNKNOWN", "None", "null", "dual"])
    def test_a_relational_query_group_is_never_named_after_a_placeholder(self, placeholder):
        from src.agents.referee.synthesis_data import SynthesisData
        from src.agents.referee.synthesis_report import (
            UNRESOLVED_TABLE_GROUP_LABEL,
            build_query_groups,
        )

        data = SynthesisData(
            job_id="t",
            database_name="db",
            collector={
                "queries": {
                    "query_patterns": [
                        {
                            "query_id": "q1",
                            "query_text": "SELECT 1",
                            "query_type": "SELECT",
                            "tables_accessed": [placeholder],
                        }
                    ]
                }
            },
            assignment={
                "query_assignments": [
                    {
                        "query_id": "q1",
                        "assigned_engine": "aurora_postgresql",
                        "source_tables": [placeholder],
                        "in_scope": True,
                    }
                ]
            },
        )
        names = [g["group_name"] for g in build_query_groups(data)]
        assert placeholder not in names
        assert names == [UNRESOLVED_TABLE_GROUP_LABEL]

    @pytest.mark.parametrize("placeholder", ["UNKNOWN", "None", "null", "dual"])
    def test_the_splitter_sees_no_source_table_behind_a_placeholder(self, placeholder) -> None:
        from src.agents.schema_design.group_splitter import has_no_source_table

        assert has_no_source_table({"tables_accessed": [placeholder]})
        assert not has_no_source_table({"tables_accessed": [placeholder, "db.a"]})
        assert not has_no_source_table({"tables_accessed": ["Null"]})
