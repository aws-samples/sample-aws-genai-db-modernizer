# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``cut_collection`` (#1633, contract 1.3): cut an ``assess()`` result per service and
assess each piece again, as a library caller does it.

- a cut keeps its tables and the statements wholly among them; a statement that crosses
  the cut is left out unless the caller gives a part for it; the foreign keys between a
  kept and a dropped table are listed in ``scope.dropped_foreign_keys``;
- a part's id is built by the library (``part-<h>-<from_query_id>``), never collides with
  a collected id, and the part inherits its statement's execution statistics;
- every invalid part is refused with a typed ``CutError``, never dropped;
- ``assess()`` takes the cut back at ``analysis`` and ``reality_check``; a part's row is
  marked ``rewritten_by_caller`` and names its statement in ``from_query_id`` (1.3 only);
- a cut's ``collection`` saved by an earlier release is still accepted
  (``tests/fixtures/assess_cut_v1_3_trailgear_support.json``).

The services are cut from the trailgear sample (SQL Server).
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from scripts import render_contract_schemas as rcs
from src.assessment import (
    AssessmentResult,
    CollectionCut,
    CutError,
    CutSourceError,
    DuplicatePartError,
    EmptyCutError,
    InvalidPartError,
    InvalidTableIdsError,
    NotACrossingStatementError,
    PartDialectError,
    PartIdCollisionError,
    PartOfKeptStatementError,
    PartParseError,
    PartTableNotInStatementError,
    PartTableOutsideCutError,
    RetiredContractVersionError,
    StatementPart,
    UnknownStatementError,
    UnknownTableError,
    ViewOutsideCutError,
    assess,
    cut_collection,
    load_collection,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures"
#: A cut written by contract 1.3's first release: the trailgear support service
#: (Ticket, TicketMessage, KnowledgeArticle) with one part of the crossing statement
#: 0x31648DAA008826FB, as ``CollectionCut.model_dump(mode="json")`` wrote it.
SAVED_CUT = FIXTURES / "assess_cut_v1_3_trailgear_support.json"

ORDERS = [
    "trailgear.SalesOrder",
    "trailgear.SalesOrderLine",
    "trailgear.Payment",
    "trailgear.Shipment",
    "trailgear.OrderStatusHistory",
]
CATALOG = [
    "trailgear.Product",
    "trailgear.ProductCategory",
    "trailgear.Brand",
    "trailgear.ProductImage",
    "trailgear.ProductPrice",
    "trailgear.ProductReview",
]
SUPPORT = ["trailgear.Ticket", "trailgear.TicketMessage", "trailgear.KnowledgeArticle"]

#: Top products: SalesOrderLine, Product and SalesOrder (crosses orders and catalog).
TOP_PRODUCTS = "0x41A1D5EE8655B412"
#: A customer's order lines: SalesOrderLine, Product and SalesOrder.
ORDER_LINES = "0x288281D96014BDB3"
#: Buyers by region: SalesOrder and CustomerAddress.
BUYERS_BY_REGION = "0x930174356CDABACA"
#: Open tickets with the customer's email: Ticket and Customer.
OPEN_TICKETS = "0x31648DAA008826FB"
#: An order by id: SalesOrder only.
ORDER_BY_ID = "0x579133B43C5DF08B"
#: A product search: Product only.
PRODUCT_SEARCH = "0xE715A6116BCB0673"
#: EXEC usp_PlaceOrder: reads no table the parser can see (0.93 calls a second).
PLACE_ORDER = "0xF4CBAAB59A417F55"
#: The statements of trailgear the cut cannot place: each reads no table.
TRAILGEAR_UNPLACED = [
    PLACE_ORDER,
    "0xA227D4C4883C8A81",  # EXEC usp_ReserveStock
    "0xB656697AF8467E3D",  # EXEC usp_RecalculateLoyaltyTier
    "0xA5625CDD8366F8B1",  # NEXT VALUE FOR OrderNumberSeq
    "0xE8316B5EB310DD77",  # EXEC usp_CancelOrder
    "0x8BBAEC4CCECE4C82",  # SELECT dbo.ufn_EffectivePrice(...)
    "0xBCD2639091D097D5",  # SET TRANSACTION ISOLATION LEVEL
]

ORDERS_PART = (
    "SELECT l.[ProductID], l.[Quantity] FROM [SalesOrderLine] l "
    "JOIN [SalesOrder] o ON o.[SalesOrderID] = l.[SalesOrderID] WHERE o.[CustomerID] = @c"
)
PLACE_ORDER_PART = (
    "INSERT INTO [SalesOrderLine] ([SalesOrderID], [ProductID], [Quantity]) "
    "SELECT o.[SalesOrderID], @ProductID, @Quantity FROM [SalesOrder] o WHERE o.[CustomerID] = @CustomerID"
)
SUPPORT_PART = (
    "SELECT t.[TicketID], t.[Subject], t.[Priority], t.[CustomerID] FROM [Ticket] t "
    "WHERE t.[Status] = @Status ORDER BY t.[Priority] DESC, t.[OpenedDate]"
)

#: The collector output's statistics a part inherits from its statement.
STATISTICS = (
    "frequency_per_hour",
    "calls_per_second",
    "rows_returned_avg",
    "rows_returned_p95",
    "rows_affected_avg",
    "rows_examined_avg",
    "execution_time_ms_avg",
    "execution_time_ms_min",
    "execution_time_ms_max",
    "execution_time_ms_p50",
    "execution_time_ms_p95",
    "execution_time_ms_p99",
    "total_time_ms",
    "db_load_contribution_percent",
)


@cache
def _collection() -> dict[str, Any]:
    loaded: dict[str, Any] = load_collection(
        REPO / "docs" / "examples" / "trailgear" / "trailgear.zip"
    )
    return loaded


@cache
def _whole(run_through: str = "analysis") -> AssessmentResult:
    return assess(
        {
            "database_name": "trailgear",
            "collection": _collection(),
            "options": {"run_through": run_through},
        }
    )


def _rows(cut: CollectionCut) -> dict[str, dict[str, Any]]:
    return {p["query_id"]: p for p in cut.collection["queries"]["query_patterns"]}


def _expected_part_id(from_query_id: str, tables: list[str]) -> str:
    """The documented rule, written out here on its own."""
    key = json.dumps([from_query_id, sorted(tables)], separators=(",", ":"))
    return f"part-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]}-{from_query_id}"


def _assess_cut(cut: CollectionCut | dict[str, Any], run_through: str, version: str | None = None):
    scope = cut.scope if isinstance(cut, CollectionCut) else cut["scope"]
    collection = cut.collection if isinstance(cut, CollectionCut) else cut["collection"]
    request: dict[str, Any] = {
        "database_name": "service",
        "collection": collection,
        "scope": scope,
        "options": {"run_through": run_through},
    }
    if version is not None:
        request["contract_version"] = version
    return assess(request)


# --------------------------------------------------------------------------- kept, dropped, crossing


@pytest.mark.parametrize(
    "service", [ORDERS, CATALOG, SUPPORT], ids=["orders", "catalog", "support"]
)
def test_a_cut_keeps_the_statements_wholly_among_its_tables(service: list[str]) -> None:
    whole = _whole()
    cut = cut_collection(whole, table_ids=service)
    kept = set(service)
    inside = [s.query_id for s in whole.statements if s.tables and set(s.tables) <= kept]
    assert cut.table_ids == sorted(service)
    assert cut.query_ids == inside
    assert cut.scope.tables == sorted(service)
    assert cut.scope.rewritten_query_ids == []
    tables = {t["table_id"] for t in cut.collection["database_schema"]["tables"]}
    assert tables == kept
    assert list(_rows(cut)) == inside


def test_the_crossing_and_dropped_statements_of_each_service() -> None:
    whole = _whole()
    for service, crossing in (
        (ORDERS, {TOP_PRODUCTS, ORDER_LINES, BUYERS_BY_REGION}),
        (CATALOG, {TOP_PRODUCTS, ORDER_LINES}),
        (SUPPORT, {OPEN_TICKETS}),
    ):
        cut = cut_collection(whole, table_ids=service)
        kept = set(service)
        found = {
            s.query_id
            for s in whole.statements
            if kept & set(s.tables) and not set(s.tables) <= kept
        }
        assert found == crossing
        assert not crossing & set(cut.query_ids)
        dropped = {s.query_id for s in whole.statements if not kept & set(s.tables)}
        assert not dropped & set(cut.query_ids)
        assert len(cut.query_ids) + len(crossing) + len(dropped) == len(whole.statements)
    assert ORDER_BY_ID in cut_collection(whole, table_ids=ORDERS).query_ids
    assert PRODUCT_SEARCH not in cut_collection(whole, table_ids=ORDERS).query_ids


def _cut_keys(whole: AssessmentResult, service: list[str]) -> list[dict[str, str]]:
    """Every foreign key between a kept table and a dropped one, from the result."""
    kept = set(service)
    return sorted(
        (
            {
                "constraint_name": fk.constraint_name,
                "table_id": table.table_id,
                "referenced_table_id": fk.referenced_table,
            }
            for table in whole.summary.table_facts
            for fk in table.foreign_keys
            if fk.referenced_table is not None
            and (table.table_id in kept) != (fk.referenced_table in kept)
        ),
        key=lambda k: (k["table_id"], k["constraint_name"], k["referenced_table_id"]),
    )


def test_the_dropped_foreign_keys_of_the_orders_service() -> None:
    whole = _whole()
    cut = cut_collection(whole, table_ids=ORDERS)
    names = [
        (k["constraint_name"], k["table_id"], k["referenced_table_id"])
        for k in cut.scope.dropped_foreign_keys
    ]
    assert names == [
        ("FK_SalesOrder_Address", "trailgear.SalesOrder", "trailgear.CustomerAddress"),
        ("FK_SalesOrder_Customer", "trailgear.SalesOrder", "trailgear.Customer"),
        ("FK_SalesOrder_Promotion", "trailgear.SalesOrder", "trailgear.Promotion"),
        ("FK_SalesOrderLine_Product", "trailgear.SalesOrderLine", "trailgear.Product"),
    ]
    assert cut.scope.dropped_foreign_keys == _cut_keys(whole, ORDERS)


def test_a_foreign_key_from_a_dropped_table_into_the_cut_is_listed_too() -> None:
    whole = _whole()
    cut = cut_collection(whole, table_ids=CATALOG)
    keys = {(k["constraint_name"], k["table_id"]) for k in cut.scope.dropped_foreign_keys}
    # Kept tables referenced from outside: StockLevel, SalesOrderLine and others -> Product.
    assert ("FK_SalesOrderLine_Product", "trailgear.SalesOrderLine") in keys
    assert ("FK_ProductReview_Customer", "trailgear.ProductReview") in keys
    # A key inside the cut (and a self-reference) stays.
    assert ("FK_Product_Brand", "trailgear.Product") not in keys
    assert ("FK_ProductCategory_Parent", "trailgear.ProductCategory") not in keys
    assert cut.scope.dropped_foreign_keys == _cut_keys(whole, CATALOG)


@pytest.mark.parametrize(
    "service", [ORDERS, CATALOG, SUPPORT], ids=["orders", "catalog", "support"]
)
def test_the_cut_holds_no_foreign_key_to_a_dropped_table(service: list[str]) -> None:
    result = _assess_cut(cut_collection(_whole(), table_ids=service), "analysis")
    assert [t.table_id for t in result.summary.table_facts] == sorted(service)
    for table in result.summary.table_facts:
        for fk in table.foreign_keys:
            assert fk.referenced_table in service, fk.constraint_name


def test_a_trigger_on_a_dropped_table_is_left_out() -> None:
    whole_triggers = whole_collector_output()["database_schema"]["triggers"]
    assert {t["table_id"] for t in whole_triggers} == {"ProductPrice", "SalesOrder"}
    cut = cut_collection(_whole(), table_ids=ORDERS)
    assert [t["table_id"] for t in cut.collection["database_schema"]["triggers"]] == ["SalesOrder"]


@cache
def whole_collector_output() -> dict[str, Any]:
    run = assess(
        {
            "database_name": "trailgear",
            "collection": _collection(),
            "options": {"run_through": "analysis", "include_artifacts": True},
        }
    )
    assert run.artifacts is not None
    out: dict[str, Any] = run.artifacts.model_dump(mode="json")["collector_output"]
    return out


# --------------------------------------------------------------------------- parts


def test_a_part_takes_the_place_of_its_crossing_statement() -> None:
    whole = _whole()
    cut = cut_collection(
        whole, table_ids=ORDERS, parts=[{"from_query_id": TOP_PRODUCTS, "query_text": ORDERS_PART}]
    )
    # The id is the statement and the cut's tables, never what the parser reads.
    part_id = _expected_part_id(TOP_PRODUCTS, ORDERS)
    # The id assess.md shows for this part.
    assert part_id == "part-7855a261de974f29-0x41A1D5EE8655B412"
    assert part_id in (REPO / "docs" / "contracts" / "assess.md").read_text("utf-8")
    assert cut.scope.rewritten_query_ids == [part_id]
    without = cut_collection(whole, table_ids=ORDERS).query_ids
    assert [q for q in cut.query_ids if q != part_id] == without
    # It stands where its statement stood in the collection.
    order = [s.query_id for s in whole.statements]
    before = [q for q in without if order.index(q) < order.index(TOP_PRODUCTS)]
    assert cut.query_ids.index(part_id) == len(before)
    row = _rows(cut)[part_id]
    assert row["query_text"] == ORDERS_PART
    assert sorted(row["tables_accessed"]) == ["trailgear.SalesOrder", "trailgear.SalesOrderLine"]


def test_a_part_inherits_its_statements_statistics() -> None:
    whole = whole_collector_output()
    original = {p["query_id"]: p for p in whole["queries"]["query_patterns"]}[TOP_PRODUCTS]
    cut = cut_collection(
        _whole(),
        table_ids=ORDERS,
        parts=[StatementPart(from_query_id=TOP_PRODUCTS, query_text=ORDERS_PART)],
    )
    (part_id,) = cut.scope.rewritten_query_ids
    row = _rows(cut)[part_id]
    assert original["calls_per_second"] and original["execution_time_ms_avg"]
    for name in STATISTICS:
        assert row.get(name) == original.get(name), name
    # And the result reads them: the part's calls are its statement's.
    result = _assess_cut(cut, "analysis")
    calls = {s.query_id: s.calls_per_second for s in result.statements}
    by_id = {s.query_id: s for s in _whole().statements}
    assert calls[part_id] == by_id[TOP_PRODUCTS].calls_per_second


def test_the_part_id_does_not_follow_the_parts_text() -> None:
    """Two cuts with the same statement and tables but different part texts give the same
    id: the id is built from the caller's input, never from what the parser reads."""
    texts = (
        ORDERS_PART,
        "SELECT o.[OrderDate] FROM [SalesOrder] o WHERE o.[SalesOrderID] = @o",
    )
    ids = {
        cut_collection(
            _whole(), table_ids=ORDERS, parts=[{"from_query_id": TOP_PRODUCTS, "query_text": t}]
        ).scope.rewritten_query_ids[0]
        for t in texts
    }
    assert ids == {_expected_part_id(TOP_PRODUCTS, ORDERS)}


def test_parts_of_a_cut_of_a_cut_name_their_own_statement() -> None:
    cut = cut_collection(
        _whole(),
        table_ids=ORDERS,
        parts=[{"from_query_id": TOP_PRODUCTS, "query_text": ORDERS_PART}],
    )
    (part_id,) = cut.scope.rewritten_query_ids
    first = _assess_cut(cut, "analysis")
    again = cut_collection(
        first,
        table_ids=["trailgear.SalesOrderLine"],
        parts=[
            {"from_query_id": part_id, "query_text": "SELECT l.[Quantity] FROM [SalesOrderLine] l"}
        ],
    )
    (nested,) = again.scope.rewritten_query_ids
    assert nested == _expected_part_id(part_id, ["trailgear.SalesOrderLine"])
    result = _assess_cut(again, "analysis")
    assert {s.query_id: s.from_query_id for s in result.statements}[nested] == part_id


def test_a_cut_of_a_cut_carries_the_earlier_scope_forward() -> None:
    """A part of the first cut that the second keeps whole stays rewritten_by_caller; the
    first cut's dropped foreign keys that still touch a kept table, its unplaced
    statements and its derived_from carry forward."""
    cut = cut_collection(
        _whole(),
        table_ids=ORDERS,
        parts=[{"from_query_id": TOP_PRODUCTS, "query_text": ORDERS_PART}],
    )
    (part_id,) = cut.scope.rewritten_query_ids
    scope = cut.scope.model_copy(update={"derived_from": "trailgear, whole database"})
    first = assess(
        {
            "database_name": "orders",
            "collection": cut.collection,
            "scope": scope,
            "options": {"run_through": "analysis"},
        }
    )
    again = cut_collection(first, table_ids=["trailgear.SalesOrder", "trailgear.SalesOrderLine"])
    assert part_id in again.query_ids
    assert again.scope.rewritten_query_ids == [part_id]
    assert again.scope.derived_from == "trailgear, whole database"
    assert again.scope.unplaced_statements == cut.scope.unplaced_statements
    keys = {(k["constraint_name"], k["table_id"]) for k in again.scope.dropped_foreign_keys}
    # From the first cut (SalesOrder is still kept) and from this one (Payment dropped).
    assert ("FK_SalesOrder_Customer", "trailgear.SalesOrder") in keys
    assert ("FK_Payment_Order", "trailgear.Payment") in keys
    # From the first cut, but between two tables this cut drops: no longer listed.
    narrower = cut_collection(first, table_ids=["trailgear.Payment"])
    names = {k["constraint_name"] for k in narrower.scope.dropped_foreign_keys}
    assert "FK_SalesOrderLine_Product" not in names and "FK_Payment_Order" in names
    result = _assess_cut(again, "analysis")
    marked = {s.query_id: (s.rewritten_by_caller, s.from_query_id) for s in result.statements}
    assert marked[part_id] == (True, TOP_PRODUCTS)


# --------------------------------------------------------------------------- refusals


def _refused(table_ids: Any, parts: list[Any], error: type[CutError]) -> CutError:
    with pytest.raises(error) as raised:
        cut_collection(_whole(), table_ids=table_ids, parts=parts)
    refused = raised.value
    assert isinstance(refused, CutError) and isinstance(refused, ValueError)
    return refused


def test_a_part_naming_a_table_outside_the_cut_is_refused() -> None:
    text = (
        "SELECT p.[Name], l.[Quantity] FROM [SalesOrderLine] l "
        "JOIN [Product] p ON p.[ProductID] = l.[ProductID]"
    )
    error = _refused(
        ORDERS, [{"from_query_id": TOP_PRODUCTS, "query_text": text}], PartTableOutsideCutError
    )
    assert error.outside == ["trailgear.Product"]  # type: ignore[attr-defined]
    assert error.from_query_id == TOP_PRODUCTS  # type: ignore[attr-defined]


def test_a_part_naming_a_table_the_collection_lacks_is_refused() -> None:
    text = "UPDATE sales.Orders SET Status = @v WHERE OrderID = @id"
    _refused(
        ORDERS, [{"from_query_id": TOP_PRODUCTS, "query_text": text}], PartTableOutsideCutError
    )


def test_a_part_of_an_unknown_statement_is_refused() -> None:
    error = _refused(
        ORDERS, [{"from_query_id": "q7", "query_text": ORDERS_PART}], UnknownStatementError
    )
    assert isinstance(error, NotACrossingStatementError)


def test_a_part_of_a_statement_that_does_not_cross_the_cut_is_refused() -> None:
    text = "SELECT [Name] FROM [Product] WHERE [ProductID] = @id"
    error = _refused(
        ORDERS, [{"from_query_id": PRODUCT_SEARCH, "query_text": text}], NotACrossingStatementError
    )
    assert not isinstance(error, UnknownStatementError)


def test_a_statement_kept_whole_takes_no_part() -> None:
    text = "SELECT [Status] FROM [SalesOrder] WHERE [SalesOrderID] = @id"
    _refused(ORDERS, [{"from_query_id": ORDER_BY_ID, "query_text": text}], PartOfKeptStatementError)


def test_a_duplicate_part_is_refused() -> None:
    part = {"from_query_id": TOP_PRODUCTS, "query_text": ORDERS_PART}
    _refused(ORDERS, [part, dict(part)], DuplicatePartError)
    # One part per statement per cut: another text for the same statement is refused.
    other = {
        "from_query_id": TOP_PRODUCTS,
        "query_text": "SELECT o.[OrderDate] FROM [SalesOrder] o JOIN [SalesOrderLine] l ON l.[SalesOrderID] = o.[SalesOrderID]",
    }
    _refused(ORDERS, [part, other], DuplicatePartError)


@pytest.mark.parametrize(
    "text",
    [
        "this is not sql at all",
        "SELECT [Quantity] FROM [SalesOrderLine] WHERE",  # cut off
        "SELECT [Quantity] FROM [SalesOrderLine]; DELETE FROM [SalesOrder]",  # two statements
        "SELECT 1",  # no table
        "",
    ],
    ids=["not-sql", "cut-off", "two-statements", "no-table", "empty"],
)
def test_text_the_source_dialect_cannot_read_is_refused(text: str) -> None:
    _refused(ORDERS, [{"from_query_id": TOP_PRODUCTS, "query_text": text}], PartParseError)


@pytest.mark.parametrize(
    "part",
    [
        {"from_query_id": TOP_PRODUCTS},
        {"query_text": ORDERS_PART},
        {"from_query_id": TOP_PRODUCTS, "query_text": ORDERS_PART, "query_id": "mine"},
        {"from_query_id": "", "query_text": ORDERS_PART},
        "not a part",
    ],
    ids=["no-text", "no-id", "caller-id", "empty-id", "not-a-mapping"],
)
def test_a_malformed_part_is_refused(part: Any) -> None:
    _refused(ORDERS, [part], InvalidPartError)


@pytest.mark.parametrize("parts", [None, "a part", {"from_query_id": TOP_PRODUCTS}])
def test_parts_that_are_not_a_list_are_refused(parts: Any) -> None:
    _refused(ORDERS, parts, InvalidPartError)


def test_the_table_ids_are_checked() -> None:
    error = _refused(["trailgear.SalesOrder", "trailgear.Nope"], [], UnknownTableError)
    assert error.table_ids == ["trailgear.Nope"]  # type: ignore[attr-defined]
    _refused([], [], EmptyCutError)
    for bad in ("trailgear.SalesOrder", None, 7, ["trailgear.SalesOrder", None], [""]):
        _refused(bad, [], InvalidTableIdsError)


def test_a_part_over_a_kept_table_its_statement_never_reads_is_refused() -> None:
    """Top products reads SalesOrderLine, Product and SalesOrder, never Payment: a part
    over Payment would carry its statistics onto another table."""
    text = "SELECT [Amount] FROM [Payment] WHERE [SalesOrderID] = @o"
    error = _refused(
        ORDERS, [{"from_query_id": TOP_PRODUCTS, "query_text": text}], PartTableNotInStatementError
    )
    assert error.extra == ["trailgear.Payment"]  # type: ignore[attr-defined]


def test_one_refused_part_refuses_the_cut() -> None:
    """A valid part next to a refused one: no cut, nothing kept."""
    good = {"from_query_id": TOP_PRODUCTS, "query_text": ORDERS_PART}
    bad = {
        "from_query_id": BUYERS_BY_REGION,
        "query_text": "SELECT c.[Region] FROM [CustomerAddress] c",
    }
    _refused(ORDERS, [good, bad], PartTableOutsideCutError)


def test_a_result_read_from_json_is_cut_only_with_its_artifacts() -> None:
    text = _whole().model_dump_json()
    with pytest.raises(CutSourceError):
        cut_collection(AssessmentResult.model_validate_json(text), table_ids=ORDERS)
    with_artifacts = assess(
        {
            "database_name": "trailgear",
            "collection": _collection(),
            "options": {"run_through": "analysis", "include_artifacts": True},
        }
    )
    read = AssessmentResult.model_validate_json(with_artifacts.model_dump_json())
    assert (
        cut_collection(read, table_ids=ORDERS).query_ids
        == cut_collection(_whole(), table_ids=ORDERS).query_ids
    )


def test_the_cut_source_is_not_part_of_the_result() -> None:
    result = _whole()
    assert "collector_output" not in result.model_dump_json()
    assert result == AssessmentResult.model_validate_json(result.model_dump_json())


# --------------------------------------------------------------------------- determinism


def test_the_same_input_gives_the_same_cut() -> None:
    parts = [
        {"from_query_id": TOP_PRODUCTS, "query_text": ORDERS_PART},
        {
            "from_query_id": ORDER_LINES,
            "query_text": "SELECT l.[Quantity] FROM [SalesOrderLine] l WHERE l.[SalesOrderID] = @o",
        },
        {"from_query_id": PLACE_ORDER, "query_text": PLACE_ORDER_PART},
    ]
    first = cut_collection(_whole(), table_ids=ORDERS, parts=parts)
    again = cut_collection(_whole(), table_ids=list(reversed(ORDERS)), parts=list(reversed(parts)))
    assert first == again
    # A separate run of the same collection gives the same ids and statements.
    other_run = assess(
        {
            "database_name": "trailgear",
            "collection": _collection(),
            "options": {"run_through": "analysis"},
        }
    )
    third = cut_collection(other_run, table_ids=ORDERS, parts=parts)
    assert (third.table_ids, third.query_ids, third.scope) == (
        first.table_ids,
        first.query_ids,
        first.scope,
    )
    # The collection differs only in the run it was cut from: its job id and the time
    # that run read the collection (assess.md says so).
    differ = {key for key in first.collection if first.collection[key] != third.collection[key]}
    assert differ <= {"job_id", "metadata"}
    first_meta, third_meta = first.collection["metadata"], third.collection["metadata"]
    # collection_duration_seconds is run timing too, so a cut leaves it out (null) rather
    # than carry a value that differs between runs.
    assert first_meta["collection_duration_seconds"] is None
    assert third_meta["collection_duration_seconds"] is None
    assert {k for k in first_meta if first_meta[k] != third_meta[k]} <= {"collection_timestamp"}


def test_the_cut_is_frozen_and_leaves_the_result_unchanged() -> None:
    whole = _whole()
    before = copy.deepcopy(whole.cut_source())
    cut = cut_collection(
        whole, table_ids=ORDERS, parts=[{"from_query_id": TOP_PRODUCTS, "query_text": ORDERS_PART}]
    )
    assert whole.cut_source() == before
    with pytest.raises(Exception, match="frozen"):
        cut.table_ids = []  # type: ignore[misc]


# --------------------------------------------------------------------------- the id rule


#: Every form of a collected statement id (assess.md, Statement ids), one example each.
COLLECTED_IDS = [
    "d0c4b1f0e2a3c5b7d9e1f3a5c7e9b1d3f5a7c9e1b3d5f7a9c1e3b5d7f9a1c3e5",  # MySQL 8.0 DIGEST
    "-3141592653589793238",  # PostgreSQL queryid
    "3141592653589793238",
    "0x1A2B3C4D5E6F7081",  # SQL Server query_hash
    "fms_1234567890123456789",  # Oracle FORCE_MATCHING_SIGNATURE
    "a1b2c3d4e5f6g",  # Oracle SQL_ID
    "126679dc6aaed0c3",  # SHA-256 fallback  # pragma: allowlist secret
]
COLLECTED_ID_FORMS = re.compile(
    r"^(?:[0-9a-fA-F]+|-?[0-9]+|0x[0-9A-Fa-f]+|fms_[0-9]+|[0-9a-z]{13})$"
)


@cache
def _sample_ids() -> list[str]:
    ids: list[str] = []
    for sample in ("wordpress", "discourse", "trailgear"):
        collection = load_collection(REPO / "docs" / "examples" / sample / f"{sample}.zip")
        result = assess(
            {
                "database_name": sample,
                "collection": collection,
                "options": {"run_through": "analysis"},
            }
        )
        ids += [s.query_id for s in result.statements]
    return ids


def test_a_part_id_never_has_a_collected_ids_form() -> None:
    from src.assessment.part_ids import from_query_id_of, part_query_id

    assert all(COLLECTED_ID_FORMS.match(i) for i in COLLECTED_IDS)
    collected = COLLECTED_IDS + _sample_ids()
    assert all(COLLECTED_ID_FORMS.match(i) for i in _sample_ids())
    for qid in collected:
        assert from_query_id_of(qid) is None, qid
        for tables in (["a.b"], ["a.b", "c.d"], []):
            part = part_query_id(qid, tables)
            assert not COLLECTED_ID_FORMS.match(part), part
            assert part not in collected
            assert from_query_id_of(part) == qid
    # Deterministic, order-free over the tables, and distinct per table set.
    assert part_query_id("q", ["b", "a"]) == part_query_id("q", ["a", "b", "a"])
    assert part_query_id("q", ["a"]) != part_query_id("q", ["b"])
    assert part_query_id("q1", ["a"]) != part_query_id("q2", ["a"])


def test_a_part_id_already_in_the_collection_is_refused() -> None:
    """Only a hand-edited collection can hold one: the id is refused, never reused."""
    collector_output = copy.deepcopy(whole_collector_output())
    patterns = collector_output["queries"]["query_patterns"]
    taken = _expected_part_id(TOP_PRODUCTS, ORDERS)
    planted = copy.deepcopy(next(p for p in patterns if p["query_id"] == PRODUCT_SEARCH))
    patterns.append({**planted, "query_id": taken})
    result = assess(
        {
            "database_name": "trailgear",
            "collection": collector_output,
            "options": {"run_through": "analysis"},
        }
    )
    assert taken in {s.query_id for s in result.statements}
    with pytest.raises(PartIdCollisionError):
        cut_collection(
            result,
            table_ids=ORDERS,
            parts=[{"from_query_id": TOP_PRODUCTS, "query_text": ORDERS_PART}],
        )


# --------------------------------------------------------------------------- round trip


@pytest.mark.parametrize("run_through", ["analysis", "reality_check"])
def test_assess_takes_the_cut_back(run_through: str) -> None:
    cut = cut_collection(
        _whole(),
        table_ids=ORDERS,
        parts=[{"from_query_id": TOP_PRODUCTS, "query_text": ORDERS_PART}],
    )
    (part_id,) = cut.scope.rewritten_query_ids
    result = _assess_cut(cut, run_through)
    assert result.summary.run_through_reached == run_through
    assert sorted(s.query_id for s in result.statements) == sorted(cut.query_ids)
    if run_through == "analysis":
        assert [s.query_id for s in result.statements] == cut.query_ids
    else:
        assert all(s.engine for s in result.statements)
    by_id = {s.query_id: s for s in result.statements}
    assert by_id[part_id].rewritten_by_caller
    assert by_id[part_id].from_query_id == TOP_PRODUCTS
    assert sorted(by_id[part_id].tables) == ["trailgear.SalesOrder", "trailgear.SalesOrderLine"]
    others = [s for s in result.statements if s.query_id != part_id]
    assert not any(s.rewritten_by_caller or s.from_query_id for s in others)
    assert result.scope == cut.scope
    assert [t.table_id for t in result.summary.table_facts] == cut.table_ids
    # The kept statements keep their scores: the cut changes no statement it keeps whole.
    whole = {s.query_id: s for s in _whole().statements}
    if run_through == "analysis":
        for s in others:
            assert s.query_text == whole[s.query_id].query_text
            assert s.calls_per_second == whole[s.query_id].calls_per_second


def test_no_statement_of_a_whole_collection_has_a_from_query_id() -> None:
    assert not any(s.from_query_id for s in _whole().statements)
    document = json.loads(_whole().model_dump_json())
    assert all(s["from_query_id"] is None for s in document["statements"])


# --------------------------------------------------------------------------- versions


@pytest.mark.parametrize("version", ["1.0", "1.1", "1.2"])
def test_a_cut_assessed_for_a_retired_version_is_refused(version: str) -> None:
    """A cut's request names 1.3 or nothing: 1.0 to 1.2 are retired (#1641)."""
    cut = cut_collection(
        _whole(),
        table_ids=ORDERS,
        parts=[{"from_query_id": TOP_PRODUCTS, "query_text": ORDERS_PART}],
    )
    with pytest.raises(RetiredContractVersionError, match="supports contract 1.3 only"):
        _assess_cut(cut, "analysis", version)
    text = _assess_cut(cut, "analysis", "1.3").model_dump_json()
    document = json.loads(text)
    assert document["contract_version"] == "1.3"
    (part_id,) = cut.scope.rewritten_query_ids
    assert [s["from_query_id"] for s in document["statements"] if s["rewritten_by_caller"]] == [
        TOP_PRODUCTS
    ]
    assert [s["query_id"] for s in document["statements"] if s["rewritten_by_caller"]] == [part_id]
    assert "unplaced_statements" in document["scope"]
    assert AssessmentResult.model_validate_json(text).model_dump_json() == text


def test_the_published_schemas_carry_the_1_3_additions() -> None:
    result = json.loads(rcs.schema_path("assess_result").read_text("utf-8"))
    statement = result["$defs"]["StatementResult"]
    assert "from_query_id" in statement["properties"]
    # The result schema describes 1.3 only: the library always writes both.
    assert "from_query_id" in statement["required"]
    scope_def = result["$defs"]["WorkloadScope"]
    assert "unplaced_statements" in scope_def["properties"]
    assert "unplaced_statements" in scope_def["required"]
    request = json.loads(rcs.schema_path("assess_request").read_text("utf-8"))
    scope = request["$defs"]["WorkloadScope"]["properties"]["dropped_foreign_keys"]
    assert "referenced_table_id" in scope["description"]
    collection = request["properties"]["collection"]["description"]
    assert "any version the library reads" in collection and "3.x" not in collection
    cut = cut_collection(
        _whole(),
        table_ids=ORDERS,
        parts=[{"from_query_id": TOP_PRODUCTS, "query_text": ORDERS_PART}],
    )
    document = json.loads(_assess_cut(cut, "analysis").model_dump_json())
    Draft202012Validator(result).validate(document)
    Draft202012Validator(request).validate(
        {
            "database_name": "orders",
            "collection": cut.collection,
            "scope": cut.scope.model_dump(mode="json"),
        }
    )


# --------------------------------------------------------------------------- an earlier cut


@cache
def _saved() -> dict[str, Any]:
    saved: dict[str, Any] = json.loads(SAVED_CUT.read_text("utf-8"))
    return saved


@pytest.mark.parametrize("run_through", ["analysis", "reality_check"])
def test_a_cut_saved_by_an_earlier_release_is_accepted(run_through: str) -> None:
    saved = _saved()
    assert saved["contract_version"] == "1.3"
    result = _assess_cut(saved, run_through)
    assert sorted(s.query_id for s in result.statements) == sorted(saved["query_ids"])
    (part_id,) = saved["scope"]["rewritten_query_ids"]
    assert part_id == _expected_part_id(OPEN_TICKETS, SUPPORT)
    by_id = {s.query_id: s for s in result.statements}
    assert by_id[part_id].rewritten_by_caller
    assert by_id[part_id].from_query_id == OPEN_TICKETS
    assert by_id[part_id].tables == ["trailgear.Ticket"]
    assert [t.table_id for t in result.summary.table_facts] == saved["table_ids"]
    assert result.scope is not None
    assert result.scope.model_dump(mode="json") == saved["scope"]


def test_the_saved_cut_is_what_cut_collection_writes_today() -> None:
    """The fixture is a real cut: today's cut of the same service gives the same ids
    and scope. The fixture is never regenerated: when this test fails because a later
    release cuts differently, save that release's cut as a new fixture next to this one
    and keep this one, so a cut from every earlier release stays tested."""
    saved = _saved()
    cut = cut_collection(
        _whole(),
        table_ids=SUPPORT,
        parts=[{"from_query_id": OPEN_TICKETS, "query_text": SUPPORT_PART}],
    )
    assert cut.table_ids == saved["table_ids"]
    assert cut.query_ids == saved["query_ids"]
    assert cut.scope.model_dump(mode="json") == saved["scope"]


# --------------------------------------------------------------------------- literal redaction


def test_a_parts_literals_are_redacted_as_collected_text_is() -> None:
    """A part's text goes through the collected statements' literal redaction (#1625)."""
    text = (
        "SELECT t.[TicketID] FROM [Ticket] t WHERE t.[Subject] = N'Ottoline Brackenbury' "
        "AND t.[CustomerID] = 412345"
    )
    cut = cut_collection(
        _whole(), table_ids=SUPPORT, parts=[{"from_query_id": OPEN_TICKETS, "query_text": text}]
    )
    (part_id,) = cut.scope.rewritten_query_ids
    stored = _rows(cut)[part_id]["query_text"]
    assert "Ottoline" not in stored and "412345" not in stored


# --------------------------------------------------------------------------- unplaced statements


@cache
def _whole_sample(sample: str) -> AssessmentResult:
    collection = load_collection(REPO / "docs" / "examples" / sample / f"{sample}.zip")
    return assess(
        {"database_name": sample, "collection": collection, "options": {"run_through": "analysis"}}
    )


@pytest.mark.parametrize(
    "service", [ORDERS, CATALOG, SUPPORT], ids=["orders", "catalog", "support"]
)
def test_every_cut_lists_the_statements_it_cannot_place(service: list[str]) -> None:
    """A procedure call, a sequence read, a function call and a session command read no
    table: no cut keeps them, and each cut says so, never leaving them out silently."""
    cut = cut_collection(_whole(), table_ids=service)
    assert [(u.query_id, u.reason) for u in cut.scope.unplaced_statements] == [
        (qid, "no_tables_read") for qid in TRAILGEAR_UNPLACED
    ]
    assert not set(TRAILGEAR_UNPLACED) & set(cut.query_ids)
    unread = {s.query_id for s in _whole().statements if s.tables == ["unknown"]}
    assert unread == set(TRAILGEAR_UNPLACED)


def test_a_statement_over_tables_the_collection_lacks_is_unplaced_with_its_reason() -> None:
    """PostgreSQL: system catalog reads read tables the collection does not hold; COMMIT
    reads none."""
    whole = _whole_sample("discourse")
    cut = cut_collection(whole, table_ids=["discourse.users"])
    reasons = {u.query_id: u.reason for u in cut.scope.unplaced_statements}
    texts = {s.query_id: s.query_text or "" for s in whole.statements}
    catalog = next(q for q, t in texts.items() if "FROM pg_type as t" in t)
    commit = next(q for q, t in texts.items() if t.strip() == "COMMIT")
    assert reasons[catalog] == "tables_not_collected"
    assert reasons[commit] == "no_tables_read"
    # Each unplaced statement reads no collected table or view.
    collected = {t.table_id for t in whole.summary.table_facts} | {"badge_posts"}
    tables = {s.query_id: s.tables for s in whole.statements}
    assert all(not collected & set(tables[q]) for q in reasons)


def test_a_part_can_place_a_statement_the_cut_cannot() -> None:
    """The caller's part names the tables of a procedure call; it inherits the call's
    statistics, and the usual checks apply."""
    cut = cut_collection(
        _whole(),
        table_ids=ORDERS,
        parts=[{"from_query_id": PLACE_ORDER, "query_text": PLACE_ORDER_PART}],
    )
    part_id = _expected_part_id(PLACE_ORDER, ORDERS)
    assert cut.scope.rewritten_query_ids == [part_id]
    assert PLACE_ORDER not in {u.query_id for u in cut.scope.unplaced_statements}
    assert len(cut.scope.unplaced_statements) == len(TRAILGEAR_UNPLACED) - 1
    result = _assess_cut(cut, "reality_check")
    placed = {s.query_id: s for s in result.statements}[part_id]
    assert placed.from_query_id == PLACE_ORDER and placed.rewritten_by_caller and placed.engine
    assert (
        placed.calls_per_second
        == {s.query_id: s for s in _whole().statements}[PLACE_ORDER].calls_per_second
    )
    outside = "INSERT INTO [Customer] ([Email]) VALUES (@Email)"
    _refused(
        ORDERS, [{"from_query_id": PLACE_ORDER, "query_text": outside}], PartTableOutsideCutError
    )


# --------------------------------------------------------------------------- views


def test_a_view_the_collection_lists_can_be_kept() -> None:
    catalog = [*CATALOG, "vw_ActiveProduct"]
    effective_price = "0x8BBAEC4CCECE4C82"  # SELECT dbo.ufn_EffectivePrice(...): unplaced
    text = "SELECT v.[Name] FROM [vw_ActiveProduct] v WHERE v.[ProductID] = @ProductID"
    cut = cut_collection(
        _whole(), table_ids=catalog, parts=[{"from_query_id": effective_price, "query_text": text}]
    )
    assert cut.table_ids == sorted(catalog)
    (part_id,) = cut.scope.rewritten_query_ids
    assert _rows(cut)[part_id]["tables_accessed"] == ["vw_ActiveProduct"]
    # A crossing statement's part over the view counts as the view's base tables
    # (Product and Brand): top products never reads Brand, so the part is refused.
    over_view = "SELECT v.[ProductID], v.[Name] FROM [vw_ActiveProduct] v"
    error = _refused(
        catalog,
        [{"from_query_id": TOP_PRODUCTS, "query_text": over_view}],
        PartTableNotInStatementError,
    )
    assert error.extra == ["trailgear.Brand"]  # type: ignore[attr-defined]
    result = _assess_cut(cut, "analysis")
    assert [t.table_id for t in result.summary.table_facts] == sorted(CATALOG)
    assert {s.query_id: s.tables for s in result.statements}[part_id] == ["vw_ActiveProduct"]
    # Without the view in table_ids, a part over it names a table outside the cut.
    _refused(
        CATALOG, [{"from_query_id": effective_price, "query_text": text}], PartTableOutsideCutError
    )
    _refused(["vw_Nope"], [], UnknownTableError)


def test_a_view_over_another_services_tables_is_refused() -> None:
    """vw_ActiveProduct reads Product and Brand (its definition, parsed in the source
    dialect: the collection gives no referenced_tables), both catalog tables: kept with
    the orders tables it would pull them back in."""
    whole = whole_collector_output()
    (view,) = whole["database_schema"]["views"]
    assert view["view_id"] == "vw_ActiveProduct" and not view.get("referenced_tables")
    error = _refused([*ORDERS, "vw_ActiveProduct"], [], ViewOutsideCutError)
    assert error.view_id == "vw_ActiveProduct"  # type: ignore[attr-defined]
    assert sorted(error.outside) == ["trailgear.Brand", "trailgear.Product"]  # type: ignore[attr-defined]
    # With only one of its tables kept, it is refused too.
    error = _refused(["trailgear.Product", "vw_ActiveProduct"], [], ViewOutsideCutError)
    assert error.outside == ["trailgear.Brand"]  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- the dialect


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ('SELECT "Quantity" FROM "SalesOrderLine" WHERE "SalesOrderID" = $1 LIMIT 5', "$n"),
        ("SELECT [Quantity] FROM [SalesOrderLine] WHERE [SalesOrderID] = $1", "$n"),
        ("SELECT [Quantity]::int FROM [SalesOrderLine] WHERE [SalesOrderID] = @o", "::"),
        ("SELECT [Quantity] FROM [SalesOrderLine] WHERE [Note] ILIKE @n", "ILIKE"),
        ("SELECT [Quantity] FROM [SalesOrderLine] WHERE ROWNUM <= 5", "ROWNUM"),
        ("SELECT [Quantity] FROM [SalesOrderLine] LIMIT 5", "LIMIT"),
        ("SELECT `Quantity` FROM `SalesOrderLine` WHERE `SalesOrderID` = @o", "backtick"),
        ("UPDATE [SalesOrder] SET [Status] = @s RETURNING [SalesOrderID]", "RETURNING"),
        (
            "INSERT INTO [SalesOrder] ([SalesOrderID]) VALUES (@o) ON CONFLICT DO NOTHING",
            "ON CONFLICT",
        ),
        ("SELECT [Note] || [Status] FROM [SalesOrder]", "||"),
        ("SELECT [SalesOrderID] FROM [SalesOrder] WHERE [OrderDate] < NOW()", "NOW()"),
        ("SELECT IFNULL([Note], '') FROM [SalesOrder]", "IFNULL"),
        ("SELECT NVL([Note], '') FROM [SalesOrder]", "NVL"),
    ],
    ids=[
        "postgres-quoting",
        "dollar-param",
        "cast",
        "ilike",
        "rownum",
        "limit",
        "backticks",
        "returning",
        "on-conflict",
        "concat",
        "now",
        "ifnull",
        "nvl",
    ],
)
def test_another_dialects_syntax_is_refused_for_sql_server(text: str, found: str) -> None:
    error = _refused(
        ORDERS, [{"from_query_id": TOP_PRODUCTS, "query_text": text}], PartDialectError
    )
    assert isinstance(error, PartParseError)
    assert found in error.found  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "text",
    [
        # "..." names: T-SQL reads them as names under QUOTED_IDENTIFIER ON, the default.
        'SELECT l."Quantity" FROM "SalesOrderLine" l WHERE l."SalesOrderID" = @o',
        # A CLR type's static method, and a securable class.
        "SELECT geography::Point(@lat, @lon, 4326).STDistance([Location]) FROM [Shipment]",
        "SELECT HAS_PERMS_BY_NAME('sales', 'SCHEMA', 'SELECT') FROM [SalesOrder] WHERE SCHEMA::sales IS NULL",
        # A money literal.
        "SELECT [SalesOrderID] FROM [SalesOrder] WHERE [TotalDue] > $19.99",
        "SELECT ISNULL([Note], N'') + [Status], GETDATE() FROM [SalesOrder]",
    ],
    ids=["quoted-identifier", "clr-method", "securable", "money", "tsql-functions"],
)
def test_valid_t_sql_is_not_foreign(text: str) -> None:
    from src.assessment.cut import foreign_syntax

    assert foreign_syntax(text, "sqlserver") is None


