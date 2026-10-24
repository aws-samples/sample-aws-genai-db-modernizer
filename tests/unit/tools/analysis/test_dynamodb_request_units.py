"""DynamoDB on-demand request units follow item size and read kind (#1476, #1000).

DynamoDB bills a read 0.5 / 1 / 2 read request units per 4 KB (eventually consistent /
strongly consistent / transactional), rounded up on the bytes the call reads, and a
write 1 write request unit per 1 KB of item size (2 when transactional), rounded up
per item (https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/read-write-operations.html,
https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis.html).
The maintainer decision on #1000 (2026-10-24) says which kind a statement is priced at:
a statement inside a transaction at transactional rates, a read that precedes a write
to the same item strongly consistent, every other read eventually consistent, with a
footnote when the collection can't tell.

Every expected figure below is worked by hand from those rules and the price table
(``src/catalog/prices.toml``: $0.125 per million read units, $0.625 per million write
units).
"""

from __future__ import annotations

import pytest

from src.shared.catalog.prices import SECONDS_PER_MONTH
from src.tools.analysis.dynamodb_analysis_tools import (
    CONSISTENCY_FOOTNOTE,
    EVENTUAL,
    STANDARD,
    STRONG,
    TRANSACTIONAL,
    estimate_dynamodb_costs,
    read_request_units,
    write_request_units,
)

KB = 1024
READ_PRICE = 0.125
WRITE_PRICE = 0.625


# A table whose average row is ``kb`` KB: ``size_mb * 1 MiB / row_count``.
def _table(table_id: str, kb: float) -> dict:
    return {"table_id": table_id, "size_mb": kb, "row_count": 1024}


def _q(qid: str, qtype: str, text: str, table: str, cps: float = 100.0, **stats) -> dict:
    return {
        "query_id": qid,
        "query_type": qtype,
        "query_text": text,
        "calls_per_second": cps,
        "tables_accessed": [table],
        **stats,
    }


def _collection(tables: list[dict], queries: list[dict], engine: str = "mysql") -> dict:
    return {
        "metadata": {"source_database": {"engine": engine}},
        "database_schema": {"tables": tables},
        "queries": {"query_patterns": queries},
    }


def _monthly(units_per_call: float, price: float, cps: float = 100.0) -> float:
    return round(cps * SECONDS_PER_MONTH * units_per_call * price / 1_000_000, 2)


# --- the unit rules, by hand ------------------------------------------------------------


@pytest.mark.parametrize(
    ("kb", "kind", "units"),
    [
        # 3.5 KB is one 4 KB block.
        (3.5, EVENTUAL, 0.5),
        (3.5, STRONG, 1.0),
        (3.5, TRANSACTIONAL, 2.0),
        # 9 KB rounds up to three 4 KB blocks.
        (9.0, EVENTUAL, 1.5),
        (9.0, STRONG, 3.0),
        (9.0, TRANSACTIONAL, 6.0),
        # Exactly 4 KB is one block, not two.
        (4.0, EVENTUAL, 0.5),
        # A read of nothing (no size collected) still bills one block.
        (0.0, EVENTUAL, 0.5),
    ],
)
def test_read_units_per_4_kb_by_read_kind(kb: float, kind: str, units: float) -> None:
    assert read_request_units(kb * KB, kind, 4 * KB) == units


@pytest.mark.parametrize(
    ("kb", "items", "kind", "units"),
    [
        # 2.5 KB rounds up to 3 KB: 3 WRU, 6 when transactional.
        (2.5, 1, STANDARD, 3.0),
        (2.5, 1, TRANSACTIONAL, 6.0),
        # Rounded per item, not on the total: two 2.5 KB items are 6 WRU, not 5.
        (2.5, 2, STANDARD, 6.0),
        (2.5, 2, TRANSACTIONAL, 12.0),
        (1.0, 1, STANDARD, 1.0),
        (0.0, 1, STANDARD, 1.0),
    ],
)
def test_write_units_per_1_kb_of_item_size(
    kb: float, items: float, kind: str, units: float
) -> None:
    assert write_request_units(kb * KB, kind, 1 * KB, items) == units


