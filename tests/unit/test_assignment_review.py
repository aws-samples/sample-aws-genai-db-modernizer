"""Assignment-review render/parse/diff round-trip (ADR-028).

The editable surface must round-trip: rendering an assignment then parsing and
diffing it back yields zero changes; a single edited cell yields exactly one
minimal override; and any malformed edit fails loudly rather than silently
dropping the change.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.agents.referee.assignment_review import (
    REVIEW_BEGIN_MARKER,
    REVIEW_END_MARKER,
    REVIEW_TABLE_COLUMNS,
    ReviewParseError,
    diff_review_rows,
    parse_assignment_review,
    render_assignment_review,
    valid_target_engines,
)
from src.contracts.assignment_models import (
    Assignment,
    AssignmentSource,
    AssignmentStatus,
    QueryAssignment,
)


def _qa(query_id, engine, tables, reason="because", in_scope=True):
    return QueryAssignment(
        query_id=query_id,
        assigned_engine=engine,
        confidence=80,
        source_tables=tables,
        assignment_reason=reason,
        in_scope=in_scope,
    )


def _assignment() -> Assignment:
    return Assignment(
        job_id="job-1",
        version=2,
        status=AssignmentStatus.AUTO_GENERATED,
        source=AssignmentSource.REALITY_CHECK,
        timestamp=datetime.now(UTC),
        query_assignments=[
            _qa("q1", "dynamodb", ["t.users"], reason="key-value lookups"),
            _qa("q2", "dynamodb", ["t.posts"], reason="highest confidence for dynamodb"),
            _qa("q3", "opensearch", ["t.docs"], reason="signal override: text_search"),
            _qa("q4", "elasticache", ["t.sessions"], reason="hot key", in_scope=False),
        ],
        table_assignments=[],
        co_dependency_groups=[],
        validation_warnings=[],
    )


class TestRoundTrip:
    def test_unedited_render_parses_to_no_changes(self) -> None:
        a = _assignment()
        rows = parse_assignment_review(render_assignment_review(a))
        assert [r.query_id for r in rows] == ["q1", "q2", "q3", "q4"]
        assert diff_review_rows(a, rows) == []

    def test_render_carries_why_and_valid_engines(self) -> None:
        md = render_assignment_review(_assignment())
        assert "signal override: text_search" in md  # the "why"
        assert "key-value lookups" in md
        for engine in valid_target_engines():
            assert engine in md
        assert "| " + " | ".join(REVIEW_TABLE_COLUMNS) + " |" in md


class TestEdits:
    def test_engine_reassignment_yields_one_override(self) -> None:
        a = _assignment()
        md = render_assignment_review(a)
        edited = md.replace(
            "| q2 | t.posts | dynamodb | dynamodb | yes |",
            "| q2 | t.posts | dynamodb | opensearch | yes |",
        )
        assert edited != md
        overrides = diff_review_rows(a, parse_assignment_review(edited))
        assert len(overrides) == 1
        assert overrides[0].query_id == "q2"
        assert overrides[0].assigned_engine == "opensearch"
        assert overrides[0].in_scope is None  # scope not touched

    def test_scope_toggle_yields_one_override(self) -> None:
        a = _assignment()
        md = render_assignment_review(a)
        edited = md.replace(
            "| q1 | t.users | dynamodb | dynamodb | yes |",
            "| q1 | t.users | dynamodb | dynamodb | no |",
        )
        overrides = diff_review_rows(a, parse_assignment_review(edited))
        assert len(overrides) == 1
        assert overrides[0].query_id == "q1"
        assert overrides[0].in_scope is False
        assert overrides[0].assigned_engine is None

    def test_both_fields_changed_in_one_row(self) -> None:
        a = _assignment()
        edited = render_assignment_review(a).replace(
            "| q4 | t.sessions | elasticache | elasticache | no |",
            "| q4 | t.sessions | elasticache | dynamodb | yes |",
        )
        overrides = diff_review_rows(a, parse_assignment_review(edited))
        assert len(overrides) == 1
        assert overrides[0].assigned_engine == "dynamodb"
        assert overrides[0].in_scope is True

    def test_partial_table_with_only_changed_rows(self) -> None:
        # A customer on a large workload replies with ONLY the rows they changed
        # (markers + header + one edited row). Omitted queries are unchanged.
        a = _assignment()
        header = "| " + " | ".join(REVIEW_TABLE_COLUMNS) + " |"
        sep = "| " + " | ".join(["---"] * len(REVIEW_TABLE_COLUMNS)) + " |"
        partial = "\n".join(
            [
                "Here are just my changes:",
                REVIEW_BEGIN_MARKER,
                header,
                sep,
                "| q2 | t.posts | dynamodb | opensearch | yes |",
                REVIEW_END_MARKER,
            ]
        )
        overrides = diff_review_rows(a, parse_assignment_review(partial))
        assert len(overrides) == 1
        assert overrides[0].query_id == "q2"
        assert overrides[0].assigned_engine == "opensearch"

    def test_editing_read_only_current_column_is_ignored(self) -> None:
        # The customer wrongly edits the read-only "current engine" cell; the
        # Assignment stays the source of truth, so no delta is produced.
        a = _assignment()
        edited = render_assignment_review(a).replace(
            "| q1 | t.users | dynamodb | dynamodb | yes |",
            "| q1 | t.users | opensearch | dynamodb | yes |",
        )
        assert diff_review_rows(a, parse_assignment_review(edited)) == []


class TestStrictParsing:
    def test_missing_markers_raises(self) -> None:
        with pytest.raises(ReviewParseError, match="markers"):
            parse_assignment_review("no table here")

    def test_wrong_columns_raises(self) -> None:
        a = _assignment()
        md = render_assignment_review(a).replace(
            "| query_id | access pattern | current engine | new engine | in scope |",
            "| query_id | engine | in scope |",
        )
        with pytest.raises(ReviewParseError, match="columns"):
            parse_assignment_review(md)

    def test_unknown_engine_raises(self) -> None:
        a = _assignment()
        md = render_assignment_review(a).replace(
            "| q2 | t.posts | dynamodb | dynamodb | yes |",
            "| q2 | t.posts | dynamodb | dynamdb | yes |",
        )
        with pytest.raises(ReviewParseError, match="Unknown engine"):
            parse_assignment_review(md)

    def test_bad_scope_value_raises(self) -> None:
        a = _assignment()
        md = render_assignment_review(a).replace(
            "| q1 | t.users | dynamodb | dynamodb | yes |",
            "| q1 | t.users | dynamodb | dynamodb | maybe |",
        )
        with pytest.raises(ReviewParseError, match="in scope"):
            parse_assignment_review(md)

    def test_duplicate_query_id_raises(self) -> None:
        a = _assignment()
        md = render_assignment_review(a).replace(
            "| q3 | t.docs | opensearch | opensearch | yes |",
            "| q3 | t.docs | opensearch | opensearch | yes |\n"
            "| q1 | t.docs | opensearch | opensearch | yes |",
        )
        with pytest.raises(ReviewParseError, match="Duplicate query_id"):
            parse_assignment_review(md)

    def test_wrong_column_count_raises(self) -> None:
        a = _assignment()
        md = render_assignment_review(a).replace(
            "| q1 | t.users | dynamodb | dynamodb | yes |",
            "| q1 | dynamodb | yes |",
        )
        with pytest.raises(ReviewParseError, match="columns"):
            parse_assignment_review(md)


class TestDiffUnknownQuery:
    def test_row_for_unknown_query_raises(self) -> None:
        a = _assignment()
        md = render_assignment_review(a).replace(
            "| q4 | t.sessions | elasticache | elasticache | no |",
            "| q4 | t.sessions | elasticache | elasticache | no |\n"
            "| q99 | t.x | dynamodb | dynamodb | yes |",
        )
        rows = parse_assignment_review(md)
        with pytest.raises(ReviewParseError, match="not in the current"):
            diff_review_rows(a, rows)


class TestSummary:
    """render_assignment_summary is the engine-level recommendation shown first."""

    def test_summary_lists_engines_with_counts_and_rationale(self) -> None:
        from src.agents.referee.assignment_review import render_assignment_summary

        md = render_assignment_summary(_assignment())
        assert "recommendation" in md.lower()
        # One row per engine in use, with in-scope/total counts.
        assert "dynamodb" in md and "opensearch" in md and "elasticache" in md
        assert "2 / 2" in md  # dynamodb: 2 in scope of 2
        assert "0 / 1" in md  # elasticache: q4 is out of scope
        # A rationale is surfaced (aggregated from assignment_reason).
        assert "key-value lookups" in md or "highest confidence" in md
        # It is NOT the full per-query editable table.
        assert REVIEW_BEGIN_MARKER not in md


class TestStructuredTable:
    """build_review_table + diff_review_items are the HITL structured analogues
    of render/parse/diff."""

    def test_table_columns_mark_editable_and_include_rationale(self) -> None:
        from src.agents.referee.assignment_review import build_review_table

        cols, items = build_review_table(_assignment())
        by_field = {c["field"]: c for c in cols}
        # The two editable columns carry an editConfig; the rest do not.
        assert by_field["new_engine"]["editConfig"]["editingCell"] is True
        assert by_field["in_scope"]["editConfig"]["editingCell"] is True
        assert "editConfig" not in by_field["query_id"]
        assert "editConfig" not in by_field["rationale"]
        # The new-engine validation regex is built from the valid engine set.
        for engine in valid_target_engines():
            assert engine in by_field["new_engine"]["editConfig"]["validation"]
        # One row per query, anchored by id, with the rationale carried through.
        assert len(items) == 4
        q3 = next(i for i in items if i["query_id"] == "q3")
        assert q3["id"] == "q3"
        assert q3["current_engine"] == "opensearch"
        assert q3["new_engine"] == "opensearch"  # defaults to current
        assert "text_search" in q3["rationale"]

    def test_unedited_items_diff_to_no_changes(self) -> None:
        from src.agents.referee.assignment_review import build_review_table, diff_review_items

        a = _assignment()
        _cols, items = build_review_table(a)
        assert diff_review_items(a, items) == []

    def test_engine_edit_yields_one_override(self) -> None:
        from src.agents.referee.assignment_review import build_review_table, diff_review_items

        a = _assignment()
        _cols, items = build_review_table(a)
        for item in items:
            if item["query_id"] == "q2":
                item["new_engine"] = "opensearch"
        overrides = diff_review_items(a, items)
        assert len(overrides) == 1
        assert overrides[0].query_id == "q2"
        assert overrides[0].assigned_engine == "opensearch"
        assert overrides[0].in_scope is None  # unchanged field left unset

    def test_scope_edit_yields_one_override(self) -> None:
        from src.agents.referee.assignment_review import build_review_table, diff_review_items

        a = _assignment()
        _cols, items = build_review_table(a)
        for item in items:
            if item["query_id"] == "q1":
                item["in_scope"] = "no"
        overrides = diff_review_items(a, items)
        assert len(overrides) == 1
        assert overrides[0].query_id == "q1"
        assert overrides[0].in_scope is False

    def test_unknown_engine_raises(self) -> None:
        from src.agents.referee.assignment_review import diff_review_items

        a = _assignment()
        items = [{"query_id": "q1", "new_engine": "dynamdb", "in_scope": "yes"}]
        with pytest.raises(ReviewParseError, match="Unknown engine"):
            diff_review_items(a, items)

    def test_unknown_query_raises(self) -> None:
        from src.agents.referee.assignment_review import diff_review_items

        a = _assignment()
        items = [{"query_id": "q999", "new_engine": "dynamodb", "in_scope": "yes"}]
        with pytest.raises(ReviewParseError, match="not in the current"):
            diff_review_items(a, items)

    def test_bad_scope_raises(self) -> None:
        from src.agents.referee.assignment_review import diff_review_items

        a = _assignment()
        items = [{"query_id": "q1", "new_engine": "dynamodb", "in_scope": "maybe"}]
        with pytest.raises(ReviewParseError, match="in scope"):
            diff_review_items(a, items)

    def test_id_used_when_query_id_missing(self) -> None:
        from src.agents.referee.assignment_review import diff_review_items

        a = _assignment()
        # A submission that carries only ``id`` (the row anchor) still resolves.
        items = [{"id": "q2", "new_engine": "opensearch", "in_scope": "yes"}]
        overrides = diff_review_items(a, items)
        assert len(overrides) == 1
        assert overrides[0].query_id == "q2"