def test_a_part_with_quoted_identifiers_is_accepted_for_sql_server() -> None:
    text = (
        'SELECT l."Quantity" FROM "SalesOrderLine" l JOIN "SalesOrder" o '
        'ON o."SalesOrderID" = l."SalesOrderID" WHERE o."TotalDue" > $19.99'
    )
    cut = cut_collection(
        _whole(), table_ids=ORDERS, parts=[{"from_query_id": TOP_PRODUCTS, "query_text": text}]
    )
    (part_id,) = cut.scope.rewritten_query_ids
    assert sorted(_rows(cut)[part_id]["tables_accessed"]) == [
        "trailgear.SalesOrder",
        "trailgear.SalesOrderLine",
    ]


def test_sql_servers_own_syntax_is_accepted() -> None:
    """TOP, @parameters and [names] are SQL Server's; a foreign word in a string or a
    comment is not SQL."""
    text = (
        "SELECT TOP (5) l.[Quantity] FROM [SalesOrderLine] l JOIN [SalesOrder] o "
        "ON o.[SalesOrderID] = l.[SalesOrderID] WHERE o.[Note] = 'LIMIT 5 ILIKE $1' /* ROWNUM :: */"
    )
    cut = cut_collection(
        _whole(), table_ids=ORDERS, parts=[{"from_query_id": TOP_PRODUCTS, "query_text": text}]
    )
    assert cut.scope.rewritten_query_ids == [_expected_part_id(TOP_PRODUCTS, ORDERS)]