def test_an_unknown_kind_raises() -> None:
    with pytest.raises(KeyError):
        read_request_units(KB, "serializable", 4 * KB)
    with pytest.raises(KeyError):
        write_request_units(KB, EVENTUAL, KB)


# --- the estimate -----------------------------------------------------------------------


def test_a_plain_read_is_priced_eventually_consistent_on_its_bytes() -> None:
    """One 9 KB item a call: three 4 KB blocks at 0.5 RRU = 1.5 RRU, not one unit."""
    est = estimate_dynamodb_costs(
        _collection(
            [_table("shop.items", 9.0)],
            [_q("r", "SELECT", "SELECT * FROM items WHERE id = ?", "shop.items")],
        )
    )
    assert est.cost_components["read_request_units"] == _monthly(1.5, READ_PRICE)
    assert est.cost_components["read_request_units_per_month"] == 1.5 * 100 * SECONDS_PER_MONTH
    assert CONSISTENCY_FOOTNOTE in est.pricing_assumptions


def test_a_read_is_billed_on_all_the_rows_it_returns_rounded_once() -> None:
    """Three 3.5 KB rows a call are 10.5 KB: three blocks, 1.5 RRU (rounded on the total)."""
    est = estimate_dynamodb_costs(
        _collection(
            [_table("shop.items", 3.5)],
            [
                _q(
                    "r",
                    "SELECT",
                    "SELECT * FROM items WHERE owner = ?",
                    "shop.items",
                    rows_returned_avg=3,
                )
            ],
        )
    )
    assert est.cost_components["read_request_units"] == _monthly(1.5, READ_PRICE)


def test_a_locking_read_before_an_update_is_priced_strongly_consistent() -> None:
    """``SELECT ... FOR UPDATE`` reads the item it is about to write (read-modify-write):
    3.5 KB is one block at 1 RRU. The update writes one 3.5 KB item: 4 WRU."""
    est = estimate_dynamodb_costs(
        _collection(
            [_table("shop.items", 3.5)],
            [
                _q("r", "SELECT", "SELECT * FROM items WHERE id = ? FOR UPDATE", "shop.items"),
                _q(
                    "w",
                    "UPDATE",
                    "UPDATE items SET qty = qty - 1 WHERE id = ?",
                    "shop.items",
                    rows_affected_avg=1,
                ),
            ],
        )
    )
    assert est.cost_components["read_request_units"] == _monthly(1.0, READ_PRICE)
    assert est.cost_components["write_request_units"] == _monthly(4.0, WRITE_PRICE)
    # Every read's kind is known from the collection: no footnote.
    assert CONSISTENCY_FOOTNOTE not in est.pricing_assumptions
    assert any("strongly consistent (1 RRU per 4 KB)" in a for a in est.pricing_assumptions)


def test_the_lock_is_read_in_the_source_dialect() -> None:
    """``# FOR UPDATE`` is a MySQL comment, not a lock: the read stays eventually
    consistent (0.5 RRU). Read in PostgreSQL, where ``#`` is no comment, the parser
    sees the lock; the dialect is what decides."""
    text = "SELECT * FROM items WHERE id = ? # FOR UPDATE"
    tables = [_table("shop.items", 3.5)]
    mysql = estimate_dynamodb_costs(
        _collection(tables, [_q("r", "SELECT", text, "shop.items")], engine="mysql")
    )
    assert mysql.cost_components["read_request_units"] == _monthly(0.5, READ_PRICE)
    postgres = estimate_dynamodb_costs(
        _collection(tables, [_q("r", "SELECT", text, "shop.items")], engine="postgresql")
    )
    assert postgres.cost_components["read_request_units"] == _monthly(1.0, READ_PRICE)


def test_a_multi_item_write_is_priced_transactional() -> None:
    """An UPDATE that changes 2 items a call keeps its atomicity on DynamoDB as one
    ``TransactWriteItems`` call (``transaction_shape`` BOUNDED): each 2.5 KB item is
    3 KB at 2 WRU per 1 KB = 6 WRU, 12 WRU a call."""
    est = estimate_dynamodb_costs(
        _collection(
            [_table("shop.items", 2.5)],
            [
                _q(
                    "w",
                    "UPDATE",
                    "UPDATE items SET qty = 0 WHERE id IN (?, ?)",
                    "shop.items",
                    rows_affected_avg=2,
                )
            ],
        )
    )
    assert est.cost_components["write_request_units"] == _monthly(12.0, WRITE_PRICE)
    assert any(
        "priced transactional (2 WRU per 1 KB per item)" in a for a in est.pricing_assumptions
    )


