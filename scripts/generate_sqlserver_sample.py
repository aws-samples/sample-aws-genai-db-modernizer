#!/usr/bin/env python3
"""Generate the synthetic SQL Server sample (``docs/examples/trailgear``, #618).

The two other public samples (wordpress on MySQL, discourse on PostgreSQL) are
homogeneous sources. This one is a heterogeneous source: a fictional
outdoor-gear retailer, "Trailgear", on SQL Server, invented here from scratch
(no real schema, data or customer workload). It is shaped to reach, end to end,
the routing paths only a heterogeneous source can reach:

- the Aurora MySQL vs Aurora PostgreSQL pick-one choice for a source with no
  Aurora dialect of its own (``aurora_choice.choose_heterogeneous_engine``,
  #381): Aurora MySQL scores the higher total, within the 5% margin, and the
  workload's own features (two schemas, ``MERGE``, ``NEXT VALUE FOR``, a
  recursive CTE, ``JSON_VALUE``/``OPENJSON``) tip it to Aurora PostgreSQL;
- a co-dependency group tie (``CATEGORY_GROUP``: Aurora PostgreSQL and
  DocumentDB score the group the same, and SQL Server has no
  source-compatible engine to break it, so it is ``tie_unresolved``);
- an individual tie that survives the reality check (the ``dbo.Shipment``
  queries: Aurora PostgreSQL and DynamoDB, both kept), so every deliverable
  lists it as pending the customer's choice;
- hot, bounded reads the cache overlay fronts (``HOT_READS`` and a few
  lookups), which the e2e suite also overrides as a customer would;
- T-SQL shapes: ``TOP``, ``WITH (NOLOCK)``, bracket-quoted names, ``EXEC`` of
  stored procedures, ``OFFSET ... FETCH``, a leading-wildcard ``LIKE``.

Most tables live in ``dbo`` and are named bare (``[Product]``), the way an
application on its default schema writes them. The two ``Audit`` tables are
named schema-qualified (``[Audit].[EventLog]``): the offline collector keys
every table as ``<database>.<table>``, so those queries do not match their
table's per-engine scores and fall back to the all-tables average -- kept on
purpose so the sample shows that gap until it is fixed.

``tests/e2e/test_sqlserver_sample.py`` fails when a change stops the sample
from reaching one of these paths; the numbers that put each query there
(calls per second, rows) were tuned against the pipeline, so retune here
rather than loosening the test. The two tie expectations are the exception:
they assume #656 is answered "no" (a heterogeneous source's chosen Aurora
engine is not source-compatible). If #656 decides otherwise, those ties
resolve onto Aurora PostgreSQL, and the tests' expected outcome changes
rather than the generator. Every cache candidate's traffic is kept clear of
the hot-read floor, so seed jitter never moves a query in or out of the
overlay.

The output is exactly what ``scripts/collect-sqlserver.sql`` writes (the raw
offline format ``scripts/run_collect.py`` parses with
``src/tools/database/offline_parser.py``), including the ``collection_output``
header line ``sqlcmd`` prints before the JSON. Every number comes from a seeded
``random.Random`` and every timestamp is fixed, so the same seed always writes
byte-identical files; ``tests/unit/scripts/test_generate_sqlserver_sample.py``
checks the committed zip matches a fresh run.

Usage::

    uv run python scripts/generate_sqlserver_sample.py            # rewrite the zip
    uv run python scripts/generate_sqlserver_sample.py --check    # exit 1 if stale
    uv run python scripts/generate_sqlserver_sample.py --json out.json

After changing the workload, regenerate the routing snapshot (never hand-edit
it)::

    uv run python -m tests.e2e.routing_snapshot --write --reason "..."
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import random
import sys
import zipfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
SAMPLE = "trailgear"
SEED = 618
OUT_DIR = REPO / "docs" / "examples" / SAMPLE
ZIP_PATH = OUT_DIR / f"{SAMPLE}.zip"
MEMBER = f"{SAMPLE}-collection.json"
# Fixed so the zip is byte-identical across runs (a ZipInfo needs a date).
ZIP_DATE = (2026, 10, 13, 0, 0, 0)
COLLECTED_AT = "2026-10-13 06:00:00.000"
WINDOW_SECONDS = 24 * 3600  # offline_parser reads execution_count as a 24 h window

# ---------------------------------------------------------------------------
# Schema: (schema, table, rows, columns, primary key, foreign keys, indexes)
# A column is (name, sql_server_type, max_length or None, nullable, identity).
# ---------------------------------------------------------------------------

Col = tuple[str, str, int | None, bool, bool]


def _c(name: str, typ: str, length: int | None = None, null: bool = False) -> Col:
    return (name, typ, length, null, False)


def _id(name: str, typ: str = "int") -> Col:
    return (name, typ, None, False, True)


TABLES: list[dict[str, Any]] = [
    # --- Crm -----------------------------------------------------------------
    {
        "schema": "dbo",
        "name": "Customer",
        "rows": 412_000,
        "columns": [
            _id("CustomerID"),
            _c("Email", "nvarchar", 256),
            _c("FirstName", "nvarchar", 80),
            _c("LastName", "nvarchar", 80),
            _c("Phone", "varchar", 32, True),
            _c("Preferences", "nvarchar", None, True),
            _c("CreatedDate", "datetime2"),
            _c("ModifiedDate", "datetime2"),
        ],
        "pk": ["CustomerID"],
        "fks": [],
        "indexes": [("UX_Customer_Email", ["Email"], True)],
    },
    {
        "schema": "dbo",
        "name": "CustomerAddress",
        "rows": 530_000,
        "columns": [
            _id("AddressID"),
            _c("CustomerID", "int"),
            _c("Line1", "nvarchar", 120),
            _c("City", "nvarchar", 80),
            _c("Region", "nvarchar", 40),
            _c("PostalCode", "varchar", 16),
            _c("IsDefault", "bit"),
        ],
        "pk": ["AddressID"],
        "fks": [("FK_CustomerAddress_Customer", "CustomerID", "Customer", "CustomerID")],
        "indexes": [("IX_CustomerAddress_CustomerID", ["CustomerID"], False)],
    },
    {
        "schema": "dbo",
        "name": "LoyaltyAccount",
        "rows": 198_000,
        "columns": [
            _id("LoyaltyAccountID"),
            _c("CustomerID", "int"),
            _c("Tier", "varchar", 16),
            _c("Points", "int"),
            _c("ModifiedDate", "datetime2"),
        ],
        "pk": ["LoyaltyAccountID"],
        "fks": [("FK_LoyaltyAccount_Customer", "CustomerID", "Customer", "CustomerID")],
        "indexes": [("UX_LoyaltyAccount_CustomerID", ["CustomerID"], True)],
    },
    {
        "schema": "dbo",
        "name": "CustomerNote",
        "rows": 61_000,
        "columns": [
            _id("NoteID"),
            _c("CustomerID", "int"),
            _c("Body", "nvarchar", None),
            _c("CreatedBy", "nvarchar", 80),
            _c("CreatedDate", "datetime2"),
        ],
        "pk": ["NoteID"],
        "fks": [("FK_CustomerNote_Customer", "CustomerID", "Customer", "CustomerID")],
        "indexes": [("IX_CustomerNote_CustomerID", ["CustomerID"], False)],
    },
    # --- Catalog -------------------------------------------------------------
    {
        "schema": "dbo",
        "name": "Brand",
        "rows": 140,
        "columns": [_id("BrandID"), _c("Name", "nvarchar", 80), _c("Country", "char", 2)],
        "pk": ["BrandID"],
        "fks": [],
        "indexes": [],
    },
    {
        "schema": "dbo",
        "name": "ProductCategory",
        "rows": 260,
        "columns": [
            _id("CategoryID"),
            _c("ParentCategoryID", "int", None, True),
            _c("Name", "nvarchar", 80),
            _c("Slug", "varchar", 80),
        ],
        "pk": ["CategoryID"],
        "fks": [
            (
                "FK_ProductCategory_Parent",
                "ParentCategoryID",
                "ProductCategory",
                "CategoryID",
            )
        ],
        "indexes": [("IX_ProductCategory_Parent", ["ParentCategoryID"], False)],
    },
    {
        "schema": "dbo",
        "name": "Product",
        "rows": 38_500,
        "columns": [
            _id("ProductID"),
            _c("Sku", "varchar", 32),
            _c("Name", "nvarchar", 200),
            _c("Description", "nvarchar", None, True),
            _c("CategoryID", "int"),
            _c("BrandID", "int"),
            _c("Specs", "nvarchar", None, True),
            _c("IsActive", "bit"),
            _c("ModifiedDate", "datetime2"),
        ],
        "pk": ["ProductID"],
        "fks": [
            ("FK_Product_Category", "CategoryID", "ProductCategory", "CategoryID"),
            ("FK_Product_Brand", "BrandID", "Brand", "BrandID"),
        ],
        "indexes": [
            ("UX_Product_Sku", ["Sku"], True),
            ("IX_Product_CategoryID", ["CategoryID"], False),
        ],
    },
    {
        "schema": "dbo",
        "name": "ProductPrice",
        "rows": 41_000,
        "columns": [
            _id("PriceID"),
            _c("ProductID", "int"),
            _c("ListPrice", "decimal"),
            _c("SalePrice", "decimal", None, True),
            _c("ValidFrom", "datetime2"),
            _c("ValidTo", "datetime2", None, True),
        ],
        "pk": ["PriceID"],
        "fks": [("FK_ProductPrice_Product", "ProductID", "Product", "ProductID")],
        "indexes": [("IX_ProductPrice_ProductID", ["ProductID"], False)],
    },
    {
        "schema": "dbo",
        "name": "ProductReview",
        "rows": 905_000,
        "columns": [
            _id("ReviewID"),
            _c("ProductID", "int"),
            _c("CustomerID", "int"),
            _c("Rating", "tinyint"),
            _c("Title", "nvarchar", 120),
            _c("Body", "nvarchar", None),
            _c("CreatedDate", "datetime2"),
        ],
        "pk": ["ReviewID"],
        "fks": [
            ("FK_ProductReview_Product", "ProductID", "Product", "ProductID"),
            ("FK_ProductReview_Customer", "CustomerID", "Customer", "CustomerID"),
        ],
        "indexes": [("IX_ProductReview_ProductID", ["ProductID"], False)],
    },
    {
        "schema": "dbo",
        "name": "ProductImage",
        "rows": 152_000,
        "columns": [
            _id("ImageID"),
            _c("ProductID", "int"),
            _c("Url", "varchar", 400),
            _c("SortOrder", "smallint"),
        ],
        "pk": ["ImageID"],
        "fks": [("FK_ProductImage_Product", "ProductID", "Product", "ProductID")],
        "indexes": [("IX_ProductImage_ProductID", ["ProductID"], False)],
    },
    # --- Sales ---------------------------------------------------------------
    {
        "schema": "dbo",
        "name": "SalesOrder",
        "rows": 2_740_000,
        "columns": [
            _id("SalesOrderID", "bigint"),
            _c("OrderNumber", "varchar", 20),
            _c("CustomerID", "int"),
            _c("Status", "varchar", 16),
            _c("OrderDate", "datetime2"),
            _c("TotalDue", "decimal"),
            _c("ShipToAddressID", "int"),
            _c("PromotionID", "int", None, True),
        ],
        "pk": ["SalesOrderID"],
        "fks": [
            ("FK_SalesOrder_Customer", "CustomerID", "Customer", "CustomerID"),
            ("FK_SalesOrder_Address", "ShipToAddressID", "CustomerAddress", "AddressID"),
            ("FK_SalesOrder_Promotion", "PromotionID", "Promotion", "PromotionID"),
        ],
        "indexes": [
            ("UX_SalesOrder_OrderNumber", ["OrderNumber"], True),
            ("IX_SalesOrder_CustomerID_OrderDate", ["CustomerID", "OrderDate"], False),
            ("IX_SalesOrder_Status", ["Status"], False),
        ],
    },
    {
        "schema": "dbo",
        "name": "SalesOrderLine",
        "rows": 8_310_000,
        "columns": [
            _id("SalesOrderLineID", "bigint"),
            _c("SalesOrderID", "bigint"),
            _c("ProductID", "int"),
            _c("Quantity", "int"),
            _c("UnitPrice", "decimal"),
        ],
        "pk": ["SalesOrderLineID"],
        "fks": [
            ("FK_SalesOrderLine_Order", "SalesOrderID", "SalesOrder", "SalesOrderID"),
            ("FK_SalesOrderLine_Product", "ProductID", "Product", "ProductID"),
        ],
        "indexes": [("IX_SalesOrderLine_SalesOrderID", ["SalesOrderID"], False)],
    },
    {
        "schema": "dbo",
        "name": "Payment",
        "rows": 2_810_000,
        "columns": [
            _id("PaymentID", "bigint"),
            _c("SalesOrderID", "bigint"),
            _c("Method", "varchar", 16),
            _c("Amount", "decimal"),
            _c("AuthCode", "varchar", 40, True),
            _c("CapturedDate", "datetime2", None, True),
        ],
        "pk": ["PaymentID"],
        "fks": [("FK_Payment_Order", "SalesOrderID", "SalesOrder", "SalesOrderID")],
        "indexes": [("IX_Payment_SalesOrderID", ["SalesOrderID"], False)],
    },
    {
        "schema": "dbo",
        "name": "Shipment",
        "rows": 2_600_000,
        "columns": [
            _id("ShipmentID", "bigint"),
            _c("SalesOrderID", "bigint"),
            _c("Carrier", "varchar", 24),
            _c("TrackingNumber", "varchar", 40),
            _c("ShippedDate", "datetime2", None, True),
            _c("DeliveredDate", "datetime2", None, True),
        ],
        "pk": ["ShipmentID"],
        "fks": [("FK_Shipment_Order", "SalesOrderID", "SalesOrder", "SalesOrderID")],
        "indexes": [("IX_Shipment_TrackingNumber", ["TrackingNumber"], False)],
    },
    {
        "schema": "dbo",
        "name": "Promotion",
        "rows": 320,
        "columns": [
            _id("PromotionID"),
            _c("Code", "varchar", 24),
            _c("DiscountPct", "decimal"),
            _c("StartsOn", "date"),
            _c("EndsOn", "date"),
        ],
        "pk": ["PromotionID"],
        "fks": [],
        "indexes": [("UX_Promotion_Code", ["Code"], True)],
    },
    {
        "schema": "dbo",
        "name": "OrderStatusHistory",
        "rows": 9_400_000,
        "columns": [
            _id("HistoryID", "bigint"),
            _c("SalesOrderID", "bigint"),
            _c("Status", "varchar", 16),
            _c("ChangedDate", "datetime2"),
        ],
        "pk": ["HistoryID"],
        "fks": [("FK_OrderStatusHistory_Order", "SalesOrderID", "SalesOrder", "SalesOrderID")],
        "indexes": [("IX_OrderStatusHistory_SalesOrderID", ["SalesOrderID"], False)],
    },
    # --- Inventory -----------------------------------------------------------
    {
        "schema": "dbo",
        "name": "Warehouse",
        "rows": 12,
        "columns": [_id("WarehouseID"), _c("Code", "char", 4), _c("Name", "nvarchar", 80)],
        "pk": ["WarehouseID"],
        "fks": [],
        "indexes": [],
    },
    {
        "schema": "dbo",
        "name": "StockLevel",
        "rows": 410_000,
        "columns": [
            _c("WarehouseID", "int"),
            _c("ProductID", "int"),
            _c("OnHand", "int"),
            _c("Reserved", "int"),
            _c("ModifiedDate", "datetime2"),
        ],
        "pk": ["WarehouseID", "ProductID"],
        "fks": [
            ("FK_StockLevel_Warehouse", "WarehouseID", "Warehouse", "WarehouseID"),
            ("FK_StockLevel_Product", "ProductID", "Product", "ProductID"),
        ],
        "indexes": [],
    },
    {
        "schema": "dbo",
        "name": "StockMovement",
        "rows": 14_200_000,
        "columns": [
            _id("MovementID", "bigint"),
            _c("WarehouseID", "int"),
            _c("ProductID", "int"),
            _c("Delta", "int"),
            _c("Reason", "varchar", 24),
            _c("MovedDate", "datetime2"),
        ],
        "pk": ["MovementID"],
        "fks": [("FK_StockMovement_Product", "ProductID", "Product", "ProductID")],
        "indexes": [("IX_StockMovement_ProductID_MovedDate", ["ProductID", "MovedDate"], False)],
    },
    {
        "schema": "dbo",
        "name": "Supplier",
        "rows": 410,
        "columns": [
            _id("SupplierID"),
            _c("Name", "nvarchar", 120),
            _c("ContactEmail", "nvarchar", 256),
        ],
        "pk": ["SupplierID"],
        "fks": [],
        "indexes": [],
    },
    {
        "schema": "dbo",
        "name": "PurchaseOrder",
        "rows": 52_000,
        "columns": [
            _id("PurchaseOrderID"),
            _c("SupplierID", "int"),
            _c("WarehouseID", "int"),
            _c("Status", "varchar", 16),
            _c("OrderedDate", "datetime2"),
        ],
        "pk": ["PurchaseOrderID"],
        "fks": [
            ("FK_PurchaseOrder_Supplier", "SupplierID", "Supplier", "SupplierID"),
            ("FK_PurchaseOrder_Warehouse", "WarehouseID", "Warehouse", "WarehouseID"),
        ],
        "indexes": [],
    },
    {
        "schema": "dbo",
        "name": "PurchaseOrderLine",
        "rows": 390_000,
        "columns": [
            _id("PurchaseOrderLineID"),
            _c("PurchaseOrderID", "int"),
            _c("ProductID", "int"),
            _c("Quantity", "int"),
            _c("UnitCost", "decimal"),
        ],
        "pk": ["PurchaseOrderLineID"],
        "fks": [
            (
                "FK_PurchaseOrderLine_PO",
                "PurchaseOrderID",
                "PurchaseOrder",
                "PurchaseOrderID",
            ),
            ("FK_PurchaseOrderLine_Product", "ProductID", "Product", "ProductID"),
        ],
        "indexes": [],
    },
    # --- Support -------------------------------------------------------------
    {
        "schema": "dbo",
        "name": "Ticket",
        "rows": 288_000,
        "columns": [
            _id("TicketID"),
            _c("CustomerID", "int"),
            _c("SalesOrderID", "bigint", None, True),
            _c("Subject", "nvarchar", 200),
            _c("Status", "varchar", 16),
            _c("Priority", "tinyint"),
            _c("OpenedDate", "datetime2"),
            _c("ClosedDate", "datetime2", None, True),
        ],
        "pk": ["TicketID"],
        "fks": [("FK_Ticket_Customer", "CustomerID", "Customer", "CustomerID")],
        "indexes": [("IX_Ticket_Status", ["Status"], False)],
    },
    {
        "schema": "dbo",
        "name": "TicketMessage",
        "rows": 1_350_000,
        "columns": [
            _id("MessageID", "bigint"),
            _c("TicketID", "int"),
            _c("Author", "nvarchar", 80),
            _c("Body", "nvarchar", None),
            _c("SentDate", "datetime2"),
        ],
        "pk": ["MessageID"],
        "fks": [("FK_TicketMessage_Ticket", "TicketID", "Ticket", "TicketID")],
        "indexes": [("IX_TicketMessage_TicketID", ["TicketID"], False)],
    },
    {
        "schema": "dbo",
        "name": "KnowledgeArticle",
        "rows": 4_100,
        "columns": [
            _id("ArticleID"),
            _c("Title", "nvarchar", 200),
            _c("Body", "nvarchar", None),
            _c("Tags", "nvarchar", 400, True),
            _c("PublishedDate", "datetime2"),
        ],
        "pk": ["ArticleID"],
        "fks": [],
        "indexes": [],
    },
    # --- Ops -----------------------------------------------------------------
    {
        "schema": "dbo",
        "name": "UserSession",
        "rows": 96_000,
        "columns": [
            _c("SessionToken", "char", 64),
            _c("CustomerID", "int", None, True),
            _c("CartJson", "nvarchar", None, True),
            _c("ExpiresAt", "datetime2"),
        ],
        "pk": ["SessionToken"],
        "fks": [],
        "indexes": [],
    },
    {
        "schema": "dbo",
        "name": "FeatureFlag",
        "rows": 85,
        "columns": [_c("FlagKey", "varchar", 64), _c("Enabled", "bit"), _c("Rollout", "tinyint")],
        "pk": ["FlagKey"],
        "fks": [],
        "indexes": [],
    },
    {
        "schema": "dbo",
        "name": "StoreSetting",
        "rows": 140,
        "columns": [_c("SettingKey", "varchar", 64), _c("SettingValue", "nvarchar", 400)],
        "pk": ["SettingKey"],
        "fks": [],
        "indexes": [],
    },
    # --- Audit ---------------------------------------------------------------
    {
        "schema": "Audit",
        "name": "EventLog",
        "rows": 31_000_000,
        "columns": [
            _id("EventID", "bigint"),
            _c("EventType", "varchar", 40),
            _c("EntityID", "bigint"),
            _c("Payload", "nvarchar", None, True),
            _c("EventTime", "datetime2"),
        ],
        "pk": ["EventID"],
        "fks": [],
        "indexes": [("IX_EventLog_EventTime", ["EventTime"], False)],
    },
    {
        "schema": "Audit",
        "name": "PriceChangeLog",
        "rows": 640_000,
        "columns": [
            _id("ChangeID", "bigint"),
            _c("ProductID", "int"),
            _c("OldPrice", "decimal"),
            _c("NewPrice", "decimal"),
            _c("ChangedBy", "nvarchar", 80),
            _c("ChangedDate", "datetime2"),
        ],
        "pk": ["ChangeID"],
        "fks": [],
        "indexes": [],
    },
]

PROCEDURES = [
    ("dbo", "usp_PlaceOrder", "PROCEDURE"),
    ("dbo", "usp_CancelOrder", "PROCEDURE"),
    ("dbo", "usp_RecalculateLoyaltyTier", "PROCEDURE"),
    ("dbo", "usp_ReserveStock", "PROCEDURE"),
    ("dbo", "ufn_EffectivePrice", "FUNCTION"),
]

TRIGGERS = [
    ("trg_ProductPrice_Audit", "ProductPrice", "dbo", "AFTER", "UPDATE"),
    ("trg_SalesOrder_Status", "SalesOrder", "dbo", "AFTER", "UPDATE"),
]

VIEWS = [
    (
        "vw_ActiveProduct",
        "dbo",
        "CREATE VIEW [dbo].[vw_ActiveProduct] AS SELECT p.[ProductID], p.[Sku], p.[Name], "
        "b.[Name] AS [BrandName] FROM [Product] p JOIN [Brand] b "
        "ON b.[BrandID] = p.[BrandID] WHERE p.[IsActive] = 1",
    ),
]

# ---------------------------------------------------------------------------
# Workload: (calls per second, rows returned avg, avg ms, query text).
# ``calls/s`` is turned into a 24 h execution count, with seeded jitter.
# Grouped by the routing path each block is there to exercise; the module
# docstring and the PR explain which query reaches which path.
# ---------------------------------------------------------------------------

Q = tuple[float, float, float, str]

# Hot, bounded reads on read-mostly tables: cache overlay candidates (#296).
HOT_READS: list[Q] = [
    (
        42.0,
        1,
        0.4,
        "SELECT [ProductID], [Sku], [Name], [CategoryID], [BrandID], [IsActive] "
        "FROM [Product] WITH (NOLOCK) WHERE [ProductID] = @ProductID",
    ),
    (
        18.0,
        1,
        0.5,
        "SELECT TOP (1) [ListPrice], [SalePrice] FROM [ProductPrice] WITH (NOLOCK) "
        "WHERE [ProductID] = @ProductID AND [ValidTo] IS NULL ORDER BY [ValidFrom] DESC",
    ),
    (
        25.0,
        1,
        0.3,
        "SELECT [CustomerID], [CartJson], [ExpiresAt] FROM [UserSession] "
        "WHERE [SessionToken] = @SessionToken",
    ),
    (
        9.0,
        85,
        0.6,
        "SELECT [FlagKey], [Enabled], [Rollout] FROM [FeatureFlag]",
    ),
    (
        6.5,
        1,
        0.3,
        "SELECT [SettingValue] FROM [StoreSetting] WHERE [SettingKey] = @SettingKey",
    ),
    (
        3.2,
        24,
        0.9,
        "SELECT [CategoryID], [ParentCategoryID], [Name], [Slug] FROM [ProductCategory] "
        "WITH (NOLOCK) WHERE [ParentCategoryID] = @ParentCategoryID",
    ),
    (
        2.4,
        6,
        0.7,
        "SELECT [ImageID], [Url], [SortOrder] FROM [ProductImage] WITH (NOLOCK) "
        "WHERE [ProductID] = @ProductID ORDER BY [SortOrder]",
    ),
    (
        1.6,
        140,
        0.8,
        "SELECT [BrandID], [Name], [Country] FROM [Brand]",
    ),
]

# Key lookups and short reads (DynamoDB / DocumentDB vs Aurora candidates).
# The shipment-tracking lookup runs 5 calls/s: at that traffic DynamoDB scores
# dbo.Shipment the same as Aurora PostgreSQL, so it and the two Shipment
# writes tie between them -- a tie that survives the reality check.
LOOKUPS: list[Q] = [
    (
        4.8,
        1,
        0.6,
        "SELECT [CustomerID], [Email], [FirstName], [LastName], [Phone] FROM [Customer] "
        "WHERE [CustomerID] = @CustomerID",
    ),
    (
        2.1,
        1,
        0.8,
        "SELECT [CustomerID], [FirstName], [LastName] FROM [Customer] " "WHERE [Email] = @Email",
    ),
    (
        3.9,
        1,
        0.5,
        "SELECT [LoyaltyAccountID], [Tier], [Points] FROM [LoyaltyAccount] "
        "WHERE [CustomerID] = @CustomerID",
    ),
    (
        1.2,
        2,
        0.7,
        "SELECT [AddressID], [Line1], [City], [Region], [PostalCode], [IsDefault] "
        "FROM [CustomerAddress] WHERE [CustomerID] = @CustomerID",
    ),
    (
        3.4,
        1,
        0.6,
        "SELECT [SalesOrderID], [OrderNumber], [Status], [OrderDate], [TotalDue] "
        "FROM [SalesOrder] WHERE [OrderNumber] = @OrderNumber",
    ),
    (
        2.7,
        1,
        0.5,
        "SELECT [Status], [OrderDate] FROM [SalesOrder] WITH (NOLOCK) "
        "WHERE [SalesOrderID] = @SalesOrderID",
    ),
    (
        5.0,
        1,
        0.6,
        "SELECT [ShipmentID], [Carrier], [ShippedDate], [DeliveredDate] FROM [Shipment] "
        "WHERE [TrackingNumber] = @TrackingNumber",
    ),
    (
        0.6,
        1,
        0.4,
        "SELECT [PromotionID], [DiscountPct], [StartsOn], [EndsOn] FROM [Promotion] "
        "WHERE [Code] = @Code",
    ),
    (
        1.4,
        3,
        0.9,
        "SELECT [WarehouseID], [OnHand], [Reserved] FROM [StockLevel] "
        "WHERE [ProductID] = @ProductID",
    ),
    (
        0.8,
        1,
        0.4,
        "SELECT [OnHand] - [Reserved] AS [Available] FROM [StockLevel] "
        "WHERE [WarehouseID] = @WarehouseID AND [ProductID] = @ProductID",
    ),
    (
        0.35,
        1,
        0.6,
        "SELECT [TicketID], [Subject], [Status], [Priority] FROM [Ticket] "
        "WHERE [TicketID] = @TicketID",
    ),
    (
        0.2,
        1,
        0.5,
        "SELECT [ArticleID], [Title], [Body], [Tags] FROM [KnowledgeArticle] "
        "WHERE [ArticleID] = @ArticleID",
    ),
    (
        0.05,
        1,
        0.4,
        "SELECT [SupplierID], [Name], [ContactEmail] FROM [Supplier] "
        "WHERE [SupplierID] = @SupplierID",
    ),
    (
        0.04,
        12,
        0.3,
        "SELECT [WarehouseID], [Code], [Name] FROM [Warehouse]",
    ),
]

# Order reads. The three-table joins here and in REPORTING are significant
# JOINs on dbo.SalesOrder / dbo.SalesOrderLine, so union-find
# (``co_dependency.build_co_dependency_groups``) puts them in one
# co-dependency group that only Aurora PostgreSQL can serve in full: a group
# with no tie, for contrast with CATEGORY_GROUP.
ORDER_GROUP: list[Q] = [
    (
        1.9,
        4,
        2.1,
        "SELECT o.[SalesOrderID], o.[OrderNumber], l.[ProductID], l.[Quantity], l.[UnitPrice] "
        "FROM [SalesOrder] o JOIN [SalesOrderLine] l "
        "ON l.[SalesOrderID] = o.[SalesOrderID] WHERE o.[SalesOrderID] = @SalesOrderID",
    ),
    (
        0.75,
        10,
        6.4,
        "SELECT o.[SalesOrderID], o.[OrderNumber], o.[Status], o.[OrderDate], o.[TotalDue] "
        "FROM [SalesOrder] o WHERE o.[CustomerID] = @CustomerID "
        "ORDER BY o.[OrderDate] DESC OFFSET @Skip ROWS FETCH NEXT 10 ROWS ONLY",
    ),
    (
        0.42,
        4,
        5.2,
        "SELECT l.[ProductID], p.[Name], l.[Quantity], l.[UnitPrice] "
        "FROM [SalesOrderLine] l JOIN [Product] p "
        "ON p.[ProductID] = l.[ProductID] JOIN [SalesOrder] o "
        "ON o.[SalesOrderID] = l.[SalesOrderID] WHERE o.[CustomerID] = @CustomerID",
    ),
    (
        0.31,
        1,
        3.9,
        "SELECT o.[OrderNumber], pay.[Method], pay.[Amount], s.[TrackingNumber] "
        "FROM [SalesOrder] o JOIN [Payment] pay "
        "ON pay.[SalesOrderID] = o.[SalesOrderID] LEFT JOIN [Shipment] s "
        "ON s.[SalesOrderID] = o.[SalesOrderID] WHERE o.[SalesOrderID] = @SalesOrderID",
    ),
    (
        0.12,
        6,
        4.4,
        "SELECT h.[Status], h.[ChangedDate] FROM [OrderStatusHistory] h "
        "JOIN [SalesOrder] o ON o.[SalesOrderID] = h.[SalesOrderID] "
        "WHERE o.[OrderNumber] = @OrderNumber ORDER BY h.[ChangedDate]",
    ),
]

# Support reads. The two aggregates over Ticket/TicketMessage form a second
# co-dependency group, which Aurora PostgreSQL wins outright over DocumentDB.
SUPPORT_GROUP: list[Q] = [
    (
        0.28,
        8,
        2.6,
        "SELECT t.[TicketID], t.[Subject], m.[Author], m.[Body], m.[SentDate] "
        "FROM [Ticket] t JOIN [TicketMessage] m "
        "ON m.[TicketID] = t.[TicketID] WHERE t.[TicketID] = @TicketID",
    ),
    (
        0.09,
        20,
        7.5,
        "SELECT t.[TicketID], t.[Subject], t.[Priority], c.[Email] FROM [Ticket] t "
        "JOIN [Customer] c ON c.[CustomerID] = t.[CustomerID] "
        "WHERE t.[Status] = @Status ORDER BY t.[Priority] DESC, t.[OpenedDate]",
    ),
    (
        0.06,
        5,
        9.5,
        "SELECT t.[TicketID], COUNT(m.[MessageID]) AS [Messages] FROM [Ticket] t "
        "JOIN [TicketMessage] m ON m.[TicketID] = t.[TicketID] "
        "WHERE t.[CustomerID] = @CustomerID GROUP BY t.[TicketID]",
    ),
    (
        0.01,
        30,
        120.0,
        "SELECT m.[Author], COUNT(*) AS [Replies] FROM [TicketMessage] m "
        "JOIN [Ticket] t ON t.[TicketID] = m.[TicketID] WHERE t.[Status] = N'OPEN' "
        "GROUP BY m.[Author]",
    ),
]

# The category co-dependency group: two self-join aggregates on
# dbo.ProductCategory (the adjacency list behind the recursive CTE below). At
# the children-count's 0.5 calls/s, Aurora PostgreSQL and DocumentDB score
# ProductCategory the same, so the group's scores tie (tie_unresolved: SQL
# Server has no source-compatible engine to break it). The reality check
# later drops DocumentDB, which resolves the tie onto Aurora PostgreSQL.
CATEGORY_GROUP: list[Q] = [
    (
        0.5,
        260,
        48.0,
        "SELECT p.[CategoryID], p.[Name], COUNT(c.[CategoryID]) AS [Children] "
        "FROM [ProductCategory] p LEFT JOIN [ProductCategory] c "
        "ON c.[ParentCategoryID] = p.[CategoryID] GROUP BY p.[CategoryID], p.[Name]",
    ),
    (
        0.004,
        18,
        31.0,
        "SELECT p.[Name], COUNT(*) AS [Subcategories] FROM [ProductCategory] c "
        "JOIN [ProductCategory] p ON p.[CategoryID] = c.[ParentCategoryID] "
        "WHERE p.[ParentCategoryID] IS NULL GROUP BY p.[Name]",
    ),
]

# Writes: inserts/updates on the transactional tables.
WRITES: list[Q] = [
    (
        6.2,
        0,
        1.4,
        "INSERT INTO [UserSession] ([SessionToken], [CustomerID], [CartJson], [ExpiresAt]) "
        "VALUES (@SessionToken, @CustomerID, @CartJson, @ExpiresAt)",
    ),
    (
        2.2,
        0,
        0.9,
        "UPDATE [UserSession] SET [CartJson] = @CartJson, [ExpiresAt] = @ExpiresAt "
        "WHERE [SessionToken] = @SessionToken",
    ),
    (
        0.95,
        0,
        2.8,
        "INSERT INTO [SalesOrder] ([OrderNumber], [CustomerID], [Status], [OrderDate], "
        "[TotalDue], [ShipToAddressID]) VALUES (@OrderNumber, @CustomerID, N'PENDING', "
        "SYSUTCDATETIME(), @TotalDue, @ShipToAddressID)",
    ),
    (
        2.6,
        0,
        1.1,
        "INSERT INTO [SalesOrderLine] ([SalesOrderID], [ProductID], [Quantity], "
        "[UnitPrice]) VALUES (@SalesOrderID, @ProductID, @Quantity, @UnitPrice)",
    ),
    (
        0.9,
        0,
        1.6,
        "UPDATE [SalesOrder] SET [Status] = @Status WHERE [SalesOrderID] = @SalesOrderID",
    ),
    (
        0.94,
        0,
        1.9,
        "INSERT INTO [Payment] ([SalesOrderID], [Method], [Amount], [AuthCode]) "
        "VALUES (@SalesOrderID, @Method, @Amount, @AuthCode)",
    ),
    (
        1.8,
        0,
        0.8,
        "INSERT INTO [OrderStatusHistory] ([SalesOrderID], [Status], [ChangedDate]) "
        "VALUES (@SalesOrderID, @Status, SYSUTCDATETIME())",
    ),
    (
        0.7,
        0,
        1.3,
        "INSERT INTO [Shipment] ([SalesOrderID], [Carrier], [TrackingNumber]) "
        "VALUES (@SalesOrderID, @Carrier, @TrackingNumber)",
    ),
    (
        0.66,
        0,
        1.2,
        "UPDATE [Shipment] SET [DeliveredDate] = @DeliveredDate "
        "WHERE [TrackingNumber] = @TrackingNumber",
    ),
    (
        1.5,
        0,
        1.0,
        "UPDATE [StockLevel] SET [Reserved] = [Reserved] + @Quantity, "
        "[ModifiedDate] = SYSUTCDATETIME() WHERE [WarehouseID] = @WarehouseID "
        "AND [ProductID] = @ProductID",
    ),
    (
        4.1,
        0,
        0.7,
        "INSERT INTO [StockMovement] ([WarehouseID], [ProductID], [Delta], [Reason], "
        "[MovedDate]) VALUES (@WarehouseID, @ProductID, @Delta, @Reason, SYSUTCDATETIME())",
    ),
    (
        0.4,
        0,
        1.1,
        "UPDATE [LoyaltyAccount] SET [Points] = [Points] + @Points, "
        "[ModifiedDate] = SYSUTCDATETIME() WHERE [CustomerID] = @CustomerID",
    ),
    (
        0.11,
        0,
        2.4,
        "INSERT INTO [Customer] ([Email], [FirstName], [LastName], [Phone], [CreatedDate], "
        "[ModifiedDate]) VALUES (@Email, @FirstName, @LastName, @Phone, SYSUTCDATETIME(), "
        "SYSUTCDATETIME())",
    ),
    (
        0.06,
        0,
        1.7,
        "UPDATE [Customer] SET [Phone] = @Phone, [ModifiedDate] = SYSUTCDATETIME() "
        "WHERE [CustomerID] = @CustomerID",
    ),
    (
        0.05,
        0,
        1.5,
        "INSERT INTO [CustomerAddress] ([CustomerID], [Line1], [City], [Region], "
        "[PostalCode], [IsDefault]) VALUES (@CustomerID, @Line1, @City, @Region, @PostalCode, 0)",
    ),
    (
        0.08,
        0,
        2.2,
        "INSERT INTO [ProductReview] ([ProductID], [CustomerID], [Rating], [Title], "
        "[Body], [CreatedDate]) VALUES (@ProductID, @CustomerID, @Rating, @Title, @Body, "
        "SYSUTCDATETIME())",
    ),
    (
        0.07,
        0,
        1.9,
        "INSERT INTO [Ticket] ([CustomerID], [SalesOrderID], [Subject], [Status], "
        "[Priority], [OpenedDate]) VALUES (@CustomerID, @SalesOrderID, @Subject, N'OPEN', "
        "@Priority, SYSUTCDATETIME())",
    ),
    (
        0.19,
        0,
        1.4,
        "INSERT INTO [TicketMessage] ([TicketID], [Author], [Body], [SentDate]) "
        "VALUES (@TicketID, @Author, @Body, SYSUTCDATETIME())",
    ),
    (
        0.03,
        0,
        1.2,
        "UPDATE [Ticket] SET [Status] = N'CLOSED', [ClosedDate] = SYSUTCDATETIME() "
        "WHERE [TicketID] = @TicketID",
    ),
    (
        0.02,
        0,
        3.1,
        "UPDATE [ProductPrice] SET [ValidTo] = SYSUTCDATETIME() "
        "WHERE [ProductID] = @ProductID AND [ValidTo] IS NULL",
    ),
    (
        0.02,
        0,
        1.8,
        "INSERT INTO [ProductPrice] ([ProductID], [ListPrice], [SalePrice], "
        "[ValidFrom]) VALUES (@ProductID, @ListPrice, @SalePrice, SYSUTCDATETIME())",
    ),
    (
        0.02,
        0,
        0.9,
        "INSERT INTO [Audit].[PriceChangeLog] ([ProductID], [OldPrice], [NewPrice], [ChangedBy], "
        "[ChangedDate]) VALUES (@ProductID, @OldPrice, @NewPrice, @ChangedBy, SYSUTCDATETIME())",
    ),
    (
        8.5,
        0,
        0.4,
        "INSERT INTO [Audit].[EventLog] ([EventType], [EntityID], [Payload], [EventTime]) "
        "VALUES (@EventType, @EntityID, @Payload, SYSUTCDATETIME())",
    ),
    (
        0.01,
        0,
        950.0,
        "DELETE TOP (5000) FROM [UserSession] WHERE [ExpiresAt] < SYSUTCDATETIME()",
    ),
    (
        0.004,
        0,
        2.6,
        "INSERT INTO [PurchaseOrder] ([SupplierID], [WarehouseID], [Status], "
        "[OrderedDate]) VALUES (@SupplierID, @WarehouseID, N'OPEN', SYSUTCDATETIME())",
    ),
    (
        0.006,
        0,
        1.3,
        "INSERT INTO [PurchaseOrderLine] ([PurchaseOrderID], [ProductID], "
        "[Quantity], [UnitCost]) VALUES (@PurchaseOrderID, @ProductID, @Quantity, @UnitCost)",
    ),
]

# Search: a leading-wildcard LIKE (text_search signal, OpenSearch candidate),
# and an anchored prefix search for comparison.
SEARCH: list[Q] = [
    (
        1.3,
        24,
        180.0,
        "SELECT TOP (24) [ProductID], [Sku], [Name] FROM [Product] "
        "WHERE [Name] LIKE '%' + @Term + '%' OR [Description] LIKE '%' + @Term + '%' "
        "ORDER BY [Name]",
    ),
    (
        0.16,
        10,
        240.0,
        "SELECT TOP (10) [ArticleID], [Title] FROM [KnowledgeArticle] "
        "WHERE [Body] LIKE '%' + @Term + '%' ORDER BY [PublishedDate] DESC",
    ),
    (
        0.22,
        15,
        12.0,
        "SELECT TOP (15) [CustomerID], [Email], [LastName] FROM [Customer] "
        "WHERE [LastName] LIKE @Prefix + '%' ORDER BY [LastName]",
    ),
    (
        0.05,
        40,
        310.0,
        "SELECT r.[ReviewID], r.[Rating], r.[Title] FROM [ProductReview] r "
        "WHERE r.[ProductID] = @ProductID AND r.[Body] LIKE '%' + @Term + '%'",
    ),
]

# Reporting and aggregation (Aurora-shaped).
REPORTING: list[Q] = [
    (
        0.02,
        30,
        2_400.0,
        "SELECT CAST(o.[OrderDate] AS date) AS [Day], COUNT(*) AS [Orders], "
        "SUM(o.[TotalDue]) AS [Revenue] FROM [SalesOrder] o WITH (NOLOCK) "
        "WHERE o.[OrderDate] >= @StartDate AND o.[OrderDate] < @EndDate "
        "GROUP BY CAST(o.[OrderDate] AS date) ORDER BY [Day]",
    ),
    (
        0.008,
        50,
        5_800.0,
        "SELECT TOP (50) p.[ProductID], p.[Name], SUM(l.[Quantity]) AS [Units] "
        "FROM [SalesOrderLine] l JOIN [Product] p ON p.[ProductID] = l.[ProductID] "
        "JOIN [SalesOrder] o ON o.[SalesOrderID] = l.[SalesOrderID] "
        "WHERE o.[OrderDate] >= @StartDate GROUP BY p.[ProductID], p.[Name] ORDER BY [Units] DESC",
    ),
    (
        0.004,
        12,
        3_100.0,
        "SELECT c.[Region], COUNT(DISTINCT o.[CustomerID]) AS [Buyers], AVG(o.[TotalDue]) AS [Aov] "
        "FROM [SalesOrder] o JOIN [CustomerAddress] c "
        "ON c.[AddressID] = o.[ShipToAddressID] GROUP BY c.[Region]",
    ),
    (
        0.3,
        1,
        14.0,
        "SELECT AVG(CAST([Rating] AS decimal(4, 2))) AS [AvgRating], COUNT(*) AS [Reviews] "
        "FROM [ProductReview] WITH (NOLOCK) WHERE [ProductID] = @ProductID",
    ),
    (
        0.006,
        120,
        870.0,
        "SELECT s.[Name], COUNT(*) AS [OpenLines], SUM(pol.[Quantity] * pol.[UnitCost]) AS [Value] "
        "FROM [PurchaseOrder] po JOIN [Supplier] s "
        "ON s.[SupplierID] = po.[SupplierID] JOIN [PurchaseOrderLine] pol "
        "ON pol.[PurchaseOrderID] = po.[PurchaseOrderID] WHERE po.[Status] = N'OPEN' "
        "GROUP BY s.[Name]",
    ),
    (
        0.002,
        25,
        1_900.0,
        "SELECT t.[Priority], COUNT(*) AS [Open], AVG(DATEDIFF(hour, t.[OpenedDate], "
        "SYSUTCDATETIME())) AS [AgeHours] FROM [Ticket] t "
        "WHERE t.[Status] <> N'CLOSED' GROUP BY t.[Priority]",
    ),
    (
        0.003,
        500,
        4_200.0,
        "SELECT [EventType], COUNT_BIG(*) AS [Events] FROM [Audit].[EventLog] WITH (NOLOCK) "
        "WHERE [EventTime] >= DATEADD(day, -1, SYSUTCDATETIME()) GROUP BY [EventType]",
    ),
    (
        0.04,
        200,
        95.0,
        "SELECT TOP (200) [EventID], [EventType], [EntityID], [EventTime] FROM [Audit].[EventLog] "
        "WHERE [EntityID] = @EntityID AND [EventTime] BETWEEN @StartDate AND @EndDate "
        "ORDER BY [EventTime] DESC",
    ),
    (
        0.015,
        90,
        160.0,
        "SELECT [MovedDate], [Delta], [Reason] FROM [StockMovement] "
        "WHERE [ProductID] = @ProductID AND [MovedDate] >= @StartDate ORDER BY [MovedDate]",
    ),
    (
        0.012,
        30,
        65.0,
        "SELECT [ChangedDate], [OldPrice], [NewPrice], [ChangedBy] FROM [Audit].[PriceChangeLog] "
        "WHERE [ProductID] = @ProductID ORDER BY [ChangedDate] DESC",
    ),
    (
        0.05,
        3,
        22.0,
        "SELECT o.[SalesOrderID] FROM [SalesOrder] o WHERE o.[Status] = N'PAID' "
        "AND NOT EXISTS (SELECT 1 FROM [Shipment] s WHERE s.[SalesOrderID] = "
        "o.[SalesOrderID]) AND o.[OrderDate] < DATEADD(day, -2, SYSUTCDATETIME())",
    ),
]

# T-SQL features that tip (or reinforce) the Aurora choice: a recursive CTE,
# MERGE, NEXT VALUE FOR, JSON_VALUE / OPENJSON (aurora_choice.FEATURE_SIGNALS).
DIALECT: list[Q] = [
    (
        0.9,
        6,
        3.4,
        "WITH [Tree] AS (SELECT [CategoryID], [ParentCategoryID], [Name], 0 AS [Depth] "
        "FROM [ProductCategory] WHERE [CategoryID] = @CategoryID UNION ALL "
        "SELECT c.[CategoryID], c.[ParentCategoryID], c.[Name], t.[Depth] + 1 "
        "FROM [ProductCategory] c JOIN [Tree] t ON c.[ParentCategoryID] = t.[CategoryID]) "
        "SELECT [CategoryID], [Name], [Depth] FROM [Tree] OPTION (MAXRECURSION 16)",
    ),
    (
        0.02,
        0,
        640.0,
        "MERGE [StockLevel] AS tgt USING (SELECT [WarehouseID], [ProductID], "
        "SUM([Delta]) AS [Delta] FROM [StockMovement] WHERE [MovedDate] >= @Since "
        "GROUP BY [WarehouseID], [ProductID]) AS src ON tgt.[WarehouseID] = src.[WarehouseID] "
        "AND tgt.[ProductID] = src.[ProductID] WHEN MATCHED THEN UPDATE SET tgt.[OnHand] = "
        "tgt.[OnHand] + src.[Delta] WHEN NOT MATCHED THEN INSERT ([WarehouseID], [ProductID], "
        "[OnHand], [Reserved], [ModifiedDate]) VALUES (src.[WarehouseID], src.[ProductID], "
        "src.[Delta], 0, SYSUTCDATETIME());",
    ),
    (
        1.6,
        1,
        0.2,
        "SELECT NEXT VALUE FOR [dbo].[OrderNumberSeq] AS [NextOrderNumber]",
    ),
    (
        0.18,
        1,
        2.1,
        "SELECT [ProductID], JSON_VALUE([Specs], '$.weightGrams') AS [WeightGrams], "
        "JSON_VALUE([Specs], '$.material') AS [Material] FROM [Product] "
        "WHERE [ProductID] = @ProductID",
    ),
    (
        0.07,
        9,
        6.8,
        "SELECT s.[SessionToken], j.[ProductID], j.[Quantity] FROM [UserSession] s "
        "CROSS APPLY OPENJSON(s.[CartJson]) WITH ([ProductID] int '$.productId', "
        "[Quantity] int '$.qty') AS j WHERE s.[CustomerID] = @CustomerID",
    ),
]

# Stored-procedure calls and admin statements.
PROCS_AND_ADMIN: list[Q] = [
    (
        0.95,
        1,
        14.0,
        "EXEC [dbo].[usp_PlaceOrder] @CustomerID = @CustomerID, @CartJson = @CartJson",
    ),
    (
        0.03,
        0,
        9.0,
        "EXEC [dbo].[usp_CancelOrder] @SalesOrderID = @SalesOrderID, @Reason = @Reason",
    ),
    (
        0.01,
        0,
        45.0,
        "EXEC [dbo].[usp_RecalculateLoyaltyTier] @CustomerID = @CustomerID",
    ),
    (
        1.1,
        0,
        3.2,
        "EXEC [dbo].[usp_ReserveStock] @WarehouseID = @WarehouseID, "
        "@ProductID = @ProductID, @Quantity = @Quantity",
    ),
    (
        0.02,
        1,
        0.6,
        "SELECT [dbo].[ufn_EffectivePrice](@ProductID, SYSUTCDATETIME()) AS [Price]",
    ),
    (
        0.001,
        0,
        12_000.0,
        "ALTER INDEX [IX_StockMovement_ProductID_MovedDate] ON [StockMovement] " "REORGANIZE",
    ),
    (
        0.001,
        0,
        1.0,
        "SET TRANSACTION ISOLATION LEVEL READ COMMITTED",
    ),
]

# Browse and listing reads (OFFSET/FETCH, TOP with ORDER BY).
BROWSE: list[Q] = [
    (
        2.2,
        24,
        8.5,
        "SELECT p.[ProductID], p.[Sku], p.[Name], b.[Name] AS [Brand] FROM [Product] p "
        "WITH (NOLOCK) JOIN [Brand] b ON b.[BrandID] = p.[BrandID] "
        "WHERE p.[CategoryID] = @CategoryID AND p.[IsActive] = 1 ORDER BY p.[Name] "
        "OFFSET @Skip ROWS FETCH NEXT 24 ROWS ONLY",
    ),
    (
        0.6,
        10,
        11.0,
        "SELECT TOP (10) r.[ReviewID], r.[Rating], r.[Title], r.[CreatedDate] "
        "FROM [ProductReview] r WITH (NOLOCK) WHERE r.[ProductID] = @ProductID "
        "ORDER BY r.[CreatedDate] DESC",
    ),
    (
        0.45,
        12,
        4.0,
        "SELECT TOP (12) [ProductID], [Sku], [Name] FROM [Product] WITH (NOLOCK) "
        "WHERE [BrandID] = @BrandID AND [IsActive] = 1 ORDER BY [ModifiedDate] DESC",
    ),
    (
        0.25,
        5,
        2.2,
        "SELECT [NoteID], [Body], [CreatedBy], [CreatedDate] FROM [CustomerNote] "
        "WHERE [CustomerID] = @CustomerID ORDER BY [CreatedDate] DESC",
    ),
    (
        0.14,
        20,
        5.5,
        "SELECT [TicketID], [Subject], [Status], [OpenedDate] FROM [Ticket] "
        "WHERE [CustomerID] = @CustomerID ORDER BY [OpenedDate] DESC",
    ),
    (
        0.11,
        40,
        3.1,
        "SELECT [PromotionID], [Code], [DiscountPct] FROM [Promotion] "
        "WHERE @Today BETWEEN [StartsOn] AND [EndsOn] ORDER BY [EndsOn]",
    ),
    (
        0.04,
        25,
        9.0,
        "SELECT po.[PurchaseOrderID], po.[Status], po.[OrderedDate] FROM [PurchaseOrder] po "
        "WHERE po.[SupplierID] = @SupplierID ORDER BY po.[OrderedDate] DESC",
    ),
    (
        0.03,
        30,
        7.2,
        "SELECT pol.[ProductID], pol.[Quantity], pol.[UnitCost] FROM [PurchaseOrderLine] pol "
        "WHERE pol.[PurchaseOrderID] = @PurchaseOrderID",
    ),
    (
        0.2,
        3,
        1.4,
        "SELECT [PaymentID], [Method], [Amount], [CapturedDate] FROM [Payment] "
        "WHERE [SalesOrderID] = @SalesOrderID",
    ),
]

WORKLOAD: list[Q] = [
    *HOT_READS,
    *LOOKUPS,
    *ORDER_GROUP,
    *SUPPORT_GROUP,
    *CATEGORY_GROUP,
    *WRITES,
    *SEARCH,
    *REPORTING,
    *DIALECT,
    *PROCS_AND_ADMIN,
    *BROWSE,
]


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _digest(text: str) -> str:
    """A SQL Server ``query_hash`` rendered the way ``CONVERT(VARCHAR(40), h, 1)`` does."""
    return "0x" + hashlib.sha256(text.encode()).hexdigest()[:16].upper()


def _timestamp(rng: random.Random, start_hour: int, end_hour: int) -> str:
    hour = rng.randint(start_hour, end_hour)
    minute, second, ms = rng.randint(0, 59), rng.randint(0, 59), rng.randint(0, 999)
    day = 12 if hour < 24 else 13
    return f"2026-10-{day:02d} {hour % 24:02d}:{minute:02d}:{second:02d}.{ms:03d}"


def build_queries(rng: random.Random) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for cps, rows_avg, avg_ms, text in WORKLOAD:
        execs = max(10, int(round(cps * WINDOW_SECONDS * rng.uniform(0.94, 1.06))))
        avg = round(avg_ms * rng.uniform(0.9, 1.1), 3)
        total_rows = int(round(rows_avg * execs))
        reads_per_exec = max(2.0, rows_avg * rng.uniform(2.5, 6.0))
        if avg_ms >= 100:
            reads_per_exec *= 40
        logical_reads = int(round(reads_per_exec * execs))
        rows.append(
            {
                "digest": _digest(text),
                "query_text": text,
                "execution_count": execs,
                "total_time_ms": round(avg * execs, 3),
                "avg_time_ms": avg,
                "min_time_ms": round(avg * rng.uniform(0.2, 0.5), 3),
                "max_time_ms": round(avg * rng.uniform(4.0, 30.0), 3),
                "total_rows_sent": total_rows,
                "total_rows_examined": logical_reads,
                "total_rows_affected": total_rows,
                "total_cpu_ms": round(avg * execs * rng.uniform(0.5, 0.85), 3),
                "avg_cpu_time_ms": round(avg * rng.uniform(0.5, 0.85), 3),
                "avg_logical_reads": round(reads_per_exec, 3),
                "avg_physical_reads": round(reads_per_exec * rng.uniform(0.0, 0.05), 3),
                "first_seen": _timestamp(rng, 0, 5),
                "last_seen": _timestamp(rng, 24, 29),
            }
        )
    # collect-sqlserver.sql orders by total elapsed time, descending.
    rows.sort(key=lambda r: (-r["total_time_ms"], r["digest"]))
    return rows


_TYPE_BYTES = {
    "int": 4,
    "bigint": 8,
    "smallint": 2,
    "tinyint": 1,
    "bit": 1,
    "decimal": 9,
    "datetime2": 8,
    "date": 3,
}


def build_schema(rng: random.Random) -> dict[str, list[dict[str, Any]]]:
    tables, columns, indexes, fks, pks = [], [], [], [], []
    for t in sorted(TABLES, key=lambda t: (t["schema"], t["name"])):
        schema, name = t["schema"], t["name"]
        row_bytes = 0
        for pos, (col, typ, length, nullable, identity) in enumerate(t["columns"], start=1):
            numeric = typ == "decimal"
            row_bytes += _TYPE_BYTES.get(typ, (length or 400) * (2 if typ[0] == "n" else 1) // 2)
            columns.append(
                {
                    "table_name": name,
                    "schema_name": schema,
                    "column_name": col,
                    "ordinal_position": pos,
                    "data_type": typ,
                    "udt_name": typ,
                    "max_length": length if typ not in _TYPE_BYTES else _TYPE_BYTES[typ],
                    "numeric_precision": 18 if numeric else (10 if typ == "int" else 0),
                    "numeric_scale": 2 if numeric else 0,
                    "is_nullable": "YES" if nullable else "NO",
                    "is_identity": "YES" if identity else "NO",
                    "column_default": "(sysutcdatetime())" if col == "CreatedDate" else None,
                }
            )
        data_mb = round(t["rows"] * row_bytes / 1024 / 1024 * rng.uniform(1.1, 1.4), 2)
        index_mb = round(data_mb * (0.15 + 0.12 * len(t["indexes"])) * rng.uniform(0.9, 1.1), 2)
        tables.append(
            {
                "schema_name": schema,
                "table_name": name,
                "table_id": f"{schema}.{name}",
                "row_count": t["rows"],
                "data_size_mb": max(0.02, data_mb),
                "index_size_mb": max(0.02, index_mb),
            }
        )
        pk_name = f"PK_{name}"
        for ordinal, col in enumerate(t["pk"], start=1):
            pks.append(
                {
                    "table_name": name,
                    "schema_name": schema,
                    "column_name": col,
                    "ordinal_position": ordinal,
                }
            )
            indexes.append(
                {
                    "table_name": name,
                    "schema_name": schema,
                    "index_name": pk_name,
                    "column_name": col,
                    "key_ordinal": ordinal,
                    "non_unique": 0,
                    "index_type": "btree",
                }
            )
        for idx_name, idx_cols, unique in t["indexes"]:
            for ordinal, col in enumerate(idx_cols, start=1):
                indexes.append(
                    {
                        "table_name": name,
                        "schema_name": schema,
                        "index_name": idx_name,
                        "column_name": col,
                        "key_ordinal": ordinal,
                        "non_unique": 0 if unique else 1,
                        "index_type": "btree",
                    }
                )
        for fk_name, col, ref_table, ref_col in t["fks"]:
            fks.append(
                {
                    "table_name": name,
                    "schema_name": schema,
                    "constraint_name": fk_name,
                    "column_name": col,
                    "referenced_table_name": ref_table,
                    "referenced_column_name": ref_col,
                    "on_update": "NO ACTION",
                    "on_delete": "NO ACTION",
                    "constraint_column_id": 1,
                }
            )
    return {
        "tables": tables,
        "columns": columns,
        "indexes": indexes,
        "foreign_keys": fks,
        "primary_keys": pks,
    }


def build_collection(seed: int = SEED) -> dict[str, Any]:
    """The whole raw collection, as ``collect-sqlserver.sql`` would print it."""
    rng = random.Random(seed)  # nosec B311 -- seeded synthetic data, not security
    # Queries first: the collection script runs that section first, too.
    queries = build_queries(rng)
    schema = build_schema(rng)
    data_mb = sum(t["data_size_mb"] + t["index_size_mb"] for t in schema["tables"])
    return {
        "collection_version": "1.0",
        "collected_at": "2026-10-13T06:00:00.000Z",
        "metadata": {
            "database_name": SAMPLE,
            "engine": "sqlserver",
            "version_full": (
                "Microsoft SQL Server 2019 (RTM-CU22) - 15.0.4322.2 (X64) \n\t"
                "Standard Edition (64-bit) on Windows Server 2019 Standard 10.0 <X64>"
            ),
            "product_version": "15.0.4322.2",
            "edition": "Standard Edition (64-bit)",
            "product_level": "RTM",
            "database_size_gb": max(1, int(round(data_mb / 1024))),
            "collected_at": COLLECTED_AT,
        },
        **schema,
        "views": [
            {"view_name": n, "schema_name": s, "definition": d, "owner": "dbo"} for n, s, d in VIEWS
        ],
        "procedures": [
            {
                "routine_name": n,
                "schema_name": s,
                "routine_type": rt,
                "language": "TSQL",
            }
            for s, n, rt in PROCEDURES
        ],
        "triggers": [
            {
                "trigger_name": n,
                "table_name": t,
                "schema_name": s,
                "timing": timing,
                "event_type": ev,
            }
            for n, t, s, timing, ev in TRIGGERS
        ],
        "queries": queries,
        "global_stats": {
            "innodb_buffer_pool_read_requests": 918_442_310,
            "innodb_buffer_pool_reads": 4_118_224,
            "created_tmp_disk_tables": 0,
            "created_tmp_tables": 0,
            "threads_connected": 64,
            "questions": 1_904_118,
        },
        "io_stats": {
            "physical_reads_per_sec": 212.4,
            "physical_writes_per_sec": 96.8,
            "read_bytes_per_sec": 1_739_980.8,
            "write_bytes_per_sec": 905_216.0,
            "avg_read_latency_ms": 1.82,
            "avg_write_latency_ms": 0.94,
        },
        "wait_events": [
            {
                "event": "PAGEIOLATCH_SH",
                "total_waits": 1_820_441,
                "time_waited_ms": 2_904_118.0,
                "avg_wait_ms": 1.595,
                "wait_class": "System",
            },
            {
                "event": "WRITELOG",
                "total_waits": 940_212,
                "time_waited_ms": 1_120_877.0,
                "avg_wait_ms": 1.192,
                "wait_class": "System",
            },
            {
                "event": "LCK_M_U",
                "total_waits": 18_204,
                "time_waited_ms": 402_118.0,
                "avg_wait_ms": 22.09,
                "wait_class": "System",
            },
        ],
        "os_stats": {
            "cpu_count": 8,
            "physical_memory_gb": 64.0,
            "committed_memory_gb": 41.27,
            "db_uptime_hours": 1_412,
        },
    }


def render(seed: int = SEED) -> str:
    """The collection file's text: sqlcmd's column header line, then the JSON."""
    body = json.dumps(build_collection(seed), ensure_ascii=False, separators=(",", ":"))
    return "collection_output\n" + body + "\n"


def render_zip(seed: int = SEED) -> bytes:
    buf = io.BytesIO()
    info = zipfile.ZipInfo(MEMBER, date_time=ZIP_DATE)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(info, render(seed))
    return buf.getvalue()


def committed_text() -> str | None:
    """The committed zip's collection text, or ``None`` when there is no zip."""
    if not ZIP_PATH.exists():
        return None
    with zipfile.ZipFile(ZIP_PATH) as zf:
        return zf.read(MEMBER).decode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--check", action="store_true", help="exit 1 if the zip is stale")
    parser.add_argument("--json", type=Path, help="also write the bare collection JSON here")
    args = parser.parse_args(argv)

    data = render_zip(args.seed)
    if args.check:
        # Compare the member text, not the zip bytes: another deflate build
        # (e.g. zlib-ng) writes different bytes for the same content.
        current = committed_text()
        if current != render(args.seed):
            print(json.dumps({"status": "stale", "zip": str(ZIP_PATH.relative_to(REPO))}))
            return 1
        print(json.dumps({"status": "ok", "zip": str(ZIP_PATH.relative_to(REPO))}))
        return 0
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ZIP_PATH.write_bytes(data)
    if args.json:
        args.json.write_text(render(args.seed), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "complete",
                "zip": str(ZIP_PATH.relative_to(REPO)),
                "tables": len(TABLES),
                "queries": len(WORKLOAD),
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