@pytest.mark.parametrize(
    ("engine", "text", "found"),
    [
        ("postgresql", "SELECT TOP 5 id FROM users", "TOP"),
        ("postgresql", "SELECT id FROM users WHERE name = @name", "@parameter"),
        ("postgresql", "SELECT [id] FROM [users]", "bracket"),
        ("postgresql", "SELECT `id` FROM users", "backtick"),
        ("postgresql", "SELECT id FROM users WHERE ROWNUM <= 5", "ROWNUM"),
        ("mysql", "SELECT [ID] FROM [wp_posts]", "bracket"),
        ("mysql", "SELECT TOP (5) ID FROM wp_posts", "TOP"),
        ("mysql", "SELECT ID FROM wp_posts WHERE ID = $1", "$n"),
        ("mysql", "SELECT ID::int FROM wp_posts", "::"),
        ("mysql", "SELECT ID FROM wp_posts WHERE post_title ILIKE ?", "ILIKE"),
        ("mariadb", "SELECT ID FROM wp_posts WHERE ROWNUM <= 5", "ROWNUM"),
        ("oracle", "SELECT id FROM users LIMIT 5", "LIMIT"),
        ("oracle", "SELECT `id` FROM users", "backtick"),
        ("oracle", "SELECT TOP 5 id FROM users", "TOP"),
        ("oracle", "SELECT id FROM users WHERE id = @id", "@parameter"),
        ("oracle", "SELECT id FROM users WHERE id = $1", "$n"),
        ("oracle", "SELECT id::int FROM users", "::"),
        ("oracle", "SELECT [id] FROM users", "bracket"),
        ("postgresql", "SELECT IFNULL(name, $1) FROM users", "IFNULL"),
        ("postgresql", "SELECT NVL(name, $1) FROM users", "NVL"),
        ("postgresql", "SELECT ISNULL(name, $1) FROM users", "ISNULL"),
        ("postgresql", "SELECT id FROM users WHERE created_at < GETDATE()", "GETDATE"),
        ("mysql", "UPDATE wp_posts SET post_status = ? RETURNING ID", "RETURNING"),
        ("mysql", "INSERT INTO wp_posts (ID) VALUES (?) ON CONFLICT DO NOTHING", "ON CONFLICT"),
        ("mysql", "SELECT NVL(post_title, ?) FROM wp_posts", "NVL"),
        ("mysql", "SELECT ID FROM wp_posts WHERE post_date < GETDATE()", "GETDATE"),
        ("mariadb", "INSERT INTO wp_posts (ID) VALUES (?) ON CONFLICT DO NOTHING", "ON CONFLICT"),
        ("oracle", "INSERT INTO users (id) VALUES (:1) ON CONFLICT DO NOTHING", "ON CONFLICT"),
        ("oracle", "SELECT id FROM users WHERE created_at < NOW()", "NOW()"),
        ("oracle", "SELECT IFNULL(name, :1) FROM users", "IFNULL"),
        ("oracle", "SELECT ISNULL(name, :1) FROM users", "ISNULL"),
        ("oracle", "SELECT id FROM users WHERE created_at < GETDATE()", "GETDATE"),
    ],
)
def test_another_dialects_syntax_is_found_for_each_source(
    engine: str, text: str, found: str
) -> None:
    from src.assessment.cut import foreign_syntax

    what = foreign_syntax(text, engine)
    assert what is not None and found in what, what