def test_a_single_item_write_is_priced_at_the_standard_rate() -> None:
    """One 2.5 KB item: 3 WRU, the standard rate (a conditional write, no transaction)."""
    est = estimate_dynamodb_costs(
        _collection(
            [_table("shop.items", 2.5)],
            [
                _q(
                    "w",
                    "UPDATE",
                    "UPDATE items SET qty = 0 WHERE id = ?",
                    "shop.items",
                    rows_affected_avg=1,
                )
            ],
        )
    )
    assert est.cost_components["write_request_units"] == _monthly(3.0, WRITE_PRICE)
    assert not any("priced transactional" in a for a in est.pricing_assumptions)


def test_the_assumption_lines_state_the_rates_used() -> None:
    est = estimate_dynamodb_costs(
        _collection(
            [_table("shop.items", 3.5)],
            [_q("r", "SELECT", "SELECT * FROM items WHERE id = ?", "shop.items")],
        )
    )
    lines = est.pricing_assumptions
    # The old line named the strongly consistent rate for eventually consistent reads.
    assert "Eventually consistent reads (1 RRU per 4KB)" not in lines
    assert any("eventually consistent (0.5 RRU per 4 KB)" in a for a in lines)
    assert not any("strongly consistent (1 RRU" in a for a in lines)
    assert any("secondary indexes" in a for a in lines)
    assert lines[-1] == CONSISTENCY_FOOTNOTE
    assert CONSISTENCY_FOOTNOTE == (
        "DynamoDB reads are priced eventually consistent, except transactions, "
        "which are priced at transactional rates."
    )


def test_no_reads_no_footnote() -> None:
    est = estimate_dynamodb_costs(
        _collection(
            [_table("shop.items", 1.0)],
            [_q("w", "INSERT", "INSERT INTO items VALUES (?)", "shop.items")],
        )
    )
    assert est.cost_components["read_request_units"] == 0.0
    assert CONSISTENCY_FOOTNOTE not in est.pricing_assumptions


def test_an_empty_collection_prices_nothing_but_storage() -> None:
    est = estimate_dynamodb_costs(_collection([], []))
    assert est.cost_components["read_request_units"] == 0.0
    assert est.cost_components["write_request_units"] == 0.0
    assert est.monthly_cost_usd == round(0.01 * 0.25, 2)


# --- the footnote reaches the deliverables ----------------------------------------------


def _synthesis(collector: dict, engine: str = "dynamodb"):
    from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData

    estimate = estimate_dynamodb_costs(collector)
    assignment = {
        "query_assignments": [
            {
                "query_id": q["query_id"],
                "assigned_engine": engine,
                "source_tables": q["tables_accessed"],
                "assignment_reason": "test",
            }
            for q in collector["queries"]["query_patterns"]
        ]
    }
    return SynthesisData(
        job_id="j",
        database_name="shop",
        collector=collector,
        engines={
            "dynamodb": EngineArtifacts(
                engine="dynamodb", analysis={"cost_estimate": estimate.model_dump()}
            )
        },
        assignment=assignment,
    )


def test_the_tco_states_the_footnote_when_dynamodb_prices_a_read_by_default() -> None:
    from src.agents.referee.synthesis_report import build_tco_analysis

    collector = _collection(
        [_table("shop.items", 3.5)],
        [_q("r", "SELECT", "SELECT * FROM items WHERE id = ?", "shop.items")],
    )
    assert CONSISTENCY_FOOTNOTE in build_tco_analysis(_synthesis(collector))["assumptions"]


def test_the_tco_has_no_footnote_when_every_read_kind_is_known() -> None:
    from src.agents.referee.synthesis_report import build_tco_analysis

    collector = _collection(
        [_table("shop.items", 3.5)],
        [_q("r", "SELECT", "SELECT * FROM items WHERE id = ? FOR UPDATE", "shop.items")],
    )
    assert CONSISTENCY_FOOTNOTE not in build_tco_analysis(_synthesis(collector))["assumptions"]
