"""What forms a unit that moves as one, as ADR-031 Decision 4 states it (#1270).

Decision 4 once said statements in one transaction move together. The code never
did that: the collected statistics carry no transaction boundary, so no group can
form from one. Units form from two sources only, merged by
``write_groups.placement_groups`` for every routing pass:

- a co-dependency (JOIN) group: queries sharing a significant join on a table
  (``co_dependency.build_co_dependency_groups``);
- a table's write group: its writes and its locking reads (Amendment 9,
  ``write_groups.build_write_groups``). A plain read stays per query.

Transaction groups are #1533. When that lands, these tests and Decision 4's text
change together.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from src.agents.referee.co_dependency import build_co_dependency_groups
from src.agents.referee.write_groups import build_write_groups, placement_groups
from src.contracts.collector_output import QueryPattern

REPO_ROOT = Path(__file__).resolve().parents[4]
ADR_031 = (
    REPO_ROOT
    / "docs"
    / "architecture"
    / "decisions"
    / "ADR-031-query-first-routing-and-engine-rule-catalog.md"
)


def _q(qid: str, text: str, tables: list[str], **extra: Any) -> dict[str, Any]:
    query: dict[str, Any] = {
        "query_id": qid,
        "query_text": text,
        "query_type": text.split()[0].upper(),
        "tables_accessed": tables,
        "calls_per_second": 1.0,
    }
    query.update(extra)
    return query


def _units(queries: list[dict[str, Any]]) -> list[list[str]]:
    """The units the first assignment places: the resolver's Step 3, verbatim."""
    co = build_co_dependency_groups(queries, [])
    return placement_groups(co, build_write_groups(queries, "postgresql"))


def _decision_4() -> str:
    text = ADR_031.read_text(encoding="utf-8")
    match = re.search(r"^### 4\. .*?(?=^### 5\. )", text, re.MULTILINE | re.DOTALL)
    assert match, "ADR-031 has no Decision 4 section"
    return match.group(0)


@pytest.mark.passes_on_main
class TestTheTwoSources:
    """The behaviour Decision 4 now describes, which main already has."""

    def test_queries_sharing_a_significant_join_form_a_co_dependency_group(self) -> None:
        join = {"has_joins": True, "join_count": 2}
        queries = [
            _q(
                "j1",
                "SELECT * FROM a JOIN b ON a.id = b.a_id JOIN c ON c.id = b.c_id",
                ["db.a", "db.b", "db.c"],
                **join,
            ),
            _q(
                "j2",
                "SELECT * FROM b JOIN d ON d.id = b.d_id JOIN e ON e.id = d.e_id",
                ["db.b", "db.d", "db.e"],
                **join,
            ),
        ]
        assert _units(queries) == [["j1", "j2"]]

    def test_a_light_join_forms_no_group(self) -> None:
        light = {"has_joins": True, "join_count": 1}
        queries = [
            _q(
                "j1", "SELECT a.x, b.name FROM a JOIN b ON a.b_id = b.id", ["db.a", "db.b"], **light
            ),
            _q(
                "j2", "SELECT c.x, b.name FROM c JOIN b ON c.b_id = b.id", ["db.c", "db.b"], **light
            ),
        ]
        assert _units(queries) == []

    def test_a_tables_writes_and_locking_reads_form_its_write_group(self) -> None:
        queries = [
            _q("w1", "INSERT INTO a (id, x) VALUES ($1, $2)", ["db.a"]),
            _q("w2", "UPDATE a SET x = $1 WHERE id = $2", ["db.a"]),
            _q("l1", "SELECT * FROM a WHERE id = $1 FOR UPDATE", ["db.a"], query_type="SELECT"),
        ]
        assert _units(queries) == [["w1", "w2", "l1"]]

    def test_a_plain_read_of_a_written_table_stays_per_query(self) -> None:
        queries = [
            _q("w1", "INSERT INTO a (id, x) VALUES ($1, $2)", ["db.a"]),
            _q("w2", "UPDATE a SET x = $1 WHERE id = $2", ["db.a"]),
            _q("r1", "SELECT * FROM a WHERE id = $1", ["db.a"]),
        ]
        assert _units(queries) == [["w1", "w2"]]

    def test_a_join_group_sharing_a_statement_with_a_write_group_is_one_unit(self) -> None:
        join = {"has_joins": True, "join_count": 2}
        queries = [
            _q(
                "j1",
                "SELECT * FROM a JOIN b ON a.id = b.a_id JOIN c ON c.id = b.c_id",
                ["db.a", "db.b", "db.c"],
                **join,
            ),
            _q(
                "j2",
                "UPDATE b SET x = 1 FROM c, d WHERE b.c_id = c.id AND c.d_id = d.id",
                ["db.b", "db.c", "db.d"],
                **join,
            ),
            _q("w1", "INSERT INTO b (id) VALUES ($1)", ["db.b"]),
        ]
        assert _units(queries) == [["j1", "j2", "w1"]]