@pytest.mark.parametrize(
    ("engine", "text"),
    [
        (
            "postgresql",
            'SELECT "id", tags[1], ARRAY[2] FROM users WHERE name ILIKE $1 AND id::text = $2 LIMIT 5',
        ),
        ("postgresql", "SELECT id FROM users WHERE doc @> $1 AND tsv @@ to_tsquery($2)"),
        ("mysql", "SELECT `ID` FROM `wp_posts` WHERE post_author = @uid LIMIT 5"),
        ("oracle", 'SELECT "ID" FROM users@remote WHERE ROWNUM <= :1'),
        ("sqlserver", "SELECT TOP (5) [Id] FROM [Users] WHERE [Name] = @n"),
        ("db2", "SELECT id FROM users FETCH FIRST 5 ROWS ONLY"),
        (
            "postgresql",
            "INSERT INTO users (id) VALUES ($1) ON CONFLICT DO NOTHING RETURNING id || $2",
        ),
        ("postgresql", "SELECT id FROM users WHERE created_at < NOW()"),
        ("mysql", "SELECT IFNULL(post_title, ?), NOW() FROM wp_posts"),
        ("mariadb", "UPDATE wp_posts SET post_status = ? RETURNING NVL(post_title, ?)"),
        ("oracle", "UPDATE users SET name = :1 || :2 RETURNING NVL(id, 0) INTO :3"),
    ],
)
def test_a_sources_own_syntax_is_not_foreign(engine: str, text: str) -> None:
    from src.assessment.cut import foreign_syntax

    assert foreign_syntax(text, engine) is None


