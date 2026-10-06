"""#367-3: ``engine_table_scope``'s table/engine membership must survive the
same qualifier mismatch #116 fixed in schema design.

``engine_table_scope`` (``src/agents/referee/synthesis_grounding.py``) feeds
the executive-summary grounding check and its deterministic fallback, not
query routing: an assignment's ``query_assignments[].source_tables`` is
qualified with the SQL schema the parser saw, while ``known_tables``
(collector ``table_id``s) is qualified with the customer-entered database
label. An exact-string membership test against ``known`` dropped every table
for every engine when the two diverge, the same way the schema-design join
did before #116's fix.
"""

from __future__ import annotations

from src.agents.referee.synthesis_grounding import engine_table_scope


def test_qualifier_mismatch_still_resolves_to_known_table() -> None:
    assignment = {
        "query_assignments": [
            {
                "query_id": "q-1",
                "assigned_engine": "dynamodb",
                "in_scope": True,
                "source_tables": ["discourse.admin_notices"],
            }
        ]
    }
    known_tables = ["discource.admin_notices"]  # customer-entered label typo

    scope = engine_table_scope(assignment, [], [], known_tables)

    assert scope == {"dynamodb": ["discource.admin_notices"]}


def test_exact_match_is_unaffected() -> None:
    """No regression for the common case where both qualifiers agree."""
    assignment = {
        "query_assignments": [
            {
                "query_id": "q-1",
                "assigned_engine": "dynamodb",
                "in_scope": True,
                "source_tables": ["discourse.admin_notices"],
            }
        ]
    }
    known_tables = ["discourse.admin_notices"]

    scope = engine_table_scope(assignment, [], [], known_tables)

    assert scope == {"dynamodb": ["discourse.admin_notices"]}


def test_unresolvable_name_is_dropped_not_guessed() -> None:
    """A name that resolves to nothing (ambiguous, or no match at all) is
    dropped, same as an exact-match failure was before this fix -- it must
    never silently attribute a table to the wrong engine."""
    assignment = {
        "query_assignments": [
            {
                "query_id": "q-1",
                "assigned_engine": "dynamodb",
                "in_scope": True,
                "source_tables": ["unknown"],
            }
        ]
    }
    known_tables = ["discourse.admin_notices"]

    scope = engine_table_scope(assignment, [], [], known_tables)

    assert scope == {"dynamodb": []}


def test_empty_known_tables_keeps_every_name() -> None:
    """Without a known-tables set at all, every table name is kept
    unfiltered -- the pre-#319 behavior for an absent schema."""
    assignment = {
        "query_assignments": [
            {
                "query_id": "q-1",
                "assigned_engine": "dynamodb",
                "in_scope": True,
                "source_tables": ["anything.at_all"],
            }
        ]
    }

    scope = engine_table_scope(assignment, [], [], [])

    assert scope == {"dynamodb": ["anything.at_all"]}