@pytest.mark.passes_on_main
class TestNoGroupFormsFromATransaction:
    """Decision 4: statements in one transaction form no group of their own (#1533)."""

    def test_writes_to_two_tables_in_one_transaction_are_placed_apart(self) -> None:
        # One application transaction, as far as anything could record it: the
        # control statements collected beside it, and the same transaction and
        # session id on each row. Nothing links the two writes, so each table's
        # write is its own unit of one and routes alone.
        txn = {"transaction_id": "t-1", "session_id": "s-1"}
        queries = [
            _q("b", "BEGIN", [], query_type="OTHER", **txn),
            _q("w_orders", "INSERT INTO orders (id, total) VALUES ($1, $2)", ["db.orders"], **txn),
            _q("w_stock", "UPDATE stock SET qty = qty - $1 WHERE sku = $2", ["db.stock"], **txn),
            _q("c", "COMMIT", [], query_type="OTHER", **txn),
        ]
        units = _units(queries)
        assert not any({"w_orders", "w_stock"} <= set(u) for u in units), (
            "statements in one transaction now move together: update ADR-031 Decision 4 "
            "and its Enforcement row in the same change (#1533)"
        )
        assert units == []

    def test_the_collected_statement_carries_no_transaction_boundary(self) -> None:
        # Why no group can form today: a collected row is one statement shape's
        # aggregated statistics, with no transaction or session it ran in. A field
        # that adds one is #1533, and Decision 4 changes with it.
        boundary = [
            name
            for name in QueryPattern.model_fields
            if re.search(r"transaction|session|txn|xact", name, re.IGNORECASE)
        ]
        assert boundary == [], (
            f"the collector now records {boundary}: form groups from it through "
            "placement_groups and update ADR-031 Decision 4 (#1533)"
        )


class TestDecision4StatesWhatTheCodeDoes:
    """The ADR claims no more than the tests above enforce (AGENTS.md)."""

    def test_decision_4_no_longer_says_a_transaction_moves_as_one(self) -> None:
        assert "statements in one transaction, or reads that must stay consistent" not in (
            _decision_4()
        )

    def test_decision_4_names_both_sources_and_the_merge(self) -> None:
        text = _decision_4()
        for name in (
            "co_dependency.build_co_dependency_groups",
            "write_groups.build_write_groups",
            "write_groups.placement_groups",
        ):
            assert f"`{name}`" in text, name

    def test_decision_4_says_transactions_form_no_group_and_names_the_follow_up(self) -> None:
        text = _decision_4()
        assert "no group forms from a transaction" in text.lower()
        assert "#1533" in text

    def test_decision_4_names_this_guard_in_its_enforcement_row(self) -> None:
        text = ADR_031.read_text(encoding="utf-8")
        row = next(
            line for line in text.splitlines() if line.startswith("| 4. Co-dependency groups")
        )
        assert "tests/unit/agents/referee/test_co_dependency_sources.py" in row