def test_a_mysql_cut_refuses_sql_server_syntax() -> None:
    whole = _whole_sample("wordpress")
    keep = ["wordpress.wp_posts"]
    crossing = next(
        s for s in whole.statements if "wordpress.wp_posts" in s.tables and len(set(s.tables)) > 1
    )
    good = "SELECT `ID` FROM `wp_posts` WHERE `post_status` = ? LIMIT ?"
    bad = "SELECT TOP (5) [ID] FROM [wp_posts] WHERE [post_status] = @s"
    cut = cut_collection(
        whole, table_ids=keep, parts=[{"from_query_id": crossing.query_id, "query_text": good}]
    )
    assert cut.scope.rewritten_query_ids == [_expected_part_id(crossing.query_id, keep)]
    with pytest.raises(PartDialectError):
        cut_collection(
            whole, table_ids=keep, parts=[{"from_query_id": crossing.query_id, "query_text": bad}]
        )


# --------------------------------------------------------------------------- unknown values


def test_a_cuts_result_keeps_an_unknown_row_count_unknown() -> None:
    """Payment's row count is unknown to the collection: the whole result has no row
    count and no average for FK_Payment_Order, and neither does the cut's (never 0 or
    0.0 from a recorded default)."""
    collection = copy.deepcopy(_collection())
    tables = {t["table_name"]: t for t in collection["tables"]}
    tables["Payment"]["row_count"] = None
    whole = assess(
        {
            "database_name": "trailgear",
            "collection": collection,
            "options": {"run_through": "analysis"},
        }
    )
    cut = cut_collection(whole, table_ids=ORDERS)

    def payment(result: AssessmentResult) -> tuple[Any, ...]:
        facts = {t.table_id: t for t in result.summary.table_facts}["trailgear.Payment"]
        (fk,) = [f for f in facts.foreign_keys if f.constraint_name == "FK_Payment_Order"]
        return facts.row_count, fk.avg_children_per_parent, fk.basis

    assert payment(whole) == (None, None, None)
    for run_through in ("analysis", "reality_check"):
        assert payment(_assess_cut(cut, run_through)) == (None, None, None)
    # A known count is the same in both.
    orders = {t.table_id: t for t in _assess_cut(cut, "analysis").summary.table_facts}
    assert orders["trailgear.SalesOrder"].row_count == 2_740_000
